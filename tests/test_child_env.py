"""Children get no secrets, and no tool is found through a relative PATH entry.

The `.p12` password (under the name `signing.cert_password_env` gives it) and
`GRAFT_SSH_PASS` used to reach every tool Graft ran — cyan, ipatool, UnfairPlay,
zsign, iproxy, ideviceinstaller — and every process those started. And a tool
started by name, or found with `shutil.which`, could come from a relative PATH
entry: a `zsign` in whatever directory the CLI ran from.
"""

from __future__ import annotations

import ast
import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from graft_ipa import child_env, cyan_runner, installer, ipatool, tools, unfairplay
from graft_ipa.config import AppConfig, Config, CyanOptions, SigningConfig

PACKAGE = Path(__file__).resolve().parent.parent / "graft_ipa"

CERT_VAR = "GRAFT_TEST_CERT_PASSWORD"
CERT_SECRET = "the-p12-password"
SSH_SECRET = "the-root-password"


@pytest.fixture
def secrets_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both secrets in this process's environment, as a shell export puts them."""
    SigningConfig(cert=Path("/x.p12"), cert_password_env=CERT_VAR,
                  profile=Path("/x.mobileprovision"), team_id="")
    monkeypatch.setenv(CERT_VAR, CERT_SECRET)
    monkeypatch.setenv("GRAFT_SSH_PASS", SSH_SECRET)
    monkeypatch.setenv("GRAFT_TEST_HARMLESS", "kept")


def _assert_clean(env: dict[str, str] | None, *, given: frozenset[str] = frozenset()) -> None:
    """No secret anywhere in `env`, except under the names in `given`."""
    assert env is not None, "the child inherited this process's environment whole"
    assert CERT_VAR not in env
    assert "GRAFT_SSH_PASS" not in env
    rest = [v for k, v in env.items() if k not in given]
    assert CERT_SECRET not in rest and SSH_SECRET not in rest
    assert env.get("GRAFT_TEST_HARMLESS") == "kept"


# ---- the helper ----


def test_child_env_drops_both_secrets_and_keeps_the_rest(secrets_set) -> None:
    _assert_clean(child_env.child_env())


def test_a_signing_config_registers_its_own_variable() -> None:
    SigningConfig(cert=Path("/x.p12"), cert_password_env="MY_OWN_P12_VAR",
                  profile=Path("/x.mobileprovision"), team_id="")
    assert "MY_OWN_P12_VAR" in child_env.secret_names()


def test_extra_is_how_a_child_gets_a_credential_on_purpose(secrets_set) -> None:
    env = child_env.child_env({"SSH_PASSWORD": SSH_SECRET})
    assert env["SSH_PASSWORD"] == SSH_SECRET
    assert "GRAFT_SSH_PASS" not in env


def test_the_childs_path_has_only_absolute_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", f"bin{os.pathsep}{os.pathsep}.{os.pathsep}/usr/bin{os.pathsep}./x{os.pathsep}/bin")
    assert child_env.child_env()["PATH"] == f"/usr/bin{os.pathsep}/bin"


def _tool(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / name
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return tool


def test_which_never_finds_a_tool_through_a_relative_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tool(tmp_path / "rel", "graft-test-tool")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", f"rel{os.pathsep}/usr/bin")
    assert child_env.which("graft-test-tool") is None
    # The control: what the plain lookup would have answered.
    import shutil

    assert shutil.which("graft-test-tool") is not None


def test_which_still_finds_a_tool_on_an_absolute_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _tool(tmp_path / "abs", "graft-test-tool")
    monkeypatch.setenv("PATH", f"rel{os.pathsep}{tmp_path / 'abs'}")
    assert child_env.which("graft-test-tool") == str(tool)


def test_ipatool_is_not_located_through_a_relative_path_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ipatool holds the Apple ID's session; one planted in the working
    directory would be handed every download."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _tool(tmp_path / "rel", "ipatool")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", f"rel{os.pathsep}/usr/bin")
    configured = tmp_path / "nowhere" / "ipatool"
    assert ipatool.locate(configured) == configured


