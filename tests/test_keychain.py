"""The Keychain service rename: `tweak-build` items are found and copied to `graft`.

Run against a fake `security` (a dict keyed by service and account), never
the real login keychain. The autouse fixture in conftest installs an empty
one for every test and sets GRAFT_NO_KEYCHAIN=1; these install their own so
they can seed it and look.
"""

from __future__ import annotations

import shlex
import subprocess

import pytest

from graft_ipa import keychain

from .keychain_fakes import FakeSecurity


@pytest.fixture
def security(monkeypatch: pytest.MonkeyPatch) -> FakeSecurity:
    fake = FakeSecurity()
    monkeypatch.setattr(keychain, "_run", fake)
    return fake


def _services_queried(fake: FakeSecurity) -> list[str]:
    return [c[c.index("-s") + 1] for c in fake.calls if c[1] == "find-generic-password"]


def test_the_service_names() -> None:
    assert keychain.SERVICE == "graft"
    assert keychain.LEGACY_SERVICE == "tweak-build"


def test_an_item_under_graft_is_read_without_looking_further(security: FakeSecurity) -> None:
    security.items[("graft", "ssh-UDID1")] = "new-pw"
    security.items[("tweak-build", "ssh-UDID1")] = "old-pw"

    assert keychain.read_ssh_password("UDID1") == "new-pw"
    assert _services_queried(security) == ["graft"]


@pytest.mark.parametrize(
    ("read", "account"),
    [
        (lambda: keychain.read_ssh_password("UDID1"), "ssh-UDID1"),
        (lambda: keychain.read_p12_password("TEAM123456"), "p12-TEAM123456"),
    ],
    ids=["ssh", "p12"],
)
def test_a_legacy_item_is_returned_and_copied_to_graft_and_kept(
    security: FakeSecurity, read, account: str
) -> None:
    security.items[("tweak-build", account)] = "old-pw"

    assert read() == "old-pw"

    assert security.items[("graft", account)] == "old-pw"
    # Kept, not moved: an older install on this machine (or the pre-rename
    # app still in /Applications) reads the old service and must keep working.
    assert security.items[("tweak-build", account)] == "old-pw"
    # And copied once: the next read is answered by the new service alone.
    security.calls.clear()
    assert read() == "old-pw"
    assert _services_queried(security) == ["graft"]


def test_the_copy_keeps_any_app_able_to_read_it(security: FakeSecurity) -> None:
    """`-A`, like every write here: without it the Dock-launched GUI would get
    a Keychain Access prompt on every build for the copied item."""
    security.items[("tweak-build", "ssh-UDID1")] = "old-pw"
    keychain.read_ssh_password("UDID1")
    tokens = shlex.split(security.stdin[-1])
    assert "-A" in tokens and tokens[tokens.index("-s") + 1] == "graft"


def test_a_miss_in_both_is_none_and_writes_nothing(security: FakeSecurity) -> None:
    assert keychain.read_ssh_password("UDID1") is None
    assert _services_queried(security) == ["graft", "tweak-build"]
    assert security.items == {}


def test_only_a_not_found_falls_back_to_the_legacy_item(security: FakeSecurity) -> None:
    """A denied prompt or a locked keychain is not "no such item". Treating it
    as one would read the stale legacy password and write it *over* the newer
    `graft` item the user could not unlock this time."""
    security.items[("graft", "ssh-UDID1")] = "new-pw"
    security.items[("tweak-build", "ssh-UDID1")] = "stale-pw"
    security.find_status["graft"] = 51  # the user pressed Deny

    assert keychain.read_ssh_password("UDID1") is None

    assert _services_queried(security) == ["graft"]
    assert security.items[("graft", "ssh-UDID1")] == "new-pw"
    assert security.stdin == []


def test_a_failed_copy_still_returns_the_secret(security: FakeSecurity) -> None:
    """A locked keychain can refuse the write; the secret was read fine and
    the build that asked for it should not fail over a copy. The next read
    tries the copy again."""
    security.items[("tweak-build", "ssh-UDID1")] = "old-pw"
    security.fail_writes = True

    assert keychain.read_ssh_password("UDID1") == "old-pw"
    assert ("graft", "ssh-UDID1") not in security.items


def test_writes_go_to_graft_only(security: FakeSecurity) -> None:
    keychain.write_ssh_password("UDID1", "pw")
    keychain.write_p12_password("TEAM123456", "p12")

    assert security.items == {("graft", "ssh-UDID1"): "pw", ("graft", "p12-TEAM123456"): "p12"}


