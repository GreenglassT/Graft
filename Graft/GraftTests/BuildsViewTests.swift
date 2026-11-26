import SwiftUI
import XCTest
@testable import Graft

/// Which action a build row offers.
///
/// The polarity is the feature. A Mac build has a tweak dylib retargeted at a
/// bundled stub `Preferences.framework`; iOS has the real one, so offering
/// `Re-install…` on one pushes a stub-linked IPA to a phone and breaks the
/// tweak silently. Both directions are asserted for that reason — a case that
/// only feeds a Mac row passes against code that offers `Install to Mac…`
/// always, which would withhold the phone action from every ordinary build.
@MainActor
final class BuildsViewTests: XCTestCase {
    func test_a_mac_build_offers_the_mac_action() throws {
        XCTAssertEqual(BuildsView.action(for: try record(target: "mac")), .installToMac)
    }

    /// Install to Mac…'s tooltip says what to expect of any app under
    /// PlayCover (spec 5d item 4); Re-install…'s doesn't.
    func test_install_to_mac_says_what_to_expect() throws {
        XCTAssertEqual(BuildsView.installHelp(for: try record(target: "mac")), PlayCover.expectations)
        XCTAssertNil(BuildsView.installHelp(for: try record(target: "device")))
    }

    func test_a_device_build_offers_the_phone_action() throws {
        XCTAssertEqual(BuildsView.action(for: try record(target: "device")), .reinstall)
    }

    func test_a_failed_build_offers_neither() throws {
        XCTAssertEqual(
            BuildsView.action(for: try record(target: "mac", success: false)), .none,
            "a build that failed produced no artifact to install"
        )
    }

    func test_a_build_that_kept_no_archive_offers_neither() throws {
        XCTAssertEqual(
            BuildsView.action(for: try record(target: "device", ipaPath: nil)), .none,
            "the row survives retention; the file it names may not"
        )
    }

    /// A row whose target the CLI could not state reads as a phone build.
    ///
    /// Not a hypothetical: every row in a build log written before this column
    /// existed is one, and `_target_of` normalizes those to `device` on the
    /// Python side. Offering the phone action there is correct — that is what
    /// they are — and the failure this pins is the opposite one, where an
    /// unrecognized value fell to `mac` and withheld the action that worked.
    func test_an_unrecognised_target_offers_the_phone_action() throws {
        XCTAssertEqual(BuildsView.action(for: try record(target: "wat")), .reinstall)
    }

    /// The tag is gated the same way the action is.
    ///
    /// Pinned on `isForThisMac` rather than by rendering, because a `Table`
    /// cell's contents are invisible to an NSView walk in this app
    /// (`ClickHarness.backingTableView`) — so this is the honest level to
    /// assert at, and the view reads the same property one line away.
    func test_only_a_mac_build_is_tagged() throws {
        XCTAssertTrue(try record(target: "mac").isForThisMac)
        XCTAssertFalse(try record(target: "device").isForThisMac)
        XCTAssertFalse(try record(target: "wat").isForThisMac,
                       "an unrecognized target is not a Mac build")
    }

    // MARK: - F15: the menus

    /// The context menu read `success && ipaPath != nil` for itself and so
    /// offered Re-install… on a Mac build. Both menus read `installItem` now,
    /// which reads `action(for:)`.
    func test_a_mac_build_is_never_offered_a_reinstall() throws {
        let item = BuildsView.installItem(for: try record(target: "mac"), refusal: nil)
        XCTAssertEqual(item, .init(title: "Install to Mac…", enabled: true))
        let failed = BuildsView.installItem(for: try record(target: "mac", success: false), refusal: nil)
        XCTAssertFalse(failed.title.hasPrefix("Re-install"), failed.title)
    }

    /// A disabled item says why in its title: a menu draws no tooltip, so the
    /// `.help` reason never reached anyone.
    func test_a_refused_reinstall_says_why_in_its_title() throws {
        let item = BuildsView.installItem(
            for: try record(target: "device"), refusal: PaidOnly.sideloadApp
        )
        XCTAssertEqual(item, .init(title: "Re-install… (needs a paid account)", enabled: false))
        XCTAssertEqual(
            BuildsView.installItem(for: try record(target: "device"), refusal: nil),
            .init(title: "Re-install…", enabled: true)
        )
    }

