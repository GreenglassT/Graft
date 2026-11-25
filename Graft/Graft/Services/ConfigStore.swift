import Foundation
import Combine

private typealias Flag = CLIContract.Flag

@MainActor
final class ConfigStore: ObservableObject, CLIStore {
    @Published private(set) var apps: [AppRecord] = []

    /// Which account config.yaml is for, as the last `--list` reported it, or
    /// nil before one has landed and against a CLI too old to say.
    ///
    /// Every free-mode surface reads this one value — the Add App and Edit
    /// delivery pickers, the app pane's Build & Install, Re-install, the device
    /// pane's LiveContainer section, Settings — so they cannot disagree about
    /// which account this is. See `AccountMode` for why nil disables nothing.
    ///
    /// From `--list`'s `account` event, which it emits even with no apps;
    /// failing that, the `account` an older CLI put on each `app`. A load that
    /// throws leaves it as it was, like `apps`.
    @Published private(set) var account: AccountMode?

    /// Whether each app's last delivery still matches what a build would
    /// produce, keyed by the app's config name. Absent means the CLI said
    /// nothing about it — an older CLI, or a state it declined to compute —
    /// and absent renders as no badge, same as `.current`.
    @Published private(set) var deliveryStates: [String: DeliveryStateRecord] = [:]

    /// Apps whose last verified build was made from an IPA that was never
    /// decrypted, keyed by config name. Absent means the last build that
    /// checked found nothing wrong.
    ///
    /// On the store rather than in AppDetailView's `@State` for the reason
    /// that made this a bug in the first place: the warning has to outlive the
    /// moment it was printed. `@State` in the detail pane is wiped wholesale
    /// on every app switch (see the per-app `.task` there), and "build, click
    /// something else, come back, push the IPA to the phone" is precisely the
    /// sequence that ends with an app crashing on launch and nothing on screen
    /// explaining why.
    ///
    /// Session-scoped, deliberately: the CLI records nothing about this in the
    /// build log, so there is nothing to read back at launch. The next build
    /// of that app decides it again.
    @Published private(set) var encryptedBinaries: [String: EncryptedBinariesRecord] = [:]

    /// Apps whose current build has already reported encrypted binaries.
    ///
    /// Exists only to order two events that arrive in one direction:
    /// `encrypted-binaries` comes first, then the decryption stage's `end`.
    /// Without this, the stage-end that *is* the verdict would immediately
    /// clear the finding the same build had just made.
    /// Unpublished: bookkeeping, which nothing draws.
    private var encryptedReportedThisBuild: Set<String> = []

    /// How the last build of each app ended badly, keyed by config name — or
    /// absent when the last build succeeded or none has run this session.
    ///
    /// On the store for the same reason as `encryptedBinaries`, and it is the
    /// same defect one step further out: `AppDetailView` is removed from the
    /// hierarchy on any sidebar-section change, so a build that is still
    /// running when the user clicks "Devices" finishes into a destroyed view.
    /// `handle(event:owner:)`, `setBuildState` and `report(unexplained:)` all
    /// write to `@State` that no longer renders anywhere, and the pane the
    /// user comes back to shows an idle header over an empty log — for a build
    /// that failed.
    ///
    /// Session-scoped and deliberately not read back from the build log: the
    /// failures that need this most are the ones that never got a log row —
    /// the app gone from config.yaml, a phone unplugged before `build-start`,
    /// a state directory that wouldn't open.
    @Published private(set) var buildFailures: [String: String] = [:]

    @Published var lastError: String?

    /// Name of the app whose build subprocess is currently in flight, or nil.
    ///
    /// Lives on the store rather than in AppDetailView's `@State` because the
    /// sidebar needs it too: the sidebar's Remove used to be reachable during
    /// a build, and removing the app tears the detail view (and its log panel)
    /// down while the CLI keeps signing and pushing the IPA — a completed
    /// install minutes later with no progress, no notice, and nothing to
    /// cancel. The store is the one object both views already hold.
    @Published private(set) var buildingApp: String?

    /// The stage the running build last announced starting, or nil between
    /// builds.
    ///
    /// Read by the cancel confirmation, which has to word itself differently
    /// depending on whether stopping now costs a download or a half-written
    /// file on the phone — see `DeviceRisk`. On the
    /// store rather than derived from `AppDetailView`'s `buildState` because
    /// the sidebar offers the same cancel, and the sidebar has never had the
    /// build's state.
    @Published private(set) var buildStage: String?

    /// When each app's most recent build started this session, keyed by
    /// config name.
    ///
    /// Read by the app pane to retire what a build makes old news — today the
    /// "✓ Staged … from the App Store" line (`AppDetailView.
    /// stagedConfirmation`). Set in `beginBuild`, so every build sets it:
    /// there is one build slot and one way into it. Clearing that line from
    /// the button instead is how "Build for this Mac" came to leave it
    /// standing — Build & Install remembered, the second button did not.
    @Published private(set) var buildStartedAt: [String: ContinuousClock.Instant] = [:]

    /// The Task running the build, so it can be stopped from somewhere other
    /// than the pane that started it.
    ///
    /// Canceling it is what reaches the subprocess: `CLIRunner.launch` wraps
    /// the continuation in a `withTaskCancellationHandler` whose `onCancel`
    /// terminates the adopted process — SIGTERM, then SIGKILL `cliStopGrace`
    /// later for the stretches cyan and zsign spend in native loops.
    ///
    /// Unpublished on purpose: nothing renders the Task itself, and
    /// `buildingApp` already publishes the state the UI reads.
    private var buildTask: Task<Void, Never>?

    /// The app whose build the user asked to stop, until the next build claims
    /// the slot.
    ///
    /// This exists because a canceled build is indistinguishable from a
    /// broken one at the point it lands: the CLI is killed, so it exits
    /// non-zero with whatever it had written to stderr, and the pane's normal
    /// reading of that is "the build failed" — a red Retry and a failure card
    /// for something the user deliberately did a second ago.
    @Published private(set) var cancellingApp: String?

    /// What the running build has done to the phone so far — the single value
    /// both the confirmation and the post-mortem read. See `DeviceExposure`.
    ///
    /// This replaced `devicePushCompleted` and a stage-derived branch that two
    /// consumers read differently: the confirmation warned about the window
    /// between `lc-push end` and `build-end`, while the post-mortem read
    /// `buildStage` — nil in exactly that window — and wrote no advice at all.
    @Published private(set) var deviceExposure: DeviceExposure = .notYet

    /// When each app's last Stop happened, so the pane can tell "this build was
    /// stopped" from "an older build failed and is still worth showing".
    /// Unpublished: a change to it alone redraws nothing.
    private var stoppedAt: [String: Date] = [:]

