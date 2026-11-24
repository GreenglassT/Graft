"""scripts/verify-bundle's gates: what it must never run.

Running code from an app whose seal is broken, or from a quarantined copy
Gatekeeper rejects, puts a blocking dialog on the owner's screen. These tests
fake codesign and spctl; nothing here assesses or runs a real app.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from .toolchain import needs_toolchain, needs_tools

REPO = Path(__file__).resolve().parents[1]
_loader = importlib.machinery.SourceFileLoader("verify_bundle", str(REPO / "scripts" / "verify-bundle"))
_spec = importlib.util.spec_from_loader("verify_bundle", _loader)
vb = importlib.util.module_from_spec(_spec)
sys.modules["verify_bundle"] = vb
_loader.exec_module(vb)


def _fake_app(root: Path, *, stapled: bool = True) -> Path:
    app = root / "Graft.app"
    (app / "Contents" / "Helpers").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_text("<plist/>")
    (app / "Contents" / "Helpers" / "graft").write_text("#!/bin/sh\ntouch ran\n")
    if stapled:
        (app / "Contents" / "CodeResources").write_bytes(b"a stapled notarization ticket")
    return app


def _zip_of(app: Path, dest: Path) -> Path:
    with zipfile.ZipFile(dest, "w") as z:
        for p in sorted(app.rglob("*")):
            z.write(p, p.relative_to(app.parent))
    return dest


def _gatekeeper(monkeypatch, *, accepts: bool) -> list[list[str]]:
    """Fake spctl; let ditto and xattr run for real (on temp files); refuse
    anything else, which here could only be something from the copy."""
    calls: list[list[str]] = []
    real = vb.run

    def run(argv, **kw):
        argv = [str(a) for a in argv]
        calls.append(argv)
        if argv[0] == "spctl":
            if accepts == "unnotarized-but-0":
                return subprocess.CompletedProcess(argv, 0, "", "accepted\nsource=Unnotarized Developer ID")
            return subprocess.CompletedProcess(argv, 0 if accepts else 3, "",
                                               "accepted\nsource=Notarized Developer ID" if accepts
                                               else "rejected\nsource=Unnotarized Developer ID")
        if argv[0] in ("ditto", "xattr"):
            return real(argv, **kw)
        pytest.fail(f"verify-bundle ran {argv}")

    monkeypatch.setattr(vb, "run", run)
    return calls


def test_a_zip_gatekeeper_rejects_has_nothing_run_from_it(tmp_path, monkeypatch):
    zipped = _zip_of(_fake_app(tmp_path / "built"), tmp_path / "Graft.zip")
    calls = _gatekeeper(monkeypatch, accepts=False)
    monkeypatch.setattr(vb, "verify_app", lambda *a, **k: pytest.fail("ran the checks on a rejected copy"))
    report = vb.Report()
    work = tmp_path / "work"
    work.mkdir()
    vb.verify_zip(zipped, report, work)
    assert [c[0] for c in calls] == ["xattr", "ditto", "spctl"]
    assert ("Gatekeeper accepts the quarantined copy as notarized (spctl -a -vv -t exec)", False) in report.results
    assert report.summary() == 1
    # Review #52: the zip is quarantined, then unpacked by ditto, which carries
    # it onto every file, as a browser download unpacked by Archive Utility is.
    app = work / "unzipped" / "Graft.app"
    assert set(vb.quarantined(app)) == {app, *app.rglob("*")}, "every file, not the top folder alone"
    assert ("every unpacked file carries the quarantine", True) in report.results
    assert not vb.has_xattr(zipped, vb.QUARANTINE), "the release's own zip is left alone"
    assert not list(work.rglob("ran"))


def test_gatekeeper_must_say_notarized_not_only_exit_0(tmp_path, monkeypatch):
    """Review #55: the verdict's source, not spctl's exit status alone."""
    zipped = _zip_of(_fake_app(tmp_path / "built"), tmp_path / "Graft.zip")
    _gatekeeper(monkeypatch, accepts="unnotarized-but-0")
    monkeypatch.setattr(vb, "verify_app", lambda *a, **k: pytest.fail("ran the checks on an unnotarized copy"))
    work = tmp_path / "work"
    work.mkdir()
    report = vb.Report()
    vb.verify_zip(zipped, report, work)
    assert ("Gatekeeper accepts the quarantined copy as notarized (spctl -a -vv -t exec)", False) in report.results


def test_a_zip_gatekeeper_accepts_goes_on_to_the_runtime_checks(tmp_path, monkeypatch):
    zipped = _zip_of(_fake_app(tmp_path / "built"), tmp_path / "Graft.zip")
    _gatekeeper(monkeypatch, accepts=True)
    seen = []
    monkeypatch.setattr(vb, "verify_app", lambda app, report, work, **k: seen.append((app.name, k)))
    work = tmp_path / "work"
    work.mkdir()
    vb.verify_zip(zipped, vb.Report(), work)
    assert seen == [("Graft.app", {"gatekeeper_accepted": True})]


def test_a_quarantined_app_is_refused_before_anything_runs(tmp_path, monkeypatch):
    app = _fake_app(tmp_path)
    subprocess.run(["xattr", "-w", vb.QUARANTINE, "0081;00000000;Safari;", str(app / "Contents" / "Helpers" / "graft")],
                   check=True)
    monkeypatch.setattr(vb, "run", lambda argv, **kw: pytest.fail(f"ran {argv}"))
    report = vb.Report()
    vb.verify_app(app, report, tmp_path / "work")
    assert ("no quarantine attribute (a downloaded copy goes through --zip)", False) in report.results


def test_a_broken_seal_stops_before_the_runtime_checks(tmp_path, monkeypatch):
    app = _fake_app(tmp_path)
    monkeypatch.setattr(vb, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 1, "", "code object is not signed at all"))
    monkeypatch.setattr(vb, "runtime_checks", lambda *a: pytest.fail("ran the app with a broken seal"))
    monkeypatch.setattr(vb, "check_signatures", lambda *a: pytest.fail("went past a broken seal"))
    report = vb.Report()
    vb.verify_app(app, report, tmp_path / "work")
    assert ("codesign --verify --deep --strict", False) in report.results


@pytest.mark.parametrize("named", ["checkout", "home"])
def test_a_file_naming_the_checkout_or_home_fails_the_build_path_check(tmp_path, monkeypatch, named):
    """Each needle on its own: a checkout outside the home folder, and a home
    path that isn't the checkout."""
    checkout = Path("/opt/graft-release-checkout")
    monkeypatch.setattr(vb, "REPO", checkout)
    app = _fake_app(tmp_path)
    report = vb.Report()
    vb.check_no_build_paths(app, report)
    assert report.results[-1][1] is True
    leaked = checkout / "Graft/X.swift" if named == "checkout" else Path.home() / "Documents/x"
    (app / "Contents" / "MacOS").mkdir()
    (app / "Contents" / "MacOS" / "Graft").write_bytes(b"\0___profc_" + str(leaked).encode() + b"\0")
    vb.check_no_build_paths(app, report)
    assert report.results[-1] == ("no file names the release machine's home folder or checkout", False)


def test_a_home_path_inside_a_zlib_stream_fails_the_build_path_check(tmp_path, monkeypatch):
    """1.0 and 1.1 leaked build paths only inside compressed data (coverage
    mappings), where a plain byte search can't see them."""
    import zlib
    checkout = Path("/opt/graft-release-checkout")
    monkeypatch.setattr(vb, "REPO", checkout)
    app = _fake_app(tmp_path)
    (app / "Contents" / "MacOS").mkdir()
    hidden = zlib.compress(b"filename table\0" + str(Path.home() / "src/x.swift").encode() + b"\0" * 64)
    blob = b"\x00\x01junk before the stream" + hidden + b"trailing"
    assert str(Path.home()).encode() not in blob
    (app / "Contents" / "MacOS" / "Graft").write_bytes(blob)
    report = vb.Report()
    vb.check_no_build_paths(app, report)
    assert report.results[-1] == ("no file names the release machine's home folder or checkout", False)


def test_coverage_or_profiling_sections_fail(tmp_path):
    """A Release build with code coverage on carries __llvm_covmap/__llvm_prf_*
    sections, which hold the source paths; 1.1 build 3 turned them off."""
    from . import macho_builder as mb
    app = _fake_app(tmp_path)
    (app / "Contents" / "MacOS").mkdir()
    exe = app / "Contents" / "MacOS" / "Graft"
    exe.write_bytes(mb.thin([]))
    report = vb.Report()
    vb.check_no_coverage_sections(app, report)
    assert report.results[-1][1] is True
    exe.write_bytes(mb.thin([]) + b"__llvm_covmap\0\0\0__DATA\0")
    vb.check_no_coverage_sections(app, report)
    assert report.results[-1] == ("no Mach-O carries coverage or profiling sections", False)


# ------------------------------------------------------------- architectures
#
# The app's own executable stays universal, so that on an Intel Mac it opens
# and shows its "needs Apple silicon" screen rather than macOS's "can't be
# opened". Everything it runs is arm64 only.

MAIN = "Contents/MacOS/Graft"


def test_a_universal_executable_and_arm64_everything_else_passes():
    assert vb.arch_problems({MAIN: ["x86_64", "arm64"], "Contents/Helpers/graft": ["arm64"],
                             "Contents/Frameworks/libssl.3.dylib": ["arm64"]}) == ([], [])


