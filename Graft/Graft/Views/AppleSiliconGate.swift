import SwiftUI

/// The whole window on a Mac without Apple silicon, and `content` on one with.
///
/// On an Intel Mac `content` is never built, so nothing it would start runs:
/// no CLI launch, no setup screen, no device polling. Its stores are created
/// in `ContentView`'s initialiser and its work in its `.task`s, and neither
/// happens for a view that is not in the hierarchy.
///
/// `openURL` is how the gate's one button opens the way to 1.1: `GraftApp`
/// passes the browser, and every test a recorder. Not SwiftUI's
/// `@Environment(\.openURL)`, whose default is the real browser in a test
/// that forgets to override it (delta audit try13, #15; `NoRealDefaultsTests`).
struct AppleSiliconGate<Content: View>: View {
    let isAppleSilicon: Bool
    let openURL: (URL) -> Void
    @ViewBuilder let content: () -> Content

    var body: some View {
        if isAppleSilicon {
            content()
        } else {
            AppleSiliconRequiredView(openURL: openURL)
        }
    }
}

/// What an Intel Mac is shown instead of the app: Graft 2 can't run there,
/// and Graft 1.1 may, on macOS 15 or later, though it was never tested on
/// Intel.
///
/// The button opens the upgrade guide's way back, not the v1.1 release page
/// (whole-diff review, docs-2): that page's own install command has no tag,
/// and since 2.0 it installs 2.0's CLI under the 1.1 app. The guide's pins
/// it with `@v1.1`.
struct AppleSiliconRequiredView: View {
    /// Opens `downloadURL`: the browser in the app, a recorder in a test.
    let openURL: (URL) -> Void

    static let message = "Graft 2.0 needs a Mac with Apple silicon. Graft 1.1 may work on an Intel Mac running macOS 15 or later, but it was never tested there."
    static let downloadLabel = "How to install Graft 1.1"
    static let downloadURL = URL(string: "https://github.com/GreenglassT/Graft/blob/main/docs/upgrading.md#going-back-to-11")!

    var body: some View {
        VStack(spacing: 16) {
            Text(Self.message)
                .font(AppFont.bodyMedium)
                .foregroundStyle(Palette.text)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            Button(Self.downloadLabel) { openURL(Self.downloadURL) }
                .keyboardShortcut(.defaultAction)
        }
        .padding(Metrics.paneGutter)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Palette.bg)
    }
}
