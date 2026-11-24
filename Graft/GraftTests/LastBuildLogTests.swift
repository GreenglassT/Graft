import XCTest
@testable import Graft

/// The app pane's "Last build log" panel, before this session has built the
/// app.
///
/// It said "No build yet for this app." whenever it had no lines — directly
/// under a Last build tile reading "21h ago". The panel only ever holds this
/// session's output, and a build run from Terminal without `--json` saves no
/// log at all (`log_path` is null on its `build-record`), so the honest
/// sentence depends on the last build the CLI recorded.
final class LastBuildLogTests: XCTestCase {
    private func record(logPath: String?) throws -> BuildRecord {
        let path = logPath.map { "\"\($0)\"" } ?? "null"
        let line = "{\"type\":\"build-record\",\"id\":7,\"timestamp\":\"2026-11-17T10:00:00Z\","
            + "\"app\":\"example\",\"bundle_id\":\"com.example.app\",\"version\":\"1.0\","
            + "\"tweaks\":[],\"ipa_path\":null,\"ipa_sha256\":null,\"log_path\":\(path),"
            + "\"signing_team\":null,\"udid\":null,\"target\":\"device\",\"success\":true,"
            + "\"error\":null}"
        guard case let .buildRecord(r)? = line.buildEvents().first else {
            throw XCTSkip("fixture did not decode: \(line)")
        }
        return r
    }

    func test_an_app_never_built_says_so() {
        XCTAssertEqual(AppDetailView.emptyLogMessage(lastBuild: nil), "No build yet for this app.")
    }

    func test_a_terminal_build_says_its_log_was_not_saved() throws {
        XCTAssertEqual(
            AppDetailView.emptyLogMessage(lastBuild: try record(logPath: nil)),
            "The last build ran from Terminal, so its log wasn't saved. Builds started here keep theirs."
        )
    }

    /// The pane puts that sentence in the panel, for the record its own
    /// `--builds` read came back with. Read off the window's accessibility
    /// tree, which skips when macOS is not serving it (see `AccessibilityTree`).
    @MainActor
    func test_the_app_pane_shows_the_terminal_sentence_for_a_logless_build() async throws {
        try AccessibilityTree.requireServed()
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-lastlog-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let build = "{\"type\":\"build-record\",\"id\":7,\"timestamp\":\"2026-11-17T10:00:00Z\","
            + "\"app\":\"example\",\"bundle_id\":\"com.example.app\",\"version\":\"1.0\","
            + "\"tweaks\":[],\"ipa_path\":null,\"ipa_sha256\":null,\"log_path\":null,"
            + "\"signing_team\":null,\"udid\":null,\"target\":\"device\",\"success\":true,"
            + "\"error\":null}"
        let cli = dir.appendingPathComponent("graft")
        try """
        #!/bin/sh
        case " $* " in
          *" --builds "*) printf '%s\\n' '\(build)' ;;
        esac
        exit 0
        """.write(to: cli, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: cli.path)
        let runner = CLIRunner(executable: cli)

        let appLine = #"{"type":"app","name":"example","bundle_id":"com.example.app","preferred_version":"latest","tweaks":[],"display_name":"Example+","delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}"#
        let sh = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let config = ConfigStore(runner: sh, cliArgs: ["-c", "printf '%s\\n' '\(appLine)'"], notify: { _ in })
        let apps = try await config.loadApps()
        let app = try XCTUnwrap(apps.first)
        let pane = AppDetailView(
            app: app, runner: runner, store: config, artwork: ArtworkStore(runner: sh),
            devices: DevicesStore(runner: sh), tweaksStore: TweaksStore(runner: sh),
            staged: StagedIPAStore(runner: sh), appStore: AppStoreStore(runner: sh, shell: NoAdminShell()),
            liveContainer: LiveContainerStore(runner: sh),
            settings: SettingsStore(runner: sh), showSettings: {}
        )
        let hosted = hosted(pane, size: NSSize(width: 900, height: 1200))
        // Awaited, not pumped: the pane's `.task` reads `--builds` and resumes
        // on the main actor, which a run-loop pump inside this async test
        // would hold off (`WindowLayoutTests.laidOut` measured the same).
        try await Task.sleep(nanoseconds: 1_000_000_000)
        settle(hosted, seconds: 0.2)

        let shown = try AccessibilityTree.texts(in: hosted.window)
        XCTAssertTrue(
            shown.contains(AppDetailView.emptyLogMessage(lastBuild: try record(logPath: nil))),
            "\(shown)"
        )
        XCTAssertFalse(shown.contains("No build yet for this app."))
    }

