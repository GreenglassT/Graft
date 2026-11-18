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