# --- the secret never goes on a command line --------------------------------


SECRET = "s3cret-that-must-not-be-in-ps"


def test_a_written_secret_is_never_on_any_argv(security: FakeSecurity) -> None:
    """`ps -axww` shows every process's arguments to every user on the Mac.
    The password goes to `security -i` on stdin instead."""
    keychain.write_ssh_password("UDID1", SECRET)
    keychain.write_p12_password("TEAM123456", SECRET)
    security.items[("tweak-build", "ssh-UDID2")] = SECRET
    keychain.read_ssh_password("UDID2")  # the legacy copy writes too

    assert len(security.stdin) == 3
    assert all(SECRET in line for line in security.stdin)
    for argv in security.calls:
        assert all(SECRET not in arg for arg in argv), argv


@pytest.mark.parametrize(
    "secret",
    ["with space", 'dq"inside', "back\\slash", "trail\\", '\\"both', "it's", "$HOME `x` ;&|", "-w", ""],
)
def test_secrets_survive_the_quoting(security: FakeSecurity, secret: str) -> None:
    """Every one of these was written through real `security -i` to a
    throwaway keychain and read back unchanged while this was built."""
    keychain.write_ssh_password("UDID1", secret)
    assert security.items[("graft", "ssh-UDID1")] == secret


@pytest.mark.parametrize("secret", ["two\nlines", "cr\rhere", "nul\0byte"])
def test_a_secret_that_would_end_the_command_is_refused(
    security: FakeSecurity, secret: str
) -> None:
    """`security -i` reads one command per line; a line break in the value
    would end it early and run the rest as a second command."""
    with pytest.raises(subprocess.CalledProcessError) as e:
        keychain.write_ssh_password("UDID1", secret)
    assert b"line break" in e.value.stderr
    assert security.calls == []


@pytest.mark.parametrize(
    "team", ["ABCDE12345\ndelete-keychain login.keychain", "ABCDE12345\rx", "ABCDE12345\0x"]
)
def test_an_account_that_would_end_the_command_is_refused(
    security: FakeSecurity, team: str
) -> None:
    """The account goes into the same `security -i` line as the secret, and
    it is built from a team id, which can come off a downloaded profile."""
    with pytest.raises(subprocess.CalledProcessError) as e:
        keychain.write_p12_password(team, "pw")
    assert b"Keychain account cannot contain a line break" in e.value.stderr
    assert security.calls == []


@pytest.mark.parametrize("team", ['AB"CD', "AB\\CD", "AB\\", 'AB" -w "x'])
def test_quotes_in_an_account_stay_inside_it(security: FakeSecurity, team: str) -> None:
    """`_quote` keeps a quote or a backslash inside the one argument, so the
    account is stored as given and no other option is read out of it."""
    keychain.write_p12_password(team, "pw")
    assert security.items == {("graft", f"p12-{team}"): "pw"}


def test_a_write_that_exits_zero_but_complains_is_a_failure(security: FakeSecurity) -> None:
    """`security -i` keeps going after a failed command and can exit 0; the
    complaint on stderr is the only sign."""
    security.write_stderr_on_success = b"add-generic-password: returned -25308\n"
    with pytest.raises(subprocess.CalledProcessError) as e:
        keychain.write_ssh_password("UDID1", "pw")
    assert b"-25308" in e.value.stderr


def test_a_failed_write_raises_with_what_security_said(security: FakeSecurity) -> None:
    security.fail_writes = True
    with pytest.raises(subprocess.CalledProcessError) as e:
        keychain.write_p12_password("TEAM123456", "pw")
    assert b"write refused" in e.value.stderr


# --- reading what `security -g` prints ---------------------------------------


@pytest.mark.parametrize(
    "secret",
    ["plain", 'dq"in"side', 'ends"', "unicodé ✓", "tab\there", "back\\slash",
     "0x41", 'password: "x"', "trailing ", "  lead", "emoji 🔑",
     # No printable byte at all: `security` prints the hex alone, with one
     # trailing space and no quoted rendering.
     "日本語", "\\", "é", "\\\\"],
)
def test_every_secret_reads_back_as_itself(security: FakeSecurity, secret: str) -> None:
    """`-w` prints anything non-ASCII, a control character or a backslash as
    bare hex, indistinguishable from a password that *is* hex. `-g` says
    which form it used (`0x…` or a quoted string), so the real value comes
    back — shapes measured on a throwaway keychain."""
    security.items[("graft", "ssh-UDID1")] = secret
    assert keychain.read_ssh_password("UDID1") == secret


