import Foundation

private typealias Flag = CLIContract.Flag

/// One-click Update (spec §4): the daily check, the confirmation, the run.
///
/// The CLI does the work (`graft --update`, Plan A). This store runs it and
/// holds what the window shows: each app's plan from the last quick check
/// (`plans`: the sidebar's badges and the pane's line), the confirmation's
/// full plan (`confirmation`), and the running update's rows and transcript
/// (`progress`). `UpdateStore` is the self-updater's; this is the apps'.
///
/// **The daily check** is `--update --plan --quick --all`: at launch and once
/// a day while Graft is open, while Settings › Updates' "Check my apps for
/// updates" is on, which it is unless turned off. It sends the apps' bundle
/// ids to Apple's public lookup and their tweaks' package ids to canister.me,
/// and asks usbmux which phones are connected. It reads no keychain and opens
/// no session to the phone. It waits while a build or an update holds the
/// busy slot (`ConfigStore.busy`), and runs once the slot frees (`slotFreed`).
/// A check that finds nothing, or fails, shows nothing; the next one tries
/// again. After a get, a tweak update, a build or an update, the window asks
/// for the plan again (`recheck`), while the switch is on. With it off,
/// nothing is sent but by "Check for updates now" (spec §11.4).
///
/// **An update** claims the busy slot (`ConfigStore.beginUpdate`) and holds
/// the quit (`TerminationHold.updatingApps`), whose Quit Anyway is its Stop.
/// The Stop is a `RunStopper`: a SIGTERM, and no SIGKILL once the CLI says
/// it is finishing an install (`update-stopping`).
@MainActor
final class AppUpdatesStore: ObservableObject {
    /// The `UserDefaults` keys.
    enum Key {
        /// Settings › Updates' "Check my apps for updates".
        static let check = "appUpdates.check"
    }

    /// Which sheet is over the window: the confirmation, then the progress.
    enum Sheet: String, Identifiable {
        case confirm
        case progress
        /// One id for both (review S7): `.sheet(item:)` keeps the sheet up as
        /// the confirmation turns into the progress, rather than closing it
        /// and opening another.
        var id: String { "app-updates" }
    }

    /// The confirmation (spec 4.3), from its full plan.
    struct Confirmation: Equatable {
        let apps: [String]
        var plan: UpdatePlanRecord?
        var failure: String?
        /// "Also install … now" (`--with-affected`). Off by default.
        var alsoInstall = false
        /// Why Update didn't start just now: a build took the slot meanwhile.
        var refusal: String?

        var isLoading: Bool { plan == nil && failure == nil }
        /// The full plan found nothing to update: "Nothing left to update",
        /// and the button is Done.
        var isNothingLeft: Bool { plan.map { $0.apps.allSatisfy { !$0.hasUpdate } } ?? false }
        /// What Update runs: the apps the full plan says are ready.
        var runnable: [String] { plan?.apps.filter(\.isActionable).map(\.app) ?? [] }
        /// How many apps Update installs: the ready ones, and with "Also
        /// install" the others their updated tweaks affect (the button's
        /// count).
        var count: Int {
            let ready = runnable
            guard alsoInstall, let plan else { return ready.count }
            return ready.count + AppUpdateText.affected(plan).filter { !ready.contains($0) }.count
        }
    }

    static let noPlan = "graft gave no plan."

    /// A Stop updating the CLI refused ("example isn't on that device").
    struct DropFailure: Equatable {
        let app: String
        let udid: String
        let message: String
    }

    /// One app of a running update.
    struct Row: Equatable, Identifiable {
        let app: String
        let title: String
        var phase: UpdatePhase = .waiting
        var result: UpdateAppEndRecord?
        /// Why the plan says it can't run, if it can't.
        var blocked: String?
        /// The plan updates one of its tweaks.
        var updatesTweaks = false
        /// Each device it installs on, waits for or is blocked on (spec
        /// §11.3), in the plan's order.
        var targets: [TargetRow] = []
        var id: String { app }
    }

    /// One device's install, under its app.
    struct TargetRow: Equatable, Identifiable {
        let udid: String
        let deviceName: String?
        let liveContainer: Bool
        var phase: UpdatePhase = .waiting
        var result: UpdateAppEndRecord.Target?
        var id: String { udid }

        /// It didn't get its install, and could: Try Again is offered. One
        /// waiting for its device isn't; it updates when it's connected.
        var canTryAgain: Bool {
            switch phase {
            case .failed, .notStarted, .stopped, .blocked, .unfinished: return true
            default: return false
            }
        }

        /// The devices of `app`'s plan an update reaches: each it installs
        /// on, waits for or is blocked on (`AppUpdateText.targetLines`'s).
        static func rows(for app: UpdatePlanApp) -> [TargetRow] {
            app.targets.filter { $0.isReady ? $0.install : true }.map { target in
                var row = TargetRow(udid: target.udid, deviceName: target.deviceName,
                                    liveContainer: target.deliversToLiveContainer)
                if let waiting = target.waiting {
                    row.phase = .skipped(waiting)
                } else if let blocked = target.blockedReason {
                    row.phase = .blocked(blocked)
                }
                return row
            }
        }
    }

