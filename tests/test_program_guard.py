"""The suite-wide tripwire for programs no test may start
(`tests/program_guard.py`).

Every program actually asked for here is `security help`, which prints its
usage and touches no keychain, or `osascript -e 'return 1'`, which prints 1
and shows nothing. So even with the guard broken, these tests raise no
dialog and change nothing; they only fail. `sudo`, `su`, `authopen` and
`login` are checked by name only (`offender`), never started.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from .program_guard import ForbiddenProgram, offender

SECURITY_HELP = ["/usr/bin/security", "help"]
OSASCRIPT_ONE = ["/usr/bin/osascript", "-e", "return 1"]


@pytest.mark.parametrize(
    ("argv", "kw", "hit"),
    [
        (SECURITY_HELP, {}, "/usr/bin/security"),
        (["security", "help"], {}, "security"),
        ("/usr/bin/security help", {"shell": True}, "/usr/bin/security"),
        (["/bin/sh", "-c", "true && security help"], {}, "security"),
        (["/bin/zsh", "-lc", "echo; security help"], {}, "security"),
        (["/usr/bin/env", "-i", "A=1", "security", "help"], {}, "security"),
        ("FOO=1 osascript -e 'return 1'", {"shell": True}, "osascript"),
        (["anything"], {"executable": "/usr/bin/sudo"}, "/usr/bin/sudo"),
        (["sudo", "-n", "true"], {}, "sudo"),
        (["/usr/bin/su", "-l"], {}, "/usr/bin/su"),
        (["/usr/libexec/authopen", "-r", "/x"], {}, "/usr/libexec/authopen"),
        (["/usr/bin/login", "-f"], {}, "/usr/bin/login"),
        (["/usr/bin/xcrun", "security", "help"], {}, "security"),
    ],
)
def test_each_way_of_naming_a_forbidden_program_is_seen(argv, kw, hit) -> None:
    assert offender(argv, **kw) == hit


@pytest.mark.parametrize(
    "argv",
    [["/usr/bin/true"], ["grep", "-r", "--", "su", "."], ["/usr/bin/plutil", "-p", "login"],
     ["/usr/bin/codesign", "-s", "-", "--force", "x.app"], ["/usr/bin/codesign", "--sign=-", "x"],
     ["/usr/bin/codesign", "--verify", "--deep", "x.app"], ["/usr/bin/xcode-select", "-p"]],
)
def test_arguments_that_only_name_one_are_not_programs(argv) -> None:
    assert offender(argv) is None


@pytest.mark.parametrize(
    "argv",
    [["/usr/bin/open", "-R", "x.ipa"], ["open", "-b", "io.playcover.PlayCover", "x.ipa"],
     ["/usr/bin/pbcopy"], ["pbpaste"],
     ["/usr/bin/codesign", "-s", "ABCDE12345ABCDE12345ABCDE12345ABCDE12345", "x.app"],
     ["/usr/bin/codesign", "--force", "--sign", "Apple Development: Someone", "x"],
     ["/usr/bin/codesign", "--sign=0123456789ABCDEF", "x"],
     ["/usr/bin/xcode-select", "--install"],
     ["/Applications/Graft.app/Contents/Helpers/bin/ipatool", "auth", "info"],
     ["/opt/homebrew/bin/unfairplay", "--version"]],
)
def test_the_programs_that_act_for_real_are_refused(argv) -> None:
    assert offender(argv) is not None


def test_a_fake_ipatool_under_the_temp_folder_runs(tmp_path) -> None:
    fake = tmp_path / "ipatool"
    fake.write_text("#!/bin/sh\necho fake\n")
    fake.chmod(0o755)
    assert offender([str(fake), "--version"]) is None
    assert subprocess.run([str(fake)], capture_output=True, text=True, timeout=30).stdout == "fake\n"
    # By name, on the PATH the child is given.
    assert offender(["ipatool", "--version"], path=str(tmp_path)) is None


def test_a_real_ipatool_by_name_is_looked_up_on_the_childs_path(monkeypatch) -> None:
    """By name, it is found on the PATH the child gets, and refused when
    that copy isn't a fake. Only looked up: nothing is started."""
    import shutil

    asked: list[str | None] = []

    def which(name, path=None):
        asked.append(path)
        return "/opt/homebrew/bin/ipatool"

    monkeypatch.setattr(shutil, "which", which)
    assert offender(["ipatool", "--version"], path="/opt/homebrew/bin") is not None
    assert asked and set(asked) == {"/opt/homebrew/bin"}


