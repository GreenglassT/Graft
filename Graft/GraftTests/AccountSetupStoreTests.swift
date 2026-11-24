import XCTest
@testable import Graft

/// The account setup the app runs in place of `graft --init` in Terminal:
/// which `graft` it runs, and where the .p12 password goes and how long it
/// lives. A stand-in `graft` writes down what it was given.
@MainActor
final class AccountSetupStoreTests: XCTestCase {
    static let secret = "correct horse battery"

    static let freeResult = #"{"type":"init-result","account":"free","config":"/tmp/example/config.yaml","change":"created","dry_run":false,"summary":"Set up for a free Apple ID.","team_id":null,"profile_name":null,"certificate":null,"device_count":null,"devices":[],"expires":null,"pinned":[],"sideloads":[],"steps":["Install SideStore (https://sidestore.io), or use iloader.","Install LiveContainer: https://example.com/guide.","In LiveContainer, open Settings and tap Import Certificate from SideStore."]}"#

    static func paidResult(dryRun: Bool) -> String {
        #"{"type":"init-result","account":"paid","config":"/tmp/example/config.yaml","change":"created","dry_run":\#(dryRun),"summary":"Set up for team ABCDE12345.","team_id":"ABCDE12345","profile_name":"Test Wildcard","certificate":"Apple Development: Test Person (XYZ9876543)","device_count":2,"devices":["00008130-001A2B3C0123456D","00008110-000E1C2A3B4C5D6E"],"expires":"2027-06-01T12:00:00Z","pinned":[],"sideloads":[],"steps":[]}"#
    }

    /// What the stand-in wrote down.
    struct Fake {
        let runner: CLIRunner
        let dir: URL

        func read(_ name: String) throws -> String {
            try String(contentsOf: dir.appendingPathComponent(name), encoding: .utf8)
        }

        func argv() throws -> [String] {
            try read("argv").split(separator: "\n").map(String.init)
        }

        func wrote(_ name: String) -> Bool {
            FileManager.default.fileExists(atPath: dir.appendingPathComponent(name).path)
        }
    }

    /// A stand-in `graft`. It writes its arguments, its environment and, for a
    /// paid run, the line on its stdin into a fresh directory. Then it prints
    /// `dry` and exits `dryStatus` under `--dry-run`, or prints `real` and
    /// exits `realStatus` otherwise, with `stderr` on stderr either way. No
    /// single quotes in any of them.
    private func fakeGraft(
        dry: String = "", dryStatus: Int32 = 0,
        real: String = "", realStatus: Int32 = 0, stderr: String = ""
    ) throws -> Fake {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-account-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let script = dir.appendingPathComponent("graft")
        try """
        #!/bin/sh
        printf '%s\\n' "$@" > '\(dir.path)/argv'
        env > '\(dir.path)/env'
        case " $* " in
          *" paid "*) IFS= read -r line; printf '%s' "$line" > '\(dir.path)/stdin' ;;
        esac
        case " $* " in
          *" --dry-run "*)
            printf '%s\\n' '\(dry)'
            printf '%s\\n' '\(stderr)' >&2
            exit \(dryStatus) ;;
        esac
        printf '%s\\n' '\(real)'
        printf '%s\\n' '\(stderr)' >&2
        exit \(realStatus)
        """.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
        return Fake(runner: CLIRunner(executable: script), dir: dir)
    }

    private func filled(_ store: AccountSetupStore) {
        store.choosePaid()
        store.form.p12Path = "/tmp/example/Certificates.p12"
        store.form.password = Self.secret
        store.form.profilePath = "/tmp/example/Test.mobileprovision"
    }

    func test_the_arguments_are_the_clis_own_flags() {
        XCTAssertEqual(AccountSetupStore.freeArguments, ["--json", "--init", "--account", "free"])
        XCTAssertEqual(
            AccountSetupStore.paidArguments(p12: "/x/a.p12", profile: "/x/b.mobileprovision", dryRun: true),
            ["--json", "--init", "--account", "paid", "--p12", "/x/a.p12",
             "--profile", "/x/b.mobileprovision", "--dry-run"]
        )
        XCTAssertFalse(AccountSetupStore.paidArguments(p12: "a", profile: "b", dryRun: false).contains("--dry-run"))
    }

