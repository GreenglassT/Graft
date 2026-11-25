"""Older App Store versions, end to end through the CLI, against the fake tools.

`--appstore-versions` lists the versions the App Store serves this Apple ID
for an app, newest first, reading each one's details (one ipatool run apiece)
only for the page asked for, and remembering them. `--appstore-get
--version-id` gets one of them and holds the app at it: its
`preferred_version` pins that version, so builds use it and `--appstore-check`
doesn't call the newer one an update. A plain get (Get latest) lets go.

The stand-in ipatool prints exactly what ipatool 2.8.0's source logs for each
command (`tests/appstore_fakes.py`). The real one never runs here.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from graft_ipa import appstore, ipatool, itunes, keychain
from graft_ipa import cli as cli_mod
from graft_ipa.config import load_config, set_app_options
from graft_ipa.errors import AppStoreError
from tests.appstore_fakes import FakeTools, itunes_offline, make_ipa


@pytest.fixture(autouse=True)
def _no_price_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(itunes, "lookup_by_bundle_id", itunes_offline)


#: Apple's order, which isn't newest first, and one out of place.
IDS = ["800100", "800300", "800200", "800400"]
META = {
    "800400": {"version": "1.4.0", "date": "2026-10-01T07:00:00Z"},
    "800300": {"version": "1.3.0", "date": "2026-09-01T07:00:00Z"},
    "800200": {"version": "1.2.0", "date": "2026-08-01T07:00:00Z"},
    "800100": {"version": "1.1.0", "date": "2026-07-01T07:00:00Z"},
}


@pytest.fixture
def fakes(tmp_path: Path) -> FakeTools:
    return FakeTools(tmp_path / "tools").set(versions=IDS, version_metadata=META)


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


def _events(result, kind: str | None = None) -> list[dict]:
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    return [e for e in rows if kind is None or e["type"] == kind]


def _metadata_calls(fakes: FakeTools) -> list[str]:
    return [
        c["argv"][c["argv"].index("--external-version-id") + 1]
        for c in fakes.calls("ipatool") if c["argv"][:1] == ["get-version-metadata"]
    ]


def _pin(config: Path) -> str:
    return load_config(config).get_app("example").preferred_version


# --- --appstore-versions -------------------------------------------------------


def test_versions_are_newest_first_with_details_for_the_page_asked(config: Path, fakes: FakeTools) -> None:
    result = _run(["--json", "--appstore-versions", "--app", "example", "--limit", "2"])

    assert result.exit_code == 0, result.output
    releases = _events(result, "appstore-release")
    assert [(r["external_id"], r["version"], r["release_date"]) for r in releases] == [
        ("800400", "1.4.0", "2026-10-01T07:00:00Z"),
        ("800300", "1.3.0", "2026-09-01T07:00:00Z"),
        ("800200", None, None),
        ("800100", None, None),
    ]
    assert [r["newest"] for r in releases] == [True, False, False, False]
    assert [r["index"] for r in releases] == [0, 1, 2, 3]
    assert all(r["error"] is None and r["app"] == "example" for r in releases)
    (end,) = _events(result, "appstore-releases-end")
    assert end == {
        "type": "appstore-releases-end", "app": "example", "bundle_id": "com.example.app",
        "count": 4, "offset": 0, "limit": 2, "held_version": None,
    }
    assert _events(result)[-1] == end, "the sentinel comes last"
    listed = [c["argv"] for c in fakes.calls("ipatool") if c["argv"][:1] == ["list-versions"]]
    assert listed == [["list-versions", "-b", "com.example.app", "--format", "json", "--non-interactive"]]
    assert [c["argv"] for c in fakes.calls("ipatool") if c["argv"][:1] == ["get-version-metadata"]] == [
        ["get-version-metadata", "-b", "com.example.app", "--external-version-id", ident,
         "--format", "json", "--non-interactive"]
        for ident in ("800400", "800300")
    ]


def test_details_are_remembered_and_the_next_page_reads_only_what_is_new(
    config: Path, fakes: FakeTools
) -> None:
    first = _run(["--json", "--appstore-versions", "--app", "example", "--limit", "2"])
    assert first.exit_code == 0, first.output

    second = _run(["--json", "--appstore-versions", "--app", "example", "--offset", "2", "--limit", "1"])

    assert second.exit_code == 0, second.output
    assert _metadata_calls(fakes) == ["800400", "800300", "800200"], "one ipatool run per version, ever"
    assert [r["version"] for r in _events(second, "appstore-release")] == ["1.4.0", "1.3.0", "1.2.0", None]
    assert (load_config(config).cache_dir / appstore.VERSIONS_CACHE).is_file()


def test_the_default_page_is_ten(config: Path, fakes: FakeTools) -> None:
    many = [str(900000 + i) for i in range(12)]
    fakes.set(versions=many, version_metadata={i: {"version": f"2.{i[-2:]}", "date": "2026-01-01T00:00:00Z"}
                                               for i in many})

    result = _run(["--json", "--appstore-versions", "--app", "example"])

    assert result.exit_code == 0, result.output
    assert len(_metadata_calls(fakes)) == appstore.VERSIONS_LIMIT == 10
    assert _events(result, "appstore-releases-end")[0]["limit"] == 10


def test_a_version_apple_wont_describe_is_said_on_its_own_row(config: Path, fakes: FakeTools) -> None:
    fakes.set(version_metadata={k: v for k, v in META.items() if k != "800300"})

    result = _run(["--json", "--appstore-versions", "--app", "example", "--limit", "3"])

    assert result.exit_code == 0, result.output
    rows = {r["external_id"]: r for r in _events(result, "appstore-release")}
    assert rows["800300"]["version"] is None
    assert "no longer available" in rows["800300"]["error"]
    assert rows["800400"]["version"] == "1.4.0" and rows["800200"]["version"] == "1.2.0"
    assert rows["800100"]["error"] is None, "not asked is not a failure"


def test_a_failed_read_is_asked_again_next_time(config: Path, fakes: FakeTools) -> None:
    fakes.set(version_metadata={k: v for k, v in META.items() if k != "800400"})
    assert _run(["--json", "--appstore-versions", "--app", "example", "--limit", "1"]).exit_code == 0
    fakes.set(version_metadata=META)

    again = _run(["--json", "--appstore-versions", "--app", "example", "--limit", "1"])

    assert _metadata_calls(fakes) == ["800400", "800400"]
    assert _events(again, "appstore-release")[0]["version"] == "1.4.0"


def test_a_held_and_a_staged_version_are_marked(config: Path, fakes: FakeTools, cache_dir: Path) -> None:
    set_app_options(config, "example", preferred_version="1.3.0")
    make_ipa(cache_dir / "com.example.app_1.3.0.ipa", version="1.3.0", encrypted=False)

    result = _run(["--json", "--appstore-versions", "--app", "example", "--limit", "2"])

    rows = {r["external_id"]: r for r in _events(result, "appstore-release")}
    assert rows["800300"]["staged"] is True
    assert rows["800400"]["staged"] is False
    assert rows["800100"]["staged"] is False, "a version nobody has read can't be called staged"
    assert _events(result, "appstore-releases-end")[0]["held_version"] == "1.3.0"


def test_a_garbage_cache_is_no_cache(config: Path, fakes: FakeTools, cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / appstore.VERSIONS_CACHE).write_text("{not json")

    result = _run(["--json", "--appstore-versions", "--app", "example", "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert _events(result, "appstore-release")[0]["version"] == "1.4.0"


def test_ids_that_are_not_numbers_are_not_listed(config: Path, fakes: FakeTools) -> None:
    """Only a numeric id can come back as `--version-id`."""
    fakes.set(versions=["800400", "--external-version-id", "", "800300"])

    result = _run(["--json", "--appstore-versions", "--app", "example", "--limit", "0"])

    assert result.exit_code == 0, result.output
    assert [r["external_id"] for r in _events(result, "appstore-release")] == ["800400", "800300"]


def test_listing_for_a_person_reads_as_a_table(config: Path, fakes: FakeTools) -> None:
    result = _run(["--appstore-versions", "--app", "example", "--limit", "2"])

    assert result.exit_code == 0, result.output
    text = " ".join(result.stdout.split())
    assert '{"type"' not in result.stdout
    assert "1.4.0 2026-10-01 800400 (newest)" in text
    assert "--offset 2" in text
    assert "--appstore-get --app example --version-id" in text


def test_not_signed_in_lists_nothing(config: Path, fakes: FakeTools) -> None:
    fakes.set(signed_in=False)

    result = _run(["--json", "--appstore-versions", "--app", "example"])

    assert result.exit_code == 20
    assert "not signed in" in " ".join(result.stderr.split())
    assert not [c for c in fakes.calls("ipatool") if c["argv"][:1] == ["list-versions"]]
    assert _events(result) == []


def test_an_app_the_apple_id_does_not_have_is_the_needs_license_refusal(
    config: Path, fakes: FakeTools
) -> None:
    fakes.set(list_versions_error="license is required")

    result = _run(["--json", "--appstore-versions", "--app", "example"])

    assert result.exit_code == 20
    assert [e["reason"] for e in _events(result, "appstore-refused")] == [appstore.REFUSED_NEEDS_LICENSE]
    assert _metadata_calls(fakes) == []


def test_a_locked_keychain_runs_nothing(config: Path, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keychain, "login_keychain_locked", lambda: True)

    result = _run(["--json", "--appstore-versions", "--app", "example"])

    assert result.exit_code == 20
    assert ipatool.KEYCHAIN_LOCKED in " ".join(result.stderr.split())
    assert [c["argv"] for c in fakes.calls("ipatool")] == [["--version"]]


def test_a_sign_in_that_expires_mid_list_stops_it(config: Path, fakes: FakeTools) -> None:
    """Not one row per version saying the same thing: the account is the
    problem, so the listing stops and says so."""
    fakes.set(metadata_error="failed to get account: item could not be found in the keyring")

    result = _run(["--json", "--appstore-versions", "--app", "example"])

    assert result.exit_code == 20
    assert "not signed in" in " ".join(result.stderr.split())
    assert _metadata_calls(fakes) == ["800400"]
    assert _events(result, "appstore-releases-end") == []


def test_a_failed_list_says_what_ipatool_said(config: Path, fakes: FakeTools) -> None:
    fakes.set(list_versions_error="received error: Your request could not be completed.")

    result = _run(["--json", "--appstore-versions", "--app", "example"])

    assert result.exit_code == 20
    words = " ".join(result.stderr.split())
    assert "couldn't list the versions of com.example.app" in words
    assert "Your request could not be completed" in words


def test_apples_own_apps_are_not_listed(config: Path, fakes: FakeTools) -> None:
    raw = yaml.safe_load(config.read_text())
    raw["apps"]["example"]["bundle_id"] = "com.apple.mobilenotes"
    config.write_text(yaml.safe_dump(raw, sort_keys=False))

    result = _run(["--json", "--appstore-versions", "--app", "example"])

    assert result.exit_code == 20
    assert fakes.calls("ipatool") == []


@pytest.mark.parametrize(("args", "words"), [
    (["--appstore-versions"], "--appstore-versions requires --app <name>"),
    (["--appstore-versions", "--app", "example", "--limit", "51"], "--limit"),
    (["--appstore-versions", "--app", "example", "--limit", "-1"], "--limit"),
    (["--appstore-versions", "--app", "example", "--offset", "-1"], "--offset"),
    (["--appstore-versions", "--app", "example", "--again"], "--again is not valid with --appstore-versions"),
    (["--appstore-search", "x", "--offset", "3"], "--offset is not valid with --appstore-search"),
])
def test_a_bad_listing_is_refused_before_ipatool_runs(
    config: Path, fakes: FakeTools, args: list[str], words: str
) -> None:
    result = _run(["--json", *args])

    assert result.exit_code == 2, result.output
    assert words in " ".join(result.output.split())
    assert fakes.calls("ipatool") == []


# --- --appstore-get --version-id -------------------------------------------------


@pytest.fixture
def old_build(fakes: FakeTools, tmp_path: Path) -> FakeTools:
    """UnfairPlay hands back version 1.2.0, as a get of 800200 would."""
    return fakes.set(decrypted_ipa=str(make_ipa(tmp_path / "fixtures" / "old.ipa", version="1.2.0", encrypted=False)))


def test_a_get_by_version_downloads_that_version_and_holds_the_app_at_it(
    config: Path, old_build: FakeTools, cache_dir: Path
) -> None:
    result = _run(["--json", "--appstore-get", "--app", "example", "--version-id", "800200"])

    assert result.exit_code == 0, result.output
    (download,) = [c["argv"] for c in old_build.calls("ipatool") if c["argv"][:1] == ["download"]]
    assert download[download.index("--external-version-id") + 1] == "800200"
    assert [e["type"] for e in _events(result)][-2:] == ["ipa-provided", "appstore-hold"]
    assert _events(result, "appstore-hold") == [{
        "type": "appstore-hold", "app": "example", "bundle_id": "com.example.app",
        "version": "1.2.0", "previous": None,
    }]
    assert _pin(config) == "1.2.0"
    assert (cache_dir / "com.example.app_1.2.0.ipa").is_file()


def test_a_plain_get_does_not_name_a_version(config: Path, fakes: FakeTools) -> None:
    assert _run(["--json", "--appstore-get", "--app", "example"]).exit_code == 0
    (download,) = [c["argv"] for c in fakes.calls("ipatool") if c["argv"][:1] == ["download"]]
    assert "--external-version-id" not in download


def test_get_latest_lets_go_of_the_hold(config: Path, fakes: FakeTools) -> None:
    set_app_options(config, "example", preferred_version="1.2.0")

    result = _run(["--json", "--appstore-get", "--app", "example"])

    assert result.exit_code == 0, result.output
    assert _events(result, "appstore-hold") == [{
        "type": "appstore-hold", "app": "example", "bundle_id": "com.example.app",
        "version": None, "previous": "1.2.0",
    }]
    assert _pin(config) == "latest"


def test_get_latest_with_nothing_held_says_nothing_of_holds(config: Path, fakes: FakeTools) -> None:
    result = _run(["--json", "--appstore-get", "--app", "example"])

    assert result.exit_code == 0, result.output
    assert _events(result, "appstore-hold") == []
    assert _pin(config) == "latest"


def test_get_latest_lets_go_even_when_the_latest_is_already_staged(
    config: Path, fakes: FakeTools, cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        itunes, "lookup_by_bundle_id",
        lambda bundle_id, country=None: itunes.ITunesApp(track_id=1, bundle_id=bundle_id, name="x", version="1.4.0"),
    )
    make_ipa(cache_dir / "com.example.app_1.4.0.ipa", version="1.4.0", encrypted=False)
    set_app_options(config, "example", preferred_version="1.2.0")

    result = _run(["--json", "--appstore-get", "--app", "example"])

    assert result.exit_code == 0, result.output
    assert [e["type"] for e in _events(result)] == ["appstore-already-staged", "appstore-hold"]
    assert _pin(config) == "latest"
    assert not [c for c in fakes.calls("ipatool") if c["argv"][:1] == ["download"]]


def test_a_get_that_fails_keeps_the_hold(config: Path, fakes: FakeTools) -> None:
    set_app_options(config, "example", preferred_version="1.2.0")
    fakes.set(download_error="received error: Your request could not be completed.")

    result = _run(["--json", "--appstore-get", "--app", "example"])

    assert result.exit_code == 20
    assert _events(result, "appstore-hold") == []
    assert _pin(config) == "1.2.0"


def test_a_version_already_staged_is_held_without_downloading_it_again(
    config: Path, old_build: FakeTools, cache_dir: Path
) -> None:
    assert _run(["--json", "--appstore-versions", "--app", "example"]).exit_code == 0
    make_ipa(cache_dir / "com.example.app_1.2.0.ipa", version="1.2.0", encrypted=False)

    result = _run(["--json", "--appstore-get", "--app", "example", "--version-id", "800200"])

    assert result.exit_code == 0, result.output
    assert [e["type"] for e in _events(result)] == ["appstore-already-staged", "appstore-hold"]
    assert _events(result, "appstore-already-staged")[0]["version"] == "1.2.0"
    assert _pin(config) == "1.2.0"
    assert not [c for c in old_build.calls("ipatool") if c["argv"][:1] == ["download"]]


def test_again_downloads_a_version_already_staged(config: Path, old_build: FakeTools, cache_dir: Path) -> None:
    assert _run(["--json", "--appstore-versions", "--app", "example"]).exit_code == 0
    make_ipa(cache_dir / "com.example.app_1.2.0.ipa", version="1.2.0", encrypted=False)

    result = _run(["--json", "--appstore-get", "--app", "example", "--version-id", "800200", "--again"])

    assert result.exit_code == 0, result.output
    assert len([c for c in old_build.calls("ipatool") if c["argv"][:1] == ["download"]]) == 1
    assert _pin(config) == "1.2.0"


def test_a_version_never_listed_is_downloaded(config: Path, old_build: FakeTools, cache_dir: Path) -> None:
    """Fails open, as `already_staged` does: with no details for the id there
    is nothing to compare, so it downloads."""
    make_ipa(cache_dir / "com.example.app_1.2.0.ipa", version="1.2.0", encrypted=False)

    result = _run(["--json", "--appstore-get", "--app", "example", "--version-id", "800200"])

    assert result.exit_code == 0, result.output
    assert len([c for c in old_build.calls("ipatool") if c["argv"][:1] == ["download"]]) == 1


def test_a_version_apple_no_longer_serves_says_so_and_holds_nothing(config: Path, fakes: FakeTools) -> None:
    fakes.set(gone_versions=["800100"])

    result = _run(["--json", "--appstore-get", "--app", "example", "--version-id", "800100"])

    assert result.exit_code == 20
    words = " ".join(result.stderr.split())
    assert "no longer available" in words
    assert "doesn't serve every old version" in words
    assert _events(result, "appstore-hold") == []
    assert _pin(config) == "latest"


def test_an_unverified_old_version_names_its_version_in_the_retry(config: Path, fakes: FakeTools) -> None:
    """Older versions are the ones most likely to have no page hashes."""
    fakes.set(plaintext_verified=False)

    result = _run(["--json", "--appstore-get", "--app", "example", "--version-id", "800200"])

    assert result.exit_code == 20
    assert "--version-id 800200 --allow-unverified" in " ".join(result.stderr.split())


#: Digits, but not ASCII ones: Arabic-Indic, Extended Arabic-Indic,
#: fullwidth, and an ASCII 8 with Arabic-Indic zeros. `str.isdigit` and `\d`
#: take every one of them, and `int()` reads them as numbers.
NOT_ASCII_DIGITS = ["١٢٣", "۱۲۳", "１２３", "8٠٠"]


@pytest.mark.parametrize("bad", ["", "abc", "12 34", "-5", "--again", "1" * 21, *NOT_ASCII_DIGITS])
def test_a_version_id_is_a_number(config: Path, fakes: FakeTools, bad: str) -> None:
    result = _run(["--json", "--appstore-get", "--app", "example", f"--version-id={bad}"])

    assert result.exit_code == 2, result.output
    assert "--version-id" in " ".join(result.output.split())
    assert fakes.calls("ipatool") == []


def test_version_id_belongs_to_the_get(config: Path, fakes: FakeTools) -> None:
    result = _run(["--json", "--appstore-check", "--app", "example", "--version-id", "800200"])

    assert result.exit_code == 2
    assert "--version-id is not valid with --appstore-check" in " ".join(result.output.split())


# --- --appstore-check and the hold ----------------------------------------------


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        itunes, "lookup_by_bundle_id",
        lambda bundle_id, country=None: itunes.ITunesApp(track_id=1, bundle_id=bundle_id, name="x", version="1.4.0"),
    )


def test_a_held_app_is_not_told_of_an_update(config: Path, cache_dir: Path, store: None) -> None:
    set_app_options(config, "example", preferred_version="1.2.0")
    make_ipa(cache_dir / "com.example.app_1.2.0.ipa", version="1.2.0", encrypted=False)

    result = _run(["--json", "--appstore-check", "--app", "example"])

    (event,) = _events(result)
    assert (event["store_version"], event["cached_version"]) == ("1.4.0", "1.2.0")
    assert event["held_version"] == "1.2.0"
    assert event["update_available"] is False


def test_an_app_not_held_still_is(config: Path, cache_dir: Path, store: None) -> None:
    make_ipa(cache_dir / "com.example.app_1.2.0.ipa", version="1.2.0", encrypted=False)

    result = _run(["--json", "--appstore-check", "--app", "example"])

    (event,) = _events(result)
    assert event["held_version"] is None
    assert event["update_available"] is True


def test_a_held_app_for_a_person(config: Path, cache_dir: Path, store: None) -> None:
    set_app_options(config, "example", preferred_version="1.2.0")

    result = _run(["--appstore-check", "--app", "example"])

    assert "held at 1.2.0" in result.stdout and "update available" not in result.stdout


def test_the_hint_for_a_missing_held_version_points_at_the_versions_list(config: Path) -> None:
    """A get of the latest lets go of a hold now, so it is no way to stage
    the held version: the hint names the list and `--version-id`."""
    from graft_ipa.errors import IPANotFoundError
    from graft_ipa.ipa_cache import acquire_ipa

    set_app_options(config, "example", preferred_version="1.2.0")
    cfg = load_config(config)

    with pytest.raises(IPANotFoundError) as caught:
        acquire_ipa(cfg, cfg.get_app("example"))

    assert "--appstore-versions --app example" in caught.value.hint
    assert "--version-id" in caught.value.hint
    assert "that fetches the current release" not in caught.value.hint


# --- What a listing can't use, and what stops it ---------------------------------


def test_an_empty_list_is_no_versions(config: Path, fakes: FakeTools) -> None:
    fakes.set(versions=[])

    result = _run(["--json", "--appstore-versions", "--app", "example"])
    person = _run(["--appstore-versions", "--app", "example"])

    assert result.exit_code == 0, result.output
    assert _events(result, "appstore-release") == []
    assert _events(result, "appstore-releases-end")[0]["count"] == 0
    assert _metadata_calls(fakes) == []
    assert person.exit_code == 0, person.output
    text = " ".join(person.stdout.split())
    assert "0 version(s) on the App Store" in text
    assert "--version-id" not in text, "nothing to get"


@pytest.mark.parametrize("ids", [["abc"], ["--external-version-id", "", "8.0", *NOT_ASCII_DIGITS]])
def test_a_list_whose_every_id_was_dropped_is_an_error(config: Path, fakes: FakeTools, ids: list[str]) -> None:
    """Not "no versions": ipatool listed some, and none is one Graft can get."""
    fakes.set(versions=ids)

    result = _run(["--json", "--appstore-versions", "--app", "example"])

    assert result.exit_code == 20, result.output
    words = " ".join(result.stderr.split())
    assert f"ipatool listed {len(ids)} version id(s) for com.example.app" in words
    assert _events(result, "appstore-releases-end") == []
    assert _metadata_calls(fakes) == []


def _hang(monkeypatch: pytest.MonkeyPatch, command: str) -> list[list[str]]:
    """ipatool's `command` runs past its timeout: `subprocess.run` raises
    `TimeoutExpired`, as it does once it has killed the child. Every other
    command runs the stand-in. Returns the argv of each run that hung."""
    real = ipatool.run_ipatool
    hung: list[list[str]] = []

    def run(path: Path, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[:1] == [command]:
            hung.append(args)
            raise subprocess.TimeoutExpired([str(path), *args], kwargs["timeout"])
        return real(path, args, **kwargs)

    monkeypatch.setattr(ipatool, "run_ipatool", run)
    return hung


def test_a_list_that_times_out_says_so(config: Path, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch) -> None:
    hung = _hang(monkeypatch, "list-versions")

    result = _run(["--json", "--appstore-versions", "--app", "example"])

    assert result.exit_code == 20, result.output
    assert len(hung) == 1
    words = " ".join(result.stderr.split())
    assert "ipatool's list of versions didn't answer within 3 minutes" in words
    assert "check the connection" in words
    assert _events(result, "appstore-releases-end") == []


def test_a_version_read_that_times_out_stops_the_listing(
    config: Path, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dropped connection: not up to ten reads of 90 seconds each, then
    every row saying so and an exit of 0. The listing stops at the first, and
    fails."""
    assert _run(["--json", "--appstore-versions", "--app", "example", "--limit", "1"]).exit_code == 0
    hung = _hang(monkeypatch, "get-version-metadata")

    result = _run(["--json", "--appstore-versions", "--app", "example", "--limit", "3"])

    assert result.exit_code == 20, result.output
    assert [a[a.index("--external-version-id") + 1] for a in hung] == ["800300"], "it stops at the first"
    assert [r["version"] for r in _events(result, "appstore-release")] == ["1.4.0"], "what came before stays"
    assert _events(result, "appstore-releases-end") == []
    words = " ".join(result.stderr.split())
    assert "the list of example's versions stopped: version 800300 didn't answer within 90 seconds" in words
    assert "check the connection" in words and "read so far are remembered" in words


