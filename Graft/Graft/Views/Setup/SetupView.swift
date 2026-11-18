import SwiftUI

/// The first thing a new user sees: the Terminal steps between downloading
/// Graft.app and using it, in the order they come — install the CLI, create
/// the config, install the tools a build needs.
///
/// It replaces two modal alerts that greeted a stranger as faults — a launch
/// failure naming a path in `~/.local/bin`, then "Something didn't work: config
/// file not found at …". Neither is a fault; both are setup, and what the user
/// needs is the command, a Copy button, and a way back in once it has run.
///
/// Shown in the detail area while `ConfigStore.setupNeed` is set, so the
/// sidebar stays where it is (empty, but usable) and nothing modal covers the
/// window. Check again re-runs the same first load (`ConfigStore.checkSetup`);
/// coming back to the window does too, so a user who runs the command and
/// switches back is usually not asked to press anything.
///
/// The copy is plain on purpose: what to type, what it will ask, and where
/// the app looks. No emoji, and no assumption that the reader knows what pipx
/// or a provisioning profile is beyond the one clause that says.
struct SetupView: View {
    let need: SetupNeed
    let isChecking: Bool
    let onCheckAgain: () -> Void
    /// Step 3's way past it, for a user who wants to look around before
    /// installing everything. Only that step offers it: without the CLI or a
    /// config nothing in the app can work, but without one tool most of it
    /// still can.
    var onContinueWithoutTools: () -> Void = {}

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Metrics.sectionGap) {
                intro
                VStack(alignment: .leading, spacing: Metrics.sectionGap) {
                    SetupStep(
                        number: 1, title: Self.stepOneTitle(for: need),
                        state: Self.state(of: 1, for: need)
                    ) {
                        installSteps
                    }
                    SetupStep(
                        number: 2, title: "Create your config",
                        state: Self.state(of: 2, for: need)
                    ) {
                        configSteps
                    }
                    SetupStep(
                        number: 3, title: "Install the tools",
                        state: Self.state(of: 3, for: need)
                    ) {
                        toolSteps
                    }
                }
                checkAgain
            }
            // A readable measure for the prose, centred in a wide pane.
            .frame(maxWidth: 600, alignment: .leading)
            .padding(Metrics.paneGutter)
            .frame(maxWidth: .infinity)
        }
        .background(Palette.bg)
        .navigationTitle(Self.title)
    }

    /// The window title while this is showing — also what a test reads back
    /// from the hosting `NSWindow` to know the setup screen is what rendered.
    static let title = "Set up Graft"

    enum StepState: Equatable {
        case done
        case current
        case upcoming
    }

    /// Where step `number` stands for `need`: before it is done, it is the
    /// step, after it is waiting.
    static func state(of number: Int, for need: SetupNeed) -> StepState {
        if number < need.step { return .done }
        return number == need.step ? .current : .upcoming
    }

    /// No heading of its own: the window's title bar already says "Set up
    /// Graft", as every pane's does, and a second copy under it read as a
    /// stutter.
    private var intro: some View {
        Text("Graft.app does its work through the graft command-line tool. "
             + "A few steps in Terminal set it up; after that, everything happens here.")
            .font(AppFont.body)
            .foregroundStyle(Palette.textMuted)
            .fixedSize(horizontal: false, vertical: true)
    }

    // MARK: Step 1

    /// "Repair", not "Install", when graft is there and will not start:
    /// installing is what the user already did.
    static func stepOneTitle(for need: SetupNeed) -> String {
        if case .brokenCLI = need { return "Repair the command-line tool" }
        return "Install the command-line tool"
    }

    @ViewBuilder
    private var installSteps: some View {
        if case let .brokenCLI(broken) = need {
            repairSteps(broken)
        } else {
            freshInstallSteps
        }
    }

    /// The file, why it would not start, and the command that rebuilds it.
    ///
    /// The install command is still offered, second: `pipx reinstall` refuses
    /// a package pipx does not know, which is what a graft put there some
    /// other way looks like.
    private func repairSteps(_ broken: BrokenCLI) -> some View {
        VStack(alignment: .leading, spacing: Metrics.rowGap) {
            prose("graft is installed, but it won't start: \(broken.reason).")
            note(broken.path)
            if let errnoLine = broken.errnoLine {
                note(errnoLine)
            }
            prose("Reinstall it with pipx, which rebuilds its Python environment:")
            TerminalCommand(command: GraftExecutable.reinstallCommand)
            prose("If pipx says graft-ipa isn't installed, install it fresh instead:")
            TerminalCommand(command: GraftExecutable.installCommand)
        }
    }

    private var freshInstallSteps: some View {
        VStack(alignment: .leading, spacing: Metrics.rowGap) {
            prose("Install it with pipx:")
            TerminalCommand(command: GraftExecutable.installCommand)
            prose("No pipx yet? Install it with Homebrew, then run pipx ensurepath "
                  + "and open a new Terminal window so the change takes effect:")
            TerminalCommand(command: "brew install pipx")
            TerminalCommand(command: "pipx ensurepath")
            note("Graft.app looks for graft in "
                 + ProcessEnvironment.toolDirs(home: "~").joined(separator: ", ")
                 + ", and on your shell's PATH.")
        }
    }

    // MARK: Step 2

    private var configSteps: some View {
        VStack(alignment: .leading, spacing: Metrics.rowGap) {
            // Mirrors `graft --init`'s own order: the paid-membership
            // question first, because everything after it depends on it.
            prose("Run the setup. It first asks whether you have a paid Apple Developer "
                  + "Program membership. With one, it asks where your signing certificate "
                  + "(.p12) and provisioning profile are and copies them into Graft's config "
                  + "folder. With a free Apple ID it skips those and lists the steps to do "
                  + "on the phone. Either way it writes config.yaml:")
            TerminalCommand(command: Self.initCommand)
            // Said as paid-only in so many words: a free Apple ID has no
            // certificate, and `--set-p12-password` refuses on its config.
            prose("With a free Apple ID that is all: there is no certificate, and "
                  + "LiveContainer signs each app when you import it. With a paid "
                  + "membership, then store the certificate's password in your Keychain:")
            TerminalCommand(command: Self.passwordCommand)
            if case let .config(path?) = need {
                note("Graft looks for its config at \(path).")
            }
        }
    }

    static let initCommand = "graft --init"
    static let passwordCommand = "graft --set-p12-password"

    // MARK: Step 3

    /// One row per missing tool, each with the command `--check` gave for
    /// it. The list is the CLI's, so a free Apple ID's is shorter on its own:
    /// `--check` leaves out the tools only a paid account's builds use.
    @ViewBuilder
    private var toolSteps: some View {
        if case let .tools(missing) = need {
            VStack(alignment: .leading, spacing: Metrics.rowGap) {
                prose(Self.toolsIntro(count: missing.count))
                ForEach(missing) { tool in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(tool.name)
                            .font(AppFont.bodyMedium)
                            .foregroundStyle(Palette.text)
                        TerminalCommand(command: tool.command)
                    }
                }
                HStack(spacing: Metrics.controlGap) {
                    Button(Self.continueLabel(count: missing.count), action: onContinueWithoutTools)
                        .buttonStyle(.borderless)
                        .font(AppFont.body)
                        .foregroundStyle(Palette.accent)
                    Text(Self.continueNote(count: missing.count))
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textDim)
                }
            }
        }
    }

    static func continueLabel(count: Int) -> String {
        count == 1 ? "Continue without it" : "Continue without them"
    }

    static func continueNote(count: Int) -> String {
        count == 1 ? "Settings lists it until it's installed." : "Settings lists them until they're installed."
    }

    static func toolsIntro(count: Int) -> String {
        let what = count == 1 ? "One tool a build needs is" : "\(count) tools a build needs are"
        return "\(what) missing from this Mac. Install "
            + (count == 1 ? "it" : "each") + " in Terminal, then check again:"
    }

    // MARK: Check again

    private var checkAgain: some View {
        HStack(spacing: Metrics.controlGap) {
            Button(action: onCheckAgain) {
                if isChecking {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.small)
                        Text("Checking…")
                    }
                } else {
                    Text("Check again")
                }
            }
            .keyboardShortcut(.defaultAction)
            .disabled(isChecking)
            .accessibilityLabel("Check again")
            Text("Graft also checks when you come back to this window.")
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.textDim)
        }
    }

    private func prose(_ text: String) -> some View {
        Text(text)
            .font(AppFont.body)
            .foregroundStyle(Palette.text)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func note(_ text: String) -> some View {
        Text(text)
            .font(AppFont.monoSmall)
            .foregroundStyle(Palette.textDim)
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
    }
}

