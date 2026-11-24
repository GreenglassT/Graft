"""`appstore_login`: signing ipatool in by typing into its own prompts.

Always against the fake in `appstore_fakes`, which follows ipatool 2.8.0's
prompt contract (raw mode first, prompts on stderr, verdict on stdout). Never
the real binary: that would talk to Apple with a real account.

The Apple ID itself is typed at ipatool's prompt too, never passed as `-e`, so
it never shows in `ps` (controller decision 3).
"""

from __future__ import annotations

import hashlib
import json
import os
import pty
import select
import subprocess
import time
from pathlib import Path

import pytest

from tests.appstore_fakes import FAKE_EMAIL, FakeTools

PASSWORD = b"Example-Passw0rd-x7"
CODE = b"482913"

APPLE_ID_PROMPT = b"enter Apple ID (email address or phone number): "
PASSWORD_PROMPT = b"enter password: "
CODE_PROMPT = b"enter 2FA code: "


@pytest.fixture
def fakes(tmp_path: Path) -> FakeTools:
    return FakeTools(tmp_path / "tools")


def test_the_fake_refuses_a_password_on_a_pipe_like_ipatool_does(fakes: FakeTools) -> None:
    """The reason this module exists: ipatool 2.8.0 puts stdin in raw mode
    before it prompts, which fails unless stdin is a terminal. The Apple ID
    prompt before it is a plain line read, which a pipe can answer."""
    proc = subprocess.run(
        [str(fakes.ipatool), "auth", "login", "--format", "json"],
        input=FAKE_EMAIL.encode() + b"\n" + PASSWORD + b"\n", capture_output=True, timeout=30,
    )
    assert proc.returncode == 1
    assert b"failed to configure terminal" in proc.stdout
    assert APPLE_ID_PROMPT in proc.stderr, "asked for the Apple ID first, on stderr"
    assert PASSWORD not in proc.stdout + proc.stderr


def _converse(fakes: FakeTools, answers: list[tuple[bytes, bytes]]) -> tuple[bytes, bytes, int]:
    """Run the fake's `auth login` with a pty as stdin and stderr and a pipe as
    stdout, as `appstore_login` will. Each (prompt, answer) is typed when its
    prompt appears. Returns what the pty showed, stdout, and the exit status."""
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [str(fakes.ipatool), "auth", "login", "--format", "json"],
        stdin=slave, stderr=slave, stdout=subprocess.PIPE,
    )
    os.close(slave)
    shown = b""
    since = 0  # where the next prompt is looked for: after the last answer
    pending = list(answers)
    deadline = time.monotonic() + 30
    try:
        while proc.poll() is None and time.monotonic() < deadline:
            if pending and pending[0][0] in shown[since:]:
                os.write(master, pending.pop(0)[1])
                since = len(shown)
                continue
            if select.select([master], [], [], 0.1)[0]:
                try:
                    shown += os.read(master, 1024)
                except OSError:
                    break
        out = proc.communicate(timeout=30)[0]
    finally:
        os.close(master)
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return shown, out, proc.returncode


def test_the_fake_asks_for_the_apple_id_then_the_password_then_the_code(fakes: FakeTools) -> None:
    shown, out, status = _converse(fakes, [
        (APPLE_ID_PROMPT, FAKE_EMAIL.encode() + b"\n"),
        (PASSWORD_PROMPT, PASSWORD + b"\r"),
        (CODE_PROMPT, CODE + b"\n"),
    ])

    assert status == 0
    verdict = json.loads(out.splitlines()[-1])
    assert verdict["success"] is True
    assert verdict["email"] == FAKE_EMAIL
    _, (call,) = fakes.login_runs()
    assert call["argv"] == ["auth", "login", "--format", "json"]
    assert call["stdin_tty"] is True
    assert call["stdout_tty"] is False
    # Hashes of what was typed, never the values.
    assert call["email_sha256"] == hashlib.sha256(FAKE_EMAIL.encode()).hexdigest()
    assert call["password_sha256"] == hashlib.sha256(PASSWORD).hexdigest()
    assert call["code_sha256"] == hashlib.sha256(CODE).hexdigest()
    assert PASSWORD.decode() not in fakes.calls_file.read_text()


