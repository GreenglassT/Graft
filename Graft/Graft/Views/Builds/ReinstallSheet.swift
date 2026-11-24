import SwiftUI

private typealias Flag = CLIContract.Flag

struct ReinstallSheet: View {
    let build: BuildRecord
    let runner: CLIRunner
    /// Only so the delivery badges can be refreshed when this finishes.
    /// `--reinstall-build` pushes to the phone and records a delivery exactly
    /// like a build does, and nothing else in the Builds section holds the
    /// store that carries those badges.
    let config: ConfigStore
    let artwork: ArtworkStore
    /// Held while `--reinstall-build` runs (`reinstall`); a test passes its own.
    var hold: TerminationHold = .shared
    let onClose: () -> Void

    @State private var state: BuildState = .idle
    @State private var logLines: [LogLine] = []
    @State private var started = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            header
            Divider().background(Palette.line)
            // The pill can only say "Failed"; the message that says what to do
            // about it needs a line of its own. No dismiss — there is nothing
            // to go back to inside a sheet whose one job just failed.
            if case let .failed(message) = state {
                ErrorBanner(message: message)
            }
            BuildLogPanel(lines: logLines)
                .frame(minHeight: 260)
            HStack {
                Spacer()
                Button(closeLabel) { onClose() }
                    .keyboardShortcut(.cancelAction)
                    .disabled(isRunning)
            }
        }
        .padding(20)
        .frame(width: 640, height: 460)
        .background(Palette.bg)
        .task {
            if !started {
                started = true
                await runReinstall()
            }
        }
    }

    private var header: some View {
        HStack(spacing: 12) {
            // Keyed on the build's own bundle id — see the same call in
            // `BuildLogView` for why that is the right key here.
            AppIcon(name: build.app, size: 36, image: artwork.image(for: build.bundleId))
            VStack(alignment: .leading, spacing: 2) {
                Text("Re-install \(build.app)")
                    .font(AppFont.bodyMedium)
                    .foregroundStyle(Palette.text)
                Text("\(build.bundleId) · \(build.version)")
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
            }
            Spacer()
            statusPill
        }
    }

    @ViewBuilder
    private var statusPill: some View {
        switch state {
        case .idle:
            EmptyView()
        case .running(let stage):
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text(stage).font(AppFont.label).labelCaps()
            }
            .foregroundStyle(Palette.textMuted)
        case .ok:
            HStack(spacing: 6) {
                Circle().fill(Palette.success).frame(width: 7, height: 7)
                Text("Done").font(AppFont.label).labelCaps()
            }
            .foregroundStyle(Palette.success)
        case .failed:
            HStack(spacing: 6) {
                Circle().fill(Palette.danger).frame(width: 7, height: 7)
                Text("Failed").font(AppFont.label).labelCaps()
            }
            .foregroundStyle(Palette.danger)
        case .cancelled:
            // Deliberately not folded into `.failed`. A re-install has no Stop
            // button — it has no `store.launchBuild` handle; this sheet owns
            // its own Task — so the one way here is the CLI reporting a stop
            // it was sent from elsewhere (C-STOP's `build-end` with
            // `stopped: true`), which is not a failure either.
            HStack(spacing: 6) {
                Circle().fill(Palette.warn).frame(width: 7, height: 7)
                Text("Stopped").font(AppFont.label).labelCaps()
            }
            .foregroundStyle(Palette.warn)
        }
    }

    private var isRunning: Bool {
        if case .running = state { return true }
        return false
    }

    private var closeLabel: String {
        switch state {
        case .running: return "Running…"
        case .ok: return "Done"
        default: return "Close"
        }
    }

    /// `--reinstall-build` for the build with this `id`, with sudden and
    /// automatic termination held off from start to end, however it ends
    /// (the try14 delta audit, D14-APP-2). It pushes to the phone and records
    /// a delivery, as a build does, and a Graft that macOS ended halfway
    /// would orphan the CLI mid-push (`TerminationHold`). Its only Stop is
    /// the sheet's task being canceled, so Quit Anyway cancels its own
    /// (`TerminationHold.holding`).
    @MainActor
    static func reinstall(
        build id: Int, runner: CLIRunner, hold: TerminationHold,
        onEvent: @escaping @MainActor (BuildEvent) -> Void
    ) async throws -> CLIRunner.RunResult {
        try await hold.holding(TerminationHold.reinstalling) {
            try await runner.stream(arguments: [Flag.json, Flag.reinstallBuild, String(id)], onEvent: onEvent)
        }
    }

    @MainActor
    private func runReinstall() async {
        state = .running(stage: "starting")
        do {
            let result = try await Self.reinstall(build: build.id, runner: runner, hold: hold) { event in
                handle(event: event)
            }
            // A stop the CLI reported (C-STOP) is not a failure, whatever the
            // exit status: read off the collected events, which — unlike the
            // callback's — are all in by now.
            if BuildProgress.stopReported(in: result.events) {
                state = .cancelled
            } else if result.exitCode != 0 {
                report(unexplained: result)
            }
        } catch {
            state = .failed(message: error.localizedDescription)
        }
        // A re-install pushes to the phone, so it moves the delivery state the
        // Apps section badges — same as a build. Unconditional on the outcome
        // for the same reason: the CLI records the delivery the moment the push
        // returns, and stages after that point can still fail.
        //
        // Detached, and this is the whole point of the call: `build-end` has
        // already re-enabled Close by the time we get here, and dismissing the
        // sheet cancels the `.task` this runs in. `refreshDeliveryStates` takes
        // ~250–300 ms and swallows cancellation by design, so an awaited call
        // here loses that race to any user who clicks Close as soon as it lights
        // up — and the sidebar then shows a green "current" for an app whose
        // phone holds an older build, until the next launch. The push already
        // happened; reading back what it did is not the sheet's to cancel.
        config.refreshDeliveryStatesDetached()
    }

    /// A non-zero exit the event stream never accounted for.
    ///
    /// This is the common failure here, not the exotic one: open this sheet
    /// with the iPhone unplugged and the CLI raises `InstallError("no iOS
    /// devices connected", hint: "plug the iPhone in via USB and tap 'Trust
    /// this Computer'…")` *before* `build-start`, prints it to stderr and
    /// exits 8. Zero events reached `handle(event:)`, so the sheet used to
    /// show "Failed — exit 8" over an empty log panel while the sentence that
    /// says what to do sat unread in `result.stderr`.
    @MainActor
    private func report(unexplained result: CLIRunner.RunResult) {
        // `build-end` already said what went wrong — its message is the CLI's
        // structured one and beats anything stderr has.
        if case .failed = state { return }
        logLines += CLIFailure
            .logText(exitCode: result.exitCode, stderr: result.stderr)
            .map { LogLine(kind: .warn, text: $0) }
        state = .failed(message: result.failureSummary)
    }

    @MainActor
    private func handle(event: BuildEvent) {
        // Everything outside the build stream is not this sheet's business.
        guard let progress = event.progress else { return }
        // The pill's state machine, which is this sheet's own. The transcript
        // is not — it is the mapping every panel rendering a build stream
        // shares, and `report(unexplained:)` below depends on `.failed` being
        // set here whether or not that mapping printed anything.
        switch progress {
        case .stage(let name, "start"):
            state = .running(stage: name)
        case .finished(let ok, let error, _):
            state = ok ? .ok : .failed(message: error ?? "failed")
        case .stopped:
            state = .cancelled
        default:
            break
        }
        // `re-install`, not `build start`: this command re-ships an archived
        // IPA rather than building one. It is also the line whose `delivery`
        // is nil most often — the app removed from config.yaml is this sheet's
        // commonest failure after an unplugged phone — which is why
        // `buildLogLabel` exists rather than a `?? "sideload"` per panel.
        logLines += BuildLogView.lines(for: event, startVerb: "re-install")
    }
}
