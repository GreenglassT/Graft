from __future__ import annotations

import plistlib
import subprocess
import sys
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

from graft_ipa import artwork
from graft_ipa.itunes import ITunesApp, ITunesError

# The patch target is `requests.get`, not `graft_ipa.artwork.requests.get`:
# `artwork._download_app_store_icon` and `itunes._query` both import `requests`
# inside the function now (measured startup cost — see those imports' comments),
# so neither module has a `requests` attribute to reach through. The object
# being replaced is unchanged either way — the old target resolved to this same
# module — but the name has to be spelled at the library, and this file has to
# import it eagerly to do so. Test processes have no startup budget to protect;
# `tests/test_lazy_imports.py` is what guards the CLI's.

#: A one-pixel PNG. Content does not matter to any of these tests — only which
#: entry gets chosen — so the fixtures pad it to control relative sizes.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


def _make_ipa(path: Path, *, icon_files: list[str], entries: dict[str, bytes]) -> Path:
    """An IPA with `entries` in the .app root and `icon_files` declared primary."""
    info = {
        "CFBundleIdentifier": "com.example.App",
        "CFBundleExecutable": "App",
    }
    if icon_files:
        info["CFBundleIcons"] = {"CFBundlePrimaryIcon": {"CFBundleIconFiles": icon_files}}
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Payload/App.app/Info.plist", plistlib.dumps(info))
        for name, data in entries.items():
            zf.writestr(f"Payload/App.app/{name}", data)
    return path


def test_prefers_a_file_matching_the_declared_primary_icon(tmp_path: Path) -> None:
    """`CFBundleIconFiles` names the real app icon; other PNGs are not it.

    Bravo's IPA carries both `AppIcon60x60@2x.png` (120px, declared) and
    `AppIcon76x76@2x~ipad.png` (152px, not declared). The larger file is the
    iPad icon, and picking by size alone would silently prefer it.
    """
    ipa = _make_ipa(
        tmp_path / "app.ipa",
        icon_files=["AppIcon60x60"],
        entries={
            "AppIcon60x60@2x.png": _PNG + b"\x00" * 100,
            "AppIcon76x76@2x~ipad.png": _PNG + b"\x00" * 900,
        },
    )
    data = artwork.icon_from_ipa(ipa)
    assert data == _PNG + b"\x00" * 100


def test_picks_the_largest_among_declared_matches(tmp_path: Path) -> None:
    ipa = _make_ipa(
        tmp_path / "app.ipa",
        icon_files=["AppIcon60x60"],
        entries={
            "AppIcon60x60.png": _PNG + b"\x00" * 50,
            "AppIcon60x60@2x.png": _PNG + b"\x00" * 500,
        },
    )
    assert artwork.icon_from_ipa(ipa) == _PNG + b"\x00" * 500


def test_declares_no_icon_anywhere_returns_none(tmp_path: Path) -> None:
    """A flat PNG being present is not enough; it must match a declared prefix.

    There is deliberately no "pick something plausible" fallback here. One
    used to exist, matching any flat PNG with "icon" in its filename, and on
    real apps in the reference cache it does not degrade gracefully: it
    returns LiveContainer's `DefaultIcon.png` (its placeholder for *guest*
    apps, not its own icon) and a carrier's `ee-app-icon-3@3x.png` (a
    carrier-branded icon for a different brand) as those apps' artwork. A miss
    here costs one App Store lookup. A wrong picture gets written to disk by
    the caller with a sidecar stamped `source: "ipa"`, reads as already
    resolved forever, and is only recoverable via `--refresh-artwork` — cheap
    and recoverable beats free and silently wrong.
    """
    ipa = _make_ipa(
        tmp_path / "app.ipa",
        icon_files=[],
        entries={"AppIcon60x60@2x.png": _PNG + b"\x00" * 200},
    )
    assert artwork.icon_from_ipa(ipa) is None


def test_an_assets_car_only_ipa_returns_none(tmp_path: Path) -> None:
    """An app that declares an icon and ships only a compiled asset catalog.

    Reading a `.car` needs a parser this project has no reason to grow, so this
    rung must miss cleanly rather than half-succeed, and the caller falls
    through to the App Store. Synthetic on purpose — no IPA in the reference
    cache has this layout.
    """
    ipa = _make_ipa(
        tmp_path / "app.ipa",
        icon_files=["SomeDeclaredIcon60x60"],
        entries={"Assets.car": b"CAR\x00binary"},
    )
    assert artwork.icon_from_ipa(ipa) is None


