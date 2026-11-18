from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def temp_env(temp_config_env: Path) -> Path:
    tweaks_dir = temp_config_env.parent / "tweaks"
    tweaks_dir.mkdir(parents=True)
    (tweaks_dir / "a-tweak.deb").write_bytes(b"x" * 100)
    (tweaks_dir / "some-tweak.deb").write_bytes(b"x" * 200)
    (tweaks_dir / "unused.deb").write_bytes(b"x" * 50)
    (tweaks_dir / "libexample.deb").write_bytes(b"x" * 75)
    (tweaks_dir / "c-tweak.deb").write_bytes(b"x" * 125)
    return temp_config_env


def test_tweaks_json_emits_records_with_used_by(temp_env: Path) -> None:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(temp_env.parent.parent.parent),
        "XDG_CONFIG_HOME": str(temp_env.parent.parent),
        "XDG_CACHE_HOME": str(temp_env.parent.parent.parent / "cache"),
        "XDG_STATE_HOME": str(temp_env.parent.parent.parent / "state"),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "graft_ipa.cli", "--json", "--tweaks"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(Path(__file__).parent.parent),
    )
    assert proc.returncode == 0, proc.stderr
    lines = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    tweaks = {t["filename"]: t for t in lines if t["type"] == "tweak"}
    # The default config wires example with a-tweak + sometweak + c-tweak + libexample
    assert "a-tweak.deb" in tweaks
    assert tweaks["a-tweak.deb"]["used_by"] == ["example"]
    assert tweaks["a-tweak.deb"]["size_bytes"] == 100
    # unused.deb has no consumers
    assert "unused.deb" in tweaks
    assert tweaks["unused.deb"]["used_by"] == []


def test_tweaks_json_reports_used_by_for_path_form_config_entry(
    temp_env: Path,
) -> None:
    """Config.tweak_path explicitly supports absolute paths in `tweaks:`
    ("advanced users"). _collect_tweak_records used to key its consumers map
    on the raw config string and look it up by bare entry.name, so a
    path-form entry never matched its file in tweaks_dir and always reported
    used_by: [] — making an in-use tweak look orphaned to the GUI."""
    from graft_ipa.config import load_config, set_app_tweaks

    cfg = load_config(temp_env)
    pathform = cfg.tweaks_dir / "pathform.deb"
    pathform.write_bytes(b"x" * 10)
    set_app_tweaks(temp_env, "example", [str(pathform)])

    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(temp_env.parent.parent.parent),
        "XDG_CONFIG_HOME": str(temp_env.parent.parent),
        "XDG_CACHE_HOME": str(temp_env.parent.parent.parent / "cache"),
        "XDG_STATE_HOME": str(temp_env.parent.parent.parent / "state"),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "graft_ipa.cli", "--json", "--tweaks"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(Path(__file__).parent.parent),
    )
    assert proc.returncode == 0, proc.stderr
    lines = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    tweaks = {t["filename"]: t for t in lines if t["type"] == "tweak"}
    assert "pathform.deb" in tweaks
    assert tweaks["pathform.deb"]["used_by"] == ["example"]


def _filesystem_is_case_insensitive(directory: Path) -> bool:
    """True if `directory` folds case, like the default macOS/APFS volume."""
    probe = directory / "case_probe_a1b2.tmp"
    probe.write_bytes(b"\x00")
    try:
        return probe.with_name(probe.name.upper()).is_file()
    finally:
        probe.unlink()


def _tweaks_json(temp_env: Path) -> dict[str, dict]:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(temp_env.parent.parent.parent),
        "XDG_CONFIG_HOME": str(temp_env.parent.parent),
        "XDG_CACHE_HOME": str(temp_env.parent.parent.parent / "cache"),
        "XDG_STATE_HOME": str(temp_env.parent.parent.parent / "state"),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "graft_ipa.cli", "--json", "--tweaks"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(Path(__file__).parent.parent),
    )
    assert proc.returncode == 0, proc.stderr
    lines = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    return {t["filename"]: t for t in lines if t["type"] == "tweak"}


def test_tweaks_json_reports_used_by_for_mis_cased_config_entry(
    temp_env: Path,
) -> None:
    """`--attach-tweak SOME-TWEAK.deb` succeeds on case-insensitive APFS (attach only
    checks `src.is_file()`) and writes the literal "SOME-TWEAK.deb". The consumers map
    keyed on that raw string, so some-tweak.deb reported used_by: [] — the GUI showed
    an in-use tweak as an orphan and offered to delete it."""
    from graft_ipa.config import load_config, set_app_tweaks

    cfg = load_config(temp_env)
    if not _filesystem_is_case_insensitive(cfg.tweaks_dir):
        pytest.skip("filesystem backing tweaks_dir is case-sensitive; probe not meaningful here")

    set_app_tweaks(temp_env, "example", ["SOME-TWEAK.deb"])

    tweaks = _tweaks_json(temp_env)
    assert "some-tweak.deb" in tweaks
    assert tweaks["some-tweak.deb"]["used_by"] == ["example"]


def test_tweaks_json_dangling_entry_does_not_claim_another_file(
    temp_env: Path,
) -> None:
    """tweak_key falls back to a path string for a config entry whose file is
    missing. That fallback must never collide with a real file's inode
    identity, or a dangling entry would make an unrelated orphan look used."""
    from graft_ipa.config import set_app_tweaks

    set_app_tweaks(temp_env, "example", ["ghost.deb"])

    tweaks = _tweaks_json(temp_env)
    assert "ghost.deb" not in tweaks, "a dangling entry has no file to list"
    assert tweaks["unused.deb"]["used_by"] == []
    assert tweaks["some-tweak.deb"]["used_by"] == []
