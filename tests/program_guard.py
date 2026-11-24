"""No test runs a program that can show a dialog or act with privilege.

The suite's other guards for these live inside the code under test:
`keychain._run` is swapped for FakeSecurity, and a child's `security` is
switched off by GRAFT_NO_KEYCHAIN, which `graft_ipa/keychain.py` itself
checks. A mutant, or a new code path, that goes around either reaches the
real `/usr/bin/security`, and a real admin dialog has already appeared on the
owner's screen that way (from a Swift mutant). This guard owes nothing to
`graft_ipa`: it refuses to start any of `FORBIDDEN` before the process
exists, however it is named:

- by absolute path or by name, in any letter case (this Mac's APFS is
  case-insensitive: `/usr/bin/SECURITY` runs `security`), and through a link
  or a copy's name that resolves to one, looked up on the PATH the child
  gets, a PATH a wrapper or the shell text sets (`env PATH=dir`, `env -P
  dir`, `PATH=dir x`, `export PATH=dir`), and in the child's own folder
  (Popen's `cwd=`); a link to a shell or a wrapper is unwrapped as one;
- as `executable=`, or the file os.posix_spawn, os.exec* or os.spawn* runs,
  whatever argv[0] says, unwrapped like argv[0] would be;
- through a program that runs another (`env`, `nice`, `nohup`, `time`,
  `command`, `exec`, `xcrun`, `sudo`, `arch`, `caffeinate`, `timeout`,
  `xargs`, `taskpolicy`), past their options and the options' arguments
  (`env -u NAME`, `env -P dir`, `env -S "..."`, also clustered or attached:
  `env -iS "..."`, `env -vS"..."`; env's lone `-`; `nice -n 5`, `xcrun -r
  --sdk macosx`); after an option it doesn't know, every word that follows
  counts as a possible program;
- inside a shell's command text (`sh`/`bash`/`zsh`/... with `-c`, options
  before it or not, the text after `--` even when it starts with `-` or
  `+`, `shell=True`, `os.system`), conservatively: every shell word on
  every line, after any keyword (`if`, `then`, `{`, `!`, `do`, `eval`,
  `exec`, ...), split on whitespace and `;&|(){}<>=` and backquotes, with
  line continuations joined, `$'...'` escapes decoded, brace expansions
  written out, and quotes and backslashes taken out, is checked as a
  program; a glob is checked against the names and paths it could match
  (`/usr/bin/secur?ty`, `s[e]curity`).

What a static look can't see, and so isn't claimed: a name built when the
shell runs (`x=secur; ${x}ity`, `$(printf sec)urity`), a glob that matches a
link of another name in some folder, a PATH or folder changed in a way only
running it shows (`cd dir && ./x`, `env -C dir`, a PATH set in a sourced
file), a script run from a file (`sh script.sh`) or fed on stdin, and a
program run by a program that isn't Python (a compiled tool's own `exec`).
Python children are guarded too, through `tests/child_site`, put on the
PYTHONPATH of every child however it is started (Popen, os.posix_spawn,
os.system, os.exec*, os.spawn*, pty.spawn, multiprocessing), unless the
child is started with `-I`, `-E` or `-S`, which skip it; a child that sets
its own PYTHONPATH without it, or that isn't Python, isn't.

Wrapped: `subprocess.Popen._execute_child` (so `run`, `check_output`,
`os.popen` and asyncio's subprocesses), `os.posix_spawn`, `os.posix_spawnp`,
`os.system`, the `os.exec*` family, the `os.spawn*` family, `pty.spawn` and
`multiprocessing.util.spawnv_passfds`. os.spawn* and pty.spawn fork and then
exec, so they are checked before the fork, in the process whose test the
refusal must fail; a refusal in any forked child is also written to the
test's child log (`CHILD_LOG_ENV`), so it is never lost with the child's
memory.

Besides the fixed list, three rules about how a program is run: `codesign`
signing with an identity other than ad hoc `-` (it reads a private key from
a keychain, which can ask), clustered flags included (`-fs <identity>`,
`-sf`); `xcode-select --install` (macOS's install dialog); and an `ipatool`
or `unfairplay` that isn't a test's fake under the temp folder (a real one
acts on a real Apple ID or this Mac's enrollment). In this process for the
whole session (`install`), and in every child the suite starts
(`install_in_child`, from `tests/child_site`).

One exception, never by default: the opt-in real-keychain tests (marked
`real_keychain`, run only with `GRAFT_REAL_KEYCHAIN_TESTS=1`, the owner
watching) may start `security`, and nothing else on the list. Both must hold
(`real_keychain_allowed`): conftest turns it on for such a test alone
(`allow_security`), and for its children through `ALLOW_SECURITY_ENV`.

A refusal raises `ForbiddenProgram` (a PermissionError, so the code under
test takes its "couldn't run it" path) and is recorded, so the test fails at
teardown even if that code swallows the error. The guard's own tests make
refusals on purpose, only ever of `security help` and `osascript -e 'return
1'`, which touch no keychain and raise no dialog even if the guard is
broken, or of a harmless stub on a scratch PATH.
"""

