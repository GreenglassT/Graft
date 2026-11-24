import Foundation

// The App Store acquisition path's events, decoded.
//
// A file of their own rather than four more structs in `BuildEvent.swift`,
// which keeps the edit to that shared file down to its enum cases and decode
// arms. The CLI side is `graft_ipa/appstore.py`; the field names are its
// spellings.

/// `appstore-status`: what the App Store path needs and whether this Mac has it.
///
/// The password and the 2FA code never appear here: they go to the CLI's
/// stdin and nowhere else. `email` is the signed-in Apple ID; the account's
/// name is never sent. `setupCommands` are the Terminal commands for whatever
/// is missing, which Settings shows only for a missing tool.
struct AppStoreStatusRecord: Decodable, Equatable {
    struct IPATool: Decodable, Equatable {
        let path: String
        let present: Bool
        /// nil when the binary is absent or would not say.
        let version: String?
        /// nil when ipatool could not say — `auth info` hung or printed no
        /// verdict. Not "signed out": that would send the user to type a
        /// password and a 2FA code for nothing.
        let signedIn: Bool?
        /// Set for a version known to be broken (2.8.1, whose downloads fail).
        let warning: String?
        /// The one command that installs the release Graft is tested with,
        /// for a Copy button; nil when nothing needs installing (and from a
        /// CLI too old to say).
        var install: String? = nil
        /// The signed-in Apple ID's email, from `auth info`. nil when signed
        /// out, when ipatool couldn't say, and for a 1.x sign-in (not asked).
        var email: String? = nil
        /// A sign-in made by the user's own ipatool under Graft 1.x, which
        /// Graft's re-signed ipatool can't read (Keychain ACL). Status didn't
        /// ask ipatool about it, so no keychain dialog popped up.
        var legacySignin: Bool? = nil
        /// `CLIContract.ToolSource`: which ipatool this is. Anything but
        /// "bundled" means the Apple ID and password go to a binary Graft
        /// didn't ship, and the sign-in sheet says so. nil from a CLI too old
        /// to send it.
        var source: String? = nil
        /// Why `signedIn` is nil, when it isn't a legacy sign-in: one of
        /// `CLIContract.UnknownSignIn`, or a word this app doesn't know yet
        /// ("keychain-locked"). nil otherwise, and from a CLI too old to say.
        var unknownReason: String? = nil
        /// The CLI's own sentence for that reason, which the ipatool row
        /// shows under it. nil when the CLI didn't send one: the row then
        /// says it in its own words (`AppStoreSettingsSection.unknownLine`).
        var unknownMessage: String? = nil
        /// The CLI's note about the ipatool config.yaml names: that path
        /// isn't there any more, so Graft.app's own copy runs instead, and
        /// removing `appstore.ipatool` stops the note (delta audit try13,
        /// #7). The row shows it under itself, as it does `unknownMessage`.
        /// nil when there's nothing to say, and from a CLI too old to say.
        var note: String? = nil

        enum CodingKeys: String, CodingKey {
            case path, present, version, warning, install, source, note
            case signedIn = "signed_in"
            case email
            case legacySignin = "legacy_signin"
            case unknownReason = "unknown_reason"
            case unknownMessage = "unknown_message"
        }
    }

    struct UnfairPlay: Decodable, Equatable {
        let path: String
        let present: Bool
        let version: String?
        /// A recovered enrollment model exists. Without one nothing decrypts.
        let enrolled: Bool
        let modelDir: String
        /// The build-from-source steps as one command, for a Copy button; nil
        /// when it is present (or this Mac cannot run it, or the CLI is too
        /// old to say). Enrolling is not in it: that needs root and an app
        /// name only the user knows, and stays in `setupCommands`.
        var install: String? = nil
        /// Why a model that exists can't be used: not this user's, or not
        /// readable. nil when there is none, or nothing is wrong with it.
        var modelProblem: String? = nil
        /// The CLI's note about the UnfairPlay config.yaml names, as
        /// `IPATool.note` is about ipatool's: the configured path isn't there,
        /// so Graft.app's own copy runs. Shown under the row.
        var note: String? = nil

