import Foundation

struct AppRecord: Decodable, Identifiable, Hashable {
    let name: String
    let bundleId: String
    let preferredVersion: String
    let tweaks: [String]
    let displayName: String?
    let delivery: DeliveryMode
    let autoProfile: Bool
    // Cyan options. Required, like every other field here — see init(from:).
    let keepBundleId: Bool
    let stripExtensions: Bool
    let stripSupportedDevices: Bool
    let compressionLevel: Int
    /// The config's account, repeated on every app. `ConfigStore.account`
    /// reads `--list`'s own `account` event first; this is what it falls back
    /// on for a CLI that predates that event and still has apps to describe.
    /// Optional, unlike the rest: it is config-wide rather than this app's, a
    /// CLI from before free mode never sends it, and an app must not stop
    /// decoding over a field that is not about the app.
    let account: AccountMode?
    /// The LiveContainer this app is pinned to (`lc_bundle_id:`), or nil when
    /// it is not: the CLI then picks one per push (`lc_discovery.choose`).
    let lcBundleId: String?
    /// Whether the CLI reported the pin at all (C-LCPIN). Separate from
    /// `lcBundleId == nil`, which on its own cannot tell "unpinned" from "a
    /// CLI too old to say" — and the second also has no `--lc-bundle-id` to
    /// send, so the Edit sheet offers the picker only when this is true.
    let reportsLCPin: Bool

    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name, tweaks, delivery, account
        case lcBundleId = "lc_bundle_id"
        case bundleId = "bundle_id"
        case preferredVersion = "preferred_version"
        case displayName = "display_name"
        case autoProfile = "auto_profile"
        case keepBundleId = "keep_bundle_id"
        case stripExtensions = "strip_extensions"
        case stripSupportedDevices = "strip_supported_devices"
        case compressionLevel = "compression_level"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        bundleId = try c.decode(String.self, forKey: .bundleId)
        // Every remaining field is required. AppRecord is no longer
        // display-only: EditAppSheet seeds its state from it, diffs against it
        // in hasChanges, and writes the diff back. A defaulted value would be
        // indistinguishable from a real one and could get written back as a
        // lie — a missing `delivery` silently "becoming" sideload, or a
        // missing `compression_level` showing 6 next to a config that says 9,
        // so that nudging the stepper to 7 writes 9 → 7. That applies to the
        // four cyan keys exactly as it does to the other four; the CLI's
        // --list handler emits all eight unconditionally, so an absent key
        // means a real mismatch, and failing the decode surfaces it instead of
        // quietly seeding the editor with a fabricated baseline.
        //
        // displayName is the one true optional: `null` is a meaningful value
        // (no custom name), not a missing key.
        preferredVersion = try c.decode(String.self, forKey: .preferredVersion)
        tweaks = try c.decode([String].self, forKey: .tweaks)
        displayName = try c.decodeIfPresent(String.self, forKey: .displayName)
        delivery = try c.decode(DeliveryMode.self, forKey: .delivery)
        autoProfile = try c.decode(Bool.self, forKey: .autoProfile)
        keepBundleId = try c.decode(Bool.self, forKey: .keepBundleId)
        stripExtensions = try c.decode(Bool.self, forKey: .stripExtensions)
        stripSupportedDevices = try c.decode(Bool.self, forKey: .stripSupportedDevices)
        compressionLevel = try c.decode(Int.self, forKey: .compressionLevel)
        // `try?`: an account spelling this app does not know reads as "not
        // known", which disables nothing, rather than dropping the app.
        account = (try? c.decodeIfPresent(AccountMode.self, forKey: .account)) ?? nil
        // Optional like `account`, and for the same reason: absent from every
        // CLI before C-LCPIN, and not worth dropping the app over.
        reportsLCPin = c.contains(.lcBundleId)
        lcBundleId = (try? c.decodeIfPresent(String.self, forKey: .lcBundleId)) ?? nil
    }
}

struct AppAddedRecord: Decodable, Hashable {
    let name: String
    let bundleId: String
    let displayName: String
    let delivery: DeliveryMode

    enum CodingKeys: String, CodingKey {
        case name, delivery
        case bundleId = "bundle_id"
        case displayName = "display_name"
    }
}

struct AppRemovedRecord: Decodable, Hashable {
    let name: String
    let bundleId: String

    enum CodingKeys: String, CodingKey {
        case name
        case bundleId = "bundle_id"
    }
}

struct AppUpdatedRecord: Decodable, Hashable {
    let name: String
    let changed: [String]
}

struct CachedIPARecord: Decodable, Hashable {
    /// True if the build pipeline would have a file to use. When false, all
    /// other fields are empty/zero — the CLI omits them in the "missing" event.
    let exists: Bool
    let app: String
    let bundleId: String
    let version: String
    let filename: String
    let path: String
    let sizeBytes: Int

    enum CodingKeys: String, CodingKey {
        case exists, app, version, filename, path
        case bundleId = "bundle_id"
        case sizeBytes = "size_bytes"
    }
}

/// One IPA sitting in the cache, as its own Info.plist describes it.
///
/// The cache half of the device pane's join: the phone says what is installed,
/// this says what is staged, and a row with both can be added and built with
/// no network. `CachedIPARecord` cannot serve this — it answers for a *config
/// app name*, and every app this is asked about is one that is not in the
/// config yet.
struct StagedIPARecord: Decodable, Hashable {
    let bundleId: String
    let version: String
    /// From the IPA's own plist, so the prefilled sheet reads "ExamplePlayer"
    /// rather than a slug — and so the decrypt card, which has no device
    /// listing behind it, can prefill at all.
    let displayName: String
    let filename: String
    let path: String
    let sizeBytes: Int
    /// Whether this is the file a build would reach for. Decided by the CLI,
    /// which owns `find_in_cache`'s version rule; two versions of one app can
    /// both be staged.
    let preferred: Bool
    /// Bundle paths a dump removed to make this IPA verify.
    let stripped: [String]
    /// The plain-language capabilities that went with them.
    ///
    /// **nil is not empty.** `[]` means something looked and found no
    /// capability worth naming — a widget that goes missing is visibly
    /// missing. `nil` covers three things this side cannot tell apart on its
    /// own: no sidecar at all (a complete dump), one that will not parse, and
    /// one that predates this field. `stripped` is the tell — empty alongside
    /// a nil `lost` means the first two, non-empty alongside a nil `lost`
    /// means the sidecar is just old. Only the `[]` case may ever carry a
    /// reassurance to the user, so it is kept apart from the wire all the way
    /// to the copy.
    let lost: [String]?
    /// Removed bundle path → `"essential"` / `"major"` / `"cosmetic"`.
    ///
    /// Empty when unrecorded — and, on this side only, also when the key is
    /// renamed away: `decodeIfPresent` returns nil for an absent key or an
    /// explicit null, so `?? [:]` swallows those two indistinguishably. A
    /// present value of the *wrong type* throws instead and takes the whole
    /// record down, which is the louder and better outcome. Either way, empty
    /// must be read the way `StrippedNote`'s docstring on the Python side says:
    /// "nobody recorded one," never "nothing essential was lost." A reader
    /// joining this against `stripped` hunting an `"essential"` match has to
    /// fall through to the same neutral line for an empty map, not treat it
    /// as cleared.
    let importance: [String: String]

    enum CodingKeys: String, CodingKey {
        case version, filename, path, preferred, stripped, lost, importance
        case bundleId = "bundle_id"
        case displayName = "display_name"
        case sizeBytes = "size_bytes"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        bundleId = try c.decode(String.self, forKey: .bundleId)
        version = try c.decode(String.self, forKey: .version)
        displayName = try c.decode(String.self, forKey: .displayName)
        filename = try c.decode(String.self, forKey: .filename)
        path = try c.decode(String.self, forKey: .path)
        sizeBytes = try c.decode(Int.self, forKey: .sizeBytes)
        preferred = try c.decode(Bool.self, forKey: .preferred)
        stripped = try c.decodeIfPresent([String].self, forKey: .stripped) ?? []
        lost = try c.decodeIfPresent([String].self, forKey: .lost)
        importance = try c.decodeIfPresent([String: String].self, forKey: .importance) ?? [:]
    }
}

/// One staged IPA removed from the cache.
///
/// `alsoRemoved` names the `.stripped.json` sidecar when there was one — the
/// deletion is two files and the event says so, rather than leaving the caller
/// to infer that a sidecar went with it.
struct StagedIPADeletedRecord: Decodable, Hashable {
    let filename: String
    let alsoRemoved: [String]

    enum CodingKeys: String, CodingKey {
        case filename
        case alsoRemoved = "also_removed"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        filename = try c.decode(String.self, forKey: .filename)
        alsoRemoved = try c.decodeIfPresent([String].self, forKey: .alsoRemoved) ?? []
    }
}

/// `--delete-staged` refused: `filename` is the last staged IPA some
/// configured app can build from, and `usedBy` names which — the same shape
/// `TweakInUseRecord.usedBy` carries, and for the same reason: recovering
/// this list by scraping the "is the only staged IPA for: ..." sentence out
/// of stderr was `--delete-tweak`'s own shipped defect before it had this
/// event. Emitted just before the CLI's refusal is raised.
struct StagedIPAInUseRecord: Decodable, Hashable {
    let filename: String
    let usedBy: [String]