    /// When this app's last Stop happened, or nil.
    ///
    /// Data, not a verdict. `stopSupersedes` took a `Date` the *view* had
    /// parsed and answered a question about the *view's* card, which put the
    /// decision on the wrong side of the boundary and forced the answer back
    /// through a `String?` — so it could never reach the card's title, and the
    /// card rendered "Last build failed" above "An earlier build failed: …".
    func lastStop(of appName: String) -> Date? { stoppedAt[appName] }

    /// Whether `lastBuild`'s failure predates the stop the user just performed.
    ///
    /// A stopped build writes no history row, so anything `lastBuild` holds is
    /// necessarily from an *earlier* run — and that run's failure is real news
    /// the user still needs. What must not happen is attributing it to the
    /// build they just stopped, which is a question of labeling, not of hiding.
    func stopSupersedes(_ appName: String, buildTimestamp: Date?) -> Bool {
        guard let stopped = stoppedAt[appName] else { return false }
        guard let buildTimestamp else { return true }
        return buildTimestamp < stopped
    }

    /// The name of the app whose `--reconcile` is in flight, or nil.
    ///
    /// On the store for the same reason as `buildingApp`: `AppDetailView`'s
    /// per-app `.task` resets its `@State` wholesale on every selection
    /// change, so a view-local flag told the user a check had finished merely
    /// because they clicked another app and back — re-enabling the button and
    /// letting a second `--reconcile` race the first on `log.sqlite`.
    @Published private(set) var checkingApp: String?

    /// What the phone is waiting on while the check runs — the CLI's
    /// `device` line, "Unlock the phone and tap Trust…". Shown live under
    /// Checking…, gone when the check ends, and never part of its result.
    @Published private(set) var deviceCheckStatus: String?

    /// What the running build will actually do to the phone, captured from the
    /// build options at launch. See `EffectiveDelivery`.
    /// Unpublished: a change to it alone redraws nothing.
    private(set) var effectiveDelivery: EffectiveDelivery = .unknown

    /// Builds that have already posted a completion banner, so one build can
    /// never produce two.
    ///
    /// The case that needs it: a `build-end` with `ok: true` followed by a
    /// non-zero exit (the CLI failing during retention or the log write, after
    /// it had already reported success). Both paths record an outcome, and
    /// without this the user gets "example built ✓" and then "example build
    /// failed" for the same run.
    /// Unpublished: bookkeeping, which nothing draws.
    private var notifiedThisBuild: Set<String> = []

    /// How a finished build reaches the user's notification center.
    ///
    /// A seam, because there is no other way to keep a test out of
    /// `UNUserNotificationCenter`: `note(_:forBuildOf:)` is driven directly by
    /// a dozen existing tests, and every one of them reached
    /// `requestAuthorization` once the banner was wired in — raising a real
    /// permission prompt for `com.greenglasst.graft` in the middle of
    /// `xcodebuild test`. It also lets the store's *decisions* be asserted,
    /// which is where the interesting logic now lives.
    ///
    /// No default: `BuildNotifier.post` is named only in `GraftApp`, and a
    /// test passes its own (`NoRealDefaultsTests`).
    /// Unpublished: a change to it alone redraws nothing.
    var postNote: @MainActor (BuildNotifier.Note?) -> Void

    let runner: CLIRunner
    private let cliArgs: [String]
    /// Holds sudden and automatic termination off while a build runs
    /// (whole-diff review, app-3).
    private let hold: TerminationHold

    init(
        runner: CLIRunner, cliArgs: [String] = [Flag.json, Flag.list],
        notify: @escaping @MainActor (BuildNotifier.Note?) -> Void,
        hold: TerminationHold = .shared
    ) {
        self.runner = runner
        self.cliArgs = cliArgs
        self.postNote = notify
        self.hold = hold
    }

    /// Claim the build slot for `appName`.
    ///
    /// Note what this does *not* do any more: clear `encryptedBinaries`.
    /// It used to, on the reasoning that a build which runs the check gets to
    /// decide again — but starting work is not a verdict. A build that dies
    /// before stage 2.5 (the phone unplugged, no network for the IPA, a bad
    /// `--provide-ipa` path) reached this line and erased a standing "will
    /// crash on launch" finding that nothing had re-examined. The card
    /// vanished for the rest of the session over a cached source IPA that was
    /// byte-identical, and the next thing the user does is re-install the
    /// archived build and push a crashing app with no warning on screen.
    ///
    /// The finding is now cleared by `note(_:forBuildOf:)` when the check
    /// actually ends, which also makes the `--skip-decrypt-check` case
    /// structural rather than a flag every caller has to remember to pass: a
    /// skipped check emits no stage, so nothing clears.
    private func beginBuild(appName: String) {
        buildingApp = appName
        buildStartedAt[appName] = .now
        encryptedReportedThisBuild.remove(appName)
        // A new build supersedes the last one's failure: the header is about to
        // say "running" and a red card underneath describing the previous
        // attempt would read as this one's.
        buildFailures[appName] = nil
        // None of these describe the build that is starting. A stale
        // `cancellingApp` in particular would make this build report itself as
        // canceled the moment it ended, however it ended.
        cancellingApp = nil
        stoppedAt[appName] = nil
        buildStage = nil
        deviceExposure = DeviceExposure(delivery: effectiveDelivery)
        notifiedThisBuild.remove(appName)
    }

    /// Only the build that claimed the flag may release it, so a late `defer`
    /// from a finished build can't clear the marker for one that started after.
    ///
    /// Note what this does *not* touch: `encryptedBinaries` or `buildFailures`.
    /// The build that finds encrypted binaries ends `ok: true` — that is the
    /// whole shape of that defect — so a finding cleared at the end of a build
    /// would be cleared by the very success that hides it.
    ///
    /// Nor `cancellingApp`: this runs from `runBuildProcess`'s `defer`, and the
    /// caller reads that flag *after* the subprocess returns to decide whether
    /// the non-zero exit it is holding was a failure or the user. Clearing it
    /// here would answer "failure" every time. `beginBuild` owns it instead.
    func endBuild(appName: String) {
        guard buildingApp == appName else { return }
        buildingApp = nil
        buildStage = nil
        buildTask = nil
    }

