"""`developer_mode`: the one answer readiness and the build gate share.

`live=False` is `--update --plan --quick`'s: the last reading, no session.
`live=True` reads the phone now and remembers what it said.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from graft_ipa import developer_mode, device_info, devices_seen, signer
from graft_ipa.config import AppConfig, AppStoreConnectConfig, Config, CyanOptions, SigningConfig
from graft_ipa.errors import DeveloperModeOffError, PreflightError

UDID = "00008030-0000000000000001"
SENTENCE = (
    "Turn on Developer Mode on your iPhone or iPad in Settings › Privacy & Security › "
    "Developer Mode, then restart it."
)


def _no_session(udid: str) -> bool | None:
    raise AssertionError("the quick answer opened a session")


def test_the_quick_answer_is_the_last_reading_and_opens_no_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    devices_seen.remember(tmp_path, UDID, developer_mode=False)
    monkeypatch.setattr(device_info, "read_developer_mode", _no_session)

    assert developer_mode.status(tmp_path, UDID, live=False) is False


def test_the_quick_answer_for_a_phone_never_read_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(device_info, "read_developer_mode", _no_session)

    assert developer_mode.status(tmp_path, UDID, live=False) is None


def test_the_quick_answer_does_not_load_pymobiledevice3(tmp_path: Path) -> None:
    probe = (
        "import sys\n"
        "from pathlib import Path\n"
        "from graft_ipa import developer_mode\n"
        f"developer_mode.status(Path({str(tmp_path)!r}), 'X', live=False)\n"
        "print('pymobiledevice3' in sys.modules)\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)

    assert out.stdout.strip() == "False"


def test_the_live_answer_is_read_and_remembered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(device_info, "read_developer_mode", lambda udid: asked.append(udid) or True)

    assert developer_mode.status(tmp_path, UDID, live=True) is True
    assert asked == [UDID]
    assert devices_seen.seen(tmp_path, UDID) == {"developer_mode": True}


def test_a_live_read_with_no_answer_keeps_the_last_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    devices_seen.remember(tmp_path, UDID, developer_mode=False)
    monkeypatch.setattr(device_info, "read_developer_mode", lambda udid: None)

    assert developer_mode.status(tmp_path, UDID, live=True) is None
    assert developer_mode.status(tmp_path, UDID, live=False) is False


def _checked(
    on: bool | None, *, name: str | None = None, model_name: str | None = None,
    revealed: bool = False, reveal_error: str | None = None, asked: list | None = None,
):
    """A stand-in for `device_info.check_developer_mode` that answers `on`,
    recording each `(udid, reveal)` it was asked."""
    def check(udid: str, *, reveal: bool) -> device_info.DeveloperModeCheck:
        if asked is not None:
            asked.append((udid, reveal))
        return device_info.DeveloperModeCheck(
            on=on, name=name, model_name=model_name, revealed=revealed, reveal_error=reveal_error,
        )
    return check


def test_off_is_refused_with_the_spec_s_words(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(device_info, "check_developer_mode", _checked(False))

    with pytest.raises(DeveloperModeOffError) as caught:
        developer_mode.require_on(tmp_path, UDID)

    assert caught.value.hint == SENTENCE == developer_mode.TURN_ON
    assert caught.value.message == f"Developer Mode is off on {UDID}"
    # A device not ready. A build that hits it still exits 1, as any failed
    # build does: the reason is its `build-end.error`.
    assert isinstance(caught.value, PreflightError)
    assert devices_seen.seen(tmp_path, UDID) == {"developer_mode": False}


@pytest.mark.parametrize("answer", [True, None])
def test_on_and_unknown_are_never_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: bool | None
) -> None:
    monkeypatch.setattr(device_info, "check_developer_mode", _checked(answer))

    developer_mode.require_on(tmp_path, UDID, reveal=True)


def test_the_refusal_reads_the_phone_now_not_the_last_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Turned on since the last `--device-info`: the build goes ahead."""
    devices_seen.remember(tmp_path, UDID, developer_mode=False)
    monkeypatch.setattr(device_info, "check_developer_mode", _checked(True))

    developer_mode.require_on(tmp_path, UDID)

    assert devices_seen.seen(tmp_path, UDID) == {"developer_mode": True}


