"""Where each library tweak came from, and how a download gets into the library.

An update of a tweak comes only from the tweak's own source (2.3.1 A1): the
APT repo or canister repository it was fetched from. That source is recorded
here when a tweak is fetched or updated, per library file, beside the file's
own SHA-256, so a record never vouches for a different file put under the same
name. A tweak with no record (every library from before 2.3.1) has its source
inferred on each check (`updates.statuses`); it's recorded once that tweak is
updated. A check never writes here.

`tweak-sources.json` in the state folder: read leniently (a missing, broken or
foreign file records nothing, and one that isn't a regular file or is too big
isn't read: `data_dirs.read_small_json`), written owner-only and whole
(`data_dirs.private_write`).

And the library's filenames are Graft's (2.3.1 A2): a download is checked to be
a .deb of the package and version expected (`deb.identity`), named
`<package>_<version>[_<architecture>].deb` from its own control stanza, and
never moved over a different file (`place`).
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import data_dirs, deb, repo
from .canister import CanisterError
from .errors import ConfigError, RepoError

FILENAME = "tweak-sources.json"
_FORMAT = 1

#: A source's kinds: an APT repo the person added (`id` and `url` are its
#: normalized URL), or a repository canister.me indexes (`id` is canister's
#: `repository_id`, `url` that repository's URI).
REPO = "repo"
CANISTER = "canister"
_KINDS = (REPO, CANISTER)


class TweakFileError(CanisterError):
    """A download refused before it reached the library: not a .deb, not the
    package or version asked for, or a different file already has its name."""


class UntrustedSourceError(ConfigError):
    """A tweak update from a source that isn't verified, which the person
    didn't confirm (`--trust-tweak`)."""


@dataclass(frozen=True)
class Source:
    """Where a tweak comes from."""

    kind: str
    id: str
    #: Where its files are served: the repo URL, or the canister repository's
    #: URI. Empty when canister didn't say.
    url: str

    @property
    def label(self) -> str:
        """What a person is shown: somewhere they could go and look."""
        return self.url or f"canister repository {self.id}"

    @property
    def secure(self) -> bool:
        return self.url.lower().startswith("https://")

    @property
    def location(self) -> str:
        """The same place, however it was reached: a configured repo and the
        canister repository with the same URI are one source."""
        return self.url.strip().rstrip("/").lower() if self.url else f"{self.kind}:{self.id}"


def file_sha256(path: Path) -> str | None:
    """`path`'s SHA-256, or None when it can't be read."""
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _path(state_dir: Path) -> Path:
    return state_dir / FILENAME


