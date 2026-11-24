import Foundation
import Observation

private typealias Flag = CLIContract.Flag

/// The App Store account, from the app: signing ipatool in and out, and
/// enrolling this Mac for decryption.
///
/// **The Apple ID, the password and the code.** They go one place: the
/// CLI's stdin, through a `ChildInput` that wipes each array as it sends it.
/// None of them is ever an argument, where `ps` would show it (controller
/// decision 3). The password and the code arrive from `SecretField` as
/// `[UInt8]`, and nothing here keeps them: no property, no `UserDefaults`, no
/// log. The CLI types them into ipatool's own prompts
/// (`graft_ipa/appstore_login.py`).
///
/// Owned by `AppStoreStore`, which reads `--appstore-status` again through
/// `onChange` after each sign-in, sign-out and enrollment.
@MainActor
@Observable
final class AppStoreAccountStore {
    let runner: CLIRunner
    /// Read status again. `AppStoreStore` sets it.
    var onChange: @MainActor () async -> Void = {}
    /// The Apple ID may be another one now: a sign-in or a sign-out worked.
    /// `AppStoreStore` sets it, to drop what it knew about the last one's
    /// licenses (review part 4, #8).
    var onAccountChange: @MainActor () -> Void = {}
    /// Why Sign In, Sign Out and Set Up Decryption must wait, or nil.
    /// `AppStoreStore` sets it: not while a get or Get (free) runs, since
    /// ipatool reads the sign-in under them and a sign-in or sign-out changes
    /// it, and enrollment replaces the model a decrypt reads.
    var blockedBy: @MainActor () -> String? = { nil }

    /// A sign-in, sign-out or enrollment is running: gets wait for it.
    var isBusy: Bool { isSigningIn || isSigningOut || isEnrolling }

    enum SignIn: Equatable {
        case idle
        /// Working, with the line to show under the spinner.
        case working(String)
        /// ipatool asked for the 2FA code: show the code field.
        case needsCode
        case signedIn(email: String)
        /// The CLI's sentence, and its hint when it sent one.
        case failed(String, hint: String? = nil)
    }

    private(set) var signIn: SignIn = .idle
    private(set) var isSigningOut = false
    private(set) var signOutError: String?
    private var input: ChildInput?
    private var signInTask: Task<Void, Never>?

    static let startingLine = "Signing in…"
    /// After the password. The first sign-in on a Mac also fetches ipatool's
    /// App Store components (about 320 MB) and its Unicorn emulator from PyPI
    /// while saying nothing; with both cached a sign-in took 20–32 s (Task 0).
    static let preparingLine = "Signing in. The first sign-in on this Mac downloads ipatool's App Store components (about 320 MB) and its Unicorn emulator, which can take several minutes; later ones take under a minute."
    static let checkingLine = "Checking the code…"

    static func waitingLine(_ seconds: Int) -> String {
        "Still signing in (\(seconds) s). A first sign-in downloads about 320 MB before Apple answers; that's normal."
    }

    /// `shell` has no default: the real one comes from `GraftApp`, and a test
    /// passes a recording one (`NoRealDefaultsTests`). `hold` keeps macOS from
    /// ending the app during an enrollment (whole-diff review, app-3).
    /// `openApp` opens an app in macOS (`openEnrolApp`): `GraftApp` passes
    /// NSWorkspace's, a test a recording one, and the default opens nothing.
    init(
        runner: CLIRunner, shell: PrivilegedShell, hold: TerminationHold = .shared,
        openApp: @escaping @MainActor (URL) -> Void = { _ in }
    ) {
        self.runner = runner
        self.shell = shell
        self.hold = hold
        self.openApp = openApp
    }

    private let hold: TerminationHold

    static let lineBreakRefusal = "The Apple ID and the password can't contain a line break. Type them again."

    /// Always with the ipatool the sheet named: the CLI refuses, before it
    /// reads anything, if ipatool now resolves anywhere else (fix-check #10).
    static func signInArgs(expectedIpatool: String) -> [String] {
        [Flag.json, Flag.appstoreLogin, Flag.expectIpatool, expectedIpatool]
    }
    static func signOutArgs() -> [String] { [Flag.json, Flag.appstoreLogout] }

    var isSigningIn: Bool { signInTask != nil }

