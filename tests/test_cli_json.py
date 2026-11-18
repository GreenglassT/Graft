from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def test_list_json_emits_app_records(temp_config_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(temp_config_env.parent.parent.parent),
        "XDG_CONFIG_HOME": str(temp_config_env.parent.parent),
        "XDG_CACHE_HOME": str(temp_config_env.parent.parent.parent / "cache"),
        "XDG_STATE_HOME": str(temp_config_env.parent.parent.parent / "state"),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "graft_ipa.cli", "--json", "--list"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(Path(__file__).parent.parent),
    )
    assert proc.returncode == 0, proc.stderr
    lines = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    apps = [e for e in lines if e["type"] == "app"]
    assert len(apps) == 1
    assert apps[0]["name"] == "example"
    assert apps[0]["bundle_id"] == "com.example.app"
    assert "tweaks" in apps[0]
