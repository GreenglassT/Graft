import XCTest
@testable import Graft

/// Spec §5b, the device switcher, from the app's side: what the CLI says about
/// devices that aren't connected, the one-build pick, Make default, and the
/// Settings label. The bar's own table is `BuildTargetBarTests`.
final class DeviceSwitcherTests: XCTestCase {
    static let phone = "00008030-0000000000000001"
    static let ipad = "00008030-0000000000000002"

    // MARK: - device-seen

    func test_decode_device_seen() throws {
        let named = #"{"type":"device-seen","udid":"00008030-0000000000000001","name":"iPhone 15 Pro","model":"iPhone16,1","ios_version":"26.1","is_default":true}"#
        guard case let .deviceSeen(record) = try BuildEvent.decode(line: named) else {
            return XCTFail("expected .deviceSeen")
        }
        XCTAssertEqual(record.udid, Self.phone)
        XCTAssertEqual(record.displayName, "iPhone 15 Pro")
        XCTAssertEqual(record.iosVersion, "26.1")
        XCTAssertTrue(record.isDefault)

        let unnamed = #"{"type":"device-seen","udid":"00008030-0000000000000001","name":null,"model":null,"ios_version":null,"is_default":true}"#
        guard case let .deviceSeen(bare) = try BuildEvent.decode(line: unnamed) else {
            return XCTFail("expected .deviceSeen")
        }
        XCTAssertNil(bare.name)
        XCTAssertEqual(bare.displayName, Self.phone, "never seen: its UDID is its name")
    }

    /// A listing's event, never a build's: it says nothing about progress.
    func test_device_seen_is_not_build_progress() throws {
        let line = #"{"type":"device-seen","udid":"00008030-0000000000000001","name":null,"model":null,"ios_version":null,"is_default":false}"#
        XCTAssertNil(try BuildEvent.decode(line: line).progress)
    }
}

extension DeviceSwitcherTests {
    // MARK: - livecontainer_known

