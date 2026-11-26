import AppKit
import XCTest
@testable import Graft

/// Whole-diff review, app-3: macOS mustn't end Graft (sudden or automatic
/// termination) while a build or a re-install, an App Store get, drop or Get
/// (free), an `--init` setup, an enrollment, a LiveContainer install or a
/// decrypt dump runs (the re-install and the dump since the try14 delta
/// audit, D14-APP-2); and each hold is let go on every path, or the app
/// could never be terminated that way again. Every store gets a
/// `FakeControl` in place of `ProcessInfo`, and a stand-in CLI that waits for
/// the test's go-ahead, so the hold can be seen while the work runs.
///
/// And a quit while one runs is answered, not obeyed at once (the try14
/// delta audit, F4): `QuitGuard` is driven with a recording presenter, reply
/// and clock, never an alert or a real quit; and each store's hold names the
/// Stop that Quit Anyway goes through (`hold.stopAll()`, the "quit" endings).
@MainActor
final class TerminationHoldTests: XCTestCase {
    /// What `ProcessInfo` would have been asked, by reason.
    final class FakeControl: TerminationControl, @unchecked Sendable {
        private let lock = NSLock()
        private var sudden = 0
        private var automatic: [String: Int] = [:]
        private var taken: [String: Int] = [:]
        private var wentNegative = false

        func disableSuddenTermination() { lock.withLock { sudden += 1 } }
        func enableSuddenTermination() {
            lock.withLock {
                sudden -= 1
                if sudden < 0 { wentNegative = true }
            }
        }
        func disableAutomaticTermination(_ reason: String) {
            lock.withLock {
                automatic[reason, default: 0] += 1
                taken[reason, default: 0] += 1
            }
        }
        func enableAutomaticTermination(_ reason: String) {
            lock.withLock {
                automatic[reason, default: 0] -= 1
                if automatic[reason, default: 0] < 0 { wentNegative = true }
            }
        }

        /// Both are off, `reason` among the automatic ones.
        func isHeld(_ reason: String) -> Bool {
            lock.withLock { sudden > 0 && automatic[reason, default: 0] > 0 }
        }

        /// `reason` was held, and has been let go as often as it was taken.
        func isLetGo(_ reason: String) -> Bool {
            lock.withLock { taken[reason, default: 0] > 0 && automatic[reason, default: 0] == 0 }
        }

        /// Nothing is held, and nothing was let go that wasn't taken.
        var isBalanced: Bool {
            lock.withLock { sudden == 0 && automatic.values.allSatisfy { $0 == 0 } && !wentNegative }
        }
    }

