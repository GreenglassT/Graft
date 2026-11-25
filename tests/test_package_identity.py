"""What the package calls itself: the command, the module entry point, the
User-Agent, and the one name it deliberately does *not* rename."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from graft_ipa import USER_AGENT, __version__, apple_portal, canister, itunes, repo
from graft_ipa.config import load_config

from .conftest import REPO_ROOT


def test_python_dash_m_runs_the_cli_as_graft(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    version = subprocess.run(
        [sys.executable, "-m", "graft_ipa", "--version"],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )
    assert version.returncode == 0, version.stderr
    assert version.stdout.strip() == f"graft {__version__}"

    # The usage line names the command a user can type, not `__main__.py`.
    usage = subprocess.run(
        [sys.executable, "-m", "graft_ipa", "--help"],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )
    assert usage.returncode == 0, usage.stderr
    assert "Usage: graft " in usage.stdout


def test_every_http_client_sends_the_same_user_agent() -> None:
    assert USER_AGENT == f"graft/{__version__} (+https://github.com/GreenglassT/Graft)"
    for module in (apple_portal, canister, itunes, repo):
        assert module.USER_AGENT is USER_AGENT, module.__name__


def test_a_config_naming_the_old_password_variable_keeps_it(tmp_path: Path) -> None:
    """`cert_password_env` is the user's own choice of variable, so the rename
    must not touch it: a config written before it says `TWEAK_BUILD_P12_PASS`,
    and that shell export is what holds the password."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "signing:\n"
        f"  cert: {tmp_path / 'dev.p12'}\n"
        "  cert_password_env: TWEAK_BUILD_P12_PASS\n"
        f"  profile: {tmp_path / 'wildcard.mobileprovision'}\n"
        "  team_id: TESTTEAM01\n"
        "apps: {}\n"
    )
    assert load_config(cfg_path).signing.cert_password_env == "TWEAK_BUILD_P12_PASS"


def test_every_place_that_states_the_version_agrees() -> None:
    """pyproject, the package, the app's project file and its Info.plist all
    say the same release. verify-bundle checks the built app against the
    built CLI; this catches a bump that missed a file before any build."""
    import plistlib
    import tomllib

    import yaml

    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert __version__ == project
    yml = yaml.safe_load((REPO_ROOT / "Graft" / "project.yml").read_text())
    info = yml["targets"]["Graft"]["info"]["properties"]
    plist = plistlib.loads((REPO_ROOT / "Graft" / "Graft" / "Info.plist").read_bytes())
    for key in ("CFBundleShortVersionString", "CFBundleVersion"):
        assert str(info[key]) == plist[key], key
    short = plist["CFBundleShortVersionString"].split(".")
    assert short == project.split(".")[: len(short)], (plist["CFBundleShortVersionString"], project)


def test_this_is_graft_2_2() -> None:
    """The release this branch ships."""
    assert __version__ == "2.2"