    enum CodingKeys: String, CodingKey {
        case filename
        case usedBy = "used_by"
    }
}

/// One staged IPA handed to PlayCover for installation on this Mac.
///
/// Emitted when `open -b io.playcover.PlayCover` succeeds — which means the
/// file was handed over, **not** that PlayCover finished installing it. `open`
/// returns as soon as delivery happens, so there is no completion status to
/// carry here; PlayCover reports its own progress.
///
/// Named `handed-to-playcover` on the wire, not `installed-to-mac` — the verb
/// that triggers this (`--install-to-mac`) describes the user's intent, which
/// is fine for a command, but that same word as an *event* name would assert
/// a finished install nothing here can observe. This app renders off the
/// event name, not off this comment.
struct HandedToPlayCoverRecord: Decodable, Hashable {
    let filename: String
    let path: String
    let bundleId: String

    enum CodingKeys: String, CodingKey {
        case filename, path
        case bundleId = "bundle_id"
    }
}

/// What is published for one library file, against what it holds.
///
/// Emitted for *every* file, including ones nothing publishes — anything built
/// from source is in no index at all. "We looked and found nothing" and "we
/// never looked" have to be distinguishable from here, which is why `latest`
/// being nil is a real answer rather than a missing event.
struct TweakUpdateRecord: Decodable, Hashable {
    /// The library row this answers for. Keyed by filename, not package id:
    /// two files can carry the same package (`1.1.8` and `1.1.8-jailed` sit in
    /// this library today) and either may be the one attached to an app.
    let filename: String
    let packageId: String?
    let installed: String?
    let latest: String?
    /// A repo URL, or `canister`. Shown to the user, so it names somewhere
    /// they could go and look rather than just asserting an update exists.
    let source: String?
    /// The CLI's verdict, not something to re-derive here. Debian version
    /// comparison is `repo.version_key`'s job — `1.10` after `1.9`, `1.0~beta`
    /// before `1.0` — and a second implementation in Swift would be a second
    /// thing to be wrong.
    let updateAvailable: Bool
    /// The lookup could not be made at all — offline, or the service refused
    /// — which is not "nothing newer" (that is `latest` nil with this false).
    /// The summary already counts these (`TweakUpdatesEndRecord.unchecked`);
    /// this says *which* rows. False when absent, from a CLI too old to say.
    var unchecked: Bool = false

    enum CodingKeys: String, CodingKey {
        case filename, installed, latest, source, unchecked
        case packageId = "package_id"
        case updateAvailable = "update_available"
    }
}

extension TweakUpdateRecord {
    /// Written out for `unchecked` alone: a synthesised decoder reads a
    /// non-optional `Bool` with `decode`, default value or not, so a CLI that
    /// does not send it would fail the whole row. In an extension so the
    /// memberwise initialiser survives.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        filename = try c.decode(String.self, forKey: .filename)
        packageId = try c.decodeIfPresent(String.self, forKey: .packageId)
        installed = try c.decodeIfPresent(String.self, forKey: .installed)
        latest = try c.decodeIfPresent(String.self, forKey: .latest)
        source = try c.decodeIfPresent(String.self, forKey: .source)
        updateAvailable = try c.decode(Bool.self, forKey: .updateAvailable)
        unchecked = try c.decodeIfPresent(Bool.self, forKey: .unchecked) ?? false
    }
}

struct TweakUpdatesEndRecord: Decodable, Hashable {
    let checked: Int
    let outdated: Int
    /// How many could not be looked up at all — offline, or the service
    /// refused. Never folded into "up to date": that is the one claim this
    /// feature must not make without evidence.
    let unchecked: Int
}

/// The result of actually performing one update.
///
/// `updated: false` is a real answer, not a failure: "there is nothing newer"
/// is what a caller acting on a badge from ten minutes ago deserves to be told
/// plainly rather than as an error.
struct TweakUpdatedRecord: Decodable, Hashable {
    let filename: String
    let newFilename: String?
    let updated: Bool
    let reason: String?
    let installed: String?
    let latest: String?
    /// The apps re-pointed from the old file to the new one. Empty for an
    /// orphan, and empty when the download replaced the file in place.
    let apps: [String]?
    /// The repo served the same filename, so every app already points at it
    /// and nothing needed re-attaching.
    let replacedInPlace: Bool?
    /// Where the previous build was kept, so a bad update can be undone.
    let rollback: String?
    /// Set when the swap got partway: the download landed and `apps` names the
    /// ones actually re-pointed. Not a failed update — a partial one.
    let error: String?

    enum CodingKeys: String, CodingKey {
        case filename, updated, reason, installed, latest, apps, rollback, error
        case newFilename = "new_filename"
        case replacedInPlace = "replaced_in_place"
    }
}

/// One app installed on a connected device — a candidate to decrypt.
///
/// `encrypted` is the whole point of listing them: a FairPlay-encrypted App
/// Store app is what a dump tool (bagbak, frida-ios-dump) attaches to, while a
/// sideload is already decrypted and dumping it does nothing. Derived on the
/// CLI side from `SignerIdentity`, verified against a real device.
struct DeviceAppRecord: Decodable, Hashable, Identifiable {
    let bundleId: String
    let name: String
    let version: String
    let encrypted: Bool
    /// Whether this app is on screen right now.
    ///
    /// Decoded with a `false` default rather than required, so a `graft`
    /// older than this field lists apps normally instead of failing to decode
    /// the whole row — the same treatment every other added field gets here.
    ///
    /// It decides which backend can dump the app: frida-ios-dump attaches, and
    /// only the frontmost app is reliably attachable (iOS freezes a
    /// backgrounded app, which keeps its pid and then times out the attach).
    let frontmost: Bool
    /// Where the app's real icon lives on disk, when `--with-icons` asked the
    /// CLI to resolve one. Absent, not null, when there is none — a
    /// LiveContainer-delivered app is not a top-level install, so SpringBoard
    /// has never had an icon for it to hand back.
    let iconPath: String?

    var id: String { bundleId }

    enum CodingKeys: String, CodingKey {
        case name, version, encrypted, frontmost
        case bundleId = "bundle_id"
        case iconPath = "icon_path"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        bundleId = try c.decode(String.self, forKey: .bundleId)
        name = try c.decode(String.self, forKey: .name)
        version = try c.decode(String.self, forKey: .version)
        encrypted = try c.decode(Bool.self, forKey: .encrypted)
        frontmost = try c.decodeIfPresent(Bool.self, forKey: .frontmost) ?? false
        iconPath = try c.decodeIfPresent(String.self, forKey: .iconPath)
    }

    init(
        bundleId: String, name: String, version: String, encrypted: Bool,
        frontmost: Bool = false, iconPath: String? = nil
    ) {
        self.bundleId = bundleId
        self.name = name
        self.version = version
        self.encrypted = encrypted
        self.frontmost = frontmost
        self.iconPath = iconPath
    }
}

struct DeviceAppsEndRecord: Decodable, Hashable {
    let count: Int
    let encrypted: Int
}

/// A finished decryption dump: where the decrypted IPA landed.
///
/// Emitted once bagbak has dumped the app *and* the CLI has verified every
/// binary is cryptid 0 — so a `decrypt-end` is a promise the staged IPA will
/// pass the build's own stage 2.5, not just that a file was written.
/// Whether the device can dump an app right now, and what is blocking it.
///
/// The answer to "why is decrypting so hit-or-miss": a dump has several
/// preconditions — jailbreak, SSH, an app actually in the foreground — and each
/// used to be discovered by starting one and waiting for a timeout that named
/// none of them. This is that list, checked up front.
struct DecryptCheckRecord: Decodable, Hashable {
    struct Check: Decodable, Hashable, Identifiable {
        let key: String
        let label: String
        let ok: Bool
        let detail: String
        /// What to do about it. nil when `ok`.
        let fix: String?
        /// Whether failing this stops *every* dump, or only one route to one.
        ///
        /// The CLI decides it — "App on screen" is non-blocking whenever a
        /// backend can still launch the app itself — and dropping it here made
        /// the pane draw an advisory line as a hard red failure, which is the
        /// misreading commit 79f6251 set out to remove. Defaults to true so an
        /// older CLI's checks stay conservative rather than silently softening.
        let blocking: Bool
        /// Which failure this is, as a stable word, for a check whose
        /// failures need different responses — today the `ssh` check's
        /// `CLIContract.DecryptCheck.SSHReason`. Nil when `ok`, for every
        /// other check, and from a CLI older than the field.
        let reason: String?

        var id: String { key }

        enum CodingKeys: String, CodingKey {
            case key, label, ok, detail, fix, blocking, reason
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            key = try c.decode(String.self, forKey: .key)
            label = try c.decode(String.self, forKey: .label)
            ok = try c.decode(Bool.self, forKey: .ok)
            detail = try c.decode(String.self, forKey: .detail)
            fix = try c.decodeIfPresent(String.self, forKey: .fix)
            blocking = try c.decodeIfPresent(Bool.self, forKey: .blocking) ?? true
            // `try?`: an unreadable reason costs the distinction, never the check.
            reason = (try? c.decodeIfPresent(String.self, forKey: .reason)) ?? nil
        }
    }

