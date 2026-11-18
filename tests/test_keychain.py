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


def test_an_empty_secret_is_none(security: FakeSecurity) -> None:
    security.items[("graft", "ssh-UDID1")] = ""
    security.items[("tweak-build", "ssh-UDID1")] = "old"
    assert keychain.read_ssh_password("UDID1") is None
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