@pytest.mark.parametrize("secret", ["unicodé ✓", "tab\there", "back\\slash", "日本語", "é"])
def test_a_legacy_secret_is_copied_as_itself_not_as_hex(
    security: FakeSecurity, secret: str
) -> None:
    """The copy happens once and is never re-read from the old service, so
    a hex rendering copied here would *be* the password from then on."""
    security.items[("tweak-build", "ssh-UDID1")] = secret

    assert keychain.read_ssh_password("UDID1") == secret
    assert security.items[("graft", "ssh-UDID1")] == secret


def test_reads_use_g_and_never_w(security: FakeSecurity) -> None:
    security.items[("graft", "ssh-UDID1")] = "pw"
    keychain.read_ssh_password("UDID1")
    (find,) = [c for c in security.calls if c[1] == "find-generic-password"]
    assert "-g" in find and "-w" not in find


def test_an_empty_secret_reads_back_empty(security: FakeSecurity) -> None:
    """A .p12 exported with a blank password is stored as "", and its builds
    must find it: "" is a password, None is none."""
    security.items[("graft", "ssh-UDID1")] = ""
    security.items[("tweak-build", "ssh-UDID1")] = "old"
    assert keychain.read_ssh_password("UDID1") == ""
    # Found, just empty: not a miss, so not a reason to look at the old one.
    assert _services_queried(security) == ["graft"]


@pytest.mark.parametrize(
    "line",
    [
        "password: 0xZZ",          # not hex
        "password: 0xFF",          # hex, but not UTF-8
        "password: <unexpected>",  # neither shape
        'password: "unterminated',
        "",                        # no password line at all
    ],
)
def test_an_unreadable_answer_is_not_a_miss(security: FakeSecurity, line: str) -> None:
    """Something `security` returned that this cannot read must not count as
    "no such item": falling back would copy the stale legacy password over
    an item that exists."""
    security.items[("graft", "ssh-UDID1")] = "whatever"
    security.items[("tweak-build", "ssh-UDID1")] = "stale"
    security.password_lines["graft"] = line or "class: nothing here"

    assert keychain.read_ssh_password("UDID1") is None
    assert _services_queried(security) == ["graft"]
    assert security.stdin == []


def test_an_unreadable_legacy_answer_is_not_copied(security: FakeSecurity) -> None:
    security.items[("tweak-build", "ssh-UDID1")] = "x"
    security.password_lines["tweak-build"] = "password: 0xFF"

    assert keychain.read_ssh_password("UDID1") is None
    assert ("graft", "ssh-UDID1") not in security.items


# --- the kill switch ---------------------------------------------------------


