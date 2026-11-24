"""Release gate B.3, the CLI half: what root runs to enroll this Mac is exactly
the script expected, built only here, and root never writes into, or runs
from, anything this user can write to.

Task 0 settled the shape: root under `do shell script … with administrator
privileges` has no `SUDO_USER` or `SUDO_UID` (S2.3), so UnfairPlay leaves
what it makes root's (S2.1). The first design chowned a user-made model
folder as root afterward (S2.2); a security review showed that a hard link
or symlink planted in a folder the user can write turns that `chown -R` into
re-owning any root file, and that root running UnfairPlay from a user-owned
Graft.app can be handed a swapped binary. So now:

- root makes its own folder (`mktemp -d` under /private/var/tmp), copies
  UnfairPlay into it and checks the *copy* against Graft.app's team (when it
  has one), checks the app (in /Applications, root's, writable by no one
  else), runs setup into that folder, and only then gives the folder, whose
  every file its own UnfairPlay wrote, to the user;
- the user's own model folder is filled afterward by `finish`, as the user,
  from files opened without following links, each a regular file with one
  link that the user owns.

Nothing here runs anything elevated. The script is run as this user by
/bin/sh, with the trusted folder and owner pointed at temp ones, to show it
does what its text says. The app half (the admin prompt, the AppleScript) is
GraftTests/AdminShellTests.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa import enrollment
from graft_ipa.errors import AppStoreError
from tests.appstore_fakes import FakeTools, make_ios_app

TEAM = "TESTTEAM01"

#: Where this Mac keeps its FairPlay state, written out here rather than
#: read from the code under test: no script built in this file may name
#: them (`real_fairplay_state`).
REAL_STATE_FOLDERS = ("/private/var/db/fpsd", "/Users/Shared")
#: What root says when neither folder holds that state (exit 69).
NO_STATE = ("graft: this Mac has no SC Info.sidb, the App Store state UnfairPlay reads: "
            "launch the app once, then set up again")


@pytest.fixture(autouse=True)
def real_fairplay_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, ...]:
    """Every script built here looks for this Mac's FairPlay state in temp
    folders, never in the real ones (the fpsd one is root's alone, and no
    test lists it). `place` puts the state in temp folders of its own; a
    test without `place` gets a folder that isn't there.

    A tripwire on the script's text, so no test's safety rests on the code
    under test reading the setting: a built script that names a real
    folder fails the test before anything runs it. Returns the code's own
    setting, as it was, for the test that checks it."""
    real = enrollment.FAIRPLAY_STATE_DIRS
    monkeypatch.setattr(enrollment, "FAIRPLAY_STATE_DIRS", (tmp_path / "no-fairplay-state" / "SC Info",))
    built = enrollment.enroll_command

    def checked(*args, **kw) -> enrollment.EnrollCommand:
        command = built(*args, **kw)
        for folder in REAL_STATE_FOLDERS:
            assert folder not in command.script, f"a script built in a test names {folder}"
        return command

    monkeypatch.setattr(enrollment, "enroll_command", checked)
    return real


@pytest.fixture
def place(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Temp stand-ins for /Applications (owned, here, by this user), root's
    work folder, the user's model folder and the fake tools; and for the
    two folders that hold this Mac's FairPlay state: the first with its
    `SC Info.sidb` in it, the second not there at all."""
    apps = (tmp_path / "Applications").resolve()
    apps.mkdir()
    work = (tmp_path / "var-tmp").resolve()
    work.mkdir()
    monkeypatch.setattr(enrollment, "TRUSTED_ROOT", apps)
    monkeypatch.setattr(enrollment, "TRUSTED_OWNER", os.getuid())
    monkeypatch.setattr(enrollment, "WORK_TEMPLATE", str(work / "graft-enroll.XXXXXXXX"))
    state = ((tmp_path / "fpsd" / "SC Info").resolve(), (tmp_path / "shared" / "SC Info").resolve())
    state[0].mkdir(parents=True)
    (state[0] / "SC Info.sidb").write_bytes(b"sidb")
    monkeypatch.setattr(enrollment, "FAIRPLAY_STATE_DIRS", state)
    # The user's temp folder, where the cancel file goes; and the folder the
    # scripts run in, where a name that ran a command would leave its file.
    (tmp_path / "user-tmp").mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp_path / "user-tmp") + "/")
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    fakes = FakeTools(tmp_path / "tools")
    return {
        "cwd": tmp_path / "cwd",
        "user_tmp": tmp_path / "user-tmp",
        "apps": apps,
        "work": work,
        "home": tmp_path / "home",
        "models": tmp_path / "home" / "Library" / "Application Support" / "unfairplay" / "models",
        "fakes": fakes,  # type: ignore[dict-item]
        "up": fakes.unfairplay,
        "state": state,  # type: ignore[dict-item]
    }


def _helper(place, team: str | None = None) -> enrollment.RootHelper:
    """The fake UnfairPlay as root's helper: checked against `team`, or,
    with none, unchecked as a CLI that isn't Graft.app's would have it."""
    if team is None:
        return enrollment.RootHelper(place["up"], unverified_kind=enrollment.UNVERIFIED_NOT_BUNDLED)
    return enrollment.RootHelper(place["up"], team=team)


def _command(place, app: Path, *, team: str | None = None, **kw) -> enrollment.EnrollCommand:
    return enrollment.enroll_command(_helper(place, team), app, place["models"], home=place["home"], **kw)


def _as_this_user(script: str) -> subprocess.CompletedProcess[str]:
    """The script, run by /bin/sh as this user: what root would run, without root."""
    return subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, timeout=60)


# --- The script's text --------------------------------------------------------


def _plain(path: Path | str) -> str:
    """`path`, which the test made, checked to need no quoting of its own,
    so the expected text below can be written out literally rather than
    built with `enrollment.quote`, the function under test (review #5)."""
    assert re.fullmatch(r"[\w/.-]+", str(path)), path
    return str(path)


def test_every_value_in_the_script_is_a_single_quoted_literal(place) -> None:
    """Each value where root reads it, as literal text: the UnfairPlay to
    copy, the requirement, the app, the Applications folder the case
    pattern takes (review #31), the inner app, the work template, the cancel
    file, the folders this Mac's FairPlay state is in, and uid:gid."""
    app = make_ios_app(place["apps"])
    command = _command(place, app, team=TEAM)
    script, apps = command.script, _plain(place["apps"])
    fpsd, shared = (f"'{_plain(folder.parent)}/SC Info'" for folder in place["state"])
    for line in (
        f"D=$(/usr/bin/mktemp -d '{_plain(enrollment.WORK_TEMPLATE)}')",
        f"cancelled() {{ [ -e '{_plain(command.cancel_file)}' ]; }}",
        f"for F in {fpsd}/* {shared}/*; do case \"${{F##*/}}\" in "
        "[Ss][Cc]' '[Ii][Nn][Ff][Oo].[Ss][Ii][Dd][Bb]) "
        "if [ ! -h \"$F\" ] && [ -f \"$F\" ]; then I=1; fi ;; esac; done",
        f"[ -n \"$I\" ] || {{ echo '{NO_STATE}' >&2; exit 69; }}",
        f"/bin/cp '{_plain(place['up'])}' \"$D/unfairplay\"",
        "/usr/bin/codesign --verify --strict -R '=anchor apple generic and identifier \"unfairplay\" "
        "and certificate leaf[subject.OU] = \"TESTTEAM01\"' \"$D/unfairplay\"",
        f"/bin/realpath '{apps}/Example.app' >\"$D/got\"",
        f"case \"$A\" in '{apps}'/*) ;; *) {{ echo 'graft: the app is not in the Applications folder' >&2; exit 64; }} ;; esac",
        "/bin/cp -c -R -p \"$A/Wrapper/\"'Example.app' \"$D/App.app/Wrapper/App.app\"",
        f"/usr/sbin/chown -R -h '{os.getuid()}:{os.getgid()}' \"$D\"",
    ):
        assert line in script.splitlines(), line


def test_the_script_is_root_s_own_folder_its_own_copy_and_absolute_tools(place) -> None:
    script = _command(place, make_ios_app(place["apps"]), team=TEAM).script
    lines = script.splitlines()
    assert lines[:3] == [
        "set -eu", "umask 077", f"D=$(/usr/bin/mktemp -d '{enrollment.WORK_TEMPLATE}')",
    ]
    assert lines.index("trap leave EXIT") < lines.index(f"/bin/cp '{place['up']}' \"$D/unfairplay\"")
    copy = lines.index(f'/bin/cp {enrollment.quote(str(place["up"]))} "$D/unfairplay"')
    verify = next(i for i, line in enumerate(lines) if line.startswith("/usr/bin/codesign --verify --strict -R "))
    clone = lines.index('/bin/cp -c -R -p "$A/Wrapper/"\'Example.app\' "$D/App.app/Wrapper/App.app"')
    recheck = lines.index('C="$D/App.app/Wrapper/App.app"')
    run = lines.index('/usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin HOME="$D/home" TMPDIR="$D/tmp/" '
                      '"$D/unfairplay" setup --app "$D/App.app" --model-dir "$D/models" </dev/null >"$D/log" 2>&1 &')
    give = next(i for i, line in enumerate(lines) if line.startswith("/usr/sbin/chown -R -h "))
    assert copy < verify < clone < recheck < run < give, (
        "UnfairPlay copied, the copy checked, the app cloned, the clone checked, then run, then given back"
    )
    assert lines[-2:] == ["trap - EXIT", 'echo "$D"']
    assert str(place["up"]) not in "\n".join(lines[copy + 1:]), "only the copy ever runs"


def test_the_root_script_never_names_the_user_s_model_folder(place) -> None:
    command = _command(place, make_ios_app(place["apps"]), team=TEAM)
    assert command.model_dir == place["models"]
    assert str(place["models"]) not in command.script
    assert "Application Support" not in command.script
    assert str(place["home"]) not in command.script


def test_the_requirement_is_an_apple_issued_certificate_for_graft_s_team() -> None:
    assert enrollment.requirement(TEAM) == (
        'anchor apple generic and identifier "unfairplay" '
        'and certificate leaf[subject.OU] = "TESTTEAM01"'
    )


@pytest.mark.parametrize("team", ['TEAM"; x', "short", "lowercase1", "TESTTEAM01 "])
def test_a_team_that_isnt_one_is_refused(team: str) -> None:
    with pytest.raises(AppStoreError, match="isn't a team identifier"):
        enrollment.requirement(team)


@pytest.mark.parametrize("kind", [enrollment.UNVERIFIED_AD_HOC, enrollment.UNVERIFIED_NOT_BUNDLED])
def test_an_unchecked_helper_checks_no_signature_and_says_why(place, kind: str) -> None:
    helper = enrollment.RootHelper(place["up"], unverified_kind=kind)
    command = enrollment.enroll_command(helper, make_ios_app(place["apps"]), place["models"], home=place["home"])
    assert command.verified is False
    assert command.requirement is None
    assert command.unverified_kind == kind
    assert command.helper == place["up"]
    assert "codesign" not in command.script
    assert '/bin/cp ' in command.script, "the root-owned copy stays"


def test_the_reasons_say_what_is_true() -> None:
    """Review #1: the old note blamed an unsigned Graft for every unchecked
    script, which was false for a config.yaml UnfairPlay."""
    up = Path("/opt/elsewhere/unfairplay")
    assert enrollment.RootHelper(up, unverified_kind="ad-hoc").unverified_reason == (
        "This copy of Graft has no developer-team signature, so root can't check "
        "UnfairPlay's signature before running it."
    )
    assert enrollment.RootHelper(up, unverified_kind="not-bundled").unverified_reason == (
        "The UnfairPlay root would run is /opt/elsewhere/unfairplay, not Graft.app's own copy, "
        "and nothing checks its signature."
    )
    assert enrollment.RootHelper(up, team=TEAM).unverified_reason is None


