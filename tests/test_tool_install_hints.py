"""A new user is told exactly how to install each missing tool.

T2: every `brew install …` hint assumed Homebrew; on a Mac without it the
command fails with "command not found" and the hint has nothing more to say.
Now it says Homebrew is needed first, and for libimobiledevice's tools names
the MacPorts port as well.

T3: `--appstore-status` said "install ipatool 2.8.0 from <url>" and "build
UnfairPlay v0.4.0" — a destination, not a route. It now prints the commands:
the release asset for this Mac's processor, and the clone, checkout and build.
They are printed, never run.

T5: each failing `preflight-check` row, and each tool in `appstore-status`,
carries `install`: the exact command, or null. The app's Copy button reads it
instead of parsing the hint, which still says "install with: …".

C9: an ipatool 2.8.1 found at the path `appstore.ipatool` names is not fixed by
installing 2.8.0 to ~/.local/bin, because the configured path wins; the hint
says so.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest

from graft_ipa import (
    appstore,
    decrypt_runner,
    host,
    install_hints,
    ipatool,
    preflight,
    tools,
    unfairplay,
)
from graft_ipa.config import AppStoreToolsConfig
from graft_ipa.decrypt_runner import DecryptError
from graft_ipa.errors import SigningError
from graft_ipa.json_output import JsonEmitter
from tests.appstore_fakes import FakeTools
from tests.test_ipatool_discovery import _install_copy
from tests.tool_fakes import fake_path

BREW_NOTE = "Homebrew is needed first: https://brew.sh"


@pytest.fixture
def no_homebrew(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install_hints, "homebrew_present", lambda: False)


@pytest.fixture
def homebrew(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install_hints, "homebrew_present", lambda: True)


def _missing(monkeypatch: pytest.MonkeyPatch, *names: str) -> dict[str, preflight.CheckResult]:
    monkeypatch.setattr(tools, "which", lambda tool: None)
    return {r.name: r for r in preflight.check_tools(list(names))}


# ---- T2: Homebrew first ----


def test_a_brew_hint_says_homebrew_is_needed_when_it_is_not_there(
    monkeypatch: pytest.MonkeyPatch, no_homebrew
) -> None:
    rows = _missing(monkeypatch, "zsign")
    assert rows["zsign"].hint.startswith("install with: brew install zsign")
    assert BREW_NOTE in rows["zsign"].hint


def test_no_homebrew_note_when_homebrew_is_installed(
    monkeypatch: pytest.MonkeyPatch, homebrew
) -> None:
    rows = _missing(monkeypatch, "zsign")
    assert rows["zsign"].hint == "install with: brew install zsign"


def test_a_non_brew_hint_gets_no_homebrew_note(monkeypatch: pytest.MonkeyPatch, no_homebrew) -> None:
    rows = _missing(monkeypatch, "security")
    assert "brew.sh" not in rows["security"].hint


def test_cyan_says_pipx_is_needed_first(monkeypatch: pytest.MonkeyPatch, homebrew) -> None:
    monkeypatch.setattr(install_hints, "pipx_present", lambda: False)
    rows = _missing(monkeypatch, "cyan")
    assert "pipx is needed first: brew install pipx" in rows["cyan"].hint
    assert rows["cyan"].install == preflight.REQUIRED_TOOLS["cyan"]


def test_homebrew_is_found_where_it_installs_even_off_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A Mac app's CLI may not have /opt/homebrew/bin on PATH; Homebrew is
    still installed, and "install Homebrew" would be wrong."""
    brew = tmp_path / "brew"
    brew.write_text("#!/bin/sh\n")
    brew.chmod(0o755)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(install_hints, "HOMEBREW_LOCATIONS", (str(brew),))
    assert install_hints.homebrew_present() is True
    monkeypatch.setattr(install_hints, "HOMEBREW_LOCATIONS", (str(tmp_path / "none"),))
    assert install_hints.homebrew_present() is False


def test_the_other_brew_hints_say_it_too(no_homebrew) -> None:
    hint = decrypt_runner.iproxy_install_hint()
    assert hint.startswith("brew install libimobiledevice")
    assert BREW_NOTE in hint and "sudo port install libimobiledevice" in hint


def test_zsign_not_found_says_it_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_homebrew
) -> None:
    from unittest.mock import patch

    from graft_ipa import signer
    from tests.test_signer import make_cfg

    cfg = make_cfg(tmp_path)
    monkeypatch.setenv("TEST_P12_PASS", "supersecret")
    with patch.object(signer.subprocess, "run", side_effect=FileNotFoundError("zsign")), \
         pytest.raises(SigningError) as e:
        signer.run_zsign(cfg, tmp_path / "in.ipa", tmp_path / "out.ipa")
    assert e.value.hint.startswith("brew install zsign")
    assert BREW_NOTE in e.value.hint


# ---- T5: `install` on preflight-check rows ----