    private var dir: URL!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appending(path: "hold_\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    /// The test's go-ahead to the stand-in: until it exists, `wait` loops,
    /// for 30 s at most, so a stand-in a failing test never stops doesn't
    /// outlive the run by long.
    private func go() throws { try Data().write(to: dir.appending(path: "go")) }
    private var wait: String {
        "n=0; while [ ! -e '\(dir.path)/go' ] && [ $n -lt 600 ]; do /bin/sleep 0.05; n=$((n+1)); done"
    }

    private func cli(_ body: String) throws -> CLIRunner {
        let file = dir.appending(path: "graft.sh")
        try "#!/bin/sh\n\(body)\n".write(to: file, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: file.path)
        return CLIRunner(executable: file)
    }

    // MARK: The hold itself

    func test_begin_turns_both_off_and_end_turns_them_back_on() {
        let control = FakeControl()
        let hold = TerminationHold(control: control)
        let one = hold.begin("one", stop: nil)
        let two = hold.begin("two", stop: nil)
        XCTAssertTrue(control.isHeld("one"))
        hold.end(one)
        XCTAssertTrue(control.isHeld("two"), "the other is still held")
        hold.end(two)
        XCTAssertTrue(control.isBalanced)
        XCTAssertTrue(control.isLetGo("one") && control.isLetGo("two"))
    }

    /// The hold knows what runs, one reason per `begin` not yet ended, and
    /// stops each through its own stop: two holds with one reason are told
    /// apart by their tickets, and an ended one is neither named nor stopped.
    func test_the_hold_names_what_runs_and_stops_each_through_its_own_stop() {
        let control = FakeControl()
        let hold = TerminationHold(control: control)
        var stopped: [String] = []
        XCTAssertEqual(hold.reasons, [])
        XCTAssertFalse(hold.isHolding)
        let first = hold.begin("one") { stopped.append("first") }
        let other = hold.begin("two", stop: nil)
        let second = hold.begin("one") { stopped.append("second") }
        XCTAssertEqual(hold.reasons, ["one", "two", "one"])
        XCTAssertTrue(hold.isHolding)
        hold.end(first)
        XCTAssertEqual(hold.reasons, ["two", "one"])
        hold.stopAll()
        XCTAssertEqual(stopped, ["second"], "only what still runs, and only what has a stop")
        hold.end(first)
        XCTAssertTrue(control.isHeld("one"), "a ticket ended twice is undone once")
        hold.end(second)
        hold.end(other)
        XCTAssertEqual(hold.reasons, [])
        XCTAssertFalse(hold.isHolding)
        XCTAssertTrue(control.isBalanced)
    }

    /// A stop that ends its own hold before it returns, as a stop of work
    /// already ending can: `stopAll` doesn't hold the lock while it runs one.
    func test_a_stop_may_end_its_own_hold() {
        let control = FakeControl()
        let hold = TerminationHold(control: control)
        var ticket: TerminationHold.Ticket?
        ticket = hold.begin("one") { if let ticket { hold.end(ticket) } }
        hold.stopAll()
        XCTAssertFalse(hold.isHolding)
        XCTAssertTrue(control.isBalanced)
    }

    // MARK: Quitting while it works (the try14 delta audit, F4)

    /// `QuitGuard` with everything past it recorded: the questions shown, the
    /// presenter's answer callback, its take-downs, the replies, and a clock
    /// that moves only when the test steps it.
    final class QuitHarness {
        let control = FakeControl()
        let hold: TerminationHold
        private(set) var now: TimeInterval = 0
        private var due: [(at: TimeInterval, order: Int, block: @MainActor () -> Void)] = []
        private var scheduled = 0
        private(set) var questions: [QuitQuestion] = []
        private(set) var answer: (@MainActor (QuitAnswer) -> Void)?
        private(set) var takeDowns = 0
        private(set) var replies: [Bool] = []
        private(set) var repliedAt: TimeInterval?
        /// What the wait after Quit Anyway said, in order; nil when it came down.
        private(set) var notices: [String?] = []
        private(set) var quitGuard: QuitGuard!

        @MainActor
        init() {
            hold = TerminationHold(control: control)
            quitGuard = QuitGuard(
                hold: hold,
                present: { [unowned self] question, answer in
                    self.questions.append(question)
                    self.answer = answer
                    return { [unowned self] then in
                        self.takeDowns += 1
                        then()
                    }
                },
                reply: { [unowned self] quit in
                    self.replies.append(quit)
                    self.repliedAt = self.now
                },
                schedule: { [unowned self] delay, block in self.after(delay, block) },
                notice: { [unowned self] note in self.notices.append(note) }
            )
        }

        func after(_ delay: TimeInterval, _ block: @escaping @MainActor () -> Void) {
            scheduled += 1
            due.append((now + delay, scheduled, block))
        }

        /// Run the next block due, moving the clock to its time. False when
        /// nothing is due.
        @MainActor
        @discardableResult
        func step() -> Bool {
            guard let next = due.indices.min(by: { (due[$0].at, due[$0].order) < (due[$1].at, due[$1].order) })
            else { return false }
            let item = due.remove(at: next)
            now = item.at
            item.block()
            return true
        }

        /// Step until nothing is due, or `limit` steps: a guard that never
        /// replies mustn't hang the test.
        @MainActor
        func runDown(limit: Int = 10_000) {
            var steps = 0
            while steps < limit, step() { steps += 1 }
        }
    }

    func test_with_nothing_running_graft_quits_at_once() {
        let h = QuitHarness()
        XCTAssertEqual(h.quitGuard.shouldTerminate(), .terminateNow)
        h.hold.end(h.hold.begin(TerminationHold.building, stop: nil))
        XCTAssertEqual(h.quitGuard.shouldTerminate(), .terminateNow, "ended work is not running")
        XCTAssertEqual(h.questions, [], "nothing asked")
        XCTAssertFalse(h.step(), "nothing waited for")
        XCTAssertEqual(h.replies, [], "terminateNow needs no reply")
    }

    func test_with_work_running_graft_asks_what_to_do_and_waits_for_the_answer() {
        let h = QuitHarness()
        _ = h.hold.begin(TerminationHold.building, stop: nil)
        XCTAssertEqual(h.quitGuard.shouldTerminate(), .terminateLater)
        XCTAssertEqual(h.questions.map(\.title), ["Quit while Graft is building and installing an app?"])
        XCTAssertEqual(h.replies, [], "no reply until it's answered")
        XCTAssertEqual(h.quitGuard.shouldTerminate(), .terminateCancel, "a second quit while one waits")
        XCTAssertEqual(h.questions.count, 1, "asked once")
    }

    func test_keep_working_replies_no_and_stops_nothing() {
        let h = QuitHarness()
        var stops = 0
        let ticket = h.hold.begin(TerminationHold.building) { stops += 1 }
        XCTAssertEqual(h.quitGuard.shouldTerminate(), .terminateLater)
        h.answer?(.keepWorking)
        XCTAssertEqual(h.replies, [false], "macOS cancels the quit, and a logout or restart with it")
        h.runDown()
        XCTAssertEqual(h.replies, [false], "one reply")
        XCTAssertEqual(stops, 0)
        XCTAssertTrue(h.control.isHeld(TerminationHold.building))
        XCTAssertEqual(h.quitGuard.shouldTerminate(), .terminateLater, "the next quit asks again")
        XCTAssertEqual(h.questions.count, 2)
        h.hold.end(ticket)
    }

    /// Quit Anyway asks the work to stop, then waits for its hold to clear,
    /// but no longer than `stopGrace`: work that doesn't end is quit from.
    /// Not before the runner's SIGKILL for a stopped CLI (`cliStopGrace`),
    /// though: only a Graft still running can send it.
    func test_quit_anyway_stops_the_work_then_waits_a_bounded_time_and_quits() throws {
        let h = QuitHarness()
        var stops = 0
        _ = h.hold.begin(TerminationHold.building) { stops += 1 }
        XCTAssertEqual(h.quitGuard.shouldTerminate(), .terminateLater)
        h.answer?(.quitAnyway)
        XCTAssertEqual(stops, 1, "stopped through its own stop, first")
        XCTAssertEqual(h.replies, [], "not before it has had time to stop")
        h.runDown()
        XCTAssertEqual(h.replies, [true])
        XCTAssertEqual(try XCTUnwrap(h.repliedAt), QuitGuard.stopGrace, accuracy: QuitGuard.checkEvery)
        XCTAssertGreaterThanOrEqual(try XCTUnwrap(h.repliedAt), cliStopGrace + 1,
                                    "past the SIGKILL, with time for it to land and the hold to clear")
        XCTAssertEqual(stops, 1, "asked once")
        XCTAssertEqual(h.takeDowns, 0, "answered, so nothing to take down")
    }

    /// The same against a real CLI that ignores its SIGTERM, as a wedged one
    /// does, and whose child does too: Graft doesn't quit until the runner's
    /// SIGKILL (`cliStopGrace` after the stop) has ended both, so neither is
    /// left running without it. With the 5-second wait this had, Graft quit
    /// with both still running and nothing left to kill them.
    func test_quit_anyway_stays_until_the_runner_has_killed_a_cli_that_ignores_its_stop() async throws {
        let hold = TerminationHold(control: FakeControl())
        let mine = dir.path
        addTeardownBlock { Self.reap(matching: mine) }
        // `trap '' TERM` is inherited: the child's shell ignores it too.
        let runner = try cli("""
            trap '' TERM
            /bin/sh -c 'while :; do /bin/sleep 0.1; done' '\(mine)/child' &
            wait
            """)
        let work = Task { @MainActor in
            try? await hold.holding(TerminationHold.building) { _ = try await runner.runOnce(arguments: []) }
        }
        await eventually({ Self.running(matching: mine) >= 2 }, "the CLI and its child run")

        final class Reply { var at: Date? }
        let reply = Reply()
        let quitGuard = QuitGuard(
            hold: hold,
            present: { _, answer in
                Task { @MainActor in answer(.quitAnyway) }
                return { then in then() }
            },
            reply: { _ in reply.at = Date() },
            schedule: { delay, block in
                Task { @MainActor in
                    try? await Task.sleep(for: .seconds(delay))
                    block()
                }
            },
            notice: { _ in }
        )
        let asked = Date()
        XCTAssertEqual(quitGuard.shouldTerminate(), .terminateLater)
        await eventually({ reply.at != nil }, timeout: QuitGuard.stopGrace + 10, "Graft quits")

        let waited = try XCTUnwrap(reply.at).timeIntervalSince(asked)
        XCTAssertGreaterThanOrEqual(waited, cliStopGrace, "not before the SIGKILL")
        XCTAssertLessThan(waited, QuitGuard.stopGrace, "once the hold cleared, not at the end of the wait")
        XCTAssertFalse(hold.isHolding)
        XCTAssertEqual(Self.running(matching: mine), 0, "nothing of it is left running without Graft")
        await work.value
    }

    /// How many processes have `text` in their arguments (`pgrep -f`, which
    /// never counts itself).
    nonisolated private static func running(matching text: String) -> Int {
        let probe = Process()
        probe.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
        probe.arguments = ["-f", text]
        let out = Pipe()
        probe.standardOutput = out
        probe.standardError = Pipe()
        guard (try? probe.run()) != nil else { return 0 }
        probe.waitUntilExit()
        let found = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        return found.split(separator: "\n").count
    }

    /// So a failing run doesn't leave its stand-ins behind.
    nonisolated private static func reap(matching text: String) {
        let killer = Process()
        killer.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        killer.arguments = ["-9", "-f", text]
        killer.standardOutput = Pipe()
        killer.standardError = Pipe()
        guard (try? killer.run()) != nil else { return }
        killer.waitUntilExit()
    }

    /// Work with no stop isn't asked to stop, and Graft doesn't quit from
    /// under it: Quit Anyway waits for it to finish, past the grace, then
    /// quits; through the handover, since nothing is held by then.
    func test_quit_anyway_waits_for_work_with_no_stop_to_finish() throws {
        let h = QuitHarness()
        let handover = FakeHandover()
        h.quitGuard.handover = handover
        let ticket = h.hold.begin(TerminationHold.settingUp, stop: nil)
        XCTAssertEqual(h.quitGuard.shouldTerminate(), .terminateLater)
        h.answer?(.quitAnyway)
        while h.now < QuitGuard.stopGrace * 3 { XCTAssertTrue(h.step(), "still checking") }
        XCTAssertEqual(h.replies, [], "still waiting, well past the grace")
        XCTAssertEqual(handover.offers, 0, "nothing installs while it runs")

        let ended = h.now
        h.hold.end(ticket)
        h.runDown()
        XCTAssertEqual(handover.offers, 1)
        handover.done?(true)
        XCTAssertEqual(h.replies, [true])
        XCTAssertEqual(try XCTUnwrap(h.repliedAt), ended, accuracy: QuitGuard.checkEvery + 0.001)
    }

    /// With work that has no stop beside work that does: the one is stopped
    /// and given the grace, the other waited for. A stopped one still
    /// holding then quits with it, and hands nothing over.
    func test_quit_anyway_stops_what_it_can_and_waits_for_the_rest() throws {
        let h = QuitHarness()
        let handover = FakeHandover()
        h.quitGuard.handover = handover
        var stops = 0
        _ = h.hold.begin(TerminationHold.building) { stops += 1 }
        let setup = h.hold.begin(TerminationHold.settingUp, stop: nil)
        XCTAssertEqual(h.quitGuard.shouldTerminate(), .terminateLater)
        h.answer?(.quitAnyway)
        XCTAssertEqual(stops, 1)
        while h.now < QuitGuard.stopGrace * 2 { XCTAssertTrue(h.step()) }
        XCTAssertEqual(h.replies, [])

        h.hold.end(setup)
        h.runDown()
        XCTAssertEqual(h.replies, [true], "the stopped build outlasted its grace")
        XCTAssertEqual(handover.offers, 0, "nothing installs while work holds")
    }

    func test_quit_anyway_quits_as_soon_as_the_stopped_work_ends() throws {
        let h = QuitHarness()
        var ticket: TerminationHold.Ticket?
        // The CLI unwinds and exits 0.3 s after its SIGTERM.
        ticket = h.hold.begin(TerminationHold.reinstalling) { [unowned h] in
            h.after(0.3) { if let ticket { h.hold.end(ticket) } }
        }
        XCTAssertEqual(h.quitGuard.shouldTerminate(), .terminateLater)
        h.answer?(.quitAnyway)
        h.runDown()
        XCTAssertEqual(h.replies, [true])
        XCTAssertEqual(try XCTUnwrap(h.repliedAt), 0.3, accuracy: QuitGuard.checkEvery + 0.001, "not the whole grace")
        XCTAssertTrue(h.control.isBalanced)
    }

    // MARK: A step the work is finishing (2.3, spec 4.5)

    /// Work says it is finishing a step its stop waits for, and when it's
    /// done; only while it holds.
    func test_work_can_say_it_is_finishing_a_step_until_it_is_done() {
        let control = FakeControl()
        let hold = TerminationHold(control: control)
        let update = hold.begin(TerminationHold.updatingApps) {}
        let build = hold.begin(TerminationHold.building) {}
        XCTAssertEqual(hold.finishingNotes, [])

        hold.finishing(update, note: "Finishing the install, then quitting…")
        XCTAssertEqual(hold.finishingNotes, ["Finishing the install, then quitting…"])
        hold.finishing(update, note: nil)
        XCTAssertEqual(hold.finishingNotes, [], "done")

        hold.finishing(update, note: "again")
        hold.end(update)
        XCTAssertEqual(hold.finishingNotes, [], "an ended ticket finishes nothing")
        hold.finishing(update, note: "late")
        XCTAssertEqual(hold.finishingNotes, [], "nor does a note for a ticket already ended")
        hold.end(build)
        XCTAssertTrue(control.isBalanced)
    }

    /// An update finishes an install it has begun before it stops
    /// (`update-stopping`), which can outlast `stopGrace`. Quit Anyway waits
    /// for it, past the grace, saying so, and quits once the update has
    /// ended, so a quit never leaves a half-done install either.
    func test_quit_anyway_waits_through_an_install_the_update_is_finishing() throws {
        let h = QuitHarness()
        let note = "Finishing the install on Example iPhone, then quitting…"
        var ticket: TerminationHold.Ticket?
        // The CLI defers its Stop for the install, finishes it 30 s later
        // (`update-app-end`), then exits.
        ticket = h.hold.begin(TerminationHold.updatingApps) { [unowned h] in
            h.after(0.2) { if let ticket { h.hold.finishing(ticket, note: note) } }
            h.after(30) {
                guard let ticket else { return }
                h.hold.finishing(ticket, note: nil)
                h.after(0.3) { h.hold.end(ticket) }
            }
        }
        XCTAssertEqual(h.quitGuard.shouldTerminate(), .terminateLater)
        h.answer?(.quitAnyway)

        while h.now < QuitGuard.stopGrace + 1 { XCTAssertTrue(h.step(), "still waiting") }
        XCTAssertEqual(h.replies, [], "past the usual grace: the install is still finishing")
        XCTAssertEqual(h.notices, [note], "the wait says what it waits for")

        h.runDown()
        XCTAssertEqual(h.replies, [true])
        XCTAssertEqual(try XCTUnwrap(h.repliedAt), 30.3, accuracy: QuitGuard.checkEvery + 0.001,
                       "once the update has ended")
        XCTAssertEqual(h.notices, [note, nil], "and the notice comes down")
        XCTAssertTrue(h.control.isBalanced)
    }

    /// An install that never ends: Graft gives up at `finishGrace`, as it
    /// does on stopped work at `stopGrace`.
    func test_quit_anyway_gives_up_on_a_finishing_step_at_the_bound() throws {
        let h = QuitHarness()
        var ticket: TerminationHold.Ticket?
        ticket = h.hold.begin(TerminationHold.updatingApps) { [unowned h] in
            if let ticket { h.hold.finishing(ticket, note: "Finishing the install, then quitting…") }
        }
        XCTAssertEqual(h.quitGuard.shouldTerminate(), .terminateLater)
        h.answer?(.quitAnyway)
        h.runDown()

        XCTAssertEqual(QuitGuard.finishGrace, 60, "spec 4.5")
        XCTAssertEqual(h.replies, [true])
        XCTAssertEqual(try XCTUnwrap(h.repliedAt), QuitGuard.finishGrace, accuracy: QuitGuard.checkEvery + 0.001)
        XCTAssertEqual(h.notices, ["Finishing the install, then quitting…", nil], "taken down on the way out")
    }

    /// Work that ends by itself while the question is up: the question comes
    /// down and Graft quits, as it was asked to; a late answer changes
    /// nothing.
    func test_work_that_ends_while_the_question_is_up_takes_it_down_and_quits() {
        let h = QuitHarness()
        var stops = 0
        let ticket = h.hold.begin(TerminationHold.decrypting) { stops += 1 }
        XCTAssertEqual(h.quitGuard.shouldTerminate(), .terminateLater)
        h.step()
        XCTAssertEqual(h.replies, [], "still running: still asking")
        h.hold.end(ticket)
        h.runDown()
        XCTAssertEqual(h.takeDowns, 1)
        XCTAssertEqual(h.replies, [true])
        h.answer?(.keepWorking)
        XCTAssertEqual(h.replies, [true], "a late answer changes nothing")
        XCTAssertEqual(stops, 0, "nothing left to stop")
    }

    // MARK: On the way out

    /// A `QuitHandover` that records each offer and keeps the `done` it was
    /// given, for the test to call.
    @MainActor
    final class FakeHandover: QuitHandover {
        var takes = true
        private(set) var offers = 0
        private(set) var canceled = 0
        private(set) var done: (@MainActor (Bool) -> Void)?

        func handOver(_ done: @escaping @MainActor (Bool) -> Void) -> Bool {
            offers += 1
            guard takes else { return false }
            self.done = done
            return true
        }

        func quitCanceled() { canceled += 1 }
    }

    func test_with_nothing_running_the_handover_takes_the_quit_and_a_second_quit_waits() {
        let h = QuitHarness()
        let handover = FakeHandover()
        h.quitGuard.handover = handover

        XCTAssertEqual(h.quitGuard.shouldTerminate(), .terminateLater)
        XCTAssertEqual(handover.offers, 1)
        XCTAssertEqual(h.questions, [], "nothing held: nothing asked")
        XCTAssertEqual(h.quitGuard.shouldTerminate(), .terminateCancel,
                       "a quit during the handover can't go past it")
        XCTAssertEqual(handover.offers, 1)
        XCTAssertEqual(h.replies, [])

        handover.done?(true)
        XCTAssertEqual(h.replies, [true])
        XCTAssertEqual(h.quitGuard.shouldTerminate(), .terminateLater, "the next quit is offered again")
        XCTAssertEqual(handover.offers, 2)
    }

    func test_a_handover_with_nothing_to_do_lets_graft_quit_at_once() {
        let h = QuitHarness()
        let handover = FakeHandover()
        handover.takes = false
        h.quitGuard.handover = handover
        XCTAssertEqual(h.quitGuard.shouldTerminate(), .terminateNow)
        XCTAssertEqual(h.quitGuard.shouldTerminate(), .terminateNow, "and isn't left pending")
        XCTAssertEqual(handover.offers, 2)
        XCTAssertEqual(h.replies, [])
    }

    func test_a_handover_can_cancel_the_quit() {
        let h = QuitHarness()
        let handover = FakeHandover()
        h.quitGuard.handover = handover
        XCTAssertEqual(h.quitGuard.shouldTerminate(), .terminateLater)
        handover.done?(false)
        XCTAssertEqual(h.replies, [false])
    }

    func test_keep_working_hands_nothing_over_and_says_the_quit_was_canceled() {
        let h = QuitHarness()
        let handover = FakeHandover()
        h.quitGuard.handover = handover
        let ticket = h.hold.begin(TerminationHold.building, stop: nil)
        XCTAssertEqual(h.quitGuard.shouldTerminate(), .terminateLater)
        h.answer?(.keepWorking)
        XCTAssertEqual(h.replies, [false])
        XCTAssertEqual(handover.offers, 0)
        XCTAssertEqual(handover.canceled, 1)
        h.hold.end(ticket)
    }

    func test_work_that_ends_while_asking_is_handed_over_before_the_quit() {
        let h = QuitHarness()
        let handover = FakeHandover()
        h.quitGuard.handover = handover
        let ticket = h.hold.begin(TerminationHold.decrypting, stop: nil)
        XCTAssertEqual(h.quitGuard.shouldTerminate(), .terminateLater)
        h.step()
        XCTAssertEqual(handover.offers, 0, "not while it runs")
        h.hold.end(ticket)
        h.runDown()
        XCTAssertEqual(h.takeDowns, 1)
        XCTAssertEqual(handover.offers, 1)
        XCTAssertEqual(h.replies, [], "the quit waits for the handover")
        handover.done?(true)
        XCTAssertEqual(h.replies, [true])
    }

    /// Quit Anyway: handed over once the stopped work has ended, and never
    /// while it still holds.
    func test_quit_anyway_hands_over_only_once_the_work_has_ended() {
        let stubborn = QuitHarness()
        let first = FakeHandover()
        stubborn.quitGuard.handover = first
        _ = stubborn.hold.begin(TerminationHold.building) {}
        XCTAssertEqual(stubborn.quitGuard.shouldTerminate(), .terminateLater)
        stubborn.answer?(.quitAnyway)
        stubborn.runDown()
        XCTAssertEqual(stubborn.replies, [true], "quit after the grace")
        XCTAssertEqual(first.offers, 0, "nothing installs while work holds")

        let h = QuitHarness()
        let handover = FakeHandover()
        h.quitGuard.handover = handover
        var ticket: TerminationHold.Ticket?
        ticket = h.hold.begin(TerminationHold.reinstalling) { [unowned h] in
            h.after(0.3) { if let ticket { h.hold.end(ticket) } }
        }
        XCTAssertEqual(h.quitGuard.shouldTerminate(), .terminateLater)
        h.answer?(.quitAnyway)
        h.runDown()
        XCTAssertEqual(handover.offers, 1)
        handover.done?(true)
        XCTAssertEqual(h.replies, [true])
    }

    /// The question names what runs, in plain words, each once.
    func test_the_question_names_what_runs_in_plain_words() {
        let words: [(String, String)] = [
            (TerminationHold.building, "building and installing an app"),
            (TerminationHold.buildingOnly, "building an app"),
            (TerminationHold.exporting, "exporting an IPA"),
            (TerminationHold.gettingFromAppStore, "getting an app from the App Store"),
            (TerminationHold.staging, "staging an IPA for a build"),
            (TerminationHold.claiming, "getting a free app for your Apple ID"),
            (TerminationHold.settingUp, "setting up your signing account"),
            (TerminationHold.enrolling, "setting up App Store decryption"),
            (TerminationHold.liveContainer, "changing LiveContainer on a device"),
            (TerminationHold.reinstalling, "re-installing a build on a device"),
            (TerminationHold.decrypting, "decrypting an app from a device"),
        ]
        for (reason, phrase) in words {
            XCTAssertEqual(QuitQuestion(reasons: [reason]).title, "Quit while Graft is \(phrase)?", reason)
        }
        XCTAssertEqual(
            QuitQuestion(reasons: [TerminationHold.building, TerminationHold.decrypting]).title,
            "Quit while Graft is building and installing an app and decrypting an app from a device?"
        )
        XCTAssertEqual(
            QuitQuestion(reasons: [
                TerminationHold.gettingFromAppStore, TerminationHold.building,
                TerminationHold.gettingFromAppStore, TerminationHold.decrypting,
            ]).title,
            "Quit while Graft is getting an app from the App Store, building and installing an app, "
                + "and decrypting an app from a device?",
            "each once, in order"
        )
        XCTAssertEqual(QuitQuestion(reasons: ["something new"]).title, "Quit while Graft is working?")
        XCTAssertEqual(
            QuitQuestion(reasons: ["something new", TerminationHold.staging]).title,
            "Quit while Graft is staging an IPA for a build?"
        )
        let detail = QuitQuestion(reasons: [TerminationHold.building]).detail
        XCTAssertTrue(detail.contains(QuitQuestion.keepWorking) && detail.contains(QuitQuestion.quitAnyway), detail)
    }

    func test_the_question_names_an_update_of_apps() {
        XCTAssertEqual(
            QuitQuestion(reasons: [TerminationHold.updatingApps]).title,
            "Quit while Graft is updating your apps?"
        )
    }

    /// A Mac build that has handed its app to PlayCover and waits for the
    /// install is named for that in the quit question, not "building an
    /// app" (review part 2, COPY), and goes back once the wait ends.
    func test_a_mac_builds_playcover_wait_is_named_in_the_quit_question() async {
        let control = FakeControl()
        let hold = TerminationHold(control: control)
        let config = ConfigStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")), notify: { _ in },
                                 hold: hold)
        var release: CheckedContinuation<Void, Never>?
        config.launchBuild(appName: "example", delivery: .notDelivered(reason: "Build for this Mac")) {
            await withCheckedContinuation { release = $0 }
        }
        await eventually({ release != nil }, "the build never started")
        XCTAssertEqual(hold.reasons, [TerminationHold.buildingOnly])

        config.noteWaitingForPlayCover("example")

        XCTAssertEqual(hold.reasons, [TerminationHold.waitingForPlayCover], "the reason Install to Mac… holds for")
        XCTAssertEqual(QuitQuestion(reasons: hold.reasons).title,
                       "Quit while Graft is waiting for PlayCover to install an app?")
        config.noteWaitingForPlayCover(nil)
        XCTAssertEqual(hold.reasons, [TerminationHold.buildingOnly])
        release?.resume()
        await eventually({ !hold.isHolding }, "the build's hold never ended")
        XCTAssertTrue(control.isBalanced, "begun and ended under its first reason")
    }

