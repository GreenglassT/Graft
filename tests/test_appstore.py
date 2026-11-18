"""`appstore.py`: status, version check, and the download → decrypt → stage path.

Everything here runs against the fake ipatool and UnfairPlay in
`appstore_fakes` — no network, no Apple ID, no enrollment model. The checks the
spec singles out as the point of the design get the most attention: the
encrypted download (which carries the account's license) must be gone after
*every* outcome, and an unverified decrypt must be refused unless asked for.
"""

from __future__ import annotations

import json
import os
import signal
import time
import zipfile
from io import StringIO
from pathlib import Path

import pytest
import yaml

from graft_ipa import appstore, itunes
from graft_ipa.config import Config, load_config
from graft_ipa.errors import AppStoreError, ConfigError, ITunesError
from graft_ipa.json_output import JsonEmitter
from tests.appstore_fakes import FAKE_EMAIL, FAKE_NAME, FakeTools, make_ipa


@pytest.fixture
def fakes(tmp_path: Path) -> FakeTools:
    return FakeTools(tmp_path / "tools")


@pytest.fixture
def cfg(temp_config_env: Path, fakes: FakeTools) -> Config:
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["appstore"] = fakes.appstore_section()
    temp_config_env.write_text(yaml.safe_dump(raw, sort_keys=False))
    return load_config(temp_config_env)