    func test_an_item_with_nothing_to_act_on_says_why() throws {
        XCTAssertEqual(
            BuildsView.installItem(for: try record(target: "device", success: false), refusal: nil),
            .init(title: "Re-install… (build failed)", enabled: false)
        )
        XCTAssertEqual(
            BuildsView.installItem(for: try record(target: "device", ipaPath: nil), refusal: nil),
            .init(title: "Re-install… (archive not kept)", enabled: false)
        )
        XCTAssertEqual(
            BuildsView.revealItem(for: try record(target: "device", ipaPath: nil)),
            .init(title: "Reveal in Finder (no archive)", enabled: false)
        )
        XCTAssertEqual(
            BuildsView.logItem(for: try record(target: "device")),
            .init(title: "View log… (not saved)", enabled: false),
            "the fixture's log_path is null: a build run from Terminal"
        )
    }

    // MARK: -

    /// A `BuildRecord` as `--builds --json` actually emits one.
    ///
    /// Decoded from JSON rather than constructed, because `BuildRecord` has an
    /// explicit `CodingKeys` enum and no memberwise init — and because a
    /// fixture that skipped the decode would not notice a key the CLI stopped
    /// sending. Note this does *not* set `convertFromSnakeCase`: nothing in
    /// this app does, and setting it would stop those explicit raw values
    /// matching.
    private func record(
        target: String, success: Bool = true, ipaPath: String? = "/cache/built/y.ipa"
    ) throws -> BuildRecord {
        let path = ipaPath.map { "\"\($0)\"" } ?? "null"
        let json = """
        {"type":"build-record","id":1,"timestamp":"2026-08-18T12:00:00Z",\
        "app":"example","bundle_id":"com.example.app","version":"21.16.2",\
        "tweaks":[],"ipa_path":\(path),"ipa_sha256":null,"log_path":null,\
        "signing_team":null,"udid":null,"target":"\(target)",\
        "success":\(success),"error":null}
        """
        return try XCTUnwrap(
            json.buildEvents().compactMap { event -> BuildRecord? in
                if case let .buildRecord(record) = event { return record }
                return nil
            }.first,
            "the fixture did not decode — a key the CLI sends is missing from it"
        )
    }
}

/// A title or sentence that names an app uses the name the sidebar shows:
/// its display name, and its short name (config.yaml's key, which commands
/// take) only when it has none.
@MainActor
final class AppDisplayNameTests: XCTestCase {
    private func app(_ name: String, displayName: String?) throws -> AppRecord {
        let shown = displayName.map { "\"\($0)\"" } ?? "null"
        let line = #"{"type":"app","name":"\#(name)","bundle_id":"com.example.app","preferred_version":"latest","tweaks":[],"display_name":\#(shown),"delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}"#
        guard case let .app(record)? = line.buildEvents().first else {
            XCTFail("fixture did not decode: \(line)")
            throw FixtureError()
        }
        return record
    }

    private func build(app: String) throws -> BuildRecord {
        let line = #"{"type":"build-record","id":3,"timestamp":"2026-08-18T12:00:00Z","app":"\#(app)","bundle_id":"com.example.app","version":"1.0","tweaks":[],"ipa_path":"/cache/built/y.ipa","ipa_sha256":null,"log_path":null,"signing_team":null,"udid":null,"target":"device","success":true,"error":null}"#
        guard case let .buildRecord(record)? = line.buildEvents().first else {
            XCTFail("fixture did not decode: \(line)")
            throw FixtureError()
        }
        return record
    }

    func test_the_shown_name_is_the_display_name_and_else_the_short_name() throws {
        XCTAssertEqual(try app("example_music_app", displayName: "Example").shownName, "Example")
        XCTAssertEqual(try app("example_music_app", displayName: nil).shownName, "example_music_app")
        XCTAssertEqual(try app("example_music_app", displayName: " ").shownName, "example_music_app",
                       "a blank display name is none")
    }

