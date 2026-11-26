"""`graft --check-update`: is a newer Graft published?

One GET to GitHub's latest-release API, compared with the version Graft.app
passes (`--current-version`) or this CLI's own. What it may send is the point
of most of these tests: a generic User-Agent, and nothing that identifies the
person, the Mac or an account.

The HTTP is faked one layer above the socket, at `HTTPAdapter.send`, so
everything `requests` does on the way there runs for real: the headers, and
whatever a `~/.netrc` or a redirect would add, are exactly what would go on
the wire. The suite's network guard is the backstop.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests
from requests.adapters import HTTPAdapter
from typer.testing import CliRunner

from graft_ipa import __version__, release_check
from graft_ipa import cli as cli_mod

URL = release_check.LATEST_RELEASE_URL
PAGE = "https://github.com/GreenglassT/Graft/releases/tag/"
#: Where GitHub sends the API's request for a renamed repository.
MOVED = "https://api.github.com/repositories/100000001/releases/latest"


def release(tag: str = "v2.0.1", **fields: Any) -> dict[str, Any]:
    """The latest-release answer, trimmed to the fields that matter plus one
    that doesn't (`body`)."""
    answer = {
        "tag_name": tag, "name": f"Graft {tag}", "html_url": f"{PAGE}{tag}",
        "draft": False, "prerelease": False, "body": "What changed.",
    }
    answer.update(fields)
    return answer


