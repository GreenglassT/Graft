"""The sign-in's edges the 2.0 review's part 4 found untested or wrong.

Each section names its finding. Always the fake ipatool (`appstore_fakes`),
never the real one, and the suite's fake `security`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from graft_ipa import ipatool
from tests.appstore_fakes import FakeTools


@pytest.fixture
def fakes(tmp_path: Path) -> FakeTools:
    return FakeTools(tmp_path / "tools")


# --- #10: a silent re-sign-in that Apple wants a code for ----------------------

#: ipatool 2.8.0's `ErrAuthCodeRequired`, returned unwrapped by the
#: `store.Login` a purchase or a download runs when the session expired.
AUTH_CODE = "auth code is required"


def test_a_purchase_whose_renewal_needs_a_code_says_sign_in_again(fakes: FakeTools) -> None:
    fakes.set(purchase_error=AUTH_CODE)
    with pytest.raises(ipatool.AppStoreError) as e:
        ipatool.purchase(fakes.ipatool, "com.example.app")
    assert "signing in again" in e.value.message and "2FA code" in e.value.message
    assert e.value.hint == ipatool.RENEW_HINT
    # Still "signed in" by status, so Settings shows "Sign In Again…" beside
    # Sign Out (the fix re-check's problem 13), and Terminal keeps its command.
    assert "Settings › App Store › Sign In Again…" in e.value.hint
    assert "graft --appstore-login" in e.value.hint


def test_a_download_whose_renewal_needs_a_code_says_sign_in_again(fakes: FakeTools, tmp_path: Path) -> None:
    fakes.set(download_error=AUTH_CODE)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(ipatool.AppStoreError) as e:
        ipatool.download(fakes.ipatool, "com.example.app", out)
    assert "2FA code" in e.value.message
    assert e.value.hint == ipatool.RENEW_HINT
    assert "Settings › App Store › Sign In Again…" in e.value.hint
    assert "Terminal may say more" not in e.value.hint


# --- #35: classify matches words, and the network wins ---------------------------

from graft_ipa import appstore_login  # noqa: E402

#: ipatool 2.8.0's network failure, wrapping a Go cause it doesn't control.
NETWORK_WRAP = "sign-in request failed: authentication request failed after 3 attempts ({}): {}"


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        # The review's string: "blocked" holds "locked".
        ('authentication request failed after 3 attempts (Post "https://buy.itunes.apple.com/'
         'auth"): blocked by network policy', "network"),
        (NETWORK_WRAP.format("transport error, transport error, transport error",
                             "proxy says: tunnel disabled"), "network"),
        (NETWORK_WRAP.format("transport error", "unlocked session dropped"), "network"),
        # A whole word in the cause: the network still wins.
        (NETWORK_WRAP.format("transport error", "proxy: port locked by administrator"), "network"),
        # A rate limit after three tries is still a rate limit.
        (NETWORK_WRAP.format("HTTP 429, 429, 429", "apple rate limited authentication; try again later"),
         "rate-limited"),
        # Apple's own words for a locked or disabled Apple ID still are.
        ("This Apple ID has been locked for security reasons.", "locked"),
        ("account is disabled", "locked"),
        ("Your account is disabled.", "locked"),
        ("This Apple ID has been disabled for security reasons.", "locked"),
        # Neither word inside another, nor "disabled" about anything else.
        ("the request was blocked", "other"),
        ("sign-in failed: the lookup service is disabled", "other"),
        ("failed to save account in keychain: the keychain is locked", "keychain"),
    ],
)
def test_locked_and_disabled_are_words_and_the_network_wins(error: str, reason: str) -> None:
    assert appstore_login.classify(error).reason == reason


# --- #22: a Stop anywhere leaves no secret unwiped -------------------------------

import os  # noqa: E402
import signal  # noqa: E402
import sys  # noqa: E402

from graft_ipa import lc_manage, secret_input  # noqa: E402
from graft_ipa.errors import Stopped  # noqa: E402

SECRET = b"hunter2-pw"


@pytest.fixture
def made(monkeypatch: pytest.MonkeyPatch) -> list[secret_input.SecretLine]:
    """Every SecretLine made from here on, to look at after the run."""
    lines: list[secret_input.SecretLine] = []
    real = secret_input.SecretLine.__init__

    def init(self, capacity: int = secret_input.MAX_LINE) -> None:
        real(self, capacity)
        lines.append(self)

    monkeypatch.setattr(secret_input.SecretLine, "__init__", init)
    return lines


def _stop_after_event(k: int, frames: set[str]):
    """A tracer that sends this process SIGTERM after the `k`th line or
    return event in the named functions. The handler (`exit_on_termination`)
    then raises `Stopped` at the next point Python checks for signals, as a
    real Stop from the app does: asynchronously, wherever that lands."""
    seen = [0]

    def local(frame, event, _arg):
        if event in ("line", "return"):
            seen[0] += 1
            if seen[0] == k:
                signal.raise_signal(signal.SIGTERM)
        return local

    def tracer(frame, event, _arg):
        return local if event == "call" and frame.f_code.co_name in frames else None

    return tracer, seen


def test_a_stop_after_any_step_of_reading_a_line_leaves_nothing_unwiped(made) -> None:
    """The review's race probe, made exhaustive and deterministic: a Stop
    after every step of `prompt_line` and `read_line`, including after
    `return line` (in its `finally`, and between it and the caller's
    assignment). The Stop handler's `wipe_all` must reach every line."""
    k, stops = 1, 0
    while True:
        r, w = os.pipe()
        os.write(w, SECRET + b"\n")
        os.close(w)
        tracer, seen = _stop_after_event(k, {"prompt_line", "read_line"})
        line = None
        try:
            with lc_manage.exit_on_termination():
                sys.settrace(tracer)
                try:
                    line = secret_input.prompt_line(r, "password: ")
                finally:
                    sys.settrace(None)
                # A Stop pending past the call lands here, before `line` is used.
                for _ in range(3):
                    pass
        except Stopped:
            stops += 1
            secret_input.wipe_all()  # what the sign-in's Stop handler does
        finally:
            os.close(r)
        if line is not None:
            line.wipe()
        if seen[0] < k:
            break  # every event had its Stop
        k += 1
    assert stops >= 10, "premise: Stops landed all through the read"
    assert made, "premise: lines were made"
    unwiped = [m for m in made if any(m._buf)]
    assert unwiped == []


