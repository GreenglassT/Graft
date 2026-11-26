"""Set Up LiveContainer's first step (spec §5c) is `--lc-install`'s own check
of the device against the developer account. Plan E put it there: when the
device is new to `app_store_connect.device_udids`, the registered devices are
read again (`setup_portal.refresh_registered_devices`, through
`signer.target_profile_udids`), and a device that isn't registered stops the
install with what to do. `install` asks the device first, then works out the
profiles' devices (`ProfileSource.check_devices`), then downloads: so a phone
that isn't there costs no round trip to Apple, and one that isn't registered
costs no download.

The app draws that check as the first of Set Up's three steps, done when the
first `lc-install-progress` arrives, and puts a failure that came before any
progress on it (`LiveContainerStore.runSetUp`). So the re-read must come
before the first progress event, and a device that isn't registered must
stop the install before one. These pin that order. Nothing here reaches a
phone, App Store Connect, GitHub or a keychain.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from graft_ipa import apple_portal, lc_manage, setup_portal, signer
from graft_ipa.config import load_config
from graft_ipa.errors import SigningError

from .test_lc_install import FakeDevice, Recorder

PHONE = "00008030-0000000000000001"
IPAD = "00008030-0000000000000002"


class _Fetched(Exception):
    """Where these tests stop `--lc-install`: the download, past the check."""


@pytest.fixture
def lc_install(temp_config_env: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A paid config whose `device_udids` snapshot has the phone and not the
    iPad, and stand-ins for everything `--lc-install` reaches before its
    download: the keychain, App Store Connect's client and the .p12, the
    device. The download itself raises `_Fetched`."""
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["default_device_udid"] = PHONE
    raw["app_store_connect"] = {
        "key_id": "KEYID12345", "issuer_id": "00000000-0000-0000-0000-000000000000",
        "key_file": str(temp_config_env.parent / "AuthKey_KEYID12345.p8"),
        "device_udids": [PHONE],
    }
    temp_config_env.write_text(yaml.safe_dump(raw, sort_keys=False))
    monkeypatch.setenv("GRAFT_NO_KEYCHAIN", "1")
    monkeypatch.setattr(signer, "resolve_p12_password", lambda cfg: "pw")
    monkeypatch.setattr(apple_portal, "ASCClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(apple_portal, "read_p12_cert_serial", lambda cert, password: "SERIAL")
    monkeypatch.setattr(lc_manage, "UsbmuxDevice", lambda: FakeDevice({"com.example.app": {}}))
    fetched: list[str] = []

    def fetch(channel: str, cache_dir: Path, **_: object):
        fetched.append(channel)
        raise _Fetched

    monkeypatch.setattr(lc_manage, "fetch_ipa", fetch)
    return SimpleNamespace(config=temp_config_env, fetched=fetched, rec=Recorder())


def _registered(monkeypatch: pytest.MonkeyPatch, rec: Recorder, *udids: str) -> list[int]:
    """App Store Connect lists `udids` as registered. Each re-read records how
    many `lc-install-progress` events had gone out when it was made."""
    calls: list[int] = []

    def refresh(cfg_path: Path) -> list[str]:
        calls.append(len(rec.of("lc-install-progress")))
        return list(udids)

    monkeypatch.setattr(setup_portal, "refresh_registered_devices", refresh)
    return calls


def _run(env: SimpleNamespace) -> None:
    lc_manage.run_install(
        load_config(env.config), udid=IPAD, instances="1", channel=None, force=False,
        events=env.rec.events,
    )


def test_the_registered_devices_are_read_before_any_progress(
    lc_install: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _registered(monkeypatch, lc_install.rec, PHONE, IPAD)

    with pytest.raises(_Fetched):
        _run(lc_install)

    assert calls == [0], "read once, before the first progress event"
    assert [p["step"] for p in lc_install.rec.of("lc-install-progress")] == ["fetch"]


def test_a_device_that_isnt_registered_stops_before_any_progress(
    lc_install: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _registered(monkeypatch, lc_install.rec, PHONE)

    with pytest.raises(SigningError, match="isn't registered in your developer account"):
        _run(lc_install)

    assert calls == [0]
    assert lc_install.rec.of("lc-install-progress") == [], "the app puts this on Set Up's first step"
    assert lc_install.fetched == []
