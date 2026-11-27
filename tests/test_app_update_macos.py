"""An update that needs a newer macOS than this Mac has is no update for
this Mac: its Info.plist's `LSMinimumSystemVersion` (`app_update.MACOS_KEY`)
is checked against the running macOS (`app_update.running_macos`), which
every test here sets (`FakeTools.macos`), never the host's.

Refused as `NOT_FOR_THIS_MAC`, like a release without this build's zip: the
app shows the sentence and keeps the version it has, and the next check
finds the same zip refused before downloading it again
(`app_update.MACOS_NOTE`), until this Mac has that macOS or the release has
another zip.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from graft_ipa import app_update

from .app_update_fakes import (
    TAG_PAGE,
    FakeTools,
    FixtureServer,
    Route,
    dummy_app,
    release_json,
    zip_app,
)
from .test_app_update import (  # noqa: F401 (fixtures)
    TAG,
    ZIP_PATH,
    _events,
    _run,
    download,
    failed_download,
    installed,
    leftovers,
    server,
    tools,
)


def publish_floor(server: FixtureServer, tmp_path: Path, minimum: Any, *,
                  version: str = "2.0.2") -> bytes:
    """Serve release TAG whose Graft.zip holds a dummy Graft `version` with
    `minimum` as its LSMinimumSystemVersion (none for None). The zip's
    bytes."""
    src = tmp_path / f"release-{len(server.routes)}"
    src.mkdir()
    data = zip_app(dummy_app(src, version, marker=str(minimum), minimum_macos=minimum),
                   src / "Graft.zip")
    server.serve_json("/latest", release_json(server.base, TAG, data))
    server.routes[ZIP_PATH] = Route(body=data, headers={"Content-Type": "application/octet-stream"})
    return data


def downloads(server: FixtureServer) -> int:
    """How many times the zip was downloaded."""
    return server.paths().count(ZIP_PATH)


# --- Reading the versions --------------------------------------------------------


@pytest.mark.parametrize(("text", "parsed"), [
    ("14.0", (14, 0, 0)),
    ("14", (14, 0, 0)),
    ("13.7.1", (13, 7, 1)),
    ("14.10", (14, 10, 0)),
    ("26.1", (26, 1, 0)),
])
def test_a_macos_version_is_read_as_numbers(text: str, parsed: tuple[int, int, int]) -> None:
    assert app_update.macos_version(text) == parsed


@pytest.mark.parametrize("text", [None, 15, 15.0, "", "fifteen", "15.0.0.1", "15.", ".15", "15.x",
                                  " 15.0", "15.0\n", "\uff11\uff15"])
def test_anything_else_is_no_macos_version(text: Any) -> None:
    assert app_update.macos_version(text) is None


@pytest.mark.parametrize(("said", "read"), [
    ("14.6.1", "14.6.1"),
    ("26.1", "26.1"),
    # What macOS tells a program that asks as an old one would
    # (SYSTEM_VERSION_COMPAT): never a macOS Graft runs on.
    ("10.16", None),
    ("", None),
    ("not a version", None),
])
def test_the_running_macos_is_platform_s_answer_when_it_is_one(
    monkeypatch: pytest.MonkeyPatch, said: str, read: str | None
) -> None:
    import platform

    monkeypatch.setattr(platform, "mac_ver", lambda: (said, ("", "", ""), "arm64"))
    assert app_update.running_macos() == read


# --- The check --------------------------------------------------------------------


@pytest.mark.parametrize(("running", "minimum"), [
    ("14.6", "15.0"),
    ("13.7.1", "14.0"),
    ("14.6", "14.7"),
    ("14.6.1", "14.6.2"),
    ("14.9", "14.10"),   # by number, not by text
    ("15", "15.0.1"),
])
def test_an_update_for_a_newer_macos_is_no_update_for_this_mac(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path,
    running: str, minimum: str,
) -> None:
    publish_floor(server, tmp_path, minimum)
    tools.macos = running

    error = failed_download(installed)

    assert isinstance(error, app_update.NeedsNewerMacOS)
    assert error.reason == app_update.NOT_FOR_THIS_MAC
    assert error.message == (
        f"Graft 2.0.2 needs macOS {minimum} or later, and this Mac has macOS {running}, so "
        "there's no update for this Mac. It stays on Graft 2.0.1."
    )
    assert error.hint == f"Graft 2.0.2 can update this Mac once it has macOS {minimum} or later"
    assert error.url == TAG_PAGE + TAG
    assert error.minimum == minimum
    # Refused off the plist, before any signature check: nothing of the
    # download is left but the note that it needs a newer macOS.
    assert tools.tools_run() == ["ditto"]
    assert leftovers() == [app_update.MACOS_NOTE]


@pytest.mark.parametrize(("running", "minimum"), [
    ("14.6", "14.6"),
    ("14.6", "14.6.0"),
    ("15.0", "15"),
    ("14.6", "14.0"),
    ("14.10", "14.9"),
    ("26.1", "14.0"),
    ("13.0", "13.0"),
    ("14.6", None),       # no floor: macOS's own rule, as for the dummy apps everywhere else
])
def test_an_update_for_this_mac_s_macos_or_an_older_one_is_downloaded(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path,
    running: str, minimum: str | None,
) -> None:
    publish_floor(server, tmp_path, minimum)
    tools.macos = running

    staged, events = download(installed)

    assert staged.version == "2.0.2"
    assert [e["check"] for e in events if e["step"] == "verify"] == list(app_update.CHECKS)
    assert tools.tools_run() == ["ditto", "signature", "requirement", "stapled", "spctl"]
    assert app_update.MACOS_NOTE not in leftovers()


@pytest.mark.parametrize("minimum", [15, "", "fifteen", "15.0.0.1", ["15.0"]])
def test_a_floor_that_is_no_version_is_refused_as_a_broken_bundle(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path, minimum: Any,
) -> None:
    """Never guessed at: an Info.plist macOS can't read either is a broken
    release, and the app remembers it as refused (`BUNDLE`)."""
    publish_floor(server, tmp_path, minimum)

    error = failed_download(installed)

    assert error.reason == app_update.BUNDLE
    assert app_update.MACOS_KEY in error.message
    assert leftovers() == []


def test_a_mac_whose_macos_can_t_be_read_is_let_through(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path,
) -> None:
    """As before the check: an unreadable answer is no reason to stop every
    update (`running_macos`, None)."""
    publish_floor(server, tmp_path, "99.0")
    tools.macos = None

    staged, _ = download(installed)

    assert staged.version == "2.0.2"


def test_the_check_comes_with_the_bundle_s_own_and_says_so_in_progress(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path,
) -> None:
    """Part of the `bundle` step: no new `check` name for the app to read."""
    publish_floor(server, tmp_path, "15.0")
    tools.macos = "14.6"
    events: list[dict] = []

    with pytest.raises(app_update.NeedsNewerMacOS):
        app_update.download_update("2.0.1", installed, progress=events.append)

    assert [e["check"] for e in events if e["step"] == "verify"] == ["archive", "bundle"]


def test_a_graft_that_isn_t_newer_says_so_before_the_macos_it_needs(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path,
) -> None:
    publish_floor(server, tmp_path, "15.0")
    tools.macos = "14.6"
    assert failed_download(installed, current="2.0.2").reason == app_update.NOT_NEWER


# --- No loop of downloads ---------------------------------------------------------


def test_the_same_zip_is_refused_again_without_downloading_it(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path,
) -> None:
    """The app asks again at its next launch and offers Try Again, as for a
    release without this build's zip: each answers from the note, at the
    cost of the release check alone."""
    publish_floor(server, tmp_path, "15.0")
    tools.macos = "14.6"
    first = failed_download(installed)
    assert downloads(server) == 1
    tools.calls.clear()

    for _ in range(3):
        again = failed_download(installed)
        assert (again.reason, again.message, again.hint, again.url) == (
            first.reason, first.message, first.hint, first.url)

    assert downloads(server) == 1, "never downloaded again"
    assert server.paths().count("/latest") == 4
    assert tools.calls == []
    assert leftovers() == [app_update.MACOS_NOTE]


def test_the_note_is_written_owner_only_and_never_through_a_link(tmp_path: Path) -> None:
    """Written as Graft's other private files are (`data_dirs.private_write`):
    a link planted where the note's temporary used to go, `<note>.part`, was
    opened with O_TRUNC and written through (the 2.3.1 review's A4)."""
    import os
    import stat

    root = tmp_path / "updates"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("someone else's file")
    (root / (app_update.MACOS_NOTE + ".part")).symlink_to(elsewhere)

    app_update._note_minimum(root, version="2.0.2", sha256="ab" * 32, minimum="15.0")

    assert elsewhere.read_text() == "someone else's file"
    note = root / app_update.MACOS_NOTE
    assert not note.is_symlink()
    assert stat.S_IMODE(os.lstat(note).st_mode) == 0o600
    assert app_update._noted_minimum(root, version="2.0.2", sha256="ab" * 32) == "15.0"


def test_a_note_that_cant_be_written_is_no_failure(tmp_path: Path) -> None:
    """Best effort: without it the next try downloads the zip again."""
    app_update._note_minimum(tmp_path / "no-such-folder", version="2.0.2", sha256="ab" * 32,
                             minimum="15.0")
    assert not (tmp_path / "no-such-folder").exists()


def test_once_this_mac_has_that_macos_the_same_zip_downloads(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path,
) -> None:
    publish_floor(server, tmp_path, "15.0")
    tools.macos = "14.6"
    failed_download(installed)

    tools.macos = "15.0.1"
    staged, _ = download(installed)

    assert staged.version == "2.0.2"
    assert downloads(server) == 2
    assert app_update.MACOS_NOTE not in leftovers(), "the note goes with the download it was about"


def test_another_zip_for_the_release_is_downloaded_and_checked_afresh(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path,
) -> None:
    """The note is about one zip, by the SHA-256 GitHub lists for it: a
    release whose zip is replaced is downloaded again."""
    publish_floor(server, tmp_path, "15.0")
    tools.macos = "14.6"
    failed_download(installed)

    publish_floor(server, tmp_path, "14.0")
    staged, _ = download(installed)

    assert (staged.path / "Contents" / "Resources" / "marker.txt").read_text() == "14.0"
    assert downloads(server) == 2


def test_a_newer_release_is_downloaded_and_checked_afresh(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path,
) -> None:
    publish_floor(server, tmp_path, "15.0")
    tools.macos = "14.6"
    failed_download(installed)

    src = tmp_path / "next"
    src.mkdir()
    data = zip_app(dummy_app(src, "2.0.3", minimum_macos="14.0"), src / "Graft.zip")
    server.serve_json("/latest", release_json(server.base, "v2.0.3", data))
    server.routes["/download/v2.0.3/Graft.zip"] = Route(body=data)

    staged, _ = download(installed)

    assert staged.version == "2.0.3"


@pytest.mark.parametrize("note", [
    b"not json",
    b"[]",
    b'{"version": "2.0.2", "sha256": 7, "minimum": "15.0"}',
    b'{"version": "2.0.2", "minimum": "15.0"}',
])
def test_a_note_that_isn_t_one_is_no_reason_to_refuse(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path, note: bytes,
) -> None:
    publish_floor(server, tmp_path, None)
    root = app_update.updates_dir()
    root.mkdir(parents=True)
    (root / app_update.MACOS_NOTE).write_bytes(note)

    staged, _ = download(installed)

    assert staged.version == "2.0.2"
    assert app_update.MACOS_NOTE not in leftovers()


# --- The verb ---------------------------------------------------------------------


def test_the_event_says_there_s_no_update_for_this_mac(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path,
) -> None:
    publish_floor(server, tmp_path, "15.0")
    tools.macos = "14.6"

    result = _run(["--json", "--download-update", "--current-version", "2.0.1",
                   "--app-path", str(installed)])

    assert result.exit_code == 1
    end = _events(result)[-1]
    assert end == {
        "type": "update-download-end", "ok": False, "current": "2.0.1", "version": None,
        "app_path": None, "url": TAG_PAGE + TAG, "reason": "not-for-this-mac",
        "message": "Graft 2.0.2 needs macOS 15.0 or later, and this Mac has macOS 14.6, so "
                   "there's no update for this Mac. It stays on Graft 2.0.1.",
    }
    said = " ".join(result.stderr.split())
    assert " ".join(end["message"].split()) in said
    assert "hint: Graft 2.0.2 can update this Mac once it has macOS 15.0 or later" in said


def test_a_floor_too_long_to_be_one_is_shown_cut_short(tmp_path: Path) -> None:
    """The message reaches the JSON and the app's banner: a floor of 900,000
    characters is shown as its first few, not whole."""
    app = dummy_app(tmp_path, "2.0.2", minimum_macos="9" * 900_000)

    with pytest.raises(app_update.UpdateError) as caught:
        app_update.check_macos(app, macos="14.6", version="2.0.2", current="2.0.1")

    assert caught.value.reason == app_update.BUNDLE
    assert f"({app_update.MACOS_KEY} is '{'9' * 38}…)" in caught.value.message
    assert len(caught.value.message) < 200


def test_a_short_floor_that_is_no_version_is_shown_whole(tmp_path: Path) -> None:
    app = dummy_app(tmp_path, "2.0.2", minimum_macos="fifteen")

    with pytest.raises(app_update.UpdateError) as caught:
        app_update.check_macos(app, macos="14.6", version="2.0.2", current="2.0.1")

    assert f"({app_update.MACOS_KEY} is 'fifteen')" in caught.value.message


# --- The check ------------------------------------------------------------------


def _check_update(current: str = "2.0.1", *flags: str) -> tuple[int, dict]:
    result = _run([*flags, "--check-update", "--current-version", current])
    events = _events(result) if "--json" in flags else []
    return result.exit_code, (events[-1] if events else {"stdout": result.stdout})


def test_the_check_knows_a_release_this_mac_was_refused_for_its_macos(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path,
) -> None:
    """No update for this Mac, so the banner stops offering one: the
    download found the zip needs a newer macOS (`MACOS_NOTE`), and this Mac
    still has an older one."""
    publish_floor(server, tmp_path, "15.0")
    tools.macos = "14.6"
    failed_download(installed)

    code, event = _check_update("2.0.1", "--json")

    assert code == 0
    assert event == {
        "type": "update-check", "ok": True, "current": "2.0.1", "latest": "2.0.2",
        "url": TAG_PAGE + TAG, "update_available": False, "needs_macos": "15.0",
        "reason": None, "status": None, "message": None,
    }
    assert downloads(server) == 1, "the check downloads nothing"


def test_the_check_says_so_in_terminal(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path,
) -> None:
    publish_floor(server, tmp_path, "15.0")
    tools.macos = "14.6"
    failed_download(installed)

    code, said = _check_update("2.0.1")

    assert code == 0
    assert " ".join(said["stdout"].split()) == (
        "Graft 2.0.2 needs macOS 15.0 or later, and this Mac has macOS 14.6, so there's no "
        "update for this Mac. It stays on Graft 2.0.1."
    )


def test_the_check_offers_the_release_once_this_mac_has_that_macos(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path,
) -> None:
    publish_floor(server, tmp_path, "15.0")
    tools.macos = "14.6"
    failed_download(installed)

    tools.macos = "15.0"
    _, event = _check_update("2.0.1", "--json")

    assert (event["update_available"], event["needs_macos"]) == (True, None)


def test_the_check_offers_another_zip_for_the_release(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path,
) -> None:
    """The note is about one zip: a replaced one may not need that macOS."""
    publish_floor(server, tmp_path, "15.0")
    tools.macos = "14.6"
    failed_download(installed)
    publish_floor(server, tmp_path, "14.0")

    _, event = _check_update("2.0.1", "--json")

    assert (event["update_available"], event["needs_macos"]) == (True, None)


def test_without_a_note_the_check_is_as_it_was(
    server: FixtureServer, tools: FakeTools, tmp_path: Path,
) -> None:
    publish_floor(server, tmp_path, "15.0")
    tools.macos = "14.6"

    _, event = _check_update("2.0.1", "--json")

    assert (event["update_available"], event["needs_macos"]) == (True, None)


def test_a_release_that_isn_t_newer_needs_no_macos(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path,
) -> None:
    publish_floor(server, tmp_path, "15.0")
    tools.macos = "14.6"
    failed_download(installed)

    _, event = _check_update("2.0.2", "--json")

    assert (event["update_available"], event["needs_macos"]) == (False, None)


@pytest.mark.parametrize("why", ["outside an app", "macos unknown"])
def test_a_check_that_can_t_tell_offers_the_release(
    server: FixtureServer, tools: FakeTools, tmp_path: Path, installed: Path,
    monkeypatch: pytest.MonkeyPatch, why: str,
) -> None:
    """A graft outside Graft.app has no build, so no zip to have a note
    about; a Mac whose macOS can't be read is let through, as the download
    lets it through."""
    publish_floor(server, tmp_path, "15.0")
    tools.macos = "14.6"
    failed_download(installed)
    if why == "outside an app":
        def no_bundle(app: Path | None = None) -> str:
            raise app_update.UpdateError("not in an app", reason=app_update.NO_TEAM)

        monkeypatch.setattr(app_update, "own_flavor", no_bundle)
    else:
        tools.macos = None

    _, event = _check_update("2.0.1", "--json")

    assert (event["update_available"], event["needs_macos"]) == (True, None)
