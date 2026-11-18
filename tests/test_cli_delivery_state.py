"""Where a delivery row comes from — the push, and nothing else — and how it
is surfaced: the `delivery-state` event, the `--delivery-state` verb, and the
column `--list` prints for a human.

"Built but never delivered" is one of the four states this feature exists to
surface, so the tests that matter most here are the negative ones — a dry run,
a build with no install step, a build that fails, a push that throws. Each of
those must leave the `deliveries` table exactly as it found it.

`_build_one` is driven directly with every external stage stubbed out, as in
`test_cli_build_retention.py`. Nothing here touches a device, the network, or
any path outside `tmp_path`.
"""

from __future__ import annotations

import hashlib
import json
import plistlib
import sqlite3
import zipfile
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from graft_ipa.build_log import BuildLog
from graft_ipa.config import (
    AppConfig,
    Config,
    CyanOptions,
    SigningConfig,
    add_app,
    load_config,
)
from graft_ipa.delivery import (
    CYAN_KEYS,
    FORMAT_VERSION,
    InputsDigest,
    compute_inputs_digest,
    diff_delivery,
)
from graft_ipa.errors import CyanError, InstallError
from graft_ipa.json_output import EVENT_TYPES, JsonEmitter

TWEAK_BYTES = b"tweak-payload"
TWEAKED_BYTES = b"y" * 64
# Deliberately not f"{app_name}.ipa". The row must carry the name lc_runner
# reports having written, not a second guess at how that name is built — Task
# 6's reconcile goes looking for this file on the device.
PUSHED_AS = "yt-as-pushed.ipa"


def _cfg(tmp_path: Path, *, delivery: str = "livecontainer") -> Config:
    tweaks = tmp_path / "tweaks"
    tweaks.mkdir(parents=True, exist_ok=True)
    (tweaks / "a.deb").write_bytes(TWEAK_BYTES)
    return Config(
        path=tmp_path / "config.yaml",
        signing=SigningConfig(
            cert=tmp_path / "cert.p12",
            cert_password_env="X",
            profile=tmp_path / "profile",
            team_id="TEAM",
        ),
        cache_dir=tmp_path / "cache",
        built_dir=tmp_path / "built",
        tweaks_dir=tweaks,
        state_dir=tmp_path / "state",
        apps={
            "yt": AppConfig(
                name="yt",
                bundle_id="com.example.app",
                preferred_version="latest",
                tweaks=["a.deb"],
                cyan=CyanOptions(display_name="Example+"),
                delivery=delivery,
                lc_bundle_id="com.kdt.livecontainer.T",
            )
        },
    )


def _log(cfg: Config) -> BuildLog:
    return BuildLog(cfg.state_dir / "log.sqlite")


def _lc_result() -> MagicMock:
    result = MagicMock()
    result.bundle_id = "com.kdt.livecontainer.T"
    result.size_bytes = 4096
    result.documents_path = f"Documents/{PUSHED_AS}"
    result.others = []
    return result


def _run_build(
    cfg: Config,
    tmp_path: Path,
    log: BuildLog,
    *,
    dry_run: bool = False,
    skip_install: bool = False,
    push: MagicMock | None = None,
    cyan_error: bool = False,
    install_error: bool = False,
) -> None:
    """Drive `_build_one` through to whichever push its delivery mode uses."""
    from graft_ipa import cli as cli_mod

    source = tmp_path / "in.ipa"
    source.write_bytes(b"x")
    tweaked = tmp_path / "tweaked.ipa"
    tweaked.write_bytes(TWEAKED_BYTES)

    def _fake_zsign(_cfg, _src, dst, **_kw):
        Path(dst).write_bytes(TWEAKED_BYTES)
        sg = MagicMock()
        sg.team_id = "TEAM"
        sg.cert_subject = "subject"
        sg.expiry = "2027-01-01"
        sg.stdout = ""
        sg.stderr = ""
        # A real SignResult only carries this when auto-provisioning had to
        # replace a cached profile. Left as a MagicMock it is truthy, and the
        # build warns about a re-provisioning that never happened.
        sg.profile_note = None
        return sg

    install_result = MagicMock()
    install_result.stdout = ""
    install_result.stderr = ""

    cyan_patch = (
        patch.object(cli_mod, "run_cyan", side_effect=CyanError("cyan blew up"))
        if cyan_error
        else patch.object(cli_mod, "run_cyan")
    )
    install_patch = (
        patch.object(cli_mod, "install", side_effect=InstallError("ideviceinstaller failed"))
        if install_error
        else patch.object(cli_mod, "install", return_value=install_result)
    )
    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "acquire_ipa") as acquire, \
         cyan_patch as cyan, \
         patch.object(cli_mod, "select_device", return_value=("UDID1", "usb")), \
         patch.object(cli_mod, "lc_runner") as lc, \
         patch.object(cli_mod, "run_zsign", side_effect=_fake_zsign), \
         install_patch:
        lc.push_ipa_to_livecontainer = push or MagicMock(return_value=_lc_result())
        acquire.return_value.path = source
        acquire.return_value.version = "21.16.2"
        if not cyan_error:
            cyan.return_value.output_ipa = tweaked
            cyan.return_value.stdout = ""
            cyan.return_value.stderr = ""
        cli_mod._build_one(
            cfg=cfg,
            log=log,
            app_name="yt",
            dry_run=dry_run,
            skip_install=skip_install,
            skip_decrypt_check=True,
            verbose=False,
            device=None,
            emitter=None,
            trash_dir=tmp_path / "Trash",
        )


# --- the helper, in isolation -------------------------------------------------


def _digest(cfg: Config, app: str = "example", version: str = "21.16.2"):
    return compute_inputs_digest(cfg, cfg.get_app(app), source_version=version)


def test_a_dry_run_records_no_delivery(temp_config_env: Path, tmp_path: Path) -> None:
    """A dry run touches no device, so it must not claim one was reached."""
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    log = BuildLog(cfg.state_dir / "log.sqlite")

    cli_mod._record_delivery_if_pushed(
        log, app_name="example", inputs=_digest(cfg), ipa_path=None,
        lc_bundle_id=None, remote_name=None, udid=None, dry_run=True,
    )

    assert log.latest_delivery("example") is None


