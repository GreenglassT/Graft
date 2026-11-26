import AppKit
import SwiftUI
import XCTest
@testable import Graft

/// The update UI on screen: hosted views, read through the accessibility
/// tree, and the views that compose them. Each on-screen test skips in its
/// first line when macOS isn't serving the tree.
@MainActor
final class AppUpdateViewsTests: XCTestCase {
    private typealias F = UpdatePlanFixture

    private final class InertControl: TerminationControl, @unchecked Sendable {
        func disableSuddenTermination() {}
        func enableSuddenTermination() {}
        func disableAutomaticTermination(_ reason: String) {}
        func enableAutomaticTermination(_ reason: String) {}
    }

    private func stale(_ app: String = "example") -> DeliveryStateRecord {
        DeliveryStateRecord(app: app, kind: .stale, summary: "Something changed.", reasons: [], pushedAt: nil)
    }

    private func nodes<V: View>(_ view: V, width: CGFloat = 520) throws -> [AccessibilityTree.Node] {
        let h = hosted(view, width: width)
        settle(h)
        return try AccessibilityTree.nodes(in: h.window)
    }

    /// A stand-in `graft`: `quick` for the quick plan, `full` for the full
    /// one, `list` for `--list`, and `run` for anything else, after
    /// `runFirst`.
    private func fakeCLI(quick: String = "", full: String = "", list: String = "", run: String = "",
                         runFirst: String = ":") throws -> ScriptCLI {
        try ScriptCLI("""
        case "$*" in
        *--quick*)
        cat <<'EOF'
        \(quick)
        EOF
        ;;
        *--plan*)
        cat <<'EOF'
        \(full)
        EOF
        ;;
        *--list*)
        cat <<'EOF'
        \(list)
        EOF
        ;;
        *)
        \(runFirst)
        cat <<'EOF'
        \(run)
        EOF
        ;;
        esac
        """, test: self)
    }

    /// A store and its `ConfigStore` over `cli`, with a throwaway defaults
    /// suite and a hold that does nothing.
    private func stores(_ cli: ScriptCLI, checkApps: Bool = true) throws -> (AppUpdatesStore, ConfigStore) {
        let name = "AppUpdateViewsTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: name))
        addTeardownBlock { defaults.removePersistentDomain(forName: name) }
        defaults.set(checkApps, forKey: AppUpdatesStore.Key.check)
        let hold = TerminationHold(control: InertControl())
        let config = ConfigStore(runner: cli.runner, notify: { _ in }, hold: hold)
        let store = AppUpdatesStore(runner: cli.runner, config: config, defaults: defaults, now: Date.init, hold: hold)
        return (store, config)
    }

    // MARK: The sidebar

    func test_a_row_with_an_update_shows_the_glyph_in_place_of_the_dot() throws {
        try AccessibilityTree.requireServed()
        let row = AppSidebarRow(app: F.appRecord("example", displayName: "Example+"), state: stale(),
                                isSelected: false, artwork: nil, update: F.plan(F.app("example")))

        let found = try nodes(row, width: 260)

        let glyph = try XCTUnwrap(found.first { $0.name == AppUpdateText.badgeLabel }, "VoiceOver's words for the glyph")
        XCTAssertEqual(glyph.help, "Update available: the latest build isn't on Example iPhone yet",
                       "its tooltip names the device")
        XCTAssertFalse(found.contains { ($0.value as? String) == AppUpdateText.badge },
                       "no text chip in the 260 pt row (review S8)")
    }

    func test_a_row_with_nothing_to_update_has_no_glyph() throws {
        try AccessibilityTree.requireServed()
        let row = AppSidebarRow(app: F.appRecord("example", displayName: "Example+"), state: stale(),
                                isSelected: false, artwork: nil,
                                update: F.plan(F.app("example", install: false, nothingToDo: true)))

        XCTAssertFalse(try nodes(row, width: 260).contains { $0.name == AppUpdateText.badgeLabel })
    }

    func test_update_all_shows_for_two_or_more_apps() {
        XCTAssertFalse(SidebarView.showsUpdateAll(count: 1))
        XCTAssertTrue(SidebarView.showsUpdateAll(count: 2))
    }

    private struct SidebarHarness: View {
        @ObservedObject var config: ConfigStore
        @PassedThrough var appUpdates: AppUpdatesStore
        @State var selection: SidebarItem?

        var body: some View {
            NavigationSplitView {
                SidebarView(
                    store: config, selection: $selection,
                    artwork: ArtworkStore(runner: config.runner), devices: DevicesStore(runner: config.runner),
                    appStore: AppStoreStore(runner: config.runner, shell: NoAdminShell()),
                    appUpdates: appUpdates
                )
                .navigationSplitViewColumnWidth(min: 220, ideal: 260, max: 360)
            } detail: {
                Text("detail")
            }
        }
    }

    /// Composition (review S10): the sidebar reads the store's plans in its
    /// own order, marks and offers Update All for the apps Update can do
    /// something for now (not one waiting for a device, spec §11.3), and
    /// Update All opens the confirmation for them.
    func test_the_sidebar_offers_update_all_for_the_apps_with_an_update_and_opens_the_confirmation() async throws {
        try AccessibilityTree.requireServed()
        let cli = try fakeCLI(
            quick: F.planLine([F.app("example"), F.app("bravo", tweaks: [F.tweak("a.deb")]),
                               F.app("charlie", to: "2.0", waiting: F.notConnected("Example iPad")),
                               F.app("delta", install: false, nothingToDo: true)]),
            list: [F.appLine("example"), F.appLine("bravo"), F.appLine("charlie"), F.appLine("delta")]
                .joined(separator: "\n")
        )
        let (appUpdates, config) = try stores(cli)
        _ = try await config.loadApps()
        await appUpdates.checkIfDue()
        let h = hosted(SidebarHarness(config: config, appUpdates: appUpdates), size: NSSize(width: 700, height: 600))
        settle(h)

        XCTAssertEqual(try AccessibilityTree.nodes(in: h.window).filter { $0.name == AppUpdateText.badgeLabel }.count, 2)
        try clickButton(named: "Update All (2)", in: h.window)
        settle(h, seconds: 0.2)

        XCTAssertEqual(appUpdates.sheet, .confirm)
        XCTAssertEqual(appUpdates.confirmation?.apps, ["bravo", "example"], "sidebar order")
    }

