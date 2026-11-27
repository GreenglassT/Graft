"""Whether a newer Graft is published: `graft --check-update`.

One GET to GitHub's latest-release API, which already leaves out drafts and
prereleases. Graft.app runs it at most once a day while it is open (Settings
turns that off), and when you choose Help › Check for Updates…. It downloads
nothing and installs nothing: `app_update` does that, from the same answer
(`latest_release_json`).

It sends as little as a request can: a generic User-Agent, which GitHub's API
requires, and nothing else that names the person, the Mac, an account or this
copy's version. Not `graft_ipa.USER_AGENT`, which carries the version. And not
the environment's credentials: with `trust_env` on, requests adds a
`~/.netrc` login for the host by itself, to the request and again to a
redirect. The environment's proxy is still used, as for every other request.

Versions are dotted numbers with an optional "v" ("v2.0.1"), compared as
numbers with trailing zeros ignored, so the app's "2.0" is the CLI's "2.0.0".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .errors import GraftError

LATEST_RELEASE_URL = "https://api.github.com/repos/GreenglassT/Graft/releases/latest"
#: Graft's releases. The events' `url` is this release's page under it, or
#: `/latest` (`release_page`). Graft.app doesn't open that `url`: it builds
#: its own link from `latest` (`UpdateStore.releasePage`).
RELEASES_PAGE = "https://github.com/GreenglassT/Graft/releases"
#: Generic on purpose: GitHub's API refuses a request without one.
USER_AGENT = "Graft"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
#: Seconds, to connect and then between bytes: a check is never worth a
#: long wait, but 10 failed once on a link another download was filling
#: (the 2.3 self-update's own, at about 1 MB/s), where the retry passed
#: (the 2.3.1 review's Track 4 item 1).
TIMEOUT = 30

#: Why a check failed, the event's `reason`: no connection to GitHub, one
#: that didn't answer within `TIMEOUT`, an HTTP error, or an answer that
#: names no published release.
OFFLINE = "offline"
TIMED_OUT = "timeout"
HTTP = "http"
MALFORMED = "malformed"

_VERSION_RE = re.compile(r"[vV]?(\d+(?:\.\d+)*)", re.ASCII)


class UpdateCheckError(GraftError):
    """The check got no usable answer. `reason` is one of the four above,
    and `status` the HTTP status, when there was one."""

    def __init__(self, message: str, *, reason: str, status: int | None = None) -> None:
        super().__init__(message, hint=f"releases are listed at {RELEASES_PAGE}")
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class Release:
    #: "2.0.1": the tag without its "v".
    version: str
    url: str


def parse_version(text: object) -> tuple[int, ...] | None:
    """`"v2.0.1"` → (2, 0, 1), with trailing zeros dropped so that "2.0" and
    "2.0.0" compare equal; None for anything that isn't a plain release
    version ("nightly", "2.1.0-beta.1")."""
    if not isinstance(text, str):
        return None
    match = _VERSION_RE.fullmatch(text.strip())
    if match is None:
        return None
    parts = [int(part) for part in match.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    """Whether `latest` is a later version than `current`. Both must parse."""
    newer, have = parse_version(latest), parse_version(current)
    assert newer is not None and have is not None, (latest, current)
    return newer > have


def latest_release() -> Release:
    """The latest published release, as GitHub's API names it. Raises
    `UpdateCheckError`."""
    return release_from(latest_release_json())


def latest_release_json() -> dict[str, Any]:
    """GitHub's answer about the latest published release, whole: the
    check reads its tag and page (`release_from`), and `app_update` its
    `assets` too. Raises `UpdateCheckError`."""
    # Function-local, as in every module here that speaks HTTP
    # (tests/test_lazy_imports.py): `cli.py` imports this one at module scope.
    import requests

    session = requests.Session()
    # No credentials from the environment (the module docstring), but its
    # proxy, which macOS's own settings can supply too.
    session.trust_env = False
    session.proxies.update(requests.utils.get_environ_proxies(LATEST_RELEASE_URL))
    try:
        with session:
            resp = session.get(LATEST_RELEASE_URL, headers=HEADERS, timeout=TIMEOUT)
    except requests.Timeout as e:
        # Slow, not offline: a busy link, or GitHub itself. "Check this Mac's
        # internet connection" sent the person looking for a fault that
        # wasn't there. Ahead of the arm below: a ConnectTimeout is a
        # ConnectionError too.
        raise UpdateCheckError(
            "GitHub didn't answer in time. Try again in a moment.",
            reason=TIMED_OUT,
        ) from e
    except requests.RequestException as e:
        raise UpdateCheckError(
            "Couldn't reach GitHub to check for a newer Graft. Check this Mac's internet "
            "connection, then try again.",
            reason=OFFLINE,
        ) from e
    if resp.status_code in (403, 429):
        raise UpdateCheckError(
            "GitHub is limiting requests from this network for now. Try again in an hour.",
            reason=HTTP, status=resp.status_code,
        )
    if resp.status_code != 200:
        raise UpdateCheckError(
            f"GitHub answered the update check with HTTP {resp.status_code}. Try again later.",
            reason=HTTP, status=resp.status_code,
        )
    return _json(resp)


def _json(resp: Any) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise _malformed()
    return data


def _malformed() -> UpdateCheckError:
    return UpdateCheckError(
        "GitHub's answer to the update check didn't name a published release.",
        reason=MALFORMED,
    )


def release_from(data: dict[str, Any]) -> Release:
    """The release `data` names, or `UpdateCheckError` (malformed) when it
    names no published version."""
    # The endpoint never answers with either; one that did isn't an offer.
    if data.get("draft") or data.get("prerelease"):
        raise _malformed()
    tag = data.get("tag_name")
    if parse_version(tag) is None:
        raise _malformed()
    assert isinstance(tag, str)
    return Release(version=tag.strip().lstrip("vV"), url=release_page(data.get("html_url"), tag.strip()))


def release_page(url: object, tag: str) -> str:
    """`url` when it is exactly `tag`'s page under `RELEASES_PAGE`, else the
    releases' `/latest` page. Terminal prints it and the update hints name
    it, so it is only ever a page of this project's releases.

    Equal to the page, not just starting with `RELEASES_PAGE`: a prefix let
    `…/releases/../../../elsewhere` through, which a browser resolves to
    another page on github.com. And compared whole rather than parsed and
    normalized: `%2e%2e`, a backslash, `user@host` or a stray port are each
    read one way by a parser here and another by a browser, and an exact
    match has nothing to read. `tag` has passed `parse_version` (ASCII digits
    and dots, and a "v"), so the page needs no quoting."""
    page = f"{RELEASES_PAGE}/tag/{tag}"
    if isinstance(url, str) and url == page:
        return url
    return RELEASES_PAGE + "/latest"


def event(
    current: str, *, release: Release | None = None, error: UpdateCheckError | None = None,
    needs_macos: str | None = None,
) -> dict[str, Any]:
    """The `update-check` event's fields: the same keys whether the check
    worked or not, so the app decodes one shape.

    `needs_macos` is the macOS the latest release needs, when a download
    found this Mac's older (`app_update.noted_refusal`): that release is no
    update for this Mac, so `update_available` is false and the app's banner
    offers nothing it would refuse. Null otherwise."""
    if error is not None:
        return {
            "ok": False, "current": current, "latest": None, "url": None,
            "update_available": False, "needs_macos": None,
            "reason": error.reason, "status": error.status, "message": error.message,
        }
    assert release is not None
    return {
        "ok": True, "current": current, "latest": release.version, "url": release.url,
        "update_available": is_newer(release.version, current) and needs_macos is None,
        "needs_macos": needs_macos,
        "reason": None, "status": None, "message": None,
    }