def test_a_stop_between_a_read_and_its_caller_is_wiped_by_the_sign_in(made, tmp_path: Path) -> None:
    """run()'s Stop handler wipes what no `finally` holds yet: a line the
    reader made and never handed back."""
    caught: list[secret_input.SecretLine] = []

    def reader(prompt: str, timeout: float | None, echo: bool = False):
        line = secret_input.SecretLine()
        line._buf[: len(SECRET)] = SECRET
        line._len = len(SECRET)
        caught.append(line)
        raise Stopped(signal.SIGTERM)  # landed after the read, before the return

    fakes = FakeTools(tmp_path / "tools")
    cfg = _cfg_for(fakes, tmp_path)
    with pytest.raises(Stopped):
        appstore_login.run(cfg, emitter=None, say=lambda _: None, read_line=reader)
    assert caught and not any(caught[0]._buf)


def test_a_line_alive_at_exit_is_wiped(tmp_path: Path) -> None:
    """The way out that isn't the Stop handler: the interpreter exiting. A
    check registered before `secret_input` is imported runs after its hook
    (atexit runs last-registered first), and sees the line zeroed."""
    import subprocess

    from tests.conftest import NO_USBMUXD, REPO_ROOT

    script = (
        "import atexit, sys\n"
        "held = []\n"
        "atexit.register(lambda: print('wiped' if held and not any(held[0]._buf) else 'NOT WIPED'))\n"
        "from graft_ipa import secret_input\n"
        "line = secret_input.SecretLine()\n"
        "line._buf[:10] = b'hunter2-pw'; line._len = 10\n"
        "held.append(line)\n"
        "sys.exit(143)\n"
    )
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GRAFT_NO_KEYCHAIN": "1",
           "USBMUXD_SOCKET_ADDRESS": NO_USBMUXD, "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.run([sys.executable, "-c", script], env=env, cwd=tmp_path,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 143
    assert proc.stdout.strip() == "wiped", proc.stderr


def _cfg_for(fakes: FakeTools, tmp_path: Path):
    import yaml

    from graft_ipa.config import load_config
    from tests.config_fixtures import write_test_config

    config = tmp_path / "config.yaml"
    write_test_config(config, team_id="TESTTEAM01")
    raw = yaml.safe_load(config.read_text())
    raw["appstore"] = fakes.appstore_section()
    config.write_text(yaml.safe_dump(raw, sort_keys=False))
    return load_config(config)


# --- #36: a second code prompt ---------------------------------------------------

from graft_ipa.appstore_login import LoginFailed  # noqa: E402
from tests.test_appstore_login import Codes, _sign_in  # noqa: E402


def test_a_second_code_prompt_wipes_the_first_code_and_fails_fast(fakes: FakeTools) -> None:
    """ipatool 2.8.0 asks once; a build that asks again gets no second code,
    and the first is wiped (the review's #36)."""
    fakes.set(login_two_factor=True, login_code_twice=True)
    handed: list[secret_input.SecretLine] = []

    def codes(timeout: float) -> secret_input.SecretLine:
        handed.append(Codes()(timeout))
        return handed[-1]

    with pytest.raises(LoginFailed) as e:
        _sign_in(fakes, codes=codes)
    assert "2FA code twice" in e.value.message
    assert len(handed) == 1, "never asked for a second code"
    assert not any(handed[0]._buf)


