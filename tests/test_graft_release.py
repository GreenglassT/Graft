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

import re
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
    verify = script.index('"$PY" "$EXPORT/scripts/verify-bundle" ${ARCH_ARGS[@]+"${ARCH_ARGS[@]}"} "$APP"')
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


# --- 2.2: the whole script, with every tool it signs, builds or zips with a stand-in -----------

#: The stand-ins a full run uses: each records its arguments, one line per
#: call, then does just enough for the script to go on. Nothing is built,
#: signed or zipped for real. codesign also records what Info.plist's
#: GraftBundleArch is at that moment, so the order of the two shows.
FULL_FAKES = {
    "xcodebuild": r'''#!/bin/sh
echo "xcodebuild $*" >> "$LOG"
while [ $# -gt 0 ]; do [ "$1" = -derivedDataPath ] && D="$2"; shift; done
app="$D/Build/Products/Release/Graft.app/Contents"
mkdir -p "$app/MacOS" && : > "$app/MacOS/Graft"
cat > "$app/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict><key>CFBundleShortVersionString</key><string>9.9</string></dict></plist>
PLIST
''',
    "codesign": r'''#!/bin/sh
arch=$(/usr/libexec/PlistBuddy -c "Print :GraftBundleArch" "$APPDIR/Graft.app/Contents/Info.plist" 2>/dev/null || echo none)
echo "codesign [GraftBundleArch=$arch] $*" >> "$LOG"
''',
    "ditto": r'''#!/bin/sh
echo "ditto $*" >> "$LOG"
if [ "$1" = -c ]; then : > "$5"; else mkdir -p "$2" && cp -R "$1/." "$2/"; fi
''',
    "python": r'''#!/bin/sh
echo "python $* work=${GRAFT_BUNDLE_WORK-unset}" >> "$LOG"
case "$1 $2" in
  *graft_bundle.py\ stage) for last; do :; done
         mkdir -p "$last/Resources/Licenses" "$last/Helpers/bin" "$last/Frameworks"
         echo '[]' > "$last/Resources/Licenses/components.json"
         printf 'Helpers/bin/ipatool\nHelpers/bin/zsign\n' > "$last/macho-list.txt"
         case "$*" in *x86_64*) ;; *) echo 'Helpers/bin/unfairplay' >> "$last/macho-list.txt";; esac
         printf 'Helpers/bin/ipatool\tipatool.plist\n' > "$last/entitlements.txt"
         echo "staged $last: 3 Mach-O files to sign" ;;
  *graft_bundle.py\ sources) : > "$4" ;;
esac
[ "$1" = - ] && cat > /dev/null
exit 0
''',
}


@pytest.fixture
def full_release(tmp_path: Path):
    return _full_release(tmp_path)


def _full_release(tmp_path: Path):
    """A checkout with graft-release and a committed Xcode project, the
    stand-ins first on PATH, and a run() that returns the log with this
    run's own paths as placeholders."""
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    (root / "Graft" / "Graft.xcodeproj").mkdir(parents=True)
    (root / "Graft" / "Graft.xcodeproj" / "project.pbxproj").write_text("// the project\n")
    (root / ".gitignore").write_text(".venv/\nbuild/\n")
    bindir = tmp_path / "fakes"
    bindir.mkdir()
    for name, body in FULL_FAKES.items():
        target = (root / ".venv" / "bin" / "python") if name == "python" else bindir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        target.chmod(0o755)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    log = tmp_path / "ran.log"
    env = {"HOME": str(tmp_path), "TMPDIR": str(tmp), "LOG": str(log), "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "PATH": f"{bindir}:/usr/bin:/bin:/usr/sbin:/sbin",
           "GRAFT_SIGN_IDENTITY": "Developer ID Application: Nobody (ZZZZZZZZZZ)"}

    def run(script: str, *args: str, out: Path | None = None, app_swift: dict[str, str] | None = None):
        """`app_swift`: Swift files to have in the checkout (path -> text); the
        app's files are otherwise none, as before 2.2's Intel support."""
        (root / "scripts" / "graft-release").write_text(script)
        swift = root / "Graft" / "Graft"
        shutil.rmtree(swift, ignore_errors=True)
        shutil.rmtree(root / "Graft" / "GraftTests", ignore_errors=True)
        for rel, text in (app_swift or {}).items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        for git in (["init", "-q"], ["add", "-A"], ["commit", "-q", "--allow-empty", "-m", "x"]):
            subprocess.run(["git", "-C", str(root), *git], check=True, env=env, capture_output=True)
        out = out or tmp_path / "out"
        log.unlink(missing_ok=True)
        proc = subprocess.run(["/bin/bash", str(root / "scripts" / "graft-release"), *args, str(out)],
                              capture_output=True, text=True, env={**env, "APPDIR": str(out)}, timeout=120,
                              cwd=root)
        text = log.read_text() if log.exists() else ""
        text = re.sub(r"/\S*/graft-release\.[A-Za-z0-9]+", "$WORK", text)
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                              env=env).stdout.strip()
        text = text.replace(str(out), "$OUT").replace(str(root), "$ROOT").replace(head, "$HEAD")
        return proc, text, out

    return root, run