    /// The progress sheet (spec 4.4).
    struct Progress: Equatable {
        var rows: [Row]
        /// "Also install" was on: Try Again keeps it.
        var withAffected = false
        /// The step a Stop waits for (`update-stopping`).
        var stopping: UpdateStoppingRecord?
        var stopRequested = false
        /// "Updating tweaks" while the shared tweak step runs.
        var shared: String?
        var end: UpdateEndRecord?
        /// Why the run ended with no `update-end`.
        var failure: String?
        /// The app whose step started last.
        var current: String?
        /// The device whose install started last and hasn't ended: the one
        /// a quit waits for.
        var installing: String?
        /// When the CLI said it is finishing a step a Stop waits for, while
        /// it is (`update-stopping`, until the step ends or a second Stop).
        var heldSince: Date?
        /// The kill has been held `forceStopAfter`: the sheet offers Force
        /// Stop (spec §11.2).
        var offersForceStop = false
        /// Stop was pressed again: nothing is waited for any more.
        var stoppedAgain = false
        /// Force Stop was pressed.
        var forced = false

        /// Until the CLI has exited, after its `update-end` too: Done and Try
        /// Again wait for that.
        var isRunning = true
        /// What Try Again confirms: every app that didn't finish.
        var unfinished: [String] { rows.filter { $0.result?.ok != true }.map(\.app) }

        mutating func setPhase(_ app: String, _ phase: UpdatePhase) {
            if let i = rows.firstIndex(where: { $0.app == app }) {
                rows[i].phase = phase
            } else {
                rows.append(Row(app: app, title: app, phase: phase))
            }
        }

        /// The run's own plan, its first event: a row for each app it will
        /// update, `--with-affected`'s included, named and placed by it. A
        /// row already there keeps its name if the plan has none, and how
        /// far it got.
        mutating func adopt(_ plan: UpdatePlanRecord) {
            let had = Dictionary(rows.map { ($0.app, $0) }, uniquingKeysWith: { first, _ in first })
            rows = plan.apps.filter { $0.hasUpdate || had[$0.app] != nil }.map { app in
                let old = had[app.app]
                var row = Row(app: app.app, title: app.displayName ?? old?.title ?? app.app,
                              blocked: app.blockedReason, updatesTweaks: !app.tweakUpdates.isEmpty)
                row.phase = old?.phase ?? (app.blockedReason.map { .blocked($0) } ?? .waiting)
                row.result = old?.result
                // The run's own plan names the devices; each keeps how far
                // it got.
                row.targets = TargetRow.rows(for: app).map { target in
                    guard let was = old?.targets.first(where: { $0.udid == target.udid }) else { return target }
                    var kept = target
                    kept.phase = was.phase
                    kept.result = was.result
                    return kept
                }
                return row
            }
        }

        mutating func finish(_ end: UpdateAppEndRecord) {
            if !rows.contains(where: { $0.app == end.app }) { setPhase(end.app, .waiting) }
            guard let i = rows.firstIndex(where: { $0.app == end.app }) else { return }
            rows[i].result = end
            // An app the plan blocked didn't fail: its row says why.
            if !end.ok, let reason = rows[i].blocked {
                rows[i].phase = .blocked(reason)
            } else {
                rows[i].phase = AppUpdateText.phase(for: end)
            }
            // Each device's own result. One waiting for its device, or
            // blocked, keeps saying why: it didn't fail either.
            for result in end.targets {
                guard let t = rows[i].targets.firstIndex(where: { $0.udid == result.udid }) else { continue }
                rows[i].targets[t].result = result
                switch rows[i].targets[t].phase {
                case .blocked: continue
                // Waiting, as its result says too, or with nothing more said.
                case .skipped where result.waiting || result.error == nil: continue
                default: rows[i].targets[t].phase = AppUpdateText.targetPhase(result)
                }
            }
        }

        /// One device's install step (`update-step` `install`): started,
        /// ended, or skipped. An end with a detail failed, as the build
        /// step's does; `update-app-end` has the last word.
        mutating func installStep(_ step: UpdateStepRecord) {
            guard let app = step.app, let udid = step.udid,
                  let i = rows.firstIndex(where: { $0.app == app }),
                  let t = rows[i].targets.firstIndex(where: { $0.udid == udid })
            else { return }
            switch step.status {
            case CLIContract.Update.statusStart:
                rows[i].targets[t].phase = .installing
                installing = udid
            case CLIContract.Update.statusSkipped:
                if case .blocked = rows[i].targets[t].phase { break }
                guard let detail = step.detail else { break }
                // Waiting for its device, from the plan or unplugged before
                // its turn: not a failure. Any other skipped install did
                // fail: its get or build, a tweak it needed, or a refusal
                // about this device alone (Developer Mode off at build time,
                // a stranger's IPA), and it's offered again (final review
                // part 3, #3).
                rows[i].targets[t].phase = step.waiting ? .skipped(detail) : .failed
            default:
                // An install whose device went away waits for it, as one not
                // connected from the start does (review part 2, #9).
                if step.waiting {
                    rows[i].targets[t].phase = .skipped(step.detail ?? AppUpdateText.waitingFallback(step.deviceName))
                } else {
                    rows[i].targets[t].phase = step.detail == nil ? .done : .failed
                }
                if installing == udid { installing = nil }
            }
        }

        /// The device a quit waits for when `app`'s install is finishing:
        /// the one being installed on, else its first still to come.
        func installingDevice(of app: String) -> String? {
            guard let row = rows.first(where: { $0.app == app }) else { return nil }
            let target = row.targets.first { $0.udid == installing && $0.phase == .installing }
                ?? row.targets.first { $0.phase == .installing || $0.phase == .waiting }
            return target?.deviceName
        }

