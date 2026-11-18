"""Hints that name a command the user can actually run (F9, F10, K5, K11, R11).

- F9: a free config's leftover `delivery: sideload` entry for LiveContainer
  itself was told to use `graft --lc-install`, which is paid-only.
- F10: the push failure said "run `pymobiledevice3 apps list`" (not on PATH
  under pipx) and assumed a cable; a sandbox that would not open said "set
  LiveContainer up" about a LiveContainer that was found installed; and
  `--skip-install`'s help said "Build & sign only" for builds that sign
  nothing.
- K5: the bagbak install hint was unpinned, and bagbak 5.x needs frida 17,
  which cannot talk to the 16.x frida-server this project pins.
- K11: the paid-only refusal ran 530-640 characters, past the app's banner
  limit of 420, which then cut it from the middle.
- R11: the frida-ios-dump setup command was glued to prose ("— or set
  GRAFT_FRIDA_IOS_DUMP …"), so a Copy button copied a command that fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from graft_ipa import cli as cli_mod
from graft_ipa import config, decrypt_ready, decrypt_runner, lc_runner
from graft_ipa.config import AppConfig, CyanOptions, load_config
from graft_ipa.errors import ConfigError
from graft_ipa.lc_manage import refuse_legacy_build

#: `ErrorBanner.messageLimit` in the Mac app: past it, the banner cuts the
#: text from the middle.
BANNER_LIMIT = 420


# --- F9 ---------------------------------------------------------------------------


def _lc_itself() -> AppConfig:
    return AppConfig(name="lc", bundle_id="com.kdt.livecontainer", preferred_version="latest",
                     tweaks=[], cyan=CyanOptions(), delivery="sideload")


def test_a_free_account_is_not_sent_to_the_paid_only_lc_install() -> None:
    with pytest.raises(ConfigError) as exc:
        refuse_legacy_build(_lc_itself(), free_account=True)

    assert "--lc-install" not in exc.value.hint
    assert config.FREE_INSTALLERS in exc.value.hint
    assert "remove" in exc.value.hint


def test_a_paid_account_is_still_sent_to_lc_install() -> None:
    with pytest.raises(ConfigError) as exc:
        refuse_legacy_build(_lc_itself(), free_account=False)

    assert "graft --lc-install" in exc.value.hint


def test_a_free_build_of_the_leftover_entry_gets_the_free_hint(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"state_dir": str(tmp_path / "s"), "apps": {
        "lc": {"bundle_id": "com.kdt.livecontainer", "delivery": "sideload"}}}))

    with pytest.raises(ConfigError) as exc:
        cli_mod._build_one(cfg=load_config(path), app_name="lc", dry_run=True,
                           skip_install=True, skip_decrypt_check=True, verbose=False,
                           device=None)

    assert "--lc-install" not in (exc.value.hint or "")


# --- F10 --------------------------------------------------------------------------


def test_a_failed_push_names_a_command_graft_has(monkeypatch, tmp_path: Path) -> None:
    import pymobiledevice3.lockdown as lockdown_mod

    async def unreachable(*, serial):
        raise ConnectionError("no route")

    monkeypatch.setattr(lockdown_mod, "create_using_usbmux", unreachable)
    ipa = tmp_path / "a.ipa"
    ipa.write_bytes(b"x")

    with pytest.raises(lc_runner.LiveContainerError) as exc:
        lc_runner.push_ipa_to_livecontainer(ipa, "UDID1")

    assert "pymobiledevice3" not in exc.value.hint
    assert "plugged in" not in exc.value.hint
    assert "graft --lc-status" in exc.value.hint
    assert "Wi-Fi" in exc.value.hint


def test_a_sandbox_that_will_not_open_is_not_called_missing(monkeypatch, tmp_path: Path) -> None:
    import pymobiledevice3.services.house_arrest as house_arrest_mod

    from .test_lc_discovery import fake_device

    fake_device(monkeypatch, ["com.kdt.livecontainer.ABCDE12345"])

    async def refused(*, lockdown, bundle_id):
        raise RuntimeError("InstallationLookupFailed")

    monkeypatch.setattr(house_arrest_mod.HouseArrestService, "create", refused)
    ipa = tmp_path / "a.ipa"
    ipa.write_bytes(b"x")

    with pytest.raises(lc_runner.LiveContainerError) as exc:
        lc_runner.push_ipa_to_livecontainer(ipa, "UDID1", free_account=True)

    assert "is installed" in exc.value.hint
    assert "set LiveContainer up" not in exc.value.hint
    assert "unlock" in exc.value.hint


def test_skip_install_help_does_not_promise_signing() -> None:
    import typer

    (param,) = [p for p in typer.main.get_command(cli_mod.app).params
                if "--skip-install" in p.opts]
    assert "sign only" not in (param.help or "").lower()
    assert "push" in (param.help or "").lower()


# --- K5 ---------------------------------------------------------------------------


def test_the_bagbak_hint_pins_the_major_that_speaks_frida_16(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(decrypt_runner, "bagbak_available", lambda: None)

    with pytest.raises(decrypt_runner.DecryptError) as exc:
        decrypt_runner.decrypt_app("com.example.app", tmp_path / "dump", udid="UDID1")

    assert decrypt_runner.BAGBAK_INSTALL == "npm install -g bagbak@4.0.3"
    assert decrypt_runner.BAGBAK_INSTALL in exc.value.hint


def test_no_unpinned_bagbak_install_is_left_anywhere() -> None:
    package = Path(cli_mod.__file__).parent
    for source in package.glob("*.py"):
        text = source.read_text()
        assert "npm install -g bagbak " not in text and "npm install -g bagbak\"" not in text, (
            source.name
        )


# --- K11 --------------------------------------------------------------------------


@pytest.mark.parametrize("config_path", [
    None,
    Path("/Users/someone/Documents/configs/graft-elsewhere/config.yaml"),
])
@pytest.mark.parametrize("what", [
    "delivery: sideload (app 'example-with-a-longer-name')",
    "--lc-install",
    "--signing-cert, --signing-profile, --signing-team-id",
])
def test_the_paid_only_refusal_fits_the_banner(what: str, config_path: Path | None) -> None:
    err = config.paid_account_required(what, config_path)
    shown = f"error: {err.message}\n  hint: {err.hint}"

    assert len(shown) <= BANNER_LIMIT, (len(shown), shown)
    # The first sentence says it all on its own: what needs what, and why.
    assert err.message.startswith(f"{what} needs a paid Apple Developer Program membership")
    assert "free Apple ID" in err.message
    assert "--init" in err.hint and config.FREE_ALTERNATIVE in err.hint


# --- R11 --------------------------------------------------------------------------


def test_the_frida_ios_dump_command_is_only_a_command() -> None:
    command = decrypt_runner.frida_ios_dump_setup_command()

    assert "GRAFT_FRIDA_IOS_DUMP" not in command and " — " not in command
    assert command.startswith("git clone ")
    # The full hint keeps the alternative, for a person reading stderr.
    hint = decrypt_runner.frida_ios_dump_setup_hint()
    assert hint.startswith(command) and "GRAFT_FRIDA_IOS_DUMP" in hint


def test_the_backend_note_is_the_command_and_the_reason_has_the_prose(monkeypatch) -> None:
    monkeypatch.setattr(decrypt_runner, "frida_ios_dump_available", lambda: None)
    monkeypatch.setattr(decrypt_runner, "bagbak_available", lambda: None)

    frida = next(b for b in decrypt_ready._backends(True, True, "16.5.9")
                 if b.name == decrypt_runner.FRIDA)

    assert frida.note == decrypt_runner.frida_ios_dump_setup_command()
    assert "GRAFT_FRIDA_IOS_DUMP" in frida.reason
