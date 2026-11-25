from __future__ import annotations

import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest

from graft_ipa import macho

from . import macho_builder as mb
from .toolchain import needs_toolchain, needs_tools

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("graft_bundle", REPO / "scripts" / "graft_bundle.py")
gb = importlib.util.module_from_spec(_spec)
# @dataclass resolves the module's string annotations through sys.modules.
sys.modules["graft_bundle"] = gb
_spec.loader.exec_module(gb)

#: Integration tests that download pinned artifacts and build or install
#: them (OpenSSL takes about three minutes the first time; later runs reuse
#: build/bundle-work). Off unless asked for.
slow = pytest.mark.skipif(os.environ.get("GRAFT_BUNDLE_SLOW") != "1",
                          reason="downloads and builds: set GRAFT_BUNDLE_SLOW=1")


@pytest.fixture(autouse=True)
def _scratch_download_cache(tmp_path_factory, monkeypatch):
    """The downloads cache is the real account's (~/Library/Caches/graft-release),
    found when graft_bundle was imported. What reads CACHE when it runs (Go's
    module cache and its records, the crates) gets a scratch one instead, so
    no test writes there; fetch()'s default, which the slow tests use, keeps
    the real one."""
    monkeypatch.setattr(gb, "CACHE", tmp_path_factory.mktemp("bundle-cache"))


def test_the_pins_file_names_every_artifact_with_a_hash():
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    assert set(pins) == {"python", "ipatool", "unfairplay", "zsign", "openssl", "cyan",
                         "libplist", "ldid", "cctools", "insert_dylib", "go",
                         "python-build-standalone", "hacl"}
    # ipatool is its v2.8.0 source. No Unicorn: ipatool downloads its own
    # (the combined fix check's medium #2, option (a)).
    assert pins["ipatool"].url.endswith("/archive/72c47d538b726df397fbf17bb3af809d69ef51b7.tar.gz")
    # UnfairPlay is its v0.4.0 source, the tag's commit, not upstream's prebuilt release.
    assert pins["unfairplay"].url == ("https://github.com/DrWhax/unfairplay/archive/"
                                      "d7503082a59e3dcfdd3ea7a70434dcecaaaf07df.tar.gz")
    assert pins["unfairplay"].version == "0.4.0" and "unfairplay" in gb.BUILT_FROM_SOURCE
    assert not [name for name in pins if "unicorn" in name]
    assert pins["go"].url == "https://go.dev/dl/go1.26.5.darwin-arm64.tar.gz"
    for name, pin in pins.items():
        assert pin.url.startswith("https://"), name
        assert len(pin.sha256) == 64 and int(pin.sha256, 16) >= 0, name
        assert pin.version, name
    assert pins["cctools"].builds == ("lipo", "otool", "install_name_tool")
    assert {b for pin in pins.values() for b in pin.builds} | {"ldid", "insert_dylib"} >= set(gb.CYAN_HELPERS)


# --- Intel Macs: Graft-Intel.zip, the x86_64 bundle --------------------------------------------

#: The arm64 bundle's two per-architecture files, as 2.2 pinned them.
ARM64_PYTHON = ("https://github.com/astral-sh/python-build-standalone/releases/download/20261026/"
                "cpython-3.13.18+20261026-aarch64-apple-darwin-install_only.tar.gz",
                "e5af2fde6a937a0e0c795a0c595c4efb70578d57463a2657c819992b9b9acbac")
ARM64_ZSIGN = ("https://github.com/zhlynn/zsign/releases/download/v1.1.3/zsign-macos-arm64.tar.gz",
               "2790ec58a55db5f4282843d0cdbb0b587d19b972331b5285aee2de67f4ab705e")


def test_the_arm64_bundle_keeps_every_pin_it_had():
    """The default is the arm64 bundle, exactly as before: the variants an
    Intel bundle takes change none of its pins."""
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    assert gb.load_pins(REPO / "bundle" / "pins.toml", "arm64") == pins
    assert (pins["python"].url, pins["python"].sha256) == ARM64_PYTHON
    assert (pins["zsign"].url, pins["zsign"].sha256) == ARM64_ZSIGN
    assert not [n for n, p in pins.items() if p.arch or p.from_source], "every arm64 pin is its base table"
    assert gb.pin_kind("zsign", pins) == "artifact" and "unfairplay" in pins


def test_an_intel_bundle_takes_the_x86_64_python_zsign_built_from_source_and_no_unfairplay():
    arm = gb.load_pins(REPO / "bundle" / "pins.toml")
    intel = gb.load_pins(REPO / "bundle" / "pins.toml", "x86_64")
    assert set(intel) == set(arm) - {"unfairplay"}, "UnfairPlay runs iOS apps, which only Apple silicon can"
    python, zsign = intel["python"], intel["zsign"]
    assert (python.url, python.sha256) == (
        "https://github.com/astral-sh/python-build-standalone/releases/download/20261026/"
        "cpython-3.13.18+20261026-x86_64-apple-darwin-install_only.tar.gz",
        "708be9e7df297f68bdae589ea9e62a621db5abe7708f79e851c41410347452d1")
    # zsign has no macOS x86_64 release file: the same release's source, the tag's commit.
    assert (zsign.url, zsign.sha256) == (
        "https://github.com/zhlynn/zsign/archive/9299685bab7ef99827e06e5dfda93af747386590.tar.gz",
        "97bcc2dbbc0dc5f092dc4a33cbe1f9fdbd5c83bdc291da23db45044d4396270e")
    assert zsign.from_source and gb.pin_kind("zsign", intel) == "built-from-source"
    assert gb.source_commit(zsign) == ("https://github.com/zhlynn/zsign.git", "9299685bab7ef99827e06e5dfda93af747386590")
    for name in ("python", "zsign"):
        assert intel[name].arch == "x86_64"
        assert (intel[name].version, intel[name].license) == (arm[name].version, arm[name].license), name
    for name in set(intel) - {"python", "zsign"}:
        assert intel[name] == arm[name], f"{name} is source or the host's toolchain: the same for both"


def _pins_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "pins.toml"
    path.write_text(text)
    return path


_BASE = f'version = "1"\nurl = "https://example.invalid/arm64.tar.gz"\nsha256 = "{"a" * 64}"\nlicense = "MIT"\n'
_VARIANT = f'url = "https://example.invalid/x86_64.tar.gz"\nsha256 = "{"b" * 64}"\n'


def test_a_pin_whose_file_is_a_mac_binary_needs_a_variant_for_intel(tmp_path):
    pins = _pins_file(tmp_path, f"[python]\n{_BASE}")
    assert gb.load_pins(pins)["python"].url.endswith("/arm64.tar.gz")
    with pytest.raises(gb.BundleError, match=r"\[python\] has no \[python.x86_64\]"):
        gb.load_pins(pins, "x86_64")
    # Source is the same for both: no variant needed.
    assert gb.load_pins(_pins_file(tmp_path, f"[openssl]\n{_BASE}"), "x86_64")["openssl"].url.endswith("/arm64.tar.gz")


@pytest.mark.parametrize("text, error", [
    (f"[python]\n{_BASE}[python.ppc]\n{_VARIANT}", r"\[python.ppc\], which names no architecture"),
    (f"[python]\n{_BASE}[python.arm64]\n{_VARIANT}", r"\[python.arm64\], which names no architecture"),
    (f'[python]\n{_BASE}[python.x86_64]\n{_VARIANT}license = "GPL-3.0"\n', "may set only"),
    (f'[python]\n{_BASE}[python.x86_64]\nurl = "https://example.invalid/x"\n', "needs both url and sha256"),
    (f'[zsign]\n{_BASE}[zsign.x86_64]\n{_VARIANT}build = "magic"\n', 'build = "magic"'),
    (f'[unfairplay]\n{_BASE}archs = ["x86_64"]\n', "must include arm64"),
    (f'[unfairplay]\n{_BASE}archs = ["arm64", "ppc"]\n', "ppc"),
])
def test_a_variant_or_architecture_list_that_makes_no_sense_is_refused(tmp_path, text, error):
    with pytest.raises(gb.BundleError, match=error):
        gb.load_pins(_pins_file(tmp_path, text), "x86_64")


def test_an_architecture_no_release_is_built_for_is_refused(tmp_path):
    with pytest.raises(gb.BundleError, match="riscv64"):
        gb.load_pins(_pins_file(tmp_path, f"[openssl]\n{_BASE}"), "riscv64")


def test_a_pin_for_arm64_only_is_left_out_of_an_intel_bundle(tmp_path):
    pins = _pins_file(tmp_path, f'[unfairplay]\n{_BASE}archs = ["arm64"]\n[openssl]\n{_BASE}')
    assert set(gb.load_pins(pins)) == {"unfairplay", "openssl"}
    assert set(gb.load_pins(pins, "x86_64")) == {"openssl"}


def test_every_architectures_pins_are_listed_for_the_shared_source_archive():
    by_arch = gb.load_pins_by_arch(REPO / "bundle" / "pins.toml")
    assert list(by_arch) == ["arm64", "x86_64"]
    assert by_arch["x86_64"]["zsign"].from_source and not by_arch["arm64"]["zsign"].from_source


def test_ipatools_licence_expression_covers_what_is_compiled_into_it():
    """RC audit #6: it left out mpb (the Unlicense) and xi2/xz (public
    domain), two Go modules in the binary; and since RC audit #1 the crates
    in its 1Password SDK core are listed with it, so each crate's license
    must be one the expression gives, or offer one."""
    terms = set(gb.load_pins(REPO / "bundle" / "pins.toml")["ipatool"].license.split(" AND "))
    assert {"Unlicense", "LicenseRef-public-domain"} <= terms
    for (crate, version), pinned in gb.pinned_crates().items():
        assert "(" not in pinned["license"], crate
        choices = [set(alt.split(" AND ")) for alt in pinned["license"].replace("/", " OR ").split(" OR ")]
        assert any(choice <= terms for choice in choices), (crate, version, pinned["license"])
    notes = gb.FOLDER_NOTES["ipatool-modules"]
    assert "mpb" in notes and "Unlicense" in notes and "xi2/xz" in notes and "public domain" in notes


def test_no_note_or_message_names_a_macos_the_stage_doesnt_build_for():
    """RC audit #5, #10, #17: the cyan helpers' notes and the plan's error
    still said macOS 15 after the minimum moved to 14.0."""
    assert gb.MACOS_MIN == "14.0"
    for path in (REPO / "scripts/graft_bundle.py", REPO / "bundle/pins.toml", REPO / "THIRD_PARTY_NOTICES.md"):
        assert "macOS 15" not in path.read_text(), path.name


def test_third_party_notices_counts_the_python_distributions_the_app_holds():
    """RC audit #4: it said 101 (99 from PyPI) after frida went. The app
    holds every runtime-lock pin, plus graft and cyan; the build backends
    are uninstalled."""
    from_pypi = len(gb.lock_names(gb.RUNTIME_LOCK))
    text = (REPO / "THIRD_PARTY_NOTICES.md").read_text()
    assert f"The embedded Python holds {from_pypi + 2} distributions." in text
    assert f"The other {from_pypi} are installed from PyPI" in text


def test_fetch_refuses_a_file_whose_hash_differs(tmp_path, monkeypatch):
    body = b"not the pinned bytes"
    pin = gb.Pin(name="x", url="https://example.invalid/x.tar.gz", sha256="0" * 64, license="MIT")
    monkeypatch.setattr(gb, "_download", lambda url, dest: dest.write_bytes(body))
    with pytest.raises(gb.BundleError, match="SHA-256 mismatch"):
        gb.fetch(pin, tmp_path)
    assert not (tmp_path / "x.tar.gz").exists(), "a file that failed its check is not kept"
    assert list(tmp_path.iterdir()) == [], "nor is its partial download"


def test_fetch_reuses_a_cached_file_that_matches(tmp_path, monkeypatch):
    body = b"pinned bytes"
    pin = gb.Pin(name="x", url="https://example.invalid/x.tar.gz",
                 sha256=hashlib.sha256(body).hexdigest(), license="MIT")
    (tmp_path / "x.tar.gz").write_bytes(body)
    monkeypatch.setattr(gb, "_download", lambda *a: pytest.fail("downloaded despite a good cache"))
    assert gb.fetch(pin, tmp_path) == tmp_path / "x.tar.gz"


def test_fetch_replaces_a_cached_file_that_does_not_match(tmp_path, monkeypatch):
    good = b"pinned bytes"
    pin = gb.Pin(name="x", url="https://example.invalid/x.tar.gz",
                 sha256=hashlib.sha256(good).hexdigest(), license="MIT")
    (tmp_path / "x.tar.gz").write_bytes(b"stale")
    monkeypatch.setattr(gb, "_download", lambda url, dest: dest.write_bytes(good))
    assert gb.fetch(pin, tmp_path).read_bytes() == good


def test_extracting_refuses_members_that_escape_the_destination(tmp_path):
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tf:
        info = tarfile.TarInfo("../outside.txt")
        info.size = 0
        tf.addfile(info)
    with pytest.raises(gb.BundleError, match="outside"):
        gb.extract(evil, tmp_path / "out")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _lock_entries(text: str) -> dict[str, str]:
    """Each requirement in a pip-compile lock: its name, and its whole entry
    (the `name==version` line plus its backslash-continued `--hash` lines)."""
    entries: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            current = None
            continue
        if not line[0].isspace():
            current = _norm(re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", line).group(0))
            assert current not in entries, f"{current} is locked twice"
            entries[current] = line
        elif current is not None:
            entries[current] += "\n" + line
    return entries


def _assert_pinned_and_hashed(entries: dict[str, str]) -> None:
    assert entries, "the lock lists no requirements"
    for name, entry in entries.items():
        assert re.search(r"^\S+==\S+", entry), f"{name} is not pinned to one version"
        assert re.search(r"--hash=sha256:[0-9a-f]{64}\b", entry), f"{name} has no sha256 hash"


def test_the_lock_hashes_every_requirement_and_covers_every_runtime_dependency():
    entries = _lock_entries((REPO / "bundle" / "requirements.lock").read_text())
    _assert_pinned_and_hashed(entries)
    deps = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["dependencies"]
    for dep in deps:
        name = _norm(re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", dep).group(0))
        assert name in entries, f"pyproject's {dep!r} is missing from bundle/requirements.lock"


def test_the_build_lock_hashes_both_backends_and_agrees_with_the_runtime_lock():
    build = _lock_entries((REPO / "bundle" / "build-requirements.lock").read_text())
    runtime = _lock_entries((REPO / "bundle" / "requirements.lock").read_text())
    _assert_pinned_and_hashed(build)
    graft_backend = tomllib.loads((REPO / "pyproject.toml").read_text())["build-system"]["requires"]
    for req in [*graft_backend, "setuptools"]:   # setuptools: cyan has only a setup.py
        name = _norm(re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", req).group(0))
        assert name in build, f"{req!r} is missing from bundle/build-requirements.lock"
    for name in build.keys() & runtime.keys():
        assert build[name].split()[0] == runtime[name].split()[0], f"the two locks pin {name} differently"


def test_the_repoint_plan_maps_every_homebrew_openssl_link_to_frameworks(tmp_path):
    zsign = tmp_path / "zsign"
    zsign.write_bytes(mb.thin([
        mb.dylib_cmd("/opt/homebrew/opt/openssl@3/lib/libssl.3.dylib"),
        mb.dylib_cmd("/opt/homebrew/opt/openssl@3/lib/libcrypto.3.dylib"),
        mb.dylib_cmd("/usr/lib/libc++.1.dylib"),
    ]))
    plan = gb.openssl_repoint_plan(zsign)
    assert plan == [
        ("/opt/homebrew/opt/openssl@3/lib/libssl.3.dylib", "@executable_path/../../Frameworks/libssl.3.dylib"),
        ("/opt/homebrew/opt/openssl@3/lib/libcrypto.3.dylib", "@executable_path/../../Frameworks/libcrypto.3.dylib"),
    ]


def test_a_zsign_linking_an_unexpected_openssl_major_is_refused(tmp_path):
    zsign = tmp_path / "zsign"
    zsign.write_bytes(mb.thin([mb.dylib_cmd("/opt/homebrew/opt/openssl@4/lib/libssl.4.dylib")]))
    with pytest.raises(gb.BundleError, match=r"libssl\.4"):
        gb.openssl_repoint_plan(zsign)


def _dyld_loaded(stderr: str) -> set[str]:
    """Real paths of the images DYLD_PRINT_LIBRARIES says were loaded.

    Each line reads `dyld[<pid>]: <UUID> /path/to/image`."""
    paths = set()
    for line in stderr.splitlines():
        m = re.match(r"dyld\[\d+\]: (?:<[0-9A-F-]+> )?(/.*)$", line)
        if m:
            paths.add(os.path.realpath(m.group(1)))
    return paths


@slow
def test_the_staged_zsign_runs_on_the_bundled_openssl(tmp_path):
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    prefix = gb.build_openssl(pins["openssl"])
    unpacked = gb.extract(gb.fetch(pins["zsign"]), tmp_path / "zsign-release")
    contents = tmp_path / "Contents"
    gb.stage_openssl_and_zsign(prefix, unpacked / "zsign", contents)

    fw = contents / "Frameworks"
    zsign = contents / "Helpers" / "bin" / "zsign"
    staged = [zsign, fw / "libssl.3.dylib", fw / "libcrypto.3.dylib", fw / "ossl-modules" / "legacy.dylib"]
    assert not [n for n in macho.dylib_names(zsign) if "/opt/homebrew" in n]
    for f in staged:
        for name in macho.dylib_names(f):
            assert name.startswith(("/usr/lib/", "/System/", "@")), f"{f.name} links {name}"

    run = subprocess.run([str(zsign), "-v"], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "DYLD_PRINT_LIBRARIES": "1"})
    assert run.returncode == 0, run.stderr[-2000:]
    loaded = _dyld_loaded(run.stderr)
    assert os.path.realpath(fw / "libssl.3.dylib") in loaded
    assert os.path.realpath(fw / "libcrypto.3.dylib") in loaded
    assert not [p for p in loaded if p.startswith("/opt/homebrew")]


def _tree(root: Path, paths: list[str]) -> None:
    for p in paths:
        f = root / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")


def test_prune_removes_what_the_bundle_never_runs(tmp_path):
    py = tmp_path / "GraftPython"
    keep = ["bin/python3.13", "lib/python3.13/json/__init__.py",
            "lib/libpython3.13.dylib", "lib/python3.13/lib-dynload/_dbm.cpython-313-darwin.so",
            "lib/python3.13/site-packages/graft_ipa/__init__.py",
            "lib/python3.13/site-packages/PIL/_imaging.cpython-313-darwin.so",
            "lib/python3.13/site-packages/cyan/tools/Darwin/arm64/ldid",
            "lib/python3.13/site-packages/cyan/extras/CydiaSubstrate.framework/CydiaSubstrate"]
    gone = ["bin/pip3", "bin/idle3", "bin/graft", "bin/pymobiledevice3", "lib/python3.13/site-packages/pip/__init__.py",
            "lib/python3.13/site-packages/pip-26.2.2.dist-info/METADATA",
            "lib/python3.13/ensurepip/__init__.py", "lib/python3.13/idlelib/x.py",
            "lib/python3.13/tkinter/__init__.py", "lib/tcl9.0/init.tcl", "lib/tk9.0/tk.tcl",
            "lib/tcl9/9.0/x.tm", "lib/itcl4.3.8/itcl.tcl", "lib/thread3.0.6/libtcl9thread3.0.6.dylib",
            "lib/libtcl9.0.dylib", "lib/libtcl9tk9.0.dylib",
            "lib/python3.13/lib-dynload/_tkinter.cpython-313-darwin.so",
            "lib/python3.13/site-packages/PIL/_imagingtk.cpython-313-darwin.so",
            "lib/python3.13/site-packages/PIL/_imagingtk.pyi",
            "lib/python3.13/test/test_x.py", "include/python3.13/Python.h",
            "lib/python3.13/site-packages/cyan/tools/Darwin/x86_64/ldid",
            "lib/python3.13/site-packages/cyan/tools/Darwin/iOS/ldid",
            "lib/python3.13/site-packages/cyan/tools/Linux/x86_64/ldid",
            # Notarization candidates (critic): LLVM bitcode, and remotezip2's
            # stray top-level test package with a zip inside it.
            "lib/python3.13/config-3.13-darwin/python.o",
            "lib/python3.13/site-packages/test_data/zip64.zip",
            # Nothing at run time reads these; they were half of the bundle's symlinks.
            "share/man/man1/python3.13.1", "lib/pkgconfig/python-3.13.pc"]
    _tree(py, [*keep, "lib/python3.13/config-3.13-darwin/Makefile", *gone])
    for link, target in (("bin/python3", "python3.13"), ("bin/python", "python3.13"), ("bin/pip", "pip3"),
                         ("share/man/man1/python3.1", "python3.13.1"), ("lib/pkgconfig/python3.pc", "python-3.13.pc")):
        (py / link).symlink_to(target)
    gb.prune_python(py)
    for p in keep:
        assert (py / p).exists(), p
    for p in gone:
        assert not (py / p).exists(), p
    # The critic: a third-party unarchiver that writes symlinks as files, or
    # skips them, breaks the seal. The launcher execs bin/python3.13 itself.
    assert not [p for p in py.rglob("*") if p.is_symlink()], "no symlinks are left"
    assert (py / "lib/python3.13/config-3.13-darwin/Makefile").exists(), "only python.o goes from config"


#: A Windows DLL's first bytes: MZ, then at 0x3c the offset of "PE\\0\\0".
PE_DLL = b"MZ" + b"\0" * 58 + (64).to_bytes(4, "little") + b"PE\0\0" + b"\0" * 32


def test_prune_removes_upstream_wheel_content_the_app_never_loads(tmp_path):
    """RC audit #2 and #7: about 38 MB on disk of other projects' tests,
    stubs, GIFs, developer scripts and Windows DLLs (Wintun's, under a
    license that isn't open source) shipped in every download."""
    py = tmp_path / "GraftPython"
    site = "lib/python3.13/site-packages"
    keep = ["bin/python3.13", f"{site}/pytun_pmd3/__init__.py", f"{site}/pytun_pmd3/wintun.py", f"{site}/pytun_pmd3/darwin.py",
            f"{site}/jedi/__init__.py", f"{site}/Crypto/__init__.py", f"{site}/Crypto/Cipher/AES.py",
            # IPython.core imports IPython.testing.skipdoctest at import time.
            f"{site}/IPython/testing/skipdoctest.py", f"{site}/IPython/core/display.py",
            f"{site}/certifi/cacert.pem", f"{site}/ifaddr/__init__.py", f"{site}/annotated_types/__init__.py",
            f"{site}/pymobiledevice3/__init__.py", f"{site}/fastapi/__init__.py", f"{site}/typer/main.py",
            f"{site}/pexpect/bashrc.sh", f"{site}/PIL/.dylibs/libjpeg.62.dylib"]
    gone = [*(f"{site}/pytun_pmd3/wintun/bin/{arch}/wintun.dll" for arch in ("amd64", "x86", "arm", "arm64")),
            f"{site}/pytun_pmd3/wintun/LICENSE.txt", f"{site}/pytun_pmd3/wintun/include/wintun.h",
            f"{site}/jedi/third_party/typeshed/stdlib/builtins.pyi",
            f"{site}/jedi/third_party/django-stubs/django-stubs/__init__.pyi",
            f"{site}/Crypto/SelfTest/__init__.py", f"{site}/Crypto/SelfTest/Cipher/test_AES.py",
            f"{site}/certifi/tests/test_certifi.py", f"{site}/traitlets/tests/utils.py",
            f"{site}/IPython/terminal/tests/test_x.py", f"{site}/ifaddr/test_ifaddr.py",
            f"{site}/annotated_types/test_cases.py", f"{site}/gifs/demo.gif",
            f"{site}/misc/usbmux_sniff.sh", f"{site}/misc/linux-vm/run.sh", f"{site}/overrides/main.html",
            f"{site}/typer/.agents/skills/typer/SKILL.md", f"{site}/fastapi/.agents/skills/fastapi/SKILL.md"]
    _tree(py, [*keep, *gone])
    for dll in gone[:4]:
        (py / dll).write_bytes(PE_DLL)
    gb.prune_python(py)
    for f in keep:
        assert (py / f).exists(), f
    for f in gone:
        assert not (py / f).exists(), f


def test_a_windows_binary_left_after_pruning_stops_the_stage(tmp_path):
    """A new wheel bringing one in stops the build, to be looked at: Wintun's
    DLLs came under a license that isn't open source, and nothing on a Mac
    loads a DLL."""
    py = tmp_path / "GraftPython"
    _tree(py, ["bin/python3.13", "lib/python3.13/site-packages/newpkg/helper.dll"])
    (py / "lib/python3.13/site-packages/newpkg/helper.dll").write_bytes(PE_DLL)
    with pytest.raises(gb.BundleError, match=r"newpkg/helper\.dll"):
        gb.prune_python(py)
    assert gb.windows_binaries(tmp_path) == [py / "lib/python3.13/site-packages/newpkg/helper.dll"]
    (py / "lib/python3.13/site-packages/newpkg/helper.dll").unlink()
    (py / "lib/python3.13/site-packages/newpkg/NOTES").write_bytes(b"MZ is how this text begins, " * 4)
    (py / "lib/python3.13/site-packages/newpkg/plugin").write_bytes(PE_DLL)
    assert gb.windows_binaries(tmp_path) == [py / "lib/python3.13/site-packages/newpkg/plugin"]
    (py / "lib/python3.13/site-packages/newpkg/plugin").unlink()
    (py / "lib/python3.13/site-packages/newpkg/tool.exe").write_bytes(b"not even PE, but named like a program")
    assert gb.windows_binaries(tmp_path) == [py / "lib/python3.13/site-packages/newpkg/tool.exe"]


def test_the_macho_list_is_deepest_first_and_skips_symlinks_and_lookalikes(tmp_path):
    c = tmp_path / "Contents"
    (c / "Resources/GraftPython/lib").mkdir(parents=True)
    (c / "Helpers/bin").mkdir(parents=True)
    (c / "Resources/GraftPython/lib/a.so").write_bytes(mb.thin([mb.rpath_cmd("@loader_path")]))
    (c / "Helpers/bin/tool").write_bytes(mb.thin([]))
    (c / "Helpers/graft").write_bytes(mb.fat([(mb.CPU_ARM64, mb.thin([]))]))
    (c / "Resources/GraftPython/lib/Some.class").write_bytes(b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + b"\0" * 32)
    (c / "Resources/GraftPython/lib/a.py").write_text("print('not a Mach-O')\n")
    (c / "Resources/GraftPython/lib/link.so").symlink_to("a.so")
    got = gb.macho_files(c)
    assert got == [c / "Resources/GraftPython/lib/a.so", c / "Helpers/bin/tool", c / "Helpers/graft"]


def test_a_mach_o_whose_load_commands_cannot_be_read_is_refused_not_skipped(tmp_path):
    """Skipping it would leave a Mach-O unsigned inside the signed app."""
    c = tmp_path / "Contents"
    (c / "Helpers").mkdir(parents=True)
    (c / "Helpers/broken").write_bytes(mb.thin([mb.rpath_cmd("@loader_path")])[:40])
    with pytest.raises(gb.BundleError, match="broken"):
        gb.macho_files(c)


def _libpython_stage(tmp_path: Path, *so_links: str) -> Path:
    """A Contents with the interpreter, libpython beside it, and one extension
    module linking `so_links`."""
    c = tmp_path / "Contents"
    gp = c / "Resources/GraftPython"
    (gp / "bin").mkdir(parents=True)
    (gp / "lib/python3.13/lib-dynload").mkdir(parents=True)
    (gp / "bin/python3.13").write_bytes(mb.thin([mb.dylib_cmd("/usr/lib/libSystem.B.dylib")]))
    (gp / "lib/libpython3.13.dylib").write_bytes(mb.thin([mb.dylib_cmd("/usr/lib/libSystem.B.dylib")]))
    (gp / "lib/python3.13/lib-dynload/_x.cpython-313-darwin.so").write_bytes(
        mb.thin([mb.dylib_cmd(n) for n in so_links]))
    (c / "Helpers").mkdir()
    (c / "Helpers/graft").write_bytes(mb.thin([]))
    return c


def test_libpython_is_pruned_when_no_staged_mach_o_links_it(tmp_path):
    c = _libpython_stage(tmp_path, "/usr/lib/libSystem.B.dylib")
    assert gb.prune_unlinked_libpython(c) == []
    assert not (c / "Resources/GraftPython/lib/libpython3.13.dylib").exists()
    assert (c / "Resources/GraftPython/bin/python3.13").exists()


def test_libpython_is_kept_when_a_staged_mach_o_links_it(tmp_path):
    c = _libpython_stage(tmp_path, "@rpath/libpython3.13.dylib")
    assert gb.prune_unlinked_libpython(c) == [Path("Resources/GraftPython/lib/python3.13/lib-dynload/"
                                                   "_x.cpython-313-darwin.so")]
    assert (c / "Resources/GraftPython/lib/libpython3.13.dylib").exists()


def _files_naming(root: Path, needles: set[str]) -> list[Path]:
    raw = {n.encode() for n in needles}
    return [p for p in root.rglob("*")
            if p.is_file() and not p.is_symlink() and any(n in p.read_bytes() for n in raw)]


def _wheel_name(filename: str) -> str:
    return _norm(filename.rsplit("/", 1)[-1].split("-", 1)[0])


@slow
def test_the_staged_python_runs_the_cli_with_everything_it_imports(tmp_path, monkeypatch):
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    contents = tmp_path / "Contents"
    real_run = subprocess.run
    pip_output: list[str] = []

    def spy(argv, **kw):
        argv = [str(a) for a in argv]
        if argv[3:5] == ["pip", "install"]:
            done = real_run(argv, **{**kw, "capture_output": True, "text": True})
            pip_output.append(done.stdout + done.stderr)
            return done
        return real_run(argv, **kw)

    monkeypatch.setattr(gb.subprocess, "run", spy)
    gp = gb.stage_python(pins, contents)
    monkeypatch.undo()

    # Nothing but the two hash-locked sets came from an index.
    log = "\n".join(pip_output)
    locked = (set(_lock_entries((REPO / "bundle" / "requirements.lock").read_text()))
              | set(_lock_entries((REPO / "bundle" / "build-requirements.lock").read_text())))
    collected = re.findall(r"^Collecting ([A-Za-z0-9][A-Za-z0-9._-]*)", log, re.M)
    downloaded = re.findall(r"^\s*Downloading (\S+)", log, re.M)
    assert len(collected) == len(locked) and downloaded, "pip's output no longer reads as expected"
    assert {_norm(n) for n in collected} == locked
    assert {_wheel_name(f) for f in downloaded} <= locked
    assert "Installing build dependencies" not in log, "pip used build isolation"
    site = gp / "lib" / "python3.13" / "site-packages"
    assert not list(site.glob("hatchling*")) and not list(site.glob("setuptools*"))
    assert not (site / "_distutils_hack").exists()

    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    assert gb.check_python(gp / "bin" / "python3.13") == f"graft {version}"
    assert sorted(p.name for p in (gp / "bin").iterdir()) == ["python", "python3", "python3.13"]
    lib = gp / "lib" / "python3.13"
    assert not (lib / "site-packages" / "pip").exists()
    assert not (lib / "tkinter").exists()
    assert (lib / "site-packages" / "cyan" / "tools" / "Darwin" / "arm64" / "ldid").is_file()
    uncompiled = [p for p in lib.rglob("*.py")
                  if not (p.parent / "__pycache__" / f"{p.stem}.cpython-313.pyc").is_file()]
    assert not uncompiled, uncompiled[:5]
    assert not list(lib.glob("site-packages/*.dist-info/direct_url.json"))
    leaks = _files_naming(gp, {str(contents), str(contents.resolve()), str(REPO)})
    assert not leaks, leaks[:5]


@needs_toolchain  # it compiles bundle/launcher.c
def test_the_launcher_execs_the_embedded_python_through_a_symlink(tmp_path):
    contents = tmp_path / "Contents"
    fake_py = contents / "Resources/GraftPython/bin/python3.13"
    fake_py.parent.mkdir(parents=True)
    fake_py.write_text('#!/bin/sh\nfor a in "$0" "$@"; do printf "%s\\n" "$a"; done\n')
    fake_py.chmod(0o755)
    gb.build_launchers(contents)
    link = tmp_path / "bin" / "graft"
    link.parent.mkdir()
    link.symlink_to(contents / "Helpers" / "graft")
    out = subprocess.run([str(link), "--check", "x y"], capture_output=True, text=True, check=True).stdout.splitlines()
    assert out == [str(fake_py.resolve()), "-I", "-B", "-m", "graft_ipa", "--check", "x y"]
    cyan = subprocess.run([str(contents / "Helpers/bin/cyan"), "-h"], capture_output=True, text=True, check=True)
    assert cyan.stdout.splitlines() == [str(fake_py.resolve()), "-I", "-B", "-m", "cyan", "-h"]


def _launchers_with(tmp_path: Path, body: str) -> Path:
    contents = tmp_path / "Contents"
    fake_py = contents / "Resources/GraftPython/bin/python3.13"
    fake_py.parent.mkdir(parents=True)
    fake_py.write_text("#!/bin/sh\n" + body)
    fake_py.chmod(0o755)
    gb.build_launchers(contents)
    return contents


@needs_toolchain  # it compiles bundle/launcher.c
def test_a_launcher_whose_python_is_missing_says_the_app_is_damaged(tmp_path):
    """Review #18: the app maps exit 126 and this prefix to its "Graft.app is
    damaged, download it again" screen. A generic 127 left an empty app."""
    contents = _launchers_with(tmp_path, "exit 0\n")
    (contents / "Resources/GraftPython/bin/python3.13").unlink()
    for launcher in ("Helpers/graft", "Helpers/bin/cyan"):
        r = subprocess.run([str(contents / launcher), "--version"], capture_output=True, text=True)
        assert r.returncode == gb.LAUNCHER_DAMAGED_EXIT == 126, (launcher, r)
        assert r.stderr.startswith(gb.LAUNCHER_DAMAGED_PREFIX) and r.stderr.count("\n") == 1, r.stderr
        assert "Python is missing" in r.stderr and "GraftPython/bin/python3.13" in r.stderr
    assert gb.LAUNCHER_DAMAGED_PREFIX == "graft: Graft.app is damaged: "


@needs_toolchain  # it compiles bundle/launcher.c
def test_a_launcher_whose_python_cannot_run_says_the_app_is_damaged(tmp_path):
    contents = _launchers_with(tmp_path, "exit 0\n")
    (contents / "Resources/GraftPython/bin/python3.13").chmod(0o644)
    r = subprocess.run([str(contents / "Helpers/graft")], capture_output=True, text=True)
    assert r.returncode == 126 and r.stderr.startswith("graft: Graft.app is damaged: can't run its Python"), r


@needs_toolchain  # it compiles bundle/launcher.c
def test_the_launcher_drops_the_two_variables_python_reads_despite_dash_i(tmp_path):
    """Review #39/#42: CPython on macOS takes sys.executable from
    PYTHONEXECUTABLE or __PYVENV_LAUNCHER__ even under -I, and graft finds
    Graft.app's tools from sys.executable."""
    contents = _launchers_with(tmp_path, 'printf "%s|%s|%s\\n" "${PYTHONEXECUTABLE-unset}" '
                                         '"${__PYVENV_LAUNCHER__-unset}" "${KEEP-unset}"\n')
    env = {"PATH": "/usr/bin:/bin", "PYTHONEXECUTABLE": "/tmp/Evil.app/Contents/Resources/x",
           "__PYVENV_LAUNCHER__": "/tmp/Evil.app/x", "KEEP": "kept"}
    r = subprocess.run([str(contents / "Helpers/graft")], capture_output=True, text=True, env=env, check=True)
    assert r.stdout.strip() == "unset|unset|kept"


@needs_toolchain
def test_the_launcher_started_with_no_argv_at_all_runs_cleanly(tmp_path):
    """Review #37: with argc == 0 the argv handed to execv had no room for its
    NULL, so execv read whatever followed the allocation. An interposed calloc
    makes that memory non-NULL, which an argv without its own NULL can't survive."""
    contents = _launchers_with(tmp_path, 'printf "%s\\n" "$@"\n')
    (tmp_path / "dirty.c").write_text(
        "#include <stdlib.h>\n#include <string.h>\n"
        "static void *dirty_calloc(size_t n, size_t s) { size_t t = n * s; unsigned char *p = malloc(t + 64);"
        " memset(p, 0, t); memset(p + t, 0xAA, 64); return p; }\n"
        "__attribute__((used)) static struct { const void *r, *o; } interpose[] "
        "__attribute__((section(\"__DATA,__interpose\"))) = {{(const void *)dirty_calloc, (const void *)calloc}};\n")
    (tmp_path / "spawn.c").write_text("#include <unistd.h>\nint main(int c, char **v) { char *none[] = {0}; "
                                      "execv(v[1], none); return 99; }\n")
    subprocess.run(["clang", "-dynamiclib", "-o", str(tmp_path / "dirty.dylib"), str(tmp_path / "dirty.c")], check=True)
    subprocess.run(["clang", "-o", str(tmp_path / "spawn"), str(tmp_path / "spawn.c")], check=True)
    r = subprocess.run([str(tmp_path / "spawn"), str(contents / "Helpers/graft")], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "DYLD_INSERT_LIBRARIES": str(tmp_path / "dirty.dylib")},
                       timeout=60)
    assert r.returncode == 0, r
    assert r.stdout.splitlines() == ["-I", "-B", "-m", "graft_ipa"]


def test_graft_and_cyan_are_built_only_with_the_hash_locked_backends(tmp_path, monkeypatch):
    """No pip install in the stage may fetch anything unhashed: the two
    projects built from source get no build isolation and no index."""
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    calls: list[list[str]] = []

    def fake_extract(archive, dest):
        (dest / "python" / "bin").mkdir(parents=True)
        return dest

    def fake_run(argv, **kw):
        argv = [str(a) for a in argv]
        calls.append(argv)
        if argv[3:5] == ["pip", "wheel"]:
            out = Path(argv[argv.index("-w") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "graft_ipa-0-py3-none-any.whl").write_bytes(b"")
        if "--report" in argv:
            Path(argv[argv.index("--report") + 1]).write_text(json.dumps({"install": [
                {"metadata": {"name": "x", "version": "1"},
                 "download_info": {"url": "https://x", "archive_info": {"hashes": {"sha256": "a" * 64}}}}]}))

    monkeypatch.setattr(gb, "fetch", lambda pin, cache=None: tmp_path / "cache" / pin.filename)
    monkeypatch.setattr(gb, "extract", fake_extract)
    monkeypatch.setattr(gb, "prune_python", lambda py, arch="arm64": None)
    monkeypatch.setattr(gb, "check_python", lambda py: "graft 0")
    monkeypatch.setattr(gb.subprocess, "run", fake_run)
    reports = tmp_path / "reports"
    gb.stage_python(pins, tmp_path / "Contents", reports)

    installs = [c for c in calls if c[3:5] == ["pip", "install"] and "--dry-run" not in c]
    for c in installs:
        assert "--require-hashes" in c or {"--no-index", "--no-build-isolation"} <= set(c), c
    # Review #49: a locked set is wheels only, so nothing is built from an
    # sdist with unhashed build dependencies. Review #36: chosen for macOS 15
    # arm64 and CPython 3.13, whatever this Mac runs, then installed offline.
    plans = [c for c in calls if "--dry-run" in c]
    downloads = [c for c in calls if c[3:5] == ["pip", "download"]]
    offline = [c for c in installs if "--require-hashes" in c]
    assert len(plans) == len(downloads) == len(offline) == 2
    for c in plans + downloads:
        assert "--only-binary=:all:" in c and "--require-hashes" in c and "--no-deps" in c, c
        target = " ".join(c)
        assert ("--platform macosx_14_0_arm64 --python-version 3.13 --implementation cp --abi cp313" in target), c
    for c in offline:
        assert {"--only-binary=:all:", "--no-index", "--no-deps"} <= set(c) and "--find-links" in c, c
        assert c[c.index("--find-links") + 1] in {d[d.index("-d") + 1] for d in downloads}, c
    assert all(c[c.index("--report") + 1].startswith(str(reports)) for c in plans), "components come from the plans"
    assert calls.index(plans[0]) < calls.index(downloads[0]) < calls.index(offline[0])
    # graft is built from this checkout into a wheel, so the wheel it installs has a hash.
    wheel = next(c for c in calls if c[3:5] == ["pip", "wheel"])
    assert wheel[-1] == str(REPO)
    assert {"--no-build-isolation", "--no-index", "--no-deps"} <= set(wheel), wheel
    cyan_zip = str(tmp_path / "cache" / pins["cyan"].filename)
    built = {c[-1]: c for c in installs if c[-1] == cyan_zip or c[-1].endswith("/graft_ipa-0-py3-none-any.whl")}
    assert len(built) == 2 and cyan_zip in built
    for c in built.values():
        assert "--no-build-isolation" in c and "--no-index" in c and "--no-deps" in c, c
    locks = [c[-1] for c in offline]
    assert locks == [str(gb.BUILD_LOCK), str(gb.RUNTIME_LOCK)]
    assert calls.index(next(c for c in installs if c[-1] == str(gb.BUILD_LOCK))) < calls.index(wheel)
    # Every install records what it came from, for components.json: the
    # locked sets by their plans, graft and cyan by their own reports.
    written = [c[c.index("--report") + 1] for c in plans + list(built.values())]
    assert len(written) == 4 == len(set(written))
    assert all(Path(r).parent == reports for r in written)

    uninstall = next(c for c in calls if c[3:5] == ["pip", "uninstall"])
    assert calls.index(uninstall) > calls.index(built[cyan_zip])
    removed = set(uninstall[uninstall.index("--disable-pip-version-check") + 1:])
    build = set(_lock_entries((REPO / "bundle" / "build-requirements.lock").read_text()))
    runtime = set(_lock_entries((REPO / "bundle" / "requirements.lock").read_text()))
    assert {"hatchling", "setuptools"} <= removed
    assert removed == build - runtime, "every build-only package goes; nothing the runtime needs does"
    compile_all = next(c for c in calls if c[3:5] == ["compileall", "-q"])
    assert compile_all[compile_all.index("--invalidation-mode") + 1] == "unchecked-hash"  # review #40
    # Review #30: with every install --no-deps, pip check is what confirms the set.
    check = next(c for c in calls if c[3:5] == ["pip", "check"])
    assert calls.index(check) > calls.index(uninstall)


def test_a_locked_install_must_be_the_files_the_plan_chose(tmp_path):
    def report(path, items):
        path.write_text(json.dumps({"install": [
            {"metadata": {"name": n, "version": v}, "download_info": {"archive_info": {"hashes": {"sha256": h}}}}
            for n, v, h in items]}))
        return path
    plan = report(tmp_path / "plan.json", [("Pkg-A", "1.0", "a" * 64), ("b", "2.0", "b" * 64)])
    gb.check_same_files(plan, report(tmp_path / "ok.json", [("pkg_a", "1.0", "a" * 64)]))
    with pytest.raises(gb.BundleError, match=r"pkg-a 1\.0"):
        gb.check_same_files(plan, report(tmp_path / "bad.json", [("pkg-a", "1.0", "c" * 64)]))


def test_licences_name_every_bundled_component_and_python_package(tmp_path):
    py = tmp_path / "Contents/Resources/GraftPython"
    dist = py / "lib/python3.13/site-packages/example_pkg-1.0.dist-info"
    (dist / "licenses").mkdir(parents=True)
    (dist / "METADATA").write_text("Name: example-pkg\nVersion: 1.0\nLicense-Expression: MIT\n")
    (dist / "licenses/LICENSE").write_text("MIT text")
    old = py / "lib/python3.13/site-packages/old_pkg-2.0.dist-info"
    old.mkdir()
    (old / "METADATA").write_text("Name: old_pkg\nVersion: 2.0\nLicense: UNKNOWN\n"
                                  "Classifier: License :: OSI Approved :: BSD License\n")
    (old / "COPYING").write_text("BSD text")
    full = py / "lib/python3.13/site-packages/full_text-3.0.dist-info"
    (full / "licenses").mkdir(parents=True)
    (full / "METADATA").write_text("Name: full-text\nVersion: 3.0\nLicense: MIT License\n        \n"
                                   "        Copyright (c) someone\nLicense-File: LICENSE\n")
    (full / "licenses/LICENSE").write_text("MIT text")
    bare = py / "lib/python3.13/site-packages/bare-4.0.dist-info"
    (bare / "licenses").mkdir(parents=True)
    (bare / "METADATA").write_text("Name: bare\nVersion: 4.0\nLicense-File: LICENSE\n")
    (bare / "licenses/LICENSE").write_text("some text")
    extra = tmp_path / "tool-licences"
    extra.mkdir()
    (extra / "zsign-LICENSE").write_text("zsign MIT")
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    gb.collect_licenses(tmp_path / "Contents", pins, extra)
    lic = tmp_path / "Contents/Resources/Licenses"
    index = (lic / "INDEX.txt").read_text()
    assert gb.LICENCE_STATEMENT in index
    # Review #28: an aggregate, never one license for the whole app.
    assert "Graft.app is distributed under" not in index and "no one license covers the app as a whole" in index
    assert "GPL-3.0-or-later" in gb.LICENCE_STATEMENT
    # Unicorn isn't in the app; the statement says what ipatool downloads.
    assert ("Unicorn is not part of Graft.app: the bundled ipatool downloads Unicorn 2.1.4 (GPL-2.0) from "
            "PyPI on first use") in gb.LICENCE_STATEMENT
    assert "unicorn-COPYING" not in index and "GPL-2.0's is" not in index
    assert "example-pkg 1.0 — MIT\n" in index
    assert "old_pkg 2.0 — BSD License\n" in index
    assert "full-text 3.0 — MIT License\n" in index
    assert "bare 4.0 — not stated in its metadata; see its license file\n" in index
    # RC audit #3: each pin says what it is to the app; the licence-text
    # pins were listed as "the exact artifact bundled", and aren't in it.
    assert "the exact artifact bundled" not in index
    for name, pin in pins.items():
        if name in gb.LICENCE_TEXT_PINS:
            assert f"  {name} — {pin.license} — {pin.url}\n" in index.split("License texts only")[1], name
            assert f"\n{name} — " not in index.split("License texts only")[0], name
        elif name in gb.BUILT_FROM_SOURCE:
            assert f"{name} — {pin.license} — built here from {pin.url}\n" in index, name
        elif name in gb.TOOLCHAINS:
            assert f"{name} — {pin.license} — the toolchain a binary is built with, {pin.url}" in index, name
        else:
            assert f"{name} — {pin.license} — {pin.url}\n" in index.split("License texts only")[0], name
    assert "ldid, insert_dylib, lipo, install_name_tool, otool" in index
    assert (lic / "COPYING-GPL-3.0.txt").read_bytes() == (REPO / "bundle" / "COPYING-GPL-3.0.txt").read_bytes()
    assert (lic / "python-packages/example-pkg-1.0/LICENSE").read_text() == "MIT text"
    assert (lic / "python-packages/old_pkg-2.0/COPYING").read_text() == "BSD text"
    assert (lic / "zsign-LICENSE").read_text() == "zsign MIT"


def _fake_install(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    """A staged site-packages holding every package bundle/requirements.lock
    pins, plus graft and cyan, and the pip --report files that installed them.
    Returns (Contents, the reports dir, each lock name's expected sha256)."""
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    contents = tmp_path / "Contents"
    site = contents / "Resources/GraftPython/lib/python3.13/site-packages"
    reports = tmp_path / "reports"
    reports.mkdir()
    lock = _lock_entries((REPO / "bundle" / "requirements.lock").read_text())
    expected, from_index, local = {}, [], []
    for name, entry in lock.items():
        version = entry.split()[0].split("==")[1]
        sha = re.search(r"--hash=sha256:([0-9a-f]{64})", entry).group(1)
        expected[name] = sha
        from_index.append((name, version, f"https://files.pythonhosted.org/x/{name}-{version}.whl", sha))
    local.append(("graft-ipa", "1.1.0", "file:///private/var/tmp/w/graft_ipa-1.1.0-py3-none-any.whl", "a" * 64))
    local.append(("cyan", "1.4.4", "file:///private/var/tmp/cache/cyan.zip", pins["cyan"].sha256))
    for name, version, _, _ in from_index + local:
        dist = site / f"{name.replace('-', '_')}-{version}.dist-info"
        dist.mkdir(parents=True)
        stated = "" if name == "cyan" else "License-Expression: MIT\n"
        (dist / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n{stated}")

    def report(path: Path, items) -> None:
        path.write_text(json.dumps({"version": "1", "install": [
            {"metadata": {"name": n, "version": v},
             "download_info": {"url": u, "archive_info": {"hashes": {"sha256": s}, "hash": f"sha256={s}"}}}
            for n, v, u, s in items]}))

    report(reports / "1.json", from_index)
    report(reports / "2.json", local)
    return contents, reports, expected


def test_the_component_manifest_names_every_pin_and_locked_package_with_hash_and_licence(tmp_path):
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    contents, reports, expected = _fake_install(tmp_path)
    out = gb.write_components(contents, pins, reports)
    assert out == contents / "Resources/Licenses/components.json"
    entries = json.loads(out.read_text())
    for e in entries:
        assert set(e) == {"name", "version", "kind", "source_url", "sha256", "license", "licence_files"}, e
        assert re.fullmatch(r"[0-9a-f]{64}", e["sha256"]), e
        assert e["license"] and e["version"], e
        assert e["source_url"].startswith("https://"), e
    by_kind = {}
    for e in entries:
        by_kind.setdefault(e["kind"], {})[_norm(e["name"]) if e["kind"] == "python-package" else e["name"]] = e
    assert set(by_kind) == {"artifact", "built-from-source", "python-package", "toolchain", "licence-text"}
    for name, pin in pins.items():
        kind = ("built-from-source" if name in {"openssl", "libplist", "ldid", "cctools", "insert_dylib", "ipatool",
                                                "unfairplay"}
                else "toolchain" if name == "go"
                else "licence-text" if name in gb.LICENCE_TEXT_PINS else "artifact")
        for built in pin.builds or (name,):
            assert by_kind[kind][built] == {"name": built, "version": pin.version, "kind": kind,
                                            "source_url": pin.url, "sha256": pin.sha256, "license": pin.license,
                                            "licence_files": []}  # no Licenses/ in this fixture
    # Each of cyan's five helpers, and libplist, by the binary's own name.
    for name in ("ldid", "lipo", "otool", "install_name_tool", "insert_dylib", "libplist"):
        assert name in by_kind["built-from-source"], name
    assert "cctools" not in by_kind["built-from-source"], "listed by the three binaries built from it"
    assert by_kind["built-from-source"]["otool"]["source_url"] == pins["cctools"].url
    packages = by_kind["python-package"]
    for name, sha in expected.items():
        assert packages[name]["sha256"] == sha, name
    assert set(packages) == set(expected) | {"graft-ipa", "cyan"}
    assert packages["cyan"]["source_url"] == pins["cyan"].url
    assert packages["cyan"]["license"] == pins["cyan"].license, "an unstated license falls back to the pin's"
    assert packages["graft-ipa"]["source_url"] == "https://github.com/GreenglassT/Graft"


def test_an_installed_package_with_no_recorded_download_is_refused(tmp_path):
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    contents, reports, _ = _fake_install(tmp_path)
    site = contents / "Resources/GraftPython/lib/python3.13/site-packages"
    (site / "stray-1.0.dist-info").mkdir()
    (site / "stray-1.0.dist-info/METADATA").write_text("Name: stray\nVersion: 1.0\nLicense-Expression: MIT\n")
    with pytest.raises(gb.BundleError, match="stray"):
        gb.write_components(contents, pins, reports)


def test_a_package_installed_from_an_unpinned_local_file_is_refused(tmp_path):
    """Its only source is a path on the release machine, which the manifest must not name."""
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    contents, reports, _ = _fake_install(tmp_path)
    data = json.loads((reports / "2.json").read_text())
    data["install"][1]["download_info"]["archive_info"]["hashes"]["sha256"] = "b" * 64
    (reports / "2.json").write_text(json.dumps(data))
    with pytest.raises(gb.BundleError, match="cyan"):
        gb.write_components(contents, pins, reports)


def test_the_gpl_text_is_version_3():
    head = (REPO / "bundle" / "COPYING-GPL-3.0.txt").read_text().splitlines()[:2]
    assert [line.strip() for line in head] == ["GNU GENERAL PUBLIC LICENSE", "Version 3, 29 June 2007"]


def _tarball(path: Path, members: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))
    return path


def test_the_tool_licences_start_as_the_vendored_texts_of_pins_and_nothing_is_fetched(tmp_path, monkeypatch):
    """ipatool and UnfairPlay are built from source (stage_ipatool,
    stage_unfairplay), so no release tarball is unpacked for its binary:
    what starts the folder is bundle/licenses' texts for pins (zsign's)."""
    monkeypatch.setattr(gb, "fetch", lambda pin, cache=None: pytest.fail(f"fetched {pin.name}"))
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    (tmp_path / "work" / "tool-licences").mkdir(parents=True)
    (tmp_path / "work" / "tool-licences" / "unfairplay-NOTICE").write_text("left by an earlier stage")
    licences = gb.vendored_licences(pins, tmp_path / "work")
    assert licences == tmp_path / "work" / "tool-licences"
    assert sorted(p.name for p in licences.iterdir()) == ["zsign-LICENSE"]
    assert not hasattr(gb, "TARBALL_TOOLS") and not hasattr(gb, "stage_tools")


def test_a_tarball_without_exactly_one_matching_binary_is_refused(tmp_path):
    two = _tarball(tmp_path / "two.tar.gz", {"a/zsign": mb.thin([]), "b/zsign-arm64": mb.thin([])})
    with pytest.raises(gb.BundleError, match="zsign"):
        gb._the_binary(gb.extract(two, tmp_path / "x"), "zsign")


def test_a_stage_naming_the_build_machine_is_refused(tmp_path):
    c = tmp_path / "Contents"
    (c / "Resources").mkdir(parents=True)
    (c / "Resources/clean.txt").write_text("nothing to see")
    gb.check_no_build_paths(c)
    (c / "Resources/leak.pyc").write_bytes(b"\0\0" + str(REPO).encode() + b"/graft_ipa/cli.py\0")
    with pytest.raises(gb.BundleError, match=r"leak\.pyc"):
        gb.check_no_build_paths(c)


def test_stage_refuses_a_directory_that_is_not_empty(tmp_path):
    (tmp_path / "left-over").write_text("x")
    with pytest.raises(gb.BundleError, match="not empty"):
        gb.stage(tmp_path)


# ------------------------------------------------------------ cyan's helpers

CYAN_TOOLS = "Resources/GraftPython/lib/python3.13/site-packages/cyan/tools/Darwin/arm64"

#: SHA-256 of the five prebuilt helpers cyan 1.4.4 (commit 04a40f2) ships in
#: cyan/tools/Darwin/arm64. The release builds each from source instead, so
#: none of these bytes may be anywhere in a stage.
CYAN_ORIGINALS = {
    "insert_dylib": "d3caadb5fc634888b58c0386896416e808f85d73ac2420371ec57cc41004bd77",
    "install_name_tool": "1b246d41b85de2fcf026df7c702ab8b33b41beb0114f4908031522dbe975eae4",
    "ldid": "842a16d0b2f5dd5a107e2364b6e912caeba53c5dafc8a83ecf48ffa43c612dbb",
    "lipo": "513b4a1fef8c3b9065b2b038d91fa480c7d77dbe6a9ca5766f39953ff766bee4",
    "otool": "b8cd87f5171fdbf350a0115dd9ee453877d7e68fcdf67e5dbb76197a60c87440",
}

LIBCRYPTO_BUILT = "/opt/graft/openssl/lib/libcrypto.3.dylib"
LIBPLIST_BUILT = "/opt/graft/libplist/lib/libplist-2.0.12.dylib"
#: From cyan/tools/Darwin/arm64 up to Contents, then into Frameworks: written
#: out, not computed, so the test doesn't share the code's arithmetic.
TO_FRAMEWORKS = "@loader_path/../../../../../../../../../Frameworks"


def _ldid(contents: Path, *links: str) -> Path:
    ldid = contents / CYAN_TOOLS / "ldid"
    ldid.parent.mkdir(parents=True, exist_ok=True)
    ldid.write_bytes(mb.thin([mb.dylib_cmd(n) for n in links]))
    return ldid


def test_the_ldid_link_plan_points_libcrypto_and_libplist_into_frameworks(tmp_path):
    contents = tmp_path / "Contents"
    ldid = _ldid(contents, LIBCRYPTO_BUILT, LIBPLIST_BUILT, "/usr/lib/libc++.1.dylib", "/usr/lib/libSystem.B.dylib")
    assert gb.ldid_link_plan(ldid, contents) == [
        (LIBCRYPTO_BUILT, f"{TO_FRAMEWORKS}/libcrypto.3.dylib"),
        (LIBPLIST_BUILT, f"{TO_FRAMEWORKS}/libplist-2.0.dylib"),
    ]


@pytest.mark.parametrize("link", ["/opt/homebrew/opt/openssl@3/lib/libcrypto.1.1.dylib",
                                  "/opt/homebrew/lib/libzip.5.dylib", "@rpath/libplist++-2.0.dylib"])
def test_an_ldid_linking_anything_the_bundle_does_not_carry_is_refused(tmp_path, link):
    contents = tmp_path / "Contents"
    ldid = _ldid(contents, LIBCRYPTO_BUILT, LIBPLIST_BUILT, link)
    with pytest.raises(gb.BundleError, match=re.escape(link.rsplit("/", 1)[-1])):
        gb.ldid_link_plan(ldid, contents)


def test_the_rewritten_ldid_links_only_the_system_and_the_two_bundled_libraries(tmp_path):
    contents = tmp_path / "Contents"
    good = [f"{TO_FRAMEWORKS}/libcrypto.3.dylib", f"{TO_FRAMEWORKS}/libplist-2.0.dylib",
            "/usr/lib/libc++.1.dylib", "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"]
    gb.check_ldid_links(_ldid(contents, *good), contents)
    for bad in ([LIBCRYPTO_BUILT, *good[1:]],                 # one link left unrewritten
                good[1:],                                     # libcrypto not linked at all
                [*good, "/opt/homebrew/lib/libplist-2.0.dylib"]):
        with pytest.raises(gb.BundleError, match="ldid"):
            gb.check_ldid_links(_ldid(contents, *bad), contents)


def _cyan_zip(path: Path, tools: dict[str, bytes]) -> Path:
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("pyzule-rw-0/cyan/__init__.py", "")
        for name, data in tools.items():
            z.writestr(f"pyzule-rw-0/cyan/tools/Darwin/arm64/{name}", data)
        z.writestr("pyzule-rw-0/cyan/tools/Darwin/x86_64/ldid", b"another platform's")
    return path


def test_the_originals_are_read_from_cyans_archive_and_all_five_are_required(tmp_path):
    tools = {name: f"cyan's {name}".encode() for name in gb.CYAN_HELPERS}
    got = gb.cyan_original_hashes(_cyan_zip(tmp_path / "cyan.zip", tools))
    assert got == {name: hashlib.sha256(data).hexdigest() for name, data in tools.items()}
    del tools["otool"]
    with pytest.raises(gb.BundleError, match="otool"):
        gb.cyan_original_hashes(_cyan_zip(tmp_path / "short.zip", tools))


def _helpers_stage(tmp_path: Path) -> tuple[Path, dict[str, Path], dict[str, str]]:
    """A stage whose site-packages holds cyan's five originals, five fake
    builds to put in their place, and the originals' hashes."""
    contents = tmp_path / "Contents"
    tools = contents / CYAN_TOOLS
    tools.mkdir(parents=True)
    (contents / "Frameworks").mkdir()
    originals = {}
    for name in gb.CYAN_HELPERS:
        data = f"cyan's prebuilt {name}".encode()
        (tools / name).write_bytes(data)
        originals[name] = hashlib.sha256(data).hexdigest()
    built_dir = tmp_path / "built"
    built_dir.mkdir()
    built = {}
    for name in gb.CYAN_HELPERS:
        links = [LIBCRYPTO_BUILT, LIBPLIST_BUILT, "/usr/lib/libc++.1.dylib"] if name == "ldid" else []
        (built_dir / name).write_bytes(mb.thin([mb.dylib_cmd(n) for n in links]) + name.encode())
        built[name] = built_dir / name
    (built_dir / "libplist-2.0.12.dylib").write_bytes(mb.thin([]) + b"libplist")
    built["libplist"] = built_dir / "libplist-2.0.12.dylib"
    return contents, built, originals


def _install_name_tool_fake(monkeypatch) -> list[list[str]]:
    """install_name_tool, done in Python on the fake Mach-O files mb builds:
    -change rebuilds the file with the name replaced; -id is recorded only."""
    calls = []

    def run(*args, cwd=None):
        argv = [str(a) for a in args]
        calls.append(argv)
        assert argv[0] == gb.INSTALL_NAME_TOOL == "/usr/bin/install_name_tool", argv
        target = Path(argv[-1])
        names = macho.dylib_names(target)
        rest = argv[1:-1]
        while rest:
            flag, value, *rest = rest
            if flag == "-change":
                new, *rest = rest
                names = [new if n == value else n for n in names]
        if "-change" in argv:
            target.write_bytes(mb.thin([mb.dylib_cmd(n) for n in names]))

    monkeypatch.setattr(gb, "_run", run)
    return calls


def test_cyans_helpers_are_replaced_by_the_builds_and_the_originals_are_gone(tmp_path, monkeypatch):
    contents, built, originals = _helpers_stage(tmp_path)
    calls = _install_name_tool_fake(monkeypatch)
    gb.stage_cyan_helpers(built, contents, originals)
    tools = contents / CYAN_TOOLS
    for name in gb.CYAN_HELPERS:
        if name != "ldid":
            assert (tools / name).read_bytes() == built[name].read_bytes(), name
        assert (tools / name).stat().st_mode & 0o777 == 0o755, name
    assert macho.dylib_names(tools / "ldid") == [f"{TO_FRAMEWORKS}/libcrypto.3.dylib",
                                                 f"{TO_FRAMEWORKS}/libplist-2.0.dylib", "/usr/lib/libc++.1.dylib"]
    lib = contents / "Frameworks" / "libplist-2.0.dylib"
    assert lib.read_bytes() == built["libplist"].read_bytes()
    assert [gb.INSTALL_NAME_TOOL, "-id", "@rpath/libplist-2.0.dylib", str(lib)] in calls
    staged = {hashlib.sha256(p.read_bytes()).hexdigest() for p in contents.rglob("*") if p.is_file()}
    assert not staged & set(originals.values())


def test_a_stage_still_holding_one_of_cyans_originals_is_refused(tmp_path):
    contents, _, originals = _helpers_stage(tmp_path)
    with pytest.raises(gb.BundleError, match="ldid"):
        gb.check_originals_gone(contents, originals)
    # Anywhere in the stage, under any name: the bytes are what matter.
    for name in gb.CYAN_HELPERS:
        (contents / CYAN_TOOLS / name).write_bytes(b"rebuilt " + name.encode())
    gb.check_originals_gone(contents, originals)
    (contents / "Helpers").mkdir()
    (contents / "Helpers" / "copy").write_bytes(b"cyan's prebuilt lipo")
    with pytest.raises(gb.BundleError, match="Helpers/copy"):
        gb.check_originals_gone(contents, originals)


@pytest.mark.parametrize("which", ["lipo", "libplist"])
def test_a_helper_or_libplist_linking_a_library_the_bundle_lacks_is_refused(tmp_path, monkeypatch, which):
    contents, built, originals = _helpers_stage(tmp_path)
    _install_name_tool_fake(monkeypatch)
    built[which].write_bytes(mb.thin([mb.dylib_cmd("/opt/homebrew/lib/libintl.8.dylib")]))
    with pytest.raises(gb.BundleError, match="libintl"):
        gb.stage_cyan_helpers(built, contents, originals)


def test_cyan_must_carry_each_helper_its_code_calls(tmp_path, monkeypatch):
    """cyan runs tools/Darwin/arm64/<name>; a build put anywhere else would never run."""
    contents, built, originals = _helpers_stage(tmp_path)
    _install_name_tool_fake(monkeypatch)
    (contents / CYAN_TOOLS / "otool").unlink()
    with pytest.raises(gb.BundleError, match="otool"):
        gb.stage_cyan_helpers(built, contents, originals)


@slow
def test_the_five_helpers_build_from_their_pinned_sources_and_run(tmp_path):
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    built = gb.build_cyan_helpers(pins, gb.build_openssl(pins["openssl"]))
    assert set(built) == {*gb.CYAN_HELPERS, "libplist"} and not set(built) & {"licences"}
    for name, path in built.items():
        assert path.is_file(), name
        if name != "libplist":
            assert macho.load_commands(path), name
            r = subprocess.run(["lipo", "-archs", str(path)], capture_output=True, text=True, check=True)
            assert r.stdout.split() == ["arm64"], name
    assert "@rpath" not in " ".join(macho.dylib_names(built["install_name_tool"]))
    assert not [n for n in macho.dylib_names(built["install_name_tool"]) if "codedirectory" in n]

    # Staged the way the release stages them, outside any .app, then run.
    contents = tmp_path / "Contents"
    tools = contents / CYAN_TOOLS
    tools.mkdir(parents=True)
    for name in gb.CYAN_HELPERS:
        (tools / name).write_bytes(b"cyan's " + name.encode())
    prefix = gb.build_openssl(pins["openssl"])
    fw = contents / "Frameworks"
    fw.mkdir()
    import shutil
    shutil.copy2(prefix / "lib" / "libcrypto.3.dylib", fw / "libcrypto.3.dylib")
    subprocess.run(["install_name_tool", "-id", "@rpath/libcrypto.3.dylib", str(fw / "libcrypto.3.dylib")], check=True)
    gb.stage_cyan_helpers(built, contents, CYAN_ORIGINALS)
    staged = {hashlib.sha256(p.read_bytes()).hexdigest() for p in contents.rglob("*") if p.is_file()}
    assert not staged & set(CYAN_ORIGINALS.values())

    def tool(name, *args, ok=True):
        r = subprocess.run([str(tools / name), *map(str, args)], capture_output=True, text=True, timeout=60)
        if ok:
            assert r.returncode == 0, (name, r.stdout[-2000:], r.stderr[-2000:])
        return r

    r = tool("ldid", ok=False)                         # no arguments: its usage
    assert "Usage" in r.stdout + r.stderr and "procursus" in r.stdout + r.stderr
    src = tmp_path / "x.c"
    src.write_text("int main(void) { return 0; }\n")
    fat = tmp_path / "fat"
    subprocess.run(["clang", "-arch", "arm64", "-arch", "x86_64", "-o", str(fat), str(src)], check=True)
    assert "x86_64 arm64" in tool("lipo", "-info", fat).stdout
    assert "/usr/lib/libSystem.B.dylib" in tool("otool", "-L", "/bin/ls").stdout
    lib = tmp_path / "libx.dylib"
    subprocess.run(["clang", "-arch", "arm64", "-dynamiclib", "-Wl,-headerpad_max_install_names",
                    "-install_name", "/usr/local/lib/libx.dylib", "-o", str(lib), str(src)], check=True)
    tool("install_name_tool", "-id", "@rpath/libx.dylib", "-add_rpath", "@loader_path", lib)
    tool("insert_dylib", "--weak", "--inplace", "--all-yes", "@rpath/liby.dylib", lib)
    assert "@rpath/libx.dylib" in tool("otool", "-D", lib).stdout
    assert "@rpath/liby.dylib" in macho.dylib_names(lib) and "@loader_path" in macho.rpaths(lib)
    tool("ldid", "-S", lib)
    assert tool("ldid", "-h", lib).returncode == 0

    # Review #2: a file name is never a shell command. Upstream's hook ran
    # `codesign -s - -f <name>` through system() here and made PWNED.
    for name in gb.CYAN_HELPERS:
        assert not gb.imported_symbols(tools / name) & set(gb.SHELL_IMPORTS), name
    hostile = tmp_path / "a;touch PWNED;.dylib"
    shutil.copy2(lib, hostile)
    env = {k: v for k, v in os.environ.items() if k != "NO_CODESIGN"}
    r = subprocess.run([str(tools / "install_name_tool"), "-add_rpath", "@executable_path", str(hostile)],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "@executable_path" in macho.rpaths(hostile), r.stderr
    assert not list(tmp_path.rglob("PWNED")) and "command not found" not in r.stderr


CCTOOLS_PATCH = REPO / "bundle" / "patches" / "cctools-port-no-fake-sign.patch"


def _expected_by(patch: Path) -> str:
    """What a one-hunk unified diff expects to find: its context and `-` lines."""
    lines = patch.read_text().splitlines()
    hunk = next(i for i, line in enumerate(lines) if line.startswith("@@"))
    return "".join(line[1:] + "\n" for line in lines[hunk + 1:] if line[:1] in (" ", "-"))


def test_the_cctools_patch_compiles_the_fake_sign_hook_out(tmp_path):
    """Review #2: cctools-port's FAKE_SIGN_BINARY ran `system("<codesign> -s -
    -f <file>")` with the file name unquoted after every change install_name_tool
    made to an ARM file, so `Fx;touch PWNED;.dylib` ran `touch PWNED`. The patch
    makes both macros no-ops; applied to upstream's text, no system() is left."""
    top = tmp_path / "cctools-port"
    port = top / "cctools" / "include" / "stuff" / "port.h"
    port.parent.mkdir(parents=True)
    upstream = "#include <stdlib.h> /* system(), free() & getenv() */\n" + "\n" * 20 + _expected_by(CCTOOLS_PATCH)
    assert "system(codesign_command);" in upstream and "getenv(\"NO_CODESIGN\")" in upstream
    port.write_text(upstream)
    gb.apply_patches("cctools", top)
    patched = port.read_text()
    body = patched.split("#define FAKE_SIGN_BINARY", 1)[1]
    assert "system(" not in body and "codesign_command" not in body and "find_executable" not in body
    assert "#define FAKE_SIGN_BINARY(filename, verbose) do { } while (0)" in patched
    assert "#define FAKE_SIGN_ARM_BINARY(archs, narchs, filename) do { } while (0)" in patched
    with pytest.raises(subprocess.CalledProcessError):
        gb.apply_patches("cctools", top)      # --forward: never applied twice


def test_a_patch_the_stage_names_but_lacks_is_refused(tmp_path):
    with pytest.raises(gb.BundleError, match=r"cctools-port-no-fake-sign\.patch"):
        gb.patch_hashes("cctools", tmp_path)


def test_cctools_is_patched_before_it_is_built_and_its_key_covers_the_patch(tmp_path, monkeypatch):
    events = []
    pin = gb.Pin(name="cctools", url="https://example.invalid/c.tar.gz", sha256="c" * 64, license="APSL-2.0",
                 builds=("lipo", "install_name_tool", "otool"))

    def unpack(pin, tmp):
        top = tmp / "src" / "cctools-port"
        for d in ("misc", "otool"):
            (top / "cctools" / d).mkdir(parents=True)
        (top / "cctools" / "APPLE_LICENSE").write_text("APSL")
        return top

    def build_steps(steps, cwd, arch="arm64"):
        events.append(("build", cwd.name))
        for tool in pin.builds:
            (cwd / ("otool" if tool == "otool" else "misc") / tool).write_bytes(b"built " + tool.encode())

    patches = tmp_path / "patches"
    patches.mkdir()
    (patches / "cctools-port-no-fake-sign.patch").write_bytes(CCTOOLS_PATCH.read_bytes())
    monkeypatch.setattr(gb, "PATCHES_DIR", patches)
    monkeypatch.setattr(gb, "_unpack", unpack)
    monkeypatch.setattr(gb, "_build_steps", build_steps)
    monkeypatch.setattr(gb, "apply_patches", lambda name, src, p=None: events.append(("patch", name, src.name)))
    monkeypatch.setattr(gb, "check_no_shell", lambda path, name: events.append(("no shell", name)))
    first = gb.build_cctools(pin, tmp_path / "work")
    assert events[:2] == [("patch", "cctools", "cctools-port"), ("build", "cctools")]
    assert [e[1] for e in events if e[0] == "no shell"] == list(pin.builds)
    (patches / "cctools-port-no-fake-sign.patch").write_text(CCTOOLS_PATCH.read_text() + "\n")
    assert gb.build_cctools(pin, tmp_path / "work") != first, "a changed patch is a new build"


@needs_toolchain
def test_a_built_tool_that_can_start_a_shell_is_refused(tmp_path):
    def build(name, body):
        src = tmp_path / f"{name}.c"
        src.write_text("#include <stdio.h>\n#include <stdlib.h>\nint main(int c, char **v) { " + body + " }\n")
        subprocess.run(["clang", "-arch", "arm64", "-o", str(tmp_path / name), str(src)], check=True)
        return tmp_path / name

    gb.check_no_shell(build("quiet", "return c > 1;"), "quiet")
    for name, body in (("sys", "return system(v[0]);"), ("pop", "return popen(v[0], \"r\") != 0;")):
        with pytest.raises(gb.BundleError, match="start a shell"):
            gb.check_no_shell(build(name, body), name)


def test_a_helper_build_is_cached_by_its_key_and_rebuilt_when_the_key_changes(tmp_path):
    runs = []

    def build(tmp, out):
        runs.append(tmp)
        (out / "tool").write_bytes(b"built")

    work = tmp_path / "work"
    first = gb._cached_build("x", gb.build_key("sha", ["make"]), work, build)
    assert (first / "tool").read_bytes() == b"built" and len(runs) == 1
    assert gb._cached_build("x", gb.build_key("sha", ["make"]), work, build) == first and len(runs) == 1
    other = gb._cached_build("x", gb.build_key("sha", ["make", "CFLAGS=-O0"]), work, build)
    assert other != first and len(runs) == 2, "different steps are a different build"
    assert " " not in str(runs[0])


def test_a_helper_build_whose_products_name_its_build_directory_is_refused(tmp_path):
    def build(tmp, out):
        (out / "tool").write_bytes(b"\0" + str(tmp / "src" / "main.c").encode() + b"\0")

    with pytest.raises(gb.BundleError, match="tool"):
        gb._cached_build("x", "k" * 12, tmp_path / "work", build)
    assert not list((tmp_path / "work").glob("x-*")), "nothing is cached from a refused build"


def _lipo_archs(path: Path) -> list[str]:
    return subprocess.run(["lipo", "-archs", str(path)], capture_output=True, text=True, check=True).stdout.split()


def _clang_dylib(out: Path, *archs: str) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    src = out.with_suffix(".c")
    src.write_text("int f(void) { return 1; }\n")
    subprocess.run(["clang", *(a for arch in archs for a in ("-arch", arch)), "-dynamiclib", "-o", str(out),
                    str(src)], check=True)
    src.unlink()
    return out


@needs_tools("clang", "lipo")  # compiles with clang; the code under test runs lipo
def test_universal_mach_o_files_in_the_stage_are_thinned_to_arm64(tmp_path):
    """Some wheels are universal2; everything the app bundles is arm64 only."""
    c = tmp_path / "Contents"
    fat = _clang_dylib(c / "Resources/GraftPython/lib/python3.13/site-packages/x/_x.abi3.so", "arm64", "x86_64")
    thin = _clang_dylib(c / "Frameworks/libthin.dylib", "arm64")
    before = thin.read_bytes()
    assert gb.thin_to_arm64(c) == [fat]
    assert _lipo_archs(fat) == ["arm64"]
    assert thin.read_bytes() == before, "an arm64-only file is left alone"


@needs_tools("clang", "lipo")  # compiles with clang; the code under test runs lipo
@pytest.mark.parametrize("archs", [("x86_64",), ("arm64e",), ("x86_64", "arm64e")])
def test_a_mach_o_with_no_arm64_slice_is_refused(tmp_path, archs):
    c = tmp_path / "Contents"
    _clang_dylib(c / "Helpers/bin/libother.dylib", *archs)
    with pytest.raises(gb.BundleError, match="libother"):
        gb.thin_to_arm64(c)


# ------------------------------------------- license texts packages don't ship

VENDORED_FOR_PACKAGES = {"bpylist2": "4.1.1", "enum-compat": "0.0.3", "loguru": "0.7.3"}


def test_every_vendored_licence_belongs_to_a_pin_or_a_locked_package():
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    locked = set(_lock_entries((REPO / "bundle" / "requirements.lock").read_text()))
    for f in (REPO / "bundle" / "licenses").iterdir():
        if f.name in ("cyan-extras", "spdx"):        # cyan's frameworks' (their own tests, below); SPDX texts
            continue
        if f.name == "crates":  # for crates that ship none, each named by a bundle/ipatool-core-crates.lock entry
            named = {p["license-file"] for p in gb.pinned_crates().values() if "license-file" in p}
            assert {f"crates/{c.name}" for c in f.iterdir()} == named
            continue
        owner = f.name.removesuffix("-LICENSE")
        assert f.name.endswith("-LICENSE") and (owner in pins or owner in locked), f.name
    for name, version in VENDORED_FOR_PACKAGES.items():
        text = (REPO / "bundle" / "licenses" / f"{name}-LICENSE").read_text()
        head = text.splitlines()[0]
        assert head.startswith(f"# {name} {version}."), head
        assert "https://github.com/" in text.split("\n\n", 1)[0], f"{name}: its header names its source URL"
        assert "MIT License" in text and "Permission is hereby granted" in text, name
        assert f"{name}=={version}" in (REPO / "bundle" / "requirements.lock").read_text().lower().replace("_", "-")


def _dist(site: Path, name: str, version: str, licence_file: bool = False) -> None:
    dist = site / f"{name.replace('-', '_')}-{version}.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text(f"Name: {name}\nVersion: {version}\nLicense: MIT\n")
    if licence_file:
        (dist / "LICENSE").write_text(f"{name}'s own license")


def test_the_index_lists_playcovers_stub_when_it_is_staged(tmp_path):
    """RC audit #3: components.json listed the stub, INDEX.txt didn't."""
    contents = tmp_path / "Contents"
    (contents / "Resources/GraftPython/lib/python3.13/site-packages").mkdir(parents=True)
    extra = tmp_path / "tool-licences"
    extra.mkdir()
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    index = (gb.collect_licenses(contents, pins, extra) / "INDEX.txt").read_text()
    assert gb.STUB_COMPONENT not in index
    (contents / gb.STUB_IN_APP).mkdir(parents=True)
    (contents / gb.STUB_IN_APP / "Preferences").write_bytes(b"iOS code")
    index = (gb.collect_licenses(contents, pins, extra) / "INDEX.txt").read_text()
    line = next(line for line in index.splitlines() if line.startswith(gb.STUB_COMPONENT))
    assert line.startswith(f"{gb.STUB_COMPONENT} — MIT — built here from Graft's own source")
    assert "python-packages/graft-ipa-" in line


def test_packages_without_a_licence_file_get_their_upstream_one(tmp_path):
    site = tmp_path / "Contents/Resources/GraftPython/lib/python3.13/site-packages"
    for name, version in VENDORED_FOR_PACKAGES.items():
        _dist(site, name, version)
    _dist(site, "has-one", "1.0", licence_file=True)
    extra = tmp_path / "tool-licences"
    extra.mkdir()
    gb.collect_licenses(tmp_path / "Contents", gb.load_pins(REPO / "bundle" / "pins.toml"), extra)
    lic = tmp_path / "Contents/Resources/Licenses"
    for name, version in VENDORED_FOR_PACKAGES.items():
        got = (lic / "python-packages" / f"{name}-{version}" / "LICENSE").read_text()
        assert got == (REPO / "bundle/licenses" / f"{name}-LICENSE").read_text(), name
    assert (lic / "python-packages/has-one-1.0/LICENSE").read_text() == "has-one's own license"
    # Every package has a license file in Licenses/.
    for dist in site.glob("*.dist-info"):
        name, version = dist.name.removesuffix(".dist-info").rsplit("-", 1)
        assert list((lic / "python-packages" / f"{name.replace('_', '-')}-{version}").iterdir()), name
    index = (lic / "INDEX.txt").read_text()
    assert "loguru 0.7.3 — MIT (the package ships no license file; LICENSE is from its upstream repository)" in index


def test_a_vendored_licence_for_another_version_is_refused(tmp_path):
    """The text was taken at one version; a newer one may have changed license."""
    site = tmp_path / "Contents/Resources/GraftPython/lib/python3.13/site-packages"
    _dist(site, "loguru", "0.8.0")
    extra = tmp_path / "tool-licences"
    extra.mkdir()
    with pytest.raises(gb.BundleError, match="loguru"):
        gb.collect_licenses(tmp_path / "Contents", gb.load_pins(REPO / "bundle" / "pins.toml"), extra)


# ------------------------------------------------------------ cyan's iOS frameworks

CYAN_EXTRAS = "Resources/GraftPython/lib/python3.13/site-packages/cyan/extras"

#: cyan/extras at the pinned cyan: each framework's Info.plist (bundle id,
#: version), its license, and the file in bundle/licenses/cyan-extras with
#: the text. Written out, not read from gb.CYAN_FRAMEWORKS, so dropping or
#: changing one there fails these tests.
PINNED_EXTRAS = {
    "Cephei.framework": ("ws.hbang.common", "1.0.0", "Apache-2.0", "libcephei-LICENSE"),
    "CepheiPrefs.framework": ("ws.hbang.common.prefs", "1.0.0", "Apache-2.0", "libcephei-LICENSE"),
    "CepheiUI.framework": ("ws.hbang.common.UI", "1.0.0", "Apache-2.0", "libcephei-LICENSE"),
    "CydiaSubstrate.framework": ("ellekit", "1.1.3", "BSD-3-Clause", "ellekit-LICENSE"),
    "Orion.framework": ("dev.theos.orion", "1.0.2", "MIT", "orion-LICENSE"),
}


UPSTREAM_VERSIONS = {"Cephei.framework": "1.17", "CepheiPrefs.framework": "1.17", "CepheiUI.framework": "1.17",
                     "CydiaSubstrate.framework": "1.1.3", "Orion.framework": "1.0.2"}


def _framework(extras: Path, name: str, bundle_id: str, version: str) -> Path:
    """A framework folder shaped like cyan's: Info.plist, its executable (a
    Mach-O of its own bytes), a resource."""
    import plistlib
    exe = name.removesuffix(".framework")
    d = extras / name
    d.mkdir(parents=True)
    (d / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleExecutable": exe, "CFBundleIdentifier": bundle_id, "CFBundleShortVersionString": version}))
    (d / exe).write_bytes(mb.thin([mb.dylib_cmd(f"@rpath/{name}/{exe}")], filetype=mb.MH_DYLIB))
    (d / "icon.png").write_bytes(b"\x89PNG, not a Mach-O")
    return d


def _extras(contents: Path, frameworks: dict[str, tuple[str, ...]] = PINNED_EXTRAS) -> Path:
    extras = contents / CYAN_EXTRAS
    for name, (bundle_id, version, *_) in frameworks.items():
        _framework(extras, name, bundle_id, version)
    return extras


def _audit(monkeypatch, extras: Path) -> None:
    """Pin gb.CYAN_FRAMEWORKS to the fake executables in `extras`, as an
    audit of them would; everything else in each entry stays as it is."""
    import dataclasses

    def sha(name: str) -> str:
        return hashlib.sha256((extras / name / name.removesuffix(".framework")).read_bytes()).hexdigest()

    monkeypatch.setattr(gb, "CYAN_FRAMEWORKS", {
        name: dataclasses.replace(fw, sha256=sha(name)) if (extras / name).is_dir() else fw
        for name, fw in gb.CYAN_FRAMEWORKS.items()})


def test_every_framework_in_cyan_extras_gets_its_licence_text_and_a_components_entry(tmp_path, monkeypatch):
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    cyan = pins["cyan"]
    contents, reports, _ = _fake_install(tmp_path)
    extras = _extras(contents)
    _audit(monkeypatch, extras)
    tools = tmp_path / "tool-licences"
    tools.mkdir()
    lic = gb.collect_licenses(contents, pins, tools)
    entries = json.loads(gb.write_components(contents, pins, reports).read_text())
    listed = {e["name"]: e for e in entries if "/cyan/extras/" in e["source_url"]}
    index = (lic / "INDEX.txt").read_text()
    staged = sorted(p.name for p in extras.iterdir())
    assert staged == sorted(PINNED_EXTRAS)
    for name in staged:
        _, version, licence, text = PINNED_EXTRAS[name]
        stem = name.removesuffix(".framework")
        got = (lic / "cyan-extras" / f"{stem}-LICENSE").read_bytes()
        assert got == (REPO / "bundle/licenses/cyan-extras" / text).read_bytes(), name
        line = next(line for line in index.splitlines() if line.startswith(f"  {name} — "))
        assert line.startswith(f"  {name} — {licence} — ") and line.endswith(f" — cyan-extras/{stem}-LICENSE"), line
        binary = extras / name / stem
        # Fix-check #24: the release it is, not its Info.plist's (every
        # Cephei's says 1.0.0), which stays as bundle_version.
        assert listed[name] == {
            "name": name, "version": UPSTREAM_VERSIONS[name], "bundle_version": version, "kind": "artifact",
            "source_url": f"{cyan.url}#pyzule-rw-{cyan.version}/cyan/extras/{name}/{stem}",
            "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "license": licence,
            "cyan_commit": cyan.version, "licence_files": [f"cyan-extras/{stem}-LICENSE"]}, name
    assert sorted(listed) == staged
    assert len({e["sha256"] for e in listed.values()}) == len(staged), "each hash is its own binary's"


def _refused_case(extras: Path, case: str) -> str:
    """Make `case` true of an otherwise good cyan/extras; returns what the refusal names."""
    import plistlib
    if case == "an unknown framework":
        _framework(extras, "Libra.framework", "com.example.libra", "2.0")
        return r"cyan/extras/Libra\.framework is not one of the frameworks whose license is known"
    if case == "a loose dylib":
        (extras / "libextra.dylib").write_bytes(mb.thin([], filetype=mb.MH_DYLIB))
        return r"cyan/extras/libextra\.dylib is not one of"
    info = extras / "Orion.framework" / "Info.plist"
    plist = plistlib.loads(info.read_bytes())
    if case == "another version":
        info.write_bytes(plistlib.dumps({**plist, "CFBundleShortVersionString": "1.1.0"}))
        return r"cyan/extras/Orion\.framework is dev\.theos\.orion 1\.1\.0, but its license was checked for"
    if case == "another bundle id":
        info.write_bytes(plistlib.dumps({**plist, "CFBundleIdentifier": "com.example.orion"}))
        return r"cyan/extras/Orion\.framework is com\.example\.orion 1\.0\.2"
    if case == "a second Mach-O":
        (extras / "Orion.framework" / "Helper.dylib").write_bytes(mb.thin([], filetype=mb.MH_DYLIB))
        return r"cyan/extras/Orion\.framework holds Mach-O files besides its executable.*Helper\.dylib"
    if case == "no executable":
        (extras / "Orion.framework" / "Orion").unlink()
        return r"cyan/extras/Orion\.framework has no executable Orion"
    if case == "another binary":
        (extras / "Orion.framework" / "Orion").write_bytes(
            mb.thin([mb.dylib_cmd("@rpath/Orion.framework/Orion"), mb.rpath_cmd("@loader_path")],
                    filetype=mb.MH_DYLIB))
        return (r"cyan/extras/Orion\.framework/Orion is not the binary whose license was audited: "
                r"its SHA-256 is [0-9a-f]{64}.*The license audit must be redone for the new binary")
    raise AssertionError(case)


REFUSED_CASES = ["an unknown framework", "a loose dylib", "another version", "another bundle id",
                 "a second Mach-O", "no executable", "another binary"]


@pytest.mark.parametrize("case", REFUSED_CASES)
def test_a_framework_whose_licence_was_not_checked_is_refused(tmp_path, monkeypatch, case):
    """A new cyan pin can't bring in a binary nobody has found the license of."""
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    contents = tmp_path / "Contents"
    extras = _extras(contents)
    _audit(monkeypatch, extras)
    match = _refused_case(extras, case)
    tools = tmp_path / "tool-licences"
    tools.mkdir()
    (tmp_path / "reports").mkdir()
    with pytest.raises(gb.BundleError, match=match):
        gb.collect_licenses(contents, pins, tools)
    with pytest.raises(gb.BundleError, match=match):
        gb.write_components(contents, pins, tmp_path / "reports")


class _Built(Exception):
    """Raised by a fake build_openssl: the stage got past the frameworks."""


@pytest.mark.parametrize("unknown", [False, True])
def test_stage_stops_at_an_unknown_framework_before_building_anything(tmp_path, monkeypatch, unknown):
    def stage_python(pins, contents, reports=None, arch="arm64"):
        extras = _extras(contents)
        _audit(monkeypatch, extras)
        if unknown:
            _refused_case(extras, "an unknown framework")
        return contents / "Resources/GraftPython"

    def build_openssl(*args, **kw):
        raise _Built

    monkeypatch.setattr(gb, "stage_python", stage_python)
    monkeypatch.setattr(gb, "build_openssl", build_openssl)
    if unknown:
        with pytest.raises(gb.BundleError, match=r"cyan/extras/Libra\.framework is not one of"):
            gb.stage(tmp_path / "stage", tmp_path / "work")
    else:
        with pytest.raises(_Built):
            gb.stage(tmp_path / "stage", tmp_path / "work")


def test_each_framework_is_pinned_by_the_hash_audited_at_a_cyan_commit(tmp_path):
    """The real table, unpatched: every entry pins a SHA-256 and names the
    cyan commit it was audited at, and a framework with the right name, id
    and version but other bytes is refused (Cephei's Info.plist says 1.0.0
    in every release, so only the hash tells one Cephei from another)."""
    for name, fw in gb.CYAN_FRAMEWORKS.items():
        assert re.fullmatch(r"[0-9a-f]{64}", fw.sha256) and re.fullmatch(r"[0-9a-f]{40}", fw.audited_at), name
    assert len({fw.sha256 for fw in gb.CYAN_FRAMEWORKS.values()}) == len(gb.CYAN_FRAMEWORKS)
    contents = tmp_path / "Contents"
    _extras(contents, {"Cephei.framework": PINNED_EXTRAS["Cephei.framework"]})
    with pytest.raises(gb.BundleError, match=r"Cephei\.framework/Cephei is not the binary whose license was "
                                             r"audited.*at cyan [0-9a-f]{40}\) saw 7b3b0056c992"):
        gb.cyan_frameworks(contents)


def test_cyan_extras_licence_texts_are_upstreams_with_a_header_naming_the_commit():
    texts = REPO / "bundle" / "licenses" / "cyan-extras"
    assert sorted(f.name for f in texts.iterdir()) == sorted({t for *_, t in PINNED_EXTRAS.values()})
    bodies = {}
    for f in texts.iterdir():
        header, body = f.read_text().split("\n\n", 1)
        assert all(line.startswith("# ") for line in header.splitlines()), f.name
        assert re.search(r"^# https://github\.com/[^/]+/[^/]+/blob/[0-9a-f]{40}/LICENSE(\.md)?$", header, re.M), f.name
        bodies[f.name] = body
    # libcephei 1.17's credit for the icon its prefs framework ships (package.png), verbatim.
    assert bodies["libcephei-LICENSE"].startswith(
        'Includes "Box" (Cydia) icon by Даниил Пронин from the Noun Project.\n\n---\n')
    assert "Apache License\n                           Version 2.0, January 2004" in bodies["libcephei-LICENSE"]
    assert bodies["orion-LICENSE"].startswith("MIT License\n\nCopyright (c) 2020 Theos\n\nPermission is hereby")
    assert bodies["ellekit-LICENSE"].startswith("Copyright (c) 2022 Évelyne Bélanger All rights reserved.")
    assert "Neither ElleKit nor the names of its" in bodies["ellekit-LICENSE"]


@slow
def test_every_framework_in_the_pinned_cyan_has_its_licence_and_a_components_entry(tmp_path):
    """The real cyan/extras: each framework is known, and each components.json
    entry's source_url names a member of cyan's archive with that hash."""
    import zipfile
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    archive = gb.fetch(pins["cyan"])
    contents = tmp_path / "Contents"
    extras = contents / CYAN_EXTRAS
    members = {}
    with zipfile.ZipFile(archive) as z:
        for n in z.namelist():
            if "/cyan/extras/" in n and not n.endswith("/"):
                members[n] = z.read(n)
                out = extras / n.split("/cyan/extras/", 1)[1]
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(members[n])
    tools = tmp_path / "tool-licences"
    tools.mkdir()
    (tmp_path / "reports").mkdir()
    lic = gb.collect_licenses(contents, pins, tools)
    entries = json.loads(gb.write_components(contents, pins, tmp_path / "reports").read_text())
    listed = {e["name"]: e for e in entries if "/cyan/extras/" in e["source_url"]}
    frameworks = sorted(p.name for p in extras.iterdir())
    assert frameworks == sorted(PINNED_EXTRAS) and sorted(listed) == frameworks
    for name in frameworks:
        assert (lic / "cyan-extras" / f"{name.removesuffix('.framework')}-LICENSE").stat().st_size, name
        url, member = listed[name]["source_url"].split("#", 1)
        assert url == pins["cyan"].url
        assert listed[name]["sha256"] == hashlib.sha256(members[member]).hexdigest(), name
    shipped = (extras / "CydiaSubstrate.framework" / "LICENSE").read_text()
    assert (lic / "cyan-extras/CydiaSubstrate-LICENSE").read_text().split("\n\n", 1)[1] == shipped


# ------------------------------------------------------------ the source archive

COPYLEFT_LICENCES = ["GPL-3.0-or-later", "GNU General Public License v3 or later (GPLv3+)", "LGPL-2.1",
                     "LGPL-3.0-only", "MPL-2.0", "wxWindows Library License, Version 3.1", "AGPL-3.0",
                     "LGPL-2.1-or-later", "APSL-2.0", "GNU GENERAL PUBLIC LICENSE Version 3, 29 June 2007",
                     "EPL-2.0",
                     # Review #33: no word boundary after GPL in these, and EUPL was missing.
                     "GPLv3", "GPLv2+", "LGPLv3+", "LGPLv2", "AGPLv3", "EUPL-1.2", "License :: OSI Approved :: GPLv3"]
PERMISSIVE_LICENCES = ["MIT", "MIT License", "Apache-2.0", "Apache Software License", "BSD License", "PSF-2.0",
                       "PSFL", "0BSD", "MIT-CMU", "ISC License (ISCL)", "Unlicense", "Apache-2.0 OR BSD-3-Clause",
                       "BSD-2-Clause AND Unlicense AND MIT", "not stated in its metadata; see its license file"]


@pytest.mark.parametrize("licence", COPYLEFT_LICENCES)
def test_copyleft_licences_are_recognised(licence):
    assert gb.is_copyleft(licence)


@pytest.mark.parametrize("licence", PERMISSIVE_LICENCES)
def test_permissive_licences_are_not(licence):
    assert not gb.is_copyleft(licence)


#: cyan's pinned zip as the fixture has it: its package, its iOS frameworks
#: (which the app ships, so their files stay) and its prebuilt tools.
CYAN_FILES = {"cyan/__init__.py": (b"# cyan", 0o644), "pyproject.toml": (b"[project]", 0o644),
              "cyan/extras/Orion.framework/Orion": (b"Orion's binary", 0o755),
              "cyan/tools/Darwin/arm64/ldid": (b"a prebuilt ldid", 0o755),
              "cyan/tools/Linux/x86_64/otool": (b"a prebuilt otool", 0o755)}


def _cyan_archive(path: Path, pin) -> Path:
    import zipfile

    top = f"pyzule-rw-{pin.version}"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo(f"{top}/"), b"")
        for rel, (data, mode) in CYAN_FILES.items():
            info = zipfile.ZipInfo(f"{top}/{rel}", date_time=(2026, 5, 1, 12, 0, 0))
            info.external_attr = mode << 16
            z.writestr(info, data)
    return path


REAL_GRAFT_SOURCE_TAR = gb.graft_source_tar


def _sources_fixture(tmp_path: Path, monkeypatch, *, sdist_sha=None):
    """components.json naming every pin and five Python packages, a lock for
    those packages, and fakes for the downloads and for git archive."""
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    packages = [("gplpkg", "1.0", "GPL-3.0-or-later"), ("lgplpkg", "2.0", "LGPL-2.1"),
                ("mplpkg", "3.0", "MPL-2.0"), ("wx_pkg", "4.0", "wxWindows Library License, Version 3.1"),
                ("mitpkg", "5.0", "MIT"), ("wheelonly", "6.0", "MIT")]
    lock = tmp_path / "requirements.lock"
    lock.write_text("".join(f"{n.replace('_', '-')}=={v} \\\n    --hash=sha256:{hashlib.sha256(n.encode()).hexdigest()} "
                            f"\\\n    --hash=sha256:{'f' * 64}\n    # via graft-ipa\n" for n, v, _ in packages))
    entries = [{"name": built, "version": pin.version,
                "kind": "built-from-source" if name in gb.BUILT_FROM_SOURCE else "artifact",
                "source_url": pin.url, "sha256": pin.sha256, "license": pin.license}
               for name, pin in pins.items() for built in pin.builds or (name,)]
    entries += [{"name": n, "version": v, "kind": "python-package", "source_url": "https://files.pythonhosted.org/x",
                 "sha256": "e" * 64, "license": lic} for n, v, lic in packages]
    components = tmp_path / "components.json"
    components.write_text(json.dumps(entries))
    cache = tmp_path / "cache"
    cache.mkdir()

    licences = {gb._norm(n): lic for n, _, lic in packages}

    def fetch(pin, cache_dir=None):
        f = cache / pin.filename
        if pin.name == "cyan":
            return _cyan_archive(f, pin)
        if pin.url.startswith("https://files.pythonhosted.org/"):
            # A source distribution states its own license, in its PKG-INFO.
            return _sdist_with_licence(f, pin.name, pin.version, licences[pin.name])
        f.write_bytes(f"the bytes of {pin.name}".encode())
        return f

    def pypi(name, version):
        if name == "wheelonly":
            return None  # PyPI has no source distribution for it
        return (f"https://files.pythonhosted.org/packages/ab/{name}-{version}.tar.gz",
                sdist_sha or hashlib.sha256(name.replace("-", "_").encode()).hexdigest())

    def git_archive(dest, prefix, repo=None, commit=None):
        with tarfile.open(dest, "w") as tf:
            data = b"[project]\nname = 'graft-ipa'\n"
            info = tarfile.TarInfo(f"{prefix}pyproject.toml")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        return "0123456789abcdef0123456789abcdef01234567", 1_700_000_000

    vendor = tmp_path / "vendor.tar.gz"
    vendor.write_bytes(b"ipatool's vendored modules")
    monkeypatch.setattr(gb, "fetch", fetch)
    monkeypatch.setattr(gb, "_pypi_sdist", pypi)
    monkeypatch.setattr(gb, "graft_source_tar", git_archive)
    unfairplay_vendor = tmp_path / "unfairplay-vendor.tar.gz"
    unfairplay_vendor.write_bytes(b"UnfairPlay's vendored modules")
    monkeypatch.setattr(gb, "built_sources", lambda pins: [(vendor, "ipatool/vendor.tar.gz"),
                                                           (unfairplay_vendor, "unfairplay/vendor.tar.gz")])
    return pins, components, lock, packages


def test_the_source_archive_covers_every_copyleft_component(tmp_path, monkeypatch):
    pins, components, lock, packages = _sources_fixture(tmp_path, monkeypatch)
    out = tmp_path / "out" / "Graft-9.9-sources.tar.gz"
    out.parent.mkdir()
    gb.write_sources(components, out, lock=lock)
    with tarfile.open(out) as tf:
        members = {m.name: m for m in tf.getmembers()}
        md = tf.extractfile("Graft-9.9-sources/SOURCES.md").read().decode()
    top = "Graft-9.9-sources"
    assert f"{top}/graft/pyproject.toml" in members
    # One archive serves both bundles: each architecture's pins.
    for arch_pins in gb.load_pins_by_arch(REPO / "bundle" / "pins.toml").values():
        for name, pin in arch_pins.items():
            if name in gb.LICENCE_TEXT_PINS:
                assert not [m for m in members if f"/pinned/{name}/" in m], f"{name} is license texts, not in the app"
            elif (name in gb.BUILT_FROM_SOURCE or pin.from_source or gb.is_copyleft(pin.license)
                  or name == "cyan") and not pin.source:
                assert f"{top}/pinned/{name}/{gb.archived_filename(pin)}" in members, name
                assert pin.sha256 in md and pin.url in md, name
            elif pin.source:
                # A copyleft binary: its source pin is what goes in.
                assert f"{top}/pinned/{name}/{pin.filename}" not in members, f"{name}'s binary, not its source"
                assert f"{top}/pinned/{pin.source}/{arch_pins[pin.source].filename}" in members, name
            else:
                assert f"{top}/pinned/{name}/{pin.filename}" not in members, f"{name} is neither built here nor copyleft"
    assert f"{top}/pinned/ipatool/vendor.tar.gz" in members
    assert f"{top}/pinned/unfairplay/vendor.tar.gz" in members
    assert f"{top}/pinned/unfairplay/{pins['unfairplay'].filename}" in members, "UnfairPlay's source, not its release"
    unfairplay = _section(md, "unfairplay")
    commit = gb.source_commit(pins["unfairplay"])[1]
    assert "- In the app: `Contents/Helpers/bin/unfairplay`" in unfairplay and commit in unfairplay
    assert "vcs.modified=false" in unfairplay and "`--identifier unfairplay`" not in unfairplay
    assert "identifier `unfairplay`" in unfairplay and "no entitlements" in unfairplay
    assert "unfairplay" not in md.split("## Not built here", 1)[1].split("\n\n", 2)[1], "not a release file now"
    assert "go mod vendor" in md and "-trimpath" in md and "com.apple.security.cs.allow-jit" in md
    assert "com.apple.security.cs.disable-library-validation" in md and gb.LICENCE_STATEMENT in md
    assert "libunicorn" not in md and not [m for m in members if "unicorn" in m.lower()], "no Unicorn source"
    assert "unpatched" not in _section(md, "ipatool") and "IPATOOL_UNICORN_LIBRARY" not in md
    # 2.1: the shipped ipatool carries its range-reader patch, named with its
    # hash in ipatool's own section, applied before the steps that build it.
    ((patch, sha),) = gb.patch_hashes("ipatool")
    ipatool_md = _section(md, "ipatool")
    assert f"`bundle/patches/{patch}` (SHA-256 `{sha}`)" in ipatool_md
    applied = gb.shell_line([*gb.PATCH_ARGV, f"$GRAFT/bundle/patches/{patch}"])
    assert applied in ipatool_md
    assert ipatool_md.index(applied) < ipatool_md.index(gb.shell_line(["go", "mod", "vendor"]))
    # Review #58: every bundled package's source, copyleft or not; one PyPI
    # has none for is named, with where its source is.
    for name, version, _licence in packages:
        sdist = f"{top}/sdists/{gb._norm(name)}-{version}.tar.gz"
        assert (sdist in members) == (name != "wheelonly"), name
        assert f"{gb._norm(name)}-{version}.tar.gz" in md or name == "wheelonly"
    assert "wheelonly 6.0" in md.split("no source distribution", 1)[1]
    assert f"{top}/pinned/cyan/{pins['cyan'].version}-without-tools.zip" in members, "cyan's pinned archive, repacked"
    assert f"{top}/pinned/cyan/{pins['cyan'].filename}" not in members, "not as pinned: it holds prebuilt tools"
    # Review #48: each recipe whole, not just its first word (`make` is in all of them).
    for step in (gb.cctools_steps([]) + gb.ldid_steps("$LIBPLIST", "$OPENSSL", []) + gb.insert_dylib_steps()
                 + gb.libplist_steps("$DESTDIR", []) + gb.openssl_steps("$DESTDIR", [])
                 + gb.ipatool_steps(pins["ipatool"].version) + gb.stub_steps()
                 + gb.unfairplay_steps(pins["unfairplay"].version, *gb.source_commit(pins["unfairplay"]))):
        assert gb.shell_line(step) in md, step
    for name in gb.PATCHES:
        for patch, sha in gb.patch_hashes(name):
            assert f"$GRAFT/bundle/patches/{patch}" in md and sha in md, (name, patch)
    assert "0123456789abcdef0123456789abcdef01234567" in md
    # Nothing in the archive names whoever built it.
    for m in members.values():
        assert (m.uid, m.gid, m.uname, m.gname) == (0, 0, "", ""), m.name
    assert not list(out.parent.glob("*.part"))


def test_cyans_prebuilt_tools_never_reach_the_source_archive(tmp_path, monkeypatch):
    """Review 2.0 bundle #1: cyan's zip carries 24 prebuilt executables in
    cyan/tools/ (ldid, cctools and insert_dylib for five platforms), built by
    upstream with no source for those builds. The app carries none of them;
    the archive gets cyan's zip without them, and SOURCES.md says so."""
    import hashlib
    import zipfile

    pins, components, lock, _ = _sources_fixture(tmp_path, monkeypatch)
    out = tmp_path / "out" / "Graft-9.9-sources.tar.gz"
    out.parent.mkdir()
    names = gb.write_sources(components, out, lock=lock)
    assert not [n for n in names if "/cyan/tools/" in n]
    cyan = pins["cyan"]
    inner = f"Graft-9.9-sources/pinned/cyan/{cyan.version}-without-tools.zip"
    with tarfile.open(out) as tf:
        data = tf.extractfile(inner).read()
        md = tf.extractfile("Graft-9.9-sources/SOURCES.md").read().decode()
        for m in tf.getmembers():
            if m.isfile() and m.name.endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(tf.extractfile(m).read())) as z:
                    assert not [n for n in z.namelist() if "/cyan/tools/" in n], m.name
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        got = {i.filename.split("/", 1)[1]: (z.read(i), i.external_attr >> 16) for i in z.infolist()
               if not i.is_dir()}
    assert got == {rel: v for rel, v in CYAN_FILES.items() if not rel.startswith("cyan/tools/")}
    section = _section(md, "cyan")
    assert f"`pinned/cyan/{cyan.version}-without-tools.zip`, SHA-256 `{hashlib.sha256(data).hexdigest()}`" in section
    assert cyan.url in section and cyan.sha256 in section, "repacked from the pin, which it names"
    assert "without `cyan/tools/`" in section and "2 prebuilt executables" in section
    # d14-bundle-1: not "every other file as it is", which the modes weren't.
    assert "Every other file keeps its contents, times and mode, and the zip keeps its comment" in section
    assert "as it is" not in section
    assert "with no source" in section and "Graft.app carries none of them" in section


def test_cyans_repacked_archive_is_the_same_bytes_every_time(tmp_path):
    pin = gb.load_pins(REPO / "bundle" / "pins.toml")["cyan"]
    archive = _cyan_archive(tmp_path / pin.filename, pin)
    one, two = tmp_path / "1.zip", tmp_path / "2.zip"
    assert gb.cyan_source_zip(archive, one) == gb.cyan_source_zip(archive, two) == [
        f"pyzule-rw-{pin.version}/cyan/tools/Darwin/arm64/ldid", f"pyzule-rw-{pin.version}/cyan/tools/Linux/x86_64/otool"]
    assert one.read_bytes() == two.read_bytes()


def _github_like_cyan_zip(path: Path, top: str) -> Path:
    """A zip shaped like GitHub's archive of cyan: directories and plain
    files made on MS-DOS (`create_system` 0) with no Unix mode (a file's
    `external_attr` 0, a directory's 0x10), executables made on Unix with
    their mode, an extended timestamp (UT, 9 bytes) on each member that
    differs from its DOS time, the text flag on text files, and the commit
    as the zip's comment."""
    import struct
    import zipfile

    stamp = 1_700_000_000  # 2023-11-14 22:13:20 UTC
    ut = struct.pack("<HHBl", 0x5455, 5, 1, stamp)
    members = [(f"{top}/", 0, 0x10, b""), (f"{top}/cyan/", 0, 0x10, b""),
               (f"{top}/cyan/__init__.py", 0, 0, b"# cyan"), (f"{top}/README.md", 0, 0, b"# readme"),
               (f"{top}/cyan/extras/", 0, 0x10, b""),
               (f"{top}/cyan/extras/run.sh", 3, (0o100755 << 16) | 0x20, b"#!/bin/sh"),
               (f"{top}/cyan/tools/Darwin/arm64/ldid", 3, (0o100755 << 16) | 0x20, b"a prebuilt ldid")]
    with zipfile.ZipFile(path, "w") as z:
        for name, system, attr, data in members:
            info = zipfile.ZipInfo(name, date_time=(2025, 2, 15, 6, 15, 22))
            info.create_system, info.extra = system, ut
            info.internal_attr = 1 if name.endswith((".py", ".md")) else 0  # GitHub's text flag
            info.compress_type = zipfile.ZIP_STORED if name.endswith("/") else zipfile.ZIP_DEFLATED
            z.writestr(info, data)
            info.external_attr = attr  # `writestr` turns a 0 into rw-------; GitHub's is 0
        z.comment = b"0123456789abcdef0123456789abcdef01234567"
    return path


def _unzipped_modes(archive: Path, into: Path) -> dict[str, tuple[str, int]]:
    """Each path `/usr/bin/unzip` (Info-ZIP, as on every Mac) makes from
    `archive`, under umask 022, with its mode and mtime."""
    import stat

    into.mkdir()
    subprocess.run(["/usr/bin/unzip", "-q", str(archive), "-d", str(into)], check=True, umask=0o022,
                   capture_output=True)
    return {p.relative_to(into).as_posix(): (stat.filemode(p.lstat().st_mode), int(p.lstat().st_mtime))
            for p in sorted(into.rglob("*"))}


@pytest.mark.skipif(not Path("/usr/bin/unzip").exists(), reason="needs Info-ZIP's /usr/bin/unzip")
def test_cyans_repacked_zip_unpacks_with_the_pins_modes_and_keeps_its_comment(tmp_path):
    """The delta audit's d14-bundle-1: the repack made a fresh ZipInfo per
    member and kept only its time and `external_attr`. A new ZipInfo says
    Unix, and `writestr` turns an `external_attr` of 0 into rw-------, so
    under unzip GitHub's plain files came out -rw------- and its
    directories d--------- (mode 000, the tree unopenable), and the zip's
    comment, the upstream commit, was gone."""
    import zipfile

    top = "pyzule-rw-0"
    pinned = _github_like_cyan_zip(tmp_path / "pinned.zip", top)
    repack = tmp_path / "repack.zip"
    gb.cyan_source_zip(pinned, repack)
    original = {p: v for p, v in _unzipped_modes(pinned, tmp_path / "a").items()
                if not p.startswith(f"{top}/cyan/tools")}
    repacked = _unzipped_modes(repack, tmp_path / "b")
    assert repacked == original
    assert repacked[f"{top}/README.md"][0] == "-rw-r--r--"
    assert repacked[f"{top}/cyan"][0] == "drwxr-xr-x"
    assert repacked[f"{top}/cyan/extras/run.sh"][0] == "-rwxr-xr-x"
    assert repacked[f"{top}/README.md"][1] == 1_700_000_000, "the extended timestamp, not the DOS time"
    with zipfile.ZipFile(pinned) as a, zipfile.ZipFile(repack) as b:
        assert b.comment == a.comment
        kept = [i for i in a.infolist() if "/cyan/tools/" not in i.filename]
        def attributes(i: zipfile.ZipInfo) -> tuple:
            return i.filename, i.create_system, i.external_attr, i.internal_attr, i.date_time, i.extra

        assert [attributes(i) for i in b.infolist()] == [attributes(i) for i in kept]
        assert [b.read(i.filename) for i in kept] == [a.read(i) for i in kept]


def test_a_copyleft_package_whose_sdist_the_lock_does_not_vouch_for_is_refused(tmp_path, monkeypatch):
    _, components, lock, _ = _sources_fixture(tmp_path, monkeypatch, sdist_sha="9" * 64)
    out = tmp_path / "Graft-9.9-sources.tar.gz"
    with pytest.raises(gb.BundleError, match=r"requirements\.lock"):
        gb.write_sources(components, out, lock=lock)
    assert not out.exists()


def test_a_copyleft_package_missing_from_the_lock_is_refused(tmp_path, monkeypatch):
    _, components, lock, _ = _sources_fixture(tmp_path, monkeypatch)
    lock.write_text(lock.read_text().replace("gplpkg==1.0", "gplpkg==1.1"))
    with pytest.raises(gb.BundleError, match="gplpkg"):
        gb.write_sources(components, tmp_path / "Graft-9.9-sources.tar.gz", lock=lock)


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": os.devnull}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout


def _objects(repo: Path) -> list[str]:
    return sorted(str(p.relative_to(repo)) for p in (repo / ".git" / "objects").rglob("*") if p.is_file())


def _exported_tree(public: Path, tar: Path, prefix: str) -> str:
    """The tree of a fresh repository committing the archive's files, as a
    public export does."""
    public.mkdir()
    _git(public, "init", "-q")
    with tarfile.open(tar) as tf:
        for m in tf.getmembers():
            if m.isfile():
                out = public / m.name.removeprefix(prefix)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(tf.extractfile(m).read())
                out.chmod(m.mode & 0o777)
    _git(public, "add", "-A")
    _git(public, "commit", "-q", "-m", "the public export")
    return _git(public, "rev-parse", "HEAD^{tree}").strip()


@needs_tools("git")
def test_graft_source_is_head_without_the_internal_notes_and_only_from_a_clean_checkout(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _tree(repo, ["pyproject.toml", "graft_ipa/cli.py", "docs/network.md", "docs/superpowers/plans/x.md",
                 "tweaks-src/SomeTweak/Tweak.x"])
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "x")
    dest = tmp_path / "graft.tar"
    objects = _objects(repo)
    tree, when = gb.graft_source_tar(dest, "top/graft/", repo=repo)
    assert when > 0
    with tarfile.open(dest) as tf:
        names = {m.name for m in tf.getmembers() if m.isfile()}
    assert names == {"top/graft/pyproject.toml", "top/graft/graft_ipa/cli.py", "top/graft/docs/network.md"}
    # Review 2.0, bundle #8: not the private commit, but the tree a public
    # export of it (the same exclusions) commits, which anyone can match.
    assert tree != _git(repo, "rev-parse", "HEAD").strip() and tree != _git(repo, "rev-parse", "HEAD^{tree}").strip()
    assert tree == _exported_tree(tmp_path / "public", dest, "top/graft/")
    assert _objects(repo) == objects, "nothing written into the private repository"
    (repo / "graft_ipa/cli.py").write_text("changed, not committed")
    with pytest.raises(gb.BundleError, match=r"cli\.py"):
        gb.graft_source_tar(dest, "top/graft/", repo=repo)


@needs_tools("git")
def test_an_untracked_file_is_a_dirty_tree_too(tmp_path):
    """Review #9/#45: hatchling packs untracked files under graft_ipa/ into
    the wheel, and `git archive` leaves them out."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _tree(repo, ["pyproject.toml", "graft_ipa/cli.py"])
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "x")
    (repo / "graft_ipa" / "new_feature.py").write_text("untracked")
    with pytest.raises(gb.BundleError, match=r"new_feature\.py"):
        gb.graft_source_tar(tmp_path / "g.tar", "top/graft/", repo=repo)


@needs_tools("git")
def test_the_source_archive_is_the_commit_the_release_recorded(tmp_path):
    """Review #11/#26: graft-release records HEAD before it builds, builds
    from an export of it, and archives that commit, not HEAD at the end."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _tree(repo, ["pyproject.toml", "graft_ipa/cli.py"])
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "built")
    built = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "graft_ipa" / "later.py").write_text("committed during the build")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "later")
    (repo / "scratch.txt").write_text("untracked, but the archive is of a commit")
    tree, _ = gb.graft_source_tar(tmp_path / "g.tar", "top/graft/", repo=repo, commit=built)
    assert tree == _git(repo, "rev-parse", f"{built}^{{tree}}").strip(), "the recorded commit's tree, nothing excluded"
    with tarfile.open(tmp_path / "g.tar") as tf:
        assert {m.name for m in tf.getmembers() if m.isfile()} == {"top/graft/pyproject.toml", "top/graft/graft_ipa/cli.py"}
    with pytest.raises(gb.BundleError, match="not a commit"):
        gb.graft_source_tar(tmp_path / "g.tar", "top/graft/", repo=repo, commit="0" * 40)


@needs_tools("git")
def test_the_source_archive_names_the_tree_and_never_the_private_commit(tmp_path, monkeypatch):
    """Review 2.0, bundle #8: SOURCES.md named the private repository's
    commit, which never exists on GitHub. It names the archived tree, which
    the public export's commit has."""
    import gzip

    _, components, lock, _ = _sources_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(gb, "graft_source_tar", REAL_GRAFT_SOURCE_TAR)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _tree(repo, ["pyproject.toml", "graft_ipa/cli.py", "docs/superpowers/plans/x.md", "tweaks-src/T/Tweak.x"])
    (repo / "graft_ipa/run.sh").write_text("#!/bin/sh\n")
    (repo / "graft_ipa/run.sh").chmod(0o755)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "x")
    commit = _git(repo, "rev-parse", "HEAD").strip()
    out = tmp_path / "out" / "Graft-9.9-sources.tar.gz"
    out.parent.mkdir()
    gb.write_sources(components, out, lock=lock, repo=repo)
    raw = gzip.decompress(out.read_bytes())
    assert commit.encode() not in raw and commit[:12].encode() not in raw
    graft = tmp_path / "graft.tar"
    with tarfile.open(out) as tf, tarfile.open(graft, "w") as g:
        md = tf.extractfile("Graft-9.9-sources/SOURCES.md").read().decode()
        for m in tf.getmembers():
            if m.name.startswith("Graft-9.9-sources/graft/"):
                g.addfile(m, tf.extractfile(m) if m.isfile() else None)
    tree = _exported_tree(tmp_path / "public", graft, "Graft-9.9-sources/graft/")
    section = md.split("\n## Graft\n", 1)[1].split("\n## ", 1)[0]
    assert f"git tree `{tree}`" in section and "docs/superpowers" in section and "tweaks-src" in section
    assert "`git rev-parse <release commit>^{tree}` gives that hash" in section and "commit `" not in section


def test_a_tree_that_isnt_what_git_archive_wrote_is_refused(tmp_path):
    empty = ("100644", gb.git_blob_id(b""))
    with tarfile.open(tmp_path / "g.tar", "w") as tf:
        for name in ("top/graft/a", "top/graft/b"):
            info = tarfile.TarInfo(name)
            info.mode = 0o664
            tf.addfile(info, io.BytesIO(b""))
    gb.check_archived_tree({"a": empty, "b": empty}, tmp_path / "g.tar", "top/graft/")
    with pytest.raises(gb.BundleError, match="export-ignore"):
        gb.check_archived_tree({"a": empty, "b": empty, "c": empty}, tmp_path / "g.tar", "top/graft/")
    with pytest.raises(gb.BundleError, match="export-ignore"):
        gb.check_archived_tree({"a": empty}, tmp_path / "g.tar", "top/graft/")
    # The delta audit's d14-bundle-4: the same names, other contents or mode.
    with pytest.raises(gb.BundleError, match=r"contents or mode aren't the tree's.*export-subst.*\['b'\]"):
        gb.check_archived_tree({"a": empty, "b": ("100644", gb.git_blob_id(b"x"))}, tmp_path / "g.tar", "top/graft/")
    with pytest.raises(gb.BundleError, match=r"\['a'\]"):
        gb.check_archived_tree({"a": ("100755", empty[1]), "b": empty}, tmp_path / "g.tar", "top/graft/")


def test_a_git_blob_id_is_gits_own():
    assert gb.git_blob_id(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    assert gb.git_blob_id(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"
    assert len(gb.git_blob_id(b"", "sha256")) == 64


def _repo_with_placeholder(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _tree(repo, ["pyproject.toml"])
    (repo / "VERSION").write_text("built from $Format:%H$\n")
    (repo / "run.sh").write_text("#!/bin/sh\n")
    (repo / "run.sh").chmod(0o755)
    (repo / "link").symlink_to("pyproject.toml")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "x")
    return repo


@needs_tools("git")
def test_an_export_subst_from_the_checkouts_own_attributes_is_refused(tmp_path):
    """The delta audit's d14-bundle-4: .git/info/attributes is never
    committed or reviewed, and git archive of a commit honors it, expanding
    $Format:%H$ to the private commit while every name stays the same."""
    repo = _repo_with_placeholder(tmp_path)
    tree, _ = gb.graft_source_tar(tmp_path / "plain.tar", "top/graft/", repo=repo)
    assert tree, "no attributes: the archive is the tree, executable bit and symlink included"
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("VERSION export-subst\n")
    commit = _git(repo, "rev-parse", "HEAD").strip()
    with pytest.raises(gb.BundleError, match=r"export-subst.*\['VERSION'\]"):
        gb.graft_source_tar(tmp_path / "g.tar", "top/graft/", repo=repo, commit=commit)
    with tarfile.open(tmp_path / "g.tar") as tf:
        assert commit.encode() in tf.extractfile("top/graft/VERSION").read(), "what the check stops"


@needs_tools("git")
def test_an_export_subst_from_a_committed_gitattributes_is_refused(tmp_path):
    repo = _repo_with_placeholder(tmp_path)
    (repo / ".gitattributes").write_text("VERSION export-subst\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "attributes")
    with pytest.raises(gb.BundleError, match=r"export-subst.*\['VERSION'\]"):
        gb.graft_source_tar(tmp_path / "g.tar", "top/graft/", repo=repo)


def test_the_stage_takes_its_caches_from_the_environment(monkeypatch, tmp_path):
    """graft-release stages from an export, but keeps the checkout's caches."""
    monkeypatch.setenv("GRAFT_BUNDLE_CACHE", str(tmp_path / "c"))
    monkeypatch.setenv("GRAFT_BUNDLE_WORK", str(tmp_path / "w"))
    assert gb.cache_dirs() == (tmp_path / "c", tmp_path / "w")
    monkeypatch.setenv("GRAFT_BUNDLE_WORK", "relative/w")
    with pytest.raises(gb.BundleError, match="absolute"):
        gb.cache_dirs()


def test_the_printed_steps_run_in_a_shell_as_the_build_ran_them(tmp_path):
    """SOURCES.md's lines are for copying into a shell: $PLACEHOLDERS must
    expand there, and every other argument must arrive as one word."""
    step = ["make", "ldid", "CXXFLAGS=-arch arm64 -O2", "LIBPLIST_LIBS=$LIBPLIST/libplist-2.0.dylib",
            'odd="quote" $HOME`x`']
    line = gb.shell_line(step)
    out = subprocess.run(["/bin/sh", "-c", f'printf "%s\\n" {line[len("make "):]}'], capture_output=True, text=True,
                         env={"LIBPLIST": "/x/plist", "HOME": "/h", "PATH": "/usr/bin:/bin"}, check=True).stdout
    assert out.splitlines() == ["ldid", "CXXFLAGS=-arch arm64 -O2", "LIBPLIST_LIBS=/x/plist/libplist-2.0.dylib",
                                'odd="quote" /h`x`']


# ------------------------------------------------- review #4: ipatool, without Unicorn


IPATOOL_PATCH = REPO / "bundle" / "patches" / "ipatool-range-readahead.patch"


def test_ipatool_s_one_patch_is_its_range_reader():
    """2.1: get-version-metadata read an IPA's Info.plist with one range
    request per small read, hundreds per version, and failed on any one that
    stalled. The one patch changes only that reader and its tests; no
    library-path patch (option (a): ipatool fetches Unicorn as upstream does),
    and nothing on the full download's path or the restore image's checks."""
    assert gb.PATCHES["ipatool"] == ("ipatool-range-readahead.patch",)
    assert not (REPO / "bundle" / "patches" / "ipatool-unicorn-library-env.patch").exists()
    text = IPATOOL_PATCH.read_text()
    touched = re.findall(r"^\+\+\+ b/(\S+)", text, re.MULTILINE)
    assert touched == ["pkg/appstore/appstore_partial_zip.go", "pkg/appstore/appstore_get_version_metadata_test.go",
                       "pkg/appstore/appstore_partial_zip_readahead_test.go"]
    assert re.findall(r"^--- (\S+)", text, re.MULTILINE) == [
        "a/pkg/appstore/appstore_partial_zip.go", "a/pkg/appstore/appstore_get_version_metadata_test.go", "/dev/null"]


def test_the_ipatool_patch_applies_to_its_reader_as_the_build_applies_it(tmp_path):
    """PATCH_ARGV, from the source's top directory, to upstream's text as the
    patch expects it: the reader reads ahead, and a second run refuses."""
    top = tmp_path / "ipatool"
    reader = top / "pkg" / "appstore" / "appstore_partial_zip.go"
    test = top / "pkg" / "appstore" / "appstore_get_version_metadata_test.go"
    reader.parent.mkdir(parents=True)
    # Each file's hunks, as the upstream text they expect, padded to their
    # line numbers (`patch --fuzz=0` places a hunk by its context alone, but
    # never moves one past an earlier hunk's lines).
    for path, name in ((reader, "appstore_partial_zip.go"), (test, "appstore_get_version_metadata_test.go")):
        path.write_text(_upstream_of(IPATOOL_PATCH, f"pkg/appstore/{name}"))
    gb.apply_patches("ipatool", top)
    patched = reader.read_text()
    assert "rangeTailSize = 1 << 20" in patched and "r.mu.Lock()" in patched
    assert "ctx, cancel, watch := watchForStall()" in patched and "watch.received()" in patched
    assert (top / "pkg" / "appstore" / "appstore_partial_zip_readahead_test.go").is_file()
    with pytest.raises(subprocess.CalledProcessError):
        gb.apply_patches("ipatool", top)      # --forward: never applied twice


def _upstream_of(patch: Path, name: str) -> str:
    """The text a unified diff's hunks for file `name` expect (their context
    and `-` lines), each at the line its hunk header names, blank lines
    between."""
    out: list[str] = []
    current = None
    for line in patch.read_text().splitlines():
        if line.startswith("--- "):
            current = None
        elif line.startswith("+++ "):
            current = line[len("+++ b/"):]
        elif current == name and (m := re.match(r"@@ -(\d+)", line)):
            out += [""] * (int(m[1]) - 1 - len(out))
        elif current == name and line[:1] in (" ", "-"):
            out.append(line[1:])
    return "\n".join(out) + "\n"


@needs_tools("git")  # git ls-files
def test_every_patch_reaches_the_source_archive():
    """The source archive is `git archive` of the commit built, without
    SOURCE_EXCLUDES: each patch the build applies has to be a tracked file
    outside them, or SOURCES.md names a patch the archive lacks."""
    listed = subprocess.run(["git", "-C", str(REPO), "ls-files", "--", ".",
                             *(f":(exclude){p}" for p in gb.SOURCE_EXCLUDES)],
                            check=True, capture_output=True, text=True).stdout.splitlines()
    for name, patches in gb.PATCHES.items():
        for patch in patches:
            assert f"bundle/patches/{patch}" in listed, (name, patch)


def test_ipatool_is_patched_then_vendored_then_built_offline_and_its_key_covers_every_input(tmp_path, monkeypatch):
    events = []
    pin = gb.Pin(name="ipatool", url="https://example.invalid/i.tar.gz", sha256="a" * 64, license="MIT",
                 version="2.8.0")
    go_pin = gb.Pin(name="go", url="https://example.invalid/go.tar.gz", sha256="b" * 64, license="BSD-3-Clause",
                    version="1.26.5")

    def extract(archive, dest):
        (dest / "go" / "bin").mkdir(parents=True)
        for f in ("LICENSE", "PATENTS"):
            (dest / "go" / f).write_text(f"Go's {f}")
        return dest

    def unpack(pin, tmp):
        top = tmp / "src" / "ipatool-x"
        top.mkdir(parents=True)
        (top / "LICENSE").write_text("ipatool MIT")
        return top

    def run(argv, cwd=None, env=None, check=False, **kw):
        offline = argv[:3] == gb.GO_OFFLINE
        command = argv[3:] if offline else argv
        events.append(("run", command[:3], env["PATH"].split(":")[0].endswith("/go/bin"), env["GOMODCACHE"],
                       {k: env[k] for k in ("GOPATH", "GOCACHE", "HOME")}, Path(cwd), offline))
        if argv[:3] == ["go", "mod", "vendor"]:
            (Path(cwd) / "vendor" / "example.com" / "m").mkdir(parents=True)
            (Path(cwd) / "vendor" / "example.com" / "m" / "m.go").write_text("package m")
            (Path(cwd) / "vendor" / "modules.txt").write_text("# example.com/m v1.0.0\n")
            zipped = Path(env["GOMODCACHE"]) / "cache" / "download" / "example.com" / "m" / "@v" / "v1.0.0.zip"
            zipped.parent.mkdir(parents=True, exist_ok=True)
            zipped.write_bytes(b"a module zip")
        if argv[:2] == ["go", "build"]:
            assert "-trimpath" in argv and "-mod=vendor" in argv and env["CGO_ENABLED"] == "1"
            assert env["GOPROXY"] == gb.GOPROXY and env["GOFLAGS"] == "" and env["GOTOOLCHAIN"] == "local"
            (Path(cwd) / "ipatool").write_bytes(b"ipatool binary")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def module_licences(modcache, modules, dest):
        events.append(("licences", modules))
        (dest / "example.com/m@v1.0.0").mkdir(parents=True)
        (dest / "example.com/m@v1.0.0" / "LICENSE").write_text(MIT_TEXT)
        return {}

    patches = tmp_path / "patches"
    patches.mkdir()
    (patches / "example.patch").write_text("a patch\n")
    monkeypatch.setattr(gb, "PATCHES_DIR", patches)
    monkeypatch.setitem(gb.PATCHES, "ipatool", ("example.patch",))
    monkeypatch.setattr(gb, "fetch", lambda pin, cache=None: tmp_path / pin.filename)
    monkeypatch.setattr(gb, "extract", extract)
    monkeypatch.setattr(gb, "_unpack", unpack)
    monkeypatch.setattr(gb, "apply_patches", lambda name, src, p=None: events.append(("patch", name)))
    monkeypatch.setattr(gb.subprocess, "run", run)
    monkeypatch.setattr(gb, "check_no_shell", lambda path, name: events.append(("no shell", name)))
    monkeypatch.setattr(gb, "compiled_modules", lambda go, binary, env: [("example.com/m", "v1.0.0")])
    monkeypatch.setattr(gb, "module_licences", module_licences)
    # Its key's compiler, which would otherwise run clang through the stand-in
    # `run` when no test before this one has cached it.
    monkeypatch.setattr(gb, "toolchain_id", lambda: "clang 1; SDK 15.0")
    built = gb.build_ipatool(pin, go_pin, tmp_path / "work")
    assert [e[0] for e in events] == ["patch", "run", "run", "run", "run", "run", "no shell", "licences"]
    assert events[0] == ("patch", "ipatool")
    assert [e[1] for e in events if e[0] == "run"] == [["go", "mod", "vendor"], ["go", "mod", "verify"],
                                                      ["go", "build", "-mod=vendor"],
                                                      ["go", "generate", "-mod=vendor"], ["go", "test", "-mod=vendor"]]
    assert [e[6] for e in events if e[0] == "run"] == [False, False, False, True, True], \
        "the mocks and tests download nothing (GO_OFFLINE)"
    assert all(e[2] for e in events if e[0] == "run"), "the pinned Go first on PATH"
    for e in (e for e in events if e[0] == "run"):
        assert e[3] == str(gb.go_modcache()), "the module cache every build shares"
        assert all(Path(v).is_relative_to(e[5].parents[1]) for v in e[4].values()), "the rest is the build's own"
    assert gb.go_modules_cached(gb.go_modcache(), pin), "the modules it vendored, recorded"
    assert (built / "ipatool").read_bytes() == b"ipatool binary"
    assert (built / "go-LICENSE").read_text() == "Go's LICENSE" and (built / "LICENSE").read_text() == "ipatool MIT"
    with tarfile.open(built / "vendor.tar.gz") as tf:
        assert "vendor/modules.txt" in tf.getnames()
    first = built.name
    for changed in (dict(go_pin=gb.Pin(**{**go_pin.__dict__, "sha256": "c" * 64})),
                    dict(pin=gb.Pin(**{**pin.__dict__, "version": "2.8.1"}))):
        args = {"pin": pin, "go_pin": go_pin, **changed}
        assert gb.build_ipatool(args["pin"], args["go_pin"], tmp_path / "work").name != first, changed
    (patches / "example.patch").write_text("a patch\nchanged\n")
    assert gb.build_ipatool(pin, go_pin, tmp_path / "work").name != first, "a changed patch is a new build"


def test_go_version_m_lists_every_compiled_in_module_and_follows_replacements():
    out = ("/x/ipatool: go1.26.5\n\tpath\tgithub.com/majd/ipatool/v2\n\tmod\tgithub.com/majd/ipatool/v2\t(devel)\t\n"
           "\tdep\tgithub.com/a/b\tv1.0.0\th1:x=\n\tdep\tgolang.org/x/c\tv0.1.0\n\t=>\texample.com/fork/c\tv0.1.1\th1:y=\n"
           "\tbuild\t-trimpath=true\n")
    assert gb.go_deps(out) == [("github.com/a/b", "v1.0.0"), ("example.com/fork/c", "v0.1.1")]


def test_each_compiled_in_go_modules_licence_is_copied_and_a_module_without_one_is_refused(tmp_path):
    mit = "Permission is hereby granted, free of charge, to any person obtaining a copy of this software " * 2
    cache = tmp_path / "modcache"
    for root, files in {"github.com/!burnt!sushi/toml@v1.0.0": {"COPYING": mit, "go.mod": "module x"},
                        "github.com/v/mpb@v7.5.3": {"UNLICENSE": "This is free and unencumbered software released "
                                                    "into the public domain. " * 2,
                                                    # RC audit #6: a test, though its name starts COPYRIGHT.
                                                    "copyright_test.go": "package mpb // Copyright, license " * 5},
                        "example.com/pointer@v1.0.0": {"LICENSE": "see AUTHORS"}}.items():
        (cache / root).mkdir(parents=True)
        for name, body in files.items():
            (cache / root / name).write_text(body)
    dest = tmp_path / "out"
    gb.module_licences(cache, [("github.com/BurntSushi/toml", "v1.0.0"), ("github.com/v/mpb", "v7.5.3")], dest)
    assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()) == [
        "github.com/BurntSushi/toml@v1.0.0/COPYING", "github.com/v/mpb@v7.5.3/UNLICENSE"]
    for missing in (("example.com/pointer", "v1.0.0"), ("example.com/absent", "v1.0.0")):
        with pytest.raises(gb.BundleError, match=missing[0]):
            gb.module_licences(cache, [missing], tmp_path / f"out-{missing[0].rsplit('/', 1)[-1]}")


def test_the_vendored_modules_archive_is_the_same_bytes_every_time(tmp_path):
    src = tmp_path / "vendor"
    (src / "b").mkdir(parents=True)
    (src / "b" / "x.go").write_text("package b")
    (src / "modules.txt").write_text("# b v1\n")
    os.chmod(src / "b" / "x.go", 0o600)
    one, two = tmp_path / "1.tar.gz", tmp_path / "2.tar.gz"
    gb.deterministic_tar_gz(src, one, "vendor")
    os.utime(src / "modules.txt", (1, 1))
    gb.deterministic_tar_gz(src, two, "vendor")
    assert one.read_bytes() == two.read_bytes()
    with tarfile.open(one) as tf:
        members = tf.getmembers()
    assert [m.name for m in members] == ["vendor/b", "vendor/b/x.go", "vendor/modules.txt"]
    assert {(m.uid, m.gid, m.uname, m.gname, m.mtime) for m in members} == {(0, 0, "", "", 0)}
    assert {m.mode for m in members if m.isfile()} == {0o644}


def test_ipatool_is_staged_with_every_licence_and_no_unicorn(tmp_path, monkeypatch):
    built = tmp_path / "built"
    (built / "modules" / "gopkg.in/retry.v1@v1.0.3").mkdir(parents=True)
    (built / "modules" / "gopkg.in/retry.v1@v1.0.3" / "LICENSE").write_text("LGPLv3 with an exception")
    # RC audit #6: a build cached before is_licence_name kept go-sdk's test.
    (built / "modules" / "gopkg.in/retry.v1@v1.0.3" / "copyright_test.go").write_text("package retry")
    (built / "ipatool").write_bytes(mb.thin([]))
    for f, body in (("LICENSE", "ipatool MIT"), ("go-LICENSE", "Go BSD"), ("go-PATENTS", "Go patents")):
        (built / f).write_text(body)
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    monkeypatch.setattr(gb, "build_ipatool", lambda pin, go, work=None, arch="arm64": built)
    monkeypatch.setattr(gb, "fetch", lambda pin, cache=None: pytest.fail(f"fetched {pin.name}"))
    contents, licences = tmp_path / "Contents", tmp_path / "licences"
    licences.mkdir()
    gb.stage_ipatool(pins, contents, licences, tmp_path / "work")
    assert (contents / "Helpers/bin/ipatool").read_bytes() == mb.thin([])
    assert not [p for p in contents.rglob("*") if "unicorn" in p.name.lower()]
    got = sorted(p.relative_to(licences).as_posix() for p in licences.rglob("*") if p.is_file())
    assert got == sorted(["ipatool-LICENSE", "go-LICENSE", "go-PATENTS",
                          "ipatool-modules/gopkg.in/retry.v1@v1.0.3/LICENSE"])


def test_only_ipatool_is_signed_with_entitlements_allow_jit_and_disable_library_validation(tmp_path):
    import plistlib
    assert gb.ENTITLED == {"Helpers/bin/ipatool": "ipatool.plist"}
    assert plistlib.loads((gb.ENTITLEMENTS_DIR / "ipatool.plist").read_bytes()) == gb.IPATOOL_ENTITLEMENTS == {
        "com.apple.security.cs.allow-jit": True, "com.apple.security.cs.disable-library-validation": True}
    dest = tmp_path / "stage"
    (dest / "Helpers/bin").mkdir(parents=True)
    found = [dest / "Helpers/bin/ipatool", dest / "Helpers/bin/zsign"]
    gb.write_entitlements_list(dest, found)
    assert (dest / "entitlements.txt").read_text() == "Helpers/bin/ipatool\tipatool.plist\n"
    with pytest.raises(gb.BundleError, match="Helpers/bin/ipatool"):
        gb.write_entitlements_list(dest, found[1:])


@pytest.mark.parametrize("bad, error", [
    ('member = "lib/x.dylib"\n', "member and member_sha256"),
    (f'member_sha256 = "{"7" * 64}"\n', "member and member_sha256"),
    ('source = "nowhere"\n', "nowhere"),
])
def test_a_pin_taking_one_file_names_its_hash_and_a_source_that_is_pinned(tmp_path, bad, error):
    pins = tmp_path / "pins.toml"
    pins.write_text(f'[x]\nversion = "1"\nurl = "https://example.invalid/x.whl"\nsha256 = "{"a" * 64}"\n'
                    f'license = "MIT"\n{bad}')
    with pytest.raises(gb.BundleError, match=error):
        gb.load_pins(pins)


@slow
def test_ipatool_builds_from_its_pinned_source_with_the_pinned_go(tmp_path):
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    built = gb.build_ipatool(pins["ipatool"], pins["go"])
    exe = built / "ipatool"
    r = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=60)
    assert "version 2.8.0" in r.stdout + r.stderr
    assert _lipo_archs(exe) == ["arm64"]
    load = subprocess.run(["otool", "-l", str(exe)], capture_output=True, text=True, check=True).stdout
    assert re.search(r"cmd LC_BUILD_VERSION\n.*\n\s*platform 1\n\s*minos 15\.0\n", load), "macOS 15"
    data = exe.read_bytes()
    assert b"/Users/runner" not in data and b"graft-ipatool-" not in data and b"/private/var/folders" not in data
    assert not gb.imported_symbols(exe) & set(gb.SHELL_IMPORTS)
    # Every module compiled in has its license text; the copyleft two among them.
    assert (built / "modules" / "github.com/hashicorp/golang-lru/v2@v2.0.7" / "LICENSE").read_text().find(
        "Mozilla Public License") >= 0
    assert "LGPLv3" in (built / "modules" / "gopkg.in/retry.v1@v1.0.3" / "LICENSE").read_text()
    with tarfile.open(built / "vendor.tar.gz") as tf:
        names = tf.getnames()
    assert "vendor/modules.txt" in names and any(n.startswith("vendor/github.com/ebitengine/purego/") for n in names)


# ------------------- UnfairPlay, from its pinned source (owner, 2026-11-23: not upstream's prebuilt)

UNFAIRPLAY_COMMIT = "d7503082a59e3dcfdd3ea7a70434dcecaaaf07df"
#: The source tree's files the build reads or ships, as a GitHub archive holds them.
UNFAIRPLAY_TREE = {"go.mod": b"module github.com/DrWhax/unfairplay\n", "cmd/unfairplay/main.go": b"package main",
                   "LICENSE": b"MIT", "NOTICE": b"the notice", "LICENSE-DARKPHOENIX.txt": b"Apache-2.0",
                   "LICENSE-MACOSFAIRFREE.txt": b"MIT, macOSFairFree", "README.md": b"readme"}


def _source_tarball(path: Path, commit: str | None, files: dict[str, bytes]) -> Path:
    """A GitHub archive/<commit>.tar.gz: one top directory, and the commit in
    the pax global header's comment, where `git archive` puts it."""
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT,
                      pax_headers={"comment": commit} if commit else {}) as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(f"unfairplay-{commit or 'x'}/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


#: `go version -m` on upstream's v0.4.0 release binary, as the pinned Go reads it.
UPSTREAM_BUILDINFO = f"""unfairplay: go1.26.0
\tpath\tgithub.com/DrWhax/unfairplay/cmd/unfairplay
\tmod\tgithub.com/DrWhax/unfairplay\tv0.4.0\t
\tdep\tgithub.com/spf13/cobra\tv1.10.2\th1:DMTTonx5m65Ic0GOoRY2c16WCbHxOOw6xxezuLaBpcU=
\tdep\tgithub.com/spf13/pflag\tv1.0.9\th1:9exaQaMOCwffKiiiYk6/BndUBv+iRViNW+4lEMi0PvY=
\tdep\thowett.net/plist\tv1.0.1\th1:37GdZ8tP09Q35o9ych3ehygcsL+HqKSwzctveSlarvM=
\tbuild\t-buildmode=exe
\tbuild\t-compiler=gc
\tbuild\t-trimpath=true
\tbuild\tCGO_ENABLED=0
\tbuild\tGOARCH=arm64
\tbuild\tGOOS=darwin
\tbuild\tGOARM64=v8.0
\tbuild\tvcs=git
\tbuild\tvcs.revision={UNFAIRPLAY_COMMIT}
\tbuild\tvcs.time=2026-11-16T00:58:55Z
\tbuild\tvcs.modified=false
"""
#: The stage's build of the same source: the pinned Go, vendored modules (no sums).
OUR_BUILDINFO = (UPSTREAM_BUILDINFO.replace("go1.26.0", "go1.26.5")
                 .replace("\th1:DMTTonx5m65Ic0GOoRY2c16WCbHxOOw6xxezuLaBpcU=", "\t")
                 .replace("\th1:9exaQaMOCwffKiiiYk6/BndUBv+iRViNW+4lEMi0PvY=", "\t")
                 .replace("\th1:37GdZ8tP09Q35o9ych3ehygcsL+HqKSwzctveSlarvM=", "\t"))


def _unfairplay_pins(**changed) -> tuple:
    pin = gb.Pin(name="unfairplay", url=f"https://example.invalid/o/unfairplay/archive/{UNFAIRPLAY_COMMIT}.tar.gz",
                 sha256="a" * 64, license="MIT", version="0.4.0")
    go_pin = gb.Pin(name="go", url="https://example.invalid/go1.26.5.tar.gz", sha256="b" * 64,
                    license="BSD-3-Clause", version="1.26.5")
    return gb.Pin(**{**pin.__dict__, **changed}), go_pin


@pytest.mark.parametrize("header", [UNFAIRPLAY_COMMIT, "0" * 40, None])
def test_a_source_archive_must_be_the_commit_its_url_names(tmp_path, header):
    """The revision UnfairPlay is built from: GitHub's archive of a commit
    says which in its header, and it must be the commit the pin's URL names."""
    pin, _ = _unfairplay_pins()
    archive = _source_tarball(tmp_path / "s.tar.gz", header, UNFAIRPLAY_TREE)
    if header == UNFAIRPLAY_COMMIT:
        assert gb.check_archive_commit(pin, archive) == UNFAIRPLAY_COMMIT
    else:
        with pytest.raises(gb.BundleError, match=f"{header or 'no commit'}.*{UNFAIRPLAY_COMMIT}"):
            gb.check_archive_commit(pin, archive)
    tag, _ = _unfairplay_pins(url="https://example.invalid/o/unfairplay/archive/refs/tags/v0.4.0.tar.gz")
    with pytest.raises(gb.BundleError, match="names no commit"):
        gb.check_archive_commit(tag, archive)
    assert gb.source_commit(pin) == ("https://example.invalid/o/unfairplay.git", UNFAIRPLAY_COMMIT)
    real = gb.load_pins(REPO / "bundle" / "pins.toml")["unfairplay"]
    assert gb.source_commit(real) == ("https://github.com/DrWhax/unfairplay.git", UNFAIRPLAY_COMMIT)


def _fake_unfairplay_build(tmp_path: Path, monkeypatch, header: str | None = UNFAIRPLAY_COMMIT,
                           buildinfo: list | None = None) -> list:
    """build_unfairplay's tools faked, its source a real archive: the Go
    toolchain's extract, `git` and `go` themselves, the shell check, the
    binary's build info (`buildinfo[0]`, OUR_BUILDINFO unless the test changes
    it) and the license copy."""
    events: list = []
    buildinfo = buildinfo if buildinfo is not None else [OUR_BUILDINFO]
    source = _source_tarball(tmp_path / f"{UNFAIRPLAY_COMMIT}.tar.gz", header, UNFAIRPLAY_TREE)
    real_extract = gb.extract

    def extract(archive, dest):
        if archive.name.startswith("go"):
            (dest / "go" / "bin").mkdir(parents=True)
            return dest
        return real_extract(archive, dest)

    def run(argv, cwd=None, env=None, check=False, **kw):
        events.append(("run", list(argv), dict(env), Path(cwd)))
        if argv[:3] == ["go", "mod", "vendor"]:
            for module, version in gb.go_deps(OUR_BUILDINFO):
                (Path(cwd) / "vendor" / module).mkdir(parents=True)
                (Path(cwd) / "vendor" / module / "x.go").write_text("package x")
                with (Path(cwd) / "vendor" / "modules.txt").open("a") as f:
                    f.write(f"# {module} {version}\n{module}\n")
        if argv[:2] == ["go", "build"]:
            (Path(cwd) / "unfairplay").write_bytes(b"unfairplay binary")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def module_licences(modcache, modules, dest):
        events.append(("licences", modules, modcache))
        _notices(dest, [f"{m}@{v}" for m, v in modules])
        return {}

    monkeypatch.setattr(gb, "fetch", lambda pin, cache=None: source if pin.name == "unfairplay" else
                        tmp_path / "go1.26.5.tar.gz")
    monkeypatch.setattr(gb, "toolchain_id", lambda: "Apple clang version 99, macOS SDK 99.0")
    monkeypatch.setattr(gb, "extract", extract)
    monkeypatch.setattr(gb.subprocess, "run", run)
    monkeypatch.setattr(gb, "check_no_shell", lambda path, name: events.append(("no shell", name, path.name)))
    monkeypatch.setattr(gb, "go_version_m", lambda go, binary, env: events.append(("build info", binary.name))
                        or buildinfo[0])
    monkeypatch.setattr(gb, "module_licences", module_licences)
    return events


def test_unfairplay_is_vendored_then_built_offline_as_upstreams_release_is(tmp_path, monkeypatch):
    """Upstream's release workflow: `go build -trimpath -ldflags "-s -w -X
    main.version=${tag}" ./cmd/unfairplay`, CGO_ENABLED=0, in a depth-1
    checkout of the tag. Here the files are the pinned archive's; git fetches
    only the tag's commit, so Go stamps its revision (and says vcs.modified
    if the archive isn't that commit's tree). Built with the pinned Go, from
    vendor/ alone (kept out of git's status), Go told to use only that
    toolchain, the module proxy and the checksum database, and git told to
    read no configuration and ask for nothing."""
    events = _fake_unfairplay_build(tmp_path, monkeypatch)
    pin, go_pin = _unfairplay_pins()
    built = gb.build_unfairplay(pin, go_pin, tmp_path / "work")
    assert [e[0] for e in events] == ["run"] * 8 + ["build info", "no shell", "licences"]
    repo = "https://example.invalid/o/unfairplay.git"
    assert [e[1] for e in events if e[0] == "run"] == gb.unfairplay_steps("0.4.0", repo, UNFAIRPLAY_COMMIT) == [
        ["git", "init", "-q"],
        ["git", "-c", "credential.helper=", "fetch", "-q", "--depth=1", "--no-tags", repo,
         "+refs/tags/v0.4.0:refs/tags/v0.4.0"],
        ["git", "update-ref", "--no-deref", "HEAD", UNFAIRPLAY_COMMIT],
        ["git", "read-tree", "HEAD"],
        ["/bin/sh", "-c", "echo /vendor/ >> .git/info/exclude"],
        ["go", "mod", "vendor"],
        ["go", "mod", "verify"],
        ["go", "build", "-mod=vendor", "-trimpath", "-buildvcs=true", "-ldflags=-s -w -X main.version=v0.4.0",
         "-o", "unfairplay", "./cmd/unfairplay"]]
    for _, _, env, cwd in (e for e in events if e[0] == "run"):
        assert {k: env[k] for k in ("GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_TERMINAL_PROMPT")} == {
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"}
        assert cwd.name == f"unfairplay-{UNFAIRPLAY_COMMIT}", "in the archive's top directory"
        assert env["PATH"].split(":")[0].endswith("/go/bin") and env["PATH"].split(":")[1:] == [
            "/usr/bin", "/bin", "/usr/sbin", "/sbin"], "the pinned Go first, then only the system's"
        assert {k: env[k] for k in ("CGO_ENABLED", "GOOS", "GOARCH")} == {
            "CGO_ENABLED": "0", "GOOS": "darwin", "GOARCH": "arm64"}
        assert {k: env[k] for k in ("GOTOOLCHAIN", "GOFLAGS", "GOWORK", "GOENV", "GOPROXY", "GOSUMDB", "GONOSUMDB",
                                    "GOPRIVATE", "GOINSECURE")} == {
            "GOTOOLCHAIN": "local", "GOFLAGS": "", "GOWORK": "off", "GOENV": "off", "GOPROXY": gb.GOPROXY,
            "GOSUMDB": gb.GOSUMDB, "GONOSUMDB": "", "GOPRIVATE": "", "GOINSECURE": ""}
        assert "CC" not in env and "CGO_CFLAGS" not in env, "no cgo: nothing for a C compiler"
        for var in ("GOPATH", "GOCACHE", "HOME"):
            assert "/work/" not in env[var] and Path(env[var]).is_relative_to(cwd.parents[1]), var
        assert env["GOMODCACHE"] == str(gb.go_modcache()), "the module cache every build shares"
    assert events[8][1:] == ("unfairplay",) and events[9][1:] == ("unfairplay", "unfairplay")
    assert events[10][1] == gb.go_deps(OUR_BUILDINFO), "the modules compiled in, from the binary"
    assert events[10][2] == gb.go_modcache(), "their notices, from the shared module cache"
    assert (built / "unfairplay").read_bytes() == b"unfairplay binary"
    for f in ("LICENSE", "NOTICE", "LICENSE-DARKPHOENIX.txt", "LICENSE-MACOSFAIRFREE.txt"):
        assert (built / f).read_bytes() == UNFAIRPLAY_TREE[f], f
    assert gb.UNFAIRPLAY_LICENCES == ("LICENSE", "NOTICE", "LICENSE-DARKPHOENIX.txt", "LICENSE-MACOSFAIRFREE.txt")
    assert not (built / "README.md").exists()
    with tarfile.open(built / "vendor.tar.gz") as tf:
        assert "vendor/modules.txt" in tf.getnames()
    assert (built / "modules" / "github.com/spf13/cobra@v1.10.2").is_dir()


def test_unfairplays_build_key_covers_its_source_go_steps_and_settings(tmp_path, monkeypatch):
    buildinfo = [OUR_BUILDINFO]
    _fake_unfairplay_build(tmp_path, monkeypatch, buildinfo=buildinfo)
    pin, go_pin = _unfairplay_pins()
    first = gb.build_unfairplay(pin, go_pin, tmp_path / "work").name
    assert first.startswith("unfairplay-")
    assert gb.build_unfairplay(pin, go_pin, tmp_path / "work").name == first, "the same inputs, the cached build"
    for changed, info in ((dict(go_pin=gb.Pin(**{**go_pin.__dict__, "sha256": "c" * 64})), OUR_BUILDINFO),
                          (dict(pin=gb.Pin(**{**pin.__dict__, "sha256": "d" * 64})), OUR_BUILDINFO),
                          (dict(pin=gb.Pin(**{**pin.__dict__, "version": "0.4.1"})),
                           OUR_BUILDINFO.replace("\tv0.4.0\t", "\tv0.4.1\t"))):
        buildinfo[0] = info
        args = {"pin": pin, "go_pin": go_pin, **changed}
        assert gb.build_unfairplay(args["pin"], args["go_pin"], tmp_path / "work").name != first, changed
    buildinfo[0] = OUR_BUILDINFO
    monkeypatch.setitem(gb.UNFAIRPLAY_ENV, "GOPROXY", "https://proxy.example.invalid")
    assert gb.build_unfairplay(pin, go_pin, tmp_path / "work").name != first, "new settings, new build"


def test_an_unfairplay_archive_of_another_commit_is_refused_before_go_runs(tmp_path, monkeypatch):
    events = _fake_unfairplay_build(tmp_path, monkeypatch, header="0" * 40)
    pin, go_pin = _unfairplay_pins()
    with pytest.raises(gb.BundleError, match=UNFAIRPLAY_COMMIT):
        gb.build_unfairplay(pin, go_pin, tmp_path / "work")
    assert events == [] and not list((tmp_path / "work").glob("unfairplay-*"))


@pytest.mark.parametrize("info, said", [
    (OUR_BUILDINFO, None),
    (UPSTREAM_BUILDINFO, "go1.26.0, not go1.26.5"),
    (OUR_BUILDINFO.replace("vcs.modified=false", "vcs.modified=true"), "vcs.modified"),
    (OUR_BUILDINFO.replace(UNFAIRPLAY_COMMIT, "1" * 40), f"{'1' * 40}, not {UNFAIRPLAY_COMMIT}"),
    (re.sub(r"\tbuild\tvcs.*\n", "", OUR_BUILDINFO), "vcs.revision: not recorded"),
    (OUR_BUILDINFO.replace("\tv0.4.0\t", "\t(devel)\t"), "(devel), not github.com/DrWhax/unfairplay v0.4.0"),
    (OUR_BUILDINFO.replace("CGO_ENABLED=0", "CGO_ENABLED=1"), "CGO_ENABLED: 1, not 0"),
    (OUR_BUILDINFO.replace("\tbuild\t-trimpath=true\n", ""), "-trimpath: not recorded"),
    (OUR_BUILDINFO.replace("GOARCH=arm64", "GOARCH=amd64"), "GOARCH: amd64, not arm64"),
    (OUR_BUILDINFO.replace("/cmd/unfairplay", "/cmd/other"), "path: github.com/DrWhax/unfairplay/cmd/other"),
    (OUR_BUILDINFO.replace("\tmod\tgithub.com/DrWhax/unfairplay", "\tmod\texample.com/fork"), "example.com/fork"),
])
def test_unfairplays_build_info_must_be_the_pinned_commit_built_with_the_pinned_go(info, said):
    """What the stage and verify-bundle check in the binary itself: the
    pinned Go (not upstream's go1.26.0), the pinned module at its tag and
    commit, the archive unmodified, and upstream's settings."""
    pin, go_pin = _unfairplay_pins()
    problems = gb.unfairplay_build_problems(info, pin, go_pin)
    if said is None:
        assert problems == []
    else:
        assert said in "; ".join(problems), problems
        assert len(problems) == (3 if "vcs.revision: not recorded" in said else 1), problems


@pytest.mark.parametrize("info", [UPSTREAM_BUILDINFO, OUR_BUILDINFO.replace("vcs.modified=false", "vcs.modified=true")])
def test_a_built_unfairplay_whose_build_info_is_wrong_is_refused(tmp_path, monkeypatch, info):
    """vcs.modified=true: the pinned archive's files aren't the commit's tree."""
    events = _fake_unfairplay_build(tmp_path, monkeypatch, buildinfo=[info])
    pin, go_pin = _unfairplay_pins()
    with pytest.raises(gb.BundleError, match="build info"):
        gb.build_unfairplay(pin, go_pin, tmp_path / "work")
    assert "licences" not in [e[0] for e in events] and not list((tmp_path / "work").glob("unfairplay-*"))


def test_unfairplay_is_staged_from_its_build_with_every_licence(tmp_path, monkeypatch):
    built = tmp_path / "built"
    module = built / "modules" / "github.com/spf13/cobra@v1.10.2"
    module.mkdir(parents=True)
    (module / "LICENSE.txt").write_text("Apache License 2.0")
    (module / "cobra_test.go").write_text("package cobra")  # cached by a build before is_licence_name
    (built / "unfairplay").write_bytes(mb.thin([]))
    (built / "vendor.tar.gz").write_bytes(b"the vendored modules, for the source archive only")
    for f in gb.UNFAIRPLAY_LICENCES:
        (built / f).write_text(f"{f} text")
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    asked = []
    monkeypatch.setattr(gb, "build_unfairplay", lambda pin, go, work=None: asked.append((pin, go, work)) or built)
    monkeypatch.setattr(gb, "fetch", lambda pin, cache=None: pytest.fail(f"fetched {pin.name}"))
    contents, licences = tmp_path / "Contents", tmp_path / "licences"
    licences.mkdir()
    assert gb.stage_unfairplay(pins, contents, licences, tmp_path / "work") == built
    assert asked == [(pins["unfairplay"], pins["go"], tmp_path / "work")]
    staged = contents / "Helpers/bin/unfairplay"
    assert staged.read_bytes() == mb.thin([]) and staged.stat().st_mode & 0o777 == 0o755
    assert sorted(p.relative_to(contents).as_posix() for p in contents.rglob("*") if p.is_file()) == [
        "Helpers/bin/unfairplay"]
    got = sorted(p.relative_to(licences).as_posix() for p in licences.rglob("*") if p.is_file())
    assert got == ["unfairplay-LICENSE", "unfairplay-LICENSE-DARKPHOENIX.txt", "unfairplay-LICENSE-MACOSFAIRFREE.txt",
                   "unfairplay-NOTICE", "unfairplay-modules/github.com/spf13/cobra@v1.10.2/LICENSE.txt"]
    assert (licences / "unfairplay-NOTICE").read_text() == "NOTICE text"


def test_an_unfairplay_build_linking_a_library_the_bundle_lacks_is_refused(tmp_path, monkeypatch):
    built = tmp_path / "built"
    (built / "modules").mkdir(parents=True)
    (built / "unfairplay").write_bytes(mb.thin([mb.dylib_cmd("/opt/homebrew/lib/libx.dylib")]))
    for f in gb.UNFAIRPLAY_LICENCES:
        (built / f).write_text(f)
    monkeypatch.setattr(gb, "build_unfairplay", lambda pin, go, work=None: built)
    licences = tmp_path / "licences"
    licences.mkdir()
    with pytest.raises(gb.BundleError, match="libx"):
        gb.stage_unfairplay(gb.load_pins(REPO / "bundle" / "pins.toml"), tmp_path / "Contents", licences)


class _StagedUnfairPlay(Exception):
    """Raised by a fake stage_unfairplay: the stage got that far."""


def test_the_stage_puts_its_unfairplay_build_in_with_the_tool_licences(tmp_path, monkeypatch):
    """stage() takes UnfairPlay from stage_unfairplay, beside ipatool, into
    the folder the tools' licenses gather in; nothing else stages it."""
    calls = []
    dest, work = tmp_path / "stage", tmp_path / "work"
    monkeypatch.setattr(gb, "stage_python", lambda pins, contents, reports=None, arch="arm64":
                        contents / "Resources/GraftPython")
    monkeypatch.setattr(gb, "build_openssl", lambda pin, work=None, arch="arm64": tmp_path / "openssl")
    monkeypatch.setattr(gb, "fetch", lambda pin, cache=None: calls.append(("fetch", pin.name)) or tmp_path / "f")
    monkeypatch.setattr(gb, "extract", lambda archive, dest: dest)
    monkeypatch.setattr(gb, "_the_binary", lambda unpacked, tool: unpacked / tool)
    monkeypatch.setattr(gb, "stage_openssl_and_zsign", lambda prefix, zsign, contents: None)
    monkeypatch.setattr(gb, "stage_ipatool", lambda pins, contents, licences, work=None, arch="arm64":
                        calls.append(("ipatool", licences)))

    def stage_unfairplay(pins, contents, licences, work=None):
        calls.append(("unfairplay", contents, licences, work, sorted(p.name for p in licences.iterdir())))
        raise _StagedUnfairPlay

    monkeypatch.setattr(gb, "stage_unfairplay", stage_unfairplay)
    with pytest.raises(_StagedUnfairPlay):
        gb.stage(dest, work)
    licences = work / "tool-licences"
    assert calls == [("fetch", "zsign"), ("ipatool", licences),
                     ("unfairplay", dest, licences, work, ["zsign-LICENSE"])]


@slow
def test_unfairplay_builds_from_its_pinned_source_with_the_pinned_go(tmp_path):
    """The real build, read with the pinned Go's `go version -m`; nothing here
    runs UnfairPlay."""
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    built = gb.build_unfairplay(pins["unfairplay"], pins["go"])
    exe = built / "unfairplay"
    go = gb.extract(gb.fetch(pins["go"]), tmp_path / "go") / "go"
    info = subprocess.run([str(go / "bin" / "go"), "version", "-m", str(exe)], capture_output=True, text=True,
                          check=True, env=gb.go_env(go, tmp_path, gb.UNFAIRPLAY_ENV)).stdout
    assert info.splitlines()[0].endswith(f": go{pins['go'].version}")
    assert gb.unfairplay_build_problems(info, pins["unfairplay"], pins["go"]) == []
    assert f"\tbuild\tvcs.revision={UNFAIRPLAY_COMMIT}\n" in info and "\tbuild\tvcs.modified=false\n" in info
    assert gb.go_deps(info) == [("github.com/spf13/cobra", "v1.10.2"), ("github.com/spf13/pflag", "v1.0.9"),
                                ("howett.net/plist", "v1.0.1")]
    assert _lipo_archs(exe) == ["arm64"]
    data = exe.read_bytes()
    assert b"v0.4.0" in data, "upstream's -X main.version"
    # The release machine's checkout, home and name (/Users/Shared/SC Info is UnfairPlay's own path).
    for needle in (*gb._build_paths(tmp_path), "graft-unfairplay-", "/private/var/folders"):
        assert needle.encode() not in data, needle
    assert not gb.imported_symbols(exe) & set(gb.SHELL_IMPORTS)
    assert "Apache License" in (built / "modules/github.com/spf13/cobra@v1.10.2/LICENSE.txt").read_text()
    assert (built / "modules/github.com/spf13/pflag@v1.0.9/LICENSE").is_file()
    assert (built / "modules/howett.net/plist@v1.0.1/LICENSE").is_file()
    assert "DarkPhoenix" in (built / "NOTICE").read_text()
    with tarfile.open(built / "vendor.tar.gz") as tf:
        names = tf.getnames()
    assert "vendor/modules.txt" in names and any(n.startswith("vendor/howett.net/plist/") for n in names)


# --- Every Go module a binary holds, by its build info: its notice and its source (release reviewer) ---

MIT_TEXT = "Permission is hereby granted, free of charge, to any person obtaining a copy of this software " * 2


def _vendor_tar(path: Path, modules_txt: str, files: list[str]) -> Path:
    vendor = path.parent / f"{path.name}-tree"
    (vendor / "modules.txt").parent.mkdir(parents=True)
    (vendor / "modules.txt").write_text(modules_txt)
    for f in files:
        (vendor / f).parent.mkdir(parents=True, exist_ok=True)
        (vendor / f).write_text("package x")
    gb.deterministic_tar_gz(vendor, path, "vendor")
    return path


def _notices(root: Path, modules: list[str], text: str = MIT_TEXT) -> Path:
    for m in modules:
        (root / m).mkdir(parents=True)
        (root / m / "LICENSE").write_text(text)
    return root


MODULES_TXT = ("# github.com/spf13/cobra v1.10.2\n## explicit; go 1.15\ngithub.com/spf13/cobra\n"
               "# github.com/inconshreveable/mousetrap v1.1.0\n## explicit; go 1.18\n"
               "# golang.org/x/c v0.1.0 => example.com/fork/c v0.1.1\ngolang.org/x/c\n")
COMPILED = [("github.com/spf13/cobra", "v1.10.2"), ("example.com/fork/c", "v0.1.1")]


def test_each_go_module_a_binary_holds_needs_its_notice_and_its_vendored_source(tmp_path):
    """From the binary's own build info, not from a list: a module compiled
    in must have a license text in the notices and its source in the
    vendored tree the source archive carries. A replaced module is the
    replacement, vendored under the original's path."""
    vendor = _vendor_tar(tmp_path / "vendor.tar.gz", MODULES_TXT,
                         ["github.com/spf13/cobra/cobra.go", "golang.org/x/c/c.go"])
    notices = _notices(tmp_path / "modules", ["github.com/spf13/cobra@v1.10.2", "example.com/fork/c@v0.1.1"])
    assert gb.go_module_problems(COMPILED, vendor, notices) == []
    # A module the binary holds that the vendored tree lacks (mousetrap is
    # listed in modules.txt for Windows, with no source vendored).
    held = [*COMPILED, ("github.com/inconshreveable/mousetrap", "v1.1.0")]
    _notices(notices, ["github.com/inconshreveable/mousetrap@v1.1.0"])
    assert gb.go_module_problems(held, vendor, notices) == [
        "github.com/inconshreveable/mousetrap v1.1.0: no source in vendor.tar.gz"]
    assert gb.go_module_problems([("example.com/other", "v1.0.0")], vendor, notices) == [
        "example.com/other v1.0.0: no source in vendor.tar.gz", "example.com/other v1.0.0: no license notice in modules/"]
    assert gb.go_module_problems([("github.com/spf13/cobra", "v1.10.3")], vendor, notices) == [
        "github.com/spf13/cobra v1.10.3: no source in vendor.tar.gz",
        "github.com/spf13/cobra v1.10.3: no license notice in modules/"], "the version compiled in, exactly"


@pytest.mark.parametrize("damage", ["removed", "pointer only", "not a license file"])
def test_a_go_module_whose_notice_is_missing_or_empty_fails(tmp_path, damage):
    vendor = _vendor_tar(tmp_path / "vendor.tar.gz", MODULES_TXT,
                         ["github.com/spf13/cobra/cobra.go", "golang.org/x/c/c.go"])
    notices = _notices(tmp_path / "modules", ["github.com/spf13/cobra@v1.10.2", "example.com/fork/c@v0.1.1"])
    folder = notices / "example.com/fork/c@v0.1.1"
    if damage == "removed":
        shutil.rmtree(folder)
    elif damage == "pointer only":
        (folder / "LICENSE").write_text("see AUTHORS")
    else:
        (folder / "LICENSE").rename(folder / "c_test.go")
    assert gb.go_module_problems(COMPILED, vendor, notices) == [
        "example.com/fork/c v0.1.1: no license notice in modules/"]


def test_a_vendored_tree_is_archived_as_go_spells_its_paths(tmp_path):
    """Found by the module check on ipatool's real build: on a Mac's
    case-insensitive disk, `go mod vendor` puts github.com/1password/
    onepassword-sdk-go in the folder github.com/1Password/ that
    connect-sdk-go made first. The archive must spell each path as Go does,
    or a rebuild from it on a case-sensitive disk can't find the module."""
    vendor = tmp_path / "vendor"
    files = {"github.com/1Password/connect-sdk-go/connect/client.go": "package connect",
             "github.com/1Password/connect-sdk-go/LICENSE": "MIT",
             "github.com/1Password/onepassword-sdk-go/client.go": "package onepassword",
             "github.com/1Password/onepassword-sdk-go/internal/core.go": "package internal",
             "golang.org/x/c/c.go": "package c",
             # A module inside another's folder, its path cased otherwise: the longer path wins.
             "example.com/x/x.go": "package x", "example.com/x/y/y.go": "package y"}
    for rel, body in files.items():
        (vendor / rel).parent.mkdir(parents=True, exist_ok=True)
        (vendor / rel).write_text(body)
    (vendor / "modules.txt").write_text(
        "# github.com/1Password/connect-sdk-go v1.5.4\n## explicit\ngithub.com/1Password/connect-sdk-go/connect\n"
        "# github.com/1password/onepassword-sdk-go v0.4.1-beta.1\n## explicit; go 1.24.0\n"
        "github.com/1password/onepassword-sdk-go\ngithub.com/1password/onepassword-sdk-go/internal\n"
        "# golang.org/x/c v0.1.0 => example.com/fork/c v0.1.1\ngolang.org/x/c\n"
        "# example.com/x v1.0.0\nexample.com/x\n# example.com/X/y v1.0.0\nexample.com/X/y\n")
    out = tmp_path / "vendor.tar.gz"
    gb.vendor_tar_gz(vendor, out)
    with tarfile.open(out) as tf:
        names = sorted(m.name for m in tf.getmembers() if m.isfile())
    assert names == ["vendor/example.com/X/y/y.go", "vendor/example.com/x/x.go",
                     "vendor/github.com/1Password/connect-sdk-go/LICENSE",
                     "vendor/github.com/1Password/connect-sdk-go/connect/client.go",
                     "vendor/github.com/1password/onepassword-sdk-go/client.go",
                     "vendor/github.com/1password/onepassword-sdk-go/internal/core.go",
                     "vendor/golang.org/x/c/c.go", "vendor/modules.txt"]
    assert gb.vendored_modules(out) == {("github.com/1Password/connect-sdk-go", "v1.5.4"),
                                        ("github.com/1password/onepassword-sdk-go", "v0.4.1-beta.1"),
                                        ("golang.org/x/c", "v0.1.0"), ("example.com/fork/c", "v0.1.1"),
                                        ("example.com/x", "v1.0.0"), ("example.com/X/y", "v1.0.0")}
    two = tmp_path / "two.tar.gz"
    gb.vendor_tar_gz(vendor, two)
    assert two.read_bytes() == out.read_bytes(), "the same bytes every time"


def test_an_archive_whose_renames_give_two_members_one_name_is_refused(tmp_path):
    (tmp_path / "src" / "a").mkdir(parents=True)
    (tmp_path / "src" / "a" / "x.go").write_text("x")
    (tmp_path / "src" / "a" / "y.go").write_text("y")
    with pytest.raises(gb.BundleError, match="one name"):
        gb.deterministic_tar_gz(tmp_path / "src", tmp_path / "o.tar.gz", "vendor", lambda rel: rel.replace("y", "x"))


def test_a_vendored_tree_naming_one_path_two_ways_is_refused(tmp_path):
    """Two paths that differ only in case can't both be on this disk, so one
    of them isn't what Go vendored."""
    vendor = tmp_path / "vendor"
    (vendor / "example.com/A").mkdir(parents=True)
    (vendor / "example.com/A/a.go").write_text("package a")
    (vendor / "modules.txt").write_text("# example.com/A v1.0.0\nexample.com/A\n# example.com/a v1.0.0\nexample.com/a\n")
    with pytest.raises(gb.BundleError, match=r"example\.com/A and example\.com/a"):
        gb.vendor_tar_gz(vendor, tmp_path / "v.tar.gz")


def _fake_go_tool_build(tmp_path: Path, monkeypatch, vendored: list[str]) -> None:
    """build_unfairplay with every tool faked, `go mod vendor` vendoring only
    `vendored`, and build info naming cobra and pflag."""
    _fake_unfairplay_build(tmp_path, monkeypatch, buildinfo=[OUR_BUILDINFO.replace(
        "\tdep\thowett.net/plist\tv1.0.1\t\n", "")])
    real_run = gb.subprocess.run

    def run(argv, cwd=None, env=None, check=False, **kw):
        r = real_run(argv, cwd=cwd, env=env, check=check, **kw)
        if argv[:3] == ["go", "mod", "vendor"]:
            shutil.rmtree(Path(cwd) / "vendor")
            (Path(cwd) / "vendor").mkdir()
            (Path(cwd) / "vendor" / "modules.txt").write_text(
                "# github.com/spf13/cobra v1.10.2\ngithub.com/spf13/cobra\n"
                "# github.com/spf13/pflag v1.0.9\ngithub.com/spf13/pflag\n")
            for module in vendored:
                (Path(cwd) / "vendor" / module).mkdir(parents=True, exist_ok=True)
                (Path(cwd) / "vendor" / module / "x.go").write_text("package x")
        return r

    def module_licences(modcache, modules, dest):
        _notices(dest, [f"{m}@{v}" for m, v in modules])
        return {}

    monkeypatch.setattr(gb.subprocess, "run", run)
    monkeypatch.setattr(gb, "module_licences", module_licences)


def test_the_stage_refuses_a_go_build_whose_modules_are_not_all_vendored(tmp_path, monkeypatch):
    _fake_go_tool_build(tmp_path, monkeypatch, ["github.com/spf13/cobra"])
    pin, go_pin = _unfairplay_pins()
    with pytest.raises(gb.BundleError, match=r"github\.com/spf13/pflag v1\.0\.9: no source in vendor\.tar\.gz"):
        gb.build_unfairplay(pin, go_pin, tmp_path / "work")
    assert not list((tmp_path / "work").glob("unfairplay-*"))


def test_the_stage_takes_a_go_build_whose_modules_are_all_vendored_and_noticed(tmp_path, monkeypatch):
    _fake_go_tool_build(tmp_path, monkeypatch, ["github.com/spf13/cobra", "github.com/spf13/pflag"])
    pin, go_pin = _unfairplay_pins()
    built = gb.build_unfairplay(pin, go_pin, tmp_path / "work")
    assert (built / "modules" / "github.com/spf13/pflag@v1.0.9" / "LICENSE").is_file()


def test_ipatools_build_checks_its_modules_against_its_vendored_source_too(tmp_path, monkeypatch):
    pin = gb.Pin(name="ipatool", url="https://example.invalid/i.tar.gz", sha256="a" * 64, license="MIT",
                 version="2.8.0")
    go_pin = gb.Pin(name="go", url="https://example.invalid/go.tar.gz", sha256="b" * 64, license="BSD-3-Clause",
                    version="1.26.5")

    def extract(archive, dest):
        (dest / "go" / "bin").mkdir(parents=True)
        for f in ("LICENSE", "PATENTS"):
            (dest / "go" / f).write_text(f)
        return dest

    def unpack(pin, tmp):
        top = tmp / "src" / "ipatool-x"
        top.mkdir(parents=True)
        (top / "LICENSE").write_text("ipatool MIT")
        return top

    def run(argv, cwd=None, env=None, check=False, **kw):
        if argv[:3] == ["go", "mod", "vendor"]:
            (Path(cwd) / "vendor" / "example.com" / "m").mkdir(parents=True)
            (Path(cwd) / "vendor" / "modules.txt").write_text("# example.com/m v1.0.0\nexample.com/m\n")
        if argv[:2] == ["go", "build"]:
            (Path(cwd) / "ipatool").write_bytes(b"ipatool binary")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(gb, "fetch", lambda pin, cache=None: tmp_path / pin.filename)
    monkeypatch.setattr(gb, "extract", extract)
    monkeypatch.setattr(gb, "_unpack", unpack)
    monkeypatch.setattr(gb, "toolchain_id", lambda: "Apple clang version 99, macOS SDK 99.0")
    monkeypatch.setattr(gb.subprocess, "run", run)
    monkeypatch.setattr(gb, "check_no_shell", lambda path, name: None)
    monkeypatch.setattr(gb, "compiled_modules", lambda go, binary, env: [("example.com/m", "v1.0.0"),
                                                                         ("example.com/n", "v2.0.0")])
    monkeypatch.setattr(gb, "module_licences", lambda modcache, modules, dest: _notices(
        dest, [f"{m}@{v}" for m, v in modules]))
    with pytest.raises(gb.BundleError, match=r"ipatool.*example\.com/n v2\.0\.0: no source in vendor\.tar\.gz"):
        gb.build_ipatool(pin, go_pin, tmp_path / "work")


def test_frida_is_neither_a_dependency_nor_bundled():
    """Review #3: frida's native module statically links LGPL GLib and
    friends, whose source the download would have to carry. Graft asks frida
    through frida-ios-dump's own virtualenv instead (graft_ipa/frida_probe.py)."""
    deps = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["dependencies"]
    assert not [d for d in deps if re.match(r"frida\b", d, re.I)], deps
    assert "frida" not in gb.lock_names(gb.RUNTIME_LOCK) and "frida" not in gb.lock_names(gb.BUILD_LOCK)
    assert "frida" not in gb.IMPORT_CHECK


def test_ios_code_is_neither_signed_nor_thinned_by_the_release(tmp_path, monkeypatch):
    """Review #6/#34: cyan's iOS frameworks were signed with the release
    Developer ID and given _CodeSignature/, and cyan copies them into users'
    IPAs as they are. They now stay byte for byte what cyan's archive carries."""
    contents = tmp_path / "Contents"
    ios = contents / CYAN_EXTRAS / "Orion.framework" / "Orion"
    mac = contents / "Helpers" / "bin" / "zsign"
    for path, cmds in ((ios, [mb.build_version_cmd(mb.PLATFORM_IOS, "14.0")]),
                       (mac, [mb.build_version_cmd(mb.PLATFORM_MACOS, "15.0")])):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(mb.thin(cmds))
    assert gb.signed_machos(contents) == [mac]
    assert set(gb.macho_files(contents)) == {ios, mac}, "still seen, so cyan_frameworks can audit it"
    ios.write_bytes(mb.fat([(mb.CPU_ARM64, mb.thin([mb.build_version_cmd(mb.PLATFORM_IOS, "14.0")])),
                            (mb.CPU_X86_64, mb.thin([mb.build_version_cmd(mb.PLATFORM_IOS, "14.0")]))]))
    before = ios.read_bytes()
    monkeypatch.setattr(gb, "_archs", lambda p: ["arm64", "x86_64"] if p == ios else ["arm64"])
    monkeypatch.setattr(gb, "_run", lambda *a, **k: pytest.fail(f"thinned {a}"))
    assert gb.thin_to_arm64(contents) == [] and ios.read_bytes() == before


def test_a_locked_install_that_strays_from_its_plan_stops_the_stage(tmp_path, monkeypatch):
    def fake_run(argv, **kw):
        argv = [str(a) for a in argv]
        if "--report" in argv:
            sha = "a" * 64 if "--dry-run" in argv else "f" * 64  # the offline install found another file
            Path(argv[argv.index("--report") + 1]).write_text(json.dumps({"install": [
                {"metadata": {"name": "x", "version": "1"},
                 "download_info": {"archive_info": {"hashes": {"sha256": sha}}}}]}))
    monkeypatch.setattr(gb.subprocess, "run", fake_run)
    # RC audit #10: it said "the macOS 15 plan"; the plan is for MACOS_MIN.
    with pytest.raises(gb.BundleError, match=r"the plan for macOS 14\.0 \(macosx_14_0_arm64\) didn't choose"):
        gb.install_locked(tmp_path / "python3.13", gb.RUNTIME_LOCK, tmp_path / "plan.json", tmp_path / "s", {})


@pytest.mark.parametrize("version, machine, implementation, ok", [
    ((3, 13), "arm64", "cpython", True), ((3, 14), "arm64", "cpython", False),
    ((3, 13), "x86_64", "cpython", False), ((3, 13), "arm64", "pypy", False)])
def test_locks_and_stage_run_only_under_the_bundled_python(version, machine, implementation, ok):
    if ok:
        gb.check_interpreter(version, machine, implementation)
    else:
        with pytest.raises(gb.BundleError, match=r"CPython 3\.13 on arm64"):
            gb.check_interpreter(version, machine, implementation)


def test_lock_bundle_and_graft_release_check_the_interpreter_first():
    lock = (REPO / "scripts" / "lock-bundle").read_text()
    assert lock.index("graft_bundle.py check-python") < lock.index(".venv/bin/pip-compile")
    release = (REPO / "scripts" / "graft-release").read_text()
    assert release.index('graft_bundle.py" check-python') < release.index("\nxcodebuild ")


def test_graft_release_builds_the_commit_it_recorded_from_an_export_of_it():
    """Review #9, #11, #26, #45: HEAD is recorded and the tree checked clean,
    untracked files included, before anything reads it; the app is built and
    staged from a `git archive` export; that commit is archived as the source;
    HEAD must not have moved by the end. #35: nothing of an earlier run's
    may sit in the output folder."""
    s = (REPO / "scripts" / "graft-release").read_text()
    record = s.index('HEAD_AT_START="$(git -C "$REPO_ROOT" rev-parse HEAD)"')
    clean = s.index('git -C "$REPO_ROOT" status --porcelain)')
    export = s.index('git -C "$REPO_ROOT" archive --format=tar "$HEAD_AT_START" | tar -x -C "$EXPORT"')
    assert record < clean < export < s.index("\nxcodebuild ")
    assert "--untracked-files=no" not in s
    assert '-project "$EXPORT/Graft/Graft.xcodeproj"' in s
    assert '"$PY" "$EXPORT/scripts/graft_bundle.py" stage' in s and '"$PY" "$EXPORT/scripts/verify-bundle"' in s
    assert '--commit "$HEAD_AT_START" --repo "$REPO_ROOT"' in s
    assert s.index('"$(git -C "$REPO_ROOT" rev-parse HEAD)" != "$HEAD_AT_START"') > s.index("→ the source archive")
    for old in ('"$OUT_DIR/Graft.app"', '"$OUT_DIR/$ZIP_NAME"', '"$OUT_DIR/components.json"',
                '"$OUT_DIR"/Graft-*-sources.tar.gz'):
        assert old in s.split("check-python", 1)[0], old
    assert "ZIP_NAME=Graft.zip" in s and "ZIP_NAME=Graft-Intel.zip" in s
    assert "ditto -c -k --keepParent Graft.app Graft.zip" in s and "verify-bundle --zip" in s  # review #41


def test_playcover_runs_the_bundles_own_install_name_tool():
    from graft_ipa import playcover
    assert playcover.BUNDLED_INSTALL_NAME_TOOL == gb.CYAN_TOOLS / "install_name_tool"


def test_the_playcover_stub_is_built_at_release_and_carried_beside_its_builder(tmp_path, monkeypatch):
    """Decision 9: Graft.app ships the Preferences stub prebuilt (iOS code, so
    never re-signed), where playcover.build_stub looks for it first."""
    from graft_ipa import playcover
    built = tmp_path / "built" / "Preferences.framework"
    built.mkdir(parents=True)
    (built / "Preferences").write_bytes(mb.thin([mb.build_version_cmd(mb.PLATFORM_IOS, "11.0")], filetype=mb.MH_DYLIB))
    (built / "Info.plist").write_text("<plist/>")
    monkeypatch.setattr(gb, "build_preferences_stub", lambda work=None: built)
    contents = tmp_path / "Contents"
    staged = gb.stage_preferences_stub(contents, tmp_path / "work")
    assert staged == contents / gb.STUB_IN_APP
    site = contents / "Resources/GraftPython/lib/python3.13/site-packages"
    assert staged.parent.relative_to(site) == playcover.STUB_TOOLS_DIR.relative_to(Path(playcover.__file__).parents[1])
    assert (staged / "Preferences").read_bytes() == (built / "Preferences").read_bytes()
    assert gb.signed_machos(contents) == [], "iOS code: not signed by the release"


def test_the_stub_build_and_its_key_follow_its_source(tmp_path, monkeypatch):
    ran = []

    def build_steps(steps, cwd, arch="arm64"):
        ran.append((steps, sorted(p.name for p in cwd.iterdir())))
        (cwd / "Preferences.framework" / "Preferences").write_bytes(b"built")

    monkeypatch.setattr(gb, "_build_steps", build_steps)
    first = gb.build_preferences_stub(tmp_path / "work")
    assert (first / "Preferences").read_bytes() == b"built" and (first / "Info.plist").is_file()
    assert ran[0][0] == gb.stub_steps() and {"stub.m", "Info.plist"} <= set(ran[0][1])
    assert gb.stub_steps()[0][:4] == ["xcrun", "-sdk", "iphoneos", "clang"]
    assert "codesign" not in json.dumps(gb.stub_steps()), "PlayCover signs it on install"
    stub = tmp_path / "stub"
    shutil.copytree(gb.STUB_SOURCE, stub)
    (stub / "stub.m").write_text((stub / "stub.m").read_text() + "\n// changed\n")
    monkeypatch.setattr(gb, "STUB_SOURCE", stub)
    assert gb.build_preferences_stub(tmp_path / "work") != first, "a changed source is a new build"


@slow
@needs_toolchain
def test_the_playcover_stub_builds_for_ios_with_the_release_macs_xcode(tmp_path):
    framework = gb.build_preferences_stub(tmp_path / "work")
    assert macho.build_versions(framework / "Preferences") == [(macho.PLATFORM_IOS, (11, 0, 0))]
    assert "@rpath/Preferences.framework/Preferences" in subprocess.run(
        ["otool", "-D", str(framework / "Preferences")], capture_output=True, text=True, check=True).stdout



def test_an_rpath_into_another_machines_build_folder_is_deleted(tmp_path, monkeypatch):
    """Pillow's libjpeg carries LC_RPATH /Users/runner/work/Pillow/... (the
    critic: a notarization candidate, and a path to nowhere on a user's Mac)."""
    contents = tmp_path / "Contents"
    lib = contents / "Resources/GraftPython/lib/python3.13/site-packages/PIL/.dylibs/libjpeg.62.4.0.dylib"
    lib.parent.mkdir(parents=True)
    lib.write_bytes(mb.thin([mb.rpath_cmd("/Users/runner/work/Pillow/Pillow/build/deps/darwin/lib"),
                             mb.rpath_cmd("@loader_path"), mb.rpath_cmd("/usr/lib/swift")], filetype=mb.MH_DYLIB))
    ran = []
    monkeypatch.setattr(gb, "_run", lambda *a, **k: ran.append([str(x) for x in a]))
    assert gb.prune_foreign_rpaths(contents) == [(lib, "/Users/runner/work/Pillow/Pillow/build/deps/darwin/lib")]
    assert ran == [[gb.INSTALL_NAME_TOOL, "-delete_rpath", "/Users/runner/work/Pillow/Pillow/build/deps/darwin/lib",
                    str(lib)]]


def test_the_stage_names_the_release_machine_by_pwd_not_home(monkeypatch, tmp_path):
    import pwd
    me = pwd.getpwuid(os.getuid())
    monkeypatch.setenv("HOME", "/tmp/elsewhere")
    paths = gb._build_paths(tmp_path)
    assert me.pw_dir in paths and "/tmp/elsewhere" not in paths and str(tmp_path) in paths


# ------------------------------------------------- licenses for code linked in (#5, #8, #53)

MIT_TEXT = ("Permission is hereby granted, free of charge, to any person obtaining a copy of this software and "
            "associated documentation files, to deal in the Software without restriction.\n")


def test_a_source_file_named_like_a_notice_is_not_a_licence_file():
    for name in ("LICENSE", "LICENSE.md", "LICENSE-APACHE", "COPYING.LESSER", "UNLICENSE", "COPYRIGHT", "NOTICE.txt"):
        assert gb.is_licence_name(name), name
    for name in ("copyright_test.go", "license.rs", "notice.py", "README.md", "lib.rs"):
        assert not gb.is_licence_name(name), name


def test_the_index_names_every_package_and_tool_with_rust_crates(tmp_path):
    """RC audit #1: INDEX said rust-crates/ held pydantic_core's, qh3's and
    cryptography's, a fixed list; bcrypt's and ipatool's are there now."""
    extra = tmp_path / "tool-licences"
    for owner in ("bcrypt", "cryptography", "ipatool"):
        (extra / "rust-crates" / owner / "x@1.0.0").mkdir(parents=True)
        (extra / "rust-crates" / owner / "x@1.0.0" / "LICENSE").write_text(MIT_TEXT)
    contents = tmp_path / "Contents"
    (contents / "Resources/GraftPython/lib/python3.13/site-packages").mkdir(parents=True)
    lic = gb.collect_licenses(contents, gb.load_pins(REPO / "bundle" / "pins.toml"), extra)
    index = (lic / "INDEX.txt").read_text()
    assert ("  rust-crates/ — each Rust crate compiled into bcrypt, cryptography and ipatool, by package or tool "
            "and crate (ipatool's are the crates.io crates its embedded 1Password SDK core shows it holds, by "
            "source path, function name or wasm-bindgen record: 1Password publishes no Cargo.lock for it, so a "
            "crate that left none of these isn't seen, and a version the core doesn't carry is inferred, as "
            "bundle/ipatool-core-crates.lock in the source archive says)") in index
    assert "rust-crates/ipatool/ for those ipatool's embedded 1Password SDK core shows it holds." in index


def test_no_licence_text_says_the_pinned_crates_are_all_the_core_is_built_from(tmp_path):
    """Delta audit #2: the notices, INDEX.txt and SOURCES.md said the pinned
    crates were what ipatool's core is built from; they are what it shows."""
    (tmp_path / "rust-crates" / "ipatool").mkdir(parents=True)
    texts = {"THIRD_PARTY_NOTICES.md": (REPO / "THIRD_PARTY_NOTICES.md").read_text(),
             "SOURCES.md": gb._recipes("2.8.0")["ipatool"][3],
             "INDEX.txt": gb._folder_note("rust-crates", tmp_path / "rust-crates")}
    for where, text in texts.items():
        assert not re.search(r"crates[^.]*\bbuilt from", text), where
    assert "shows it holds" in texts["SOURCES.md"] and "can't be seen" in texts["SOURCES.md"]
    notices = " ".join(texts["THIRD_PARTY_NOTICES.md"].split())
    assert "1Password publishes no `Cargo.lock` for it, so the list is read out of the binary" in notices
    assert "a crate that left none of those traces can't be seen" in notices


def test_licence_folders_reach_licenses_too(tmp_path):
    """try5 shipped no ipatool-modules/: collect_licenses copied files only."""
    extra = tmp_path / "tool-licences"
    (extra / "ipatool-modules" / "gopkg.in/retry.v1@v1.0.3").mkdir(parents=True)
    (extra / "ipatool-modules" / "gopkg.in/retry.v1@v1.0.3" / "LICENSE").write_text("LGPLv3 " + MIT_TEXT)
    (extra / "zsign-LICENSE").write_text(MIT_TEXT)
    contents = tmp_path / "Contents"
    (contents / "Resources/GraftPython/lib/python3.13/site-packages").mkdir(parents=True)
    lic = gb.collect_licenses(contents, gb.load_pins(REPO / "bundle" / "pins.toml"), extra)
    assert (lic / "ipatool-modules/gopkg.in/retry.v1@v1.0.3/LICENSE").is_file()
    assert "  ipatool-modules/ — each Go module compiled into ipatool" in (lic / "INDEX.txt").read_text()


def test_each_library_built_into_python_gets_its_licence_text(tmp_path, monkeypatch):
    gp = tmp_path / "GraftPython"
    (gp / "bin").mkdir(parents=True)
    (gp / "lib/python3.13/lib-dynload").mkdir(parents=True)
    monkeypatch.setattr(gb, "defined_symbols", lambda paths: {"_BZ2_bzCompress", "_PyExpat_XML_ParserCreate",
                                                             "_python_hashlib_Hacl_Hash_SHA2_digest_256", "_main"})
    assert gb.python_static_libraries(gp) == ["bzip2", "expat", "hacl"]
    pbs = _tarball(tmp_path / "20261026.tar.gz", {"python-build-standalone-20261026/LICENSE.bzip2.txt":
                                                  ("bzip2 " + MIT_TEXT).encode(),
                                                  "python-build-standalone-20261026/LICENSE.expat.txt":
                                                  ("expat " + MIT_TEXT).encode()})
    hacl = tmp_path / "Hacl_Hash_SHA2.c"
    hacl.write_text("/* MIT License\n *\n * Copyright (c) 2016-2022 INRIA\n *\n * " + MIT_TEXT + " */\n\nint x;\n")
    files = {"python-build-standalone": pbs, "hacl": hacl}
    monkeypatch.setattr(gb, "fetch", lambda pin, cache=None: files[pin.name])
    dest = tmp_path / "licences"
    dest.mkdir()
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    assert gb.python_static_licences(pins, gp, dest) == [
        ("bzip2", "python-bzip2-LICENSE.txt"), ("expat", "python-expat-LICENSE.txt"), ("hacl", "python-hacl-LICENSE.txt")]
    assert (dest / "python-hacl-LICENSE.txt").read_text().startswith("MIT License\n\nCopyright (c) 2016-2022 INRIA")
    monkeypatch.setattr(gb, "defined_symbols", lambda paths: {"_lzma_code"})
    with pytest.raises(gb.BundleError, match=r"liblzma, but 20261026\.tar\.gz has no LICENSE\.liblzma\.txt"):
        gb.python_static_licences(pins, gp, tmp_path)


def _crate(path: Path, name: str, version: str, files: dict[str, bytes]) -> tuple[Path, str]:
    _tarball(path, {f"{name}-{version}/{f}": data for f, data in files.items()} | {f"{name}-{version}/src/lib.rs": b""})
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_each_rust_crate_in_a_wheels_sbom_gets_its_licence_checked_against_cargo_lock(tmp_path, monkeypatch):
    contents = tmp_path / "Contents"
    site = contents / "Resources/GraftPython/lib/python3.13/site-packages"
    dist = site / "qh3-1.9.4.dist-info"
    (dist / "sboms").mkdir(parents=True)
    (dist / "METADATA").write_text("Name: qh3\nVersion: 1.9.4\n")
    comps = [{"name": "ring", "version": "0.17.8", "purl": "pkg:cargo/ring@0.17.8", "licenses": [{"expression": "ISC"}]},
             {"name": "bare", "version": "1.0.0", "purl": "pkg:cargo/bare@1.0.0",
              "licenses": [{"expression": "MIT OR Apache-2.0"}]},
             {"name": "qh3", "version": "1.9.4", "purl": "pkg:cargo/qh3@1.9.4"}]
    (dist / "sboms/qh3.cyclonedx.json").write_text(json.dumps({"components": comps}))
    crates = tmp_path / "crates"
    crates.mkdir()
    ring, ring_sum = _crate(crates / "ring.crate", "ring", "0.17.8", {"LICENSE": ("ISC " + MIT_TEXT).encode()})
    bare, bare_sum = _crate(crates / "bare.crate", "bare", "1.0.0", {"README.md": b"no license here"})
    lock = (f'[[package]]\nname = "ring"\nversion = "0.17.8"\nchecksum = "{ring_sum}"\n'
            f'[[package]]\nname = "bare"\nversion = "1.0.0"\nchecksum = "{bare_sum}"\n'
            '[[package]]\nname = "qh3"\nversion = "1.9.4"\n')
    sdist = _tarball(tmp_path / "qh3-1.9.4.tar.gz", {"qh3-1.9.4/Cargo.lock": lock.encode()})
    fetched = []

    def fetch(pin, cache=None):
        fetched.append((pin.name, pin.sha256))
        return {"qh3": sdist, "ring": ring, "bare": bare}[pin.name]
    monkeypatch.setattr(gb, "fetch", fetch)
    monkeypatch.setattr(gb, "sdist_pin", lambda name, version, lic, locked: gb.Pin("qh3", "https://x/qh3.tar.gz", "a" * 64, ""))
    dest = tmp_path / "rust-crates"
    assert gb.rust_crate_licences(contents, {}, dest) == {"qh3": ["qh3/ring@0.17.8", "qh3/bare@1.0.0"]}
    assert ("ring", ring_sum) in fetched and ("bare", bare_sum) in fetched, "each crate checked by Cargo.lock"
    assert (dest / "qh3/ring@0.17.8/LICENSE").read_text().startswith("ISC ")
    assert (dest / "qh3/bare@1.0.0/Apache-2.0.txt").read_bytes() == gb.SPDX_FILES["Apache-2.0"].read_bytes()
    assert not (dest / "qh3/qh3@1.9.4").exists(), "the package's own crate is covered by its own license"
    comps[1]["licenses"] = [{"expression": "MIT"}]
    (dist / "sboms/qh3.cyclonedx.json").write_text(json.dumps({"components": comps}))
    with pytest.raises(gb.BundleError, match=r"bare 1\.0\.0"):
        gb.rust_crate_licences(contents, {}, tmp_path / "again")


def _rust_module(site: Path, dist_name: str, module: str, crates: list[str]) -> Path:
    """A compiled module naming each `crate-version` the way rustc's panic
    locations do, listed in its dist's RECORD."""
    path = site / module
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xcf\xfa\xed\xfe" + b"\0".join(
        f"/cargo/registry/src/index.crates.io-1949cf8c6b5b557f/{c}/src/lib.rs".encode() for c in crates))
    dist = site / f"{dist_name}.dist-info"
    dist.mkdir(parents=True, exist_ok=True)
    name, version = dist_name.rsplit("-", 1)
    (dist / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n")
    (dist / "RECORD").write_text(f"{module},sha256=x,1\n{dist.name}/METADATA,,\n")
    return path


def test_crates_are_read_from_the_source_paths_a_compiled_module_names(tmp_path):
    so = _rust_module(tmp_path, "x-1.0", "x/_x.so", ["pyo3-ffi-0.26.0", "curve25519-dalek-4.1.3",
                                                     "wasi-0.14.7+wasi-0.2.4", "md-5-0.10.6"])
    assert gb.crates_named(so) == {("pyo3-ffi", "0.26.0"), ("curve25519-dalek", "4.1.3"),
                                   ("wasi", "0.14.7+wasi-0.2.4"), ("md-5", "0.10.6")}


def _bcrypt_fixture(tmp_path, monkeypatch, *, named, lock_crates):
    """bcrypt as 5.0.0 ships: a Rust module, no SBOM; its sdist's Cargo.lock
    pins `lock_crates` (name, version, files) and the package's own crate."""
    contents = tmp_path / "Contents"
    site = contents / "Resources/GraftPython/lib/python3.13/site-packages"
    _rust_module(site, "bcrypt-5.0.0", "bcrypt/_bcrypt.abi3.so", named)
    crates = tmp_path / "crates"
    crates.mkdir()
    built, lock = {}, ""
    for name, version, files in lock_crates:
        built[name], sha = _crate(crates / f"{name}.crate", name, version, files)
        lock += f'[[package]]\nname = "{name}"\nversion = "{version}"\nchecksum = "{sha}"\n'
    lock += '[[package]]\nname = "bcrypt-rust"\nversion = "0.1.0"\n'
    sdist = _tarball(tmp_path / "bcrypt-5.0.0.tar.gz", {"bcrypt-5.0.0/src/_bcrypt/Cargo.lock": lock.encode()})
    monkeypatch.setattr(gb, "fetch", lambda pin, cache=None: {"bcrypt": sdist, **built}[pin.name])
    monkeypatch.setattr(gb, "sdist_pin", lambda name, version, lic, locked: gb.Pin(name, "https://x/b.tar.gz",
                                                                                   "a" * 64, ""))
    return contents


def test_a_rust_module_with_no_sbom_gets_every_crate_its_sdists_cargo_lock_pins(tmp_path, monkeypatch):
    """RC audit #1: bcrypt's wheel has no SBOM, so none of its crates had a
    notice. subtle (BSD-3-Clause) leaves no source path in the module, and
    is linked in all the same: the Cargo.lock names it."""
    toml = b'[package]\nname = "bare"\nversion = "1.0.0"\nlicense = "MIT OR Apache-2.0"\n'
    contents = _bcrypt_fixture(tmp_path, monkeypatch, named=["blowfish-0.9.1"], lock_crates=[
        ("blowfish", "0.9.1", {"LICENSE-MIT": MIT_TEXT.encode()}),
        ("subtle", "2.6.1", {"LICENSE": ("BSD-3-Clause " + MIT_TEXT).encode()}),
        ("bare", "1.0.0", {"Cargo.toml": toml, "README.md": b"no license here"})])
    dest = tmp_path / "rust-crates"
    assert gb.rust_crate_licences(contents, {}, dest) == {
        "bcrypt": ["bcrypt/bare@1.0.0", "bcrypt/blowfish@0.9.1", "bcrypt/subtle@2.6.1"]}
    assert (dest / "bcrypt/subtle@2.6.1/LICENSE").read_text().startswith("BSD-3-Clause ")
    # No license file: its Cargo.toml's license offers Apache-2.0, whose text Graft has.
    assert (dest / "bcrypt/bare@1.0.0/Apache-2.0.txt").read_bytes() == gb.SPDX_FILES["Apache-2.0"].read_bytes()
    assert not (dest / "bcrypt/bcrypt-rust@0.1.0").exists(), "the package's own crate is covered by its own license"


def test_a_crate_the_module_names_that_its_cargo_lock_does_not_pin_is_refused(tmp_path, monkeypatch):
    contents = _bcrypt_fixture(tmp_path, monkeypatch, named=["blowfish-0.9.1", "stray-1.0.0"], lock_crates=[
        ("blowfish", "0.9.1", {"LICENSE-MIT": MIT_TEXT.encode()})])
    with pytest.raises(gb.BundleError, match=r"Cargo\.lock doesn't list: stray 1\.0\.0"):
        gb.rust_crate_licences(contents, {}, tmp_path / "rust-crates")


def test_a_rust_module_with_neither_an_sbom_nor_a_cargo_lock_is_refused(tmp_path, monkeypatch):
    contents = _bcrypt_fixture(tmp_path, monkeypatch, named=["blowfish-0.9.1"], lock_crates=[])
    sdist = _tarball(tmp_path / "no-lock.tar.gz", {"bcrypt-5.0.0/setup.py": b""})
    monkeypatch.setattr(gb, "fetch", lambda pin, cache=None: sdist)
    with pytest.raises(gb.BundleError, match="no SBOM and its sdist no Cargo.lock"):
        gb.rust_crate_licences(contents, {}, tmp_path / "rust-crates")


def test_a_wheel_with_no_rust_in_it_fetches_nothing(tmp_path, monkeypatch):
    site = tmp_path / "Contents/Resources/GraftPython/lib/python3.13/site-packages"
    _rust_module(site, "cffi-2.1.1", "_cffi_backend.so", [])
    monkeypatch.setattr(gb, "fetch", lambda pin, cache=None: pytest.fail(f"fetched {pin.name}"))
    assert gb.rust_crate_licences(tmp_path / "Contents", {}, tmp_path / "rust-crates") == {}


def test_a_crate_an_sbom_misses_is_refused_too(tmp_path, monkeypatch):
    contents = _bcrypt_fixture(tmp_path, monkeypatch, named=["blowfish-0.9.1", "sha2-0.10.9"], lock_crates=[
        ("blowfish", "0.9.1", {"LICENSE-MIT": MIT_TEXT.encode()}),
        ("sha2", "0.10.9", {"LICENSE-MIT": MIT_TEXT.encode()})])
    dist = next((contents / "Resources/GraftPython/lib/python3.13/site-packages").glob("bcrypt-*.dist-info"))
    (dist / "sboms").mkdir()
    (dist / "sboms/bcrypt.cdx.json").write_text(json.dumps({"components": [
        {"name": "blowfish", "version": "0.9.1", "purl": "pkg:cargo/blowfish@0.9.1"}]}))
    with pytest.raises(gb.BundleError, match=r"SBOM doesn't list: sha2 0\.10\.9"):
        gb.rust_crate_licences(contents, {}, tmp_path / "rust-crates")


def _core_lock(path: Path, crates: dict[str, tuple[Path, str]], licence: str = "MIT") -> Path:
    lock = path
    lock.write_text("".join(f'[[package]]\nname = "{c.rsplit("-", 1)[0]}"\nversion = "{c.rsplit("-", 1)[1]}"\n'
                            f'license = "{licence}"\nchecksum = "{sha}"\n' for c, (_, sha) in crates.items()))
    return lock


def test_ipatools_core_crates_come_from_the_pinned_lock(tmp_path, monkeypatch):
    """RC audit #1: the 1Password SDK core inside ipatool is Rust built to
    WebAssembly, with no Cargo.lock anywhere; its crates are pinned in
    bundle/ipatool-core-crates.lock and must be the ones the binary names."""
    crates = {c: _crate(tmp_path / f"{c}.crate", *c.rsplit("-", 1), {"LICENSE": ("ISC " + MIT_TEXT).encode()})
              for c in ("ring-0.17.14", "zxcvbn-3.1.0")}
    monkeypatch.setattr(gb, "fetch", lambda pin, cache=None: crates[f"{pin.name}-{pin.version}"][0])
    ipatool = tmp_path / "ipatool"
    ipatool.write_bytes(b"go code\0" + b"\0".join(
        f"/usr/local/cargo/registry/src/index.crates.io-1949cf8c6b5b557f/{c}/src/x.rs".encode() for c in crates))
    dest = tmp_path / "rust-crates"
    lock = _core_lock(tmp_path / "core-crates.lock", crates)
    assert gb.binary_crate_licences(ipatool, "ipatool", dest, lock) == ["ipatool/ring@0.17.14", "ipatool/zxcvbn@3.1.0"]
    assert (dest / "ipatool/ring@0.17.14/LICENSE").read_text().startswith("ISC ")
    with pytest.raises(gb.BundleError, match=r"not pinned: zxcvbn 3\.1\.0"):
        gb.binary_crate_licences(ipatool, "ipatool", tmp_path / "again", _core_lock(
            tmp_path / "ring-only.lock", {"ring-0.17.14": crates["ring-0.17.14"]}))
    ipatool.write_bytes(b"go code, and a core with ring alone: /registry/src/i/ring-0.17.14/src/x.rs")
    with pytest.raises(gb.BundleError, match=r"pinned but not in it: zxcvbn 3\.1\.0"):
        gb.binary_crate_licences(ipatool, "ipatool", tmp_path / "third", lock)


def test_the_pinned_core_crates_each_have_a_checksum_and_a_licence():
    pinned = gb.pinned_crates()
    assert len(pinned) == 108 and ("ring", "0.17.14") in pinned and ("nom-supreme", "0.6.0") in pinned
    for (name, version), p in pinned.items():
        assert re.fullmatch(r"[0-9a-f]{64}", p["checksum"]) and p["license"], (name, version)
    assert gb.core_crate_sources() == [("nom-supreme", "0.6.0", "MPL-2.0")]


def test_a_binary_naming_a_crate_with_no_licence_folder_is_named(tmp_path):
    contents = tmp_path / "Contents"
    so = _rust_module(contents, "x-1.0", "Helpers/bin/tool", ["ring-0.17.14", "spin-0.9.8"])
    (contents / "Resources/Licenses/rust-crates/tool/ring@0.17.14").mkdir(parents=True)
    assert gb.crate_notice_problems(contents, [so]) == [
        "Helpers/bin/tool holds Rust crates with no license in Licenses/rust-crates/: spin@0.9.8"]
    (contents / "Resources/Licenses/rust-crates/other/spin@0.9.8").mkdir(parents=True)
    assert gb.crate_notices_needed([so], lock=_core_lock(tmp_path / "none.lock", {})) == {
        so: {"ring@0.17.14", "spin@0.9.8"}}
    assert gb.crate_notice_problems(contents, [so]) == []


def _leb(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _custom_section(name: bytes, payload: bytes) -> bytes:
    body = _leb(len(name)) + name + payload
    return b"\0" + _leb(len(body)) + body


def _wasm(functions: list[str] = (), bindgen: list[tuple[str, str]] = ()) -> bytes:
    """A WebAssembly module as ipatool embeds its 1Password SDK core: a
    `name` section naming `functions`, and a `__wasm_bindgen_unstable`
    section with one entry per (wasm-bindgen version, `<crate>-<hash>`),
    laid out as wasm-bindgen 0.2.100 writes them."""
    names = _leb(len(functions)) + b"".join(_leb(i) + _leb(len(f.encode())) + f.encode()
                                            for i, f in enumerate(functions))
    module = b"\0asm\x01\0\0\0" + _custom_section(b"name", b"\x01" + _leb(len(names)) + names)
    if bindgen:
        entries = b""
        for version, crate in bindgen:
            schema = json.dumps({"schema_version": version, "version": version}).encode()
            program = b"\x01\x06window\x01\x01\0\0\0\0\0\0\0" + bytes([len(crate)]) + crate.encode() + b"\0\0"
            entries += (len(schema).to_bytes(4, "little") + schema
                        + len(program).to_bytes(4, "little") + program)
        module += _custom_section(b"__wasm_bindgen_unstable", entries)
    return module


#: Function names as the core's `name` section has them (legacy Rust
#: mangling), and the crates each shows.
_CORE_SYMBOLS = [
    "_ZN10extism_pdk6extism5alloc17h944d363220faf261E",  # extism_pdk
    "_ZN60_$LT$reqwest..error..Error$u20$as$u20$core..fmt..Display$GT$3fmt17ha5d1cf94e187867bE",  # reqwest, core
    "_ZN3md58compress17h0123456789abcdefE",  # md-5, whose library is md5
    "_ZN6op_log8loggable7reqwest20redact_urls_in_error17h48478c94fa5fa4e6E",  # 1Password's own
    "_ZN4core3ops8function6FnOnce9call_once17h6a2d713b524bcf86E.llvm.8223809834213924217",  # std's
    "_ZN8dlmalloc8dlmalloc17Dlmalloc$LT$A$GT$6malloc17h0123456789abcdefE",  # std's: under /rust/deps/
    "ring_core_0_17_14__bn_mul_mont",  # C, not Rust
    "_RNvCsiGVaDesi5rv_7___rustc12___rust_alloc",  # v0 mangling, std's
]


def _core_binary(path: Path, *, paths: list[str] = (), symbols: list[str] = _CORE_SYMBOLS,
                 bindgen: list[tuple[str, str]] = (("0.2.100", "serde-wasm-bindgen-2f9f48233d7fab00"),
                                                   ("0.2.100", "op-fileaccess-1b0fda583a9d94ce"))) -> Path:
    """ipatool as it ships: Go code, then the core, a WebAssembly module
    whose crates.io source paths are `paths`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"go code \0asm\x01\0\0\0 and a false start\0" + _wasm(symbols, bindgen)
                     + b"\0".join(f"/usr/local/cargo/registry/src/index.crates.io-1949cf8c6b5b557f/{c}/src/x.rs"
                                  .encode() for c in paths)
                     + b"\0/rust/deps/dlmalloc-0.2.10/src/dlmalloc.rs\0")
    return path


def test_crates_are_read_from_an_embedded_webassembly_modules_names_too(tmp_path):
    """Delta audit #1: only a crate whose code can panic leaves a source
    path. The core's `name` section shows the rest by their function names,
    and its wasm-bindgen section by the crates that call JavaScript, and
    records wasm-bindgen's own version."""
    ipatool = _core_binary(tmp_path / "ipatool", paths=["ring-0.17.14"])
    assert gb.crates_compiled(ipatool) == {"extism_pdk", "reqwest", "md5", "op_log", "serde_wasm_bindgen",
                                           "op_fileaccess"}
    assert gb.crates_named(ipatool) == {("ring", "0.17.14"), ("wasm-bindgen", "0.2.100")}
    plain = tmp_path / "plain"
    plain.write_bytes(b"\0asm\x01\0\0\0" + b"\xff" * 64 + b"/registry/src/i/spin-0.9.8/src/lib.rs")
    assert gb.crates_compiled(plain) == set()
    assert gb.crates_named(plain) == {("spin", "0.9.8")}


def _named_lock(path: Path, crates: dict[str, tuple[Path, str]], seen: dict[str, str], *,
                lib: dict[str, str] | None = None, own: str | None = "op_") -> Path:
    lib = lib or {}
    path.write_text((f'own-prefix = "{own}"\n' if own else "") + "".join(
        f'[[package]]\nname = "{c.rsplit("-", 1)[0]}"\nversion = "{c.rsplit("-", 1)[1]}"\n'
        + (f'seen = "{seen[c]}"\n' if c in seen else "")
        + (f'lib = "{lib[c]}"\n' if c in lib else "")
        + f'license = "MIT"\nchecksum = "{sha}"\n' for c, (_, sha) in crates.items()))
    return path


def test_ipatools_core_crates_include_those_only_its_names_show(tmp_path, monkeypatch):
    """Delta audit #1: the gate compared the lock with the source paths
    alone, so 36 crates compiled into the core had no notice and the stage
    passed. A crate the core's names show must be pinned too, with the
    version the lock gives it; one pinned by name must be there by name."""
    files = {c: _crate(tmp_path / f"{c}.crate", *c.rsplit("-", 1), {"LICENSE": ("ISC " + MIT_TEXT).encode()})
             for c in ("ring-0.17.14", "extism-pdk-1.4.1", "reqwest-0.12.28", "md-5-0.10.6",
                       "serde-wasm-bindgen-0.6.5", "wasm-bindgen-0.2.100")}
    monkeypatch.setattr(gb, "fetch", lambda pin, cache=None: files[f"{pin.name}-{pin.version}"][0])
    ipatool = _core_binary(tmp_path / "ipatool", paths=["ring-0.17.14"])
    seen = {"extism-pdk-1.4.1": "name", "reqwest-0.12.28": "name", "md-5-0.10.6": "name",
            "serde-wasm-bindgen-0.6.5": "name", "wasm-bindgen-0.2.100": "bindgen"}
    lib = {"md-5-0.10.6": "md5"}
    lock = _named_lock(tmp_path / "core.lock", files, seen, lib=lib)
    assert gb.binary_crate_licences(ipatool, "ipatool", tmp_path / "out", lock) == [
        "ipatool/extism-pdk@1.4.1", "ipatool/md-5@0.10.6", "ipatool/reqwest@0.12.28", "ipatool/ring@0.17.14",
        "ipatool/serde-wasm-bindgen@0.6.5", "ipatool/wasm-bindgen@0.2.100"]

    def refused(n: int, **change) -> str:
        crates = {c: v for c, v in files.items() if c not in change.get("drop", ())}
        with pytest.raises(gb.BundleError) as e:
            gb.binary_crate_licences(ipatool, "ipatool", tmp_path / f"out{n}", _named_lock(
                tmp_path / f"core{n}.lock", crates, change.get("seen", seen), lib=change.get("lib", lib),
                own=change.get("own", "op_")))
        return str(e.value)

    assert "not pinned: extism_pdk (named in the core)" in refused(1, drop={"extism-pdk-1.4.1"})
    assert "not pinned: serde_wasm_bindgen (named in the core)" in refused(2, drop={"serde-wasm-bindgen-0.6.5"})
    assert "not pinned: md5 (named in the core)" in refused(3, lib={})
    assert "pinned but not in it: md-5 0.10.6" in refused(4, lib={})
    assert "not pinned: wasm-bindgen 0.2.100" in refused(5, drop={"wasm-bindgen-0.2.100"})
    assert "not pinned: op_fileaccess (named in the core), op_log (named in the core)" in refused(6, own=None)
    # A crate pinned as seen by its source path must be: a name isn't enough.
    assert "pinned but not in it: reqwest 0.12.28" in refused(7, seen={**seen, "reqwest-0.12.28": "path"})
    _core_binary(ipatool, paths=["ring-0.17.14"], symbols=[s for s in _CORE_SYMBOLS if "reqwest" not in s])
    assert "pinned but not in it: reqwest 0.12.28" in refused(8)


def test_a_crate_only_the_names_show_needs_its_pinned_versions_notice(tmp_path):
    """Delta audit #1: verify-bundle's check read source paths alone, and
    22 of the 36 crates were "covered" only by another package's folder at
    another version."""
    contents = tmp_path / "Contents"
    tool = _core_binary(contents / "Helpers/bin/tool", paths=["ring-0.17.14"])
    lock = _named_lock(tmp_path / "core.lock", {c: (tmp_path, "0" * 64) for c in (
        "ring-0.17.14", "extism-pdk-1.4.1", "reqwest-0.12.28", "md-5-0.10.6", "serde-wasm-bindgen-0.6.5",
        "wasm-bindgen-0.2.100")}, {"extism-pdk-1.4.1": "name", "reqwest-0.12.28": "name",
                                   "md-5-0.10.6": "name", "serde-wasm-bindgen-0.6.5": "name",
                                   "wasm-bindgen-0.2.100": "bindgen"}, lib={"md-5-0.10.6": "md5"})
    lic = contents / "Resources/Licenses/rust-crates"
    for crate in ("tool/ring@0.17.14", "tool/reqwest@0.12.28", "other/extism-pdk@1.3.0", "tool/wasm-bindgen@0.2.100"):
        (lic / crate).mkdir(parents=True)
    assert gb.crate_notices_needed([tool], lock=lock) == {tool: {
        "ring@0.17.14", "extism-pdk@1.4.1", "reqwest@0.12.28", "md-5@0.10.6", "serde-wasm-bindgen@0.6.5",
        "wasm-bindgen@0.2.100"}}
    assert gb.crate_notice_problems(contents, [tool], lock=lock) == [
        "Helpers/bin/tool holds Rust crates with no license in Licenses/rust-crates/: "
        "extism-pdk@1.4.1, md-5@0.10.6, serde-wasm-bindgen@0.6.5"]
    for crate in ("tool/extism-pdk@1.4.1", "other/md-5@0.10.6", "tool/serde-wasm-bindgen@0.6.5"):
        (lic / crate).mkdir(parents=True)
    assert gb.crate_notice_problems(contents, [tool], lock=lock) == []
    # A crate its names show that no lock pins has no version to look for.
    _core_binary(tool, paths=["ring-0.17.14"], symbols=[*_CORE_SYMBOLS, "_ZN5stray1f17h0123456789abcdefE"])
    assert gb.crate_notice_problems(contents, [tool], lock=lock) == [
        "Helpers/bin/tool holds Rust crates with no license in Licenses/rust-crates/: "
        "stray (no version pinned in core.lock)"]


def test_a_crate_that_ships_no_licence_text_takes_the_one_its_lock_entry_names(tmp_path, monkeypatch):
    """extism-convert and extism-manifest (BSD-3-Clause, whose clause 2 wants
    the copyright notice) ship none in their crates: the lock names the text
    from their repository, whose first line must name the crate and version."""
    crate, sha = _crate(tmp_path / "x.crate", "extism-convert", "1.13.0", {"README.md": b"no license here"})
    monkeypatch.setattr(gb, "fetch", lambda pin, cache=None: crate)
    text = tmp_path / "extism-LICENSE"
    text.write_text("# extism-convert 1.13.0 and extism-manifest 1.13.0. Their crates ship no license file.\n"
                    "Copyright 2022 Example, Inc.\n\nBSD-3-Clause " + MIT_TEXT)
    owner = tmp_path / "out" / "ipatool"
    assert gb._crate_licence_folder("extism-convert", "1.13.0", sha, "BSD-3-Clause", owner, "ipatool",
                                    licence_file=text) == "ipatool/extism-convert@1.13.0"
    assert (owner / "extism-convert@1.13.0/LICENSE").read_text() == text.read_text()
    text.write_text("# extism-convert 1.12.0. Their crates ship no license file.\nBSD-3-Clause " + MIT_TEXT)
    with pytest.raises(gb.BundleError, match=r"is for extism-convert 1\.12\.0.*take the license text at 1\.13\.0"):
        gb._crate_licence_folder("extism-convert", "1.13.0", sha, "BSD-3-Clause", tmp_path / "again", "ipatool",
                                 licence_file=text)
    with pytest.raises(gb.BundleError, match="ships no license text"):
        gb._crate_licence_folder("extism-convert", "1.13.0", sha, "BSD-3-Clause", tmp_path / "third", "ipatool")


def test_the_pinned_core_crates_are_every_crate_try13s_core_shows():
    """Delta audit #1: the 36 crates.io crates try13's core.wasm shows only
    by name (and two that call JavaScript through wasm-bindgen) are pinned
    beside the 69 its source paths name, each by how the core shows it."""
    pinned = gb.pinned_crates()
    seen = {}
    for (name, _version), p in pinned.items():
        seen.setdefault(p.get("seen", "path"), set()).add(name)
    assert len(seen["path"]) == 67 and sum(p.get("seen", "path") == "path" for p in pinned.values()) == 69
    assert seen["bindgen"] == {"wasm-bindgen"}
    assert seen["name"] == {
        "aes-gcm", "cbc", "crypto-common", "des", "digest", "ed25519-dalek", "extism-convert", "extism-manifest",
        "extism-pdk", "form_urlencoded", "futures-lite", "getrandom", "hmac", "js-sys", "libm", "litemap",
        "md-5", "num-traits", "pbkdf2", "pkcs1", "pkcs8", "poly1305", "polyval", "reqwest", "sha1", "sha2",
        "spki", "subtle", "typeshare", "universal-hash", "untrusted", "urlencoding", "utf8_iter", "web-sys",
        "zmij", "serde-wasm-bindgen", "wasm-bindgen-futures"}
    assert {v for (n, v) in pinned if n == "getrandom"} == {"0.2.17", "0.3.4"}, "ring's and pasetors'"
    assert {gb._lib(p) for p in pinned.values() if gb._lib(p) != p["name"].replace("-", "_")} == {"md5"}
    for p in pinned.values():
        if "license-file" in p:
            first = (gb.VENDORED_LICENSES / p["license-file"]).read_text().split("\n", 1)[0]
            assert f"{p['name']} {p['version']}" in first, p["name"]
    assert {p["name"] for p in pinned.values() if "license-file" in p} == {"extism-convert", "extism-manifest"}


def test_every_component_names_the_licence_files_that_cover_it(tmp_path):
    lic = tmp_path / "Licenses"
    for rel in ("openssl-LICENSE.txt", "cctools-APPLE_LICENSE", "ipatool-LICENSE", "ipatool-modules/a@v1/LICENSE",
                "python-LICENSE.txt", "python-hacl-LICENSE.txt", "go-LICENSE",
                "python-packages/qh3-1.9.4/LICENSE", "rust-crates/qh3/ring@0.17.8/LICENSE",
                "rust-crates/ipatool/ring@0.17.14/LICENSE", "python-packages/graft-ipa-1.1.0/LICENSE", "python-packages/cyan-1.4.4/LICENSE",
                "cyan-extras/Orion-LICENSE"):
        (lic / rel).parent.mkdir(parents=True, exist_ok=True)
        (lic / rel).write_text("x")
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")

    def files(**entry):
        return gb.licence_files_for({"kind": "artifact", "version": "1", **entry}, lic, pins)
    assert files(name="openssl", kind="built-from-source") == ["openssl-LICENSE.txt"]
    assert files(name="otool", kind="built-from-source") == ["cctools-APPLE_LICENSE"]
    assert files(name="ipatool", kind="built-from-source") == ["ipatool-LICENSE", "ipatool-modules/a@v1/LICENSE",
                                                               "rust-crates/ipatool/ring@0.17.14/LICENSE"]
    assert files(name="python") == ["python-LICENSE.txt", "python-hacl-LICENSE.txt"]
    assert files(name="hacl", kind="licence-text") == [] and files(name="hacl") == ["python-hacl-LICENSE.txt"]
    assert files(name="go", kind="toolchain") == ["go-LICENSE"]
    assert files(name="qh3", version="1.9.4", kind="python-package") == [
        "python-packages/qh3-1.9.4/LICENSE", "rust-crates/qh3/ring@0.17.8/LICENSE"]
    assert files(name="Orion.framework", cyan_commit="c") == ["cyan-extras/Orion-LICENSE"]
    assert files(name=gb.STUB_COMPONENT, kind="built-from-source") == ["python-packages/graft-ipa-1.1.0/LICENSE"]
    assert files(name="cyan") == ["python-packages/cyan-1.4.4/LICENSE"]


def test_a_copyleft_package_with_no_source_distribution_is_refused(tmp_path, monkeypatch):
    _, components, lock, _ = _sources_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(gb, "_pypi_sdist", lambda name, version: None)
    with pytest.raises(gb.NoSdist, match="gplpkg"):
        gb.write_sources(components, tmp_path / "Graft-9.9-sources.tar.gz", lock=lock)


def test_openssl_builds_in_the_clean_environment_and_its_key_covers_steps_and_toolchain(tmp_path, monkeypatch):
    """Review #22, #27, #29, #31: OpenSSL's cache was keyed by its source alone
    and it built with the release machine's whole environment, so a changed
    Configure line or compiler reused a stale build that SOURCES.md then
    misdescribed. Now it builds like the helpers, under _build_env, keyed by
    its steps and the toolchain like them too."""
    envs, toolchain = [], ["clang 1; SDK 15.0"]
    pin = gb.Pin(name="openssl", url="https://example.invalid/o.tar.gz", sha256="d" * 64, license="Apache-2.0",
                 version="3.5.9")

    def unpack(pin, tmp):
        (tmp / "src" / "openssl-3.5.9").mkdir(parents=True)
        (tmp / "src" / "openssl-3.5.9" / "LICENSE.txt").write_text("Apache License")
        return tmp / "src" / "openssl-3.5.9"

    def build_steps(steps, cwd, arch="arm64"):
        envs.append(steps)
        dest = Path(next(s for s in steps[-1] if s.startswith("DESTDIR=")).removeprefix("DESTDIR="))
        lib = dest / gb.OPENSSL_BAKED_PREFIX.lstrip("/") / "lib"
        (lib / "ossl-modules").mkdir(parents=True)
        for f in (*gb.OPENSSL_LIBS, "ossl-modules/legacy.dylib"):
            (lib / f).write_bytes(b"lib")

    monkeypatch.setattr(gb, "_unpack", unpack)
    monkeypatch.setattr(gb, "_build_steps", build_steps)
    monkeypatch.setattr(gb, "toolchain_id", lambda: toolchain[0])
    first = gb.build_openssl(pin, tmp_path / "work")
    assert (first / "lib" / "libcrypto.3.dylib").is_file() and (first / "LICENSE.txt").is_file()
    assert envs and envs[0][0][0] == "./Configure", "run through _build_steps, so under _build_env"
    assert gb.build_openssl(pin, tmp_path / "work") == first and len(envs) == 1, "cached"
    toolchain[0] = "clang 2; SDK 15.0"
    assert gb.build_openssl(pin, tmp_path / "work") != first, "a new compiler is a new build"
    monkeypatch.setattr(gb, "openssl_steps", lambda destdir, jobs, arch="arm64": [["./Configure", "no-asm"], ["make", "install_sw",
                                                                                                f"DESTDIR={destdir}"]])
    assert gb.build_openssl(pin, tmp_path / "work").parent.name not in {first.parent.name}, "new steps, new build"


def test_every_build_key_and_sources_md_name_the_toolchain():
    import inspect
    for fn in (gb.build_libplist, gb.build_ldid, gb.build_cctools, gb.build_insert_dylib, gb.ipatool_key,
               gb.unfairplay_key, gb.build_preferences_stub, gb.build_openssl, gb.build_zsign):
        assert "toolchain_id()" in inspect.getsource(fn), fn.__name__
    for fn, key in ((gb.build_ipatool, "ipatool_key"), (gb.build_unfairplay, "unfairplay_key")):
        assert re.search(rf"{key}\(pin, go_pin(, arch)?\)", inspect.getsource(fn)), fn.__name__
    assert "toolchain_id()" in inspect.getsource(gb._toolchain)



def test_a_package_whose_licence_is_not_stated_needs_its_source_as_copyleft_does():
    """Review #33: "not stated" was taken for permissive."""
    assert gb.needs_source("not stated in its metadata; see its license file")
    assert gb.needs_source("GPLv3") and not gb.needs_source("MIT")


# ---- combined fix check #9, #10, #11 ----------------------------------------------


def test_ipatools_licence_names_its_modules_copyleft_licences():
    """#9: ipatool's binary holds golang-lru (MPL-2.0) and retry.v1
    (LGPL-3.0); an upstream "MIT" copied over would drop both, and the
    source archive's reason to carry its vendored modules."""
    pin = gb.load_pins(REPO / "bundle" / "pins.toml")["ipatool"]
    assert "MPL-2.0" in pin.license and "LGPL-3.0-only" in pin.license, pin.license
    assert gb.is_copyleft(pin.license)


def test_the_source_archive_gets_ipatools_vendored_modules_from_its_build(tmp_path, monkeypatch):
    """#9: the archive test replaces built_sources; this is built_sources."""
    build = tmp_path / "ipatool-build"
    build.mkdir()
    (build / "vendor.tar.gz").write_bytes(b"vendored")
    unfairplay = tmp_path / "unfairplay-build"
    unfairplay.mkdir()
    (unfairplay / "vendor.tar.gz").write_bytes(b"vendored too")
    asked = []
    monkeypatch.setattr(gb, "build_ipatool", lambda pin, go: asked.append((pin.name, go.name)) or build)
    monkeypatch.setattr(gb, "build_unfairplay", lambda pin, go: asked.append((pin.name, go.name)) or unfairplay)
    fetched = []
    monkeypatch.setattr(gb, "fetch_crate", lambda name, version, sha256, cache=None:
                        fetched.append((name, version, sha256)) or tmp_path / f"{name}-{version}.crate")
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    got = gb.built_sources(pins)
    assert got[:2] == [(build / "vendor.tar.gz", "ipatool/vendor.tar.gz"),
                       (unfairplay / "vendor.tar.gz", "unfairplay/vendor.tar.gz")]
    assert asked == [("ipatool", "go"), ("unfairplay", "go")]
    # RC audit #1: MPL-2.0 has its source go with the binary, and nom-supreme
    # is in ipatool's 1Password SDK core with no other license to choose.
    nom = gb.pinned_crates()[("nom-supreme", "0.6.0")]
    assert got[2:] == [(tmp_path / "nom-supreme-0.6.0.crate", "ipatool/core-crates/nom-supreme-0.6.0.crate")]
    assert fetched == [("nom-supreme", "0.6.0", nom["checksum"])]


@pytest.mark.parametrize("licence, only", [
    ("MPL-2.0", True), ("LGPL-3.0-only WITH LGPL-3.0-linking-exception", True), ("MIT", False),
    ("MIT OR Apache-2.0 OR LGPL-2.1-or-later", False), ("Apache-2.0 OR GPL-2.0-only", False),
    ("MIT/Apache-2.0", False), ("", False),
    # The delta audit's d14-bundle-3: read clause by clause, an OR in one
    # clause is no choice about another clause's copyleft license.
    ("(MIT OR Apache-2.0) AND MPL-2.0", True), ("LGPL-2.1-only AND (MIT OR Apache-2.0)", True),
    ("Apache-2.0/MIT AND GPL-3.0-only", True), ("MPL-2.0 OR (MIT AND Apache-2.0)", False),
    ("(MPL-2.0 OR MIT) AND Apache-2.0", False), ("GPL-3.0-or-later", True), ("MIT or Apache-2.0", False),
    ("Apache-2.0 WITH LLVM-exception OR GPL-2.0-only", False), ("GPL-2.0-only WITH Classpath-exception-2.0", True),
    # A wheel's SBOM gives several licenses space-joined: one text, copyleft if any part is.
    ("MPL-2.0 MIT", True),
    # Not an expression: copyleft-only if it names a copyleft license at all.
    ("MPL-2.0 OR", True), ("MIT AND (MPL-2.0", True), ("MIT OR (Apache-2.0", False)])
def test_a_crate_whose_licence_offers_a_permissive_choice_needs_no_source(licence, only):
    assert gb.copyleft_only(licence) is only


def test_a_licence_expression_is_read_clause_by_clause():
    assert gb.licence_expression("(MIT OR Apache-2.0) AND MPL-2.0") == ("and", [("or", ["MIT", "Apache-2.0"]), "MPL-2.0"])
    assert gb.licence_expression("MIT OR Apache-2.0 AND MPL-2.0") == ("or", ["MIT", ("and", ["Apache-2.0", "MPL-2.0"])])
    assert gb.licence_expression("A/B AND C") == ("and", [("or", ["A", "B"]), "C"]), "Cargo's / never makes C optional"
    assert gb.licence_expression("LGPL-3.0-only WITH LGPL-3.0-linking-exception") == "LGPL-3.0-only"
    assert gb.licence_expression("GNU General Public License v3") == "GNU General Public License v3"
    for broken in ("", "MIT AND", "(MIT", "MIT)", "AND MIT", "MIT WITH", "MIT () Apache-2.0"):
        assert gb.licence_expression(broken) is None, broken


def test_every_licence_ipatools_core_crates_are_pinned_with_parses():
    """So none of them is called copyleft-only for want of being read."""
    for (crate, version), pinned in gb.pinned_crates().items():
        assert gb.licence_expression(pinned["license"]) is not None, (crate, version, pinned["license"])
    assert [c for c, _, _ in gb.core_crate_sources()] == ["nom-supreme"]


def _md(tmp_path, monkeypatch) -> str:
    pins, components, lock, _ = _sources_fixture(tmp_path, monkeypatch)
    out = tmp_path / "out" / "Graft-9.9-sources.tar.gz"
    out.parent.mkdir()
    gb.write_sources(components, out, lock=lock)
    with tarfile.open(out) as tf:
        return tf.extractfile("Graft-9.9-sources/SOURCES.md").read().decode()


def _section(md: str, name: str) -> str:
    return md.split(f"\n### {name}\n", 1)[1].split("\n### ", 1)[0].split("\n## ", 1)[0]


def test_sources_md_tells_whoever_replaces_libplist_to_re_sign_ldid(tmp_path, monkeypatch):
    """#10 (review #21, #24): ldid runs under the hardened runtime, so a
    replaced libplist is refused until ldid is re-signed ad hoc."""
    libplist = _section(_md(tmp_path, monkeypatch), "libplist")
    assert "replace it with your own build" in libplist
    assert "library validation" in libplist
    assert f"`codesign --force --sign - {gb.CYAN_TOOLS}/ldid`" in libplist
    # Docs agent, 2026-11-23: the steps re-signed ldid but never the app, so
    # its seal broke and macOS called it damaged. Work on a copy, re-seal the
    # whole app last, and know what that costs.
    assert "work on a copy of Graft.app" in libplist
    reseal = "`codesign --force --sign - Graft.app`"
    assert reseal in libplist and libplist.index(reseal) > libplist.index(f"{gb.CYAN_TOOLS}/ldid`")
    assert "or macOS reports it as damaged" in libplist
    assert "Set Up Decryption can no longer check its copy of UnfairPlay" in libplist
    assert "Run Unchecked Copy as Root…" in libplist
    notices = (REPO / "THIRD_PARTY_NOTICES.md").read_text()
    assert "re-sign ldid ad hoc, `codesign --force --sign -`" in notices


def test_sources_md_never_calls_cyans_unlicense_copyleft(tmp_path, monkeypatch):
    """#11: cyan has no recipe, and took the copyleft default."""
    md = _md(tmp_path, monkeypatch)
    cyan = _section(md, "cyan")
    assert "License: Unlicense" in cyan and "copyleft" not in cyan
    assert "installed into the app's Python from its pinned zip, so this archive" in cyan
    intro = md.split("\n## ", 1)[0]
    assert "whose license is copyleft" not in intro
    assert "source distribution of every bundled Python package that publishes one" in intro


def test_the_docs_say_what_the_licence_statement_says_and_that_ipatool_downloads_unicorn():
    """Option (a) for the combined fix check's medium #2: the README and the
    notices carry the statement word for word, and network.md says when the
    bundled ipatool fetches Unicorn and what the app says offline."""
    from graft_ipa import ipatool

    for doc in ("README.md", "THIRD_PARTY_NOTICES.md"):
        text = " ".join((REPO / doc).read_text().split())
        assert gb.LICENCE_STATEMENT in text, doc
        assert "GPL-2.0 (Unicorn, which only ipatool loads)" not in text, doc
    network = " ".join((REPO / "docs" / "network.md").read_text().split())
    assert "files.pythonhosted.org" in network and "Unicorn 2.1.4's macOS wheel" in network
    assert ipatool.UNICORN_FIRST_USE.split(";")[0] in network
    assert "so it never fetches it" not in network


# ---- the minimum macOS (owner, 2026-11-22: 14.0, the floor the app's Observation code sets;
# 2026-11-25: 13.0 for the Intel build, for Intel Macs on Ventura) ----

#: Each bundle's floor: Graft.zip's, and Graft-Intel.zip's.
FLOORS = {"arm64": "14.0", "x86_64": "13.0"}


def test_each_bundle_s_minimum_macos_reaches_every_component_the_stage_builds_for_it():
    """One floor per bundle, so no component can be left at another target:
    the helpers, OpenSSL, zsign, the launchers, ipatool's cgo and the build
    env. The wheels are chosen as for 14.0 in both: none the Intel bundle
    takes needs more than 10.15, and verify-bundle checks that every one
    loads on its bundle's floor."""
    assert gb.MACOS_MINS == FLOORS
    assert gb.MACOS_MIN == gb.macos_min() == gb.macos_min("arm64") == "14.0"
    for arch, floor in FLOORS.items():
        flag = f"-mmacosx-version-min={floor}"
        assert flag in gb.target(arch)
        assert gb._build_env(arch)["MACOSX_DEPLOYMENT_TARGET"] == floor
        assert gb.ipatool_env(arch)["CGO_CFLAGS"] == flag == gb.ipatool_env(arch)["CGO_LDFLAGS"]
        assert flag in gb.openssl_steps("/x", [], arch)[0]
        for steps in (gb.libplist_steps("$D", [], arch), gb.ldid_steps("$L", "$O", [], arch), gb.cctools_steps([], arch),
                      gb.insert_dylib_steps(arch), gb.zsign_steps("$O", [], "0.7", arch)):
            assert flag in json.dumps(steps) and f"-mmacosx-version-min={FLOORS[_other(arch)]}" not in json.dumps(steps)
    assert gb._build_env() == gb._build_env("arm64"), "the arm64 bundle's, as before"
    # Wheels too: pip then refuses, up front, a wheel built for a newer macOS
    # than the bundle's, rather than verify-bundle finding it in the app.
    assert gb.wheel_target("x86_64")[:2] == ("--platform", "macosx_13_0_x86_64")
    assert gb.WHEEL_TARGET[:2] == ("--platform", "macosx_14_0_arm64")
    with pytest.raises(gb.BundleError, match="ppc"):
        gb.macos_min("ppc")
    source = (REPO / "scripts" / "graft_bundle.py").read_text()
    assert "15.0" not in re.sub(r'"MinimumOSVersion"[^\n]*', "", source), "a macOS 15 target is left in the stage"


def _other(arch: str) -> str:
    return next(a for a in gb.ARCHS if a != arch)


class _Built(Exception):
    """Stops a builder at its first build step, once it is recorded."""


@pytest.mark.parametrize("arch", ["arm64", "x86_64"])
def test_every_native_build_runs_in_its_bundle_s_environment(tmp_path, monkeypatch, arch):
    """MACOSX_DEPLOYMENT_TARGET too, not only the flags: a compile a
    Makefile or configure runs without CFLAGS takes the environment's."""
    ran: list = []

    def build_steps(steps, cwd, arch=None):
        ran.append(arch)
        raise _Built

    def cached(name, key, work, build):
        (tmp_path / name).mkdir(exist_ok=True)
        try:
            build(tmp_path / name, tmp_path / name / "out")
        except _Built:
            pass
        return tmp_path / name

    def unpack(pin, tmp):
        (tmp / "src").mkdir(parents=True, exist_ok=True)
        return tmp / "src"

    with monkeypatch.context() as m:
        m.setattr(gb, "_build_steps", build_steps)
        m.setattr(gb, "_cached_build", cached)
        m.setattr(gb, "_unpack", unpack)
        m.setattr(gb, "apply_patches", lambda name, src, p=None: None)
        m.setattr(gb, "check_archive_commit", lambda pin, archive: None)
        m.setattr(gb, "fetch", lambda pin, cache=None: tmp_path / pin.filename)
        m.setattr(gb, "toolchain_id", lambda: "clang 1; SDK 15.0")
        m.setattr(gb.shutil, "copytree", lambda src, dst, **k: Path(dst).mkdir(parents=True, exist_ok=True))
        m.setattr(gb.shutil, "copy2", lambda *a, **k: None)
        pins = gb.load_pins(REPO / "bundle" / "pins.toml", arch)
        zsign = gb.Pin(**{**pins["zsign"].__dict__, "from_source": True})
        gb.build_openssl(pins["openssl"], tmp_path, arch)
        gb.build_libplist(pins["libplist"], tmp_path, arch)
        gb.build_ldid(pins["ldid"], tmp_path / "libplist", tmp_path / "openssl" / "prefix", tmp_path, arch)
        gb.build_cctools(pins["cctools"], tmp_path, arch)
        gb.build_insert_dylib(pins["insert_dylib"], tmp_path, arch)
        gb.build_zsign(zsign, tmp_path / "openssl" / "prefix", tmp_path, arch)
    assert ran == [arch] * 6
    # The real _build_steps and launchers, with only subprocess.run stood in.
    runs: list = []
    monkeypatch.setattr(gb.subprocess, "run", lambda argv, env=None, **kw: runs.append((argv, env)))
    gb._build_steps([["make"]], tmp_path, arch)
    gb.build_launchers(tmp_path / "Contents", arch)
    assert len(runs) == 1 + len(gb.LAUNCHERS)
    for argv, env in runs:
        assert env["MACOSX_DEPLOYMENT_TARGET"] == FLOORS[arch], argv
    for argv, _ in runs[1:]:
        assert f"-mmacosx-version-min={FLOORS[arch]}" in argv
    go = gb.go_env(tmp_path / "go", tmp_path, gb.ipatool_env(arch), tmp_path / "mod", arch)
    assert go["MACOSX_DEPLOYMENT_TARGET"] == FLOORS[arch] and go["GOARCH"] == gb.GOARCH[arch]


def _build_keys(tmp_path: Path, monkeypatch, arch: str) -> dict[str, str]:
    """Each native build's cache key for `arch`, as its builder computes it
    (nothing is built)."""
    keys: dict[str, str] = {}

    def cached(name, key, work, build):
        keys[name] = key
        return tmp_path / f"{name}-{key[:12]}"

    with monkeypatch.context() as m:
        m.setattr(gb, "_cached_build", cached)
        m.setattr(gb, "toolchain_id", lambda: "clang 1; SDK 15.0")
        pins = gb.load_pins(REPO / "bundle" / "pins.toml", arch)
        zsign = gb.Pin(**{**pins["zsign"].__dict__, "from_source": True})
        openssl = gb.build_openssl(pins["openssl"], tmp_path, arch)
        plist = gb.build_libplist(pins["libplist"], tmp_path, arch)
        gb.build_ldid(pins["ldid"], plist, openssl, tmp_path, arch)
        gb.build_cctools(pins["cctools"], tmp_path, arch)
        gb.build_insert_dylib(pins["insert_dylib"], tmp_path, arch)
        gb.build_zsign(zsign, openssl, tmp_path, arch)
        keys["ipatool"] = gb.ipatool_key(pins["ipatool"], pins["go"], arch)
    return keys


def test_a_build_cached_for_another_minimum_macos_is_never_reused(tmp_path, monkeypatch):
    """Each key covers its bundle's floor: an x86_64 build cached when the
    Intel floor was 14.0 is a different key now, for every native build;
    and the Intel floor is no part of the arm64 builds' keys."""
    intel, arm = _build_keys(tmp_path, monkeypatch, "x86_64"), _build_keys(tmp_path, monkeypatch, "arm64")
    assert sorted(intel) == ["cctools", "insert_dylib", "ipatool", "ldid", "libplist", "openssl", "zsign"]
    monkeypatch.setitem(gb.MACOS_MINS, "x86_64", "14.0")
    intel_at_14 = _build_keys(tmp_path, monkeypatch, "x86_64")
    assert [name for name in intel if intel[name] == intel_at_14[name]] == []
    assert _build_keys(tmp_path, monkeypatch, "arm64") == arm


@needs_toolchain  # it compiles a C file for each bundle
def test_a_real_compile_with_each_bundle_s_flags_needs_only_its_floor(tmp_path):
    """Through clang and the repo's own Mach-O reader: what target(arch)
    compiles in _build_env(arch) loads on that bundle's macOS and no older
    one says otherwise."""
    (tmp_path / "x.c").write_text("int main(void) { return 0; }\n")
    for arch, floor in FLOORS.items():
        out = tmp_path / f"x-{arch}"
        subprocess.run([gb.CLANG, *gb.target(arch), "-o", str(out), str(tmp_path / "x.c")],
                       env=gb._build_env(arch), check=True, capture_output=True)
        assert macho.architectures(out) == [arch]
        want = (*map(int, floor.split(".")), 0)
        assert macho.build_versions(out) == [(macho.PLATFORM_MACOS, want)], arch


@needs_toolchain  # it compiles bundle/launcher.c
def test_the_intel_bundle_s_launchers_load_on_macos_13(tmp_path):
    for arch, floor in FLOORS.items():
        contents = tmp_path / arch / "Contents"
        gb.build_launchers(contents, arch)
        want = (*map(int, floor.split(".")), 0)
        for rel in gb.LAUNCHERS:
            assert macho.architectures(contents / rel) == [arch]
            assert macho.build_versions(contents / rel) == [(macho.PLATFORM_MACOS, want)], (arch, rel)


def test_the_app_project_targets_one_of_the_bundles_floors():
    """The release passes each bundle's own floor to xcodebuild
    (scripts/graft-release), so the project's is a Debug build's: 14.0, or
    13.0 once the app's Swift builds for Ventura."""
    project = (REPO / "Graft" / "project.yml").read_text()
    (said,) = re.findall(r'macOS:\s*"([0-9.]+)"', project)
    pbx = (REPO / "Graft" / "Graft.xcodeproj" / "project.pbxproj").read_text()
    targets = set(re.findall(r"MACOSX_DEPLOYMENT_TARGET = ([0-9.]+);", pbx))
    assert targets == {said} and said in gb.MACOS_MINS.values(), (said, targets)


# --- A dropped network doesn't end a release build (2026-11-22: try10 lost
# two attempts to a DNS outage and a PyPI TLS timeout) ---------------------


class _Body(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_transient_network_error_is_retried_then_succeeds(monkeypatch):
    import urllib.error

    calls, sleeps = [], []
    answers = [urllib.error.URLError("nodename nor servname provided"), TimeoutError("timed out"), _Body(b"ok")]

    def urlopen(req, timeout):
        calls.append((req.full_url, timeout))
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(gb.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(gb.time, "sleep", sleeps.append)
    with gb._urlopen("https://example.invalid/x", timeout=7) as r:
        assert r.read() == b"ok"
    assert [t for _, t in calls] == [7, 7, 7]
    assert len(sleeps) == 2 and all(s > 0 for s in sleeps)


def test_an_http_error_is_not_retried(monkeypatch):
    import urllib.error

    calls = []

    def urlopen(req, timeout):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", None, None)

    monkeypatch.setattr(gb.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(gb.time, "sleep", lambda s: pytest.fail("an HTTP answer is final"))
    with pytest.raises(urllib.error.HTTPError):
        gb._urlopen("https://example.invalid/x", timeout=7)
    assert len(calls) == 1


def test_the_last_network_error_is_raised_after_the_retries(monkeypatch):
    import urllib.error

    calls = []

    def urlopen(req, timeout):
        calls.append(1)
        raise urllib.error.URLError("down")

    monkeypatch.setattr(gb.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(gb.time, "sleep", lambda s: None)
    with pytest.raises(urllib.error.URLError):
        gb._urlopen("https://example.invalid/x", timeout=7)
    assert len(calls) == gb.NETWORK_ATTEMPTS


def test_pip_retries_and_waits_longer_than_its_defaults():
    flags = list(gb._QUIET_PIP)
    assert flags[flags.index("--retries") + 1] == "10"
    assert flags[flags.index("--timeout") + 1] == "60"


# --- review 2.0, bundle #5: the build cache's keys cover the builder's own code ---------------


def test_every_build_key_covers_the_builders_own_code(monkeypatch):
    """What graft_bundle.py does with a build after it runs (vendor.tar.gz,
    the module notices, its checks) is part of the cached product, so a
    change to the file is a new build, never a stale one reused."""
    assert gb.BUILDER == hashlib.sha256((REPO / "scripts" / "graft_bundle.py").read_bytes()).hexdigest()
    key = gb.build_key("a source's hash", ["a", "step"])
    assert gb.build_key("a source's hash", ["a", "step"]) == key
    monkeypatch.setattr(gb, "BUILDER", "0" * 64)
    assert gb.build_key("a source's hash", ["a", "step"]) != key


def test_a_build_cached_before_the_builder_changed_is_not_reused(tmp_path, monkeypatch):
    built = []

    def build(tmp, out):
        built.append(1)
        (out / "x").write_text("built")
    first = gb._cached_build("thing", gb.build_key("same inputs"), tmp_path, build)
    assert gb._cached_build("thing", gb.build_key("same inputs"), tmp_path, build) == first and built == [1]
    monkeypatch.setattr(gb, "BUILDER", "1" * 64)
    assert gb._cached_build("thing", gb.build_key("same inputs"), tmp_path, build) != first and built == [1, 1]


# --- review 2.0, bundle #6: the tools the stage runs, by path, in the clean environment ---------


def test_the_launchers_and_the_macho_tools_run_by_path_in_the_build_environment(tmp_path, monkeypatch):
    """A Homebrew LLVM first on the release shell's PATH, or its CPATH,
    SDKROOT or CCC_OVERRIDE_OPTIONS, must not reach clang (the launchers),
    install_name_tool, lipo or nm."""
    calls = []

    def run(argv, **kw):
        calls.append(([str(a) for a in argv], kw.get("env")))
        return subprocess.CompletedProcess(argv, 0, "arm64\n" if "-archs" in argv else "", "")

    monkeypatch.setattr(gb.subprocess, "run", run)
    monkeypatch.setenv("PATH", "/opt/homebrew/opt/llvm/bin:" + os.environ.get("PATH", ""))
    for var in ("CPATH", "SDKROOT", "CCC_OVERRIDE_OPTIONS"):
        monkeypatch.setenv(var, "from the release shell")
    binary = tmp_path / "f"
    binary.write_bytes(mb.thin([]))
    gb.build_launchers(tmp_path / "Contents")
    gb._run(gb.INSTALL_NAME_TOOL, "-id", "@rpath/x", binary)
    gb.imported_symbols(binary)
    gb.defined_symbols([binary])
    gb._archs(binary)
    tools = [argv[0] for argv, _ in calls]
    assert tools == ["/usr/bin/clang"] * len(gb.LAUNCHERS) + [
        "/usr/bin/install_name_tool", "/usr/bin/nm", "/usr/bin/nm", "/usr/bin/lipo"]
    assert (gb.CLANG, gb.INSTALL_NAME_TOOL, gb.NM, gb.LIPO) == (
        "/usr/bin/clang", "/usr/bin/install_name_tool", "/usr/bin/nm", "/usr/bin/lipo")
    for argv, env in calls:
        assert env == gb._build_env(), argv
        assert not {"CPATH", "SDKROOT", "CCC_OVERRIDE_OPTIONS"} & set(env) and "homebrew" not in env["PATH"]


def test_no_tool_is_run_by_a_bare_name_outside_the_build_environment():
    """Every `_run` names its tool by path; `nm`, `lipo` and `clang` are never
    looked up on the release shell's PATH."""
    source = (REPO / "scripts" / "graft_bundle.py").read_text()
    assert not re.findall(r'_run\("', source)
    assert not re.findall(r'subprocess\.run\(\[\s*"(nm|lipo|clang|install_name_tool)"', source)


# --- review 2.0, bundle #9: a copyleft-only crate or module anywhere in the app is caught -------

GPL_TEXT = ("GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n\nCopyright (C) 2007 Free Software Foundation, "
            "Inc. Everyone is permitted to copy and distribute verbatim copies of this license document.\n")
LGPL_TEXT = ("This software is licensed under the LGPLv3, included below.\n\n"
             "GNU LESSER GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n\nThis version of the GNU Lesser General "
             "Public License incorporates the terms and conditions of version 3 of the GNU General Public License.\n")
MPL_TEXT = "Mozilla Public License Version 2.0\n==================================\n\n1. Definitions\n" + MIT_TEXT


def _qh3_with_crate(tmp_path: Path, monkeypatch, sbom_licence: str, cargo_licence: str = "") -> Path:
    """A staged qh3 whose SBOM lists one crate, dep 1.0.0, under `sbom_licence`
    (none when empty), and whose crate states `cargo_licence` in Cargo.toml."""
    contents = tmp_path / "Contents"
    dist = contents / "Resources/GraftPython/lib/python3.13/site-packages/qh3-1.9.4.dist-info"
    (dist / "sboms").mkdir(parents=True)
    (dist / "METADATA").write_text("Name: qh3\nVersion: 1.9.4\n")
    dep = {"name": "dep", "version": "1.0.0", "purl": "pkg:cargo/dep@1.0.0"}
    if sbom_licence:
        dep["licenses"] = [{"expression": sbom_licence}]
    (dist / "sboms/qh3.cyclonedx.json").write_text(json.dumps({"components": [
        dep, {"name": "qh3", "version": "1.9.4", "purl": "pkg:cargo/qh3@1.9.4"}]}))
    files = {"LICENSE": MIT_TEXT.encode()}
    if cargo_licence:
        files["Cargo.toml"] = f'[package]\nname = "dep"\nversion = "1.0.0"\nlicense = "{cargo_licence}"\n'.encode()
    crate, checksum = _crate(tmp_path / "dep.crate", "dep", "1.0.0", files)
    lock = f'[[package]]\nname = "dep"\nversion = "1.0.0"\nchecksum = "{checksum}"\n'
    sdist = _tarball(tmp_path / "qh3-1.9.4.tar.gz", {"qh3-1.9.4/Cargo.lock": lock.encode()})
    monkeypatch.setattr(gb, "fetch", lambda pin, cache=None: {"qh3": sdist, "dep": crate}[pin.name])
    monkeypatch.setattr(gb, "sdist_pin", lambda name, version, lic, locked: gb.Pin("qh3", "https://x/qh3.tar.gz",
                                                                                  "a" * 64, ""))
    return contents


@pytest.mark.parametrize("sbom, cargo, refused", [
    ("MIT OR Apache-2.0", "", False), ("Apache-2.0 OR GPL-2.0-only", "", False),
    ("MPL-2.0", "", True), ("LGPL-3.0-only", "", True),
    ("", "GPL-3.0-only", True), ("", "MIT/Apache-2.0", False),
    # d14-bundle-3: a copyleft license ANDed beside an OR is still mandatory.
    ("(MIT OR Apache-2.0) AND MPL-2.0", "", True), ("", "LGPL-2.1-only AND (MIT OR Apache-2.0)", True),
])
def test_a_wheels_rust_crate_with_no_permissive_choice_is_refused(tmp_path, monkeypatch, sbom, cargo, refused):
    """Its source would have to ship with the app, and the source archive
    carries only the wheel's sdist, not the crates it was built from."""
    contents = _qh3_with_crate(tmp_path, monkeypatch, sbom, cargo)
    if refused:
        with pytest.raises(gb.BundleError, match=r"qh3 1\.9\.4.*dep 1\.0\.0 \(.*(MPL|LGPL|GPL)"):
            gb.rust_crate_licences(contents, {}, tmp_path / "rust-crates")
    else:
        assert gb.rust_crate_licences(contents, {}, tmp_path / "rust-crates") == {"qh3": ["qh3/dep@1.0.0"]}


@pytest.mark.parametrize("lock_licence, refused", [("MPL-2.0", False), ("", True)])
def test_a_core_crate_whose_cargo_toml_is_copyleft_needs_its_source_archived(tmp_path, monkeypatch, lock_licence,
                                                                            refused):
    """ipatool's core crates go into the source archive by the lock's
    license (core_crate_sources). One whose lock entry states none, but whose
    own Cargo.toml is copyleft-only, would have been left out."""
    crates = {"nom-supreme-0.6.0": _crate(tmp_path / "nom-supreme-0.6.0.crate", "nom-supreme", "0.6.0", {
        "LICENSE": MPL_TEXT.encode(),
        "Cargo.toml": b'[package]\nname = "nom-supreme"\nversion = "0.6.0"\nlicense = "MPL-2.0"\n'})}
    monkeypatch.setattr(gb, "fetch", lambda pin, cache=None: crates[f"{pin.name}-{pin.version}"][0])
    ipatool = tmp_path / "ipatool"
    ipatool.write_bytes(b"go code\0/usr/local/cargo/registry/src/index.crates.io-1949cf8c6b5b557f/"
                        b"nom-supreme-0.6.0/src/x.rs")
    lock = _core_lock(tmp_path / "core-crates.lock", crates, licence=lock_licence)
    if refused:
        with pytest.raises(gb.BundleError, match=r"nom-supreme 0\.6\.0 \(MPL-2\.0\)"):
            gb.binary_crate_licences(ipatool, "ipatool", tmp_path / "rust-crates", lock)
    else:
        assert gb.binary_crate_licences(ipatool, "ipatool", tmp_path / "rust-crates", lock) == [
            "ipatool/nom-supreme@0.6.0"]
        assert [c for c, _, _ in gb.core_crate_sources(lock)] == ["nom-supreme"], "its source is archived"


@pytest.mark.parametrize("text, family", [(GPL_TEXT, "GPL"), (LGPL_TEXT, "LGPL"), (MPL_TEXT, "MPL"),
                                          ("GNU AFFERO GENERAL PUBLIC LICENSE\nVersion 3\n" + MIT_TEXT, "AGPL"),
                                          (MIT_TEXT, None)])
def test_a_licence_texts_copyleft_family_is_read_from_its_title(text, family):
    assert gb.licence_text_family(text) == family


def test_a_go_module_under_a_copyleft_licence_its_tool_isnt_listed_under_is_refused(tmp_path):
    """Go modules carry no SPDX license: the text tells. Every module's source
    is archived (go_module_problems), so a copyleft one is shipped right, but
    the license components.json gives its tool must name it."""
    notices = _notices(tmp_path / "modules", ["example.com/lib@v1.0.0"], text=GPL_TEXT)
    _notices(notices, ["example.com/mit@v1.0.0"])
    assert gb.go_copyleft_problems(notices, "MIT AND BSD-3-Clause") == [
        "example.com/lib@v1.0.0: GPL, which its tool's license (MIT AND BSD-3-Clause) doesn't name"]
    assert gb.go_copyleft_problems(notices, "MIT AND GPL-3.0-only") == []
    assert gb.go_copyleft_problems(notices, "MIT AND LGPL-3.0-only") != [], "LGPL is not the GPL"
    (notices / "example.com/lib@v1.0.0" / "LICENSE-MIT").write_text(MIT_TEXT)
    assert gb.go_copyleft_problems(notices, "MIT AND BSD-3-Clause") == [], "dual-licensed: a permissive choice"


@pytest.mark.parametrize("name", ["NOTICE", "COPYRIGHT", "PATENTS", "LEGAL.txt"])
def test_a_notice_beside_a_copyleft_licence_offers_no_choice(tmp_path, name):
    """The delta audit's d14-bundle-2: in a Go module, a NOTICE, COPYRIGHT or
    PATENTS file adds to its license. Any licence-like text there used to
    count as a permissive choice, so a GPL-only module with one passed."""
    notices = _notices(tmp_path / "modules", ["example.com/gplmod@v1.0.0"], text=GPL_TEXT)
    (notices / "example.com/gplmod@v1.0.0" / name).write_text(
        "Copyright 2026 Example Authors. Parts of this module carry their own copyright notices. " * 3)
    assert gb.go_copyleft_problems(notices, "MIT AND BSD-3-Clause") == [
        "example.com/gplmod@v1.0.0: GPL, which its tool's license (MIT AND BSD-3-Clause) doesn't name"]


def test_a_licence_file_is_a_choice_only_when_its_text_is_a_permissive_licence(tmp_path):
    notices = _notices(tmp_path / "modules", ["example.com/gplmod@v1.0.0"], text=GPL_TEXT)
    other = notices / "example.com/gplmod@v1.0.0" / "LICENSE-OTHER"
    other.write_text("A license nobody here recognizes, whose copyright holder grants some permission. " * 3)
    assert gb.go_copyleft_problems(notices, "MIT") != [], "a text nothing recognizes can't be called permissive"
    other.write_text("Copyright 2026 Example Authors.\n\nRedistribution and use in source and binary forms, with or "
                     "without modification, are permitted provided that the following conditions are met.\n")
    assert gb.permissive_text(other.read_text()) == "BSD"
    assert gb.go_copyleft_problems(notices, "MIT") == []
    assert gb.permissive_text(MPL_TEXT) is None, "a copyleft text quoting permissive words is still copyleft"


def test_a_copyleft_title_after_a_long_preamble_is_found_and_one_named_in_passing_is_not():
    preamble = "This module is maintained by Example Authors and offered under the license that follows. " * 12
    assert len(preamble) > 800
    assert gb.licence_text_family(preamble + "\n\n" + GPL_TEXT) == "GPL"
    assert gb.licence_text_family(preamble + "\n\n" + LGPL_TEXT) == "LGPL"
    assert gb.licence_text_family(preamble + "\n\n" + MPL_TEXT) == "MPL"
    assert gb.licence_text_family(preamble + "\nYou may also ask us about the GNU General Public License.\n") is None
    # MPL-2.0's own text names the GPL family in its body, wrapped across lines.
    mpl = MPL_TEXT + "x " * 500 + ("\n1.12. \"Secondary License\"\n    means either the GNU General Public License, "
                                   "Version 2.0, the GNU\n    Lesser General Public License, Version 2.1, the GNU "
                                   "Affero General\n    Public License, Version 3.0\n")
    assert gb.licence_text_family(mpl) == "MPL"


def test_a_tools_licence_must_name_a_copyleft_module_in_every_choice_it_offers(tmp_path):
    notices = _notices(tmp_path / "modules", ["example.com/lib@v1.0.0"], text=GPL_TEXT)
    assert gb.go_copyleft_problems(notices, "MIT OR GPL-3.0-only") == [
        "example.com/lib@v1.0.0: GPL, which its tool's license (MIT OR GPL-3.0-only) names only as one choice"]
    assert gb.go_copyleft_problems(notices, "(MIT OR Apache-2.0) AND GPL-3.0-only") == []
    assert gb.licence_names("GPL", "MIT AND (GPL-2.0-only OR GPL-3.0-or-later)")
    assert not gb.licence_names("GPL", "MIT AND (GPL-3.0-only"), "an expression that doesn't parse names nothing"


def test_ipatools_real_copyleft_modules_are_ones_its_licence_names():
    """golang-lru (MPL-2.0) and retry.v1 (LGPL-3.0) are named by ipatool's pin."""
    licence = gb.load_pins(REPO / "bundle" / "pins.toml")["ipatool"].license
    assert gb.licence_names("MPL", licence) and gb.licence_names("LGPL", licence)
    assert not gb.licence_names("GPL", licence) and not gb.licence_names("AGPL", licence)


def test_staging_a_go_tool_whose_copyleft_module_its_licence_doesnt_name_is_refused(tmp_path, monkeypatch):
    built = tmp_path / "built"
    _notices(built / "modules", ["example.com/lib@v1.0.0"], text=GPL_TEXT)
    (built / "unfairplay").write_bytes(mb.thin([]))
    (built / "ipatool").write_bytes(mb.thin([]))
    for f in (*gb.UNFAIRPLAY_LICENCES, "go-LICENSE", "go-PATENTS"):
        (built / f).write_text(f)
    monkeypatch.setattr(gb, "build_unfairplay", lambda pin, go, work=None: built)
    monkeypatch.setattr(gb, "build_ipatool", lambda pin, go, work=None, arch="arm64": built)
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    for tool, stage in (("unfairplay", gb.stage_unfairplay), ("ipatool", gb.stage_ipatool)):
        licences = tmp_path / f"{tool}-licences"
        licences.mkdir()
        with pytest.raises(gb.BundleError, match=rf"{tool}.*example\.com/lib@v1\.0\.0: GPL"):
            stage(pins, tmp_path / f"{tool}-Contents", licences, tmp_path / "work")


# --- 2.0.1: one download cache per user, shared Go modules, and a phone's hotspot ---------------


def test_downloads_are_cached_per_user_outside_every_checkout(monkeypatch):
    """A cache in the checkout started empty in every new worktree, and a
    release from one fetched some 350 MB again. Builds stay per checkout."""
    monkeypatch.delenv("GRAFT_BUNDLE_CACHE", raising=False)
    monkeypatch.delenv("GRAFT_BUNDLE_WORK", raising=False)
    downloads, builds = gb.cache_dirs()
    assert downloads == Path.home() / "Library" / "Caches" / "graft-release" / "bundle-cache"
    assert not downloads.is_relative_to(REPO)
    assert builds == REPO / "build" / "bundle-work"


def test_two_releases_fetching_one_file_at_once_each_keep_their_own_partial_file(tmp_path, monkeypatch):
    """The cache is shared between checkouts; one partial file name for every
    fetch let two downloads write into one file."""
    body = b"pinned bytes"
    pin = gb.Pin(name="x", url="https://example.invalid/x.tar.gz", sha256=hashlib.sha256(body).hexdigest(),
                 license="MIT")
    parts = []

    def download(url, dest):
        parts.append(dest)
        if len(parts) == 1:
            gb.fetch(pin, tmp_path)  # another release, mid-download
        dest.write_bytes(body)

    monkeypatch.setattr(gb, "_download", download)
    assert gb.fetch(pin, tmp_path).read_bytes() == body
    assert len(set(parts)) == 2 and all(p.parent == tmp_path for p in parts)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.tar.gz"]
    assert (tmp_path / "x.tar.gz").stat().st_mode & 0o777 == 0o644


def test_go_modules_are_vendored_then_verified_then_built(tmp_path):
    """Go checks a cached module only by the hash it recorded at download;
    with the cache shared, `go mod verify` re-hashes the zips and unpacked
    files before anything is built from them."""
    ipatool = gb.ipatool_steps("2.8.0")
    unfairplay = gb.unfairplay_steps("0.4.0", "https://example.invalid/u.git", "0" * 40)
    for steps in (ipatool, unfairplay):
        at = steps.index(["go", "mod", "vendor"])
        assert steps[at + 1] == ["go", "mod", "verify"] == gb.GO_VERIFY
        assert steps[at + 2][:2] == ["go", "build"] and "-mod=vendor" in steps[at + 2]
    assert gb.GO_HERMETIC["GOFLAGS"] == "" and gb.GO_HERMETIC["GOPROXY"] == gb.GOPROXY
    assert gb.GO_HERMETIC["GOSUMDB"] == gb.GOSUMDB and gb.GO_HERMETIC["GONOSUMDB"] == ""


def test_go_shares_its_module_cache_and_nothing_else(tmp_path):
    go = tmp_path / "go"
    (tmp_path / "build").mkdir()
    env = gb.go_env(go, tmp_path / "build", gb.UNFAIRPLAY_ENV, tmp_path / "shared" / "mod")
    assert env["GOMODCACHE"] == str(tmp_path / "shared" / "mod") and (tmp_path / "shared" / "mod").is_dir()
    for var in ("GOPATH", "GOCACHE", "HOME"):
        assert Path(env[var]).parent == tmp_path / "build", var
    assert gb.go_env(go, tmp_path / "build", gb.UNFAIRPLAY_ENV)["GOMODCACHE"] == str(gb.go_modcache())
    assert gb.go_modcache() == gb.CACHE / "go-modcache"


def test_a_go_build_records_the_modules_it_fetched_for_the_next_releases_plan(tmp_path):
    modcache = tmp_path / "mod"
    pin = gb.Pin(name="ipatool", url="https://example.invalid/i.tar.gz", sha256="a" * 64, license="MIT")
    txt = tmp_path / "modules.txt"
    txt.write_text("# example.com/A v1.0.0\n## explicit\nexample.com/A\n# example.com/only/required v0.1.0\n"
                   "## explicit\n# example.com/old v1.0.0 => example.com/new v1.1.0\nexample.com/old\n")
    for module, version in (("example.com/!a", "v1.0.0"), ("example.com/new", "v1.1.0")):
        z = modcache / "cache" / "download" / module / "@v" / f"{version}.zip"
        z.parent.mkdir(parents=True)
        z.write_bytes(b"zip")
    assert not gb.go_modules_cached(modcache, pin)
    gb.record_go_modules(modcache, pin, txt)
    record = gb._go_modules_record(modcache, pin)
    assert record.read_text() == "example.com/A v1.0.0\nexample.com/new v1.1.0\n", \
        "a requirement no package came from is never downloaded, so isn't recorded"
    assert gb.go_modules_cached(modcache, pin)
    assert not gb.go_modules_cached(modcache, gb.Pin(**{**pin.__dict__, "sha256": "b" * 64})), "another source"
    (modcache / "cache" / "download" / "example.com/new" / "@v" / "v1.1.0.zip").unlink()
    assert not gb.go_modules_cached(modcache, pin), "a module gone from the cache since"


def test_a_build_product_naming_the_download_cache_is_refused(tmp_path):
    def build(tmp, out):
        (out / "tool").write_bytes(b"built from " + str(gb.CACHE / "go-modcache" / "x").encode())
    with pytest.raises(gb.BundleError, match="download cache's path in tool"):
        gb._cached_build("leaky", "k" * 12, tmp_path / "work", build)


def _plan_world(tmp_path, monkeypatch):
    """Pins, locks and caches for download_plan, all made up."""
    monkeypatch.setattr(gb, "toolchain_id", lambda: "Apple clang version 99, macOS SDK 99.0")
    cache, work = tmp_path / "cache", tmp_path / "work"
    (cache / "crates").mkdir(parents=True)
    work.mkdir()

    def pin(name, body, url=None):
        return gb.Pin(name=name, url=url or f"https://example.invalid/{name}.tar.gz",
                      sha256=hashlib.sha256(body).hexdigest(), license="MIT", version="1.0")
    commit = "c" * 40
    pins = {"go": pin("go", b"go"), "openssl": pin("openssl", b"openssl"),
            "ipatool": pin("ipatool", b"ipatool"),
            "unfairplay": pin("unfairplay", b"u", f"https://example.invalid/o/u/archive/{commit}.tar.gz")}
    (cache / "go.tar.gz").write_bytes(b"go")
    (cache / "openssl.tar.gz").write_bytes(b"stale bytes")
    (cache / f"{commit}.tar.gz").write_bytes(b"u")
    lock = tmp_path / "requirements.lock"
    lock.write_text("aaa-pkg==1.0 \\\n    --hash=sha256:" + "1" * 64 + "\nbbb==2.0 \\\n    --hash=sha256:" + "2" * 64
                    + "\nccc==3.0 \\\n    --hash=sha256:" + "3" * 64 + "\n")
    (cache / "aaa_pkg-1.0.tar.gz").write_bytes(b"sdist")
    (cache / "bbb-2.0.zip").write_bytes(b"sdist")
    crates = tmp_path / "core-crates.lock"
    crates.write_text('[[package]]\nname = "ring"\nversion = "0.17.0"\nchecksum = "' + "4" * 64 + '"\n'
                      '[[package]]\nname = "nom"\nversion = "7.1.0"\nchecksum = "' + "5" * 64 + '"\n')
    (cache / "crates" / "ring-0.17.0.crate").write_bytes(b"crate")
    return pins, cache, work, lock, crates


def test_the_plan_lists_only_what_isnt_cached_without_touching_the_network(tmp_path, monkeypatch):
    pins, cache, work, lock, crates = _plan_world(tmp_path, monkeypatch)
    monkeypatch.setattr(gb, "_urlopen", lambda *a, **k: pytest.fail("the plan went to the network"))
    build_lock = tmp_path / "build-requirements.lock"
    build_lock.write_text("bbb==2.0 \\\n    --hash=sha256:" + "2" * 64 + "\nddd==4.0 \\\n    --hash=sha256:" + "6" * 64 + "\n")
    plan = gb.download_plan(pins, cache, work, lock, crates, build_lock)
    assert [(d.what, d.mb, d.kept) for d in plan] == [
        ("openssl 1.0", 53, True),
        ("ipatool 1.0", 6, True),
        ("ipatool's Go modules", 100, True),
        ("unfairplay's Go modules", 1, True),
        ("1 source distribution(s) for the source archive: ccc 3.0", 1.0, True),
        ("1 Rust crate(s) for their licenses", 0.15, True),
        ("4 Python wheel(s) the app bundles: aaa-pkg 1.0, bbb 2.0, ccc 3.0, ddd 4.0", 4.0, True)]


def test_go_modules_a_cached_build_or_an_earlier_one_fetched_are_not_planned(tmp_path, monkeypatch):
    pins, cache, work, lock, crates = _plan_world(tmp_path, monkeypatch)
    (work / f"ipatool-{gb.ipatool_key(pins['ipatool'], pins['go'])[:12]}").mkdir()
    record = gb._go_modules_record(gb.go_modcache(cache), pins["unfairplay"])
    record.parent.mkdir(parents=True)
    record.write_text("")
    whats = [d.what for d in gb.download_plan(pins, cache, work, lock, crates)]
    assert "ipatool's Go modules" not in whats and "unfairplay's Go modules" not in whats


# --- 2.0.2: the locked wheels are kept in pip's own cache, per user --------------------------


def _pip_report(path: Path, files: list[tuple[str, str, str, str]]) -> Path:
    """A pip --report choosing (name, version, url, sha256) for each file."""
    path.write_text(json.dumps({"install": [
        {"metadata": {"name": n, "version": v}, "download_info": {"url": u, "archive_info": {"hashes": {"sha256": h}}}}
        for n, v, u, h in files]}))
    return path


def _cached_body(cache_dir: Path, url: str, data: bytes) -> Path:
    body = gb.pip_cache_body(cache_dir, url)
    body.parent.mkdir(parents=True, exist_ok=True)
    body.write_bytes(data)
    return body


def test_pips_cache_is_in_the_shared_downloads_cache():
    assert gb.pip_cache() == gb.CACHE / "pip"
    assert gb.default_download_cache() == Path.home() / "Library" / "Caches" / "graft-release" / "bundle-cache"


def test_a_cached_wheel_is_looked_for_where_pip_itself_keeps_it(tmp_path):
    """pip's own layout (http-v2/, keyed by the URL as cachecontrol
    normalizes it), from the pip in this venv: a layout that moved would
    only make the plan count wheels as still to download, but should be
    seen."""
    from pip._internal.network.cache import SafeFileCache
    from pip._vendor.cachecontrol.controller import CacheController

    url = "HTTPS://Files.PythonHosted.org/packages/ab/cd/x-1.0-py3-none-any.whl#sha256=" + "0" * 64
    pips = SafeFileCache(str(tmp_path / "http-v2"))._get_cache_path(CacheController.cache_url(url)) + ".body"
    assert gb.pip_cache_body(tmp_path, url) == Path(pips)
    assert gb.pip_cache_body(tmp_path, url) == gb.pip_cache_body(
        tmp_path, "https://files.pythonhosted.org/packages/ab/cd/x-1.0-py3-none-any.whl")


def test_a_locked_install_shares_pips_cache_keeps_every_hash_check_and_records_the_wheels(tmp_path, monkeypatch):
    """The plan's resolution (which fetches whole wheels in hash-checking
    mode) and the download share pip's cache; the install from the
    downloaded files needs none. --require-hashes stays on all three."""
    url = "https://files.pythonhosted.org/packages/aa/bb/x-1.0-py3-none-any.whl"
    calls = []

    def fake_run(argv, **kw):
        argv = [str(a) for a in argv]
        calls.append(argv)
        if "--dry-run" in argv:
            _cached_body(gb.pip_cache(), url, b"wheel bytes")
        if "--report" in argv:
            _pip_report(Path(argv[argv.index("--report") + 1]), [("X", "1.0", url, "a" * 64)])
    monkeypatch.setattr(gb.subprocess, "run", fake_run)
    gb.install_locked(tmp_path / "python3.13", gb.RUNTIME_LOCK, tmp_path / "plan.json", tmp_path / "s", {})
    resolve, download, install = calls
    assert "--dry-run" in resolve and "download" in download and "--no-index" in install
    for argv in (resolve, download):
        assert argv[argv.index("--cache-dir") + 1] == str(gb.CACHE / "pip") and "--no-cache-dir" not in argv
    for argv in calls:
        assert "--require-hashes" in argv and "--only-binary=:all:" in argv
    assert "--no-cache-dir" in install and "--cache-dir" not in install
    assert gb.cached_wheels() == {"a" * 64: {"name": "x", "version": "1.0", "url": url, "mb": 0.0}}


def test_a_failed_resolution_or_download_names_the_cache_to_delete(tmp_path, monkeypatch):
    """pip never replaces a cached file that fails its hash: the way out is
    deleting the cache, which the error names."""
    def fake_run(argv, **kw):
        if "download" in [str(a) for a in argv]:
            raise subprocess.CalledProcessError(1, argv)
    monkeypatch.setattr(gb.subprocess, "run", fake_run)
    with pytest.raises(gb.BundleError, match=rf"delete pip's cache, {re.escape(str(gb.CACHE / 'pip'))}, and build"):
        gb.install_locked(tmp_path / "python3.13", gb.RUNTIME_LOCK, tmp_path / "plan.json", tmp_path / "s", {})


def test_the_plan_counts_a_wheel_cached_only_while_pip_holds_one_the_lock_allows(tmp_path):
    lock = tmp_path / "requirements.lock"
    lock.write_text("aaa==1.0 \\\n    --hash=sha256:" + "1" * 64 + " \\\n    --hash=sha256:" + "7" * 64
                    + "\nbbb==2.0 \\\n    --hash=sha256:" + "2" * 64 + "\n")
    cache = tmp_path / "cache"
    pip = gb.pip_cache(cache)
    a, b = "https://files.example.invalid/aaa-1.0.whl", "https://files.example.invalid/bbb-2.0.whl"
    _cached_body(pip, a, b"a" * 2_000_000)
    _cached_body(pip, b, b"b")
    plan = _pip_report(tmp_path / "plan.json", [("aaa", "1.0", a, "7" * 64), ("bbb", "2.0", b, "9" * 64)])
    assert gb.record_cached_wheels(plan, pip) == 2
    assert gb.uncached_wheels((lock,), cache) == ["bbb 2.0"], "bbb's cached file isn't one its lock allows"
    assert gb.cached_wheels(pip)["7" * 64]["mb"] == 2.0
    gb.pip_cache_body(pip, a).unlink()
    assert gb.uncached_wheels((lock,), cache) == ["aaa 1.0", "bbb 2.0"], "gone from pip's cache since"
    (pip / gb.WHEEL_RECORD).write_text("not json")
    assert gb.cached_wheels(pip) == {}
    assert gb.uncached_wheels((lock,), tmp_path / "empty") == ["aaa 1.0", "bbb 2.0"]


ROUTE = """   route to: default
destination: default
       mask: default
    gateway: {gateway}
  interface: {interface}
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>
"""


@pytest.mark.parametrize("out, code, want", [
    (ROUTE.format(gateway="172.20.10.1", interface="en7"), 0, ("172.20.10.1", "en7")),
    (ROUTE.format(gateway="192.168.1.1", interface="en0"), 0, ("192.168.1.1", "en0")),
    ("route: writing to routing socket: not in table\n", 1, None)])
def test_the_default_route_is_read_from_route(monkeypatch, out, code, want):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, code, out, "")
    monkeypatch.setattr(gb.subprocess, "run", run)
    assert gb.default_route() == want
    assert calls == [["/sbin/route", "-n", "get", "default"]]


def _on(monkeypatch, gateway: str, plan: list) -> None:
    monkeypatch.setattr(gb, "default_route", lambda: (gateway, "en7"))
    monkeypatch.setattr(gb, "download_plan", lambda pins, *a, **kw: plan)
    monkeypatch.delenv("GRAFT_ALLOW_HOTSPOT", raising=False)


BIG = [gb.Download("go 1.26.5", 65), gb.Download("ipatool's Go modules", 100), gb.Download("x", None),
       gb.Download("the Python wheels", None, kept=False)]


def test_off_a_hotspot_nothing_is_planned_or_asked(monkeypatch, capsys):
    _on(monkeypatch, "192.168.1.1", BIG)
    monkeypatch.setattr(gb, "download_plan", lambda pins, *a, **kw: pytest.fail("planned off a hotspot"))
    assert gb.check_hotspot({}, interactive=True, answer=lambda: pytest.fail("asked"))
    assert capsys.readouterr().err == ""


def test_on_a_hotspot_with_only_the_wheels_left_it_says_so_and_goes_on(monkeypatch, capsys):
    _on(monkeypatch, gb.HOTSPOT_GATEWAY, BIG[3:])
    assert gb.check_hotspot({}, interactive=True, answer=lambda: pytest.fail("asked"))
    assert "hotspot" in capsys.readouterr().err


def test_on_a_hotspot_with_everything_cached_it_says_so_and_goes_on(monkeypatch, capsys):
    _on(monkeypatch, gb.HOTSPOT_GATEWAY, [])
    assert gb.check_hotspot({}, interactive=True, answer=lambda: pytest.fail("asked"))
    err = capsys.readouterr().err
    assert err.endswith("everything is cached\n") and " but " not in err


def test_on_a_hotspot_it_says_what_and_how_much_then_asks(monkeypatch, capsys):
    _on(monkeypatch, gb.HOTSPOT_GATEWAY, BIG)
    assert gb.check_hotspot({}, interactive=True, answer=lambda: "y\n")
    err = capsys.readouterr().err
    assert "172.20.10.1 on en7" in err and "65 MB  go 1.26.5" in err and "about 165 MB in all, plus 1 of a size" in err
    assert "[y/N]" in err
    assert not gb.check_hotspot({}, interactive=True, answer=lambda: "\n"), "no is the default"
    assert not gb.check_hotspot({}, interactive=True, answer=lambda: ""), "and end of input is no"


def test_on_a_hotspot_with_no_one_to_ask_it_stops_unless_allowed(monkeypatch, capsys):
    _on(monkeypatch, gb.HOTSPOT_GATEWAY, BIG)
    assert not gb.check_hotspot({}, interactive=False, answer=lambda: pytest.fail("asked"))
    assert "GRAFT_ALLOW_HOTSPOT=1" in capsys.readouterr().err
    monkeypatch.setenv("GRAFT_ALLOW_HOTSPOT", "1")
    assert gb.check_hotspot({}, interactive=False, answer=lambda: pytest.fail("asked"))


def test_the_hotspot_check_is_a_command_whose_status_says_go_on_or_stop(monkeypatch):
    monkeypatch.setattr(gb, "check_hotspot", lambda pins, **kw: False)
    assert gb.main(["hotspot-check"]) == 1
    monkeypatch.setattr(gb, "check_hotspot", lambda pins, **kw: True)
    assert gb.main(["hotspot-check"]) == 0


# --- the x86_64 stage: Graft-Intel.zip -------------------------------------------------------------

#: What the stage ran for arm64 at v2.2 (17fe3a8), every command, setting
#: and path, written out by the stage of that commit (`$TMP` and `$REPO` for
#: the two paths that were this machine's).
ARM64_STAGE = json.loads((REPO / "tests" / "fixtures" / "bundle" / "arm64-stage-2.2.json").read_text())
#: The two v2.2 records' SHA-256s. They are data, so writing them again would
#: quietly make any change pass: a change to either needs review, as a change
#: to the arm64 bundle, never a regenerated file.
STAGE_FIXTURES = {"arm64-stage-2.2.json": "bb921eb702f34c6ab77843bd83c6013e95e9a309cc9e06e8e80a9db3bda46252",
                  "arm64-stage-calls-2.2.json": "c12615fd153cc91089656dd55cdea2c273cadd4d3e2da5c5dbc9efa2f56c9d13"}
#: The two steps 2.2.1 runs after ipatool's build: its tests' mocks, then
#: the tests of the package its patch changes (IPATOOL_MOCKS, IPATOOL_TESTS).
IPATOOL_TEST_STEPS = [["/usr/bin/env", "GOPROXY=off", "GOFLAGS=-mod=vendor", "go", "generate", "-mod=vendor", "./pkg/..."],
                      ["/usr/bin/env", "GOPROXY=off", "GOFLAGS=-mod=vendor",
                       "go", "test", "-mod=vendor", "-count=1", "./pkg/appstore/..."]]
#: The arm64 stage now: v2.2's record and the one change made to it since,
#: stated here rather than written into the record.
ARM64_STAGE_NOW = {**ARM64_STAGE, "ipatool": [*ARM64_STAGE["ipatool"], *IPATOOL_TEST_STEPS]}


def _stage_commands(tmp_path: Path, monkeypatch, arch: str | None) -> dict:
    """The stage's commands, settings and paths, as ARM64_STAGE records them,
    for `arch` (None: each function called with no arch at all)."""
    a = {} if arch is None else {"arch": arch}
    pins = gb.load_pins(REPO / "bundle" / "pins.toml", arch or "arm64")
    calls = []
    monkeypatch.setattr(gb.subprocess, "run", lambda argv, **kw: calls.append([str(x) for x in argv]))
    contents = tmp_path / (arch or "default") / "Contents"
    gb.build_launchers(contents, **a)
    monkeypatch.undo()
    got = {
        "TARGET": list(gb.target(**a)),
        "openssl": gb.openssl_steps("$DESTDIR", ["-j8"], **a),
        "libplist": gb.libplist_steps("$DESTDIR", ["-j8"], **a),
        "ldid": gb.ldid_steps("$LIBPLIST", "$OPENSSL", ["-j8"], **a),
        "cctools": gb.cctools_steps(["-j8"], **a),
        "insert_dylib": gb.insert_dylib_steps(**a),
        "ipatool": gb.ipatool_steps("2.8.0"),
        "IPATOOL_ENV": gb.ipatool_env(**a),
        "WHEEL_TARGET": list(gb.wheel_target(**a)),
        "PRUNE": list(gb.prune_patterns(**a)),
        "CYAN_TOOLS": str(gb.cyan_tools(**a)),
        "stub": gb.stub_steps(),
        "launchers": [[x.replace(str(contents.parent), "$TMP").replace(str(REPO), "$REPO") for x in c] for c in calls],
        "ENTITLED": gb.ENTITLED,
    }
    if "unfairplay" in pins:
        got["unfairplay"] = gb.unfairplay_steps("0.4.0", *gb.source_commit(pins["unfairplay"]))
        got["UNFAIRPLAY_ENV"] = gb.UNFAIRPLAY_ENV
    return got


def test_the_arm64_stage_runs_exactly_what_2_2_did(tmp_path, monkeypatch):
    """Every build command, setting and path of the arm64 bundle, with arch
    left out and with arch="arm64", is v2.2's, as is each module constant;
    but for ipatool's tests, run after its build (ARM64_STAGE_NOW)."""
    assert _stage_commands(tmp_path, monkeypatch, None) == ARM64_STAGE_NOW
    assert _stage_commands(tmp_path, monkeypatch, "arm64") == ARM64_STAGE_NOW
    assert (list(gb.TARGET), gb.IPATOOL_ENV, list(gb.WHEEL_TARGET), list(gb.PRUNE), str(gb.CYAN_TOOLS)) == (
        ARM64_STAGE["TARGET"], ARM64_STAGE["IPATOOL_ENV"], ARM64_STAGE["WHEEL_TARGET"], ARM64_STAGE["PRUNE"],
        ARM64_STAGE["CYAN_TOOLS"])


#: How an arm64 command becomes its x86_64 one: nothing else may differ.
_TO_INTEL = [('"-arch", "arm64"', '"-arch", "x86_64"'), ("-arch arm64", "-arch x86_64"),
             ("darwin64-arm64-cc", "darwin64-x86_64-cc"), ("macosx_14_0_arm64", "macosx_13_0_x86_64"),
             ('"GOARCH": "arm64"', '"GOARCH": "amd64"'), ("Darwin/arm64", "Darwin/@"), ("Darwin/x86_64", "Darwin/arm64"),
             ("Darwin/@", "Darwin/x86_64"),
             # The Intel bundle's floor (MACOS_MINS): every compile, link and cgo flag.
             ("-mmacosx-version-min=14.0", "-mmacosx-version-min=13.0")]


def _intel(value):
    text = json.dumps(value)
    for old, new in _TO_INTEL:
        text = text.replace(old, new)
    out = json.loads(text)
    # The two autotools configures are told they cross-compile (AUTOTOOLS_HOST).
    for key in ("libplist", "cctools"):
        if isinstance(out, dict) and key in out:
            out[key][0].insert(2, "--host=x86_64-apple-darwin")
    return out


def test_the_x86_64_stage_runs_the_arm64_commands_for_x86_64_and_nothing_else(tmp_path, monkeypatch):
    """Each Mac binary is built for x86_64 (OpenSSL's darwin64-x86_64-cc,
    clang's -arch x86_64, Go's GOARCH=amd64, x86_64 wheels, cyan's
    Darwin/x86_64), for macOS 13.0, by the same commands; PRUNE
    drops Darwin/arm64 in place of Darwin/x86_64. PlayCover's stub is iOS
    code: arm64 in both. No UnfairPlay."""
    got = _stage_commands(tmp_path, monkeypatch, "x86_64")
    want = {k: v for k, v in ARM64_STAGE_NOW.items() if k not in ("unfairplay", "UNFAIRPLAY_ENV", "stub")}
    want = {**_intel(want), "stub": ARM64_STAGE["stub"]}
    assert got == want
    for key in ("TARGET", "openssl", "libplist", "ldid", "cctools", "insert_dylib", "IPATOOL_ENV", "WHEEL_TARGET",
                "PRUNE", "CYAN_TOOLS", "launchers"):
        assert got[key] != ARM64_STAGE[key], f"{key} is built for x86_64"
        assert "arm64" not in json.dumps(got[key]).replace("Darwin/arm64", ""), key
    assert got["stub"][0][:6] == ["xcrun", "-sdk", "iphoneos", "clang", "-arch", "arm64"], "iOS code, for the phone"
    assert "-mmacosx-version-min=13.0" in got["TARGET"]
    assert "14.0" not in json.dumps(got) and "14_0" not in json.dumps(got), "Ventura's, throughout"


def test_an_architecture_no_release_is_built_for_builds_nothing():
    for fn in (gb.target, gb.wheel_target, gb.prune_patterns):
        with pytest.raises(gb.BundleError, match="ppc"):
            fn("ppc")
    with pytest.raises(KeyError):
        gb.openssl_steps("$D", [], "ppc")


def test_zsign_builds_from_its_makefile_for_x86_64_on_the_bundled_openssl():
    """Upstream's build/macos/Makefile as its release runs it (make
    VERSION=1.1.3), with no pkg-config: the bundled OpenSSL by path, the
    target on CC and CXX (its zlib, minizip and link use nothing else), and
    room in the header for the @executable_path names."""
    assert gb.zsign_steps("$OPENSSL", ["-j8"], "1.1.3", "x86_64") == [[
        "make", "-j8", "-C", "build/macos", "VERSION=1.1.3",
        "CC=clang -arch x86_64 -mmacosx-version-min=13.0", "CXX=clang++ -arch x86_64 -mmacosx-version-min=13.0",
        "CXXFLAGS=-std=c++11 -O3 -MMD -MP -DZSIGN_VERSION=1.1.3", "OPENSSL_INCLUDE=-I$OPENSSL/include",
        "LIBS=$OPENSSL/lib/libssl.3.dylib $OPENSSL/lib/libcrypto.3.dylib -Wl,-headerpad_max_install_names"]]


def _fake_zsign_build(tmp_path: Path, monkeypatch, imports: set[str]) -> tuple[list, Path]:
    """build_zsign's tools faked: the archive, its unpacking, make (which
    leaves a bin/zsign), and nm (which reads `imports`)."""
    seen = []
    openssl = tmp_path / "openssl-abc" / "prefix"
    (openssl / "include" / "openssl").mkdir(parents=True)
    (openssl / "lib").mkdir()
    for lib in gb.OPENSSL_LIBS:
        (openssl / "lib" / lib).write_bytes(b"lib")

    def unpack(pin, tmp):
        src = tmp / "src" / "zsign"
        (src / "build" / "macos").mkdir(parents=True)
        (src / "LICENSE").write_text("MIT License\n")
        return src

    def build_steps(steps, cwd, arch="arm64"):
        seen.append(("make", steps, cwd.name))
        (cwd / "bin").mkdir()
        (cwd / "bin" / "zsign").write_bytes(mb.thin([]))

    monkeypatch.setattr(gb, "fetch", lambda pin, cache=None: tmp_path / "zsign.tar.gz")
    monkeypatch.setattr(gb, "check_archive_commit", lambda pin, archive: seen.append(("commit", pin.name)))
    monkeypatch.setattr(gb, "_unpack", unpack)
    monkeypatch.setattr(gb, "_build_steps", build_steps)
    monkeypatch.setattr(gb, "imported_symbols", lambda path: imports)
    monkeypatch.setattr(gb, "toolchain_id", lambda: "clang 1")
    return seen, openssl


def test_zsign_is_built_from_the_commit_its_pin_names_and_cached_by_its_inputs(tmp_path, monkeypatch):
    seen, openssl = _fake_zsign_build(tmp_path, monkeypatch, {"_popen", "_execlp", "_fopen"})
    pin = gb.load_pins(REPO / "bundle" / "pins.toml", "x86_64")["zsign"]
    built = gb.build_zsign(pin, openssl, tmp_path / "work", "x86_64")
    assert (built / "zsign").is_file() and (built / "LICENSE").read_text() == "MIT License\n"
    assert seen[0] == ("commit", "zsign"), "checked to be the pinned commit before anything is built"
    (_, steps, cwd), = seen[1:]
    assert steps == gb.zsign_steps(str(steps[0][-2].split("-I", 1)[1].removesuffix("/include")), gb._jobs(),
                                   "1.1.3", "x86_64")
    assert " " not in steps[0][-2], "the OpenSSL it builds against is a copy without a space in its path"
    assert gb.build_zsign(pin, openssl, tmp_path / "work", "x86_64") == built and len(seen) == 2, "cached"
    assert gb.build_zsign(pin, openssl, tmp_path / "work", "arm64") != built, "another target is another build"
    other = tmp_path / "openssl-def" / "prefix"
    shutil.copytree(openssl, other)
    assert gb.build_zsign(pin, other, tmp_path / "work", "x86_64") != built, "another OpenSSL build is another build"


def test_a_zsign_build_that_can_start_a_shell_some_other_way_is_refused(tmp_path, monkeypatch):
    _, openssl = _fake_zsign_build(tmp_path, monkeypatch, {"_popen", "_system"})
    pin = gb.load_pins(REPO / "bundle" / "pins.toml", "x86_64")["zsign"]
    with pytest.raises(gb.BundleError, match="imports _popen, _system"):
        gb.build_zsign(pin, openssl, tmp_path / "work", "x86_64")


class _Thinned(Exception):
    """Raised by a fake thin_to_arch: the stage got that far."""


def test_the_x86_64_stage_builds_zsign_passes_its_arch_everywhere_and_has_no_unfairplay(tmp_path, monkeypatch):
    calls = []
    dest, work = tmp_path / "stage", tmp_path / "work"
    gp = dest / "Resources" / "GraftPython"
    prefix = tmp_path / "openssl"

    def stage_python(pins, contents, reports=None, arch="arm64"):
        calls.append(("python", pins["python"].sha256[:8], arch))
        (gp / "lib" / "python3.13").mkdir(parents=True)
        (gp / "lib" / "python3.13" / "LICENSE.txt").write_text("PSF")
        return gp

    def build_openssl(pin, work=None, arch="arm64"):
        calls.append(("openssl", arch))
        prefix.mkdir(exist_ok=True)
        (prefix / "LICENSE.txt").write_text("Apache")
        return prefix

    def record(name):
        return lambda *a, **kw: calls.append((name, kw.get("arch")))

    def thin(contents, arch="arm64"):
        calls.append(("thin", arch))
        raise _Thinned

    monkeypatch.setattr(gb, "stage_python", stage_python)
    monkeypatch.setattr(gb, "build_openssl", build_openssl)
    monkeypatch.setattr(gb, "build_zsign", lambda pin, openssl, work=None, arch="arm64":
                        calls.append(("zsign", pin.from_source, openssl, arch)) or tmp_path / "zsign-built")
    monkeypatch.setattr(gb, "fetch", lambda pin, cache=None: calls.append(("fetch", pin.name)) or tmp_path / "f")
    monkeypatch.setattr(gb, "stage_openssl_and_zsign", lambda p, zsign, contents: calls.append(("repoint", zsign)))
    monkeypatch.setattr(gb, "stage_unfairplay", lambda *a, **kw: pytest.fail("the Intel bundle has no UnfairPlay"))
    for name in ("stage_ipatool", "python_static_licences", "rust_crate_licences", "binary_crate_licences",
                 "stage_cyan_helpers", "build_launchers", "stage_preferences_stub"):
        monkeypatch.setattr(gb, name, record(name))
    monkeypatch.setattr(gb, "build_cyan_helpers", lambda *a, arch="arm64", **kw: calls.append(("helpers", arch)) or {})
    monkeypatch.setattr(gb, "cyan_original_hashes", lambda archive, arch="arm64": calls.append(("originals", arch)) or {})
    monkeypatch.setattr(gb, "prune_unlinked_libpython", lambda contents: [])
    monkeypatch.setattr(gb, "thin_to_arch", thin)
    with pytest.raises(_Thinned):
        gb.stage(dest, work, arch="x86_64")
    assert calls == [("python", "708be9e7", "x86_64"), ("openssl", "x86_64"),
                     ("zsign", True, prefix, "x86_64"), ("repoint", tmp_path / "zsign-built" / "zsign"),
                     ("stage_ipatool", "x86_64"), ("python_static_licences", None), ("rust_crate_licences", None),
                     ("binary_crate_licences", None), ("helpers", "x86_64"), ("fetch", "cyan"), ("originals", "x86_64"),
                     ("stage_cyan_helpers", "x86_64"), ("build_launchers", "x86_64"), ("stage_preferences_stub", None),
                     ("thin", "x86_64")]
    assert sorted(p.name for p in (work / "tool-licences").iterdir()) == ["openssl-LICENSE.txt", "python-LICENSE.txt",
                                                                          "zsign-LICENSE"]


def test_the_arm64_stage_still_takes_zsigns_release_binary(tmp_path, monkeypatch):
    pins = gb.load_pins(REPO / "bundle" / "pins.toml")
    monkeypatch.setattr(gb, "build_zsign", lambda *a, **kw: pytest.fail("arm64 builds no zsign"))
    monkeypatch.setattr(gb, "fetch", lambda pin, cache=None: tmp_path / pin.filename)
    monkeypatch.setattr(gb, "extract", lambda archive, dest: dest)
    monkeypatch.setattr(gb, "_the_binary", lambda unpacked, tool: unpacked / f"{tool}-release")
    assert gb.stage_zsign_binary(pins, tmp_path / "openssl", tmp_path / "work") == \
        tmp_path / "work" / "zsign-release" / "zsign-release"


@needs_tools("clang", "lipo")  # compiles with clang; the code under test runs lipo
def test_universal_mach_o_files_in_an_intel_stage_are_thinned_to_x86_64(tmp_path):
    c = tmp_path / "Contents"
    fat = _clang_dylib(c / "Resources/GraftPython/lib/python3.13/site-packages/x/_x.abi3.so", "arm64", "x86_64")
    thin = _clang_dylib(c / "Frameworks/libthin.dylib", "x86_64")
    before = thin.read_bytes()
    assert gb.thin_to_arch(c, "x86_64") == [fat]
    assert _lipo_archs(fat) == ["x86_64"]
    assert thin.read_bytes() == before, "an x86_64-only file is left alone"
    _clang_dylib(c / "Helpers/bin/libarm.dylib", "arm64")
    with pytest.raises(gb.BundleError, match=r"libarm\.dylib is arm64, with no x86_64 slice"):
        gb.thin_to_arch(c, "x86_64")


def test_ios_code_is_not_thinned_in_an_intel_stage_either(tmp_path, monkeypatch):
    contents = tmp_path / "Contents"
    ios = contents / CYAN_EXTRAS / "Orion.framework" / "Orion"
    ios.parent.mkdir(parents=True)
    ios.write_bytes(mb.fat([(mb.CPU_ARM64, mb.thin([mb.build_version_cmd(mb.PLATFORM_IOS, "14.0")])),
                            (mb.CPU_X86_64, mb.thin([mb.build_version_cmd(mb.PLATFORM_IOS, "14.0")]))]))
    before = ios.read_bytes()
    monkeypatch.setattr(gb, "_run", lambda *a, **k: pytest.fail(f"thinned {a}"))
    assert gb.thin_to_arch(contents, "x86_64") == [] and ios.read_bytes() == before


def test_an_intel_stage_prunes_cyans_arm64_helpers_and_keeps_its_x86_64_ones(tmp_path):
    py = tmp_path / "GraftPython"
    tools = py / "lib/python3.13/site-packages/cyan/tools"
    for d in ("Darwin/arm64", "Darwin/x86_64", "Darwin/iOS", "Linux/x86_64"):
        (tools / d).mkdir(parents=True)
        (tools / d / "ldid").write_bytes(b"x")
    (py / "bin").mkdir()
    (py / "bin" / "python3.13").write_bytes(b"py")
    gb.prune_python(py, "x86_64")
    assert sorted(str(p.relative_to(tools)) for p in tools.rglob("ldid")) == ["Darwin/x86_64/ldid"]


def test_cyans_x86_64_originals_are_read_and_replaced_in_darwin_x86_64(tmp_path, monkeypatch):
    import zipfile
    tools = {name: f"cyan's x86_64 {name}".encode() for name in gb.CYAN_HELPERS}
    archive = tmp_path / "cyan.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for name, data in tools.items():
            z.writestr(f"pyzule-rw-0/cyan/tools/Darwin/x86_64/{name}", data)
            z.writestr(f"pyzule-rw-0/cyan/tools/Darwin/arm64/{name}", b"arm64 " + data)
    originals = gb.cyan_original_hashes(archive, "x86_64")
    assert originals == {name: hashlib.sha256(data).hexdigest() for name, data in tools.items()}
    contents, built, _ = _helpers_stage(tmp_path / "stage")
    intel = contents / gb.cyan_tools("x86_64")
    (contents / CYAN_TOOLS).rename(intel)
    for name, data in tools.items():
        (intel / name).write_bytes(data)
    _install_name_tool_fake(monkeypatch)
    gb.stage_cyan_helpers(built, contents, originals, "x86_64")
    assert str(gb.cyan_tools("x86_64")).endswith("cyan/tools/Darwin/x86_64")
    for name in gb.CYAN_HELPERS:
        assert (intel / name).is_file() and (intel / name).read_bytes() != tools[name], name
    # Darwin/x86_64 is as deep as Darwin/arm64: ldid reaches Frameworks the same way.
    assert macho.dylib_names(intel / "ldid")[:2] == [f"{TO_FRAMEWORKS}/libcrypto.3.dylib",
                                                     f"{TO_FRAMEWORKS}/libplist-2.0.dylib"]


def test_a_locked_install_for_x86_64_picks_x86_64_wheels(tmp_path, monkeypatch):
    calls = []
    url = "https://files.pythonhosted.org/packages/aa/bb/x-1.0-py3-none-any.whl"

    def fake_run(argv, **kw):
        argv = [str(a) for a in argv]
        calls.append(argv)
        if "--report" in argv:
            _pip_report(Path(argv[argv.index("--report") + 1]), [("X", "1.0", url, "a" * 64)])
    monkeypatch.setattr(gb.subprocess, "run", fake_run)
    gb.install_locked(tmp_path / "python3.13", gb.RUNTIME_LOCK, tmp_path / "plan.json", tmp_path / "s", {}, "x86_64")
    resolve, download, _ = calls
    for argv in (resolve, download):
        assert argv[argv.index("--platform") + 1] == "macosx_13_0_x86_64"


def test_the_index_names_only_the_go_module_folders_the_bundle_has(tmp_path):
    def index(folders: list[str]) -> str:
        root = tmp_path / "-".join(folders or ["none"])
        extra = root / "tool-licences"
        for f in folders:
            (extra / f / "example.com@v1").mkdir(parents=True)
            (extra / f / "example.com@v1" / "LICENSE").write_text(MIT_TEXT)
        (root / "Contents").mkdir()
        gb.collect_licenses(root / "Contents", gb.load_pins(REPO / "bundle" / "pins.toml"), extra)
        return (root / "Contents/Resources/Licenses/INDEX.txt").read_text()
    assert "; ipatool-modules/ and unfairplay-modules/ for the Go modules in each; " in index(
        ["ipatool-modules", "unfairplay-modules"])
    intel = index(["ipatool-modules"])
    assert "; ipatool-modules/ for the Go modules in it; " in intel and "unfairplay-modules" not in intel


# --- the Intel bundle's runtime lock ------------------------------------------------------------

_LOCK = """#
# This file is autogenerated by pip-compile with Python 3.13
#
alpha==1.0 \\
    --hash=sha256:{a}
    # via graft-ipa (pyproject.toml)
cryptography==50.0.2 \\
    --hash=sha256:{b} \\
    --hash=sha256:{c}
    # via
    #   graft-ipa (pyproject.toml)
    #   paramiko
zeta==2.0 \\
    --hash=sha256:{d}
    # via cryptography
""".format(a="a" * 64, b="b" * 64, c="c" * 64, d="d" * 64)


def test_the_intel_lock_is_the_runtime_lock_but_for_each_override_rehashed_by_pip_compile(tmp_path):
    asked = []

    def hashes(name: str, version: str) -> list[str]:
        asked.append((name, version))
        return ["e" * 64, "f" * 64]

    text = gb.x86_64_lock_text(_LOCK, {"cryptography": "48.0.1"}, hashes)
    assert asked == [("cryptography", "48.0.1")]
    head, body = text.split("alpha==1.0", 1)
    original = _LOCK.split("alpha==1.0")[0]
    assert head.startswith("#\n# The Intel (x86_64) bundle's runtime lock") and "bundle/x86_64-overrides.txt" in head
    assert head.endswith(original.removeprefix("#\n")), "pip-compile's own header follows the note"
    assert "alpha==1.0" + body == "alpha==1.0" + _LOCK.split("alpha==1.0", 1)[1].replace(
        f"cryptography==50.0.2 \\\n    --hash=sha256:{'b' * 64} \\\n    --hash=sha256:{'c' * 64}\n",
        f"cryptography==48.0.1 \\\n    --hash=sha256:{'e' * 64} \\\n    --hash=sha256:{'f' * 64}\n")
    locked = gb.lock_hashes(_write(tmp_path / "x.lock", text))
    assert locked["cryptography"] == ("48.0.1", {"e" * 64, "f" * 64}) and locked["zeta"] == ("2.0", {"d" * 64})


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_an_override_the_runtime_lock_does_not_pin_is_refused():
    with pytest.raises(gb.BundleError, match="nothing"):
        gb.x86_64_lock_text(_LOCK, {"nothing": "1.0"}, lambda n, v: ["e" * 64])
    with pytest.raises(gb.BundleError, match="no hashes"):
        gb.x86_64_lock_text(_LOCK, {"cryptography": "48.0.1"}, lambda n, v: [])


def test_the_overrides_file_names_each_package_with_its_version(tmp_path):
    overrides = _write(tmp_path / "o.txt", "# why\ncryptography==48.0.1  # the last with an x86_64 wheel\n\n")
    assert gb.lock_overrides(overrides) == {"cryptography": "48.0.1"}
    with pytest.raises(gb.BundleError, match="name==version"):
        gb.lock_overrides(_write(tmp_path / "bad.txt", "cryptography>=48\n"))


def test_the_committed_intel_lock_is_the_runtime_lock_with_only_the_overrides_changed():
    """lock-bundle writes both; a runtime lock re-made without the Intel one
    would leave Intel on old versions, which this catches."""
    arm, intel = gb.lock_hashes(gb.RUNTIME_LOCK), gb.lock_hashes(gb.X86_64_LOCK)
    overrides = gb.lock_overrides()
    assert overrides == {"cryptography": "48.0.1"}, "cryptography 49 and later ship no macOS x86_64 wheel"
    assert set(arm) == set(intel)
    for name in arm:
        if name in overrides:
            assert intel[name][0] == overrides[name] and len(intel[name][1]) > 1, name
        else:
            assert intel[name] == arm[name], name
    assert gb.runtime_lock() == gb.runtime_lock("arm64") == gb.RUNTIME_LOCK
    assert gb.runtime_lock("x86_64") == gb.X86_64_LOCK
    blocks = gb.lock_entries(gb.X86_64_LOCK.read_text())[1]
    assert [n for n, _ in blocks] == [n for n, _ in gb.lock_entries(gb.RUNTIME_LOCK.read_text())[1]]


def test_the_intel_stage_installs_the_intel_lock(tmp_path, monkeypatch):
    locks = []

    def install_locked(py, lock, plan, scratch, env, arch="arm64"):
        locks.append((lock.name, arch))
        if lock != gb.BUILD_LOCK:
            raise _Thinned

    monkeypatch.setattr(gb, "fetch", lambda pin, cache=None: tmp_path / "python.tar.gz")
    monkeypatch.setattr(gb, "extract", lambda archive, dest: (dest / "python" / "bin").mkdir(parents=True) or dest)
    monkeypatch.setattr(gb, "install_locked", install_locked)
    for arch, want in (("x86_64", "requirements-x86_64.lock"), ("arm64", "requirements.lock")):
        with pytest.raises(_Thinned):
            gb.stage_python(gb.load_pins(REPO / "bundle" / "pins.toml", arch), tmp_path / arch, arch=arch)
    assert locks == [("build-requirements.lock", "x86_64"), ("requirements-x86_64.lock", "x86_64"),
                     ("build-requirements.lock", "arm64"), ("requirements.lock", "arm64")]


# --- one source archive for both bundles ----------------------------------------------------------

def _archive(components: Path, out: Path, lock: Path | None) -> tuple[bytes, str]:
    out.parent.mkdir(parents=True, exist_ok=True)
    gb.write_sources(components, out, lock=lock)
    with tarfile.open(out) as tf:
        md = tf.extractfile(f"{out.name.removesuffix('.tar.gz')}/SOURCES.md").read().decode()
    return out.read_bytes(), md


def test_sources_md_gives_the_intel_bundles_builds_beside_the_arm64_ones(tmp_path, monkeypatch):
    pins, components, lock, _ = _sources_fixture(tmp_path, monkeypatch)
    _, md = _archive(components, tmp_path / "out" / "Graft-9.9-sources.tar.gz", lock)
    intel = gb.load_pins(REPO / "bundle" / "pins.toml", "x86_64")
    assert "One archive serves both downloads: arm64 (Graft.zip) and x86_64 (Graft-Intel.zip)." in md
    for arch in ("arm64", "x86_64"):
        for step in (gb.openssl_steps("$DESTDIR", [], arch) + gb.libplist_steps("$DESTDIR", [], arch)
                     + gb.ldid_steps("$LIBPLIST", "$OPENSSL", [], arch) + gb.cctools_steps([], arch)
                     + gb.insert_dylib_steps(arch)):
            assert gb.shell_line(step) in md, (arch, step)
    for name in ("openssl", "libplist", "ldid", "cctools", "insert_dylib"):
        section = _section(md, name)
        assert "- For arm64 (Graft.zip), in " in section and "- For x86_64 (Graft-Intel.zip), in " in section, name
    assert f"`Contents/{gb.cyan_tools('x86_64')}/ldid`" in _section(md, "ldid")
    # zsign: the Intel bundle's, from source, with its own section; the arm64 release file isn't built here.
    zsign = _section(md, "zsign")
    assert "- Only in the x86_64 bundle (Graft-Intel.zip)" in zsign and intel["zsign"].url in zsign
    assert gb.shell_line(gb.zsign_steps("$OPENSSL", [], "1.1.3", "x86_64")[0]) in zsign
    assert "zsign 1.1.3 (MIT), the arm64 bundle's" in md.split("## Not built here", 1)[1]
    assert f"python {pins['python'].version} (PSF-2.0)," in md.split("## Not built here", 1)[1]
    assert "- Only in the arm64 bundle (Graft.zip)" in _section(md, "unfairplay")
    ipatool = _section(md, "ipatool")
    assert "`GOARCH=arm64` (arm64) or `GOARCH=amd64` (x86_64)" in ipatool
    assert ipatool.count("\n" + gb.shell_line(["go", "mod", "vendor"]) + "\n") == 1, "one set of steps: only GOARCH differs"
    assert "- For x86_64" not in ipatool and "- In the archive's top directory, once patched" in ipatool
    assert "Darwin/x86_64 in Graft-Intel.zip" in _section(md, "cyan")


def test_either_bundles_components_give_the_same_source_archive(tmp_path, monkeypatch):
    """The arm64 and the Intel release each write the archive; it must be one
    archive, whichever wrote it."""
    pins, components, lock, packages = _sources_fixture(tmp_path, monkeypatch)
    intel = gb.load_pins(REPO / "bundle" / "pins.toml", "x86_64")
    entries = [e for e in json.loads(components.read_text()) if e["kind"] == "python-package"]
    entries += [{"name": n, "version": p.version, "kind": gb.pin_kind(n, intel), "source_url": p.url,
                 "sha256": p.sha256, "license": p.license} for n, p in intel.items()]
    other = tmp_path / "intel-components.json"
    other.write_text(json.dumps(entries))
    first, _ = _archive(components, tmp_path / "a" / "Graft-9.9-sources.tar.gz", lock)
    second, _ = _archive(other, tmp_path / "b" / "Graft-9.9-sources.tar.gz", lock)
    assert first == second


@contextlib.contextmanager
def _fixed_targz(path: Path):
    """A .tar.gz whose bytes don't depend on the clock. `tarfile.open(path,
    "w:gz")` stamps the gzip header with the current second, so two archives
    built from the same fake sdist a second apart differed, and the tests that
    build the sources archive twice and compare the bytes failed whenever the
    suite was slow enough to straddle a second."""
    with path.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w") as tf:
        yield tf


def _sdist_with_licence(path: Path, name: str, version: str, licence: str) -> Path:
    data = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\nLicense-Expression: {licence}\n".encode()
    with _fixed_targz(path) as tf:
        info = tarfile.TarInfo(f"{name}-{version}/PKG-INFO")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return path


def test_a_package_the_bundles_lock_at_two_versions_ships_both_sources_with_their_own_licences(tmp_path, monkeypatch):
    pins, components, lock, packages = _sources_fixture(tmp_path, monkeypatch)
    text = lock.read_text()
    intel_lock = tmp_path / "requirements-x86_64.lock"
    intel_lock.write_text(text.replace("mitpkg==5.0", "mitpkg==4.9").replace(
        hashlib.sha256(b"mitpkg").hexdigest(), hashlib.sha256(b"mitpkg 4.9").hexdigest()))
    monkeypatch.setattr(gb, "runtime_lock", lambda arch="arm64": lock if arch == "arm64" else intel_lock)
    real_fetch, real_pypi = gb.fetch, gb._pypi_sdist

    def pypi(name, version):
        if name == "mitpkg":
            sha = hashlib.sha256(b"mitpkg" if version == "5.0" else b"mitpkg 4.9").hexdigest()
            return f"https://files.pythonhosted.org/packages/ab/mitpkg-{version}.tar.gz", sha
        return real_pypi(name, version)

    def fetch(pin, cache_dir=None):
        if pin.name == "mitpkg":
            licence = "MIT" if pin.version == "5.0" else "BSD-3-Clause"
            return _sdist_with_licence(tmp_path / pin.filename, "mitpkg", pin.version, licence)
        return real_fetch(pin, cache_dir)

    monkeypatch.setattr(gb, "_pypi_sdist", pypi)
    monkeypatch.setattr(gb, "fetch", fetch)
    arm, md = _archive(components, tmp_path / "a" / "Graft-9.9-sources.tar.gz", None)
    with tarfile.open(tmp_path / "a" / "Graft-9.9-sources.tar.gz") as tf:
        names = set(tf.getnames())
    assert {"Graft-9.9-sources/sdists/mitpkg-5.0.tar.gz", "Graft-9.9-sources/sdists/mitpkg-4.9.tar.gz"} <= names
    assert "| mitpkg | 4.9 | BSD-3-Clause |" in md and "| mitpkg | 5.0 | MIT |" in md
    assert "(mitpkg 4.9), and both versions' are here" in md
    # The Intel release's components name mitpkg 4.9: the archive is the same.
    entries = json.loads(components.read_text())
    for e in entries:
        if e["name"] == "mitpkg":
            e.update(version="4.9", license="something its wheel says")
    other = tmp_path / "intel-components.json"
    other.write_text(json.dumps(entries))
    intel, _ = _archive(other, tmp_path / "b" / "Graft-9.9-sources.tar.gz", None)
    assert arm == intel


def test_an_sdists_licence_is_read_from_its_own_pkg_info(tmp_path):
    assert gb.sdist_licence(_sdist_with_licence(tmp_path / "x-1.tar.gz", "x", "1", "Apache-2.0 OR BSD-3-Clause")) == \
        "Apache-2.0 OR BSD-3-Clause"
    with tarfile.open(tmp_path / "bare.tar.gz", "w:gz"):
        pass
    assert gb.sdist_licence(tmp_path / "bare.tar.gz") == "not stated in its metadata"


# --- what an Intel release downloads, on a phone's hotspot -------------------------------------------

def test_an_intel_releases_plan_counts_its_own_builds_and_what_the_shared_archive_needs(tmp_path, monkeypatch):
    """The Intel stage builds ipatool for x86_64; the source archive takes
    the arm64 builds' vendored modules, UnfairPlay's among them, and both
    locks' source distributions."""
    arm, cache, work, lock, crates = _plan_world(tmp_path, monkeypatch)
    monkeypatch.setattr(gb, "_urlopen", lambda *a, **k: pytest.fail("the plan went to the network"))
    intel = {n: p for n, p in arm.items() if n != "unfairplay"}
    (work / f"ipatool-{gb.ipatool_key(intel['ipatool'], intel['go'], 'x86_64')[:12]}").mkdir()
    intel_lock = tmp_path / "requirements-x86_64.lock"
    intel_lock.write_text(lock.read_text().replace("bbb==2.0", "bbb==1.9"))
    plan = gb.download_plan(intel, cache, work, intel_lock, crates, arch="x86_64", extra=[arm["unfairplay"]],
                            source_locks=(lock, intel_lock), source_pins=arm)
    whats = [d.what for d in plan]
    assert "ipatool's Go modules" in whats, "the archive's arm64 ipatool isn't built, nor its modules fetched"
    assert "unfairplay's Go modules" in whats, "the archive carries UnfairPlay's modules though Intel has none"
    assert "unfairplay 1.0 (arm64's, for the source archive)" not in whats, "its pin is cached"
    assert "2 source distribution(s) for the source archive: bbb 1.9, ccc 3.0" in whats
    # bbb 2.0's sdist, which only the arm64 lock pins, is in the archive too.
    (cache / "bbb-2.0.zip").unlink()
    whats = [d.what for d in gb.download_plan(intel, cache, work, intel_lock, crates, arch="x86_64",
                                              source_locks=(lock, intel_lock), source_pins=arm)]
    assert "3 source distribution(s) for the source archive: bbb 1.9, bbb 2.0, ccc 3.0" in whats
    # The x86_64 build alone, cached under its own key, needs no modules.
    assert "ipatool's Go modules" not in [d.what for d in gb.download_plan(
        intel, cache, work, intel_lock, crates, arch="x86_64")]
    (work / f"ipatool-{gb.ipatool_key(arm['ipatool'], arm['go'])[:12]}").mkdir()
    assert "ipatool's Go modules" not in [d.what for d in gb.download_plan(
        intel, cache, work, intel_lock, crates, arch="x86_64", source_pins=arm)], "both builds are cached"
    (cache / f"{'c' * 40}.tar.gz").unlink()
    assert "unfairplay 1.0 (arm64's, for the source archive)" in [d.what for d in gb.download_plan(
        intel, cache, work, intel_lock, crates, arch="x86_64", extra=[arm["unfairplay"]], source_pins=arm)]


def test_a_cached_wheel_counts_only_for_the_architecture_it_is_for(tmp_path):
    lock = tmp_path / "requirements.lock"
    lock.write_text("aaa==1.0 \\\n    --hash=sha256:" + "1" * 64 + " \\\n    --hash=sha256:" + "2" * 64
                    + "\nbbb==2.0 \\\n    --hash=sha256:" + "3" * 64
                    + "\nccc==3.0 \\\n    --hash=sha256:" + "4" * 64 + "\n")
    cache = tmp_path / "cache"
    pip = gb.pip_cache(cache)
    urls = {"1" * 64: "https://files.example.invalid/aaa-1.0-cp313-cp313-macosx_11_0_arm64.whl",
            "3" * 64: "https://files.example.invalid/bbb-2.0-py3-none-any.whl",
            "4" * 64: "https://files.example.invalid/ccc-3.0-cp39-abi3-macosx_10_9_universal2.whl"}
    for url in urls.values():
        _cached_body(pip, url, b"wheel")
    plan = _pip_report(tmp_path / "plan.json", [(u.rsplit("/", 1)[1].split("-")[0], "1.0", u, sha)
                                                 for sha, u in urls.items()])
    gb.record_cached_wheels(plan, pip)
    assert gb.uncached_wheels((lock,), cache, "arm64") == [] == gb.uncached_wheels((lock,), cache)
    assert gb.uncached_wheels((lock,), cache, "x86_64") == ["aaa 1.0"], "its cached wheel is arm64's"


def test_each_releases_plan_covers_its_stage_and_the_one_source_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(gb, "_urlopen", lambda *a, **k: pytest.fail("the plan went to the network"))
    empty = tmp_path / "cache"
    empty.mkdir()
    for arch, own, archived in (("arm64", "zsign 1.1.3", "zsign 1.1.3 (x86_64's, for the source archive)"),
                                ("x86_64", "zsign 1.1.3", "unfairplay 0.4.0 (arm64's, for the source archive)")):
        plan = gb.release_plan(arch, empty, tmp_path / "work")
        whats = [d.what for d in plan]
        assert own in whats and archived in whats, (arch, whats)
        assert ("unfairplay 0.4.0" in whats) == (arch == "arm64")
        assert "unfairplay's Go modules" in whats, arch
        sizes = {d.what: d.mb for d in plan}
        assert sizes["python 3.13.18+20261026"] == 25
    intel = {d.what: d.mb for d in gb.release_plan("x86_64", empty, tmp_path / "work")}
    assert intel["zsign 1.1.3"] == 24, "the Intel zsign is its 24 MB source archive, not the arm64 release"


def test_the_hotspot_check_plans_for_the_arch_it_is_given(monkeypatch):
    asked = []
    monkeypatch.setattr(gb, "release_plan", lambda arch="arm64": asked.append(arch) or [])
    monkeypatch.setattr(gb, "default_route", lambda: (gb.HOTSPOT_GATEWAY, "en7"))
    assert gb.main(["hotspot-check", "--arch", "x86_64"]) == 0
    assert gb.main(["hotspot-check"]) == 0
    assert asked == ["x86_64", "arm64"]
    assert gb.main(["hotspot-check", "--arch", "ppc"]) == 2



# --- review: the source archive's licenses are the sdists' own --------------------------------------

def test_each_sdists_licence_in_sources_md_is_its_own_not_a_wheels(tmp_path, monkeypatch):
    """Review #3: the license column came from the release's components.json,
    its wheels' metadata, which an x86_64 wheel may state otherwise than the
    arm64 one: two archives under one name. Now each sdist states its own."""
    pins, components, lock, packages = _sources_fixture(tmp_path, monkeypatch)
    entries = json.loads(components.read_text())
    for e in entries:
        if e["kind"] == "python-package":
            e["license"] = f"what {e['name']}'s wheel for one Mac says"
    other = tmp_path / "other-components.json"
    other.write_text(json.dumps(entries))
    first, md = _archive(components, tmp_path / "a" / "Graft-9.9-sources.tar.gz", lock)
    second, _ = _archive(other, tmp_path / "b" / "Graft-9.9-sources.tar.gz", lock)
    assert first == second, "the same archive, whichever release's components it was given"
    for name, version, licence in packages:
        if name != "wheelonly":
            assert f"| {gb._norm(name)} | {version} | {licence} |" in md, name
    assert "wheel for one Mac" not in md


def _sdist(path: Path, top: str, files: dict[str, str]) -> Path:
    with _fixed_targz(path) as tf:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


@pytest.mark.parametrize("files, licence", [
    ({"PKG-INFO": "Metadata-Version: 2.4\nName: a\nLicense-Expression: MIT\n",
      "pyproject.toml": '[project]\nlicense = "BSD-3-Clause"\n'}, "MIT"),
    # ptyprocess 0.7.0: an old flit sdist, whose PKG-INFO states nothing; its pyproject.toml has the classifier.
    ({"PKG-INFO": "Metadata-Version: 1.1\nName: a\n", "LICENSE": "ISC License text",
      "pyproject.toml": '[tool.flit.metadata]\nclassifiers = ["License :: OSI Approved :: ISC License (ISCL)"]\n'},
     "ISC License (ISCL)"),
    ({"PKG-INFO": "Metadata-Version: 2.1\nName: a\n", "pyproject.toml": '[project]\nlicense = "Apache-2.0"\n'},
     "Apache-2.0"),
    ({"PKG-INFO": "Metadata-Version: 2.1\nName: a\n",
      "pyproject.toml": '[project]\nlicense = {text = "MIT License"}\n'}, "MIT License"),
    ({"PKG-INFO": "Metadata-Version: 2.1\nName: a\n", "LICENSE.txt": "some text"},
     "not stated in its metadata; see its license file"),
    ({"PKG-INFO": "Metadata-Version: 2.1\nName: a\n", "pyproject.toml": "not [valid toml"},
     "not stated in its metadata"),
])
def test_an_sdists_licence_is_its_pkg_info_s_else_its_pyproject_s(tmp_path, files, licence):
    assert gb.sdist_licence(_sdist(tmp_path / "a-1.tar.gz", "a-1", files)) == licence



# --- review: more of v2.2's arm64 stage, recorded -------------------------------------------------

#: The stage functions record_stage_calls stands in for, and what each gives back.
_STAGE_STEPS = ("stage_python", "cyan_frameworks", "build_openssl", "fetch", "extract", "_the_binary",
                "stage_openssl_and_zsign", "vendored_licences", "stage_ipatool", "stage_unfairplay",
                "python_static_licences", "lock_hashes", "rust_crate_licences", "binary_crate_licences",
                "build_cyan_helpers", "cyan_original_hashes", "stage_cyan_helpers", "build_launchers",
                "stage_preferences_stub", "prune_unlinked_libpython", "thin_to_arm64", "thin_to_arch",
                "prune_foreign_rpaths", "collect_licenses", "crate_notice_problems", "write_components",
                "check_no_build_paths", "signed_machos", "write_entitlements_list")


def record_stage_calls(module, tmp: Path) -> dict:
    """What a graft_bundle `module` does for the arm64 bundle beyond its build
    commands: its build environment, zsign's OpenSSL repoint plan, a locked
    install's pip commands, and the order of stage()'s steps. Paths are
    `$TMP`, `$REPO` and `$HOME`, so v2.2's record (written by this, from
    v2.2's module) can be compared. Every tool is a stand-in: nothing runs."""
    real_run, real_cache, real_steps = module.subprocess.run, module.CACHE, {n: getattr(module, n, None)
                                                                             for n in _STAGE_STEPS}
    repo = str(module.REPO)

    def norm(value):
        text = json.dumps(value)
        for path, name in ((str(tmp), "$TMP"), (repo, "$REPO"), (os.environ.get("HOME", "/"), "$HOME"),
                           (os.environ.get("TMPDIR", "/tmp"), "$TMPDIR")):
            text = text.replace(path, name)
        return json.loads(text)

    record = {"_build_env": norm({k: v for k, v in module._build_env().items() if k != "DEVELOPER_DIR"})}
    zsign = tmp / "zsign"
    zsign.write_bytes(mb.thin([mb.dylib_cmd(n) for n in (
        "/opt/homebrew/opt/openssl@3/lib/libssl.3.dylib", "/opt/homebrew/opt/openssl@3/lib/libcrypto.3.dylib",
        "/usr/lib/libz.1.dylib", "/usr/lib/libc++.1.dylib", "/usr/lib/libSystem.B.dylib")]))
    record["openssl_repoint_plan"] = [list(p) for p in module.openssl_repoint_plan(zsign)]
    pip = []

    def fake_run(argv, **kw):
        argv = [str(a) for a in argv]
        pip.append(argv)
        if "--report" in argv:
            url = "https://files.pythonhosted.org/packages/aa/a-1.0-py3-none-any.whl"
            Path(argv[argv.index("--report") + 1]).write_text(json.dumps({"install": [{
                "download_info": {"url": url, "archive_info": {"hashes": {"sha256": "a" * 64}}},
                "metadata": {"name": "a", "version": "1.0"}}]}))
        return subprocess.CompletedProcess(argv, 0, "", "")

    order = []
    try:
        module.subprocess.run, module.CACHE = fake_run, tmp / "cache"
        module.install_locked(Path("/py/bin/python3.13"), module.RUNTIME_LOCK, tmp / "plan.json", tmp / "pip", {})
        record["install_locked"] = norm(pip)
        gp, prefix = tmp / "GraftPython", tmp / "openssl"
        for d in (gp / "lib" / "python3.13", prefix):
            d.mkdir(parents=True)
        (gp / "lib" / "python3.13" / "LICENSE.txt").write_text("PSF")
        (prefix / "LICENSE.txt").write_text("Apache")
        gives = {"stage_python": gp, "build_openssl": prefix, "fetch": tmp / "f", "extract": tmp / "x",
                 "_the_binary": tmp / "zsign", "vendored_licences": tmp / "licences", "lock_hashes": {},
                 "build_cyan_helpers": {}, "cyan_original_hashes": {}, "prune_unlinked_libpython": [],
                 "thin_to_arm64": [], "thin_to_arch": [], "prune_foreign_rpaths": [], "crate_notice_problems": [],
                 "signed_machos": [], "cyan_frameworks": []}
        (tmp / "licences").mkdir()
        for name in _STAGE_STEPS:
            if hasattr(module, name):
                setattr(module, name, lambda *a, _n=name, **k: order.append(_n) or gives.get(_n))
        module.stage(tmp / "stage", tmp / "work")
        record["stage_order"] = order
    finally:
        module.subprocess.run, module.CACHE = real_run, real_cache
        for name, fn in real_steps.items():
            if fn is not None:
                setattr(module, name, fn)
    return record


def test_the_v2_2_records_are_the_ones_reviewed():
    for name, sha in STAGE_FIXTURES.items():
        got = hashlib.sha256((REPO / "tests" / "fixtures" / "bundle" / name).read_bytes()).hexdigest()
        assert got == sha, f"{name} changed: that is a change to what the arm64 stage does, so review it"


def test_the_arm64_stage_still_builds_its_env_repoints_zsign_installs_and_stages_in_2_2_s_order(tmp_path):
    """The build environment, zsign's OpenSSL repoint plan, a locked
    install's three pip commands and stage()'s steps, in order: v2.2's
    (tests/fixtures/bundle/arm64-stage-calls-2.2.json, recorded from 17fe3a8
    by record_stage_calls). Only thin_to_arm64 is now thin_to_arch."""
    want = json.loads((REPO / "tests" / "fixtures" / "bundle" / "arm64-stage-calls-2.2.json").read_text())
    got = record_stage_calls(gb, tmp_path)
    got["stage_order"] = ["thin_to_arm64" if n == "thin_to_arch" else n for n in got["stage_order"]]
    assert got == want


# --- 2.2.1: ipatool's build runs its patch's Go tests ----------------------------------------------


def test_ipatool_s_build_runs_its_patch_s_go_tests_after_the_build_from_vendor():
    """v2.2's three steps, then the mocks ipatool's tests use and the tests
    of the package the patch changes: offline (-mod=vendor), never from a
    test cache (-count=1). After `go build`, so the mocks `go generate`
    writes into pkg/ are never compiled into ipatool."""
    steps = gb.ipatool_steps("2.8.0")
    assert steps == [*ARM64_STAGE["ipatool"], *IPATOOL_TEST_STEPS]
    assert steps[-2:] == [gb.IPATOOL_MOCKS, gb.IPATOOL_TESTS]
    assert all("-mod=vendor" in step for step in steps[2:])


def test_ipatool_s_mocks_and_tests_can_download_nothing_not_even_a_command_go_generate_runs():
    """After vendoring, the mocks and the tests run with GOPROXY=off and
    GOFLAGS=-mod=vendor in their environment, not only -mod=vendor on their
    own command line: a `//go:generate go run x@v` directive starts a go of
    its own, which reads the environment and never the outer flag, and would
    otherwise download x from the module proxy without anyone noticing."""
    for step in (gb.IPATOOL_MOCKS, gb.IPATOOL_TESTS):
        assert step[:3] == ["/usr/bin/env", "GOPROXY=off", "GOFLAGS=-mod=vendor"], step
        assert step[3] == "go"
    assert gb.IPATOOL_MOCKS[4] == "generate" and gb.IPATOOL_TESTS[4] == "test"


def test_every_file_ipatool_s_patch_changes_is_in_the_package_its_tests_run_for():
    """`./pkg/appstore/...` covers the patch: each file it adds or changes,
    its two test files among them."""
    for patch in gb.PATCHES["ipatool"]:
        text = (gb.PATCHES_DIR / patch).read_text()
        changed = [line[len("+++ b/"):] for line in text.splitlines() if line.startswith("+++ b/")]
        assert changed and all(p.startswith("pkg/appstore/") for p in changed), changed
        assert sorted(p for p in changed if p.endswith("_test.go")) == [
            "pkg/appstore/appstore_get_version_metadata_test.go",
            "pkg/appstore/appstore_partial_zip_readahead_test.go"]


def _stub_ipatool_build(tmp_path: Path, monkeypatch, runs: list, fail: str | None = None) -> tuple:
    """build_ipatool with everything but its own steps stubbed out: each
    command it runs is recorded in `runs` with its env, and the one whose
    second word is `fail` fails, as `subprocess.run` does: raising only
    when it was asked to check.
    Returns the two pins."""
    pin = gb.Pin(name="ipatool", url="https://example.invalid/i.tar.gz", sha256="a" * 64, license="MIT",
                 version="2.8.0")
    go_pin = gb.Pin(name="go", url="https://example.invalid/go.tar.gz", sha256="b" * 64, license="BSD-3-Clause",
                    version="1.26.5")

    def extract(archive, dest):
        (dest / "go" / "bin").mkdir(parents=True, exist_ok=True)
        for f in ("LICENSE", "PATENTS"):
            (dest / "go" / f).write_text(f"Go's {f}")
        return dest

    def unpack(pin, tmp):
        top = tmp / "src" / "ipatool-x"
        top.mkdir(parents=True)
        (top / "LICENSE").write_text("ipatool MIT")
        return top

    def run(argv, cwd=None, env=None, check=False, **kw):
        runs.append((list(argv), dict(env)))
        go = argv.index("go")
        if argv[go + 1] == fail:
            if check:
                raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(argv, 1, "", "")
        if argv[:3] == ["go", "mod", "vendor"]:
            (Path(cwd) / "vendor").mkdir()
            (Path(cwd) / "vendor" / "modules.txt").write_text("")
        if argv[:2] == ["go", "build"]:
            (Path(cwd) / "ipatool").write_bytes(b"ipatool binary")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(gb, "fetch", lambda pin, cache=None: tmp_path / pin.filename)
    monkeypatch.setattr(gb, "extract", extract)
    monkeypatch.setattr(gb, "_unpack", unpack)
    monkeypatch.setattr(gb, "apply_patches", lambda name, src, p=None: None)
    monkeypatch.setattr(gb.subprocess, "run", run)
    monkeypatch.setattr(gb, "check_no_shell", lambda path, name: None)
    monkeypatch.setattr(gb, "compiled_modules", lambda go, binary, env: [])
    monkeypatch.setattr(gb, "module_licences", lambda modcache, modules, dest: dest.mkdir(parents=True))
    monkeypatch.setattr(gb, "toolchain_id", lambda: "clang 1; SDK 15.0")
    return pin, go_pin


@pytest.mark.parametrize("arch", ["arm64", "x86_64"])
def test_ipatool_s_tests_run_with_its_build_s_own_settings(tmp_path, monkeypatch, arch):
    """The pinned Go first on PATH, the hermetic settings, and the bundle's
    architecture: an x86_64 build's mockgen and tests are amd64 programs,
    run here under Rosetta, as the x86_64 stage's configure tests and
    Python are."""
    runs: list = []
    pin, go_pin = _stub_ipatool_build(tmp_path, monkeypatch, runs)
    gb.build_ipatool(pin, go_pin, tmp_path / "work", arch)
    assert [argv for argv, _ in runs][-2:] == IPATOOL_TEST_STEPS
    build_env = next(env for argv, env in runs if argv[:2] == ["go", "build"])
    for argv, env in runs[-2:]:
        assert env == build_env, argv
    assert build_env["GOARCH"] == {"arm64": "arm64", "x86_64": "amd64"}[arch] and build_env["CGO_ENABLED"] == "1"
    assert {k: build_env[k] for k in gb.GO_HERMETIC} == gb.GO_HERMETIC
    assert build_env["PATH"].split(":")[0].endswith("/go/bin"), "the pinned Go"
    floor = {"arm64": "14.0", "x86_64": "13.0"}[arch]
    assert build_env["MACOSX_DEPLOYMENT_TARGET"] == floor
    assert build_env["CGO_CFLAGS"] == build_env["CGO_LDFLAGS"] == f"-mmacosx-version-min={floor}"


@pytest.mark.parametrize("fail", ["generate", "test"])
def test_a_failing_go_test_stops_ipatool_s_build_and_caches_nothing(tmp_path, monkeypatch, fail):
    runs: list = []
    pin, go_pin = _stub_ipatool_build(tmp_path, monkeypatch, runs, fail=fail)
    with pytest.raises(subprocess.CalledProcessError):
        gb.build_ipatool(pin, go_pin, tmp_path / "work")
    last = runs[-1][0]
    assert last[last.index("go") + 1] == fail
    assert not list((tmp_path / "work").glob("ipatool-*")), "no build left to be reused"
    runs.clear()
    _stub_ipatool_build(tmp_path, monkeypatch, runs)
    built = gb.build_ipatool(pin, go_pin, tmp_path / "work")
    assert (built / "ipatool").read_bytes() == b"ipatool binary"
    assert [argv for argv, _ in runs][-2:] == IPATOOL_TEST_STEPS, "built again, tests and all"


def test_an_ipatool_build_cached_before_its_tests_ran_is_never_reused(monkeypatch):
    """The cache key covers the steps, the tests among them: a build cached
    by v2.2's steps, which never ran them, has another key, for either
    bundle, even with this builder's own hash (BUILDER) the same."""
    monkeypatch.setattr(gb, "toolchain_id", lambda: "clang 1; SDK 15.0")
    real = gb.ipatool_steps
    for arch in gb.ARCHS:
        pins = gb.load_pins(REPO / "bundle" / "pins.toml", arch)
        now = gb.ipatool_key(pins["ipatool"], pins["go"], arch)
        with monkeypatch.context() as m:
            m.setattr(gb, "ipatool_steps", lambda version: real(version)[:-2])
            untested = gb.ipatool_key(pins["ipatool"], pins["go"], arch)
        assert now != untested, arch
        assert now == gb.ipatool_key(pins["ipatool"], pins["go"], arch), "and the same steps, the same key"
