"""`graft --appstore-search` end to end, with the network faked.

What the Add App sheet, or a person in Terminal, sees: the event stream, the
exit status, stderr and the icon cache. `FakeITunes` replaces `requests.get`
and refuses any URL it doesn't answer, so no test here reaches Apple.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests
import yaml
from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa import host, itunes, storefronts
from graft_ipa.errors import ITunesError
from graft_ipa.itunes import ITunesApp
from tests.itunes_fakes import PNG, FakeITunes, listing


def _run(args: list[str]):
    return CliRunner().invoke(cli_mod.app, args)


def _events(result) -> list[dict]:
    return [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeITunes:
    f = FakeITunes()
    monkeypatch.setattr(requests, "get", f)
    return f


RESULT_FIELDS = {
    "type", "track_id", "bundle_id", "name", "developer", "version", "price",
    "formatted_price", "currency", "artwork_url", "icon_path", "configured_as",
}


def test_each_result_is_one_event_then_the_end(temp_config_env: Path, fake: FakeITunes) -> None:
    fake.results = [listing(1), listing(2, price=4.99, formattedPrice="$4.99")]
    fake.images = {listing(1)["artworkUrl100"]: PNG}

    result = _run(["--json", "--appstore-search", "example", "--country", "gb"])

    assert result.exit_code == 0, result.output
    first, second, end = _events(result)
    assert set(first) == RESULT_FIELDS and set(second) == RESULT_FIELDS
    assert (first["type"], first["name"], first["price"], first["formatted_price"]) == (
        "appstore-search-result", "Example 1", 0.0, "Free",
    )
    assert Path(first["icon_path"]).read_bytes() == PNG
    assert second["icon_path"] is None, "its icon answered 404"
    assert (second["price"], second["formatted_price"]) == (4.99, "$4.99")
    assert end == {"type": "appstore-search-end", "term": "example", "country": "GB", "count": 2, "note": None}
    assert fake.searches == [{"term": "example", "country": "GB", "entity": "software", "limit": 25}]


def test_a_result_already_in_config_names_the_app(temp_config_env: Path, fake: FakeITunes) -> None:
    fake.results = [listing(1, bundleId="com.example.app"), listing(2)]
    first, second, _ = _events(_run(["--json", "--appstore-search", "example", "--country", "us"]))
    assert (first["configured_as"], second["configured_as"]) == ("example", None)


def test_no_results_is_the_end_event_alone(temp_config_env: Path, fake: FakeITunes) -> None:
    result = _run(["--json", "--appstore-search", "zzzz", "--country", "us"])
    assert result.exit_code == 0, result.output
    assert _events(result) == [
        {"type": "appstore-search-end", "term": "zzzz", "country": "US", "count": 0, "note": None}
    ]


def test_without_a_country_it_asks_this_macs_region(
    temp_config_env: Path, fake: FakeITunes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host, "_read_apple_locale", lambda: "en_US@rg=dezzzz")
    result = _run(["--json", "--appstore-search", "example"])
    assert fake.searches[0]["country"] == "DE"
    assert _events(result)[-1]["country"] == "DE"


def test_with_no_region_it_asks_the_us_store(temp_config_env: Path, fake: FakeITunes) -> None:
    _run(["--json", "--appstore-search", "example"])
    assert fake.searches[0]["country"] == "US"


def test_a_region_with_no_store_asks_the_us_store_and_the_end_event_says_so(
    temp_config_env: Path, fake: FakeITunes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host, "_read_apple_locale", lambda: "fa_IR")
    result = _run(["--json", "--appstore-search", "example"])
    assert result.exit_code == 0, result.output
    assert fake.searches[0]["country"] == "US"
    assert _events(result)[-1]["note"] == storefronts.NO_STORE_NOTE


def test_a_territory_region_asks_the_store_it_shops_in_with_nothing_to_say(
    temp_config_env: Path, fake: FakeITunes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host, "_read_apple_locale", lambda: "es_PR")
    result = _run(["--json", "--appstore-search", "example"])
    assert fake.searches[0]["country"] == "US"
    assert _events(result)[-1]["note"] is None


def test_in_terminal_the_region_note_is_one_line(
    temp_config_env: Path, fake: FakeITunes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host, "_read_apple_locale", lambda: "fa_IR")
    fake.results = [listing(1)]
    result = _run(["--appstore-search", "example"])
    assert result.exit_code == 0, result.output
    assert result.stderr.count(storefronts.NO_STORE_NOTE) == 1
    assert storefronts.NO_STORE_NOTE not in result.stdout


def test_an_alias_country_is_searched_as_its_store_with_a_note(temp_config_env: Path, fake: FakeITunes) -> None:
    result = _run(["--json", "--appstore-search", "example", "--country", "uk"])
    assert result.exit_code == 0, result.output
    assert fake.searches[0]["country"] == "GB"
    end = _events(result)[-1]
    assert end["country"] == "GB" and "UK" in end["note"]


def test_a_failed_search_carries_the_note_too(
    temp_config_env: Path, fake: FakeITunes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host, "_read_apple_locale", lambda: "fa_IR")
    fake.error = requests.ConnectionError("no route to host")
    result = _run(["--json", "--appstore-search", "example"])
    assert result.exit_code == 13
    assert _events(result) == [{
        "type": "appstore-search-failed", "term": "example", "reason": "offline",
        "note": storefronts.NO_STORE_NOTE,
    }]


@pytest.mark.parametrize(("given", "sent"), [("3", 3), ("50", 50), ("500", 50)])
def test_the_limit_is_passed_and_capped(temp_config_env: Path, fake: FakeITunes, given: str, sent: int) -> None:
    _run(["--json", "--appstore-search", "example", "--country", "us", "--limit", given])
    assert fake.searches[0]["limit"] == sent


@pytest.mark.parametrize(
    ("args", "words"),
    [
        (["--appstore-search", "example", "--limit", "0"], "--limit must be at least 1"),
        (["--appstore-search", "example", "--country", "GBR"], "two-letter country code"),
        (["--appstore-search", "example", "--country", "ZZ"], "there is no App Store for ZZ"),
        (["--appstore-search", "   "], "needs something to search for"),
    ],
)
def test_a_bad_request_is_a_usage_error_and_asks_nothing(
    temp_config_env: Path, fake: FakeITunes, args: list[str], words: str
) -> None:
    result = _run(["--json", *args])
    assert result.exit_code == 2, result.output
    assert words in " ".join(result.stderr.split())
    assert fake.calls == []


def test_a_term_starting_with_a_dash_is_still_the_term(temp_config_env: Path, fake: FakeITunes) -> None:
    """The app sends `--appstore-search=<term>` for exactly this case."""
    result = _run(["--json", "--appstore-search=-ab", "--country", "us"])
    assert result.exit_code == 0, result.output
    assert fake.searches[0]["term"] == "-ab"


@pytest.mark.parametrize(
    ("break_it", "reason", "words"),
    [
        (lambda f: setattr(f, "error", requests.ConnectionError("no route to host")),
         "offline", "couldn't reach the App Store to search"),
        (lambda f: setattr(f, "status", 500), "http", "the App Store search answered HTTP 500"),
        (lambda f: setattr(f, "status", 429), "rate-limited", "Apple is limiting searches from this Mac"),
        (lambda f: setattr(f, "bad_json", True), "malformed", "isn't a list of apps"),
    ],
)
def test_a_failed_search_says_why_as_data_then_exits_13(
    temp_config_env: Path, fake: FakeITunes, break_it, reason: str, words: str
) -> None:
    break_it(fake)

    result = _run(["--json", "--appstore-search", "example", "--country", "us"])

    assert result.exit_code == ITunesError.exit_code == 13, result.output
    assert _events(result) == [
        {"type": "appstore-search-failed", "term": "example", "reason": reason, "note": None}
    ]
    assert words in " ".join(result.stderr.split())


def test_without_json_it_prints_a_table_and_fetches_no_icons(temp_config_env: Path, fake: FakeITunes) -> None:
    fake.results = [listing(1), listing(2, price=4.99, formattedPrice="$4.99", trackName="Ex [/x]")]
    fake.images = {listing(1)["artworkUrl100"]: PNG}

    result = _run(["--appstore-search", "example", "--country", "us"])

    assert result.exit_code == 0, result.output
    assert "Example 1" in result.stdout and "Free" in result.stdout and "$4.99" in result.stdout
    assert "Ex [/x]" in result.stdout, "a listing's name is printed, not parsed as markup"
    assert fake.image_requests == []


def test_a_400_names_the_store_in_terminal(temp_config_env: Path, fake: FakeITunes) -> None:
    fake.status = 400
    result = _run(["--appstore-search", "example", "--country", "gb"])
    assert result.exit_code == 13
    words = " ".join(result.stderr.split())
    assert "GB store (HTTP 400)" in words
    assert "try again" not in words.lower()


def test_a_term_that_is_not_utf8_says_why_as_data_then_exits_2(temp_config_env: Path, fake: FakeITunes) -> None:
    """A Terminal set to Latin-1: `$'caf\\xe9'` reaches Python as a lone surrogate."""
    result = _run(["--json", "--appstore-search=caf\udce9", "--country", "us"])

    assert result.exit_code == 2, result.output
    assert _events(result) == [
        {"type": "appstore-search-failed", "term": "caf\ufffd", "reason": "invalid-term", "note": None}
    ]
    assert "UTF-8" in result.stderr
    assert fake.calls == []


def test_the_table_strips_escape_sequences_and_escapes_markup(temp_config_env: Path, fake: FakeITunes) -> None:
    fake.results = [listing(
        1, trackName="Evil\x1b]0;PWNED\x07\x1b[31mRED\x1b[0m [bold]x[/bold]",
        artistName="Dev\x1b[2J\x9b", formattedPrice="\x1b[5mFree", bundleId="com.example.\x1b[8mhid",
    )]

    result = _run(["--appstore-search", "example", "--country", "us"])

    assert result.exit_code == 0, result.output
    assert "\x1b" not in result.stdout and "\x9b" not in result.stdout and "\x07" not in result.stdout
    flat = "".join(result.stdout.split())
    assert "EvilRED[bold]x[/bold]" in flat, "markup printed as text"
    assert "com.example.hid" in flat


def test_a_long_bundle_id_is_never_cut_and_names_stay_visible(
    temp_config_env: Path, fake: FakeITunes, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_id = "com.verylongcompanyname.products.verylongapplication.ext"
    fake.results = [listing(1, bundleId=long_id), listing(2)]
    for columns in ("80", "60"):
        monkeypatch.setenv("COLUMNS", columns)
        result = _run(["--appstore-search", "example", "--country", "us"])
        assert result.exit_code == 0, result.output
        assert "…" not in result.stdout, f"cut at {columns} columns"
        # Folded across lines, the id is still all there.
        cells = "".join(
            part.strip() for line in result.stdout.splitlines() for part in line.split("│")[4:5]
        )
        assert long_id in cells, f"{columns} columns:\n{result.stdout}"
        assert "Exam" in result.stdout and "Dev" in result.stdout, f"{columns} columns:\n{result.stdout}"


def test_the_footer_adds_one_with_the_store_it_came_from(temp_config_env: Path, fake: FakeITunes) -> None:
    fake.results = [listing(1)]
    result = _run(["--appstore-search", "example", "--country", "de"])
    assert "--store-country DE" in " ".join(result.stdout.split())


def test_country_and_limit_are_refused_beside_another_verb(temp_config_env: Path, fake: FakeITunes) -> None:
    result = _run(["--appstore-check", "--country", "us"])
    assert result.exit_code == 2
    assert "--country is not valid with --appstore-check" in " ".join(result.output.split())


def test_adding_a_result_writes_what_add_app_writes_for_its_url(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Add App adds a picked result with --add-app-manual, sending the store's
    name, its bundle id and that name again as the display name. That has to
    be the very entry --add-app writes from the app's URL: Search must not be
    a second, slightly different way to add the same app."""
    hit = listing(7)
    monkeypatch.setattr(itunes, "lookup", lambda track_id, country=None: ITunesApp(
        track_id=hit["trackId"], bundle_id=hit["bundleId"], name=hit["trackName"], version=hit["version"],
    ))

    by_url = _run(["--add-app", str(hit["trackId"]), "--delivery", "livecontainer"])
    by_search = _run([
        "--add-app-manual", "--name", hit["trackName"], "--bundle-id", hit["bundleId"],
        "--display-name", hit["trackName"], "--delivery", "livecontainer",
    ])

    assert by_url.exit_code == 0 and by_search.exit_code == 0, by_url.output + by_search.output
    apps = yaml.safe_load(temp_config_env.read_text())["apps"]
    assert apps["example_7"] == apps["example_7_2"]


