import XCTest
@testable import Graft

final class CanisterStoreTests: XCTestCase {
    @MainActor
    func test_search_parses_canister_result_events() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let script = #"""
        printf '{"type":"canister-result","package_id":"com.example.ctweak","name":"C Tweak","version":"1.0","description":"x","author":"a","section":"Tweaks","repository_id":"havoc","price":"Free","size_bytes":100,"depiction":null}\n{"type":"canister-result","package_id":"com.example.second","name":"Two","version":"2.0","description":"y","author":"b","section":"Tweaks","repository_id":"chariz","price":"Free","size_bytes":200,"depiction":null}\n{"type":"canister-search-end","count":2}\n'
        """#
        let store = CanisterStore(runner: runner)
        let results = try await store.search(query: "x", argsOverride: ["-c", script])
        XCTAssertEqual(results.count, 2)
        XCTAssertEqual(results[0].packageId, "com.example.ctweak")
        XCTAssertEqual(results[1].repositoryId, "chariz")
        XCTAssertTrue(store.hasSearched)
    }

    /// A fetch from a search result takes that result's repository only, so
    /// another repository listing the same id at a higher version can't win
    /// it (2.3.1 A1). A fetch with no repository says none.
    @MainActor
    func test_a_fetch_from_a_result_names_its_repository() {
        XCTAssertEqual(CanisterStore.fetchArguments("com.example.ctweak", repository: "examplerepo"),
                       ["--json", "--fetch", "com.example.ctweak", "--fetch-repository", "examplerepo"])
        XCTAssertEqual(CanisterStore.fetchArguments("com.example.ctweak", repository: nil),
                       ["--json", "--fetch", "com.example.ctweak"])
        XCTAssertEqual(CanisterStore.fetchArguments("com.example.ctweak", repository: ""),
                       ["--json", "--fetch", "com.example.ctweak"])
    }
}