def test_graft_no_keychain_makes_every_read_a_miss_and_every_write_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For spawned test subprocesses, which do not inherit the in-process fake."""

    def real_security_called(*args, **kwargs):
        raise AssertionError("reached the real `security`")

    monkeypatch.setattr(keychain, "_run", keychain._run_security)
    monkeypatch.setattr(keychain.subprocess, "run", real_security_called)
    monkeypatch.setenv("GRAFT_NO_KEYCHAIN", "1")

    assert keychain.read_ssh_password("UDID1") is None
    assert keychain.read_p12_password("TEAM123456") is None
    with pytest.raises(subprocess.CalledProcessError) as e:
        keychain.write_ssh_password("UDID1", "pw")
    assert b"GRAFT_NO_KEYCHAIN" in e.value.stderr


def test_no_security_binary_reads_as_a_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(argv, **kwargs):
        raise FileNotFoundError("security")

    monkeypatch.setattr(keychain, "_run", missing)
    assert keychain.read_ssh_password("UDID1") is None


def test_the_suite_never_reaches_the_real_keychain() -> None:
    """Both guards from conftest: the in-process fake, and the switch that
    subprocesses the suite spawns inherit."""
    import os

    assert keychain._run is not keychain._run_security
    assert os.environ.get("GRAFT_NO_KEYCHAIN") == "1"


# --- `security -i`'s 4096-byte line (the 2.0 review's SEC-1, F2, SEC-3) ------


@pytest.mark.parametrize(("size", "fits"), [
    (1024, True), (1025, False), (4095, False), (4096, False), (8192, False),
])
def test_a_secret_over_1024_bytes_is_refused_before_security_runs(
    security: FakeSecurity, size: int, fits: bool
) -> None:
    """`security -i` ends a command after 4095 bytes and runs the rest as
    another: whoever chose a long .p12 password would choose that command."""
    secret = ("SECRET-PIECE-" * 700)[:size]
    if fits:
        keychain.write_p12_password("ABCDE12345", secret)
        assert security.items[("graft", "p12-ABCDE12345")] == secret
        return
    with pytest.raises(subprocess.CalledProcessError) as e:
        keychain.write_p12_password("ABCDE12345", secret)
    assert security.calls == []
    assert b"longer than 1024 bytes" in e.value.stderr
    assert b"SECRET-PIECE" not in e.value.stderr


@pytest.mark.parametrize(("secret", "fits"), [
    ("é" * 512, True),            # 1024 bytes of UTF-8
    ("é" * 512 + "a", False),     # 1025
    ("€" * 341, True),            # 1023
    ("€" * 342, False),           # 1026
    ("🔑" * 256, True),           # 1024
    ("🔑" * 256 + "a", False),
    ('"' * 512, True),            # escaped, 1024
    ('"' * 513, False),           # escaped, 1026: the cap is on what is sent
    ("\\" * 511 + "ab", True),    # escaped, 1024
    ("\\" * 512 + "a", False),
])
def test_the_cap_counts_escaped_utf8_bytes(security: FakeSecurity, secret: str, fits: bool) -> None:
    assert keychain.secret_fits(secret) is fits
    if fits:
        keychain.write_ssh_password("UDID1", secret)
        assert security.items[("graft", "ssh-UDID1")] == secret
    else:
        with pytest.raises(subprocess.CalledProcessError):
            keychain.write_ssh_password("UDID1", secret)
        assert security.calls == []


def test_every_line_write_builds_stays_under_the_line_cap(security: FakeSecurity) -> None:
    """A property, over a few thousand hostile secrets of every length up to
    8 KiB, for both accounts and the longest a UDID or team gets: each is
    either refused before `security` runs, or sent as one line under
    `MAX_LINE_BYTES` that stores exactly the secret."""
    import random

    rng = random.Random(20261120)
    alphabet = ['"', "\\", "'", " ", "$", "`", ";", "-", "x", "é", "€", "🔑", "\t", "A"]
    accounts = [("p12", "ABCDE12345"), ("ssh", "00008130-001A2B3C0123456D"), ("ssh", "f" * 40)]
    for n in range(3000):
        size = rng.choice([rng.randint(0, 40), rng.randint(900, 1100), rng.randint(0, 8192)])
        secret = "".join(rng.choice(alphabet) for _ in range(size))
        kind, who = accounts[n % len(accounts)]
        write = keychain.write_p12_password if kind == "p12" else keychain.write_ssh_password
        sent = len(security.stdin)
        try:
            write(who, secret)
        except subprocess.CalledProcessError:
            assert len(security.stdin) == sent, "refused, but security ran"
            assert not keychain.secret_fits(secret)
            continue
        (line,) = security.stdin[sent:]
        assert len(line.encode()) < keychain.MAX_LINE_BYTES, len(line.encode())
        assert line.count("\n") == 1 and line.endswith("\n")
        assert security.items[("graft", f"{kind}-{who}")] == secret


def test_a_line_over_the_cap_is_refused_even_if_the_secret_fits(
    security: FakeSecurity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The floor under the secret cap: were it raised, or an account to grow,
    the line itself is still measured before `security` sees it."""
    monkeypatch.setattr(keychain, "MAX_SECRET_BYTES", 10**6)
    secret = "y" * (keychain.MAX_LINE_BYTES - 60)
    with pytest.raises(subprocess.CalledProcessError) as e:
        keychain.write_p12_password("ABCDE12345", secret)
    assert security.calls == []
    assert b"over the 3800" in e.value.stderr
    keychain.write_p12_password("ABCDE12345", "y" * (keychain.MAX_LINE_BYTES - 100))


def test_what_security_says_is_redacted_of_any_piece_of_the_secret(security: FakeSecurity) -> None:
    secret = "Tr0ub4dor&3-LAST-PART-OF-THE-SECRET"
    security.write_stderr_on_success = (
        f'security: unknown command "{secret[-23:]}"\n'
        f'{secret[-12:]}": returned 1\n'
        "add-generic-password: returned -25308\n"
    ).encode()
    with pytest.raises(subprocess.CalledProcessError) as e:
        keychain.write_p12_password("ABCDE12345", secret)
    said = e.value.stderr.decode() + (e.value.output or b"").decode()
    assert "-25308" in said, "security's own words stay"
    assert not [secret[i:i + 4] for i in range(len(secret) - 3) if secret[i:i + 4] in said]


