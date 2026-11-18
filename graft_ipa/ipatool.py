"""Run the user's own ipatool, and say what its failures mean.

ipatool (github.com/majd/ipatool) downloads the App Store's *encrypted,
account-personalized* IPA for an app the signed-in Apple ID owns. This module
only ever runs three of its commands — `--version`, `auth info`, `download` —
and never `auth login` or `purchase`: signing in takes a password and a 2FA
code, and both stay in the user's Terminal, never in this process or the GUI.
`login_command` is how the hint names the command to type there instead.

Pinned to **2.8.0**, which is what was installed and proven on 2026-11-17.
2.8.1 (Homebrew's current at the time) breaks `download` with "available
storefronts request failed" (majd/ipatool#672); `KNOWN_BAD_VERSIONS` is how status
and errors say so, wherever the binary was found (`locate`).

`run` is injectable for the odd unit test, but the real seam is the path: tests
point it at a fake executable that prints what the real one prints.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import child_env
from .errors import AppStoreError

GOOD_VERSION = "2.8.0"
KNOWN_BAD_VERSIONS = frozenset({"2.8.1"})
RELEASE_URL = f"https://github.com/majd/ipatool/releases/tag/v{GOOD_VERSION}"

Runner = Callable[..., subprocess.CompletedProcess[str]]

_VERSION_RE = re.compile(r"version\s+v?(\S+)")


def present(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def login_command(path: Path) -> str:
    """What to type in Terminal to sign in. Never run from here.

    `--email` is left for the user to fill in rather than read from anywhere:
    this tool does not know the Apple ID and is not supposed to.
    """
    return f"{shlex.quote(str(path))} auth login --email YOUR_APPLE_ID_EMAIL"


def default_path() -> Path:
    """Where Graft looks after the configured path, and where it says to install."""
    return Path.home() / ".local" / "bin" / "ipatool"


def mac_arch() -> str:
    """The release asset's architecture for this Mac: arm64 or amd64.

    Asked of the kernel (`host.apple_silicon`), so a Python running under
    Rosetta still gets the arm64 binary; `platform.machine` only when the
    kernel would not say.
    """
    from . import host

    arm = host.apple_silicon()
    if arm is None:
        arm = platform.machine() in ("arm64", "aarch64")
    return "arm64" if arm else "amd64"


def install_steps(target: Path, arch: str | None = None) -> list[str]:
    """The Terminal commands that put ipatool GOOD_VERSION at `target`.

    The macOS release asset for this Mac's processor (`mac_arch`), unpacked into
    the user's own `$TMPDIR` — the tarball holds `bin/ipatool-<v>-macos-<arch>`
    — made executable, and moved into place. Printed by `--appstore-status`,
    never run by Graft.
    """
    name = f"ipatool-{GOOD_VERSION}-macos-{arch or mac_arch()}"
    url = f"https://github.com/majd/ipatool/releases/download/v{GOOD_VERSION}/{name}.tar.gz"
    return [
        f"mkdir -p {shlex.quote(str(target.parent))}",
        f'curl -fL {url} | tar -xzf - -C "$TMPDIR" bin/{name}',
        f'chmod +x "$TMPDIR/bin/{name}"',
        f'mv "$TMPDIR/bin/{name}" {shlex.quote(str(target))}',
    ]


def install_command(target: Path, arch: str | None = None) -> str:
    """`install_steps` as one command, for the GUI's Copy button."""
    return " && ".join(install_steps(target, arch))


def install_target(located: Path, configured: Path) -> Path:
    """Where installing GOOD_VERSION fixes what `locate` found.

    The located binary itself when it is the configured one or the default one
    (replacing it is the fix); otherwise — a bad one on PATH — the default,
    which `locate` prefers to PATH.
    """
    return located if located in (configured, default_path()) else default_path()


