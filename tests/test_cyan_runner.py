from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from graft_ipa import tools
from graft_ipa.config import AppConfig, Config, CyanOptions, SigningConfig
from graft_ipa.cyan_runner import build_command, run_cyan
from graft_ipa.errors import CyanError
from tests.tool_fakes import fake_path


@pytest.fixture(autouse=True)
def _cyan_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cyan to find, whatever this Mac has: `run_cyan` refuses before it
    spawns anything when there is none. `subprocess.run` is faked in every test
    that gets that far, so it is never run."""
    monkeypatch.setattr(tools, "which", fake_path(tmp_path / "path-bin"))


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
            extra_args=list(cyan_overrides.get("extra_args", [])),
        ),
    )


def test_build_command_keeps_bundle_id(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    app = make_app(keep_bundle_id=True)
    cmd = build_command(cfg, app, tmp_path / "in.ipa", tmp_path / "out.ipa")
    assert "-b" not in cmd
    assert "-u" in cmd
    assert "-e" not in cmd
    assert "-n=Example+" in cmd


def test_build_command_changes_bundle_id(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    app = make_app(keep_bundle_id=False, new_bundle_id="com.foo.bar")
    cmd = build_command(cfg, app, tmp_path / "in.ipa", tmp_path / "out.ipa")
    assert "-b" in cmd
    assert "com.foo.bar" in cmd


WATCH_FLAGS = ("-w", "--no-watch")


def _watch_flags(cmd: list[str]) -> list[str]:
    return [a for a in cmd if a in WATCH_FLAGS]


@pytest.mark.parametrize("new_bundle_id", ["com.foo.bar", None])
def test_a_renamed_bundle_id_removes_the_watch_app(
    tmp_path: Path, new_bundle_id: str | None
) -> None:
    # A Watch app names its companion's bundle id (WKCompanionAppBundleIdentifier),
    # and cyan's -b does not rewrite it, so the device refused the renamed IPA
    # with InvalidCompanionAppBundleIdentifier. A Watch app cannot install under
    # a renamed companion at all, so a rename removes it (cyan -w).
    cfg = make_cfg(tmp_path)
    app = make_app(keep_bundle_id=False, new_bundle_id=new_bundle_id)
    cmd = build_command(cfg, app, tmp_path / "in.ipa", tmp_path / "out.ipa")
    assert "-b" in cmd
    assert _watch_flags(cmd) == ["-w"]


@pytest.mark.parametrize("new_bundle_id", [None, "com.foo.bar"])
def test_a_kept_bundle_id_keeps_the_watch_app(
    tmp_path: Path, new_bundle_id: str | None
) -> None:
    # With keep_bundle_id true a new_bundle_id is not applied (no -b), so the
    # Watch app still matches its companion and stays.
    cfg = make_cfg(tmp_path)
    app = make_app(keep_bundle_id=True, new_bundle_id=new_bundle_id)
    cmd = build_command(cfg, app, tmp_path / "in.ipa", tmp_path / "out.ipa")
    assert "-b" not in cmd
    assert _watch_flags(cmd) == []


@pytest.mark.parametrize("flag", WATCH_FLAGS)
def test_a_watch_flag_already_in_extra_args_is_not_repeated(
    tmp_path: Path, flag: str
) -> None:
    cfg = make_cfg(tmp_path)
    app = make_app(keep_bundle_id=False, new_bundle_id="com.foo.bar", extra_args=[flag])
    cmd = build_command(cfg, app, tmp_path / "in.ipa", tmp_path / "out.ipa")
    assert _watch_flags(cmd) == [flag]


def test_a_watch_app_naming_the_old_bundle_id_explains_itself(tmp_path: Path) -> None:
    from graft_ipa import device_install, installer
    from graft_ipa.errors import InstallError

    # The reason is checked before the generic codes, so it wins even if the
    # device wraps it in one. The words are the device's refusal as
    # `device_install` hands it on: pymobiledevice3's exception name, then
    # InstallationProxy's error and description.
    device_output = (
        "AppInstallError: ApplicationVerificationFailed: Failed to verify code "
        "signature of /private/var/installd/Library/Caches/com.apple.mobile.installd"
        ".staging/temp.abc/extracted/Payload/Example.app : 0xe8008001 "
        "InvalidCompanionAppBundleIdentifier: Invalid value of "
        "WKCompanionAppBundleIdentifier key in WatchKit 2.0 app's Info.plist: "
        "com.example.app (expected com.foo.bar)"
    )

    def refuse(*_a, **_k):
        raise device_install.DeviceInstallError(device_output)

    with patch.object(installer.device_install, "install_ipa", side_effect=refuse):
        with pytest.raises(InstallError) as exc:
            installer.install(tmp_path / "out.ipa", udid="0000", transport="usb")
    hint = exc.value.hint
    assert "Apple Watch app" in hint
    assert "old bundle id" in hint
    assert "Keep the bundle id" in hint


def test_the_watch_app_hint_matches_the_wording_a_real_device_sent(tmp_path: Path) -> None:
    """The refusal exactly as an iPhone sent it through `device_install`
    (2026-11-18, a renamed sideload of an app carrying a Watch app; ids made
    generic): the bare code, not wrapped in ApplicationVerificationFailed."""
    from graft_ipa import device_install, installer
    from graft_ipa.errors import InstallError

    device_output = (
        "AppInstallError: InvalidCompanionAppBundleIdentifier: Invalid value of "
        "WKCompanionAppBundleIdentifier key in WatchKit 2.0 app's Info.plist: "
        "com.example.app (expected com.example.renamed)"
    )

    def refuse(*_a, **_k):
        raise device_install.DeviceInstallError(device_output)

    with patch.object(installer.device_install, "install_ipa", side_effect=refuse):
        with pytest.raises(InstallError) as exc:
            installer.install(tmp_path / "out.ipa", udid="0000", transport="usb")
    assert "Apple Watch app" in exc.value.hint


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


def test_cyan_runs_with_its_install_name_tool_told_never_to_sign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cctools-port's install_name_tool re-signs every ARM file it changes
    through `system("codesign -s - -f <path>")`, with the path unquoted, so a
    tweak named `Fx;touch PWNED;.dylib` ran `touch PWNED` (review #2). The
    bundle builds it with that hook compiled out; NO_CODESIGN, the hook's own
    off switch, is the second layer, for a cyan whose helpers were built some
    other way. Set by Graft, whatever this process's environment says."""
    monkeypatch.delenv("NO_CODESIGN", raising=False)
    out = tmp_path / "out.ipa"
    seen = {}

    def cyan(cmd, **kw):
        seen.update(kw["env"])
        out.write_bytes(b"tweaked")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=cyan):
        run_cyan(make_cfg(tmp_path), make_app(), tmp_path / "in.ipa", out)
    assert seen.get("NO_CODESIGN") == "1"