@pytest.mark.parametrize("call", [
    lambda: keychain.write_p12_password("ABCDE12345", "pw"),
    lambda: keychain.read_p12_password("ABCDE12345"),
    lambda: keychain.find_p12_password("ABCDE12345"),
    lambda: keychain.delete_p12_password("ABCDE12345"),
], ids=["write", "read", "find", "delete"])
def test_a_keychain_that_does_not_answer_is_given_up_on(
    monkeypatch: pytest.MonkeyPatch, call
) -> None:
    """A locked keychain's unlock dialog, behind the window: `security`
    would wait for ever, and the app with it."""
    timeouts = []

    def hangs(argv, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 0)

    monkeypatch.setattr(keychain, "_run", hangs)
    with pytest.raises(keychain.KeychainNoAnswer) as e:
        call()
    assert timeouts == [60]
    assert e.value.message == (
        "The Keychain didn't answer; unlock it in Keychain Access and try again."
    )


def test_a_named_keychain_goes_last_on_the_line(security: FakeSecurity) -> None:
    keychain._write("acct", "pw", keychain_path="/tmp/x y.keychain")
    assert security.stdin[-1].endswith(' -A "/tmp/x y.keychain"\n')
    assert security.calls[0][-1] == "/tmp/x y.keychain", "the existence check names it too"


def test_a_new_item_is_created_with_any_app_access_and_an_old_one_keeps_its_own(
    security: FakeSecurity,
) -> None:
    """`-U -A` on an item that is there asks macOS to change its access list,
    which needs the keychain password: a dialog in front of the user, and
    refused, a failed command that has already replaced the password
    (measured). So a new item gets `-A`, an existing one `-U` alone."""
    keychain.write_p12_password("ABCDE12345", "first")
    keychain.write_p12_password("ABCDE12345", "second")

    first, second = (shlex.split(line) for line in security.stdin)
    assert "-A" in first and "-U" not in first
    assert "-U" in second and "-A" not in second
    assert security.items[("graft", "p12-ABCDE12345")] == "second"
    assert security.access_prompts == 0


def test_an_existence_check_that_fails_is_a_failed_write(security: FakeSecurity) -> None:
    security.find_status["graft"] = 51
    with pytest.raises(subprocess.CalledProcessError):
        keychain.write_p12_password("ABCDE12345", "pw")
    assert security.stdin == []


@pytest.mark.parametrize(("secret", "fits"), [
    ("€" * 341, True), ("€" * 342, False),   # 1023 and 1026 bytes of UTF-8
    ('"' * 512, True), ('"' * 513, False),   # each escaped to two bytes
    ("a" * 1024, True), ("a" * 1025, False),
])
def test_the_cap_counts_escaped_utf8_bytes_and_the_message_says_bytes(secret, fits):
    """Combined fix check #19: the refusal said '1024 characters' for a cap
    of 1024 escaped UTF-8 bytes, so 342 euro signs were refused as if 1024."""
    assert keychain.secret_fits(secret) is fits
    assert "1024 bytes" in keychain.P12_PASSWORD_TOO_LONG and "characters" not in keychain.P12_PASSWORD_TOO_LONG


# --- Is the login keychain locked? (the owner's try12 round) ------------------


def test_conftest_never_lets_a_test_ask_the_security_framework() -> None:
    assert keychain._lock_probe is not keychain._default_keychain_locked
    assert keychain.login_keychain_locked() is False


@pytest.mark.parametrize(("probe", "locked"), [(True, True), (False, False), (None, False)])
def test_locked_only_when_the_check_says_so(monkeypatch, probe, locked: bool) -> None:
    monkeypatch.setattr(keychain, "_lock_probe", lambda: probe)
    assert keychain.login_keychain_locked() is locked


def test_a_check_that_fails_says_not_known_locked(monkeypatch) -> None:
    def broken():
        raise OSError("no framework")

    monkeypatch.setattr(keychain, "_lock_probe", broken)
    assert keychain.login_keychain_locked() is False


