from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from graft_ipa.canister import Package
from graft_ipa.config import load_config
from graft_ipa.json_output import JsonEmitter


@pytest.fixture
def stub_packages() -> list[Package]:
    return [
        Package(
            package_id="com.example.first",
            name="First",
            version="1.0",
            description="first tweak",
            author="alice",
            section="Tweaks",
            repository_id="havoc",
            package_filename="first.deb",
            package_size=1234,
            sha256="abc",
            price="Free",
            depiction=None,
        ),
        Package(
            package_id="com.example.second",
            name="Second",
            version="2.1",
            description="second tweak",
            author="bob",
            section="Tweaks",
            repository_id="chariz",
            package_filename="second.deb",
            package_size=5678,
            sha256="def",
            price="Free",
            depiction=None,
        ),
    ]


def test_search_json_emits_results_and_terminator(
    temp_config_env: Path, stub_packages: list[Package], capsys
) -> None:
    from graft_ipa import cli as cli_mod

    with patch.object(cli_mod.canister, "search", return_value=stub_packages):
        cli_mod._do_search("anything", emitter=JsonEmitter())

    events = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()
    ]
    results = [e for e in events if e["type"] == "canister-result"]
    assert len(results) == 2
    assert results[0]["package_id"] == "com.example.first"
    assert results[0]["repository_id"] == "havoc"
    assert results[1]["package_id"] == "com.example.second"
    enders = [e for e in events if e["type"] == "canister-search-end"]
    assert enders == [{"type": "canister-search-end", "count": 2}]


def test_fetch_json_emits_start_and_success(
    temp_config_env: Path, tmp_path: Path
) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    fake_deb = tmp_path / "out.deb"
    fake_deb.write_bytes(b"x" * 50)

    with patch.object(cli_mod.canister, "fetch_deb", return_value=fake_deb):
        from io import StringIO

        buf = StringIO()
        cli_mod._do_fetch(cfg, "com.example.first", emitter=JsonEmitter(stream=buf))

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    assert events[0] == {"type": "canister-fetch-start", "target": "com.example.first"}
    end = next(e for e in events if e["type"] == "canister-fetch-end")
    assert end["ok"] is True
    assert end["filename"] == "out.deb"
    assert end["size_bytes"] == 50


def test_fetch_json_passes_expected_sha(
    temp_config_env: Path, tmp_path: Path
) -> None:
    from io import StringIO
    from unittest.mock import MagicMock

    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    fake_deb = tmp_path / "a.deb"
    fake_deb.write_bytes(b"y" * 30)

    mock_fetch = MagicMock(return_value=fake_deb)
    with patch.object(cli_mod.canister, "fetch_deb", mock_fetch):
        buf = StringIO()
        cli_mod._do_fetch(
            cfg,
            "https://x.example.com/a.deb",
            emitter=JsonEmitter(stream=buf),
            expected_sha="abc123",
        )

    # _do_fetch calls canister.fetch_deb(target, cfg.tweaks_dir, expected_sha)
    assert mock_fetch.call_args[0][2] == "abc123"
