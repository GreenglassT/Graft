"""A paid-account install to a device the saved `device_udids` predate (spec
§5b): Graft re-reads the registered devices from App Store Connect first,
includes the device when it's registered, says so plainly when it isn't, and
never registers one. App Store Connect is a stand-in throughout."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from graft_ipa import apple_portal, devices_seen, preflight, setup_portal, signer
from graft_ipa.config import AppConfig, CyanOptions, load_config
from graft_ipa.errors import ConfigError, DeviceNotRegisteredError, SigningError

PHONE = "00008030-0000000000000001"
IPAD = "00008030-0000000000000002"
WATCH = "00008030-0000000000000009"
RETIRED = "00008030-00000000000000DD"


class _ASC:
    """A stand-in App Store Connect with `list_devices` and nothing else:
    there is no way to register a device on it, so a call to one fails."""

    def __init__(self, *enabled: str, disabled: tuple[str, ...] = (),
                 watches: tuple[str, ...] = ()) -> None:
        def device(udid: str, status: str, kind: str) -> apple_portal.Device:
            return apple_portal.Device(id=f"id-{udid}", udid=udid, name=udid, platform="IOS",
                                       status=status, device_class=kind)

        self.devices = ([device(u, "ENABLED", "IPAD") for u in enabled]
                        + [device(u, "DISABLED", "IPAD") for u in disabled]
                        + [device(u, "ENABLED", "APPLE_WATCH") for u in watches])
        self.listed = 0

    def list_devices(self, *, platform: str = "IOS") -> list[apple_portal.Device]:
        self.listed += 1
        return self.devices


@pytest.fixture
def asc(monkeypatch: pytest.MonkeyPatch):
    """Make every `ASCClient` Graft builds one stand-in: `asc(PHONE, IPAD)`."""

    def install(*enabled: str, **kwargs) -> _ASC:
        client = _ASC(*enabled, **kwargs)
        monkeypatch.setattr(apple_portal, "ASCClient", lambda creds, session=None: client)
        return client

    return install


def _with_asc(config: Path, *, udids: list[str], pinned: bool = False,
              default: str | None = None) -> None:
    raw = yaml.safe_load(config.read_text())
    raw["app_store_connect"] = {
        "key_id": "ABCDE12345", "issuer_id": "00000000-0000-0000-0000-000000000000",
        "key_file": str(config.parent / "AuthKey_ABCDE12345.p8"), "device_udids": udids,
    }
    if pinned:
        raw["app_store_connect"]["device_udids_pinned"] = True
    if default:
        raw["default_device_udid"] = default
    config.write_text(yaml.safe_dump(raw, sort_keys=False))


def _saved(config: Path) -> list[str]:
    return yaml.safe_load(config.read_text())["app_store_connect"]["device_udids"]


def _auto_app() -> AppConfig:
    return AppConfig(name="example", bundle_id="com.example.app", preferred_version="latest",
                     tweaks=[], cyan=CyanOptions(), delivery="sideload", auto_profile=True)


# --- refresh_registered_devices ----------------------------------------------------


def test_the_refresh_re_reads_and_saves_the_registered_devices(temp_config_env: Path, asc) -> None:
    _with_asc(temp_config_env, udids=[PHONE])
    client = asc(PHONE, IPAD, disabled=(RETIRED,), watches=(WATCH,))

    assert setup_portal.refresh_registered_devices(temp_config_env) == [PHONE, IPAD]
    assert client.listed == 1
    assert _saved(temp_config_env) == [PHONE, IPAD], "a disabled device and a Watch are left out"


def test_a_pinned_list_is_not_re_read(temp_config_env: Path, asc) -> None:
    _with_asc(temp_config_env, udids=[PHONE], pinned=True)
    client = asc(PHONE, IPAD)

    assert setup_portal.refresh_registered_devices(temp_config_env) == [PHONE]
    assert client.listed == 0
    assert _saved(temp_config_env) == [PHONE]


def test_no_key_no_refresh(temp_config_env: Path) -> None:
    with pytest.raises(ConfigError, match="App Store Connect"):
        setup_portal.refresh_registered_devices(temp_config_env)


# --- the profile's devices ------------------------------------------------------


def test_a_target_in_the_saved_list_asks_nothing(temp_config_env: Path, asc) -> None:
    _with_asc(temp_config_env, udids=[PHONE, IPAD])
    client = asc(PHONE, IPAD)

    assert signer.target_profile_udids(load_config(temp_config_env), IPAD) == [PHONE, IPAD]
    assert client.listed == 0


def test_a_target_registered_since_is_read_saved_and_included(temp_config_env: Path, asc) -> None:
    _with_asc(temp_config_env, udids=[PHONE])
    cfg = load_config(temp_config_env)
    client = asc(PHONE, IPAD)

    assert signer.target_profile_udids(cfg, IPAD) == [PHONE, IPAD]
    assert client.listed == 1
    assert _saved(temp_config_env) == [PHONE, IPAD]
    assert cfg.app_store_connect.device_udids == [PHONE, IPAD], "this run sees it too"


def test_a_target_that_is_not_registered_says_so(temp_config_env: Path, asc) -> None:
    _with_asc(temp_config_env, udids=[PHONE])
    cfg = load_config(temp_config_env)
    devices_seen.remember(cfg.state_dir, IPAD, name="iPad Air")
    asc(PHONE)

    with pytest.raises(DeviceNotRegisteredError) as caught:
        signer.target_profile_udids(cfg, IPAD)

    assert caught.value.message == (
        "iPad Air isn't registered in your developer account: add it at developer.apple.com, "
        "then try again"
    )
    assert IPAD in caught.value.hint
    assert caught.value.exit_code == 7


def test_a_disabled_device_is_not_registered(temp_config_env: Path, asc) -> None:
    _with_asc(temp_config_env, udids=[PHONE])
    asc(PHONE, disabled=(IPAD,))

    with pytest.raises(DeviceNotRegisteredError, match=f"^{IPAD} isn't registered"):
        signer.target_profile_udids(load_config(temp_config_env), IPAD)


def test_a_pinned_list_is_kept_and_the_target_still_added(temp_config_env: Path, asc) -> None:
    _with_asc(temp_config_env, udids=[PHONE], pinned=True)
    client = asc(PHONE, IPAD)

    assert signer.target_profile_udids(load_config(temp_config_env), IPAD) == [PHONE, IPAD]
    assert client.listed == 0
    assert _saved(temp_config_env) == [PHONE]


def test_no_target_is_the_saved_list_and_the_default(temp_config_env: Path, asc) -> None:
    _with_asc(temp_config_env, udids=[PHONE], default=IPAD)
    client = asc(PHONE)

    assert signer.target_profile_udids(load_config(temp_config_env), None) == [PHONE, IPAD]
    assert client.listed == 0


def test_the_build_s_profile_is_made_for_its_target_and_says_it_asked(
    temp_config_env: Path, asc, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_asc(temp_config_env, udids=[PHONE])
    cfg = load_config(temp_config_env)
    devices_seen.remember(cfg.state_dir, IPAD, name="iPad Air")
    asc(PHONE, IPAD)
    asked: dict = {}

    def ensure(client, **kwargs):
        asked.update(kwargs)
        return SimpleNamespace(profile_path=Path("/p.mobileprovision"), refresh_reason=None,
                               unregistered_devices=())

    monkeypatch.setattr(apple_portal, "read_p12_cert_serial", lambda cert, password: "SERIAL")
    monkeypatch.setattr(apple_portal, "ensure_profile_for_bundle", ensure)

    resolved = signer.resolve_profile_for_app(cfg, _auto_app(), password="pw", target=IPAD)

    assert asked["device_udids"] == [PHONE, IPAD]
    assert resolved.refreshed_for == "iPad Air"
    assert any("re-read your registered devices" in note and "iPad Air" in note
               for note in signer._provisioning_notes(resolved))


def test_a_refresh_app_store_connect_refuses_is_a_signing_failure(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_asc(temp_config_env, udids=[PHONE])

    class Down:
        def list_devices(self, **_):
            raise apple_portal.PortalError("App Store Connect said 503")

    monkeypatch.setattr(apple_portal, "ASCClient", lambda creds, session=None: Down())
    monkeypatch.setattr(apple_portal, "read_p12_cert_serial", lambda cert, password: "SERIAL")

    with pytest.raises(SigningError, match="auto-provisioning failed for example: App Store "
                                           "Connect said 503"):
        signer.resolve_profile_for_app(load_config(temp_config_env), _auto_app(), password="pw",
                                       target=IPAD)


# --- --lc-install -------------------------------------------------------------------


def test_lc_install_s_profiles_are_made_for_its_device(
    temp_config_env: Path, asc, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graft_ipa import lc_manage

    _with_asc(temp_config_env, udids=[PHONE])
    asc(PHONE, IPAD)
    monkeypatch.setattr(apple_portal, "read_p12_cert_serial", lambda cert, password: "SERIAL")

    profiles = lc_manage.AscProfiles(load_config(temp_config_env), "pw", target=IPAD)

    assert profiles._udids == [PHONE, IPAD]


def test_lc_install_hands_its_device_to_the_profiles(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graft_ipa import lc_manage

    made: list = []
    monkeypatch.setattr(signer, "resolve_p12_password", lambda cfg: "pw")
    monkeypatch.setattr(lc_manage, "AscProfiles",
                        lambda cfg, password, *, target=None, note=None: made.append(target))
    monkeypatch.setattr(lc_manage, "install", lambda **kw: [])

    lc_manage.run_install(load_config(temp_config_env), udid=IPAD, instances=None, channel=None,
                          force=False, events=lc_manage.LcEvents(None))

    assert made == [IPAD]


# --- pre-flight and the build ---------------------------------------------------------


def test_pre_flight_leaves_the_build_s_target_to_stage_four(
    tmp_path: Path, temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An iPad-only bench, the iPad outside the saved list: pre-flight must not
    fail the build before stage 4 can re-read and include it."""
    _with_asc(temp_config_env, udids=[PHONE])
    cfg = load_config(temp_config_env)
    profile = tmp_path / "cached.mobileprovision"
    profile.write_bytes(b"x")
    plan = SimpleNamespace(path=profile, created_on_demand=True)
    monkeypatch.setattr(preflight, "planned_profile", lambda cfg, app: plan)
    monkeypatch.setattr(preflight, "parse_mobileprovision", lambda path: {})
    monkeypatch.setattr(preflight.mobileprovision, "provisioned_devices", lambda data: [PHONE])

    def row(target: str | None):
        results = preflight.check_devices(cfg, check_profile=True, app=_auto_app(),
                                          udids=[IPAD], target=target)
        return next(r for r in results if r.name == "device in provisioning profile")

    assert row(IPAD).ok is True
    assert "re-provisions it at sign time" in row(IPAD).detail
    assert row(None).ok is False, "without the build's target, as before"


