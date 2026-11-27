import AppKit

/// What `TerminationHold` asks of the process: `ProcessInfo`'s own four
/// calls, so a test can count them instead.
protocol TerminationControl: AnyObject {
    func disableSuddenTermination()
    func enableSuddenTermination()
    func disableAutomaticTermination(_ reason: String)
    func enableAutomaticTermination(_ reason: String)
}

extension ProcessInfo: TerminationControl {}

/// Keeps macOS from ending Graft on its own, without asking, while work it
/// can't take back is running (whole-diff review, app-3), and knows what that
/// work is and how to stop it, so a quit that is asked for can be answered
/// (`QuitGuard`).
///
/// Info.plist says Graft supports sudden and automatic termination, which is
/// true while it is idle: macOS may then kill it at logout, or quit it once
/// its window is closed, without asking. In the middle of a build or a
/// re-install, an App Store get, drop or Get (free), an `--init` setup, an
/// enrollment, a LiveContainer install or a decrypt dump, that killed the app
/// and orphaned the CLI, which then aborted on a broken pipe halfway through
/// signing, staging or installing. Each of those now holds both off from
/// start to end, so macOS neither kills Graft outright nor quits it by
/// itself while one runs.
///
/// Every `begin` is matched by one `end` of the ticket it returned, on every
/// path (a `defer`, or straight after a `do`/`catch` that catches
/// everything): `ProcessInfo` counts them, and one left over would keep the
/// app from ever being terminated that way again. Ending a ticket twice
/// undoes it once.
///
/// **A quit that is asked for** (the try14 delta audit, F4). ⌘Q quits
/// Graft, and so does a logout, a restart or a shutdown: with the hold on,
/// macOS sends Graft an ordinary quit in place of the kill. `QuitGuard`
/// answers that quit, from `GraftAppDelegate.applicationShouldTerminate`.
/// With nothing held, Graft quits at once. Otherwise it asks, naming what is
/// running (`reasons`, in `activities`' words): Keep Working cancels the
/// quit, and with it the logout or restart; Quit Anyway first asks each piece
/// of work to stop through the `stop` its `begin` named, which is that work's
/// own Stop, so the CLI unwinds and cleans up as it does when the user
/// presses Stop. It then waits up to `QuitGuard.stopGrace` for the hold to
/// clear, and quits. That grace outlasts `cliStopGrace`, after which the
/// runner SIGKILLs a stopped CLI that is still running, and its children:
/// Graft is still there to do it.
///
/// Work with no Stop (`begin`'s nil `stop`: an `--init` setup that writes,
/// Remove Graft's Data) can't be stopped partway, so Quit Anyway waits for
/// it to finish however long that takes, and the question says so.
///
/// Work whose Stop waits for a step to finish (`finishing`: an install
/// `graft --update` had begun) is waited for longer, up to
/// `QuitGuard.finishGrace`, with a notice saying what Graft is finishing.
///
/// What it still doesn't stop: a Force Quit, a kill or a crash, which no app
/// can answer.
final class TerminationHold: @unchecked Sendable {
    /// The app's one hold, on the real `ProcessInfo`. Reaches nothing past
    /// this process, so stores default to it; a test passes its own.
    static let shared = TerminationHold(control: ProcessInfo.processInfo)