#: A stalled read now ends inside ipatool: the bundled patch fails a range
#: request that receives nothing for 30 seconds, twice in a row
#: (bundle/patches/ipatool-range-readahead.patch), with Go's words for a
#: deadline, well before Graft's own 90 seconds.
#: As ipatool 2.8.0 wraps them on the way out: the reader's errors through
#: get-version-metadata (appstore_get_version_metadata.go,
#: appstore_partial_zip.go), the Store transport's (internal/store/backend.go),
#: and net/http's own. The signed URL's query is Apple's, and secret.
_SIGNED = 'Get "https://iosapps.itunes.apple.com/itunes-assets/x/app.ipa?accessKey=1_secret"'
IPATOOL_TIMEOUTS = [
    # The patch's stall timeout, waiting for a range request's answer.
    "failed to read version metadata: failed to open zip reader: request failed: "
    "received nothing for 30s: context deadline exceeded",
    # The same, while its body came in.
    "failed to read version metadata: failed to open zip reader: failed to read response body: "
    "received nothing for 30s: context deadline exceeded",
    # A Go deadline as the Store transport passes it on.
    f"failed to read version metadata: failed to open zip reader: request failed: received error: "
    f"store request: store HTTP transport: {_SIGNED}: context deadline exceeded",
    # The size request, through the transport's header timeout.
    f"failed to read version metadata: failed to read remote file size: request failed: received error: "
    f"store request: store HTTP transport: {_SIGNED}: net/http: timeout awaiting response headers",
    f"request failed: store HTTP transport: {_SIGNED}: net/http: TLS handshake timeout",
    f"request failed: {_SIGNED}: dial tcp 17.253.0.1:443: i/o timeout",
    f"request failed: {_SIGNED}: context deadline exceeded (Client.Timeout exceeded while awaiting headers)",
]


