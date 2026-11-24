"""Every test that needs the developer tools skips without them (re-check #11, #34).

`tests/toolchain.py`'s probe was pinned, but nothing checked that the tests
which run `clang`, `make`, `lipo` or `git` carry a marker that uses it. Five
came in through a merge without one, and failed (CalledProcessError) rather
than skipped on a Mac with no developer directory; reverting #34's own marker
to `which("clang")` stayed green everywhere but there.

So this runs the suite's tool-using files the way such a Mac would: a bare
PATH, where clang, make, lipo and git are /usr/bin xcrun shims, and an
invalid DEVELOPER_DIR, under which those shims fail at once (an invalid
DEVELOPER_DIR makes xcrun error out; it does not offer an install). A test
that forgot its marker fails here, by name.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

REPO = Path(__file__).resolve().parent.parent

#: Every test file that runs one of the shimmed tools (found by grepping for
#: the argv). `test_the_list_of_tool_using_files_is_complete` keeps it honest.
TOOL_USING = (
    "tests/test_denylist.py",
    "tests/test_graft_bundle.py",
    "tests/test_graft_release.py",
    "tests/test_lc_golden.py",
    "tests/test_lc_sign.py",
    "tests/test_macho.py",
    "tests/test_playcover.py",
    "tests/test_tweak_sources.py",
    "tests/test_verify_bundle.py",
)
_SHIMS = ("clang", "make", "lipo", "git", "xcrun", "otool", "install_name_tool", "codesign")
_RUNS_A_SHIM = re.compile(r'\["(?:%s)"' % "|".join(_SHIMS))

#: Tests that need the tools, which must be among the skips: proof the run
#: really had no toolchain, and that each one's marker is what stopped it.
MUST_SKIP = (
    "test_graft_bundle::test_the_launcher_execs_the_embedded_python_through_a_symlink",
    "test_graft_bundle::test_universal_mach_o_files_in_the_stage_are_thinned_to_arm64",
    "test_graft_bundle::test_a_mach_o_with_no_arm64_slice_is_refused[archs0]",
    "test_graft_bundle::test_graft_source_is_head_without_the_internal_notes_and_only_from_a_clean_checkout",
)


def _must_skip() -> tuple[str, ...]:
    """MUST_SKIP plus one id per tweak suite in a local `tweaks-src/`, read
    from the checkout so no tweak is named here (the public export has no
    `tweaks-src/`, and then there are none)."""
    tweaks = REPO / "tweaks-src"
    suites = sorted(tweaks.glob("*/tests/Makefile")) if tweaks.is_dir() else []
    return MUST_SKIP + tuple(
        f"test_tweak_sources::test_a_tweaks_own_tests_pass[{m.parent.parent.name}]" for m in suites
    )


def test_the_list_of_tool_using_files_is_complete() -> None:
    found = sorted(
        str(p.relative_to(REPO))
        for p in (REPO / "tests").glob("test_*.py")
        if p.name != Path(__file__).name and _RUNS_A_SHIM.search(p.read_text())
    )
    assert found == sorted(TOOL_USING)


def test_without_a_developer_dir_they_skip_rather_than_fail(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "DEVELOPER_DIR": "/nonexistent-dev",
    }
    report = tmp_path / "report.xml"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", f"--junitxml={report}",
         *TOOL_USING],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=600,
    )
    out = proc.stdout + proc.stderr
    cases = ElementTree.parse(report).getroot().iter("testcase")
    outcome = {
        f"{c.get('classname', '').rsplit('.', 1)[-1]}::{c.get('name')}": (
            "failed" if c.find("failure") is not None or c.find("error") is not None
            else "skipped" if c.find("skipped") is not None
            else "passed"
        )
        for c in cases
    }
    failed = sorted(k for k, v in outcome.items() if v == "failed")
    assert proc.returncode == 0 and not failed, f"failed without a developer dir: {failed}\n{out[-3000:]}"
    not_skipped = [m for m in _must_skip() if outcome.get(m) != "skipped"]
    assert not not_skipped, f"ran, or wasn't found: {not_skipped}"
