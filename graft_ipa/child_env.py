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
import stat
from collections.abc import Iterable, Mapping, MutableMapping

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


#: The system's own tool folders, after Graft.app's in its children's PATH.
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

#: Variables a login shell may export that change where OpenSSL and Python
#: look for their configuration and CA certificates. Graft.app's CLI gets the
#: user's login-shell environment from the app.
#:
#: OpenSSL's own configuration never reaches the bundled zsign, ipatool or
#: Python: an OPENSSL_CONF meant for Homebrew's openssl would make every TLS
#: connection behave differently from the release's checks (the review's
#: completeness critic).
OPENSSL_OVERRIDES = ("OPENSSL_CONF", "OPENSSL_ENGINES")
#: A CA bundle is different: behind a TLS-inspecting proxy it is the only way
#: to trust the proxy's root, since the bundled Python reads certifi and
#: /etc/ssl/cert.pem, never the keychain (combined fix check #13). Each is
#: kept when it names an existing, readable regular file by an absolute path,
#: and dropped otherwise, so a stale export can't send TLS to a file that
#: isn't there. Proxy variables are the user's choice and always stay.
CA_FILE_OVERRIDES = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
#: Kept when it names an existing directory by an absolute path.
CA_DIR_OVERRIDES = ("SSL_CERT_DIR",)
TLS_OVERRIDES = (*OPENSSL_OVERRIDES, *CA_FILE_OVERRIDES, *CA_DIR_OVERRIDES)

#: For a child running the OpenSSL bundled in Graft.app (its zsign, and the
#: ldid its cyan runs): no configuration file. That libcrypto was configured
#: with OPENSSLDIR /opt/graft/openssl/ssl, and OpenSSL 3 reads openssl.cnf
#: there at start-up unless OPENSSL_CONF says otherwise (and the user's is
#: dropped above), so a file put there could load providers or engines into
#: the process holding the signing key (review 2.0, bundle #7). /dev/null is
#: an empty configuration, which OpenSSL 3.5 accepts. Not for every child: a
#: tool the user installed brings its own OpenSSL and its configuration.
BUNDLED_OPENSSL_CONF = {"OPENSSL_CONF": "/dev/null"}


def usable_ca_override(name: str, value: str | None) -> bool:
    """Whether the CA override `name`=`value` is kept in Graft.app: a
    readable regular file for the file variables, a directory for
    SSL_CERT_DIR, either named by an absolute path. Never OPENSSL_*."""
    if not value or not os.path.isabs(value):
        return False
    if name in CA_DIR_OVERRIDES:
        return os.path.isdir(value)
    if name not in CA_FILE_OVERRIDES:
        return False
    try:
        st = os.stat(value)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and os.access(value, os.R_OK)


def _drop_tls_overrides(env: MutableMapping[str, str]) -> None:
    for name in TLS_OVERRIDES:
        if name in env and not usable_ca_override(name, env[name]):
            del env[name]


def _bundled_bin():
    """Graft.app's `Contents/Helpers/bin` when this CLI is the app's, else None."""
    from . import tools

    return tools.bundled_tools_dir()


def child_env(extra: Mapping[str, str] | None = None, *, path_also: Iterable[os.PathLike | str] = ()) -> dict[str, str]:
    """This process's environment for a child: no secrets, an absolute `PATH`,
    and then `extra` — the one place a credential a child needs goes in.

    In Graft.app the child's PATH is the app's `Helpers/bin`, then
    `path_also` (the folder of a tool the user installed, which its own
    `#!/usr/bin/env node` needs), then the system's folders: never Homebrew's
    zip, unzip, tar, codesign or install_name_tool in place of the system's.
    OPENSSL_OVERRIDES are left out, and so is any CA override that names
    nothing usable (`usable_ca_override`). Finding a tool the user installed
    (`which`) still searches their own PATH."""
    env = {k: v for k, v in os.environ.items() if k not in _SECRETS}
    bundled = _bundled_bin()
    if bundled is not None:
        also = [str(d) for d in path_also if os.path.isabs(str(d))]
        env["PATH"] = os.pathsep.join([str(bundled), *also, SYSTEM_PATH])
        _drop_tls_overrides(env)
    elif "PATH" in env:
        env["PATH"] = absolute_path(env["PATH"])
    if extra:
        env.update(extra)
    return env


def tame_bundled_process() -> None:
    """In Graft.app, drop the same TLS overrides from this process too: the
    CLI's own requests and ssl read SSL_CERT_FILE and REQUESTS_CA_BUNDLE.
    Called first thing by `python -m graft_ipa`."""
    if _bundled_bin() is not None:
        _drop_tls_overrides(os.environ)
