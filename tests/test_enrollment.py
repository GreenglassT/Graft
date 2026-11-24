"""`enrollment`: the apps UnfairPlay v0.4.0's `setup --app` accepts, and the
command that enrolls this Mac with one. Only temp folders are scanned."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa import enrollment
from graft_ipa.enrollment import NotACandidate
from tests.appstore_fakes import make_ios_app


@pytest.fixture
def apps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp /Applications, trusted as the real one is, but owned (here) by
    this user rather than root."""
    root = (tmp_path / "Applications").resolve()
    root.mkdir()
    monkeypatch.setattr(enrollment, "TRUSTED_ROOT", root)
    monkeypatch.setattr(enrollment, "TRUSTED_OWNER", os.getuid())
    return root


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def test_an_iphone_app_from_the_mac_app_store_qualifies(apps: Path, home: Path) -> None:
    make_ios_app(apps)
    (found,) = enrollment.candidates([apps], home=home)
    assert (found.path, found.name, found.bundle_id, found.opened) == (
        apps / "Example.app", "Example", "com.example.app", False
    )


def test_opened_means_it_has_a_container(apps: Path, home: Path) -> None:
    """S2.5: `~/Library/Containers/<bundle id>` exists after one launch."""
    make_ios_app(apps)
    (home / "Library" / "Containers" / "com.example.app").mkdir(parents=True)
    (found,) = enrollment.candidates([apps], home=home)
    assert found.opened is True


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"wrapper": False}, "not an iPhone or iPad app"),
        ({"sinfs": 0}, "exactly one"),
        ({"sinfs": 2}, "exactly one"),
        ({"encrypted": False}, "isn't encrypted"),
        ({"plist": False}, "Info.plist"),
    ],
)
def test_what_unfairplay_refuses_is_not_offered(apps: Path, home: Path, kwargs, why: str) -> None:
    app = make_ios_app(apps, **kwargs)
    assert enrollment.candidates([apps], home=home) == []
    with pytest.raises(NotACandidate, match=why):
        enrollment.check(app, home=home)


@pytest.mark.parametrize("executable", ["", ".", "..", "../Example", "a/b"])
def test_an_executable_that_isn_t_a_name_inside_the_app_is_not_offered(
    apps: Path, home: Path, executable: str
) -> None:
    """UnfairPlay copies `<inner>/<CFBundleExecutable>`, so it must name a
    file in the app; root checks its own copy for the same (`root_script`)."""
    import plistlib

    app = make_ios_app(apps)
    plist = app / "Wrapper" / "Example.app" / "Info.plist"
    plist.write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.example.app", "CFBundleExecutable": executable}))
    assert enrollment.candidates([apps], home=home) == []
    with pytest.raises(NotACandidate, match="names no bundle id or executable"):
        enrollment.check(app, home=home)


def test_the_candidate_names_the_app_inside_the_wrapper(apps: Path, home: Path) -> None:
    app = make_ios_app(apps, "Some Game")
    assert enrollment.check(app, home=home).inner == app / "Wrapper" / "Some Game.app"


def test_a_symlinked_app_is_not_offered(apps: Path, home: Path, tmp_path: Path) -> None:
    real = make_ios_app(tmp_path / "elsewhere")
    (apps / "Example.app").symlink_to(real)
    assert enrollment.candidates([apps], home=home) == []


def test_opened_apps_come_first_then_by_name(apps: Path, home: Path) -> None:
    make_ios_app(apps, "Bravo", bundle_id="com.example.bravo")
    make_ios_app(apps, "Alpha", bundle_id="com.example.alpha")
    make_ios_app(apps, "Charlie", bundle_id="com.example.charlie")
    (home / "Library" / "Containers" / "com.example.charlie").mkdir(parents=True)
    assert [c.name for c in enrollment.candidates([apps], home=home)] == ["Charlie", "Alpha", "Bravo"]


def test_a_folder_that_isnt_there_is_skipped(tmp_path: Path, home: Path) -> None:
    assert enrollment.candidates([tmp_path / "nope"], home=home) == []


def _cli(args: list[str]):
    return CliRunner().invoke(cli_mod.app, args)


def test_the_cli_lists_each_candidate_then_the_count(
    temp_config_env: Path, apps: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_ios_app(apps)
    monkeypatch.setattr(enrollment, "SCAN_DIRS", (apps,))
    result = _cli(["--json", "--appstore-enroll-candidates"])
    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    assert [e["type"] for e in events] == ["appstore-enroll-candidate", "appstore-enroll-candidates-end"]
    assert events[0] == {
        "type": "appstore-enroll-candidate", "path": str(apps / "Example.app"),
        "name": "Example", "bundle_id": "com.example.app", "opened": False,
    }
    assert events[1] == {"type": "appstore-enroll-candidates-end", "count": 1}


def test_with_none_the_count_is_zero_not_silence(
    temp_config_env: Path, apps: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(enrollment, "SCAN_DIRS", (apps,))
    result = _cli(["--json", "--appstore-enroll-candidates"])
    assert result.exit_code == 0, result.output
    assert [json.loads(line) for line in result.stdout.splitlines()] == [
        {"type": "appstore-enroll-candidates-end", "count": 0}
    ]


def test_with_none_a_person_is_told_what_to_install(
    temp_config_env: Path, apps: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(enrollment, "SCAN_DIRS", (apps,))
    result = _cli(["--appstore-enroll-candidates"])
    assert result.exit_code == 0, result.output
    assert "Mac App Store" in result.stdout


def test_an_app_root_wouldnt_take_is_not_offered(apps: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Owned by the user rather than the system: the enrollment would refuse it,
    so the sheet never offers it."""
    make_ios_app(apps)
    monkeypatch.setattr(enrollment, "TRUSTED_OWNER", 0)
    assert enrollment.candidates([apps], home=home) == []


def test_only_applications_is_scanned() -> None:
    from pathlib import Path as P

    assert P("/Applications") in enrollment.SCAN_DIRS
    assert all(d == P("/Applications") for d in enrollment.SCAN_DIRS)