@pytest.mark.parametrize("said", IPATOOL_TIMEOUTS)
def test_a_version_read_ipatool_gave_up_on_stops_the_listing(
    config: Path, fakes: FakeTools, said: str
) -> None:
    """A timeout is a timeout whoever's clock ran out. As
    `VersionUnavailable` it went on through the page, at up to a minute a
    version, every row saying the same."""
    assert _run(["--json", "--appstore-versions", "--app", "example", "--limit", "1"]).exit_code == 0
    fakes.set(version_metadata={**META, "800300": {"error": said}})

    result = _run(["--json", "--appstore-versions", "--app", "example", "--limit", "3"])

    assert result.exit_code == 20, result.output
    assert _metadata_calls(fakes) == ["800400", "800300"], "it stops at the first"
    assert [r["version"] for r in _events(result, "appstore-release")] == ["1.4.0"], "what came before stays"
    assert _events(result, "appstore-releases-end") == []
    words = " ".join(result.stderr.split())
    assert "the list of example's versions stopped: reading version 800300 timed out" in words
    assert "check the connection" in words and "read so far are remembered" in words
    assert "secret" not in result.output, "never the signed URL's query"


@pytest.mark.parametrize("said", IPATOOL_TIMEOUTS)
def test_ipatool_s_own_timeouts_are_timeouts(fakes: FakeTools, said: str) -> None:
    fakes.set(version_metadata={"800300": {"error": said}})

    with pytest.raises(ipatool.VersionMetadataTimeout) as caught:
        ipatool.version_metadata(fakes.ipatool, "com.example.app", "800300")

    assert caught.value.hint == "check the connection and try again"
    assert "secret" not in caught.value.message


