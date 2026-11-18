import AppKit
import SwiftUI
import XCTest
@testable import Graft

// Free Apple ID mode, on the app's side.
//
// A config with no `signing:` section is a free Apple ID: Graft builds unsigned
// IPAs and pushes them into a LiveContainer the user's store installed, and the
// CLI refuses everything that signs. The app learns the mode from `--list`'s
// `account` event and disables what the CLI would refuse, with the reason
// (`PaidOnly`). These tests hold each surface to that, in both modes, and hold
// nil (not known yet) to disabling nothing.
//
// Shared helpers are in `AccountEventTests.swift`.

// MARK: - Paid-only gates, as values

@MainActor
final class PaidOnlyGateTests: XCTestCase {
    func test_a_sideload_app_cannot_build_on_a_free_account() {
        XCTAssertEqual(AppDetailView.buildRefusal(delivery: .sideload, account: .free),
                       PaidOnly.sideloadApp)
        XCTAssertNil(AppDetailView.buildRefusal(delivery: .livecontainer, account: .free))
        XCTAssertNil(AppDetailView.buildRefusal(delivery: .sideload, account: .paid))
        XCTAssertNil(AppDetailView.buildRefusal(delivery: .sideload, account: nil))
    }

    func test_a_free_account_has_no_profile_to_name() async throws {
        let store = try await accountTestStore([accountTestAppLine()])
        let app = try XCTUnwrap(store.apps.first)
        XCTAssertEqual(AppDetailView.profileValue(app, account: .free), "not used")
        XCTAssertEqual(AppDetailView.profileValue(app, account: .paid), "manual")
        XCTAssertEqual(AppDetailView.profileValue(app, account: nil), "manual")
    }

    private func phone() throws -> DeviceRecord {
        let line = #"{"type":"device","udid":"00008030-0000000000000001","name":"Example Phone","model":"iPhone16,1","ios_version":"26.1","connection_type":"USB","livecontainer_bundle_id":"com.kdt.livecontainer.ABCDE12345","is_default":true}"#
        guard case let .device(record)? = line.buildEvents().first else {
            throw XCTSkip("device fixture did not decode")
        }
        return record
    }

    /// The bar says so ahead of everything else, the build options included:
    /// a dry run of a sideload app is refused too.
    func test_the_build_target_bar_names_the_refusal_first() throws {
        let devices = [try phone()]
        XCTAssertEqual(
            BuildTargetBar.target(devices: devices, delivery: .sideload, account: .free),
            .needsPaidAccount
        )
        XCTAssertEqual(
            BuildTargetBar.target(devices: devices, delivery: .sideload,
                                  notDelivering: .dryRun, account: .free),
            .needsPaidAccount
        )
        XCTAssertEqual(
            BuildTargetBar.target(devices: [], delivery: .sideload, account: .free),
            .needsPaidAccount, "nor does an empty bench change it"
        )
        XCTAssertEqual(
            BuildTargetBar.target(devices: devices, delivery: .livecontainer, account: .free),
            .device(name: "Example Phone", mode: "LiveContainer")
        )
        XCTAssertEqual(
            BuildTargetBar.target(devices: devices, delivery: .sideload, account: .paid),
            .device(name: "Example Phone", mode: "Sideload")
        )
        XCTAssertEqual(
            BuildTargetBar.target(devices: devices, delivery: .sideload, account: nil),
            .device(name: "Example Phone", mode: "Sideload")
        )
        XCTAssertTrue(BuildTarget.needsPaidAccount.needsAttention)
    }

    func test_the_edit_pickers_lock_only_where_there_is_no_choice() {
        XCTAssertTrue(EditAppSheet.deliveryLocked(original: .livecontainer, account: .free))
        XCTAssertFalse(EditAppSheet.deliveryLocked(original: .sideload, account: .free),
                       "a sideload app in a free config needs the picker to get out")
        XCTAssertFalse(EditAppSheet.deliveryLocked(original: .livecontainer, account: .paid))
        XCTAssertFalse(EditAppSheet.deliveryLocked(original: .livecontainer, account: nil))
    }