    /// Start `--appstore-login`. The Apple ID and then the password are
    /// queued on the child's stdin before it launches, and the password is
    /// wiped here. False when a sign-in is running or there is nothing to send.
    ///
    /// The Apple ID is trimmed of spaces and line breaks; one left inside it,
    /// or in the password, is refused in words (review part 4, #20).
    ///
    /// `expectedIpatool` is the ipatool the sheet said the password goes to
    /// (`AppStoreStore.signInIpatool`), and has no default: the CLI signs in
    /// only through that one (`--expect-ipatool`).
    @discardableResult
    func startSignIn(email: String, password: inout [UInt8], expectedIpatool: String) -> Bool {
        defer { Secret.wipe(&password) }
        let appleID = email.trimmingCharacters(in: .whitespacesAndNewlines)
        guard signInTask == nil, !appleID.isEmpty, !password.isEmpty else { return false }
        guard ChildInput.isOneLine(Array(appleID.utf8)), ChildInput.isOneLine(password) else {
            signIn = .failed(Self.lineBreakRefusal)
            return false
        }
        if let reason = blockedBy() {
            signIn = .failed(reason)
            return false
        }
        let input = ChildInput()
        var appleId = Array(appleID.utf8)
        guard input.send(&appleId), input.send(&password) else {
            input.close()
            return false
        }
        self.input = input
        signIn = .working(Self.startingLine)
        signInTask = Task { [weak self] in await self?.runSignIn(input: input, expectedIpatool: expectedIpatool) }
        return true
    }

    /// Send the 2FA code, only when the CLI has asked for it. Wiped either way.
    @discardableResult
    func submitCode(_ code: inout [UInt8]) -> Bool {
        defer { Secret.wipe(&code) }
        guard signIn == .needsCode, let input else { return false }
        signIn = .working(Self.checkingLine)
        return input.send(&code)
    }

    /// Stop the sign-in: end of input, then the runner's SIGTERM.
    func cancelSignIn() {
        input?.close()
        signInTask?.cancel()
    }

    /// Back to idle when the sheet opens again, unless a sign-in is running.
    func resetSignIn() {
        if signInTask == nil { signIn = .idle }
    }

    private func runSignIn(input: ChildInput, expectedIpatool: String) async {
        var outcome: SignIn
        do {
            let events = try await runner.streamEvents(
                Self.signInArgs(expectedIpatool: expectedIpatool), failing: "Sign in",
                refusal: .signInFailed, input: input
            ) { [weak self] event in
                self?.note(event)
            }
            outcome = Self.outcome(of: events)
        } catch let failed as SignInFailed {
            outcome = .failed(failed.message, hint: failed.hint)
        } catch {
            outcome = .failed(error.localizedDescription)
        }
        input.close()
        self.input = nil
        let cancelled = Task.isCancelled
        signInTask = nil
        // Status is read again whatever happened (review part 4, #16): the
        // CLI clears Graft 1.x's items before ipatool runs, so a sign-in that
        // failed or was canceled may have changed the account all the same.
        // After "ipatool-changed" it is also what shows the sheet the ipatool
        // the next try would reach (fix-check #10).
        guard !cancelled else {
            signIn = .idle
            // A Task of its own: this one is canceled, and a status read in
            // it would be stopped and record nothing.
            Task { [weak self] in await self?.onChange() }
            return
        }
        signIn = outcome
        if case .signedIn = outcome { onAccountChange() }
        await onChange()
    }

    /// How the run ended, from its last word: signed in, or the CLI's own
    /// sentence for a failure, whatever its `reason` (one this app has never
    /// heard of included).
    static func outcome(of events: [BuildEvent]) -> SignIn {
        for case let .appStoreLogin(record) in events.reversed() {
            if record.state == CLIContract.AppStoreLogin.signedIn { return .signedIn(email: record.email ?? "") }
            if record.state == CLIContract.AppStoreLogin.failed, let message = record.message {
                return .failed(message, hint: record.hint)
            }
        }
        return .failed("graft didn't say whether it signed in")
    }