def test_the_fake_masks_the_password_and_nothing_else(fakes: FakeTools) -> None:
    """ipatool echoes `*` per character of the password and leaves the Apple
    ID and the code to the terminal's own echo."""
    shown, _, status = _converse(fakes, [
        (APPLE_ID_PROMPT, FAKE_EMAIL.encode() + b"\n"),
        (PASSWORD_PROMPT, PASSWORD + b"\r"),
        (CODE_PROMPT, CODE + b"\n"),
    ])
    assert status == 0
    assert PASSWORD not in shown
    assert b"*" * len(PASSWORD) in shown


def test_the_fake_skips_the_apple_id_prompt_when_given_one(fakes: FakeTools) -> None:
    """As ipatool does with `-e`. Graft never passes it; the fake keeps the
    behavior so a test can tell the two apart."""
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [str(fakes.ipatool), "auth", "login", "-e", FAKE_EMAIL, "--format", "json"],
        stdin=slave, stderr=slave, stdout=subprocess.PIPE,
    )
    os.close(slave)
    seen = b""
    try:
        deadline = time.monotonic() + 30
        while PASSWORD_PROMPT not in seen and proc.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    seen += os.read(master, 1024)
                except OSError:
                    break
        assert PASSWORD_PROMPT in seen, "positive control: it got as far as the password"
        assert APPLE_ID_PROMPT not in seen
    finally:
        proc.kill()
        proc.wait()
        os.close(master)
        proc.stdout.close()


def test_a_purchase_and_a_revoke_answer_as_ipatool_does(fakes: FakeTools) -> None:
    bought = subprocess.run(
        [str(fakes.ipatool), "purchase", "-b", "com.example.app", "--format", "json", "--non-interactive"],
        capture_output=True, text=True, timeout=30,
    )
    assert json.loads(bought.stdout) == {"level": "info", "alreadyOwned": False, "success": True}
    revoked = subprocess.run(
        [str(fakes.ipatool), "auth", "revoke", "--format", "json", "--non-interactive"],
        capture_output=True, text=True, timeout=30,
    )
    assert json.loads(revoked.stdout)["success"] is True


# --- sign_in(): the pty conversation -------------------------------------------

import io  # noqa: E402

from graft_ipa import appstore_login  # noqa: E402
from graft_ipa.appstore_login import LoginFailed  # noqa: E402
from graft_ipa.errors import AppStoreError  # noqa: E402
from graft_ipa.json_output import JsonEmitter  # noqa: E402
from graft_ipa.secret_input import SecretLine  # noqa: E402


def secret(value: bytes) -> SecretLine:
    line = SecretLine()
    line._buf[: len(value)] = value
    line._len = len(value)
    return line


class Codes:
    """The 2FA code callback: hands over `code` and counts the asks."""

    def __init__(self, code: bytes | None = CODE) -> None:
        self.code = code
        self.asked = 0

    def __call__(self, timeout: float) -> SecretLine | None:
        self.asked += 1
        return None if self.code is None else secret(self.code)


def _sign_in(fakes: FakeTools, *, codes=None, **kw) -> tuple[str, list[dict]]:
    # The silence limit counts from the spawn. macOS's check of a new
    # executable on its first run belongs before that, not inside the limit.
    fakes.warm_up()
    stream = io.StringIO()
    email = appstore_login.sign_in(
        fakes.ipatool, secret(FAKE_EMAIL.encode()), secret(PASSWORD), codes or Codes(),
        emitter=JsonEmitter(stream=stream), say=lambda _: None, **kw,
    )
    return email, [json.loads(line) for line in stream.getvalue().splitlines()]


def _states(events: list[dict]) -> list[str]:
    return [e["state"] for e in events if e["type"] == "appstore-login"]


def test_nothing_of_the_account_is_on_ipatools_argv_or_in_its_environment(fakes: FakeTools) -> None:
    """Not even the Apple ID: it is typed at ipatool's own prompt (decision 3)."""
    _sign_in(fakes)
    _, (call,) = fakes.login_runs()
    assert call["argv"] == ["auth", "login", "--format", "json"]
    for value in (FAKE_EMAIL, PASSWORD.decode(), CODE.decode()):
        assert not any(value in a for a in call["argv"])
        assert not any(value in v for v in call["env"].values())