    /// A build with a saved log is not "no build" either: its log is one
    /// click away, in Builds.
    func test_a_build_with_a_saved_log_points_at_it() throws {
        let message = AppDetailView.emptyLogMessage(
            lastBuild: try record(logPath: "/x/state/graft/logs/a.jsonl")
        )
        XCTAssertNotEqual(message, "No build yet for this app.")
        XCTAssertTrue(message.contains("Builds"), message)
    }
}

/// What a Stop adds to the log (`AppDetailView.stopTranscript`), as values.
///
/// Under `--json` the CLI sends its build narration to stderr, and a stop it
/// reports prints no `error:` block. Dumping stderr then put "✗ exit 143"
/// under "Stopped during cyan." and replayed every stage line the panel had
/// already shown.
@MainActor
final class StopTranscriptTests: XCTestCase {
    private static let narration = "→ stage 1: fetch\n→ stage 2: cyan\n  cyan: injecting\n"

    private static func events(stopped: Bool) -> [BuildEvent] {
        let end = stopped
            ? #"{"type":"build-end","app":"example","ok":false,"stopped":true,"error":null,"elapsed_seconds":2.0}"#
            : #"{"type":"build-end","app":"example","ok":false,"error":"cyan failed"}"#
        return """
        {"type":"build-start","app":"example","bundle_id":"com.example.app","delivery":"livecontainer"}
        {"type":"stage","name":"cyan","status":"start"}
        \(end)
        """.buildEvents()
    }

    func test_a_stop_the_cli_reported_does_not_replay_its_narration() {
        let result = CLIRunner.RunResult(
            exitCode: 143, stdout: "", stderr: Self.narration, events: Self.events(stopped: true)
        )
        let lines = AppDetailView.stopTranscript(stage: "cyan", aftermath: nil, result: result)
        XCTAssertEqual(lines.map(\.text), ["Stopped during cyan."])
    }

    func test_the_aftermath_still_follows_a_reported_stop() {
        let result = CLIRunner.RunResult(
            exitCode: 143, stdout: "", stderr: Self.narration, events: Self.events(stopped: true)
        )
        let lines = AppDetailView.stopTranscript(
            stage: "lc-push", aftermath: "A partial .ipa may be in LiveContainer.", result: result
        )
        XCTAssertEqual(lines.map(\.text), [
            "Stopped during lc-push.", "A partial .ipa may be in LiveContainer.",
        ])
    }

    /// Control: a build that failed before the kill landed reports a failure,
    /// not a stop, and its last words are still the explanation.
    func test_a_failure_that_beat_the_stop_still_reaches_the_log() {
        let result = CLIRunner.RunResult(
            exitCode: 1, stdout: "",
            stderr: "error: cyan failed\n  hint: check the .deb\n", events: Self.events(stopped: false)
        )
        let lines = AppDetailView.stopTranscript(stage: "cyan", aftermath: nil, result: result)
        XCTAssertEqual(lines.map(\.text), ["Stopped during cyan.", "✗ cyan failed", "  hint: check the .deb"])
    }

    /// Control: a CLI killed before it could report anything — SIGKILL after
    /// the grace, or one too old for C-STOP — still shows what it said.
    func test_a_stop_the_cli_never_reported_still_shows_its_stderr() {
        let result = CLIRunner.RunResult(exitCode: 137, stdout: "", stderr: Self.narration, events: [])
        let lines = AppDetailView.stopTranscript(stage: nil, aftermath: nil, result: result)
        XCTAssertEqual(lines.first?.text, "Stopped.")
        XCTAssertTrue(lines.contains { $0.text == "✗ exit 137" }, "\(lines.map(\.text))")
    }
}