    /// One streamed step, applied only while a sign-in runs: a late hop to
    /// the main actor must not move a finished sheet back to "working".
    func note(_ event: BuildEvent) {
        guard signInTask != nil, case let .appStoreLogin(record) = event else { return }
        switch record.state {
        case CLIContract.AppStoreLogin.needsCode:
            signIn = .needsCode
        case CLIContract.AppStoreLogin.preparing:
            signIn = .working(Self.preparingLine)
        case CLIContract.AppStoreLogin.waiting where signIn != .needsCode:
            signIn = .working(Self.waitingLine(record.elapsed ?? 0))
        case CLIContract.AppStoreLogin.verifying:
            signIn = .working(Self.checkingLine)
        default:
            break
        }
    }

    func signOut() async {
        guard !isSigningOut else { return }
        if let reason = blockedBy() {
            signOutError = reason
            return
        }
        isSigningOut = true
        defer { isSigningOut = false }
        do {
            _ = try await runner.runEvents(Self.signOutArgs(), failing: "Sign out")
            signOutError = nil
            signIn = .idle
            onAccountChange()
            await onChange()
        } catch {
            signOutError = error.localizedDescription
        }
    }

    // MARK: Enrollment

    let shell: PrivilegedShell
    private(set) var candidates: [EnrollCandidateRecord] = []
    /// Whether a candidate load has *finished*: an empty list is an answer
    /// ("install one first") only once it has (memory: an empty collection is
    /// not a load state).
    private(set) var hasLoadedCandidates = false
    private(set) var candidatesError: String?
    /// Which way the last administrator run went (decision 6's record):
    /// in-process NSAppleScript, or the osascript fallback.
    private(set) var lastAdminVia: AdminRun.Via?

    enum Enrol: Equatable {
        case idle
        /// Asking the CLI for the script.
        case preparing
        /// The CLI wouldn't hand over a script whose UnfairPlay nothing would
        /// check before root runs it. Nothing has run and nothing has asked
        /// for a password; the sheet shows the CLI's warning and, where
        /// `mayRunUnchecked` allows, the explicit "Run Unchecked Copy as Root…".
        case unchecked(UncheckedHelper)
        /// The script is in hand and the explanation is showing: nothing has
        /// asked for a password yet (decision 7: explained first).
        case ready(EnrollCommandRecord)
        case running
        case done
        case cancelled
        /// Stopped by its cancel file (exit 67): root removed its folder and
        /// nothing was changed.
        case stopped
        /// Ran past the script's time limit and stopped itself (exit 68),
        /// with nothing changed. The limit, in seconds, when the CLI said it.
        case timedOut(limitSeconds: Int?)
        /// Root found none of this Mac's FairPlay state for UnfairPlay to
        /// read and stopped before it copied anything (exit 69), with
        /// nothing changed. `appName` is the app picked (`appName(for:)`),
        /// which, opened once, writes that state again.
        case noFairPlayState(appName: String)
        case failed(String)
    }

    private(set) var enrol: Enrol = .idle
    /// The user took the explicit step for an unchecked UnfairPlay: the sheet
    /// showed the CLI's warning (`.unchecked`) and they pressed "Run Unchecked
    /// Copy as Root…". Set only by `prepareEnrol(…, allowUncheckedHelper:
    /// true)` from that state; any other prepare, and `resetEnrol`, clear it.
    /// `confirmEnrol` won't run a `verified: false` script without it.
    private(set) var uncheckedConsent = false
    /// The command whose root run is going now: its cancel file and its time
    /// limit. nil outside `.running`.
    private(set) var runningCommand: EnrollCommandRecord?
    /// Cancel was pressed during the root run, and the cancel file exists.
    private(set) var isStoppingEnrol = false
    /// Why a Cancel during the root run couldn't ask it to stop.
    private(set) var stopError: String?
    /// Opens an app in macOS: the sheet's "Open <App>" (`openEnrolApp`).
    let openApp: @MainActor (URL) -> Void

    /// The name of the app `command` enrolls with, as the sheet lists it:
    /// the candidate it was picked from, else its folder's own name.
    func appName(for command: EnrollCommandRecord) -> String {
        candidates.first { $0.path == command.app }?.name
            ?? URL(fileURLWithPath: command.app).deletingPathExtension().lastPathComponent
    }

    /// Before the password: open the app the script in hand reads, so macOS
    /// writes this Mac's App Store state again if it cleared it (a restart
    /// did, in the owner's run). Only in `.ready`; it changes nothing here.
    func openEnrolApp() {
        guard case let .ready(command) = enrol else { return }
        openApp(URL(fileURLWithPath: command.app))
    }