    /// Only a listing that could read the phone's apps can say LiveContainer
    /// is missing. An older CLI sends no key: unknown, so it never blocks.
    func test_decode_device_livecontainer_known() throws {
        let base = #"{"type":"device","udid":"U","name":"Test iPhone","model":"iPhone14,2","ios_version":"26.0.1","connection_type":"USB","livecontainer_bundle_id":null"#
        let cases: [(String, Bool)] = [
            (#","livecontainer_known":true}"#, true),
            (#","livecontainer_known":false}"#, false),
            ("}", false),
        ]
        for (tail, expected) in cases {
            guard case let .device(record) = try BuildEvent.decode(line: base + tail) else {
                return XCTFail("expected .device for \(tail)")
            }
            XCTAssertEqual(record.livecontainerKnown, expected, tail)
        }
    }
}

extension DeviceSwitcherTests {
    // MARK: - DevicesStore.remembered

    /// `device-seen`: the remembered devices that aren't described, the default
    /// among them, kept beside the real list and replaced with it.
    @MainActor
    func test_a_listing_keeps_the_devices_it_has_seen_before() async throws {
        let store = DevicesStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        let script = #"""
        printf '%s\n' '{"type":"device","udid":"00008030-0000000000000002","name":"iPad Air","model":"iPad13,16","ios_version":"26.1","connection_type":"USB","livecontainer_bundle_id":null,"is_default":false}' '{"type":"device-seen","udid":"00008030-0000000000000001","name":"iPhone 15 Pro","model":"iPhone16,1","ios_version":"26.1","is_default":true}' '{"type":"devices-end","count":1,"failed":0}'
        """#

        _ = try await store.load(argsOverride: ["-c", script])

        XCTAssertEqual(store.devices.map(\.udid), [Self.ipad])
        XCTAssertEqual(store.remembered.map(\.displayName), ["iPhone 15 Pro"])
        // `.first`, not `[0]`: an empty list fails here rather than crashing the run.
        XCTAssertEqual(store.remembered.first?.isDefault, true)

        let replaced = #"printf '%s\n' '{"type":"devices-end","count":0,"failed":0}'"#
        _ = try await store.load(argsOverride: ["-c", replaced])
        XCTAssertTrue(store.remembered.isEmpty, "a completed listing replaces it")
    }
}

extension DeviceSwitcherTests {
    // MARK: - The one-build pick (spec §5b, §11.5)

    /// A hold of its own per store, so a test can tell the build's Task has
    /// ended: the hold is released last in its `defer`.
    private final class NoTermination: TerminationControl {
        func disableSuddenTermination() {}
        func enableSuddenTermination() {}
        func disableAutomaticTermination(_ reason: String) {}
        func enableAutomaticTermination(_ reason: String) {}
    }

    @MainActor
    private func makeConfigStore(hold: TerminationHold? = nil) -> ConfigStore {
        ConfigStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")), notify: { _ in },
                    hold: hold ?? TerminationHold(control: NoTermination()))
    }

    /// Launch a build of example carrying a pick of the iPad, run `work`,
    /// and wait for its Task to end.
    @MainActor
    private func build(
        _ store: ConfigStore, hold: TerminationHold,
        _ work: @escaping @MainActor () async -> Void
    ) async {
        let started = expectation(description: "the build started")
        store.launchBuild(appName: "example", delivery: .livecontainer, clearingDevicePick: true) {
            started.fulfill()
            await work()
        }
        await fulfillment(of: [started], timeout: 5)
        await eventually({ !hold.isHolding }, "the build's Task never ended")
    }

    @MainActor
    private func succeed(_ store: ConfigStore) {
        store.note(.finished(ok: true, error: nil, elapsed: 1), forBuildOf: "example")
    }

    @MainActor
    private func startInstalling(_ store: ConfigStore) {
        store.note(.stage(name: CLIContract.Stage.liveContainerPush, status: "start"),
                   forBuildOf: "example")
    }

    @MainActor
    func test_a_pick_is_there_while_its_build_runs_and_gone_after_it_succeeds() async {
        let hold = TerminationHold(control: NoTermination())
        let store = makeConfigStore(hold: hold)
        store.pickDevice(Self.ipad, for: "example")
        var during: String?

        await build(store, hold: hold) {
            during = store.devicePick(for: "example")
            self.succeed(store)
        }

        XCTAssertEqual(during, Self.ipad, "the build reads the pick while it runs")
        XCTAssertNil(store.devicePick(for: "example"), "a build that succeeded used its pick")
    }

    /// For the retry: nothing reached the device.
    @MainActor
    func test_a_failure_before_installing_keeps_the_pick() async {
        let hold = TerminationHold(control: NoTermination())
        let store = makeConfigStore(hold: hold)
        store.pickDevice(Self.ipad, for: "example")

        await build(store, hold: hold) {
            store.noteBuildFailure(appName: "example", message: "cyan failed")
        }

        XCTAssertEqual(store.devicePick(for: "example"), Self.ipad)
    }

    @MainActor
    func test_a_failure_once_the_install_started_uses_the_pick() async {
        let hold = TerminationHold(control: NoTermination())
        let store = makeConfigStore(hold: hold)
        store.pickDevice(Self.ipad, for: "example")

        await build(store, hold: hold) {
            self.startInstalling(store)
            store.noteBuildFailure(appName: "example", message: "the push failed")
        }

        XCTAssertNil(store.devicePick(for: "example"))
    }

    @MainActor
    func test_a_stop_before_installing_keeps_the_pick() async {
        let hold = TerminationHold(control: NoTermination())
        let store = makeConfigStore(hold: hold)
        store.pickDevice(Self.ipad, for: "example")
        let started = expectation(description: "the build started")

        store.launchBuild(appName: "example", delivery: .livecontainer, clearingDevicePick: true) {
            started.fulfill()
            try? await Task.sleep(nanoseconds: 30_000_000_000)
        }
        await fulfillment(of: [started], timeout: 5)
        store.cancelBuild(appName: "example")
        await eventually({ !hold.isHolding }, "the stopped build's Task never ended")

        XCTAssertEqual(store.devicePick(for: "example"), Self.ipad)
    }

    @MainActor
    func test_a_stop_during_the_install_uses_the_pick() async {
        let hold = TerminationHold(control: NoTermination())
        let store = makeConfigStore(hold: hold)
        store.pickDevice(Self.ipad, for: "example")
        let installing = expectation(description: "the install started")

        store.launchBuild(appName: "example", delivery: .livecontainer, clearingDevicePick: true) {
            self.startInstalling(store)
            installing.fulfill()
            try? await Task.sleep(nanoseconds: 30_000_000_000)
        }
        await fulfillment(of: [installing], timeout: 5)
        store.cancelBuild(appName: "example")
        await eventually({ !hold.isHolding }, "the stopped build's Task never ended")

        XCTAssertNil(store.devicePick(for: "example"))
    }

    /// Picked mid-build, for the next Build & Install: the build ending
    /// doesn't take it away.
    @MainActor
    func test_a_pick_changed_while_the_build_ran_stays() async {
        let hold = TerminationHold(control: NoTermination())
        let store = makeConfigStore(hold: hold)
        store.pickDevice(Self.ipad, for: "example")

        await build(store, hold: hold) {
            store.pickDevice(Self.phone, for: "example")
            self.succeed(store)
        }

        XCTAssertEqual(store.devicePick(for: "example"), Self.phone)
    }

    @MainActor
    func test_a_run_that_doesnt_use_the_pick_keeps_it() async {
        let hold = TerminationHold(control: NoTermination())
        let store = makeConfigStore(hold: hold)
        store.pickDevice(Self.ipad, for: "example")
        let ran = expectation(description: "the build ran")

        store.launchBuild(appName: "example", delivery: .livecontainer) {
            ran.fulfill()
            self.succeed(store)
        }
        await fulfillment(of: [ran], timeout: 5)
        // Past the Task's `defer`, which is where a pick would go.
        await eventually({ !hold.isHolding }, "the build's Task never ended")

        XCTAssertEqual(store.devicePick(for: "example"), Self.ipad)
    }

    @MainActor
    func test_picks_are_per_app() async {
        let hold = TerminationHold(control: NoTermination())
        let store = makeConfigStore(hold: hold)
        store.pickDevice(Self.ipad, for: "example")
        store.pickDevice(Self.ipad, for: "sample")

        await build(store, hold: hold) { self.succeed(store) }

        XCTAssertNil(store.devicePick(for: "example"))
        XCTAssertEqual(store.devicePick(for: "sample"), Self.ipad)
    }

    /// The ✕ and Build options' Auto both send nil; "" is no pick either.
    @MainActor
    func test_nil_or_empty_clears_the_pick() {
        let store = makeConfigStore()
        store.pickDevice(Self.ipad, for: "example")
        store.pickDevice(nil, for: "example")
        XCTAssertNil(store.devicePick(for: "example"))

        store.pickDevice(Self.ipad, for: "example")
        store.pickDevice("", for: "example")
        XCTAssertTrue(store.devicePicks.isEmpty)
    }

    /// The name a pick was made with names it once it's unplugged, and goes
    /// with the pick.
    @MainActor
    func test_a_pick_keeps_the_name_it_was_made_with() {
        let store = makeConfigStore()
        store.pickDevice(Self.ipad, for: "example", name: "iPad Air")
        XCTAssertEqual(store.devicePickName(for: "example"), "iPad Air")

        store.pickDevice(nil, for: "example")
        XCTAssertNil(store.devicePickName(for: "example"))
    }
}

extension DeviceSwitcherTests {
    // MARK: - BuildOptions and the pick

    func test_picking_sets_the_device_and_nothing_else() {
        var options = BuildOptions()
        options.skipDecryptCheck = true

        let picked = options.picking(Self.ipad)

        XCTAssertEqual(picked.deviceUDID, Self.ipad)
        XCTAssertTrue(picked.skipDecryptCheck)
        XCTAssertNil(options.picking(nil).deviceUDID)
    }

    /// Only a run that installs uses the pick up; a preview, a skipped
    /// install, a Mac build and an export keep it for the build after.
    func test_which_runs_use_the_pick() {
        XCTAssertFalse(BuildOptions().usesDevicePick, "no pick, nothing to use")
        XCTAssertTrue(BuildOptions().picking(Self.ipad).usesDevicePick, "Build & Install")

        var dry = BuildOptions().picking(Self.ipad)
        dry.dryRun = true
        XCTAssertFalse(dry.usesDevicePick, "a dry run installs nothing")

        var skipped = BuildOptions().picking(Self.ipad)
        skipped.skipInstall = true
        XCTAssertFalse(skipped.usesDevicePick)

        var mac = BuildOptions().picking(Self.ipad)
        mac.forPlaycover = true
        XCTAssertFalse(mac.usesDevicePick)

        XCTAssertFalse(
            BuildOptions().picking(Self.ipad).exporting(to: "/tmp/x.ipa", replacing: false)
                .usesDevicePick,
            "an export drops the device"
        )
    }

    /// The bar's Change menu and Build options' Device picker set one thing,
    /// and it reaches the command line as `--device` (spec §5b).
    @MainActor
    func test_the_store_s_pick_goes_on_the_command_line() {
        let store = makeConfigStore()
        store.pickDevice(Self.ipad, for: "example")

        XCTAssertEqual(
            BuildOptions().picking(store.devicePick(for: "example")).cliArgs,
            [CLIContract.Flag.device, Self.ipad]
        )
    }
}

extension DeviceSwitcherTests {
    // MARK: - Settings' Default device

    func test_an_unplugged_default_is_named_in_the_picker() throws {
        let line = #"{"type":"device-seen","udid":"00008030-0000000000000001","name":"iPhone 15 Pro","model":null,"ios_version":null,"is_default":true}"#
        guard case let .deviceSeen(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .deviceSeen")
        }

        XCTAssertEqual(SettingsView.notConnectedLabel(Self.phone, remembered: [record]),
                       "iPhone 15 Pro (not connected)")
        XCTAssertEqual(SettingsView.notConnectedLabel("00008030-0000000000000009", remembered: [record]),
                       "00008030-0000000000000009 (not connected)", "never seen: its UDID")
    }
}

extension DeviceSwitcherTests {
    // MARK: - Make … the default

    /// From the bar's menu: the one setting written, and the store and an
    /// idle draft take the new value.
    @MainActor
    func test_make_default_writes_only_the_default_device() async throws {
        let dir = NSTemporaryDirectory()
        let capture = dir + "make_default_args_\(UUID().uuidString).txt"
        let scriptPath = dir + "make_default_stub_\(UUID().uuidString).sh"
        let script = """
        #!/bin/sh
        for arg in "$@"; do printf '%s\\n' "$arg" >> "\(capture)"; done
        printf '{"type":"settings-updated","changed":["default_device_udid"]}\\n'
        printf '{"type":"settings","default_device_udid":"\(Self.ipad)","archive_keep":2,"signing_cert":"/c.p12","signing_profile":"/p.mp","signing_team_id":"ABCDE12345"}\\n'
        """
        try script.write(toFile: scriptPath, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: scriptPath)
        defer {
            try? FileManager.default.removeItem(atPath: scriptPath)
            try? FileManager.default.removeItem(atPath: capture)
        }
        let store = SettingsStore(runner: CLIRunner(executable: URL(fileURLWithPath: scriptPath)))

        try await store.makeDefaultDevice(Self.ipad)

        let args = try String(contentsOfFile: capture, encoding: .utf8)
            .split(separator: "\n", omittingEmptySubsequences: true).map(String.init)
        XCTAssertEqual(args, [CLIContract.Flag.json, CLIContract.Flag.setSettings,
                              CLIContract.Flag.defaultDevice, Self.ipad])
        XCTAssertEqual(store.settings?.defaultDeviceUDID, Self.ipad)
        XCTAssertEqual(store.draft.edited.defaultDeviceUDID, Self.ipad, "nobody was editing")
    }

    /// An edit someone has open in Settings survives a Make default from the bar.
    @MainActor
    func test_make_default_keeps_an_edit_in_progress() async throws {
        let store = SettingsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        // Settings read, the draft seeded from it as the pane does, and an
        // edit left unsaved: a draft with no baseline has no changes to keep.
        let read = #"printf '%s\n' '{"type":"settings","default_device_udid":"00008030-0000000000000001","archive_keep":2,"signing_cert":"/c.p12","signing_profile":"/p.mp","signing_team_id":"ABCDE12345"}'"#
        _ = try await store.loadSettings(argsOverride: ["-c", read])
        store.resetDraft()
        store.draft.edited.archiveKeep = 9
        XCTAssertTrue(store.draft.hasChanges, "premise: an edit is in progress")
        let script = #"printf '%s\n' '{"type":"settings-updated","changed":["default_device_udid"]}' '{"type":"settings","default_device_udid":"00008030-0000000000000002","archive_keep":2,"signing_cert":"/c.p12","signing_profile":"/p.mp","signing_team_id":"ABCDE12345"}'"#

        try await store.makeDefaultDevice(Self.ipad, argsOverride: ["-c", script])

        XCTAssertEqual(store.settings?.defaultDeviceUDID, Self.ipad)
        XCTAssertEqual(store.draft.edited.archiveKeep, 9, "the open edit was thrown away")
    }
}

extension DeviceSwitcherTests {
    // MARK: - Wiring, on screen

    private static let appLine = #"{"type":"app","name":"example","bundle_id":"com.example.app","preferred_version":"latest","tweaks":[],"display_name":"Example+","delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}"#

    private static func deviceLine(_ udid: String, _ name: String, isDefault: Bool = false) -> String {
        #"{"type":"device","udid":"\#(udid)","name":"\#(name)","model":"iPad13,16","ios_version":"26.1","connection_type":"USB","livecontainer_bundle_id":"com.kdt.livecontainer.ABCDE12345","livecontainer_known":true,"is_default":\#(isDefault)}"#
    }

    /// example's pane, on screen. Its CLI is a script that writes each argv
    /// to `capture` and prints the app for `--list`; `devices` is what
    /// `--devices` listed; `pick` is the app's pick for Build & Install.
    @MainActor
    private func pane(devices lines: [String], pick: String?) async throws -> (Hosted, ConfigStore, URL) {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-switcher-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let capture = dir.appendingPathComponent("argv.txt")
        let cli = dir.appendingPathComponent("graft")
        try """
        #!/bin/sh
        printf '%s\\n' "$*" >> "\(capture.path)"
        case " $* " in
          *" --list "*) printf '%s\\n' '\(Self.appLine)' ;;
        esac
        exit 0
        """.write(to: cli, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: cli.path)

        let sh = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let config = ConfigStore(runner: CLIRunner(executable: cli), notify: { _ in },
                                 hold: TerminationHold(control: NoTermination()))
        config.postNote = { _ in }
        let apps = try await config.loadApps()
        let app = try XCTUnwrap(apps.first)
        let devices = DevicesStore(runner: sh)
        let listing = (lines + [#"{"type":"devices-end","count":\#(lines.count),"failed":0}"#])
            .map { "'\($0)'" }.joined(separator: " ")
        _ = try await devices.load(argsOverride: ["-c", "printf '%s\\n' \(listing)"])
        config.pickDevice(pick, for: "example")
        let view = AppDetailView(
            app: app, runner: CLIRunner(executable: cli), store: config,
            artwork: ArtworkStore(runner: sh), devices: devices,
            tweaksStore: TweaksStore(runner: sh), staged: StagedIPAStore(runner: sh),
            appStore: AppStoreStore(runner: sh, shell: NoAdminShell()),
            liveContainer: LiveContainerStore(runner: sh), settings: SettingsStore(runner: sh),
            showSettings: {}, playCover: .inert
        )
        let box = hosted(view, size: NSSize(width: 1000, height: 1000))
        try await Task.sleep(nanoseconds: 300_000_000)
        settle(box, seconds: 0.2)
        return (box, config, capture)
    }

    @MainActor
    private func buildButton(in box: Hosted) throws -> AccessibilityTree.Node {
        try XCTUnwrap(
            try AccessibilityTree.nodes(in: box.window)
                .first { $0.role == kAXButtonRole && $0.name == "Build & Install" },
            "no Build & Install button"
        )
    }

    /// The bar's `blocksBuild` reaches the button: several devices and none
    /// picked, or a picked device that isn't connected.
    @MainActor
    func test_build_and_install_waits_while_the_bar_says_it_can_only_fail() async throws {
        try AccessibilityTree.requireServed()
        let several = [Self.deviceLine(Self.ipad, "iPad Air"),
                       Self.deviceLine("00008030-0000000000000003", "Spare iPad")]
        let (waiting, _, _) = try await pane(devices: several, pick: nil)
        XCTAssertEqual(try buildButton(in: waiting).enabled, false, "several devices, none picked")

        let (unplugged, _, _) = try await pane(devices: [Self.deviceLine(Self.ipad, "iPad Air")],
                                               pick: "00008030-0000000000000009")
        XCTAssertEqual(try buildButton(in: unplugged).enabled, false, "the picked device isn't here")

        let (ready, _, _) = try await pane(devices: several, pick: Self.ipad)
        XCTAssertEqual(try buildButton(in: ready).enabled, true, "premise: a pick unblocks it")
    }

    @MainActor
    func test_the_x_clears_the_pick() async throws {
        try AccessibilityTree.requireServed()
        let (box, config, _) = try await pane(devices: [Self.deviceLine(Self.ipad, "iPad Air")],
                                              pick: Self.ipad)

        try clickButton(named: "Clear the device picked for Build & Install", in: box.window)

        await eventually({ config.devicePick(for: "example") == nil }, "the ✕ left the pick")
    }

    /// Check device asks about the device this app was last delivered to,
    /// which the CLI reads from its own record: no `--device`, and never the
    /// pick, which is the next Build & Install's.
    @MainActor
    func test_check_device_sends_no_device_and_never_the_pick() async throws {
        try AccessibilityTree.requireServed()
        let (box, _, capture) = try await pane(devices: [Self.deviceLine(Self.ipad, "iPad Air")],
                                               pick: Self.ipad)

        try clickButton(named: "Check device", in: box.window)

        var reconcile: String?
        await eventually({
            reconcile = (try? String(contentsOf: capture, encoding: .utf8))?
                .split(separator: "\n").map(String.init).first { $0.contains("--reconcile") }
            return reconcile != nil
        }, "Check device never ran --reconcile")
        XCTAssertFalse(reconcile?.contains("--device") ?? true, reconcile ?? "")
    }
}
