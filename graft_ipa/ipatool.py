"""Run the user's own ipatool, and say what its failures mean.

ipatool (github.com/majd/ipatool) downloads the App Store's *encrypted,
account-personalized* IPA for an app the signed-in Apple ID owns. This module
only ever runs three of its commands — `--version`, `auth info`, `download` —
and never `auth login` or `purchase`: signing in takes a password and a 2FA
code, and both stay in the user's Terminal, never in this process or the GUI.
`login_command` is how the hint names the command to type there instead.

Pinned to **2.8.0**, which is what was installed and proven on 2026-11-17.
2.8.1 (Homebrew's current at the time) breaks `download` with "available
storefronts request failed" (majd/ipatool#672); `BROKEN_VERSION` is how status
and errors say so.

`run` is injectable for the odd unit test, but the real seam is the path: tests
point it at a fake executable that prints what the real one prints.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import AppStoreError

GOOD_VERSION = "2.8.0"
BROKEN_VERSION = "2.8.1"
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


def install_hint(path: Path) -> str:
    return (
        f"install ipatool {GOOD_VERSION} (not Homebrew's {BROKEN_VERSION}, whose "
        f"downloads fail — majd/ipatool#672) from {RELEASE_URL} to {path}"
    )


def version(path: Path, *, run: Runner = subprocess.run) -> str | None:
    """The version ipatool reports, or None if it isn't there or won't say."""
    if not present(path):
        return None
    try:
        proc = run([str(path), "--version"], capture_output=True, text=True, timeout=15)
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
            capture_output=True, text=True, timeout=timeout,
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
    if reported_version == BROKEN_VERSION or "storefront" in lower:
        return AppStoreError(
            f"ipatool couldn't download {bundle_id}: {error}",
            hint=install_hint(path),
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
            capture_output=True, text=True,
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
