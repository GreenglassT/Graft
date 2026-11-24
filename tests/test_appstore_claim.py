"""Getting an app the Apple ID doesn't own yet: the refusal as data
(`needs-license`, with the price), and `--appstore-claim`'s $0 purchase.

The price is always the one in the app's own store: its `store_country`, else
this Mac's region, else the US (controller decision 4). Never assumed US.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from graft_ipa import appstore, host, ipatool, itunes
from graft_ipa import cli as cli_mod
from graft_ipa.errors import ITunesError
from tests.appstore_fakes import FakeTools, itunes_offline


@pytest.fixture(autouse=True)
def _no_price_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never on the network: a test that wants a listing answers it (`_listing`)."""
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


def _set_store_country(config: Path, country: str) -> None:
    raw = yaml.safe_load(config.read_text())
    raw["apps"]["example"]["store_country"] = country
    config.write_text(yaml.safe_dump(raw, sort_keys=False))


def _listing(
    monkeypatch: pytest.MonkeyPatch, price: float | None, formatted: str | None = None
) -> list[str | None]:
    """The iTunes lookup, answered without the network. Returns the list of
    stores it was asked, in order."""
    asked: list[str | None] = []

    def lookup(bundle_id: str, *, country: str | None = None) -> itunes.ITunesApp:
        asked.append(country)
        if price is None:
            raise ITunesError("iTunes lookup request failed: no network")
        return itunes.ITunesApp(
            track_id=100000001, bundle_id=bundle_id, name="Example", version="2.0",
            price=price, formatted_price=formatted,
        )

    monkeypatch.setattr(itunes, "lookup_by_bundle_id", lookup)
    return asked


def _run(args: list[str]):
    return CliRunner().invoke(cli_mod.app, args)


def _events(result) -> list[dict]:
    return [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]


def test_the_listing_carries_the_price() -> None:
    app = itunes._app_from_result(
        {"trackId": 1, "bundleId": "com.example.app", "trackName": "Example", "version": "1",
         "price": 0.0, "formattedPrice": "Free"},
        described="x",
    )
    assert (app.price, app.formatted_price) == (0.0, "Free")


def test_a_listing_with_a_strange_price_has_none() -> None:
    app = itunes._app_from_result(
        {"trackId": 1, "bundleId": "com.example.app", "trackName": "Example", "version": "1",
         "price": True, "formattedPrice": 3},
        described="x",
    )
    assert (app.price, app.formatted_price) == (None, None)


def test_licence_required_is_its_own_error(fakes: FakeTools, tmp_path: Path) -> None:
    fakes.set(download_error="license is required")
    (tmp_path / "out").mkdir()
    with pytest.raises(ipatool.LicenseRequired) as e:
        ipatool.download(fakes.ipatool, "com.example.app", tmp_path / "out")
    assert "tap Get" in e.value.hint


