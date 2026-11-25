"""Which build an update is: the Apple silicon build updates from Graft.zip,
the Intel build from Graft-Intel.zip, and neither ever installs the other.

The build is the bundle's own (`app_update.bundle_flavor`): its Info.plist's
`GraftBundleArch`, which must agree with its embedded Python's Mach-O header.
Never the Mac's processor (`host.apple_silicon`) nor the asking process's: the
app's executable is universal in both builds, so an Intel build on an Apple
silicon Mac is still the Intel build. The Mach-O files here are real headers
made by `macho_builder`, thin or universal, read by the same reader the
updater uses.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path
from typing import Any

import pytest

from graft_ipa import app_update, host

from .app_update_fakes import (
    TAG_PAGE,
    FakeTools,
    FixtureServer,
    Route,
    asset_json,
    dummy_app,
    macho_for,
    zip_app,
)
from .test_app_update import (  # noqa: F401 (fixtures)
    TAG,
    Spawned,
    _events,
    _refused,
    _run,
    _stage,
    download,
    failed_download,
    installed,
    leftovers,
    server,
    spawned,
    tools,
    waiting_pid,
)

ARM64, X86_64 = app_update.ARM64, app_update.X86_64


def publish_builds(server: FixtureServer, tmp_path: Path, builds: dict[str, dict[str, Any]],
                   *, version: str = "2.0.2") -> None:
    """Serve release TAG with one zip per entry of `builds`: its asset name,
    and the `dummy_app` arguments of the Graft.app inside it. Each app's
    marker is its zip's name, so a test can tell which one was installed."""
    assets = []
    for name, app_args in builds.items():
        src = tmp_path / f"release-{name}"
        src.mkdir()
        data = zip_app(dummy_app(src, version, marker=name, **app_args), src / name)
        assets.append(asset_json(server.base, TAG, data, name))
        server.routes[f"/download/{TAG}/{name}"] = Route(
            body=data, headers={"Content-Type": "application/octet-stream"},
        )
    server.serve_json("/latest", {
        "tag_name": TAG, "html_url": TAG_PAGE + TAG, "draft": False, "prerelease": False,
        "assets": assets,
    })


#: A release as it should be: each build's zip holds that build.
BOTH = {
    "Graft.zip": {"flavor": ARM64},
    "Graft-Intel.zip": {"flavor": X86_64},
}


def marker(staged: app_update.Staged) -> str:
    return (staged.path / "Contents" / "Resources" / "marker.txt").read_text()


# --- The build's own zip --------------------------------------------------------


def test_each_builds_zip_is_named_for_it_and_graft_zip_stays_apple_silicon() -> None:
    """Graft.zip keeps meaning arm64: the 2.0.2 and 2.1 updaters ask for it."""
    assert app_update.ASSETS == {"arm64": "Graft.zip", "x86_64": "Graft-Intel.zip"}


@pytest.mark.parametrize("apple_silicon_mac", [True, False], ids=["Apple silicon Mac", "Intel Mac"])
@pytest.mark.parametrize("flavor", [ARM64, X86_64])
def test_each_build_downloads_its_own_zip_whatever_the_mac(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path,
    monkeypatch: pytest.MonkeyPatch, apple_silicon_mac: bool, flavor: str,
) -> None:
    """All four: the build decides, the Mac doesn't. An Intel build on Apple
    silicon (Rosetta) stays the Intel build, and the Apple silicon build on
    an Intel Mac, which can't run its CLI at all, would still ask for its own."""
    monkeypatch.setattr(host, "_sysctl_arm64", lambda: 1 if apple_silicon_mac else None)
    publish_builds(server, tmp_path, BOTH)
    tools.flavor = flavor

    staged, events = download(installed)

    assert marker(staged) == app_update.ASSETS[flavor]
    assert server.paths() == ["/latest", f"/download/{TAG}/{app_update.ASSETS[flavor]}"]
    assert "architecture" in [e["check"] for e in events]


def test_an_intel_build_with_no_intel_zip_has_no_update_and_downloads_nothing(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path
) -> None:
    """Never Graft.zip in its place: "no update for this Mac", its own reason."""
    publish_builds(server, tmp_path, {"Graft.zip": {"flavor": ARM64}})
    tools.flavor = X86_64

    error = failed_download(installed)

    assert error.reason == app_update.NOT_FOR_THIS_MAC
    assert error.message == ("The latest release has no Graft-Intel.zip, the download for Intel "
                             "Macs, so there's no update for this Mac yet.")
    assert error.url == TAG_PAGE + TAG
    assert server.paths() == ["/latest"], "the other build's zip is never fetched"
    assert tools.calls == []
    assert leftovers() == []


