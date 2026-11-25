import Foundation
import Combine

private typealias Flag = CLIContract.Flag

@MainActor
final class RepoStore: ObservableObject, CLIStore {
    @Published private(set) var repos: [RepoRecord] = []
    @Published private(set) var packages: [RepoPackage] = []
    @Published private(set) var packagesRepoURL: String?
    @Published private(set) var isLoading: Bool = false
    @Published private(set) var isRefreshing: Bool = false
    @Published private(set) var isAdding: Bool = false
    @Published var fetchResult: String?
    @Published var lastError: String?

    let runner: CLIRunner
    private let cliArgs: [String]

    init(runner: CLIRunner, cliArgs: [String] = [Flag.json, Flag.repos]) {
        self.runner = runner
        self.cliArgs = cliArgs
    }

    /// This store's label for a CLI failure. Kept as the type name because
    /// that is what the messages already said; every call here surfaces the
    /// CLI's own stderr when there is any, so it only shows on a silent
    /// non-zero exit.
    private static let label = "RepoStore"

    @discardableResult
    func loadRepos(argsOverride: [String]? = nil) async throws -> [RepoRecord] {
        // Note: we deliberately don't set `isLoading` here — that flag drives the
        // packages-pane spinner. loadRepos has no sidebar spinner today; mutating
        // isLoading would falsely flash the packages "Loading…" placeholder.
        let events = try await runner.runEvents(
            argsOverride ?? cliArgs, failing: Self.label
        )
        let sawEnd = events.contains { if case .reposEnd = $0 { return true } else { return false } }
        guard sawEnd else {
            throw CLIError.reported("repo list incomplete — the CLI exited without finishing")
        }
        let collected = events.compactMap { event -> RepoRecord? in
            if case let .repo(record) = event { return record }
            return nil
        }
        self.repos = collected
        return collected
    }

    /// `CLIStore`'s post-mutation refresh: for this store, the repo list.
    func reload() async throws {
        _ = try await loadRepos()
    }

    // add/remove go through `CLIStore.mutate` for the rule it encodes: the
    // write already landed in config.yaml, so a failed refresh afterward is
    // "it worked, the list is stale" and belongs in `lastError` — not a throw
    // that makes ReposView tell the user the repo wasn't added. They used to
    // rethrow, which is the exact bug `mutate` exists to prevent; ReposView
    // renders `lastError`, so the message still reaches the user either way.

    func addRepo(url: String, argsOverride: [String]? = nil) async throws {
        isAdding = true
        defer { isAdding = false }
        let trimmed = url.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return }
        let added = try await mutate(
            argsOverride ?? [Flag.json, Flag.addRepo, trimmed],
            failing: Self.label,
            succeeded: "Repo added"
        ) { event in
            if case let .repoAdded(record) = event { return record }
            return nil
        }
        if let added {
            fetchResult = "Added \(added.name) — \(added.packageCount) packages"
        }
    }

    func removeRepo(url: String, argsOverride: [String]? = nil) async throws {
        _ = try await mutate(
            argsOverride ?? [Flag.json, Flag.removeRepo, url],
            failing: Self.label,
            succeeded: "Repo removed"
        ) { event -> String? in
            if case let .repoRemoved(removed) = event { return removed }
            return nil
        }
        // After the reload, not before: the packages pane should keep showing
        // the repo's contents for as long as the repo is still in `repos`, and
        // both assignments land in the same actor step once `mutate` returns.
        if packagesRepoURL == url {
            packages = []
            packagesRepoURL = nil
        }
    }

    @discardableResult
    func loadPackages(
        repoURL: String, refresh: Bool, argsOverride: [String]? = nil
    ) async throws -> [RepoPackage] {
        if refresh { isRefreshing = true } else { isLoading = true }
        defer { if refresh { isRefreshing = false } else { isLoading = false } }
        var args = [Flag.json, Flag.repoPackages, repoURL]
        if refresh { args.append(Flag.refresh) }
        // streamEvents, not runEvents: a large repo's package events easily
        // exceed the 64 KB stdout pipe buffer, and it checks for cancellation
        // before the exit status so a replaced `.task(id:)` unwinds without
        // leaving an error banner behind.
        let events = try await runner.streamEvents(argsOverride ?? args, failing: Self.label)
        var collectedPkgs: [RepoPackage] = []
        var sawEnd = false
        for event in events {
            switch event {
            case let .repoPackage(pkg):
                collectedPkgs.append(pkg)
            case .repoPackagesEnd:
                sawEnd = true
            default:
                break
            }
        }
        guard sawEnd else {
            throw CLIError.reported("package list incomplete — the CLI exited without finishing")
        }
        self.packages = collectedPkgs.sorted { $0.name.lowercased() < $1.name.lowercased() }
        self.packagesRepoURL = repoURL
        return self.packages
    }

    func download(_ package: RepoPackage, argsOverride: [String]? = nil) async throws {
        fetchResult = "Downloading \(package.name)…"
        var args = [Flag.json, Flag.fetch, package.downloadURL]
        if let sha = package.sha256, !sha.isEmpty {
            args += [Flag.fetchSha, sha]
        }
        let events: [BuildEvent]
        do {
            events = try await runner.runEvents(argsOverride ?? args, failing: Self.label)
        } catch {
            fetchResult = nil
            throw error
        }
        var sawEnd = false
        for event in events {
            guard case let .canisterFetchEnd(outcome) = event else { continue }
            sawEnd = true
            switch outcome {
            case let .success(filename, sizeBytes, _):
                let size = fileSize(sizeBytes)
                fetchResult = "Downloaded \(filename) (\(size))"
            case let .failure(error):
                fetchResult = nil
                throw CLIError.reported(error)
            }
        }
        if !sawEnd {
            fetchResult = nil
        }
    }
}
