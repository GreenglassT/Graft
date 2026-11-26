"""The App Store verbs end to end through the CLI, against the fake tools.

`--appstore-status`, `--appstore-check`, `--appstore-get`, and `--provide-ipa`'s
new decrypt. `test_appstore.py` holds the module's own rules; this holds what a
caller of the CLI — the GUI, or a person — actually sees: the event stream, the
exit status, the hint on stderr, and what ends up in the cache.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from graft_ipa import appstore, itunes
from graft_ipa import cli as cli_mod
from graft_ipa.config import add_app, load_config
from tests.appstore_fakes import FakeTools, itunes_offline, make_ipa


@pytest.fixture(autouse=True)
def _no_price_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A license refusal looks the price up; never on the network here."""
    monkeypatch.setattr(itunes, "lookup_by_bundle_id", itunes_offline)


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
    return load_config(config).cache_dir


def _run(args: list[str]):
    return CliRunner().invoke(cli_mod.app, args)


def _events(result) -> list[dict]:
    return [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]


def _types(result) -> list[str]:
    return [e["type"] for e in _events(result)]


# --- --appstore-status -------------------------------------------------------


def test_status_is_one_event(config: Path) -> None:
    result = _run(["--json", "--appstore-status"])

    assert result.exit_code == 0, result.output
    (event,) = _events(result)
    assert event["type"] == "appstore-status"
    assert event["ipatool"]["signed_in"] is True
    assert event["unfairplay"]["enrolled"] is True


def test_status_for_a_person_is_not_json(config: Path, fakes: FakeTools) -> None:
    fakes.set(signed_in=False)

    result = _run(["--appstore-status"])

    assert result.exit_code == 0, result.output
    assert '{"type"' not in result.stdout
    assert "NOT signed in" in result.stdout
    assert "--appstore-login" in result.stdout


# --- --appstore-check --------------------------------------------------------


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        itunes, "lookup_by_bundle_id",
        lambda bundle_id, country=None: itunes.ITunesApp(
            track_id=1, bundle_id=bundle_id, name="x", version="9.0"
        ),
    )


def test_check_one_app(config: Path, store: None) -> None:
    # A second app, so "every app" and "the one asked for" differ.
    add_app(config, "bravo", {"bundle_id": "com.bravo.app", "tweaks": []})

    result = _run(["--json", "--appstore-check", "--app", "example"])

    assert result.exit_code == 0, result.output
    (event,) = _events(result)
    assert event["app"] == "example"
    assert event["store_version"] == "9.0"


def test_check_without_app_checks_every_app(config: Path, store: None) -> None:
    add_app(config, "bravo", {"bundle_id": "com.bravo.app", "tweaks": []})

    result = _run(["--json", "--appstore-check"])

    assert result.exit_code == 0, result.output
    assert sorted(e["app"] for e in _events(result)) == ["bravo", "example"]


def test_a_stray_flag_on_check_is_named(config: Path) -> None:
    result = _run(["--appstore-check", "--allow-unverified"])

    assert result.exit_code == 2
    assert "--allow-unverified is not valid with --appstore-check" in result.output


# --- --appstore-get ----------------------------------------------------------


def _staged(cache_dir: Path) -> list[Path]:
    return sorted(cache_dir.glob("*.ipa"))


def test_get_streams_its_stages_and_stages_the_decrypted_ipa(
    config: Path, cache_dir: Path
) -> None:
    result = _run(["--json", "--appstore-get", "--app", "example"])

    assert result.exit_code == 0, result.output
    events = _events(result)
    assert [(e["type"], e.get("stage")) for e in events] == [
        ("appstore-stage", "download"),
        ("appstore-stage", "decrypt"),
        ("appstore-decrypted", None),
        ("appstore-stage", "stage"),
        ("ipa-provided", None),
    ]
    provided = events[-1]
    assert provided["filename"] == "com.example.app_21.46.3.ipa"
    assert _staged(cache_dir) == [cache_dir / "com.example.app_21.46.3.ipa"]
    assert appstore.main_binary_encrypted(_staged(cache_dir)[0]) is False
    assert list((cache_dir / appstore.TMP_DIRNAME).iterdir()) == []


def test_get_needs_an_app(config: Path) -> None:
    result = _run(["--appstore-get"])

    assert result.exit_code == 2
    assert "--appstore-get requires --app <name>" in result.output