from __future__ import annotations

import codecs
import contextlib
import fnmatch
import os
import re
import shlex
import warnings
from collections.abc import Callable, Iterable, Mapping
from typing import Any

#: The programs, by basename, compared case-folded: Keychain access
#: (`security`), AppleScript and its admin prompt (`osascript`), privilege
#: (`sudo`, `su`, `authopen`), a login (`login`), Launch Services (`open`,
#: which reveals in Finder or starts an app such as PlayCover), and the
#: general pasteboard (`pbcopy`, `pbpaste`).
FORBIDDEN = frozenset({
    "security", "osascript", "sudo", "su", "authopen", "login", "open", "pbcopy", "pbpaste",
})
#: The same, where macOS keeps them.
FORBIDDEN_PATHS = frozenset({
    "/usr/bin/security", "/usr/bin/osascript", "/usr/bin/sudo", "/usr/bin/su",
    "/usr/libexec/authopen", "/usr/bin/login", "/usr/bin/open", "/usr/bin/pbcopy",
    "/usr/bin/pbpaste",
})
#: Programs that hold a real account or run as root, refused unless the
#: copy is a test's own fake, under the temp folder: a real ipatool signs in
#: to Apple with the login keychain's sign-in, a real UnfairPlay decrypts
#: with this Mac's enrollment.
REAL_TOOLS = frozenset({"ipatool", "unfairplay"})
#: The file a child records its refusals in (`install_in_child`).
CHILD_LOG_ENV = "GRAFT_TEST_PROGRAM_LOG"
#: The owner's opt-in for the real-keychain tests ...
REAL_KEYCHAIN_ENV = "GRAFT_REAL_KEYCHAIN_TESTS"
#: ... the marker such a test carries ...
REAL_KEYCHAIN_MARKER = "real_keychain"
#: ... and what conftest sets for that test's children alone.
ALLOW_SECURITY_ENV = "GRAFT_TEST_ALLOW_SECURITY"

#: tests/child_site, whose sitecustomize guards a Python child
#: (`install_in_child`): put on the PYTHONPATH of every child this process
#: starts, whichever way (delta audit #14).
CHILD_SITE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "child_site")

_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish"})


class _Wrapper:
    """How a program that runs another reads its own options: those that
    take an argument (`takes`), those whose argument is the program itself
    (`program`), those whose argument is a command line it splits (`split`),
    whether VAR=value words may come first, and how many operands come
    before the program (`timeout DURATION cmd`)."""

    def __init__(self, takes: Iterable[str] = (), *, program: Iterable[str] = (), split: Iterable[str] = (),
                 flags: Iterable[str] = (), assignments: bool = False, operands: int = 0,
                 dash_is_option: bool = False, path_option: str | None = None) -> None:
        self.program = frozenset(program)
        self.split = frozenset(split)
        self.takes = frozenset(takes) | self.program | self.split
        # Options known to take no argument, so they don't make every word
        # after them a possible program.
        self.flags = frozenset(flags)
        self.assignments = assignments
        self.operands = operands
        # A lone `-` is an option (env's: the same as -i), not the program.
        self.dash_is_option = dash_is_option
        # The option whose argument is the PATH the program is found on.
        self.path_option = path_option


