"""iTunes Lookup API client for resolving an App Store URL/ID/bundle id to metadata.

Endpoint:
    GET https://itunes.apple.com/lookup?id=<numeric-track-id>
    GET https://itunes.apple.com/lookup?bundleId=<bundle-id>

The response is JSON: `{"resultCount": N, "results": [{...}, ...]}`. We use the
US storefront (no `country=`); the apps graft targets are global so a
single store is sufficient.

Two lookups share one endpoint and one result shape: `lookup` (by numeric
track ID, used by `--add-app <url>`) and `lookup_by_bundle_id` (used by the
artwork cascade's App Store rung, since a freshly-added app has a bundle id
on file but no track ID). `_query` holds the shared HTTP-and-shape validation
and calls `_app_from_result` at its tail to parse the winning result; each
public function is just a one-line declaration supplying its own query
param, description, and hint.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import USER_AGENT
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


# Matches the `.../app/<slug>/id<digits>` form specifically — accepting any
# `/id<digits>` would also match developer-portal URLs (`/developer/.../idNNN`)
# whose ID is an entity ID, not an app track ID, and would silently look up
# the wrong thing.
_ID_RE = re.compile(r"/app/[^/]+/id(\d+)(?:[/?#]|$)")


def parse_id_from_url(url: str) -> int:
    """Extract the numeric App Store track ID from an apps.apple.com URL.

    Also accepts a bare numeric string for convenience.
    """
    text = (url or "").strip()
    if not text:
        raise ITunesError("empty App Store URL or ID")
    if text.isdigit():
        return int(text)
    match = _ID_RE.search(text)
    if not match:
        raise ITunesError(
            f"couldn't parse an App Store ID from {url!r}",
            hint="paste the full URL, e.g. https://apps.apple.com/us/app/example/id100000001",
        )
    return int(match.group(1))


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
    artwork_url = item.get("artworkUrl512") or item.get("artworkUrl100")
    return ITunesApp(
        track_id=int(item["trackId"]),
        bundle_id=str(item["bundleId"]),
        name=str(item["trackName"]),
        version=str(item["version"]),
        artwork_url=artwork_url if isinstance(artwork_url, str) and artwork_url else None,
    )


def _query(params: dict, *, described: str, empty_hint: str) -> ITunesApp:
    """GET `/lookup` with `params`, validate the response, and return the app.

    Shared HTTP-and-shape validation for `lookup` and `lookup_by_bundle_id`.
    `described` names the query for every error message; `empty_hint` is the
    caller-specific advice attached to a zero-result response (the one error
    a person hits normally — a typo'd URL or bundle id — so it's the one
    that gets a hint instead of just a message).
    """
    # Function-local, as in the other four modules that speak HTTP — see
    # `artwork._download_app_store_icon` for the measurement (~28ms off every
    # CLI invocation). `cli.py` imports this module at module scope via
    # `artwork`, and the five have to stay lazy together or none of them saves
    # anything. Both uses below — the `get` and the `except` clause that
    # catches what it raises — are in this function, so one import covers
    # them.
    import requests

    url = f"{API_BASE}/lookup"
    try:
        resp = requests.get(
            url,
            params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        raise ITunesError(f"iTunes lookup request failed: {e}") from e
    if resp.status_code != 200:
        raise ITunesError(f"iTunes lookup returned HTTP {resp.status_code} for {described}")
    try:
        data = resp.json()
    except ValueError as e:
        raise ITunesError(f"iTunes returned non-JSON for {described}: {e}") from e
    # Checked before `.get`, because a top-level JSON array parses fine and then
    # raises `AttributeError: 'list' object has no attribute 'get'` — which is
    # not an `ITunesError`, so `--add-app` renders it as a raw traceback rather
    # than through `_fatal`'s single error boundary. The artwork cascade is
    # already safe against it (`icon_from_app_store` catches everything), which
    # is exactly why it went unnoticed here: the newer caller absorbed the
    # symptom and the older one is the one that would show it.
    if not isinstance(data, dict):
        raise ITunesError(
            f"iTunes returned unexpected shape for {described}: "
            f"the response is {type(data).__name__}, not an object"
        )
    results = data.get("results") or []
    if not isinstance(results, list):
        raise ITunesError(
            f"iTunes returned unexpected shape for {described}: "
            f"results is {type(results).__name__}, not list"
        )
    if not results:
        raise ITunesError(f"no app found for {described}", hint=empty_hint)
    return _app_from_result(results[0], described=described)


def lookup(app_store_id: int) -> ITunesApp:
    """Look up an app's metadata by numeric track ID."""
    return _query(
        {"id": app_store_id},
        described=f"App Store ID {app_store_id}",
        empty_hint="double-check the URL, or use the manual form to add it by bundle id",
    )


def lookup_by_bundle_id(bundle_id: str) -> ITunesApp:
    """Look up an app's metadata by bundle id, e.g. `com.example.app`.

    Exists for the artwork cascade's App Store rung (`artwork.icon_from_app_store`):
    a freshly `--add-app`'d app has a bundle id on file but nothing cached yet
    to read an icon out of, and rung one (`icon_from_ipa`) has nothing to work
    with until the IPA is downloaded. `--add-app` itself still goes through
    `lookup`/track-ID, since that's what an App Store URL gives up first.
    """
    return _query(
        {"bundleId": bundle_id},
        described=f"bundle id {bundle_id}",
        # Not "double-check the bundle id, or use the manual form" — that's
        # `--add-app` advice, and by the time anything looks an app up by
        # bundle id here, the app is already added. The only caller
        # (`icon_from_app_store`) swallows this exception anyway, so the
        # hint is never surfaced, but it should still be true on its own
        # terms rather than nonsense left over from a copy-paste.
        empty_hint="the app may not be on the US App Store",
    )