@pytest.mark.parametrize("kw", [{}, {"unverified_kind": "whatever"}, {"team": TEAM, "unverified_kind": "ad-hoc"}])
def test_a_helper_is_checked_or_says_why_not_never_both_or_neither(kw) -> None:
    with pytest.raises(ValueError):
        enrollment.RootHelper(Path("/x/unfairplay"), **kw)


def test_a_team_signed_build_checks_the_copy(place) -> None:
    command = _command(place, make_ios_app(place["apps"]), team=TEAM)
    assert command.verified is True
    assert enrollment.quote("=" + enrollment.requirement(TEAM)) in command.script


def _codesign_says(monkeypatch: pytest.MonkeyPatch, *, rc: int = 0, stderr: str = "",
                   raises: BaseException | None = None) -> list[list[str]]:
    seen: list[list[str]] = []

    def codesign(argv, **kw):
        seen.append(argv)
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(argv, rc, "", stderr)

    monkeypatch.setattr(enrollment.subprocess, "run", codesign)
    return seen


def test_the_team_is_read_from_the_app_s_own_signature(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = _codesign_says(monkeypatch, stderr="Identifier=com.example.graft\nTeamIdentifier=TESTTEAM01\n")
    assert enrollment.app_team(tmp_path / "Graft.app") == TEAM
    assert seen == [["/usr/bin/codesign", "-dv", "--verbose=2", str(tmp_path / "Graft.app")]]


def test_a_signature_with_no_team_is_ad_hoc_explicitly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _codesign_says(monkeypatch, stderr="Identifier=com.example.graft\nSignature=adhoc\nTeamIdentifier=not set\n")
    assert enrollment.app_team(tmp_path / "Graft.app") is enrollment.AD_HOC


@pytest.mark.parametrize(
    "said",
    [
        {"raises": OSError(2, "No such file or directory")},
        {"raises": subprocess.TimeoutExpired("/usr/bin/codesign", 30)},
        {"rc": 1, "stderr": "Graft.app: code object is not signed at all\n"},
        {"rc": 0, "stderr": "Identifier=com.example.graft\n"},  # no TeamIdentifier line
        {"rc": 0, "stderr": "TeamIdentifier=TESTTEAM01\nTeamIdentifier=OTHERTEAM1\n"},
        {"rc": 0, "stderr": "TeamIdentifier=lowercase1\n"},  # a format Graft can't read
        {"rc": 1, "stderr": "TeamIdentifier=TESTTEAM01\n"},  # a team, but codesign failed
    ],
    ids=["oserror", "timeout", "nonzero", "no-line", "two-lines", "unreadable", "nonzero-with-team"],
)
def test_a_team_lookup_that_fails_is_an_error_never_ad_hoc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, said: dict
) -> None:
    """Review #28: every one of these used to read as "no team", which made a
    team-signed release's script run its UnfairPlay copy unchecked."""
    _codesign_says(monkeypatch, **said)
    with pytest.raises(AppStoreError) as e:
        enrollment.app_team(tmp_path / "Graft.app")
    assert "Graft sets up decryption only once it knows" in e.value.hint


def test_an_unsigned_folder_is_an_error_not_ad_hoc(tmp_path: Path) -> None:
    """The real codesign, reading a temp folder: it fails, so the lookup does."""
    (tmp_path / "Graft.app").mkdir()
    with pytest.raises(AppStoreError, match="couldn't read Graft.app's signature"):
        enrollment.app_team(tmp_path / "Graft.app")


# --- Refusals, as the user, before anything runs ---------------------------------


#: Crafted app names, each with the single-quoted word root must see,
#: written out by hand. No "/" in these: a name can't hold one, and `check`
#: refuses an executable name with one.
CRAFTED = [
    ("Ex'; touch graft-b3-pwned; '", "'Ex'\"'\"'; touch graft-b3-pwned; '\"'\"''"),
    ("$(touch graft-b3-pwned)", "'$(touch graft-b3-pwned)'"),
    ("`touch graft-b3-pwned`", "'`touch graft-b3-pwned`'"),
    ('x"; touch graft-b3-pwned; "', "'x\"; touch graft-b3-pwned; \"'"),
    ("a;touch graft-b3-pwned", "'a;touch graft-b3-pwned'"),
    ("star*", "'star*'"),
    ("-leading", "'-leading'"),
    ("two words", "'two words'"),
    ("--model-dir", "'--model-dir'"),
]


@pytest.mark.parametrize(("name", "word"), CRAFTED, ids=[n for n, _ in CRAFTED])
def test_a_crafted_app_name_is_one_quoted_word(place, name: str, word: str) -> None:
    app = make_ios_app(place["apps"], name)
    script = _command(place, app).script
    apps = _plain(place["apps"])
    lines = script.splitlines()
    assert word.endswith("'") and f"/bin/realpath '{apps}/{word[1:-1]}.app' >\"$D/got\"" in lines
    assert f"/bin/cp -c -R -p \"$A/Wrapper/\"{word[:-1]}.app' \"$D/App.app/Wrapper/App.app\"" in lines


@pytest.mark.parametrize(("name", "word"), CRAFTED, ids=[n for n, _ in CRAFTED])
def test_a_crafted_app_name_runs_nothing_and_reaches_root_whole(place, tmp_path: Path, name: str, word: str) -> None:
    """Run through /bin/sh (review #5): nothing in the name runs, and the
    name gets to realpath and to the copy as one argument. UnfairPlay is
    given root's copy at a fixed path, so the proof it got the named app is
    what it read there: that app's files, the executable (also the name)
    among them. A sibling for `*` to match shows nothing was globbed."""
    app = make_ios_app(place["apps"], name)
    inner = app / "Wrapper" / f"{name}.app"
    if "*" in name:
        make_ios_app(app / "Wrapper", name.replace("*", "X"))  # a second app a glob would pick up
    script = _command(place, app).script

    run = _as_this_user(script)

    assert run.returncode == 0, run.stderr
    (call,) = place["fakes"].calls("unfairplay")
    assert call["wrapper"] == ["App.app"]
    assert call["files"] == _files(inner, "App.app")
    assert f"App.app/{name}" in call["files"], "the executable, by the crafted name"
    assert not list(tmp_path.rglob("graft-b3-pwned")), "nothing in the name ran"


def test_a_control_character_in_the_path_is_refused(place) -> None:
    app = make_ios_app(place["apps"], "Line\nBreak")
    with pytest.raises(AppStoreError, match="control character"):
        _command(place, app)


def test_a_relative_path_is_refused(place) -> None:
    with pytest.raises(AppStoreError, match="full path"):
        _command(place, Path("Applications/Example.app"))


def test_an_app_that_doesnt_qualify_is_refused(place) -> None:
    app = make_ios_app(place["apps"], encrypted=False)
    with pytest.raises(enrollment.NotACandidate):
        _command(place, app)


def test_a_symlinked_app_is_refused(place, tmp_path: Path) -> None:
    real = make_ios_app(place["apps"] / "deeper")
    link = place["apps"] / "Example.app"
    link.symlink_to(real)
    with pytest.raises(enrollment.NotACandidate, match="symlink"):
        _command(place, link)


def test_an_app_outside_applications_is_refused(place, tmp_path: Path) -> None:
    app = make_ios_app(tmp_path / "Downloads")
    with pytest.raises(enrollment.NotACandidate, match="isn't in"):
        _command(place, app)


