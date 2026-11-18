"""The buyer's Apple ID data leaves every IPA Graft ships, on every door (K8, R3, R4).

- R4: a build healed a pre-1.1 staged IPA in place, unguarded, so a full or
  read-only cache failed a build that 1.0 ran fine. On that failure it now
  warns and builds from a scrubbed copy in its own work directory instead;
  only when even that copy cannot be made does it fail, since the
  alternative is shipping the data (design call: privacy over the build).
- R3: the heal said nothing until it was over, and a Stop during it, or
  during any staging or archiving, left a hidden `.….partial` file nothing
  ever swept. It says "removing Apple ID data from …" first, and stale
  partials are swept at startup.
- K8: `--reinstall-build` and `--install-to-mac` shipped their input as it
  was, so a 1.0 archive or a pre-1.1 staged IPA still carried the data. Both
  now take it out first. A zsign-signed archive keeps the `SC_Info` entries
  its signature seals: removing those would leave an IPA iOS refuses to
  install, and re-signing is a rebuild.
"""

from __future__ import annotations

import json
import os
import plistlib
import time
import zipfile
from io import StringIO
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa import data_dirs, ipa_inspect, provide_ipa
from graft_ipa.build_log import BuildLog
from graft_ipa.errors import GraftError
from graft_ipa.json_output import JsonEmitter
from graft_ipa.lc_runner import LCDeliveryResult

from .test_account_data_stripping import account_ipa, assert_identifies_nobody
from .test_free_account_build import _build, free_config  # noqa: F401 — the fixture
from .test_lc_discovery import fake_device


def _names(ipa: Path) -> set[str]:
    with zipfile.ZipFile(ipa) as zf:
        return set(zf.namelist())


# --- R4: a cache that won't take the write --------------------------------------


@pytest.fixture
def read_only(tmp_path: Path):
    """chmod a directory read-only for the test, and back so it can be removed."""
    made: list[Path] = []

    def make(path: Path) -> Path:
        path.chmod(0o500)
        made.append(path)
        return path

    yield make
    for path in made:
        path.chmod(0o700)


def test_a_read_only_cache_builds_from_a_scrubbed_copy(
    free_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_only  # noqa: F811
) -> None:
    cfg = free_config
    staged = next(cfg.cache_dir.glob("*.ipa"))
    account_ipa(staged, version="1.0", cryptid=None)
    before = staged.read_bytes()
    written = fake_device(monkeypatch, ["com.kdt.livecontainer.FGHIJ67890"])
    read_only(cfg.cache_dir)

    events = _build(cfg, tmp_path)

    assert events[-1]["type"] == "build-end" and events[-1]["ok"] is True, events[-1]
    assert staged.read_bytes() == before, "the cache could not be written, and was not"
    (pushed,) = written.values()
    shipped = tmp_path / "shipped.ipa"
    shipped.write_bytes(pushed.removesuffix(b"<tweaked>"))  # the fake cyan's mark
    assert_identifies_nobody(shipped)
    warnings = [e["line"] for e in events if e["type"] == "log" and e["source"] == "warning"]
    assert any("couldn't remove" in w and "copy" in w for w in warnings), warnings


