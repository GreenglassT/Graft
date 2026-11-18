import SwiftUI

/// The Source IPA section's App Store line: what the store has against what is
/// cached, and **Get latest from App Store**.
///
/// The get runs in the CLI — ipatool downloads, UnfairPlay decrypts, the
/// result is staged like a drop — so this row only shows where it has got to
/// (Downloading… / Decrypting… / Staging…). A failure reaches the pane's
/// existing `ErrorBanner` with the CLI's hint, which is where "tap Get on a
/// device first" or the Terminal login command appears.
///
/// Always the App Store's *latest* version: there is no version picker, by
/// the spec's decision.
struct AppStoreSourceRow: View {
    let version: AppStoreVersionRecord?
    let isGetting: Bool
    let stage: AppStoreStage?
    /// Another operation owns the cache for this app right now — a drop being
    /// staged, or a build reading the IPA.
    let isBlocked: Bool
    let onGet: () -> Void
    /// Cancels the running get; the CLI then deletes its encrypted download
    /// on the SIGTERM this sends (`appstore._stop_cleans_up`).
    let onStop: () -> Void
    /// Asks the App Store again now. The pane reuses an answer for ten
    /// minutes rather than asking on every click (`AppStoreStore.checkVersion`).
    let onRefresh: () -> Void

    static func versionLine(_ record: AppStoreVersionRecord?) -> String {
        guard let record else { return "Checking the App Store…" }
        guard let store = record.storeVersion else {
            return "Couldn't check the App Store: \(record.error ?? "no answer")"
        }
        guard let cached = record.cachedVersion else {
            return "App Store \(store) · nothing cached"
        }
        return record.updateAvailable
            ? "App Store \(store) · cached \(cached) — update available"
            : "App Store \(store) · cached \(cached) — up to date"
    }

    static func progressLabel(_ stage: AppStoreStage?) -> String {
        stage?.label ?? "Starting…"
    }

    var body: some View {
        HStack(spacing: Metrics.controlGap) {
            // One line, truncated: a long `error` must not widen the pane
            // (see `AppDetailLayoutTests` on what unbounded text does here).
            Text(isGetting ? Self.progressLabel(stage) : Self.versionLine(version))
                .font(AppFont.monoSmall)
                .foregroundStyle(
                    !isGetting && version?.updateAvailable == true ? Palette.accent : Palette.textDim
                )
                .lineLimit(1)
                .truncationMode(.tail)
                .help(Self.versionLine(version))
            if !isGetting {
                Button(action: onRefresh) { Symbols.refresh.image }
                    .buttonStyle(.borderless)
                    .foregroundStyle(Palette.accent)
                    .help("Check the App Store again")
                    // Named for what it does: the glyph alone reads "Refresh",
                    // and there are two refreshes on this pane.
                    .accessibilityLabel("Check the App Store again")
            }
            Spacer(minLength: Metrics.controlGap)
            if isGetting {
                ProgressView().controlSize(.small)
                Button("Stop", action: onStop)
            } else {
                Button("Get latest from App Store", action: onGet)
                    .disabled(isBlocked)
            }
        }
    }
}