def test_an_app_the_user_owns_is_refused(place, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Mac App Store installs as root: a user-owned copy could be anything."""
    monkeypatch.setattr(enrollment, "TRUSTED_OWNER", 0)
    with pytest.raises(enrollment.NotACandidate, match="not the system"):
        _command(place, make_ios_app(place["apps"]))


def test_an_app_others_can_change_is_refused(place) -> None:
    app = make_ios_app(place["apps"])
    app.chmod(0o775)
    with pytest.raises(enrollment.NotACandidate, match="other users"):
        _command(place, app)


def test_the_model_folder_is_the_user_s_own_and_0700(place) -> None:
    _command(place, make_ios_app(place["apps"]))
    for folder in (place["models"], place["models"].parent):
        assert os.lstat(folder).st_uid == os.getuid()
    assert stat.S_IMODE(os.lstat(place["models"]).st_mode) == 0o700


def test_a_symlinked_model_folder_is_refused(place, tmp_path: Path) -> None:
    place["models"].parent.mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    place["models"].symlink_to(tmp_path / "elsewhere")
    with pytest.raises(AppStoreError, match="not a plain folder"):
        _command(place, make_ios_app(place["apps"]))


# --- The script, run as this user -----------------------------------------------


def test_the_script_runs_its_own_copy_and_leaves_only_the_model(place) -> None:
    app = make_ios_app(place["apps"], "Ex'; touch graft-b3-pwned; '")
    run = _as_this_user(_command(place, app).script)
    assert run.returncode == 0, run.stderr
    work = Path(run.stdout.strip())
    assert re.fullmatch(re.escape(enrollment.WORK_TEMPLATE[:-8]) + r"[A-Za-z0-9]{8}", str(work))
    assert sorted(p.name for p in work.iterdir()) == ["models"], "its UnfairPlay copy is gone"
    (call,) = place["fakes"].calls("unfairplay")
    assert call["argv"] == ["setup", "--app", str(work / "App.app"), "--model-dir", str(work / "models")]
    assert call["argv0"] == str(work / "unfairplay"), "the copy ran, never the original"
    assert not list(Path.cwd().glob("graft-b3-pwned")) and not list(place["apps"].rglob("graft-b3-pwned"))


def _env_recorder(root: Path) -> tuple[Path, Path]:
    """An UnfairPlay that writes down the environment it was given, and
    who owns its HOME and TMPDIR and with what mode, then makes a model.

    A /bin/sh script, so the only things added to that environment are
    /bin/sh's own: PWD, SHLVL, and `_` (the command it runs, /usr/bin/env).
    Python would add LC_CTYPE (PEP 538), and its trampoline FAKE_ARGV0.
    """
    root.mkdir(parents=True)
    record = root / "env.txt"
    fake = root / "unfairplay"
    fake.write_text(
        "#!/bin/sh\n"
        f"/usr/bin/env > '{record}'\n"
        f"/usr/bin/stat -f '%Lp %u' \"$HOME\" \"$TMPDIR\" > '{record}.dirs'\n"
        # setup --app APP --model-dir DIR
        '/bin/mkdir -p "$5" && echo model > "$5/m.bin"\n'
    )
    fake.chmod(0o755)
    return fake, record


def test_root_s_unfairplay_gets_a_clean_environment_and_folders_of_root_s_own(place, tmp_path: Path) -> None:
    """Review #2: root's UnfairPlay inherited the caller's whole environment,
    and its `os.MkdirTemp("", …)` work folder landed in the caller's TMPDIR, a
    folder the user owns. Run with every variable it reads set to junk."""
    fake, record = _env_recorder(tmp_path / "recorder")
    helper = enrollment.RootHelper(fake, unverified_kind=enrollment.UNVERIFIED_NOT_BUNDLED)
    script = enrollment.enroll_command(helper, make_ios_app(place["apps"]), place["models"], home=place["home"]).script
    hostile = dict(
        os.environ, TMPDIR=str(tmp_path / "user-tmp") + "/", HOME=str(tmp_path / "user-home"),
        SUDO_UID="0", SUDO_GID="0", SUDO_USER="root", UNFAIRPLAY_MODEL_DIR=str(tmp_path / "models"),
        GODEBUG="junk=1", DYLD_INSERT_LIBRARIES="/junk.dylib",
    )
    cwd = place["cwd"]

    run = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, timeout=60,
                         env=hostile, cwd=cwd)

    assert run.returncode == 0, run.stderr
    work = Path(run.stdout.strip())
    seen = dict(line.split("=", 1) for line in record.read_text().splitlines())
    assert seen == {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": f"{work}/home",
        "TMPDIR": f"{work}/tmp/",
        # /bin/sh's own, from the fake's interpreter; nothing else.
        "PWD": str(cwd), "SHLVL": "1", "_": "/usr/bin/env",
    }
    assert record.with_suffix(".txt.dirs").read_text().split("\n")[:2] == [
        f"700 {os.getuid()}", f"700 {os.getuid()}"
    ], "HOME and TMPDIR existed, root's own (here, the user running the test), 0700"
    assert sorted(p.name for p in work.iterdir()) == ["models"], "the folders went before the handover"


def _files(inner: Path, as_name: str) -> dict[str, str]:
    import hashlib

    return {
        f"{as_name}/{f.relative_to(inner)}": hashlib.sha256(f.read_bytes()).hexdigest()
        for f in sorted(inner.rglob("*")) if f.is_file() and not f.is_symlink()
    }


def test_unfairplay_reads_root_s_own_copy_of_the_app_never_the_app_itself(place) -> None:
    """Review #34: /Applications is admin-writable, so the app root checked
    could be swapped before UnfairPlay opened it again by path. UnfairPlay
    only reads files from it, so it reads root's copy in $D: the app inside
    Wrapper/, byte for byte, and nothing else beside it."""
    app = make_ios_app(place["apps"], "Some Game")
    inner = app / "Wrapper" / "Some Game.app"
    (inner / "Frameworks").mkdir()
    (inner / "Frameworks" / "lib.dylib").write_bytes(b"\0" * 4096)
    before = {p: os.lstat(p).st_mtime_ns for p in [app, *app.rglob("*")]}

    run = _as_this_user(_command(place, app).script)

    assert run.returncode == 0, run.stderr
    work = Path(run.stdout.strip())
    (call,) = place["fakes"].calls("unfairplay")
    assert call["argv"][:3] == ["setup", "--app", f"{work}/App.app"]
    assert call["wrapper"] == ["App.app"], "only the app it checked: nothing else to pick"
    assert call["files"] == _files(inner, "App.app")
    assert {p: os.lstat(p).st_mtime_ns for p in [app, *app.rglob("*")]} == before, "the app is left alone"
    assert sorted(p.name for p in work.iterdir()) == ["models"], "the copy went before the handover"


def _swap_info(app: Path, **changes) -> None:
    """The inner app's Info.plist, changed after Graft's own check: what an
    admin-group process swapping the bundle in /Applications could do."""
    import plistlib

    plist = app / "Wrapper" / f"{app.stem}.app" / "Info.plist"
    info = plistlib.loads(plist.read_bytes())
    info.update(changes)
    plist.write_bytes(plistlib.dumps(info))


@pytest.mark.parametrize("executable", ["..", ".", "", "../Example", "../../../../etc/passwd", "a/b"])
def test_an_executable_outside_the_copy_is_refused_by_root(place, executable: str) -> None:
    app = make_ios_app(place["apps"])
    script = _command(place, app).script
    _swap_info(app, CFBundleExecutable=executable)

    run = _as_this_user(script)

    assert run.returncode == enrollment.EXIT_NOT_ROOTS, run.stderr
    assert run.stderr.strip() == "graft: the app root copied names an executable outside it"
    assert place["fakes"].calls("unfairplay") == []
    assert list(place["work"].iterdir()) == []


def test_an_info_plist_with_no_executable_is_refused_by_root(place) -> None:
    import plistlib

    app = make_ios_app(place["apps"])
    script = _command(place, app).script
    plist = app / "Wrapper" / "Example.app" / "Info.plist"
    plist.write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.example.app"}))
    run = _as_this_user(script)
    assert run.returncode == enrollment.EXIT_NOT_ROOTS
    assert "graft: the app root copied has no Info.plist it can read" in run.stderr
    assert place["fakes"].calls("unfairplay") == []


@pytest.mark.parametrize("swap", ["symlink", "missing", "folder"])
def test_an_executable_that_isn_t_a_plain_file_in_the_copy_is_refused_by_root(
    place, tmp_path: Path, swap: str
) -> None:
    app = make_ios_app(place["apps"])
    script = _command(place, app).script
    exe = app / "Wrapper" / "Example.app" / "Example"
    (tmp_path / "outside").write_bytes(b"root-only bytes")
    exe.unlink()
    if swap == "symlink":
        exe.symlink_to(tmp_path / "outside")
    elif swap == "folder":
        exe.mkdir()

    run = _as_this_user(script)

    assert run.returncode == enrollment.EXIT_NOT_ROOTS, run.stderr
    assert run.stderr.strip() == "graft: the app root copied has no executable file where it says"
    assert place["fakes"].calls("unfairplay") == []
    assert list(place["work"].iterdir()) == []


def test_an_inner_app_swapped_for_a_link_after_the_check_is_refused_on_the_copy(place, tmp_path: Path) -> None:
    """The fix re-check's problem 2: Wrapper/<inner> swapped for a link to an
    app outside /Applications between Graft's check and root's clone. cp -R
    copies a link as a link, `[ -d ]` follows it and `stat` reads the link's
    own owner (here this user, the trusted owner), so only root's `[ ! -h ]`
    keeps UnfairPlay from being handed a tree outside $D."""
    app = make_ios_app(place["apps"])
    script = _command(place, app).script
    elsewhere = make_ios_app(tmp_path / "elsewhere") / "Wrapper" / "Example.app"
    inner = app / "Wrapper" / "Example.app"
    os.rename(inner, tmp_path / "moved-inner.app")
    inner.symlink_to(elsewhere)

    run = _as_this_user(script)

    assert run.returncode == enrollment.EXIT_NOT_ROOTS, run.stderr
    assert run.stderr.strip() == "graft: root's copy of the app is not the system's"
    assert place["fakes"].calls("unfairplay") == []
    assert list(place["work"].iterdir()) == []


def test_an_inner_app_made_writable_after_the_check_is_refused_on_the_copy(place) -> None:
    """The copy keeps the mode (cp -p), and root checks the copy."""
    app = make_ios_app(place["apps"])
    script = _command(place, app).script
    (app / "Wrapper" / "Example.app").chmod(0o775)
    run = _as_this_user(script)
    assert run.returncode == enrollment.EXIT_WRITABLE, run.stderr
    assert run.stderr.strip() == "graft: others could change the app root copied"
    assert place["fakes"].calls("unfairplay") == []


def test_a_copy_root_doesn_t_own_is_refused(place) -> None:
    """Root's own check of the copy's owner, which the copy keeps (cp -p),
    seen as root would: the owner it expects rewritten to 0, the way the
    review's verifier did, here for the copy's line only."""
    app = make_ios_app(place["apps"])
    script = _command(place, app).script
    mine = f'/usr/bin/stat -f %u "$C" >"$D/got" && [ "$(/bin/cat "$D/got")" = {os.getuid()} ]'
    assert script.count(mine) == 1
    run = _as_this_user(script.replace(mine, '/usr/bin/stat -f %u "$C" >"$D/got" && [ "$(/bin/cat "$D/got")" = 0 ]'))
    assert run.returncode == enrollment.EXIT_NOT_ROOTS, run.stderr
    assert run.stderr.strip() == "graft: root's copy of the app is not the system's"
    assert place["fakes"].calls("unfairplay") == []
    assert list(place["work"].iterdir()) == []


def _flagged(path: Path, flag: str) -> None:
    subprocess.run(["/usr/bin/chflags", flag, str(path)], check=True)


@pytest.fixture
def unflag():
    """Paths a test gave the user-immutable flag, cleared again afterward
    so the temp folder can be removed."""
    paths: list[Path] = []
    yield paths
    for p in paths:
        subprocess.run(["/usr/bin/chflags", "-R", "nouchg", str(p)], check=False)


def test_a_file_flagged_immutable_in_the_app_doesn_t_keep_the_copy_from_going(place, unflag) -> None:
    """cp -p copies file flags too; root clears them on its copy, or the
    copy (and so $D) could never be removed."""
    app = make_ios_app(place["apps"])
    locked = app / "Wrapper" / "Example.app" / "locked.dat"
    locked.write_bytes(b"x")
    _flagged(locked, "uchg")
    unflag.append(app)

    run = _as_this_user(_command(place, app).script)

    assert run.returncode == 0, run.stderr
    assert sorted(p.name for p in Path(run.stdout.strip()).iterdir()) == ["models"]


def test_a_copy_that_fails_halfway_still_leaves_nothing(place, unflag) -> None:
    """A copy that stops with a flagged file already in it: the trap clears
    the flags before it removes $D."""
    app = make_ios_app(place["apps"])
    inner = app / "Wrapper" / "Example.app"
    (inner / "a-locked.dat").write_bytes(b"x")
    _flagged(inner / "a-locked.dat", "uchg")
    unflag.append(app)
    script = _command(place, app).script
    (inner / "z-unreadable.dat").write_bytes(b"y")
    (inner / "z-unreadable.dat").chmod(0)

    run = _as_this_user(script)

    assert run.returncode != 0
    assert place["fakes"].calls("unfairplay") == []
    assert list(place["work"].iterdir()) == []


def test_a_failed_setup_leaves_nothing_behind(place) -> None:
    place["fakes"].set(setup_error="UnfairPlay: no license")
    run = _as_this_user(_command(place, make_ios_app(place["apps"])).script)
    assert run.returncode != 0
    assert "no license" in run.stderr
    assert list(place["work"].iterdir()) == []


# --- Stopping it: the cancel file, the time limit ------------------------------


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def hang(place):
    """The fake UnfairPlay hangs in setup, with a child of its own. Yields a
    starter: it runs the script in the background and returns it with the
    fake's `started` record once the fake is running. Each script starts a
    session of its own, so whatever a broken one left running, its
    watchdog included, is killed afterward as a group."""
    place["fakes"].set(setup_sleep=30, setup_child=True)
    procs: list[subprocess.Popen] = []

    def start(script: str, *, wait_for_start: bool = True) -> tuple[subprocess.Popen, dict | None]:
        proc = subprocess.Popen(["/bin/sh", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        procs.append(proc)
        if not wait_for_start:
            return proc, None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            started = [c for c in place["fakes"].calls("unfairplay") if c.get("started")]
            if started:
                return proc, started[0]
            assert proc.poll() is None, proc.communicate()
            time.sleep(0.05)
        pytest.fail("the fake UnfairPlay never started")

    yield start
    for proc in procs:
        try:
            os.killpg(proc.pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait(timeout=10)
    for c in place["fakes"].calls("unfairplay"):
        for pid in (c.get("pid"), c.get("child")):
            if pid and _alive(pid):
                os.kill(pid, 9)


def _nothing_runs(fakes: FakeTools) -> bool:
    """No process is running anything from the fakes' folder: the fake
    UnfairPlay and its child are gone, however early they were stopped."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        ps = subprocess.run(["/bin/ps", "-axo", "command="], capture_output=True, text=True).stdout
        if not any(str(fakes.root) in line and "/bin/sh -c" not in line for line in ps.splitlines()):
            return True
        time.sleep(0.05)
    return False


def _gone(*pids: int) -> bool:
    deadline = time.monotonic() + 5  # a killed orphan is reaped by launchd, not at once
    while time.monotonic() < deadline:
        if not any(_alive(p) for p in pids):
            return True
        time.sleep(0.05)
    return False


def _cancel_file(command: enrollment.EnrollCommand) -> Path:
    return command.cancel_file


@pytest.mark.parametrize("ignores_term", [False, True], ids=["dies-on-term", "ignores-term"])
def test_a_cancel_file_made_during_the_run_stops_unfairplay_and_its_child(place, hang, ignores_term) -> None:
    """TERM, a grace, then KILL: an UnfairPlay that ignored TERM still goes."""
    place["fakes"].set(setup_ignore_term=ignores_term)
    command = _command(place, make_ios_app(place["apps"]))
    proc, started = hang(command.script)

    fd = os.open(command.cancel_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)  # as the app does
    os.close(fd)
    out, err = proc.communicate(timeout=20)

    assert proc.returncode == enrollment.EXIT_CANCELLED == 67, err
    assert err.strip().splitlines()[-1] == "graft: stopped: the enrollment was canceled while UnfairPlay ran"
    assert "Killed" not in err and "Terminated" not in err and "trap" not in err, (
        "no job notices from the shell, with the script's text in them"
    )
    assert _nothing_runs(place["fakes"])
    assert out == "", "no folder for finish"
    assert _gone(started["pid"], started["child"]), "UnfairPlay and what it started were killed"
    assert list(place["work"].iterdir()) == [], "root's folder is gone"
    assert [c for c in place["fakes"].calls("unfairplay") if not c.get("started")] == [], "it never finished"
    if not ignores_term:
        assert place["fakes"].calls("unfairplay-signal") == [{"tool": "unfairplay-signal", "signal": "TERM"}], (
            "TERM first, with a grace, not KILL at once"
        )


def test_a_script_killed_outright_leaves_no_watchdog(place, hang) -> None:
    """A script KILLed (no trap runs) leaves its folder, but its watchdog,
    no child of the script's, goes within a couple of seconds rather than
    living on to the time limit."""
    command = _command(place, make_ios_app(place["apps"]))
    proc, _ = hang(command.script)

    os.kill(proc.pid, 9)
    proc.wait(timeout=10)

    def shells_left() -> list[str]:
        ps = subprocess.run(["/bin/ps", "-axo", "pgid=,command="], capture_output=True, text=True).stdout
        return [line for line in ps.splitlines()
                if line.split(None, 1)[0] == str(proc.pid) and "/bin/sh -c" in line]

    deadline = time.monotonic() + 5
    while shells_left() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert shells_left() == [], "the watchdog noticed the script was gone"


def test_a_run_past_the_time_limit_is_stopped(place, hang, monkeypatch: pytest.MonkeyPatch) -> None:
    """On a loaded Mac the fake may not even have started when the limit
    passes, so "killed" is that nothing of it still runs."""
    monkeypatch.setattr(enrollment, "TIME_LIMIT_SECONDS", 4)
    command = _command(place, make_ios_app(place["apps"]))
    assert command.time_limit_seconds == 4
    proc, started = hang(command.script, wait_for_start=False)

    out, err = proc.communicate(timeout=30)

    assert proc.returncode == enrollment.EXIT_TIME_LIMIT == 68, err
    assert err.strip().splitlines()[-1] == "graft: stopped: the enrollment ran for more than 4 seconds"
    assert out == ""
    assert _nothing_runs(place["fakes"])
    assert [c for c in place["fakes"].calls("unfairplay") if not c.get("started")] == [], "it never finished"
    assert list(place["work"].iterdir()) == []


def _fifo_helper(place) -> enrollment.RootHelper:
    """An UnfairPlay swapped for a FIFO after root_helper looked at it (the
    fix re-check's problem 1): root's `/bin/cp` of it opens the FIFO and
    blocks, with no writer ever to come."""
    fifo = place["fakes"].root / "unfairplay-fifo"
    os.mkfifo(fifo)
    return enrollment.RootHelper(fifo, unverified_kind=enrollment.UNVERIFIED_NOT_BUNDLED)


def _stopped_before_unfairplay(place, proc: subprocess.Popen, status: int, last: str) -> None:
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == status, err
    assert err.strip().splitlines()[-1] == last
    assert out == "", "no folder for finish"
    assert _nothing_runs(place["fakes"]), "root's cp of the FIFO, and anything else, is gone"
    assert list(place["work"].iterdir()) == [], "root's folder is gone"
    assert place["fakes"].calls("unfairplay") == [], "UnfairPlay never started"


def test_a_cancel_file_stops_a_copy_that_blocks_before_unfairplay(place, hang) -> None:
    """The cancel file is watched from the start of the run, not only once
    UnfairPlay runs: a `/bin/cp` that never returns is stopped too."""
    command = enrollment.enroll_command(_fifo_helper(place), make_ios_app(place["apps"]), place["models"],
                                        home=place["home"])
    proc, _ = hang(command.script, wait_for_start=False)
    time.sleep(1)
    assert proc.poll() is None, "blocked in the copy"
    assert len(list(place["work"].iterdir())) == 1

    fd = os.open(command.cancel_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)  # as the app does
    os.close(fd)

    _stopped_before_unfairplay(
        place, proc, enrollment.EXIT_CANCELLED,
        "graft: stopped: the enrollment was canceled before UnfairPlay started",
    )


def test_the_time_limit_covers_a_copy_that_blocks_before_unfairplay(place, hang, monkeypatch) -> None:
    monkeypatch.setattr(enrollment, "TIME_LIMIT_SECONDS", 2)
    command = enrollment.enroll_command(_fifo_helper(place), make_ios_app(place["apps"]), place["models"],
                                        home=place["home"])
    assert command.time_limit_seconds == 2
    proc, _ = hang(command.script, wait_for_start=False)

    _stopped_before_unfairplay(
        place, proc, enrollment.EXIT_TIME_LIMIT, "graft: stopped: the enrollment ran for more than 2 seconds",
    )


def test_a_cancel_file_stops_a_step_that_blocks_after_root_s_checks(place, hang) -> None:
    """Later steps are covered too. A swapped file can't block them (cp -R
    makes a FIFO in the app rather than reading it, and plutil refuses one,
    both measured), so the clone here stands for one that never returns, a
    stuck volume say: the script's clone line, rewritten to read a FIFO
    first, the way other tests here rewrite a line to see it as root would."""
    fifo = place["fakes"].root / "clone-fifo"
    os.mkfifo(fifo)
    command = _command(place, make_ios_app(place["apps"]))
    clone = '/bin/cp -c -R -p "$A/Wrapper/"\'Example.app\' "$D/App.app/Wrapper/App.app"'
    assert command.script.count(clone) == 1
    proc, _ = hang(command.script.replace(clone, f"/bin/cat '{fifo}' && {clone}"), wait_for_start=False)
    time.sleep(1.5)
    assert proc.poll() is None, "blocked in the clone"

    command.cancel_file.touch()

    _stopped_before_unfairplay(
        place, proc, enrollment.EXIT_CANCELLED,
        "graft: stopped: the enrollment was canceled before UnfairPlay started",
    )


def test_a_large_app_gets_longer_by_its_size(place) -> None:
    """UnfairPlay setup zips the whole inner app (installedValidationIPA), so
    past `LARGE_APP_BYTES` the limit grows by a second for each
    `SETUP_BYTES_PER_SECOND`. The size is measured here, as the user, and
    reaches root as a literal. A sparse file: its size, none of the disk."""
    app = make_ios_app(place["apps"])
    with open(app / "Wrapper" / "Example.app" / "big.pak", "wb") as f:
        f.truncate(enrollment.LARGE_APP_BYTES + 1_000 * enrollment.SETUP_BYTES_PER_SECOND)

    command = _command(place, app)

    assert command.time_limit_seconds == enrollment.TIME_LIMIT_SECONDS + 1_000 + 1
    assert f'    if [ "$T" -ge {command.time_limit_seconds} ]; then' in command.script


def test_the_time_limit_for_each_size() -> None:
    """The numbers: 900 s up to 6 GB, then one second per 10 MB, at most
    four hours (`enrollment.TIME_LIMIT_SECONDS`'s comment has the
    measurements)."""
    assert enrollment.time_limit(0) == 900
    assert enrollment.time_limit(6_000_000_000) == 900
    assert enrollment.time_limit(6_000_000_001) == 901
    assert enrollment.time_limit(16_000_000_000) == 1_900
    assert enrollment.time_limit(10**15) == 4 * 60 * 60


def test_the_size_counts_regular_files_and_never_follows_a_link(tmp_path: Path) -> None:
    inner = tmp_path / "App.app"
    (inner / "sub").mkdir(parents=True)
    (inner / "a").write_bytes(b"x" * 10)
    (inner / "sub" / "b").write_bytes(b"y" * 5)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "huge").write_bytes(b"z" * 1000)
    (inner / "link-dir").symlink_to(outside)
    (inner / "link-file").symlink_to(outside / "huge")
    os.mkfifo(inner / "fifo")
    assert enrollment.app_bytes(inner) == 15