#: Everything v2.2's graft-release (17fe3a8) ran, in order, in the full run
#: above: its arm64 release, which this one must still be.
ARM64_RELEASE = (REPO / "tests" / "fixtures" / "bundle" / "graft-release-arm64-2.2.log").read_text()
#: Its SHA-256. The log is data, so writing it again would quietly make any
#: change pass: a change to it needs review, as a change to the arm64
#: release, never a regenerated file.
ARM64_RELEASE_SHA256 = "64247a7764750579dabff09b00833c66d6d6a54a8ca9914324e97c8b0fb4048a"
SCRIPT = (REPO / "scripts" / "graft-release").read_text()


def _plist(app: Path) -> dict:
    import plistlib
    return plistlib.loads((app / "Contents" / "Info.plist").read_bytes())


def test_the_arm64_release_runs_exactly_what_2_2_did_and_says_which_bundle_it_is_before_signing(full_release):
    """Every command, argument and output of v2.2's release, unchanged; and
    Info.plist names the bundle's architecture before anything is signed,
    so the seal covers it."""
    _, run = full_release
    proc, log, out = run(SCRIPT)
    assert proc.returncode == 0, proc.stderr
    assert log == ARM64_RELEASE.replace("[GraftBundleArch=none]", "[GraftBundleArch=arm64]")
    assert sorted(p.name for p in out.iterdir()) == ["Graft-9.9-sources.tar.gz", "Graft.app", "Graft.zip",
                                                     "components.json"]
    assert _plist(out / "Graft.app")["GraftBundleArch"] == "arm64"
    _, same, _ = run(SCRIPT, "--arch", "arm64", out=out.parent / "out-explicit")
    assert same == log, "--arch arm64 is the default"


def test_the_intel_release_stages_verifies_and_zips_the_x86_64_bundle(full_release):
    _, run = full_release
    proc, log, out = run(SCRIPT, "--arch", "x86_64", app_swift=APP_READS_ARCH)
    assert proc.returncode == 0, proc.stderr
    lines = log.splitlines()
    want = [line.replace("[GraftBundleArch=none]", "[GraftBundleArch=x86_64]") for line in ARM64_RELEASE.splitlines()
            if "--identifier unfairplay" not in line]
    swaps = {
        "graft_bundle.py hotspot-check work=": "graft_bundle.py hotspot-check --arch x86_64 work=",
        "graft_bundle.py stage $WORK/stage": "graft_bundle.py stage --arch x86_64 $WORK/stage",
        "verify-bundle $OUT/Graft.app": "verify-bundle --arch x86_64 $OUT/Graft.app",
        "$OUT/Graft.zip": "$OUT/Graft-Intel.zip",
    }
    for old, new in swaps.items():
        want = [line.replace(old, new) for line in want]
    assert lines == want, "the arm64 release's commands, for x86_64, and no UnfairPlay to sign"
    assert sorted(p.name for p in out.iterdir()) == ["Graft-9.9-sources.tar.gz", "Graft-Intel.zip", "Graft.app",
                                                     "components.json"]
    assert _plist(out / "Graft.app")["GraftBundleArch"] == "x86_64"
    assert "Graft-Intel.zip" in proc.stdout and "Graft.zip " not in proc.stdout


