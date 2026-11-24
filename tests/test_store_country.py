"""`store_country`: an app is looked up in the App Store it was found in.

Search asks this Mac's own store (`appstore_search.resolve_country`), and an
app found there may not exist on the US store, which every lookup used to
ask. So the store an app came from is kept in config.yaml (`store_country`),
and `--appstore-check` and `--artwork` ask that one. An app without it is
asked exactly as before: no `country=`, which the iTunes API reads as the US.

The network is faked at `itunes._get_json`, the HTTP every iTunes request
shares, so these see the very parameters each request would carry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa import host, itunes
from graft_ipa.config import AppConfig, add_app, load_config, set_app_options, set_app_tweaks
from graft_ipa.errors import ConfigError
from tests.itunes_fakes import listing


def _run(args: list[str]):
    return CliRunner().invoke(cli_mod.app, args)


def _apps(config: Path) -> dict[str, Any]:
    return yaml.safe_load(config.read_text())["apps"]


class FakeLookups:
    """`itunes._get_json`: every request's endpoint and params, answered with
    one listing (app 7, with no artwork, so `--artwork` fetches no image)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, endpoint: str, params: dict, *, described: str) -> dict:
        self.calls.append((endpoint, dict(params)))
        entry = {k: v for k, v in listing(7).items() if not k.startswith("artworkUrl")}
        return {"resultCount": 1, "results": [entry]}

    @property
    def countries(self) -> list[str | None]:
        return [params.get("country") for _, params in self.calls]


@pytest.fixture
def lookups(monkeypatch: pytest.MonkeyPatch) -> FakeLookups:
    fake = FakeLookups()
    monkeypatch.setattr(itunes, "_get_json", fake)
    return fake


@pytest.fixture
def german_app(temp_config_env: Path) -> str:
    """An app added from the German store, as Add App's Search adds one."""
    add_app(temp_config_env, "beispiel", {
        "bundle_id": "com.example.app7", "tweaks": [], "store_country": "DE",
    })
    return "beispiel"


# --- config.yaml ----------------------------------------------------------------


@pytest.mark.parametrize(("value", "code"), [("DE", "DE"), ("de", "DE"), (" gb ", "GB")])
def test_store_country_is_read_upper_cased(value: str, code: str) -> None:
    app = AppConfig.from_dict("x", {"bundle_id": "com.example.app", "store_country": value})
    assert app.store_country == code


def test_an_app_without_store_country_has_none() -> None:
    assert AppConfig.from_dict("x", {"bundle_id": "com.example.app"}).store_country is None


@pytest.mark.parametrize(
    ("value", "shown"),
    [("ZZ", "'ZZ'"), ("IR", "'IR'"), ("aq", "'aq'"), ("GBR", "'GBR'"), ("", "''"), ("1A", "'1A'"),
     (49, "49"), (True, "True")],
)
def test_a_hand_written_store_country_that_isnt_a_store_is_ignored_with_a_notice(
    value: Any, shown: str
) -> None:
    """A hand edit must not stop every command: the app is loaded with no
    store (this Mac's, else the US), and the CLI says so once."""
    notices: list[str] = []
    app = AppConfig.from_dict("x", {"bundle_id": "com.example.app", "store_country": value}, notices=notices)
    assert app.store_country is None
    assert notices == [f"store_country {shown} for x isn't an App Store; ignoring it"]


@pytest.mark.parametrize(("value", "code"), [("UK", "GB"), ("PR", "US"), ("re", "FR")])
def test_a_hand_written_territory_or_alias_is_its_store_with_a_note(value: str, code: str) -> None:
    notices: list[str] = []
    app = AppConfig.from_dict("x", {"bundle_id": "com.example.app", "store_country": value}, notices=notices)
    assert app.store_country == code
    assert notices == [f"store_country {value!r} for x is the {code} App Store; using {code}"]


