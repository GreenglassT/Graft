"""The `--delete-staged` verb: `_do_delete_staged`.

The only destructive verb this feature adds, so the tests that matter are about
*when it refuses*. The refusal exists because deleting the IPA an app builds
from leaves that app unbuildable: the cache is the only place a build takes
its IPA from, so `find_in_cache` returns None and stage 2 has nothing to
acquire.

The pair that distinguishes a correct guard from a merely cautious one is
`test_it_refuses_when_it_is_the_only_staged_ipa_for_a_configured_app` against
`test_it_does_not_refuse_when_another_version_remains`. A guard written as "any
configured app claims this bundle id" passes the first and fails the second.

The guard is also decided *per claiming app*, not once for the bundle id:
`find_in_cache` filters candidates against `app.preferred_version` too, so a
sibling with the wrong version does not rescue a pinned app even though it
shares the bundle id. See
`test_it_refuses_when_a_pinned_app_would_lose_its_only_buildable_version`.
"""

from __future__ import annotations

import json
import plistlib
import subprocess
import sys
import zipfile
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from graft_ipa import cli as cli_mod
from graft_ipa import ipa_inspect
from graft_ipa.config import load_config
from graft_ipa.errors import ConfigError, GraftError, StagedIPAInUseError
from graft_ipa.json_output import EVENT_TYPES, JsonEmitter


def _events(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _ipa(cache: Path, bundle_id: str, version: str) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{bundle_id}_{version}.ipa"
    info = plistlib.dumps({
        "CFBundleIdentifier": bundle_id,
        "CFBundleShortVersionString": version,
        "CFBundleDisplayName": "Fake",
        "CFBundleExecutable": "Fake",
    })
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Payload/Fake.app/Info.plist", info)
    return path


def _cfg(cache: Path, apps: dict[str, str] | None = None) -> SimpleNamespace:
    """Enough Config for this verb: a cache dir, and the configured apps it
    checks against. `apps` maps YAML key -> bundle id, all pinned to
    "latest" — the default a real `AppConfig` carries when nothing else is
    configured. Tests that need a real pin build their own app entry."""
    entries = {
        name: SimpleNamespace(bundle_id=bid, preferred_version="latest", preferred_version_id=None)
        for name, bid in (apps or {}).items()
    }
    return SimpleNamespace(cache_dir=cache, apps=entries)


def _filesystem_is_case_insensitive(directory: Path) -> bool:
    """True if `directory` folds case, like the default macOS/APFS volume."""
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / "case_probe_a1b2.tmp"
    probe.write_bytes(b"\x00")
    try:
        return probe.with_name(probe.name.upper()).is_file()
    finally:
        probe.unlink()


def _subprocess_env(config_path: Path) -> dict[str, str]:
    """Isolated PATH/HOME/XDG_* env for a `python -m graft_ipa.cli`
    subprocess, rooted at the same tmp_path `temp_config_env` used. Mirrors
    `test_cli_delete_tweak.py`'s helper of the same name."""
    tmp_path = config_path.parent.parent.parent
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(config_path.parent.parent),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }


def test_the_event_is_registered() -> None:
    assert "staged-ipa-deleted" in EVENT_TYPES


def test_the_in_use_event_is_registered() -> None:
    assert "staged-ipa-in-use" in EVENT_TYPES


def test_staged_ipa_in_use_error_exit_code_is_17() -> None:
    """Pinned the same way `test_tweak_in_use_error_exit_code_is_16` pins its
    own precedent, so this cannot drift either."""
    assert StagedIPAInUseError("x").exit_code == 17


