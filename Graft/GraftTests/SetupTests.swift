import SwiftUI
import XCTest
@testable import Graft

/// The first-run setup screen: which failures put it up, that it replaces the
/// alert rather than joining it, and that Check again takes it down.
///
/// The CLI side of the one sentence this depends on — "config file not found
/// at …" — is pinned by `CLIContractTests.test_a_missing_config_is_reported_
/// the_way_the_setup_screen_expects` against the installed CLI, and by the
/// Python suite (`tests/test_setup_signals.py`) from its side.
@MainActor
final class SetupTests: XCTestCase {
    // MARK: - Which failure is which

    /// `_fatal`'s two lines, as Rich prints them with the app's wide console.
    private static func fatal(_ error: String, hint: String? = nil) -> String {
        "error: \(error)\n" + (hint.map { "  hint: \($0)\n" } ?? "")
    }

    private static let missingConfig = fatal(
        "config file not found at /Users/x/.config/graft/config.yaml",
        hint: "run `graft --init` to create one"
    )

    func test_a_cli_that_cannot_be_started_is_the_install_step() {
        let error = CLIRunner.RunError.launchFailed(
            executable: "/Users/x/.local/bin/graft",
            underlying: CocoaError(.fileNoSuchFile)
        )
        XCTAssertEqual(SetupNeed(error: error), .cli)
    }

    func test_a_missing_config_is_the_config_step_and_carries_the_path() {
        let error = CLIError.exited(doing: "Load apps", code: 2, stderr: Self.missingConfig)
        XCTAssertEqual(
            SetupNeed(error: error), .config(path: "/Users/x/.config/graft/config.yaml")
        )
    }

    /// Every other config problem exits 2 as well. A config that exists and
    /// is broken needs the alert that says what is wrong with it — sending
    /// that user to `graft --init` would offer to overwrite their file.
    func test_a_broken_config_is_not_a_setup_step() {
        let broken = CLIError.exited(
            doing: "Load apps", code: 2,
            stderr: Self.fatal("config missing 'signing' section")
        )
        XCTAssertNil(SetupNeed(error: broken))
    }

    /// The sentence decides only on the status `ConfigError` exits with.
    func test_the_sentence_under_another_status_is_not_a_setup_step() {
        let other = CLIError.exited(doing: "Load apps", code: 1, stderr: Self.missingConfig)
        XCTAssertNil(SetupNeed(error: other))
    }

    /// Matched against the parsed `error:` line, not raw stderr: the same
    /// words in a hint, or in a path, are not this failure.
    func test_the_sentence_has_to_be_the_error_itself() {
        let inHint = CLIError.exited(
            doing: "Load apps", code: 2,
            stderr: Self.fatal(
                "failed to parse /x/config.yaml: bad indent",
                hint: "config file not found at /elsewhere is a different problem"
            )
        )
        XCTAssertNil(SetupNeed(error: inHint))
    }

    func test_other_failures_are_not_setup_steps() {
        XCTAssertNil(SetupNeed(error: CLIError.reported("config file not found at /x")))
        XCTAssertNil(SetupNeed(error: CancellationError()))
    }

    // MARK: - The store's first load

    private func store(executable: String = "/bin/sh", script: String) -> ConfigStore {
        ConfigStore(
            runner: CLIRunner(executable: URL(fileURLWithPath: executable)),
            cliArgs: ["-c", script]
        )
    }