def test_the_apple_id_is_typed_at_its_prompt(fakes: FakeTools) -> None:
    _sign_in(fakes)
    _, (call,) = fakes.login_runs()
    assert call["email_sha256"] == hashlib.sha256(FAKE_EMAIL.encode()).hexdigest()


def test_the_password_is_typed_at_the_prompt_on_a_terminal(fakes: FakeTools) -> None:
    _sign_in(fakes)
    _, (call,) = fakes.login_runs()
    assert call["stdin_tty"] is True
    assert call["stdout_tty"] is False, "the verdict must come back on a pipe, apart from the prompts"
    assert call["password_sha256"] == hashlib.sha256(PASSWORD).hexdigest()


def test_without_two_factor_no_code_is_asked_for(fakes: FakeTools) -> None:
    """The case the spikes saw every time on this Mac (decision 8)."""
    fakes.set(login_two_factor=False)
    codes = Codes()
    email, events = _sign_in(fakes, codes=codes)
    assert email == FAKE_EMAIL
    assert codes.asked == 0
    assert _states(events) == ["preparing"]


def test_with_two_factor_the_code_follows_needs_code(fakes: FakeTools) -> None:
    codes = Codes()
    email, events = _sign_in(fakes, codes=codes)
    states = _states(events)
    assert states.index("preparing") < states.index("needs-code") < states.index("verifying")
    assert codes.asked == 1
    _, (call,) = fakes.login_runs()
    assert call["code_sha256"] == hashlib.sha256(CODE).hexdigest()
    assert email == FAKE_EMAIL


def test_a_silent_ipatool_gets_heartbeats(fakes: FakeTools) -> None:
    """A first sign-in can sit silent for minutes; the app shows these."""
    fakes.set(login_silent=0.8, login_two_factor=False)
    _, events = _sign_in(fakes, heartbeat=0.2)
    assert _states(events).count("waiting") >= 2
    assert all(isinstance(e["elapsed"], int) for e in events if e["state"] == "waiting")


def test_no_event_carries_a_secret(fakes: FakeTools) -> None:
    _, events = _sign_in(fakes)
    text = json.dumps(events)
    assert PASSWORD.decode() not in text
    assert CODE.decode() not in text


def test_the_code_is_wiped_once_typed(fakes: FakeTools) -> None:
    handed: list[SecretLine] = []

    def codes(timeout: float) -> SecretLine:
        handed.append(secret(CODE))
        return handed[-1]

    _sign_in(fakes, codes=codes)
    assert handed[0]._buf == bytearray(len(handed[0]._buf))


def test_a_code_typed_with_a_space_is_still_six_digits(fakes: FakeTools) -> None:
    _sign_in(fakes, codes=Codes(b"482 913"))
    _, (call,) = fakes.login_runs()
    assert call["code_sha256"] == hashlib.sha256(CODE).hexdigest()


def test_the_silence_limit_allows_a_first_sign_in_its_download() -> None:
    """The first sign-in downloads ~320 MB and Unicorn while silent: it is
    never stopped as hung in under about 15 minutes."""
    assert appstore_login.SILENCE_LIMIT_SECONDS >= 15 * 60


def test_the_states_are_the_words_the_app_reads() -> None:
    import re

    from tests.cli_ast import contract_swift_source

    block = re.search(r"enum AppStoreLogin \{(.*?)\n    \}", contract_swift_source(), re.S)
    assert block, "CLIContract.AppStoreLogin is gone from CLIContract.swift"
    swift = dict(re.findall(r'static let (\w+) = "([^"\\]*)"', block.group(1)))
    assert swift == {
        "preparing": appstore_login.STATE_PREPARING,
        "waiting": appstore_login.STATE_WAITING,
        "needsCode": appstore_login.STATE_NEEDS_CODE,
        "verifying": appstore_login.STATE_VERIFYING,
        "signedIn": appstore_login.STATE_SIGNED_IN,
        "failed": appstore_login.STATE_FAILED,
    }