def test_the_size_never_follows_an_inner_app_that_is_a_link(tmp_path: Path) -> None:
    """Delta audit try13, #4: os.walk read its top through a link, so a
    Wrapper/<X>.app that is a symlink was measured at its target, every
    file under it walked, before root refused the link (65)."""
    target = tmp_path / "elsewhere"
    (target / "sub").mkdir(parents=True)
    (target / "sub" / "big").write_bytes(b"z" * 5000)
    (tmp_path / "Wrapper").mkdir()
    inner = tmp_path / "Wrapper" / "In.app"
    inner.symlink_to(target)
    assert enrollment.app_bytes(target) == 5000, "premise: the target is measured"
    assert enrollment.app_bytes(inner) == 0
    assert enrollment.app_bytes(tmp_path / "not-there.app") == 0, "what can't be read counts as nothing"


def test_an_inner_app_that_is_a_link_gets_the_plain_time_limit_and_root_still_refuses_it(
    place, tmp_path: Path
) -> None:
    """`check` follows the link and accepts the app behind it; the size
    doesn't, so the limit is the plain one, not the target's. What root runs
    is the same script as ever, and it refuses the link on its copy."""
    app = make_ios_app(place["apps"])
    inner = app / "Wrapper" / "Example.app"
    moved = tmp_path / "elsewhere" / "Example.app"
    moved.parent.mkdir()
    os.rename(inner, moved)
    with open(moved / "big.pak", "wb") as f:
        f.truncate(enrollment.LARGE_APP_BYTES + 1_000 * enrollment.SETUP_BYTES_PER_SECOND)
    inner.symlink_to(moved)

    command = _command(place, app)

    assert command.time_limit_seconds == enrollment.TIME_LIMIT_SECONDS
    run = _as_this_user(command.script)
    assert run.returncode == enrollment.EXIT_NOT_ROOTS, run.stderr
    assert run.stderr.strip() == "graft: root's copy of the app is not the system's"
    assert place["fakes"].calls("unfairplay") == []


def test_a_cancel_file_there_before_the_start_stops_at_once(place) -> None:
    """A Cancel pressed while macOS's password dialog is up: nothing runs."""
    command = _command(place, make_ios_app(place["apps"]))
    command.cancel_file.touch()
    began = time.monotonic()

    run = _as_this_user(command.script)

    assert run.returncode == 67, run.stderr
    assert run.stderr.strip() == "graft: stopped: the enrollment was canceled before UnfairPlay started"
    assert time.monotonic() - began < 5
    assert place["fakes"].calls("unfairplay") == []
    assert list(place["work"].iterdir()) == []