def _load(state_dir: Path) -> dict[str, dict[str, str]]:
    raw = data_dirs.read_small_json(_path(state_dir))
    if not isinstance(raw, dict) or raw.get("format") != _FORMAT:
        return {}
    tweaks = raw.get("tweaks")
    if not isinstance(tweaks, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for filename, entry in tweaks.items():
        if not isinstance(filename, str) or not isinstance(entry, dict):
            continue
        fields = {k: entry.get(k) for k in ("kind", "id", "url", "sha256")}
        if fields["kind"] not in _KINDS or not all(isinstance(v, str) for v in fields.values()):
            continue
        if not fields["id"] or not fields["sha256"]:
            continue
        out[filename] = fields  # type: ignore[assignment]
    return out


def recorded_for(state_dir: Path, filename: str, sha256: str | None) -> Source | None:
    """The source recorded for library file `filename`, when it was recorded
    for these bytes (`sha256`); else None."""
    if not sha256:
        return None
    entry = _load(state_dir).get(filename)
    if entry is None or entry["sha256"] != sha256.lower():
        return None
    return Source(entry["kind"], entry["id"], entry["url"])


#: How long a writer waits for another to finish: far longer than a write.
_LOCK_TIMEOUT = 10.0


@contextlib.contextmanager
def _locked(state_dir: Path) -> Iterator[None]:
    """An exclusive `flock` on `tweak-sources.json.lock` for a read, change
    and write: a fetch and an update writing at once would otherwise each
    write what they read, and the second would drop the first's record.
    Proceeds unlocked where the lock file can't be made or the filesystem
    can't lock; raises `OSError` when another writer holds it too long."""
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(state_dir / f"{FILENAME}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        yield
        return
    try:
        deadline = time.monotonic() + _LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError(errno.EAGAIN, "another graft is writing tweak-sources.json") from None
                time.sleep(0.05)
            except OSError:
                break  # this filesystem can't lock: as before, unlocked
        yield
    finally:
        os.close(fd)  # releases the lock


def record(state_dir: Path, tweaks_dir: Path, filename: str, source: Source, sha256: str) -> None:
    """Record that library file `filename` (these bytes) came from `source`.
    Records for files no longer in the library are dropped. Read and written
    under `_locked`, so another writer's record isn't lost. Raises `OSError`
    when the file can't be written."""
    with _locked(state_dir):
        tweaks: dict[str, Any] = {
            name: entry for name, entry in _load(state_dir).items()
            if name != filename and (tweaks_dir / name).is_file()
        }
        tweaks[filename] = {"kind": source.kind, "id": source.id, "url": source.url,
                            "sha256": sha256.lower()}
        data = json.dumps({"format": _FORMAT, "tweaks": tweaks}, sort_keys=True,
                          indent=1).encode("utf-8")
        data_dirs.private_write(data, _path(state_dir))


_UNSAFE = re.compile(r"[^A-Za-z0-9.+~-]")


def library_name(package: str, version: str, architecture: str | None) -> str:
    """The library filename Graft gives a .deb: `<package>_<version>[_<arch>].deb`,
    with anything but letters, digits and `.+~-` as `_`, and never hidden."""
    parts = [package, version] + ([architecture] if architecture else [])
    name = "_".join(_UNSAFE.sub("_", part) for part in parts)
    return name.lstrip(".") + ".deb"


def _same_file(a: Path, b: Path) -> bool:
    return a.is_file() and not a.is_symlink() and file_sha256(a) == file_sha256(b)


def checked_name(downloaded: Path, tweaks_dir: Path, *, package_id: str | None = None,
                 version: str | None = None) -> str:
    """The library name `place` would give `downloaded`, after its checks:
    a .deb Graft can read, of `package_id` at `version` when they're given,
    whose name no different file in `tweaks_dir` has. Raises `TweakFileError`.
    """
    found = deb.identity(downloaded)
    if found is None:
        raise TweakFileError(
            "the download isn't a .deb Graft can read, so it wasn't added",
            hint="the repo may have sent an error page or something else; nothing in your "
                 "tweaks library changed",
        )
    package, got_version, architecture = found
    if package_id is not None and package != package_id:
        raise TweakFileError(
            f"the download is {package}, not {package_id}, so it wasn't added",
            hint="the repo's index and its file disagree; nothing in your tweaks library changed",
        )
    if version is not None and got_version != version:
        raise TweakFileError(
            f"the download is {package} {got_version}, not {version}, so it wasn't added",
            hint="the repo's index and its file disagree; nothing in your tweaks library changed",
        )
    name = library_name(package, got_version, architecture)
    dest = tweaks_dir / name
    if (dest.exists() or dest.is_symlink()) and not _same_file(dest, downloaded):
        raise _taken(name)
    return name


def _taken(name: str) -> TweakFileError:
    return TweakFileError(
        f"a different {name} is already in your tweaks library, so the download wasn't added",
        hint=f"if you want the new one, delete {name} in Tweaks first",
    )


def place(downloaded: Path, tweaks_dir: Path, *, package_id: str | None = None,
          version: str | None = None) -> Path:
    """Move `downloaded` into the library under Graft's name for it, and
    return where it is.

    Refused (`TweakFileError`, and `downloaded` is left where it is) when it
    fails `checked_name`'s checks. The same bytes already there are reused.
    The move is a hard link, which fails rather than replace a file that
    appeared meanwhile; where there are no hard links (exFAT, some SMB
    shares, a download folder on another volume), a copy that never replaces
    one either (`_copy_into_place`).
    """
    tweaks_dir.mkdir(parents=True, exist_ok=True)
    dest = tweaks_dir / checked_name(downloaded, tweaks_dir, package_id=package_id, version=version)
    try:
        os.link(downloaded, dest)
    except FileExistsError:
        if not _same_file(dest, downloaded):
            raise _taken(dest.name) from None
    except OSError:
        _copy_into_place(downloaded, dest)
    downloaded.unlink(missing_ok=True)
    return dest


def _copy_into_place(downloaded: Path, dest: Path) -> None:
    """`downloaded`'s bytes at `dest`, never over a file this didn't make.

    Copied into a fresh dotfile beside `dest` (so `--tweaks` never lists it
    half-written), then `dest`'s name is claimed with an exclusive-create
    placeholder, and the copy renamed over that placeholder, its own. A file
    already at `dest` with the same bytes is reused; with other bytes,
    refused. Nothing is left behind either way.
    """
    fd, name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".partial", dir=dest.parent)
    copy = Path(name)
    try:
        with os.fdopen(fd, "wb") as out, downloaded.open("rb") as src:
            shutil.copyfileobj(src, out, 1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(copy, 0o644)
        try:
            claim = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        except FileExistsError:
            if _same_file(dest, copy):
                return
            raise _taken(dest.name) from None
        os.close(claim)
        try:
            os.replace(copy, dest)
        except BaseException:
            dest.unlink(missing_ok=True)  # the placeholder, ours
            raise
    finally:
        copy.unlink(missing_ok=True)


def repo_source_for(repos: Any, url: str) -> Source | None:
    """The configured repo `url` is under (the longest match), as a source,
    or None: a download URL from the Repos browser is the repo's own URL
    and the package's `Filename`."""
    best: str | None = None
    for configured in repos:
        try:
            base = repo.normalize_url(configured.url)
        except RepoError:
            continue
        if url.startswith(base + "/") and (best is None or len(base) > len(best)):
            best = base
    return Source(REPO, best, best) if best else None


def untrusted(filename: str, version: str, source: str, reason: str | None,
              key: str) -> UntrustedSourceError:
    """The refusal of an update the person didn't confirm. `key`: its
    `confirm_key`, which `--trust-tweak` takes."""
    return UntrustedSourceError(
        f"the update of {filename} to {version} isn't verified ({reason}), so it wasn't downloaded",
        hint=f"if you trust {source}, confirm it: graft --update-tweak {filename} --trust-tweak {key}",
    )


#: The update the person confirmed isn't the one the source offers now.
TRUST_CHANGED = ("{filename} changed since you confirmed it: it's now {version} from {source}, "
                 "so it wasn't updated")


def trust_changed(filename: str, version: str, source: str) -> UntrustedSourceError:
    return UntrustedSourceError(
        TRUST_CHANGED.format(filename=filename, version=version, source=source),
        hint="look at it again, and confirm it if you trust it",
    )