@pytest.mark.parametrize("archs", [["arm64"], ["x86_64"], ["arm64", "x86_64", "arm64e"], []])
def test_the_executable_must_be_exactly_arm64_and_x86_64(archs):
    main, others = vb.arch_problems({MAIN: archs, "Contents/Helpers/graft": ["arm64"]})
    assert len(main) == 1 and main[0].startswith(MAIN), main
    assert others == []


def test_a_missing_executable_is_a_problem_not_a_pass():
    main, _ = vb.arch_problems({"Contents/Helpers/graft": ["arm64"]})
    assert main and MAIN in main[0]


@pytest.mark.parametrize("archs", [["arm64", "x86_64"], ["x86_64"], ["arm64e"], []])
def test_every_other_mach_o_must_be_arm64_only(archs):
    main, others = vb.arch_problems({MAIN: ["arm64", "x86_64"], "Contents/Helpers/bin/zsign": archs,
                                     "Contents/Helpers/graft": ["arm64"]})
    assert main == []
    assert len(others) == 1 and others[0].startswith("Contents/Helpers/bin/zsign"), others


def test_the_architecture_checks_read_lipo_for_every_mach_o(tmp_path, monkeypatch):
    from . import macho_builder as mb
    app = _fake_app(tmp_path)
    (app / "Contents" / "MacOS").mkdir()
    (app / "Contents" / "MacOS" / "Graft").write_bytes(mb.thin([]))
    (app / "Contents" / "Helpers" / "graft").write_bytes(mb.thin([]))
    (app / "Contents" / "Helpers" / "tool").write_bytes(mb.thin([]))
    answers = {"Contents/MacOS/Graft": "x86_64 arm64", "Contents/Helpers/graft": "arm64",
               "Contents/Helpers/tool": "x86_64 arm64"}
    asked = []

    def run(argv, **kw):
        argv = [str(a) for a in argv]
        assert argv[:2] == ["lipo", "-archs"], argv
        rel = str(Path(argv[2]).relative_to(app))
        asked.append(rel)
        return subprocess.CompletedProcess(argv, 0, answers[rel] + "\n", "")

    monkeypatch.setattr(vb, "run", run)
    report = vb.Report()
    vb.check_architectures(app, report)
    assert sorted(asked) == sorted(answers)
    assert report.results == [
        ("the app's executable is universal (arm64 and x86_64), for the Intel screen", True),
        ("every other Mach-O file is arm64 only", False),
    ]


# ---------------------------------------------------------------- components


def _components(app: Path, drop: str | None = None) -> None:
    import json
    import tomllib
    entries = []
    for name, pin in tomllib.loads((REPO / "bundle" / "pins.toml").read_text()).items():
        for built in pin.get("builds", [name]):
            if built != drop:
                entries.append({"name": built, "version": pin["version"],
                                "kind": "built-from-source" if name in vb.BUILT_FROM_SOURCE else "artifact",
                                "source_url": pin["url"], "sha256": pin["sha256"], "license": pin["license"]})
    lic = app / "Contents" / "Resources" / "Licenses"
    lic.mkdir(parents=True, exist_ok=True)
    (lic / "components.json").write_text(json.dumps(entries))


def test_components_must_list_every_pin_and_each_of_cyans_helpers_built_from_source(tmp_path):
    app = _fake_app(tmp_path)
    _components(app)
    report = vb.Report()
    vb.check_components(app, report)
    assert [ok for _, ok in report.results] == [True, True]
    for drop in ("otool", "libplist", "zsign"):
        _components(app, drop=drop)
        report = vb.Report()
        vb.check_components(app, report)
        assert False in [ok for _, ok in report.results], drop


def test_verify_bundle_and_the_stage_agree_on_cyans_helpers():
    _gb_spec = importlib.util.spec_from_file_location("graft_bundle_vb", REPO / "scripts" / "graft_bundle.py")
    gb = importlib.util.module_from_spec(_gb_spec)
    sys.modules["graft_bundle_vb"] = gb
    _gb_spec.loader.exec_module(gb)
    assert vb.CYAN_HELPERS == gb.CYAN_HELPERS
    assert vb.CYAN_TOOLS == str(gb.CYAN_TOOLS)
    assert vb.CYAN_EXTRAS == str(gb.CYAN_EXTRAS)
    assert vb.BUILT_FROM_SOURCE == gb.BUILT_FROM_SOURCE


def test_every_python_package_in_components_must_have_a_licence_file(tmp_path):
    import json
    app = _fake_app(tmp_path)
    lic = app / "Contents" / "Resources" / "Licenses"
    (lic / "python-packages" / "loguru-0.7.3").mkdir(parents=True)
    (lic / "python-packages" / "loguru-0.7.3" / "LICENSE").write_text("MIT")
    (lic / "python-packages" / "empty-1.0").mkdir()
    entries = [{"name": "loguru", "version": "0.7.3", "kind": "python-package"}]
    (lic / "components.json").write_text(json.dumps(entries))
    report = vb.Report()
    vb.check_package_licences(app, report)
    assert report.results == [("every Python package in components.json has a license file in Licenses/", True)]
    for extra in ({"name": "empty", "version": "1.0", "kind": "python-package"},
                  {"name": "absent", "version": "2.0", "kind": "python-package"}):
        (lic / "components.json").write_text(json.dumps([*entries, extra]))
        report = vb.Report()
        vb.check_package_licences(app, report)
        assert report.results[-1][1] is False, extra


def _framework_licences(app: Path, frameworks: list[str], *, skip_text: str = "", empty_text: str = "",
                        skip_entry: str = "", elsewhere: str = "") -> list[tuple[str, bool]]:
    """Run check_framework_licences on an app with `frameworks` in cyan/extras,
    each with its license text and components.json entry, less the ones named."""
    import json
    import shutil
    contents = app / "Contents"
    shutil.rmtree(contents / "Resources", ignore_errors=True)
    lic = contents / "Resources" / "Licenses"
    (lic / "cyan-extras").mkdir(parents=True)
    extras = contents / "Resources/GraftPython/lib/python3.13/site-packages/cyan/extras"
    entries = [{"name": "cyan", "kind": "python-package", "source_url": "https://example.invalid/cyan.zip"}]
    for fw in frameworks:
        stem = fw.removesuffix(".framework")
        (extras / fw).mkdir(parents=True)
        (extras / fw / stem).write_bytes(b"binary")
        if fw != skip_text:
            (lic / "cyan-extras" / f"{stem}-LICENSE").write_text("" if fw == empty_text else "license text")
        if fw != skip_entry:
            where = "tools/Darwin/arm64" if fw == elsewhere else f"extras/{fw}"
            entries.append({"name": fw, "kind": "artifact",
                            "source_url": f"https://github.com/o/r/archive/{'0' * 40}.zip#r-0/cyan/{where}/{stem}"})
    (lic / "components.json").write_text(json.dumps(entries))
    report = vb.Report()
    vb.check_framework_licences(app, report)
    return report.results


def test_every_framework_in_cyan_extras_must_have_a_licence_file_and_a_components_entry(tmp_path):
    app = _fake_app(tmp_path)
    good = ["CydiaSubstrate.framework", "Orion.framework"]
    name = "every framework in cyan/extras has a license file in Licenses/ and a components.json entry"
    assert _framework_licences(app, good) == [(name, True)]
    for broken in ({"skip_text": "Orion.framework"}, {"empty_text": "Orion.framework"},
                   {"skip_entry": "CydiaSubstrate.framework"}, {"elsewhere": "Orion.framework"}):
        assert _framework_licences(app, good, **broken) == [(name, False)], broken
    assert _framework_licences(app, []) == [(name, False)], "an app with no cyan/extras at all"


# ------------------------------------------------- review #2: no shell in a helper

ZSIGN_AUDITED = "2790ec58a55db5f4282843d0cdbb0b587d19b972331b5285aee2de67f4ab705e"
TOOLS = vb.CYAN_TOOLS


def test_a_helper_importing_system_or_popen_is_a_problem():
    imports = {f"{TOOLS}/install_name_tool": {"_system", "_getenv"}, f"{TOOLS}/ldid": {"_open"},
               "Frameworks/libx.dylib": {"_popen"}, "Helpers/bin/ipatool": {"_fork"}}
    assert vb.shell_import_problems(imports, {"zsign": {"sha256": ZSIGN_AUDITED}}) == [
        "Frameworks/libx.dylib imports _popen", f"{TOOLS}/install_name_tool imports _system"]


def test_an_unreadable_helper_is_a_problem_not_a_pass():
    assert vb.shell_import_problems({"Helpers/bin/zsign": None}, {}) == ["Helpers/bin/zsign: nm can't read its imports"]


def test_zsigns_audited_popen_is_allowed_only_for_the_zsign_that_was_audited():
    imports = {"Helpers/bin/zsign": {"_popen", "_fork"}}
    assert vb.shell_import_problems(imports, {"zsign": {"sha256": ZSIGN_AUDITED.upper()}}) == []
    assert vb.shell_import_problems(imports, {"zsign": {"sha256": "0" * 64}}) == ["Helpers/bin/zsign imports _popen"]
    # The exception is for zsign's popen, not for anything else it might import.
    assert vb.shell_import_problems({"Helpers/bin/zsign": {"_system"}}, {"zsign": {"sha256": ZSIGN_AUDITED}}) == [
        "Helpers/bin/zsign imports _system"]
    assert vb.SHELL_IMPORT_EXCEPTIONS[("Helpers/bin/zsign", "_popen")][1] == ZSIGN_AUDITED
    pinned = tomllib.loads((REPO / "bundle" / "pins.toml").read_text())
    assert pinned["zsign"]["sha256"] == ZSIGN_AUDITED, "a new zsign pin needs its popen audited again"


