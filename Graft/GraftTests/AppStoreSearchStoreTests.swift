import XCTest
@testable import Graft

/// Add App's Search: typing, debouncing, the store, and the six states. Each
/// state is reached through a script CLI that prints what the real one prints.
@MainActor
final class AppStoreSearchStoreTests: XCTestCase {
    private static func result(_ n: Int, price: String = "0", formatted: String = "Free") -> String {
        #"{"type":"appstore-search-result","track_id":10000000\#(n),"bundle_id":"com.example.app\#(n)","name":"Example \#(n)","developer":"Dev \#(n)","version":"\#(n).0","price":\#(price),"formatted_price":"\#(formatted)","currency":"USD","artwork_url":null,"icon_path":null,"configured_as":null}"#
    }

    private static func end(_ term: String, count: Int, country: String = "US", note: String? = nil) -> String {
        let noted = note.map { #","note":"\#($0)""# } ?? ""
        return #"{"type":"appstore-search-end","term":"\#(term)","country":"\#(country)","count":\#(count)\#(noted)}"#
    }

    private func store(
        _ cli: ScriptCLI, region: String? = nil, debounce: Duration = .milliseconds(50)
    ) -> AppStoreSearchStore {
        AppStoreSearchStore(runner: cli.runner, debounce: debounce, region: { region })
    }

    /// Wait for the search to finish, failing if it never does.
    private func finished(_ search: AppStoreSearchStore, file: StaticString = #filePath, line: UInt = #line) async {
        await eventually({ !search.isSearching }, timeout: 10, "the search to finish", file: file, line: line)
    }

    // MARK: empty

    func test_fewer_than_two_characters_search_nothing() async throws {
        let cli = try ScriptCLI("printf '%s\\n' '\(Self.end("a", count: 0))'", test: self)
        let search = store(cli)

        search.update(term: "a")
        search.update(term: "  b ")
        try await Task.sleep(for: .milliseconds(300))

        XCTAssertEqual(search.state, .idle)
        XCTAssertFalse(search.isSearching)
        XCTAssertEqual(cli.argv(), [], "no CLI run for a term this short")
    }

    // MARK: debounce

    /// The keystrokes are apart in time, as typing is, but closer than the
    /// pause. Two `update`s in one synchronous run would pass without any
    /// debounce at all: the first task is canceled before it ever starts.
    func test_typing_quickly_searches_only_the_last_term() async throws {
        let cli = try ScriptCLI(
            "printf '%s\\n' '\(Self.result(1))' '\(Self.end("abc", count: 1))'", test: self
        )
        let search = store(cli, debounce: .seconds(1))

        search.update(term: "ab")
        try await Task.sleep(for: .milliseconds(150))
        search.update(term: "abc")
        await finished(search)

        XCTAssertEqual(cli.argv(), ["--json --appstore-search=abc --country US"])
    }

    /// The pause production uses, not one a test injected: each search is a
    /// request against Apple's per-Mac limit (review part 5, finding 28).
    func test_the_shipped_pause_is_at_least_half_a_second() {
        XCTAssertGreaterThanOrEqual(AppStoreSearchStore.defaultDebounce, .milliseconds(500))
        let production = AppStoreStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/true")), shell: NoAdminShell())
        XCTAssertGreaterThanOrEqual(production.search.debounce, .milliseconds(500))
    }

    // MARK: results

    func test_results_arrive_in_order_with_the_store_they_came_from() async throws {
        let cli = try ScriptCLI("""
        printf '%s\\n' '\(Self.result(1))' '\(Self.result(2, price: "4.99", formatted: "$4.99"))' '\(Self.end("example", count: 2, country: "GB"))'
        """, test: self)
        let search = store(cli)

        search.update(term: "example")
        await finished(search)

        guard case let .results(results, term, country) = search.state else {
            return XCTFail("\(search.state)")
        }
        XCTAssertEqual(results.map(\.bundleId), ["com.example.app1", "com.example.app2"])
        XCTAssertEqual(term, "example")
        XCTAssertEqual(country, "GB")
        XCTAssertEqual(results[1].acquisition, .paid(price: "$4.99"))
    }

    // MARK: no results

    func test_a_search_that_found_nothing_is_no_results_not_empty() async throws {
        let cli = try ScriptCLI("printf '%s\\n' '\(Self.end("zzzz", count: 0))'", test: self)
        let search = store(cli)

        search.update(term: "zzzz")
        await finished(search)

        XCTAssertEqual(search.state, .noResults(term: "zzzz", country: "US"))
    }

    /// The house defect (memory `empty-collection-is-not-a-load-state`): no
    /// results is a claim only the sentinel makes. A CLI that exits 0 without
    /// one (an older CLI, a truncated stream) is a failure, not "nothing found".
    func test_a_cli_that_ends_without_the_sentinel_is_an_error_not_no_results() async throws {
        let cli = try ScriptCLI("exit 0", test: self)
        let search = store(cli)

        search.update(term: "example")
        await finished(search)

        guard case .failed = search.state else { return XCTFail("\(search.state)") }
    }

    // MARK: offline, and Apple's limit

    func test_offline_is_its_own_state() async throws {
        let cli = try ScriptCLI("""
        printf '%s\\n' '{"type":"appstore-search-failed","term":"example","reason":"offline","note":null}'
        echo "error: couldn't reach the App Store to search" >&2
        exit 13
        """, test: self)
        let search = store(cli)

        search.update(term: "example")
        await finished(search)

        XCTAssertEqual(search.state, .offline)
    }

    func test_apples_rate_limit_is_its_own_state_in_the_clis_words() async throws {
        let cli = try ScriptCLI("""
        printf '%s\\n' '{"type":"appstore-search-failed","term":"example","reason":"rate-limited","note":null}'
        echo "error: \(AppStoreSearchRateLimited.message)" >&2
        exit 13
        """, test: self)
        let search = store(cli)

        search.update(term: "example")
        await finished(search)

        XCTAssertEqual(search.state, .rateLimited)
        XCTAssertEqual(AppStoreSearchForm.rateLimited,
                       "Apple is limiting searches from this Mac for a minute. Try again shortly.")
    }

    // MARK: error

    func test_any_other_failure_is_an_error_in_the_clis_words() async throws {
        let cli = try ScriptCLI("""
        printf '%s\\n' '{"type":"appstore-search-failed","term":"example","reason":"http"}'
        echo "error: the App Store search answered HTTP 500" >&2
        echo "  hint: Apple's search is having trouble; try again in a minute" >&2
        exit 13
        """, test: self)
        let search = store(cli)

        search.update(term: "example")
        await finished(search)

        guard case let .failed(message) = search.state else { return XCTFail("\(search.state)") }
        XCTAssertTrue(message.contains("HTTP 500"), message)
    }

    // MARK: canceling and superseding

    /// A search the sheet canceled (closed, or the term shortened) leaves no
    /// trace. Its killed CLI is not "the search failed".
    func test_a_cancelled_search_changes_nothing() async throws {
        let cli = try ScriptCLI("sleep 5; printf '%s\\n' '\(Self.end("slow", count: 0))'", test: self)
        let search = store(cli)

        search.update(term: "slow")
        await eventually({ cli.argv().count == 1 }, "premise: the CLI is running")
        search.reset()
        try await Task.sleep(for: .seconds(1))

        XCTAssertEqual(search.state, .idle)
        XCTAssertFalse(search.isSearching)
    }

    /// A newer term while a search's CLI is already running: the old run is
    /// killed, and its death writes nothing (review part 5, finding 22).
    /// Before, only a cancel during the pause and `reset()` were tested, and
    /// neither goes through the generation guard a running search needs.
    func test_a_newer_term_replaces_a_running_search_and_the_old_one_writes_nothing() async throws {
        let marker = FileManager.default.temporaryDirectory
            .appending(path: "old_search_finished_\(UUID().uuidString)")
        addTeardownBlock { try? FileManager.default.removeItem(at: marker) }
        let cli = try ScriptCLI("""
        case "$*" in
          *"--appstore-search=calc --country"*)
            sleep 2; touch '\(marker.path)'; printf '%s\\n' '\(Self.end("calc", count: 0))' ;;
          *) printf '%s\\n' '\(Self.result(1))' '\(Self.end("calcu", count: 1))' ;;
        esac
        """, test: self)
        let search = store(cli)

        search.update(term: "calc")
        await eventually({ cli.argv().count == 1 }, "premise: the first search's CLI is running")
        search.update(term: "calcu")
        await finished(search)

        guard case let .results(results, term, _) = search.state else {
            return XCTFail("the killed search wrote its death: \(search.state)")
        }
        XCTAssertEqual(term, "calcu")
        XCTAssertEqual(results.count, 1)
        XCTAssertEqual(cli.argv().count, 2)
        try await Task.sleep(for: .milliseconds(2500))
        XCTAssertFalse(FileManager.default.fileExists(atPath: marker.path), "the old CLI was killed, not left to run")
        XCTAssertEqual(search.state.isResults(term: "calcu"), true, "and it never wrote late")
    }

    /// A trailing space is not a new search: nothing is killed, nothing asked
    /// again (review part 5, finding 9).
    func test_a_whitespace_only_edit_searches_nothing_new() async throws {
        let cli = try ScriptCLI("""
        sleep 0.5; printf '%s\\n' '\(Self.result(1))' '\(Self.end("notes", count: 1))'
        """, test: self)
        let search = store(cli)

        search.update(term: "notes")
        await eventually({ cli.argv().count == 1 }, "premise: the CLI is running")
        search.update(term: "notes ")
        search.update(term: " notes")
        await finished(search)
        search.update(term: "notes  ")
        try await Task.sleep(for: .milliseconds(200))

        XCTAssertEqual(cli.argv().count, 1, "\(cli.argv())")
        XCTAssertTrue(search.state.isResults(term: "notes"), "\(search.state)")
        XCTAssertFalse(search.isSearching)
    }

    // MARK: answered from memory

    /// A term this sheet already searched in this store is answered without a
    /// request: backspacing and retyping is most of what typing is.
    func test_a_term_already_searched_is_answered_from_the_cache() async throws {
        let cli = try ScriptCLI("""
        case "$*" in
          *"=ab "*) printf '%s\\n' '\(Self.result(1))' '\(Self.end("ab", count: 1))' ;;
          *) printf '%s\\n' '\(Self.end("abc", count: 0))' ;;
        esac
        """, test: self)
        let search = store(cli)

        search.update(term: "ab")
        await finished(search)
        search.update(term: "abc")
        await finished(search)
        search.update(term: "ab")

        XCTAssertFalse(search.isSearching, "answered at once")
        XCTAssertTrue(search.state.isResults(term: "ab"), "\(search.state)")
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(cli.argv().count, 2, "\(cli.argv())")
    }

    func test_a_failure_is_not_remembered_and_try_again_asks_again() async throws {
        let cli = try ScriptCLI("""
        printf '%s\\n' '{"type":"appstore-search-failed","term":"example","reason":"offline"}'
        exit 13
        """, test: self)
        let search = store(cli)

        search.update(term: "example")
        await finished(search)
        search.retry(term: "example")
        await eventually({ cli.argv().count == 2 }, "Try Again's search")
        await finished(search)
        search.update(term: "examples")
        await finished(search)
        search.update(term: "example")
        await eventually({ cli.argv().count == 4 }, "the failed term, typed again, asks again")
    }

    func test_closing_the_sheet_forgets_what_was_searched() async throws {
        let cli = try ScriptCLI("printf '%s\\n' '\(Self.end("ab", count: 0))'", test: self)
        let search = store(cli)

        search.update(term: "ab")
        await finished(search)
        search.reset()
        search.update(term: "ab")
        await finished(search)

        XCTAssertEqual(cli.argv().count, 2, "an add may have changed which results say Added")
    }

    // MARK: the store asked

    func test_the_store_starts_at_the_one_this_macs_region_shops_in() throws {
        let cli = try ScriptCLI("exit 0", test: self)
        XCTAssertEqual(store(cli, region: "GB").country, "GB")
        XCTAssertEqual(store(cli, region: "gb").country, "GB")
        XCTAssertEqual(store(cli, region: "PR").country, "US", "Puerto Rico uses the US store")
        XCTAssertEqual(store(cli, region: "RE").country, "FR", "Réunion uses the French one")
        XCTAssertEqual(store(cli, region: "IR").country, "US", "no store at all: the US one")
        XCTAssertEqual(store(cli, region: "419").country, "US", "Latin America: no two-letter code")
        XCTAssertEqual(store(cli, region: nil).country, "US")
    }

    func test_a_region_with_no_store_says_so_and_a_picked_store_does_not() throws {
        let cli = try ScriptCLI("exit 0", test: self)
        let noStore = store(cli, region: "IR")
        XCTAssertEqual(noStore.note, Storefronts.noStoreNote)
        XCTAssertEqual(noStore.note, "Searching the US store: your region has no App Store.")
        XCTAssertNil(store(cli, region: "PR").note, "a territory's store is simply its store")
        XCTAssertNil(store(cli, region: "GB").note)

        noStore.select(country: "GB", term: "")
        XCTAssertNil(noStore.note)
    }

    func test_the_argv_attaches_the_term_and_adds_the_country() {
        XCTAssertEqual(
            AppStoreSearchStore.args(term: "-x", country: "GB"),
            ["--json", "--appstore-search=-x", "--country", "GB"]
        )
        XCTAssertEqual(AppStoreSearchStore.args(term: "ab", country: nil), ["--json", "--appstore-search=ab"])
    }

    func test_the_store_reaches_the_cli_mapped() async throws {
        let cli = try ScriptCLI("printf '%s\\n' '\(Self.end("example", count: 0, country: "US"))'", test: self)
        let search = store(cli, region: "PR")

        search.update(term: "example")
        await finished(search)

        XCTAssertEqual(cli.argv(), ["--json --appstore-search=example --country US"])
    }

    func test_picking_another_store_searches_the_term_there() async throws {
        let cli = try ScriptCLI("""
        case "$*" in
          *"--country GB"*) printf '%s\\n' '\(Self.end("example", count: 0, country: "GB"))' ;;
          *) printf '%s\\n' '\(Self.end("example", count: 0, country: "DE"))' ;;
        esac
        """, test: self)
        let search = store(cli, region: "DE")

        search.update(term: "example")
        await finished(search)
        search.select(country: "UK", term: "example")
        await finished(search)

        XCTAssertEqual(search.country, "GB", "UK is GB")
        XCTAssertEqual(search.state, .noResults(term: "example", country: "GB"))
        XCTAssertEqual(cli.argv().last, "--json --appstore-search=example --country GB")
        search.select(country: "ZZ", term: "example")
        XCTAssertEqual(search.country, "GB", "no store, no change")
    }

    func test_the_clis_note_is_shown() async throws {
        let cli = try ScriptCLI(
            "printf '%s\\n' '\(Self.end("example", count: 0, note: "A note from the CLI"))'", test: self
        )
        let search = store(cli, region: "GB")

        search.update(term: "example")
        await finished(search)

        XCTAssertEqual(search.note, "A note from the CLI")
    }
}

private extension AppStoreSearchStore.State {
    func isResults(term wanted: String) -> Bool {
        if case let .results(_, term, _) = self { return term == wanted }
        return false
    }
}

/// The wait every hosted Swift test leans on (`eventually`, ScriptCLI.swift).
/// The combined fix check's critic: its XCTFail on timeout, review part 5's
/// #5, had no test, so a revert to "return either way" would have let every
/// wait whose condition never held pass silently.
extension AppStoreSearchStoreTests {
    @MainActor
    func test_eventually_fails_the_test_when_its_condition_never_holds() async {
        let options = XCTExpectedFailure.Options()
        options.issueMatcher = { $0.compactDescription.contains("eventually: the condition never held") }
        XCTExpectFailure("a wait that times out is a failure", options: options)
        await eventually({ false }, timeout: 0.1, "a condition that never holds")
    }

    @MainActor
    func test_eventually_returns_once_its_condition_holds() async {
        var ticks = 0
        await eventually({ ticks += 1; return ticks > 3 }, timeout: 5, "a few polls")
        XCTAssertGreaterThan(ticks, 3)
    }
}