@pytest.mark.parametrize("said", [
    "received error: This item is no longer available.",
    "failed to read version metadata: could not find Info.plist",
    "failed to read version metadata: failed to open zip reader: zip: not a valid zip file",
    "failed to read version metadata: expected partial content response, got status 403",
])
def test_what_isnt_a_timeout_is_this_version_alone(fakes: FakeTools, said: str) -> None:
    fakes.set(version_metadata={"800300": {"error": said}})

    with pytest.raises(ipatool.VersionUnavailable):
        ipatool.version_metadata(fakes.ipatool, "com.example.app", "800300")


def test_graft_s_own_cap_ends_a_read_that_keeps_trickling(fakes: FakeTools, monkeypatch: pytest.MonkeyPatch) -> None:
    """ipatool's own deadline now counts silence, so a read that keeps a
    little coming never ends there (bundle/patches/ipatool-range-readahead.
    patch). Graft's cap on the whole run still ends it, and kills it. The cap
    is cut to 3 seconds; the stand-in sends a line every 0.1 s for 20."""
    monkeypatch.setattr(ipatool, "VERSION_METADATA_TIMEOUT", 3)
    fakes.warm_up()
    fakes.set(metadata_trickle={"every": 0.1, "for": 20})
    started = time.monotonic()

    with pytest.raises(ipatool.VersionMetadataTimeout) as caught:
        ipatool.version_metadata(fakes.ipatool, "com.example.app", "800300")

    assert time.monotonic() - started < 15, "it waited for the trickle to end"
    assert caught.value.message == "version 800300 didn't answer within 3 seconds"
    (trickling,) = [c for c in fakes.calls("ipatool") if c.get("trickling")]
    with pytest.raises(ProcessLookupError):
        os.kill(trickling["pid"], 0)


