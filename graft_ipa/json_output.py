"""JSON-Lines event emitter used by the GUI subprocess contract.

When the CLI is invoked with --json, all user-facing output is replaced by a
stream of newline-delimited JSON objects on stdout. Each object has a "type"
field plus event-specific keys.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

# Every event type the CLI may put on the wire. This is half of the contract
# the GUI decodes against (`BuildEvent.decode` in Swift); the other half is
# CLIContract.eventsHandled, which a Swift test checks is a subset of this set
# via `--describe`.
#
# The registry is kept honest by `emit()` raising on anything unregistered:
# a new or renamed event blows up the first time any test exercises it,
# instead of being silently dropped by Swift's `try?` line decoding.
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "app",
        "app-added",
        "app-removed",
        "app-updated",
        # --- App Store acquisition (`appstore.py`) ---
        #
        # What the App Store path needs and has, from `--appstore-status`,
        # exactly once: `ipatool` {path, present, version, signed_in,
        # warning} and `unfairplay` {path, present, version, enrolled,
        # model_dir}, plus `setup_commands` — the Terminal commands for
        # whatever is missing, because signing in and enrolling are never done
        # by this tool. `signed_in` is a bool and nothing more: the account's
        # email and name are never on the wire.
        "appstore-status",
        # One app's App Store version against what is cached: `app`,
        # `bundle_id`, `store_version`, `cached_version`, `update_available`,
        # `error`. One per app, always — a lookup that failed is this event
        # with `store_version: null` and the reason in `error`, never silence,
        # so the GUI can tell "not asked" from "couldn't tell".
        "appstore-version",
        # `--appstore-get` (and an encrypted `--provide-ipa`) moving on to
        # `stage`: "download", "decrypt" or "stage". Its own event rather than
        # a `stage` name: STAGE_NAMES is the *build's* vocabulary and is held
        # equal to what a build emits.
        "appstore-stage",
        # What UnfairPlay reported: `app`, `encrypted_images`,
        # `decrypted_pages`, `plaintext_verified`. Emitted only for a decrypt
        # that was accepted — verified, or `--allow-unverified`.
        "appstore-decrypted",
        # One app's resolved icon artwork: bundle_id, path (a string), source
        # (one of "ipa" / "app-store" / "device", or "unknown" for a cached
        # sidecar this project didn't stamp itself — see
        # `artwork.SOURCE_UNKNOWN`), and whether it was served from the
        # on-disk cache rather than freshly resolved. Emitted once per app
        # that resolves, from `--artwork`. Absent for an app every rung
        # missed on — that is an ordinary outcome (the GUI's letter-tile
        # fallback is correct for it), not an error, so nothing goes on the
        # wire and the human-readable path is the only place a miss is
        # explained.
        "artwork",
        "build-end",
        "build-pruned",
        "build-record",
        "build-start",
        "cached-ipa",
        "canister-fetch-end",
        "canister-fetch-start",
        "canister-result",
        "canister-search-end",
        "cli-contract",
        "delivery-state",
        "device",
        # One connected device, from `--devices` (never `--brief`): `udid`,
        # `name`, `model`, `ios_version`, `connection_type`,
        # `livecontainer_bundle_id`, and `is_default` — whether this device's
        # `udid` matches the config's `default_device_udid`. At most one
        # `device` event per `--devices` call carries `is_default: true`;
        # zero do when the config names no default, or names one that is not
        # among the devices on the wire (a phone that is simply unplugged).
        #
        # `is_default` exists so the GUI can mark the configured build target
        # without a second subprocess: it used to spawn a whole extra
        # `--settings` invocation at launch just to learn this, then thread
        # the answer down through three views to reach the chip that shows
        # it. `cli._do_devices` already loads `cfg` to build this event and
        # was otherwise never reading it.
        #
        # One device as usbmux alone can describe it: `udid` and
        # `connection_type`, and deliberately nothing else. Emitted only by
        # `--devices --brief`, which never opens a lockdown session and so has
        # no name, model, iOS version, or `is_default` to report — `--brief`
        # answers only "which UDIDs are on the bus", and knowing the default
        # requires nothing more than the config already in hand, but adding
        # the field here would still be a field nothing reads on a path whose
        # whole point is being cheap.
        #
        # A separate type from `device` rather than a two-field version of it.
        # The GUI's `DeviceRecord` makes `name`, `model`, `ios_version` and
        # `connection_type` non-optional, so this payload under the `device`
        # name decoded to nil and was dropped by `try?`. The one-name
        # alternative — discriminate on whether `name` is present — would make
        # "a brief event" and "a full event that lost its name" the same
        # signal, turning a visible drop into a silent misclassification. And
        # no invocation emits both shapes, so nothing ever wants to decode "a
        # device event, either kind". See `cli._do_devices_brief`.
        #
        # `devices-end` is shared between the two paths on purpose: it has the
        # same shape and the same meaning on both, so one name costs nothing
        # there. The rule is about shape, not about which verb emitted it.
        "device-brief",
        "device-error",
        "device-app",
        "device-apps-end",
        # One device's storage and battery, plus `reachable`. Emitted exactly
        # once per `--device-info`, always — an unreachable phone is
        # `reachable: false` with four null stats, not silence.
        #
        # It used to be silence, and silence is indistinguishable from a
        # crashed CLI, a killed process, or a future bug that skips the emit.
        # Every other listing verb here has a sentinel (`devices-end`,
        # `device-apps-end`, `preflight-end`, `repos-end`); this one had none.
        # `read_device_info` really does distinguish "no phone" (None) from
        # "the phone declined a domain" (a record of nulls), and that
        # distinction is worth keeping in the library — but at the CLI boundary
        # it evaporated, because all-nulls and no-event render identically.
        #
        # The flag is what lets a store say "don't overwrite last-known-good":
        # a phone that misses one lockdown handshake must not blank a strip
        # that read 81% a moment ago. Nothing polls this verb — the GUI runs it
        # when a pane is pointed at a phone and when Refresh is pressed — so
        # the case it protects is a *press* landing on a blip, which is the
        # worst moment for the strip to empty: the gesture was meant to confirm
        # the reading. See `DeviceInfoStore`, which does the merging.
        #
        # Individual fields are null when the phone declined that domain, which
        # a locked phone routinely does — a separate condition from `reachable`.
        "device-info",
        "decrypt-start",
        "decrypt-end",
        # Whether a dump could run right now, and what is blocking it. Read-only
        # and quick, so a pane can ask before offering the action rather than
        # after spending thirty seconds failing.
        "decrypt-check",
        "devices-end",
        # Binaries that are still encrypted after stage 2.5. A warning, not a
        # failed build — the pipeline carries on, and `--skip-decrypt-check`
        # skips the check entirely — but the only channel that survives to a
        # machine consumer: the count used to ride in the `stage` event's extra
        # fields, which the GUI's decoder drops, and the detail existed only as
        # console text on the JSON stream's own stdout.
        "encrypted-binaries",
        # One staged IPA handed to PlayCover for installation on this Mac:
        # `filename`, `path`, `bundle_id`. Emitted when the `open` succeeds,
        # which means the file was handed over — **not** that PlayCover
        # finished installing it. `open` returns as soon as the event is
        # delivered, so a completion status is not available to us, and
        # claiming one would be a lie. PlayCover reports its own progress.
        #
        # Named for what happened rather than for the verb that triggered it:
        # `--install-to-mac` describes the user's intent and is fine for a
        # command, but "installed-to-mac" as an event name asserts a finished
        # install this app cannot observe — the GUI renders off this name, not
        # off this comment.
        "handed-to-playcover",
        "ipa-provided",
        # Which LiveContainer the IPA was pushed into, when more than one is
        # installed. The count used to ride in the `stage` event's extra fields
        # (dropped by the GUI's decoder) with the detail as console text on the
        # JSON stream's own stdout — so under --json the user had no way to
        # learn they were importing from the wrong container, and concluded the
        # tweak hadn't built. Same shape, and same reason, as
        # `encrypted-binaries` below.
        "livecontainer-choice",
        # LiveContainer itself, managed by `lc_manage` — see that module.
        #
        # `lc-status`: one per device asked about, always, unreachable or not
        # (`reachable: false` + `error`): `udid`, `instances` [{bundle_id,
        # version, role: primary|2|3|legacy, wildcard_signed}], `managed_by`
        # (graft|altstore|unknown), `latest` {version, channel} or null
        # when the lookup failed, `update_available`, and `sources` — the two
        # AltStore source URLs, so the GUI can offer the one to add without
        # spelling it itself.
        "lc-status",
        # `--lc-install`'s progress: `udid`, `instance` (null for the shared
        # fetch), `step` (fetch|prepare|profile|sign|install), `detail`.
        "lc-install-progress",
        # The install of `instance`/`bundle_id` has run ~20s: iOS has staged
        # it behind a running LiveContainer and applies it only when that is
        # closed. `message` is the sentence to show. Emitted at most once per
        # instance, and the verb keeps waiting.
        "lc-install-waiting",
        # One instance installed and recorded: `udid`, `bundle_id`, `instance`,
        # `version`, `channel`.
        "lc-installed",
        # The .p12 is in the primary instance's Documents: `udid`,
        # `bundle_id`, `path`, and `note` — that it stays there, readable by
        # guest apps, until `--lc-clean --cert-only` removes it.
        "lc-cert-pushed",
        # What `--lc-clean` removed (`removed`, names) and how many other
        # files it left alone (`kept`).
        "lc-cleaned",
        "log",
        "preflight-check",
        "preflight-end",
        "prune-end",
        "repo",
        "repo-added",
        "repo-package",
        "repo-packages-end",
        "repo-refresh-start",
        "repo-removed",
        "repos-end",
        "stage",
        # One IPA sitting in cache_dir, as its own Info.plist describes it:
        # bundle_id, version, display_name, filename, path, size_bytes, and
        # `preferred` — whether this is the file a build would reach for when
        # two versions of one app are staged. Emitted once per readable file
        # by `--staged-ipas`; unreadable files are skipped, exactly as
        # `find_in_cache` skips them.
        #
        # `stripped` is the bundle paths a dump removed to make this IPA
        # verify, and `lost` the plain-language capabilities that went with
        # them. `lost` is **absent** rather than empty when the sidecar
        # predates that field, because an empty list is the different claim
        # that something looked and found nothing worth naming. See
        # `ipa_inspect.StrippedNote`.
        "staged-ipa",
        # One staged IPA removed from the cache, by `--delete-staged`:
        # `filename`, and `also_removed` naming the `.stripped.json` sidecar
        # when there was one. Emitted after the removal, so its presence means
        # the file is gone rather than that it was asked for.
        "staged-ipa-deleted",
        # `--delete-staged` refused: the file is the last one some configured
        # app can build from, and `used_by` names which apps that is — the
        # same shape `tweak-in-use`'s `used_by` carries, and for the same
        # reason: `_do_delete_tweak`'s own comment records that recovering
        # this list by scraping the "is the only staged IPA for: ..." stderr
        # sentence was a shipped defect once already, and rewording that
        # sentence would silently degrade a GUI button while every test kept
        # passing. Emitted just before the refusal is raised.
        "staged-ipa-in-use",
        # The sentinel. A cache with nothing in it emits this alone, so the
        # GUI can tell "no IPAs staged" from "the listing never ran".
        "staged-ipas-end",
        "tweak",
        "tweak-attached",
        "tweak-deleted",
        "tweak-detached",
        "settings",
        "settings-updated",
        "tweak-in-use",
        # One per library file: the version it holds against the newest
        # published for the same package id. Emitted for every file, including
        # ones nothing publishes (anything built from source), because "we
        # looked and found nothing" and "we never looked" have to be
        # distinguishable from the GUI's side.
        "tweak-update",
        "tweak-updates-end",
        "tweak-updated",
    }
)

# ---------------------------------------------------------------------------
# Who consumes each event
# ---------------------------------------------------------------------------
#
# `EVENT_TYPES` says what the CLI may put on the wire. It said nothing at all
# about whether anybody reads it, and that silence is what let `device-error`
# exist for months: registered here, emitted on every `--devices` call, and
# absent from `CLIContract.eventsHandled` — so the GUI's `try?` line decoder
# dropped it, `--devices` exited 0, and a phone that usbmux saw but could not
# describe simply vanished from the picker with no explanation anywhere.
#
# So every registered event must now be accounted for exactly once:
#
#   EVENT_TYPES == CLIContract.eventsHandled | CLI_ONLY_EVENTS
#
# `tests/test_cli_event_contract.py` asserts that partition against the Swift
# source, and `--describe` publishes the dict below so the Swift side can assert
# the same thing from its end against the *installed* CLI. There is deliberately
# no third bucket for "the GUI will get to it": an event nothing renders is
# invisible however well it is documented, and a backlog is where that becomes
# a permanent address.


# Events the GUI is not expected to decode, with the reason each is CLI-only.
# An entry here is a claim that no user-visible state depends on the event —
# not a licence to ignore one. `CLIContract.cliOnlyEvents` declares the same set
# on the Swift side and the contract test checks the two agree, so this cannot
# become a place to park an event the GUI simply has not got to.
CLI_ONLY_EVENTS: dict[str, str] = {
    "cli-contract": (
        "the handshake itself. `--describe` prints this one event and exits; it "
        "is consumed by CLIContractTests and by tests/test_cli_describe.py, "
        "never by a view. A GUI that rendered it would be rendering the schema."
    ),
}

# The other side of the partition: every registered event some front-end is
# expected to decode. Derived, never listed, so it cannot drift from the two
# declarations it is the difference of.
#
# `cli._warn(..., carried_by=…)` is what reads it. Suppressing the `log` half of
# a warning is only safe if a *handled* event carries the same fact, and this is
# the set that makes that claim checkable rather than a comment. Naming a
# CLI-only or unregistered event there would suppress the one channel the GUI
# renders in favour of one it never decodes — a warning that exists nowhere.
GUI_EVENTS: frozenset[str] = EVENT_TYPES - frozenset(CLI_ONLY_EVENTS)

# ---------------------------------------------------------------------------
# Stage names
# ---------------------------------------------------------------------------
#
# The `stage` event carries a `name`, and that name is a second vocabulary
# hiding inside the first. `EVENT_TYPES` proves the GUI will recognise the
# *event*; it says nothing about the string inside it, and the GUI reads one of
# those strings for meaning rather than for display.
#
# `CLIContract.Stage.decryptionCheck == "verify"` is that reader.
# `ConfigStore.note(_:forBuildOf:)` treats `verify`'s `status: "end"` as the
# only signal that the cryptid-0 check reached a verdict — the CLI emits
# `encrypted-binaries` when it finds something and nothing at all when it
# doesn't, so "the check ran and the IPA is fine" has no event of its own. It
# is this stage ending without one. Rename the stage and no test fails, no line
# is dropped, nothing logs: the GUI simply never sees a verdict, and a card
# reading "N binaries are still encrypted — will crash on launch" stands
# forever over an IPA that is fine.
#
# So the names get the same treatment the event types get: registered here,
# enforced by `_line` raising on an unregistered one, published by `--describe`,
# and asserted from both sides.
#
# This set is deliberately *exactly* what the CLI emits, not a permit list of
# what it may emit — `tests/test_cli_stage_contract.py` asserts the equality
# against a parse of the package. A superset would be worse than nothing here:
# the Swift assertion asks "is the name I depend on one the CLI emits", and a
# stale entry left behind by a rename would answer yes about a name that has
# not been on the wire since.
STAGE_NAMES: frozenset[str] = frozenset(
    {
        # Stage 2 — download or reuse the cached IPA.
        "acquire",
        # Stage 4 (livecontainer delivery) — zip the tweaked .app.
        "archive",
        # Stage 3 — inject the tweaks with cyan.
        "cyan",
        # Stage 5 (sideload delivery) — ideviceinstaller.
        "install",
        # Stage 5 (livecontainer delivery) — push the IPA over usbmux,
        # which reaches a Wi-Fi-paired device as readily as a cabled one.
        "lc-push",
        # Stage 4 (sideload delivery) — zsign.
        "sign",
        # Stage 2.5 — the cryptid-0 decryption check. The one name the GUI
        # reads for meaning; see the note above before renaming it.
        "verify",
    }
)

# App-option fields stored under an app's `cyan:` block in config.yaml.
CYAN_OPTION_FIELDS: frozenset[str] = frozenset(
    {
        "display_name",
        "keep_bundle_id",
        "strip_extensions",
        "strip_supported_devices",
        "compression_level",
    }
)

# Every field name that can appear in an `app-updated` event's `changed` list.
# `_do_set_app_options` builds `changed` from exactly these names and refuses
# to run if its own parameter set drifts from them, so `--describe` advertises
# what the CLI can actually report rather than a hand-maintained copy.
#
# Swift's ConfigStore.updateAppOptions reconciles the fields it requested
# against `changed` and throws when one is missing, so a spelling drift here
# turns a successful write into a spurious "graft didn't apply X".
#
# Built from CYAN_OPTION_FIELDS rather than restating it: a typo in the cyan
# set would otherwise put a field in `changed` that never reaches the cyan
# block on disk — success reported for a value that was never written.
# Composing them means such a typo lands in this set too, where
# _do_set_app_options' parameter-set check catches it immediately.
APP_OPTION_FIELDS: frozenset[str] = CYAN_OPTION_FIELDS | frozenset(
    # Stored at the app's top level, not under `cyan:`.
    {"preferred_version", "delivery"}
)


class JsonEmitter:
    def __init__(self, stream: TextIO | None = None, tee: TextIO | None = None) -> None:
        """Emit one JSON object per line to `stream` (defaults to stdout).

        If `tee` is provided, every event is also written there — used to persist
        a per-build log file to disk while still streaming live to the GUI.
        """
        self.stream = stream if stream is not None else sys.stdout
        self.tee = tee

    def _line(self, event_type: str, fields: dict[str, Any]) -> str:
        if event_type not in EVENT_TYPES:
            raise ValueError(
                f"unregistered event type {event_type!r}: add it to "
                "EVENT_TYPES (and to CLIContract.eventsHandled on the Swift "
                "side if the GUI should react to it)"
            )
        # Raises rather than warns, for the same reason the line above does and
        # one more. A warning is invisible to the only consumer that matters:
        # under `--json` the GUI reads stdout and decodes with `try?`, and
        # anything written to stderr instead never reaches it — that is exactly
        # how `device-error` stayed hidden for months. "Warn" here would mean
        # "say nothing", which is the defect this registry exists to close.
        #
        # The counter-argument is that a `stage` event is on the path that
        # installs software to a phone, so raising can abort a real build. It
        # can — but a stage name is a string literal in cli.py, never derived
        # from user input (`test_every_stage_name_the_cli_emits_is_registered`
        # asserts that literalness), so an unregistered one is not a runtime
        # condition a user can hit. It is a source mistake that fires on the
        # *first* build anybody runs, in development, before a release. And the
        # build it aborts is one whose "will crash on launch" card the GUI can
        # no longer get right, which is not a build worth finishing quietly.
        if event_type == "stage":
            name = fields.get("name")
            if name not in STAGE_NAMES:
                raise ValueError(
                    f"unregistered stage name {name!r}: add it to STAGE_NAMES. "
                    "If you renamed a stage, check CLIContract.Stage on the "
                    "Swift side — ConfigStore reads `verify` for the "
                    "decryption verdict and a rename there fails silently."
                )
        return json.dumps({"type": event_type, **fields}, separators=(",", ":")) + "\n"

    def emit(self, event_type: str, **fields: Any) -> None:
        line = self._line(event_type, fields)
        self.stream.write(line)
        self.stream.flush()
        if self.tee is not None:
            self.tee.write(line)
            self.tee.flush()

    def emit_to_tee(self, event_type: str, **fields: Any) -> None:
        """Write one event to the tee alone, replaying it into a late log file.

        A build's tee log is opened *after* the build has started, because the
        directory it lives in can fail to open and that failure has to be
        reportable as a build-end — which means the first event of every build
        is already on the wire before the file exists. Replaying it here is what
        keeps the saved log a complete record: the GUI renders a replayed log
        from `build-start` onwards, and one that begins mid-build has no header.
        """
        if self.tee is None:
            return
        line = self._line(event_type, fields)
        self.tee.write(line)
        self.tee.flush()
