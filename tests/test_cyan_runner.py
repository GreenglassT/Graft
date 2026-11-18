from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from graft_ipa.config import AppConfig, Config, CyanOptions, SigningConfig
from graft_ipa.cyan_runner import build_command, run_cyan
from graft_ipa.errors import CyanError


def make_cfg(tmp_path: Path) -> Config:
    tweaks = tmp_path / "tweaks"
    tweaks.mkdir()
    (tweaks / "a.deb").write_bytes(b"x")
    return Config(
        path=tmp_path / "config.yaml",
        signing=SigningConfig(
            cert=tmp_path / "cert.p12",
            cert_password_env="X",
            profile=tmp_path / "profile",
            team_id="A",
        ),
        cache_dir=tmp_path / "cache",
        built_dir=tmp_path / "built",
        tweaks_dir=tweaks,
        state_dir=tmp_path / "state",
        apps={},
    )


def make_app(**cyan_overrides) -> AppConfig:
    return AppConfig(
        name="example",
        bundle_id="com.example.app",
        preferred_version="latest",
        tweaks=["a.deb"],
        cyan=CyanOptions(
            display_name="Example+",
            keep_bundle_id=cyan_overrides.get("keep_bundle_id", True),
            new_bundle_id=cyan_overrides.get("new_bundle_id"),
            strip_extensions=cyan_overrides.get("strip_extensions", False),
            strip_supported_devices=cyan_overrides.get("strip_supported_devices", True),
            compression_level=6,
        ),
    )


def test_build_command_keeps_bundle_id(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    app = make_app(keep_bundle_id=True)
    cmd = build_command(cfg, app, tmp_path / "in.ipa", tmp_path / "out.ipa")
    assert "-b" not in cmd
    assert "-u" in cmd
    assert "-e" not in cmd
    assert "-n" in cmd
    assert "Example+" in cmd


def test_build_command_changes_bundle_id(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    app = make_app(keep_bundle_id=False, new_bundle_id="com.foo.bar")
    cmd = build_command(cfg, app, tmp_path / "in.ipa", tmp_path / "out.ipa")
    assert "-b" in cmd
    assert "com.foo.bar" in cmd


def test_missing_tweak_file_raises(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    app = make_app()
    app.tweaks = ["missing.deb"]
    with pytest.raises(CyanError) as exc:
        build_command(cfg, app, tmp_path / "in.ipa", tmp_path / "out.ipa")
    assert "missing tweak" in str(exc.value).lower()


def test_run_cyan_translates_missing_lib(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    app = make_app()
    fake = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr='dyld: Library not loaded: @rpath/libexample.dylib',
    )
    with patch("subprocess.run", return_value=fake):
        with pytest.raises(CyanError) as exc:
            run_cyan(cfg, app, tmp_path / "in.ipa", tmp_path / "out.ipa")
    assert "libexample" in str(exc.value)


def test_run_cyan_translates_bad_deb(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    app = make_app()
    fake = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="couldn't extract foo.deb\n", stderr=""
    )
    with patch("subprocess.run", return_value=fake):
        with pytest.raises(CyanError) as exc:
            run_cyan(cfg, app, tmp_path / "in.ipa", tmp_path / "out.ipa")
    assert "foo.deb" in str(exc.value)


def test_run_cyan_dry_run(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    app = make_app()
    res = run_cyan(cfg, app, tmp_path / "in.ipa", tmp_path / "out.ipa", dry_run=True)
    assert "cyan" in res.stdout
    assert "a.deb" in res.stdout


def test_cyan_unpacks_inside_the_build_work_dir_not_the_system_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cyan unpacks the IPA into `tempfile.TemporaryDirectory()`, which only
    cleans up if cyan exits normally. A Stop kills it, and the unpacked app
    (431 MB on a real build) was left in $TMPDIR. Its TMPDIR is now a private
    folder beside the output IPA, inside the build's work dir, which the
    build's own cleanup removes whatever happened to cyan."""
    # A broken fix would hand cyan this process's TMPDIR: keep that in tmp_path too.
    monkeypatch.setenv("TMPDIR", str(tmp_path / "system-tmp"))
    cfg = make_cfg(tmp_path)
    work = tmp_path / "graft-example-work"
    out = work / "example-tweaked.ipa"
    seen = {}

    def fake_run(cmd, **kw):
        tmp = Path(kw["env"]["TMPDIR"])
        seen["tmp"] = tmp
        seen["mode"] = tmp.stat().st_mode & 0o777
        out.write_bytes(b"tweaked")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        run_cyan(cfg, make_app(), tmp_path / "in.ipa", out)

    assert seen["tmp"].parent == work
    assert seen["mode"] == 0o700
    assert not seen["tmp"].exists(), "an empty scratch folder is left behind after a good run"


def test_a_killed_cyan_leaves_its_unpacked_app_where_the_build_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A broken fix would hand cyan this process's TMPDIR: keep that in tmp_path too.
    monkeypatch.setenv("TMPDIR", str(tmp_path / "system-tmp"))
    cfg = make_cfg(tmp_path)
    work = tmp_path / "graft-example-work"
    out = work / "example-tweaked.ipa"

    seen_tmp = []

    def killed(cmd, **kw):
        seen_tmp.append(Path(kw["env"]["TMPDIR"]))
        leftover = Path(kw["env"]["TMPDIR"]) / "tmp_abcdefgh" / "Payload"
        leftover.mkdir(parents=True)
        raise KeyboardInterrupt  # stands in for the Stop that kills cyan mid-run

    with patch("subprocess.run", side_effect=killed):
        with pytest.raises(KeyboardInterrupt):
            run_cyan(cfg, make_app(), tmp_path / "in.ipa", out)

    assert seen_tmp and seen_tmp[0].parent == work
    assert not list(tmp_path.rglob("Payload")), "the unpacked app outlived the killed cyan"
