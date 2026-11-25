"""The self-update's checks with the real tools, on a real notarized Graft.

Read-only, on the Graft.app installed in /Applications: codesign's
`--verify` and `-dvv` and spctl's `-a` only read the bundle, and nothing here
writes, moves or replaces it. Skipped where there is no such Graft, or it
isn't the Developer ID build (a Mac that installed it from source).

This is what the stand-ins in `test_app_update.py` stand in for: that the
team is read off a genuine release's own signature (`own_team`), as a
running Graft reads its own; that the release then passes every check for
that team; and that the requirement's two pins each fail a genuine
signature when changed, so neither is decoration. No team is written here:
the one checked is the one read.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from graft_ipa import app_update

INSTALLED = Path("/Applications/Graft.app")


def _installed_team() -> str | None:
    """The Developer ID team of the Graft.app in /Applications, read as a
    running Graft reads its own, or None when there's no such Graft."""
    if not INSTALLED.is_dir() or app_update.bundle_info(INSTALLED).bundle_id != app_update.BUNDLE_ID:
        return None
    try:
        return app_update.own_team(app_update.run_tool, app=INSTALLED)
    except app_update.UpdateError:
        return None


TEAM = _installed_team()

pytestmark = pytest.mark.skipif(TEAM is None, reason="no Developer ID Graft.app in /Applications")


def test_its_team_is_read_from_its_own_signature_and_is_codesigns() -> None:
    """What `own_team` read is the TeamIdentifier codesign names, a
    Developer ID team, and the release's requirement for it holds."""
    assert TEAM is not None and app_update.TEAM_RE.fullmatch(TEAM)
    said = app_update.run_tool([app_update.CODESIGN, "-dv", "--verbose=2", str(INSTALLED)]).output
    assert f"TeamIdentifier={TEAM}" in said.splitlines()
    app_update.check_requirement(INSTALLED, app_update.run_tool, team=TEAM)


def test_its_cli_runs_from_where_own_bundle_looks() -> None:
    """The interpreter is where `own_bundle` expects it, so the bundled
    CLI finds its own Graft.app."""
    python = INSTALLED.joinpath(*app_update.EMBEDDED_PYTHON, "python3.13")
    assert python.is_file()
    assert app_update.own_bundle(str(python)) == INSTALLED


def test_the_installed_release_passes_every_check() -> None:
    checks: list[str] = []
    assert TEAM is not None
    version = app_update.verify_app(
        INSTALLED, newer_than="1.0", team=TEAM, flavor=app_update.bundle_flavor(INSTALLED),
        run=app_update.run_tool, progress=lambda e: checks.append(e["check"]),
    )
    assert version == app_update.bundle_info(INSTALLED).version
    assert checks == ["bundle", "architecture", "signature", "requirement", "notarization"]


def test_it_is_not_newer_than_itself() -> None:
    version = app_update.bundle_info(INSTALLED).version
    assert version is not None
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.check_bundle(INSTALLED, newer_than=version)
    assert caught.value.reason == app_update.NOT_NEWER


def test_another_identifier_fails_a_genuine_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    """Another app of the same team, as codesign sees it: the same
    certificate, a different identifier."""
    assert TEAM is not None
    monkeypatch.setattr(app_update, "BUNDLE_ID", "com.example.other")
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.check_requirement(INSTALLED, app_update.run_tool, team=TEAM)
    assert caught.value.reason == app_update.REQUIREMENT_FAILED


def test_another_team_fails_a_genuine_signature() -> None:
    """Graft from anyone but the team that signed the running one."""
    assert TEAM is not None
    other = "ABCDE12345" if TEAM != "ABCDE12345" else "FGHIJ67890"
    with pytest.raises(app_update.UpdateError) as caught:
        app_update.check_requirement(INSTALLED, app_update.run_tool, team=other)
    assert caught.value.reason == app_update.REQUIREMENT_FAILED


def _helper_checks() -> list[str]:
    """The helper's three checks of its copy, as written in `HELPER`, each
    without the `|| give_up` that follows it."""
    text = app_update.HELPER.replace("\\\n", " ")
    lines = [line.split(" || give_up")[0].strip() for line in text.splitlines()
             if line.startswith(('"$GRAFT_UPDATE_CODESIGN"', '"$GRAFT_UPDATE_SPCTL"'))]
    assert len(lines) == 3, lines
    return lines


def test_the_helpers_own_checks_pass_on_it() -> None:
    """The helper's own lines, run by /bin/sh on this bundle in place of its
    copy: the requirement, the stapled ticket, and Gatekeeper's verdict, each
    matched as a whole line (`grep -qx`)."""
    env = {
        "PATH": app_update.SYSTEM_PATH, "copy": str(INSTALLED), "log": "/dev/null",
        "GRAFT_UPDATE_CODESIGN": app_update.CODESIGN, "GRAFT_UPDATE_SPCTL": app_update.SPCTL,
        "GRAFT_UPDATE_REQUIREMENT": app_update.requirement(TEAM or ""),
    }
    for line in _helper_checks():
        done = subprocess.run(["/bin/sh", "-c", line], env=env, capture_output=True, text=True,
                              timeout=120)
        assert done.returncode == 0, (line, done.stdout, done.stderr)


def test_the_helpers_requirement_check_fails_another_identifier() -> None:
    env = {
        "PATH": app_update.SYSTEM_PATH, "copy": str(INSTALLED), "log": "/dev/null",
        "GRAFT_UPDATE_CODESIGN": app_update.CODESIGN,
        "GRAFT_UPDATE_REQUIREMENT": app_update.requirement(TEAM or "").replace(
            'identifier "com.greenglasst.graft"', 'identifier "com.example.other"'),
    }
    requirement = _helper_checks()[0]
    assert "-R" in requirement
    done = subprocess.run(["/bin/sh", "-c", requirement], env=env, capture_output=True, timeout=120)
    # codesign's own status for "signed, but not as the requirement says".
    assert done.returncode == 3


def _developer_tools() -> bool:
    try:
        return subprocess.run(["/usr/bin/xcode-select", "-p"], capture_output=True,
                              timeout=10).returncode == 0
    except OSError:
        return False


@pytest.mark.skipif(not _developer_tools(), reason="stapler needs the developer tools")
def test_stapler_agrees_that_its_ticket_is_stapled() -> None:
    """Graft doesn't run stapler (a Mac without the developer tools has only
    the stub that offers to install them); on this one, it agrees with the
    checks Graft does run.

    stapler asks Apple's servers: it failed here once while this Mac had no
    DNS, when codesign's and spctl's checks, which Graft runs, still passed.
    So an answer it couldn't get skips; only "no ticket" fails."""
    done = subprocess.run(["/usr/bin/xcrun", "stapler", "validate", str(INSTALLED)],
                          capture_output=True, text=True, timeout=120)
    said = done.stdout + done.stderr
    assert "does not have a ticket stapled" not in said
    if done.returncode != 0:
        pytest.skip(f"stapler couldn't validate (offline?): {said.strip()[:300]}")
    assert "The validate action worked!" in done.stdout