    /// Run a build in a Task the store keeps a handle on, claiming the build
    /// slot synchronously. Returns false when a build is already in flight.
    ///
    /// The handle is the entire point: `AppDetailView` used to own it in a
    /// local `Task {}`, which meant the only object that could stop a build
    /// was the pane that started it — and that pane is destroyed by any
    /// sidebar-section change. A build that wedged after the user clicked
    /// away could not be stopped at all, short of quitting the app.
    ///
    /// The slot is claimed *here*, before the Task is created, rather than
    /// inside the work body. `BuildButton` is gated on `buildingApp != nil`,
    /// but a claim made inside the Task does not land until the main actor next
    /// runs it — leaving a window in which the button is still enabled. A
    /// second activation in that window (a double-click, a held Return on the
    /// focused button) reassigned `buildTask` to the newcomer and dropped the
    /// first Task's handle on the floor: two `graft` processes signing
    /// and pushing the same app concurrently, with Stop able to reach only one
    /// of them.
    @discardableResult
    func launchBuild(
        appName: String,
        delivery: EffectiveDelivery,
        _ work: @escaping @MainActor () async -> Void
    ) -> Bool {
        guard busy == nil else { return false }
        // Set before `beginBuild`, which derives the run's device exposure from it.
        effectiveDelivery = delivery
        beginBuild(appName: appName)
        // macOS mustn't kill the app under a build: the CLI would be left
        // signing or installing with nobody reading it (`TerminationHold`).
        // Quit Anyway stops it as Stop does, while it is still this build.
        let hold = self.hold
        buildTask = Task { @MainActor [weak self] in
            let ticket = hold.begin(TerminationHold.building) { [weak self] in
                guard let self, self.buildingApp == appName else { return }
                self.cancelBuild(appName: appName)
            }
            defer { hold.end(ticket) }
            await work()
        }
        return true
    }

    /// Stop the running build.
    ///
    /// Canceling the Task is what reaches the subprocess — see `buildTask`.
    /// The flag is set *before* the cancel so the completion path, which can
    /// run immediately, already knows why the CLI died.
    ///
    /// - Parameter appName: the build the *caller* meant, re-checked here
    ///   rather than trusted. Both confirmation dialogs can sit open across
    ///   the end of one build and the start of another — the menu that opened
    ///   them was only a snapshot of that moment — and a Stop that then killed
    ///   whatever happened to be running would be the most destructive thing
    ///   in the app firing on the wrong target. Same re-check as
    ///   `SidebarView.remove`. Passing nil means "whatever is building",
    ///   which no UI does today.
    func cancelBuild(appName: String? = nil) {
        guard let buildingApp else {
            // Said out loud, not swallowed. The confirmation sheet warns that
            // stopping now may leave a partial .ipa on the phone; if the build
            // finished while that sheet was open, a silent no-op leaves the
            // user believing it did, and the next thing they do is delete a
            // file that was delivered correctly. Same channel and same reason
            // as `SidebarView.remove`'s stale-confirmation re-check.
            lastError = "That build already finished — nothing was stopped, "
                + "and whatever it delivered is intact."
            return
        }
        guard appName == nil || appName == buildingApp else {
            lastError = "\(appName ?? "That app") isn't building any more. "
                + "\(buildingApp) is building now, and was left alone."
            return
        }
        cancellingApp = buildingApp
        buildTask?.cancel()
    }

    /// Whether the non-zero exit the caller is holding is the user's own doing.
    func wasCancelled(_ appName: String) -> Bool { cancellingApp == appName }

    /// What stopping the build right now actually costs, as one sentence for a
    /// confirmation dialog.
    ///
    /// On the store rather than in a view because two views offer the cancel —
    /// the detail pane's Stop button and the sidebar's context menu — and a
    /// warning phrased twice is a warning that will eventually disagree with
    /// itself. It also puts the copy where a test can read it, the same reason
    /// `DeliveryKind.stateLabel` lives on the model.
    ///
    /// The Stop is a SIGTERM, and the CLI runs the whole build under
    /// `lc_manage.exit_on_termination`, which turns it into `Stopped` — a
    /// `SystemExit` — so the build unwinds rather than dying where it stands:
    /// `_build_one`'s `finally` removes `tmp_root` (often several hundred MB).
    /// The temp files survive only if that unwinding outlasts `CLIRunner`'s
    /// grace — `cliStopGrace` after the SIGTERM it SIGKILLs the process. That
    /// much is true at every stage, and is the second sentence. (It used to
    /// say graft was "killed outright" and the directory "left behind", from
    /// when the CLI took SIGTERM's default.) What changes is whether anything
    /// had started reaching the *phone*, which is the part that matters — it
    /// is the only consequence the user has to undo by hand.
    ///
    /// Worth stating plainly: none of this is new risk. Force-quitting the app
    /// does all of the same damage with no warning at all, and before there was
    /// a Stop button that was the only way to end a build.
    var cancelConsequence: String {
        deviceExposure.cancelWarning + " graft stops and removes its temp files; "
            + "they are left behind only if that takes longer than the "
            + "\(Int(cliStopGrace)) seconds it is given before it is killed."
    }

    /// Record a build the user stopped.
    ///
    /// Marks it rather than clearing `buildFailures`, which is what this did
    /// first and which was wrong in both directions. Nilling the entry did not
    /// remove the failure card — `AppDetailView.buildFailureMessage` falls
    /// through to `lastBuild`, so it *revealed the previous build's* failure
    /// under an amber "Stopped." And when the CLI had genuinely failed in the
    /// second before the kill landed, nilling it discarded the only record of
    /// why, leaving nothing on screen to explain a build that really did break.
    ///
    /// So a recorded failure is kept: the user stopped a build that was already
    /// failing, and the reason is still the useful thing to show. The marker is
    /// what tells the pane not to invent a card from an older build.
    func noteBuildCancelled(appName: String) {
        // Recorded, not suppressed. An earlier attempt gated the pane's whole
        // fall-through to `lastBuild` on a stop marker that nothing cleared
        // except that app's next *successful* launch — so one Stop hid a
        // genuine earlier failure ("cyan failed: no such file") for the rest of
        // the session. The pane now reads `lastStop(of:)` and *relabels* the
        // older failure rather than hiding it.
        stoppedAt[appName] = Date()
        // Belt and braces with `notify`'s own guard: if a `build-end` raced
        // the kill and already banner'd, at least nothing further will.
        notifiedThisBuild.insert(appName)
    }

