"""Every external tool graft runs goes through `tools.resolve`, and the
status output says where each one came from."""

from __future__ import annotations

import json
import re
import subprocess
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from graft_ipa import appstore, ipatool, preflight, signer, tools, unfairplay
from graft_ipa.config import AppStoreToolsConfig
from graft_ipa.errors import SigningError
from graft_ipa.json_output import JsonEmitter
from graft_ipa.preflight import CheckResult


def _exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    helpers = tmp_path / "Graft.app" / "Contents" / "Helpers" / "bin"
    helpers.mkdir(parents=True)
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: helpers)
    monkeypatch.setattr(tools, "which", lambda name: None)
    # The App Store tools' defaults live under HOME: keep this Mac's out of it.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return helpers


def test_check_tools_reports_a_bundled_tool_and_its_source(bundle):
    _exe(bundle / "cyan")
    row = preflight.check_tools(["cyan"])[0]
    assert row.ok and row.source == "bundled"
    assert str(bundle / "cyan") in row.detail


def test_ipatool_prefers_the_bundled_copy_unless_the_user_chose_one(tmp_path, bundle):
    _exe(bundle / "ipatool")
    default_cfg = AppStoreToolsConfig.from_dict(None)
    assert ipatool.locate(default_cfg.ipatool, explicit=False) == bundle / "ipatool"
    chosen = _exe(tmp_path / "chosen" / "ipatool")
    assert ipatool.locate(chosen, explicit=True) == chosen


def test_unfairplay_prefers_the_bundled_copy(bundle):
    _exe(bundle / "unfairplay")
    r = unfairplay.resolve_tool(AppStoreToolsConfig.from_dict(None))
    assert (r.path, r.source) == (bundle / "unfairplay", "bundled")


def test_unfairplay_the_user_chose_beats_the_bundled_copy(tmp_path, bundle):
    _exe(bundle / "unfairplay")
    chosen = _exe(tmp_path / "chosen" / "unfairplay")
    r = unfairplay.resolve_tool(AppStoreToolsConfig.from_dict({"unfairplay": str(chosen)}))
    assert (r.path, r.source) == (chosen, "config")


def test_zsign_runs_the_resolved_binary(tmp_path, bundle):
    _exe(bundle / "zsign")
    cmd = signer._zsign_cmd(str(bundle / "zsign"), tmp_path / "in.ipa", tmp_path / "out.ipa",
                            "id.p12", "pw", tmp_path / "p.mobileprovision")
    assert cmd[0] == str(bundle / "zsign")


def test_run_zsign_hands_subprocess_the_resolved_binary(tmp_path, bundle, monkeypatch):
    from tests.test_signer import make_cfg

    _exe(bundle / "zsign")
    cfg = make_cfg(tmp_path)
    monkeypatch.setenv("TEST_P12_PASS", "supersecret")
    ran: list[list[str]] = []

    def zsign(cmd, **_):
        ran.append(list(cmd))
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"signed")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch.object(signer.subprocess, "run", side_effect=zsign):
        signer.run_zsign(cfg, tmp_path / "in.ipa", tmp_path / "out.ipa")
    assert [cmd[0] for cmd in ran] == [str(bundle / "zsign")]


def test_a_bundled_zsign_runs_with_the_bundled_openssl_modules(tmp_path, bundle, monkeypatch):
    _exe(bundle / "zsign")
    modules = bundle.parent.parent / "Frameworks" / "ossl-modules"
    modules.mkdir(parents=True)
    env = signer.zsign_env(tools.resolve("zsign"))
    assert env["OPENSSL_MODULES"] == str(modules)


def test_a_bundled_zsign_with_no_modules_folder_gets_no_override(bundle):
    _exe(bundle / "zsign")
    assert "OPENSSL_MODULES" not in signer.zsign_env(tools.resolve("zsign"))


def test_a_path_zsign_gets_no_openssl_override(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: None)
    monkeypatch.setattr(tools, "which", lambda n: str(_exe(tmp_path / "zsign")))
    assert "OPENSSL_MODULES" not in signer.zsign_env(tools.resolve("zsign"))


