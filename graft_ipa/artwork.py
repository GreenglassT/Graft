"""Resolve an app's icon artwork, and cache it on disk.

Three sources, tried in order, first hit wins — see `resolve`: a cached IPA,
the App Store, a connected device. Missing all three is an ordinary outcome
rather than a fourth source: the GUI draws its letter tile and nothing is
written.

The second and third rungs exist because most apps have nothing cached yet,
not because the first one is unreliable — measured against the reference
cache, the IPA rung answers for all nine.

Matching goes by the `CFBundleIconFiles` prefixes an app declares, never by
the word "icon" appearing in a filename — that heuristic missed
`com.example.app`'s real icon during design and has been removed.

**What a rung is.** Every rung is a total-catch probe over its *acquisition*
step, with *validation* outside the catch. Both halves are load-bearing, and
this is the rule a fourth rung has to keep:

* The acquisition half — the part talking to something this project did not
  write, be it an arbitrary zip, a storefront or a phone — is wrapped in
  `except Exception`, because every way it can fail has the same remedy for
  the caller (try the next rung) and none of them is worth failing a whole
  `--artwork` run over. Narrow catches are not the alternative: they have
  already missed a real shape twice here, and each rung's own docstring names
  the specific ones it was caught out by.
* The validation half — `_looks_like_an_image` — sits *outside* that catch, so
  a bug in this project's own validator propagates instead of masquerading as
  "this app has no icon". The contract worth offering is "a broken source
  never fails a run"; "a bug in graft never fails a run" is not
  achievable, and promising it anyway is what turns bugs into mysteries.

`resolve` sniffs again after the winning rung, and `_cached_artwork` sniffs
what is already on disk. That is defence in depth rather than duplication: the
cache is a directory anything can write to, so the bytes served on a *hit* were
not necessarily produced by a rung at all.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import plistlib
import re
import tempfile
import time
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import data_dirs
from .device_session import closing_best_effort
from .itunes import lookup_by_bundle_id

#: Which rung answered. Recorded in the sidecar and reported to the GUI, so a
#: wrong-looking icon can be traced to where it came from without guessing.
SOURCE_IPA = "ipa"
SOURCE_APP_STORE = "app-store"
SOURCE_DEVICE = "device"

#: Not a rung — no code path produces it. It is what the cache-hit branch
#: reports when the sidecar on disk carries no readable `source`, which means a
#: hand-edit or a file some other tool wrote. Defaulting to one of the three
#: above instead would *fabricate* provenance, and `SOURCE_IPA` is the worst
#: possible guess: it is the highest-trust label in this vocabulary, so someone
#: reading `--artwork` output to work out why an icon looks wrong would take it
#: to mean the bytes came out of the app's own bundle. Saying "unknown" costs
#: nothing, since nothing branches on this field — it is reported to a human.
SOURCE_UNKNOWN = "unknown"

#: How long a cache entry that has no IPA behind it may be served before it is
#: re-resolved. Everything else in the sidecar is compared against something
#: that can change; an entry resolved with no IPA has nothing to compare —
#: `resolved_at` is the only thing about it that ever moves. Without this, the
#: ordinary state of a freshly `--add-app`'d app (no IPA downloaded yet, icon
#: from the App Store) is a cache entry with *no invalidation trigger at all*
#: short of `--refresh-artwork` or a deleted PNG. Deliberately generous: app
#: icons do change, but rarely, and a miss costs one iTunes lookup.
_TTL_WITHOUT_AN_IPA_SECONDS = 30 * 24 * 60 * 60

#: Anything that is not a bundle-id character. A bundle id reaches us from an
#: IPA's Info.plist, which is to say from a file we did not write, and it is
#: about to become a filename.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True)
class Artwork:
    """Where an app's artwork ended up, and which rung produced it."""

    bundle_id: str
    path: Path
    source: str
    #: True when the cached file was served without re-resolving. The GUI does
    #: not care; a person reading `--artwork` output to work out why an icon is
    #: stale very much does.
    cached: bool


def artwork_dir() -> Path:
    return data_dirs.cache_dir() / "artwork"


def _slug(bundle_id: str) -> str:
    """A bundle id as a filename, with no path left in it.

    `/` is what actually has to collapse to `_` — that is what stops a hostile
    id from naming a file outside the artwork directory, since nothing left in
    the result can be read as a path separator. `.` is not in `_UNSAFE`'s
    excluded set, so `..` survives the substitution untouched:
    `../../etc/passwd` becomes `_.._etc_passwd`, not `unnamed`. That is still
    safe — `_.._etc_passwd` is one filename, not a path — just not because of
    the dots.

    `strip(".")` is a separate, cosmetic safeguard, not a security one: an id
    that is only dots (`.`, `..`) or starts/ends with one (`.hidden`) would
    otherwise slug to a filename that starts with `.` — a dotfile, invisible
    to a plain `ls` in a cache directory whose whole point is being
    human-inspectable. It only trims the leading and trailing run, so it
    doesn't touch dots elsewhere in a normal bundle id like `com.bravo.app`.

    Deliberately not a hash, for the same human-inspectable reason.
    """
    cleaned = _UNSAFE.sub("_", bundle_id).strip(".")
    return cleaned or "unnamed"


def artwork_path(bundle_id: str) -> Path:
    # `.png` is fixed regardless of which rung answered. Rung two
    # (`icon_from_app_store`) routinely hands back a JPEG — mzstatic artwork
    # URLs commonly end `512x512bb.jpg` — and it lands here under a `.png`
    # name anyway. That's fine for the GUI: `NSImage` sniffs content, not the
    # extension, so it renders either way. It would NOT be fine for a future
    # "is this really a PNG?" check written against this path — the
    # extension is a filename convention here, not a content guarantee.
    return artwork_dir() / f"{_slug(bundle_id)}.png"


def sidecar_path(bundle_id: str) -> Path:
    return artwork_dir() / f"{_slug(bundle_id)}.json"


