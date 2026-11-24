"""`appstore_search`: what `--appstore-search` adds around `itunes.search`.

The store to ask, the icon cache, and which results are already configured.
The network is `FakeITunes` throughout.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import requests

from graft_ipa import appstore_search, artwork, host, itunes, storefronts
from graft_ipa.config import load_config
from graft_ipa.errors import ConfigError
from tests.itunes_fakes import PNG, FakeITunes, listing


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeITunes:
    f = FakeITunes()
    monkeypatch.setattr(requests, "get", f)
    return f


# --- icons ---------------------------------------------------------------------


def test_each_icon_is_downloaded_once_and_then_served_from_the_cache(fake: FakeITunes) -> None:
    a, b = listing(1)["artworkUrl100"], listing(2)["artworkUrl100"]
    fake.images = {a: PNG, b: PNG + b"two"}

    first = appstore_search.fetch_icons([a, b, a])

    assert sorted(fake.image_requests) == sorted([a, b]), "a repeated URL is fetched once"
    assert first[a].read_bytes() == PNG
    assert first[b].read_bytes() == PNG + b"two"
    assert first[a].parent == appstore_search.icon_dir()

    fake.calls.clear()
    assert appstore_search.fetch_icons([a, b]) == first
    assert fake.image_requests == [], "the second search draws from the cache"


def test_an_answer_that_is_not_an_image_is_not_kept(fake: FakeITunes) -> None:
    url = listing(1)["artworkUrl100"]
    fake.images = {url: b"<html>Access Denied</html>"}

    assert appstore_search.fetch_icons([url]) == {}
    assert list(appstore_search.icon_dir().glob("*.img")) == []


def test_an_icon_that_is_not_there_is_simply_absent(fake: FakeITunes) -> None:
    assert appstore_search.fetch_icons([listing(1)["artworkUrl100"]]) == {}  # a 404


def test_an_oversized_image_is_refused(fake: FakeITunes) -> None:
    url = listing(1)["artworkUrl100"]
    fake.images = {url: PNG + b"\0" * (4 * artwork._MAX_ICON_BYTES)}
    assert artwork.fetch_image(url) is None
    # Refused while it streams, not after reading it whole: the download
    # stops within one chunk of the cap.
    assert fake.image_bytes_served[url] <= artwork._MAX_ICON_BYTES + fake.CHUNK


# --- the deadline: a slow icon never holds back the results ------------------------


class _SlowArtwork:
    """A real HTTP server on 127.0.0.1 for icons: `/fast` answers a PNG at
    once, `/slow` sends its headers and then one byte a second, for ever (the
    review's trickling CDN edge, which never trips a read timeout)."""

    def __init__(self) -> None:
        import http.server
        import threading

        stop = self.stop = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.end_headers()
                if self.path.startswith("/fast"):
                    self.wfile.write(PNG)
                    return
                self.wfile.write(PNG[:4])
                self.wfile.flush()
                while not stop.wait(1.0):
                    try:
                        self.wfile.write(b"\0")
                        self.wfile.flush()
                    except OSError:
                        return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def slow_artwork(monkeypatch: pytest.MonkeyPatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    server = _SlowArtwork()
    yield server
    server.close()


def test_the_icons_of_one_search_share_a_deadline_of_about_four_seconds() -> None:
    assert 3 <= appstore_search.ICON_DEADLINE <= 5


def test_a_slow_icon_is_left_out_at_the_deadline_and_the_rest_are_kept(slow_artwork) -> None:
    fast, slow = f"{slow_artwork.base}/fast/1.png", f"{slow_artwork.base}/slow/2.png"

    started = time.monotonic()
    found = appstore_search.fetch_icons([fast, slow], deadline=1.0)
    took = time.monotonic() - started

    assert took < 2.5, f"waited {took:.1f}s on a trickling icon"
    assert set(found) == {fast}
    assert found[fast].read_bytes() == PNG


def test_each_icon_is_cached_as_it_arrives_not_when_the_batch_ends(slow_artwork) -> None:
    """A search the app kills keeps the icons that had already arrived."""
    import threading

    fast, slow = f"{slow_artwork.base}/fast/3.png", f"{slow_artwork.base}/slow/4.png"
    batch = threading.Thread(
        target=appstore_search.fetch_icons, args=([fast, slow],), kwargs={"deadline": 30.0},
        daemon=True,
    )
    batch.start()
    cached = appstore_search._icon_path(fast)
    deadline = time.monotonic() + 5
    while not cached.exists() and time.monotonic() < deadline:
        time.sleep(0.05)

    assert cached.exists(), "the fast icon is on disk while the slow one still trickles"
    assert batch.is_alive(), "premise: the batch has not finished"


def test_a_search_with_a_slow_icon_still_answers(
    temp_config_env: Path, fake: FakeITunes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through `search` itself, with an icon that never arrives."""
    import threading

    fake.results = [listing(1), listing(2)]
    fake.images = {listing(1)["artworkUrl100"]: PNG}
    never = threading.Event()
    real = artwork.fetch_image

    def fetch(url: str, *, timeout: float):
        if url == listing(2)["artworkUrl100"]:
            never.wait(10)
            return None
        return real(url, timeout=timeout)

    monkeypatch.setattr(artwork, "fetch_image", fetch)
    monkeypatch.setattr(appstore_search, "ICON_DEADLINE", 0.5)

    started = time.monotonic()
    found = appstore_search.search(load_config(temp_config_env), "example", country="US")
    never.set()

    assert time.monotonic() - started < 2
    assert [h.icon_path is not None for h in found.hits] == [True, False]


#: The real CLI, run as `python -m graft_ipa` is, with only the iTunes
#: answer faked: one result whose icon is fast and ten that trickle.
_SEARCH_CHILD = r"""
import runpy, sys
repo, base, deadline = sys.argv[1], sys.argv[2], float(sys.argv[3])
sys.path.insert(0, repo)
from graft_ipa import appstore_search, itunes
appstore_search.ICON_DEADLINE = deadline

def entry(n):
    url = f"{base}/{'fast' if n == 0 else 'slow'}/{n}.png"
    return {"trackId": 100000000 + n, "bundleId": f"com.example.app{n}", "trackName": f"Example {n}",
            "artistName": "Dev", "version": "1.0", "price": 0.0, "formattedPrice": "Free",
            "currency": "USD", "artworkUrl60": url, "artworkUrl100": url, "artworkUrl512": url}

itunes._get_json = lambda endpoint, params, *, described: {
    "resultCount": 11, "results": [entry(n) for n in range(11)]}
sys.argv = ["graft", "--json", "--appstore-search", "notes", "--country", "US"]
runpy.run_module("graft_ipa", run_name="__main__", alter_sys=True)
"""


def test_the_cli_exits_once_the_results_are_out_with_icons_still_trickling(
    temp_config_env: Path, slow_artwork
) -> None:
    """Combined fix check, medium 3. The deadline helps the app only if the
    process can then exit: the app reads nothing until it does
    (CLIRunner.streamEvents). That rests on the icon threads being daemons;
    joined workers would keep the CLI alive for as long as a CDN edge
    trickles, which is finding #2 back again."""
    import subprocess
    import sys

    deadline, margin = 1.5, 2.5
    repo = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen(
        [sys.executable, "-B", "-c", _SEARCH_CHILD, str(repo), slow_artwork.base, str(deadline)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(repo),
    )
    events, ended_at = [], None
    try:
        for line in proc.stdout:
            if line.startswith("{"):
                events.append(json.loads(line))
                if events[-1]["type"] == "appstore-search-end":
                    ended_at = time.monotonic()
                    break
        assert ended_at is not None, f"no end event; stderr: {proc.stderr.read()!r}"
        try:
            code = proc.wait(timeout=deadline + margin)
        except subprocess.TimeoutExpired:
            pytest.fail(f"the CLI was still running {deadline + margin:.1f}s after appstore-search-end, "
                        "with its icons still trickling")
        assert code == 0, proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()
    results = [e for e in events if e["type"] == "appstore-search-result"]
    assert len(results) == 11
    assert [r["icon_path"] is not None for r in results] == [True] + [False] * 10, "premise: ten trickled"


def test_a_search_prunes_the_icon_cache(temp_config_env: Path, fake: FakeITunes) -> None:
    d = appstore_search.icon_dir()
    d.mkdir(parents=True)
    old = d / "old.img"
    old.write_bytes(PNG)
    week_ago = time.time() - 8 * 86400
    os.utime(old, (week_ago, week_ago))

    appstore_search.search(load_config(temp_config_env), "example", country="US")

    assert not old.exists()


def test_icons_older_than_a_week_are_pruned_and_newer_ones_kept() -> None:
    d = appstore_search.icon_dir()
    d.mkdir(parents=True)
    old, new = d / "old.img", d / "new.img"
    old.write_bytes(PNG)
    new.write_bytes(PNG)
    now = time.time()
    os.utime(old, (now - 8 * 86400, now - 8 * 86400))
    os.utime(new, (now - 6 * 86400, now - 6 * 86400))

    appstore_search.prune_icons(now=now)

    assert not old.exists()
    assert new.exists()


def test_pruning_a_cache_that_was_never_made_is_nothing() -> None:
    assert not appstore_search.icon_dir().exists()
    appstore_search.prune_icons()  # no error


# --- the store, and how many --------------------------------------------------


@pytest.mark.parametrize(("given", "code"), [("GB", "GB"), ("gb", "GB"), (" de ", "DE")])
def test_a_given_country_is_used_upper_cased(given: str, code: str) -> None:
    assert appstore_search.resolve_country(given) == storefronts.Store(code)


@pytest.mark.parametrize("given", ["", "G", "GBR", "1A", "Ü1", "g b"])
def test_a_given_country_that_is_not_two_letters_is_refused(given: str) -> None:
    with pytest.raises(ConfigError, match="two-letter country code"):
        appstore_search.resolve_country(given)


def test_a_given_country_with_no_store_is_refused() -> None:
    with pytest.raises(ConfigError, match="no App Store for ZZ"):
        appstore_search.resolve_country("zz")


def test_a_given_alias_is_its_store_with_a_note() -> None:
    store = appstore_search.resolve_country("UK")
    assert store.code == "GB" and store.note and "UK" in store.note


def test_without_a_country_this_macs_region_is_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "_read_apple_locale", lambda: "de_DE")
    assert appstore_search.resolve_country(None) == storefronts.Store("DE")


def test_a_region_with_no_store_asks_the_us_store_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "_read_apple_locale", lambda: "fa_IR")
    assert appstore_search.resolve_country(None) == storefronts.Store("US", storefronts.NO_STORE_NOTE)


def test_with_no_readable_region_the_us_store_is_asked() -> None:
    # conftest pins the region unreadable.
    assert appstore_search.resolve_country(None) == storefronts.Store(appstore_search.DEFAULT_COUNTRY)
    assert appstore_search.DEFAULT_COUNTRY == "US"


@pytest.mark.parametrize(("given", "limit"), [(None, 25), (1, 1), (7, 7), (50, 50), (500, 50)])
def test_the_limit_defaults_to_25_and_stops_at_50(given: int | None, limit: int) -> None:
    assert appstore_search.resolve_limit(given) == limit


@pytest.mark.parametrize("given", [0, -1])
def test_a_limit_below_one_is_refused(given: int) -> None:
    with pytest.raises(ConfigError, match="at least 1"):
        appstore_search.resolve_limit(given)


# --- the search ------------------------------------------------------------------


def test_a_result_already_configured_names_its_app_in_any_case(
    temp_config_env: Path, fake: FakeITunes
) -> None:
    # The test config's one app is `example`, bundle id com.example.app.
    fake.results = [listing(1, bundleId="COM.Example.App"), listing(2)]

    found = appstore_search.search(load_config(temp_config_env), "example", country="us")

    assert [h.configured_as for h in found.hits] == ["example", None]
    assert (found.term, found.country) == ("example", "US")


def test_each_hit_carries_its_icon_when_one_was_fetched(temp_config_env: Path, fake: FakeITunes) -> None:
    fake.results = [listing(1), listing(2)]
    fake.images = {listing(1)["artworkUrl100"]: PNG}

    found = appstore_search.search(load_config(temp_config_env), "example", country="US")

    assert found.hits[0].icon_path is not None and found.hits[0].icon_path.read_bytes() == PNG
    assert found.hits[1].icon_path is None


def test_without_icons_no_image_is_asked_for(temp_config_env: Path, fake: FakeITunes) -> None:
    fake.results = [listing(1)]
    fake.images = {listing(1)["artworkUrl100"]: PNG}

    found = appstore_search.search(load_config(temp_config_env), "example", country="US", with_icons=False)

    assert found.hits[0].icon_path is None
    assert fake.image_requests == []


def test_a_bad_country_or_limit_is_refused_before_anything_is_asked(
    temp_config_env: Path, fake: FakeITunes
) -> None:
    cfg = load_config(temp_config_env)
    with pytest.raises(ConfigError):
        appstore_search.search(cfg, "example", country="GBR")
    with pytest.raises(ConfigError):
        appstore_search.search(cfg, "example", country="US", limit=0)
    assert fake.calls == []


# --- the wire shape -----------------------------------------------------------------


def test_event_fields_are_the_wire_shape(tmp_path: Path) -> None:
    r = itunes.SearchResult(
        track_id=100000001, bundle_id="com.example.app1", name="Example 1", developer="Dev 1",
        version="1.0", price=4.99, formatted_price="$4.99", currency="USD",
        artwork_url="https://is1.example.invalid/1/512x512bb.jpg",
        icon_url="https://is1.example.invalid/1/100x100bb.jpg",
    )
    hit = appstore_search.Hit(result=r, icon_path=tmp_path / "a.img", configured_as="example")

    assert appstore_search.event_fields(hit) == {
        "track_id": 100000001, "bundle_id": "com.example.app1", "name": "Example 1",
        "developer": "Dev 1", "version": "1.0", "price": 4.99, "formatted_price": "$4.99",
        "currency": "USD", "artwork_url": "https://is1.example.invalid/1/512x512bb.jpg",
        "icon_path": str(tmp_path / "a.img"), "configured_as": "example",
    }


@pytest.mark.parametrize(
    ("price", "formatted", "label"),
    [(0.0, "Free", "Free"), (4.99, "$4.99", "$4.99"), (0.0, None, "Free"),
     (4.99, None, "4.99"),
     # No number is "price unknown", whatever the listing spells: the Mac app
     # says the same (`AppStoreSearchResult.Acquisition.unknown`).
     (None, None, "price unknown"), (None, "Free", "price unknown"), (None, "$4.99", "price unknown")],
)
def test_the_price_label_prefers_the_stores_own_spelling(price, formatted, label) -> None:
    r = itunes.SearchResult(
        track_id=1, bundle_id="com.example.app", name="Example", developer="", version="1",
        price=price, formatted_price=formatted, currency=None, artwork_url=None, icon_url=None,
    )
    assert appstore_search.price_label(r) == label


def test_the_unknown_price_label_is_the_one_the_app_shows() -> None:
    from tests.cli_ast import strip_swift_comments

    form = Path(__file__).resolve().parent.parent / "Graft" / "Graft" / "Views" / "Apps" / "AppStoreSearchForm.swift"
    assert f'"{appstore_search.PRICE_UNKNOWN}"'.lower() in strip_swift_comments(form.read_text()).lower()


# --- what a listing may not put on a terminal --------------------------------------------


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("Evil\x1b]0;PWNED\x07\x1b[31mRED\x1b[0m", "EvilRED"),
        ("Dev\x1b[2J", "Dev"),
        ("Tab\tand\nnewline", "Tabandnewline"),
        ("C1\x9b31m and \x85", "C131m and "),
        ("Bell\x07 DEL\x7f", "Bell DEL"),
        ("ST\x1bPq#0\x1b\\end", "STend"),
        ("Plain Name", "Plain Name"),
        ("微信 – Ünïcödé", "微信 – Ünïcödé"),
    ],
)
def test_control_characters_and_escape_sequences_are_stripped(raw: str, shown: str) -> None:
    assert appstore_search.printable(raw) == shown


def test_a_search_term_that_is_not_text_is_refused_before_anything_is_asked(
    temp_config_env: Path, fake: FakeITunes
) -> None:
    with pytest.raises(appstore_search.InvalidTerm) as caught:
        appstore_search.search(load_config(temp_config_env), "caf\udce9", country="US")
    assert caught.value.reason == itunes.INVALID_TERM
    assert caught.value.exit_code == 2
    assert fake.calls == []
