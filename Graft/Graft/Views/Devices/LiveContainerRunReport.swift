import SwiftUI

/// One LiveContainer run as it goes and once it's over: its latest step and
/// Stop while it runs, the wait for LiveContainer to be closed, then what it
/// installed, put on the device or cleaned, and its error. The device page's
/// LiveContainer section and the Set Up LiveContainer sheet both draw it,
/// from the same `LiveContainerStore.Run`, so the two never tell a run
/// differently.
struct LiveContainerRunReport: View {
    let run: LiveContainerStore.Run
    /// The device's LiveContainers, to name each one a clean reports on
    /// (`DeviceLiveContainerSection.cleanedLines`).
    let instances: [LCInstanceRecord]
    let onStop: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if !run.finished {
                HStack(spacing: Metrics.controlGap) {
                    if let last = run.progress.last {
                        dim(Self.progressLine(last))
                    }
                    Spacer(minLength: 0)
                    // Cancels the run; the CLI is SIGTERMed and takes its
                    // throwaway keychain off the search list on the way out.
                    Button("Stop") { onStop() }
                        .controlSize(.small)
                        .fixedSize()
                }
            }
            if let waiting = run.waitingMessage {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Symbols.attention.image(size: 11).foregroundStyle(Palette.warn)
                    Text(waiting.prefix(1).uppercased() + waiting.dropFirst())
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.warn)
                }
            }
            ForEach(run.installed, id: \.bundleId) { r in
                success("✓ \(r.bundleId) \(r.version) installed")
            }
            if let pushed = run.pushed {
                success("✓ Certificate copied to \(pushed.path)")
                if let note = pushed.note {
                    Text(note)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.warn)
                        .lineLimit(3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            let cleaned = DeviceLiveContainerSection.cleanedLines(run.cleaned, instances: instances)
            ForEach(Array(cleaned.enumerated()), id: \.offset) { _, line in
                success(line)
            }
            if let error = run.error {
                ErrorBanner(message: error, fit: .inset, onDismiss: nil)
            }
        }
    }

    /// "LiveContainer2: signing…", from one `lc-install-progress`.
    static func progressLine(_ p: LCInstallProgressRecord) -> String {
        let who = p.instance.map { $0 == 1 ? "LiveContainer" : "LiveContainer\($0)" }
            ?? "LiveContainer"
        let verb = [
            "fetch": "downloading", "prepare": "preparing", "profile": "provisioning",
            "sign": "signing", "install": "installing",
        ][p.step] ?? p.step
        return "\(who): \(verb)…"
    }

    private func dim(_ text: String) -> some View {
        Text(text)
            .font(AppFont.monoSmall)
            .foregroundStyle(Palette.textDim)
            .lineLimit(3)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func success(_ text: String) -> some View {
        Text(text)
            .font(AppFont.monoSmall)
            .foregroundStyle(Palette.success)
            .textSelection(.enabled)
            .lineLimit(2)
            .fixedSize(horizontal: false, vertical: true)
    }
}
