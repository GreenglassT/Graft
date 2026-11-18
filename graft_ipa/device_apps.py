"""What apps are installed on a connected device — the target list for dumping.

The first half of on-device decryption: before a tool like bagbak or
frida-ios-dump can pull a decrypted IPA off the phone, you have to name the app
it should attach to, by bundle id. This lists the user apps the phone actually
has, so that name comes from the device rather than from memory.

Read through the same `InstallationProxyService` `lc_runner` already uses to
find LiveContainer, and reachable over exactly the same transport as everything
else here — usbmux, which is a Wi-Fi-paired device as readily as a cabled one.
A jailbreak is not required to *list* apps; it is required to decrypt one.

`icons_for` decorates that listing with the phone's own home-screen icons, one
connection for the whole batch, cached through `artwork` so the GUI loads a
listed app's icon by path exactly as it loads a configured app's. Opt-in
(`--device-apps --with-icons`), because the first run costs seconds the listing
itself does not — a later one asks the phone only about the ids the cache has
no believable answer for, which on the test phone was 1 of 67.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from . import artwork
from .device_session import closing_best_effort


@dataclass(frozen=True)
class InstalledApp:
    bundle_id: str
    name: str
    version: str
    #: An App Store app is FairPlay-encrypted and is what there is any point
    #: dumping. One sideloaded or built here (LiveContainer, AltStore) is
    #: already decrypted, so decrypting it is a no-op worth greying out rather
    #: than offering. `SBAppTags` carries `platform-application` for the latter.
    encrypted: bool
    #: Whether this app is on screen *right now*.
    #:
    #: Carried on the listing rather than queried separately, because it is what
    #: decides which backend can dump the app and the GUI needs it per row. An
    #: app that is merely *running* is not enough: iOS freezes a backgrounded
    #: app without killing it, so it keeps its pid, passes a "is it running"
    #: check, and then fails the frida attach. Only the frontmost app is
    #: guaranteed foreground-active — see `decrypt_runner.frontmost_app`.
    frontmost: bool = False


def list_installed_apps(udid: str | None = None) -> list[InstalledApp]:
    """User-installed apps on the target device, sorted by display name.

    `udid` picks the device; None takes the first usbmux enumerates, matching
    how the rest of the CLI resolves a default target before a preference is
    set. Raises whatever pymobiledevice3 raises — a caller listing apps to dump
    from wants a connection failure to say so, not to look like an empty phone.
    """
    return asyncio.run(_list_installed_apps_async(udid))


async def _list_installed_apps_async(udid: str | None) -> list[InstalledApp]:
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.installation_proxy import InstallationProxyService
    from pymobiledevice3.usbmux import list_devices as mux_list

    if udid is None:
        devices = await mux_list()
        udid = next(
            (getattr(d, "serial", None) or getattr(d, "udid", None) for d in devices), None
        )
        if udid is None:
            return []

    lock = await create_using_usbmux(serial=udid)
    # What a bare close cost here specifically: `raw` is the phone's complete
    # answer by the time the teardown runs, and `list_installed_apps` is
    # documented to raise so that a connection failure doesn't read as an empty
    # phone — so a phone that went away during teardown turned a finished
    # listing of 67 apps into exactly the connection error that docstring is
    # about. A failure from the body still propagates, which is the half that
    # docstring actually promises. See `device_session`.
    async with closing_best_effort(lock):
        proxy = InstallationProxyService(lockdown=lock)
        raw = await proxy.get_apps(application_type="User")

    apps = [app for app in (_to_app(meta) for meta in raw.values()) if app is not None]
    front = _frontmost_bundle_id(udid)
    if front is not None:
        apps = [
            replace(app, frontmost=True) if app.bundle_id == front else app for app in apps
        ]
    return sorted(apps, key=lambda a: a.name.lower())


def icons_for(
    bundle_ids: Sequence[str], *, udid: str | None = None, refresh: bool = False
) -> dict[str, str]:
    """Cache a home-screen icon for each id, returning bundle id → path.

    **The cache is consulted before the phone is.** An id the artwork cache
    already holds a believable entry for reports that entry's path and costs no
    round trip at all; only the rest are fetched. That is not a new freshness
    policy — it is `artwork._believable_sidecar`, the same rule `resolve`
    applies on the single-icon path, applied one rung lower by the one caller
    that writes into this cache without going through `resolve`.

    Measured against the real test phone (iPhone 15 Pro, Wi-Fi-paired, 67
    top-level user apps), warm cache, medians of five: **3.18s before, 0.50s
    after**, with the same 66 paths reported either way. 66 of the 67 ids had a
    believable entry, so 66 of the 68 round trips — the probe included, at
    ~59ms each — were re-fetching bytes already on disk, and one of those (an
    `ipa`-sourced entry, see `outranks_device` below) was fetched only to be
    provably discarded. The one id with no entry is fetched, as it was.

    What is left after the gate is still one lockdown session and one
    SpringBoard service for the whole batch, and that is still why this exists
    rather than a loop over `artwork.icon_from_device`: that rung opens its own
    session per call, so a cold listing would pay 67 handshakes on top of the
    round trips. The *rules* for reading an icon are shared with it through
    `artwork.icons_over_service`.

    `refresh` skips the gate and asks the phone about every id — the escape
    hatch `--refresh-artwork` reaches, and the only way to pick up a
    home-screen icon that changed under an unchanged bundle id. It follows
    `resolve(refresh=True)`'s posture rather than inventing one: an id the
    phone declines on a refresh is absent from *this* result, and its cached
    PNG is left on disk rather than deleted, so nothing is traded away for an
    answer that did not arrive.

    An id absent from the result is one with no cached entry that the phone
    also had no icon for. That includes every LiveContainer-delivered app —
    those are not top-level installs, so the home screen has never had an icon
    for them — and ids that come back as SpringBoard's generic placeholder; see
    `artwork._ABSENT_BUNDLE_ID` for why that has to be detected rather than
    trusted, and for the measurement behind it.

    An id whose cached entry a *higher* rung of `artwork.resolve`'s cascade
    produced keeps that entry — this is the bottom rung and does not overwrite
    the ones above it (`artwork.outranks_device`). The existing path is still
    reported, so the listing is decorated either way. The gate above now
    usually spares such an id the fetch as well, but `outranks_device` stays
    where it is: it is what protects the *write*, and a `refresh` still reaches
    it with bytes in hand.

    Never raises. This is the same posture as `_frontmost_bundle_id` and for the
    same reason: it decorates a listing, and a phone that goes away mid-listing
    has to cost icons, not the listing. The caller gets fewer entries.

    `udid=None` takes the first device usbmux enumerates, matching
    `list_installed_apps`.
    """
    # Split before the batch, not after. `outranks_device` — the only thing
    # that used to consult the cache here — runs *after* the bytes are already
    # decoded, so it could decline a write but never decline the fetch that
    # made the write possible.
    cached: dict[str, str] = {}
    wanted: list[str] = []
    for bundle_id in bundle_ids:
        path = artwork.artwork_path(bundle_id)
        if not refresh and artwork._believable_sidecar(bundle_id, path) is not None:
            cached[bundle_id] = str(path)
        else:
            wanted.append(bundle_id)
    if not wanted:
        # Not merely an optimisation: with nothing to ask about there is no
        # reason to open a lockdown session, so a fully-cached listing costs
        # nothing on a phone that is asleep or off the network — and reports
        # every icon anyway.
        return cached

    try:
        icons = asyncio.run(_icons_from_device_async(wanted, udid))
    except Exception:
        # Nothing narrower is honest — see `artwork.icon_from_device`'s catch
        # for the list of shapes a lockdown handshake can fail with.
        #
        # What reaches here is the connection itself never coming up. Partial
        # results are not lost to it: everything that can fail *per id* is
        # already caught inside `icons_over_service`, and the session's close —
        # which used to land here too — is now suppressed at its own `finally`.
        # That last clause is not a technicality. This comment used to claim a
        # close failure arrived "harmlessly", and it was the opposite: the whole
        # batch was already decoded by then, and a phone dropping off the
        # network at teardown threw away all 67 icons and cached none of them.
        #
        # `cached`, not `{}`: an unreachable phone costs only the ids that
        # actually needed it. Everything the cache already answered for was
        # answered before this call was made.
        return cached

    paths: dict[str, str] = dict(cached)
    for bundle_id, data in icons.items():
        # Outside every catch above, per the rung contract in `artwork`'s module
        # docstring: SpringBoard handing back garbage is a miss, but a bug in
        # *our own* validator must propagate rather than read as "this app has
        # no icon". Sniffed here rather than trusted from the service because
        # these bytes are about to be written into the artwork cache, and what
        # gets into that cache is served forever.
        if not artwork._looks_like_an_image(data):
            continue
        path = artwork.artwork_path(bundle_id)
        if artwork.outranks_device(bundle_id):
            # Do not overwrite a higher rung's work. `artwork.resolve`'s whole
            # design is an ordered cascade — cached IPA, then App Store, then
            # the phone — and this function writes into the same cache without
            # going through it, so it used to write unconditionally and stamp
            # `source: "device"` over whatever was there. Verified live: one
            # `--device-apps --with-icons` run replaced Bravo's 120px
            # IPA-sourced icon with the phone's 192px home-screen one. Not data
            # loss (an app with a cached IPA re-resolves on the next
            # `--artwork`, and everything else is TTL-bounded), but it made a
            # configured app's icon change depending on whether you had opened
            # the device pane — a surprising coupling between two features.
            #
            # The path is still reported, because the app does have a cached
            # icon at it; only the write is skipped. An app with no artwork yet
            # still gets the device icon, which is the point of this pass.
            #
            # Checked after the sniff above, not before, so an id whose device
            # bytes are garbage keeps behaving exactly as it did — absent from
            # the result — and this fix changes only the write.
            paths[bundle_id] = str(path)
            continue
        try:
            # The same path, the same format, and the same atomic write the
            # artwork cascade uses — the GUI loads both through one code path,
            # so an icon that landed anywhere else would need a second loader.
            #
            # The sidecar is not optional here, and the order (PNG, then
            # sidecar) is `resolve`'s. Writing the PNG alone would leave any
            # app that already had cached artwork with *these* bytes under the
            # *old* sidecar — a device icon reported as `source: "ipa"`, which
            # is fabricated provenance of exactly the kind `SOURCE_UNKNOWN`
            # exists to refuse. Stamping `source: "device"` with no IPA identity
            # is both true and self-healing: `_cached_artwork` re-resolves any
            # app that has an IPA behind it (the stored key won't match) and
            # expires the rest after `_TTL_WITHOUT_AN_IPA_SECONDS`.
            #
            # Reached only for an app whose cache entry this rung is allowed to
            # own — see the `outranks_device` guard above. What lands here is a
            # first icon, or a refresh of one this same rung wrote.
            artwork._atomic_write(path, data)
            artwork.write_sidecar(bundle_id, source=artwork.SOURCE_DEVICE, ipa_sha256=None)
        except OSError:
            # A full disk or an unwritable cache directory. The one remaining
            # way this function could raise, and it must not: the listing this
            # decorates is still perfectly good without the icon.
            continue
        paths[bundle_id] = str(path)
    return paths


async def _icons_from_device_async(
    bundle_ids: Iterable[str], udid: str | None
) -> dict[str, bytes]:
    """Acquire icon bytes for `bundle_ids` over a single lockdown session."""
    # Function-local, exactly as in `_list_installed_apps_async` above and
    # `artwork._icon_from_device_async` — importing `pymobiledevice3.lockdown`
    # plus `services.springboard` costs ~0.22s, more than the whole CLI, and
    # `cli.py` imports this module at module scope on every invocation.
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.springboard import SpringBoardServicesService

    lock = await create_using_usbmux(serial=udid)
    # What a bare close cost here specifically: 67 icons read over ~4s, the
    # phone drops off the network on `wait_closed()`, and `icons_for`'s broad
    # `except` returned `{}` — no `icon_path` reported for any app, and nothing
    # written to the artwork cache either, so the next run paid the 4s again.
    # A failure from the body still propagates, so a phone that never answered
    # still reaches that catch and still costs the icons. See `device_session`.
    async with closing_best_effort(lock):
        # One service for the whole batch. `SpringBoardServicesService` starts a
        # service on the phone; constructing one per id would pay that setup 67
        # times over on the listing this was measured against, on top of a
        # lockdown session each.
        service = SpringBoardServicesService(lockdown=lock)
        return await artwork.icons_over_service(service, bundle_ids)


def _frontmost_bundle_id(udid: str | None) -> str | None:
    """The app on screen, or None if we couldn't tell.

    Best-effort, and swallowing every failure on purpose. This listing is built
    over usbmux/lockdown, which works on any paired phone; reading the
    foreground app needs frida-server, which only a jailbroken one has. A stock
    device must still list its apps — the honest answer there is "we don't know
    which is frontmost", not an error — so a missing frida, a version mismatch,
    or an unreachable server all degrade to None rather than taking the whole
    listing down with them.
    """
    try:
        from .decrypt_runner import frontmost_app

        return frontmost_app(udid)
    except Exception:
        return None


def _to_app(meta: dict) -> InstalledApp | None:
    """One `get_apps` entry as an `InstalledApp`, or None if it has no id.

    A record without `CFBundleIdentifier` is nothing a dump could target, so it
    is dropped rather than listed as a nameless row.
    """
    bundle_id = meta.get("CFBundleIdentifier")
    if not bundle_id:
        return None
    name = (
        meta.get("CFBundleDisplayName")
        or meta.get("CFBundleName")
        or bundle_id
    )
    version = meta.get("CFBundleShortVersionString") or meta.get("CFBundleVersion") or "?"
    return InstalledApp(
        bundle_id=bundle_id,
        name=str(name),
        version=str(version),
        encrypted=_is_encrypted(meta),
    )


#: What `SignerIdentity` reads for an app installed from the App Store.
#: Probed on a real device against a store app (this value) and an AltStore
#: sideload ("iPhone Developer: …"); the fields the internet suggests for this
#: — `SBAppTags`, `Purchase` — came back None on both and are useless here.
_STORE_SIGNER = "Apple iPhone OS Application Signing"


def _is_encrypted(meta: dict) -> bool:
    """Whether the app is FairPlay-encrypted — i.e. worth decrypting.

    Only an App Store install is, and only that is signed by Apple's store
    identity. A developer- or enterprise-signed build (a sideload, an AltStore
    app, anything re-signed) is already decrypted, so dumping it is a no-op the
    UI should grey out rather than offer.

    `ApplicationDSID` — the account that bought it, present on store apps and
    absent on the sideload — corroborates, and covers a record where
    `SignerIdentity` is missing. Best-effort: a wrong guess only mislabels a
    row the dump itself would still handle correctly.
    """
    signer = meta.get("SignerIdentity")
    if signer == _STORE_SIGNER:
        return True
    if signer:
        return False
    return meta.get("ApplicationDSID") is not None
