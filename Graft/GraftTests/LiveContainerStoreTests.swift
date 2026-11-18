import XCTest
@testable import Graft

/// `LiveContainerStore` and the `lc-*` events it decodes.
///
/// The CLI is `/bin/sh -c <script>` printing the lines the real CLI prints —
/// the pattern the other store tests use. The argv the store builds is pinned
/// separately through its static builders, since an override replaces it.
final class LiveContainerStoreTests: XCTestCase {
    private static let ipad = "00008030-0000000000000002"

    /// Real `--lc-status --json` output for the iPad, 2026-11-17, shortened to
    /// two instances.
    private static let ipadStatus = #"""
    {"type":"lc-status","udid":"00008030-0000000000000002","reachable":true,"instances":[{"bundle_id":"com.kdt.livecontainer.ABCDE12345","version":"3.8.14","role":"primary","wildcard_signed":false},{"bundle_id":"com.kdt.LiveContainer2.ABCDE12345","version":"3.8.14","role":"2","wildcard_signed":false}],"managed_by":"unknown","latest":{"version":"3.8.14","channel":"nightly"},"update_available":false,"sources":{"stable":"https://github.com/LiveContainer/LiveContainer/releases/download/1.0/apps.json","nightly":"https://github.com/LiveContainer/LiveContainer/releases/download/nightly/apps_nightly.json"}}
    """#

    private static let iphoneStatus = #"""
    {"type":"lc-status","udid":"00008030-0000000000000001","reachable":true,"instances":[{"bundle_id":"com.kdt.livecontainer.ABCDE12345","version":"3.7.2","role":"primary","wildcard_signed":false}],"managed_by":"altstore","latest":{"version":"3.8.14","channel":"nightly"},"update_available":true,"sources":{"stable":"s","nightly":"n"}}
    """#

    private func sh() -> CLIRunner { CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")) }

    private func printing(_ lines: [String], exit code: Int32 = 0, stderr: String = "") -> [String] {
        let body = lines.map { "printf '%s\\n' '\($0)'" }.joined(separator: "; ")
        let err = stderr.isEmpty ? "" : "; printf '%s' '\(stderr)' >&2"
        return ["-c", "\(body)\(err); exit \(code)"]
    }

    // MARK: decoding

    func test_status_decodes() throws {
        let events = Self.ipadStatus.buildEvents()
        guard case let .lcStatus(status)? = events.first else {
            return XCTFail("not decoded as lc-status: \(events)")
        }
        XCTAssertEqual(status.udid, Self.ipad)
        XCTAssertEqual(status.managedBy, .unknown)
        XCTAssertEqual(status.instances.map(\.displayName), ["LiveContainer", "LiveContainer2"])
        XCTAssertEqual(status.latest, .init(version: "3.8.14", channel: "nightly"))
        XCTAssertFalse(status.updateAvailable)
        XCTAssertNil(status.error)
    }

    func test_an_unreachable_status_decodes() throws {
        let line = #"{"type":"lc-status","udid":"u","reachable":false,"error":"Device not found","instances":[],"managed_by":"unknown","latest":null,"update_available":false,"sources":{}}"#
        guard case let .lcStatus(status)? = line.buildEvents().first else {
            return XCTFail("not decoded")
        }
        XCTAssertFalse(status.reachable)
        XCTAssertEqual(status.error, "Device not found")
        XCTAssertNil(status.latest)
    }

    func test_every_install_event_decodes() {
        let lines = [
            #"{"type":"lc-install-progress","udid":"u","instance":null,"step":"fetch","detail":"LiveContainer (nightly)"}"#,
            #"{"type":"lc-install-waiting","udid":"u","instance":1,"bundle_id":"b","message":"close LiveContainer on the device to finish"}"#,
            #"{"type":"lc-installed","udid":"u","bundle_id":"b","instance":1,"version":"3.8.14","channel":"nightly"}"#,
            #"{"type":"lc-cert-pushed","udid":"u","bundle_id":"b","path":"Documents/AppleDevelopment.p12"}"#,
            #"{"type":"lc-cleaned","udid":"u","bundle_id":"b","removed":["AppleDevelopment.p12"],"kept":2}"#,
        ]
        let events = lines.joined(separator: "\n").buildEvents()
        XCTAssertEqual(events.count, 5)
        var seen: [String] = []
        for event in events {
            switch event {
            case let .lcInstallProgress(r): seen.append("progress:\(r.instance.map(String.init) ?? "nil")")
            case let .lcInstallWaiting(r): seen.append("waiting:\(r.instance)")
            case let .lcInstalled(r): seen.append("installed:\(r.version)")
            case let .lcCertPushed(r): seen.append("pushed:\(r.path)")
            case let .lcCleaned(r): seen.append("cleaned:\(r.kept)")
            default: seen.append("other")
            }
        }
        XCTAssertEqual(seen, [
            "progress:nil", "waiting:1", "installed:3.8.14",
            "pushed:Documents/AppleDevelopment.p12", "cleaned:2",
        ])
    }

    // MARK: what the section promises before an install

    private func status(managedBy: String, wildcard: [Bool]) throws -> LCStatusRecord {
        let instances = wildcard.enumerated().map { i, w in
            #"{"bundle_id":"com.kdt.LiveContainer\#(i + 2).T","version":"3.7.2","role":"\#(i + 2)","wildcard_signed":\#(w)}"#
        }.joined(separator: ",")
        let line = #"{"type":"lc-status","udid":"U","reachable":true,"instances":[\#(instances)],"managed_by":"\#(managedBy)","latest":null,"update_available":false,"sources":{}}"#
        guard case let .lcStatus(s)? = line.buildEvents().first else {
            throw XCTSkip("fixture did not decode")
        }
        return s
    }

    /// The old line said the replacement keeps the apps inside, full stop.
    /// It does keep the *data* on an in-place upgrade — but apps signed under
    /// another certificate then need re-signing, and that must be said.
    func test_a_copy_installed_elsewhere_is_told_about_re_signing_not_promised_untouched() throws {
        let notes = DeviceLiveContainerSection.installNotes(
            for: try status(managedBy: "unknown", wildcard: [false])
        )

        XCTAssertEqual(notes.count, 1)
        XCTAssertTrue(notes[0].contains("keeps its data"), notes[0])
        XCTAssertTrue(notes[0].contains("Force Sign"), notes[0])
        XCTAssertFalse(notes.joined().contains("apps inside it are kept"))
        XCTAssertFalse(notes.joined().contains("delete"), "no wildcard copy here")
    }

    func test_a_wildcard_signed_copy_is_told_to_delete_first_and_lose_its_data() throws {
        let notes = DeviceLiveContainerSection.installNotes(
            for: try status(managedBy: "unknown", wildcard: [false, true])
        )

        let deleteLine = try XCTUnwrap(notes.first { $0.contains("delete") })
        XCTAssertTrue(deleteLine.contains("LiveContainer3"), deleteLine)
        XCTAssertFalse(deleteLine.contains("LiveContainer2"), deleteLine)
        XCTAssertTrue(deleteLine.contains("loses its data"), deleteLine)
    }

    /// Our own install, same certificate: nothing to re-sign, nothing to say.
    func test_our_own_copy_needs_no_caveat() throws {
        XCTAssertEqual(
            DeviceLiveContainerSection.installNotes(for: try status(managedBy: "graft", wildcard: [false])),
            []
        )
    }

    // MARK: review fixes — cancellation, supersession, Stop, late events

    private func slow(_ seconds: Int, then lines: [String] = []) -> [String] {
        let body = lines.map { "printf '%s\\n' '\($0)'" }.joined(separator: "; ")
        return ["-c", "sleep \(seconds)" + (body.isEmpty ? "" : "; \(body)")]
    }

    /// The `.task(id:)` is cancelled when the user picks another device; the
    /// SIGTERMed CLI comes back as a non-zero exit, and that is not a failure.
    @MainActor
    func test_a_cancelled_load_records_nothing_and_a_new_one_proceeds() async throws {
        let store = LiveContainerStore(runner: sh())
        let task = Task { await store.load(udid: Self.ipad, argsOverride: slow(10)) }
        try await Task.sleep(nanoseconds: 300_000_000)
        task.cancel()
        await task.value

        XCTAssertNil(store.loadErrors[Self.ipad], "a cancelled load is not a failed one")
        XCTAssertFalse(store.hasLoaded.contains(Self.ipad))
        XCTAssertFalse(store.loading.contains(Self.ipad))

        await store.load(udid: Self.ipad, argsOverride: printing([Self.ipadStatus]))
        XCTAssertEqual(store.status[Self.ipad]?.instances.count, 2)
    }

    /// A→B→A: the second A load must run, and the first must not overwrite it.
    @MainActor
    func test_a_newer_load_for_the_same_device_wins_over_one_still_in_flight() async throws {
        let store = LiveContainerStore(runner: sh())
        let first = Task {
            await store.load(udid: Self.ipad, argsOverride: slow(1, then: [Self.iphoneStatus]))
        }
        try await Task.sleep(nanoseconds: 100_000_000)

        await store.load(udid: Self.ipad, argsOverride: printing([Self.ipadStatus]))
        XCTAssertEqual(store.status[Self.ipad]?.managedBy, .unknown, "the newer load ran")
        await first.value

        XCTAssertEqual(store.status[Self.ipad]?.managedBy, .unknown, "the older one was ignored")
        XCTAssertFalse(store.loading.contains(Self.ipad))
    }

    @MainActor
    func test_a_cancelled_action_is_reported_as_stopped_and_frees_the_buttons() async throws {
        let store = LiveContainerStore(runner: sh())
        let task = Task { await store.pushCert(udid: "U", argsOverride: slow(30)) }
        try await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertTrue(store.isRunning)
        XCTAssertEqual(store.busyElsewhere(than: "other"), "U")
        XCTAssertNil(store.busyElsewhere(than: "U"))

        let started = Date()
        task.cancel()
        await task.value

        XCTAssertLessThan(Date().timeIntervalSince(started), 10, "the CLI was stopped, not waited out")
        XCTAssertFalse(store.isRunning)
        XCTAssertEqual(store.run(for: "U")?.stopped, true)
        XCTAssertEqual(store.run(for: "U")?.error, "Stopped.")
    }

    @MainActor
    func test_stop_cancels_the_action_the_store_started() async {
        let store = LiveContainerStore(runner: sh())
        store.startClean(udid: "U")
        let action = store.currentAction
        store.stop()

        XCTAssertEqual(action?.isCancelled, true)
        await action?.value
        XCTAssertFalse(store.isRunning)
    }

    /// Two clicks in one main-actor turn: the first task has not started, so
    /// `run` is not set yet and `isRunning` says no. Without a guard of its
    /// own, `launch` made a second task, `actionTask` pointed at it (it
    /// returns at once, finding the first running), and Stop cancelled that
    /// one instead of the CLI that was actually running.
    @MainActor
    func test_a_double_click_starts_one_action_and_stop_cancels_that_one() async {
        let store = LiveContainerStore(runner: sh())

        store.startClean(udid: "U")
        let first = store.currentAction
        store.startClean(udid: "U")

        XCTAssertNotNil(first)
        XCTAssertEqual(store.currentAction, first, "the second click must not replace the first")
        store.stop()
        XCTAssertEqual(first?.isCancelled, true)
        await first?.value
        XCTAssertFalse(store.isRunning)
    }

    /// A partial install changed the device too.
    @MainActor
    func test_a_failed_install_still_re_reads_the_device() async {
        let store = LiveContainerStore(runner: sh())

        await store.install(udid: "U", instances: [1], channel: nil,
                            argsOverride: printing([], exit: 8, stderr: "failed"))

        XCTAssertTrue(store.hasLoaded.contains("U"), "the status load ran after the failure")
    }

    @MainActor
    func test_an_event_after_the_run_finished_is_ignored() async {
        let store = LiveContainerStore(runner: sh())
        await store.pushCert(udid: "U", argsOverride: printing([]))
        XCTAssertEqual(store.run(for: "U")?.finished, true)

        let late = #"{"type":"lc-install-waiting","udid":"U","instance":1,"bundle_id":"b","message":"close LiveContainer on the device to finish"}"#
        for event in late.buildEvents() { store.apply(event) }

        XCTAssertNil(store.run(for: "U")?.waitingMessage)
    }

    func test_the_certificate_note_decodes() {
        let line = #"{"type":"lc-cert-pushed","udid":"u","bundle_id":"b","path":"Documents/AppleDevelopment.p12","note":"stays until removed"}"#
        guard case let .lcCertPushed(r)? = line.buildEvents().first else {
            return XCTFail("not decoded")
        }
        XCTAssertEqual(r.note, "stays until removed")
    }

    func test_remove_certificate_argv() {
        XCTAssertEqual(
            LiveContainerStore.removeCertArgs(udid: "U"),
            ["--json", "--lc-clean", "--cert-only", "--device", "U"]
        )
    }

    // MARK: argv

    func test_the_argv_names_the_device_every_time() {
        XCTAssertEqual(LiveContainerStore.statusArgs(udid: "U"), ["--json", "--lc-status", "--device", "U"])
        XCTAssertEqual(LiveContainerStore.pushCertArgs(udid: "U"), ["--json", "--lc-push-cert", "--device", "U"])
        XCTAssertEqual(LiveContainerStore.cleanArgs(udid: "U"), ["--json", "--lc-clean", "--device", "U"])
    }

    func test_install_argv_is_sorted_and_omits_a_default_channel() {
        XCTAssertEqual(
            LiveContainerStore.installArgs(udid: "U", instances: [3, 1], channel: nil),
            ["--json", "--lc-install", "--device", "U", "--instances", "1,3"]
        )
        XCTAssertEqual(
            LiveContainerStore.installArgs(udid: "U", instances: [2], channel: "stable"),
            ["--json", "--lc-install", "--device", "U", "--instances", "2", "--channel", "stable"]
        )
    }

    // MARK: status

    @MainActor
    func test_load_stores_the_status_and_marks_the_device_loaded() async {
        let store = LiveContainerStore(runner: sh())
        XCTAssertFalse(store.hasLoaded.contains(Self.ipad))

        await store.load(udid: Self.ipad, argsOverride: printing([Self.ipadStatus]))

        XCTAssertTrue(store.hasLoaded.contains(Self.ipad))
        XCTAssertEqual(store.status[Self.ipad]?.instances.count, 2)
        XCTAssertTrue(store.canInstall(udid: Self.ipad))
    }

    /// A failed load is still a load: "Checking…" forever is the
    /// `isEmpty`-as-load-state defect in a new costume.
    @MainActor
    func test_a_failed_load_still_counts_as_loaded_and_says_why() async {
        let store = LiveContainerStore(runner: sh())

        await store.load(udid: "U", argsOverride: printing([], exit: 3, stderr: "boom"))

        XCTAssertTrue(store.hasLoaded.contains("U"))
        XCTAssertNil(store.status["U"])
        XCTAssertNotNil(store.loadErrors["U"])
        XCTAssertFalse(store.canInstall(udid: "U"))
    }

    @MainActor
    func test_an_altstore_device_cannot_be_installed_to() async {
        let store = LiveContainerStore(runner: sh())

        await store.load(udid: "00008030-0000000000000001", argsOverride: printing([Self.iphoneStatus]))

        XCTAssertEqual(store.status["00008030-0000000000000001"]?.managedBy, .altstore)
        XCTAssertFalse(store.canInstall(udid: "00008030-0000000000000001"))
    }

    // MARK: actions

    @MainActor
    func test_install_collects_progress_and_results_and_clears_the_wait() async {
        let store = LiveContainerStore(runner: sh())
        let lines = [
            #"{"type":"lc-install-progress","udid":"U","instance":1,"step":"install","detail":"b"}"#,
            #"{"type":"lc-install-waiting","udid":"U","instance":1,"bundle_id":"b","message":"close LiveContainer on the device to finish"}"#,
            #"{"type":"lc-installed","udid":"U","bundle_id":"b","instance":1,"version":"3.8.14","channel":"nightly"}"#,
            #"{"type":"log","source":"graft","line":"LiveContainer.app: T.b keychain-groups=128"}"#,
        ]

        await store.install(udid: "U", instances: [1], channel: nil, argsOverride: printing(lines))

        let run = try? XCTUnwrap(store.run(for: "U"))
        XCTAssertEqual(run?.action, .install)
        XCTAssertEqual(run?.finished, true)
        XCTAssertEqual(run?.progress.map(\.step), ["install"])
        XCTAssertEqual(run?.installed.map(\.bundleId), ["b"])
        XCTAssertNil(run?.waitingMessage, "the wait is over once it installed")
        XCTAssertEqual(run?.notes, ["LiveContainer.app: T.b keychain-groups=128"])
        XCTAssertNil(run?.error)
        XCTAssertFalse(store.isRunning)
        XCTAssertNil(store.run(for: "other"), "a run belongs to its device")
    }

    /// The CLI keeps waiting after it says so; if the run ends without an
    /// install (killed, failed) the notice must not outlive it.
    @MainActor
    func test_a_waiting_notice_does_not_outlive_a_failed_run() async {
        let store = LiveContainerStore(runner: sh())
        let waiting = #"{"type":"lc-install-waiting","udid":"U","instance":1,"bundle_id":"b","message":"close LiveContainer on the device to finish"}"#

        await store.install(
            udid: "U", instances: [1], channel: nil,
            argsOverride: printing([waiting], exit: 8, stderr: "installing b failed")
        )

        XCTAssertNil(store.run(for: "U")?.waitingMessage)
        XCTAssertEqual(store.run(for: "U")?.error?.contains("installing b failed"), true)
    }

    @MainActor
    func test_push_and_clean_report_what_they_did() async {
        let store = LiveContainerStore(runner: sh())

        await store.pushCert(udid: "U", argsOverride: printing([
            #"{"type":"lc-cert-pushed","udid":"U","bundle_id":"b","path":"Documents/AppleDevelopment.p12"}"#,
        ]))
        XCTAssertEqual(store.run(for: "U")?.pushed?.path, "Documents/AppleDevelopment.p12")

        await store.clean(udid: "U", argsOverride: printing([
            #"{"type":"lc-cleaned","udid":"U","bundle_id":"b","removed":["example.ipa"],"kept":3}"#,
        ]))
        XCTAssertEqual(store.run(for: "U")?.action, .clean)
        XCTAssertEqual(store.run(for: "U")?.cleaned?.removed, ["example.ipa"])
        XCTAssertNil(store.run(for: "U")?.pushed, "a new run starts clean")
    }
}