    private func build(app: String) throws -> BuildRecord {
        let line = #"{"type":"build-record","id":7,"timestamp":"2026-11-17T10:00:00","app":"\#(app)","bundle_id":"com.example.app","version":"1.0","tweaks":[],"ipa_path":"/b/x.ipa","ipa_sha256":null,"log_path":null,"signing_team":null,"udid":null,"success":true,"error":null,"target":"device"}"#
        guard case let .buildRecord(record)? = line.buildEvents().first else {
            throw XCTSkip("build-record fixture did not decode")
        }
        return record
    }

    /// By the app's mode now, which is what `--reinstall-build` reads.
    func test_a_reinstall_is_refused_only_for_a_sideload_app_on_a_free_account() async throws {
        let store = try await accountTestStore([
            accountTestAppLine(name: "side", delivery: .sideload), accountTestAppLine(name: "lc", delivery: .livecontainer),
        ])
        let apps = store.apps
        XCTAssertEqual(BuildsView.reinstallRefusal(for: try build(app: "side"), apps: apps, account: .free),
                       PaidOnly.sideloadApp)
        XCTAssertNil(BuildsView.reinstallRefusal(for: try build(app: "lc"), apps: apps, account: .free))
        XCTAssertNil(BuildsView.reinstallRefusal(for: try build(app: "side"), apps: apps, account: .paid))
        XCTAssertNil(BuildsView.reinstallRefusal(for: try build(app: "gone"), apps: apps, account: .free),
                     "an app gone from config is the CLI's other refusal, not this one")
    }

    func test_the_livecontainer_guidance_names_who_to_update_it_with() throws {
        func status(_ managedBy: String, instances: Bool) throws -> LCStatusRecord {
            let list = instances
                ? #"[{"bundle_id":"com.kdt.livecontainer.ABCDE12345","version":"3.8.14","role":"primary","wildcard_signed":false}]"#
                : "[]"
            let line = #"{"type":"lc-status","udid":"U","reachable":true,"instances":\#(list),"managed_by":"\#(managedBy)","latest":null,"update_available":false,"sources":{}}"#
            guard case let .lcStatus(s)? = line.buildEvents().first else {
                throw XCTSkip("lc-status fixture did not decode")
            }
            return s
        }
        let none = DeviceLiveContainerSection.freeAccountNotes(for: try status("unknown", instances: false))
        XCTAssertEqual(none.count, 1)
        XCTAssertTrue(none[0].hasPrefix("Install LiveContainer with SideStore or iloader"), none[0])

        let outside = DeviceLiveContainerSection.freeAccountNotes(for: try status("unknown", instances: true))
        XCTAssertEqual(outside.count, 1)
        XCTAssertTrue(outside[0].contains("Update LiveContainer in the one that installed it"), outside[0])

        let ours = DeviceLiveContainerSection.freeAccountNotes(for: try status("graft", instances: true))
        XCTAssertEqual(ours.count, 1)
        XCTAssertTrue(ours[0].hasPrefix("Installed by Graft with a paid account"), ours[0])
    }

    // MARK: F1 — Clean up wherever there is a LiveContainer

