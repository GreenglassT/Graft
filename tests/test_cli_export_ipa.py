"""`--export-ipa PATH`: the tweak-injected IPA, unsigned, written where the user chose.

For people who sign on the phone (an on-device signer and a certificate
service) or install with TrollStore. An export is the build up to and
including cyan, and nothing after it:

- it never signs, never archives into `built_dir`, and never looks for,
  pushes to or installs on a device, whatever the app's delivery mode;
- it writes the file whole or not at all (a temp file beside it, then a
  rename), and never replaces a file without `--overwrite`;
- it emits the usual build stream, with `ipa-exported` naming the file just
  before `build-end`;
- it adds no row to the build history, opens no saved log, and runs no
  archive retention sweep. The history is the record of builds Graft archived
  and can re-ship (Re-install, Install to Mac, Reveal, the `archive_keep`
  sweep); an export's file is the user's, outside all of that.

Everything between the config and the file is real except cyan (a stand-in
that copies the IPA and marks it), the tools' presence (looked up, never run)
and the device, which an export must never reach: every way to it fails the
test.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import unicodedata
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa import keychain, lc_runner, p12, preflight, tools
from graft_ipa.build_log import BuildLog
from graft_ipa.config import add_app, default_config_path, load_config, write_default_config
from graft_ipa.errors import ConfigError, GraftError, IPANotFoundError
from graft_ipa.json_output import JsonEmitter
from tests.tool_fakes import fake_path

from .test_free_account_build import _stage_ipa

TWEAK = "some-tweak.deb"
MARK = b"<tweaked>"


def _forbid(reached: list[str], what: str):
    """A stand-in that records `what` and fails the test, from anywhere.

    `pytest.fail` raises a BaseException, so no `except Exception` on the way
    out (and the build has several) can swallow it.
    """

    def stand_in(*_a, **_k):
        reached.append(what)
        pytest.fail(f"an export reached {what}")

    return stand_in


@pytest.fixture
def reached() -> list[str]:
    return []


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reached: list[str], *, free: bool,
           **signing):
    path = default_config_path()
    write_default_config(path, free_account=free, default_device_udid="UDID1", **signing)
    # A sideload app and a LiveContainer one: an export ignores the mode, so
    # both must come out the same way.
    add_app(path, "example", {"bundle_id": "com.example.app", "tweaks": [TWEAK],
                              "delivery": "sideload"})
    add_app(path, "other", {"bundle_id": "com.example.other", "tweaks": [TWEAK],
                            "delivery": "livecontainer"})
    cfg = load_config(path)
    _stage_ipa(cfg.cache_dir)
    _stage_ipa(cfg.cache_dir, bundle_id="com.example.other", version="2.0")
    cfg.tweaks_dir.mkdir(parents=True, exist_ok=True)
    (cfg.tweaks_dir / TWEAK).write_bytes(b"a tweak")

    # cyan is looked up, never run: the stand-in copies the IPA and marks it.
    monkeypatch.setattr(tools, "which", fake_path(tmp_path / "path-bin"))
    injected: list[Path] = []

    def cyan(_cfg, _app, src, dst, *, dry_run=False):
        injected.append(Path(src))
        if not dry_run:
            shutil.copyfile(src, dst)
            Path(dst).write_bytes(Path(dst).read_bytes() + MARK)
        return SimpleNamespace(output_ipa=Path(dst), stdout="", stderr="")

    monkeypatch.setattr(cli_mod, "run_cyan", cyan)

    # Signing, archiving for a delivery, and every way to a device.
    monkeypatch.setattr(cli_mod, "run_zsign", _forbid(reached, "zsign"))
    monkeypatch.setattr(cli_mod, "install", _forbid(reached, "a sideload install"))
    monkeypatch.setattr(cli_mod, "select_device", _forbid(reached, "select_device"))
    monkeypatch.setattr(cli_mod, "_check_lc_target_early", _forbid(reached, "the LiveContainer check"))
    monkeypatch.setattr(lc_runner, "push_ipa_to_livecontainer", _forbid(reached, "a LiveContainer push"))
    monkeypatch.setattr(preflight, "list_connected_devices", _forbid(reached, "the device list"))
    monkeypatch.setattr(preflight, "list_connected_devices_with_transport",
                        _forbid(reached, "the device list"))
    # The signing identity: its password in the keychain, and the .p12
    # itself, which every way of reading one goes through `p12`. Forbidden
    # here rather than left to conftest's empty fake keychain, which answers
    # "no password" and lets a read pass unnoticed.
    monkeypatch.setattr(keychain, "read_p12_password", _forbid(reached, "the .p12 password"))
    monkeypatch.setattr(keychain, "find_p12_password", _forbid(reached, "the .p12 password"))
    for name in ("load_key_and_certificates", "write_reencrypted", "private_identity"):
        monkeypatch.setattr(p12, name, _forbid(reached, "the .p12"))
    cfg.injected = injected  # type: ignore[attr-defined]
    return cfg


@pytest.fixture
def paid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reached: list[str]):
    """A paid config whose signing files don't exist: any signing check fails."""
    return _setup(tmp_path, monkeypatch, reached, free=False)


