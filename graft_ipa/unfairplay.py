"""Decrypt an App Store IPA through the user's own UnfairPlay build.

UnfairPlay (github.com/DrWhax/unfairplay, v0.4.0, no network code) decrypts
a FairPlay-encrypted IPA offline, with a model recovered once from this
Mac's App Store enrollment. That enrollment, `unfairplay setup` as root, is
never run from here. The CLI only builds its script
(`enrollment.enroll_command`); Graft.app runs it behind macOS's
administrator prompt, or a Terminal user runs it with sudo, and the model is
moved into place as the user (`enrollment.finish`). This module runs
`--version` and `decrypt`, and says whether a model this user can use
exists (`model_state`).

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
import pwd
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import install_hints, tools
from .child_env import child_env
from .errors import AppStoreError

if TYPE_CHECKING:
    from .config import AppStoreToolsConfig

Runner = Callable[..., subprocess.CompletedProcess[str]]

PROJECT_URL = "https://github.com/DrWhax/unfairplay"

#: The release Graft is tested with; `cmd/unfairplay` is its command at this tag.
TESTED_TAG = "v0.4.0"

_VERSION_RE = re.compile(r"version\s+v?(\S+)")


@dataclass(frozen=True)
class DecryptReport:
    output_path: Path
    encrypted_images: int
    decrypted_pages: int
    plaintext_verified: bool


def present(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def resolve_tool(cfg: AppStoreToolsConfig) -> tools.Resolved:
    """The UnfairPlay to run (`tools.resolve`): the user's own choice, then
    Graft.app's bundled copy, then the default build location, then PATH.

    `path` is never None: when nothing is found it is `cfg.unfairplay`, the
    place the build instructions put it.
    """
    explicit = "unfairplay" in cfg.explicit
    return tools.resolve(
        "unfairplay", configured=cfg.unfairplay if explicit else None, default=cfg.unfairplay
    )


#: What goes in the enrollment command's `--app` path: said in words, since
#: which app it is is the user's choice.
APP_PLACEHOLDER = "<an iPhone/iPad app from the Mac App Store, opened once>"

#: How a Terminal user starts enrolling this Mac: it prints the script
#: (`enrollment.enroll_command`) to run with `sudo /bin/sh`, after which
#: `--appstore-enroll-finish` moves the model in. Graft never prints a
#: command that runs anything of Graft.app's as root itself: UnfairPlay as
#: root runs only inside that script, from root's own checked copy.
ENROLL_COMMAND = f'graft --appstore-enroll-command --app "/Applications/{APP_PLACEHOLDER}.app"'

#: The model decrypts only IPAs personalized to the Apple ID the Mac App
#: Store installed the enrolling app with, and Graft downloads with the
#: Apple ID signed in to Graft: the two must be the same.
SAME_APPLE_ID = "use the same Apple ID in Graft as in the Mac App Store"

#: The same steps in Terminal, set up for the first time or again.
_ENROLL_IN_TERMINAL = (
    f"in Terminal, `{ENROLL_COMMAND}`, then run the script it prints with `sudo /bin/sh` and "
    "`graft --appstore-enroll-finish <the folder it prints>`"
)
#: Where to set up decryption, in Graft.app and in Terminal, on a Mac that
#: isn't enrolled yet ...
SET_UP_DECRYPTION = f"in Graft.app, Settings › App Store › Set Up Decryption…; {_ENROLL_IN_TERMINAL}"
#: ... and on one that is, where Settings' button reads "Set Up Again…"
#: (AppStoreSettingsSection): a decrypt only fails after enrollment.
SET_UP_AGAIN = f"in Graft.app, Settings › App Store › Set Up Again…; {_ENROLL_IN_TERMINAL}"


def build_steps(path: Path) -> list[str]:
    """The Terminal commands that build UnfairPlay TESTED_TAG into `path`.

    Go first if there is none (from Homebrew), then the tagged source, built
    straight to where Graft looks for it. Printed by `--appstore-status`, never
    run by Graft.
    """
    steps = [] if install_hints.go_present() else ["brew install go"]
    return [
        *steps,
        f"git clone {PROJECT_URL}",
        f"git -C unfairplay checkout {TESTED_TAG}",
        f"mkdir -p {shlex.quote(str(path.parent))}",
        f"go -C unfairplay build -o {shlex.quote(str(path))} ./cmd/unfairplay",
    ]


def install_hint(path: Path) -> str:
    """For an error message; the commands are in `--appstore-status`."""
    return (
        f"build UnfairPlay {TESTED_TAG} from source to {path} — "
        "`graft --appstore-status` prints the commands"
    )


def version(path: Path, *, run: Runner = subprocess.run) -> str | None:
    if not present(path):
        return None
    try:
        proc = run([str(path), "--version"], capture_output=True, text=True, timeout=15,
                   env=child_env())
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION_RE.search(f"{proc.stdout}\n{proc.stderr}")
    return match.group(1) if match else None


@dataclass(frozen=True)
class ModelState:
    """Whether a model this user can use exists in the model folder, and if
    one exists but can't be used, why (`problem`)."""

    enrolled: bool
    problem: str | None = None


