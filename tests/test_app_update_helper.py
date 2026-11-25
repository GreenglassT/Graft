"""The install helper (`app_update.HELPER`), run for real by /bin/sh on dummy
Graft.app bundles in a temp dir.

Only the tools that would reach past the temp dir are stand-ins: codesign
and spctl (the dummies aren't signed), lsregister (it would register the
dummies with Launch Services) and open (it would launch one). `mv` and
PlistBuddy are the system's, except where a test needs a move to fail.
Nothing here touches /Applications or the real Trash.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from graft_ipa import app_update

from .app_update_fakes import TEAM, dummy_app
from .cli_ast import REPO_ROOT, strip_swift_comments


class Rig:
    """Graft 2.0.1 installed, 2.0.2 downloaded, an empty Trash, and the
    stand-in tools, all under one temp dir."""

    #: The helper's time zone (`env`), whatever this Mac's is or the test
    #: run's TZ says, so a test reading its times reads them in the same
    #: one. Graft's helper inherits the CLI's environment (`spawn_detached`),
    #: but `env` is all this one gets. UTC+14: a helper that didn't use it
    #: would be hours off here.
    TZ = "Pacific/Kiritimati"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.apps = root / "Applications"
        self.apps.mkdir()
        self.app = dummy_app(self.apps, "2.0.1", marker="old")
        work = root / "cache" / "graft" / "updates" / "Graft-x.noindex" / "app"
        work.mkdir(parents=True)
        self.staged = dummy_app(work, "2.0.2", marker="new")
        self.trash = root / "Trash"
        self.log = root / "Logs" / "update.log"
        self.log.parent.mkdir()
        self.calls = root / "calls.log"
        self.bin = root / "bin"
        self.bin.mkdir()
        #: What the stand-ins answer: each reads its file at run time.
        self.answers = root / "answers"
        self.answers.mkdir()
        for name in ("signature", "stapled", "gatekeeper"):
            (self.answers / name).write_text("yes")
        self._tool("codesign", r'''
if [ "$1" = "-dvv" ]; then
  echo "Identifier=com.greenglasst.graft" >&2
  [ "$(cat "$ANSWERS/stapled")" = yes ] && echo "Notarization Ticket=stapled" >&2
  exit 0
fi
[ "$(cat "$ANSWERS/signature")" = yes ] || { echo "$5: code failed to satisfy specified code requirement(s)" >&2; exit 3; }
''')
        self._tool("spctl", r'''
[ "$(cat "$ANSWERS/gatekeeper")" = yes ] || { echo "$5: rejected" >&2; exit 3; }
echo "$5: accepted" >&2
echo "source=Notarized Developer ID" >&2
''')
        self._tool("lsregister", "")
        self._tool("fake-open", "")
        self.tools = app_update.HelperTools(
            codesign=str(self.bin / "codesign"), spctl=str(self.bin / "spctl"),
            plistbuddy=app_update.PLISTBUDDY, lsregister=str(self.bin / "lsregister"),
            open=str(self.bin / "fake-open"), mv=app_update.MV,
        )

    def _tool(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(f'#!/bin/sh\necho "{name} $*" >> "{self.calls}"\n{body}\nexit 0\n')
        path.chmod(0o755)

    def env(self, *, pid: int, relaunch: bool = True, version: str = "2.0.2",
            wait_ticks: int = 50) -> dict[str, str]:
        env = app_update.helper_env(
            pid=pid, app=self.app, staged=self.staged, version=version, relaunch=relaunch,
            log=self.log, trash=self.trash, team=TEAM, tools=self.tools, wait_ticks=wait_ticks,
        )
        env["ANSWERS"] = str(self.answers)
        env["TZ"] = self.TZ
        return env

    def run(self, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(app_update.HELPER_ARGV, env=env, capture_output=True, text=True,
                              timeout=60, check=False)

    def tool_calls(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def marker(self, app: Path) -> str:
        return (app / "Contents" / "Resources" / "marker.txt").read_text()

    def leftovers_beside_the_app(self) -> list[str]:
        return sorted(p.name for p in self.apps.iterdir() if p.name != "Graft.app")


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


@pytest.fixture
def gone_pid() -> int:
    """The pid of a process that has quit (and been reaped): Graft, gone."""
    done = subprocess.Popen(["/usr/bin/true"])
    done.wait()
    return done.pid


@pytest.fixture
def running() -> Iterator[subprocess.Popen]:
    """A process that keeps running until the test ends it: Graft, still
    open. Reaped as soon as it ends, so `kill -0` sees it gone."""
    proc = subprocess.Popen(["/bin/sleep", "30"])
    reaper = threading.Thread(target=proc.wait, daemon=True)
    reaper.start()
    yield proc
    proc.kill()
    reaper.join(5)


def test_it_swaps_the_app_trashes_the_old_one_registers_and_reopens(rig: Rig, gone_pid: int) -> None:
    done = rig.run(rig.env(pid=gone_pid))

    assert done.returncode == 0, rig.log.read_text()
    assert rig.marker(rig.app) == "new"
    trashed = rig.trash / "Graft.app"
    assert rig.marker(trashed) == "old"
    assert not rig.staged.exists()
    # The empty download folders go too.
    assert not rig.staged.parent.parent.exists()
    assert rig.leftovers_beside_the_app() == []
    calls = rig.tool_calls()
    copy_checks = [c for c in calls if c.startswith(("codesign", "spctl"))]
    assert len(copy_checks) == 3
    assert copy_checks[0].startswith("codesign --verify --deep --strict -R =" + app_update.requirement(TEAM))
    # The checks ran on the copy beside the app, the bytes that were installed.
    assert all("/Applications/.Graft-update-" in c for c in copy_checks)
    assert calls[-3:] == [
        f"lsregister -u {trashed}",
        f"lsregister -f {rig.app}",
        f"fake-open {rig.app}",
    ]
    assert "installed Graft 2.0.2" in rig.log.read_text()


def test_the_installed_line_is_the_one_the_app_reads(rig: Rig, gone_pid: int) -> None:
    """The app reads this line at its next launch (`UpdateStore.installLogged`
    in the Swift app): a launch still on the version it replaced, whose
    attempt was written down before this line, is a copy put back by hand
    since, not a failed install. So its shape is a contract: the time, to
    the second, with its offset from UTC, then "installed Graft <version>
    at <app>;" (UpdateStoreTests writes it the same way). The time is in
    the zone of the helper's environment: in Graft, the CLI's, which has
    any TZ the login shell sets, and the app may not. With the offset, the
    app reads it exactly whatever its own zone. Every line the helper
    writes has its time so."""
    zone = ZoneInfo(Rig.TZ)
    before = datetime.now(zone).replace(microsecond=0)
    assert rig.run(rig.env(pid=gone_pid)).returncode == 0
    after = datetime.now(zone)

    written = rig.log.read_text().splitlines()
    assert written and all(re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4} \S", line)
                           for line in written), written
    lines = [line for line in written if " installed Graft " in line]
    assert len(lines) == 1, written
    said = re.fullmatch(
        r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4}) installed Graft 2\.0\.2 at (.+?); .*", lines[0],
    )
    assert said is not None, lines[0]
    assert said.group(2) == str(rig.app)
    when = datetime.strptime(said.group(1), "%Y-%m-%d %H:%M:%S %z")
    assert when.utcoffset() == after.utcoffset(), "the offset is the helper's zone's"
    assert before <= when <= after


#: The Swift tests, and the folder their copy of the helper's line names
#: where the rig's is (`UpdateStoreTests.helperInstalledLine`).
SWIFT_TESTS = REPO_ROOT / "Graft" / "GraftTests" / "UpdateStoreTests.swift"
FIXTURE_ROOT = "/private/var/empty/Example"


def test_the_swift_tests_read_the_line_the_helper_writes(rig: Rig, gone_pid: int) -> None:
    """The round trip. `UpdateStoreTests.helperInstalledLine` is the line
    the Swift tests hand the app's reader (`UpdateStore.installLogged`), so
    it must be this helper's line for an install that worked, as the helper
    writes it, but for its time (the same shape and the same offset, the
    rig's zone's, but another second) and the rig's folder, which it names
    FIXTURE_ROOT. A plain literal, read with comments stripped."""
    source = strip_swift_comments(SWIFT_TESTS.read_text())
    found = re.search(r'static let helperInstalledLine = "([^"\\]*)"\n', source)
    assert found, "helperInstalledLine is gone from UpdateStoreTests.swift, or isn't a plain literal"
    fixture = found.group(1)

    assert rig.run(rig.env(pid=gone_pid)).returncode == 0
    [written] = [line for line in rig.log.read_text().splitlines() if " installed Graft " in line]

    stamp = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4} ")
    assert stamp.match(written), written
    assert stamp.match(fixture), fixture
    datetime.strptime(fixture[:25], "%Y-%m-%d %H:%M:%S %z")
    assert fixture[19:25] == written[19:25], "the fixture's offset is the helper's, in Rig.TZ"
    assert fixture[26:] == written[26:].replace(str(rig.root), FIXTURE_ROOT)


def test_without_relaunch_nothing_is_opened(rig: Rig, gone_pid: int) -> None:
    assert rig.run(rig.env(pid=gone_pid, relaunch=False)).returncode == 0
    assert rig.marker(rig.app) == "new"
    assert not [c for c in rig.tool_calls() if c.startswith("fake-open")]


def test_an_old_graft_already_in_the_trash_is_kept(rig: Rig, gone_pid: int) -> None:
    rig.trash.mkdir()
    earlier = dummy_app(rig.trash, "1.9", marker="older")
    (rig.trash / "Graft 2.app").mkdir()

    assert rig.run(rig.env(pid=gone_pid)).returncode == 0

    assert rig.marker(earlier) == "older"
    assert rig.marker(rig.trash / "Graft 3.app") == "old"


def test_it_waits_for_graft_to_quit(rig: Rig, running: subprocess.Popen) -> None:
    helper = subprocess.Popen(app_update.HELPER_ARGV, env=rig.env(pid=running.pid))
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            helper.wait(timeout=1)
        assert rig.marker(rig.app) == "old", "nothing moves while Graft runs"
        assert rig.staged.exists()
        running.kill()
        assert helper.wait(timeout=30) == 0
    finally:
        helper.kill()
    assert rig.marker(rig.app) == "new"


def test_a_graft_that_does_not_quit_keeps_its_version(rig: Rig, running: subprocess.Popen) -> None:
    done = rig.run(rig.env(pid=running.pid, wait_ticks=3))

    assert done.returncode == 1
    assert rig.marker(rig.app) == "old"
    assert rig.marker(rig.staged) == "new"
    assert rig.tool_calls() == [], "not reopened: it is still open"
    assert "didn't quit" in rig.log.read_text()


@pytest.mark.parametrize(("answer", "said"), [
    ("signature", "isn't signed as Graft"),
    ("stapled", "no notarization ticket"),
    ("gatekeeper", "Gatekeeper didn't accept it"),
])
def test_a_copy_that_fails_a_check_is_deleted_and_the_old_app_reopened(
    rig: Rig, gone_pid: int, answer: str, said: str
) -> None:
    (rig.answers / answer).write_text("no")

    done = rig.run(rig.env(pid=gone_pid))

    assert done.returncode == 1
    assert rig.marker(rig.app) == "old"
    assert not (rig.trash / "Graft.app").exists()
    assert rig.leftovers_beside_the_app() == [], "the copy beside the app is deleted"
    assert rig.tool_calls()[-1] == f"fake-open {rig.app}"
    assert said in rig.log.read_text()


def test_a_copy_of_another_version_is_refused(rig: Rig, gone_pid: int) -> None:
    """The CLI checked 2.0.2; a different Graft put in its place since,
    even a genuine one, isn't what was asked for."""
    done = rig.run(rig.env(pid=gone_pid, version="2.0.3"))

    assert done.returncode == 1
    assert rig.marker(rig.app) == "old"
    assert rig.leftovers_beside_the_app() == []
    assert "not 2.0.3" in rig.log.read_text()


