"""Whether the .debs in the tweaks library have newer versions published.

Both halves of this already existed and never met. The Tweaks library reads a
package's version out of the .deb it holds; the Repos browser and Canister both
know the newest version published for a package id. Nothing compared them, so
`ATweak 1.2.14` sat in the library while `1.3.2` was one pane away — and
the whole reason to open this app is that a tweak moved.

Two sources, in this order:

  - the configured repos' *cached* Packages indexes, which cost no network and
    are already on disk from the last `--repo-packages`;
  - canister.me, for everything the configured repos don't carry — which is
    most of it, since a tweak fetched through the Canister pane never required
    adding its repo.

A tweak that neither source knows is a normal outcome, not an error: anything
built from source lives in no index at all. It reports as "no published version
found" and the row simply says nothing.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import canister, repo
from .config import Config
from .errors import RepoError
from .repo import version_key


@dataclass(frozen=True)
class Availability:
    """The newest published version of one package, and where that was found."""

    version: str
    #: A repo URL, or `canister`. Shown to the user, so it has to name a place
    #: they could go and look.
    source: str
    #: What to hand `canister.fetch_deb` to actually get this build — a package
    #: id for a canister answer, an absolute .deb URL for a repo one.
    #:
    #: Carried rather than re-derived at download time, because "newest version"
    #: and "the file that *is* that version" have to come from the same place.
    #: Re-resolving a repo's answer through canister would quietly download a
    #: different build than the one whose version number was compared, which is
    #: the kind of mismatch nobody would ever notice.
    download: str = ""
    #: The hash the source published, when it published one.
    #:
    #: `fetch_deb` verifies a package-id target against canister's own hash but
    #: can only verify a *URL* target against a hash the caller hands it — so
    #: without carrying this, the repo-sourced download was the one fetch path
    #: in the app that wrote an unverified .deb and then re-pointed every app
    #: at it. `--fetch --fetch-sha` has always checked.
    sha256: str | None = None


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


#: How old a cached Packages index may be and still be trusted to answer.
#:
#: The cache is refreshed only when the user runs `--repo-packages`, so without
#: a bound a months-old index answers ahead of canister and pins `latest` below
#: the live figure — reporting a tweak as up to date on evidence it knows is
#: stale, which is the one claim this feature must not make. Past the bound the
#: package falls through to canister, which costs a request and is current.
CACHE_MAX_AGE = 24 * 60 * 60


def repo_catalog(cfg: Config, max_age: float = CACHE_MAX_AGE) -> dict[str, Availability]:
    """The newest version of every package the configured repos' caches carry.

    Reads only what `--repo-packages` already downloaded, and only if that was
    recent. A repo that has never been fetched contributes nothing rather than
    being fetched here: this runs behind a button the user pressed to check
    versions, not to sync repos, and a silent multi-megabyte download is not
    what they asked for.
    """
    best: dict[str, Availability] = {}
    for configured in cfg.repos:
        # Normalized, because that is what every *writer* hashes —
        # `_do_add_repo`, `_do_repo_packages` and `_do_repos` all normalize
        # first, and `cache_path` is a SHA-1 of the string it is given.
        # `ConfiguredRepo.from_entry` is deliberately lenient about
        # hand-written entries, so a trailing slash in config.yaml hashed to a
        # different file, `except OSError` swallowed the miss, and the repo
        # silently contributed nothing to the catalog it was configured for.
        try:
            url = repo.normalize_url(configured.url)
        except RepoError:
            continue
        cached = repo.cache_path(cfg.state_dir, url)
        try:
            if time.time() - cached.stat().st_mtime > max_age:
                continue
            text = cached.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for package in repo.parse_packages(text):
            # `filename` guard before `download_url`, which *raises* without
            # one — the same guard `_do_repo_packages` applies for the same
            # reason. A single virtual or truncated stanza in any cached index
            # would otherwise abort the whole check, taking down update
            # checking for every tweak including the ones canister answers.
            if not package.version or not package.filename:
                continue
            current = best.get(package.package_id)
            if current is None or version_key(package.version) > version_key(current.version):
                best[package.package_id] = Availability(
                    package.version, url, repo.download_url(url, package), package.sha256
                )
    return best


class LookupUnavailable(Exception):
    """The source could not be reached — distinct from "nothing is published".

    Collapsing the two is how a machine with no network reported every tweak as
    up to date: `outdated` was 0 because nothing could be looked up, and the
    pane said so in the same words it uses when it has checked and is sure.
    """


def canister_latest(package_id: str) -> Availability | None:
    """What canister.me publishes for `package_id`, or None if nothing is.

    A 404 is an ordinary answer — anything built from source is in no index —
    and returns None. Anything else (a timeout, DNS, a 503) raises
    `LookupUnavailable`, because "we could not ask" is not "there is nothing".
    """
    try:
        package = canister.get_package(package_id)
    except canister.CanisterError as e:
        if getattr(e, "status", None) == 404:
            return None
        raise LookupUnavailable(str(e)) from e
    except Exception as e:
        raise LookupUnavailable(str(e)) from e
    # The package id, not a URL, and no hash: `fetch_deb` resolves the id
    # through the same two calls this one made and verifies against canister's
    # own published hash, which is fresher than anything cached here. (The
    # update path pins the resolution to `package.version` — see
    # `_do_update_tweak` — so the two cannot disagree about which build.)
    return Availability(package.version, "canister", package_id) if package.version else None


def statuses(
    records: list[dict],
    catalog: dict[str, Availability],
    lookup: Callable[[str], Availability | None] | None = None,
) -> list[TweakStatus]:
    """Pair each library record with the newest version published for it.

    `records` are `--tweaks`' own dicts, so this stays in step with whatever
    that lists — including two files carrying the same `package_id`, which is a
    real state (`1.1.8` and `1.1.8-jailed` side by side). Each file is answered
    for separately; they are different files and either may be the one attached
    to an app.

    `lookup` is called at most once per distinct package id, and only for ids
    the cached repo indexes could not answer — the network cost is a function
    of what is *missing* locally, not of how big the library is. It defaults to
    `canister_latest`, resolved *here* rather than as a default argument value,
    which would bind the function object at import and quietly ignore anything
    that replaced it afterwards.
    """
    resolve = lookup if lookup is not None else canister_latest
    resolved: dict[str, Availability | None] = {}
    unreachable: set[str] = set()

    # Every id the caches could not answer, looked up concurrently.
    #
    # Serially this was one 30-second-timeout request per package, and the
    # module docstring says most of the library needs one — so a network that
    # hangs rather than refusing froze the pane for minutes behind a button
    # with no cancel. The requests are independent and IO-bound, so the whole
    # round is now bounded by the slowest single one.
    missing = [
        pid
        for pid in dict.fromkeys(
            record.get("package_id") or "" for record in records
        )
        if pid and pid not in catalog
    ]
    if missing:
        with ThreadPoolExecutor(max_workers=min(8, len(missing))) as pool:
            futures = {pid: pool.submit(resolve, pid) for pid in missing}
            for pid, future in futures.items():
                try:
                    resolved[pid] = future.result()
                except LookupUnavailable:
                    resolved[pid] = None
                    unreachable.add(pid)

    out: list[TweakStatus] = []
    for record in records:
        package_id = record.get("package_id") or None
        installed = record.get("version") or None
        available: Availability | None = None
        unchecked = False
        if package_id:
            if package_id not in resolved:
                resolved[package_id] = catalog.get(package_id)
            available = resolved[package_id]
            unchecked = package_id in unreachable
        out.append(
            TweakStatus(
                filename=record["filename"],
                package_id=package_id,
                installed=installed,
                available=available,
                unchecked=unchecked,
            )
        )
    return out


def as_event(status: TweakStatus) -> dict:
    """The `tweak-update` event's payload.

    `filename` is the key the GUI joins on, because that is what identifies a
    row in the library — `package_id` does not, since two files can share one.
    """
    return {
        "filename": status.filename,
        "package_id": status.package_id,
        "installed": status.installed,
        "latest": status.available.version if status.available else None,
        "source": status.available.source if status.available else None,
        "update_available": status.update_available,
        # "we could not ask" — never folded into `latest: null`, which means
        # "we asked and nothing is published".
        "unchecked": status.unchecked,
    }
