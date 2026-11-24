"""A sideload build warns, before zsign runs, about capabilities the app asks
for and its signing profile can't grant: in a `--dry-run` and in a real
build's stage 4, as the `log`/`warning` event the app's build panel shows.

From the device test of the release build: an app whose binary asks for an
app group, signed with the wildcard profile, died at launch with SIGTRAP,
and nothing had said a word. The real `run_zsign` runs here, with zsign
itself faked underneath it; the IPA, the identity and the profile are made
at test time.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from graft_ipa import capabilities, signer, tools
from graft_ipa import cli as cli_mod
from graft_ipa.config import SigningConfig
from graft_ipa.cyan_runner import CyanResult

from .signing_fixtures import PASSWORD, TEAM, make_identity
from .test_capabilities import APP_GROUPS, GROUP, REWRITTEN, _lzma_ipa, make_ipa, profile_granting
from .test_cli_build_json import _stub_config
from .tool_fakes import fake_path

(WARNING,) = capabilities.warnings("example", "example", [capabilities.Gap("App Groups", (GROUP,))])


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(tools, "which", fake_path(tmp_path / "path-bin"))
    monkeypatch.setenv("TEST_P12_PASS", PASSWORD)


def _cfg(tmp_path: Path, *, profile: Path | None = None, delivery: str = "sideload"):
    ident = make_identity(tmp_path / "identity")
    cfg = _stub_config(tmp_path)
    cfg.signing = SigningConfig(
        cert=ident.p12, cert_password_env="TEST_P12_PASS",
        profile=profile or ident.profile, team_id=TEAM,
    )
    cfg.apps["example"].delivery = delivery
    return cfg


def _build(cfg, ipa: Path, *, dry_run: bool, capsys) -> tuple[int, list[dict], list[str]]:
    """Build `example` from `ipa`. Returns the exit code, the JSON events and
    the order things happened in: each warning's text, and "zsign"."""
    order: list[str] = []
    real_warn = cli_mod._warn

    def warn(emitter, message, **kw):
        order.append(message)
        return real_warn(emitter, message, **kw)

    def cyan(cfg, app, src, out, *, dry_run=False):
        if not dry_run:
            shutil.copyfile(src, out)
        return CyanResult(output_ipa=out, stdout="", stderr="")

    def zsign(cmd, **_):
        order.append("zsign")
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"signed")
        return subprocess.CompletedProcess(cmd, 0, stdout=">>> Signed OK!\n", stderr="")

    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "acquire_ipa") as acquire, \
         patch.object(cli_mod, "run_cyan", side_effect=cyan), \
         patch.object(cli_mod, "_warn", side_effect=warn), \
         patch.object(signer.subprocess, "run", side_effect=zsign), \
         patch.object(cli_mod.lc_runner, "push_ipa_to_livecontainer",
                      side_effect=AssertionError("pushed")):
        acquire.return_value.path = ipa
        acquire.return_value.version = "1.0"
        code = cli_mod._verb_build_apps(
            cfg=cfg, apps=["example"], dry_run=dry_run, skip_install=True,
            for_playcover=False, skip_decrypt_check=True, verbose=False, device=None,
            json_mode=True,
        )
    out = capsys.readouterr()
    events = [json.loads(line) for line in out.out.splitlines() if line.strip()]
    assert code == 0, out.err
    return code, events, order


def _warnings(events: list[dict]) -> list[str]:
    return [e["line"] for e in events if e["type"] == "log" and e.get("source") == "warning"]


def _capability_warnings(events: list[dict]) -> list[str]:
    return [line for line in _warnings(events) if "which the profile doesn't grant" in line]


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "build"])
def test_a_wildcard_profile_warns_about_the_app_groups_the_app_asks_for(
    tmp_path: Path, capsys, dry_run: bool
) -> None:
    ipa = make_ipa(tmp_path / "cache" / "example.ipa", {**REWRITTEN, **APP_GROUPS})
    _, events, _ = _build(_cfg(tmp_path), ipa, dry_run=dry_run, capsys=capsys)

    assert _capability_warnings(events) == [WARNING]


def test_the_warning_comes_before_zsign_runs(tmp_path: Path, capsys) -> None:
    ipa = make_ipa(tmp_path / "cache" / "example.ipa", {**REWRITTEN, **APP_GROUPS})
    _, _, order = _build(_cfg(tmp_path), ipa, dry_run=False, capsys=capsys)

    assert "zsign" in order
    assert order.index(WARNING) < order.index("zsign")


def test_the_warning_sits_inside_the_sign_stage(tmp_path: Path, capsys) -> None:
    """Between `sign` start and end in the stream the build panel renders."""
    ipa = make_ipa(tmp_path / "cache" / "example.ipa", {**REWRITTEN, **APP_GROUPS})
    _, events, _ = _build(_cfg(tmp_path), ipa, dry_run=False, capsys=capsys)

    sign = [i for i, e in enumerate(events) if e["type"] == "stage" and e["name"] == "sign"]
    warned = next(i for i, e in enumerate(events) if e.get("line") == WARNING)
    assert sign[0] < warned < sign[1]


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "build"])
def test_a_profile_that_grants_app_groups_says_nothing(
    tmp_path: Path, capsys, dry_run: bool
) -> None:
    ipa = make_ipa(tmp_path / "cache" / "example.ipa", {**REWRITTEN, **APP_GROUPS})
    profile = profile_granting(tmp_path, APP_GROUPS)
    _, events, _ = _build(_cfg(tmp_path, profile=profile), ipa, dry_run=dry_run, capsys=capsys)

    assert _capability_warnings(events) == []


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "build"])
def test_an_unsigned_binary_says_nothing_and_builds(tmp_path: Path, capsys, dry_run: bool) -> None:
    ipa = make_ipa(tmp_path / "cache" / "example.ipa", None)
    _, events, _ = _build(_cfg(tmp_path), ipa, dry_run=dry_run, capsys=capsys)

    assert _capability_warnings(events) == []
    assert events[-1]["type"] == "build-end" and events[-1]["ok"] is True