/// C-STOP through the pane: a build the CLI reports stopped, from a signal this
/// app did not send (a `kill` in Terminal), and which therefore arrives as a
/// non-zero exit. Before, that was "Last build failed / 143" and a red Retry.
@MainActor
final class StoppedBuildPaneTests: XCTestCase {
    private func pane(exitingAfterStop code: Int) async throws -> (Hosted, ConfigStore) {
        try AccessibilityTree.requireServed()
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-stopped-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        // No history row for a stopped build (C-STOP), so `--builds` is empty.
        let cli = dir.appendingPathComponent("graft")
        try """
        #!/bin/sh
        case " $* " in
          *" --builds "*) exit 0 ;;
        esac
        printf '%s\\n' '{"type":"build-start","app":"example","bundle_id":"com.example.app","delivery":"livecontainer"}'
        printf '%s\\n' '{"type":"stage","name":"cyan","status":"start"}'
        printf '%s\\n' '{"type":"build-end","app":"example","ok":false,"stopped":true,"error":null,"elapsed_seconds":1.0}'
        echo 'stopped' >&2
        exit \(code)
        """.write(to: cli, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: cli.path)

        let appLine = #"{"type":"app","name":"example","bundle_id":"com.example.app","preferred_version":"latest","tweaks":[],"display_name":"Example+","delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}"#
        let sh = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let config = ConfigStore(runner: sh, cliArgs: ["-c", "printf '%s\\n' '\(appLine)'"], notify: { _ in })
        config.postNote = { _ in }
        let apps = try await config.loadApps()
        let app = try XCTUnwrap(apps.first)
        let view = AppDetailView(
            app: app, runner: CLIRunner(executable: cli), store: config,
            artwork: ArtworkStore(runner: sh), devices: DevicesStore(runner: sh),
            tweaksStore: TweaksStore(runner: sh), staged: StagedIPAStore(runner: sh),
            appStore: AppStoreStore(runner: sh, shell: NoAdminShell()), liveContainer: LiveContainerStore(runner: sh),
            settings: SettingsStore(runner: sh),
            showSettings: {}
        )
        let box = hosted(view, size: NSSize(width: 1000, height: 1000))
        try await Task.sleep(nanoseconds: 300_000_000)
        settle(box, seconds: 0.2)
        return (box, config)
    }

    private func runBuild(_ box: Hosted, _ config: ConfigStore) async throws -> [AccessibilityTree.Node] {
        try clickButton(named: "Build & Install", in: box.window)
        // The slot is claimed synchronously on the click, so this is the
        // check that the click reached the button at all.
        XCTAssertEqual(config.buildingApp, "example", "premise: the click started a build")
        var waited = 0
        while config.buildingApp != nil || waited == 0, waited < 50 {
            try await Task.sleep(nanoseconds: 100_000_000)
            waited += 1
        }
        XCTAssertNil(config.buildingApp, "premise: the build ended")
        try await Task.sleep(nanoseconds: 300_000_000)
        settle(box, seconds: 0.2)
        return try AccessibilityTree.nodes(in: box.window)
    }

    /// The value under "Last build", read off the tree. The meta strip's four
    /// labels come out first and its four values after them, in the same
    /// order, so the value is as far past the labels as the label is into them.
    private func lastBuildCell(_ nodes: [AccessibilityTree.Node]) -> String? {
        let texts = nodes.filter { $0.role == kAXStaticTextRole }.compactMap { $0.value as? String }
        let labels = ["DELIVERY STATE", "TWEAKS", "PROFILE", "LAST BUILD"]
        guard let first = texts.firstIndex(where: { $0.uppercased() == labels[0] }),
              first + 2 * labels.count <= texts.count,
              texts[first..<first + labels.count].map({ $0.uppercased() }) == labels
        else { return nil }
        return texts[first + labels.count + 3]
    }

