"""The self-update: `--download-update` and `--install-update` (`app_update`).

What a download has to get past before Graft calls it ready, each check
failing on its own, and that a failure leaves nothing behind. The release is
a fake GitHub answer served from 127.0.0.1 with a zip of a tiny unsigned
dummy Graft.app; codesign and spctl are stand-ins (`FakeTools`). The real
tools are run, read-only, against the installed Graft.app in
`test_app_update_real.py`, and the helper that swaps the app in
`test_app_update_helper.py`.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from graft_ipa import app_update, release_check
from graft_ipa import cli as cli_mod

from .app_update_fakes import (
    RUNNING_APP,
    TEAM,
    FakeTools,
    FixtureServer,
    Route,
    dummy_app,
    release_json,
    zip_app,
)

TAG = "v2.0.2"
ZIP_PATH = f"/download/{TAG}/{app_update.ASSET_NAME}"


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[FixtureServer]:
    """The fixture server, standing in for both GitHub hosts: the API's
    latest release, and the release's files. Plain http, which only a test
    allows."""
    fake = FixtureServer()
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(release_check, "LATEST_RELEASE_URL", fake.base + "/latest")
    monkeypatch.setattr(app_update, "DOWNLOAD_PREFIX", fake.base + "/download/")
    monkeypatch.setattr(app_update, "SCHEMES", frozenset({"http", "https"}))
    yield fake
    fake.close()


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> FakeTools:
    fake = FakeTools()
    # `download_update` looks the runner up when called (its docstring), so
    # the verb, which passes none, gets this one.
    monkeypatch.setattr(app_update, "run_tool", fake)
    # The Graft.app this CLI runs from, whose signature names the team to
    # trust: a test's interpreter runs from no bundle at all.
    monkeypatch.setattr(app_update, "own_bundle", lambda: RUNNING_APP)
    return fake


@pytest.fixture
def installed(tmp_path: Path) -> Path:
    """The running Graft.app, 2.0.1, in a folder of its own."""
    where = tmp_path / "Applications"
    where.mkdir()
    return dummy_app(where, "2.0.1", marker="old")


def publish(server: FixtureServer, tmp_path: Path, *, version: str = "2.0.2",
            bundle_id: str = app_update.BUNDLE_ID, tag: str = TAG, **asset_fields: Any) -> bytes:
    """Serve a release `tag` whose Graft.zip holds a dummy app of `version`.
    The zip's bytes."""
    src = tmp_path / f"release-{version}-{len(server.routes)}"
    src.mkdir()
    data = zip_app(dummy_app(src, version, bundle_id=bundle_id, marker="new"), src / "Graft.zip")
    server.serve_json("/latest", release_json(server.base, tag, data, **asset_fields))
    server.routes[f"/download/{tag}/{app_update.ASSET_NAME}"] = Route(
        body=data, headers={"Content-Type": "application/octet-stream"},
    )
    return data


def download(installed: Path, current: str = "2.0.1") -> tuple[app_update.Staged, list[dict]]:
    events: list[dict] = []
    staged = app_update.download_update(current, installed, progress=events.append)
    return staged, events


def failed_download(installed: Path, current: str = "2.0.1") -> app_update.UpdateError:
    with pytest.raises(app_update.UpdateError) as caught:
        download(installed, current)
    return caught.value


def leftovers() -> list[str]:
    """What the cache's updates folder holds besides its lock."""
    root = app_update.updates_dir()
    return sorted(p.name for p in root.iterdir() if p.name != ".lock") if root.exists() else []


# --- The requirement ----------------------------------------------------------


def test_the_requirement_pins_the_team_and_the_bundle_id_and_developer_id() -> None:
    """Both pins, so another app from the same team doesn't pass, and the
    Developer ID markers, so a development certificate on the team doesn't."""
    req = app_update.requirement(TEAM)
    assert req.startswith("anchor apple generic and ")
    assert 'identifier "com.greenglasst.graft"' in req
    assert f'certificate leaf[subject.OU] = "{TEAM}"' in req
    assert "certificate 1[field.1.2.840.113635.100.6.2.6] exists" in req
    assert "certificate leaf[field.1.2.840.113635.100.6.1.13] exists" in req


@pytest.mark.parametrize("team", ["", "not set", "abcde12345", "ABCDE1234", "ABCDE123456",
                                  'ABCDE12345" or anchor apple', "ABCDE\n2345", "ABCDE\uff112345"])
def test_only_a_developer_id_team_goes_into_the_requirement(team: str) -> None:
    with pytest.raises(ValueError):
        app_update.requirement(team)


def test_no_team_is_written_into_the_source() -> None:
    """The team comes from the running Graft's signature (`own_team`), so a
    fork signed by someone else trusts that someone, and the source names
    nobody's team."""
    assert not hasattr(app_update, "TEAM_ID")
    assert not hasattr(app_update, "REQUIREMENT")


# --- Whose updates --------------------------------------------------------------


