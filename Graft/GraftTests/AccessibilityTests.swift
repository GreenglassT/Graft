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

    /// Controls VoiceOver can't name for what they do: no name at all, or
    /// only their glyph's (`glyphNames`), "Trash" for a delete.
    private func unnamedControls(in window: NSWindow) throws -> [String] {
        let glyphs = try Self.glyphNames()
        return try AccessibilityTree.nodes(in: window)
            .filter { Self.controlRoles.contains($0.role) }
            .filter { !($0.subrole.map(Self.windowChrome.contains) ?? false) }
            .filter { node in
                if node.name.isEmpty { return true }
                if let symbol = node.identifier, glyphs.buttons[symbol] == node.name { return true }
                return node.role == kAXMenuButtonRole && glyphs.menus.contains(node.name)
            }
            .map { "\($0.role)\($0.subrole.map { "/\($0)" } ?? "") \($0.name)" }
    }

    /// What VoiceOver reads for a control labeled with a glyph alone: every
    /// symbol in the vocabulary (`Symbols.all`) in a bare button and a bare
    /// menu, read back. A button keys it by its symbol (`AXIdentifier`), so
    /// "Add" on a glyph is caught and a button titled Add isn't; a menu
    /// carries no identifier, so its glyph names ("More") are a set.
    private static var glyphs: (buttons: [String: String], menus: Set<String>)?

    private static func glyphNames() throws -> (buttons: [String: String], menus: Set<String>) {
        if let glyphs { return glyphs }
        let box = hosted(BareGlyphs(), size: NSSize(width: 520, height: 420))
        defer { takeDown(box) }
        settle(box, seconds: 0.3)
        let nodes = try AccessibilityTree.nodes(in: box.window)
        var buttons: [String: String] = [:]
        for node in nodes where node.role == kAXButtonRole {
            if let symbol = node.identifier, !node.name.isEmpty { buttons[symbol] = node.name }
        }
        let menus = Set(nodes.filter { $0.role == kAXMenuButtonRole && !$0.name.isEmpty }.map(\.name))
        XCTAssertGreaterThanOrEqual(buttons.count, 30, "premise: the bare glyphs were read: \(buttons)")
        XCTAssertEqual(buttons[Symbols.delete.name], "Trash", "premise: a bare trash reads Trash")
        XCTAssertTrue(menus.contains("More"), "premise: a bare ellipsis menu reads More: \(menus)")
        let found = (buttons, menus)
        glyphs = found
        return found
    }

    /// Each vocabulary symbol as a button's whole label, and a menu's.
    private struct BareGlyphs: View {
        var body: some View {
            LazyVGrid(columns: Array(repeating: GridItem(.fixed(44)), count: 10)) {
                ForEach(Array(Symbols.all.enumerated()), id: \.offset) { _, symbol in
                    Button {} label: { symbol.image }
                    Menu { Button("One") {} } label: { symbol.image }
                        .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
                }
            }
            .padding()
        }
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
                         "Get latest from App Store", "Attach…", "Add App"] {
            XCTAssertTrue(names.contains(expected), "no control named \(expected): \(names)")
        }
        XCTAssertTrue(names.contains { $0.hasPrefix("Detach ") }, "no Detach for the attached tweak: \(names)")
        let unnamed = try unnamedControls(in: layout.window)
        XCTAssertEqual(unnamed, [])
    }

    /// The Tweaks pane's glyph-only buttons: VoiceOver read them by their
    /// symbols' names, "Arrow Up Circle" for Check for Updates and "Trash" on
    /// every row. Each says what it does, and a row's which tweak.
    func test_the_tweaks_panes_icon_buttons_say_what_they_do() async throws {
        try AccessibilityTree.requireServed()
        let library = await tweakLibrary()
        let update = #"{"type":"tweak-update","filename":"a.deb","package_id":"com.example.alpha","installed":"1.0","latest":"1.1","source":"canister","update_available":true}"#
        let end = #"{"type":"tweak-updates-end","checked":2,"outdated":1,"unchecked":0}"#
        await library.checkForUpdates(argsOverride: ["-c", "printf '%s\\n' '\(update)' '\(end)'"])
        XCTAssertEqual(library.outdatedCount, 1, "premise: Alpha Tweak has an update")
        let pane = hosted(TweaksView(store: library, config: configStore()), size: NSSize(width: 760, height: 560))
        settle(pane)

        let names = try controls(in: pane.window).map(\.name)
        for expected in [TweaksView.checkUpdatesLabel, TweaksView.reloadLabel,
                         "Update Alpha Tweak", "Delete Alpha Tweak", "Delete Bravo Tweak"] {
            XCTAssertTrue(names.contains(expected), "no control named \(expected): \(names)")
        }
        for glyph in ["Arrow Up Circle", "Trash"] {
            XCTAssertFalse(names.contains(glyph), "a control is still read as \(glyph): \(names)")
        }
        XCTAssertTrue(names.contains("Orphans only"), "\(names)")
        XCTAssertEqual(try unnamedControls(in: pane.window), [])
    }

    /// The Cache pane's three row controls, each a glyph: VoiceOver read
    /// the delete as "Trash".
    func test_the_cache_rows_controls_say_what_they_do() async throws {
        try AccessibilityTree.requireServed()
        let row = stagedIPAStreamLine(bundleId: "com.example.app", version: "1.0", displayName: "Example")
        let cli = try ScriptCLI("printf '%s\\n' '\(row)' '\(stagedIPAsEndLine(1))'", test: self)
        let store = StagedIPAStore(runner: cli.runner)
        let pane = hosted(CachePaneView(staged: store, onInstallToMac: { _ in MacInstall(handed: nil, ending: .handedOnly) }, onAddAsApp: { _ in },
                                        onDelete: { _, _ in .deleted }),
                          size: NSSize(width: 760, height: 520))
        await eventually({ store.hasLoaded }, "premise: the cache was read")
        settle(pane)

        let names = try controls(in: pane.window).map(\.name)
        for expected in ["Install Example on this Mac", "Add Example as an app", "Delete com.example.app_1.0.ipa"] {
            XCTAssertTrue(names.contains(expected), "no control named \(expected): \(names)")
        }
        XCTAssertEqual(try unnamedControls(in: pane.window), [])
    }

    /// The Builds row's menu, a glyph: VoiceOver read it as "More". And its
    /// column's header, which had no title, is a sort button with no name.
    func test_the_builds_row_menu_says_what_it_is() async throws {
        try AccessibilityTree.requireServed()
        let cli = try ScriptCLI("printf '%s\\n' '\(BuildFixtures.buildLine())'", test: self)
        let builds = BuildsStore(runner: cli.runner)
        _ = try await builds.load()
        let pane = hosted(BuildsView(store: builds, config: configStore(), artwork: ArtworkStore(runner: cli.runner),
                                     staged: StagedIPAStore(runner: cli.runner), devices: DevicesStore(runner: cli.runner),
                                     settings: SettingsStore(runner: cli.runner)),
                          size: NSSize(width: 900, height: 500))
        settle(pane)

        let names = try controls(in: pane.window).map(\.name)
        XCTAssertTrue(names.contains("Build actions"), "\(names)")
        XCTAssertEqual(try unnamedControls(in: pane.window), [])
    }

    /// The sidebar's retry on a phone it couldn't read, and an error banner's
    /// dismiss: glyphs, read as "Refresh" and "Close".
    func test_the_sidebar_retry_and_an_error_banners_dismiss_say_what_they_do() throws {
        try AccessibilityTree.requireServed()
        let line = #"{"type":"device-error","udid":"00008030-0000000000000003","error":"the device is locked"}"#
        guard case let .deviceError(failure)? = line.buildEvents().first else {
            return XCTFail("fixture did not decode: \(line)")
        }
        let row = hosted(UndescribableDeviceRow(failure: failure, isRetrying: false, onRetry: {}), width: 260)
        settle(row)
        XCTAssertTrue(try controls(in: row.window).map(\.name).contains("Read this device again"))
        XCTAssertEqual(try unnamedControls(in: row.window), [])

        let banner = hosted(ErrorBanner(message: "Example failure", onDismiss: {}), width: 420)
        settle(banner)
        XCTAssertTrue(try controls(in: banner.window).map(\.name).contains("Dismiss"))
        XCTAssertEqual(try unnamedControls(in: banner.window), [])
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

/// `AccessibilityTree.requireServed()`'s memory of whether macOS serves
/// this process's tree, with a stand-in probe: no window, no tree needed.
///
/// A "not served" is never remembered. In the 2.2 runs one miss at the
/// first on-screen test skipped every one after it (109), though the tree
/// was served a moment later. So a miss is asked again a few times, briefly,
/// and asked again at the next test; only "served" is remembered.
///
/// Every call that should not skip is inside `XCTAssertNoThrow`: a skip
/// leaving the test method is reported as a skip, and would hide a
/// regression here behind a green run.
@MainActor
final class AccessibilityProbeTests: XCTestCase {
    private typealias Memory = AccessibilityTree.ServedMemory

    func test_a_tree_not_served_at_first_then_served_is_found() {
        var memory = Memory(tries: 3, pause: 0.5)
        var answers = [false, true]
        var waits: [TimeInterval] = []
        XCTAssertNoThrow(try AccessibilityTree.requireServed(&memory, probe: { answers.removeFirst() },
                                                             wait: { waits.append($0) }))
        XCTAssertEqual(answers, [], "asked twice")
        XCTAssertEqual(waits, [0.5], "with a pause between, none before the first ask")
    }

    func test_a_tree_never_served_skips_after_the_bounded_tries_and_is_asked_again_next_time() {
        var memory = Memory(tries: 3, pause: 0.5)
        var asked = 0
        var waits: [TimeInterval] = []
        for run in 1...2 {
            XCTAssertThrowsError(
                try AccessibilityTree.requireServed(&memory, probe: { asked += 1; return false },
                                                    wait: { waits.append($0) })
            ) { error in
                XCTAssertTrue(error is XCTSkip, "a skip, not a failure: \(error)")
            }
            XCTAssertEqual(asked, 3 * run, "three tries, every time: a miss is not remembered")
            XCTAssertEqual(waits.count, 2 * run, "no pause after the last try")
        }
    }

    func test_a_served_tree_is_remembered() {
        var memory = Memory(tries: 3, pause: 0.5)
        var asked = 0
        XCTAssertNoThrow(try AccessibilityTree.requireServed(&memory, probe: { asked += 1; return true }, wait: { _ in }))
        XCTAssertNoThrow(try AccessibilityTree.requireServed(&memory, probe: { asked += 1; return false }, wait: { _ in }))
        XCTAssertEqual(asked, 1, "asked once, then remembered")
    }

    /// `nodes(in:)` finding its window missing forgets a "served": the tree
    /// can go away mid-run, and the next ask probes again.
    func test_forgetting_a_served_tree_asks_again() {
        var memory = Memory(tries: 2, pause: 0.5)
        var asked = 0
        XCTAssertNoThrow(try AccessibilityTree.requireServed(&memory, probe: { asked += 1; return true }, wait: { _ in }))
        memory.forget()
        XCTAssertThrowsError(
            try AccessibilityTree.requireServed(&memory, probe: { asked += 1; return false }, wait: { _ in })
        )
        XCTAssertEqual(asked, 3)
    }

    /// Bounded: a run on a Mac that never serves the tree spends about a
    /// second per on-screen test on it, not more.
    func test_the_real_memory_asks_a_few_times_briefly() {
        let memory = AccessibilityTree.ServedMemory.standard
        XCTAssertEqual(memory.tries, 3)
        XCTAssertEqual(memory.pause, 0.5)
    }
}

/// Every `Button` and `Menu` in the app whose label is a glyph alone says what
/// it does in an `.accessibilityLabel`: VoiceOver otherwise reads the
/// symbol's own name ("Trash", "Arrow Up Circle", "More"). Read from the
/// source, with comments and strings blanked, so it covers the controls no
/// test hosts; `AccessibilityTests` reads the ones it hosts from the tree.
@MainActor
final class IconButtonScanTests: XCTestCase {
    struct Scan {
        /// Glyph-only buttons and menus found.
        var glyphOnly = 0
        /// Those with no `.accessibilityLabel` in their modifiers: each
        /// one's line, trimmed.
        var unlabeled: [String] = []
    }

    static func scan(_ source: String) -> Scan {
        let code = Array(NoRealDefaultsTests.blankCommentsAndStrings(source))
        let lines = source.components(separatedBy: "\n")
        func at(_ i: Int) -> Character? { i >= 0 && i < code.count ? code[i] : nil }
        func isWord(_ c: Character?) -> Bool { c.map { $0.isLetter || $0.isNumber || $0 == "_" } ?? false }
        func space(_ i: Int) -> Int { var j = i; while let c = at(j), c.isWhitespace { j += 1 }; return j }
        func word(_ i: Int) -> (text: String, end: Int) {
            var j = i, text = ""
            while isWord(at(j)), let c = at(j) { text.append(c); j += 1 }
            return (text, j)
        }
        /// Just past the bracket that closes the one at `i`.
        func past(_ i: Int) -> Int? {
            guard let open = at(i) else { return nil }
            let close: Character = open == "(" ? ")" : (open == "{" ? "}" : "]")
            var depth = 0, j = i
            while let c = at(j) {
                if c == open { depth += 1 }
                if c == close { depth -= 1; if depth == 0 { return j + 1 } }
                j += 1
            }
            return nil
        }
        func text(_ range: Range<Int>) -> String { String(code[range]) }
        let glyph = try! NSRegularExpression(pattern: #"\b(Symbols|Marks)\.\w+\.image\b|\bsymbol\.image\b|\bImage\("#)
        let words = try! NSRegularExpression(pattern: #"\bText\(|\.label\(|\bLabel\(|\bTextField\("#)
        func matches(_ regex: NSRegularExpression, _ s: String) -> Bool {
            regex.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) != nil
        }

        var scan = Scan()
        var i = 0
        while i < code.count {
            guard isWord(at(i)) else { i += 1; continue }
            let (name, end) = word(i)
            defer { i = end }
            guard name == "Button" || name == "Menu", !isWord(at(i - 1)), at(i - 1) != "." else { continue }
            // Its arguments, then its closures: `{ … }` and `label: { … }`.
            var j = space(end)
            var arguments: String?
            if at(j) == "(" {
                guard let k = past(j) else { continue }
                arguments = text(j + 1 ..< k - 1)
                j = k
            }
            var closures: [String] = []
            var label: String?
            while true {
                let k = space(j)
                if at(k) == "{", let close = past(k) {
                    closures.append(text(k + 1 ..< close - 1))
                    j = close
                    continue
                }
                let (next, after) = word(k)
                if next == "label", at(space(after)) == ":", at(space(space(after) + 1)) == "{",
                   let close = past(space(space(after) + 1)) {
                    label = text(space(space(after) + 1) + 1 ..< close - 1)
                    j = close
                    continue
                }
                break
            }
            // A title as the first argument (`Button("Add")`, `Menu(title)`)
            // is a name. Otherwise the label is `label:`'s, or a button's
            // one closure after `action:`.
            let firstLabel = arguments.flatMap { args -> String? in
                let trimmed = args.trimmingCharacters(in: .whitespacesAndNewlines)
                guard let colon = trimmed.firstIndex(of: ":") else { return nil }
                let head = trimmed[..<colon]
                return head.allSatisfy { $0.isLetter || $0.isNumber || $0 == "_" } ? String(head) : nil
            }
            if let arguments, !arguments.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
               !["action", "role"].contains(firstLabel ?? "") {
                continue
            }
            if label == nil, name == "Button", arguments != nil, closures.count == 1 { label = closures[0] }
            guard let label, matches(glyph, label), !matches(words, label) else { continue }
            scan.glyphOnly += 1
            // Its modifiers, to the end of the chain.
            var modifiers: [String] = []
            var m = space(j)
            while at(m) == "." {
                let (modifier, after) = word(m + 1)
                guard !modifier.isEmpty else { break }
                modifiers.append(modifier)
                var n = after
                if at(n) == "(", let close = past(n) { n = close }
                while at(space(n)) == "{", let close = past(space(n)) { n = close }
                m = space(n)
            }
            // A Menu's name is its label's: VoiceOver reads the glyph's
            // whatever `.accessibilityLabel` says (measured on the Builds
            // row's), so a Menu needs words in its label.
            if name == "Menu" || !modifiers.contains("accessibilityLabel") {
                let line = code[..<i].filter { $0 == "\n" }.count
                scan.unlabeled.append(lines[line].trimmingCharacters(in: .whitespaces))
            }
        }
        return scan
    }

    func test_the_scan_finds_a_glyph_only_button_without_a_label() {
        let sample = #"""
        struct Sample: View {
            var body: some View {
                Button { go() } label: { Symbols.delete.image }
                Button { go() } label: { Symbols.delete.image }
                    .help("Move it to the Trash")
                    .accessibilityLabel("Delete it")
                Button(action: go) { Symbols.refresh.image(size: 10).foregroundStyle(.red) }
                    .buttonStyle(.plain)
                Button("Add") { go() }
                Button(Self.title) { go() }
                Button { go() } label: { Symbols.add.label("Attach…") }
                Button { go() } label: { HStack { Text("Dry run"); Symbols.clearOption.image(size: 8) } }
                Menu { Button("One") {} } label: { Symbols.rowMenu.image }
                    .menuStyle(.borderlessButton)
                Menu { Button("One") {} } label: { Symbols.rowMenu.image }.accessibilityLabel("Actions")
                Menu { Button("One") {} } label: { Label { Text("Actions") } icon: { Symbols.rowMenu.image } }
                // Button { go() } label: { Symbols.delete.image }
                Text("Button { go() } label: { Symbols.delete.image }")
                Button(role: .destructive) { go() } label: { if busy { ProgressView() } else { Symbols.delete.image } }
                Button(action: go) { symbol.image }
                    .contextMenu { Button("Copy") {} }
                    .accessibilityLabel(name)
            }
        }
        """#
        let scan = Self.scan(sample)
        XCTAssertEqual(scan.unlabeled, [
            "Button { go() } label: { Symbols.delete.image }",
            "Button(action: go) { Symbols.refresh.image(size: 10).foregroundStyle(.red) }",
            #"Menu { Button("One") {} } label: { Symbols.rowMenu.image }"#,
            #"Menu { Button("One") {} } label: { Symbols.rowMenu.image }.accessibilityLabel("Actions")"#,
            "Button(role: .destructive) { go() } label: { if busy { ProgressView() } else { Symbols.delete.image } }",
        ])
        XCTAssertEqual(scan.glyphOnly, 7)
    }

    func test_every_glyph_only_button_in_the_app_has_an_accessibility_label() throws {
        let sources = try ObservableStoresTests.appSources()
        XCTAssertGreaterThan(sources.count, 50, "premise: the app's sources were found")
        var glyphOnly = 0
        var unlabeled: [String] = []
        for (path, source) in sources {
            let scan = Self.scan(source)
            glyphOnly += scan.glyphOnly
            unlabeled += scan.unlabeled.map { "\(path): \($0)" }
        }
        XCTAssertGreaterThanOrEqual(glyphOnly, 12, "premise: the app's glyph-only controls were found")
        XCTAssertEqual(unlabeled, [], """
            A Button or Menu labeled with a glyph alone has no .accessibilityLabel, so \
            VoiceOver reads the symbol's name. Say what it does, and for a row's, which item:
            \(unlabeled.joined(separator: "\n"))
            """)
    }
}