def test_with_the_keychain_switched_off_the_framework_is_never_asked(monkeypatch) -> None:
    """GRAFT_NO_KEYCHAIN=1 (every child the suite starts) keeps the real check
    from running at all."""
    asked: list[int] = []

    def real():
        asked.append(1)
        return True

    monkeypatch.setattr(keychain, "_default_keychain_locked", real)
    monkeypatch.setattr(keychain, "_lock_probe", real)
    monkeypatch.setenv("GRAFT_NO_KEYCHAIN", "1")
    assert keychain.login_keychain_locked() is False
    assert asked == []


def test_the_check_names_symbols_the_frameworks_have() -> None:
    """Looked up only (dlsym), never called: the real check reads the login
    keychain's state, which no test does."""
    import ctypes

    security = ctypes.CDLL(keychain._SECURITY_FRAMEWORK)
    foundation = ctypes.CDLL(keychain._CORE_FOUNDATION)
    for name in ("SecKeychainOpen", "SecKeychainCopyDefault", "SecKeychainGetStatus"):
        assert getattr(security, name)
    assert foundation.CFRelease
    # The real calls' wrapper builds from those alone; none of its calls is made.
    assert isinstance(keychain._KeychainAPI(), keychain._KeychainAPI)


class _FakeSecurityFramework:
    """The four calls the lock check makes, as `keychain._KeychainAPI` has
    them, answering as told and recording what was asked. Never the real
    framework: no test asks it about the real login keychain."""

    def __init__(self, *, opened: int = 0, default: int = 0, status: int = 0, state: int = 1) -> None:
        self.answers = {"open": opened, "default": default, "status": status}
        self.state = state
        self.asked: list[tuple] = []

    def open(self, path):
        self.asked.append(("open", path))
        return self.answers["open"], (None if self.answers["open"] else 101)

    def copy_default(self):
        self.asked.append(("default",))
        return self.answers["default"], (None if self.answers["default"] else 102)

    def get_status(self, ref):
        self.asked.append(("status", ref))
        return self.answers["status"], self.state

    def release(self, ref):
        self.asked.append(("release", ref))


def test_the_login_keychain_is_found_by_the_account_s_home_never_home(monkeypatch, tmp_path) -> None:
    """The owner's try13 run: the opt-in check failed against a really
    locked keychain, because SecKeychainCopyDefault finds the default
    keychain through $HOME, and a test's HOME is a temp folder: it said
    -25307 (no default keychain), and the check "couldn't tell". The login
    keychain is now the account's, from its password-database home."""
    import pwd
    from types import SimpleNamespace

    monkeypatch.setenv("HOME", str(tmp_path / "some-other-home"))
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path / "account")))
    assert keychain.login_keychain_path() == tmp_path / "account/Library/Keychains/login.keychain-db"


@pytest.mark.parametrize(("state", "locked"), [(2, True), (0, True), (1, False), (3, False), (7, False)])
def test_the_login_keychain_file_is_opened_and_its_unlocked_bit_read(tmp_path, state: int, locked: bool) -> None:
    """SecKeychainOpen on the file (it never unlocks or asks), not the
    default keychain; locked unless kSecUnlockStateStatus (1) is set, so a
    readable-only 2 is locked. The reference is released."""
    login = tmp_path / "login.keychain-db"
    login.write_bytes(b"kych")
    api = _FakeSecurityFramework(state=state)
    assert keychain._keychain_locked(api, login) is locked
    assert api.asked == [("open", login), ("status", 101), ("release", 101)]


def test_with_no_login_keychain_file_the_default_one_is_read(tmp_path) -> None:
    api = _FakeSecurityFramework(state=2)
    assert keychain._keychain_locked(api, tmp_path / "missing.keychain-db") is True
    assert api.asked == [("default",), ("status", 102), ("release", 102)]


@pytest.mark.parametrize("fails", ["open", "status"])
def test_a_call_that_fails_is_can_t_tell(tmp_path, fails: str) -> None:
    login = tmp_path / "login.keychain-db"
    login.write_bytes(b"kych")
    api = _FakeSecurityFramework(**{"opened" if fails == "open" else "status": -25294})
    assert keychain._keychain_locked(api, login) is None
    assert ("default",) not in api.asked, "the file is there: never the $HOME-dependent default"
    assert api.asked.count(("release", 101)) == (fails == "status")


def test_no_default_keychain_is_can_t_tell(tmp_path) -> None:
    api = _FakeSecurityFramework(default=-25307)
    assert keychain._keychain_locked(api, tmp_path / "missing.keychain-db") is None
