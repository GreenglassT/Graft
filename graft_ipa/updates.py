"""Whether the .debs in the tweaks library have newer versions published.

Both halves of this already existed and never met. The Tweaks library reads a
package's version out of the .deb it holds; the Repos browser and Canister both
know the newest version published for a package id. Nothing compared them, so
`ATweak 1.2.14` sat in the library while `1.3.2` was one pane away — and
the whole reason to open this app is that a tweak moved.

**Only from the tweak's own source** (2.3.1 A1). The newest version of a
package id used to be taken across every configured repo and every repository
canister.me indexes, and a SHA-256 only proves a file matches its own row: any
repo publishing the same id at a higher version won, and Update installed it.
Now each library file's source is the one recorded when it was fetched
(`tweak_sources`), else inferred from the file's own SHA-256 (which source
lists exactly these bytes), and only that source's versions count. Two sources,
read per package id:

  - the configured repos' *cached* Packages indexes (`repo_offers`), which cost
    no network and are already on disk from the last `--repo-packages`;
  - canister.me (`canister_offers`), one offer per repository it indexes.

An update is *verified* when its source is known, serves over HTTPS, and
publishes a SHA-256 for that version. Anything else (a source inferred
ambiguously or not at all, `http://`, no hash) is offered only with the
person's confirmation, and says why (`unverified_reason`).

A tweak that neither source knows is a normal outcome, not an error: anything
built from source lives in no index at all. It reports as "no published version
found" and the row simply says nothing.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import canister, repo, tweak_sources
from .errors import RepoError
from .repo import version_key
from .tweak_sources import Source

#: Why an update isn't verified (`unverified_reason`), in the order checked.
#: Inferred from the file's own hash and not yet confirmed by the person: any
#: index can list any hash, and a public build's is public (re-verify #1).
SOURCE_INFERRED = "source not confirmed yet: {source}"
SOURCE_UNKNOWN = "source unknown: {source}"
#: The recorded canister repository's URI isn't the one recorded (#3).
SOURCE_MOVED = "its source moved from {was} to {now}"
NOT_HTTPS = "{source} doesn't use HTTPS"
NO_CHECKSUM = "{source} lists no checksum for {version}"


@dataclass(frozen=True)
class Download:
    """One build to download: its URL, and the SHA-256 its source published."""

    url: str
    sha256: str | None


@dataclass(frozen=True)
class Offer:
    """What one source publishes for one package."""

    source: Source
    #: Its newest downloadable version, or None.
    version: str | None
    #: That version's builds, best architecture first.
    downloads: tuple[Download, ...] = ()
    #: Every SHA-256 it lists for the package, any version, lowercased: what a
    #: library file's own hash is matched against to infer its source.
    hashes: frozenset[str] = frozenset()
    #: A repo's cached index within `CACHE_MAX_AGE`. A stale one can still say
    #: where a file came from, never what's newest.
    fresh: bool = True


@dataclass(frozen=True)
class Availability:
    """The newest version published by one tweak's source, and how to get it."""

    version: str
    #: Where it comes from (`tweak_sources.Source`).
    origin: Source
    #: Exactly the builds whose version was compared, with their hashes: the
    #: download takes these and never resolves the package afresh, so the
    #: file fetched is the build that was compared, from the source that was
    #: shown.
    downloads: tuple[Download, ...] = ()
    verified: bool = False
    #: Why it isn't verified, for the person (`SOURCE_UNKNOWN`, `NOT_HTTPS`,
    #: `NO_CHECKSUM`); None when it is.
    unverified_reason: str | None = None

    @property
    def source(self) -> str:
        """Shown to the person, so it names a place they could go and look."""
        return self.origin.label


@dataclass(frozen=True)
class TweakStatus:
    """One library file, and what is published for the package it contains."""

    filename: str
    package_id: str | None
    installed: str | None
    available: Availability | None
    #: The lookup could not be performed at all — offline, or the service
    #: refused. Distinct from `available is None`, which means it was asked and
    #: nothing is published.
    unchecked: bool = False

    @property
    def update_available(self) -> bool:
        """Strictly newer, never merely different.

        `!=` would be wrong in the direction that matters: a library holding a
        *newer* build than the repo has — which is the normal state for anything
        compiled locally, and for a `1.1.8-jailed` sitting beside the repo's
        `1.1.8` — would nag forever to install an older one.
        """
        if self.installed is None or self.available is None:
            return False
        return version_key(self.available.version) > version_key(self.installed)

    @property
    def verified(self) -> bool:
        return self.available is not None and self.available.verified

    @property
    def confirm_key(self) -> str | None:
        """What `--trust-tweak` takes to confirm exactly this update."""
        return confirm_key(self.filename, self.available) if self.available else None