    struct Backend: Decodable, Hashable, Identifiable {
        let name: String
        let usable: Bool
        /// Why it cannot run. nil when `usable`.
        let reason: String?
        /// True and worth knowing, but not a reason it cannot run — kept apart
        /// so a suspicion is never rendered as a verdict.
        let note: String?

        var id: String { name }
    }

    let device: String?
    let deviceName: String?
    let ready: Bool
    /// Whether *any* app could be dumped, foreground or not — both backends
    /// launch the app themselves, so `ready` (the on-screen route) being false
    /// does not mean decryption is unavailable.
    ///
    /// Absent from a CLI older than this field, and absence must not fail the
    /// whole event: the GUI ships separately from the pipx CLI, so a decoding
    /// error here would blank the readiness card — losing the checks that *did*
    /// arrive — over one missing key. False is the conservative reading: the
    /// CLI never said the list route works, and offering a dump that then fails
    /// is the exact thing this card exists to prevent.
    let canDumpFromList: Bool
    let recommendedBackend: String?
    let frontmostBundleId: String?
    let frontmostName: String?
    let checks: [Check]
    let backends: [Backend]

    enum CodingKeys: String, CodingKey {
        case device, ready, checks, backends
        case deviceName = "device_name"
        case canDumpFromList = "can_dump_from_list"
        case recommendedBackend = "recommended_backend"
        case frontmostBundleId = "frontmost_bundle_id"
        case frontmostName = "frontmost_name"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        device = try c.decodeIfPresent(String.self, forKey: .device)
        deviceName = try c.decodeIfPresent(String.self, forKey: .deviceName)
        ready = try c.decode(Bool.self, forKey: .ready)
        canDumpFromList = try c.decodeIfPresent(Bool.self, forKey: .canDumpFromList) ?? false
        recommendedBackend = try c.decodeIfPresent(String.self, forKey: .recommendedBackend)
        frontmostBundleId = try c.decodeIfPresent(String.self, forKey: .frontmostBundleId)
        frontmostName = try c.decodeIfPresent(String.self, forKey: .frontmostName)
        checks = try c.decode([Check].self, forKey: .checks)
        backends = try c.decode([Backend].self, forKey: .backends)
    }
}

struct DecryptEndRecord: Decodable, Hashable {
    let bundleId: String
    let version: String
    let path: String
    /// How many Mach-O binaries were verified decrypted.
    let binaries: Int
    /// App extensions dropped to make the dump verify, as bundle paths.
    ///
    /// Dropping them is automatic — no tool can decrypt an extension built for
    /// a newer iOS than the phone runs, so the choice is a partial app or none.
    /// That makes showing it mandatory rather than nice: the CLI says so on
    /// stderr, but a `--json` run returns before that line, so the pane was the
    /// one front-end where "decrypted" could quietly mean "minus your widgets".
    /// Empty from a CLI that predates the field, which is the safe reading —
    /// that CLI never stripped anything without being asked.
    let stripped: [String]

    enum CodingKeys: String, CodingKey {
        case version, path, binaries, stripped
        case bundleId = "bundle_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        bundleId = try c.decode(String.self, forKey: .bundleId)
        version = try c.decode(String.self, forKey: .version)
        path = try c.decode(String.self, forKey: .path)
        binaries = try c.decode(Int.self, forKey: .binaries)
        stripped = try c.decodeIfPresent([String].self, forKey: .stripped) ?? []
    }
}

struct TweakMutationRecord: Decodable, Hashable {
    let app: String
    let filename: String
    let tweaks: [String]
}

/// The global settings the pane can edit, as the CLI currently has them.
///
/// The same five keys `--set-settings` writes, and only those. A pane that
/// shows a field it cannot edit is what this feature exists to remove; one
/// that edits a field it cannot show is worse, because the user types blind
/// over a value they never saw.
///
/// Every field is optional because `read_settings` is *deliberately* lenient:
/// it bypasses config validation so a half-filled `signing:` block reports the
/// fields it has and null for the ones it doesn't — which is the exact state
/// this editor exists to repair. Declaring them non-optional made that
/// leniency unreachable: a config missing `signing.team_id` emitted
/// `"signing_team_id":null`, the line failed to decode, `buildEvents()`'s
/// `try?` dropped it, and the pane rendered "Couldn't read your settings." for
/// the one config it was written to fix. A two-sided contract, widened on one
/// side only. `archiveKeep` is null for a value `_archive_keep` rejects, which
/// is not the same as absent — see `SettingsDraft`.
struct SettingsRecord: Decodable, Hashable, Equatable {
    let defaultDeviceUDID: String?
    let archiveKeep: Int?
    let signingCert: String?
    let signingProfile: String?
    let signingTeamId: String?
    /// Where this config puts everything, as the CLI resolves it. Only
    /// `--settings` reports it — `--set-settings`' echo does not — so nil means
    /// "not on this event", and `SettingsStore` keeps the last one it saw.
    var paths: SettingsPaths? = nil
    /// Why config.yaml could not be read, as the parser put it, or nil when it
    /// parsed (C-SETTINGS). Set, every setting above is null *because nothing
    /// was read*, not because nothing is set — and `--set-settings` refuses to
    /// write a file it cannot parse, so nothing here can be saved either.
    var parseError: String? = nil
    /// Whether `paths` are the defaults rather than what this config says,
    /// which is what they are when the config did not parse (C-SETTINGS).
    var pathsAreDefaults: Bool = false

    /// The contract enum itself, not a second copy of its five spellings —
    /// see `CLIContract.SettingField`.
    typealias CodingKeys = CLIContract.SettingField
}

extension SettingsRecord {
    private enum PathsKey: String, CodingKey {
        case paths
        case parseError = "parse_error"
        case pathsAreDefaults = "paths_are_defaults"
    }

    /// Written out because `paths` is not a setting: the five settings keep
    /// decoding through `CLIContract.SettingField`, and this one key is read
    /// beside them. In an extension so the memberwise initialiser survives.
    init(from decoder: Decoder) throws {
        let settings = try decoder.container(keyedBy: CLIContract.SettingField.self)
        defaultDeviceUDID = try settings.decodeIfPresent(String.self, forKey: .defaultDeviceUDID)
        archiveKeep = try settings.decodeIfPresent(Int.self, forKey: .archiveKeep)
        signingCert = try settings.decodeIfPresent(String.self, forKey: .signingCert)
        signingProfile = try settings.decodeIfPresent(String.self, forKey: .signingProfile)
        signingTeamId = try settings.decodeIfPresent(String.self, forKey: .signingTeamId)
        // `try?`: a malformed `paths` costs the Paths section, never the
        // settings editor beside it.
        let extra = try? decoder.container(keyedBy: PathsKey.self)
        paths = (try? extra?.decodeIfPresent(SettingsPaths.self, forKey: .paths)) ?? nil
        // Both optional, and both absent from every CLI before C-SETTINGS:
        // that CLI's answer to a broken file is the defaults with no word
        // about it, which is what decoding to nil/false reproduces.
        parseError = (try? extra?.decodeIfPresent(String.self, forKey: .parseError)) ?? nil
        pathsAreDefaults = (try? extra?.decodeIfPresent(Bool.self, forKey: .pathsAreDefaults)) ?? false
    }
}

/// The directories the CLI resolves for this config — `XDG_*_HOME`, and the
/// config's own `cache_dir` / `built_dir` / `state_dir` / `tweaks_dir`, all
/// applied. What Settings → Paths shows, so it names the directories a build
/// actually uses rather than the defaults it might not.
struct SettingsPaths: Decodable, Hashable {
    let configDir: String
    let cacheDir: String
    let builtDir: String
    let stateDir: String
    let tweaksDir: String

    enum CodingKeys: String, CodingKey {
        case configDir = "config_dir"
        case cacheDir = "cache_dir"
        case builtDir = "built_dir"
        case stateDir = "state_dir"
        case tweaksDir = "tweaks_dir"
    }
}

/// Which global settings a `--set-settings` call actually wrote.
///
/// `changed` is the point: the pane builds the set of fields it asked for and
/// refuses to report success for any that come back missing — the same
/// reconciliation `app-updated` gets, and for the same reason. A setting the
/// CLI silently declined is how a user ends up trusting a value that is not
/// on disk.
struct SettingsUpdatedRecord: Decodable, Hashable {
    let changed: [String]
}

/// Why `--delete-tweak` refused: the apps still holding the tweak, as data.
///
/// The GUI offers "Detach and Delete" off exactly this list and used to
/// recover it by finding "is attached to:" in stderr. That was the app's one
/// dependency on an English sentence, and the only CLI coupling with none of
/// the contract checking the rest get — rewording `cli.py`'s message would
/// have silently degraded the button while every Swift test kept passing,
/// because the test fakes carry the same sentence.
struct TweakInUseRecord: Decodable, Hashable {
    let filename: String
    let usedBy: [String]

    enum CodingKeys: String, CodingKey {
        case filename
        case usedBy = "used_by"
    }
}

struct TweakDeletedRecord: Decodable, Hashable {
    let filename: String
    let trashPath: String