def _atomic_write(path: Path, data: bytes) -> None:
    """Write `data` to a sibling temp file, then `os.replace` it into place.

    The sidecar doesn't strictly need this — `read_sidecar` already treats a
    truncated or half-written file as absent, so a torn sidecar write costs
    one harmless re-resolve. What needs it is the artwork PNG that Task 5
    writes right before calling `write_sidecar`: `Path.write_bytes` truncates
    the file before writing its new contents, so a crash or a concurrent
    `--refresh-artwork` mid-write can leave a torn PNG on disk with a sidecar
    that still vouches for it. That file exists, fails to decode, and — because
    its sidecar says "already resolved" — is served from the cache forever,
    with no way back short of `--refresh-artwork`. This helper is reused for
    the sidecar because the write is free once it exists, not because the
    sidecar was ever at risk.

    No `fsync`, matching `IPAInfoMemo._replace`'s reasoning (ipa_memo.py): this
    cache is entirely regenerable, so a write lost to a crash costs a
    re-resolve, not data.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def write_sidecar(
    bundle_id: str,
    *,
    source: str,
    ipa_sha256: str | None,
    ipa_size: int | None = None,
    ipa_mtime_ns: int | None = None,
) -> None:
    """Record how `bundle_id`'s cached artwork was arrived at.

    `ipa_size`/`ipa_mtime_ns` default to None so a caller that has no IPA — or
    that predates them — writes a sidecar meaning "no IPA behind this entry",
    which is exactly what `resolve` then compares against. They are what
    `resolve` invalidates on; `ipa_sha256` is provenance. See `_ipa_key`.
    """
    payload = {
        # Recorded despite being derivable from the filename via `sidecar_path`,
        # because `_slug` is lossy: `com.foo/bar` and `com.foo_bar` both slug to
        # `com.foo_bar`, and on case-insensitive APFS `com.Bravo` and
        # `com.bravo` share one file. Storing the real id lets a reader (Task
        # 5) notice the mismatch and re-resolve instead of silently serving one
        # app's icon under another app's name.
        "bundle_id": bundle_id,
        "source": source,
        # Read, not decorative: `resolve` expires an entry that has no IPA
        # behind it after `_TTL_WITHOUT_AN_IPA_SECONDS`, because such an entry
        # has nothing else that can ever change.
        "resolved_at": int(time.time()),
        # Provenance, deliberately *not* the invalidation key — see `_ipa_key`
        # for why that is `(size, mtime_ns)` instead. This is the authoritative
        # identity of the IPA an icon was made from, recorded once on the write
        # path so a human debugging a wrong icon can check it against a file.
        # Do not "simplify" the cache-hit path to compare it: that is a
        # hundreds-of-megabytes read on the hot path, which is the cost this
        # design exists to avoid.
        "ipa_sha256": ipa_sha256,
        "ipa_size": ipa_size,
        "ipa_mtime_ns": ipa_mtime_ns,
    }
    _atomic_write(sidecar_path(bundle_id), json.dumps(payload).encode())


def read_sidecar(bundle_id: str) -> dict | None:
    """The sidecar for `bundle_id`, or None if it is missing, unreadable, or
    not a JSON object.

    Unreadable reads as absent rather than raising. The only consequence of a
    corrupt sidecar is that the artwork gets resolved again, which is cheap and
    correct; raising would take a whole `--artwork` run down over one bad file.

    `[]`, `"abc"`, `42`, and `null` are all well-formed JSON that `json.loads`
    happily returns, and none of them has a `.get()` that behaves like a
    sidecar's. Task 5 reads fields off the result with no further guard, so a
    sidecar that parses but isn't a `dict` has to read as absent for the same
    reason a sidecar that doesn't parse at all does.
    """
    path = sidecar_path(bundle_id)
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


#: `Payload/<Something>.app/Info.plist` — exactly one path segment between
#: `Payload/` and the trailing `.app/Info.plist`, so a nested bundle's own
#: plist (a `.appex` or `.framework` carries one too) doesn't match.
_INFO_PLIST = re.compile(r"^Payload/[^/]+\.app/Info\.plist$")

#: A cap on the icon `icon_from_ipa` will read, applied before `max()` is
#: allowed to prefer the biggest candidate. `ipa` is a file this project did
#: not write, and `max()` actively *selects* the largest match — so without a
#: cap, a hostile IPA that declares a deflate-bombed PNG as its icon gets it
#: read preferentially. 2 MiB clears both real-world marks in the reference
#: cache with room to spare: 18,485 bytes for the largest file any app there
#: actually gets *selected* as its icon (a carrier's), and 402,410 bytes for
#: the largest flat PNG anywhere in any `.app` root, matched or not (Bravo's
#: `france_ppo.png`, not an icon at all). A 1024x1024 PNG icon fits well
#: inside it too.
_MAX_ICON_BYTES = 2 * 1024 * 1024


def _as_str_list(value: object) -> list[str] | None:
    """`value` as a non-empty list of prefixes, or None if it isn't shaped like one.

    Guards a shape that is wrong without raising, which is the dangerous kind.
    A bare string is itself iterable — `any(n.startswith(p) for p in
    "AppIcon60x60")` without this check walks it letter by letter, turning one
    filename into the single-character prefix set `{"A", "p", "I", ...}`,
    which matches nearly every PNG in the `.app` root and hands `max()`
    whichever happens to be biggest. A dict fails differently (`TypeError` in
    `startswith`) but is just as wrong a shape for this key. Both need to read
    as "nothing usable declared here", not as a prefix list — hence returning
    None rather than `[]`, so the caller can tell "declared, but malformed"
    from "not declared" and keep checking older keys either way.
    """
    if isinstance(value, list) and value and all(isinstance(v, str) and v for v in value):
        return value
    return None


def _declared_icon_prefixes(info: dict) -> list[str]:
    """Where an Info.plist can name its primary icon, newest key first.

    Every IPA in the reference cache uses the nested `CFBundleIcons` ->
    `CFBundlePrimaryIcon` -> `CFBundleIconFiles` form, but two older, flatter
    keys are still legal Info.plist shapes: a top-level `CFBundleIconFiles`
    array, and — oldest — a single `CFBundleIconFile` string naming the icon
    directly. Reading them means an app that declares its icon under one of
    them still gets an ordinary hit, instead of being funneled into "nothing
    declared" for a key this rung simply never checked.

    `info.get(...)` below is allowed to raise `AttributeError` when `info`
    itself isn't a dict (an array-root plist, say) — that propagates out to
    `icon_from_ipa`'s catch-all, same as any other malformed input.
    """
    icons = info.get("CFBundleIcons")
    if isinstance(icons, dict):
        primary = icons.get("CFBundlePrimaryIcon")
        if isinstance(primary, dict):
            nested = _as_str_list(primary.get("CFBundleIconFiles"))
            if nested:
                return nested

    top_level = _as_str_list(info.get("CFBundleIconFiles"))
    if top_level:
        return top_level

    single = info.get("CFBundleIconFile")
    if isinstance(single, str) and single:
        return [single]

    return []


def _read_icon_from_ipa(ipa: Path) -> bytes | None:
    """Acquire the bytes of the declared icon inside `ipa`, or None.

    Acquisition only, per the rung contract in the module docstring — the
    `_looks_like_an_image` check belongs to `icon_from_ipa`, outside this
    catch.

    Candidates are the PNGs in the `.app` *root* whose names match a prefix
    from `_declared_icon_prefixes`. That declaration is what makes this
    precise: Bravo ships a 120px declared icon and a 152px iPad icon, and the
    larger of the two is not the app's icon. An app that declares nothing
    findable is a clean miss — there is no name-matching fallback. That
    heuristic used to stand in here, and on real apps in this cache it does
    not degrade gracefully: it returns LiveContainer's placeholder-for-guests
    icon and a carrier's carrier-branded icon as those apps' own artwork,
    silently, under a sidecar that then claims the resolution is done. A miss
    costs one App Store lookup; a wrong picture is sticky.

    Size, not pixel dimensions, breaks a tie among matches, capped at
    `_MAX_ICON_BYTES`. These PNGs are CgBI-encoded, which shifts `IHDR` past
    the `CgBI` chunk, so header parsing is fiddly for no gain — file size
    orders them correctly (Bravo: 8,241 bytes for 120px against 11,149 for
    152px).
    """
    try:
        with zipfile.ZipFile(ipa) as zf:
            names = zf.namelist()
            info_name = next((n for n in names if _INFO_PLIST.match(n)), None)
            if info_name is None:
                return None
            app_root = info_name.rsplit("/", 1)[0] + "/"
            info = plistlib.loads(zf.read(info_name))

            # Root-only: `"/" not in n[len(app_root):]` excludes anything under
            # a subdirectory of the .app, so a bundled framework's own icon
            # (Frameworks/Thing.framework/AppIcon.png) never competes with the
            # app's real one.
            flat = [
                n
                for n in names
                if n.startswith(app_root)
                and "/" not in n[len(app_root) :]
                and n.lower().endswith(".png")
            ]
            declared = _declared_icon_prefixes(info)
            matches = [
                n
                for n in flat
                if any(n[len(app_root) :].startswith(prefix) for prefix in declared)
            ]
            matches = [n for n in matches if zf.getinfo(n).file_size <= _MAX_ICON_BYTES]
            if not matches:
                return None
            best = max(matches, key=lambda n: zf.getinfo(n).file_size)
            return zf.read(best)
    except Exception:
        # As wide as the rung contract requires (module docstring). The shapes
        # that caught the earlier tuple out are worth naming, because they are
        # what "narrow catches have missed a real shape" means here: a
        # malformed XML Info.plist raises `xml.parsers.expat.ExpatError`, not
        # a `zipfile` or `plistlib` exception at all; a root-level array plist
        # raises `AttributeError` on `.get` (a `CFBundleIcons`/
        # `CFBundlePrimaryIcon` shaped as a string is guarded above and reads
        # as a clean miss instead, but nothing guards `info` itself — only
        # `plistlib` decides its shape); an unsupported zip compression method
        # raises `NotImplementedError`; an encrypted entry raises
        # `RuntimeError`. The tuple this replaced missed all four — and its
        # `plistlib.InvalidFileException` was redundant besides, being itself a
        # `ValueError` subclass that only ever covered the binary-plist half of
        # the format anyway.
        return None


def icon_from_ipa(ipa: Path) -> bytes | None:
    """The app icon PNG from a cached IPA, or None if it has no flat one.

    "Flat" is load-bearing. An app can compile its icons into `Assets.car` and
    ship no loose PNG at all — no IPA in the reference cache does, but the
    layout is legal and asset catalogs are the modern default — and reading a
    compiled asset catalog needs a CAR parser this project has no reason to
    grow. Missing here is ordinary, and the caller falls through to the App
    Store.

    Acquisition (`_read_icon_from_ipa`) and validation are split per the rung
    contract in the module docstring. This rung is the one reading the most
    hostile input of the three — an arbitrary zip, from which it deliberately
    selects the *largest* declared match — so the docstring's promise of a
    "PNG" has to be checked rather than assumed. `resolve` sniffs again after
    the winning rung and that is not made redundant by this: the sniff there
    is what stops a *fourth* rung from forgetting.
    """
    data = _read_icon_from_ipa(ipa)
    return data if data and _looks_like_an_image(data) else None


#: Applies to both the iTunes lookup and the artwork image fetch itself. Kept
#: separate from `itunes.HTTP_TIMEOUT` even though the value matches today —
#: that constant covers a small JSON response, this one an image, and nothing
#: says they have to move together.
_APP_STORE_ARTWORK_TIMEOUT = 15


def _looks_like_an_image(data: bytes) -> bool:
    """PNG or JPEG magic bytes. Nothing more is asserted — this is a sniff,
    not a decode.

    Real mzstatic artwork URLs commonly end `512x512bb.jpg`, not `.png`, so
    JPEG has to be a first-class accept here, not an afterthought: a
    PNG-only check would reject the majority of what this rung actually
    fetches from the real store, not just the pathological cases it exists
    to catch.
    """
    return data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")


def _download_app_store_icon(bundle_id: str) -> bytes | None:
    """Acquire the raw bytes of `bundle_id`'s App Store artwork, or None.

    Reuses `_MAX_ICON_BYTES` as the download cap rather than a second
    constant — both guard the same thing (an app icon image this project
    didn't produce), just from two different sources. See that constant's
    own comment, above `_declared_icon_prefixes`.

    Acquisition only, per the rung contract in the module docstring —
    validation belongs to `icon_from_app_store`, outside this catch. The
    shape that caught the narrower catch out here is worth naming:
    `itunes._query` calls `data.get("results")` on whatever `resp.json()`
    handed back, so a storefront answering with a top-level JSON array raises
    `AttributeError`, not `ITunesError`.

    Streamed with a cap at `_MAX_ICON_BYTES`, not read via `.content` in one
    shot: `.content` on an uncapped response tries to allocate the whole body
    before this function gets any say in the matter, and `MemoryError` is an
    `Exception` — so an oversized body would be caught by the very handler
    that's supposed to make network trouble harmless, but only *after*
    paying for the allocation it was meant to avoid. `iter_content` lets this
    function bail mid-download instead.
    """
    # Imported here, never at module scope — the same argument as
    # `_icon_from_device_async` below, and this is where its measurement
    # lives. `requests` costs 57ms to import on its own (`python -X importtime
    # -c "import requests"`, min of 11 runs) against a ~0.01s bare
    # interpreter. Measured end to end by toggling one module-scope `import
    # requests` back on and alternating A/B blocks so both states ran under
    # the same machine load (min of each set, which is the statistic a
    # neighbour's build disturbs least):
    #
    #   `-X importtime -c "import graft_ipa.cli"`   128ms -> 101ms  (n=18)
    #   `graft --version`, which does no work   152ms -> 124ms  (n=24)
    #   `graft --devices --brief --json`        166ms -> 137ms  (n=24)
    #
    # So ~28ms off every invocation, including the `--devices --brief --json`
    # poll the GUI runs every five seconds and which never makes an HTTP
    # request at all — about a sixth of what that poll used to cost.
    #
    # ~28ms and not the 47ms `-X importtime` attributes to `requests` inside
    # this graph, because that figure is cumulative over its whole subtree and
    # ~18ms of that subtree (`urllib.request` -> `http.client` -> `ssl`,
    # `email`) was still imported after this change alone — `jwt`, at
    # `apple_portal.py`'s module scope, reached the same tail through
    # `jwt.jwks_client`. `apple_portal.make_jwt` now defers `jwt` for exactly
    # that reason; the two together are worth ~50ms, and neither is worth its
    # full price without the other.
    #
    # None of this is worth anything unless it holds in all five modules that
    # speak HTTP — `artwork`, `apple_portal`, `canister`, `itunes`, `repo`.
    # `cli.py` imports every one of them at module scope, so a single
    # module-scope `import requests` anywhere in that set puts the full cost
    # back on the other four. `tests/test_lazy_imports.py` guards
    # the set as a set, by asserting `requests` is absent from `sys.modules`
    # after `import graft_ipa.cli`.
    #
    # Outside the `try`, deliberately: that catch is `except Exception`, so an
    # import placed inside it would turn a broken install into "this app has
    # no icon" rather than an error anyone could act on.
    import requests

    try:
        app = lookup_by_bundle_id(bundle_id)
        if not app.artwork_url:
            return None
        resp = requests.get(app.artwork_url, timeout=_APP_STORE_ARTWORK_TIMEOUT, stream=True)
        if resp.status_code != 200:
            # Closed explicitly, like the oversize path below. `stream=True`
            # means the body was never read, so without this the connection is
            # only returned to the pool when the `Response` is collected.
            resp.close()
            return None
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > _MAX_ICON_BYTES:
                resp.close()
                return None
            chunks.append(chunk)
        content = b"".join(chunks)
        if not content:
            return None
        return content
    except Exception:
        return None


def icon_from_app_store(bundle_id: str) -> bytes | None:
    """The app icon image for `bundle_id`, fetched from its App Store listing.

    Second rung of the cascade (see the module docstring): it exists because
    rung one (`icon_from_ipa`) needs a cached IPA to read, and the ordinary
    state of a freshly `--add-app`'d app is that nothing has been downloaded
    yet — not because IPA-embedded icons are somehow untrustworthy.

    Acquisition (`_download_app_store_icon`) and validation are split per the
    rung contract in the module docstring. What makes the validation half
    non-negotiable *here* is that a 200 response is not proof of an image: a
    CDN edge case, a WAF block page, or a redirect to an HTML "Access Denied"
    body all arrive as a 200 with a body, and `resolve` would cache it under a
    sidecar stamped `source: "app-store"` that then reads as resolved forever.
    """
    raw = _download_app_store_icon(bundle_id)
    return raw if raw and _looks_like_an_image(raw) else None


def icon_from_device(bundle_id: str, *, udid: str) -> bytes | None:
    """The home-screen icon for `bundle_id`, read off a connected phone over
    SpringBoard.

    Third and last rung before giving up (see the module docstring). It only
    answers for an app installed *top-level*: a LiveContainer-delivered app
    lives inside LiveContainer's own container, and the home screen has never
    had an icon for it.

    A non-None return here is proof only that SpringBoard handed back
    something PNG-shaped for this bundle id — not proof the app is
    installed. Measured on a real device (iOS 26.1): SpringBoard answers an
    id it doesn't recognize with a genuine, valid, non-empty PNG — a generic
    placeholder icon, not empty `pngData` — so a naive "got bytes back" read
    would treat every uninstalled app as a hit. `icons_over_service`
    filters this out by probing a bundle id that cannot be installed and
    discarding any answer identical to it (see `_ABSENT_BUNDLE_ID`), which
    covers the case actually observed. That still doesn't make a returned
    icon airtight proof of installation in general — a device that answers
    unknown ids with *varying*, non-placeholder bytes rather than one stable
    blob would defeat this comparison — so a later plan building an
    installed-apps list on top of this rung should not treat "non-None" as
    a synonym for "installed" without its own corroboration (SpringBoard's
    own listing service, for instance).

    Acquisition (`_icon_from_device_async`) and validation are split per the
    rung contract in the module docstring.
    """
    try:
        data = asyncio.run(_icon_from_device_async(bundle_id, udid))
    except Exception:
        # No exception shape worth naming, which is itself the local evidence:
        # this rung talks to SpringBoard over a USB link, and no such device, a
        # locked phone, a refused lockdown handshake, an expired pairing
        # record, USB yanked mid-transfer, a SpringBoard service that won't
        # start and a pymobiledevice3 version whose exception types this
        # project doesn't model all arrive here — from a library whose raised
        # types are not part of its documented interface.
        return None
    return data if data and _looks_like_an_image(data) else None


#: Fed to `get_icon_pngdata` to learn what "nothing installed under this id"
#: looks like on *this* device, in *this* session — not a hardcoded hash,
#: deliberately. On the phone this was measured against (iPhone 15 Pro, iOS
#: 26.1), SpringBoard does not answer an unrecognized bundle id with empty
#: `pngData`; it answers with a genuine, valid, non-empty PNG — a generic
#: placeholder icon. Three bundle ids probed there — one configured app (a real app, but
#: LiveContainer-delivered and so not top-level) and two invented ids — came
#: back byte-identical (same SHA-256, 11,598 bytes), while `com.apple.
#: Preferences` and the LiveContainer host app each returned distinct, larger,
#: genuinely different icons. `_looks_like_an_image` cannot catch this — the
#: placeholder is not malformed, it simply isn't this app's icon — so without
#: this comparison, `resolve()` (Task 5) would cache the placeholder, stamp a
#: sidecar `source: "device"`, and serve it forever: worse than the letter
#: tile it replaces, which at least names the app. A pinned constant would
#: silently stop matching the moment the placeholder's bytes change — a new
#: iOS version, a different device class, a different display scale — so this
#: probes the live device instead. The id is obviously synthetic so it can
#: never collide with a real bundle id.
_ABSENT_BUNDLE_ID = "com.graft.probe.absent-app-id"


async def icons_over_service(service: Any, bundle_ids: Iterable[str]) -> dict[str, bytes]:
    """Home-screen icons for `bundle_ids`, over one already-open SpringBoard
    service. Ids the device has no icon for are absent from the result.

    The caller owns the connection; this owns the *rules* for reading icons off
    it — the `_ABSENT_BUNDLE_ID` probe above all. That split is the whole reason
    this is a separate function: `_icon_from_device_async` (one icon, its own
    session) and `device_apps.icons_for` (a whole installed-apps listing, one
    shared session) need the same rules over two very different connection
    lifetimes, and the second one cannot be built by calling the first in a loop
    — that would open a lockdown session per app, 67 of them for the phone this
    was measured on.

    What must not be duplicated is the probe. A second copy of the comparison
    would drift from this one, and the failure mode of a drifted copy is
    *silently caching a grey placeholder as an app's icon*, which the artwork
    cache then serves forever — precisely the defect `_ABSENT_BUNDLE_ID` exists
    to prevent. So there is one copy, here, and both callers go through it.

    Probed once for the whole batch, before anything else: the answer describes
    the device, not the app, so it does not change between ids, and one extra
    `get_icon_pngdata` round trip — measured at ~59ms against a Wi-Fi-paired
    phone — amortised over a listing is nothing. Probing first also means the
    comparison is available for the very first id rather than from the second
    one onwards.

    Acquisition only, per the rung contract in the module docstring —
    `_looks_like_an_image` belongs to the callers, outside their catches.
    """
    placeholder = await service.get_icon_pngdata(_ABSENT_BUNDLE_ID)
    # Some iOS version could plausibly answer an unrecognized id with no
    # `pngData` at all rather than a placeholder — in which case there is
    # nothing to compare against. Trusting every answer beats rejecting every
    # real icon on such a device.
    absent = bytes(placeholder) if placeholder else None

    icons: dict[str, bytes] = {}
    for bundle_id in bundle_ids:
        try:
            data = await service.get_icon_pngdata(bundle_id)
        except Exception:
            # Per id, and `continue` rather than `break`: a phone that goes away
            # mid-listing must yield fewer icons, not a failed listing, and one
            # id that upsets SpringBoard must not decide the fate of the 66
            # after it. The remaining calls against a dead connection fail fast
            # and cost a batch that was already lost nothing that matters.
            continue
        if not data:
            # The ordinary LiveContainer case: the app is not installed
            # top-level, so the home screen has never had an icon for it.
            continue
        data = bytes(data)
        if absent is not None and data == absent:
            continue
        icons[bundle_id] = data
    return icons


async def _icon_from_device_async(bundle_id: str, udid: str) -> bytes | None:
    # Imported here, never at module scope: `pymobiledevice3.lockdown` plus
    # `services.springboard` measured at ~0.22s to import (mean of 5 runs),
    # against a ~0.01s bare interpreter and a ~0.14s floor for `graft
    # --describe` (mean of 5 runs) — i.e. importing these two modules alone
    # costs more than the entire CLI does today. Task 6 has `cli.py` import
    # `artwork` at module scope, so an eager import here would be paid by
    # every invocation — including `--describe`, which the GUI's contract
    # test spawns and `conftest.describe_proc` session-scopes precisely
    # because spawning it was already too slow — and by the devices poll the
    # GUI runs every five seconds. `devices.py` does the same function-local
    # import at lines 49, 87 and 130; don't "tidy" this one upward to match a
    # normal module.
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.springboard import SpringBoardServicesService

    lockdown = await create_using_usbmux(serial=udid)
    # This close used to be bare, and it was the fourth copy of the defect
    # `device_session` documents — the one that shipped and stayed. The icon is
    # already decoded when the teardown runs, and `icon_from_device` wraps this
    # whole call in `except Exception: return None`, so a phone that dropped off
    # the network on `wait_closed()` discarded an icon it had just successfully
    # fetched and reported the app as having none. `devices`, `device_info` and
    # `device_apps` were fixed for this; this one was missed because the loss is
    # a single icon rather than a whole listing, which is exactly why it was
    # never noticed in use.
    #
    # A failure from the body still propagates and still reaches
    # `icon_from_device`'s catch, which is the behaviour that rung's docstring
    # describes: a locked phone, a refused handshake, an expired pairing record.
    async with closing_best_effort(lockdown):
        service = SpringBoardServicesService(lockdown=lockdown)
        # One connection, two calls — not two lockdown sessions. Both calls, and
        # the placeholder comparison between them, live in `icons_over_service`:
        # this rung and `device_apps.icons_for` read icons by the same rules, and
        # the one thing that must never exist twice is the `_ABSENT_BUNDLE_ID`
        # probe. A batch of one, so the probe is paid on every call to this rung
        # exactly as it was before the extraction.
        icons = await icons_over_service(service, [bundle_id])
        return icons.get(bundle_id)


def _sha256(path: Path) -> str | None:
    """`path`'s SHA-256, or None if it cannot be read.

    Chunked rather than `read_bytes()`: the inputs are IPAs, and the ones in
    the reference cache run to hundreds of megabytes — there is no reason to
    hold one in memory to hash it.

    Called only on `resolve`'s *write* path, where an icon has already been
    fetched and is about to be cached. That is what makes returning None on
    failure the right shape rather than a quiet loss: the digest is provenance
    (see `write_sidecar`), so a file that cannot be hashed must cost the
    recorded digest and not the icon. It is emphatically not a reason to throw
    away work already done.

    `except Exception`, matching every other catch in this module: `Path.open`
    raises `ValueError` on a path with an embedded NUL, which is neither an
    `OSError` nor a shape worth enumerating, and narrow catches have already
    missed a real shape twice in this feature (`icon_from_ipa`'s tuple,
    `_download_app_store_icon`'s `ITunesError` alone).
    """
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except Exception:
        return None
    return digest.hexdigest()


def _ipa_key(ipa: Path) -> tuple[int, int] | None:
    """`(size, mtime_ns)` for `ipa`, or None if it cannot be stat'd.

    This, not the SHA-256, is what `resolve` compares on the cache-hit path.
    Hashing was measured at 86 ms for the 242 MB Example IPA — warm, best case
    — and the GUI runs `--artwork` across every configured app on launch, so
    the digest turned every cache *hit* into hundreds of megabytes of pure
    re-confirmation that nothing had changed. Two `stat()` calls say the same
    thing with no I/O.

    `ipa_memo.py` solves the identical problem for Info.plist reads and its
    module docstring carries the full argument; the short form is that any
    rewrite of a file changes at least one of size or nanosecond mtime, and a
    replacement matching both is not a case worth hashing hundreds of
    megabytes on every call to defend against.

    None on failure covers a missing, unreadable, or vanished-mid-run IPA, and
    reads the same as "no IPA was named" — correct in both directions here,
    since an IPA that cannot be stat'd is one `icon_from_ipa` cannot open
    either, and an entry with no IPA identity is exactly what
    `_TTL_WITHOUT_AN_IPA_SECONDS` then bounds.
    """
    try:
        stat = ipa.stat()
    except OSError:
        return None
    return (stat.st_size, stat.st_mtime_ns)


def _believable_sidecar(bundle_id: str, png: Path) -> dict | None:
    """The sidecar for `bundle_id`, if the entry it describes is still
    believable *on its own terms* — the file is image-shaped, the sidecar names
    this app, and an entry with no IPA behind it has not aged out.

    Everything `_cached_artwork` checks except the caller's IPA identity, which
    is an *input* rather than a property of what is on disk. Split out so a
    caller that has no IPA to compare against — `device_apps.icons_for`, which
    is deciding whether it would be clobbering a higher rung's work — asks the
    cache's own questions instead of reimplementing the freshness rules and
    drifting from them.

    The TTL branch keys off the *stored* identity rather than the caller's,
    which is the same condition by a different name: `_cached_artwork` only
    reaches its TTL check once the two keys have already been proved equal, so
    "the caller named no IPA" and "the sidecar records no IPA" are the same
    state there.
    """
    # Eight bytes, not the whole file — enough for `_looks_like_an_image` and
    # nothing more, because this runs for every configured app on every GUI
    # launch.
    #
    # Sniffed on the *hit* path and not only on the write path, which is where
    # the argument for sniffing at all actually lands: stickiness is a property
    # of the hit. `resolve` refuses to write non-image bytes, but the cache is
    # an ordinary directory — an entry left by this feature's own earlier code,
    # a hand-placed file, a partial restore — and whatever is in there is
    # served forever, which is the exact defect the write-path sniff exists to
    # prevent.
    #
    # Also how a zero-byte PNG is caught, which is a file that exists — so a
    # bare `exists()` serves it forever — and decodes to nothing: it reads back
    # as `b""` and matches no signature. `resolve` cannot create one
    # (`_atomic_write` renames a finished file into place, and an empty rung
    # result is refused), so that state is a truncating restore, a copy off a
    # full disk, or somebody else's ENOSPC. A missing or unreadable file lands
    # in the same place via `OSError`.
    try:
        with png.open("rb") as handle:
            header = handle.read(8)
    except OSError:
        return None
    if not _looks_like_an_image(header):
        return None

    sidecar = read_sidecar(bundle_id)
    if sidecar is None:
        return None

    # The id is checked as well as the IPA identity because `_slug` is lossy:
    # `com.foo/bar` and `com.foo_bar` produce the same filename, and on
    # case-insensitive APFS so do `com.Bravo` and `com.bravo`. Neither can
    # happen with an id Apple would accept, so this is not a live bug — it is
    # what makes a collision *self-healing* rather than a permanently wrong
    # icon, and the sidecar's copy of the id is the only thing on disk that
    # says which app a lossy filename belongs to.
    if sidecar.get("bundle_id") != bundle_id:
        return None

    if _stored_ipa_key(sidecar) == (None, None):
        # No IPA behind this entry, so there is nothing about it that can ever
        # change: it would be served until someone passed `--refresh-artwork` or
        # deleted the PNG. That is the ordinary state of a freshly `--add-app`'d
        # app, which is precisely the case the App Store rung exists for.
        # Entries that *do* have an IPA behind them are deliberately excluded —
        # they already have a better trigger, and a second one would only
        # re-download icons that a perfectly good comparison just said were
        # current.
        resolved_at = sidecar.get("resolved_at")
        if not isinstance(resolved_at, (int, float)):
            # Missing or hand-edited to a non-number. Unbounded age is the one
            # thing this branch exists to prevent, so an unreadable age is
            # treated as too old rather than as no limit.
            return None
        if time.time() - resolved_at > _TTL_WITHOUT_AN_IPA_SECONDS:
            return None

    return sidecar


def _stored_ipa_key(sidecar: dict) -> tuple[object, object]:
    """The IPA identity a sidecar records, in `_ipa_key`'s shape.

    A sidecar written before these two fields existed has neither, so it reads
    as `(None, None)` and any app that has an IPA misses exactly once and gets
    rewritten. That is the whole migration — the fields are additive, and an
    entry that legitimately has no IPA behind it was already `(None, None)` and
    keeps hitting.
    """
    return (sidecar.get("ipa_size"), sidecar.get("ipa_mtime_ns"))


#: The rungs above the device rung in `resolve`'s cascade. `device_apps.
#: icons_for` writes into the same cache without going through `resolve`, and
#: this is what it checks before overwriting somebody else's entry.
_ABOVE_THE_DEVICE_RUNG = frozenset({SOURCE_IPA, SOURCE_APP_STORE})


def outranks_device(bundle_id: str) -> bool:
    """Whether the cache already holds a believable entry that a rung *above*
    the device rung produced.

    The cascade in `resolve` is ordered — cached IPA, then App Store, then the
    phone — but `device_apps.icons_for` writes device icons into the same cache
    without going through `resolve` at all, so nothing but this stopped the
    bottom rung from overwriting the top one. Measured: one
    `--device-apps --with-icons` run replaced Bravo's 120px IPA-sourced icon
    with the phone's 192px home-screen one, meaning a configured app's icon
    changed depending on whether you had visited the device pane.

    The one rule `_believable_sidecar` cannot apply here is the IPA identity
    comparison, because this caller has no IPA path to compare against — it is
    listing what is installed on a phone, not what is configured locally. The
    consequence is bounded and points the safe way: an IPA-backed entry whose
    IPA has since changed is re-resolved by the next `--artwork` regardless, and
    until then keeping the older high-rung icon is exactly what would have
    happened if this function had never been visited.

    A `source: "device"` entry is deliberately *not* protected: refreshing the
    phone's own icon from the phone is this caller's job. Neither is
    `SOURCE_UNKNOWN`, which is not a rung at all — it is what a hand-edited or
    foreign sidecar reads as, and it has no claim on the ordering.
    """
    sidecar = _believable_sidecar(bundle_id, artwork_path(bundle_id))
    return sidecar is not None and sidecar.get("source") in _ABOVE_THE_DEVICE_RUNG


def _cached_artwork(
    bundle_id: str, png: Path, ipa_key: tuple[int, int] | None
) -> Artwork | None:
    """The already-resolved artwork for `bundle_id`, if what is on disk still
    describes this app, this IPA, and recently enough to be believed.

    Every "return None" here means "re-resolve", which is cheap and always
    correct. The asymmetry is the whole design: a wrong cache *hit* is sticky,
    recoverable only via `--refresh-artwork`, while a wrong cache *miss* costs
    one lookup.
    """
    sidecar = _believable_sidecar(bundle_id, png)
    if sidecar is None:
        return None

    if _stored_ipa_key(sidecar) != (ipa_key if ipa_key is not None else (None, None)):
        return None

    raw_source = sidecar.get("source")
    return Artwork(
        bundle_id=bundle_id,
        path=png,
        # `Artwork.source` has to be a string — Task 6 serialises it into the
        # GUI's contract — but this file is one anything could have touched,
        # and `str()` on a stray `42` would cheerfully produce `"42"`. See
        # `SOURCE_UNKNOWN` for why the fallback is not one of the real rungs.
        source=raw_source if isinstance(raw_source, str) and raw_source else SOURCE_UNKNOWN,
        cached=True,
    )


def attempted_sources(ipa: Path | None, udid: str | None) -> list[str]:
    """Which rungs `resolve` will try, in order, for these inputs.

    The one home for the cascade's skipping rules. `resolve` iterates this to
    pick its rungs, and `cli._do_artwork` names it when it reports a miss —
    the only thing that can still say what was tried, since every rung
    swallows its own failures. That caller used to re-derive the answer as
    `"cached IPA, App Store" if cached else "App Store"`, which was correct
    only because it hardcodes `udid=None`: it would have started lying the
    moment anything passed a udid, and no test could have caught it.

    A rung with no input is skipped rather than called and left to miss.
    `icon_from_ipa` takes a `Path` and cannot be handed None; `icon_from_device`
    with no udid would cost a USB round trip per app and answer for whichever
    phone happened to be attached rather than the one the caller meant.
    """
    sources: list[str] = []
    if ipa is not None:
        sources.append(SOURCE_IPA)
    sources.append(SOURCE_APP_STORE)
    if udid:
        sources.append(SOURCE_DEVICE)
    return sources


def resolve(
    bundle_id: str,
    *,
    ipa: Path | None,
    udid: str | None = None,
    refresh: bool = False,
) -> Artwork | None:
    """Artwork for `bundle_id`, from the cache or from the first rung that has it.

    Returns None when every rung missed, which is an ordinary outcome and not
    an error — the GUI draws its letter tile, exactly as it does today. A miss
    writes nothing at all: no PNG, and in particular no sidecar, because a
    sidecar recording a miss would have to be re-examined by every future
    caller and would earn nothing over just asking the rungs again. It also
    *deletes* nothing, so a `refresh` whose rungs all miss — an offline
    machine, say — leaves the previous answer cached and still servable rather
    than trading a stale icon for none at all.

    The cache is authoritative unless `refresh` is set, the IPA it was resolved
    against has changed, or — for an entry with no IPA behind it at all — it
    has aged past `_TTL_WITHOUT_AN_IPA_SECONDS`. The IPA condition is the one
    worth keeping: a version bump can add a flat icon an older IPA lacked, and
    a tweak can replace the icon outright, and neither shows up as a missing
    file.

    Cache hits cost one `stat()` on the IPA, an eight-byte read of the cached
    PNG's header and a small JSON read — no part of the IPA itself is read.
    See `_ipa_key`.
    """
    png = artwork_path(bundle_id)
    ipa_key = _ipa_key(ipa) if ipa is not None else None

    if not refresh:
        hit = _cached_artwork(bundle_id, png, ipa_key)
        if hit is not None:
            return hit

    # Keyed by `attempted_sources`, which is the only thing that decides which
    # rungs apply to which inputs — a rung it leaves out is never looked up
    # here, so these can take `ipa` and `udid` at face value rather than
    # re-checking them and leaving two copies of the rule to drift apart.
    rungs = {
        SOURCE_IPA: lambda: icon_from_ipa(ipa),
        SOURCE_APP_STORE: lambda: icon_from_app_store(bundle_id),
        SOURCE_DEVICE: lambda: icon_from_device(bundle_id, udid=udid),
    }
    for source in attempted_sources(ipa, udid):
        data = rungs[source]()
        # "The rung found nothing." `not data` rather than `data is None`
        # because `b""` is also nothing found — caching zero bytes would leave
        # a file that exists, satisfies the cache-hit branch above, and decodes
        # to nothing.
        #
        # The sniff below independently rejects `b""` (no signature to match),
        # so these two guards do overlap and a mutation swapping this one for
        # `data is None` changes no behaviour. Both are kept because they say
        # different things — "no answer" and "an answer that is not an image" —
        # and only this one can run first: `_looks_like_an_image(None)` raises.
        if not data:
            continue
        # Sniffed here for the same reason. Every rung now validates its own
        # bytes, so this is deliberately the second check on the same data: it
        # makes "an icon is image-shaped" a property of the *cascade* rather
        # than a habit each rung has to remember, which is what a fourth rung
        # would otherwise have to be told. What is at stake if one forgets is
        # concrete — an IPA declaring `CFBundleIconFiles: ["AppIcon60x60"]` and
        # shipping an `AppIcon60x60@2x.png` full of `<!DOCTYPE html>` resolves
        # clean, gets written to the cache under `source: "ipa"`, and is served
        # as an icon that renders as nothing until someone finds
        # `--refresh-artwork`.
        if not _looks_like_an_image(data):
            continue
        # Re-stat the IPA now that the rung has finished reading it, and record
        # an identity only if it still matches the one the cache check used.
        # `--provide-ipa` or a download landing mid-run can replace the file
        # between the two, and recording the pre-write identity against
        # post-write contents would pin a wrong icon until the *next* time the
        # IPA changed. Disagreement records no identity at all, so the entry
        # has no IPA behind it and the next call re-resolves. Same reasoning,
        # and the same two-stat shape, as `IPAInfoMemo.info_for`.
        after = _ipa_key(ipa) if ipa is not None else None
        recorded = after if after == ipa_key else None
        # Atomic, via the helper Task 1 added — and this PNG is the reason that
        # helper exists. A torn *sidecar* costs one re-resolve, because
        # `read_sidecar` already treats unreadable as absent. A torn *PNG* is
        # worse in kind: the file exists, so the cache-hit branch above serves
        # it forever, and `NSImage` gets bytes it cannot decode, so the GUI
        # shows nothing at all — with no way back short of `--refresh-artwork`.
        #
        # PNG first, then the sidecar. That order cannot produce a valid
        # sidecar vouching for a half-written image, which is the one
        # combination the cache-hit branch would trust. The reverse order can:
        # a crash between the two writes would leave a sidecar promising an
        # image that isn't there yet.
        #
        # `_atomic_write` does its own `mkdir(parents=True)`, so there is
        # deliberately no `artwork_dir().mkdir(...)` ahead of this.
        _atomic_write(png, data)
        write_sidecar(
            bundle_id,
            source=source,
            # The one place the IPA is hashed, and only once a fetch has
            # already happened. Skipped when there is no identity to attach it
            # to, so a file that changed mid-read is not hashed to describe a
            # state nothing will be compared against.
            ipa_sha256=_sha256(ipa) if ipa is not None and recorded is not None else None,
            ipa_size=recorded[0] if recorded is not None else None,
            ipa_mtime_ns=recorded[1] if recorded is not None else None,
        )
        return Artwork(bundle_id=bundle_id, path=png, source=source, cached=False)

    return None