# --- #18: a sign-in Graft couldn't record, and a Stop after ipatool saved ---------

import io  # noqa: E402
import json  # noqa: E402
import stat  # noqa: E402

from graft_ipa import appstore_account, data_dirs, keychain, tools  # noqa: E402
from graft_ipa.json_output import JsonEmitter  # noqa: E402
from tests.keychain_fakes import FakeSecurity  # noqa: E402
from tests.test_appstore_login import EMAIL, PASSWORD, Stdin, secret  # noqa: E402


def _login(cfg, stdin: Stdin):
    stream = io.StringIO()
    try:
        outcome: object = appstore_login.run(
            cfg, emitter=JsonEmitter(stream=stream), say=lambda _: None, read_line=stdin,
        )
    except BaseException as e:  # Stopped is a SystemExit
        outcome = e
    return outcome, [json.loads(line) for line in stream.getvalue().splitlines()]


@pytest.fixture
def bundled_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fakes: FakeTools):
    """Graft.app's own ipatool, and a fake login keychain it shares."""
    store = FakeSecurity(store=tmp_path / "keychain.json")
    monkeypatch.setattr(keychain, "_run", store)
    fakes.set(keychain=str(store.store), login_two_factor=False)
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: fakes.root)
    _cfg_for(fakes, tmp_path)
    return _bundled(tmp_path)


def _bundled(tmp_path: Path):
    """The same config with no appstore: section, so the bundled copy runs."""
    import yaml

    from graft_ipa.config import load_config

    config = tmp_path / "config.yaml"
    raw = yaml.safe_load(config.read_text())
    raw.pop("appstore", None)
    config.write_text(yaml.safe_dump(raw, sort_keys=False))
    return load_config(config)


def test_a_marker_that_cant_be_written_is_a_sign_in_graft_couldnt_record(bundled_cfg) -> None:
    """The review's probe: the state dir read-only (or a full disk). ipatool
    did sign in; Graft says so, and what to do, never a raw OSError."""
    state = data_dirs.state_dir()
    state.mkdir(parents=True, exist_ok=True)
    state.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        outcome, events = _login(bundled_cfg, Stdin(EMAIL, PASSWORD))
    finally:
        state.chmod(stat.S_IRWXU)
    assert isinstance(outcome, appstore_login.LoginFailed), outcome
    assert outcome.reason == appstore_login.REASON_NOT_RECORDED == "not-recorded"
    assert events[-1]["state"] == "failed" and events[-1]["reason"] == "not-recorded"
    message = events[-1]["message"]
    assert message.startswith("You're signed in, but Graft couldn't record")
    assert "Permission denied" in message
    assert "free some disk space" in outcome.hint and "sign in again" in outcome.hint