    enum CodingKeys: String, CodingKey {
        case filename
        case trashPath = "trash_path"
    }
}

struct TweakRecord: Decodable, Identifiable, Hashable {
    let filename: String
    let sizeBytes: Int
    let usedBy: [String]

    /// Read out of the .deb's own control stanza by the CLI. All optional, and
    /// all absent together for a file it couldn't parse — a truncated
    /// download, a renamed zip, a compression the CLI's Python lacks. The
    /// library must keep listing those, so every one of these is `nil`-safe
    /// and the row falls back to the filename it always showed.
    ///
    /// Worth having because the Repos browser displays a package's name,
    /// version and section, and the moment it was downloaded the library
    /// showed a filename and a byte count — information the user had on screen
    /// one click earlier. Two builds of `d-tweak.deb` a month apart were
    /// indistinguishable.
    let name: String?
    let version: String?
    let section: String?
    let author: String?

    var id: String { filename }

    /// The row's headline: the tweak's own name when it has one, else the
    /// filename, which is all this list ever had.
    var displayName: String { name ?? filename }

    /// `1.2.3 · Tweaks · someone`, from whatever parts exist — nil when the
    /// CLI could read none of them, so the view can omit the line entirely
    /// rather than render a lonely separator.
    var detailLine: String? {
        let parts = [version, section, author].compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    enum CodingKeys: String, CodingKey {
        case filename
        case sizeBytes = "size_bytes"
        case usedBy = "used_by"
        case name, version, section, author
    }

    /// Written out with defaults for the control fields, rather than left to
    /// the synthesized memberwise init: the four of them are absent together
    /// for any .deb the CLI couldn't read, so "just the three keys this event
    /// has always had" needs to stay the easy thing to construct — in a test
    /// and in a decode alike.
    init(
        filename: String,
        sizeBytes: Int,
        usedBy: [String],
        name: String? = nil,
        version: String? = nil,
        section: String? = nil,
        author: String? = nil
    ) {
        self.filename = filename
        self.sizeBytes = sizeBytes
        self.usedBy = usedBy
        self.name = name
        self.version = version
        self.section = section
        self.author = author
    }
}

struct CanisterPackage: Decodable, Identifiable, Hashable {
    let packageId: String
    let name: String
    let version: String
    let description: String
    let author: String
    let section: String
    let repositoryId: String
    let price: String
    let sizeBytes: Int?
    let depiction: String?

    var id: String { packageId }

    enum CodingKeys: String, CodingKey {
        case name, version, description, author, section, price, depiction
        case packageId = "package_id"
        case repositoryId = "repository_id"
        case sizeBytes = "size_bytes"
    }
}

struct RepoRecord: Decodable, Identifiable, Hashable {
    let url: String
    let name: String
    let origin: String?
    let packageCount: Int
    let lastRefreshed: String?

    var id: String { url }

    enum CodingKeys: String, CodingKey {
        case url, name, origin
        case packageCount = "package_count"
        case lastRefreshed = "last_refreshed"
    }
}

struct RepoPackage: Decodable, Identifiable, Hashable {
    let packageId: String
    let name: String
    let version: String
    let description: String
    let author: String
    let section: String
    let filename: String
    let sizeBytes: Int?
    let sha256: String?
    let depiction: String?
    let downloadURL: String

    var id: String { packageId }

    enum CodingKeys: String, CodingKey {
        case name, version, description, author, section, filename, sha256, depiction
        case packageId = "package_id"
        case sizeBytes = "size_bytes"
        case downloadURL = "download_url"
    }
}

/// One app's real icon, resolved by the CLI's artwork cascade.
///
/// `source` is a bare `String`, deliberately not an enum. The CLI emits
/// `"ipa"`, `"app-store"`, `"device"`, and also `"unknown"` for a cached hit
/// whose sidecar has no readable source — and nothing here branches on the
/// value. It exists so a person debugging a wrong-looking icon can see where
/// it came from, which a `String` already does. An enum would buy type safety
/// nothing decodes for: a fifth source the CLI adds later would fail to
/// decode on an older GUI build instead of just showing up as new text, and
/// `"unknown"` would need its own case meaning "we don't know" — exactly what
/// a plain `String` already says without one.
struct ArtworkRecord: Decodable, Hashable {
    let bundleId: String
    /// Where the resolved icon lives on disk. A `String`, not a `URL`: it
    /// comes off the wire as a string and is handed straight to
    /// `NSImage(contentsOfFile:)`, which itself takes a path.
    let path: String
    let source: String
    /// Whether this hit came from the cascade's on-disk cache rather than a
    /// fresh resolve this run.
    let cached: Bool

    enum CodingKeys: String, CodingKey {
        case bundleId = "bundle_id"
        case path, source, cached
    }
}

struct DeviceRecord: Decodable, Identifiable, Hashable {
    enum ConnectionType: String, Decodable, Hashable {
        case usb = "USB"
        case network = "Network"
    }

    let udid: String
    let name: String
    let model: String
    let iosVersion: String
    let connectionType: ConnectionType
    let livecontainerBundleId: String?
    /// Whether this is the phone builds are delivered to when no device is
    /// named explicitly — `default_device_udid` in config.yaml, echoed back
    /// onto the device's own event so `DeviceHeader` can draw its chip
    /// without cross-referencing a separate `--settings` read.
    ///
    /// Decoded with `decodeIfPresent(...) ?? false`, deliberately not
    /// `decode(...)`. An older `graft` on PATH does not emit this
    /// field, and `CLIRunner` decodes every event with `try?` — a required
    /// key a real installation doesn't send yet would fail decoding and drop
    /// the *entire* device from the list, not just its chip. The difference
    /// this default is buying is between a missing chip and a missing phone.
    let isDefault: Bool

    var id: String { udid }

    enum CodingKeys: String, CodingKey {
        case udid, name, model
        case iosVersion = "ios_version"
        case connectionType = "connection_type"
        case livecontainerBundleId = "livecontainer_bundle_id"
        case isDefault = "is_default"
    }

    // Written out rather than left to the synthesised conformance:
    // `isDefault` needs `decodeIfPresent(...) ?? false` where every other
    // field keeps the exact behaviour the synthesised initializer already
    // gave it — required keys stay required, and `livecontainerBundleId`
    // (an `Optional` stored property) keeps decoding via `decodeIfPresent`
    // under the hood, same as synthesis would produce.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        udid = try container.decode(String.self, forKey: .udid)
        name = try container.decode(String.self, forKey: .name)
        model = try container.decode(String.self, forKey: .model)
        iosVersion = try container.decode(String.self, forKey: .iosVersion)
        connectionType = try container.decode(ConnectionType.self, forKey: .connectionType)
        livecontainerBundleId = try container.decodeIfPresent(
            String.self, forKey: .livecontainerBundleId
        )
        isDefault = try container.decodeIfPresent(Bool.self, forKey: .isDefault) ?? false
    }
}

/// One device, as `--devices --brief` reports it: identity and connection
/// only, none of the lockdown fields `DeviceRecord` carries.
///
/// A separate type, deliberately, not a `DeviceRecord` with `name` and the
/// rest made optional. A reviewer proposed exactly that — discriminate on
/// whether `name` is present — and it was rejected: it makes "this is a brief
/// event" and "this is a *malformed full* event" the same signal. Today a
/// full `device` event that lost `name` throws, hits `CLIRunner`'s `try?`, is
/// dropped, and the phone visibly vanishes from the list — the `device-error`
/// class of failure this codebase already spent months fixing. Under a
/// presence check, that same corrupt event would instead succeed as a brief
/// record and land in the poll's UDID set. Silent misclassification is worse
/// than a silent drop.
///
/// No invocation emits both shapes — `--devices --brief` emits only brief,
/// `--devices` only full — so no consumer ever wants "a device event, either
/// kind".
struct BriefDeviceRecord: Decodable, Hashable {
    let udid: String
    let connectionType: DeviceRecord.ConnectionType

    enum CodingKeys: String, CodingKey {
        case udid
        case connectionType = "connection_type"
    }
}

/// One device's lockdown-derived vitals: capacity and battery.
///
/// Every field but `udid` and `reachable` is optional, deliberately: a locked
/// or busy phone answers one lockdown domain and declines the other, so the
/// pane renders "—" per cell rather than failing the whole row.
/// `reachable: false` means the phone could not be reached at all — the CLI
/// emits the event anyway so that silence stays distinguishable from a
/// crashed CLI, and so a store can decline to overwrite a last-known-good
/// reading on a transient blip.
struct DeviceInfoRecord: Decodable, Hashable {
    let udid: String
    let reachable: Bool
    let totalBytes: Int?
    let freeBytes: Int?
    let batteryPercent: Int?
    let batteryCharging: Bool?

    enum CodingKeys: String, CodingKey {
        case udid, reachable
        case totalBytes = "total_bytes"
        case freeBytes = "free_bytes"
        case batteryPercent = "battery_percent"
        case batteryCharging = "battery_charging"
    }
}

