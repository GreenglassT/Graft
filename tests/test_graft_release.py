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


# --- 2.0.1: the private deny-list, the shared download cache, a phone's hotspot ---------------


def _python_that_fails_at(root: Path, tmp_path: Path, step: str | None, code: int = 1) -> Path:
    """The stand-in venv Python, also logging the caches it was given, and
    exiting `code` from the call whose arguments hold `step`."""
    envlog = tmp_path / "env.log"
    fail = f'case "$*" in *"{step}"*) exit {code};; esac\n' if step else ""
    py = root / ".venv" / "bin" / "python"
    py.write_text(f'#!/bin/sh\necho "python $*" >> "{tmp_path / "ran.log"}"\n'
                  f'echo "$* cache=${{GRAFT_BUNDLE_CACHE-unset}} work=${{GRAFT_BUNDLE_WORK-unset}}" >> "{envlog}"\n'
                  f"{fail}exit 0\n")
    return envlog


def test_before_building_it_checks_the_deny_list_scans_the_tracked_files_and_asks_about_a_hotspot(release, tmp_path):
    """In this order, all before Xcode resolves a package or anything is
    signed. The downloads cache is left to graft_bundle's per-user default:
    nothing points it into the checkout any more."""
    root, run = release
    envlog = _python_that_fails_at(root, tmp_path, None)
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
    proc, ran = run(str(tmp_path / "out"))
    assert proc.returncode != 0, "this checkout has no Xcode project to build"
    steps = [r.split(maxsplit=2)[1:] for r in ran]
    assert [Path(s[0]).name for s in steps] == ["graft_bundle.py", *["denylist.py"] * 4, "graft_bundle.py"]
    assert [s[1] for s in steps] == ["check-python", "check", f"tracked {root} {head}", "devices check",
                                     f"devices tracked {root} {head}", "hotspot-check"]
    assert Path(steps[2][0]).parent.parent != root, "the scan runs from the export of the commit"
    assert not [r for r in ran if r.split()[0] in TRIPPED]
    hotspot = envlog.read_text().splitlines()[-1]
    assert "graft_bundle.py hotspot-check cache=unset" in hotspot
    assert f"work={root}/build/bundle-work" in hotspot


@pytest.mark.parametrize("step", ["denylist.py check", "denylist.py tracked", "devices check", "devices tracked",
                                  "hotspot-check"])
def test_a_missing_list_a_deny_listed_name_or_a_no_on_the_hotspot_stops_it_there(release, tmp_path, step):
    root, run = release
    _python_that_fails_at(root, tmp_path, step)
    proc, ran = run(str(tmp_path / "out"))
    assert proc.returncode == 1, proc.stderr
    assert step in ran[-1] and not [r for r in ran if r.split()[0] in TRIPPED]


def test_with_no_profile_configured_the_device_scans_are_skipped_and_the_release_goes_on(release, tmp_path):
    """`devices check` exits 3, having said so: no `devices tracked`, and on
    to the hotspot check. Any other failure of it stops the release."""
    root, run = release
    _python_that_fails_at(root, tmp_path, "devices check", code=3)
    proc, ran = run(str(tmp_path / "out"))
    steps = [r.split(maxsplit=2)[2] for r in ran if len(r.split()) > 2]
    assert "devices check" in steps and not [s for s in steps if s.startswith("devices tracked")]
    assert steps[-1] == "hotspot-check", steps
    (tmp_path / "ran.log").unlink()
    _python_that_fails_at(root, tmp_path, "devices check", code=2)
    proc, ran = run(str(tmp_path / "out"))
    assert proc.returncode == 1 and ran[-1].endswith("devices check")


def test_the_app_and_the_source_archive_are_scanned_for_the_owners_devices_and_a_hit_removes_the_zip():
    script = (REPO / "scripts" / "graft-release").read_text()
    verify = script.index('"$PY" "$EXPORT/scripts/verify-bundle" "$APP"')
    app = script.index('denylist.py" devices app "$APP"')
    sources = script.index('denylist.py" devices sources "$SOURCES"')
    assert verify < app < script.index('graft_bundle.py" sources "$OUT_DIR/components.json"')
    assert script.index('denylist.py" sources "$SOURCES"') < sources < script.index('rev-parse HEAD)" != "$HEAD_AT_START"')
    for at in (app, sources):
        assert 'if [[ "$DEVICE_SCAN" == 1 ]] && !' in script[at - 60:at] and 'rm -f "$ZIP"' in script[at:at + 200]
    tail = script[script.index('echo "✓ $OUT_DIR/components.json"'):]
    assert 'if [[ "$DEVICE_SCAN" == 1 ]]; then' in tail and "no device UDID scan" in tail


def test_the_source_archive_is_scanned_and_a_hit_removes_the_zip():
    script = (REPO / "scripts" / "graft-release").read_text()
    sources = script.index('graft_bundle.py" sources "$OUT_DIR/components.json"')
    scan = script.index('denylist.py" sources "$SOURCES"')
    assert sources < scan < script.index('rev-parse HEAD)" != "$HEAD_AT_START"')
    assert 'rm -f "$ZIP"' in script[scan:scan + 200]
    assert "GRAFT_BUNDLE_CACHE=" not in script, "downloads are cached per user, not per checkout"


def test_a_release_with_the_scans_skipped_says_so_at_the_end():
    script = (REPO / "scripts" / "graft-release").read_text()
    tail = script[script.index('echo "✓ $OUT_DIR/components.json"'):]
    assert 'if [[ "${GRAFT_SKIP_DENYLIST:-}" == 1 ]]' in tail and "SKIPPED" in tail