def test_run_zsign_hands_a_bundled_zsign_the_openssl_modules(tmp_path, bundle, monkeypatch):
    """The environment `subprocess` was actually given, not only what
    `zsign_env` would say: zsign's OpenSSL reads `OPENSSL_MODULES` from its
    own process, and nowhere else."""
    from tests.test_signer import make_cfg

    _exe(bundle / "zsign")
    modules = bundle.parent.parent / "Frameworks" / "ossl-modules"
    modules.mkdir(parents=True)
    cfg = make_cfg(tmp_path)
    monkeypatch.setenv("TEST_P12_PASS", "supersecret")
    envs: list[dict[str, str]] = []

    def zsign(cmd, *, env, **_):
        envs.append(env)
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"signed")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch.object(signer.subprocess, "run", side_effect=zsign):
        signer.run_zsign(cfg, tmp_path / "in.ipa", tmp_path / "out.ipa")
    assert [e.get("OPENSSL_MODULES") for e in envs] == [str(modules)]
    assert "TEST_P12_PASS" not in envs[0], "still child_env: the password stays out"


def test_a_missing_zsign_is_refused_before_anything_runs(tmp_path, bundle, monkeypatch):
    """Not after the profile is resolved: that can register a Bundle ID and
    spend a provisioning-profile slot in the user's developer account."""
    from tests.test_signer import make_cfg

    cfg = make_cfg(tmp_path)
    monkeypatch.setenv("TEST_P12_PASS", "supersecret")
    monkeypatch.setattr(signer, "_resolve_profile",
                        lambda *a, **k: pytest.fail("the profile was resolved"))
    with patch.object(signer.subprocess, "run",
                      side_effect=lambda *a, **k: pytest.fail("something ran")), \
         pytest.raises(SigningError) as e:
        signer.run_zsign(cfg, tmp_path / "in.ipa", tmp_path / "out.ipa")
    assert e.value.message == "zsign not found in PATH"
    assert "brew install zsign" in (e.value.hint or "")


def test_the_preflight_check_event_carries_source_only_on_rows_that_have_one(temp_config_env):
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import load_config

    rows = [
        CheckResult(name="cyan", ok=True, detail="/x/cyan (bundled)", source="bundled"),
        CheckResult(name=".p12 password", ok=True, detail="(keychain)"),
    ]
    buf = StringIO()
    with patch.object(cli_mod, "run_all_checks", return_value=rows):
        cli_mod._do_check(load_config(temp_config_env), emitter=JsonEmitter(stream=buf))
    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    checks = {e["name"]: e for e in events if e["type"] == "preflight-check"}
    assert checks["cyan"]["source"] == "bundled"
    assert "source" not in checks[".p12 password"]


def test_appstore_status_says_where_each_tool_came_from(bundle):
    _exe(bundle / "ipatool")
    _exe(bundle / "unfairplay")
    payload = appstore.status(AppStoreToolsConfig.from_dict(None))
    assert (payload["ipatool"]["path"], payload["ipatool"]["source"]) == (
        str(bundle / "ipatool"), "bundled")
    assert (payload["unfairplay"]["path"], payload["unfairplay"]["source"]) == (
        str(bundle / "unfairplay"), "bundled")


def test_the_human_status_marks_a_bundled_tool(bundle):
    _exe(bundle / "ipatool")
    _exe(bundle / "unfairplay")
    lines: list[str] = []
    appstore.report_status(SimpleNamespace(appstore=AppStoreToolsConfig.from_dict(None)),
                           emitter=None, say=lines.append)
    [ipa_line] = [line for line in lines if line.startswith("ipatool ")]
    [up_line] = [line for line in lines if line.startswith("UnfairPlay ")]
    assert f"{bundle / 'ipatool'} (bundled)" in ipa_line
    assert f"{bundle / 'unfairplay'} (bundled)" in up_line


def test_the_human_status_does_not_mark_a_tool_found_elsewhere(tmp_path, bundle):
    chosen = _exe(tmp_path / "chosen" / "ipatool")
    lines: list[str] = []
    cfg = AppStoreToolsConfig.from_dict({"ipatool": str(chosen)})
    appstore.report_status(SimpleNamespace(appstore=cfg), emitter=None, say=lines.append)
    [ipa_line] = [line for line in lines if line.startswith("ipatool ")]
    assert str(chosen) in ipa_line and "(bundled)" not in ipa_line


def test_a_get_downloads_and_decrypts_with_the_bundled_tools(bundle, temp_config_env):
    """The whole `--appstore-get` path, with only Graft.app's own copies to
    run: the download, the enrollment check and the decrypt all find them."""
    from graft_ipa.config import load_config
    from tests.appstore_fakes import FakeTools

    fakes = FakeTools(bundle)  # its ipatool and unfairplay land in Helpers/bin
    cfg = load_config(temp_config_env)
    cfg.appstore = AppStoreToolsConfig.from_dict({"unfairplay_models": str(fakes.models)})

    with appstore.fetched(cfg, "example", emitter=None) as ipa:
        assert ipa.is_file()
    assert [c["argv"][0] for c in fakes.calls("ipatool") if c["argv"][:1] == ["download"]]
    assert [c["argv"][0] for c in fakes.calls("unfairplay") if c["argv"][:1] == ["decrypt"]]