    /// Fold one build-stream event into the records that have to outlive the
    /// panel rendering it.
    ///
    /// Called by the build panel for every event, *before* its own "is this
    /// still the app on screen" guard, because the build the user navigated
    /// away from is the one that needs this most — that user is the one about
    /// to import an IPA that crashes, or to sit in front of a pane that shows
    /// no sign a build ever failed.
    ///
    /// - Parameter appName: the app whose build this stream belongs to,
    ///   captured when the build started rather than read from the view.
    func note(_ progress: BuildProgress, forBuildOf appName: String) {
        switch progress {
        case let .encrypted(record):
            // Keyed by the CLI's own `app` field, the same join
            // `delivery-state` uses.
            encryptedBinaries[record.app] = record
            encryptedReportedThisBuild.insert(record.app)
        case let .stage(name, status):
            // The verdict, and the only form an all-clear takes: the CLI emits
            // `encrypted-binaries` when it finds something and nothing at all
            // when it doesn't, so a clean check is this stage ending without
            // one. Guarded so the same build's own finding, which arrives just
            // above this line, isn't what gets cleared.
            //
            // One `case` for both readings of a stage event, where this was
            // two: a `where`-guarded case and a fall-through. A second case
            // matching `.stage` could never run once the guarded one existed,
            // so stage tracking had to live here or not at all.
            if name == CLIContract.Stage.decryptionCheck, status == "end",
               !encryptedReportedThisBuild.contains(appName) {
                encryptedBinaries[appName] = nil
            }
            // An `end` means the CLI is between stages, and it must clear the
            // stage as surely as a `start` sets it. Setting only on `start`
            // was the defect: after `lc-push end` the CLI still runs
            // `_record_delivery_if_pushed`, retention pruning (seconds on a
            // full `built_dir`) and the history write before `build-end`, and
            // for that whole window `cancelConsequence` went on saying "it is
            // writing the .ipa into LiveContainer right now — delete that
            // before you import anything" about a push that had completed
            // correctly. Following that advice deletes a good delivery.
            //
            // Guarded on the name so a late `end` from a stage the CLI has
            // already moved past cannot blank the one now running.
            if status == "start" {
                buildStage = name
            } else if status == "end", buildStage == name {
                buildStage = nil
            }
            // `buildStage` is only ever the raw label for "Stopped during cyan."
            // Everything about the *device* lives in one folded value, so the
            // warning and the aftermath cannot read different flags.
            deviceExposure = deviceExposure.advanced(stage: name, status: status)
        case let .finished(ok, error, elapsed):
            buildFailures[appName] = ok ? nil : (error ?? "failed")
            notify(appName, outcome(ok: ok, error: error, elapsed: elapsed, of: appName))
        case .stopped:
            // C-STOP: the CLI was stopped, and says so. A stop, whoever sent
            // the signal — Stop here, or a `kill` from Terminal — so it is
            // read exactly as one: no failure card (`buildFailures` is left as
            // `beginBuild` cleared it), no "failed" banner, and the build is
            // marked canceled so the pane's guard drops whatever the stream
            // still has queued behind this line. Only while this build holds
            // the slot: a late event from a finished build must not mark the
            // next one canceled.
            if buildingApp == appName { cancellingApp = appName }
            notify(appName, .cancelled)
        case .start, .log, .pruned, .liveContainerChoice, .exported:
            // Transcript-only. `liveContainerChoice` is deliberately here and
            // not given a card of its own: unlike an encrypted binary it is
            // not a standing property of the artifact — it describes one push,
            // it is only actionable in the minute after that push, and the
            // next build re-decides it from what is installed then.
            // `exported` likewise: the pane that started the export reads it
            // off the finished stream for its confirmation, and a banner
            // already says the run ended (`EffectiveDelivery.notDelivered`).
            break
        }
    }

    /// A build that failed without the event stream accounting for it — the
    /// CLI raising before `build-start`, or the subprocess never launching.
    ///
    /// Separate from `note(_:forBuildOf:)` because there is no event to fold:
    /// this is what the panel knows from the exit status and stderr.
    func noteBuildFailure(appName: String, message: String) {
        buildFailures[appName] = message
        notify(appName, outcome(ok: false, error: message, elapsed: nil, of: appName))
    }

    /// Classify how a build ended. The store decides *what happened*;
    /// `BuildNotifier.note` decides what to say about it — including that a
    /// stop is worth no banner at all.
    ///
    /// This split is what makes `Outcome.cancelled` reachable. The store used
    /// to return early on `wasCancelled` before ever constructing an outcome,
    /// so the notifier's `case .cancelled: return nil` was dead in production
    /// and the rule it encodes was really being enforced by a guard two files
    /// away. Now the rule sits once, in the exhaustive switch that owns banner
    /// policy, and the test for it guards live code.
    private func outcome(
        ok: Bool, error: String?, elapsed: Double?, of appName: String
    ) -> BuildNotifier.Outcome {
        if wasCancelled(appName) { return .cancelled }
        return ok ? .succeeded(elapsed: elapsed) : .failed(message: error)
    }

    /// Put the build's outcome in front of a user who isn't looking at the app.
    ///
    /// Here rather than in `AppDetailView` for the reason every other record on
    /// this store is: the pane is destroyed by a sidebar-section change, and a
    /// build the user walked away from is precisely the one whose ending they
    /// need told to them. Routing it through the view would deliver a banner
    /// only when the user was already watching.
    private func notify(_ appName: String, _ outcome: BuildNotifier.Outcome) {
        // A cancel in flight makes every outcome from this build the user's
        // own doing, whatever the CLI managed to say on its way down — a
        // `build-end` can still win the race against SIGTERM.
        // `insert` reports whether it was new, which is the once-per-build
        // gate: see `notifiedThisBuild`.
        guard notifiedThisBuild.insert(appName).inserted else { return }
        // `effectiveDelivery`, captured from the build options at launch —
        // deliberately not `apps.first { … }?.delivery`, which is the app's
        // *configured* mode and says nothing about whether this run pushed
        // anything. See `EffectiveDelivery`.
        postNote(
            BuildNotifier.note(app: appName, outcome: outcome, delivery: effectiveDelivery)
        )
    }

    /// Claim the device-check slot, or refuse when one is already running.
    ///
    /// Returns false rather than queueing: two concurrent `--reconcile`
    /// processes race on `log.sqlite`, and `mark_reconciled` is a write, so the
    /// loser surfaces as "couldn't update the delivery log: database is
    /// locked" — an alarming line about a check that was only ever redundant.
    @discardableResult
    func beginDeviceCheck(appName: String) -> Bool {
        guard busy == nil else { return false }
        checkingApp = appName
        deviceCheckStatus = nil
        return true
    }

    /// Whether any exclusive CLI operation holds the store.
    ///
    /// The rule "a build and a device check may not run at once" used to live
    /// only in view modifiers — three copies of
    /// `store.buildingApp != nil || store.checkingApp != nil` across two files
    /// — so the store itself would happily start either during the other, and
    /// no test could express the invariant because it wasn't there. Both
    /// claims now go through this, and the views read it instead of
    /// re-deriving it.
    var busy: Busy? {
        if let buildingApp { return .building(buildingApp) }
        if let checkingApp { return .checking(checkingApp) }
        return nil
    }

    /// Which exclusive operation holds the store, and for which app.
    ///
    /// A `Bool` here meant every consumer that had to *explain* the
    /// disablement re-derived the distinction, and one got it wrong: with a
    /// check running, the Check-device button's own tooltip fell through to
    /// the build button's and read "Build and install example" on a disabled
    /// control. The gate and the sentence now come from one value.
    enum Busy: Equatable {
        case building(String)
        case checking(String)

        var explanation: String {
            switch self {
            case let .building(app): return "\(app) is building — wait for it to finish"
            case .checking: return "Checking the device — wait for it to finish"
            }
        }
    }

    /// Only the check that claimed the slot may release it.
    func endDeviceCheck(appName: String) {
        guard checkingApp == appName else { return }
        checkingApp = nil
        deviceCheckStatus = nil
    }