    static let building = "Graft is building and installing an app"
    /// A build with nothing to install: a dry run, Install skipped, or
    /// Build for this Mac (`reason(forBuild:)`).
    static let buildingOnly = "Graft is building an app"
    static let exporting = "Graft is exporting an IPA"
    static let gettingFromAppStore = "Graft is getting an app from the App Store"
    static let staging = "Graft is staging an IPA"
    static let claiming = "Graft is getting a free app for this Apple ID"
    static let settingUp = "Graft is setting up the signing account"
    static let enrolling = "Graft is setting up App Store decryption"
    static let liveContainer = "Graft is changing LiveContainer on a device"
    static let reinstalling = "Graft is re-installing a build on a device"
    static let decrypting = "Graft is decrypting an app from a device"
    static let removingData = "Graft is removing its data"
    /// `graft --update` (`AppUpdatesStore`): its Stop finishes an install it
    /// has begun first.
    static let updatingApps = "Graft is updating apps"
    /// Waiting for PlayCover's install, which the CLI does for up to two
    /// minutes after a handoff: Install to Mac… from Builds or the Cache
    /// (`StagedIPAStore.installToMac`), whose Stop ends the wait, never the
    /// handoff; and an app pane's Mac build once it has handed over, whose
    /// hold is renamed for the wait rather than saying "building an app"
    /// (`ConfigStore.noteWaitingForPlayCover`, review part 2, COPY).
    static let waitingForPlayCover = "Graft is waiting for PlayCover to install an app"

    /// Each reason in the words the quit question uses: "Quit while Graft
    /// is …?" (`QuitQuestion`).
    static let activities: [String: String] = [
        building: "building and installing an app",
        buildingOnly: "building an app",
        exporting: "exporting an IPA",
        gettingFromAppStore: "getting an app from the App Store",
        staging: "staging an IPA for a build",
        claiming: "getting a free app for your Apple ID",
        settingUp: "setting up your signing account",
        enrolling: "setting up App Store decryption",
        liveContainer: "changing LiveContainer on a device",
        reinstalling: "re-installing a build on a device",
        decrypting: "decrypting an app from a device",
        removingData: "removing its data",
        updatingApps: "updating your apps",
        waitingForPlayCover: "waiting for PlayCover to install an app",
    ]

    /// A build's reason, by what it does with what it builds: `building`
    /// when it installs it, or nothing says it won't; `buildingOnly` when an
    /// option keeps it off the device (`EffectiveDelivery.notDelivered`).
    /// Export IPA… names its own (`exporting`).
    static func reason(forBuild delivery: EffectiveDelivery) -> String {
        if case .notDelivered = delivery { return buildingOnly }
        return building
    }

    /// One `begin`, which `end` takes back.
    struct Ticket: Hashable, Sendable {
        let reason: String
        fileprivate let serial: Int
    }

    private struct Held {
        let ticket: Ticket
        let stop: (@MainActor () -> Void)?
        /// What Quit Anyway says while this work finishes a step its stop
        /// waits for (`finishing`); nil otherwise.
        var finishing: String?
        /// What the work is doing now, when it isn't what it began as
        /// (`relabel`).
        var relabeled: String?

        var reason: String { relabeled ?? ticket.reason }
    }

    private let control: TerminationControl
    private let lock = NSLock()
    private var held: [Held] = []
    private var issued = 0

    init(control: TerminationControl) {
        self.control = control
    }

    /// Sudden and automatic termination off, for `reason`, until `end` gets
    /// the ticket back.
    ///
    /// `stop` is how Quit Anyway asks this work to stop: its own Stop, so the
    /// CLI unwinds and cleans up as it does when the user presses Stop. Nil
    /// for work that has none, which Quit Anyway can only wait for.
    func begin(_ reason: String, stop: (@MainActor () -> Void)?) -> Ticket {
        lock.withLock {
            control.disableSuddenTermination()
            control.disableAutomaticTermination(reason)
            issued += 1
            let ticket = Ticket(reason: reason, serial: issued)
            held.append(Held(ticket: ticket, stop: stop))
            return ticket
        }
    }

    /// Undo one `begin`. A ticket already ended does nothing.
    func end(_ ticket: Ticket) {
        lock.withLock {
            guard let index = held.firstIndex(where: { $0.ticket == ticket }) else { return }
            held.remove(at: index)
            control.enableAutomaticTermination(ticket.reason)
            control.enableSuddenTermination()
        }
    }

    /// What is running: the reason of each `begin` not yet ended, oldest
    /// first.
    var reasons: [String] {
        lock.withLock { held.map(\.reason) }
    }