/// A device usbmux enumerated that the CLI then failed to describe.
///
/// The distinction this exists to preserve: an empty device list reads as "no
/// phone is plugged in" everywhere downstream — the Devices tab's empty state,
/// the build popover's picker, the "no iOS devices connected" the build itself
/// raises. A phone that *is* plugged in and paired but whose lockdown query
/// failed is a different problem with a different fix (unlock it, re-trust it,
/// restart usbmuxd), and reporting it as the first one sends the user looking
/// for a cable.
///
/// `--devices` exits 0 in this case, so there is no failure for a caller to
/// notice, and stderr never reaches one either: `runEvents` surfaces it only by
/// throwing, and `checkExit` throws only on a non-zero exit. This event is the
/// only channel that survives to the GUI.
struct DeviceErrorRecord: Decodable, Identifiable, Hashable {
    let udid: String
    /// The CLI's own words for what went wrong querying this device.
    let error: String

    var id: String { udid }

    /// One line naming the device and the consequence. The UDID is all we
    /// have — the query that would have produced a name is the one that
    /// failed — so it is what identifies the row.
    var summary: String {
        "\(udid) is connected but couldn't be queried, so it isn't in the list: \(error)"
    }
}

struct PreflightCheck: Decodable, Identifiable, Hashable {
    let name: String
    let ok: Bool
    let detail: String?
    let hint: String?
    let actionKey: String?
    /// The row reports a fact rather than passing a test — today only a free
    /// Apple ID's `account` row, which is always `ok`. Rendered as
    /// information, and counted as neither OK nor Fail: a free account is not
    /// a check that passed. Absent on the wire for every other row.
    let info: Bool?
    /// The exact command that installs this row's tool, or nil when there is
    /// none to give — a row that is not a tool (no phone connected), or a
    /// tool with no one-line install.
    let install: String?
    /// Whether the CLI sent `install` at all, null included. A CLI from
    /// before the field existed did not, and for it the command is read back
    /// out of the hint instead; see `installCommand`.
    let reportsInstall: Bool

    var id: String { name }
    var isInfo: Bool { info == true }

    /// The command to show beside this row with a Copy button.
    ///
    /// `install` when the CLI reports it — including its null, which is the
    /// CLI saying there is no command, and is not second-guessed from prose.
    /// From an older CLI, the hint's own `install with: <command>` shape,
    /// which is how `--check` has phrased every tool row's hint since the
    /// first release; any other hint is advice, not a command.
    var installCommand: String? {
        if reportsInstall { return install.flatMap { $0.isEmpty ? nil : $0 } }
        guard let hint, hint.hasPrefix(Self.installHintPrefix) else { return nil }
        let command = hint.dropFirst(Self.installHintPrefix.count)
            .trimmingCharacters(in: .whitespaces)
        return command.isEmpty ? nil : command
    }

    static let installHintPrefix = "install with: "

    enum CodingKeys: String, CodingKey {
        case name, ok, detail, hint, info, install
        case actionKey = "action_key"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        ok = try c.decode(Bool.self, forKey: .ok)
        detail = try c.decodeIfPresent(String.self, forKey: .detail)
        hint = try c.decodeIfPresent(String.self, forKey: .hint)
        actionKey = try c.decodeIfPresent(String.self, forKey: .actionKey)
        info = try c.decodeIfPresent(Bool.self, forKey: .info)
        reportsInstall = c.contains(.install)
        // `try?`: a malformed `install` costs the Copy button, never the row.
        install = (try? c.decodeIfPresent(String.self, forKey: .install)) ?? nil
    }

    init(
        name: String, ok: Bool, detail: String? = nil, hint: String? = nil,
        actionKey: String? = nil, info: Bool? = nil, install: String?? = .none
    ) {
        self.name = name
        self.ok = ok
        self.detail = detail
        self.hint = hint
        self.actionKey = actionKey
        self.info = info
        self.reportsInstall = install != nil
        self.install = install ?? nil
    }
}

struct BuildRecord: Decodable, Identifiable, Hashable {
    let id: Int
    let timestamp: String
    let app: String
    let bundleId: String
    let version: String
    let tweaks: [String]
    let ipaPath: String?
    let ipaSha256: String?
    let logPath: String?
    let signingTeam: String?
    let udid: String?
    /// `"device"` or `"mac"` — which target this artifact was prepared for.
    ///
    /// Never optional, and deliberately so. The CLI normalises the column on
    /// read (`build_log._target_of`): a row written before the column existed
    /// reads NULL and comes back `"device"`, as does any spelling other than
    /// the exact string `"mac"`. So every row `--builds --json` emits carries
    /// a populated `target`, and this side needs no "missing means phone"
    /// rule of its own to get wrong.
    ///
    /// The optional-with-default alternative fails in the wrong direction.
    /// `buildEvents()` drops an undecodable line with `try?`, so if the key
    /// ever went missing a non-optional field empties the Builds list —
    /// visible, and nothing is offered. A `?? "device"` default would instead
    /// label Mac builds as phone builds and offer the phone install on them,
    /// which is precisely the confusion `isForThisMac` exists to prevent.
    let target: String
    let success: Bool
    let error: String?

    enum CodingKeys: String, CodingKey {
        case id, timestamp, app, version, tweaks, success, error, udid, target
        case bundleId = "bundle_id"
        case ipaPath = "ipa_path"
        case ipaSha256 = "ipa_sha256"
        case logPath = "log_path"
        case signingTeam = "signing_team"
    }

    /// `timestamp` as a `Date`, parsed once.
    ///
    /// Three call sites parsed this string by hand — two in `AppDetailView`
    /// within a single `body` pass, one in `BuildsView` — and
    /// `ISO8601DateFormatter.date(from:)` costs ~26 µs, which on a repainting
    /// build log is real. It also papered over failure inconsistently: one site
    /// fell back to `.distantPast`, another passed nil through.
    var date: Date? { DateHelpers.parseISO8601(timestamp) }

    /// Whether this artifact is for this Mac rather than a phone.
    ///
    /// A Mac build has a tweak dylib retargeted at a bundled *stub*
    /// framework, because macOS lacks the private framework it wants. iOS has
    /// the real one, so installing a Mac build on a phone points the tweak at
    /// the stub and breaks it silently. The two actions must never be offered
    /// on the same row.
    var isForThisMac: Bool { target == "mac" }
}

/// One archived IPA the CLI's retention sweep moved to the Trash.
///
/// Emitted once per file, ahead of the `prune-end` summary. Decoded but not
/// rendered line-by-line: a sweep after a build routinely removes several
/// archives at once, and a line each would bury the build's own output under
/// housekeeping. The summary is what the log shows; this exists so the detail
/// is typed and available when something wants it (and so the event isn't a
/// `.unknown` the contract test can't reason about).
struct BuildPrunedRecord: Decodable, Hashable {
    let app: String
    let filename: String
    let path: String
    let sizeBytes: Int
    /// Where the archive landed, or nil on a dry run — nothing moved.
    let trashPath: String?
    /// Build-log rows whose `ipa_path` was cleared because it pointed here.
    /// Those rows lose their "Re-install" button, which is the point: the file
    /// it would have shipped is gone.
    let buildIds: [Int]
    let dryRun: Bool

    enum CodingKeys: String, CodingKey {
        case app, filename, path
        case sizeBytes = "size_bytes"
        case trashPath = "trash_path"
        case buildIds = "build_ids"
        case dryRun = "dry_run"
    }
}

/// The `prune-end` summary that closes a retention sweep.
struct PruneSummary: Decodable, Hashable {
    /// Archives-per-app the policy kept.
    let keep: Int
    let dryRun: Bool
    let removed: Int
    let kept: Int
    let bytesRemoved: Int
    /// Files in built_dir whose name didn't parse as an archive, so the policy
    /// left them alone.
    let unparsed: Int
    /// Archives the sweep tried and failed to move. The CLI emits a `log`
    /// event with `source: "retention"` naming each one and why, so the
    /// specifics are already in the log above this line.
    let failed: Int

    enum CodingKeys: String, CodingKey {
        case keep, removed, kept, unparsed, failed
        case dryRun = "dry_run"
        case bytesRemoved = "bytes_removed"
    }

    /// The single line a build log shows for this sweep, or nil when the sweep
    /// did nothing — the common case after a build, and not worth a line.
    var logSummary: String? {
        guard removed > 0 || failed > 0 else { return nil }
        var text: String
        if removed > 0 {
            let size = fileSize(bytesRemoved)
            let noun = removed == 1 ? "archive" : "archives"
            text = "→ retention: \(dryRun ? "would move" : "moved") \(removed) "
                + "\(noun) to the Trash — \(size) (keeping the newest \(keep) per app)"
        } else {
            text = "→ retention: nothing moved"
        }
        if failed > 0 {
            text += ", \(failed) couldn't be moved"
        }
        return text
    }
}