def test_the_helpers_are_cyans_tools_helpers_and_frameworks_not_the_python_runtime(tmp_path, monkeypatch):
    contents = tmp_path / "Contents"
    for rel in (f"{TOOLS}/otool", "Helpers/graft", "Helpers/bin/zsign", "Frameworks/libcrypto.3.dylib",
                "Frameworks/ossl-modules/legacy.dylib", "Resources/GraftPython/bin/python3.13",
                "Resources/GraftPython/lib/python3.13/lib-dynload/_ssl.so", "MacOS/Graft"):
        (contents / rel).parent.mkdir(parents=True, exist_ok=True)
        (contents / rel).write_bytes(b"x")
    monkeypatch.setattr(vb, "mach_o_files", lambda root: sorted(p for p in root.rglob("*") if p.is_file()))
    got = {str(p.relative_to(contents)) for p in vb.helper_files(contents)}
    assert got == {f"{TOOLS}/otool", "Helpers/graft", "Helpers/bin/zsign", "Frameworks/libcrypto.3.dylib",
                   "Frameworks/ossl-modules/legacy.dylib"}


def test_the_hostile_deb_is_one_bsdtar_unpacks_as_cyan_does(tmp_path):
    """cyan unpacks a .deb on a Mac with `tar -xf` (bsdtar reads ar), then its
    data.tar.* the same way, and keys every tweak by its file name."""
    deb = vb.make_deb(tmp_path / "x.deb", {f"Library/MobileSubstrate/DynamicLibraries/{vb.HOSTILE_TWEAK}": b"dylib"})
    out = tmp_path / "out"
    out.mkdir()
    subprocess.run(["/usr/bin/tar", "-xf", str(deb), f"--directory={out}"], check=True)
    assert sorted(p.name for p in out.iterdir()) == ["control.tar.gz", "data.tar.gz", "debian-binary"]
    subprocess.run(["/usr/bin/tar", "-xf", str(out / "data.tar.gz"), f"--directory={out}"], check=True)
    tweak = out / "Library" / "MobileSubstrate" / "DynamicLibraries" / "Fx;touch PWNED;.dylib"
    assert tweak.read_bytes() == b"dylib"
    assert vb.HOSTILE_EXECUTABLE == "X;touch PWNED #"


def test_only_a_file_the_shell_would_have_made_counts_as_pwned(tmp_path):
    for name in ("Fx;touch PWNED;.dylib", "X;touch PWNED #", "PWNEDish"):
        (tmp_path / name).write_text("an input, not a side effect")
    assert vb.pwned_files(tmp_path) == []
    (tmp_path / "deep").mkdir()
    (tmp_path / "deep" / "PWNED").touch()
    assert vb.pwned_files(tmp_path) == [tmp_path / "deep" / "PWNED"]


# ------------------------------------------------- review #4 under option (a): entitlements, Unicorn

JIT = {"com.apple.security.cs.allow-jit": True}
DLV = {"com.apple.security.cs.disable-library-validation": True}
IPATOOL = {**JIT, **DLV}


def test_only_ipatool_has_entitlements_and_exactly_allow_jit_and_disable_library_validation():
    """The combined fix check's medium #2, option (a): the Unicorn ipatool
    downloads is ad hoc signed, so ipatool, and only ipatool, gains
    disable-library-validation; never allow-dyld-environment-variables."""
    good = {"Helpers/bin/ipatool": IPATOOL, "Helpers/bin/zsign": {}, "MacOS/Graft": {}}
    assert vb.entitlement_problems(good) == []
    for bad, why in (
        ({**good, "Helpers/bin/ipatool": JIT}, "ipatool"),
        ({**good, "Helpers/bin/ipatool": DLV}, "ipatool"),
        ({**good, "Helpers/bin/ipatool": {**IPATOOL, "com.apple.security.cs.allow-dyld-environment-variables": True}},
         "ipatool"),
        ({**good, "Helpers/bin/ipatool": {}}, "ipatool"),
        ({**good, "Resources/GraftPython/bin/python3.13": DLV}, "python3.13"),
        ({**good, "Helpers/bin/zsign": JIT}, "zsign"),
        ({**good, "Helpers/bin/zsign": None}, "can't be read"),
        ({"Helpers/bin/zsign": {}}, "ipatool: missing"),
    ):
        problems = vb.entitlement_problems(bad)
        assert problems and why in problems[0], (bad, problems)


def test_the_entitlements_ipatool_is_signed_with_are_the_ones_verify_bundle_expects():
    import plistlib

    from . import test_graft_bundle as tgb

    shipped = plistlib.loads((REPO / "bundle" / "entitlements" / "ipatool.plist").read_bytes())
    assert shipped == vb.EXPECTED_ENTITLEMENTS["Helpers/bin/ipatool"] == tgb.gb.IPATOOL_ENTITLEMENTS
    assert set(vb.JIT_VARIANTS["as ipatool"]) == set(shipped)


def _contents(tmp_path: Path, files: dict[str, bytes]) -> Path:
    contents = tmp_path / "Contents"
    for rel, data in files.items():
        (contents / rel).parent.mkdir(parents=True, exist_ok=True)
        (contents / rel).write_bytes(data)
    return contents


SITE = "Resources/GraftPython/lib/python3.13/site-packages"
CLEAN = {
    # ipatool downloads Unicorn, so it names the wheel's member.
    "Helpers/bin/ipatool": b"unicorn/lib/libunicorn.2.dylib",
    f"{SITE}/graft_ipa/ipatool.py": b'UNICORN_FIRST_USE = "ipatool needs to download its emulator (Unicorn)"\n',
    # gunicorn is not Unicorn: uvicorn's workers.py names it.
    f"{SITE}/uvicorn/workers.py": b'import gunicorn.workers  # see gunicorn/workers/base.py\n',
    "Resources/Licenses/INDEX.txt": b"ipatool downloads Unicorn 2.1.4 (GPL-2.0) from PyPI on first use",
    "Helpers/bin/zsign": b"zsign",
}


def test_no_unicorn_anywhere_in_the_app(tmp_path):
    contents = _contents(tmp_path, CLEAN)
    machos = [contents / "Helpers/bin/ipatool", contents / "Helpers/bin/zsign"]
    assert vb.unicorn_problems(contents, machos) == []


@pytest.mark.parametrize(("rel", "data", "said"), [
    ("Frameworks/libunicorn.2.dylib", b"\xcf\xfa\xed\xfe", "Frameworks/libunicorn.2.dylib: Unicorn is in the app"),
    ("Resources/Licenses/unicorn-COPYING", b"GPL", "Resources/Licenses/unicorn-COPYING: Unicorn is in the app"),
    (f"{SITE}/unicorn/__init__.py", b"", f"{SITE}/unicorn: Unicorn is in the app"),
    ("Helpers/bin/zsign", b"dlopen libunicorn.2.dylib", "Helpers/bin/zsign names Unicorn"),
    (f"{SITE}/graft_ipa/cli.py", b"ctypes.CDLL('libunicorn.2.dylib')\n", f"{SITE}/graft_ipa/cli.py names Unicorn"),
    (f"{SITE}/graft_ipa/ipatool.py", b'BUNDLED_UNICORN = "libunicorn.2.dylib"\n',
     f"{SITE}/graft_ipa/ipatool.py names Unicorn"),
])
def test_a_unicorn_file_or_a_new_namer_is_named(tmp_path, rel, data, said):
    contents = _contents(tmp_path, {**CLEAN, rel: data})
    machos = [contents / "Helpers/bin/ipatool", contents / "Helpers/bin/zsign", contents / "Frameworks/libunicorn.2.dylib"]
    assert said in vb.unicorn_problems(contents, machos)


def test_the_real_ipatool_module_names_no_unicorn_library(tmp_path):
    source = (REPO / "graft_ipa" / "ipatool.py").read_bytes()
    contents = _contents(tmp_path, {**CLEAN, f"{SITE}/graft_ipa/ipatool.py": source})
    assert vb.unicorn_problems(contents, [contents / "Helpers/bin/ipatool"]) == []


# ---- the JIT harness: ipatool's entitlements, with an ad hoc signed library ----


def _ran(code: int, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, out, err)


GOOD_RUNS = {
    "as ipatool": _ran(0, "jit=42\n"),
    "without disable-library-validation": _ran(10, "", "dlopen: ... mapping process and mapped file "
                                               "(non-platform) have different Team IDs"),
    "without allow-jit": _ran(12, "jit=-22\n", "MAP_JIT refused: errno 22"),
}


def test_the_harness_passes_only_when_ipatools_signing_works_and_both_controls_fail():
    assert vb.jit_harness_problems(GOOD_RUNS) == []
    for label, run, said in (
        ("as ipatool", _ran(10, "", "dlopen: ... different Team IDs"), "as ipatool"),
        ("without disable-library-validation", _ran(0, "jit=42\n"), "without disable-library-validation"),
        ("without disable-library-validation", _ran(10, "", "dlopen: no such file"), "Team ID"),
        ("without allow-jit", _ran(0, "jit=42\n"), "without allow-jit"),
    ):
        problems = vb.jit_harness_problems({**GOOD_RUNS, label: run})
        assert problems and said in " ".join(problems), (label, problems)