def test_a_tool_started_by_name_is_not_found_through_a_relative_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lookup `subprocess` itself does, on the PATH of the env it is given."""
    _tool(tmp_path / "rel", "graft-test-tool")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", f"rel{os.pathsep}/usr/bin")
    with pytest.raises(FileNotFoundError):
        subprocess.run(["graft-test-tool"], env=child_env.child_env(), check=False)


# ---- every subprocess in the package is started with it ----


def _calls_child_env(node: ast.AST) -> bool:
    """`child_env(...)` or `child_env.child_env(...)` somewhere in `node`."""
    return any(
        isinstance(n, ast.Call) and (
            (isinstance(n.func, ast.Name) and n.func.id == "child_env")
            or (isinstance(n.func, ast.Attribute) and n.func.attr == "child_env")
        )
        for n in ast.walk(node)
    )


def _mentions_os_environ(node: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Attribute) and n.attr == "environ"
        and isinstance(n.value, ast.Name) and n.value.id == "os"
        for n in ast.walk(node)
    )


def _env_problem(call: ast.Call) -> str | None:
    """Why this call's environment is not `child_env`'s, or None.

    It used to be enough to pass `env=` at all, so `env=None` (inherit the
    whole environment) and `env=dict(os.environ)` (a copy of it, secrets and
    all) passed the scan: the two mutants that survived. Now `env=` must be
    built by `child_env` — `child_env(...)`, or an expression whose only way
    to an environment goes through it, like `_run_streaming`'s
    `child_env() if env is None else env` — and must not touch `os.environ`.
    `**kwargs` could carry an `env` the scan cannot see, so with one the
    explicit `env=` is required (Python then refuses a second one at run time).
    """
    env = next((k.value for k in call.keywords if k.arg == "env"), None)
    if env is None:
        if any(k.arg is None for k in call.keywords):
            return "passes **kwargs and no explicit env=, which hides env= from this check"
        return "passes no env="
    if _mentions_os_environ(env):
        return "builds env= from os.environ"
    if not _calls_child_env(env):
        return "env= is not built by child_env()"
    return None


def _env_problems_in(source: str, filename: str) -> list[str]:
    """`subprocess.run(...)` / `subprocess.Popen(...)` and friends in `source`,
    and `run(...)` through the injectable runner in ipatool and unfairplay,
    whose environment is not `child_env`'s."""
    problems: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        direct = (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
            and func.attr in {"run", "Popen", "call", "check_call", "check_output"}
        )
        injected = (
            filename in {"ipatool.py", "unfairplay.py"}
            and isinstance(func, ast.Name)
            and func.id == "run"
        )
        if not (direct or injected):
            continue
        why = _env_problem(node)
        if why:
            problems.append(f"{filename}:{node.lineno} {why}")
    return problems


def _subprocess_calls_without_child_env() -> list[str]:
    return [
        problem
        for path in sorted(PACKAGE.glob("*.py"))
        for problem in _env_problems_in(path.read_text(), path.name)
    ]


def test_every_subprocess_in_the_package_gets_child_env() -> None:
    assert _subprocess_calls_without_child_env() == []


@pytest.mark.parametrize(("call", "bad"), [
    ("subprocess.run(cmd, env=child_env())", False),
    ("subprocess.run(cmd, env=child_env.child_env())", False),
    ("subprocess.run(cmd, env=child_env({'SSH_PASSWORD': pw}))", False),
    ("subprocess.Popen(cmd, env=child_env() if env is None else env)", False),
    ("subprocess.run(cmd)", True),
    ("subprocess.run(cmd, env=None)", True),
    ("subprocess.run(cmd, env=os.environ)", True),
    ("subprocess.run(cmd, env=dict(os.environ))", True),
    ("subprocess.run(cmd, env={**os.environ, 'X': '1'})", True),
    ("subprocess.run(cmd, env=env)", True),
    ("subprocess.run(cmd, **kwargs)", True),
    ("subprocess.run(cmd, **kwargs, env=child_env())", False),
])
def test_the_scan_tells_child_env_from_everything_else(call: str, bad: bool) -> None:
    """The scan itself, against the shapes that slipped past it."""
    assert bool(_env_problems_in(call, "x.py")) is bad


# ---- the wrappers, end to end ----


def _capturing_run(seen: dict, **result):
    def run(cmd, **kwargs):
        seen.setdefault("envs", []).append(kwargs.get("env"))
        seen.setdefault("cmds", []).append(list(cmd))
        return subprocess.CompletedProcess(cmd, result.get("returncode", 0),
                                           stdout=result.get("stdout", ""),
                                           stderr=result.get("stderr", ""))
    return run