def test_a_store_written_as_a_store_says_nothing() -> None:
    notices: list[str] = []
    for value in ("GB", "gb", " de ", False):
        AppConfig.from_dict("x", {"bundle_id": "com.example.app", "store_country": value}, notices=notices)
    assert notices == []


def _hand_write(config: Path, value: str) -> None:
    text = config.read_text().replace(
        "bundle_id: com.example.app", f"bundle_id: com.example.app\n    store_country: {value}", 1
    )
    assert f"store_country: {value}" in text, "premise: the test config's app entry"
    config.write_text(text)


def test_a_config_with_a_store_that_isnt_one_still_loads(temp_config_env: Path) -> None:
    _hand_write(temp_config_env, "ZZ")
    cfg = load_config(temp_config_env)
    assert cfg.apps["example"].store_country is None
    assert cfg.notices == ["store_country 'ZZ' for example isn't an App Store; ignoring it"]


@pytest.mark.parametrize("json_mode", [True, False])
def test_every_command_warns_of_it_once_and_carries_on(temp_config_env: Path, json_mode: bool) -> None:
    _hand_write(temp_config_env, "ZZ")
    warning = "store_country 'ZZ' for example isn't an App Store; ignoring it"

    result = _run(["--json", "--list"] if json_mode else ["--list"])

    assert result.exit_code == 0, result.output
    assert " ".join(result.stderr.split()).count(warning) == 1
    if json_mode:
        logs = [e for e in map(json.loads, result.stdout.splitlines()) if e["type"] == "log"]
        assert [(e["source"], e["line"]) for e in logs] == [("startup", warning)]
        app = next(e for e in map(json.loads, result.stdout.splitlines()) if e["type"] == "app")
        assert app["store_country"] is None


def test_a_hand_written_alias_loads_as_its_store_with_a_note(temp_config_env: Path) -> None:
    _hand_write(temp_config_env, "UK")
    result = _run(["--list"])
    assert result.exit_code == 0, result.output
    assert "store_country 'UK' for example is the GB App Store; using GB" in " ".join(result.stderr.split())
    assert load_config(temp_config_env).apps["example"].store_country == "GB"


def test_an_unquoted_no_is_norway_not_false(temp_config_env: Path) -> None:
    """YAML 1.1 reads `store_country: NO` as False. No other country code
    collides, so False can only mean Norway."""
    text = temp_config_env.read_text().replace(
        "bundle_id: com.example.app", "bundle_id: com.example.app\n    store_country: NO", 1
    )
    assert "store_country: NO" in text, "premise: the test config's app entry"
    temp_config_env.write_text(text)
    assert yaml.safe_load(text)["apps"]["example"]["store_country"] is False, "premise: YAML 1.1"

    assert load_config(temp_config_env).apps["example"].store_country == "NO"


def test_the_writer_always_quotes_store_country(temp_config_env: Path, german_app: str) -> None:
    add_app(temp_config_env, "norsk", {"bundle_id": "com.example.no", "tweaks": [], "store_country": "NO"})
    text = temp_config_env.read_text()
    assert "store_country: 'DE'" in text
    assert "store_country: 'NO'" in text
    # And a value YAML read as False is written back as the country it meant.
    raw = yaml.safe_load(text)
    raw["apps"]["norsk"]["store_country"] = False
    temp_config_env.write_text(yaml.safe_dump(raw, sort_keys=False))
    set_app_tweaks(temp_config_env, "norsk", ["a-tweak.deb"])
    assert "store_country: 'NO'" in temp_config_env.read_text()
    assert load_config(temp_config_env).apps["norsk"].store_country == "NO"


def test_the_other_writers_keep_store_country(temp_config_env: Path, german_app: str) -> None:
    set_app_tweaks(temp_config_env, german_app, ["a-tweak.deb"])
    set_app_options(temp_config_env, german_app, preferred_version="1.0")
    assert _apps(temp_config_env)[german_app]["store_country"] == "DE"
    assert load_config(temp_config_env).apps[german_app].store_country == "DE"


# --- adding -----------------------------------------------------------------------


