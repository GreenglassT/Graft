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
/// What it still doesn't stop: a Force Quit, a kill or a crash, which no app
/// can answer.
final class TerminationHold: @unchecked Sendable {
    /// The app's one hold, on the real `ProcessInfo`. Reaches nothing past
    /// this process, so stores default to it; a test passes its own.
    static let shared = TerminationHold(control: ProcessInfo.processInfo)

    static let building = "Graft is building and installing an app"
    static let gettingFromAppStore = "Graft is getting an app from the App Store"
    static let staging = "Graft is staging an IPA"
    static let claiming = "Graft is getting a free app for this Apple ID"
    static let settingUp = "Graft is setting up the signing account"
    static let enrolling = "Graft is setting up App Store decryption"
    static let liveContainer = "Graft is changing LiveContainer on a device"
    static let reinstalling = "Graft is re-installing a build on a device"
    static let decrypting = "Graft is decrypting an app from a device"
    static let removingData = "Graft is removing its data"

    /// Each reason in the words the quit question uses: "Quit while Graft
    /// is …?" (`QuitQuestion`).
    static let activities: [String: String] = [
        building: "building and installing an app",
        gettingFromAppStore: "getting an app from the App Store",
        staging: "staging an IPA for a build",
        claiming: "getting a free app for your Apple ID",
        settingUp: "setting up your signing account",
        enrolling: "setting up App Store decryption",
        liveContainer: "changing LiveContainer on a device",
        reinstalling: "re-installing a build on a device",
        decrypting: "decrypting an app from a device",
        removingData: "removing its data",
    ]

    /// One `begin`, which `end` takes back.
    struct Ticket: Hashable, Sendable {
        let reason: String
        fileprivate let serial: Int
    }

    private struct Held {
        let ticket: Ticket
        let stop: (@MainActor () -> Void)?
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
        lock.withLock { held.map(\.ticket.reason) }
    }

    /// Something is running.
    var isHolding: Bool {
        lock.withLock { !held.isEmpty }
    }

    /// The reasons of what runs with no stop (`begin`'s nil `stop`), oldest
    /// first: work Quit Anyway can only wait for.
    var reasonsWithoutStop: [String] {
        lock.withLock { held.filter { $0.stop == nil }.map(\.ticket.reason) }
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

    static let detail = "Quit Anyway stops what Graft is doing and leaves it unfinished. "
        + "Keep Working cancels the quit, and any logout or restart, so it can finish. "
        + "If it finishes while this message is open, Graft quits then."

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
        guard !withoutStop.isEmpty else {
            detail = Self.detail
            return
        }
        // `withoutStop` is some of `reasons`: any more, and something runs
        // that Quit Anyway does stop.
        detail = Self.waitingDetail(
            Self.words(withoutStop) ?? Self.unnamedWork, others: reasons.count > withoutStop.count
        )
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
///   waited for to its end, past `stopGrace`: the question said so.
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
    /// How often the hold is looked at, while the question is up and while
    /// Quit Anyway waits.
    static let checkEvery: TimeInterval = 0.1

    private let hold: TerminationHold
    private let present: Present
    private let reply: @MainActor (Bool) -> Void
    private let schedule: Schedule

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
        reply: @escaping @MainActor (Bool) -> Void, schedule: @escaping Schedule
    ) {
        self.hold = hold
        self.present = present
        self.reply = reply
        self.schedule = schedule
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
    /// once `stopGrace` has passed and nothing with no stop is left.
    private func quitAnyway() {
        hold.stopAll()
        wait(checksLeft: Int((Self.stopGrace / Self.checkEvery).rounded(.up)))
    }

    private func wait(checksLeft: Int) {
        // Work with no stop wasn't asked to stop, and the question said
        // Graft quits when it finishes: no deadline for it. Quitting under it
        // would leave its CLI running until its next write found no reader.
        guard hold.isHolding, checksLeft > 0 || hold.holdsWithoutStop else { return leave() }
        schedule(Self.checkEvery) { [weak self] in self?.wait(checksLeft: checksLeft - 1) }
    }

    /// The quit goes ahead: through the handover, if it takes it, else now.
    private func leave() {
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