    /// The work `ticket` holds for is now doing `reason` (nil: what it
    /// began as again), for the quit question to say: a Mac build waiting
    /// for PlayCover's install (`waitingForPlayCover`). A ticket already
    /// ended does nothing.
    func relabel(_ ticket: Ticket, as reason: String?) {
        lock.withLock {
            guard let index = held.firstIndex(where: { $0.ticket == ticket }) else { return }
            held[index].relabeled = reason
        }
    }

    /// Something is running.
    var isHolding: Bool {
        lock.withLock { !held.isEmpty }
    }

    /// The work `ticket` holds for is finishing a step its stop waits for,
    /// so it can't be cut off halfway: an install `graft --update` had begun
    /// when it was told to stop (`update-stopping`), until that app's
    /// `update-app-end`. `note` is what Graft says while Quit Anyway waits
    /// for it ("Finishing the install on iPad Air, then quitting…"), and nil
    /// once the step is done. Quit Anyway waits for such a step up to
    /// `QuitGuard.finishGrace`, past its usual `stopGrace`. A ticket already
    /// ended does nothing.
    func finishing(_ ticket: Ticket, note: String?) {
        lock.withLock {
            guard let index = held.firstIndex(where: { $0.ticket == ticket }) else { return }
            held[index].finishing = note
        }
    }

    /// What held work is finishing (`finishing`), in its own words, oldest
    /// first.
    var finishingNotes: [String] {
        lock.withLock { held.compactMap(\.finishing) }
    }

    /// The reasons of what runs with no stop (`begin`'s nil `stop`), oldest
    /// first: work Quit Anyway can only wait for.
    var reasonsWithoutStop: [String] {
        lock.withLock { held.filter { $0.stop == nil }.map(\.reason) }
    }

    /// Something with no stop is running.
    var holdsWithoutStop: Bool {
        lock.withLock { held.contains { $0.stop == nil } }
    }

    /// Ask every piece of work that holds to stop, through the `stop` it gave
    /// `begin`. The stops run outside the lock: one may end its own hold
    /// before it returns.
    @MainActor
    func stopAll() {
        let stops = lock.withLock { held.compactMap(\.stop) }
        for stop in stops { stop() }
    }

    /// `work`, held for `reason` from start to end, in a task of its own, so
    /// that canceling that task is its `stop`.
    ///
    /// For work whose only Stop is its task being canceled (a re-install, a
    /// decrypt dump): the caller's own task can't be canceled from here.
    /// Canceling the caller's task cancels this one too, as it did before.
    @MainActor
    func holding<T: Sendable>(
        _ reason: String, _ work: @escaping @MainActor () async throws -> T
    ) async throws -> T {
        let task = Task { @MainActor in try await work() }
        let ticket = begin(reason) { task.cancel() }
        defer { end(ticket) }
        return try await withTaskCancellationHandler {
            try await task.value
        } onCancel: {
            task.cancel()
        }
    }
}

/// What Graft asks when it is told to quit while it works, in plain words.
struct QuitQuestion: Equatable {
    static let keepWorking = "Keep Working"
    static let quitAnyway = "Quit Anyway"

    /// For when no reason has words of its own.
    static let working = "working"

    static let stops = "Quit Anyway stops what Graft is doing and leaves it unfinished."
    static let keeps = "Keep Working cancels the quit, and any logout or restart, so it can finish. "
        + "If it finishes while this message is open, Graft quits then."
    static let detail = stops + " " + keeps

    /// Said when an update is among the work (review part 2, COPY): its Stop
    /// lets an install it has begun finish, and Quit Anyway waits for that
    /// (`QuitGuard.finishGrace`).
    static let updateFinishes = "An install the update has begun finishes first, for up to a minute."

    /// For work with no words of its own that can't be stopped.
    static let unnamedWork = "what it's doing"

