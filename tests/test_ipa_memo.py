"""The Info.plist memo: it must be fast, and it must never be wrong.

`find_in_cache` is on the GUI's refresh path via `--list`, and the memo is what
stops it re-parsing every cached IPA's zip central directory on every refresh.
Two failure classes matter and both are tested here:

  - **Staleness.** A memo that answers with a version the file no longer has
    feeds a wrong `source_version` into the delivery digest, which then
    describes an IPA that was never built from. Every way a file can change —
    contents, size, mtime, existence — has to invalidate.
  - **Fragility.** `--list` renders the GUI's entire app list. A memo that is
    corrupt, truncated, unreadable, from another format version, or sitting in
    a directory that can't be written must degrade to "just read the plist",
    never raise.

Everything runs under `tmp_path`. Nothing here touches the real cache or state
directory.
"""

from __future__ import annotations

import datetime
import json
import os
import plistlib
import stat
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from graft_ipa import ipa_memo
from graft_ipa.config import AppConfig, CyanOptions
from graft_ipa.ipa_cache import find_in_cache
from graft_ipa.ipa_memo import MEMO_FILENAME, MEMO_FORMAT, IPAInfoMemo

BUNDLE = "com.example.app"


def _ipa(
    path: Path,
    *,
    bundle: str = BUNDLE,
    version: str = "21.16.2",
    padding: int = 0,
) -> Path:
    """A zip real enough for read_info_plist. `padding` varies the byte size."""
    info = {
        "CFBundleIdentifier": bundle,
        "CFBundleShortVersionString": version,
        "CFBundleName": "Example",
        "CFBundleExecutable": "Example",
    }
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Payload/Example.app/Info.plist", plistlib.dumps(info))
        if padding:
            z.writestr("Payload/Example.app/pad.bin", b"p" * padding)
    return path


def _cached_ipa(cache: Path, version: str, bundle: str = BUNDLE, **kw) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    return _ipa(cache / f"{bundle}_{version}.ipa", bundle=bundle, version=version, **kw)