def test_a_failed_get_exits_20_with_the_hint_and_stages_nothing(
    config: Path, cache_dir: Path, fakes: FakeTools
) -> None:
    fakes.set(download_error="license is required")

    result = _run(["--json", "--appstore-get", "--app", "example"])

    assert result.exit_code == 20
    assert "tap Get" in result.stderr
    assert _staged(cache_dir) == []
    assert list((cache_dir / appstore.TMP_DIRNAME).iterdir()) == []


def test_the_unverified_refusal_reaches_the_event_stream(
    config: Path, cache_dir: Path, fakes: FakeTools, tmp_path: Path
) -> None:
    """Both ways in: a get, and a drop of an encrypted IPA. The event is what
    the app keys "Use it anyway…" on (C-UNVERIFIED)."""
    fakes.set(plaintext_verified=False)
    drop = make_ipa(tmp_path / "Downloads" / "Example.ipa", encrypted=True)

    for args in (
        ["--json", "--appstore-get", "--app", "example"],
        ["--json", "--provide-ipa", str(drop), "--app", "example"],
    ):
        result = _run(args)
        assert result.exit_code == 20, result.output
        refused = [e for e in _events(result) if e["type"] == "appstore-refused"]
        assert refused == [{"type": "appstore-refused", "app": "example", "reason": "unverified"}]
    assert _staged(cache_dir) == []


def test_other_get_failures_are_not_the_unverified_refusal(
    config: Path, fakes: FakeTools
) -> None:
    fakes.set(download_error="license is required")

    result = _run(["--json", "--appstore-get", "--app", "example"])

    assert result.exit_code == 20
    # A missing license is a refusal of its own ("needs-license"), never the
    # one "Use it anyway…" answers.
    reasons = [e["reason"] for e in _events(result) if e["type"] == "appstore-refused"]
    assert reasons == [appstore.REFUSED_NEEDS_LICENSE]


def test_the_refusal_reason_is_the_word_the_app_reads() -> None:
    import re

    from tests.cli_ast import contract_swift_source

    match = re.search(
        r"enum AppStoreRefusal \{.*?static let unverified = \"([^\"\\]*)\"",
        contract_swift_source(),
        re.S,
    )
    assert match, "CLIContract.AppStoreRefusal.unverified is gone from CLIContract.swift"
    assert appstore.REFUSED_UNVERIFIED == match.group(1)


def test_unverified_is_refused_and_then_accepted_on_request(
    config: Path, cache_dir: Path, fakes: FakeTools
) -> None:
    fakes.set(plaintext_verified=False)

    refused = _run(["--json", "--appstore-get", "--app", "example"])
    assert refused.exit_code == 20
    assert _staged(cache_dir) == []

    accepted = _run(["--json", "--appstore-get", "--app", "example", "--allow-unverified"])
    assert accepted.exit_code == 0, accepted.output
    assert _staged(cache_dir)
    warnings = [e for e in _events(accepted) if e["type"] == "log" and e["source"] == "warning"]
    assert len(warnings) == 1 and "could not verify" in warnings[0]["line"]


# --- --provide-ipa -----------------------------------------------------------


def test_an_encrypted_drop_is_decrypted_before_it_is_staged(
    config: Path, cache_dir: Path, tmp_path: Path
) -> None:
    drop = make_ipa(tmp_path / "Downloads" / "Example.ipa", encrypted=True)

    result = _run(["--json", "--provide-ipa", str(drop), "--app", "example"])

    assert result.exit_code == 0, result.output
    assert _types(result) == [
        "appstore-stage", "appstore-decrypted", "appstore-stage", "ipa-provided",
    ]
    (staged,) = _staged(cache_dir)
    assert appstore.main_binary_encrypted(staged) is False
    assert sorted(p.name for p in drop.parent.iterdir()) == ["Example.ipa"], (
        "nothing may be left beside the user's own file"
    )


def test_an_encrypted_drop_without_enrollment_is_refused_and_not_cached(
    config: Path, cache_dir: Path, fakes: FakeTools, tmp_path: Path
) -> None:
    """The defect this replaced: an encrypted drop was cached, and the build
    then died at stage 2.5 with nothing pointing at the cause."""
    (fakes.models / "enrollment.bin").unlink()
    drop = make_ipa(tmp_path / "Example.ipa", encrypted=True)

    result = _run(["--json", "--provide-ipa", str(drop), "--app", "example"])

    assert result.exit_code == 20
    assert "--appstore-enroll-command" in "".join(result.stderr.split())
    assert _staged(cache_dir) == []


