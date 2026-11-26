"""The IPA cache: finding the decrypted IPA a build starts from.

Every source puts its IPA in `cache_dir` — `--provide-ipa` (bring your own),
`--decrypt` (dumped on a jailbroken device) and `--appstore-get` (App Store +
UnfairPlay on this Mac) — and a build only ever reads it from there.

The lookup is intentionally flexible: hand-copied files are named however their
source named them, so we match on the canonical `{bundle_id}_{version}.ipa` OR
a name-like pattern, and let the embedded Info.plist decide.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig, Config
from .errors import IPANotFoundError
from .ipa_memo import IPAInfoMemo
from .repo import version_key


@dataclass
class CachedIPA:
    path: Path
    bundle_id: str
    version: str
    #: The App Store version id of the build in `path`, when it was staged by
    #: a get of a chosen version (`staged_name`) and found as that build. None
    #: for every other file.
    external_version_id: str | None = None


def staged_name(bundle_id: str, version: str, external_version_id: str | None = None) -> str:
    """The name a staged IPA is given: `<bundle id>_<version>.ipa`, or, for a
    get of a chosen version, `<bundle id>_<version>_<version id>.ipa`.

    The second keeps two builds of one display version apart (2026.42.0
    twice): each get by id stages a file of its own, and a hold by build
    names exactly one (`admits`). It starts `<bundle id>_`, so every rule that
    finds a staged file by name finds it (`_is_cache_candidate`), and it sorts
    after the plain name, which therefore wins a tie for `latest`
    (`_cache_candidates`: `.` is 0x2E, `_` is 0x5F).

    The id must be ASCII digits, as `--version-id` is checked to be: it lands
    in a file name.
    """
    if external_version_id is None:
        return f"{bundle_id}_{version}.ipa"
    if not (external_version_id.isascii() and external_version_id.isdigit()):
        raise ValueError(f"{external_version_id!r} isn't an App Store version id")
    return f"{bundle_id}_{version}_{external_version_id}.ipa"


def _bundle_short_name(bundle_id: str) -> str:
    """com.example.app -> app"""
    return bundle_id.split(".")[-1]


def _is_cache_candidate(path: Path, bundle_id: str) -> bool:
    """Whether `path`'s *name* could possibly be `bundle_id`'s cache file.

    The same two shapes `find_in_cache` ever opens: the canonical
    `{bundle_id}_*.ipa`, or a hand-copied file whose stem starts with the
    bundle's short name (`com.example.app` -> `app`, matching e.g.
    `App_21.16.2.ipa`). A file matching neither is invisible to a build no
    matter what its own Info.plist says — this is that test, extracted so
    `_do_staged_ipas` can apply it too instead of hand-rolling a second copy.
    A file like `com.example.app-21.16.2.ipa` — a hyphen where the
    canonical name has an underscore — fails both and is exactly the shape
    that once slipped through a second, drifted copy of this rule and got
    offered as `preferred: true` for a build that could never reach it.
    """
    return path.name.startswith(f"{bundle_id}_") or path.stem.lower().startswith(
        _bundle_short_name(bundle_id).lower()
    )


def _cache_candidates(cache_dir: Path, bundle_id: str) -> list[Path]:
    """Every `*.ipa` in `cache_dir` passing `_is_cache_candidate` for
    `bundle_id`, in the order `find_in_cache` scans them: canonically-named
    files (`{bundle_id}_*.ipa`) before hand-copied ones, each bucket sorted by
    filename.

    The order is as load-bearing as the membership. `find_in_cache` (via
    `max(candidates, key=version_key)`, which keeps whichever tied candidate
    it saw first) relies on a canonical file always outranking a hand-copied
    one on a version tie, *regardless of alphabetical order between the two
    filenames* — a flat filename sort gets that backwards exactly when the
    hand copy's name sorts first, e.g. `App_21.16.2.ipa` before
    `com.example.app_21.16.2.ipa` (`A` is 0x41, `c` is 0x63).
    """
    prefix = f"{bundle_id}_"
    all_ipas = sorted(cache_dir.glob("*.ipa"))
    canonical = [p for p in all_ipas if p.name.startswith(prefix)]
    short = _bundle_short_name(bundle_id).lower()
    rest = [
        p for p in all_ipas if not p.name.startswith(prefix) and p.stem.lower().startswith(short)
    ]
    return canonical + rest


def admits(app: AppConfig, path: Path, version: str) -> bool:
    """Whether a build of `app` may start from the staged IPA at `path`,
    whose own Info.plist says `version`.

    `latest`: any. A pinned version (set by hand, or a hold from before 2.3):
    that version. A hold by build (`preferred_version_id`): that version in
    that build's own file (`staged_name`) and no other, because a file of the
    same version under another name may be the other build.

    The one rule `find_in_cache` and `--delete-staged`'s guard share.
    """
    pinned = app.preferred_version
    if pinned == "latest":
        return True
    if version != pinned:
        return False
    if app.preferred_version_id is None:
        return True
    return path.name == staged_name(app.bundle_id, pinned, app.preferred_version_id)


def _held_build(cfg: Config, app: AppConfig) -> CachedIPA | None:
    """The one file a hold by build may use, when it is there and its
    Info.plist says this app and the held version."""
    held_id = app.preferred_version_id
    assert held_id is not None
    path = cfg.cache_dir / staged_name(app.bundle_id, app.preferred_version, held_id)
    if not path.is_file():
        return None
    memo = IPAInfoMemo.load(cfg.cache_dir)
    try:
        info = memo.info_for(path)
    finally:
        memo.save()
    if info is None or info.bundle_id != app.bundle_id or not admits(app, path, info.version):
        return None
    return CachedIPA(
        path=path, bundle_id=info.bundle_id, version=info.version, external_version_id=held_id,
    )


def find_in_cache(cfg: Config, app: AppConfig) -> CachedIPA | None:
    """Look in cache_dir for an IPA that matches the app.

    Match strategy (in order):
      1. {bundle_id}_{version}.ipa exact match
      2. Any {bundle_id}_*.ipa (pick highest version-looking suffix)
      3. Files whose lowercase stem starts with the short bundle name (e.g. App_21.16.2.ipa)
    For each candidate we open it and inspect the embedded Info.plist to confirm the bundle id,
    so a mis-named file is never silently used.

    Those reads go through :class:`IPAInfoMemo`, which remembers each answer
    against the file's ``(size, mtime_ns)`` — the plist is still what decides,
    but an unchanged file costs a ``stat()`` instead of a central-directory
    parse. This function is on the GUI's refresh path via ``--list``; see
    ``ipa_memo`` for the numbers.
    """
    if not cfg.cache_dir.exists():
        return None
    if app.preferred_version != "latest" and app.preferred_version_id is not None:
        # A hold by build: that build's own file, or nothing (`admits`).
        return _held_build(cfg, app)

    candidates: list[Path] = []
    pinned = app.preferred_version
    exact = cfg.cache_dir / f"{app.bundle_id}_{pinned}.ipa"
    if pinned != "latest" and exact.exists():
        candidates.append(exact)

    # Strategies 2 and 3 above, shared with `_do_staged_ipas` rather than
    # mirrored — see `_cache_candidates`'s own doc for why a mirrored copy of
    # this ordering once drifted from its membership test and offered a file
    # this function could never find.
    candidates.extend(_cache_candidates(cfg.cache_dir, app.bundle_id))

    memo = IPAInfoMemo.load(cfg.cache_dir)
    matches: list[CachedIPA] = []
    try:
        for p in candidates:
            # `None` is every reason read_info_plist would have raised — not a
            # zip, no Payload/<App>.app/Info.plist, vanished between the glob
            # and now — which is the same "skip this candidate" as before.
            info = memo.info_for(p)
            if info is None:
                continue
            if info.bundle_id != app.bundle_id:
                continue
            if pinned != "latest":
                if info.version != pinned:
                    continue
                # An explicit pin has exactly one right answer; the first match is
                # the exact-filename candidate when it exists.
                return CachedIPA(path=p, bundle_id=info.bundle_id, version=info.version)
            matches.append(CachedIPA(path=p, bundle_id=info.bundle_id, version=info.version))
    finally:
        # In a `finally` because the pinned branch returns from inside the
        # loop, and a pinned lookup that never writes its memo re-reads the
        # same plist on every refresh forever.
        memo.save()

    if not matches:
        return None

    # `latest` means newest, not first-in-glob-order. `candidates` is sorted by
    # FILENAME, so returning matches[0] handed back whichever version sorted
    # first lexicographically — with 21.16.2 and 22.1.0 both cached that is the
    # OLDER one, and 21.16.2 vs 21.9.1 only ordered correctly by accident.
    # Compare the version parsed out of each Info.plist instead, which is also
    # what this function's docstring has always claimed to do.
    return max(matches, key=lambda c: version_key(c.version))


def acquire_ipa(cfg: Config, app: AppConfig) -> CachedIPA:
    """The cached IPA a build of `app` should start from, or an error naming
    every way to put one there.

    Cache only. There used to be a network fallback here; there is no source
    left that downloads on a build's behalf, so a miss is the user's to fix
    with one of the three verbs the hint names.
    """
    cached = find_in_cache(cfg, app)
    if cached:
        return cached
    pinned = app.preferred_version
    if pinned != "latest" and app.preferred_version_id is not None:
        # A hold by build takes only that build's own file, which only a get
        # of it stages: a provided or dumped IPA of the same version may be
        # the other build, so neither is offered.
        held = app.preferred_version_id
        raise IPANotFoundError(
            f"no IPA found for {app.name}: the build of {pinned} it's held at "
            f"(App Store version id {held})",
            # Edit App lets go of the build only when its Preferred version
            # changes, and it already says `pinned`: the command that holds by
            # version is named outright, for an agent or Terminal.
            hint=(
                f"get that build again (--appstore-get --app {app.name} --version-id {held}), "
                f"or let go of it: hold {pinned} by version (--set-app-options --app {app.name} "
                f"--preferred-version {pinned}), set another preferred version in Edit App, "
                f"or get the latest (--appstore-get --app {app.name})"
            ),
        )
    if pinned == "latest":
        message = f"no IPA found for {app.name}"
        what = f"a decrypted IPA of {app.bundle_id}"
        from_store = f"(--appstore-get --app {app.name})"
    else:
        # Other versions may well be cached; saying only "no IPA found" would
        # be false. A plain get fetches the latest and lets go of the pin
        # (`appstore.release_hold`), so the App Store route for the pinned
        # version is a get of that version, by its id in the list.
        message = f"no IPA found for {app.name}: version {pinned} of {app.bundle_id}"
        what = f"a decrypted IPA of version {pinned} of {app.bundle_id}"
        from_store = (
            f"(find {pinned} in --appstore-versions --app {app.name}, then "
            f"--appstore-get --app {app.name} --version-id <its id>)"
        )
    raise IPANotFoundError(
        message,
        hint=(
            f"stage {what} in {cfg.cache_dir}: bring your own (--provide-ipa "
            f"<file.ipa> --app {app.name}), decrypt it on a jailbroken device "
            f"(--decrypt {app.bundle_id}), or get it from the App Store "
            f"{from_store}"
        ),
    )