def test_a_free_app_not_owned_is_refused_as_needs_license_with_its_price(
    config: Path, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes.set(download_error="license is required")
    _listing(monkeypatch, 0.0, "Free")

    result = _run(["--json", "--appstore-get", "--app", "example"])

    assert result.exit_code == 20
    assert {
        "type": "appstore-refused", "app": "example", "reason": "needs-license",
        "price": 0.0, "formatted_price": "Free", "track_id": 100000001,
    } in _events(result)
    assert "--appstore-claim --app example" in " ".join(result.output.split())


def test_a_paid_app_not_owned_says_to_buy_it(
    config: Path, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes.set(download_error="license is required")
    _listing(monkeypatch, 2.99, "$2.99")
    result = _run(["--json", "--appstore-get", "--app", "example"])
    (refused,) = [e for e in _events(result) if e["type"] == "appstore-refused"]
    assert refused["price"] == 2.99
    assert "buy it in the App Store" in " ".join(result.output.split())


def test_when_the_price_cant_be_looked_up_the_refusal_still_comes(
    config: Path, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes.set(download_error="license is required")
    _listing(monkeypatch, None)
    result = _run(["--json", "--appstore-get", "--app", "example"])
    (refused,) = [e for e in _events(result) if e["type"] == "appstore-refused"]
    assert (refused["reason"], refused["price"]) == ("needs-license", None)


#: (the app's `store_country`, this Mac's region, the store asked)
STORES = [("GB", "DE", "GB"), (None, "DE", "DE"), (None, None, "US")]


@pytest.mark.parametrize(("store_country", "region", "asked_of"), STORES)
def test_the_refusal_asks_the_price_of_the_apps_store_else_the_macs_else_the_us(
    config: Path, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch,
    store_country: str | None, region: str | None, asked_of: str,
) -> None:
    """An app can be free in one store and paid in another (decision 4)."""
    if store_country:
        _set_store_country(config, store_country)
    monkeypatch.setattr(host, "mac_region", lambda: region)
    fakes.set(download_error="license is required")
    asked = _listing(monkeypatch, 0.0, "Free")
    _run(["--json", "--appstore-get", "--app", "example"])
    assert asked == [asked_of]


def test_the_refusal_words_are_the_ones_the_app_reads() -> None:
    from tests.cli_ast import contract_swift_source

    source = contract_swift_source()
    for name, value in (("needsLicense", appstore.REFUSED_NEEDS_LICENSE), ("paid", appstore.REFUSED_PAID)):
        match = re.search(rf'enum AppStoreRefusal \{{.*?static let {name} = "([^"\\]*)"', source, re.S)
        assert match, f"CLIContract.AppStoreRefusal.{name} is gone"
        assert match.group(1) == value


# --- --appstore-claim -----------------------------------------------------------


def _purchases(fakes: FakeTools) -> list[list[str]]:
    return [c["argv"] for c in fakes.calls("ipatool") if c["argv"][:1] == ["purchase"]]


def test_a_free_app_is_claimed_with_ipatools_purchase(
    config: Path, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    _listing(monkeypatch, 0.0, "Free")
    result = _run(["--json", "--appstore-claim", "--app", "example"])
    assert result.exit_code == 0, result.output
    assert _purchases(fakes) == [["purchase", "-b", "com.example.app", "--format", "json", "--non-interactive"]]
    assert _events(result) == [{
        "type": "appstore-claimed", "app": "example", "bundle_id": "com.example.app",
        "already_owned": False,
    }]


def test_an_app_already_owned_is_said_so(
    config: Path, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes.set(already_owned=True)
    _listing(monkeypatch, 0.0, "Free")
    result = _run(["--json", "--appstore-claim", "--app", "example"])
    assert _events(result)[-1]["already_owned"] is True


def test_without_json_the_claim_is_one_sentence(
    config: Path, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    _listing(monkeypatch, 0.0, "Free")
    result = _run(["--appstore-claim", "--app", "example"])
    assert result.exit_code == 0, result.output
    assert "example: added to this Apple ID" in " ".join(result.output.split())


def test_a_paid_app_is_refused_and_never_purchased(
    config: Path, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    _listing(monkeypatch, 2.99, "$2.99")
    result = _run(["--json", "--appstore-claim", "--app", "example"])
    assert result.exit_code == 20
    assert _purchases(fakes) == []
    (refused,) = [e for e in _events(result) if e["type"] == "appstore-refused"]
    assert (refused["reason"], refused["price"]) == ("paid", 2.99)


def test_an_unknown_price_is_refused_and_never_purchased(
    config: Path, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    _listing(monkeypatch, None)
    result = _run(["--json", "--appstore-claim", "--app", "example"])
    assert result.exit_code == 20
    assert _purchases(fakes) == []
    assert "couldn't check" in " ".join(result.output.split())


def test_a_listing_with_no_price_is_refused_and_never_purchased(
    config: Path, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listing that answers but names no price is not "free" either."""
    monkeypatch.setattr(itunes, "lookup_by_bundle_id", lambda bundle_id, *, country=None: itunes.ITunesApp(
        track_id=100000001, bundle_id=bundle_id, name="Example", version="2.0",
    ))
    result = _run(["--json", "--appstore-claim", "--app", "example"])
    assert result.exit_code == 20
    assert _purchases(fakes) == []


@pytest.mark.parametrize(("store_country", "region", "asked_of"), STORES)
def test_the_claim_checks_the_price_in_the_apps_store_else_the_macs_else_the_us(
    config: Path, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch,
    store_country: str | None, region: str | None, asked_of: str,
) -> None:
    """Decision 4: free in the US store is not free in the app's own."""
    if store_country:
        _set_store_country(config, store_country)
    monkeypatch.setattr(host, "mac_region", lambda: region)
    asked = _listing(monkeypatch, 0.0, "Free")
    result = _run(["--json", "--appstore-claim", "--app", "example"])
    assert result.exit_code == 0, result.output
    assert asked == [asked_of]


def test_signed_out_is_refused_before_anything_is_purchased(
    config: Path, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes.set(signed_in=False)
    _listing(monkeypatch, 0.0, "Free")
    result = _run(["--json", "--appstore-claim", "--app", "example"])
    assert result.exit_code == 20
    assert _purchases(fakes) == []
    assert ipatool.LOGIN_COMMAND in " ".join(result.output.split())


def test_a_known_bad_ipatool_is_refused_before_anything_is_purchased(
    config: Path, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes.set(ipatool_version="2.8.1")
    _listing(monkeypatch, 0.0, "Free")
    result = _run(["--json", "--appstore-claim", "--app", "example"])
    assert result.exit_code == 20
    assert _purchases(fakes) == []
    assert "majd/ipatool#672" in " ".join(result.output.split())


@pytest.mark.parametrize(
    ("error", "says"),
    [
        ("failed to purchase item with param 'STDQ': Account Not In This Store", "different country"),
        ("purchasing paid apps is not supported", "isn't free"),
        ("password token is expired", "not signed in"),
        ("subscription required", "Apple Arcade"),
        ("item is temporarily unavailable", "isn't available"),
    ],
)
def test_purchase_failures_say_what_happened(
    config: Path, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch, error: str, says: str
) -> None:
    fakes.set(purchase_error=error)
    _listing(monkeypatch, 0.0, "Free")
    result = _run(["--json", "--appstore-claim", "--app", "example"])
    assert result.exit_code == 20
    assert says in " ".join(result.output.split())


def test_claim_needs_an_app(config: Path) -> None:
    result = _run(["--json", "--appstore-claim"])
    assert "--appstore-claim requires --app" in " ".join(result.output.split())


def test_claim_takes_no_other_verbs_flags(config: Path) -> None:
    result = _run(["--json", "--appstore-claim", "--app", "example", "--allow-unverified"])
    assert result.exit_code != 0
    assert "--allow-unverified is not valid with --appstore-claim" in " ".join(result.output.split())
