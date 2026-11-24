import ApplicationServices
import SwiftUI
import XCTest
@testable import Graft

/// Every control on the panes a new user meets has a name VoiceOver can read,
/// and every checkbox is a checkbox.
///
/// The tree is read through `AccessibilityTree` — the Accessibility API, the
/// one VoiceOver reads — which says how and why.
///
/// **What a name is here: `AXDescription`, or `AXTitle`.** SwiftUI puts a
/// button's label in `AXDescription` (and lists only `AXAttributedDescription`
/// among the element's attribute *names*), AppKit's table headers in
/// `AXTitle`. That listing is why System Events reports these buttons as
/// nameless — it reads only attributes an element advertises — while VoiceOver
/// and this test get the label by asking for it. Measured on the hand-test
/// build, 2026-11-18: "Get latest from App Store", "Choose IPA…", "Attach…",
/// "Edit…", Canister's "Fetch", and both sheets' Cancel/Add/Attach all answer
/// `AXDescription` with their title. What was genuinely unnamed was the
/// options chevron ("Go Down", the symbol's own name), Canister's empty
/// column header, the Paths section's Reveal icons, and the Attach sheet's
/// checkbox — which was an `AXImage` with no action at all.
@MainActor
final class AccessibilityTests: XCTestCase {
    private static let controlRoles: Set<String> = [
        kAXButtonRole, kAXCheckBoxRole, kAXRadioButtonRole, kAXPopUpButtonRole,
        kAXMenuButtonRole, "AXSwitch",
    ]

    /// The title bar's own buttons, which AppKit names by subrole alone.
    private static let windowChrome: Set<String> = [
        kAXCloseButtonSubrole, kAXMinimizeButtonSubrole, kAXZoomButtonSubrole,
        kAXFullScreenButtonSubrole,
    ]

    private func unnamedControls(in window: NSWindow) throws -> [String] {
        try AccessibilityTree.nodes(in: window)
            .filter { Self.controlRoles.contains($0.role) }
            .filter { !($0.subrole.map(Self.windowChrome.contains) ?? false) }
            .filter { $0.name.isEmpty }
            .map { "\($0.role)\($0.subrole.map { "/\($0)" } ?? "")" }
    }

    private func controls(in window: NSWindow) throws -> [AccessibilityTree.Node] {
        try AccessibilityTree.nodes(in: window).filter { Self.controlRoles.contains($0.role) }
    }

    // MARK: - Fixtures

    private static func appLine(tweaks: [String]) -> String {
        let attached = tweaks.map { "\"\($0)\"" }.joined(separator: ",")
        return #"{"type":"app","name":"example","bundle_id":"com.example.app","preferred_version":"latest","tweaks":[\#(attached)],"display_name":"Example+","delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}\n"#
    }

    private func configStore(tweaks: [String] = ["a.deb"]) -> ConfigStore {
        ConfigStore(
            runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")),
            cliArgs: ["-c", "printf '\(Self.appLine(tweaks: tweaks))'"], notify: { _ in })
    }

    private func tweakLibrary() async -> TweaksStore {
        let lines = [
            #"{"type":"tweak","filename":"a.deb","size_bytes":19154,"used_by":["example"],"name":"Alpha Tweak","version":"1.0"}"#,
            #"{"type":"tweak","filename":"b.deb","size_bytes":2048,"used_by":[],"name":"Bravo Tweak","version":"2.1"}"#,
        ].joined(separator: "\\n") + "\\n"
        let store = TweaksStore(
            runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")),
            cliArgs: ["-c", "printf '\(lines)'"]
        )
        _ = try? await store.loadTweaks()
        return store
    }

    private func wait(seconds: TimeInterval) async {
        try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
    }

    // MARK: - The panes

    /// The app pane, as the window shows it: header, build-target bar, source
    /// IPA, tweaks and log.
    func test_every_control_on_the_app_pane_has_a_name() async throws {
        try AccessibilityTree.requireServed()
        let store = configStore()
        let layout = laidOutForMeasurement(
            ContentView(store: store, selection: .app("example"), adminShell: NoAdminShell()), width: 1100, height: 900
        )
        // On screen: the Accessibility API lists only windows that are.
        layout.window.orderFront(nil)
        let deadline = Date().addingTimeInterval(5)
        while store.apps.isEmpty, Date() < deadline { await wait(seconds: 0.02) }
        await wait(seconds: 0.5)
        layout.root.layoutSubtreeIfNeeded()

        let names = try controls(in: layout.window).map(\.name)
        for expected in ["Edit…", "Build & Install", "Build options", "Choose IPA…",
                         "Get latest from App Store", "Attach…"] {
            XCTAssertTrue(names.contains(expected), "no control named \(expected): \(names)")
        }
        let unnamed = try unnamedControls(in: layout.window)
        XCTAssertEqual(unnamed, [])
    }

    /// The URL form, opened explicitly: this was written for it, and a sheet
    /// with no `initialMode` now opens on Search (covered below).
    func test_every_control_on_the_add_app_sheet_has_a_name() throws {
        try AccessibilityTree.requireServed()
        let config = configStore()
        let hosted = hosted(
            AddAppSheet(store: config, appStore: AppStoreStore(runner: config.runner, shell: NoAdminShell()), initialMode: .url),
            width: 480
        )
        settle(hosted)
        let names = try controls(in: hosted.window).map(\.name)
        XCTAssertTrue(names.contains("Cancel"), "\(names)")
        XCTAssertTrue(names.contains("Add"), "\(names)")
        XCTAssertTrue(
            try AccessibilityTree.nodes(in: hosted.window).contains {
                $0.role == kAXRadioButtonRole && $0.name == AddAppSheet.Mode.url.rawValue
                    && ($0.value as? NSNumber)?.boolValue == true
            },
            "premise: the URL form"
        )
        let unnamed = try unnamedControls(in: hosted.window)
        XCTAssertEqual(unnamed, [])
    }