# --- A version that stalls every time doesn't hold the list up ------------------

#: How ipatool says a version's read stalled, with the bundled patch: nothing
#: received for 30 seconds (bundle/patches/ipatool-range-readahead.patch).
STALLED = {"error": "failed to read version metadata: failed to open zip reader: failed to read response "
                    "body: received nothing for 30s: context deadline exceeded"}


def _versions(*args: str):
    return _run(["--json", "--appstore-versions", "--app", "example", *args])


def _past_graft_s_cap(monkeypatch: pytest.MonkeyPatch, external_id: str) -> None:
    """`external_id`'s read runs past Graft's own cap (`TimeoutExpired`, as
    `subprocess.run` raises once it has killed the child). Every other run
    is the stand-in's."""
    real = ipatool.run_ipatool

    def run(path: Path, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[:1] == ["get-version-metadata"] and args[args.index("--external-version-id") + 1] == external_id:
            raise subprocess.TimeoutExpired([str(path), *args], kwargs["timeout"])
        return real(path, args, **kwargs)

    monkeypatch.setattr(ipatool, "run_ipatool", run)


@pytest.mark.parametrize("whose", ["ipatool's", "Graft's"])
def test_a_version_that_stalls_twice_in_a_row_is_unavailable_and_the_list_goes_on(
    config: Path, fakes: FakeTools, cache_dir: Path, monkeypatch: pytest.MonkeyPatch, whose: str
) -> None:
    """Try Again picks up at the version that stalled, so one that stalls
    every time stopped the list there for good. The first time, the list
    stops; the second time in a row, that version is said on its row, as one
    Apple won't describe is, and the rest are read."""
    if whose == "ipatool's":
        fakes.set(version_metadata={**META, "800300": STALLED})
    else:
        _past_graft_s_cap(monkeypatch, "800300")

    first = _versions("--limit", "3")

    assert first.exit_code == 20, first.output
    assert [r["version"] for r in _events(first, "appstore-release")] == ["1.4.0"]
    assert _events(first, "appstore-releases-end") == []
    assert "if this one stalls again, the list goes on without it" in " ".join(first.stderr.split())
    assert json.loads(_cache(cache_dir).read_text())["stalled"] == {"com.example.app": "800300"}, (
        "remembered for Try Again, a run of its own"
    )

    again = _versions("--limit", "3")

    assert again.exit_code == 0, again.output
    rows = {r["external_id"]: r for r in _events(again, "appstore-release")}
    assert rows["800300"]["version"] is None
    assert rows["800300"]["error"].startswith("Timed out twice in a row, so the list went on without it: ")
    assert "800300" in rows["800300"]["error"], "with what timed out"
    assert rows["800200"]["version"] == "1.2.0", "and the list went on"
    assert rows["800400"]["version"] == "1.4.0"
    assert len(_events(again, "appstore-releases-end")) == 1


def test_a_stall_on_another_version_in_between_starts_the_count_again(config: Path, fakes: FakeTools) -> None:
    fakes.set(version_metadata={**META, "800400": STALLED, "800300": STALLED})
    assert _versions("--offset", "1", "--limit", "1").exit_code == 20
    assert _versions("--limit", "1").exit_code == 20

    third = _versions("--offset", "1", "--limit", "1")

    assert third.exit_code == 20, "800300 stalled twice, but not in a row"
    assert _metadata_calls(fakes) == ["800300", "800400", "800300"]


@pytest.mark.parametrize("between", [
    META["800400"], {"error": "received error: This item is no longer available."},
], ids=["read", "unavailable"])
def test_a_version_read_in_between_starts_the_count_again(
    config: Path, fakes: FakeTools, between: dict[str, str]
) -> None:
    """A read that ends with an answer, whatever it is, isn't a stall: the
    connection was working."""
    fakes.set(version_metadata={**META, "800400": between, "800300": STALLED})
    assert _versions("--offset", "1", "--limit", "1").exit_code == 20
    assert _versions("--limit", "1").exit_code == 0

    third = _versions("--offset", "1", "--limit", "1")

    assert third.exit_code == 20, "800300 stalled twice, but not in a row"
    assert _metadata_calls(fakes) == ["800300", "800400", "800300"]


def test_a_stall_is_counted_for_its_own_app(config: Path, fakes: FakeTools, cache_dir: Path) -> None:
    """Another app's version, stalled last, is no count for this one."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    _cache(cache_dir).write_text(json.dumps({
        "format": 1, "apps": {}, "stalled": {"com.example.other": "800300"},
    }))
    fakes.set(version_metadata={**META, "800300": STALLED})

    result = _versions("--offset", "1", "--limit", "1")

    assert result.exit_code == 20, result.output


def test_a_licence_refused_partway_through_is_the_needs_license_refusal(config: Path, fakes: FakeTools) -> None:
    """As a get maps it, so the app says why in its own words and offers the
    way out, rather than ipatool's bare error."""
    fakes.set(metadata_error="license is required")

    result = _run(["--json", "--appstore-versions", "--app", "example"])

    assert result.exit_code == 20, result.output
    assert [e["reason"] for e in _events(result, "appstore-refused")] == [appstore.REFUSED_NEEDS_LICENSE]
    assert _metadata_calls(fakes) == ["800400"]
    assert _events(result, "appstore-releases-end") == []


# --- A version id is ASCII digits, wherever it is checked -----------------------


@pytest.mark.parametrize("bad", NOT_ASCII_DIGITS)
def test_only_ascii_digits_are_a_version_id(bad: str) -> None:
    assert bad.isdigit(), "the trap: Python calls these digits"
    assert not ipatool.is_external_version_id(bad)
    assert ipatool.is_external_version_id("800200")


@pytest.mark.parametrize("bad", ["--again", "", "12 34", "1" * 21, *NOT_ASCII_DIGITS])
def test_download_checks_a_version_id_itself(fakes: FakeTools, tmp_path: Path, bad: str) -> None:
    """Its own check, not only the CLI's: nothing after
    `--external-version-id` on ipatool's argv can read as a flag."""
    out = tmp_path / "out"
    out.mkdir()

    with pytest.raises(AppStoreError) as caught:
        ipatool.download(fakes.ipatool, "com.example.app", out, external_version_id=bad)

    assert "isn't an App Store version id" in caught.value.message
    assert fakes.calls("ipatool") == []


def test_an_old_version_of_an_app_the_apple_id_lacks_buys_nothing_and_holds_nothing(
    config: Path, fakes: FakeTools
) -> None:
    set_app_options(config, "example", preferred_version="1.3.0")
    fakes.set(download_error="license is required")

    result = _run(["--json", "--appstore-get", "--app", "example", "--version-id", "800200"])

    assert result.exit_code == 20, result.output
    assert [e["reason"] for e in _events(result, "appstore-refused")] == [appstore.REFUSED_NEEDS_LICENSE]
    commands = [c["argv"][:1] for c in fakes.calls("ipatool")]
    assert ["download"] in commands
    assert ["purchase"] not in commands, "a get never buys, not even a free app"
    assert _events(result, "appstore-hold") == []
    assert _pin(config) == "1.3.0"
    words = " ".join(result.stderr.split())
    assert "--version-id 800200" in words, "following the hint gets that version again, not the latest"


# --- The hold is written against the config as it is then ------------------------


def _edit_during_the_get(monkeypatch: pytest.MonkeyPatch, config: Path, **edit: Any) -> None:
    """Edit App saves `edit` while the get runs: after the download, before
    the get stages it and writes the hold."""
    real = cli_mod._stage_ipa

    def stage(cfg: Any, **kwargs: Any) -> str:
        set_app_options(config, "example", **edit)
        return real(cfg, **kwargs)

    monkeypatch.setattr(cli_mod, "_stage_ipa", stage)


@pytest.mark.parametrize(("start", "args", "edited"), [
    ("latest", ["--version-id", "800200"], "1.3.0"),
    ("1.2.0", [], "9.9.9"),
])
def test_a_preferred_version_edited_during_the_get_is_kept(
    config: Path, old_build: FakeTools, monkeypatch: pytest.MonkeyPatch,
    start: str, args: list[str], edited: str,
) -> None:
    """The get doesn't put back what the pin was when it started, over what
    the person chose since. It says so instead."""
    set_app_options(config, "example", preferred_version=start)
    _edit_during_the_get(monkeypatch, config, preferred_version=edited)

    result = _run(["--json", "--appstore-get", "--app", "example", *args])

    assert result.exit_code == 0, result.output
    assert _pin(config) == edited
    assert _events(result, "appstore-hold") == []
    warnings = [e["line"] for e in _events(result, "log") if e["source"] == "warning"]
    assert len(warnings) == 1 and f"changed to {edited} while this get ran" in warnings[0], warnings


def test_a_hold_written_after_an_edit_keeps_the_edit(
    config: Path, old_build: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    _edit_during_the_get(monkeypatch, config, cyan={"display_name": "Edited"}, delivery="livecontainer")

    result = _run(["--json", "--appstore-get", "--app", "example", "--version-id", "800200"])

    assert result.exit_code == 0, result.output
    app = load_config(config).get_app("example")
    assert (app.preferred_version, app.cyan.display_name, app.delivery) == ("1.2.0", "Edited", "livecontainer")
    assert [(e["version"], e["previous"]) for e in _events(result, "appstore-hold")] == [("1.2.0", None)]


# --- The cache file ---------------------------------------------------------------


def _cache(cache_dir: Path) -> Path:
    return cache_dir / appstore.VERSIONS_CACHE


def _cache_claiming(path: Path, version: str = "6.6.6") -> None:
    """A well-formed cache that says 800400 is `version`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": 1, "apps": {"com.example.app": {
        "800400": {"version": version, "release_date": None}}}}))


def test_the_cache_file_is_the_users_alone(config: Path, fakes: FakeTools, cache_dir: Path) -> None:
    assert _run(["--json", "--appstore-versions", "--app", "example", "--limit", "1"]).exit_code == 0

    assert stat.S_IMODE(os.lstat(_cache(cache_dir)).st_mode) == 0o600


def test_a_link_at_the_cache_path_is_not_followed(
    config: Path, fakes: FakeTools, cache_dir: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere.json"
    _cache_claiming(elsewhere)
    before = elsewhere.read_text()
    cache_dir.mkdir(parents=True, exist_ok=True)
    _cache(cache_dir).symlink_to(elsewhere)

    result = _run(["--json", "--appstore-versions", "--app", "example", "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert _events(result, "appstore-release")[0]["version"] == "1.4.0", "read from ipatool, not through the link"
    assert _metadata_calls(fakes) == ["800400"]
    assert _cache(cache_dir).is_file() and not _cache(cache_dir).is_symlink(), "the write replaces the link"
    assert elsewhere.read_text() == before, "and never writes through it"


def test_a_fifo_at_the_cache_path_is_no_cache(cache_dir: Path) -> None:
    """A plain read of a FIFO waits for a writer that never comes."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.mkfifo(_cache(cache_dir))
    loaded: list[appstore._VersionCache] = []
    reader = threading.Thread(target=lambda: loaded.append(appstore._VersionCache.load(cache_dir)), daemon=True)

    reader.start()
    reader.join(5)

    if reader.is_alive():
        # Free the stuck reader: a writer opens the FIFO, and closes it.
        try:
            os.close(os.open(_cache(cache_dir), os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass
        pytest.fail("loading the versions cache waited on a FIFO")
    assert loaded[0].known("com.example.app") == {}


def test_a_cache_too_big_to_be_graft_s_is_no_cache(cache_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _cache_claiming(_cache(cache_dir))
    size = _cache(cache_dir).stat().st_size

    monkeypatch.setattr(appstore, "_VERSIONS_CACHE_MAX", size - 1)
    assert appstore._VersionCache.load(cache_dir).known("com.example.app") == {}
    monkeypatch.setattr(appstore, "_VERSIONS_CACHE_MAX", size)
    assert appstore._VersionCache.load(cache_dir).known("com.example.app")["800400"].version == "6.6.6"


def _reads(patched: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Record each `os.read` the cache load makes, before it runs: (the
    file type of what it reads, how many bytes it asks for). Recorded first
    because a read of a directory raises."""
    real_read, real_fstat = os.read, os.fstat
    asked: list[tuple[int, int]] = []

    def read(fd: int, n: int) -> bytes:
        asked.append((stat.S_IFMT(real_fstat(fd).st_mode), n))
        return real_read(fd, n)

    patched.setattr(appstore.os, "read", read)
    return asked


def _grows_after_its_size_is_read(patched: pytest.MonkeyPatch) -> None:
    """`os.fstat` says the file is empty, as it was an instant before a
    writer filled it: what is read then is all that bounds the read."""
    real = os.fstat

    def fstat(fd: int) -> os.stat_result:
        info = real(fd)
        return os.stat_result((*info[:6], 0, *info[7:10]))

    patched.setattr(appstore.os, "fstat", fstat)


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_only_a_regular_file_is_ever_read(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Checked before the first read. Not left to what a read does: a
    directory's raises and a FIFO opened without blocking reads as empty,
    and each is no cache either way, so only the reads show the check."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    if kind == "directory":
        _cache(cache_dir).mkdir()
    else:
        os.mkfifo(_cache(cache_dir))

    with monkeypatch.context() as patched:
        asked = _reads(patched)
        loaded = appstore._VersionCache.load(cache_dir)

    assert asked == [], f"read a {kind}"
    assert loaded.known("com.example.app") == {}


def test_a_file_over_the_cap_is_never_read(cache_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first of the three caps: its size, before any read."""
    _cache_claiming(_cache(cache_dir))
    monkeypatch.setattr(appstore, "_VERSIONS_CACHE_MAX", _cache(cache_dir).stat().st_size - 1)

    with monkeypatch.context() as patched:
        asked = _reads(patched)
        loaded = appstore._VersionCache.load(cache_dir)

    assert asked == []
    assert loaded.known("com.example.app") == {}


def test_a_file_that_grew_is_read_no_further_than_one_byte_past_the_cap(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second: the reads themselves. Each asks for no more than is left
    of the cap and a byte, and none follows the one that passed it, so a file
    that grows without end (a link swapped for /dev/zero's like) is read no
    further."""
    _cache_claiming(_cache(cache_dir))
    cap = _cache(cache_dir).stat().st_size + 5
    monkeypatch.setattr(appstore, "_VERSIONS_CACHE_MAX", cap)
    with _cache(cache_dir).open("a") as f:
        f.write(" " * (3 * cap))

    with monkeypatch.context() as patched:
        _grows_after_its_size_is_read(patched)
        asked = _reads(patched)
        appstore._VersionCache.load(cache_dir)

    assert asked, "a file its size let through is read"
    assert all(kind == stat.S_IFREG for kind, _ in asked)
    assert sum(n for _, n in asked) == cap + 1, asked
    assert all(n > 0 for _, n in asked), "no read after the one that passed the cap"


def test_a_file_that_grew_past_the_cap_is_no_cache(cache_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The third: what was read. The cap and a byte of it is a whole cache
    here, trailing spaces and all, and still no cache: it is more than the
    cap."""
    _cache_claiming(_cache(cache_dir))
    cap = _cache(cache_dir).stat().st_size + 5
    monkeypatch.setattr(appstore, "_VERSIONS_CACHE_MAX", cap)
    with _cache(cache_dir).open("a") as f:
        f.write(" " * (3 * cap))
    assert json.loads(_cache(cache_dir).read_bytes()[: cap + 1]), "what the reads take parses"

    with monkeypatch.context() as patched:
        _grows_after_its_size_is_read(patched)
        loaded = appstore._VersionCache.load(cache_dir)

    assert loaded.known("com.example.app") == {}


def test_a_cache_nested_too_deep_to_parse_is_no_cache(cache_dir: Path) -> None:
    """`json.loads` raises RecursionError here, which isn't a ValueError."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    _cache(cache_dir).write_text("[" * 200_000)

    assert appstore._VersionCache.load(cache_dir).known("com.example.app") == {}


def test_the_cache_is_replaced_whole_and_never_written_in_place(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cache_claiming(_cache(cache_dir), "1.4.0")
    before = _cache(cache_dir).read_text()
    inode = _cache(cache_dir).stat().st_ino
    cache = appstore._VersionCache.load(cache_dir)
    newer = ipatool.VersionMetadata(version="1.3.0", release_date=None)

    def full(src: Any, dst: Any) -> None:
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as patched:
        patched.setattr(appstore.os, "replace", full)
        cache.remember("com.example.app", "800300", newer)
    assert _cache(cache_dir).read_text() == before, "a write that fails leaves the old file whole"
    assert sorted(p.name for p in cache_dir.iterdir()) == [appstore.VERSIONS_CACHE], "and no temp file"

    cache.remember("com.example.app", "800300", newer)

    assert _cache(cache_dir).stat().st_ino != inode, "a new file, renamed over the old"
    assert set(appstore._VersionCache.load(cache_dir).known("com.example.app")) == {"800400", "800300"}
    assert sorted(p.name for p in cache_dir.iterdir()) == [appstore.VERSIONS_CACHE]


# --- The app decodes every key, and nothing it can't do without is null ----------


def _keys_match(record: str, payload: dict) -> None:
    from tests.test_appstore_contract_pins import _decoded

    payload = dict(payload)
    payload.pop("type")
    swift = _decoded(record)
    assert set(swift) == set(payload), record
    for key, optional in swift.items():
        if not optional:
            assert payload[key] is not None, f"{record}.{key} is non-optional in Swift but null here"


def test_the_listing_records_decode_what_the_cli_sends(config: Path, fakes: FakeTools) -> None:
    result = _run(["--json", "--appstore-versions", "--app", "example", "--limit", "1"])
    releases = _events(result, "appstore-release")

    _keys_match("AppStoreReleaseRecord", releases[0])
    _keys_match("AppStoreReleaseRecord", releases[-1])
    _keys_match("AppStoreReleasesEndRecord", _events(result, "appstore-releases-end")[0])


def test_the_hold_record_decodes_what_the_cli_sends(config: Path, old_build: FakeTools) -> None:
    held = _run(["--json", "--appstore-get", "--app", "example", "--version-id", "800200"])
    released = _run(["--json", "--appstore-get", "--app", "example"])

    _keys_match("AppStoreHoldRecord", _events(held, "appstore-hold")[0])
    _keys_match("AppStoreHoldRecord", _events(released, "appstore-hold")[0])


def test_the_version_record_decodes_what_the_cli_sends(config: Path, store: None) -> None:
    set_app_options(config, "example", preferred_version="1.2.0")

    _keys_match("AppStoreVersionRecord", _events(_run(["--json", "--appstore-check", "--app", "example"]))[0])


def test_the_new_flags_are_the_ones_the_app_sends() -> None:
    from tests.cli_ast import contract_swift_source

    swift = contract_swift_source()
    for name, flag in (("versionId", "--version-id"), ("appstoreVersions", "--appstore-versions"),
                       ("offset", "--offset")):
        assert f'static let {name} = "{flag}"' in swift