        /// The run has ended (its CLI exited). A row or device that got no
        /// result settles (review part 2, #4): one mid-step reads "Stopped"
        /// after a Stop, else "Didn't finish" (a CLI that died), and one that
        /// was still waiting its turn "Not started". Nothing stays
        /// "Installing" or "Building" under "Update Finished".
        mutating func settle() {
            let stopped = stopRequested
            func settled(_ phase: UpdatePhase) -> UpdatePhase {
                switch phase {
                case .updatingTweaks, .getting, .building, .installing: return stopped ? .stopped : .unfinished
                case .waiting: return .notStarted
                default: return phase
                }
            }
            for i in rows.indices {
                if rows[i].result == nil { rows[i].phase = settled(rows[i].phase) }
                for t in rows[i].targets.indices where rows[i].targets[t].result == nil {
                    rows[i].targets[t].phase = settled(rows[i].targets[t].phase)
                }
            }
            installing = nil
            shared = nil
        }

        /// The shared tweak step started (`updating`) or ended: the rows
        /// whose tweaks it updates say so meanwhile.
        mutating func tweakStep(updating: Bool) {
            for i in rows.indices where rows[i].updatesTweaks {
                if updating, rows[i].phase == .waiting { rows[i].phase = .updatingTweaks }
                if !updating, rows[i].phase == .updatingTweaks { rows[i].phase = .waiting }
            }
        }
    }

    /// The last update to end, for the window's refreshes.
    struct Finished: Equatable {
        let apps: [String]
        let serial: Int
    }

    static let noResult = "graft ended without saying how the update went."
    static let stopped = "Stopped."
    /// How long the kill may be held for a step the CLI is finishing before
    /// the sheet offers Force Stop (spec §11.2).
    static let forceStopAfter: TimeInterval = 3 * 60

    /// A stopped run that ended with no `update-end`: "Stopped." when graft
    /// unwound as a Stop asks it to (SIGTERM's 143, a hang-up's 129), and
    /// otherwise what it said as it ended, so a CLI that died isn't passed
    /// off as a clean stop.
    static func stoppedText(_ result: CLIRunner.RunResult, forced: Bool) -> String {
        if forced { return result.stderr.isEmpty ? forceStopped : "\(forceStopped) \(result.failureSummary)" }
        if [0, 129, 143].contains(result.exitCode) { return stopped }
        return "\(stopped) graft ended with an error: \(result.failureSummary)"
    }

    static let forceStopped = "Force stopped."

    /// How long a successful check lasts.
    static let interval: TimeInterval = 24 * 60 * 60
    /// How often the window asks `checkIfDue` while Graft is open.
    static let pollInterval: Duration = .seconds(60 * 60)

    /// Settings › Updates' "Check my apps for updates". On unless turned off.
    /// Either way round the last check no longer counts (review S5): off
    /// drops what it found, so no badge outlives the switch, and on checks
    /// at once rather than a day after the last.
    @Published var checkApps: Bool {
        didSet {
            defaults.set(checkApps, forKey: Key.check)
            guard checkApps != oldValue else { return }
            lastChecked = nil
            checkedByHand = false
            if checkApps {
                Task { await checkIfDue() }
            } else {
                plans = [:]
            }
        }
    }

    /// Each app's plan from the last quick check, by app name.
    @Published private(set) var plans: [String: UpdatePlanApp] = [:]
    @Published private(set) var isChecking = false
    @Published private(set) var sheet: Sheet?
    @Published private(set) var confirmation: Confirmation?
    @Published private(set) var progress: Progress?
    @Published private(set) var lastFinished: Finished?
    /// The device a Stop updating is dropping now (its udid): the menu item
    /// and the button wait.
    @Published private(set) var dropping: String?
    /// The last Stop updating the CLI refused, for the pane's line or the
    /// confirmation to say.
    @Published private(set) var dropFailure: DropFailure?
    /// What each app is doing now that an update of it must wait for, and
    /// the busy slot doesn't hold (review part 2, #2): a get or a drop
    /// staging its IPA, or an update of a tweak it uses. By app name, in
    /// `BusyReason.explanation`'s words. The window keeps it current
    /// (`ContentView`, `appWork(apps:staging:updatingTweak:names:)`).
    @Published private(set) var appWork: [String: String] = [:]

    /// The running update's live log, apart from the store so a streamed
    /// line redraws only the sheet's log panel (`UpdateLog`).
    let log = UpdateLog()

    let runner: CLIRunner
    private let config: ConfigStore
    private let defaults: UserDefaults
    private let now: () -> Date
    private let hold: TerminationHold