    /// An update's Stop lets an install it has begun finish, and Quit
    /// Anyway waits for that (spec 4.5): the question says so rather than
    /// that everything is left unfinished (review part 2, COPY).
    func test_the_question_says_an_updates_install_finishes_first() {
        XCTAssertEqual(
            QuitQuestion(reasons: [TerminationHold.updatingApps]).detail,
            "Quit Anyway stops what Graft is doing and leaves it unfinished. An install the update has begun "
                + "finishes first, for up to a minute. Keep Working cancels the quit, and any logout or "
                + "restart, so it can finish. If it finishes while this message is open, Graft quits then."
        )
        XCTAssertTrue(QuitQuestion(reasons: [TerminationHold.updatingApps, TerminationHold.removingData],
                                   withoutStop: [TerminationHold.removingData]).detail
            .hasSuffix(QuitQuestion.updateFinishes))
        XCTAssertFalse(QuitQuestion(reasons: [TerminationHold.building]).detail.contains("finishes first"))
        XCTAssertEqual(QuitGuard.finishGrace, 60, "\"up to a minute\"")
    }

    /// Work with no stop: the question says Quit Anyway can't stop it and
    /// Graft quits when it finishes, not that Quit Anyway stops it.
    func test_the_question_says_when_graft_waits_for_work_it_cannot_stop() {
        let setUp = "Graft can't stop setting up your signing account partway. With Quit Anyway, "
            + "Graft will quit when it finishes. Keep Working cancels the quit, and any logout or restart."
        XCTAssertEqual(
            QuitQuestion(reasons: [TerminationHold.settingUp], withoutStop: [TerminationHold.settingUp]).detail,
            setUp
        )
        XCTAssertEqual(
            QuitQuestion(
                reasons: [TerminationHold.building, TerminationHold.removingData],
                withoutStop: [TerminationHold.removingData]
            ).detail,
            "Graft can't stop removing its data partway. With Quit Anyway, Graft stops everything else, "
                + "leaving it unfinished, and will quit when removing its data finishes. Keep Working "
                + "cancels the quit, and any logout or restart."
        )
        XCTAssertEqual(
            QuitQuestion(reasons: ["something new"], withoutStop: ["something new"]).detail,
            "Graft can't stop what it's doing partway. With Quit Anyway, Graft will quit when it "
                + "finishes. Keep Working cancels the quit, and any logout or restart."
        )
        XCTAssertEqual(QuitQuestion(reasons: [TerminationHold.building]).detail, QuitQuestion.detail)
        XCTAssertEqual(QuitQuestion(reasons: [TerminationHold.building], withoutStop: []).detail,
                       QuitQuestion.detail)

        // What `QuitGuard` asks, from the hold.
        let h = QuitHarness()
        _ = h.hold.begin(TerminationHold.settingUp, stop: nil)
        XCTAssertEqual(h.quitGuard.shouldTerminate(), .terminateLater)
        XCTAssertEqual(h.questions.map(\.detail), [setUp])
        let other = QuitHarness()
        _ = other.hold.begin(TerminationHold.settingUp) {}
        XCTAssertEqual(other.quitGuard.shouldTerminate(), .terminateLater)
        XCTAssertEqual(other.questions.map(\.detail), [QuitQuestion.detail], "it has a stop")
    }