def test_the_declared_prefix_wins_over_larger_undeclared_pngs(tmp_path: Path) -> None:
    """The declared prefix is matched even when other, larger PNGs sit beside it.

    This is Example's real shape: its icon,
    `logo_example_2024_q4_color60x60@2x.png`, sits among roughly thirty other
    flat PNGs in the `.app` root, several of them bigger. Nothing about the
    filename marks it as an icon; only the declaration does.
    """
    ipa = _make_ipa(
        tmp_path / "app.ipa",
        icon_files=["logo_example_2024_q4_color60x60"],
        entries={
            "logo_example_2024_q4_color60x60@2x.png": _PNG + b"\x00" * 100,
            "RETRO.png": _PNG + b"\x00" * 9000,
            "LaunchImageStoryboard.png": _PNG + b"\x00" * 5000,
        },
    )
    assert artwork.icon_from_ipa(ipa) == _PNG + b"\x00" * 100


def test_a_bare_string_declared_icon_is_not_iterated_char_by_char(tmp_path: Path) -> None:
    """`CFBundleIconFiles` as a bare string must not be walked letter by letter.

    A string is itself iterable, so without a type guard,
    `any(n.startswith(p) for p in declared)` turns `"AppIcon60x60"` into the
    single-character prefix set `{"A", "p", "I", "c", "o", "n", "6", "0",
    "x"}`, which matches nearly every PNG in the root — including the
    9,000-byte decoy here — and hands `max()` whichever one is biggest. The
    correct behaviour is a clean miss.
    """
    info = {
        "CFBundleIdentifier": "com.example.App",
        "CFBundleIcons": {"CFBundlePrimaryIcon": {"CFBundleIconFiles": "AppIcon60x60"}},
    }
    with zipfile.ZipFile(tmp_path / "app.ipa", "w") as zf:
        zf.writestr("Payload/App.app/Info.plist", plistlib.dumps(info))
        zf.writestr("Payload/App.app/AppIcon60x60@2x.png", _PNG + b"\x00" * 100)
        zf.writestr("Payload/App.app/Decoy.png", _PNG + b"\x00" * 9000)
    assert artwork.icon_from_ipa(tmp_path / "app.ipa") is None


def test_icon_files_as_a_dict_returns_none(tmp_path: Path) -> None:
    """`CFBundleIconFiles` shaped as a dict, not a list, is not a prefix source.

    Iterating a dict yields its keys, which are strings — the same
    silently-wrong shape a bare string is — so this needs the same guard.

    The key deliberately *matches* a file in the root. An earlier version used
    `{"a": "b"}` against `AppIcon.png`, which returns None whether the guard is
    there or not — `"AppIcon.png".startswith("a")` is False on the capital A —
    so the test passed while proving nothing. Removing the guard must make this
    fail, and with these values it does: the dict's one key is a real prefix of
    the decoy.
    """
    info = {
        "CFBundleIdentifier": "com.example.App",
        "CFBundleIcons": {
            "CFBundlePrimaryIcon": {"CFBundleIconFiles": {"AppIcon60x60": "irrelevant"}}
        },
    }
    with zipfile.ZipFile(tmp_path / "app.ipa", "w") as zf:
        zf.writestr("Payload/App.app/Info.plist", plistlib.dumps(info))
        zf.writestr("Payload/App.app/AppIcon60x60@2x.png", _PNG + b"\x00" * 100)
    assert artwork.icon_from_ipa(tmp_path / "app.ipa") is None


def test_reads_the_legacy_top_level_icon_files_array(tmp_path: Path) -> None:
    """Pre-`CFBundleIcons` apps declare their icon with a flat top-level array.

    Every IPA in the reference cache uses the nested form, but the flatter key
    is still a legal Info.plist shape and worth reading — an app that declares
    under it should still be an ordinary hit, not "nothing declared".
    """
    info = {
        "CFBundleIdentifier": "com.example.App",
        "CFBundleIconFiles": ["AppIcon60x60"],
    }
    with zipfile.ZipFile(tmp_path / "app.ipa", "w") as zf:
        zf.writestr("Payload/App.app/Info.plist", plistlib.dumps(info))
        zf.writestr("Payload/App.app/AppIcon60x60@2x.png", _PNG + b"\x00" * 100)
        zf.writestr("Payload/App.app/AppIcon76x76@2x~ipad.png", _PNG + b"\x00" * 900)
    assert artwork.icon_from_ipa(tmp_path / "app.ipa") == _PNG + b"\x00" * 100