    /// The detail when some of the work has no Stop (`waiting`, in
    /// `TerminationHold.activities`' words): Quit Anyway can't stop it, so
    /// Graft quits once it's done (`QuitGuard`). `others`: something else
    /// runs too, which Quit Anyway does stop.
    static func waitingDetail(_ waiting: String, others: Bool) -> String {
        "Graft can't stop \(waiting) partway. With \(quitAnyway), Graft "
            + (others ? "stops everything else, leaving it unfinished, and " : "")
            + "will quit when \(others ? waiting : "it") finishes. "
            + "\(keepWorking) cancels the quit, and any logout or restart."
    }

    let title: String
    let detail: String

    /// The question for `reasons` (`TerminationHold.reasons`): each in
    /// `TerminationHold.activities`' words, once, in order. `withoutStop` is
    /// the reasons among them with no stop (`reasonsWithoutStop`): with any,
    /// the detail says Graft waits for them.
    init(reasons: [String], withoutStop: [String] = []) {
        title = "Quit while Graft is \(Self.words(reasons) ?? Self.working)?"
        // An update Quit Anyway stops still finishes an install it has begun.
        let update = reasons.contains(TerminationHold.updatingApps) ? " " + Self.updateFinishes : ""
        guard !withoutStop.isEmpty else {
            detail = Self.stops + update + " " + Self.keeps
            return
        }
        // `withoutStop` is some of `reasons`: any more, and something runs
        // that Quit Anyway does stop.
        detail = Self.waitingDetail(
            Self.words(withoutStop) ?? Self.unnamedWork, others: reasons.count > withoutStop.count
        ) + update
    }

    /// `reasons` in `TerminationHold.activities`' words, each once, in
    /// order; nil when none has words.
    private static func words(_ reasons: [String]) -> String? {
        var seen = Set<String>()
        let doing = reasons.compactMap { TerminationHold.activities[$0] }.filter { seen.insert($0).inserted }
        return doing.isEmpty ? nil : list(doing)
    }

    /// "a", "a and b", "a, b, and c".
    static func list(_ items: [String]) -> String {
        guard let last = items.last else { return "" }
        switch items.count {
        case 1: return last
        case 2: return "\(items[0]) and \(last)"
        default: return items.dropLast().joined(separator: ", ") + ", and " + last
        }
    }
}

/// The button pressed.
enum QuitAnswer: Equatable {
    case keepWorking
    case quitAnyway
}

/// Something to do on the way out, once a quit is going ahead with nothing
/// held: an update that installs when Graft quits (`UpdateStore`).
@MainActor
protocol QuitHandover: AnyObject {
    /// The quit is going ahead and no work is held. Return false to quit
    /// now. Return true when this started something that ends with `done`,
    /// called later and never from inside this call: `done(true)` quits,
    /// `done(false)` cancels the quit.
    func handOver(_ done: @escaping @MainActor (Bool) -> Void) -> Bool
    /// The quit was canceled before anything was handed over (Keep Working).
    func quitCanceled()
}