    /// A real Stop cancels the build's Task, and the pane then re-read the
    /// newest history row inside that canceled Task: `runOnce` killed its own
    /// child at once, the read came back nil, and "Last build" went from
    /// "just now" to "—" over a history the stop had not touched. Seen on a
    /// real Stop from the app.
    func test_a_real_stop_keeps_the_last_build_it_did_not_replace() async throws {
        try AccessibilityTree.requireServed()
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-stopped-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let cli = dir.appendingPathComponent("graft")
        try """
        #!/bin/sh
        case " $* " in
          *" --builds "*)
            printf '%s\\n' '{"type":"build-record","id":7,"timestamp":"2026-08-18T12:00:00Z","app":"example","bundle_id":"com.example.app","version":"1.0","tweaks":[],"ipa_path":null,"ipa_sha256":null,"signing_team":null,"udid":null,"target":"device","success":true,"error":null}'
            exit 0 ;;
        esac
        trap 'printf "%s\\n" "{\\"type\\":\\"build-end\\",\\"app\\":\\"example\\",\\"ok\\":false,\\"stopped\\":true,\\"error\\":null,\\"elapsed_seconds\\":1.0}"; exit 143' TERM
        printf '%s\\n' '{"type":"build-start","app":"example","bundle_id":"com.example.app","delivery":"livecontainer"}'
        printf '%s\\n' '{"type":"stage","name":"cyan","status":"start"}'
        while :; do sleep 0.1; done
        """.write(to: cli, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: cli.path)

        let appLine = #"{"type":"app","name":"example","bundle_id":"com.example.app","preferred_version":"latest","tweaks":[],"display_name":"Example+","delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}"#
        let sh = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let config = ConfigStore(runner: sh, cliArgs: ["-c", "printf '%s\\n' '\(appLine)'"], notify: { _ in })
        config.postNote = { _ in }
        let apps = try await config.loadApps()
        let app = try XCTUnwrap(apps.first)
        let view = AppDetailView(
            app: app, runner: CLIRunner(executable: cli), store: config,
            artwork: ArtworkStore(runner: sh), devices: DevicesStore(runner: sh),
            tweaksStore: TweaksStore(runner: sh), staged: StagedIPAStore(runner: sh),
            appStore: AppStoreStore(runner: sh, shell: NoAdminShell()), liveContainer: LiveContainerStore(runner: sh),
            settings: SettingsStore(runner: sh),
            showSettings: {}
        )
        let probe = try await CLIRunner(executable: cli).runOnce(
            arguments: ["--json", "--builds", "--builds-app", "example", "--builds-limit", "1"])
        let decoded = probe.stdout.buildEvents()
        XCTAssertEqual(decoded.count, 1, "premise: the fake's history decodes: stdout=\(probe.stdout) stderr=\(probe.stderr) exit=\(probe.exitCode)")
        let box = hosted(view, size: NSSize(width: 1000, height: 1000))
        var before: String?
        var lastTexts: [String] = []
        for _ in 0..<20 {
            try await Task.sleep(nanoseconds: 200_000_000)
            settle(box, seconds: 0.05)
            let nodes = try AccessibilityTree.nodes(in: box.window)
            before = lastBuildCell(nodes)
            lastTexts = nodes.filter { $0.role == kAXStaticTextRole }.compactMap { $0.value as? String }
            if let before, before != "—" { break }
        }
        XCTAssertNotEqual(before, "—", "premise: the history row was read: \(lastTexts)")
        XCTAssertNotNil(before, "premise: the Last build cell is on screen")

        try clickButton(named: "Build & Install", in: box.window)
        XCTAssertEqual(config.buildingApp, "example", "premise: the click started a build")
        try await Task.sleep(nanoseconds: 700_000_000)
        config.cancelBuild(appName: "example")
        var waited = 0
        while config.buildingApp != nil, waited < 100 {
            try await Task.sleep(nanoseconds: 100_000_000)
            waited += 1
        }
        XCTAssertNil(config.buildingApp, "premise: the stop ended the build")
        try await Task.sleep(nanoseconds: 800_000_000)
        settle(box, seconds: 0.2)

        let after = lastBuildCell(try AccessibilityTree.nodes(in: box.window))
        XCTAssertEqual(after, before)
    }

