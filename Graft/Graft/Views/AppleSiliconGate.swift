import SwiftUI

/// The whole window, by `MacSupport.verdict`: `content` when this copy of
/// Graft runs here, and one screen saying why when it can't.
///
/// - **It runs** (the Apple silicon download on Apple silicon, the Intel
///   download on either Mac): `content`, with `support` in its environment
///   (`\.macSupport`), so each Apple-silicon-only action can disable itself.
///   The Intel download on an Apple silicon Mac adds one banner on top
///   (`IntelBuildBanner`).
/// - **The Apple silicon download on an Intel Mac**: its Python and tools
///   can't run there, so `content` is never built and nothing it would start
///   runs: no CLI launch, no setup screen, no device polling. Its stores are
///   created in `ContentView`'s initialiser and its work in its `.task`s, and
///   neither happens for a view that is not in the hierarchy. The screen
///   names the download this Mac needs (`WrongDownloadView`).
/// - **A bundle that disagrees with itself**: nothing runs either, and the
///   screen says so.
///
/// `openURL` is how the screens' button and the banner's open the releases
/// page: `GraftApp` passes the browser, and every test a recorder. Not
/// SwiftUI's `@Environment(\.openURL)`, whose default is the real browser in
/// a test that forgets to override it (delta audit try13, #15;
/// `NoRealDefaultsTests`).
struct AppleSiliconGate<Content: View>: View {
    let support: MacSupport
    let openURL: (URL) -> Void
    @ViewBuilder let content: () -> Content

    var body: some View {
        switch support.verdict {
        case .runs:
            content()
                .environment(\.macSupport, support)
                .safeAreaInset(edge: .top, spacing: 0) {
                    if let line = support.intelBuildBannerLine {
                        IntelBuildBanner(line: line, openURL: openURL)
                    }
                }
        case .wrongDownload:
            WrongDownloadView(message: WrongDownloadView.message, openURL: openURL)
        case let .mixedUp(why):
            WrongDownloadView(message: WrongDownloadView.mixedUpMessage(why), openURL: openURL)
        }
    }
}

/// What a Mac that can't run this copy of Graft is shown instead of the app:
/// which download it needs, or that this one is mixed up, and a button to
/// the releases page, where both downloads are.
struct WrongDownloadView: View {
    let message: String
    /// Opens `releasesURL`: the browser in the app, a recorder in a test.
    let openURL: (URL) -> Void

    static let message = "This download is for Macs with Apple silicon. For this Mac, "
        + "download Graft-Intel.zip from Graft's releases page."
    static func mixedUpMessage(_ why: String) -> String {
        "This copy of Graft.app is mixed up: \(why). Download Graft again from its releases "
            + "page and replace this copy."
    }
    static let buttonLabel = "Open the Releases Page"
    /// The latest release, which carries both downloads.
    static let releasesURL = URL(string: "https://github.com/GreenglassT/Graft/releases/latest")!

    var body: some View {
        VStack(spacing: 16) {
            Text(message)
                .font(AppFont.bodyMedium)
                .foregroundStyle(Palette.text)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            Button(Self.buttonLabel) { openURL(Self.releasesURL) }
                .keyboardShortcut(.defaultAction)
        }
        .padding(Metrics.paneGutter)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Palette.bg)
    }
}

/// The Intel download on an Apple silicon Mac: one line over the app, since
/// everything that needs Apple silicon is disabled here though the Mac has
/// it, and Graft.zip is the fix.
struct IntelBuildBanner: View {
    /// `MacSupport.intelBuildBannerLine`.
    let line: String
    let openURL: (URL) -> Void

    var body: some View {
        HStack(spacing: Metrics.controlGap) {
            Symbols.attention.image(size: 11)
                .foregroundStyle(Palette.warn)
                .accessibilityHidden(true)
            Text(line)
                .font(AppFont.body)
                .foregroundStyle(Palette.text)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: Metrics.controlGap)
            Button(WrongDownloadView.buttonLabel) { openURL(WrongDownloadView.releasesURL) }
                .controlSize(.small)
        }
        .padding(.horizontal, Metrics.paneGutter)
        .padding(.vertical, 8)
        .background(Palette.surface)
    }
}