    func test_no_sets_up_a_free_apple_id_and_holds_the_screen_for_its_steps() async throws {
        let fake = try fakeGraft(real: Self.freeResult)
        let store = AccountSetupStore(runner: fake.runner)
        store.begin()
        XCTAssertEqual(store.step, .question)

        await store.chooseFree()

        guard case let .done(result) = store.step else { return XCTFail("\(store.step)") }
        XCTAssertEqual(result.account, .free)
        XCTAssertEqual(result.steps.count, 3)
        XCTAssertTrue(store.holdsSetupScreen, "the steps are on screen until they are read")
        XCTAssertEqual(try fake.argv(), AccountSetupStore.freeArguments)
        XCTAssertFalse(fake.wrote("stdin"), "nothing on stdin for a free Apple ID")
        XCTAssertNil(store.error)

        store.finish()
        XCTAssertFalse(store.holdsSetupScreen)
        XCTAssertEqual(store.step, .welcome)
    }

    func test_the_password_goes_on_stdin_and_nowhere_else() async throws {
        let fake = try fakeGraft(dry: Self.paidResult(dryRun: true))
        let store = AccountSetupStore(runner: fake.runner)
        filled(store)

        await store.review()

        guard case let .paidReview(result) = store.step else { return XCTFail("\(store.step)") }
        XCTAssertEqual(result.teamId, "ABCDE12345")
        XCTAssertEqual(result.devices.count, 2)
        XCTAssertEqual(try fake.read("stdin"), Self.secret)
        XCTAssertFalse(try fake.read("argv").contains(Self.secret), "the password is in argv")
        XCTAssertFalse(try fake.read("env").contains(Self.secret), "the password is in the environment")
        XCTAssertEqual(try fake.argv().last, "--dry-run")
        XCTAssertEqual(store.form.password, Self.secret, "kept for the confirm, and only for it")
    }

    func test_confirm_clears_the_password_when_it_works() async throws {
        let fake = try fakeGraft(dry: Self.paidResult(dryRun: true), real: Self.paidResult(dryRun: false))
        let store = AccountSetupStore(runner: fake.runner)
        filled(store)
        await store.review()

        await store.confirm()

        guard case let .done(result) = store.step else { return XCTFail("\(store.step)") }
        XCTAssertFalse(result.dryRun)
        XCTAssertEqual(store.form.password, "")
        XCTAssertEqual(try fake.read("stdin"), Self.secret, "the confirm sent it too")
        XCTAssertFalse(try fake.argv().contains("--dry-run"))
    }

    func test_confirm_clears_the_password_when_it_fails_and_goes_back_to_the_form() async throws {
        let fake = try fakeGraft(
            dry: Self.paidResult(dryRun: true),
            realStatus: 1, stderr: "error: the Keychain refused the password"
        )
        let store = AccountSetupStore(runner: fake.runner)
        filled(store)
        await store.review()

        await store.confirm()

        XCTAssertEqual(store.step, .paidForm)
        XCTAssertEqual(store.form.password, "")
        XCTAssertEqual(store.form.p12Path, "/tmp/example/Certificates.p12", "the choices stay")
        XCTAssertEqual(store.error, "the Keychain refused the password")
    }

    func test_a_failed_review_stays_on_the_form_and_clears_the_password() async throws {
        let fake = try fakeGraft(dryStatus: 2, stderr: "error: wrong password for Certificates.p12")
        let store = AccountSetupStore(runner: fake.runner)
        filled(store)

        await store.review()

        XCTAssertEqual(store.step, .paidForm)
        XCTAssertEqual(store.form.password, "")
        XCTAssertEqual(store.error, "wrong password for Certificates.p12")
    }

    func test_back_and_cancel_clear_the_password() async throws {
        let fake = try fakeGraft(dry: Self.paidResult(dryRun: true))
        let store = AccountSetupStore(runner: fake.runner)
        filled(store)
        await store.review()

        store.back()
        XCTAssertEqual(store.step, .paidForm)
        XCTAssertEqual(store.form.password, "")

        store.form.password = Self.secret
        store.cancel()
        XCTAssertEqual(store.form, AccountSetupStore.PaidForm())
        XCTAssertEqual(store.step, .welcome)
    }