def test_the_hint_names_the_device_when_it_can(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(device_info, "check_developer_mode",
                        _checked(False, name="Sample's iPad", model_name="iPad Air"))

    with pytest.raises(DeveloperModeOffError) as caught:
        developer_mode.require_on(tmp_path, UDID)

    assert caught.value.hint == (
        "Turn on Developer Mode on your iPad Air in Settings › Privacy & Security › "
        "Developer Mode, then restart it."
    )


def test_the_hint_never_names_the_device_by_its_own_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the model's name or the family goes in "your …": with neither,
    the hint says "your iPhone or iPad", not the name Graft remembers, which
    is the one the device gives itself ("your Sample's iPad")."""
    devices_seen.remember(tmp_path, UDID, name="Sample's iPad")
    monkeypatch.setattr(device_info, "check_developer_mode", _checked(False, name="Sample's iPad"))

    with pytest.raises(DeveloperModeOffError) as caught:
        developer_mode.require_on(tmp_path, UDID)

    assert caught.value.hint == SENTENCE
    assert caught.value.message == "Developer Mode is off on Sample's iPad"


def test_a_revealed_switch_is_said_in_the_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list = []
    monkeypatch.setattr(
        device_info, "check_developer_mode",
        _checked(False, name="iPad Air", model_name="iPad Air", revealed=True, asked=asked),
    )

    with pytest.raises(DeveloperModeOffError) as caught:
        developer_mode.require_on(tmp_path, UDID, reveal=True)

    assert asked == [(UDID, True)]
    assert caught.value.hint == (
        "Turn on Developer Mode on your iPad Air in Settings › Privacy & Security › "
        "Developer Mode; Graft has made the option visible. The device restarts; "
        "confirm after it does."
    )
    assert developer_mode.turn_on(revealed=True).startswith(
        "Turn on Developer Mode on your iPhone or iPad in Settings"
    )


def test_a_reveal_that_failed_is_warned_and_the_hint_is_the_plain_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    warned: list[str] = []
    monkeypatch.setattr(
        device_info, "check_developer_mode", _checked(False, reveal_error="no answer in time"),
    )

    with pytest.raises(DeveloperModeOffError) as caught:
        developer_mode.require_on(tmp_path, UDID, reveal=True, warn=warned.append)

    assert caught.value.hint == developer_mode.TURN_ON
    (warning,) = warned
    assert "no answer in time" in warning and UDID in warning


@pytest.mark.parametrize("answer", [True, False, None])
def test_require_on_reveals_only_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: bool | None
) -> None:
    asked: list = []
    monkeypatch.setattr(device_info, "check_developer_mode", _checked(answer, asked=asked))

    try:
        developer_mode.require_on(tmp_path, UDID)
    except DeveloperModeOffError:
        pass

    assert asked == [(UDID, False)]


def test_readiness_never_reveals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`status` is `--update`'s readiness and its plans: off, it says so,
    and asks the phone for nothing."""
    asked: list = []
    monkeypatch.setattr(device_info, "check_developer_mode", _checked(False, asked=asked))

    assert developer_mode.status(tmp_path, UDID, live=True) is False
    assert developer_mode.status(tmp_path, UDID, live=False) is False
    assert asked == [(UDID, False)]


# --- Only development-signed installs need it (spec §5) -------------------------
#
# `needed` is the rule the build gate asks, and `--update`'s readiness and the
# `app` event ask it too: a sideload app whose signing profile is a development
# one, its entitlements saying `get-task-allow: true`. Ad hoc and enterprise
# profiles say false. None is "don't know", and never blocks.

DEVELOPMENT = {"get-task-allow": True, "application-identifier": "ABCDE12345.com.example.*"}
AD_HOC = {"get-task-allow": False, "application-identifier": "ABCDE12345.com.example.*"}


def _profile(path: Path, entitlements: dict | None) -> Path:
    """A profile as `mobileprovision.decode` reads one: the XML plist a CMS
    envelope carries verbatim, here without the envelope."""
    plist: dict = {"Name": "Example", "TeamIdentifier": ["ABCDE12345"]}
    if entitlements is not None:
        plist["Entitlements"] = entitlements
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_XML))
    return path


