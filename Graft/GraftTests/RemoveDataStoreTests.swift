import AppKit
import XCTest
@testable import Graft

/// Help › Remove Graft's Data…: the listing, what is checked to start with,
/// the refusal while Graft works, and what a removal reports.
///
/// The CLI is a script (`ScriptCLI`) that logs its argv and prints canned
/// events in the shape `graft_ipa/remove_data.py` writes them; what it
/// finds and removes is tested on the Python side
/// (tests/test_remove_data.py). The hold is a private one over
/// `TerminationHoldTests.FakeControl`, and removing the app's settings and
/// quitting are closures that count. Nothing here touches a real file,
/// keychain or preference.
@MainActor
final class RemoveDataStoreTests: XCTestCase {
    private var settingsRemoved = 0
    private var quits = 0
    private let control = TerminationHoldTests.FakeControl()
    private lazy var hold = TerminationHold(control: control)

    // MARK: Fixtures

    private static func item(
        _ id: String, group: String = "files", groupTitle: String = "Graft's folders",
        title: String? = nil, size: Int? = 100, present: Bool? = true, isDefault: Bool = true,
        removable: Bool = true, keychain: [[String: String]] = [], note: String? = nil
    ) -> String {
        let payload: [String: Any] = [
            "type": "data-item", "id": id, "group": group, "group_title": groupTitle,
            "title": title ?? "Title of \(id)", "detail": "What \(id) is.",
            "note": note as Any? ?? NSNull(), "how": NSNull(),
            "paths": keychain.isEmpty ? ["/Users/example/\(id)"] : [],
            "keychain": keychain, "size_bytes": size as Any? ?? NSNull(),
            "present": present as Any? ?? NSNull(), "default": isDefault, "removable": removable,
        ]
        let data = try! JSONSerialization.data(withJSONObject: payload)
        return String(data: data, encoding: .utf8)!
    }

    private static let end = #"{"type":"data-items-end","count":7,"total_bytes":300,"keychain":"checked"}"#

    private static let listing = [
        item("config"),
        item("cache", size: 5_000_000),
        item("state", size: 0, present: false),
        item("appstore-signin", group: "app-store", groupTitle: "App Store", size: nil,
             keychain: [["service": "ipatool-auth.service", "account": "account"]],
             note: "If you use ipatool on its own, it signs in with these same items."),
        item("passwords", group: "passwords", groupTitle: "Passwords", size: nil, isDefault: false,
             keychain: [["service": "graft", "account": "p12-ABCDE12345"]]),
        item("app-settings", group: "app", groupTitle: "Graft.app", size: 2000, removable: false),
        item("kept-cache_dir", group: "kept", groupTitle: "Not removed", isDefault: false, removable: false),
        end,
    ].joined(separator: "\n")

    private static func removed(_ id: String, ok: Bool = true, freed: Int = 100) -> String {
        let failed = ok ? "[]" : #"[{"what":"/Users/example/\#(id)","reason":"Operation not permitted"}]"#
        return #"{"type":"data-removed","id":"\#(id)","ok":\#(ok),"dry_run":false,"removed":[],"failed":\#(failed),"freed_bytes":\#(freed)}"#
    }

    /// A graft that lists on `--remove-data` alone and prints `removal` when
    /// `--items` is given, exiting `status` then.
    private func fakeCLI(removal: String = "", status: Int = 0) throws -> ScriptCLI {
        try ScriptCLI("""
        case "$*" in
        *--items*)
        cat <<'EOF'
        \(removal)
        EOF
        exit \(status) ;;
        *)
        cat <<'EOF'
        \(Self.listing)
        EOF
        ;;
        esac
        """, test: self)
    }

    private func store(_ cli: ScriptCLI) -> RemoveDataStore {
        RemoveDataStore(
            runner: cli.runner, hold: hold,
            removeAppSettings: { [weak self] in self?.settingsRemoved += 1 },
            quit: { [weak self] in self?.quits += 1 }
        )
    }

    // MARK: The listing

    func test_the_listing_decodes_what_the_cli_writes() throws {
        let events = Self.listing.buildEvents()
        XCTAssertEqual(events.count, 8)
        guard case let .dataItem(signIn) = events[3] else { return XCTFail("\(events[3])") }
        XCTAssertEqual(signIn.id, "appstore-signin")
        XCTAssertEqual(signIn.groupTitle, "App Store")
        XCTAssertEqual(signIn.keychain, [DataItemRecord.KeychainEntry(service: "ipatool-auth.service", account: "account")])
        XCTAssertNil(signIn.sizeBytes)
        guard case let .dataItemsEnd(end) = events[7] else { return XCTFail("\(events[7])") }
        XCTAssertEqual(end.keychain, DataItemsEndRecord.keychainChecked)
    }