# --- classify(), timeouts and Stop ----------------------------------------------

from graft_ipa import ipatool  # noqa: E402

#: S1.4, recorded on this Mac: a wrong password, then a code Apple never sent.
NOT_ACCEPTED = "apple did not complete verification; try a fresh 2FA code"
#: K1: what ipatool says after a denied Keychain dialog (and for no item at all).
DENIED_K1 = (
    "failed to get account: failed to get item: The specified item could not be found in the keyring"
)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (NOT_ACCEPTED, "not-accepted"),
        ("2FA code must contain exactly six digits", "wrong-code"),
        ("Your Apple ID or password was entered incorrectly.", "wrong-password"),
        ("account is disabled", "locked"),
        ("This Apple ID has been locked for security reasons.", "locked"),
        ("apple rate limited authentication; try again later: HTTP 429", "rate-limited"),
        (
            "sign-in request failed: authentication request failed after 3 attempts "
            "(transport error, transport error, transport error): EOF",
            "network",
        ),
        (
            "failed to save account in keychain: failed to set item: The user name or "
            "passphrase you entered is not correct.",
            "keychain",
        ),
        ("failed to save account in keychain: the keychain is locked", "keychain"),
        ("something nobody has seen before", "other"),
    ],
)
def test_each_failure_is_one_plain_message(error: str, reason: str) -> None:
    failed = appstore_login.classify(error)
    assert failed.reason == reason
    assert failed.message.startswith(appstore_login.MESSAGES[reason])


def test_a_refused_password_and_code_blames_neither_alone() -> None:
    """Decision 9: ipatool's text blames the code, but a wrong password gets it
    too. Never "the code" alone, never "wait for a code"."""
    message = appstore_login.classify(NOT_ACCEPTED).message
    assert message == "Apple didn't accept the password or the code: check both and try again."
    assert "wait" not in message.lower()


def test_with_no_code_typed_only_the_password_is_named() -> None:
    failed = appstore_login.classify(NOT_ACCEPTED, code_typed=False)
    assert failed.reason == "not-accepted"
    assert failed.message == appstore_login.PASSWORD_NOT_ACCEPTED
    assert "code" not in failed.message


def test_ipatool_fetching_unicorn_offline_says_the_first_use_sentence() -> None:
    """The first sign-in fetches Unicorn from PyPI; offline it fails with
    these words (ipatool 2.8.0 internal/sap/unicorn), and the user is told what
    every other Graft path tells them."""
    failed = appstore_login.classify(
        "sign-in request failed: failed to download Unicorn: Get "
        "\"https://pypi.org/pypi/unicorn/json\": dial tcp: lookup pypi.org: no such host"
    )
    assert failed.reason == "network"
    assert failed.message == ipatool.UNICORN_FIRST_USE
    assert "pypi.org" in failed.hint


def test_an_unrecognised_failure_still_says_what_ipatool_said() -> None:
    assert "something nobody has seen" in appstore_login.classify(
        "something nobody has seen"
    ).message


def test_a_wrong_password_or_code_from_ipatool_fails_as_not_accepted(fakes: FakeTools) -> None:
    fakes.set(login_code_error=NOT_ACCEPTED)
    with pytest.raises(LoginFailed) as e:
        _sign_in(fakes)
    assert e.value.reason == "not-accepted"
    assert "password or the code" in e.value.message


def test_a_refusal_before_any_code_names_only_the_password(fakes: FakeTools) -> None:
    fakes.set(login_error=NOT_ACCEPTED, login_two_factor=False)
    with pytest.raises(LoginFailed) as e:
        _sign_in(fakes)
    assert e.value.message == appstore_login.PASSWORD_NOT_ACCEPTED