def test_the_apple_silicon_build_with_only_an_intel_zip_has_no_update(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path
) -> None:
    publish_builds(server, tmp_path, {"Graft-Intel.zip": {"flavor": X86_64}})
    assert failed_download(installed).reason == app_update.NOT_FOR_THIS_MAC
    assert server.paths() == ["/latest"]


@pytest.mark.parametrize("name", ["graft-intel.zip", "Graft-Intel.zip.zip", "Graft-Intel", "Graft-intel.zip"])
def test_only_the_exact_name_is_the_intel_zip(name: str) -> None:
    data = {"assets": [asset_json("https://github.com/GreenglassT/Graft/releases", TAG, b"x", name)]}
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.release_asset(data, flavor=X86_64)
    assert caught.value.reason == app_update.NOT_FOR_THIS_MAC


def test_the_intel_zip_must_be_served_under_its_own_name() -> None:
    """Listed as Graft-Intel.zip, linked to Graft.zip: not on the release page."""
    data = {"assets": [asset_json(
        "https://github.com/GreenglassT/Graft/releases", TAG, b"x", "Graft-Intel.zip",
        browser_download_url=f"https://github.com/GreenglassT/Graft/releases/download/{TAG}/Graft.zip",
    )]}
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.release_asset(data, flavor=X86_64)
    assert caught.value.reason == app_update.NO_ASSET


# --- A download of the wrong build -----------------------------------------------


WRONG_FOR_INTEL = [
    pytest.param({"flavor": ARM64}, "is the build for Macs with Apple silicon", id="the Apple silicon build"),
    pytest.param({"flavor": None}, "is the build for Macs with Apple silicon", id="no key: Apple silicon"),
    pytest.param({"flavor": X86_64, "python": (ARM64,)}, "is mixed up", id="key says Intel, Python arm64"),
    pytest.param({"flavor": X86_64, "executable": (ARM64,)}, "no code for Intel Macs in its main executable",
                 id="an arm64-only executable"),
]
WRONG_FOR_APPLE_SILICON = [
    pytest.param({"flavor": X86_64}, "is the build for Intel Macs", id="the Intel build"),
    pytest.param({"flavor": None, "python": (X86_64,)}, "is mixed up", id="no key, Python x86_64"),
    pytest.param({"flavor": ARM64, "python": (X86_64,)}, "is mixed up", id="key says arm64, Python x86_64"),
    pytest.param({"flavor": ARM64, "executable": (X86_64,)},
                 "no code for Macs with Apple silicon in its main executable", id="an Intel-only executable"),
    pytest.param({"flavor": "universal"}, "names no build Graft knows", id="an unknown build"),
]


@pytest.mark.parametrize(("inside", "said"), WRONG_FOR_INTEL)
def test_an_intel_build_refuses_a_download_that_is_not_the_intel_build(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path,
    inside: dict[str, Any], said: str,
) -> None:
    """A genuine Graft of the other build would pass every signature check:
    this is the check that keeps it out, before those run."""
    publish_builds(server, tmp_path, {"Graft-Intel.zip": inside})
    tools.flavor = X86_64

    error = failed_download(installed)

    assert error.reason == app_update.ARCHITECTURE
    assert said in error.message
    assert error.message.endswith("so Graft deleted it.")
    assert tools.tools_run() == ["ditto"], "refused before the signature checks"
    assert leftovers() == []


@pytest.mark.parametrize(("inside", "said"), WRONG_FOR_APPLE_SILICON)
def test_the_apple_silicon_build_refuses_a_download_that_is_not_its_build(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path,
    inside: dict[str, Any], said: str,
) -> None:
    publish_builds(server, tmp_path, {"Graft.zip": inside})

    error = failed_download(installed)

    assert error.reason == app_update.ARCHITECTURE
    assert said in error.message
    assert leftovers() == []


def test_a_download_with_no_key_is_the_apple_silicon_build(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path
) -> None:
    """As every Graft.app made before the key was: arm64 only."""
    publish_builds(server, tmp_path, {"Graft.zip": {"flavor": None}})
    staged, _ = download(installed)
    assert marker(staged) == "Graft.zip"


def test_a_universal_python_agrees_with_either_build(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path
) -> None:
    publish_builds(server, tmp_path, {"Graft-Intel.zip": {"flavor": X86_64, "python": (X86_64, ARM64)}})
    tools.flavor = X86_64
    assert marker(download(installed)[0]) == "Graft-Intel.zip"


@pytest.mark.parametrize("break_it", ["no executable", "executable is a script", "executable is a link",
                                      "no CFBundleExecutable", "CFBundleExecutable climbs out"])