def test_a_search_result_is_added_with_the_store_it_came_from(temp_config_env: Path) -> None:
    result = _run([
        "--json", "--add-app-manual", "--name", "Example 7", "--bundle-id", "com.example.app7",
        "--display-name", "Example 7", "--store-country", "de",
    ])
    assert result.exit_code == 0, result.output
    assert _apps(temp_config_env)["example_7"]["store_country"] == "DE"


def test_a_manual_add_without_a_store_writes_none(temp_config_env: Path) -> None:
    result = _run(["--add-app-manual", "--name", "Example 7", "--bundle-id", "com.example.app7"])
    assert result.exit_code == 0, result.output
    assert "store_country" not in _apps(temp_config_env)["example_7"]


def test_a_store_country_with_no_store_is_a_usage_error_that_writes_nothing(temp_config_env: Path) -> None:
    before = _apps(temp_config_env)
    result = _run([
        "--add-app-manual", "--name", "Example 7", "--bundle-id", "com.example.app7",
        "--store-country", "ZZ",
    ])
    assert result.exit_code == 2, result.output
    words = " ".join(result.output.split())
    assert "no App Store for ZZ" in words and "--store-country" in words
    assert _apps(temp_config_env) == before


def test_a_mapped_store_country_is_written_as_its_store_with_a_note(temp_config_env: Path) -> None:
    result = _run([
        "--add-app-manual", "--name", "Example 7", "--bundle-id", "com.example.app7",
        "--store-country", "uk",
    ])
    assert result.exit_code == 0, result.output
    assert _apps(temp_config_env)["example_7"]["store_country"] == "GB"
    assert "UK is written GB" in " ".join(result.output.split())


def test_a_store_country_that_is_not_two_letters_is_a_usage_error(temp_config_env: Path) -> None:
    before = _apps(temp_config_env)
    result = _run([
        "--add-app-manual", "--name", "Example 7", "--bundle-id", "com.example.app7",
        "--store-country", "GBR",
    ])
    assert result.exit_code == 2, result.output
    assert "two-letter country code" in " ".join(result.output.split())
    assert _apps(temp_config_env) == before


def test_store_country_is_refused_beside_another_verb(temp_config_env: Path) -> None:
    result = _run(["--appstore-check", "--store-country", "DE"])
    assert result.exit_code == 2
    assert "--store-country is not valid with --appstore-check" in " ".join(result.output.split())


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("https://apps.apple.com/gb/app/example-7/id100000007", "GB"),
        ("https://apps.apple.com/US/app/example-7/id100000007?l=en", "US"),
        ("https://apps.apple.com/app/id100000007", None),
        ("https://apps.apple.com/app/example-7/id100000007", None),
        ("100000007", None),
    ],
)
def test_the_store_is_read_out_of_an_app_store_url(url: str, code: str | None) -> None:
    assert itunes.country_from_url(url) == code


def test_an_add_by_url_asks_and_keeps_the_urls_store(
    temp_config_env: Path, lookups: FakeLookups
) -> None:
    result = _run(["--json", "--add-app", "https://apps.apple.com/gb/app/example-7/id100000007"])

    assert result.exit_code == 0, result.output
    assert lookups.calls == [("lookup", {"id": 100000007, "country": "GB"})]
    assert _apps(temp_config_env)["example_7"]["store_country"] == "GB"


def test_a_legacy_itunes_link_keeps_its_store(temp_config_env: Path, lookups: FakeLookups) -> None:
    result = _run(["--add-app", "https://itunes.apple.com/gb/app/example-7/id100000007?mt=8"])

    assert result.exit_code == 0, result.output
    assert lookups.calls == [("lookup", {"id": 100000007, "country": "GB"})]
    assert _apps(temp_config_env)["example_7"]["store_country"] == "GB"
    assert "names no country" not in " ".join(result.stdout.split())


