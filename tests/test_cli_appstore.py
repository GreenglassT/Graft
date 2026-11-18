"""The App Store verbs end to end through the CLI, against the fake tools.

`--appstore-status`, `--appstore-check`, `--appstore-get`, and `--provide-ipa`'s
new decrypt. `test_appstore.py` holds the module's own rules; this holds what a
caller of the CLI — the GUI, or a person — actually sees: the event stream, the
exit status, the hint on stderr, and what ends up in the cache.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from graft_ipa import appstore, itunes
from graft_ipa import cli as cli_mod
from graft_ipa.config import add_app, load_config
from tests.appstore_fakes import FakeTools, make_ipa


@pytest.fixture
def fakes(tmp_path: Path) -> FakeTools:
    return FakeTools(tmp_path / "tools")


@pytest.fixture
def config(temp_config_env: Path, fakes: FakeTools) -> Path:
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["appstore"] = fakes.appstore_section()
    temp_config_env.write_text(yaml.safe_dump(raw, sort_keys=False))
    return temp_config_env


@pytest.fixture
def cache_dir(config: Path) -> Path:
    return load_config(config).cache_dir


def _run(args: list[str]):
    return CliRunner().invoke(cli_mod.app, args)


def _events(result) -> list[dict]:
    return [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]


def _types(result) -> list[str]:
    return [e["type"] for e in _events(result)]


# --- --appstore-status -------------------------------------------------------


def test_status_is_one_event(config: Path) -> None:
    result = _run(["--json", "--appstore-status"])

    assert result.exit_code == 0, result.output
    (event,) = _events(result)
    assert event["type"] == "appstore-status"
    assert event["ipatool"]["signed_in"] is True
    assert event["unfairplay"]["enrolled"] is True


def test_status_for_a_person_is_not_json(config: Path, fakes: FakeTools) -> None:
    fakes.set(signed_in=False)

    result = _run(["--appstore-status"])

    assert result.exit_code == 0, result.output
    assert '{"type"' not in result.stdout
    assert "NOT signed in" in result.stdout
    assert "auth login" in result.stdout


# --- --appstore-check --------------------------------------------------------


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        itunes, "lookup_by_bundle_id",
        lambda bundle_id: itunes.ITunesApp(track_id=1, bundle_id=bundle_id, name="x", version="9.0"),
    )


def test_check_one_app(config: Path, store: None) -> None:
    # A second app, so "every app" and "the one asked for" differ.
    add_app(config, "bravo", {"bundle_id": "com.bravo.app", "tweaks": []})

    result = _run(["--json", "--appstore-check", "--app", "example"])

    assert result.exit_code == 0, result.output
    (event,) = _events(result)
    assert event["app"] == "example"
    assert event["store_version"] == "9.0"


def test_check_without_app_checks_every_app(config: Path, store: None) -> None:
    add_app(config, "bravo", {"bundle_id": "com.bravo.app", "tweaks": []})

    result = _run(["--json", "--appstore-check"])

    assert result.exit_code == 0, result.output
    assert sorted(e["app"] for e in _events(result)) == ["bravo", "example"]


def test_a_stray_flag_on_check_is_named(config: Path) -> None:
    result = _run(["--appstore-check", "--allow-unverified"])

    assert result.exit_code == 2
    assert "--allow-unverified is not valid with --appstore-check" in result.output


# --- --appstore-get ----------------------------------------------------------


def _staged(cache_dir: Path) -> list[Path]:
    return sorted(cache_dir.glob("*.ipa"))


def test_get_streams_its_stages_and_stages_the_decrypted_ipa(
    config: Path, cache_dir: Path
) -> None:
    result = _run(["--json", "--appstore-get", "--app", "example"])

    assert result.exit_code == 0, result.output
    events = _events(result)
    assert [(e["type"], e.get("stage")) for e in events] == [
        ("appstore-stage", "download"),
        ("appstore-stage", "decrypt"),
        ("appstore-decrypted", None),
        ("appstore-stage", "stage"),
        ("ipa-provided", None),
    ]
    provided = events[-1]
    assert provided["filename"] == "com.example.app_21.46.3.ipa"
    assert _staged(cache_dir) == [cache_dir / "com.example.app_21.46.3.ipa"]
    assert appstore.main_binary_encrypted(_staged(cache_dir)[0]) is False
    assert list((cache_dir / appstore.TMP_DIRNAME).iterdir()) == []


def test_get_needs_an_app(config: Path) -> None:
    result = _run(["--appstore-get"])

    assert result.exit_code == 2
    assert "--appstore-get requires --app <name>" in result.output


def test_a_failed_get_exits_20_with_the_hint_and_stages_nothing(
    config: Path, cache_dir: Path, fakes: FakeTools
) -> None:
    fakes.set(download_error="license is required")

    result = _run(["--json", "--appstore-get", "--app", "example"])

    assert result.exit_code == 20
    assert "tap Get" in result.stderr
    assert _staged(cache_dir) == []
    assert list((cache_dir / appstore.TMP_DIRNAME).iterdir()) == []


def test_unverified_is_refused_and_then_accepted_on_request(
    config: Path, cache_dir: Path, fakes: FakeTools
) -> None:
    fakes.set(plaintext_verified=False)

    refused = _run(["--json", "--appstore-get", "--app", "example"])
    assert refused.exit_code == 20
    assert _staged(cache_dir) == []

    accepted = _run(["--json", "--appstore-get", "--app", "example", "--allow-unverified"])
    assert accepted.exit_code == 0, accepted.output
    assert _staged(cache_dir)
    warnings = [e for e in _events(accepted) if e["type"] == "log" and e["source"] == "warning"]
    assert len(warnings) == 1 and "could not verify" in warnings[0]["line"]


# --- --provide-ipa -----------------------------------------------------------


def test_an_encrypted_drop_is_decrypted_before_it_is_staged(
    config: Path, cache_dir: Path, tmp_path: Path
) -> None:
    drop = make_ipa(tmp_path / "Downloads" / "Example.ipa", encrypted=True)

    result = _run(["--json", "--provide-ipa", str(drop), "--app", "example"])

    assert result.exit_code == 0, result.output
    assert _types(result) == [
        "appstore-stage", "appstore-decrypted", "appstore-stage", "ipa-provided",
    ]
    (staged,) = _staged(cache_dir)
    assert appstore.main_binary_encrypted(staged) is False
    assert sorted(p.name for p in drop.parent.iterdir()) == ["Example.ipa"], (
        "nothing may be left beside the user's own file"
    )


def test_an_encrypted_drop_without_enrollment_is_refused_and_not_cached(
    config: Path, cache_dir: Path, fakes: FakeTools, tmp_path: Path
) -> None:
    """The defect this replaced: an encrypted drop was cached, and the build
    then died at stage 2.5 with nothing pointing at the cause."""
    (fakes.models / "enrollment.bin").unlink()
    drop = make_ipa(tmp_path / "Example.ipa", encrypted=True)

    result = _run(["--json", "--provide-ipa", str(drop), "--app", "example"])

    assert result.exit_code == 20
    assert "setup --app" in result.stderr
    assert _staged(cache_dir) == []


def test_a_decrypted_drop_is_copied_byte_for_byte_and_says_nothing_new(
    config: Path, cache_dir: Path, fakes: FakeTools, tmp_path: Path
) -> None:
    drop = make_ipa(tmp_path / "Example.ipa", encrypted=False, marker="mine")

    result = _run(["--json", "--provide-ipa", str(drop), "--app", "example"])

    assert result.exit_code == 0, result.output
    assert _types(result) == ["ipa-provided"]
    (staged,) = _staged(cache_dir)
    assert staged.read_bytes() == drop.read_bytes()
    assert fakes.calls("unfairplay") == []


def test_a_drop_for_the_wrong_app_fails_before_anything_is_decrypted(
    config: Path, fakes: FakeTools, tmp_path: Path
) -> None:
    drop = make_ipa(tmp_path / "Bravo.ipa", bundle_id="com.bravo.app", encrypted=True)

    result = _run(["--json", "--provide-ipa", str(drop), "--app", "example"])

    assert result.exit_code == 14
    assert fakes.calls("unfairplay") == []