def test_a_signal_to_the_script_stops_unfairplay_and_removes_root_s_folder(place, hang) -> None:
    """Ctrl-C under sudo, say: the trap stops UnfairPlay and waits for it
    before the folder goes."""
    command = _command(place, make_ios_app(place["apps"]))
    proc, started = hang(command.script)

    proc.send_signal(15)
    out, err = proc.communicate(timeout=20)

    assert proc.returncode == 143, err
    assert _gone(started["pid"], started["child"])
    assert list(place["work"].iterdir()) == []


def test_a_second_signal_during_the_stop_doesn_t_cut_it_short(place, hang) -> None:
    """Ctrl-C pressed again while `leave` is stopping UnfairPlay (its grace
    second): `leave` still finishes, so an UnfairPlay that ignores TERM is
    still KILLed and root's folder still goes."""
    place["fakes"].set(setup_ignore_term=True)
    command = _command(place, make_ios_app(place["apps"]))
    proc, started = hang(command.script)

    proc.send_signal(15)
    time.sleep(0.4)
    proc.send_signal(15)
    proc.communicate(timeout=20)

    assert proc.returncode == 143
    assert _gone(started["pid"], started["child"]), "UnfairPlay and what it started were killed"
    assert list(place["work"].iterdir()) == []


def _reader_gone() -> tuple[int, int]:
    """stdout and stderr for a child whose reader has quit: what root's
    script has once Graft quits mid-setup."""
    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()
    os.close(out_r)
    os.close(err_r)
    return out_w, err_w


def test_a_reader_that_quits_doesn_t_kill_unfairplay_mid_setup(place) -> None:
    """UnfairPlay writes to a file in $D, not to the pipe the script was
    given: a Graft that quits can't kill it with SIGPIPE halfway through."""
    command = _command(place, make_ios_app(place["apps"]))
    out_w, err_w = _reader_gone()
    try:
        proc = subprocess.run(["/bin/sh", "-c", command.script], stdout=out_w, stderr=err_w, timeout=60)
    finally:
        os.close(out_w)
        os.close(err_w)
    assert proc.returncode != 0, "it couldn't say where the model is"
    (call,) = place["fakes"].calls("unfairplay")
    assert call["argv"][0] == "setup", "UnfairPlay ran to the end"
    assert list(place["work"].iterdir()) == [], "and the folder went with the script"


def test_a_reader_that_quits_before_a_refusal_still_leaves_nothing(place, tmp_path: Path) -> None:
    """The script's own messages hit the dead pipe too; the trap still runs."""
    app = make_ios_app(place["apps"])
    command = _command(place, app)
    os.rename(app, tmp_path / "moved.app")
    app.symlink_to(make_ios_app(tmp_path / "elsewhere"))  # refused, 64, with a message
    out_w, err_w = _reader_gone()
    try:
        proc = subprocess.run(["/bin/sh", "-c", command.script], stdout=out_w, stderr=err_w, timeout=60)
    finally:
        os.close(out_w)
        os.close(err_w)
    assert proc.returncode != 0
    assert list(place["work"].iterdir()) == []


#: The script's own exit statuses, written out: each means something to the
#: app (`CLIContract.EnrolExit`), so UnfairPlay may never end with one.
SCRIPT_STATUSES = [64, 65, 66, 67, 68, 69]


def test_the_script_s_own_statuses_are_these() -> None:
    assert sorted(v for k, v in vars(enrollment).items() if k.startswith("EXIT_")) == SCRIPT_STATUSES


@pytest.mark.parametrize("status", SCRIPT_STATUSES)
def test_unfairplay_s_own_exit_status_is_never_one_of_the_script_s(place, status: int) -> None:
    place["fakes"].set(setup_error="UnfairPlay: something", setup_exit=status)
    run = _as_this_user(_command(place, make_ios_app(place["apps"])).script)
    assert run.returncode == 1, run.stderr
    assert "something" in run.stderr


#: /bin/sh is bash unless someone points /private/var/select/sh at dash or
#: zsh, which macOS allows; the script has to work in each.
SHELLS = {
    "bash": ["/bin/bash", "--posix", "-c"],
    "dash": ["/bin/dash", "-c"],
    "zsh": ["/bin/zsh", "--emulate", "sh", "-c"],
}