def test_the_build_asks_pre_flight_and_stage_four_about_its_device(tmp_path: Path) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa.build_log import BuildLog
    from graft_ipa.installer import Target
    from graft_ipa.json_output import JsonEmitter

    from .test_cli_build_json import _stub_config

    cfg = _stub_config(tmp_path)
    cfg.apps["example"].delivery = "sideload"
    checks = MagicMock(return_value=[])
    signed_with: dict = {}

    def zsign(_cfg, _src, signed, **kwargs):
        signed_with.update(kwargs)
        signed.write_bytes(b"signed")
        return SimpleNamespace(profile_notes=(), stdout="", stderr="", team_id="ABCDE12345",
                               cert_subject="s", expiry="e")

    with patch.object(cli_mod, "run_all_checks", checks), \
         patch.object(cli_mod, "acquire_ipa") as acquire, \
         patch.object(cli_mod, "run_cyan") as cyan, \
         patch.object(cli_mod, "run_zsign", side_effect=zsign), \
         patch.object(cli_mod, "install") as install, \
         patch.object(cli_mod.developer_mode, "require_on"), \
         patch.object(cli_mod, "resolve_target", return_value=Target(IPAD, "iPad Air", "usb")):
        acquire.return_value.path = tmp_path / "in.ipa"
        acquire.return_value.version = "1.0"
        cyan.return_value.output_ipa = tmp_path / "tweaked.ipa"
        cyan.return_value.stdout = cyan.return_value.stderr = ""
        install.return_value.stdout = install.return_value.stderr = ""
        cli_mod._build_one(cfg=cfg, log=BuildLog(tmp_path / "log.sqlite"), app_name="example",
                           dry_run=False, skip_install=False, skip_decrypt_check=True,
                           verbose=False, device=None, emitter=JsonEmitter())

    assert checks.call_args.kwargs["target"] == IPAD
    assert signed_with["target"] == IPAD
    assert install.call_args.kwargs["udid"] == IPAD


