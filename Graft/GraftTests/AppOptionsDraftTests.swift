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

    /// How the `app` event carries `lc_bundle_id` (C-LCPIN): not at all (a CLI
    /// from before it), as null (unpinned), or as an id.
    enum PinKey { case absent, null, id(String) }

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
        compressionLevel: Int = 6,
        pin: PinKey = .null
    ) throws -> AppRecord {
        var json: [String: Any] = [
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
        switch pin {
        case .absent: break
        case .null: json["lc_bundle_id"] = NSNull()
        case let .id(id): json["lc_bundle_id"] = id
        }
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
        if draft.changed(\.lcPin) != nil { names.append("lc_bundle_id") }
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
            ("display_name", { $0.displayName = "Example" }),
            ("delivery", { $0.delivery = .sideload }),
            ("keep_bundle_id", { $0.keepBundleId = false }),
            ("strip_extensions", { $0.stripExtensions = true }),
            ("strip_supported_devices", { $0.stripSupportedDevices = false }),
            ("compression_level", { $0.compressionLevel = 9 }),
            ("lc_bundle_id", { $0.lcPin = .bundleId("com.kdt.livecontainer2.ABCDE12345") }),
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

    // MARK: - The LiveContainer pin (C-LCPIN)

    func test_the_pin_decodes_as_the_cli_reports_it() throws {
        let old = try makeApp(pin: .absent)
        XCTAssertFalse(old.reportsLCPin, "a CLI from before C-LCPIN says nothing")
        XCTAssertNil(old.lcBundleId)

        let unpinned = try makeApp(pin: .null)
        XCTAssertTrue(unpinned.reportsLCPin)
        XCTAssertNil(unpinned.lcBundleId)
        XCTAssertEqual(AppOptionsDraft(app: unpinned).edited.lcPin, .automatic)

        let pinned = try makeApp(pin: .id("com.kdt.livecontainer2.ABCDE12345"))
        XCTAssertEqual(pinned.lcBundleId, "com.kdt.livecontainer2.ABCDE12345")
        XCTAssertEqual(
            AppOptionsDraft(app: pinned).edited.lcPin,
            .bundleId("com.kdt.livecontainer2.ABCDE12345")
        )
    }

    @MainActor
    func test_pinning_a_livecontainer_sends_the_pin_alone() async throws {
        var draft = AppOptionsDraft(app: try makeApp(pin: .null))
        draft.edited.lcPin = .bundleId("com.kdt.livecontainer2.ABCDE12345")

        let args = try await capturedArgs(for: draft, echoing: ["lc_bundle_id"])

        XCTAssertEqual(args, [
            "--json", "--set-app-options", "--app", "example",
            "--lc-bundle-id", "com.kdt.livecontainer2.ABCDE12345",
        ])
    }

    /// Automatic is a write too — the one that clears a pin — and it is its
    /// own flag, never `--lc-bundle-id ""`.
    @MainActor
    func test_choosing_automatic_clears_the_pin() async throws {
        var draft = AppOptionsDraft(app: try makeApp(pin: .id("com.kdt.livecontainer2.ABCDE12345")))
        draft.edited.lcPin = .automatic

        let args = try await capturedArgs(for: draft, echoing: ["lc_bundle_id"])

        XCTAssertEqual(args, ["--json", "--set-app-options", "--app", "example", "--no-lc-bundle-id"])
    }

    /// Reconciled like every other field: a pin the CLI did not report
    /// applying is a failure, not a quiet success.
    @MainActor
    func test_a_pin_the_cli_did_not_apply_is_reported() async throws {
        var draft = AppOptionsDraft(app: try makeApp(pin: .null))
        draft.edited.lcPin = .bundleId("com.kdt.livecontainer2.ABCDE12345")
        do {
            _ = try await capturedArgs(for: draft, echoing: [])
            XCTFail("a dropped pin reported success")
        } catch {
            XCTAssertTrue(error.localizedDescription.contains("lc_bundle_id"),
                          error.localizedDescription)
        }
    }
}

/// The Edit sheet's LiveContainer picker, as values.
final class LiveContainerPinPickerTests: XCTestCase {
    private func instances(_ list: [(String, String)]) throws -> [LCInstanceRecord] {
        let body = list.map { #"{"bundle_id":"\#($0.0)","version":"3.8.14","role":"\#($0.1)","wildcard_signed":false}"# }
            .joined(separator: ",")
        let line = #"{"type":"lc-status","udid":"U","reachable":true,"instances":[\#(body)],"managed_by":"unknown","latest":null,"update_available":false,"sources":{}}"#
        guard case let .lcStatus(status)? = line.buildEvents().first else {
            throw XCTSkip("lc-status fixture did not decode")
        }
        return status.instances
    }

    private let primary = ("com.kdt.livecontainer.ABCDE12345", "primary")
    private let second = ("com.kdt.livecontainer2.ABCDE12345", "2")

    func test_unpinned_lists_automatic_first_then_each_livecontainer() throws {
        let choices = EditAppSheet.lcPinChoices(
            instances: try instances([primary, second]), current: nil, deviceName: "Example Phone"
        )
        XCTAssertEqual(choices.map(\.label), ["Automatic", "LiveContainer", "LiveContainer2"])
        XCTAssertEqual(choices.map(\.pin), [
            .automatic, .bundleId("com.kdt.livecontainer.ABCDE12345"),
            .bundleId("com.kdt.livecontainer2.ABCDE12345"),
        ])
    }

    /// Two primaries — two Apple IDs' installs — share a name, and are told
    /// apart by id: that is the case a pin exists to settle.
    func test_two_livecontainers_with_one_name_are_told_apart_by_id() throws {
        let choices = EditAppSheet.lcPinChoices(
            instances: try instances([primary, ("com.kdt.livecontainer.FGHIJ67890", "primary")]),
            current: nil, deviceName: "Example Phone"
        )
        XCTAssertEqual(choices.map(\.label), [
            "Automatic",
            "LiveContainer — com.kdt.livecontainer.ABCDE12345",
            "LiveContainer — com.kdt.livecontainer.FGHIJ67890",
        ])
    }

    /// The pin matches as the CLI matches it — ignoring case, or as the
    /// prefix of one id — and the matching row carries the pin's own spelling,
    /// so the picker shows it selected instead of showing nothing.
    func test_a_pin_matching_an_installed_livecontainer_selects_it() throws {
        let list = try instances([primary, second])
        for pin in ["com.kdt.livecontainer2.ABCDE12345", "COM.KDT.LIVECONTAINER2.abcde12345",
                    "com.kdt.livecontainer2"] {
            let choices = EditAppSheet.lcPinChoices(instances: list, current: pin, deviceName: "Example Phone")
            XCTAssertEqual(choices.count, 3, "no extra row for \(pin): \(choices.map(\.label))")
            XCTAssertEqual(choices[2].pin, .bundleId(pin), "the pinned row carries the pin")
        }
    }

    /// A pin nothing on the phone matches stays listed, and says so: dropping
    /// it would re-point the app at Automatic on the next save.
    func test_a_pin_not_on_the_device_stays_and_says_where_it_is_not() throws {
        let choices = EditAppSheet.lcPinChoices(
            instances: try instances([primary]), current: "com.kdt.livecontainer3.ABCDE12345",
            deviceName: "Example Phone"
        )
        XCTAssertEqual(choices.last?.label, "com.kdt.livecontainer3.ABCDE12345 (not on Example Phone)")
        XCTAssertEqual(choices.last?.pin, .bundleId("com.kdt.livecontainer3.ABCDE12345"))
    }

    func test_with_no_device_the_pin_and_automatic_are_all_there_is() {
        let choices = EditAppSheet.lcPinChoices(
            instances: nil, current: "com.kdt.livecontainer.ABCDE12345", deviceName: nil
        )
        XCTAssertEqual(choices.map(\.label), ["Automatic", "com.kdt.livecontainer.ABCDE12345"])
    }

    /// F8/S4: an exact match wins, as in the CLI's `_pinned`. The bare id is a
    /// prefix of the primary's too, and counting both as matches found two,
    /// called the pin not installed, and listed it again as "(not on …)" —
    /// a second row with the same tag as the one it was installed as.
    func test_an_exact_match_wins_over_a_prefix_match() throws {
        let list = try instances([("com.kdt.livecontainer", "legacy"), primary])
        let choices = EditAppSheet.lcPinChoices(
            instances: list, current: "com.kdt.livecontainer", deviceName: "Example Phone"
        )
        XCTAssertEqual(choices.map(\.label), ["Automatic", "com.kdt.livecontainer", "LiveContainer"])
        XCTAssertEqual(choices.map(\.pin), [
            .automatic, .bundleId("com.kdt.livecontainer"), .bundleId("com.kdt.livecontainer.ABCDE12345"),
        ])
        XCTAssertEqual(Set(choices.map(\.pin)).count, choices.count, "every tag is distinct")
    }

    /// A prefix is only the fallback, and only when it names exactly one.
    func test_a_prefix_of_two_livecontainers_matches_neither() throws {
        let list = try instances([primary, ("com.kdt.livecontainer.FGHIJ67890", "primary")])
        let choices = EditAppSheet.lcPinChoices(
            instances: list, current: "com.kdt.livecontainer", deviceName: "Example Phone"
        )
        XCTAssertEqual(choices.last?.label, "com.kdt.livecontainer (not on Example Phone)")
        XCTAssertEqual(
            EditAppSheet.pinnedInstance("com.kdt.livecontainer", in: list)?.bundleId, nil
        )
    }

    /// The CLI's other rules, mirrored: ignoring case, the id spelled as
    /// pinned wins a tie, and a pin that is not a LiveContainer id at all
    /// matches nothing — the CLI refuses it — even as a prefix of one.
    func test_the_pin_is_matched_exactly_as_the_cli_matches_it() throws {
        let list = try instances([primary, second])
        XCTAssertEqual(
            EditAppSheet.pinnedInstance("COM.KDT.LIVECONTAINER2.abcde12345", in: list)?.bundleId,
            "com.kdt.livecontainer2.ABCDE12345"
        )
        XCTAssertEqual(
            EditAppSheet.pinnedInstance("com.kdt.LiveContainer2", in: list)?.bundleId,
            "com.kdt.livecontainer2.ABCDE12345"
        )
        let twins = try instances([("COM.KDT.LIVECONTAINER", "legacy"), ("com.kdt.livecontainer", "legacy")])
        XCTAssertEqual(
            EditAppSheet.pinnedInstance("com.kdt.livecontainer", in: twins)?.bundleId,
            "com.kdt.livecontainer"
        )
        XCTAssertNil(EditAppSheet.pinnedInstance("com.kdt", in: try instances([primary])))
    }

    /// F8/S5: no answer from the phone is not an answer that the pin is
    /// missing from it. Before the status loads, or when the phone could not
    /// be read, the pin is listed by itself — the note beside it says which.
    func test_a_pin_is_not_called_missing_before_the_phone_has_answered() {
        let choices = EditAppSheet.lcPinChoices(
            instances: nil, current: "com.kdt.livecontainer2.ABCDE12345", deviceName: "Example Phone"
        )
        XCTAssertEqual(choices.map(\.label), ["Automatic", "com.kdt.livecontainer2.ABCDE12345"])
    }

    /// The sheet's reading of the store: a status that was read and reached
    /// the phone is a list, anything else is no list at all.
    func test_only_a_reachable_status_lists_instances() throws {
        let reachable = #"{"type":"lc-status","udid":"U","reachable":true,"instances":[],"managed_by":"unknown","latest":null,"update_available":false,"sources":{}}"#
        let unreachable = #"{"type":"lc-status","udid":"U","reachable":false,"error":"locked","instances":[],"managed_by":"unknown","latest":null,"update_available":false,"sources":{}}"#
        guard case let .lcStatus(read)? = reachable.buildEvents().first,
              case let .lcStatus(refused)? = unreachable.buildEvents().first else {
            throw XCTSkip("lc-status fixture did not decode")
        }
        XCTAssertEqual(EditAppSheet.instances(from: read)?.count, 0, "read, and none there")
        XCTAssertNil(EditAppSheet.instances(from: refused))
        XCTAssertNil(EditAppSheet.instances(from: nil))
    }

    /// F8/S6: the list is loaded for whichever phone is the default *now* —
    /// so plugging it in with the sheet open loads it, rather than the task
    /// having run once, found no phone, and never run again.
    func test_the_list_is_loaded_for_the_default_device_as_it_arrives() throws {
        let app = try XCTUnwrap(try pinnedApp(#","lc_bundle_id":null"#))
        XCTAssertNil(EditAppSheet.lcPinLoadTarget(app: app, delivery: .livecontainer, defaultUDID: nil))
        XCTAssertEqual(EditAppSheet.lcPinLoadTarget(app: app, delivery: .livecontainer, defaultUDID: "U1"), "U1")
        XCTAssertNil(EditAppSheet.lcPinLoadTarget(app: app, delivery: .sideload, defaultUDID: "U1"),
                     "no picker, nothing to load")
    }

    private func pinnedApp(_ pinPart: String) throws -> AppRecord? {
        let line = accountTestAppLine().replacingOccurrences(
            of: #""compression_level":6"#, with: #""compression_level":6\#(pinPart)"#
        )
        guard case let .app(record)? = line.buildEvents().first else {
            throw XCTSkip("app fixture did not decode")
        }
        return record
    }

    func test_the_note_says_where_the_list_comes_from() {
        XCTAssertTrue(EditAppSheet.lcPinNote(deviceName: nil, hasLoaded: false, status: nil)
            .hasPrefix("Plug in your default device"))
        // "Ask each time" with a phone plugged in: plugging one in is not
        // what is missing.
        XCTAssertTrue(EditAppSheet.lcPinNote(
            deviceName: nil, hasLoaded: false, status: nil, otherDevicesConnected: true
        ).hasPrefix("No connected device is your default. Choose a default device in Settings "
                    + "to list its LiveContainers."))
        XCTAssertEqual(EditAppSheet.lcPinNote(deviceName: "Example Phone", hasLoaded: false, status: nil),
                       "Looking for LiveContainers on Example Phone…")
        XCTAssertTrue(EditAppSheet.lcPinNote(deviceName: "Example Phone", hasLoaded: true, status: nil)
            .hasPrefix("Automatic uses the only LiveContainer"))
    }

    func test_the_picker_is_offered_only_where_a_pin_means_something() throws {
        func app(_ pinPart: String, delivery: DeliveryMode = .livecontainer) throws -> AppRecord {
            let line = accountTestAppLine(delivery: delivery).replacingOccurrences(
                of: #""compression_level":6"#, with: #""compression_level":6\#(pinPart)"#
            )
            guard case let .app(record)? = line.buildEvents().first else {
                throw XCTSkip("app fixture did not decode")
            }
            return record
        }
        XCTAssertTrue(EditAppSheet.offersLCPin(app: try app(#","lc_bundle_id":null"#), delivery: .livecontainer))
        XCTAssertFalse(EditAppSheet.offersLCPin(app: try app(#","lc_bundle_id":null"#), delivery: .sideload),
                       "a sideload app has no LiveContainer to pin")
        XCTAssertFalse(EditAppSheet.offersLCPin(app: try app(""), delivery: .livecontainer),
                       "a CLI that does not report the pin cannot set it either")
    }

    // MARK: Hosted

    private static let udid = "00008030-0000000000000001"

    /// The sheet, hosted against a default device whose `--lc-status` lists
    /// two LiveContainers, read off the accessibility tree.
    @MainActor
    private func sheet(pinPart: String) async throws -> [AccessibilityTree.Node] {
        try AccessibilityTree.requireServed()
        let appLine = accountTestAppLine().replacingOccurrences(
            of: #""compression_level":6"#, with: #""compression_level":6\#(pinPart)"#
        )
        let config = try await accountTestStore([accountEventLine("free"), appLine])
        let app = try XCTUnwrap(config.apps.first)
        let device = #"{"type":"device","udid":"\#(Self.udid)","name":"Example Phone","model":"iPhone16,1","ios_version":"26.1","connection_type":"USB","livecontainer_bundle_id":"com.kdt.livecontainer.ABCDE12345","is_default":true}"#
        let devices = DevicesStore(runner: accountTestShell)
        _ = try await devices.load(argsOverride: ["-c", "printf '%s\\n' '\(device)'"])
        let status = #"{"type":"lc-status","udid":"\#(Self.udid)","reachable":true,"instances":[{"bundle_id":"com.kdt.livecontainer.ABCDE12345","version":"3.8.14","role":"primary","wildcard_signed":false},{"bundle_id":"com.kdt.livecontainer2.ABCDE12345","version":"3.8.14","role":"2","wildcard_signed":false}],"managed_by":"altstore","latest":null,"update_available":false,"sources":{}}"#
        let script = NSTemporaryDirectory() + "pin_lc_status_\(UUID().uuidString).sh"
        try "#!/bin/sh\nprintf '%s\\n' '\(status)'\n".write(toFile: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script)
        addTeardownBlock { try? FileManager.default.removeItem(atPath: script) }
        let lc = LiveContainerStore(runner: CLIRunner(executable: URL(fileURLWithPath: script)))
        let box = hosted(
            EditAppSheet(app: app, store: config, liveContainer: lc, devices: devices, onClose: {}),
            width: 460
        )
        try await Task.sleep(nanoseconds: 600_000_000)
        settle(box, seconds: 0.2)
        XCTAssertTrue(lc.hasLoaded.contains(Self.udid), "the sheet asked the default device")
        return try AccessibilityTree.nodes(in: box.window)
    }

    @MainActor
    func test_an_unpinned_app_shows_automatic() async throws {
        let nodes = try await sheet(pinPart: #","lc_bundle_id":null"#)
        let picker = nodes.first { $0.role == kAXPopUpButtonRole && ($0.value as? String) == "Automatic" }
        XCTAssertNotNil(picker, "\(nodes.filter { $0.role == kAXPopUpButtonRole }.map(\.value))")
        let texts = nodes.filter { $0.role == kAXStaticTextRole }.compactMap { $0.value as? String }
        XCTAssertTrue(texts.contains { $0.hasPrefix("Automatic uses the only LiveContainer") }, "\(texts)")
    }

    @MainActor
    func test_a_pinned_app_shows_its_livecontainer() async throws {
        let nodes = try await sheet(pinPart: #","lc_bundle_id":"com.kdt.livecontainer2.ABCDE12345""#)
        XCTAssertNotNil(nodes.first {
            $0.role == kAXPopUpButtonRole && ($0.value as? String) == "LiveContainer2"
        }, "\(nodes.filter { $0.role == kAXPopUpButtonRole }.map(\.value))")
    }

    /// F8/S6, through the sheet's own `.task`: opened with no default device
    /// connected, then the phone is plugged in. The task was keyed on whether
    /// the picker shows, which plugging a phone in does not change, so it ran
    /// once, found no phone, and never asked. Read off the store, not the
    /// accessibility tree.
    @MainActor
    func test_plugging_the_default_device_in_with_the_sheet_open_lists_it() async throws {
        let appLine = accountTestAppLine().replacingOccurrences(
            of: #""compression_level":6"#, with: #""compression_level":6,"lc_bundle_id":null"#
        )
        let config = try await accountTestStore([accountEventLine("free"), appLine])
        let app = try XCTUnwrap(config.apps.first)
        let devices = DevicesStore(runner: accountTestShell)
        let status = #"{"type":"lc-status","udid":"\#(Self.udid)","reachable":true,"instances":[],"managed_by":"unknown","latest":null,"update_available":false,"sources":{}}"#
        let script = NSTemporaryDirectory() + "pin_lc_arrives_\(UUID().uuidString).sh"
        try "#!/bin/sh\nprintf '%s\\n' '\(status)'\n".write(toFile: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script)
        addTeardownBlock { try? FileManager.default.removeItem(atPath: script) }
        let lc = LiveContainerStore(runner: CLIRunner(executable: URL(fileURLWithPath: script)))
        let box = hosted(
            EditAppSheet(app: app, store: config, liveContainer: lc, devices: devices, onClose: {}),
            width: 460
        )
        try await Task.sleep(nanoseconds: 300_000_000)
        settle(box, seconds: 0.1)
        XCTAssertFalse(lc.hasLoaded.contains(Self.udid), "premise: no phone yet, nothing asked")

        let device = #"{"type":"device","udid":"\#(Self.udid)","name":"Example Phone","model":"iPhone16,1","ios_version":"26.1","connection_type":"USB","livecontainer_bundle_id":null,"is_default":true}"#
        _ = try await devices.load(argsOverride: ["-c", "printf '%s\\n' '\(device)'"])
        let deadline = Date().addingTimeInterval(3)
        while !lc.hasLoaded.contains(Self.udid), Date() < deadline {
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        XCTAssertTrue(lc.hasLoaded.contains(Self.udid), "the sheet never asked the phone that arrived")
    }
}
