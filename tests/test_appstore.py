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
import zlib
from io import StringIO
from pathlib import Path

import pytest
import yaml

from graft_ipa import appstore, ipatool, itunes, unfairplay
from graft_ipa.config import Config, load_config
from graft_ipa.errors import AppStoreError, ConfigError, ITunesError
from graft_ipa.json_output import JsonEmitter
from tests.appstore_fakes import FAKE_EMAIL, FAKE_NAME, FakeTools, itunes_offline, make_ipa


@pytest.fixture(autouse=True)
def _no_price_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A license refusal looks the price up; never on the network here."""
    monkeypatch.setattr(itunes, "lookup_by_bundle_id", itunes_offline)


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


def test_an_auth_info_with_no_verdict_is_couldn_t_tell_and_says_so(cfg: Config, fakes: FakeTools) -> None:
    fakes.set(auth_raw="")
    ipa = appstore.status(cfg.appstore)["ipatool"]
    assert (ipa["signed_in"], ipa["unknown_reason"]) == (None, ipatool.UNKNOWN_NO_VERDICT)
    assert ipa["unknown_message"] == ipatool.UNKNOWN_MESSAGES[ipatool.UNKNOWN_NO_VERDICT]


def test_status_reports_both_tools_ready(cfg: Config, fakes: FakeTools) -> None:
    assert appstore.status(cfg.appstore) == {
        "ipatool": {
            "path": str(fakes.ipatool), "source": "config", "present": True, "version": "2.8.0",
            "signed_in": True, "email": FAKE_EMAIL, "legacy_signin": False,
            "unknown_reason": None, "unknown_message": None, "warning": None, "install": None,
            "note": None,
        },
        "unfairplay": {
            "path": str(fakes.unfairplay), "source": "config", "present": True,
            "version": "dev-d7503082a59e", "enrolled": True,
            "model_dir": str(fakes.models), "model_problem": None, "install": None, "note": None,
        },
        "setup_commands": [],
    }


def test_status_carries_the_email_and_never_the_name(cfg: Config) -> None:
    """Since 2.0 Settings shows who is signed in. The fake's `auth info` prints
    an email and a name just as the real one does."""
    buf = StringIO()
    appstore.report_status(cfg, emitter=JsonEmitter(stream=buf), say=print)

    line = buf.getvalue()
    assert FAKE_EMAIL in line
    assert FAKE_NAME not in line
    assert '"type":"appstore-status"' in line


def test_signed_out_names_the_login_command_and_nothing_else(
    cfg: Config, fakes: FakeTools
) -> None:
    fakes.set(signed_in=False)

    payload = appstore.status(cfg.appstore)

    assert payload["ipatool"]["signed_in"] is False
    assert payload["setup_commands"] == [ipatool.LOGIN_COMMAND]


def test_not_enrolled_names_graft_s_own_enrolment_never_unfairplay_as_root(cfg: Config, fakes: FakeTools) -> None:
    """The critic: status still printed `sudo '<unfairplay>' setup`, the
    pre-redesign root command, which runs the (bundled) UnfairPlay as root
    from wherever it is, unchecked, writing into the user's folders."""
    (fakes.models / "enrollment.bin").unlink()

    payload = appstore.status(cfg.appstore)

    assert payload["unfairplay"]["enrolled"] is False
    assert payload["setup_commands"][-2:] == [
        "# set up decryption once: install an iPhone or iPad app on this Mac from the Mac App "
        "Store and open it once (use the same Apple ID in Graft as in the Mac App Store); then, "
        "in Graft.app, Settings › App Store › Set Up Decryption…; in Terminal, run this with that "
        "app's name in the path, run the script it prints with `sudo /bin/sh`, then "
        "`graft --appstore-enroll-finish <the folder it prints>`:",
        'graft --appstore-enroll-command --app "/Applications/<an iPhone/iPad app from the Mac App Store, opened once>.app"',
    ]
    assert not any("sudo " + str(fakes.unfairplay) in c or " setup --app" in c for c in payload["setup_commands"])
    assert not any("--appstore-login" in c for c in payload["setup_commands"])