    /// When the last check of every app succeeded.
    /// Unpublished: nothing draws it.
    private var lastChecked: Date?
    /// A check came due while the busy slot was held, or while one ran.
    /// Unpublished: nothing draws it.
    private var pendingCheck = false
    /// With the switch off, a check by hand ("Check for updates now", spec
    /// §11.4) found `plans`: they are what the Update buttons show, so they
    /// are kept, and the confirmation's full plan updates them. Never read
    /// again on their own: with the switch off, only a check by hand sends
    /// anything.
    /// Unpublished: nothing draws it; `plans` is what is drawn.
    private var checkedByHand = false
    /// The check that waited for the slot was asked for by hand.
    /// Unpublished: nothing draws it.
    private var pendingByHand = false
    /// The SIGKILL grace an update's Stop gives (`RunStopper`); a test
    /// shortens it.
    /// Unpublished: a setting, drawn nowhere.
    var stopGrace: TimeInterval = cliStopGrace
    /// The running update's Stop.
    /// Unpublished: nothing draws it.
    private var stopper: RunStopper?
    /// The running update's Task.
    /// Unpublished: nothing draws it; `progress` says what runs.
    private var task: Task<Void, Never>?
    /// The running update's hold on the quit, which says when the CLI is
    /// finishing an install (`TerminationHold.finishing`).
    /// Unpublished: nothing draws it.
    private var holdTicket: TerminationHold.Ticket?
    /// Numbers `lastFinished`.
    /// Unpublished: nothing draws it.
    private var finishedSerial = 0
    /// How often a held kill is looked at for Force Stop; a test shortens
    /// it.
    /// Unpublished: a setting, drawn nowhere.
    var forceStopTick: Duration = .seconds(1)
    /// When a change in the connected devices last read the plan.
    /// Unpublished: nothing draws it.
    private var lastDeviceRecheck: Date?
    /// Waits out `deviceRecheckInterval` for a change that came inside it.
    /// Unpublished: nothing draws it.
    private var deviceRecheckWatch: Task<Void, Never>?
    /// How often that wait looks at the clock; a test shortens it.
    /// Unpublished: a setting, drawn nowhere.
    var deviceRecheckTick: Duration = .seconds(30)
    /// Watches a held kill until it has been held `forceStopAfter`.
    /// Unpublished: nothing draws it; `progress.offersForceStop` says what it
    /// found.
    private var forceStopWatch: Task<Void, Never>?

    /// Every dependency is a parameter: `GraftApp` passes the real
    /// preferences, clock and hold, a test its own (`NoRealDefaultsTests`).
    init(runner: CLIRunner, config: ConfigStore, defaults: UserDefaults, now: @escaping () -> Date,
         hold: TerminationHold) {
        self.runner = runner
        self.config = config
        self.defaults = defaults
        self.now = now
        self.hold = hold
        checkApps = defaults.object(forKey: Key.check) as? Bool ?? true
    }

    // MARK: The daily check

    /// Due when the switch is on and the last successful check of every app
    /// is more than a day old, or there wasn't one this launch. A last check
    /// in the future (the clock was set back) is due too.
    static func isDue(checkApps: Bool, lastChecked: Date?, now: Date) -> Bool {
        guard checkApps else { return false }
        guard let lastChecked else { return true }
        return now < lastChecked || now.timeIntervalSince(lastChecked) > interval
    }

    /// The window calls this at launch and every `pollInterval`.
    func checkIfDue() async {
        guard Self.isDue(checkApps: checkApps, lastChecked: lastChecked, now: now()) else { return }
        await check(apps: nil)
    }

    /// "Check for updates now", on an app's pane and in the sidebar: the
    /// quick plan for `apps` (nil: every app), with the switch on or off
    /// (spec §11.4). With it off, what this finds is what the Update buttons
    /// show.
    func checkNow(apps: [String]? = nil) async {
        await check(apps: apps, byHand: true)
    }

    /// The plan read again for `apps` (nil: every app), after a get, a tweak
    /// update, a build, an update or a change to their config
    /// (`configChanged`). Only with the switch on: with it off,
    /// nothing is sent but by Check for updates now (spec §11.4, review part
    /// 1 #3), and the buttons keep what that found, stale or not.
    func recheck(apps: [String]?) async {
        guard checkApps else { return }
        await check(apps: apps)
    }

    // MARK: A change to an app's config

    /// What of an app's record its plan is worked out from: the version it
    /// builds (a hold), its tweaks, delivery and LiveContainer pin, how it's
    /// signed (the account, the profile), the cyan options that change the
    /// IPA (`delivery.CYAN_KEYS`, a display name included), its bundle id
    /// and App Store country. Left out: whether its profile needs Developer
    /// Mode, which is read from the profile a build makes rather than set
    /// (that build's end reads the plan already), and what the CLI reports
    /// about itself (`reportsLCPin`, `reportsStoreCountry`).
    struct PlanInputs: Equatable {
        let bundleId: String
        let preferredVersion: String
        let tweaks: [String]
        let displayName: String?
        let delivery: DeliveryMode
        let lcBundleId: String?
        let account: AccountMode?
        let autoProfile: Bool
        let profileOverride: String?
        let keepBundleId: Bool
        let stripExtensions: Bool
        let stripSupportedDevices: Bool
        let compressionLevel: Int
        let storeCountry: String?

        init(_ app: AppRecord) {
            bundleId = app.bundleId
            preferredVersion = app.preferredVersion
            tweaks = app.tweaks
            displayName = app.displayName
            delivery = app.delivery
            lcBundleId = app.lcBundleId
            account = app.account
            autoProfile = app.autoProfile
            profileOverride = app.profileOverride
            keepBundleId = app.keepBundleId
            stripExtensions = app.stripExtensions
            stripSupportedDevices = app.stripSupportedDevices
            compressionLevel = app.compressionLevel
            storeCountry = app.storeCountry
        }
    }

    /// Between two reads of the app list: `changed`, each app added or
    /// whose `PlanInputs` differ, in `new`'s order; `removed`, each app gone.
    static func configChanges(from old: [AppRecord], to new: [AppRecord]) -> (changed: [String], removed: [String]) {
        let was = Dictionary(old.map { ($0.name, PlanInputs($0)) }, uniquingKeysWith: { _, latest in latest })
        let names = Set(new.map(\.name))
        let changed = new.filter { was[$0.name] != PlanInputs($0) }.map(\.name)
        let removed = old.map(\.name).filter { !names.contains($0) }
        return (changed, removed)
    }

