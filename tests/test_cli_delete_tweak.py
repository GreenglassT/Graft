from __future__ import annotations

import json
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest

from graft_ipa.config import load_config, set_app_tweaks
from graft_ipa.errors import TweakInUseError, TweakNotFoundError
from graft_ipa.json_output import JsonEmitter


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00")
    return path


def _filesystem_is_case_insensitive(directory: Path) -> bool:
    """True if `directory` folds case, like the default macOS/APFS volume."""
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / "case_probe_a1b2.tmp"
    probe.write_bytes(b"\x00")
    try:
        return probe.with_name(probe.name.upper()).is_file()
    finally:
        probe.unlink()


def test_deletes_an_orphan_tweak_to_the_trash(
    temp_config_env: Path, tmp_path: Path
) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    orphan = _touch(cfg.tweaks_dir / "orphan.deb")
    trash = tmp_path / "Trash"

    buf = StringIO()
    cli_mod._do_delete_tweak(
        cfg, filename="orphan.deb",
        emitter=JsonEmitter(stream=buf), trash_dir=trash,
    )

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    ev = next(e for e in events if e["type"] == "tweak-deleted")
    assert ev["filename"] == "orphan.deb"
    assert ev["trash_path"] == str(trash / "orphan.deb")
    assert not orphan.exists()
    assert (trash / "orphan.deb").exists()


def test_refuses_to_delete_a_tweak_that_is_attached(
    temp_config_env: Path, tmp_path: Path
) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    attached_name = cfg.get_app("example").tweaks[0]
    attached = _touch(cfg.tweaks_dir / attached_name)

    with pytest.raises(TweakInUseError) as excinfo:
        cli_mod._do_delete_tweak(
            cfg, filename=attached_name,
            trash_dir=tmp_path / "Trash",
        )

    assert "example" in str(excinfo.value)
    assert attached.exists(), "a blocked delete must not touch the file"


def test_in_use_error_names_every_attached_app(
    temp_config_env: Path, tmp_path: Path
) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import add_app, set_app_tweaks

    cfg = load_config(temp_config_env)
    _touch(cfg.tweaks_dir / "shared.deb")
    # The default config ships exactly one app (example), so add a second.
    add_app(cfg.path, "delta", {"bundle_id": "com.delta.app"})
    set_app_tweaks(cfg.path, "example", ["shared.deb"])
    set_app_tweaks(cfg.path, "delta", ["shared.deb"])
    cfg = load_config(temp_config_env)

    with pytest.raises(TweakInUseError) as excinfo:
        cli_mod._do_delete_tweak(
            cfg, filename="shared.deb",
            trash_dir=tmp_path / "Trash",
        )

    message = str(excinfo.value)
    assert "example" in message and "delta" in message


def test_missing_tweak_raises_tweak_not_found(
    temp_config_env: Path, tmp_path: Path
) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    with pytest.raises(TweakNotFoundError):
        cli_mod._do_delete_tweak(
            cfg, filename="ghost.deb",
            trash_dir=tmp_path / "Trash",
        )


def test_tweak_in_use_error_exit_code_is_16() -> None:
    assert TweakInUseError("x").exit_code == 16