    func test_edit_and_remove_name_the_app_by_its_display_name() throws {
        let named = try app("example_music_app", displayName: "Example")
        XCTAssertEqual(EditAppSheet.title(for: named), "Edit Example")
        XCTAssertEqual(SidebarView.removeTitle(for: named), "Remove Example…")
        XCTAssertEqual(
            SidebarView.removeMessage(for: named),
            "Example will be removed from your config. Cached IPAs, archived builds, and build history are kept."
        )
        let unnamed = try app("example_music_app", displayName: nil)
        XCTAssertEqual(EditAppSheet.title(for: unnamed), "Edit example_music_app")
        XCTAssertEqual(SidebarView.removeTitle(for: unnamed), "Remove example_music_app…")
    }

    /// The rest of the places a short name showed: the sidebar's Stop
    /// building… and its confirmation's title (the app pane's too, through
    /// the same `name`), Remove's answer once a build has started, a save
    /// that failed after the Edit sheet closed.
    func test_stop_remove_and_save_errors_name_the_app_by_its_display_name() throws {
        let named = try app("example_music_app", displayName: "Example")
        XCTAssertEqual(SidebarView.stopTitle(for: named), "Stop building Example…")
        XCTAssertEqual(named[keyPath: \AppRecord.shownName], "Example", "the Stop confirmations' name")
        XCTAssertEqual(SidebarView.stillBuilding(named),
                       "Example is building right now. Wait for the build to finish, then remove it.")
        XCTAssertEqual(EditAppSheet.saveFailed(named, "No."), "Couldn't save changes to Example: No.")
    }

    /// What the store says about the build it's running: the busy line under
    /// every build control, "Clean up old builds…"'s, and a Stop that came
    /// too late.
    func test_the_store_names_the_running_build_by_its_display_name() async throws {
        let line = #"{"type":"app","name":"example_music_app","bundle_id":"com.example.app","preferred_version":"latest","tweaks":[],"display_name":"Example","delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}"#
        let store = ConfigStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")),
                                cliArgs: ["-c", "printf '%s\\n' '\(line)'"], notify: { _ in },
                                hold: TerminationHold(control: TerminationHoldTests.FakeControl()))
        _ = try await store.loadApps()
        XCTAssertEqual(store.shownName(of: "example_music_app"), "Example")
        XCTAssertEqual(store.shownName(of: "gone"), "gone")

        XCTAssertTrue(store.launchBuild(appName: "example_music_app", delivery: .unknown) {})
        XCTAssertEqual(store.busy?.explanation, "Example is building — wait for it to finish")
        store.cancelBuild(appName: "other")
        XCTAssertEqual(store.lastError, "other isn't building any more. Example is building now, and was left alone.")
        store.endBuild(appName: "example_music_app")
    }

    /// The app pane's title and header: a blank display name reads as none,
    /// as the sidebar's row does, rather than as a blank title.
    func test_the_app_pane_title_uses_the_short_name_for_a_blank_display_name() async throws {
        let line = #"{"type":"app","name":"example_music_app","bundle_id":"com.example.app","preferred_version":"latest","tweaks":[],"display_name":" ","delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}"#
        let store = ConfigStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")),
                                cliArgs: ["-c", "printf '%s\\n' '\(line)'"], notify: { _ in })
        let layout = laidOutForMeasurement(
            ContentView(store: store, selection: .app("example_music_app"), adminShell: NoAdminShell()),
            width: 1100, height: 760
        )
        await eventually({ !store.apps.isEmpty }, "premise: the app was listed")
        settle(Hosted(window: layout.window, size: NSSize(width: 1100, height: 760)), seconds: 0.4)
        XCTAssertEqual(layout.window.title, "example_music_app")
        withExtendedLifetime(layout) {}
    }

    /// No view spells out `displayName ?? name` itself: that fallback is
    /// `shownName`'s, which also takes a blank display name for none.
    func test_no_view_falls_back_from_a_display_name_by_hand() throws {
        let fallback = try NSRegularExpression(pattern: #"\b(\w+)\.displayName\s*\?\?\s*\1\.name\b"#)
        var found: [String] = []
        for (path, source) in try ObservableStoresTests.appSources() {
            let code = NoRealDefaultsTests.blankCommentsAndStrings(source)
            let lines = code.components(separatedBy: "\n")
            for (index, line) in lines.enumerated()
            where fallback.firstMatch(in: line, range: NSRange(line.startIndex..., in: line)) != nil {
                found.append("\(path):\(index + 1)")
            }
        }
        XCTAssertEqual(found, [], "use AppRecord.shownName")
    }

    /// The Builds pane's App column and Re-install's title: a build carries
    /// the short name, so the name comes from the config's app by that name.
    func test_a_build_is_named_by_its_apps_display_name() throws {
        let apps = [try app("example_music_app", displayName: "Example"), try app("other", displayName: nil)]
        XCTAssertEqual(BuildsView.appName(for: try build(app: "example_music_app"), apps: apps), "Example")
        XCTAssertEqual(BuildsView.appName(for: try build(app: "other"), apps: apps), "other")
        XCTAssertEqual(BuildsView.appName(for: try build(app: "removed_app"), apps: apps), "removed_app",
                       "an app gone from the config has only the short name its build kept")
    }
}