def _owner(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"user {uid}"


_AGAIN = "set up decryption again (Graft: Settings › App Store), which saves a model that is yours"


def model_state(model_dir: Path, *, uid: int | None = None) -> ModelState:
    """A `*.bin` in `model_dir` that this user owns and can read.

    UnfairPlay run as root with no `SUDO_UID` (the macOS administrator
    prompt sets none: Task 0, S2.3) leaves its model root's, in a root-owned
    folder, mode 0700 (S2.1). Graft's own enrollment never lets root write
    here: root works in a folder of its own, and the model is moved in
    afterward, as this user (`enrollment.finish`). A model someone else owns
    is not enrolled: `unfairplay decrypt`, running as this user, couldn't
    open it.
    """
    uid = os.getuid() if uid is None else uid
    try:
        models = [
            p for p in model_dir.iterdir()
            if p.suffix == ".bin" and p.is_file() and not p.is_symlink()
        ]
    except FileNotFoundError:
        return ModelState(False)
    except PermissionError:
        return ModelState(False, f"you can't open {model_dir}: {_AGAIN}")
    except OSError as e:
        return ModelState(False, f"couldn't read {model_dir}: {e.strerror}")
    if not models:
        return ModelState(False)
    folder_owner = model_dir.stat().st_uid
    if folder_owner != uid:
        return ModelState(False, f"{model_dir} belongs to {_owner(folder_owner)}, not you: {_AGAIN}")
    for model in models:
        if model.stat().st_uid == uid and os.access(model, os.R_OK):
            return ModelState(True)
    return ModelState(
        False,
        f"the model in {model_dir} belongs to {_owner(models[0].stat().st_uid)} or you "
        f"can't read it: {_AGAIN}",
    )


def enrolled(model_dir: Path) -> bool:
    """Whether `model_dir` holds a model this user can use (`model_state`)."""
    return model_state(model_dir).enrolled


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
    fields from iTunesMetadata.plist. It is asked for, not relied on: staging
    (`provide_ipa.stage`) removes both itself and checks they are gone.

    `--report` sends the private report where the caller asked — a temp dir it
    is about to delete. Without it the report lands *beside the input*, which
    for a dropped IPA is the user's own folder. A build that ignores the flag
    would still do that, so the default sibling is removed afterward — but
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
                capture_output=True, text=True, env=child_env(),
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
                "this Mac's decryption works only for apps downloaded with the Apple ID "
                "that installed the Mac App Store app it was set up with, so "
                f"{SAME_APPLE_ID}, or set decryption up again with an app installed from "
                "the Mac App Store under the same Apple ID that Graft signs in with "
                f"({SET_UP_AGAIN}). `unfairplay doctor` in Terminal can say more"
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