def test_a_push_records_a_delivery_with_a_digest(
    temp_config_env: Path, tmp_path: Path
) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    cfg.tweaks_dir.mkdir(parents=True, exist_ok=True)
    for name in cfg.get_app("example").tweaks:
        (cfg.tweaks_dir / name).write_bytes(b"x")
    log = BuildLog(cfg.state_dir / "log.sqlite")

    cli_mod._record_delivery_if_pushed(
        log, app_name="example", inputs=_digest(cfg), ipa_path=None,
        lc_bundle_id="com.kdt.livecontainer.T", remote_name="example.ipa",
        udid="UDID1", dry_run=False,
    )

    row = log.latest_delivery("example")
    assert row is not None
    assert len(row["inputs_digest"]) == 64
    assert row["source_version"] == "21.16.2"
    assert row["lc_bundle_id"] == "com.kdt.livecontainer.T"


def test_no_digest_means_no_row(temp_config_env: Path, tmp_path: Path) -> None:
    """Fingerprinting is allowed to fail; a row with no digest is worse than none.

    `diff_delivery` reads a digestless row as `unknown`, so writing one would
    replace "we know nothing yet" with "we knew and lost it" — and it would do
    so on the same evidence.
    """
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    log = BuildLog(cfg.state_dir / "log.sqlite")

    cli_mod._record_delivery_if_pushed(
        log, app_name="example", inputs=None, ipa_path=None,
        lc_bundle_id=None, remote_name=None, udid="UDID1", dry_run=False,
    )

    assert log.latest_delivery("example") is None


# --- through a whole build: nothing but a push writes a row -------------------


def test_a_build_that_is_never_pushed_records_no_delivery(tmp_path: Path) -> None:
    """The load-bearing case: --skip-install builds an artifact and ships nothing.

    This is the motivating story — a finished tweak sitting undelivered — so a
    recording call anywhere on the build path (next to `log.record`, say) rather
    than inside the push block makes the whole feature report a lie.
    """
    cfg = _cfg(tmp_path)
    log = _log(cfg)

    _run_build(cfg, tmp_path, log, skip_install=True)

    assert log.recent(app="yt"), "the build itself should still be logged"
    assert log.latest_delivery("yt") is None


def test_a_dry_run_build_records_no_delivery(tmp_path: Path) -> None:
    """--dry-run walks the entire push block; only the guard stops it."""
    cfg = _cfg(tmp_path)
    log = _log(cfg)

    _run_build(cfg, tmp_path, log, dry_run=True)

    assert log.latest_delivery("yt") is None