def test_the_harness_needs_the_release_identity(tmp_path, monkeypatch):
    monkeypatch.delenv("GRAFT_SIGN_IDENTITY", raising=False)
    monkeypatch.setattr(vb, "run", lambda *a, **k: pytest.fail("ran something without an identity"))
    report = vb.Report()
    vb.check_jit_harness(tmp_path / "Graft.app", vb.Sandbox(tmp_path / "sb"), report)
    assert report.results == [(report.results[0][0], False)]


@needs_toolchain
@needs_tools("codesign")
def test_the_harness_sources_build_and_run_jit_code_and_allow_jit_is_what_permits_it(tmp_path):
    """The C the harness compiles at verify time, built and run here, ad hoc
    signed (the release identity's Team-ID control runs only in
    verify-bundle): under the hardened runtime it runs JIT code with both of
    ipatool's entitlements, and is refused MAP_JIT without allow-jit."""
    import plistlib

    (tmp_path / "jit.c").write_text(vb.JIT_LIBRARY)
    (tmp_path / "loader.c").write_text(vb.JIT_LOADER)
    lib = tmp_path / "libjit.dylib"
    for step in (["clang", "-arch", "arm64", f"-mmacosx-version-min={vb._stage_module().MACOS_MIN}", "-Wall", "-Werror", "-dynamiclib",
                  "-o", lib, tmp_path / "jit.c"],
                 ["codesign", "--force", "--sign", "-", lib],
                 ["clang", "-arch", "arm64", f"-mmacosx-version-min={vb._stage_module().MACOS_MIN}", "-Wall", "-Werror", "-o",
                  tmp_path / "loader", tmp_path / "loader.c"]):
        subprocess.run([str(a) for a in step], check=True, capture_output=True)
    seen = {}
    for label in ("as ipatool", "without allow-jit"):
        exe, plist = tmp_path / label.replace(" ", "-"), tmp_path / f"{label.replace(' ', '-')}.plist"
        plist.write_bytes(plistlib.dumps({k: True for k in vb.JIT_VARIANTS[label]}))
        subprocess.run(["cp", str(tmp_path / "loader"), str(exe)], check=True)
        subprocess.run(["codesign", "--force", "--sign", "-", "--options", "runtime", "--entitlements", str(plist),
                        str(exe)], check=True, capture_output=True)
        seen[label] = subprocess.run([str(exe), str(lib)], capture_output=True, text=True, timeout=30)
    assert (seen["as ipatool"].returncode, seen["as ipatool"].stdout) == (0, "jit=42\n"), seen
    assert seen["without allow-jit"].returncode == 12, seen


# ------------------------------------------------- review #6/#34: iOS code as shipped

def test_ios_code_must_be_the_audited_frameworks_byte_for_byte_and_unsealed(tmp_path):
    import hashlib
    contents = tmp_path / "Contents"
    orion = contents / vb.CYAN_EXTRAS / "Orion.framework" / "Orion"
    orion.parent.mkdir(parents=True)
    orion.write_bytes(b"orion")
    rel = str(orion.relative_to(contents))
    expected = {rel: hashlib.sha256(b"orion").hexdigest(),
                f"{vb.CYAN_EXTRAS}/Cephei.framework/Cephei": "0" * 64}
    assert vb.ios_code_problems(contents, [orion], expected) == [f"{vb.CYAN_EXTRAS}/Cephei.framework/Cephei: missing"]
    del expected[f"{vb.CYAN_EXTRAS}/Cephei.framework/Cephei"]
    assert vb.ios_code_problems(contents, [orion], expected) == []
    (orion.parent / "_CodeSignature").mkdir()
    orion.write_bytes(b"orion, re-signed")
    stray = contents / "Resources" / "x.dylib"
    stray.write_bytes(b"ios")
    assert vb.ios_code_problems(contents, [orion, stray], expected) == [
        f"{rel}: not the bytes cyan ships (SHA-256 {hashlib.sha256(b'orion, re-signed').hexdigest()})",
        f"{vb.CYAN_EXTRAS}/Orion.framework: has a _CodeSignature/", "Resources/x.dylib: iOS code nothing accounts for"]


# ------------------------------------------------- review #14/#36: macOS 15

def test_every_macos_slice_must_load_on_the_promised_macos():
    versions = {"Contents/MacOS/Graft": [(1, (15, 0, 0)), (1, (14, 0, 0))],
                "Contents/Helpers/bin/x": [(1, (11, 0, 0))],
                "Contents/Resources/ios.framework/ios": [(2, (17, 0, 0))],
                "Contents/Frameworks/new.dylib": [(1, (15, 4, 0))],
                "Contents/Frameworks/catalyst.dylib": [(6, (16, 0, 0))],
                "Contents/Frameworks/silent.dylib": [(None, None)]}
    assert vb.minos_problems(versions, (15, 0, 0)) == [
        "Contents/Frameworks/catalyst.dylib: needs macOS 16.0.0",
        "Contents/Frameworks/new.dylib: needs macOS 15.4.0",
        "Contents/Frameworks/silent.dylib: no minimum macOS in its load commands"]


def test_the_promised_minimum_must_be_the_stages(tmp_path):
    """LSMinimumSystemVersion must be exactly the macOS the stage builds for
    (14.0): a 15.0 left in Info.plist would refuse macOS 14 users the app
    was built for, and an older one would promise a macOS nothing was built
    or checked against."""
    for promised, ok in (("14.0", True), ("15.0", False), ("13.0", False), ("", False)):
        app = _fake_app(tmp_path / (promised or "none"))
        import plistlib
        (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"LSMinimumSystemVersion": promised}))
        report = vb.Report()
        vb.check_minimum_macos(app, report)
        assert report.results and all(ok_ for _, ok_ in report.results) is ok, (promised, report.results)


def test_a_symlink_anywhere_in_the_app_fails(tmp_path):
    app = _fake_app(tmp_path)
    report = vb.Report()
    vb.check_no_symlinks(app, report)
    (app / "Contents" / "Helpers" / "python3").symlink_to("graft")
    vb.check_no_symlinks(app, report)
    assert [ok for _, ok in report.results] == [True, False]



# ------------------------------------------------- fix-check #8: a private usbmuxd socket

def test_the_sandboxs_usbmuxd_socket_is_private_and_short_enough(tmp_path):
    import stat
    sb = vb.Sandbox(tmp_path / "sandbox")
    sock = Path(sb.env["USBMUXD_SOCKET_ADDRESS"])
    assert sock.parent.is_dir() and not sock.exists()
    mode = sock.parent.stat()
    assert stat.S_IMODE(mode.st_mode) == 0o700 and mode.st_uid == os.getuid()
    assert len(os.fsencode(sock)) < 104, "AF_UNIX's limit"
    deep = tmp_path / ("d" * 90)
    assert len(os.fsencode(vb.Sandbox(deep).env["USBMUXD_SOCKET_ADDRESS"])) < 104


def test_the_release_machine_is_named_by_its_real_home_and_its_username(monkeypatch):
    """Review #59: $HOME is whatever the caller set; pwd says whose account it is."""
    import pwd
    me = pwd.getpwuid(os.getuid())
    monkeypatch.setenv("HOME", "/tmp/elsewhere")
    needles = vb.machine_needles(Path("/x/checkout"))
    assert b"/x/checkout" in needles and me.pw_dir.encode() in needles and b"/tmp/elsewhere" not in needles
    assert f"/Users/{me.pw_name}/".encode() in needles
    assert (me.pw_name.encode() in needles) == (len(me.pw_name) >= 5)


# ------------------------------------------------- review #54: what each Mach-O links

def test_every_link_and_rpath_must_resolve_inside_the_app_or_be_the_systems(tmp_path):
    from . import macho_builder as mb
    contents = tmp_path / "Contents"
    fw = contents / "Frameworks"
    fw.mkdir(parents=True)
    (fw / "libcrypto.3.dylib").write_bytes(mb.thin([], filetype=mb.MH_DYLIB))
    good = contents / "Helpers" / "bin" / "zsign"
    good.parent.mkdir(parents=True)
    good.write_bytes(mb.thin([mb.dylib_cmd("/usr/lib/libSystem.B.dylib"),
                              mb.dylib_cmd("@executable_path/../../Frameworks/libcrypto.3.dylib"),
                              mb.dylib_cmd("@rpath/libcrypto.3.dylib"), mb.rpath_cmd("@loader_path/../../Frameworks")]))
    bad = fw / "libbad.dylib"
    bad.write_bytes(mb.thin([mb.dylib_cmd("/opt/homebrew/lib/libssl.3.dylib"), mb.dylib_cmd("@loader_path/libnone.dylib"),
                             mb.dylib_cmd("@rpath/libgone.dylib"), mb.rpath_cmd("/Users/runner/work/x/lib"),
                             mb.rpath_cmd("@loader_path")], filetype=mb.MH_DYLIB))
    assert vb.link_problems(contents, [good]) == []
    assert vb.link_problems(contents, [bad]) == [
        "Frameworks/libbad.dylib: rpath /Users/runner/work/x/lib",
        "Frameworks/libbad.dylib: links /opt/homebrew/lib/libssl.3.dylib",
        "Frameworks/libbad.dylib: links @loader_path/libnone.dylib, which isn't there",
        "Frameworks/libbad.dylib: links @rpath/libgone.dylib, which no rpath reaches"]


