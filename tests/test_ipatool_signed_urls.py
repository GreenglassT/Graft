"""ipatool's errors can carry the IPA's signed download URL: Go's transport
errors quote the URL they failed on (`Get "https://iosapps.itunes.apple.com/
…?accessKey=…": net/http: timeout awaiting response headers`), and that
query string is what lets anyone fetch the file. Graft takes the query off
every http(s) URL where ipatool's words first become a Python string
(`ipatool._failure`), so no message, hint, `--json` event, stderr line,
versions sheet row or file Graft writes ever has it.

Against the stand-in ipatool (`appstore_fakes`), never the real one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from graft_ipa import appstore_login, ipatool, itunes
from graft_ipa import cli as cli_mod
from tests.appstore_fakes import FakeTools, itunes_offline

#: What only the signed URL's query carries.
SECRET = "1764000000_123456789_AbCdEfSecretKey0123%2Bxyz"
SIGNED = (
    "https://iosapps.itunes.apple.com/itunes-assets/Purple221/v4/aa/bb/cc/"
    f"0000-1111/example.signed.dpkg.ipa?accessKey={SECRET}&a=1&b=2"
)
#: As Go's net/http reports a request that stalled.
STALLED = (
    f'failed to open zip reader: request failed: received error: Get "{SIGNED}": '
    "net/http: timeout awaiting response headers"
)
#: What is left of it: the host and path, so the error still says where.
LEFT = "https://iosapps.itunes.apple.com/itunes-assets/Purple221/v4/aa/bb/cc/0000-1111/example.signed.dpkg.ipa?…"


@pytest.mark.parametrize(
    ("text", "want"),
    [
        (STALLED, f'failed to open zip reader: request failed: received error: Get "{LEFT}": '
                  "net/http: timeout awaiting response headers"),
        (f"{SIGNED} and http://example.invalid/x?y=1#z", f"{LEFT} and http://example.invalid/x?…"),
        ("https://buy.itunes.apple.com/WebObjects/MZFinance.woa/wa/buyProduct?guid=0011#f",
         "https://buy.itunes.apple.com/WebObjects/MZFinance.woa/wa/buyProduct?…"),
        ("HTTPS://IOSAPPS.ITUNES.APPLE.COM/a.ipa?accessKey=x", "HTTPS://IOSAPPS.ITUNES.APPLE.COM/a.ipa?…"),
        ("https://p25-buy.itunes.apple.com/x#frag", "https://p25-buy.itunes.apple.com/x?…"),
        ("no URL here: accessKey is a word", "no URL here: accessKey is a word"),
        ("https://example.invalid/plain/path stays", "https://example.invalid/plain/path stays"),
        (f"'{SIGNED}'", f"'{LEFT}'"),
    ],
)
def test_a_url_keeps_its_host_and_path_and_loses_its_query(text: str, want: str) -> None:
    assert ipatool.without_url_queries(text) == want
    assert ipatool.without_url_queries(want) == want, "twice is once"


@pytest.fixture(autouse=True)
def _no_price_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(itunes, "lookup_by_bundle_id", itunes_offline)


@pytest.fixture
def fakes(tmp_path: Path) -> FakeTools:
    return FakeTools(tmp_path / "tools").set(versions=["800100"], version_metadata={})


@pytest.fixture
def config(temp_config_env: Path, fakes: FakeTools) -> Path:
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["appstore"] = fakes.appstore_section()
    temp_config_env.write_text(yaml.safe_dump(raw, sort_keys=False))
    return temp_config_env


def _never_kept(tmp_path: Path, *outputs: str) -> None:
    """The query is in none of `outputs`, nor in any file under tmp_path (the
    config, cache and state Graft writes: versions cache, build logs, history)
    or HOME, but the stand-in's own scenario and call record."""
    for out in outputs:
        assert SECRET not in out and "accessKey" not in out, out
    tools = tmp_path / "tools"
    for root in (tmp_path, Path(os.environ["HOME"])):
        for path in root.rglob("*"):
            if path.is_file() and not path.is_relative_to(tools):
                assert SECRET.encode() not in path.read_bytes(), path