def test_the_running_bundle_is_the_one_around_the_interpreter() -> None:
    assert app_update.own_bundle(
        "/Applications/Graft.app/Contents/Resources/GraftPython/bin/python3.13"
    ) == Path("/Applications/Graft.app")
    assert app_update.own_bundle(
        "/Users/someone/Apps/Graft 2.app/Contents/Resources/GraftPython/bin/python3"
    ) == Path("/Users/someone/Apps/Graft 2.app")
    for elsewhere in [
        "/Users/someone/.local/pipx/venvs/graft/bin/python3.13",       # pipx
        "/Applications/Graft.app/Contents/Helpers/python3.13",          # not where Graft keeps it
        "/Applications/Graft.app/Contents/Resources/Other/bin/python3",
        "/Volumes/Example.app/x/Graft.app/Contents/Resources/GraftPython/lib/python3.13",
        "/Applications/Graft/Contents/Resources/GraftPython/bin/python3.13",
        "Graft.app/Contents/Resources/GraftPython/bin/python3.13",      # relative
        "/bin/python3",
    ]:
        assert app_update.own_bundle(elsewhere) is None, elsewhere
    # This test's own interpreter runs from no bundle.
    assert app_update.own_bundle() is None


def test_the_team_is_read_from_the_running_graft_s_own_signature() -> None:
    fake = FakeTools()
    assert app_update.own_team(fake, app=RUNNING_APP) == TEAM
    path = str(RUNNING_APP)
    assert fake.own_calls == [
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", path],
        ["/usr/bin/codesign", "-dv", "--verbose=2", path],
        ["/usr/bin/codesign", "--verify", "--strict", "-R", "=" + app_update.requirement(TEAM), path],
    ]
    fake.own_team = "FGHIJ67890"
    assert app_update.own_team(fake, app=RUNNING_APP) == "FGHIJ67890", "whoever signed it"


@pytest.mark.parametrize(("setting", "value"), [
    ("own_signature_ok", False),       # changed since it was signed
    ("own_team", "not set"),           # ad hoc
    ("own_team", ""),
    ("own_team", "abcde12345"),
    ("own_team", 'ABCDE12345" or anchor apple'),
    ("own_developer_id", False),       # a development build: Apple Development, same team
])
def test_a_graft_with_no_developer_id_team_refuses_to_update_and_says_why(
    setting: str, value: Any
) -> None:
    fake = FakeTools()
    setattr(fake, setting, value)
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.own_team(fake, app=RUNNING_APP)
    assert caught.value.reason == app_update.NO_TEAM
    assert "whose updates to trust" in caught.value.message


def test_a_graft_outside_an_app_refuses_to_update() -> None:
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.own_team(FakeTools())
    assert caught.value.reason == app_update.NO_TEAM
    assert "Only Graft.app updates itself" in caught.value.message


def test_a_graft_with_no_team_downloads_nothing(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path
) -> None:
    publish(server, tmp_path)
    tools.own_developer_id = False
    assert failed_download(installed).reason == app_update.NO_TEAM
    assert server.requests == [], "not even the check: nothing is asked for"
    assert tools.calls == []


def test_the_download_must_be_signed_by_the_running_graft_s_team(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path
) -> None:
    """A Developer ID Graft from another team is refused, though it is
    notarized and Graft's bundle id, and so would pass every other check."""
    publish(server, tmp_path)
    tools.signed_by = "FGHIJ67890"
    assert failed_download(installed).reason == app_update.REQUIREMENT_FAILED
    assert leftovers() == []

    tools.own_team = "FGHIJ67890"
    staged, _ = download(installed)
    assert staged.version == "2.0.2", "the same download, from a Graft that team signed"


def test_the_app_path_s_signature_is_never_asked_whose_it_is(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path
) -> None:
    """`--app-path` says where to install, not whom to trust."""
    publish(server, tmp_path)
    download(installed)
    assert tools.own_calls, "the running Graft's was"
    assert not [argv for argv in tools.calls if str(installed) in argv]


# --- The release's asset ------------------------------------------------------


def _release(**asset_fields: Any) -> dict[str, Any]:
    return release_json("https://github.com/GreenglassT/Graft/releases", TAG, b"zip bytes", **asset_fields)


def test_the_asset_is_read_off_the_release() -> None:
    asset = app_update.release_asset(_release())
    assert asset == app_update.Asset(
        url=f"https://github.com/GreenglassT/Graft/releases/download/{TAG}/Graft.zip",
        size=len(b"zip bytes"), sha256=hashlib.sha256(b"zip bytes").hexdigest(),
    )


@pytest.mark.parametrize(("fields", "reason"), [
    ({"digest": None}, app_update.DIGEST),
    ({"digest": "sha512:" + "a" * 128}, app_update.DIGEST),
    ({"digest": "sha256:" + "A" * 64}, app_update.DIGEST),
    ({"digest": "sha256:abc"}, app_update.DIGEST),
    ({"size": 0}, app_update.NO_ASSET),
    ({"size": True}, app_update.NO_ASSET),
    ({"size": "9"}, app_update.NO_ASSET),
    ({"size": app_update.MAX_ZIP_BYTES + 1}, app_update.NO_ASSET),
    ({"state": "starter"}, app_update.NO_ASSET),
    ({"browser_download_url": "https://example.invalid/Graft.zip"}, app_update.NO_ASSET),
    ({"browser_download_url": f"https://github.com/GreenglassT/Graft/releases/download/{TAG}/Other.zip"},
     app_update.NO_ASSET),
])
def test_an_asset_without_a_usable_url_size_or_digest_is_refused(fields: dict, reason: str) -> None:
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.release_asset(_release(**fields))
    assert caught.value.reason == reason