@pytest.mark.parametrize("args", [["--arch"], ["--arch", "ppc"], ["--arch", "intel"]])
def test_an_architecture_no_release_is_built_for_stops_it_before_anything(release, tmp_path, args):
    _, run = release
    proc, ran = run(*args, str(tmp_path / "out"))
    assert proc.returncode == 2 and "--arch takes arm64 or x86_64" in proc.stderr
    assert ran == []


def test_an_earlier_intel_zip_stops_an_intel_release_before_anything(release, tmp_path):
    _, run = release
    out = tmp_path / "out"
    out.mkdir()
    (out / "Graft-Intel.zip").write_text("old")
    proc, ran = run("--arch", "x86_64", str(out))
    assert proc.returncode == 1 and f"{out / 'Graft-Intel.zip'} already exists" in proc.stderr and ran == []


def test_an_intel_release_defaults_to_its_own_output_folder(release, tmp_path):
    root, run = release
    old = root / "build" / "release-intel" / "Graft-Intel.zip"
    old.parent.mkdir(parents=True)
    old.write_text("old")
    proc, ran = run("--arch", "x86_64")
    assert proc.returncode == 1 and f"{old} already exists" in proc.stderr and ran == []


# --- review: an Intel release needs an app that reads GraftBundleArch ---------------------------

#: The app's own code reading the key, as its Intel support does (AppleSilicon.swift).
APP_READS_ARCH = {"Graft/Graft/Services/AppleSilicon.swift":
                  'enum BundleArch {\n    static let infoKey = "GraftBundleArch"\n}\n'}


@pytest.mark.parametrize("swift", [
    {},
    {"Graft/GraftTests/AppleSiliconTests.swift": 'let key = "GraftBundleArch"\n'},
    {"Graft/Graft/Services/AppleSilicon.swift": "/// Its Info.plist's `GraftBundleArch`, in a comment only.\n"},
    {"Graft/Graft/Services/Notes.txt": '"GraftBundleArch"\n'},
    {"Graft/Graft/Services/AppleSilicon.swift": '    // static let infoKey = "GraftBundleArch"\n'},
], ids=["no Swift", "only a test reads it", "only a comment names it", "not a Swift file", "commented out"])
def test_an_intel_release_of_an_app_that_doesnt_read_its_arch_is_refused_before_anything_is_built(
        full_release, swift):
    """Its executable is universal, so without the app's own reading of
    GraftBundleArch an Intel Mac gets the old "needs Apple silicon" screen
    from a bundle that verify-bundle passes."""
    _, run = full_release
    proc, log, out = run(SCRIPT, "--arch", "x86_64", app_swift=swift)
    assert proc.returncode == 1
    assert "GraftBundleArch" in proc.stderr and "needs Apple silicon" in proc.stderr
    assert [line.split()[1].rsplit("/", 1)[-1] + " " + line.split()[2] for line in log.splitlines()] == [
        "graft_bundle.py check-python", "denylist.py check"], "refused before any scan, build or signing"
    assert not out.exists()


def test_an_arm64_release_needs_no_such_code(full_release):
    _, run = full_release
    proc, log, _ = run(SCRIPT, app_swift={})
    assert proc.returncode == 0, proc.stderr
    assert log == ARM64_RELEASE.replace("[GraftBundleArch=none]", "[GraftBundleArch=arm64]")


def test_the_v2_2_release_record_is_the_one_reviewed():
    import hashlib
    got = hashlib.sha256((REPO / "tests" / "fixtures" / "bundle" / "graft-release-arm64-2.2.log").read_bytes()).hexdigest()
    assert got == ARM64_RELEASE_SHA256, "the v2.2 release record changed: review it as a change to the arm64 release"
