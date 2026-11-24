import SwiftUI

/// Settings › Command-line tool: `graft` in Terminal, linked to this app's
/// own CLI (`CommandLineTool`).
///
/// Release builds only (`isOffered`): a Debug build has no
/// `Contents/Helpers/graft` to link.
struct CommandLineToolSection: View {
    let context: CommandLineTool.Context

    @State private var status: CommandLineTool.Status?
    @State private var advice: String?
    /// Another `graft` earlier on the login PATH than the link.
    @State private var shadowedBy: String?
    /// The login shell's PATH: the launch-time answer, or Check again's.
    @State private var loginPATH: String?
    @State private var failure: String?
    @State private var confirmingReplace = false
    /// Check again is asking the shell.
    @State private var isRechecking = false

    static var isOffered: Bool { GraftExecutable.preferBundledDefault }

    static let installLabel = "Install graft for Terminal"
    static let reinstallLabel = "Reinstall"
    static let replaceLabel = "Replace it…"
    static let pipxUninstall = "pipx uninstall graft-ipa"
    /// Beside the PATH advice: ask the login shell again, once the line is in.
    static let checkAgainLabel = "Check again"
    static let checkAgainCaption = "Added it? Open a new Terminal window to use it there, and check again here."

    /// In place of every action, while the app runs from somewhere a link
    /// would not outlive (`AppLocation`).
    static let moveFirst = "Move Graft to Applications first, then install the command."

    /// - Parameter shadowedBy: another `graft` that comes before the link on
    ///   the login shell's PATH, which is the one Terminal runs.
    static func statusLine(_ status: CommandLineTool.Status, link: String, shadowedBy: String? = nil) -> String {
        switch status {
        case .notInstalled:
            return "Not installed. This puts a link to the app's graft at \(link)."
        case .installed:
            if let shadowedBy {
                return "Installed at \(link), but \(shadowedBy) comes first on your shell's PATH, "
                    + "so graft in Terminal runs that one."
            }
            return "Installed: graft in Terminal runs this app's, through \(link)."
        case let .otherGraft(path):
            return "Another graft is at \(link): \(path)."
        case let .broken(path):
            return "The link at \(link) is broken: it points to \(path), which isn't there any "
                + "more. Graft.app was probably moved. Reinstall points it at this copy."
        case .blockedByNonFile:
            return "A folder is at \(link), so there's no room for the link. "
                + "Move it out of the way, then come back."
        }
    }

    /// The pipx line goes with another graft that pipx could have put there:
    /// not a link to another copy of Graft.app, which pipx knows nothing of.
    static func offersPipxUninstall(_ status: CommandLineTool.Status) -> Bool {
        guard case let .otherGraft(path) = status else { return false }
        return !GraftExecutable.isBundled(path)
    }

    /// What Replace does to what is there: a link is only repointed, and
    /// what it pointed to stays; a regular file is deleted to make room.
    static func replaceMessage(other: String, link: String, shown: (String) -> String) -> String {
        if other == link {
            return "\(shown(link)) is a file, not a link. It will be deleted, and a link to "
                + "this app's graft put in its place."
        }
        return "\(shown(link)) will run this app's graft instead. "
            + "\(shown(other)) itself stays where it is."
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Metrics.titleGap) {
            SectionTitle(text: "Command-line tool")
            VStack(alignment: .leading, spacing: Metrics.rowGap) {
                if let status {
                    HStack(alignment: .firstTextBaseline) {
                        Text(Self.statusLine(
                            status, link: context.abbreviated(context.link),
                            shadowedBy: shadowedBy.map(context.abbreviated)
                        ))
                            .font(AppFont.body)
                            .foregroundStyle(Palette.text)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: Metrics.controlGap)
                        action(for: status)
                            .disabled(context.location != .settled)
                    }
                    if context.location != .settled {
                        Text(Self.moveFirst)
                            .font(AppFont.monoSmall)
                            .foregroundStyle(Palette.warn)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if Self.offersPipxUninstall(status) {
                        CopyLine(
                            caption: "Optional, if pipx installed it: remove that copy",
                            text: Self.pipxUninstall
                        )
                    }
                }
                if let advice {
                    CopyLine(
                        caption: "~/.local/bin isn't on your shell's PATH. Add this to "
                            + CommandLineTool.startupFile(shell: context.shell),
                        text: advice
                    )
                    HStack(alignment: .firstTextBaseline) {
                        Text(Self.checkAgainCaption)
                            .font(AppFont.monoSmall)
                            .foregroundStyle(Palette.textDim)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: Metrics.controlGap)
                        Button(Self.checkAgainLabel) {
                            Task { await readLoginPATH(askShellAgain: true) }
                        }
                        .controlSize(.small)
                        .disabled(isRechecking)
                    }
                }
                if let failure {
                    Text(failure)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.danger)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(Metrics.cardPadding)
            .cardStyle()
        }
        // Each time the section appears: an app moved in Finder, or a link
        // changed in Terminal, shows the next time Settings is opened. The
        // login shell's PATH is the one read at launch: asking the shell
        // again runs the user's startup files, so only Check again does
        // (whole-diff review, app-1).
        .task { await readLoginPATH(askShellAgain: false) }
        .alert("Replace the other graft?", isPresented: $confirmingReplace) {
            Button("Cancel", role: .cancel) {}
            Button("Replace") { install(replace: true) }
        } message: {
            if case let .otherGraft(other) = status {
                Text(Self.replaceMessage(other: other, link: context.link, shown: context.abbreviated))
            }
        }
    }

    @ViewBuilder
    private func action(for status: CommandLineTool.Status) -> some View {
        switch status {
        case .notInstalled:
            Button(Self.installLabel) { install(replace: false) }
                .controlSize(.small)
        case .installed, .broken:
            Button(Self.reinstallLabel) { install(replace: false) }
                .controlSize(.small)
        case .otherGraft:
            Button(Self.replaceLabel) { confirmingReplace = true }
                .controlSize(.small)
        case .blockedByNonFile:
            EmptyView()
        }
    }

    private func readLoginPATH(askShellAgain: Bool) async {
        if askShellAgain { isRechecking = true }
        defer { if askShellAgain { isRechecking = false } }
        refresh()
        loginPATH = await context.loginPATH(askShellAgain)
        advice = CommandLineTool.pathAdvice(
            loginPATH: loginPATH, shell: context.shell, home: context.home
        )
        refresh()
    }

    private func refresh() {
        status = CommandLineTool.status(link: context.link, target: context.target, in: context.fs)
        shadowedBy = status == .installed
            ? CommandLineTool.shadowingGraft(
                loginPATH: loginPATH, link: context.link, target: context.target, in: context.fs
            )
            : nil
    }

    private func install(replace: Bool) {
        // The buttons are disabled there too; this is the guarantee.
        guard context.location == .settled else { return }
        do {
            try CommandLineTool.install(
                link: context.link, target: context.target, replace: replace, in: context.fs
            )
            failure = nil
        } catch {
            failure = error.localizedDescription
        }
        refresh()
    }
}

/// A caption, a line to paste, and Copy.
private struct CopyLine: View {
    let caption: String
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(caption)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer()
                Button("Copy") { Pasteboard.copy(text) }
                    .buttonStyle(.borderless)
                    .font(AppFont.label)
                    .foregroundStyle(Palette.accent)
                    .accessibilityLabel("Copy \(text)")
                    .help("Copy \(text)")
            }
            Text(text)
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.text)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(Metrics.innerPadding)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.surface2.opacity(0.5))
        .clipShape(RoundedRectangle(cornerRadius: Metrics.radiusInner))
    }
}