    private func lcStatus(_ managedBy: String, roles: [String]) throws -> LCStatusRecord {
        let ids = ["primary": "com.kdt.livecontainer.ABCDE12345", "2": "com.kdt.livecontainer2.ABCDE12345",
                   "legacy": "com.kdt.livecontainer"]
        let list = roles.map { #"{"bundle_id":"\#(ids[$0]!)","version":"3.8.14","role":"\#($0)","wildcard_signed":false}"# }
            .joined(separator: ",")
        let line = #"{"type":"lc-status","udid":"U","reachable":true,"instances":[\#(list)],"managed_by":"\#(managedBy)","latest":null,"update_available":false,"sources":{}}"#
        guard case let .lcStatus(s)? = line.buildEvents().first else {
            throw XCTSkip("lc-status fixture did not decode")
        }
        return s
    }

    /// The usual free setup — SideStore installed LiveContainer — got no
    /// Clean up and no checklist: both sat behind "not AltStore-managed".
    func test_a_free_account_on_a_sidestore_phone_gets_the_checklist_and_clean_up() throws {
        let blocks = DeviceLiveContainerSection.blocks(
            for: try lcStatus("altstore", roles: ["primary"]), account: .free
        )
        XCTAssertEqual(blocks, [.altStoreNotice, .freeChecklist])
    }

    /// Nor did a phone with only LiveContainer2, or only the bare upstream
    /// id (a jailbreak or TrollStore install): both sat behind "has a primary".
    func test_a_free_account_without_a_primary_still_gets_clean_up() throws {
        for roles in [["2"], ["legacy"]] {
            let blocks = DeviceLiveContainerSection.blocks(
                for: try lcStatus("unknown", roles: roles), account: .free
            )
            XCTAssertEqual(blocks, [.freeNotes, .installControls, .freeChecklist], "\(roles)")
        }
    }

    /// Clean up works on either account, so a paid account's phone with a
    /// LiveContainer and no checklist still has it.
    func test_a_paid_account_gets_clean_up_wherever_a_livecontainer_is_installed() throws {
        XCTAssertEqual(
            DeviceLiveContainerSection.blocks(for: try lcStatus("altstore", roles: ["primary"]), account: .paid),
            [.altStoreNotice, .cleanUp]
        )
        XCTAssertEqual(
            DeviceLiveContainerSection.blocks(for: try lcStatus("unknown", roles: ["2"]), account: .paid),
            [.installControls, .cleanUp]
        )
        XCTAssertEqual(
            DeviceLiveContainerSection.blocks(for: try lcStatus("unknown", roles: ["primary"]), account: .paid),
            [.installControls, .checklist], "the paid checklist carries its own Clean up"
        )
    }

    /// Nothing to clean, nothing offered.
    func test_no_livecontainer_offers_no_clean_up() throws {
        XCTAssertEqual(
            DeviceLiveContainerSection.blocks(for: try lcStatus("altstore", roles: []), account: .free),
            [.altStoreNotice]
        )
        XCTAssertEqual(
            DeviceLiveContainerSection.blocks(for: try lcStatus("unknown", roles: []), account: .free),
            [.freeNotes, .installControls]
        )
        XCTAssertEqual(
            DeviceLiveContainerSection.blocks(for: try lcStatus("unknown", roles: []), account: .paid),
            [.installControls]
        )
    }
}

// MARK: - Rendered surfaces

/// Each affected pane, hosted, in both accounts, read off the accessibility
/// tree (which skips when macOS is not serving it).
@MainActor
final class FreeAccountViewTests: XCTestCase {
    private func button(_ title: String, in nodes: [AccessibilityTree.Node]) -> AccessibilityTree.Node? {
        nodes.first { $0.role == kAXButtonRole && $0.name == title }
    }

    private func texts(_ nodes: [AccessibilityTree.Node]) -> [String] {
        nodes.filter { $0.role == kAXStaticTextRole }.compactMap { $0.value as? String }
    }

    // MARK: App pane

    private func appPane(
        delivery: DeliveryMode, account: AccountMode
    ) async throws -> [AccessibilityTree.Node] {
        try AccessibilityTree.requireServed()
        let config = try await accountTestStore([accountEventLine(account.rawValue), accountTestAppLine(delivery: delivery)])
        let app = try XCTUnwrap(config.apps.first)
        let pane = AppDetailView(
            app: app, runner: accountTestShell, store: config, artwork: ArtworkStore(runner: accountTestShell),
            devices: DevicesStore(runner: accountTestShell), tweaksStore: TweaksStore(runner: accountTestShell),
            staged: StagedIPAStore(runner: accountTestShell), appStore: AppStoreStore(runner: accountTestShell),
            liveContainer: LiveContainerStore(runner: accountTestShell), showSettings: {}
        )
        let box = hosted(pane, size: NSSize(width: 1000, height: 900))
        try await Task.sleep(nanoseconds: 500_000_000)
        settle(box, seconds: 0.2)
        return try AccessibilityTree.nodes(in: box.window)
    }

    func test_a_sideload_app_on_a_free_account_has_no_live_build_button() async throws {
        let nodes = try await appPane(delivery: .sideload, account: .free)
        let build = try XCTUnwrap(button("Build & Install", in: nodes), "the button is still shown")
        XCTAssertEqual(build.enabled, false)
        XCTAssertEqual(build.help, PaidOnly.sideloadApp)
        XCTAssertTrue(texts(nodes).contains(BuildTarget.needsPaidAccountLine), "\(texts(nodes))")
    }

