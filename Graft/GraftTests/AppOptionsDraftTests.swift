import XCTest
@testable import Graft

/// EditAppSheet's diffing, tested where it now lives.
///
/// The sheet decides two things: whether Save is enabled, and which of the
/// seven fields go on the wire. The second is the dangerous one — the CLI
/// writes every field it receives, so a field sent unchanged rewrites itself,
/// and `ConfigStore.updateAppOptions` throws when the CLI's reported `changed`
/// list doesn't match what was asked for. Neither question is answerable from a
/// SwiftUI `@State`, which is why the pair lives in `AppOptionsDraft`.
final class AppOptionsDraftTests: XCTestCase {

    // MARK: - Fixture

    private struct NotAnAppEvent: Error {}

    /// `AppRecord` has no memberwise init on purpose — it exists only as
    /// decoded CLI output, so that a missing key fails the decode instead of
    /// fabricating a default the sheet would later write back as fact. Build
    /// one the way the app does.
    private func makeApp(
        displayName: String? = "Example+",
        preferredVersion: String = "latest",
        delivery: DeliveryMode = .livecontainer,
        keepBundleId: Bool = true,
        stripExtensions: Bool = false,
        stripSupportedDevices: Bool = true,
        compressionLevel: Int = 6
    ) throws -> AppRecord {
        let json: [String: Any] = [
            "type": "app",
            "name": "example",
            "bundle_id": "com.example.app",
            "preferred_version": preferredVersion,
            "tweaks": [],
            "display_name": displayName.map { $0 as Any } ?? NSNull(),
            "delivery": delivery.rawValue,
            "auto_profile": false,
            "keep_bundle_id": keepBundleId,
            "strip_extensions": stripExtensions,
            "strip_supported_devices": stripSupportedDevices,
            "compression_level": compressionLevel,
        ]
        let line = try XCTUnwrap(
            String(data: JSONSerialization.data(withJSONObject: json), encoding: .utf8)
        )
        guard case let .app(record) = try BuildEvent.decode(line: line) else {
            throw NotAnAppEvent()
        }
        return record
    }

    /// The CLI config-key names the draft would send, derived by asking it
    /// field by field. Restating the list here is the point: it fails if a
    /// field stops being diffed, and it is written independently of the
    /// argv-building code it checks.
    private func changedFields(_ draft: AppOptionsDraft) -> [String] {
        var names: [String] = []
        if draft.changed(\.preferredVersion) != nil { names.append("preferred_version") }
        if draft.changed(\.displayName) != nil { names.append("display_name") }
        if draft.changed(\.delivery) != nil { names.append("delivery") }
        if draft.changed(\.keepBundleId) != nil { names.append("keep_bundle_id") }
        if draft.changed(\.stripExtensions) != nil { names.append("strip_extensions") }
        if draft.changed(\.stripSupportedDevices) != nil { names.append("strip_supported_devices") }
        if draft.changed(\.compressionLevel) != nil { names.append("compression_level") }
        return names
    }

    // MARK: - hasChanges

    func test_a_fresh_draft_is_clean() throws {
        let draft = AppOptionsDraft(app: try makeApp())
        XCTAssertFalse(draft.hasChanges, "opening the sheet must not enable Save")
        XCTAssertEqual(changedFields(draft), [], "and must not send anything")
    }

    func test_a_missing_display_name_edits_as_empty_and_stays_clean() throws {
        // The `?? ""` case: an app with no display_name shows an empty field,
        // and leaving it empty means "unchanged", not "set it to empty".
        let draft = AppOptionsDraft(app: try makeApp(displayName: nil))
        XCTAssertEqual(draft.edited.displayName, "")
        XCTAssertFalse(draft.hasChanges)
        XCTAssertNil(draft.changed(\.displayName))
    }

    func test_each_field_flips_hasChanges_and_diffs_only_itself() throws {
        // The failure this guards: adding a field to the form but not to the
        // change test greys Save out forever on a real edit, and adding it to
        // neither the change test nor the write drops it silently. One list
        // now, so both follow — but only if every field is actually in it.
        let edits: [(field: String, apply: (inout AppOptionsDraft.Fields) -> Void)] = [
            ("preferred_version", { $0.preferredVersion = "21.16.2" }),
            ("display_name", { $0.displayName = "YT" }),
            ("delivery", { $0.delivery = .sideload }),
            ("keep_bundle_id", { $0.keepBundleId = false }),
            ("strip_extensions", { $0.stripExtensions = true }),
            ("strip_supported_devices", { $0.stripSupportedDevices = false }),
            ("compression_level", { $0.compressionLevel = 9 }),
        ]
        for edit in edits {
            var draft = AppOptionsDraft(app: try makeApp())
            edit.apply(&draft.edited)
            XCTAssertTrue(draft.hasChanges, "editing \(edit.field) must enable Save")
            XCTAssertEqual(
                changedFields(draft), [edit.field],
                "editing \(edit.field) must diff that field and no other"
            )
        }
    }