def test_os_error_is_translated_to_a_graft_error(
    temp_config_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cli._fatal only catches GraftError; a raw OSError would escape as a
    traceback and, under --json, leave the GUI with no failure event."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.errors import GraftError

    cfg = load_config(temp_config_env)
    _touch(cfg.tweaks_dir / "orphan.deb")

    def _boom(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(cli_mod, "move_to_trash", _boom)

    with pytest.raises(GraftError) as excinfo:
        cli_mod._do_delete_tweak(
            cfg, filename="orphan.deb",
            trash_dir=tmp_path / "Trash",
        )
    assert not isinstance(excinfo.value, OSError)
    assert "Permission denied" in str(excinfo.value)


def test_path_traversal_is_rejected_and_victim_survives(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """Path.__truediv__ rejects neither ".." nor absolute operands, so
    `cfg.tweaks_dir / filename` alone would happily resolve outside
    tweaks_dir. tmp_path/cfg/graft/tweaks is exactly 3 levels below
    tmp_path, so "../../../victim.txt" from tweaks_dir lands on the victim
    built here."""
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    cfg.tweaks_dir.mkdir(parents=True, exist_ok=True)
    assert cfg.tweaks_dir == tmp_path / "cfg" / "graft" / "tweaks"
    victim = tmp_path / "victim.txt"
    victim.write_text("important")

    with pytest.raises(TweakNotFoundError):
        cli_mod._do_delete_tweak(
            cfg, filename="../../../victim.txt",
            trash_dir=tmp_path / "Trash",
        )

    assert victim.exists(), "traversal must never touch a file outside tweaks_dir"


def test_absolute_path_is_rejected_and_victim_survives(
    temp_config_env: Path, tmp_path: Path
) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    cfg.tweaks_dir.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "victim_abs.txt"
    victim.write_text("important")

    with pytest.raises(TweakNotFoundError):
        cli_mod._do_delete_tweak(
            cfg, filename=str(victim),
            trash_dir=tmp_path / "Trash",
        )

    assert victim.exists(), "an absolute path must never be honored"


def test_dotfile_is_rejected(temp_config_env: Path, tmp_path: Path) -> None:
    """.DS_Store passes .is_file() but --tweaks never lists it (the listing
    rule skips dotfiles), so --delete-tweak must not accept it either."""
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    ds_store = _touch(cfg.tweaks_dir / ".DS_Store")

    with pytest.raises(TweakNotFoundError):
        cli_mod._do_delete_tweak(
            cfg, filename=".DS_Store",
            trash_dir=tmp_path / "Trash",
        )

    assert ds_store.exists()


def test_mis_cased_attached_tweak_raises_in_use(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """macOS/APFS is case-insensitive by default, so A-TWEAK.DEB passes
    .is_file() while the old `filename in app.tweaks` string check missed it —
    deleting a tweak that's still attached. This is only meaningful if the
    filesystem backing tmp_path actually folds case; skip rather than pass
    vacuously on a case-sensitive volume."""
    from graft_ipa import cli as cli_mod

    if not _filesystem_is_case_insensitive(tmp_path):
        pytest.skip("filesystem backing tmp_path is case-sensitive; probe not meaningful here")

    cfg = load_config(temp_config_env)
    attached_name = cfg.get_app("example").tweaks[0]
    attached = _touch(cfg.tweaks_dir / attached_name)
    mis_cased = attached_name.swapcase()
    assert mis_cased != attached_name, "fixture tweak name has no case to flip"

    with pytest.raises(TweakInUseError) as excinfo:
        cli_mod._do_delete_tweak(
            cfg, filename=mis_cased,
            trash_dir=tmp_path / "Trash",
        )

    assert "example" in str(excinfo.value)
    assert attached.exists(), "a blocked delete must not touch the file"


def test_path_form_config_entry_protects_its_file(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """Config.tweak_path explicitly supports absolute paths ("advanced
    users"). A tweak referenced that way must still be recognized as in-use
    when deleted by its bare filename — the old check keyed on the raw
    config string and missed this entirely."""
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    target = _touch(cfg.tweaks_dir / "pathform.deb")
    set_app_tweaks(cfg.path, "example", [str(target)])
    cfg = load_config(temp_config_env)

    with pytest.raises(TweakInUseError) as excinfo:
        cli_mod._do_delete_tweak(
            cfg, filename="pathform.deb",
            trash_dir=tmp_path / "Trash",
        )

    assert "example" in str(excinfo.value)
    assert target.exists()


def test_mis_cased_config_entry_protects_its_file(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """The other half of the case-folding hole. `--attach-tweak SOME-TWEAK.deb`
    passes `src.is_file()` on case-insensitive APFS and writes the literal
    "SOME-TWEAK.deb" into the config, but the file on disk is some-tweak.deb. The delete
    guard keyed on the raw config string, so it saw no consumer and trashed a
    file the config still referenced. Identity (inode) matching closes it."""
    from graft_ipa import cli as cli_mod

    if not _filesystem_is_case_insensitive(tmp_path):
        pytest.skip("filesystem backing tmp_path is case-sensitive; probe not meaningful here")

    cfg = load_config(temp_config_env)
    target = _touch(cfg.tweaks_dir / "some-tweak.deb")
    set_app_tweaks(cfg.path, "example", ["SOME-TWEAK.deb"])
    cfg = load_config(temp_config_env)

    with pytest.raises(TweakInUseError) as excinfo:
        cli_mod._do_delete_tweak(
            cfg, filename="some-tweak.deb", trash_dir=tmp_path / "Trash",
        )

    assert "example" in str(excinfo.value)
    assert target.exists()


def test_symlinked_config_entry_protects_its_target(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """Inode identity also folds a symlink onto its target, so deleting the
    real file while an app references it through a link is refused."""
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    target = _touch(cfg.tweaks_dir / "real.deb")
    link = tmp_path / "link.deb"
    link.symlink_to(target)
    set_app_tweaks(cfg.path, "example", [str(link)])
    cfg = load_config(temp_config_env)

    with pytest.raises(TweakInUseError) as excinfo:
        cli_mod._do_delete_tweak(
            cfg, filename="real.deb", trash_dir=tmp_path / "Trash",
        )

    assert "example" in str(excinfo.value)
    assert target.exists()


def test_dangling_config_entry_is_not_confused_with_another_file(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """tweak_key falls back to a normalized path string when the file is
    missing. That fallback must not collide with a real file's identity —
    otherwise a dangling `ghost.deb` entry would block deleting an unrelated
    orphan (or, worse, make it look used)."""
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    orphan = _touch(cfg.tweaks_dir / "orphan.deb")
    set_app_tweaks(cfg.path, "example", ["ghost.deb"])
    cfg = load_config(temp_config_env)

    assert cfg.tweak_key("ghost.deb") != cfg.tweak_key("orphan.deb")
    trash = tmp_path / "Trash"
    cli_mod._do_delete_tweak(cfg, filename="orphan.deb", trash_dir=trash)
    assert not orphan.exists()
    assert (trash / "orphan.deb").exists()


def test_dangling_config_entry_is_stable_against_itself(
    temp_config_env: Path,
) -> None:
    """The string fallback has to be self-consistent, or a dangling entry
    would never match itself and detach could never remove it."""
    cfg = load_config(temp_config_env)
    assert cfg.tweak_key("ghost.deb") == cfg.tweak_key("ghost.deb")
    assert cfg.tweak_key("ghost.deb") == cfg.tweak_key(str(cfg.tweaks_dir / "ghost.deb"))


def test_detach_then_delete_round_trip_for_path_form_entry(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """The delete guard's hint says "detach it from those apps first", so
    detach must be able to satisfy it. `_do_detach_tweak` filtered
    `app.tweaks` by exact string, which can't match a path-form entry when the
    user detaches by the bare filename --tweaks showed them: detach exited 0
    reporting success, changed nothing, and delete refused again forever."""
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    target = _touch(cfg.tweaks_dir / "pathform.deb")
    set_app_tweaks(cfg.path, "example", [str(target)])
    cfg = load_config(temp_config_env)

    with pytest.raises(TweakInUseError):
        cli_mod._do_delete_tweak(cfg, filename="pathform.deb", trash_dir=tmp_path / "Trash")

    cli_mod._do_detach_tweak(cfg, app_name="example", filename="pathform.deb")
    assert load_config(temp_config_env).get_app("example").tweaks == []

    cfg = load_config(temp_config_env)
    cli_mod._do_delete_tweak(cfg, filename="pathform.deb", trash_dir=tmp_path / "Trash")
    assert not target.exists()


def test_detach_by_bare_filename_removes_a_mis_cased_entry(
    temp_config_env: Path, tmp_path: Path
) -> None:
    """Same round trip for the case-folding spelling: `--attach-tweak SOME-TWEAK.deb`
    writes "SOME-TWEAK.deb" while the file is some-tweak.deb, and the user detaches using
    the name --tweaks printed."""
    from graft_ipa import cli as cli_mod

    if not _filesystem_is_case_insensitive(tmp_path):
        pytest.skip("filesystem backing tmp_path is case-sensitive; probe not meaningful here")

    cfg = load_config(temp_config_env)
    target = _touch(cfg.tweaks_dir / "some-tweak.deb")
    set_app_tweaks(cfg.path, "example", ["SOME-TWEAK.deb"])
    cfg = load_config(temp_config_env)

    cli_mod._do_detach_tweak(cfg, app_name="example", filename="some-tweak.deb")
    assert load_config(temp_config_env).get_app("example").tweaks == []

    cfg = load_config(temp_config_env)
    cli_mod._do_delete_tweak(cfg, filename="some-tweak.deb", trash_dir=tmp_path / "Trash")
    assert not target.exists()


def test_detach_removes_every_spelling_of_the_same_file(
    temp_config_env: Path,
) -> None:
    """Deliberate behaviour change: filtering by identity rather than by
    string means a config that lists the same file twice under different
    spellings loses BOTH entries on one detach. That's the right outcome —
    leaving the duplicate behind would keep the delete guard tripping — but
    it is a change, so pin it."""
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    target = _touch(cfg.tweaks_dir / "dupe.deb")
    other = _touch(cfg.tweaks_dir / "keep.deb")
    set_app_tweaks(cfg.path, "example", ["dupe.deb", str(target), "keep.deb"])
    cfg = load_config(temp_config_env)

    cli_mod._do_detach_tweak(cfg, app_name="example", filename="dupe.deb")

    assert load_config(temp_config_env).get_app("example").tweaks == ["keep.deb"]
    assert target.exists() and other.exists(), "detach must not touch the files"


def test_in_use_lookup_os_error_is_translated(
    temp_config_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-use lookup stats the target and every configured tweak. Before
    the identity rewrite this region also called `tweaks_dir.iterdir()` and
    `samefile()` outside any handler, so an unreadable tweaks_dir escaped
    `_do_delete_tweak` as a raw PermissionError traceback — leaving --json
    consumers with a dead process and no failure event."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.errors import GraftError

    cfg = load_config(temp_config_env)
    _touch(cfg.tweaks_dir / "orphan.deb")

    def _boom(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(cli_mod, "_tweak_consumers", _boom)

    with pytest.raises(GraftError) as excinfo:
        cli_mod._do_delete_tweak(
            cfg, filename="orphan.deb", trash_dir=tmp_path / "Trash",
        )
    assert not isinstance(excinfo.value, OSError)
    assert "still in use" in str(excinfo.value)


def _subprocess_env(config_path: Path) -> dict[str, str]:
    """Isolated PATH/HOME/XDG_* env for a `python -m graft_ipa.cli`
    subprocess, rooted at the same tmp_path `temp_config_env` used.

    Pointing HOME here (not just the XDG_* vars) also exercises the real
    `default_trash_dir()` (~/.Trash) branch, which no other test touches.
    """
    tmp_path = config_path.parent.parent.parent
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(config_path.parent.parent),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }


def test_delete_tweak_subprocess_reports_in_use_via_exit_16(
    temp_config_env: Path,
) -> None:
    """The production wiring (main() -> _fatal() -> typer.Exit(code=16)) had
    zero automated coverage before this — exit 16 was only ever verified by
    hand. Assert the GUI's actual subprocess contract: nonzero exit, the
    error on stderr, and — since the refusal became structured — a
    `tweak-in-use` event on stdout that the GUI's parser recognises.

    That last assertion used to be `stdout == ""`, on the reasoning that the
    JSON-event parser must not choke. It still must not, but the way to
    guarantee that is a registered event, not an empty stream: the GUI offers
    "Detach and Delete" off this app list and was otherwise reduced to finding
    "is attached to:" in the stderr prose."""
    cfg = load_config(temp_config_env)
    attached_name = cfg.get_app("example").tweaks[0]
    _touch(cfg.tweaks_dir / attached_name)

    proc = subprocess.run(
        [sys.executable, "-m", "graft_ipa.cli", "--json", "--delete-tweak", attached_name],
        env=_subprocess_env(temp_config_env),
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent.parent),
    )

    assert proc.returncode == 16
    assert "attached to" in proc.stderr

    events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert [e["type"] for e in events] == ["tweak-in-use"], (
        "the refusal must put exactly one event on the wire"
    )
    assert events[0]["filename"] == attached_name
    assert events[0]["used_by"] == ["example"]


def test_delete_tweak_subprocess_on_unwritable_dir_reports_cleanly(
    temp_config_env: Path,
) -> None:
    """The whole point of translating OSError: the GUI must get an error, not
    a traceback. With tweaks_dir read-only the trash move fails with EACCES;
    the process must still exit with a graft code and a one-line
    `error:` on stderr rather than dumping a Rich traceback."""
    import os

    if os.geteuid() == 0:
        pytest.skip("running as root bypasses the directory permission bits")

    cfg = load_config(temp_config_env)
    _touch(cfg.tweaks_dir / "orphan.deb")
    set_app_tweaks(cfg.path, "example", [])
    cfg.tweaks_dir.chmod(0o500)  # r-x: readable, but nothing can be unlinked
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "graft_ipa.cli", "--json", "--delete-tweak", "orphan.deb"],
            env=_subprocess_env(temp_config_env),
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent),
        )
    finally:
        cfg.tweaks_dir.chmod(0o700)

    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr and "PermissionError" not in proc.stderr
    assert "error:" in proc.stderr
    assert proc.stdout.strip() == ""


def test_a_refusal_emits_the_blocking_apps_as_data(temp_config_env: Path) -> None:
    """The GUI offers "Detach and Delete" off this list. Recovering it by
    finding "is attached to:" in stderr was the app's one dependency on an
    English sentence, and the only CLI/GUI coupling with none of the contract
    checking the rest get — rewording the message would have degraded that
    button silently, because the Swift test fakes carry the sentence too.
    """
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    attached_name = cfg.get_app("example").tweaks[0]
    _touch(cfg.tweaks_dir / attached_name)

    out = StringIO()
    with pytest.raises(TweakInUseError):
        cli_mod._do_delete_tweak(cfg, filename=attached_name, emitter=JsonEmitter(stream=out))

    events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert len(events) == 1
    assert events[0]["type"] == "tweak-in-use"
    assert events[0]["filename"] == attached_name
    assert events[0]["used_by"] == ["example"]


def test_the_refusal_leaves_the_file_alone(temp_config_env: Path) -> None:
    """It reports a refusal, not work done."""
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    attached_name = cfg.get_app("example").tweaks[0]
    target = cfg.tweaks_dir / attached_name
    _touch(target)

    out = StringIO()
    with pytest.raises(TweakInUseError):
        cli_mod._do_delete_tweak(cfg, filename=attached_name, emitter=JsonEmitter(stream=out))

    assert target.exists()


def test_no_event_without_an_emitter(temp_config_env: Path) -> None:
    """A terminal user gets the prose on stderr; stdout carries the event
    stream and stays untouched when nothing asked for JSON."""
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    attached_name = cfg.get_app("example").tweaks[0]
    _touch(cfg.tweaks_dir / attached_name)

    with pytest.raises(TweakInUseError):
        cli_mod._do_delete_tweak(cfg, filename=attached_name, emitter=None)