def test_when_the_new_app_will_not_move_in_the_old_one_is_put_back(rig: Rig, gone_pid: int) -> None:
    """The last rename fails (here, a stand-in mv that refuses it): the old
    app comes back out of the Trash, and opens."""
    mv = rig.bin / "mv"
    mv.write_text(f'''#!/bin/sh
case "$1" in */.Graft-update-*) [ "$2" = "{rig.app}" ] && {{ echo "mv: refused" >&2; exit 1; }} ;; esac
exec /bin/mv "$@"
''')
    mv.chmod(0o755)
    env = rig.env(pid=gone_pid)
    env["GRAFT_UPDATE_MV"] = str(mv)

    done = rig.run(env)

    assert done.returncode == 1
    assert rig.marker(rig.app) == "old"
    assert not (rig.trash / "Graft.app").exists()
    assert rig.leftovers_beside_the_app() == []
    log = rig.log.read_text()
    assert "put the old Graft back" in log
    assert "couldn't move Graft 2.0.2" in log
    assert rig.tool_calls()[-1] == f"fake-open {rig.app}"


def test_an_old_app_that_will_not_go_to_the_trash_stays(rig: Rig, gone_pid: int) -> None:
    rig.trash.write_text("a file where the Trash folder should be")

    done = rig.run(rig.env(pid=gone_pid))

    assert done.returncode == 1
    assert rig.marker(rig.app) == "old"
    assert rig.leftovers_beside_the_app() == []