def test_an_add_by_url_with_no_store_asks_this_macs_store_and_says_so(
    temp_config_env: Path, lookups: FakeLookups, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host, "_read_apple_locale", lambda: "en_US@rg=dezzzz")
    result = _run(["--add-app", "https://apps.apple.com/app/example-7/id100000007"])

    assert result.exit_code == 0, result.output
    assert lookups.calls == [("lookup", {"id": 100000007, "country": "DE"})]
    assert "store_country" not in _apps(temp_config_env)["example_7"], "the chain, not a pinned store"
    words = " ".join(result.stdout.split())
    assert "names no country" in words and "DE App Store" in words


def test_adding_a_result_writes_what_its_countrys_url_writes(
    temp_config_env: Path, lookups: FakeLookups
) -> None:
    """Task 6's equivalence, for a store other than the US: Search sends the
    store's name, bundle id and country; the URL from that store names the same."""
    by_url = _run([
        "--add-app", "https://apps.apple.com/de/app/example-7/id100000007",
        "--delivery", "livecontainer",
    ])
    by_search = _run([
        "--add-app-manual", "--name", "Example 7", "--bundle-id", "com.example.app7",
        "--display-name", "Example 7", "--delivery", "livecontainer", "--store-country", "DE",
    ])

    assert by_url.exit_code == 0 and by_search.exit_code == 0, by_url.output + by_search.output
    apps = _apps(temp_config_env)
    assert apps["example_7"] == apps["example_7_2"]
    assert apps["example_7"]["store_country"] == "DE"


# --- looking it up ------------------------------------------------------------------


def test_the_app_store_check_asks_the_apps_own_store(
    temp_config_env: Path, german_app: str, lookups: FakeLookups
) -> None:
    result = _run(["--json", "--appstore-check", "--app", german_app])

    assert result.exit_code == 0, result.output
    assert lookups.calls == [("lookup", {"bundleId": "com.example.app7", "country": "DE"})]
    event = json.loads(result.stdout.splitlines()[0])
    assert (event["type"], event["store_version"], event["error"]) == ("appstore-version", "7.0", None)


def test_artwork_asks_the_apps_own_store(
    temp_config_env: Path, german_app: str, lookups: FakeLookups
) -> None:
    result = _run(["--json", "--artwork", "--app", german_app])

    assert result.exit_code == 0, result.output
    assert lookups.calls == [("lookup", {"bundleId": "com.example.app7", "country": "DE"})]


@pytest.mark.parametrize("verb", ["--appstore-check", "--artwork"])
@pytest.mark.parametrize(("locale", "store"), [(None, "US"), ("de_DE", "DE"), ("es_PR", "US")])
def test_an_app_without_a_store_asks_this_macs_store_else_the_us(
    temp_config_env: Path, lookups: FakeLookups, monkeypatch: pytest.MonkeyPatch,
    verb: str, locale: str | None, store: str,
) -> None:
    monkeypatch.setattr(host, "_read_apple_locale", lambda: locale)
    result = _run(["--json", verb, "--app", "example"])

    assert result.exit_code == 0, result.output
    assert lookups.calls == [("lookup", {"bundleId": "com.example.app", "country": store})]


def test_every_lookup_of_an_app_shares_one_fallback_chain(
    temp_config_env: Path, lookups: FakeLookups, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The update check, the artwork rung, the free-price check and an add
    by a URL naming no store all ask `host.lookup_countries` through
    `host.look_up`, so they can't drift apart: one asked the US, another
    this Mac's region, before."""
    from graft_ipa import appstore

    asked: list[str | None] = []

    def chain(store_country: str | None) -> tuple[str, ...]:
        asked.append(store_country)
        return (store_country,) if store_country else ("JP",)

    monkeypatch.setattr(host, "lookup_countries", chain)

    for args in (["--json", "--appstore-check", "--app", "example"],
                 ["--json", "--artwork", "--app", "example"],
                 ["--json", "--add-app", "https://apps.apple.com/app/example-7/id100000007"]):
        result = _run(args)
        assert result.exit_code == 0, result.output
    appstore._listing(load_config(temp_config_env).get_app("example"))

    assert lookups.countries == ["JP", "JP", "JP", "JP"]
    assert asked == [None, None, None, None]


class StoresWith(FakeLookups):
    """Only the stores in `has` list app 7; every other store answers with
    no results, as Apple's does for an app it doesn't sell."""

    def __init__(self, *has: str) -> None:
        super().__init__()
        self.has = set(has)

    def __call__(self, endpoint: str, params: dict, *, described: str) -> dict:
        found = super().__call__(endpoint, params, described=described)
        return found if params.get("country") in self.has else {"resultCount": 0, "results": []}


@pytest.fixture
def gb_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "_read_apple_locale", lambda: "en_GB")