def test_reads_the_legacy_single_icon_file_string(tmp_path: Path) -> None:
    """The oldest form: one `CFBundleIconFile` string naming the icon directly."""
    info = {
        "CFBundleIdentifier": "com.example.App",
        "CFBundleIconFile": "AppIcon",
    }
    with zipfile.ZipFile(tmp_path / "app.ipa", "w") as zf:
        zf.writestr("Payload/App.app/Info.plist", plistlib.dumps(info))
        zf.writestr("Payload/App.app/AppIcon.png", _PNG + b"\x00" * 100)
        zf.writestr("Payload/App.app/Unrelated.png", _PNG + b"\x00" * 900)
    assert artwork.icon_from_ipa(tmp_path / "app.ipa") == _PNG + b"\x00" * 100


def test_an_icon_over_the_size_cap_is_not_selected(tmp_path: Path) -> None:
    """A declared match over `_MAX_ICON_BYTES` is excluded, not preferred.

    `max()` actively prefers the biggest candidate, so without a cap a hostile
    IPA could declare an oversized file as its icon and have it read
    preferentially. Capped out, this becomes a clean miss rather than a huge
    read — even though the file matches the declared prefix and nothing else
    does.
    """
    ipa = _make_ipa(
        tmp_path / "app.ipa",
        icon_files=["AppIcon"],
        entries={"AppIcon@2x.png": _PNG + b"\x00" * (artwork._MAX_ICON_BYTES + 1)},
    )
    assert artwork.icon_from_ipa(ipa) is None


def test_a_malformed_xml_plist_returns_none(tmp_path: Path) -> None:
    """A broken XML `Info.plist` raises `xml.parsers.expat.ExpatError`.

    That is neither a `zipfile` exception nor a `plistlib.InvalidFileException`
    — it comes from the XML parser plistlib delegates to once it has decided
    the file looks like the XML format, and it has to be caught too.
    """
    with zipfile.ZipFile(tmp_path / "app.ipa", "w") as zf:
        zf.writestr(
            "Payload/App.app/Info.plist",
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b"<plist version=\"1.0\"><dict><key>oops</dict></plist>",
        )
        zf.writestr("Payload/App.app/AppIcon.png", _PNG)
    assert artwork.icon_from_ipa(tmp_path / "app.ipa") is None


def test_an_array_root_plist_returns_none(tmp_path: Path) -> None:
    """A root-level array plist has no `.get` — legal plist, wrong shape for us."""
    with zipfile.ZipFile(tmp_path / "app.ipa", "w") as zf:
        zf.writestr("Payload/App.app/Info.plist", plistlib.dumps(["not", "a", "dict"]))
        zf.writestr("Payload/App.app/AppIcon.png", _PNG)
    assert artwork.icon_from_ipa(tmp_path / "app.ipa") is None


def test_a_string_where_a_dict_is_expected_returns_none(tmp_path: Path) -> None:
    """`CFBundleIcons` shaped as a string rather than a dict."""
    info = {"CFBundleIdentifier": "com.example.App", "CFBundleIcons": "not-a-dict"}
    with zipfile.ZipFile(tmp_path / "app.ipa", "w") as zf:
        zf.writestr("Payload/App.app/Info.plist", plistlib.dumps(info))
        zf.writestr("Payload/App.app/AppIcon.png", _PNG)
    assert artwork.icon_from_ipa(tmp_path / "app.ipa") is None


