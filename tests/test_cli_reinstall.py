from __future__ import annotations

import json
import sqlite3
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from graft_ipa.build_log import BuildLog
from graft_ipa.config import load_config
from graft_ipa.errors import GraftError
from graft_ipa.json_output import JsonEmitter


def _seed_app_in_config(
    cfg_path: Path, app_name: str, delivery: str, *, lc_bundle_id: str | None = None
) -> None:
    """Append an app entry to the test config so _do_reinstall_build can resolve delivery."""
    import yaml
    data = yaml.safe_load(cfg_path.read_text()) or {}
    entry = {
        "bundle_id": f"com.example.{app_name}",
        "preferred_version": "1.0",
        "delivery": delivery,
        "tweaks": [],
        "cyan": {},
    }
    if lc_bundle_id is not None:
        entry["lc_bundle_id"] = lc_bundle_id
    data.setdefault("apps", {})[app_name] = entry
    cfg_path.write_text(yaml.safe_dump(data))


def _seed_build(
    cfg, app: str, ipa_path: Path | None, success: bool = True,
    target: str = "device",
) -> int:
    log = BuildLog(cfg.state_dir / "log.sqlite")
    # The delivery the app has now, as the build path records it: these rows
    # stand for builds made in the app's current mode. A row from before the
    # column existed, or one made in the other mode, is
    # tests/test_reinstall_delivery.py's business.
    log.record(
        app=app, bundle_id=f"com.example.{app}", version="1.0",
        tweaks=[], ipa_path=ipa_path,
        signing_team="TEAM", udid="00008130-x", success=success,
        target=target,
        delivery=cfg.apps[app].delivery if app in cfg.apps else None,
    )
    import sqlite3
    with sqlite3.connect(cfg.state_dir / "log.sqlite") as conn:
        row = conn.execute("SELECT MAX(id) FROM builds").fetchone()
    return int(row[0])


def test_reinstall_livecontainer_emits_lc_push_stage(
    temp_config_env: Path, tmp_path: Path
) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa.lc_runner import LCDeliveryResult

    _seed_app_in_config(temp_config_env, "echo", delivery="livecontainer")
    cfg = load_config(temp_config_env)
    ipa = tmp_path / "echo.ipa"
    ipa.write_bytes(b"x" * 1024)
    build_id = _seed_build(cfg, "echo", ipa)

    # The real dataclass, not an ad-hoc stand-in: the reinstall path reads
    # `documents_path` to record what it put on the phone, and a fake that
    # carries fewer fields than lc_runner returns tests a narrower call than
    # the one the code makes.
    fake_lc_result = LCDeliveryResult(
        udid="00008130-test",
        bundle_id="com.kdt.livecontainer.X",
        documents_path="Documents/echo.ipa",
        size_bytes=1024,
    )

    buf = StringIO()
    with patch.object(cli_mod, "select_device", return_value=("00008130-test", "usb")), \
         patch.object(cli_mod.lc_runner, "push_ipa_to_livecontainer", return_value=fake_lc_result):
        cli_mod._do_reinstall_build(cfg, build_id, emitter=JsonEmitter(stream=buf))

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    types = [e["type"] for e in events]
    assert "build-start" in types
    assert any(e["type"] == "stage" and e["name"] == "lc-push" and e["status"] == "start" for e in events)
    assert any(e["type"] == "stage" and e["name"] == "lc-push" and e["status"] == "end" for e in events)
    end = next(e for e in events if e["type"] == "build-end")
    assert end["ok"] is True