def _on_intel(monkeypatch: pytest.MonkeyPatch) -> None:
    from graft_ipa import host

    monkeypatch.setattr(host, "_sysctl_arm64", lambda: None)  # no such sysctl: Intel


def test_on_an_intel_mac_status_does_not_offer_an_enrollment_that_cannot_work(
    cfg: Config, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`unfairplay setup` enrolls with an iPhone/iPad app installed on this Mac
    from the App Store, and only Apple silicon Macs can install those. Handing
    an Intel user that `sudo` line sends them through a setup that cannot
    succeed; the status has to say why instead."""
    _on_intel(monkeypatch)
    (fakes.models / "enrollment.bin").unlink()

    commands = appstore.status(cfg.appstore)["setup_commands"]

    assert not any(" setup --app " in c for c in commands)
    assert any("Apple silicon" in c for c in commands)
    # Every line stays safe to paste into a shell.
    assert all(c.startswith("#") or "--appstore-login" in c for c in commands)


def test_on_an_intel_mac_an_unenrolled_get_says_why(
    cfg: Config, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on_intel(monkeypatch)
    (fakes.models / "enrollment.bin").unlink()

    with pytest.raises(AppStoreError) as e:
        _fetch(cfg)

    assert "Apple silicon" in e.value.message
    assert "setup" not in (e.value.hint or "")
    assert not [c for c in fakes.calls("ipatool") if c["argv"][0] == "download"]


def _intel_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Intel build of Graft.app on an Apple silicon Mac (conftest's): its
    GraftBundleArch is x86_64, and it carries no UnfairPlay."""
    from graft_ipa import host

    monkeypatch.setattr(host, "_running_flavor", lambda: "x86_64")


def test_the_intel_build_offers_no_decryption_setup_and_says_which_download(
    cfg: Config, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not UnfairPlay's build-from-source steps, nor an enrollment: the fix
    is Graft.zip, on this Apple silicon Mac."""
    _intel_build(monkeypatch)
    fakes.unfairplay.unlink()
    (fakes.models / "enrollment.bin").unlink()

    payload = appstore.status(cfg.appstore)
    commands = payload["setup_commands"]

    assert ("# App Store decryption can't be set up here: this is the Intel build of Graft; "
            "on an Apple silicon Mac, download Graft.zip.") in commands
    assert not any("go -C unfairplay build" in c or "git clone" in c for c in commands)
    assert not any("--appstore-enroll-command" in c or "Set Up Decryption" in c for c in commands)
    assert payload["unfairplay"]["install"] is None
    assert all(c.startswith("#") or "--appstore-login" in c for c in commands)


def test_the_intel_build_says_why_it_cant_get_from_the_app_store(
    cfg: Config, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A get stops before ipatool runs (`store_requests_blocker`), and says
    which download, not that UnfairPlay is missing."""
    _intel_build(monkeypatch)
    fakes.unfairplay.unlink()

    with pytest.raises(AppStoreError) as e:
        _fetch(cfg)

    assert e.value.message == ("App Store sign-in and downloads need a Mac with Apple silicon and "
                               "Graft.zip: this is the Intel build of Graft; on an Apple silicon Mac, "
                               "download Graft.zip")
    assert "UnfairPlay not found" not in e.value.message
    assert fakes.calls("ipatool") == []


def test_the_intel_build_says_why_it_cant_decrypt(
    cfg: Config, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where only decryption is asked (an encrypted drop), its own refusal."""
    _intel_build(monkeypatch)
    fakes.unfairplay.unlink()

    with pytest.raises(AppStoreError) as e:
        appstore._require_unfairplay(cfg.appstore)

    assert e.value.message == ("decrypting App Store IPAs can't be done here: this is the Intel "
                               "build of Graft; on an Apple silicon Mac, download Graft.zip")
    assert e.value.hint == "Use an IPA that is already decrypted, or dump one from a jailbroken device (--decrypt)"


def test_the_intel_build_refuses_an_encrypted_drop_saying_why(
    cfg: Config, fakes: FakeTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropping an encrypted IPA, or Add App › Manual with one, reaches the
    same refusal (`decrypted_if_encrypted`)."""
    _intel_build(monkeypatch)
    fakes.unfairplay.unlink()
    src = make_ipa(tmp_path / "drop" / "Example.ipa", encrypted=True)

    with pytest.raises(AppStoreError) as e, appstore.decrypted_if_encrypted(
        cfg, "example", src, emitter=None
    ):
        pytest.fail("must not yield an encrypted IPA to be staged")

    assert "this is the Intel build of Graft; on an Apple silicon Mac, download Graft.zip" in e.value.message
    assert fakes.calls("unfairplay") == []


def test_the_apple_silicon_build_still_says_unfairplay_is_missing(
    cfg: Config, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the same missing UnfairPlay in the Apple silicon build is
    a damaged copy, said as before."""
    from graft_ipa import host

    monkeypatch.setattr(host, "_running_flavor", lambda: "arm64")
    fakes.unfairplay.unlink()

    with pytest.raises(AppStoreError) as e:
        _fetch(cfg)

    assert e.value.message.startswith("UnfairPlay not found at ")


def test_2_8_1_is_warned_about(cfg: Config, fakes: FakeTools) -> None:
    fakes.set(ipatool_version="2.8.1")

    payload = appstore.status(cfg.appstore)

    assert "#672" in payload["ipatool"]["warning"]
    assert any("2.8.0" in c for c in payload["setup_commands"])


def test_2_8_0_is_not_warned_about(cfg: Config) -> None:
    assert appstore.status(cfg.appstore)["ipatool"]["warning"] is None


def test_nothing_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from graft_ipa.config import AppStoreToolsConfig

    # Discovery also looks in ~/.local/bin and on PATH: keep this machine's out of it.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    tools = AppStoreToolsConfig(
        ipatool=tmp_path / "no-ipatool", unfairplay=tmp_path / "no-up",
        unfairplay_models=tmp_path / "no-models",
    )

    payload = appstore.status(tools)

    assert payload["ipatool"] == {
        "path": str(tmp_path / "no-ipatool"), "source": "missing", "present": False,
        "version": None,
        "signed_in": False, "email": None, "legacy_signin": False, "unknown_reason": None,
        "unknown_message": None, "warning": None, "install": ipatool.install_command(tmp_path / "no-ipatool"),
        "note": None,
    }
    assert payload["unfairplay"]["present"] is False
    # Not "sign in": there is no ipatool to sign in with yet.
    assert not any("--appstore-login" in c for c in payload["setup_commands"])
    assert any("ipatool 2.8.0" in c for c in payload["setup_commands"])
    assert any("UnfairPlay" in c for c in payload["setup_commands"])


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def _store_says(monkeypatch: pytest.MonkeyPatch, version: str | Exception) -> None:
    def lookup(bundle_id: str, country: str | None = None) -> itunes.ITunesApp:
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
        "cached_version": "21.9.1", "update_available": True, "held_version": None, "error": None,
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


def test_an_unverified_refusal_says_so_as_data(cfg: Config, fakes: FakeTools) -> None:
    """C-UNVERIFIED: the app offers "Use it anyway…" off this event, so it
    must not have to recognize the refusal by its sentence or its hint."""
    fakes.set(plaintext_verified=False)
    buf = StringIO()

    with pytest.raises(AppStoreError), appstore.fetched(
        cfg, "example", emitter=JsonEmitter(stream=buf)
    ):
        pytest.fail("must not yield an unverified decrypt")

    events = _events(buf)
    assert [e for e in events if e["type"] == "appstore-refused"] == [
        {"type": "appstore-refused", "app": "example", "reason": appstore.REFUSED_UNVERIFIED},
    ]
    assert not any(e["type"] == "appstore-decrypted" for e in events)


def test_a_verified_decrypt_emits_no_refusal(cfg: Config) -> None:
    events, _, _ = _fetch(cfg)

    assert not any(e["type"] == "appstore-refused" for e in events)


def test_an_accepted_unverified_decrypt_emits_no_refusal(cfg: Config, fakes: FakeTools) -> None:
    fakes.set(plaintext_verified=False)

    events, _, _ = _fetch(cfg, allow_unverified=True)

    assert not any(e["type"] == "appstore-refused" for e in events)


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
    assert "in Graft.app, Settings › App Store › Set Up Decryption…" in e.value.hint
    assert "use the same Apple ID in Graft as in the Mac App Store" in e.value.hint
    assert "graft --appstore-enroll-command --app" in e.value.hint
    assert str(fakes.unfairplay) not in e.value.hint, "never UnfairPlay itself, as root"
    assert not [c for c in fakes.calls("ipatool") if c["argv"][0] == "download"]


def test_signed_out_is_refused_before_anything_is_downloaded(
    cfg: Config, fakes: FakeTools
) -> None:
    fakes.set(signed_in=False)

    with pytest.raises(AppStoreError) as e:
        _fetch(cfg)

    assert e.value.message == "ipatool is not signed in to the App Store"
    assert ipatool.LOGIN_COMMAND in e.value.hint
    assert not [c for c in fakes.calls("ipatool") if c["argv"][0] == "download"]


def test_not_owned_carries_the_tap_get_hint(cfg: Config, fakes: FakeTools) -> None:
    fakes.set(download_error="license is required")

    with pytest.raises(AppStoreError) as e:
        _fetch(cfg)

    assert "tap Get" in e.value.hint


def test_2_8_1_is_refused_before_any_download(cfg: Config, fakes: FakeTools) -> None:
    fakes.set(ipatool_version="2.8.1")

    with pytest.raises(AppStoreError) as e:
        _fetch(cfg)

    assert "2.8.1" in e.value.message
    assert "#672" in e.value.hint
    assert not [c for c in fakes.calls("ipatool") if c["argv"][0] == "download"]


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

    assert "graft --appstore-enroll-command --app" in e.value.hint
    assert str(fakes.unfairplay) not in e.value.hint
    assert fakes.calls("unfairplay") == []


def test_an_unverified_drop_says_so_as_data(
    cfg: Config, fakes: FakeTools, tmp_path: Path
) -> None:
    fakes.set(plaintext_verified=False)
    src = make_ipa(tmp_path / "drop" / "Example.ipa", encrypted=True)
    buf = StringIO()

    with pytest.raises(AppStoreError), appstore.decrypted_if_encrypted(
        cfg, "example", src, emitter=JsonEmitter(stream=buf)
    ):
        pytest.fail("must not yield an unverified decrypt")

    assert [e for e in _events(buf) if e["type"] == "appstore-refused"] == [
        {"type": "appstore-refused", "app": "example", "reason": "unverified"},
    ]


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


# ---------------------------------------------------------------------------
# The account, since 2.0: a 1.x sign-in, a Keychain refusal, a model not yours
# ---------------------------------------------------------------------------


def test_a_1x_sign_in_is_named_and_ipatool_is_not_asked(
    temp_config_env: Path, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asking would put a keychain dialog in front of a status check (K1)."""
    from graft_ipa import keychain, tools
    from tests.keychain_fakes import FakeSecurity

    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: fakes.root)
    monkeypatch.setattr(keychain, "_run", FakeSecurity({("ipatool-auth.service", "account"): "old"}))
    payload = appstore.status(load_config(temp_config_env).appstore)

    assert payload["ipatool"]["legacy_signin"] is True
    assert payload["ipatool"]["signed_in"] is None
    assert not [c for c in fakes.calls("ipatool") if c["argv"][:2] == ["auth", "info"]]
    assert ipatool.LOGIN_COMMAND in payload["setup_commands"]


def test_a_1x_sign_in_refuses_a_get_without_asking_ipatool(
    temp_config_env: Path, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graft_ipa import keychain, tools
    from tests.keychain_fakes import FakeSecurity

    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: fakes.root)
    monkeypatch.setattr(keychain, "_run", FakeSecurity({("ipatool-auth.service", "account"): "old"}))
    tools_cfg = load_config(temp_config_env).appstore
    with pytest.raises(AppStoreError) as e:
        # The preflight a get and a claim share.
        appstore._require_signed_in(tools_cfg, appstore._ipatool_path(tools_cfg))
    assert "Graft 1.x" in e.value.message
    assert ipatool.LOGIN_COMMAND in e.value.hint
    assert not [c for c in fakes.calls("ipatool") if c["argv"][:2] == ["auth", "info"]]


def test_a_keychain_refusal_is_said_as_one_and_status_stops_asking(
    cfg: Config, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 10: never ipatool's "not found" after a denied dialog."""
    from graft_ipa import appstore_account, keychain
    from tests.keychain_fakes import FakeSecurity

    monkeypatch.setattr(keychain, "_run", FakeSecurity({("ipatool-auth.service", "account"): "x"}))
    fakes.set(auth_raw=json.dumps({"level": "error", "success": False, "error": (
        "failed to get account: failed to get item: The specified item could not be found in the keyring"
    )}))
    payload = appstore.status(cfg.appstore)
    assert payload["ipatool"]["legacy_signin"] is True
    assert payload["ipatool"]["signed_in"] is None
    assert ipatool.LOGIN_COMMAND in payload["setup_commands"]
    assert not appstore_account.marker_path().exists()
    with pytest.raises(AppStoreError) as e:
        _fetch(cfg)
    # Not Graft.app's own ipatool here (signin-B): the refusal names this one
    # and says its sign-in saves over the item, as status's line does.
    path = appstore._ipatool_path(cfg.appstore)
    assert e.value.message == (
        f"macOS's Keychain didn't let the ipatool at {path} read the App Store "
        "sign-in in your login keychain"
    )
    assert "Graft's ipatool" not in e.value.message + e.value.hint
    assert "saves over it" in e.value.hint


def test_a_keychain_refusal_of_graft_s_own_ipatool_keeps_its_words(
    cfg: Config, fakes: FakeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """signin-B's counterpart: Graft.app's own ipatool, refused an item its
    record vouches for (a denied dialog), is still "Graft's ipatool", and its
    sign-in replaces the item (`appstore_account.needs_clearing`)."""
    from graft_ipa import appstore_account, keychain, tools
    from tests.keychain_fakes import FakeSecurity

    monkeypatch.setattr(keychain, "_run", FakeSecurity({("ipatool-auth.service", "account"): "x"}))
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: fakes.root)
    appstore_account.mark_signed_in(tools.SOURCE_BUNDLED)
    fakes.set(auth_raw=json.dumps({"level": "error", "success": False, "error": (
        "failed to get account: failed to get item: The specified item could not be found in the keyring"
    )}))
    path = appstore._ipatool_path(cfg.appstore)
    assert ipatool.is_bundled(path)
    with pytest.raises(AppStoreError) as e:
        appstore._require_signed_in(cfg.appstore, path)
    assert e.value.message == ipatool.KEYCHAIN_REFUSED
    assert e.value.hint == ipatool.KEYCHAIN_REFUSED_HINT


def test_a_root_owned_model_is_a_problem_status_names(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As if the model belonged to another user (root, after an enrollment
    # whose chown never ran). Not os.getuid itself: everything else reads that.
    real = unfairplay.model_state
    monkeypatch.setattr(unfairplay, "model_state", lambda d: real(d, uid=os.getuid() + 1))
    payload = appstore.status(cfg.appstore)
    assert payload["unfairplay"]["enrolled"] is False
    assert "set up decryption again" in payload["unfairplay"]["model_problem"]
    assert any("set up decryption again" in c for c in payload["setup_commands"])
    with pytest.raises(AppStoreError) as e:
        _fetch(cfg)
    assert "can't be used" in e.value.message


def test_a_person_sees_the_email_and_a_model_problem(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = unfairplay.model_state
    monkeypatch.setattr(unfairplay, "model_state", lambda d: real(d, uid=os.getuid() + 1))
    lines: list[str] = []
    appstore.report_status(cfg, emitter=None, say=lines.append)
    text = "\n".join(lines)
    assert f"signed in as {FAKE_EMAIL}" in text
    assert "set up decryption again" in text


# --- get-5: UnfairPlay's word isn't the only check --------------------------------

import plistlib  # noqa: E402

from tests.appstore_fakes import macho  # noqa: E402


def _ipa_with(path: Path, *, main: int, extension: int | None = None) -> Path:
    """An IPA whose app executable has cryptid `main`, and, with
    `extension`, an app extension whose executable has that one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Payload/App.app/Info.plist", plistlib.dumps({
            "CFBundleIdentifier": "com.example.app", "CFBundleShortVersionString": "21.46.3",
            "CFBundleVersion": "21.46.3", "CFBundleExecutable": "App",
        }))
        zf.writestr("Payload/App.app/App", macho(main))
        if extension is not None:
            zf.writestr("Payload/App.app/PlugIns/Ext.appex/Info.plist", plistlib.dumps({
                "CFBundleIdentifier": "com.example.app.ext", "CFBundleExecutable": "Ext",
            }))
            zf.writestr("Payload/App.app/PlugIns/Ext.appex/Ext", macho(extension))
    return path


@pytest.mark.parametrize(("main", "extension", "named"), [
    (1, None, "Payload/App.app/App"),
    (0, 1, "Payload/App.app/PlugIns/Ext.appex/Ext"),
])
@pytest.mark.parametrize("allow_unverified", [False, True])
def test_a_decrypt_whose_output_is_still_encrypted_is_refused(
    cfg: Config, fakes: FakeTools, tmp_path: Path, main: int, extension: int | None, named: str,
    allow_unverified: bool,
) -> None:
    """get-5: staging trusted UnfairPlay's report alone, and a report saying
    PlaintextVerified with 0 pages passed. Every binary of the output is now
    read for its cryptid (`verify_decrypted`, stage 2.5's check), and one
    still encrypted is refused before anything is staged, even under
    --allow-unverified, which accepts only a decrypt Apple's hashes can't
    vouch for."""
    fakes.set(decrypted_ipa=str(_ipa_with(tmp_path / "fixtures" / "half.ipa", main=main, extension=extension)),
              decrypted_pages=0, plaintext_verified=not allow_unverified)
    buf = StringIO()
    with pytest.raises(AppStoreError) as e:
        with appstore.fetched(cfg, "example", emitter=JsonEmitter(stream=buf), allow_unverified=allow_unverified):
            pytest.fail("yielded an IPA that is still encrypted")
    assert "still encrypted" in e.value.message and named in e.value.message
    assert "refusing to stage it" in e.value.message
    assert [ev for ev in _events(buf) if ev["type"] == "appstore-decrypted"] == []
    assert _leftovers(cfg) == []


def test_a_decrypt_whose_output_can_t_be_read_is_refused(cfg: Config, fakes: FakeTools) -> None:
    """A declared executable that is no Mach-O at all can't be shown
    decrypted, and is never read as decrypted."""
    fakes.set(decrypted_ipa=str(make_ipa(Path(fakes.root) / "fixtures" / "not-macho.ipa", encrypted=None)))
    with pytest.raises(AppStoreError) as e:
        with appstore.fetched(cfg, "example", emitter=None):
            pytest.fail("yielded an IPA nobody could check")
    assert "couldn't check" in e.value.message and "refusing to stage it" in e.value.message


def test_an_encrypted_drop_whose_decrypt_is_still_encrypted_is_refused(
    cfg: Config, fakes: FakeTools, tmp_path: Path,
) -> None:
    """The `--provide-ipa` half shares `_decrypt`, and the check."""
    fakes.set(decrypted_ipa=str(_ipa_with(tmp_path / "fixtures" / "still.ipa", main=1)))
    dropped = make_ipa(tmp_path / "drop.ipa", encrypted=True)
    with pytest.raises(AppStoreError, match="still encrypted"):
        with appstore.decrypted_if_encrypted(cfg, "example", dropped, emitter=None):
            pytest.fail("yielded an IPA that is still encrypted")


# --- get5-hint: the check's own failures are said as Graft's, and its copy goes --

import errno  # noqa: E402

from graft_ipa import ipa_inspect  # noqa: E402

_REAL_EXTRACT = ipa_inspect.extract_ipa


def _extract_then(monkeypatch: pytest.MonkeyPatch, after) -> list[Path]:
    """`extract_ipa` that really unpacks UnfairPlay's output, then calls
    `after()`; returns the folders it unpacked into."""
    into: list[Path] = []

    def extract(ipa: Path, target: Path) -> Path:
        into.append(target)
        _REAL_EXTRACT(ipa, target)
        assert any(target.rglob("*")), "the check's copy is there when `after` runs"
        after()
        return target

    monkeypatch.setattr(appstore, "extract_ipa", extract)
    return into


def test_a_full_disk_while_unpacking_for_the_check_is_not_blamed_on_unfairplay(
    cfg: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delta audit's get5-hint: an ENOSPC from unpacking the decrypted IPA
    was refused with "UnfairPlay can't decrypt this app on this Mac yet",
    which points at the wrong fix. It names Graft's own unpacking, and the
    error, and the copy is gone."""
    def full() -> None:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    into = _extract_then(monkeypatch, full)
    with pytest.raises(AppStoreError) as e:
        with appstore.fetched(cfg, "example", emitter=None):
            pytest.fail("yielded an IPA nobody could check")
    assert "couldn't unpack the decrypted IPA to check it" in e.value.message
    assert os.strerror(errno.ENOSPC) in e.value.message
    assert "refusing to stage it" in e.value.message
    assert "UnfairPlay can't decrypt" not in (e.value.hint or "")
    assert "disk" in (e.value.hint or "")
    assert into and not into[0].exists()
    assert _leftovers(cfg) == []


def test_an_output_the_check_can_t_read_still_gets_the_unfairplay_hint(cfg: Config, fakes: FakeTools) -> None:
    """The other half: UnfairPlay's output itself unreadable (no Mach-O where
    the bundle names its executable) is still UnfairPlay's to answer for."""
    fakes.set(decrypted_ipa=str(make_ipa(Path(fakes.root) / "fixtures" / "not-macho.ipa", encrypted=None)))
    with pytest.raises(AppStoreError) as e:
        with appstore.fetched(cfg, "example", emitter=None):
            pytest.fail("yielded an IPA nobody could check")
    assert e.value.hint == appstore._STILL_ENCRYPTED_HINT


def _stop_during_the_check(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    def stop() -> None:
        # Safe whatever the code under test does: with no Python handler in
        # place the SIGTERM would end pytest itself, so it is never sent.
        if not callable(signal.getsignal(signal.SIGTERM)):
            pytest.fail("no Stop handler is installed during the check")
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(30)  # the handler raises out of here; this never finishes

    return _extract_then(monkeypatch, stop)


def test_a_stop_during_the_check_ends_the_get_at_once_and_removes_everything(
    cfg: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delta audit's critic: nobody had stopped a Get while it unpacks
    the whole decrypted IPA into work/cryptid-check. The GUI's Stop is a
    SIGTERM; it ends the Get at once, and neither the copy nor the rest of
    the working folder is left."""
    into = _stop_during_the_check(monkeypatch)
    started = time.monotonic()
    with pytest.raises(AppStoreError) as e:
        with appstore.fetched(cfg, "example", emitter=None):
            pytest.fail("yielded an IPA after a Stop")
    assert time.monotonic() - started < 10
    assert e.value.message.startswith("stopped before finishing")
    assert into and into[0].name == "cryptid-check" and not into[0].exists()
    assert _leftovers(cfg) == []


def test_a_stop_during_the_check_removes_the_copy_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_refuse_still_encrypted` removes its own copy on a Stop, rather than
    leaving it to whoever made `work`: the docstring's "that copy is gone
    before this returns"."""
    ipa = _ipa_with(tmp_path / "fixtures" / "ok.ipa", main=0)
    work = tmp_path / "work"
    work.mkdir()
    _stop_during_the_check(monkeypatch)
    with pytest.raises(AppStoreError, match="stopped before finishing"), appstore._stop_cleans_up():
        appstore._refuse_still_encrypted(ipa, work, source=ipa)
    assert work.is_dir() and list(work.iterdir()) == []


@pytest.mark.parametrize("failure", ["full", "unreadable", "still-encrypted", "fine"])
def test_the_check_s_copy_is_gone_on_every_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    ipa = _ipa_with(tmp_path / "fixtures" / "x.ipa", main=1 if failure == "still-encrypted" else 0)
    if failure == "full":
        def full() -> None:
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        _extract_then(monkeypatch, full)
    elif failure == "unreadable":
        ipa = make_ipa(tmp_path / "fixtures" / "not-macho.ipa", encrypted=None)
    work = tmp_path / "work"
    work.mkdir()
    if failure == "fine":
        appstore._refuse_still_encrypted(ipa, work, source=ipa)
    else:
        with pytest.raises(AppStoreError):
            appstore._refuse_still_encrypted(ipa, work, source=ipa)
    assert list(work.iterdir()) == []


def _with_a_damaged_member(ipa: Path, *, how: str = "block") -> Path:
    """`ipa` plus one deflated member whose compressed stream is damaged, as
    UnfairPlay passes through untouched (it raw-copies every member it
    doesn't replace): reading it back raises `zlib.error`, not an OSError."""
    with zipfile.ZipFile(ipa, "a", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Payload/App.app/Assets.car", bytes(range(256)) * 400)
    with zipfile.ZipFile(ipa) as zf:
        info = zf.getinfo("Payload/App.app/Assets.car")
    data = bytearray(ipa.read_bytes())
    start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    if how == "block":
        # The first block's header: BFINAL=1 with the reserved block type
        # 3, which zlib refuses as "invalid block type".
        data[start] = 0xFF
    else:
        # Bytes inside a block, which decode to the wrong bytes: zipfile's
        # "Bad CRC-32", a BadZipFile that `extract_ipa` wraps.
        middle = start + info.compress_size // 2
        data[middle:middle + 8] = b"\xff" * 8
    ipa.write_bytes(bytes(data))
    return ipa


@pytest.mark.parametrize("how", ["block", "crc"])
def test_a_damaged_member_in_the_output_is_refused_with_a_hint_and_no_copy(tmp_path: Path, how: str) -> None:
    """The try15 delta audit's d15-py-5: `extract_ipa` wraps only
    BadZipFile, so a damaged deflate stream raised `zlib.error` past both
    clauses and the user got cli's generic "isn't a failure graft knows how
    to explain". It is refused as an IPA Graft couldn't unpack, with a hint,
    and the copy is gone. A bad CRC, the BadZipFile `extract_ipa` does wrap,
    is the same damage and gets the same hint, never "UnfairPlay can't
    decrypt this app" (the try16 reviews)."""
    ipa = _with_a_damaged_member(_ipa_with(tmp_path / "fixtures" / "damaged.ipa", main=0), how=how)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(AppStoreError) as e:
        appstore._refuse_still_encrypted(ipa, work, source=ipa)
    assert "couldn't unpack the decrypted IPA to check it" in e.value.message
    assert "damaged" in e.value.message and "refusing to stage it" in e.value.message
    assert e.value.hint == appstore._DAMAGED_HINT
    assert isinstance(e.value.__cause__, zlib.error if how == "block" else ipa_inspect.IPAInspectionError)
    assert list(work.iterdir()) == []


@pytest.mark.parametrize("hint", ["_STILL_ENCRYPTED_HINT", "_UNPACK_FAILED_HINT", "_DAMAGED_HINT"])
def test_the_refusal_hints_fit_a_dropped_ipa_too(hint: str) -> None:
    """d15-py-4: `_refuse_still_encrypted` refuses a Get's output and a
    dropped or --provide-ipa one's alike (`decrypted_if_encrypted`), where
    no Get is involved, so neither hint says to get it again."""
    text = getattr(appstore, hint)
    assert "get it again" not in text
    assert "try again" in text
