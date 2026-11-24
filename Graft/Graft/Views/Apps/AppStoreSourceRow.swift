import SwiftUI

/// The Source IPA section's App Store line: what the store has against what is
/// cached, and **Get latest from App Store**.
///
/// The get runs in the CLI — ipatool downloads, UnfairPlay decrypts, the
/// result is staged like a drop — so this row only shows where it has got to
/// (Downloading… / Decrypting… / Staging…). A failure reaches the pane's
/// existing `ErrorBanner` with the CLI's hint; a get the Apple ID has no
/// license for shows Get (free) or Open in App Store instead.
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
    /// From `AppStoreStore.licenseNeeded`: the button to show instead of Get latest.
    var license: AppStoreStore.LicenseNeed? = nil
    var isClaiming = false
    /// Get (free) is reading status before it asks
    /// (`AppStoreStore.checkBeforeClaim`).
    var isCheckingClaim = false
    /// The question to put up, once that read has named the Apple ID
    /// (`AppStoreStore.claimConfirmations`); nil while there is none.
    var claimConfirmation: AppStoreStore.ClaimConfirmation? = nil
    /// Get (free) pressed: read status again, then ask. Never the claim.
    var onCheckClaim: () -> Void = {}
    /// The confirmation's own Get (free): the claim, for the Apple ID the
    /// confirmation named.
    var onClaim: (AppStoreStore.ClaimConfirmation) -> Void = { _ in }
    /// The confirmation's Cancel, or the dialog closing.
    var onCancelClaim: () -> Void = {}
    var onOpenInStore: () -> Void = {}
    /// For the confirmation: the app's name.
    var appName = ""
    /// Why Get (free) can't run now (`AppStoreStore.claimBlockedReason`).
    var claimBlockedReason: String? = nil
    /// Why Get latest can't run now (`AppStoreStore.getBlockedReason`).
    var getBlockedReason: String? = nil

    enum PrimaryAction: Equatable { case getLatest, claimFree, openInStore }

    static func primaryAction(license: AppStoreStore.LicenseNeed?) -> PrimaryAction {
        guard let license else { return .getLatest }
        return license.offersClaim ? .claimFree : .openInStore
    }

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
            } else if isClaiming {
                ProgressView().controlSize(.small)
                Text("Getting it for this Apple ID…").font(AppFont.monoSmall).foregroundStyle(Palette.textDim)
                // The same Stop as a get's: the CLI is SIGTERMed.
                Button("Stop", action: onStop)
            } else {
                switch Self.primaryAction(license: license) {
                case .getLatest:
                    Button("Get latest from App Store", action: onGet)
                        .disabled(isBlocked || getBlockedReason != nil)
                        .help(getBlockedReason ?? "Download, decrypt and stage the App Store's latest version")
                case .claimFree:
                    if isCheckingClaim {
                        ProgressView().controlSize(.small)
                        Text(AppStoreStore.checkingClaimLine)
                            .font(AppFont.monoSmall)
                            .foregroundStyle(Palette.textDim)
                    } else {
                        Button("Get (free)", action: onCheckClaim)
                            .disabled(isBlocked || claimBlockedReason != nil)
                            .help(claimBlockedReason ?? "Add it to this Apple ID's purchases, at no cost, then get it")
                    }
                case .openInStore:
                    Button("Open in App Store", action: onOpenInStore)
                }
            }
        }
        // Up only once a fresh status read has named the Apple ID
        // (whole-diff review, get-4), and its Get (free) claims for the one it
        // named: the store refuses if status names another by then.
        .alert(
            AppStoreStore.claimConfirmation(app: appName, email: claimConfirmation?.email).title,
            isPresented: Binding(
                get: { claimConfirmation != nil },
                set: { if !$0 { onCancelClaim() } }
            ),
            presenting: claimConfirmation
        ) { confirmation in
            Button("Cancel", role: .cancel) { onCancelClaim() }
            Button("Get (free)") { onClaim(confirmation) }
        } message: { confirmation in
            Text(AppStoreStore.claimConfirmation(app: appName, email: confirmation.email).message)
        }
    }
}