def test_a_1x_app_the_macs_store_doesnt_sell_is_found_in_the_us_store(
    temp_config_env: Path, gb_mac: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Combined fix check #21: an app added in 1.x has no store_country, and
    every 1.x lookup asked the US store. On a GB Mac it is asked in GB
    first, then in the US, and the US answer wins for this lookup."""
    stores = StoresWith("US")
    monkeypatch.setattr(itunes, "_get_json", stores)
    result = _run(["--json", "--appstore-check", "--app", "example"])

    assert result.exit_code == 0, result.output
    assert stores.countries == ["GB", "US"]
    event = json.loads(result.stdout.splitlines()[0])
    assert (event["store_version"], event["error"]) == ("7.0", None)
    assert "store_country" not in _apps(temp_config_env)["example"], "nothing is written"


def test_the_macs_store_wins_when_it_has_the_app(
    temp_config_env: Path, gb_mac: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    stores = StoresWith("GB", "US")
    monkeypatch.setattr(itunes, "_get_json", stores)
    assert _run(["--json", "--appstore-check", "--app", "example"]).exit_code == 0
    assert stores.countries == ["GB"]


def test_an_app_with_its_own_store_is_never_asked_elsewhere(
    temp_config_env: Path, german_app: str, gb_mac: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    stores = StoresWith("US")
    monkeypatch.setattr(itunes, "_get_json", stores)
    result = _run(["--json", "--appstore-check", "--app", german_app])
    event = json.loads(result.stdout.splitlines()[0])
    assert stores.countries == ["DE"]
    assert event["error"] == "no app found for bundle id com.example.app7 in the DE App Store"


def test_when_no_store_has_it_the_error_names_every_store_tried(
    temp_config_env: Path, gb_mac: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(itunes, "_get_json", StoresWith())
    result = _run(["--json", "--appstore-check", "--app", "example"])
    event = json.loads(result.stdout.splitlines()[0])
    assert event["error"] == "no app found for bundle id com.example.app in the GB or US App Store"


def test_an_offline_lookup_is_not_retried_in_the_us_store(
    temp_config_env: Path, gb_mac: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str | None] = []

    def offline(endpoint: str, params: dict, *, described: str) -> dict:
        asked.append(params.get("country"))
        raise itunes.ITunesRequestError("iTunes lookup request failed: offline", reason=itunes.OFFLINE)

    monkeypatch.setattr(itunes, "_get_json", offline)
    result = _run(["--json", "--appstore-check", "--app", "example"])
    assert asked == ["GB"]
    assert "offline" in json.loads(result.stdout.splitlines()[0])["error"]


def test_a_countryless_link_for_a_us_only_app_on_a_gb_mac_is_added(
    temp_config_env: Path, gb_mac: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Combined fix check #22: the README's countryless link form."""
    stores = StoresWith("US")
    monkeypatch.setattr(itunes, "_get_json", stores)
    result = _run(["--add-app", "https://apps.apple.com/app/example-7/id100000007"])

    assert result.exit_code == 0, result.output
    assert [p for _, p in stores.calls] == [{"id": 100000007, "country": "GB"},
                                            {"id": 100000007, "country": "US"}]
    assert "store_country" not in _apps(temp_config_env)["example_7"], "for this lookup only"
    assert "found in the US App Store" in " ".join(result.stdout.split())


@pytest.mark.parametrize("json_mode", [True, False])
def test_a_countryless_link_no_store_has_names_the_stores_not_the_url(
    temp_config_env: Path, gb_mac: None, monkeypatch: pytest.MonkeyPatch, json_mode: bool
) -> None:
    monkeypatch.setattr(itunes, "_get_json", StoresWith())
    result = _run([*(["--json"] if json_mode else []), "--add-app", "https://apps.apple.com/app/x/id100000007"])

    assert result.exit_code == 13
    words = " ".join(result.output.split())
    assert "no app found for App Store ID 100000007 in the GB or US App Store" in words
    assert "double-check the URL" not in words
    assert "isn't sold in that store" in words


@pytest.mark.parametrize("url", ["itms-apps://apps.apple.com/gb/app/example-7/id100000007",
                                 "https://geo.itunes.apple.com/gb/app/example-7/id100000007?mt=8"])
def test_an_app_store_scheme_or_link_maker_link_is_added_with_its_store(
    temp_config_env: Path, lookups: FakeLookups, url: str
) -> None:
    result = _run(["--json", "--add-app", url])
    assert result.exit_code == 0, result.output
    assert lookups.calls == [("lookup", {"id": 100000007, "country": "GB"})]
    assert _apps(temp_config_env)["example_7"]["store_country"] == "GB"


def test_the_artwork_rung_falls_back_to_the_us_store(
    temp_config_env: Path, gb_mac: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    stores = StoresWith("US")
    monkeypatch.setattr(itunes, "_get_json", stores)
    assert _run(["--json", "--artwork", "--app", "example"]).exit_code == 0
    assert stores.countries == ["GB", "US"]


def test_the_free_price_check_falls_back_to_the_us_store(
    temp_config_env: Path, gb_mac: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graft_ipa import appstore

    stores = StoresWith("US")
    monkeypatch.setattr(itunes, "_get_json", stores)
    listing_ = appstore._listing(load_config(temp_config_env).get_app("example"))
    assert listing_ is not None and listing_.price == 0.0
    assert stores.countries == ["GB", "US"]


# --- seeing it, and changing it -------------------------------------------------------


def test_the_list_carries_each_apps_store(temp_config_env: Path, german_app: str) -> None:
    result = _run(["--json", "--list"])
    assert result.exit_code == 0, result.output
    apps = {e["name"]: e for e in map(json.loads, result.stdout.splitlines()) if e["type"] == "app"}
    assert apps[german_app]["store_country"] == "DE"
    assert apps["example"]["store_country"] is None


def test_the_terminal_list_shows_each_apps_store(temp_config_env: Path, german_app: str) -> None:
    result = _run(["--list"])
    assert result.exit_code == 0, result.output
    row = next(line for line in result.stdout.splitlines() if german_app in line)
    assert " DE " in row


def test_set_app_options_changes_the_store(temp_config_env: Path, german_app: str) -> None:
    result = _run(["--json", "--set-app-options", "--app", german_app, "--store-country", "gb"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "type": "app-updated", "name": german_app, "changed": ["store_country"],
    }
    assert _apps(temp_config_env)[german_app]["store_country"] == "GB"


def test_set_app_options_maps_an_alias_and_refuses_a_code_with_no_store(
    temp_config_env: Path, german_app: str
) -> None:
    mapped = _run(["--set-app-options", "--app", german_app, "--store-country", "UK"])
    assert mapped.exit_code == 0, mapped.output
    assert _apps(temp_config_env)[german_app]["store_country"] == "GB"
    assert "UK is written GB" in " ".join(mapped.output.split())

    refused = _run(["--set-app-options", "--app", german_app, "--store-country", "ZZ"])
    assert refused.exit_code == 2
    assert "no App Store for ZZ" in " ".join(refused.output.split())
    assert _apps(temp_config_env)[german_app]["store_country"] == "GB"
