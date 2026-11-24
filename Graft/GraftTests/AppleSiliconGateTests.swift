import SwiftUI
import XCTest
@testable import Graft

/// Graft 2 on an Intel Mac: one screen that says so, and nothing else runs.
@MainActor
final class AppleSiliconGateTests: XCTestCase {
    // MARK: - Detection

    func test_hw_optional_arm64_of_one_is_apple_silicon() {
        var asked: [String] = []
        XCTAssertTrue(AppleSilicon.isPresent(sysctl: { asked.append($0); return 1 }))
        XCTAssertEqual(asked, ["hw.optional.arm64"])
    }

    func test_hw_optional_arm64_of_zero_is_intel() {
        XCTAssertFalse(AppleSilicon.isPresent(sysctl: { _ in 0 }))
    }

    /// Older Intel Macs have no such entry at all.
    func test_no_hw_optional_arm64_is_intel() {
        XCTAssertFalse(AppleSilicon.isPresent(sysctl: { _ in nil }))
    }

    /// The real reader answers nil, not 0 or a crash, for a name this Mac
    /// does not have: what an Intel Mac's missing entry looks like to it.
    func test_the_sysctl_reader_answers_nil_for_an_entry_that_is_not_there() {
        XCTAssertNil(AppleSilicon.readSysctl("hw.optional.graft-test-no-such-entry"))
    }

    // MARK: - The gate

    /// Whole-diff review, docs-2: the button opens the upgrade guide's way
    /// back to 1.1, whose pipx command is tagged `@v1.1`, not the v1.1
    /// release page, whose own command isn't (and so installs 2.0's CLI).
    func test_the_message_and_the_way_to_1_1() {
        XCTAssertEqual(
            AppleSiliconRequiredView.message,
            "Graft 2.0 needs a Mac with Apple silicon. Graft 1.1 may work on an Intel Mac running macOS 15 or later, but it was never tested there."
        )
        XCTAssertEqual(AppleSiliconRequiredView.downloadLabel, "How to install Graft 1.1")
        XCTAssertEqual(
            AppleSiliconRequiredView.downloadURL.absoluteString,
            "https://github.com/GreenglassT/Graft/blob/main/docs/upgrading.md#going-back-to-11"
        )
        XCTAssertFalse(AppleSiliconRequiredView.downloadURL.absoluteString.contains("/releases/"))
    }