/// A fixture that didn't decode: the test fails (`XCTFail`) rather than
/// skipping, since a key the CLI sends gone missing is a defect.
struct FixtureError: Error {}

/// One `--json` line of each kind these tests feed, decoded as the app
/// decodes the CLI's.
@MainActor
enum BuildFixtures {
    static func buildLine(id: Int = 3, udid: String? = nil, logPath: String? = nil, target: String = "device") -> String {
        let device = udid.map { "\"\($0)\"" } ?? "null"
        let log = logPath.map { "\"\($0)\"" } ?? "null"
        return #"{"type":"build-record","id":\#(id),"timestamp":"2026-08-18T12:00:00Z","app":"example","bundle_id":"com.example.app","version":"2.4.1","tweaks":[],"ipa_path":"/cache/built/y.ipa","ipa_sha256":null,"log_path":\#(log),"signing_team":null,"udid":\#(device),"target":"\#(target)","success":true,"error":null}"#
    }

    static func deviceLine(_ udid: String, _ name: String) -> String {
        #"{"type":"device","udid":"\#(udid)","name":"\#(name)","model":"iPhone14,2","ios_version":"17.0","connection_type":"USB","livecontainer_bundle_id":null,"is_default":false}"#
    }

    static func build(udid: String? = nil) throws -> BuildRecord {
        let line = buildLine(udid: udid)
        guard case let .buildRecord(record)? = line.buildEvents().first else {
            XCTFail("fixture did not decode: \(line)")
            throw FixtureError()
        }
        return record
    }

    static func device(_ udid: String, _ name: String) throws -> DeviceRecord {
        let line = deviceLine(udid, name)
        guard case let .device(record)? = line.buildEvents().first else {
            XCTFail("fixture did not decode: \(line)")
            throw FixtureError()
        }
        return record
    }
}

/// Re-install… pushes to a phone, so it asks first, naming the app, the
/// build and the device the CLI will install on. Its ellipsis promised a
/// dialog, and it installed at once.
@MainActor
final class ReinstallConfirmationTests: XCTestCase {
    private typealias Target = BuildsView.ReinstallTarget

    private func target(
        udid: String? = nil, defaultDevice: String? = nil, defaultKnown: Bool = true,
        devices: [DeviceRecord], unreadable: [String] = []
    ) throws -> Target {
        BuildsView.reinstallTarget(
            for: try BuildFixtures.build(udid: udid), defaultDevice: defaultDevice,
            defaultKnown: defaultKnown, devices: devices, unreadable: unreadable
        )
    }