@pytest.fixture
def free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reached: list[str]):
    return _setup(tmp_path, monkeypatch, reached, free=True)


@pytest.fixture
def signer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reached: list[str]):
    """A paid config with everything a sideload build would read to sign:
    a team, so the pre-flight asks the keychain for the .p12's password, and
    a .p12 and a profile on disk."""
    ids = tmp_path / "identity"
    ids.mkdir()
    cert, profile = ids / "AppleDevelopment.p12", ids / "wildcard.mobileprovision"
    cert.write_bytes(b"a .p12")
    profile.write_bytes(b"a profile")
    return _setup(tmp_path, monkeypatch, reached, free=False, team_id="ABCDE12345",
                  cert=cert, profile=profile)


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    d = tmp_path / "Exports"
    d.mkdir()
    return d


def _export(cfg, dest: Path, *, app: str = "example", overwrite: bool = False,
            dry_run: bool = False) -> tuple[list[dict], BaseException | None]:
    out = StringIO()
    error: BaseException | None = None
    try:
        cli_mod._build_one(
            cfg=cfg, app_name=app, dry_run=dry_run, skip_install=False,
            skip_decrypt_check=False, verbose=False, device=None,
            emitter=JsonEmitter(stream=out), export_to=dest, overwrite=overwrite,
        )
    except GraftError as exc:
        error = exc
    events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    return events, error


def _staged(cfg, bundle_id: str = "com.example.app", version: str = "1.0") -> bytes:
    return (cfg.cache_dir / f"{bundle_id}_{version}.ipa").read_bytes()


# --- what it writes ---------------------------------------------------------------


@pytest.mark.parametrize("app", ["example", "other"])
def test_it_writes_the_injected_ipa_and_nothing_else(
    paid, out_dir: Path, reached: list[str], app: str
) -> None:
    dest = out_dir / "Example.ipa"

    _, error = _export(paid, dest, app=app)

    assert error is None, error
    assert reached == []
    bundle, version = ("com.example.app", "1.0") if app == "example" else ("com.example.other", "2.0")
    assert dest.read_bytes() == _staged(paid, bundle, version) + MARK, "cyan's output, as it was"
    assert sorted(p.name for p in out_dir.iterdir()) == ["Example.ipa"], "no temp file left"
    assert not paid.built_dir.exists() or not any(paid.built_dir.iterdir()), "no archive"


def test_the_stream_is_a_build_that_ends_naming_the_file(paid, out_dir: Path) -> None:
    dest = out_dir / "Example.ipa"

    events, _ = _export(paid, dest)

    start = events[0]
    assert start["type"] == "build-start"
    assert start["app"] == "example"
    assert start["delivery"] is None, "an export delivers nothing, whatever the app's mode"
    assert start["export_to"] == str(dest)
    stages = [(e["name"], e["status"]) for e in events if e["type"] == "stage"]
    assert stages == [
        ("acquire", "start"), ("acquire", "end"),
        ("verify", "start"), ("verify", "end"),
        ("cyan", "start"), ("cyan", "end"),
        ("export", "start"), ("export", "end"),
    ]
    exported, end = events[-2], events[-1]
    assert exported == {
        "type": "ipa-exported", "app": "example", "version": "1.0", "path": str(dest),
        "filename": "Example.ipa", "size_bytes": dest.stat().st_size, "tweaks": [TWEAK],
        "replaced": False, "dry_run": False,
    }
    assert end["type"] == "build-end" and end["ok"] is True and end["stopped"] is False


