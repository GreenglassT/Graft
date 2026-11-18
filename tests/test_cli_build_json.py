from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from graft_ipa.config import AppConfig, Config, CyanOptions, SigningConfig
from graft_ipa.json_output import JsonEmitter


def _stub_config(tmp_path: Path) -> Config:
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
        tweaks_dir=tmp_path / "tweaks",
        state_dir=tmp_path / "state",
        apps={
            "yt": AppConfig(
                name="yt",
                bundle_id="com.example.app",
                preferred_version="latest",
                tweaks=["a.deb"],
                cyan=CyanOptions(),
                delivery="livecontainer",
            )
        },
    )


def test_build_emits_stage_events_in_order_via_json_mode(tmp_path: Path, capsys) -> None:
    from graft_ipa import cli as cli_mod

    cfg = _stub_config(tmp_path)
    em = JsonEmitter()

    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "acquire_ipa") as acquire, \
         patch.object(cli_mod, "extract_ipa"), \
         patch.object(cli_mod, "verify_decrypted", return_value=[]), \
         patch.object(cli_mod, "run_cyan") as cyan, \
         patch.object(cli_mod, "select_device", return_value=("00008130-test-udid", "usb")), \
         patch.object(cli_mod, "lc_runner") as lc:
        acquire.return_value.path = tmp_path / "in.ipa"
        acquire.return_value.version = "1.0"
        cyan.return_value.output_ipa = tmp_path / "tweaked.ipa"
        cyan.return_value.output_ipa.parent.mkdir(parents=True, exist_ok=True)
        cyan.return_value.output_ipa.write_bytes(b"x")
        lc.push_ipa_to_livecontainer.return_value.size_bytes = 1024
        lc.push_ipa_to_livecontainer.return_value.bundle_id = "com.kdt.livecontainer"
        lc.push_ipa_to_livecontainer.return_value.documents_path = "Documents/yt.ipa"
        lc.push_ipa_to_livecontainer.return_value.others = []

        from graft_ipa.build_log import BuildLog
        log = BuildLog(tmp_path / "log.sqlite")
        cli_mod._build_one(
            cfg=cfg, log=log, app_name="yt", dry_run=False,
            skip_install=False,
            skip_decrypt_check=False, verbose=False, device=None,
            emitter=em,
        )

    out = capsys.readouterr().out
    events = [json.loads(line) for line in out.splitlines() if line.strip().startswith("{")]
    types_seen = [e.get("type") for e in events]
    assert "build-start" in types_seen
    assert types_seen.count("stage") >= 4
    assert "build-end" in types_seen
    stage_names = [e.get("name") for e in events if e["type"] == "stage"]
    assert "acquire" in stage_names
    assert "cyan" in stage_names


def test_encrypted_binaries_reach_the_event_stream(tmp_path: Path, capsys) -> None:
    """The cryptid-0 failure had no machine-readable form.

    The detail went out through `console.print` — on the same stdout as the
    JSON stream, where `BuildEvent.decode` throws on the line and the Swift
    decoder swallows it with `try?` — and the only structured trace was
    `bad=<n>` among the `stage` event's extra fields, which `BuildEvent.progress`
    explicitly drops. The GUI showed a fully green build; the user pushed an IPA
    with an encrypted binary and the app crashed on launch with nothing on
    screen explaining why.
    """
    from graft_ipa import cli as cli_mod
    from graft_ipa.ipa_inspect import CryptidResult

    cfg = _stub_config(tmp_path)
    em = JsonEmitter()

    def _one_encrypted(extract_dir: Path) -> list[CryptidResult]:
        # Built from the directory the caller passes so the reported path is
        # relative to the real extract dir, as the production code renders it.
        return [
            CryptidResult(binary=extract_dir / "Payload/YT.app/YT", cryptids=[1]),
            CryptidResult(binary=extract_dir / "Payload/YT.app/Frameworks/ok.dylib",
                          cryptids=[0]),
        ]

    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "acquire_ipa") as acquire, \
         patch.object(cli_mod, "extract_ipa"), \
         patch.object(cli_mod, "verify_decrypted", side_effect=_one_encrypted), \
         patch.object(cli_mod, "run_cyan") as cyan:
        acquire.return_value.path = tmp_path / "in.ipa"
        acquire.return_value.version = "1.0"
        cyan.return_value.output_ipa = tmp_path / "tweaked.ipa"
        cyan.return_value.output_ipa.parent.mkdir(parents=True, exist_ok=True)
        cyan.return_value.output_ipa.write_bytes(b"x")
        cyan.return_value.stdout = ""
        cyan.return_value.stderr = ""

        from graft_ipa.build_log import BuildLog
        log = BuildLog(tmp_path / "log.sqlite")
        cli_mod._build_one(
            cfg=cfg, log=log, app_name="yt", dry_run=False,
            skip_install=True,
            skip_decrypt_check=False, verbose=False, device=None,
            emitter=em,
        )

    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.out.splitlines()
              if line.strip().startswith("{")]

    bad = [e for e in events if e["type"] == "encrypted-binaries"]
    assert len(bad) == 1, "the one machine-readable signal that a binary is encrypted"
    assert bad[0]["app"] == "yt"
    assert bad[0]["count"] == 1
    assert bad[0]["binaries"] == [
        {"path": "Payload/YT.app/YT", "cryptids": [1]}
    ], "the offending binary, named, not just counted"

    # And on the channel a consumer that has never heard of the event still
    # reads — the same two-channel rule `_do_devices` follows.
    assert "still encrypted" in captured.err

    # Still a warning, not a failure: `--skip-decrypt-check` exists, and the
    # pipeline deliberately carries on.
    end = next(e for e in events if e["type"] == "build-end")
    assert end["ok"] is True