    /// The CLI's rule (`_do_reinstall_build`): `row["udid"] or
    /// cfg.default_device_udid`, and then that phone must be connected
    /// (`select_device`); only with neither does it take the first connected.
    /// `--devices` marks no default that isn't plugged in, so a default
    /// can't be read off the phones that are.
    func test_the_target_is_the_phone_the_cli_requires() throws {
        let phone = try BuildFixtures.device("UDID-1", "Example iPhone")
        let pad = try BuildFixtures.device("UDID-2", "Example iPad")

        // The build's own phone, over the default.
        XCTAssertEqual(try target(udid: "UDID-1", defaultDevice: "UDID-2", devices: [phone, pad]),
                       .phone("Example iPhone"))
        XCTAssertEqual(try target(udid: "UDID-9", defaultDevice: "UDID-1", devices: [phone]),
                       .notConnected("the device this build went to"),
                       "the build's phone is required, not the default nor the one connected")
        XCTAssertEqual(try target(udid: "UDID-9", devices: [phone], unreadable: ["UDID-9"]),
                       .unnamed("the device this build went to"), "connected, but locked")

        // No phone of its own: the default.
        XCTAssertEqual(try target(defaultDevice: "UDID-2", devices: [phone, pad]), .phone("Example iPad"))
        XCTAssertEqual(try target(defaultDevice: "UDID-DEF", devices: [phone]),
                       .notConnected("your default device"),
                       "the default is unplugged and another phone is connected: not that phone")
        XCTAssertEqual(try target(defaultDevice: "UDID-DEF", devices: []), .notConnected("your default device"))

        // Neither: the first connected.
        XCTAssertEqual(try target(devices: [phone]), .phone("Example iPhone"))
        XCTAssertEqual(try target(devices: [phone, pad]), .firstConnected)
        XCTAssertEqual(try target(devices: [phone], unreadable: ["UDID-3"]), .firstConnected)
        XCTAssertEqual(try target(devices: []), .noneConnected)
        XCTAssertEqual(try target(defaultDevice: "", devices: [phone]), .phone("Example iPhone"),
                       "a blank default is none")

        // config.yaml unread: the default is unknown, so no phone is named.
        XCTAssertEqual(try target(defaultKnown: false, devices: [phone]), .defaultOrFirst)
        XCTAssertEqual(try target(defaultKnown: false, devices: []), .noneConnected)
        XCTAssertEqual(try target(udid: "UDID-1", defaultKnown: false, devices: [phone]), .phone("Example iPhone"),
                       "a build's own phone needs no default")
    }

    func test_the_words_name_the_app_the_build_and_the_device() throws {
        let build = try BuildFixtures.build()
        let when = DateHelpers.readable("2026-08-18T12:00:00Z")
        let what = "the Example 2.4.1 build from \(when)"
        func words(_ target: Target) -> BuildsView.ReinstallWords {
            BuildsView.reinstallConfirmation(for: build, appName: "Example", target: target)
        }
        XCTAssertEqual(words(.phone("Example iPhone")), .init(
            title: "Re-install Example?", message: "This installs \(what) on “Example iPhone”.", canConfirm: true))
        XCTAssertEqual(words(.firstConnected).message, "This installs \(what) on the first connected iPhone or iPad.")
        XCTAssertEqual(words(.unnamed("the device this build went to")).message,
                       "This installs \(what) on the device this build went to.")
        // An iPad is a device too: "phone" named only half of them.
        XCTAssertEqual(words(.notConnected("the device this build went to")).title,
                       "The device this build went to isn't connected")
        XCTAssertTrue(words(.defaultOrFirst).canConfirm)
        XCTAssertEqual(words(.notConnected("your default device")), .init(
            title: "Your default device isn't connected",
            message: "Graft re-installs \(what) on your default device. Connect it, then choose Re-install… again.",
            canConfirm: false))
        XCTAssertEqual(words(.noneConnected).title, "No iPhone or iPad is connected")
        XCTAssertFalse(words(.noneConnected).canConfirm)
    }

    // MARK: - The Builds pane itself

    /// The Builds pane with one build, behind a `graft` that logs each run:
    /// its devices, config.yaml's default device, the app, and
    /// `--reinstall-build`.
    private struct Pane {
        let box: Hosted
        let cli: ScriptCLI
        /// What the hold over the quit was asked (`TerminationHold`).
        let control: TerminationHoldTests.FakeControl
        /// There once the stand-in has said it handed the Mac build over.
        let handed: URL

        var reinstalls: [String] { cli.argv().filter { $0.contains("--reinstall-build") } }
    }

    /// `PlayCover`'s handoff of the Mac build, said at once; then the wait,
    /// which only a Stop ends (30 s at most).
    private static let handedLine = #"{"type":"handed-to-playcover","filename":"y.ipa","path":"/cache/built/y.ipa","bundle_id":"com.example.app","display_name":"Example"}"#