    func test_a_livecontainer_app_on_a_free_account_still_builds() async throws {
        let nodes = try await appPane(delivery: .livecontainer, account: .free)
        let build = try XCTUnwrap(button("Build & Install", in: nodes))
        XCTAssertNotEqual(build.enabled, false)
        XCTAssertFalse(texts(nodes).contains(BuildTarget.needsPaidAccountLine))
    }

    func test_a_sideload_app_on_a_paid_account_still_builds() async throws {
        let nodes = try await appPane(delivery: .sideload, account: .paid)
        let build = try XCTUnwrap(button("Build & Install", in: nodes))
        XCTAssertNotEqual(build.enabled, false)
        XCTAssertFalse(texts(nodes).contains(BuildTarget.needsPaidAccountLine))
    }

    // MARK: Edit sheet

    private func editSheet(
        delivery: DeliveryMode, account: AccountMode
    ) async throws -> [AccessibilityTree.Node] {
        try AccessibilityTree.requireServed()
        let config = try await accountTestStore([accountEventLine(account.rawValue), accountTestAppLine(delivery: delivery)])
        let app = try XCTUnwrap(config.apps.first)
        let box = hosted(EditAppSheet(
            app: app, store: config, liveContainer: LiveContainerStore(runner: accountTestShell),
            devices: DevicesStore(runner: accountTestShell), onClose: {}
        ), width: 460)
        settle(box)
        return try AccessibilityTree.nodes(in: box.window)
    }

    /// The Form's picker is a pop-up button whose value is the selection.
    private func deliveryPicker(in nodes: [AccessibilityTree.Node]) -> AccessibilityTree.Node? {
        nodes.first {
            $0.role == kAXPopUpButtonRole
                && [DeliveryMode.sideload.displayName, DeliveryMode.livecontainer.displayName]
                    .contains(($0.value as? String) ?? "")
        }
    }

    func test_the_edit_picker_is_locked_for_a_livecontainer_app_on_a_free_account() async throws {
        let nodes = try await editSheet(delivery: .livecontainer, account: .free)
        let picker = try XCTUnwrap(deliveryPicker(in: nodes), "the picker is still shown")
        XCTAssertEqual(picker.enabled, false)
        XCTAssertTrue(texts(nodes).contains(PaidOnly.sideload))
    }

    func test_the_edit_picker_stays_live_for_a_sideload_app_on_a_free_account() async throws {
        let nodes = try await editSheet(delivery: .sideload, account: .free)
        let picker = try XCTUnwrap(deliveryPicker(in: nodes))
        XCTAssertNotEqual(picker.enabled, false, "it is how the app gets to LiveContainer")
        XCTAssertTrue(texts(nodes).contains(PaidOnly.sideload))
    }

    func test_the_edit_picker_is_live_on_a_paid_account() async throws {
        let nodes = try await editSheet(delivery: .livecontainer, account: .paid)
        let picker = try XCTUnwrap(deliveryPicker(in: nodes))
        XCTAssertNotEqual(picker.enabled, false)
        XCTAssertFalse(texts(nodes).contains(PaidOnly.sideload))
    }

    // MARK: Device pane's LiveContainer section

    private static let udid = "00008030-0000000000000001"

    private func lcSection(
        account: AccountMode, managedBy: String = "unknown",
        instance: (id: String, role: String) = ("com.kdt.livecontainer.ABCDE12345", "primary")
    ) async throws -> [AccessibilityTree.Node] {
        try AccessibilityTree.requireServed()
        let device = #"{"type":"device","udid":"\#(Self.udid)","name":"Example Phone","model":"iPhone16,1","ios_version":"26.1","connection_type":"USB","livecontainer_bundle_id":"com.kdt.livecontainer.ABCDE12345","is_default":true}"#
        guard case let .device(record)? = device.buildEvents().first else {
            throw XCTSkip("device fixture did not decode")
        }
        // By default installed outside Graft, no store on the device: the case
        // where a paid account gets Install / Update and the checklist.
        let status = #"{"type":"lc-status","udid":"\#(Self.udid)","reachable":true,"instances":[{"bundle_id":"\#(instance.id)","version":"3.8.14","role":"\#(instance.role)","wildcard_signed":false}],"managed_by":"\#(managedBy)","latest":{"version":"3.8.14","channel":"nightly"},"update_available":false,"sources":{"nightly":"https://example.invalid/source.json"}}"#
        let script = NSTemporaryDirectory() + "free_lc_status_\(UUID().uuidString).sh"
        try "#!/bin/sh\nprintf '%s\\n' '\(status)'\n"
            .write(toFile: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script)
        addTeardownBlock { try? FileManager.default.removeItem(atPath: script) }
        let store = LiveContainerStore(runner: CLIRunner(executable: URL(fileURLWithPath: script)))
        await store.load(udid: Self.udid)
        let box = hosted(
            DeviceLiveContainerSection(device: record, store: store, account: account), width: 700
        )
        try await Task.sleep(nanoseconds: 300_000_000)
        settle(box)
        return try AccessibilityTree.nodes(in: box.window)
    }