#: Programs that run the program named after them (macOS's, and GNU's long
#: options where the two differ).
_WRAPPERS: dict[str, _Wrapper] = {
    "env": _Wrapper({"-u", "-P", "-L", "-U", "-C", "--unset", "--chdir"},
                    split={"-S", "--split-string"}, flags={"-i", "-v", "-0", "--ignore-environment", "--null"},
                    assignments=True, dash_is_option=True, path_option="-P"),
    "nice": _Wrapper({"-n", "--adjustment"}),
    "nohup": _Wrapper(),
    "time": _Wrapper({"-o", "-f", "--output", "--format"}),
    "command": _Wrapper(),
    "exec": _Wrapper({"-a"}),
    # -r/--run and -f/--find are modes, not options with an argument: the
    # tool is the first word that isn't an option (delta audit #10).
    "xcrun": _Wrapper({"-sdk", "--sdk", "-toolchain", "--toolchain"},
                      flags={"-r", "--run", "-f", "--find", "-v", "--verbose", "-l", "--log", "-n",
                             "--no-cache", "-k", "--kill-cache"}),
    "sudo": _Wrapper({"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T", "--user", "--group",
                      "--close-from", "--chdir", "--host", "--prompt", "--role", "--type",
                      "--other-user", "--command-timeout"}),
    "arch": _Wrapper({"-arch", "-d", "-e"}),
    "caffeinate": _Wrapper({"-t", "-w"}),
    "timeout": _Wrapper({"-s", "-k", "--signal", "--kill-after"}, operands=1),
    "xargs": _Wrapper({"-E", "-I", "-J", "-L", "-n", "-P", "-R", "-S", "-s"}),
    "taskpolicy": _Wrapper({"-c", "-d", "-g", "-t", "-l", "-p"}),
}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
#: Where shell text is split into commands, and then into words.
_SHELL_COMMANDS = re.compile(r"[;&|(){}\n]+")
_SHELL_WORDS = re.compile(r"[\s<>=`]+")
_SHELL_QUOTING = re.compile(r"['\"\\]")

#: Whether `security` alone may run now (`allow_security`).
_SECURITY_ALLOWED = False


class ForbiddenProgram(PermissionError):
    """A test tried to start a program the suite never runs."""


def _text(word: Any) -> str:
    if isinstance(word, bytes):
        return os.fsdecode(word)
    return os.fspath(word) if isinstance(word, os.PathLike) else str(word)


def _base(word: str) -> str:
    return os.path.basename(word).casefold()


def real_keychain_allowed() -> bool:
    """`security` is let through only for an opt-in real-keychain test: the
    owner's `GRAFT_REAL_KEYCHAIN_TESTS=1` and the test's own permission
    (conftest's, in this process; `ALLOW_SECURITY_ENV`, in its children)."""
    if os.environ.get(REAL_KEYCHAIN_ENV) != "1":
        return False
    return _SECURITY_ALLOWED or os.environ.get(ALLOW_SECURITY_ENV) == "1"


def allow_security(allowed: bool) -> None:
    """conftest's switch, on for an opt-in real-keychain test alone."""
    global _SECURITY_ALLOWED
    _SECURITY_ALLOWED = bool(allowed)


def _shell_line(words: list[str]) -> list[str]:
    """The command lines a shell's argv carries: with `-c` (alone or in a
    cluster, `-ec`, `-lc`, after other options or not), every operand, since
    options that take an argument (`-o errexit`, `--rcfile f`) make the
    command string's place uncertain; and a word that starts with `-` or `+`
    but holds shell text no option could (blanks, `;&|<>()`, a backquote or
    `$`), which after `--` or a lone `-` is the command string (`sh -c --
    '-x; ...'`, delta audit #12). A word like `-x` is skipped: as a command
    it could only run a program named `-x`. Without `-c`, none (a script
    file, or stdin, isn't read)."""
    rest = words[1:]
    if not any(w.startswith("-") and not w.startswith("--") and "c" in w[1:] for w in rest):
        return []
    return [w for w in rest if not w.startswith(("-", "+")) or re.search(r"[\s;&|<>()`$]", w)]