    static func candidatesArgs() -> [String] { [Flag.json, Flag.appstoreEnrollCandidates] }
    static func enrollCommandArgs(app: String, allowUncheckedHelper: Bool = false) -> [String] {
        [Flag.json, Flag.appstoreEnrollCommand, Flag.app, app]
            + (allowUncheckedHelper ? [Flag.allowUnverifiedHelper] : [])
    }
    static func enrollFinishArgs(folder: String) -> [String] { [Flag.json, Flag.appstoreEnrollFinish, folder] }

    var isEnrolling: Bool { enrol == .preparing || enrol == .running }

    func resetEnrol() {
        if !isEnrolling {
            enrol = .idle
            uncheckedConsent = false
        }
    }

    /// Whether this build of Graft may run an UnfairPlay nothing checks, as
    /// root, once the user has asked to.
    ///
    /// - "ad-hoc": this Graft.app has no team signature, so there is nothing
    ///   to check its own UnfairPlay against. Allowed, after the warning.
    /// - "not-bundled": the CLI isn't Graft.app's own, so the UnfairPlay is
    ///   someone's own build. A Debug build (the repo's CLI) may; a Release
    ///   build never does: that is the review's config.yaml route to root.
    /// - Anything else: never. An unknown kind is not a reason to run.
    nonisolated static func mayRunUnchecked(kind: String?, release: Bool = isRelease) -> Bool {
        switch kind {
        case CLIContract.UnverifiedHelperKind.adHoc?: return true
        case CLIContract.UnverifiedHelperKind.notBundled?: return !release
        default: return false
        }
    }

    /// Whether `command` may go to the administrator prompt: it checks its
    /// UnfairPlay, or the user took the explicit step for one of a kind this
    /// build allows. The one rule `prepareEnrol` and `confirmEnrol` both apply.
    nonisolated static func mayRun(_ command: EnrollCommandRecord, consented: Bool, release: Bool = isRelease) -> Bool {
        command.verified || (consented && mayRunUnchecked(kind: command.unverifiedKind, release: release))
    }

    nonisolated static let isRelease: Bool = {
        #if DEBUG
        return false
        #else
        return true
        #endif
    }()

    /// A canceled load (the sheet closed) records nothing, for `loadStatus`'s reason.
    func loadCandidates() async {
        let found: [EnrollCandidateRecord]
        let failure: String?
        do {
            found = try await runner.runCollecting(Self.candidatesArgs(), failing: "List apps that can enroll") { event in
                if case let .appStoreEnrollCandidate(record) = event { return record }
                return nil
            }
            failure = nil
        } catch {
            found = []
            failure = error.localizedDescription
        }
        guard !Task.isCancelled else { return }
        candidates = found
        candidatesError = failure
        hasLoadedCandidates = true
    }

    /// Ask the CLI for the enrollment script for `appPath`, which checks the
    /// app first. Nothing runs as root here: the sheet shows what will, and
    /// `confirmEnrol` runs it.
    ///
    /// `allowUncheckedHelper` is the sheet's "Run Unchecked Copy as Root…",
    /// and is honoured only from `.unchecked`, the state that showed the
    /// CLI's warning, and only for a kind `mayRunUnchecked` allows.
    func prepareEnrol(appPath: String, allowUncheckedHelper: Bool = false) async {
        guard !isEnrolling, blockedBy() == nil else { return }
        if allowUncheckedHelper {
            guard case let .unchecked(refusal) = enrol, Self.mayRunUnchecked(kind: refusal.kind) else { return }
        }
        uncheckedConsent = false
        enrol = .preparing
        do {
            let events = try await runner.streamEvents(
                Self.enrollCommandArgs(app: appPath, allowUncheckedHelper: allowUncheckedHelper),
                failing: "Prepare enrollment", refusal: .uncheckedHelper
            )
            let command = events.lazy.compactMap { event -> EnrollCommandRecord? in
                if case let .appStoreEnrollCommand(record) = event { return record }
                return nil
            }.first
            guard let command else {
                enrol = .failed("graft didn't return the enrollment script")
                return
            }
            // A script nothing checks is shown as the warning, never as an
            // ordinary script, until the user has taken the explicit step.
            guard Self.mayRun(command, consented: allowUncheckedHelper) else {
                enrol = .unchecked(UncheckedHelper(command))
                return
            }
            uncheckedConsent = !command.verified
            enrol = .ready(command)
        } catch let refusal as UncheckedHelper {
            enrol = .unchecked(refusal)
        } catch {
            enrol = .failed(error.localizedDescription)
        }
    }