def test_a_malformed_signature_says_nothing_and_builds(tmp_path: Path, capsys) -> None:
    from . import macho_builder as mb

    ipa = make_ipa(tmp_path / "cache" / "example.ipa", None,
                   main_binary=mb.signed([], b"\0" * 64, datasize=1 << 30))
    _, events, _ = _build(_cfg(tmp_path), ipa, dry_run=False, capsys=capsys)

    assert _capability_warnings(events) == []
    assert events[-1]["ok"] is True


def test_livecontainer_delivery_signs_nothing_and_says_nothing(tmp_path: Path, capsys) -> None:
    ipa = make_ipa(tmp_path / "cache" / "example.ipa", {**REWRITTEN, **APP_GROUPS})
    _, events, order = _build(_cfg(tmp_path, delivery="livecontainer"), ipa,
                              dry_run=True, capsys=capsys)

    assert _capability_warnings(events) == []
    assert "zsign" not in order


def test_a_push_only_gap_is_one_line_that_says_nothing_of_a_crash(tmp_path: Path, capsys) -> None:
    """Combined fix check #29: nearly every App Store app asks for push."""
    ipa = make_ipa(tmp_path / "cache" / "example.ipa", {**REWRITTEN, "aps-environment": "production"})
    _, events, _ = _build(_cfg(tmp_path), ipa, dry_run=True, capsys=capsys)

    assert _capability_warnings(events) == [
        "example asks for Push Notifications, which the profile doesn't grant; those features won't work."
    ]


def test_a_profile_granting_another_group_warns_naming_the_apps_group(tmp_path: Path, capsys) -> None:
    ipa = make_ipa(tmp_path / "cache" / "example.ipa", {**REWRITTEN, **APP_GROUPS})
    profile = profile_granting(tmp_path, {"com.apple.security.application-groups": ["group.someone.else"]})
    _, events, _ = _build(_cfg(tmp_path, profile=profile), ipa, dry_run=True, capsys=capsys)

    assert _capability_warnings(events) == [WARNING]
    assert f"App Groups ({GROUP})" in WARNING


def test_a_corrupt_ipa_dry_run_with_skip_decrypt_check_still_builds(tmp_path: Path, capsys) -> None:
    """Combined fix check #26: the check raised LZMAError at stage 4 and
    failed a dry run that reads the member nowhere else."""
    ipa = _lzma_ipa(tmp_path / "cache" / "example.ipa")
    _, events, _ = _build(_cfg(tmp_path), ipa, dry_run=True, capsys=capsys)

    assert _capability_warnings(events) == []
    assert events[-1]["type"] == "build-end" and events[-1]["ok"] is True


def test_with_strip_extensions_an_extensions_ask_is_not_warned(tmp_path: Path, capsys) -> None:
    ipa = make_ipa(tmp_path / "cache" / "example.ipa", REWRITTEN,
                   appexes={"Widget": {**REWRITTEN, **APP_GROUPS}})
    cfg = _cfg(tmp_path)
    cfg.apps["example"].cyan.strip_extensions = True
    _, events, _ = _build(cfg, ipa, dry_run=True, capsys=capsys)
    assert _capability_warnings(events) == []

    cfg.apps["example"].cyan.strip_extensions = False
    _, events, _ = _build(cfg, ipa, dry_run=True, capsys=capsys)
    assert len(_capability_warnings(events)) == 1


@pytest.mark.parametrize("dry_run", [False, True], ids=["build", "dry-run"])
def test_markup_in_the_certificate_never_fails_the_signing_line(tmp_path: Path, capsys, dry_run: bool) -> None:
    """Combined fix check #27: the subject and team come from the .p12's own
    certificate, and rich raised MarkupError on a closing tag that matched
    nothing, after zsign had signed."""
    ipa = make_ipa(tmp_path / "cache" / "example.ipa", None)
    hostile = signer.SignResult(
        output_ipa=tmp_path / "signed.ipa", team_id="AB[/b]CD", cert_subject="Apple Development: A [/b] (ZZ99)",
        expiry="2030 [/i]", stdout="would sign [/u] with zsign", stderr="",
    )

    def run_zsign(cfg, src, out, **kw):
        out.write_bytes(b"signed")
        return hostile

    with patch.object(cli_mod, "run_zsign", side_effect=run_zsign):
        _, events, _ = _build(_cfg(tmp_path), ipa, dry_run=dry_run, capsys=capsys)
    assert events[-1]["type"] == "build-end" and events[-1]["ok"] is True



def test_a_renamed_build_isnt_warned_about_its_removed_watch_app(tmp_path: Path, capsys) -> None:
    """cli-11, through the build: `keep_bundle_id: false` makes cyan pass
    `-w`, so the Watch app's extension isn't on the device."""
    from .test_capabilities import WATCH_GROUP

    ipa = make_ipa(tmp_path / "cache" / "example.ipa", REWRITTEN,
                   watch_appexes={"WatchExt": {"com.apple.security.application-groups": [WATCH_GROUP]}})
    cfg = _cfg(tmp_path)
    _, events, _ = _build(cfg, ipa, dry_run=True, capsys=capsys)
    assert len(_capability_warnings(events)) == 1, "premise: kept, the Watch app is warned about"

    cfg.apps["example"].cyan.keep_bundle_id = False
    _, events, _ = _build(cfg, ipa, dry_run=True, capsys=capsys)
    assert _capability_warnings(events) == []
