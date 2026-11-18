import SwiftUI

private typealias Flag = CLIContract.Flag

struct BuildLogView: View {
    let build: BuildRecord
    let runner: CLIRunner
    let artwork: ArtworkStore
    let onClose: () -> Void

    @State private var logLines: [LogLine] = []
    @State private var loading = true
    @State private var loadError: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            header
            Divider().background(Palette.line)
            content
            HStack {
                Spacer()
                Button("Close") { onClose() }
                    .keyboardShortcut(.cancelAction)
            }
        }
        .padding(20)
        .frame(width: 720, height: 500)
        .background(Palette.bg)
        .task { await loadLog() }
    }

    private var header: some View {
        HStack(spacing: 12) {
            // `build.bundleId`, not a lookup by `build.app`: the build row
            // records the id the app was configured with, which is the same key
            // `--artwork` caches under. A build whose app has since been removed
            // from config has nothing cached and falls back to the tile, which
            // is the right answer for a history that outlives the config.
            AppIcon(name: build.app, size: 36, image: artwork.image(for: build.bundleId))
            VStack(alignment: .leading, spacing: 2) {
                Text("\(build.app) — build log")
                    .font(AppFont.bodyMedium)
                    .foregroundStyle(Palette.text)
                Text("\(build.bundleId) · \(build.version) · \(build.timestamp)")
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
            }
            Spacer()
            statusBadge
        }
    }

    private var statusBadge: some View {
        HStack(spacing: 6) {
            Circle().fill(build.success ? Palette.success : Palette.danger).frame(width: 7, height: 7)
            Text(build.success ? "Built" : "Failed").font(AppFont.label).labelCaps()
        }
        .foregroundStyle(Palette.textMuted)
    }

    @ViewBuilder
    private var content: some View {
        if loading {
            VStack(spacing: 8) {
                ProgressView().controlSize(.regular)
                Text("Loading log…").font(AppFont.body).foregroundStyle(Palette.textDim)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if let err = loadError {
            VStack(alignment: .leading, spacing: 8) {
                Text("Couldn't load log")
                    .font(AppFont.bodyMedium)
                    .foregroundStyle(Palette.danger)
                Text(err)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textMuted)
                    .textSelection(.enabled)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .padding(12)
            .cardStyle()
        } else {
            BuildLogPanel(lines: logLines)
                .frame(maxHeight: .infinity)
        }
    }

    @MainActor
    private func loadLog() async {
        defer { loading = false }
        let result: CLIRunner.RunResult
        do {
            result = try await runner.runOnce(
                arguments: [Flag.json, Flag.buildLog, String(build.id)]
            )
        } catch {
            loadError = error.localizedDescription
            return
        }
        if result.exitCode != 0 {
            // `failureSummary`, not raw stderr: this is a `runOnce` result
            // rather than a thrown `CLIError`, so it never passed through
            // `errorDescription` and was the last place in the app still
            // pasting Rich's 80-column output into a SwiftUI `Text`.
            loadError = result.failureSummary
            return
        }
        logLines = result.stdout.buildEvents().flatMap { Self.lines(for: $0) }
    }

    /// One build-stream event as log lines — for **all three** panels that
    /// render a build stream, not just this one.
    ///
    /// Switches over `BuildProgress`, not `BuildEvent`, and that is the fix
    /// rather than a tidy-up. As a `BuildEvent` switch this ended in
    /// `default: return []`, so when `encrypted-binaries` was added — with a
    /// docstring saying the new `BuildProgress` case would force *every* panel
    /// that ships an IPA to a phone to decide about it — this panel compiled
    /// unchanged and was never asked. It dropped the finding silently.
    ///
    /// That mattered more here than anywhere else: `ConfigStore.encryptedBinaries`
    /// is session-scoped because the CLI records nothing about it, so the
    /// persisted build log replayed by this sheet is the *only* place the
    /// warning can survive a relaunch. Routing through `progress` means the
    /// next case added there is a compile error here too.
    ///
    /// `static` and internal for that reason and one more: `AppDetailView` and
    /// `ReinstallSheet` held their own copy of this switch, and Swift's
    /// exhaustiveness guaranteed each copy was *non-empty* while guaranteeing
    /// nothing about the three agreeing. They had already drifted — a failed
    /// build read `✗ <error> (12.4s)` replayed and `✗ <error>` live, the same
    /// event punctuated two ways — after costing a dropped finding once and a
    /// three-way hand edit for `.liveContainerChoice` a second time. The
    /// elapsed time is kept on both outcomes: `✓ done in 84.3s` already reports
    /// it, and a failure is the run whose duration says the most.
    ///
    /// - Parameters:
    ///   - startVerb: how the first line names what is running. The one
    ///     difference between the three call sites that is about the *command*
    ///     rather than about formatting — `--reinstall-build` re-ships an
    ///     archived IPA and calling that "build start" would be a lie. Stage
    ///     text, log lines and the outcome are the caller's business nowhere:
    ///     a panel that wants to phrase those differently is drift.
    static func lines(for event: BuildEvent, startVerb: String = "build start") -> [LogLine] {
        // Everything outside the build stream — `build-record`, the per-file
        // `build-pruned` rows — is no panel's business.
        guard let progress = event.progress else { return [] }
        switch progress {
        case .start(let appName, let delivery):
            return [LogLine(
                kind: .arrow,
                text: "→ \(startVerb): \(appName) (\(delivery.buildLogLabel))"
            )]
        case .stage(let name, let status):
            switch status {
            case "start": return [LogLine(kind: .arrow, text: "→ \(name)")]
            case "end":   return [LogLine(kind: .ok,    text: "  ✓ \(name)")]
            default:      return []
            }
        case .log(let source, let line):
            return [LogLine(kind: .dim, text: "  [\(source)] \(line)")]
        case .pruned(let summary):
            // The persisted log is teed from the same stream, so a build that
            // swept archives has these events on disk too.
            return summary.logSummary.map { [LogLine(kind: .dim, text: $0)] } ?? []
        case .encrypted(let record):
            // The line that contradicts the `✓ done` a few rows under it. A
            // build that shipped an encrypted binary succeeded, so this
            // transcript is otherwise entirely green.
            //
            // `--reinstall-build` runs no decryption check, so the re-install
            // sheet cannot receive this today. It renders it anyway rather
            // than special-casing it away: if that command ever verifies what
            // it is about to push, swallowing the answer is the defect this
            // case exists to close.
            return [LogLine(kind: .warn, text: record.logLine)]
        case .liveContainerChoice(let record):
            // Which container to import from. The re-install sheet needs this
            // more than the build panel does, not less — it re-ships an old
            // artifact to whatever is installed *now* — and by the time
            // someone opens a past build's log to work out why the tweak never
            // appeared, this sheet is the only place left that says.
            return [LogLine(kind: .warn, text: record.logLine)]
        case .finished(let ok, let error, let elapsed):
            let elapsedTxt = elapsed.map { String(format: "%.1fs", $0) } ?? "?"
            if ok {
                return [LogLine(kind: .ok, text: "✓ done in \(elapsedTxt)")]
            } else {
                return [LogLine(kind: .warn, text: "✗ \(error ?? "failed") (\(elapsedTxt))")]
            }
        }
    }
}
