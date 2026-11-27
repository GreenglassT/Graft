"""A Get and the store the app was found in, and Apple's own apps.

ipatool downloads from the signed-in Apple ID's own storefront, whatever
`store_country` says, and Graft can't see which one that is. So a Get that
fails for an app found in another store says which store that was. Apple's own
apps (`com.apple.*`) come first in generic searches; a Get of one is refused
before ipatool runs, with the reason.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa import itunes
from tests.appstore_fakes import FakeTools, itunes_offline


@pytest.fixture(autouse=True)
def _no_price_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(itunes, "lookup_by_bundle_id", itunes_offline)


@pytest.fixture
def fakes(tmp_path: Path) -> FakeTools:
    return FakeTools(tmp_path / "tools")


@pytest.fixture
def config(temp_config_env: Path, fakes: FakeTools) -> Path:
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["appstore"] = fakes.appstore_section()
    temp_config_env.write_text(yaml.safe_dump(raw, sort_keys=False))
    return temp_config_env


def _set_app(config: Path, **fields: str) -> None:
    raw = yaml.safe_load(config.read_text())
    raw["apps"]["example"].update(fields)
    config.write_text(yaml.safe_dump(raw, sort_keys=False))


def _run(args: list[str]):
    return CliRunner().invoke(cli_mod.app, args)


def _words(result) -> str:
    return " ".join(result.output.split())


# --- the store it was found in ------------------------------------------------------


def test_a_failed_get_of_an_app_found_in_another_store_names_that_store(
    config: Path, fakes: FakeTools
) -> None:
    _set_app(config, store_country="GB")
    fakes.set(download_error="failed to resolve the app: not found")

    result = _run(["--appstore-get", "--app", "example"])

    assert result.exit_code != 0
    words = _words(result)
    assert "ipatool couldn't download com.example.app" in words
    assert "this app was found in the GB App Store; your Apple ID may be in a different one" in words


def test_a_failed_get_with_no_store_keeps_ipatools_own_hint(config: Path, fakes: FakeTools) -> None:
    fakes.set(download_error="failed to resolve the app: not found")

    result = _run(["--appstore-get", "--app", "example"])

    assert result.exit_code != 0
    assert "that is ipatool's own error" in _words(result)
    assert "App Store; your Apple ID" not in _words(result)


def test_a_get_that_failed_for_another_reason_says_nothing_of_the_store(
    config: Path, fakes: FakeTools
) -> None:
    """Signed out after `auth info` passed (an expired token): the store is not why."""
    _set_app(config, store_country="GB")
    fakes.set(download_error="password token is expired")

    result = _run(["--appstore-get", "--app", "example"])

    assert result.exit_code != 0
    assert "sign-in has expired" in _words(result)
    assert "GB App Store" not in _words(result)


# --- Apple's own apps ------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["--appstore-get", "--appstore-claim"])
def test_a_get_of_apples_own_app_is_refused_before_ipatool_downloads(
    config: Path, fakes: FakeTools, verb: str
) -> None:
    _set_app(config, bundle_id="com.apple.example")

    result = _run(["--json", verb, "--app", "example"])

    assert result.exit_code == 20, result.output
    words = _words(result)
    assert "com.apple.example is one of Apple's own apps" in words
    ran = [call["argv"][:1] for call in fakes.calls("ipatool")]
    assert ["download"] not in ran and ["purchase"] not in ran
    assert not any(line.startswith("{") and json.loads(line)["type"] == "appstore-stage"
                   for line in result.stdout.splitlines())


def test_apples_own_apps_are_told_apart_by_bundle_id_in_any_case() -> None:
    from graft_ipa import appstore

    assert appstore.apples_own("com.apple.mobilenotes")
    assert appstore.apples_own("COM.Apple.Pages")
    assert not appstore.apples_own("com.applessoftware.app")
    assert not appstore.apples_own("com.example.apple")