        enum CodingKeys: String, CodingKey {
            case path, present, version, enrolled, install, note
            case modelDir = "model_dir"
            case modelProblem = "model_problem"
        }
    }

    let ipatool: IPATool
    let unfairplay: UnfairPlay
    let setupCommands: [String]

    enum CodingKeys: String, CodingKey {
        case ipatool, unfairplay
        case setupCommands = "setup_commands"
    }
}

/// `appstore-version`: one app's App Store version against what is cached.
///
/// `storeVersion` nil with `error` set is the CLI saying it could not tell —
/// a different claim from "no update", and the row says so.
struct AppStoreVersionRecord: Decodable, Equatable {
    let app: String
    let bundleId: String
    let storeVersion: String?
    let cachedVersion: String?
    let updateAvailable: Bool
    let error: String?

    enum CodingKeys: String, CodingKey {
        case app, error
        case bundleId = "bundle_id"
        case storeVersion = "store_version"
        case cachedVersion = "cached_version"
        case updateAvailable = "update_available"
    }
}

/// Where an App Store get (or an encrypted drop) has got to.
///
/// `other` keeps a stage name this app does not know yet, so a new step on
/// the CLI side still reads as work in progress rather than a dropped line.
/// Not part of the machine-checked contract — nothing here *branches* on a
/// stage, it only labels it (see `CLIContract.Stage`'s rule).
enum AppStoreStage: Equatable {
    case download
    case decrypt
    case stage
    case other(String)

    init(raw: String) {
        switch raw {
        case "download": self = .download
        case "decrypt": self = .decrypt
        case "stage": self = .stage
        default: self = .other(raw)
        }
    }

    /// The progress line the app pane shows while this stage runs.
    var label: String {
        switch self {
        case .download: return "Downloading…"
        case .decrypt: return "Decrypting…"
        case .stage: return "Staging…"
        case .other: return "Working…"
        }
    }
}

/// `appstore-stage`.
struct AppStoreStageRecord: Equatable {
    let app: String
    let stage: AppStoreStage
}

/// `appstore-decrypted`: what UnfairPlay reported for a decrypt that was kept.
struct AppStoreDecryptedRecord: Decodable, Equatable {
    let app: String
    let encryptedImages: Int
    let decryptedPages: Int
    /// Every decrypted page matched Apple's own pre-encryption hashes. False
    /// only after "Use it anyway…": the CLI refuses that unless told
    /// `--allow-unverified`, which this app sends only behind a confirmation.
    let plaintextVerified: Bool

    enum CodingKeys: String, CodingKey {
        case app
        case encryptedImages = "encrypted_images"
        case decryptedPages = "decrypted_pages"
        case plaintextVerified = "plaintext_verified"
    }
}

/// `appstore-already-staged`: a get that stopped before downloading, because
/// the App Store's current version is already staged as a get stages it
/// (`appstore.already_staged`). It is that get's whole output, and it exits 0:
/// nothing was downloaded, decrypted or changed. Get Again (`--again`)
/// downloads anyway.
struct AppStoreAlreadyStagedRecord: Decodable, Equatable {
    let app: String
    let bundleId: String
    /// The version the App Store has, and the staged IPA is.
    let version: String
    let filename: String
    let path: String

    enum CodingKeys: String, CodingKey {
        case app, version, filename, path
        case bundleId = "bundle_id"
    }
}

/// `appstore-refused`: a get or an encrypted drop the CLI would not stage, and
/// why (C-UNVERIFIED). Emitted just before the refusal's non-zero exit, so it
/// is read off the failed run's events — the only part of a failure that is
/// data rather than a sentence.
struct AppStoreRefusalRecord: Decodable, Equatable {
    /// The config app's name for a get, a drop or a claim. On
    /// `--appstore-enroll-command`'s "unverified-helper" refusal it is
    /// something else: the enrolling app's resolved path
    /// (`/Applications/<App>.app`), not a configured app (fix-check #12, #15).
    /// Nothing here reads it from that refusal. Optional, so a refusal without
    /// it still decodes: `buildEvents` drops a line that fails to decode
    /// without a trace.
    var app: String? = nil
    /// `CLIContract.AppStoreRefusal`. Kept as text so a reason this app does
    /// not know yet still decodes, and is simply not one it offers a retry for.
    let reason: String
    /// With "needs-license" and "paid": the iTunes price (0 for a free app),
    /// its formatted text, and the App Store id. nil when the lookup failed.
    var price: Double? = nil
    var formattedPrice: String? = nil
    var trackId: Int? = nil
    /// With "unverified-helper": `CLIContract.UnverifiedHelperKind`, the
    /// UnfairPlay root would run, and the CLI's own sentence saying why
    /// nothing checks it.
    var kind: String? = nil
    var helper: String? = nil
    var message: String? = nil
    /// With "account-changed": the Apple ID ipatool is signed in as now.
    var email: String? = nil

