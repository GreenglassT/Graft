import Foundation
import Combine

private typealias Flag = CLIContract.Flag

/// Getting an app from the App Store and decrypting it on this Mac — and,
/// since both write the same cache file, every other way an app's source IPA
/// gets staged from the app pane, and the IPA Add App is given with a new app
/// (`AddAppSheet.add`).
///
/// The CLI does the work — ipatool downloads the App Store's encrypted IPA,
/// UnfairPlay decrypts it offline, and the result is staged like any drop (see
/// `graft_ipa/appstore.py`). This store runs the verbs and holds what they
/// said: whether this Mac is set up (`status`), how each app's App Store
/// version compares with its cached IPA (`versions`), and each app's in-flight
/// get or drop (`activity`, `stages`) and its outcome (`errors`,
/// `completions`).
///
/// **Why the in-flight work lives here and not in the app pane.** The pane is
/// one view reused across sidebar clicks, and a get takes minutes. Its
/// outcome used to be routed by comparing `app.name` read off the pane's own
/// struct copy — always the app the get started for — so a get for A that
/// finished while B was shown wrote A's error into B's banner. Keyed by app
/// here, the pane only *reads* the entry for the app it shows, so there is no
/// comparison left to get wrong; and the Task handle survives the pane, which
/// is what lets Stop reach it.
///
/// **The account lives next door.** Signing in, signing out and enrolling are
/// `account`'s (`AppStoreAccountStore`): the Apple ID, the password and the
/// 2FA code go to the CLI's stdin and are wiped once sent, and enrollment runs
/// as root only behind macOS's administrator prompt.
///
/// One instance, owned by `ContentView` and injected into the app pane and
/// Settings, for the reason `DevicesStore` is: a view that builds its own
/// copy buys a second subprocess and a load gate that can latch.
@MainActor
final class AppStoreStore: ObservableObject {
    let runner: CLIRunner
    /// Add App's Search (`AppStoreSearchStore`). It lives here so the sheet,
    /// which is rebuilt each time it opens, needs no store of its own. The
    /// sheet resets it when it opens and when it closes.
    let search: AppStoreSearchStore
    /// Signing in and out, and enrolling (`AppStoreAccountStore`). Reads
    /// status again through `loadStatus` after each.
    let account: AppStoreAccountStore
    /// Get an older version…'s list (`AppStoreVersionsStore`). Here for
    /// Search's reason: the sheet is rebuilt each time it opens. It loads
    /// this when it opens and resets it when it closes.
    let olderVersions: AppStoreVersionsStore
    /// `account`'s changes, sent on as this store's own. Views read it through
    /// this store (`appStore.account.signIn`), and so does this store's
    /// `getBlockedReason`, which the app pane reads: a view observing only
    /// this store still redraws when the account changes. It changes when
    /// the user signs in or out, or enrolls.
    ///
    /// `search` and `olderVersions` aren't sent on. One changes with every
    /// keystroke in Add App's search, the other with every older version the
    /// list reads, and every view observing this store would redraw for
    /// each. The one view that reads each observes it itself
    /// (`AppStoreSearchForm`, `AppStoreVersionsSheet`), and
    /// `ObservableStoresTests` keeps any other from reading them through here.
    /// Unpublished: set once, in `init`.
    private var accountChanges: AnyCancellable?

    /// The last `--appstore-status` answer, or nil before one arrives or after
    /// one fails. A failed reload clears it: a "signed in" left over from
    /// before the failure would be a claim nothing currently supports.
    @Published private(set) var status: AppStoreStatusRecord?
    /// Whether a status load has *finished*, successfully or not.
    ///
    /// Not `status != nil`: that cannot tell "never asked" from "asked and it
    /// failed", and the Settings rows need to say "Checking…" for one and the
    /// error for the other. See memory `empty-collection-is-not-a-load-state`.
    /// A *canceled* load does not set it — see `loadStatus`.
    @Published private(set) var hasLoadedStatus = false
    @Published private(set) var isLoadingStatus = false
    /// Status reads started, and running now; and which one's answer is the
    /// one in `status` (see `loadStatus`).
    /// Unpublished: bookkeeping, which nothing draws.
    private var statusReadsStarted = 0
    /// Unpublished: bookkeeping, which nothing draws.
    private var statusReadsRunning = 0
    /// Unpublished: bookkeeping, which nothing draws.
    private var statusReadRecorded = 0
    @Published private(set) var statusError: String?

    /// The App Store's version against the cached one, by config app name.
    /// A check that could not run at all is still an entry, with `error` set,
    /// so a row can say "couldn't check" rather than nothing.
    @Published private(set) var versions: [String: AppStoreVersionRecord] = [:]
    /// What the pane knew about an app's cache when it asked: the version pin
    /// and the cached version it is showing. An answer is only reused for the
    /// same context — a pin edit, or a staged IPA deleted in the Cache pane,
    /// changes what "cached" means, and a reused answer would name the old
    /// file.
    struct VersionContext: Equatable {
        let pin: String
        let cachedVersion: String?
    }

    /// When each *successful* answer in `versions` arrived, and for which
    /// context. See `checkVersion`.
    /// Unpublished: bookkeeping, which nothing draws.
    private var checkedAt: [String: (context: VersionContext?, at: Date)] = [:]
    /// How long a version answer is reused for an app the user clicks back to.
    static let versionFreshFor: TimeInterval = 10 * 60

    enum Activity: Equatable {
        case getting
        case providing
        /// Get (free): the $0 purchase before the get.
        case claiming
    }