/// Answers macOS's quit, whether ⌘Q's or the one a logout, restart or
/// shutdown sends (`TerminationHold`, "A quit that is asked for").
///
/// `applicationShouldTerminate` returns `shouldTerminate()`. Nothing held:
/// `.terminateNow`. Otherwise `.terminateLater`, with the question on screen,
/// and one `reply` later:
/// - Keep Working: false, so macOS cancels the quit, and a logout or restart
///   with it.
/// - Quit Anyway: `TerminationHold.stopAll()` first, then true once the hold
///   has cleared, or after `stopGrace` if it hasn't. Work with no stop is
///   waited for to its end, past `stopGrace`: the question said so. Work
///   finishing a step its stop waits for (`TerminationHold.finishing`) is
///   waited for up to `finishGrace`, with a notice on screen (`notice`)
///   saying what is finishing; its `stopGrace` runs on once the step is
///   done.
/// - The work ends by itself while the question is up: the question comes
///   down and the reply is true at once. The quit was asked for, and nothing
///   is left that asking was about.
///
/// **On the way out** (`handover`). A quit that goes ahead with nothing
/// held, at once or after the question, is first offered to the handover:
/// an update ready to install starts its helper there, and the reply waits
/// for that (`UpdateStore.handOver`). Until it replies, a second quit is
/// refused like one during the question, so it can't skip the install. A
/// quit that Quit Anyway forces while work still holds hands nothing over:
/// nothing is installed while work runs.
///
/// The question, the reply and the clock are all handed in, so a test drives
/// every path without an alert or a real quit.
@MainActor
final class QuitGuard {
    /// Puts `question` on screen and calls `answer` with the button pressed,
    /// later, never from inside this call. Returns the way to take it down
    /// unanswered: the presenter takes it down, then calls `then`, and never
    /// calls `answer`.
    typealias Present = @MainActor (
        _ question: QuitQuestion, _ answer: @escaping @MainActor (QuitAnswer) -> Void
    ) -> TakeDown
    typealias TakeDown = @MainActor (_ then: @escaping @MainActor () -> Void) -> Void
    /// Runs `block` once, `delay` seconds from now.
    typealias Schedule = @MainActor (_ delay: TimeInterval, _ block: @escaping @MainActor () -> Void) -> Void
    /// Shows `note` on screen while Quit Anyway waits for a step to finish
    /// (`TerminationHold.finishing`), in place of what it showed; nil takes it
    /// down. Called only when what it shows changes.
    typealias Notice = @MainActor (_ note: String?) -> Void

    /// How long Quit Anyway waits for the stopped work to end: past
    /// `cliStopGrace`, when the runner SIGKILLs a stopped CLI still running
    /// and its children, plus time for that to land and for the hold to
    /// clear. GCD may run the SIGKILL up to a tenth of its delay late
    /// (`asyncAfter`'s leeway), a second here, and the runner then waits up
    /// to half a second a pipe for its output. A stopped CLI usually ends in
    /// 2 to 3 seconds, and Graft quits then; only one that ignores its
    /// SIGTERM costs the whole wait.
    ///
    /// Not a shorter wait with a reaper left behind to SIGKILL the CLI after
    /// Graft has quit. Such a reaper has only pid numbers: once those
    /// processes exit, launchd reaps them and the numbers go to whatever
    /// starts next, so its SIGKILL could land on another program. The
    /// runner's own SIGKILL walks the tree from a `Process` Foundation hasn't
    /// reaped yet (`ProcessHandle.stop`), which can't, and Graft staying
    /// until it has fired is all it needs.
    static let stopGrace: TimeInterval = cliStopGrace + 3
    /// How long Quit Anyway waits for work finishing a step its stop waits
    /// for (`TerminationHold.finishing`): an install `graft --update` had
    /// begun, which it finishes so the phone isn't left with half of one
    /// (spec 4.5). The CLI holds off the runner's SIGKILL meanwhile
    /// (`RunStopper.holdKill`), so this is the bound: past it, Graft quits,
    /// as it does when stopped work outlasts `stopGrace`.
    static let finishGrace: TimeInterval = 60
    /// How often the hold is looked at, while the question is up and while
    /// Quit Anyway waits.
    static let checkEvery: TimeInterval = 0.1

    private let hold: TerminationHold
    private let present: Present
    private let reply: @MainActor (Bool) -> Void
    private let schedule: Schedule
    private let notice: Notice
    /// What `notice` shows now.
    private var noticeShown: String?

    /// The question on screen, until it is answered or taken down.
    private final class Asking {
        var takeDown: TakeDown?
    }
    private var asking: Asking?
    /// From `.terminateLater` until its `reply`.
    private var pending = false

    /// What happens on the way out, when anything does. Set once the window
    /// is up (`GraftApp`); weak, since the store is the app's.
    weak var handover: QuitHandover?

