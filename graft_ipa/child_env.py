"""What a tool Graft runs inherits: the environment, minus Graft's secrets.

Two secrets reach this process through the environment — the `.p12` password,
under whatever name `signing.cert_password_env` gives it, and a jailbroken
device's root password in `GRAFT_SSH_PASS`. Neither is any business of cyan,
ipatool, UnfairPlay, zsign, iproxy or `ideviceinstaller`, and every one of them
used to get both: a child inherits the whole environment unless told otherwise,
and so does every process *it* starts. `child_env()` is that "otherwise", and
every subprocess in the package is started with it. A child that does need a
credential is handed it on purpose, under its own name — bagbak gets
`SSH_PASSWORD` (`decrypt_runner.bagbak_env`), frida-ios-dump the variable its
launcher reads (`decrypt_runner.FRIDA_DUMP_PASSWORD_ENV`).

The same environment carries a `PATH` of absolute entries only. An empty or
relative entry (`PATH=bin:...`, or the `::` a careless export leaves) is looked
up against whatever directory the CLI happens to run in, so `zsign` or
`security` could be a file sitting in the user's download folder. A child
started by name is found on the `PATH` of the environment it is given — Python
searches `os.get_exec_path(env)`, not this process's — so the cleaned `PATH`
covers the lookup as well as the child's own children. `which` answers on that
same `PATH`, for the code that wants a tool's location rather than to run it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

#: The device root password `decrypt_runner.resolve_ssh_password` reads.
SSH_PASSWORD_ENV = "GRAFT_SSH_PASS"

#: The `.p12` password variable `--init` writes into every starter config.
DEFAULT_CERT_PASSWORD_ENV = "GRAFT_P12_PASS"

#: Secrets by the names Graft reads them under. `TB_SSH_PASS` is the pre-rename
#: spelling of `GRAFT_SSH_PASS`: no longer read (`legacy_env`), but still a
#: password if someone has it set, and nothing a child needs either.
_SECRETS: set[str] = {DEFAULT_CERT_PASSWORD_ENV, SSH_PASSWORD_ENV, "TB_SSH_PASS"}


def register_secret(name: str) -> None:
    """Keep the variable `name` out of every child's environment.

    `SigningConfig` calls this with its `cert_password_env`, so a config that
    names its own variable has that one scrubbed too, from the moment the
    config is read. Additive and process-wide: a name once registered is a
    secret for the rest of the run, which is the safe direction to be wrong in.
    """
    if name:
        _SECRETS.add(name)


def secret_names() -> frozenset[str]:
    return frozenset(_SECRETS)


def absolute_path(path: str | None = None) -> str:
    """`path` (default: this process's `PATH`) with every entry that is not an
    absolute directory dropped: empty ones, `.`, `bin`, `./tools`."""
    if path is None:
        path = os.environ.get("PATH", os.defpath)
    return os.pathsep.join(p for p in path.split(os.pathsep) if os.path.isabs(p))


def which(name: str) -> str | None:
    """`shutil.which` over the absolute entries of `PATH` only.

    Its own loop rather than `shutil.which(name, path=…)`: the same lookup,
    and not one a caller's stand-in for `shutil.which` (a test's "no such
    tool") reaches into.
    """
    for directory in absolute_path().split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """This process's environment for a child: no secrets, an absolute `PATH`,
    and then `extra` — the one place a credential a child needs goes in."""
    env = {k: v for k, v in os.environ.items() if k not in _SECRETS}
    if "PATH" in env:
        env["PATH"] = absolute_path(env["PATH"])
    if extra:
        env.update(extra)
    return env
