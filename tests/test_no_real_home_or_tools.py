"""No test can reach the developer's home folder or the tools on their PATH.

Graft finds its tools through an explicit config path, the bundled copy, a
default location under HOME (`~/.local/bin/ipatool`) and then PATH. A test
that forgets to fake one of those runs the developer's real tool: one did,
running a real `ipatool auth info`, which reads that ipatool's Keychain item.
`conftest._no_real_home_or_tools` closes all of it for every test at once.
"""

from __future__ import annotations

import os
import pwd
import shutil
from pathlib import Path

from graft_ipa import tools

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
SYSTEM_PATH = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]


def test_home_is_not_the_developers_home() -> None:
    assert Path(os.environ["HOME"]).resolve() != REAL_HOME.resolve()
    assert Path.home().resolve() != REAL_HOME.resolve()
    assert not str(Path("~").expanduser().resolve()).startswith(str(REAL_HOME.resolve()) + "/")


def test_path_holds_only_the_system_directories() -> None:
    assert os.environ["PATH"].split(os.pathsep) == SYSTEM_PATH


def test_a_tool_only_the_developer_installed_is_not_found() -> None:
    # Neither ipatool nor bagbak ships with macOS, so on a bare PATH and a
    # scratch HOME nothing should find either one.
    for name in ("ipatool", "bagbak", "iproxy", "frida"):
        assert shutil.which(name) is None, name
        assert tools.which(name) is None, name


def test_home_relative_defaults_follow_the_scratch_home() -> None:
    from graft_ipa import cli, ipatool, trash

    for path in (
        ipatool.default_path(),
        trash.default_trash_dir(),
        cli._playcover_settings_dir(),
    ):
        assert not path.resolve().is_relative_to(REAL_HOME.resolve()), path
