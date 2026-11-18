"""A sidecar memo of Info.plist reads, keyed by ``(path, size, mtime_ns)``.

``find_in_cache`` opens every candidate IPA and parses its zip central
directory to confirm the bundle id and version. The cost tracks the archive's
*entry count*, not its byte size (~22 ms at 7,400 entries; ~33 ms at 11,000),
and a ``preferred_version: latest`` pin cannot short-circuit — it has to open
every retained version of an app to know which one is newest. The delivery
state feature put that call on ``--list``, which the GUI runs on every refresh,
so a realistic 3 apps x 3 retained versions cost ~180-300 ms of pure re-reading
per refresh, every refresh, of files that had not changed.

This turns each of those opens into a ``stat()`` after the first. The memo
lives in the cache directory it describes (``.ipa-info.json``), so deleting the
IPA cache deletes its memo with it and there is no orphan state to reconcile;
it is regenerable by definition.

**Why not just parse the filename.** Cached IPAs are named
``{bundle_id}_{version}.ipa``, which looks like the same information for free.
It is not: the plist read is exactly what stops a mis-named or hand-copied file
being trusted, and ``--provide-ipa`` stages user-supplied files under that
canonical name, so a wrong file under a right name is reachable. The memo keeps
reading the plist — it just remembers the answer.

**Why ``(size, mtime_ns)`` is enough.** Any rewrite of a file changes at least
one of the two. A replacement with byte-identical size *and* identical
nanosecond mtime is not a case worth defending against, and defending against
it means hashing hundreds of megabytes — the cost this exists to avoid.

**Everything degrades.** ``--list`` renders the GUI's entire app list, so a
memo that is missing, truncated, corrupt, unreadable, or written by a future
version must cost a plist read and nothing else. No method here raises.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .ipa_inspect import IPAInfo, read_info_plist

MEMO_FILENAME = ".ipa-info.json"

# Bumped when the entry shape changes incompatibly. A memo written by a
# different format is discarded wholesale rather than merged, because a partial
# read of a shape we don't know is how a wrong version gets returned.
MEMO_FORMAT = 1

_FIELDS = ("bundle_id", "version", "display_name", "executable", "info_plist_path_in_zip")


def _entry_to_info(entry: Any) -> IPAInfo | None:
    """Rebuild an ``IPAInfo`` from a memo entry, or ``None`` if it isn't one.

    Validates every field's type. A hand-edited or half-written memo must read
    as a miss, not as an ``IPAInfo`` with ``None`` where a version belongs —
    that value would flow straight into the delivery digest.
    """
    if not isinstance(entry, dict):
        return None
    values = {}
    for field in _FIELDS:
        value = entry.get(field)
        if not isinstance(value, str):
            return None
        values[field] = value
    if not values["bundle_id"]:
        # read_info_plist raises rather than return an empty bundle id; a memo
        # claiming one did not come from it.
        return None
    return IPAInfo(**values)


class IPAInfoMemo:
    """Memoized ``read_info_plist`` over the files in one directory.

    Usage is load / query / :meth:`save`. Nothing is written unless a lookup
    actually missed, so a warm ``--list`` is reads only.
    """

    def __init__(self, directory: Path) -> None:
        self.path = Path(directory) / MEMO_FILENAME
        self._entries: dict[str, Any] = {}
        self._dirty = False

    @classmethod
    def load(cls, directory: Path) -> IPAInfoMemo:
        memo = cls(directory)
        memo._read()
        return memo

    def _read(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            # Absent, unreadable, a directory, or not JSON. All the same
            # answer: start empty and re-read the plists.
            return
        if not isinstance(raw, dict) or raw.get("format") != MEMO_FORMAT:
            return
        entries = raw.get("entries")
        if not isinstance(entries, dict):
            return
        self._entries = entries

    def info_for(self, ipa: Path) -> IPAInfo | None:
        """The IPA's ``IPAInfo``, from the memo when it is still valid.

        Returns ``None`` for anything ``read_info_plist`` refuses — a missing
        file, a non-zip, an archive with no ``Payload/<App>.app/Info.plist`` —
        which is the same "skip this candidate" signal ``find_in_cache``
        already acted on.
        """
        key = os.path.abspath(ipa)
        before = _stat_key(key)
        if before is not None:
            hit = self._entries.get(key)
            if isinstance(hit, dict) and (hit.get("size"), hit.get("mtime_ns")) == before:
                info = _entry_to_info(hit)
                if info is not None:
                    return info

        try:
            info = read_info_plist(ipa)
        except Exception:
            # Deliberately not memoized. A negative entry would need its own
            # validity rules and its own way to go stale, to save re-opening
            # files that should not be in the cache directory at all; the one
            # file the tool itself puts there mid-write (`.<bundle>.partial.ipa`)
            # is not a `find_in_cache` candidate, and a non-zip fails on the
            # 4-byte magic check without touching the central directory.
            return None

        # Stat again and only record when both agree. Between the first stat
        # and the read, `--provide-ipa` (or a download landing) can replace the
        # file — recording the pre-write identity against post-write contents
        # would pin a wrong version until the *next* write, and the delivery
        # digest would describe an IPA that was never built from.
        after = _stat_key(key)
        if after is not None and after == before:
            self._entries[key] = {
                "size": after[0],
                "mtime_ns": after[1],
                **{field: getattr(info, field) for field in _FIELDS},
            }
            self._dirty = True
        return info

    def save(self) -> None:
        """Write the memo back if a lookup missed. Never raises.

        That "never" used to be aspirational rather than enforced:
        `json.dumps` sat outside this method's own `try`, so an `_entries`
        value it could not serialize raised straight out of here — which
        happened for real, once, when an uncoerced `CFBundleExecutable` (a
        plist `<date>`) reached an entry as a raw `datetime`; see
        `ipa_inspect.read_info_plist`'s own comment on that field. That
        specific value is coerced now and cannot recur through this class's
        one caller — but `--list`/`--staged-ipas` render the GUI's entire app
        listing through `IPAInfoMemo`, and `_do_staged_ipas`'s `finally:
        memo.save()` runs *after* it has already emitted some `staged-ipa`
        lines and *before* the `staged-ipas-end` sentinel. A raise here does
        not just lose the memo's speedup — it kills that listing mid-stream,
        which `StagedIPAStore.load()`'s own doc says leaves `hasLoaded` false
        forever. `json.dumps` is inside the `try` and `TypeError` alongside
        `OSError` below so that stays true structurally, not only for as long
        as every field `IPAInfo` ever grows happens to stay coerced.
        """
        if not self._dirty:
            return
        # Drop entries whose file is gone, so retention sweeps and manual
        # deletions don't leave the memo growing forever. Only on a write, and
        # only over the handful of IPAs one cache directory holds.
        entries = {k: v for k, v in self._entries.items() if os.path.exists(k)}
        try:
            payload = json.dumps({"format": MEMO_FORMAT, "entries": entries})
            self._replace(payload)
        except (OSError, TypeError):
            # OSError: a read-only or full cache directory. TypeError: an
            # entry `json.dumps` cannot serialize — see this method's own
            # doc. Either way this costs the memo, not the listing reading
            # through it; the next call re-earns the speedup by re-reading
            # whichever plists didn't make it into a memo this time.
            return
        self._entries = entries
        self._dirty = False

    def _replace(self, payload: str) -> None:
        """Write ``payload`` to a sibling temp file and rename it into place.

        Atomic rather than a plain ``write_text`` because two ``--list`` runs
        (or a ``--list`` racing a build) can write at the same time, and a
        reader must never see a truncated memo. Last writer wins, which at
        worst costs the loser's entries one re-read. No ``fsync``: a memo lost
        to a crash is a memo regenerated on the next call.
        """
        directory = self.path.parent
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(payload)
            os.replace(tmp_name, self.path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise


def _stat_key(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)