def test_a_code_that_is_not_six_digits_is_never_typed(fakes: FakeTools) -> None:
    with pytest.raises(LoginFailed) as e:
        _sign_in(fakes, codes=Codes(b"12345"))
    assert e.value.reason == "wrong-code"
    started, finished = fakes.login_runs()
    assert started and not finished, "ipatool was stopped before it got a code"


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_ipatool_silent_past_the_limit_is_stopped_as_a_timeout(fakes: FakeTools) -> None:
    fakes.set(login_silent=60, login_two_factor=False)
    began = time.monotonic()
    with pytest.raises(LoginFailed) as e:
        # Past the Python start-up of the fake, which must have begun: the
        # limit counts from ipatool's last output, its "preparing" line.
        _sign_in(fakes, silence_limit=2.0, heartbeat=0.2)
    assert e.value.reason == "timeout"
    assert time.monotonic() - began < 20
    (started,), _ = fakes.login_runs()
    assert _gone(started["pid"])


def test_no_code_in_time_is_a_timeout_that_doesnt_promise_a_code(fakes: FakeTools) -> None:
    def late(timeout: float) -> SecretLine:
        raise TimeoutError

    with pytest.raises(LoginFailed) as e:
        _sign_in(fakes, codes=late)
    assert e.value.reason == "timeout"
    assert "password may be wrong" in e.value.message


def test_stdin_closing_while_ipatool_waits_for_the_code_is_a_stop(fakes: FakeTools) -> None:
    with pytest.raises(LoginFailed) as e:
        _sign_in(fakes, codes=Codes(None))
    assert e.value.reason == "stopped"
    (started,), _ = fakes.login_runs()
    assert _gone(started["pid"])


def test_a_failed_verdict_never_repeats_a_secret(fakes: FakeTools) -> None:
    fakes.set(login_error=f"bad login for {FAKE_EMAIL} with {PASSWORD.decode()}")
    with pytest.raises(LoginFailed) as e:
        _sign_in(fakes)
    assert PASSWORD.decode() not in e.value.message
    assert FAKE_EMAIL not in e.value.message
    assert "[redacted]" in e.value.message


# --- run(): the verb's body ----------------------------------------------------

import yaml  # noqa: E402

from graft_ipa import appstore_account, keychain, tools  # noqa: E402
from graft_ipa.config import load_config  # noqa: E402
from tests.keychain_fakes import FakeSecurity  # noqa: E402


@pytest.fixture
def cfg(temp_config_env: Path, fakes: FakeTools):
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["appstore"] = fakes.appstore_section()
    temp_config_env.write_text(yaml.safe_dump(raw, sort_keys=False))
    return load_config(temp_config_env)


@pytest.fixture
def bundled_cfg(temp_config_env: Path, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch):
    """No appstore: section, and the fakes standing in for Graft.app's own tools."""
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: fakes.root)
    return load_config(temp_config_env)


class Stdin:
    """`read_line` for `run`: the lines the app sends, in order (None = closed)."""

    def __init__(self, *lines: bytes | None) -> None:
        self.lines = list(lines)
        self.handed: list[SecretLine] = []
        self.asked: list[tuple[str, bool]] = []

    def __call__(self, prompt: str, timeout: float | None, echo: bool = False) -> SecretLine | None:
        self.asked.append((prompt, echo))
        value = self.lines.pop(0) if self.lines else None
        if value is None:
            return None
        self.handed.append(secret(value))
        return self.handed[-1]


def _run(cfg, stdin: Stdin) -> tuple[object, list[dict]]:
    stream = io.StringIO()
    try:
        outcome: object = appstore_login.run(
            cfg, emitter=JsonEmitter(stream=stream), say=lambda _: None, read_line=stdin,
        )
    except AppStoreError as e:  # LoginFailed among them
        outcome = e
    return outcome, [json.loads(line) for line in stream.getvalue().splitlines()]


EMAIL = FAKE_EMAIL.encode()


def test_a_sign_in_ends_signed_in_and_leaves_the_marker(cfg, fakes: FakeTools) -> None:
    stdin = Stdin(EMAIL, PASSWORD, CODE)
    outcome, events = _run(cfg, stdin)
    assert outcome == FAKE_EMAIL
    assert events[-1] == {"type": "appstore-login", "state": "signed-in", "email": FAKE_EMAIL}
    assert appstore_account.marker_path().exists()
    assert len(stdin.handed) == 3
    assert all(line._buf == bytearray(len(line._buf)) for line in stdin.handed)


