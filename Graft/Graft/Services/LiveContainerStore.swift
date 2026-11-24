import Foundation
import Observation

private typealias Flag = CLIContract.Flag

/// LiveContainer itself, per device: what is installed, and the things the
/// device pane can do about it — install/update, put the signing certificate
/// on the device and take it off again, clean up afterward.
///
/// Everything it shows is the CLI's answer (`graft_ipa/lc_manage.py`): which
/// instances exist, who manages them, whether an update is available, and
/// above all whether installing is allowed at all. An AltStore-managed device
/// is the CLI's call, not this store's, and `canInstall` only mirrors it.
///
/// `hasLoaded` rather than `status[udid] == nil` for "have we looked": a
/// device that could not be read, or a load that failed outright, would
/// otherwise look like one never asked — the defect three stores here have
/// shipped (`empty-collection-is-not-a-load-state`).
@MainActor
@Observable
final class LiveContainerStore {
    private(set) var status: [String: LCStatusRecord] = [:]
    /// Devices a load has *finished* for, whatever it found — but not one a
    /// canceled or superseded load was for: that load found nothing out.
    private(set) var hasLoaded: Set<String> = []
    private(set) var loading: Set<String> = []
    var loadErrors: [String: String] = [:]
    /// The newest load per device. A load publishes only if it is still the
    /// newest when it returns, so A→B→A quickly runs the second A load rather
    /// than dropping it behind the first one's in-flight guard.
    private var loadGeneration: [String: Int] = [:]

    enum Action: Equatable {
        case install
        case pushCert
        case removeCert
        case clean
    }

    /// One install / push / clean, start to finish, for one device. Kept after
    /// it ends so the section can show what happened; replaced by the next.
    struct Run: Equatable {
        let udid: String
        let action: Action
        var finished = false
        /// Ended by Stop rather than by the CLI.
        var stopped = false
        var progress: [LCInstallProgressRecord] = []
        /// Set while the CLI says the install is blocked behind a running
        /// LiveContainer; cleared as soon as anything else happens.
        var waitingMessage: String?
        var notes: [String] = []
        var installed: [LCInstalledRecord] = []
        var pushed: LCCertPushedRecord?
        /// One record per LiveContainer instance `--lc-clean` cleaned, in the
        /// order it reported them: the primary first (the only one that ever
        /// holds the certificate), then LiveContainer2/3. A single record
        /// here used to be overwritten by each event, so the last instance's
        /// answer replaced the primary's.
        var cleaned: [LCCleanedRecord] = []
        var error: String?
    }

    /// One run at a time across devices: they share the signing keychain and
    /// the user's attention. A device that is not the one running says so
    /// (`busyElsewhere`) rather than greying out without a reason.
    private(set) var run: Run?
    private var actionTask: Task<Void, Never>?

    /// The current or last run, if it was for `udid`.
    func run(for udid: String) -> Run? { run?.udid == udid ? run : nil }

    var isRunning: Bool { run.map { !$0.finished } ?? false }

    /// The device a run is in progress on, if it is not `udid`.
    func busyElsewhere(than udid: String) -> String? {
        guard let run, !run.finished, run.udid != udid else { return nil }
        return run.udid
    }

    let runner: CLIRunner
    /// Holds sudden and automatic termination off while an install or
    /// another change to the device runs (whole-diff review, app-3).
    private let hold: TerminationHold

    init(runner: CLIRunner, hold: TerminationHold = .shared) {
        self.runner = runner
        self.hold = hold
    }

    /// Whether the Install / Update button belongs on screen for `udid`.
    /// False until the CLI has answered, false for a device it could not
    /// read, and false for an AltStore-managed one.
    func canInstall(udid: String) -> Bool {
        guard let s = status[udid], s.reachable else { return false }
        return s.managedBy != .altstore
    }

    // MARK: argv

    nonisolated static func statusArgs(udid: String) -> [String] {
        [Flag.json, Flag.lcStatus, Flag.device, udid]
    }

    /// `instances` is sorted so the argv is the same whatever order the
    /// toggles were flipped in; `channel` nil leaves the CLI's default.
    nonisolated static func installArgs(udid: String, instances: Set<Int>, channel: String?) -> [String] {
        var args = [Flag.json, Flag.lcInstall, Flag.device, udid]
        args += [Flag.instances, instances.sorted().map(String.init).joined(separator: ",")]
        if let channel { args += [Flag.channel, channel] }
        return args
    }

    nonisolated static func pushCertArgs(udid: String) -> [String] {
        [Flag.json, Flag.lcPushCert, Flag.device, udid]
    }

    nonisolated static func removeCertArgs(udid: String) -> [String] {
        [Flag.json, Flag.lcClean, Flag.certOnly, Flag.device, udid]
    }

    nonisolated static func cleanArgs(udid: String) -> [String] {
        [Flag.json, Flag.lcClean, Flag.device, udid]
    }

    // MARK: status