def test_an_unsupported_zip_compression_method_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`zipfile` raises `NotImplementedError` for a compression method it can't read.

    Not a `zipfile.BadZipFile` — a distinct exception the narrower catch this
    replaced never named.
    """
    ipa = _make_ipa(
        tmp_path / "app.ipa", icon_files=["AppIcon"], entries={"AppIcon@2x.png": _PNG}
    )

    def _boom(*args: object, **kwargs: object) -> bytes:
        raise NotImplementedError("compression type 99")

    monkeypatch.setattr(zipfile.ZipFile, "read", _boom)
    assert artwork.icon_from_ipa(ipa) is None


def test_an_encrypted_zip_entry_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`zipfile` raises `RuntimeError` for an encrypted entry with no password."""
    ipa = _make_ipa(
        tmp_path / "app.ipa", icon_files=["AppIcon"], entries={"AppIcon@2x.png": _PNG}
    )

    def _boom(*args: object, **kwargs: object) -> bytes:
        raise RuntimeError("File 'Payload/App.app/Info.plist' is encrypted, password required")

    monkeypatch.setattr(zipfile.ZipFile, "read", _boom)
    assert artwork.icon_from_ipa(ipa) is None


def test_a_nested_png_is_not_mistaken_for_an_app_icon(tmp_path: Path) -> None:
    """Only the .app root counts. A bundled framework's icon is not the app's."""
    with zipfile.ZipFile(tmp_path / "app.ipa", "w") as zf:
        zf.writestr(
            "Payload/App.app/Info.plist",
            plistlib.dumps({"CFBundleIdentifier": "com.example.App"}),
        )
        zf.writestr("Payload/App.app/Frameworks/Thing.framework/AppIcon.png", _PNG)
    assert artwork.icon_from_ipa(tmp_path / "app.ipa") is None


def test_a_corrupt_ipa_returns_none(tmp_path: Path) -> None:
    bad = tmp_path / "bad.ipa"
    bad.write_bytes(b"not a zip")
    assert artwork.icon_from_ipa(bad) is None


def test_a_declared_icon_that_is_not_an_image_returns_none(tmp_path: Path) -> None:
    """Rung one validates its own output, like the other two.

    Nothing in the acquisition step objects to this IPA — it is a legal zip
    declaring a legal prefix — so without the sniff the rung's docstring
    promise of a "PNG" is only ever true by the goodwill of an archive this
    project did not write, and the one it deliberately picks the *largest*
    match from. `resolve` sniffs again afterwards, which makes this defence in
    depth rather than the only guard; two of three rungs validating was the
    incoherent state.
    """
    ipa = _make_ipa(
        tmp_path / "app.ipa",
        icon_files=["AppIcon60x60"],
        entries={"AppIcon60x60@2x.png": b"<!DOCTYPE html>\n<html>Access Denied</html>"},
    )
    assert artwork.icon_from_ipa(ipa) is None
    # The acquisition half still found it — so this is the validator declining
    # the bytes, not the matcher failing to see them.
    assert artwork._read_icon_from_ipa(ipa) is not None


# --- icon_from_app_store: the second rung -----------------------------------
#
# Every failure mode here is a miss, not an error (see the docstring on
# `icon_from_app_store`): no listing, no artwork on the listing, no network,
# a bad status. `resolve()` (Task 5) falls through to the device rung and
# ultimately the GUI's letter tile, so this function's whole contract is that
# it never raises.

_FAKE_APP = ITunesApp(
    track_id=100000001,
    bundle_id="com.bravo.app",
    name="Bravo",
    version="2025.20.0",
    artwork_url="https://example.com/512.png",
)


def _mock_resp(status: int = 200, content: bytes = b"\x89PNG\r\n\x1a\n") -> MagicMock:
    """A streamed `requests.Response` stand-in: `_download_app_store_icon`
    reads via `iter_content`, not `.content`, so the mock has to hand back an
    iterable of chunks rather than a single attribute."""
    r = MagicMock()
    r.status_code = status
    r.iter_content.return_value = [content] if content else []
    return r


def test_icon_from_app_store_returns_bytes_on_success() -> None:
    with (
        patch("graft_ipa.artwork.lookup_by_bundle_id", return_value=_FAKE_APP),
        patch("requests.get", return_value=_mock_resp()) as mock_get,
    ):
        data = artwork.icon_from_app_store("com.bravo.app")
    assert data == b"\x89PNG\r\n\x1a\n"
    assert mock_get.call_args.args[0] == "https://example.com/512.png"
    assert mock_get.call_args.kwargs["timeout"] == artwork._APP_STORE_ARTWORK_TIMEOUT


def test_icon_from_app_store_returns_none_on_itunes_error() -> None:
    with patch(
        "graft_ipa.artwork.lookup_by_bundle_id",
        side_effect=ITunesError("no app found"),
    ):
        assert artwork.icon_from_app_store("com.nonexistent.app") is None


