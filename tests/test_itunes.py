from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from graft_ipa import itunes
from graft_ipa.itunes import ITunesError

# The patch target is `requests.get`, not `graft_ipa.itunes.requests.get`:
# `itunes._query` imports `requests` inside the function now (measured startup
# cost — see that import's comment), so the module has no `requests` attribute
# to reach through. The object being replaced is unchanged either way — the old
# target resolved to this same module — but the name has to be spelled at the
# library, and this file has to import it eagerly to do so. Test processes have
# no startup budget to protect; `tests/test_lazy_imports.py` is what guards the
# CLI's.


def test_parse_id_from_url_full_url() -> None:
    assert itunes.parse_id_from_url(
        "https://apps.apple.com/us/app/bravo/id100000001"
    ) == 100000001


def test_parse_id_from_url_with_query_string() -> None:
    assert itunes.parse_id_from_url(
        "https://apps.apple.com/us/app/bravo/id100000001?platform=iphone"
    ) == 100000001


@pytest.mark.parametrize("url", [
    # Apple serves these too, with no app name between /app/ and /id; the
    # README's own example is the second form.
    "https://apps.apple.com/us/app/id100000001",
    "https://apps.apple.com/app/id100000001",
    "https://apps.apple.com/app/id100000001?mt=8",
])
def test_parse_id_from_url_without_a_name_in_the_path(url: str) -> None:
    assert itunes.parse_id_from_url(url) == 100000001


def test_parse_id_from_url_bare_numeric_id() -> None:
    assert itunes.parse_id_from_url("100000001") == 100000001
    assert itunes.parse_id_from_url("  100000001  ") == 100000001


def test_parse_id_from_url_rejects_garbage() -> None:
    with pytest.raises(ITunesError):
        itunes.parse_id_from_url("https://example.com/not-an-app")
    with pytest.raises(ITunesError):
        itunes.parse_id_from_url("")


def test_parse_id_from_url_rejects_developer_portal_urls() -> None:
    # `/developer/.../id<digits>` is a developer entity ID, not an app track ID;
    # iTunes lookup against it returns a wrong-type result, so we must refuse it.
    with pytest.raises(ITunesError):
        itunes.parse_id_from_url("https://apps.apple.com/us/developer/bravo-inc/id100000003")


# --- the link, parsed as a URL (`parse_app_link`) --------------------------------


@pytest.mark.parametrize(
    ("url", "track_id", "country"),
    [
        ("https://apps.apple.com/us/app/bravo/id100000001", 100000001, "US"),
        ("https://apps.apple.com/app/bravo/id100000001", 100000001, None),
        ("https://apps.apple.com/gb/app/id100000001", 100000001, "GB"),
        ("https://apps.apple.com/app/id100000001", 100000001, None),
        ("https://apps.apple.com/de/app/bravo/id100000001?platform=iphone#reviews", 100000001, "DE"),
        ("https://apps.apple.com/jp/app/bravo/id100000001/", 100000001, "JP"),
        ("apps.apple.com/fr/app/bravo/id100000001", 100000001, "FR"),
        ("http://apps.apple.com/us/app/bravo/id100000001", 100000001, "US"),
        ("  https://APPS.apple.com/US/app/bravo/id100000001  ", 100000001, "US"),
        # The legacy form, which Apple still redirects: it keeps its country.
        ("https://itunes.apple.com/gb/app/bravo/id100000001?mt=8", 100000001, "GB"),
        ("https://itunes.apple.com/app/id100000001", 100000001, None),
        # A slug of literally "app" is still a slug.
        ("https://apps.apple.com/us/app/app/id100000001", 100000001, "US"),
        ("100000001", 100000001, None),
        # What 1.x accepted (combined fix check #23): the old Link Maker host,
        # and the App Store app's own schemes.
        ("https://geo.itunes.apple.com/us/app/example/id11?mt=8", 11, "US"),
        ("https://geo.itunes.apple.com/app/id11", 11, None),
        ("itms-apps://itunes.apple.com/app/id12", 12, None),
        ("itms-apps://apps.apple.com/us/app/example/id13", 13, "US"),
        ("itms-apps://itunes.apple.com/us/app/example/id14?mt=8", 14, "US"),
        ("ITMS-APPSS://apps.apple.com/gb/app/example/id15", 15, "GB"),
    ],
)
def test_each_form_of_app_link_gives_its_id_and_store(url: str, track_id: int, country) -> None:
    assert itunes.parse_app_link(url) == itunes.AppLink(track_id=track_id, country=country)
    assert itunes.parse_id_from_url(url) == track_id
    assert itunes.country_from_url(url) == country