    func load(udid: String, argsOverride: [String]? = nil) async {
        let generation = (loadGeneration[udid] ?? 0) + 1
        loadGeneration[udid] = generation
        loading.insert(udid)
        let outcome: Result<LCStatusRecord?, Error>
        do {
            outcome = .success(try await runner.run(
                argsOverride ?? Self.statusArgs(udid: udid), failing: "Check LiveContainer"
            ) { event -> LCStatusRecord? in
                if case let .lcStatus(r) = event { return r }
                return nil
            })
        } catch {
            outcome = .failure(error)
        }
        // A newer load for this device owns the outcome now.
        guard loadGeneration[udid] == generation else { return }
        loading.remove(udid)
        // Canceled — the `.task(id:)` moved to another device. Not a failure:
        // the SIGTERMed CLI arrives as `CLIError.exited`, not as a
        // `CancellationError`, so only the task's own flag can tell, and
        // recording it would leave an error banner on a pane nobody had a
        // problem with. The same rule as `DeviceDeliveryStore.load`.
        guard !Task.isCancelled else { return }
        hasLoaded.insert(udid)
        switch outcome {
        case let .success(record):
            if let record { status[udid] = record }
            loadErrors[udid] = nil
        case let .failure(error):
            loadErrors[udid] = error.localizedDescription
        }
    }

    // MARK: actions — started and stopped from the view

    func startInstall(udid: String, instances: Set<Int>, channel: String?) {
        launch { await $0.install(udid: udid, instances: instances, channel: channel) }
    }

    func startPushCert(udid: String) { launch { await $0.pushCert(udid: udid) } }
    func startRemoveCert(udid: String) { launch { await $0.removeCert(udid: udid) } }
    func startClean(udid: String) { launch { await $0.clean(udid: udid) } }

    /// Cancel the running action. CLIRunner SIGTERMs the CLI, which the
    /// `--lc-*` verbs turn into a clean exit — the throwaway keychain comes
    /// off the search list on the way out. Without this a hung
    /// `ideviceinstaller` grayed out every LiveContainer button until the app
    /// was restarted.
    func stop() { actionTask?.cancel() }

    /// The running action's task, for a caller (the tests) that has to wait
    /// for it.
    var currentAction: Task<Void, Never>? { actionTask }

    /// True from `launch` until its task ends — set synchronously, unlike
    /// `run`, which `perform` sets only once the task has started. Two clicks
    /// in one main-actor turn would otherwise both see `isRunning` false, the
    /// second task would replace `actionTask` (and return at once, finding the
    /// first running), and Stop would cancel that one instead of the CLI.
    private var actionInFlight = false

    private func launch(_ work: @escaping @MainActor (LiveContainerStore) async -> Void) {
        guard !isRunning, !actionInFlight else { return }
        actionInFlight = true
        actionTask = Task { [weak self] in
            guard let self else { return }
            defer { self.actionInFlight = false }
            await work(self)
        }
    }

    func install(
        udid: String, instances: Set<Int>, channel: String?, argsOverride: [String]? = nil
    ) async {
        await perform(
            .install, udid: udid,
            args: argsOverride ?? Self.installArgs(udid: udid, instances: instances, channel: channel),
            failing: "Install LiveContainer"
        )
        // After every run, success or not: a partial install (instance 1 in,
        // instance 2 failed or stopped) changed the device too. In its own
        // task so a Stop's cancellation does not cancel the re-read with it.
        await Task { await self.load(udid: udid) }.value
    }

    func pushCert(udid: String, argsOverride: [String]? = nil) async {
        await perform(.pushCert, udid: udid,
                      args: argsOverride ?? Self.pushCertArgs(udid: udid),
                      failing: "Put the certificate on the device")
    }

    func removeCert(udid: String, argsOverride: [String]? = nil) async {
        await perform(.removeCert, udid: udid,
                      args: argsOverride ?? Self.removeCertArgs(udid: udid),
                      failing: "Remove the certificate from the device")
    }

    func clean(udid: String, argsOverride: [String]? = nil) async {
        await perform(.clean, udid: udid,
                      args: argsOverride ?? Self.cleanArgs(udid: udid),
                      failing: "Clean up LiveContainer")
    }

    private func perform(_ action: Action, udid: String, args: [String], failing: String) async {
        guard !isRunning else { return }
        let ticket = hold.begin(TerminationHold.liveContainer) { [weak self] in self?.stop() }
        defer { hold.end(ticket) }
        run = Run(udid: udid, action: action)
        do {
            let result = try await runner.stream(arguments: args) { [weak self] event in
                self?.apply(event)
            }
            if result.exitCode != 0 {
                throw CLIError.exited(
                    doing: failing, code: result.exitCode,
                    stderr: result.stderr.isEmpty ? result.stdout : result.stderr
                )
            }
        } catch {
            if Task.isCancelled {
                run?.stopped = true
                run?.error = "Stopped."
            } else {
                run?.error = error.localizedDescription
            }
        }
        run?.waitingMessage = nil
        run?.finished = true
    }

    /// One streamed event into the current run. Internal for the tests.
    ///
    /// Ignored once the run has finished: the stream's trailing callbacks can
    /// land after `perform` returned, and a late `lc-install-waiting` would
    /// otherwise put "close LiveContainer on the device" back over a run that
    /// already ended.
    func apply(_ event: BuildEvent) {
        guard let current = run, !current.finished else { return }
        switch event {
        case let .lcInstallProgress(r):
            run?.waitingMessage = nil
            run?.progress.append(r)
        case let .lcInstallWaiting(r):
            run?.waitingMessage = r.message
        case let .lcInstalled(r):
            run?.waitingMessage = nil
            run?.installed.append(r)
        case let .lcCertPushed(r):
            run?.pushed = r
        case let .lcCleaned(r):
            run?.cleaned.append(r)
        case let .log(source, _) where source == CLIContract.startupLogSource:
            // About the machine, not this run (C-NOTICE).
            break
        case let .log(source, line) where source != "warning":
            run?.notes.append(line)
        case let .log(_, line):
            run?.notes.append("warning: " + line)
        default:
            break
        }
    }
}
