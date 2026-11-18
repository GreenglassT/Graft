import XCTest
@testable import Graft

/// `AppStoreStore`: the GUI's half of getting an app from the App Store.
///
/// Every runner here is a real executable — a `/bin/sh` script that logs its
/// argv and prints canned events — so the argv under test is the one
/// production builds, not a fixture standing in for it (see memory
/// `empty-collection-is-not-a-load-state`, on why the store has no
/// `argsOverride`).
final class AppStoreStoreTests: XCTestCase {
    /// A script that appends its argv to a log file, then runs `body`.
    private func scriptRunner(_ body: String) throws -> (CLIRunner, URL) {
        let dir = FileManager.default.temporaryDirectory
            .appending(path: "appstore_store_\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let log = dir.appending(path: "argv.log")
        let script = dir.appending(path: "tb.sh")
        try """
        #!/bin/sh
        echo "$*" >> '\(log.path)'
        \(body)
        """.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
        return (CLIRunner(executable: script), log)
    }

    private func argv(_ log: URL) throws -> [String] {
        try String(contentsOf: log, encoding: .utf8)
            .split(separator: "\n").map(String.init)
    }

    private static let statusLine = #"{"type":"appstore-status","ipatool":{"path":"/i","present":true,"version":"2.8.0","signed_in":true,"warning":null},"unfairplay":{"path":"/u","present":true,"version":"dev","enrolled":true,"model_dir":"/m"},"setup_commands":[]}"#

    // MARK: status

    @MainActor
    func test_status_asks_for_json_and_the_verb() async throws {
        let (runner, log) = try scriptRunner("printf '%s\\n' '\(Self.statusLine)'")
        let store = AppStoreStore(runner: runner)

        await store.loadStatus()

        XCTAssertEqual(try argv(log), ["--json --appstore-status"])
        XCTAssertEqual(store.status?.ipatool.version, "2.8.0")
        XCTAssertTrue(store.hasLoadedStatus)
        XCTAssertNil(store.statusError)
    }

    /// The house defect, in this store's terms. A failed load is a *loaded*
    /// state with an error — not "still checking" forever, and not a reason
    /// for the next appearance of the pane to spawn the CLI again.
    @MainActor
    func test_a_failed_status_load_is_loaded_with_an_error() async throws {
        let (runner, _) = try scriptRunner("echo 'error: No such option: --appstore-status' >&2; exit 2")
        let store = AppStoreStore(runner: runner)
        XCTAssertFalse(store.hasLoadedStatus)

        await store.loadStatus()

        XCTAssertTrue(store.hasLoadedStatus)
        XCTAssertNil(store.status)
        XCTAssertNotNil(store.statusError)
    }

    @MainActor
    func test_a_reload_that_fails_drops_the_old_answer() async throws {
        // A status that says "signed in" from before the failure would be a
        // claim nothing currently supports.
        let flag = FileManager.default.temporaryDirectory
            .appending(path: "appstore_fail_\(UUID().uuidString)")
        addTeardownBlock { try? FileManager.default.removeItem(at: flag) }
        let (runner, _) = try scriptRunner("""
        if [ -e '\(flag.path)' ]; then exit 1; fi
        printf '%s\\n' '\(Self.statusLine)'
        """)
        let store = AppStoreStore(runner: runner)
        await store.loadStatus()
        XCTAssertNotNil(store.status)

        FileManager.default.createFile(atPath: flag.path, contents: Data())
        await store.loadStatus()

        XCTAssertNil(store.status)
        XCTAssertNotNil(store.statusError)
    }

    // MARK: version check

    @MainActor
    func test_check_asks_about_one_app_and_keys_the_answer_by_it() async throws {
        let line = #"{"type":"appstore-version","app":"example","bundle_id":"com.example.app","store_version":"21.46.3","cached_version":"21.40.0","update_available":true,"error":null}"#
        let (runner, log) = try scriptRunner("printf '%s\\n' '\(line)'")
        let store = AppStoreStore(runner: runner)

        await store.checkVersion(app: "example")

        XCTAssertEqual(try argv(log), ["--json --appstore-check --app example"])
        XCTAssertEqual(store.versions["example"]?.storeVersion, "21.46.3")
    }

    @MainActor
    func test_a_check_the_cli_could_not_run_is_an_answer_with_an_error() async throws {
        // An older CLI without the verb, or no network: the row says it could
        // not tell, rather than showing nothing (which reads as "not asked").
        let (runner, _) = try scriptRunner("echo 'error: offline' >&2; exit 13")
        let store = AppStoreStore(runner: runner)

        await store.checkVersion(app: "example")

        let record = try XCTUnwrap(store.versions["example"])
        XCTAssertNil(record.storeVersion)
        XCTAssertNotNil(record.error)
        XCTAssertFalse(record.updateAvailable)
    }

    // MARK: get

    private static let getScript = """
    printf '%s\\n' '{"type":"appstore-stage","app":"example","stage":"download"}'
    sleep 1
    printf '%s\\n' '{"type":"appstore-stage","app":"example","stage":"decrypt"}'
    printf '%s\\n' '{"type":"appstore-decrypted","app":"example","encrypted_images":8,"decrypted_pages":5,"plaintext_verified":true}'
    printf '%s\\n' '{"type":"appstore-stage","app":"example","stage":"stage"}'
    printf '%s\\n' '{"type":"ipa-provided","app":"example","bundle_id":"com.example.app","version":"21.46.3","filename":"com.example.app_21.46.3.ipa","path":"/c/com.example.app_21.46.3.ipa","size_bytes":10}'
    """

    @MainActor
    private func waitUntil(_ condition: () -> Bool, timeout: TimeInterval = 5) async {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition(), Date() < deadline {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    @MainActor
    func test_get_publishes_each_stage_as_it_arrives_then_clears_it() async throws {
        let (runner, log) = try scriptRunner(Self.getScript)
        let store = AppStoreStore(runner: runner)

        XCTAssertTrue(store.startGet(app: "example"))
        await waitUntil { store.stages["example"] == .download }
        XCTAssertEqual(store.stages["example"], .download, "the first stage is visible while the CLI runs")
        XCTAssertTrue(store.isGetting("example"))

        await waitUntil { store.completions["example"] != nil }

        XCTAssertEqual(store.completions["example"], 1)
        XCTAssertNil(store.stages["example"])
        XCTAssertFalse(store.isGetting("example"))
        XCTAssertEqual(try argv(log).first, "--json --appstore-get --app example")
    }

    @MainActor
    func test_a_failed_get_records_the_clis_message_and_clears_the_stage() async throws {
        let (runner, _) = try scriptRunner("""
        printf '%s\\n' '{"type":"appstore-stage","app":"example","stage":"download"}'
        echo "error: this Apple ID doesn't own com.example.app yet" >&2
        echo "  hint: tap Get for this app on an iPhone or iPad" >&2
        exit 20
        """)
        let store = AppStoreStore(runner: runner)

        XCTAssertTrue(store.startGet(app: "example"))
        await waitUntil { !store.isBusy("example") }

        let message = try XCTUnwrap(store.errors["example"])
        XCTAssertTrue(message.contains("doesn't own"), message)
        XCTAssertTrue(message.contains("tap Get"), message)
        XCTAssertNil(store.stages["example"])
        XCTAssertFalse(store.isGetting("example"))
    }

    /// `onEvent` hops to the MainActor and can land after the call returned.
    /// A stage arriving then must not put "Downloading…" back on a row whose
    /// get has finished.
    @MainActor
    func test_a_stage_for_a_get_that_is_not_running_is_ignored() {
        let store = AppStoreStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/true")))

        store.note(.appStoreStage(AppStoreStageRecord(app: "example", stage: .download)), forGetOf: "example")

        XCTAssertNil(store.stages["example"])
    }

    // MARK: ConfigStore.provideIPA(onStage:)

    @MainActor
    func test_a_drop_reports_decrypting_when_the_cli_says_so() async throws {
        let store = ConfigStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        let script = """
        printf '%s\\n' '{"type":"appstore-stage","app":"example","stage":"decrypt"}'
        sleep 1
        printf '%s\\n' '{"type":"ipa-provided","app":"example","bundle_id":"b","version":"1","filename":"b_1.ipa","path":"/c/b_1.ipa","size_bytes":1}'
        """
        var seen: [AppStoreStage] = []

        let record = try await store.provideIPA(
            appName: "example", fileURL: URL(fileURLWithPath: "/tmp/x.ipa"),
            argsOverride: ["-c", script], onStage: { seen.append($0) }
        )

        XCTAssertEqual(record?.record.filename, "b_1.ipa")
        XCTAssertEqual(record?.decrypted, false, "a decrypt stage alone is not the CLI saying it decrypted")
        XCTAssertEqual(seen, [.decrypt])
    }
}
