import Foundation
import Combine

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
final class LiveContainerStore: ObservableObject {
    @Published private(set) var status: [String: LCStatusRecord] = [:]
    /// Devices a load has *finished* for, whatever it found — but not one a
    /// canceled or superseded load was for: that load found nothing out.
    @Published private(set) var hasLoaded: Set<String> = []
    @Published private(set) var loading: Set<String> = []
    @Published var loadErrors: [String: String] = [:]
    /// The newest load per device. A load publishes only if it is still the
    /// newest when it returns, so A→B→A quickly runs the second A load rather
    /// than dropping it behind the first one's in-flight guard.
    /// Unpublished: bookkeeping, which nothing draws.
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
        /// `--lc-install` reported at least one step, so it got past its
        /// checks (Set Up's first step) to the download. Set from the run's
        /// own event list when it ends, which holds every event however late
        /// its callback lands, and live from `apply`.
        var progressed = false
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
    @Published private(set) var run: Run?
    /// Unpublished: bookkeeping, which nothing draws.
    private var actionTask: Task<Void, Never>?

    /// Set Up LiveContainer's run on one device (spec §5c): three steps, two
    /// CLI runs. `--lc-install` is the first two, its checks and then the
    /// download, signing and install; `--lc-push-cert` is the third. Kept
    /// after it ends so the sheet can say what's left; replaced by the next,
    /// and ended by a one-step Install, Put certificate or Clean up.
    struct SetUp: Equatable {
        enum Step: CaseIterable, Hashable {
            /// `--lc-install` up to its first progress event: the .p12, App
            /// Store Connect and the registered devices (read again when this
            /// device is new to them, Plan E), the device's apps, AltStore.
            case check
            /// The rest of `--lc-install`: download, sign, install.
            case install
            /// `--lc-push-cert`.
            case pushCert
        }

        enum State: Equatable {
            case waiting, running, done
            /// The CLI's message.
            case failed(String)
            /// Stop, while this step ran or was next.
            case stopped
            /// After a step that failed or stopped.
            case notRun
        }

        let udid: String
        /// What it installed, and from which channel: Set Up again with other
        /// choices has something new to install, so it runs in full.
        let instances: Set<Int>
        let channel: String?
        /// It put the certificate on and nothing more, after an earlier Set
        /// Up's push failed (`resumesAtCertificate`).
        var resumed = false
        var states: [Step: State] = [.check: .waiting, .install: .waiting, .pushCert: .waiting]
        var finished = false

        /// The steps not done, in order.
        var left: [Step] { Step.allCases.filter { states[$0] != .done } }

        var stopped: Bool { states.values.contains(.stopped) }

        /// It installed and then didn't put the certificate on: Set Up again
        /// can put the certificate on and nothing more
        /// (`LiveContainerStore.resumesAtCertificate`, which checks the rest).
        /// Not once a resumed push has failed too: the install may be what's
        /// wrong, so the next one starts over rather than skip it every time.
        var resumesAtCertificate: Bool {
            guard finished, !resumed, states[.install] == .done else { return false }
            switch states[.pushCert] {
            case .failed?, .stopped?: return true
            default: return false
            }
        }
    }

    @Published private(set) var setUp: SetUp?

    /// Each device's name, from the views that show its LiveContainer
    /// (`remember`), so "Busy with LiveContainer on …" names the device
    /// rather than its UDID. A run only starts from one of those views.
    @Published private(set) var names: [String: String] = [:]

    func remember(_ device: DeviceRecord) {
        if names[device.udid] != device.name { names[device.udid] = device.name }
    }

    /// `udid`'s name, or the UDID itself when no view has shown that device.
    func name(of udid: String) -> String { names[udid] ?? udid }

    /// The current or last run, if it was for `udid`.
    func run(for udid: String) -> Run? { run?.udid == udid ? run : nil }

    var isRunning: Bool { run.map { !$0.finished } ?? false }

    /// A run in progress, or Set Up between two of its runs: the buttons
    /// that start one wait.
    var isBusy: Bool { isRunning || setUp.map { !$0.finished } ?? false }

    /// The device a run, or a Set Up between its runs, is in progress on, if
    /// it is not `udid`.
    func busyElsewhere(than udid: String) -> String? {
        if let run, !run.finished, run.udid != udid { return run.udid }
        if let setUp, !setUp.finished, setUp.udid != udid { return setUp.udid }
        return nil
    }

    /// Whether Set Up on `udid` with these choices puts the certificate on
    /// and nothing more: the last run was this device's Set Up's push, which
    /// failed or was stopped after it installed (`SetUp.resumesAtCertificate`),
    /// with the same LiveContainers and channel, and the primary it installed
    /// is still there to take the certificate. Anything else runs in full.
    func resumesAtCertificate(udid: String, instances: Set<Int>, channel: String?) -> Bool {
        guard let setUp, setUp.udid == udid, setUp.resumesAtCertificate,
              setUp.instances == instances, setUp.channel == channel,
              let run, run.udid == udid, run.action == .pushCert, run.error != nil
        else { return false }
        return status[udid]?.hasPrimary == true
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

    /// Read LiveContainer again on each device a load has finished for that
    /// is in `connected`: after an update, which may have pushed to it
    /// (review 23-2, item 7). Each asks the device, so one that isn't
    /// connected is left as it was read, not marked unreachable; its page
    /// reads it again when it's back. One at a time, in UDID order.
    func reload(connected: Set<String>) async {
        for udid in hasLoaded.intersection(connected).sorted() {
            await load(udid: udid)
        }
    }

    // MARK: actions — started and stopped from the view

    // Install, Put certificate and Clean up, one step at a time, end Set Up:
    // what it says is left is no longer so. A removal (the sheet's tick, the
    // page's Remove it) is the step after Set Up's, and leaves it.
    func startInstall(udid: String, instances: Set<Int>, channel: String?) {
        launch {
            $0.setUp = nil
            await $0.install(udid: udid, instances: instances, channel: channel)
        }
    }

    func startPushCert(udid: String) {
        launch {
            $0.setUp = nil
            await $0.pushCert(udid: udid)
        }
    }

    func startRemoveCert(udid: String) { launch { await $0.removeCert(udid: udid) } }

    func startClean(udid: String) {
        launch {
            $0.setUp = nil
            await $0.clean(udid: udid)
        }
    }

    func startSetUp(udid: String, instances: Set<Int>, channel: String?) {
        launch { await $0.runSetUp(udid: udid, instances: instances, channel: channel) }
    }

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
    /// Unpublished: bookkeeping, which nothing draws.
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
        await reread(udid)
    }

