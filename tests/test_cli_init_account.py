"""`graft --init --account free|paid`: the setup without questions, as the
Mac app runs it. The password arrives on stdin and goes nowhere but the
Keychain; every step and the result are events under --json."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa import keychain
from graft_ipa.config import FREE_DEVICE_STEPS, load_config

from .keychain_fakes import FakeSecurity
from .signing_fixtures import COMMON_NAME, PASSWORD, TEAM, UDIDS, make_identity


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("GRAFT_P12_PASS", raising=False)
    return tmp_path / "cfg" / "graft" / "config.yaml"


@pytest.fixture
def fake_keychain(monkeypatch: pytest.MonkeyPatch) -> FakeSecurity:
    fake = FakeSecurity()
    monkeypatch.setattr(keychain, "_run", fake)
    return fake


def _invoke(args: list[str], *, input: str | None = None):
    return CliRunner().invoke(cli_mod.app, args, input=input)


def _events(stdout: str) -> list[dict]:
    """Every stdout line, each of which must be a JSON event; startup notices dropped."""
    events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    return [e for e in events if e["type"] != "log"]


def _paid_args(ident, *extra: str) -> list[str]:
    return ["--json", "--init", "--account", "paid", "--p12", str(ident.p12),
            "--profile", str(ident.profile), *extra]


def test_free_writes_a_free_config_and_says_so_in_events(cfg_path: Path) -> None:
    result = _invoke(["--json", "--init", "--account", "free"])

    assert result.exit_code == 0, result.output
    events = _events(result.stdout)
    assert [e["type"] for e in events] == ["init-step", "init-result"]
    done = events[-1]
    assert (done["account"], done["change"], done["dry_run"]) == ("free", "created", False)
    assert done["steps"] == list(FREE_DEVICE_STEPS)
    assert done["team_id"] is None and done["devices"] == [] and done["device_count"] is None
    assert load_config(cfg_path).free_account


def test_paid_takes_the_password_from_stdin_and_reports_the_profile(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    ident = make_identity(tmp_path)

    result = _invoke(_paid_args(ident), input=PASSWORD + "\n")

    assert result.exit_code == 0, result.output
    events = _events(result.stdout)
    assert [e["step"] for e in events if e["type"] == "init-step"] == [
        "profile", "certificate", "keychain", "files", "config",
    ]
    done = events[-1]
    assert done["type"] == "init-result"
    assert (done["team_id"], done["device_count"], done["devices"]) == (TEAM, 2, list(UDIDS))
    assert done["certificate"] == COMMON_NAME
    assert done["expires"].endswith("Z")
    assert fake_keychain.items[("graft", f"p12-{TEAM}")] == PASSWORD
    for where in (result.stdout, result.stderr, cfg_path.read_text()):
        assert PASSWORD not in where


def test_a_dry_run_checks_everything_and_writes_nothing(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    result = _invoke(_paid_args(make_identity(tmp_path), "--dry-run"), input=PASSWORD + "\n")

    assert result.exit_code == 0, result.output
    done = _events(result.stdout)[-1]
    assert done["dry_run"] is True and done["team_id"] == TEAM
    # The directory itself may exist: `main` makes Graft's directories 0700
    # at startup (`secure_app_dirs`) for every verb. What a dry run must not
    # write is anything in it.
    for name in ("config.yaml", "AppleDevelopment.p12", "wildcard.mobileprovision"):
        assert not (cfg_path.parent / name).exists(), name
    assert fake_keychain.items == {}


def test_a_wrong_password_is_refused_in_words_and_reports_no_result(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    result = _invoke(_paid_args(make_identity(tmp_path)), input="not it\n")

    assert result.exit_code == 2, result.output
    assert "couldn't open Certificates.p12 with that password" in " ".join(result.stderr.split())
    assert "init-result" not in result.stdout
    assert not cfg_path.exists()


@pytest.mark.parametrize("args", [
    ["--init", "--json"],
    ["--init", "--dry-run"],
    ["--init", "--p12", "x.p12"],
])
def test_without_account_init_is_the_interactive_one_and_refuses_these(
    cfg_path: Path, args: list[str]
) -> None:
    result = _invoke(args)

    assert result.exit_code == 2, result.output
    assert "with --init needs --account free or --account paid" in " ".join(result.stderr.split())
    assert not cfg_path.exists()


@pytest.mark.parametrize(("args", "flag"), [
    (["--list", "--account", "free"], "--account"),
    (["--check", "--p12", "x.p12"], "--p12"),
    (["example", "--profile", "x.mobileprovision"], "--profile"),
])
def test_the_setup_flags_are_refused_beside_anything_else(
    temp_config_env: Path, args: list[str], flag: str
) -> None:
    result = _invoke(args)

    assert result.exit_code == 2, result.output
    assert f"{flag} is not valid with" in " ".join(result.stderr.split())


def test_an_account_that_is_neither_is_refused(cfg_path: Path) -> None:
    result = _invoke(["--init", "--account", "team"])

    assert result.exit_code == 2
    assert "--account must be free or paid" in " ".join(result.stderr.split())


def test_a_real_process_with_the_keychain_switched_off_leaves_nothing(tmp_path: Path) -> None:
    """The CLI as its own process, as the app runs it, with GRAFT_NO_KEYCHAIN=1
    (set for the whole suite by conftest): the Keychain step fails, so no
    config.yaml and no copies are left, and the password is nowhere in what it
    printed."""
    ident = make_identity(tmp_path)
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }
    assert env.get("GRAFT_NO_KEYCHAIN") == "1"

    proc = subprocess.run(
        [sys.executable, "-m", "graft_ipa", *_paid_args(ident)],
        input=PASSWORD + "\n", capture_output=True, text=True, env=env, timeout=120,
    )

    assert proc.returncode == 1, proc.stderr
    assert "couldn't store the .p12 password in the Keychain" in " ".join(proc.stderr.split())
    cfg_dir = tmp_path / "cfg" / "graft"
    assert not (cfg_dir / "config.yaml").exists()
    assert not (cfg_dir / "AppleDevelopment.p12").exists()
    assert PASSWORD not in proc.stdout + proc.stderr


# --- --replace: a new certificate and profile for a paid account -------------------


def test_paid_on_a_paid_config_says_to_add_replace_and_changes_nothing(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    assert _invoke(_paid_args(make_identity(tmp_path / "old")), input=PASSWORD + "\n").exit_code == 0
    before, items = cfg_path.read_text(), dict(fake_keychain.items)

    again = _invoke(_paid_args(make_identity(tmp_path / "new")), input=PASSWORD + "\n")

    assert again.exit_code == 2, again.output
    assert ("already set up for a paid account; add --replace to swap in a new certificate "
            "and profile") in " ".join(again.stderr.split())
    assert "init-result" not in again.stdout
    assert (cfg_path.read_text(), fake_keychain.items) == (before, items)


def test_replace_swaps_the_identity_and_names_both_teams(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    assert _invoke(_paid_args(make_identity(tmp_path / "old")), input=PASSWORD + "\n").exit_code == 0
    new = make_identity(tmp_path / "new", team="FGHIJ67890", password="a new password")

    result = _invoke(_paid_args(new, "--replace"), input="a new password\n")

    assert result.exit_code == 0, result.output
    done = _events(result.stdout)[-1]
    assert (done["change"], done["team_id"], done["previous_team_id"]) == (
        "replaced", "FGHIJ67890", TEAM,
    )
    # The old team's item stays: another config, or Graft 1.x, may read it.
    assert fake_keychain.items == {
        ("graft", "p12-FGHIJ67890"): "a new password", ("graft", f"p12-{TEAM}"): PASSWORD,
    }
    assert any(f"p12-{TEAM}" in w for w in done["warnings"])
    assert (cfg_path.parent / "AppleDevelopment.p12").read_bytes() == new.p12.read_bytes()


@pytest.mark.parametrize(("args", "said"), [
    (["--init", "--replace"], "--replace with --init needs --account free or --account paid"),
    (["--list", "--replace"], "--replace is not valid with"),
])
def test_replace_is_refused_where_it_would_be_dropped(
    temp_config_env: Path, args: list[str], said: str
) -> None:
    result = _invoke(args)

    assert result.exit_code == 2, result.output
    assert said in " ".join(result.stderr.split())


# --- the app's side of init-result (2.0 review F11) ----------------------------------


def _swift_coding_keys() -> set[str]:
    """The JSON keys `InitResultRecord` decodes, read out of its CodingKeys."""
    import re

    source = (Path(__file__).parents[1] / "Graft/Graft/Models/AccountSetupRecords.swift").read_text()
    record = source[source.index("struct InitResultRecord"):]
    block = record[record.index("enum CodingKeys"):record.index("}", record.index("enum CodingKeys"))]
    keys: set[str] = set()
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("case "):
            continue
        for part in line[len("case "):].split(","):
            name, _, raw = part.partition("=")
            keys.add(raw.strip().strip('"') if raw else name.strip())
    assert re.fullmatch(r"[a-z_, ]+", ", ".join(sorted(keys))), keys
    return keys


def test_every_init_result_field_is_one_the_app_decodes_by_that_name(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    """A renamed key (K1: `profile_name`) decodes as nil in the app with no
    error, so the review screen says "Not named". The names, both ways."""
    result = _invoke(_paid_args(make_identity(tmp_path), "--dry-run"), input=PASSWORD + "\n")

    assert result.exit_code == 0, result.output
    done = _events(result.stdout)[-1]
    assert set(done) - {"type"} == _swift_coding_keys()
    assert (done["team_id"], done["profile_name"], done["certificate"]) == (
        TEAM, "Test Wildcard", COMMON_NAME,
    )
    assert done["devices"] == list(UDIDS) and done["device_count"] == len(UDIDS)
    assert isinstance(done["warnings"], list)


# --- the password comes from stdin, and only stdin (F7) -------------------------------


def test_there_is_no_password_flag(cfg_path: Path, tmp_path: Path) -> None:
    result = _invoke(_paid_args(make_identity(tmp_path), "--p12-password", PASSWORD))
    assert result.exit_code == 2
    assert "No such option" in result.output


def test_the_password_is_never_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    monkeypatch.setenv("GRAFT_P12_PASS", "from the environment")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli_mod._read_secret_line("Password: ") == ""
    monkeypatch.setattr(sys, "stdin", io.StringIO("from stdin\nand not this\n"))
    assert cli_mod._read_secret_line("Password: ") == "from stdin"


# --- a verb-only flag beside the scripted setup is refused, not dropped (F9) --------


@pytest.mark.parametrize("extra", [
    ["--force"], ["--default-device", "00008130-001A2B3C0123456D"],
    ["--signing-team-id", "ZZZZZ99999"], ["--app", "example"],
])
def test_a_verb_only_flag_beside_init_account_is_refused(cfg_path: Path, extra: list[str]) -> None:
    result = _invoke(["--json", "--init", "--account", "free", *extra])
    assert result.exit_code == 2, result.output
    assert f"{extra[0]} is not valid with --init --account" in " ".join(result.stderr.split())
    assert not cfg_path.exists()


# --- a Stop, the app quitting or a closed pipe mid-setup rolls it back ---------------


def _nothing_left(cfg_path: Path, fake: FakeSecurity) -> None:
    for name in ("config.yaml", "AppleDevelopment.p12", "wildcard.mobileprovision"):
        assert not (cfg_path.parent / name).exists(), name
    assert fake.items == {}


def test_sigterm_mid_create_through_the_cli_rolls_it_back(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the app's Stop, or its quitting, sends. `--init` turns it into
    `Stopped` (exit 143) and the undo runs; were the handler missing, the
    canary installed here would fail the test rather than kill the suite."""
    import signal

    from graft_ipa import account_setup

    real_store = account_setup._store_password

    def store_then_stop(team: str, password: str) -> None:
        real_store(team, password)
        os.kill(os.getpid(), signal.SIGTERM)

    def canary(signum, frame):
        raise AssertionError("--init installed no SIGTERM handler")

    monkeypatch.setattr(account_setup, "_store_password", store_then_stop)
    previous = signal.signal(signal.SIGTERM, canary)
    try:
        result = _invoke(_paid_args(make_identity(tmp_path)), input=PASSWORD + "\n")
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert result.exit_code == 143, (result.exit_code, result.output, result.exception)
    _nothing_left(cfg_path, fake_keychain)