def test_a_failing_tool_row_carries_its_install_command(
    monkeypatch: pytest.MonkeyPatch, homebrew
) -> None:
    rows = _missing(monkeypatch, "zsign", "cyan", "security")
    assert rows["zsign"].install == "brew install zsign"
    assert rows["cyan"].install.startswith("pipx install --force https://github.com/")
    # Not a command: there is nothing to paste for a builtin.
    assert rows["security"].install is None


def test_a_passing_row_has_no_install_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "which", fake_path(tmp_path / "path-bin"))
    (row,) = preflight.check_tools(["zsign"])
    assert row.ok and row.install is None


def test_every_preflight_check_event_carries_install(
    tmp_path: Path, temp_config_env: Path, monkeypatch: pytest.MonkeyPatch, homebrew
) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import load_config

    monkeypatch.setattr(tools, "which", fake_path(tmp_path / "path-bin", missing={"zsign"}))
    monkeypatch.setattr(preflight, "list_connected_devices", lambda: [])
    out = StringIO()
    cli_mod._do_check(load_config(temp_config_env), JsonEmitter(stream=out))
    rows = [json.loads(line) for line in out.getvalue().splitlines()]
    checks = [r for r in rows if r["type"] == "preflight-check"]
    assert checks and all("install" in r for r in checks)
    by_name = {r["name"]: r for r in checks}
    assert by_name["zsign"]["install"] == "brew install zsign"
    assert by_name["zsign"]["hint"] == "install with: brew install zsign"
    assert by_name["cyan"]["install"] is None  # present
    assert by_name["connected ios device(s)"]["install"] is None  # not a tool


# ---- T3 + T5: --appstore-status ----


def test_the_ipatool_install_is_the_release_asset_for_this_macs_processor(tmp_path: Path) -> None:
    target = tmp_path / "bin" / "ipatool"
    steps = ipatool.install_steps(target, arch="arm64")
    assert steps == [
        f"mkdir -p {target.parent}",
        "curl -fL https://github.com/majd/ipatool/releases/download/v2.8.0/"
        'ipatool-2.8.0-macos-arm64.tar.gz | tar -xzf - -C "$TMPDIR" bin/ipatool-2.8.0-macos-arm64',
        'chmod +x "$TMPDIR/bin/ipatool-2.8.0-macos-arm64"',
        f'mv "$TMPDIR/bin/ipatool-2.8.0-macos-arm64" {target}',
    ]
    assert ipatool.install_command(target, arch="arm64") == " && ".join(steps)
    assert "macos-amd64" in ipatool.install_command(target, arch="amd64")


def test_the_asset_follows_the_kernel_not_rosetta(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(host, "_sysctl_arm64", lambda: 1)
    assert "macos-arm64" in ipatool.install_command(tmp_path / "ipatool")
    monkeypatch.setattr(host, "_sysctl_arm64", lambda: None)  # Intel
    assert "macos-amd64" in ipatool.install_command(tmp_path / "ipatool")


def test_the_unfairplay_build_is_spelled_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, homebrew
) -> None:
    target = tmp_path / "up" / "unfairplay"
    monkeypatch.setattr(install_hints, "go_present", lambda: False)
    assert unfairplay.build_steps(target) == [
        "brew install go",
        "git clone https://github.com/DrWhax/unfairplay",
        "git -C unfairplay checkout v0.4.0",
        f"mkdir -p {target.parent}",
        f"go -C unfairplay build -o {target} ./cmd/unfairplay",
    ]
    monkeypatch.setattr(install_hints, "go_present", lambda: True)
    assert "brew install go" not in unfairplay.build_steps(target)


def test_the_enrollment_command_says_which_app_in_words() -> None:
    assert unfairplay.ENROLL_COMMAND == (
        'graft --appstore-enroll-command --app '
        '"/Applications/<an iPhone/iPad app from the Mac App Store, opened once>.app"'
    )


@pytest.fixture
def nothing_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppStoreToolsConfig:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(install_hints, "go_present", lambda: True)
    return AppStoreToolsConfig(
        ipatool=tmp_path / "home" / ".local" / "bin" / "ipatool",
        unfairplay=tmp_path / "up" / "unfairplay",
        unfairplay_models=tmp_path / "no-models",
    )


def test_status_prints_the_commands_and_runs_none_of_them(
    nothing_installed: AppStoreToolsConfig, homebrew
) -> None:
    payload = appstore.status(nothing_installed)
    commands = payload["setup_commands"]
    for step in ipatool.install_steps(nothing_installed.ipatool):
        assert step in commands, step
    for step in unfairplay.build_steps(nothing_installed.unfairplay):
        assert step in commands, step
    assert unfairplay.ENROLL_COMMAND in commands
    # Printed, not run.
    assert not nothing_installed.ipatool.exists()
    assert not nothing_installed.unfairplay.exists()


def test_status_tools_carry_install(nothing_installed: AppStoreToolsConfig, homebrew) -> None:
    payload = appstore.status(nothing_installed)
    assert payload["ipatool"]["install"] == ipatool.install_command(nothing_installed.ipatool)
    assert payload["unfairplay"]["install"] == " && ".join(
        unfairplay.build_steps(nothing_installed.unfairplay)
    )