def confirm_key(filename: str, available: Availability) -> str:
    """`<filename>@<sha256>` over everything the person is shown and the
    download takes: the source (kind, id, URL), the version, and each build's
    URL and published hash (re-verify #2). A person's yes (`--trust-tweak`)
    is for this key alone, so an update that moved on since (another
    version, build or source) is refused rather than trusted by name."""
    origin = available.origin
    parts = [filename, origin.kind, origin.id, origin.url, available.version]
    for build in available.downloads:
        parts += [build.url, build.sha256 or ""]
    digest = hashlib.sha256()
    for part in parts:
        data = part.encode("utf-8")
        digest.update(f"{len(data)}:".encode() + data)
    return f"{filename}@{digest.hexdigest()}"


_CONFIRM_KEY = re.compile(r"^(?P<filename>.+)@(?P<digest>[0-9a-f]{64})$")


def confirm_key_file(key: str) -> str | None:
    """The library filename a `confirm_key` is for, or None when `key`
    isn't one."""
    found = _CONFIRM_KEY.match(key)
    return found["filename"] if found else None


#: How old a cached Packages index may be and still be trusted to answer.
#:
#: The cache is refreshed only when the user runs `--repo-packages`, so without
#: a bound a months-old index answers and pins `latest` below the live figure —
#: reporting a tweak as up to date on evidence it knows is stale, which is the
#: one claim this feature must not make. Past the bound a tweak from that repo
#: is unchecked: canister can't answer for it, being another source.
CACHE_MAX_AGE = 24 * 60 * 60


def repo_offers(cfg: Any, max_age: float = CACHE_MAX_AGE) -> dict[str, list[Offer]]:
    """Every package the configured repos' caches carry: one `Offer` per repo,
    in config order.

    Reads only what `--repo-packages` already downloaded. A repo that has
    never been fetched contributes nothing rather than being fetched here:
    this runs behind a button the user pressed to check versions, not to sync
    repos, and a silent multi-megabyte download is not what they asked for.
    """
    out: dict[str, list[Offer]] = {}
    for configured in cfg.repos:
        # Normalized, because that is what every *writer* hashes —
        # `_do_add_repo`, `_do_repo_packages` and `_do_repos` all normalize
        # first, and `cache_path` is a SHA-1 of the string it is given.
        try:
            url = repo.normalize_url(configured.url)
        except RepoError:
            continue
        cached = repo.cache_path(cfg.state_dir, url)
        try:
            fresh = time.time() - cached.stat().st_mtime <= max_age
            text = cached.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        source = Source(tweak_sources.REPO, url, url)
        by_id: dict[str, list[repo.RepoPackage]] = {}
        for package in repo.iter_packages(text):
            by_id.setdefault(package.package_id, []).append(package)
        for package_id, stanzas in by_id.items():
            hashes = frozenset(p.sha256.lower() for p in stanzas if p.sha256)
            # `filename` guard before `download_url`, which *raises* without
            # one: a virtual or truncated stanza is never "the newest".
            usable = [p for p in stanzas if p.version and p.filename]
            version = max((p.version for p in usable), key=version_key) if usable else None
            downloads = tuple(Download(repo.download_url(url, p), p.sha256)
                              for p in usable if p.version == version)
            out.setdefault(package_id, []).append(
                Offer(source, version, downloads, hashes, fresh=fresh))
    return out


class LookupUnavailable(Exception):
    """The source could not be reached — distinct from "nothing is published".

    Collapsing the two is how a machine with no network reported every tweak as
    up to date: `outdated` was 0 because nothing could be looked up, and the
    pane said so in the same words it uses when it has checked and is sure.
    """


class _Repositories:
    """canister's repositories, each asked for once per check, across threads."""

    def __init__(self) -> None:
        self._known: dict[str, canister.Repository] = {}
        self._lock = threading.Lock()

    def get(self, repo_id: str) -> canister.Repository:
        with self._lock:
            known = self._known.get(repo_id)
        if known is not None:
            return known
        found = canister.get_repository(repo_id)
        with self._lock:
            return self._known.setdefault(repo_id, found)


