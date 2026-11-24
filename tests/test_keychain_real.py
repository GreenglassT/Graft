"""Graft's own `security -i` line, through the real `/usr/bin/security`, into
a throwaway keychain file, and read back with `find-generic-password`.

Everything else in the suite talks to `FakeSecurity`, which only models the
quoting it was told about. This is the one place the real parser sees what
`keychain._write` builds: quotes, backslashes, UTF-8, a blank password, and
a password carrying a `security` command, at the 1024-byte cap.

**Opt-in only**: skipped unless `GRAFT_REAL_KEYCHAIN_TESTS=1`, so it is in
neither the default suite nor verify-bundle. Run it with the owner watching:

    GRAFT_REAL_KEYCHAIN_TESTS=1 .venv/bin/python -m pytest -p no:cacheprovider -m real_keychain tests/test_keychain_real.py tests/test_account_setup.py

(the marker also takes in `test_account_setup.py`'s Keychain Access export).
The program guard lets these tests, and only these, start `security`.

No GUI prompt, which these tests enforce rather than assume:

- the scratch keychain is created under `tmp_path` with a known random
  password, then unlocked with it and given settings with no lock timeout and
  no lock on sleep (`set-keychain-settings` without `-l` or `-u`), so nothing
  asks for its password;
- every `security` call names that keychain file, and it is deleted in the
  fixture's teardown whatever happens; it is never added to the search list
  and `default-keychain`, `list-keychains` and `login-keychain` are never run;
- `_write` is reached through a guard that fails the test unless each call
  names that keychain, so nothing can go to the default (login) one;
- nothing changes an item's access list: items are created with `-A` and
  updated with `-U` alone (`keychain._write`), since `-U -A` on an existing
  item asks for the keychain password (the dialog an earlier version of this
  file raised); secrets are read back with `find-generic-password -w` only.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
from pathlib import Path

import pytest

from graft_ipa import keychain

SECURITY = "/usr/bin/security"

pytestmark = [
    # The program guard lets these start `security` (and nothing else on its
    # list), with GRAFT_REAL_KEYCHAIN_TESTS=1 as well (tests/program_guard.py).
    pytest.mark.real_keychain,
    pytest.mark.skipif(
        os.environ.get("GRAFT_REAL_KEYCHAIN_TESTS") != "1",
        reason="opt-in: set GRAFT_REAL_KEYCHAIN_TESTS=1, with the owner watching",
    ),
    pytest.mark.skipif(not os.access(SECURITY, os.X_OK), reason="needs /usr/bin/security"),
]

#: Subcommands that change which keychains the user has. Never run here.
_NEVER = ("default-keychain", "list-keychains", "login-keychain")


def _security(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    assert not set(args) & set(_NEVER), args
    return subprocess.run([SECURITY, *args], capture_output=True, timeout=60, check=check)


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`(keychain path, what each _write got back)`. The keychain is a file
    under `tmp_path`, unlocked with its known password and set never to lock,
    deleted at the end whatever happens."""
    path = str(tmp_path / "graft-test.keychain")
    password = secrets.token_hex(16)
    answers: list[subprocess.CompletedProcess] = []

    def only_the_scratch_keychain(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        if argv[1:] == ["-i"]:
            line = kwargs["input"].decode()
            assert line.count("\n") == 1 and line.startswith("add-generic-password "), line[:80]
            assert line.endswith(f" {keychain._quote(path)}\n"), "would write to another keychain"
            # The flags, with every quoted value blanked first: the secret at
            # the cap carries " -U -A" itself, which tripped a check of the
            # whole line (try15 owner round). Not a check of where `_write`
            # puts its flag, which a -U anywhere else would get past (the
            # shape of 1.1's -U -A bug), and not shlex, which reads a secret
            # that is just "-U" as a flag.
            bare = re.sub(r'"(?:[^"\\]|\\.)*"', '""', line)
            assert not (" -U " in bare and " -A " in bare), "would change an access list"
            proc = subprocess.run([SECURITY, "-i"], **kwargs)
            answers.append(proc)
            return proc
        # `_write`'s existence check: attributes only, never the secret.
        if argv[1] == "find-generic-password" and argv[-1] == path and not {"-g", "-w"} & set(argv):
            return subprocess.run([SECURITY, *argv[1:]], **kwargs)
        pytest.fail(f"unexpected security call: {argv}")

    try:
        _security("create-keychain", "-p", password, path)
        _security("unlock-keychain", "-p", password, path)
        _security("set-keychain-settings", path)  # no -l, no -u: it never locks
        monkeypatch.setattr(keychain, "_run", only_the_scratch_keychain)
        yield path, answers
    finally:
        _security("delete-keychain", path, check=False)
        assert not Path(path).exists() and not Path(path + "-db").exists()


def _read_back(path: str, account: str) -> tuple[str, str | None]:
    """The secret, by `find-generic-password -w`, which prints it as text or,
    for anything it won't print as is (non-ASCII, a control character), as
    hex; both are taken."""
    w = _security("find-generic-password", "-s", keychain.SERVICE, "-a", account, "-w", path)
    shown = w.stdout.decode().rstrip("\n")
    try:
        unhexed = bytes.fromhex(shown).decode("utf-8")
    except ValueError:
        unhexed = None
    return shown, unhexed


def _round_trip(scratch, secret: str, account: str = "p12-ABCDE12345") -> None:
    path, answers = scratch
    keychain._write(account, secret, keychain_path=path)
    assert (answers[-1].stdout, answers[-1].stderr) == (b"", b""), "security said something"
    assert secret in _read_back(path, account)


def test_a_password_at_the_cap_carrying_a_command_round_trips_and_runs_nothing(
    scratch, tmp_path: Path
) -> None:
    """1024 bytes escaped, the most `_write` takes: double quotes, single
    quotes, backslashes (a trailing one too), `$()`, backticks, UTF-8, and
    a `show-keychain-info` that would print an error if it ever ran as its
    own command. It is stored exactly, and `security` says nothing."""
    canary = tmp_path / "never-created.keychain"
    core = (
        f'p"q\\r é $(id) `id` \' ; show-keychain-info {canary} '
        f'" -U -A\tshow-keychain-info "{canary}" '
    )
    pad = keychain.MAX_SECRET_BYTES - len(keychain._escape(core).encode()) - 2
    secret = core + "x" * pad + "\\"
    assert len(keychain._escape(secret).encode()) == keychain.MAX_SECRET_BYTES

    _round_trip(scratch, secret)

    assert not canary.exists()


@pytest.mark.parametrize("secret", [
    "", "x" * 1024, "pässwörd with spaces ", "日本語🔑", 'dq"in"side', "back\\slash\\",
], ids=["blank", "1024 plain", "utf-8 spaces", "no ascii", "quotes", "backslashes"])
def test_secrets_round_trip_through_the_real_parser(scratch, secret: str) -> None:
    _round_trip(scratch, secret)


def test_an_upsert_puts_back_what_was_there(scratch) -> None:
    """What a failed setup's undo does: write the kept secret over the new one."""
    for secret in ("the old one", "the new one", "the old one"):
        keychain._write("p12-ABCDE12345", secret, keychain_path=scratch[0])
    assert "the old one" in _read_back(scratch[0], "p12-ABCDE12345")
    assert all(a.returncode == 0 and a.stderr == b"" for a in scratch[1])


def test_the_split_is_real_so_the_check_above_could_see_it(scratch, tmp_path: Path) -> None:
    """The control for the first test: past 4095 bytes `security -i` really
    does run the rest as a command, and says so on stderr. Both commands only
    look for keychains that don't exist; neither is ever `add-generic-password`,
    which would go to the login keychain."""
    absent, canary = tmp_path / "absent.keychain", tmp_path / "canary.keychain"
    first = f'show-keychain-info "{absent}"'.encode()
    line = first + b" " * (4095 - len(first)) + f'show-keychain-info "{canary}"\n'.encode()

    proc = subprocess.run([SECURITY, "-i"], input=line, capture_output=True, timeout=60)

    assert str(canary).encode() in proc.stderr, "the overflow ran as its own command"
    assert not canary.exists()


def test_write_refuses_what_would_split_before_security_runs(scratch) -> None:
    path, answers = scratch
    with pytest.raises(subprocess.CalledProcessError):
        keychain._write("p12-ABCDE12345", "x" * 4096 + " show-keychain-info /nowhere", keychain_path=path)
    assert answers == []


def test_the_lock_check_reads_the_login_keychain_s_state_without_asking() -> None:
    """`keychain._default_keychain_locked`, the up-front check for a locked
    login keychain, against the real one: it opens the account's login
    keychain (SecKeychainOpen) and only reads whether it is locked
    (SecKeychainGetStatus), and must show nothing. Run it unlocked, then
    again after `security lock-keychain ~/Library/Keychains/login.keychain-db`
    with `GRAFT_EXPECT_LOGIN_LOCKED=1` (no dialog may appear either way;
    unlock the keychain afterward).

    On try13 this failed against a really locked keychain: the check went
    through $HOME, which conftest points at a temp folder, so it found no
    keychain at all and answered None. The path it reads is the account's,
    whatever HOME says, and it must be the file there.

    Since the try14 delta audit's kc-path the check also reads the default
    keychain (SecKeychainCopyDefault), through $HOME. Under this test's
    temp HOME there is none (-25307), so the login keychain's answer is
    the one checked here. That a custom default keychain, locked, reads as
    locked under the real HOME is a hands-on check, not a test."""
    import pwd
    from pathlib import Path

    login = keychain.login_keychain_path()
    assert login == Path(pwd.getpwuid(os.getuid()).pw_dir) / "Library" / "Keychains" / "login.keychain-db"
    assert login.is_file(), "the real login keychain, not a fallback"
    assert not login.is_relative_to(Path(os.environ["HOME"])), "not the test's temp HOME"
    found = keychain._default_keychain_locked()
    assert found is (os.environ.get("GRAFT_EXPECT_LOGIN_LOCKED") == "1")
