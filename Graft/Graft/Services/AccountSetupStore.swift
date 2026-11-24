import Foundation
import Observation

private typealias Flag = CLIContract.Flag

/// The Apple-account setup the app runs in place of `graft --init` in
/// Terminal. On the first-run screen it is Welcome, then "Do you have a paid
/// Apple Developer account?", then the free checklist or the signing files.
/// In Settings › Account it is Add a paid account and Switch to a free Apple ID.
///
/// Every step that changes anything is one `graft --init --account …` call
/// (`account_setup` in the CLI), which does the work and words every refusal.
/// This holds only where the user is and what they have typed.
///
/// **The .p12 password.** Typed into a SecureField, sent to the CLI on stdin,
/// never in argv or the environment, and never written anywhere by the app:
/// not UserDefaults, not a file, not a log. `form.password` keeps it only
/// between a review that worked and the confirm that needs it again. Every
/// other way out clears it: a confirm, whatever it came to; a review that
/// failed; Back, Cancel and Finish.
@MainActor
@Observable
final class AccountSetupStore {
    enum Step: Equatable {
        case welcome
        case question
        case paidForm
        /// The dry run's answer, before anything is written: the team, the
        /// certificate, the expiry and the devices the profile covers.
        case paidReview(InitResultRecord)
        /// What was set up, and for a free Apple ID what to do on the device.
        case done(InitResultRecord)
    }

    struct PaidForm: Equatable {
        var p12Path = ""
        var password = ""
        var profilePath = ""

        /// Both files chosen. Not the password: Keychain Access can export a
        /// .p12 with a blank one, and the CLI says so when one is needed.
        var isReady: Bool { !p12Path.isEmpty && !profilePath.isEmpty }
    }

    let runner: CLIRunner
    private(set) var step: Step = .welcome
    var form = PaidForm()
    private(set) var isRunning = false
    /// A run that writes: a setup, a replace, a switch to a free Apple ID.
    /// It can't be stopped halfway (the CLI undoes a failure itself, and a
    /// Stop mid-write is what it has to undo), so while it runs Back and
    /// Cancel are not offered. A review (`--dry-run`) writes nothing and can
    /// be canceled.
    private(set) var isWriting = false
    /// What to re-read once a run that writes has finished, whatever it came
    /// to: Settings › Account sets it (`ContentView.afterAccountChange`), so
    /// the account every pane gates on is never left stale, even when the
    /// Settings pane was left while the run went on.
    var onAccountRunFinished: (() async -> Void)?
    /// The review in flight, which Cancel and Back stop.
    private var reviewTask: Task<Void, Never>?
    /// Bumped by every reset: a run that comes back after one is dropped.
    private var generation = 0
    /// Settings › Account's Replace certificate and profile: the paid flow,
    /// sent with `--replace`, for a config that is already paid.
    private(set) var replacing = false
    /// The last run's `init-step` sentences, in order.
    private(set) var progress: [InitStepRecord] = []
    var error: String?

    /// Holds sudden and automatic termination off while `--init` runs
    /// (whole-diff review, app-3).
    private let hold: TerminationHold

    init(runner: CLIRunner, hold: TerminationHold = .shared) {
        self.runner = runner
        self.hold = hold
    }

    /// Whether the first-run screen has to stay up although config.yaml now
    /// exists: the result, and a free Apple ID's steps, are still being read.
    /// `ContentView.setupScreen` reads it; `finish` lets the screen go.
    var holdsSetupScreen: Bool {
        // While a write runs too: once config.yaml exists, a window return's
        // check clears the need and would take the screen away mid-run.
        if isWriting { return true }
        if case .done = step { return true }
        return false
    }

    nonisolated static let freeArguments = [
        Flag.json, Flag.initSetup, Flag.account, AccountMode.free.rawValue,
    ]

    nonisolated static func paidArguments(
        p12: String, profile: String, dryRun: Bool, replace: Bool = false
    ) -> [String] {
        [Flag.json, Flag.initSetup, Flag.account, AccountMode.paid.rawValue,
         Flag.p12, p12, Flag.profile, profile]
            + (replace ? [Flag.replace] : []) + (dryRun ? [Flag.dryRun] : [])
    }

    /// Welcome's Get started.
    func begin() {
        error = nil
        step = .question
    }

    /// "Yes": the signing files.
    func choosePaid() {
        error = nil
        step = .paidForm
    }

