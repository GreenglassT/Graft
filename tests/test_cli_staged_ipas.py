"""The `--staged-ipas` verb: `_do_staged_ipas`.

What the cache holds, with no device involved. The GUI joins this against a
device listing by bundle id to decide which installed apps it can offer to add,
so the two facts that matter here are that the *plist* names each file and that
the CLI — not Swift — picks which of two versions a build would use.
"""

from __future__ import annotations

import datetime
import json
import plistlib
import zipfile
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from graft_ipa import cli as cli_mod
from graft_ipa import ipa_inspect
from graft_ipa.ipa_cache import find_in_cache
from graft_ipa.json_output import EVENT_TYPES, JsonEmitter


def _events(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _ipa(cache: Path, bundle_id: str, version: str, display_name: str = "Fake") -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{bundle_id}_{version}.ipa"
    info = plistlib.dumps({
        "CFBundleIdentifier": bundle_id,
        "CFBundleShortVersionString": version,
        "CFBundleDisplayName": display_name,
        "CFBundleExecutable": "Fake",
    })
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Payload/Fake.app/Info.plist", info)
    return path


def _cfg(cache: Path) -> SimpleNamespace:
    return SimpleNamespace(cache_dir=cache)


def test_both_events_are_registered() -> None:
    assert {"staged-ipa", "staged-ipas-end"} <= EVENT_TYPES


def test_an_empty_cache_still_answers(tmp_path: Path) -> None:
    """The sentinel is the whole point: "nothing staged" and "nobody asked"
    must not decode the same way on the GUI side."""
    buf = StringIO()
    cli_mod._do_staged_ipas(_cfg(tmp_path / "cache"), emitter=JsonEmitter(stream=buf))
    events = _events(buf.getvalue())
    assert [e["type"] for e in events] == ["staged-ipas-end"]
    assert events[0]["count"] == 0


def test_a_complete_ipa_reports_its_plist_fields(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    _ipa(cache, "com.example.app", "1.2.3", display_name="Example")
    buf = StringIO()
    cli_mod._do_staged_ipas(_cfg(cache), emitter=JsonEmitter(stream=buf))
    staged = [e for e in _events(buf.getvalue()) if e["type"] == "staged-ipa"]
    assert len(staged) == 1
    assert staged[0]["bundle_id"] == "com.example.app"
    assert staged[0]["version"] == "1.2.3"
    assert staged[0]["display_name"] == "Example"
    assert staged[0]["filename"] == "com.example.app_1.2.3.ipa"
    assert staged[0]["size_bytes"] > 0
    assert staged[0]["stripped"] == []
    assert staged[0]["preferred"] is True


def test_a_non_string_display_name_reaches_the_wire_as_a_string(tmp_path: Path) -> None:
    """`CFBundleDisplayName` is a legal plist integer, array or dict just as
    easily as a string, and `find_in_cache`'s third match strategy accepts a
    hand-copied file without validating its plist's shape — so this is
    reachable, not hypothetical. Without coercion in `read_info_plist` a
    non-string value reaches this event untouched, `StagedIPARecord`'s
    `String`-typed decode throws on the Swift side, `compactMap { try? }`
    drops the whole record, and the GUI's own tally-based guard then refuses
    to publish *any* staged IPA — one bad plist going permanently dark for
    the whole feature."""
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / "com.example.weird_1.0.ipa"
    info = plistlib.dumps({
        "CFBundleIdentifier": "com.example.weird",
        "CFBundleShortVersionString": "1.0",
        "CFBundleDisplayName": 42,
        "CFBundleExecutable": "Fake",
    })
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Payload/Fake.app/Info.plist", info)
    buf = StringIO()
    cli_mod._do_staged_ipas(_cfg(cache), emitter=JsonEmitter(stream=buf))
    staged = [e for e in _events(buf.getvalue()) if e["type"] == "staged-ipa"][0]
    assert staged["display_name"] == "42"
    assert isinstance(staged["display_name"], str)


def test_a_non_string_bundle_id_reaches_the_wire_as_a_string(tmp_path: Path) -> None:
    """The key this whole feature joins on. `CFBundleIdentifier` has the
    exact same gap `CFBundleDisplayName` did — read with a bare
    `plist.get(...)`, checked only for truthiness, reachable as a legal
    plist integer, array or dict via the same hand-copied-file path
    `find_in_cache`'s third match strategy exists to accept — and it is
    worse than `display_name` besides: `find_in_cache` compares
    `info.bundle_id != app.bundle_id` directly, so a non-string value is
    silently unequal to every string bundle id and the file is skipped
    there too, a second wrong behaviour from the same uncoerced value.

    The filename still has to satisfy `find_in_cache`'s own candidate test
    against the *coerced* bundle id ("999888", stringified) — Finding 1 means
    a name that doesn't is unreachable and omitted regardless of what its
    plist says, which would prove nothing about coercion here."""
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / "999888_weird.ipa"
    info = plistlib.dumps({
        "CFBundleIdentifier": 999888,
        "CFBundleShortVersionString": "1.0",
        "CFBundleDisplayName": "Fine",
        "CFBundleExecutable": "Fine",
    })
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Payload/Fine.app/Info.plist", info)
    buf = StringIO()
    cli_mod._do_staged_ipas(_cfg(cache), emitter=JsonEmitter(stream=buf))
    staged = [e for e in _events(buf.getvalue()) if e["type"] == "staged-ipa"][0]
    assert staged["bundle_id"] == "999888"
    assert isinstance(staged["bundle_id"], str)


def test_a_date_valued_executable_does_not_kill_the_listing_mid_stream(
    tmp_path: Path,
) -> None:
    """`CFBundleExecutable` never reaches this event's own wire format — but it
    does reach `IPAInfoMemo`'s memo entries via `getattr(info, field)`, and
    `finally: memo.save()` runs a `json.dumps` over them that cannot
    serialize a raw `datetime` (a legal, if unusual, plist `<date>` value).
    Uncoerced, that raised from inside the `finally`, after this file's own
    `staged-ipa` line had already been written to `buf` and before
    `staged-ipas-end` — exactly the "some lines, no sentinel" shape
    `StagedIPAStore.load()`'s own doc says leaves `hasLoaded` false forever.
    A second, unrelated app is staged alongside it so this test can tell "the
    listing kept going" apart from "the listing happened to have only one
    file"."""
    cache = tmp_path / "cache"
    _ipa(cache, "com.example.clean", "1.0")
    cache.mkdir(parents=True, exist_ok=True)
    weird_path = cache / "com.example.weird_1.0.ipa"
    weird_info = plistlib.dumps({
        "CFBundleIdentifier": "com.example.weird",
        "CFBundleShortVersionString": "1.0",
        "CFBundleDisplayName": "Weird",
        "CFBundleExecutable": datetime.datetime(2020, 1, 1),
    })
    with zipfile.ZipFile(weird_path, "w") as zf:
        zf.writestr("Payload/Weird.app/Info.plist", weird_info)

    buf = StringIO()
    cli_mod._do_staged_ipas(_cfg(cache), emitter=JsonEmitter(stream=buf))
    events = _events(buf.getvalue())
    assert [e["type"] for e in events] == [
        "staged-ipa", "staged-ipa", "staged-ipas-end",
    ], "must reach the sentinel, not die in memo.save() partway through"
    end = [e for e in events if e["type"] == "staged-ipas-end"][0]
    assert end["count"] == 2


def test_a_stripped_ipa_carries_what_was_lost(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    path = _ipa(cache, "com.example.books", "5.9.1")
    ipa_inspect.mark_stripped(
        path,
        ["Payload/Fake.app/PlugIns/Intents.appex"],
        lost=["Siri and Shortcuts"],
        importance={"Payload/Fake.app/PlugIns/Intents.appex": "major"},
    )
    buf = StringIO()
    cli_mod._do_staged_ipas(_cfg(cache), emitter=JsonEmitter(stream=buf))
    staged = [e for e in _events(buf.getvalue()) if e["type"] == "staged-ipa"][0]
    assert staged["stripped"] == ["Payload/Fake.app/PlugIns/Intents.appex"]
    assert staged["lost"] == ["Siri and Shortcuts"]
    assert staged["importance"] == {"Payload/Fake.app/PlugIns/Intents.appex": "major"}


def test_an_old_sidecar_omits_lost_rather_than_reporting_it_empty(tmp_path: Path) -> None:
    """Every sidecar in the real cache today is this shape. `lost: []` would
    tell the GUI that nothing important went, which nobody checked."""
    cache = tmp_path / "cache"
    path = _ipa(cache, "com.example.shop", "27.11.8")
    (cache / (path.name + ipa_inspect.STRIPPED_SUFFIX)).write_text(
        '{"removed": ["Payload/Fake.app/PlugIns/Widget.appex"]}'
    )
    buf = StringIO()
    cli_mod._do_staged_ipas(_cfg(cache), emitter=JsonEmitter(stream=buf))
    staged = [e for e in _events(buf.getvalue()) if e["type"] == "staged-ipa"][0]
    assert staged["stripped"] == ["Payload/Fake.app/PlugIns/Widget.appex"]
    assert "lost" not in staged


def test_a_recorded_empty_lost_is_reported_as_empty_not_omitted(tmp_path: Path) -> None:
    """The twin of the old-sidecar case above, and the only one of the two
    that may ever reassure a user. A dump that *was* inspected and found no
    capability worth naming among its casualties — a widget going missing is
    visibly missing, so it is deliberately absent from the capability map
    (`lost`) — says so with `lost: []`. That is a different claim from a
    sidecar nobody ever inspected, and only this shape may license telling
    anyone nothing important went."""
    cache = tmp_path / "cache"
    path = _ipa(cache, "com.example.notes", "9.0.0")
    ipa_inspect.mark_stripped(
        path,
        ["Payload/Fake.app/PlugIns/Widget.appex"],
        lost=[],
        importance={"Payload/Fake.app/PlugIns/Widget.appex": "cosmetic"},
    )
    buf = StringIO()
    cli_mod._do_staged_ipas(_cfg(cache), emitter=JsonEmitter(stream=buf))
    staged = [e for e in _events(buf.getvalue()) if e["type"] == "staged-ipa"][0]
    assert "lost" in staged
    assert staged["lost"] == []
    assert staged["importance"] == {"Payload/Fake.app/PlugIns/Widget.appex": "cosmetic"}


def test_the_newer_of_two_versions_is_the_preferred_one(tmp_path: Path) -> None:
    """Decided here so Swift never reimplements version comparison. 21.9.1
    sorts after 21.16.2 lexicographically and before it numerically — the same
    trap `find_in_cache` records having fallen into."""
    cache = tmp_path / "cache"
    _ipa(cache, "com.example.app", "21.9.1")
    _ipa(cache, "com.example.app", "21.16.2")
    buf = StringIO()
    cli_mod._do_staged_ipas(_cfg(cache), emitter=JsonEmitter(stream=buf))
    staged = [e for e in _events(buf.getvalue()) if e["type"] == "staged-ipa"]
    assert len(staged) == 2
    preferred = [e["version"] for e in staged if e["preferred"]]
    assert preferred == ["21.16.2"]


def test_preferred_matches_find_in_cache_when_the_hand_copy_sorts_before_the_canonical_name(
    tmp_path: Path,
) -> None:
    """`find_in_cache`'s own docstring names `App_21.16.2.ipa` as the
    shape its third match strategy exists for — a hand-copied file sitting
    beside the canonical `com.example.app_21.16.2.ipa`, same bundle id
    and version from each file's own plist. `find_in_cache` scans a
    canonically-named bucket before its short-name-fallback bucket, so the
    canonical file wins this tie *regardless of alphabetical order* — but a
    flat filename sort gets it backwards here, since `A` (0x41) sorts before
    `c` (0x63). An earlier version of this function used a flat sort and
    picked the hand copy: unique, but the wrong one — which would have handed
    the GUI that file's strip note for a build `find_in_cache` would not
    actually have used. The strongest check is not a filename literal but
    agreement with `find_in_cache` itself, for the same directory."""
    cache = tmp_path / "cache"
    hand_copied = _ipa(cache, "com.example.app", "21.16.2")
    hand_copied.rename(cache / "App_21.16.2.ipa")
    canonical = _ipa(cache, "com.example.app", "21.16.2")
    buf = StringIO()
    cli_mod._do_staged_ipas(_cfg(cache), emitter=JsonEmitter(stream=buf))
    staged = [e for e in _events(buf.getvalue()) if e["type"] == "staged-ipa"]
    assert len(staged) == 2
    preferred = [e for e in staged if e["preferred"]]
    assert len(preferred) == 1
    assert preferred[0]["filename"] == canonical.name

    winner = find_in_cache(
        _cfg(cache),
        SimpleNamespace(bundle_id="com.example.app", preferred_version="latest"),
    )
    assert winner is not None
    assert winner.path.name == preferred[0]["filename"]


def test_preferred_matches_find_in_cache_when_the_hand_copy_sorts_after_the_canonical_name(
    tmp_path: Path,
) -> None:
    """The mirror of the case above: a hand-copied name that a flat filename
    sort already ranks after the canonical one, so this pins that the fix
    did not flip an ordering that was already correct — it must still agree
    with `find_in_cache`, not just still be unique."""
    cache = tmp_path / "cache"
    # A bundle id whose short name (`zeta`) sorts after `com`, so the hand copy
    # really does come second in a flat filename sort.
    hand_copied = _ipa(cache, "com.example.zeta", "21.16.2")
    hand_copied.rename(cache / "zeta_zz_backup_21.16.2.ipa")
    canonical = _ipa(cache, "com.example.zeta", "21.16.2")
    assert sorted([hand_copied.name, canonical.name])[0] == canonical.name, (
        "premise: a flat filename sort already ranks the canonical file first"
    )
    buf = StringIO()
    cli_mod._do_staged_ipas(_cfg(cache), emitter=JsonEmitter(stream=buf))
    staged = [e for e in _events(buf.getvalue()) if e["type"] == "staged-ipa"]
    assert len(staged) == 2
    preferred = [e for e in staged if e["preferred"]]
    assert len(preferred) == 1
    assert preferred[0]["filename"] == canonical.name

    winner = find_in_cache(
        _cfg(cache),
        SimpleNamespace(bundle_id="com.example.zeta", preferred_version="latest"),
    )
    assert winner is not None
    assert winner.path.name == preferred[0]["filename"]


def test_the_plist_decides_the_bundle_id_not_the_filename(tmp_path: Path) -> None:
    """The filename here still has to pass `find_in_cache`'s own short-name
    candidate test (`stem.lower().startswith(short)`) — a filename that fails
    every one of its tests is unreachable regardless of what its plist says
    (see the "never reach" tests below), so it can no longer stand in here for
    "any old wrong-looking name". `Real_Backup_9.9` still reads like a
    different app/version than `com.example.real` v1.0 at a glance, while its
    stem starts with the short name "real" — a real hand-copy shape, the same
    one `App_21.16.2.ipa` is elsewhere in this file."""
    cache = tmp_path / "cache"
    path = _ipa(cache, "com.example.real", "1.0")
    path.rename(cache / "Real_Backup_9.9.ipa")
    buf = StringIO()
    cli_mod._do_staged_ipas(_cfg(cache), emitter=JsonEmitter(stream=buf))
    staged = [e for e in _events(buf.getvalue()) if e["type"] == "staged-ipa"][0]
    assert staged["bundle_id"] == "com.example.real"
    assert staged["version"] == "1.0"
    assert staged["filename"] == "Real_Backup_9.9.ipa"


def test_a_file_the_build_can_never_reach_is_not_offered(tmp_path: Path) -> None:
    """A hand-dropped download can sit in `cache_dir` under any name at all —
    the plist is what identifies it, and this listing used to trust that
    alone. But `find_in_cache` never even opens a file whose *name* fails both
    of its candidate tests (`{bundle_id}_*.ipa`, or a stem starting with the
    bundle's short name) — such a file is invisible to a build no matter what
    its own Info.plist says. Before this fix this file still came out
    `preferred: true`: the sheet would say "the build will use this", the row
    would offer Add, and `find_in_cache` would then return None at build time,
    leaving the build nothing to start from — the exact deception this whole
    feature exists to prevent, and worse than a plain miss because it looked
    like a hit.

    Omitted here rather than merely marked `preferred: false`: this is the
    bundle id's *only* file, and `StagedIPAStore.load()` keeps the first
    record it sees for a bundle id regardless of `preferred` — `if
    record.preferred || found[record.bundleId] == nil`. A lone non-preferred
    record still satisfies that `nil` check and still lands in `byBundleId`,
    and `InstalledAppsSection.rowState` offers `.addable` for anything it
    finds there without ever consulting `preferred` itself. Downgrading the
    flag alone would not have closed this case; only leaving it out of the
    listing does.
    """
    cache = tmp_path / "cache"
    hand_dropped = _ipa(cache, "com.example.app", "21.16.2")
    # A hyphen, not the canonical underscore — a real shape a hand download
    # produces.
    hand_dropped.rename(cache / "com.example.app-21.16.2.ipa")
    buf = StringIO()
    cli_mod._do_staged_ipas(_cfg(cache), emitter=JsonEmitter(stream=buf))
    events = _events(buf.getvalue())
    staged = [e for e in events if e["type"] == "staged-ipa"]
    end = [e for e in events if e["type"] == "staged-ipas-end"][0]
    assert staged == []
    assert end["count"] == 0

    assert (
        find_in_cache(
            _cfg(cache),
            SimpleNamespace(bundle_id="com.example.app", preferred_version="latest"),
        )
        is None
    ), "premise: find_in_cache really cannot reach this file either"


def test_an_unreachable_file_beside_a_reachable_one_is_never_the_one_offered(
    tmp_path: Path,
) -> None:
    """Two files agreeing on a bundle id per each one's own plist: one under
    the canonical name, one — a *newer* version — that fails both of
    `find_in_cache`'s candidate tests. A flat "does the filename start with
    `{bundle_id}_`" split put both of these in the same `rest` bucket before
    this fix, and version comparison alone then picked a winner — so the
    newer, unreachable file could still win `preferred`. That is not
    cosmetic: the sidecar (strip state) is keyed to the file, so naming the
    wrong one hands the sheet the wrong filename, the wrong size, and the
    wrong strip state for the file a build actually reaches."""
    cache = tmp_path / "cache"
    canonical = _ipa(cache, "com.foo.bar", "1.0")
    unreachable = _ipa(cache, "com.foo.bar", "2.0")
    unreachable.rename(cache / "zzz.ipa")
    buf = StringIO()
    cli_mod._do_staged_ipas(_cfg(cache), emitter=JsonEmitter(stream=buf))
    events = _events(buf.getvalue())
    staged = [e for e in events if e["type"] == "staged-ipa"]
    end = [e for e in events if e["type"] == "staged-ipas-end"][0]
    assert [s["filename"] for s in staged] == [canonical.name]
    assert staged[0]["preferred"] is True
    assert end["count"] == 1

    winner = find_in_cache(
        _cfg(cache),
        SimpleNamespace(bundle_id="com.foo.bar", preferred_version="latest"),
    )
    assert winner is not None
    assert winner.path.name == canonical.name


def test_unreadable_files_are_skipped_and_not_counted(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    _ipa(cache, "com.example.app", "1.0")
    (cache / "truncated.ipa").write_bytes(b"<html>nope</html>")
    (cache / "notes.txt").write_text("ignore me")
    buf = StringIO()
    cli_mod._do_staged_ipas(_cfg(cache), emitter=JsonEmitter(stream=buf))
    events = _events(buf.getvalue())
    staged = [e for e in events if e["type"] == "staged-ipa"]
    end = [e for e in events if e["type"] == "staged-ipas-end"][0]
    assert len(staged) == 1
    assert end["count"] == 1


def test_a_partial_download_beside_it_is_not_counted(tmp_path: Path) -> None:
    """`*.ipa` matches leading dots too — pathlib globbing is not a shell
    glob. `provide_ipa.py` writes an in-flight copy straight into cache_dir
    under a dotted name, and for a
    window it is a complete, readable zip. `find_in_cache` never sees it: its
    globs require the real `{bundle_id}_` prefix, which the leading dot
    defeats. This listing needs its own guard against the same file."""
    cache = tmp_path / "cache"
    _ipa(cache, "com.example.real", "1.0")
    partial = _ipa(cache, "com.example.app", "1.0")
    partial.rename(cache / ".com.example.app_1.0.partial.ipa")
    buf = StringIO()
    cli_mod._do_staged_ipas(_cfg(cache), emitter=JsonEmitter(stream=buf))
    events = _events(buf.getvalue())
    staged = [e for e in events if e["type"] == "staged-ipa"]
    end = [e for e in events if e["type"] == "staged-ipas-end"][0]
    assert len(staged) == 1
    assert end["count"] == 1


def test_the_console_branch_prints_a_line_per_staged_ipa_and_marks_stripped_ones(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The `emitter=None` path — nothing exercised it until now. After the
    size-bytes fix above it reads `size` off `found` rather than statting the
    file a second time to print it."""
    cache = tmp_path / "cache"
    _ipa(cache, "com.example.clean", "1.0")
    stripped_path = _ipa(cache, "com.example.stripped", "2.0")
    ipa_inspect.mark_stripped(
        stripped_path,
        ["Payload/Fake.app/PlugIns/Widget.appex"],
        lost=["Siri and Shortcuts"],
    )
    cli_mod._do_staged_ipas(_cfg(cache))
    out = capsys.readouterr().out
    assert "com.example.clean_1.0.ipa" in out
    assert "com.example.stripped_2.0.ipa" in out
    assert "(minus 1)" in out