def test_a_signature_that_displays_but_does_not_verify_fails(tmp_path, monkeypatch):
    """Review #56: `codesign -d` prints a signature even for changed code."""
    from . import macho_builder as mb
    app = _fake_app(tmp_path)
    (app / "Contents" / "Helpers" / "graft").write_bytes(mb.thin([mb.build_version_cmd(mb.PLATFORM_MACOS, "15.0")]))
    shown = ("Authority=Developer ID Application: Test (ABCDE12345)\nTeamIdentifier=ABCDE12345\n"
             "Timestamp=Nov 20, 2026\nCodeDirectory v=20500 size=1 flags=0x10000(runtime) hashes=1+1\n")

    def fake(argv, **kw):
        argv = [str(a) for a in argv]
        if argv[1] == "-d":
            return subprocess.CompletedProcess(argv, 0, "", shown)
        modified = argv[-1].endswith("/graft")
        return subprocess.CompletedProcess(argv, 1 if modified else 0, "",
                                           "invalid signature (code or signature have been modified)" if modified else "")
    monkeypatch.setattr(vb, "run", fake)
    report = vb.Report()
    vb.check_signatures(app, report)
    assert [ok for _, ok in report.results][:2] == [True, False]


def test_every_components_licence_files_must_exist_and_hold_licence_text(tmp_path):
    """Review #53: not only Python packages, and not any file at all."""
    lic = tmp_path / "Licenses"
    lic.mkdir()
    (lic / "openssl-LICENSE.txt").write_text("Apache License, Version 2.0. " + "Permission granted. " * 10)
    (lic / "jedi-LICENSE.txt").write_text("see AUTHORS.txt")
    entries = [{"name": "openssl", "version": "3", "kind": "built-from-source", "licence_files": ["openssl-LICENSE.txt"]},
               {"name": "hacl", "version": "x", "kind": "licence-text", "licence_files": []},
               {"name": "jedi", "version": "1", "kind": "python-package", "licence_files": ["jedi-LICENSE.txt"]},
               {"name": "zsign", "version": "1", "kind": "artifact", "licence_files": []},
               {"name": "ldid", "version": "2", "kind": "built-from-source", "licence_files": ["ldid-COPYING"]}]
    assert vb.licence_file_problems(lic, entries) == [
        "jedi 1: none of jedi-LICENSE.txt holds license text", "zsign 1: no license files", "ldid 2: ldid-COPYING missing"]


def test_the_apps_version_must_be_the_clis():
    """Review #32: nothing compared Info.plist's literal with pyproject."""
    assert vb.versions_agree("1.1", "1.1.0") and vb.versions_agree("2.0.1", "2.0.1")
    assert not vb.versions_agree("1.1", "2.0.0") and not vb.versions_agree("2.0", "2.1.0")
    assert not vb.versions_agree("", "1.1.0") and not vb.versions_agree("1.1.0.1", "1.1.0")


# ---- CA overrides (combined fix check #13) ----


def _app_with_this_python(tmp_path: Path) -> Path:
    """A Graft.app whose GraftPython is a venv of this interpreter that sees
    this checkout's graft_ipa, so verify-bundle's CA probe runs as it would
    in the bundle: `python3.13 -I`, and bundled_tools_dir() finds Helpers/bin."""
    import sysconfig

    app = tmp_path / "Graft.app"
    (app / "Contents" / "Helpers" / "bin").mkdir(parents=True)
    gp = app / "Contents" / "Resources" / "GraftPython"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(gp)], check=True)
    ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    site = gp / "lib" / ver / "site-packages"
    (site / "graft-test.pth").write_text(f"{REPO}\n{sysconfig.get_paths()['purelib']}\n")
    if ver != "python3.13":
        (gp / "bin" / "python3.13").symlink_to(gp / "bin" / ver)
    return app


def test_the_ca_check_passes_on_this_code_and_names_what_it_kept(tmp_path):
    app = _app_with_this_python(tmp_path)
    report = vb.Report()
    vb.check_ca_overrides(app, vb.Sandbox(tmp_path / "sb"), report)
    assert report.results == [(report.results[0][0], True)], report.results


def test_the_ca_check_fails_when_a_real_ca_file_is_dropped(tmp_path, monkeypatch):
    """The 2.0 behavior (every CA override dropped) is what it must catch."""
    app = _app_with_this_python(tmp_path)
    report = vb.Report()
    real_run = vb.Sandbox.run

    def run(self, argv, *, stdin="", extra=None):
        if "-c" in [str(a) for a in argv] and argv[-1] == vb.CA_PROBE:
            argv = [*argv[:-1], vb.CA_PROBE.replace(
                "from graft_ipa import child_env\n",
                "from graft_ipa import child_env\n"
                "child_env.usable_ca_override = lambda name, value: False\n")]
        return real_run(self, argv, stdin=stdin, extra=extra)

    monkeypatch.setattr(vb.Sandbox, "run", run)
    vb.check_ca_overrides(app, vb.Sandbox(tmp_path / "sb"), report)
    assert [ok for _, ok in report.results] == [False]


def test_ca_override_problems_names_each_wrong_value():
    given = {"REQUESTS_CA_BUNDLE": "/real.pem", "SSL_CERT_FILE": "/gone.pem"}
    good = {"REQUESTS_CA_BUNDLE": "/real.pem", "SSL_CERT_FILE": None}
    assert vb.ca_override_problems({"process": good, "child": good}, given, {"REQUESTS_CA_BUNDLE"}) == []
    bad = {"REQUESTS_CA_BUNDLE": None, "SSL_CERT_FILE": "/gone.pem"}
    assert vb.ca_override_problems({"process": good, "child": bad}, given, {"REQUESTS_CA_BUNDLE"}) == [
        "child: REQUESTS_CA_BUNDLE=None, want '/real.pem'",
        "child: SSL_CERT_FILE='/gone.pem', want None",
    ]


def test_the_hostile_environments_ca_settings_all_name_nothing_usable(tmp_path):
    """So the hostile --check and build show junk being dropped, not kept."""
    from graft_ipa import child_env

    env, _ = vb.hostile_env(vb.Sandbox(tmp_path / "sb"))
    for name in child_env.TLS_OVERRIDES:
        assert name in env and not child_env.usable_ca_override(name, env[name]), name


# ---- the stage's static-library and Rust-crate notices (combined fix check #8) ----


def test_a_static_library_without_its_licence_file_is_named(tmp_path):
    lic = tmp_path / "Licenses"
    lic.mkdir()
    (lic / "python-bzip2-LICENSE.txt").write_text("bzip2 license")
    assert vb.static_licence_problems(lic, ["bzip2"]) == []
    assert vb.static_licence_problems(lic, ["bzip2", "libffi"]) == [
        "python3.13 contains libffi, but Licenses/python-libffi-LICENSE.txt is missing"]