class _Cfg:
    """The duck-typed config find_in_cache actually needs."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir


def _app(pinned: str = "latest", bundle: str = BUNDLE) -> AppConfig:
    return AppConfig(
        name="example",
        bundle_id=bundle,
        preferred_version=pinned,
        tweaks=[],
        cyan=CyanOptions(),
    )


def _set_mtime(path: Path, mtime_ns: int) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, mtime_ns))


# --- it answers the same thing read_info_plist does --------------------------


def test_a_cold_lookup_matches_read_info_plist(tmp_path: Path) -> None:
    from graft_ipa.ipa_inspect import read_info_plist

    ipa = _ipa(tmp_path / "app.ipa")

    assert IPAInfoMemo.load(tmp_path).info_for(ipa) == read_info_plist(ipa)


def test_a_warm_lookup_matches_read_info_plist(tmp_path: Path) -> None:
    """Every field round-trips through JSON, not just bundle_id and version."""
    from graft_ipa.ipa_inspect import read_info_plist

    ipa = _ipa(tmp_path / "app.ipa")
    memo = IPAInfoMemo.load(tmp_path)
    memo.info_for(ipa)
    memo.save()

    assert IPAInfoMemo.load(tmp_path).info_for(ipa) == read_info_plist(ipa)


def test_a_warm_lookup_does_not_open_the_zip(tmp_path: Path) -> None:
    """The whole point. Without this the memo is a JSON file that costs time."""
    ipa = _ipa(tmp_path / "app.ipa")
    memo = IPAInfoMemo.load(tmp_path)
    memo.info_for(ipa)
    memo.save()

    with patch.object(ipa_memo, "read_info_plist") as read:
        info = IPAInfoMemo.load(tmp_path).info_for(ipa)

    assert not read.called
    assert info is not None
    assert info.version == "21.16.2"


def test_a_warm_memo_is_not_rewritten(tmp_path: Path) -> None:
    """A refresh that hits on everything must be reads only."""
    ipa = _ipa(tmp_path / "app.ipa")
    memo = IPAInfoMemo.load(tmp_path)
    memo.info_for(ipa)
    memo.save()
    before = (tmp_path / MEMO_FILENAME).stat().st_mtime_ns

    warm = IPAInfoMemo.load(tmp_path)
    warm.info_for(ipa)
    warm.save()

    assert (tmp_path / MEMO_FILENAME).stat().st_mtime_ns == before


# --- invalidation ------------------------------------------------------------


def test_changed_contents_invalidate(tmp_path: Path) -> None:
    """The version the memo returns must be the version in the file today."""
    ipa = _ipa(tmp_path / "app.ipa", version="21.16.2")
    memo = IPAInfoMemo.load(tmp_path)
    memo.info_for(ipa)
    memo.save()

    _ipa(ipa, version="22.1.0", padding=4096)
    info = IPAInfoMemo.load(tmp_path).info_for(ipa)

    assert info is not None
    assert info.version == "22.1.0"


def test_size_alone_invalidates(tmp_path: Path) -> None:
    """Pin size as a key in its own right: mtime is restored to the old value."""
    ipa = _ipa(tmp_path / "app.ipa", version="21.16.2")
    memo = IPAInfoMemo.load(tmp_path)
    memo.info_for(ipa)
    memo.save()
    old_mtime = ipa.stat().st_mtime_ns

    _ipa(ipa, version="22.1.0", padding=4096)
    _set_mtime(ipa, old_mtime)
    assert ipa.stat().st_mtime_ns == old_mtime

    info = IPAInfoMemo.load(tmp_path).info_for(ipa)

    assert info is not None
    assert info.version == "22.1.0"


def test_mtime_alone_invalidates(tmp_path: Path) -> None:
    """Pin mtime as a key in its own right: the file is byte-identical."""
    ipa = _ipa(tmp_path / "app.ipa")
    size = ipa.stat().st_size
    memo = IPAInfoMemo.load(tmp_path)
    memo.info_for(ipa)
    memo.save()

    _set_mtime(ipa, ipa.stat().st_mtime_ns + 1_000_000)
    assert ipa.stat().st_size == size, "this test is about mtime, not size"

    with patch.object(ipa_memo, "read_info_plist") as read:
        IPAInfoMemo.load(tmp_path).info_for(ipa)

    assert read.called


def test_a_file_that_disappears_is_a_miss_not_a_crash(tmp_path: Path) -> None:
    ipa = _ipa(tmp_path / "app.ipa")
    memo = IPAInfoMemo.load(tmp_path)
    memo.info_for(ipa)
    memo.save()

    ipa.unlink()

    assert IPAInfoMemo.load(tmp_path).info_for(ipa) is None


def test_a_file_replaced_mid_read_is_not_memoized(tmp_path: Path) -> None:
    """`--provide-ipa` can land between the stat and the read.

    Recording the pre-write identity against post-write contents would pin a
    wrong version until the *next* write of that file.
    """
    ipa = _ipa(tmp_path / "app.ipa", version="21.16.2")
    real = ipa_memo.read_info_plist

    def _rewrite_then_read(path: Path):
        info = real(path)
        _ipa(path, version="22.1.0", padding=4096)
        return info

    memo = IPAInfoMemo.load(tmp_path)
    with patch.object(ipa_memo, "read_info_plist", _rewrite_then_read):
        memo.info_for(ipa)
    memo.save()

    assert not (tmp_path / MEMO_FILENAME).exists(), "nothing was safe to record"
    info = IPAInfoMemo.load(tmp_path).info_for(ipa)
    assert info is not None
    assert info.version == "22.1.0"


def test_a_non_ipa_is_a_miss_and_is_not_memoized(tmp_path: Path) -> None:
    junk = tmp_path / "junk.ipa"
    junk.write_bytes(b"<html>not an ipa</html>")

    memo = IPAInfoMemo.load(tmp_path)

    assert memo.info_for(junk) is None
    memo.save()
    assert not (tmp_path / MEMO_FILENAME).exists()


def test_a_deleted_file_is_pruned_on_the_next_write(tmp_path: Path) -> None:
    old = _ipa(tmp_path / "old.ipa")
    memo = IPAInfoMemo.load(tmp_path)
    memo.info_for(old)
    memo.save()
    old.unlink()

    new = _ipa(tmp_path / "new.ipa")
    memo = IPAInfoMemo.load(tmp_path)
    memo.info_for(new)
    memo.save()

    entries = json.loads((tmp_path / MEMO_FILENAME).read_text())["entries"]
    assert list(entries) == [os.path.abspath(new)]


# --- everything degrades -----------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "not json at all",
        "",
        '{"format": 1, "entries": ',  # truncated mid-write
        "[]",
        '{"format": 1}',
        '{"format": 1, "entries": []}',
        "null",
        # Too deep for the parser: RecursionError, which is no ValueError
        # (the 2.3.1 review's T2-B5).
        "[" * 10_000 + "]" * 10_000,
    ],
    ids=[
        "garbage",
        "empty",
        "truncated",
        "a-list",
        "no-entries-key",
        "entries-not-an-object",
        "json-null",
        "nested-too-deep",
    ],
)
def test_an_unusable_memo_falls_back_to_reading_the_plist(tmp_path: Path, body: str) -> None:
    ipa = _ipa(tmp_path / "app.ipa", version="22.1.0")
    (tmp_path / MEMO_FILENAME).write_text(body)

    info = IPAInfoMemo.load(tmp_path).info_for(ipa)

    assert info is not None
    assert info.version == "22.1.0"


def _entry(ipa: Path, **overrides: object) -> dict:
    """A complete, valid memo entry for `ipa`, with `overrides` applied.

    Overrides are applied over a *complete* entry on purpose. An entry that is
    merely short of several fields is rejected by the first missing one, so it
    proves nothing about the check the case is named for — that is precisely
    how the `empty-bundle-id` case below first shipped passing against the bug
    it was written to catch.
    """
    st = ipa.stat()
    entry = {
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "bundle_id": BUNDLE,
        "version": "9.9.9-from-the-memo",
        "display_name": "Example",
        "executable": "Example",
        "info_plist_path_in_zip": "Payload/Example.app/Info.plist",
    }
    entry.update(overrides)
    return {k: v for k, v in entry.items() if v is not ...}


def _write_memo(tmp_path: Path, ipa: Path, entry: object, fmt: int = MEMO_FORMAT) -> None:
    (tmp_path / MEMO_FILENAME).write_text(
        json.dumps({"format": fmt, "entries": {os.path.abspath(ipa): entry}})
    )


def test_a_valid_entry_is_trusted_verbatim(tmp_path: Path) -> None:
    """The control for every rejection case below.

    While `(size, mtime_ns)` match, the memo *is* the answer — that is the
    whole optimization. Without this pinned, a rejection test can pass because
    the memo was never consulted at all.
    """
    ipa = _ipa(tmp_path / "app.ipa", version="22.1.0")
    _write_memo(tmp_path, ipa, _entry(ipa))

    info = IPAInfoMemo.load(tmp_path).info_for(ipa)

    assert info is not None
    assert info.version == "9.9.9-from-the-memo"


@pytest.mark.parametrize(
    "overrides",
    [
        {"bundle_id": ..., "version": ..., "display_name": ..., "executable": ...,
         "info_plist_path_in_zip": ...},
        {"size": ..., "mtime_ns": ...},
        {"version": ...},
        {"executable": ...},
        {"info_plist_path_in_zip": ...},
        {"version": 99},
        {"bundle_id": 99},
        {"bundle_id": ""},
        {"bundle_id": None, "version": None},
        {"size": "big"},
        {"mtime_ns": None},
    ],
    ids=[
        "no-fields",
        "no-stat-key",
        "version-missing",
        "executable-missing",
        "plist-path-missing",
        "version-not-a-string",
        "bundle-id-not-a-string",
        "empty-bundle-id",
        "nulls",
        "size-not-an-int",
        "mtime-null",
    ],
)
def test_a_malformed_entry_is_a_miss_not_a_wrong_answer(tmp_path: Path, overrides: dict) -> None:
    """A half-written entry must never become an IPAInfo with holes in it."""
    ipa = _ipa(tmp_path / "app.ipa", version="22.1.0")
    _write_memo(tmp_path, ipa, _entry(ipa, **overrides))

    info = IPAInfoMemo.load(tmp_path).info_for(ipa)

    assert info is not None
    assert info.bundle_id == BUNDLE
    assert info.version == "22.1.0"


@pytest.mark.parametrize("entry", ["a string where an object belongs", None, [], 7])
def test_an_entry_that_is_not_an_object_is_a_miss(tmp_path: Path, entry: object) -> None:
    ipa = _ipa(tmp_path / "app.ipa", version="22.1.0")
    _write_memo(tmp_path, ipa, entry)

    info = IPAInfoMemo.load(tmp_path).info_for(ipa)

    assert info is not None
    assert info.version == "22.1.0"


def test_a_memo_from_another_format_is_discarded(tmp_path: Path) -> None:
    """Not merged, not partially read — dropped.

    The entry here is well-formed and would be trusted under the current
    format (see `test_a_valid_entry_is_trusted_verbatim`), so only the
    `format` field can be what rejects it. An empty `entries` under a future
    format would prove nothing.
    """
    ipa = _ipa(tmp_path / "app.ipa", version="22.1.0")
    _write_memo(tmp_path, ipa, _entry(ipa), fmt=MEMO_FORMAT + 1)

    info = IPAInfoMemo.load(tmp_path).info_for(ipa)

    assert info is not None
    assert info.version == "22.1.0"


def test_a_memo_with_no_format_field_is_discarded(tmp_path: Path) -> None:
    ipa = _ipa(tmp_path / "app.ipa", version="22.1.0")
    (tmp_path / MEMO_FILENAME).write_text(
        json.dumps({"entries": {os.path.abspath(ipa): _entry(ipa)}})
    )

    info = IPAInfoMemo.load(tmp_path).info_for(ipa)

    assert info is not None
    assert info.version == "22.1.0"


def test_a_memo_path_that_is_a_directory_degrades(tmp_path: Path) -> None:
    """Neither the read nor the write may raise."""
    ipa = _ipa(tmp_path / "app.ipa", version="22.1.0")
    (tmp_path / MEMO_FILENAME).mkdir()

    memo = IPAInfoMemo.load(tmp_path)
    info = memo.info_for(ipa)
    memo.save()

    assert info is not None
    assert info.version == "22.1.0"


def test_an_unwritable_cache_dir_degrades(tmp_path: Path) -> None:
    cache = tmp_path / "ipas"
    cache.mkdir()
    ipa = _cached_ipa(cache, "22.1.0")
    os.chmod(cache, stat.S_IRUSR | stat.S_IXUSR)
    try:
        memo = IPAInfoMemo.load(cache)
        info = memo.info_for(ipa)
        memo.save()  # must not raise
    finally:
        os.chmod(cache, stat.S_IRWXU)

    assert info is not None
    assert info.version == "22.1.0"
    assert not (cache / MEMO_FILENAME).exists()


def test_an_unreadable_memo_degrades(tmp_path: Path) -> None:
    ipa = _ipa(tmp_path / "app.ipa", version="22.1.0")
    memo_path = tmp_path / MEMO_FILENAME
    memo_path.write_text("{}")
    os.chmod(memo_path, 0)
    try:
        info = IPAInfoMemo.load(tmp_path).info_for(ipa)
    finally:
        os.chmod(memo_path, stat.S_IRUSR | stat.S_IWUSR)

    assert info is not None
    assert info.version == "22.1.0"


def test_a_value_json_cannot_serialize_does_not_crash_save(tmp_path: Path) -> None:
    """Defense in depth, not a currently-reachable path: after
    `ipa_inspect.read_info_plist` coerces every field to `str` (see its own
    comment on `CFBundleExecutable`), nothing on the ordinary `info_for` ->
    `save` path can put a non-JSON-serializable value into `_entries` today.
    But `save()`'s own docstring promises "Never raises" unconditionally, not
    "never raises given today's callers" — and this memo already lived
    through one real crash of exactly this shape (a `datetime` from a plist
    `<date>` `CFBundleExecutable`, closed at the source in `ipa_inspect.py`,
    not here). Reaching into `_entries` directly is the only way left to
    prove the *memo* itself holds that promise structurally, rather than only
    by accident of what its one caller happens to pass it today — a second
    field added to `IPAInfo` without the same coercion would reopen this
    exact crash if `save()` didn't guard against it directly."""
    ipa = _ipa(tmp_path / "app.ipa")
    memo = IPAInfoMemo.load(tmp_path)
    memo.info_for(ipa)  # populates and dirties one entry
    key = os.path.abspath(ipa)
    memo._entries[key]["executable"] = datetime.datetime(2020, 1, 1)

    memo.save()  # must not raise

    assert not (tmp_path / MEMO_FILENAME).exists(), (
        "a write that can't be serialized must not land a truncated or "
        "wrong memo on disk"
    )