    private func pane(defaultDevice: String?, logPath: String? = nil, target: String = "device") async throws -> Pane {
        let settings = #"{"type":"settings","default_device_udid":\#(defaultDevice.map { "\"\($0)\"" } ?? "null"),"archive_keep":2,"signing_cert":null,"signing_profile":null,"signing_team_id":null}"#
        let app = #"{"type":"app","name":"example","bundle_id":"com.example.app","preferred_version":"latest","tweaks":[],"display_name":"Example","delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}"#
        let handed = FileManager.default.temporaryDirectory.appending(path: "builds-handed-\(UUID().uuidString)")
        addTeardownBlock { try? FileManager.default.removeItem(at: handed) }
        let cli = try ScriptCLI("""
        case " $* " in
          *" --list "*) printf '%s\\n' '\(app)' ;;
          *" --settings "*) printf '%s\\n' '\(settings)' ;;
          *" --devices "*) printf '%s\\n' '\(BuildFixtures.deviceLine("UDID-1", "Example iPhone"))' '{"type":"devices-end","count":1,"failed":0}' ;;
          *" --builds "*) printf '%s\\n' '\(BuildFixtures.buildLine(logPath: logPath, target: target))' ;;
          *" --install-to-mac "*)
            printf '%s\\n' '\(Self.handedLine)'
            touch '\(handed.path)'
            i=0; while [ $i -lt 600 ]; do /bin/sleep 0.05; i=$((i+1)); done ;;
        esac
        exit 0
        """, test: self)
        let builds = BuildsStore(runner: cli.runner)
        let config = ConfigStore(runner: cli.runner, notify: { _ in })
        let devices = DevicesStore(runner: cli.runner)
        _ = try await builds.load()
        _ = try await config.loadApps()
        _ = try await devices.load()
        XCTAssertEqual(builds.builds.count, 1, "premise: the build is listed")
        XCTAssertEqual(devices.devices.map(\.name), ["Example iPhone"], "premise: one phone is connected")
        let control = TerminationHoldTests.FakeControl()
        let hold = TerminationHold(control: control)
        let suite = "ReinstallConfirmationTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        addTeardownBlock { defaults.removePersistentDomain(forName: suite) }
        let updates = AppUpdatesStore(runner: cli.runner, config: config, defaults: defaults,
                                      now: { Date() }, hold: hold)
        let view = BuildsView(store: builds, config: config, artwork: ArtworkStore(runner: cli.runner),
                              staged: StagedIPAStore(runner: cli.runner, hold: hold), devices: devices,
                              settings: SettingsStore(runner: cli.runner), appUpdates: updates)
        let box = hosted(view, size: NSSize(width: 900, height: 500))
        addTeardownBlock { takeDown(box) }
        settle(box, seconds: 0.3)
        return Pane(box: box, cli: cli, control: control, handed: handed)
    }

    private func views<T: NSView>(_ type: T.Type, in view: NSView?) -> [T] {
        guard let view else { return [] }
        return ((view as? T).map { [$0] } ?? []) + view.subviews.flatMap { views(type, in: $0) }
    }

    /// Choose `title` in the build row's own menu, as a click does.
    ///
    /// SwiftUI fills a `Menu`'s `NSMenu` only as it opens, from its popup
    /// cell's `popUpButtonCell:willShowMenu:`; a test can't open it (menu
    /// tracking never sees a synthetic mouse-up), so it asks for the same
    /// fill, then performs the item, which runs the row's own Button.
    private func chooseReinstall(in pane: Pane) throws {
        try choose("Re-install…", in: pane)
    }

    private func choose(_ title: String, in pane: Pane) throws {
        let popup = try XCTUnwrap(views(NSPopUpButton.self, in: pane.box.window.contentView).first,
                                  "no row menu in the Builds table")
        let menu = try XCTUnwrap(popup.menu)
        let fill = NSSelectorFromString("popUpButtonCell:willShowMenu:")
        let coordinator = try XCTUnwrap(menu.delegate as AnyObject?, "the row menu has no SwiftUI coordinator")
        guard coordinator.responds(to: fill) else {
            return XCTFail("SwiftUI no longer fills a Menu through \(fill): this test needs another way in")
        }
        _ = coordinator.perform(fill, with: popup.cell, with: menu)
        let index = try XCTUnwrap(menu.items.firstIndex { $0.title == title },
                                  "no \(title) in \(menu.items.map(\.title))")
        menu.performActionForItem(at: index)
    }

