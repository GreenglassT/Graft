import Foundation
import Combine

private typealias Flag = CLIContract.Flag

@MainActor
final class CanisterStore: ObservableObject {
    @Published private(set) var results: [CanisterPackage] = []
    @Published private(set) var hasSearched: Bool = false
    @Published private(set) var isSearching: Bool = false
    @Published private(set) var isFetching: Bool = false
    @Published private(set) var fetchResult: String?
    @Published var lastError: String?

    let runner: CLIRunner

    init(runner: CLIRunner) {
        self.runner = runner
    }

    func search(query: String, argsOverride: [String]? = nil) async throws -> [CanisterPackage] {
        let trimmed = query.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else {
            results = []
            hasSearched = false
            return []
        }
        isSearching = true
        defer { isSearching = false }
        let args = argsOverride ?? [Flag.json, Flag.search, trimmed]
        let collected = try await runner.runCollecting(args, failing: "Search") { event in
            if case let .canisterResult(pkg) = event { return pkg }
            return nil
        }
        self.results = collected
        self.hasSearched = true
        return collected
    }

    /// `--fetch`'s arguments: with `repository`, the search result's own
    /// Canister repository, the only one the fetch takes it from (2.3.1 A1).
    static func fetchArguments(_ target: String, repository: String?) -> [String] {
        [Flag.json, Flag.fetch, target]
            + (repository.map { $0.isEmpty ? [] : [Flag.fetchRepository, $0] } ?? [])
    }

    func fetch(_ target: String, repository: String? = nil, argsOverride: [String]? = nil) async throws {
        isFetching = true
        fetchResult = "Fetching \(target)…"
        let args = argsOverride ?? Self.fetchArguments(target, repository: repository)
        let events: [BuildEvent]
        do {
            events = try await runner.runEvents(args, failing: "Fetch")
        } catch {
            // Every failure path clears both flags: a launch that never
            // started and a non-zero exit leave the same "not fetching, no
            // result" state behind.
            isFetching = false
            fetchResult = nil
            throw error
        }
        isFetching = false
        for event in events {
            guard case let .canisterFetchEnd(outcome) = event else { continue }
            switch outcome {
            case .success(let filename, let sizeBytes, _):
                fetchResult = "Fetched \(filename) (\(sizeBytes / 1024) KB)"
            case .failure(let error):
                fetchResult = nil
                throw CLIError.reported(error)
            }
        }
    }
}