def test_when_no_scrubbed_copy_can_be_made_the_build_fails(
    free_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    cfg = free_config
    staged = next(cfg.cache_dir.glob("*.ipa"))
    account_ipa(staged, version="1.0", cryptid=None)
    fake_device(monkeypatch, ["com.kdt.livecontainer.FGHIJ67890"])

    def full(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(provide_ipa, "scrub_staged", full)
    monkeypatch.setattr(provide_ipa, "scrubbed_copy", full)
    monkeypatch.setattr(cli_mod, "run_cyan", lambda *a, **k: pytest.fail("cyan ran"))

    with pytest.raises(GraftError) as exc:
        _build(cfg, tmp_path)

    assert "Apple ID" in exc.value.message


# --- R3: said first, and nothing left behind -----------------------------------


def test_the_heal_says_what_it_is_doing_before_it_does_it(
    free_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    cfg = free_config
    staged = next(cfg.cache_dir.glob("*.ipa"))
    account_ipa(staged, version="1.0", cryptid=None)
    fake_device(monkeypatch, ["com.kdt.livecontainer.FGHIJ67890"])

    events = _build(cfg, tmp_path)

    lines = [(e["source"], e["line"]) for e in events if e["type"] == "log"]
    progress = [i for i, (s, line) in enumerate(lines) if "removing Apple ID data from" in line]
    done = [i for i, (s, line) in enumerate(lines) if s == "warning" and "removed the Apple ID" in line]
    assert progress and done and progress[0] < done[0], lines


def _aged(path: Path, hours: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"half an IPA")
    then = time.time() - hours * 3600
    os.utime(path, (then, then))
    return path


def test_stale_partials_are_swept_at_startup(tmp_path: Path, monkeypatch) -> None:
    cfg_path = tmp_path / "xdg-config" / "graft" / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    cache, built = tmp_path / "ipas", tmp_path / "built"
    cfg_path.write_text(yaml.safe_dump({"cache_dir": str(cache), "built_dir": str(built),
                                        "apps": {}}))
    stale = [
        _aged(cache / ".com.example.app_1.0.k2j3h4ab.partial.ipa", 30),         # provide_ipa.stage
        _aged(built / ".example_1.0_20260101T000000.ipa.a8d7f6_q.partial", 30),  # private_copy
        _aged(cache / ".com.kdt.livecontainer-1a2b3c4d.part", 30),              # lc_manage.ensure_ipa
    ]
    fresh = _aged(cache / ".com.example.app_1.1.zz9y8x7w.partial.ipa", 0.1)  # another run's, now
    kept = [_aged(cache / "com.example.app_1.0.ipa", 30), _aged(built / ".hidden-but-mine", 30)]

    result = CliRunner().invoke(cli_mod.app, ["--list"])

    assert result.exit_code == 0, result.output
    assert not any(p.exists() for p in stale)
    assert fresh.exists() and all(p.exists() for p in kept)


# --- only Graft's own (P6) -------------------------------------------------------
#
# cache_dir and built_dir are settings, and either can be a folder other
# things write to. The sweep removed any hidden `.part`, `.partial` or
# `.partial.ipa` older than a day there: a browser's or a sync tool's
# half-finished download along with Graft's. It now removes only the names
# Graft's three writers give their temporaries.


def test_a_shared_folders_own_hidden_partials_are_never_swept(tmp_path: Path) -> None:
    shared = tmp_path / "Downloads"
    theirs = [
        _aged(shared / name, 30) for name in (
            ".download.part",
            ".Some Movie.mp4.part",
            ".report.pdf.partial",
            ".notes.txt.abcd1234.partial",           # an mkstemp-shaped name, not an IPA's
            ".com.example.app_1.0.partial.ipa",     # no random part: nobody's mkstemp
            ".com.example.app_1.0.abc12.partial.ipa",
            ".com.other.thing-1a2b3c4d.part",
            ".com.kdt.livecontainer-1a2b3c4d.part.old",
        )
    ]
    ours = _aged(shared / ".com.example.app_1.0.k2j3h4ab.partial.ipa", 30)

    removed = data_dirs.sweep_stale_partials(shared, shared)

    assert removed == [ours]
    assert all(p.exists() for p in theirs)


def test_every_temporary_graft_writes_there_is_one_the_sweep_removes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asked of the writers themselves, so a change to how one names its
    temporary cannot quietly leave the sweep blind to it."""
    import tempfile

    from graft_ipa import lc_manage

    made: list[str] = []
    mkstemp = tempfile.mkstemp

    def recording(*args, **kwargs):
        fd, name = mkstemp(*args, **kwargs)
        made.append(Path(name).name)
        return fd, name

    monkeypatch.setattr(tempfile, "mkstemp", recording)
    cache, built = tmp_path / "ipas", tmp_path / "built"
    cache.mkdir()
    built.mkdir()
    src = account_ipa(tmp_path / "in.ipa", version="1.0", cryptid=None)

    # A build archiving its result; a drop being staged.
    data_dirs.private_copy(src, built / "example_1.0_20260101T000000.ipa")
    provide_ipa.stage(src, cache / "com.example.app_1.0.ipa")

    # LiveContainer's own IPA being downloaded.
    def download(_url: str, part: Path) -> None:
        made.append(part.name)
        raise ConnectionError("cut off")

    with pytest.raises(ConnectionError):
        lc_manage.ensure_ipa(
            lc_manage.Release("nightly", "3.4.0", "https://example.invalid/LiveContainer.ipa"),
            cache, get_json=lambda _url: {"assets": []}, download=download,
        )

    assert len(made) == 3, made
    left = [_aged(tmp_path / "swept" / name, 30) for name in made]

    removed = data_dirs.sweep_stale_partials(tmp_path / "swept")

    assert sorted(removed) == sorted(left), made


# --- K8: the re-install and the hand-over ------------------------------------------


def _signed_account_ipa(path: Path) -> Path:
    """`account_ipa`, as zsign left it: the app's own SC_Info sealed by its
    CodeResources, the extension's not."""
    account_ipa(path, version="1.0", cryptid=None)
    seal = plistlib.dumps({"files": {}, "files2": {
        "SC_Info/App.sinf": {"hash2": b"x"}, "SC_Info/App.supp": {"hash2": b"y"},
        "marker.txt": {"hash2": b"z"},
    }})
    with zipfile.ZipFile(path, "a") as zf:
        zf.writestr("Payload/App.app/_CodeSignature/CodeResources", seal)
        zf.writestr("Payload/App.app/embedded.mobileprovision", b"profile")
    return path


def test_the_sealed_entries_are_named(tmp_path: Path) -> None:
    ipa = _signed_account_ipa(tmp_path / "a.ipa")

    assert ipa_inspect.sealed_account_data(ipa) == {
        "Payload/App.app/SC_Info/App.sinf", "Payload/App.app/SC_Info/App.supp",
    }


def _free_paths(tmp_path: Path):
    from graft_ipa.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "cache_dir": str(tmp_path / "ipas"), "built_dir": str(tmp_path / "built"),
        "state_dir": str(tmp_path / "state"), "default_device_udid": "UDID1",
        "apps": {"example": {"bundle_id": "com.example.app"}},
    }))
    return load_config(path)