/// Mach-O binaries the build's decryption check found still encrypted.
///
/// The build does not fail on this. The CLI prints a warning, carries on, and
/// ends `ok: true` — and the archive it produces is real, signed and installed.
/// It just cannot run: LiveContainer will not load an encrypted binary, and a
/// re-signed one has no FairPlay key either, so the app dies on launch.
///
/// That combination is why this event exists at all and why it gets a surface
/// of its own rather than a log line. The only machine-readable trace used to
/// be a `bad=<n>` field on the `stage` event, which `BuildEvent.progress`
/// drops, and console text on the same stdout as the JSON stream, which
/// `String.buildEvents()` throws away — so the panel showed `✓ verify`,
/// `✓ done in 84.3s`, and nothing else, while the user pushed an app that
/// crashes to their phone.
struct EncryptedBinariesRecord: Decodable, Hashable {
    struct Binary: Decodable, Hashable {
        let path: String
        /// One cryptid per arch slice; a non-zero one is what put this binary
        /// in the list. Carried so the log and a bug report can name it —
        /// never rendered at the user as "cryptid 1", which is the tool's
        /// vocabulary and says nothing about what to do next.
        let cryptids: [Int]
    }

    /// The app's config key — the CLI spells it `app` here, the same as on a
    /// `delivery-state` event and for the same reason: it is what `--list`
    /// calls `name`.
    let app: String
    /// How many binaries the CLI found. Kept alongside `binaries` rather than
    /// derived from it: the count is the CLI's own claim about the whole IPA,
    /// and if a future CLI ever caps the list it names, the headline should
    /// still say how many there really are.
    let count: Int
    let binaries: [Binary]

    /// The headline: the consequence, in the user's vocabulary.
    ///
    /// Deliberately not "cryptid 1", not "verify failed", not "N of M Mach-O
    /// binaries". None of those tell someone staring at a green build why the
    /// app they just installed bounces off the home screen.
    var summary: String {
        let noun = count == 1 ? "binary is" : "binaries are"
        return "\(count) \(noun) still encrypted — \(app) will crash on launch"
    }

    /// What to do about it, which is the half the user actually needs. The
    /// cause is always the same one thing: the IPA this build started from was
    /// never decrypted.
    ///
    /// Names the GUI's own control ("Source IPA"), not the CLI flag behind it
    /// (`--provide-ipa`) — the person reading this is in the app, not the
    /// terminal. No spatial claim ("below"), because this string is also the
    /// one a future panel elsewhere would print.
    var advice: String {
        "Anything downloaded from the App Store is encrypted, and Graft "
            + "needs a decrypted copy. Get one from the App Store and decrypt "
            + "it on this Mac, or dump it from a jailbroken device, set it as "
            + "this app's Source IPA, then build again."
    }

    /// The offending binaries, named. nil when the CLI counted without naming
    /// any, so a caller renders nothing rather than an empty row.
    var pathLine: String? {
        binaries.isEmpty ? nil : binaries.map(\.path).joined(separator: " · ")
    }

    /// The build transcript's one line for this, marker included — the same
    /// shape `PruneSummary.logSummary` uses, so the two panels that render a
    /// build stream can't punctuate it two different ways.
    var logLine: String {
        pathLine.map { "✗ \(summary): \($0)" } ?? "✗ \(summary)"
    }
}

/// Which LiveContainer a livecontainer build actually pushed into, when more
/// than one is installed.
///
/// The gotcha this closes: LiveContainer has shipped under several bundle ids
/// (`com.kdt.livecontainer`, `com.kdt.LiveContainer2`, …, each with a team-id
/// suffix) and they can sit side by side on one phone. A push writes the IPA
/// into *one* container's Documents/, and the import that installs it is
/// manual — so opening the other one shows no new IPA and the build looks like
/// it silently did nothing.
///
/// Emitted only when there is a real choice to report; a single installed
/// LiveContainer produces no event, because a line on every push would train
/// the user to skip it.
struct LiveContainerChoiceRecord: Decodable, Hashable {
    let app: String
    /// The container the IPA was written into — the one to import from.
    let bundleId: String
    /// The other LiveContainers installed, which do *not* have this build.
    let others: [String]

    enum CodingKeys: String, CodingKey {
        case app, others
        case bundleId = "bundle_id"
    }

    /// The build transcript's one line for this, marker included — same shape
    /// as `PruneSummary.logSummary` and `EncryptedBinariesRecord.logLine`, so
    /// the panels that render a build stream can't punctuate it three ways.
    ///
    /// Leads with the instruction rather than the count: the user is looking
    /// at their phone deciding which icon to tap, and "2 other LiveContainers
    /// installed" does not tell them which one to open.
    var logLine: String {
        let noun = others.count == 1 ? "LiveContainer" : "LiveContainers"
        return "→ import \(app) from \(bundleId) — \(others.count) other \(noun) "
            + "installed (\(others.joined(separator: ", "))) and this build is not in "
            + (others.count == 1 ? "it" : "them")
    }
}

enum CanisterFetchResult: Equatable {
    case success(filename: String, sizeBytes: Int, path: String?)
    case failure(error: String)
}

enum BuildEvent {
    /// `--list`'s first line: which account config.yaml is for. Its own event
    /// because a config with no apps has no `app` to carry it.
    case account(AccountMode)
    case app(AppRecord)
    case tweak(TweakRecord)
    case tweakUpdate(TweakUpdateRecord)
    case tweakUpdatesEnd(TweakUpdatesEndRecord)
    case tweakUpdated(TweakUpdatedRecord)
    case deviceApp(DeviceAppRecord)
    case deviceAppsEnd(DeviceAppsEndRecord)
    case decryptStart(String)
    case decryptEnd(DecryptEndRecord)
    case decryptCheck(DecryptCheckRecord)
    /// `delivery` is optional because the CLI declines to name one for a
    /// re-install of an app that has since been removed from config.yaml —
    /// see `BuildStartPayload`.
    case buildStart(app: String, bundleId: String, delivery: DeliveryMode?)
    case stage(name: String, status: String, fields: [String: Any])
    case log(source: String, line: String)
    /// `stopped` is the CLI saying the build ended because it was told to
    /// stop (SIGTERM/SIGINT), not because anything went wrong: C-STOP. False
    /// when absent, which is every CLI from before that contract.
    case buildEnd(app: String, ok: Bool, error: String?, elapsedSeconds: Double?, stopped: Bool = false)
    case canisterResult(CanisterPackage)
    case canisterSearchEnd(count: Int)
    case canisterFetchStart(target: String)
    case canisterFetchEnd(CanisterFetchResult)
    case repoAdded(RepoRecord)
    case repoRemoved(url: String)
    case repo(RepoRecord)
    case repoRefreshStart(url: String)
    case repoPackage(RepoPackage)
    case repoPackagesEnd(url: String, count: Int)
    case reposEnd(count: Int)
    case appAdded(AppAddedRecord)
    case appRemoved(AppRemovedRecord)
    case appUpdated(AppUpdatedRecord)
    case deliveryState(DeliveryStateRecord)
    case cachedIPA(CachedIPARecord)
    case ipaProvided(CachedIPARecord)
    case stagedIPA(StagedIPARecord)
    case stagedIPAsEnd(count: Int)
    case stagedIPADeleted(StagedIPADeletedRecord)
    case stagedIPAInUse(StagedIPAInUseRecord)
    case handedToPlayCover(HandedToPlayCoverRecord)
    case tweakAttached(TweakMutationRecord)
    case tweakDetached(TweakMutationRecord)
    case tweakDeleted(TweakDeletedRecord)
    case tweakInUse(TweakInUseRecord)
    case settingsUpdated(SettingsUpdatedRecord)
    case settings(SettingsRecord)
    case buildRecord(BuildRecord)
    case buildPruned(BuildPrunedRecord)
    case pruneEnd(PruneSummary)
    case encryptedBinaries(EncryptedBinariesRecord)
    case liveContainerChoice(LiveContainerChoiceRecord)
    case device(DeviceRecord)
    case deviceBrief(BriefDeviceRecord)
    case deviceInfo(DeviceInfoRecord)
    case lcStatus(LCStatusRecord)
    case lcInstallProgress(LCInstallProgressRecord)
    case lcInstallWaiting(LCInstallWaitingRecord)
    case lcInstalled(LCInstalledRecord)
    case lcCertPushed(LCCertPushedRecord)
    case lcCleaned(LCCleanedRecord)
    case artwork(ArtworkRecord)
    case deviceError(DeviceErrorRecord)
    case devicesEnd(count: Int)
    case preflightCheck(PreflightCheck)
    /// `account` is nil from a CLI that predates free mode.
    case preflightEnd(okCount: Int, failCount: Int, account: AccountMode?)
    // App Store acquisition — records in `AppStoreRecords.swift`.
    case appStoreStatus(AppStoreStatusRecord)
    case appStoreVersion(AppStoreVersionRecord)
    case appStoreStage(AppStoreStageRecord)
    case appStoreDecrypted(AppStoreDecryptedRecord)
    case unknown(type: String, raw: [String: Any])

    enum DecodeError: Error {
        case missingType
        case notJsonObject
    }

    private struct TypeOnly: Decodable { let type: String }

    /// `delivery` is `null` on the wire when the CLI genuinely does not know
    /// which mode this build would use: `_do_reinstall_build` emits it that
    /// way for an app that has been removed from config.yaml, deliberately, so
    /// that the guard which raises "no longer in config.yaml" can still run
    /// *inside* the build envelope and be reported as a build-end.
    ///
    /// Carried through as nil rather than defaulted. A `?? .sideload` here
    /// turned "we don't know" into a confident claim the CLI had explicitly
    /// declined to make — and, for the livecontainer apps this tool mostly
    /// builds, a confident claim of the opposite mode, printed one line above
    /// the error saying the app is gone.
    private struct BuildStartPayload: Decodable {
        let app: String?
        let bundleId: String?
        let delivery: DeliveryMode?
        enum CodingKeys: String, CodingKey {
            case app, delivery
            case bundleId = "bundle_id"
        }
    }