def test_it_removes_the_ipa_and_its_sidecar(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    path = _ipa(cache, "com.example.app", "1.0")
    ipa_inspect.mark_stripped(path, ["Payload/Fake.app/PlugIns/W.appex"], lost=[])
    sidecar = cache / (path.name + ipa_inspect.STRIPPED_SUFFIX)
    assert sidecar.exists(), "premise: the sidecar was written"

    buf = StringIO()
    cli_mod._do_delete_staged(
        _cfg(cache), filename=path.name, force=False,
        emitter=JsonEmitter(stream=buf),
    )

    assert not path.exists()
    assert not sidecar.exists()
    event = _events(buf.getvalue())[0]
    assert event["type"] == "staged-ipa-deleted"
    assert event["filename"] == path.name
    assert event["also_removed"] == [sidecar.name], (
        "the field exists so a caller can tell a sidecar went with the "
        "delete without a second stat — a constant `[]` would pass every "
        "other assertion here"
    )


def test_it_deletes_happily_when_no_app_claims_the_bundle_id(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    path = _ipa(cache, "com.example.unclaimed", "1.0")
    cli_mod._do_delete_staged(
        _cfg(cache, {"example": "com.example.app"}),
        filename=path.name, force=False, emitter=None,
    )
    assert not path.exists()


def test_it_refuses_when_it_is_the_only_staged_ipa_for_a_configured_app(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    path = _ipa(cache, "com.example.app", "21.16.2")
    with pytest.raises(StagedIPAInUseError) as e:
        cli_mod._do_delete_staged(
            _cfg(cache, {"example": "com.example.app"}),
            filename=path.name, force=False, emitter=None,
        )
    assert "example" in str(e.value)
    assert path.exists(), "a refusal must not delete"


def test_a_refusal_emits_the_blocking_apps_as_data(tmp_path: Path) -> None:
    """The GUI's future confirm needs the blocking app names as structured
    data. `_do_delete_tweak`'s own comment records that recovering
    `used_by`-equivalent data by scraping the "is the only staged IPA for:
    ..." sentence out of stderr was a shipped defect once already."""
    cache = tmp_path / "cache"
    path = _ipa(cache, "com.example.app", "21.16.2")
    buf = StringIO()
    with pytest.raises(StagedIPAInUseError):
        cli_mod._do_delete_staged(
            _cfg(cache, {"example": "com.example.app"}),
            filename=path.name, force=False, emitter=JsonEmitter(stream=buf),
        )
    event = _events(buf.getvalue())[0]
    assert event["type"] == "staged-ipa-in-use"
    assert event["filename"] == path.name
    assert event["used_by"] == ["example"]


def test_a_refusal_names_only_the_apps_it_actually_blocks(tmp_path: Path) -> None:
    """Two app entries can claim one bundle id with different pins, and a
    delete can rescue one while dooming the other. The reported list has to
    be the apps actually left with nothing (`broken`), not every app that
    merely shares the bundle id (`claimed_by`) — naming an unaffected app as
    blocked would tell the pane's future confirm to threaten an app this
    delete does not touch.

    `example` (pinned "latest") is fine after this delete: 21.16.2 remains
    and satisfies any pin of "latest". `example_locked` (pinned exactly to
    the file being deleted) is not: no remaining file matches its pin.
    """
    cache = tmp_path / "cache"
    old = _ipa(cache, "com.example.app", "21.9.1")
    _ipa(cache, "com.example.app", "21.16.2")
    cfg = SimpleNamespace(
        cache_dir=cache,
        apps={
            "example": SimpleNamespace(
                bundle_id="com.example.app", preferred_version="latest", preferred_version_id=None,
            ),
            "example_locked": SimpleNamespace(
                bundle_id="com.example.app", preferred_version="21.9.1", preferred_version_id=None,
            ),
        },
    )
    buf = StringIO()
    with pytest.raises(StagedIPAInUseError) as e:
        cli_mod._do_delete_staged(
            cfg, filename=old.name, force=False, emitter=JsonEmitter(stream=buf),
        )
    assert e.value.message == f"{old.name} is the only staged IPA that example_locked can build from", (
        "naming claimed_by instead of broken would also list example here"
    )
    event = _events(buf.getvalue())[0]
    assert event["used_by"] == ["example_locked"]


def test_it_does_not_refuse_when_another_version_remains(tmp_path: Path) -> None:
    """The case that separates a correct guard from a cautious one. Deleting a
    superseded version leaves the app buildable from the newer file, so there is
    nothing to warn about — and a confirm here would be noise."""
    cache = tmp_path / "cache"
    old = _ipa(cache, "com.example.app", "21.9.1")
    new = _ipa(cache, "com.example.app", "21.16.2")

    cli_mod._do_delete_staged(
        _cfg(cache, {"example": "com.example.app"}),
        filename=old.name, force=False, emitter=None,
    )

    assert not old.exists()
    assert new.exists(), "the current file must survive"


def test_it_refuses_when_a_pinned_app_would_lose_its_only_buildable_version(
    tmp_path: Path,
) -> None:
    """`find_in_cache` filters candidates against `app.preferred_version` when
    that isn't "latest" — a sibling with a different version is not something
    a pinned app can build from, even though it shares the bundle id. Deleting
    the file the pin actually resolves to must refuse even though another
    version of the same bundle id is still staged, because that other version
    is not one this app is allowed to use.

    Measured before this guard was fixed: with `preferred_version: 21.9.1`
    and both 21.9.1 and 21.16.2 staged, deleting 21.9.1 printed "✓ deleted"
    with no refusal, and `find_in_cache` afterward returned None.
    """
    cache = tmp_path / "cache"
    old = _ipa(cache, "com.example.app", "21.9.1")
    _ipa(cache, "com.example.app", "21.16.2")
    cfg = SimpleNamespace(
        cache_dir=cache,
        apps={
            "example": SimpleNamespace(
                bundle_id="com.example.app", preferred_version="21.9.1", preferred_version_id=None,
            ),
        },
    )

    with pytest.raises(StagedIPAInUseError) as e:
        cli_mod._do_delete_staged(cfg, filename=old.name, force=False, emitter=None)
    assert "example" in str(e.value)
    assert old.exists(), "a refusal must not delete"


def test_a_pinned_app_is_not_refused_when_its_own_version_remains(
    tmp_path: Path,
) -> None:
    """The positive case beside the refusal above: deleting the *newer*,
    unpinned-away version leaves the pinned one in place, so the app this pin
    names is untouched and there is nothing to warn about."""
    cache = tmp_path / "cache"
    _ipa(cache, "com.example.app", "21.9.1")
    newer = _ipa(cache, "com.example.app", "21.16.2")
    cfg = SimpleNamespace(
        cache_dir=cache,
        apps={
            "example": SimpleNamespace(
                bundle_id="com.example.app", preferred_version="21.9.1", preferred_version_id=None,
            ),
        },
    )

    cli_mod._do_delete_staged(cfg, filename=newer.name, force=False, emitter=None)
    assert not newer.exists()


def test_it_does_not_refuse_a_file_a_claiming_apps_pin_could_never_use(
    tmp_path: Path,
) -> None:
    """An app pinned to a version that isn't even the one staged was already
    unbuildable before this delete — `find_in_cache` returns None for it
    regardless, since the pin rejects the only file staged just as it would
    reject any other. Deleting that file changes nothing this app could
    build, so it must not be named in a refusal it has no real stake in.

    Measured before this was fixed: `preferred_version: 22.0.0` with only
    `..._21.16.2.ipa` staged — already unbuildable from cache — still refused
    the delete of that same file.
    """
    cache = tmp_path / "cache"
    only = _ipa(cache, "com.example.app", "21.16.2")
    cfg = SimpleNamespace(
        cache_dir=cache,
        apps={
            "example": SimpleNamespace(
                bundle_id="com.example.app", preferred_version="22.0.0", preferred_version_id=None,
            ),
        },
    )

    cli_mod._do_delete_staged(cfg, filename=only.name, force=False, emitter=None)
    assert not only.exists()


def test_a_file_merely_named_like_the_app_does_not_count_as_a_sibling(
    tmp_path: Path,
) -> None:
    """`_cache_candidates` matches on filename; `find_in_cache` then drops any
    candidate whose plist disagrees. The guard has to apply both halves, or a
    mis-named file waves through a delete that leaves the app with nothing.

    Here `app_9.9.ipa` is a candidate *by name* for com.example.app
    but its plist says otherwise, so it is not something a build could use — and
    deleting the real file must still refuse.
    """
    cache = tmp_path / "cache"
    real = _ipa(cache, "com.example.app", "21.16.2")
    impostor = _ipa(cache, "com.someone.else", "1.0")
    impostor.rename(cache / "app_9.9.ipa")

    with pytest.raises(StagedIPAInUseError):
        cli_mod._do_delete_staged(
            _cfg(cache, {"example": "com.example.app"}),
            filename=real.name, force=False, emitter=None,
        )
    assert real.exists()


def test_a_decoy_filename_with_a_matching_plist_does_not_count_as_a_sibling(
    tmp_path: Path,
) -> None:
    """The mirror of the test above: this time the *name* fails and the
    *plist* would pass. `random.ipa` is not a candidate for
    com.example.app under either of `_is_cache_candidate`'s shapes —
    `find_in_cache` would never open it — even though its own plist claims
    the right bundle id. Swapping `_cache_candidates` for a bare `*.ipa` glob
    would count it anyway and wave through a delete that leaves the app with
    nothing.
    """
    cache = tmp_path / "cache"
    real = _ipa(cache, "com.example.app", "21.16.2")
    decoy = _ipa(cache, "com.example.app", "1.0")
    decoy.rename(cache / "random.ipa")

    with pytest.raises(StagedIPAInUseError):
        cli_mod._do_delete_staged(
            _cfg(cache, {"example": "com.example.app"}),
            filename=real.name, force=False, emitter=None,
        )
    assert real.exists()


def test_deleting_a_file_no_build_could_ever_reach_does_not_refuse(
    tmp_path: Path,
) -> None:
    """`random.ipa`'s plist claims a configured app's bundle id, but its name
    matches neither of `find_in_cache`'s shapes, so no build could ever have
    opened it — the app was already unbuildable from cache before this
    delete, and deleting it changes nothing a build could reach. Refusing
    here would tell the user removing it breaks the app, which isn't true."""
    cache = tmp_path / "cache"
    junk = _ipa(cache, "com.example.app", "1.0")
    junk.rename(cache / "random.ipa")

    cli_mod._do_delete_staged(
        _cfg(cache, {"example": "com.example.app"}),
        filename="random.ipa", force=False, emitter=None,
    )
    assert not (cache / "random.ipa").exists()


def test_a_case_mismatched_spelling_of_the_target_does_not_count_as_a_sibling(
    tmp_path: Path,
) -> None:
    """macOS/APFS is case-insensitive by default: `_cache_candidates`'s own
    glob can return `target`'s file under a different spelling of its case —
    measured as `..._21.16.2.IPA` against the real `..._21.16.2.ipa`. A bare
    path inequality (`p != target`) would then count the file as its own
    sibling and the guard would silently not fire, even though this is
    genuinely the only staged file. Skipped rather than vacuously passed on a
    case-sensitive volume, where the premise doesn't hold."""
    cache = tmp_path / "cache"
    if not _filesystem_is_case_insensitive(cache):
        pytest.skip("filesystem backing tmp_path is case-sensitive; probe not meaningful here")

    path = _ipa(cache, "com.example.app", "21.16.2")
    mis_cased = path.stem + path.suffix.upper()
    assert mis_cased != path.name, "fixture extension has no case to flip"

    with pytest.raises(StagedIPAInUseError):
        cli_mod._do_delete_staged(
            _cfg(cache, {"example": "com.example.app"}),
            filename=mis_cased, force=False, emitter=None,
        )
    assert path.exists(), "a refusal must not delete"


def test_a_vanished_candidate_does_not_crash_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_cache_candidates`'s glob and the `os.path.samefile` compare that
    follows it are two separate moments, and a file present at the first can
    be gone by the second — `_do_staged_ipas`'s own comment records this
    exact race happening for real in this directory, when a writer
    completes a same-named file's `shutil.move` between two passes.
    `os.path.samefile` raises where a bare `!=` could not; a candidate gone
    by the time it's checked must not propagate a raw `OSError` out of the
    only destructive verb here — it has to be filtered like any other
    candidate that fails to open, which for a genuinely absent file happens
    to be the correct outcome too, not just a safe one.
    """
    cache = tmp_path / "cache"
    target = _ipa(cache, "com.example.app", "21.16.2")
    ghost = cache / "com.example.app_ghost.ipa"  # named as a candidate, never created

    real_cache_candidates = cli_mod._cache_candidates

    def _with_ghost(cache_dir: Path, bundle_id: str) -> list[Path]:
        return [ghost, *real_cache_candidates(cache_dir, bundle_id)]

    monkeypatch.setattr(cli_mod, "_cache_candidates", _with_ghost)

    with pytest.raises(StagedIPAInUseError):
        cli_mod._do_delete_staged(
            _cfg(cache, {"example": "com.example.app"}),
            filename=target.name, force=False, emitter=None,
        )
    assert target.exists(), "a refusal must not delete"


def test_force_deletes_the_last_one_anyway(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    path = _ipa(cache, "com.example.app", "21.16.2")
    cli_mod._do_delete_staged(
        _cfg(cache, {"example": "com.example.app"}),
        filename=path.name, force=True, emitter=None,
    )
    assert not path.exists()


def test_deleting_an_unreadable_file_does_not_require_force(tmp_path: Path) -> None:
    """`IPAInfoMemo.info_for` returns `None` for a truncated or corrupt `.ipa`
    rather than raising, the same "skip this candidate" signal
    `find_in_cache` already acts on. A direct `read_info_plist` call would
    raise here and require `--force` to clear junk out of the cache, with a
    bad-zip-magic error that never mentions `--force` — cleaning junk out is
    arguably what a cache pane is for."""
    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    junk = cache / "com.example.app_1.0.ipa"
    junk.write_bytes(b"not a zip")

    cli_mod._do_delete_staged(
        _cfg(cache, {"example": "com.example.app"}),
        filename=junk.name, force=False, emitter=None,
    )
    assert not junk.exists()


def test_a_filename_not_in_the_cache_fails_without_deleting_anything(
    tmp_path: Path,
) -> None:
    """`ConfigError`, specifically — not a bare `Exception`. Drop the
    `target.is_file()` guard and `not-here.ipa` still fails, but by way of
    `target.unlink()` raising `FileNotFoundError`, caught below and re-raised
    as a `GraftError` — a different type, with no `--staged-ipas` hint
    and no sentence naming `cache_dir`. A blind `pytest.raises(Exception)`
    cannot tell that apart from the guard actually firing.
    """
    cache = tmp_path / "cache"
    keep = _ipa(cache, "com.example.app", "1.0")
    with pytest.raises(ConfigError) as e:
        cli_mod._do_delete_staged(
            _cfg(cache), filename="not-here.ipa", force=False, emitter=None,
        )
    assert keep.exists()
    assert "not-here.ipa" in e.value.message
    assert "--staged-ipas" in (e.value.hint or "")


def test_a_path_outside_the_cache_is_refused(tmp_path: Path) -> None:
    """`filename`, not a path. A caller that passes `../../something.ipa` must
    not reach outside `cache_dir` — this verb deletes files."""
    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    outside = tmp_path / "precious.ipa"
    outside.write_bytes(b"not an ipa")
    with pytest.raises(ConfigError) as e:
        cli_mod._do_delete_staged(
            _cfg(cache), filename="../precious.ipa", force=False, emitter=None,
        )
    assert outside.exists()
    assert "not a filename" in e.value.message


def test_a_path_outside_the_cache_is_refused_even_with_force(tmp_path: Path) -> None:
    """The filename guard has to run before the `force` branch, not inside it,
    and before any read of the target at all — with `force=True` nothing else
    in this function ever looks at the escaped path's contents, so this
    exercises the guard directly rather than by way of a side effect of it."""
    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    outside = tmp_path / "precious.ipa"
    outside.write_bytes(b"not an ipa")
    with pytest.raises(ConfigError) as e:
        cli_mod._do_delete_staged(
            _cfg(cache), filename="../precious.ipa", force=True, emitter=None,
        )
    assert outside.exists()
    assert "not a filename" in e.value.message


def test_deleting_from_an_unwritable_cache_dir_reports_cleanly(
    tmp_path: Path,
) -> None:
    """`target.unlink()` untranslated would still surface as *some*
    `GraftError` further up the stack (`_unexpected`'s generic
    fallback), but as a bare `PermissionError` class name rather than a
    sentence naming `cache_dir` — the same gap `_do_delete_tweak` closed for
    `move_to_trash`'s `OSError`."""
    import os

    if os.geteuid() == 0:
        pytest.skip("running as root bypasses the directory permission bits")

    cache = tmp_path / "cache"
    path = _ipa(cache, "com.example.app", "1.0")
    cache.chmod(0o500)  # r-x: readable, but nothing can be unlinked
    try:
        with pytest.raises(GraftError) as e:
            cli_mod._do_delete_staged(
                _cfg(cache), filename=path.name, force=False, emitter=None,
            )
    finally:
        cache.chmod(0o700)
    assert "permission" in str(e.value).lower() or str(cache) in str(e.value)
    assert not isinstance(e.value, StagedIPAInUseError)


def test_delete_staged_subprocess_reports_in_use_via_exit_17(
    temp_config_env: Path,
) -> None:
    """The production wiring (main() -> _fatal() -> typer.Exit(code=17)) had
    zero automated coverage before this, mirroring
    `test_delete_tweak_subprocess_reports_in_use_via_exit_16` — the precedent
    this verb's exit code, event, and `used_by` shape all follow."""
    cfg = load_config(temp_config_env)
    path = _ipa(cfg.cache_dir, "com.example.app", "21.16.2")

    proc = subprocess.run(
        [sys.executable, "-m", "graft_ipa.cli", "--json", "--delete-staged", path.name],
        env=_subprocess_env(temp_config_env),
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent.parent),
    )

    assert proc.returncode == 17
    assert "is the only staged IPA for" in proc.stderr

    events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert [e["type"] for e in events] == ["staged-ipa-in-use"], (
        "the refusal must put exactly one event on the wire"
    )
    assert events[0]["filename"] == path.name
    assert events[0]["used_by"] == ["example"]


def test_delete_never_reaches_the_built_archives(tmp_path: Path) -> None:
    """`--delete-staged` is for the cache and only the cache.

    Built archives are pruned by `--prune-builds`, which honours
    `archive_keep`; a delete verb that also resolved names in `built_dir`
    would quietly destroy retained builds the retention policy meant to keep.

    Written because it nearly happened: a `built_dir` fallback intended for
    `--install-to-mac` was applied to this function instead — the two resolve a
    filename with near-identical code, and the edit anchored on the wrong copy.
    Nothing in either suite would have noticed.
    """
    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    built = tmp_path / "built"
    archive = _ipa(built, "com.example.app", "1.0")
    cfg = SimpleNamespace(cache_dir=cache, built_dir=built, apps={})

    with pytest.raises(ConfigError) as e:
        cli_mod._do_delete_staged(
            cfg, filename=archive.name, force=False, emitter=None
        )

    assert archive.exists(), "the build survives"
    assert str(cache) in e.value.message
    assert str(built) not in e.value.message, (
        "and this verb does not even mention built_dir — it has no business there"
    )