def test_no_graft_zip_or_two_of_them_is_no_asset() -> None:
    data = _release()
    data["assets"] = [a for a in data["assets"] if a["name"] != "Graft.zip"]
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.release_asset(data)
    assert caught.value.reason == app_update.NO_ASSET

    data = _release()
    data["assets"].append(dict(data["assets"][1]))
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.release_asset(data)
    assert caught.value.reason == app_update.NO_ASSET


def test_an_answer_without_assets_is_malformed() -> None:
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.release_asset({"tag_name": TAG})
    assert caught.value.reason == app_update.MALFORMED


# --- The zip's listing --------------------------------------------------------


def test_a_zip_made_the_way_the_release_is_passes(tmp_path: Path) -> None:
    data = zip_app(dummy_app(tmp_path, "2.0.2"), tmp_path / "Graft.zip")
    assert app_update.check_listing(tmp_path / "Graft.zip") > 0
    assert data


def _zip(tmp_path: Path, entries: list[tuple[str, bytes, int]]) -> Path:
    """A zip of `entries`: (name, data, unix mode)."""
    path = tmp_path / "crafted.zip"
    with zipfile.ZipFile(path, "w") as z:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            z.writestr(info, data)
    return path


FILE = stat.S_IFREG | 0o644
LINK = stat.S_IFLNK | 0o755


@pytest.mark.parametrize("entries", [
    [("Graft.app/Contents/Info.plist", b"x", FILE), ("Other.app/Contents/MacOS/x", b"x", FILE)],
    [("../Graft.app/x", b"x", FILE)],
    [("Graft.app/../../x", b"x", FILE)],
    [("/Graft.app/x", b"x", FILE)],
    [("Graft.app//x", b"x", FILE)],
    [("Graft.app\\..\\x", b"x", FILE)],
    # A link out of the bundle, absolute or climbing.
    [("Graft.app/Contents/out", b"/Users", LINK)],
    [("Graft.app/Contents/out", b"../../..", LINK)],
    # A file stored through a link, even one that stays inside: ditto would
    # write it wherever the link points.
    [("Graft.app/Contents/in", b"MacOS", LINK), ("Graft.app/Contents/in/x", b"x", FILE)],
])
def test_an_entry_that_could_land_outside_the_app_is_refused(tmp_path: Path, entries: list) -> None:
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.check_listing(_zip(tmp_path, entries))
    assert caught.value.reason == app_update.ARCHIVE


def test_a_link_inside_the_app_is_fine(tmp_path: Path) -> None:
    path = _zip(tmp_path, [
        ("Graft.app/Contents/Frameworks/X.framework/Versions/A/X", b"x", FILE),
        ("Graft.app/Contents/Frameworks/X.framework/Versions/Current", b"A", LINK),
        ("Graft.app/Contents/Frameworks/X.framework/X", b"Versions/Current/X", LINK),
    ])
    assert app_update.check_listing(path) == 1 + 1 + len(b"Versions/Current/X")


def test_something_that_is_not_a_zip_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "Graft.zip"
    path.write_bytes(b"<html>not a zip</html>")
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.check_listing(path)
    assert caught.value.reason == app_update.ARCHIVE


def test_a_zip_that_unpacks_too_big_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    zip_app(dummy_app(tmp_path, "2.0.2"), tmp_path / "Graft.zip")
    monkeypatch.setattr(app_update, "MAX_UNPACKED_BYTES", 10)
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.check_listing(tmp_path / "Graft.zip")
    assert caught.value.reason == app_update.ARCHIVE


# --- The unpacked app ---------------------------------------------------------


def test_the_bundle_must_be_graft_and_newer(tmp_path: Path) -> None:
    good = dummy_app(tmp_path / "good", "2.0.2")
    assert app_update.check_bundle(good, newer_than="2.0.1") == "2.0.2"
    assert app_update.check_bundle(good, newer_than="2.0") == "2.0.2"

    other = dummy_app(tmp_path / "other", "9.0", bundle_id="com.example.other")
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.check_bundle(other, newer_than="2.0.1")
    assert caught.value.reason == app_update.BUNDLE

    for current in ("2.0.2", "2.0.2.0", "2.1"):
        with pytest.raises(app_update.UpdateError) as caught:
            app_update.check_bundle(good, newer_than=current)
        assert caught.value.reason == app_update.NOT_NEWER, current

    odd = dummy_app(tmp_path / "odd", "2.0.2-beta")
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.check_bundle(odd, newer_than="2.0.1")
    assert caught.value.reason == app_update.BUNDLE


@pytest.mark.parametrize(("setting", "reason"), [
    ("signature_ok", app_update.SIGNATURE),
    ("requirement_ok", app_update.REQUIREMENT_FAILED),
    ("stapled", app_update.NOTARIZATION),
    ("gatekeeper_ok", app_update.NOTARIZATION),
])
def test_each_signature_check_fails_on_its_own(tmp_path: Path, setting: str, reason: str) -> None:
    app = dummy_app(tmp_path, "2.0.2")
    fake = FakeTools()
    setattr(fake, setting, False)
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.verify_app(app, newer_than="2.0.1", team=TEAM, run=fake, progress=lambda _: None)
    assert caught.value.reason == reason


def test_developer_id_that_is_not_notarized_is_refused(tmp_path: Path) -> None:
    app = dummy_app(tmp_path, "2.0.2")
    fake = FakeTools()
    fake.source = "Developer ID"
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.verify_app(app, newer_than="2.0.1", team=TEAM, run=fake, progress=lambda _: None)
    assert caught.value.reason == app_update.NOTARIZATION