def test_icon_from_app_store_returns_none_when_no_artwork_url() -> None:
    no_artwork = ITunesApp(
        track_id=1, bundle_id="com.x.y", name="X", version="1.0", artwork_url=None
    )
    with (
        patch("graft_ipa.artwork.lookup_by_bundle_id", return_value=no_artwork),
        patch("requests.get") as mock_get,
    ):
        assert artwork.icon_from_app_store("com.x.y") is None
    # Never even worth the network round trip with nothing to fetch.
    mock_get.assert_not_called()


def test_icon_from_app_store_returns_none_on_request_exception() -> None:
    with (
        patch("graft_ipa.artwork.lookup_by_bundle_id", return_value=_FAKE_APP),
        patch(
            "requests.get",
            side_effect=requests.RequestException("timed out"),
        ),
    ):
        assert artwork.icon_from_app_store("com.bravo.app") is None


def test_icon_from_app_store_returns_none_on_non_200() -> None:
    with (
        patch("graft_ipa.artwork.lookup_by_bundle_id", return_value=_FAKE_APP),
        patch("requests.get", return_value=_mock_resp(status=404)),
    ):
        assert artwork.icon_from_app_store("com.bravo.app") is None


def test_icon_from_app_store_returns_none_on_empty_content() -> None:
    with (
        patch("graft_ipa.artwork.lookup_by_bundle_id", return_value=_FAKE_APP),
        patch("requests.get", return_value=_mock_resp(content=b"")),
    ):
        assert artwork.icon_from_app_store("com.bravo.app") is None


def test_icon_from_app_store_returns_none_when_download_exceeds_the_size_cap() -> None:
    """`MemoryError` is an `Exception`, so an uncapped `.content` read would
    let an oversized body get caught by the same broad handler that's
    supposed to make network trouble harmless — only after already paying
    for the allocation. Streaming with a cap avoids ever reaching that."""
    oversized = b"\xff\xd8\xff" + b"\x00" * artwork._MAX_ICON_BYTES
    with (
        patch("graft_ipa.artwork.lookup_by_bundle_id", return_value=_FAKE_APP),
        patch("requests.get", return_value=_mock_resp(content=oversized)),
    ):
        assert artwork.icon_from_app_store("com.bravo.app") is None


def test_icon_from_app_store_survives_a_shape_error_below_itunes_error() -> None:
    """`lookup_by_bundle_id` can raise more than `ITunesError` — deliberately
    left un-mocked here so the real `itunes._query` is exercised, not a
    stand-in for the claim.

    `_query` does `data.get("results")` on whatever `resp.json()` returned.
    A storefront response shaped as a top-level JSON array — not the
    documented `{"results": [...]}` object — raises `AttributeError` there,
    the same "narrow catch missed a real shape" trap `icon_from_ipa` hit
    first. Only `requests.get` is patched — the one call `itunes._query`
    makes out to the network; `lookup_by_bundle_id` and `_query` themselves
    run for real. A catch that named only `ITunesError` around the lookup
    call would let this `AttributeError` escape `icon_from_app_store` and
    break its no-raise contract; only a catch around the whole body closes
    it — which is exactly what `_download_app_store_icon` has.
    """
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = ["not", "the", "documented", "shape"]
    with patch("requests.get", return_value=resp):
        assert artwork.icon_from_app_store("com.bravo.app") is None


# --- _looks_like_an_image ----------------------------------------------------


def test_looks_like_an_image_accepts_png_magic_bytes() -> None:
    assert artwork._looks_like_an_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)


def test_looks_like_an_image_accepts_jpeg_magic_bytes() -> None:
    assert artwork._looks_like_an_image(b"\xff\xd8\xff\xe0" + b"\x00" * 20)


def test_looks_like_an_image_rejects_html() -> None:
    assert not artwork._looks_like_an_image(b"<!DOCTYPE html><html>Access Denied</html>")


def test_looks_like_an_image_rejects_empty_bytes() -> None:
    assert not artwork._looks_like_an_image(b"")