def test_a_main_executable_that_cant_be_read_is_refused(tmp_path: Path, break_it: str) -> None:
    app = dummy_app(tmp_path, "2.0.2")
    exe = app / "Contents" / "MacOS" / "Graft"
    plist = app / "Contents" / "Info.plist"
    info = plistlib.loads(plist.read_bytes())
    if break_it == "no executable":
        exe.unlink()
    elif break_it == "executable is a script":
        exe.write_text("#!/bin/sh\nexit 0\n")
    elif break_it == "executable is a link":
        real = tmp_path / "elsewhere"
        real.write_bytes(macho_for((ARM64,)))
        exe.unlink()
        exe.symlink_to(real)
    elif break_it == "no CFBundleExecutable":
        del info["CFBundleExecutable"]
    else:
        info["CFBundleExecutable"] = "../Resources/GraftPython/bin/python3.13"
    plist.write_bytes(plistlib.dumps(info))
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.check_architecture(app, flavor=ARM64)
    assert caught.value.reason == app_update.ARCHITECTURE


# --- Which build a bundle is -----------------------------------------------------


@pytest.mark.parametrize(("args", "flavor"), [
    ({"flavor": ARM64}, ARM64),
    ({"flavor": X86_64}, X86_64),
    ({"flavor": None}, ARM64),
    ({"flavor": X86_64, "python": (ARM64, X86_64)}, X86_64),
])
def test_a_bundle_is_the_build_its_plist_names_once_its_python_agrees(
    tmp_path: Path, args: dict[str, Any], flavor: str
) -> None:
    assert app_update.bundle_flavor(dummy_app(tmp_path, "2.2", **args)) == flavor


@pytest.mark.parametrize(("args", "said"), [
    ({"flavor": X86_64, "python": (ARM64,)}, "says it is the build for Intel Macs (x86_64), and its "
                                              "embedded Python has code only for arm64"),
    ({"flavor": ARM64, "python": (X86_64,)}, "has code only for x86_64"),
    ({"flavor": None, "python": (X86_64,)}, "has code only for x86_64"),
    ({"flavor": "x86_64h"}, "names no build Graft knows (GraftBundleArch is 'x86_64h')"),
    ({"flavor": "arm64e"}, "names no build Graft knows"),
    ({"flavor": ""}, "names no build Graft knows"),
])
def test_a_bundle_that_disagrees_with_itself_is_no_build_at_all(
    tmp_path: Path, args: dict[str, Any], said: str
) -> None:
    with pytest.raises(app_update.MixedBuild, match=said.replace("(", r"\(").replace(")", r"\)")):
        app_update.bundle_flavor(dummy_app(tmp_path, "2.2", **args))


@pytest.mark.parametrize("value", [1, ["arm64"], {"arm64": True}, True])
def test_a_key_that_is_not_a_string_is_no_build(tmp_path: Path, value: object) -> None:
    app = dummy_app(tmp_path, "2.2")
    plist = app / "Contents" / "Info.plist"
    info = plistlib.loads(plist.read_bytes())
    info[app_update.FLAVOR_KEY] = value
    plist.write_bytes(plistlib.dumps(info))
    with pytest.raises(app_update.MixedBuild):
        app_update.bundle_flavor(app)


@pytest.mark.parametrize("break_it", ["no Python", "Python is a script", "Python is a link", "no Info.plist"])
def test_a_bundle_without_a_readable_python_or_plist_is_no_build(tmp_path: Path, break_it: str) -> None:
    """A Debug or Xcode build, too: no embedded Python, so no answer."""
    app = dummy_app(tmp_path, "2.2")
    python = app.joinpath(*app_update.EMBEDDED_PYTHON, "python3.13")
    if break_it == "no Python":
        python.unlink()
    elif break_it == "Python is a script":
        python.write_text("#!/bin/sh\n")
    elif break_it == "Python is a link":
        real = tmp_path / "real-python"
        real.write_bytes(macho_for((ARM64,)))
        python.unlink()
        python.symlink_to(real)
    else:
        (app / "Contents" / "Info.plist").unlink()
    with pytest.raises(app_update.MixedBuild):
        app_update.bundle_flavor(app)


