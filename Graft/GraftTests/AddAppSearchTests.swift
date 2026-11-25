import AppKit
import SwiftUI
import XCTest
@testable import Graft

/// Add App's Search mode: what picking a result sends, the Get offer, the
/// sentences for each state, and that Search is where the sheet opens.
@MainActor
final class AddAppSearchTests: XCTestCase {
    private static func result(price: Double? = 0, formatted: String? = "Free",
                               configuredAs: String? = nil,
                               bundleId: String = "com.example.app") -> AppStoreSearchResult {
        AppStoreSearchResult(
            trackId: 100000001, bundleId: bundleId, name: "Example Pro", developer: "Dev",
            version: "2.1", price: price, formattedPrice: formatted, currency: "USD",
            artworkUrl: nil, iconPath: nil, configuredAs: configuredAs
        )
    }

    private static func resultLine(_ n: Int, configuredAs: String? = nil) -> String {
        let added = configuredAs.map { #""\#($0)""# } ?? "null"
        return #"{"type":"appstore-search-result","track_id":10000000\#(n),"bundle_id":"com.example.app\#(n)","name":"Example \#(n)","developer":"Dev \#(n)","version":"\#(n).0","price":0,"formatted_price":"Free","currency":"USD","artwork_url":null,"icon_path":null,"configured_as":\#(added)}"#
    }

    private static func endLine(_ term: String, count: Int, country: String) -> String {
        #"{"type":"appstore-search-end","term":"\#(term)","country":"\#(country)","count":\#(count)}"#
    }

    /// A CLI that answers Add App (`app-added`), the reload after it (`--list`),
    /// and a get, which only logs its argv.
    private static let addScript = """
    case " $* " in
      *" --add-app-manual "*)
        printf '%s\\n' '{"type":"app-added","name":"example_pro","bundle_id":"com.example.app","display_name":"Example Pro","delivery":"livecontainer"}'
        exit 0 ;;
      *" --list "*) exit 0 ;;
      *" --appstore-get "*) exit 0 ;;
    esac
    """

    // MARK: the mode

    func test_search_is_the_first_mode_and_where_the_sheet_opens() {
        XCTAssertEqual(AddAppSheet.Mode.allCases.map(\.rawValue), ["Search", "From App Store URL", "Manual"])
        XCTAssertEqual(AddAppSheet.defaultMode, .search)
    }

    func test_the_sheet_opens_with_search_selected() throws {
        try AccessibilityTree.requireServed()
        let config = ConfigStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/true")), notify: { _ in })
        let box = hosted(AddAppSheet(store: config, appStore: AppStoreStore(runner: config.runner, shell: NoAdminShell())), width: 480)
        settle(box)
        let modes = try AccessibilityTree.nodes(in: box.window).filter {
            $0.role == kAXRadioButtonRole && AddAppSheet.Mode.allCases.map(\.rawValue).contains($0.name)
        }
        XCTAssertEqual(modes.count, 3, "premise: the mode picker's three segments")
        let selected = modes.first { ($0.value as? NSNumber)?.boolValue == true }?.name
        XCTAssertEqual(selected, "Search")
    }

    // MARK: what picking sends

    /// The same add path a URL or a hand-filled form takes: the store's name as
    /// key and display name, its bundle id, the delivery picker's choice, and
    /// the store it was found in. The CLI side (tests/test_cli_appstore_search.py,
    /// tests/test_store_country.py) pins that this is the very entry `--add-app`
    /// writes from that store's URL.
    func test_picking_a_result_adds_it_by_bundle_id_with_its_store_name() async throws {
        let cli = try ScriptCLI(Self.addScript, test: self)
        let config = ConfigStore(runner: cli.runner, notify: { _ in })
        let appStore = AppStoreStore(runner: cli.runner, shell: NoAdminShell())

        let added = try await AddAppSheet.add(
            .search(Self.result(), country: "GB", delivery: .livecontainer, getAfterAdd: false),
            store: config, appStore: appStore
        )

        XCTAssertEqual(added, "example_pro")
        XCTAssertEqual(cli.argv().first,
            "--json --add-app-manual --name Example Pro --bundle-id com.example.app --delivery livecontainer --display-name Example Pro --store-country GB")
    }

    /// No store known: nothing is sent, and the CLI asks the US store as before.
    func test_a_result_with_no_store_sends_no_store_country() async throws {
        let cli = try ScriptCLI(Self.addScript, test: self)

        _ = try await AddAppSheet.add(
            .search(Self.result(), country: nil, delivery: .livecontainer, getAfterAdd: false),
            store: ConfigStore(runner: cli.runner, notify: { _ in }), appStore: AppStoreStore(runner: cli.runner, shell: NoAdminShell())
        )

        XCTAssertEqual(cli.argv().first,
            "--json --add-app-manual --name Example Pro --bundle-id com.example.app --delivery livecontainer --display-name Example Pro")
    }

    /// A row clicked in the old list while a newer search runs belongs to the
    /// store that list came from, whatever the newer search ends in (review
    /// part 5, findings 6 and 26). The store used to be read off the state on
    /// screen at Add, and a newer search ending in no results dropped it.
    func test_a_pick_made_while_a_new_search_runs_keeps_its_own_store() async throws {
        try AccessibilityTree.requireServed()
        let cli = try ScriptCLI("""
        case "$*" in
          *"--appstore-search=example --country"*)
            printf '%s\\n' '\(Self.resultLine(1))' '\(Self.endLine("example", count: 1, country: "GB"))' ;;
          *) sleep 2; printf '%s\\n' '\(Self.endLine("examplexq", count: 0, country: "US"))' ;;
        esac
        """, test: self)
        // The picker on DE, and the list from GB: the pick is the list's store.
        let search = AppStoreSearchStore(runner: cli.runner, debounce: .milliseconds(50), region: { "DE" })
        let box = FormBox()
        let hosted = hosted(FormHarness(search: search, box: box), width: 440)
        search.update(term: "example")
        await eventually({ !search.isSearching && search.state != .idle }, timeout: 10, "the GB results")
        settle(hosted)

        search.update(term: "examplexq")
        await eventually({ cli.argv().count == 2 }, timeout: 10, "premise: the newer search's CLI is running")
        try clickButton(named: "Example 1, Dev 1, Free", in: hosted.window)
        await eventually({ !search.isSearching }, timeout: 10, "the newer search to end")

        XCTAssertEqual(search.state, .noResults(term: "examplexq", country: "US"), "premise")
        XCTAssertEqual(box.picked?.result.bundleId, "com.example.app1")
        XCTAssertEqual(box.picked?.country, "GB")
    }

    func test_get_after_add_starts_a_get_for_the_new_app() async throws {
        let cli = try ScriptCLI(Self.addScript, test: self)
        let appStore = AppStoreStore(runner: cli.runner, shell: NoAdminShell())

        _ = try await AddAppSheet.add(
            .search(Self.result(), country: "US", delivery: .livecontainer, getAfterAdd: true),
            store: ConfigStore(runner: cli.runner, notify: { _ in }), appStore: appStore
        )
        await eventually { cli.argv().contains { $0.contains("--appstore-get") } }

        XCTAssertTrue(cli.argv().contains("--json --appstore-get --app example_pro"), "\(cli.argv())")
    }

    func test_without_get_after_add_nothing_is_downloaded() async throws {
        let cli = try ScriptCLI(Self.addScript, test: self)
        let appStore = AppStoreStore(runner: cli.runner, shell: NoAdminShell())

        _ = try await AddAppSheet.add(
            .search(Self.result(), country: "US", delivery: .livecontainer, getAfterAdd: false),
            store: ConfigStore(runner: cli.runner, notify: { _ in }), appStore: appStore
        )
        try await Task.sleep(for: .milliseconds(300))

        XCTAssertFalse(cli.argv().contains { $0.contains("--appstore-get") }, "\(cli.argv())")
        XCTAssertFalse(appStore.isBusy("example_pro"))
    }

    // MARK: the Get offer

    private func status(ready: Bool) -> AppStoreStatusRecord {
        AppStoreStatusRecord(
            ipatool: .init(path: "/i", present: true, version: "2.8.0", signedIn: ready, warning: nil),
            unfairplay: .init(path: "/u", present: true, version: "v0.4.0", enrolled: true, modelDir: "/m"),
            setupCommands: []
        )
    }

    func test_the_get_offer_needs_both_tools_ready() {
        XCTAssertEqual(AddAppSheet.getOffer(status: status(ready: true), hasLoaded: true), .ready)
        XCTAssertEqual(AddAppSheet.getOffer(status: status(ready: false), hasLoaded: true), .notReady)
        XCTAssertEqual(AddAppSheet.getOffer(status: nil, hasLoaded: true), .notReady, "a failed status load")
        XCTAssertEqual(AddAppSheet.getOffer(status: nil, hasLoaded: false), .checking)
        XCTAssertEqual(AddAppSheet.getOffer(status: nil, hasLoaded: false, timedOut: true), .timedOut)
        XCTAssertEqual(AddAppSheet.getOffer(status: status(ready: true), hasLoaded: true, timedOut: true), .ready)
    }

    func test_a_free_result_gets_by_default_and_a_paid_one_does_not() {
        XCTAssertTrue(AddAppSheet.getsAfterAddingByDefault(Self.result()))
        XCTAssertFalse(AddAppSheet.getsAfterAddingByDefault(Self.result(price: 4.99, formatted: "$4.99")))
    }

    // MARK: the status, asked only when a pick needs it

    /// Opening Add App and typing never runs `--appstore-status`, which runs
    /// `ipatool auth info` and reads the keychain. Only a pick that could be
    /// got asks it.
    func test_the_status_is_asked_only_once_a_pick_could_be_got() {
        XCTAssertFalse(AddAppSheet.wantsStatus(mode: .search, picked: nil), "the sheet opening, typing")
        XCTAssertTrue(AddAppSheet.wantsStatus(mode: .search, picked: Self.result()))
        XCTAssertFalse(AddAppSheet.wantsStatus(mode: .url, picked: Self.result()))
        XCTAssertFalse(AddAppSheet.wantsStatus(mode: .search, picked: Self.result(configuredAs: "example")))
        XCTAssertFalse(AddAppSheet.wantsStatus(mode: .search, picked: Self.result(bundleId: "com.apple.example")))
    }

    func test_opening_the_sheet_and_searching_runs_no_status_and_a_pick_does() async throws {
        try AccessibilityTree.requireServed()
        let cli = try ScriptCLI("""
        case "$*" in
          *"--appstore-search="*)
            printf '%s\\n' '\(Self.resultLine(1))' '\(Self.endLine("example", count: 1, country: "US"))' ;;
          *) exit 0 ;;
        esac
        """, test: self)
        let config = ConfigStore(runner: cli.runner, notify: { _ in })
        let appStore = AppStoreStore(runner: cli.runner, shell: NoAdminShell())
        let hosted = hosted(AddAppSheet(store: config, appStore: appStore), width: 480)
        settle(hosted, seconds: 1)
        appStore.search.update(term: "example")
        await eventually({ !appStore.search.isSearching && appStore.search.state != .idle }, timeout: 10,
                         "the results")
        settle(hosted, seconds: 1)

        XCTAssertFalse(cli.argv().contains { $0.contains("--appstore-status") }, "\(cli.argv())")

        try clickButton(named: "Example 1, Dev 1, Free", in: hosted.window)
        await eventually({ cli.argv().contains { $0.contains("--appstore-status") } }, timeout: 10,
                         "the status, once a result that could be got is picked")
    }

    func test_a_status_that_doesnt_answer_in_time_is_given_up_and_records_nothing() async throws {
        let cli = try ScriptCLI("sleep 20", test: self)
        let appStore = AppStoreStore(runner: cli.runner, shell: NoAdminShell())

        let started = Date()
        let timedOut = await AddAppSheet.loadStatus(appStore, timeout: .milliseconds(300))

        XCTAssertTrue(timedOut)
        XCTAssertLessThan(Date().timeIntervalSince(started), 5)
        XCTAssertFalse(appStore.hasLoadedStatus, "Settings › App Store still loads it fresh")
        XCTAssertLessThanOrEqual(AddAppSheet.statusTimeout, .seconds(10))
    }

    func test_a_status_that_answers_is_not_a_timeout() async throws {
        let cli = try ScriptCLI("exit 0", test: self)
        let appStore = AppStoreStore(runner: cli.runner, shell: NoAdminShell())

        let timedOut = await AddAppSheet.loadStatus(appStore, timeout: .seconds(10))

        XCTAssertFalse(timedOut)
        XCTAssertTrue(appStore.hasLoadedStatus)
    }

    // MARK: Apple's own apps, and a price the store didn't give

    func test_apples_own_app_offers_no_get_and_says_why() {
        let apple = Self.result(bundleId: "com.apple.example")
        XCTAssertFalse(AddAppSheet.offersGet(apple))
        XCTAssertFalse(AddAppSheet.getsAfterAddingByDefault(apple))
        XCTAssertTrue(AddAppSheet.offersGet(Self.result(bundleId: "com.applesoft.example")))
        XCTAssertTrue(AppStoreSearchForm.applesOwn.contains("Apple's own apps"))
    }

    /// The CLI's table and the app agree: no number is "price unknown",
    /// never "Free", and a get of it is never pre-checked.
    func test_a_missing_price_is_price_unknown_never_free() throws {
        let unknown = Self.result(price: nil, formatted: "Free")
        XCTAssertEqual(unknown.acquisition, .unknown)
        XCTAssertEqual(AppStoreSearchForm.price(unknown), "Price unknown")
        XCTAssertFalse(AddAppSheet.getsAfterAddingByDefault(unknown))
        XCTAssertNotNil(AppStoreSearchForm.ownershipNote(unknown))
    }

    // MARK: VoiceOver and the keyboard

    func test_a_rows_label_says_added_and_the_picked_row_says_selected() async throws {
        try AccessibilityTree.requireServed()
        let cli = try ScriptCLI(
            "printf '%s\\n' '\(Self.resultLine(1, configuredAs: "example"))' '\(Self.resultLine(2))' '\(Self.endLine("example", count: 2, country: "US"))'",
            test: self
        )
        let search = AppStoreSearchStore(runner: cli.runner, debounce: .milliseconds(50), region: { "US" })
        let box = FormBox()
        let hosted = hosted(FormHarness(search: search, box: box), width: 440)
        search.update(term: "example")
        await eventually({ !search.isSearching && search.state != .idle }, timeout: 10, "the results")
        settle(hosted)

        try clickButton(named: "Example 2, Dev 2, Free", in: hosted.window)
        settle(hosted)

        let rows = try AccessibilityTree.nodes(in: hosted.window).filter {
            $0.role == kAXButtonRole && $0.name.hasPrefix("Example ")
        }
        XCTAssertEqual(rows.map(\.name), ["Example 1, Dev 1, Free, Added", "Example 2, Dev 2, Free"])
        XCTAssertEqual(rows.map { $0.value as? String }, ["", AppStoreSearchForm.selectedValue])
    }

    func test_the_arrow_keys_move_the_pick_and_return_picks_the_first() async throws {
        let cli = try ScriptCLI(
            "printf '%s\\n' '\(Self.resultLine(1))' '\(Self.resultLine(2))' '\(Self.endLine("example", count: 2, country: "DE"))'",
            test: self
        )
        let search = AppStoreSearchStore(runner: cli.runner, debounce: .milliseconds(50), region: { "DE" })
        let box = FormBox()
        let hosted = hosted(FormHarness(search: search, box: box), width: 440)
        search.update(term: "example")
        await eventually({ !search.isSearching && search.state != .idle }, timeout: 10, "the results")
        settle(hosted)
        let field = try XCTUnwrap(textField(in: hosted.window.contentView), "the search field")
        XCTAssertTrue(hosted.window.makeFirstResponder(field), "premise: the field takes focus")
        settle(hosted)

        press(key: 36, "\r", in: hosted.window)  // Return
        settle(hosted, seconds: 0.2)
        XCTAssertEqual(box.picked?.result.bundleId, "com.example.app1", "Return picks the first")
        press(key: 125, String(UnicodeScalar(UInt16(NSDownArrowFunctionKey))!), in: hosted.window)
        settle(hosted, seconds: 0.2)
        XCTAssertEqual(box.picked?.result.bundleId, "com.example.app2")
        press(key: 125, String(UnicodeScalar(UInt16(NSDownArrowFunctionKey))!), in: hosted.window)
        settle(hosted, seconds: 0.2)
        XCTAssertEqual(box.picked?.result.bundleId, "com.example.app2", "kept inside the list")
        press(key: 126, String(UnicodeScalar(UInt16(NSUpArrowFunctionKey))!), in: hosted.window)
        settle(hosted, seconds: 0.2)
        XCTAssertEqual(box.picked?.result.bundleId, "com.example.app1")
        XCTAssertEqual(box.picked?.country, "DE")
    }

    func test_an_arrow_key_moves_through_the_list_and_stays_inside_it() {
        let a = Self.result()
        let b = AppStoreSearchResult(
            trackId: 100000002, bundleId: "com.example.b", name: "B", developer: "Dev", version: "1",
            price: 0, formattedPrice: "Free", currency: "USD", artworkUrl: nil, iconPath: nil, configuredAs: nil
        )
        XCTAssertEqual(AppStoreSearchForm.moved(1, from: nil, in: [a, b]), a)
        XCTAssertEqual(AppStoreSearchForm.moved(-1, from: nil, in: [a, b]), b)
        XCTAssertEqual(AppStoreSearchForm.moved(1, from: a, in: [a, b]), b)
        XCTAssertEqual(AppStoreSearchForm.moved(1, from: b, in: [a, b]), b)
        XCTAssertEqual(AppStoreSearchForm.moved(-1, from: a, in: [a, b]), a)
        XCTAssertNil(AppStoreSearchForm.moved(1, from: nil, in: []))
    }

    private func textField(in view: NSView?) -> NSTextField? {
        guard let view else { return nil }
        if let field = view as? NSTextField, field.isEditable { return field }
        for sub in view.subviews {
            if let found = textField(in: sub) { return found }
        }
        return nil
    }

    private func press(key code: UInt16, _ characters: String, in window: NSWindow) {
        for type: NSEvent.EventType in [.keyDown, .keyUp] {
            guard let event = NSEvent.keyEvent(
                with: type, location: .zero, modifierFlags: [], timestamp: 0,
                windowNumber: window.windowNumber, context: nil, characters: characters,
                charactersIgnoringModifiers: characters, isARepeat: false, keyCode: code
            ) else { continue }
            window.sendEvent(event)
        }
    }

    // MARK: paid apps

    func test_a_paid_result_shows_its_price_and_says_graft_downloads_only_what_the_apple_id_owns() throws {
        let paid = Self.result(price: 4.99, formatted: "$4.99")
        XCTAssertEqual(AppStoreSearchForm.price(paid), "$4.99")
        let note = try XCTUnwrap(AppStoreSearchForm.ownershipNote(paid))
        XCTAssertTrue(note.hasPrefix("$4.99 on the App Store."), note)
        XCTAssertTrue(note.contains("Graft can only download apps this Apple ID already owns"), note)
    }

    func test_a_free_result_has_no_ownership_note() {
        XCTAssertEqual(AppStoreSearchForm.price(Self.result()), "Free")
        XCTAssertNil(AppStoreSearchForm.ownershipNote(Self.result()))
    }

    func test_a_result_already_added_says_under_which_name() {
        XCTAssertEqual(AppStoreSearchForm.alreadyAdded("example"), "Already added as “example”.")
    }

    // MARK: each state's sentence

    func test_add_sends_the_store_the_pick_carries_and_gets_only_what_it_can() {
        let pick = AppStoreSearchPick(result: Self.result(), country: "GB")
        XCTAssertEqual(
            AddAppSheet.searchRequest(pick, delivery: .livecontainer, getAfterAdd: true, offer: .ready),
            .search(pick.result, country: "GB", delivery: .livecontainer, getAfterAdd: true)
        )
        XCTAssertEqual(
            AddAppSheet.searchRequest(pick, delivery: .livecontainer, getAfterAdd: true, offer: .checking),
            .search(pick.result, country: "GB", delivery: .livecontainer, getAfterAdd: false)
        )
        let apple = AppStoreSearchPick(result: Self.result(bundleId: "com.apple.example"), country: "US")
        XCTAssertEqual(
            AddAppSheet.searchRequest(apple, delivery: .livecontainer, getAfterAdd: true, offer: .ready),
            .search(apple.result, country: "US", delivery: .livecontainer, getAfterAdd: false)
        )
    }

    func test_a_pick_sends_the_store_it_carries() async throws {
        let cli = try ScriptCLI(Self.addScript, test: self)
        let pick = AppStoreSearchPick(result: Self.result(), country: "JP")

        _ = try await AddAppSheet.add(
            .search(pick.result, country: pick.country, delivery: .livecontainer, getAfterAdd: false),
            store: ConfigStore(runner: cli.runner, notify: { _ in }), appStore: AppStoreStore(runner: cli.runner, shell: NoAdminShell())
        )

        XCTAssertTrue(cli.argv().first?.hasSuffix("--store-country JP") == true, "\(cli.argv())")
    }

    func test_a_whitespace_only_edit_is_the_same_search() {
        XCTAssertTrue(AddAppSheet.sameSearch("notes", "notes "))
        XCTAssertTrue(AddAppSheet.sameSearch(" notes", "notes"))
        XCTAssertFalse(AddAppSheet.sameSearch("notes", "notes p"))
    }

    /// Combined fix check #25: `sameSearch` alone was tested, and the sheet's
    /// `onChange(of: searchTerm)` that uses it never ran in a test. Typed into
    /// the field, as a keyboard does: a trailing space keeps the pick (and its
    /// Get toggle); a letter is a new search and drops it.
    func test_typing_a_space_after_a_pick_keeps_it_and_a_letter_drops_it() async throws {
        try AccessibilityTree.requireServed()
        let cli = try ScriptCLI("""
        case "$*" in
          *"--appstore-search="*)
            printf '%s\\n' '\(Self.resultLine(1))' '\(Self.endLine("example", count: 1, country: "US"))' ;;
          *) exit 0 ;;
        esac
        """, test: self)
        let config = ConfigStore(runner: cli.runner, notify: { _ in })
        let appStore = AppStoreStore(runner: cli.runner, shell: NoAdminShell())
        let box = hosted(AddAppSheet(store: config, appStore: appStore), width: 480)
        settle(box, seconds: 1)
        try typeSearch("example", in: box.window)
        await eventually({ !appStore.search.isSearching && appStore.search.state != .idle }, timeout: 10,
                         "the results")
        settle(box, seconds: 1)
        let row = "Example 1, Dev 1, Free"
        try clickButton(named: row, in: box.window)
        settle(box)
        XCTAssertTrue(try isPicked(row, in: box.window), "premise: the click picked it")

        try typeSearch(" ", in: box.window)
        settle(box, seconds: 1)
        XCTAssertTrue(try isPicked(row, in: box.window), "a trailing space dropped the pick")

        try typeSearch("x", in: box.window)
        settle(box, seconds: 1)
        XCTAssertFalse(try isPicked(row, in: box.window), "a new term kept the old pick")
    }

    /// Types into the search field through its field editor, so SwiftUI's
    /// binding, and the sheet's onChange, see the edit.
    private func typeSearch(_ text: String, in window: NSWindow) throws {
        func fields(in view: NSView) -> [NSTextField] {
            ((view as? NSTextField).map { [$0] } ?? []) + view.subviews.flatMap { fields(in: $0) }
        }
        let field = try XCTUnwrap(
            window.contentView.flatMap(fields(in:))?.first {
                $0.isEditable && $0.placeholderString == AppStoreSearchForm.placeholder
            },
            "no search field"
        )
        if field.currentEditor() == nil { XCTAssertTrue(window.makeFirstResponder(field)) }
        let editor = try XCTUnwrap(field.currentEditor() as? NSTextView, "no field editor")
        editor.insertText(text, replacementRange: NSRange(location: NSNotFound, length: 0))
    }

    private func isPicked(_ row: String, in window: NSWindow) throws -> Bool {
        let node = try AccessibilityTree.nodes(in: window).first { $0.name == row }
        return (node?.value as? String) == AppStoreSearchForm.selectedValue
    }

    func test_each_state_says_what_it_is() {
        XCTAssertEqual(AppStoreSearchForm.idle, "Type at least 2 letters of an app's name.")
        XCTAssertEqual(AppStoreSearchForm.noResults(term: "zzzz", country: "GB"),
                       "No iPhone apps on the GB App Store match “zzzz”.")
        XCTAssertEqual(AppStoreSearchForm.offline, AppStoreSearchOffline.message)
        XCTAssertEqual(AppStoreSearchForm.failed("HTTP 503"), "The search didn't work: HTTP 503")
    }
}


/// What the sheet holds for its Search form, for a test to host the form alone.
@MainActor
final class FormBox: ObservableObject {
    @Published var term = ""
    @Published var picked: AppStoreSearchPick?
}

/// `AppStoreSearchForm` bound to `@State`, as `AddAppSheet` binds it, and
/// copied to a `FormBox` for the test to read. Not bound to the box itself:
/// the form writes the pick from its arrow keys' handler, where `@State` may
/// change but an `ObservableObject` may not publish.
struct FormHarness: View {
    @ObservedObject var search: AppStoreSearchStore
    @ObservedObject var box: FormBox
    @State var term = ""
    @State var picked: AppStoreSearchPick?

    var body: some View {
        AppStoreSearchForm(search: search, term: $term, picked: $picked)
            .padding(20)
            .onChange(of: term) { box.term = $0 }
            .onChange(of: picked) { box.picked = $0 }
    }
}