def test_icon_from_app_store_rejects_a_200_with_an_html_body() -> None:
    """A 200 status is not proof of an image — a CDN can serve an
    'Access Denied' page with a 200. Left uncaught, Task 5 would write this
    straight to `artwork_path(bundle_id)` and stamp a sidecar claiming the
    icon is resolved, recoverable only via `--refresh-artwork` — the same
    sticky-wrong-answer failure `icon_from_ipa`'s own docstring argues
    against."""
    html = b"<!DOCTYPE html><html><body>Access Denied</body></html>"
    with (
        patch("graft_ipa.artwork.lookup_by_bundle_id", return_value=_FAKE_APP),
        patch("requests.get", return_value=_mock_resp(content=html)),
    ):
        assert artwork.icon_from_app_store("com.bravo.app") is None


def test_icon_from_app_store_accepts_jpeg_bytes() -> None:
    """Real mzstatic artwork URLs commonly end `.jpg`, not `.png` — the
    fixture `_FAKE_APP.artwork_url` ending `.png` must not be read as a
    guarantee of the content type, and the validator must accept JPEG on its
    own terms rather than by accident."""
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 100
    with (
        patch("graft_ipa.artwork.lookup_by_bundle_id", return_value=_FAKE_APP),
        patch("requests.get", return_value=_mock_resp(content=jpeg)),
    ):
        assert artwork.icon_from_app_store("com.bravo.app") == jpeg


# --- icon_from_device: the third rung ----------------------------------------
#
# `pymobiledevice3` is imported inside `_icon_from_device_async`, never at
# module scope (see that function's own comment for the measured cost), so
# these mocks patch the source modules directly — `patch.object(artwork,
# "create_using_usbmux")` would raise `AttributeError`, because that name
# never exists as an attribute of `artwork` at all.


#: A stand-in for SpringBoard's real placeholder icon — distinct from `_PNG`
#: only so tests can tell "the probe answer" and "the requested app's icon"
#: apart. Its exact bytes don't matter (it is never run through
#: `_looks_like_an_image`); only whether it matches `data` does.
_PLACEHOLDER = b"\x89PNG\r\n\x1a\n" + b"\xaa" * 40


def _mock_lockdown() -> MagicMock:
    """A lockdown stand-in whose `close()` is awaitable.

    `object()` was enough until `_icon_from_device_async` grew a teardown —
    the session it opens on the phone has to be closed, and it now does that
    through `device_session.closing_best_effort`, the same helper the other
    three modules that open one go through. These tests failing on that change
    is the mock doing its job: the real object has a `close`, and a stand-in
    that doesn't is no longer standing in for it.
    """
    lockdown = MagicMock()
    lockdown.close = AsyncMock()
    return lockdown


def _mock_springboard_service(*, probe: object, data: object) -> MagicMock:
    """A `SpringBoardServicesService` stand-in whose `get_icon_pngdata` is
    called twice by `_icon_from_device_async` — once for the
    `_ABSENT_BUNDLE_ID` probe, once for the real request, in that order — and
    can be told to answer each differently. `side_effect` as a list, not
    `return_value`, is what makes the two calls distinguishable; that
    distinction is the entire point of the placeholder-detection fix these
    tests guard.
    """
    service = MagicMock()
    service.get_icon_pngdata = AsyncMock(side_effect=[probe, data])
    return service


@contextmanager
def _a_phone_answering(*, probe: object, data: object) -> Iterator[None]:
    """A connected phone whose SpringBoard answers the `_ABSENT_BUNDLE_ID`
    probe with `probe` and the real request with `data`.

    Both patches always travel together — `_icon_from_device_async` opens a
    lockdown session and then a SpringBoard service on it, so mocking either
    alone reaches real USB — which is why they are one helper rather than two.
    """
    with (
        patch(
            "pymobiledevice3.lockdown.create_using_usbmux",
            new=AsyncMock(return_value=_mock_lockdown()),
        ),
        patch(
            "pymobiledevice3.services.springboard.SpringBoardServicesService",
            return_value=_mock_springboard_service(probe=probe, data=data),
        ),
    ):
        yield


def test_icon_from_device_returns_bytes_on_success() -> None:
    """The probe and the real request answer differently, so the real icon
    passes the placeholder comparison and comes back untouched."""
    with _a_phone_answering(probe=_PLACEHOLDER, data=_PNG):
        data = artwork.icon_from_device("com.example.App", udid="fake-udid")
    assert data == _PNG