def test_subprocess_refuses_security_and_osascript(forbidden_programs) -> None:
    for argv in (SECURITY_HELP, ["security", "help"], OSASCRIPT_ONE):
        with pytest.raises(ForbiddenProgram):
            subprocess.run(argv, capture_output=True, timeout=30)
    with pytest.raises(ForbiddenProgram):
        subprocess.run("security help", shell=True, capture_output=True, timeout=30)
    with pytest.raises(ForbiddenProgram):
        subprocess.check_output(["/usr/bin/env", "security", "help"], timeout=30)
    assert len(forbidden_programs.attempts) == 5
    forbidden_programs.clear()


def test_posix_spawn_and_system_refuse_too(forbidden_programs) -> None:
    with pytest.raises(ForbiddenProgram):
        os.posix_spawn("/usr/bin/security", ["security", "help"], dict(os.environ))
    with pytest.raises(ForbiddenProgram):
        os.posix_spawnp("osascript", ["osascript", "-e", "return 1"], dict(os.environ))
    with pytest.raises(ForbiddenProgram):
        os.system("security help >/dev/null")
    assert len(forbidden_programs.attempts) == 3
    forbidden_programs.clear()


def test_a_refusal_is_recorded_even_when_swallowed(forbidden_programs) -> None:
    """Code under test that treats any OSError as "not there" still fails
    its test: the teardown reads the record."""
    try:
        subprocess.run(SECURITY_HELP, capture_output=True, timeout=30)
    except OSError:
        pass
    assert forbidden_programs.attempts, "recorded for the teardown to fail on"
    forbidden_programs.clear()


def test_everything_else_still_runs() -> None:
    assert subprocess.run(["/usr/bin/true"], timeout=30).returncode == 0
    assert subprocess.run("echo ok | grep -q ok", shell=True, timeout=30).returncode == 0


def _child(code: str) -> subprocess.CompletedProcess:
    """A Python child started the suite's normal way: a plain Popen, which
    `children_offline` hands tests/child_site."""
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize(
    "code",
    [
        "import subprocess; subprocess.run(['/usr/bin/security', 'help'])",
        "import subprocess; subprocess.run(['osascript', '-e', 'return 1'])",
        "import os; os.execv('/usr/bin/security', ['security', 'help'])",
        "import os; os.system('security help')",
    ],
)
def test_a_child_python_refuses_them_and_its_test_is_told(forbidden_programs, code: str) -> None:
    proc = _child(code)
    assert proc.returncode != 0
    assert "ForbiddenProgram" in proc.stderr
    assert "security" in forbidden_programs.children() or "osascript" in forbidden_programs.children()
    forbidden_programs.clear()


def test_a_child_with_its_own_environment_is_guarded_too(forbidden_programs, tmp_path) -> None:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    proc = subprocess.run(
        [sys.executable, "-c", "import subprocess; subprocess.run(['/usr/bin/security', 'help'])"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert "ForbiddenProgram" in proc.stderr
    assert forbidden_programs.children()
    forbidden_programs.clear()


def test_graft_s_own_switch_off_in_a_child_still_reaches_no_security(forbidden_programs) -> None:
    """GRAFT_NO_KEYCHAIN lives in graft_ipa/keychain.py, the code under test.
    With it unset in a child, keychain's own runner goes to subprocess, and
    the guard stops it there. Only `security help`, even then."""
    env = {k: v for k, v in os.environ.items() if k != "GRAFT_NO_KEYCHAIN"}
    code = "from graft_ipa import keychain\nkeychain._run_security(['security', 'help'])\n"
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                          text=True, timeout=60)
    assert "ForbiddenProgram" in proc.stderr
    assert "security" in forbidden_programs.children()
    forbidden_programs.clear()


@pytest.mark.parametrize("where", ["in the test", "in a child"])
def test_a_test_that_swallows_the_refusal_is_reported(tmp_path, where: str) -> None:
    """The teardown check, seen from outside: a test whose code catches the
    refusal and carries on still ends in an error naming the program."""
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    (tmp_path / "conftest.py").write_text(
        "from tests.conftest import forbidden_programs, children_offline  # noqa: F401\n"
    )
    call = (
        "subprocess.run(['/usr/bin/security', 'help'], capture_output=True)" if where == "in the test"
        else "subprocess.run([sys.executable, '-c', \"import subprocess; "
             "subprocess.run(['/usr/bin/security', 'help'])\"], capture_output=True)"
    )
    (tmp_path / "test_quiet_caller.py").write_text(
        "import subprocess, sys\n"
        "def test_not_there_is_fine():\n"
        "    try:\n"
        f"        {call}\n"
        "    except OSError:\n"
        "        pass\n"
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", str(tmp_path)],
        cwd=tmp_path, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(repo)},
    )
    assert proc.returncode != 0, proc.stdout
    assert "tried to start a program the suite never runs" in proc.stdout
    assert "security" in proc.stdout
    assert "1 error" in proc.stdout