    // MARK: A build

    @MainActor
    private final class Gate {
        var open = false
        var finished = false
    }

    /// Quit Anyway stops it through Stop (`cancelBuild`), which records the
    /// stop as the user's.
    func test_a_build_holds_until_it_ends_whether_it_finishes_or_is_stopped() async throws {
        for ending in ["finishes", "stopped", "quit"] {
            let control = FakeControl()
            let hold = TerminationHold(control: control)
            let store = ConfigStore(
                runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/true")),
                notify: { _ in }, hold: hold
            )
            let gate = Gate()
            let started = store.launchBuild(appName: "example", delivery: .unknown) {
                while !gate.open, !Task.isCancelled { try? await Task.sleep(nanoseconds: 10_000_000) }
                gate.finished = true
            }
            XCTAssertTrue(started)
            await eventually { control.isHeld(TerminationHold.building) }
            XCTAssertFalse(control.isBalanced, "held while it runs")
            XCTAssertEqual(hold.reasons, [TerminationHold.building])
            switch ending {
            case "stopped": store.cancelBuild(appName: "example")
            case "quit": hold.stopAll()
            default: gate.open = true
            }
            await eventually({ gate.finished }, ending)
            await eventually({ control.isLetGo(TerminationHold.building) && control.isBalanced }, ending)
            XCTAssertEqual(store.wasCancelled("example"), ending != "finishes", ending)
        }
    }

