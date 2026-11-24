import AppKit
import SwiftUI

/// Settings › App Store › Set Up Decryption…: pick an installed iPhone or iPad
/// app, read what will happen, then macOS asks for an administrator password
/// and UnfairPlay recovers this Mac's decryption model from that app.
///
/// Explained first (controller decision 7): macOS 27's dialog ignores the
/// text Graft gives it and words the request itself. In the owner's run it
/// named Graft as the app asking for administrator access and offered Touch
/// ID or the password (`AdminShell.recordedDialogWording`). So before it
/// appears, this sheet says what the step is and what that dialog will
/// usually ask, and shows the exact script behind a disclosure. Cancel, here
/// or in macOS's dialog, is always safe.
///
/// macOS's own dialog takes the password; Graft never sees it (`AdminShell`).
/// With no app to use, the sheet says what to install and offers to open the
/// Mac App Store. Afterward it reads the result off status, which accepts a
/// model only if it is the user's and readable.
struct AppStoreEnrollSheet: View {
    let account: AppStoreAccountStore
    let store: AppStoreStore

    @State private var chosen: String?
    @State private var showScript = false
    @Environment(\.dismiss) private var dismiss

    static let intro = "UnfairPlay reads this Mac's App Store state once, from an iPhone or iPad app you installed from the Mac App Store, and saves a private model that decrypts the apps you get in Graft. "
        + "The model works only for the Apple ID that installed that app: use the same Apple ID in Graft as in the Mac App Store."
    static let noCandidates = "No installed app can do this yet. Install any free iPhone or iPad app from the Mac App Store, open it once, then check again."

    /// What the sheet says before macOS's dialog appears (decision 7): what
    /// the dialog usually looks like, as the owner's run of the notarized app
    /// recorded it (`AdminShell.recordedDialogWording`), and how it differs.
    /// That run was one Mac with Touch ID, through `AppleScriptAdminShell`
    /// (delta audit try13, #5): a Mac without Touch ID asks for the password
    /// with no Touch ID offer and no Use Password… button, and through the
    /// osascript fallback (`FallbackAdminShell`) the dialog names osascript.
    /// So the sheet says "usually", and says each of those, rather than
    /// stating one dialog as fact.
    static let dialogLine = "macOS's dialog usually names Graft and asks you to allow it administrator access. "
        + "On a Mac with Touch ID it offers Touch ID or Use Password\u{2026}; on one without, it asks for the password. "
        + "If Graft has to start this step through osascript instead, the dialog names osascript."

    /// Two lines depend on `verified`, so the sheet never claims a check the
    /// script won't make: what root runs, and whether anything has checked it.
    static func explanation(verified: Bool) -> [String] {
        [
            "This is a one-time step for this Mac, until its App Store Apple ID changes.",
            "It reads the App Store license of the app you picked, to save this Mac's decryption model.",
            verified
                ? "Root runs its own copy of UnfairPlay, and only after checking that it carries Graft's developer signature."
                : "Root runs a copy of UnfairPlay that nothing has checked: the one the warning above names.",
            "macOS will ask for Touch ID or your Mac's administrator password. Graft never sees either.",
            verified
                ? dialogLine
                : dialogLine + " It doesn't check what runs, and this time nothing else has either.",
            "Cancel is always safe: nothing changes.",
        ]
    }

    // The unchecked-UnfairPlay warning (review part 4, #1 and #28). Shown
    // above everything else, never inside the script's disclosure.
    static let uncheckedTitle = "Nothing checks this copy of UnfairPlay"
    static func uncheckedHelperLine(_ helper: String) -> String { "Root would run: \(helper)" }
    static let uncheckedButton = "Run Unchecked Copy as Root…"

    /// What the warning offers: the explicit second step, or why there isn't one.
    enum UncheckedOffer: Equatable {
        case runAnyway
        case refused(String)
    }

    static func uncheckedOffer(kind: String, release: Bool = AppStoreAccountStore.isRelease) -> UncheckedOffer {
        if AppStoreAccountStore.mayRunUnchecked(kind: kind, release: release) { return .runAnyway }
        if kind == CLIContract.UnverifiedHelperKind.notBundled {
            return .refused("A release build of Graft runs only the UnfairPlay inside Graft.app as root. This one isn't that copy, so Graft won't run it.")
        }
        return .refused("Graft won't run a copy of UnfairPlay it can't check as root.")
    }

    // The root run's limits (the review's critic: no timeout, no cancel).

    /// "10 minutes", "1 minute", "45 seconds".
    static func duration(_ seconds: Int) -> String {
        guard seconds >= 60 else { return "\(seconds) seconds" }
        let minutes = (seconds + 59) / 60
        return minutes == 1 ? "1 minute" : "\(minutes) minutes"
    }

    /// Under the spinner while root runs.
    static func runningLine(limitSeconds: Int?) -> String {
        guard let limitSeconds else { return "This can take a few minutes." }
        return "This can take a few minutes; it stops by itself after \(duration(limitSeconds))."
    }

    static let stoppingLine = "Stopping…"
    static let stoppedLine = "Stopped: nothing was changed."

