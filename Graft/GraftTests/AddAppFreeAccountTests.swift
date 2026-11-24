import AppKit
import SwiftUI
import XCTest
@testable import Graft

// Add App on a free Apple ID: every add path sends LiveContainer, which is
// the only delivery the CLI accepts there, and a paid account keeps its
// remembered choice.

// MARK: - Add App, end to end

/// Both add paths, both accounts, through the real sheet: what reaches the
/// CLI's argv. The stub CLI captures every invocation's argv and answers
/// `--list` with the account under test and anything else with `app-added`.
@MainActor
final class AddAppFreeAccountTests: XCTestCase {
    private enum Path { case url, manual }

    private struct Outcome {
        let delivery: String?
        let rememberedAfter: String?
    }

    private static let sentinel = "__INVOCATION_END__"

    func test_an_add_on_a_free_account_is_always_livecontainer() {
        XCTAssertEqual(AddAppSheet.delivery(chosen: .sideload, account: .free), .livecontainer)
        XCTAssertEqual(AddAppSheet.delivery(chosen: .livecontainer, account: .free), .livecontainer)
        XCTAssertEqual(AddAppSheet.delivery(chosen: .sideload, account: .paid), .sideload)
        XCTAssertEqual(AddAppSheet.delivery(chosen: .sideload, account: nil), .sideload,
                       "not known disables nothing")
    }