def test_reinstall_sideload_emits_install_stage(
    temp_config_env: Path, tmp_path: Path
) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa.installer import InstallResult

    _seed_app_in_config(temp_config_env, "example", delivery="sideload")
    cfg = load_config(temp_config_env)
    ipa = tmp_path / "example.ipa"
    ipa.write_bytes(b"x" * 1024)
    build_id = _seed_build(cfg, "example", ipa)

    buf = StringIO()
    fake_install = InstallResult(udid="00008130-test", stdout="Install: Complete", stderr="")
    with patch.object(cli_mod, "select_device", return_value=("00008130-test", "usb")), \
         patch.object(cli_mod, "install", return_value=fake_install):
        cli_mod._do_reinstall_build(cfg, build_id, emitter=JsonEmitter(stream=buf))

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    assert any(e["type"] == "stage" and e["name"] == "install" and e["status"] == "start" for e in events)
    assert any(e["type"] == "stage" and e["name"] == "install" and e["status"] == "end" for e in events)
    end = next(e for e in events if e["type"] == "build-end")
    assert end["ok"] is True


def test_reinstall_rejects_failed_build(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    _seed_app_in_config(temp_config_env, "broken", delivery="sideload")
    cfg = load_config(temp_config_env)
    build_id = _seed_build(cfg, "broken", ipa_path=None, success=False)

    with pytest.raises(GraftError, match="did not produce a signed IPA"):
        cli_mod._do_reinstall_build(cfg, build_id)


def test_reinstall_rejects_missing_ipa(temp_config_env: Path, tmp_path: Path) -> None:
    from graft_ipa import cli as cli_mod

    _seed_app_in_config(temp_config_env, "ghost", delivery="sideload")
    cfg = load_config(temp_config_env)
    ipa = tmp_path / "ghost.ipa"
    ipa.write_bytes(b"x")
    build_id = _seed_build(cfg, "ghost", ipa)
    ipa.unlink()

    with pytest.raises(GraftError, match="archived IPA missing"):
        cli_mod._do_reinstall_build(cfg, build_id)


def test_reinstall_rejects_unknown_build_id(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    BuildLog(cfg.state_dir / "log.sqlite")  # create empty db
    with pytest.raises(GraftError, match="no build with id"):
        cli_mod._do_reinstall_build(cfg, 999)


def _delivery_row(cfg, app: str = "echo"):
    return BuildLog(cfg.state_dir / "log.sqlite").latest_delivery(app)


def test_a_reinstall_records_a_delivery_marked_as_one(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """A reinstall is a third push, and the table is the record of what is on
    the phone. Leaving it out let the newest build's row keep describing a
    device that now holds an older artifact — a `current` badge for something
    that is not there.

    It carries no digest: `compute_inputs_digest` hashes the tweak files as
    they are now, not the ones inside that archive, so a digest computed here
    would describe a build that was never pushed.
    """
    from graft_ipa import cli as cli_mod
    from graft_ipa.lc_runner import LCDeliveryResult

    _seed_app_in_config(temp_config_env, "echo", delivery="livecontainer")
    cfg = load_config(temp_config_env)
    ipa = tmp_path / "echo.ipa"
    ipa.write_bytes(b"x" * 1024)
    build_id = _seed_build(cfg, "echo", ipa)

    lc_result = LCDeliveryResult(
        udid="00008130-test",
        bundle_id="com.kdt.livecontainer.X",
        documents_path="Documents/echo.ipa",
        size_bytes=1024,
    )
    with patch.object(cli_mod, "select_device", return_value=("00008130-test", "usb")), \
         patch.object(cli_mod.lc_runner, "push_ipa_to_livecontainer", return_value=lc_result):
        cli_mod._do_reinstall_build(cfg, build_id)

    row = _delivery_row(cfg)
    assert row is not None
    assert row["origin"] == "reinstall"
    assert row["inputs_digest"] is None
    assert row["remote_name"] == "echo.ipa"
    assert row["lc_bundle_id"] == "com.kdt.livecontainer.X"
    assert row["udid"] == "00008130-test"
    assert row["source_version"] == "1.0"


def test_a_sideload_reinstall_records_a_delivery_too(
    temp_config_env: Path, tmp_path: Path
) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa.installer import InstallResult

    _seed_app_in_config(temp_config_env, "example", delivery="sideload")
    cfg = load_config(temp_config_env)
    ipa = tmp_path / "example.ipa"
    ipa.write_bytes(b"x" * 1024)
    build_id = _seed_build(cfg, "example", ipa)

    with patch.object(cli_mod, "select_device", return_value=("00008130-test", "usb")), \
         patch.object(cli_mod, "install",
                      return_value=InstallResult(udid="00008130-test", stdout="", stderr="")):
        cli_mod._do_reinstall_build(cfg, build_id)

    row = _delivery_row(cfg, "example")
    assert row is not None
    assert row["origin"] == "reinstall"
    assert row["delivery"] == "sideload"
    # Nothing to look for in a LiveContainer, and reconcile must not go
    # looking: these are what tell it so.
    assert row["lc_bundle_id"] is None
    assert row["remote_name"] is None


def test_a_reinstall_that_fails_records_no_delivery(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """Same discipline as the build path: a push that throws reached nothing."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.lc_runner import LiveContainerError

    _seed_app_in_config(temp_config_env, "echo", delivery="livecontainer")
    cfg = load_config(temp_config_env)
    ipa = tmp_path / "echo.ipa"
    ipa.write_bytes(b"x" * 1024)
    build_id = _seed_build(cfg, "echo", ipa)

    with patch.object(cli_mod, "select_device", return_value=("00008130-test", "usb")), \
         patch.object(cli_mod.lc_runner, "push_ipa_to_livecontainer",
                      side_effect=LiveContainerError("phone went away")):
        with pytest.raises(GraftError):
            cli_mod._do_reinstall_build(cfg, build_id)

    assert _delivery_row(cfg) is None


def test_a_recording_failure_does_not_fail_a_reinstall(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """The artifact already reached the phone; losing the row costs a spurious
    badge, and failing the command would cost the user a working install."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.lc_runner import LCDeliveryResult

    _seed_app_in_config(temp_config_env, "echo", delivery="livecontainer")
    cfg = load_config(temp_config_env)
    ipa = tmp_path / "echo.ipa"
    ipa.write_bytes(b"x" * 1024)
    build_id = _seed_build(cfg, "echo", ipa)

    lc_result = LCDeliveryResult(
        udid="00008130-test", bundle_id="com.kdt.livecontainer.X",
        documents_path="Documents/echo.ipa", size_bytes=1024,
    )
    with patch.object(cli_mod, "select_device", return_value=("00008130-test", "usb")), \
         patch.object(cli_mod.lc_runner, "push_ipa_to_livecontainer", return_value=lc_result), \
         patch.object(BuildLog, "record_delivery", side_effect=sqlite3.OperationalError("locked")):
        cli_mod._do_reinstall_build(cfg, build_id)

    assert _delivery_row(cfg) is None


# --- the envelope: a re-install that cannot start still ends ----------------


def _terminal_events(buf: StringIO) -> list[dict]:
    return [
        e for e in (json.loads(line) for line in buf.getvalue().splitlines() if line.strip())
        if e["type"] in ("build-start", "build-end")
    ]


def test_a_device_failure_before_the_push_still_emits_start_and_end(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """`select_device` raised outside the try and ahead of `build-start`.

    Every pre-push guard did: the archived IPA check, the config lookup, and
    this one. The result was no `build-start` and no `build-end` at all — the
    Re-install sheet showed "Failed — exit 8" over an empty log panel, with the
    reason only on stderr where that sheet never looks.
    """
    from graft_ipa import cli as cli_mod
    from graft_ipa.errors import InstallError

    _seed_app_in_config(temp_config_env, "echo", delivery="livecontainer")
    cfg = load_config(temp_config_env)
    ipa = tmp_path / "echo.ipa"
    ipa.write_bytes(b"x" * 1024)
    build_id = _seed_build(cfg, "echo", ipa)

    buf = StringIO()
    with patch.object(
        cli_mod, "select_device", side_effect=InstallError("no device connected")
    ):
        with pytest.raises(InstallError):
            cli_mod._do_reinstall_build(cfg, build_id, emitter=JsonEmitter(stream=buf))

    events = _terminal_events(buf)
    assert [e["type"] for e in events] == ["build-start", "build-end"]
    assert events[1]["ok"] is False
    assert "no device connected" in events[1]["error"]


def test_a_missing_archive_is_reported_through_the_event_stream(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """The other guard that used to raise ahead of `build-start`."""
    from graft_ipa import cli as cli_mod

    _seed_app_in_config(temp_config_env, "ghost", delivery="sideload")
    cfg = load_config(temp_config_env)
    ipa = tmp_path / "ghost.ipa"
    ipa.write_bytes(b"x")
    build_id = _seed_build(cfg, "ghost", ipa)
    ipa.unlink()

    buf = StringIO()
    with pytest.raises(GraftError):
        cli_mod._do_reinstall_build(cfg, build_id, emitter=JsonEmitter(stream=buf))

    events = _terminal_events(buf)
    assert [e["type"] for e in events] == ["build-start", "build-end"]
    assert "archived IPA missing" in events[1]["error"]


def test_an_app_dropped_from_config_is_reported_through_the_event_stream(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """The build row names the app; config.yaml no longer does.

    `build-start` still goes out — it carries a null delivery, which is exactly
    what is known — so the sheet has something to end.
    """
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)  # no app seeded
    ipa = tmp_path / "gone.ipa"
    ipa.write_bytes(b"x" * 16)
    build_id = _seed_build(cfg, "gone", ipa)

    buf = StringIO()
    with pytest.raises(GraftError):
        cli_mod._do_reinstall_build(cfg, build_id, emitter=JsonEmitter(stream=buf))

    events = _terminal_events(buf)
    assert [e["type"] for e in events] == ["build-start", "build-end"]
    assert events[0]["delivery"] is None
    assert "no longer in config.yaml" in events[1]["error"]


def test_a_reinstall_pushes_into_the_pinned_livecontainer(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """Re-install ignored the app's `lc_bundle_id:` pin that the build path honours.

    With two LiveContainers installed, the archive went into whichever one
    `push_ipa_to_livecontainer` defaults to — not the one the build went to.
    The push reported success and a delivery was recorded against the wrong
    container, so the user opened the LiveContainer they always use and the IPA
    simply was not there.
    """
    from graft_ipa import cli as cli_mod
    from graft_ipa.lc_runner import LCDeliveryResult

    pin = "com.kdt.livecontainer.SideStore"
    _seed_app_in_config(
        temp_config_env, "echo", delivery="livecontainer", lc_bundle_id=pin
    )
    cfg = load_config(temp_config_env)
    ipa = tmp_path / "echo.ipa"
    ipa.write_bytes(b"x" * 1024)
    build_id = _seed_build(cfg, "echo", ipa)

    lc_result = LCDeliveryResult(
        udid="00008130-test", bundle_id=pin,
        documents_path="Documents/echo.ipa", size_bytes=1024,
    )
    with patch.object(cli_mod, "select_device", return_value=("00008130-test", "usb")), \
         patch.object(
             cli_mod.lc_runner, "push_ipa_to_livecontainer", return_value=lc_result
         ) as push:
        cli_mod._do_reinstall_build(cfg, build_id)

    assert push.call_args.kwargs["lc_bundle_id"] == pin


def test_a_reinstall_may_overwrite_only_what_this_device_recorded(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """The push refuses to replace a same-named file it has no record of; the
    re-install hands it the records for the device it is pushing to."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.build_log import BuildLog
    from graft_ipa.lc_runner import LCDeliveryResult

    _seed_app_in_config(temp_config_env, "echo", delivery="livecontainer")
    cfg = load_config(temp_config_env)
    ipa = tmp_path / "echo.ipa"
    ipa.write_bytes(b"x" * 1024)
    build_id = _seed_build(cfg, "echo", ipa)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    for udid in ("00008130-test", "another-phone"):
        log.record_delivery(
            app="echo", bundle_id="com.echo.app", inputs_digest=None, digest_version=None,
            cyan=None, tweaks=[], source_version="1", ipa_sha256=None, delivery="livecontainer",
            lc_bundle_id=f"LC-{udid}", remote_name="echo.ipa", udid=udid,
        )

    lc_result = LCDeliveryResult(
        udid="00008130-test", bundle_id="LC-00008130-test",
        documents_path="Documents/echo.ipa", size_bytes=1024,
    )
    with patch.object(cli_mod, "select_device", return_value=("00008130-test", "usb")), \
         patch.object(
             cli_mod.lc_runner, "push_ipa_to_livecontainer", return_value=lc_result
         ) as push:
        cli_mod._do_reinstall_build(cfg, build_id)

    assert push.call_args.kwargs["ours"] == {"LC-00008130-test": {"echo.ipa"}}


class TestAMacBuildIsNotAPhoneBuild:
    """`--reinstall-build <id>` re-ships an archive to a phone, and the id comes
    from a person reading the `--builds` table. A `--for-playcover` archive has
    its tweak dylib retargeted at a bundled stub Preferences.framework; iOS has
    the real one, so pushing it breaks the tweak silently.

    `main` refuses that combination on the run that builds it. This door was
    safe only by absence until builds started recording `target`, so both
    directions are pinned here: refusing everything would break the verb, and
    refusing nothing is the defect.
    """

    def _cfg_and_ipa(self, temp_config_env: Path, tmp_path: Path):
        _seed_app_in_config(temp_config_env, "example", delivery="sideload")
        cfg = load_config(temp_config_env)
        ipa = tmp_path / "example_1.0_20260818T134203.ipa"
        ipa.write_bytes(b"x" * 1024)
        return cfg, ipa

    def test_a_mac_build_is_refused(self, temp_config_env: Path, tmp_path: Path) -> None:
        from graft_ipa import cli as cli_mod
        from graft_ipa.installer import InstallResult

        cfg, ipa = self._cfg_and_ipa(temp_config_env, tmp_path)
        build_id = _seed_build(cfg, "example", ipa, target="mac")

        fake = InstallResult(udid="00008130-test", stdout="", stderr="")
        with patch.object(cli_mod, "select_device", return_value=("00008130-test", "usb")), \
             patch.object(cli_mod, "install", return_value=fake) as install:
            with pytest.raises(GraftError) as e:
                cli_mod._do_reinstall_build(cfg, build_id)

        assert str(build_id) in e.value.message, "the row the user picked, by id"
        assert "this Mac" in e.value.message
        # The action that *is* correct for this artifact, by the name
        # --install-to-mac resolves: a bare filename in built_dir.
        assert f"--install-to-mac {ipa.name}" in (e.value.hint or "")
        # Refused before anything reaches the phone.
        install.assert_not_called()

    def test_a_device_build_still_reinstalls(
        self, temp_config_env: Path, tmp_path: Path
    ) -> None:
        """The other direction. A guard that refuses every row would satisfy the
        test above and delete the feature."""
        from graft_ipa import cli as cli_mod
        from graft_ipa.installer import InstallResult

        cfg, ipa = self._cfg_and_ipa(temp_config_env, tmp_path)
        build_id = _seed_build(cfg, "example", ipa, target="device")

        fake = InstallResult(udid="00008130-test", stdout="Install: Complete", stderr="")
        with patch.object(cli_mod, "select_device", return_value=("00008130-test", "usb")), \
             patch.object(cli_mod, "install", return_value=fake) as install:
            cli_mod._do_reinstall_build(cfg, build_id)

        assert install.called

    def test_a_row_from_before_the_column_still_reinstalls(
        self, temp_config_env: Path, tmp_path: Path
    ) -> None:
        """NULL means a build made before Mac builds existed, which is a phone
        build — every row in an existing user's log is one, so a guard that
        refused them would break re-install for everybody who upgrades.

        This does not pin the *reader*: a raw `row["target"] == "mac"` passes it
        too, measured. It pins the answer, which is what a user with an old log
        experiences."""
        from graft_ipa import cli as cli_mod
        from graft_ipa.installer import InstallResult

        cfg, ipa = self._cfg_and_ipa(temp_config_env, tmp_path)
        build_id = _seed_build(cfg, "example", ipa)
        with sqlite3.connect(cfg.state_dir / "log.sqlite") as conn:
            conn.execute("UPDATE builds SET target = NULL WHERE id = ?", (build_id,))

        fake = InstallResult(udid="00008130-test", stdout="Install: Complete", stderr="")
        with patch.object(cli_mod, "select_device", return_value=("00008130-test", "usb")), \
             patch.object(cli_mod, "install", return_value=fake) as install:
            cli_mod._do_reinstall_build(cfg, build_id)

        assert install.called


def test_reinstalling_a_sideloaded_livecontainer_build_is_refused(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """--reinstall-build re-ships an archived IPA without rebuilding, so it
    skipped `_build_one` and with it the legacy refusal: an old zsign-signed
    LiveContainer archive could be put straight back on a phone."""
    import yaml

    from graft_ipa import cli as cli_mod

    data = yaml.safe_load(temp_config_env.read_text()) or {}
    data.setdefault("apps", {})["livecontainer"] = {
        "bundle_id": "com.kdt.livecontainer", "preferred_version": "1.0",
        "delivery": "sideload", "tweaks": [], "cyan": {},
    }
    temp_config_env.write_text(yaml.safe_dump(data))
    cfg = load_config(temp_config_env)
    ipa = tmp_path / "livecontainer.ipa"
    ipa.write_bytes(b"x" * 1024)
    build_id = _seed_build(cfg, "livecontainer", ipa)

    buf = StringIO()
    with patch.object(cli_mod, "install") as install, \
         patch.object(cli_mod, "select_device", return_value=("u", "usb")):
        with pytest.raises(GraftError) as exc:
            cli_mod._do_reinstall_build(cfg, build_id, emitter=JsonEmitter(stream=buf))

    assert exc.value.message.startswith(
        "livecontainer: building LiveContainer through the sideload path"
    )
    install.assert_not_called()
    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    end = next(e for e in events if e["type"] == "build-end")
    assert end["ok"] is False, "refused inside the envelope, so the GUI sees a build-end"


def test_a_reinstall_holds_its_archive_until_it_is_delivered(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """`--reinstall-build` re-ships an archive of any age, which at
    `archive_keep: 0` is exactly the one a sweep would take. It shares the
    retention lock from before it opens the archive until it is delivered, so
    a sweep in another process mid-push is refused."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.lc_runner import LCDeliveryResult

    from .retention_procs import sweep_from_another_process

    _seed_app_in_config(temp_config_env, "echo", delivery="livecontainer")
    cfg = load_config(temp_config_env)
    cfg.built_dir.mkdir(parents=True, exist_ok=True)
    archive = cfg.built_dir / "echo_1.0_20260101T000000.ipa"
    archive.write_bytes(b"x" * 1024)
    build_id = _seed_build(cfg, "echo", archive)
    trash = tmp_path / "Trash"
    seen: dict = {}

    def push(ipa, **_):
        seen["sweep"] = sweep_from_another_process(cfg, trash=trash, home=tmp_path)
        seen["archive_there"] = archive.exists()
        return LCDeliveryResult(udid="00008130-test", bundle_id="com.kdt.livecontainer",
                                documents_path="Documents/echo.ipa", size_bytes=1024)

    with patch.object(cli_mod, "select_device", return_value=("00008130-test", "usb")), \
         patch.object(cli_mod.lc_runner, "push_ipa_to_livecontainer", side_effect=push):
        cli_mod._do_reinstall_build(cfg, build_id, emitter=JsonEmitter(stream=StringIO()))

    sweep = seen["sweep"]
    assert sweep.returncode == 1, sweep.stdout + sweep.stderr
    assert "refused: a build is delivering" in sweep.stdout, sweep.stdout
    assert seen["archive_there"], "another process trashed the archive mid-delivery"
    assert archive.exists()