    /// Read the app list, and the delivery state that rides along with it.
    ///
    /// One subprocess, deliberately. `--list` emits a `delivery-state` beside
    /// every `app`, and it is not cheap: `find_in_cache` cannot short-circuit
    /// under a `latest` pin, so it opens the central directory of every cached
    /// version, putting a realistic refresh (3 apps × 3 versions) at ~250–300
    /// ms. Asking for the badge separately would pay that twice on every
    /// refresh — so the badge arrives *with* the list rather than the list
    /// waiting on it. `DeliveryStateTests` pins the invocation count.
    func loadApps() async throws -> [AppRecord] {
        let events = try await runner.runEvents(cliArgs, failing: "Load apps")

        account = events.lazy.compactMap { event -> AccountMode? in
            if case let .account(mode) = event { return mode }
            return nil
        }.first ?? events.lazy.compactMap { event -> AccountMode? in
            if case let .app(record) = event { return record.account }
            return nil
        }.first

        apps = events.compactMap { event -> AppRecord? in
            if case let .app(record) = event { return record }
            return nil
        }.sorted { $0.name < $1.name }

        // The join is across two spellings: the `app` event calls the config
        // key `name`, `delivery-state` calls it `app`. Both are pinned by
        // Python tests, so match on the pair rather than "fixing" either.
        //
        // `uniquingKeysWith`, not `uniqueKeysWithValues`: the latter traps on a
        // repeated key, and this dictionary is built from a subprocess's
        // stdout — the one input this process doesn't control. A duplicated app
        // should cost a badge, not the GUI.
        deliveryStates = Dictionary(
            events.compactMap { event -> (String, DeliveryStateRecord)? in
                if case let .deliveryState(record) = event { return (record.app, record) }
                return nil
            },
            uniquingKeysWith: { _, latest in latest }
        )

        // Every other per-app record this store holds, bounded by the app list
        // it is keyed against.
        //
        // `deliveryStates` above gets this for free — it is rebuilt wholesale
        // from the same events, so an app that is gone simply isn't in it. The
        // three below are written by the build stream and purged by nothing,
        // and `removeApp` → `mutate` → `reload()` lands exactly here: remove an
        // app after a failed or encrypted build, re-add it (the ordinary
        // fix-a-botched-add loop, and the CLI dedups back to the same YAML
        // key), and the new app's pane opens with a red "Last build failed"
        // card and a "will crash on launch" warning describing a build of the
        // *previous* app. Surviving a torn-down view is what putting those
        // cards on the store bought; surviving the app itself is not.
        //
        // Their three lifetimes are deliberately different — per-build for
        // `encryptedReportedThisBuild`, per-session for the other two — and
        // this does not flatten them. It is the one bound all three share: a
        // record about an app that no longer exists describes nothing.
        //
        // Only the two session-scoped maps had the defect. The marker is
        // already bounded per app per build by `beginBuild`, so no sequence
        // the UI can produce observes a stale one — it is purged here for the
        // keyspace, not for a bug, and there is deliberately no test claiming
        // otherwise: one would pass with this line deleted.
        let live = Set(apps.map(\.name))
        encryptedBinaries = encryptedBinaries.filter { live.contains($0.key) }
        buildFailures = buildFailures.filter { live.contains($0.key) }
        encryptedReportedThisBuild.formIntersection(live)

        return apps
    }

    /// What the user still has to do in Terminal before anything here works,
    /// or nil once the app list has loaded. Set only by `checkSetup`.
    @Published private(set) var setupNeed: SetupNeed?

    /// Whether `checkSetup` is running — the setup screen's Check again shows
    /// it, and refuses a second press while it does.
    @Published private(set) var isCheckingSetup = false
    /// The last Check again found the login shell asked too recently to ask
    /// again, and used its last answer (`ShellEnvironment.askedTooRecentlyNote`).
    @Published private(set) var shellAskedTooRecently = false

    /// The app's first load, and the setup screen's Check again.
    ///
    /// `loadApps()` with its failure sorted: one that means "not set up yet"
    /// (`SetupNeed`) becomes `setupNeed` and the setup screen, and anything else
    /// goes to `lastError` and its alert, as every failure here always has. A
    /// success clears `setupNeed`, which is what puts the app back.
    ///
    /// Returns the apps when the load succeeded, nil when it did not — the
    /// caller's "did it work", without a second copy of the state to agree with
    /// this one. A canceled load leaves both channels alone: nobody is waiting
    /// on it, and a SIGTERM'd `--list` is not evidence of anything.
    ///
    /// - Parameters:
    ///   - refreshingEnvironment: ask the login shell again first. Check again
    ///     passes true: the user pressing it has usually just changed their
    ///     setup in Terminal (`pipx ensurepath`, a PATH line), and the answer
    ///     read at launch cannot have seen that.
    ///   - toolChecks: `--check`'s rows, or nil when they could not be read.
    ///     Asked only when the user is on the setup screen and the config now
    ///     loads: that is the moment a new user would otherwise learn which
    ///     tools are missing from a failed build. Missing ones become step 3
    ///     (`SetupNeed.tools`). Someone already using the app is not put back
    ///     on a setup screen for a tool that went missing — the window's
    ///     notice says so instead.
    @discardableResult
    func checkSetup(
        refreshingEnvironment: Bool = false,
        toolChecks: (@MainActor () async -> [PreflightCheck]?)? = nil
    ) async -> [AppRecord]? {
        guard !isCheckingSetup else { return nil }
        isCheckingSetup = true
        defer { isCheckingSetup = false }
        shellAskedTooRecently = refreshingEnvironment && !runner.refreshEnvironment()
        let onSetupScreen = setupNeed != nil
        do {
            let loaded = try await loadApps()
            if onSetupScreen, !toolsStepSkipped, let toolChecks,
               let checks = await toolChecks() {
                let missing = MissingTool.all(in: checks)
                if !missing.isEmpty {
                    setupNeed = .tools(missing)
                    return loaded
                }
            }
            setupNeed = nil
            return loaded
        } catch {
            guard !Task.isCancelled else { return nil }
            if let need = SetupNeed(error: error) {
                setupNeed = need
            } else {
                setupNeed = nil
                lastError = error.localizedDescription
            }
            return nil
        }
    }

    /// The user chose to go on without the tools step 3 lists. For the
    /// session: they have seen the list, and the window's notice keeps
    /// pointing at Settings until the tools are there.
    @Published private(set) var toolsStepSkipped = false

    /// Leave the tools step for the app. Only from that step — every other
    /// step is one the app cannot work without.
    func continueWithoutTools() {
        guard case .tools = setupNeed else { return }
        toolsStepSkipped = true
        setupNeed = nil
    }

    /// `CLIStore`'s post-mutation refresh: for this store, the apps list.
    func reload() async throws {
        _ = try await loadApps()
    }