def test_cyan_gets_no_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secrets_set) -> None:
    from .tool_fakes import fake_path

    monkeypatch.setattr(tools, "which", fake_path(tmp_path / "path-bin"))
    cfg = Config(path=tmp_path / "c.yaml", signing=None, cache_dir=tmp_path,
                 built_dir=tmp_path, tweaks_dir=tmp_path, state_dir=tmp_path, apps={})
    app = AppConfig(name="example", bundle_id="com.example.app", preferred_version="latest",
                    tweaks=[], cyan=CyanOptions())
    seen: dict = {}

    def run(cmd, **kwargs):
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"ipa")
        return _capturing_run(seen)(cmd, **kwargs)

    with patch.object(cyan_runner.subprocess, "run", side_effect=run):
        cyan_runner.run_cyan(cfg, app, tmp_path / "in.ipa", tmp_path / "out.ipa")
    _assert_clean(seen["envs"][0])


def test_zsign_gets_no_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secrets_set
) -> None:
    """The real `run_zsign`, with `subprocess.run` replaced underneath it.

    zsign gets a re-encrypted copy of the identity and its one-time password
    on the command line (`p12`), and no business with the real one in the
    environment: the mutant that handed it `dict(os.environ)` survived, since
    no test looked at zsign's environment at all."""
    from graft_ipa import signer

    from .test_signer import make_cfg
    from .tool_fakes import fake_path

    monkeypatch.setattr(tools, "which", fake_path(tmp_path / "path-bin"))
    cfg = make_cfg(tmp_path)
    from .test_lc_sign import _make_p12

    cfg.signing.cert, _ = _make_p12(tmp_path, CERT_SECRET)
    cfg.signing.cert_password_env = CERT_VAR
    seen: dict = {}

    def zsign(cmd, **kwargs):
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"signed")
        return _capturing_run(seen)(cmd, **kwargs)

    with patch.object(signer.subprocess, "run", side_effect=zsign):
        signer.run_zsign(cfg, tmp_path / "in.ipa", tmp_path / "out.ipa")

    assert seen["cmds"][0][0] == str(tmp_path / "path-bin" / "zsign")
    _assert_clean(seen["envs"][0])


def test_the_lc_sign_runner_gets_no_secrets(secrets_set) -> None:
    """`lc_sign.Runner` runs codesign and security for `--lc-install`. The
    mutant that gave `run` `env=None` survived, as nothing looked."""
    from graft_ipa import lc_sign

    seen: dict = {}

    with patch.object(lc_sign.subprocess, "run", side_effect=_capturing_run(seen)):
        lc_sign.Runner().run(["security", "list-keychains", "-d", "user"])

    assert len(seen["envs"]) == 1
    _assert_clean(seen["envs"][0])