    private static let oneApp = #"""
    printf '{"type":"app","name":"example","bundle_id":"com.example.app","preferred_version":"latest","tweaks":[],"display_name":"Example+","delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}\n'
    """#

    private static let noConfigScript = """
    printf 'error: config file not found at /tmp/example/config.yaml\\n  hint: run `graft --init` to create one\\n' >&2; exit 2
    """

    /// The whole point of the screen: a missing CLI is a step, and it does not
    /// also raise the alert it replaced.
    func test_no_cli_puts_up_the_install_step_and_no_alert() async {
        let store = store(executable: "/nonexistent-\(UUID().uuidString)/graft", script: "")
        let apps = await store.checkSetup()
        XCTAssertNil(apps)
        XCTAssertEqual(store.setupNeed, .cli)
        XCTAssertNil(store.lastError, "the setup screen replaces the alert")
    }

    func test_no_config_puts_up_the_config_step_and_no_alert() async {
        let store = store(script: Self.noConfigScript)
        _ = await store.checkSetup()
        XCTAssertEqual(store.setupNeed, .config(path: "/tmp/example/config.yaml"))
        XCTAssertNil(store.lastError)
    }

    /// Anything else is still an alert, exactly as before.
    func test_an_ordinary_failure_is_still_the_alert() async {
        let store = store(script: "printf 'error: config missing signing section\\n' >&2; exit 2")
        _ = await store.checkSetup()
        XCTAssertNil(store.setupNeed)
        XCTAssertEqual(store.lastError, "config missing signing section")
    }

    /// Check again: the same store, the same runner, and the user has since
    /// fixed things — the screen comes down and the apps are there.
    func test_check_again_clears_the_step_once_the_load_works() async throws {
        let flag = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-setup-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: flag) }
        let script = "if [ -e '\(flag.path)' ]; then \(Self.oneApp); else \(Self.noConfigScript); fi"
        let store = store(script: script)

        _ = await store.checkSetup()
        XCTAssertEqual(store.setupNeed?.step, 2, "premise: the config step is up")

        try Data().write(to: flag)
        let apps = await store.checkSetup()
        XCTAssertEqual(apps?.map(\.name), ["example"])
        XCTAssertNil(store.setupNeed)
        XCTAssertFalse(store.isCheckingSetup)
    }

    // MARK: - The window

    private struct Hosted {
        let layout: HostedLayout
        let store: ConfigStore
    }

    private func hostedContent(_ store: ConfigStore) async -> Hosted {
        let layout = laidOutForMeasurement(
            ContentView(store: store), width: Metrics.windowMinWidth, height: Metrics.windowMinHeight
        )
        let deadline = Date().addingTimeInterval(5)
        while store.setupNeed == nil && store.apps.isEmpty && Date() < deadline {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        settleLayout(layout)
        return Hosted(layout: layout, store: store)
    }

    private func settleLayout(_ layout: HostedLayout) {
        let deadline = Date().addingTimeInterval(0.3)
        while Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.02))
        }
        layout.root.layoutSubtreeIfNeeded()
        layout.window.displayIfNeeded()
    }

    /// The detail column shows the setup screen — read back from the window
    /// title, which `.navigationTitle` drives — and no alert is attached.
    func test_the_window_shows_the_setup_screen_when_the_cli_is_missing() async {
        let hosted = await hostedContent(
            store(executable: "/nonexistent-\(UUID().uuidString)/graft", script: "")
        )
        XCTAssertEqual(hosted.store.setupNeed, .cli)
        XCTAssertEqual(hosted.layout.window.title, SetupView.title)
        XCTAssertNil(hosted.layout.window.attachedSheet, "no alert over the setup screen")
        XCTAssertNil(hosted.store.lastError)
    }

    func test_the_window_shows_the_setup_screen_when_the_config_is_missing() async {
        let hosted = await hostedContent(store(script: Self.noConfigScript))
        XCTAssertEqual(hosted.store.setupNeed?.step, 2)
        XCTAssertEqual(hosted.layout.window.title, SetupView.title)
        XCTAssertNil(hosted.layout.window.attachedSheet)
    }

    /// The control: a store that loads opens on its first app, so the title
    /// assertion above is measuring the setup screen and not a window that
    /// never takes its title from anything.
    func test_a_working_cli_opens_on_the_app_not_the_setup_screen() async {
        let hosted = await hostedContent(store(script: Self.oneApp))
        XCTAssertNil(hosted.store.setupNeed)
        XCTAssertEqual(hosted.layout.window.title, "Example+")
    }

    // MARK: - The steps

    func test_the_install_step_is_current_until_graft_runs() {
        XCTAssertEqual(SetupView.state(of: 1, for: .cli), .current)
        XCTAssertEqual(SetupView.state(of: 2, for: .cli), .upcoming)
        XCTAssertEqual(SetupView.state(of: 1, for: .config(path: nil)), .done)
        XCTAssertEqual(SetupView.state(of: 2, for: .config(path: nil)), .current)
        XCTAssertEqual(SetupView.state(of: 3, for: .config(path: nil)), .upcoming)
        let tools = SetupNeed.tools([MissingTool(name: "cyan", command: "pipx install cyan")])
        XCTAssertEqual(SetupView.state(of: 1, for: tools), .done)
        XCTAssertEqual(SetupView.state(of: 2, for: tools), .done)
        XCTAssertEqual(SetupView.state(of: 3, for: tools), .current)
        XCTAssertEqual(SetupNeed.stepCount, 3)
    }

    // MARK: - An installed graft that will not start

    private func scratchDir() throws -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-setup-broken-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        return dir
    }

    private func executable(_ dir: URL, _ name: String, _ body: String, mode: Int = 0o755) throws -> URL {
        let url = dir.appendingPathComponent(name)
        try body.write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: mode], ofItemAtPath: url.path)
        return url
    }

    /// The real launch failure for `url`, read as a setup step.
    private func need(launching url: URL) async -> SetupNeed? {
        do {
            _ = try await CLIRunner(executable: url).runOnce(arguments: [])
            XCTFail("premise: \(url.lastPathComponent) should not start")
            return nil
        } catch {
            return SetupNeed(error: error)
        }
    }

    /// What a pipx venv looks like after Homebrew removed the Python under it:
    /// the step carries the file, the system's error, and a reason that names
    /// the missing interpreter — and offers a reinstall, not an install.
    func test_a_graft_whose_python_is_gone_is_the_repair_step() async throws {
        let dir = try scratchDir()
        let graft = try executable(dir, "graft", "#!/opt/example/bin/python3.11-gone\nprint(1)\n")
        guard case let .brokenCLI(broken)? = await need(launching: graft) else {
            return XCTFail("not the repair step")
        }
        XCTAssertEqual(broken.path, graft.path)
        XCTAssertEqual(broken.errno, ENOENT)
        XCTAssertEqual(broken.errnoLine, "error 2: No such file or directory")
        XCTAssertTrue(broken.reason.contains("/opt/example/bin/python3.11-gone"), broken.reason)
        XCTAssertEqual(SetupNeed.brokenCLI(broken).step, 1)
        XCTAssertEqual(SetupView.stepOneTitle(for: .brokenCLI(broken)), "Repair the command-line tool")
        XCTAssertEqual(SetupView.stepOneTitle(for: .cli), "Install the command-line tool")
        XCTAssertEqual(GraftExecutable.reinstallCommand, "pipx reinstall graft-ipa")
    }

    func test_each_way_an_installed_graft_fails_to_start_has_its_own_reason() async throws {
        let dir = try scratchDir()
        let noShebang = try executable(dir, "no-shebang", "print(1)\n")
        let notExecutable = try executable(dir, "not-executable", "#!/bin/sh\n", mode: 0o644)
        let folder = dir.appendingPathComponent("folder")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let dangling = dir.appendingPathComponent("dangling")
        try FileManager.default.createSymbolicLink(
            at: dangling, withDestinationURL: dir.appendingPathComponent("venv-gone/bin/graft")
        )

        let cases: [(URL, Int32?, String)] = [
            (noShebang, ENOEXEC, "neither a program nor a script"),
            (notExecutable, EACCES, "not marked as executable"),
            (folder, EACCES, "a folder"),
            (dangling, ENOENT, "venv-gone/bin/graft"),
        ]
        for (url, errno, words) in cases {
            guard case let .brokenCLI(broken)? = await need(launching: url) else {
                XCTFail("\(url.lastPathComponent): not the repair step")
                continue
            }
            XCTAssertEqual(broken.errno, errno, url.lastPathComponent)
            XCTAssertTrue(broken.reason.contains(words), "\(url.lastPathComponent): \(broken.reason)")
        }
    }

    /// Nothing at the path is still the plain install step.
    func test_nothing_installed_is_still_the_install_step() async throws {
        let dir = try scratchDir()
        let absent = await need(launching: dir.appendingPathComponent("graft"))
        XCTAssertEqual(absent, .cli)
    }

    /// Every other surface shows the same failure as a banner: it says what
    /// is wrong with the file and the reinstall, not "needs the CLI installed".
    func test_a_banner_for_a_broken_graft_says_reinstall() async throws {
        let dir = try scratchDir()
        let graft = try executable(dir, "graft", "#!/opt/example/bin/python3.11-gone\n")
        do {
            _ = try await CLIRunner(executable: graft).runOnce(arguments: [])
            XCTFail("premise")
        } catch {
            let text = error.localizedDescription
            XCTAssertTrue(text.contains(GraftExecutable.reinstallCommand), text)
            XCTAssertTrue(text.contains(graft.path), text)
            XCTAssertFalse(text.contains(GraftExecutable.installCommand), text)
        }
    }

    // MARK: - Step 3: the tools

    /// `preflight-check` rows as the CLI writes them: one with the structured
    /// `install` field, one from an older CLI with only the hint, one whose
    /// `install` is null, one that is not a tool, and the passing and
    /// information rows.
    private static let checkOutput = [
        #"{"type":"preflight-check","name":"account","ok":true,"detail":"free Apple ID","info":true}"#,
        #"{"type":"preflight-check","name":"cyan","ok":false,"detail":null,"hint":"install with: pipx install --force https://example.com/cyan.zip","install":"pipx install --force https://example.com/cyan.zip"}"#,
        #"{"type":"preflight-check","name":"otool","ok":true,"detail":"/usr/bin/otool"}"#,
        #"{"type":"preflight-check","name":"idevice_id","ok":false,"detail":null,"hint":"install with: brew install libimobiledevice"}"#,
        #"{"type":"preflight-check","name":"zsign","ok":false,"detail":null,"hint":"install with: brew install zsign","install":null}"#,
        #"{"type":"preflight-check","name":"connected ios device(s)","ok":false,"detail":"none","hint":"plug an iPhone in via USB and trust this Mac"}"#,
        #"{"type":"preflight-end","ok_count":1,"fail_count":4,"account":"free"}"#,
    ].joined(separator: "\n")

    private static var checks: [PreflightCheck] {
        checkOutput.buildEvents().compactMap { event -> PreflightCheck? in
            if case let .preflightCheck(check) = event { return check }
            return nil
        }
    }

    func test_the_install_field_decodes_and_is_optional() throws {
        let rows = Dictionary(uniqueKeysWithValues: Self.checks.map { ($0.name, $0) })
        XCTAssertEqual(rows.count, 6, "every row decodes, with or without `install`")
        XCTAssertEqual(rows["cyan"]?.install, "pipx install --force https://example.com/cyan.zip")
        XCTAssertEqual(rows["cyan"]?.installCommand, "pipx install --force https://example.com/cyan.zip")
        XCTAssertNil(rows["idevice_id"]?.install, "absent from an older CLI")
        XCTAssertEqual(
            rows["idevice_id"]?.installCommand, "brew install libimobiledevice",
            "an older CLI's command comes from its hint"
        )
        XCTAssertNil(
            rows["zsign"]?.installCommand,
            "a null `install` is the CLI saying there is no command — not read from the hint"
        )
        XCTAssertNil(rows["connected ios device(s)"]?.installCommand, "advice, not a command")

        let malformed = #"{"type":"preflight-check","name":"x","ok":false,"install":42}"#
        guard case let .preflightCheck(row)? = malformed.buildEvents().first else {
            return XCTFail("a malformed `install` cost the row")
        }
        XCTAssertNil(row.installCommand)
    }

    /// One tool is "it", not "them": the screen most often lists exactly one.
    func test_step_three_speaks_of_one_tool_in_the_singular() {
        XCTAssertEqual(SetupView.toolsIntro(count: 1),
                       "One tool a build needs is missing from this Mac. Install it in Terminal, then check again:")
        XCTAssertEqual(SetupView.toolsIntro(count: 2),
                       "2 tools a build needs are missing from this Mac. Install each in Terminal, then check again:")
        XCTAssertEqual(SetupView.continueLabel(count: 1), "Continue without it")
        XCTAssertEqual(SetupView.continueLabel(count: 3), "Continue without them")
        XCTAssertEqual(SetupView.continueNote(count: 1), "Settings lists it until it's installed.")
        XCTAssertEqual(SetupView.continueNote(count: 2), "Settings lists them until they're installed.")
    }

    func test_only_failed_rows_with_a_command_are_missing_tools() {
        XCTAssertEqual(MissingTool.all(in: Self.checks), [
            MissingTool(name: "cyan", command: "pipx install --force https://example.com/cyan.zip"),
            MissingTool(name: "idevice_id", command: "brew install libimobiledevice"),
        ])
    }

    /// A store fresh off the config step, whose `--list` now works.
    private func storeOnTheConfigStep() async throws -> (ConfigStore, URL) {
        let flag = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-setup-\(UUID().uuidString)")
        addTeardownBlock { try? FileManager.default.removeItem(at: flag) }
        let script = "if [ -e '\(flag.path)' ]; then \(Self.oneApp); else \(Self.noConfigScript); fi"
        let store = store(script: script)
        _ = await store.checkSetup()
        XCTAssertEqual(store.setupNeed?.step, 2, "premise: the config step is up")
        try Data().write(to: flag)
        return (store, flag)
    }

    func test_a_config_with_tools_missing_is_step_three() async throws {
        let (store, _) = try await storeOnTheConfigStep()
        let apps = await store.checkSetup(toolChecks: { Self.checks })
        XCTAssertEqual(apps?.map(\.name), ["example"], "the config loaded")
        XCTAssertEqual(store.setupNeed, .tools(MissingTool.all(in: Self.checks)))
        XCTAssertEqual(store.setupNeed?.step, 3)
        XCTAssertNil(store.lastError)
    }

    func test_step_three_comes_down_once_the_tools_are_there() async throws {
        let (store, _) = try await storeOnTheConfigStep()
        _ = await store.checkSetup(toolChecks: { Self.checks })
        XCTAssertEqual(store.setupNeed?.step, 3, "premise")
        let installed = Self.checks.filter { $0.name == "otool" }
        _ = await store.checkSetup(toolChecks: { installed })
        XCTAssertNil(store.setupNeed)
    }

    /// Someone already using the app is not put back on a setup screen: the
    /// tools step is part of setting up, and the window's notice covers the
    /// rest.
    func test_a_working_config_at_launch_never_shows_step_three() async {
        let store = store(script: Self.oneApp)
        var asked = false
        _ = await store.checkSetup(toolChecks: { asked = true; return Self.checks })
        XCTAssertNil(store.setupNeed)
        XCTAssertFalse(asked, "no setup screen, so no scan for one")
    }

    /// Unreadable checks are not a reason to hold the user on the screen.
    func test_checks_that_could_not_run_do_not_hold_up_setup() async throws {
        let (store, _) = try await storeOnTheConfigStep()
        _ = await store.checkSetup(toolChecks: { nil })
        XCTAssertNil(store.setupNeed)
    }

    func test_continuing_without_the_tools_opens_the_app_for_the_session() async throws {
        let (store, _) = try await storeOnTheConfigStep()
        _ = await store.checkSetup(toolChecks: { Self.checks })
        XCTAssertEqual(store.setupNeed?.step, 3, "premise")
        store.continueWithoutTools()
        XCTAssertNil(store.setupNeed)

        // Only step 3 can be skipped.
        let other = self.store(script: Self.noConfigScript)
        _ = await other.checkSetup()
        other.continueWithoutTools()
        XCTAssertEqual(other.setupNeed?.step, 2)
    }

    // MARK: - The window's notice

    func test_the_notice_counts_the_missing_tools_and_points_at_settings() {
        XCTAssertEqual(MissingToolsNotice.text(count: 1), "1 tool missing — see Settings")
        XCTAssertEqual(MissingToolsNotice.text(count: 2), "2 tools missing — see Settings")
    }

    func test_the_notice_shows_until_the_tools_are_there_or_it_is_closed() {
        let missing = MissingTool.all(in: Self.checks)
        XCTAssertEqual(ContentView.toolsNotice(missing: missing, dismissed: nil, showingSettings: false), missing)
        XCTAssertNil(ContentView.toolsNotice(missing: [], dismissed: nil, showingSettings: false))
        XCTAssertNil(
            ContentView.toolsNotice(missing: missing, dismissed: nil, showingSettings: true),
            "Settings lists them itself"
        )
        XCTAssertNil(ContentView.toolsNotice(missing: missing, dismissed: missing, showingSettings: false))
        XCTAssertEqual(
            ContentView.toolsNotice(missing: missing, dismissed: Array(missing.prefix(1)), showingSettings: false),
            missing, "closed on a different list: a new tool went missing"
        )
    }

    /// A fake `graft` that answers `--list` once `flag` exists (always, when
    /// it is nil) and `--check` with `checkOutput`'s two missing tools.
    private func fakeGraft(configExistsWhen flag: URL?) throws -> URL {
        let dir = try scratchDir()
        let check = Self.checkOutput.split(separator: "\n")
            .map { "printf '%s\\n' '\($0)'" }.joined(separator: "\n")
        let condition = flag.map { "[ -e '\($0.path)' ]" } ?? "true"
        return try executable(dir, "graft", """
        #!/bin/sh
        case " $* " in
          *" --check "*)
            \(check)
            exit 3 ;;
          *" --list "*)
            if \(condition); then \(Self.oneApp); exit 0; fi
            \(Self.noConfigScript) ;;
        esac
        exit 0
        """)
    }

    /// The window in front, which is what the accessibility tree is served
    /// for — the measurement harness's window is not. (`GraftTests.Hosted`:
    /// this class's own `Hosted` shadows the harness's.)
    private func frontWindow(_ store: ConfigStore) -> GraftTests.Hosted {
        hosted(ContentView(store: store), size: NSSize(width: 1100, height: 760))
    }

    /// Wait, suspended, until `done` holds or five seconds pass. Suspended
    /// because this test is a main-queue job and the window's `.task`s are
    /// main-actor work that runs only while it is.
    private func waitFor(_ window: GraftTests.Hosted, _ done: () -> Bool) async {
        let deadline = Date().addingTimeInterval(5)
        while !done(), Date() < deadline {
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        settle(window, seconds: 0.2)
    }

    /// The window end to end: on the config step, the user runs `graft
    /// --init` and presses Check again — the window's own button, through its
    /// own wiring to `--check` — and the screen moves on to step 3 with each
    /// missing tool and its command, rather than opening the app.
    func test_check_again_moves_the_window_on_to_step_three() async throws {
        try AccessibilityTree.requireServed()
        let flag = try scratchDir().appendingPathComponent("config-exists")
        let store = ConfigStore(runner: CLIRunner(executable: try fakeGraft(configExistsWhen: flag)))
        let window = frontWindow(store)
        await waitFor(window) { store.setupNeed != nil }
        XCTAssertEqual(store.setupNeed?.step, 2, "premise: the config step")

        try Data().write(to: flag)
        try clickButton(named: "Check again", in: window.window)
        await waitFor(window) { store.setupNeed?.step == 3 }
        XCTAssertEqual(store.setupNeed?.step, 3)
        XCTAssertEqual(window.window.title, SetupView.title)

        let texts = try AccessibilityTree.texts(in: window.window)
        for line in ["cyan", "pipx install --force https://example.com/cyan.zip",
                     "idevice_id", "brew install libimobiledevice"] {
            XCTAssertTrue(texts.contains(line), "\(line) not on screen: \(texts)")
        }
        XCTAssertFalse(texts.contains("zsign"), "a null `install` is not a missing tool")
    }

    /// Someone already set up opens on the app, with the calm notice under it
    /// rather than a setup screen — from the window's own launch-time scan.
    func test_the_window_shows_the_missing_tools_notice_over_a_working_config() async throws {
        try AccessibilityTree.requireServed()
        let store = ConfigStore(runner: CLIRunner(executable: try fakeGraft(configExistsWhen: nil)))
        let window = frontWindow(store)
        let notice = MissingToolsNotice.text(count: 2)
        var texts: [String] = []
        let deadline = Date().addingTimeInterval(5)
        while Date() < deadline {
            try await Task.sleep(nanoseconds: 100_000_000)
            settle(window, seconds: 0.05)
            texts = try AccessibilityTree.texts(in: window.window)
            if texts.contains(notice) { break }
        }
        XCTAssertNil(store.setupNeed)
        XCTAssertEqual(window.window.title, "Example+", "the app, not a setup screen")
        XCTAssertTrue(texts.contains(notice), "\(texts)")
    }
}

/// F2: an account change made in Terminal is noticed when the window comes
/// back to the front, not only on the setup screen.
@MainActor
final class AccountChangeOnReturnTests: XCTestCase {
    /// `controlActiveState` injected, as `DeviceVitalsPollTests` does: a test
    /// window is not reliably frontmost, and the transition is the subject.
    private struct Harness: View {
        let store: ConfigStore
        let active: ControlActiveState
        var body: some View {
            ContentView(store: store).environment(\.controlActiveState, active)
        }
    }

    private func fakeGraft(accountFile: URL, listCount: URL) throws -> URL {
        let dir = accountFile.deletingLastPathComponent()
        let cli = dir.appendingPathComponent("graft")
        try """
        #!/bin/sh
        case " $* " in
          *" --list "*)
            echo x >> '\(listCount.path)'
            printf '{"type":"account","account":"%s"}\\n' "$(cat '\(accountFile.path)')"
            printf '%s\\n' '{"type":"app","name":"example","bundle_id":"com.example.app","preferred_version":"latest","tweaks":[],"display_name":"Example+","delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}'
            exit 0 ;;
        esac
        exit 0
        """.write(to: cli, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: cli.path)
        return cli
    }

    private func lines(_ url: URL) -> Int {
        ((try? String(contentsOf: url, encoding: .utf8)) ?? "").split(separator: "\n").count
    }

    func test_returning_to_the_window_rereads_the_account() async throws {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-account-return-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let accountFile = dir.appendingPathComponent("account")
        let listCount = dir.appendingPathComponent("list-count")
        try "free".write(to: accountFile, atomically: true, encoding: .utf8)
        let store = ConfigStore(runner: CLIRunner(
            executable: try fakeGraft(accountFile: accountFile, listCount: listCount)
        ))

        let host = NSHostingView(rootView: Harness(store: store, active: .key))
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1100, height: 760),
            styleMask: [.titled, .resizable], backing: .buffered, defer: false
        )
        window.contentView = host
        host.frame = NSRect(x: 0, y: 0, width: 1100, height: 760)
        host.layoutSubtreeIfNeeded()
        window.displayIfNeeded()

        func wait(_ done: () -> Bool) async {
            let deadline = Date().addingTimeInterval(5)
            while !done(), Date() < deadline { try? await Task.sleep(nanoseconds: 50_000_000) }
        }
        await wait { store.account != nil }
        XCTAssertEqual(store.account, .free, "premise: the launch read")
        XCTAssertNil(store.setupNeed, "premise: set up, so the old gate would do nothing")

        // `graft --init` in Terminal, answered yes.
        try "paid".write(to: accountFile, atomically: true, encoding: .utf8)
        host.rootView = Harness(store: store, active: .inactive)
        try await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(store.account, .free, "control: nothing re-reads while away")
        let readsBefore = lines(listCount)

        host.rootView = Harness(store: store, active: .key)
        await wait { store.account == .paid }
        XCTAssertEqual(store.account, .paid, "coming back did not notice the new account")
        XCTAssertEqual(lines(listCount), readsBefore + 1, "one re-read per return")
        withExtendedLifetime(window) {}
    }

    /// A `ContentView` hosted under an injected `controlActiveState`, loaded,
    /// with the file that counts its `--list` runs.
    private func launched() async throws -> (NSHostingView<Harness>, NSWindow, ConfigStore, URL) {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-return-gate-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let accountFile = dir.appendingPathComponent("account")
        let listCount = dir.appendingPathComponent("list-count")
        try "free".write(to: accountFile, atomically: true, encoding: .utf8)
        let store = ConfigStore(runner: CLIRunner(
            executable: try fakeGraft(accountFile: accountFile, listCount: listCount)
        ))
        let host = NSHostingView(rootView: Harness(store: store, active: .key))
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1100, height: 760),
            styleMask: [.titled, .resizable], backing: .buffered, defer: false
        )
        window.contentView = host
        host.frame = NSRect(x: 0, y: 0, width: 1100, height: 760)
        host.layoutSubtreeIfNeeded()
        window.displayIfNeeded()
        let deadline = Date().addingTimeInterval(5)
        while store.account == nil, Date() < deadline { try await Task.sleep(nanoseconds: 50_000_000) }
        XCTAssertNotNil(store.account, "premise: the launch read")
        // Past the launch read: the window's own `--check` and anything else
        // the first render starts.
        try await Task.sleep(nanoseconds: 300_000_000)
        return (host, window, store, listCount)
    }

    /// S8: returns that come in a burst — focus flicking away and back —
    /// coalesce into one refresh.
    ///
    /// The other half of S8, that `.key` → `.active` → `.key` (a sheet taking
    /// key and giving it back) is not a return, has no hosted test: injected
    /// here, `.active` reads back as `.key` — the macOS 15 mapping
    /// `ContentView.controlActiveState` describes — so `onChange` never sees
    /// it, and a test of it passed with the rule reverted. It is
    /// `ReturnRefreshTests.test_only_coming_back_from_inactive_is_a_return`,
    /// on the rule `onChange` calls.
    func test_a_burst_of_returns_is_one_refresh() async throws {
        let (host, window, store, listCount) = try await launched()
        let before = lines(listCount)
        for _ in 0..<3 {
            host.rootView = Harness(store: store, active: .inactive)
            try await Task.sleep(nanoseconds: 150_000_000)
            host.rootView = Harness(store: store, active: .key)
            try await Task.sleep(nanoseconds: 300_000_000)
        }
        try await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(lines(listCount), before + 1, "three returns inside two seconds")
        withExtendedLifetime(window) {}
    }
}

/// What a return to the window re-reads (`ContentView.refreshOnReturn`),
/// driven with stores alone, and the gate that coalesces returns.
@MainActor
final class ReturnRefreshTests: XCTestCase {
    private struct Fake {
        let cli: URL
        let account: URL
        let broken: URL
        let checks: URL
        let settingsReads: URL
    }

    /// A `graft` whose account is a file, whose `--check` reports that
    /// account, and whose `--settings` cannot parse config.yaml while `broken`
    /// exists. `--check` and `--settings` each count their runs.
    private func fake() throws -> Fake {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-return-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let fake = Fake(
            cli: dir.appendingPathComponent("graft"),
            account: dir.appendingPathComponent("account"),
            broken: dir.appendingPathComponent("broken"),
            checks: dir.appendingPathComponent("checks"),
            settingsReads: dir.appendingPathComponent("settings-reads")
        )
        let paths = #""paths":{"config_dir":"/x/cfg/graft","cache_dir":"/x/ipas","built_dir":"/x/built","state_dir":"/x/state/graft","tweaks_dir":"/x/debs"}"#
        let parsed = #"{"type":"settings","default_device_udid":null,"archive_keep":2,"signing_cert":null,"signing_profile":null,"signing_team_id":null,"#
            + paths + "}"
        let unparsed = #"{"type":"settings","default_device_udid":null,"archive_keep":null,"signing_cert":null,"signing_profile":null,"signing_team_id":null,"parse_error":"mapping values are not allowed here (line 3, column 9)","paths_are_defaults":true,"#
            + paths + "}"
        try """
        #!/bin/sh
        account=$(cat '\(fake.account.path)')
        case " $* " in
          *" --list "*)
            printf '{"type":"account","account":"%s"}\\n' "$account"
            printf '%s\\n' '\(accountTestAppLine())'
            exit 0 ;;
          *" --check "*)
            echo x >> '\(fake.checks.path)'
            printf '{"type":"preflight-check","name":"account","ok":true,"detail":"%s","info":true}\\n' "$account"
            printf '{"type":"preflight-end","ok_count":0,"fail_count":0,"account":"%s"}\\n' "$account"
            exit 0 ;;
          *" --settings "*)
            echo x >> '\(fake.settingsReads.path)'
            if [ -e '\(fake.broken.path)' ]; then printf '%s\\n' '\(unparsed)'; else printf '%s\\n' '\(parsed)'; fi
            exit 0 ;;
        esac
        exit 0
        """.write(to: fake.cli, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: fake.cli.path)
        try "free".write(to: fake.account, atomically: true, encoding: .utf8)
        return fake
    }

    private func count(_ url: URL) -> Int {
        ((try? String(contentsOf: url, encoding: .utf8)) ?? "").split(separator: "\n").count
    }

    /// Launched: the app list read, and the launch scan run.
    private func launched(_ fake: Fake) async throws -> (ConfigStore, SettingsStore) {
        let runner = CLIRunner(executable: fake.cli)
        let config = ConfigStore(runner: runner)
        let settings = SettingsStore(runner: runner)
        _ = try await config.loadApps()
        try await settings.runChecks()
        XCTAssertEqual(config.account, .free, "premise")
        XCTAssertEqual(settings.account, .free, "premise: the scan was for the free account")
        XCTAssertTrue(settings.missingTools.isEmpty, "premise: nothing missing, so the old gate never scans")
        return (config, settings)
    }

    /// F2/S2: free → paid in Terminal, with no tool missing. The app list
    /// picked it up, and the Preflight rows went on showing the free set —
    /// no notice for the paid-only tools, and the first build failed at
    /// preflight.
    func test_an_account_changed_in_terminal_rescans_the_tools() async throws {
        let fake = try fake()
        let (config, settings) = try await launched(fake)
        try "paid".write(to: fake.account, atomically: true, encoding: .utf8)

        await ContentView.refreshOnReturn(config: config, settings: settings)

        XCTAssertEqual(config.account, .paid)
        XCTAssertEqual(count(fake.checks), 2, "the scan was not re-run for the new account")
        XCTAssertEqual(settings.account, .paid)
        XCTAssertEqual(settings.checks.first?.detail, "paid")
    }

    /// Control: the same account is not a reason to scan again.
    func test_an_unchanged_account_does_not_rescan() async throws {
        let fake = try fake()
        let (config, settings) = try await launched(fake)
        await ContentView.refreshOnReturn(config: config, settings: settings)
        XCTAssertEqual(count(fake.checks), 1)
    }

    /// K4/S3: the notice says to fix config.yaml and come back to Settings;
    /// coming back now re-reads it, so the notice and the frozen editor go.
    func test_a_config_that_did_not_parse_is_reread_on_return() async throws {
        let fake = try fake()
        let (config, settings) = try await launched(fake)
        try Data().write(to: fake.broken)
        _ = try await settings.loadSettings()
        XCTAssertNotNil(settings.parseError, "premise: the notice is up")

        try FileManager.default.removeItem(at: fake.broken)
        await ContentView.refreshOnReturn(config: config, settings: settings)

        XCTAssertNil(settings.parseError, "the notice stayed up over a fixed config.yaml")
        XCTAssertEqual(settings.settings?.archiveKeep, 2)
    }

    /// Control: a config that parsed costs no `--settings` on return.
    func test_a_config_that_parsed_is_not_reread_on_return() async throws {
        let fake = try fake()
        let (config, settings) = try await launched(fake)
        _ = try await settings.loadSettings()
        XCTAssertNil(settings.parseError, "premise")
        await ContentView.refreshOnReturn(config: config, settings: settings)
        XCTAssertEqual(count(fake.settingsReads), 1)
    }

    // MARK: - S8: which changes are a return, and coalescing them

    func test_only_coming_back_from_inactive_is_a_return() {
        XCTAssertTrue(ContentView.isReturn(from: .inactive, to: .key))
        XCTAssertTrue(ContentView.isReturn(from: .inactive, to: .active))
        XCTAssertFalse(ContentView.isReturn(from: .key, to: .active), "a sheet took key")
        XCTAssertFalse(ContentView.isReturn(from: .active, to: .key), "the sheet closed")
        XCTAssertFalse(ContentView.isReturn(from: .key, to: .inactive))
    }

    func test_the_gate_runs_one_refresh_at_a_time() {
        var gate = ReturnRefreshGate()
        let start = ContinuousClock.now
        XCTAssertTrue(gate.begin(at: start))
        XCTAssertFalse(gate.begin(at: start + .seconds(5)), "one is in flight")
        gate.end(at: start + .milliseconds(300))
        XCTAssertFalse(gate.begin(at: start + .seconds(2)), "under two seconds since it ran")
        XCTAssertTrue(gate.begin(at: start + .milliseconds(2400)))
    }
}