def test_a_keychain_that_wont_say_after_the_sign_in_is_not_recorded_either(
    bundled_cfg, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_answer() -> dict:
        raise appstore_account.KeychainUnanswered("account")

    monkeypatch.setattr(appstore_account, "_record_items", no_answer)
    outcome, events = _login(bundled_cfg, Stdin(EMAIL, PASSWORD))
    assert isinstance(outcome, appstore_login.LoginFailed)
    assert events[-1]["reason"] == "not-recorded"
    assert "keychain didn't answer" in events[-1]["message"]


def test_a_stop_after_ipatool_signed_in_doesnt_say_nothing_was_saved(
    bundled_cfg, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Stop landing between ipatool's success and Graft's record. ipatool
    did save it, so the message says so, and the record is still made."""
    def stopped(source: str) -> None:
        raise Stopped(signal.SIGTERM)

    monkeypatch.setattr(appstore_login, "_record", stopped)
    outcome, events = _login(bundled_cfg, Stdin(EMAIL, PASSWORD))
    assert isinstance(outcome, Stopped)
    assert events[-1]["reason"] == "stopped"
    assert events[-1]["message"] == appstore_login.STOPPED_AFTER_SIGN_IN
    assert "Nothing was saved" not in events[-1]["message"]
    assert set(appstore_account.vouched()) == {"account", "anisette-v4"}, "recorded on the way out"


@pytest.mark.parametrize(
    ("where", "message"),
    [
        ("at the code prompt", appstore_login.MESSAGES[appstore_login.REASON_STOPPED]),
        ("after the code", appstore_login.STOPPED_AFTER_PASSWORD),
    ],
)
def test_a_stop_before_any_verdict_says_only_what_may_be_true(
    bundled_cfg, fakes: FakeTools, where: str, message: str,
) -> None:
    """While ipatool waits for the code, Apple has accepted nothing and
    nothing is saved. Once the code is in, ipatool may save at any moment."""
    fakes.set(login_two_factor=True, login_answer_delay=30)
    stop_now = where == "at the code prompt"

    class StopIn(Stdin):
        def __call__(self, prompt, timeout, echo=False):
            if prompt == appstore_login.CODE_ASK and stop_now:
                raise Stopped(signal.SIGTERM)
            return super().__call__(prompt, timeout, echo)

    stdin = StopIn(EMAIL, PASSWORD, b"482913")
    if not stop_now:
        real = appstore_login._report

        def report(emitter, say, state, **fields):
            real(emitter, say, state, **fields)
            if state == appstore_login.STATE_VERIFYING:
                raise Stopped(signal.SIGTERM)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(appstore_login, "_report", report)
            outcome, events = _login(bundled_cfg, stdin)
    else:
        outcome, events = _login(bundled_cfg, stdin)
    assert isinstance(outcome, Stopped)
    assert events[-1]["message"] == message


# --- #19: ipatool gone by the time the code is typed ------------------------------


def _late_code(delay: float):
    def codes(timeout: float):
        import time

        time.sleep(delay)
        return secret(b"482913")

    return codes


def test_a_code_typed_after_ipatool_died_is_a_sign_in_failure_not_an_oserror(fakes: FakeTools) -> None:
    fakes.set(login_two_factor=True, login_code_then_exit=True, login_exit_after=0.2)
    with pytest.raises(LoginFailed) as e:
        _sign_in(fakes, codes=_late_code(1.5))
    assert e.value.reason == appstore_login.REASON_IPATOOL_EXITED == "ipatool-exited"
    assert "the 2FA code" in e.value.message


def test_what_a_dying_ipatool_said_is_still_read(fakes: FakeTools) -> None:
    fakes.set(login_two_factor=True, login_code_then_exit=True, login_exit_after=0.2,
              login_dying_words="sign-in request failed: account is disabled")
    with pytest.raises(LoginFailed) as e:
        _sign_in(fakes, codes=_late_code(1.5))
    assert e.value.reason == "locked"


def test_the_verb_reports_ipatool_exiting_as_a_failed_event(fakes: FakeTools, tmp_path: Path) -> None:
    fakes.set(login_two_factor=True, login_code_then_exit=True, login_exit_after=0.2)
    cfg = _cfg_for(fakes, tmp_path)

    class Late(Stdin):
        def __call__(self, prompt, timeout, echo=False):
            if prompt == appstore_login.CODE_ASK:
                return _late_code(1.5)(timeout)
            return super().__call__(prompt, timeout, echo)

    outcome, events = _login(cfg, Late(EMAIL, PASSWORD))
    assert isinstance(outcome, LoginFailed)
    assert events[-1] == {"type": "appstore-login", "state": "failed", "reason": "ipatool-exited",
                          "message": outcome.message, "hint": None}


# --- The critic: no sign-in through a known-bad ipatool ---------------------------


def test_a_known_bad_ipatool_gets_no_password(fakes: FakeTools, tmp_path: Path) -> None:
    """As `--appstore-claim` refuses it: before any secret is even read, so
    the Apple ID and the password never leave stdin's buffer."""
    fakes.set(ipatool_version="2.8.1")
    stdin = Stdin(EMAIL, PASSWORD)
    outcome, events = _login(_cfg_for(fakes, tmp_path), stdin)
    assert isinstance(outcome, LoginFailed)
    assert outcome.reason == appstore_login.REASON_BAD_VERSION == "bad-version"
    assert events == [{"type": "appstore-login", "state": "failed", "reason": "bad-version",
                       "message": outcome.message, "hint": outcome.hint}]
    assert "2.8.1" in outcome.message and "majd/ipatool#672" in outcome.hint
    assert stdin.asked == [], "nothing read"
    assert fakes.login_runs() == ([], [])


# --- The critic and #24: what counts as a sign of life -----------------------------


def test_output_on_stdout_restarts_the_silence_limit(fakes: FakeTools) -> None:
    """H1: ipatool logging past the limit is alive (a deadline fixed at the
    start would stop it)."""
    fakes.set(login_two_factor=False, login_chatter={"every": 0.4, "for": 4.5})
    email, _ = _sign_in(fakes, silence_limit=2.0, heartbeat=0.2)
    assert email


def test_typing_the_code_restarts_the_silence_limit(fakes: FakeTools) -> None:
    """H2: minutes over the code don't come off ipatool's time to answer."""
    fakes.set(login_two_factor=True, login_answer_delay=2.0)
    email, _ = _sign_in(fakes, codes=_late_code(2.5), silence_limit=4.0, heartbeat=0.2)
    assert email


@pytest.mark.parametrize("where", ["cache", "tmp"])
def test_a_download_in_progress_is_alive_while_ipatool_says_nothing(
    fakes: FakeTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str,
) -> None:
    """The first sign-in downloads ipatool's components silently. Growth of
    its download folders counts: Unicorn in ~/Library/Caches/ipatool, the
    macOS assets in a $TMPDIR/ipatool-* workspace (ipatool 2.8.0's source)."""
    tmp = tmp_path / "T"
    tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp))
    fakes.set(login_two_factor=False, login_grow={"where": where, "every": 0.3, "for": 4.5})
    email, events = _sign_in(fakes, silence_limit=2.0, heartbeat=0.2)
    assert email
    assert any(e["state"] == "waiting" for e in events), "premise: it was silent throughout"