def _setup(
    tmp_path: Path, *, delivery: str = "sideload", free: bool = False, **app: object
) -> tuple[Config, AppConfig]:
    """A config whose global profile is `tmp_path/global.mobileprovision`
    (written by the test, or not), with one app."""
    app_cfg = AppConfig(
        name="example", bundle_id="com.example.app", preferred_version="latest",
        tweaks=[], cyan=CyanOptions(), delivery=delivery, **app,
    )
    cfg = Config(
        path=tmp_path / "config.yaml",
        signing=None if free else SigningConfig(
            cert=tmp_path / "cert.p12", cert_password_env="X",
            profile=tmp_path / "global.mobileprovision", team_id="ABCDE12345",
        ),
        cache_dir=tmp_path / "cache",
        built_dir=tmp_path / "built",
        tweaks_dir=tmp_path / "tweaks",
        state_dir=tmp_path / "state",
        apps={"example": app_cfg},
        default_device_udid=UDID,
        app_store_connect=AppStoreConnectConfig(
            key_id="KEY", issuer_id="ISSUER", key_file=tmp_path / "key.p8",
            device_udids=[UDID], profile_cache_dir=tmp_path / "profiles",
        ),
    )
    return cfg, app_cfg


def test_a_development_profile_needs_it(tmp_path: Path) -> None:
    cfg, app = _setup(tmp_path)
    _profile(cfg.signing.profile, DEVELOPMENT)

    assert developer_mode.needed(cfg, app) is True


@pytest.mark.parametrize("entitlements", [
    AD_HOC,  # ad hoc and enterprise profiles both say False
    {"application-identifier": "ABCDE12345.com.example.*"},
    None,
    {"get-task-allow": "true"},
    {"get-task-allow": 1},
], ids=["false", "no-key", "no-entitlements", "a-string", "a-number"])
def test_a_profile_that_does_not_grant_get_task_allow_never_needs_it(
    tmp_path: Path, entitlements: dict | None
) -> None:
    """Only a real `True`: a profile is a downloaded file, and `"true"` or
    `1` there is a value nobody understood, which never stops a build."""
    cfg, app = _setup(tmp_path)
    _profile(cfg.signing.profile, entitlements)

    assert developer_mode.needed(cfg, app) is False


def test_it_reads_the_profile_that_signs_the_app(tmp_path: Path) -> None:
    """A per-app `profile:` outranks the global one (`signer._plan_profile`),
    so the global one says nothing about this app's install."""
    cfg, app = _setup(tmp_path, profile_override=tmp_path / "override.mobileprovision")
    _profile(cfg.signing.profile, DEVELOPMENT)
    _profile(app.profile_override, AD_HOC)
    assert developer_mode.needed(cfg, app) is False

    _profile(cfg.signing.profile, AD_HOC)
    _profile(app.profile_override, DEVELOPMENT)
    assert developer_mode.needed(cfg, app) is True