@pytest.mark.parametrize(
    ("name", "bundle_id", "key"),
    [("微信", "com.example.xin", "xin"), ("カカオトーク", "com.example.Talk", "talk"),
     ("Яндекс Карты", "ru.example.maps-app", "maps_app"), ("微博", "com.example.微博", "app"),
     ("Example Pro", "com.example.pro", "example_pro")],
)
def test_a_name_with_nothing_to_slug_is_keyed_by_its_bundle_id(
    temp_config_env: Path, name: str, bundle_id: str, key: str
) -> None:
    """A CJK or Cyrillic store name has no ASCII to slug; three of them used to
    be keyed app, app_2 and app_3."""
    result = _run(["--json", "--add-app-manual", "--name", name, "--bundle-id", bundle_id,
                   "--display-name", name])
    assert result.exit_code == 0, result.output
    assert _events(result)[-1]["name"] == key
    again = _run(["--json", "--add-app-manual", "--name", name, "--bundle-id", bundle_id])
    assert _events(again)[-1]["name"] == f"{key}_2", "and then de-duplicated"


def test_a_search_never_touches_the_keychain_or_ipatool(
    temp_config_env: Path, fake: FakeITunes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Add App searches on every typing pause. A search is the public Search
    API and nothing else: no `security`, no `ipatool` (whose `auth info`
    reads the keychain), whichever the account and the tools set up."""
    import subprocess

    from graft_ipa import keychain

    ran: list[list[str]] = []
    real_run, real_popen = subprocess.run, subprocess.Popen

    def run(argv, *args, **kwargs):
        ran.append([str(a) for a in argv] if isinstance(argv, (list, tuple)) else [str(argv)])
        return real_run(argv, *args, **kwargs)

    class Popen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, argv, *args, **kwargs):
            ran.append([str(a) for a in argv] if isinstance(argv, (list, tuple)) else [str(argv)])
            super().__init__(argv, *args, **kwargs)

    def no_keychain(*_: object, **__: object):
        raise AssertionError("a search reached the keychain")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", Popen)
    monkeypatch.setattr(keychain, "_run", no_keychain)  # the seam every keychain call goes through
    fake.results = [listing(1)]
    fake.images = {listing(1)["artworkUrl100"]: PNG}

    result = _run(["--json", "--appstore-search", "example", "--country", "us"])

    assert result.exit_code == 0, result.output
    touched = [argv for argv in ran if any("security" in a or "ipatool" in a for a in argv)]
    assert touched == []
