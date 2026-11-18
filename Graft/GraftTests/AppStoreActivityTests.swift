import XCTest
@testable import Graft

/// `AppStoreStore` as the owner of each app's in-flight IPA work.
///
/// Review found the app pane routing a get's outcome by comparing `app.name`
/// read off its own struct copy — always the app the get started for, so a get
/// for A that finished after switching to B wrote A's error into B's pane. The
/// pane cannot be pressed or read from an in-process test (measured: a hosted
/// SwiftUI `Button` is no `NSButton` subview, and the accessibility tree is
/// empty), so the outcome now lives here, keyed by app, where the pane only
/// *reads* it for the app it shows. These tests pin that keying, the
/// cancellation rules a `.task` relies on, Stop, and the version check's
/// freshness window.
final class AppStoreActivityTests: XCTestCase {
    private func scriptRunner(_ body: String) throws -> (CLIRunner, URL) {
        let dir = FileManager.default.temporaryDirectory
            .appending(path: "appstore_activity_\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let script = dir.appending(path: "tb.sh")
        try """
        #!/bin/sh
        echo "$*" >> '\(dir.appending(path: "argv.log").path)'
        \(body)
        """.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
        return (CLIRunner(executable: script), dir)
    }

    private func runs(_ dir: URL) -> [String] {
        ((try? String(contentsOf: dir.appending(path: "argv.log"), encoding: .utf8)) ?? "")
            .split(separator: "\n").map(String.init)
    }

    @MainActor
    private func waitUntil(_ condition: () -> Bool, timeout: TimeInterval = 10) async {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition(), Date() < deadline {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    private static let provided = #"{"type":"ipa-provided","app":"a","bundle_id":"com.a","version":"2","filename":"com.a_2.ipa","path":"/c/com.a_2.ipa","size_bytes":1}"#
    private static let version = #"{"type":"appstore-version","app":"a","bundle_id":"com.a","store_version":"2","cached_version":"2","update_available":false,"error":null}"#

    // MARK: I2 — a cancelled load is not a failed one

    @MainActor
    func test_a_cancelled_status_load_latches_nothing() async throws {
        let (runner, dir) = try scriptRunner("exec sleep 30")
        let store = AppStoreStore(runner: runner)

        let load = Task { await store.loadStatus() }
        await waitUntil { !self.runs(dir).isEmpty }
        load.cancel()
        await load.value

        XCTAssertFalse(store.hasLoadedStatus, "a cancelled load must leave the next .task free to load")
        XCTAssertNil(store.statusError, "the CLI being killed is not a status failure")
        XCTAssertFalse(store.isLoadingStatus)
    }

    @MainActor
    func test_a_cancelled_version_check_records_nothing() async throws {
        let (runner, dir) = try scriptRunner("exec sleep 30")
        let store = AppStoreStore(runner: runner)

        let check = Task { await store.checkVersion(app: "a") }
        await waitUntil { !self.runs(dir).isEmpty }
        check.cancel()
        await check.value

        XCTAssertNil(store.versions["a"], "an app switch cancels this; it must not read as 'couldn't check'")
    }

    // MARK: I1 — outcomes are keyed by the app they belong to

    @MainActor
    func test_a_failed_get_is_recorded_against_its_own_app_only() async throws {
        let (runner, _) = try scriptRunner("""
        echo "error: this Apple ID doesn't own com.a yet" >&2
        exit 20
        """)
        let store = AppStoreStore(runner: runner)

        XCTAssertTrue(store.startGet(app: "a"))
        await waitUntil { !store.isBusy("a") }

        XCTAssertTrue(store.errors["a"]?.contains("doesn't own com.a") == true, "\(store.errors)")
        XCTAssertNil(store.errors["b"])
        XCTAssertNil(store.completions["a"])
    }

    @MainActor
    func test_a_successful_get_completes_its_own_app_and_rechecks_it() async throws {
        let (runner, dir) = try scriptRunner("""
        case "$*" in
          *--appstore-get*) printf '%s\\n' '\(Self.provided)' ;;
          *--appstore-check*) printf '%s\\n' '\(Self.version)' ;;
        esac
        """)
        let store = AppStoreStore(runner: runner)

        XCTAssertTrue(store.startGet(app: "a"))
        await waitUntil { store.versions["a"] != nil && !store.isBusy("a") }

        XCTAssertEqual(store.completions["a"], 1)
        XCTAssertNil(store.completions["b"])
        XCTAssertEqual(store.lastCompleted?.app, "a")
        XCTAssertNil(store.errors["a"])
        XCTAssertEqual(runs(dir), ["--json --appstore-get --app a", "--json --appstore-check --app a"])
    }

    @MainActor
    func test_a_drop_is_owned_the_same_way() async throws {
        let (runner, _) = try scriptRunner("""
        case "$*" in
          *--provide-ipa*)
            printf '%s\\n' '{"type":"appstore-stage","app":"a","stage":"decrypt"}'
            sleep 1
            printf '%s\\n' '\(Self.provided)' ;;
        esac
        """)
        let store = AppStoreStore(runner: runner)
        let config = ConfigStore(runner: runner)

        XCTAssertTrue(store.startProvide(app: "a", fileURL: URL(fileURLWithPath: "/tmp/a.ipa"), config: config))
        await waitUntil { store.stages["a"] == .decrypt }
        XCTAssertEqual(store.stages["a"], .decrypt, "the drop's stage is the app's stage")
        await waitUntil { !store.isBusy("a") }

        XCTAssertEqual(store.completions["a"], 1)
        XCTAssertNil(store.stages["a"])
    }

    // MARK: I3 — one operation per app's cache at a time

    @MainActor
    func test_a_drop_is_refused_while_a_get_runs_for_that_app_but_not_another() async throws {
        let (runner, dir) = try scriptRunner("""
        case "$*" in
          *--appstore-get*) exec sleep 30 ;;
          *--provide-ipa*) printf '%s\\n' '\(Self.provided)' ;;
        esac
        """)
        let store = AppStoreStore(runner: runner)
        let config = ConfigStore(runner: runner)

        XCTAssertTrue(store.startGet(app: "a"))
        XCTAssertTrue(store.isBusy("a"))
        XCTAssertFalse(store.startGet(app: "a"), "a second get for the same app")
        XCTAssertFalse(
            store.startProvide(app: "a", fileURL: URL(fileURLWithPath: "/tmp/a.ipa"), config: config),
            "a drop while that app's get is staging into the same cache name"
        )
        XCTAssertFalse(store.isBusy("b"))

        store.stopGet(app: "a")
        await waitUntil { !store.isBusy("a") }
        XCTAssertFalse(runs(dir).contains { $0.contains("--provide-ipa") })
    }

    // MARK: M6 — Stop

    @MainActor
    func test_stop_terminates_the_cli_and_is_not_an_error() async throws {
        let (runner, dir) = try scriptRunner("""
        trap 'echo stopped > "$(dirname "$0")/terminated"; exit 143' TERM
        printf '%s\\n' '{"type":"appstore-stage","app":"a","stage":"download"}'
        sleep 30 &
        wait $!
        """)
        let store = AppStoreStore(runner: runner)

        XCTAssertTrue(store.startGet(app: "a"))
        await waitUntil { store.stages["a"] == .download }
        store.stopGet(app: "a")
        await waitUntil { !store.isBusy("a") }

        XCTAssertFalse(store.isBusy("a"))
        XCTAssertTrue(
            FileManager.default.fileExists(atPath: dir.appending(path: "terminated").path),
            "the CLI must get the SIGTERM its scoped handler cleans up on"
        )
        XCTAssertNil(store.errors["a"], "a Stop the user asked for is not a failure to report")
        XCTAssertNil(store.completions["a"])
    }

    // MARK: M8 — the version check is not re-run on every click

    @MainActor
    func test_a_fresh_answer_is_reused_and_a_stale_or_forced_one_is_not() async throws {
        let (runner, dir) = try scriptRunner("printf '%s\\n' '\(Self.version)'")
        var now = Date(timeIntervalSince1970: 1_000_000)
        let store = AppStoreStore(runner: runner, now: { now })

        await store.checkVersion(app: "a")
        await store.checkVersion(app: "a")
        XCTAssertEqual(runs(dir).count, 1, "a second click within ten minutes reuses the answer")

        now += 9 * 60
        await store.checkVersion(app: "a")
        XCTAssertEqual(runs(dir).count, 1)

        now += 2 * 60
        await store.checkVersion(app: "a")
        XCTAssertEqual(runs(dir).count, 2, "eleven minutes on, it asks again")

        await store.checkVersion(app: "a", force: true)
        XCTAssertEqual(runs(dir).count, 3, "Refresh always asks")
    }

    /// The cached answer says "cached 21.40.0". After a pin edit, or the Cache
    /// pane deleting that file, reusing it would name a file the next build
    /// will not use — so it is reused only for the same pin and cached version.
    @MainActor
    func test_a_pin_edit_or_a_changed_cache_asks_again_inside_the_window() async throws {
        let (runner, dir) = try scriptRunner("printf '%s\\n' '\(Self.version)'")
        let store = AppStoreStore(runner: runner, now: { Date(timeIntervalSince1970: 1_000_000) })
        let latest = AppStoreStore.VersionContext(pin: "latest", cachedVersion: "21.40.0")

        await store.checkVersion(app: "a", context: latest)
        await store.checkVersion(app: "a", context: latest)
        XCTAssertEqual(runs(dir).count, 1, "premise: the same context is reused")

        await store.checkVersion(app: "a", context: .init(pin: "21.9.1", cachedVersion: "21.40.0"))
        XCTAssertEqual(runs(dir).count, 2, "a pin edit asks again")

        await store.checkVersion(app: "a", context: .init(pin: "21.9.1", cachedVersion: nil))
        XCTAssertEqual(runs(dir).count, 3, "a deleted staged IPA asks again")
    }

    // MARK: a drop that cannot run says why

    @MainActor
    func test_a_refused_drop_says_why_in_that_apps_banner() async throws {
        let (runner, _) = try scriptRunner("exec sleep 30")
        let store = AppStoreStore(runner: runner)
        let config = ConfigStore(runner: runner)
        XCTAssertTrue(store.startGet(app: "a"))

        let started = store.startProvide(
            app: "a", fileURL: URL(fileURLWithPath: "/tmp/a.ipa"), config: config
        )

        XCTAssertFalse(started)
        XCTAssertEqual(
            store.errors["a"],
            "Not staged: an App Store get is running for this app. "
                + "Wait for it, or press Stop, then drop the IPA again."
        )
        XCTAssertNil(store.errors["b"])
        store.stopGet(app: "a")
        await waitUntil { !store.isBusy("a") }
    }

    @MainActor
    func test_a_failed_check_is_not_cached() async throws {
        let (runner, dir) = try scriptRunner("exit 13")
        let store = AppStoreStore(runner: runner)

        await store.checkVersion(app: "a")
        await store.checkVersion(app: "a")

        XCTAssertEqual(runs(dir).count, 2, "'couldn't check' must not stick for ten minutes")
        XCTAssertNotNil(store.versions["a"]?.error)
    }
}