def test_a_guard_installed_twice_in_one_process_still_refuses(tmp_path) -> None:
    """A child of the suite that imports the suite's conftest (an inner
    pytest) has the child site's guard and the conftest's, one inside the
    other. The outer one must still read subprocess's own arguments."""
    from pathlib import Path

    from . import program_guard

    seen: list[str] = []
    program_guard.install(seen.append)  # a second layer, on this process's
    try:
        with pytest.raises(ForbiddenProgram):
            subprocess.run(SECURITY_HELP, capture_output=True, timeout=30)
        assert seen, "the outer layer refused it, not only the inner"
    finally:
        _unwrap_once()
    from .conftest import PROGRAM_ATTEMPTS

    PROGRAM_ATTEMPTS.clear()
    assert Path(__file__).exists()


def _unwrap_once() -> None:
    import os as _os

    _execute = subprocess.Popen._execute_child
    subprocess.Popen._execute_child = _execute.__wrapped__
    for name in ("posix_spawn", "posix_spawnp", "system", "execv", "execve", "execl", "execle",
                 "execlp", "execlpe", "execvp", "execvpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
                 "spawnl", "spawnle", "spawnlp", "spawnlpe"):
        fn = getattr(_os, name, None)
        if fn is not None and hasattr(fn, "__wrapped__"):
            setattr(_os, name, fn.__wrapped__)
    import pty

    if hasattr(pty.spawn, "__wrapped__"):
        pty.spawn = pty.spawn.__wrapped__
    import multiprocessing.util

    if hasattr(multiprocessing.util.spawnv_passfds, "__wrapped__"):
        multiprocessing.util.spawnv_passfds = multiprocessing.util.spawnv_passfds.__wrapped__


# --- The fix re-check's problem 4: what the guard used to miss -----------------
#
# Each of the reviewer's evasions, as `offender` sees it, and the harmless
# ones actually started: `security help`, `osascript -e 'return 1'`, or a
# stub named `security` on a scratch PATH that only leaves a mark. The
# guard's refusal is what must stop them.

