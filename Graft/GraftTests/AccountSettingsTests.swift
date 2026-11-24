import SwiftUI
import XCTest
@testable import Graft

/// Settings › Account: the account, and the way to the other one, in the app.
@MainActor
final class AccountSettingsTests: XCTestCase {
    private func buttons(account: AccountMode, setup: AccountSetupStore?) throws -> [String] {
        try AccessibilityTree.requireServed()
        let pane = SettingsView(
            store: SettingsStore(runner: accountTestShell),
            devicesStore: DevicesStore(runner: accountTestShell),
            appStore: AppStoreStore(runner: accountTestShell, shell: NoAdminShell()),
            account: account, accountSetup: setup
        )
        let box = hosted(pane, size: NSSize(width: 900, height: 1400))
        settle(box)
        return try AccessibilityTree.nodes(in: box.window)
            .filter { $0.role == kAXButtonRole }.map(\.name)
    }

    func test_a_free_account_offers_to_add_a_paid_one() throws {
        let names = try buttons(account: .free, setup: AccountSetupStore(runner: accountTestShell))
        XCTAssertTrue(names.contains(AccountMode.free.changeLabel), "\(names)")
        XCTAssertFalse(names.contains(AccountMode.paid.changeLabel), "\(names)")
    }

    func test_a_paid_account_offers_to_switch_to_a_free_apple_id() throws {
        let names = try buttons(account: .paid, setup: AccountSetupStore(runner: accountTestShell))
        XCTAssertTrue(names.contains(AccountMode.paid.changeLabel), "\(names)")
        XCTAssertFalse(names.contains(AccountMode.free.changeLabel), "\(names)")
    }

    /// The yearly renewal: a paid account replaces its certificate and profile
    /// here, and a free one has nothing to replace.
    func test_only_a_paid_account_offers_to_replace_its_certificate() throws {
        let paid = try buttons(account: .paid, setup: AccountSetupStore(runner: accountTestShell))
        XCTAssertTrue(paid.contains(AccountMode.replaceLabel), "\(paid)")
        let free = try buttons(account: .free, setup: AccountSetupStore(runner: accountTestShell))
        XCTAssertFalse(free.contains(AccountMode.replaceLabel), "\(free)")
    }

    func test_without_the_flows_there_is_no_button() throws {
        let names = try buttons(account: .free, setup: nil)
        XCTAssertFalse(names.contains(AccountMode.free.changeLabel), "\(names)")
    }

    func test_the_labels_are_the_two_flows() {
        XCTAssertEqual(AccountMode.free.changeLabel, "Add a paid account…")
        XCTAssertEqual(AccountMode.paid.changeLabel, "Switch to a free Apple ID…")
    }

    /// The account copy used to say "run graft --init in Terminal".
    func test_nothing_about_the_account_sends_the_user_to_terminal() {
        for text in [AccountMode.free.detail, AccountMode.paid.detail,
                     PaidOnly.signingSettings, AccountMode.switchToFreeMessage] {
            XCTAssertFalse(text.contains("Terminal"), text)
            XCTAssertFalse(text.contains("graft --init"), text)
        }
    }

    /// After a switch: the list (which carries the account every pane gates
    /// on), the settings (the signing fields) and the checks are all re-read.
    func test_an_account_change_rereads_the_list_the_settings_and_the_checks() async throws {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-account-change-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let script = dir.appendingPathComponent("graft")
        try """
        #!/bin/sh
        case " $* " in
          *" --list "*) printf '%s\\n' '{"type":"account","account":"paid"}' ;;
          *" --settings "*) printf '%s\\n' '{"type":"settings","default_device_udid":null,"archive_keep":2,"signing_cert":"/x/AppleDevelopment.p12","signing_profile":"/x/wildcard.mobileprovision","signing_team_id":"ABCDE12345"}' ;;
          *" --check "*) printf '%s\\n' '{"type":"preflight-end","ok_count":4,"fail_count":0,"account":"paid"}' ;;
        esac
        exit 0
        """.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
        let runner = CLIRunner(executable: script)
        let config = ConfigStore(runner: runner, notify: { _ in })
        let settings = SettingsStore(runner: runner)

        await ContentView.afterAccountChange(config: config, settings: settings)

        XCTAssertEqual(config.account, .paid)
        XCTAssertEqual(settings.settings?.signingTeamId, "ABCDE12345")
        XCTAssertTrue(settings.hasChecked)
        XCTAssertEqual(settings.account, .paid)
    }

    // MARK: - While a setup writes, nothing offers to stop it (APP-1)