    /// What is writing each app's source IPA right now. At most one per app:
    /// a drop and a get of the same version would stage the same cache name.
    @Published private(set) var activity: [String: Activity] = [:]
    /// Where each app's running get or drop has got to, as the CLI reports it.
    @Published private(set) var stages: [String: AppStoreStage] = [:]
    /// The last get or drop failure per app, until dismissed or retried.
    @Published private(set) var errors: [String: String] = [:]

    /// What "Use it anyway…" would re-run, per app: set only when the CLI
    /// refused the last get or drop because UnfairPlay couldn't verify the
    /// decrypt (C-UNVERIFIED), and cleared with that app's error. Kept beside
    /// the error rather than inside it, so every other failure stays the plain
    /// sentence it was.
    enum Retry: Equatable {
        case get
        /// A Get Again (`--again`): re-run as one, or it would only find the
        /// copy already staged and download nothing.
        case getAgain
        /// A get of the version this id names (`--version-id`), from Get an
        /// older version….
        case version(String)
        case drop(URL)
    }

    @Published private(set) var unverifiedRetry: [String: Retry] = [:]
    /// How many gets or drops have staged an IPA for each app. The pane watches
    /// its own app's count to re-read the cached-IPA card.
    @Published private(set) var completions: [String: Int] = [:]

    struct Completion: Equatable {
        let app: String
        let serial: Int
    }

    /// The most recent completion for any app — what `ContentView` watches to
    /// refresh that app's artwork, whichever pane is showing.
    @Published private(set) var lastCompleted: Completion?
    /// The most recent get that changed an app's hold (`appstore-hold`): a
    /// get of a chosen version, or a get of the latest that let go. The hold
    /// is the app's `preferred_version`, so `ContentView` reads the app list
    /// again when this changes.
    @Published private(set) var lastHoldChange: Completion?

    /// What the last finished get or drop staged, per app — the line the app
    /// pane shows under the Source IPA section until the next action.
    ///
    /// The pane used to return to exactly the state it was in before a drop,
    /// so one that worked looked like one that did nothing. Cleared when the
    /// next get or drop for the app starts. A build is the third "next
    /// action", and it is not cleared here: builds start from two buttons
    /// (Build & Install, Build for this Mac) and only one of them used to
    /// remember to clear it. The pane compares `Staged.at` with
    /// `ConfigStore.buildStartedAt` instead, which every build sets on its way
    /// through the one build slot — `AppDetailView.stagedConfirmation`.
    struct Staged: Equatable {
        enum Source: Equatable {
            case appStore
            /// The file the user dropped or chose, by name.
            case drop(filename: String)
        }

        let source: Source
        let version: String
        /// The CLI decrypted it on the way in. Always so for a get; for a drop,
        /// only when the IPA came straight from the App Store encrypted.
        let decrypted: Bool
        /// What the CLI warned about on the way in — after "Use it anyway…",
        /// that the decrypt was never verified. Shown under the line, so the
        /// warning outlives the confirmation that caused it.
        var warnings: [String] = []
        /// When it landed, against `ConfigStore.buildStartedAt`: a build that
        /// started after this is the next action, and the line is old news.
        var at: ContinuousClock.Instant = .now
        /// Nothing landed: the get found the App Store's current version
        /// already staged and downloaded nothing (`appstore-already-staged`).
        var alreadyStaged = false
        /// A get of a version picked in Get an older version…, which holds
        /// the app at it.
        var chosen = false

        var message: String {
            switch source {
            case .appStore where chosen:
                return alreadyStaged
                    ? "✓ Already have \(version): nothing downloaded. Builds use it until you get the latest."
                    : "✓ Staged \(version) from the App Store. Builds use it until you get the latest."
            case .appStore:
                return alreadyStaged
                    ? "✓ Already have \(version), the App Store's current version: nothing downloaded"
                    : "✓ Staged \(version) from the App Store"
            case let .drop(filename):
                return decrypted
                    ? "✓ Decrypted and staged \(version) from \(filename)"
                    : "✓ Staged \(version) from \(filename)"
            }
        }
    }

    @Published private(set) var staged: [String: Staged] = [:]
    /// Unpublished: bookkeeping, which nothing draws.
    private var serial = 0
    /// Unpublished: bookkeeping, which nothing draws.
    private var tasks: [String: Task<Void, Never>] = [:]

    private let now: () -> Date
    /// Holds sudden and automatic termination off while a get, drop or
    /// claim runs (whole-diff review, app-3).
    private let hold: TerminationHold