@pytest.mark.parametrize("apple_silicon_mac", [True, False])
@pytest.mark.parametrize("flavor", [ARM64, X86_64])
def test_the_running_graft_is_the_build_its_own_bundle_says_whatever_the_mac(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, apple_silicon_mac: bool, flavor: str
) -> None:
    """`own_flavor` asks the bundle the CLI runs from, never the host: the
    one around the interpreter running this (`own_bundle`)."""
    monkeypatch.setattr(host, "_sysctl_arm64", lambda: 1 if apple_silicon_mac else None)
    app = dummy_app(tmp_path, "2.2", flavor=flavor)
    monkeypatch.setattr(sys, "executable", str(app.joinpath(*app_update.EMBEDDED_PYTHON, "python3.13")))
    assert app_update.own_bundle() == app
    assert app_update.own_flavor() == flavor


def test_the_running_graft_s_python_is_the_one_running_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its version is known: the interpreter running this. A second Python
    beside it, which a download would be refused for, doesn't matter here;
    which one is running does."""
    app = dummy_app(tmp_path, "2.2", flavor=X86_64)
    bin_dir = app.joinpath(*app_update.EMBEDDED_PYTHON)
    (bin_dir / "python3.14").write_bytes(macho_for((ARM64,)))
    monkeypatch.setattr(sys, "executable", str(bin_dir / "python3.13"))
    assert app_update.own_flavor() == X86_64
    monkeypatch.setattr(sys, "executable", str(bin_dir / "python3.14"))
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.own_flavor()
    assert caught.value.reason == app_update.MIXED_BUILD


def test_a_graft_outside_a_bundle_has_no_build_to_update() -> None:
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.own_flavor()  # this test's interpreter runs from no bundle
    assert caught.value.reason == app_update.NO_TEAM


def test_a_running_graft_that_disagrees_with_itself_says_so(tmp_path: Path) -> None:
    app = dummy_app(tmp_path, "2.2", flavor=X86_64, python=(ARM64,))
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.own_flavor(app)
    assert caught.value.reason == app_update.MIXED_BUILD
    assert caught.value.message.startswith("This Graft.app is mixed up: its Info.plist says")
    assert "Download Graft again" in caught.value.message


def test_a_mixed_up_graft_downloads_nothing(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real `own_flavor`, on a running bundle whose plist (no key: arm64)
    and Python (x86_64) disagree: no release is even asked about."""
    publish_builds(server, tmp_path, BOTH)
    mixed = dummy_app(tmp_path / "running", "2.0.1", flavor=None, python=(X86_64,))
    monkeypatch.setattr(app_update, "own_flavor", lambda app=None: _real_own_flavor(mixed))
    assert failed_download(installed).reason == app_update.MIXED_BUILD
    assert server.requests == [], "not even the check"


#: `own_flavor` as the module defines it, before the `tools` fixture
#: replaces it with its stand-in.
_real_own_flavor = app_update.own_flavor


# --- The install ------------------------------------------------------------------


def _stage_build(tmp_path: Path, **args: Any) -> Path:
    work = app_update.updates_dir() / "Graft-test.noindex" / "app"
    work.mkdir(parents=True)
    return dummy_app(work, "2.0.2", marker="new", **args)


def test_the_install_refuses_another_build_put_in_the_cache_since(
    spawned: Spawned, tools: FakeTools, waiting_pid: int, tmp_path: Path, installed: Path
) -> None:
    """The cache is this user's to change: the build is checked again before
    the helper starts, which then checks the plist once more."""
    staged = _stage_build(tmp_path, flavor=ARM64)
    tools.flavor = X86_64
    result = _run(["--json", "--install-update", str(staged), "--app-path", str(installed),
                   "--wait-pid", str(waiting_pid)])
    _refused(result, app_update.ARCHITECTURE, spawned)


def test_the_install_hands_the_helper_its_own_build(
    spawned: Spawned, tools: FakeTools, waiting_pid: int, tmp_path: Path, installed: Path
) -> None:
    staged = _stage_build(tmp_path, flavor=X86_64)
    tools.flavor = X86_64
    result = _run(["--json", "--install-update", str(staged), "--app-path", str(installed),
                   "--wait-pid", str(waiting_pid)])
    assert result.exit_code == 0, result.output
    assert _events(result)[0]["ok"] is True
    ((_, env),) = spawned.calls
    assert env["GRAFT_UPDATE_ARCH"] == X86_64


@pytest.mark.parametrize("flavor", ["", "universal", "x86_64h", "arm64; rm -rf /"])
def test_the_helper_is_never_handed_a_build_that_is_not_one(tmp_path: Path, flavor: str) -> None:
    with pytest.raises(ValueError):
        app_update.helper_env(
            pid=2, app=tmp_path / "Graft.app", staged=tmp_path / "s" / "Graft.app", version="2.0.2",
            flavor=flavor, relaunch=False, log=tmp_path / "log", trash=tmp_path / "Trash",
            team="ABCDE12345",
        )