_EVASIONS = [
    (["/bin/sh", "-c", "true\nsecurity help"], "a second line"),
    (["/bin/sh", "-c", "set -eu\n/usr/bin/osascript -e 'return 1'"], "a later line of a script"),
    (["/bin/sh", "-c", "if true; then security help; fi"], "after then"),
    (["/bin/sh", "-c", "{ security help; }"], "after {"),
    (["/bin/sh", "-c", "! security help"], "after !"),
    (["/bin/sh", "-c", "for i in 1; do security help; done"], "after do"),
    (["/bin/sh", "-c", "eval security help"], "after eval"),
    (["/bin/sh", "-c", "exec security help"], "after exec"),
    (["/bin/sh", "-c", "x=$(security help)"], "in a substitution"),
    (["/bin/sh", "-c", "echo `security help`"], "in backquotes"),
    (["/bin/sh", "-c", "sec''urity help"], "quoted apart"),
    (["/bin/sh", "-c", "security>/dev/null help"], "a redirection on it"),
    (["/bin/bash", "--norc", "-c", "security help"], "a long option before -c"),
    (["/bin/zsh", "-o", "errexit", "-c", "security help"], "an option's argument before -c"),
    (["/bin/sh", "-ec", "true; security help"], "a cluster with c"),
    (["/usr/bin/env", "-u", "FOO", "security", "help"], "env -u NAME"),
    (["/usr/bin/env", "-uFOO", "security", "help"], "env -uNAME"),
    (["/usr/bin/env", "-P", "/usr/bin", "security", "help"], "env -P dir"),
    (["/usr/bin/env", "-S", "security help"], "env -S"),
    (["/usr/bin/env", "-i", "A=1", "-S", "B=2 security help"], "env -S with its own words"),
    (["/usr/bin/env", "--weird", "5", "security", "help"], "an option env's table doesn't know"),
    (["/usr/bin/nice", "-n", "5", "security", "help"], "nice -n 5"),
    (["/usr/bin/nice", "-n5", "security", "help"], "nice -n5"),
    (["/usr/bin/nohup", "security", "help"], "nohup"),
    (["/usr/bin/time", "-o", "/dev/null", "security", "help"], "time -o file"),
    (["/usr/bin/xcrun", "--sdk", "macosx", "security", "help"], "xcrun --sdk macosx"),
    (["/usr/bin/xcrun", "-r", "security", "help"], "xcrun -r"),
    (["/usr/bin/arch", "-arch", "arm64", "security", "help"], "arch -arch"),
    (["command", "-p", "security", "help"], "command -p"),
    (["/usr/bin/caffeinate", "-i", "security", "help"], "caffeinate"),
    (["/usr/bin/SECURITY", "help"], "another letter case"),
    (["/usr/bin/Sudo", "-n", "true"], "Sudo"),
    (["/usr/bin/OSAScript", "-e", "return 1"], "OSAScript"),
    (["Security", "help"], "by name, in another case"),
    (["/usr/bin/codesign", "-fs", "Apple Development: Someone (ABCDE12345)", "x.app"], "codesign -fs"),
    (["/usr/bin/codesign", "-sf", "x.app"], "codesign -sf (identity f)"),
    (["/usr/bin/codesign", "-vfs", "ABCDE12345ABCDE12345", "x"], "a longer cluster"),
]


@pytest.mark.parametrize(("argv", "how"), _EVASIONS, ids=[how for _, how in _EVASIONS])
def test_every_evasion_the_review_found_is_refused(argv, how) -> None:
    assert offender(argv) is not None, how


@pytest.mark.parametrize(
    "line",
    ["true\nsecurity help", "if true; then security help; fi", "x=1; { osascript -e 'return 1'; }"],
)
def test_a_shell_line_s_every_command_is_seen_through_shell_true(line: str) -> None:
    assert offender(line, shell=True) is not None


def test_shell_text_is_read_conservatively() -> None:
    """Every word of a shell line counts as a program: an argument that only
    names one is refused there too, unlike in a plain argv (`grep -r su .`)."""
    assert offender(["/bin/sh", "-c", "echo login | grep su"]) is not None
    assert offender(["/usr/bin/grep", "-r", "su", "."]) is None


def test_ad_hoc_signing_in_a_cluster_still_runs() -> None:
    assert offender(["/usr/bin/codesign", "-fs", "-", "x.app"]) is None
    assert offender(["/usr/bin/codesign", "--force", "--sign=-", "x.app"]) is None


def test_a_link_to_a_forbidden_program_is_refused(tmp_path) -> None:
    link = tmp_path / "harmless-name"
    link.symlink_to("/usr/bin/security")
    assert offender([str(link), "help"]) is not None
    assert offender(["harmless-name", "help"], path=str(tmp_path)) is not None


def test_every_line_of_the_root_script_is_seen(tmp_path, monkeypatch) -> None:
    """The b3 tests run the generated root script through `/bin/sh -c`: a
    forbidden program added on any of its lines, at any depth (the
    watchdog's body, `leave`), is seen. Never started: only looked at."""
    from graft_ipa import enrollment
    from tests.appstore_fakes import FakeTools, make_ios_app

    apps = (tmp_path / "Applications").resolve()
    apps.mkdir()
    monkeypatch.setattr(enrollment, "TRUSTED_ROOT", apps)
    monkeypatch.setattr(enrollment, "TRUSTED_OWNER", os.getuid())
    fakes = FakeTools(tmp_path / "tools")
    helper = enrollment.RootHelper(fakes.unfairplay, unverified_kind=enrollment.UNVERIFIED_NOT_BUNDLED)
    script = enrollment.enroll_command(helper, make_ios_app(apps), tmp_path / "models",
                                       cancel_file=tmp_path / "cancel", home=tmp_path / "home").script
    lines = script.splitlines()
    assert offender(["/bin/sh", "-c", script]) is None, "the script as built starts nothing forbidden"
    for i in range(len(lines) + 1):
        for added in ("/usr/bin/osascript -e 'return 1'", "  security help", "x && /usr/bin/sudo -n true"):
            mutated = "\n".join([*lines[:i], added, *lines[i:]])
            assert offender(["/bin/sh", "-c", mutated]) is not None, (i, added)