    /// The build slot holds for what it runs, and the quit question says
    /// that: an export isn't "building and installing an app", nor is a
    /// build that installs nothing.
    func test_a_build_holds_for_what_it_runs() async throws {
        let kinds: [(name: String, reason: String, title: String)] = [
            ("export", TerminationHold.exporting, "Quit while Graft is exporting an IPA?"),
            ("build", TerminationHold.building, "Quit while Graft is building and installing an app?"),
            ("sideload", TerminationHold.building, "Quit while Graft is building and installing an app?"),
            ("dry run", TerminationHold.buildingOnly, "Quit while Graft is building an app?"),
            ("this Mac", TerminationHold.buildingOnly, "Quit while Graft is building an app?"),
        ]
        for kind in kinds {
            let control = FakeControl()
            let hold = TerminationHold(control: control)
            let store = ConfigStore(
                runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/true")),
                notify: { _ in }, hold: hold
            )
            let gate = Gate()
            let work: @MainActor () async -> Void = {
                while !gate.open, !Task.isCancelled { try? await Task.sleep(nanoseconds: 10_000_000) }
                gate.finished = true
            }
            let started: Bool
            switch kind.name {
            case "export":
                started = store.launchExport(
                    appName: "example", delivery: .notDelivered(reason: "Exported to a file"),
                    staging: nil, work
                )
            case "build": started = store.launchBuild(appName: "example", delivery: .livecontainer, work)
            case "sideload": started = store.launchBuild(appName: "example", delivery: .sideload, work)
            case "dry run":
                started = store.launchBuild(appName: "example", delivery: .notDelivered(reason: "Dry run"), work)
            default:
                started = store.launchBuild(
                    appName: "example", delivery: .notDelivered(reason: "Built for this Mac"), work
                )
            }
            XCTAssertTrue(started, kind.name)
            await eventually({ control.isHeld(kind.reason) }, kind.name)
            XCTAssertEqual(hold.reasons, [kind.reason], kind.name)
            XCTAssertEqual(QuitQuestion(reasons: hold.reasons).title, kind.title, kind.name)
            gate.open = true
            await eventually({ gate.finished && control.isBalanced }, kind.name)
        }
    }

    // MARK: App Store: get, drop, Get (free)

    private static let provided = #"{"type":"ipa-provided","app":"example","bundle_id":"com.example.app","version":"2.0","filename":"com.example.app_2.0.ipa","path":"/c/com.example.app_2.0.ipa","size_bytes":1}"#
    private static let version = #"{"type":"appstore-version","app":"example","bundle_id":"com.example.app","store_version":"2.0","cached_version":"2.0","update_available":false,"error":null}"#