def test_a_decrypted_drop_is_copied_byte_for_byte_and_says_nothing_new(
    config: Path, cache_dir: Path, fakes: FakeTools, tmp_path: Path
) -> None:
    drop = make_ipa(tmp_path / "Example.ipa", encrypted=False, marker="mine")

    result = _run(["--json", "--provide-ipa", str(drop), "--app", "example"])

    assert result.exit_code == 0, result.output
    assert _types(result) == ["ipa-provided"]
    (staged,) = _staged(cache_dir)
    assert staged.read_bytes() == drop.read_bytes()
    assert fakes.calls("unfairplay") == []


def test_a_drop_for_the_wrong_app_fails_before_anything_is_decrypted(
    config: Path, fakes: FakeTools, tmp_path: Path
) -> None:
    drop = make_ipa(tmp_path / "Bravo.ipa", bundle_id="com.bravo.app", encrypted=True)

    result = _run(["--json", "--provide-ipa", str(drop), "--app", "example"])

    assert result.exit_code == 14
    assert fakes.calls("unfairplay") == []


# --- --appstore-login / --appstore-logout -------------------------------------

import subprocess  # noqa: E402
import sys  # noqa: E402

from tests.appstore_fakes import FAKE_EMAIL  # noqa: E402
from tests.conftest import REPO_ROOT  # noqa: E402


def _login(stdin: bytes, *args: str) -> subprocess.CompletedProcess[str]:
    """The real CLI, as a child: it reads the Apple ID and password from its
    own stdin descriptor, which CliRunner doesn't give it."""
    return subprocess.run(
        [sys.executable, "-m", "graft_ipa", "--json", "--appstore-login", *args],
        input=stdin.decode(), capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )


def test_login_reads_the_apple_id_and_password_from_stdin(config: Path, fakes: FakeTools) -> None:
    fakes.set(login_two_factor=False)
    proc = _login(f"{FAKE_EMAIL}\nExample-Passw0rd\n".encode())
    assert proc.returncode == 0, proc.stderr
    events = [json.loads(line) for line in proc.stdout.splitlines()]
    assert events[-1] == {"type": "appstore-login", "state": "signed-in", "email": FAKE_EMAIL}


def test_login_with_nothing_on_stdin_says_so(config: Path, fakes: FakeTools) -> None:
    proc = _login(b"")
    assert proc.returncode == 20
    events = [json.loads(line) for line in proc.stdout.splitlines()]
    assert events[-1]["state"] == "failed"
    assert "no Apple ID" in " ".join(proc.stderr.split())
    assert fakes.login_runs() == ([], [])


def test_login_refuses_an_ipatool_other_than_the_one_expected(config: Path, fakes: FakeTools, tmp_path: Path) -> None:
    """`--expect-ipatool`: the sign-in sheet's ipatool, or nothing read and
    nothing started (the fix re-check's problem 10)."""
    expected = tmp_path / "the-one-the-sheet-named" / "ipatool"
    proc = _login(f"{FAKE_EMAIL}\nExample-Passw0rd\n".encode(), "--expect-ipatool", str(expected))
    assert proc.returncode == 20, proc.stderr
    (event,) = [json.loads(line) for line in proc.stdout.splitlines()]
    assert (event["state"], event["reason"]) == ("failed", "ipatool-changed")
    assert str(expected) in event["message"] and str(fakes.ipatool) in event["message"]
    assert fakes.calls("ipatool") == [], "not even `--version`"


def test_login_with_the_expected_ipatool_signs_in(config: Path, fakes: FakeTools) -> None:
    fakes.set(login_two_factor=False)
    proc = _login(f"{FAKE_EMAIL}\nExample-Passw0rd\n".encode(), "--expect-ipatool", str(fakes.ipatool))
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.splitlines()[-1])["state"] == "signed-in"


def test_expect_ipatool_belongs_to_appstore_login_only(config: Path, tmp_path: Path) -> None:
    from graft_ipa.errors import ConfigError

    result = _run(["--appstore-status", "--expect-ipatool", str(tmp_path / "ipatool")])
    assert result.exit_code == ConfigError.exit_code, result.output
    assert "--expect-ipatool" in " ".join(result.output.split())


