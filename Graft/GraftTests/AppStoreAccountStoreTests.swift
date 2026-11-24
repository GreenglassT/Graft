import CryptoKit
import XCTest
@testable import Graft

/// `AppStoreAccountStore`'s sign-in, against `/bin/sh` stand-ins for the CLI
/// that hash what they were sent on stdin, and record their argv and
/// environment. The B.2 checks on this side: the Apple ID, the password and
/// the code go over stdin and nowhere else, the secrets are wiped once sent,
/// and nothing of them lands in UserDefaults.
final class AppStoreAccountStoreTests: XCTestCase {
    static let email = "someone@example.invalid"
    static let password = "Store-Test-Passw0rd"
    static let code = "482913"
    /// The ipatool a sheet named, for the tests that call the account store
    /// directly.
    static let ipatoolPath = "/i"

    private func cli(_ body: (URL) -> String) throws -> (CLIRunner, URL) {
        let dir = FileManager.default.temporaryDirectory
            .appending(path: "account_store_\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let file = dir.appending(path: "graft.sh")
        try """
        #!/bin/sh
        printf '%s\\n' "$*" >> '\(dir.path)/argv.txt'
        /usr/bin/env > '\(dir.path)/env.txt'
        \(body(dir))
        """.write(to: file, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: file.path)
        return (CLIRunner(executable: file), dir)
    }

    private static func hash(_ variable: String, into dir: URL, as name: String) -> String {
        "printf '%s' \"$\(variable)\" | /usr/bin/shasum -a 256 | /usr/bin/cut -d' ' -f1 > '\(dir.path)/\(name)'"
    }

    /// The CLI's side of a sign-in where Apple asks for a code.
    private static func twoFactor(_ dir: URL) -> String { """
        IFS= read -r id
        \(hash("id", into: dir, as: "id.sha"))
        IFS= read -r pw
        \(hash("pw", into: dir, as: "pw.sha"))
        printf '%s\\n' '{"type":"appstore-login","state":"preparing"}'
        printf '%s\\n' '{"type":"appstore-login","state":"needs-code"}'
        IFS= read -r code
        \(hash("code", into: dir, as: "code.sha"))
        printf '%s\\n' '{"type":"appstore-login","state":"verifying"}'
        printf '%s\\n' '{"type":"appstore-login","state":"signed-in","email":"\(email)"}'
        """ }

    private func read(_ dir: URL, _ name: String) throws -> String {
        try String(contentsOf: dir.appending(path: name), encoding: .utf8)
    }

    private func sha(_ dir: URL, _ name: String) throws -> String {
        try read(dir, name).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    @MainActor
    private func signIn(_ store: AppStoreAccountStore) -> Bool {
        var bytes = Array(Self.password.utf8)
        let started = store.startSignIn(email: Self.email, password: &bytes, expectedIpatool: Self.ipatoolPath)
        XCTAssertTrue(bytes.isEmpty, "the password bytes are wiped as soon as they are handed over")
        return started
    }

    @MainActor
    func test_apple_id_and_password_first_code_after_needs_code_none_on_argv_or_in_env() async throws {
        let (runner, dir) = try cli(Self.twoFactor)
        let store = AppStoreAccountStore(runner: runner, shell: RecordingShell())
        var reloads = 0
        store.onChange = { reloads += 1 }

        XCTAssertTrue(signIn(store))
        await eventually { store.signIn == .needsCode }
        var code = Array(Self.code.utf8)
        XCTAssertTrue(store.submitCode(&code))
        XCTAssertTrue(code.isEmpty)
        await eventually { store.signIn == .signedIn(email: Self.email) }

        XCTAssertEqual(reloads, 1, "status is read again after a sign-in")
        XCTAssertEqual(try read(dir, "argv.txt"), "--json --appstore-login --expect-ipatool /i\n",
                       "nothing of the account on argv, and always the ipatool the sheet named")
        XCTAssertEqual(try sha(dir, "id.sha"), ChildInputTests.sha256(Self.email))
        XCTAssertEqual(try sha(dir, "pw.sha"), ChildInputTests.sha256(Self.password))
        XCTAssertEqual(try sha(dir, "code.sha"), ChildInputTests.sha256(Self.code))
        let env = try read(dir, "env.txt")
        XCTAssertFalse(env.contains(Self.email))
        XCTAssertFalse(env.contains(Self.password))
        XCTAssertFalse(env.contains(Self.code))
    }

    /// Review part 4, #20: an Apple ID pasted with a trailing newline goes as
    /// the Apple ID alone; a password with a line break in it isn't sent.
    @MainActor
    func test_the_apple_id_is_trimmed_and_a_broken_password_is_refused_in_words() async throws {
        let (runner, dir) = try cli(Self.twoFactor)
        let store = AppStoreAccountStore(runner: runner, shell: RecordingShell())
        var broken = Array("Store-Test\nPassw0rd".utf8)
        XCTAssertFalse(store.startSignIn(email: Self.email, password: &broken, expectedIpatool: Self.ipatoolPath))
        XCTAssertTrue(broken.isEmpty)
        XCTAssertEqual(store.signIn, .failed(AppStoreAccountStore.lineBreakRefusal))
        XCTAssertFalse(FileManager.default.fileExists(atPath: dir.appending(path: "argv.txt").path), "nothing started")

        var password = Array(Self.password.utf8)
        XCTAssertTrue(store.startSignIn(email: " \(Self.email)\r\n", password: &password, expectedIpatool: Self.ipatoolPath))
        await eventually { store.signIn == .needsCode }
        XCTAssertEqual(try sha(dir, "id.sha"), ChildInputTests.sha256(Self.email))
        XCTAssertEqual(try sha(dir, "pw.sha"), ChildInputTests.sha256(Self.password), "the password is still the second line")
        store.cancelSignIn()
        await eventually { !store.isSigningIn }
    }

    @MainActor
    func test_a_sign_in_apple_answers_without_a_code_needs_none() async throws {
        let (runner, _) = try cli { _ in """
            IFS= read -r id; IFS= read -r pw
            printf '%s\\n' '{"type":"appstore-login","state":"preparing"}'
            printf '%s\\n' '{"type":"appstore-login","state":"signed-in","email":"\(Self.email)"}'
            """ }
        let store = AppStoreAccountStore(runner: runner, shell: RecordingShell())
        XCTAssertTrue(signIn(store))
        await eventually { !store.isSigningIn }
        XCTAssertEqual(store.signIn, .signedIn(email: Self.email))
    }

    @MainActor
    func test_a_code_before_ipatool_asks_is_not_sent() async throws {
        let (runner, _) = try cli { _ in "IFS= read -r id; IFS= read -r pw; exec /bin/sleep 30" }
        let store = AppStoreAccountStore(runner: runner, shell: RecordingShell())
        XCTAssertTrue(signIn(store))
        var code = Array(Self.code.utf8)
        XCTAssertFalse(store.submitCode(&code))
        XCTAssertTrue(code.isEmpty, "wiped even when refused")
        store.cancelSignIn()
        await eventually { !store.isSigningIn }
    }

    @MainActor
    func test_a_second_sign_in_while_one_runs_is_refused_and_still_wiped() async throws {
        let (runner, _) = try cli { _ in "IFS= read -r id; IFS= read -r pw; exec /bin/sleep 30" }
        let store = AppStoreAccountStore(runner: runner, shell: RecordingShell())
        XCTAssertTrue(signIn(store))
        var again = Array(Self.password.utf8)
        XCTAssertFalse(store.startSignIn(email: Self.email, password: &again, expectedIpatool: Self.ipatoolPath))
        XCTAssertTrue(again.isEmpty)
        store.cancelSignIn()
        await eventually { !store.isSigningIn }
    }

    @MainActor
    func test_a_failed_sign_in_shows_the_clis_own_sentence() async throws {
        let (runner, _) = try cli { _ in """
            IFS= read -r id; IFS= read -r pw
            printf '%s\\n' '{"type":"appstore-login","state":"failed","reason":"not-accepted","message":"Apple didn'"'"'t accept the password or the code: check both and try again."}'
            echo "error: Apple didn't accept the password or the code" >&2
            exit 20
            """ }
        let store = AppStoreAccountStore(runner: runner, shell: RecordingShell())
        XCTAssertTrue(signIn(store))
        await eventually { !store.isSigningIn }
        XCTAssertEqual(store.signIn, .failed("Apple didn't accept the password or the code: check both and try again."))
    }

    /// Whole-diff review, signin-4: a failed event's `hint` comes with the
    /// sentence, so the sheet can show what to do; on a non-zero exit (the
    /// refusal) and on exit 0 (the last event) alike. None sent is none shown.
    @MainActor
    func test_a_failed_sign_in_keeps_the_clis_hint() async throws {
        let message = "macOS refused ipatool the keychain items."
        let hint = "if macOS asked whether ipatool may use your keychain, sign in again and choose Allow"
        for exit in [20, 0] {
            let (runner, _) = try cli { _ in """
                IFS= read -r id; IFS= read -r pw
                printf '%s\\n' '{"type":"appstore-login","state":"failed","reason":"keychain","message":"\(message)","hint":"\(hint)"}'
                exit \(exit)
                """ }
            let store = AppStoreAccountStore(runner: runner, shell: RecordingShell())
            XCTAssertTrue(signIn(store))
            await eventually { !store.isSigningIn }
            XCTAssertEqual(store.signIn, .failed(message, hint: hint), "exit \(exit)")
        }
        let (runner, _) = try cli { _ in """
            IFS= read -r id; IFS= read -r pw
            printf '%s\\n' '{"type":"appstore-login","state":"failed","reason":"wrong-password","message":"\(message)"}'
            exit 20
            """ }
        let store = AppStoreAccountStore(runner: runner, shell: RecordingShell())
        XCTAssertTrue(signIn(store))
        await eventually { !store.isSigningIn }
        XCTAssertEqual(store.signIn, .failed(message, hint: nil))
    }

    /// The sheet shows the hint under the sentence (signin-4).
    @MainActor
    func test_the_sign_in_sheet_shows_the_hint_under_the_message() async throws {
        try AccessibilityTree.requireServed()
        let message = "macOS refused ipatool the keychain items."
        let hint = "if macOS asked whether ipatool may use your keychain, sign in again and choose Allow"
        let (runner, _) = try cli { _ in """
            case "$*" in
              *--appstore-status*)
                printf '%s\\n' '{"type":"appstore-status","ipatool":{"path":"/i","present":true,"version":"2.8.0","signed_in":false,"warning":null,"legacy_signin":false,"source":"bundled"},"unfairplay":{"path":"/u","present":true,"version":"v0.4.0","enrolled":true,"model_dir":"/m"},"setup_commands":[]}' ;;
              *--appstore-login*)
                IFS= read -r id; IFS= read -r pw
                printf '%s\\n' '{"type":"appstore-login","state":"failed","reason":"keychain","message":"\(message)","hint":"\(hint)"}'
                exit 20 ;;
            esac
            """ }
        let store = AppStoreStore(runner: runner, shell: RecordingShell())
        let pane = hosted(AppStoreSignInSheet(store: store), width: 440)
        await eventually { store.signInIpatool != nil }
        settle(pane)
        XCTAssertTrue(signIn(store))
        await eventually { !store.account.isSigningIn }
        settle(pane)
        let texts = try AccessibilityTree.texts(in: pane.window)
        let shownMessage = try XCTUnwrap(texts.firstIndex(of: message), "\(texts)")
        let shownHint = try XCTUnwrap(texts.firstIndex(of: hint), "the hint is shown: \(texts)")
        XCTAssertLessThan(shownMessage, shownHint, "under the message")
    }

    /// The try14 delta audit, F2, on screen: a sign-in the ipatool can't read
    /// gets "Graft removes it first" only when that ipatool is Graft.app's
    /// own; for one config.yaml names, the sheet says what its sign-in does.
    @MainActor
    func test_the_sign_in_sheet_says_what_happens_to_an_unreadable_sign_in_for_its_ipatool() async throws {
        try AccessibilityTree.requireServed()
        for source in [CLIContract.ToolSource.bundled, CLIContract.ToolSource.config] {
            let (runner, _) = try cli { _ in """
                case "$*" in
                  *--appstore-status*)
                    printf '%s\\n' '{"type":"appstore-status","ipatool":{"path":"/u/x/ipatool","present":true,"version":"2.8.0","signed_in":null,"email":null,"warning":null,"legacy_signin":true,"source":"\(source)"},"unfairplay":{"path":"/u","present":true,"version":"v0.4.0","enrolled":true,"model_dir":"/m"},"setup_commands":[]}' ;;
                esac
                """ }
            let store = AppStoreStore(runner: runner, shell: RecordingShell())
            let pane = hosted(AppStoreSignInSheet(store: store), width: 440)
            await eventually({ store.signInIpatool != nil }, source)
            settle(pane)
            let texts = try AccessibilityTree.texts(in: pane.window)
            let other = AppStoreSignInSheet.otherLegacyNote(path: "/u/x/ipatool")
            XCTAssertEqual(texts.contains(AppStoreSignInSheet.legacyNote), source == CLIContract.ToolSource.bundled,
                           "\(source): \(texts)")
            XCTAssertEqual(texts.contains(other), source != CLIContract.ToolSource.bundled, "\(source): \(texts)")
            pane.window.orderOut(nil)
        }
    }

    /// Review part 4, #16: the CLI has already cleared Graft 1.x's items by
    /// the time ipatool fails, so status is read again after a failure too.
    @MainActor
    func test_a_failed_sign_in_reads_status_again() async throws {
        let (runner, _) = try cli { _ in """
            IFS= read -r id; IFS= read -r pw
            printf '%s\\n' '{"type":"appstore-login","state":"failed","reason":"wrong-password","message":"Apple didn'"'"'t accept that password. Check it and sign in again."}'
            exit 20
            """ }
        let store = AppStoreAccountStore(runner: runner, shell: RecordingShell())
        var reloads = 0
        store.onChange = { reloads += 1 }
        XCTAssertTrue(signIn(store))
        await eventually { !store.isSigningIn && reloads == 1 }
        XCTAssertEqual(reloads, 1)
    }

    @MainActor
    func test_a_cancelled_sign_in_reads_status_again_outside_the_cancelled_task() async throws {
        let (runner, _) = try cli { _ in "IFS= read -r id; IFS= read -r pw; exec /bin/sleep 30" }
        let store = AppStoreAccountStore(runner: runner, shell: RecordingShell())
        var reloads = 0
        var reloadWasCancelled: Bool?
        store.onChange = {
            reloads += 1
            reloadWasCancelled = Task.isCancelled
        }
        XCTAssertTrue(signIn(store))
        store.cancelSignIn()
        await eventually { !store.isSigningIn && reloads == 1 }
        XCTAssertEqual(store.signIn, .idle)
        XCTAssertEqual(reloadWasCancelled, false, "a status read in the canceled task would record nothing")
    }

    /// New failure reasons (`ipatool-exited`, one for a sign-in Graft
    /// couldn't record, `ipatool-changed` and `unknown-prompt` since the
    /// fix-check) show the CLI's own sentence, never a generic one.
    @MainActor
    func test_a_failure_reason_this_app_has_never_heard_of_shows_the_clis_sentence() async throws {
        for (reason, exit) in [("ipatool-exited", 20), ("not-recorded", 20), ("not-recorded", 0),
                               ("ipatool-changed", 20), ("unknown-prompt", 20), ("keychain-locked", 20),
                               ("something-new", 20)] {
            let (runner, _) = try cli { _ in """
                IFS= read -r id; IFS= read -r pw
                printf '%s\\n' '{"type":"appstore-login","state":"failed","reason":"\(reason)","message":"ipatool stopped before Apple answered (\(reason))."}'
                exit \(exit)
                """ }
            let store = AppStoreAccountStore(runner: runner, shell: RecordingShell())
            XCTAssertTrue(signIn(store))
            await eventually { !store.isSigningIn }
            XCTAssertEqual(store.signIn, .failed("ipatool stopped before Apple answered (\(reason))."), "\(reason), exit \(exit)")
        }
    }

    @MainActor
    func test_a_failure_without_a_message_shows_the_clis_stderr_not_a_generic_line() async throws {
        let (runner, _) = try cli { _ in """
            IFS= read -r id; IFS= read -r pw
            printf '%s\\n' '{"type":"appstore-login","state":"failed","reason":"something-new"}'
            echo "error: ipatool exited 3 while signing in" >&2
            exit 20
            """ }
        let store = AppStoreAccountStore(runner: runner, shell: RecordingShell())
        XCTAssertTrue(signIn(store))
        await eventually { !store.isSigningIn }
        guard case let .failed(message, _) = store.signIn else { return XCTFail("expected .failed, got \(store.signIn)") }
        XCTAssertTrue(message.contains("ipatool exited 3 while signing in"), message)
        XCTAssertNotEqual(message, "Sign-in failed.")
    }

    @MainActor
    func test_a_silent_first_sign_in_says_what_it_is_waiting_for() async throws {
        let (runner, _) = try cli { _ in """
            IFS= read -r id; IFS= read -r pw
            printf '%s\\n' '{"type":"appstore-login","state":"preparing"}'
            printf '%s\\n' '{"type":"appstore-login","state":"waiting","elapsed":40}'
            exec /bin/sleep 30
            """ }
        let store = AppStoreAccountStore(runner: runner, shell: RecordingShell())
        XCTAssertTrue(signIn(store))
        await eventually { store.signIn == .working(AppStoreAccountStore.waitingLine(40)) }
        XCTAssertTrue(AppStoreAccountStore.waitingLine(40).contains("320 MB"))
        XCTAssertTrue(AppStoreAccountStore.preparingLine.contains("Unicorn"))
        store.cancelSignIn()
        await eventually { !store.isSigningIn }
    }

    @MainActor
    func test_cancelling_at_the_code_stops_the_cli_and_settles_idle() async throws {
        let (runner, _) = try cli { _ in """
            IFS= read -r id; IFS= read -r pw
            printf '%s\\n' '{"type":"appstore-login","state":"needs-code"}'
            exec /bin/sleep 30
            """ }
        let store = AppStoreAccountStore(runner: runner, shell: RecordingShell())
        XCTAssertTrue(signIn(store))
        await eventually { store.signIn == .needsCode }
        store.cancelSignIn()
        await eventually { !store.isSigningIn }
        XCTAssertEqual(store.signIn, .idle)
    }

    @MainActor
    func test_sign_out_runs_the_verb_and_rereads_status() async throws {
        let (runner, dir) = try cli { _ in """
            printf '%s\\n' '{"type":"appstore-logout","signed_out":true}'
            """ }
        let store = AppStoreAccountStore(runner: runner, shell: RecordingShell())
        var reloads = 0
        store.onChange = { reloads += 1 }
        await store.signOut()
        XCTAssertEqual(try read(dir, "argv.txt"), "--json --appstore-logout\n")
        XCTAssertEqual(reloads, 1)
        XCTAssertNil(store.signOutError)
    }

    @MainActor
    func test_nothing_of_a_sign_in_is_left_in_user_defaults() async throws {
        let (runner, _) = try cli(Self.twoFactor)
        let store = AppStoreAccountStore(runner: runner, shell: RecordingShell())
        XCTAssertTrue(signIn(store))
        await eventually { store.signIn == .needsCode }
        var code = Array(Self.code.utf8)
        store.submitCode(&code)
        await eventually { !store.isSigningIn }
        let defaults = "\(UserDefaults.standard.dictionaryRepresentation())"
        XCTAssertFalse(defaults.contains(Self.password))
        XCTAssertFalse(defaults.contains(Self.code))
    }

    @MainActor
    func test_the_app_store_store_owns_one_that_rereads_status() async throws {
        let (runner, dir) = try cli { _ in """
            case "$*" in
              *--appstore-logout*) printf '%s\\n' '{"type":"appstore-logout","signed_out":true}' ;;
            esac
            """ }
        let store = AppStoreStore(runner: runner, shell: RecordingShell())
        await store.account.signOut()
        XCTAssertTrue(try read(dir, "argv.txt").contains("--json --appstore-status"))
    }

    // MARK: Which ipatool signs in, read as the sheet opens (fix-check #10)

    /// A stand-in whose status names the ipatool in `ipa` ("<path> <source>"),
    /// and whose sign-in refuses with `ipatool-changed`, reading nothing,
    /// unless `--expect-ipatool` names that same path.
    private static func resolving(_ dir: URL) -> String { """
        read -r path source < '\(dir.path)/ipa'
        case "$*" in
          *--appstore-status*)
            printf '{"type":"appstore-status","ipatool":{"path":"%s","source":"%s","present":true,"version":"2.8.0","signed_in":false,"email":null,"legacy_signin":false,"warning":null},"unfairplay":{"path":"/u","present":true,"version":"v0.4.0","enrolled":true,"model_dir":"/m"},"setup_commands":[]}\\n' "$path" "$source" ;;
          *"--appstore-login --expect-ipatool $path")
            IFS= read -r id; IFS= read -r pw
            printf '{"type":"appstore-login","state":"signed-in","email":"%s"}\\n' "$id" ;;
          *--appstore-login*)
            printf '{"type":"appstore-login","state":"failed","reason":"ipatool-changed","message":"The sign-in was for another ipatool; ipatool is now %s."}\\n' "$path"
            exit 20 ;;
        esac
        """ }

    private static func ipa(_ path: String, _ source: String, in dir: URL) throws {
        try "\(path) \(source)\n".write(to: dir.appending(path: "ipa"), atomically: true, encoding: .utf8)
    }

    /// The fix-check's scenario: status was read once, then config.yaml
    /// named another ipatool. Opening the sheet reads status again, so the
    /// note names the one `--appstore-login` will run, and the password goes
    /// only with `--expect-ipatool` set to it. Before that read, Sign In
    /// sends nothing.
    @MainActor
    func test_the_sheet_reads_status_again_as_it_opens_and_signs_in_only_through_that_ipatool() async throws {
        let (runner, dir) = try cli(Self.resolving)
        try Self.ipa("/b/ipatool", CLIContract.ToolSource.bundled, in: dir)
        let store = AppStoreStore(runner: runner, shell: RecordingShell())
        await store.loadStatus()
        XCTAssertEqual(store.status?.ipatool.path, "/b/ipatool")
        var early = Array(Self.password.utf8)
        XCTAssertFalse(store.startSignIn(email: Self.email, password: &early), "not before the sheet's own read")
        XCTAssertTrue(early.isEmpty, "wiped even so")
        XCTAssertNil(store.signInIpatool)

        try Self.ipa("/u/x/ipatool", CLIContract.ToolSource.config, in: dir)
        await store.checkBeforeSignIn()
        let named = try XCTUnwrap(store.signInIpatool)
        XCTAssertEqual(named.path, "/u/x/ipatool", "the ipatool now, not the one read earlier")
        XCTAssertNotNil(AppStoreSignInSheet.elsewhereNote(for: named), "so the note says where the password goes")
        XCTAssertEqual(try read(dir, "argv.txt").components(separatedBy: "--appstore-status").count - 1, 2,
                       "the sheet's own read, after the one before")

        XCTAssertTrue(signIn(store))
        await eventually { !store.account.isSigningIn }
        XCTAssertEqual(store.account.signIn, .signedIn(email: Self.email))
        let argv = try read(dir, "argv.txt").split(separator: "\n").map(String.init)
        XCTAssertEqual(argv.filter { $0.contains("--appstore-login") }, ["--json --appstore-login --expect-ipatool /u/x/ipatool"])
    }

    /// ipatool changes between the sheet's read and Sign In: the CLI refuses
    /// ("ipatool-changed"), the sheet shows its sentence, and status, read
    /// again, names the new ipatool for the next try.
    @MainActor
    func test_an_ipatool_changed_refusal_shows_the_clis_sentence_and_the_status_read_again() async throws {
        let (runner, dir) = try cli(Self.resolving)
        try Self.ipa("/b/ipatool", CLIContract.ToolSource.bundled, in: dir)
        let store = AppStoreStore(runner: runner, shell: RecordingShell())
        await store.checkBeforeSignIn()
        let before = try XCTUnwrap(store.signInIpatool)
        XCTAssertNil(AppStoreSignInSheet.elsewhereNote(for: before), "premise: Graft's own")

        try Self.ipa("/u/x/ipatool", CLIContract.ToolSource.config, in: dir)
        XCTAssertTrue(signIn(store))
        await eventually { !store.account.isSigningIn && store.signInIpatool?.path == "/u/x/ipatool" }
        XCTAssertEqual(store.account.signIn, .failed("The sign-in was for another ipatool; ipatool is now /u/x/ipatool."))
        let after = try XCTUnwrap(store.signInIpatool)
        XCTAssertNotNil(AppStoreSignInSheet.elsewhereNote(for: after), "the sheet now names it")
        XCTAssertTrue(try read(dir, "argv.txt").contains("--json --appstore-login --expect-ipatool /b/ipatool\n"))
    }

    @MainActor
    private func signIn(_ store: AppStoreStore) -> Bool {
        var bytes = Array(Self.password.utf8)
        let started = store.startSignIn(email: Self.email, password: &bytes)
        XCTAssertTrue(bytes.isEmpty)
        return started
    }

    // MARK: Enrollment

    final class RecordingShell: PrivilegedShell, @unchecked Sendable {
        private(set) var scripts: [String] = []
        var error: AdminShellError?
        var output = "{\"success\":true}\n/private/var/tmp/graft-enroll.AbCd1234\n"

        func run(_ script: String, prompt: String) async throws -> AdminRun {
            scripts.append(script)
            if let error { throw error }
            return AdminRun(output: output, via: .plain)
        }
    }

    private static let script = "set -eu\nD=$(/usr/bin/mktemp -d '/private/var/tmp/graft-enroll.XXXXXXXX')\necho \"$D\"\n"
    private static let commandLine = #"{"type":"appstore-enroll-command","app":"/Applications/Example.app","model_dir":"/u/m","script":"set -eu\nD=$(/usr/bin/mktemp -d '/private/var/tmp/graft-enroll.XXXXXXXX')\necho \"$D\"\n","verified":true}"#

    /// The CLI's two enrollment verbs. The command event is read from a file:
    /// the script in it has quotes of its own.
    private static func enrolling(_ dir: URL) -> String {
        try? (commandLine + "\n").write(to: dir.appending(path: "command.json"), atomically: true, encoding: .utf8)
        return """
        case "$*" in
          *--appstore-enroll-command*) /bin/cat '\(dir.path)/command.json' ;;
          *--appstore-enroll-finish*) printf '%s\\n' '{"type":"appstore-enrolled","model_dir":"/u/m","count":1}' ;;
        esac
        """
    }

    @MainActor
    func test_the_script_runs_only_after_the_explanation_then_finish_then_status() async throws {
        let (runner, dir) = try cli(Self.enrolling)
        let shell = RecordingShell()
        let store = AppStoreAccountStore(runner: runner, shell: shell)
        var reloads = 0
        store.onChange = { reloads += 1 }

        await store.prepareEnrol(appPath: "/Applications/Example.app")
        guard case let .ready(command) = store.enrol else { return XCTFail("expected .ready, got \(store.enrol)") }
        XCTAssertEqual(command.script, Self.script)
        XCTAssertTrue(shell.scripts.isEmpty, "decision 7: nothing asks for a password before the sheet explains")

        await store.confirmEnrol()

        XCTAssertEqual(try read(dir, "argv.txt"), """
            --json --appstore-enroll-command --app /Applications/Example.app
            --json --appstore-enroll-finish /private/var/tmp/graft-enroll.AbCd1234

            """)
        XCTAssertEqual(shell.scripts, [Self.script], "the CLI's script, byte for byte")
        XCTAssertEqual(store.enrol, .done)
        XCTAssertEqual(store.lastAdminVia, .plain, "which way the admin run went is recorded")
        XCTAssertEqual(reloads, 1)
    }

    @MainActor
    func test_cancelling_the_prompt_is_cancelled_not_a_failure_and_nothing_is_moved() async throws {
        let (runner, dir) = try cli(Self.enrolling)
        let shell = RecordingShell()
        shell.error = AdminShellError(number: AdminShellError.userCancelled, message: "User canceled.")
        let store = AppStoreAccountStore(runner: runner, shell: shell)
        await store.prepareEnrol(appPath: "/Applications/Example.app")
        await store.confirmEnrol()
        XCTAssertEqual(store.enrol, .cancelled)
        XCTAssertFalse(try read(dir, "argv.txt").contains("--appstore-enroll-finish"))
    }

    @MainActor
    func test_a_refused_app_never_reaches_the_prompt() async throws {
        let (runner, _) = try cli { _ in "echo 'error: Example.app is not an iPhone or iPad app' >&2; exit 20" }
        let shell = RecordingShell()
        let store = AppStoreAccountStore(runner: runner, shell: shell)
        await store.prepareEnrol(appPath: "/Applications/Example.app")
        await store.confirmEnrol()
        XCTAssertTrue(shell.scripts.isEmpty)
        guard case .failed = store.enrol else { return XCTFail("expected .failed, got \(store.enrol)") }
    }

    @MainActor
    func test_a_script_that_names_no_folder_is_a_failure_and_finish_never_runs() async throws {
        let (runner, dir) = try cli(Self.enrolling)
        let shell = RecordingShell()
        shell.output = "\n"
        let store = AppStoreAccountStore(runner: runner, shell: shell)
        await store.prepareEnrol(appPath: "/Applications/Example.app")
        await store.confirmEnrol()
        guard case .failed = store.enrol else { return XCTFail("expected .failed, got \(store.enrol)") }
        XCTAssertFalse(try read(dir, "argv.txt").contains("--appstore-enroll-finish"))
    }

    @MainActor
    func test_confirm_without_a_prepared_script_does_nothing() async throws {
        let (runner, _) = try cli(Self.enrolling)
        let shell = RecordingShell()
        let store = AppStoreAccountStore(runner: runner, shell: shell)
        await store.confirmEnrol()
        XCTAssertTrue(shell.scripts.isEmpty)
        XCTAssertEqual(store.enrol, .idle)
    }

    // MARK: Stopping the root run, and its time limit (the review's critic)

    /// A root run as the sheet sees one: it waits, as macOS's dialog and then
    /// UnfairPlay would, until the cancel file appears, notes the file's mode,
    /// then ends the way `afterStop` says (by default the script's 67).
    /// Nothing privileged runs.
    final class StoppableShell: PrivilegedShell, @unchecked Sendable {
        let file: URL
        var afterStop: AdminShellError? = AdminShellError(number: CLIContract.EnrolExit.stopped, message: "graft: stopped")
        private let lock = NSLock()
        private var mode: Int?

        init(file: URL) { self.file = file }

        var sawMode: Int? { lock.withLock { mode } }

        func run(_ script: String, prompt: String) async throws -> AdminRun {
            let deadline = Date().addingTimeInterval(5)
            while !FileManager.default.fileExists(atPath: file.path), Date() < deadline {
                try await Task.sleep(nanoseconds: 10_000_000)
            }
            let attributes = try? FileManager.default.attributesOfItem(atPath: file.path)
            lock.withLock { mode = (attributes?[.posixPermissions] as? NSNumber)?.intValue }
            if let afterStop { throw afterStop }
            return AdminRun(output: "/private/var/tmp/graft-enroll.AbCd1234\n", via: .plain)
        }
    }

    /// The CLI's verbs, with a command that names `stop` as its cancel file.
    private static func enrolling(cancelFile stop: URL, limit: Int = 600) -> (URL) -> String {
        { dir in
            let line = #"{"type":"appstore-enroll-command","app":"/Applications/Example.app","model_dir":"/u/m","script":"set -eu\necho \"$D\"\n","verified":true,"helper":"/Applications/Graft.app/Contents/Helpers/bin/unfairplay","unverified_kind":null,"unverified_reason":null,"cancel_file":""# + stop.path + #"","time_limit_seconds":"# + "\(limit)}"
            try? (line + "\n").write(to: dir.appending(path: "command.json"), atomically: true, encoding: .utf8)
            return """
            case "$*" in
              *--appstore-enroll-command*) /bin/cat '\(dir.path)/command.json' ;;
              *--appstore-enroll-finish*) printf '%s\\n' '{"type":"appstore-enrolled","model_dir":"/u/m","count":1}' ;;
            esac
            """
        }
    }

    private func stopFile() throws -> URL {
        let dir = FileManager.default.temporaryDirectory.appending(path: "enrol_stop_\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        return dir.appending(path: "stop")
    }

    @MainActor
    func test_cancel_while_root_runs_creates_the_cancel_file_then_says_stopped_and_removes_it() async throws {
        let stop = try stopFile()
        let (runner, dir) = try cli(Self.enrolling(cancelFile: stop))
        let shell = StoppableShell(file: stop)
        let store = AppStoreAccountStore(runner: runner, shell: shell)
        await store.prepareEnrol(appPath: "/Applications/Example.app")
        store.stopEnrol()
        XCTAssertFalse(FileManager.default.fileExists(atPath: stop.path), "no stop before Continue: nothing is running")

        let run = Task { await store.confirmEnrol() }
        await eventually { store.enrol == .running }
        XCTAssertEqual(store.runningCommand?.timeLimitSeconds, 600)
        store.stopEnrol()
        XCTAssertTrue(store.isStoppingEnrol, "the sheet says Stopping…")
        await run.value

        XCTAssertEqual(store.enrol, .stopped)
        XCTAssertEqual(shell.sawMode, 0o600, "O_CREAT|O_EXCL|O_WRONLY, 0600")
        XCTAssertFalse(FileManager.default.fileExists(atPath: stop.path), "removed once the run is over")
        XCTAssertFalse(store.isStoppingEnrol)
        XCTAssertFalse(try read(dir, "argv.txt").contains("--appstore-enroll-finish"), "nothing to move in")
    }

    @MainActor
    func test_the_time_limit_is_its_own_outcome() async throws {
        let stop = try stopFile()
        let (runner, _) = try cli(Self.enrolling(cancelFile: stop, limit: 900))
        let shell = RecordingShell()
        shell.error = AdminShellError(number: CLIContract.EnrolExit.timedOut, message: "graft: ran past its time limit")
        let store = AppStoreAccountStore(runner: runner, shell: shell)
        await store.prepareEnrol(appPath: "/Applications/Example.app")
        await store.confirmEnrol()
        XCTAssertEqual(store.enrol, .timedOut(limitSeconds: 900))
        XCTAssertEqual(AppStoreEnrollSheet.timedOutLine(limitSeconds: 900),
                       "The setup ran past its time limit (15 minutes) and was stopped. Nothing was changed.")
    }

    /// A stop that lands too late, or a run that fails anyway: the cancel
    /// file still goes, and the outcome is what actually happened.
    @MainActor
    func test_the_cancel_file_is_removed_whatever_the_outcome() async throws {
        for afterStop in [nil, AdminShellError(number: 3, message: "setup failed")] {
            let stop = try stopFile()
            let (runner, dir) = try cli(Self.enrolling(cancelFile: stop))
            let shell = StoppableShell(file: stop)
            shell.afterStop = afterStop
            let store = AppStoreAccountStore(runner: runner, shell: shell)
            await store.prepareEnrol(appPath: "/Applications/Example.app")
            let run = Task { await store.confirmEnrol() }
            await eventually { store.enrol == .running }
            store.stopEnrol()
            await run.value
            XCTAssertFalse(FileManager.default.fileExists(atPath: stop.path))
            if afterStop == nil {
                XCTAssertEqual(store.enrol, .done, "root finished first: its model is moved in")
                XCTAssertTrue(try read(dir, "argv.txt").contains("--appstore-enroll-finish"))
            } else {
                XCTAssertEqual(store.enrol, .failed("setup failed"))
            }
        }
    }

    @MainActor
    func test_a_cancel_file_already_there_is_a_stop_already_asked_for() throws {
        let stop = try stopFile()
        XCTAssertNil(EnrolStopFile.create(stop.path))
        XCTAssertNil(EnrolStopFile.create(stop.path), "EEXIST is not a failure")
        EnrolStopFile.remove(stop.path)
        XCTAssertFalse(FileManager.default.fileExists(atPath: stop.path))
        EnrolStopFile.remove(stop.path)
        XCTAssertNotNil(EnrolStopFile.create("/nonexistent-\(UUID().uuidString)/stop"), "a real failure says so")
    }

    func test_cancel_stays_enabled_while_root_runs_until_a_stop_is_asked_for() {
        typealias Sheet = AppStoreEnrollSheet
        XCTAssertTrue(Sheet.cancelEnabled(.running, stopping: false, canStop: true))
        XCTAssertFalse(Sheet.cancelEnabled(.running, stopping: true, canStop: true))
        XCTAssertFalse(Sheet.cancelEnabled(.running, stopping: false, canStop: false), "a CLI with no cancel file can't stop it")
        XCTAssertTrue(Sheet.cancelEnabled(.idle, stopping: false, canStop: false))
        XCTAssertEqual(Sheet.runningLine(limitSeconds: 600), "This can take a few minutes; it stops by itself after 10 minutes.")
        XCTAssertEqual(Sheet.runningLine(limitSeconds: nil), "This can take a few minutes.")
        XCTAssertEqual(Sheet.duration(60), "1 minute")
        XCTAssertEqual(Sheet.duration(61), "2 minutes")
        XCTAssertEqual(Sheet.stoppedLine, "Stopped: nothing was changed.")
    }

    /// The try15 owner round: after a restart, root's copy of this Mac's
    /// FairPlay state had no SIDB, and UnfairPlay's own words were all the
    /// sheet said. Opening the picked app once wrote it again. The advice
    /// comes first, naming that app; UnfairPlay's words stay, for a report.
    func test_no_sidb_state_says_to_open_the_picked_app_once_then_set_up_again() {
        typealias Sheet = AppStoreEnrollSheet
        let raw = "error: normalize capture: canonical input has no SIDB state"
        let line = Sheet.failedLine(raw, appName: "Example Game")
        XCTAssertTrue(line.contains("Open Example Game once, then set up again."), line)
        XCTAssertTrue(line.contains("macOS sometimes clears it, even for an app you've already opened."), line)
        XCTAssertNil(Sheet.advice(for: "error: something else", appName: "Example Game"))
        XCTAssertTrue(line.hasSuffix("\n" + raw), line)
        XCTAssertFalse(line.hasPrefix(raw), "the advice comes first")
        XCTAssertTrue(Sheet.failedLine(raw, appName: nil).contains("Open the app you picked once"))
        XCTAssertEqual(Sheet.failedLine("error: something else", appName: "Example Game"), "error: something else",
                       "any other failure is said as it is")
    }

    // MARK: Account actions against a running get (the review's critic)

    /// A get that runs until stopped, a sign-in that waits after reading its
    /// two lines, and the enrollment verbs.
    private static func racing(_ dir: URL) -> String {
        try? (commandLine + "\n").write(to: dir.appending(path: "command.json"), atomically: true, encoding: .utf8)
        return """
        case "$*" in
          *--appstore-get*) exec /bin/sleep 30 ;;
          *--appstore-login*) IFS= read -r id; IFS= read -r pw; exec /bin/sleep 30 ;;
          *--appstore-logout*) printf '%s\\n' '{"type":"appstore-logout","signed_out":true}' ;;
          *--appstore-enroll-command*) /bin/cat '\(dir.path)/command.json' ;;
        esac
        """
    }

    @MainActor
    func test_sign_in_sign_out_and_set_up_wait_for_a_running_get() async throws {
        let (runner, dir) = try cli(Self.racing)
        let shell = RecordingShell()
        let store = AppStoreStore(runner: runner, shell: shell)
        let account = store.account
        await account.prepareEnrol(appPath: "/Applications/Example.app")
        guard case .ready = account.enrol else { return XCTFail("expected .ready, got \(account.enrol)") }

        XCTAssertTrue(store.startGet(app: "example"))
        XCTAssertEqual(store.accountBlockedReason, AppStoreStore.getRunningReason)

        XCTAssertFalse(signIn(account))
        XCTAssertEqual(account.signIn, .failed(AppStoreStore.getRunningReason))
        await account.signOut()
        XCTAssertEqual(account.signOutError, AppStoreStore.getRunningReason)
        await account.confirmEnrol()
        guard case .ready = account.enrol else { return XCTFail("Continue waits: \(account.enrol)") }
        XCTAssertTrue(shell.scripts.isEmpty)

        let argv = try read(dir, "argv.txt")
        XCTAssertFalse(argv.contains("--appstore-login"))
        XCTAssertFalse(argv.contains("--appstore-logout"))
        store.stopGet(app: "example")
        await eventually { !store.isBusy("example") }
        XCTAssertNil(store.accountBlockedReason)
    }

    @MainActor
    func test_gets_wait_for_a_running_sign_in() async throws {
        let (runner, dir) = try cli(Self.racing)
        let store = AppStoreStore(runner: runner, shell: RecordingShell())
        XCTAssertTrue(signIn(store.account))
        XCTAssertEqual(store.getBlockedReason, AppStoreStore.accountBusyReason)
        XCTAssertEqual(store.claimBlockedReason, AppStoreStore.accountBusyReason)
        XCTAssertFalse(store.startGet(app: "example"))
        XCTAssertEqual(store.errors["example"], AppStoreStore.accountBusyReason, "refused in words, not silently")
        await eventually { FileManager.default.fileExists(atPath: dir.appending(path: "argv.txt").path) }
        store.account.cancelSignIn()
        await eventually { !store.account.isSigningIn }
        XCTAssertNil(store.getBlockedReason)
        XCTAssertFalse(try read(dir, "argv.txt").contains("--appstore-get"))
    }

    @MainActor
    func test_candidates_load_and_none_is_an_answer() async throws {
        let (runner, _) = try cli { _ in
            "printf '%s\\n' '{\"type\":\"appstore-enroll-candidates-end\",\"count\":0}'"
        }
        let store = AppStoreAccountStore(runner: runner, shell: RecordingShell())
        XCTAssertFalse(store.hasLoadedCandidates)
        await store.loadCandidates()
        XCTAssertTrue(store.hasLoadedCandidates)
        XCTAssertEqual(store.candidates, [])
        XCTAssertNil(store.candidatesError)
    }

    /// The owner's run of the notarized try12 recorded macOS's dialog: it
    /// names Graft and asks for Touch ID or the password, with Use Password…
    /// and Cancel. The sheet says that, and no longer quotes the malware
    /// sentence Task 0's launchd spike saw, which this dialog doesn't have.
    ///
    /// Delta audit try13, #5: that was one Touch ID Mac, through
    /// `AppleScriptAdminShell`. So the sheet says the dialog *usually* names
    /// Graft, offers Touch ID only on a Mac that has it, and says the
    /// osascript fallback's dialog names osascript. No sentence states the
    /// recorded shape as fact for every Mac, and no "Use Password…." typo.
    func test_the_explanation_says_what_the_dialog_usually_shows_and_when_it_differs() {
        for verified in [true, false] {
            let lines = AppStoreEnrollSheet.explanation(verified: verified)
            let text = lines.joined(separator: " ")
            XCTAssertTrue(text.contains("one-time step"))
            XCTAssertTrue(text.contains("administrator password"))
            XCTAssertTrue(text.contains("Cancel is always safe"))
            XCTAssertTrue(text.contains("macOS's dialog usually names Graft"), text)
            XCTAssertTrue(text.contains("On a Mac with Touch ID it offers Touch ID or Use Password\u{2026}"), text)
            XCTAssertTrue(text.contains("on one without, it asks for the password"), text)
            XCTAssertTrue(text.contains("through osascript instead, the dialog names osascript"), text)
            XCTAssertTrue(lines.contains(AppStoreEnrollSheet.dialogLine) || lines.contains { $0.hasPrefix(AppStoreEnrollSheet.dialogLine + " ") },
                          "\(verified): the dialog line, whole")
            for line in lines {
                XCTAssertFalse(line.contains("\u{2026}."), "an ellipsis then a full stop: \(line)")
                XCTAssertFalse(line.contains("dialog names Graft"), "the recorded shape as fact: \(line)")
                XCTAssertFalse(line.contains("offers Touch ID or Use Password\u{2026}") && !line.contains("with Touch ID"),
                               "Touch ID as fact for every Mac: \(line)")
            }
            for gone in ["malware", "Apple could not verify", "Don't Allow", "Don\u{2019}t Allow", "bash"] {
                XCTAssertFalse(text.contains(gone), "\(verified): \(gone)")
            }
        }
        // The recorded wording stays word for word (AdminShellTests pins it);
        // the sheet's "usually" is about it, not a rewrite of it.
        XCTAssertTrue(AdminShell.recordedDialogWording.hasPrefix("Allow administrator access for \u{201C}Graft\u{201D}?"))
    }

    /// Review part 4, #1: the bullets never changed, so an unchecked UnfairPlay
    /// read exactly like a checked one, down to "that's how macOS words every
    /// request", which reassures about the one thing that is now true.
    func test_the_explanation_claims_a_check_only_when_the_script_makes_one() {
        let checked = AppStoreEnrollSheet.explanation(verified: true).joined(separator: " ")
        let unchecked = AppStoreEnrollSheet.explanation(verified: false).joined(separator: " ")
        XCTAssertTrue(checked.contains("only after checking that it carries Graft's developer signature"))
        XCTAssertTrue(unchecked.contains("a copy of UnfairPlay that nothing has checked"))
        for claim in ["after checking", "developer signature", "That's how macOS words every request"] {
            XCTAssertFalse(unchecked.contains(claim), "an unchecked script's explanation says: \(claim)")
        }
    }

    // MARK: An UnfairPlay nothing checks (review part 4, #1 and #28)

    private static let helperPath = "/u/src/unfairplay/unfairplay"
    private static let refusalLine = #"{"type":"appstore-refused","app":"/Applications/Example.app","reason":"unverified-helper","kind":"ad-hoc","helper":"/u/src/unfairplay/unfairplay","message":"This Graft.app has no developer team signature, so nothing can check the UnfairPlay it would run as root."}"#
    private static let uncheckedLine = #"{"type":"appstore-enroll-command","app":"/Applications/Example.app","model_dir":"/u/m","script":"set -eu\necho unchecked\n","verified":false,"helper":"/u/src/unfairplay/unfairplay","unverified_kind":"ad-hoc","unverified_reason":"This Graft.app has no developer team signature, so nothing can check the UnfairPlay it would run as root.","cancel_file":null,"time_limit_seconds":600}"#

    /// The CLI's side: the refusal unless `--allow-unverified-helper` is on
    /// argv, then the unchecked script. `kind` replaces the refusal's.
    private static func refusingUnchecked(kind: String = "ad-hoc") -> (URL) -> String {
        { dir in
            try? (refusalLine.replacingOccurrences(of: #""kind":"ad-hoc""#, with: #""kind":"\#(kind)""#) + "\n")
                .write(to: dir.appending(path: "refusal.json"), atomically: true, encoding: .utf8)
            try? (uncheckedLine.replacingOccurrences(of: #""unverified_kind":"ad-hoc""#, with: #""unverified_kind":"\#(kind)""#) + "\n")
                .write(to: dir.appending(path: "command.json"), atomically: true, encoding: .utf8)
            return """
            case "$*" in
              *--allow-unverified-helper*) /bin/cat '\(dir.path)/command.json' ;;
              *--appstore-enroll-command*)
                /bin/cat '\(dir.path)/refusal.json'
                echo "error: nothing would check UnfairPlay" >&2
                exit 20 ;;
              *--appstore-enroll-finish*) printf '%s\\n' '{"type":"appstore-enrolled","model_dir":"/u/m","count":1}' ;;
            esac
            """
        }
    }

    @MainActor
    func test_the_refusal_is_a_warning_of_its_own_and_nothing_runs() async throws {
        let (runner, _) = try cli(Self.refusingUnchecked())
        let shell = RecordingShell()
        let store = AppStoreAccountStore(runner: runner, shell: shell)
        await store.prepareEnrol(appPath: "/Applications/Example.app")
        XCTAssertEqual(store.enrol, .unchecked(UncheckedHelper(
            kind: "ad-hoc", helper: Self.helperPath,
            message: "This Graft.app has no developer team signature, so nothing can check the UnfairPlay it would run as root."
        )), "the CLI's own sentence and the path, not its stderr")
        await store.confirmEnrol()
        XCTAssertTrue(shell.scripts.isEmpty, "nothing reaches the administrator prompt from a refusal")
    }

    @MainActor
    func test_run_unchecked_asks_again_with_the_flag_and_runs_only_after_continue() async throws {
        let (runner, dir) = try cli(Self.refusingUnchecked())
        let shell = RecordingShell()
        let store = AppStoreAccountStore(runner: runner, shell: shell)
        await store.prepareEnrol(appPath: "/Applications/Example.app")
        await store.prepareEnrol(appPath: "/Applications/Example.app", allowUncheckedHelper: true)
        guard case let .ready(command) = store.enrol else { return XCTFail("expected .ready, got \(store.enrol)") }
        XCTAssertFalse(command.verified)
        XCTAssertTrue(store.uncheckedConsent)
        XCTAssertTrue(shell.scripts.isEmpty, "still explained first: Continue runs it")

        await store.confirmEnrol()
        XCTAssertEqual(shell.scripts, ["set -eu\necho unchecked\n"])
        XCTAssertEqual(try read(dir, "argv.txt").split(separator: "\n").prefix(2), [
            "--json --appstore-enroll-command --app /Applications/Example.app",
            "--json --appstore-enroll-command --app /Applications/Example.app --allow-unverified-helper",
        ])
    }

    @MainActor
    func test_the_flag_is_never_sent_without_the_warning_having_been_shown() async throws {
        let (runner, dir) = try cli(Self.refusingUnchecked())
        let store = AppStoreAccountStore(runner: runner, shell: RecordingShell())
        await store.prepareEnrol(appPath: "/Applications/Example.app", allowUncheckedHelper: true)
        XCTAssertEqual(store.enrol, .idle)
        XCTAssertFalse(FileManager.default.fileExists(atPath: dir.appending(path: "argv.txt").path),
                       "the CLI wasn't even asked")
    }

    @MainActor
    func test_an_unknown_kind_offers_no_second_step() async throws {
        let (runner, dir) = try cli(Self.refusingUnchecked(kind: "something-new"))
        let store = AppStoreAccountStore(runner: runner, shell: RecordingShell())
        await store.prepareEnrol(appPath: "/Applications/Example.app")
        guard case .unchecked = store.enrol else { return XCTFail("expected .unchecked, got \(store.enrol)") }
        await store.prepareEnrol(appPath: "/Applications/Example.app", allowUncheckedHelper: true)
        XCTAssertFalse(try read(dir, "argv.txt").contains("--allow-unverified-helper"))
    }

    /// The CLI that hands over an unchecked script without being asked (one
    /// from before the refusal existed): the script is still the warning, and
    /// never reaches the prompt without the explicit step.
    @MainActor
    func test_an_unchecked_script_handed_over_unasked_never_runs() async throws {
        let (runner, _) = try cli { dir in
            try? (Self.uncheckedLine + "\n").write(to: dir.appending(path: "command.json"), atomically: true, encoding: .utf8)
            return "/bin/cat '\(dir.path)/command.json'"
        }
        let shell = RecordingShell()
        let store = AppStoreAccountStore(runner: runner, shell: shell)
        await store.prepareEnrol(appPath: "/Applications/Example.app")
        guard case let .unchecked(refusal) = store.enrol else { return XCTFail("expected .unchecked, got \(store.enrol)") }
        XCTAssertEqual(refusal.helper, Self.helperPath)
        XCTAssertFalse(store.uncheckedConsent)
        await store.confirmEnrol()
        XCTAssertTrue(shell.scripts.isEmpty)
    }

    private func command(verified: Bool, kind: String?) -> EnrollCommandRecord {
        EnrollCommandRecord(app: "/Applications/Example.app", modelDir: "/u/m", script: "x",
                            verified: verified, unverifiedKind: kind)
    }

    func test_what_may_run_as_root() {
        typealias A = AppStoreAccountStore
        XCTAssertTrue(A.mayRun(command(verified: true, kind: nil), consented: false))
        XCTAssertFalse(A.mayRun(command(verified: false, kind: "ad-hoc"), consented: false), "never without the step")
        XCTAssertTrue(A.mayRun(command(verified: false, kind: "ad-hoc"), consented: true, release: true))
        XCTAssertFalse(A.mayRun(command(verified: false, kind: "not-bundled"), consented: true, release: true),
                       "a release build never runs someone's own UnfairPlay as root")
        XCTAssertTrue(A.mayRun(command(verified: false, kind: "not-bundled"), consented: true, release: false))
        XCTAssertFalse(A.mayRun(command(verified: false, kind: nil), consented: true, release: false))
        XCTAssertFalse(A.mayRun(command(verified: false, kind: "something-new"), consented: true, release: false))
    }

    func test_a_release_build_refuses_not_bundled_in_words_and_offers_ad_hoc() {
        XCTAssertEqual(AppStoreEnrollSheet.uncheckedOffer(kind: "ad-hoc", release: true), .runAnyway)
        XCTAssertEqual(AppStoreEnrollSheet.uncheckedOffer(kind: "not-bundled", release: false), .runAnyway)
        guard case let .refused(why) = AppStoreEnrollSheet.uncheckedOffer(kind: "not-bundled", release: true) else {
            return XCTFail("a release build must refuse not-bundled")
        }
        XCTAssertTrue(why.contains("runs only the UnfairPlay inside Graft.app"))
        XCTAssertNotEqual(AppStoreEnrollSheet.uncheckedOffer(kind: "", release: false), .runAnyway)
    }

    func test_the_sheet_says_which_way_the_administrator_step_ran() {
        XCTAssertEqual(AppStoreEnrollSheet.viaLine(.appleScript), "The administrator step ran in Graft (NSAppleScript).")
        XCTAssertEqual(AppStoreEnrollSheet.viaLine(.osascript), "The administrator step ran through osascript.")
        XCTAssertNil(AppStoreEnrollSheet.viaLine(nil))
    }
}