    /// `now` exists for the freshness window's test; production passes nothing.
    /// `shell` is the account store's administrator shell, and has no default:
    /// `GraftApp` passes the real one, and every test a fake, so a forgotten
    /// one is a compile error (`NoRealDefaultsTests`; the real shells also
    /// refuse in a test host, `AdminShell.insideTests`). Last, so a call that
    /// passes `now` reads `runner, now, shell`.
    /// `hold` is the account store's too, for enrollment, and so is
    /// `openApp` (`AppStoreAccountStore.openEnrolApp`): `GraftApp` passes
    /// NSWorkspace's, and the default opens nothing.
    init(
        runner: CLIRunner, now: @escaping () -> Date = Date.init, shell: PrivilegedShell,
        hold: TerminationHold = .shared, openApp: @escaping @MainActor (URL) -> Void = { _ in }
    ) {
        self.runner = runner
        self.now = now
        self.hold = hold
        self.search = AppStoreSearchStore(runner: runner)
        self.olderVersions = AppStoreVersionsStore(runner: runner)
        self.account = AppStoreAccountStore(runner: runner, shell: shell, hold: hold, openApp: openApp)
        account.onChange = { [weak self] in await self?.loadStatus() }
        account.onAccountChange = { [weak self] in self?.forgetLicences() }
        account.blockedBy = { [weak self] in self?.accountBlockedReason }
        accountChanges = account.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }
    }

    // MARK: Account actions against gets (the review's critic)

    static let getRunningReason = "Wait for the App Store get to finish, or stop it, first."
    static let accountBusyReason = "Wait for the App Store sign-in, sign-out or setup in Settings to finish."

    /// Why Sign In, Sign Out and Set Up Decryption must wait: a get or Get
    /// (free) is running, and ipatool is reading the sign-in under it.
    var accountBlockedReason: String? {
        activity.values.contains { $0 == .getting || $0 == .claiming } ? Self.getRunningReason : nil
    }

    /// Why Get latest and Get (free) must wait: a sign-in, sign-out or
    /// enrollment is changing what they would use.
    var getBlockedReason: String? { account.isBusy ? Self.accountBusyReason : nil }

    static func statusArgs() -> [String] { [Flag.json, Flag.appstoreStatus] }
    /// `expectEmail` is the Apple ID the confirmation named: the CLI then
    /// refuses to buy for any other (`ClaimAccountChanged`).
    static func claimArgs(app: String, expectEmail: String? = nil) -> [String] {
        [Flag.json, Flag.appstoreClaim, Flag.app, app] + (expectEmail.map { [Flag.expectEmail, $0] } ?? [])
    }
    static func checkArgs(app: String) -> [String] { [Flag.json, Flag.appstoreCheck, Flag.app, app] }
    static func getArgs(
        app: String, allowUnverified: Bool = false, again: Bool = false, versionId: String? = nil
    ) -> [String] {
        [Flag.json, Flag.appstoreGet, Flag.app, app]
            + (versionId.map { [Flag.versionId, $0] } ?? [])
            + (again ? [Flag.again] : [])
            + (allowUnverified ? [Flag.allowUnverified] : [])
    }

    // MARK: Status

    /// Read `--appstore-status`.
    ///
    /// **A canceled load changes nothing but `isLoadingStatus`.** Settings
    /// starts this from a `.task`, which SwiftUI cancels when the pane goes
    /// away; the runner then kills the CLI, and that arrives here as an
    /// ordinary non-zero exit (`runOnce` never checks cancellation). Recording
    /// it would latch a failure nobody had — `hasLoadedStatus` true with an
    /// error — and the `.task` gate would never load again.
    ///
    /// **Reads can overlap** (Settings' Refresh, the sign-in sheet's own
    /// read, a reload after a sign-in), and one that started earlier can
    /// finish later. Its older answer is dropped rather than put over a newer
    /// one: a second of a stale "signed in" before the legacy state was the
    /// owner's try12 report. `isLoadingStatus` holds while any read runs.
    func loadStatus() async {
        statusReadsStarted += 1
        let started = statusReadsStarted
        statusReadsRunning += 1
        isLoadingStatus = true
        let record: AppStoreStatusRecord?
        let failure: String?
        do {
            record = try await runner.run(Self.statusArgs(), failing: "App Store status") { event in
                if case let .appStoreStatus(record) = event { return record }
                return nil
            }
            failure = record == nil ? "graft reported no App Store status" : nil
        } catch {
            record = nil
            failure = error.localizedDescription
        }
        statusReadsRunning -= 1
        isLoadingStatus = statusReadsRunning > 0
        guard !Task.isCancelled, started > statusReadRecorded else { return }
        statusReadRecorded = started
        status = record
        statusError = failure
        hasLoadedStatus = true
        if let record {
            let now = Self.account(of: record)
            if let lastAccount, Self.isSwitch(from: lastAccount, to: now) { forgetLicences() }
            lastAccount = Self.remembered(lastAccount, now)
        }
    }

    // MARK: Sign In… (fix-check #10)

    /// Whether status has been read since Sign In… last opened. Until it
    /// has, the sheet can't say which ipatool gets the password.
    @Published private(set) var hasCheckedForSignIn = false

    /// Sign In…'s first step, as the sheet opens: read status again, so the
    /// "goes to another ipatool" note describes the ipatool `--appstore-login`
    /// will run now, not the one a status read earlier in the session named
    /// (config.yaml can change under the app). A canceled read (the sheet
    /// closed) leaves the sheet unchecked.
    func checkBeforeSignIn() async {
        hasCheckedForSignIn = false
        await loadStatus()
        guard !Task.isCancelled else { return }
        hasCheckedForSignIn = true
    }

    /// The ipatool the sign-in sheet describes, and the only one its Sign In
    /// sends the password to: from a status read since the sheet opened, or
    /// a newer one (a sign-in reads status again after any outcome). nil
    /// before that read, and when it failed.
    var signInIpatool: AppStoreStatusRecord.IPATool? {
        hasCheckedForSignIn ? status?.ipatool : nil
    }

    /// The sheet's Sign In: `account.startSignIn` with `--expect-ipatool` set
    /// to `signInIpatool`'s path, so the CLI refuses ("ipatool-changed")
    /// rather than type the password into an ipatool the sheet didn't name.
    /// False, with the password wiped, before the sheet's status read.
    @discardableResult
    func startSignIn(email: String, password: inout [UInt8]) -> Bool {
        guard let ipatool = signInIpatool else {
            Secret.wipe(&password)
            return false
        }
        return account.startSignIn(email: email, password: &password, expectedIpatool: ipatool.path)
    }

    /// Who status says is signed in, as far as a license answer depends on it.
    struct SignedInAccount: Equatable {
        let signedIn: Bool?
        let email: String?
        let legacy: Bool?
    }

    static func account(of status: AppStoreStatusRecord) -> SignedInAccount {
        SignedInAccount(
            signedIn: status.ipatool.signedIn, email: status.ipatool.email, legacy: status.ipatool.legacySignin
        )
    }

    /// Whether status now names another Apple ID than before: signed in,
    /// then signed out; one email, then another; or Graft 1.x's sign-in,
    /// then Graft's own. Not a change to or from an answer status couldn't
    /// give: a login keychain that doesn't answer reads as signed in "nil"
    /// with no email, and a flap of that would otherwise clear every claim's
    /// banner and offer (the fix-check's critic).
    nonisolated static func isSwitch(from old: SignedInAccount, to new: SignedInAccount) -> Bool {
        if old.signedIn == true, new.signedIn == false { return true }
        if let was = old.email, let now = new.email, was != now { return true }
        return old.legacy == true && new.legacy == false && new.signedIn == true
    }

    /// What the next status is compared with: this one's answers, and the
    /// last answer for whatever this one couldn't say, so Apple ID A, then no
    /// answer, then B is still a switch. Signed out has no email.
    nonisolated static func remembered(_ old: SignedInAccount?, _ new: SignedInAccount) -> SignedInAccount {
        SignedInAccount(
            signedIn: new.signedIn ?? old?.signedIn,
            email: new.signedIn == false ? nil : new.email ?? old?.email,
            legacy: new.legacy ?? old?.legacy
        )
    }

    /// The last successful statuses' account (`remembered`), kept across a
    /// failed reload so a switch seen after one is still a switch.
    /// Unpublished: bookkeeping, which nothing draws.
    private var lastAccount: SignedInAccount?

    /// The Apple ID changed: a sign-in, a sign-out, or status naming another
    /// account. Every license answer and Get (free) offer was about the one
    /// before, and so is the banner that came with each (review part 4, #8).
    /// A claim can't be running: account actions wait for gets and claims.
    func forgetLicences() {
        for app in licenseNeeded.keys where activity[app] == nil {
            errors[app] = nil
            licenseNeeded[app] = nil
            claimConfirmations[app] = nil
        }
    }

    // MARK: Version check

    /// Compare `app`'s App Store version with its cached IPA.
    ///
    /// Reused for ten minutes unless `force`: the app pane asks on every
    /// selection, and each ask is a subprocess plus an iTunes round trip. Only
    /// a successful answer is reused — "couldn't check" is asked again next
    /// time. A get or drop that stages a file forces a fresh one, and so does
    /// the row's refresh button.
    ///
    /// Canceled — an app switch cancels the pane's `.task` — it records
    /// nothing, for `loadStatus`'s reason: a killed CLI is not "couldn't check".
    func checkVersion(app: String, context: VersionContext? = nil, force: Bool = false) async {
        if !force, let last = checkedAt[app], last.context == context,
           now().timeIntervalSince(last.at) < Self.versionFreshFor {
            return
        }
        let answer: AppStoreVersionRecord
        do {
            let record = try await runner.run(Self.checkArgs(app: app), failing: "App Store check") { event in
                if case let .appStoreVersion(record) = event { return record }
                return nil
            }
            answer = record ?? Self.couldNotTell(app, "graft reported no version")
        } catch {
            answer = Self.couldNotTell(app, error.localizedDescription)
        }
        guard !Task.isCancelled else { return }
        versions[app] = answer
        checkedAt[app] = answer.storeVersion != nil ? (context, now()) : nil
    }

    private static func couldNotTell(_ app: String, _ why: String) -> AppStoreVersionRecord {
        AppStoreVersionRecord(
            app: app, bundleId: "", storeVersion: nil, cachedVersion: nil,
            updateAvailable: false, error: why
        )
    }

    // MARK: In-flight work

    func isBusy(_ app: String) -> Bool { activity[app] != nil }
    func isGetting(_ app: String) -> Bool { activity[app] == .getting }

    func dismissError(app: String) {
        errors[app] = nil
        unverifiedRetry[app] = nil
        licenseNeeded[app] = nil
        claimConfirmations[app] = nil
    }

    // MARK: A license the Apple ID doesn't have

    /// What a get refused for want of a license said, per app: the button the
    /// row shows instead of Get latest. Cleared by the next get, by a get or
    /// drop that stages, and with the app's error.
    struct LicenseNeed: Equatable {
        let price: Double?
        let formattedPrice: String?
        let trackId: Int?
        /// Whether Get (free) is still the answer. After a claim was tried and
        /// didn't get the app, it isn't: the row offers Open in App Store.
        var claim: Claim = .offer
        /// The refused get was a Get Again, so the get after a claim is one
        /// too: a plain get would find the old copy staged and stop there.
        var again = false
        /// The refused get was of a version picked in Get an older version…
        /// (`--version-id`), so the get after a claim is of that version too:
        /// a plain get would fetch the latest and let go of the hold.
        var versionId: String? = nil

        enum Claim: Equatable {
            /// Not tried yet.
            case offer
            /// The CLI refused the claim as paid (`ClaimRefusedPaid`).
            case refusedPaid
            /// The claim failed some other way. That includes ipatool's own
            /// "paid apps" refusal (the Apple ID's storefront prices it),
            /// which reaches the app only as stderr, with no event: this side
            /// can't tell it from a dropped connection without matching
            /// words, so no failed claim is offered again from here.
            /// Dismissing the banner starts over from Get latest.
            case failed
            /// The claim said the Apple ID already owns it, and the get still
            /// wanted a license (`NeedsLicense.ownedButRefusedMessage`).
            case alreadyOwned
            /// The claim said it worked, not already owned, and the get still
            /// wanted a license: Apple took the request and hasn't added the
            /// app, as with Ask to Buy waiting for a parent
            /// (`NeedsLicense.requestedButRefusedMessage`).
            case requested
        }

        var isFree: Bool { price == 0 }
        /// Get (free): free, and no claim has been tried and failed.
        var offersClaim: Bool { isFree && claim == .offer }
        var storeURL: URL? { trackId.flatMap { URL(string: "https://apps.apple.com/app/id\($0)") } }
    }

    @Published private(set) var licenseNeeded: [String: LicenseNeed] = [:]

    func isClaiming(_ app: String) -> Bool { activity[app] == .claiming }

    nonisolated static let signedOutReason = "Sign in to the App Store in Settings › App Store first."

    /// Why Get (free) can't run now, or nil. A claim adds the app to the
    /// signed-in Apple ID's purchases, so not while status says nobody is
    /// signed in, or only Graft 1.x's ipatool is. Status not read yet is no
    /// reason: the CLI checks again.
    var claimBlockedReason: String? {
        if let getBlockedReason { return getBlockedReason }
        guard let ipa = status?.ipatool else { return nil }
        return ipa.signedIn == false || ipa.legacySignin == true ? Self.signedOutReason : nil
    }

    /// The confirmation Get (free) asks first: one click is a permanent
    /// entry in an Apple ID's purchase history.
    nonisolated static func claimConfirmation(app: String, email: String?) -> (title: String, message: String) {
        let whose = email.map { "the Apple ID \($0)" } ?? "this Apple ID"
        return (
            "Get \(app) for free?",
            "Graft adds \(app) to \(whose)'s purchases, at no cost, as Get in the App Store would. "
                + "That's permanent: it stays in the purchase history. Then Graft downloads it."
        )
    }

    // MARK: Who Get (free) buys for (whole-diff review, get-4)

    /// What an app's Get (free) confirmation names: the Apple ID a status read
    /// made as the button was pressed said is signed in. ipatool claims for
    /// whoever is signed in when it runs, so the email shown is that read's,
    /// never one cached from earlier in the session, and the claim passes it
    /// as `--expect-email`: an Apple ID changed in Terminal while the dialog
    /// was open is refused by the CLI rather than given a purchase the dialog
    /// put another name on (the try14 delta audit's D14-APP-1 and F5).
    struct ClaimConfirmation: Equatable {
        /// nil when status says signed in but `auth info` gave no email:
        /// the dialog then says "this Apple ID".
        let email: String?
    }

    /// Apps whose Get (free) is reading status before it asks.
    @Published private(set) var checkingClaim: Set<String> = []
    /// Each app's status read for Get (free), by number, until it ends or is
    /// abandoned (`cancelClaim`). A read whose number is gone asks nothing.
    /// Unpublished: bookkeeping, which nothing draws.
    private var claimChecks: [String: Int] = [:]
    /// Unpublished: bookkeeping, which nothing draws.
    private var claimCheckSerial = 0
    /// Each app's confirmation, from that read, until it is answered.
    @Published private(set) var claimConfirmations: [String: ClaimConfirmation] = [:]

    static let checkingClaimLine = "Checking which Apple ID is signed in…"

    /// Why Get (free) doesn't claim, said in the app's banner instead.
    enum ClaimRefusal: Equatable {
        /// The status read failed: nobody can say who would get the app.
        case couldNotCheck(String)
        /// Nobody is signed in, or only Graft 1.x's ipatool is.
        case signedOut
        /// Status couldn't tell (a locked keychain, `auth info` silent), with
        /// the CLI's sentence for why when it sent one.
        case couldNotTell(String?)
        /// Another Apple ID than the one the license answer or the
        /// confirmation was about, with its email when status has one. Its
        /// license answer is dropped, as `forgetLicences` drops them.
        case switched(to: String?)

        var message: String {
            switch self {
            case let .couldNotCheck(why):
                return "Not claimed: Graft couldn't check which Apple ID is signed in to the App Store (\(why)). "
                    + "Press Get (free) to try again."
            case .signedOut:
                return "Not claimed: no Apple ID is signed in to the App Store. " + AppStoreStore.signedOutReason
            case let .couldNotTell(why):
                return "Not claimed: Graft couldn't tell which Apple ID is signed in to the App Store, "
                    + "so it can't say whose purchases the app would go to."
                    + (why.map { " " + $0 } ?? " Press Get (free) to try again.")
            case let .switched(email):
                return "Not claimed: the Apple ID signed in to the App Store changed"
                    + (email.map { " (it's now \($0))" } ?? "")
                    + ". Press Get latest to check this app for it."
            }
        }
    }

    /// Whether status lets Get (free) claim, and if not, why. `switched` is
    /// whether it names another Apple ID than the claim is meant for.
    nonisolated static func claimRefusal(
        status: AppStoreStatusRecord?, statusError: String?, switched: Bool
    ) -> ClaimRefusal? {
        guard let ipa = status?.ipatool else { return .couldNotCheck(statusError ?? "no App Store status") }
        if ipa.signedIn == false || ipa.legacySignin == true { return .signedOut }
        guard ipa.signedIn == true else {
            return .couldNotTell(ipa.unknownMessage.flatMap { $0.isEmpty ? nil : $0 })
        }
        return switched ? .switched(to: ipa.email) : nil
    }

    /// Say `refusal` in `app`'s banner; a switch also drops the license
    /// answer, which was about the other Apple ID.
    private func refuseClaim(app: String, _ refusal: ClaimRefusal) {
        errors[app] = refusal.message
        if case .switched = refusal { licenseNeeded[app] = nil }
    }

    /// Get (free)'s first step, as the button is pressed: read status again,
    /// then ask, naming the Apple ID that read found
    /// (`claimConfirmations[app]`). A read that failed, nobody signed in, an
    /// Apple ID status can't name, or one other than the last status named
    /// (changed in Terminal since) is refused in words, and nothing is asked.
    ///
    /// A read the pane gave up on (`cancelClaim`, when it moves to another
    /// app) asks nothing when it ends: its dialog would otherwise open by
    /// itself when the user came back to the app, maybe hours later, naming
    /// that read's Apple ID (the try14 delta audit's D14-APP-1).
    func checkBeforeClaim(app: String) async {
        guard activity[app] == nil, licenseNeeded[app]?.offersClaim == true, claimBlockedReason == nil,
              !checkingClaim.contains(app) else { return }
        claimCheckSerial += 1
        let mine = claimCheckSerial
        claimChecks[app] = mine
        checkingClaim.insert(app)
        claimConfirmations[app] = nil
        let before = lastAccount
        await loadStatus()
        guard claimChecks[app] == mine else { return }
        claimChecks[app] = nil
        checkingClaim.remove(app)
        // A drop started meanwhile: nothing to ask.
        guard activity[app] == nil else { return }
        let switched = before.flatMap { old in status.map { Self.isSwitch(from: old, to: Self.account(of: $0)) } } ?? false
        // The offer is gone, and not because this read saw a switch (which
        // `forgetLicences` answers by dropping it, and which is said): a
        // drop finished and staged meanwhile, or the banner was closed.
        // Nothing to ask, and no "Not claimed" over a staged IPA either.
        guard licenseNeeded[app]?.offersClaim == true || switched else { return }
        if let refusal = Self.claimRefusal(status: status, statusError: statusError, switched: switched) {
            refuseClaim(app: app, refusal)
            return
        }
        guard licenseNeeded[app]?.offersClaim == true else { return }
        claimConfirmations[app] = ClaimConfirmation(email: status?.ipatool.email)
    }

    /// The confirmation's Cancel, or the dialog closing, or the pane moving
    /// to another app: no dialog, and a status read still running for it
    /// asks nothing when it ends.
    func cancelClaim(app: String) {
        claimConfirmations[app] = nil
        if claimChecks.removeValue(forKey: app) != nil { checkingClaim.remove(app) }
    }

    /// Get (free), confirmed: the $0 purchase, then, once it worked, the get
    /// itself. The CLI refuses anything but a free app; this asks only for
    /// one the get's refusal said was free, and that no claim has failed for
    /// since, and only while status still names the Apple ID `confirmed`
    /// named: one changed since is refused in words. Status can be minutes
    /// old by the time the dialog is answered, so the CLI checks again as it
    /// runs, against `confirmed.email` (`--expect-email`).
    @discardableResult
    func startClaim(app: String, confirmed: ClaimConfirmation) -> Bool {
        claimConfirmations[app] = nil
        guard activity[app] == nil else { return false }
        if let refusal = Self.claimRefusal(
            status: status, statusError: statusError, switched: status?.ipatool.email != confirmed.email
        ) {
            refuseClaim(app: app, refusal)
            return false
        }
        guard licenseNeeded[app]?.offersClaim == true else { return false }
        if let claimBlockedReason {
            // Confirmed, but a sign-in or another action started meanwhile:
            // said, not silently dropped.
            errors[app] = claimBlockedReason
            return false
        }
        activity[app] = .claiming
        errors[app] = nil
        tasks[app] = Task { [weak self] in await self?.runClaim(app: app, expectEmail: confirmed.email) }
        return true
    }

    private func runClaim(app: String, expectEmail: String?) async {
        var failure: Error?
        var alreadyOwned = false
        let ticket = hold.begin(TerminationHold.claiming) { [weak self] in self?.stopGet(app: app) }
        do {
            let events = try await runner.streamEvents(
                Self.claimArgs(app: app, expectEmail: expectEmail), failing: "Get (free)", refusal: .appStoreClaim
            )
            alreadyOwned = events.contains { event in
                if case let .appStoreClaimed(record) = event { return record.alreadyOwned }
                return false
            }
        } catch {
            failure = error
        }
        hold.end(ticket)
        if failure is ClaimAccountChanged, !Task.isCancelled {
            // The CLI saw another Apple ID than the dialog named. Read status
            // now, while the row still says claiming, so Settings and the
            // next Get (free) know who is signed in; `forgetLicences` then
            // leaves this app alone, and the banner below is the one shown.
            await loadStatus()
        }
        let cancelled = Task.isCancelled
        activity[app] = nil
        tasks[app] = nil
        guard !cancelled else { return }
        if let changed = failure as? ClaimAccountChanged {
            errors[app] = changed.localizedDescription
            // Its license answer was about the other Apple ID, as with a
            // switch status saw (`refuseClaim`): back to Get latest.
            licenseNeeded[app] = nil
            return
        }
        if let failure {
            errors[app] = failure.localizedDescription
            let earlier = licenseNeeded[app]
            if let paid = failure as? ClaimRefusedPaid {
                licenseNeeded[app] = LicenseNeed(
                    price: paid.price, formattedPrice: paid.formattedPrice,
                    trackId: paid.trackId ?? earlier?.trackId, claim: .refusedPaid,
                    again: earlier?.again ?? false, versionId: earlier?.versionId
                )
            } else {
                licenseNeeded[app] = LicenseNeed(
                    price: earlier?.price, formattedPrice: earlier?.formattedPrice,
                    trackId: earlier?.trackId, claim: .failed, again: earlier?.again ?? false,
                    versionId: earlier?.versionId
                )
            }
            return
        }
        let again = licenseNeeded[app]?.again ?? false
        let versionId = licenseNeeded[app]?.versionId
        licenseNeeded[app] = nil
        beginGet(
            app: app, allowUnverified: false, again: again, versionId: versionId,
            afterClaim: alreadyOwned ? .alreadyOwned : .claimed
        )
    }

    /// What the claim before a get said, which decides what a needs-license
    /// refusal of that get means.
    enum AfterClaim: Equatable {
        case claimed
        case alreadyOwned
    }

    /// Start a get for `app`, owned by this store. False when something is
    /// already writing that app's source IPA.
    ///
    /// `allowUnverified` only ever comes from `useUnverified`: a first try
    /// never stages a decrypt nobody could verify. `again` is Get Again: the
    /// CLI downloads even when the App Store's current version is already
    /// staged, where a plain get stops and says so
    /// (`appstore-already-staged`).
    ///
    /// `versionId` is a version picked in Get an older version…: the CLI gets
    /// that one instead of the latest and holds the app at it
    /// (`appstore-hold`). Without one, a get that works lets go of any hold.
    @discardableResult
    func startGet(
        app: String, allowUnverified: Bool = false, again: Bool = false, versionId: String? = nil
    ) -> Bool {
        beginGet(app: app, allowUnverified: allowUnverified, again: again, versionId: versionId, afterClaim: nil)
    }

    @discardableResult
    private func beginGet(
        app: String, allowUnverified: Bool, again: Bool, versionId: String?, afterClaim: AfterClaim?
    ) -> Bool {
        guard activity[app] == nil else { return false }
        if let getBlockedReason {
            // Not silent: Add App's "Get it after adding" lands here too.
            errors[app] = getBlockedReason
            return false
        }
        activity[app] = .getting
        errors[app] = nil
        unverifiedRetry[app] = nil
        licenseNeeded[app] = nil
        stages[app] = nil
        staged[app] = nil
        let retry: Retry = versionId.map { .version($0) } ?? (again ? .getAgain : .get)
        tasks[app] = Task { [weak self] in
            await self?.run(
                app: app, source: .appStore, retry: retry, afterClaim: afterClaim
            ) {
                try await $0.streamGet(app: app, allowUnverified: allowUnverified, again: again, versionId: versionId)
            }
        }
        return true
    }

    /// "Use it anyway…", confirmed: re-run the get or drop the CLI just
    /// refused as unverified, with `--allow-unverified`. False when there is
    /// no such refusal to answer, or something is already writing the app's
    /// source IPA. The CLI's warning comes back on `staged[app].warnings`.
    @discardableResult
    func useUnverified(app: String, config: ConfigStore) -> Bool {
        guard activity[app] == nil, let retry = unverifiedRetry[app] else { return false }
        switch retry {
        case .get:
            return startGet(app: app, allowUnverified: true)
        case .getAgain:
            return startGet(app: app, allowUnverified: true, again: true)
        case let .version(id):
            return startGet(app: app, allowUnverified: true, versionId: id)
        case let .drop(fileURL):
            return startProvide(app: app, fileURL: fileURL, config: config, allowUnverified: true)
        }
    }

    /// Stop `app`'s running get, or its Get (free). Canceling the Task makes
    /// the runner SIGTERM the CLI, whose scoped handler deletes the encrypted
    /// download on the way out (`appstore._stop_cleans_up`). Not reported as
    /// an error: the user asked for it. A stopped claim leaves Get (free) on
    /// offer, as it was.
    func stopGet(app: String) {
        guard activity[app] == .getting || activity[app] == .claiming else { return }
        tasks[app]?.cancel()
    }

    /// Stage a dropped IPA for `app`, owned by this store — the CLI decrypts
    /// it first if it came straight from the App Store. False when something
    /// is already writing that app's source IPA.
    ///
    /// A refusal is not silent: the drop zone accepted the file, so the user
    /// is told why nothing happened, in the same banner a failure uses.
    @discardableResult
    func startProvide(
        app: String, fileURL: URL, config: ConfigStore, allowUnverified: Bool = false
    ) -> Bool {
        if let busy = activity[app] {
            errors[app] = Self.busyRefusal(busy)
            return false
        }
        activity[app] = .providing
        errors[app] = nil
        unverifiedRetry[app] = nil
        staged[app] = nil
        tasks[app] = Task { [weak self] in
            await self?.run(
                app: app, source: .drop(filename: fileURL.lastPathComponent),
                retry: .drop(fileURL)
            ) { store in
                try await config.provideIPA(
                    appName: app, fileURL: fileURL, allowUnverified: allowUnverified
                ) { [weak store] stage in
                    store?.noteStage(stage, for: app)
                }.map(Landed.provided)
            }
        }
        return true
    }

    /// Why a drop was refused, in words true of what is running (review part
    /// 4, #11: a claim stages nothing, so "another IPA" was false).
    static func busyRefusal(_ busy: Activity) -> String {
        switch busy {
        case .getting:
            return "Not staged: an App Store get is running for this app. "
                + "Wait for it, or press Stop, then drop the IPA again."
        case .claiming:
            return "Not staged: Graft is getting this app for your Apple ID (Get (free)). "
                + "Wait for it, or press Stop, then drop the IPA again."
        case .providing:
            return "Not staged: another IPA is still being staged for this app."
        }
    }

    /// The tail every get and drop shares. The slot is released *before* the
    /// follow-up version check, so a second operation can start the moment the
    /// first has staged — and nothing after that point writes `activity`, so a
    /// new operation's claim cannot be clobbered by this one finishing.
    ///
    /// `retry` is what "Use it anyway…" re-runs, recorded only when the
    /// failure is the CLI's unverified refusal (`UnverifiedDecrypt`).
    private func run(
        app: String, source: Staged.Source, retry: Retry, afterClaim: AfterClaim? = nil,
        _ work: (AppStoreStore) async throws -> Landed?
    ) async {
        var failure: Error?
        var landed: Landed?
        // Not under the version check at the end: by then it has staged.
        // Quit Anyway stops a get as its Stop does. A drop has no Stop, so its
        // task is canceled, which is all that Stop does: the runner SIGTERMs
        // the CLI either way.
        let ticket: TerminationHold.Ticket
        if source == .appStore {
            ticket = hold.begin(TerminationHold.gettingFromAppStore) { [weak self] in self?.stopGet(app: app) }
        } else {
            ticket = hold.begin(TerminationHold.staging) { [weak self] in self?.tasks[app]?.cancel() }
        }
        do {
            landed = try await work(self)
        } catch {
            failure = error
        }
        hold.end(ticket)
        let cancelled = Task.isCancelled
        activity[app] = nil
        stages[app] = nil
        tasks[app] = nil
        if let failure {
            guard !cancelled else { return }
            errors[app] = failure.localizedDescription
            if failure is UnverifiedDecrypt { unverifiedRetry[app] = retry }
            if let need = failure as? NeedsLicense {
                // After a claim, "It's free: Get (free)" again would be a loop:
                // owned, by Apple's own account, and still no license (review
                // part 4, #9); or a claim Apple accepted that hasn't added the
                // app, as when Ask to Buy waits for a parent, where each
                // confirm could send another request (fix-check #14).
                let claim: LicenseNeed.Claim
                switch afterClaim {
                case .alreadyOwned?:
                    claim = .alreadyOwned
                    errors[app] = NeedsLicense.ownedButRefusedMessage
                case .claimed?:
                    claim = .requested
                    errors[app] = NeedsLicense.requestedButRefusedMessage
                case nil:
                    claim = .offer
                }
                let versionId: String?
                if case let .version(id) = retry { versionId = id } else { versionId = nil }
                licenseNeeded[app] = LicenseNeed(
                    price: need.price, formattedPrice: need.formattedPrice, trackId: need.trackId, claim: claim,
                    again: retry == .getAgain, versionId: versionId
                )
            }
            return
        }
        guard !cancelled else { return }
        // Staged: whatever a license refusal said, the cache now has a copy,
        // so the row goes back to Get latest. A drop is the one way here with
        // a refusal still standing (whole-diff review, get-1): a get clears
        // it as it starts, but a drop doesn't, and the row stayed on Get
        // (free) or Open in App Store, with no banner left to dismiss.
        licenseNeeded[app] = nil
        let chosen: Bool
        if case .version = retry { chosen = true } else { chosen = false }
        switch landed {
        case let .alreadyStaged(record)?:
            // Nothing was staged, so no completion: the cache is as it was.
            // The version line is asked again, as the get's lookup just was.
            staged[app] = Staged(
                source: source, version: record.version, decrypted: false, alreadyStaged: true, chosen: chosen
            )
            await checkVersion(app: app, force: true)
            return
        case let .provided(provided)?:
            staged[app] = Staged(
                source: source, version: provided.record.version,
                decrypted: provided.decrypted, warnings: provided.warnings, chosen: chosen
            )
        case nil:
            break
        }
        serial += 1
        completions[app, default: 0] += 1
        lastCompleted = Completion(app: app, serial: serial)
        await checkVersion(app: app, force: true)
    }

    /// Download, decrypt and stage `app`'s latest App Store version — the
    /// work `startGet` runs in its slot, never called outside one. Throws the
    /// CLI's own message and hint, which is where "tap Get on a device first"
    /// and the login command come from.
    ///
    /// An unverified decrypt throws `UnverifiedDecrypt` instead, whose words are
    /// the app's: the CLI's hint for it is a Terminal command.
    ///
    /// A get of the version already staged downloads nothing and says so
    /// (`appstore-already-staged`), unless `again`.
    private func streamGet(
        app: String, allowUnverified: Bool, again: Bool, versionId: String?
    ) async throws -> Landed? {
        let events = try await runner.streamEvents(
            Self.getArgs(app: app, allowUnverified: allowUnverified, again: again, versionId: versionId),
            failing: "Get from App Store", refusal: .appStoreGet
        ) { [weak self] event in
            self?.note(event, forGetOf: app)
        }
        if Self.holdChanged(in: events) {
            serial += 1
            lastHoldChange = Completion(app: app, serial: serial)
        }
        return Self.landed(in: events)
    }

    /// Whether a get's events say it changed the app's hold.
    static func holdChanged(in events: [BuildEvent]) -> Bool {
        events.contains { event in
            if case .appStoreHold = event { return true }
            return false
        }
    }

    /// What a get or drop's work came back with: the IPA it staged, or, for a
    /// get, the one already staged that it left alone.
    enum Landed: Equatable {
        case provided(ConfigStore.ProvidedIPA)
        case alreadyStaged(AppStoreAlreadyStagedRecord)
    }

    /// A get's outcome in its events: `ipa-provided`, else
    /// `appstore-already-staged`, else nothing to say.
    static func landed(in events: [BuildEvent]) -> Landed? {
        if let provided = ConfigStore.provided(in: events) { return .provided(provided) }
        for case let .appStoreAlreadyStaged(record) in events { return .alreadyStaged(record) }
        return nil
    }

    /// Apply one streamed event to `app`'s progress — only while its work runs.
    ///
    /// `streamEvents` delivers on MainActor hops that can land after the call
    /// has returned and cleared the row; without this guard a late "decrypt"
    /// would leave "Decrypting…" on a finished row.
    func note(_ event: BuildEvent, forGetOf app: String) {
        guard case let .appStoreStage(record) = event else { return }
        noteStage(record.stage, for: app)
    }

    private func noteStage(_ stage: AppStoreStage, for app: String) {
        guard activity[app] != nil else { return }
        stages[app] = stage
    }
}