def test_the_checks_run_in_order_with_the_pinned_requirement(tmp_path: Path) -> None:
    app = dummy_app(tmp_path, "2.0.2")
    fake = FakeTools()
    events: list[dict] = []
    assert app_update.verify_app(app, newer_than="2.0.1", team=TEAM, run=fake, progress=events.append) == "2.0.2"
    assert fake.tools_run() == ["signature", "requirement", "stapled", "spctl"]
    assert [e["check"] for e in events] == ["bundle", "signature", "requirement", "notarization"]
    signature, requirement, stapled, gatekeeper = fake.calls
    assert signature == ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)]
    assert requirement == ["/usr/bin/codesign", "--verify", "--strict", "-R", "=" + app_update.requirement(TEAM),
                           str(app)]
    assert stapled == ["/usr/bin/codesign", "-dvv", str(app)]
    assert gatekeeper == ["/usr/sbin/spctl", "-a", "-t", "exec", "-vv", str(app)]


def test_a_failed_check_stops_the_rest(tmp_path: Path) -> None:
    app = dummy_app(tmp_path, "2.0.2")
    fake = FakeTools()
    fake.signature_ok = False
    with pytest.raises(app_update.UpdateError):
        app_update.verify_app(app, newer_than="2.0.1", team=TEAM, run=fake, progress=lambda _: None)
    assert fake.tools_run() == ["signature"]


def test_a_tool_that_will_not_run_is_a_failed_check(tmp_path: Path) -> None:
    app = dummy_app(tmp_path, "2.0.2")
    ran = app_update.run_tool([str(tmp_path / "no-such-tool"), "--verify"])
    assert ran.returncode != 0
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.check_signature(app, lambda argv: ran)
    assert caught.value.reason == app_update.SIGNATURE


# --- Where it can be installed ------------------------------------------------


def test_a_graft_in_a_folder_this_user_can_change_is_installable(installed: Path) -> None:
    app_update.check_installable(installed)


def test_a_translocated_graft_is_refused(tmp_path: Path) -> None:
    app = dummy_app(tmp_path / "AppTranslocation" / "0D1E2F" / "d", "2.0.1")
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.check_installable(app)
    assert caught.value.reason == app_update.TRANSLOCATED
    assert "Applications folder" in caught.value.message


def test_a_graft_in_a_folder_this_user_cannot_change_is_refused(installed: Path) -> None:
    folder = installed.parent
    folder.chmod(0o555)
    try:
        with pytest.raises(app_update.UpdateError) as caught:
            app_update.check_installable(installed)
    finally:
        folder.chmod(0o755)
    assert caught.value.reason == app_update.NOT_WRITABLE
    assert str(folder) in caught.value.message


def test_something_that_is_not_graft_is_never_replaced(tmp_path: Path, installed: Path) -> None:
    other = dummy_app(tmp_path / "other", "1.0", bundle_id="com.example.other")
    link = tmp_path / "link" / "Graft.app"
    link.parent.mkdir()
    link.symlink_to(installed)
    for app in (other, link, Path("Applications/Graft.app"), installed.parent):
        with pytest.raises(app_update.UpdateError) as caught:
            app_update.check_installable(app)
        assert caught.value.reason == app_update.NOT_GRAFT, app


# --- The whole download -------------------------------------------------------


def test_a_newer_release_is_downloaded_checked_and_kept(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path
) -> None:
    data = publish(server, tmp_path)

    staged, events = download(installed)

    assert staged.version == "2.0.2"
    assert staged.url == "https://github.com/GreenglassT/Graft/releases/tag/" + TAG
    assert staged.path.name == "Graft.app"
    assert (staged.path / "Contents" / "Resources" / "marker.txt").read_text() == "new"
    assert (staged.path / "Contents/Frameworks/Example.framework/Versions/Current").is_symlink()
    root = app_update.updates_dir()
    assert root in staged.path.parents
    # Owner-only all the way down to the app, and nothing else kept: the zip
    # goes once it's unpacked.
    for folder in (root, staged.path.parent.parent, staged.path.parent):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700, folder
    assert staged.path.parent.parent.name.endswith(".noindex")
    assert sorted(p.name for p in staged.path.parent.parent.iterdir()) == ["app"]

    downloads = [e for e in events if e["step"] == "download"]
    assert downloads[0]["received"] == 0
    assert downloads[-1]["received"] == downloads[-1]["total"] == len(data)
    assert all(e["version"] == "2.0.2" for e in events)
    assert [e["check"] for e in events if e["step"] == "verify"] == list(app_update.CHECKS)
    assert tools.tools_run() == ["ditto", "signature", "requirement", "stapled", "spctl"]
    assert server.paths() == ["/latest", ZIP_PATH]