    func test_a_run_that_reports_no_result_is_an_error_not_a_success() async throws {
        let fake = try fakeGraft(real: "")
        let store = AccountSetupStore(runner: fake.runner)
        store.begin()

        await store.chooseFree()

        XCTAssertEqual(store.step, .question)
        XCTAssertNotNil(store.error)
    }

    func test_replace_sends_replace_on_the_review_and_the_confirm() async throws {
        let fake = try fakeGraft(dry: Self.paidResult(dryRun: true), real: Self.paidResult(dryRun: false))
        let store = AccountSetupStore(runner: fake.runner)
        store.startReplace()
        XCTAssertEqual(store.step, .paidForm)
        store.form.p12Path = "/tmp/example/Certificates.p12"
        store.form.password = Self.secret
        store.form.profilePath = "/tmp/example/Test.mobileprovision"

        await store.review()
        XCTAssertEqual(Array(try fake.argv().suffix(2)), ["--replace", "--dry-run"])
        await store.confirm()

        guard case .done = store.step else { return XCTFail("\(store.step)") }
        XCTAssertEqual(try fake.argv().last, "--replace")
        XCTAssertEqual(try fake.read("stdin"), Self.secret)
        XCTAssertEqual(store.form.password, "")
    }

    func test_a_new_flow_after_a_replace_is_not_a_replace() {
        let store = AccountSetupStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/false")))
        store.startReplace()
        XCTAssertTrue(store.replacing)

        store.startUpgrade()

        XCTAssertFalse(store.replacing, "Add a paid account must not send --replace")
        XCTAssertFalse(AccountSetupStore.paidArguments(p12: "a", profile: "b", dryRun: true)
            .contains(CLIContract.Flag.replace))
    }

    func test_a_replace_result_names_the_team_it_moved_from() throws {
        let line = #"{"type":"init-result","account":"paid","config":"/tmp/x/config.yaml","change":"replaced","dry_run":true,"summary":"Everything checks out.","team_id":"FGHIJ67890","profile_name":"Test Wildcard","certificate":"Apple Development: Test Person (XYZ9876543)","device_count":2,"devices":["00008130-001A2B3C0123456D","00008110-000E1C2A3B4C5D6E"],"expires":"2027-06-01T12:00:00Z","pinned":[],"sideloads":[],"steps":[],"previous_team_id":"ABCDE12345"}"#
        guard case let .initResult(result) = try BuildEvent.decode(line: line) else { return XCTFail(line) }
        XCTAssertEqual(result.movedFromTeam, "ABCDE12345")
        XCTAssertTrue(AccountSetupView.replaceNote(result).contains("from team ABCDE12345"))
    }

    func test_settings_starts_the_upgrade_on_a_clean_form() {
        let store = AccountSetupStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/false")))
        store.form.password = Self.secret
        store.error = "left over"

        store.startUpgrade()

        XCTAssertEqual(store.step, .paidForm)
        XCTAssertEqual(store.form, AccountSetupStore.PaidForm())
        XCTAssertNil(store.error)
    }

    // MARK: - A review can be stopped; a write can't, and Settings re-reads after it (APP-1)

    /// A stand-in that takes its time: `dry-started`/`real-started` when it
    /// begins, then sleeps, then `…-finished`. A SIGTERM'd shell never
    /// writes the second.
    private func slowGraft(
        dry: String = "", dryDelay: Double = 0, real: String = "", realDelay: Double = 0,
        realStatus: Int32 = 0
    ) throws -> Fake {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-account-slow-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let script = dir.appendingPathComponent("graft")
        try """
        #!/bin/sh
        printf '%s\\n' "$@" > '\(dir.path)/argv'
        case " $* " in
          *" paid "*) IFS= read -r line; printf '%s' "$line" > '\(dir.path)/stdin' ;;
        esac
        case " $* " in
          *" --dry-run "*)
            : > '\(dir.path)/dry-started'
            sleep \(dryDelay)
            : > '\(dir.path)/dry-finished'
            printf '%s\\n' '\(dry)'
            exit 0 ;;
        esac
        : > '\(dir.path)/real-started'
        sleep \(realDelay)
        : > '\(dir.path)/real-finished'
        printf '%s\\n' '\(real)'
        exit \(realStatus)
        """.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
        return Fake(runner: CLIRunner(executable: script), dir: dir)
    }