def locate(configured: Path) -> Path:
    """Where the ipatool to run is: configured path, then ~/.local/bin, then PATH.

    A Homebrew install lives on PATH (/opt/homebrew/bin), and used to be
    ignored. When nothing is found the configured path comes back unchanged, so
    "not found at ..." names the place the user was told to put it.
    """
    if present(configured):
        return configured
    default = default_path()
    if present(default):
        return default
    # Absolute PATH entries only: the binary found here is handed the signed-in
    # Apple ID's session, and a relative entry would let one in the current
    # directory answer for it.
    found = child_env.which("ipatool")
    if found and present(Path(found)):
        return Path(found)
    return configured


def install_hint(path: Path) -> str:
    """For an error message: where to install it, and where the commands are.

    The commands themselves are long, and the app shortens a banner's hint
    from the middle; `--appstore-status` prints them whole, and its event
    carries them as `install`.
    """
    return (
        f"install ipatool {GOOD_VERSION} to {path} — `graft --appstore-status` "
        "prints the commands"
    )


def bad_version_hint(bad: str, *, path: Path | None = None, configured: Path | None = None) -> str:
    """What to do about a known-bad ipatool, worded to stay true as releases move.

    `path` is where the bad one was found and `configured` is `appstore.ipatool`.
    When they are the same non-default path, installing GOOD_VERSION to
    ~/.local/bin would change nothing — the configured path is looked at first
    — so the hint names the config key and that binary instead.
    """
    head = (
        f"Graft is tested with ipatool {GOOD_VERSION}; {bad} has a download bug "
        f"(majd/ipatool#672)."
    )
    if path is not None and path == configured and configured != default_path():
        return (
            f"{head} appstore.ipatool in config.yaml points at {path}, so that is the one "
            f"Graft runs: replace it with ipatool {GOOD_VERSION} from {RELEASE_URL}, or "
            f"point appstore.ipatool at a {GOOD_VERSION} binary."
        )
    return (
        f"{head} Install ipatool {GOOD_VERSION} from {RELEASE_URL} to "
        "~/.local/bin/ipatool, which Graft uses before one on PATH, or set appstore.ipatool."
    )


def version(path: Path, *, run: Runner = subprocess.run) -> str | None:
    """The version ipatool reports, or None if it isn't there or won't say."""
    if not present(path):
        return None
    try:
        proc = run([str(path), "--version"], capture_output=True, text=True, timeout=15,
                   env=child_env.child_env())
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION_RE.search(f"{proc.stdout}\n{proc.stderr}")
    return match.group(1) if match else None


