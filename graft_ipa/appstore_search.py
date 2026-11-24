"""Find an app on the App Store by name: `graft --appstore-search`.

The search is `itunes.search`, the public iTunes Search API. It involves no
Apple ID and no credentials, and sends nothing of the user's but the search
term (docs/network.md). This module adds what the Add App sheet needs around it:

- **Which country's store to ask** (`resolve_country`): `--country` when
  given, else the store this Mac's region shops in (`host.mac_store`), else
  the US. Both go through `storefronts`, so a code Apple has no store for is
  never sent: it would only get HTTP 400.
  - The signed-in Apple ID's own storefront would be better, and Graft can't
    know it without credentials. `ipatool auth info` prints only the
    account's name and email, and the storefront sits in ipatool's keychain
    item beside the password token, which Graft doesn't read.
  - The Mac app passes the store its picker holds as `--country`, which
    starts at the same mapping of its own region (`Storefronts.swift`).
- **A small icon per result** (`fetch_icons`). The CLI downloads it because
  the app makes no connections of its own, and caches it by URL for a week.
- **Whether each result is already in config.yaml** (`configured_names`).
"""

from __future__ import annotations

import hashlib
import os
import queue
import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import artwork, data_dirs, host, itunes, storefronts
from .config import Config
from .errors import ConfigError

#: How long a search icon is kept. Artwork URLs change when the artwork does,
#: so a cached file is never stale, only unused; this bounds the directory.
ICON_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
#: Shorter than the artwork rung's 15 s: a search waits on these, and a result
#: with no icon is still a result (the app draws its letter tile).
ICON_TIMEOUT = 5
#: How long one search waits for all of its icons together. `ICON_TIMEOUT`
#: alone bounds nothing: it is per connect attempt and per socket read, so a
#: CDN host with eight blackholed addresses costs 40 s an icon, and one that
#: trickles a byte a second never times out at all. An icon not here by the
#: deadline is drawn as the letter tile, and the results go out without it.
ICON_DEADLINE = 4.0
ICON_WORKERS = 8


def icon_dir() -> Path:
    return data_dirs.cache_dir() / "appstore-search-icons"


def _icon_path(url: str) -> Path:
    return icon_dir() / f"{hashlib.sha256(url.encode()).hexdigest()[:32]}.img"


def prune_icons(*, now: float | None = None) -> None:
    """Delete cached icons older than `ICON_MAX_AGE_SECONDS`.

    Leftover temp files from a killed run go the same way. Best effort: a
    file that can't be removed stays for the next search.
    """
    cutoff = (time.time() if now is None else now) - ICON_MAX_AGE_SECONDS
    try:
        entries = list(os.scandir(icon_dir()))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < cutoff:
                os.unlink(entry.path)
        except OSError:
            continue


def _download_icon(url: str) -> Path | None:
    """Download `url`'s icon and cache it, or None. Written the moment it
    arrives, so a search the app kills keeps every icon that had come in."""
    data = artwork.fetch_image(url, timeout=ICON_TIMEOUT)
    if data is None:
        return None
    path = _icon_path(url)
    try:
        # The artwork cache's own torn-write guard.
        artwork._atomic_write(path, data)
    except OSError:
        return None
    return path


def fetch_icons(urls: Iterable[str], *, deadline: float | None = None) -> dict[str, Path]:
    """Each URL's icon on disk, from the cache or downloaded now; a URL with
    no image (a 404, an HTML page, a timeout) is absent, and so is one still
    downloading when `deadline` seconds (`ICON_DEADLINE`) have passed.

    Downloads run in parallel on daemon threads, never joined: a thread still
    waiting on a slow icon when the deadline passes is simply left, and the
    CLI exits under it. (A `ThreadPoolExecutor` would join its workers at
    exit, and the search would then wait out the slow icon anyway.) Each icon
    is written atomically as it arrives, so a canceled search leaves no torn
    file and keeps the icons it had.
    """
    found: dict[str, Path] = {}
    missing: list[str] = []
    for url in dict.fromkeys(u for u in urls if u):
        path = _icon_path(url)
        try:
            if path.stat().st_size > 0:
                found[url] = path
                continue
        except OSError:
            pass
        missing.append(url)
    if not missing:
        return found
    todo: queue.SimpleQueue[str] = queue.SimpleQueue()
    for url in missing:
        todo.put(url)
    done: queue.SimpleQueue[tuple[str, Path | None]] = queue.SimpleQueue()

    def work() -> None:
        while True:
            try:
                url = todo.get_nowait()
            except queue.Empty:
                return
            try:
                done.put((url, _download_icon(url)))
            except Exception:  # an icon is never worth a search
                done.put((url, None))

    for _ in range(min(ICON_WORKERS, len(missing))):
        threading.Thread(target=work, name="graft-search-icon", daemon=True).start()
    ends = time.monotonic() + (ICON_DEADLINE if deadline is None else deadline)
    for _ in missing:
        left = ends - time.monotonic()
        if left <= 0:
            break
        try:
            url, path = done.get(timeout=left)
        except queue.Empty:
            break
        if path is not None:
            found[url] = path
    return found


DEFAULT_COUNTRY = "US"


