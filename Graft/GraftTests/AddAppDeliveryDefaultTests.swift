import SwiftUI
import XCTest
@testable import Graft

/// The Add App sheet's delivery picker, before the user has ever added an app.
///
/// With nothing remembered (`lastDeliveryMode` unset) it starts on the
/// account's default (`AddAppSheet.defaultDelivery(for:)`): Sideload on a paid
/// account, as the README, the wiki and the setup screen say (decided for
/// 2.3.1), and LiveContainer on a free one or before `--list` has said
/// which. Once an add has gone through, the remembered choice wins, as before.
///
/// Read off the segmented picker's radio buttons in the accessibility tree,
/// which skips when macOS is not serving it (`AccessibilityTree`). Each render
/// gets a throwaway `UserDefaults` suite through `.defaultAppStorage`, so the
/// test host's own preferences are never read or written.
///
/// The sheet is opened in URL mode explicitly. This was written for the URL
/// form's picker, and once Search became the mode the sheet opens in, a sheet
/// built with no `initialMode` drew Search instead.
@MainActor
final class AddAppDeliveryDefaultTests: XCTestCase {
    private func selectedDelivery(remembered: String?, account: AccountMode? = nil) async throws -> String? {
        try AccessibilityTree.requireServed()
        let suiteName = "AddAppDeliveryDefaultTests.\(UUID().uuidString)"
        guard let defaults = UserDefaults(suiteName: suiteName) else {
            XCTFail("could not create an isolated UserDefaults suite")
            return nil
        }
        defer { defaults.removePersistentDomain(forName: suiteName) }
        if let remembered { defaults.set(remembered, forKey: "lastDeliveryMode") }

        let store: ConfigStore
        if let account {
            store = try await accountTestStore([accountEventLine(account.rawValue)])
            XCTAssertEqual(store.account, account, "premise: the store knows the account")
        } else {
            store = ConfigStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/true")), notify: { _ in })
        }
        let sheet = AddAppSheet(store: store, appStore: AppStoreStore(runner: store.runner, shell: NoAdminShell()), initialMode: .url)
            .defaultAppStorage(defaults)
        let box = hosted(sheet, width: 480)
        settle(box)
        let radios = try AccessibilityTree.nodes(in: box.window).filter {
            $0.role == kAXRadioButtonRole
                && [DeliveryMode.sideload.displayName, DeliveryMode.livecontainer.displayName]
                    .contains($0.name)
        }
        XCTAssertEqual(radios.count, 2, "premise: the delivery picker's two segments")
        let selectedMode = try AccessibilityTree.nodes(in: box.window).first {
            $0.role == kAXRadioButtonRole && ($0.value as? NSNumber)?.boolValue == true
                && AddAppSheet.Mode.allCases.map(\.rawValue).contains($0.name)
        }?.name
        XCTAssertEqual(selectedMode, AddAppSheet.Mode.url.rawValue, "premise: the URL form")
        return radios.first { ($0.value as? NSNumber)?.boolValue == true }?.name
    }

    // Read into a constant first, not inside `XCTAssertEqual`: a skip thrown
    // from an assertion's argument is recorded as a failure.
    func test_with_nothing_remembered_and_no_account_known_the_sheet_starts_on_livecontainer() async throws {
        let selected = try await selectedDelivery(remembered: nil)
        XCTAssertEqual(selected, DeliveryMode.livecontainer.displayName)
    }

    func test_with_nothing_remembered_a_paid_account_starts_on_sideload() async throws {
        let selected = try await selectedDelivery(remembered: nil, account: .paid)
        XCTAssertEqual(selected, DeliveryMode.sideload.displayName)
    }

    func test_a_remembered_choice_still_wins() async throws {
        let selected = try await selectedDelivery(remembered: "livecontainer", account: .paid)
        XCTAssertEqual(selected, DeliveryMode.livecontainer.displayName)
        let other = try await selectedDelivery(remembered: "sideload")
        XCTAssertEqual(other, DeliveryMode.sideload.displayName)
    }

    /// The sheet can open before `--list` has said which account this is. An
    /// untouched picker follows the account once it's known, rather than
    /// keeping the guess it opened with.
    func test_an_untouched_picker_follows_an_account_that_arrives_late() async throws {
        try AccessibilityTree.requireServed()
        let suiteName = "AddAppDeliveryDefaultTests.late.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let body = "printf '%s\\n' '\(accountEventLine("paid"))'"
        let store = ConfigStore(runner: accountTestShell, cliArgs: ["-c", body], notify: { _ in })
        let box = hosted(
            AddAppSheet(store: store, appStore: AppStoreStore(runner: store.runner, shell: NoAdminShell()),
                        initialMode: .url)
                .defaultAppStorage(defaults),
            width: 480
        )
        settle(box)
        func selected() throws -> String? {
            try AccessibilityTree.nodes(in: box.window).first {
                $0.role == kAXRadioButtonRole && ($0.value as? NSNumber)?.boolValue == true
                    && [DeliveryMode.sideload.displayName, DeliveryMode.livecontainer.displayName]
                        .contains($0.name)
            }?.name
        }
        let before = try selected()
        XCTAssertEqual(before, DeliveryMode.livecontainer.displayName, "premise: not known yet")

        _ = try await store.loadApps()
        XCTAssertEqual(store.account, .paid, "premise")
        settle(box)

        let after = try selected()
        XCTAssertEqual(after, DeliveryMode.sideload.displayName)
    }

    func test_the_default_follows_the_account() {
        XCTAssertEqual(AddAppSheet.defaultDelivery(for: .paid), .sideload)
        XCTAssertEqual(AddAppSheet.defaultDelivery(for: .free), .livecontainer)
        XCTAssertEqual(AddAppSheet.defaultDelivery(for: nil), .livecontainer,
                       "not known: the one mode every account can build")
    }
}