def test_icon_from_device_treats_an_answer_identical_to_the_probe_as_a_miss() -> None:
    """The regression test for the finding: SpringBoard answering an unknown
    bundle id with a valid, non-empty, but generic placeholder PNG.

    Measured on a real device (iPhone 15 Pro, iOS 26.1): Example
    (LiveContainer-delivered, not top-level) and two invented bundle ids all
    returned the exact same 11,598-byte blob, same SHA-256. `_looks_like_an_
    image` cannot catch this — the placeholder is a genuine PNG — so this has
    to be caught by comparing against what the same device says for a bundle
    id that provably cannot be installed. Without this check, `resolve()`
    (Task 5) would cache the placeholder under a `source: "device"` sidecar
    and serve it forever, which is worse than the letter tile it replaces.
    """
    with _a_phone_answering(probe=_PLACEHOLDER, data=_PLACEHOLDER):
        assert artwork.icon_from_device("com.nonexistent.app", udid="fake-udid") is None


def test_icon_from_device_trusts_the_icon_when_the_probe_itself_answers_nothing() -> None:
    """Not every iOS version necessarily returns a placeholder for an unknown
    id — `pymobiledevice3`'s own docstring implies some may answer with no
    `pngData` at all. When the probe comes back falsy there is nothing to
    compare against, so the real answer must be trusted rather than rejected
    — otherwise a device that behaves this way would never return an icon
    from this rung at all.
    """
    with _a_phone_answering(probe=None, data=_PNG):
        data = artwork.icon_from_device("com.example.App", udid="fake-udid")
    assert data == _PNG


def test_icon_from_device_returns_none_when_springboard_has_no_icon() -> None:
    """No `pngData` for `bundle_id` at all (not even a placeholder) is the
    ordinary LiveContainer case: the app is not installed top-level, so the
    home screen has never had an icon for it. That is a clean miss, not a
    failure — `resolve()` falls through to giving up, same as any other rung
    finding nothing.
    """
    with _a_phone_answering(probe=None, data=None):
        assert artwork.icon_from_device("com.kdt.livecontainer.guest", udid="fake-udid") is None


def test_icon_from_device_returns_none_when_the_phone_is_unreachable() -> None:
    """No such device, a locked phone, a refused handshake, an expired
    pairing record, USB yanked mid-transfer — `create_using_usbmux` can raise
    for any of them, and every one has the same remedy for the caller: try
    the next rung. The catch has to be broad because narrowing it has already
    failed twice in this feature (`icon_from_ipa`, `icon_from_app_store`).
    """
    with patch(
        "pymobiledevice3.lockdown.create_using_usbmux",
        new=AsyncMock(side_effect=ConnectionError("no device found")),
    ):
        assert artwork.icon_from_device("com.example.App", udid="fake-udid") is None


def test_icon_from_device_keeps_an_icon_a_failed_close_arrives_after() -> None:
    """A teardown failure must not discard an icon that was already fetched.

    The fourth copy of the defect `device_session` documents, and the one that
    shipped: `devices`, `device_info` and `device_apps` were all fixed for it
    while this rung kept a bare `finally: await lockdown.close()`. The close
    runs *after* `icons_over_service` has returned the decoded bytes, and
    `icon_from_device` wraps the whole call in `except Exception: return None`
    — so a Wi-Fi phone dropping off the network on `wait_closed()` reported an
    app as having no icon when the phone had just handed one over. Cheap to
    miss in use, because the loss is one icon rather than a whole listing:
    `resolve` writes nothing, the letter tile is drawn, and the next run pays
    the round trip again.

    A failure from the *body* is deliberately not covered by this — that one
    still propagates to `icon_from_device`'s catch, which is what the
    unreachable-phone test above pins.
    """
    lockdown = _mock_lockdown()
    lockdown.close = AsyncMock(side_effect=ConnectionResetError("phone went away"))
    with (
        patch(
            "pymobiledevice3.lockdown.create_using_usbmux",
            new=AsyncMock(return_value=lockdown),
        ),
        patch(
            "pymobiledevice3.services.springboard.SpringBoardServicesService",
            return_value=_mock_springboard_service(probe=_PLACEHOLDER, data=_PNG),
        ),
    ):
        data = artwork.icon_from_device("com.example.App", udid="fake-udid")

    assert data == _PNG
    lockdown.close.assert_awaited_once()