    /// A stand-in CLI that notes every call, so a test can tell whether
    /// anything ran at all: the first load, the setup check, device polling
    /// and every other store go through the one runner `ContentView` shares.
    private func recordingCLI() throws -> (runner: CLIRunner, calls: URL) {
        let fm = FileManager.default
        let dir = fm.temporaryDirectory.appendingPathComponent("graft-gate-\(UUID().uuidString)")
        try fm.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? fm.removeItem(at: dir) }
        let calls = dir.appendingPathComponent("calls")
        let graft = dir.appendingPathComponent("graft")
        try """
        #!/bin/sh
        [ -z "${GRAFT_GATE_WARM_UP-}" ] || exit 0
        echo "$*" >> '\(calls.path)'
        printf 'error: config file not found at /tmp/example/config.yaml\\n' >&2
        exit 2
        """.write(to: graft, atomically: true, encoding: .utf8)
        try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: graft.path)
        // macOS checks a new executable on its first exec, which over a phone
        // hotspot can outlast the 1.5 s `window` waits (`warmUpFreshExecutable`).
        // The warm-up run exits before it records anything.
        try warmUpFreshExecutable(graft.path, environment: ["GRAFT_GATE_WARM_UP": "1"])
        return (CLIRunner(executable: graft), calls)
    }

    private func window(isAppleSilicon: Bool, store: ConfigStore) async throws -> (Hosted, [String]) {
        let pane = hosted(
            AppleSiliconGate(isAppleSilicon: isAppleSilicon, openURL: { _ in }) {
                ContentView(store: store, adminShell: NoAdminShell())
            },
            size: NSSize(width: Metrics.windowMinWidth, height: Metrics.windowMinHeight)
        )
        // Long enough for ContentView's first-load `.task` to have run the
        // CLI, when there is a ContentView: the control below sees it do so.
        let deadline = Date().addingTimeInterval(1.5)
        while Date() < deadline {
            try await Task.sleep(nanoseconds: 100_000_000)
            settle(pane, seconds: 0.05)
        }
        let nodes = try AccessibilityTree.nodes(in: pane.window)
        let shown = nodes.flatMap { [$0.name, ($0.value as? String) ?? ""] }.filter { !$0.isEmpty }
        return (pane, shown)
    }

    func test_an_intel_mac_shows_only_the_gate_and_runs_nothing() async throws {
        try AccessibilityTree.requireServed()
        let (runner, calls) = try recordingCLI()
        let store = ConfigStore(runner: runner, notify: { _ in })
        let (pane, shown) = try await window(isAppleSilicon: false, store: store)

        XCTAssertTrue(shown.contains(AppleSiliconRequiredView.message), "\(shown)")
        let nodes = try AccessibilityTree.nodes(in: pane.window)
        XCTAssertTrue(nodes.contains {
            $0.role == kAXButtonRole && $0.name == AppleSiliconRequiredView.downloadLabel
        }, "the button to 1.1's install steps")
        XCTAssertFalse(FileManager.default.fileExists(atPath: calls.path),
                       "the CLI ran: \((try? String(contentsOf: calls, encoding: .utf8)) ?? "")")
        XCTAssertNil(store.setupNeed, "no setup screen")
        XCTAssertNotEqual(pane.window.title, SetupView.title)
    }

    /// Delta audit try13, #15: the download button opens the page through
    /// the opener it was given, which in the app is the browser and here is a
    /// recorder, never through SwiftUI's `openURL`, whose default is the real
    /// browser. The hosted view's `openURL` is overridden too, as a backstop
    /// this test owns: had the button gone back to the environment, the click
    /// would land in `viaEnvironment` and fail here, not open a browser.
    func test_the_download_button_opens_the_page_through_the_opener_it_was_given() throws {
        try AccessibilityTree.requireServed()
        final class Opened { var given: [URL] = []; var viaEnvironment: [URL] = [] }
        let opened = Opened()
        let pane = hosted(
            AppleSiliconGate(isAppleSilicon: false, openURL: { opened.given.append($0) }) { Text("the app") }
                .environment(\.openURL, OpenURLAction { url in
                    opened.viaEnvironment.append(url)
                    return .handled
                }),
            size: NSSize(width: Metrics.windowMinWidth, height: Metrics.windowMinHeight)
        )
        settle(pane)
        XCTAssertEqual(opened.given, [], "nothing opens until the button is pressed")

        try clickButton(named: AppleSiliconRequiredView.downloadLabel, in: pane.window)
        settle(pane, seconds: 0.2)
        XCTAssertEqual(opened.given, [AppleSiliconRequiredView.downloadURL])
        XCTAssertEqual(opened.viaEnvironment, [], "not through SwiftUI's openURL")
    }

    /// The test host is Graft.app itself, under its real bundle id and the
    /// real HOME. Its own window must run nothing: in a Debug build the CLI
    /// lookup finds ~/.local/bin/graft first, the owner's real CLI, and its
    /// App Store status had macOS ask for ipatool's keychain item during a
    /// test run (2.0.1). So the window is a placeholder, and its runner
    /// points at nothing that exists.
    @MainActor
    func test_the_test_hosts_own_window_runs_nothing() throws {
        try AccessibilityTree.requireServed()
        XCTAssertTrue(AdminShell.insideTests)
        XCTAssertFalse(FileManager.default.fileExists(atPath: GraftApp.testHostExecutable.path))
        // Every window the host has, whatever it's titled: the real UI names
        // its window after the selected item.
        let deadline = Date().addingTimeInterval(5)
        var shown: [[String]] = []
        repeat {
            RunLoop.main.run(until: Date().addingTimeInterval(0.1))
            shown = try NSApp.windows.filter { $0.isVisible && !($0 is NSPanel) }.map { window in
                try AccessibilityTree.nodes(in: window)
                    .flatMap { [$0.name, ($0.value as? String) ?? ""] }.filter { !$0.isEmpty }
            }
        } while !shown.contains(where: { $0.contains(TestHostWindow.text) }) && Date() < deadline
        XCTAssertTrue(shown.contains(where: { $0.contains(TestHostWindow.text) }), "\(shown)")
        // Not "no window shows Devices": other cases here host a ContentView
        // of their own, which may still be open. The app's own window
        // showing the placeholder is what proves its real UI never came up.
    }

    /// The control: the same window on Apple silicon is the app, and its
    /// first load runs the CLI — so "nothing ran" above is the gate's doing.
    func test_apple_silicon_gets_the_app() async throws {
        try AccessibilityTree.requireServed()
        let (runner, calls) = try recordingCLI()
        let store = ConfigStore(runner: runner, notify: { _ in })
        let (_, shown) = try await window(isAppleSilicon: true, store: store)

        XCTAssertFalse(shown.contains(AppleSiliconRequiredView.message), "\(shown)")
        XCTAssertTrue(FileManager.default.fileExists(atPath: calls.path), "the first load ran the CLI")
        XCTAssertEqual(store.setupNeed?.step, 2, "the CLI's missing config is the setup screen")
    }
}