    func test_reverting_an_edit_is_clean_again() throws {
        var draft = AppOptionsDraft(app: try makeApp(compressionLevel: 6))
        draft.edited.compressionLevel = 9
        XCTAssertTrue(draft.hasChanges)
        draft.edited.compressionLevel = 6
        XCTAssertFalse(draft.hasChanges, "stepping back to the original is not a change")
        XCTAssertEqual(changedFields(draft), [])
    }

    func test_clearing_a_display_name_is_a_change_that_sends_the_empty_string() throws {
        var draft = AppOptionsDraft(app: try makeApp(displayName: "Example+"))
        draft.edited.displayName = ""
        XCTAssertTrue(draft.hasChanges)
        XCTAssertEqual(draft.changed(\.displayName), "", "clearing the field must be sent, not skipped")
    }

    // MARK: - What actually reaches the CLI

    /// Spawn a real script in place of `graft` and return the exact
    /// argument vector `updateAppOptions(appName:draft:)` handed it — the same
    /// technique as `ConfigStoreTests.test_updateAppOptions_sends_only_changed_flags`,
    /// pointed at the draft path EditAppSheet's `save()` calls.
    ///
    /// `echoing` is the `changed` list the stub CLI reports back. It is written
    /// out per test rather than derived, so a leaked flag fails twice over: the
    /// argv assertion sees it, and `updateAppOptions` throws because a field it
    /// requested is missing from `changed`.
    @MainActor
    private func capturedArgs(
        for draft: AppOptionsDraft,
        echoing changed: [String]
    ) async throws -> [String] {
        let dir = NSTemporaryDirectory()
        let capturePath = dir + "draft_args_\(UUID().uuidString).txt"
        let scriptPath = dir + "draft_capture_\(UUID().uuidString).sh"
        let changedJSON = changed.map { "\"\($0)\"" }.joined(separator: ",")
        let script = """
        #!/bin/sh
        for arg in "$@"; do printf '%s\\n' "$arg" >> "\(capturePath)"; done
        printf '{"type":"app-updated","name":"example","changed":[\(changedJSON)]}\\n'
        """
        try script.write(toFile: scriptPath, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: scriptPath)
        defer {
            try? FileManager.default.removeItem(atPath: scriptPath)
            try? FileManager.default.removeItem(atPath: capturePath)
        }

        // The post-write reload runs the same stub and appends to the same
        // file; a marker argument nobody else uses separates the two calls.
        let runner = CLIRunner(executable: URL(fileURLWithPath: scriptPath))
        let store = ConfigStore(runner: runner, cliArgs: ["--reload-marker"])
        _ = try await store.updateAppOptions(appName: "example", draft: draft)

        let captured = try String(contentsOfFile: capturePath, encoding: .utf8)
        let lines = captured.split(separator: "\n", omittingEmptySubsequences: true).map(String.init)
        return Array(lines.prefix(while: { $0 != "--reload-marker" }))
    }

    @MainActor
    func test_editing_one_field_sends_only_that_flag() async throws {
        var draft = AppOptionsDraft(app: try makeApp(compressionLevel: 6))
        draft.edited.compressionLevel = 9

        let args = try await capturedArgs(for: draft, echoing: ["compression_level"])

        XCTAssertEqual(
            args,
            ["--json", "--set-app-options", "--app", "example", "--compression-level", "9"],
            "a compression nudge must not also rewrite delivery or preferred_version"
        )
    }

    @MainActor
    func test_editing_delivery_sends_only_delivery() async throws {
        var draft = AppOptionsDraft(app: try makeApp(delivery: .livecontainer))
        draft.edited.delivery = .sideload

        let args = try await capturedArgs(for: draft, echoing: ["delivery"])

        XCTAssertEqual(
            args,
            ["--json", "--set-app-options", "--app", "example", "--delivery", "sideload"]
        )
    }

    @MainActor
    func test_turning_a_toggle_off_sends_the_negated_flag_alone() async throws {
        var draft = AppOptionsDraft(app: try makeApp(keepBundleId: true))
        draft.edited.keepBundleId = false

        let args = try await capturedArgs(for: draft, echoing: ["keep_bundle_id"])

        XCTAssertEqual(
            args,
            ["--json", "--set-app-options", "--app", "example", "--no-keep-bundle-id"]
        )
    }

    @MainActor
    func test_an_untouched_draft_sends_no_field_flags_at_all() async throws {
        // Save is disabled in this state, but nothing about the write path
        // depends on that — if a field leaked here it would leak from every
        // save, rewriting config for an app the user only looked at.
        let draft = AppOptionsDraft(app: try makeApp())

        let args = try await capturedArgs(for: draft, echoing: [])

        XCTAssertEqual(args, ["--json", "--set-app-options", "--app", "example"])
    }

    @MainActor
    func test_two_edits_send_both_flags_and_nothing_more() async throws {
        var draft = AppOptionsDraft(app: try makeApp(displayName: nil, preferredVersion: "latest"))
        draft.edited.displayName = "Example+"
        draft.edited.preferredVersion = "21.16.2"

        let args = try await capturedArgs(
            for: draft, echoing: ["display_name", "preferred_version"]
        )

        XCTAssertEqual(
            args,
            [
                "--json", "--set-app-options", "--app", "example",
                "--preferred-version", "21.16.2",
                "--display-name", "Example+",
            ]
        )
    }
}