@pytest.mark.parametrize(
    "url",
    [
        # A developer or a bundle, even one whose slug is literally "app".
        "https://apps.apple.com/us/developer/app/id100000003",
        "https://apps.apple.com/us/developer/bravo-inc/id100000003",
        "https://apps.apple.com/developer/app/id100000003",
        "https://apps.apple.com/us/app-bundle/app/id100000003",
        "https://apps.apple.com/us/story/id100000003",
        # Another host, however it is dressed.
        "https://evil.example/apps.apple.com/de/app/x/id5",
        "https://apps.apple.com.evil.example/gb/app/x/id5",
        "https://example.com/app/x/id5",
        "ftp://apps.apple.com/us/app/x/id5",
        "itms-apps://evil.example/us/app/x/id5",
        "itms-services://apps.apple.com/us/app/x/id5",
        "https://geo.itunes.apple.com.evil.example/us/app/x/id5",
        "https://evil.geo.itunes.apple.com/us/app/x/id5",
        # More than one link, or more path than an app link has.
        "https://apps.apple.com/us/app/x/id5\nhttps://apps.apple.com/de/app/y/id6",
        "https://apps.apple.com/us/app/x/y/id5",
        "https://apps.apple.com/us/app/x",
        "https://apps.apple.com/us/app/x/id5%3F",
    ],
)
def test_anything_but_an_app_link_is_refused(url: str) -> None:
    with pytest.raises(ITunesError) as caught:
        itunes.parse_app_link(url)
    assert "paste the full URL" in (caught.value.hint or "")
    assert itunes.country_from_url(url) is None


def test_a_link_to_a_store_apple_has_not_got_is_refused_by_name() -> None:
    with pytest.raises(ITunesError, match="ZZ"):
        itunes.parse_app_link("https://apps.apple.com/zz/app/bravo/id100000001")


def test_lookup_raises_on_non_list_results() -> None:
    # `match=` is load-bearing, not decorative: with the `isinstance(results,
    # list)` guard deleted, `results` stays the string "oops", `results[0]`
    # becomes `"o"`, and `_app_from_result`'s `k not in item` degrades to a
    # substring test — every required key reads as "missing" and this still
    # raises `ITunesError`, just from the wrong branch and for the wrong
    # reason. A bare `pytest.raises(ITunesError)` can't tell those apart.
    with patch(
        "requests.get",
        return_value=_mock_resp(200, {"resultCount": 1, "results": "oops"}),
    ):
        with pytest.raises(ITunesError, match="results is str, not list"):
            itunes.lookup(100000001)