    private struct LogPayload: Decodable {
        let source: String?
        let line: String?
    }

    private struct BuildEndPayload: Decodable {
        let app: String?
        let ok: Bool?
        let error: String?
        let elapsedSeconds: Double?
        /// Optional on the wire: a CLI from before C-STOP never sends it.
        let stopped: Bool?
        enum CodingKeys: String, CodingKey {
            case app, ok, error, stopped
            case elapsedSeconds = "elapsed_seconds"
        }
    }

    private struct CanisterSearchEndPayload: Decodable {
        let count: Int?
    }

    private struct DevicesEndPayload: Decodable {
        let count: Int?
    }

    private struct PreflightEndPayload: Decodable {
        let okCount: Int?
        let failCount: Int?
        let account: AccountMode?
        enum CodingKeys: String, CodingKey {
            case okCount = "ok_count"
            case failCount = "fail_count"
            case account
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            okCount = try c.decodeIfPresent(Int.self, forKey: .okCount)
            failCount = try c.decodeIfPresent(Int.self, forKey: .failCount)
            // An unknown spelling is "not known", as on `AppRecord`.
            account = (try? c.decodeIfPresent(AccountMode.self, forKey: .account)) ?? nil
        }
    }

    private struct AccountPayload: Decodable {
        let account: AccountMode
    }

    private struct CanisterFetchStartPayload: Decodable {
        let target: String?
    }

    private struct CanisterFetchEndPayload: Decodable {
        let ok: Bool?
        let filename: String?
        let sizeBytes: Int?
        let path: String?
        let error: String?
        enum CodingKeys: String, CodingKey {
            case ok, filename, path, error
            case sizeBytes = "size_bytes"
        }
    }

    private struct RepoRemovedPayload: Decodable {
        let url: String?
    }

    private struct RepoRefreshStartPayload: Decodable {
        let url: String?
    }

    private struct RepoPackagesEndPayload: Decodable {
        let url: String?
        let count: Int?
    }

    private struct ReposEndPayload: Decodable {
        let count: Int?
    }

    private struct StagedIPAsEndPayload: Decodable {
        let count: Int?
    }

    private struct IPAProvidedPayload: Decodable {
        let app: String
        let bundleId: String
        let version: String
        let filename: String
        let path: String
        let sizeBytes: Int
        enum CodingKeys: String, CodingKey {
            case app, version, filename, path
            case bundleId = "bundle_id"
            case sizeBytes = "size_bytes"
        }
    }

    static func decode(line: String) throws -> BuildEvent {
        guard let data = line.data(using: .utf8) else {
            throw DecodeError.notJsonObject
        }
        let decoder = JSONDecoder()
        let head = try decoder.decode(TypeOnly.self, from: data)

        switch head.type {
        case "account":
            return .account(try decoder.decode(AccountPayload.self, from: data).account)
        case "app":
            return .app(try decoder.decode(AppRecord.self, from: data))
        case "tweak":
            return .tweak(try decoder.decode(TweakRecord.self, from: data))
        case "tweak-update":
            return .tweakUpdate(try decoder.decode(TweakUpdateRecord.self, from: data))
        case "tweak-updates-end":
            return .tweakUpdatesEnd(try decoder.decode(TweakUpdatesEndRecord.self, from: data))
        case "tweak-updated":
            return .tweakUpdated(try decoder.decode(TweakUpdatedRecord.self, from: data))
        case "device-app":
            return .deviceApp(try decoder.decode(DeviceAppRecord.self, from: data))
        case "device-apps-end":
            return .deviceAppsEnd(try decoder.decode(DeviceAppsEndRecord.self, from: data))
        case "decrypt-start":
            struct P: Decodable { let bundleId: String
                enum CodingKeys: String, CodingKey { case bundleId = "bundle_id" } }
            return .decryptStart((try decoder.decode(P.self, from: data)).bundleId)
        case "decrypt-end":
            return .decryptEnd(try decoder.decode(DecryptEndRecord.self, from: data))
        case "decrypt-check":
            return .decryptCheck(try decoder.decode(DecryptCheckRecord.self, from: data))
        case "build-start":
            let p = try decoder.decode(BuildStartPayload.self, from: data)
            return .buildStart(
                app: p.app ?? "",
                bundleId: p.bundleId ?? "",
                delivery: p.delivery
            )
        case "log":
            let p = try decoder.decode(LogPayload.self, from: data)
            return .log(source: p.source ?? "", line: p.line ?? "")
        case "build-end":
            let p = try decoder.decode(BuildEndPayload.self, from: data)
            return .buildEnd(
                app: p.app ?? "",
                ok: p.ok ?? false,
                error: p.error,
                elapsedSeconds: p.elapsedSeconds,
                stopped: p.stopped ?? false
            )
        case "stage":
            guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw DecodeError.notJsonObject
            }
            let extras = json.filter { $0.key != "type" && $0.key != "name" && $0.key != "status" }
            return .stage(
                name: (json["name"] as? String) ?? "",
                status: (json["status"] as? String) ?? "",
                fields: extras
            )
        case "build-record":
            return .buildRecord(try decoder.decode(BuildRecord.self, from: data))
        case "build-pruned":
            return .buildPruned(try decoder.decode(BuildPrunedRecord.self, from: data))
        case "prune-end":
            return .pruneEnd(try decoder.decode(PruneSummary.self, from: data))
        case "encrypted-binaries":
            return .encryptedBinaries(
                try decoder.decode(EncryptedBinariesRecord.self, from: data)
            )
        case "livecontainer-choice":
            return .liveContainerChoice(
                try decoder.decode(LiveContainerChoiceRecord.self, from: data)
            )
        case "device":
            return .device(try decoder.decode(DeviceRecord.self, from: data))
        case "device-brief":
            return .deviceBrief(try decoder.decode(BriefDeviceRecord.self, from: data))
        case "device-info":
            return .deviceInfo(try decoder.decode(DeviceInfoRecord.self, from: data))
        case "lc-status":
            return .lcStatus(try decoder.decode(LCStatusRecord.self, from: data))
        case "lc-install-progress":
            return .lcInstallProgress(try decoder.decode(LCInstallProgressRecord.self, from: data))
        case "lc-install-waiting":
            return .lcInstallWaiting(try decoder.decode(LCInstallWaitingRecord.self, from: data))
        case "lc-installed":
            return .lcInstalled(try decoder.decode(LCInstalledRecord.self, from: data))
        case "lc-cert-pushed":
            return .lcCertPushed(try decoder.decode(LCCertPushedRecord.self, from: data))
        case "lc-cleaned":
            return .lcCleaned(try decoder.decode(LCCleanedRecord.self, from: data))
        case "artwork":
            return .artwork(try decoder.decode(ArtworkRecord.self, from: data))
        case "device-error":
            return .deviceError(try decoder.decode(DeviceErrorRecord.self, from: data))
        case "devices-end":
            let p = try decoder.decode(DevicesEndPayload.self, from: data)
            return .devicesEnd(count: p.count ?? 0)
        case "preflight-check":
            return .preflightCheck(try decoder.decode(PreflightCheck.self, from: data))
        case "preflight-end":
            let p = try decoder.decode(PreflightEndPayload.self, from: data)
            return .preflightEnd(
                okCount: p.okCount ?? 0, failCount: p.failCount ?? 0, account: p.account
            )
        case "canister-result":
            return .canisterResult(try decoder.decode(CanisterPackage.self, from: data))
        case "canister-search-end":
            let p = try decoder.decode(CanisterSearchEndPayload.self, from: data)
            return .canisterSearchEnd(count: p.count ?? 0)
        case "canister-fetch-start":
            let p = try decoder.decode(CanisterFetchStartPayload.self, from: data)
            return .canisterFetchStart(target: p.target ?? "")
        case "canister-fetch-end":
            let p = try decoder.decode(CanisterFetchEndPayload.self, from: data)
            if p.ok == true, let filename = p.filename, let size = p.sizeBytes {
                return .canisterFetchEnd(.success(filename: filename, sizeBytes: size, path: p.path))
            } else {
                return .canisterFetchEnd(.failure(error: p.error ?? "fetch failed"))
            }
        case "repo-added":
            return .repoAdded(try decoder.decode(RepoRecord.self, from: data))
        case "repo-removed":
            let p = try decoder.decode(RepoRemovedPayload.self, from: data)
            return .repoRemoved(url: p.url ?? "")
        case "repo":
            return .repo(try decoder.decode(RepoRecord.self, from: data))
        case "repo-refresh-start":
            let p = try decoder.decode(RepoRefreshStartPayload.self, from: data)
            return .repoRefreshStart(url: p.url ?? "")
        case "repo-package":
            return .repoPackage(try decoder.decode(RepoPackage.self, from: data))
        case "repo-packages-end":
            let p = try decoder.decode(RepoPackagesEndPayload.self, from: data)
            return .repoPackagesEnd(url: p.url ?? "", count: p.count ?? 0)
        case "repos-end":
            let p = try decoder.decode(ReposEndPayload.self, from: data)
            return .reposEnd(count: p.count ?? 0)
        case "app-added":
            return .appAdded(try decoder.decode(AppAddedRecord.self, from: data))
        case "app-removed":
            return .appRemoved(try decoder.decode(AppRemovedRecord.self, from: data))
        case "app-updated":
            return .appUpdated(try decoder.decode(AppUpdatedRecord.self, from: data))
        case "delivery-state":
            return .deliveryState(try decoder.decode(DeliveryStateRecord.self, from: data))
        case "cached-ipa":
            // The "missing" event omits all fields except exists+app; decode
            // permissively so a partial JSON shape doesn't fail.
            guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw DecodeError.notJsonObject
            }
            let exists = json["exists"] as? Bool ?? false
            let record = CachedIPARecord(
                exists: exists,
                app: (json["app"] as? String) ?? "",
                bundleId: exists ? ((json["bundle_id"] as? String) ?? "") : "",
                version: exists ? ((json["version"] as? String) ?? "") : "",
                filename: exists ? ((json["filename"] as? String) ?? "") : "",
                path: exists ? ((json["path"] as? String) ?? "") : "",
                sizeBytes: exists ? ((json["size_bytes"] as? Int) ?? 0) : 0
            )
            return .cachedIPA(record)
        case "ipa-provided":
            // ipa-provided is always a full payload — same shape as cached-ipa
            // with exists implicitly true.
            let r = try decoder.decode(IPAProvidedPayload.self, from: data)
            return .ipaProvided(CachedIPARecord(
                exists: true,
                app: r.app,
                bundleId: r.bundleId,
                version: r.version,
                filename: r.filename,
                path: r.path,
                sizeBytes: r.sizeBytes
            ))
        case "staged-ipa":
            return .stagedIPA(try decoder.decode(StagedIPARecord.self, from: data))
        case "staged-ipas-end":
            let p = try decoder.decode(StagedIPAsEndPayload.self, from: data)
            return .stagedIPAsEnd(count: p.count ?? 0)
        case "staged-ipa-deleted":
            return .stagedIPADeleted(try decoder.decode(StagedIPADeletedRecord.self, from: data))
        case "staged-ipa-in-use":
            return .stagedIPAInUse(try decoder.decode(StagedIPAInUseRecord.self, from: data))
        case "handed-to-playcover":
            return .handedToPlayCover(try decoder.decode(HandedToPlayCoverRecord.self, from: data))
        case "tweak-attached":
            return .tweakAttached(try decoder.decode(TweakMutationRecord.self, from: data))
        case "tweak-detached":
            return .tweakDetached(try decoder.decode(TweakMutationRecord.self, from: data))
        case "settings":
            return .settings(try decoder.decode(SettingsRecord.self, from: data))
        case "settings-updated":
            return .settingsUpdated(try decoder.decode(SettingsUpdatedRecord.self, from: data))
        case "tweak-in-use":
            return .tweakInUse(try decoder.decode(TweakInUseRecord.self, from: data))
        case "tweak-deleted":
            return .tweakDeleted(try decoder.decode(TweakDeletedRecord.self, from: data))
        case "appstore-status":
            return .appStoreStatus(try decoder.decode(AppStoreStatusRecord.self, from: data))
        case "appstore-version":
            return .appStoreVersion(try decoder.decode(AppStoreVersionRecord.self, from: data))
        case "appstore-stage":
            struct P: Decodable { let app: String; let stage: String }
            let p = try decoder.decode(P.self, from: data)
            return .appStoreStage(AppStoreStageRecord(app: p.app, stage: AppStoreStage(raw: p.stage)))
        case "appstore-decrypted":
            return .appStoreDecrypted(try decoder.decode(AppStoreDecryptedRecord.self, from: data))
        default:
            guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw DecodeError.notJsonObject
            }
            return .unknown(type: head.type, raw: json)
        }
    }
}

