"""The `--decrypt-check` key and reasons the Mac app branches on.

The device pane's decrypt card collapses to one line on a phone that cannot be
dumped, and decides that from the check whose `key` is
`CLIContract.DecryptCheck.ssh` — and, since the SSH check fails for fixable
reasons too, from that check's `reason`: only `SSH_NO_SERVER` collapses.
Renaming either here would leave the card wrong with no Swift test able to
notice, so both are pinned from this side against the Swift literals.
"""

from __future__ import annotations

import io
import json
import re
from types import SimpleNamespace
from unittest import mock

import pytest

from graft_ipa import cli, decrypt_ready
from graft_ipa.json_output import JsonEmitter
from tests.cli_ast import contract_swift_source

UDID = "00008030-0000000000000003"


def _swift_ssh_key() -> str:
    match = re.search(
        r"enum DecryptCheck \{.*?static let ssh = \"([^\"\\]*)\"",
        contract_swift_source(),
        re.S,
    )
    assert match, "CLIContract.DecryptCheck.ssh is gone from CLIContract.swift"
    return match.group(1)


def _swift_ssh_reason(name: str) -> str:
    match = re.search(
        r"enum SSHReason \{.*?static let " + name + r" = \"([^\"\\]*)\"",
        contract_swift_source(),
        re.S,
    )
    assert match, f"CLIContract.DecryptCheck.SSHReason.{name} is gone from CLIContract.swift"
    return match.group(1)


def _report(ssh: tuple[bool, str, str | None, str | None]) -> decrypt_ready.Readiness:
    with (
        mock.patch.object(decrypt_ready, "_check_frida", return_value=(True, "reachable", None)),
        mock.patch.object(decrypt_ready, "_check_ssh", return_value=ssh),
        mock.patch.object(decrypt_ready, "_frontmost", return_value=None),
    ):
        return decrypt_ready.check(UDID)


def _ssh(report: decrypt_ready.Readiness) -> decrypt_ready.Check:
    found = [c for c in report.checks if c.key == _swift_ssh_key()]
    assert len(found) == 1, [c.key for c in report.checks]
    return found[0]


def test_the_ssh_check_carries_the_key_the_app_reads() -> None:
    report = _report((False, "no SSH server answered on the device", "fix", decrypt_ready.SSH_NO_SERVER))
    assert _ssh(report).ok is False
    assert report.ready is False, "the state the card collapses on"


def test_the_ssh_reasons_are_the_words_the_app_reads() -> None:
    assert decrypt_ready.SSH_NO_SERVER == _swift_ssh_reason("noServer")
    assert decrypt_ready.SSH_NO_IPROXY == _swift_ssh_reason("noIproxy")
    assert decrypt_ready.SSH_LOGIN_REJECTED == _swift_ssh_reason("loginRejected")
    assert len({decrypt_ready.SSH_NO_SERVER, decrypt_ready.SSH_NO_IPROXY,
                decrypt_ready.SSH_LOGIN_REJECTED}) == 3


# --- `_check_ssh` names each outcome ---------------------------------------


def test_no_iproxy_is_its_own_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decrypt_ready.shutil, "which", lambda _name: None)
    ok, _detail, fix, reason = decrypt_ready._check_ssh(UDID)
    assert (ok, reason) == (False, decrypt_ready.SSH_NO_IPROXY)
    assert fix == "brew install libimobiledevice"


def test_silence_on_port_22_is_no_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decrypt_ready.shutil, "which", lambda _name: "/opt/example/bin/iproxy")
    monkeypatch.setattr(decrypt_ready, "_ssh_banner", lambda _udid: None)
    ok, _detail, _fix, reason = decrypt_ready._check_ssh(UDID)
    assert (ok, reason) == (False, decrypt_ready.SSH_NO_SERVER)