def test_a_sideload_install_starts_no_child_process_at_all(
    tmp_path: Path, secrets_set, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It used to run `ideviceinstaller`, which had to be kept from the
    secrets. It now installs in-process (`device_install`), so there is no
    child to leak them to: any process spawn fails this test.

    The real install, over a phone faked at pymobiledevice3's boundary
    (`tests/device_fakes.py`): with `device_install.install_ipa` itself mocked
    out, a shell-out added there passed (review #28). Every way to start a
    process is a tripwire, including a name bound before the patch, which the
    audit hook in `tests/tripwires.py` catches (re-check #35).
    """
    from .device_fakes import phone
    from .tripwires import forbid_child_processes

    reached = phone(monkeypatch)
    forbid_child_processes(monkeypatch, "a sideload install")
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"PK")
    installer.install(ipa, udid="UDID-EXAMPLE")
    assert reached.serials == ["UDID-EXAMPLE"]
    assert reached.sent == [("Install", b"PK")]


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_ipatool_gets_no_secrets(tmp_path: Path, secrets_set) -> None:
    tool = _executable(tmp_path / "ipatool")
    seen: dict = {}
    ipatool.version(tool, run=_capturing_run(seen, stdout="version 2.8.0"))
    ipatool.signed_in(tool, run=_capturing_run(seen, stdout='{"success": true}'))
    out = tmp_path / "dl"
    out.mkdir()
    (out / "x.ipa").write_bytes(b"ipa")
    ipatool.download(tool, "com.example.app", out, run=_capturing_run(
        seen, stdout=f'{{"success": true, "output": "{out / "x.ipa"}"}}'))
    assert len(seen["envs"]) == 3
    for env in seen["envs"]:
        _assert_clean(env)


def test_unfairplay_gets_no_secrets(tmp_path: Path, secrets_set) -> None:
    tool = _executable(tmp_path / "unfairplay")
    seen: dict = {}
    unfairplay.version(tool, run=_capturing_run(seen, stdout="version v0.4.0"))
    out = tmp_path / "out.ipa"

    def decrypt_run(cmd, **kwargs):
        out.write_bytes(b"ipa")
        return _capturing_run(seen, stdout=(
            '{"EncryptedImages": 1, "DecryptedPages": 2, "PlaintextVerified": true}'
        ))(cmd, **kwargs)

    unfairplay.decrypt(tool, input_ipa=tmp_path / "in.ipa", output_ipa=out,
                       report=tmp_path / "r.json", model_dir=tmp_path, run=decrypt_run)
    assert len(seen["envs"]) == 2
    for env in seen["envs"]:
        _assert_clean(env)


def test_the_keychain_tool_gets_no_secrets(monkeypatch: pytest.MonkeyPatch, secrets_set) -> None:
    """The real `_run_security`, with `subprocess.run` replaced underneath it,
    so nothing reaches a keychain."""
    from graft_ipa import keychain

    monkeypatch.delenv("GRAFT_NO_KEYCHAIN")
    seen: dict = {}
    with patch.object(keychain.subprocess, "run", side_effect=_capturing_run(seen)):
        keychain._run_security(["security", "find-generic-password", "-s", "x"],
                               capture_output=True)
    _assert_clean(seen["envs"][0])


def test_bagbak_gets_ssh_password_and_nothing_else_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, secrets_set
) -> None:
    from graft_ipa import decrypt_runner

    monkeypatch.setattr(decrypt_runner, "bagbak_available", lambda: "/opt/fake/bagbak")
    monkeypatch.setattr(decrypt_runner, "bagbak_major", lambda: 4)
    seen: dict = {}

    def fake_streaming(cmd, *, cwd=None, env=None, on_line=None):
        seen["env"] = env
        (Path(cwd) / "app.ipa").write_bytes(b"PK\x03\x04")
        return 0, ""

    monkeypatch.setattr(decrypt_runner, "_run_streaming", fake_streaming)
    decrypt_runner.decrypt_app("com.example.app", tmp_path / "dump", udid="UDID-EXAMPLE")
    _assert_clean(seen["env"], given=frozenset({"SSH_PASSWORD"}))
    # Deliberately, under bagbak's own name: 4.x pulls over SSH.
    assert seen["env"]["SSH_PASSWORD"] == SSH_SECRET


def test_iproxy_gets_no_secrets(monkeypatch: pytest.MonkeyPatch, secrets_set) -> None:
    from graft_ipa import decrypt_runner

    seen: dict = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            seen["env"] = kwargs.get("env")

        def poll(self):
            return 1

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(decrypt_runner.shutil, "which", lambda name: "/opt/fake/iproxy")
    monkeypatch.setattr(decrypt_runner.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(decrypt_runner, "_wait_for_port", lambda port: False)
    decrypt_runner._with_tunnel("UDID-EXAMPLE", lambda port: True)
    _assert_clean(seen["env"])


def _assert_private(where: Path) -> None:
    import os
    import stat

    from .conftest import SUN_PATH_MAX

    # What defeats a planted socket: a directory only this user can enter,
    # made fresh. Not where it is: /tmp is fine for an 0700 mkdtemp directory
    # (re-check #17/#23), and the old fixed /tmp path fails this anyway,
    # /tmp being root's and 1777.
    mode = where.parent.stat()
    assert mode.st_uid == os.getuid() and stat.S_IMODE(mode.st_mode) == 0o700
    assert not where.exists(), "something is listening where no usbmuxd should be"
    assert len(str(where).encode()) < SUN_PATH_MAX, "past the AF_UNIX limit it fails differently"


def test_the_no_usbmuxd_guard_is_not_a_shared_tmp_path() -> None:
    """Review #25: a fixed name in world-writable /tmp held only while no
    other account had a socket there."""
    import os

    from .conftest import NO_USBMUXD

    _assert_private(Path(NO_USBMUXD))
    assert NO_USBMUXD != "/tmp/graft-no-usbmuxd.sock"
    assert os.environ["USBMUXD_SOCKET_ADDRESS"] == NO_USBMUXD


def test_a_long_temp_folder_still_gives_a_usable_socket_path(tmp_path: Path) -> None:
    """Re-check #18: under a long TMPDIR the path went past 104 bytes and
    pymobiledevice3 failed with "AF_UNIX path too long", not "no usbmuxd"."""
    from .conftest import SUN_PATH_MAX, private_socket_path

    long_base = tmp_path / ("t" * 60) / ("u" * 60)
    long_base.mkdir(parents=True)

    where = Path(private_socket_path(str(long_base)))

    assert len(str(long_base).encode()) > SUN_PATH_MAX
    assert not where.is_relative_to(long_base)
    _assert_private(where)


def test_a_short_temp_folder_is_used_as_it_is() -> None:
    """With TMPDIR unset Python falls back to /tmp, and that is fine."""
    from .conftest import private_socket_path

    where = Path(private_socket_path("/tmp"))

    assert where.parent.parent == Path("/tmp")
    _assert_private(where)


def test_no_helper_spells_the_old_shared_socket_path() -> None:
    """Fix-check #8: scripts/verify-bundle used the shared path too."""
    tests = Path(__file__).parent
    scripts = tests.parent / "scripts"
    offenders = [
        p.name for p in [*tests.glob("*.py"), *(f for f in scripts.iterdir() if f.is_file())]
        if p.name != Path(__file__).name and "/tmp/graft-no-usbmuxd.sock" in p.read_text(errors="replace")
    ]
    assert offenders == []


# ---- Graft.app's own CLI: the login shell's environment, tamed ----

HOSTILE = {
    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin", "OPENSSL_CONF": "/opt/homebrew/etc/openssl@3/x.cnf",
    "OPENSSL_ENGINES": "/junk/engines", "SSL_CERT_FILE": "/junk/cert.pem", "SSL_CERT_DIR": "/junk/certs",
    "REQUESTS_CA_BUNDLE": "/junk/ca.pem", "CURL_CA_BUNDLE": "/junk/curl.pem", "HTTPS_PROXY": "http://proxy:3128",
    "https_proxy": "http://proxy:3128", "NO_PROXY": "localhost",
}


def _bundled(monkeypatch, tmp_path, env: dict) -> Path:
    from graft_ipa import tools
    helpers = tmp_path / "Graft.app" / "Contents" / "Helpers" / "bin"
    helpers.mkdir(parents=True)
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: helpers)
    for k in [*HOSTILE, "PATH"]:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return helpers


def test_graft_apps_children_get_its_tools_then_the_systems_and_no_tls_overrides(monkeypatch, tmp_path):
    """Decision 8 (critic gap): the app hands the CLI the login shell's
    environment. Homebrew's zip, unzip, tar, codesign or install_name_tool
    must never stand in for the system's, and an OPENSSL_CONF, or a CA bundle
    that names nothing (every HOSTILE one is a missing file), must not reach
    the bundled zsign, ipatool or Python. Proxies are the user's to set and
    stay."""
    helpers = _bundled(monkeypatch, tmp_path, HOSTILE)
    env = child_env.child_env({"TMPDIR": "/x/"})
    assert env["PATH"] == f"{helpers}:/usr/bin:/bin:/usr/sbin:/sbin"
    for k in child_env.TLS_OVERRIDES:
        assert k not in env, k
    assert set(child_env.TLS_OVERRIDES) == {"OPENSSL_CONF", "OPENSSL_ENGINES", "SSL_CERT_FILE", "SSL_CERT_DIR",
                                            "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"}
    assert (env["HTTPS_PROXY"], env["https_proxy"], env["NO_PROXY"], env["TMPDIR"]) == (
        "http://proxy:3128", "http://proxy:3128", "localhost", "/x/")


def test_a_user_installed_tool_brings_its_own_folder_and_nothing_else(monkeypatch, tmp_path):
    """bagbak is `#!/usr/bin/env node`: its folder (npm's, beside node) goes on
    its PATH, after Graft.app's tools and before the system's."""
    helpers = _bundled(monkeypatch, tmp_path, HOSTILE)
    env = child_env.child_env(path_also=[Path("/opt/homebrew/bin"), Path("relative/bin")])
    assert env["PATH"] == f"{helpers}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def test_finding_a_user_tool_still_searches_the_users_path(monkeypatch, tmp_path):
    _bundled(monkeypatch, tmp_path, {"PATH": f"{tmp_path / 'brew'}:/usr/bin:/bin"})
    (tmp_path / "brew").mkdir()
    (tmp_path / "brew" / "bagbak").write_text("#!/bin/sh\n")
    (tmp_path / "brew" / "bagbak").chmod(0o755)
    assert child_env.which("bagbak") == str(tmp_path / "brew" / "bagbak")


def test_the_bundled_cli_drops_the_tls_overrides_from_its_own_process(monkeypatch, tmp_path):
    """The CLI's own requests and ssl read SSL_CERT_FILE and REQUESTS_CA_BUNDLE too."""
    _bundled(monkeypatch, tmp_path, HOSTILE)
    child_env.tame_bundled_process()
    for k in child_env.TLS_OVERRIDES:
        assert k not in os.environ, k
    assert os.environ["HTTPS_PROXY"] == "http://proxy:3128"


def _ca_files(tmp_path: Path) -> dict[str, Path]:
    ca = tmp_path / "corp" / "corp-ca.pem"
    ca.parent.mkdir(parents=True, exist_ok=True)
    ca.write_text("-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n")
    certs = tmp_path / "corp" / "certs"
    certs.mkdir(exist_ok=True)
    return {"file": ca, "dir": certs, "missing": tmp_path / "corp" / "gone.pem"}


@pytest.mark.parametrize("tame", [False, True], ids=["child_env", "tame_bundled_process"])
def test_a_ca_override_naming_a_real_file_or_folder_is_kept_in_graft_app(monkeypatch, tmp_path, tame):
    """Combined fix check #13: behind a TLS-inspecting proxy a CA bundle is
    the only way to trust its root; the bundled Python never reads the
    keychain. A readable file (or, for SSL_CERT_DIR, a folder) is kept."""
    ca = _ca_files(tmp_path)
    _bundled(monkeypatch, tmp_path, {**HOSTILE, "REQUESTS_CA_BUNDLE": str(ca["file"]),
                                     "SSL_CERT_FILE": str(ca["file"]), "CURL_CA_BUNDLE": str(ca["file"]),
                                     "SSL_CERT_DIR": str(ca["dir"])})
    if tame:
        child_env.tame_bundled_process()
        env = dict(os.environ)
    else:
        env = child_env.child_env()
    assert {k: env.get(k) for k in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE", "SSL_CERT_DIR")} == {
        "REQUESTS_CA_BUNDLE": str(ca["file"]), "SSL_CERT_FILE": str(ca["file"]),
        "CURL_CA_BUNDLE": str(ca["file"]), "SSL_CERT_DIR": str(ca["dir"])}
    assert "OPENSSL_CONF" not in env and "OPENSSL_ENGINES" not in env, "never kept"


@pytest.mark.parametrize("tame", [False, True], ids=["child_env", "tame_bundled_process"])
@pytest.mark.parametrize("what", ["missing", "a-directory", "relative", "empty", "unreadable"])
def test_a_ca_file_override_naming_no_usable_file_is_dropped(monkeypatch, tmp_path, tame, what):
    ca = _ca_files(tmp_path)
    if what == "unreadable":
        ca["file"].chmod(0)
        if os.access(ca["file"], os.R_OK):
            pytest.skip("running as a user who can read any file")
    value = {"missing": str(ca["missing"]), "a-directory": str(ca["dir"]), "relative": "corp/corp-ca.pem",
             "empty": "", "unreadable": str(ca["file"])}[what]
    monkeypatch.chdir(tmp_path)  # so the relative one names a real file
    _bundled(monkeypatch, tmp_path, {**HOSTILE, "REQUESTS_CA_BUNDLE": value, "SSL_CERT_FILE": value,
                                     "CURL_CA_BUNDLE": value})
    try:
        if tame:
            child_env.tame_bundled_process()
            env = dict(os.environ)
        else:
            env = child_env.child_env()
    finally:
        ca["file"].chmod(0o644)
    for k in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE"):
        assert k not in env, (k, what)


@pytest.mark.parametrize("what", ["missing", "a-file", "relative"])
def test_ssl_cert_dir_naming_no_folder_is_dropped(monkeypatch, tmp_path, what):
    ca = _ca_files(tmp_path)
    monkeypatch.chdir(tmp_path)
    value = {"missing": str(tmp_path / "nowhere"), "a-file": str(ca["file"]), "relative": "corp/certs"}[what]
    _bundled(monkeypatch, tmp_path, {**HOSTILE, "SSL_CERT_DIR": value})
    assert "SSL_CERT_DIR" not in child_env.child_env()


def test_openssl_settings_are_dropped_even_when_they_name_real_files(monkeypatch, tmp_path):
    ca = _ca_files(tmp_path)
    _bundled(monkeypatch, tmp_path, {**HOSTILE, "OPENSSL_CONF": str(ca["file"]), "OPENSSL_ENGINES": str(ca["dir"])})
    env = child_env.child_env()
    assert "OPENSSL_CONF" not in env and "OPENSSL_ENGINES" not in env


def test_outside_graft_app_nothing_changes(monkeypatch, tmp_path):
    from graft_ipa import tools
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: None)
    for k, v in HOSTILE.items():
        monkeypatch.setenv(k, v)
    env = child_env.child_env()
    assert env["PATH"] == HOSTILE["PATH"] and env["SSL_CERT_FILE"] == "/junk/cert.pem"
    child_env.tame_bundled_process()
    assert os.environ["SSL_CERT_FILE"] == "/junk/cert.pem"


# --- upgrade-3 = cli-13: bagbak under a custom npm prefix finds the user's node ----


def _npm_prefix_bagbak(monkeypatch, tmp_path) -> tuple[Path, Path]:
    """Graft.app's CLI; bagbak in a custom npm prefix (`~/.npm-global`), as
    `#!/usr/bin/env node`; node elsewhere on the user's login PATH (say
    /opt/homebrew/bin). Both are stubs: the `node` stub prints a 4.x
    version, and is the only `node` on any PATH here."""
    prefix, node_bin = tmp_path / "npm-global" / "bin", tmp_path / "node-bin"
    prefix.mkdir(parents=True)
    node_bin.mkdir()
    bagbak = prefix / "bagbak"
    bagbak.write_text("#!/usr/bin/env node\nconsole.log('never run: the stub node prints the version')\n")
    bagbak.chmod(0o755)
    (node_bin / "node").write_text("#!/bin/sh\necho 4.0.3\n")
    (node_bin / "node").chmod(0o755)
    _bundled(monkeypatch, tmp_path, {"PATH": f"{prefix}:{node_bin}:/usr/bin:/bin"})
    return bagbak, node_bin


def test_bagbak_s_version_is_read_with_the_user_s_node_on_its_path(monkeypatch, tmp_path):
    from graft_ipa import decrypt_runner

    bagbak, _ = _npm_prefix_bagbak(monkeypatch, tmp_path)
    monkeypatch.setattr(decrypt_runner.bagbak_major, "__defaults__", ({},))
    assert decrypt_runner.bagbak_available() == str(bagbak)
    assert decrypt_runner.bagbak_major() == 4, "exit 127 from env, and 5.x's argv for a 4.x bagbak"


def test_a_bagbak_dump_has_the_user_s_node_on_its_path(monkeypatch, tmp_path):
    from graft_ipa import decrypt_runner

    bagbak, node_bin = _npm_prefix_bagbak(monkeypatch, tmp_path)
    monkeypatch.setattr(decrypt_runner, "bagbak_major", lambda: 4)
    seen: dict = {}

    def fake_streaming(cmd, *, cwd=None, env=None, on_line=None):
        seen["env"] = env
        (Path(cwd) / "app.ipa").write_bytes(b"PK\x03\x04")
        return 0, ""

    monkeypatch.setattr(decrypt_runner, "_run_streaming", fake_streaming)
    decrypt_runner.decrypt_app("com.example.app", tmp_path / "dump", udid="UDID-EXAMPLE")
    path = seen["env"]["PATH"].split(":")
    assert str(bagbak.parent) in path and str(node_bin) in path
    assert path.index(str(node_bin)) < path.index("/usr/bin")