def test_a_relative_path_is_written_where_it_names(
    paid, out_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(out_dir)

    events, error = _export(paid, Path("Example.ipa"))

    assert error is None, error
    assert (out_dir / "Example.ipa").read_bytes().endswith(MARK)
    assert events[-2]["path"] == str(out_dir / "Example.ipa"), "said as the absolute path"


# --- whatever the delivery mode -----------------------------------------------------


def test_a_free_account_exports_an_app_it_could_not_build(
    free, out_dir: Path, reached: list[str]
) -> None:
    """`example` is a sideload app, which a free Apple ID can't build (it has
    no certificate to sign with). An export signs nothing, so it isn't
    refused."""
    with pytest.raises(GraftError):
        cli_mod._build_one(
            cfg=free, app_name="example", dry_run=False, skip_install=True,
            skip_decrypt_check=False, verbose=False, device=None,
            emitter=JsonEmitter(stream=StringIO()),
        )

    events, error = _export(free, out_dir / "Example.ipa")

    assert error is None, error
    assert reached == []
    assert events[-1]["ok"] is True


def test_the_preflight_asks_only_for_cyan(
    paid, out_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The paid config's signing files don't exist, so a sideload build's
    pre-flight fails. An export's doesn't look at them, nor for the signing
    tools."""
    monkeypatch.setattr(
        tools, "which", fake_path(tmp_path / "bin2", missing=["zsign", "security"])
    )

    _, error = _export(paid, out_dir / "Example.ipa")

    assert error is None, error


def test_a_missing_cyan_is_still_a_failed_preflight(
    paid, out_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "which", fake_path(tmp_path / "bin2", missing=["cyan"]))

    events, error = _export(paid, out_dir / "Example.ipa")

    assert error is not None and "cyan" in error.message
    assert events[-1]["type"] == "build-end" and events[-1]["ok"] is False
    assert list(out_dir.iterdir()) == []


# --- no history, no saved log, no retention ----------------------------------------


def test_an_export_writes_no_history_and_no_saved_log(paid, out_dir: Path) -> None:
    _export(paid, out_dir / "Example.ipa")

    assert not (paid.state_dir / "log.sqlite").exists()
    assert not (paid.state_dir / "build-logs").exists()


def test_a_failed_export_writes_no_history_either(paid, out_dir: Path) -> None:
    log = BuildLog(paid.state_dir / "log.sqlite")  # a history that already exists
    paid_ipa = paid.cache_dir / "com.example.app_1.0.ipa"
    paid_ipa.unlink()  # nothing staged: the export fails at acquire

    events, error = _export(paid, out_dir / "Example.ipa")

    assert error is not None
    assert events[-1]["ok"] is False
    assert log.recent(app=None, limit=10) == []
    assert log.latest_delivery("example") is None


def test_an_export_leaves_the_archives_and_their_sweep_alone(paid, out_dir: Path) -> None:
    """A build that worked sweeps `built_dir` down to `archive_keep` per app
    version. An export archives nothing, so it has nothing to sweep for."""
    paid.built_dir.mkdir(parents=True, exist_ok=True)
    archives = [paid.built_dir / f"example_1.0_2026010{i}T000000.ipa" for i in range(1, 6)]
    for a in archives:
        a.write_bytes(b"an archive")

    _, error = _export(paid, out_dir / "Example.ipa")

    assert error is None, error
    assert all(a.exists() for a in archives)
    assert sorted(paid.built_dir.iterdir()) == archives


@pytest.mark.parametrize("folder", ["cache_dir", "built_dir"])
def test_it_refuses_to_write_into_a_folder_graft_manages(
    paid, folder: str, reached: list[str]
) -> None:
    """The cache would take a tweaked IPA for a source to build from, and the
    retention sweep moves files in `built_dir` to the Trash."""
    target_dir = getattr(paid, folder)
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / "example_1.0_20260101T000000.ipa"

    events, error = _export(paid, dest)

    assert isinstance(error, ConfigError)
    assert "Graft's own folder" in error.message
    assert not dest.exists()
    assert paid.injected == [], "refused before the build ran"
    assert events[0]["type"] == "build-start" and events[-1]["ok"] is False


# --- the overwrite rule -------------------------------------------------------------


def test_an_existing_file_is_refused_before_anything_runs(paid, out_dir: Path) -> None:
    dest = out_dir / "Example.ipa"
    dest.write_bytes(b"theirs")

    events, error = _export(paid, dest)

    assert isinstance(error, ConfigError)
    assert "already exists" in error.message
    assert "--overwrite" in (error.hint or "")
    assert dest.read_bytes() == b"theirs"
    assert paid.injected == [], "refused before minutes of work, not after"
    assert events[-1]["type"] == "build-end" and events[-1]["ok"] is False
    assert sorted(p.name for p in out_dir.iterdir()) == ["Example.ipa"]


def test_overwrite_replaces_it(paid, out_dir: Path) -> None:
    dest = out_dir / "Example.ipa"
    dest.write_bytes(b"theirs")

    events, error = _export(paid, dest, overwrite=True)

    assert error is None, error
    assert dest.read_bytes().endswith(MARK)
    assert events[-2]["replaced"] is True
    assert sorted(p.name for p in out_dir.iterdir()) == ["Example.ipa"]


def test_a_file_that_appears_mid_build_is_not_replaced(
    paid, out_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checked up front, and again at the moment of the move: the move is a
    hard link, which fails on a name that exists."""
    dest = out_dir / "Example.ipa"
    cyan = cli_mod.run_cyan

    def cyan_then_someone_saves(*a, **k):
        result = cyan(*a, **k)
        dest.write_bytes(b"saved meanwhile")
        return result

    monkeypatch.setattr(cli_mod, "run_cyan", cyan_then_someone_saves)

    events, error = _export(paid, dest)

    assert isinstance(error, ConfigError)
    assert dest.read_bytes() == b"saved meanwhile"
    assert sorted(p.name for p in out_dir.iterdir()) == ["Example.ipa"], "temp file removed"
    assert events[-1]["ok"] is False
    assert all(e["type"] != "ipa-exported" for e in events)


def test_without_hard_links_it_still_will_not_replace(
    paid, out_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drive that can't hard-link (FAT, exFAT, some shares) looks, then
    renames."""

    def no_links(src, dst):
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(cli_mod.os, "link", no_links)
    dest = out_dir / "Example.ipa"

    _, error = _export(paid, dest)

    assert error is None, error
    assert dest.read_bytes().endswith(MARK)
    assert sorted(p.name for p in out_dir.iterdir()) == ["Example.ipa"]

    other = out_dir / "Other.ipa"
    cyan = cli_mod.run_cyan

    def cyan_then_someone_saves(*a, **k):
        result = cyan(*a, **k)
        other.write_bytes(b"saved meanwhile")
        return result

    monkeypatch.setattr(cli_mod, "run_cyan", cyan_then_someone_saves)
    _, error = _export(paid, other)

    assert isinstance(error, ConfigError)
    assert other.read_bytes() == b"saved meanwhile"


# --- whole or not at all -------------------------------------------------------------


def _during_the_write(monkeypatch: pytest.MonkeyPatch, module, name: str, stand_in) -> None:
    """Replace `module.name` only while the export's own write runs.

    `shutil.copyfileobj` and `tempfile.mkstemp` are used all over a build —
    unzipping for the decryption check, for one — so replacing them for the
    whole build would fail it somewhere else first.
    """
    write = cli_mod._write_export

    def scoped(*a, **k):
        with monkeypatch.context() as m:
            m.setattr(module, name, stand_in)
            return write(*a, **k)

    monkeypatch.setattr(cli_mod, "_write_export", scoped)



def test_a_write_that_fails_leaves_nothing_behind(
    paid, out_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def disk_full(src, dst, length=0):
        dst.write(b"half an IPA")
        raise OSError(errno.ENOSPC, "No space left on device")

    _during_the_write(monkeypatch, shutil, "copyfileobj", disk_full)
    dest = out_dir / "Example.ipa"

    events, error = _export(paid, dest)

    assert error is not None
    assert "space" in (error.hint or "")
    assert list(out_dir.iterdir()) == [], "no half-written file, no temp file"
    assert events[-1]["ok"] is False


def test_a_folder_macos_will_not_let_graft_write_says_so(
    paid, out_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tempfile

    def denied(*a, **k):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    _during_the_write(monkeypatch, tempfile, "mkstemp", denied)

    _, error = _export(paid, out_dir / "Example.ipa")

    assert error is not None
    assert "Privacy & Security" in (error.hint or "")


def test_a_stop_mid_write_leaves_nothing_behind(
    paid, out_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graft_ipa.errors import Stopped

    def stopped(src, dst, length=0):
        dst.write(b"half an IPA")
        raise Stopped(15)

    _during_the_write(monkeypatch, shutil, "copyfileobj", stopped)
    out = StringIO()

    with pytest.raises(Stopped):
        cli_mod._build_one(
            cfg=paid, app_name="example", dry_run=False, skip_install=False,
            skip_decrypt_check=False, verbose=False, device=None,
            emitter=JsonEmitter(stream=out), export_to=out_dir / "Example.ipa",
        )

    assert list(out_dir.iterdir()) == []
    end = json.loads(out.getvalue().splitlines()[-1])
    assert end["type"] == "build-end" and end["stopped"] is True


# --- where it can write --------------------------------------------------------------


def test_a_folder_is_refused(paid, out_dir: Path) -> None:
    _, error = _export(paid, out_dir)

    assert isinstance(error, ConfigError)
    assert "is a folder" in error.message
    assert paid.injected == []


def test_a_missing_folder_is_refused(paid, out_dir: Path) -> None:
    dest = out_dir / "not-there" / "Example.ipa"

    _, error = _export(paid, dest)

    assert isinstance(error, ConfigError)
    assert "no folder" in error.message
    assert not dest.parent.exists()
    assert paid.injected == []


# --- a dry run ------------------------------------------------------------------------


def test_a_dry_run_writes_nothing_and_says_where_it_would(paid, out_dir: Path) -> None:
    dest = out_dir / "Example.ipa"

    events, error = _export(paid, dest, dry_run=True)

    assert error is None, error
    assert list(out_dir.iterdir()) == []
    exported = events[-2]
    assert exported["type"] == "ipa-exported"
    assert exported["dry_run"] is True and exported["path"] == str(dest)
    assert exported["size_bytes"] is None


# --- the verb ---------------------------------------------------------------------------


def _run(args: list[str]):
    return CliRunner().invoke(cli_mod.app, args)


def _events(stdout: str) -> list[dict]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip().startswith("{")]


def test_the_verb_exports_the_app_named_beside_it(
    paid, out_dir: Path, reached: list[str]
) -> None:
    dest = out_dir / "Example 1.0.ipa"

    result = _run(["--json", "--export-ipa", str(dest), "example"])

    assert result.exit_code == 0, result.output
    assert reached == []
    events = _events(result.stdout)
    assert [e["type"] for e in events][-2:] == ["ipa-exported", "build-end"]
    assert dest.read_bytes().endswith(MARK)
    assert not (paid.state_dir / "log.sqlite").exists()


def test_the_verb_takes_overwrite(paid, out_dir: Path) -> None:
    dest = out_dir / "Example.ipa"
    dest.write_bytes(b"theirs")

    refused = _run(["--json", "--export-ipa", str(dest), "example"])
    replaced = _run(["--json", "--export-ipa", str(dest), "--overwrite", "example"])

    assert refused.exit_code == 2, refused.output
    assert "already exists" in " ".join(refused.stderr.split())
    assert replaced.exit_code == 0, replaced.output
    assert dest.read_bytes().endswith(MARK)


def test_the_verb_previews_under_dry_run(paid, out_dir: Path) -> None:
    dest = out_dir / "Example.ipa"

    result = _run(["--json", "--dry-run", "--export-ipa", str(dest), "example"])

    assert result.exit_code == 0, result.output
    assert not dest.exists()
    assert _events(result.stdout)[-2]["dry_run"] is True


@pytest.mark.parametrize("apps", [[], ["example", "other"]])
def test_the_verb_needs_exactly_one_app(paid, out_dir: Path, apps: list[str]) -> None:
    """A build's usage error, exit 64: no build was asked for, so none starts."""
    result = _run(["--json", "--export-ipa", str(out_dir / "Example.ipa"), *apps])

    assert result.exit_code == 64, result.output
    said = " ".join(result.stderr.split())
    assert "usage: graft APP --export-ipa PATH" in said and "one app" in said
    assert result.stdout == "", "no build-start for a build that was never asked for"
    assert paid.injected == []


@pytest.mark.parametrize(
    ("extra", "named"),
    [
        (["--device", "UDID1"], "--device"),
        (["--skip-install"], "--skip-install"),
        (["--skip-install", "--for-playcover"], "--for-playcover"),
    ],
)
def test_the_verb_refuses_what_an_export_would_ignore(
    paid, out_dir: Path, reached: list[str], extra: list[str], named: str
) -> None:
    result = _run(["--json", "--export-ipa", str(out_dir / "Example.ipa"), *extra, "example"])

    assert result.exit_code == 2, result.output
    said = " ".join(result.stderr.split())
    assert f"{named}" in said and "not valid with --export-ipa" in said
    assert reached == [] and paid.injected == []
    assert list(out_dir.iterdir()) == []


def test_overwrite_without_an_export_is_refused(paid) -> None:
    """A build would drop it, and say nothing."""
    result = _run(["--overwrite", "example"])

    assert result.exit_code == 2, result.output
    said = " ".join(result.stderr.split())
    assert "--overwrite is not valid with a build" in said
    assert paid.injected == []


def test_overwrite_beside_another_verb_is_refused(paid) -> None:
    """Even a verb above the stray-flag gate, which ignores verb-only flags:
    `--overwrite` is a modifier, refused before anything runs."""
    result = _run(["--list", "--overwrite"])

    assert result.exit_code == 2, result.output
    assert "--overwrite is not valid with --list" in " ".join(result.stderr.split())


def test_skip_decrypt_check_still_applies(
    paid, out_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_mod, "verify_decrypted", _forbid([], "the decryption check"))

    result = _run(["--json", "--export-ipa", str(out_dir / "Example.ipa"),
                   "--skip-decrypt-check", "example"])

    assert result.exit_code == 0, result.output
    names = [e["name"] for e in _events(result.stdout) if e["type"] == "stage"]
    assert "verify" not in names


def test_describe_publishes_the_flags_the_event_and_the_stage(describe_proc) -> None:
    contract = json.loads(describe_proc.stdout)
    assert {"--export-ipa", "--overwrite"} <= set(contract["flags"])
    assert "ipa-exported" in contract["events"]
    assert "export" in contract["stages"]


def test_the_partial_name_is_not_one_the_startup_sweep_takes_outside_graft_folders(
    paid, out_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The temp file is hidden and beside the destination, which is never
    `cache_dir` or `built_dir` (refused above): the only folders the startup
    sweep of stale partials looks in. So a slow export can't lose its temp file
    to another Graft starting up."""
    seen: list[str] = []
    real = cli_mod.tempfile.mkstemp

    def watch(*a, **k):
        fd, name = real(*a, **k)
        seen.append(name)
        return fd, name

    monkeypatch.setattr(cli_mod.tempfile, "mkstemp", watch)

    _, error = _export(paid, out_dir / "Example.ipa")

    assert error is None, error
    temps = [Path(n) for n in seen if Path(n).parent == out_dir]
    assert temps and all(t.name.startswith(".Example.ipa.") for t in temps)
    assert not any(os.path.exists(t) for t in temps)


# --- never signs: the identity is never read --------------------------------------


@pytest.mark.parametrize("app", ["example", "other"])
def test_an_export_never_reads_the_p12_or_its_password(
    signer, out_dir: Path, reached: list[str], app: str
) -> None:
    """With a team set and the files there, a sideload build's pre-flight
    asks the keychain for the .p12's password and opens the .p12. An export
    signs nothing, so it does neither: `_setup` fails the test if it does."""
    events, error = _export(signer, out_dir / "Example.ipa", app=app)

    assert error is None, error
    assert reached == []
    assert events[-1]["ok"] is True


# --- refused early, with the reason said (EX1) --------------------------------------


def _not_root() -> None:
    if os.geteuid() == 0:
        pytest.skip("root writes through any folder's permissions")


def _stages(events: list[dict]) -> list[dict]:
    return [e for e in events if e["type"] == "stage"]


def test_a_folder_graft_cant_search_is_said_plainly(paid, out_dir: Path) -> None:
    """`Path.is_dir` raises on a folder whose parent can't be searched (on
    Python 3.13) rather than answering False. It used to escape as a raw
    PermissionError: exit 1, "a failure graft doesn't know how to explain"."""
    _not_root()
    locked = out_dir / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        events, error = _export(paid, locked / "Example.ipa")
    finally:
        locked.chmod(0o755)

    assert isinstance(error, GraftError), error
    assert "your user can't write" in (error.hint or "")
    assert _stages(events) == [] and paid.injected == []
    assert events[-1]["reason"] == "unwritable"


def test_a_read_only_folder_fails_before_the_build_not_after(paid, out_dir: Path) -> None:
    """It used to pass the early check and fail at the write, after the whole
    build. A file made and removed there first finds out up front."""
    _not_root()
    out_dir.chmod(0o555)
    try:
        events, error = _export(paid, out_dir / "Example.ipa")
    finally:
        out_dir.chmod(0o755)

    assert isinstance(error, GraftError), error
    assert "your user can't write" in (error.hint or "")
    assert _stages(events) == [], "refused before the first stage"
    assert paid.injected == [], "cyan never ran"
    assert list(out_dir.iterdir()) == []
    assert events[-1]["ok"] is False and events[-1]["reason"] == "unwritable"


def _mkstemp_refused_in(folder: Path, code: int, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """`tempfile.mkstemp` fails with `code` in `folder` and works elsewhere.
    Returns the folders it was asked for, in order."""
    real = cli_mod.tempfile.mkstemp
    asked: list[Path] = []

    def mkstemp(suffix=None, prefix=None, dir=None, text=False):
        asked.append(Path(dir) if dir is not None else Path())
        if dir is not None and Path(dir) == folder:
            raise PermissionError(code, os.strerror(code))
        return real(suffix=suffix, prefix=prefix, dir=dir, text=text)

    monkeypatch.setattr(cli_mod.tempfile, "mkstemp", mkstemp)
    return asked


def test_a_folder_macos_wont_let_graft_write_fails_before_the_build(
    paid, out_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EPERM is macOS's privacy controls (Files and Folders), which only the
    attempt to write finds: the folder's permissions say yes."""
    _mkstemp_refused_in(out_dir, errno.EPERM, monkeypatch)

    events, error = _export(paid, out_dir / "Example.ipa")

    assert error is not None
    assert "Privacy & Security" in (error.hint or "")
    assert _stages(events) == [] and paid.injected == []
    assert events[-1]["reason"] == "unwritable"


def test_the_probe_leaves_nothing_and_the_export_still_writes(
    paid, out_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _mkstemp_refused_in(Path("/nowhere"), errno.EPERM, monkeypatch)

    _, error = _export(paid, out_dir / "Example.ipa")

    assert error is None, error
    assert asked.count(out_dir) == 2, "the probe, then the write"
    assert sorted(p.name for p in out_dir.iterdir()) == ["Example.ipa"]


def test_a_dry_run_writes_nothing_to_find_out_either(
    paid, out_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry run writes nothing anywhere, not even the probe's file: it asks
    the folder's permissions instead."""
    asked = _mkstemp_refused_in(Path("/nowhere"), errno.EPERM, monkeypatch)

    _, error = _export(paid, out_dir / "Example.ipa", dry_run=True)

    assert error is None, error
    assert out_dir not in asked


def test_a_dry_run_still_refuses_a_read_only_folder(paid, out_dir: Path) -> None:
    _not_root()
    out_dir.chmod(0o555)
    try:
        events, error = _export(paid, out_dir / "Example.ipa", dry_run=True)
    finally:
        out_dir.chmod(0o755)

    assert error is not None and "your user can't write" in (error.hint or "")
    assert events[-1]["reason"] == "unwritable"


def test_a_tilde_for_a_user_this_mac_doesnt_have_is_said_plainly(paid) -> None:
    """`Path.expanduser` raises RuntimeError for `~nosuchuser`, which used to
    escape: exit 1, and no hint."""
    result = _run(["--json", "--export-ipa", "~nosuchuser-graft/Example.ipa", "example"])

    assert result.exit_code == 2, result.output
    said = " ".join(result.stderr.split())
    assert "~nosuchuser-graft" in said and "home folder" in said
    assert _events(result.stdout)[-1]["reason"] == "bad-destination"
    assert paid.injected == []


# --- Graft's own folders, however they're spelled (EX2) -----------------------------


def _same_folder_differently_spelled(folder: Path, spelled: Path) -> None:
    if not spelled.is_dir():
        pytest.skip(f"this volume tells {spelled.name!r} from {folder.name!r}")
    assert str(spelled) != str(folder)


@pytest.mark.parametrize("folder", ["cache_dir", "built_dir"])
def test_a_graft_folder_in_other_letter_case_is_still_refused(paid, folder: str) -> None:
    """APFS and HFS+ are case-insensitive unless formatted otherwise, so
    `…/graft/IPAS` is `cache_dir`. Compared as strings, it wasn't."""
    target = getattr(paid, folder)
    (target / "sub").mkdir(parents=True, exist_ok=True)
    spelled = target.parent / target.name.upper()
    _same_folder_differently_spelled(target, spelled)

    for dest in (spelled / "x.ipa", spelled / "SUB" / "x.ipa"):
        events, error = _export(paid, dest)

        assert isinstance(error, ConfigError), (dest, error)
        assert "Graft's own folder" in error.message
        assert events[-1]["reason"] == "bad-destination"
        assert not dest.exists()
    assert paid.injected == []


@pytest.mark.parametrize("folder", ["cache_dir", "built_dir"])
def test_a_graft_folder_spelled_decomposed_is_still_refused(
    paid, tmp_path: Path, folder: str
) -> None:
    """APFS doesn't tell a precomposed é from e and a combining accent apart,
    and a path typed or pasted can carry either."""
    composed = unicodedata.normalize("NFC", "Café")
    decomposed = unicodedata.normalize("NFD", "Café")
    setattr(paid, folder, tmp_path / composed / folder)
    getattr(paid, folder).mkdir(parents=True)
    spelled = tmp_path / decomposed / folder
    _same_folder_differently_spelled(getattr(paid, folder), spelled)

    _, error = _export(paid, spelled / "x.ipa")

    assert isinstance(error, ConfigError), error
    assert "Graft's own folder" in error.message


def test_a_link_to_a_graft_folder_is_refused(paid, tmp_path: Path) -> None:
    """Through a link, and through `..` after one, which is where the link
    leads's parent, not the folder the link sits in."""
    (paid.cache_dir / "sub").mkdir(parents=True, exist_ok=True)
    link = tmp_path / "linked"
    link.symlink_to(paid.cache_dir)
    to_sub = tmp_path / "linked-sub"
    to_sub.symlink_to(paid.cache_dir / "sub")

    for dest in (link / "x.ipa", to_sub / ".." / "x.ipa"):
        _, error = _export(paid, dest)

        assert isinstance(error, ConfigError), (dest, error)
        assert "Graft's own folder" in error.message
    assert not (tmp_path / "x.ipa").exists()


def test_a_folder_beside_a_graft_folder_is_not_one(paid, tmp_path: Path) -> None:
    """Identity, not a prefix of the name: `ipas-exports` beside `ipas` is
    the user's. Nor is the folder macOS gives an app to write a file it then
    moves into place, which is where Graft.app's export writes."""
    beside = paid.cache_dir.parent / f"{paid.cache_dir.name}-exports"
    staging = tmp_path / "TemporaryItems" / "NSIRD_Graft_x"
    for folder in (beside, staging):
        folder.mkdir(parents=True)

        _, error = _export(paid, folder / "Example.ipa")

        assert error is None, (folder, error)
        assert (folder / "Example.ipa").read_bytes().endswith(MARK)


# --- the file's mode (EX3) ----------------------------------------------------------------


@pytest.mark.parametrize(("umask", "mode"), [(0o022, 0o644), (0o027, 0o640), (0o077, 0o600)])
def test_the_files_mode_is_what_the_umask_makes_it(
    paid, out_dir: Path, monkeypatch: pytest.MonkeyPatch, umask: int, mode: int
) -> None:
    """As any new file's is, rather than `mkstemp`'s 0600 whatever the umask."""
    monkeypatch.setattr(cli_mod, "_UMASK", umask)
    dest = out_dir / "Example.ipa"

    _, error = _export(paid, dest)

    assert error is None, error
    assert stat.S_IMODE(dest.stat().st_mode) == mode


def test_overwrite_doesnt_narrow_a_files_mode(
    paid, out_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_mod, "_UMASK", 0o022)
    dest = out_dir / "Example.ipa"
    dest.write_bytes(b"theirs")
    dest.chmod(0o644)

    _, error = _export(paid, dest, overwrite=True)

    assert error is None, error
    assert stat.S_IMODE(dest.stat().st_mode) == 0o644, "was turned into 0600"


def test_the_umask_is_read_and_put_back() -> None:
    old = os.umask(0o027)
    try:
        assert cli_mod._read_umask() == 0o027
        assert os.umask(0o027) == 0o027, "left changed"
    finally:
        os.umask(old)


# --- build-end says why, for an agent (EX6) ----------------------------------------------


def test_a_successful_exports_build_end_has_no_reason(paid, out_dir: Path) -> None:
    events, _ = _export(paid, out_dir / "Example.ipa")

    assert events[-1]["ok"] is True and events[-1]["reason"] is None


def test_a_builds_build_end_has_no_reason_field(paid) -> None:
    out = StringIO()
    with pytest.raises(GraftError):
        cli_mod._build_one(
            cfg=paid, app_name="example", dry_run=False, skip_install=True,
            skip_decrypt_check=False, verbose=False, device=None,
            emitter=JsonEmitter(stream=out),
        )
    end = json.loads(out.getvalue().splitlines()[-1])
    assert end["type"] == "build-end" and "reason" not in end


def test_the_reasons(paid, out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    taken = out_dir / "Taken.ipa"
    taken.write_bytes(b"theirs")
    assert _export(paid, taken)[0][-1]["reason"] == "exists"
    assert _export(paid, out_dir)[0][-1]["reason"] == "bad-destination"
    assert _export(paid, out_dir / "gone" / "x.ipa")[0][-1]["reason"] == "bad-destination"

    def failing(code: int):
        def copy(src, dst, length=0):
            raise OSError(code, os.strerror(code))
        return copy

    for code, reason in [(errno.ENOSPC, "no-space"), (errno.EROFS, "unwritable"),
                         (errno.EIO, "write-failed")]:
        with monkeypatch.context() as m:
            _during_the_write(m, shutil, "copyfileobj", failing(code))
            events, error = _export(paid, out_dir / "Example.ipa")
        assert events[-1]["reason"] == reason, (code, error)

    monkeypatch.setattr(tools, "which", fake_path(out_dir.parent / "bin3", missing=["cyan"]))
    events, _ = _export(paid, out_dir / "Example.ipa")
    assert events[-1]["ok"] is False and events[-1]["reason"] is None, "a failed pre-flight"


def test_nothing_staged_is_no_ipa(paid, out_dir: Path) -> None:
    (paid.cache_dir / "com.example.app_1.0.ipa").unlink()

    events, error = _export(paid, out_dir / "Example.ipa")

    assert isinstance(error, IPANotFoundError)
    assert events[-1]["reason"] == "no-ipa"


def test_a_file_that_appears_mid_build_is_exists_too(
    paid, out_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = out_dir / "Example.ipa"
    cyan = cli_mod.run_cyan

    def cyan_then_someone_saves(*a, **k):
        result = cyan(*a, **k)
        dest.write_bytes(b"saved meanwhile")
        return result

    monkeypatch.setattr(cli_mod, "run_cyan", cyan_then_someone_saves)

    events, _ = _export(paid, dest)

    assert events[-1]["reason"] == "exists"


def test_a_stopped_export_has_no_reason(
    paid, out_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graft_ipa.errors import Stopped

    def stopped(src, dst, length=0):
        raise Stopped(15)

    _during_the_write(monkeypatch, shutil, "copyfileobj", stopped)
    out = StringIO()
    with pytest.raises(Stopped):
        cli_mod._build_one(
            cfg=paid, app_name="example", dry_run=False, skip_install=False,
            skip_decrypt_check=False, verbose=False, device=None,
            emitter=JsonEmitter(stream=out), export_to=out_dir / "Example.ipa",
        )
    end = json.loads(out.getvalue().splitlines()[-1])
    assert end["stopped"] is True and end["reason"] is None


@pytest.mark.parametrize(
    ("setup", "code", "reason"),
    [("exists", 2, "exists"), ("read-only", 1, "unwritable"), ("nothing staged", 4, "no-ipa")],
)
def test_the_verbs_exit_codes_and_reasons(
    paid, out_dir: Path, setup: str, code: int, reason: str
) -> None:
    _not_root()
    dest = out_dir / "Example.ipa"
    if setup == "exists":
        dest.write_bytes(b"theirs")
    elif setup == "read-only":
        out_dir.chmod(0o555)
    else:
        (paid.cache_dir / "com.example.app_1.0.ipa").unlink()
    try:
        result = _run(["--json", "--export-ipa", str(dest), "example"])
    finally:
        out_dir.chmod(0o755)

    assert result.exit_code == code, result.output
    assert _events(result.stdout)[-1]["reason"] == reason


def test_every_reason_is_documented_for_people_and_agents() -> None:
    root = Path(__file__).resolve().parents[1]
    for doc in ("README.md", "AGENTS.md"):
        text = (root / doc).read_text()
        for reason in cli_mod.EXPORT_FAILURE_REASONS:
            assert f"`{reason}`" in text, f"{reason} isn't in {doc}"
