"""Retention policy for the signed-IPA archive in ``built_dir``.

Every successful build drops an ``<app>_<version>_<timestamp>.ipa`` into
``built_dir`` and nothing has ever removed one, so a few debugging sessions
turn into gigabytes of near-identical rebuilds. This module works out *which*
archives a keep-N-per-``(app, version)`` policy would drop; the CLI owns the
actual removal (and the build-log bookkeeping that must accompany it).

The grouping key is ``(app, version)``, not ``app``, because the waste this
exists to bound is *rebuild churn*, not version history: a night of debugging
leaves fourteen near-identical builds of one upstream version. Keeping N per
app spends the whole budget on that one night and evicts the last archive of
the version actually shipped last month — the one thing worth rolling back to.
Keeping N per version collapses the churn and leaves one archive set per
version, so ``keep`` reads as "how many rebuilds of a given version do I keep".

Deliberately pure: no filesystem mutation, no sqlite, no console. That keeps
the "what would go" decision testable on its own and lets ``--dry-run`` and the
real prune share one answer instead of two implementations that can disagree.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# `_build_one` names archives `f"{app_name}_{cached.version}_{ts}.ipa"` with
# ts = time.strftime("%Y%m%dT%H%M%S").
#
# Parsed right-to-left, because the app name is the field that can contain an
# underscore: config keys come from `slugify_name`, which turns every run of
# non-alphanumerics into "_", and `add_app`'s dedup appends "_2". Anchoring on
# the fixed-width timestamp and a dotted version keeps `my_app_1.0_...ipa`
# grouping under "my_app" rather than "my". (A version string containing an
# underscore would be mis-split — the greedy app capture would swallow its
# first half — but App Store versions are dotted numerics.)
_ARCHIVE_RE = re.compile(
    r"^(?P<app>.+)_(?P<version>[^_]*)_(?P<stamp>\d{8}T\d{6})\.ipa$"
)


@dataclass(frozen=True)
class Archive:
    """One parsed file in ``built_dir``."""

    path: Path
    app: str
    version: str
    stamp: str  # "20260602T014429" — lexicographic order is chronological order
    size_bytes: int


@dataclass(frozen=True)
class PrunePlan:
    """What a keep-N-per-(app, version) policy would drop, and what it leaves."""

    remove: list[Archive]
    kept: list[Archive]
    unparsed: list[Path]

    @property
    def bytes_removed(self) -> int:
        return sum(a.size_bytes for a in self.remove)

    @property
    def apps(self) -> list[str]:
        """Apps that give something up. Deliberately coarser than the key.

        Grouping is per ``(app, version)``, but "which apps did this touch" is
        the question a summary line wants to answer; the per-version detail is
        already in every removed archive's filename.
        """
        return sorted({a.app for a in self.remove})


def parse_archive(path: Path) -> Archive | None:
    """Parse an archive filename, or return None if it isn't one of ours.

    Returns None rather than guessing for anything that doesn't match the
    pattern *this tool* writes. A file we can't attribute to an app *and a
    version* has no place in a per-(app, version) retention decision, and
    pruning is destructive: the safe failure mode is to leave the stranger
    alone.
    """
    m = _ARCHIVE_RE.match(path.name)
    if m is None:
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    return Archive(
        path=path,
        app=m["app"],
        version=m["version"],
        stamp=m["stamp"],
        size_bytes=size,
    )


def scan_archives(built_dir: Path) -> tuple[list[Archive], list[Path]]:
    """Split ``built_dir`` into parseable archives and everything else.

    A missing directory is an empty archive, not an error — pruning before the
    first build is a no-op, not a failure.
    """
    archives: list[Archive] = []
    unparsed: list[Path] = []
    try:
        entries = sorted(built_dir.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return archives, unparsed
    for entry in entries:
        # Same rule `_collect_tweak_records` uses: a real file, not a dotfile.
        # That keeps .DS_Store and friends out of the "unparsed" report.
        if entry.name.startswith(".") or not entry.is_file():
            continue
        parsed = parse_archive(entry)
        if parsed is None:
            unparsed.append(entry)
        else:
            archives.append(parsed)
    return archives, unparsed


def plan_prune(
    built_dir: Path, *, keep: int, exclude: Iterable[Path] = ()
) -> PrunePlan:
    """Decide which archives a "newest `keep` per (app, version)" policy drops.

    Each upstream version gets its own budget, so a rebuild storm on the
    version you're debugging today can never evict the last archive of the
    version you shipped last month. The cost is that the directory is bounded
    by `keep` times the number of versions built rather than by `keep` times
    the number of apps — bounded in the axis that grows slowly (versions you
    actually ship) instead of the one that grows in a single evening (rebuilds
    of one version).

    Recency comes from the timestamp *in the filename*, not st_mtime. The
    filename records when the build actually ran and is immutable once written;
    mtime is not — a Time Machine restore, an rsync of the cache, or dragging a
    file back out of the Trash all rewrite it, and any of those would silently
    reorder which rebuild counts as "newest". The timestamp is also what the
    user sees when they list the directory, so the kept set matches the one
    they'd predict.

    `exclude` names paths that must survive regardless — the archive the
    current build just produced. It does not buy an extra keep slot; it only
    vetoes removal, so `keep: 0` still means "keep nothing but what I just
    built".

    Raises ValueError for a negative `keep`.
    """
    if keep < 0:
        raise ValueError(f"keep must be >= 0, got {keep}")

    archives, unparsed = scan_archives(built_dir)
    spared = {_identity(p) for p in exclude}

    # One bucket per (app, version). Both halves are load-bearing: version
    # strings are not unique across apps — `bravo_1.0_*` and `delta_1.0_*`
    # must not share a budget — and app alone is the grouping this replaced.
    by_version: dict[tuple[str, str], list[Archive]] = {}
    for archive in archives:
        by_version.setdefault((archive.app, archive.version), []).append(archive)

    remove: list[Archive] = []
    kept: list[Archive] = []
    for key in sorted(by_version):
        # Newest first; the filename tiebreaks so equal-second rebuilds have a
        # stable order rather than whatever iterdir happened to yield.
        ordered = sorted(
            by_version[key], key=lambda a: (a.stamp, a.path.name), reverse=True
        )
        kept.extend(ordered[:keep])
        for archive in ordered[keep:]:
            if _identity(archive.path) in spared:
                kept.append(archive)
            else:
                remove.append(archive)

    return PrunePlan(remove=remove, kept=kept, unparsed=unparsed)


def _identity(path: Path) -> str:
    """Normalized path string, matching `Config.tweak_key`'s fallback form.

    Used only to compare a caller-supplied exclusion against a scanned entry;
    both are built from the same `built_dir`, so this just absorbs the trivial
    spelling differences (a trailing slash, a redundant "./").
    """
    return os.path.normcase(os.path.normpath(str(path)))