    func test_a_get_and_a_drop_hold_until_they_end_however_they_end() async throws {
        // A get that stages, one that fails, one that is stopped; and a drop.
        // Quit Anyway stops either ("quit", "drop-quit").
        for ending in ["stages", "fails", "stopped", "drop", "quit", "drop-quit"] {
            try? FileManager.default.removeItem(at: dir.appending(path: "go"))
            let control = FakeControl()
            let runner = try cli("""
                case "$*" in
                  *--appstore-check*) printf '%s\\n' '\(Self.version)' ;;
                  *--appstore-get*|*--provide-ipa*)
                    \(wait)
                    if [ '\(ending)' = fails ]; then echo "error: the download failed" >&2; exit 20; fi
                    printf '%s\\n' '\(Self.provided)' ;;
                esac
                """)
            let hold = TerminationHold(control: control)
            let store = AppStoreStore(runner: runner, shell: NoAdminShell(), hold: hold)
            let reason: String
            if ending.hasPrefix("drop") {
                let config = ConfigStore(runner: runner, notify: { _ in })
                XCTAssertTrue(store.startProvide(app: "example", fileURL: URL(fileURLWithPath: "/u/Example.ipa"), config: config))
                reason = TerminationHold.staging
            } else {
                XCTAssertTrue(store.startGet(app: "example"))
                reason = TerminationHold.gettingFromAppStore
            }
            await eventually({ control.isHeld(reason) }, ending)
            switch ending {
            case "stopped": store.stopGet(app: "example")
            case "quit", "drop-quit": hold.stopAll()
            default: try go()
            }
            await eventually({ !store.isBusy("example") }, ending)
            await eventually({ control.isLetGo(reason) && control.isBalanced }, ending)
            XCTAssertEqual(store.errors["example"] != nil, ending == "fails", ending)
            XCTAssertEqual(store.staged["example"] != nil, ending == "stages" || ending == "drop", ending)
        }
    }

    func test_get_free_holds_until_the_claim_ends() async throws {
        for ending in ["works", "stopped", "quit"] {
            try? FileManager.default.removeItem(at: dir.appending(path: "go"))
            try? FileManager.default.removeItem(at: dir.appending(path: "claimed"))
            let control = FakeControl()
            let runner = try cli("""
                case "$*" in
                  *--appstore-status*)
                    printf '%s\\n' '{"type":"appstore-status","ipatool":{"path":"/i","present":true,"version":"2.8.0","signed_in":true,"warning":null,"email":"a@example.invalid","legacy_signin":false,"source":"bundled"},"unfairplay":{"path":"/u","present":true,"version":"v0.4.0","enrolled":true,"model_dir":"/m"},"setup_commands":[]}' ;;
                  *--appstore-check*) printf '%s\\n' '\(Self.version)' ;;
                  *--appstore-claim*)
                    \(wait)
                    touch '\(dir.path)/claimed'
                    printf '%s\\n' '{"type":"appstore-claimed","app":"example","bundle_id":"com.example.app","already_owned":false}' ;;
                  *--appstore-get*)
                    if [ -e '\(dir.path)/claimed' ]; then printf '%s\\n' '\(Self.provided)'; exit 0; fi
                    printf '%s\\n' '{"type":"appstore-refused","app":"example","reason":"needs-license","price":0,"formatted_price":"Free","track_id":100000001}'
                    exit 20 ;;
                esac
                """)
            let hold = TerminationHold(control: control)
            let store = AppStoreStore(runner: runner, shell: NoAdminShell(), hold: hold)
            store.startGet(app: "example")
            await eventually { !store.isBusy("example") }
            XCTAssertEqual(store.licenseNeeded["example"]?.offersClaim, true, "premise")
            await store.checkBeforeClaim(app: "example")
            XCTAssertTrue(store.startClaim(app: "example", confirmed: try XCTUnwrap(store.claimConfirmations["example"])))
            await eventually { control.isHeld(TerminationHold.claiming) }
            switch ending {
            case "stopped": store.stopGet(app: "example")
            case "quit": hold.stopAll()
            default: try go()
            }
            await eventually({ !store.isBusy("example") }, ending)
            await eventually({ control.isLetGo(TerminationHold.claiming) && control.isBalanced }, ending)
            XCTAssertEqual(store.completions["example"], ending == "works" ? 1 : nil, "claimed, then got: \(ending)")
        }
    }

    // MARK: --init

    private static let freeResult = #"{"type":"init-result","account":"free","config":"/tmp/example/config.yaml","change":"created","dry_run":false,"summary":"Set up for a free Apple ID.","team_id":null,"profile_name":null,"certificate":null,"device_count":null,"devices":[],"expires":null,"pinned":[],"sideloads":[],"steps":[]}"#

    func test_an_init_setup_holds_until_it_ends_whether_it_works_or_not() async throws {
        for works in [true, false] {
            try? FileManager.default.removeItem(at: dir.appending(path: "go"))
            let control = FakeControl()
            let runner = try cli("""
                \(wait)
                if \(works ? "true" : "false"); then printf '%s\\n' '\(Self.freeResult)'; exit 0; fi
                echo "error: config.yaml is not writable" >&2
                exit 2
                """)
            let hold = TerminationHold(control: control)
            let store = AccountSetupStore(runner: runner, hold: hold)
            let run = Task { await store.chooseFree() }
            await eventually({ control.isHeld(TerminationHold.settingUp) }, "works: \(works)")
            // It has no Stop, so Quit Anyway's stop leaves it to finish.
            hold.stopAll()
            try go()
            await run.value
            XCTAssertTrue(control.isLetGo(TerminationHold.settingUp), "works: \(works)")
            XCTAssertTrue(control.isBalanced, "works: \(works)")
            XCTAssertEqual(store.error == nil, works)
        }
    }

    /// A setup that writes has no Stop: the quit question says Graft quits
    /// when it finishes, which is what Quit Anyway then does.
    func test_an_init_write_has_no_stop_so_the_question_says_graft_waits_for_it() async throws {
        let control = FakeControl()
        let runner = try cli("""
            \(wait)
            printf '%s\\n' '\(Self.freeResult)'
            """)
        let hold = TerminationHold(control: control)
        let store = AccountSetupStore(runner: runner, hold: hold)
        let run = Task { await store.chooseFree() }
        await eventually({ control.isHeld(TerminationHold.settingUp) })

        XCTAssertEqual(hold.reasonsWithoutStop, [TerminationHold.settingUp])
        XCTAssertEqual(
            QuitQuestion(reasons: hold.reasons, withoutStop: hold.reasonsWithoutStop).detail,
            QuitQuestion.waitingDetail("setting up your signing account", others: false)
        )
        try go()
        await run.value
        XCTAssertTrue(control.isBalanced)
    }

    /// A review writes nothing and has Cancel: Quit Anyway stops it the same
    /// way, and the question is the usual one.
    func test_an_init_review_is_stopped_by_quit_anyway() async throws {
        let control = FakeControl()
        let runner = try cli(wait)
        let hold = TerminationHold(control: control)
        let store = AccountSetupStore(runner: runner, hold: hold)
        store.choosePaid()
        store.form.p12Path = "/private/var/empty/example.p12"
        store.form.profilePath = "/private/var/empty/example.mobileprovision"
        store.startReview()
        await eventually({ control.isHeld(TerminationHold.settingUp) })

        XCTAssertEqual(hold.reasonsWithoutStop, [])
        XCTAssertEqual(QuitQuestion(reasons: hold.reasons, withoutStop: hold.reasonsWithoutStop).detail,
                       QuitQuestion.detail)
        hold.stopAll()
        await eventually({ control.isLetGo(TerminationHold.settingUp) }, "stopped, well before its 30 s wait")
        XCTAssertTrue(control.isBalanced)
        XCTAssertEqual(store.step, .paidForm, "still on the form")
        XCTAssertNil(store.error, "a stop isn't a failure")
    }

    // MARK: Enrollment

    /// Notes whether the hold was on when the root run began, and ends that
    /// run however the test says.
    final class SeeingShell: PrivilegedShell, @unchecked Sendable {
        let control: FakeControl
        var error: AdminShellError?
        /// Run while root's run goes, before it ends.
        var during: (@MainActor () -> Void)?
        private(set) var heldWhenRun: Bool?