    /// The window read the app list again (`ConfigStore.apps`), and it
    /// changed: Edit…, a tweak attached or detached, a hold let go, or a
    /// change made outside the app (`graft --set-app-options` in Terminal)
    /// and read on a reload. Each app whose plan inputs changed, or that was
    /// added, is checked again, all in one check; with the switch off,
    /// nothing is sent (`recheck`, spec §11.4). An app removed drops what
    /// was found for it, either way: that sends nothing, and an app added
    /// later under its name mustn't show it.
    func configChanged(from old: [AppRecord], to new: [AppRecord]) async {
        let (changed, removed) = Self.configChanges(from: old, to: new)
        if removed.contains(where: { plans[$0] != nil }) {
            plans = plans.filter { !removed.contains($0.key) }
        }
        guard !changed.isEmpty else { return }
        await recheck(apps: changed)
    }

    /// How often a change in the connected devices may read the plan again.
    static let deviceRecheckInterval: TimeInterval = 10 * 60

    /// A device was connected or went away (`DevicesStore`'s set changed).
    /// When an update waits for a device that isn't connected (spec §11.3),
    /// the plan is read again for every app, so the pane offers the install
    /// once that device is back, without a relaunch. At most once every
    /// `deviceRecheckInterval`, as the check goes online: a change inside it
    /// is read once it's up. With nothing waiting for a device, a change
    /// reads nothing; the confirmation's full plan sees the devices anyway.
    /// With the switch off, nothing either (spec §11.4).
    func deviceSetChanged() async {
        guard checkApps, plans.values.contains(where: { !$0.waitingTargets.isEmpty }) else { return }
        let now = now()
        if let last = lastDeviceRecheck, now >= last, now.timeIntervalSince(last) < Self.deviceRecheckInterval {
            watchDeviceRecheck()
            return
        }
        lastDeviceRecheck = now
        await recheck(apps: nil)
    }

