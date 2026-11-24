"""Under --json, a build's stdout is the event stream and nothing else (K7).

`_build_one` narrated itself with `console.rule` and `console.print` — the app
heading, "→ stage 2: acquiring IPA", the archive path, the summary — on
stdout, which under --json *is* the event stream. The app's line decoder
drops a line that is not JSON, so nothing broke visibly; but the stream was
not what the README says it is, and a consumer that does not `try?` every line
(a script piping into `jq`) choked on the first one. The narration goes to
stderr under --json, and every stdout line of a build, a dry run and a
re-install must parse as JSON.
"""

from __future__ import annotations

import dataclasses
import functools
import inspect
import json
import plistlib
import subprocess
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa.config import load_config
from graft_ipa.cyan_runner import CyanResult
from graft_ipa.installer import InstallResult
from graft_ipa.json_output import JsonEmitter
from graft_ipa.lc_runner import LCDeliveryResult
from graft_ipa.signer import SignResult

from .test_cli_build_json import _stub_config


@pytest.fixture(autouse=True)
def _work_dirs_in_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """These builds run with verbose=True, which keeps the build's work dir
    (`temp kept at …`); without this every run left four in the real $TMPDIR."""
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


def _assert_only_json(out: str) -> list[dict]:
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines, "the build emitted nothing"
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pytest.fail(f"a stdout line under --json is not JSON: {line!r}")
    return events


def _fakes(tmp_path: Path):
    def cyan(cfg, app, src, out, *, dry_run=False):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"tweaked")
        return CyanResult(output_ipa=out, stdout="cyan says hello", stderr="")

    def zsign(cfg, src, out, *, app=None, dry_run=False, **_):
        out.write_bytes(b"signed")
        return SignResult(output_ipa=out, team_id="ABCDE12345", cert_subject="dev",
                          expiry="2027", stdout="zsign says hello", stderr="")

    def push(ipa, udid, *, dry_run=False, **kw):
        return LCDeliveryResult(udid=udid, bundle_id="com.kdt.livecontainer.ABCDE12345",
                                documents_path="Documents/example.ipa", size_bytes=7)

    return cyan, zsign, push


@pytest.mark.parametrize("delivery", ["livecontainer", "sideload"])
@pytest.mark.parametrize("dry_run", [True, False])
def test_every_stdout_line_of_a_json_build_is_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], delivery: str, dry_run: bool
) -> None:
    cfg = _stub_config(tmp_path)
    cfg.apps["example"].delivery = delivery
    src = tmp_path / "cache" / "com.example.app_1.0.ipa"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"not a zip, and scrub_staged leaves that to the build's checks")
    cyan, zsign, push = _fakes(tmp_path)

    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "acquire_ipa") as acquire, \
         patch.object(cli_mod, "run_cyan", side_effect=cyan), \
         patch.object(cli_mod, "run_zsign", side_effect=zsign), \
         patch.object(cli_mod, "select_device", return_value=("UDID1", "USB")), \
         patch.object(cli_mod, "install",
                      return_value=InstallResult(udid="UDID1", stdout="Install: Complete",
                                                 stderr="")), \
         patch.object(cli_mod.lc_runner, "push_ipa_to_livecontainer", side_effect=push):
        acquire.return_value.path = src
        acquire.return_value.version = "1.0"
        code = cli_mod._verb_build_apps(
            cfg=cfg, apps=["example"], dry_run=dry_run, skip_install=False,
            for_playcover=False, skip_decrypt_check=True, verbose=True, device=None,
            json_mode=True,
        )

    captured = capsys.readouterr()
    assert code == 0, captured.err
    events = _assert_only_json(captured.out)
    assert events[0]["type"] == "build-start" and events[-1]["type"] == "build-end"
    # The narration still exists, for a person reading the terminal.
    assert "stage 3" in captured.err


def test_a_json_build_that_fails_keeps_stdout_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _stub_config(tmp_path)
    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "acquire_ipa", side_effect=OSError("disk went away")):
        code = cli_mod._verb_build_apps(
            cfg=cfg, apps=["example"], dry_run=False, skip_install=True, for_playcover=False,
            skip_decrypt_check=True, verbose=False, device=None, json_mode=True,
        )

    captured = capsys.readouterr()
    assert code == 1
    events = _assert_only_json(captured.out)
    assert events[-1]["ok"] is False
    assert "disk went away" in captured.err


def test_every_stdout_line_of_a_json_reinstall_is_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from graft_ipa.build_log import BuildLog

    cfg = _stub_config(tmp_path)
    ipa = tmp_path / "built" / "example.ipa"
    ipa.parent.mkdir(parents=True)
    ipa.write_bytes(b"unsigned")
    build_id = BuildLog(cfg.state_dir / "log.sqlite").record(
        app="example", bundle_id="com.example.app", version="1.0", tweaks=[], ipa_path=ipa,
        signing_team=None, udid="UDID1", success=True, delivery="livecontainer",
    )
    _cyan, _zsign, push = _fakes(tmp_path)

    with patch.object(cli_mod, "select_device", return_value=("UDID1", "USB")), \
         patch.object(cli_mod.lc_runner, "push_ipa_to_livecontainer", side_effect=push):
        cli_mod._verb_reinstall_build(cfg=cfg, reinstall_build=build_id, json_mode=True)

    captured = capsys.readouterr()
    _assert_only_json(captured.out)
    assert "re-installing" in captured.err