def _kind(program: str, path: str | None) -> str:
    """A program's name for what it is: its own, or, when that is no shell
    or wrapper, the name of the file it runs (a link named `runner` to
    /bin/sh is a shell: delta audit #13)."""
    base = _base(program)
    if base in _SHELLS or base in _WRAPPERS:
        return base
    found = _found(program, path)
    return _base(os.path.realpath(found)) if found else base


def _commands(words: list[str], path: str | None = None):
    """(program, its arguments, the PATH it is found on) for each program an
    argv could run: its first word, through any wrapper (with the PATH the
    wrapper gives it: `env PATH=dir`, `env -P dir`), and each command of a
    shell's `-c` text (`_shell_commands`)."""
    if not words:
        return
    program = words[0]
    yield program, words[1:], path
    base = _kind(program, path)
    if base in _SHELLS:
        for line in _shell_line(words):
            yield from _shell_commands(line, path)
        return
    wrapper = _WRAPPERS.get(base)
    if wrapper is None:
        return
    i, operands = 1, wrapper.operands
    while i < len(words):
        word = words[i]
        if word == "--":
            i += 1
            break
        if wrapper.assignments and _ASSIGNMENT.match(word):
            if word.startswith("PATH="):
                path = word[len("PATH="):]
            i += 1
            continue
        if word == "-" and wrapper.dash_is_option:
            i += 1
            continue
        if not word.startswith("-") or word == "-":
            if operands:
                operands -= 1
                i += 1
                continue
            break
        if word in wrapper.flags:
            i += 1
            continue
        name, attached = (word.split("=", 1) + [None])[:2] if word.startswith("--") else (word, None)
        if name in wrapper.takes:
            arg = attached if attached is not None else (words[i + 1] if i + 1 < len(words) else None)
            i += 1 if attached is not None else 2
            if arg is not None and name == wrapper.path_option:
                path = arg
            if arg is not None and name in wrapper.program:
                yield from _commands([arg, *words[i:]], path)
            if arg is not None and name in wrapper.split:
                yield from _commands([program, *_split(arg), *words[i:]], path)
                return
            continue
        if not word.startswith("--") and len(word) > 2:
            # A cluster: flags, then perhaps one letter that takes an
            # argument, which is the rest of the word (`-uNAME`, `-n5`,
            # `-vSsecurity x`) or else the next word (`-iu NAME`, `-iS 'x'`).
            letters = [f"-{c}" for c in word[1:]]
            k = next((k for k, letter in enumerate(letters) if letter in wrapper.takes), None)
            unknown = any(letter not in wrapper.flags for letter in letters[:k])
            if k is not None:
                attached = word[k + 2:]
                arg = attached or (words[i + 1] if i + 1 < len(words) else None)
                if unknown:
                    yield from _every_word_after(words, i)
                i += 1 if attached else 2
                letter = letters[k]
                if arg is not None and letter == wrapper.path_option:
                    path = arg
                if arg is not None and letter in wrapper.program:
                    yield from _commands([arg, *words[i:]], path)
                if arg is not None and letter in wrapper.split:
                    yield from _commands([program, *_split(arg), *words[i:]], path)
                    return
                continue
            if not unknown:
                i += 1
                continue
        # An option this table doesn't know: it may take the next word, so
        # every word from here is a possible program.
        yield from _every_word_after(words, i, path)
        i += 1
    yield from _commands(words[i:], path)


def _every_word_after(words: list[str], i: int, path: str | None = None):
    for j in range(i + 1, len(words)):
        yield words[j], words[j + 1:], path
        if _base(words[j]) in _SHELLS:
            for line in _shell_line(words[j:]):
                yield from _shell_commands(line, path)