    func pushCert(udid: String, argsOverride: [String]? = nil) async {
        await perform(.pushCert, udid: udid,
                      args: argsOverride ?? Self.pushCertArgs(udid: udid),
                      failing: "Put the certificate on the device")
        await reread(udid)
    }

    func removeCert(udid: String, argsOverride: [String]? = nil) async {
        await perform(.removeCert, udid: udid,
                      args: argsOverride ?? Self.removeCertArgs(udid: udid),
                      failing: "Remove the certificate from the device")
        await reread(udid)
    }

    func clean(udid: String, argsOverride: [String]? = nil) async {
        await perform(.clean, udid: udid,
                      args: argsOverride ?? Self.cleanArgs(udid: udid),
                      failing: "Clean up LiveContainer")
        await reread(udid)
    }

    /// After every run, success or not, the device's status again. A partial
    /// install (instance 1 in, instance 2 failed or stopped) changed the
    /// device too, and the certificate's reminder (`certificatePushed`)
    /// follows a push, a removal or a clean up at once. In its own task so a
    /// Stop's cancellation does not cancel the re-read with it.
    private func reread(_ udid: String) async {
        await Task { await self.load(udid: udid) }.value
    }

    /// Set Up (spec §5c) for one device: `--lc-install`, whose checks come
    /// first (Plan E: it reads the registered devices again when this one is
    /// new to them, and stops when it isn't registered), then `--lc-push-cert`.
    /// A failure stops there: the step that ran takes the error, the rest are
    /// not run. A Stop between the two runs stops the push before it starts:
    /// a canceled task never launches the CLI (`CLIRunner.launch`).
    ///
    /// After a Set Up that installed and then didn't put the certificate on,
    /// the next one on that device, with the same choices, puts the
    /// certificate on only (`resumesAtCertificate`).
    ///
    /// The quit is held from start to end, not only while each run goes
    /// (`perform`): ⌘Q during the status read between them quit without
    /// asking, and left LiveContainer with no certificate (review 23-2,
    /// §11). Quit Anyway there stops Set Up before its push, as Stop does.
    func runSetUp(udid: String, instances: Set<Int>, channel: String?) async {
        let ticket = hold.begin(TerminationHold.liveContainer) { [weak self] in self?.stop() }
        defer { hold.end(ticket) }
        let resume = resumesAtCertificate(udid: udid, instances: instances, channel: channel)
        var fresh = SetUp(udid: udid, instances: instances, channel: channel)
        fresh.resumed = resume
        setUp = fresh
        if resume {
            setUp?.states[.check] = .done
            setUp?.states[.install] = .done
        } else {
            setUp?.states[.check] = .running
            await install(udid: udid, instances: instances, channel: channel)
            // Got to the download, or stopped before it: from the run's own
            // events (`Run.progressed`), not from callbacks still in flight.
            if endSetUpIfFailed(at: run?.progressed == true ? .install : .check) { return }
            setUp?.states[.check] = .done
            setUp?.states[.install] = .done
        }
        setUp?.states[.pushCert] = .running
        await pushCert(udid: udid)
        if endSetUpIfFailed(at: .pushCert) { return }
        setUp?.states[.pushCert] = .done
        setUp?.finished = true
    }

    /// After one of Set Up's runs: if it failed or was stopped, the steps
    /// before `step` are done, `step` failed (or stopped), the rest are not
    /// run, and Set Up ends. True when it ended.
    private func endSetUpIfFailed(at step: SetUp.Step) -> Bool {
        guard let error = run?.error, var current = setUp else { return false }
        let order = SetUp.Step.allCases
        let at = order.firstIndex(of: step) ?? 0
        for (index, each) in order.enumerated() {
            if index < at {
                current.states[each] = .done
            } else if index > at {
                current.states[each] = .notRun
            } else {
                current.states[each] = run?.stopped == true ? .stopped : .failed(error)
            }
        }
        current.finished = true
        setUp = current
        return true
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
            // From the run's own list, which holds every event however late
            // its callback lands (`CLIRunner.RunResult.events`): Set Up reads
            // it to tell its check from its install (`runSetUp`).
            if result.events.contains(where: { event -> Bool in
                if case .lcInstallProgress = event { return true }
                return false
            }) {
                run?.progressed = true
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
            run?.progressed = true
            // `--lc-install`'s first step event is its download: what it
            // checks before that (Set Up's first step) is done. Shown live;
            // `runSetUp` settles it from the run's own events when it ends.
            if var s = setUp, !s.finished, s.udid == current.udid, s.states[.check] == .running {
                s.states[.check] = .done
                s.states[.install] = .running
                setUp = s
            }
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
