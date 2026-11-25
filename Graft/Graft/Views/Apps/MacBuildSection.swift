import SwiftUI

/// Build options' "This Mac" section: Build for this Mac and, when PlayCover
/// isn't installed, a line that says so and a Get PlayCover button.
///
/// The line is here, beside the button that starts the build, so it's read
/// before the build rather than after it: a Mac build without PlayCover still
/// builds, then can't be handed over. Build for this Mac stays enabled, since
/// the archive it makes can be handed over from Builds once PlayCover is
/// installed.
///
/// With PlayCover installed the section is what it was: the label and the
/// button.
///
/// A view of its own, rather than lines in `AppDetailView`'s popover, so a
/// test can host it with a lookup of its choosing (`PlayCoverNoticeTests`).
///
/// **Without Apple silicon** (`MacSupport.unavailableLine`): PlayCover runs
/// iPhone and iPad apps only on Apple silicon, and the PlayCover handover is
/// only in the Apple silicon download. Both buttons are disabled, and the
/// line says why in place of "PlayCover isn't installed", whose remedy
/// wouldn't help.
struct MacBuildSection: View {
    let playCover: PlayCover
    /// Build for this Mac's own gate and tooltip, which `AppDetailView` owns.
    let isBuildDisabled: Bool
    let buildHelp: String
    let onBuild: () -> Void
    @Environment(\.macSupport) private var mac

    static let buildLabel = "Build for this Mac"
    static let missingMessage = "PlayCover isn't installed. Graft hands a build for this Mac to "
        + "PlayCover, a free app you install yourself."
    static let getLabel = "Get PlayCover"
    static let getHelp = "Open PlayCover's releases page on GitHub"

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("This Mac")
                .labelCaps()
            // Asked on each draw, so PlayCover installed while Graft is open
            // counts the next time Build options opens.
            let missing = !playCover.isInstalled
            let unavailable = mac.unavailableLine
            if let unavailable {
                Text(unavailable)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
                    .fixedSize(horizontal: false, vertical: true)
            } else if missing {
                // The device warning's style, a few lines up the same popover.
                Text(Self.missingMessage)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.warn)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 8) {
                Button(Self.buildLabel, action: onBuild)
                    .controlSize(.small)
                    .disabled(isBuildDisabled || unavailable != nil)
                    .help(unavailable ?? buildHelp)
                if missing {
                    Button(Self.getLabel) { playCover.openReleasesPage() }
                        .controlSize(.small)
                        .disabled(unavailable != nil)
                        .help(unavailable ?? Self.getHelp)
                }
            }
        }
    }
}