def _events(buf: StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def _tmp_root(cfg: Config) -> Path:
    return cfg.cache_dir / appstore.TMP_DIRNAME


def _leftovers(cfg: Config) -> list[str]:
    root = _tmp_root(cfg)
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_reports_both_tools_ready(cfg: Config, fakes: FakeTools) -> None:
    assert appstore.status(cfg.appstore) == {
        "ipatool": {
            "path": str(fakes.ipatool), "present": True, "version": "2.8.0",
            "signed_in": True, "warning": None,
        },
        "unfairplay": {
            "path": str(fakes.unfairplay), "present": True,
            "version": "dev-d7503082a59e", "enrolled": True,
            "model_dir": str(fakes.models),
        },
        "setup_commands": [],
    }


def test_status_never_carries_the_account(cfg: Config) -> None:
    """The spec: `signed_in: true/false` only. The fake's `auth info` prints an
    email and a name just as the real one does."""
    buf = StringIO()
    appstore.report_status(cfg, emitter=JsonEmitter(stream=buf), say=print)

    line = buf.getvalue()
    assert FAKE_EMAIL not in line
    assert FAKE_NAME not in line
    assert '"type":"appstore-status"' in line


def test_signed_out_names_the_login_command_and_nothing_else(
    cfg: Config, fakes: FakeTools
) -> None:
    fakes.set(signed_in=False)

    payload = appstore.status(cfg.appstore)

    assert payload["ipatool"]["signed_in"] is False
    assert payload["setup_commands"] == [
        f"{fakes.ipatool} auth login --email YOUR_APPLE_ID_EMAIL"
    ]


def test_not_enrolled_names_the_setup_command(cfg: Config, fakes: FakeTools) -> None:
    (fakes.models / "enrollment.bin").unlink()

    payload = appstore.status(cfg.appstore)

    assert payload["unfairplay"]["enrolled"] is False
    assert f"sudo {fakes.unfairplay} setup --app /Applications/YOUR_IPAD_APP.app" in (
        payload["setup_commands"]
    )
    assert not any("auth login" in c for c in payload["setup_commands"])


def test_2_8_1_is_warned_about(cfg: Config, fakes: FakeTools) -> None:
    fakes.set(ipatool_version="2.8.1")

    payload = appstore.status(cfg.appstore)

    assert "#672" in payload["ipatool"]["warning"]
    assert any("2.8.0" in c for c in payload["setup_commands"])


def test_2_8_0_is_not_warned_about(cfg: Config) -> None:
    assert appstore.status(cfg.appstore)["ipatool"]["warning"] is None


def test_nothing_installed(tmp_path: Path) -> None:
    from graft_ipa.config import AppStoreToolsConfig

    tools = AppStoreToolsConfig(
        ipatool=tmp_path / "no-ipatool", unfairplay=tmp_path / "no-up",
        unfairplay_models=tmp_path / "no-models",
    )

    payload = appstore.status(tools)

    assert payload["ipatool"] == {
        "path": str(tmp_path / "no-ipatool"), "present": False, "version": None,
        "signed_in": False, "warning": None,
    }
    assert payload["unfairplay"]["present"] is False
    # Not "sign in": there is no ipatool to sign in with yet.
    assert not any("auth login" in c for c in payload["setup_commands"])
    assert any("ipatool 2.8.0" in c for c in payload["setup_commands"])
    assert any("UnfairPlay" in c for c in payload["setup_commands"])


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def _store_says(monkeypatch: pytest.MonkeyPatch, version: str | Exception) -> None:
    def lookup(bundle_id: str) -> itunes.ITunesApp:
        if isinstance(version, Exception):
            raise version
        return itunes.ITunesApp(track_id=1, bundle_id=bundle_id, name="x", version=version)

    monkeypatch.setattr(itunes, "lookup_by_bundle_id", lookup)


def _stage_cached(cfg: Config, version: str) -> None:
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    make_ipa(cfg.cache_dir / f"com.example.app_{version}.ipa", version=version)


def _check(cfg: Config) -> dict:
    buf = StringIO()
    appstore.check(cfg, ["example"], emitter=JsonEmitter(stream=buf), say=print)
    (event,) = _events(buf)
    return event


def test_a_newer_store_version_is_an_update(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    _store_says(monkeypatch, "21.46.3")
    _stage_cached(cfg, "21.9.1")

    assert _check(cfg) == {
        "type": "appstore-version", "app": "example",
        "bundle_id": "com.example.app", "store_version": "21.46.3",
        "cached_version": "21.9.1", "update_available": True, "error": None,
    }


def test_the_same_version_is_not_an_update(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    _store_says(monkeypatch, "21.46.3")
    _stage_cached(cfg, "21.46.3")

    assert _check(cfg)["update_available"] is False


def test_a_newer_cache_than_the_store_is_not_an_update(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Numeric, not string, comparison: "21.9.1" sorts after "21.46.3" as text."""
    _store_says(monkeypatch, "21.9.1")
    _stage_cached(cfg, "21.46.3")

    assert _check(cfg)["update_available"] is False


def test_nothing_cached_is_an_update(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    _store_says(monkeypatch, "21.46.3")

    event = _check(cfg)

    assert event["cached_version"] is None
    assert event["update_available"] is True


def test_a_failed_lookup_is_an_event_not_an_exception(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store_says(monkeypatch, ITunesError("iTunes lookup request failed: offline"))
    _stage_cached(cfg, "21.46.3")

    event = _check(cfg)

    assert event["store_version"] is None
    assert event["update_available"] is False
    assert event["error"] == "iTunes lookup request failed: offline"
    assert event["cached_version"] == "21.46.3"


def test_an_unknown_app_is_a_config_error(cfg: Config) -> None:
    with pytest.raises(ConfigError):
        appstore.check(cfg, ["nope"], emitter=None, say=print)


# ---------------------------------------------------------------------------
# fetched: download → decrypt → stage
# ---------------------------------------------------------------------------


def _fetch(cfg: Config, *, allow_unverified: bool = False) -> tuple[list[dict], bytes, list[str]]:
    buf = StringIO()
    warnings: list[str] = []
    with appstore.fetched(
        cfg, "example", emitter=JsonEmitter(stream=buf),
        allow_unverified=allow_unverified, warn=warnings.append,
    ) as ipa:
        body = ipa.read_bytes()
        assert ipa.is_relative_to(_tmp_root(cfg))
    return _events(buf), body, warnings


def test_the_happy_path_yields_the_decrypted_ipa_and_says_each_stage(
    cfg: Config, fakes: FakeTools
) -> None:
    events, body, warnings = _fetch(cfg)

    assert body == Path(fakes.scenario["decrypted_ipa"]).read_bytes()
    assert [e for e in events if e["type"] == "appstore-stage"] == [
        {"type": "appstore-stage", "app": "example", "stage": "download"},
        {"type": "appstore-stage", "app": "example", "stage": "decrypt"},
        {"type": "appstore-stage", "app": "example", "stage": "stage"},
    ]
    (decrypted,) = [e for e in events if e["type"] == "appstore-decrypted"]
    assert decrypted == {
        "type": "appstore-decrypted", "app": "example", "encrypted_images": 8,
        "decrypted_pages": 51088, "plaintext_verified": True,
    }
    assert warnings == []


def test_the_working_directory_is_private(cfg: Config, fakes: FakeTools) -> None:
    _fetch(cfg)

    (download,) = [c for c in fakes.calls("ipatool") if c["argv"][0] == "download"]
    assert download["out_dir_mode"] == 0o700
    assert (_tmp_root(cfg).stat().st_mode & 0o777) == 0o700


def test_nothing_is_left_after_success(cfg: Config) -> None:
    _fetch(cfg)

    assert _leftovers(cfg) == []


@pytest.mark.parametrize(
    "failure",
    [
        {"download_error": "license is required"},
        {"decrypt_error": "no enrollment model matches this IPA"},
        {"plaintext_verified": False},
    ],
    ids=["download", "decrypt", "unverified"],
)
def test_nothing_is_left_after_a_failure(cfg: Config, fakes: FakeTools, failure: dict) -> None:
    fakes.set(write_default_report=True, **failure)

    with pytest.raises(AppStoreError):
        _fetch(cfg)

    assert _leftovers(cfg) == []
    assert not list(cfg.cache_dir.glob("*.ipa")), "nothing may be staged on failure"


def test_nothing_is_left_when_the_caller_fails_while_staging(cfg: Config) -> None:
    """The `yield` is inside the cleanup: a staging error is the caller's, but
    the license-bearing download is still ours to remove."""
    with pytest.raises(RuntimeError), appstore.fetched(cfg, "example", emitter=None):
        raise RuntimeError("disk full")

    assert _leftovers(cfg) == []


def test_stop_cleans_up_too(cfg: Config) -> None:
    """The GUI's Stop is a SIGTERM. Without the handler Python dies in place
    and the encrypted IPA stays in the cache directory for good."""
    with pytest.raises(AppStoreError) as e, appstore.fetched(cfg, "example", emitter=None):
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(1)  # the handler raises out of here; this never finishes

    assert e.value.message.startswith("stopped before finishing")
    assert _leftovers(cfg) == []


def test_the_handler_is_put_back(cfg: Config) -> None:
    before = signal.getsignal(signal.SIGTERM)

    _fetch(cfg)

    assert signal.getsignal(signal.SIGTERM) is before


def test_a_killed_runs_leftovers_are_swept_and_a_live_ones_are_not(cfg: Config) -> None:
    root = _tmp_root(cfg)
    root.mkdir(parents=True)
    old, fresh = root / "get-old", root / "get-fresh"
    old.mkdir()
    fresh.mkdir()
    a_day_and_more = time.time() - appstore.STALE_AFTER_SECONDS - 60
    os.utime(old, (a_day_and_more, a_day_and_more))

    _fetch(cfg)

    assert _leftovers(cfg) == ["get-fresh"]


def test_unverified_is_refused_by_default(cfg: Config, fakes: FakeTools) -> None:
    fakes.set(plaintext_verified=False)

    with pytest.raises(AppStoreError) as e:
        _fetch(cfg)

    assert "PlaintextVerified: false" in e.value.message
    assert "--appstore-get --app example --allow-unverified" in e.value.hint


def test_unverified_is_accepted_when_asked_with_a_warning(cfg: Config, fakes: FakeTools) -> None:
    fakes.set(plaintext_verified=False)

    events, _, warnings = _fetch(cfg, allow_unverified=True)

    (decrypted,) = [e for e in events if e["type"] == "appstore-decrypted"]
    assert decrypted["plaintext_verified"] is False
    assert len(warnings) == 1 and "could not verify" in warnings[0]


def test_not_enrolled_is_refused_before_anything_is_downloaded(
    cfg: Config, fakes: FakeTools
) -> None:
    (fakes.models / "enrollment.bin").unlink()

    with pytest.raises(AppStoreError) as e:
        _fetch(cfg)

    assert e.value.message.startswith("this Mac isn't enrolled for decryption")
    assert f"sudo {fakes.unfairplay} setup" in e.value.hint
    assert not [c for c in fakes.calls("ipatool") if c["argv"][0] == "download"]


def test_signed_out_is_refused_before_anything_is_downloaded(
    cfg: Config, fakes: FakeTools
) -> None:
    fakes.set(signed_in=False)

    with pytest.raises(AppStoreError) as e:
        _fetch(cfg)

    assert e.value.message == "ipatool is not signed in to the App Store"
    assert "auth login" in e.value.hint
    assert not [c for c in fakes.calls("ipatool") if c["argv"][0] == "download"]


def test_not_owned_carries_the_tap_get_hint(cfg: Config, fakes: FakeTools) -> None:
    fakes.set(download_error="license is required")

    with pytest.raises(AppStoreError) as e:
        _fetch(cfg)

    assert "tap Get" in e.value.hint


def test_2_8_1_warns_and_still_tries(cfg: Config, fakes: FakeTools) -> None:
    fakes.set(ipatool_version="2.8.1")

    _, _, warnings = _fetch(cfg)

    assert any("2.8.1" in w and "#672" in w for w in warnings)


# ---------------------------------------------------------------------------
# decrypted_if_encrypted: the --provide-ipa half
# ---------------------------------------------------------------------------


def test_the_cryptid_of_the_main_binary_is_what_decides(tmp_path: Path) -> None:
    assert appstore.main_binary_encrypted(make_ipa(tmp_path / "e.ipa", encrypted=True)) is True
    assert appstore.main_binary_encrypted(make_ipa(tmp_path / "d.ipa", encrypted=False)) is False


def test_a_non_macho_or_missing_executable_is_not_called_encrypted(tmp_path: Path) -> None:
    assert appstore.main_binary_encrypted(make_ipa(tmp_path / "junk.ipa", encrypted=None)) is False
    bare = tmp_path / "bare.ipa"
    with zipfile.ZipFile(bare, "w") as zf:
        zf.writestr("Payload/A.app/Info.plist", b"""<?xml version="1.0"?>
<plist version="1.0"><dict><key>CFBundleIdentifier</key><string>com.x</string>
<key>CFBundleExecutable</key><string>Gone</string></dict></plist>""")
    assert appstore.main_binary_encrypted(bare) is False


def test_an_encrypted_drop_is_decrypted(cfg: Config, fakes: FakeTools, tmp_path: Path) -> None:
    src = make_ipa(tmp_path / "drop" / "Example.ipa", encrypted=True)
    buf = StringIO()

    with appstore.decrypted_if_encrypted(
        cfg, "example", src, emitter=JsonEmitter(stream=buf)
    ) as usable:
        assert usable != src
        assert appstore.main_binary_encrypted(usable) is False

    (call,) = fakes.calls("unfairplay")
    assert call["argv"][call["argv"].index("--input") + 1] == str(src)
    assert [e["stage"] for e in _events(buf) if e["type"] == "appstore-stage"] == [
        "decrypt", "stage",
    ]
    assert _leftovers(cfg) == []
    assert sorted(p.name for p in src.parent.iterdir()) == ["Example.ipa"]


def test_an_unencrypted_drop_is_untouched(cfg: Config, fakes: FakeTools, tmp_path: Path) -> None:
    src = make_ipa(tmp_path / "drop" / "Example.ipa", encrypted=False)
    buf = StringIO()

    with appstore.decrypted_if_encrypted(
        cfg, "example", src, emitter=JsonEmitter(stream=buf)
    ) as usable:
        assert usable == src

    assert fakes.calls("unfairplay") == []
    assert buf.getvalue() == ""
    assert not _tmp_root(cfg).exists()


def test_an_encrypted_drop_without_enrollment_is_refused(
    cfg: Config, fakes: FakeTools, tmp_path: Path
) -> None:
    (fakes.models / "enrollment.bin").unlink()
    src = make_ipa(tmp_path / "drop" / "Example.ipa", encrypted=True)

    with pytest.raises(AppStoreError) as e, appstore.decrypted_if_encrypted(
        cfg, "example", src, emitter=None
    ):
        pytest.fail("must not yield an encrypted IPA to be staged")

    assert f"sudo {fakes.unfairplay} setup" in e.value.hint
    assert fakes.calls("unfairplay") == []


def test_an_unverified_drop_names_the_provide_command(
    cfg: Config, fakes: FakeTools, tmp_path: Path
) -> None:
    fakes.set(plaintext_verified=False)
    src = make_ipa(tmp_path / "drop" / "Example.ipa", encrypted=True)

    with pytest.raises(AppStoreError) as e, appstore.decrypted_if_encrypted(
        cfg, "example", src, emitter=None
    ):
        pytest.fail("must not yield an unverified decrypt")

    assert f"--provide-ipa {src} --app example --allow-unverified" in e.value.hint