def test_status_install_is_null_for_a_tool_that_is_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Discovery also looks in ~/.local/bin: keep this Mac's out of it.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    fakes = FakeTools(tmp_path / "tools")
    chosen = AppStoreToolsConfig(ipatool=fakes.ipatool, unfairplay=fakes.unfairplay,
                                 unfairplay_models=fakes.models,
                                 explicit=frozenset({"ipatool", "unfairplay"}))
    payload = appstore.status(chosen)
    assert payload["ipatool"]["install"] is None
    assert payload["unfairplay"]["install"] is None


def test_on_an_intel_mac_there_is_no_unfairplay_install(
    nothing_installed: AppStoreToolsConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host, "_sysctl_arm64", lambda: None)
    payload = appstore.status(nothing_installed)
    assert payload["unfairplay"]["install"] is None
    assert payload["ipatool"]["install"] is not None


def test_the_appstore_status_event_carries_install(
    nothing_installed: AppStoreToolsConfig,
) -> None:
    from types import SimpleNamespace

    out = StringIO()
    appstore.report_status(SimpleNamespace(appstore=nothing_installed),
                           emitter=JsonEmitter(stream=out), say=print)
    (event,) = [json.loads(line) for line in out.getvalue().splitlines()]
    assert event["type"] == "appstore-status"
    assert isinstance(event["ipatool"]["install"], str)
    assert isinstance(event["unfairplay"]["install"], str)


# ---- C9: the configured path is the bad one ----


def test_a_bad_ipatool_at_the_configured_path_names_the_config_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    fakes = FakeTools(tmp_path / "tools")
    fakes.set(ipatool_version="2.8.1")
    configured = fakes.ipatool  # appstore.ipatool points straight at the 2.8.1
    chosen = AppStoreToolsConfig(ipatool=configured, unfairplay=fakes.unfairplay,
                                 unfairplay_models=fakes.models, explicit=frozenset({"ipatool"}))

    payload = appstore.status(chosen)

    warning = payload["ipatool"]["warning"]
    assert f"appstore.ipatool in config.yaml points at {configured}" in warning
    assert "~/.local/bin/ipatool" not in warning, "installing there would change nothing"
    # And the command replaces the binary the config names.
    assert payload["ipatool"]["install"] == ipatool.install_command(configured)


def test_a_bad_ipatool_on_path_is_fixed_by_the_default_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    fakes = FakeTools(tmp_path / "tools")
    fakes.set(ipatool_version="2.8.1")
    _install_copy(fakes, tmp_path / "pathdir")
    monkeypatch.setenv("PATH", f"{tmp_path / 'pathdir'}:/usr/bin:/bin")
    defaults = AppStoreToolsConfig(ipatool=home / ".local" / "bin" / "ipatool",
                                   unfairplay=fakes.unfairplay, unfairplay_models=fakes.models)

    payload = appstore.status(defaults)

    assert "~/.local/bin/ipatool" in payload["ipatool"]["warning"]
    assert payload["ipatool"]["install"] == ipatool.install_command(home / ".local" / "bin" / "ipatool")


# ---- T4: frida-ios-dump ----


def test_the_frida_ios_dump_hint_is_the_setup_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "frida-ios-dump"
    monkeypatch.setattr(decrypt_runner, "FRIDA_DUMP_HOME", home)
    hint = decrypt_runner.frida_ios_dump_setup_hint()
    assert f"git clone https://github.com/AloneMonkey/frida-ios-dump {home}" in hint
    # The last frida-16 dump.py: master moved to Frida 17 on 2026-10-14.
    assert f"git -C {home} checkout 56e99b2138fc213fa759b3aeb9717a1fb4ec6a59" in hint
    assert f"python3 -m venv {home / '.venv'}" in hint
    assert f"{home / '.venv' / 'bin' / 'pip'} install 'frida==16.*' paramiko scp tqdm" in hint
    assert "GRAFT_FRIDA_IOS_DUMP" in hint

    with pytest.raises(DecryptError) as e:
        decrypt_runner.decrypt_app("com.example.app", tmp_path / "out", udid=None,
                                   backend=decrypt_runner.FRIDA)
    assert e.value.hint == hint


def test_decrypt_check_says_how_to_set_frida_ios_dump_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graft_ipa import decrypt_ready

    monkeypatch.setattr(decrypt_runner, "FRIDA_DUMP_HOME", tmp_path / "nowhere")
    monkeypatch.setattr(decrypt_runner, "bagbak_available", lambda: None)
    monkeypatch.setattr(decrypt_ready, "_bagbak_frida_version", lambda: None)
    (frida,) = [b for b in decrypt_ready._backends(True, True, "16.5.9")
                if b.name == decrypt_runner.FRIDA]
    assert frida.usable is False
    assert frida.reason.startswith("frida-ios-dump is not set up")
    # The command alone, for the app's Copy button; the prose is in `reason`.
    assert frida.note == decrypt_runner.frida_ios_dump_setup_command()
