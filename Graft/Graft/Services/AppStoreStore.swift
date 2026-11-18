import Foundation
import Observation

private typealias Flag = CLIContract.Flag

/// Getting an app from the App Store and decrypting it on this Mac — and,
/// since both write the same cache file, every other way an app's source IPA
/// gets staged from the app pane.
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
/// **Credentials never pass through here.** Signing in to ipatool takes a
/// password and a 2FA code, and enrolling UnfairPlay takes root; both are
/// Terminal commands the CLI names in `status.setupCommands` and the Settings
/// pane offers for copying. Nothing in this app sends either.
///
/// One instance, owned by `ContentView` and injected into the app pane and
/// Settings, for the reason `DevicesStore` is: a view that builds its own
/// copy buys a second subprocess and a load gate that can latch.
@MainActor
@Observable
final class AppStoreStore {
    let runner: CLIRunner

    /// The last `--appstore-status` answer, or nil before one arrives or after
    /// one fails. A failed reload clears it: a "signed in" left over from
    /// before the failure would be a claim nothing currently supports.
    private(set) var status: AppStoreStatusRecord?
    /// Whether a status load has *finished*, successfully or not.
    ///
    /// Not `status != nil`: that cannot tell "never asked" from "asked and it
    /// failed", and the Settings rows need to say "Checking…" for one and the
    /// error for the other. See memory `empty-collection-is-not-a-load-state`.
    /// A *cancelled* load does not set it — see `loadStatus`.
    private(set) var hasLoadedStatus = false
    private(set) var isLoadingStatus = false
    private(set) var statusError: String?

    /// The App Store's version against the cached one, by config app name.
    /// A check that could not run at all is still an entry, with `error` set,
    /// so a row can say "couldn't check" rather than nothing.
    private(set) var versions: [String: AppStoreVersionRecord] = [:]
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
    private var checkedAt: [String: (context: VersionContext?, at: Date)] = [:]
    /// How long a version answer is reused for an app the user clicks back to.
    static let versionFreshFor: TimeInterval = 10 * 60

    enum Activity: Equatable {
        case getting
        case providing
    }

    /// What is writing each app's source IPA right now. At most one per app:
    /// a drop and a get of the same version would stage the same cache name.
    private(set) var activity: [String: Activity] = [:]
    /// Where each app's running get or drop has got to, as the CLI reports it.
    private(set) var stages: [String: AppStoreStage] = [:]
    /// The last get or drop failure per app, until dismissed or retried.
    private(set) var errors: [String: String] = [:]
    /// How many gets or drops have staged an IPA for each app. The pane watches
    /// its own app's count to re-read the cached-IPA card.
    private(set) var completions: [String: Int] = [:]

    struct Completion: Equatable {
        let app: String
        let serial: Int
    }

    /// The most recent completion for any app — what `ContentView` watches to
    /// refresh that app's artwork, whichever pane is showing.
    private(set) var lastCompleted: Completion?

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
        /// When it landed, against `ConfigStore.buildStartedAt`: a build that
        /// started after this is the next action, and the line is old news.
        var at: ContinuousClock.Instant = .now