def _run(args: list[str]):
    return CliRunner().invoke(cli_mod.app, args)


@pytest.mark.parametrize("json_mode", [True, False])
def test_a_version_ipatool_couldnt_read_never_shows_its_link(
    config: Path, fakes: FakeTools, tmp_path: Path, json_mode: bool
) -> None:
    fakes.set(version_metadata={"800100": {"error": STALLED}})
    result = _run([*(["--json"] if json_mode else []), "--appstore-versions", "--app", "example"])
    assert result.exit_code == 0, result.output
    if json_mode:
        (row,) = [e for e in map(json.loads, result.stdout.splitlines()) if e["type"] == "appstore-release"]
        assert LEFT in row["error"], "the row still says where it failed"
    _never_kept(tmp_path, result.stdout, result.stderr)


def test_a_list_that_failed_never_shows_its_link(config: Path, fakes: FakeTools, tmp_path: Path) -> None:
    fakes.set(list_versions_error=STALLED)
    result = _run(["--json", "--appstore-versions", "--app", "example"])
    assert result.exit_code == 20
    _never_kept(tmp_path, result.stdout, result.stderr)


@pytest.mark.parametrize("on_stderr", [False, True])
def test_a_get_that_failed_never_shows_its_link(
    config: Path, fakes: FakeTools, tmp_path: Path, on_stderr: bool
) -> None:
    fakes.set(download_error=STALLED, error_on_stderr=on_stderr)
    result = _run(["--json", "--appstore-get", "--app", "example"])
    assert result.exit_code == 20
    assert "iosapps.itunes.apple.com" in result.stderr, "it still says where it failed"
    _never_kept(tmp_path, result.stdout, result.stderr)


def test_a_get_that_said_no_verdict_never_shows_its_link(config: Path, fakes: FakeTools, tmp_path: Path) -> None:
    """No JSON verdict: the error is ipatool's last line, as it printed it."""
    fakes.set(download_raw=STALLED, download_exit=1)
    result = _run(["--appstore-get", "--app", "example"])
    assert result.exit_code == 20
    _never_kept(tmp_path, result.stdout, result.stderr)


def test_a_claim_that_failed_never_shows_its_link(
    config: Path, fakes: FakeTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(itunes, "lookup_by_bundle_id", lambda bundle_id, *, country=None: itunes.ITunesApp(
        track_id=100000001, bundle_id=bundle_id, name="Example", version="2.0", price=0.0, formatted_price="Free"))
    fakes.set(purchase_error=STALLED)
    result = _run(["--json", "--appstore-claim", "--app", "example"])
    assert result.exit_code == 20
    _never_kept(tmp_path, result.stdout, result.stderr)


def test_a_sign_out_that_failed_never_shows_its_link(config: Path, fakes: FakeTools, tmp_path: Path) -> None:
    fakes.set(revoke_error=STALLED)
    result = _run(["--json", "--appstore-logout"])
    assert result.exit_code == 20
    _never_kept(tmp_path, result.stdout, result.stderr)


@pytest.mark.parametrize("words", [STALLED, f"failed to download Unicorn: {STALLED}"])
def test_a_sign_in_that_failed_never_shows_its_link(words: str) -> None:
    failed = appstore_login.classify(words)
    assert SECRET not in failed.message and SECRET not in (failed.hint or "")


@pytest.mark.parametrize("words", [STALLED, f"load Unicorn: {STALLED}", f"license is required {STALLED}"])
def test_what_explains_an_error_never_repeats_its_link(words: str, tmp_path: Path) -> None:
    """explain, explain_purchase and account_error scrub what they're handed
    too, whoever hands it."""
    path = tmp_path / "ipatool"
    found = ipatool.account_error(words, bundle_id="com.example.app", path=path)
    for err in (ipatool.explain(words, bundle_id="com.example.app", path=path, reported_version=None),
                ipatool.explain_purchase(words, bundle_id="com.example.app", path=path),
                *([found] if found is not None else [])):
        assert SECRET not in err.message and SECRET not in (err.hint or ""), err