    /// Run the script the sheet showed behind the administrator prompt, then
    /// move the model in as the user (`--appstore-enroll-finish` with the
    /// folder the script printed), then read status again: `--appstore-status`
    /// is what confirms the model exists, belongs to the user and is readable.
    ///
    /// A `verified: false` script runs only with `uncheckedConsent`, and only
    /// for a kind this build allows: otherwise it goes back to the warning.
    func confirmEnrol() async {
        guard case let .ready(command) = enrol, blockedBy() == nil else { return }
        guard Self.mayRun(command, consented: uncheckedConsent) else {
            enrol = .unchecked(UncheckedHelper(command))
            return
        }
        enrol = .running
        runningCommand = command
        isStoppingEnrol = false
        stopError = nil
        // Quit Anyway stops the root run as Cancel does, with the cancel file.
        let ticket = hold.begin(TerminationHold.enrolling) { [weak self] in self?.stopEnrol() }
        defer { hold.end(ticket) }
        do {
            let run: AdminRun
            do {
                run = try await shell.run(command.script, prompt: AdminShell.prompt)
            } catch {
                endRootRun(command)
                throw error
            }
            endRootRun(command)
            lastAdminVia = run.via
            guard let folder = AdminShell.lastLine(run.output) else {
                enrol = .failed("The setup ran but didn't say where it left the model.")
                await onChange()
                return
            }
            _ = try await runner.run(Self.enrollFinishArgs(folder: folder), failing: "Finish enrollment") { event -> EnrolledRecord? in
                if case let .appStoreEnrolled(record) = event { return record }
                return nil
            }
            await onChange()
            enrol = .done
        } catch let error as AdminShellError where error.cancelled {
            enrol = .cancelled
        } catch let error as AdminShellError where error.number == CLIContract.EnrolExit.stopped {
            enrol = .stopped
        } catch let error as AdminShellError where error.number == CLIContract.EnrolExit.timedOut {
            enrol = .timedOut(limitSeconds: command.timeLimitSeconds)
        } catch let error as AdminShellError where error.number == CLIContract.EnrolExit.noFairPlayState {
            enrol = .noFairPlayState(appName: appName(for: command))
        } catch {
            await onChange()
            enrol = .failed(error.localizedDescription)
        }
    }

    /// Cancel while the root run goes, Esc, or the sheet closing: create the
    /// command's cancel file. The script looks for it before it starts, while
    /// macOS's password dialog waits, and while UnfairPlay runs, then stops
    /// with nothing changed (exit 67). The file is removed when the run ends.
    func stopEnrol() {
        guard enrol == .running, let command = runningCommand, !isStoppingEnrol else { return }
        guard let file = command.cancelFile else {
            stopError = "This graft can't stop a setup once it has started."
            return
        }
        if let failure = EnrolStopFile.create(file) {
            stopError = "Graft couldn't ask the setup to stop: \(failure)"
            return
        }
        isStoppingEnrol = true
    }

    /// The root run is over, whichever way: its cancel file goes, so a stop
    /// asked for this run can't stop the next one.
    private func endRootRun(_ command: EnrollCommandRecord) {
        if let file = command.cancelFile { EnrolStopFile.remove(file) }
        runningCommand = nil
        isStoppingEnrol = false
    }
}

/// The enrollment's cancel file (`EnrollCommandRecord.cancelFile`). The CLI
/// names it and never creates it; root's script stops when it appears.
enum EnrolStopFile {
    /// `open(O_CREAT|O_EXCL|O_WRONLY, 0600)`, then close: never follows a
    /// link, never truncates anything. Already there is a stop already
    /// asked for. nil on success, else why not.
    static func create(_ path: String) -> String? {
        let fd = open(path, O_CREAT | O_EXCL | O_WRONLY, 0o600)
        if fd >= 0 {
            close(fd)
            return nil
        }
        if errno == EEXIST { return nil }
        return String(cString: strerror(errno))
    }

    /// `unlink`: the file itself, never what a link points at, and nothing
    /// to do when it isn't there.
    static func remove(_ path: String) {
        _ = unlink(path)
    }
}
