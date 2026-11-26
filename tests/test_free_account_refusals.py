"""Every path that needs a paid Apple Developer Program membership refuses on a
free Apple ID, with the same sentence, before it does anything.

A free config has no `signing:` section, so each of these used to end in an
AttributeError on `cfg.signing` — or, worse, in a prompt or a network call
first. Each is driven here against a real free config.yaml and must raise
`PaidAccountRequiredError` naming what was asked for, the paid requirement,
and both ways out (`graft --init`, or a `signing:` section).
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa import lc_manage, setup_portal, signer
from graft_ipa.build_log import BuildLog
from graft_ipa.config import AppConfig, CyanOptions, load_config
from graft_ipa.errors import PaidAccountRequiredError
from graft_ipa.json_output import JsonEmitter

APPS = {
    # Carried over from a paid config: loads, and refuses to build.
    "side": {"bundle_id": "com.example.side", "delivery": "sideload"},
    # No delivery key: livecontainer, on a free config.
    "example": {"bundle_id": "com.example.app"},
}


@pytest.fixture
def free_cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A free config at the default location (`--setup-portal` reads only that)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    path = tmp_path / "cfg" / "graft" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump({
        "cache_dir": str(tmp_path / "ipas"),
        "state_dir": str(tmp_path / "state"),
        "default_device_udid": "UDID1",
        "apps": APPS,
    }))
    return path


def _nothing_else(*_a, **_k):
    raise AssertionError("reached past the free-account refusal")


# --- each path, called where it lives -------------------------------------------


def _build(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_mod, "run_all_checks", _nothing_else)
    cli_mod._build_one(
        cfg=cfg, log=BuildLog(tmp_path / "log.sqlite"), app_name="side", dry_run=False,
        skip_install=False, skip_decrypt_check=False, verbose=False, device=None,
    )


def _dry_run_build(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_mod, "run_all_checks", _nothing_else)
    cli_mod._build_one(
        cfg=cfg, log=BuildLog(tmp_path / "log.sqlite"), app_name="side", dry_run=True,
        skip_install=True, skip_decrypt_check=True, verbose=False, device=None,
    )


def _reinstall(cfg, tmp_path, monkeypatch):
    log = BuildLog(cfg.state_dir / "log.sqlite")
    ipa = tmp_path / "side.ipa"
    ipa.write_bytes(b"signed-by-a-paid-team")
    log.record(app="side", bundle_id="com.example.side", version="1.0", tweaks=[],
               ipa_path=ipa, signing_team="ABCDE12345", udid="UDID1", success=True)
    monkeypatch.setattr(cli_mod, "select_device", _nothing_else)
    monkeypatch.setattr(cli_mod, "install", _nothing_else)
    cli_mod._do_reinstall_build(cfg, 1)


def _lc_install(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(lc_manage, "install", _nothing_else)
    lc_manage.run_install(cfg, udid="UDID1", instances=None, channel=None, force=False,
                          events=lc_manage.LcEvents(None))


def _lc_push_cert(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(lc_manage, "push_cert", _nothing_else)
    lc_manage.run_push_cert(cfg, udid="UDID1", events=lc_manage.LcEvents(None))


def _set_p12_password(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdin", StringIO("hunter2\n"))
    monkeypatch.setattr(cli_mod, "_fatal", _nothing_else)
    cli_mod._do_set_p12_password(cfg)


def _auto_profile(cfg, tmp_path, monkeypatch):
    app = AppConfig(name="auto", bundle_id="com.example.auto", preferred_version="latest",
                    tweaks=[], cyan=CyanOptions(), auto_profile=True)
    signer.plan_profile_for_app(cfg, app)


def _setup_portal(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(setup_portal.Prompt, "ask", staticmethod(_nothing_else))
    setup_portal.run_setup_portal()


def _zsign(cfg, tmp_path, monkeypatch):
    signer.run_zsign(cfg, tmp_path / "in.ipa", tmp_path / "out.ipa", app=cfg.apps["side"])


def _p12_password_lookup(cfg, tmp_path, monkeypatch):
    signer.resolve_p12_password(cfg)


def _add_app(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_mod.itunes, "lookup", _nothing_else)
    cli_mod._do_add_app_url(cfg, "https://apps.apple.com/app/id1", delivery="sideload")


def _add_app_manual(cfg, tmp_path, monkeypatch):
    cli_mod._do_add_app_manual(cfg, name="new", bundle_id="com.example.new",
                               display_name=None, delivery="sideload")


def _set_app_options(cfg, tmp_path, monkeypatch):
    cli_mod._do_set_app_options(cfg, app_name="example", delivery="sideload")


def _preflight_for_sideload(cfg, tmp_path, monkeypatch):
    """Not a refusal by exception: pre-flight reports the same sentence as a
    failed row, and `assert_all_passed` turns it into the build's error."""
    from graft_ipa import preflight

    monkeypatch.setattr(preflight, "list_connected_devices", lambda: [])
    rows = preflight.run_all_checks(cfg, delivery="sideload", will_install=False,
                                    app=cfg.apps["side"])
    [row] = [r for r in rows if r.name == "account"]
    assert not row.ok
    raise PaidAccountRequiredError(row.detail, hint=row.hint)