def test_a_missing_variable_stops_it_before_it_touches_anything(rig: Rig, gone_pid: int) -> None:
    for name in [k for k in rig.env(pid=gone_pid) if k.startswith("GRAFT_UPDATE_")]:
        env = rig.env(pid=gone_pid)
        del env[name]
        done = rig.run(env)
        assert done.returncode != 0, name
        assert rig.marker(rig.app) == "old", name
        assert rig.marker(rig.staged) == "new", name
        assert rig.tool_calls() == [], name


@pytest.fixture
def odd_rig(tmp_path: Path) -> Rig:
    root = tmp_path / "Someone's \"Mac\" $HOME"
    root.mkdir()
    return Rig(root)


def test_a_path_with_spaces_quotes_and_dollars_is_used_as_it_is(odd_rig: Rig, gone_pid: int) -> None:
    done = odd_rig.run(odd_rig.env(pid=gone_pid))
    assert done.returncode == 0, odd_rig.log.read_text()
    assert odd_rig.marker(odd_rig.app) == "new"
    assert odd_rig.marker(odd_rig.trash / "Graft.app") == "old"


def test_the_helper_names_no_path_of_its_own() -> None:
    """Every path comes from its environment, so a test can point it at a
    temp dir and nothing in it can reach the real Applications folder."""
    assert "/Applications" not in app_update.HELPER
    assert ".Trash" not in app_update.HELPER
    assert "HOME" not in app_update.HELPER
    assert os.path.isabs(app_update.HELPER_ARGV[0])