def test_silence_with_no_download_is_still_a_timeout(fakes: FakeTools) -> None:
    fakes.set(login_two_factor=False, login_silent=60)
    with pytest.raises(LoginFailed) as e:
        _sign_in(fakes, silence_limit=2.0, heartbeat=0.2)
    assert e.value.reason == "timeout"


def test_the_download_mark_sees_growth_and_ignores_other_folders(tmp_path: Path, monkeypatch) -> None:
    tmp = tmp_path / "T"
    (tmp / "unrelated").mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(tmp))
    before = appstore_login.download_mark()
    (tmp / "unrelated" / "big").write_bytes(b"x" * 100)
    assert appstore_login.download_mark() == before
    work = tmp / "ipatool-macos-27-123" / "frameworks"
    work.mkdir(parents=True)
    (work / "CoreFP").write_bytes(b"x")
    grown = appstore_login.download_mark()
    assert grown != before
    with open(work / "CoreFP", "ab") as f:
        f.write(b"y")
    assert appstore_login.download_mark() != grown


# --- #25: the 1.x items go before ipatool starts, in one ordered log ---------------


def test_the_old_items_are_deleted_before_ipatool_starts(
    fakes: FakeTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[tuple[str, list[str]]] = []
    store = FakeSecurity({("ipatool-auth.service", k): "old" for k in ("account", "anisette-v4", "kbsync-v1")})

    def security(argv, **kw):
        order.append(("security", list(argv)))
        return store(argv, **kw)

    real_run_ipatool = ipatool.run_ipatool

    def run_ipatool(path, args, **kw):
        order.append(("ipatool", list(args)))
        return real_run_ipatool(path, args, **kw)

    monkeypatch.setattr(keychain, "_run", security)
    monkeypatch.setattr(ipatool, "run_ipatool", run_ipatool)
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: fakes.root)
    fakes.set(login_two_factor=False)
    _cfg_for(fakes, tmp_path)
    outcome, _ = _login(_bundled(tmp_path), Stdin(EMAIL, PASSWORD))
    assert outcome == "fake-apple-id@example.invalid"
    login = next(i for i, (who, argv) in enumerate(order) if who == "ipatool" and argv[:2] == ["auth", "login"])
    deletes = [i for i, (who, argv) in enumerate(order) if who == "security" and argv[1] == "delete-generic-password"]
    assert len(deletes) == 3
    assert max(deletes) < login, order


# --- #26: the exact names, written out, not read from IPATOOL_ITEMS ----------------


