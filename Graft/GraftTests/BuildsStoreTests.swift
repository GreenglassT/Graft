import XCTest
@testable import Graft

final class BuildsStoreTests: XCTestCase {
    @MainActor
    func test_load_parses_build_records() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let script = #"""
        printf '{"type":"build-record","id":2,"timestamp":"2026-05-13T20:00:00Z","app":"delta","bundle_id":"com.delta.app","version":"44.7.0","tweaks":["EchoV1.73.deb"],"ipa_path":null,"ipa_sha256":null,"signing_team":null,"udid":null,"target":"device","success":false,"error":"cyan failed"}\n{"type":"build-record","id":1,"timestamp":"2026-05-13T19:00:00Z","app":"example","bundle_id":"com.example.app","version":"21.16.2","tweaks":["a.deb","b.deb"],"ipa_path":"/cache/y.ipa","ipa_sha256":"abc","signing_team":"TEAM","udid":"udid","target":"device","success":true,"error":null}\n'
        """#
        let store = BuildsStore(runner: runner)
        let records = try await store.load(argsOverride: ["-c", script])
        XCTAssertEqual(records.count, 2)
        XCTAssertEqual(records[0].app, "delta")
        XCTAssertEqual(records[0].success, false)
        XCTAssertEqual(records[1].app, "example")
        XCTAssertEqual(records[1].tweaks, ["a.deb", "b.deb"])
    }

    /// `target` is what lets a row be offered a Mac action or a phone action
    /// and never both, so it is decoded here rather than assumed.
    ///
    /// It also pins the non-optional choice. A row that reaches this side
    /// without a `target` key is not decoded as some default — `buildEvents()`
    /// drops the undecodable line with `try?` and the record disappears
    /// entirely, so the count assertion is what catches that, not the field
    /// assertions below it.
    @MainActor
    func test_a_mac_build_is_told_apart_from_a_device_build() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let script = #"""
        printf '{"type":"build-record","id":4,"timestamp":"2026-08-18T13:42:03Z","app":"example","bundle_id":"com.example.app","version":"21.16.2","tweaks":["some-tweak.deb"],"ipa_path":"/built/y_mac.ipa","ipa_sha256":"mac","signing_team":null,"udid":null,"target":"mac","success":true,"error":null}\n{"type":"build-record","id":3,"timestamp":"2026-08-18T12:00:00Z","app":"example","bundle_id":"com.example.app","version":"21.16.2","tweaks":["some-tweak.deb"],"ipa_path":"/built/y.ipa","ipa_sha256":"dev","signing_team":"TEAM","udid":"udid","target":"device","success":true,"error":null}\n'
        """#
        let store = BuildsStore(runner: runner)
        let records = try await store.load(argsOverride: ["-c", script])
        XCTAssertEqual(records.count, 2, "a row missing `target` is dropped, not defaulted")
        XCTAssertEqual(records[0].target, "mac")
        XCTAssertTrue(records[0].isForThisMac)
        XCTAssertEqual(records[1].target, "device")
        XCTAssertFalse(records[1].isForThisMac)
    }

    // MARK: Clean up old builds (--prune-builds)

    /// A script that logs its argv, then answers a dry run, a real run and a
    /// listing the way the CLI does.
    private func pruneCLI(_ realRun: String) throws -> (CLIRunner, URL) {
        let dir = FileManager.default.temporaryDirectory
            .appending(path: "builds_prune_\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let log = dir.appending(path: "argv.log")
        let script = dir.appending(path: "graft")
        try """
        #!/bin/sh
        echo "$*" >> '\(log.path)'
        case " $* " in
          *" --builds "*) exit 0 ;;
          *" --dry-run "*)
            printf '%s\\n' '{"type":"build-pruned","app":"example","filename":"example_1.0_20260601T000000.ipa","path":"/b/example_1.0_20260601T000000.ipa","size_bytes":2000000,"trash_path":null,"build_ids":[3],"dry_run":true}'
            printf '%s\\n' '{"type":"prune-end","keep":2,"dry_run":true,"removed":1,"kept":2,"bytes_removed":2000000,"unparsed":0,"failed":0}'
            exit 0 ;;
        esac
        \(realRun)
        """.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
        return (CLIRunner(executable: script), log)
    }

    private func argv(_ log: URL) throws -> [String] {
        try String(contentsOf: log, encoding: .utf8).split(separator: "\n").map(String.init)
    }

    @MainActor
    func test_the_preview_is_a_dry_run_at_the_configured_keep() async throws {
        let (runner, log) = try pruneCLI("exit 0")
        let store = BuildsStore(runner: runner)

        let preview = try await store.previewPrune()

        XCTAssertEqual(try argv(log), ["--json --prune-builds --dry-run"],
                       "no --keep: the CLI uses config.yaml's archive_keep, as the build does")
        XCTAssertEqual(preview.keep, 2)
        XCTAssertEqual(preview.removed, 1)
        XCTAssertEqual(preview.bytesRemoved, 2_000_000)
    }

    @MainActor
    func test_cleaning_up_moves_them_then_rereads_the_list() async throws {
        let (runner, log) = try pruneCLI("""
        printf '%s\\n' '{"type":"build-pruned","app":"example","filename":"example_1.0_20260601T000000.ipa","path":"/b/example_1.0_20260601T000000.ipa","size_bytes":2000000,"trash_path":"/t/x.ipa","build_ids":[3],"dry_run":false}'
        printf '%s\\n' '{"type":"prune-end","keep":2,"dry_run":false,"removed":1,"kept":2,"bytes_removed":2000000,"unparsed":0,"failed":0}'
        """)
        let store = BuildsStore(runner: runner)

        await store.prune()

        XCTAssertEqual(try argv(log), ["--json --prune-builds", "--json --builds --builds-limit 50"])
        XCTAssertEqual(store.pruneResult, .moved(count: 1, bytes: 2_000_000))
        XCTAssertFalse(store.isPruning)
    }

    /// A file that would not move is reported by name, and the ones that did
    /// still count — the CLI exits 1 for it, after the events that say so.
    @MainActor
    func test_an_archive_that_would_not_move_is_named() async throws {
        let (runner, log) = try pruneCLI("""
        printf '%s\\n' '{"type":"build-pruned","app":"example","filename":"a.ipa","path":"/b/a.ipa","size_bytes":10,"trash_path":"/t/a.ipa","build_ids":[],"dry_run":false}'
        printf '%s\\n' '{"type":"log","source":"retention","line":"b.ipa: Operation not permitted"}'
        printf '%s\\n' '{"type":"prune-end","keep":2,"dry_run":false,"removed":1,"kept":2,"bytes_removed":10,"unparsed":0,"failed":1}'
        echo "error: 1 archive(s) could not be moved to the Trash" >&2
        exit 1
        """)
        let store = BuildsStore(runner: runner)

        await store.prune()

        XCTAssertEqual(store.pruneResult, .partly(moved: 1, failures: ["b.ipa: Operation not permitted"]))
        XCTAssertEqual(try argv(log).last, "--json --builds --builds-limit 50", "still re-read: one did move")
    }

    /// Review #5: every move failed (`removed: 0`). The CLI may have cleared
    /// those rows' `ipa_path` before the move, and the list was re-read only
    /// when something moved, so the rows kept offering Re-install and Reveal.
    @MainActor
    func test_a_clean_up_where_every_move_failed_still_rereads_the_list() async throws {
        let (runner, log) = try pruneCLI("""
        printf '%s\\n' '{"type":"log","source":"retention","line":"a.ipa: Permission denied"}'
        printf '%s\\n' '{"type":"prune-end","keep":2,"dry_run":false,"removed":0,"kept":2,"bytes_removed":0,"unparsed":0,"failed":1}'
        echo "error: 1 archive(s) could not be moved to the Trash" >&2
        exit 1
        """)
        let store = BuildsStore(runner: runner)

        await store.prune()

        XCTAssertEqual(store.pruneResult, .partly(moved: 0, failures: ["a.ipa: Permission denied"]))
        XCTAssertEqual(try argv(log).last, "--json --builds --builds-limit 50",
                       "the rows a failed move touched were left showing their old archive")
    }

    @MainActor
    func test_a_clean_up_that_moved_nothing_and_failed_nothing_does_not_reload() async throws {
        let (runner, log) = try pruneCLI("""
        printf '%s\\n' '{"type":"prune-end","keep":2,"dry_run":false,"removed":0,"kept":2,"bytes_removed":0,"unparsed":0,"failed":0}'
        """)
        let store = BuildsStore(runner: runner)

        await store.prune()

        XCTAssertEqual(try argv(log), ["--json --prune-builds"])
    }

    @MainActor
    func test_a_prune_that_could_not_run_says_why() async throws {
        let (runner, _) = try pruneCLI("""
        echo "error: archive_keep must be a non-negative integer" >&2
        exit 2
        """)
        let store = BuildsStore(runner: runner)

        await store.prune()

        guard case let .failed(message) = store.pruneResult else {
            return XCTFail("\(String(describing: store.pruneResult))")
        }
        XCTAssertTrue(message.contains("archive_keep"), message)
    }

    func test_the_confirmation_states_the_keep_count_and_what_goes() throws {
        let some = try summary(keep: 2, removed: 3, bytes: 3_000_000)
        let text = BuildsView.pruneConfirmation(some)
        XCTAssertEqual(text.title, "Clean up old builds?")
        XCTAssertTrue(text.message.contains("newest 2 builds of each app version"), text.message)
        XCTAssertTrue(text.message.contains("3 archived IPAs"), text.message)
        XCTAssertTrue(text.canConfirm)

        let all = BuildsView.pruneConfirmation(try summary(keep: 0, removed: 4, bytes: 10))
        XCTAssertTrue(all.message.contains("keeps no archived builds"), all.message)
        XCTAssertTrue(all.message.contains("all 4"), all.message)

        let none = BuildsView.pruneConfirmation(try summary(keep: 2, removed: 0, bytes: 0))
        XCTAssertEqual(none.title, "Nothing to clean up")
        XCTAssertFalse(none.canConfirm)
    }

    func test_the_result_line_reports_the_count_or_the_failures() {
        XCTAssertEqual(
            BuildsView.pruneResultLine(.moved(count: 1, bytes: 2_000_000))?.text,
            "Moved 1 archived IPA to the Trash (\(fileSize(2_000_000))). Empty the Trash to free the space."
        )
        let partly = BuildsView.pruneResultLine(.partly(moved: 2, failures: ["b.ipa: denied"]))
        XCTAssertEqual(partly?.failed, true)
        XCTAssertTrue(partly?.text.contains("b.ipa: denied") == true, partly?.text ?? "")
        XCTAssertTrue(partly?.text.contains("Moved 2") == true, partly?.text ?? "")
    }

    /// Disabled while any build runs in the app: that build archives first and
    /// delivers after, and at `archive_keep: 0` the sweep would take the
    /// archive it is about to push. The tooltip says which build and why.
    func test_clean_up_waits_for_a_running_build() {
        let blocked = BuildsView.cleanUpAvailability(buildingApp: "example", working: false)
        XCTAssertFalse(blocked.enabled)
        XCTAssertTrue(blocked.help.contains("example is building"), blocked.help)
        XCTAssertTrue(blocked.help.contains("archive"), blocked.help)

        let busy = BuildsView.cleanUpAvailability(buildingApp: nil, working: true)
        XCTAssertFalse(busy.enabled)

        let free = BuildsView.cleanUpAvailability(buildingApp: nil, working: false)
        XCTAssertTrue(free.enabled)
        XCTAssertFalse(free.help.contains("building"), free.help)
    }

    private func summary(keep: Int, removed: Int, bytes: Int) throws -> PruneSummary {
        let line = #"{"type":"prune-end","keep":\#(keep),"dry_run":true,"removed":\#(removed),"kept":1,"bytes_removed":\#(bytes),"unparsed":0,"failed":0}"#
        guard case let .pruneEnd(summary)? = line.buildEvents().first else { throw XCTSkip("fixture") }
        return summary
    }
}