    /// Settings › Account's Replace certificate and profile, on a paid
    /// account: the same form, and every run sent with `--replace`.
    func startReplace() {
        reset()
        replacing = true
        step = .paidForm
    }

    /// Settings › Account's Add a paid account: straight to an empty form.
    func startUpgrade() {
        reset()
        step = .paidForm
    }

    /// "No": a free Apple ID. Also Settings' Switch to a free Apple ID, after
    /// its confirmation. A failure stays where it was, with the CLI's reason.
    func chooseFree() async {
        guard !isRunning else { return }
        let started = generation
        isWriting = true
        let result = await run(Self.freeArguments, stdin: nil, doing: "Set up a free Apple ID")
        isWriting = false
        if let result, generation == started { step = .done(result) }
        await onAccountRunFinished?()
    }

    /// Settings' Switch to a free Apple ID, after its confirmation: from a
    /// clean flow, so a result left from an earlier run can't read as this
    /// one's.
    func switchToFree() async {
        guard !isRunning else { return }
        reset()
        await chooseFree()
    }

    /// Continue, from the form: the review as a task Cancel and Back can stop.
    func startReview() {
        guard !isRunning, form.isReady else { return }
        reviewTask = Task { await self.review() }
    }

    /// Continue: check the .p12, its password and the profile with
    /// `--dry-run`, which writes nothing.
    func review() async {
        guard !isRunning, form.isReady else { return }
        let started = generation
        let password = form.password
        let args = Self.paidArguments(
            p12: form.p12Path, profile: form.profilePath, dryRun: true, replace: replacing
        )
        let result = await run(args, stdin: password, doing: "Check the signing files")
        reviewTask = nil
        // Canceled, or reset meanwhile: the form has already moved on.
        guard generation == started, !Task.isCancelled else { return }
        if let result {
            step = .paidReview(result)
        } else {
            form.password = ""
        }
    }

    /// Set up: the real run. The form's copy of the password is cleared as it
    /// starts; a failure goes back to the form, with the choices kept.
    func confirm() async {
        guard !isRunning, case .paidReview = step else { return }
        let started = generation
        let password = form.password
        form.password = ""
        let args = Self.paidArguments(
            p12: form.p12Path, profile: form.profilePath, dryRun: false, replace: replacing
        )
        isWriting = true
        let result = await run(args, stdin: password, doing: "Set up signing")
        isWriting = false
        if generation == started {
            step = result.map(Step.done) ?? .paidForm
        }
        await onAccountRunFinished?()
    }

    func back() {
        guard !isWriting else { return }
        stopReview()
        form.password = ""
        error = nil
        switch step {
        case .paidReview: step = .paidForm
        case .paidForm: step = .question
        case .question: step = .welcome
        case .welcome, .done: break
        }
    }

    /// Cancel. Not while a write runs (the view doesn't offer it then); a
    /// review in flight is stopped, which stops its CLI.
    func cancel() {
        guard !isWriting else { return }
        reset()
    }

    /// After the done screen has been read: back to the start, for whoever
    /// opens this next.
    func finish() {
        guard !isWriting else { return }
        reset()
    }

    private func stopReview() {
        reviewTask?.cancel()
        reviewTask = nil
    }

    private func reset() {
        stopReview()
        generation += 1
        form = PaidForm()
        replacing = false
        error = nil
        progress = []
        step = .welcome
    }

    /// One `--init` run: its steps into `progress`, its result back, or nil
    /// with the CLI's reason in `error`.
    private func run(_ args: [String], stdin: String?, doing: String) async -> InitResultRecord? {
        isRunning = true
        hold.begin(TerminationHold.settingUp)
        defer {
            hold.end(TerminationHold.settingUp)
            isRunning = false
        }
        error = nil
        progress = []
        do {
            let events = try await runner.runEvents(args, failing: doing, stdin: stdin)
            progress = events.compactMap { event -> InitStepRecord? in
                if case let .initStep(record) = event { return record }
                return nil
            }
            guard let result = events.lazy.compactMap({ event -> InitResultRecord? in
                if case let .initResult(record) = event { return record }
                return nil
            }).first else {
                self.error = "graft didn't say how the setup went, so Graft doesn't assume it worked."
                return nil
            }
            return result
        } catch {
            if !Task.isCancelled { self.error = error.localizedDescription }
            return nil
        }
    }
}