def _mock_resp(status: int, payload: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload or {}
    r.text = ""
    return r


def test_lookup_returns_app_metadata() -> None:
    payload = {
        "resultCount": 1,
        "results": [
            {
                "trackId": 100000001,
                "bundleId": "com.bravo.app",
                "trackName": "Bravo",
                "version": "2025.20.0",
            }
        ],
    }
    with patch("requests.get", return_value=_mock_resp(200, payload)):
        app = itunes.lookup(100000001)
    assert app.bundle_id == "com.bravo.app"
    assert app.name == "Bravo"
    assert app.version == "2025.20.0"
    assert app.track_id == 100000001


def test_lookup_raises_on_empty_result() -> None:
    with patch(
        "requests.get",
        return_value=_mock_resp(200, {"resultCount": 0, "results": []}),
    ):
        with pytest.raises(ITunesError):
            itunes.lookup(999999999)


def test_lookup_raises_on_http_error() -> None:
    with patch(
        "requests.get",
        return_value=_mock_resp(503),
    ):
        with pytest.raises(ITunesError):
            itunes.lookup(100000001)


def test_query_passes_the_configured_timeout() -> None:
    """`_query` is now the single choke point both `lookup` and
    `lookup_by_bundle_id` depend on — pin the timeout so a future edit that
    drops `timeout=HTTP_TIMEOUT` from the `requests.get` call fails loudly
    instead of leaving every other test green."""
    payload = {
        "resultCount": 1,
        "results": [
            {
                "trackId": 100000001,
                "bundleId": "com.bravo.app",
                "trackName": "Bravo",
                "version": "2025.20.0",
            }
        ],
    }
    with patch(
        "requests.get", return_value=_mock_resp(200, payload)
    ) as mock_get:
        itunes.lookup(100000001)
    assert mock_get.call_args.kwargs["timeout"] == itunes.HTTP_TIMEOUT


# --- lookup_by_bundle_id / artwork_url -------------------------------------
#
# `artwork_url` is the thing Task 3 added: an App Store artwork URL surfaced
# from `artworkUrl512`, falling back to `artworkUrl100`. It must be optional
# everywhere — a lookup with no artwork on the listing is still a successful
# `--add-app`, because `--add-app` never looks at the field at all.


def test_lookup_by_bundle_id_sends_bundle_id_param() -> None:
    payload = {
        "resultCount": 1,
        "results": [
            {
                "trackId": 100000001,
                "bundleId": "com.bravo.app",
                "trackName": "Bravo",
                "version": "2025.20.0",
                "artworkUrl512": "https://example.com/512.png",
            }
        ],
    }
    with patch(
        "requests.get", return_value=_mock_resp(200, payload)
    ) as mock_get:
        app = itunes.lookup_by_bundle_id("com.bravo.app")
    assert mock_get.call_args.kwargs["params"] == {"bundleId": "com.bravo.app"}
    assert app.bundle_id == "com.bravo.app"
    assert app.artwork_url == "https://example.com/512.png"


def test_artwork_url_falls_back_to_artwork_url_100_when_512_absent() -> None:
    payload = {
        "resultCount": 1,
        "results": [
            {
                "trackId": 100000001,
                "bundleId": "com.bravo.app",
                "trackName": "Bravo",
                "version": "2025.20.0",
                "artworkUrl100": "https://example.com/100.png",
            }
        ],
    }
    with patch("requests.get", return_value=_mock_resp(200, payload)):
        app = itunes.lookup_by_bundle_id("com.bravo.app")
    assert app.artwork_url == "https://example.com/100.png"


def test_artwork_url_is_none_when_absent_and_lookup_still_succeeds() -> None:
    """The `--add-app` protection: no artwork keys on the listing must not
    turn a successful lookup into a failed one. `--add-app` reads `name` and
    `version` off the result and has never looked at artwork; a listing with
    no picture is not a reason to fail adding the app."""
    payload = {
        "resultCount": 1,
        "results": [
            {
                "trackId": 100000001,
                "bundleId": "com.bravo.app",
                "trackName": "Bravo",
                "version": "2025.20.0",
            }
        ],
    }
    with patch("requests.get", return_value=_mock_resp(200, payload)):
        app = itunes.lookup_by_bundle_id("com.bravo.app")
    assert app.artwork_url is None
    assert app.name == "Bravo"
    assert app.version == "2025.20.0"


def test_lookup_by_bundle_id_raises_on_empty_result() -> None:
    with patch(
        "requests.get",
        return_value=_mock_resp(200, {"resultCount": 0, "results": []}),
    ):
        with pytest.raises(ITunesError):
            itunes.lookup_by_bundle_id("com.nonexistent.app")


def test_lookup_by_bundle_id_raises_on_http_error() -> None:
    with patch(
        "requests.get",
        return_value=_mock_resp(503),
    ):
        with pytest.raises(ITunesError):
            itunes.lookup_by_bundle_id("com.bravo.app")


def test_lookup_by_bundle_id_raises_on_non_list_results() -> None:
    # See the comment on `test_lookup_raises_on_non_list_results` — same trap.
    with patch(
        "requests.get",
        return_value=_mock_resp(200, {"resultCount": 1, "results": "oops"}),
    ):
        with pytest.raises(ITunesError, match="results is str, not list"):
            itunes.lookup_by_bundle_id("com.bravo.app")


def test_lookup_by_bundle_id_raises_on_missing_fields() -> None:
    payload = {"resultCount": 1, "results": [{"trackId": 1, "bundleId": "com.x.y"}]}
    with patch("requests.get", return_value=_mock_resp(200, payload)):
        with pytest.raises(ITunesError):
            itunes.lookup_by_bundle_id("com.x.y")


def test_lookup_by_id_still_returns_artwork_url_after_refactor() -> None:
    """`lookup` and `lookup_by_bundle_id` share the same result-parsing helper
    post-refactor — confirm the numeric-id path got `artwork_url` too."""
    payload = {
        "resultCount": 1,
        "results": [
            {
                "trackId": 100000001,
                "bundleId": "com.bravo.app",
                "trackName": "Bravo",
                "version": "2025.20.0",
                "artworkUrl512": "https://example.com/512.png",
            }
        ],
    }
    with patch("requests.get", return_value=_mock_resp(200, payload)):
        app = itunes.lookup(100000001)
    assert app.artwork_url == "https://example.com/512.png"


def test_lookup_still_raises_missing_fields_after_refactor() -> None:
    """Pins that the refactor didn't fold `artworkUrl512`/`artworkUrl100` into
    the required-`missing` check — losing this would make an app with no
    App Store picture fail to add, which is exactly the bug this rung must
    not reintroduce."""
    payload = {
        "resultCount": 1,
        "results": [{"trackId": 1, "bundleId": "com.x.y", "trackName": "X"}],  # no version
    }
    with patch("requests.get", return_value=_mock_resp(200, payload)):
        with pytest.raises(ITunesError):
            itunes.lookup(1)


def test_a_top_level_json_array_raises_itunes_error_not_attribute_error() -> None:
    """A response that parses but is not an object must not escape as a raw
    `AttributeError`.

    `--add-app` calls `lookup` with no local catch and relies on the CLI's
    single `_fatal` boundary, which only renders `GraftError`. Without the
    shape guard the user gets a traceback. The artwork cascade never showed
    this because `icon_from_app_store` catches everything — the newer caller
    absorbed the symptom, which is why it survived here.
    """
    from unittest.mock import Mock, patch

    from graft_ipa import itunes
    from graft_ipa.errors import ITunesError

    resp = Mock(status_code=200, json=Mock(return_value=[{"trackId": 1}]))
    with patch.object(requests, "get", return_value=resp):
        with pytest.raises(ITunesError, match="the response is list, not an object"):
            itunes.lookup(100000002)


def test_a_non_object_result_entry_raises_itunes_error_not_type_error() -> None:
    """The pair to the top-level shape guard, one level down.

    `k not in item` is a membership test, so a number raises `TypeError:
    argument of type 'int' is not iterable` — not a `GraftError`, so
    `--add-app` prints a traceback. A string or a list survives it and yields a
    clean error, which is what made this look covered when it wasn't.
    """
    from unittest.mock import Mock, patch

    from graft_ipa import itunes
    from graft_ipa.errors import ITunesError

    resp = Mock(status_code=200, json=Mock(return_value={"resultCount": 1, "results": [12345]}))
    with patch.object(requests, "get", return_value=resp):
        with pytest.raises(ITunesError, match="a result is int, not an object"):
            itunes.lookup(100000002)


# --- get-3: a trackId that isn't a whole number is a malformed listing ---------

import math  # noqa: E402

from tests.itunes_fakes import listing  # noqa: E402


@pytest.mark.parametrize("track_id", ["123", "abc", True, 1.5, 1e3, math.inf, math.nan, None, [1], {"id": 1}])
def test_a_track_id_that_isnt_a_whole_number_is_an_itunes_error(track_id) -> None:
    """`int(item["trackId"])` raised ValueError, TypeError or OverflowError,
    which `_listing` and `check` don't catch: the needs-license refusal was
    lost, and one bad listing ended `--appstore-check` for every app."""
    with pytest.raises(ITunesError, match="trackId"):
        itunes._app_from_result(listing(1, trackId=track_id), described="bundle id com.example.app1")


def test_a_whole_number_track_id_is_kept() -> None:
    assert itunes._app_from_result(listing(1), described="x").track_id == 100000001


def _lookups_with(monkeypatch, bad_bundle: str) -> None:
    def get_json(endpoint, params, *, described):
        n = 1 if params["bundleId"] == bad_bundle else 2
        fields = {"trackId": "not-a-number"} if params["bundleId"] == bad_bundle else {}
        return {"resultCount": 1, "results": [listing(n, bundleId=params["bundleId"], **fields)]}

    monkeypatch.setattr(itunes, "_get_json", get_json)


def test_check_goes_on_past_a_listing_with_a_bad_track_id(temp_config_env, monkeypatch) -> None:
    from graft_ipa import appstore
    from graft_ipa.config import add_app, load_config

    add_app(temp_config_env, "bravo", {"bundle_id": "com.bravo.app", "tweaks": []})
    cfg = load_config(temp_config_env)
    _lookups_with(monkeypatch, cfg.apps["example"].bundle_id)
    said: list[str] = []
    appstore.check(cfg, ["example", "bravo"], emitter=None, say=said.append)
    assert said[0].startswith("example: couldn't check the App Store") and "trackId" in said[0]
    assert said[1].startswith("bravo: 2.0 on the App Store")


def test_a_bad_track_id_is_a_listing_that_couldnt_be_read(temp_config_env, monkeypatch) -> None:
    """`_listing`'s None is "couldn't tell", so a needs-license refusal still
    goes out, with nulls, rather than a crash."""
    from graft_ipa import appstore
    from graft_ipa.config import load_config

    cfg = load_config(temp_config_env)
    _lookups_with(monkeypatch, cfg.apps["example"].bundle_id)
    assert appstore._listing(cfg.apps["example"]) is None


# --- get3-price: a price no float can hold is an unknown price ---------------
#
# `float(raw_price)` raised OverflowError for a JSON integer too large for a
# float (the delta audit's get3-price): not an `ITunesError`, so `--appstore-
# check` ended at that app and a claim's refusal became a traceback. A NaN or
# infinite price came through as a price, read as paid, and went out as bare
# `NaN` in `appstore-refused`. Each reads as no price, the way a price that
# isn't a number already did.

_UNUSABLE_PRICES = [10**400, -(10**400), math.inf, -math.inf, math.nan, "0", True, None, [0]]
_UNUSABLE_IDS = ["huge", "huge-negative", "inf", "-inf", "nan", "text", "bool", "none", "list"]


@pytest.mark.parametrize("price", _UNUSABLE_PRICES, ids=_UNUSABLE_IDS)
def test_an_unusable_price_in_a_lookup_reads_as_unknown(price) -> None:
    app = itunes._app_from_result(listing(1, price=price), described="bundle id com.example.app1")
    assert app.price is None


@pytest.mark.parametrize("price", _UNUSABLE_PRICES, ids=_UNUSABLE_IDS)
def test_an_unusable_price_in_a_search_reads_as_unknown(price) -> None:
    result = itunes._search_result(listing(1, price=price))
    assert result is not None and result.price is None


@pytest.mark.parametrize(("price", "expected"), [(0, 0.0), (0.0, 0.0), (5, 5.0), (4.99, 4.99)])
def test_a_usable_price_is_kept(price, expected) -> None:
    assert itunes._app_from_result(listing(1, price=price), described="x").price == expected
    assert itunes._search_result(listing(1, price=price)).price == expected


def test_check_goes_on_past_a_listing_with_a_huge_price(temp_config_env, monkeypatch) -> None:
    from graft_ipa import appstore
    from graft_ipa.config import add_app, load_config

    add_app(temp_config_env, "bravo", {"bundle_id": "com.bravo.app", "tweaks": []})
    cfg = load_config(temp_config_env)

    def get_json(endpoint, params, *, described):
        n = 1 if params["bundleId"] == cfg.apps["example"].bundle_id else 2
        return {"resultCount": 1, "results": [listing(n, bundleId=params["bundleId"], price=10**400)]}

    monkeypatch.setattr(itunes, "_get_json", get_json)
    said: list[str] = []
    appstore.check(cfg, ["example", "bravo"], emitter=None, say=said.append)
    assert said[0].startswith("example: 1.0 on the App Store")
    assert said[1].startswith("bravo: 2.0 on the App Store")


def test_a_huge_price_is_a_listing_with_no_price(temp_config_env, monkeypatch) -> None:
    """`_listing` catches only `ITunesError`: the OverflowError ended a claim,
    or a Get's needs-license refusal, with a traceback. An unknown price is
    what a claim refuses as "couldn't check its price"."""
    from graft_ipa import appstore
    from graft_ipa.config import load_config

    cfg = load_config(temp_config_env)
    monkeypatch.setattr(
        itunes, "_get_json",
        lambda endpoint, params, *, described: {"resultCount": 1, "results": [listing(1, price=10**400)]},
    )
    found = appstore._listing(cfg.apps["example"])
    assert found is not None and found.price is None