    private func add(
        via path: Path, account: AccountMode, remembered: String?
    ) async throws -> Outcome {
        let suiteName = "AddAppFreeAccountTests.\(UUID().uuidString)"
        guard let defaults = UserDefaults(suiteName: suiteName) else {
            XCTFail("could not create an isolated UserDefaults suite")
            return Outcome(delivery: nil, rememberedAfter: nil)
        }
        defer { defaults.removePersistentDomain(forName: suiteName) }
        if let remembered { defaults.set(remembered, forKey: "lastDeliveryMode") }

        let dir = NSTemporaryDirectory()
        let capture = dir + "free_add_args_\(UUID().uuidString).txt"
        let script = dir + "free_add_cli_\(UUID().uuidString).sh"
        try """
        #!/bin/sh
        for arg in "$@"; do printf '%s\\n' "$arg" >> "\(capture)"; done
        printf '%s\\n' "\(Self.sentinel)" >> "\(capture)"
        case " $* " in
          *' --list '*) printf '%s\\n' '\(accountEventLine(account.rawValue))' ;;
          *) printf '%s\\n' '{"type":"app-added","name":"example","bundle_id":"com.example.app","display_name":"Example","delivery":"livecontainer"}' ;;
        esac
        """.write(toFile: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script)
        defer {
            try? FileManager.default.removeItem(atPath: script)
            try? FileManager.default.removeItem(atPath: capture)
        }

        let store = ConfigStore(runner: CLIRunner(executable: URL(fileURLWithPath: script)), notify: { _ in })
        _ = try await store.loadApps()
        XCTAssertEqual(store.account, account, "premise: the store knows the account")
        try? FileManager.default.removeItem(atPath: capture)

        var added = false
        let prefill = path == .manual
            ? AddAppSheet.Prefill(
                staged: try stagedIPARecord(bundleId: "com.example.app", displayName: "Example",
                                            filename: "com.example.app_1.0.ipa"),
                installedVersion: nil)
            : nil
        let sheet = AddAppSheet(
            store: store, appStore: AppStoreStore(runner: store.runner, shell: NoAdminShell()),
            onAdded: { _ in added = true }, prefill: prefill,
            initialMode: path == .url ? .url : AddAppSheet.defaultMode
        )
            .defaultAppStorage(defaults)
        let box = hosted(sheet, width: 480)
        settle(box)

        if path == .url {
            try typeURL("https://apps.apple.com/us/app/example/id100000001", in: box.window)
            settle(box, seconds: 0.2)
        }
        // "Add" sits in the footer's bottom-right corner; swept, as
        // `AddAppSheetPrefillTests` does, and kept clear of Cancel.
        for y in stride(from: CGFloat(4), through: 40, by: 4) {
            for x in stride(from: box.size.width - 70, through: box.size.width - 4, by: 6) {
                click(at: NSPoint(x: x, y: y), in: box.window)
            }
        }

        // `onAdded` runs in the same synchronous stretch of `submit()` as the
        // `lastDeliveryMode` write, so once it has fired, that write has too.
        let deadline = Date().addingTimeInterval(10)
        while !added, Date() < deadline {
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        XCTAssertTrue(added, "the add never completed")

        let lines = ((try? String(contentsOfFile: capture, encoding: .utf8)) ?? "")
            .split(separator: "\n").map(String.init)
        let first = Array(lines.prefix { $0 != Self.sentinel })
        XCTAssertTrue(first.contains(path == .url ? "--add-app" : "--add-app-manual"),
                      "premise: the add path under test ran: \(first)")
        let delivery = first.firstIndex(of: "--delivery").flatMap {
            $0 + 1 < first.count ? first[$0 + 1] : nil
        }
        return Outcome(delivery: delivery,
                       rememberedAfter: defaults.string(forKey: "lastDeliveryMode"))
    }

    /// Types into the URL field through its field editor, the way a keyboard
    /// does, so SwiftUI's binding sees the edit.
    private func typeURL(_ text: String, in window: NSWindow) throws {
        func fields(in view: NSView) -> [NSTextField] {
            ((view as? NSTextField).map { [$0] } ?? []) + view.subviews.flatMap { fields(in: $0) }
        }
        let field = try XCTUnwrap(
            window.contentView.flatMap(fields(in:))?.first {
                $0.isEditable && ($0.placeholderString ?? "").contains("apps.apple.com")
            },
            "no URL field"
        )
        XCTAssertTrue(window.makeFirstResponder(field))
        let editor = try XCTUnwrap(field.currentEditor() as? NSTextView, "no field editor")
        editor.insertText(text, replacementRange: NSRange(location: NSNotFound, length: 0))
    }

    func test_free_url_add_sends_livecontainer_over_a_remembered_sideload() async throws {
        let outcome = try await add(via: .url, account: .free, remembered: "sideload")
        XCTAssertEqual(outcome.delivery, "livecontainer")
        XCTAssertEqual(outcome.rememberedAfter, "sideload",
                       "a free add is no choice, and must not overwrite a paid one")
    }

    func test_free_manual_add_sends_livecontainer_over_a_remembered_sideload() async throws {
        let outcome = try await add(via: .manual, account: .free, remembered: "sideload")
        XCTAssertEqual(outcome.delivery, "livecontainer")
        XCTAssertEqual(outcome.rememberedAfter, "sideload")
    }

    func test_paid_url_add_keeps_the_remembered_choice() async throws {
        let outcome = try await add(via: .url, account: .paid, remembered: "sideload")
        XCTAssertEqual(outcome.delivery, "sideload")
    }

    func test_paid_manual_add_keeps_the_remembered_choice() async throws {
        let outcome = try await add(via: .manual, account: .paid, remembered: "sideload")
        XCTAssertEqual(outcome.delivery, "sideload")
    }

    func test_paid_adds_with_nothing_remembered_send_livecontainer() async throws {
        for path in [Path.url, .manual] {
            let outcome = try await add(via: path, account: .paid, remembered: nil)
            XCTAssertEqual(outcome.delivery, "livecontainer", "\(path)")
            XCTAssertEqual(outcome.rememberedAfter, "livecontainer", "\(path)")
        }
    }

    /// The picker on a free account: LiveContainer, disabled, Sideload still
    /// on screen, and the reason both as its tooltip and as a line under it.
    func test_the_free_picker_is_disabled_with_its_reason() async throws {
        try AccessibilityTree.requireServed()
        for account in [AccountMode.free, .paid] {
            let store = try await accountTestStore([accountEventLine(account.rawValue)])
            let suiteName = "AddAppFreeAccountTests.picker.\(UUID().uuidString)"
            let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
            defer { defaults.removePersistentDomain(forName: suiteName) }
            defaults.set("sideload", forKey: "lastDeliveryMode")
            let box = hosted(
                AddAppSheet(store: store, appStore: AppStoreStore(runner: store.runner, shell: NoAdminShell()))
                    .defaultAppStorage(defaults),
                width: 480
            )
            settle(box)
            let nodes = try AccessibilityTree.nodes(in: box.window)
            let segments = nodes.filter {
                $0.role == kAXRadioButtonRole
                    && [DeliveryMode.sideload.displayName, DeliveryMode.livecontainer.displayName]
                        .contains($0.name)
            }
            XCTAssertEqual(segments.count, 2, "\(account): both segments are on screen")
            let selected = segments.first { ($0.value as? NSNumber)?.boolValue == true }?.name
            let texts = nodes.filter { $0.role == kAXStaticTextRole }.compactMap { $0.value as? String }
            switch account {
            case .free:
                XCTAssertEqual(selected, DeliveryMode.livecontainer.displayName)
                XCTAssertTrue(segments.allSatisfy { $0.enabled == false }, "\(segments.map(\.enabled))")
                XCTAssertTrue(texts.contains(PaidOnly.sideload), "\(texts)")
            case .paid:
                XCTAssertEqual(selected, DeliveryMode.sideload.displayName, "the remembered choice")
                XCTAssertTrue(segments.allSatisfy { $0.enabled != false })
                XCTAssertFalse(texts.contains(PaidOnly.sideload))
            }
        }
    }
}