def test_a_failed_write_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    ipa = _ipa(tmp_path / "app.ipa")
    memo = IPAInfoMemo.load(tmp_path)
    memo.info_for(ipa)

    with patch.object(ipa_memo.os, "replace", side_effect=OSError("nope")):
        memo.save()

    assert [p.name for p in tmp_path.iterdir()] == ["app.ipa"]


# --- find_in_cache still answers what it always did --------------------------


def test_latest_still_picks_the_newest_when_the_memo_is_warm(tmp_path: Path) -> None:
    _cached_ipa(tmp_path, "21.16.2")
    _cached_ipa(tmp_path, "22.1.0")
    cfg = _Cfg(tmp_path)

    assert find_in_cache(cfg, _app()).version == "22.1.0"
    warm = find_in_cache(cfg, _app())

    assert warm is not None
    assert warm.version == "22.1.0"


def test_a_pinned_lookup_writes_its_memo(tmp_path: Path) -> None:
    """The pinned branch returns from inside the loop; the save is in a finally."""
    _cached_ipa(tmp_path, "21.16.2")
    cfg = _Cfg(tmp_path)

    assert find_in_cache(cfg, _app("21.16.2")) is not None

    with patch.object(ipa_memo, "read_info_plist") as read:
        assert find_in_cache(cfg, _app("21.16.2")) is not None
    assert not read.called