    /// UnfairPlay's words when root's copy of this Mac's FairPlay state
    /// (/private/var/db/fpsd/SC Info) has no SIDB. macOS cleared it, after a
    /// restart in the try15 owner round, and opening an iPhone or iPad app
    /// installed from the Mac App Store wrote it again, as UnfairPlay's own
    /// README says to do before setup.
    static let noSIDBState = "canonical input has no SIDB state"

    /// What to do about a failed setup's `message`, or nil when there's
    /// nothing to add: for UnfairPlay's "no SIDB state", open the app picked
    /// once, then set up again. The release body's known issue says the same.
    static func advice(for message: String, appName: String?) -> String? {
        guard message.contains(noSIDBState) else { return nil }
        return noStateAdvice(appName: appName)
    }

    /// The advice for this Mac's missing App Store state, whether UnfairPlay
    /// said so at its end or root found none before it started
    /// (`AppStoreAccountStore.Enrol.noFairPlayState`, exit 69).
    static func noStateAdvice(appName: String?) -> String {
        "This Mac's App Store state, which UnfairPlay reads, isn't there right now: macOS sometimes "
            + "clears it, even for an app you've already opened. Open \(appName ?? "the app you picked") once, "
            + "then set up again."
    }

    /// Before the password, under the explanation: the same state, before
    /// it can fail. A quiet line, with "Open <App>" (`openButton`) beside it.
    static func openFirstLine(appName: String) -> String {
        "Open \(appName) once before you continue: macOS sometimes clears the App Store state setup reads, and opening the app writes it again."
    }

    static func openButton(appName: String) -> String { "Open \(appName)" }

    /// What the sheet says about a failed setup: the advice first, when there
    /// is any, then the error as it came, which stays for a report.
    static func failedLine(_ message: String, appName: String?) -> String {
        advice(for: message, appName: appName).map { "\($0)\n\(message)" } ?? message
    }

    static func timedOutLine(limitSeconds: Int?) -> String {
        let limit = limitSeconds.map { " (\(duration($0)))" } ?? ""
        return "The setup ran past its time limit\(limit) and was stopped. Nothing was changed."
    }

    /// Whether Cancel (and Esc) can do anything now. While root runs it asks
    /// the run to stop, once, and only a CLI that named a cancel file can.
    static func cancelEnabled(_ enrol: AppStoreAccountStore.Enrol, stopping: Bool, canStop: Bool) -> Bool {
        switch enrol {
        case .preparing: return false
        case .running: return canStop && !stopping
        default: return true
        }
    }

    /// Which way the administrator step went (decision 6's record), shown
    /// small under the outcome so the hands-on check can read it.
    static func viaLine(_ via: AdminRun.Via?) -> String? {
        switch via {
        case .appleScript?: return "The administrator step ran in Graft (NSAppleScript)."
        case .osascript?: return "The administrator step ran through osascript."
        case .plain?, nil: return nil
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Metrics.sectionGap) {
            Text("Set up decryption on this Mac")
                .font(AppFont.bodyMedium)
                .foregroundStyle(Palette.text)
            Text(Self.intro)
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.textDim)
                .fixedSize(horizontal: false, vertical: true)
            switch account.enrol {
            case let .unchecked(refusal):
                uncheckedWarning(refusal)
            case let .ready(command):
                if !command.verified { uncheckedWarning(UncheckedHelper(command)) }
                explanation(command)
            default:
                choices
            }
            outcome
            if !account.isEnrolling, let reason = account.blockedBy() {
                Text(reason)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            buttons
        }
        .padding(Metrics.popoverGutter)
        .frame(width: 480)
        .onAppear { account.resetEnrol() }
        .task { await reload() }
        // Closing the sheet while root runs is a Cancel too.
        .onDisappear { account.stopEnrol() }
    }

    private func reload() async {
        await account.loadCandidates()
        if chosen == nil || !account.candidates.contains(where: { $0.path == chosen }) {
            chosen = account.candidates.first?.path
        }
    }

