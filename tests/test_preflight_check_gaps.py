"""`--check` gaps: the REPLACEME team id and a Mac without Command Line Tools."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from graft_ipa import preflight
from graft_ipa.config import Config, SigningConfig


def _cfg(tmp_path: Path, team_id: str) -> Config:
    return Config(
        path=tmp_path / "config.yaml",
        signing=SigningConfig(
            cert=tmp_path / "cert.p12",
            cert_password_env="GRAFT_P12_PASS_TEST_UNSET",
            profile=tmp_path / "profile.mobileprovision",
            team_id=team_id,
        ),
        cache_dir=tmp_path / "cache",
        built_dir=tmp_path / "built",
        tweaks_dir=tmp_path / "tweaks",
        state_dir=tmp_path / "state",
        apps={},
    )


def _row(results, name):
    return next(r for r in results if r.name == name)


def test_replaceme_team_id_is_a_failure_with_a_fix_hint(tmp_path: Path) -> None:
    results = preflight.check_signing_files(_cfg(tmp_path, "REPLACEME"))
    row = _row(results, preflight.TEAM_ID_CHECK_NAME)
    assert row.ok is False
    assert "graft --set-settings --signing-team-id <TEAM>" in row.hint


def test_a_real_team_id_passes_the_team_id_check(tmp_path: Path) -> None:
    results = preflight.check_signing_files(_cfg(tmp_path, "TEAM123456"))
    assert _row(results, preflight.TEAM_ID_CHECK_NAME).ok is True



# ---- otool: the shim with nothing behind it ----
#
# `/usr/bin/otool` exists on every Mac; without developer tools it is Apple's
# shim. The fakes below behave like the real binaries, measured on a Mac with
# full Xcode: `otool --version` exits 0 and prints its version, while
# `otool -version` exits 1 with usage (it reads `-version` as six flags). A
# fake that exits 0 for anything let `return probe.returncode != 0` — which
# fails every build on every working Mac — pass the whole suite.

OTOOL_VERSION = (
    "llvm-otool(1): Apple Inc. version cctools-1040\n"
    "otool(1): Apple Inc. version cctools-1040\n"
)
OTOOL_USAGE = (
    "error: /usr/bin/otool: unknown char `s' in flag -version\n\n"
    "Usage: /usr/bin/otool [-arch arch_type] [-fahlLDtdorSTMRIHGvVcXmqQjCP] "
    "[-mcpu=arg] [--version] <object file> ...\n"
)


def _run_result(returncode: int, stderr: str = "", stdout: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class Tools:
    """`xcode-select -p` and `/usr/bin/otool` as a Mac answers them."""

    def __init__(self, developer_dir: Path | None, otool=None) -> None:
        self.developer_dir = developer_dir
        self.otool = otool or self.real_otool
        self.calls: list[list[str]] = []

    @staticmethod
    def real_otool(argv: list[str]):
        if argv[1:] == ["--version"]:
            return _run_result(0, stdout=OTOOL_VERSION)
        return _run_result(1, stderr=OTOOL_USAGE)

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        if cmd[0] == "xcode-select":
            if self.developer_dir is None:
                return _run_result(2, "xcode-select: error: unable to get active developer directory")
            return _run_result(0, stdout=f"{self.developer_dir}\n")
        return self.otool(cmd)

    def ran_otool(self) -> bool:
        return any(c[0] == "/usr/bin/otool" for c in self.calls)


def _otool_row(tools: Tools):
    with patch.object(preflight.shutil, "which", return_value="/usr/bin/otool"), \
         patch.object(preflight.subprocess, "run", side_effect=tools):
        return preflight.check_tools(["otool"])[0]


def test_a_working_otool_passes(tmp_path: Path) -> None:
    tools = Tools(tmp_path)
    assert _otool_row(tools).ok is True
    assert ["/usr/bin/otool", "--version"] in tools.calls


def test_a_working_otool_that_exits_non_zero_still_passes(tmp_path: Path) -> None:
    """The regression that blocks every build: a non-zero exit is not a stub.
    Only the shim's own words are. (This is what `otool -version` does.)"""
    tools = Tools(tmp_path, otool=lambda argv: _run_result(1, stderr=OTOOL_USAGE))
    assert _otool_row(tools).ok is True


def test_no_developer_directory_fails_without_running_the_shim() -> None:
    """Run without tools, the shim raises the install dialog."""
    tools = Tools(None)
    row = _otool_row(tools)
    assert row.ok is False
    assert "xcode-select --install" in row.hint
    assert not tools.ran_otool()


def test_a_developer_directory_that_is_gone_fails_without_running_the_shim(
    tmp_path: Path,
) -> None:
    """`xcode-select -p` exits 0 and prints a path that no longer exists — an
    Xcode deleted or renamed, a Command Line Tools folder removed by hand."""
    vanished = tmp_path / "Xcode-beta.app" / "Contents" / "Developer"
    tools = Tools(vanished)
    row = _otool_row(tools)
    assert row.ok is False
    assert str(vanished) in row.detail
    assert row.hint.startswith("install with: xcode-select --install")
    assert "xcode-select --reset" in row.hint
    assert not tools.ran_otool()


@pytest.mark.parametrize(
    "said",
    [
        "xcode-select: note: The tool 'otool' requires the command line developer tools",
        "xcrun: error: invalid active developer path (/Library/Developer/CommandLineTools), "
        "missing xcrun at: /Library/Developer/CommandLineTools/usr/bin/xcrun",
        "xcrun: error: invalid DEVELOPER_DIR path (/Applications/Xcode-old.app), missing xcrun",
        # What this Mac's shim says for DEVELOPER_DIR=/nonexistent.
        "xcrun: error: missing DEVELOPER_DIR path: /nonexistent",
    ],
)
def test_the_shims_own_words_mean_a_stub(tmp_path: Path, said: str) -> None:
    tools = Tools(tmp_path, otool=lambda argv: _run_result(1, said))
    row = _otool_row(tools)
    assert row.ok is False
    assert row.hint.startswith("install with: xcode-select --install")


def test_otool_outside_usr_bin_is_not_probed() -> None:
    with patch.object(preflight.shutil, "which", return_value="/opt/homebrew/bin/otool"), \
         patch.object(preflight.subprocess, "run", side_effect=AssertionError("no probe")):
        assert preflight.check_tools(["otool"])[0].ok is True


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple's otool")
def test_on_this_mac_a_working_otool_passes_the_real_check() -> None:
    """Unpatched. A false positive here fails every build, so the real check
    is held against the real tool: if `/usr/bin/otool --version` works, the
    row must pass. Skipped where there are no developer tools at all, since
    running the shim there raises the install dialog."""
    selected = subprocess.run(["xcode-select", "-p"], capture_output=True, text=True)
    if selected.returncode != 0 or not Path(selected.stdout.strip()).is_dir():
        pytest.skip("no developer tools selected on this Mac")
    if subprocess.run(["/usr/bin/otool", "--version"], capture_output=True).returncode != 0:
        pytest.skip("otool does not run here")
    row = preflight.check_tools(["otool"])[0]
    assert row.ok is True, (row.detail, row.hint)
