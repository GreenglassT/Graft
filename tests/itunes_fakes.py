"""A stand-in for `requests.get`: the iTunes Search API and Apple's artwork
CDN, answered from canned data.

Installed with `monkeypatch.setattr(requests, "get", fake)`. Every module that
speaks HTTP imports `requests` inside the function that uses it
(`tests/test_lazy_imports.py`), so the library attribute is the one place to
replace it. An artwork URL with no canned bytes answers 404; any other URL it
has no answer for fails the test, so nothing here can reach the network.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from graft_ipa import itunes

#: A PNG as far as `artwork._looks_like_an_image` is concerned.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
SEARCH_URL = f"{itunes.API_BASE}/search"
ARTWORK_HOST = "https://is1.example.invalid/"


def listing(n: int, **fields: Any) -> dict[str, Any]:
    """One `results[]` entry shaped like the real API's, for app number `n`.

    `fields` override or add keys: `listing(2, price=4.99, formattedPrice="$4.99")`.
    Short names on purpose: the Terminal table must fit 80 columns unwrapped.
    """
    entry: dict[str, Any] = {
        "trackId": 100000000 + n,
        "bundleId": f"com.example.app{n}",
        "trackName": f"Example {n}",
        "artistName": f"Dev {n}",
        "sellerName": f"Seller {n}",
        "version": f"{n}.0",
        "price": 0.0,
        "formattedPrice": "Free",
        "currency": "USD",
        "artworkUrl60": f"{ARTWORK_HOST}{n}/60x60bb.jpg",
        "artworkUrl100": f"{ARTWORK_HOST}{n}/100x100bb.jpg",
        "artworkUrl512": f"{ARTWORK_HOST}{n}/512x512bb.jpg",
    }
    entry.update(fields)
    return entry


class FakeITunes:
    """`requests.get`, for `/search` and the artwork host only."""

    def __init__(self) -> None:
        self.results: list[Any] = []
        #: The whole response body instead of `{"resultCount", "results"}`, when set.
        self.payload: Any = None
        self.status = 200
        #: Raised by the search request instead of answering, when set.
        self.error: Exception | None = None
        #: The body is not JSON: `resp.json()` raises.
        self.bad_json = False
        #: Artwork URL → the bytes it answers with.
        self.images: dict[str, bytes] = {}
        #: Every call: (url, params, the other keyword arguments).
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        #: Artwork URL → how many of its bytes a reader has pulled so far.
        self.image_bytes_served: dict[str, int] = {}

    def __call__(self, url: str, params: dict | None = None, **kwargs: Any) -> MagicMock:
        self.calls.append((url, dict(params or {}), kwargs))
        resp = MagicMock()
        if url == SEARCH_URL:
            if self.error is not None:
                raise self.error
            resp.status_code = self.status
            if self.bad_json:
                resp.json.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")
            else:
                resp.json.return_value = (
                    self.payload if self.payload is not None
                    else {"resultCount": len(self.results), "results": self.results}
                )
            return resp
        if url.startswith(ARTWORK_HOST):
            body = self.images.get(url)
            resp.status_code = 200 if body is not None else 404
            resp.iter_content = lambda chunk_size=1, decode_unicode=False: self._stream(url, body or b"", chunk_size)
            return resp
        raise AssertionError(f"a request to {url}, which no test here answers")

    #: The chunk the fake streams in when asked for none (`chunk_size=None`):
    #: the whole body at once, as `requests` would hand back whatever arrived.
    CHUNK = 65536

    def _stream(self, url: str, body: bytes, chunk_size: int | None):
        """The body in `chunk_size` pieces, as `requests` streams it, counting
        each piece handed out so a test can see where a reader stopped."""
        step = chunk_size or len(body) or 1
        for start in range(0, len(body), step):
            piece = body[start:start + step]
            self.image_bytes_served[url] = self.image_bytes_served.get(url, 0) + len(piece)
            yield piece

    @property
    def searches(self) -> list[dict[str, Any]]:
        """The params of every search request, in order."""
        return [params for url, params, _ in self.calls if url == SEARCH_URL]

    @property
    def image_requests(self) -> list[str]:
        return [url for url, _, _ in self.calls if url.startswith(ARTWORK_HOST)]
