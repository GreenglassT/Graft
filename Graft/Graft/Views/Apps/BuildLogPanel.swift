import SwiftUI

struct LogLine: Identifiable, Hashable {
    let id = UUID()
    enum Kind { case arrow, ok, warn, dim, plain }
    let kind: Kind
    let text: String
}

struct BuildLogPanel: View {
    let lines: [LogLine]
    /// What the panel says while it has no lines. The app pane passes a
    /// sentence about the last recorded build (`AppDetailView.emptyLogMessage`),
    /// because "No build yet" under a tile reading "21h ago" contradicted it.
    var emptyMessage: String = "No build yet for this app."

    private static let scrollAnchorID = "build-log-tail"

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    if lines.isEmpty {
                        Text(emptyMessage)
                            .font(AppFont.mono).foregroundStyle(Palette.textDim)
                            .fixedSize(horizontal: false, vertical: true)
                            .padding(.vertical, 8)
                    } else {
                        ForEach(lines) { line in
                            Text(line.text)
                                .font(AppFont.mono)
                                .foregroundStyle(color(for: line.kind))
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                }
                .padding(10)
                // The panel's width comes from its container, not its contents.
                //
                // A vertical ScrollView sizes to its content on the horizontal
                // axis, so without this the panel was as wide as whatever it
                // happened to be showing: 213pt for "No build yet for this
                // app." — inside a 626pt column, next to tweak rows spanning
                // the full pane — and then a different width again once a build
                // started writing lines into it. The per-line
                // `maxWidth: .infinity` above cannot fix that; it resolves
                // against the content's own width, which is the thing being
                // measured. Empty is not the rare case either: `logLines` starts
                // empty and is never read back from disk, so this is the state
                // every launch opens in.
                .frame(maxWidth: .infinity, alignment: .leading)
                .id(Self.scrollAnchorID)
            }
            .background(Palette.surface2)
            .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Palette.line.opacity(0.4), lineWidth: 0.5))
            .clipShape(RoundedRectangle(cornerRadius: Metrics.radius))
            .onChange(of: lines.count) { _ in
                // No animation — tail-follow during fast log bursts (50-200 lines/sec
                // from zsign/cyan) would otherwise queue overlapping 100ms tweens and
                // pin the main thread.
                proxy.scrollTo(Self.scrollAnchorID, anchor: .bottom)
            }
        }
    }

    private func color(for kind: LogLine.Kind) -> Color {
        switch kind {
        case .arrow: return Palette.accent
        case .ok: return Palette.success
        case .warn: return Palette.warn
        case .dim: return Palette.textDim
        case .plain: return Palette.textMuted
        }
    }
}
