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
/// clear, and quits.
///
/// What it still doesn't stop: a Force Quit, a kill or a crash, which no app
/// can answer. Work that hasn't ended within the grace is left to finish its
/// cleanup without Graft. The `--init` setup has no Stop, so Quit Anyway only
/// waits for it.
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

    let title: String
    let detail: String

    /// The question for `reasons` (`TerminationHold.reasons`): each in
    /// `TerminationHold.activities`' words, once, in order.
    init(reasons: [String]) {
        var seen = Set<String>()
        let doing = reasons.compactMap { TerminationHold.activities[$0] }.filter { seen.insert($0).inserted }
        title = "Quit while Graft is \(doing.isEmpty ? Self.working : Self.list(doing))?"
        detail = Self.detail
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

/// Answers macOS's quit, whether ⌘Q's or the one a logout, restart or
/// shutdown sends (`TerminationHold`, "A quit that is asked for").
///
/// `applicationShouldTerminate` returns `shouldTerminate()`. Nothing held:
/// `.terminateNow`. Otherwise `.terminateLater`, with the question on screen,
/// and one `reply` later:
/// - Keep Working: false, so macOS cancels the quit, and a logout or restart
///   with it.
/// - Quit Anyway: `TerminationHold.stopAll()` first, then true once the hold
///   has cleared, or after `stopGrace` if it hasn't.
/// - The work ends by itself while the question is up: the question comes
///   down and the reply is true at once. The quit was asked for, and nothing
///   is left that asking was about.
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

    /// How long Quit Anyway waits for the stopped work to end. A stopped
    /// build's cleanup took 2 to 3 seconds for a real app (`cliStopGrace`).
    static let stopGrace: TimeInterval = 5
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
        guard !reasons.isEmpty else { return .terminateNow }
        pending = true
        let asked = Asking()
        asking = asked
        asked.takeDown = present(QuitQuestion(reasons: reasons)) { [weak self, weak asked] answer in
            guard let self, let asked, self.asking === asked else { return }
            self.asking = nil
            switch answer {
            case .keepWorking: self.finish(quit: false)
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
            guard let takeDown = asked.takeDown else { return self.finish(quit: true) }
            takeDown { [weak self] in self?.finish(quit: true) }
        }
    }

    /// Stop everything that holds, then quit once the hold has cleared, or
    /// once `stopGrace` has passed.
    private func quitAnyway() {
        hold.stopAll()
        wait(checksLeft: Int((Self.stopGrace / Self.checkEvery).rounded(.up)))
    }

    private func wait(checksLeft: Int) {
        guard hold.isHolding, checksLeft > 0 else { return finish(quit: true) }
        schedule(Self.checkEvery) { [weak self] in self?.wait(checksLeft: checksLeft - 1) }
    }

    private func finish(quit: Bool) {
        pending = false
        reply(quit)
    }
}