        init(control: FakeControl) { self.control = control }

        func run(_ script: String, prompt: String) async throws -> AdminRun {
            heldWhenRun = control.isHeld(TerminationHold.enrolling)
            if let during { await during() }
            if let error { throw error }
            return AdminRun(output: "{\"success\":true}\n/private/var/tmp/graft-enroll.AbCd1234\n", via: .plain)
        }
    }

    /// Quit Anyway stops the root run as Cancel does: it creates the cancel
    /// file, and the script stops (exit 67).
    func test_an_enrolment_holds_through_the_root_run_and_the_finish_however_it_ends() async throws {
        let cancelFile = dir.appending(path: "cancel").path
        let command = #"{"type":"appstore-enroll-command","app":"/Applications/Example.app","model_dir":"/u/m","script":"echo ok\n","verified":true,"cancel_file":""# + cancelFile + #""}"#
        try (command + "\n").write(to: dir.appending(path: "command.json"), atomically: true, encoding: .utf8)
        let runner = try cli("""
            case "$*" in
              *--appstore-enroll-command*) /bin/cat '\(dir.path)/command.json' ;;
              *--appstore-enroll-finish*) printf '%s\\n' '{"type":"appstore-enrolled","model_dir":"/u/m","count":1}' ;;
            esac
            """)
        for ending in ["done", "canceled", "quit"] {
            let control = FakeControl()
            let hold = TerminationHold(control: control)
            let shell = SeeingShell(control: control)
            var stopAsked = false
            switch ending {
            case "canceled":
                shell.error = AdminShellError(number: AdminShellError.userCancelled, message: "User canceled.")
            case "quit":
                shell.during = {
                    hold.stopAll()
                    stopAsked = FileManager.default.fileExists(atPath: cancelFile)
                }
                shell.error = AdminShellError(number: CLIContract.EnrolExit.stopped, message: "stopped")
            default: break
            }
            let store = AppStoreAccountStore(runner: runner, shell: shell, hold: hold)
            await store.prepareEnrol(appPath: "/Applications/Example.app")
            guard case .ready = store.enrol else { return XCTFail("expected .ready, got \(store.enrol)") }
            await store.confirmEnrol()
            let expected: AppStoreAccountStore.Enrol = ending == "done" ? .done : ending == "quit" ? .stopped : .cancelled
            XCTAssertEqual(store.enrol, expected, ending)
            XCTAssertEqual(stopAsked, ending == "quit", "the cancel file, while root runs: \(ending)")
            XCTAssertFalse(FileManager.default.fileExists(atPath: cancelFile), "removed after: \(ending)")
            XCTAssertEqual(shell.heldWhenRun, true, "held while root runs: \(ending)")
            XCTAssertTrue(control.isLetGo(TerminationHold.enrolling), ending)
            XCTAssertTrue(control.isBalanced, ending)
        }
    }

    // MARK: LiveContainer

    func test_a_livecontainer_install_holds_until_it_ends_whether_it_works_fails_or_is_stopped() async throws {
        for ending in ["works", "fails", "stopped", "quit"] {
            try? FileManager.default.removeItem(at: dir.appending(path: "go"))
            let control = FakeControl()
            let runner = try cli("""
                case "$*" in
                  *--lc-install*)
                    \(wait)
                    if [ '\(ending)' = fails ]; then echo "error: the device went away" >&2; exit 8; fi ;;
                esac
                """)
            let hold = TerminationHold(control: control)
            let store = LiveContainerStore(runner: runner, hold: hold)
            store.startInstall(udid: "U", instances: [1], channel: nil)
            await eventually({ control.isHeld(TerminationHold.liveContainer) }, ending)
            switch ending {
            case "stopped": store.stop()
            case "quit": hold.stopAll()
            default: try go()
            }
            await store.currentAction?.value
            await eventually({ control.isLetGo(TerminationHold.liveContainer) && control.isBalanced }, ending)
            XCTAssertEqual(store.run(for: "U")?.error != nil, ending != "works", ending)
        }
    }

    /// Set Up LiveContainer is two runs, `--lc-install` then
    /// `--lc-push-cert`, with a status read between them. Each run held the
    /// quit on its own, so ⌘Q during that read quit without asking, and
    /// LiveContainer was left with no certificate (review 23-2, §11). The
    /// whole Set Up holds; Quit Anyway there stops it before the push.
    func test_set_up_livecontainer_holds_from_the_install_through_the_certificate() async throws {
        for ending in ["works", "quit"] {
            for marker in ["go", "reading", "pushed"] {
                try? FileManager.default.removeItem(at: dir.appending(path: marker))
            }
            let control = FakeControl()
            let runner = try cli("""
                case "$*" in
                  *--lc-status*)
                    touch '\(dir.path)/reading'
                    \(wait) ;;
                  *--lc-push-cert*) touch '\(dir.path)/pushed' ;;
                esac
                """)
            let hold = TerminationHold(control: control)
            let store = LiveContainerStore(runner: runner, hold: hold)
            store.startSetUp(udid: "U", instances: [1], channel: nil)
            await eventually({ FileManager.default.fileExists(atPath: self.dir.appending(path: "reading").path) },
                             "the install never ended: \(ending)")
            XCTAssertEqual(store.setUp?.states[.pushCert], .waiting, "premise: between the two runs: \(ending)")
            XCTAssertTrue(control.isHeld(TerminationHold.liveContainer),
                          "nothing held the quit between --lc-install and --lc-push-cert: \(ending)")
            if ending == "quit" { hold.stopAll() }
            try go()
            await store.currentAction?.value
            await eventually({ control.isLetGo(TerminationHold.liveContainer) && control.isBalanced }, ending)
            let pushed = FileManager.default.fileExists(atPath: dir.appending(path: "pushed").path)
            XCTAssertEqual(pushed, ending == "works", "Quit Anyway stops Set Up before its push: \(ending)")
            XCTAssertEqual(store.setUp?.states[.pushCert], ending == "works" ? .done : .stopped, ending)
        }
    }

    // MARK: Re-install (the try14 delta audit, D14-APP-2)

    private static let reinstallEnd = #"{"type":"build-end","app":"example","ok":true,"stopped":false,"error":null}"#

    /// `--reinstall-build` pushes to the phone and records a delivery, as a
    /// build does. It holds until it ends: it works, it fails, it is stopped
    /// (the sheet's `.task` canceled, or Quit Anyway), or the CLI can't even
    /// start, which throws.
    func test_a_reinstall_holds_until_it_ends_however_it_ends() async throws {
        for ending in ["works", "fails", "stopped", "quit", "unstartable"] {
            try? FileManager.default.removeItem(at: dir.appending(path: "go"))
            try? FileManager.default.removeItem(at: dir.appending(path: "started"))
            let control = FakeControl()
            let runner: CLIRunner
            if ending == "unstartable" {
                runner = CLIRunner(executable: dir.appending(path: "no-such-graft"))
            } else {
                runner = try cli("""
                    case "$*" in
                      *"--reinstall-build 7"*)
                        touch '\(dir.path)/started'
                        \(wait)
                        if [ '\(ending)' = fails ]; then echo "error: no iOS devices connected" >&2; exit 8; fi
                        printf '%s\\n' '\(Self.reinstallEnd)' ;;
                    esac
                    """)
            }
            let hold = TerminationHold(control: control)
            let run = Task { @MainActor in
                try await ReinstallSheet.reinstall(build: 7, runner: runner, hold: hold) { _ in }
            }
            if ending != "unstartable" {
                await eventually({ control.isHeld(TerminationHold.reinstalling) }, ending)
                // Stopped once the CLI runs: a stop before it starts throws,
                // which "unstartable" covers.
                let started = dir.appending(path: "started").path
                await eventually({ FileManager.default.fileExists(atPath: started) }, ending)
                switch ending {
                case "stopped": run.cancel()
                case "quit": hold.stopAll()
                default: try go()
                }
            }
            let result = try? await run.value
            XCTAssertTrue(control.isLetGo(TerminationHold.reinstalling), ending)
            XCTAssertTrue(control.isBalanced, ending)
            XCTAssertEqual(result?.exitCode == 0, ending == "works", ending)
            XCTAssertEqual(result == nil, ending == "unstartable", "only that one throws: \(ending)")
        }
    }