class FakeGitHub:
    """`HTTPAdapter.send` for the latest-release URL, and for `MOVED` when
    `redirect` is set. Any other URL fails the test."""

    def __init__(self) -> None:
        #: The JSON answered, or raw bytes for a body that isn't JSON.
        self.body: Any = release()
        self.status = 200
        #: Raised instead of answering, when set.
        self.error: Exception | None = None
        #: The first answer is a 301 to `MOVED`.
        self.redirect = False
        #: Every request that would have gone out, with the adapter's keyword arguments.
        self.sent: list[tuple[requests.PreparedRequest, dict[str, Any]]] = []

    def __call__(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        self.sent.append((request, kwargs))
        if self.error is not None:
            raise self.error
        resp = requests.Response()
        resp.request, resp.url, resp.encoding = request, request.url, "utf-8"
        resp._content_consumed = True
        if self.redirect and request.url == URL:
            resp.status_code, resp._content = 301, b""
            resp.headers["Location"] = MOVED
            return resp
        assert request.url in {URL, MOVED}, f"a request to {request.url}, which no test here answers"
        resp.status_code = self.status
        resp._content = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        return resp


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(HTTPAdapter, "send", fake)
    return fake


def _check(current: str = "2.0") -> dict[str, Any]:
    """`--check-update`'s event, as the CLI would emit it."""
    try:
        found = release_check.latest_release()
    except release_check.UpdateCheckError as e:
        return release_check.event(current, error=e)
    return release_check.event(current, release=found)


# --- What it finds ----------------------------------------------------------


def test_a_newer_release_is_available(github: FakeGitHub) -> None:
    assert _check("2.0") == {
        "ok": True, "current": "2.0", "latest": "2.0.1", "url": PAGE + "v2.0.1",
        "update_available": True, "needs_macos": None, "reason": None, "status": None,
        "message": None,
    }


@pytest.mark.parametrize(("tag", "current"), [
    ("v2.0", "2.0.0"), ("v2.0.0", "2.0"), ("2.0.0", "2.0"), ("v2.0.1", "2.0.1"), ("v2", "2.0.0"),
])
def test_the_same_version_is_not_an_update_however_it_is_spelled(
    github: FakeGitHub, tag: str, current: str
) -> None:
    github.body = release(tag)
    found = _check(current)
    assert found["ok"] is True
    assert found["update_available"] is False


@pytest.mark.parametrize("tag", ["v1.9.9", "v1.10", "v2.0.0"])
def test_an_older_release_is_not_an_update(github: FakeGitHub, tag: str) -> None:
    github.body = release(tag)
    assert _check("2.0.1")["update_available"] is False


@pytest.mark.parametrize(("tag", "current"), [
    ("v2.0.10", "2.0.9"),  # numbers, not text: "10" sorts before "9" as a string
    ("v2.1", "2.0.99"),
    ("v10.0", "9.9.9"),
    ("v2.0.0.1", "2.0"),
])
def test_versions_compare_as_numbers(github: FakeGitHub, tag: str, current: str) -> None:
    github.body = release(tag)
    assert _check(current)["update_available"] is True


@pytest.mark.parametrize(("tag", "shown"), [
    ("v2.0.1", "2.0.1"), ("V2.0.1", "2.0.1"), ("2.0.1", "2.0.1"), (" v2.0.1 ", "2.0.1"),
])
def test_the_v_prefix_is_optional_and_not_shown(github: FakeGitHub, tag: str, shown: str) -> None:
    github.body = release(tag)
    assert _check()["latest"] == shown


@pytest.mark.parametrize("html_url", [
    "https://example.invalid/phish",
    None,
    201,
    "",
    # Under the releases, but not this release's page: `..` climbs out of
    # them in a browser, whether spelled out or percent-encoded.
    "https://github.com/GreenglassT/Graft/releases/../../../example/phish",
    "https://github.com/GreenglassT/Graft/releases/tag/v2.0.1/../../../../../example/phish",
    "https://github.com/GreenglassT/Graft/releases/%2e%2e/%2e%2e/%2e%2e/example/phish",
    "https://github.com/GreenglassT/Graft/releases/tag/..%2f..%2f..%2fexample",
    "https://github.com/GreenglassT/Graft/releases/tag/v2.0.1?next=https://example.invalid",
    "https://github.com/GreenglassT/Graft/releases/tag/v2.0.1#x",
    "https://github.com/GreenglassT/Graft/releases/tag/v9.9.9",
    "https://github.com/GreenglassT/Graft/releases/latest/../tag/v2.0.1",
    # Another host, however close.
    "https://github.com.example.invalid/GreenglassT/Graft/releases/tag/v2.0.1",
    "https://github.com@example.invalid/GreenglassT/Graft/releases/tag/v2.0.1",
    "https://github.com:8443/GreenglassT/Graft/releases/tag/v2.0.1",
    "https://github.com\\@example.invalid/GreenglassT/Graft/releases/tag/v2.0.1",
    "http://github.com/GreenglassT/Graft/releases/tag/v2.0.1",
    " https://github.com/GreenglassT/Graft/releases/tag/v2.0.1",
])
def test_anything_but_this_releases_own_page_falls_back_to_the_releases(
    github: FakeGitHub, html_url: Any
) -> None:
    """Terminal prints `url` and the update hints name it, so it is only ever
    a page of this project's releases. (Graft.app builds its own link from
    `latest`, and never opens this one.)"""
    github.body = release(html_url=html_url)
    assert _check()["url"] == "https://github.com/GreenglassT/Graft/releases/latest"


@pytest.mark.parametrize(("tag", "page"), [
    ("v2.0.1", PAGE + "v2.0.1"),
    ("2.0.1", PAGE + "2.0.1"),
])
def test_this_releases_own_page_is_kept(github: FakeGitHub, tag: str, page: str) -> None:
    github.body = release(tag, html_url=page)
    assert _check()["url"] == page


# --- How it fails: quietly, with a reason -----------------------------------


@pytest.mark.parametrize("body", [
    b"<html>not json</html>",
    [release()],                            # an array, not an object
    {"message": "Not Found"},               # no tag_name
    release(tag=None),
    release(tag=201),                       # not text
    release(tag="nightly"),
    release(tag="v2.1.0-beta.1"),
    release(tag="v2..1"),
    release(tag=""),
    release(draft=True),                    # the endpoint excludes these; ignored anyway
    release("v9.0", prerelease=True),
])
def test_an_answer_that_names_no_published_version_is_malformed(github: FakeGitHub, body: Any) -> None:
    github.body = body
    found = _check()
    assert found["ok"] is False
    assert found["reason"] == release_check.MALFORMED
    assert found["update_available"] is False
    assert found["latest"] is None and found["url"] is None
    assert found["message"]


@pytest.mark.parametrize("status", [404, 403, 429, 500, 502])
def test_an_http_error_is_a_failure_with_its_status(github: FakeGitHub, status: int) -> None:
    github.status, github.body = status, {"message": "nope"}
    found = _check()
    assert (found["ok"], found["reason"], found["status"]) == (False, release_check.HTTP, status)
    assert found["update_available"] is False
    assert found["message"]


@pytest.mark.parametrize("status", [403, 429])
def test_github_s_rate_limit_says_so(github: FakeGitHub, status: int) -> None:
    github.status = status
    assert "limiting" in _check()["message"]


@pytest.mark.parametrize("error", [
    requests.ConnectTimeout("connect timed out"),
    requests.ReadTimeout("read timed out"),
    requests.ConnectionError("no route to host"),
])
def test_no_answer_is_offline(github: FakeGitHub, error: Exception) -> None:
    github.error = error
    found = _check()
    assert (found["ok"], found["reason"], found["status"]) == (False, release_check.OFFLINE, None)
    assert "internet connection" in found["message"]


def test_the_timeout_is_short(github: FakeGitHub) -> None:
    _check()
    (_, kwargs), = github.sent
    assert kwargs["timeout"] == release_check.TIMEOUT
    assert 0 < release_check.TIMEOUT <= 10


# --- What it sends ----------------------------------------------------------


def test_one_get_to_the_latest_release_with_a_generic_user_agent(github: FakeGitHub) -> None:
    _check()
    (request, _), = github.sent
    assert (request.method, request.url, request.body) == ("GET", URL, None)
    assert request.headers["User-Agent"] == "Graft"
    assert __version__ not in json.dumps(dict(request.headers))
    # requests' own transport headers, and the API's media type. Nothing else:
    # no cookie, no credentials, nothing naming the Mac or the person.
    assert set(request.headers) == {"User-Agent", "Accept", "Accept-Encoding", "Connection"}
    assert request.headers["Accept"] == "application/vnd.github+json"


def test_a_netrc_login_for_github_is_not_sent_even_through_a_redirect(
    github: FakeGitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """requests adds a `~/.netrc` login for the host on its own, to the
    request and again to a redirect (a renamed repository's API answers 301).
    The check must send neither."""
    netrc = tmp_path / "netrc"
    netrc.write_text("machine api.github.com login someone password not-a-real-token\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))
    # The premise: a plain `requests.get` would send it.
    requests.get(URL, timeout=1)
    assert "Authorization" in github.sent.pop()[0].headers

    github.redirect = True
    assert _check()["latest"] == "2.0.1"

    assert [r.url for r, _ in github.sent] == [URL, MOVED]
    for request, _ in github.sent:
        assert "Authorization" not in request.headers
        assert request.headers["User-Agent"] == "Graft"


def test_the_system_proxy_is_still_used(github: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    """Credentials from the environment are refused; a proxy isn't one, and a
    Mac that reaches the internet only through one must still be able to
    check."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.invalid:3128")
    _check()
    (_, kwargs), = github.sent
    assert kwargs["proxies"].get("https") == "http://proxy.example.invalid:3128"


def test_the_check_never_asks_this_macs_own_proxy_settings(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """conftest's `_no_real_proxy_settings`. With no proxy in the
    environment, urllib asks SystemConfiguration, which leaves native threads
    running in the test process: every later fork in the run then warned
    "multi-threaded process". These tests were the first to reach it."""
    import urllib.request

    asked: list[str] = []
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setattr(urllib.request, "_get_proxies", lambda: asked.append("proxies") or {}, raising=False)
    monkeypatch.setattr(
        urllib.request, "_get_proxy_settings", lambda: asked.append("bypass") or {}, raising=False
    )
    assert _check()["latest"] == "2.0.1"
    assert asked == []


# --- The verb ---------------------------------------------------------------


def _run(argv: list[str]) -> Any:
    return CliRunner().invoke(cli_mod.app, argv)


def test_json_emits_one_update_check_event(github: FakeGitHub) -> None:
    result = _run(["--json", "--check-update", "--current-version", "2.0"])

    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events == [{
        "type": "update-check", "ok": True, "current": "2.0", "latest": "2.0.1",
        "url": PAGE + "v2.0.1", "update_available": True, "needs_macos": None,
        "reason": None, "status": None, "message": None,
    }]


def test_without_current_version_it_compares_with_this_cli(github: FakeGitHub) -> None:
    github.body = release("v" + __version__)
    result = _run(["--json", "--check-update"])

    assert result.exit_code == 0, result.output
    (event,) = [json.loads(line) for line in result.stdout.splitlines()]
    assert (event["current"], event["update_available"]) == (__version__, False)


def test_a_failed_check_says_so_in_the_event_and_the_exit_status(github: FakeGitHub) -> None:
    github.error = requests.ConnectionError("no route to host")
    result = _run(["--json", "--check-update", "--current-version", "2.0"])

    assert result.exit_code == 1
    (event,) = [json.loads(line) for line in result.stdout.splitlines()]
    assert (event["type"], event["ok"], event["reason"]) == ("update-check", False, "offline")
    # Terminal reads the same sentence, on stderr.
    assert " ".join(event["message"].split()) in " ".join(result.stderr.split())


def test_in_terminal_it_says_what_is_available(github: FakeGitHub) -> None:
    result = _run(["--check-update", "--current-version", "2.0"])

    assert result.exit_code == 0, result.output
    said = " ".join(result.stdout.split())
    assert "Graft 2.0.1 is available" in said
    assert PAGE + "v2.0.1" in said


def test_in_terminal_it_says_up_to_date(github: FakeGitHub) -> None:
    github.body = release("v2.0")
    result = _run(["--check-update", "--current-version", "2.0.0"])

    assert result.exit_code == 0, result.output
    assert "up to date" in result.stdout


@pytest.mark.parametrize("given", ["", "abc", "2.0-beta", "-1", "2..0"])
def test_a_current_version_that_is_not_one_is_refused_before_asking(
    github: FakeGitHub, given: str
) -> None:
    result = _run(["--json", "--check-update", "--current-version", given])

    assert result.exit_code == 2, result.output
    assert "--current-version" in result.stderr
    assert github.sent == []
    assert result.stdout == ""


def test_current_version_needs_check_update(github: FakeGitHub, temp_config_env: Path) -> None:
    result = _run(["--json", "--list", "--current-version", "2.0"])

    assert result.exit_code == 2
    assert "--current-version is not valid with --list" in " ".join(result.stderr.split())
    assert github.sent == []


def test_it_needs_no_config_and_touches_no_user_folder(github: FakeGitHub, tmp_path: Path) -> None:
    """The app checks before setup is done, and the check is about the
    program, not the user's files: no config is read, and no folder is
    made, moved or re-permissioned."""
    before = sorted(tmp_path.rglob("*"))

    result = _run(["--json", "--check-update", "--current-version", "2.0"])

    assert result.exit_code == 0, result.output
    assert sorted(tmp_path.rglob("*")) == before