    /// Look at the clock every `deviceRecheckTick` until the interval since
    /// the last device re-check is up, then take the change that came
    /// inside it, once.
    private func watchDeviceRecheck() {
        guard deviceRecheckWatch == nil else { return }
        let tick = deviceRecheckTick
        deviceRecheckWatch = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: tick)
                guard !Task.isCancelled, let self else { return }
                guard let last = self.lastDeviceRecheck else { break }
                let now = self.now()
                if now < last || now.timeIntervalSince(last) >= Self.deviceRecheckInterval { break }
            }
            guard !Task.isCancelled, let self else { return }
            self.deviceRecheckWatch = nil
            await self.deviceSetChanged()
        }
    }

    /// The busy slot came free: a check that waited for it runs now.
    func slotFreed() async {
        guard pendingCheck, config.busy == nil else { return }
        let byHand = pendingByHand
        pendingCheck = false
        pendingByHand = false
        await check(apps: nil, byHand: byHand)
    }

    /// The plans are kept and read again: the switch is on, or a check by
    /// hand found them.
    private var keepsPlans: Bool { checkApps || checkedByHand }

    static func quickArguments(apps: [String]?) -> [String] {
        [Flag.json, Flag.update] + (apps ?? [Flag.updateAll]) + [Flag.plan, Flag.quick]
    }

    /// `byHand`: asked for by "Check for updates now", which runs with the
    /// switch off too.
    private func check(apps: [String]?, byHand: Bool = false) async {
        guard checkApps || byHand else { return }
        guard config.busy == nil, !isChecking else {
            pendingCheck = true
            pendingByHand = pendingByHand || byHand
            return
        }
        isChecking = true
        let found = await quickPlan(apps: apps)
        isChecking = false
        // A switch turned off meanwhile drops it, unless it was asked for.
        if let found, checkApps || byHand {
            if !checkApps { checkedByHand = true }
            if apps == nil {
                plans = Dictionary(found.apps.map { ($0.app, $0) }, uniquingKeysWith: { _, latest in latest })
                lastChecked = now()
            } else {
                for app in found.apps { plans[app.app] = app }
            }
        }
        if pendingCheck, config.busy == nil {
            let byHand = pendingByHand
            pendingCheck = false
            pendingByHand = false
            await check(apps: nil, byHand: byHand)
        }
    }

    /// Quiet: a run that fails, or answers with no plan, is nil.
    private func quickPlan(apps: [String]?) async -> UpdatePlanRecord? {
        guard let result = try? await runner.runOnce(arguments: Self.quickArguments(apps: apps)),
              result.exitCode == 0
        else { return nil }
        return result.stdout.buildEvents().lazy.compactMap { event -> UpdatePlanRecord? in
            if case let .updatePlan(record) = event { return record }
            return nil
        }.first
    }

    // MARK: Stop updating on a device

    static func dropArguments(app: String, udid: String) -> [String] {
        [Flag.json, Flag.dropUpdateTarget, app, Flag.device, udid]
    }

    /// Stop updating `app` on the device `udid` (spec §11.3, devices that
    /// are gone): a device sold, retired or reset would wait forever. The
    /// CLI drops it as a target until a new delivery there, and changes
    /// nothing on any device. Then that app's plan is read again: the
    /// confirmation's full plan when it's open on that app, else the quick
    /// one, while the switch is on. With it off, nothing is sent (spec
    /// §11.4): the device leaves the plan the buttons show. Not while an
    /// update runs, whose plan is made.
    func dropTarget(app: String, udid: String) async {
        guard !isUpdating, dropping == nil else { return }
        dropping = udid
        dropFailure = nil
        do {
            _ = try await runner.runEvents(Self.dropArguments(app: app, udid: udid),
                                           failing: "Stop updating \(app) on that device")
        } catch {
            dropping = nil
            dropFailure = DropFailure(app: app, udid: udid, message: error.localizedDescription)
            return
        }
        dropping = nil
        if let open = confirmation, open.apps.contains(app) {
            confirmation?.plan = nil
            confirmation?.failure = nil
            await loadConfirmation(apps: open.apps)
        } else if checkApps {
            await recheck(apps: [app])
        } else {
            plans[app] = plans[app]?.dropping(udid)
        }
    }

    // MARK: Each app's own work

    /// The window's reading of what each app is doing (`appWork`).
    func setAppWork(_ work: [String: String]) {
        if work != appWork { appWork = work }
    }

    /// Why an update of `apps` must wait now, or nil: the first one's own
    /// work. Update All, the confirmation's Update and `start` all wait for
    /// it, as an app's own Update & Install does (`AppDetailView`).
    func waitsFor(_ apps: [String]) -> String? {
        apps.lazy.compactMap { self.appWork[$0] }.first
    }

    /// `appWork` from what the window's stores say: `staging`, each app's
    /// get or drop (`AppStoreStore.activity`); `updatingTweak`, the tweak
    /// being updated (`TweaksStore.updatingFilename`), which every app that
    /// uses it waits for. `names`: each app as the sidebar shows it.
    static func appWork(apps: [AppRecord], staging: [String: AppStoreStore.Activity], updatingTweak: String?,
                        names: [String: String]) -> [String: String] {
        var work: [String: String] = [:]
        for app in apps {
            let name = names[app.name] ?? app.name
            switch staging[app.name] {
            case .getting, .claiming:
                work[app.name] = "\(name) is getting from the App Store — wait for it to finish"
            case .providing:
                work[app.name] = "\(name) is staging an IPA — wait for it to finish"
            case nil:
                if let tweak = updatingTweak, app.tweaks.contains(tweak) {
                    work[app.name] = "A tweak \(name) uses is updating — wait for it to finish"
                }
            }
        }
        return work
    }

    // MARK: The confirmation

    static func planArguments(apps: [String]) -> [String] {
        [Flag.json, Flag.update] + apps + [Flag.plan]
    }

    /// Update & Install, or Update All: the confirmation, which runs the full
    /// plan (readiness checked, spec 3.1) first. Its button waits for it.
    ///
    /// The CLI refuses a whole plan that names an app it doesn't know, so an
    /// app removed since the last plan is left out, once the app list has
    /// been read (review S11). With nothing left, the confirmation says so,
    /// and asks the CLI nothing.
    func openConfirmation(apps: [String], alsoInstall: Bool = false) {
        guard !isUpdating else { return }
        let present = config.hasLoadedApps
            ? apps.filter { name in config.apps.contains { $0.name == name } } : apps
        var opened = Confirmation(apps: present)
        opened.alsoInstall = alsoInstall
        // A refusal the pane said isn't this sheet's.
        dropFailure = nil
        if present.isEmpty { opened.plan = UpdatePlanRecord(quick: false, apps: []) }
        confirmation = opened
        sheet = .confirm
        guard !present.isEmpty else { return }
        Task { await loadConfirmation(apps: present) }
    }

    private func loadConfirmation(apps: [String]) async {
        var record: UpdatePlanRecord?
        var failure: String?
        do {
            let events = try await runner.runEvents(Self.planArguments(apps: apps),
                                                    failing: "Check what Update would change")
            record = events.lazy.compactMap { event -> UpdatePlanRecord? in
                if case let .updatePlan(found) = event { return found }
                return nil
            }.first
            if record == nil { failure = Self.noPlan }
        } catch {
            failure = error.localizedDescription
        }
        // Closed, or another opened, while it ran: not this one's.
        guard confirmation?.apps == apps else { return }
        confirmation?.plan = record
        confirmation?.failure = failure
        if let record, keepsPlans {
            for app in record.apps { plans[app.app] = app }
        }
    }

    func setAlsoInstall(_ on: Bool) {
        confirmation?.alsoInstall = on
    }

    func cancelConfirmation() {
        confirmation = nil
        sheet = nil
    }

    /// The confirmation's Update: run the apps its full plan says are ready.
    /// With none, the button is Done, which closes the sheet. True when an
    /// update started.
    @discardableResult
    func confirm() -> Bool {
        guard let confirmation else { return false }
        // A full plan that failed offers Done, which closes the sheet
        // (review part 2, #5).
        if confirmation.failure != nil {
            closeSheet()
            return false
        }
        guard let plan = confirmation.plan else { return false }
        guard !confirmation.runnable.isEmpty else {
            closeSheet()
            return false
        }
        // An app's own get, drop or tweak update, which the busy slot
        // doesn't hold, started while the confirmation was open (review
        // part 2, #2).
        let reaching = confirmation.runnable + (confirmation.alsoInstall ? AppUpdateText.affected(plan) : [])
        if let wait = waitsFor(reaching) {
            self.confirmation?.refusal = wait
            return false
        }
        guard start(apps: confirmation.runnable, withAffected: confirmation.alsoInstall, plan: plan) else {
            // Said, not swallowed: a build (or another update) took the slot
            // while the confirmation was open.
            self.confirmation?.refusal = config.busy?.explanation ?? Self.anotherRuns
            return false
        }
        return true
    }

    static let anotherRuns = "Another update is running — wait for it to finish"

    // MARK: The run

    /// An update is running.
    var isUpdating: Bool { task != nil }

    static func runArguments(apps: [String], withAffected: Bool) -> [String] {
        [Flag.json, Flag.update] + apps + (withAffected ? [Flag.withAffected] : [])
    }

    /// Run `graft --update` for `apps`, in the busy slot and under the quit
    /// hold. `plan` names each row and its phone. False when an update or a
    /// build already holds the slot.
    @discardableResult
    func start(apps: [String], withAffected: Bool, plan: UpdatePlanRecord?) -> Bool {
        guard task == nil, !apps.isEmpty, waitsFor(apps) == nil, config.beginUpdate(apps: apps) else { return false }
        let byApp = Dictionary((plan?.apps ?? []).map { ($0.app, $0) }, uniquingKeysWith: { first, _ in first })
        progress = Progress(rows: apps.map { name in
            let p = byApp[name]
            return Row(app: name, title: p?.displayName ?? name, blocked: p?.blockedReason,
                       updatesTweaks: !(p?.tweakUpdates.isEmpty ?? true),
                       targets: p.map(TargetRow.rows(for:)) ?? [])
        })
        progress?.withAffected = withAffected
        confirmation = nil
        sheet = .progress
        log.clear()
        let stopper = RunStopper(grace: stopGrace)
        self.stopper = stopper
        let args = Self.runArguments(apps: apps, withAffected: withAffected)
        // Held from here, with the slot: a quit a moment after Update is
        // asked about it. Quit Anyway is this update's own first Stop.
        let hold = self.hold
        let ticket = hold.begin(TerminationHold.updatingApps) { [weak self] in self?.quitStop() }
        holdTicket = ticket
        task = Task { @MainActor [weak self] in
            await self?.perform(args, stopper: stopper)
            hold.end(ticket)
            self?.finish(apps: apps)
        }
        return true
    }

    /// Stop: now, unless the CLI is finishing an install or a tweak swap,
    /// which it then says (`update-stopping`), and the sheet says too. A
    /// second Stop cancels that install (spec §11.2): the CLI cancels it on
    /// the second SIGTERM, and nothing is waited for any more.
    func stop() {
        guard let stopper, task != nil, var p = progress else { return }
        if p.stopRequested {
            p.stoppedAgain = true
            endDeferral(&p)
        }
        p.stopRequested = true
        progress = p
        stopper.stop()
    }

    /// Quit Anyway's stop (spec 4.5): a first Stop, never a second. After
    /// the person's own Stop it sends nothing more, so an install that Stop
    /// waits for isn't canceled, and the quit waits for it instead
    /// (`TerminationHold.finishing`, review part 2, #1).
    func quitStop() {
        guard progress?.stopRequested != true else { return }
        stop()
    }

    /// Force Stop, which the sheet offers once the kill has been held
    /// `forceStopAfter`: SIGKILL now, whatever the CLI is finishing.
    func forceStop() {
        guard let stopper, task != nil, var p = progress else { return }
        p.stopRequested = true
        p.forced = true
        endDeferral(&p)
        progress = p
        stopper.forceKill()
    }

    /// Try Again: the confirmation, for the apps that didn't finish, with
    /// "Also install" as the run had it.
    func tryAgain() {
        guard let finished = progress else { return }
        tryAgain(apps: finished.unfinished)
    }

    /// A device's own Try Again: the confirmation for its app (`apps`). The
    /// CLI installs only on the devices that still need it.
    func tryAgain(apps: [String]) {
        guard task == nil, let finished = progress else { return }
        progress = nil
        openConfirmation(apps: apps, alsoInstall: finished.withAffected)
    }

    /// Done, or the sheet dismissed: not while an update runs.
    func closeSheet() {
        guard task == nil else { return }
        sheet = nil
        confirmation = nil
        progress = nil
        log.clear()
    }

    private func perform(_ args: [String], stopper: RunStopper) async {
        do {
            let result = try await runner.stream(arguments: args, stopper: stopper) { [weak self] event in
                self?.note(event)
            }
            // The hops `onEvent` makes can land after `stream` returns; the
            // result's events are complete, and these two are idempotent.
            for event in result.events {
                switch event {
                case let .updateAppEnd(record): progress?.finish(record)
                case let .updateEnd(record): progress?.end = record
                default: break
                }
            }
            if progress?.end == nil {
                progress?.failure = stopper.wasStopped ? Self.stoppedText(result, forced: stopper.wasForced)
                    : (result.exitCode == 0 ? Self.noResult : result.failureSummary)
            }
        } catch {
            if progress?.end == nil {
                progress?.failure = error is CancellationError ? Self.stopped : error.localizedDescription
            }
        }
    }

    /// One event of the run: the rows and the Stop's state on `progress`,
    /// published only when they change, and the transcript on `log`.
    private func note(_ event: BuildEvent) {
        guard var p = progress else { return }
        var lines: [LogLine] = []
        switch event {
        case let .updatePlan(record):
            // The run's own plan (review S4): its rows, and the slot covers
            // every app in it.
            p.adopt(record)
            config.widenUpdate(to: p.rows.map(\.app))
        case let .updateStep(step):
            if step.step == CLIContract.Update.stepInstall {
                // One device's install (spec §11.3): the first runs inside
                // the build, each later one after it.
                p.installStep(step)
                if step.status == CLIContract.Update.statusStart, let app = step.app {
                    p.current = app
                    p.setPhase(app, .installing)
                }
            } else if step.status == CLIContract.Update.statusStart {
                if let app = step.app {
                    p.current = app
                    p.shared = nil
                    p.setPhase(app, step.step == CLIContract.Update.stepGet ? .getting(step.detail) : .building)
                } else {
                    p.shared = AppUpdateText.phaseText(.updatingTweaks)
                    p.tweakStep(updating: true)
                }
            } else if step.app == nil {
                p.shared = nil
                p.tweakStep(updating: false)
                // The shared tweak step a Stop waited for is done.
                if p.stopping.map({ $0.app == nil }) == true { endDeferral(&p) }
            }
            lines = AppUpdateText.logLines(step)
        case let .stage(name, status, _)
            where status == "start"
                && (name == CLIContract.Stage.liveContainerPush || name == CLIContract.Stage.sideloadInstall):
            if let app = p.current { p.setPhase(app, .installing) }
            lines = BuildLogView.lines(for: event)
        case let .updateStopping(record):
            // The CLI is finishing an install or a tweak swap. The stopper
            // already held the kill as the runner read this
            // (`RunStopper.read`); a quit waits for it too (spec 4.5),
            // saying where. After a second Stop nothing is waited for.
            guard !p.stoppedAgain, !p.forced else { break }
            p.stopping = record
            p.heldSince = now()
            p.offersForceStop = false
            watchForceStop()
            let device = record.app.flatMap { p.installingDevice(of: $0) }
            if let holdTicket {
                hold.finishing(holdTicket, note: AppUpdateText.quittingText(record, deviceName: device))
            }
        case let .updateAppEnd(record):
            p.finish(record)
            // That app's install, which a Stop waited for, is done.
            if let held = p.stopping, held.app != nil, held.app == record.app { endDeferral(&p) }
        case let .updateEnd(record):
            p.end = record
            endDeferral(&p)
        case let .tweakUpdated(record):
            lines = [LogLine(kind: .ok, text: "  ✓ \(record.filename) → \(record.newFilename ?? record.filename)")]
        case let .appStoreStage(record):
            lines = [LogLine(kind: .dim, text: "  [app store] \(record.stage.label)")]
        default:
            lines = BuildLogView.lines(for: event)
        }
        log.append(lines)
        if p != progress { progress = p }
    }

    /// The step a Stop waited for has ended, or a second Stop gave up on it:
    /// nothing is waited for, by the sheet or a quit, and no Force Stop is
    /// offered.
    private func endDeferral(_ p: inout Progress) {
        p.stopping = nil
        p.heldSince = nil
        p.offersForceStop = false
        forceStopWatch?.cancel()
        forceStopWatch = nil
        if let holdTicket { hold.finishing(holdTicket, note: nil) }
    }

    /// Look at the held kill every `forceStopTick` until it has been held
    /// `forceStopAfter`, by the store's clock.
    private func watchForceStop() {
        forceStopWatch?.cancel()
        let tick = forceStopTick
        forceStopWatch = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: tick)
                guard !Task.isCancelled, let self else { return }
                guard let since = self.progress?.heldSince else { return }
                if self.now().timeIntervalSince(since) >= Self.forceStopAfter {
                    self.progress?.offersForceStop = true
                    return
                }
            }
        }
    }

    private func finish(apps: [String]) {
        log.flush()
        progress?.settle()
        progress?.isRunning = false
        forceStopWatch?.cancel()
        forceStopWatch = nil
        config.endUpdate()
        task = nil
        stopper = nil
        holdTicket = nil
        finishedSerial += 1
        // The run's own plan's apps, `--with-affected`'s among them.
        lastFinished = Finished(apps: progress?.rows.map(\.app) ?? apps, serial: finishedSerial)
        Task { await recheck(apps: nil) }
    }
}