def test_what_the_download_sends(server: FixtureServer, tools: FakeTools, tmp_path: Path,
                                 installed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The check's generic User-Agent and nothing that identifies anyone: not
    a `~/.netrc` login, which requests would otherwise add."""
    publish(server, tmp_path)
    netrc = tmp_path / "netrc"
    netrc.write_text("machine 127.0.0.1 login someone password not-a-real-token\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))

    download(installed)

    for path, headers in server.requests:
        assert "Authorization" not in headers, path
        assert headers["User-Agent"] == "Graft"
    _, zip_headers = server.requests[-1]
    assert zip_headers["Accept-Encoding"] == "identity"
    assert zip_headers["Accept"] == "application/octet-stream"


def test_the_redirect_to_the_download_host_is_followed(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path
) -> None:
    data = publish(server, tmp_path)
    server.routes["/assets/blob"] = Route(body=data)
    server.routes[ZIP_PATH] = Route(status=302, headers={"Location": "/assets/blob?sig=x"})
    server.routes["/assets/blob?sig=x"] = Route(body=data)

    staged, _ = download(installed)

    assert staged.version == "2.0.2"
    assert server.paths() == ["/latest", ZIP_PATH, "/assets/blob?sig=x"]


def test_a_redirect_off_https_is_refused(server: FixtureServer, tools: FakeTools,
                                         tmp_path: Path, installed: Path) -> None:
    publish(server, tmp_path)
    server.routes[ZIP_PATH] = Route(status=302, headers={"Location": "ftp://example.invalid/Graft.zip"})
    assert failed_download(installed).reason == app_update.HTTP
    assert leftovers() == []


def test_endless_redirects_stop(server: FixtureServer, tools: FakeTools, tmp_path: Path,
                                installed: Path) -> None:
    publish(server, tmp_path)
    server.routes[ZIP_PATH] = Route(status=302, headers={"Location": ZIP_PATH})
    assert failed_download(installed).reason == app_update.HTTP
    assert server.paths().count(ZIP_PATH) == app_update.MAX_REDIRECTS + 1


def test_bytes_that_do_not_match_the_digest_are_deleted(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path
) -> None:
    data = publish(server, tmp_path)
    server.routes[ZIP_PATH] = Route(body=bytes([data[0] ^ 1]) + data[1:])

    error = failed_download(installed)

    assert error.reason == app_update.DIGEST
    assert error.url == "https://github.com/GreenglassT/Graft/releases/tag/" + TAG
    assert leftovers() == []
    assert tools.calls == [], "nothing is unpacked or checked past a bad digest"


def test_a_release_page_that_climbs_out_of_the_releases_is_never_named(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path
) -> None:
    """The hint and the event's `url` name this release's page or the
    releases' /latest, nothing else (`release_check.release_page`)."""
    data = publish(server, tmp_path)
    answer = release_json(server.base, TAG, data)
    answer["html_url"] = f"https://github.com/GreenglassT/Graft/releases/tag/{TAG}/../../../../../example/phish"
    server.serve_json("/latest", answer)
    server.routes[ZIP_PATH] = Route(status=404)

    error = failed_download(installed)

    assert error.url == release_check.RELEASES_PAGE + "/latest"
    assert "phish" not in str(error)


@pytest.mark.parametrize(("body", "reason"), [
    (lambda data: data + b"more", app_update.DIGEST),
    (lambda data: data[:-10], app_update.OFFLINE),
])
def test_a_download_of_the_wrong_size_is_deleted(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path, body: Any, reason: str
) -> None:
    data = publish(server, tmp_path)
    server.routes[ZIP_PATH] = Route(body=body(data))
    assert failed_download(installed).reason == reason
    assert leftovers() == []


def test_an_http_error_on_the_file_is_a_failure(server: FixtureServer, tools: FakeTools,
                                                tmp_path: Path, installed: Path) -> None:
    publish(server, tmp_path)
    server.routes[ZIP_PATH] = Route(status=404)
    assert failed_download(installed).reason == app_update.HTTP
    assert leftovers() == []


@pytest.mark.parametrize(("setting", "reason"), [
    ("signature_ok", app_update.SIGNATURE),
    ("requirement_ok", app_update.REQUIREMENT_FAILED),
    ("stapled", app_update.NOTARIZATION),
    ("gatekeeper_ok", app_update.NOTARIZATION),
])
def test_a_download_that_fails_a_check_is_deleted(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path, setting: str, reason: str
) -> None:
    publish(server, tmp_path)
    setattr(tools, setting, False)
    error = failed_download(installed)
    assert error.reason == reason
    assert "download it from https://github.com/GreenglassT/Graft/releases/tag/" in str(error)
    assert leftovers() == []


def test_an_app_from_the_same_team_under_another_bundle_id_is_deleted(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path
) -> None:
    publish(server, tmp_path, bundle_id="com.example.other")
    assert failed_download(installed).reason == app_update.BUNDLE
    assert leftovers() == []


def test_a_release_whose_app_is_not_newer_is_deleted(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path
) -> None:
    """The tag says 2.0.2; the app inside says 2.0.1. The app's own version
    is the one that counts."""
    publish(server, tmp_path, version="2.0.1")
    assert failed_download(installed).reason == app_update.NOT_NEWER
    assert leftovers() == []


def test_a_release_that_is_not_newer_downloads_nothing(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path
) -> None:
    publish(server, tmp_path)
    assert failed_download(installed, current="2.0.2").reason == app_update.NOT_NEWER
    assert server.paths() == ["/latest"]


def test_a_translocated_graft_downloads_nothing(server: FixtureServer, tools: FakeTools,
                                                tmp_path: Path) -> None:
    publish(server, tmp_path)
    app = dummy_app(tmp_path / "AppTranslocation" / "0D1E2F" / "d", "2.0.1")
    assert failed_download(app).reason == app_update.TRANSLOCATED
    assert server.paths() == []


def test_an_earlier_download_is_replaced(server: FixtureServer, tools: FakeTools,
                                         tmp_path: Path, installed: Path) -> None:
    publish(server, tmp_path)
    first, _ = download(installed)
    second, _ = download(installed)
    assert not first.path.exists()
    assert second.path.exists()
    assert leftovers() == [second.path.parent.parent.name]


def test_no_room_downloads_nothing(server: FixtureServer, tools: FakeTools, tmp_path: Path,
                                   installed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    publish(server, tmp_path)
    real = app_update.shutil.disk_usage
    monkeypatch.setattr(app_update.shutil, "disk_usage",
                        lambda path: real(path)._replace(free=1000))
    assert failed_download(installed).reason == app_update.DISK
    assert server.paths() == ["/latest"]


def test_a_second_download_waits_for_the_first(server: FixtureServer, tools: FakeTools, tmp_path: Path,
                                               installed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fcntl

    publish(server, tmp_path)
    root = app_update.updates_dir()
    root.mkdir(parents=True)
    fd = os.open(root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    monkeypatch.setattr(app_update, "LOCK_WAIT", 0.3)
    try:
        assert failed_download(installed).reason == app_update.BUSY
    finally:
        os.close(fd)
    assert ZIP_PATH not in server.paths()


# --- The verbs ----------------------------------------------------------------


def _run(argv: list[str]) -> Any:
    return CliRunner().invoke(cli_mod.app, argv)


def _events(result: Any) -> list[dict]:
    return [json.loads(line) for line in result.stdout.splitlines()]


def test_download_update_emits_progress_then_its_answer(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path
) -> None:
    publish(server, tmp_path)
    result = _run(["--json", "--download-update", "--current-version", "2.0.1",
                   "--app-path", str(installed)])

    assert result.exit_code == 0, result.output
    events = _events(result)
    assert {e["type"] for e in events[:-1]} == {"update-progress"}
    for e in events[:-1]:
        assert set(e) == {"type", "step", "version", "received", "total", "check"}
    end = events[-1]
    assert end["type"] == "update-download-end"
    assert end["ok"] is True
    assert (end["current"], end["version"]) == ("2.0.1", "2.0.2")
    assert Path(end["app_path"]).is_dir()
    assert end["url"] == "https://github.com/GreenglassT/Graft/releases/tag/" + TAG
    assert (end["reason"], end["message"]) == (None, None)


def test_a_failed_download_says_why_in_the_event_and_the_exit_status(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path
) -> None:
    publish(server, tmp_path)
    tools.requirement_ok = False
    result = _run(["--json", "--download-update", "--current-version", "2.0.1",
                   "--app-path", str(installed)])

    assert result.exit_code == 1
    end = _events(result)[-1]
    assert end == {
        "type": "update-download-end", "ok": False, "current": "2.0.1", "version": None,
        "app_path": None, "url": "https://github.com/GreenglassT/Graft/releases/tag/" + TAG,
        "reason": "requirement", "message": end["message"],
    }
    assert "isn't signed by Graft's developer" in end["message"]
    assert " ".join(end["message"].split()) in " ".join(result.stderr.split())
    assert leftovers() == []


def test_download_update_outside_an_app_needs_the_app_path(server: FixtureServer) -> None:
    result = _run(["--json", "--download-update", "--current-version", "2.0.1"])
    assert result.exit_code == 2
    assert "--app-path" in result.stderr
    assert server.requests == []


@pytest.mark.parametrize("flags", [
    ["--app-path", "/Applications/Graft.app"],
    ["--wait-pid", "123"],
    ["--relaunch"],
])
def test_the_update_modifiers_are_refused_beside_another_verb(
    server: FixtureServer, flags: list[str]
) -> None:
    result = _run(["--json", "--check-update", *flags])
    assert result.exit_code == 2
    assert f"{flags[0]} is not valid with --check-update" in " ".join(result.stderr.split())
    assert server.requests == []


class Spawned:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, argv: list[str], env: dict[str, str]) -> None:
        self.calls.append((argv, env))


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch, tools: FakeTools) -> Spawned:
    fake = Spawned()
    monkeypatch.setattr(app_update, "spawn_detached", fake)
    return fake


@pytest.fixture
def waiting_pid() -> Iterator[int]:
    """A process to wait for, standing in for the running Graft."""
    sleeper = subprocess.Popen(["/bin/sleep", "30"])
    yield sleeper.pid
    sleeper.kill()
    sleeper.wait()


def _stage(tmp_path: Path, version: str = "2.0.2") -> Path:
    """A download in the updates folder, as `download_update` leaves one."""
    work = app_update.updates_dir() / "Graft-test.noindex" / "app"
    work.mkdir(parents=True)
    return dummy_app(work, version, marker="new")


def test_install_update_starts_the_helper_and_says_so(
    spawned: Spawned, waiting_pid: int, tmp_path: Path, installed: Path
) -> None:
    staged = _stage(tmp_path)
    result = _run(["--json", "--install-update", str(staged), "--app-path", str(installed),
                   "--wait-pid", str(waiting_pid), "--relaunch"])

    assert result.exit_code == 0, result.output
    log = Path.home() / "Library" / "Logs" / "Graft" / "update.log"
    assert _events(result) == [{
        "type": "update-install", "ok": True, "version": "2.0.2", "log": str(log),
        "relaunch": True, "url": None, "reason": None, "message": None,
    }]
    assert log.parent.is_dir()
    ((argv, env),) = spawned.calls
    assert argv == ["/bin/sh", "-c", app_update.HELPER, "graft-update-helper"]
    assert env == app_update.helper_env(
        pid=waiting_pid, app=installed, staged=staged, version="2.0.2", relaunch=True,
        log=log, trash=Path.home() / ".Trash", team=TEAM,
    )
    assert env["GRAFT_UPDATE_REQUIREMENT"] == app_update.requirement(TEAM)
    assert env["GRAFT_UPDATE_RELAUNCH"] == "1"
    # Nothing moved yet: that is the helper's, once Graft has quit.
    assert (installed / "Contents/Resources/marker.txt").read_text() == "old"
    assert staged.is_dir()


def test_without_relaunch_the_helper_does_not_reopen(
    spawned: Spawned, waiting_pid: int, tmp_path: Path, installed: Path
) -> None:
    staged = _stage(tmp_path)
    result = _run(["--json", "--install-update", str(staged), "--app-path", str(installed),
                   "--wait-pid", str(waiting_pid)])
    assert result.exit_code == 0, result.output
    assert _events(result)[0]["relaunch"] is False
    ((_, env),) = spawned.calls
    assert env["GRAFT_UPDATE_RELAUNCH"] == "0"


def _refused(result: Any, reason: str, spawned: Spawned) -> None:
    assert result.exit_code == 1, result.output
    (event,) = _events(result)
    assert (event["type"], event["ok"], event["reason"]) == ("update-install", False, reason)
    assert event["message"]
    assert spawned.calls == [], "nothing is started when the install is refused"


def test_a_graft_with_no_team_starts_no_helper(
    spawned: Spawned, tools: FakeTools, waiting_pid: int, tmp_path: Path, installed: Path
) -> None:
    """The helper checks the copy against the running Graft's team, so with
    none there is nothing to check it against."""
    staged = _stage(tmp_path)
    tools.own_team = "not set"
    result = _run(["--json", "--install-update", str(staged), "--app-path", str(installed),
                   "--wait-pid", str(waiting_pid)])
    _refused(result, app_update.NO_TEAM, spawned)


def test_a_staged_app_outside_the_updates_folder_is_refused(
    spawned: Spawned, waiting_pid: int, tmp_path: Path, installed: Path
) -> None:
    elsewhere = dummy_app(tmp_path / "elsewhere", "2.0.2")
    result = _run(["--json", "--install-update", str(elsewhere), "--app-path", str(installed),
                   "--wait-pid", str(waiting_pid)])
    _refused(result, app_update.NOT_STAGED, spawned)

    # Nor a link into it from outside.
    staged = _stage(tmp_path)
    link = tmp_path / "link" / "Graft.app"
    link.parent.mkdir()
    link.symlink_to(staged)
    result = _run(["--json", "--install-update", str(link), "--app-path", str(installed),
                   "--wait-pid", str(waiting_pid)])
    _refused(result, app_update.NOT_STAGED, spawned)


def test_a_staged_app_that_is_not_newer_than_the_installed_one_is_refused(
    spawned: Spawned, waiting_pid: int, tmp_path: Path
) -> None:
    """Someone installed 2.0.3 by hand since the download: 2.0.2 isn't an
    update over it."""
    where = tmp_path / "Applications"
    where.mkdir()
    newer = dummy_app(where, "2.0.3")
    staged = _stage(tmp_path)
    result = _run(["--json", "--install-update", str(staged), "--app-path", str(newer),
                   "--wait-pid", str(waiting_pid)])
    _refused(result, app_update.NOT_NEWER, spawned)


def test_install_into_a_translocated_or_read_only_graft_is_refused(
    spawned: Spawned, waiting_pid: int, tmp_path: Path, installed: Path
) -> None:
    staged = _stage(tmp_path)
    moved = dummy_app(tmp_path / "AppTranslocation" / "0D1E2F" / "d", "2.0.1")
    result = _run(["--json", "--install-update", str(staged), "--app-path", str(moved),
                   "--wait-pid", str(waiting_pid)])
    _refused(result, app_update.TRANSLOCATED, spawned)

    installed.parent.chmod(0o555)
    try:
        result = _run(["--json", "--install-update", str(staged), "--app-path", str(installed),
                       "--wait-pid", str(waiting_pid)])
    finally:
        installed.parent.chmod(0o755)
    _refused(result, app_update.NOT_WRITABLE, spawned)


def test_install_waits_only_for_a_running_process(
    spawned: Spawned, tmp_path: Path, installed: Path
) -> None:
    staged = _stage(tmp_path)
    gone = subprocess.Popen(["/usr/bin/true"])
    gone.wait()
    result = _run(["--json", "--install-update", str(staged), "--app-path", str(installed),
                   "--wait-pid", str(gone.pid)])
    _refused(result, app_update.HELPER_FAILED, spawned)

    result = _run(["--json", "--install-update", str(staged), "--app-path", str(installed)])
    assert result.exit_code == 2
    assert "--wait-pid" in result.stderr
    assert spawned.calls == []


def test_a_helper_that_will_not_start_is_a_failure(
    monkeypatch: pytest.MonkeyPatch, tools: FakeTools, waiting_pid: int, tmp_path: Path, installed: Path
) -> None:
    def broken(argv: list[str], env: dict[str, str]) -> None:
        raise OSError(7, "Argument list too long")

    monkeypatch.setattr(app_update, "spawn_detached", broken)
    staged = _stage(tmp_path)
    result = _run(["--json", "--install-update", str(staged), "--app-path", str(installed),
                   "--wait-pid", str(waiting_pid)])
    assert result.exit_code == 1
    (event,) = _events(result)
    assert (event["ok"], event["reason"]) == (False, app_update.HELPER_FAILED)


def test_the_real_spawn_detaches_and_holds_no_stream(tmp_path: Path) -> None:
    """The helper must not hold the CLI's stdout: the app reads it to its
    end, and a helper still holding it would keep the app waiting for a
    helper that waits for the app. A CLI that starts one the real way
    reaches its end at once, while the helper sleeps on, in a process group
    of its own."""
    import sys
    import time

    pid_file = tmp_path / "helper.pid"
    script = (
        "import sys; from graft_ipa import app_update; "
        "app_update.spawn_detached(['/bin/sh', '-c', 'echo $$ $(ps -o pgid= -p $$) > \"$OUT\"; exec sleep 5'],"
        " {'PATH': app_update.SYSTEM_PATH, 'OUT': sys.argv[1]}); print('started')"
    )
    began = time.monotonic()
    done = subprocess.run([sys.executable, "-c", script, str(pid_file)], capture_output=True,
                          text=True, timeout=4, cwd=Path(__file__).parent.parent)
    assert done.returncode == 0, done.stderr
    assert done.stdout == "started\n"
    assert time.monotonic() - began < 4
    for _ in range(100):
        if pid_file.exists() and len(pid_file.read_text().split()) == 2:
            break
        time.sleep(0.05)
    pid, group = (int(x) for x in pid_file.read_text().split())
    try:
        assert group == pid, "the helper leads a process group (and session) of its own"
        os.kill(pid, 0)  # still sleeping: the CLI didn't wait for it
    finally:
        os.kill(pid, 15)


def test_the_helper_gets_no_secret_and_its_own_variables_win(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The helper starts over the CLI's environment without Graft's secrets
    (`child_env`), with its own variables on top."""
    import time

    monkeypatch.setenv("GRAFT_SSH_PASS", "not-a-real-password")
    monkeypatch.setenv("GRAFT_UPDATE_APP", "/somewhere/else.app")
    out = tmp_path / "seen"
    app_update.spawn_detached(
        ["/bin/sh", "-c", 'echo "${GRAFT_SSH_PASS:-none} $GRAFT_UPDATE_APP $PATH" > "$OUT"'],
        {"PATH": app_update.SYSTEM_PATH, "OUT": str(out), "GRAFT_UPDATE_APP": "/ours/Graft.app"},
    )
    for _ in range(100):
        if out.exists() and out.read_text().strip():
            break
        time.sleep(0.05)
    assert out.read_text().split() == ["none", "/ours/Graft.app", app_update.SYSTEM_PATH]


# --- What the app reads -------------------------------------------------------


def test_the_app_does_not_retry_what_no_retry_can_fix() -> None:
    """`UpdateStore.refusedForWhatItIs` (Swift) names the reasons a download
    isn't tried again on its own for: each must be one this module sends,
    and the running Graft having no team (`NO_TEAM`) is one, since retrying
    can't give it one. Read with comments stripped, so a reason named only
    in prose doesn't count."""
    import re

    from tests.cli_ast import REPO_ROOT, strip_swift_comments

    swift = strip_swift_comments(
        (REPO_ROOT / "Graft" / "Graft" / "Services" / "UpdateStore.swift").read_text()
    )
    found = re.search(r"static let refusedForWhatItIs: Set<String> = \[(.*?)\]", swift, re.S)
    assert found, "refusedForWhatItIs is gone from UpdateStore.swift"
    named = set(re.findall(r'"([^"]+)"', found.group(1)))
    assert named == {
        app_update.NO_ASSET, app_update.DIGEST, app_update.ARCHIVE, app_update.BUNDLE,
        app_update.SIGNATURE, app_update.REQUIREMENT_FAILED, app_update.NOTARIZATION,
        app_update.NOT_NEWER, app_update.NO_TEAM,
    }


def test_the_app_offers_try_again_only_where_trying_again_can_help() -> None:
    """`UpdateStore.retryCantHelp` (Swift) names the reasons Check for
    Updates… offers no Try Again for: those `refusedForWhatItIs` names, and
    where the running copy is, which no retry changes. Each of the latter
    must be one this module sends. Read with comments stripped."""
    import re

    from tests.cli_ast import REPO_ROOT, strip_swift_comments

    swift = strip_swift_comments(
        (REPO_ROOT / "Graft" / "Graft" / "Services" / "UpdateStore.swift").read_text()
    )
    found = re.search(
        r"static let retryCantHelp: Set<String> = refusedForWhatItIs\.union\(\[(.*?)\]\)", swift, re.S,
    )
    assert found, "retryCantHelp is gone from UpdateStore.swift, or isn't refusedForWhatItIs and more"
    named = set(re.findall(r'"([^"]+)"', found.group(1)))
    assert named == {app_update.TRANSLOCATED, app_update.NOT_WRITABLE, app_update.NOT_GRAFT}