    enum CodingKeys: String, CodingKey {
        case app, reason, price, kind, helper, message, email
        case formattedPrice = "formatted_price"
        case trackId = "track_id"
    }
}

/// A get or drop the CLI refused because UnfairPlay decrypted the app but
/// couldn't verify it against Apple's own page hashes.
///
/// Thrown in place of the CLI's own `CLIError`, for two reasons. The app pane
/// offers "Use it anyway…" for exactly this failure, so it has to be a case it
/// can match. And the CLI's hint for it is a Terminal command — "run in
/// Terminal: graft … --allow-unverified" — which is the wrong advice in a
/// window with a button for it; this carries the app's own sentence instead.
struct UnverifiedDecrypt: LocalizedError, Equatable {
    var errorDescription: String? { Self.message }

    /// What the banner says. The button under it says what to do.
    static let message = "UnfairPlay decrypted this app but couldn't check it against "
        + "Apple's own hashes, so it wasn't staged."

    /// The confirmation "Use it anyway…" opens.
    static let confirmTitle = "Use this decrypt anyway?"
    static let confirmMessage = "UnfairPlay decrypted the app but couldn't check it "
        + "against Apple's own hashes. Older apps often report this. If the app "
        + "crashes at launch, this decrypt is the likely cause."
    static let confirmButton = "Use It Anyway"
    static let offerButton = "Use it anyway…"

    /// The refusal, when a failed run's events carry it.
    static func refusal(in events: [BuildEvent]) -> UnverifiedDecrypt? {
        let refused = events.contains { event in
            if case let .appStoreRefused(record) = event {
                return record.reason == CLIContract.AppStoreRefusal.unverified
            }
            return false
        }
        return refused ? UnverifiedDecrypt() : nil
    }
}

extension CLIRunner.Refusal {
    /// `UnverifiedDecrypt.refusal(in:)`, as `streamEvents` takes it.
    static var unverifiedDecrypt: Self { Self(UnverifiedDecrypt.refusal(in:)) }
}

// MARK: - App Store search (`--appstore-search`)

/// `appstore-search-result`: one app the App Store search found.
///
/// The CLI side is `graft_ipa/appstore_search.py` (`event_fields`); the field
/// names are its spellings.
struct AppStoreSearchResult: Decodable, Equatable, Identifiable {
    let trackId: Int
    let bundleId: String
    let name: String
    let developer: String
    let version: String
    /// 0 for a free app; nil when the listing gave no number.
    let price: Double?
    /// The store's own spelling: "Free", "$4.99", "4,99 €". nil when absent.
    let formattedPrice: String?
    let currency: String?
    let artworkUrl: String?
    /// A small copy of the icon, downloaded and cached by the CLI (this app
    /// makes no connections of its own). nil draws the letter tile.
    let iconPath: String?
    /// The config.yaml app already using this bundle id, or nil.
    let configuredAs: String?

    var id: Int { trackId }

    /// Whether getting this app costs money: the paid-app note, and whether
    /// Get-after-add starts checked.
    ///
    /// **The hook sub-project 4 builds on.** `.free` adds nothing today. A get
    /// of a free app this Apple ID doesn't own yet fails with ipatool's license
    /// error, and its hint lands on the app pane. Part 4's `--appstore-claim`
    /// changes what happens on that branch. Graft never buys a paid app.
    ///
    /// `.unknown` is a listing with no usable price. Never `.free`: only a
    /// price of 0 is free, and the CLI's table says "price unknown" too
    /// (`appstore_search.price_label`).
    enum Acquisition: Equatable {
        case free
        case paid(price: String)
        case unknown
    }