    private func alert(of pane: Pane) async throws -> NSWindow {
        await eventually({ pane.box.window.attachedSheet != nil }, "the confirmation never came up")
        return try XCTUnwrap(pane.box.window.attachedSheet)
    }

    private func button(_ title: String, in sheet: NSWindow) -> NSButton? {
        views(NSButton.self, in: sheet.contentView).first { $0.title == title }
    }

    private func texts(in sheet: NSWindow) -> [String] {
        views(NSTextField.self, in: sheet.contentView).map(\.stringValue)
    }

    /// The row's Re-install… asks first, naming the phone, and installs
    /// nothing until Re-install is pressed; then the re-install sheet runs
    /// `--reinstall-build` as before. Return is Re-install and Escape is
    /// Cancel, as in any macOS confirmation.
    func test_re_install_asks_first_then_runs_the_re_install() async throws {
        let pane = try await pane(defaultDevice: nil)
        try chooseReinstall(in: pane)
        let sheet = try await alert(of: pane)
        let when = DateHelpers.readable("2026-08-18T12:00:00Z")
        XCTAssertTrue(texts(in: sheet).contains("Re-install Example?"), "\(texts(in: sheet))")
        XCTAssertTrue(texts(in: sheet).contains("This installs the Example 2.4.1 build from \(when) on “Example iPhone”."),
                      "\(texts(in: sheet))")
        let cancel = try XCTUnwrap(button("Cancel", in: sheet))
        let reinstall = try XCTUnwrap(button(BuildsView.reinstallButton, in: sheet))
        XCTAssertEqual(cancel.keyEquivalent, "\u{1b}", "Escape cancels")
        XCTAssertEqual(reinstall.keyEquivalent, "\r", "Return confirms")
        XCTAssertEqual(pane.reinstalls, [], "nothing runs before it's confirmed")

        reinstall.performClick(nil)
        await eventually({ pane.reinstalls == ["--json --reinstall-build 3"] },
                         "the confirmed re-install never ran: \(pane.cli.argv())")
    }

    /// A re-install may be the install an update was offering, or an older
    /// build the update then offers to replace: the app's update plan is read
    /// again once it ends, so its Update badge follows (review 23-2, item 7).
    func test_a_re_install_reads_the_apps_update_plan_again() async throws {
        let pane = try await pane(defaultDevice: nil)
        let quick = AppUpdatesStore.quickArguments(apps: ["example"]).joined(separator: " ")
        try chooseReinstall(in: pane)
        let sheet = try await alert(of: pane)
        try XCTUnwrap(button(BuildsView.reinstallButton, in: sheet)).performClick(nil)
        await eventually({ pane.reinstalls == ["--json --reinstall-build 3"] }, "the re-install never ran")
        await eventually({ pane.cli.argv().contains(quick) }, "the app's update plan wasn't read again")
    }

    /// Install to Mac… in Builds: after the handoff the CLI waits up to two
    /// minutes for PlayCover's install. The wait holds the quit, and the
    /// strip's Stop ends it, never the handoff (review 23-2, §11).
    func test_install_to_mac_holds_the_quit_and_its_stop_ends_the_wait() async throws {
        try AccessibilityTree.requireServed()
        let pane = try await pane(defaultDevice: nil, target: "mac")
        try choose("Install to Mac…", in: pane)
        await eventually({ pane.cli.argv().contains { $0.contains(CLIContract.Flag.installToMac) } },
                         "Install to Mac never ran")
        await eventually({ pane.control.isHeld(TerminationHold.waitingForPlayCover) },
                         "nothing held the quit while Builds waited for PlayCover")
        await eventually({ FileManager.default.fileExists(atPath: pane.handed.path) },
                         "premise: the stand-in handed it over")

        try await CachePaneTests.press("Stop", whenShownIn: pane.box)
        await eventually({ pane.control.isLetGo(TerminationHold.waitingForPlayCover) && pane.control.isBalanced },
                         "the Stop didn't end the wait")
        let line = MacInstall(handed: nil, ending: .stopped).endingLines(app: "Example", retry: .buildsRow)[0].text
        try await CachePaneTests.waitForText(line, in: pane.box)
    }

