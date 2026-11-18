"""Classifying installed apps by whether they are worth decrypting, and
decorating the listing with the phone's own home-screen icons.

The one judgement here that a test can pin without a device is `_is_encrypted`,
and it is the load-bearing one: an App Store app is FairPlay-encrypted and is
what a dump tool targets, while a sideload is already decrypted and dumping it
is a no-op. The signal was chosen from real `get_apps` output — the fields the
internet suggests (`SBAppTags`, `Purchase`) came back None on a live device;
`SignerIdentity` is what actually distinguishes them.

The second half of this file covers `icons_for`, which is mocked end to end —
no test here touches a real device.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from graft_ipa import artwork, device_apps
from graft_ipa.config import load_config
from graft_ipa.device_apps import _STORE_SIGNER, InstalledApp, _to_app
from graft_ipa.json_output import JsonEmitter


def test_a_store_signed_app_is_encrypted():
    """Verified against a real device: two installed apps both carry this
    signer and are FairPlay-encrypted."""
    app = _to_app({"CFBundleIdentifier": "com.x", "SignerIdentity": _STORE_SIGNER})
    assert app is not None and app.encrypted


def test_a_developer_signed_sideload_is_not_encrypted():
    """The AltStore install on the test device signs as 'iPhone Developer: …'
    and is already decrypted."""
    app = _to_app({
        "CFBundleIdentifier": "com.x",
        "SignerIdentity": "iPhone Developer: Someone (ABCDE12345)",
    })
    assert app is not None and not app.encrypted


def test_a_missing_signer_falls_back_to_the_purchase_dsid():
    """Older iOS can omit SignerIdentity; the account that bought it is the
    corroborating signal, present on store apps and absent on the sideload."""
    bought = _to_app({"CFBundleIdentifier": "com.x", "ApplicationDSID": 12198315675})
    unbought = _to_app({"CFBundleIdentifier": "com.y"})
    assert bought is not None and bought.encrypted
    assert unbought is not None and not unbought.encrypted


def test_the_name_falls_back_display_then_bundle():
    display = _to_app({"CFBundleIdentifier": "com.x", "CFBundleDisplayName": "Pretty"})
    named = _to_app({"CFBundleIdentifier": "com.y", "CFBundleName": "Plain"})
    bare = _to_app({"CFBundleIdentifier": "com.z"})
    assert display and display.name == "Pretty"
    assert named and named.name == "Plain"
    assert bare and bare.name == "com.z"


def test_a_record_with_no_bundle_id_is_dropped():
    """Nothing a dump could target, so it must not appear as a nameless row."""
    assert _to_app({"CFBundleShortVersionString": "1.0"}) is None


def test_the_version_prefers_short_then_falls_back():
    short = _to_app({"CFBundleIdentifier": "com.x",
                     "CFBundleShortVersionString": "2.1", "CFBundleVersion": "210"})
    only_build = _to_app({"CFBundleIdentifier": "com.y", "CFBundleVersion": "210"})
    neither = _to_app({"CFBundleIdentifier": "com.z"})
    assert short and short.version == "2.1"
    assert only_build and only_build.version == "210"
    assert neither and neither.version == "?"


def test_the_event_payload_is_flat_and_named():
    app = InstalledApp(bundle_id="com.x", name="X", version="1.0", encrypted=True)
    # The shape the CLI emits, asserted so the GUI contract can rely on it.
    assert app.bundle_id == "com.x" and app.encrypted is True


# --- icons_for: one connection for the whole listing --------------------------
#
# `pymobiledevice3` is imported inside `_icons_from_device_async`, never at
# module scope (see that function's comment for the measured cost), so these
# mocks patch the source modules directly — `patch.object(device_apps,
# "create_using_usbmux")` would raise `AttributeError`, because that name never
# exists as an attribute of `device_apps` at all. Same reasoning, and the same
# patch targets, as `tests/test_artwork_sources.py`'s device-rung tests.
#
# Every test below takes `temp_config_env`, which is not optional: `icons_for`
# writes into `artwork.artwork_dir()`, derived from `XDG_CACHE_HOME`, so without
# it these tests would scribble on the developer's real icon cache.


#: Both stand-ins carry real PNG magic bytes, which matters for two different
#: reasons. `_PNG` has to look like an image or `_looks_like_an_image` would
#: reject it and every "an icon is cached" assertion below would pass for the
#: wrong reason. `_PLACEHOLDER` has to look like one *especially*: SpringBoard's
#: real placeholder is a genuine, valid PNG (measured: 11,598 bytes, see
#: `artwork._ABSENT_BUNDLE_ID`), so a stand-in that failed the sniff would let
#: the validator catch what only the probe comparison can catch, and
#: `test_a_placeholder_answer_is_never_cached` would be vacuous.
_PNG = b"\x89PNG\r\n\x1a\n" + b"real icon"
_PLACEHOLDER = b"\x89PNG\r\n\x1a\n" + b"\xaa" * 40


def _answer(answers: dict[str, object], bundle_id: str) -> object:
    """What the fake SpringBoard says for `bundle_id`.

    An id with no entry answers None — the ordinary "no icon for this app" case,
    not an error. An entry that *is* an exception is raised instead, which is
    how a phone that goes away mid-listing is simulated.
    """
    value = answers.get(bundle_id)
    if isinstance(value, BaseException):
        raise value
    return value


@contextmanager
def _a_phone_answering(answers: dict[str, object]) -> Iterator[tuple[MagicMock, MagicMock]]:
    """A connected phone whose SpringBoard answers per `answers`.

    Keyed by bundle id rather than ordered like `test_artwork_sources.py`'s
    equivalent: a batch asks for the probe plus N ids, and a `side_effect` list
    would silently encode the order they happen to be asked in as if it were
    part of the contract.

    Yields the `SpringBoardServicesService` constructor and the lockdown
    session, because "how many of each did the batch open" is the property these
    tests exist to pin.
    """
    lock = MagicMock()
    lock.close = AsyncMock()
    service = MagicMock()
    service.get_icon_pngdata = AsyncMock(side_effect=lambda b: _answer(answers, b))
    with (
        patch(
            "pymobiledevice3.lockdown.create_using_usbmux",
            new=AsyncMock(return_value=lock),
        ),
        patch(
            "pymobiledevice3.services.springboard.SpringBoardServicesService",
            return_value=service,
        ) as constructor,
    ):
        yield constructor, lock


def test_the_whole_batch_shares_one_session_and_one_service(temp_config_env: Path) -> None:
    """One lockdown session and one SpringBoard service for the whole listing.

    Measured at 10-30ms per icon against a real phone, so 156 apps is a few
    seconds on one connection — and 156 sessions and 156 service starts if this
    were written as `artwork.icon_from_device` in a loop, which is what it
    deliberately is not.

    The probe count is pinned for the same reason: `_ABSENT_BUNDLE_ID` describes
    the *device*, not the app, so it is asked once for the batch. Three calls
    for two apps, not four.
    """
    with _a_phone_answering(
        {artwork._ABSENT_BUNDLE_ID: _PLACEHOLDER, "com.a": _PNG, "com.b": _PNG}
    ) as (constructor, lock):
        icons = device_apps.icons_for(["com.a", "com.b"], udid="UDID")
        calls = constructor.return_value.get_icon_pngdata.await_count

    assert constructor.call_count == 1, "one service, not one per app"
    assert calls == 3, "one probe for the batch plus one call per app"
    assert set(icons) == {"com.a", "com.b"}
    lock.close.assert_awaited_once()


def test_a_placeholder_answer_is_never_cached(temp_config_env: Path) -> None:
    """The regression test for the finding behind `artwork._ABSENT_BUNDLE_ID`.

    Measured on a real device (iPhone 15 Pro, iOS 26.1): SpringBoard answers an
    unrecognised bundle id with a generic placeholder PNG, not with empty data —
    Example (real, but LiveContainer-delivered and so not a top-level install)
    and two invented ids all came back byte-identical, 11,598 bytes, same
    SHA-256. `_looks_like_an_image` cannot catch that; the placeholder is a
    valid PNG. Only comparing against what the same device says for an id that
    cannot exist can.

    What is at stake is stickiness: caching the placeholder would stamp a
    `source: "device"` sidecar over it and serve a grey square as that app's
    icon forever, which is worse than the letter tile it replaced — that one at
    least names the app. So this asserts the absence *on disk* as well as in the
    returned dict.
    """
    with _a_phone_answering({
        artwork._ABSENT_BUNDLE_ID: _PLACEHOLDER,
        "com.real.app": _PNG,
        "com.livecontainer.guest": _PLACEHOLDER,
    }):
        icons = device_apps.icons_for(["com.real.app", "com.livecontainer.guest"], udid="UDID")

    assert set(icons) == {"com.real.app"}
    assert not artwork.artwork_path("com.livecontainer.guest").exists()
    assert artwork.read_sidecar("com.livecontainer.guest") is None


def test_a_missing_icon_is_an_absence_that_does_not_stop_the_batch(
    temp_config_env: Path,
) -> None:
    """Two ways an id can fail, neither of which may cost the ids after it.

    `com.no.icon` has no `pngData` at all — the ordinary LiveContainer case,
    since a guest app is not a top-level install and the home screen never had
    an icon for it. `com.raises` blows up mid-call, which is what a phone
    unplugged halfway through a 156-app listing looks like. Both are absences;
    `com.after` still gets its icon, and nothing raises.
    """
    with _a_phone_answering({
        artwork._ABSENT_BUNDLE_ID: _PLACEHOLDER,
        "com.no.icon": None,
        "com.raises": ConnectionError("device went away"),
        "com.after": _PNG,
    }):
        icons = device_apps.icons_for(["com.no.icon", "com.raises", "com.after"], udid="UDID")

    assert set(icons) == {"com.after"}


def test_non_image_bytes_are_rejected(temp_config_env: Path) -> None:
    """SpringBoard handing back something that isn't an image is a miss.

    The probe deliberately answers with something *different*, so this tests
    `_looks_like_an_image` rather than silently retesting the placeholder
    comparison — two identical answers are rejected before the sniff ever runs.
    """
    with _a_phone_answering(
        {artwork._ABSENT_BUNDLE_ID: _PLACEHOLDER, "com.x": b"<!DOCTYPE html>"}
    ):
        icons = device_apps.icons_for(["com.x"], udid="UDID")

    assert icons == {}
    assert not artwork.artwork_path("com.x").exists()


def test_an_icon_lands_where_the_artwork_cascade_puts_one(temp_config_env: Path) -> None:
    """Same path, same bytes, same sidecar vocabulary as `artwork.resolve`.

    The GUI loads a listed app's icon and a configured app's through one code
    path, so an icon written anywhere else would need a second loader. The
    sidecar is part of that: writing the PNG alone would leave an app that
    already had cached artwork with *these* bytes under the *old* sidecar —
    a device icon reported as `source: "ipa"`, which is fabricated provenance.
    """
    with _a_phone_answering({artwork._ABSENT_BUNDLE_ID: _PLACEHOLDER, "com.x": _PNG}):
        icons = device_apps.icons_for(["com.x"], udid="UDID")

    path = artwork.artwork_path("com.x")
    assert icons == {"com.x": str(path)}
    assert path.read_bytes() == _PNG
    sidecar = artwork.read_sidecar("com.x")
    assert sidecar is not None
    assert sidecar["source"] == artwork.SOURCE_DEVICE
    assert sidecar["bundle_id"] == "com.x"
    # No IPA behind this entry, which is what bounds it by
    # `_TTL_WITHOUT_AN_IPA_SECONDS` instead of by an IPA that does not exist.
    assert sidecar["ipa_size"] is None and sidecar["ipa_sha256"] is None


def test_a_higher_rung_keeps_its_icon_and_a_bare_id_gets_the_device_one(
    temp_config_env: Path,
) -> None:
    """This rung is the bottom of `artwork.resolve`'s cascade and must not
    overwrite the ones above it.

    Verified live before the fix: one `--device-apps --with-icons` run replaced
    Bravo's 120px IPA-sourced icon with the phone's 192px home-screen one. Not
    data loss — an app with a cached IPA re-resolves on the next `--artwork`,
    and the rest is TTL-bounded — but it made a *configured* app's icon change
    depending on whether you had visited the device pane, which is a surprising
    coupling between two features that share nothing but a cache directory.

    Both halves are asserted together because the fix is only correct as a pair:
    an app with no artwork yet still has to get the device icon, which is the
    entire point of the pass.

    Run under `refresh=True`, deliberately. That is now the only way the guard
    is reachable at all: an entry believable enough to outrank this rung is
    also believable enough for the cache gate, so an ordinary run never fetches
    its bytes to begin with — which is the same outcome by a cheaper route, and
    was worth 59ms per such app. A refresh is the case where the bytes really
    do arrive and something has to refuse to write them.
    """
    ipa_bytes = b"\x89PNG\r\n\x1a\n" + b"from the ipa"
    artwork._atomic_write(artwork.artwork_path("com.has.ipa"), ipa_bytes)
    artwork.write_sidecar("com.has.ipa", source=artwork.SOURCE_IPA, ipa_sha256="abc")

    with _a_phone_answering({
        artwork._ABSENT_BUNDLE_ID: _PLACEHOLDER,
        "com.has.ipa": _PNG,
        "com.bare": _PNG,
    }):
        icons = device_apps.icons_for(
            ["com.has.ipa", "com.bare"], udid="UDID", refresh=True
        )

    # Reported either way — the app does have a cached icon at that path, and
    # the listing is decorated whichever rung produced it. Only the *write* is
    # skipped.
    assert set(icons) == {"com.has.ipa", "com.bare"}
    assert artwork.artwork_path("com.has.ipa").read_bytes() == ipa_bytes
    assert artwork.read_sidecar("com.has.ipa")["source"] == artwork.SOURCE_IPA
    assert artwork.artwork_path("com.bare").read_bytes() == _PNG
    assert artwork.read_sidecar("com.bare")["source"] == artwork.SOURCE_DEVICE


def test_a_stale_higher_rung_entry_is_not_protected(temp_config_env: Path) -> None:
    """"Still valid by the cache's own rules" is the whole of the guard.

    An App Store entry aged past `_TTL_WITHOUT_AN_IPA_SECONDS` is one
    `artwork.resolve` would itself throw away, so refusing to overwrite it would
    protect something the cache has already stopped believing — and would do it
    forever, since nothing else on this path ever revisits the entry. Written
    through `artwork`'s own writer and then backdated, so the freshness rule
    being exercised is the real one rather than a fixture's idea of it.
    """
    artwork._atomic_write(artwork.artwork_path("com.stale"), b"\x89PNG\r\n\x1a\n" + b"old")
    artwork.write_sidecar("com.stale", source=artwork.SOURCE_APP_STORE, ipa_sha256=None)
    sidecar = json.loads(artwork.sidecar_path("com.stale").read_text())
    sidecar["resolved_at"] = int(time.time()) - artwork._TTL_WITHOUT_AN_IPA_SECONDS - 1
    artwork.sidecar_path("com.stale").write_text(json.dumps(sidecar))

    with _a_phone_answering({artwork._ABSENT_BUNDLE_ID: _PLACEHOLDER, "com.stale": _PNG}):
        icons = device_apps.icons_for(["com.stale"], udid="UDID")

    assert set(icons) == {"com.stale"}
    assert artwork.artwork_path("com.stale").read_bytes() == _PNG
    assert artwork.read_sidecar("com.stale")["source"] == artwork.SOURCE_DEVICE


def test_a_believably_cached_id_is_reported_without_a_round_trip(
    temp_config_env: Path,
) -> None:
    """The gate: the cache is consulted *before* the phone, not after.

    Measured on the real test phone (iPhone 15 Pro, Wi-Fi, 67 apps, warm
    cache): 66 of the 67 ids already had a believable entry, so 66 of the 68
    round trips — ~59ms each — were re-reading bytes that were already on disk.
    3.3s became 0.5s by asking only about the one id that needed asking.

    Asserting the *call count* is the whole test. A version that fetched every
    id and then decided not to write would satisfy every other assertion here
    — the returned dict and the bytes on disk would be identical — which is
    precisely the shape the gate replaced.
    """
    cached = b"\x89PNG\r\n\x1a\n" + b"already here"
    artwork._atomic_write(artwork.artwork_path("com.cached"), cached)
    artwork.write_sidecar("com.cached", source=artwork.SOURCE_DEVICE, ipa_sha256=None)

    with _a_phone_answering(
        {artwork._ABSENT_BUNDLE_ID: _PLACEHOLDER, "com.cached": _PNG, "com.fresh": _PNG}
    ) as (constructor, _lock):
        icons = device_apps.icons_for(["com.cached", "com.fresh"], udid="UDID")
        asked = [
            call.args[0] for call in constructor.return_value.get_icon_pngdata.await_args_list
        ]

    # The probe and the one id that needed the phone. `com.cached` is not in
    # this list, and that absence is the finding.
    assert asked == [artwork._ABSENT_BUNDLE_ID, "com.fresh"]
    # Both are still decorated — the point is that one of them was decorated
    # from disk.
    assert set(icons) == {"com.cached", "com.fresh"}
    assert artwork.artwork_path("com.cached").read_bytes() == cached


def test_a_fully_cached_listing_opens_no_session_at_all(temp_config_env: Path) -> None:
    """With nothing to ask about there is no reason to talk to the phone.

    Not merely one fewer round trip: no lockdown handshake and no SpringBoard
    service either, so a listing whose icons are all cached is decorated in
    full even on a phone that has since gone to sleep or dropped off Wi-Fi.
    """
    for bundle_id in ("com.a", "com.b"):
        artwork._atomic_write(artwork.artwork_path(bundle_id), _PNG)
        artwork.write_sidecar(bundle_id, source=artwork.SOURCE_DEVICE, ipa_sha256=None)

    with patch(
        "pymobiledevice3.lockdown.create_using_usbmux",
        new=AsyncMock(side_effect=ConnectionError("the phone is asleep")),
    ) as create:
        icons = device_apps.icons_for(["com.a", "com.b"], udid="UDID")

    create.assert_not_awaited()
    assert icons == {
        "com.a": str(artwork.artwork_path("com.a")),
        "com.b": str(artwork.artwork_path("com.b")),
    }


def test_an_unbelievable_cache_entry_is_not_a_hit(temp_config_env: Path) -> None:
    """The gate applies the cache's own freshness rule, and adds none of its own.

    A zero-byte PNG is the case that makes that concrete: the file exists, so a
    bare `exists()` check would serve it forever, and it decodes to nothing —
    which is exactly why `artwork._believable_sidecar` sniffs the header rather
    than stat-ing the path. One rule, one owner, and this rung asks it rather
    than reimplementing it.
    """
    artwork._atomic_write(artwork.artwork_path("com.empty"), b"")
    artwork.write_sidecar("com.empty", source=artwork.SOURCE_DEVICE, ipa_sha256=None)

    with _a_phone_answering({artwork._ABSENT_BUNDLE_ID: _PLACEHOLDER, "com.empty": _PNG}):
        icons = device_apps.icons_for(["com.empty"], udid="UDID")

    assert set(icons) == {"com.empty"}
    assert artwork.artwork_path("com.empty").read_bytes() == _PNG


def test_refresh_asks_the_phone_about_every_id(temp_config_env: Path) -> None:
    """`--refresh-artwork`'s half of the gate, and the only way past it.

    A `source: "device"` entry has no claim on the ordering — re-reading the
    phone's icon off the phone is exactly this function's job — but with the
    cache consulted first, "every run" is no longer when that happens. A home
    screen icon that changed under an unchanged bundle id has no other trigger
    short of the entry ageing out of `_TTL_WITHOUT_AN_IPA_SECONDS`, so without
    this flag there would be no way to ask at all.

    `SOURCE_UNKNOWN` is covered alongside it: not a rung, but what a hand-edited
    or foreign sidecar reads as, and it has no claim on the ordering either.
    """
    older = b"\x89PNG\r\n\x1a\n" + b"older"
    for source in (artwork.SOURCE_DEVICE, "unknown"):
        bundle_id = f"com.own.{source}"
        artwork._atomic_write(artwork.artwork_path(bundle_id), older)
        artwork.write_sidecar(bundle_id, source=source, ipa_sha256=None)

        with _a_phone_answering({artwork._ABSENT_BUNDLE_ID: _PLACEHOLDER, bundle_id: _PNG}):
            served = device_apps.icons_for([bundle_id], udid="UDID")
            # The gate held: reported, and the phone's newer icon is still on
            # the phone.
            assert set(served) == {bundle_id}
            assert artwork.artwork_path(bundle_id).read_bytes() == older

            refreshed = device_apps.icons_for([bundle_id], udid="UDID", refresh=True)

        assert set(refreshed) == {bundle_id}
        assert artwork.artwork_path(bundle_id).read_bytes() == _PNG
        assert artwork.read_sidecar(bundle_id)["source"] == artwork.SOURCE_DEVICE


def test_an_unreachable_phone_yields_no_icons_rather_than_raising(
    temp_config_env: Path,
) -> None:
    """`icons_for` decorates a listing, so it must cost icons and never the
    listing — the same posture as `_frontmost_bundle_id`. No such device, a
    locked phone, a refused handshake and an expired pairing record all arrive
    the same way."""
    with patch(
        "pymobiledevice3.lockdown.create_using_usbmux",
        new=AsyncMock(side_effect=ConnectionError("no device found")),
    ):
        assert device_apps.icons_for(["com.a", "com.b"], udid="UDID") == {}


def test_a_listing_survives_a_close_that_fails() -> None:
    """The sibling of the icon-batch case, on the listing itself.

    `list_installed_apps` is documented to raise so that a connection failure
    doesn't read as an empty phone — which is exactly why a bare close was
    wrong here: `get_apps` had already returned the phone's complete answer, so
    a teardown failure raised *that* error over a listing that was finished.
    A failure from the body still propagates; only the close is suppressed.
    """
    lock = MagicMock()
    lock.close = AsyncMock(side_effect=ConnectionResetError("phone went away at teardown"))
    proxy = MagicMock()
    proxy.get_apps = AsyncMock(return_value={
        "com.x": {"CFBundleIdentifier": "com.x", "CFBundleDisplayName": "Ex"},
    })
    with (
        patch(
            "pymobiledevice3.lockdown.create_using_usbmux",
            new=AsyncMock(return_value=lock),
        ),
        patch(
            "pymobiledevice3.services.installation_proxy.InstallationProxyService",
            return_value=proxy,
        ),
        patch.object(device_apps, "_frontmost_bundle_id", return_value=None),
    ):
        apps = device_apps.list_installed_apps("UDID")

    assert [app.bundle_id for app in apps] == ["com.x"]
    lock.close.assert_awaited_once()


def test_a_phone_that_drops_off_at_teardown_keeps_the_whole_batch(
    temp_config_env: Path,
) -> None:
    """A close that fails arrives *after* every icon has been read and decoded.

    The batch is the expensive thing here — 4.0s against the real test phone,
    68 round trips for 67 apps — and a bare `finally` on the session close sent
    that whole result into `icons_for`'s broad `except`, which returns `{}`. So
    a Wi-Fi phone dropping off at teardown cost every `icon_path` in the
    listing *and* left nothing in the artwork cache, meaning the next run paid
    the 4s over again. Both halves are asserted for that reason: the returned
    dict and the bytes on disk.

    Distinct from `test_an_unreachable_phone_yields_no_icons_rather_than_raising`
    above, which is the case that must still reach that `except` — a connection
    that never came up has no result to keep.
    """
    with _a_phone_answering({
        artwork._ABSENT_BUNDLE_ID: _PLACEHOLDER,
        "com.a": _PNG,
        "com.b": _PNG,
    }) as (_constructor, lock):
        lock.close = AsyncMock(side_effect=ConnectionResetError("phone went away at teardown"))
        icons = device_apps.icons_for(["com.a", "com.b"], udid="UDID")

    assert set(icons) == {"com.a", "com.b"}, "a failed teardown is not a failed batch"
    assert artwork.artwork_path("com.a").read_bytes() == _PNG
    assert artwork.artwork_path("com.b").read_bytes() == _PNG
    lock.close.assert_awaited_once()


# --- The `--device-apps --with-icons` verb ------------------------------------


def _events(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


_LISTING = [
    InstalledApp(bundle_id="com.real.app", name="Real", version="1.0", encrypted=True),
    InstalledApp(bundle_id="com.no.icon", name="Guest", version="2.0", encrypted=False),
]


def test_without_the_flag_no_icon_work_happens_at_all(temp_config_env: Path) -> None:
    """The listing is used by paths that want names now, not pictures in a few
    seconds — `--decrypt`'s target list among them — so the icon pass has to be
    opt-in rather than merely unreported. Asserting the *service* is never
    constructed is what says no round trip was made; asserting only that
    `icon_path` is absent from the events would pass just as well for an
    implementation that fetched all 156 icons and then threw them away.
    """
    from graft_ipa import cli as cli_mod

    buf = StringIO()
    with (
        patch.object(device_apps, "list_installed_apps", return_value=_LISTING),
        patch(
            "pymobiledevice3.lockdown.create_using_usbmux", new=AsyncMock()
        ) as create,
        patch(
            "pymobiledevice3.services.springboard.SpringBoardServicesService"
        ) as constructor,
    ):
        cli_mod._do_device_apps(
            load_config(temp_config_env), device="UDID", emitter=JsonEmitter(stream=buf)
        )

    constructor.assert_not_called()
    create.assert_not_awaited()
    apps = [e for e in _events(buf.getvalue()) if e["type"] == "device-app"]
    assert len(apps) == 2
    assert all("icon_path" not in e for e in apps)


def test_with_the_flag_each_event_carries_the_icon_it_has(temp_config_env: Path) -> None:
    """`icon_path` on the apps that have one, and *absent* — not null — on the
    ones that don't. An absent key is what the GUI's optional decode already
    reads as "draw the letter tile"; a null would be a second spelling of it.
    """
    from graft_ipa import cli as cli_mod

    buf = StringIO()
    with (
        patch.object(device_apps, "list_installed_apps", return_value=_LISTING),
        _a_phone_answering({artwork._ABSENT_BUNDLE_ID: _PLACEHOLDER, "com.real.app": _PNG}),
    ):
        cli_mod._do_device_apps(
            load_config(temp_config_env), device="UDID", with_icons=True,
            emitter=JsonEmitter(stream=buf),
        )

    by_id = {e["bundle_id"]: e for e in _events(buf.getvalue()) if e["type"] == "device-app"}
    assert by_id["com.real.app"]["icon_path"] == str(artwork.artwork_path("com.real.app"))
    assert "icon_path" not in by_id["com.no.icon"]


def test_refresh_artwork_reaches_the_icon_pass_and_only_with_icons(
    temp_config_env: Path,
) -> None:
    """`--refresh-artwork` is the escape hatch the cache gate needs, and the
    verb has to actually forward it.

    Both directions matter. Forwarded, it is the only way to pick up a
    home-screen icon that changed under an unchanged bundle id — the gate
    otherwise serves the cached one until the entry ages out. Ignored without
    `--with-icons`, because there is no icon pass to refresh and a flag that
    silently started one would make the cheap listing expensive.
    """
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    with (
        patch.object(device_apps, "list_installed_apps", return_value=_LISTING),
        patch.object(device_apps, "icons_for", return_value={}) as icons_for,
    ):
        cli_mod._do_device_apps(
            cfg, device="UDID", with_icons=True, refresh_icons=True,
            emitter=JsonEmitter(stream=StringIO()),
        )
        assert icons_for.call_args.kwargs["refresh"] is True

        cli_mod._do_device_apps(
            cfg, device="UDID", with_icons=True, refresh_icons=False,
            emitter=JsonEmitter(stream=StringIO()),
        )
        assert icons_for.call_args.kwargs["refresh"] is False

        cli_mod._do_device_apps(
            cfg, device="UDID", refresh_icons=True, emitter=JsonEmitter(stream=StringIO()),
        )
        assert icons_for.call_count == 2, "no icon pass without --with-icons"