def _split(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


#: `$'...'`, whose backslash escapes the shell decodes, and `$"..."`.
_ANSI_C = re.compile(r"\$'((?:[^'\\]|\\.)*)'", re.S)
#: A word with a brace expansion in it (`{a,b}`, `x{a,b}y`); not a `{ ...; }`
#: group, which has blanks in it.
_BRACED = re.compile(r"[^\s;&|(){}<>`'\"]*\{[^{}\s]*,[^{}\s]*\}[^\s;&|()<>`'\"]*")


def _decode_ansi_c(match: re.Match) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return "'" + codecs.decode(match[1], "unicode_escape") + "'"
        except (UnicodeDecodeError, ValueError):
            return "'" + match[1] + "'"


def _braces(word: str) -> list[str]:
    m = re.search(r"\{([^{}]*,[^{}]*)\}", word)
    if not m:
        return [word]
    return [w for alt in m[1].split(",") for w in _braces(word[:m.start()] + alt + word[m.end():])][:64]


def _shell_text(text: str) -> str:
    """Shell text as the shell reads its words (delta audit #11): line
    continuations joined, `$'...'` decoded and `$"..."` taken as `"..."`,
    brace expansions written out; then quotes and backslashes taken out."""
    text = text.replace("\\\n", "")
    text = _ANSI_C.sub(_decode_ansi_c, text)
    text = re.sub(r"\$(?=\")", "", text)
    text = _BRACED.sub(lambda m: " ".join(_braces(m[0])), text)
    return _SHELL_QUOTING.sub("", text)


def _shell_commands(text: str, path: str | None = None):
    """Every word of a shell command line, on every line, as a possible
    program, with the rest of its command as its arguments, found on the
    PATH the child has and on each PATH the text sets (`PATH=dir x`,
    `export PATH=dir`: delta audit #13). Conservative on purpose: `echo
    security` is refused too."""
    text = _shell_text(text)
    paths = [path, *dict.fromkeys(os.path.expandvars(p) for p in re.findall(r"\bPATH=([^\s;&|()<>]+)", text))]
    for command in _SHELL_COMMANDS.split(text):
        words = [w for w in _SHELL_WORDS.split(command) if w]
        for i, word in enumerate(words):
            for p in paths:
                yield word, words[i + 1:], p


def _temp_roots() -> tuple[str, ...]:
    import tempfile

    roots = {tempfile.gettempdir(), "/private/var/folders", "/var/folders", "/private/tmp", "/tmp"}
    return tuple(os.path.realpath(r) for r in roots) + tuple(roots)


def _found(program: str, path: str | None, cwd: str | None = None) -> str | None:
    """Where `program` is: itself when it is a path (a relative one taken
    from `cwd`, the child's folder, when there is one), else looked up on
    `path` (the child's PATH), else this process's. Never one with an
    expansion the shell would make (`$D/x`): that isn't a path yet."""
    if "$" in program:
        program = os.path.expandvars(program)
        if "$" in program:
            return None
    if os.sep in program:
        return os.path.join(cwd, program) if cwd and not os.path.isabs(program) else program
    import shutil

    return shutil.which(program, path=path)


def _a_real_tool(program: str, path: str | None, cwd: str | None = None) -> bool:
    """`ipatool` or `unfairplay` that isn't a test's fake under the temp
    folder. By name only, it is looked up on `path` (the child's PATH), else
    this process's."""
    if _base(program) not in REAL_TOOLS:
        return False
    found = _found(program, path, cwd)
    if found is None:
        return False  # nothing to run, or nothing a static look can place
    real = os.path.realpath(found)
    return not any(real == r or real.startswith(r.rstrip(os.sep) + os.sep) for r in _temp_roots())


def _signs_with_an_identity(program: str, args: list[str]) -> bool:
    """`codesign` signing with a real identity, which reads its private key
    from a keychain and can ask for it. Ad hoc (`-s -`) touches no keychain.
    A cluster with `s` in it (`-fs X`, `-sf`) signs too: the identity is the
    rest of the cluster, or else the next word."""
    if _base(program) != "codesign":
        return False
    for i, arg in enumerate(args):
        if arg in ("-s", "--sign"):
            identity = args[i + 1] if i + 1 < len(args) else None
        elif arg.startswith("--sign="):
            identity = arg.split("=", 1)[1]
        elif arg.startswith("-") and not arg.startswith("--") and "s" in arg[1:]:
            after = arg[arg.index("s", 1) + 1:]
            identity = after or (args[i + 1] if i + 1 < len(args) else None)
        else:
            continue
        if identity != "-":
            return True
    return False


def _glob_names(program: str) -> set[str]:
    """Which of `FORBIDDEN` a glob could stand for (delta audit #11): a
    path whose every part matches one of FORBIDDEN_PATHS' (`/usr/bin/secur?ty`),
    or a last part with three or more letters of its own that matches a
    name (`s[e]curity`, `osascrip*`). A bare `*` matches no name."""
    if not re.search(r"[*?\[]", program):
        return set()
    pattern = program.casefold()
    names = {_base(p) for p in FORBIDDEN_PATHS
             if os.sep in pattern and len(p.split(os.sep)) == len(pattern.split(os.sep))
             and all(fnmatch.fnmatchcase(a, b) for a, b in zip(p.split(os.sep), pattern.split(os.sep), strict=True))}
    last = os.path.basename(pattern)
    if len(re.sub(r"\[[^\]]*\]|[*?]", "", last)) >= 3:
        names |= {n for n in FORBIDDEN if fnmatch.fnmatchcase(n, last)}
    return names


def _forbidden_names(program: str, path: str | None, cwd: str | None = None) -> set[str]:
    """Which of `FORBIDDEN` `program` is: by its own name, by a glob it is,
    and, when it can be placed, by the real file it runs (a link, a copy's
    name). Case-folded."""
    names = {_base(program)} if _base(program) in FORBIDDEN or program.casefold() in FORBIDDEN_PATHS else set()
    names |= _glob_names(program)
    found = _found(program, path, cwd)
    if found is not None:
        real = os.path.realpath(found)
        if _base(real) in FORBIDDEN or real.casefold() in FORBIDDEN_PATHS:
            names.add(_base(real))
    return {_base(n) for n in names}


def _refused(program: str, args: list[str], path: str | None, cwd: str | None = None) -> str | None:
    if forbidden := _forbidden_names(program, path, cwd):
        if forbidden == {"security"} and real_keychain_allowed():
            return None
        return program
    if _a_real_tool(program, path, cwd):
        return f"{program} (a real one, not a test's fake)"
    if _signs_with_an_identity(program, args):
        return f"{program} signing with an identity"
    if _base(program) == "xcode-select" and "--install" in args:
        return f"{program} --install"
    return None


def offender(
    argv: Iterable[Any] | str | bytes, *, executable: Any = None, shell: bool = False,
    path: str | None = None, cwd: Any = None,
) -> str | None:
    """The forbidden program this would start, or None.

    Looks at the program, the program behind a wrapper (`env`, `nice`,
    `xcrun`...), and every word of a shell command line (`shell=True`, `sh -c
    "..."`, `os.system`). With `executable=`, that is the program that runs,
    with argv's other words, and it is unwrapped the same way whatever argv[0]
    says (delta audit #9). `path` is the child's PATH and `cwd` its folder.
    Arguments to a program that isn't a shell or a wrapper (`grep -r su .`)
    are not programs; they count only for the rules about them (`codesign -s
    <identity>`, `xcode-select --install`).
    """
    commands: list[tuple[str, list[str], str | None]] = []
    if isinstance(argv, (str, bytes, os.PathLike)):
        line = _text(argv)
        commands += list(_shell_commands(line, path)) if shell else [(line, [], path)]
        words = [line]
    else:
        words = [_text(w) for w in argv]
        if shell and words:
            commands += list(_shell_commands(words[0], path))
        else:
            commands += list(_commands(words, path))
    if executable is not None:
        exe = _text(executable)
        commands[:0] = [(exe, [], path)] if shell else list(_commands([exe, *words[1:]], path))
    folder = _text(cwd) if cwd is not None else None
    return next((hit for program, args, where in commands if (hit := _refused(program, args, where, folder))), None)


def _with_child_site(env: Any) -> Any:
    """`env` with CHILD_SITE on its PYTHONPATH (and the test's child log), so
    a Python child started with it is guarded too; as it was when it isn't a
    mapping of strings, or has it already."""
    if not isinstance(env, Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        return env
    entries = [e for e in env.get("PYTHONPATH", "").split(os.pathsep) if e]
    log = os.environ.get(CHILD_LOG_ENV)
    if CHILD_SITE in entries and (not log or CHILD_LOG_ENV in env):
        return env
    env = dict(env)
    if CHILD_SITE not in entries:
        env["PYTHONPATH"] = os.pathsep.join([*entries, CHILD_SITE])
    if log and CHILD_LOG_ENV not in env:
        env[CHILD_LOG_ENV] = log
    return env


@contextlib.contextmanager
def _child_site_in_environ():
    """This process's own environment with CHILD_SITE on its PYTHONPATH, for
    a child started from it with no environment of its own (os.system,
    multiprocessing's spawn): put back afterward."""
    before = os.environ.get("PYTHONPATH")
    after = _with_child_site({"PYTHONPATH": before or ""})["PYTHONPATH"]
    os.environ["PYTHONPATH"] = after
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = before


def _guard(setattr_: Callable[..., Any], record: Callable[[str], None]) -> None:
    import functools
    import inspect
    import subprocess

    installed_in = os.getpid()

    def refuse(what: str, how: str) -> ForbiddenProgram:
        record(f"{how}: {what}")
        log = os.environ.get(CHILD_LOG_ENV)
        if os.getpid() != installed_in and log:
            # A forked child (os.fork, multiprocessing): its own record goes
            # with its memory, so the test's log gets it too.
            try:
                with open(log, "a") as fh:
                    fh.write(f"pid {os.getpid()} (forked): {how}: {what}\n")
            except OSError:
                pass
        return ForbiddenProgram(
            f"the test suite never runs {what!r} ({how}); fake it (FakeSecurity, a fake "
            "AdminShell), and see tests/program_guard.py"
        )

    real_execute_child = subprocess.Popen._execute_child
    # Through any guard already there (a child's site, then a conftest, in
    # one process): `wraps` sets __wrapped__, which `signature` follows to
    # subprocess's own parameters.
    signature = inspect.signature(real_execute_child)

    @functools.wraps(real_execute_child)
    def execute_child(*args, **kw):
        # By name, not position: its parameters differ across Pythons.
        bound = signature.bind(*args, **kw).arguments
        argv, executable, shell = bound["args"], bound.get("executable"), bound.get("shell")
        env = bound.get("env")
        path = env.get("PATH") if isinstance(env, dict) and isinstance(env.get("PATH"), str) else None
        if (hit := offender(argv, executable=executable, shell=shell is True, path=path,
                            cwd=bound.get("cwd"))) is not None:
            raise refuse(hit, "subprocess")
        return real_execute_child(*args, **kw)

    setattr_(subprocess.Popen, "_execute_child", execute_child)

    for name in ("posix_spawn", "posix_spawnp"):
        real = getattr(os, name, None)
        if real is None:
            continue

        @functools.wraps(real)
        def spawn(path, argv, *a, _real=real, _name=name, **kw):
            env = a[0] if a else kw.get("env")
            child_path = env.get("PATH") if isinstance(env, dict) else None
            words = list(argv)
            # The file is what runs, with argv's other words (delta audit #9).
            if (hit := offender([path, *words[1:]], path=child_path) or offender(words, path=child_path)):
                raise refuse(hit, f"os.{_name}")
            if a:
                a = (_with_child_site(a[0]), *a[1:])
            elif "env" in kw:
                kw["env"] = _with_child_site(kw["env"])
            return _real(path, argv, *a, **kw)

        setattr_(os, name, spawn)

    real_system = os.system

    @functools.wraps(real_system)
    def system(command):
        if (hit := offender(command, shell=True)) is not None:
            raise refuse(hit, "os.system")
        with _child_site_in_environ():
            return real_system(command)

    setattr_(os, "system", system)

    for name in ("execv", "execve", "execl", "execle", "execlp", "execlpe", "execvp", "execvpe"):
        real = getattr(os, name, None)
        if real is None:
            continue

        @functools.wraps(real)
        def exec_(file, *args, _real=real, _name=name):
            argv = args[0] if _name in ("execv", "execve", "execvp", "execvpe") else args
            words = [_text(a) for a in argv if isinstance(a, (str, bytes, os.PathLike))]
            has_env = _name in ("execve", "execle", "execlpe", "execvpe")
            env = args[-1] if has_env and args else None
            child_path = env.get("PATH") if isinstance(env, Mapping) else None
            # The file is what runs, with argv's other words (delta audit #9).
            if (hit := offender([file, *words[1:]], path=child_path) or offender(words, path=child_path)):
                raise refuse(hit, f"os.{_name}")
            if has_env:
                return _real(file, *args[:-1], _with_child_site(env))
            with _child_site_in_environ():
                return _real(file, *args)

        setattr_(os, name, exec_)

    # os.spawn* and pty.spawn fork, then exec in the child: checked here,
    # before the fork, so the refusal is this process's and fails its test.
    for name in ("spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe"):
        real = getattr(os, name, None)
        if real is None:
            continue

        @functools.wraps(real)
        def spawn_(mode, file, *args, _real=real, _name=name):
            if _name in ("spawnv", "spawnve", "spawnvp", "spawnvpe"):
                argv = args[0]
            elif _name in ("spawnle", "spawnlpe"):
                argv = args[:-1]
            else:
                argv = args
            words = [_text(a) for a in argv if isinstance(a, (str, bytes, os.PathLike))]
            env = args[-1] if _name in ("spawnve", "spawnvpe", "spawnle", "spawnlpe") and args else None
            child_path = env.get("PATH") if isinstance(env, Mapping) else None
            # The file is what runs, with argv's other words (delta audit #9).
            # The child's exec goes through the wrappers above, which hand it
            # CHILD_SITE.
            if (hit := offender([file, *words[1:]], path=child_path) or offender(words, path=child_path)):
                raise refuse(hit, f"os.{_name}")
            return _real(mode, file, *args)

        setattr_(os, name, spawn_)

    import pty

    real_pty_spawn = pty.spawn

    @functools.wraps(real_pty_spawn)
    def pty_spawn(argv, *args, **kw):
        words = [argv] if isinstance(argv, (str, bytes, os.PathLike)) else list(argv)
        if (hit := offender(words)) is not None:
            raise refuse(hit, "pty.spawn")
        return real_pty_spawn(argv, *args, **kw)

    setattr_(pty, "spawn", pty_spawn)

    # multiprocessing's spawn and forkserver start their Python with this
    # process's environment, through neither Popen nor os (delta audit #14).
    import multiprocessing.util

    real_spawnv_passfds = multiprocessing.util.spawnv_passfds

    @functools.wraps(real_spawnv_passfds)
    def spawnv_passfds(path, args, passfds):
        words = [_text(a) for a in args]
        if (hit := offender([path, *words[1:]]) or offender(words)) is not None:
            raise refuse(hit, "multiprocessing")
        with _child_site_in_environ():
            return real_spawnv_passfds(path, args, passfds)

    setattr_(multiprocessing.util, "spawnv_passfds", spawnv_passfds)


def install(record: Callable[[str], None]) -> None:
    """Guard this process for good (the suite's own: conftest, at import)."""
    _guard(setattr, record)


def install_in_child(log: str | None) -> None:
    """Guard a child process for good: what `tests/child_site`'s
    sitecustomize runs. A refusal is appended to `log` when there is one."""

    def record(what: str) -> None:
        if not log:
            return
        with open(log, "a") as fh:
            fh.write(f"pid {os.getpid()}: {what}\n")

    _guard(setattr, record)
