import SwiftUI
import XCTest
@testable import Graft

/// The Add App sheet's delivery picker, before the user has ever added an app.
///
/// It started on Sideload while the README calls LiveContainer delivery the
/// recommended one. With nothing remembered (`lastDeliveryMode` unset) it now
/// starts on LiveContainer; once an add has gone through, the remembered
/// choice wins, as before.
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
    private func selectedDelivery(remembered: String?) throws -> String? {
        try AccessibilityTree.requireServed()
        let suiteName = "AddAppDeliveryDefaultTests.\(UUID().uuidString)"
        guard let defaults = UserDefaults(suiteName: suiteName) else {
            XCTFail("could not create an isolated UserDefaults suite")
            return nil
        }
        defer { defaults.removePersistentDomain(forName: suiteName) }
        if let remembered { defaults.set(remembered, forKey: "lastDeliveryMode") }

        let store = ConfigStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/true")), notify: { _ in })
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
    func test_with_nothing_remembered_the_sheet_starts_on_livecontainer() throws {
        let selected = try selectedDelivery(remembered: nil)
        XCTAssertEqual(selected, DeliveryMode.livecontainer.displayName)
    }

    func test_a_remembered_choice_still_wins() throws {
        let selected = try selectedDelivery(remembered: "sideload")
        XCTAssertEqual(selected, DeliveryMode.sideload.displayName)
    }

    func test_the_default_is_the_recommended_mode() {
        XCTAssertEqual(AddAppSheet.defaultDelivery, .livecontainer)
    }
}