    var acquisition: Acquisition {
        guard let price else { return .unknown }
        guard price > 0 else { return .free }
        return .paid(price: formattedPrice ?? String(format: "%.2f", price))
    }

    /// One of Apple's own apps (`com.apple.*`), which the CLI won't get
    /// (`appstore.apples_own`): iOS ships them, and only Apple can sign one.
    var isApplesOwn: Bool { bundleId.lowercased().hasPrefix("com.apple.") }

    enum CodingKeys: String, CodingKey {
        case name, developer, version, price, currency
        case trackId = "track_id"
        case bundleId = "bundle_id"
        case formattedPrice = "formatted_price"
        case artworkUrl = "artwork_url"
        case iconPath = "icon_path"
        case configuredAs = "configured_as"
    }
}

/// `appstore-search-end`: the sentinel. A search with no results is this alone.
struct AppStoreSearchEndRecord: Decodable, Equatable {
    let term: String
    /// The store that was asked, upper-case.
    let country: String
    let count: Int
    /// How the store was chosen, when the CLI has something to say ("Searching
    /// the US store: your region has no App Store."); nil otherwise, and from
    /// a CLI too old to send it.
    var note: String? = nil
}

/// `appstore-search-failed`: why a search got no usable answer, just before its
/// non-zero exit. `reason` is `CLIContract.AppStoreSearchFailure` or another
/// word this app shows as a plain error.
struct AppStoreSearchFailedRecord: Decodable, Equatable {
    let term: String
    let reason: String
    /// As `AppStoreSearchEndRecord.note`.
    var note: String? = nil
}

/// The App Store couldn't be reached: Search's offline state.
///
/// Thrown in place of the CLI's own error, so the store can tell offline
/// apart from every other failure without matching words.
struct AppStoreSearchOffline: LocalizedError, Equatable {
    var errorDescription: String? { Self.message }

    static let message = "Can't reach the App Store. Check this Mac's internet connection."

    static func refusal(in events: [BuildEvent]) -> AppStoreSearchOffline? {
        let offline = events.contains { event in
            if case let .appStoreSearchFailed(record) = event {
                return record.reason == CLIContract.AppStoreSearchFailure.offline
            }
            return false
        }
        return offline ? AppStoreSearchOffline() : nil
    }
}

/// Apple is limiting this Mac's searches: Search's rate-limited state.
///
/// Thrown in place of the CLI's own error, as `AppStoreSearchOffline` is.
/// `message` is the CLI's own sentence (`itunes.RATE_LIMITED_MESSAGE`, pinned
/// by tests/test_itunes_search.py).
struct AppStoreSearchRateLimited: LocalizedError, Equatable {
    var errorDescription: String? { Self.message }

    static let message = "Apple is limiting searches from this Mac for a minute. Try again shortly."
}

/// The failures Search shows as states of their own, read off a failed run's
/// `appstore-search-failed`: offline, and rate-limited. nil for any other
/// reason, whose stderr stands.
enum AppStoreSearchRefusal {
    static func error(in events: [BuildEvent]) -> Error? {
        for event in events {
            guard case let .appStoreSearchFailed(record) = event else { continue }
            switch record.reason {
            case CLIContract.AppStoreSearchFailure.offline: return AppStoreSearchOffline()
            case CLIContract.AppStoreSearchFailure.rateLimited: return AppStoreSearchRateLimited()
            default: return nil
            }
        }
        return nil
    }
}

extension CLIRunner.Refusal {
    /// `AppStoreSearchOffline.refusal(in:)`, as `streamEvents` takes it.
    static var appStoreSearchOffline: Self { Self(AppStoreSearchOffline.refusal(in:)) }
    /// `AppStoreSearchRefusal.error(in:)`: offline or rate-limited.
    static var appStoreSearch: Self { Self(AppStoreSearchRefusal.error(in:)) }
}