    /// Re-read the list after a CLI call that changed *the phone* rather than
    /// config.yaml — a build, or a `--reinstall-build`.
    ///
    /// `mutate` already reloads after every config write, which is why
    /// add/remove-app and attach-tweak happened to clear a stale badge. A
    /// build goes through `runner.stream` and writes no config, so nothing
    /// reloaded and `deliveryStates` kept whatever `loadApps()` found at
    /// launch. The user was told "Changed since the last delivery" seconds
    /// after making the phone current, for the rest of the session, and the
    /// obvious response to that is to build again.
    ///
    /// Deliberately unconditional on the build's outcome. The CLI records the
    /// delivery the moment the push returns, and several stages run after that
    /// point (retention, the build-log write) — so a build that ends `ok:
    /// false` may still have delivered, and skipping the refresh there would
    /// leave the badge wrong in exactly the case the user is most likely to be
    /// staring at it.
    ///
    /// Never throws: the CLI work it follows already landed on disk and on the
    /// device, so a failure here is "it worked, the badge is stale" — the same
    /// story `mutate` tells, routed to the same channel. A failed refresh also
    /// leaves the previous badges standing rather than blanking them; stale is
    /// closer to the truth than absent.
    func refreshDeliveryStates() async {
        do {
            _ = try await loadApps()
        } catch {
            // Cancellation is not a failure the user needs an alert about:
            // `runOnce` reports the SIGTERM'd child as a non-zero exit, and
            // work nobody is waiting on should unwind quietly. Same reasoning
            // as `streamEvents`' cancellation check.
            //
            // This is a *quiet* unwind, not a harmless one — see
            // `refreshDeliveryStatesDetached`, which exists because a caller
            // whose Task dies at the finish line ends up here and the badges
            // it was refreshing stay wrong with nothing said about it.
            guard !Task.isCancelled else { return }
            lastError = "Couldn't refresh the delivery badges: "
                + error.localizedDescription
        }
    }

    /// Start a delivery-badge refresh that outlives the caller's Task, and
    /// hand back the Task so a test (or a caller that wants to wait) can.
    ///
    /// `refreshDeliveryStates()` awaited inline inherits the cancellation of
    /// whatever started it, and for the re-install sheet that is a race it
    /// loses every time: the sheet re-enables Close the moment `build-end`
    /// lands, dismissal cancels the `.task` the refresh is running in, and the
    /// refresh takes ~250–300 ms (`--list` opens the central directory of every
    /// cached version). The cancellation is then swallowed by design, and the
    /// sidebar goes on showing a green "current" for an app whose phone holds
    /// an older archived build the CLI has already recorded as stale. Nothing
    /// re-reads it until the next launch.
    ///
    /// An unstructured `Task` inherits the actor and priority but *not*
    /// cancellation, which is exactly the difference wanted: the push already
    /// happened, so the work is no longer the sheet's to cancel.
    @discardableResult
    func refreshDeliveryStatesDetached() -> Task<Void, Never> {
        Task { await self.refreshDeliveryStates() }
    }

    /// What a device check found: the states it re-reported, and whatever it
    /// couldn't do.
    ///
    /// `warnings` is not an afterthought — it is the only thing separating
    /// "checked, and the file is there" from "never got as far as the phone".
    /// `--reconcile` is best-effort by design: an unreachable device, a row
    /// pushed to a different phone, a locked database all leave the record
    /// untouched and still print the local answer. So the states below look
    /// identical in both cases, and a caller that drops the warnings shows a
    /// button that appears to confirm the delivery when nothing was checked.
    struct ReconcileOutcome {
        let states: [DeliveryStateRecord]
        let warnings: [String]
    }

    /// Check the connected device for what was last delivered, then fold the
    /// re-reported states back into the badges.
    ///
    /// This is the one path in the app where a badge can be contradicted by
    /// the phone rather than by our own record. Everywhere else — `--list`,
    /// `refreshDeliveryStates` — the CLI compares an inputs digest against the
    /// delivery log and never looks at the device at all.
    ///
    /// Merged into `deliveryStates`, not assigned over it, which `loadApps()`
    /// deliberately does the other way. The difference is what each call
    /// emits: `--list` reports every app, so a wholesale replacement is how an
    /// app that is *gone* loses its badge. `--reconcile --app example` reports
    /// example and nothing else, so the same assignment would blank every
    /// other app in the sidebar — states the CLI never claimed had changed.
    ///
    /// Not `mutate`: this writes to the delivery log rather than config.yaml,
    /// so `apps` is unchanged and a reload here would be a second `--list`
    /// subprocess (~250–300 ms) that could only re-derive the states this call
    /// already returned — and re-derive them from the row this call just
    /// updated, so it would agree and cost the wait for nothing.
    /// - Parameter deviceUDID: the phone to check, when the user has picked one
    ///   in Build options. Omitting it is not neutral: `_do_reconcile` falls
    ///   back to `select_device(None)`, which takes the *first* connected
    ///   device, and `_reconcile_app` then bails silently on the udid mismatch
    ///   (no `_warn`, by design — a different phone is not evidence). The check
    ///   returns clean, `warnings` is empty, and the pane reports "the record
    ///   still stands" having looked at the wrong phone.
    @discardableResult
    func reconcile(
        appName: String? = nil,
        deviceUDID: String? = nil,
        argsOverride: [String]? = nil
    ) async throws -> ReconcileOutcome {
        var args = [Flag.json, Flag.reconcile]
        if let appName { args += [Flag.app, appName] }
        if let deviceUDID, !deviceUDID.isEmpty { args += [Flag.device, deviceUDID] }
        // Streamed, so the phone's own prompt shows while the check waits on
        // it — up to 60 s for "tap Trust" — rather than after it is over.
        let events = try await runner.streamEvents(
            argsOverride ?? args, failing: "Check the device"
        ) { [weak self] event in
            // Only while a check holds the slot: a line that lands after it
            // ended must not stand under the verdict.
            guard let self, self.checkingApp != nil,
                  case let .log(source, line) = event, source == CLIContract.deviceLogSource
            else { return }
            self.deviceCheckStatus = line
        }

        let states = events.compactMap { event -> DeliveryStateRecord? in
            if case let .deliveryState(record) = event { return record }
            return nil
        }
        for state in states {
            deliveryStates[state.app] = state
        }

        // Every `log` line, not just the ones sourced "warning". The reconcile
        // path speaks to a machine consumer through exactly one function —
        // `_warn` — because stdout carries the JSON event stream and prose
        // can't go there; so on this verb a `log` event *is* the CLI telling
        // the user something went unchecked. Filtering by source would be a
        // guess about a field this side has no contract for (`--describe`
        // reports events, not their fields), and the failure it buys is the
        // silent one: a check that quietly did nothing.
        //
        // The sources that are excluded are the ones the CLI has a contract
        // for. `startup` lines (C-NOTICE) are written before the verb runs —
        // a renamed environment variable, a moved data directory — and are
        // about the machine, not the check. Counted here, "TB_SSH_PASS is set
        // but no longer read" became every device check's result. `device`
        // lines are the phone's prompt while the check waits on it, shown
        // live above; counted, a check that succeeded once the user tapped
        // Trust read as one that hadn't (fix-check #19, #21).
        let notResults: Set<String> = [CLIContract.startupLogSource, CLIContract.deviceLogSource]
        let warnings = events.compactMap { event -> String? in
            if case let .log(source, line) = event, !notResults.contains(source) {
                return line
            }
            return nil
        }

        return ReconcileOutcome(states: states, warnings: warnings)
    }