    /// Escape and Command-period close the confirmation, as Cancel, and
    /// nothing is installed.
    ///
    /// Escape goes in through `NSApp.sendEvent`, as a key press does: the
    /// alert's button whose key equivalent is Escape takes it. Command-period
    /// can't: AppKit routes it only for the key window of the active app, and
    /// a test host is never the active app (it would take the screen from
    /// whoever is using it). So it takes the route AppKit documents for it
    /// (`NSResponder.cancelOperation(_:)`): the window asks its views for
    /// the key equivalent, and with none taking it, sends `cancelOperation:`
    /// to its first responder, which the alert answers with its
    /// Escape button. Re-install, which has Return, mustn't take it.
    func test_escape_and_command_period_cancel_the_confirmation() async throws {
        let pane = try await pane(defaultDevice: nil)
        func key(_ characters: String, _ flags: NSEvent.ModifierFlags, _ code: UInt16, in sheet: NSWindow) throws -> NSEvent {
            try XCTUnwrap(NSEvent.keyEvent(
                with: .keyDown, location: .zero, modifierFlags: flags, timestamp: 0,
                windowNumber: sheet.windowNumber, context: nil, characters: characters,
                charactersIgnoringModifiers: characters, isARepeat: false, keyCode: code
            ))
        }

        try chooseReinstall(in: pane)
        var sheet = try await alert(of: pane)
        NSApp.sendEvent(try key("\u{1b}", [], 53, in: sheet))
        await eventually({ pane.box.window.attachedSheet == nil }, "Escape didn't close the confirmation")
        settle(pane.box, seconds: 0.3)
        XCTAssertEqual(pane.reinstalls, [], "Escape installed it")

        try chooseReinstall(in: pane)
        sheet = try await alert(of: pane)
        XCTAssertFalse(sheet.performKeyEquivalent(with: try key(".", .command, 47, in: sheet)),
                       "a button took Command-period as its own key equivalent")
        let responder = try XCTUnwrap(sheet.firstResponder)
        XCTAssertTrue(responder.tryToPerform(#selector(NSResponder.cancelOperation(_:)), with: nil),
                      "nothing in the alert answers cancelOperation:")
        await eventually({ pane.box.window.attachedSheet == nil }, "Command-period didn't close the confirmation")
        settle(pane.box, seconds: 0.3)
        XCTAssertEqual(pane.reinstalls, [], "Command-period installed it")
    }

    /// The row's View log… opens the build's log by its app's display name,
    /// as the row does, not its config key.
    func test_view_log_names_the_app_by_its_display_name() async throws {
        try AccessibilityTree.requireServed()
        let pane = try await pane(defaultDevice: nil, logPath: "/logs/build-3.jsonl")
        try choose("View log…", in: pane)
        await eventually({ pane.box.window.attachedSheet != nil }, "the log never opened")
        settle(pane.box, seconds: 0.3)
        // Read through the pane's window: the Accessibility API lists a
        // sheet as its window's child, not as a window of its own.
        let texts = try AccessibilityTree.texts(in: pane.box.window)
        XCTAssertTrue(texts.contains(BuildLogView.title("Example")), "\(texts)")
        XCTAssertFalse(texts.contains(BuildLogView.title("example")), "\(texts)")
    }

    /// A build with no phone of its own goes to config.yaml's default device.
    /// With that one unplugged and another connected, the CLI refuses
    /// rather than use the other, so the confirmation says it isn't
    /// connected and offers no Re-install.
    func test_an_unplugged_default_is_named_and_nothing_is_offered() async throws {
        let pane = try await pane(defaultDevice: "UDID-DEF")
        try chooseReinstall(in: pane)
        let sheet = try await alert(of: pane)
        XCTAssertTrue(texts(in: sheet).contains("Your default device isn't connected"), "\(texts(in: sheet))")
        XCTAssertFalse(texts(in: sheet).joined().contains("Example iPhone"), "\(texts(in: sheet))")
        XCTAssertNil(button(BuildsView.reinstallButton, in: sheet), "nothing to confirm")
        let ok = try XCTUnwrap(button("OK", in: sheet))
        ok.performClick(nil)
        await eventually({ pane.box.window.attachedSheet == nil }, "OK didn't close it")
        settle(pane.box, seconds: 0.3)
        XCTAssertEqual(pane.reinstalls, [])
    }
}