    func test_what_is_checked_to_start_with() async throws {
        let cli = try fakeCLI()
        let sut = store(cli)

        await sut.load()

        XCTAssertEqual(cli.argv(), ["--json --remove-data"])
        XCTAssertEqual(sut.phase, .listed)
        XCTAssertEqual(sut.chosen, ["config", "cache", "appstore-signin", "app-settings"],
                       "the passwords are off, and what the user chose is never offered")
        XCTAssertEqual(sut.shown.map(\.id).contains("state"), false, "nothing there, not shown")
        XCTAssertEqual(sut.groups.map(\.title),
                       ["Graft's folders", "App Store", "Passwords", "Graft.app", "Not removed"])
        let kept = try XCTUnwrap(sut.items.first { $0.id == "kept-cache_dir" })
        XCTAssertFalse(RemoveDataStore.isChoosable(kept))
        XCTAssertEqual(sut.chosenBytes, 100 + 5_000_000 + 2000)
        XCTAssertTrue(sut.canRemove)
    }

    func test_an_item_the_keychain_couldnt_answer_for_has_no_checkbox() throws {
        let line = Self.item("passwords", size: nil, present: nil,
                             keychain: [["service": "graft", "account": "p12-ABCDE12345"]])
        guard case let .dataItem(record) = try BuildEvent.decode(line: line) else { return XCTFail() }
        XCTAssertFalse(RemoveDataStore.isChoosable(record))
        XCTAssertEqual(RemoveDataSheet.sizeText(record), "Couldn't check")
    }

    func test_a_listing_that_failed_says_why() async throws {
        let cli = try ScriptCLI("echo 'error: something broke' >&2; exit 1", test: self)
        let sut = store(cli)

        await sut.load()

        guard case let .failed(message) = sut.phase else { return XCTFail("\(sut.phase)") }
        XCTAssertTrue(message.contains("something broke"), message)
        XCTAssertFalse(sut.canRemove)
    }

    // MARK: Refusing while Graft works

    func test_nothing_is_removed_while_graft_works() async throws {
        let cli = try fakeCLI(removal: Self.removed("config"))
        let sut = store(cli)
        await sut.load()
        let ticket = hold.begin(TerminationHold.building, stop: nil)
        defer { hold.end(ticket) }

        sut.refreshWork()
        XCTAssertFalse(sut.canRemove)
        XCTAssertEqual(RemoveDataStore.refusal(sut.working),
                       "Graft is building and installing an app. Wait for it to finish, then remove its data.")

        await sut.remove()

        XCTAssertEqual(cli.argv(), ["--json --remove-data"], "no removal ran")
        XCTAssertEqual(settingsRemoved, 0)
        XCTAssertEqual(sut.phase, .listed)
    }

    func test_work_that_starts_after_the_sheet_opened_still_stops_it() async throws {
        let cli = try fakeCLI(removal: Self.removed("config"))
        let sut = store(cli)
        await sut.load()
        sut.refreshWork()
        XCTAssertTrue(sut.canRemove)
        let ticket = hold.begin(TerminationHold.gettingFromAppStore, stop: nil)
        defer { hold.end(ticket) }

        await sut.remove()

        XCTAssertEqual(cli.argv().count, 1)
        XCTAssertNil(RemoveDataStore.refusal([]))
    }

    // MARK: Removing