    /// Add an app from an App Store URL (or numeric ID), then reload the
    /// apps list so the sidebar reflects the new entry. Returns the YAML key
    /// the CLI assigned (post-dedup), or nil if no `app-added` event was seen.
    /// `addArgsOverride` exists for tests; in production we invoke the real
    /// CLI with `--json --add-app <url> --delivery <mode>`.
    ///
    /// `delivery` has no default, and must not get one. It defaulted to
    /// `.sideload`, which a free Apple ID cannot build, so any caller that
    /// leaned on it sent `--delivery sideload` and got the paid-account
    /// refusal. The caller decides, and on a free Apple ID that is
    /// `AddAppSheet.delivery(chosen:account:)`'s LiveContainer.
    @discardableResult
    func addApp(
        appStoreURL: String,
        delivery: DeliveryMode,
        addArgsOverride: [String]? = nil
    ) async throws -> String? {
        let trimmed = appStoreURL.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return nil }
        let args = addArgsOverride ?? [
            Flag.json, Flag.addApp, trimmed,
            Flag.delivery, delivery.rawValue,
        ]
        return try await addAndReload(args)
    }

    /// Add an app via the explicit-fields path (no iTunes lookup).
    @discardableResult
    func addAppManual(
        name: String,
        bundleId: String,
        displayName: String?,
        delivery: DeliveryMode,
        storeCountry: String? = nil,
        addArgsOverride: [String]? = nil
    ) async throws -> String? {
        var args = [
            Flag.json, Flag.addAppManual,
            Flag.name, name,
            Flag.bundleId, bundleId,
            Flag.delivery, delivery.rawValue,
        ]
        if let displayName, !displayName.isEmpty {
            args += [Flag.displayName, displayName]
        }
        if let storeCountry {
            args += [Flag.storeCountry, storeCountry]
        }
        return try await addAndReload(addArgsOverride ?? args)
    }

    /// The tail both add paths share: they differ only in how they name the
    /// app, and both answer with the key the CLI settled on.
    private func addAndReload(_ args: [String]) async throws -> String? {
        try await mutate(args, failing: "Add app", succeeded: "App added") { event in
            if case let .appAdded(record) = event { return record.name }
            return nil
        }
    }

    /// What a drop staged, and whether the CLI had to decrypt it first — the
    /// difference between "your file is in the cache" and "a decrypted copy of
    /// your file is", which the app pane's confirmation says.
    struct ProvidedIPA: Equatable {
        let record: CachedIPARecord
        /// The CLI reported decrypting it (`appstore-decrypted`). Read from
        /// the returned events, not the `onStage` callbacks, which are MainActor
        /// hops that can land after this returns.
        let decrypted: Bool
        /// The CLI's warnings on the way in (`log` events sourced
        /// `CLIContract.warningLogSource`) — after "Use it anyway…", the line
        /// saying it staged a decrypt UnfairPlay could not verify. The pane
        /// keeps it on screen under the confirmation.
        var warnings: [String] = []
    }

    /// Validate `fileURL` and stage it in cache_dir for the given app.
    /// Returns the CLI's `ipa-provided` record on success, or nil if no event
    /// was emitted.
    ///
    /// Deliberately not `mutate`: this writes into cache_dir, not config.yaml,
    /// so `apps` is unchanged and a reload here would be a second `--list`
    /// subprocess that can only produce what's already loaded.
    ///
    /// Streamed, so `onStage` can say "Decrypting…": the CLI now decrypts a
    /// drop that came straight from the App Store (UnfairPlay), which takes
    /// seconds where a plain copy takes none. The argv is the same either way —
    /// the CLI decides by the IPA's cryptid, not this app — and a decrypted
    /// drop reports no stage at all.
    ///
    /// `allowUnverified` is "Use it anyway…": the same drop again, after the
    /// CLI refused an unverified decrypt and the user confirmed. That refusal
    /// throws `UnverifiedDecrypt` rather than the CLI's own error.
    @discardableResult
    func provideIPA(
        appName: String,
        fileURL: URL,
        allowUnverified: Bool = false,
        argsOverride: [String]? = nil,
        onStage: (@MainActor (AppStoreStage) -> Void)? = nil
    ) async throws -> ProvidedIPA? {
        let args = argsOverride ?? Self.provideArgs(
            appName: appName, fileURL: fileURL, allowUnverified: allowUnverified
        )
        let events = try await runner.streamEvents(
            args, failing: "Provide IPA", refusal: .unverifiedDecrypt
        ) { event in
            if case let .appStoreStage(record) = event { onStage?(record.stage) }
        }
        return Self.provided(in: events)
    }

    static func provideArgs(appName: String, fileURL: URL, allowUnverified: Bool = false) -> [String] {
        [Flag.json, Flag.provideIPA, fileURL.path, Flag.app, appName]
            + (allowUnverified ? [Flag.allowUnverified] : [])
    }

    /// The `ipa-provided` record in a stream, with whether the same stream
    /// reported a decrypt. Shared with the App Store get, which stages the
    /// same way and reports the same two events.
    static func provided(in events: [BuildEvent]) -> ProvidedIPA? {
        let record = events.lazy.compactMap { event -> CachedIPARecord? in
            if case let .ipaProvided(rec) = event { return rec }
            return nil
        }.first
        guard let record else { return nil }
        let decrypted = events.contains { event in
            if case .appStoreDecrypted = event { return true }
            return false
        }
        return ProvidedIPA(record: record, decrypted: decrypted, warnings: events.warningLines)
    }

    /// Look up what IPA the build pipeline would use for the given app.
    /// Returns the CLI's `cached-ipa` record (which may have `exists: false`)
    /// or nil if no event was emitted. A pure read — no reload.
    func cachedIPA(
        appName: String,
        argsOverride: [String]? = nil
    ) async throws -> CachedIPARecord? {
        let args = argsOverride ?? [Flag.json, Flag.cachedIPA, appName]
        return try await runner.run(args, failing: "Cached IPA lookup") { event in
            if case let .cachedIPA(rec) = event { return rec }
            return nil
        }
    }

    /// Attach a tweak (.deb filename already in tweaks_dir) to an app's
    /// tweaks list. Returns the CLI's `tweak-attached` record on success,
    /// or nil if no event was emitted. Reloads `apps` so the UI reflects
    /// the new list immediately.
    @discardableResult
    func attachTweak(
        appName: String, filename: String,
        argsOverride: [String]? = nil
    ) async throws -> TweakMutationRecord? {
        let args = argsOverride ?? [
            Flag.json, Flag.attachTweak, filename, Flag.app, appName,
        ]
        return try await mutate(args, failing: "Tweak mutation", succeeded: "Tweak updated") { event in
            if case let .tweakAttached(rec) = event { return rec }
            return nil
        }
    }

    /// Detach a tweak from an app's tweaks list. Idempotent at the CLI:
    /// detaching a filename that isn't attached is a successful no-op.
    @discardableResult
    func detachTweak(
        appName: String, filename: String,
        argsOverride: [String]? = nil
    ) async throws -> TweakMutationRecord? {
        let args = argsOverride ?? [
            Flag.json, Flag.detachTweak, filename, Flag.app, appName,
        ]
        return try await mutate(args, failing: "Tweak mutation", succeeded: "Tweak updated") { event in
            if case let .tweakDetached(rec) = event { return rec }
            return nil
        }
    }

    /// Remove an app from config.yaml. Cached IPAs, archived builds, and
    /// sqlite history are deliberately kept — this only drops the entry.
    @discardableResult
    func removeApp(
        appName: String,
        argsOverride: [String]? = nil
    ) async throws -> AppRemovedRecord? {
        let args = argsOverride ?? [Flag.json, Flag.removeApp, appName]
        return try await mutate(args, failing: "Remove app", succeeded: "App removed") { event in
            if case let .appRemoved(rec) = event { return rec }
            return nil
        }
    }

    /// Patch an app's options. Pass only what changed — the CLI writes only
    /// the fields it receives.
    ///
    /// The CLI's `app-updated` event carries `changed`: the exact field names
    /// it actually wrote. We build the set of fields *we* asked for (using
    /// the CLI's own key spellings) and reconcile it against `changed` after
    /// the call. A field we requested that isn't in `changed` means the CLI
    /// silently declined to apply it — that's the class of bug this guards
    /// against (see the `--delivery` default-vs-explicit footgun), so it
    /// throws naming the dropped fields rather than reporting success.
    @discardableResult
    func updateAppOptions(
        appName: String,
        preferredVersion: String? = nil,
        displayName: String? = nil,
        delivery: DeliveryMode? = nil,
        keepBundleId: Bool? = nil,
        stripExtensions: Bool? = nil,
        stripSupportedDevices: Bool? = nil,
        compressionLevel: Int? = nil,
        lcPin: AppOptionsDraft.LCPin? = nil,
        autoProfile: Bool? = nil,
        storeCountry: String? = nil,
        argsOverride: [String]? = nil
    ) async throws -> AppUpdatedRecord? {
        var args = [Flag.json, Flag.setAppOptions, Flag.app, appName]
        // Field names come from CLIContract, not literals: they must match the
        // CLI's config-key spellings exactly or the reconciliation below
        // reports a phantom failure. CLIContractTests checks them against the
        // CLI's `--describe` output.
        var requestedFields: Set<String> = []
        func request(_ field: CLIContract.AppOptionField) {
            requestedFields.insert(field.rawValue)
        }
        if let preferredVersion {
            args += [Flag.preferredVersion, preferredVersion]
            request(.preferredVersion)
        }
        if let displayName {
            args += [Flag.displayName, displayName]
            request(.displayName)
        }
        if let delivery {
            args += [Flag.delivery, delivery.rawValue]
            request(.delivery)
        }
        if let keepBundleId {
            args.append(keepBundleId ? Flag.keepBundleId : Flag.noKeepBundleId)
            request(.keepBundleId)
        }
        if let stripExtensions {
            args.append(stripExtensions ? Flag.stripExtensions : Flag.noStripExtensions)
            request(.stripExtensions)
        }
        if let stripSupportedDevices {
            args.append(stripSupportedDevices
                        ? Flag.stripSupportedDevices : Flag.noStripSupportedDevices)
            request(.stripSupportedDevices)
        }
        if let compressionLevel {
            args += [Flag.compressionLevel, String(compressionLevel)]
            request(.compressionLevel)
        }
        // C-LCPIN: setting and clearing are two flags, and both report the one
        // field name in `changed`.
        switch lcPin {
        case let .bundleId(id)?:
            args += [Flag.lcBundleId, id]
            request(.lcBundleId)
        case .automatic?:
            args.append(Flag.noLcBundleId)
            request(.lcBundleId)
        case nil:
            break
        }
        if let autoProfile {
            args.append(autoProfile ? Flag.autoProfile : Flag.noAutoProfile)
            request(.autoProfile)
        }
        if let storeCountry {
            args += [Flag.storeCountry, storeCountry]
            request(.storeCountry)
        }

        let events = try await mutateEvents(
            argsOverride ?? args, failing: "Update app", succeeded: "App updated"
        )
        var record = events.lazy.compactMap { event -> AppUpdatedRecord? in
            if case let .appUpdated(rec) = event { return rec }
            return nil
        }.first
        // Kept for the Edit sheet, which shows them before it closes: the
        // CLI saves an `auto_profile` an app's own `profile:` outranks, and
        // says so only here (fix-check #6/#26).
        record?.warnings = events.warningLines

        try requireApplied(requestedFields, reportedIn: record?.changed ?? [])
        return record
    }

    /// Write a sheet's pending edit: exactly the fields that differ from what
    /// config said when the sheet opened, and no others.
    ///
    /// This lives here rather than in EditAppSheet so the mapping from draft to
    /// flags is reachable from a test — it is the highest-consequence pure
    /// function in the view layer, and a stray non-nil argument here silently
    /// rewrites a field the user never touched. See
    /// `AppOptionsDraftTests.test_editing_one_field_sends_only_that_flag`.
    @discardableResult
    func updateAppOptions(
        appName: String,
        draft: AppOptionsDraft
    ) async throws -> AppUpdatedRecord? {
        try await updateAppOptions(
            appName: appName,
            preferredVersion: draft.changed(\.preferredVersion),
            displayName: draft.changed(\.displayName),
            delivery: draft.changed(\.delivery),
            keepBundleId: draft.changed(\.keepBundleId),
            stripExtensions: draft.changed(\.stripExtensions),
            stripSupportedDevices: draft.changed(\.stripSupportedDevices),
            compressionLevel: draft.changed(\.compressionLevel),
            lcPin: draft.changed(\.lcPin),
            autoProfile: draft.changed(\.autoProfile),
            // `String??`: nil is unchanged. A change to nil can't be drawn
            // (`EditAppSheet.storeChoices`), so it flattens to unchanged too.
            storeCountry: draft.changed(\.storeCountry) ?? nil
        )
    }
}
