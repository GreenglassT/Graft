import SwiftUI

/// The Source IPA section's App Store line: what the store has against what is
/// cached, and **Get latest from App Store**, or **Get Again** when that
/// version is the one already staged.
///
/// The get runs in the CLI — ipatool downloads, UnfairPlay decrypts, the
/// result is staged like a drop — so this row only shows where it has got to
/// (Downloading… / Decrypting… / Staging…). A failure reaches the pane's
/// existing `ErrorBanner` with the CLI's hint; a get the Apple ID has no
/// license for shows Get (free) or Open in App Store instead.
///
/// Get latest fetches the App Store's *latest* version. A get of the version
/// already staged downloads nothing and says so (`appstore-already-staged`);
/// Get Again, behind a confirmation, downloads it anyway.
///
/// **Get an older version…** opens `AppStoreVersionsSheet`, where a version
/// the App Store still serves can be picked. Getting one holds the app at it
/// (its `preferred_version`): the line then says "Held at 1.2.0 · newest
/// 1.4.0", never "update available", and Get latest lets go.
///
/// **Without Apple silicon** (`MacSupport.unavailableLine`) every get is
/// disabled, not hidden: a get downloads an encrypted IPA, and only an Apple
/// silicon Mac, with the download that carries UnfairPlay, decrypts one. The
/// row says why on a line of its own, and the store's version line still
/// shows, since adding an app by name works anywhere.
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
    /// Get Again's confirmation, answered: the get with `--again`.
    var onGetAgain: () -> Void = {}
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
    /// Get an older version… pressed: the pane opens the sheet.
    var onOlderVersions: () -> Void = {}

    /// Get Again's confirmation is up.
    @State private var confirmingGetAgain = false
    /// Whether this copy can get from the App Store here (`MacSupport`).
    @Environment(\.macSupport) private var mac

    enum PrimaryAction: Equatable { case getLatest, getAgain, claimFree, openInStore }

    /// A license refusal decides first. Otherwise Get Again where the line
    /// says up to date with the very version Get latest would fetch: the
    /// store's version is the cached one. A store version nobody could check
    /// is never "again", and nor is a newer copy staged than the store has.
    ///
    /// The line's own versions decide, never what the last get said. With a
    /// version pin the cached version is the pinned one, so a get of the
    /// store's version, staged beside it, says "Already have" and the row
    /// still offers Get latest. An answer kept from that get could outlive
    /// the file it was about (deleted in the Cache pane), and Get Again's
    /// question would then call a missing file staged.
    ///
    /// A held app is always Get latest: that is how the hold is let go, and
    /// it downloads nothing when the latest is staged already. Get Again
    /// there would download the held version's newest twin again for
    /// nothing, and leave the user wondering how to stop holding.
    static func primaryAction(
        license: AppStoreStore.LicenseNeed?, version: AppStoreVersionRecord? = nil
    ) -> PrimaryAction {
        if let license { return license.offersClaim ? .claimFree : .openInStore }
        if version?.heldVersion != nil { return .getLatest }
        guard let store = version?.storeVersion else { return .getLatest }
        return store == version?.cachedVersion ? .getAgain : .getLatest
    }

    static let getAgainButton = "Get Again"

    /// What Get Again asks first: it downloads the whole app again.
    static func getAgainConfirmation(app: String, version: String?) -> (title: String, message: String) {
        let which = version ?? "This version"
        return (
            "Download \(app) again?",
            "\(which) is already staged, and it's the App Store's current version. "
                + "Getting it again downloads the whole app, decrypts it and replaces the staged copy."
        )
    }

    static let olderVersionsButton = "Get an older version…"

    /// What Get latest's help says: what it does, and that it lets go of a hold.
    static func getLatestHelp(held: String?) -> String {
        guard let held else { return "Download, decrypt and stage the App Store's latest version" }
        return "Download, decrypt and stage the App Store's latest version, and stop holding this app at \(held)"
    }

    static func versionLine(_ record: AppStoreVersionRecord?) -> String {
        guard let record else { return "Checking the App Store…" }
        if let held = record.heldVersion { return heldLine(held, record) }
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

    /// The line for a held app: the version its builds use, and the newest
    /// the App Store has. "(not staged)" when the held version's IPA isn't in
    /// the cache any more (deleted in the Cache pane): a build would stop.
    ///
    /// A hold by build whose number the App Store has more than once says
    /// which build, in the Older Versions sheet's words
    /// (`DuplicateBuild.suffix`): "Held at 2026.42.0 (build 1 of 2)". When the
    /// App Store's current version is that same number, its newest build is
    /// the last one, which build 1 isn't, so the line says so rather than
    /// "the App Store's newest".
    static func heldLine(_ held: String, _ record: AppStoreVersionRecord) -> String {
        let build = record.heldBuild.map {
            " " + AppStoreVersionsStore.DuplicateBuild(version: held, number: $0.number, count: $0.count).suffix
        } ?? ""
        let staged = record.cachedVersion == held ? "" : " (not staged)"
        let shown = "\(held)\(build)\(staged)"
        guard let store = record.storeVersion else {
            return "Held at \(shown) · couldn't check the App Store: \(record.error ?? "no answer")"
        }
        if store == held, let heldBuild = record.heldBuild, heldBuild.number < heldBuild.count {
            let newest = AppStoreVersionsStore.DuplicateBuild(
                version: store, number: heldBuild.count, count: heldBuild.count
            )
            return "Held at \(shown) · newest \(store) \(newest.suffix)"
        }
        return store == held
            ? "Held at \(shown) · the App Store's newest"
            : "Held at \(shown) · newest \(store)"
    }

    /// Whether Get an older version… is offered: not while a get or claim
    /// runs, and not while the Apple ID lacks the app (Get (free) or Open in
    /// App Store first): the App Store lists versions only of apps it has.
    static func offersOlderVersions(license: AppStoreStore.LicenseNeed?, isGetting: Bool, isClaiming: Bool) -> Bool {
        license == nil && !isGetting && !isClaiming
    }

    static func progressLabel(_ stage: AppStoreStage?) -> String {
        stage?.label ?? "Starting…"
    }

    /// Why a get can't run now: this Mac first (it won't change), then what
    /// is running.
    static func blockedReason(unavailable: String?, running: String?) -> String? {
        unavailable ?? running
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            row
            if let line = mac.unavailableLine {
                Text(line)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var getBlocked: String? {
        Self.blockedReason(unavailable: mac.unavailableLine, running: getBlockedReason)
    }

    private var claimBlocked: String? {
        Self.blockedReason(unavailable: mac.unavailableLine, running: claimBlockedReason)
    }

    private var row: some View {
        HStack(spacing: Metrics.controlGap) {
            if !isGetting, version?.heldVersion != nil {
                // The line beside it says which version; the pin is drawing.
                Symbols.held.image(size: 10)
                    .foregroundStyle(Palette.textDim)
                    .accessibilityHidden(true)
            }
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
                if Self.offersOlderVersions(license: license, isGetting: isGetting, isClaiming: isClaiming) {
                    Button(Self.olderVersionsButton, action: onOlderVersions)
                        .disabled(isBlocked || getBlocked != nil)
                        .help(getBlocked ?? "Pick a version the App Store still has for this Apple ID, and get that one")
                }
                switch Self.primaryAction(license: license, version: version) {
                case .getLatest:
                    Button("Get latest from App Store", action: onGet)
                        .disabled(isBlocked || getBlocked != nil)
                        .help(getBlocked ?? Self.getLatestHelp(held: version?.heldVersion))
                case .getAgain:
                    getAgain
                case .claimFree:
                    if isCheckingClaim {
                        ProgressView().controlSize(.small)
                        Text(AppStoreStore.checkingClaimLine)
                            .font(AppFont.monoSmall)
                            .foregroundStyle(Palette.textDim)
                    } else {
                        Button("Get (free)", action: onCheckClaim)
                            .disabled(isBlocked || claimBlocked != nil)
                            .help(claimBlocked ?? "Add it to this Apple ID's purchases, at no cost, then get it")
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

    /// Get Again, asking first. Its own alert, on the button rather than
    /// beside the claim's on the row, so the two never share a view.
    private var getAgain: some View {
        let words = Self.getAgainConfirmation(app: appName, version: version?.storeVersion)
        return Button(Self.getAgainButton) { confirmingGetAgain = true }
            .disabled(isBlocked || getBlocked != nil)
            .help(getBlocked ?? "Download, decrypt and stage the App Store's current version again, though it's already staged")
            .alert(words.title, isPresented: $confirmingGetAgain) {
                Button("Cancel", role: .cancel) {}
                Button(Self.getAgainButton) { onGetAgain() }
            } message: {
                Text(words.message)
            }
    }
}
