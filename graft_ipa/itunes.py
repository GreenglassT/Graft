"""iTunes Lookup API client for resolving an App Store URL/ID/bundle id to metadata.

Endpoint:
    GET https://itunes.apple.com/lookup?id=<numeric-track-id>
    GET https://itunes.apple.com/lookup?bundleId=<bundle-id>
    GET https://itunes.apple.com/search?term=…&country=…&entity=software&limit=…

The response is JSON: `{"resultCount": N, "results": [{...}, ...]}`. A lookup
asks the store it is given (`country=`). Every lookup of an app goes through
`host.look_up`, which asks the app's `store_country` (the store Add App's
Search found it in, or the one its App Store URL names), else this Mac's
region's store and then the US, and moves to the next store only on
`NotInStore`. Given no country, a lookup sends no `country=`, which the API
reads as the US store.

The lookups share one endpoint and one result shape. `_get_json` is the HTTP
that every request here shares, the search included: one User-Agent, one
timeout, and the same three ways to fail (`ITunesRequestError.reason`).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import USER_AGENT, storefronts
from .errors import ITunesError

API_BASE = "https://itunes.apple.com"
HTTP_TIMEOUT = 15


@dataclass
class ITunesApp:
    track_id: int
    bundle_id: str
    name: str
    version: str
    #: The artwork image URL for the listing, or None. Deliberately outside
    #: the `missing`-keys check in `_app_from_result`: `--add-app` reads
    #: `name`/`version`/`bundle_id` off this and must never fail to add an
    #: app just because its App Store listing happens to have no picture —
    #: only the artwork cascade (`artwork.icon_from_app_store`) treats an
    #: absent URL as anything more than "nothing to fetch".
    artwork_url: str | None = None
    #: The listing's price in its store's currency (0 for a free app), or None
    #: when the listing doesn't say. Read by `appstore` before a claim: only a
    #: listing that says 0 is ever claimed.
    price: float | None = None
    #: The price as the store writes it ("Free", "$2.99"), or None.
    formatted_price: str | None = None


#: The hosts an App Store link is on: today's, the legacy one Apple still
#: redirects (`itunes.apple.com/<cc>/app/...`), and the old Link Maker's
#: `geo.itunes.apple.com`, which 1.x accepted (combined fix check #23).
_APP_HOSTS = frozenset({"apps.apple.com", "itunes.apple.com", "geo.itunes.apple.com"})
#: The schemes an App Store link comes in: the web's, and the App Store app's
#: own (`itms-apps://`, `itms-appss://`), which 1.x accepted too.
_APP_SCHEMES = frozenset({"http", "https", "itms-apps", "itms-appss"})
_TRACK_ID_RE = re.compile(r"id(\d+)", re.ASCII)


@dataclass(frozen=True)
class AppLink:
    """What an App Store link names: the app, and the store it is in."""

    track_id: int
    #: The store the link's path names, upper-case and one of
    #: `storefronts.STOREFRONTS`; None when it names none.
    country: str | None


def _not_an_app_link(text: str) -> ITunesError:
    return ITunesError(
        f"couldn't parse an App Store ID from {text!r}",
        hint="paste the full URL, e.g. https://apps.apple.com/us/app/example/id100000001",
    )


def parse_app_link(url: str) -> AppLink:
    """The app and store an App Store link names, parsed as a URL.

    The scheme is http, https, itms-apps or itms-appss, the host is
    `apps.apple.com`, `itunes.apple.com` or `geo.itunes.apple.com`, and the path is an
    optional two-letter country, then `app`, then an optional slug, then
    `id<digits>`; anything else is refused. So a developer's page
    (`/developer/<slug>/id<n>`) is refused even when its slug is `app`, and
    the id and the country can't come from two different links. A bare
    number is a track id with no store.
    """
    text = (url or "").strip()
    if not text:
        raise ITunesError("empty App Store URL or ID")
    if text.isascii() and text.isdigit():
        return AppLink(track_id=int(text), country=None)
    if any(ch.isspace() for ch in text):
        raise _not_an_app_link(text)
    split = urlsplit(text if "://" in text else f"https://{text}")
    if split.scheme.lower() not in _APP_SCHEMES or (split.hostname or "") not in _APP_HOSTS:
        raise _not_an_app_link(text)
    parts = [part for part in split.path.split("/") if part]
    country: str | None = None
    if parts and len(parts[0]) == 2 and parts[0].isascii() and parts[0].isalpha():
        named = parts.pop(0)
        country = storefronts.store_for(named)
        if country is None:
            raise ITunesError(
                f"the URL names the {named.upper()} App Store, which there isn't one of",
                hint="paste the app's link from apps.apple.com, e.g. "
                "https://apps.apple.com/us/app/example/id100000001",
            )
    match = _TRACK_ID_RE.fullmatch(parts[-1]) if parts else None
    if match is None or parts[0] != "app" or len(parts) not in (2, 3):
        raise _not_an_app_link(text)
    return AppLink(track_id=int(match.group(1)), country=country)


def parse_id_from_url(url: str) -> int:
    """The numeric App Store track ID an App Store link names (`parse_app_link`).

    Also accepts a bare numeric string for convenience.
    """
    return parse_app_link(url).track_id


def country_from_url(url: str) -> str | None:
    """The store an App Store link names, upper-cased, or None (`parse_app_link`).

    `https://apps.apple.com/gb/app/example/id1` → GB, and so does the legacy
    `itunes.apple.com/gb/app/...`. A bare id, a link with no country segment,
    and anything that isn't an App Store link name none.
    """
    try:
        return parse_app_link(url).country
    except ITunesError:
        return None


def _price(raw: object) -> float | None:
    """A listing's `price` as a float, or None when it isn't one a price can be.

    `bool` is an `int`: `True` is not a price. A JSON integer too large for a
    float raised OverflowError from `float()`, which no caller catches, so
    `--appstore-check` ended at that app (the delta audit's get3-price); NaN
    and infinity, which Python's json reads, came through as a paid price.
    Each is an unknown price, like one that isn't a number at all, and a
    claim refuses an unknown price.
    """
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None
    try:
        price = float(raw)
    except OverflowError:
        return None
    return price if math.isfinite(price) else None


def _app_from_result(item: dict, *, described: str) -> ITunesApp:
    """Turn one `results[]` entry into an `ITunesApp`, or raise if it can't.

    `described` names the query in the error message ("App Store ID 123" /
    "bundle id com.x.y") — the two callers share this validation but want
    their own wording. `artworkUrl512`/`artworkUrl100` are read here but kept
    out of `missing` on purpose: see the field comment on `ITunesApp.artwork_url`.
    """
    # The pair to `_query`'s guard on the top-level response, and it closes the
    # same hole one level down. `k not in item` is a *membership* test: given a
    # number it raises `TypeError: argument of type 'int' is not iterable`,
    # which is not a `GraftError`, so `--add-app` prints a traceback
    # instead of routing through `_fatal`. (A string or a list happens to
    # survive it and produce a clean error, which is exactly the kind of
    # near-miss that makes this look tested when it isn't.)
    if not isinstance(item, dict):
        raise ITunesError(
            f"iTunes returned unexpected shape for {described}: "
            f"a result is {type(item).__name__}, not an object"
        )
    missing = [k for k in ("trackId", "bundleId", "trackName", "version") if k not in item]
    if missing:
        raise ITunesError(f"iTunes result for {described} is missing {', '.join(missing)}")
    # A malformed listing can hand back `{"artworkUrl512": {"nested": "thing"}}`
    # — some shape other than a string. `or` alone would happily carry that
    # dict through as "the" artwork_url (truthy, so it wins over a perfectly
    # good `artworkUrl100` string underneath it); the `isinstance` catches it
    # and this reads as no artwork rather than a bad URL a downstream `GET`
    # would just fail on anyway.
    # A whole number, as JSON has it, like the search's (`_search_result`):
    # `int()` on anything else raised ValueError, TypeError or OverflowError
    # (`"abc"`, `[1]`, `1e400`), which no caller catches, and turned `True`
    # into 1 and `1.5` into 1. A listing this malformed is an `ITunesError`,
    # so a claim's refusal still goes out and `--appstore-check` goes on to
    # the next app (the whole-diff review's get-3).
    track_id = item["trackId"]
    if not isinstance(track_id, int) or isinstance(track_id, bool):
        raise ITunesError(f"iTunes result for {described} has a trackId that isn't a whole number: {track_id!r}")
    artwork_url = item.get("artworkUrl512") or item.get("artworkUrl100")
    price = _price(item.get("price"))
    formatted = item.get("formattedPrice")
    return ITunesApp(
        track_id=track_id,
        bundle_id=str(item["bundleId"]),
        name=str(item["trackName"]),
        version=str(item["version"]),
        artwork_url=artwork_url if isinstance(artwork_url, str) and artwork_url else None,
        price=price,
        formatted_price=formatted if isinstance(formatted, str) and formatted else None,
    )


#: Why an iTunes request got no usable answer: `ITunesRequestError.reason`, and
#: the `reason` of an `appstore-search-failed` event. `OFFLINE` is the one the
#: app branches on (`CLIContract.AppStoreSearchFailure.offline`, pinned by
#: tests/test_itunes_search.py).
OFFLINE = "offline"
HTTP = "http"
MALFORMED = "malformed"
#: The Search API's per-IP limit (Apple documents roughly 20 calls a minute).
#: Apple answers it with one of `RATE_LIMIT_STATUSES`; the app shows
#: `RATE_LIMITED_MESSAGE` as it is (`CLIContract.AppStoreSearchFailure`).
RATE_LIMITED = "rate-limited"
#: A search term that isn't text: invalid UTF-8 from a Terminal set to another
#: encoding. Refused before anything is asked, with exit 2.
INVALID_TERM = "invalid-term"

RATE_LIMIT_STATUSES = frozenset({403, 429, 503})
RATE_LIMITED_MESSAGE = "Apple is limiting searches from this Mac for a minute. Try again shortly."


class ITunesRequestError(ITunesError):
    """An iTunes request that got no usable answer, and which way it failed.

    An `ITunesError`, exit 13, like every other iTunes failure. `reason` is
    what a caller can branch on without reading the message: the search emits
    it as data, so the app can show its own offline state. `status` is the
    HTTP status for `HTTP`, else None.
    """

    def __init__(
        self, message: str, *, reason: str, status: int | None = None, hint: str | None = None
    ) -> None:
        super().__init__(message, hint=hint)
        self.reason = reason
        self.status = status


def _get_json(endpoint: str, params: dict, *, described: str) -> dict:
    """GET `API_BASE/<endpoint>` with `params` and return its JSON object.

    The HTTP every request here shares. Raises `ITunesRequestError`: `OFFLINE`
    for no answer at all, `HTTP` for an error status, `MALFORMED` for a body
    that is not a JSON object. `described` names the request in each message.
    """
    # Function-local, as in the other four modules that speak HTTP — see
    # `artwork._download_app_store_icon` for the measurement (~28ms off every
    # CLI invocation). `cli.py` imports this module at module scope via
    # `artwork`, and the five have to stay lazy together or none of them saves
    # anything. Both uses below — the `get` and the `except` clause that
    # catches what it raises — are in this function, so one import covers
    # them.
    import requests

    try:
        resp = requests.get(
            f"{API_BASE}/{endpoint}",
            params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        raise ITunesRequestError(f"iTunes {endpoint} request failed: {e}", reason=OFFLINE) from e
    if resp.status_code != 200:
        raise ITunesRequestError(
            f"iTunes {endpoint} returned HTTP {resp.status_code} for {described}",
            reason=HTTP, status=resp.status_code,
        )
    try:
        data = resp.json()
    except ValueError as e:
        raise ITunesRequestError(
            f"iTunes returned non-JSON for {described}: {e}", reason=MALFORMED
        ) from e
    # Checked before `.get`, because a top-level JSON array parses fine and then
    # raises `AttributeError: 'list' object has no attribute 'get'` — which is
    # not an `ITunesError`, so `--add-app` renders it as a raw traceback rather
    # than through `_fatal`'s single error boundary. The artwork cascade is
    # already safe against it (`icon_from_app_store` catches everything), which
    # is exactly why it went unnoticed here: the newer caller absorbed the
    # symptom and the older one is the one that would show it.
    if not isinstance(data, dict):
        raise ITunesRequestError(
            f"iTunes returned unexpected shape for {described}: "
            f"the response is {type(data).__name__}, not an object",
            reason=MALFORMED,
        )
    return data


def _results(data: dict, *, described: str) -> list:
    """`data["results"]`, which must be a list (absent reads as empty)."""
    results = data.get("results") or []
    if not isinstance(results, list):
        raise ITunesRequestError(
            f"iTunes returned unexpected shape for {described}: "
            f"results is {type(results).__name__}, not list",
            reason=MALFORMED,
        )
    return results


class NotInStore(ITunesError):
    """A lookup that the store answered with no app: the one failure that
    `host.look_up` answers by asking the next store."""

    def __init__(self, described: str, country: str | None, *, kind: str) -> None:
        self.described, self.country, self.kind = described, country, kind
        message, hint = _not_in_stores(described, [country or storefronts.DEFAULT], kind)
        super().__init__(message, hint=hint)

    def in_stores(self, tried: list[str]) -> NotInStore:
        """The same miss, naming every store `tried`."""
        e = NotInStore(self.described, self.country, kind=self.kind)
        e.message, e.hint = _not_in_stores(self.described, tried, self.kind)
        e.args = (e.message,)
        return e


#: `NotInStore.kind`: what was looked up.
BY_ID, BY_BUNDLE_ID = "id", "bundle-id"


def _not_in_stores(described: str, tried: list[str], kind: str) -> tuple[str, str]:
    stores = " or ".join(tried) if len(tried) <= 2 else ", ".join(tried[:-1]) + f" or {tried[-1]}"
    message = f"no app found for {described} in the {stores} App Store"
    if kind == BY_ID:
        hint = (
            "the app isn't sold in that store; if the link opens in a browser, use the link from "
            "the store that sells it (apps.apple.com/<country>/app/...), or add it by bundle id "
            "with the manual form and pick its store"
        )
    else:
        hint = (
            "the app may not be on that App Store; set the store it came from with "
            "`graft --set-app-options --app <name> --store-country <CC>`, or in Edit App"
        )
    return message, hint


def _query(params: dict, *, described: str, kind: str) -> ITunesApp:
    """`/lookup` with `params`: the one app it names, or an `ITunesError`.

    `described` names the query in every message. A zero-result answer, the
    one error a person hits normally (an app another store sells, or a
    typo'd id), is `NotInStore`, naming the store asked.
    """
    results = _results(_get_json("lookup", params, described=described), described=described)
    if not results:
        raise NotInStore(described, params.get("country"), kind=kind)
    return _app_from_result(results[0], described=described)


def _in_store(params: dict, country: str | None) -> dict:
    """`params`, asking `country`'s store when one is given. None adds nothing,
    so an app with no `store_country` is asked exactly as it always was."""
    return {**params, "country": country} if country else params


def lookup(app_store_id: int, *, country: str | None = None) -> ITunesApp:
    """Look up an app's metadata by numeric track ID, in `country`'s store
    (default: the US)."""
    return _query(
        _in_store({"id": app_store_id}, country),
        described=f"App Store ID {app_store_id}", kind=BY_ID,
    )


def lookup_by_bundle_id(bundle_id: str, *, country: str | None = None) -> ITunesApp:
    """Look up an app's metadata by bundle id, e.g. `com.example.app`.

    Exists for the artwork cascade's App Store rung (`artwork.icon_from_app_store`):
    a freshly `--add-app`'d app has a bundle id on file but nothing cached yet
    to read an icon out of, and rung one (`icon_from_ipa`) has nothing to work
    with until the IPA is downloaded. `--add-app` itself still goes through
    `lookup`/track-ID, since that's what an App Store URL gives up first.
    """
    return _query(
        _in_store({"bundleId": bundle_id}, country),
        described=f"bundle id {bundle_id}",
        # Not "use the link from another store" — that's `--add-app` advice,
        # and by the time anything looks an app up by bundle id here, the
        # app is already added: the advice is to set its store.
        kind=BY_BUNDLE_ID,
    )


# --- the Search API (`--appstore-search`) -------------------------------------

#: The Search API's own default is 50 and its ceiling 200. Graft asks for fewer,
#: because each result costs the CLI an icon download for the app to draw.
SEARCH_DEFAULT_LIMIT = 25
SEARCH_MAX_LIMIT = 50


@dataclass(frozen=True)
class SearchResult:
    """One app the Search API found."""

    track_id: int
    bundle_id: str
    name: str
    #: `artistName` (the developer as the store shows it), else `sellerName`.
    developer: str
    version: str
    #: 0.0 for a free app; None when the listing gives no number.
    price: float | None
    #: The store's own spelling: "Free", "$4.99", "4,99 €".
    formatted_price: str | None
    currency: str | None
    #: The largest artwork the listing has (512, else 100), as `ITunesApp`'s.
    artwork_url: str | None
    #: The smallest worth drawing in a list (100, else 60, else 512).
    icon_url: str | None


def _text(item: dict, key: str) -> str | None:
    value = item.get(key)
    return value if isinstance(value, str) and value else None


def _search_result(item: object) -> SearchResult | None:
    """One `results[]` entry as a `SearchResult`, or None for one that can't be used.

    None rather than raising, unlike `_app_from_result`: a lookup wants its one
    result and has nothing to show without it, while a search with one odd
    listing still has the rest to show.
    """
    if not isinstance(item, dict):
        return None
    track_id = item.get("trackId")
    bundle_id, name, version = _text(item, "bundleId"), _text(item, "trackName"), _text(item, "version")
    if not isinstance(track_id, int) or isinstance(track_id, bool) or not (bundle_id and name and version):
        return None
    return SearchResult(
        track_id=track_id,
        bundle_id=bundle_id,
        name=name,
        developer=_text(item, "artistName") or _text(item, "sellerName") or "",
        version=version,
        price=_price(item.get("price")),
        formatted_price=_text(item, "formattedPrice"),
        currency=_text(item, "currency"),
        artwork_url=_text(item, "artworkUrl512") or _text(item, "artworkUrl100"),
        icon_url=_text(item, "artworkUrl100") or _text(item, "artworkUrl60") or _text(item, "artworkUrl512"),
    )


#: What a person reads for each way a search fails. `_get_json`'s messages are
#: about the HTTP; these are about the search, and the HTTP one is chained.
_SEARCH_FAILURE: dict[str, tuple[str, str]] = {
    OFFLINE: (
        "couldn't reach the App Store to search",
        "check this Mac's internet connection, then search again",
    ),
    HTTP: (
        "the App Store search answered HTTP {status}",
        "Apple's search is having trouble; try again in a minute",
    ),
    MALFORMED: (
        "the App Store search answered with something that isn't a list of apps",
        "try again in a minute; if it keeps happening, Apple may have changed the search API",
    ),
}


def search(term: str, *, country: str, limit: int = SEARCH_DEFAULT_LIMIT) -> list[SearchResult]:
    """The `country` App Store's iPhone apps matching `term`, in the store's order.

    No account and no credentials: the Search API is public, and the request
    carries only the term, the country and the limit. `limit` is kept between
    1 and `SEARCH_MAX_LIMIT`. Raises `ITunesRequestError` in plain words.
    """
    params = {
        "term": term,
        "country": country,
        "entity": "software",  # iPhone apps; iPadSoftware and macSoftware are other entities
        "limit": max(1, min(limit, SEARCH_MAX_LIMIT)),
    }
    described = f"the search for {term!r}"
    try:
        results = _results(_get_json("search", params, described=described), described=described)
    except ITunesRequestError as e:
        if e.reason == HTTP and e.status in RATE_LIMIT_STATUSES:
            raise ITunesRequestError(
                RATE_LIMITED_MESSAGE, reason=RATE_LIMITED, status=e.status
            ) from e
        if e.reason == HTTP and e.status == 400:
            # What Apple answers for a store it doesn't have. `country` is checked
            # against `storefronts` before this, so it means Apple's list has
            # changed, or the term is too long; retrying can never help.
            raise ITunesRequestError(
                f"the App Store search refused a search of the {country} store (HTTP 400)",
                reason=HTTP, status=400,
                hint=f"Apple may have no App Store for {country} any more, or the term is too "
                "long: search another store (--country), or a shorter term",
            ) from e
        message, hint = _SEARCH_FAILURE[e.reason]
        raise ITunesRequestError(
            message.format(status=e.status), reason=e.reason, status=e.status, hint=hint
        ) from e
    return [r for r in map(_search_result, results) if r is not None]