def test_there_is_no_email_option(config: Path) -> None:
    """Decision 3: the Apple ID is never an argument, Graft's or ipatool's,
    where `ps` would show it."""
    result = _run(["--json", "--appstore-login", "--email", "someone@example.invalid"])
    assert result.exit_code == 2
    assert "No such option" in " ".join(result.output.split())


def test_logout_revokes_and_forgets_what_revoke_removed(config: Path, fakes: FakeTools) -> None:
    """`auth revoke` removes `account` and `kbsync-v1`, and leaves
    `anisette-v4`, which stays Graft's on the record (the 2.0 review's #15)."""
    import json

    from graft_ipa import appstore_account

    appstore_account.marker_path().parent.mkdir(parents=True, exist_ok=True)
    appstore_account.marker_path().write_text(json.dumps({
        "format": 2, "ipatool_source": "bundled", "signed_in_at": "2026-11-22T00:00:00+00:00",
        "bundled_items": {"account": "20261122000000Z", "anisette-v4": "20261122000007Z",
                          "kbsync-v1": "20261122000014Z"},
    }))
    result = _run(["--json", "--appstore-logout"])
    assert result.exit_code == 0, result.output
    assert _events(result) == [{"type": "appstore-logout", "signed_out": True}]
    assert [c["argv"] for c in fakes.calls("ipatool")] == [
        ["auth", "revoke", "--format", "json", "--non-interactive"]
    ]
    assert appstore_account.vouched() == {"anisette-v4": "20261122000007Z"}


def test_logout_with_nothing_signed_in_is_still_signed_out(config: Path, fakes: FakeTools) -> None:
    fakes.set(revoke_error=(
        "failed to remove account from keychain: failed to remove item: "
        "The specified item could not be found in the keychain."
    ))
    result = _run(["--json", "--appstore-logout"])
    assert result.exit_code == 0, result.output


def test_a_logout_that_fails_says_so(config: Path, fakes: FakeTools) -> None:
    fakes.set(revoke_error="failed to remove account from keychain: User interaction is not allowed.")
    result = _run(["--json", "--appstore-logout"])
    assert result.exit_code == 20
    assert "couldn't sign out" in " ".join(result.output.split())


def test_a_successful_get_forgets_a_version_the_apple_id_was_refused(config: Path) -> None:
    from graft_ipa import update_refusals

    cfg = load_config(config)
    update_refusals.remember(cfg.state_dir, cfg.get_app("example"), "21.46.3")

    result = _run(["--json", "--appstore-get", "--app", "example"])

    assert result.exit_code == 0, result.output
    assert update_refusals.refused_version(cfg.state_dir, cfg.get_app("example")) is None


def test_a_failed_get_keeps_it(config: Path, fakes: FakeTools) -> None:
    from graft_ipa import update_refusals

    cfg = load_config(config)
    update_refusals.remember(cfg.state_dir, cfg.get_app("example"), "21.46.3")
    fakes.set(download_error="license is required")

    result = _run(["--json", "--appstore-get", "--app", "example"])

    assert result.exit_code != 0
    assert update_refusals.refused_version(cfg.state_dir, cfg.get_app("example")) == "21.46.3"


def test_staged_as_get_reads_a_version_without_a_lookup(config: Path, cache_dir: Path) -> None:
    cfg = load_config(config)
    make_ipa(cache_dir / "com.example.app_2.0.ipa", version="2.0", encrypted=False)

    found = appstore.staged_as_get(cfg, cfg.get_app("example"), "2.0")

    assert found is not None and found.version == "2.0"
    assert appstore.staged_as_get(cfg, cfg.get_app("example"), "3.0") is None
    assert issubclass(appstore.GetStopped, appstore.AppStoreError)


def test_staged_as_get_never_answers_for_apples_own_app(config: Path, cache_dir: Path) -> None:
    """A get refuses Apple's own apps, so no staged copy of one counts as a get's."""
    add_app(config, "apple", {"bundle_id": "com.apple.Example"})
    make_ipa(cache_dir / "com.apple.Example_2.0.ipa", bundle_id="com.apple.Example", version="2.0",
             encrypted=False)
    cfg = load_config(config)

    assert appstore.staged_as_get(cfg, cfg.get_app("apple"), "2.0") is None