    func test_a_build_the_cli_reports_stopped_is_not_a_failure() async throws {
        let (box, config) = try await pane(exitingAfterStop: 143)
        let nodes = try await runBuild(box, config)
        let shown = nodes.filter { $0.role == kAXStaticTextRole }.compactMap { $0.value as? String }

        XCTAssertNil(config.buildFailures["example"], "a stop wrote a failure card")
        XCTAssertFalse(shown.contains("Last build failed"), "\(shown)")
        XCTAssertNil(nodes.first { $0.role == kAXButtonRole && $0.name == "Retry" },
                     "a stop painted the red Retry")
        XCTAssertTrue(shown.contains("Stopped during cyan."), "\(shown)")
    }

    /// The exit status is not what decides it: a stopped CLI that exits 0
    /// is still a stop, and still not a finished build to hand anywhere.
    func test_a_stop_is_a_stop_whatever_the_exit_status() async throws {
        let (box, config) = try await pane(exitingAfterStop: 0)
        let nodes = try await runBuild(box, config)
        let shown = nodes.filter { $0.role == kAXStaticTextRole }.compactMap { $0.value as? String }
        XCTAssertNil(config.buildFailures["example"])
        XCTAssertTrue(shown.contains("Stopped during cyan."), "\(shown)")
        XCTAssertNil(nodes.first { $0.role == kAXButtonRole && $0.name == "Done" },
                     "a stop read as a finished build")
    }

    /// The re-install sheet has no store to mark the stop for it: the
    /// collected events are its only reading, and without them the non-zero
    /// exit painted "Failed" over a re-install that was only stopped.
    func test_a_stopped_reinstall_is_not_a_failure() async throws {
        try AccessibilityTree.requireServed()
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-stopped-reinstall-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let cli = dir.appendingPathComponent("graft")
        try """
        #!/bin/sh
        case " $* " in
          *" --reinstall-build "*)
            printf '%s\\n' '{"type":"build-start","app":"example","bundle_id":"com.example.app","delivery":"livecontainer"}'
            printf '%s\\n' '{"type":"stage","name":"lc-push","status":"start"}'
            printf '%s\\n' '{"type":"build-end","app":"example","ok":false,"stopped":true,"error":null}'
            echo 'stopped' >&2
            exit 143 ;;
        esac
        exit 0
        """.write(to: cli, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: cli.path)
        let line = #"{"type":"build-record","id":7,"timestamp":"2026-11-17T10:00:00Z","app":"example","bundle_id":"com.example.app","version":"1.0","tweaks":[],"ipa_path":"/b/x.ipa","ipa_sha256":null,"log_path":null,"signing_team":null,"udid":null,"target":"device","success":true,"error":null}"#
        guard case let .buildRecord(build)? = line.buildEvents().first else {
            throw XCTSkip("build-record fixture did not decode")
        }
        let sh = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let sheet = ReinstallSheet(
            build: build, runner: CLIRunner(executable: cli),
            config: ConfigStore(runner: sh, cliArgs: ["-c", "true"], notify: { _ in }),
            artwork: ArtworkStore(runner: sh), onClose: {}
        )
        let box = hosted(sheet, size: NSSize(width: 640, height: 460))
        var shown: [String] = []
        let deadline = Date().addingTimeInterval(5)
        while Date() < deadline {
            try await Task.sleep(nanoseconds: 100_000_000)
            settle(box, seconds: 0.05)
            shown = try AccessibilityTree.texts(in: box.window)
            if shown.contains(where: { $0.caseInsensitiveCompare("stopped") == .orderedSame
                                       || $0.caseInsensitiveCompare("failed") == .orderedSame }) { break }
        }
        // The event can settle the pill before the subprocess has returned,
        // and the exit status is read after that: look again once it has.
        try await Task.sleep(nanoseconds: 700_000_000)
        settle(box, seconds: 0.1)
        shown = try AccessibilityTree.texts(in: box.window)
        XCTAssertTrue(shown.contains { $0.caseInsensitiveCompare("stopped") == .orderedSame }, "\(shown)")
        XCTAssertFalse(shown.contains { $0.caseInsensitiveCompare("failed") == .orderedSame }, "\(shown)")
    }
}
