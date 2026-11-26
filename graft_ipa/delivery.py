"""What was delivered to the device, and whether it still matches the config.

The unit of comparison is an *inputs digest*: a hash over everything that
determines the artifact a build would produce. Two digests being equal means
building now would produce the same IPA that was last pushed.

Deliberately NOT hashed: the source IPA's bytes. It is identified by
(bundle_id, version), and the held build when a hold names one, which is
what `find_in_cache` resolves on, because
hashing it costs ~128 MB of reads (Example) or ~400 MB (Delta) per check. The
accepted gap is a cached IPA replaced in place under the same filename. Tweak
contents ARE hashed — they are ~181 KB in total for a real app and hashing them
takes ~47 ms, and a tweak rebuilt from source keeps its filename, which is the
single most likely way this repo's own workflow makes a phone stale.

Deliberately pure otherwise: this module reads tweak bytes off disk and
nothing else — no sqlite, no device I/O, no config mutation. That boundary is
what lets a status check, a push, and a GUI badge all call it and get the
same answer instead of three implementations that can disagree.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

# Real (non-TYPE_CHECKING) import: sha256_of is called below, not just
# annotated with. `config` types, by contrast, only ever type-annotate
# parameters this module never constructs, so they stay TYPE_CHECKING-only —
# that keeps this module's runtime dependency surface to "hashlib + one
# function from build_log", not the whole config/yaml stack.
from .build_log import sha256_of

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import AppConfig, Config

# The digest's on-disk/persisted format. Bump this when what gets hashed
# changes (adding a field, fixing an omission like `extra_args` below) so a
# comparison against an old recorded digest can be explained as "format
# changed" rather than silently reporting every app stale with no reason.
FORMAT_VERSION = 1

# How many reasons a state names before the rest collapse into "+N more
# changes". Two is what fits a badge; see `diff_delivery`, where the *order*
# reasons are emitted is what decides which two survive.
MAX_REASONS = 2


@dataclass(frozen=True)
class TweakFingerprint:
    name: str
    sha256: str | None  # None when the file couldn't be hashed; see `error`
    # This module's own vocabulary, not CPython's exception taxonomy: "absent"
    # (the containing directory is readable and the file just isn't in it) or
    # "unreadable" (anything else — permission denied, is-a-directory, an
    # unmounted tweaks_dir). None on success. Deliberately excluded from the
    # hashed payload below: a transient read failure isn't a build input and
    # must never move the digest.
    error: str | None


@dataclass(frozen=True)
class InputsDigest:
    """The digest plus the structured inputs that produced it.

    Every key in `compute_inputs_digest`'s hashed payload must be carried here
    as a field, and the payload — not this field list — is the authority on
    what that set is. An input that is hashed but not carried can never get a
    reason branch in `diff_delivery`, so a change to it moves the digest and
    then reports as the fallback reason: a confident, wrong explanation.
    `bundle_id` was exactly that for two rounds of review.
    """

    digest: str
    version: int
    bundle_id: str
    source_version: str
    delivery: str
    tweaks: tuple[TweakFingerprint, ...]
    cyan: dict[str, object]
    #: The App Store version id of the build the app is held at, when a hold
    #: names one (`AppConfig.preferred_version_id`), else None. Two builds of
    #: one number read the same `source_version`, and a build of a hold by
    #: build starts from that build's own file (`ipa_cache.admits`), so moving
    #: the hold between them changes the artifact. Hashed only when set, so a
    #: digest for an app without one is what it always was.
    held_version_id: str | None = None

    @property
    def missing(self) -> list[str]:
        """Tweaks referenced by config that are genuinely absent from disk.

        Deliberately narrower than "every tweak that failed to hash": a
        permission error or an unmounted tweaks_dir means "can't verify", not
        "not here" — collapsing those into `missing` would make an offline
        network volume read as every tweak in the app having disappeared.
        """
        return [t.name for t in self.tweaks if t.error == "absent"]


# Cyan keys that change the produced IPA. Listed explicitly rather than
# derived from the dataclass, so adding an unrelated field to CyanOptions
# can't silently invalidate every recorded delivery. `getattr` below has no
# default: a renamed field must raise, not quietly stop contributing to the
# digest. test_delivery.py pins this tuple to CyanOptions' full field set.
CYAN_KEYS = (
    "display_name",
    "new_bundle_id",
    "keep_bundle_id",
    "strip_extensions",
    "strip_supported_devices",
    "compression_level",
    "extra_args",
)


def _fingerprint(name: str, path: Path) -> TweakFingerprint:
    """Hash one tweak file, or classify why it couldn't be hashed.

    Reuses `build_log.sha256_of` for the actual chunked read so there is
    exactly one place that decides the chunk size.

    The absent/unreadable split can't be read off the exception type alone:
    `FileNotFoundError` is raised both for a file that simply isn't in its
    (readable) directory *and* for a directory that isn't mounted at all —
    the latter is "can't tell", not "gone", and conflating them is exactly
    what would make an unmounted tweaks_dir report every tweak as missing.
    So "absent" additionally requires the parent directory to be readable;
    everything else (permission denied, is-a-directory, an absent parent,
    ELOOP, ...) is "unreadable".
    """
    try:
        return TweakFingerprint(name=name, sha256=sha256_of(path), error=None)
    except OSError as e:
        absent = isinstance(e, FileNotFoundError) and path.parent.is_dir()
        return TweakFingerprint(name=name, sha256=None, error="absent" if absent else "unreadable")


def _freeze(value: object) -> object:
    """Make a CyanOptions field value safe to store in a frozen InputsDigest.

    Only `extra_args` needs this today (a `list[str]`) — everything else in
    CyanOptions is an immutable scalar. Without it, `cyan[k]` would alias
    `app.cyan.extra_args` itself, so mutating the live config after computing
    a digest would silently change what the "frozen" record reports. A tuple
    serializes to the same JSON array as a list (verified: `json.dumps`
    doesn't distinguish them), so this doesn't touch the hashed bytes.
    """
    return tuple(value) if isinstance(value, list) else value


def compute_inputs_digest(cfg: Config, app: AppConfig, *, source_version: str) -> InputsDigest:
    """Digest the inputs that determine what a build of `app` would produce."""
    fingerprints = [_fingerprint(entry, cfg.tweak_path(entry)) for entry in app.tweaks]

    cyan: dict[str, object] = {k: _freeze(getattr(app.cyan, k)) for k in CYAN_KEYS}

    # A hold by build, as `appstore.held_version_id` reads it: `latest` names
    # none, whatever an AppConfig built by hand carries.
    held_version_id = app.preferred_version_id if app.preferred_version != "latest" else None

    # Order matters and is config order, because that is the order cyan injects.
    payload: dict[str, object] = {
        "v": FORMAT_VERSION,
        "bundle_id": app.bundle_id,
        "source_version": source_version,
        "delivery": app.delivery,
        "tweaks": [[f.name, f.sha256] for f in fingerprints],
        "cyan": cyan,
    }
    # Only when there is one: every digest recorded for an app with no hold by
    # build (all of them, before 2.3) hashes the same bytes as before, so no
    # FORMAT_VERSION bump and no row read as `unknown`.
    if held_version_id is not None:
        payload["held_version_id"] = held_version_id
    # ensure_ascii and the utf-8 encode are the other two knobs (besides
    # separators, pinned above) that change the hashed bytes. Both are
    # already the default; naming them makes that a choice, not an
    # inheritance, and ensure_ascii=True keeps a non-ASCII display_name
    # escaping the same way on every machine that computes this digest.
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )

    return InputsDigest(
        digest=hashlib.sha256(blob).hexdigest(),
        version=FORMAT_VERSION,
        bundle_id=app.bundle_id,
        source_version=source_version,
        delivery=app.delivery,
        tweaks=tuple(fingerprints),
        cyan=cyan,
        held_version_id=held_version_id,
    )


@dataclass(frozen=True)
class DeliveredSnapshot:
    """The parts of a delivery row the comparison needs."""

    inputs_digest: str | None
    digest_version: int | None
    bundle_id: str
    source_version: str
    delivery: str
    # As stored. The annotation documents the shape the writer produces; it is
    # not enforcement, and `diff_delivery` does not assume the row honours it —
    # see `_tweak_map`'s caller.
    tweaks: list[list[str | None]]
    pushed_at: str
    # Carried, deliberately not diffed. Task 5 reads the `cyan` column into
    # this, so the field has to exist for the row to round-trip; naming *which*
    # option changed instead of "build options changed" is a separate decision
    # about user-facing copy that nobody has made yet. Defaulted so a row
    # written before the column existed still constructs.
    cyan: dict | None = None
    # `InputsDigest.held_version_id` as the row stored it; None for a row with
    # no hold by build, and for one from before the column.
    held_version_id: str | None = None


@dataclass(frozen=True)
class DeliveryState:
    kind: str  # never | stale | current | unknown
    summary: str
    reasons: list[str] = field(default_factory=list)
    pushed_at: str | None = None


def _tweak_map(pairs) -> dict[str, str | None]:
    return {name: sha for name, sha in pairs}


def _render_option_value(value: object) -> str:
    """Format one cyan option's value for a reason string.

    `None` and `True`/`False` are the implementation's vocabulary, and a badge
    is the last place to leak them — the same objection as rendering a tuple
    repr for `extra_args`. Numbers and strings are already the user's own
    words (a display name, a bundle id, a compression level), so they pass
    through untouched.
    """
    if value is None:
        return "(unset)"
    if isinstance(value, bool):  # before anything numeric: bool subclasses int
        return "yes" if value else "no"
    return str(value)


def diff_delivery(current: InputsDigest, delivered: DeliveredSnapshot | None) -> DeliveryState:
    """Compare what a build would produce now against what was last pushed.

    Returns `unknown` rather than guessing whenever the comparison is not
    trustworthy — an old record, a digest this build can't reproduce, or tweak
    files it can't currently read. `unknown` is treated as `current` by callers,
    because a badge that fires when the tool is merely confused is a badge the
    user learns to ignore.
    """
    if delivered is None:
        return DeliveryState(kind="never", summary="Never delivered to a device.")

    # A row written before this feature existed has no digest to compare, so
    # report `unknown` (which callers render as "no badge") rather than
    # `stale`: nagging about history it cannot reconstruct would fire on every
    # app at once and teach the user to ignore the badge.
    if not delivered.inputs_digest:
        return DeliveryState(
            kind="unknown",
            summary="Delivered before this tool tracked build inputs.",
            pushed_at=delivered.pushed_at,
        )

    # Likewise for a digest produced by a different hashing format: the
    # structured parts below would all compare equal while the digests differ,
    # so we'd report "stale" and be unable to name a single reason.
    if delivered.digest_version != current.version:
        return DeliveryState(
            kind="unknown",
            summary=(
                "Delivered by a different version of this tool. "
                "Push again to re-establish the baseline."
            ),
            pushed_at=delivered.pushed_at,
        )

    # A tweak that exists but can't be read hashes to None — the same
    # contribution an absent one makes. Comparing anyway would report every
    # tweak in the app as rebuilt when the real problem is an unmounted volume.
    unreadable = [t.name for t in current.tweaks if t.error == "unreadable"]
    if unreadable:
        return DeliveryState(
            kind="unknown",
            summary=f"Can't read {', '.join(unreadable)} — unable to compare.",
            pushed_at=delivered.pushed_at,
        )

    # Deliberately "the last delivery", not "the phone": for livecontainer a
    # push only writes a file into Documents/, the import that actually
    # installs it is manual and unverified, and nothing here has touched the
    # device at all — this compares against our own local record. Spec §3 and
    # §10.1: say "delivered", never "installed".
    if delivered.inputs_digest == current.digest:
        return DeliveryState(
            kind="current",
            summary="Up to date with the last delivery.",
            pushed_at=delivered.pushed_at,
        )

    try:
        was = _tweak_map(delivered.tweaks)
        now = _tweak_map([[t.name, t.sha256] for t in current.tweaks])
    except (TypeError, ValueError):
        # A stored row we can't parse. Everywhere else this module answers
        # "unknown" rather than guess; this was the one place that raised —
        # out of a loop that renders every app, so a single bad row took down
        # the whole list instead of one badge.
        return DeliveryState(
            kind="unknown",
            summary="The delivery record can't be read. Push again to re-establish the baseline.",
            pushed_at=delivered.pushed_at,
        )

    gone = set(current.missing)
    reasons: list[str] = []

    # What follows is a priority, not a sequence. Everything past MAX_REASONS
    # collapses into "+N more changes", so the order these are appended in is
    # what decides which ones the user actually gets to see.

    # 1. Reasons that predict a *failed* build rather than merely a different
    #    one: cyan_runner raises "missing tweak files:" on all of these.
    for name in now:
        if name not in gone:
            continue
        # Attached in config either way; the distinction is whether it was ever
        # delivered. Both hash to None, so the rebuilt check below would call
        # this a rebuild and send the user looking for a change that never was.
        delivered_before = name in was
        reasons.append(
            f"{name} missing from disk" if delivered_before else f"{name} added (not on disk)"
        )

    # 2. Identity of the artifact as a whole.
    if current.bundle_id != delivered.bundle_id:
        reasons.append(f"bundle id changed: {delivered.bundle_id} → {current.bundle_id}")
    if current.source_version != delivered.source_version:
        # "app version" rather than a bare "version": this renders next to a
        # list of .deb filenames, where "version changed" reads as a tweak's.
        reasons.append(
            f"app version changed: {delivered.source_version} → {current.source_version}"
        )
    elif current.held_version_id != delivered.held_version_id:
        # Two builds of one number: the version reads the same, and the file
        # a build starts from doesn't. `elif`, because a build of another
        # version is already named by the line above.
        reasons.append("the held build changed")
    if current.delivery != delivered.delivery:
        reasons.append(f"delivery mode changed: {delivered.delivery} → {current.delivery}")

    # 3. Which tweaks are attached. Names in `gone` are skipped — bucket 1
    #    already named them, with the wording that carries the warning.
    for name in now:
        if name not in was and name not in gone:
            reasons.append(f"{name} added")
    for name in was:
        if name not in now:
            reasons.append(f"{name} removed")

    # 4. Contents of a tweak that is attached in both.
    for name, sha in now.items():
        if name in was and name not in gone and was[name] != sha:
            reasons.append(f"{name} rebuilt since delivery")

    # 5. Which build option changed. Iterating CYAN_KEYS rather than either
    #    dict keeps the order deterministic and stops a key a stored row still
    #    carries, but the digest no longer hashes, from inventing a reason. (A
    #    row whose key set predates a CYAN_KEYS change was written under an
    #    older FORMAT_VERSION, which the guard above already answered
    #    `unknown` for — this loop never sees one.)
    if delivered.cyan is not None:
        for key in CYAN_KEYS:
            # Freeze both sides before comparing: `current.cyan` holds
            # extra_args as a tuple, while a row that has been through
            # json.loads holds a list. Those are never equal, so comparing raw
            # would tell every stale app with extra_args set that its build
            # arguments changed when only the app version did.
            before = _freeze(delivered.cyan.get(key))
            after = _freeze(current.cyan.get(key))
            if before == after:
                continue
            if key == "extra_args":
                # Named without values on purpose: a sequence rendered into a
                # badge reads as Python source, not as something the user typed.
                reasons.append("extra build arguments changed")
            else:
                reasons.append(
                    f"{key.replace('_', ' ')} changed: "
                    f"{_render_option_value(before)} → {_render_option_value(after)}"
                )

    # 6. The shape of the list itself, which only makes sense to report when
    #    the set is unchanged: after an add or a remove, a length or position
    #    difference is a consequence of that edit rather than an independent
    #    cause, and naming both would burn the cap on one event.
    if set(was) == set(now):
        # config.py doesn't dedup and cyan_runner passes every entry with -f,
        # so a repeated entry really does change the artifact. `_tweak_map`
        # collapses duplicates, which is why this compares the stored lengths
        # instead — and why direction matters: telling someone a tweak "is
        # listed more than once" when they just removed the duplicate is the
        # same wrong-explanation failure as blaming build options.
        if len(current.tweaks) > len(delivered.tweaks):
            reasons.append("a tweak is listed more than once")
        elif len(current.tweaks) < len(delivered.tweaks):
            reasons.append("a duplicate tweak entry was removed")
        # Order is a real build input — cyan injects in config order. dict
        # preserves insertion order (guaranteed since 3.7; this repo targets
        # 3.11), so these are the delivered and current orderings.
        if list(was) != list(now):
            reasons.append("tweak order changed")

    if not reasons:
        # Every hashed input now has its own branch above — bundle id, source
        # version, the held build, delivery mode, tweak set, tweak contents, tweak order,
        # duplicate entries, and each cyan option by name. So this is no longer
        # a fallback for "some build option": it is the legacy-row path, and
        # reachable only when `delivered.cyan is None`, i.e. a row written
        # before the cyan column existed, where there is genuinely nothing to
        # compare against.
        #
        # Any *other* way of landing here is a bug: an input that the payload
        # in `compute_inputs_digest` hashes without a matching branch. The
        # record/payload invariant documented on InputsDigest, and the test
        # that pins it, are what keep the two lists in step.
        #
        # "build options" rather than "cyan" because that is what the GUI calls
        # them; cyan is an implementation detail the user never sees.
        reasons.append("build options changed")

    # `> MAX_REASONS + 1`, not `> MAX_REASONS`: at exactly one over, "+1 more
    # changes" spends a line to say less than the reason it replaced. "changes"
    # rather than a bare "+N more" because it has to stand alone in a badge.
    if len(reasons) > MAX_REASONS + 1:
        extra = len(reasons) - MAX_REASONS
        reasons = [*reasons[:MAX_REASONS], f"+{extra} more changes"]

    return DeliveryState(
        kind="stale",
        summary="Changed since the last delivery.",
        reasons=reasons,
        pushed_at=delivered.pushed_at,
    )