def test_a_reader_that_hangs_up_mid_create_rolls_it_back(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    """The app quitting closes the pipe; SIGPIPE is ignored, so the next
    event is a BrokenPipeError, which the undo catches like any failure."""
    from graft_ipa.json_output import JsonEmitter

    class HungUp:
        def __init__(self) -> None:
            self.lines = 0

        def write(self, text: str) -> int:
            self.lines += text.count("\n")
            if self.lines > 3:  # profile, certificate, keychain, then the files step
                raise BrokenPipeError(32, "Broken pipe")
            return len(text)

        def flush(self) -> None:
            pass

    ident = make_identity(tmp_path)
    import io

    with pytest.raises(BrokenPipeError):
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(PASSWORD + "\n")
        try:
            cli_mod._do_init_account(
                cfg_path, account="paid", p12=str(ident.p12), profile=str(ident.profile),
                dry_run=False, emitter=JsonEmitter(stream=HungUp()),
            )
        finally:
            sys.stdin = old_stdin
    _nothing_left(cfg_path, fake_keychain)


# --- text mode: no escape sequences reach the terminal (F6, SEC-5) ------------------


def test_text_mode_prints_no_control_characters_from_the_files(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    ident = make_identity(tmp_path, common_name="Apple Development: X\x1bc\x1b]0;pwn\x07")

    result = _invoke(["--init", "--account", "paid", "--p12", str(ident.p12),
                      "--profile", str(ident.profile), "--dry-run"], input=PASSWORD + "\n")

    assert result.exit_code == 0, result.output
    assert "\x1b" not in result.output and "\x07" not in result.output
    assert "Apple Development: Xc]0;pwn" in result.output


def test_text_mode_prints_the_warnings(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    result = _invoke(["--init", "--account", "paid", "--p12", str(make_identity(tmp_path, listed=None).p12),
                      "--profile", str(make_identity(tmp_path, listed=None).profile), "--dry-run"],
                     input=PASSWORD + "\n")
    assert result.exit_code == 0, result.output
    assert "! The profile lists no certificates" in result.output


# --- --set-p12-password takes the same cap (SEC-1) ----------------------------------


def test_set_p12_password_refuses_one_over_the_cap(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    assert _invoke(_paid_args(make_identity(tmp_path)), input=PASSWORD + "\n").exit_code == 0
    fake_keychain.calls.clear()

    result = _invoke(["--set-p12-password"], input="x" * 1025 + "\n")

    assert result.exit_code != 0
    assert "a .p12 password longer than 1024 bytes isn't supported" in " ".join(
        result.output.split())
    assert not [c for c in fake_keychain.calls if c[1:] == ["-i"]]


def test_text_mode_strips_control_characters_from_any_step(
    cfg_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI's own floor, under `read_identity`'s: whatever a step says
    reaches the terminal without an escape sequence."""
    from graft_ipa import account_setup

    def setup(cfg, *, on_step, **kw):
        on_step("profile", "a name with \x1b]0;pwn\x07 and \x1bc in it")
        return account_setup.InitResult(account="free", config=cfg, change="unchanged",
                                        dry_run=False, warnings=("warned \x1b[2J here",))

    monkeypatch.setattr(account_setup, "setup_account", setup)
    result = _invoke(["--init", "--account", "free"])

    assert result.exit_code == 0, result.output
    assert "\x1b" not in result.output and "\x07" not in result.output
    assert "a name with ]0;pwn and c in it" in result.output


# --- --set-p12-password and a blank-password .p12 (combined fix check #17) -----------


def test_set_p12_password_stores_an_empty_one_for_a_p12_that_opens_with_it(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    """Keychain Access can export a .p12 with a blank password: setup stores
    "", and a user whose item is gone has to be able to store it again."""
    from .signing_fixtures import openssl_p12
    from .test_account_setup import LIBRESSL

    if LIBRESSL is None:
        pytest.skip("needs /usr/bin/openssl")
    ident = make_identity(tmp_path, password="", p12_maker=openssl_p12(LIBRESSL))
    assert _invoke(_paid_args(ident), input="\n").exit_code == 0
    fake_keychain.items.clear()

    result = _invoke(["--set-p12-password"], input="\n")

    assert result.exit_code == 0, result.output
    assert fake_keychain.items == {("graft", f"p12-{TEAM}"): ""}


def test_set_p12_password_still_refuses_an_empty_one_for_a_p12_that_needs_one(
    cfg_path: Path, tmp_path: Path, fake_keychain: FakeSecurity
) -> None:
    assert _invoke(_paid_args(make_identity(tmp_path)), input=PASSWORD + "\n").exit_code == 0
    fake_keychain.items.clear()

    result = _invoke(["--set-p12-password"], input="\n")

    assert result.exit_code != 0
    words = " ".join(result.output.split())
    assert "no password provided" in words and "doesn't open with an empty password" in words
    assert fake_keychain.items == {}


# --- cli-4: a Stop that cut short an undo says what wasn't put back -------------------


@pytest.mark.parametrize("json_mode", [True, False])
def test_a_stop_carrying_an_undo_report_says_it_then_stops(tmp_path, monkeypatch, capsys, json_mode) -> None:
    import io
    import signal

    from graft_ipa import account_setup
    from graft_ipa.errors import GraftError, Stopped
    from graft_ipa.json_output import JsonEmitter

    stop = Stopped(signal.SIGTERM)
    stop.undo_incomplete = GraftError(
        "the setup failed (Stopped), and the Keychain item p12-ABCDE12345 couldn't be put back",
        hint="put those back by hand, or run the setup again",
    )

    def stopped(*a, **k):
        raise stop

    monkeypatch.setattr(account_setup, "setup_account", stopped)
    stream = io.StringIO()
    with pytest.raises(Stopped):
        cli_mod._do_init_account(tmp_path / "config.yaml", account="paid", p12=None, profile=None,
                                 dry_run=False, emitter=JsonEmitter(stream=stream) if json_mode else None)
    said = " ".join(capsys.readouterr().err.split())
    assert "p12-ABCDE12345 couldn't be put back" in said and "put those back by hand" in said
    if json_mode:
        (log,) = [json.loads(line) for line in stream.getvalue().splitlines()]
        assert log["type"] == "log" and log["source"] == "warning"
        assert "couldn't be put back" in log["line"] and "put those back by hand" in log["line"]