def test_the_three_items_are_ipatools_exact_names(
    fakes: FakeTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision 2's pin: ipatool 2.8.0's own spellings (appstore_login.go,
    appstore_native_login.go, appstore_kbsync_cache.go), and nothing else."""
    store = FakeSecurity({
        ("ipatool-auth.service", "account"): "old",
        ("ipatool-auth.service", "anisette-v4"): "old",
        ("ipatool-auth.service", "kbsync-v1"): "old",
        ("ipatool-auth.service", "kbsync"): "not ipatool 2.8.0's",
        ("graft", "p12-ABCDE12345"): "Graft's own",
    })
    monkeypatch.setattr(keychain, "_run", store)
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: fakes.root)
    fakes.set(login_two_factor=False)
    _cfg_for(fakes, tmp_path)
    _login(_bundled(tmp_path), Stdin(EMAIL, PASSWORD))
    assert [c for c in store.calls if c[1] == "delete-generic-password"] == [
        ["security", "delete-generic-password", "-s", "ipatool-auth.service", "-a", "account"],
        ["security", "delete-generic-password", "-s", "ipatool-auth.service", "-a", "anisette-v4"],
        ["security", "delete-generic-password", "-s", "ipatool-auth.service", "-a", "kbsync-v1"],
    ]
    assert ("ipatool-auth.service", "kbsync") in store.items
    assert ("graft", "p12-ABCDE12345") in store.items


# --- #32: the pty's edge cases ------------------------------------------------------


def test_a_second_password_prompt_is_never_answered(fakes: FakeTools) -> None:
    """I4: the password is typed once. A build that asks again ends the run."""
    import time

    fakes.set(login_two_factor=False, login_password_twice=True)
    began = time.monotonic()
    with pytest.raises(LoginFailed) as e:
        _sign_in(fakes, silence_limit=10.0)
    assert "password twice" in e.value.message
    assert time.monotonic() - began < 8, "failed fast, not at the silence limit"
    started, finished = fakes.login_runs()
    assert started and finished == [], "stopped while it waited for a second password"


def test_a_keyring_passphrase_prompt_ends_the_run(fakes: FakeTools) -> None:
    """I5: macOS never uses ipatool's file keyring; its prompt isn't left to hang."""
    import time

    fakes.set(login_two_factor=False, login_passphrase=True)
    began = time.monotonic()
    with pytest.raises(LoginFailed) as e:
        _sign_in(fakes, silence_limit=10.0)
    assert "keyring passphrase" in e.value.message
    assert time.monotonic() - began < 8


def test_the_terminal_never_echoes_the_apple_id_or_the_code(fakes: FakeTools) -> None:
    """I2: ECHO is off on the pty before ipatool reads the Apple ID or the
    code, both canonical-mode line reads it leaves to the terminal's echo."""
    fakes.set(login_two_factor=True)
    _sign_in(fakes)
    _, (call,) = fakes.login_runs()
    assert call["echo"] == [False, False]


# --- The fix re-check's problem 7: a prompt that isn't 2.8.0's -------------------


def test_an_ipatool_whose_prompt_isn_t_2_8_0_s_fails_fast(
    fakes: FakeTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 2.9.0 whose Apple ID prompt reads "Apple ID: " used to get nothing
    typed and sit out the 20-minute silence limit as a "timeout" that blamed
    the connection. Output that ends like a prompt, matching none of
    2.8.0's, then silence, ends the run as `unknown-prompt`."""
    import time

    import functools

    monkeypatch.setattr(appstore_login, "UNKNOWN_PROMPT_SECONDS", 1.0)
    # The test's own bound, whatever the code under test does: a 6 s silence
    # limit, so without the fix this ends as `timeout` in 6 s, not 20 minutes.
    monkeypatch.setattr(appstore_login, "sign_in", functools.partial(appstore_login.sign_in, silence_limit=6.0))
    fakes.set(ipatool_version="2.9.0", id_prompt="Apple ID: ")
    began = time.monotonic()

    outcome, events = _login(_cfg_for(fakes, tmp_path), Stdin(EMAIL, PASSWORD))

    assert time.monotonic() - began < 5, "not the silence limit"
    assert isinstance(outcome, LoginFailed)
    assert outcome.reason == appstore_login.REASON_UNKNOWN_PROMPT == "unknown-prompt"
    assert events[-1] == {"type": "appstore-login", "state": "failed", "reason": "unknown-prompt",
                          "message": outcome.message, "hint": outcome.hint}
    assert outcome.hint and "ipatool 2.8.0" in outcome.hint
    assert str(fakes.ipatool) in outcome.message and "2.9.0" in outcome.message
    assert "'Apple ID: '" in outcome.message
    assert "Graft is tested with ipatool 2.8.0" in outcome.message
    started, finished = fakes.login_runs()
    assert len(started) == 1 and finished == [], "nothing typed: it was stopped at its prompt"


def test_the_unknown_prompt_wait_is_short_but_longer_than_a_prompt_takes_to_arrive() -> None:
    assert appstore_login.UNKNOWN_PROMPT_SECONDS == 15.0
    assert appstore_login.UNKNOWN_PROMPT_SECONDS < appstore_login.SILENCE_LIMIT_SECONDS


@pytest.mark.parametrize(
    ("tail", "asked"),
    [
        (b"Apple ID: ", True),
        (b"note\nPassword: ", True),
        (b"Continue? ", True),
        (b"enter password: ****\r\n", False),
        (b"preparing: done\n", False),
        (b"enter Apple ID (email", False),
        (b"", False),
    ],
)
def test_what_looks_like_a_prompt(tail: bytes, asked: bool) -> None:
    assert (appstore_login._asked(bytearray(tail)) is not None) is asked


def test_2_8_0_s_own_prompts_never_look_unknown(fakes: FakeTools, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every known prompt is answered as it arrives, so even a tiny wait
    never fires on a normal sign-in, a code and a slow first one included."""
    monkeypatch.setattr(appstore_login, "UNKNOWN_PROMPT_SECONDS", 0.05)
    fakes.set(login_two_factor=True, login_silent=1)
    email, events = _sign_in(fakes, heartbeat=0.2)
    assert email and [e["state"] for e in events][-1] != "failed"


def test_a_prompt_like_line_followed_by_more_is_not_a_prompt_left_waiting(
    fakes: FakeTools, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any output starts the wait again: "note: ", then 2.8.0's own prompt
    half a second later, is answered, with a 1 s wait."""
    monkeypatch.setattr(appstore_login, "UNKNOWN_PROMPT_SECONDS", 1.0)
    fakes.set(login_two_factor=False, login_preamble="note: ")
    email, events = _sign_in(fakes, silence_limit=6.0)
    assert email and events[-1]["state"] != "failed"


# --- --expect-ipatool: the ipatool the sheet named is the one that signs in ------


def test_an_ipatool_other_than_the_one_expected_gets_nothing(fakes: FakeTools, tmp_path: Path) -> None:
    """Settings named one ipatool, and by the time the sign-in runs config.yaml
    names another (the fix re-check's problem 10): refused before stdin is
    read or any ipatool is started, `--version` included."""
    expected = tmp_path / "the-one-the-sheet-named" / "ipatool"
    stdin = Stdin(EMAIL, PASSWORD)
    stream = io.StringIO()

    with pytest.raises(LoginFailed) as e:
        appstore_login.run(_cfg_for(fakes, tmp_path), emitter=JsonEmitter(stream=stream),
                           say=lambda _: None, read_line=stdin, expect_ipatool=expected)

    assert e.value.reason == appstore_login.REASON_IPATOOL_CHANGED == "ipatool-changed"
    assert str(fakes.ipatool) in e.value.message and str(expected) in e.value.message
    assert [json.loads(line) for line in stream.getvalue().splitlines()] == [
        {"type": "appstore-login", "state": "failed", "reason": "ipatool-changed", "message": e.value.message,
         "hint": e.value.hint}
    ]
    assert stdin.asked == [], "nothing read"
    assert fakes.calls("ipatool") == [], "no ipatool started, not even for its version"


def test_the_expected_ipatool_signs_in_even_by_another_path_to_it(fakes: FakeTools, tmp_path: Path) -> None:
    fakes.set(login_two_factor=False)
    link = tmp_path / "link-to-ipatool"
    link.symlink_to(fakes.ipatool)
    outcome, events = _login_expecting(_cfg_for(fakes, tmp_path), Stdin(EMAIL, PASSWORD), link)
    assert events[-1]["state"] == "signed-in", events


def _login_expecting(cfg, stdin: Stdin, expected: Path):
    stream = io.StringIO()
    try:
        outcome: object = appstore_login.run(cfg, emitter=JsonEmitter(stream=stream), say=lambda _: None,
                                             read_line=stdin, expect_ipatool=expected)
    except BaseException as e:
        outcome = e
    return outcome, [json.loads(line) for line in stream.getvalue().splitlines()]


# --- Delta audit #6: the unknown prompt's text is redacted before it is cut ------


@pytest.mark.parametrize("cut_into", [5, 20, 40, 55])
def test_an_apple_id_the_80_character_cut_falls_inside_never_survives_in_part(cut_into: int) -> None:
    """The message keeps the prompt's last 80 printable characters, and was
    cut before the Apple ID was taken out: with the cut inside the Apple
    ID, the rest of it no longer matched and went out as is."""
    from graft_ipa.secret_input import SecretLine

    apple_id = "averyveryverylongname.with.dots@example-company-domain.com"
    # After the Apple ID, 80 - (its length - cut_into) characters, so the
    # last 80 begin `cut_into` characters into it.
    tail = " (leave empty" + "." * (80 - len(apple_id) + cut_into - 27) + " to skip it): "
    asked = f"please re-enter the app-specific password for {apple_id}{tail}"
    assert asked[-80:] == apple_id[cut_into:] + tail
    line = SecretLine()
    line._buf[: len(apple_id)] = apple_id.encode()
    line._len = len(apple_id)
    failed = appstore_login._unknown_prompt(Path("/opt/ipatool"), "2.9.0", asked.encode(), line, None, None)
    said = failed.message.split(" asked ", 1)[1]
    assert apple_id[cut_into:] not in said, said
    assert not [apple_id[i:i + 6] for i in range(len(apple_id) - 5) if apple_id[i:i + 6] in said], said
    assert "to skip it): '" in said


def test_the_unknown_prompt_still_says_what_was_asked_around_the_apple_id() -> None:
    from graft_ipa.secret_input import SecretLine

    apple_id = b"someone@example.com"
    line = SecretLine()
    line._buf[: len(apple_id)] = apple_id
    line._len = len(apple_id)
    failed = appstore_login._unknown_prompt(Path("/opt/ipatool"), "2.9.0",
                                            b"Password for someone@example.com: ", line)
    assert "asked 'Password for [redacted]: '" in failed.message


# --- The whole-diff review's signin-3 and signin-4 ---------------------------------

from graft_ipa.secret_input import SecretTooLong  # noqa: E402


def _too_long(timeout: float):
    raise SecretTooLong("a line longer than 1024 bytes")


def test_a_code_line_too_long_is_a_failure_not_a_crash(fakes: FakeTools) -> None:
    """signin-3: more than 1024 bytes at the code prompt (piped stdin) was
    a SecretTooLong out of `sign_in`, past `run`'s failure event."""
    fakes.set(login_two_factor=True)
    with pytest.raises(LoginFailed) as e:
        _sign_in(fakes, codes=_too_long)
    assert e.value.reason == appstore_login.REASON_WRONG_CODE
    assert e.value.message == appstore_login.MESSAGES[appstore_login.REASON_WRONG_CODE]
    started, finished = fakes.login_runs()
    assert started and not finished, "ipatool was stopped before it got a code"


def test_a_code_line_too_long_ends_with_a_failed_event(fakes: FakeTools, tmp_path: Path) -> None:
    fakes.set(login_two_factor=True)

    class TooLong(Stdin):
        def __call__(self, prompt, timeout, echo=False):
            if prompt == appstore_login.CODE_ASK:
                raise SecretTooLong("a line longer than 1024 bytes")
            return super().__call__(prompt, timeout, echo)

    outcome, events = _login(_cfg_for(fakes, tmp_path), TooLong(EMAIL, PASSWORD))
    assert isinstance(outcome, LoginFailed)
    assert events[-1]["state"] == "failed"
    assert events[-1]["reason"] == "wrong-code"


def test_every_failed_event_carries_the_hint_or_null(fakes: FakeTools, tmp_path: Path) -> None:
    """signin-4: the app had only `message`, so every hint (bad-version's,
    keychain's, unknown-prompt's, not-recorded's, other's) was lost there."""
    fakes.set(ipatool_version="2.8.1")
    outcome, events = _login(_cfg_for(fakes, tmp_path), Stdin(EMAIL, PASSWORD))
    assert events[-1]["hint"] == outcome.hint == ipatool.bad_version_hint(
        "2.8.1", path=fakes.ipatool, configured=fakes.ipatool, source=tools.SOURCE_CONFIG)
    assert "majd/ipatool#672" in events[-1]["hint"]

    fakes.set(ipatool_version="2.8.0", login_two_factor=False, login_error="keychain write failed")
    outcome, events = _login(_cfg_for(fakes, tmp_path), Stdin(EMAIL, PASSWORD))
    assert events[-1]["reason"] == "keychain"
    assert events[-1]["hint"] == outcome.hint and "Allow" in events[-1]["hint"]

    fakes.ipatool.unlink()
    outcome, events = _login(_cfg_for(fakes, tmp_path), Stdin(EMAIL, PASSWORD))
    assert (events[-1]["reason"], events[-1]["hint"]) == ("other", outcome.hint)
    assert "install ipatool" in events[-1]["hint"]


@pytest.mark.parametrize("how", ["stdin closed", "Stop"])
def test_a_failed_event_with_no_hint_says_null(fakes: FakeTools, tmp_path: Path, how: str) -> None:
    fakes.set(login_two_factor=True)

    class StopAtCode(Stdin):
        def __call__(self, prompt, timeout, echo=False):
            if prompt == appstore_login.CODE_ASK:
                raise Stopped(signal.SIGTERM)
            return super().__call__(prompt, timeout, echo)

    stdin = Stdin(EMAIL, PASSWORD, None) if how == "stdin closed" else StopAtCode(EMAIL, PASSWORD)
    outcome, events = _login(_cfg_for(fakes, tmp_path), stdin)
    assert isinstance(outcome, Stopped if how == "Stop" else LoginFailed)
    assert events[-1]["reason"] == "stopped"
    assert "hint" in events[-1] and events[-1]["hint"] is None


@pytest.mark.parametrize("how", ["no answer", "not deleted"])
def test_keychain_clear_says_to_unlock_the_login_keychain(fakes: FakeTools, tmp_path: Path,
                                                          monkeypatch, how: str) -> None:
    """signin-4: keychain-clear sent `security`'s own words as its message,
    which never said what to do; now MESSAGES' sentence, which does, with
    the hint beside it."""
    store = FakeSecurity({(appstore_account.IPATOOL_SERVICE, "account"): "1.x"})
    monkeypatch.setattr(keychain, "_run", store)
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: fakes.root)
    if how == "no answer":
        store.find_status[appstore_account.IPATOOL_SERVICE] = 36
    else:
        store.fail_deletes = True
    from graft_ipa.config import load_config
    from tests.config_fixtures import write_test_config

    config = tmp_path / "plain.yaml"
    write_test_config(config)
    outcome, events = _login(load_config(config), Stdin(EMAIL, PASSWORD))
    assert events[-1]["reason"] == "keychain-clear"
    assert events[-1]["message"] == appstore_login.MESSAGES["keychain-clear"] == outcome.message
    assert "Unlock the login keychain in Keychain Access" in events[-1]["message"]
    assert events[-1]["hint"] == appstore_account.KEYCHAIN_UNANSWERED_HINT
    assert fakes.login_runs() == ([], [])