    func test_the_livecontainer_section_is_read_only_on_a_free_account() async throws {
        let nodes = try await lcSection(account: .free)
        let install = try XCTUnwrap(button("Install / Update LiveContainer", in: nodes),
                                    "shown, not hidden")
        XCTAssertEqual(install.enabled, false)
        XCTAssertEqual(install.help, PaidOnly.installLiveContainer)
        let push = try XCTUnwrap(button("Put certificate on device", in: nodes))
        XCTAssertEqual(push.enabled, false)
        XCTAssertEqual(push.help, PaidOnly.pushCertificate)
        XCTAssertNotEqual(button("Clean up", in: nodes)?.enabled, false, "works on either account")
        let shown = texts(nodes)
        XCTAssertTrue(shown.contains("LiveContainer 3.8.14"), "what is installed is listed: \(shown)")
        XCTAssertTrue(shown.contains(PaidOnly.installLiveContainer))
        XCTAssertTrue(shown.contains { $0.contains("Update LiveContainer in the one that installed it") })
    }

    /// F1, rendered: a free Apple ID on a SideStore phone — the usual free
    /// setup — has Clean up, live, and the step that says where the
    /// certificate comes from.
    func test_a_sidestore_phone_on_a_free_account_has_clean_up() async throws {
        let nodes = try await lcSection(account: .free, managedBy: "altstore")
        let clean = try XCTUnwrap(button("Clean up", in: nodes), "\(texts(nodes))")
        XCTAssertNotEqual(clean.enabled, false)
        let shown = texts(nodes)
        XCTAssertTrue(shown.contains("Managed by SideStore or AltStore — update it there"), "\(shown)")
        XCTAssertTrue(shown.contains { $0.hasPrefix("Settings › Import Certificate from SideStore") },
                      "\(shown)")
    }

    /// And a phone whose only LiveContainer is LiveContainer2.
    func test_a_phone_with_only_livecontainer2_on_a_free_account_has_clean_up() async throws {
        let nodes = try await lcSection(
            account: .free, instance: ("com.kdt.livecontainer2.ABCDE12345", "2")
        )
        XCTAssertNotEqual(try XCTUnwrap(button("Clean up", in: nodes)).enabled, false)
    }

    func test_the_livecontainer_section_installs_on_a_paid_account() async throws {
        let nodes = try await lcSection(account: .paid)
        XCTAssertNotEqual(button("Install / Update LiveContainer", in: nodes)?.enabled, false)
        XCTAssertNotEqual(button("Put certificate on device", in: nodes)?.enabled, false)
        XCTAssertNotNil(button("Put certificate on device", in: nodes))
        XCTAssertFalse(texts(nodes).contains(PaidOnly.installLiveContainer))
    }

    // MARK: Settings

    private func settingsPane(account: AccountMode) async throws -> [AccessibilityTree.Node] {
        try AccessibilityTree.requireServed()
        let settings = SettingsStore(runner: accountTestShell)
        let line = #"{"type":"settings","default_device_udid":null,"archive_keep":2,"signing_cert":null,"signing_profile":null,"signing_team_id":null}"#
        _ = try await settings.loadSettings(argsOverride: ["-c", "printf '%s\\n' '\(line)'"])
        settings.reseedDraftIfIdle()
        let pane = SettingsView(
            store: settings, devicesStore: DevicesStore(runner: accountTestShell),
            appStore: AppStoreStore(runner: accountTestShell), account: account
        )
        let box = hosted(pane, size: NSSize(width: 900, height: 1400))
        try await Task.sleep(nanoseconds: 300_000_000)
        settle(box)
        return try AccessibilityTree.nodes(in: box.window)
    }