def _stub(tmp_path):
    """A `security` that only leaves a mark, on a PATH of its own."""
    from pathlib import Path

    stub = Path(tmp_path) / "stub-bin"
    stub.mkdir()
    mark = Path(tmp_path) / "stub-ran"
    script = stub / "security"
    script.write_text(f"#!/bin/sh\n: > '{mark}'\n")
    script.chmod(0o755)
    (stub / "sec").symlink_to(script)
    return stub, mark


@pytest.mark.parametrize(
    "argv",
    [
        ["/bin/sh", "-c", "true\nsecurity x"],
        ["/bin/sh", "-c", "if true; then security x; fi"],
        ["/bin/sh", "-c", "{ security x; }"],
        ["/usr/bin/env", "-u", "FOO", "security", "x"],
        ["/usr/bin/nice", "-n", "5", "security", "x"],
        ["{stub}/SECURITY", "x"],
        ["{stub}/sec", "x"],
        ["sec", "x"],
    ],
)
def test_the_evasions_started_for_real_are_stopped(forbidden_programs, tmp_path, argv) -> None:
    stub, mark = _stub(tmp_path)
    argv = [a.replace("{stub}", str(stub)) for a in argv]
    with pytest.raises(ForbiddenProgram):
        subprocess.run(argv, env={"PATH": str(stub), "HOME": str(tmp_path)}, capture_output=True, timeout=30)
    assert not mark.exists(), "the stub never ran"
    assert forbidden_programs.attempts
    forbidden_programs.clear()


def test_os_system_with_a_second_line_is_stopped(forbidden_programs, tmp_path, monkeypatch) -> None:
    stub, mark = _stub(tmp_path)
    monkeypatch.setenv("PATH", str(stub))
    with pytest.raises(ForbiddenProgram):
        os.system("true\nsecurity x")
    assert not mark.exists()
    forbidden_programs.clear()


@pytest.mark.parametrize("how", ["spawnve", "spawnv", "spawnl", "spawnvpe", "pty"])
def test_spawn_and_pty_spawn_fail_the_test_in_this_process(forbidden_programs, tmp_path, how) -> None:
    """They fork and then exec: refused before the fork, so the refusal is
    this process's, raised here and recorded for this test."""
    import pty

    stub, mark = _stub(tmp_path)
    target = str(stub / "security")
    env = {"PATH": str(stub)}
    with pytest.raises(ForbiddenProgram):
        if how == "spawnve":
            os.spawnve(os.P_WAIT, target, ["security", "x"], env)
        elif how == "spawnv":
            os.spawnv(os.P_WAIT, target, ["security", "x"])
        elif how == "spawnl":
            os.spawnl(os.P_WAIT, target, "security", "x")
        elif how == "spawnvpe":
            os.spawnvpe(os.P_WAIT, "security", ["security", "x"], env)
        else:
            pty.spawn([target, "x"])
    assert not mark.exists()
    assert len(forbidden_programs.attempts) == 1, "recorded in this process"
    forbidden_programs.clear()


def test_a_refusal_in_a_forked_child_reaches_the_test(forbidden_programs, tmp_path) -> None:
    """os.fork, then exec in the child: the child's own record goes with its
    memory, so it writes to the test's child log as well."""
    stub, mark = _stub(tmp_path)
    pid = os.fork()
    if pid == 0:  # the child
        try:
            os.execv(str(stub / "security"), ["security", "x"])
        except BaseException:
            pass
        os._exit(0)
    os.waitpid(pid, 0)
    assert not mark.exists()
    assert "security" in forbidden_programs.children()
    forbidden_programs.clear()


# --- The owner's opt-in real-keychain tests may run `security`, and only it --------