PATHS = [
    (_build, "delivery: sideload (app 'side')"),
    (_dry_run_build, "delivery: sideload (app 'side')"),
    (_reinstall, "delivery: sideload (app 'side')"),
    (_lc_install, "--lc-install"),
    (_lc_push_cert, "--lc-push-cert"),
    (_set_p12_password, "--set-p12-password"),
    (_auto_profile, "auto_profile: true (app 'auto')"),
    (_setup_portal, "--setup-portal"),
    (_zsign, "signing app 'side'"),
    (_p12_password_lookup, "signing"),
    (_add_app, "--delivery sideload"),
    (_add_app_manual, "--delivery sideload"),
    (_set_app_options, "--delivery sideload"),
    (_preflight_for_sideload, "delivery: sideload (app 'side')"),
]


@pytest.mark.parametrize(("path", "what"), PATHS, ids=[p.__name__.strip("_") for p, _ in PATHS])
def test_each_signing_path_refuses_on_a_free_account(
    path, what: str, free_cfg_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load_config(free_cfg_path)
    before = free_cfg_path.read_text()

    with pytest.raises(PaidAccountRequiredError) as exc:
        path(cfg, tmp_path, monkeypatch)

    assert exc.value.message == (
        f"{what} needs a paid Apple Developer Program membership; this config is "
        "set up for a free Apple ID (it has no signing: section)"
    )
    assert "graft --init" in exc.value.hint
    assert "signing:" in exc.value.hint
    assert free_cfg_path.read_text() == before, "a refusal writes nothing"


def test_a_refused_build_still_ends_its_event_stream(
    free_cfg_path: Path, tmp_path: Path
) -> None:
    """Inside the envelope: the GUI gets `build-end` carrying the reason, not
    an exit code over an empty panel."""
    cfg = load_config(free_cfg_path)
    out = StringIO()

    with pytest.raises(PaidAccountRequiredError):
        cli_mod._build_one(
            cfg=cfg, log=BuildLog(tmp_path / "log.sqlite"), app_name="side", dry_run=False,
            skip_install=False, skip_decrypt_check=False, verbose=False, device=None,
            emitter=JsonEmitter(stream=out),
        )

    events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert events[0]["type"] == "build-start"
    end = events[-1]
    assert end["type"] == "build-end"
    assert end["ok"] is False
    assert "paid Apple Developer Program" in end["error"]


def test_a_livecontainer_app_on_a_free_account_is_not_refused(
    free_cfg_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal is about sideload, not about the account: the same config's
    LiveContainer app gets as far as pre-flight."""
    cfg = load_config(free_cfg_path)
    reached = []
    # A device to go to: the target is resolved before the pre-flight.
    monkeypatch.setattr(cli_mod, "resolve_target",
                        lambda cfg, app, explicit_udid=None: cli_mod.Target("UDID1", None, "usb"))
    monkeypatch.setattr(
        cli_mod, "run_all_checks",
        lambda *a, **k: reached.append(k["delivery"]) or _nothing_else(),
    )

    with pytest.raises(AssertionError, match="reached past"):
        cli_mod._build_one(
            cfg=cfg, log=BuildLog(tmp_path / "log.sqlite"), app_name="example",
            dry_run=False, skip_install=False, skip_decrypt_check=False, verbose=False,
            device=None,
        )
    assert reached == ["livecontainer"]


# --- through the command line ---------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "what"),
    [
        (["--lc-install"], "--lc-install"),
        (["--lc-push-cert"], "--lc-push-cert"),
        (["--set-p12-password"], "--set-p12-password"),
        (["--setup-portal"], "--setup-portal"),
        (["--set-settings", "--signing-team-id", "ABCDE12345"], "--signing-team-id"),
        (["--set-settings", "--signing-cert", "/tmp/x.p12", "--signing-profile", "/tmp/x"],
         "--signing-cert, --signing-profile"),
        (["--add-app-manual", "--name", "n", "--bundle-id", "com.example.n",
          "--delivery", "sideload"], "--delivery sideload"),
        (["--set-app-options", "--app", "example", "--delivery", "sideload"],
         "--delivery sideload"),
    ],
)
def test_the_cli_refuses_with_the_paid_requirement(
    argv: list[str], what: str, free_cfg_path: Path
) -> None:
    before = free_cfg_path.read_text()

    result = CliRunner().invoke(cli_mod.app, argv)
    said = " ".join(result.output.split())  # Rich wraps long lines

    assert result.exit_code == 2, result.output
    assert f"{what} needs a paid Apple Developer Program membership" in said
    assert "graft --init" in said
    assert free_cfg_path.read_text() == before


def test_building_a_sideload_app_from_the_cli_fails_with_the_reason(free_cfg_path: Path) -> None:
    """Exit 1, as for any failed app in a build run, with the reason said."""
    result = CliRunner().invoke(cli_mod.app, ["side"])
    said = " ".join(result.output.split())

    assert result.exit_code == 1, result.output
    assert "delivery: sideload (app 'side') needs a paid Apple Developer Program" in said


def test_set_settings_without_signing_fields_still_works_on_a_free_account(
    free_cfg_path: Path,
) -> None:
    result = CliRunner().invoke(cli_mod.app, ["--set-settings", "--archive-keep", "4"])

    assert result.exit_code == 0, result.output
    assert load_config(free_cfg_path).archive_keep == 4


def test_add_app_defaults_to_livecontainer_on_a_free_account(free_cfg_path: Path) -> None:
    fake = type("Info", (), {"name": "New App", "bundle_id": "com.example.new"})()
    with patch.object(cli_mod.itunes, "lookup", return_value=fake):
        result = CliRunner().invoke(cli_mod.app, ["--add-app", "https://apps.apple.com/us/app/example/id100000001"])

    assert result.exit_code == 0, result.output
    raw = yaml.safe_load(free_cfg_path.read_text())
    # Written explicitly, so the app stays a LiveContainer app if the config
    # later becomes paid (where a missing key means sideload).
    assert raw["apps"]["new_app"]["delivery"] == "livecontainer"


def test_add_app_manual_defaults_to_livecontainer_on_a_free_account(free_cfg_path: Path) -> None:
    result = CliRunner().invoke(
        cli_mod.app, ["--add-app-manual", "--name", "n", "--bundle-id", "com.example.n"]
    )

    assert result.exit_code == 0, result.output
    assert yaml.safe_load(free_cfg_path.read_text())["apps"]["n"]["delivery"] == "livecontainer"


def test_add_app_still_defaults_to_sideload_on_a_paid_account(temp_config_env: Path) -> None:
    result = CliRunner().invoke(
        cli_mod.app, ["--add-app-manual", "--name", "n", "--bundle-id", "com.example.n"]
    )

    assert result.exit_code == 0, result.output
    assert yaml.safe_load(temp_config_env.read_text())["apps"]["n"]["delivery"] == "sideload"


def test_livecontainer_is_accepted_explicitly_on_a_free_account(free_cfg_path: Path) -> None:
    result = CliRunner().invoke(
        cli_mod.app, ["--set-app-options", "--app", "side", "--delivery", "livecontainer"]
    )

    assert result.exit_code == 0, result.output
    assert load_config(free_cfg_path).apps["side"].delivery == "livecontainer"


# --- one refusal, asked in one place ----------------------------------------------
#
# The sideload refusal was written out six times. The delivery checks now all
# ask `Config.require_delivery`; signing keeps one guard of its own at the
# boundary, run once per entry point rather than once per helper it calls.


class _Asked(Exception):
    pass


DELIVERY_PATHS = [_build, _dry_run_build, _reinstall, _add_app, _add_app_manual,
                  _set_app_options, _preflight_for_sideload]


@pytest.mark.parametrize("path", DELIVERY_PATHS, ids=[p.__name__.strip("_") for p in DELIVERY_PATHS])
def test_every_delivery_check_asks_the_one_helper(
    path, free_cfg_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graft_ipa.config import Config

    asked: list[tuple[str, str | None]] = []

    def require_delivery(self, delivery: str, *, app: str | None = None) -> None:
        asked.append((delivery, app))
        raise _Asked

    monkeypatch.setattr(Config, "require_delivery", require_delivery)
    cfg = load_config(free_cfg_path)

    with pytest.raises(_Asked):
        path(cfg, tmp_path, monkeypatch)

    assert asked and asked[0][0] == "sideload"


def test_require_delivery_refuses_sideload_only_on_a_free_account(
    free_cfg_path: Path, tmp_path: Path
) -> None:
    from graft_ipa.config import write_default_config
    from graft_ipa.errors import ConfigError

    paid = tmp_path / "paid" / "config.yaml"
    write_default_config(paid, team_id="ABCDE12345")
    free = load_config(free_cfg_path)
    with pytest.raises(PaidAccountRequiredError, match=r"^delivery: sideload \(app 'x'\) needs"):
        free.require_delivery("sideload", app="x")
    with pytest.raises(PaidAccountRequiredError, match=r"^--delivery sideload needs"):
        free.require_delivery("sideload")
    free.require_delivery("livecontainer")
    load_config(paid).require_delivery("sideload")
    with pytest.raises(ConfigError, match="invalid delivery 'usb'"):
        free.require_delivery("usb")


def _counting_signing_guard(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    real = signer._signing_for

    def counted(cfg, app):
        calls.append(app.name if app else "-")
        return real(cfg, app)

    monkeypatch.setattr(signer, "_signing_for", counted)
    return calls


def _paid_signing_cfg(temp_config_env: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = load_config(temp_config_env)
    cfg.signing.cert.parent.mkdir(parents=True, exist_ok=True)
    cfg.signing.cert.write_bytes(b"p12")
    cfg.signing.profile.write_bytes(b"profile")
    monkeypatch.setenv(cfg.signing.cert_password_env, "pw")
    return cfg


def test_a_dry_run_zsign_passes_the_signing_guard_once(
    temp_config_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _paid_signing_cfg(temp_config_env, monkeypatch)
    calls = _counting_signing_guard(monkeypatch)

    signer.run_zsign(cfg, tmp_path / "in.ipa", tmp_path / "out.ipa",
                     app=cfg.apps["example"], dry_run=True)

    assert calls == ["example"]


def test_a_real_zsign_passes_the_signing_guard_once(
    temp_config_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from graft_ipa.errors import SigningError

    cfg = _paid_signing_cfg(temp_config_env, monkeypatch)
    calls = _counting_signing_guard(monkeypatch)
    monkeypatch.setattr(signer.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "boom"))

    with pytest.raises(SigningError):
        signer.run_zsign(cfg, tmp_path / "in.ipa", tmp_path / "out.ipa", app=cfg.apps["example"])

    assert calls == ["example"]


@pytest.mark.parametrize("entry", ["resolve_p12_password", "plan_profile_for_app"])
def test_the_public_signing_helpers_still_refuse_on_their_own(
    entry: str, free_cfg_path: Path
) -> None:
    """The floor stays under every entry point another module calls."""
    cfg = load_config(free_cfg_path)
    args = (cfg,) if entry == "resolve_p12_password" else (cfg, None)

    with pytest.raises(PaidAccountRequiredError):
        getattr(signer, entry)(*args)
