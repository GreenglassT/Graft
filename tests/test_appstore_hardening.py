"""The App Store path against tools that misbehave, and against being stopped.

Review findings, each pinned: ipatool lines that are missing, truncated, on
the other stream or name no file; an `auth info` that never answers; an
UnfairPlay report with junk around it or junk in it; a `PlaintextVerified`
that is truthy without being `true`; a SIGTERM that lands during cleanup; and
the GUI's Stop, end to end through a real CLI process.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from graft_ipa import appstore, ipatool, unfairplay
from graft_ipa.config import Config, load_config
from graft_ipa.errors import AppStoreError
from tests.appstore_fakes import FakeTools, make_ipa

REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture
def fakes(tmp_path: Path) -> FakeTools:
    return FakeTools(tmp_path / "tools")


@pytest.fixture
def config_path(temp_config_env: Path, fakes: FakeTools) -> Path:
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["appstore"] = fakes.appstore_section()
    temp_config_env.write_text(yaml.safe_dump(raw, sort_keys=False))
    return temp_config_env


@pytest.fixture
def cfg(config_path: Path) -> Config:
    return load_config(config_path)


def _out(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    return out


def _leftovers(cfg: Config) -> list[str]:
    root = cfg.cache_dir / appstore.TMP_DIRNAME
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


# --- ipatool's output --------------------------------------------------------


@pytest.mark.parametrize("shape", ["null", "omit"])
def test_success_that_names_no_file_is_an_error_of_its_own(
    fakes: FakeTools, tmp_path: Path, shape: str
) -> None:
    """`Path("")` resolves to the current directory, so this used to be
    reported as "ipatool wrote the IPA outside …: ." — a wrong diagnosis."""
    fakes.set(download_no_output=shape)

    with pytest.raises(AppStoreError) as e:
        ipatool.download(fakes.ipatool, "com.x", _out(tmp_path))

    assert e.value.message == "ipatool reported success but named no file"


def test_no_verdict_line_at_all_is_an_error_carrying_what_it_did_say(
    fakes: FakeTools, tmp_path: Path
) -> None:
    fakes.set(download_raw="downloading... 50%", download_exit=0)

    with pytest.raises(AppStoreError) as e:
        ipatool.download(fakes.ipatool, "com.x", _out(tmp_path))

    assert e.value.message == "ipatool couldn't download com.x: downloading... 50%"


def test_a_truncated_verdict_line_is_an_error_not_a_crash(fakes: FakeTools, tmp_path: Path) -> None:
    fakes.set(download_raw='{"level":"info","output":"/x/y.ipa","succ', download_exit=0)

    with pytest.raises(AppStoreError) as e:
        ipatool.download(fakes.ipatool, "com.x", _out(tmp_path))

    assert e.value.message.startswith("ipatool couldn't download com.x: ")


def test_an_error_line_on_stderr_is_still_classified(fakes: FakeTools, tmp_path: Path) -> None:
    fakes.set(download_error="license is required", error_on_stderr=True)

    with pytest.raises(AppStoreError) as e:
        ipatool.download(fakes.ipatool, "com.x", _out(tmp_path))

    assert e.value.message == "this Apple ID doesn't own com.x yet (ipatool: license is required)"


# --- auth info that doesn't answer -------------------------------------------


def test_a_sign_in_check_that_times_out_is_unknown_not_signed_out(fakes: FakeTools) -> None:
    """Telling the user to sign in when ipatool merely hung sends them to type
    a password and a 2FA code for nothing."""
    fakes.set(auth_sleep=5)

    assert ipatool.signed_in(fakes.ipatool, timeout=0.5) is None


def test_an_answer_that_is_not_json_is_unknown(fakes: FakeTools) -> None:
    fakes.set(auth_raw="Segmentation fault")

    assert ipatool.signed_in(fakes.ipatool) is None


def test_an_explicit_failure_is_still_signed_out(fakes: FakeTools) -> None:
    fakes.set(signed_in=False)

    assert ipatool.signed_in(fakes.ipatool) is False


def test_status_says_it_couldnt_tell_and_suggests_no_login(cfg: Config, fakes: FakeTools) -> None:
    fakes.set(auth_raw="Segmentation fault")

    payload = appstore.status(cfg.appstore)

    assert payload["ipatool"]["signed_in"] is None
    assert not any("auth login" in c for c in payload["setup_commands"])
    # Not silence, either: "couldn't tell" with nothing to do about it leaves
    # the Settings pane with a red dot and no next step.
    assert payload["setup_commands"] == [
        f"# run '{fakes.ipatool} auth info' in Terminal to see why ipatool "
        "couldn't say whether it is signed in"
    ]


def test_a_get_that_cant_tell_says_so(cfg: Config, fakes: FakeTools) -> None:
    fakes.set(auth_raw="Segmentation fault")

    with pytest.raises(AppStoreError) as e, appstore.fetched(cfg, "example", emitter=None):
        pytest.fail("must not download without knowing the account is signed in")

    assert e.value.message.startswith("couldn't tell whether ipatool is signed in")
    assert "auth info" in e.value.hint
    assert not [c for c in fakes.calls("ipatool") if c["argv"][0] == "download"]


# --- UnfairPlay's report ------------------------------------------------------


def _decrypt(fakes: FakeTools, tmp_path: Path) -> unfairplay.DecryptReport:
    work = _out(tmp_path)
    return unfairplay.decrypt(
        fakes.unfairplay,
        input_ipa=make_ipa(tmp_path / "in" / "enc.ipa", encrypted=True),
        output_ipa=work / "out.ipa", report=work / "r.json", model_dir=fakes.models,
    )


@pytest.mark.parametrize("truthy", ["true", 1])
def test_plaintext_verified_must_be_the_json_true(
    fakes: FakeTools, tmp_path: Path, truthy: object
) -> None:
    """`bool("false")` is True; only the JSON literal `true` is a verdict."""
    fakes.set(plaintext_verified=truthy)

    assert _decrypt(fakes, tmp_path).plaintext_verified is False


@pytest.mark.parametrize("truthy", ["true", 1])
def test_and_so_a_truthy_verdict_is_refused_by_the_get(
    cfg: Config, fakes: FakeTools, truthy: object
) -> None:
    fakes.set(plaintext_verified=truthy)

    with pytest.raises(AppStoreError) as e, appstore.fetched(cfg, "example", emitter=None):
        pytest.fail("an unverified decrypt must not be yielded")

    assert "PlaintextVerified: false" in e.value.message


def test_a_report_after_log_lines_with_braces_is_still_found(
    fakes: FakeTools, tmp_path: Path
) -> None:
    fakes.set(decrypt_preamble="loading model {d713869b}\nSINF map: {1 entries}")

    report = _decrypt(fakes, tmp_path)

    assert report.encrypted_images == 8
    assert report.plaintext_verified is True


@pytest.mark.parametrize("raw", ["", "done.", "{not json at all}", '{"OutputPath": "/x"}'])
def test_exit_0_with_no_usable_report_is_an_error(
    fakes: FakeTools, tmp_path: Path, raw: str
) -> None:
    fakes.set(decrypt_raw=raw)

    with pytest.raises(AppStoreError) as e:
        _decrypt(fakes, tmp_path)

    assert e.value.message in (
        "UnfairPlay finished but printed no report",
        "UnfairPlay's report is missing EncryptedImages, DecryptedPages, PlaintextVerified",
    )


@pytest.mark.parametrize("field", ["encrypted_images", "decrypted_pages"])
@pytest.mark.parametrize("bad", ["eight", None, [1]])
def test_a_malformed_count_is_an_app_store_error(
    fakes: FakeTools, tmp_path: Path, field: str, bad: object
) -> None:
    fakes.set(**{field: bad})

    with pytest.raises(AppStoreError) as e:
        _decrypt(fakes, tmp_path)

    assert e.value.message.startswith("UnfairPlay's report has a malformed")


# --- being stopped ------------------------------------------------------------


def test_a_stop_that_lands_during_cleanup_cannot_interrupt_it(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second SIGTERM case: the work dir is being removed when the signal
    arrives. Unblocked, the handler raises out of `rmtree` and the
    license-bearing download survives; blocked, it is delivered after."""
    real_rmtree = appstore.shutil.rmtree

    def interrupted(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(0.2)  # give an unblocked handler every chance to run here
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(appstore.shutil, "rmtree", interrupted)

    with pytest.raises(AppStoreError) as e:
        with appstore.fetched(cfg, "example", emitter=None):
            pass

    assert e.value.message.startswith("stopped before finishing")
    assert _leftovers(cfg) == []


def test_the_stop_message_does_not_claim_nothing_was_staged(cfg: Config) -> None:
    """A Stop can land after `os.replace` has put the file in place, so
    "nothing was staged" would sometimes be false."""
    with pytest.raises(AppStoreError) as e, appstore.fetched(cfg, "example", emitter=None):
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(1)

    assert "nothing was staged" not in e.value.message


def test_the_guis_stop_leaves_no_work_dir_behind(
    config_path: Path, cfg: Config, fakes: FakeTools
) -> None:
    """End to end, the way the GUI does it: a real CLI process mid-download is
    sent SIGTERM (`ProcessHandle.stop`). The scoped handler turns that into an
    exception, the work dir goes, and the fake ipatool child is killed too."""
    fakes.set(download_sleep=30)
    proc = subprocess.Popen(
        [sys.executable, "-m", "graft_ipa.cli", "--json", "--appstore-get",
         "--app", "example", "--config", str(config_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=REPO_ROOT,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not any(c.get("sleeping") for c in fakes.calls()):
            time.sleep(0.05)
        assert any(c.get("sleeping") for c in fakes.calls()), "premise: the download started"
        assert len(_leftovers(cfg)) == 1, "premise: the work dir exists mid-download"

        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()

    assert proc.returncode == 20, stderr
    assert "stopped before finishing" in stderr
    assert _leftovers(cfg) == []
    assert not list(cfg.cache_dir.glob("*.ipa"))
    events = [json.loads(line) for line in stdout.splitlines()]
    assert not [e for e in events if e.get("type") == "ipa-provided"]

    # Only the CLI was signalled here — the GUI also SIGTERMs descendants, this
    # deliberately does not — so a dead child proves `subprocess.run` killed
    # it on the way out rather than leaving a download running orphaned.
    (sleeper,) = [c for c in fakes.calls() if c.get("sleeping")]
    deadline = time.time() + 5
    while time.time() < deadline and _alive(sleeper["pid"]):
        time.sleep(0.05)
    assert not _alive(sleeper["pid"]), "the fake ipatool outlived the stopped CLI"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