def test_a_failed_build_records_no_delivery(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    log = _log(cfg)

    with pytest.raises(CyanError):
        _run_build(cfg, tmp_path, log, cyan_error=True)

    assert log.latest_delivery("yt") is None


def test_a_push_that_throws_records_no_delivery(tmp_path: Path) -> None:
    """Half a transfer is not a delivery."""
    cfg = _cfg(tmp_path)
    log = _log(cfg)
    exploding = MagicMock(side_effect=RuntimeError("device went away"))

    with pytest.raises(RuntimeError):
        _run_build(cfg, tmp_path, log, push=exploding)

    assert log.latest_delivery("yt") is None


def test_a_livecontainer_push_records_what_reached_the_phone(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    log = _log(cfg)

    _run_build(cfg, tmp_path, log)

    row = log.latest_delivery("yt")
    assert row is not None
    assert row["bundle_id"] == "com.example.app"
    assert row["source_version"] == "21.16.2"
    assert row["delivery"] == "livecontainer"
    assert row["lc_bundle_id"] == "com.kdt.livecontainer.T"
    # Taken from the push result, not rebuilt from the app name — see PUSHED_AS.
    assert row["remote_name"] == PUSHED_AS
    assert row["udid"] == "UDID1"
    # The archive that was pushed, not the pre-cyan source.
    assert row["ipa_sha256"] == hashlib.sha256(TWEAKED_BYTES).hexdigest()
    # digest_version travels with the row so a later comparison can say "a
    # different version of this tool delivered that" instead of inventing
    # reasons from a cyan key set that has since changed.
    assert row["digest_version"] == FORMAT_VERSION
    assert json.loads(row["tweaks"]) == [
        ["a.deb", hashlib.sha256(TWEAK_BYTES).hexdigest()]
    ]
    cyan = json.loads(row["cyan"])
    assert set(cyan) == set(CYAN_KEYS)
    assert cyan["display_name"] == "Example+"
    assert cyan["extra_args"] == []


def test_the_row_carries_the_tweak_bytes_that_were_injected(tmp_path: Path) -> None:
    """Not whatever is on disk once the transfer finishes.

    cyan consumes the tweak files at stage 3; the push can still be running
    minutes later. If the fingerprint were taken at recording time, a tweak
    rebuilt in that window would be recorded as delivered while the phone holds
    the previous build of it — the tool would say `current` about an artifact
    that never reached the device.
    """
    cfg = _cfg(tmp_path)
    log = _log(cfg)
    # What cyan will read, and therefore what is inside the pushed IPA.
    injected = _digest(cfg, app="yt")

    def _push_then_rebuild(*args, **kwargs):
        (cfg.tweaks_dir / "a.deb").write_bytes(b"rebuilt-mid-transfer")
        return _lc_result()

    _run_build(cfg, tmp_path, log, push=MagicMock(side_effect=_push_then_rebuild))

    row = log.latest_delivery("yt")
    assert row is not None
    assert row["inputs_digest"] == injected.digest
    assert json.loads(row["tweaks"]) == [
        ["a.deb", hashlib.sha256(TWEAK_BYTES).hexdigest()]
    ]
    assert (
        row["inputs_digest"] != _digest(cfg, app="yt").digest
    ), "the post-push state of the tweaks_dir must not be what got recorded"


def test_the_row_carries_the_format_version_that_produced_the_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not a constant that happens to equal it today.

    A writer that stamps a literal `1` is indistinguishable from a correct one
    while FORMAT_VERSION is 1 — and the day the format is bumped, every row
    would claim the old version, `diff_delivery`'s version guard would answer
    `unknown`, and no app would ever show a badge again.
    """
    from graft_ipa import delivery as delivery_mod

    bumped = FORMAT_VERSION + 1
    monkeypatch.setattr(delivery_mod, "FORMAT_VERSION", bumped)
    cfg = _cfg(tmp_path)
    log = _log(cfg)

    _run_build(cfg, tmp_path, log)

    assert log.latest_delivery("yt")["digest_version"] == bumped


def test_a_sideload_install_records_a_delivery(tmp_path: Path) -> None:
    """The other push path. No LiveContainer, so no LiveContainer fields."""
    cfg = _cfg(tmp_path, delivery="sideload")
    log = _log(cfg)

    _run_build(cfg, tmp_path, log)

    row = log.latest_delivery("yt")
    assert row is not None
    assert row["delivery"] == "sideload"
    assert row["lc_bundle_id"] is None
    assert row["remote_name"] is None
    assert row["udid"] == "UDID1"


def test_a_sideload_install_that_throws_records_no_delivery(tmp_path: Path) -> None:
    """The mirror of the livecontainer case, on the branch that has its own push.

    `ideviceinstaller` reports failure by raising, so a recording call placed
    ahead of it would still be reached — and the tool would report `current`
    for a phone the IPA never landed on.
    """
    cfg = _cfg(tmp_path, delivery="sideload")
    log = _log(cfg)

    with pytest.raises(InstallError):
        _run_build(cfg, tmp_path, log, install_error=True)

    assert log.latest_delivery("yt") is None


def test_a_dry_run_records_no_delivery_over_the_real_flag_path(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Through Typer's own parsing, not a hand-passed `dry_run=True`.

    `--dry-run` has been mis-parsed in this CLI before (a trailing `--dry-run`
    is still read as a second app name), and a dry run that silently became a
    real one would also silently record a delivery. Same reasoning as
    `test_prune_builds_dry_run_over_the_real_flag_path`.
    """
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod

    monkeypatch.setenv("HOME", str(tmp_path))  # nothing may reach the real ~/.Trash
    cfg = load_config(temp_config_env)
    source = tmp_path / "in.ipa"
    source.write_bytes(b"x")

    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "acquire_ipa") as acquire, \
         patch.object(cli_mod, "run_cyan") as cyan, \
         patch.object(cli_mod, "select_device", return_value=("UDID1", "usb")), \
         patch.object(cli_mod, "run_zsign") as zsign, \
         patch.object(cli_mod, "install") as installer:
        acquire.return_value.path = source
        acquire.return_value.version = "21.16.2"
        cyan.return_value.output_ipa = source
        cyan.return_value.stdout = cyan.return_value.stderr = ""
        zsign.return_value.stdout = zsign.return_value.stderr = ""
        # See `_fake_zsign`: a MagicMock attribute is truthy, and the build
        # reads this one to decide whether to warn about a re-provisioning.
        zsign.return_value.profile_note = None
        installer.return_value.stdout = installer.return_value.stderr = ""
        result = CliRunner().invoke(
            cli_mod.app, ["--dry-run", "--skip-decrypt-check", "example"]
        )

    assert result.exit_code == 0, result.output
    # The run really did reach the install step — otherwise this test would
    # pass simply by never getting near the recording call.
    assert installer.call_args.kwargs["dry_run"] is True
    assert BuildLog(cfg.state_dir / "log.sqlite").latest_delivery("example") is None


def test_a_recording_failure_does_not_fail_the_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The artifact already reached the phone; losing the bookkeeping is cheaper.

    Same reasoning as the retention guard directly below it: without this, a
    locked database would be reported to the GUI as a failed build and tell the
    user the install never happened.
    """
    cfg = _cfg(tmp_path)
    log = _log(cfg)

    def _boom(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(BuildLog, "record_delivery", _boom)

    _run_build(cfg, tmp_path, log)

    # stderr, not stdout: stdout is the JSON event stream under --json, where
    # the GUI's decoder drops anything that isn't a registered event. `_warn`
    # puts every warning on stderr and (with an emitter) into a `log` event.
    assert "couldn't record delivery" in capsys.readouterr().err
    with log._connect() as conn:
        successes = [r[0] for r in conn.execute("SELECT success FROM builds").fetchall()]
    assert successes == [1], "the push succeeded and must be logged that way"


# --- surfacing it: the event, the verb, and the column ------------------------


def _fake_ipa(cache: Path, bundle: str, version: str) -> Path:
    """An IPA just real enough for find_in_cache to resolve a version from."""
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{bundle}_{version}.ipa"
    info = {"CFBundleIdentifier": bundle, "CFBundleShortVersionString": version}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Payload/App.app/Info.plist", plistlib.dumps(info))
    return path


def _stage(cfg: Config, app_name: str = "example", *, version: str = "21.16.2") -> None:
    """Put on disk everything a build of `app_name` would read.

    Without the cached IPA there is no version to resolve, and every state that
    needs a comparison collapses to `unknown` — which several tests below could
    then pass against for the wrong reason.
    """
    app_cfg = cfg.get_app(app_name)
    cfg.tweaks_dir.mkdir(parents=True, exist_ok=True)
    for name in app_cfg.tweaks:
        (cfg.tweaks_dir / name).write_bytes(b"payload-" + name.encode())
    _fake_ipa(cfg.cache_dir, app_cfg.bundle_id, version)


def _deliver(
    cfg: Config,
    log: BuildLog,
    app_name: str = "example",
    inputs: InputsDigest | None = None,
    **overrides: object,
) -> None:
    """Record a delivery the way the push path does."""
    d = inputs if inputs is not None else _digest(cfg, app_name)
    fields: dict = {
        "app": app_name,
        "bundle_id": d.bundle_id,
        "inputs_digest": d.digest,
        "digest_version": d.version,
        "cyan": dict(d.cyan),
        "tweaks": [[t.name, t.sha256] for t in d.tweaks],
        "source_version": d.source_version,
        "ipa_sha256": None,
        "delivery": d.delivery,
        "lc_bundle_id": None,
        "remote_name": f"{app_name}.ipa",
        "udid": "UDID1",
    }
    fields.update(overrides)
    log.record_delivery(**fields)


def _events(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip().startswith("{")]


def _state(cfg: Config, log: BuildLog | None, app_name: str = "example") -> dict:
    from graft_ipa import cli as cli_mod

    return cli_mod._delivery_state_for(cfg, app_name, cfg.get_app(app_name), log)


def test_delivery_state_is_a_registered_event_type() -> None:
    """JsonEmitter.emit refuses anything unregistered, so this is load-bearing."""
    assert "delivery-state" in EVENT_TYPES


def test_list_emits_a_delivery_state_per_app(temp_config_env: Path) -> None:
    from typer.testing import CliRunner

    from graft_ipa.cli import app as cli_app

    result = CliRunner().invoke(cli_app, ["--json", "--list"])
    assert result.exit_code == 0, result.output

    events = _events(result.stdout)
    states = [e for e in events if e["type"] == "delivery-state"]
    apps = [e for e in events if e["type"] == "app"]

    assert len(states) == len(apps)
    assert {s["app"] for s in states} == {a["name"] for a in apps}
    # Nothing has been delivered in a fresh fixture. Note the fixture has no
    # cached IPA either: "never" is a fact about the delivery log alone and
    # must not be downgraded to "unknown" (which renders as no badge at all)
    # just because the tool can't currently say what a build would produce.
    assert all(s["kind"] == "never" for s in states)


def test_each_delivery_state_follows_its_own_app_event(temp_config_env: Path) -> None:
    """Emitted inside the loop, next to the app it describes.

    The pairing assertion above passes just as well if every app is emitted and
    then every state; a consumer reading the stream incrementally sees a very
    different thing.
    """
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod

    add_app(
        temp_config_env,
        "bravo",
        {"bundle_id": "com.bravo.app", "preferred_version": "latest", "tweaks": []},
    )
    result = CliRunner().invoke(cli_mod.app, ["--json", "--list"])
    assert result.exit_code == 0, result.output

    pairs = [(e["type"], e.get("name") or e.get("app")) for e in _events(result.stdout)]
    assert pairs == [
        ("app", "bravo"),
        ("delivery-state", "bravo"),
        ("app", "example"),
        ("delivery-state", "example"),
    ]


def test_delivery_state_verb_reports_one_app(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    buf = StringIO()
    cli_mod._do_delivery_state(cfg, app_name="example", emitter=JsonEmitter(stream=buf))

    events = _events(buf.getvalue())
    ev = next(e for e in events if e["type"] == "delivery-state")
    assert ev["app"] == "example"
    assert ev["kind"] == "never"


def test_the_verb_reports_every_app_when_none_is_named(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    add_app(
        temp_config_env,
        "bravo",
        {"bundle_id": "com.bravo.app", "preferred_version": "latest", "tweaks": []},
    )
    cfg = load_config(temp_config_env)
    buf = StringIO()
    cli_mod._do_delivery_state(cfg, emitter=JsonEmitter(stream=buf))

    assert [e["app"] for e in _events(buf.getvalue())] == ["bravo", "example"]


def test_the_verb_narrows_to_the_named_app(temp_config_env: Path) -> None:
    """With one app configured, `--app` narrowing nothing is indistinguishable
    from `--app` being ignored entirely."""
    from graft_ipa import cli as cli_mod

    add_app(
        temp_config_env,
        "bravo",
        {"bundle_id": "com.bravo.app", "preferred_version": "latest", "tweaks": []},
    )
    cfg = load_config(temp_config_env)
    buf = StringIO()
    cli_mod._do_delivery_state(cfg, app_name="example", emitter=JsonEmitter(stream=buf))

    assert [e["app"] for e in _events(buf.getvalue())] == ["example"]


def test_the_verb_accepts_app_and_rejects_an_unknown_one(temp_config_env: Path) -> None:
    """`--delivery-state --app <name>` must reach the app lookup.

    If `--app` weren't in the verb's allowed set, the stray-flag guard would
    reject the flag before the name was ever looked up — a different error for
    a different reason, which is why this asserts on the message.
    """
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod

    result = CliRunner().invoke(cli_mod.app, ["--delivery-state", "--app", "nope"])

    assert result.exit_code != 0
    assert "is not configured" in result.output


def test_the_verb_scoped_by_device_reports_never_for_a_different_phone(
    temp_config_env: Path,
) -> None:
    """A delivery to one phone must not read as reaching a different one.

    Real rows through BuildLog, not a mock — the call goes through `main`'s own
    `--device` option, `_verb_delivery_state`, `_do_delivery_state` and
    `_delivery_state_for` down to `BuildLog.latest_delivery`.
    """
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    _stage(cfg)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _deliver(cfg, log, udid="PHONE-B")

    result = CliRunner().invoke(
        cli_mod.app,
        ["--json", "--delivery-state", "--app", "example", "--device", "PHONE-A"],
    )

    assert result.exit_code == 0, result.output
    ev = next(e for e in _events(result.stdout) if e["type"] == "delivery-state")
    assert ev["kind"] == "never"


def test_the_verb_scoped_by_device_reports_the_state_for_that_phone(
    temp_config_env: Path,
) -> None:
    """The same delivery, asked about the phone it actually went to."""
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    _stage(cfg)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _deliver(cfg, log, udid="PHONE-B")

    result = CliRunner().invoke(
        cli_mod.app,
        ["--json", "--delivery-state", "--app", "example", "--device", "PHONE-B"],
    )

    assert result.exit_code == 0, result.output
    ev = next(e for e in _events(result.stdout) if e["type"] == "delivery-state")
    assert ev["kind"] == "current"


def test_the_verb_without_device_is_unchanged_from_today(temp_config_env: Path) -> None:
    """Omitting `--device` must still answer "anywhere" — today's behaviour —
    even though the only delivery on record went to a specific phone."""
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    _stage(cfg)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _deliver(cfg, log, udid="PHONE-B")

    result = CliRunner().invoke(
        cli_mod.app, ["--json", "--delivery-state", "--app", "example"]
    )

    assert result.exit_code == 0, result.output
    ev = next(e for e in _events(result.stdout) if e["type"] == "delivery-state")
    assert ev["kind"] == "current"


def test_never_survives_a_missing_source_ipa(temp_config_env: Path) -> None:
    """No cached IPA is not a reason to forget that nothing was ever delivered.

    `unknown` renders as no badge, so answering it here would hide the exact
    state this feature was built to surface — and would hide it on precisely
    the machines that have never built anything.
    """
    cfg = load_config(temp_config_env)
    assert not cfg.cache_dir.exists() or not list(cfg.cache_dir.glob("*.ipa"))

    assert _state(cfg, None)["kind"] == "never"
    assert _state(cfg, BuildLog(cfg.state_dir / "log.sqlite"))["kind"] == "never"


def test_the_never_wording_is_diff_deliverys_own(temp_config_env: Path) -> None:
    """One authority for the copy, even though this path skips the diff."""
    cfg = load_config(temp_config_env)
    _stage(cfg)

    assert _state(cfg, None)["summary"] == diff_delivery(_digest(cfg), None).summary


def test_a_never_delivered_app_reads_nothing_off_disk(temp_config_env: Path) -> None:
    """`--list` runs on every GUI refresh, per app.

    `find_in_cache` opens every candidate IPA in cache_dir (hundreds of MB of
    zips) and `compute_inputs_digest` hashes every attached tweak. With no
    delivery to compare against, all of that work is thrown away.
    """
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    _stage(cfg)
    log = BuildLog(cfg.state_dir / "log.sqlite")

    with patch.object(cli_mod, "find_in_cache") as find, \
         patch.object(cli_mod, "compute_inputs_digest") as digest:
        assert _state(cfg, log)["kind"] == "never"

    assert not find.called
    assert not digest.called


def test_a_matching_delivery_reads_current(temp_config_env: Path) -> None:
    cfg = load_config(temp_config_env)
    _stage(cfg)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _deliver(cfg, log)

    state = _state(cfg, log)

    assert state["kind"] == "current"
    assert state["reasons"] == []
    assert state["pushed_at"] is not None


def test_a_rebuilt_tweak_reads_stale_and_says_which(temp_config_env: Path) -> None:
    cfg = load_config(temp_config_env)
    _stage(cfg)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _deliver(cfg, log)

    (cfg.tweaks_dir / "some-tweak.deb").write_bytes(b"rebuilt")

    state = _state(cfg, log)

    assert state["kind"] == "stale"
    assert state["reasons"] == ["some-tweak.deb rebuilt since delivery"]


def test_a_delivered_app_with_no_cached_source_reads_unknown(temp_config_env: Path) -> None:
    """Here the missing IPA really is the whole answer: there is a delivery to
    compare against and no way to say what a build would produce now."""
    cfg = load_config(temp_config_env)
    _stage(cfg)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _deliver(cfg, log)
    for ipa in cfg.cache_dir.glob("*.ipa"):
        ipa.unlink()

    state = _state(cfg, log)

    assert state["kind"] == "unknown"
    assert "source ipa" in state["summary"].lower()


def test_a_demoted_livecontainer_delivery_states_the_evidence_not_the_verdict(
    temp_config_env: Path,
) -> None:
    """A livecontainer reconcile only lists Documents/.

    Spec §3: the import step is manual and unverified, and an IPA that was
    imported and then cleaned up is missing from Documents/ too — which makes
    "the phone is out of date" a claim the evidence doesn't support. §6: the
    summary describes the record, not the device.
    """
    cfg = load_config(temp_config_env)
    _stage(cfg)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _deliver(cfg, log, delivery="livecontainer")
    log.mark_reconciled(log.latest_delivery("example")["id"], seen=False)

    assert (
        _state(cfg, log)["summary"]
        == "Our record says delivered; the file is not on the device."
    )


def test_a_demoted_sideload_delivery_keeps_the_definite_wording(
    temp_config_env: Path,
) -> None:
    """The other half of the wording split — and, today, a branch nothing can
    reach in production.

    This docstring used to say §7 routes sideload through
    InstallationProxyService, whose answer is authoritative, so the definite
    wording is warranted. §7.1 retracted that: cyan rewrites the bundle id it
    ships (`new_bundle_id or f"{bundle_id}.plus"` whenever `keep_bundle_id` is
    false, the default) while the row records the *pre-cyan* id, so asking the
    device about it would demote almost every sideload app on a default
    config. `_reconcile_app` therefore returns early on any non-livecontainer
    row, which means `reconcile_ok` is never set for one and this branch is
    dead until a column recording the effective installed bundle id exists.

    The test earns its place anyway: the branch is what the demote wording
    would use the day that column lands, and this pins which of the two
    strings goes with which delivery mode. What it must not be read as is
    evidence that sideload reconcile works.
    """
    cfg = load_config(temp_config_env)
    _stage(cfg)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _deliver(cfg, log, delivery="sideload")
    log.mark_reconciled(log.latest_delivery("example")["id"], seen=False)

    assert _state(cfg, log)["summary"] == "The phone is out of date."


def test_a_delivery_the_device_no_longer_has_reads_stale(temp_config_env: Path) -> None:
    """reconcile_ok == 0 means the file was looked for and wasn't there.

    A digest match can't rescue that: the record describes an artifact the
    phone has lost, so `current` would be a lie about the device.
    """
    cfg = load_config(temp_config_env)
    _stage(cfg)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _deliver(cfg, log)
    log.mark_reconciled(log.latest_delivery("example")["id"], seen=False)

    state = _state(cfg, log)

    assert state["kind"] == "stale"
    assert state["reasons"] == ["last delivery is no longer on the device"]


def test_a_reconciled_present_delivery_still_reads_current(temp_config_env: Path) -> None:
    """The mirror: seen=True must not be read as "checked and missing"."""
    cfg = load_config(temp_config_env)
    _stage(cfg)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _deliver(cfg, log)
    log.mark_reconciled(log.latest_delivery("example")["id"], seen=True)

    assert _state(cfg, log)["kind"] == "current"


def test_an_unparseable_row_reads_unknown_instead_of_raising(temp_config_env: Path) -> None:
    """One bad row must not take down the whole list.

    `diff_delivery` guards its own parse of `tweaks` for exactly this reason,
    but the row is decoded here, outside that guard — so the guard is only as
    good as this one.
    """
    cfg = load_config(temp_config_env)
    _stage(cfg)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _deliver(cfg, log)
    with log._connect() as conn:
        conn.execute("UPDATE deliveries SET tweaks = '{not json'")

    state = _state(cfg, log)

    assert state["kind"] == "unknown"
    assert "can't be read" in state["summary"]


@pytest.mark.parametrize("cell", ['[]', '"oops"', "5"])
def test_a_cyan_cell_that_decodes_to_the_wrong_shape_reads_unknown(
    temp_config_env: Path, cell: str
) -> None:
    """Valid JSON is not the same check as the right shape.

    `diff_delivery` calls `.get()` on whatever comes out of this cell, from
    inside a loop that no guard covers, so a list or a string there is an
    AttributeError that escapes `--list` entirely. Each of these reaches that
    line by a different route, and `5` doesn't even reach `.get` the same way.

    The app has to be stale for any of it to matter: a matching digest returns
    before the cyan comparison, which is why this rebuilds a tweak first.
    """
    cfg = load_config(temp_config_env)
    _stage(cfg)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _deliver(cfg, log)
    (cfg.tweaks_dir / "some-tweak.deb").write_bytes(b"rebuilt")
    with log._connect() as conn:
        conn.execute("UPDATE deliveries SET cyan = ?", (cell,))

    state = _state(cfg, log)

    assert state["kind"] == "unknown"
    assert "can't be read" in state["summary"]


def test_a_corrupt_row_does_not_take_the_whole_list_down(temp_config_env: Path) -> None:
    """End to end, because that is where the cost lands.

    Spec §8: `--list` is the GUI's entire app list, so a failure here costs the
    user the window rather than one badge. Before the shape check this exited
    1 with an AttributeError after emitting the `app` record and nothing else.
    """
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    _stage(cfg)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _deliver(cfg, log)
    (cfg.tweaks_dir / "some-tweak.deb").write_bytes(b"rebuilt")
    with log._connect() as conn:
        conn.execute("UPDATE deliveries SET cyan = '[]'")

    result = CliRunner().invoke(cli_mod.app, ["--json", "--list"])

    assert result.exit_code == 0, result.exception or result.output
    assert [e["type"] for e in _events(result.stdout)] == ["app", "delivery-state"]
    assert _events(result.stdout)[1]["kind"] == "unknown"


def test_a_log_that_cannot_be_queried_reads_unknown_not_never(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Never delivered" is a claim; "the log wouldn't open" is not the same one.

    A locked database during a concurrent build would otherwise paint a red
    badge on every app at once.
    """
    cfg = load_config(temp_config_env)
    _stage(cfg)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _deliver(cfg, log)

    def _locked(self, app: str, *, udid: str | None = None):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(BuildLog, "latest_delivery", _locked)

    assert _state(cfg, log)["kind"] == "unknown"


def test_a_log_that_will_not_open_reads_unknown_not_never(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same claim, one level up: opening the database is what fails here.

    `--list` is the GUI's whole app list and it never touched this database
    before today, so a corrupt log.sqlite must degrade to "no badge" rather
    than either crashing the listing or painting "never delivered" on
    everything.
    """
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    BuildLog(cfg.state_dir / "log.sqlite")

    def _corrupt(self, db_path: Path) -> None:
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(BuildLog, "__init__", _corrupt)
    result = CliRunner().invoke(cli_mod.app, ["--json", "--list"])

    assert result.exit_code == 0, result.output
    states = [e for e in _events(result.stdout) if e["type"] == "delivery-state"]
    assert [s["kind"] for s in states] == ["unknown"]


def test_list_opens_the_delivery_log_once_for_the_whole_pass(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BuildLog.__init__ runs executescript plus two PRAGMAs on the live file.

    `--list` is read-only from the user's point of view and the GUI calls it on
    every refresh; constructing one per app would migrate and write-lock the
    database once per app in the loop.
    """
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod

    add_app(
        temp_config_env,
        "bravo",
        {"bundle_id": "com.bravo.app", "preferred_version": "latest", "tweaks": []},
    )
    cfg = load_config(temp_config_env)
    BuildLog(cfg.state_dir / "log.sqlite")  # the file must exist to be opened

    opened: list[Path] = []
    real_init = BuildLog.__init__

    def _counting_init(self, db_path: Path) -> None:
        opened.append(db_path)
        real_init(self, db_path)

    monkeypatch.setattr(BuildLog, "__init__", _counting_init)
    result = CliRunner().invoke(cli_mod.app, ["--json", "--list"])

    assert result.exit_code == 0, result.output
    assert len([e for e in _events(result.stdout) if e["type"] == "delivery-state"]) == 2
    assert len(opened) == 1


def test_the_human_list_shows_a_delivery_column(temp_config_env: Path) -> None:
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod

    # Wide enough that rich can't wrap the header mid-word.
    result = CliRunner().invoke(cli_mod.app, ["--list"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "delivery" in result.output
    assert "never" in result.output


def test_the_human_list_column_shows_the_state_it_computed(temp_config_env: Path) -> None:
    """Not a constant that happens to read `never` on a fresh fixture.

    The column above is asserted on a config where every app genuinely is
    `never`, so it passes just as well against a hardcoded cell. This one
    delivers an app and then rebuilds one of its tweaks, and refuses `never`
    outright.
    """
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    _stage(cfg)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _deliver(cfg, log)
    (cfg.tweaks_dir / "some-tweak.deb").write_bytes(b"rebuilt")

    result = CliRunner().invoke(cli_mod.app, ["--list"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "stale" in result.output
    assert "never" not in result.output


def test_the_verb_prints_a_line_per_app_without_json(temp_config_env: Path) -> None:
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod

    result = CliRunner().invoke(cli_mod.app, ["--delivery-state"])

    assert result.exit_code == 0, result.output
    assert "example" in result.output
    assert "Never delivered" in result.output


def test_the_verb_prints_the_reasons_it_found(temp_config_env: Path) -> None:
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    _stage(cfg)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _deliver(cfg, log)
    (cfg.tweaks_dir / "some-tweak.deb").write_bytes(b"rebuilt")

    result = CliRunner().invoke(cli_mod.app, ["--delivery-state", "--app", "example"])

    assert result.exit_code == 0, result.output
    assert "some-tweak.deb rebuilt since delivery" in result.output


# --- reconcile: checking the record against a device that is actually there ---
#
# The rule the whole section is built on: reconcile can DEMOTE, never promote.
# A missing file downgrades the state and says so. A present file upgrades
# nothing — presence says nothing about whether LiveContainer ever imported it,
# and the import happens inside another app's private container we deliberately
# do not read (spec §3).
#
# The corollary is that a demote must be on *evidence*. An unreachable device
# is not evidence, and neither is a different phone.


def _lc_deliver(cfg: Config, log: BuildLog, **overrides: object) -> None:
    """A livecontainer delivery row, as the push path writes one."""
    fields: dict = {
        "delivery": "livecontainer",
        "lc_bundle_id": "com.kdt.livecontainer.T",
        "remote_name": "example.ipa",
        "udid": "U",
    }
    fields.update(overrides)
    _deliver(cfg, log, **fields)


def _no_device(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Stub the device listing and return the list of calls made to it."""
    from graft_ipa import cli as cli_mod

    calls: list[dict] = []

    def _listdir(**kw: object) -> list[str]:
        calls.append(kw)
        return []

    monkeypatch.setattr(cli_mod, "list_livecontainer_documents", _listdir)
    return calls


def test_reconcile_demotes_a_missing_file(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _lc_deliver(cfg, log)

    monkeypatch.setattr(cli_mod, "list_livecontainer_documents", lambda **kw: ["other.ipa"])
    cli_mod._reconcile_app(log, "example", udid="U")

    assert log.latest_delivery("example")["reconcile_ok"] == 0


def test_reconcile_marks_a_present_file_seen(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _lc_deliver(cfg, log)

    monkeypatch.setattr(
        cli_mod, "list_livecontainer_documents", lambda **kw: ["example.ipa", "x.ipa"]
    )
    cli_mod._reconcile_app(log, "example", udid="U")

    assert log.latest_delivery("example")["reconcile_ok"] == 1


def test_reconcile_asks_the_device_the_row_names(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The LiveContainer to look inside comes from the row, not from config.

    config.yaml can be edited after a push; the row is what records where the
    file was actually written, and looking anywhere else would report a file
    missing from a container it was never put in.
    """
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _lc_deliver(cfg, log, lc_bundle_id="com.kdt.LiveContainer3.T")
    calls = _no_device(monkeypatch)

    cli_mod._reconcile_app(log, "example", udid="U")

    assert calls == [{"udid": "U", "lc_bundle_id": "com.kdt.LiveContainer3.T"}]


def test_reconcile_is_a_noop_without_a_delivery(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    calls = _no_device(monkeypatch)

    cli_mod._reconcile_app(log, "example", udid="U")

    assert calls == [], "must not talk to the device when there is nothing to check"


def test_an_unreachable_device_changes_nothing(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _lc_deliver(cfg, log)

    def _boom(**kw: object) -> list[str]:
        raise OSError("device not found")

    monkeypatch.setattr(cli_mod, "list_livecontainer_documents", _boom)
    cli_mod._reconcile_app(log, "example", udid="U")

    assert log.latest_delivery("example")["reconcile_ok"] is None


def test_reconcile_skips_a_row_pushed_to_another_phone(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`latest_delivery` returns the newest row whatever device it went to.

    Push to phone A, connect phone B, reconcile — demoting there would paint a
    stale badge for a device that was never the target. A different phone is
    not evidence, so the row is left untouched rather than judged.
    """
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _lc_deliver(cfg, log, udid="PHONE-A")
    calls = _no_device(monkeypatch)

    cli_mod._reconcile_app(log, "example", udid="PHONE-B")

    assert calls == []
    assert log.latest_delivery("example")["reconcile_ok"] is None


def test_reconcile_skips_a_row_with_no_recorded_device(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row that never recorded a udid is unmatchable, not "the one in hand"."""
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _lc_deliver(cfg, log, udid=None)
    calls = _no_device(monkeypatch)

    cli_mod._reconcile_app(log, "example", udid="U")

    assert calls == []
    assert log.latest_delivery("example")["reconcile_ok"] is None


def test_reconcile_skips_when_no_device_was_identified(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`select_device` can hand back a None udid.

    Two unknowns don't make a match: a NULL row udid and an unidentified
    device would compare equal, which is the wrong-phone failure with both
    names removed.
    """
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _lc_deliver(cfg, log, udid=None)
    calls = _no_device(monkeypatch)

    cli_mod._reconcile_app(log, "example", udid=None)

    assert calls == []
    assert log.latest_delivery("example")["reconcile_ok"] is None


def test_reconcile_skips_a_sideload_delivery(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Listing a LiveContainer's Documents/ says nothing about a sideload.

    Not "until InstallationProxyService is wired up", which is what this said
    before spec §7.1: that route asks whether `row["bundle_id"]` is installed,
    and cyan ships a *different* id than the one the row records unless
    `keep_bundle_id` is set. It would answer "not installed" for an app that is
    installed and working, demoting almost every sideload app on a default
    config — a demote with no evidence, the one thing §7 forbids.

    So a sideload row is left alone deliberately and indefinitely, not
    pending a small piece of missing work. Closing it needs a second column
    recording the effective installed bundle id at push time.
    """
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _lc_deliver(cfg, log, delivery="sideload", remote_name="example.ipa")
    calls = _no_device(monkeypatch)

    cli_mod._reconcile_app(log, "example", udid="U")

    assert calls == []
    assert log.latest_delivery("example")["reconcile_ok"] is None


@pytest.mark.parametrize("missing", ["lc_bundle_id", "remote_name"])
def test_reconcile_skips_a_row_that_names_nothing_to_look_for(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """A row from before this feature carries neither field.

    Without the container there is nowhere to look; without the filename the
    membership test is `None in names`, which is False for every device on
    earth — a demote with no evidence behind it at all.
    """
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _lc_deliver(cfg, log, **{missing: None})
    calls = _no_device(monkeypatch)

    cli_mod._reconcile_app(log, "example", udid="U")

    assert calls == []
    assert log.latest_delivery("example")["reconcile_ok"] is None


def test_a_present_file_promotes_nothing(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline rule, end to end.

    The file being in Documents/ says only that: LiveContainer's import is a
    separate manual step this tool cannot observe (§3). A stale record stays
    stale after a successful reconcile.
    """
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    _stage(cfg)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _lc_deliver(cfg, log)
    (cfg.tweaks_dir / "some-tweak.deb").write_bytes(b"rebuilt")

    monkeypatch.setattr(cli_mod, "list_livecontainer_documents", lambda **kw: ["example.ipa"])
    cli_mod._reconcile_app(log, "example", udid="U")

    state = _state(cfg, log)
    assert state["kind"] == "stale"
    assert "some-tweak.deb rebuilt since delivery" in state["reasons"]


# --- the --reconcile verb -----------------------------------------------------


def test_the_reconcile_verb_checks_the_device_then_reports(temp_config_env: Path) -> None:
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    _stage(cfg)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _lc_deliver(cfg, log, udid="00008130-test")

    with patch.object(cli_mod, "select_device", return_value=("00008130-test", "usb")), \
         patch.object(cli_mod, "list_livecontainer_documents", return_value=["nothing.ipa"]):
        result = CliRunner().invoke(cli_mod.app, ["--json", "--reconcile"])

    assert result.exit_code == 0, result.output
    states = [e for e in _events(result.stdout) if e["type"] == "delivery-state"]
    assert [s["kind"] for s in states] == ["stale"]
    assert states[0]["reasons"] == ["last delivery is no longer on the device"]
    assert log.latest_delivery("example")["reconcile_ok"] == 0


def test_the_reconcile_verb_narrows_to_the_named_app(temp_config_env: Path) -> None:
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod

    add_app(
        temp_config_env,
        "bravo",
        {"bundle_id": "com.bravo.app", "preferred_version": "latest", "tweaks": []},
    )
    cfg = load_config(temp_config_env)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _lc_deliver(cfg, log, udid="00008130-test")
    _lc_deliver(cfg, log, app_name="bravo", udid="00008130-test", remote_name="bravo.ipa")

    with patch.object(cli_mod, "select_device", return_value=("00008130-test", "usb")), \
         patch.object(cli_mod, "list_livecontainer_documents", return_value=[]):
        result = CliRunner().invoke(cli_mod.app, ["--reconcile", "--app", "example"])

    assert result.exit_code == 0, result.output
    assert log.latest_delivery("example")["reconcile_ok"] == 0
    assert log.latest_delivery("bravo")["reconcile_ok"] is None


def test_the_reconcile_verb_reports_the_local_record_with_no_device(
    temp_config_env: Path,
) -> None:
    """§7 is best-effort throughout: an unreachable device leaves the record
    untouched and the UI showing the local answer. Failing outright would cost
    the caller the state report as well as the check it couldn't make.

    The reason rides along as a `log` event. It used to be an `err_console`
    line and nothing else, on a verb that exits 0 — so the GUI, which reads
    stderr only off a non-zero exit, showed a `delivery-state` with no hint
    that no device had been consulted to produce it."""
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    _stage(cfg)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _lc_deliver(cfg, log)

    with patch.object(cli_mod, "select_device", side_effect=InstallError("no iOS devices connected")):
        result = CliRunner().invoke(cli_mod.app, ["--json", "--reconcile"])

    assert result.exit_code == 0, result.output
    events = _events(result.stdout)
    assert [e["type"] for e in events] == ["log", "delivery-state"]
    assert events[0]["source"] == "warning"
    assert "no device checked" in events[0]["line"]
    assert log.latest_delivery("example")["reconcile_ok"] is None


def test_the_reconcile_verb_survives_a_log_it_cannot_write(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mark_reconciled` writes to the database a concurrent build is also
    writing to, so "database is locked" is a live possibility. Losing the
    state report over failed bookkeeping is the wrong trade.

    The bookkeeping failure still has to be *sayable*, though, which on a verb
    that exits 0 means a `log` event and not a bare stderr line."""
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    _stage(cfg)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _lc_deliver(cfg, log, udid="00008130-test")

    monkeypatch.setattr(
        BuildLog, "mark_reconciled",
        lambda *a, **kw: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
    )
    with patch.object(cli_mod, "select_device", return_value=("00008130-test", "usb")), \
         patch.object(cli_mod, "list_livecontainer_documents", return_value=[]):
        result = CliRunner().invoke(cli_mod.app, ["--json", "--reconcile"])

    assert result.exit_code == 0, result.output
    events = _events(result.stdout)
    assert [e["type"] for e in events] == ["log", "delivery-state"]
    assert events[0]["source"] == "warning"
    assert "database is locked" in events[0]["line"]


def test_the_reconcile_verb_rejects_an_unknown_app_before_touching_a_device(
    temp_config_env: Path,
) -> None:
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod

    # The log has to exist, or the reconcile pass short-circuits before it
    # would have resolved a device and the guard under test is unreachable.
    cfg = load_config(temp_config_env)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _lc_deliver(cfg, log)

    with patch.object(cli_mod, "select_device", side_effect=AssertionError("resolved a device")):
        result = CliRunner().invoke(cli_mod.app, ["--reconcile", "--app", "nope"])

    assert result.exit_code != 0
    assert "nope" in result.output + str(result.exception)
    assert "resolved a device" not in str(result.exception)


# --- a reinstall is a third push ---------------------------------------------


def test_a_reinstalled_archive_never_reads_current(temp_config_env: Path) -> None:
    """`--reinstall-build` ships an archive whose inputs were never recorded.

    `compute_inputs_digest` hashes the tweak files as they are NOW, so the row
    carries no digest at all. Reporting `unknown` for it would render as no
    badge (§6) and leave the user believing the phone matches the config it is
    looking at; over-reporting work to do is the mild failure, under-reporting
    it is the one this feature exists to stop.
    """
    cfg = load_config(temp_config_env)
    _stage(cfg)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _lc_deliver(cfg, log, inputs_digest=None, digest_version=None, cyan=None, origin="reinstall")

    state = _state(cfg, log)

    assert state["kind"] == "stale"
    assert "archived build" in state["summary"]


def test_a_reinstall_row_outranks_a_digest_that_would_match(temp_config_env: Path) -> None:
    """Belt and braces on the branch order.

    A reinstall row carrying a digest is not something the writer produces, but
    the guard has to sit ahead of the comparison rather than after it: a row
    that reaches `diff_delivery` with a matching digest reads `current`, which
    is the exact claim a reinstall cannot support.
    """
    cfg = load_config(temp_config_env)
    _stage(cfg)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _lc_deliver(cfg, log, origin="reinstall")

    assert _state(cfg, log)["kind"] == "stale"


def test_a_normal_push_is_not_read_as_a_reinstall(temp_config_env: Path) -> None:
    """The mirror: the guard must key on what the reinstall path writes, not
    fire for every row and demote a delivery that is genuinely current."""
    cfg = load_config(temp_config_env)
    _stage(cfg)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _lc_deliver(cfg, log)

    assert _state(cfg, log)["kind"] == "current"