    func test_removing_sends_exactly_whats_checked_and_the_app_removes_its_own_settings() async throws {
        let removal = [Self.removed("config", freed: 100), Self.removed("cache", freed: 5_000_000),
                       Self.removed("passwords", freed: 0),
                       #"{"type":"data-removal-end","ok":true,"dry_run":false,"removed":["config","cache","passwords"],"failed":[],"freed_bytes":5000100}"#]
            .joined(separator: "\n")
        let cli = try fakeCLI(removal: removal)
        let sut = store(cli)
        await sut.load()
        sut.chosen.insert("passwords")
        sut.chosen.remove("appstore-signin")

        await sut.remove()

        XCTAssertEqual(cli.argv().last, "--json --remove-data --items config,cache,passwords",
                       "in listing order; never the app's settings, never all")
        XCTAssertEqual(settingsRemoved, 1)
        guard case let .done(summary) = sut.phase else { return XCTFail("\(sut.phase)") }
        XCTAssertEqual(summary.removed.map(\.title),
                       ["Title of config", "Title of cache", "Title of passwords", "Title of app-settings"])
        XCTAssertTrue(summary.failed.isEmpty)
        XCTAssertTrue(summary.removedFolders)
        XCTAssertTrue(control.isLetGo(TerminationHold.removingData), "held while it ran, then let go")
        XCTAssertTrue(control.isBalanced)
    }

    func test_only_the_apps_settings_needs_no_cli() async throws {
        let cli = try fakeCLI()
        let sut = store(cli)
        await sut.load()
        sut.chosen = ["app-settings"]

        await sut.remove()

        XCTAssertEqual(cli.argv(), ["--json --remove-data"])
        XCTAssertEqual(settingsRemoved, 1)
        guard case let .done(summary) = sut.phase else { return XCTFail("\(sut.phase)") }
        XCTAssertFalse(summary.removedFolders)
    }

    func test_what_wasnt_removed_is_said_with_why() async throws {
        let cli = try fakeCLI(removal: Self.removed("cache", ok: false, freed: 10), status: 1)
        let sut = store(cli)
        await sut.load()
        sut.chosen = ["config", "cache"]

        await sut.remove()

        guard case let .done(summary) = sut.phase else { return XCTFail("\(sut.phase)") }
        XCTAssertEqual(summary.failed.map(\.title), ["Title of cache", "Title of config"])
        XCTAssertEqual(summary.failed.first?.reasons, ["/Users/example/cache: Operation not permitted"])
        XCTAssertTrue(summary.removed.isEmpty)
        XCTAssertTrue(control.isBalanced)
    }

    func test_quit_removes_the_apps_settings_once_more_first() async throws {
        let cli = try fakeCLI()
        let sut = store(cli)
        await sut.load()
        sut.chosen = ["app-settings"]
        await sut.remove()

        sut.quitGraft()

        XCTAssertEqual(settingsRemoved, 2)
        XCTAssertEqual(quits, 1)
    }

    func test_quit_after_keeping_the_settings_leaves_them() async throws {
        let cli = try fakeCLI(removal: Self.removed("cache"))
        let sut = store(cli)
        await sut.load()
        sut.chosen = ["cache"]
        await sut.remove()

        sut.quitGraft()

        XCTAssertEqual(settingsRemoved, 0)
        XCTAssertEqual(quits, 1)
    }

    func test_opening_again_starts_from_a_fresh_listing() async throws {
        let cli = try fakeCLI()
        let sut = store(cli)
        await sut.load()
        sut.chosen = ["app-settings"]
        await sut.remove()
        guard case .done = sut.phase else { return XCTFail("\(sut.phase)") }

        sut.open()

        XCTAssertEqual(sut.phase, .loading, "never the last removal's result")
        XCTAssertTrue(sut.isPresented)
        let deadline = Date().addingTimeInterval(5)
        while sut.phase != .listed, Date() < deadline {
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertEqual(sut.phase, .listed)
        XCTAssertEqual(cli.argv(), ["--json --remove-data", "--json --remove-data"])
    }

    /// The test host's own Help menu: the item is there, after Licenses.
    /// Not chosen: in a test host it would open nothing anyway.
    func test_help_has_remove_grafts_data() throws {
        let items = NSApplication.shared.mainMenu?.items ?? []
        let help = try XCTUnwrap(items.first { $0.title == "Help" }?.submenu, "\(items.map(\.title))")
        let titles = help.items.map(\.title)
        let licenses = try XCTUnwrap(titles.firstIndex(of: "Licenses"), "\(titles)")
        let remove = try XCTUnwrap(titles.firstIndex(of: RemoveDataSheet.menuTitle), "\(titles)")
        XCTAssertGreaterThan(remove, licenses)
        XCTAssertEqual(RemoveDataSheet.menuTitle, "Remove Graft's Data…")
    }

    func test_a_link_shows_no_size() throws {
        let line = Self.item("graft-link", size: 0)
        guard case let .dataItem(record) = try BuildEvent.decode(line: line) else { return XCTFail() }
        XCTAssertEqual(RemoveDataSheet.sizeText(record), "")
    }

    func test_the_confirmation_says_it_cant_be_undone() {
        let one = RemoveDataSheet.confirmation(count: 1, bytes: 0)
        XCTAssertTrue(one.hasPrefix("This removes 1 item ("), one)
        XCTAssertTrue(one.hasSuffix(" for good. Removing can't be undone."), one)
        let three = RemoveDataSheet.confirmation(count: 3, bytes: 5_000_000)
        XCTAssertTrue(three.hasPrefix("This removes 3 items (5 MB)"), three)
    }
}