@pytest.mark.parametrize("cached", [None, b"not a profile", "ad-hoc"])
def test_an_auto_provisioned_profile_always_needs_it(
    tmp_path: Path, cached: bytes | str | None
) -> None:
    """Graft provisions, and reuses, only development profiles
    (`IOS_APP_DEVELOPMENT`). So the answer doesn't wait for stage 4 to make
    the first one, or turn on a cache stage 4 may replace."""
    cfg, app = _setup(tmp_path, auto_profile=True)
    _profile(cfg.signing.profile, AD_HOC)
    cache = signer.plan_profile_for_app(cfg, app).path
    if cached == "ad-hoc":
        _profile(cache, AD_HOC)
    elif cached is not None:
        cache.parent.mkdir(parents=True)
        cache.write_bytes(cached)

    assert developer_mode.needed(cfg, app) is True


@pytest.mark.parametrize("on_disk", [None, b"not a profile"], ids=["missing", "unreadable"])
def test_a_profile_of_the_users_that_cannot_be_read_is_no_answer(
    tmp_path: Path, on_disk: bytes | None
) -> None:
    """Pre-flight fails that build before the gate; elsewhere (readiness,
    the `app` event), a profile Graft can't read is "don't know"."""
    cfg, app = _setup(tmp_path)
    if on_disk is not None:
        cfg.signing.profile.write_bytes(on_disk)

    assert developer_mode.needed(cfg, app) is None


def test_a_profile_the_config_cannot_name_is_no_answer(tmp_path: Path) -> None:
    """`auto_profile` with no App Store Connect key: `plan_profile_for_app`
    raises, and that's pre-flight's to say, not this."""
    cfg, app = _setup(tmp_path, auto_profile=True)
    cfg.app_store_connect = None
    _profile(cfg.signing.profile, DEVELOPMENT)

    assert developer_mode.needed(cfg, app) is None


def test_the_profile_is_planned_never_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolving can call App Store Connect and read the .p12's password
    from the keychain: an `auto_profile` app is answered with neither."""

    def resolved(*_: object, **__: object) -> None:
        raise AssertionError("needed resolved the profile")

    monkeypatch.setattr(signer, "resolve_profile_for_app", resolved)
    monkeypatch.setattr(signer, "_resolve_profile", resolved)
    monkeypatch.setattr(signer, "_p12_password", resolved)
    cfg, app = _setup(tmp_path, auto_profile=True)

    assert developer_mode.needed(cfg, app) is True


def test_livecontainer_delivery_never_needs_it(tmp_path: Path) -> None:
    cfg, app = _setup(tmp_path, delivery="livecontainer")
    _profile(cfg.signing.profile, DEVELOPMENT)

    assert developer_mode.needed(cfg, app) is False


def test_a_free_apple_id_never_needs_it(tmp_path: Path) -> None:
    """It can't sideload at all (`Config.require_delivery` says so), and has
    no profile to read: no answer here, rather than a second refusal."""
    cfg, app = _setup(tmp_path, free=True)

    assert developer_mode.needed(cfg, app) is False


# --- The `app` event carries the answer (the device page's row reads it) -----


def _listed(config: Path) -> object:
    import json

    from typer.testing import CliRunner

    from graft_ipa.cli import app as cli_app

    result = CliRunner().invoke(cli_app, ["--json", "--list"])
    assert result.exit_code == 0, result.output
    (event,) = [
        e for e in map(json.loads, (line for line in result.stdout.splitlines() if line.startswith("{")))
        if e["type"] == "app" and e["name"] == "example"
    ]
    assert "needs_developer_mode" in event, "the key is always there, null when unknown"
    return event["needs_developer_mode"]


def test_the_app_event_says_whether_it_needs_developer_mode(temp_config_env: Path) -> None:
    """The app can't read a profile's entitlements, so the CLI says it:
    true, false, or null when the profile can't be read."""
    from graft_ipa.config import load_config

    profile = load_config(temp_config_env).signing.profile
    assert not profile.exists(), "premise: the test config's profile isn't on disk"
    assert _listed(temp_config_env) is None

    _profile(profile, DEVELOPMENT)
    assert _listed(temp_config_env) is True

    _profile(profile, AD_HOC)
    assert _listed(temp_config_env) is False