    /// With the switch off, the sidebar still offers the check by hand, which
    /// runs the quick plan (spec §11.4).
    func test_with_the_switch_off_the_sidebar_offers_check_for_updates_now() async throws {
        try AccessibilityTree.requireServed()
        let cli = try fakeCLI(quick: F.planLine([F.app("example")]), list: F.appLine("example"))
        let (appUpdates, config) = try stores(cli, checkApps: false)
        _ = try await config.loadApps()
        let h = hosted(SidebarHarness(config: config, appUpdates: appUpdates), size: NSSize(width: 700, height: 600))
        settle(h)

        try clickButton(named: AppUpdateText.checkNow, in: h.window)

        await eventually { cli.argv().contains("--json --update --all --plan --quick") }
        await eventually { appUpdates.plans["example"] != nil }
    }

    // MARK: The app pane

    func test_the_pane_line_says_what_is_due_with_its_button() throws {
        try AccessibilityTree.requireServed()
        let line = try XCTUnwrap(AppUpdateText.paneLine(F.plan(F.app("example", to: "2026.47.0",
                                                                     tweaks: [F.tweak("a.deb")]))))

        let found = try nodes(AppUpdateLine(line: line, isBusy: false, help: "where it goes") {})

        XCTAssertTrue(found.contains { ($0.value as? String) == "Update available: App Store 2026.47.0 · 1 tweak" })
        let button = try XCTUnwrap(found.first { $0.role == kAXButtonRole && $0.name == "Update & Install" })
        XCTAssertEqual(button.enabled, true)
        XCTAssertEqual(button.help, "where it goes")
    }

    /// A device block is a note, and Update stays offered (review S3).
    func test_a_blocked_app_shows_its_reason_and_keeps_its_button() throws {
        try AccessibilityTree.requireServed()
        let line = try XCTUnwrap(AppUpdateText.paneLine(F.plan(F.app("example", to: "2.0",
                                                                     blocked: "Example iPad isn't connected"))))

        let found = try nodes(AppUpdateLine(line: line, isBusy: false) {})

        XCTAssertTrue(found.contains { ($0.value as? String) == "Example iPad isn't connected" })
        XCTAssertEqual(found.filter { $0.role == kAXButtonRole }.map(\.name), ["Update & Install"])
    }

    /// Work waiting for a device (spec §11.3): its sentence, a menu that
    /// holds Stop updating, and no Update button, as nothing could run.
    func test_a_waiting_device_shows_its_sentence_and_a_menu_and_no_button() throws {
        try AccessibilityTree.requireServed()
        let line = try XCTUnwrap(AppUpdateText.paneLine(F.plan(F.app("example", targets: [
            F.target("Example iPhone", install: false),
            F.target("Example iPad", udid: F.pad, waiting: F.notConnected("Example iPad")),
        ])), title: "Example+"))

        let found = try nodes(AppUpdateLine(line: line, isBusy: false) {})

        XCTAssertTrue(found.contains { ($0.value as? String) == F.notConnected("Example iPad") })
        XCTAssertNotNil(found.first { $0.name == AppUpdateText.waitingMenuName(device: "Example iPad") }, "its menu")
        XCTAssertEqual(found.filter { $0.role == kAXButtonRole }.map(\.name), [], "nothing can run")
    }

    /// The devices a new build installs on are named under the headline.
    func test_the_line_names_the_devices_it_installs_on() throws {
        try AccessibilityTree.requireServed()
        let line = try XCTUnwrap(AppUpdateText.paneLine(F.plan(F.app("example", to: "2.0", targets: [
            F.target("Example iPhone"), F.target("Spare iPhone", udid: F.spare),
        ]))))

        let found = try nodes(AppUpdateLine(line: line, isBusy: false) {})

        XCTAssertTrue(found.contains { ($0.value as? String) == "Installs on Example iPhone and Spare iPhone" })
        XCTAssertNotNil(found.first { $0.role == kAXButtonRole && $0.name == "Update & Install" })
    }

    func test_intel_shows_the_newer_version_with_no_button_for_it() throws {
        try AccessibilityTree.requireServed()
        let line = try XCTUnwrap(AppUpdateText.paneLine(F.plan(F.app(
            "example", to: "2026.47.0", skipped: CLIContract.Update.needsAppleSilicon, install: false,
            nothingToDo: true))))

        let found = try nodes(AppUpdateLine(line: line, isBusy: false) {})

        XCTAssertTrue(found.contains { ($0.value as? String) == "App Store has 2026.47.0, needs Apple silicon" })
        XCTAssertEqual(found.filter { $0.role == kAXButtonRole }.map(\.name), [])
    }

    func test_while_busy_the_button_waits() throws {
        try AccessibilityTree.requireServed()
        let line = try XCTUnwrap(AppUpdateText.paneLine(F.plan(F.app("example"))))

        let button = try XCTUnwrap(try nodes(AppUpdateLine(line: line, isBusy: true) {})
            .first { $0.role == kAXButtonRole })

        XCTAssertEqual(button.name, "Install Update")
        XCTAssertEqual(button.enabled, false)
    }

