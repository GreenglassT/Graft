"""canister.me v2 API client for searching and fetching iOS tweak .debs.

Endpoints used:
    GET https://api.canister.me/v2/jailbreak/package/search?q=<query>
    GET https://api.canister.me/v2/jailbreak/package/<package_id>
    GET https://api.canister.me/v2/jailbreak/repository/<repo_id>

A package's downloadable .deb lives at:
    {repository.uri}/{package.package_filename}

(That URL typically 302-redirects to a signed S3 URL — requests follows it transparently.)
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import USER_AGENT
from .errors import EXIT_CANISTER, GraftError

API_BASE = "https://api.canister.me/v2"
HTTP_TIMEOUT = 30


class CanisterError(GraftError):
    exit_code = EXIT_CANISTER


class SeveralRepositoriesError(CanisterError):
    """A fetch by package id alone that several Canister repositories list
    (re-verify #4): the newest of any of them won, and its source was then
    recorded as the tweak's. `repositories`: (id, URI) of each."""

    def __init__(self, package_id: str, repositories: list[tuple[str, str]]) -> None:
        listed = ", ".join(f"{repo_id} ({uri or 'no address'})" for repo_id, uri in repositories)
        super().__init__(
            f"several Canister repositories list {package_id}: {listed}",
            hint=f"pick the one you trust: graft --fetch {package_id} --fetch-repository <id>",
        )
        self.repositories = repositories


@dataclass
class Package:
    package_id: str
    name: str
    version: str
    description: str
    author: str
    section: str
    repository_id: str
    package_filename: str
    package_size: int | None
    sha256: str | None
    price: str
    depiction: str | None

    @property
    def deb_basename(self) -> str:
        # package_filename can include sub-paths; we just want the trailing filename.
        return self.package_filename.rsplit("/", 1)[-1] or f"{self.package_id}_{self.version}.deb"


@dataclass
class Repository:
    repo_id: str
    name: str
    uri: str


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    # Function-local, as in the other four modules that speak HTTP — see
    # `artwork._download_app_store_icon` for the measurement (~28ms off every
    # CLI invocation). `cli.py` imports this module at module scope, and the
    # five have to stay lazy together or none of them saves anything.
    #
    # `fetch_deb` below repeats this rather than sharing a module-level
    # accessor: two functions in this file need it, which is not enough
    # repetition to be worth the indirection, and a plain `import` is the form
    # the rest of this project already uses (`devices.py` repeats its
    # `pymobiledevice3` import in three functions for the same reason).
    import requests

    url = f"{API_BASE}{path}"
    try:
        resp = requests.get(
            url,
            params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        raise CanisterError(f"canister request failed: {e}") from e
    if resp.status_code != 200:
        error = CanisterError(
            f"canister returned HTTP {resp.status_code} for {url}",
            hint=resp.text[:200] if resp.text else None,
        )
        # Carried as data so callers can tell "this package does not exist"
        # (404, an ordinary answer for anything built from source) from "the
        # service did not answer" — which must never be reported as up to date.
        error.status = resp.status_code
        raise error
    try:
        return resp.json()
    except ValueError as e:
        raise CanisterError(f"canister returned non-JSON for {url}: {e}") from e


def _to_package(d: dict[str, Any]) -> Package:
    return Package(
        package_id=d.get("package_id") or d.get("package") or "",
        name=d.get("name") or "",
        version=d.get("version") or "",
        description=d.get("description") or "",
        author=d.get("author") or d.get("maintainer") or "",
        section=d.get("section") or "",
        repository_id=d.get("repository_id") or "",
        package_filename=d.get("package_filename") or "",
        package_size=d.get("package_size"),
        sha256=d.get("sha256_hash"),
        price=d.get("price") or "",
        depiction=d.get("depiction"),
    )


def search(query: str, limit: int = 20) -> list[Package]:
    if not query.strip():
        raise CanisterError("empty search query")
    data = _get(
        "/jailbreak/package/search",
        params={"q": query, "limit": limit},
    )
    raw = data.get("data") or []
    pkgs = [_to_package(p) for p in raw]
    # canister sometimes returns multiple versions of the same package; dedup by package_id,
    # keeping the one explicitly flagged latest, else the first occurrence.
    by_id: dict[str, Package] = {}
    for p in pkgs:
        if not p.package_id:
            continue
        if p.package_id in by_id:
            continue
        by_id[p.package_id] = p
    return list(by_id.values())[:limit]


#: Which build to try first when a version is published in several architectures.
#: Any of them carries an arm64 slice and cyan rewrites the substrate path, so
#: rootless vs rootful does not matter to this tool — this only fixes the order
#: so the answer is the same on every request.
_ARCH_ORDER = ("iphoneos-arm64", "iphoneos-arm64e", "iphoneos-arm")


def _arch_rank(package: Package) -> int:
    arch = variant_of(package.package_filename)
    return _ARCH_ORDER.index(arch) if arch in _ARCH_ORDER else len(_ARCH_ORDER)


def variant_of(filename: str) -> str | None:
    """The architecture a canister-style `name_version_arch.deb` names, if any."""
    m = re.search(r"_(iphoneos-[a-z0-9]+)\.deb$", filename)
    return m.group(1) if m else None


def get_packages(package_id: str) -> list[Package]:
    """Every published build of `package_id`: all versions, all architectures.

    Canister answers a package id with the whole catalog of it — about seventy
    rows for a long-lived tweak — in no order a caller may rely on. Callers that
    want "the" package go through `get_package`, which chooses.
    """
    data = _get(f"/jailbreak/package/{package_id}")
    payload = data.get("data")
    if not payload:
        raise CanisterError(f"package not found on canister: {package_id}")
    rows = payload if isinstance(payload, list) else [payload]
    packages = [_to_package(row) for row in rows]
    if not packages:
        raise CanisterError(f"package not found: {package_id}")
    return packages


def get_package(package_id: str) -> Package:
    """The newest *downloadable* published build of `package_id`.

    Not `rows[0]`: that was whichever row canister listed first *on this
    request*, so two calls a moment apart — the update check and the download —
    could disagree about what "latest" is, and a stale variant the repo no
    longer serves could win.

    The first of `_candidates(package_id, None)`, so this and the download
    choose from the same rows in the same order: highest version among rows
    that have a file, then the fixed architecture order, then
    `repository_id`. A version listed only by filename-less rows is never
    reported here, because nothing could ever download it.
    """
    return _candidates(package_id, None)[0]


def get_repository(repo_id: str) -> Repository:
    data = _get(f"/jailbreak/repository/{repo_id}")
    payload = data.get("data") or {}
    return Repository(
        repo_id=payload.get("id") or repo_id,
        name=payload.get("name") or repo_id,
        uri=payload.get("uri") or "",
    )


def download_url(package: Package, repo: Repository) -> str:
    if not repo.uri:
        raise CanisterError(f"repository {repo.repo_id} has no uri on canister")
    if not package.package_filename:
        raise CanisterError(f"package {package.package_id} has no package_filename on canister")
    base = repo.uri.rstrip("/")
    path = package.package_filename.lstrip("/")
    return f"{base}/{path}"


_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _candidates(package_id: str, version: str | None,
                repository_id: str | None = None, rows: list[Package] | None = None) -> list[Package]:
    """The builds to try, best first: one version, every architecture of it.

    With no `version` that is the newest one (`--fetch <id>`). With one, exactly
    that version — never a neighbour, so a caller that reported "latest 1.7.24"
    cannot be handed 1.7.20 because 1.7.24's file was missing. With
    `repository_id`, only that repository's rows: the one a search result
    named, so another repository listing the same id at a higher version
    can't win the fetch (2.3.1 A1).
    """
    from .repo import version_key

    # A row with no filename has no URL: `download_url` would raise on it
    # outside the 404 handling and hide the URLs that did 404. Filtered before
    # "newest" is decided, too, so a version that exists only as such rows is
    # never chosen as the one to fetch — `get_package` reports from here.
    listed = rows if rows is not None else get_packages(package_id)
    packages = [p for p in listed if p.package_filename
                and (repository_id is None or p.repository_id == repository_id)]
    if repository_id is not None and not packages:
        error = CanisterError(
            f"canister lists no downloadable build of {package_id} in repository {repository_id}",
            hint="search again: the repository may have dropped it",
        )
        error.status = 404
        raise error
    if version is None:
        if not packages:
            error = CanisterError(f"canister lists no downloadable build of {package_id}")
            # 404, like canister's own "no such package": there is nothing to
            # fetch or update to, which `updates.canister_offers` reads as
            # absent rather than as a lookup that failed.
            error.status = 404
            raise error
        version = max(packages, key=lambda p: version_key(p.version)).version
    same = [p for p in packages if p.version == version]
    if not same:
        raise CanisterError(
            f"canister lists no version {version} of {package_id}",
            hint="the index may have moved on since the version was reported — "
            "check for updates again",
        )
    # repository_id last, so same-version same-arch rows from two repos still
    # come out in one order on every request.
    return sorted(same, key=lambda p: (_arch_rank(p), p.repository_id))


def fetch_deb(
    target: str,
    dest_dir: Path,
    expected_sha: str | None = None,
    *,
    version: str | None = None,
    on_pick: Callable[[Package, Repository], None] | None = None,
    repository_id: str | None = None,
) -> Path:
    """Download a .deb to dest_dir. `target` is either a direct URL or a canister package id.

    For a URL target, `expected_sha` (when given) is verified against the downloaded
    bytes. For a package-id target the hash published by canister is used instead
    and any caller-supplied `expected_sha` is ignored.

    For a package-id target, `version` pins the build to download (default: the
    newest). Variants of that one version are tried in turn, moving on only when
    a file is genuinely absent (404) — any other failure stops the download.
    `on_pick` is told which row, from which repository, was downloaded: the
    tweak's source, which `--fetch` records (`tweak_sources`). `repository_id`
    takes only that repository's builds (`_candidates`).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    if _URL_RE.match(target):
        url = target
        filename = url.split("?", 1)[0].rsplit("/", 1)[-1]
        if not filename.endswith(".deb"):
            filename = filename + ".deb"
        return _download(url, dest_dir, filename, expected_sha)

    rows = get_packages(target)
    if repository_id is None:
        # By package id alone, only while one repository lists it: else the
        # newest of any of them would win, and be recorded as its source.
        listing = sorted({p.repository_id for p in rows if p.package_filename and p.repository_id})
        if len(listing) > 1:
            raise SeveralRepositoriesError(
                target, [(repo_id, get_repository(repo_id).uri.rstrip("/")) for repo_id in listing])
    candidates = _candidates(target, version, repository_id, rows=rows)
    repos: dict[str, Repository] = {}
    missing: list[str] = []
    for pkg in candidates:
        repo = repos.get(pkg.repository_id) or get_repository(pkg.repository_id)
        repos[pkg.repository_id] = repo
        url = download_url(pkg, repo)
        try:
            path = _download(url, dest_dir, pkg.deb_basename, pkg.sha256)
        except CanisterError as e:
            if getattr(e, "status", None) != 404:
                raise
            missing.append(url)
            continue
        if on_pick is not None:
            on_pick(pkg, repo)
        return path
    raise CanisterError(
        f"no downloadable build of {target} {candidates[0].version}: "
        + ", ".join(f"{u} -> 404" for u in missing),
        hint="the repo no longer serves this version — it may have been withdrawn",
    )


def _download(url: str, dest_dir: Path, filename: str, expected_sha: str | None) -> Path:
    # Function-local, exactly as in `_get` above — see that comment, and
    # `artwork._download_app_store_icon` for the measurement.
    import requests

    dest = dest_dir / filename
    tmp = dest.with_suffix(dest.suffix + ".partial")
    try:
        with requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            stream=True,
            timeout=HTTP_TIMEOUT * 4,
            allow_redirects=True,
        ) as r:
            r.raise_for_status()
            ct = r.headers.get("content-type", "").lower()
            if "html" in ct:
                raise CanisterError(
                    f"server returned HTML rather than a .deb for {url}",
                    hint="the package may have been removed or the repo requires auth",
                )
            h = hashlib.sha256()
            with tmp.open("wb") as f:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    h.update(chunk)
            digest = h.hexdigest()
    except requests.RequestException as e:
        tmp.unlink(missing_ok=True)
        error = CanisterError(f"download failed: {e}")
        # Carried as data, as in `_get`: the caller falling back to another
        # variant must tell "that file is not there" from "the server broke".
        error.status = getattr(getattr(e, "response", None), "status_code", None)
        raise error from e

    if expected_sha and digest != expected_sha.lower():
        tmp.unlink(missing_ok=True)
        raise CanisterError(
            f"sha256 mismatch for {filename}: got {digest}, expected {expected_sha.lower()}",
            hint="the file was corrupted in transit or the repo modified it — retry",
        )

    tmp.replace(dest)
    return dest