def test_an_intel_mac_with_a_ready_bundled_unfairplay_is_not_refused_as_intel(
    bundle, monkeypatch
):
    """The Intel refusal is for a setup that is not ready; readiness is asked
    of the UnfairPlay that would run, here Graft.app's own."""
    from graft_ipa import host
    from tests.appstore_fakes import FakeTools

    monkeypatch.setattr(host, "_sysctl_arm64", lambda: 0)
    fakes = FakeTools(bundle)
    appstore._require_unfairplay(
        AppStoreToolsConfig.from_dict({"unfairplay_models": str(fakes.models)})
    )


# A known-bad ipatool inside Graft.app. The resolver runs the bundled copy
# before ~/.local/bin, so "install 2.8.0 to ~/.local/bin" can never take
# effect: the advice has to name the two things that do.


def _bad_bundled_ipatool(bundle: Path):
    from tests.appstore_fakes import FakeTools

    return FakeTools(bundle).set(ipatool_version="2.8.1")


def _assert_the_bundled_advice(hint: str) -> None:
    assert "Graft.app" in hint and "download Graft again" in hint
    assert "appstore.ipatool" in hint and ipatool.GOOD_VERSION in hint
    assert "~/.local/bin" not in hint


def test_status_for_a_bad_bundled_ipatool_offers_no_install_that_cannot_help(bundle):
    fakes = _bad_bundled_ipatool(bundle)
    payload = appstore.status(
        AppStoreToolsConfig.from_dict({"unfairplay_models": str(fakes.models)})
    )
    ipa = payload["ipatool"]
    assert (ipa["source"], ipa["version"]) == ("bundled", "2.8.1")
    _assert_the_bundled_advice(ipa["warning"])
    assert ipa["install"] is None
    # The warning, as a comment, and none of the install steps.
    assert payload["setup_commands"] == [f"# {ipa['warning']}"]


def test_fetch_refuses_a_bad_bundled_ipatool_with_advice_that_can_help(bundle, temp_config_env):
    from graft_ipa.config import load_config
    from graft_ipa.errors import AppStoreError

    fakes = _bad_bundled_ipatool(bundle)
    cfg = load_config(temp_config_env)
    cfg.appstore = AppStoreToolsConfig.from_dict({"unfairplay_models": str(fakes.models)})
    with pytest.raises(AppStoreError) as e, appstore.fetched(cfg, "example", emitter=None):
        pass
    _assert_the_bundled_advice(e.value.hint or "")
    assert not [c for c in fakes.calls("ipatool") if c["argv"][:1] == ["download"]]


def test_a_bad_ipatool_outside_the_bundle_keeps_the_install_advice(tmp_path, bundle):
    """The contrast: in ~/.local/bin, replacing it is the fix."""
    from tests.appstore_fakes import FakeTools

    default = tmp_path / "home" / ".local" / "bin"
    fakes = FakeTools(default).set(ipatool_version="2.8.1")
    payload = appstore.status(
        AppStoreToolsConfig.from_dict({"unfairplay_models": str(fakes.models)})
    )
    ipa = payload["ipatool"]
    assert (ipa["source"], ipa["path"]) == ("default", str(default / "ipatool"))
    assert "~/.local/bin/ipatool" in ipa["warning"] and "Graft.app" not in ipa["warning"]
    assert ipa["install"] is not None and str(default / "ipatool") in ipa["install"]


# --- review #39: the call sites a resolver mutation slipped past --------------


def _bundled_unfairplay(bundle: Path, tmp_path: Path, monkeypatch) -> AppStoreToolsConfig:
    """A bundled UnfairPlay, nothing at the default path, and no enrollment."""
    from graft_ipa import host

    _exe(bundle / "unfairplay")
    monkeypatch.setattr(host, "apple_silicon", lambda: True)
    models = tmp_path / "models"
    models.mkdir()
    cfg = AppStoreToolsConfig.from_dict({"unfairplay_models": str(models)})
    assert not cfg.unfairplay.exists(), "premise: the default path is empty"
    return cfg


#: `sudo <program>` in anything Graft prints: the program root would run.
_SUDO = re.compile(r"sudo\s+(\S+)")