def canister_offers(package_id: str, repositories: _Repositories | None = None) -> list[Offer]:
    """What canister.me publishes for `package_id`: one `Offer` per repository
    it indexes, ordered by repository id. None published is `[]`.

    A 404 is an ordinary answer — anything built from source is in no index —
    and returns `[]`. Anything else (a timeout, DNS, a 503) raises
    `LookupUnavailable`, because "we could not ask" is not "there is nothing".
    """
    known = repositories if repositories is not None else _Repositories()
    try:
        rows = canister.get_packages(package_id)
    except canister.CanisterError as e:
        if getattr(e, "status", None) == 404:
            return []
        raise LookupUnavailable(str(e)) from e
    except Exception as e:
        raise LookupUnavailable(str(e)) from e
    by_repository: dict[str, list[canister.Package]] = {}
    for row in rows:
        if row.repository_id:
            by_repository.setdefault(row.repository_id, []).append(row)
    out: list[Offer] = []
    for repo_id in sorted(by_repository):
        mine = by_repository[repo_id]
        try:
            repository = known.get(repo_id)
        except Exception as e:
            raise LookupUnavailable(str(e)) from e
        hashes = frozenset(r.sha256.lower() for r in mine if r.sha256)
        usable = [r for r in mine if r.version and r.package_filename]
        version = max((r.version for r in usable), key=version_key) if usable else None
        builds = sorted((r for r in usable if r.version == version), key=canister._arch_rank)
        downloads = tuple(Download(canister.download_url(r, repository), r.sha256)
                          for r in builds) if repository.uri else ()
        out.append(Offer(Source(tweak_sources.CANISTER, repo_id, repository.uri.rstrip("/")),
                         version, downloads, hashes))
    return out


def _availability(offer: Offer, *, doubt: str | None) -> Availability:
    """`doubt`: why the source itself isn't settled (`SOURCE_INFERRED`,
    `SOURCE_UNKNOWN`, `SOURCE_MOVED`), or None when it's the one on record."""
    assert offer.version is not None
    hashed = tuple(d for d in offer.downloads if d.sha256)
    label = offer.source.label
    if doubt is not None:
        reason: str | None = doubt
    elif not offer.source.secure:
        reason = NOT_HTTPS.format(source=label)
    elif not hashed:
        reason = NO_CHECKSUM.format(source=label, version=offer.version)
    else:
        reason = None
    return Availability(offer.version, offer.source, hashed or offer.downloads,
                        verified=reason is None, unverified_reason=reason)


def _candidate_order(offers: list[Offer]) -> list[Offer]:
    """A choice that never depends on who offers the higher version:
    configured repos in config order, then canister by repository id (the
    order each list is in already)."""
    return ([o for o in offers if o.source.kind == tweak_sources.REPO]
            + [o for o in offers if o.source.kind == tweak_sources.CANISTER])


@dataclass
class _Answer:
    available: Availability | None = None
    unchecked: bool = False


def _from(offer: Offer | None, *, doubt: str | None = None, unknown: str | None = None) -> _Answer:
    """`offer`'s answer. `unknown`: a reason template formatted with its
    source (`SOURCE_INFERRED`, `SOURCE_UNKNOWN`)."""
    if offer is None or offer.version is None:
        return _Answer()
    if not offer.fresh:
        return _Answer(unchecked=True)
    if unknown is not None:
        doubt = unknown.format(source=offer.source.label)
    return _Answer(_availability(offer, doubt=doubt))


def resolve(file_sha: str | None, recorded: Source | None, from_repos: list[Offer],
            from_canister: list[Offer] | None) -> _Answer:
    """One library file's newest version, from its source alone.

    `recorded`: the source on record for these bytes. `from_canister` is None
    when canister couldn't be asked. Only a recorded source can be verified:
    one inferred from the file's hash is shown, and waits for the person's
    confirmation, after which the update records it."""
    if recorded is not None:
        if recorded.kind == tweak_sources.CANISTER and from_canister is None:
            return _Answer(unchecked=True)
        pool = from_repos if recorded.kind == tweak_sources.REPO else (from_canister or [])
        offer = next((o for o in pool if o.source.id == recorded.id), None)
        doubt = None
        if offer is not None and offer.source.location != recorded.location:
            # Matched by id, and somewhere else now (a re-registered domain,
            # or canister saying so): not the source on record.
            doubt = SOURCE_MOVED.format(was=recorded.label, now=offer.source.label)
        return _from(offer, doubt=doubt)
    if from_canister is None:
        return _Answer(unchecked=True)
    offers = _candidate_order(from_repos + from_canister)
    matches = [o for o in offers if file_sha and file_sha.lower() in o.hashes]
    locations: dict[str, Offer] = {}
    for offer in matches:
        # One place reached two ways is one source; canister's answer is live.
        held = locations.get(offer.source.location)
        if held is None or (offer.source.kind == tweak_sources.CANISTER
                            and held.source.kind != tweak_sources.CANISTER):
            locations[offer.source.location] = offer
    if len(locations) == 1:
        return _from(next(iter(locations.values())), unknown=SOURCE_INFERRED)
    if matches:
        return _from(matches[0], unknown=SOURCE_UNKNOWN)
    configured = [o for o in offers if o.source.kind == tweak_sources.REPO and o.version and o.fresh]
    if configured:
        return _from(configured[0], unknown=SOURCE_UNKNOWN)
    listed = [o for o in offers if o.source.kind == tweak_sources.CANISTER and o.version]
    if not listed:
        return _Answer()
    newest = max(listed, key=lambda o: version_key(o.version or ""))
    return _from(newest, unknown=SOURCE_UNKNOWN)