# --- A download's Python, whatever its version ------------------------------------


@pytest.mark.parametrize("name", ["python3.14", "python3.13", "python3.100"])
def test_a_download_on_another_python_is_still_its_build(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path, name: str
) -> None:
    """The first release on a newer Python must not be refused as another
    build: `ARCHITECTURE` is remembered as refused, and every Graft would
    stay where it is."""
    publish_builds(server, tmp_path, {"Graft-Intel.zip": {"flavor": X86_64, "python_name": name}})
    tools.flavor = X86_64
    assert marker(download(installed)[0]) == "Graft-Intel.zip"


@pytest.mark.parametrize(("layout", "said"), [
    ("two Pythons", "more than one embedded Python (python3.13, python3.14)"),
    ("a linked Python", "has no embedded Python"),
    ("a linked bin folder", "reached through a link"),
    ("no Python", "has no embedded Python"),
    ("a Python by another name", "has no embedded Python"),
])
def test_a_download_whose_python_cant_be_told_is_refused(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path, layout: str, said: str
) -> None:
    src = tmp_path / "release-Graft.zip"
    src.mkdir()
    app = dummy_app(src, "2.0.2", marker="Graft.zip")
    bin_dir = app.joinpath(*app_update.EMBEDDED_PYTHON)
    python = bin_dir / "python3.13"
    if layout == "two Pythons":
        (bin_dir / "python3.14").write_bytes(macho_for((ARM64,)))
    elif layout == "a linked Python":
        python.rename(bin_dir / "real")
        python.symlink_to("real")
    elif layout == "a linked bin folder":
        bin_dir.rename(bin_dir.parent / "elsewhere")
        bin_dir.symlink_to("elsewhere")
    elif layout == "no Python":
        python.unlink()
    else:
        python.rename(bin_dir / "python3")
    data = zip_app(app, src / "Graft.zip")
    server.serve_json("/latest", {"tag_name": TAG, "html_url": TAG_PAGE + TAG, "draft": False,
                                  "prerelease": False,
                                  "assets": [asset_json(server.base, TAG, data, "Graft.zip")]})
    server.routes[f"/download/{TAG}/Graft.zip"] = Route(body=data)

    error = failed_download(installed)

    assert error.reason == app_update.ARCHITECTURE
    assert said in error.message
    assert leftovers() == []


# --- An Info.plist plistlib can't read ---------------------------------------------

#: Plists plistlib raises on with something other than its own errors:
#: expat's for XML that isn't well formed, AttributeError for a <date> that
#: isn't one, and a binary plist that ends early.
BROKEN_PLISTS = [
    pytest.param(b'<?xml version="1.0"?><plist version="1.0"><dict><key>CFBundleIdentifier</key>',
                 id="unclosed XML"),
    pytest.param(b'<?xml version="1.0"?><plist version="1.0"><dict><key>a</key><date>soon</date>'
                 b'</dict></plist>', id="a date that isn't one"),
    pytest.param(b"bplist00\xd1\x01\x02", id="a cut-off binary plist"),
]


@pytest.mark.parametrize("data", BROKEN_PLISTS)
def test_a_plist_that_cant_be_read_is_refused_not_a_crash(tmp_path: Path, data: bytes) -> None:
    app = dummy_app(tmp_path, "2.0.2")
    (app / "Contents" / "Info.plist").write_bytes(data)

    assert app_update.bundle_info(app) == app_update.BundleInfo(None, None)
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.check_bundle(app, newer_than="2.0.1")
    assert caught.value.reason == app_update.BUNDLE
    with pytest.raises(app_update.MixedBuild, match="no Info.plist Graft can read"):
        app_update.bundle_flavor(app)
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.check_installable(app)
    assert caught.value.reason == app_update.NOT_GRAFT


@pytest.mark.parametrize("data", BROKEN_PLISTS)
def test_a_download_with_a_plist_that_cant_be_read_is_refused(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path, data: bytes
) -> None:
    src = tmp_path / "release-Graft.zip"
    src.mkdir()
    app = dummy_app(src, "2.0.2", marker="Graft.zip")
    (app / "Contents" / "Info.plist").write_bytes(data)
    zipped = zip_app(app, src / "Graft.zip")
    server.serve_json("/latest", {"tag_name": TAG, "html_url": TAG_PAGE + TAG, "draft": False,
                                  "prerelease": False,
                                  "assets": [asset_json(server.base, TAG, zipped, "Graft.zip")]})
    server.routes[f"/download/{TAG}/Graft.zip"] = Route(body=zipped)

    assert failed_download(installed).reason == app_update.BUNDLE
    assert leftovers() == []