/// One numbered step: a header that always shows, and the instructions only
/// while it is the step the user is on. Showing step 2's commands before
/// graft is installed would invite running them into "command not found".
private struct SetupStep<Content: View>: View {
    let number: Int
    let title: String
    let state: SetupView.StepState
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: Metrics.cardPadding) {
            HStack(spacing: 10) {
                badge
                Text(title)
                    .font(AppFont.bodyMedium)
                    .foregroundStyle(state == .upcoming ? Palette.textDim : Palette.text)
                Spacer()
                Text(statusText).labelCaps()
            }
            // Read as one element: "Step 1, Install the command-line tool, done".
            .accessibilityElement(children: .combine)
            if state == .current {
                content
            }
        }
        .padding(Metrics.cardPadding)
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardStyle(
            surfaceOpacity: state == .current ? 0.6 : 0.25,
            borderOpacity: state == .current ? 0.8 : 0.4
        )
    }

    private var statusText: String {
        switch state {
        case .done: return "Done"
        case .current: return "Step \(number) of \(SetupNeed.stepCount)"
        case .upcoming: return "Next"
        }
    }

    @ViewBuilder
    private var badge: some View {
        if state == .done {
            Symbols.ok.image(size: 16)
                .foregroundStyle(Palette.success)
                .accessibilityLabel("Step \(number)")
        } else {
            Text("\(number)")
                .font(AppFont.label)
                .foregroundStyle(state == .current ? Palette.text : Palette.textDim)
                .frame(width: 18, height: 18)
                .background(
                    Circle().fill(state == .current ? Palette.accentSelect : Palette.surface)
                )
                .accessibilityLabel("Step \(number)")
        }
    }
}

/// A command to run in Terminal, and a Copy button that says it worked.
///
/// The recess and the accent Copy are the same as Settings' App Store setup
/// commands (`AppStoreSettingsSection.SetupCommands`), so the two places the
/// app hands the user a Terminal command look like one thing.
private struct TerminalCommand: View {
    let command: String
    @State private var copied = false

    var body: some View {
        HStack(spacing: Metrics.controlGap) {
            Text(command)
                .font(AppFont.mono)
                .foregroundStyle(Palette.text)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: Metrics.controlGap)
            Button(copied ? "Copied" : "Copy") {
                Pasteboard.copy(command)
                copied = true
                Task {
                    try? await Task.sleep(nanoseconds: 1_500_000_000)
                    copied = false
                }
            }
            .buttonStyle(.borderless)
            .font(AppFont.label)
            .foregroundStyle(Palette.accent)
            // Several of these sit on one screen; each says which it copies.
            .accessibilityLabel("Copy \(command)")
            .help("Copy \(command)")
        }
        .padding(Metrics.innerPadding)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.surface2.opacity(0.5))
        .clipShape(RoundedRectangle(cornerRadius: Metrics.radiusInner))
    }
}