def test_a_hand_made_profile_is_judged_by_the_build_s_device_alone(
    tmp_path: Path, temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A profile listing the iPhone, a build going to the iPad: it would be
    refused at the install, so pre-flight says so, even with the iPhone
    plugged in too. With no target known (`--check`), any connected device
    in the profile is still enough, as before."""
    cfg = load_config(temp_config_env)
    profile = tmp_path / "hand-made.mobileprovision"
    profile.write_bytes(b"x")
    plan = SimpleNamespace(path=profile, created_on_demand=False)
    monkeypatch.setattr(preflight, "planned_profile", lambda cfg, app: plan)
    monkeypatch.setattr(preflight, "parse_mobileprovision", lambda path: {})
    monkeypatch.setattr(preflight.mobileprovision, "provisioned_devices", lambda data: [PHONE])

    def row(target: str | None):
        results = preflight.check_devices(cfg, check_profile=True, udids=[PHONE, IPAD],
                                          target=target)
        return next(r for r in results if r.name == "device in provisioning profile")

    assert row(IPAD).ok is False
    assert IPAD in row(IPAD).detail and PHONE not in row(IPAD).detail.split("provisioned=")[0]
    assert row(PHONE).ok is True
    assert row(None).ok is True, "no target: any connected device in the profile"


# --- when App Store Connect is asked, and said to be -----------------------------


def test_lc_install_checks_the_device_before_reading_the_registered_devices(
    temp_config_env: Path, asc, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A device that isn't there stops `--lc-install` before any round trip
    to Apple: the profiles' devices are worked out when first needed."""
    from graft_ipa import lc_manage
    from graft_ipa.lc_runner import LiveContainerError

    _with_asc(temp_config_env, udids=[PHONE])
    client = asc(PHONE, IPAD)
    monkeypatch.setattr(apple_portal, "read_p12_cert_serial", lambda cert, password: "SERIAL")
    monkeypatch.setattr(signer, "resolve_p12_password", lambda cfg: "pw")

    class Gone:
        def installed_apps(self, udid):
            raise LiveContainerError(f"{udid} isn't connected")

    monkeypatch.setattr(lc_manage, "UsbmuxDevice", Gone)

    with pytest.raises(LiveContainerError, match="isn't connected"):
        lc_manage.run_install(load_config(temp_config_env), udid=IPAD, instances=None,
                              channel=None, force=False, events=lc_manage.LcEvents(None))

    assert client.listed == 0


def test_the_refresh_says_it_saved_config_yaml(
    temp_config_env: Path, asc, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write to the person's config.yaml mid-build or mid-install is said,
    on both paths that make one."""
    from graft_ipa import lc_manage

    _with_asc(temp_config_env, udids=[PHONE])
    asc(PHONE, IPAD)
    monkeypatch.setattr(apple_portal, "read_p12_cert_serial", lambda cert, password: "SERIAL")
    monkeypatch.setattr(
        apple_portal, "ensure_profile_for_bundle",
        lambda client, **kw: SimpleNamespace(profile_path=Path("/p.mobileprovision"),
                                             refresh_reason=None, unregistered_devices=()),
    )

    resolved = signer.resolve_profile_for_app(load_config(temp_config_env), _auto_app(),
                                              password="pw", target=IPAD)
    assert any("config.yaml" in note for note in signer._provisioning_notes(resolved))

    _with_asc(temp_config_env, udids=[PHONE])
    said: list[str] = []
    profiles = lc_manage.AscProfiles(load_config(temp_config_env), "pw", target=IPAD,
                                     note=said.append)
    assert profiles._udids == [PHONE, IPAD]
    assert any("config.yaml" in line for line in said), said


@pytest.fixture
def no_app_store_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any App Store Connect client Graft makes fails the test."""

    def forbidden(*args, **kwargs):
        raise AssertionError("asked App Store Connect")

    monkeypatch.setattr(apple_portal, "ASCClient", forbidden)


def test_a_dry_run_asks_app_store_connect_nothing(
    tmp_path: Path, temp_config_env: Path, no_app_store_connect, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even for a device the saved list lacks: the dry run returns before a
    profile is resolved, and the refresh with it."""
    _with_asc(temp_config_env, udids=[PHONE])
    cfg = load_config(temp_config_env)
    src = tmp_path / "in.ipa"
    src.write_bytes(b"ipa")
    monkeypatch.setattr(signer, "_p12_password", lambda cfg: "pw")
    if not cfg.signing.cert.exists():
        cfg.signing.cert.parent.mkdir(parents=True, exist_ok=True)
        cfg.signing.cert.write_bytes(b"p12")

    signer.run_zsign(cfg, src, tmp_path / "out.ipa", app=_auto_app(), dry_run=True, target=IPAD)

    assert cfg.app_store_connect.device_udids == [PHONE]


def test_pre_flight_and_check_ask_app_store_connect_nothing(
    tmp_path: Path, temp_config_env: Path, no_app_store_connect, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the build's device connected and missing from the saved list, as
    far as the profile row: pre-flight counts it as one stage 4 adds, and
    asks nothing to find out."""
    _with_asc(temp_config_env, udids=[PHONE])
    cfg = load_config(temp_config_env)
    profile = tmp_path / "cached.mobileprovision"
    profile.write_bytes(b"x")
    monkeypatch.setattr(preflight, "planned_profile",
                        lambda cfg, app: SimpleNamespace(path=profile, created_on_demand=True))
    monkeypatch.setattr(preflight, "parse_mobileprovision", lambda path: {})
    monkeypatch.setattr(preflight.mobileprovision, "provisioned_devices", lambda data: [PHONE])
    monkeypatch.setattr(preflight, "list_connected_devices", lambda: [IPAD])

    results = preflight.run_all_checks(cfg, delivery="sideload", app=_auto_app(), target=IPAD)
    assert any(r.name == "device in provisioning profile" for r in results), "premise: reached"
    preflight.run_all_checks(cfg, deliveries=preflight.deliveries_in_use(cfg),
                             device_required=False)


def test_listing_devices_asks_app_store_connect_nothing(
    temp_config_env: Path, no_app_store_connect
) -> None:
    from io import StringIO

    from graft_ipa import cli as cli_mod
    from graft_ipa.devices import BriefDevice, DeviceInfo, DeviceListing
    from graft_ipa.json_output import JsonEmitter

    _with_asc(temp_config_env, udids=[PHONE])
    cfg = load_config(temp_config_env)
    ipad = DeviceInfo(udid=IPAD, name="iPad Air", model="iPad13,16", ios_version="26.1",
                      connection_type="USB", livecontainer_bundle_id=None)
    with patch.object(cli_mod, "list_devices", return_value=DeviceListing([ipad], [])), \
         patch.object(cli_mod, "list_devices_brief",
                      return_value=[BriefDevice(udid=IPAD, connection_type="USB")]):
        cli_mod._do_devices(cfg, emitter=JsonEmitter(stream=StringIO()))
        cli_mod._do_devices_brief(emitter=JsonEmitter(stream=StringIO()))


# --- graft --update: one profile for every device the build installs on (spec §11.3) ---


def test_the_other_devices_an_update_installs_on_are_in_the_profile(temp_config_env: Path, asc) -> None:
    """The build installs on the iPhone, then the same archive on the iPad:
    the profile must cover both, so the iPad is re-read like the build's own
    device would be."""
    _with_asc(temp_config_env, udids=[PHONE])
    cfg = load_config(temp_config_env)
    client = asc(PHONE, IPAD)

    assert signer.target_profile_udids(cfg, PHONE, also=[IPAD]) == [PHONE, IPAD]
    assert client.listed == 1
    assert signer.auto_profile_device_udids(cfg, target=PHONE, also=[IPAD]) == [PHONE, IPAD]


def test_a_pinned_list_still_gets_the_other_devices(temp_config_env: Path, asc) -> None:
    _with_asc(temp_config_env, udids=[PHONE], pinned=True)
    client = asc(PHONE, IPAD)

    assert signer.target_profile_udids(load_config(temp_config_env), PHONE, also=[IPAD]) == [PHONE, IPAD]
    assert client.listed == 0


def test_another_device_that_isnt_registered_doesnt_stop_the_build(temp_config_env: Path, asc) -> None:
    """Only the build's own device stops it; App Store Connect leaves the
    other one out of the profile and says so (`unregistered_devices`), and
    that device's install is refused on its own."""
    _with_asc(temp_config_env, udids=[PHONE])
    client = asc(PHONE)

    assert signer.target_profile_udids(load_config(temp_config_env), PHONE, also=[IPAD]) == [PHONE, IPAD]
    assert client.listed == 1


def test_an_update_build_s_profile_is_made_for_every_device(
    temp_config_env: Path, asc, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_asc(temp_config_env, udids=[PHONE])
    cfg = load_config(temp_config_env)
    devices_seen.remember(cfg.state_dir, IPAD, name="iPad Air")
    asc(PHONE, IPAD)
    asked: dict = {}

    def ensure(client, **kwargs):
        asked.update(kwargs)
        return SimpleNamespace(profile_path=Path("/p.mobileprovision"), refresh_reason=None,
                               unregistered_devices=())

    monkeypatch.setattr(apple_portal, "read_p12_cert_serial", lambda cert, password: "SERIAL")
    monkeypatch.setattr(apple_portal, "ensure_profile_for_bundle", ensure)

    resolved = signer.resolve_profile_for_app(cfg, _auto_app(), password="pw", target=PHONE,
                                              also=[IPAD])

    assert asked["device_udids"] == [PHONE, IPAD]
    assert resolved.refreshed_for == "iPad Air", "it says which device it re-read for"


def test_an_update_build_signs_for_its_other_devices_and_hands_back_who_was_left_out(
    tmp_path: Path,
) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa import update_run
    from graft_ipa.build_log import BuildLog
    from graft_ipa.installer import Target
    from graft_ipa.json_output import JsonEmitter

    from .test_cli_build_json import _stub_config

    cfg = _stub_config(tmp_path)
    cfg.apps["example"].delivery = "sideload"
    signed_with: dict = {}

    def zsign(_cfg, _src, signed, **kwargs):
        signed_with.update(kwargs)
        signed.write_bytes(b"signed")
        return SimpleNamespace(profile_notes=(), stdout="", stderr="", team_id="ABCDE12345",
                               cert_subject="s", expiry="e", unregistered_devices=(WATCH,))

    built = update_run.Built(also_for=[IPAD, WATCH])
    with patch.object(cli_mod, "run_all_checks", MagicMock(return_value=[])), \
         patch.object(cli_mod, "acquire_ipa") as acquire, \
         patch.object(cli_mod, "run_cyan") as cyan, \
         patch.object(cli_mod, "run_zsign", side_effect=zsign), \
         patch.object(cli_mod, "install") as install, \
         patch.object(cli_mod.developer_mode, "require_on"), \
         patch.object(cli_mod, "resolve_target", return_value=Target(PHONE, "iPhone", "usb")):
        acquire.return_value.path = tmp_path / "in.ipa"
        acquire.return_value.version = "1.0"
        cyan.return_value.output_ipa = tmp_path / "tweaked.ipa"
        cyan.return_value.stdout = cyan.return_value.stderr = ""
        install.return_value.stdout = install.return_value.stderr = ""
        cli_mod._build_one(cfg=cfg, log=BuildLog(tmp_path / "log.sqlite"), app_name="example",
                           dry_run=False, skip_install=False, skip_decrypt_check=True,
                           verbose=False, device=PHONE, emitter=JsonEmitter(), outcome=built)

    assert signed_with["target"] == PHONE
    assert list(signed_with["also"]) == [IPAD, WATCH]
    assert built.not_in_profile == (WATCH,)