    @ViewBuilder
    private var buttons: some View {
        HStack {
            Spacer()
            Button(account.enrol == .done ? "Done" : "Cancel") {
                switch account.enrol {
                case .running:
                    // Not a dismiss: the sheet stays to say how it ended.
                    account.stopEnrol()
                    return
                case .ready, .unchecked: account.resetEnrol()
                default: break
                }
                dismiss()
            }
            .keyboardShortcut(.cancelAction)
            .disabled(!Self.cancelEnabled(
                account.enrol, stopping: account.isStoppingEnrol,
                canStop: account.runningCommand?.cancelFile != nil
            ))
            switch account.enrol {
            case .done:
                EmptyView()
            case let .unchecked(refusal):
                if Self.uncheckedOffer(kind: refusal.kind) == .runAnyway {
                    Button(Self.uncheckedButton, role: .destructive) {
                        guard let chosen else { return }
                        Task { await account.prepareEnrol(appPath: chosen, allowUncheckedHelper: true) }
                    }
                    .tint(Palette.danger)
                    .disabled(account.blockedBy() != nil)
                }
            case .ready:
                Button("Continue") { Task { await account.confirmEnrol() } }
                    .keyboardShortcut(.defaultAction)
                    .disabled(account.blockedBy() != nil)
            default:
                Button(account.isEnrolling ? "Setting Up…" : "Set Up Decryption…") {
                    guard let chosen else { return }
                    Task { await account.prepareEnrol(appPath: chosen) }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(chosen == nil || account.isEnrolling || account.blockedBy() != nil)
            }
        }
    }

    /// The CLI's own sentence, the path root would run, and, when this build
    /// won't offer the second step, why not. Outside the script's disclosure,
    /// and still showing over the explanation after the user has chosen to go on.
    private func uncheckedWarning(_ refusal: UncheckedHelper) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Symbols.failure.label(Self.uncheckedTitle)
                .font(AppFont.bodyMedium)
                .foregroundStyle(Palette.danger)
            Text(refusal.message)
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.text)
                .fixedSize(horizontal: false, vertical: true)
            if !refusal.helper.isEmpty {
                Text(Self.uncheckedHelperLine(refusal.helper))
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if case let .refused(why) = Self.uncheckedOffer(kind: refusal.kind) {
                Text(why)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.warn)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(Metrics.innerPadding)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.danger.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: Metrics.radiusInner))
        .accessibilityElement(children: .combine)
    }

    private func explanation(_ command: EnrollCommandRecord) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Self.explanation(verified: command.verified), id: \.self) { line in
                Text("• " + line)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.text)
                    .fixedSize(horizontal: false, vertical: true)
            }
            let name = account.appName(for: command)
            VStack(alignment: .leading, spacing: 4) {
                Text(Self.openFirstLine(appName: name))
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
                Button(Self.openButton(appName: name)) { account.openEnrolApp() }
            }
            DisclosureGroup("The exact command, run as root", isExpanded: $showScript) {
                ScrollView {
                    Text(command.script)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textDim)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: 160)
            }
            .font(AppFont.monoSmall)
        }
    }

    @ViewBuilder
    private var choices: some View {
        if !account.hasLoadedCandidates {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("Looking for apps…").font(AppFont.monoSmall).foregroundStyle(Palette.textDim)
            }
        } else if let error = account.candidatesError {
            Text(error).font(AppFont.monoSmall).foregroundStyle(Palette.danger).textSelection(.enabled)
        } else if account.candidates.isEmpty {
            Text(Self.noCandidates)
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.textMuted)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                Button("Open the Mac App Store") { MacAppStore.open() }
                Button("Check Again") { Task { await reload() } }
            }
        } else {
            Picker("App", selection: $chosen) {
                ForEach(account.candidates) { candidate in
                    Text(candidate.opened ? candidate.name : "\(candidate.name) (open it once first)")
                        .tag(Optional(candidate.path))
                }
            }
            .pickerStyle(.radioGroup)
            .disabled(account.isEnrolling)
        }
    }

    @ViewBuilder
    private var outcome: some View {
        switch account.enrol {
        case .done:
            if let via = Self.viaLine(account.lastAdminVia) {
                Text(via).font(AppFont.monoSmall).foregroundStyle(Palette.textMuted)
            }
            if store.status?.unfairplay.enrolled == true {
                Text("✓ This Mac is set up for decryption.").foregroundStyle(Palette.success)
            } else {
                Text(store.status?.unfairplay.modelProblem
                     ?? "Setup finished, but Graft doesn't see a model it can use. Try again.")
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.warn)
                    .fixedSize(horizontal: false, vertical: true)
            }
        case .cancelled:
            Text("Canceled: nothing was changed.").font(AppFont.monoSmall).foregroundStyle(Palette.textDim)
        case .stopped:
            Text(Self.stoppedLine).font(AppFont.monoSmall).foregroundStyle(Palette.textDim)
        case let .timedOut(limit):
            Text(Self.timedOutLine(limitSeconds: limit))
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.warn)
                .fixedSize(horizontal: false, vertical: true)
        case let .noFairPlayState(appName):
            Text(Self.noStateAdvice(appName: appName))
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.warn)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        case let .failed(message):
            Text(Self.failedLine(message, appName: account.candidates.first { $0.path == chosen }?.name))
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.danger)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        case .running:
            HStack(alignment: .top, spacing: 8) {
                ProgressView().controlSize(.small)
                VStack(alignment: .leading, spacing: 2) {
                    Text(account.isStoppingEnrol ? Self.stoppingLine : "Waiting for macOS, then UnfairPlay…")
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textDim)
                    Text(Self.runningLine(limitSeconds: account.runningCommand?.timeLimitSeconds))
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textMuted)
                        .fixedSize(horizontal: false, vertical: true)
                    if let stopError = account.stopError {
                        Text(stopError)
                            .font(AppFont.monoSmall)
                            .foregroundStyle(Palette.danger)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        default:
            EmptyView()
        }
    }
}

/// The Mac App Store app, opened by bundle id.
enum MacAppStore {
    static let bundleId = "com.apple.AppStore"

    static func open() {
        guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleId) else { return }
        NSWorkspace.shared.openApplication(at: url, configuration: NSWorkspace.OpenConfiguration())
    }
}
