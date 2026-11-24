"""scripts/graft-release's early refusals, run for real (the combined fix
check's critic: its fixes were pinned only by strings in the script).

Only the exits that come before anything is built: each runs the real
script with a dummy identity, a PATH whose codesign, security, xcodebuild,
xcrun and ditto record that they ran and fail, and a stand-in for the repo
venv's Python. None of them may run: no identity is used, nothing is signed.
The rest of the script (building, signing, verifying) is checked by
verify-bundle against each try build; its log is the evidence for that.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from .toolchain import needs_tools

#: The scratch checkout is made with git.
pytestmark = needs_tools("git")

REPO = Path(__file__).resolve().parent.parent
TRIPPED = ("codesign", "security", "xcodebuild", "xcrun", "ditto", "notarytool")


@pytest.fixture
def release(tmp_path: Path):
    """A checkout holding only graft-release, with a venv Python that
    records its arguments, committed clean; and the tripwire PATH."""
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "graft-release", root / "scripts" / "graft-release")
    log = tmp_path / "ran.log"
    py = root / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text(f'#!/bin/sh\necho "python $*" >> "{log}"\nexit 0\n')
    py.chmod(0o755)
    trip = tmp_path / "trip"
    trip.mkdir()
    for name in TRIPPED:
        (trip / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 99\n')
        (trip / name).chmod(0o755)
    (root / ".gitignore").write_text(".venv/\n")
    env = {"HOME": str(tmp_path), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "PATH": f"{trip}:/usr/bin:/bin:/usr/sbin:/sbin"}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, env=env, capture_output=True)

    def run(*args: str, identity: str | None = "Developer ID Application: Nobody (ZZZZZZZZZZ)"):
        e = dict(env)
        if identity is not None:
            e["GRAFT_SIGN_IDENTITY"] = identity
        proc = subprocess.run(["/bin/bash", str(root / "scripts" / "graft-release"), *args],
                              capture_output=True, text=True, env=e, timeout=60, cwd=root)
        ran = log.read_text().splitlines() if log.exists() else []
        return proc, ran

    return root, run


def test_without_an_identity_it_stops_before_anything(release, tmp_path):
    _, run = release
    proc, ran = run(str(tmp_path / "out"), identity=None)
    assert proc.returncode != 0
    assert "set GRAFT_SIGN_IDENTITY" in proc.stderr
    assert ran == []


@pytest.mark.parametrize("left", ["Graft.app", "Graft.zip", "components.json", "Graft-2.0-sources.tar.gz"])
def test_an_earlier_releases_output_stops_it_before_any_identity_is_used(release, tmp_path, left):
    """Review #35, behaviourally: the output folder's leftovers are refused
    before the venv, git or anything that signs is touched."""
    _, run = release
    out = tmp_path / "out"
    out.mkdir()
    (out / left).mkdir() if left.endswith(".app") else (out / left).write_text("old")
    proc, ran = run(str(out))
    assert proc.returncode == 1, proc.stderr
    assert f"{out / left} already exists" in proc.stderr
    assert ran == []


def test_a_checkout_with_any_change_is_refused_before_it_builds(release, tmp_path):
    """Review #9/#11: a release is a commit. An untracked file is a change;
    the refusal comes after check-python and before any build or signing."""
    root, run = release
    (root / "stray.txt").write_text("not committed")
    proc, ran = run(str(tmp_path / "out"))
    assert proc.returncode == 1, proc.stderr
    assert "the checkout has changes" in proc.stderr and "stray.txt" in proc.stderr
    assert [r.split()[0] for r in ran] == ["python"], ran
    assert ran[0].endswith("check-python")


def test_unfairplay_is_signed_with_the_identifier_the_root_step_requires():
    """Review #29: the root step's requirement names identifier "unfairplay";
    the release sets it rather than leaving it to codesign's file-name
    default. Text only, as this script can't run here: verify-bundle checks
    the built app's helper against the requirement itself."""
    import re

    from graft_ipa import enrollment

    script = (REPO / "scripts" / "graft-release").read_text()
    assert "UNFAIRPLAY=Helpers/bin/unfairplay" in script
    assert 'grep -vxF -e "$UNFAIRPLAY" -f "$WORK/entitled.txt" "$STAGE/macho-list.txt"' in script, (
        "kept out of the batch signed without an identifier"
    )
    (identifier,) = re.findall(r"--identifier (\S+) \\\n\s+--sign \"\$IDENTITY\" \"\$APP/Contents/\$UNFAIRPLAY\"", script)
    assert identifier == enrollment.UNFAIRPLAY_IDENTIFIER
    assert script.index("$APP/Contents/$UNFAIRPLAY") < script.index('--sign "$IDENTITY" "$APP"\n'), (
        "inside-out: before the app itself is signed"
    )


def _xcodebuild_call(script: str) -> list[str]:
    """The release build's xcodebuild invocation, as its words."""
    import shlex

    block = script[script.index("\nxcodebuild \\\n") + 1:]
    block = block[:block.index(" >")]
    return shlex.split(block.replace("\\\n", " "))


def test_the_release_build_uses_only_the_package_versions_it_resolved():
    """Review 2.0, bundle #11: xcodebuild resolved SwiftPM packages over the
    network; it must take Package.resolved's pins and never re-resolve."""
    script = (REPO / "scripts" / "graft-release").read_text()
    words = _xcodebuild_call(script)
    assert words[0] == "xcodebuild" and words[-1] == "build"
    assert "-onlyUsePackageVersionsFromResolvedFile" in words
    assert "-disableAutomaticPackageResolution" in words
    resolved = REPO / "Graft" / "Graft.xcodeproj" / "project.xcworkspace" / "xcshareddata" / "swiftpm" / "Package.resolved"
    assert resolved.is_file(), "the pins it builds from"
    tracked = subprocess.run(["git", "-C", str(REPO), "ls-files", "--error-unmatch", str(resolved)],
                             capture_output=True)
    assert tracked.returncode == 0, "committed, so the export the release builds from carries it"
