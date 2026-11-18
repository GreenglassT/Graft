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
    if pinned == "latest":
        message = f"no IPA found for {app.name}"
        what = f"a decrypted IPA of {app.bundle_id}"
        store_caveat = ""
    else:
        # Other versions may well be cached; saying only "no IPA found" would
        # be false. And the App Store route cannot satisfy a pin, so say so.
        message = f"no IPA found for {app.name}: version {pinned} of {app.bundle_id}"
        what = f"a decrypted IPA of version {pinned} of {app.bundle_id}"
        store_caveat = f"; that fetches the current release, not {pinned}"
    raise IPANotFoundError(
        message,
        hint=(
            f"stage {what} in {cfg.cache_dir}: bring your own (--provide-ipa "
            f"<file.ipa> --app {app.name}), decrypt it on a jailbroken device "
            f"(--decrypt {app.bundle_id}), or get it from the App Store "
            f"(--appstore-get --app {app.name}{store_caveat})"
        ),
    )