    /// Review S9: the button waits for the busy slot, and for this app's own
    /// Get, an update of a tweak it uses, and a build refusal.
    func test_the_panes_update_waits_for_what_stands_in_its_way() {
        XCTAssertFalse(AppDetailView.updateIsBusy(busy: false, getting: false, updatingTweak: false, refused: false))
        XCTAssertTrue(AppDetailView.updateIsBusy(busy: true, getting: false, updatingTweak: false, refused: false))
        XCTAssertTrue(AppDetailView.updateIsBusy(busy: false, getting: true, updatingTweak: false, refused: false))
        XCTAssertTrue(AppDetailView.updateIsBusy(busy: false, getting: false, updatingTweak: true, refused: false))
        XCTAssertTrue(AppDetailView.updateIsBusy(busy: false, getting: false, updatingTweak: false, refused: true))
    }

    private func detail(_ app: AppRecord, config: ConfigStore, appUpdates: AppUpdatesStore) -> some View {
        let runner = config.runner
        return AppDetailView(
            app: app, runner: runner, store: config,
            artwork: ArtworkStore(runner: runner), devices: DevicesStore(runner: runner),
            tweaksStore: TweaksStore(runner: runner), staged: StagedIPAStore(runner: runner),
            appStore: AppStoreStore(runner: runner, shell: NoAdminShell()),
            liveContainer: LiveContainerStore(runner: runner), settings: SettingsStore(runner: runner),
            showSettings: {}, playCover: .inert, appUpdates: appUpdates
        )
    }

    /// Composition (review S10): the pane reads its app's plan from the store,
    /// shows its line with its button, and the button waits while the busy
    /// slot is held (here by an update of another app).
    func test_the_pane_shows_its_apps_update_line_and_waits_while_busy() async throws {
        try AccessibilityTree.requireServed()
        let cli = try fakeCLI(quick: F.planLine([F.app("example", to: "2026.47.0")]))
        let (appUpdates, config) = try stores(cli)
        await appUpdates.checkIfDue()
        let h = hosted(detail(F.appRecord("example"), config: config, appUpdates: appUpdates),
                       size: NSSize(width: 900, height: 700))
        settle(h)

        var found = try AccessibilityTree.nodes(in: h.window)
        XCTAssertTrue(found.contains { ($0.value as? String) == "Update available: App Store 2026.47.0" })
        XCTAssertEqual(found.first { $0.role == kAXButtonRole && $0.name == "Update & Install" }?.enabled, true)

        XCTAssertTrue(config.beginUpdate(apps: ["bravo"]))
        settle(h)
        found = try AccessibilityTree.nodes(in: h.window)
        XCTAssertEqual(found.first { $0.role == kAXButtonRole && $0.name == "Update & Install" }?.enabled, false)
        XCTAssertEqual(found.first { $0.role == kAXButtonRole && $0.name == "Build & Install" }?.enabled, false,
                       "Build & Install waits for an update of any app")
        config.endUpdate()
    }

    /// Composition: the pane passes its app's waiting devices on, and says
    /// when Stop updating was refused.
    func test_the_pane_shows_a_waiting_device_and_a_refused_stop_updating() async throws {
        try AccessibilityTree.requireServed()
        let cli = try fakeCLI(
            quick: F.planLine([F.app("example", to: "2.0", targets: [
                F.target("Example iPhone"), F.target("Example iPad", udid: F.pad, waiting: F.notConnected("Example iPad")),
            ])]),
            runFirst: "echo \"error: example isn't on that device\" >&2; exit 2"
        )
        let (appUpdates, config) = try stores(cli)
        await appUpdates.checkIfDue()
        let h = hosted(detail(F.appRecord("example"), config: config, appUpdates: appUpdates),
                       size: NSSize(width: 900, height: 700))
        settle(h)

        var found = try AccessibilityTree.nodes(in: h.window)
        XCTAssertTrue(found.contains { ($0.value as? String) == F.notConnected("Example iPad") })
        XCTAssertNotNil(found.first { $0.name == AppUpdateText.waitingMenuName(device: "Example iPad") })

        await appUpdates.dropTarget(app: "example", udid: F.pad)
        settle(h)
        found = try AccessibilityTree.nodes(in: h.window)
        XCTAssertTrue(found.contains { ($0.value as? String)?.contains("isn't on that device") == true },
                      "the refusal, under the line")
    }

    private static let appLine = #"{"type":"app","name":"example","bundle_id":"com.example.app","preferred_version":"latest","tweaks":[],"display_name":"Example+","delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}"#

    private static func deviceLine(_ udid: String, _ name: String, isDefault: Bool = false) -> String {
        #"{"type":"device","udid":"\#(udid)","name":"\#(name)","model":"iPad13,16","ios_version":"26.1","connection_type":"USB","livecontainer_bundle_id":"com.kdt.livecontainer.ABCDE12345","livecontainer_known":true,"is_default":\#(isDefault)}"#
    }