def test_a_refused_password_is_login_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decrypt_ready.shutil, "which", lambda _name: "/opt/example/bin/iproxy")
    monkeypatch.setattr(decrypt_ready, "_ssh_banner", lambda _udid: "SSH-2.0-OpenSSH_9.6")
    monkeypatch.delenv("GRAFT_SSH_KEY", raising=False)
    monkeypatch.setattr(decrypt_ready, "_ssh_auth", lambda _udid: False)
    ok, _detail, fix, reason = decrypt_ready._check_ssh(UDID)
    assert (ok, reason) == (False, decrypt_ready.SSH_LOGIN_REJECTED)
    assert fix is not None and "--set-ssh-password" in fix


@pytest.mark.parametrize("auth", [True, None])
def test_a_passing_ssh_check_has_no_reason(monkeypatch: pytest.MonkeyPatch, auth: bool | None) -> None:
    monkeypatch.setattr(decrypt_ready.shutil, "which", lambda _name: "/opt/example/bin/iproxy")
    monkeypatch.setattr(decrypt_ready, "_ssh_banner", lambda _udid: "SSH-2.0-OpenSSH_9.6")
    monkeypatch.delenv("GRAFT_SSH_KEY", raising=False)
    monkeypatch.setattr(decrypt_ready, "_ssh_auth", lambda _udid: auth)
    ok, _detail, fix, reason = decrypt_ready._check_ssh(UDID)
    assert (ok, fix, reason) == (True, None, None)


# --- `check` keeps the right fix for each ------------------------------------


def test_no_server_gets_the_no_jailbreak_advice() -> None:
    ssh = _ssh(_report((False, "no SSH server answered on the device", "x", decrypt_ready.SSH_NO_SERVER)))
    assert ssh.reason == decrypt_ready.SSH_NO_SERVER
    assert ssh.fix is not None and ssh.fix.startswith("either this phone has no jailbreak")


def test_a_rejected_login_keeps_its_password_fix() -> None:
    """A jailbroken phone with the wrong stored password: telling its owner the
    phone may have no jailbreak sends them away from the one fix that works."""
    fix = "set the phone's root password with `graft --set-ssh-password`"
    ssh = _ssh(_report((False, "SSH-2.0-OpenSSH_9.6, but the login was rejected", fix,
                        decrypt_ready.SSH_LOGIN_REJECTED)))
    assert (ssh.reason, ssh.fix) == (decrypt_ready.SSH_LOGIN_REJECTED, fix)


def test_a_missing_iproxy_keeps_its_install_fix() -> None:
    ssh = _ssh(_report((False, "iproxy is missing", "brew install libimobiledevice",
                        decrypt_ready.SSH_NO_IPROXY)))
    assert (ssh.reason, ssh.fix) == (decrypt_ready.SSH_NO_IPROXY, "brew install libimobiledevice")


def test_only_the_ssh_check_carries_a_reason() -> None:
    report = _report((False, "iproxy is missing", "brew install libimobiledevice",
                      decrypt_ready.SSH_NO_IPROXY))
    assert {c.key: c.reason for c in report.checks if c.key != "ssh"} == {
        "frida": None, "foreground": None,
    }


# --- the event the app decodes ----------------------------------------------


def test_the_event_reports_each_checks_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    report = _report((False, "SSH-2.0-OpenSSH_9.6, but the login was rejected", "fix",
                      decrypt_ready.SSH_LOGIN_REJECTED))
    monkeypatch.setattr(decrypt_ready, "check", lambda _udid, _name=None: report)
    monkeypatch.setattr(
        "graft_ipa.devices.list_devices",
        lambda: SimpleNamespace(devices=[SimpleNamespace(udid=UDID, name="Example Phone")]),
    )
    out = io.StringIO()
    cli._do_decrypt_check(
        SimpleNamespace(default_device_udid=UDID), device=None, emitter=JsonEmitter(stream=out)
    )
    event = json.loads(out.getvalue().splitlines()[-1])
    assert event["type"] == "decrypt-check"
    reasons = {c["key"]: c["reason"] for c in event["checks"]}
    assert reasons["ssh"] == decrypt_ready.SSH_LOGIN_REJECTED
    assert reasons["frida"] is None and "reason" in event["checks"][0]