    init(
        hold: TerminationHold, present: @escaping Present,
        reply: @escaping @MainActor (Bool) -> Void, schedule: @escaping Schedule,
        notice: @escaping Notice
    ) {
        self.hold = hold
        self.present = present
        self.reply = reply
        self.schedule = schedule
        self.notice = notice
    }

    func shouldTerminate() -> NSApplication.TerminateReply {
        // One question at a time: a second quit while one waits is refused,
        // and the first goes on waiting for its answer.
        guard !pending else { return .terminateCancel }
        let reasons = hold.reasons
        guard !reasons.isEmpty else {
            // Pending before the offer, so its reply always finds it set.
            pending = true
            guard handOver() else {
                pending = false
                return .terminateNow
            }
            return .terminateLater
        }
        pending = true
        let asked = Asking()
        asking = asked
        let question = QuitQuestion(reasons: reasons, withoutStop: hold.reasonsWithoutStop)
        asked.takeDown = present(question) { [weak self, weak asked] answer in
            guard let self, let asked, self.asking === asked else { return }
            self.asking = nil
            switch answer {
            case .keepWorking:
                self.handover?.quitCanceled()
                self.finish(quit: false)
            case .quitAnyway: self.quitAnyway()
            }
        }
        watch(asked)
        return .terminateLater
    }

    /// While `asked` is up: once the hold has cleared, take it down and quit.
    private func watch(_ asked: Asking) {
        schedule(Self.checkEvery) { [weak self, weak asked] in
            guard let self, let asked, self.asking === asked else { return }
            guard !self.hold.isHolding else { return self.watch(asked) }
            self.asking = nil
            guard let takeDown = asked.takeDown else { return self.leave() }
            takeDown { [weak self] in self?.leave() }
        }
    }

    /// Stop everything that holds, then quit once the hold has cleared, or
    /// once `stopGrace` has passed and nothing with no stop is left, or, while
    /// work is finishing a step, once `finishGrace` has.
    private func quitAnyway() {
        hold.stopAll()
        wait(checksLeft: Self.checks(Self.stopGrace), finishLeft: Self.checks(Self.finishGrace))
    }

    private static func checks(_ seconds: TimeInterval) -> Int {
        Int((seconds / checkEvery).rounded(.up))
    }

    /// `checksLeft` counts down `stopGrace`, `finishLeft` `finishGrace`, both
    /// from Quit Anyway.
    private func wait(checksLeft: Int, finishLeft: Int) {
        // A step the work is finishing (an install the update had begun) is
        // waited for up to `finishGrace`, and said on screen. `stopGrace`
        // stands still meanwhile: once the step is done, the stopped work
        // still has it to unwind and end.
        let finishing = hold.finishingNotes.first
        let inTime = finishing == nil ? checksLeft > 0 : finishLeft > 0
        // Work with no stop wasn't asked to stop, and the question said
        // Graft quits when it finishes: no deadline for it. Quitting under it
        // would leave its CLI running until its next write found no reader.
        guard hold.isHolding, inTime || hold.holdsWithoutStop else { return leave() }
        show(finishing)
        schedule(Self.checkEvery) { [weak self] in
            self?.wait(checksLeft: finishing == nil ? checksLeft - 1 : checksLeft, finishLeft: finishLeft - 1)
        }
    }

    /// `notice`, when what it says changes.
    private func show(_ note: String?) {
        guard note != noticeShown else { return }
        noticeShown = note
        notice(note)
    }

    /// The quit goes ahead: through the handover, if it takes it, else now.
    private func leave() {
        show(nil)
        guard handOver() else { return finish(quit: true) }
    }

    /// Offer the way out to the handover, with nothing held. True when it
    /// took it, and will finish the quit itself.
    private func handOver() -> Bool {
        guard !hold.isHolding, let handover else { return false }
        return handover.handOver { [weak self] quit in self?.finish(quit: quit) }
    }

    private func finish(quit: Bool) {
        pending = false
        reply(quit)
    }
}