    private static let paidReview = #"{"type":"init-result","account":"paid","config":"/tmp/x/config.yaml","change":"replaced","dry_run":true,"summary":"Everything checks out.","team_id":"ABCDE12345","profile_name":"Test Wildcard","certificate":"Apple Development: Test Person (XYZ9876543)","device_count":1,"devices":["00008130-001A2B3C0123456D"],"expires":"2027-06-01T12:00:00Z","pinned":[],"sideloads":[],"steps":[],"warnings":["GRAFT_P12_PASS is set in your shell and overrides the Keychain."]}"#

    /// A graft that answers the review at once and takes `delay` seconds over
    /// the real run.
    private func graft(realDelay: Double, real: String = paidReview) throws -> CLIRunner {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-account-view-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let script = dir.appendingPathComponent("graft")
        try """
        #!/bin/sh
        IFS= read -r line
        case " $* " in
          *" --dry-run "*) printf '%s\\n' '\(Self.paidReview)'; exit 0 ;;
        esac
        sleep \(realDelay)
        printf '%s\\n' '\(real.replacingOccurrences(of: "\"dry_run\":true", with: "\"dry_run\":false"))'
        """.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
        return CLIRunner(executable: script)
    }

    private func reviewed(_ runner: CLIRunner) async -> AccountSetupStore {
        let store = AccountSetupStore(runner: runner)
        store.startReplace()
        store.form.p12Path = "/tmp/x/Certificates.p12"
        store.form.password = "correct horse battery"
        store.form.profilePath = "/tmp/x/Test.mobileprovision"
        await store.review()
        return store
    }

    private func sheetButtons(_ store: AccountSetupStore) throws -> [AccessibilityTree.Node] {
        let box = hosted(
            AccountSetupView(store: store, onFinished: {}, onCancel: {})
                .padding(Metrics.paneGutter).frame(width: 560),
            size: NSSize(width: 600, height: 700)
        )
        settle(box)
        return try AccessibilityTree.nodes(in: box.window).filter { $0.role == kAXButtonRole }
    }

    func test_while_a_replace_writes_cancel_is_a_disabled_setting_up_and_back_is_disabled() async throws {
        try AccessibilityTree.requireServed()
        let store = await reviewed(try graft(realDelay: 3))
        guard case .paidReview = store.step else { return XCTFail("\(store.step)") }

        let confirming = Task { await store.confirm() }
        let deadline = Date().addingTimeInterval(5)
        while !store.isWriting, Date() < deadline { try await Task.sleep(nanoseconds: 20_000_000) }
        let buttons = try sheetButtons(store)
        await confirming.value

        let byName = Dictionary(buttons.map { ($0.name, $0.enabled) }, uniquingKeysWith: { a, _ in a })
        XCTAssertEqual(byName[AccountSetupView.settingUpLabel], false, "\(byName)")
        XCTAssertNil(byName["Cancel"], "\(byName)")
        XCTAssertEqual(byName["Back"], false, "\(byName)")
        XCTAssertEqual(byName[AccountSetupView.setUpLabel], false, "\(byName)")
    }

    func test_on_the_review_cancel_and_back_are_there_before_set_up_is_pressed() async throws {
        try AccessibilityTree.requireServed()
        let store = await reviewed(try graft(realDelay: 0))
        let byName = Dictionary(try sheetButtons(store).map { ($0.name, $0.enabled) },
                                uniquingKeysWith: { a, _ in a })
        XCTAssertEqual(byName["Cancel"], true, "\(byName)")
        XCTAssertEqual(byName["Back"], true, "\(byName)")
        XCTAssertNil(byName[AccountSetupView.settingUpLabel], "\(byName)")
    }

    func test_the_review_and_the_done_screen_show_the_clis_warnings() async throws {
        try AccessibilityTree.requireServed()
        let store = await reviewed(try graft(realDelay: 0))
        for _ in 0..<2 {
            let box = hosted(AccountSetupView(store: store, onFinished: {}, onCancel: {}),
                             size: NSSize(width: 600, height: 700))
            settle(box)
            let shown = try AccessibilityTree.texts(in: box.window)
            XCTAssertTrue(shown.contains("GRAFT_P12_PASS is set in your shell and overrides the Keychain."),
                          "\(store.step): \(shown)")
            await store.confirm()
        }
    }

    // MARK: - No account change while a build runs (the critic's gap)