    func test_settings_shows_the_account_and_locks_signing_on_a_free_account() async throws {
        let nodes = try await settingsPane(account: .free)
        let shown = texts(nodes)
        XCTAssertTrue(shown.contains(AccountMode.free.summary), "\(shown)")
        XCTAssertTrue(shown.contains(PaidOnly.signingSettings))
        let fields = nodes.filter { $0.role == kAXTextFieldRole }
        XCTAssertEqual(fields.count, 3, "the three signing fields are still shown")
        XCTAssertTrue(fields.allSatisfy { $0.enabled == false }, "\(fields.map(\.enabled))")
    }

    func test_settings_shows_the_account_and_edits_signing_on_a_paid_account() async throws {
        let nodes = try await settingsPane(account: .paid)
        let shown = texts(nodes)
        XCTAssertTrue(shown.contains(AccountMode.paid.summary), "\(shown)")
        XCTAssertFalse(shown.contains(PaidOnly.signingSettings))
        let fields = nodes.filter { $0.role == kAXTextFieldRole }
        XCTAssertEqual(fields.count, 3)
        XCTAssertTrue(fields.allSatisfy { $0.enabled != false })
    }
}

// MARK: - A refusal that is reached anyway

@MainActor
final class PaidAccountRefusalTests: XCTestCase {
    /// What the CLI prints when a paid-only verb reaches it on a free config,
    /// verbatim (Rich's 80-column wraps included, trailing spaces trimmed), and
    /// what a banner shows: the CLI's own sentence, not a terminal transcript.
    ///
    /// Regenerated from `config.paid_account_required` → `cli._fatal` at an
    /// 80-column `err_console`, for the default config of a user called
    /// `example`. The hint it replaced ("…answer yes, or add a signing:
    /// section (cert, cert_password_env, profile, team_id)…") was two CLI
    /// revisions old and nothing noticed, because the test checked only the
    /// message's opening words — hence the checks on the hint below.
    private static let stderr = """
    error: --delivery sideload needs a paid Apple Developer Program membership; this
    config is set up for a free Apple ID (it has no signing: section)
      hint: with a paid membership, run `graft --init` and answer yes: it asks for
    your .p12 and profile and adds a signing: section to
    /Users/example/.config/graft/config.yaml, keeping its apps, repos and settings.
    Apps with no delivery: key are given delivery: livecontainer, so they stay in
    LiveContainer. With a free Apple ID, deliver apps into LiveContainer (delivery:
    livecontainer)
    """

    func test_a_refusal_reaches_the_banner_as_the_clis_sentence() async throws {
        let escaped = Self.stderr.replacingOccurrences(of: "'", with: "'\\''")
        let store = ConfigStore(runner: accountTestShell, cliArgs: ["-c", "true"])
        do {
            try await store.addAppManual(
                name: "x", bundleId: "com.example.x", displayName: nil, delivery: .sideload,
                addArgsOverride: ["-c", "printf '%s' '\(escaped)' >&2; exit 2"]
            )
            XCTFail("the refusal did not throw")
        } catch {
            let message = error.localizedDescription
            XCTAssertTrue(
                message.hasPrefix("--delivery sideload needs a paid Apple Developer Program "
                                  + "membership; this config is set up for a free Apple ID"),
                message
            )
            XCTAssertFalse(message.contains("error:"), message)
            XCTAssertFalse(message.contains("\n"), "re-flowed, not 80-column: \(message)")
            XCTAssertTrue(message.contains("hint: with a paid membership, run `graft --init` and "
                                           + "answer yes: it asks for your .p12 and profile"), message)
            XCTAssertTrue(message.contains("keeping its apps, repos and settings. Apps with no "
                                           + "delivery: key are given delivery: livecontainer"), message)
            XCTAssertTrue(message.hasSuffix("With a free Apple ID, deliver apps into LiveContainer "
                                            + "(delivery: livecontainer)"), message)
        }
    }
}