extension AppStoreStatusRecord {
    /// Whether a get can run now. All five must hold:
    /// - ipatool is present;
    /// - it is signed in;
    /// - it is not a known-bad version;
    /// - UnfairPlay is present;
    /// - UnfairPlay is enrolled.
    ///
    /// These are the tests Settings › App Store's two rows show as ready
    /// (`AppStoreSettingsSection`). Add App offers "Get it after adding" only
    /// when this holds.
    var canGet: Bool {
        ipatool.present && ipatool.signedIn == true && ipatool.warning == nil
            && unfairplay.present && unfairplay.enrolled
    }
}

// MARK: - The App Store account (sub-project 4)

/// `appstore-login`: one step of `--appstore-login`.
///
/// `state` is one of `CLIContract.AppStoreLogin`. `email` comes with
/// "signed-in"; `reason` and `message` with "failed", and `hint` with a
/// failed one that has a next step to suggest; `elapsed` (seconds) with
/// "waiting". There is no field for the password or the code: the CLI has
/// nothing of either to put on the wire.
struct AppStoreLoginRecord: Decodable, Equatable {
    let state: String
    var email: String? = nil
    var reason: String? = nil
    var message: String? = nil
    /// What to do about a failure, in the CLI's words (whole-diff review,
    /// signin-4): the sheet shows it under `message`. nil from a CLI too old
    /// to send it.
    var hint: String? = nil
    var elapsed: Int? = nil
}

/// `appstore-logout`.
struct AppStoreLogoutRecord: Decodable, Equatable {
    let signedOut: Bool

    enum CodingKeys: String, CodingKey {
        case signedOut = "signed_out"
    }
}

/// `appstore-enroll-candidate`: one installed iPhone or iPad app UnfairPlay
/// can enroll this Mac with.
struct EnrollCandidateRecord: Decodable, Equatable, Identifiable {
    let path: String
    let name: String
    let bundleId: String
    /// Launched at least once. UnfairPlay's README asks for that, and an app
    /// never opened may not have the state enrollment reads.
    let opened: Bool

    var id: String { path }

    enum CodingKeys: String, CodingKey {
        case path, name, opened
        case bundleId = "bundle_id"
    }
}

/// `appstore-enroll-candidates-end`.
struct EnrollCandidatesEndRecord: Decodable, Equatable {
    let count: Int
}

/// `appstore-enroll-command`: the `/bin/sh` script to run as root, exactly as
/// the CLI built it (`graft_ipa/enrollment.py` `root_script`). The app runs it
/// behind the administrator prompt (`AdminShell`) and adds nothing. Root works
/// only in a folder it makes itself, and the script's output is that folder,
/// which `--appstore-enroll-finish` then empties into `modelDir` as the user.
///
/// The fields after `verified` came with the review-part-4 fixes; each is
/// optional, so an event from a CLI without them still decodes.
struct EnrollCommandRecord: Decodable, Equatable {
    let app: String
    /// The user's own model folder. The script never names it.
    let modelDir: String
    let script: String
    /// Whether the script checks its copy of UnfairPlay against Graft.app's
    /// own team signature before root runs it.
    ///
    /// False when nothing vouches for that UnfairPlay: this Graft.app has no
    /// team signature to check it against (`unverifiedKind` "ad-hoc"), or the
    /// CLI isn't Graft.app's own, so the UnfairPlay is someone's own build
    /// ("not-bundled"). The CLI hands over such a script only when asked with
    /// `--allow-unverified-helper`, and `AppStoreAccountStore.confirmEnrol`
    /// runs one only after the sheet's explicit "Run Unchecked Copy as Root…".
    let verified: Bool
    /// The UnfairPlay the script copies and root runs.
    var helper: String? = nil
    /// With `verified` false: `CLIContract.UnverifiedHelperKind`.
    var unverifiedKind: String? = nil
    /// With `verified` false: the CLI's sentence saying why nothing checks it.
    var unverifiedReason: String? = nil
    /// Creating this file stops the root run, before it starts, during macOS's
    /// password dialog, or while it runs (exit `EnrolExit.stopped`). The CLI
    /// never creates it; the app removes it once the run is over.
    var cancelFile: String? = nil
    /// The script stops itself after this long (exit `EnrolExit.timedOut`).
    var timeLimitSeconds: Int? = nil

    enum CodingKeys: String, CodingKey {
        case app, script, verified, helper
        case modelDir = "model_dir"
        case unverifiedKind = "unverified_kind"
        case unverifiedReason = "unverified_reason"
        case cancelFile = "cancel_file"
        case timeLimitSeconds = "time_limit_seconds"
    }
}