/// An update's live log (spec 4.4), kept apart from `AppUpdatesStore` as
/// `DumpLog` is from `DeviceAppsStore`.
///
/// Its own `ObservableObject` because it changes line by line while a build
/// streams, and on the store each line redrew every view observing it: the
/// sidebar, each app's pane, the sheet. Only the sheet's log panel observes
/// this. Lines are published at most once every `interval`, as each publish
/// redraws the whole panel; `flush` publishes the rest when the run ends.
@MainActor
final class UpdateLog: ObservableObject {
    /// The whole run's transcript, newest last.
    @Published private(set) var lines: [LogLine] = []

    /// Unpublished: lines that have arrived since the last publish of `lines`.
    private var waiting: [LogLine] = []
    /// Unpublished: the publish of `waiting` that is due, if any.
    private var due: Task<Void, Never>?
    /// How long a line can wait to be published.
    let interval: Duration

    init(interval: Duration = .milliseconds(100)) {
        self.interval = interval
    }

    /// Lines of the run's: published with any others that arrive within
    /// `interval` of the first.
    func append(_ new: [LogLine]) {
        guard !new.isEmpty else { return }
        waiting += new
        guard due == nil else { return }
        let interval = interval
        due = Task { [weak self] in
            try? await Task.sleep(for: interval)
            guard !Task.isCancelled else { return }
            self?.flush()
        }
    }

    /// Publish the lines still waiting, now.
    func flush() {
        due?.cancel()
        due = nil
        guard !waiting.isEmpty else { return }
        lines += waiting
        waiting = []
    }

    /// A new run: nothing of the last one, published or waiting.
    func clear() {
        due?.cancel()
        due = nil
        waiting = []
        lines = []
    }
}