def test_cyan_runs_the_resolved_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helpers = tmp_path / "Graft.app" / "Contents" / "Helpers" / "bin"
    helpers.mkdir(parents=True)
    (helpers / "cyan").write_text("#!/bin/sh\n")
    (helpers / "cyan").chmod(0o755)
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: helpers)
    cmd = build_command(make_cfg(tmp_path), make_app(), tmp_path / "in.ipa", tmp_path / "out.ipa")
    assert cmd[0] == str(helpers / "cyan")


def test_run_cyan_hands_subprocess_the_resolved_binary(tmp_path: Path) -> None:
    ran: list[list[str]] = []
    out = tmp_path / "out.ipa"

    def cyan(cmd, **_):
        ran.append(list(cmd))
        out.write_bytes(b"tweaked")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=cyan):
        run_cyan(make_cfg(tmp_path), make_app(), tmp_path / "in.ipa", out)
    assert [cmd[0] for cmd in ran] == [str(tmp_path / "path-bin" / "cyan")]


def test_a_missing_cyan_is_refused_before_anything_is_spawned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "which", lambda name: None)
    with patch("subprocess.run", side_effect=lambda *a, **k: pytest.fail("something ran")), \
         pytest.raises(CyanError) as exc:
        run_cyan(make_cfg(tmp_path), make_app(), tmp_path / "in.ipa", tmp_path / "out.ipa")
    assert exc.value.message == "cyan not found in PATH"
    assert not list(tmp_path.glob(".cyan-tmp-*")), "a scratch folder was made for nothing"


def test_a_dry_run_without_cyan_still_renders_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "which", lambda name: None)
    res = run_cyan(make_cfg(tmp_path), make_app(), tmp_path / "in.ipa", tmp_path / "out.ipa",
                   dry_run=True)
    assert res.stdout.startswith("cyan -i ")