/// `--appstore-enroll-command` refused to hand over a script whose UnfairPlay
/// nothing would check before root runs it (`appstore-refused {reason:
/// "unverified-helper"}`). Thrown in place of the CLI's stderr, so the enroll
/// sheet can show it as a warning of its own, outside the script's
/// disclosure, with the explicit second step beside it.
struct UncheckedHelper: LocalizedError, Equatable {
    /// `CLIContract.UnverifiedHelperKind`, or a word this app doesn't know.
    let kind: String
    /// The UnfairPlay root would run.
    let helper: String
    /// The CLI's own sentence.
    let message: String

    var errorDescription: String? { message }

    static func refusal(in events: [BuildEvent]) -> UncheckedHelper? {
        for case let .appStoreRefused(record) in events.reversed()
        where record.reason == CLIContract.AppStoreRefusal.unverifiedHelper {
            return UncheckedHelper(
                kind: record.kind ?? "",
                helper: record.helper ?? "",
                message: record.message ?? "Nothing checks the copy of UnfairPlay this setup runs as root."
            )
        }
        return nil
    }
}

extension UncheckedHelper {
    /// The same warning, for a script the CLI did hand over unchecked: from
    /// the fields of its `appstore-enroll-command`.
    init(_ command: EnrollCommandRecord) {
        self.init(
            kind: command.unverifiedKind ?? "",
            helper: command.helper ?? "",
            message: command.unverifiedReason ?? "Nothing checks the copy of UnfairPlay this setup runs as root."
        )
    }
}

extension CLIRunner.Refusal {
    static var uncheckedHelper: Self { Self(UncheckedHelper.refusal(in:)) }
}

/// `appstore-enrolled`: `--appstore-enroll-finish` moved the model into the
/// user's model folder.
struct EnrolledRecord: Decodable, Equatable {
    let modelDir: String
    let count: Int

    enum CodingKeys: String, CodingKey {
        case count
        case modelDir = "model_dir"
    }
}

/// `appstore-claimed`.
struct AppStoreClaimedRecord: Decodable, Equatable {
    let app: String
    let bundleId: String
    let alreadyOwned: Bool

    enum CodingKeys: String, CodingKey {
        case app
        case bundleId = "bundle_id"
        case alreadyOwned = "already_owned"
    }
}

/// A sign-in the CLI reported as failed, in its own plain sentence
/// (`appstore-login {state: "failed", message}`). Thrown in place of the CLI's
/// stderr, which repeats the sentence with a Terminal hint.
struct SignInFailed: LocalizedError, Equatable {
    let reason: String
    let message: String
    /// The event's `hint`, which the sheet shows under the message.
    var hint: String? = nil

    var errorDescription: String? { message }

    /// The CLI's own sentence, for any `reason`, a new one (`ipatool-exited`,
    /// a sign-in Graft couldn't record) included: this app never puts a
    /// sentence of its own in front of it. A failed event with no message is
    /// no refusal, so the CLI's stderr stands rather than a generic line.
    static func refusal(in events: [BuildEvent]) -> SignInFailed? {
        for case let .appStoreLogin(record) in events.reversed()
        where record.state == CLIContract.AppStoreLogin.failed {
            guard let message = record.message else { return nil }
            return SignInFailed(reason: record.reason ?? "other", message: message, hint: record.hint)
        }
        return nil
    }
}

extension CLIRunner.Refusal {
    static var signInFailed: Self { Self(SignInFailed.refusal(in:)) }
}

/// A get the CLI refused because the Apple ID doesn't own the app
/// (`appstore-refused {reason: "needs-license"}`). The App Store row offers
/// Get (free) for a price of 0, and Open in App Store otherwise.
struct NeedsLicense: LocalizedError, Equatable {
    let price: Double?
    let formattedPrice: String?
    let trackId: Int?

    static let freeMessage = "This Apple ID doesn't have this app yet. It's free: Get (free) adds it to your purchases, then Graft downloads it."

