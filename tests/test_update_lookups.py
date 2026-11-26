"""The App Store versions an update plan compares against: batched, one
request per store country (spec 3.1)."""

from __future__ import annotations

import pytest
import requests

from graft_ipa import host, itunes, update_plan
from graft_ipa.config import AppConfig, CyanOptions
from graft_ipa.errors import ITunesError

from .itunes_fakes import FakeITunes, listing


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeITunes:
    fake = FakeITunes()
    monkeypatch.setattr(requests, "get", fake)
    return fake


def test_many_bundle_ids_are_one_request(store: FakeITunes) -> None:
    store.stores["GB"] = [listing(1, version="2.0"), listing(2, version="5.1")]

    found = itunes.lookup_by_bundle_ids(
        ["com.example.app1", "com.example.app2", "com.example.app3"], country="GB"
    )

    assert [p["bundleId"] for p in store.lookups] == ["com.example.app1,com.example.app2,com.example.app3"]
    assert store.lookups[0]["country"] == "GB"
    assert {k: v.version for k, v in found.items()} == {"com.example.app1": "2.0", "com.example.app2": "5.1"}


def test_an_unreadable_listing_is_skipped_not_fatal(store: FakeITunes) -> None:
    store.stores[None] = [{"bundleId": "com.example.app1"}, listing(2)]

    found = itunes.lookup_by_bundle_ids(["com.example.app1", "com.example.app2"])

    assert list(found) == ["com.example.app2"]


def test_a_failed_request_raises(store: FakeITunes) -> None:
    store.error = requests.ConnectionError("offline")

    with pytest.raises(ITunesError):
        itunes.lookup_by_bundle_ids(["com.example.app1"], country="US")


def test_no_ids_asks_nothing(store: FakeITunes) -> None:
    assert itunes.lookup_by_bundle_ids([]) == {}
    assert store.lookups == []



def _app(name: str, bundle_id: str, store_country: str | None = None) -> AppConfig:
    return AppConfig(name=name, bundle_id=bundle_id, preferred_version="latest", tweaks=[],
                     cyan=CyanOptions(), store_country=store_country)


def test_each_store_country_is_asked_once_for_all_its_apps(
    store: FakeITunes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two apps with no store on a GB Mac look in GB, then the US; one found
    in Germany looks there alone. Three stores, three requests, not one per
    app and store."""
    monkeypatch.setattr(host, "mac_region", lambda: "GB")
    store.stores["GB"] = [listing(1, version="2.0")]
    store.stores["US"] = [listing(2, version="5.1")]
    store.stores["DE"] = [listing(3, version="9.0")]
    apps = [_app("example", "com.example.app1"), _app("bravo", "com.example.app2"),
            _app("delta", "com.example.app3", "DE")]

    answers = update_plan.store_versions(apps)

    assert sorted((p.get("country"), p["bundleId"]) for p in store.lookups) == [
        ("DE", "com.example.app3"),
        ("GB", "com.example.app1,com.example.app2"),
        ("US", "com.example.app1,com.example.app2"),
    ]
    assert {n: a.version for n, a in answers.items()} == {"example": "2.0", "bravo": "5.1", "delta": "9.0"}


def test_a_store_that_failed_is_couldnt_check_not_a_fallback(
    store: FakeITunes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As in `host.look_up`: only "not sold here" moves on down the chain."""
    calls: list[str] = []

    def ask(ids: list[str], country: str) -> dict[str, itunes.ITunesApp]:
        calls.append(country)
        if country == "GB":
            raise ITunesError("iTunes lookup request failed: offline")
        return {"com.example.app1": itunes.ITunesApp(track_id=1, bundle_id="com.example.app1",
                                                     name="x", version="2.0")}

    monkeypatch.setattr(host, "mac_region", lambda: "GB")
    answers = update_plan.store_versions([_app("example", "com.example.app1")], ask=ask)

    assert answers["example"].version is None
    assert "offline" in (answers["example"].error or "")


def test_an_app_no_store_sells_is_couldnt_check(store: FakeITunes) -> None:
    answers = update_plan.store_versions([_app("example", "com.example.app1")])

    assert answers["example"].version is None
    assert answers["example"].error


def test_this_macs_region_is_read_once_for_every_app_with_no_store(
    store: FakeITunes, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[None] = []

    def region() -> str:
        asked.append(None)
        return "GB"

    monkeypatch.setattr(host, "mac_region", region)
    apps = [_app(f"app{n}", f"com.example.app{n}") for n in range(1, 4)] + [
        _app("delta", "com.example.app9", "DE")
    ]

    update_plan.store_versions(apps)

    assert len(asked) == 1