def test_security_is_let_through_only_with_both_the_opt_in_and_the_test_s_marker(monkeypatch) -> None:
    from . import program_guard

    try:
        monkeypatch.setenv(program_guard.REAL_KEYCHAIN_ENV, "1")
        assert offender(SECURITY_HELP) is not None, "the env var alone isn't enough"
        program_guard.allow_security(True)
        assert offender(SECURITY_HELP) is None
        assert offender(["/bin/sh", "-c", "security help"]) is None
        assert offender(OSASCRIPT_ONE) is not None, "security, and nothing else"
        assert offender(["/usr/bin/sudo", "-n", "true"]) is not None
        monkeypatch.delenv(program_guard.REAL_KEYCHAIN_ENV)
        assert offender(SECURITY_HELP) is not None, "the marker alone isn't enough either"
    finally:
        program_guard.allow_security(False)


def test_only_the_marked_test_gets_security_with_the_opt_in_set(tmp_path) -> None:
    """The suite run with GRAFT_REAL_KEYCHAIN_TESTS=1: a test marked
    `real_keychain` may start `security` (here only asked about, never
    started), in its own process and in its children; an ordinary test with
    the variable set is refused as ever."""
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    (tmp_path / "conftest.py").write_text(
        "from tests.conftest import forbidden_programs, children_offline, pytest_configure  # noqa: F401\n"
    )
    (tmp_path / "test_opt_in.py").write_text(
        "import subprocess, sys\n"
        "import pytest\n"
        "from tests.program_guard import offender\n"
        "CHILD = \"from tests.program_guard import offender; print(offender(['/usr/bin/security', 'help']))\"\n"
        "def child():\n"
        "    return subprocess.run([sys.executable, '-c', CHILD], capture_output=True, text=True,\n"
        "                          timeout=60).stdout.strip()\n"
        "@pytest.mark.real_keychain\n"
        "def test_marked():\n"
        "    assert offender(['/usr/bin/security', 'help']) is None\n"
        "    assert offender(['/usr/bin/osascript', '-e', 'return 1']) is not None\n"
        "    assert child() == 'None'\n"
        "def test_ordinary():\n"
        "    assert offender(['/usr/bin/security', 'help']) is not None\n"
        "    assert child() == '/usr/bin/security'\n"
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", str(tmp_path)],
        cwd=tmp_path, capture_output=True, text=True, timeout=180,
        env={**os.environ, "PYTHONPATH": str(repo), "GRAFT_REAL_KEYCHAIN_TESTS": "1"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "2 passed" in proc.stdout


# --- The delta audit's #9 to #14: what the guard still missed ---------------------
#
# Each one as `offender` sees it, then the harmless forms started for real
# against the stub `security` (or a link to it): the refusal must stop them.

_DELTA_EVASIONS = [
    # 9: the program that runs is `executable=`, not argv[0].
    (["x", "-c", "security help"], {"executable": "/bin/sh"}, "executable= a shell"),
    (["x", "sudo", "-n", "true"], {"executable": "/usr/bin/env"}, "executable= env"),
    # 10: env's split option at the end of a cluster or attached to it, its
    # lone '-', and xcrun's mode flags before another option.
    (["/usr/bin/env", "-iS", "security help"], {}, "env -iS"),
    (["/usr/bin/env", "-vSsecurity help"], {}, "env -vS, attached"),
    (["/usr/bin/env", "-0S", "security help"], {}, "env -0S"),
    (["/usr/bin/env", "-", "security", "help"], {}, "env -"),
    (["/usr/bin/nice", "/usr/bin/env", "-vS", "osascript -e 1"], {}, "nice env -vS"),
    (["/usr/bin/xcrun", "-r", "--sdk", "macosx", "security", "help"], {}, "xcrun -r --sdk"),
    (["/usr/bin/xcrun", "--run", "-v", "security", "help"], {}, "xcrun --run -v"),
    # 11: shell quoting the shell takes out before it runs the name.
    (["/bin/sh", "-c", "secu\\\nrity help"], {}, "backslash-newline"),
    ("secu\\\nrity help", {"shell": True}, "backslash-newline, shell=True"),
    (["/bin/bash", "-c", "$'su' -"], {}, "$'..'"),
    (["/bin/bash", "-c", '$"security" help'], {}, '$".."'),
    (["/bin/bash", "-c", "$'\\163ecurity' help"], {}, "$'..' with an octal escape"),
    (["/bin/bash", "-c", "$'\\x73ecurity' help"], {}, "$'..' with a hex escape"),
    (["/bin/bash", "-c", "{security,help}"], {}, "brace expansion"),
    (["/bin/bash", "-c", "secur{i,x}ty help"], {}, "brace expansion inside a word"),
    ("/usr/bin/secur?ty help", {"shell": True}, "a ? glob on the path"),
    (["/bin/sh", "-c", "s[e]curity help"], {}, "a [] glob"),
    (["/bin/sh", "-c", "/usr/bin/osascrip* -e 1"], {}, "a * glob"),
    # 12: a -c string that starts with '-' or '+', after '--'.
    (["/bin/sh", "-c", "--", "-x 2>/dev/null; security help"], {}, "-c -- '-x; ...'"),
    (["/bin/bash", "-c", "--", "+x; sudo true"], {}, "-c -- '+x; ...'"),
    (["/bin/sh", "-c", "-", "-x\nsecurity help"], {}, "-c - '-x<newline>...'"),
    (["/bin/bash", "-c", "--", "-x`security`"], {}, "-c -- '-x`...`'"),
    (["/bin/bash", "-c", "--", "-x$(security)"], {}, "-c -- '-x$(...)'"),
]


@pytest.mark.parametrize(("argv", "kw", "how"), _DELTA_EVASIONS, ids=[how for _, _, how in _DELTA_EVASIONS])
def test_every_evasion_the_delta_audit_found_is_refused(argv, kw, how) -> None:
    assert offender(argv, **kw) is not None, how


@pytest.mark.parametrize(
    ("argv", "kw"),
    [(["/bin/sh", "-c", "ls *.txt; echo a,b"], {}), (["/usr/bin/env", "-i", "PATH=/usr/bin", "true"], {}),
     (["/usr/bin/xcrun", "--find", "clang"], {}), ("echo {a,b} $'x' && true", {"shell": True}),
     (["x", "-c", "true"], {"executable": "/bin/sh"}), (["/bin/sh", "-c", "--", "-x; true"], {}),
     (["/bin/sh", "-c", "cat \\\nfile"], {})],
)
def test_the_new_readings_still_let_ordinary_commands_run(argv, kw) -> None:
    assert offender(argv, **kw) is None


def _links(tmp_path):
    """The stub `security`, and a folder of links: `y` to the stub, and
    `runner` and `wrap`, innocently named links to /bin/sh and /usr/bin/env."""
    from pathlib import Path

    stub, mark = _stub(tmp_path)
    links = Path(tmp_path) / "links"
    links.mkdir()
    (links / "y").symlink_to(stub / "security")
    (links / "runner").symlink_to("/bin/sh")
    (links / "wrap").symlink_to("/usr/bin/env")
    return stub, mark, links


def test_a_link_is_found_on_the_path_and_in_the_folder_the_child_uses(tmp_path) -> None:
    """#13: a link was looked up on the parent's PATH and cwd only, and a
    shell or wrapper was known by argv[0]'s name only."""
    _, _, links = _links(tmp_path)
    for argv, kw in [
        (["/usr/bin/env", f"PATH={links}", "y", "x"], {}),
        (["/usr/bin/env", "-P", str(links), "y", "x"], {}),
        (["/bin/sh", "-c", f"PATH={links} y x"], {}),
        (["/bin/sh", "-c", f"export PATH={links}; y x"], {}),
        (["./y", "x"], {"cwd": str(links)}),
        ([str(links / "runner"), "-c", "security x"], {}),
        ([str(links / "wrap"), "security", "x"], {}),
    ]:
        assert offender(argv, **kw) is not None, argv


@pytest.mark.parametrize(
    "how",
    ["executable-sh", "executable-env", "env-vS", "env-dash", "backslash-newline", "ansi-c", "braces",
     "dash-dash", "env-PATH", "env-P", "sh-PATH", "cwd", "runner"],
)
def test_the_delta_evasions_started_for_real_are_stopped(forbidden_programs, tmp_path, how) -> None:
    stub, mark, links = _links(tmp_path)
    env = {"PATH": str(stub), "HOME": str(tmp_path)}
    argv, kw = {
        "executable-sh": (["x", "-c", "security x"], {"executable": "/bin/sh"}),
        "executable-env": (["x", "security", "x"], {"executable": "/usr/bin/env"}),
        "env-vS": (["/usr/bin/env", "-P", str(stub), "-vS", "security x"], {}),
        "env-dash": (["/usr/bin/env", "-P", str(stub), "-", "security", "x"], {}),
        "backslash-newline": (["/bin/sh", "-c", "secu\\\nrity x"], {}),
        "ansi-c": (["/bin/bash", "-c", "$'secu''rity' x"], {}),
        "braces": (["/bin/bash", "-c", "{security,x}"], {}),
        "dash-dash": (["/bin/sh", "-c", "--", "-x 2>/dev/null; security x"], {}),
        "env-PATH": (["/usr/bin/env", f"PATH={links}", "y", "x"], {}),
        "env-P": (["/usr/bin/env", "-P", str(links), "y", "x"], {}),
        "sh-PATH": (["/bin/sh", "-c", f"PATH={links} y x"], {}),
        "cwd": (["./y", "x"], {"cwd": str(links)}),
        "runner": ([str(links / "runner"), "-c", "security x"], {}),
    }[how]
    with pytest.raises(ForbiddenProgram):
        subprocess.run(argv, env=env, capture_output=True, timeout=30, **kw)
    assert not mark.exists(), "the stub never ran"
    assert forbidden_programs.attempts
    forbidden_programs.clear()


@pytest.mark.parametrize("how", ["posix_spawn", "spawnv", "execv"])
def test_the_file_a_spawn_or_exec_runs_is_unwrapped_too(forbidden_programs, tmp_path, monkeypatch, how) -> None:
    """#9: os.posix_spawn, os.spawn* and os.exec* run their file argument,
    whatever argv[0] says; a shell there has its -c text read."""
    stub, mark, _ = _links(tmp_path)
    monkeypatch.setenv("PATH", str(stub))
    argv = ["x", "-c", "security x"]
    if how == "execv":
        pid = os.fork()
        if pid == 0:  # the child
            try:
                os.execv("/bin/sh", argv)
            except BaseException:
                pass
            os._exit(0)
        os.waitpid(pid, 0)
        assert "security" in forbidden_programs.children()
    else:
        with pytest.raises(ForbiddenProgram):
            if how == "posix_spawn":
                os.posix_spawn("/bin/sh", argv, {"PATH": str(stub)})
            else:
                os.spawnv(os.P_WAIT, "/bin/sh", argv)
    assert not mark.exists()
    forbidden_programs.clear()


_CHILD_CODE = "import subprocess; subprocess.run(['security', 'x'])"


@pytest.mark.parametrize("how", ["posix_spawn", "system", "spawnv", "multiprocessing"])
def test_a_python_child_started_other_than_by_popen_is_guarded_too(forbidden_programs, tmp_path, monkeypatch,
                                                                     how) -> None:
    """#14: only Popen handed a child tests/child_site, so a Python child
    started through os.posix_spawn, os.system, os.spawn* or multiprocessing
    had no guard of its own. The stub `security` on its PATH must not run."""
    stub, mark, _ = _links(tmp_path)
    monkeypatch.setenv("PATH", f"{stub}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    if how == "posix_spawn":
        pid = os.posix_spawn(sys.executable, [sys.executable, "-c", _CHILD_CODE], {"PATH": str(stub)})
        os.waitpid(pid, 0)
    elif how == "system":
        os.system(f"'{sys.executable}' -c \"{_CHILD_CODE}\" 2>/dev/null")
    elif how == "spawnv":
        os.spawnv(os.P_WAIT, sys.executable, [sys.executable, "-c", _CHILD_CODE])
    else:
        # How multiprocessing's spawn and forkserver start methods start
        # their Python: with this process's environment, never through Popen.
        import multiprocessing.util

        pid = multiprocessing.util.spawnv_passfds(os.fsencode(sys.executable),
                                                  [sys.executable, "-c", _CHILD_CODE], ())
        os.waitpid(pid, 0)
    assert not mark.exists(), "the stub never ran: the child refused it"
    assert "security" in forbidden_programs.children()
    forbidden_programs.clear()