    func test_a_running_build_holds_the_account_buttons() throws {
        try AccessibilityTree.requireServed()
        let pane = SettingsView(
            store: SettingsStore(runner: accountTestShell),
            devicesStore: DevicesStore(runner: accountTestShell),
            appStore: AppStoreStore(runner: accountTestShell, shell: NoAdminShell()),
            account: .paid, accountSetup: AccountSetupStore(runner: accountTestShell),
            isBuilding: true
        )
        let box = hosted(pane, size: NSSize(width: 900, height: 1400))
        settle(box)
        let nodes = try AccessibilityTree.nodes(in: box.window)
        for label in [AccountMode.paid.changeLabel, AccountMode.replaceLabel] {
            let button = try XCTUnwrap(nodes.first { $0.role == kAXButtonRole && $0.name == label }, label)
            XCTAssertEqual(button.enabled, false, label)
        }
        XCTAssertTrue(try AccessibilityTree.texts(in: box.window).contains(AccountMode.waitForBuild))
    }

    func test_without_a_build_the_account_buttons_work() throws {
        try AccessibilityTree.requireServed()
        let pane = SettingsView(
            store: SettingsStore(runner: accountTestShell),
            devicesStore: DevicesStore(runner: accountTestShell),
            appStore: AppStoreStore(runner: accountTestShell, shell: NoAdminShell()),
            account: .paid, accountSetup: AccountSetupStore(runner: accountTestShell)
        )
        let box = hosted(pane, size: NSSize(width: 900, height: 1400))
        settle(box)
        let nodes = try AccessibilityTree.nodes(in: box.window)
        let button = try XCTUnwrap(nodes.first { $0.role == kAXButtonRole && $0.name == AccountMode.replaceLabel })
        XCTAssertEqual(button.enabled, true)
        XCTAssertFalse(try AccessibilityTree.texts(in: box.window).contains(AccountMode.waitForBuild))
    }

    // MARK: - An unreadable config.yaml: the error, its file, and the way to it

    func test_a_config_that_does_not_parse_offers_its_file_in_finder() async throws {
        try AccessibilityTree.requireServed()
        let unparsed = #"{"type":"settings","default_device_udid":null,"archive_keep":null,"signing_cert":null,"signing_profile":null,"signing_team_id":null,"parse_error":"failed to parse /Users/x/.config/graft/config.yaml: mapping values are not allowed here","paths_are_defaults":true,"paths":{"config_dir":"/Users/x/.config/graft","cache_dir":"/c","built_dir":"/b","state_dir":"/s","tweaks_dir":"/t"}}"#
        let store = SettingsStore(runner: accountTestShell)
        _ = try await store.loadSettings(argsOverride: ["-c", "printf '%s\\n' '\(unparsed)'"])
        let pane = SettingsView(
            store: store, devicesStore: DevicesStore(runner: accountTestShell),
            appStore: AppStoreStore(runner: accountTestShell, shell: NoAdminShell()), account: nil
        )
        let box = hosted(pane, size: NSSize(width: 900, height: 1600))
        try await Task.sleep(nanoseconds: 300_000_000)
        settle(box)
        let nodes = try AccessibilityTree.nodes(in: box.window)
        XCTAssertTrue(nodes.contains { $0.role == kAXButtonRole && $0.name == SettingsView.showConfigLabel },
                      "\(nodes.filter { $0.role == kAXButtonRole }.map(\.name))")
        XCTAssertTrue(try AccessibilityTree.texts(in: box.window).contains {
            $0.contains("/Users/x/.config/graft/config.yaml")
        })
        XCTAssertEqual(SettingsView.configFile(store.paths)?.path, "/Users/x/.config/graft/config.yaml")
    }

    // MARK: - Accessibility (APP-9) and the hold over a tools step (APP-7)

    func test_the_paid_answer_says_what_it_answers() throws {
        try AccessibilityTree.requireServed()
        let store = AccountSetupStore(runner: accountTestShell)
        store.begin()
        let box = hosted(AccountSetupView(store: store, onFinished: {}), size: NSSize(width: 600, height: 500))
        settle(box)
        let names = try AccessibilityTree.nodes(in: box.window).filter { $0.role == kAXButtonRole }.map(\.name)
        XCTAssertTrue(names.contains(AccountSetupView.paidAccessibilityLabel), "\(names)")
    }

    func test_a_missing_tool_waits_for_the_account_result_to_be_read() {
        let tools = SetupNeed.tools([MissingTool(name: "zsign", command: "brew install zsign")])
        XCTAssertEqual(ContentView.setupScreen(need: tools, holding: true), .config(path: nil))
        XCTAssertEqual(ContentView.setupScreen(need: tools, holding: false), tools)
    }
}