def test_a_swapped_ipa_is_still_caught_by_the_plist(tmp_path: Path) -> None:
    """The reason this memoizes the read instead of parsing the filename.

    `--provide-ipa` stages into the canonical `{bundle_id}_{version}.ipa` name,
    so a file whose name says one thing and whose plist says another is
    reachable. A warm memo must not turn that into a trusted answer.
    """
    path = _cached_ipa(tmp_path, "21.16.2")
    cfg = _Cfg(tmp_path)
    assert find_in_cache(cfg, _app()) is not None

    # Same canonical filename, someone else's app inside.
    _ipa(path, bundle="com.bravo.app", version="21.16.2", padding=4096)

    assert find_in_cache(cfg, _app()) is None


def test_a_version_rewritten_under_the_same_name_is_re_read(tmp_path: Path) -> None:
    path = _cached_ipa(tmp_path, "21.16.2")
    cfg = _Cfg(tmp_path)
    assert find_in_cache(cfg, _app()).version == "21.16.2"

    _ipa(path, version="22.1.0", padding=4096)
    found = find_in_cache(cfg, _app())

    assert found is not None
    assert found.version == "22.1.0", "the memo answered for a file that changed"


def _within(seconds: float, fifo: Path, work):
    """`work()`'s answer, or a failure if it's still waiting after
    `seconds`, as a read of a FIFO with no writer waits."""
    import threading

    answer: list[object] = []
    worker = threading.Thread(target=lambda: answer.append(work()), daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        try:  # free the stuck reader
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass
        pytest.fail(f"waited on the FIFO at {fifo.name}")
    return answer[0]


def test_a_fifo_where_the_memo_goes_is_no_memo(tmp_path: Path) -> None:
    """Read as Graft's other small state files are (`data_dirs.
    read_small_json`): a plain read of a FIFO waited for a writer that never
    came, and with it the daily check and every build (T2-B5)."""
    _cached_ipa(tmp_path, "22.1.0")
    os.mkfifo(tmp_path / MEMO_FILENAME)

    found = _within(5, tmp_path / MEMO_FILENAME, lambda: find_in_cache(_Cfg(tmp_path), _app()))

    assert found is not None and found.version == "22.1.0"


def test_a_fifo_named_as_a_cached_ipa_is_skipped(tmp_path: Path) -> None:
    """Only a regular file is a candidate: opening a FIFO to read its
    Info.plist waited forever too."""
    real = _cached_ipa(tmp_path, "22.1.0")
    fifo = tmp_path / f"{BUNDLE}_99.0.ipa"
    os.mkfifo(fifo)

    found = _within(5, fifo, lambda: find_in_cache(_Cfg(tmp_path), _app()))

    assert found is not None and found.path == real
    assert _within(5, fifo, lambda: IPAInfoMemo.load(tmp_path).info_for(fifo)) is None


def test_a_corrupt_memo_does_not_break_find_in_cache(tmp_path: Path) -> None:
    _cached_ipa(tmp_path, "22.1.0")
    (tmp_path / MEMO_FILENAME).write_text("}{ not json")

    found = find_in_cache(_Cfg(tmp_path), _app())

    assert found is not None
    assert found.version == "22.1.0"


def test_the_memo_is_not_mistaken_for_a_cached_ipa(tmp_path: Path) -> None:
    """It lives in cache_dir; the globs there must not pick it up."""
    _cached_ipa(tmp_path, "22.1.0")
    cfg = _Cfg(tmp_path)
    find_in_cache(cfg, _app())

    assert (tmp_path / MEMO_FILENAME).exists()
    assert list(tmp_path.glob("*.ipa")) == [tmp_path / f"{BUNDLE}_22.1.0.ipa"]


def test_the_cli_survives_a_corrupt_memo(temp_config_env: Path) -> None:
    """`--list` is the GUI's entire app list; a bad memo may not cost the window.

    `--cached-ipa` is asserted alongside it because it reaches `find_in_cache`
    unconditionally, where `--list` short-circuits to `never` for an app with
    no delivery row — so on its own it would pass without the memo being
    consulted at all.
    """
    from typer.testing import CliRunner

    from graft_ipa.cli import app as cli_app
    from graft_ipa.config import load_config

    cfg = load_config(temp_config_env)
    _cached_ipa(cfg.cache_dir, "22.1.0")
    (cfg.cache_dir / MEMO_FILENAME).write_text("garbage")

    listing = CliRunner().invoke(cli_app, ["--json", "--list"])
    assert listing.exit_code == 0, listing.output
    assert '"type":"app"' in listing.output

    resolved = CliRunner().invoke(cli_app, ["--json", "--cached-ipa", "example"])
    assert resolved.exit_code == 0, resolved.output
    assert '"version":"22.1.0"' in resolved.output
