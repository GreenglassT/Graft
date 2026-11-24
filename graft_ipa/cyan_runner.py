"""Wrap the `cyan` (pyzule-rw) subprocess for tweak injection."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import tools
from .child_env import BUNDLED_OPENSSL_CONF, child_env
from .config import AppConfig, Config, CyanOptions
from .errors import CyanError

# Pinned to the release Graft is tested with (cyan 1.4.4), by the commit its tag
# points at rather than by the tag: a tag can be moved, and a tag archive moves
# with it, while a commit's archive cannot change. Neither the moving main
# branch nor a retagged release can reach a fresh install this way.
CYAN_V1_4_4_COMMIT = "04a40f2aa65ec9cb36127f74d52ae4ef71377c50"
CYAN_INSTALL_COMMAND = (
    "pipx install --force "
    f"https://github.com/asdfzxcvbn/pyzule-rw/archive/{CYAN_V1_4_4_COMMIT}.zip"
)


#: Set in cyan's environment on top of `child_env`. cctools-port's
#: install_name_tool, which a cyan whose helpers were built from source runs,
#: re-signs each ARM file it changes with `system("codesign -s - -f <path>")`,
#: the path unquoted, so a tweak or app file name holding `;cmd` ran `cmd`.
#: Graft.app's copy is built with that hook compiled out
#: (bundle/patches/cctools-port-no-fake-sign.patch); NO_CODESIGN is the hook's
#: own off switch, kept as a second layer.
CYAN_ENV = {"NO_CODESIGN": "1"}


@dataclass
class CyanResult:
    output_ipa: Path
    stdout: str
    stderr: str


_MISSING_LIB_RE = re.compile(r'Library not loaded:\s+([^\s"\']+)')

_BAD_DEB_RE = re.compile(r"couldn'?t extract\s+([^\s]+)", re.IGNORECASE)

#: cyan's flag that removes Watch apps (`Watch`, `WatchKit`,
#: `com.apple.WatchPlaceholder`), short and long.
_WATCH_FLAGS = ("-w", "--no-watch")

#: Terminal escape sequences: CSI (color, `ESC[96m`; erase, `ESC[K`), OSC
#: (`ESC]…BEL` or `ESC]…ESC\\`, window titles and links), character-set
#: designations (`ESC(B`), and the two-byte forms. cyan colors its counts for
#: a terminal whether or not it has one.
_ANSI_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[ -/]+[0-~]|[@-Z\\-_])"
)


def strip_ansi(text: str) -> str:
    """`text` without terminal escape sequences.

    cyan's output is printed to a human, emitted as `log` events for the app's
    build log, and quoted in an error's hint. Its color codes rode through
    all three, so the app showed `changed [96m41[0m localized names`. Stripped
    once, where the output enters graft.
    """
    return _ANSI_RE.sub("", text)


def build_command(
    cfg: Config,
    app: AppConfig,
    input_ipa: Path,
    output_ipa: Path,
) -> list[str]:
    """Build the cyan argv from app config."""
    tweak_paths: list[str] = []
    missing: list[str] = []
    for t in app.tweaks:
        p = cfg.tweak_path(t)
        if not p.exists():
            missing.append(str(p))
        tweak_paths.append(str(p))
    if missing:
        raise CyanError(
            "missing tweak files: " + ", ".join(missing),
            hint=f"download them and place under {cfg.tweaks_dir}",
        )

    # The resolved binary (`tools.resolve`); a bare "cyan" only when there is
    # none, which a dry run still renders and `run_cyan` refuses.
    cyan = str(tools.resolve("cyan").path or "cyan")
    cmd: list[str] = [cyan, "-i", str(input_ipa), "-o", str(output_ipa)]
    if tweak_paths:
        cmd += ["-f", *tweak_paths]
    opts = app.cyan
    if opts.display_name:
        cmd.append(f"-n={opts.display_name}")  # one token: a name starting "-" is not an option
    if opts.strip_supported_devices:
        cmd.append("-u")
    if opts.strip_extensions:
        cmd.append("-e")
    if not opts.keep_bundle_id:
        new_id = opts.new_bundle_id or f"{app.bundle_id}.plus"
        cmd += ["-b", new_id]
        # A Watch app names its companion's bundle id
        # (WKCompanionAppBundleIdentifier), which -b leaves alone, so iOS
        # refuses the renamed IPA with InvalidCompanionAppBundleIdentifier. A
        # Watch app cannot install under a renamed companion, so it goes.
        # (-e removes PlugIns/Extensions only; the Watch app needs -w.)
        if not any(a in _WATCH_FLAGS for a in opts.extra_args):
            cmd.append("-w")
    if opts.compression_level is not None:
        cmd += ["-c", str(opts.compression_level)]
    cmd.extend(opts.extra_args)
    return cmd


def removes_watch(opts: CyanOptions) -> bool:
    """Whether cyan is told to remove the Watch app (`-w`): for a renamed
    bundle id (`keep_bundle_id: false`, `build_command`), or by the user in
    `extra_args`. Its extensions are then not on the device, and nothing
    should be said about what they ask for (`capabilities.missing_for`)."""
    return not opts.keep_bundle_id or any(a in _WATCH_FLAGS for a in opts.extra_args)


def run_cyan(
    cfg: Config,
    app: AppConfig,
    input_ipa: Path,
    output_ipa: Path,
    *,
    dry_run: bool = False,
) -> CyanResult:
    """Run cyan and return its output. Translates known errors into hints."""
    cmd = build_command(cfg, app, input_ipa, output_ipa)
    if dry_run:
        return CyanResult(output_ipa=output_ipa, stdout=" ".join(cmd), stderr="")
    resolved = tools.resolve("cyan")
    if not resolved.found:
        raise CyanError("cyan not found in PATH", hint=CYAN_INSTALL_COMMAND)
    # Graft.app's cyan runs Graft.app's ldid, which links the bundled
    # libcrypto: no OpenSSL configuration file for it (review 2.0, bundle #7).
    # A cyan from PATH runs its own tools, with the user's environment.
    openssl = BUNDLED_OPENSSL_CONF if resolved.source == tools.SOURCE_BUNDLED else {}

    output_ipa.parent.mkdir(parents=True, exist_ok=True)
    # cyan unpacks the IPA into a `tempfile.TemporaryDirectory()`, which is
    # only removed when cyan exits normally; a Stop kills it and left the
    # unpacked app (hundreds of MB) in the system temp folder. Its TMPDIR is
    # a private folder beside the output, inside the build's work dir: removed
    # here once cyan is gone (subprocess.run waits for the child it kills),
    # and by the build's own cleanup if this process is killed outright.
    scratch = Path(tempfile.mkdtemp(prefix=".cyan-tmp-", dir=output_ipa.parent))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            env=child_env({"TMPDIR": f"{scratch}/", **CYAN_ENV, **openssl}),
        )
    except FileNotFoundError as e:  # gone since it was resolved
        raise CyanError(
            "cyan not found in PATH",
            hint=CYAN_INSTALL_COMMAND,
        ) from e
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    stdout, stderr = strip_ansi(proc.stdout or ""), strip_ansi(proc.stderr or "")
    combined = stdout + "\n" + stderr
    if proc.returncode != 0 or not output_ipa.exists():
        _raise_translated(combined, proc.returncode)

    return CyanResult(output_ipa=output_ipa, stdout=stdout, stderr=stderr)


def _raise_translated(output: str, returncode: int) -> None:
    """Parse cyan's output for known failure modes and raise a helpful CyanError."""
    m = _MISSING_LIB_RE.search(output)
    if m:
        lib = m.group(1)
        name = lib.rsplit("/", 1)[-1]
        raise CyanError(
            f"cyan failed: tweak references missing library {lib}",
            hint=(
                f"add a sandbox-compatible {name} (as a .dylib or .deb) to the tweaks list "
                "in config.yaml and re-run. Common culprits: shared settings "
                "libraries a tweak links but the app does not ship."
            ),
        )
    m = _BAD_DEB_RE.search(output)
    if m:
        bad = m.group(1)
        raise CyanError(
            f"cyan failed: couldn't extract {bad}",
            hint=(
                f"{bad} is probably an HTML error page rather than a real .deb. "
                "Re-download from canister.me / Havoc / the dev's GitHub release."
            ),
        )
    if "Unrecognized archive format" in output or "BadZipFile" in output:
        raise CyanError(
            "cyan failed: unrecognized archive format in one of the tweaks",
            hint="one of the .deb files is corrupted. Re-download from a trusted repo.",
        )
    raise CyanError(
        f"cyan exited with code {returncode}",
        hint=output.strip() or "re-run with --verbose to see the full output",
    )