def test_without_json_the_narration_stays_on_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _stub_config(tmp_path)
    cyan, _zsign, _push = _fakes(tmp_path)
    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "acquire_ipa") as acquire, \
         patch.object(cli_mod, "run_cyan", side_effect=cyan):
        acquire.return_value.path = tmp_path / "in.ipa"
        acquire.return_value.version = "1.0"
        cli_mod._verb_build_apps(
            cfg=cfg, apps=["example"], dry_run=True, skip_install=True, for_playcover=False,
            skip_decrypt_check=True, verbose=False, device=None, json_mode=False,
        )

    assert "stage 3" in capsys.readouterr().out


# --- every verb, not only the build (P1) ----------------------------------------
#
# K8 made `--install-to-mac` take the buyer's Apple ID data out of an IPA
# first, and `_without_account_data` says so with a `console.print` — on
# stdout, ahead of the JSON events, because only the build and the re-install
# had moved `console` to stderr. The switch is now made once, by `main`, for
# every verb that answers --json with events, so a helper's narration cannot
# land in the stream whichever verb reaches it.


def _account_data_ipa(path: Path) -> Path:
    """A staged IPA from before staging took the buyer's data out."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("iTunesMetadata.plist", plistlib.dumps({"appleId": "buyer@example.com"}))
        zf.writestr("Payload/X.app/Info.plist", plistlib.dumps({
            "CFBundleIdentifier": "com.example.app", "CFBundleShortVersionString": "1.0",
        }))
        zf.writestr("Payload/X.app/SC_Info/X.sinf", b"licence")
    return path


def test_every_stdout_line_of_a_json_install_to_mac_is_json(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ipa = _account_data_ipa(load_config(temp_config_env).cache_dir / "com.example.app_1.0.ipa")
    real_run = subprocess.run

    def run(argv, *args, **kwargs):
        if argv[:2] == ["open", "-b"]:  # the hand-over; nothing launches PlayCover
            return subprocess.CompletedProcess(argv, 0, "", "")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(cli_mod.subprocess, "run", run)
    monkeypatch.setattr(cli_mod, "_tame_playcover_settings", lambda *_a, **_k: None)

    result = CliRunner().invoke(cli_mod.app, ["--json", "--install-to-mac", ipa.name])

    assert result.exit_code == 0, result.output
    events = _assert_only_json(result.stdout)
    assert events[-1]["type"] == "handed-to-playcover"
    # The narration is still said, where a person at a terminal reads it.
    assert "removing Apple ID data from" in result.stderr


def _json_verbs() -> list:
    return [v for v in cli_mod._VERBS
            if isinstance(v, cli_mod._Verb) and cli_mod._speaks_json(v)]


def _argv_selecting(verb) -> list[str]:
    """The fewest arguments that select `verb`: its flag, and a value if it
    takes one, read off `main`'s annotation for it (`bool`, `int | None`,
    `str | None` or `Path | None`)."""
    kind = str(inspect.signature(cli_mod.main).parameters[verb.param].annotation)
    if kind == "bool":
        return [verb.flag]
    return [verb.flag, "1" if kind.startswith("int") else "x"]


def _narrating(verb):
    """`_VERBS` with `verb`'s handler replaced by one that narrates and emits
    one event. `functools.wraps` keeps the real handler's signature visible,
    so `main` judges the stand-in exactly as it judges the verb."""
    @functools.wraps(verb.run)
    def run(**_opts):
        cli_mod.console.print(f"narration from {verb.flag}")
        JsonEmitter().emit("log", source="graft", line="an event")

    return tuple(dataclasses.replace(v, run=run) if v is verb else v for v in cli_mod._VERBS)


def test_the_json_verbs_are_most_of_them() -> None:
    """The parametrisation below is over something: a `_speaks_json` that
    answered False for everything would pass it vacuously."""
    flags = {v.flag for v in _json_verbs()}
    assert {"--install-to-mac", "--list", "--check", "--reinstall-build"} <= flags
    assert not flags & {"--version", "--setup-portal", "--build-log"}


@pytest.mark.parametrize("verb", _json_verbs(), ids=lambda v: v.flag)
def test_under_json_every_json_verb_narrates_on_stderr(verb, temp_config_env: Path) -> None:
    with patch.object(cli_mod, "_VERBS", _narrating(verb)):
        result = CliRunner().invoke(cli_mod.app, ["--json", *_argv_selecting(verb)])

    assert result.exit_code == 0, result.output
    _assert_only_json(result.stdout)
    assert f"narration from {verb.flag}" in result.stderr


@pytest.mark.parametrize(("argv", "stream"), [
    (["--json"], "stderr"),     # P5: stdout is the event stream, and usage is no event
    (["--dry-run"], "stdout"),  # a terminal's, where it always was (bare `graft` is --help)
])
def test_naming_no_app_prints_the_usage_and_exits_64(
    temp_config_env: Path, argv: list[str], stream: str
) -> None:
    result = CliRunner().invoke(cli_mod.app, argv)

    assert result.exit_code == 64, result.output
    assert "usage: graft APP [APP ...]   (see --help)" in getattr(result, stream)
    if stream == "stderr":
        assert result.stdout == ""


def test_without_json_a_verbs_narration_stays_on_stdout(temp_config_env: Path) -> None:
    verb = next(v for v in _json_verbs() if v.flag == "--install-to-mac")
    with patch.object(cli_mod, "_VERBS", _narrating(verb)):
        result = CliRunner().invoke(cli_mod.app, _argv_selecting(verb))

    assert result.exit_code == 0, result.output
    assert "narration from --install-to-mac" in result.stdout