def _wheel(contents: Path, name: str, *, cargo: bool) -> None:
    import json

    site = contents / "Resources" / "GraftPython" / "lib" / "python3.13" / "site-packages"
    dist = site / f"{name}-1.0.dist-info"
    (dist / "sboms").mkdir(parents=True)
    (dist / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
    purl = "pkg:cargo/serde@1.0.0" if cargo else "pkg:pypi/x@1"
    (dist / "sboms" / "x.cdx.json").write_text(json.dumps({"components": [{"name": "serde", "purl": purl}]}))


def test_a_wheel_built_from_rust_crates_without_their_licences_is_named(tmp_path):
    gb = vb._stage_module()
    contents = tmp_path / "Contents"
    _wheel(contents, "pydantic_core", cargo=True)
    _wheel(contents, "plainpkg", cargo=False)
    assert vb.rust_crate_licence_problems(contents, gb) == [
        f"pydantic_core 1.0 links Rust crates, but Licenses/rust-crates/{gb._norm('pydantic_core')}/ is missing or empty"]
    crate = contents / "Resources" / "Licenses" / "rust-crates" / gb._norm("pydantic_core") / "serde@1.0.0"
    crate.mkdir(parents=True)
    (crate / "LICENSE-MIT").write_text("MIT")
    assert vb.rust_crate_licence_problems(contents, gb) == []


def test_a_binary_holding_rust_crates_without_their_notices_fails(tmp_path):
    """RC audit #1: try12's bcrypt module and ipatool named crates.io crates
    with no license anywhere in Licenses/."""
    app = tmp_path / "Graft.app"
    contents = app / "Contents"
    bcrypt = contents / "Resources/GraftPython/lib/python3.13/site-packages/bcrypt/_bcrypt.abi3.so"
    bcrypt.parent.mkdir(parents=True)
    from . import macho_builder as mb

    bcrypt.write_bytes(mb.thin([]) + b"/cargo/registry/src/index.crates.io-1949cf8c6b5b557f/blowfish-0.9.1/src/lib.rs")
    report = vb.Report()
    vb.check_rust_crate_notices(app, report)
    assert report.results == [("every Rust crate a bundled binary holds has its license in Licenses/rust-crates/",
                               False)]
    (contents / "Resources/Licenses/rust-crates/bcrypt/blowfish@0.9.1").mkdir(parents=True)
    report = vb.Report()
    vb.check_rust_crate_notices(app, report)
    assert [ok for _, ok in report.results] == [True]


def test_a_crate_only_ipatools_core_names_show_needs_its_notice_in_the_built_app(tmp_path, capsys):
    """Delta audit #1: try13's ipatool holds 36 crates.io crates its core
    shows only by their function names; the check read source paths alone
    and printed PASS. Each needs its folder, at the version
    bundle/ipatool-core-crates.lock pins, and the check counts the crates."""
    gb = vb._stage_module()
    app = tmp_path / "Graft.app"
    contents = app / "Contents"
    ipatool = contents / "Helpers/bin/ipatool"
    ipatool.parent.mkdir(parents=True)
    from . import macho_builder as mb
    from .test_graft_bundle import _wasm

    ipatool.write_bytes(mb.thin([]) + _wasm(["_ZN10extism_pdk6extism5alloc17h944d363220faf261E",
                                             "_ZN6op_log8loggable3fmt17h48478c94fa5fa4e6E"]))
    pinned = next(f"{n}@{v}" for (n, v) in gb.pinned_crates() if n == "extism-pdk")
    report = vb.Report()
    vb.check_rust_crate_notices(app, report)
    assert report.results == [("every Rust crate a bundled binary holds has its license in Licenses/rust-crates/",
                               False)]
    assert f"Helpers/bin/ipatool holds Rust crates with no license in Licenses/rust-crates/: {pinned}" in (
        capsys.readouterr().out)
    (contents / f"Resources/Licenses/rust-crates/ipatool/{pinned}").mkdir(parents=True)
    report = vb.Report()
    vb.check_rust_crate_notices(app, report)
    assert [ok for _, ok in report.results] == [True]
    assert "1 crates in 1 of 1 files" in capsys.readouterr().out


def test_the_licence_check_reads_the_staged_python_and_reports_both(tmp_path, monkeypatch):
    real = vb._stage_module()
    monkeypatch.setattr(real, "python_static_libraries", lambda gp: ["bzip2", "expat"])
    monkeypatch.setattr(vb, "_stage_module", lambda: real)
    app = tmp_path / "Graft.app"
    contents = app / "Contents"
    (contents / "Resources" / "Licenses").mkdir(parents=True)
    (contents / "Resources" / "Licenses" / "python-bzip2-LICENSE.txt").write_text("x")
    _wheel(contents, "qh3", cargo=True)
    report = vb.Report()
    vb.check_static_and_rust_licences(app, report)
    assert [ok for _, ok in report.results] == [False, False]


@pytest.mark.parametrize("ticket", ["missing", "empty"])
def test_a_notarized_but_unstapled_zip_is_refused_before_anything_is_assessed(tmp_path, monkeypatch, ticket):
    """Combined fix check #7: spctl's online lookup calls an unstapled app
    notarized too; the zip sent to notarytool, before stapling, passed."""
    app = _fake_app(tmp_path / "built", stapled=ticket == "empty")
    if ticket == "empty":
        (app / "Contents" / "CodeResources").write_bytes(b"")
    zipped = _zip_of(app, tmp_path / "Graft.zip")
    calls = _gatekeeper(monkeypatch, accepts=True)
    monkeypatch.setattr(vb, "verify_app", lambda *a, **k: pytest.fail("ran the checks on an unstapled copy"))
    work = tmp_path / "work"
    work.mkdir()
    report = vb.Report()
    vb.verify_zip(zipped, report, work)
    assert ("the app carries its stapled notarization ticket (Contents/CodeResources)", False) in report.results
    assert "spctl" not in [c[0] for c in calls]


# --- review #29: root's check of its UnfairPlay copy, against the built app ----------


def _cp(rc: int, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["codesign"], rc, "", stderr)


def test_the_requirement_check_passes_only_when_its_own_team_accepts_and_another_is_unsatisfied():
    assert vb.requirement_problems(_cp(0), _cp(3)) == []
    (own,) = vb.requirement_problems(_cp(3, "test-requirement: code failed to satisfy"), _cp(3))
    assert "own team refuses it (exit 3)" in own
    (other,) = vb.requirement_problems(_cp(0), _cp(0))
    assert "another team doesn't refuse it" in other
    # A codesign that failed for any other reason (exit 1: unsigned, unreadable)
    # is not the requirement refusing it.
    assert vb.requirement_problems(_cp(0), _cp(1, "code object is not signed at all"))


def test_the_check_asks_codesign_with_the_root_step_s_own_requirement(tmp_path, monkeypatch):
    """The team read as the root step reads it, the requirement built by the
    root step's own function, and the bundled helper, read-only."""
    from graft_ipa import enrollment

    app = _fake_app(tmp_path)
    monkeypatch.setattr(enrollment, "app_team", lambda a: "TESTTEAM01" if a == app else pytest.fail(a))
    monkeypatch.setattr(enrollment, "app_is_developer_id", lambda a: True if a == app else pytest.fail(a))
    calls: list[list[str]] = []

    def run(argv, **kw):
        argv = [str(a) for a in argv]
        calls.append(argv)
        return _cp(0 if "TESTTEAM01" in argv[4] else 3)

    monkeypatch.setattr(vb, "run", run)
    report = vb.Report()
    vb.check_unfairplay_requirement(app, report)
    helper = str(app / "Contents" / "Helpers" / "bin" / "unfairplay")
    assert calls == [
        ["codesign", "--verify", "--strict", "-R", "=" + enrollment.requirement("TESTTEAM01", developer_id=True),
         helper],
        ["codesign", "--verify", "--strict", "-R", "=" + enrollment.requirement(vb.FIXTURE_TEAM, developer_id=True),
         helper],
    ]
    assert report.results == [(
        "root's check of its UnfairPlay copy accepts the bundled one for the app's team, and not another's", True,
    )]


@pytest.mark.parametrize("team", ["ad-hoc", "fails"])
def test_an_app_with_no_readable_team_fails_the_requirement_check(tmp_path, monkeypatch, team):
    from graft_ipa import enrollment
    from graft_ipa.errors import AppStoreError

    def app_team(a):
        if team == "fails":
            raise AppStoreError("couldn't read Graft.app's signature")
        return enrollment.AD_HOC

    monkeypatch.setattr(enrollment, "app_team", app_team)
    monkeypatch.setattr(vb, "run", lambda argv, **kw: pytest.fail(f"ran {argv}"))
    report = vb.Report()
    vb.check_unfairplay_requirement(_fake_app(tmp_path), report)
    assert [ok for _, ok in report.results] == [False]


def test_codesign_says_unsatisfied_with_3_and_satisfied_with_0():
    """What requirement_problems relies on, from the real codesign, read-only,
    against an Apple-signed system binary: no prompt, nothing signed."""
    from graft_ipa import enrollment

    unsatisfied = vb.run(["codesign", "--verify", "--strict", "-R", "=" + enrollment.requirement("ABCDE12345"),
                          "/bin/ls"])
    satisfied = vb.run(["codesign", "--verify", "--strict", "-R", '=identifier "com.apple.ls" and anchor apple',
                        "/bin/ls"])
    assert (unsatisfied.returncode, satisfied.returncode) == (vb.REQUIREMENT_UNSATISFIED, 0), (
        unsatisfied.stderr, satisfied.stderr
    )


def test_a_sealed_app_gets_the_requirement_check_before_anything_runs(tmp_path, monkeypatch):
    """verify_app runs it with the static checks, before the runtime ones."""
    app = _fake_app(tmp_path)
    order: list[str] = []
    monkeypatch.setattr(vb, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    for name in [n for n in dir(vb) if n.startswith("check_")]:
        monkeypatch.setattr(vb, name, lambda *a, _n=name, **k: order.append(_n) or True)
    monkeypatch.setattr(vb, "runtime_checks", lambda *a, **k: order.append("runtime_checks"))
    vb.verify_app(app, vb.Report(), tmp_path / "work")
    assert "check_unfairplay_requirement" in order
    assert order.index("check_unfairplay_requirement") < order.index("runtime_checks")


@pytest.mark.parametrize("check", ["check_rust_crate_notices", "check_no_junk"])
def test_the_rc_audit_checks_run_with_the_static_ones(tmp_path, monkeypatch, check):
    app = _fake_app(tmp_path)
    order: list[str] = []
    monkeypatch.setattr(vb, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    for name in [n for n in dir(vb) if n.startswith("check_")]:
        monkeypatch.setattr(vb, name, lambda *a, _n=name, **k: order.append(_n) or True)
    monkeypatch.setattr(vb, "runtime_checks", lambda *a, **k: order.append("runtime_checks"))
    vb.verify_app(app, vb.Report(), tmp_path / "work")
    assert check in order and order.index(check) < order.index("runtime_checks")


def test_a_dll_or_anything_the_stage_prunes_shipping_fails(tmp_path):
    """RC audit #2, #7: Wintun's DLLs and about 38 MB of upstream wheel
    content shipped in try12. Anything PRUNE names, or any Windows binary
    anywhere in the app, fails."""
    app = tmp_path / "Graft.app"
    site = app / "Contents/Resources/GraftPython/lib/python3.13/site-packages"
    (site / "pytun_pmd3").mkdir(parents=True)
    (site / "pytun_pmd3" / "wintun.py").write_text("# imported only on win32")
    report = vb.Report()
    vb.check_no_junk(app, report)
    assert [ok for _, ok in report.results] == [True]
    for junk in ("pytun_pmd3/wintun/bin/arm64/wintun.dll", "jedi/third_party/typeshed/stdlib/builtins.pyi",
                 "Crypto/SelfTest/__init__.py", "gifs/demo.gif", "misc/usbmux_sniff.sh", "fastapi/.agents/x.md"):
        (site / junk).parent.mkdir(parents=True, exist_ok=True)
        (site / junk).write_text("x")
        report = vb.Report()
        vb.check_no_junk(app, report)
        assert [ok for _, ok in report.results] == [False], junk
        shutil.rmtree(site / junk.split("/")[0]) if "/" in junk else (site / junk).unlink()
        if junk.startswith("pytun_pmd3/"):
            (site / "pytun_pmd3").mkdir()
            (site / "pytun_pmd3" / "wintun.py").write_text("# imported only on win32")
    helper = app / "Contents/Helpers/bin/x.dll"
    helper.parent.mkdir(parents=True)
    helper.write_bytes(b"MZ" + b"\0" * 58 + (64).to_bytes(4, "little") + b"PE\0\0" + b"\0" * 32)
    report = vb.Report()
    vb.check_no_junk(app, report)
    assert report.results == [("no Windows binaries, and nothing the stage prunes, ship in the app", False)]


# --- UnfairPlay, built from its pinned source (owner, 2026-11-23) ---------------------


def test_components_must_list_unfairplay_as_built_from_source(tmp_path):
    """Its pin is its source now: an entry calling it an artifact is the
    prebuilt release's, not the stage's build."""
    import json

    app = _fake_app(tmp_path)
    _components(app)
    path = app / "Contents" / "Resources" / "Licenses" / "components.json"
    entries = json.loads(path.read_text())
    for e in entries:
        if e["name"] == "unfairplay":
            assert e["kind"] == "built-from-source"
            e["kind"] = "artifact"
    path.write_text(json.dumps(entries))
    report = vb.Report()
    vb.check_components(app, report)
    assert report.results[1] == (
        "components.json lists cyan's five helpers, libplist, ipatool and unfairplay as built from source", False)


class _FakeStage:
    """What check_unfairplay_offline takes from graft_bundle: the pins and
    the stage's cached UnfairPlay build. Nothing upstream is fetched."""

    def __init__(self, built: Path) -> None:
        self.built, self.asked = built, []
        self.load_pins = lambda path: {"unfairplay": "the unfairplay pin", "go": "the go pin"}

    def build_unfairplay(self, pin, go_pin):
        self.asked.append((pin, go_pin))
        return self.built

    def fetch(self, pin, cache=None):
        pytest.fail(f"fetched {pin}")


class _FakeSandbox:
    """Answers `doctor` for each binary as told; runs nothing."""

    def __init__(self, root: Path, answers: dict[str, subprocess.CompletedProcess]) -> None:
        self.root, self.answers, self.ran = root, answers, []

    def run(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.ran.append(argv)
        return self.answers[argv[0]]


def _doctor(code: int, report: dict | None) -> subprocess.CompletedProcess:
    import json

    return subprocess.CompletedProcess([], code, json.dumps(report) if report is not None else "not json", "")


def _offline_fixture(tmp_path: Path, monkeypatch, bundled, reference):
    built = tmp_path / "bundle-work" / "unfairplay-0123456789ab"
    built.mkdir(parents=True)
    (built / "unfairplay").write_bytes(b"the stage's build, linker-signed")
    stage = _FakeStage(built)
    monkeypatch.setattr(vb, "_stage_module", lambda: stage)
    app = _fake_app(tmp_path)
    helper = app / "Contents" / "Helpers" / "bin" / "unfairplay"
    root = tmp_path / "sandbox"
    reference_copy = root / "unfairplay-stage" / "unfairplay"
    sb = _FakeSandbox(root, {str(helper): bundled, str(reference_copy): reference})
    return app, sb, stage, helper, reference_copy


def test_unfairplays_doctor_is_compared_with_the_stages_own_build_not_upstreams(tmp_path, monkeypatch):
    """The bundled UnfairPlay, signed with the hardened runtime, must answer
    `doctor` as the build it was made from does before the release signed it:
    the stage's build, copied out of its cache. Upstream's prebuilt isn't
    pinned any more."""
    same = {"fairplayd": "found", "created_at": "differs"}
    app, sb, stage, helper, copy = _offline_fixture(
        tmp_path, monkeypatch, _doctor(0, same), _doctor(0, {**same, "created_at": "later"}))
    report = vb.Report()
    vb.check_unfairplay_offline(app, sb, report)
    assert stage.asked == [("the unfairplay pin", "the go pin")]
    assert copy.read_bytes() == b"the stage's build, linker-signed"
    assert sb.ran == [[str(helper), "doctor"], [str(copy), "doctor"]]
    assert report.results == [("unfairplay doctor (offline) reports as the stage's own build does, before the "
                               "release signed it", True)]


@pytest.mark.parametrize("bundled, reference", [
    (_doctor(1, {"fairplayd": "found"}), _doctor(0, {"fairplayd": "found"})),
    (_doctor(0, {"fairplayd": "missing"}), _doctor(0, {"fairplayd": "found"})),
    (_doctor(0, None), _doctor(0, None)),
])
def test_unfairplays_doctor_must_answer_as_the_stages_build_does(tmp_path, monkeypatch, bundled, reference):
    app, sb, *_ = _offline_fixture(tmp_path, monkeypatch, bundled, reference)
    report = vb.Report()
    vb.check_unfairplay_offline(app, sb, report)
    assert [ok for _, ok in report.results] == [False]


def _gb():
    spec = importlib.util.spec_from_file_location("graft_bundle_vb2", REPO / "scripts" / "graft_bundle.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["graft_bundle_vb2"] = module
    spec.loader.exec_module(module)
    return module


def _build_info(go: str, *, modified: str = "false") -> str:
    """`go version -m` on an UnfairPlay built from the pinned commit with `go`."""
    pins = tomllib.loads((REPO / "bundle" / "pins.toml").read_text())
    commit = pins["unfairplay"]["url"].rsplit("/", 1)[1].removesuffix(".tar.gz")
    return (f"unfairplay: {go}\n\tpath\tgithub.com/DrWhax/unfairplay/cmd/unfairplay\n"
            f"\tmod\tgithub.com/DrWhax/unfairplay\tv{pins['unfairplay']['version']}\t\n"
            "\tdep\tgithub.com/spf13/cobra\tv1.10.2\t\n\tbuild\t-trimpath=true\n\tbuild\tCGO_ENABLED=0\n"
            "\tbuild\tGOARCH=arm64\n\tbuild\tGOOS=darwin\n\tbuild\tvcs=git\n"
            f"\tbuild\tvcs.revision={commit}\n\tbuild\tvcs.modified={modified}\n")


@pytest.mark.parametrize("info, ok", [
    (_build_info("go1.26.5"), True),
    (_build_info("go1.26.0"), False),                      # upstream's prebuilt
    (_build_info("go1.26.5", modified="true"), False),     # not the pinned archive's tree
    (_build_info("go1.26.5").replace("\tbuild\tvcs.revision=", "\tbuild\tvcs.revision=0"), False),
])
def test_the_bundled_unfairplay_must_be_the_stages_build_by_its_build_info(tmp_path, monkeypatch, info, ok):
    """Read with the pinned Go's `go version -m`, which reads the file and runs
    nothing of it."""
    app = _fake_app(tmp_path)
    helper = app / "Contents" / "Helpers" / "bin" / "unfairplay"
    go = tmp_path / "toolchain" / "go"
    monkeypatch.setattr(vb, "pinned_go", lambda work: go)
    asked = []

    def run(argv, *, env=None, **kw):
        asked.append(([str(a) for a in argv], env))
        return subprocess.CompletedProcess(argv, 0, info, "")

    monkeypatch.setattr(vb, "run", run)
    report = vb.Report()
    vb.check_unfairplay_build(app, report, tmp_path / "work")
    assert [argv for argv, _ in asked] == [[str(go / "bin" / "go"), "version", "-m", str(helper)]]
    env = asked[0][1]
    assert env["PATH"] == vb.SANDBOX_PATH and env["GOTOOLCHAIN"] == "local" and env["GOENV"] == "off"
    assert Path(env["HOME"]).is_relative_to(tmp_path / "work")
    assert report.results == [("the bundled unfairplay is the stage's build of the pinned commit, unmodified, "
                               "with the pinned Go (its Go build info)", ok)]


def test_unfairplays_build_info_check_fails_when_go_cannot_read_it(tmp_path, monkeypatch):
    monkeypatch.setattr(vb, "pinned_go", lambda work: tmp_path / "go")
    monkeypatch.setattr(vb, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "not a Go binary"))
    report = vb.Report()
    vb.check_unfairplay_build(_fake_app(tmp_path), report, tmp_path / "work")
    assert [ok for _, ok in report.results] == [False]


def test_the_pinned_go_is_the_stages_unpacked_once(tmp_path, monkeypatch):
    gb = _gb()
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    unpacked = []

    def extract(archive, dest):
        unpacked.append((archive, dest))
        (dest / "go" / "bin").mkdir(parents=True)
        (dest / "go" / "bin" / "go").write_text("")
        return dest

    gb.fetch = lambda pin, cache=None: tmp_path / pin.filename if pin == pins["go"] else pytest.fail(pin.name)
    gb.extract = extract
    monkeypatch.setattr(vb, "_stage_module", lambda: gb)
    assert vb.pinned_go(tmp_path / "work") == tmp_path / "work" / "go-toolchain" / "go"
    assert vb.pinned_go(tmp_path / "work") == tmp_path / "work" / "go-toolchain" / "go"
    assert unpacked == [(tmp_path / pins["go"].filename, tmp_path / "work" / "go-toolchain")]


def test_a_sealed_app_gets_the_unfairplay_build_check_before_anything_runs(tmp_path, monkeypatch):
    app = _fake_app(tmp_path)
    order: list[str] = []
    monkeypatch.setattr(vb, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    for name in [n for n in dir(vb) if n.startswith("check_")]:
        monkeypatch.setattr(vb, name, lambda *a, _n=name, **k: order.append(_n) or True)
    monkeypatch.setattr(vb, "runtime_checks", lambda *a, **k: order.append("runtime_checks"))
    vb.verify_app(app, vb.Report(), tmp_path / "work")
    assert order.index("check_unfairplay_build") < order.index("runtime_checks")


# --- Every Go module in a bundled Go program, by its build info (release reviewer) ---------

_LICENCE = "Permission is hereby granted, free of charge, to any person obtaining a copy of this software " * 2
_GO_MODULES = {"ipatool": [("github.com/hashicorp/golang-lru/v2", "v2.0.7"), ("gopkg.in/retry.v1", "v1.0.3")],
               "unfairplay": [("github.com/spf13/cobra", "v1.10.2"), ("howett.net/plist", "v1.0.1")]}


def _go_fixture(tmp_path: Path, monkeypatch) -> tuple[Path, dict[str, Path], list]:
    """An app with both Go tools' notices, the vendored trees the source
    archive would carry, and `go version -m` faked from _GO_MODULES."""
    import tarfile

    gb = _gb()
    app = _fake_app(tmp_path)
    lic = app / "Contents" / "Resources" / "Licenses"
    vendors = {}
    for tool, modules in _GO_MODULES.items():
        (app / "Contents" / "Helpers" / "bin").mkdir(parents=True, exist_ok=True)
        (app / "Contents" / "Helpers" / "bin" / tool).write_bytes(b"a Go program")
        tree = tmp_path / f"{tool}-vendor" / "vendor"
        tree.mkdir(parents=True)
        (tree / "modules.txt").write_text("".join(f"# {m} {v}\n{m}\n" for m, v in modules))
        for m, v in modules:
            (lic / f"{tool}-modules" / f"{m}@{v}").mkdir(parents=True)
            (lic / f"{tool}-modules" / f"{m}@{v}" / "LICENSE").write_text(_LICENCE)
            (tree / m).mkdir(parents=True)
            (tree / m / "x.go").write_text("package x")
        vendors[tool] = tmp_path / f"{tool}-vendor.tar.gz"
        with tarfile.open(vendors[tool], "w:gz") as tf:
            tf.add(tree, arcname="vendor")
    gb.built_sources = lambda pins: [(vendors["ipatool"], "ipatool/vendor.tar.gz"),
                                     (vendors["unfairplay"], "unfairplay/vendor.tar.gz"),
                                     (tmp_path / "x.crate", "ipatool/core-crates/x.crate")]
    monkeypatch.setattr(vb, "_stage_module", lambda: gb)
    read = []

    def go_version_m(binary, work):
        read.append(binary.name)
        deps = "".join(f"\tdep\t{m}\t{v}\t\n" for m, v in _GO_MODULES[binary.name])
        return subprocess.CompletedProcess([], 0, f"{binary}: go1.26.5\n\tpath\tx\n{deps}", "")

    monkeypatch.setattr(vb, "go_version_m", go_version_m)
    return app, vendors, read


_GO_CHECK = ("every Go module in ipatool and unfairplay, by their build info, has its notice in Licenses/ and its "
             "source in the source archive's vendored modules")


def test_every_go_module_in_the_bundled_tools_has_its_notice_and_archived_source(tmp_path, monkeypatch):
    app, _, read = _go_fixture(tmp_path, monkeypatch)
    report = vb.Report()
    vb.check_go_modules(app, report, tmp_path / "work")
    assert read == ["ipatool", "unfairplay"]
    assert report.results == [(_GO_CHECK, True)]


def test_a_go_module_whose_notice_is_gone_from_the_app_fails(tmp_path, monkeypatch, capsys):
    """The mutant notice list: one module's folder removed from Licenses/."""
    app, _, _ = _go_fixture(tmp_path, monkeypatch)
    shutil.rmtree(app / "Contents/Resources/Licenses/unfairplay-modules/howett.net/plist@v1.0.1")
    report = vb.Report()
    vb.check_go_modules(app, report, tmp_path / "work")
    assert report.results == [(_GO_CHECK, False)]
    assert "unfairplay: howett.net/plist v1.0.1: no license notice in unfairplay-modules/" in capsys.readouterr().out


def test_a_go_module_the_archived_vendor_tree_lacks_fails(tmp_path, monkeypatch, capsys):
    import tarfile

    app, vendors, _ = _go_fixture(tmp_path, monkeypatch)
    with tarfile.open(vendors["ipatool"], "w:gz") as tf:   # retry.v1's source left out
        tree = tmp_path / "ipatool-vendor" / "vendor"
        shutil.rmtree(tree / "gopkg.in")
        tf.add(tree, arcname="vendor")
    report = vb.Report()
    vb.check_go_modules(app, report, tmp_path / "work")
    assert report.results == [(_GO_CHECK, False)]
    assert "ipatool: gopkg.in/retry.v1 v1.0.3: no source in ipatool-vendor.tar.gz" in capsys.readouterr().out


def test_a_go_program_whose_build_info_cannot_be_read_fails(tmp_path, monkeypatch):
    app, _, _ = _go_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(vb, "go_version_m", lambda binary, work: subprocess.CompletedProcess([], 1, "", "no"))
    report = vb.Report()
    vb.check_go_modules(app, report, tmp_path / "work")
    assert report.results == [(_GO_CHECK, False)]


def test_a_sealed_app_gets_the_go_module_check_before_anything_runs(tmp_path, monkeypatch):
    app = _fake_app(tmp_path)
    order: list[str] = []
    monkeypatch.setattr(vb, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    for name in [n for n in dir(vb) if n.startswith("check_")]:
        monkeypatch.setattr(vb, name, lambda *a, _n=name, **k: order.append(_n) or True)
    monkeypatch.setattr(vb, "runtime_checks", lambda *a, **k: order.append("runtime_checks"))
    vb.verify_app(app, vb.Report(), tmp_path / "work")
    assert order.index("check_go_modules") < order.index("runtime_checks")


# --- review 2.0, bundle #4: a Mach-O that can't be read is a failure, not a skip -------------

#: A 64-bit Mach-O header cut short: MH_MAGIC_64, then a few bytes.
TRUNCATED_MACHO = bytes.fromhex("cffaedfe0c000001") + b"\0" * 4


def test_a_truncated_mach_o_is_not_skipped(tmp_path):
    from graft_ipa import macho

    root = tmp_path / "Contents"
    (root / "Resources").mkdir(parents=True)
    (root / "Resources" / "notes.txt").write_text("not a Mach-O at all")
    (root / "Resources" / "Hello.class").write_bytes(bytes.fromhex("cafebabe00000041") + b"\0" * 32)
    (root / "Resources" / "cut").write_bytes(TRUNCATED_MACHO)
    with pytest.raises(macho.MachOError):
        vb.mach_o_files(root)
    assert vb.unreadable_mach_o_files(root) == [
        f"Resources/cut: {_error(root / 'Resources' / 'cut')}"]
    (root / "Resources" / "cut").unlink()
    assert vb.mach_o_files(root) == [] and vb.unreadable_mach_o_files(root) == []


def _error(path: Path) -> str:
    from graft_ipa import macho

    try:
        macho.load_commands(path)
    except macho.MachOError as e:
        return str(e)
    raise AssertionError(f"{path} reads")


def test_a_sealed_app_holding_an_unreadable_mach_o_fails_before_any_other_check(tmp_path, monkeypatch):
    """Every static check walks the app's Mach-O files; one they can't read
    would pass them all unseen, on the --zip gate too."""
    app = _fake_app(tmp_path)
    (app / "Contents" / "Resources").mkdir()
    (app / "Contents" / "Resources" / "cut").write_bytes(TRUNCATED_MACHO)
    order: list[str] = []
    monkeypatch.setattr(vb, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    for name in [n for n in dir(vb) if n.startswith("check_") and n != "check_mach_o_files"]:
        monkeypatch.setattr(vb, name, lambda *a, _n=name, **k: order.append(_n) or True)
    monkeypatch.setattr(vb, "runtime_checks", lambda *a, **k: order.append("runtime_checks"))
    report = vb.Report()
    vb.verify_app(app, report, tmp_path / "work")
    assert order == []
    assert report.results[-1] == ("every Mach-O file in the app can be read", False)
    (app / "Contents" / "Resources" / "cut").unlink()
    report = vb.Report()
    vb.verify_app(app, report, tmp_path / "work2")
    assert ("every Mach-O file in the app can be read", True) in report.results and "runtime_checks" in order


# --- review 2.0, bundle #10: root's requirement carries the Developer ID markers -----------------


@pytest.mark.parametrize("answer", [False, "fails"])
def test_a_release_whose_certificate_isnt_read_as_developer_id_fails_the_requirement_check(
        tmp_path, monkeypatch, answer):
    """A release is Developer ID signed, so root's requirement must carry
    Apple's Developer ID markers; without them, an Apple Development
    certificate on the team would satisfy it."""
    from graft_ipa import enrollment
    from graft_ipa.errors import AppStoreError

    def developer_id(app):
        if answer == "fails":
            raise AppStoreError("couldn't read Graft.app's certificates")
        return answer

    monkeypatch.setattr(enrollment, "app_team", lambda a: "TESTTEAM01")
    monkeypatch.setattr(enrollment, "app_is_developer_id", developer_id)
    monkeypatch.setattr(vb, "run", lambda argv, **kw: pytest.fail(f"ran {argv}"))
    report = vb.Report()
    vb.check_unfairplay_requirement(_fake_app(tmp_path), report)
    assert [ok for _, ok in report.results] == [False]
