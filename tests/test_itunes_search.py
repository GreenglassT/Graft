"""`itunes.search`: the iTunes Search API, with the network faked.

`FakeITunes` stands in for `requests.get`, so nothing here reaches Apple.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from graft_ipa import USER_AGENT, itunes
from graft_ipa.errors import ITunesError
from tests.itunes_fakes import SEARCH_URL, FakeITunes, listing


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeITunes:
    f = FakeITunes()
    monkeypatch.setattr(requests, "get", f)
    return f


# --- the request -------------------------------------------------------------


def test_a_search_asks_one_store_for_iphone_software(fake: FakeITunes) -> None:
    itunes.search("photo editor", country="GB", limit=10)

    (url, params, kwargs), = fake.calls
    assert url == SEARCH_URL
    assert params == {"term": "photo editor", "country": "GB", "entity": "software", "limit": 10}
    assert kwargs["headers"]["User-Agent"] == USER_AGENT
    assert kwargs["timeout"] == itunes.HTTP_TIMEOUT


def test_the_default_limit_is_25(fake: FakeITunes) -> None:
    itunes.search("example", country="US")
    assert fake.searches[0]["limit"] == itunes.SEARCH_DEFAULT_LIMIT == 25


@pytest.mark.parametrize(("asked", "sent"), [(1, 1), (50, 50), (51, 50), (500, 50), (0, 1), (-3, 1)])
def test_the_limit_is_kept_between_1_and_50(fake: FakeITunes, asked: int, sent: int) -> None:
    itunes.search("example", country="US", limit=asked)
    assert fake.searches[0]["limit"] == sent


# --- what comes back ---------------------------------------------------------


def test_each_listing_becomes_a_result_in_the_stores_order(fake: FakeITunes) -> None:
    fake.results = [listing(1), listing(2, price=4.99, formattedPrice="$4.99")]

    first, second = itunes.search("example", country="US")

    assert first == itunes.SearchResult(
        track_id=100000001, bundle_id="com.example.app1", name="Example 1", developer="Dev 1",
        version="1.0", price=0.0, formatted_price="Free", currency="USD",
        artwork_url="https://is1.example.invalid/1/512x512bb.jpg",
        icon_url="https://is1.example.invalid/1/100x100bb.jpg",
    )
    assert (second.price, second.formatted_price) == (4.99, "$4.99")


def test_the_developer_falls_back_to_the_seller(fake: FakeITunes) -> None:
    entry = listing(1)
    del entry["artistName"]
    fake.results = [entry]
    assert itunes.search("example", country="US")[0].developer == "Seller 1"


def test_icon_and_artwork_urls_fall_back_by_size(fake: FakeITunes) -> None:
    only_60 = {k: v for k, v in listing(1).items() if k not in ("artworkUrl100", "artworkUrl512")}
    no_art = {k: v for k, v in listing(2).items() if not k.startswith("artworkUrl")}
    fake.results = [only_60, no_art]

    a, b = itunes.search("example", country="US")

    # The icon is the smallest worth drawing in a list; the artwork the
    # largest, by the same 512-then-100 rule `ITunesApp.artwork_url` uses.
    assert (a.icon_url, a.artwork_url) == ("https://is1.example.invalid/1/60x60bb.jpg", None)
    assert (b.icon_url, b.artwork_url) == (None, None)


@pytest.mark.parametrize(
    "broken",
    [
        "not an object",
        {"trackId": 100000009, "trackName": "No Bundle", "version": "1.0"},
        {**listing(9), "trackId": "100000009"},
        {**listing(9), "trackId": True},
        {**listing(9), "version": 7},
        {**listing(9), "bundleId": ""},
    ],
)
def test_a_listing_that_cannot_be_used_is_skipped_not_fatal(fake: FakeITunes, broken) -> None:
    fake.results = [listing(1), broken, listing(2)]
    found = itunes.search("example", country="US")
    assert [r.bundle_id for r in found] == ["com.example.app1", "com.example.app2"]


def test_a_price_that_is_not_a_number_reads_as_none(fake: FakeITunes) -> None:
    fake.results = [listing(1, price="free"), listing(2, price=True)]
    assert [r.price for r in itunes.search("example", country="US")] == [None, None]


def test_no_matches_is_an_empty_list_not_an_error(fake: FakeITunes) -> None:
    assert itunes.search("zzzz", country="US") == []


# --- failures: one reason each, in plain words -------------------------------


@pytest.mark.parametrize(
    "error", [requests.ConnectionError("Max retries exceeded with url: /search"),
              requests.Timeout("Read timed out.")],
)
def test_no_answer_is_offline_said_plainly(fake: FakeITunes, error: Exception) -> None:
    fake.error = error

    with pytest.raises(itunes.ITunesRequestError) as caught:
        itunes.search("example", country="US")

    e = caught.value
    assert e.reason == itunes.OFFLINE
    assert e.exit_code == ITunesError.exit_code == 13
    assert e.message == "couldn't reach the App Store to search"
    assert "internet connection" in (e.hint or "")
    assert "Max retries" not in str(e) and "timed out" not in str(e), "the plain sentence only"


def test_an_http_error_names_the_status(fake: FakeITunes) -> None:
    fake.status = 500

    with pytest.raises(itunes.ITunesRequestError) as caught:
        itunes.search("example", country="US")

    assert (caught.value.reason, caught.value.status) == (itunes.HTTP, 500)
    assert caught.value.message == "the App Store search answered HTTP 500"


@pytest.mark.parametrize("status", [403, 429, 503])
def test_apples_rate_limit_is_its_own_reason_in_plain_words(fake: FakeITunes, status: int) -> None:
    """Apple's per-IP limit on the Search API (roughly 20 a minute) is answered
    with one of these. Never tested live: the fake stands in for it."""
    fake.status = status

    with pytest.raises(itunes.ITunesRequestError) as caught:
        itunes.search("example", country="US")

    e = caught.value
    assert (e.reason, e.status) == (itunes.RATE_LIMITED, status) == ("rate-limited", status)
    assert e.message == itunes.RATE_LIMITED_MESSAGE
    assert itunes.RATE_LIMITED_MESSAGE == (
        "Apple is limiting searches from this Mac for a minute. Try again shortly."
    )
    assert e.exit_code == 13


def test_a_400_names_the_store_and_never_says_try_again(fake: FakeITunes) -> None:
    """Apple's answer for a store it doesn't have (`{"errorMessage": "Invalid
    value(s) for key(s): [country]"}`). Retrying can never help."""
    fake.status = 400
    fake.payload = {"errorMessage": "Invalid value(s) for key(s): [country]"}

    with pytest.raises(itunes.ITunesRequestError) as caught:
        itunes.search("example", country="GB")

    e = caught.value
    assert (e.reason, e.status) == (itunes.HTTP, 400)
    assert "GB" in e.message and "400" in e.message
    assert "GB" in (e.hint or "")
    assert "try again" not in f"{e.message} {e.hint}".lower()


@pytest.mark.parametrize("shape", ["not json", "a list", "results is a string"])
def test_an_answer_that_is_not_a_result_list_is_malformed(fake: FakeITunes, shape: str) -> None:
    if shape == "not json":
        fake.bad_json = True
    elif shape == "a list":
        fake.payload = [1, 2]
    else:
        fake.payload = {"resultCount": 1, "results": "oops"}

    with pytest.raises(itunes.ITunesRequestError) as caught:
        itunes.search("example", country="US")

    assert caught.value.reason == itunes.MALFORMED
    assert caught.value.message == "the App Store search answered with something that isn't a list of apps"


def test_a_lookup_failure_carries_a_reason_and_keeps_its_old_message() -> None:
    """The split moved the lookups' HTTP into `_get_json`; their messages are
    what they were (tests/test_lazy_imports.py pins one too)."""
    resp = MagicMock(status_code=503)
    with patch("requests.get", return_value=resp):
        with pytest.raises(itunes.ITunesRequestError) as caught:
            itunes.lookup(100000001)
    assert caught.value.reason == itunes.HTTP
    assert caught.value.message == "iTunes lookup returned HTTP 503 for App Store ID 100000001"


def test_the_rate_limited_reason_and_sentence_are_the_ones_the_app_reads() -> None:
    import re

    from tests.cli_ast import contract_swift_source, strip_swift_comments

    source = contract_swift_source()
    match = re.search(
        r"enum AppStoreSearchFailure \{.*?static let rateLimited = \"([^\"\\]*)\"", source, re.S
    )
    assert match, "CLIContract.AppStoreSearchFailure.rateLimited is gone from CLIContract.swift"
    assert itunes.RATE_LIMITED == match.group(1)
    records = (Path(__file__).resolve().parent.parent / "Graft" / "Graft" / "Models" / "AppStoreRecords.swift")
    assert f'"{itunes.RATE_LIMITED_MESSAGE}"' in strip_swift_comments(records.read_text())


def test_the_offline_reason_is_the_word_the_app_reads() -> None:
    import re

    from tests.cli_ast import contract_swift_source

    match = re.search(
        r"enum AppStoreSearchFailure \{.*?static let offline = \"([^\"\\]*)\"",
        contract_swift_source(),
        re.S,
    )
    assert match, "CLIContract.AppStoreSearchFailure.offline is gone from CLIContract.swift"
    assert itunes.OFFLINE == match.group(1)
