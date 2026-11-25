import XCTest
@testable import Graft

/// Older App Store versions: the sheet's list (`AppStoreVersionsStore`), a
/// get of a picked version (`AppStoreStore.startGet(versionId:)`), and the
/// held line on the App Store row.
///
/// Runners are real `/bin/sh` scripts printing the exact lines
/// `graft_ipa/appstore.py` emits (pinned on the Python side by
/// tests/test_appstore_versions.py), so the argv under test is production's.
final class AppStoreVersionsTests: XCTestCase {
    private func scriptRunner(_ body: String) throws -> (CLIRunner, URL) {
        let dir = FileManager.default.temporaryDirectory
            .appending(path: "appstore_versions_\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let log = dir.appending(path: "argv.log")
        let script = dir.appending(path: "tb.sh")
        try """
        #!/bin/sh
        echo "$*" >> '\(log.path)'
        \(body)
        """.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
        return (CLIRunner(executable: script), log)
    }

    private func argv(_ log: URL) throws -> [String] {
        try String(contentsOf: log, encoding: .utf8).split(separator: "\n").map(String.init)
    }

    @MainActor
    private func waitUntil(_ condition: () -> Bool, timeout: TimeInterval = 10) async {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition(), Date() < deadline {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    private static func row(
        _ id: String, index: Int, version: String?, date: String? = nil, staged: Bool = false,
        error: String? = nil
    ) -> String {
        func json(_ value: String?) -> String { value.map { "\"\($0)\"" } ?? "null" }
        return #"{"type":"appstore-release","app":"example","bundle_id":"com.example.app","external_id":"\#(id)","index":\#(index),"newest":\#(index == 0),"version":\#(json(version)),"release_date":\#(json(date)),"staged":\#(staged),"error":\#(json(error))}"#
    }

    private static func end(count: Int, offset: Int = 0, limit: Int = 10, held: String? = nil) -> String {
        let heldJSON = held.map { "\"\($0)\"" } ?? "null"
        return #"{"type":"appstore-releases-end","app":"example","bundle_id":"com.example.app","count":\#(count),"offset":\#(offset),"limit":\#(limit),"held_version":\#(heldJSON)}"#
    }

    private static func record(
        _ id: String, index: Int, version: String?, error: String? = nil, staged: Bool = false
    ) -> AppStoreReleaseRecord {
        AppStoreReleaseRecord(
            app: "example", bundleId: "com.example.app", externalId: id, index: index, newest: index == 0,
            version: version, releaseDate: version == nil ? nil : "2026-08-01T07:00:00Z", staged: staged,
            error: error
        )
    }

    // MARK: The list

    @MainActor
    func test_the_listing_asks_for_one_app_and_a_later_page_by_its_offset() {
        XCTAssertEqual(
            AppStoreVersionsStore.args(app: "example"),
            ["--json", "--appstore-versions", "--app", "example"]
        )
        XCTAssertEqual(
            AppStoreVersionsStore.args(app: "example", offset: 10),
            ["--json", "--appstore-versions", "--app", "example", "--offset", "10"]
        )
    }

    /// Rows show as the CLI reads them, not all at once at the end: reading
    /// each one is a request, and a page takes a while.
    @MainActor
    func test_rows_arrive_one_at_a_time_then_the_sentinel_finishes_the_list() async throws {
        let (runner, log) = try scriptRunner("""
        printf '%s\\n' '\(Self.row("800400", index: 0, version: "1.4.0", date: "2026-10-01T07:00:00Z"))'
        sleep 1
        printf '%s\\n' '\(Self.row("800300", index: 1, version: "1.3.0"))'
        printf '%s\\n' '\(Self.row("800200", index: 2, version: nil))'
        printf '%s\\n' '\(Self.end(count: 3, limit: 2, held: "1.3.0"))'
        """)
        let store = AppStoreVersionsStore(runner: runner)

        store.load(app: "example")
        XCTAssertEqual(store.phase, .loading)
        await waitUntil { store.releases.count == 1 }
        XCTAssertEqual(store.releases.map(\.externalId), ["800400"], "the first row is on screen while the CLI reads")
        XCTAssertEqual(store.phase, .loading)

        await waitUntil { store.phase == .loaded }
        XCTAssertEqual(store.releases.map(\.externalId), ["800400", "800300", "800200"])
        XCTAssertEqual(store.heldVersion, "1.3.0")
        XCTAssertEqual(store.readingUpTo, 2)
        XCTAssertEqual(store.nextOffset, 2, "800200 is unread past the page")
        XCTAssertEqual(try argv(log), ["--json --appstore-versions --app example"])
    }

    @MainActor
    func test_show_more_reads_from_the_end_of_the_page_and_keeps_the_rows() async throws {
        let flag = FileManager.default.temporaryDirectory.appending(path: "versions_more_\(UUID().uuidString)")
        addTeardownBlock { try? FileManager.default.removeItem(at: flag) }
        let (runner, log) = try scriptRunner("""
        if [ -e '\(flag.path)' ]; then
          printf '%s\\n' '\(Self.row("800400", index: 0, version: "1.4.0"))'
          printf '%s\\n' '\(Self.row("800300", index: 1, version: "1.3.0"))'
          printf '%s\\n' '\(Self.end(count: 2, offset: 1, limit: 10))'
        else
          printf '%s\\n' '\(Self.row("800400", index: 0, version: "1.4.0"))'
          printf '%s\\n' '\(Self.row("800300", index: 1, version: nil))'
          printf '%s\\n' '\(Self.end(count: 2, limit: 1))'
          touch '\(flag.path)'
        fi
        """)
        let store = AppStoreVersionsStore(runner: runner)
        store.load(app: "example")
        await waitUntil { store.phase == .loaded }
        XCTAssertEqual(store.nextOffset, 1)

        store.loadMore()
        await waitUntil { store.phase == .loaded && store.releases.last?.version != nil }

        XCTAssertEqual(try argv(log), [
            "--json --appstore-versions --app example",
            "--json --appstore-versions --app example --offset 1",
        ])
        XCTAssertEqual(store.releases.map(\.version), ["1.4.0", "1.3.0"])
        XCTAssertNil(store.nextOffset, "nothing left to read")
    }

    @MainActor
    func test_there_is_more_only_when_something_past_the_page_is_unread() {
        let read = Self.record("1", index: 1, version: "1.0")
        let unread = Self.record("1", index: 1, version: nil)
        let gone = Self.record("1", index: 1, version: nil, error: "Apple didn't describe this version")
        XCTAssertEqual(AppStoreVersionsStore.nextOffset([unread], after: 1), 1)
        XCTAssertNil(AppStoreVersionsStore.nextOffset([read], after: 1))
        XCTAssertNil(AppStoreVersionsStore.nextOffset([gone], after: 1), "Apple already said no to that one")
        XCTAssertNil(AppStoreVersionsStore.nextOffset([unread], after: 2), "it was on the page")
    }

    @MainActor
    func test_a_failed_list_is_said_and_try_again_asks_again() async throws {
        let (runner, log) = try scriptRunner("echo 'error: ipatool is not signed in to the App Store' >&2; exit 20")
        let store = AppStoreVersionsStore(runner: runner)

        store.load(app: "example")
        await waitUntil { if case .failed = store.phase { return true } else { return false } }
        guard case let .failed(message) = store.phase else { return XCTFail("expected a failure") }
        XCTAssertTrue(message.contains("not signed in"), message)

        store.retry()
        await waitUntil { (try? self.argv(log).count) == 2 }
        XCTAssertEqual(try argv(log).count, 2)
    }

    @MainActor
    func test_an_app_the_apple_id_lacks_is_said_in_the_sheets_words() async throws {
        let refused = #"{"type":"appstore-refused","app":"example","reason":"needs-license","price":0,"formatted_price":"Free","track_id":1}"#
        let (runner, _) = try scriptRunner("printf '%s\\n' '\(refused)'; echo 'error: no license' >&2; exit 20")
        let store = AppStoreVersionsStore(runner: runner)

        store.load(app: "example")
        await waitUntil { if case .failed = store.phase { return true } else { return false } }

        XCTAssertEqual(store.phase, .failed(AppStoreVersionsStore.needsLicenseMessage))
        XCTAssertFalse(AppStoreVersionsStore.needsLicenseMessage.contains("Get (free)"))
    }

    @MainActor
    func test_a_list_cut_off_before_its_sentinel_is_not_a_list() async throws {
        let (runner, _) = try scriptRunner("printf '%s\\n' '\(Self.row("800400", index: 0, version: "1.4.0"))'")
        let store = AppStoreVersionsStore(runner: runner)

        store.load(app: "example")
        await waitUntil { store.phase != .loading }

        guard case .failed = store.phase else { return XCTFail("expected a failure, got \(store.phase)") }
    }

    /// A list that fails partway (a version timed out, a license went
    /// missing) never sends its sentinel. The held version the sheet opened
    /// with stays, so the held, staged row still offers no Get.
    @MainActor
    func test_a_list_that_fails_partway_keeps_the_held_version_it_opened_with() async throws {
        let (runner, _) = try scriptRunner("""
        printf '%s\\n' '\(Self.row("800400", index: 0, version: "1.4.0", staged: true))'
        echo 'error: the list of versions stopped' >&2
        exit 20
        """)
        let store = AppStoreVersionsStore(runner: runner)

        store.load(app: "example", held: "1.4.0")
        await waitUntil { store.phase != .loading }

        guard case .failed = store.phase else { return XCTFail("expected a failure, got \(store.phase)") }
        XCTAssertEqual(store.heldVersion, "1.4.0")
        let row = try XCTUnwrap(store.releases.first)
        XCTAssertFalse(AppStoreVersionsSheet.offersGet(row, held: store.heldVersion, state: .ready),
                       "the held version is staged: nothing to get")
    }

    /// The row as the sheet draws it, not only `offersGet`: the held
    /// version, its IPA deleted, offers Get once the list says it is held,
    /// which is the one way back to it that keeps the hold. Held and
    /// staged, it offers none, and a row being read shows the spinner.
    @MainActor
    func test_the_sheet_offers_get_on_the_held_row_whose_ipa_is_gone() async throws {
        let (runner, _) = try scriptRunner("""
        printf '%s\\n' '\(Self.row("800400", index: 0, version: "1.4.0"))'
        printf '%s\\n' '\(Self.row("800300", index: 1, version: "1.3.0", staged: true))'
        printf '%s\\n' '\(Self.row("800200", index: 2, version: "1.2.0", staged: false))'
        printf '%s\\n' '\(Self.end(count: 3, held: "1.2.0"))'
        """)
        let store = AppStoreVersionsStore(runner: runner)

        store.load(app: "example")
        await waitUntil { store.phase != .loading }

        XCTAssertEqual(store.phase, .loaded)
        XCTAssertEqual(store.heldVersion, "1.2.0", "from the list, not the sheet's opening")
        let rows = Dictionary(uniqueKeysWithValues: store.releases.map { ($0.externalId, $0) })
        typealias Sheet = AppStoreVersionsSheet
        XCTAssertEqual(Sheet.accessory(try XCTUnwrap(rows["800200"]), in: store), .get, "held, not staged")
        XCTAssertEqual(Sheet.accessory(try XCTUnwrap(rows["800400"]), in: store), .get)
        XCTAssertEqual(Sheet.accessory(try XCTUnwrap(rows["800300"]), in: store), .get, "staged, not held")

        let (heldStaged, _) = try scriptRunner("""
        printf '%s\\n' '\(Self.row("800200", index: 0, version: "1.2.0", staged: true))'
        printf '%s\\n' '\(Self.end(count: 1, held: "1.2.0"))'
        """)
        let other = AppStoreVersionsStore(runner: heldStaged)
        other.load(app: "example")
        await waitUntil { other.phase != .loading }
        XCTAssertEqual(Sheet.accessory(try XCTUnwrap(other.releases.first), in: other), .nothing,
                       "held and staged: nothing to get")

        let reading = Self.record("800100", index: 0, version: nil)
        let (slow, _) = try scriptRunner("sleep 5")
        let loading = AppStoreVersionsStore(runner: slow)
        loading.load(app: "example")
        XCTAssertEqual(loading.phase, .loading)
        XCTAssertEqual(Sheet.accessory(reading, in: loading), .reading)
        loading.reset()
    }

    /// The sentinel has the last word: a hold let go elsewhere since the
    /// sheet opened is gone when the list finishes.
    @MainActor
    func test_the_sentinel_replaces_the_held_version_the_sheet_opened_with() async throws {
        let (runner, _) = try scriptRunner("""
        printf '%s\\n' '\(Self.row("800400", index: 0, version: "1.4.0"))'
        printf '%s\\n' '\(Self.end(count: 1))'
        """)
        let store = AppStoreVersionsStore(runner: runner)

        store.load(app: "example", held: "1.4.0")
        await waitUntil { store.phase != .loading }

        XCTAssertEqual(store.phase, .loaded)
        XCTAssertNil(store.heldVersion)
    }

    /// The sheet closed mid-read: what that run still prints lands nowhere.
    @MainActor
    func test_a_list_reset_while_reading_writes_nothing_after() async throws {
        let (runner, _) = try scriptRunner("""
        sleep 1
        printf '%s\\n' '\(Self.row("800400", index: 0, version: "1.4.0"))'
        printf '%s\\n' '\(Self.end(count: 1))'
        """)
        let store = AppStoreVersionsStore(runner: runner)

        store.load(app: "example")
        store.reset()
        try await Task.sleep(nanoseconds: 1_600_000_000)

        XCTAssertEqual(store.phase, .idle)
        XCTAssertTrue(store.releases.isEmpty)
        XCTAssertNil(store.app)
    }

    /// The generation guard on its own: a row from a run this store no
    /// longer owns is dropped.
    @MainActor
    func test_a_row_from_an_older_run_is_dropped() throws {
        let (runner, _) = try scriptRunner("sleep 5")
        let store = AppStoreVersionsStore(runner: runner)
        store.load(app: "example")
        let old = BuildEvent.appStoreRelease(Self.record("800400", index: 0, version: "1.4.0"))

        store.note(old, generation: -1)

        XCTAssertTrue(store.releases.isEmpty)
        store.reset()
    }

    /// A page read again that couldn't read a row this time keeps what an
    /// earlier run said of it.
    @MainActor
    func test_a_row_read_before_keeps_its_version_when_a_later_run_lacks_it() async throws {
        let flag = FileManager.default.temporaryDirectory.appending(path: "versions_keep_\(UUID().uuidString)")
        addTeardownBlock { try? FileManager.default.removeItem(at: flag) }
        let (runner, _) = try scriptRunner("""
        if [ -e '\(flag.path)' ]; then
          printf '%s\\n' '\(Self.row("800400", index: 0, version: nil))'
          printf '%s\\n' '\(Self.row("800300", index: 1, version: nil))'
          printf '%s\\n' '\(Self.end(count: 2, offset: 1, limit: 10))'
        else
          printf '%s\\n' '\(Self.row("800400", index: 0, version: "1.4.0"))'
          printf '%s\\n' '\(Self.row("800300", index: 1, version: nil))'
          printf '%s\\n' '\(Self.end(count: 2, limit: 1))'
          touch '\(flag.path)'
        fi
        """)
        let store = AppStoreVersionsStore(runner: runner)
        store.load(app: "example")
        await waitUntil { store.phase == .loaded }
        store.loadMore()
        await waitUntil { store.phase == .loaded && store.readingUpTo == 11 }

        XCTAssertEqual(store.releases.first?.version, "1.4.0")
    }

    @MainActor
    func test_each_row_says_where_it_has_got_to() {
        let ready = Self.record("4", index: 0, version: "1.4.0")
        let unread = Self.record("3", index: 3, version: nil)
        let gone = Self.record("2", index: 1, version: nil, error: "Apple didn't describe this version")
        typealias S = AppStoreVersionsStore
        XCTAssertEqual(S.rowState(ready, phase: .loading, readingUpTo: 10), .ready)
        XCTAssertEqual(S.rowState(unread, phase: .loading, readingUpTo: 10), .reading)
        XCTAssertEqual(S.rowState(unread, phase: .loading, readingUpTo: 2), .unread)
        XCTAssertEqual(S.rowState(unread, phase: .loaded, readingUpTo: 10), .unread)
        XCTAssertEqual(S.rowState(gone, phase: .loading, readingUpTo: 10), .unavailable("Apple didn't describe this version"))
    }

    // MARK: The sheet's words

    @MainActor
    func test_the_sheet_states_the_limits_plainly() {
        let limits = AppStoreVersionsSheet.limits
        XCTAssertTrue(limits.contains("Only apps this Apple ID has"))
        XCTAssertTrue(limits.contains("Apple doesn't serve every old version"))
        XCTAssertTrue(limits.contains("refuse to run once they're too old"))
        XCTAssertTrue(limits.contains("holds the app at that version"))
    }

    @MainActor
    func test_a_row_reads_as_version_date_and_what_is_true_of_it() {
        let newest = Self.record("800400", index: 0, version: "1.4.0", staged: true)
        XCTAssertEqual(AppStoreVersionsSheet.headline(newest, state: .ready), "1.4.0")
        XCTAssertEqual(
            AppStoreVersionsSheet.detailLine(newest, held: "1.4.0", state: .ready),
            "Aug 1, 2026 · newest · held: builds use this one · staged · id 800400"
        )
        let unread = Self.record("800100", index: 12, version: nil)
        XCTAssertEqual(AppStoreVersionsSheet.headline(unread, state: .unread), "Not read yet")
        XCTAssertEqual(AppStoreVersionsSheet.detailLine(unread, held: nil, state: .unread), "Show More reads it · id 800100")
        XCTAssertEqual(AppStoreVersionsSheet.headline(unread, state: .reading), "Reading…")
    }

    @MainActor
    func test_a_release_date_reads_as_its_day_in_utc() {
        XCTAssertEqual(AppStoreVersionsSheet.releaseDate("2026-08-01T00:30:00Z"), "Aug 1, 2026")
        XCTAssertNil(AppStoreVersionsSheet.releaseDate("yesterday"))
        XCTAssertNil(AppStoreVersionsSheet.releaseDate(nil))
    }

    @MainActor
    func test_get_says_whether_it_downloads() {
        XCTAssertTrue(AppStoreVersionsSheet.getHelp(Self.record("1", index: 1, version: "1.2.0", staged: true))
            .contains("without downloading"))
        XCTAssertTrue(AppStoreVersionsSheet.getHelp(Self.record("1", index: 1, version: "1.2.0"))
            .contains("Download, decrypt and stage 1.2.0"))
    }

    /// The held version's row offers no Get while its IPA is staged: there
    /// is nothing to get. Once that IPA is gone (deleted in the Cache pane),
    /// Get is the one way back to it that keeps the hold; Get latest lets go.
    @MainActor
    func test_get_is_offered_on_every_read_row_but_the_held_one_that_is_staged() {
        typealias Sheet = AppStoreVersionsSheet
        let heldStaged = Self.record("800200", index: 2, version: "1.2.0", staged: true)
        let heldGone = Self.record("800200", index: 2, version: "1.2.0", staged: false)
        let other = Self.record("800300", index: 1, version: "1.3.0")

        XCTAssertFalse(Sheet.offersGet(heldStaged, held: "1.2.0", state: .ready))
        XCTAssertTrue(Sheet.offersGet(heldGone, held: "1.2.0", state: .ready), "a held version that isn't staged")
        XCTAssertTrue(Sheet.offersGet(other, held: "1.2.0", state: .ready))
        XCTAssertTrue(Sheet.offersGet(heldStaged, held: nil, state: .ready), "staged but not held: Get holds it")
        let unread = Self.record("800100", index: 12, version: nil)
        for state in [AppStoreVersionsStore.RowState.reading, .unread, .unavailable("gone")] {
            XCTAssertFalse(Sheet.offersGet(unread, held: nil, state: state), "\(state)")
        }
        XCTAssertTrue(Sheet.getHelp(heldGone, held: "1.2.0").contains("isn't staged any more"))
        XCTAssertTrue(Sheet.getHelp(other, held: "1.2.0").contains("Download, decrypt and stage 1.3.0"))
    }

    @MainActor
    func test_the_sheets_get_waits_for_whatever_else_is_writing_the_app() {
        XCTAssertNil(AppDetailView.olderVersionBlockedReason(accountBusy: nil, busy: false))
        XCTAssertEqual(
            AppDetailView.olderVersionBlockedReason(accountBusy: AppStoreStore.accountBusyReason, busy: true),
            AppStoreStore.accountBusyReason
        )
        XCTAssertNotNil(AppDetailView.olderVersionBlockedReason(accountBusy: nil, busy: true))
    }

    // MARK: Getting a picked version

    @MainActor
    func test_a_get_of_a_version_names_it() {
        XCTAssertEqual(
            AppStoreStore.getArgs(app: "example", versionId: "800200"),
            ["--json", "--appstore-get", "--app", "example", "--version-id", "800200"]
        )
        XCTAssertEqual(
            AppStoreStore.getArgs(app: "example", allowUnverified: true, versionId: "800200"),
            ["--json", "--appstore-get", "--app", "example", "--version-id", "800200", "--allow-unverified"]
        )
        XCTAssertEqual(AppStoreStore.getArgs(app: "example"), ["--json", "--appstore-get", "--app", "example"])
    }

    private static let heldGet = """
    printf '%s\\n' '{"type":"appstore-stage","app":"example","stage":"download"}'
    printf '%s\\n' '{"type":"ipa-provided","app":"example","bundle_id":"com.example.app","version":"1.2.0","filename":"com.example.app_1.2.0.ipa","path":"/c/com.example.app_1.2.0.ipa","size_bytes":10}'
    printf '%s\\n' '{"type":"appstore-hold","app":"example","bundle_id":"com.example.app","version":"1.2.0","previous":null}'
    """

    @MainActor
    func test_a_get_of_a_version_stages_it_says_it_is_held_and_asks_for_the_app_list_again() async throws {
        let (runner, log) = try scriptRunner(Self.heldGet)
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        XCTAssertNil(store.lastHoldChange)

        XCTAssertTrue(store.startGet(app: "example", versionId: "800200"))
        await waitUntil { store.staged["example"] != nil }

        XCTAssertEqual(try argv(log).first, "--json --appstore-get --app example --version-id 800200")
        XCTAssertEqual(store.lastHoldChange?.app, "example")
        let staged = try XCTUnwrap(store.staged["example"])
        XCTAssertTrue(staged.chosen)
        XCTAssertEqual(staged.message, "✓ Staged 1.2.0 from the App Store. Builds use it until you get the latest.")
    }

    @MainActor
    func test_a_get_that_changed_no_hold_asks_for_nothing() async throws {
        let (runner, _) = try scriptRunner("""
        printf '%s\\n' '{"type":"ipa-provided","app":"example","bundle_id":"com.example.app","version":"1.4.0","filename":"com.example.app_1.4.0.ipa","path":"/c/x.ipa","size_bytes":10}'
        """)
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())

        store.startGet(app: "example")
        await waitUntil { store.staged["example"] != nil }

        XCTAssertNil(store.lastHoldChange)
        XCTAssertEqual(store.staged["example"]?.message, "✓ Staged 1.4.0 from the App Store")
    }

    @MainActor
    func test_a_hold_change_is_read_off_the_events() {
        let hold = BuildEvent.appStoreHold(AppStoreHoldRecord(app: "x", bundleId: "b", version: nil, previous: "1.0"))
        XCTAssertTrue(AppStoreStore.holdChanged(in: [hold]))
        XCTAssertFalse(AppStoreStore.holdChanged(in: []))
    }

    /// "Use it anyway…" after an older version's decrypt couldn't be
    /// verified (older apps often can't be) gets that version again, not the
    /// latest.
    @MainActor
    func test_use_it_anyway_after_a_version_get_gets_that_version() async throws {
        let refused = #"{"type":"appstore-refused","app":"example","reason":"unverified"}"#
        let (runner, log) = try scriptRunner("""
        case "$*" in
          *--allow-unverified*) printf '%s\\n' '{"type":"ipa-provided","app":"example","bundle_id":"com.example.app","version":"1.2.0","filename":"f.ipa","path":"/c/f.ipa","size_bytes":1}' ;;
          *) printf '%s\\n' '\(refused)'; echo 'error: unverified' >&2; exit 20 ;;
        esac
        """)
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        let config = ConfigStore(runner: runner, notify: { _ in })

        store.startGet(app: "example", versionId: "800200")
        await waitUntil { store.unverifiedRetry["example"] != nil }
        XCTAssertEqual(store.unverifiedRetry["example"], .version("800200"))

        XCTAssertTrue(store.useUnverified(app: "example", config: config))
        await waitUntil { store.staged["example"] != nil }

        // Then the version check every staged get ends with.
        XCTAssertEqual(try argv(log).filter { $0.contains("--appstore-get") }, [
            "--json --appstore-get --app example --version-id 800200",
            "--json --appstore-get --app example --version-id 800200 --allow-unverified",
        ])
    }

    // MARK: The row

    private static func version(
        store: String?, cached: String?, held: String?, update: Bool = false
    ) -> AppStoreVersionRecord {
        AppStoreVersionRecord(
            app: "example", bundleId: "com.example.app", storeVersion: store, cachedVersion: cached,
            updateAvailable: update, error: store == nil ? "offline" : nil, heldVersion: held
        )
    }

    @MainActor
    func test_a_held_app_says_so_and_never_update_available() {
        typealias Row = AppStoreSourceRow
        XCTAssertEqual(Row.versionLine(Self.version(store: "1.4.0", cached: "1.2.0", held: "1.2.0")),
                       "Held at 1.2.0 · newest 1.4.0")
        XCTAssertEqual(Row.versionLine(Self.version(store: "1.2.0", cached: "1.2.0", held: "1.2.0")),
                       "Held at 1.2.0 · the App Store's newest")
        XCTAssertEqual(Row.versionLine(Self.version(store: "1.4.0", cached: nil, held: "1.2.0")),
                       "Held at 1.2.0 (not staged) · newest 1.4.0")
        XCTAssertEqual(Row.versionLine(Self.version(store: nil, cached: "1.2.0", held: "1.2.0")),
                       "Held at 1.2.0 · couldn't check the App Store: offline")
        XCTAssertEqual(Row.versionLine(Self.version(store: "1.4.0", cached: "1.2.0", held: nil, update: true)),
                       "App Store 1.4.0 · cached 1.2.0 — update available")
    }

    /// Held, the button is Get latest whatever is staged: it is how the hold
    /// is let go. Not held, the latest staged is Get Again as before.
    @MainActor
    func test_a_held_app_offers_get_latest_even_when_its_version_is_the_newest() {
        typealias Row = AppStoreSourceRow
        XCTAssertEqual(Row.primaryAction(license: nil, version: Self.version(store: "1.2.0", cached: "1.2.0", held: "1.2.0")),
                       .getLatest)
        XCTAssertEqual(Row.primaryAction(license: nil, version: Self.version(store: "1.2.0", cached: "1.2.0", held: nil)),
                       .getAgain)
        XCTAssertTrue(Row.getLatestHelp(held: "1.2.0").contains("stop holding this app at 1.2.0"))
        XCTAssertFalse(Row.getLatestHelp(held: nil).contains("holding"))
    }

    @MainActor
    func test_older_versions_are_offered_only_when_a_get_could_start() {
        typealias Row = AppStoreSourceRow
        let need = AppStoreStore.LicenseNeed(price: 0, formattedPrice: "Free", trackId: 1)
        XCTAssertTrue(Row.offersOlderVersions(license: nil, isGetting: false, isClaiming: false))
        XCTAssertFalse(Row.offersOlderVersions(license: need, isGetting: false, isClaiming: false))
        XCTAssertFalse(Row.offersOlderVersions(license: nil, isGetting: true, isClaiming: false))
        XCTAssertFalse(Row.offersOlderVersions(license: nil, isGetting: false, isClaiming: true))
        XCTAssertEqual(Row.olderVersionsButton, "Get an older version…")
    }
}