def test_the_apple_id_is_asked_with_echo_and_the_secrets_without(cfg, fakes: FakeTools) -> None:
    stdin = Stdin(EMAIL, PASSWORD, CODE)
    _run(cfg, stdin)
    assert [echo for _, echo in stdin.asked] == [True, False, False]


def test_a_failed_sign_in_says_why_and_leaves_no_marker(cfg, fakes: FakeTools) -> None:
    fakes.set(login_error="account is disabled")
    outcome, events = _run(cfg, Stdin(EMAIL, PASSWORD))
    assert isinstance(outcome, LoginFailed)
    assert events[-1] == {
        "type": "appstore-login", "state": "failed", "reason": "locked",
        "message": appstore_login.MESSAGES["locked"], "hint": None,
    }
    assert not appstore_account.marker_path().exists()


@pytest.mark.parametrize(
    ("lines", "words"),
    [
        ((None,), "no Apple ID"),
        ((b"",), "no Apple ID"),
        ((b"two words@example.invalid", PASSWORD), "no spaces"),
        ((EMAIL, None), "no password"),
        ((EMAIL, b""), "no password"),
    ],
)
def test_a_missing_line_is_refused_before_ipatool_runs(cfg, fakes: FakeTools, lines, words) -> None:
    outcome, events = _run(cfg, Stdin(*lines))
    assert isinstance(outcome, LoginFailed)
    assert events[-1]["state"] == "failed"
    assert words in events[-1]["message"]
    assert fakes.login_runs() == ([], [])


def test_a_missing_ipatool_is_a_failed_event_too(cfg, fakes: FakeTools) -> None:
    fakes.ipatool.unlink()
    outcome, events = _run(cfg, Stdin(EMAIL, PASSWORD))
    assert isinstance(outcome, AppStoreError)
    assert events[-1]["state"] == "failed"
    assert "ipatool not found" in events[-1]["message"]


def test_a_1x_sign_in_is_cleared_before_graft_signs_in(
    bundled_cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSecurity({
        (appstore_account.IPATOOL_SERVICE, k): "old" for k in appstore_account.IPATOOL_ITEMS
    })
    monkeypatch.setattr(keychain, "_run", fake)
    outcome, _ = _run(bundled_cfg, Stdin(EMAIL, PASSWORD, CODE))
    assert outcome == FAKE_EMAIL
    assert not [k for k in fake.items if k[0] == appstore_account.IPATOOL_SERVICE]


def test_a_1x_device_item_alone_is_cleared_too(bundled_cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    """`auth revoke` leaves `anisette-v4`; the bundled ipatool reading it
    would ask for the Keychain password (K2)."""
    fake = FakeSecurity({(appstore_account.IPATOOL_SERVICE, "anisette-v4"): "old"})
    monkeypatch.setattr(keychain, "_run", fake)
    _run(bundled_cfg, Stdin(EMAIL, PASSWORD, CODE))
    assert fake.items == {}


def test_graft_s_own_sign_in_is_never_cleared(bundled_cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSecurity({(appstore_account.IPATOOL_SERVICE, "account"): "ours"})
    monkeypatch.setattr(keychain, "_run", fake)
    appstore_account.mark_signed_in(tools.SOURCE_BUNDLED)
    _run(bundled_cfg, Stdin(EMAIL, PASSWORD, CODE))
    assert (appstore_account.IPATOOL_SERVICE, "account") in fake.items
    assert not [c for c in fake.calls if c[1] == "delete-generic-password"]


def test_the_users_own_ipatool_s_items_are_never_cleared(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSecurity({(appstore_account.IPATOOL_SERVICE, "account"): "theirs"})
    monkeypatch.setattr(keychain, "_run", fake)
    _run(cfg, Stdin(EMAIL, PASSWORD, CODE))
    assert not [c for c in fake.calls if c[1] == "delete-generic-password"]


def test_nothing_is_cleared_when_the_sign_in_is_refused_before_ipatool(
    bundled_cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSecurity({(appstore_account.IPATOOL_SERVICE, "account"): "old"})
    monkeypatch.setattr(keychain, "_run", fake)
    _run(bundled_cfg, Stdin(EMAIL, None))
    assert (appstore_account.IPATOOL_SERVICE, "account") in fake.items
