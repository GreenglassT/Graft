from __future__ import annotations

import pytest

from graft_ipa import device_install, installer
from graft_ipa.errors import InstallError


def test_install_hands_the_ipa_to_device_install(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(installer.device_install, "install_ipa",
                        lambda ipa, *, udid, on_progress=None: calls.append((ipa, udid)))
    ipa = tmp_path / "a.ipa"
    ipa.write_bytes(b"PK")
    installer.install(ipa, udid="u1")
    assert calls == [(ipa, "u1")]


def test_install_never_looks_for_ideviceinstaller(tmp_path, monkeypatch):
    """Whatever this Mac has installed (review #31): patching `shutil.which`
    alone proved nothing, since `tools.which` deliberately doesn't go
    through it, so a re-added `tools.resolve("ideviceinstaller")` check
    passed wherever Homebrew's ideviceinstaller is on PATH.

    The real install over a faked phone, with PATH a folder of nothing, and
    every lookup graft has recorded: no lookup may even ask for it.
    """
    import shutil

    from graft_ipa import child_env, tools

    from .device_fakes import phone

    asked: list[str] = []
    real_resolve = tools.resolve

    def resolve(name, **kwargs):
        asked.append(name)
        return real_resolve(name, **kwargs)

    def which(name, *a, **k):
        asked.append(name)
        return None

    empty = tmp_path / "bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(tools, "resolve", resolve)
    monkeypatch.setattr(tools, "which", which)
    monkeypatch.setattr(child_env, "which", which)
    monkeypatch.setattr(shutil, "which", which)
    reached = phone(monkeypatch)
    ipa = tmp_path / "a.ipa"
    ipa.write_bytes(b"PK")
    installer.install(ipa, udid="u1")
    assert "ideviceinstaller" not in asked
    assert reached.sent == [("Install", b"PK")]


def test_a_refusal_becomes_an_install_error_with_the_mapped_hint(tmp_path, monkeypatch):
    def refuse(*a, **k):
        raise device_install.DeviceInstallError("ApplicationVerificationFailed: no")
    monkeypatch.setattr(installer.device_install, "install_ipa", refuse)
    ipa = tmp_path / "a.ipa"
    ipa.write_bytes(b"PK")
    with pytest.raises(InstallError) as e:
        installer.install(ipa, udid="u1")
    assert "couldn't verify the signature" in e.value.hint


@pytest.mark.parametrize("output, phrase", [
    ("PasswordRequiredError: ", "unlock"),
    ("NotPairedError: ", "Trust"),
    ("DeviceNotFoundError: u1", "not connected"),
])
def test_connection_failures_get_plain_hints(output, phrase):
    assert phrase in installer._install_hint(output)


def test_dry_run_installs_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.device_install, "install_ipa",
                        lambda *a, **k: pytest.fail("installed during a dry run"))
    r = installer.install(tmp_path / "a.ipa", udid="u1", dry_run=True)
    assert "a.ipa" in r.stdout and "u1" in r.stdout
