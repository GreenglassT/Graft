"""APT repository client for browsing and downloading iOS tweak .debs.

A flat ("./"-style) APT repo lives at a base URL with:
    <base>/Release                     — a Debian-control metadata block
    <base>/Packages[.xz|.bz2|.gz]       — the package index

A package's .deb lives at <base>/<Filename> taken from its Packages stanza.
Downloads themselves go through the existing `graft --fetch <url>` path.
"""

from __future__ import annotations

import bz2
import gzip
import hashlib
import lzma
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import USER_AGENT
from .errors import RepoError

# `requests` is imported inside `_http_get`, not here — see that function's
# comment for the measured startup cost. This block is what keeps `->
# requests.Response` on its signature resolvable to a type checker (and to
# ruff's F821) without costing anything at runtime: `TYPE_CHECKING` is False
# in a running interpreter, so the body never executes.
if TYPE_CHECKING:
    import requests

HTTP_TIMEOUT = 30


@dataclass
class Repo:
    url: str
    name: str
    origin: str | None


@dataclass
class RepoPackage:
    package_id: str
    name: str
    version: str
    description: str
    author: str
    section: str
    filename: str
    size: int | None
    sha256: str | None
    depiction: str | None


_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def normalize_url(url: str) -> str:
    """Strip whitespace and a trailing slash; require an http(s) scheme."""
    cleaned = (url or "").strip()
    if not _URL_RE.match(cleaned):
        raise RepoError(
            f"not a valid repo URL: {url!r}",
            hint="a repo URL must start with http:// or https://",
        )
    return cleaned.rstrip("/")


def _upstream_version_key(version: str) -> tuple:
    """Key for the epoch-stripped portion, recursing on `~` prerelease markers.

    `base~rest` sorts before plain `base` because the present-`~` flag (0) is
    less than the absent flag (1).
    """
    base, sep, rest = version.partition("~")
    runs: list[tuple[int, object]] = []
    for run in re.findall(r"\d+|\D+", base):
        runs.append((1, int(run)) if run.isdigit() else (0, run))
    if sep:
        return (runs, 0, _upstream_version_key(rest))
    return (runs, 1, ())


def version_key(version: str) -> tuple:
    """Comparable key for a Debian-ish version string.

    Handles epochs (`1:2.0`) and `~` prereleases (`1.0~beta` sorts *before*
    `1.0`). Within a segment, digit runs compare numerically and other runs
    lexically. Not a full dpkg implementation, but correct for the version
    forms tweak repos publish.
    """
    version = version or ""
    epoch, sep, rest = version.partition(":")
    if sep and epoch.isdigit():
        epoch_val = int(epoch)
        version = rest
    else:
        epoch_val = 0
    return (epoch_val, *_upstream_version_key(version))


# Historical private name. Kept so repo.py's callers and the existing tests
# don't churn; new callers should use `version_key`.
_version_key = version_key


def cache_path(state_dir: Path, url: str) -> Path:
    """Where the raw decompressed Packages index for `url` is cached on disk."""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return state_dir / "repos" / f"{digest}.packages"


def download_url(repo_url: str, pkg: RepoPackage) -> str:
    """Absolute URL of a package's .deb.

    A Filename that is itself an absolute http(s) URL is used as-is. Otherwise
    it is joined under the repo base, after stripping a leading slash and any
    leading './' segments (flat APT repos commonly write './debs/...').
    """
    if not pkg.filename:
        raise RepoError(f"package {pkg.package_id} has no Filename field")
    fn = pkg.filename.strip()
    if _URL_RE.match(fn):
        return fn
    fn = fn.lstrip("/")
    while fn.startswith("./"):
        fn = fn[2:]
    return f"{repo_url.rstrip('/')}/{fn}"


def _parse_stanza(block: str) -> dict[str, str]:
    """Parse one Debian-control stanza into a {field: value} dict.

    Continuation lines (starting with a space or tab) are appended to the
    previous field's value.
    """
    fields: dict[str, str] = {}
    current: str | None = None
    for line in block.splitlines():
        if not line.strip():
            continue
        if line[0] in (" ", "\t") and current is not None:
            fields[current] += " " + line.strip()
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        current = key.strip()
        fields[current] = value.strip()
    return fields