    /// The sheet's own run goes through that hold: hosted, with the
    /// stand-in waiting, Graft is held until the re-install ends.
    func test_the_reinstall_sheet_holds_while_its_run_goes() async throws {
        let runner = try cli("""
            case "$*" in
              *"--reinstall-build 7"*)
                \(wait)
                printf '%s\\n' '\(Self.reinstallEnd)' ;;
            esac
            """)
        let line = #"{"type":"build-record","id":7,"timestamp":"2026-11-17T10:00:00Z","app":"example","bundle_id":"com.example.app","version":"1.0","tweaks":[],"ipa_path":"/b/x.ipa","ipa_sha256":null,"log_path":null,"signing_team":null,"udid":null,"target":"device","success":true,"error":null}"#
        guard case let .buildRecord(build)? = line.buildEvents().first else {
            return XCTFail("build-record fixture did not decode")
        }
        let control = FakeControl()
        let sh = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let sheet = ReinstallSheet(
            build: build, runner: runner,
            config: ConfigStore(runner: sh, cliArgs: ["-c", "true"], notify: { _ in }),
            artwork: ArtworkStore(runner: sh), appUpdates: nil, hold: TerminationHold(control: control), onClose: {}
        )
        let box = hosted(sheet, size: NSSize(width: 640, height: 460))
        await eventually { control.isHeld(TerminationHold.reinstalling) }
        try go()
        await eventually { control.isLetGo(TerminationHold.reinstalling) && control.isBalanced }
        box.window.orderOut(nil)
    }

    // MARK: Decrypt (the try14 delta audit, D14-APP-2)

    private static let decryptEnd = #"{"type":"decrypt-end","bundle_id":"com.example.app","version":"2.0","path":"/c/com.example.app_2.0.ipa","binaries":1}"#

    /// A dump stages an IPA and takes minutes, and closing the window doesn't
    /// stop it. It holds until it ends, a row's dump or the frontmost app's:
    /// it stages, it fails, or it is stopped (its task canceled, or Quit
    /// Anyway).
    func test_a_decrypt_dump_holds_until_it_ends_however_it_ends() async throws {
        let app = DeviceAppRecord(bundleId: "com.example.app", name: "Example", version: "2.0", encrypted: true)
        for ending in ["stages", "frontmost", "fails", "stopped", "quit"] {
            try? FileManager.default.removeItem(at: dir.appending(path: "go"))
            let control = FakeControl()
            let runner = try cli("""
                case "$*" in
                  *--decrypt*)
                    \(wait)
                    if [ '\(ending)' = fails ]; then echo "error: the app quit during the dump" >&2; exit 1; fi
                    printf '%s\\n' '\(Self.decryptEnd)' ;;
                esac
                """)
            let hold = TerminationHold(control: control)
            let store = DeviceAppsStore(runner: runner, hold: hold)
            let run = Task { @MainActor () -> DecryptEndRecord? in
                if ending == "frontmost" { return await store.decryptFrontmost(device: "U") }
                return await store.decrypt(app, device: "U")
            }
            await eventually({ control.isHeld(TerminationHold.decrypting) }, ending)
            switch ending {
            case "stopped": run.cancel()
            case "quit": hold.stopAll()
            default: try go()
            }
            let record = await run.value
            XCTAssertTrue(control.isLetGo(TerminationHold.decrypting), ending)
            XCTAssertTrue(control.isBalanced, ending)
            XCTAssertEqual(record != nil, ending == "stages" || ending == "frontmost", ending)
            XCTAssertFalse(store.isDumping, ending)
        }
    }

    // MARK: Install to Mac (review 23-2, §11)

    private static let handedLine = #"{"type":"handed-to-playcover","filename":"com.example.app_2.0.ipa","path":"/c/com.example.app_2.0.ipa","bundle_id":"com.example.app","display_name":"Example"}"#
    private static let installedLine = #"{"type":"playcover-installed","bundle_id":"com.example.app","version":"2.0","display_name":"Example","settings_disabled":[],"settings_restored":[]}"#

    /// Install to Mac… from Builds or the Cache: after the handoff the CLI
    /// waits up to two minutes for PlayCover's install, and nothing held the
    /// quit, so ⌘Q quit without asking. It holds now until the wait ends:
    /// the install lands, the strip's Stop, or Quit Anyway, which stops it
    /// as Stop does. Each is ended after the handoff: one before it is
    /// `test_a_stop_before_the_handoff_is_not_a_refusal`'s.
    func test_install_to_mac_holds_while_it_waits_for_playcover_however_it_ends() async throws {
        for ending in ["installs", "stopped", "quit"] {
            for marker in ["go", "handed"] {
                try? FileManager.default.removeItem(at: dir.appending(path: marker))
            }
            let control = FakeControl()
            let runner = try cli("""
                case "$*" in
                  *--install-to-mac*)
                    printf '%s\\n' '\(Self.handedLine)'
                    touch '\(dir.path)/handed'
                    \(wait)
                    printf '%s\\n' '\(Self.installedLine)' ;;
                esac
                """)
            let hold = TerminationHold(control: control)
            let store = StagedIPAStore(runner: runner, hold: hold)
            let run = Task { @MainActor in
                await store.installToMac(filename: "com.example.app_2.0.ipa", holdingQuit: true)
            }
            await eventually({ control.isHeld(TerminationHold.waitingForPlayCover) }, ending)
            await eventually({ FileManager.default.fileExists(atPath: self.dir.appending(path: "handed").path) },
                             "premise: the stand-in handed it over: \(ending)")
            switch ending {
            case "stopped": run.cancel()
            case "quit": hold.stopAll()
            default: try go()
            }
            let install = await run.value
            await eventually({ control.isLetGo(TerminationHold.waitingForPlayCover) && control.isBalanced }, ending)
            if ending == "installs" {
                guard case .installed = install.ending else { return XCTFail("\(install.ending)") }
            } else {
                XCTAssertEqual(install.ending, .stopped, "a Stop ends the wait, never the handoff: \(ending)")
            }
        }
    }

    /// The app pane's handoff runs inside its Mac build, whose hold already
    /// covers the wait (`ConfigStore.launchBuild`): the store holds nothing
    /// of its own for it.
    func test_the_app_panes_handoff_holds_nothing_of_its_own() async throws {
        let control = FakeControl()
        let runner = try cli("""
            printf '%s\\n' '\(Self.handedLine)' '\(Self.installedLine)'
            """)
        let store = StagedIPAStore(runner: runner, hold: TerminationHold(control: control))
        let install = await store.installToMac(filename: "com.example.app_2.0.ipa")
        guard case .installed = install.ending else { return XCTFail("\(install.ending)") }
        XCTAssertFalse(control.isLetGo(TerminationHold.waitingForPlayCover), "it held the quit")
        XCTAssertTrue(control.isBalanced)
    }

    func test_the_question_names_a_wait_for_playcover() {
        XCTAssertEqual(
            QuitQuestion(reasons: [TerminationHold.waitingForPlayCover]).title,
            "Quit while Graft is waiting for PlayCover to install an app?"
        )
    }
}
