from __future__ import annotations

import json
from pathlib import Path

from graft_ipa.build_log import BuildLog
from graft_ipa.config import load_config
from graft_ipa.json_output import JsonEmitter


def test_builds_json_emits_records(temp_config_env: Path, tmp_path: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    # Pre-populate the build log with a couple rows
    log = BuildLog(cfg.state_dir / "log.sqlite")
    log.record(
        app="example", bundle_id="com.example.app", version="21.16.2",
        tweaks=["a-tweak.deb", "some-tweak.deb"],
        ipa_path=tmp_path / "example.ipa", signing_team="ABCDE12345",
        udid="00008130-x", success=True,
    )
    log.record(
        app="delta", bundle_id="com.delta.app", version="44.7.0",
        tweaks=["EchoV1.73.deb"],
        ipa_path=None, signing_team=None,
        udid=None, success=False, error="cyan failed",
    )

    from io import StringIO
    buf = StringIO()
    cli_mod._do_builds(cfg, app=None, limit=10, emitter=JsonEmitter(stream=buf))

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    records = [e for e in events if e["type"] == "build-record"]
    assert len(records) == 2
    # Most recent first
    assert records[0]["app"] == "delta"
    assert records[0]["success"] is False
    assert records[0]["error"] == "cyan failed"
    assert records[1]["app"] == "example"
    assert records[1]["success"] is True
    assert records[1]["tweaks"] == ["a-tweak.deb", "some-tweak.deb"]


def test_builds_json_filter_by_app(temp_config_env: Path, tmp_path: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    log.record(app="a", bundle_id="b", version="1", tweaks=[],
               ipa_path=None, signing_team=None, udid=None, success=True)
    log.record(app="b", bundle_id="b", version="1", tweaks=[],
               ipa_path=None, signing_team=None, udid=None, success=True)

    from io import StringIO
    buf = StringIO()
    cli_mod._do_builds(cfg, app="a", limit=10, emitter=JsonEmitter(stream=buf))

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    records = [e for e in events if e["type"] == "build-record"]
    assert len(records) == 1
    assert records[0]["app"] == "a"