def _reinstall(cfg, ipa: Path, delivery: str, monkeypatch) -> tuple[bytes, list[dict]]:
    build_id = BuildLog(cfg.state_dir / "log.sqlite").record(
        app="example", bundle_id="com.example.app", version="1.0", tweaks=[], ipa_path=ipa,
        signing_team=None, udid="UDID1", success=True, delivery=delivery,
    )
    got: dict[str, bytes] = {}
    monkeypatch.setattr(cli_mod, "select_device", lambda u: ("UDID1", "usb"))

    def push(p, **kw):
        got["ipa"] = Path(p).read_bytes()
        got["path"] = str(p)
        return LCDeliveryResult(udid="UDID1", bundle_id="com.kdt.livecontainer.ABCDE12345",
                                documents_path="Documents/example.ipa", size_bytes=1)

    monkeypatch.setattr(cli_mod.lc_runner, "push_ipa_to_livecontainer", push)
    buf = StringIO()
    cli_mod._do_reinstall_build(cfg, build_id, emitter=JsonEmitter(stream=buf))
    return got["ipa"], [json.loads(line) for line in buf.getvalue().splitlines()]


def test_a_reinstall_pushes_a_1_0_archive_without_the_account_data(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _free_paths(tmp_path)
    ipa = account_ipa(cfg.built_dir / "example_1.0_20260101T000000.ipa", version="1.0",
                      cryptid=None)

    pushed, events = _reinstall(cfg, ipa, "livecontainer", monkeypatch)

    shipped = tmp_path / "shipped.ipa"
    shipped.write_bytes(pushed)
    assert_identifies_nobody(shipped)
    assert_identifies_nobody(ipa), "healed in place, like a staged IPA"
    assert events[-1]["ok"] is True


def test_a_signed_archive_keeps_only_what_its_signature_seals(tmp_path: Path, monkeypatch) -> None:
    from graft_ipa.config import add_signing_section, load_config

    cfg = _free_paths(tmp_path)
    add_signing_section(cfg.path, cert=tmp_path / "c.p12", profile=tmp_path / "p.mp",
                        team_id="ABCDE12345")
    cli_mod._do_set_app_options(load_config(cfg.path), app_name="example", delivery="sideload")
    cfg = load_config(cfg.path)
    ipa = _signed_account_ipa(cfg.built_dir / "example_1.0_20260101T000000.ipa")
    got: dict[str, Path] = {}
    monkeypatch.setattr(cli_mod, "select_device", lambda u: ("UDID1", "usb"))

    class Installed:
        stdout = stderr = ""

    def install(p, **kw):
        got["names"] = _names(Path(p))
        return Installed()

    monkeypatch.setattr(cli_mod, "install", install)
    build_id = BuildLog(cfg.state_dir / "log.sqlite").record(
        app="example", bundle_id="com.example.app", version="1.0", tweaks=[], ipa_path=ipa,
        signing_team="ABCDE12345", udid="UDID1", success=True, delivery="sideload",
    )

    cli_mod._do_reinstall_build(cfg, build_id)

    names = got["names"]
    assert "iTunesMetadata.plist" not in names
    assert "Payload/App.app/PlugIns/Ext.appex/SC_Info/Ext.sinf" not in names
    assert {"Payload/App.app/SC_Info/App.sinf", "Payload/App.app/SC_Info/App.supp"} <= names
    assert "Payload/App.app/marker.txt" in names


def test_install_to_mac_hands_over_an_ipa_without_the_account_data(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _free_paths(tmp_path)
    staged = account_ipa(cfg.cache_dir / "com.example.app_1.0.ipa", version="1.0", cryptid=None)
    opened: list[set[str]] = []

    class Done:
        returncode = 0
        stdout = stderr = ""

    def run(cmd, **kw):
        opened.append(_names(Path(cmd[-1])))
        return Done()

    monkeypatch.setattr(cli_mod.subprocess, "run", run)
    monkeypatch.setattr(cli_mod, "_tame_playcover_settings", lambda bundle_id: None)

    cli_mod._do_install_to_mac(cfg, filename=staged.name, emitter=JsonEmitter(stream=StringIO()))

    assert opened and not [n for n in opened[0] if "SC_Info" in n or "iTunesMetadata" in n]
    assert_identifies_nobody(staged)
