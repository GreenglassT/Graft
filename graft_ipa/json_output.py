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
        # `--list`'s first line, exactly once: `account`, `"free"` or
        # `"paid"`. Its own event because `--list` on a config with no apps
        # emits no `app` to carry the field, and that is every fresh install.
        "account",
        # One configured app, from `--list`: its config fields, plus
        # `account` — `"free"` (no `signing:` section: a free Apple ID,
        # LiveContainer delivery only) or `"paid"`. Config-wide, repeated on
        # every app because this is the event the GUI reads at launch.
        "app",
        "app-added",
        "app-removed",
        "app-updated",
        # --- App Store acquisition (`appstore.py`) ---
        #
        # What the App Store path needs and has, from `--appstore-status`,
        # exactly once: `ipatool` {path, source, present, version, signed_in,
        # warning, install} and `unfairplay` {path, source, present, version,
        # enrolled, model_dir, install}, plus `setup_commands` — the Terminal
        # commands for whatever is missing (the app shows them only for a
        # missing tool: it has buttons to sign in and enroll). `install` is the one command
        # that installs that tool (a string, for a Copy button), or null when
        # it needs none or none can work here (UnfairPlay on an Intel Mac).
        # `ipatool.email` is the signed-in Apple ID's email (the Settings pane
        # shows it), or null; the account's name is never on the wire.
        # `ipatool.legacy_signin` is true for a sign-in another ipatool made,
        # which Graft's own can't read (`appstore_account.legacy_signin`);
        # `ipatool.unknown_reason` says why a `signed_in` of null that isn't
        # one is null: "no-verdict" (`auth info` gave none),
        # "keychain-no-answer" (`security` couldn't say, and ipatool wasn't
        # asked), "keychain-locked" (the login keychain is locked, so
        # neither `security` nor ipatool was asked) or
        # "graft-ipatool-signin" (the ipatool is any but Graft.app's own,
        # and the sign-in is Graft.app's own ipatool's, which this one can't
        # read without Keychain dialogs, so it wasn't asked;
        # `appstore_account.graft_signin`), else null;
        # `ipatool.unknown_message` is the CLI's own sentence for that reason,
        # with what to do (`ipatool.UNKNOWN_MESSAGES`), for the app's account
        # row, or null when `unknown_reason` is;
        # `unfairplay.model_problem` says why a model that exists can't be
        # used (not this user's, not readable), or is null; and
        # `ipatool.note` and `unfairplay.note` say why the tool config.yaml
        # names isn't the one that runs, with what to do, or are null: a
        # configured path that isn't there any more, so Graft.app's own copy
        # runs (`appstore.missing_path_note`), or for ipatool the 1.x default
        # spelled out (`appstore.ipatool_setting_note`, which goes into
        # `warning` instead when there is one). Each is also sent as a `log`
        # warning before this event, and printed in Terminal.
        "appstore-status",
        # One app's App Store version against what is cached: `app`,
        # `bundle_id`, `store_version`, `cached_version`, `update_available`,
        # `held_version`, `error`. One per app, always — a lookup that failed
        # is this event with `store_version: null` and the reason in `error`,
        # never silence, so the GUI can tell "not asked" from "couldn't tell".
        # `held_version` is the version the app is held at (its
        # `preferred_version` pin), or null; a held app's `update_available`
        # is always false (`appstore.held_version`).
        "appstore-version",
        # `--appstore-versions`, one per version the App Store serves this
        # Apple ID for the app, newest first: `app`, `bundle_id`,
        # `external_id` (the id `--appstore-get --version-id` takes), `index`
        # (0 is the newest), `newest`, `version` and `release_date` (RFC 3339)
        # once read, else null, `staged` (that version's IPA is in the cache
        # under the name a get gives it) and `error` (why Apple wouldn't
        # describe this version, or null). Only `--limit` versions from
        # `--offset` on are read; what was read before is remembered, so other
        # rows may have theirs too.
        "appstore-release",
        # `--appstore-versions`' sentinel, after the last `appstore-release`,
        # even when there are none: `app`, `bundle_id`, `count`, `offset`,
        # `limit`, `held_version` (as on `appstore-version`).
        "appstore-releases-end",
        # A get changed the app's hold, after it staged (or found staged)
        # what it was asked for: `app`, `bundle_id`, `version` (the version
        # it is now held at, or null when a get of the latest let go) and
        # `previous` (the one before, or null). Only when it changed. The
        # hold is the app's `preferred_version`, so the GUI reads the app
        # list again. When the preferred version was changed while the get
        # ran (Edit App), the get leaves it and warns (`log`) instead.
        "appstore-hold",
        # `--appstore-get` (and an encrypted `--provide-ipa`) moving on to
        # `stage`: "download", "decrypt" or "stage". Its own event rather than
        # a `stage` name: STAGE_NAMES is the *build's* vocabulary and is held
        # equal to what a build emits.
        "appstore-stage",
        # What UnfairPlay reported: `app`, `encrypted_images`,
        # `decrypted_pages`, `plaintext_verified`. Emitted only for a decrypt
        # that was accepted — verified, or `--allow-unverified`.
        "appstore-decrypted",
        # `--appstore-get` stopped before downloading, because the App Store's
        # current version is already staged as a get stages it
        # (`appstore.already_staged`): `app`, `bundle_id`, `version`,
        # `filename` and `path` of that staged IPA. The whole output of such a
        # get, which exits 0: no `appstore-stage`, no `ipa-provided`, nothing
        # downloaded or changed. `--again` downloads anyway. A lookup that
        # failed or named no version never ends a get here: it downloads.
        "appstore-already-staged",
        # A get or an encrypted drop the CLI refused to stage, and why, as
        # data (C-UNVERIFIED): `app` and `reason`. Emitted just before the
        # refusal is raised, so the exit status and stderr are what they
        # always were. Reasons:
        # - `"unverified"` (UnfairPlay decrypted it but `PlaintextVerified`
        #   was false), which the GUI answers with "Use it anyway…" and a
        #   re-run under `--allow-unverified`;
        # - `"needs-license"` (the Apple ID doesn't own it) and `"paid"`
        #   (`--appstore-claim` on an app that isn't free), both carrying
        #   `price`, `formatted_price` and `track_id` from the iTunes lookup
        #   (null when it failed), which the GUI answers with Get (free) or
        #   Open in App Store;
        # - `"unverified-helper"` (`--appstore-enroll-command` without
        #   `--allow-unverified-helper`, when root couldn't check the
        #   UnfairPlay it runs), carrying `kind` ("ad-hoc": Graft.app's
        #   signature names no team; "not-bundled": this CLI isn't Graft.app's
        #   own), `helper` (the UnfairPlay root would copy and run) and
        #   `message` (why, as a sentence), which the GUI answers with a
        #   confirmation and a re-run with the flag.
        # Other App Store failures emit nothing here: the app has nothing to
        # offer for them beyond the message. A failed read of Graft.app's own
        # team is one of those: an error, never an unverified script.
        "appstore-refused",
        # --- App Store account, in the app (sub-project 4) ---
        #
        # `--appstore-login`, one event per step. `state` is "preparing"
        # (ipatool said the first sign-in can take minutes), "waiting"
        # (`elapsed`, whole seconds, while ipatool is silent), "needs-code"
        # (send the 2FA code on stdin now), "verifying", "signed-in" (`email`)
        # or "failed" (`reason`, one of appstore_login.REASON_*, `message`, a
        # plain sentence, and `hint`, what to do next as a string, or null
        # when there is nothing to add: "keychain-clear" says to unlock the
        # login keychain, "bad-version" where a good ipatool goes;
        # "unknown-prompt" when ipatool asked
        # something none of 2.8.0's prompts, "ipatool-changed" when
        # `--expect-ipatool PATH` named another ipatool, before stdin is
        # read). Never the password or the code: the
        # CLI holds them only in wipeable buffers and puts them nowhere but
        # ipatool's prompt.
        "appstore-login",
        # `--appstore-logout` finished: `signed_out` (always true when emitted).
        "appstore-logout",
        # One installed app UnfairPlay's `setup --app` can enroll this Mac
        # with: `path`, `name`, `bundle_id`, `opened` (it has been launched
        # once, which UnfairPlay's README asks for).
        "appstore-enroll-candidate",
        # The candidate listing's sentinel: `count`. Zero is an answer ("install
        # a free iPhone app from the Mac App Store first"), not silence.
        "appstore-enroll-candidates-end",
        # `--appstore-enroll-command`: `app` (resolved), `model_dir` (the
        # user's own model folder, which the root script never names),
        # `script` (the `/bin/sh` script the app runs as root behind the macOS
        # administrator prompt; its stdout is the work folder root made),
        # `helper` (the UnfairPlay it copies and runs), `verified` (whether
        # that script checks its copy against Graft.app's team signature),
        # and, when it doesn't, `unverified_kind` and `unverified_reason`
        # (as `appstore-refused`'s `kind` and `message`; null when verified),
        # `cancel_file` (a path that doesn't exist yet: the app creates it,
        # O_CREAT|O_EXCL 0600, to stop the run before or during it, and
        # removes it afterward; the script exits 67) and
        # `time_limit_seconds` (root's whole run is stopped after that long;
        # exit 68; 900, or more for an app over 6 GB). An unverified script is emitted only under
        # `--allow-unverified-helper`. The CLI is the only place that builds
        # it (`enrollment.root_script`).
        "appstore-enroll-command",
        # `--appstore-enroll-finish DIR` moved the model root's UnfairPlay made
        # in DIR into the user's model folder, as the user: `model_dir`,
        # `count` (files moved).
        "appstore-enrolled",
        # `--appstore-claim` got the free license: `app`, `bundle_id`,
        # `already_owned`.
        "appstore-claimed",
        # --- App Store search (`appstore_search.py`, `--appstore-search`) ---
        #
        # One app the iTunes Search API found, in the store's order. Fields:
        # - `track_id`, `bundle_id`, `name`, `developer`, `version`;
        # - `price`: a number, 0 for free, or null when the listing gives none;
        # - `formatted_price`: the store's own spelling ("Free", "$4.99"), or null;
        # - `currency`;
        # - `artwork_url`: the listing's largest icon, or null;
        # - `icon_path`: a small copy of the icon that the CLI downloaded and
        #   cached for the app to draw, since the app makes no connections of
        #   its own. Null when the listing has none or it couldn't be fetched;
        # - `configured_as`: the config.yaml app already using this bundle id,
        #   or null.
        "appstore-search-result",
        # The sentinel, last on success: `term`, `country` (the store asked,
        # upper-case), `count`, and `note`: how the store was chosen when
        # there is something to say (a region with no store: "Searching the
        # US store: your region has no App Store."), else null. A search that
        # found nothing is this event alone, so "no results" never reads as
        # "the search never ran".
        "appstore-search-end",
        # A search that got no usable answer, emitted just before the non-zero
        # exit (13, ITunesError; 2 for "invalid-term"), so the status and
        # stderr are what they always are. `term`, `note` (as on the
        # sentinel) and `reason`:
        # - "offline": itunes.apple.com couldn't be reached;
        # - "rate-limited": Apple answered 403, 429 or 503, its per-IP limit;
        # - "http": it answered with another error status;
        # - "malformed": it answered with something that isn't a result list;
        # - "invalid-term": the term isn't text (invalid UTF-8), so nothing
        #   was asked.
        # The app shows its own state for the first two
        # (`CLIContract.AppStoreSearchFailure`).
        "appstore-search-failed",
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
        # `app`, `ok`, `stopped`, `elapsed_seconds`, and `error` when it
        # failed. An `--export-ipa`'s also carries `reason`: null, or why the
        # export failed, one of `cli.EXPORT_FAILURE_REASONS` ("exists",
        # "bad-destination", "unwritable", "no-space", "write-failed",
        # "no-ipa"), for an agent that must ask before `--overwrite`.
        "build-end",
        "build-pruned",
        "build-record",
        # `app`, `bundle_id` and `delivery`, the app's delivery mode. An
        # `--export-ipa` delivers nothing, so its `delivery` is null and it
        # also carries `export_to`, the path it will write.
        "build-start",
        "cached-ipa",
        "canister-fetch-end",
        "canister-fetch-start",
        "canister-result",
        "canister-search-end",
        "cli-contract",
        # --- What Graft keeps outside Graft.app (`remove_data`) ---
        #
        # `--remove-data`'s listing: one `data-item` per place, in order,
        # every one whether anything is there or not:
        # - `id` (what `--items` takes), `group` and `group_title` (the
        #   heading it is listed under), `title`, `detail` (what it is, in a
        #   sentence or two), `note` (a caution, such as another program
        #   sharing it) and `how` (how to remove it by hand, for one this
        #   command doesn't remove), each null when there is none;
        # - `paths` (strings) and `keychain` ([{service, account}]): where it
        #   is. Never a secret;
        # - `size_bytes`: the files' total, or null (keychain items, or not
        #   checked); `present`: true, false, or null when the keychain was
        #   locked or didn't answer, so it couldn't be checked;
        # - `default`: chosen unless unchecked, and what `--items all` takes
        #   (false for the passwords); `removable`: false for what the user
        #   chose (group "kept") and for Graft.app's own settings
        #   (`app-settings`), which the app removes itself.
        "data-item",
        # The listing's sentinel: `count`, `total_bytes` (of the removable
        # items that are there) and `keychain`: "checked", "locked" or
        # "no-answer" (then nothing more was asked of it).
        "data-items-end",
        # `--remove-data --items ...`: one per item named, after it was
        # removed (or, under --dry-run, would be): `id`, `ok`, `dry_run`,
        # `removed` (each path, and each keychain item as "<service>
        # <account>", that went), `failed` ([{what, reason}] for each still
        # there) and `freed_bytes`.
        "data-removed",
        # The removal's sentinel: `ok`, `dry_run`, `removed` and `failed`
        # (item ids) and `freed_bytes`. Exit 1 when any failed.
        "data-removal-end",
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
        # `--init --account free|paid` (`account_setup`), the setup the Mac app
        # runs. One `init-step` per thing done, in order: `step` ("profile",
        # "certificate", "keychain", "files" or "config") and `detail`, a
        # sentence the app shows as it is ("would ..." under --dry-run). Then
        # exactly one `init-result`:
        #   - `account`, `config` (the path), `change` ("created", "upgraded",
        #     "switched", "unchanged" or "replaced"), `dry_run` and `summary`
        #     (a sentence);
        #   - for a paid account, `team_id`, `profile_name`, `certificate` (the
        #     .p12's common name), `device_count`, `devices` (the profile's
        #     UDIDs) and `expires` (ISO 8601, UTC); null or empty on free;
        #   - `pinned` (apps an upgrade gave `delivery: livecontainer`),
        #     `sideloads` (apps still `delivery: sideload` after a switch to
        #     free) and `steps` (a free Apple ID's steps on the device,
        #     `config.FREE_DEVICE_STEPS`);
        #   - `previous_team_id`: the team `signing:` named before a replace
        #     (`--replace`), null for every other change.
        # A refusal exits non-zero with its reason on stderr and no
        # `init-result`. The password is never in either event.
        "init-result",
        "init-step",
        # `--export-ipa` wrote the tweak-injected IPA, unsigned, where it was
        # told: `app`, `version` (the staged IPA's), `path` (absolute),
        # `filename`, `size_bytes`, `tweaks` (the app's, as config.yaml lists
        # them), `replaced` (a file was there and `--overwrite` replaced it)
        # and `dry_run`. Emitted once the file is in place, as the export's
        # last event before `build-end`; under `--dry-run` it says where the
        # file would go, with `size_bytes` null, and nothing is written.
        "ipa-exported",
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
        # One `--check` row: `name`, `ok`, `detail`, `install`, and `hint` /
        # `action_key` when there is one. `install` is on every row: the exact
        # command that installs what a failing tool row found missing
        # ("brew install zsign"), or null — for a passing row, a row that is
        # not a tool, or a builtin. The hint still says "install with: <it>"
        # for a reader that predates the field. `info: true` marks a row that
        # reports a fact rather than passing a test (a free Apple ID's
        # account row); it is always `ok`. A tool row that was found carries
        # `source`: where `tools.resolve` found it ("bundled", "PATH", ...).
        "preflight-check",
        # `ok_count`, `fail_count`, and `account` (`"free"` or `"paid"`),
        # so the mode is known even for a config with no apps.
        "preflight-end",
        # `--set-portal`'s answer (`setup_portal.set_portal`): `key_id`,
        # `issuer_id`, `key_file` (where the .p8 was copied) and `devices`, how
        # many iOS devices App Store Connect listed when the key was checked.
        # Never anything from inside the key. A key that fails the check is a
        # non-zero exit with nothing written, and no event.
        "portal-set",
        # `--clear-portal`'s: `removed`, whether there was a key to remove, and
        # `key_file`, the .p8 it named (left on disk), or null.
        "portal-cleared",
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
        # `--settings`' answer, and the echo after `--set-settings`,
        # `--set-portal` and `--clear-portal`: the `SETTINGS` keys, `paths`
        # (on `--settings` only), `parse_error`, `paths_are_defaults`, and
        # `app_store_connect` — null, or {key_id, issuer_id, key_file,
        # key_file_exists}; never the key itself — and `graft_version`, this
        # CLI's own version, which the pane shows so the app need not spawn
        # `--version` for it.
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
        # `--check-update`'s answer, exactly once, worked or not (the same
        # keys either way): `ok`; `current`, the version compared against;
        # `latest`, the latest published release's version without its "v",
        # and `url`, its page (both null on a failure); `update_available`;
        # and on a failure `reason` (offline|http|malformed), `status` (the
        # HTTP status, or null) and `message`, the sentence Help › Check for
        # Updates… shows. `release_check.event` builds it.
        "update-check",
        # `--download-update`'s progress, the same keys every time: `step`
        # ("download" or "verify"), `version` (the release's), `received` and
        # `total` (bytes, while downloading; null while verifying) and `check`
        # (while verifying: archive|bundle|architecture|signature|requirement|notarization,
        # `app_update.CHECKS`; null while downloading).
        "update-progress",
        # `--download-update`'s answer, exactly once, worked or not (the same
        # keys either way): `ok`; `current`, the version compared against;
        # `version` and `app_path`, the verified Graft.app waiting in the
        # cache (both null on a failure); `url`, the release's page, which the
        # app links when it can't install; and on a failure `reason`
        # (`app_update`'s names) and `message`. `app_update.download_event`
        # builds it.
        "update-download-end",
        # `--install-update`'s answer, exactly once: `ok`; `version` and
        # `log` (the helper's log file) once the helper is started, which
        # waits for Graft to quit; `relaunch`; and on a failure, when nothing
        # was started, `url`, `reason` and `message`.
        # `app_update.install_event` builds it.
        "update-install",
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
# not a license to ignore one. `CLIContract.cliOnlyEvents` declares the same set
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
# hiding inside the first. `EVENT_TYPES` proves the GUI will recognize the
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
        # Stage 4 of `--export-ipa`, its last — write cyan's output, unsigned,
        # where the user chose. An export has no sign, archive or delivery stage.
        "export",
        # Stage 5 (sideload delivery) — install over usbmux (`device_install`).
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
    # Stored at the app's top level, not under `cyan:`. `lc_bundle_id` is the
    # app's LiveContainer pin, set by `--lc-bundle-id` and cleared by
    # `--no-lc-bundle-id`; either one reports it changed. `auto_profile` is
    # `--auto-profile` / `--no-auto-profile` (C-AUTOPROFILE). `store_country`
    # is `--store-country`: the App Store the app's lookups ask.
    {"preferred_version", "delivery", "lc_bundle_id", "auto_profile", "store_country"}
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
