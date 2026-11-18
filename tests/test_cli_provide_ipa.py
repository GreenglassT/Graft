from __future__ import annotations

import json
import plistlib
import zipfile
from io import StringIO
from pathlib import Path

import pytest

from graft_ipa.config import load_config
from graft_ipa.json_output import JsonEmitter


def _make_ipa(path: Path, *, bundle_id: str, version: str, app_name: str = "X") -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            f"Payload/{app_name}.app/Info.plist",
            plistlib.dumps({
                "CFBundleIdentifier": bundle_id,
                "CFBundleShortVersionString": version,
                "CFBundleVersion": version,
                "CFBundleName": app_name,
            }),
        )
        zf.writestr(f"Payload/{app_name}.app/{app_name}", b"\x00\x00")
    return path


def test_provide_ipa_json_emits_event_and_copies_file(
    temp_config_env: Path, tmp_path: Path
) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    # The default config has a `example` app with bundle_id com.example.app.
    src = _make_ipa(
        tmp_path / "src.ipa",
        bundle_id="com.example.app",
        version="21.16.2",
    )
    buf = StringIO()
    cli_mod._do_provide_ipa(
        cfg,
        app_name="example",
        source_path=src,
        emitter=JsonEmitter(stream=buf),
    )
    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    ev = next(e for e in events if e["type"] == "ipa-provided")
    assert ev["app"] == "example"
    assert ev["bundle_id"] == "com.example.app"
    assert ev["version"] == "21.16.2"
    assert ev["filename"] == "com.example.app_21.16.2.ipa"
    assert ev["size_bytes"] > 0
    assert Path(ev["path"]).exists()


def test_provide_ipa_unknown_app_raises_configerror(
    temp_config_env: Path, tmp_path: Path
) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa.errors import ConfigError

    cfg = load_config(temp_config_env)
    src = _make_ipa(tmp_path / "x.ipa", bundle_id="com.x.y", version="1.0")
    with pytest.raises(ConfigError):
        cli_mod._do_provide_ipa(
            cfg, app_name="not-a-real-app", source_path=src,
            emitter=JsonEmitter(stream=StringIO()),
        )


def test_provide_ipa_bundle_id_mismatch_propagates(
    temp_config_env: Path, tmp_path: Path
) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa.errors import IPAValidationError

    cfg = load_config(temp_config_env)
    # `example` expects com.example.app — give it Bravo instead.
    src = _make_ipa(tmp_path / "x.ipa", bundle_id="com.bravo.app", version="1.0")
    with pytest.raises(IPAValidationError):
        cli_mod._do_provide_ipa(
            cfg, app_name="example", source_path=src,
            emitter=JsonEmitter(stream=StringIO()),
        )


def test_cached_ipa_json_reports_exists_true(
    temp_config_env: Path, tmp_path: Path
) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    # Stage one first so find_in_cache has something to return.
    src = _make_ipa(
        tmp_path / "src.ipa",
        bundle_id="com.example.app",
        version="21.16.2",
    )
    cli_mod._do_provide_ipa(
        cfg, app_name="example", source_path=src,
        emitter=JsonEmitter(stream=StringIO()),
    )

    buf = StringIO()
    cli_mod._do_cached_ipa(cfg, app_name="example", emitter=JsonEmitter(stream=buf))
    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    ev = next(e for e in events if e["type"] == "cached-ipa")
    assert ev["app"] == "example"
    assert ev["exists"] is True
    assert ev["bundle_id"] == "com.example.app"
    assert ev["version"] == "21.16.2"
    assert ev["filename"] == "com.example.app_21.16.2.ipa"
    assert ev["size_bytes"] > 0


def test_cached_ipa_json_reports_exists_false_when_empty(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    buf = StringIO()
    cli_mod._do_cached_ipa(cfg, app_name="example", emitter=JsonEmitter(stream=buf))
    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    ev = next(e for e in events if e["type"] == "cached-ipa")
    assert ev["app"] == "example"
    assert ev["exists"] is False
    assert "filename" not in ev
    assert "path" not in ev


def test_cached_ipa_unknown_app_raises_configerror(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa.errors import ConfigError

    cfg = load_config(temp_config_env)
    with pytest.raises(ConfigError):
        cli_mod._do_cached_ipa(
            cfg, app_name="not-a-real-app", emitter=JsonEmitter(stream=StringIO())
        )
