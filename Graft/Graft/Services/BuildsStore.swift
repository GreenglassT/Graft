import Foundation
import Observation

private typealias Flag = CLIContract.Flag

@MainActor
@Observable
final class BuildsStore {
    private(set) var builds: [BuildRecord] = []
    var lastError: String?

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
}
