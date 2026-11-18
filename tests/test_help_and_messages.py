"""Help text and messages that had drifted from what the code does."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa.build_log import BuildLog
from graft_ipa.cli import app as cli_app
from graft_ipa.config import load_config
from graft_ipa.errors import GraftError

PKG = Path(cli_mod.__file__).parent


def _help_text() -> str:
    result = CliRunner().invoke(cli_app, ["--help"], env={"COLUMNS": "400", "NO_COLOR": "1"})
    assert result.exit_code == 0, result.output
    return re.sub(r"[\s│]+", " ", result.output)


def test_decrypt_backend_help_does_not_claim_bagbak_needs_no_ssh() -> None:
    text = _help_text()
    assert "bagbak (no SSH" not in text
    assert "--decrypt-backend" in text
    after = text.split("--decrypt-backend", 1)[1][:400]
    assert "bagbak" in after
    assert "SSH" in after


def test_set_ssh_password_help_names_both_backends() -> None:
    text = _help_text()
    after = text.split("--set-ssh-password", 1)[1][:400]
    assert "frida-ios-dump" in after
    assert "bagbak" in after


def test_build_log_hint_blames_the_missing_json_flag(temp_config_env: Path) -> None:
    cfg = load_config(temp_config_env)
    build_id = BuildLog(cfg.state_dir / "log.sqlite").record(
        app="example", bundle_id="com.example.app", version="1", tweaks=[],
        ipa_path=None, signing_team=None, udid=None, success=True, log_path=None,
    )
    with pytest.raises(GraftError, match="no captured log") as exc:
        cli_mod._do_build_log(cfg, build_id)
    hint = exc.value.hint or ""
    assert "--json" in hint
    assert "Terminal" in hint
    assert "log-persistence" not in hint


def test_scratch_dirs_are_prefixed_graft() -> None:
    source = "".join(p.read_text() for p in PKG.glob("*.py"))
    assert "tb-cryptid-" not in source
    assert "tweak-decrypt-" not in source
    assert 'prefix="graft-cryptid-"' in source
    assert 'prefix="graft-decrypt-"' in source
