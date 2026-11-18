"""Decrypt an App Store IPA through the user's own UnfairPlay build.

UnfairPlay (github.com/DrWhax/unfairplay, v0.4.0, built from source, no network
code) decrypts a FairPlay-encrypted IPA offline with a model recovered once from
this Mac's App Store enrollment. That enrollment — `sudo unfairplay setup` — is
the user's to run, in Terminal, and this module never does: it only runs
`--version` and `decrypt`, and reports whether a model exists.

The model decrypts **any** IPA personalized to the same Apple ID, including
apps Apple does not allow on the Mac — which is the whole reason this path
replaced the jailbroken-phone dumpers for most apps.

`PlaintextVerified: true` means every decrypted page matched the pre-encryption
hashes in Apple's own CodeDirectory: proof the binary is Apple's, byte for
byte. This module reports it; whether `false` is acceptable is the caller's
rule (`appstore.py`, and `--allow-unverified`).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import AppStoreError

Runner = Callable[..., subprocess.CompletedProcess[str]]

PROJECT_URL = "https://github.com/DrWhax/unfairplay"

_VERSION_RE = re.compile(r"version\s+v?(\S+)")


@dataclass(frozen=True)
class DecryptReport:
    output_path: Path
    encrypted_images: int
    decrypted_pages: int
    plaintext_verified: bool


def present(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def setup_command(path: Path) -> str:
    """What to type in Terminal to enroll this Mac. Never run from here.

    Needs root and an iPhone/iPad app installed on this Mac from the App Store
    under the same Apple ID; which app is the user's choice, so it is left as a
    placeholder rather than guessed.
    """
    return f"sudo {shlex.quote(str(path))} setup --app /Applications/YOUR_IPAD_APP.app"


def install_hint(path: Path) -> str:
    return f"build UnfairPlay v0.4.0 from {PROJECT_URL} and put the binary at {path}"


def version(path: Path, *, run: Runner = subprocess.run) -> str | None:
    if not present(path):
        return None
    try:
        proc = run([str(path), "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION_RE.search(f"{proc.stdout}\n{proc.stderr}")
    return match.group(1) if match else None


def enrolled(model_dir: Path) -> bool:
    """Whether `model_dir` holds a recovered enrollment model (`*.bin`)."""
    try:
        return any(p.suffix == ".bin" and p.is_file() for p in model_dir.iterdir())
    except OSError:
        return False


def _report_json(stdout: str) -> dict[str, Any] | None:
    """The JSON report UnfairPlay prints — pretty-printed across lines.

    Tried at every `{`, not only the first: a log line with a brace in it
    ("loading model {…}") ahead of the report would otherwise be the only thing
    parsed, fail, and turn a good decrypt into "printed no report". The report
    is the object that carries `PlaintextVerified`; failing that, the first
    object that parses, so a report missing fields is named as such.
    """
    decoder = json.JSONDecoder()
    first: dict[str, Any] | None = None
    start = stdout.find("{")
    while start >= 0:
        try:
            obj, _ = decoder.raw_decode(stdout[start:])
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            if "PlaintextVerified" in obj:
                return obj
            if first is None:
                first = obj
        start = stdout.find("{", start + 1)
    return first


def _count(report: dict[str, Any], key: str) -> int:
    value = report[key]
    # `bool` is an `int`; `True` pages decrypted is not a count.
    if isinstance(value, bool) or not isinstance(value, int):
        raise AppStoreError(f"UnfairPlay's report has a malformed {key}: {value!r}")
    return value


def decrypt(
    path: Path,
    *,
    input_ipa: Path,
    output_ipa: Path,
    report: Path,
    model_dir: Path,
    run: Runner = subprocess.run,
) -> DecryptReport:
    """Decrypt `input_ipa` to `output_ipa`, always with `--strip-account-data`.

    `--strip-account-data` drops the SC_Info license files and the Apple ID
    fields from iTunesMetadata.plist, so the staged IPA identifies nobody.

    `--report` sends the private report where the caller asked — a temp dir it
    is about to delete. Without it the report lands *beside the input*, which
    for a dropped IPA is the user's own folder. A build that ignores the flag
    would still do that, so the default sibling is removed afterwards — but
    only if this run created it.
    """
    if not present(path):
        raise AppStoreError(f"UnfairPlay not found at {path}", hint=install_hint(path))
    sibling = input_ipa.with_name(input_ipa.stem + ".decrypted.json")
    sibling_was_there = sibling.exists()
    try:
        try:
            proc = run(
                [
                    str(path), "decrypt",
                    "--input", str(input_ipa),
                    "--output", str(output_ipa),
                    "--report", str(report),
                    "--model", str(model_dir),
                    "--strip-account-data",
                ],
                capture_output=True, text=True,
            )
        except OSError as e:
            raise AppStoreError(f"couldn't run UnfairPlay at {path}: {e}") from e
    finally:
        if not sibling_was_there:
            sibling.unlink(missing_ok=True)

    if proc.returncode != 0:
        said = (proc.stderr.strip() or proc.stdout.strip()).splitlines()
        reason = said[-1] if said else f"exited {proc.returncode} without saying why"
        raise AppStoreError(
            f"UnfairPlay couldn't decrypt {input_ipa.name}: {reason}",
            hint=(
                "the IPA must be personalized to the same Apple ID this Mac "
                "enrolled with — `unfairplay doctor` in Terminal can say more"
            ),
        )
    parsed = _report_json(proc.stdout)
    if parsed is None:
        raise AppStoreError("UnfairPlay finished but printed no report")
    missing = [
        k for k in ("EncryptedImages", "DecryptedPages", "PlaintextVerified") if k not in parsed
    ]
    if missing:
        raise AppStoreError(f"UnfairPlay's report is missing {', '.join(missing)}")
    if not output_ipa.is_file():
        raise AppStoreError(f"UnfairPlay reported success but {output_ipa} does not exist")
    return DecryptReport(
        output_path=output_ipa,
        encrypted_images=_count(parsed, "EncryptedImages"),
        decrypted_pages=_count(parsed, "DecryptedPages"),
        # `is True`, not `bool(...)`: a string "false" is truthy, and this is
        # the one field whose wrong answer stages an unverified binary.
        plaintext_verified=parsed["PlaintextVerified"] is True,
    )