def _to_repo_package(fields: dict[str, str]) -> RepoPackage | None:
    """Build a RepoPackage from a parsed stanza (Author falls back to Maintainer,
    Depiction to SileoDepiction). Returns None if the stanza has no Package field."""
    package_id = fields.get("Package", "")
    if not package_id:
        return None
    size_raw = fields.get("Size")
    try:
        size = int(size_raw) if size_raw else None
    except ValueError:
        size = None
    return RepoPackage(
        package_id=package_id,
        name=fields.get("Name") or package_id,
        version=fields.get("Version", ""),
        description=fields.get("Description", ""),
        author=fields.get("Author") or fields.get("Maintainer") or "",
        section=fields.get("Section", ""),
        filename=fields.get("Filename", ""),
        size=size,
        sha256=fields.get("SHA256"),
        depiction=fields.get("Depiction") or fields.get("SileoDepiction"),
    )


def parse_packages(text: str) -> list[RepoPackage]:
    """Parse a Packages index into RepoPackages, deduped to the highest version."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    by_id: dict[str, RepoPackage] = {}
    for block in re.split(r"\n\n+", text):
        if not block.strip():
            continue
        pkg = _to_repo_package(_parse_stanza(block))
        if pkg is None:
            continue
        existing = by_id.get(pkg.package_id)
        if existing is None or _version_key(pkg.version) > _version_key(existing.version):
            by_id[pkg.package_id] = pkg
    return list(by_id.values())


def _http_get(url: str) -> requests.Response:
    # Function-local, as in the other four modules that speak HTTP — see
    # `artwork._download_app_store_icon` for the measurement (~28ms off every
    # CLI invocation). `cli.py` imports this module at module scope, and the
    # five have to stay lazy together or none of them saves anything.
    #
    # The signature above still annotates a `requests.Response`, and is safe
    # to: `from __future__ import annotations` at the top of this file makes
    # every annotation a string that is never evaluated, and nothing in this
    # project calls `typing.get_type_hints`, which is the one thing that would
    # resolve it and fail.
    import requests

    try:
        return requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        raise RepoError(f"request to {url} failed: {e}") from e


# (filename suffix, decompressor) ordered most- to least-preferred.
_PACKAGE_VARIANTS: list[tuple[str, object]] = [
    ("Packages.xz", lzma.decompress),
    ("Packages.bz2", bz2.decompress),
    ("Packages.gz", gzip.decompress),
    ("Packages", None),
]


def fetch_packages(url: str) -> str:
    """Fetch and decompress a repo's Packages index. Tries xz/bz2/gz/plain."""
    base = url.rstrip("/")
    tried: list[str] = []
    for suffix, decompress in _PACKAGE_VARIANTS:
        variant_url = f"{base}/{suffix}"
        tried.append(suffix)
        resp = _http_get(variant_url)
        if resp.status_code != 200:
            continue
        try:
            raw = decompress(resp.content) if decompress else resp.content
            text = raw.decode("utf-8", errors="replace")
        except (lzma.LZMAError, OSError, ValueError, EOFError):
            continue
        if "Package:" not in text:
            continue  # 200 but not actually an index (e.g. a server error page)
        return text
    raise RepoError(
        f"no Packages index found at {base}",
        hint=f"tried: {', '.join(tried)} — is this a flat APT repo?",
    )


def fetch_release(url: str) -> Repo:
    """Fetch and parse a repo's Release file. Used to validate a repo on add."""
    base = url.rstrip("/")
    resp = _http_get(f"{base}/Release")
    if resp.status_code != 200:
        raise RepoError(
            f"no Release file at {base} (HTTP {resp.status_code})",
            hint="this URL is not a valid APT repository, or requires login",
        )
    fields = _parse_stanza(resp.text)
    name = fields.get("Origin") or fields.get("Label") or base
    return Repo(url=base, name=name, origin=fields.get("Origin"))
