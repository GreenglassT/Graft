"""Wrap the `cyan` (pyzule-rw) subprocess for tweak injection."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .child_env import child_env
from .config import AppConfig, Config
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


@dataclass
class CyanResult:
    output_ipa: Path
    stdout: str
    stderr: str


_MISSING_LIB_RE = re.compile(r'Library not loaded:\s+([^\s"\']+)')
_BAD_DEB_RE = re.compile(r"couldn'?t extract\s+([^\s]+)", re.IGNORECASE)


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

    cmd: list[str] = ["cyan", "-i", str(input_ipa), "-o", str(output_ipa)]
    if tweak_paths:
        cmd += ["-f", *tweak_paths]
    opts = app.cyan
    if opts.display_name:
        cmd += ["-n", opts.display_name]
    if opts.strip_supported_devices:
        cmd.append("-u")
    if opts.strip_extensions:
        cmd.append("-e")
    if not opts.keep_bundle_id:
        new_id = opts.new_bundle_id or f"{app.bundle_id}.plus"
        cmd += ["-b", new_id]
    if opts.compression_level is not None:
        cmd += ["-c", str(opts.compression_level)]
    cmd.extend(opts.extra_args)
    return cmd


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
            env=child_env({"TMPDIR": f"{scratch}/"}),
        )
    except FileNotFoundError as e:
        raise CyanError(
            "cyan not found in PATH",
            hint=CYAN_INSTALL_COMMAND,
        ) from e
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0 or not output_ipa.exists():
        _raise_translated(combined, proc.returncode)

    return CyanResult(output_ipa=output_ipa, stdout=proc.stdout, stderr=proc.stderr)


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