    /// example's pane with its update line: the quick plan offers an
    /// install on the iPad after a Re-install of an older build there (spec
    /// §11.1), the iPhone being current. `devices` is what `--devices`
    /// listed.
    private func offeredPane(devices lines: [String]) async throws -> (Hosted, ConfigStore, ScriptCLI) {
        let plan = F.planLine([F.app("example", targets: [
            F.target("Example iPhone", install: false),
            F.target("Example iPad", udid: F.pad, install: false, installSkipped: "Re-installed an older build",
                     manualInstall: true),
        ])])
        let cli = try ScriptCLI("""
        case " $* " in
          *" --list "*) printf '%s\\n' '\(Self.appLine)' ;;
          *" --quick "*) printf '%s\\n' '\(plan)' ;;
        esac
        exit 0
        """, test: self)
        let (appUpdates, config) = try stores(cli)
        config.postNote = { _ in }
        let apps = try await config.loadApps()
        let app = try XCTUnwrap(apps.first)
        await appUpdates.checkIfDue()
        let sh = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let devices = DevicesStore(runner: sh)
        let listing = (lines + [#"{"type":"devices-end","count":\#(lines.count),"failed":0}"#])
            .map { "'\($0)'" }.joined(separator: " ")
        _ = try await devices.load(argsOverride: ["-c", "printf '%s\\n' \(listing)"])
        let view = AppDetailView(
            app: app, runner: cli.runner, store: config,
            artwork: ArtworkStore(runner: sh), devices: devices,
            tweaksStore: TweaksStore(runner: sh), staged: StagedIPAStore(runner: sh),
            appStore: AppStoreStore(runner: sh, shell: NoAdminShell()),
            liveContainer: LiveContainerStore(runner: sh), settings: SettingsStore(runner: sh),
            showSettings: {}, playCover: .inert, appUpdates: appUpdates
        )
        let h = hosted(view, size: NSSize(width: 1000, height: 1000))
        settle(h)
        return (h, config, cli)
    }

    /// An install offered after a Re-install of an older build (spec §11.1)
    /// is a Build & Install to the device it names, here the iPad, not to the
    /// bar's device (the default iPhone): its press picks the iPad for that
    /// build, which carries it as `--device`.
    func test_an_offered_install_builds_for_the_device_it_names() async throws {
        try AccessibilityTree.requireServed()
        let (h, config, cli) = try await offeredPane(devices: [
            Self.deviceLine(F.phone, "Example iPhone", isDefault: true), Self.deviceLine(F.pad, "Example iPad"),
        ])
        XCTAssertNil(config.devicePick(for: "example"), "premise: nothing picked, so the bar's device is the iPhone")

        try clickButton(named: "Install Update", in: h.window)

        // The build's argv is `--json`, its options (a pick's `--device
        // <udid>`), then the app: no other verb.
        func isBuild(_ argv: String) -> Bool {
            let words = argv.split(separator: " ")
            return words.last == "example"
                && words.dropLast().allSatisfy { $0 == "--json" || $0 == "--device" || !$0.hasPrefix("--") }
        }
        var build: String?
        await eventually({
            build = cli.argv().first(where: isBuild)
            return build != nil
        }, "Install Update never started a build")
        XCTAssertEqual(build, "--json --device \(F.pad) example")
    }

    /// Its device unplugged, the offered install waits, saying so, rather
    /// than going to the device that is connected, which the app was never
    /// on (review part 2, 3B).
    func test_an_offered_install_waits_while_its_device_is_unplugged() async throws {
        try AccessibilityTree.requireServed()
        let (h, config, _) = try await offeredPane(devices: [Self.deviceLine(F.spare, "Spare iPhone")])

        let button = try XCTUnwrap(try AccessibilityTree.nodes(in: h.window)
            .first { $0.role == kAXButtonRole && $0.name == "Install Update" })
        XCTAssertEqual(button.enabled, false)
        XCTAssertEqual(button.help, "Example iPad isn't connected. Connect it to install the update there.")
        XCTAssertNil(config.devicePick(for: "example"))
    }

    /// The offered install's block, in words (spec §11.1, review part 2,
    /// 3B): its device unplugged says so; a device that can take the build
    /// blocks nothing.
    func test_an_offered_installs_block_says_its_device_isnt_connected() {
        XCTAssertEqual(AppDetailView.offeredBlock(.overrideMissing(udid: F.pad, name: "Example iPad"),
                                                  device: "Example iPad"),
                       "Example iPad isn't connected. Connect it to install the update there.")
        XCTAssertNil(AppDetailView.offeredBlock(.thisBuild(name: "Example iPad", mode: "LiveContainer",
                                                           then: BuildTargetBar.askEachTime), device: "Example iPad"))
        let missing = BuildTarget.liveContainerMissing(name: "Example iPad", udid: F.pad, kind: "iPad")
        XCTAssertEqual(AppDetailView.offeredBlock(missing, device: "Example iPad"),
                       BuildTargetBar.explanation(for: missing))
    }

    // MARK: The sheet

    private func sheetNodes(_ store: AppUpdatesStore, names: [String: String] = [:]) throws -> [AccessibilityTree.Node] {
        let h = hosted(AppUpdatesSheet(store: store, names: names), width: 520)
        settle(h)
        return try AccessibilityTree.nodes(in: h.window)
    }

    func test_the_confirmation_lists_each_app_with_the_import_note_and_the_shared_tweak_toggle() async throws {
        try AccessibilityTree.requireServed()
        let full = F.planLine([
            F.app("example", displayName: "Example+", to: "2.0",
                  tweaks: [F.tweak("a-tweak.deb", name: "A Tweak", also: ["bravo"])], readiness: "checked"),
            F.app("delta", blocked: "Developer Mode is off on Example iPad", readiness: "checked"),
        ], quick: false)
        let (store, _) = try stores(try fakeCLI(full: full))
        store.openConfirmation(apps: ["example", "delta"])
        await eventually { store.confirmation?.plan != nil }

        let nodes = try sheetNodes(store, names: ["bravo": "Bravo"])

        let texts = nodes.compactMap { $0.value as? String }
        XCTAssertTrue(texts.contains("App Store 1.0 → 2.0"))
        XCTAssertTrue(texts.contains("A Tweak 1.0 → 1.1"))
        XCTAssertTrue(texts.contains("Example+ → Example iPhone via LiveContainer"), "its device")
        XCTAssertTrue(texts.contains(AppUpdateText.importNote(device: "Example iPhone")))
        XCTAssertTrue(texts.contains("also used by Bravo: it will need an install afterward"))
        XCTAssertTrue(texts.contains("Developer Mode is off on Example iPad"))
        let toggle = try XCTUnwrap(nodes.first { $0.role == kAXCheckBoxRole })
        XCTAssertEqual(toggle.name, "Also install Bravo now")
        XCTAssertEqual((toggle.value as? NSNumber)?.boolValue, false, "off by default")
        XCTAssertNotNil(nodes.first { $0.role == kAXButtonRole && $0.name == "Update" && $0.enabled == true },
                        "one app ready: Update")
        XCTAssertNotNil(nodes.first { $0.role == kAXButtonRole && $0.name == "Cancel" })
    }

    /// A device that isn't connected is listed with its sentence and its
    /// Stop updating, which drops that device for that app (spec §11.3).
    func test_the_confirmations_waiting_row_offers_stop_updating_for_that_device() async throws {
        try AccessibilityTree.requireServed()
        let full = F.planLine([F.app("example", displayName: "Example+", to: "2.0", readiness: "checked", targets: [
            F.target("Example iPhone"), F.target("Example iPad", udid: F.pad, waiting: F.notConnected("Example iPad")),
        ])], quick: false)
        let cli = try fakeCLI(full: full)
        let (store, _) = try stores(cli)
        store.openConfirmation(apps: ["example"])
        await eventually { store.confirmation?.plan != nil }
        let h = hosted(AppUpdatesSheet(store: store, names: [:]), width: 520)
        settle(h)

        let texts = try AccessibilityTree.nodes(in: h.window).compactMap { $0.value as? String }
        XCTAssertTrue(texts.contains("→ Example iPad via LiveContainer"))
        XCTAssertTrue(texts.contains(F.notConnected("Example iPad")))
        try clickButton(named: "Stop updating Example+ on Example iPad", in: h.window)

        await eventually {
            cli.argv().contains("--json \(CLIContract.Flag.dropUpdateTarget) example --device \(F.pad)")
        }
        await eventually { cli.argv().filter { $0 == "--json --update example --plan" }.count == 2 }
    }

    /// The button waits for the full plan: Update, disabled, while it is read.
    func test_the_update_button_waits_for_the_full_plan() async throws {
        try AccessibilityTree.requireServed()
        // The full plan doesn't answer until the test says so.
        let go = FileManager.default.temporaryDirectory.appending(path: "plan-go-\(UUID().uuidString)")
        addTeardownBlock { try? FileManager.default.removeItem(at: go) }
        let cli = try ScriptCLI("""
        case "$*" in *--plan*)
        i=0
        while [ ! -e '\(go.path)' ] && [ $i -lt 200 ]; do /bin/sleep 0.05; i=$((i+1)); done
        echo '\(F.planLine([F.app("example", readiness: "checked")], quick: false))'
        ;; esac
        """, test: self)
        let (store, _) = try stores(cli)
        store.openConfirmation(apps: ["example"])
        let h = hosted(AppUpdatesSheet(store: store, names: [:]), width: 520)
        settle(h)

        XCTAssertEqual(store.confirmation?.isLoading, true, "premise: still reading the plan")
        let update = try AccessibilityTree.nodes(in: h.window).first { $0.role == kAXButtonRole && $0.name == "Update" }
        XCTAssertEqual(update?.enabled, false, "not before the full plan is read")

        FileManager.default.createFile(atPath: go.path, contents: nil)
        await eventually { store.confirmation?.plan != nil }
        settle(h)
        let ready = try AccessibilityTree.nodes(in: h.window).first { $0.role == kAXButtonRole && $0.name == "Update" }
        XCTAssertEqual(ready?.enabled, true)
    }

    /// A full plan with nothing left says so, and Done closes the sheet.
    func test_nothing_left_says_so_and_offers_done() async throws {
        try AccessibilityTree.requireServed()
        let full = F.planLine([F.app("example", install: false, readiness: "checked", nothingToDo: true)], quick: false)
        let (store, _) = try stores(try fakeCLI(full: full))
        store.openConfirmation(apps: ["example"])
        await eventually { store.confirmation?.plan != nil }

        let nodes = try sheetNodes(store)

        XCTAssertTrue(nodes.contains { ($0.value as? String) == AppUpdateText.nothingLeft })
        XCTAssertEqual(nodes.first { $0.role == kAXButtonRole && $0.name == "Done" }?.enabled, true)
    }

    func test_the_progress_sheet_shows_each_app_the_log_and_try_again() async throws {
        try AccessibilityTree.requireServed()
        let run = [F.step("example", "build", "start", "Example iPhone"),
                   F.appEnd("example", ok: true, done: ["build"], targets: [F.targetEnd(ok: true)]),
                   F.appEnd("bravo", ok: false, left: ["build", "install"], error: "cyan exited with code 1",
                            targets: [F.targetEnd(ok: false, left: ["install"])]),
                   F.end(ok: false, updated: ["example"], failed: ["bravo"])].joined(separator: "\n")
        let (store, _) = try stores(try fakeCLI(run: run))
        store.start(apps: ["example", "bravo"], withAffected: false,
                    plan: F.record([F.app("example", displayName: "Example+"), F.app("bravo")]))
        await eventually { store.lastFinished != nil }

        let nodes = try sheetNodes(store)

        let texts = nodes.compactMap { $0.value as? String }
        XCTAssertTrue(texts.contains("Example+"))
        XCTAssertTrue(texts.contains(AppUpdateText.deviceLabel("Example iPhone")), "each device under its app")
        XCTAssertTrue(texts.contains("Done"))
        XCTAssertTrue(texts.contains("Open LiveContainer on Example iPhone to import it"))
        XCTAssertTrue(texts.contains("Failed"))
        XCTAssertTrue(texts.contains("cyan exited with code 1. Left to do: the build and install."))
        XCTAssertTrue(texts.contains { $0.contains("→ example: build") }, "the live log")
        let names = nodes.filter { $0.role == kAXButtonRole }.map(\.name)
        XCTAssertTrue(names.contains("Try Again"))
        XCTAssertTrue(names.contains("Done"))
        XCTAssertFalse(names.contains("Stop"), "nothing to stop")
        XCTAssertFalse(names.contains(AppUpdateText.tryAgainName(app: "Example+", device: "Example iPhone")),
                       "a device that's done has nothing to try again")
    }

    /// A device that didn't get its install has its own Try Again, which
    /// confirms its app again.
    func test_a_device_that_did_not_install_has_its_own_try_again() async throws {
        try AccessibilityTree.requireServed()
        let run = [F.appEnd("example", ok: true, targets: [F.targetEnd(ok: true)]),
                   F.appEnd("bravo", ok: false, left: ["install"], error: "the install failed",
                            targets: [F.targetEnd(ok: true),
                                      F.targetEnd(F.pad, device: "Example iPad", ok: false, error: "the install failed",
                                                  left: ["install"])]),
                   F.end(ok: false, updated: ["example"], failed: ["bravo"])].joined(separator: "\n")
        let (store, _) = try stores(try fakeCLI(full: F.planLine([F.app("bravo", readiness: "checked")], quick: false),
                                                run: run))
        store.start(apps: ["example", "bravo"], withAffected: false, plan: F.record([
            F.app("example"),
            F.app("bravo", displayName: "Bravo+", targets: [F.target("Example iPhone"), F.target("Example iPad", udid: F.pad)]),
        ]))
        await eventually { store.lastFinished != nil }
        let h = hosted(AppUpdatesSheet(store: store, names: [:]), width: 520)
        settle(h)

        let texts = try AccessibilityTree.nodes(in: h.window).compactMap { $0.value as? String }
        XCTAssertTrue(texts.contains("the install failed. Left to do: the install."))
        try clickButton(named: AppUpdateText.tryAgainName(app: "Bravo+", device: "Example iPad"), in: h.window)

        await eventually { store.sheet == .confirm }
        XCTAssertEqual(store.confirmation?.apps, ["bravo"])
    }

    func test_while_it_runs_the_sheet_offers_stop_and_says_what_stop_waits_for() async throws {
        try AccessibilityTree.requireServed()
        let go = FileManager.default.temporaryDirectory.appending(path: "sheet-go-\(UUID().uuidString)")
        addTeardownBlock { try? FileManager.default.removeItem(at: go) }
        let (store, _) = try stores(try fakeCLI(run: F.end(ok: true, updated: ["example"]),
                                               runFirst: "while [ ! -e '\(go.path)' ]; do /bin/sleep 0.05; done"))
        store.start(apps: ["example"], withAffected: false, plan: nil)

        let names = try sheetNodes(store).filter { $0.role == kAXButtonRole }.map(\.name)

        XCTAssertTrue(names.contains("Stop"), "Stop while it runs")
        XCTAssertFalse(names.contains("Done"), "no Done while it runs")
        var stopping = AppUpdatesStore.Progress(rows: [])
        stopping.stopRequested = true
        stopping.stopping = UpdateStoppingRecord(app: "example", step: "install")
        XCTAssertEqual(AppUpdatesSheet.stopLine(stopping), "Finishing the install, then stopping…")
        XCTAssertNil(AppUpdatesSheet.stopLine(AppUpdatesStore.Progress(rows: [])))
        stopping.isRunning = false
        XCTAssertNil(AppUpdatesSheet.stopLine(stopping), "not once the run has ended (review part 2, #4)")
        FileManager.default.createFile(atPath: go.path, contents: nil)
        await eventually { store.lastFinished != nil }
    }

    /// VoiceOver hears what changes on the sheet (review part 2, #8): the
    /// confirmation's plan read, each device and app finishing, a Stop
    /// waiting for an install, Force Stop offered, and the end.
    func test_the_sheet_says_what_changes_to_voiceover() {
        let loading = AppUpdatesStore.Confirmation(apps: ["example"])
        var ready = loading
        ready.plan = F.record([F.app("example", to: "2.0", readiness: "checked")])
        XCTAssertEqual(AppUpdatesSheet.announcement(from: loading, to: ready), "Ready to update 1 app")
        XCTAssertNil(AppUpdatesSheet.announcement(from: ready, to: ready), "said once")
        var failed = AppUpdatesStore.Confirmation(apps: ["example"])
        failed.failure = "graft gave no plan."
        XCTAssertEqual(AppUpdatesSheet.announcement(from: loading, to: failed), "graft gave no plan.")

        let plan = F.record([F.app("example", displayName: "Example+", targets: [
            F.target("Example iPhone"), F.target("Example iPad", udid: F.pad, waiting: F.notConnected("Example iPad")),
        ])])
        var running = AppUpdatesStore.Progress(rows: [])
        running.adopt(plan)
        XCTAssertEqual(AppUpdatesSheet.announcements(from: nil, to: running), [], "a device waiting from the start isn't news")

        var installed = running
        installed.installStep(UpdateStepRecord(app: "example", step: "install", status: "end", detail: nil,
                                               udid: F.phone, deviceName: "Example iPhone"))
        XCTAssertEqual(AppUpdatesSheet.announcements(from: running, to: installed), ["Example+ on Example iPhone: Done"])

        var stopping = installed
        stopping.stopping = UpdateStoppingRecord(app: "example", step: "install")
        stopping.offersForceStop = true
        XCTAssertEqual(AppUpdatesSheet.announcements(from: installed, to: stopping),
                       ["Finishing the install, then stopping…", "Force Stop is available"])

        var ended = stopping
        ended.finish(UpdateAppEndRecord(app: "example", ok: true, stopped: false, done: ["build"], left: [], error: nil))
        ended.isRunning = false
        XCTAssertEqual(AppUpdatesSheet.announcements(from: stopping, to: ended), ["Example+: Done", "Update Finished"])
    }

    /// Update All across many apps and devices (review part 2, #6): the
    /// rows scroll, so the sheet stays short enough to keep Stop and Done
    /// on a laptop's screen.
    func test_the_progress_sheet_scrolls_its_rows_and_stays_on_screen() async throws {
        let go = FileManager.default.temporaryDirectory.appending(path: "sheet-tall-\(UUID().uuidString)")
        addTeardownBlock { try? FileManager.default.removeItem(at: go) }
        let (store, _) = try stores(try fakeCLI(run: F.end(ok: true),
                                               runFirst: "while [ ! -e '\(go.path)' ]; do /bin/sleep 0.05; done"))
        let names = (1...12).map { "app\($0)" }
        let plan = F.record(names.map { name in
            F.app(name, to: "2.0", targets: [F.target("Example iPhone"), F.target("Example iPad", udid: F.pad)])
        })
        XCTAssertTrue(store.start(apps: names, withAffected: false, plan: plan))

        let height = NSHostingView(rootView: AppUpdatesSheet(store: store, names: [:])).fittingSize.height

        XCTAssertLessThanOrEqual(height, AppUpdatesSheet.rowsMaxHeight + 360,
                                 "twelve apps on two devices each, and the sheet still fits")
        FileManager.default.createFile(atPath: go.path, contents: nil)
        await eventually { store.lastFinished != nil }
    }

    /// Stop, then Stop Now while an install the first Stop waits for is
    /// finishing (a second Stop cancels it, spec §11.2).
    func test_the_stop_button_becomes_stop_now_while_an_install_finishes() {
        var progress = AppUpdatesStore.Progress(rows: [])
        XCTAssertEqual(AppUpdatesSheet.stopTitle(progress), "Stop")
        progress.stopRequested = true
        XCTAssertNil(AppUpdatesSheet.stopTitle(progress), "stopping: nothing more to press")
        progress.stopping = UpdateStoppingRecord(app: "example", step: "lc-push")
        XCTAssertEqual(AppUpdatesSheet.stopTitle(progress), "Stop Now")
        progress.isRunning = false
        XCTAssertNil(AppUpdatesSheet.stopTitle(progress))
    }

    // MARK: Settings

    /// Composition (review S10): Settings › Updates shows the third switch,
    /// on by default, with what it sends; turning it off is the store's
    /// switch; Check for updates now runs the quick plan with it off.
    func test_settings_shows_the_switch_and_check_for_updates_now() async throws {
        try AccessibilityTree.requireServed()
        let cli = try fakeCLI(quick: F.planLine([F.app("example")]))
        let (appUpdates, _) = try stores(cli)
        let name = "AppUpdateViewsTests.self.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: name))
        addTeardownBlock { defaults.removePersistentDomain(forName: name) }
        let log = FileManager.default.temporaryDirectory.appending(path: "update-\(UUID().uuidString).log")
        let updates = UpdateStore(runner: cli.runner, defaults: defaults, now: Date.init, appVersion: "2.3",
                                  open: { _ in }, updateLog: log)
        let h = hosted(UpdatesSection(updates: updates, appUpdates: appUpdates), width: 520)
        settle(h)

        var nodes = try AccessibilityTree.nodes(in: h.window)
        let toggle = try XCTUnwrap(nodes.first { $0.role == kAXCheckBoxRole && $0.name == AppUpdatesToggle.title })
        XCTAssertEqual((toggle.value as? NSNumber)?.boolValue, true, "on by default")
        XCTAssertTrue(nodes.contains { ($0.value as? String) == AppUpdatesToggle.note })
        XCTAssertTrue(AppUpdatesToggle.note.contains("bundle IDs") && AppUpdatesToggle.note.contains("canister.me"))

        appUpdates.checkApps = false
        settle(h)
        nodes = try AccessibilityTree.nodes(in: h.window)
        XCTAssertEqual((nodes.first { $0.role == kAXCheckBoxRole && $0.name == AppUpdatesToggle.title }?.value
                        as? NSNumber)?.boolValue, false)
        try clickButton(named: AppUpdateText.checkNow, in: h.window)

        await eventually { cli.argv().contains("--json --update --all --plan --quick") }
        await eventually { appUpdates.plans["example"] != nil }
    }

    // MARK: The Tweaks pane

    func test_a_tweak_an_app_in_the_update_uses_waits_for_it() throws {
        let json = #"{"type":"tweak","filename":"a-tweak.deb","size_bytes":1,"used_by":["example","bravo"]}"#
        guard case let .tweak(tweak) = try BuildEvent.decode(line: json) else { return XCTFail("not a tweak") }

        XCTAssertTrue(TweaksView.inRunningUpdate(tweak, updating: ["bravo"]))
        XCTAssertFalse(TweaksView.inRunningUpdate(tweak, updating: ["delta"]))
        XCTAssertFalse(TweaksView.inRunningUpdate(tweak, updating: nil))
    }

    /// Composition (review S10): on the Tweaks pane, a tweak's Update waits
    /// while an update of an app that uses it runs, and only then.
    func test_the_tweaks_pane_holds_a_tweaks_update_during_an_update_of_its_app() async throws {
        try AccessibilityTree.requireServed()
        let line = #"{"type":"tweak","filename":"a.deb","size_bytes":19154,"used_by":["example"],"name":"Alpha Tweak","version":"1.0"}"#
        let library = TweaksStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")),
                                  cliArgs: ["-c", "printf '%s\\n' '\(line)'"])
        _ = try await library.loadTweaks()
        let update = #"{"type":"tweak-update","filename":"a.deb","package_id":"com.example.alpha","installed":"1.0","latest":"1.1","source":"canister","update_available":true}"#
        let end = #"{"type":"tweak-updates-end","checked":1,"outdated":1,"unchecked":0}"#
        await library.checkForUpdates(argsOverride: ["-c", "printf '%s\\n' '\(update)' '\(end)'"])
        let config = ConfigStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/true")), notify: { _ in })
        let pane = hosted(TweaksView(store: library, config: config), size: NSSize(width: 760, height: 560))
        settle(pane)
        func updateButton() throws -> AccessibilityTree.Node? {
            try AccessibilityTree.nodes(in: pane.window).first { $0.role == kAXButtonRole && $0.name == "Update Alpha Tweak" }
        }
        XCTAssertEqual(try updateButton()?.enabled, true, "premise")

        XCTAssertTrue(config.beginUpdate(apps: ["bravo"]))
        settle(pane)
        XCTAssertEqual(try updateButton()?.enabled, true, "an update of an app that doesn't use it")
        config.endUpdate()

        XCTAssertTrue(config.beginUpdate(apps: ["example"]))
        settle(pane)
        XCTAssertEqual(try updateButton()?.enabled, false, "example uses it")
        config.endUpdate()
    }

    // MARK: The window

    /// Composition (review S10): the window runs the daily check once setup
    /// is done and the list read; a check that came due while the slot was
    /// held runs when it frees; and the store's sheet goes over the window.
    func test_the_window_runs_the_daily_check_once_the_slot_frees_and_shows_the_sheet() async throws {
        let cli = try fakeCLI(quick: F.planLine([F.app("example")]),
                              full: F.planLine([F.app("example", readiness: "checked")], quick: false),
                              list: F.appLine("example"))
        let (appUpdates, config) = try stores(cli)
        XCTAssertTrue(config.beginUpdate(apps: ["bravo"]), "the slot is held: the daily check waits")
        let h = hosted(ContentView(store: config, adminShell: NoAdminShell(), appUpdates: appUpdates),
                       size: NSSize(width: 1100, height: 700))
        await eventually({ config.hasLoadedApps }, timeout: 10, "setup checked, the list read")
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertFalse(cli.argv().contains("--json --update --all --plan --quick"), "it waits for the slot")

        config.endUpdate()

        await eventually({ cli.argv().contains("--json --update --all --plan --quick") }, timeout: 10,
                         "it runs when the slot frees")
        appUpdates.openConfirmation(apps: ["example"])
        await eventually({ h.window.attachedSheet != nil }, timeout: 5, "the sheet over the window")
        appUpdates.cancelConfirmation()
        await eventually({ h.window.attachedSheet == nil }, timeout: 5)
    }

    /// A stand-in `graft` whose `--list` prints what `list` holds when it
    /// runs, so a test can change an app's config between two reads, as
    /// Edit… or `graft --set-app-options` in Terminal does; the quick plan
    /// says example has an update.
    private func listFromFile(_ lines: [String]) throws -> (ScriptCLI, URL) {
        let dir = FileManager.default.temporaryDirectory.appending(path: "list_\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let list = dir.appending(path: "list.jsonl")
        try lines.joined(separator: "\n").write(to: list, atomically: true, encoding: .utf8)
        let cli = try ScriptCLI("""
        case "$*" in
        *--quick*)
        cat <<'EOF'
        \(F.planLine([F.app("example", to: "2.0")]))
        EOF
        ;;
        *--list*)
        cat '\(list.path)'
        ;;
        esac
        """, test: self)
        return (cli, list)
    }

    /// Composition (hands-on of 2.3): releasing a hold in Edit… left
    /// "Update available" up while the CLI had nothing to do. The window
    /// now reads an app's plan again when the list, read again, changes
    /// that app's config. Not on the first read, which the daily check
    /// covers, nor on a read that changes nothing.
    func test_the_window_reads_an_apps_plan_again_when_its_config_changes() async throws {
        let held = F.appLine("example", with: ["preferred_version": "1.2.0"])
        let (cli, list) = try listFromFile([F.appLine("bravo"), held])
        func plans() -> [String] { cli.argv().filter { $0.contains("--plan") } }
        let (appUpdates, config) = try stores(cli)
        let h = hosted(ContentView(store: config, adminShell: NoAdminShell(), appUpdates: appUpdates),
                       size: NSSize(width: 1100, height: 700))
        await eventually({ !plans().isEmpty }, timeout: 10, "the daily check")
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(plans(), ["--json --update --all --plan --quick"], "the first read is the daily check's alone")

        try await config.reload()
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(plans().count, 1, "a read that changes nothing reads no plan")

        try [F.appLine("bravo"), F.appLine("example")].joined(separator: "\n")
            .write(to: list, atomically: true, encoding: .utf8)
        try await config.reload()

        await eventually({ plans().count == 2 }, timeout: 10, "the hold let go")
        XCTAssertEqual(plans().last, "--json --update example --plan --quick")
        _ = h
    }

    /// With "Check my apps for updates" off, a change to an app's config
    /// sends no plan either (spec §11.4).
    func test_with_the_switch_off_the_window_reads_no_plan_when_an_apps_config_changes() async throws {
        let (cli, list) = try listFromFile([F.appLine("example", with: ["preferred_version": "1.2.0"])])
        let (appUpdates, config) = try stores(cli, checkApps: false)
        let h = hosted(ContentView(store: config, adminShell: NoAdminShell(), appUpdates: appUpdates),
                       size: NSSize(width: 1100, height: 700))
        await eventually({ config.hasLoadedApps }, timeout: 10, "setup checked, the list read")

        try F.appLine("example").write(to: list, atomically: true, encoding: .utf8)
        try await config.reload()
        XCTAssertEqual(config.apps.first?.preferredVersion, "latest", "premise: the change was read")
        try await Task.sleep(for: .milliseconds(500))

        XCTAssertFalse(cli.argv().contains { $0.contains("--plan") }, "no plan sent")
        _ = h
    }
}