/// Lets a quit reach `applicationShouldTerminate`, and so `QuitGuard`, while
/// a sheet is open (2.3.1 spec §2).
///
/// **Why it didn't.** AppKit's `terminate:` won't quit while any window is
/// document-modal (a sheet on another window) whose
/// `preventsApplicationTerminationWhenModal` is true, the default: it returns
/// without asking the app's delegate, and without a sound. SwiftUI leaves that
/// default on a `.sheet`'s window and a `.confirmationDialog`'s; NSAlert sets
/// it false on its own panel, so an `.alert` never had this. So with any sheet
/// open (an update's progress, Edit…, a confirmation), ⌘Q, Graft › Quit Graft,
/// a logout's or restart's quit, and Remove Graft's Data's own Quit Graft all
/// stopped inside `terminate:`, and QuitGuard never ran. Not a modal session
/// (`NSApp.modalWindow` is nil while a sheet is up), and not the menu: its
/// Quit sends `terminate:` to NSApp, enabled, as with no sheet. Measured on
/// 2026-11-26 in a standalone SwiftUI app with `terminate:` logged: with a
/// sheet up it ran and asked nothing; with the sheet's window set not to
/// prevent it, the delegate was asked, from ⌘Q's `terminate:` and the Quit
/// item alike, for AppKit's own sheets as for SwiftUI's. A confirmation over
/// a sheet is a sheet on the sheet, and both have to be let through.
///
/// **The fix.** As each sheet begins, every window is set not to prevent a
/// quit: the sheet's, on whatever window it goes over, and the rest, on
/// which the flag does nothing unless they are modal (NSAlert sets it false
/// on its own panel already). A quit with a sheet open then goes to
/// `applicationShouldTerminate` as one with none does: QuitGuard's question
/// while work runs, Quit Anyway's wait for an install an update has begun
/// included, and otherwise Graft quits, the sheet and any unsaved edits in
/// it going with the window, as closing it would. `GraftAppDelegate` starts
/// it once, for the app's life.
@MainActor
enum SheetsAllowQuit {
    /// None of `windows` stops a quit: each one, not only those that are
    /// sheets by now, so a sheet AppKit hasn't attached yet is let through
    /// too.
    static func allow(_ windows: [NSWindow]) {
        for window in windows where window.preventsApplicationTerminationWhenModal {
            window.preventsApplicationTerminationWhenModal = false
        }
    }

    /// From now on, every sheet in this app: the windows there are now, and
    /// all of them again as each sheet begins
    /// (`NSWindow.willBeginSheetNotification`, which `center` posts for the
    /// window it goes over; `sheetBegins`). Keep what this returns for as
    /// long as it should last.
    static func observe(_ center: NotificationCenter) -> NSObjectProtocol {
        allow(NSApp.windows)
        return center.addObserver(forName: NSWindow.willBeginSheetNotification, object: nil, queue: nil) { _ in
            MainActor.assumeIsolated {
                sheetBegins(windows: { NSApp.windows }, later: { block in
                    RunLoop.main.perform(inModes: [.common]) { MainActor.assumeIsolated(block) }
                })
            }
        }
    }

    /// A sheet is beginning: let every one of `windows` through now, and
    /// once more `later`, on the next run-loop turn. The sheet's window has
    /// its `sheetParent` when AppKit says it begins, on the macOS this was
    /// measured on, though the parent's `attachedSheet` is still nil. The
    /// app runs on macOS 13 too, which wasn't measured: an older AppKit may
    /// attach the sheet only after saying so, even after the next turn. So
    /// neither look asks which window is a sheet: the sheet's is let through
    /// as long as it's among `windows`, attached or not, and the second
    /// look is for one that isn't among them yet.
    static func sheetBegins(
        windows: @escaping @MainActor () -> [NSWindow],
        later: (@escaping @MainActor () -> Void) -> Void
    ) {
        allow(windows())
        later { allow(windows()) }
    }
}