@pytest.mark.parametrize("shell", sorted(SHELLS))
def test_the_script_runs_and_stops_alike_in_each_shell_sh_can_be(place, shell: str) -> None:
    if not Path(SHELLS[shell][0]).exists():
        pytest.skip(f"no {SHELLS[shell][0]}")
    command = _command(place, make_ios_app(place["apps"], "Some Game"))
    run = subprocess.run([*SHELLS[shell], command.script], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    assert sorted(p.name for p in Path(run.stdout.strip()).iterdir()) == ["models"]
    shutil.rmtree(run.stdout.strip())

    place["fakes"].set(setup_sleep=30, setup_child=True)
    proc = subprocess.Popen([*SHELLS[shell], command.script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        deadline = time.monotonic() + 20
        while not [c for c in place["fakes"].calls("unfairplay") if c.get("started")]:
            assert time.monotonic() < deadline and proc.poll() is None, proc.communicate()
            time.sleep(0.05)
        command.cancel_file.touch()
        out, err = proc.communicate(timeout=20)
    finally:
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
    assert proc.returncode == 67, err
    assert err.strip().splitlines()[-1] == "graft: stopped: the enrollment was canceled while UnfairPlay ran"
    assert _nothing_runs(place["fakes"])
    assert list(place["work"].iterdir()) == []


@pytest.mark.parametrize("shell", sorted(SHELLS))
def test_a_copy_that_blocks_is_stopped_alike_in_each_shell(place, shell: str) -> None:
    """Each shell takes TERM only once a foreground command ends, and zsh
    runs the trap inside that command's redirections: the watchdog's kill,
    and `leave`'s message, work in all three."""
    if not Path(SHELLS[shell][0]).exists():
        pytest.skip(f"no {SHELLS[shell][0]}")
    command = enrollment.enroll_command(_fifo_helper(place), make_ios_app(place["apps"]), place["models"],
                                        home=place["home"])
    proc = subprocess.Popen([*SHELLS[shell], command.script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        time.sleep(1)
        assert proc.poll() is None, "blocked in the copy"
        command.cancel_file.touch()
        _stopped_before_unfairplay(
            place, proc, enrollment.EXIT_CANCELLED,
            "graft: stopped: the enrollment was canceled before UnfairPlay started",
        )
    finally:
        try:
            os.killpg(proc.pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait(timeout=10)


#: Each step root runs whose output the script reads (delta audit #3), as
#: (the tool's absolute path, the arguments a stub blocks on, or None for
#: any): realpath and plutil, and stat of the app's owner and mode and the
#: copy's owner.
BLOCKING_STEPS = {
    "realpath": ("/bin/realpath", None),
    "stat-app-owner": ("/usr/bin/stat", "'-f %u '*/Applications/*"),
    "stat-app-mode": ("/usr/bin/stat", "'-f %Sp '*/Applications/*"),
    "stat-copy-owner": ("/usr/bin/stat", "'-f %u '*/App.app/Wrapper/App.app"),
    "plutil": ("/usr/bin/plutil", None),
}


def _blocking_stub(place, tool: str, when: str | None) -> Path:
    """`tool`, as a stub that blocks when its arguments match `when` (it
    becomes `sleep`, one process, as a stat stuck on a volume would be one),
    and is the real tool otherwise."""
    stub = place["fakes"].root / f"blocking-{Path(tool).name}"
    match = f'case "$*" in {when}) exec /bin/sleep 600 ;; esac\n' if when else "exec /bin/sleep 600\n"
    stub.write_text(f'#!/bin/sh\n{match}exec {tool} "$@"\n')
    stub.chmod(0o755)
    return stub


@pytest.mark.parametrize("step", sorted(BLOCKING_STEPS))
@pytest.mark.parametrize("shell", sorted(SHELLS))
def test_a_step_whose_output_root_reads_is_stopped_alike_in_each_shell(place, shell: str, step: str) -> None:
    """Delta audit #3: zsh, as sh, takes the watchdog's TERM at once while it
    reads a command substitution, so a step blocked inside `$(...)` was left
    running, orphaned, holding the script's stderr, and the admin run never
    ended. No step that can block runs inside one now: each writes to a
    file in root's folder, which is read afterward."""
    if not Path(SHELLS[shell][0]).exists():
        pytest.skip(f"no {SHELLS[shell][0]}")
    tool, when = BLOCKING_STEPS[step]
    command = _command(place, make_ios_app(place["apps"]))
    assert command.script.count(tool + " ") >= 1
    script = command.script.replace(tool + " ", f"{_blocking_stub(place, tool, when)} ")
    proc = subprocess.Popen([*SHELLS[shell], script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        deadline = time.monotonic() + 20
        while "/bin/sleep 600" not in subprocess.run(["/usr/bin/pgrep", "-lf", "-g", str(proc.pid)],
                                                     capture_output=True, text=True).stdout:
            assert time.monotonic() < deadline and proc.poll() is None, proc.communicate(timeout=5)
            time.sleep(0.05)
        command.cancel_file.touch()
        _stopped_before_unfairplay(
            place, proc, enrollment.EXIT_CANCELLED,
            "graft: stopped: the enrollment was canceled before UnfairPlay started",
        )
        assert _gone_from_group(proc.pid, "/bin/sleep 600"), "the blocked step was killed, not left running"
    finally:
        try:
            os.killpg(proc.pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait(timeout=10)


def _gone_from_group(pgid: int, command: str) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if command not in subprocess.run(["/usr/bin/pgrep", "-lf", "-g", str(pgid)],
                                         capture_output=True, text=True).stdout:
            return True
        time.sleep(0.05)
    return False


def test_no_step_that_can_block_runs_inside_a_command_substitution(place) -> None:
    """Delta audit #3: only `/bin/cat` of root's own file, which can't block,
    is read through `$(...)` after the watchdog starts (the watchdog's own
    `$(...)` only prints its pid)."""
    lines = _command(place, make_ios_app(place["apps"])).script.splitlines()
    watchdog_end = next(i for i, line in enumerate(lines) if line.startswith(") </dev/null >/dev/null 2>&1 &"))
    inside = [m for line in lines[watchdog_end + 1:] for m in re.findall(r"\$\((/\S+)", line)]
    assert inside and set(inside) == {"/bin/cat"}, inside
    assert '/bin/rm -rf "$D/unfairplay" "$D/App.app" "$D/tmp" "$D/home" "$D/log" "$D/got"' in lines


def test_a_normal_run_neither_waits_for_the_limit_nor_leaves_the_watchdog(place) -> None:
    command = _command(place, make_ios_app(place["apps"]))
    began = time.monotonic()
    run = _as_this_user(command.script)
    assert run.returncode == 0, run.stderr
    assert time.monotonic() - began < 10
    ps = subprocess.run(["/bin/ps", "-axo", "command="], capture_output=True, text=True).stdout
    assert str(command.cancel_file) not in ps


@pytest.mark.parametrize("target_kind", ["file", "folder"])
def test_a_link_planted_at_the_cancel_path_can_only_cancel(place, tmp_path: Path, target_kind: str) -> None:
    """Root only tests the path with `[ -e ]`: a link there, to anything,
    stops the run, and its target is left exactly as it was."""
    target = tmp_path / f"precious-{target_kind}"
    if target_kind == "file":
        target.write_bytes(b"keep me")
        target.chmod(0o640)
    else:
        target.mkdir(mode=0o750)
        (target / "inside").write_bytes(b"keep me too")
    os.utime(target, ns=(1_000_000_000, 1_000_000_000))
    before = (os.lstat(target), sorted(os.listdir(target)) if target.is_dir() else target.read_bytes())
    command = _command(place, make_ios_app(place["apps"]))
    command.cancel_file.symlink_to(target)

    run = _as_this_user(command.script)

    assert run.returncode == 67, run.stderr
    after = (os.lstat(target), sorted(os.listdir(target)) if target.is_dir() else target.read_bytes())
    assert after[1] == before[1]
    for field in ("st_mode", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ino", "st_nlink"):
        assert getattr(after[0], field) == getattr(before[0], field), field
    assert os.readlink(command.cancel_file) == str(target), "the link itself is left alone too"
    assert place["fakes"].calls("unfairplay") == []
    assert list(place["work"].iterdir()) == []


def test_a_crafted_cancel_path_is_one_literal(place) -> None:
    cancel = place["user_tmp"] / "it's $(touch graft-b3-pwned) here"
    assert re.fullmatch(r"[\w/.-]+", str(place["user_tmp"])), "the test's own folder needs no quoting"
    command = _command(place, make_ios_app(place["apps"]), cancel_file=cancel)
    assert (
        f"cancelled() {{ [ -e '{place['user_tmp']}/it'\"'\"'s $(touch graft-b3-pwned) here' ]; }}"
        in command.script
    )
    cancel.touch()
    run = _as_this_user(command.script)
    assert run.returncode == 67, run.stderr
    assert not list(place["cwd"].glob("graft-b3-pwned")), "nothing in the path ran"


def test_the_script_names_the_cancel_path_once_and_only_to_test_it(place) -> None:
    command = _command(place, make_ios_app(place["apps"]))
    path = str(command.cancel_file)
    assert command.script.count(path) == 1
    assert command.script.count(f"[ -e '{path}' ]") == 1
    assert command.script.count("cancelled() {") == 1
    assert command.script.count("if cancelled; then") == 3, "before the copy, before the start, and by the watchdog"


def test_a_copy_that_fails_its_signature_check_never_runs(place) -> None:
    """The fake isn't signed by any team: the real codesign refuses it."""
    run = _as_this_user(_command(place, make_ios_app(place["apps"]), team=TEAM).script)
    assert run.returncode != 0
    assert place["fakes"].calls("unfairplay") == []
    assert list(place["work"].iterdir()) == []


def test_an_app_swapped_for_a_link_after_the_check_is_refused_by_root(place, tmp_path: Path) -> None:
    """The root script checks the app again itself: realpath, then where it is."""
    app = make_ios_app(place["apps"])
    script = _command(place, app).script
    elsewhere = make_ios_app(tmp_path / "elsewhere")
    os.rename(app, tmp_path / "moved.app")
    app.symlink_to(elsewhere)
    run = _as_this_user(script)
    assert run.returncode == enrollment.EXIT_NOT_IN_APPLICATIONS
    assert place["fakes"].calls("unfairplay") == []


@pytest.mark.parametrize(
    ("name", "mode"), [("Example", 0o777), ("Example", 0o775), ("Some Game", 0o777), ("Some Game", 0o757)],
)
def test_an_app_made_writable_after_the_check_is_refused_by_root(place, name: str, mode: int) -> None:
    """Review #23: group-writable (0775) and other-writable alike, and a
    name with a space in it, which an unquoted "$A" would split."""
    app = make_ios_app(place["apps"], name)
    script = _command(place, app).script
    app.chmod(mode)
    run = _as_this_user(script)
    assert run.returncode == enrollment.EXIT_WRITABLE, run.stderr
    assert run.stderr.strip() == "graft: others can change the app"
    assert place["fakes"].calls("unfairplay") == []
    assert list(place["work"].iterdir()) == []


def test_an_app_root_doesn_t_own_is_refused_by_root(place) -> None:
    """Review #6: root's own check of the app's owner (exit 65), never run
    before. As root sees it: this user's app, with the owner the script
    expects rewritten to 0, the way the review's verifier did, on the app's
    line only (the copy's has a test of its own)."""
    app = make_ios_app(place["apps"], "Some Game")
    script = _command(place, app).script
    mine = f'/usr/bin/stat -f %u "$A" >"$D/got" && [ "$(/bin/cat "$D/got")" = {os.getuid()} ]'
    assert script.count(mine) == 1
    run = _as_this_user(script.replace(mine, '/usr/bin/stat -f %u "$A" >"$D/got" && [ "$(/bin/cat "$D/got")" = 0 ]'))
    assert run.returncode == enrollment.EXIT_NOT_ROOTS == 65, run.stderr
    assert run.stderr.strip() == "graft: the app is not the system's"
    assert place["fakes"].calls("unfairplay") == []
    assert list(place["work"].iterdir()) == []


# --- This Mac's FairPlay state, looked for before anything is copied -------------
#
# The owner's run: after a restart, /private/var/db/fpsd/SC Info was empty,
# and UnfairPlay failed only at its end ("normalize capture: canonical input
# has no SIDB state"). Opening the picked app once wrote `SC Info.sidb`
# again. UnfairPlay's capture looks in that folder and /Users/Shared/SC Info
# for a file of that name, in any case. So root looks first, and stops with
# exit 69 before it copies UnfairPlay or the app.


def test_root_looks_where_fairplay_keeps_this_mac_s_state(real_fairplay_state: tuple[Path, ...]) -> None:
    """The setting as the code has it; the folders themselves are never read."""
    assert real_fairplay_state == (Path("/private/var/db/fpsd/SC Info"), Path("/Users/Shared/SC Info"))


def test_root_looks_once_the_watchdog_runs_and_before_any_copy(place) -> None:
    lines = _command(place, make_ios_app(place["apps"])).script.splitlines()
    first_cancel = lines.index('if cancelled; then : >"$D/stop-67"; exit 67; fi')
    watchdog_end = next(i for i, line in enumerate(lines) if line.startswith(") </dev/null >/dev/null 2>&1 &"))
    look = next(i for i, line in enumerate(lines) if line.startswith("for F in "))
    refuse = next(i for i, line in enumerate(lines) if line.startswith('[ -n "$I" ] || '))
    first_copy = next(i for i, line in enumerate(lines) if line.startswith("/bin/cp "))
    assert lines[look - 1] == "I="
    assert first_cancel < watchdog_end < look < refuse < first_copy


def _no_state(place, tmp_path: Path, how: str) -> None:
    """Take the state `place` made away, `how`."""
    fpsd, shared = place["state"]
    (fpsd / "SC Info.sidb").unlink()
    if how == "neither-folder":
        fpsd.rmdir()
    elif how == "empty-folders":
        shared.mkdir(parents=True)
    elif how == "a-folder-by-that-name":
        (fpsd / "SC Info.sidb").mkdir()
    elif how == "a-link-to-one":
        (tmp_path / "elsewhere.sidb").write_bytes(b"sidb")
        (fpsd / "SC Info.sidb").symlink_to(tmp_path / "elsewhere.sidb")
    elif how == "other-names":
        (fpsd / "SC Info.sidf").write_bytes(b"x")
        shared.mkdir(parents=True)
        (shared / "SC Info.sidb.bak").write_bytes(b"x")
        (shared / "[Ss][Cc] [Ii][Nn][Ff][Oo].[Ss][Ii][Dd][Bb]").write_bytes(b"x")  # the pattern, as a name
    else:
        raise AssertionError(how)


NO_STATE_CASES = ["neither-folder", "empty-folders", "a-folder-by-that-name", "a-link-to-one", "other-names"]


@pytest.mark.parametrize("how", NO_STATE_CASES)
def test_without_the_state_root_stops_with_its_own_status_and_nothing_runs(place, tmp_path: Path, how: str) -> None:
    """A missing folder is no state, like an empty one, never a failure of
    its own under set -e. Only a regular file counts, never a link to one."""
    _no_state(place, tmp_path, how)

    run = _as_this_user(_command(place, make_ios_app(place["apps"])).script)

    assert run.returncode == enrollment.EXIT_NO_FAIRPLAY_STATE == 69, run.stderr
    assert run.stderr.strip() == NO_STATE
    assert run.stdout == ""
    assert place["fakes"].calls("unfairplay") == []
    assert list(place["work"].iterdir()) == [], "root's folder is gone"


def test_without_the_state_root_stops_before_it_copies_unfairplay(place, hang, tmp_path: Path) -> None:
    """Root's copy of an UnfairPlay that is a FIFO would block until the
    time limit: a run that ends at once never got to it."""
    _no_state(place, tmp_path, "neither-folder")
    command = enrollment.enroll_command(_fifo_helper(place), make_ios_app(place["apps"]), place["models"],
                                        home=place["home"])
    proc, _ = hang(command.script, wait_for_start=False)

    out, err = proc.communicate(timeout=20)

    assert proc.returncode == 69, err
    assert err.strip() == NO_STATE
    assert out == ""
    assert list(place["work"].iterdir()) == []


@pytest.mark.parametrize(
    ("where", "name"),
    [(0, "SC Info.sidb"), (1, "SC Info.sidb"), (0, "sc info.SIDB"), (1, "SC INFO.SIDB")],
    ids=["fpsd", "shared", "fpsd-any-case", "shared-any-case"],
)
def test_the_state_in_either_folder_in_any_case_lets_setup_run(place, where: int, name: str) -> None:
    """In the second folder, the first isn't there at all. Root matches
    each name as the folder lists it, so the case is root's to ignore, on a
    volume that ignores case or not."""
    fpsd = place["state"][0]
    (fpsd / "SC Info.sidb").unlink()
    if where == 1:
        fpsd.rmdir()
    place["state"][where].mkdir(parents=True, exist_ok=True)
    (place["state"][where] / name).write_bytes(b"sidb")

    run = _as_this_user(_command(place, make_ios_app(place["apps"])).script)

    assert run.returncode == 0, run.stderr
    assert len(place["fakes"].calls("unfairplay")) == 1


@pytest.mark.parametrize("state", ["there", "neither-folder"])
@pytest.mark.parametrize("shell", sorted(SHELLS))
def test_the_state_is_looked_for_alike_in_each_shell(place, tmp_path: Path, shell: str, state: str) -> None:
    """zsh, as sh, could make a pattern that matches nothing an error of its
    own (NOMATCH); in each shell it is no state, and the state is found."""
    if not Path(SHELLS[shell][0]).exists():
        pytest.skip(f"no {SHELLS[shell][0]}")
    if state != "there":
        _no_state(place, tmp_path, state)
    command = _command(place, make_ios_app(place["apps"]))

    run = subprocess.run([*SHELLS[shell], command.script], capture_output=True, text=True, timeout=60)

    if state == "there":
        assert run.returncode == 0, run.stderr
        shutil.rmtree(run.stdout.strip())
    else:
        assert run.returncode == 69, run.stderr
        assert run.stderr.strip() == NO_STATE
        assert place["fakes"].calls("unfairplay") == []
        assert list(place["work"].iterdir()) == []


# --- finish: the model moved, as the user ---------------------------------------


def _work(place, *files: tuple[str, bytes]) -> Path:
    work = Path(enrollment.WORK_TEMPLATE[:-8] + "AbCd1234")
    (work / "models").mkdir(parents=True, mode=0o700)
    for name, data in files:
        (work / "models" / name).write_bytes(data)
    return work


def test_finish_moves_the_model_and_removes_root_s_folder(place) -> None:
    work = _work(place, ("m.bin", b"model"))
    assert enrollment.finish(work, place["models"]) == ["m.bin"]
    model = place["models"] / "m.bin"
    assert model.read_bytes() == b"model"
    assert stat.S_IMODE(model.stat().st_mode) == 0o600
    assert not work.exists()


def test_after_the_script_finish_leaves_this_mac_enrolled(place) -> None:
    from graft_ipa import unfairplay

    command = _command(place, make_ios_app(place["apps"]))
    run = _as_this_user(command.script)
    enrollment.finish(Path(run.stdout.strip()), command.model_dir)
    assert unfairplay.model_state(command.model_dir) == unfairplay.ModelState(enrolled=True)
    assert list(place["work"].iterdir()) == []


def test_finish_refuses_a_symlink(place, tmp_path: Path) -> None:
    work = _work(place)
    (tmp_path / "target").write_bytes(b"not a model")
    (work / "models" / "m.bin").symlink_to(tmp_path / "target")
    with pytest.raises(AppStoreError, match="isn't a plain file") as e:
        enrollment.finish(work, place["models"])
    assert str(work) in e.value.hint, "it says where the leftover is"
    assert not place["models"].exists() or not list(place["models"].iterdir())


def test_finish_refuses_a_hard_linked_file(place, tmp_path: Path) -> None:
    work = _work(place, ("m.bin", b"model"))
    os.link(work / "models" / "m.bin", tmp_path / "second-name")
    with pytest.raises(AppStoreError, match="2 links"):
        enrollment.finish(work, place["models"])


def test_finish_refuses_something_that_isnt_a_regular_file(place) -> None:
    work = _work(place)
    os.mkfifo(work / "models" / "m.bin")
    with pytest.raises(AppStoreError, match="isn't a plain file"):
        enrollment.finish(work, place["models"])


def test_finish_refuses_a_work_folder_someone_else_owns(place) -> None:
    work = _work(place, ("m.bin", b"model"))
    with pytest.raises(AppStoreError, match="not you"):
        enrollment.finish(work, place["models"], uid=os.getuid() + 1)


def test_finish_refuses_a_file_someone_else_owns(place, monkeypatch: pytest.MonkeyPatch) -> None:
    """In a folder that is the user's, a file that isn't (only root could
    make one, so the stat is what's faked)."""
    work = _work(place, ("m.bin", b"model"))
    real = os.fstat

    def someone_elses(fd: int) -> os.stat_result:
        st = real(fd)
        fields = list(st[:10])
        fields[4] = os.getuid() + 1  # st_uid
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", someone_elses)
    with pytest.raises(AppStoreError, match="m.bin belongs to"):
        enrollment.finish(work, place["models"])


def test_a_save_that_fails_halfway_leaves_nothing_in_the_model_folder(
    place, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every file is staged before any is renamed in, so a failure on the
    second leaves no first."""
    from graft_ipa import data_dirs

    work = _work(place, ("a.bin", b"first"), ("b.bin", b"second"))
    real = data_dirs.private_stage
    calls = []

    def full_disk(data: bytes, dest: Path) -> Path:
        calls.append(dest)
        if len(calls) == 2:
            raise OSError(28, "No space left on device")
        return real(data, dest)

    monkeypatch.setattr(data_dirs, "private_stage", full_disk)
    with pytest.raises(AppStoreError, match="couldn't save the model") as e:
        enrollment.finish(work, place["models"])
    assert list(place["models"].iterdir()) == []
    assert str(work) in e.value.hint and work.exists()


def test_finish_writes_nothing_until_every_file_has_passed(place, tmp_path: Path) -> None:
    work = _work(place, ("a.bin", b"good"))
    (tmp_path / "target").write_bytes(b"x")
    (work / "models" / "b.bin").symlink_to(tmp_path / "target")
    with pytest.raises(AppStoreError):
        enrollment.finish(work, place["models"])
    assert not (place["models"] / "a.bin").exists()
    assert work.exists(), "the leftover stays where the hint says"


@pytest.mark.parametrize("name", ["/etc", "graft-enroll.AbCd1234", "/private/var/tmp/other.AbCd1234"])
def test_finish_takes_only_a_folder_the_script_made(place, name: str) -> None:
    with pytest.raises(AppStoreError, match="isn't a folder Graft's enrollment made"):
        enrollment.finish(Path(name), place["models"])


def test_finish_refuses_a_models_folder_that_is_a_link(place, tmp_path: Path) -> None:
    """Review #30: O_NOFOLLOW guards only a file's last part, so a
    work/models link would be followed to files elsewhere."""
    work = Path(enrollment.WORK_TEMPLATE[:-8] + "AbCd1234")
    work.mkdir(mode=0o700)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "m.bin").write_bytes(b"not root's model")
    (work / "models").symlink_to(elsewhere)
    with pytest.raises(AppStoreError, match="the enrollment's model folder is not a plain folder"):
        enrollment.finish(work, place["models"])
    assert not place["models"].exists() or list(place["models"].iterdir()) == []


def test_finish_refuses_a_models_folder_someone_else_owns(place, monkeypatch: pytest.MonkeyPatch) -> None:
    """In a work folder that is the user's, a models folder that isn't (only
    root could make one, so the lstat is what's faked, for that folder only)."""
    work = _work(place, ("m.bin", b"model"))
    real = os.lstat

    def someone_elses(path, *a, **kw):
        st = real(path, *a, **kw)
        if Path(path) != work / "models":
            return st
        fields = list(st[:10])
        fields[4] = os.getuid() + 1  # st_uid
        return os.stat_result(fields)

    monkeypatch.setattr(enrollment.os, "lstat", someone_elses)
    with pytest.raises(AppStoreError, match="the enrollment's model folder belongs to"):
        enrollment.finish(work, place["models"])
    assert not place["models"].exists() or list(place["models"].iterdir()) == []


def test_finish_refuses_a_work_folder_that_is_a_link(place, tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "models").mkdir(parents=True)
    (real / "models" / "m.bin").write_bytes(b"model")
    Path(enrollment.WORK_TEMPLATE[:-8] + "AbCd1234").symlink_to(real)
    with pytest.raises(AppStoreError, match="not a plain folder"):
        enrollment.finish(Path(enrollment.WORK_TEMPLATE[:-8] + "AbCd1234"), place["models"])


# --- The CLI ---------------------------------------------------------------------


def _config(temp_config_env: Path, fakes: FakeTools) -> None:
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["appstore"] = fakes.appstore_section()
    temp_config_env.write_text(yaml.safe_dump(raw, sort_keys=False))


def _events(result) -> list[dict]:
    return [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]


def _flat(text: str) -> str:
    """`text` with all whitespace gone: rich wraps stderr at the terminal's
    width, even in the middle of a path."""
    return "".join(text.split())


def _enroll(app: Path, *flags: str):
    return CliRunner().invoke(cli_mod.app, [*flags, "--appstore-enroll-command", "--app", str(app)])


def test_a_cli_that_isn_t_graft_app_s_refuses_an_unchecked_script_without_consent(
    temp_config_env: Path, place
) -> None:
    """Review #1: a pipx or Debug CLI runs an UnfairPlay nothing checks, so
    the script comes only with --allow-unverified-helper; without it, the
    refusal says why and which UnfairPlay, as data the app can ask about."""
    _config(temp_config_env, place["fakes"])
    app = make_ios_app(place["apps"])

    result = _enroll(app, "--json")

    assert result.exit_code == AppStoreError.exit_code, result.output
    up = str(place["fakes"].unfairplay)
    assert _events(result) == [{
        "type": "appstore-refused", "app": str(app.resolve()), "reason": "unverified-helper",
        "kind": "not-bundled", "helper": up,
        "message": f"The UnfairPlay root would run is {up}, not Graft.app's own copy, "
                   "and nothing checks its signature.",
    }]
    assert "set -eu" not in result.stdout, "no script without consent"
    assert place["fakes"].calls("unfairplay") == []


def test_the_refusal_exits_as_the_other_app_store_refusals_do() -> None:
    """`appstore-refused` is emitted just before an AppStoreError, whose exit
    status the get's refusals share (`appstore._decrypt`, `_needs_licence`)."""
    assert AppStoreError.exit_code == 20


def test_in_terminal_the_refusal_names_the_unfairplay_and_the_flag(temp_config_env: Path, place) -> None:
    _config(temp_config_env, place["fakes"])
    result = _enroll(make_ios_app(place["apps"]))
    assert result.exit_code == AppStoreError.exit_code
    said = _flat(result.stderr)
    assert _flat(str(place["fakes"].unfairplay)) in said and "--allow-unverified-helper" in said
    assert _flat("not Graft.app's own copy, and nothing checks its signature") in said
    assert result.stdout == "", "no script on stdout"


def test_with_consent_the_cli_emits_the_unchecked_script_and_why(temp_config_env: Path, place) -> None:
    _config(temp_config_env, place["fakes"])
    app = make_ios_app(place["apps"])

    result = _enroll(app, "--json", "--allow-unverified-helper")

    assert result.exit_code == 0, result.output
    (event,) = _events(result)
    assert set(event) == {
        "type", "app", "model_dir", "script", "verified", "helper", "unverified_kind", "unverified_reason",
        "cancel_file", "time_limit_seconds",
    }
    assert event["time_limit_seconds"] == enrollment.TIME_LIMIT_SECONDS == 900
    cancel = Path(event["cancel_file"])
    assert cancel.parent == place["user_tmp"] and cancel.name.startswith("graft-enroll-cancel.")
    assert not cancel.exists(), "the CLI never makes the cancel file; the app does, to cancel"
    assert f"cancelled() {{ [ -e '{cancel}' ]; }}" in event["script"]
    assert event["type"] == "appstore-enroll-command"
    assert event["app"] == str(app.resolve())
    assert event["model_dir"] == str(place["fakes"].models)
    assert event["verified"] is False, "a configured UnfairPlay isn't Graft.app's: nothing vouches for it"
    assert event["helper"] == str(place["fakes"].unfairplay)
    assert event["unverified_kind"] == "not-bundled"
    assert event["unverified_reason"].startswith(f"The UnfairPlay root would run is {place['fakes'].unfairplay},")
    assert f"/bin/cp '{place['fakes'].unfairplay}' \"$D/unfairplay\"" in event["script"]
    assert place["fakes"].calls("unfairplay") == [], "the CLI builds the script and never runs it"


def _bundled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, team, *,
             developer_id: bool | BaseException = False) -> tuple[FakeTools, list[Path]]:
    """A fake Graft.app this CLI runs from, whose signature names `team`, and
    whose certificate is Developer ID or not (`developer_id`)."""
    from graft_ipa import tools

    bundled = FakeTools(tmp_path / "Graft.app" / "Contents" / "Helpers" / "bin")
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: bundled.root)
    asked: list[Path] = []

    def app_team(app: Path):
        asked.append(app)
        if isinstance(team, BaseException):
            raise team
        return team

    def app_is_developer_id(app: Path):
        asked.append(("developer id", app))
        if isinstance(developer_id, BaseException):
            raise developer_id
        return developer_id

    monkeypatch.setattr(enrollment, "app_team", app_team)
    monkeypatch.setattr(enrollment, "app_is_developer_id", app_is_developer_id)
    return bundled, asked


def test_graft_app_s_own_unfairplay_is_checked_against_its_team(
    temp_config_env: Path, place, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundled, asked = _bundled(tmp_path, monkeypatch, TEAM)
    app = make_ios_app(place["apps"])

    result = _enroll(app, "--json")

    assert result.exit_code == 0, result.output
    (event,) = _events(result)
    assert event["verified"] is True
    assert event["helper"] == str(bundled.unfairplay)
    assert event["unverified_kind"] is None and event["unverified_reason"] is None
    assert asked == [tmp_path / "Graft.app", ("developer id", tmp_path / "Graft.app")]
    assert ("/usr/bin/codesign --verify --strict -R '=anchor apple generic and identifier \"unfairplay\" "
            "and certificate leaf[subject.OU] = \"TESTTEAM01\"' \"$D/unfairplay\"") in event["script"]


def test_a_bundled_cli_never_runs_a_config_yaml_unfairplay_as_root(
    temp_config_env: Path, place, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #1: an explicit appstore.unfairplay used to beat Graft.app's
    own copy, and same-user code that can write config.yaml got root through
    Graft's own prompt. In Graft.app the root step uses Graft.app's helper
    whatever config.yaml says (decrypt still honours the setting)."""
    _config(temp_config_env, place["fakes"])  # appstore.unfairplay: the user's own build
    bundled, _ = _bundled(tmp_path, monkeypatch, TEAM)

    result = _enroll(make_ios_app(place["apps"]), "--json")

    assert result.exit_code == 0, result.output
    (event,) = _events(result)
    assert str(place["fakes"].unfairplay) not in event["script"]
    assert str(place["fakes"].root) not in event["script"]
    assert f"/bin/cp '{bundled.unfairplay}' \"$D/unfairplay\"" in event["script"]
    assert event["helper"] == str(bundled.unfairplay) and event["verified"] is True


def test_an_ad_hoc_graft_app_s_script_needs_consent_and_still_uses_its_own_helper(
    temp_config_env: Path, place, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(temp_config_env, place["fakes"])
    bundled, _ = _bundled(tmp_path, monkeypatch, enrollment.AD_HOC)
    app = make_ios_app(place["apps"])

    refused = _enroll(app, "--json")
    assert refused.exit_code == AppStoreError.exit_code
    assert _events(refused) == [{
        "type": "appstore-refused", "app": str(app.resolve()), "reason": "unverified-helper",
        "kind": "ad-hoc", "helper": str(bundled.unfairplay),
        "message": "This copy of Graft has no developer-team signature, so root can't check "
                   "UnfairPlay's signature before running it.",
    }]

    allowed = _enroll(app, "--json", "--allow-unverified-helper")
    assert allowed.exit_code == 0, allowed.output
    (event,) = _events(allowed)
    assert event["unverified_kind"] == "ad-hoc" and event["verified"] is False
    assert "codesign" not in event["script"]
    assert str(place["fakes"].unfairplay) not in event["script"]
    assert f"/bin/cp '{bundled.unfairplay}' \"$D/unfairplay\"" in event["script"]


def test_a_failed_team_lookup_in_graft_app_is_an_error_with_no_script(
    temp_config_env: Path, place, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #28: not an unverified script, and not a refusal the app could
    answer with consent; even --allow-unverified-helper doesn't get one."""
    _bundled(tmp_path, monkeypatch, AppStoreError("couldn't read Graft.app's signature: boom"))
    for flags in (["--json"], ["--json", "--allow-unverified-helper"]):
        result = _enroll(make_ios_app(place["apps"], f"Example{len(flags)}"), *flags)
        assert result.exit_code == AppStoreError.exit_code
        assert _events(result) == []
        assert _flat("couldn't read Graft.app's signature") in _flat(result.stderr)


def test_a_bundled_cli_whose_helper_is_missing_says_graft_app_is_damaged(
    temp_config_env: Path, place, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(temp_config_env, place["fakes"])  # a configured one exists, and is not used
    bundled, asked = _bundled(tmp_path, monkeypatch, TEAM)
    bundled.unfairplay.unlink()

    result = _enroll(make_ios_app(place["apps"]), "--json", "--allow-unverified-helper")

    assert result.exit_code == AppStoreError.exit_code
    said = _flat(result.stderr)
    assert _flat(f"Graft.app is damaged: {bundled.unfairplay} is missing") in said
    assert "DownloadGraftagain." in said
    assert _events(result) == [] and asked == []


def test_the_cli_finishes_with_the_folder_the_script_printed(temp_config_env: Path, place) -> None:
    _config(temp_config_env, place["fakes"])
    work = _work(place, ("m.bin", b"model"))
    result = CliRunner().invoke(cli_mod.app, ["--json", "--appstore-enroll-finish", str(work)])
    assert result.exit_code == 0, result.output
    assert [json.loads(line) for line in result.stdout.splitlines()] == [
        {"type": "appstore-enrolled", "model_dir": str(place["fakes"].models), "count": 1}
    ]
    assert (place["fakes"].models / "m.bin").read_bytes() == b"model"


@pytest.mark.parametrize("columns", ["20", "300"])
def test_in_terminal_stdout_is_the_script_byte_for_byte(
    temp_config_env: Path, place, monkeypatch: pytest.MonkeyPatch, columns: str
) -> None:
    """The critic: the script went out through rich, which wrapped it at the
    terminal's width (an 81-character /bin/cp line became two commands) and
    turned `:x:` in an app name into an emoji, with the prose (backticks and
    all) on the same stdout, so `> s.sh; sudo /bin/sh s.sh` ran the prose as
    root. stdout is now the script and nothing else; the prose is stderr's."""
    _config(temp_config_env, place["fakes"])
    app = make_ios_app(place["apps"], "AC:x:DC Long Enough To Wrap Somewhere")
    monkeypatch.setattr(enrollment, "cancel_file_path", lambda: place["user_tmp"] / "graft-enroll-cancel.fixed")
    monkeypatch.setenv("COLUMNS", columns)
    # rich reads COLUMNS when a console is made: fresh ones, as a new
    # process at that width would have.
    monkeypatch.setattr(cli_mod, "console", cli_mod._PipeAwareConsole())
    monkeypatch.setattr(cli_mod, "err_console", cli_mod._PipeAwareConsole(stderr=True))
    (event,) = _events(_enroll(app, "--json", "--allow-unverified-helper"))

    result = _enroll(app, "--allow-unverified-helper")

    assert result.exit_code == 0, result.output
    assert result.stdout_bytes == event["script"].encode()
    assert "AC:x:DC Long Enough To Wrap Somewhere" in result.stdout
    said = _flat(result.stderr)
    assert _flat("sudo /bin/sh enroll.sh") in said and "--appstore-enroll-finish" in said
    assert _flat("not Graft.app's own copy, and nothing checks its signature") in said
    saved = place["cwd"] / "s.sh"
    saved.write_bytes(result.stdout_bytes)
    assert subprocess.run(["/bin/sh", "-n", str(saved)]).returncode == 0


def test_the_cli_needs_an_app(temp_config_env: Path) -> None:
    result = CliRunner().invoke(cli_mod.app, ["--json", "--appstore-enroll-command"])
    assert result.exit_code != 0
    assert "--appstore-enroll-command requires --app" in " ".join(result.output.split())


# --- review 2.0, bundle #10: a Developer ID Graft.app wants a Developer ID UnfairPlay -------------

DEVELOPER_ID_MARKERS = ("certificate 1[field.1.2.840.113635.100.6.2.6] exists and "
                        "certificate leaf[field.1.2.840.113635.100.6.1.13] exists")


def test_a_developer_id_build_s_requirement_adds_apple_s_developer_id_markers() -> None:
    """Without them, an Apple Development certificate on the same team also
    satisfied root's check of its UnfairPlay copy."""
    plain = enrollment.requirement(TEAM)
    assert enrollment.requirement(TEAM, developer_id=True) == f"{plain} and {DEVELOPER_ID_MARKERS}"
    assert enrollment.requirement(TEAM, developer_id=False) == plain and DEVELOPER_ID_MARKERS not in plain
    assert enrollment.requirement(None, developer_id=True) is None


def _der(oids: list[str]) -> bytes:
    """A certificate carrying an extension for each of `oids`, as DER."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    builder = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
               .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1)))
    for oid in oids:
        builder = builder.add_extension(x509.UnrecognizedExtension(x509.ObjectIdentifier(oid), b"\x05\x00"),
                                        critical=False)
    return builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.DER)


LEAF, CA = "1.2.840.113635.100.6.1.13", "1.2.840.113635.100.6.2.6"
APPLE_DEVELOPMENT = "1.2.840.113635.100.6.1.12"


def _codesign_extracts(monkeypatch: pytest.MonkeyPatch, certs: list[bytes], rc: int = 0) -> list[list[str]]:
    seen: list[list[str]] = []

    def codesign(argv, **kw):
        seen.append(list(argv))
        prefix = next(a.split("=", 1)[1] for a in argv if a.startswith("--extract-certificates="))
        for i, der in enumerate(certs):
            Path(f"{prefix}{i}").write_bytes(der)
        return subprocess.CompletedProcess(argv, rc, "", "" if rc == 0 else "codesign: failed")

    monkeypatch.setattr(enrollment.subprocess, "run", codesign)
    return seen


@pytest.mark.parametrize("leaf, ca, developer_id", [
    ([LEAF], [CA], True),
    ([APPLE_DEVELOPMENT], [CA], False),     # an Apple Development leaf
    ([LEAF], [], False),                    # the marker, but not issued by the Developer ID CA
    ([], [], False),
])
def test_graft_app_s_own_certificate_says_whether_it_is_developer_id(monkeypatch, tmp_path, leaf, ca, developer_id):
    seen = _codesign_extracts(monkeypatch, [_der(leaf), _der(ca), _der([])])
    assert enrollment.app_is_developer_id(tmp_path / "Graft.app") is developer_id
    (argv,) = seen
    assert argv[:2] == ["/usr/bin/codesign", "-d"] and argv[-1] == str(tmp_path / "Graft.app")
    assert argv[2].startswith("--extract-certificates=") and len(argv) == 4


@pytest.mark.parametrize("certs, rc", [([], 0), ([b"not a certificate"], 0), ([_der([LEAF]), _der([CA])], 1)],
                         ids=["none-written", "unreadable", "codesign-failed"])
def test_a_certificate_read_that_fails_is_an_error_never_not_developer_id(monkeypatch, tmp_path, certs, rc):
    """"Not Developer ID" would loosen root's check; a failure must not."""
    _codesign_extracts(monkeypatch, certs, rc)
    with pytest.raises(AppStoreError):
        enrollment.app_is_developer_id(tmp_path / "Graft.app")


def test_the_real_codesign_hands_over_an_apple_signed_binary_s_certificates() -> None:
    """Read-only, on an Apple platform binary: its chain is Apple's own
    software signing, not Developer ID."""
    assert enrollment.app_is_developer_id(Path("/bin/ls")) is False


def test_a_developer_id_graft_app_s_script_checks_its_copy_with_the_markers(
    temp_config_env: Path, place, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bundled(tmp_path, monkeypatch, TEAM, developer_id=True)
    result = _enroll(make_ios_app(place["apps"]), "--json")
    assert result.exit_code == 0, result.output
    (event,) = _events(result)
    assert enrollment.quote("=" + enrollment.requirement(TEAM, developer_id=True)) in event["script"]


def test_a_failed_certificate_read_in_graft_app_is_an_error_with_no_script(
    temp_config_env: Path, place, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bundled(tmp_path, monkeypatch, TEAM, developer_id=AppStoreError("couldn't read Graft.app's certificates"))
    result = _enroll(make_ios_app(place["apps"]), "--json")
    assert result.exit_code == AppStoreError.exit_code and _events(result) == []


def test_only_a_team_checked_helper_can_be_developer_id() -> None:
    with pytest.raises(ValueError):
        enrollment.RootHelper(Path("/x/unfairplay"), unverified_kind="ad-hoc", developer_id=True)
    assert enrollment.RootHelper(Path("/x/unfairplay"), team=TEAM, developer_id=True).developer_id