        var message: String {
            switch source {
            case .appStore:
                return "✓ Staged \(version) from the App Store"
            case let .drop(filename):
                return decrypted
                    ? "✓ Decrypted and staged \(version) from \(filename)"
                    : "✓ Staged \(version) from \(filename)"
            }
        }
    }

    private(set) var staged: [String: Staged] = [:]
    private var serial = 0
    private var tasks: [String: Task<Void, Never>] = [:]

    private let now: () -> Date

    /// `now` exists for the freshness window's test; production passes nothing.
    init(runner: CLIRunner, now: @escaping () -> Date = Date.init) {
        self.runner = runner
        self.now = now
    }

    static func statusArgs() -> [String] { [Flag.json, Flag.appstoreStatus] }
    static func checkArgs(app: String) -> [String] { [Flag.json, Flag.appstoreCheck, Flag.app, app] }
    static func getArgs(app: String) -> [String] { [Flag.json, Flag.appstoreGet, Flag.app, app] }

    // MARK: Status

    /// Read `--appstore-status`.
    ///
    /// **A cancelled load changes nothing but `isLoadingStatus`.** Settings
    /// starts this from a `.task`, which SwiftUI cancels when the pane goes
    /// away; the runner then kills the CLI, and that arrives here as an
    /// ordinary non-zero exit (`runOnce` never checks cancellation). Recording
    /// it would latch a failure nobody had — `hasLoadedStatus` true with an
    /// error — and the `.task` gate would never load again.
    func loadStatus() async {
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
        isLoadingStatus = false
        guard !Task.isCancelled else { return }
        status = record
        statusError = failure
        hasLoadedStatus = true
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
    /// Cancelled — an app switch cancels the pane's `.task` — it records
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

    func dismissError(app: String) { errors[app] = nil }

    /// Start a get for `app`, owned by this store. False when something is
    /// already writing that app's source IPA.
    @discardableResult
    func startGet(app: String) -> Bool {
        guard activity[app] == nil else { return false }
        activity[app] = .getting
        errors[app] = nil
        stages[app] = nil
        staged[app] = nil
        tasks[app] = Task { [weak self] in
            await self?.run(app: app, source: .appStore) { try await $0.streamGet(app: app) }
        }
        return true
    }

    /// Stop `app`'s running get. Cancelling the Task makes the runner SIGTERM
    /// the CLI, whose scoped handler deletes the encrypted download on the way
    /// out (`appstore._stop_cleans_up`). Not reported as an error: the user
    /// asked for it.
    func stopGet(app: String) {
        guard activity[app] == .getting else { return }
        tasks[app]?.cancel()
    }

    /// Stage a dropped IPA for `app`, owned by this store — the CLI decrypts
    /// it first if it came straight from the App Store. False when something
    /// is already writing that app's source IPA.
    ///
    /// A refusal is not silent: the drop zone accepted the file, so the user
    /// is told why nothing happened, in the same banner a failure uses.
    @discardableResult
    func startProvide(app: String, fileURL: URL, config: ConfigStore) -> Bool {
        if let busy = activity[app] {
            errors[app] = busy == .getting
                ? "Not staged: an App Store get is running for this app. "
                    + "Wait for it, or press Stop, then drop the IPA again."
                : "Not staged: another IPA is still being staged for this app."
            return false
        }
        activity[app] = .providing
        errors[app] = nil
        staged[app] = nil
        tasks[app] = Task { [weak self] in
            await self?.run(app: app, source: .drop(filename: fileURL.lastPathComponent)) { store in
                try await config.provideIPA(appName: app, fileURL: fileURL) { [weak store] stage in
                    store?.noteStage(stage, for: app)
                }
            }
        }
        return true
    }

    /// The tail every get and drop shares. The slot is released *before* the
    /// follow-up version check, so a second operation can start the moment the
    /// first has staged — and nothing after that point writes `activity`, so a
    /// new operation's claim cannot be clobbered by this one finishing.
    private func run(
        app: String, source: Staged.Source,
        _ work: (AppStoreStore) async throws -> ConfigStore.ProvidedIPA?
    ) async {
        var failure: Error?
        var provided: ConfigStore.ProvidedIPA?
        do {
            provided = try await work(self)
        } catch {
            failure = error
        }
        let cancelled = Task.isCancelled
        activity[app] = nil
        stages[app] = nil
        tasks[app] = nil
        if let failure {
            if !cancelled { errors[app] = failure.localizedDescription }
            return
        }
        guard !cancelled else { return }
        if let provided {
            staged[app] = Staged(
                source: source, version: provided.record.version,
                decrypted: provided.decrypted
            )
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
    private func streamGet(app: String) async throws -> ConfigStore.ProvidedIPA? {
        let events = try await runner.streamEvents(
            Self.getArgs(app: app), failing: "Get from App Store"
        ) { [weak self] event in
            self?.note(event, forGetOf: app)
        }
        return ConfigStore.provided(in: events)
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