/// The slice of `BuildEvent` that a live build panel actually renders: a build
/// starting, its stages advancing, its log lines, and its outcome.
///
/// This exists so the panels don't have to `switch` over all of `BuildEvent`.
/// They used to, and every new event type — `.tweakDeleted`, then
/// `.appRemoved`/`.appUpdated` — broke two unrelated view files until somebody
/// appended another name to a thirty-case ignore list. The compile error taught
/// nobody anything; it just produced churn. `BuildEvent.decode` still has a
/// `default:`, and that is the one place a new event genuinely has to be
/// considered.
///
/// Deliberately narrower than the events it comes from: `.stage`'s `fields`
/// dictionary, `.buildStart`'s bundle id and `.buildEnd`'s app name are all
/// dropped, because no panel reads them. Widen this when a panel needs more,
/// not before.
enum BuildProgress: Equatable {
    /// `delivery` is optional for the same reason the event's is: nil is the
    /// CLI saying it does not know, and a panel that cannot represent that has
    /// no choice but to invent an answer. `deliveryLabel` is what every panel
    /// renders, so the three of them can't phrase "unknown" three ways.
    case start(app: String, delivery: DeliveryMode?)
    case stage(name: String, status: String)
    case log(source: String, line: String)
    /// The archive retention sweep that runs on a successful build, just
    /// before `build-end`. Carries the summary only: the CLI also emits one
    /// `build-pruned` per file, and rendering those would drown the build.
    case pruned(PruneSummary)
    /// Binaries the decryption check found still encrypted. Part of the build
    /// stream — it happens at stage 2.5, mid-transcript — and the one line in
    /// that transcript that contradicts the `✓ done` printed under it.
    ///
    /// This is the widening the comment above invites, and deliberately not
    /// confined to the one panel that can receive it today: every panel that
    /// renders this type ships an IPA to a phone, and one that silently drops
    /// "this build will not launch" is the bug this case was added to close.
    /// The compile error in the other panel is the decision being asked for.
    case encrypted(EncryptedBinariesRecord)
    /// Which LiveContainer the push landed in, when several are installed.
    /// Part of the build stream for the same reason `.encrypted` is: it
    /// happens mid-transcript, it decides whether the user finds the IPA at
    /// all, and it is the one line in an otherwise green build that says the
    /// obvious next action is not the one they are about to take.
    case liveContainerChoice(LiveContainerChoiceRecord)
    case finished(ok: Bool, error: String?, elapsed: Double?)
    /// The build was stopped, and the CLI said so: a `build-end` with
    /// `stopped: true` (C-STOP). Its own case rather than `finished(ok:
    /// false)`, because that is the one reading it must never get — a stop is
    /// the thing the user asked for, not a failure, and every panel that
    /// switches over this type has to decide what it shows for one.
    ///
    /// The CLI writes no history row for it, so there is no "last build" to
    /// describe it either: what the panel had is all there is.
    case stopped(elapsed: Double?)

    /// Whether a build stream reported being stopped.
    ///
    /// Read off `RunResult.events` once the subprocess has returned — the
    /// callback that would also see it may not have run yet — so that a stop
    /// the app did not start (a `kill` from Terminal, the CLI's own SIGINT)
    /// is read as a stop and not as the non-zero exit that follows it.
    static func stopReported(in events: [BuildEvent]) -> Bool {
        events.contains { if case .stopped? = $0.progress { return true }; return false }
    }
}

extension Optional where Wrapped == DeliveryMode {
    /// How a build stream's delivery mode reads in a log line.
    ///
    /// Spelled out rather than left to each panel's `?? "sideload"`, because
    /// that default was the bug: the one case that produces nil is a
    /// re-install of an app that is gone from config.yaml, and the line above
    /// the error saying so used to read "→ re-install: gone (sideload)" —
    /// naming a mode nothing had looked up, usually the wrong one.
    var buildLogLabel: String { self?.rawValue ?? "delivery unknown" }
}

extension BuildEvent {
    /// This event's build-stream meaning, or nil for every event that isn't
    /// part of a build stream.
    ///
    /// The `default:` is the point of the whole type — a new `BuildEvent` case
    /// is not part of a build stream until someone decides it is, and until
    /// then it needs no edit here or in any view.
    var progress: BuildProgress? {
        switch self {
        case let .buildStart(app, _, delivery):
            return .start(app: app, delivery: delivery)
        case let .stage(name, status, _):
            return .stage(name: name, status: status)
        case let .log(source, line):
            return .log(source: source, line: line)
        case let .pruneEnd(summary):
            return .pruned(summary)
        case let .encryptedBinaries(record):
            return .encrypted(record)
        case let .liveContainerChoice(record):
            return .liveContainerChoice(record)
        case let .buildEnd(_, ok, error, elapsedSeconds, stopped):
            // Ahead of `ok`: a stopped build-end carries `ok: false`, and
            // that is not a verdict on the build.
            if stopped { return .stopped(elapsed: elapsedSeconds) }
            return .finished(ok: ok, error: error, elapsed: elapsedSeconds)
        default:
            return nil
        }
    }
}

extension String {
    func buildEvents() -> [BuildEvent] {
        split(separator: "\n", omittingEmptySubsequences: true)
            .compactMap { try? BuildEvent.decode(line: String($0)) }
    }
}