    var errorDescription: String? {
        if price == 0 { return Self.freeMessage }
        if let formattedPrice {
            return "This Apple ID hasn't bought this app (\(formattedPrice)). Buy it in the App Store with the same Apple ID, then get it here."
        }
        return "This Apple ID doesn't own this app yet. Get it in the App Store with the same Apple ID, then get it here."
    }

    static func refusal(in events: [BuildEvent]) -> NeedsLicense? {
        for case let .appStoreRefused(record) in events.reversed()
        where record.reason == CLIContract.AppStoreRefusal.needsLicense {
            return NeedsLicense(price: record.price, formattedPrice: record.formattedPrice, trackId: record.trackId)
        }
        return nil
    }
}

extension NeedsLicense {
    /// The get after a claim that said the Apple ID already owns the app,
    /// refused for want of a license all the same (review part 4, #9).
    /// Another Get (free) can't fix that, so the row offers Open in App Store.
    static let ownedButRefusedMessage = "Apple reports that this Apple ID already owns this app, but it still won't hand over a download. Try installing it once on an iPhone or iPad signed in to the same Apple ID, or check this Apple ID's purchases in the App Store, then get it here again."

    /// The get after a claim that said it worked (not already owned),
    /// refused for want of a license all the same (fix-check #14). Apple took
    /// the request and hasn't added the app: with Family Sharing's Ask to Buy
    /// on, a parent approves it first. Another Get (free) would only ask
    /// again, so the row offers Open in App Store.
    static let requestedButRefusedMessage = "Apple accepted the request, but this app isn't on this Apple ID yet. If Ask to Buy is on for this Apple ID, a parent may need to approve it first. Once it's in this Apple ID's purchases, get it here again."
}

/// `--appstore-claim` refused because the app isn't free
/// (`appstore-refused {reason: "paid", price, formatted_price, track_id}`):
/// the price changed since the get's refusal, or the CLI's lookup sees it
/// paid. The row answers with Open in App Store, never another Get (free).
struct ClaimRefusedPaid: LocalizedError, Equatable {
    let price: Double?
    let formattedPrice: String?
    let trackId: Int?

    var errorDescription: String? {
        let cost = formattedPrice ?? price.map { String(format: "%.2f", $0) }
        return "This app isn't free" + (cost.map { " (\($0))" } ?? "")
            + ", and Graft gets only free apps. Buy it in the App Store with the same Apple ID, then get it here."
    }

    static func refusal(in events: [BuildEvent]) -> ClaimRefusedPaid? {
        for case let .appStoreRefused(record) in events.reversed()
        where record.reason == CLIContract.AppStoreRefusal.paid {
            return ClaimRefusedPaid(price: record.price, formattedPrice: record.formattedPrice, trackId: record.trackId)
        }
        return nil
    }
}

/// `--appstore-claim --expect-email` refused because ipatool is signed in
/// as another Apple ID than the confirmation named
/// (`appstore-refused {reason: "account-changed", email}`): the sign-in
/// changed in Terminal while the confirmation was open. Nothing was bought.
/// Its words are Get (free)'s own for a switch
/// (`AppStoreStore.ClaimRefusal.switched`).
struct ClaimAccountChanged: LocalizedError, Equatable {
    /// The Apple ID signed in now, when the CLI named one.
    let email: String?

    var errorDescription: String? { AppStoreStore.ClaimRefusal.switched(to: email).message }

    static func refusal(in events: [BuildEvent]) -> ClaimAccountChanged? {
        for case let .appStoreRefused(record) in events.reversed()
        where record.reason == CLIContract.AppStoreRefusal.accountChanged {
            return ClaimAccountChanged(email: record.email)
        }
        return nil
    }
}

extension CLIRunner.Refusal {
    /// What Get (free) reads off a failed claim.
    static var appStoreClaim: Self {
        Self { events in
            if let changed = ClaimAccountChanged.refusal(in: events) { return changed }
            return ClaimRefusedPaid.refusal(in: events)
        }
    }

    /// The two refusals a get answers with a button: "Use it anyway…", and Get (free) / Open in App Store.
    static var appStoreGet: Self {
        Self { events in
            if let unverified = UnverifiedDecrypt.refusal(in: events) { return unverified }
            return NeedsLicense.refusal(in: events)
        }
    }
}
