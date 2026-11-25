"""No App Store request from ipatool where decryption can't run: on an Intel
Mac, or in the Intel build of Graft.app (`host.apple_silicon_blocker`).

Every ipatool Store request (sign-in, a get, a free claim, the list of older
versions) runs ipatool's Unicorn emulator. On x86_64 its code buffer is
mapped writable and executable without MAP_JIT, which the bundled ipatool's
entitlements (allow-jit only) don't allow, so the first such request would
likely crash it. And nothing a sign-in is for (a get, a claim, an older
version) can be decrypted there anyway. So each is refused before ipatool
is run at all: the stand-in ipatool here records every run, and none may
happen. Searching the App Store and adding an app by name go through the
public iTunes API (`itunes`), which needs no ipatool.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from graft_ipa import appstore, appstore_login, host, itunes
from graft_ipa import cli as cli_mod
from graft_ipa.config import Config, load_config
from graft_ipa.errors import AppStoreError
from graft_ipa.json_output import JsonEmitter
from tests.appstore_fakes import FakeTools

#: The refusal's sentence, before the reason (`host.INTEL_MAC` or
#: `host.INTEL_BUILD`).
NEEDS = "App Store sign-in and downloads need a Mac with Apple silicon and Graft.zip: "


@pytest.fixture
def fakes(tmp_path: Path) -> FakeTools:
    return FakeTools(tmp_path / "tools")


@pytest.fixture
def config(temp_config_env: Path, fakes: FakeTools) -> Path:
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["appstore"] = fakes.appstore_section()
    temp_config_env.write_text(yaml.safe_dump(raw, sort_keys=False))
    return temp_config_env


@pytest.fixture
def cfg(config: Path) -> Config:
    return load_config(config)


@pytest.fixture(autouse=True)
def _free_and_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A claim looks the price up first, from the public iTunes API: free,
    and never the network."""
    def lookup(bundle_id: str, *, country: str | None = None) -> itunes.ITunesApp:
        return itunes.ITunesApp(track_id=100000001, bundle_id=bundle_id, name="Example", version="2.0",
                                price=0.0, formatted_price="Free")

    monkeypatch.setattr(itunes, "lookup_by_bundle_id", lookup)


def _intel_mac(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(host, "_sysctl_arm64", lambda: None)  # no such sysctl: Intel
    return host.INTEL_MAC


def _intel_build(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(host, "_sysctl_arm64", lambda: 1)  # Apple silicon
    monkeypatch.setattr(host, "_running_flavor", lambda: "x86_64")
    return host.INTEL_BUILD


def _apple_silicon_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """Graft.zip on an Apple silicon Mac, both pinned as the refusals pin
    theirs: never the reading of the Mac that runs the suite."""
    monkeypatch.setattr(host, "_sysctl_arm64", lambda: 1)  # Apple silicon
    monkeypatch.setattr(host, "_running_flavor", lambda: "arm64")


WHERE = [pytest.param(_intel_mac, id="an Intel Mac"), pytest.param(_intel_build, id="the Intel build")]


def _run(args: list[str], **kw: object):
    return CliRunner().invoke(cli_mod.app, args, **kw)


def _refused(result, reason: str) -> None:
    assert result.exit_code == AppStoreError.exit_code, result.output
    said = " ".join(result.stderr.split())
    assert NEEDS + reason in said, said
    assert "Searching the App Store and adding an app by name still work" in said, said


@pytest.mark.parametrize("where", WHERE)
@pytest.mark.parametrize("verb", [
    ["--appstore-get", "--app", "example"],
    ["--appstore-versions", "--app", "example"],
    ["--appstore-claim", "--app", "example"],
])
def test_no_get_claim_or_version_list_runs_ipatool(
    config: Path, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch,
    where: Callable[[pytest.MonkeyPatch], str], verb: list[str],
) -> None:
    reason = where(monkeypatch)
    result = _run(["--json", *verb])
    _refused(result, reason)
    assert fakes.calls("ipatool") == [], "ipatool never ran, not even for its version"


@pytest.mark.parametrize("where", WHERE)
def test_no_sign_in_runs_ipatool_or_reads_the_account(
    cfg: Config, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch,
    where: Callable[[pytest.MonkeyPatch], str],
) -> None:
    """Refused before the Apple ID or the password is read from stdin, and
    said as the sheet hears every failed sign-in: an `appstore-login`
    failed event with its own reason, then the raise."""
    reason = where(monkeypatch)
    asked: list[str] = []

    def read_line(prompt: str, timeout: float | None, *, echo: bool = False):
        asked.append(prompt)
        raise AssertionError("nothing is read")

    from io import StringIO

    out = StringIO()
    with pytest.raises(AppStoreError) as caught:
        appstore_login.run(cfg, emitter=JsonEmitter(stream=out), say=lambda _: None, read_line=read_line)

    assert caught.value.message == NEEDS + reason
    assert asked == []
    assert fakes.calls("ipatool") == []
    (event,) = [json.loads(line) for line in out.getvalue().splitlines()]
    assert event["type"] == "appstore-login"
    assert event["state"] == appstore_login.STATE_FAILED
    assert event["reason"] == appstore_login.REASON_NEEDS_APPLE_SILICON
    assert event["message"] == NEEDS + reason


def test_on_apple_silicon_with_the_apple_silicon_build_ipatool_is_asked(
    cfg: Config, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the same get, where it can run, does run ipatool."""
    _apple_silicon_build(monkeypatch)
    assert host.apple_silicon_blocker() is None
    with appstore.fetched(cfg, "example", emitter=None) as ipa:
        assert ipa.exists()
    assert fakes.calls("ipatool")


def test_a_status_read_still_runs_where_store_requests_dont(
    cfg: Config, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--appstore-status` makes no Store request (`auth info` reads the
    login keychain), so Settings › App Store can still say what's there."""
    _intel_build(monkeypatch)
    payload = appstore.status(cfg.appstore)
    assert payload["ipatool"]["present"] is True