def _json_lines(*streams: str) -> list[dict[str, Any]]:
    """Every line of `streams` that is a JSON object, in order.

    `--format json` is one object per line, but which stream carries the error
    line has not been pinned down across versions — reading both costs nothing
    and a line on the "wrong" one is not a reason to report a blank error.
    """
    out: list[dict[str, Any]] = []
    for stream in streams:
        for line in stream.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def signed_in(
    path: Path, *, run: Runner = subprocess.run, timeout: float = 30
) -> bool | None:
    """Whether ipatool has an account in the keychain: True, False, or None
    for "couldn't tell".

    None is its own answer because the other two each send the user somewhere:
    False to Terminal to type a password and a 2FA code. An `auth info` that
    hung, could not run, or printed no verdict says nothing about the account,
    and treating it as signed out sends them there for nothing.

    `auth info` prints the account's email and name; neither is kept, returned
    or logged — status reports only that *someone* is signed in.
    """
    if not present(path):
        return False
    try:
        proc = run(
            [str(path), "auth", "info", "--format", "json"],
            capture_output=True, text=True, timeout=timeout, env=child_env.child_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    verdicts = [obj for obj in _json_lines(proc.stdout, proc.stderr) if "success" in obj]
    if not verdicts:
        return None
    return any(obj.get("success") is True for obj in verdicts)


def explain(error: str, *, bundle_id: str, path: Path, reported_version: str | None) -> AppStoreError:
    """Turn one of ipatool's error strings into an error with something to do.

    Matched on words rather than whole sentences: these are ipatool's own Go
    error strings (`license is required`, `password token is expired`, the
    keyring lookup's "could not be found in the keyring"), which are stable
    across 2.x but wrapped differently by different commands.
    """
    lower = error.lower()
    if (
        "keyring" in lower
        or "failed to get account" in lower
        or "not logged in" in lower
        or ("token" in lower and ("expired" in lower or "invalid" in lower))
    ):
        return AppStoreError(
            f"ipatool is not signed in to the App Store (ipatool: {error})",
            hint=(
                f"sign in from Terminal: {login_command(path)} — the first "
                "login can sit silent for ~8 minutes while it fetches its "
                "sign-in helper; that is normal"
            ),
        )
    if "license" in lower or "not owned" in lower:
        return AppStoreError(
            f"this Apple ID doesn't own {bundle_id} yet (ipatool: {error})",
            hint=(
                "tap Get for this app on an iPhone or iPad signed in to the same "
                "Apple ID, then try again — ipatool can't get a free app for a "
                "US account itself (majd/ipatool#666)"
            ),
        )
    if reported_version in KNOWN_BAD_VERSIONS or "storefront" in lower:
        return AppStoreError(
            f"ipatool couldn't download {bundle_id}: {error}",
            hint=(
                bad_version_hint(reported_version)
                if reported_version in KNOWN_BAD_VERSIONS
                else install_hint(path)
            ),
        )
    return AppStoreError(
        f"ipatool couldn't download {bundle_id}: {error}",
        hint=(
            "that is ipatool's own error — running the same download in "
            "Terminal may say more"
        ),
    )


def download(
    path: Path, bundle_id: str, out_dir: Path, *, run: Runner = subprocess.run
) -> Path:
    """Download `bundle_id`'s latest encrypted IPA into `out_dir`; return the file.

    No timeout: a ~1 GB app on a slow line is legitimately slow, and the GUI's
    Stop is the way out of one that is not.
    """
    if not present(path):
        raise AppStoreError(f"ipatool not found at {path}", hint=install_hint(path))
    try:
        proc = run(
            [
                str(path), "download", "-b", bundle_id, "-o", str(out_dir),
                "--format", "json", "--non-interactive",
            ],
            capture_output=True, text=True, env=child_env.child_env(),
        )
    except OSError as e:
        raise AppStoreError(f"couldn't run ipatool at {path}: {e}") from e

    lines = _json_lines(proc.stdout, proc.stderr)
    verdict = next((obj for obj in reversed(lines) if "success" in obj), None)
    if verdict is None or verdict.get("success") is not True:
        error = ""
        if verdict is not None:
            error = str(verdict.get("error") or "")
        if not error:
            tail = (proc.stderr.strip() or proc.stdout.strip()).splitlines()[-1:]
            error = tail[0] if tail else f"exited {proc.returncode} without saying why"
        raise explain(error, bundle_id=bundle_id, path=path, reported_version=version(path, run=run))

    named = verdict.get("output")
    if not isinstance(named, str) or not named.strip():
        # Not `Path("")`: that resolves to the current directory, and the
        # containment check below then misreports this as a file written
        # outside `out_dir`.
        raise AppStoreError("ipatool reported success but named no file")
    output = Path(named)
    root = out_dir.resolve()
    try:
        resolved = output.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        # Everything downstream deletes `out_dir` and trusts that this removes
        # the download with it — the file carries the account's license. One
        # that landed elsewhere would outlive the run, so it is not accepted
        # (and not touched: it is not ours to delete).
        raise AppStoreError(
            f"ipatool wrote the IPA outside {out_dir}: {output}",
            hint="delete that file by hand — it holds this Apple ID's license",
        ) from None
    if not resolved.is_file():
        raise AppStoreError(f"ipatool reported success but {output} does not exist")
    return resolved