def statuses(cfg: Any, records: list[dict]) -> list[TweakStatus]:
    """Pair each library record with the newest version its own source
    publishes.

    `records` are `--tweaks`' own dicts, so this stays in step with whatever
    that lists — including two files carrying the same `package_id`, which is a
    real state (`1.1.8` and `1.1.8-jailed` side by side). Each file is answered
    for separately; they are different files, either may be the one attached
    to an app, and each has its own source.

    Canister is asked at most once per distinct package id, and only for ids
    with a file whose source isn't a recorded repo, concurrently: serially, a
    network that hangs rather than refusing froze the pane for minutes behind
    a button with no cancel.
    """
    from_repos = repo_offers(cfg)
    files: dict[str, tuple[str | None, Source | None]] = {}
    for record in records:
        if not record.get("package_id"):
            continue
        file_sha = tweak_sources.file_sha256(cfg.tweaks_dir / record["filename"])
        recorded = tweak_sources.recorded_for(cfg.state_dir, record["filename"], file_sha)
        files[record["filename"]] = (file_sha, recorded)
    # Canister can't change the answer for a file recorded as from a repo.
    wanted = list(dict.fromkeys(
        record["package_id"] for record in records
        if record.get("package_id")
        and getattr(files[record["filename"]][1], "kind", None) != tweak_sources.REPO
    ))
    from_canister: dict[str, list[Offer] | None] = {}
    if wanted:
        repositories = _Repositories()
        with ThreadPoolExecutor(max_workers=min(8, len(wanted))) as pool:
            futures = {pid: pool.submit(canister_offers, pid, repositories) for pid in wanted}
            for pid, future in futures.items():
                try:
                    from_canister[pid] = future.result()
                except LookupUnavailable:
                    from_canister[pid] = None

    out: list[TweakStatus] = []
    for record in records:
        package_id = record.get("package_id") or None
        answer = _Answer()
        if package_id:
            file_sha, recorded = files[record["filename"]]
            answer = resolve(file_sha, recorded, from_repos.get(package_id, []),
                             from_canister.get(package_id, []))
        out.append(TweakStatus(
            filename=record["filename"],
            package_id=package_id,
            installed=record.get("version") or None,
            available=answer.available,
            unchecked=answer.unchecked,
        ))
    return out


def as_event(status: TweakStatus) -> dict:
    """The `tweak-update` event's payload.

    `filename` is the key the GUI joins on, because that is what identifies a
    row in the library — `package_id` does not, since two files can share one.
    """
    available = status.available
    return {
        "filename": status.filename,
        "package_id": status.package_id,
        "installed": status.installed,
        "latest": available.version if available else None,
        "source": available.source if available else None,
        "update_available": status.update_available,
        # "we could not ask" — never folded into `latest: null`, which means
        # "we asked and nothing is published".
        "unchecked": status.unchecked,
        # Only a verified update runs without the person's confirmation
        # (`--trust-tweak`); `unverified_reason` says why one isn't.
        "verified": available.verified if available else False,
        "unverified_reason": available.unverified_reason if available else None,
        "confirm_key": status.confirm_key,
    }


def download(available: Availability, dest_dir: Path) -> Path:
    """The first of `available.downloads` that is there, into `dest_dir`, each
    checked against its own published hash. A 404 moves on to the next build
    of the same version, from the same source; anything else stops. Never
    another version, and never another source."""
    missing: list[str] = []
    for build in available.downloads:
        try:
            return canister.fetch_deb(build.url, dest_dir, build.sha256)
        except canister.CanisterError as e:
            if getattr(e, "status", None) != 404:
                raise
            missing.append(build.url)
    raise canister.CanisterError(
        f"no downloadable build of {available.version} at {available.source}"
        + "".join(f"{', ' if i else ': '}{url} -> 404" for i, url in enumerate(missing)),
        hint="the repo no longer serves this version — it may have been withdrawn",
    )