def resolve_country(given: str | None) -> storefronts.Store:
    """The store to ask, and what to say about how it was chosen.

    `given` (`--country`) is checked and mapped (`storefronts.given`): a
    territory or `UK` is its store, with a note, and a code with no store is
    refused. Without one, the store this Mac's region shops in
    (`host.mac_store`), which is the US, with a note, for a region with none.
    """
    if given is not None:
        return storefronts.given(given, flag="--country")
    return host.mac_store()


def resolve_limit(given: int | None) -> int:
    """How many results to ask for: 25 by default, at most 50."""
    if given is None:
        return itunes.SEARCH_DEFAULT_LIMIT
    if given < 1:
        raise ConfigError(f"--limit must be at least 1, not {given}")
    return min(given, itunes.SEARCH_MAX_LIMIT)


def configured_names(cfg: Config) -> dict[str, str]:
    """Each configured bundle id, case-folded, to the first app (by name) using it.

    Case-folded because a bundle id's letter case is not part of its
    identity: `COM.Example.App` on the store is the `com.example.app` in
    config.yaml.
    """
    names: dict[str, str] = {}
    for name in sorted(cfg.apps):
        names.setdefault(cfg.apps[name].bundle_id.casefold(), name)
    return names


@dataclass(frozen=True)
class Hit:
    """One search result, with what the CLI adds to it."""

    result: itunes.SearchResult
    #: The cached small icon, or None (none listed, or it couldn't be fetched).
    icon_path: Path | None
    #: The config.yaml app already using this bundle id, or None.
    configured_as: str | None


@dataclass(frozen=True)
class Found:
    term: str
    #: The store that was asked, upper-case.
    country: str
    hits: list[Hit]
    #: How the store was chosen, when there is something to say
    #: (`storefronts.Store.note`); None otherwise.
    note: str | None = None


class InvalidTerm(ConfigError):
    """A search term that isn't text: invalid UTF-8 from a Terminal set to
    another encoding, which Python hands over as lone surrogates. A usage
    error (exit 2), and an `appstore-search-failed` with this `reason`."""

    reason = itunes.INVALID_TERM


def check_term(term: str) -> None:
    """Refuse a term that can't be sent (`InvalidTerm`)."""
    try:
        term.encode("utf-8")
    except UnicodeEncodeError as e:
        raise InvalidTerm(
            "the search term isn't valid text (it has bytes that aren't UTF-8)",
            hint="set Terminal's text encoding to Unicode (UTF-8), then search again",
        ) from e


def search(
    cfg: Config, term: str, *, country: str | storefronts.Store | None = None,
    limit: int | None = None, with_icons: bool = True,
) -> Found:
    """Search the App Store for `term`, the way Add App's Search does.

    The term, `country` and `limit` are checked before anything is asked;
    `country` may be a store already resolved (`resolve_country`).
    `with_icons` is the app's case: a person reading a table in Terminal has
    no use for them, and the CLI fetches none.
    """
    check_term(term)
    store = country if isinstance(country, storefronts.Store) else resolve_country(country)
    results = itunes.search(term, country=store.code, limit=resolve_limit(limit))
    icons: dict[str, Path] = {}
    if with_icons:
        prune_icons()
        icons = fetch_icons(r.icon_url for r in results if r.icon_url)
    configured = configured_names(cfg)
    return Found(
        term=term,
        country=store.code,
        note=store.note,
        hits=[
            Hit(
                result=r,
                icon_path=icons.get(r.icon_url) if r.icon_url else None,
                configured_as=configured.get(r.bundle_id.casefold()),
            )
            for r in results
        ],
    )


def event_fields(hit: Hit) -> dict[str, Any]:
    """One `appstore-search-result` event's fields (json_output.EVENT_TYPES)."""
    r = hit.result
    return {
        "track_id": r.track_id,
        "bundle_id": r.bundle_id,
        "name": r.name,
        "developer": r.developer,
        "version": r.version,
        "price": r.price,
        "formatted_price": r.formatted_price,
        "currency": r.currency,
        "artwork_url": r.artwork_url,
        "icon_path": str(hit.icon_path) if hit.icon_path else None,
        "configured_as": hit.configured_as,
    }


#: What a listing with no usable price shows. Never "Free": only a listing
#: that says 0 is free, here and in the Mac app (`AppStoreSearchForm`).
PRICE_UNKNOWN = "price unknown"


def price_label(result: itunes.SearchResult) -> str:
    """The price as a person reads it: the store's own spelling first, and
    `PRICE_UNKNOWN` when the listing gives no number, whatever it spells."""
    if result.price is None:
        return PRICE_UNKNOWN
    if result.formatted_price:
        return result.formatted_price
    return "Free" if result.price == 0 else f"{result.price:.2f}"


#: An escape sequence: CSI (`ESC [ ... final`), OSC (`ESC ] ... BEL` or
#: `ESC \\`), DCS/SOS/PM/APC (`ESC P|X|^|_ ... ESC \\`), or a two-byte one.
_ESCAPE_SEQUENCE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]?|\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|[PX^_][^\x1b]*(?:\x1b\\)?|[ -~]?)"
)
#: C0 and C1 controls, and DEL.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def printable(text: str) -> str:
    """`text` with escape sequences and control characters removed.

    A listing's name, developer and price are written by whoever publishes
    the app, and the Terminal table prints them: an ESC in one could retitle
    the window, recolour the output or clear the screen above it. Markup is a
    separate matter, escaped where the table is drawn.
    """
    return _CONTROL.sub("", _ESCAPE_SEQUENCE.sub("", text))
