"""A failed `--json --fetch` exits non-zero, like every other verb's failure."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa.errors import GraftError


def test_json_fetch_failure_still_emits_the_end_event_and_exits_nonzero(
    temp_config_env: Path,
) -> None:
    runner = CliRunner()
    with patch.object(
        cli_mod.canister, "fetch_deb", side_effect=GraftError("no such package")
    ):
        result = runner.invoke(
            cli_mod.app, ["--config", str(temp_config_env), "--json", "--fetch", "com.example.nope"]
        )

    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    end = next(e for e in events if e["type"] == "canister-fetch-end")
    assert end["ok"] is False
    assert "no such package" in end["error"]
    assert result.exit_code != 0


def test_json_fetch_success_exits_zero(temp_config_env: Path, tmp_path: Path) -> None:
    deb = tmp_path / "out.deb"
    deb.write_bytes(b"x")
    with patch.object(cli_mod.canister, "fetch_deb", return_value=deb):
        result = CliRunner().invoke(
            cli_mod.app, ["--config", str(temp_config_env), "--json", "--fetch", "com.example.ok"]
        )
    assert result.exit_code == 0, result.output