def _runs_as_root(text: str) -> list[str]:
    """What `text` tells someone to run under sudo, other than /bin/sh on a
    script they saved, which is how the enrollment script runs."""
    return [m for m in _SUDO.findall(text) if m.strip("`'\",.;:()") != "/bin/sh"]


def test_status_never_prints_a_command_that_runs_unfairplay_as_root(bundle, tmp_path, monkeypatch):
    """The critic: status printed `sudo <unfairplay> setup`, which in
    Graft.app ran the bundled helper as root straight out of the app. Only
    Graft's own enrollment remains (`unfairplay.ENROLL_COMMAND`), whose
    script runs root's checked copy."""
    cfg = _bundled_unfairplay(bundle, tmp_path, monkeypatch)

    payload = appstore.status(cfg)

    assert payload["unfairplay"]["enrolled"] is False
    assert unfairplay.ENROLL_COMMAND in payload["setup_commands"]
    for command in payload["setup_commands"]:
        assert not _runs_as_root(command), command
        assert str(bundle / "unfairplay") not in command and str(cfg.unfairplay) not in command


def test_the_not_enrolled_refusal_names_graft_s_enrolment_not_unfairplay(bundle, tmp_path, monkeypatch):
    from graft_ipa.errors import AppStoreError

    cfg = _bundled_unfairplay(bundle, tmp_path, monkeypatch)

    with pytest.raises(AppStoreError, match="isn't enrolled") as e:
        appstore._require_unfairplay(cfg)
    assert unfairplay.ENROLL_COMMAND in (e.value.hint or "")
    assert not _runs_as_root(e.value.hint or "")
    assert str(bundle / "unfairplay") not in (e.value.hint or "")
    assert str(cfg.unfairplay) not in (e.value.hint or "")


def test_status_asks_the_bundled_unfairplay_its_version(bundle, tmp_path, monkeypatch):
    cfg = _bundled_unfairplay(bundle, tmp_path, monkeypatch)
    monkeypatch.setattr(unfairplay, "version", lambda path, **_: f"version of {path}")

    payload = appstore.status(cfg)

    assert payload["unfairplay"]["version"] == f"version of {bundle / 'unfairplay'}"


def test_check_marks_a_bundled_tool_as_bundled(bundle):
    """The README promises "(bundled)"; only the path was ever checked."""
    _exe(bundle / "cyan")
    assert preflight.check_tools(["cyan"])[0].detail == f"{bundle / 'cyan'} (bundled)"


# --- review 2.0, bundle #7: the bundled OpenSSL reads no configuration file ---------------


def test_a_bundled_zsign_reads_no_openssl_configuration(bundle):
    """The bundled libcrypto was configured with OPENSSLDIR
    /opt/graft/openssl/ssl and reads openssl.cnf there at start-up unless
    OPENSSL_CONF says otherwise, and child_env drops the user's. A file put
    there could load providers into the process holding the signing key.
    /dev/null is an empty configuration, which OpenSSL 3.5 accepts."""
    from graft_ipa import child_env

    _exe(bundle / "zsign")
    assert child_env.BUNDLED_OPENSSL_CONF == {"OPENSSL_CONF": "/dev/null"}
    assert signer.zsign_env(tools.resolve("zsign"))["OPENSSL_CONF"] == "/dev/null", "with no modules folder too"
    (bundle.parent.parent / "Frameworks" / "ossl-modules").mkdir(parents=True)
    assert signer.zsign_env(tools.resolve("zsign"))["OPENSSL_CONF"] == "/dev/null"


def test_a_zsign_from_path_keeps_its_own_openssl_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: None)
    monkeypatch.setattr(tools, "which", lambda n: str(_exe(tmp_path / "zsign")))
    assert signer.zsign_env(tools.resolve("zsign")) == {}


def test_run_zsign_hands_the_bundled_zsign_no_openssl_configuration(tmp_path, bundle, monkeypatch):
    from tests.test_signer import make_cfg

    _exe(bundle / "zsign")
    monkeypatch.setenv("OPENSSL_CONF", "/opt/graft/openssl/ssl/openssl.cnf")
    cfg = make_cfg(tmp_path)
    monkeypatch.setenv("TEST_P12_PASS", "supersecret")
    envs: list[dict[str, str]] = []

    def zsign(cmd, *, env, **_):
        envs.append(env)
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"signed")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch.object(signer.subprocess, "run", side_effect=zsign):
        signer.run_zsign(cfg, tmp_path / "in.ipa", tmp_path / "out.ipa")
    assert [e.get("OPENSSL_CONF") for e in envs] == ["/dev/null"]