    private func waitUntil(timeout: TimeInterval = 10, _ condition: () -> Bool) async {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition(), Date() < deadline {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    final class Count { var value = 0 }

    func test_cancel_stops_a_review_and_its_cli_and_its_answer_never_lands() async throws {
        let fake = try slowGraft(dry: Self.paidResult(dryRun: true), dryDelay: 1)
        let store = AccountSetupStore(runner: fake.runner)
        filled(store)

        store.startReview()
        await waitUntil { fake.wrote("dry-started") }
        XCTAssertTrue(store.isRunning)
        XCTAssertFalse(store.isWriting, "a review writes nothing")
        store.cancel()
        await waitUntil { !store.isRunning }
        try await Task.sleep(nanoseconds: 1_600_000_000)

        XCTAssertFalse(store.isRunning)
        XCTAssertEqual(store.step, .welcome)
        XCTAssertFalse(fake.wrote("dry-finished"), "the CLI ran on after Cancel")
        XCTAssertEqual(store.form, AccountSetupStore.PaidForm())
    }

    func test_back_stops_a_review_too() async throws {
        let fake = try slowGraft(dry: Self.paidResult(dryRun: true), dryDelay: 1)
        let store = AccountSetupStore(runner: fake.runner)
        store.begin()
        filled(store)

        store.startReview()
        await waitUntil { fake.wrote("dry-started") }
        store.back()
        await waitUntil { !store.isRunning }
        try await Task.sleep(nanoseconds: 1_600_000_000)

        XCTAssertEqual(store.step, .question)
        XCTAssertFalse(fake.wrote("dry-finished"))
    }

    func test_a_setup_that_writes_cannot_be_cancelled_or_backed_out_of() async throws {
        let fake = try slowGraft(
            dry: Self.paidResult(dryRun: true), real: Self.paidResult(dryRun: false), realDelay: 1
        )
        let store = AccountSetupStore(runner: fake.runner)
        let rereads = Count()
        store.onAccountRunFinished = { rereads.value += 1 }
        filled(store)
        await store.review()

        let confirming = Task { await store.confirm() }
        await waitUntil { store.isWriting }
        XCTAssertTrue(store.holdsSetupScreen, "a window return can't take the screen away mid-write")
        store.cancel()
        store.back()
        store.finish()
        guard case .paidReview = store.step else { return XCTFail("left the review: \(store.step)") }
        XCTAssertEqual(rereads.value, 0)
        await confirming.value

        guard case .done = store.step else { return XCTFail("\(store.step)") }
        XCTAssertTrue(fake.wrote("real-finished"))
        XCTAssertEqual(rereads.value, 1, "Settings re-reads once the write is over")
    }

    func test_settings_rereads_after_a_setup_that_failed_too() async throws {
        let fake = try slowGraft(dry: Self.paidResult(dryRun: true), realStatus: 1)
        let store = AccountSetupStore(runner: fake.runner)
        let rereads = Count()
        store.onAccountRunFinished = { rereads.value += 1 }
        filled(store)
        await store.review()

        await store.confirm()

        XCTAssertEqual(store.step, .paidForm)
        XCTAssertEqual(rereads.value, 1)
    }

    func test_a_switch_to_free_rereads_whatever_it_came_to_and_never_shows_an_old_result() async throws {
        // Works the first time, fails every time after.
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-account-switch-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let script = dir.appendingPathComponent("graft")
        try """
        #!/bin/sh
        if [ -e '\(dir.path)/ran' ]; then echo 'error: no' >&2; exit 2; fi
        : > '\(dir.path)/ran'
        printf '%s\\n' '\(Self.freeResult)'
        """.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
        let store = AccountSetupStore(runner: CLIRunner(executable: script))
        let rereads = Count()
        store.onAccountRunFinished = { rereads.value += 1 }

        await store.switchToFree()
        guard case .done = store.step else { return XCTFail("\(store.step)") }
        XCTAssertEqual(rereads.value, 1)

        // The sheet was never closed (the pane was left): the next switch,
        // which fails, must not read as the last one's success.
        await store.switchToFree()

        XCTAssertEqual(store.step, .welcome, "the old result read as this switch's")
        XCTAssertNotNil(store.error)
        XCTAssertEqual(rereads.value, 2, "re-read after the failure too")
    }

    func test_an_answer_that_comes_back_after_cancel_is_dropped() async throws {
        let fake = try slowGraft(dry: Self.paidResult(dryRun: true), dryDelay: 0.5)
        let store = AccountSetupStore(runner: fake.runner)
        filled(store)

        // Not `startReview`: nothing cancels this run, so its answer arrives.
        let reviewing = Task { await store.review() }
        await waitUntil { fake.wrote("dry-started") }
        store.cancel()
        await reviewing.value

        XCTAssertTrue(fake.wrote("dry-finished"), "premise: the CLI answered")
        XCTAssertEqual(store.step, .welcome, "an answer from before Cancel reappeared")
    }

    // MARK: - Add a paid account and the first run never send --replace (F6)

    func test_add_a_paid_account_runs_without_replace_on_the_review_and_the_confirm() async throws {
        let fake = try fakeGraft(dry: Self.paidResult(dryRun: true), real: Self.paidResult(dryRun: false))
        let store = AccountSetupStore(runner: fake.runner)
        store.startReplace()
        store.startUpgrade()
        store.form.p12Path = "/tmp/example/Certificates.p12"
        store.form.password = Self.secret
        store.form.profilePath = "/tmp/example/Test.mobileprovision"

        await store.review()
        let reviewed = try fake.argv()
        XCTAssertFalse(reviewed.contains(CLIContract.Flag.replace), "\(reviewed)")
        await store.confirm()
        guard case .done = store.step else { return XCTFail("\(store.step)") }
        let confirmed = try fake.argv()
        XCTAssertFalse(confirmed.contains(CLIContract.Flag.replace), "\(confirmed)")
    }

    func test_the_first_runs_paid_answer_runs_without_replace() async throws {
        let fake = try fakeGraft(dry: Self.paidResult(dryRun: true), real: Self.paidResult(dryRun: false))
        let store = AccountSetupStore(runner: fake.runner)
        store.begin()
        filled(store)

        await store.review()
        XCTAssertFalse(try fake.argv().contains(CLIContract.Flag.replace))
        await store.confirm()
        XCTAssertFalse(try fake.argv().contains(CLIContract.Flag.replace))
    }

    // MARK: - A blank-password .p12, and the CLI's warnings

    func test_a_blank_password_is_sent_as_an_empty_line() async throws {
        let fake = try fakeGraft(dry: Self.paidResult(dryRun: true))
        let store = AccountSetupStore(runner: fake.runner)
        store.choosePaid()
        store.form.p12Path = "/tmp/example/Certificates.p12"
        store.form.profilePath = "/tmp/example/Test.mobileprovision"
        XCTAssertTrue(store.form.isReady, "Keychain Access exports with a blank password too")

        await store.review()

        guard case .paidReview = store.step else { return XCTFail("\(store.step)") }
        XCTAssertEqual(try fake.read("stdin"), "")
    }

    func test_the_warnings_decode_and_an_older_cli_has_none() throws {
        let line = #"{"type":"init-result","account":"paid","config":"/tmp/x/config.yaml","change":"replaced","dry_run":false,"summary":"Replaced.","team_id":"FGHIJ67890","profile_name":null,"certificate":null,"device_count":0,"devices":[],"expires":null,"pinned":[],"sideloads":[],"steps":[],"previous_team_id":"ABCDE12345","warnings":["the App Store Connect key is for team ABCDE12345; automatic profiles will be refused until you set a key for team FGHIJ67890."]}"#
        guard case let .initResult(result) = try BuildEvent.decode(line: line) else { return XCTFail(line) }
        XCTAssertEqual(result.notes.count, 1)
        XCTAssertTrue(result.notes[0].contains("App Store Connect key is for team ABCDE12345"))
        let old = try XCTUnwrap(Self.paidResult(dryRun: true).buildEvents().first)
        guard case let .initResult(older) = old else { return XCTFail("\(old)") }
        XCTAssertEqual(older.notes, [])
    }
}