# --- ANSI color ------------------------------------------------------------
#
# cyan colors its counts for a terminal, and those escapes rode through into
# the build log the app shows as `changed [96m41[0m localized names`. They are
# stripped where cyan's output enters graft, so the human console, the `log`
# events and an error's hint all get plain text.

_COLOURED = "[*] changed \x1b[96m41\x1b[0m localized names\n[*] \x1b[1;32mdone\x1b[0m\x1b[K\n"
_PLAIN = "[*] changed 41 localized names\n[*] done\n"


def test_cyan_output_reaches_graft_without_colour_codes(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    app = make_app()
    out = tmp_path / "out.ipa"

    def fake_run(cmd, **kwargs):
        out.write_bytes(b"ipa")
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=_COLOURED, stderr="\x1b[33mwarning\x1b[0m: x\n"
        )

    with patch("subprocess.run", side_effect=fake_run):
        res = run_cyan(cfg, app, tmp_path / "in.ipa", out)

    assert res.stdout == _PLAIN
    assert res.stderr == "warning: x\n"
    assert "\x1b" not in res.stdout + res.stderr


def test_a_failed_cyan_hint_is_plain_text_too(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    app = make_app()
    fake = subprocess.CompletedProcess(
        args=[], returncode=3, stdout=_COLOURED, stderr="\x1b[31mboom\x1b[0m\n"
    )
    with patch("subprocess.run", return_value=fake):
        with pytest.raises(CyanError) as exc:
            run_cyan(cfg, app, tmp_path / "in.ipa", tmp_path / "out.ipa")
    assert "\x1b" not in (exc.value.hint or "")
    assert "changed 41 localized names" in (exc.value.hint or "")
    assert "boom" in (exc.value.hint or "")


def test_the_build_log_events_carry_cyans_lines_without_colour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the emitter the build uses: the app's log is the
    `log` events with source `cyan`."""
    import io
    import json

    from graft_ipa import cli as cli_mod
    from graft_ipa.json_output import JsonEmitter

    cfg = make_cfg(tmp_path)
    app = make_app()
    out = tmp_path / "out.ipa"

    def fake_run(cmd, **kwargs):
        out.write_bytes(b"ipa")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=_COLOURED, stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        res = run_cyan(cfg, app, tmp_path / "in.ipa", out)
    buf = io.StringIO()
    cli_mod._emit_log_lines(JsonEmitter(stream=buf), "cyan", res.stdout)

    lines = [json.loads(line)["line"] for line in buf.getvalue().splitlines()]
    assert lines == ["[*] changed 41 localized names", "[*] done"]


# --- review 2.0, bundle #7: the bundled cyan's ldid reads no OpenSSL configuration ---------


def _cyan_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, bundled: bool) -> dict[str, str]:
    from graft_ipa import child_env

    helpers = tmp_path / "Graft.app" / "Contents" / "Helpers" / "bin"
    helpers.mkdir(parents=True)
    (helpers / "cyan").write_text("#!/bin/sh\n")
    (helpers / "cyan").chmod(0o755)
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: helpers if bundled else None)
    monkeypatch.setattr(child_env, "_bundled_bin", lambda: helpers if bundled else None)
    monkeypatch.setenv("OPENSSL_CONF", "/opt/homebrew/etc/openssl@3/openssl.cnf")
    out = tmp_path / "out.ipa"
    seen: dict[str, str] = {}

    def cyan(cmd, **kw):
        seen.update(kw["env"])
        out.write_bytes(b"tweaked")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=cyan):
        run_cyan(make_cfg(tmp_path), make_app(), tmp_path / "in.ipa", out)
    return seen


def test_the_bundled_cyan_runs_its_ldid_with_no_openssl_configuration(tmp_path, monkeypatch):
    """Graft.app's ldid links the bundled libcrypto, which would read
    /opt/graft/openssl/ssl/openssl.cnf; cyan runs it, in cyan's environment."""
    assert _cyan_env(tmp_path, monkeypatch, bundled=True).get("OPENSSL_CONF") == "/dev/null"


def test_a_cyan_from_path_keeps_the_users_openssl_configuration(tmp_path, monkeypatch):
    """Its ldid is the user's, with its own OpenSSL."""
    seen = _cyan_env(tmp_path, monkeypatch, bundled=False)
    assert seen.get("OPENSSL_CONF") == "/opt/homebrew/etc/openssl@3/openssl.cnf"