def test_icon_from_device_rejects_non_image_bytes() -> None:
    """Validation (`_looks_like_an_image`) sits outside the acquisition catch,
    same split as `icon_from_app_store`: SpringBoard handing back garbage is
    a miss, but a bug in *our own* validator must propagate rather than read
    as a silent "no icon" — see `icon_from_app_store`'s docstring.

    The probe deliberately answers with something *different* from `data`
    here (`_PLACEHOLDER`, not `b"not an image"` again) — otherwise this would
    silently retest the placeholder-comparison path instead of the validator,
    since two identical answers are rejected by that check before
    `_looks_like_an_image` ever runs.
    """
    with _a_phone_answering(probe=_PLACEHOLDER, data=b"not an image"):
        assert artwork.icon_from_device("com.example.App", udid="fake-udid") is None


def test_importing_artwork_does_not_import_pymobiledevice3() -> None:
    """`pymobiledevice3.lockdown` + `services.springboard` cost ~0.22s to
    import (measured, mean of 5 runs), against a ~0.01s bare interpreter —
    see `_icon_from_device_async`'s own comment for the measured numbers.
    `cli.py` imports `artwork` at module scope, so an eager import here is
    paid by every CLI invocation, `--describe` included. This has to run as a
    subprocess: the import is a module-load side effect no in-process test
    (this file's own process already has `graft_ipa.artwork` imported) can
    observe.
    """
    probe = (
        "import sys; import graft_ipa.artwork; "
        "print(any(m.startswith('pymobiledevice3') for m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False", (
        "importing graft_ipa.artwork dragged in pymobiledevice3 — move the "
        "import back inside _icon_from_device_async"
    )


# --- The acquisition/validation split, pinned ---------------------------------
#
# Every rung wraps acquisition in `except Exception` because it is talking to
# something we do not control: a file we did not write, a storefront, a phone.
# Validation deliberately sits *outside* that catch, so a bug in our own
# validator surfaces instead of masquerading as "this app has no icon".
#
# That was a convention held up by an argument and by nothing else. A review
# proved it: moving `_looks_like_an_image` inside the `try` left all 38 tests
# in this file green, on both rungs that had it at the time. These three tests
# are the enforcement — they fail if the boundary moves, which is the only
# thing that stops the next refactor from quietly undoing it.


def test_a_validator_bug_propagates_out_of_the_app_store_rung() -> None:
    """A `TypeError` from our own code is a bug, not a missing icon."""
    app = ITunesApp(
        track_id=1, bundle_id="x", name="X", version="1",
        artwork_url="https://example.invalid/512x512bb.jpg",
    )
    resp = MagicMock(status_code=200)
    resp.iter_content = MagicMock(return_value=[_PNG])
    resp.content = _PNG
    with (
        patch.object(artwork, "lookup_by_bundle_id", return_value=app),
        patch.object(requests, "get", return_value=resp),
        patch.object(artwork, "_looks_like_an_image", side_effect=TypeError("bug in our code")),
        pytest.raises(TypeError, match="bug in our code"),
    ):
        artwork.icon_from_app_store("x")


def test_a_validator_bug_propagates_out_of_the_device_rung() -> None:
    """The same boundary, on the rung with the widest acquisition catch."""
    with (
        _a_phone_answering(probe=_PLACEHOLDER, data=_PNG),
        patch.object(artwork, "_looks_like_an_image", side_effect=TypeError("bug in our code")),
        pytest.raises(TypeError, match="bug in our code"),
    ):
        artwork.icon_from_device("com.apple.Preferences", udid="UDID")


def test_a_validator_bug_propagates_out_of_the_ipa_rung(tmp_path: Path) -> None:
    """And on rung one, which grew the same split later than the other two.

    Its acquisition catch is the widest of the three in what it swallows — a
    whole zip and a whole plist parser — which is exactly why the validation
    call must not be inside it.
    """
    ipa = _make_ipa(
        tmp_path / "app.ipa", icon_files=["AppIcon"], entries={"AppIcon@2x.png": _PNG}
    )
    with (
        patch.object(artwork, "_looks_like_an_image", side_effect=TypeError("bug in our code")),
        pytest.raises(TypeError, match="bug in our code"),
    ):
        artwork.icon_from_ipa(ipa)
