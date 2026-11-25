"""`--appstore-get` and a version that is already staged.

A get downloads the App Store's current version, which can be hundreds of
megabytes, and decrypts it. When the cache already holds that version as a get
stages it, the get stops before ipatool runs and says so: in Terminal, "already
have <version>"; to the app, one `appstore-already-staged` event; exit 0.
`--again` downloads it anyway.

The check fails open: a lookup that fails or names no version, or a staged
file that isn't what a get would have staged, and the get goes on exactly as
it always did. These run against the fake ipatool and UnfairPlay, and the
iTunes lookup is always answered here, never the network.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from graft_ipa import appstore, itunes
from graft_ipa import cli as cli_mod
from graft_ipa.config import load_config
from graft_ipa.errors import ITunesError
from graft_ipa.ipa_inspect import mark_stripped
from tests.appstore_fakes import FakeTools, itunes_offline, make_ipa

#: The version the fakes' decrypted IPA carries, and so what a get stages.
VERSION = "21.46.3"
CANONICAL = f"com.example.app_{VERSION}.ipa"


class Store:
    """The iTunes lookup, answering `version` (or raising `error`), and
    counting how often it was asked."""

    def __init__(self) -> None:
        self.version: object = VERSION
        self.error: ITunesError | None = None
        self.asked = 0

    def __call__(self, bundle_id: str, *, country: str | None = None) -> itunes.ITunesApp:
        self.asked += 1
        if self.error is not None:
            raise self.error
        return itunes.ITunesApp(
            track_id=100000001, bundle_id=bundle_id, name="Example App",
            version=self.version,  # type: ignore[arg-type]
        )


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Store:
    answer = Store()
    monkeypatch.setattr(itunes, "lookup_by_bundle_id", answer)
    return answer


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
def cache_dir(config: Path) -> Path:
    path = load_config(config).cache_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def _set_app(config: Path, **fields: str) -> None:
    raw = yaml.safe_load(config.read_text())
    raw["apps"]["example"].update(fields)
    config.write_text(yaml.safe_dump(raw, sort_keys=False))


def _run(args: list[str]):
    return CliRunner().invoke(cli_mod.app, args)


def _events(result) -> list[dict]:
    return [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]


def _downloads(fakes: FakeTools) -> list[dict]:
    return [c for c in fakes.calls("ipatool") if c["argv"][:1] == ["download"]]


def _staged_as_a_get_stages_it(cache_dir: Path, *, version: str = VERSION) -> Path:
    """A decrypted IPA at its canonical name, as `provide_ipa.stage` leaves one."""
    return make_ipa(
        cache_dir / f"com.example.app_{version}.ipa", version=version, encrypted=False,
        marker="staged before",
    )


def _with_account_data(ipa: Path) -> Path:
    with zipfile.ZipFile(ipa, "a") as zf:
        zf.writestr("iTunesMetadata.plist", b"<plist/>")
    return ipa


# --- The version is already staged ----------------------------------------------


def test_a_get_of_the_version_already_staged_downloads_nothing_and_says_so_as_data(
    config: Path, cache_dir: Path, fakes: FakeTools, store: Store
) -> None:
    staged = _staged_as_a_get_stages_it(cache_dir)
    before = staged.read_bytes()

    result = _run(["--json", "--appstore-get", "--app", "example"])

    assert result.exit_code == 0, result.output
    assert _events(result) == [{
        "type": "appstore-already-staged", "app": "example", "bundle_id": "com.example.app",
        "version": VERSION, "filename": CANONICAL, "path": str(staged),
    }]
    assert _downloads(fakes) == [], "ipatool never downloaded"
    assert fakes.calls("unfairplay") == [], "and nothing was decrypted"
    assert staged.read_bytes() == before, "the staged copy is left as it was"
    assert store.asked == 1


def test_in_terminal_it_says_already_have_the_version_and_how_to_get_it_again(
    config: Path, cache_dir: Path, fakes: FakeTools, store: Store
) -> None:
    _staged_as_a_get_stages_it(cache_dir)

    result = _run(["--appstore-get", "--app", "example"])

    assert result.exit_code == 0, result.output
    words = " ".join(result.output.split())
    assert f"already have {VERSION}" in words
    assert "nothing downloaded" in words
    assert "graft --appstore-get --app example --again" in words
    assert '{"type"' not in result.stdout
    assert _downloads(fakes) == []


def test_a_version_pin_does_not_hide_the_store_s_version_from_the_check(
    config: Path, cache_dir: Path, fakes: FakeTools, store: Store
) -> None:
    """A get always fetches the store's current version, whatever the pin, so
    the question is whether *that* version is staged, not the pinned one. And
    having it, the get lets go of the pin (`appstore.release_hold`): Get
    latest is a request for the latest."""
    _staged_as_a_get_stages_it(cache_dir, version="20.0.0")
    _staged_as_a_get_stages_it(cache_dir)
    _set_app(config, preferred_version="20.0.0")

    result = _run(["--json", "--appstore-get", "--app", "example"])

    assert result.exit_code == 0, result.output
    assert [e["type"] for e in _events(result)] == ["appstore-already-staged", "appstore-hold"]
    assert _downloads(fakes) == []


# --- --again ----------------------------------------------------------------------


def test_again_downloads_decrypts_and_stages_it_anyway(
    config: Path, cache_dir: Path, fakes: FakeTools, store: Store
) -> None:
    staged = _staged_as_a_get_stages_it(cache_dir)

    result = _run(["--json", "--appstore-get", "--app", "example", "--again"])

    assert result.exit_code == 0, result.output
    assert [e["type"] for e in _events(result)] == [
        "appstore-stage", "appstore-stage", "appstore-decrypted", "appstore-stage", "ipa-provided",
    ]
    assert len(_downloads(fakes)) == 1
    assert b"staged before" not in staged.read_bytes(), "the new download replaced it"
    assert store.asked == 0, "--again doesn't ask the App Store which version it has"


def test_again_keeps_use_it_anyway_s_retry_working(
    config: Path, cache_dir: Path, fakes: FakeTools, store: Store
) -> None:
    """The app re-runs a Get Again refused as unverified with both flags."""
    _staged_as_a_get_stages_it(cache_dir)
    fakes.set(plaintext_verified=False)

    refused = _run(["--json", "--appstore-get", "--app", "example", "--again"])
    accepted = _run(["--json", "--appstore-get", "--app", "example", "--again", "--allow-unverified"])

    assert refused.exit_code == 20
    assert {"type": "appstore-refused", "app": "example", "reason": "unverified"} in _events(refused)
    assert "graft --appstore-get --app example --again --allow-unverified" in " ".join(refused.stderr.split()), (
        "the Terminal command keeps --again: without it, it would find the old copy and stop"
    )
    assert accepted.exit_code == 0, accepted.output
    assert _events(accepted)[-1]["type"] == "ipa-provided"
    assert len(_downloads(fakes)) == 2


def test_again_belongs_to_appstore_get_only(config: Path) -> None:
    result = _run(["--appstore-check", "--again"])

    assert result.exit_code == 2
    assert "--again is not valid with --appstore-check" in result.output


# --- Downloads as before ----------------------------------------------------------


def _assert_downloaded(result, fakes: FakeTools) -> None:
    assert result.exit_code == 0, result.output
    types = [e["type"] for e in _events(result)]
    assert "appstore-already-staged" not in types
    assert types[0] == "appstore-stage" and types[-1] == "ipa-provided", types
    assert len(_downloads(fakes)) == 1


def test_a_newer_store_version_is_downloaded(
    config: Path, cache_dir: Path, fakes: FakeTools, store: Store
) -> None:
    _staged_as_a_get_stages_it(cache_dir, version="21.40.0")

    _assert_downloaded(_run(["--json", "--appstore-get", "--app", "example"]), fakes)


def test_nothing_staged_is_downloaded_without_asking_the_store(
    config: Path, cache_dir: Path, fakes: FakeTools, store: Store
) -> None:
    _assert_downloaded(_run(["--json", "--appstore-get", "--app", "example"]), fakes)
    assert store.asked == 0, "with no staged copy there is nothing to compare"


@pytest.mark.parametrize("answer", ["offline", "no version", "empty version", "not text"])
def test_a_lookup_that_can_t_name_a_version_never_stops_the_get(
    config: Path, cache_dir: Path, fakes: FakeTools, store: Store, answer: str
) -> None:
    """Fail open: "couldn't tell" downloads, as every get did before."""
    _staged_as_a_get_stages_it(cache_dir)
    if answer == "offline":
        store.error = ITunesError("iTunes lookup request failed: no network in tests")
    else:
        store.version = {"no version": None, "empty version": "", "not text": 21}[answer]

    _assert_downloaded(_run(["--json", "--appstore-get", "--app", "example"]), fakes)


def test_the_suite_s_own_offline_lookup_downloads_too(
    config: Path, cache_dir: Path, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(itunes, "lookup_by_bundle_id", itunes_offline)
    _staged_as_a_get_stages_it(cache_dir)

    _assert_downloaded(_run(["--json", "--appstore-get", "--app", "example"]), fakes)


@pytest.mark.parametrize(
    "shape", ["encrypted", "account data", "another version inside", "another app inside",
              "a partial dump", "not a zip"],
)
def test_a_staged_file_that_isn_t_what_a_get_stages_is_downloaded_over(
    config: Path, cache_dir: Path, fakes: FakeTools, store: Store, shape: str
) -> None:
    path = cache_dir / CANONICAL
    if shape == "encrypted":
        make_ipa(path, encrypted=True)
    elif shape == "account data":
        _with_account_data(make_ipa(path, encrypted=False))
    elif shape == "another version inside":
        make_ipa(path, version="21.40.0", encrypted=False)
    elif shape == "another app inside":
        make_ipa(path, bundle_id="com.example.other", encrypted=False)
    elif shape == "a partial dump":
        make_ipa(path, encrypted=False)
        mark_stripped(path, ["Payload/App.app/PlugIns/Widget.appex"])
    else:
        path.write_bytes(b"not a zip")

    _assert_downloaded(_run(["--json", "--appstore-get", "--app", "example"]), fakes)


def test_a_hand_copied_file_of_that_version_is_not_one_graft_staged(
    config: Path, cache_dir: Path, fakes: FakeTools, store: Store
) -> None:
    """A get stages `<bundle id>_<version>.ipa`; a copy under any other name
    is downloaded over, and the build then prefers the canonical file."""
    make_ipa(cache_dir / f"App_{VERSION}.ipa", encrypted=False)

    _assert_downloaded(_run(["--json", "--appstore-get", "--app", "example"]), fakes)


def test_apple_s_own_app_is_still_refused_before_anything_is_compared(
    config: Path, cache_dir: Path, fakes: FakeTools, store: Store
) -> None:
    _set_app(config, bundle_id="com.apple.example")
    make_ipa(cache_dir / f"com.apple.example_{VERSION}.ipa", bundle_id="com.apple.example",
             encrypted=False)

    result = _run(["--json", "--appstore-get", "--app", "example"])

    assert result.exit_code == 20, result.output
    assert "one of Apple's own apps" in " ".join(result.output.split())
    assert store.asked == 0


# --- The module's own answer --------------------------------------------------------


def test_already_staged_names_the_canonical_file(config: Path, cache_dir: Path, store: Store) -> None:
    cfg = load_config(config)
    staged = _staged_as_a_get_stages_it(cache_dir)

    found = appstore.already_staged(cfg, cfg.get_app("example"))

    assert found is not None
    assert (found.path, found.bundle_id, found.version) == (staged, "com.example.app", VERSION)