    /// Search with results on screen: each row, the store picker, Try Again's
    /// absence aside, every control is named.
    func test_every_control_on_add_app_search_with_results_has_a_name() async throws {
        try AccessibilityTree.requireServed()
        let result = #"{"type":"appstore-search-result","track_id":100000001,"bundle_id":"com.example.app","name":"Example 1","developer":"Dev 1","version":"1.0","price":0,"formatted_price":"Free","currency":"USD","artwork_url":null,"icon_path":null,"configured_as":"example"}"#
        let end = #"{"type":"appstore-search-end","term":"example","country":"US","count":1}"#
        let cli = try ScriptCLI("printf '%s\\n' '\(result)' '\(end)'", test: self)
        let config = ConfigStore(runner: cli.runner, notify: { _ in })
        let appStore = AppStoreStore(runner: cli.runner, shell: NoAdminShell())
        let hosted = hosted(AddAppSheet(store: config, appStore: appStore), width: 480)
        settle(hosted)
        appStore.search.update(term: "example")
        await eventually({ appStore.search.state != .idle && !appStore.search.isSearching }, timeout: 10,
                         "the search's results")
        settle(hosted)
        let names = try controls(in: hosted.window).map(\.name)
        XCTAssertTrue(names.contains("Example 1, Dev 1, Free, Added"), "\(names)")
        let unnamed = try unnamedControls(in: hosted.window)
        XCTAssertEqual(unnamed, [])
    }

    func test_every_control_on_canisters_results_has_a_name() async throws {
        try AccessibilityTree.requireServed()
        let canister = CanisterStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        let result = #"{"type":"canister-result","package_id":"com.example.ctweak","name":"C Tweak","version":"1.0","description":"x","author":"a","section":"Tweaks","repository_id":"example-repo","price":"Free","size_bytes":100,"depiction":null}"#
        _ = try await canister.search(query: "x", argsOverride: ["-c", "printf '%s\\n' '\(result)'"])
        let hosted = hosted(CanisterView(store: canister), size: NSSize(width: 800, height: 400))
        settle(hosted)
        let names = try controls(in: hosted.window).map(\.name)
        XCTAssertTrue(names.contains("Fetch"), "\(names)")
        let unnamed = try unnamedControls(in: hosted.window)
        XCTAssertEqual(unnamed, [])
    }

    func test_every_control_on_the_setup_screen_has_a_name() throws {
        try AccessibilityTree.requireServed()
        for need in [SetupNeed.cli, .config(path: "/x/config.yaml")] {
            let hosted = hosted(
                SetupView(need: need, isChecking: false, onCheckAgain: {},
                          accountSetup: AccountSetupStore(
                              runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/false"))
                          )),
                size: NSSize(width: 700, height: 900)
            )
            settle(hosted)
            let names = try controls(in: hosted.window).map(\.name)
            XCTAssertTrue(names.contains("Check again"), "\(need): \(names)")
            let unnamed = try unnamedControls(in: hosted.window)
            XCTAssertEqual(unnamed, [], "\(need)")
        }
    }

    // MARK: - The Attach sheet's checkboxes

    /// The per-tweak checkbox was an `AXImage` — VoiceOver could read that it
    /// was "Selected" and do nothing about it. It is a checkbox now: named for
    /// the tweak, carrying its state, and pressable.
    func test_each_tweak_in_the_attach_sheet_is_a_checkbox_voiceover_can_toggle() async throws {
        try AccessibilityTree.requireServed()
        let store = configStore(tweaks: ["a.deb"])
        _ = try await store.loadApps()
        let app = try XCTUnwrap(store.apps.first)
        let hosted = hosted(
            AttachTweaksSheet(store: store, app: app, tweaksStore: await tweakLibrary()),
            width: 480
        )
        settle(hosted, seconds: 0.8)

        func checkbox(_ name: String) throws -> AccessibilityTree.Node? {
            try AccessibilityTree.nodes(in: hosted.window).first { $0.role == kAXCheckBoxRole && $0.name.contains(name) }
        }
        func isOn(_ node: AccessibilityTree.Node?) -> Bool? { (node?.value as? NSNumber)?.boolValue }

        // Each read is its own statement, never an `XCTAssert…`/`XCTUnwrap`
        // argument: a skip thrown in there is caught by XCTest and recorded
        // as a failure (see `AccessibilityTree.requireServed`).
        let alpha = try checkbox("Alpha Tweak")
        let attached = try XCTUnwrap(alpha, "no checkbox for the attached tweak")
        XCTAssertEqual(isOn(attached), true, "already attached reads as checked")
        let bravo = try checkbox("Bravo Tweak")
        let other = try XCTUnwrap(bravo, "no checkbox for the library tweak")
        XCTAssertEqual(isOn(other), false)

        AccessibilityTree.press(other)
        settle(hosted)
        let pressed = try checkbox("Bravo Tweak")
        XCTAssertEqual(isOn(pressed), true, "pressing it checks it")
        let after = try controls(in: hosted.window).map(\.name)
        XCTAssertTrue(after.contains("Attach 1"), "and the sheet counts it: \(after)")
        let unnamed = try unnamedControls(in: hosted.window)
        XCTAssertEqual(unnamed, [])
    }
}
