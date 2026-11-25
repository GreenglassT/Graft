import Foundation
import Combine

private typealias Flag = CLIContract.Flag

@MainActor
final class BuildsStore: ObservableObject {
    @Published private(set) var builds: [BuildRecord] = []
    @Published var lastError: String?

    let runner: CLIRunner

    init(runner: CLIRunner) {
        self.runner = runner
    }

    func load(appFilter: String? = nil, limit: Int = 50, argsOverride: [String]? = nil) async throws -> [BuildRecord] {
        var args = argsOverride ?? [Flag.json, Flag.builds, Flag.buildsLimit, String(limit)]
        if argsOverride == nil, let appFilter {
            args.append(contentsOf: [Flag.buildsApp, appFilter])
        }
        let collected = try await runner.runCollecting(args, failing: "Load builds") { event in
            if case let .buildRecord(record) = event { return record }
            return nil
        }
        self.builds = collected
        return collected
    }

    func reveal(_ buildId: Int) async {
        _ = try? await runner.runOnce(arguments: [Flag.revealBuild, String(buildId)])
    }

    // MARK: Clean up old builds

    /// What the last "Clean up old builds…" came to.
    enum PruneResult: Equatable {
        /// Every archive it meant to move went to the Trash.
        case moved(count: Int, bytes: Int)
        /// Some went; these did not, each as "<file>: <why>".
        case partly(moved: Int, failures: [String])
        /// The sweep did not run at all — the CLI's own sentence.
        case failed(String)
    }

    @Published private(set) var pruneResult: PruneResult?
    @Published private(set) var isPruning = false

    func dismissPruneResult() { pruneResult = nil }

    /// What a clean-up would move, without moving anything: `--prune-builds
    /// --dry-run`. No `--keep`, so the CLI uses config.yaml's `archive_keep` —
    /// the number every build already prunes to — and the summary carries it
    /// back for the confirmation to state.
    func previewPrune() async throws -> PruneSummary {
        let summary = try await runner.run(
            [Flag.json, Flag.pruneBuilds, Flag.dryRun], failing: "Check old builds"
        ) { event -> PruneSummary? in
            if case let .pruneEnd(summary) = event { return summary }
            return nil
        }
        guard let summary else { throw CLIError.reported("graft didn't say what it would clean up") }
        return summary
    }

    /// Move the old archives to the Trash, then re-read the list.
    ///
    /// `runOnce`, not `runEvents`: an archive that would not move makes the CLI
    /// exit 1 *after* the events saying which one and why (`log` lines sourced
    /// `retention`), and the ones that did move still moved. The list is
    /// re-read whenever anything moved or failed to: a moved archive's row
    /// lost its archive, and a failed move may have touched its row too (an
    /// older CLI cleared `ipa_path` before trying), so with no reload a row
    /// went on offering Re-install and Reveal for a path the history no longer
    /// had.
    func prune() async {
        guard !isPruning else { return }
        isPruning = true
        defer { isPruning = false }
        let result: CLIRunner.RunResult
        do {
            result = try await runner.runOnce(arguments: [Flag.json, Flag.pruneBuilds])
        } catch {
            pruneResult = .failed(error.localizedDescription)
            return
        }
        let events = result.stdout.buildEvents()
        let summary = events.lazy.compactMap { event -> PruneSummary? in
            if case let .pruneEnd(summary) = event { return summary }
            return nil
        }.first
        guard let summary else {
            pruneResult = .failed(CLIError.exited(
                doing: "Clean up old builds", code: result.exitCode, stderr: result.stderr
            ).localizedDescription)
            return
        }
        let failures = events.compactMap { event -> String? in
            if case let .log(source, line) = event, source == CLIContract.retentionLogSource {
                return line
            }
            return nil
        }
        pruneResult = failures.isEmpty && summary.failed == 0
            ? .moved(count: summary.removed, bytes: summary.bytesRemoved)
            : .partly(moved: summary.removed, failures: failures)
        if summary.removed > 0 || summary.failed > 0 || !failures.isEmpty {
            do { _ = try await load() } catch { lastError = error.localizedDescription }
        }
    }
}
