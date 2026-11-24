import SwiftUI

/// Settings › App Store: whether this Mac can get and decrypt App Store apps.
///
/// Two rows — ipatool (installed, version, who is signed in, and the 2.8.1
/// warning) and UnfairPlay (installed, version, enrolled, and why a model
/// can't be used) — and, when a tool is missing, the Terminal commands that
/// fix it, for copying.
///
/// **Signing in and enrolling happen here.** Sign In… opens
/// `AppStoreSignInSheet`, and so does Sign In Again… beside Sign Out; the
/// Apple ID, the password and the code go to the CLI's stdin and are never
/// kept. Set Up Decryption… runs the CLI's
/// enrollment script behind macOS's administrator prompt
/// (`AppStoreEnrollSheet`). The Terminal block remains only for a missing
/// tool, which in Graft.app means a damaged bundle.
struct AppStoreSettingsSection: View {
    let store: AppStoreStore
    @State private var showSignIn = false
    @State private var showEnroll = false

    /// What the section shows, read off the store's three load facts.
    enum State: Equatable {
        case checking
        case failed(String)
        case loaded(AppStoreStatusRecord)
    }

    /// Decided on `hasLoaded`, never on `status == nil`: nil is both "not
    /// asked yet" and "asked, and it failed", and the second must say why
    /// rather than claim to still be checking (memory
    /// `empty-collection-is-not-a-load-state`).
    static func state(hasLoaded: Bool, status: AppStoreStatusRecord?, error: String?) -> State {
        if let status { return .loaded(status) }
        guard hasLoaded else { return .checking }
        return .failed(error ?? "graft didn't report an App Store status")
    }

    struct Row: Equatable {
        let name: String
        let ok: Bool
        let detail: String
        let warning: String?
        /// The CLI's one-string install command for this tool
        /// (`appstore-status`'s `install`), which the row's Copy puts on the
        /// pasteboard: the command, and nothing to edit out of it first. The
        /// "Run in Terminal" block below still holds every step, comments and
        /// all, for its own Copy.
        var install: String? = nil
        /// Status is being read again: the row says so, and shows nothing of
        /// the last answer, so a stale "signed in" never shows while a new
        /// read runs (the owner's try12 run: green for a second, then legacy).
        var checking = false
    }

    /// A row's detail while status is read again.
    static let checkingDetail = "Checking\u{2026}"

    /// Under the ipatool row when status couldn't tell whether it is signed
    /// in (not a legacy sign-in): the CLI's own sentence (`unknown_message`),
    /// else this app's words for a reason it knows, else the reason itself.
    static func unknownLine(_ ipa: AppStoreStatusRecord.IPATool) -> String? {
        guard ipa.present, ipa.signedIn == nil, ipa.legacySignin != true else { return nil }
        if let message = ipa.unknownMessage, !message.isEmpty { return message }
        switch ipa.unknownReason {
        case CLIContract.UnknownSignIn.keychainNoAnswer?:
            return "Your login keychain didn't answer when Graft checked it for ipatool's sign-in. Unlock it in Keychain Access, then Refresh."
        case CLIContract.UnknownSignIn.noVerdict?:
            return "ipatool didn't say whether it is signed in. Refresh, or run \(ipa.path) auth info in Terminal to see why."
        case CLIContract.UnknownSignIn.graftIpatoolSignin?:
            return "Graft.app's own ipatool holds this App Store sign-in, and the ipatool at \(ipa.path) can't read it without Keychain dialogs. Sign In\u{2026} gives that ipatool its own sign-in."
        case let reason?:
            return "Graft couldn't tell whether ipatool is signed in (\(reason))."
        case nil:
            return nil
        }
    }

    /// How the last Set Up Decryption run ended, under the rows once its
    /// sheet has closed. In the owner's try12 run, Cancel in macOS's dialog
    /// closed the sheet too, so its "Canceled: nothing was changed." was
    /// never seen; this line doesn't depend on the sheet staying up. nil for
    /// a run that worked (the UnfairPlay row says enrolled) and before any.
    static func enrolOutcomeLine(_ enrol: AppStoreAccountStore.Enrol) -> String? {
        switch enrol {
        case .cancelled: return "Setting up decryption was canceled: nothing was changed."
        case .stopped: return "Setting up decryption was stopped: nothing was changed."
        case let .timedOut(limit): return AppStoreEnrollSheet.timedOutLine(limitSeconds: limit)
        case let .failed(message):
            // The error first, after the colon, then the sheet's advice on a
            // line of its own, so neither reads as a sentence spliced in.
            let advice = AppStoreEnrollSheet.advice(for: message, appName: nil).map { "\n\($0)" } ?? ""
            return "Setting up decryption failed: \(message)\(advice)"
        default: return nil
        }
    }

    private static func account(_ ipa: AppStoreStatusRecord.IPATool) -> String {
        // Only Graft.app's own ipatool guesses Graft 1, as the CLI's status
        // line and the sign-in sheet do (`AppStoreSignInSheet.legacyText`):
        // any other, or a CLI too old to name its source, can't read a
        // sign-in another ipatool made, Graft 1's or not (delta audit try15,
        // d15-app-1).
        if ipa.legacySignin == true {
            guard ipa.source == CLIContract.ToolSource.bundled else {
                return "a sign-in this ipatool can't read (another ipatool's?): sign in again"
            }
            return "signed in by another ipatool (Graft 1's?): sign in again"
        }
        switch ipa.signedIn {
        case true?: return ipa.email.map { "signed in as \($0)" } ?? "signed in"
        case false?: return "not signed in"
        case nil: return "couldn't tell whether it's signed in"
        }
    }

    enum AccountAction: Equatable { case signIn, signOut }

    /// The buttons beside the ipatool row, in order.
    enum AccountButton: Equatable {
        case signIn
        /// The same sheet as Sign In…, while signed in (fix-check #13).
        case signInAgain
        case signOut

        var label: String {
            switch self {
            case .signIn: return CLIContract.SettingsLabel.signIn
            case .signInAgain: return CLIContract.SettingsLabel.signInAgain
            case .signOut: return "Sign Out"
            }
        }
    }

    /// Signed in, Sign In Again… comes with Sign Out: an expired sign-in
    /// whose renewal needs a 2FA code still reads as signed in (ipatool's
    /// `auth info` reads only what it stored), and the CLI's hint then sends
    /// the user here to sign in again, over the stored one (fix-check #13).
    static func accountButtons(for status: AppStoreStatusRecord) -> [AccountButton] {
        switch accountAction(for: status) {
        case .signIn?: return [.signIn]
        case .signOut?: return [.signInAgain, .signOut]
        case nil: return []
        }
    }

    static let signInAgainHelp = "Sign in again over the current sign-in: for a sign-in that has expired, or that Apple wants a new code for."

    /// Whether Sign In…, Sign In Again…, Sign Out and Set Up… can be pressed
    /// now. Not while status is read again: the rows say Checking…, and the
    /// buttons still stand for the last answer. Not while a get or Get (free)
    /// runs (`AppStoreStore.accountBlockedReason`, which their help says).
    /// Sign Out also waits for a sign-out already running.
    static func accountActionsEnabled(checking: Bool, blockedReason: String?) -> Bool {
        !checking && blockedReason == nil
    }

    /// Sign In… or Sign Out beside the ipatool row, or neither: nothing to
    /// sign in with, or ipatool couldn't say either way.
    static func accountAction(for status: AppStoreStatusRecord) -> AccountAction? {
        let ipa = status.ipatool
        guard ipa.present else { return nil }
        if ipa.legacySignin == true { return .signIn }
        switch ipa.signedIn {
        case true?: return .signOut
        case false?: return .signIn
        // The CLI's own answer that another ipatool can't read Graft's
        // sign-in: its sentence says Sign In… fixes that, so offer it.
        case nil: return ipa.unknownReason == CLIContract.UnknownSignIn.graftIpatoolSignin ? .signIn : nil
        }
    }

    enum EnrollAction: Equatable { case setUp, setUpAgain }

    /// The button beside the UnfairPlay row, when it's there: Set Up
    /// Decryption… with no model this user can use, and Set Up Again… with
    /// one. The model works only for the Apple ID the Mac App Store installed
    /// its app with, so after a switch of Apple ID every decrypt fails; Set Up
    /// Again… is the way out that needs no Terminal (the review's critic).
    static func enrollAction(_ status: AppStoreStatusRecord) -> EnrollAction? {
        guard status.unfairplay.present else { return nil }
        return status.unfairplay.enrolled ? .setUpAgain : .setUp
    }

    static let setUpAgainHelp = "Enroll this Mac again: needed after the Mac App Store, or Graft, changes to another Apple ID."

    /// The Terminal block is for a tool that isn't there. Signing in and
    /// enrolling have buttons, so their command lines aren't shown.
    static func showsTerminalCommands(_ status: AppStoreStatusRecord) -> Bool {
        !status.setupCommands.isEmpty && (!status.ipatool.present || !status.unfairplay.present)
    }

    /// A row's warning: each thing it has to say, one per line, or nil.
    /// What the CLI says goes under the row it is about, in its own words:
    /// a broken version, why status couldn't tell, a model that can't be
    /// used, and a configured path that gave way to Graft.app's own copy
    /// (`note`, delta audit try13, #7), which the CLI used to send only as a
    /// log line the app never showed.
    static func lines(_ parts: [String?]) -> String? {
        parts.compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: "\n").nilIfEmpty
    }

    static func rows(for status: AppStoreStatusRecord, checking: Bool = false) -> [Row] {
        let ipa = status.ipatool
        let up = status.unfairplay
        if checking {
            return [
                Row(name: "ipatool", ok: false, detail: checkingDetail, warning: nil, checking: true),
                Row(name: "UnfairPlay", ok: false, detail: checkingDetail, warning: nil, checking: true),
            ]
        }
        return [
            Row(
                name: "ipatool",
                // Signed in is not enough: 2.8.1 signs in and then cannot
                // download, so a warning keeps the dot red.
                ok: ipa.present && ipa.signedIn == true && ipa.warning == nil,
                detail: ipa.present
                    ? "\(ipa.version ?? "unknown version") · \(Self.account(ipa))"
                    : "not found at \(ipa.path)",
                warning: Self.lines([ipa.warning, unknownLine(ipa), ipa.note]),
                install: ipa.install
            ),
            Row(
                name: "UnfairPlay",
                ok: up.present && up.enrolled,
                detail: up.present
                    ? "\(up.version ?? "unknown version") · \(up.enrolled ? "enrolled" : "not enrolled")"
                    : "not found at \(up.path)",
                warning: Self.lines([up.modelProblem, up.note]),
                install: up.install
            ),
        ]
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                SectionTitle(text: "App Store")
                Spacer()
                Button {
                    Task { await store.loadStatus() }
                } label: {
                    if store.isLoadingStatus {
                        HStack(spacing: 6) {
                            ProgressView().controlSize(.small)
                            Text("Checking…")
                        }
                    } else {
                        Symbols.refresh.label("Refresh")
                    }
                }
                .disabled(store.isLoadingStatus)
            }
            content
        }
        // Here, not inside the loaded status: the sheet reads status again
        // as it opens, and a read that fails must not take the sheet away.
        .sheet(isPresented: $showSignIn) {
            AppStoreSignInSheet(store: store)
        }
        // Once per store, not per appearance: the store is ContentView's and
        // outlives this pane, and Refresh is there for "I just ran the command".
        .task {
            if !store.hasLoadedStatus { await store.loadStatus() }
        }
    }

    @ViewBuilder
    private var content: some View {
        switch Self.state(
            hasLoaded: store.hasLoadedStatus, status: store.status, error: store.statusError
        ) {
        case .checking:
            Text("Checking ipatool and UnfairPlay…")
                .font(AppFont.body)
                .foregroundStyle(Palette.textDim)
                .padding(Metrics.cardPadding)
                .frame(maxWidth: .infinity, alignment: .leading)
                .cardStyle()
        case let .failed(message):
            Text(message)
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.danger)
                .textSelection(.enabled)
                .padding(Metrics.cardPadding)
                .frame(maxWidth: .infinity, alignment: .leading)
                .cardStyle()
        case let .loaded(status):
            VStack(spacing: Metrics.rowGap) {
                ForEach(Self.rows(for: status, checking: store.isLoadingStatus), id: \.name) { row in
                    StatusRow(row: row, accessory: accessory(for: row, in: status))
                }
                if !showEnroll, let outcome = Self.enrolOutcomeLine(store.account.enrol) {
                    Text(outcome)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(store.account.enrol == .cancelled || store.account.enrol == .stopped
                                         ? Palette.textDim : Palette.warn)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                // Sign In, Sign Out and Set Up wait for a running get: say why
                // they're grayed out.
                if let reason = store.accountBlockedReason {
                    Text(reason)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textMuted)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                if let error = store.account.signOutError {
                    Text(error)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.danger)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                if Self.showsTerminalCommands(status) {
                    SetupCommands(commands: status.setupCommands)
                }
            }
            .sheet(isPresented: $showEnroll) {
                AppStoreEnrollSheet(account: store.account, store: store)
            }
        }
    }

    private func accessory(for row: Row, in status: AppStoreStatusRecord) -> AnyView? {
        if row.name == "ipatool" { return AnyView(accountButton(status)) }
        if row.name == "UnfairPlay", let action = Self.enrollAction(status) {
            return AnyView(
                Button(action == .setUp ? CLIContract.SettingsLabel.setUpDecryption : CLIContract.SettingsLabel.setUpAgain) {
                    showEnroll = true
                }
                    .disabled(!actionsEnabled)
                    .help(store.accountBlockedReason ?? (action == .setUpAgain ? Self.setUpAgainHelp : ""))
            )
        }
        return nil
    }

    private var actionsEnabled: Bool {
        Self.accountActionsEnabled(checking: store.isLoadingStatus, blockedReason: store.accountBlockedReason)
    }

    private func accountButton(_ status: AppStoreStatusRecord) -> some View {
        HStack(spacing: Metrics.controlGap) {
            ForEach(Self.accountButtons(for: status), id: \.label) { button in
                switch button {
                case .signIn, .signInAgain:
                    Button(button.label) { showSignIn = true }
                        .disabled(!actionsEnabled)
                        .help(store.accountBlockedReason ?? (button == .signInAgain ? Self.signInAgainHelp : ""))
                case .signOut:
                    Button(store.account.isSigningOut ? "Signing Out…" : button.label) {
                        Task { await store.account.signOut() }
                    }
                    .disabled(store.account.isSigningOut || !actionsEnabled)
                    .help(store.accountBlockedReason ?? "")
                }
            }
        }
    }

    private struct StatusRow: View {
        let row: Row
        var accessory: AnyView? = nil

        var body: some View {
            HStack(alignment: .top, spacing: 12) {
                if row.checking {
                    ProgressView()
                        .controlSize(.mini)
                        .frame(width: 9, height: 9)
                        .padding(.top, 6)
                } else {
                    Circle()
                        .fill(row.ok ? Palette.success : Palette.danger)
                        .frame(width: 9, height: 9)
                        .padding(.top, 6)
                }
                VStack(alignment: .leading, spacing: 4) {
                    Text(row.name)
                        .font(AppFont.bodyMedium)
                        .foregroundStyle(Palette.text)
                    if row.checking {
                        // Plain, not selectable: a distinct view, so the last
                        // answer's selectable text goes with it. Kept as one
                        // text whose string changes, the accessibility tree
                        // still read the last answer beside "Checking…".
                        Text(row.detail)
                            .font(AppFont.monoSmall)
                            .foregroundStyle(Palette.textMuted)
                    } else {
                        Text(row.detail)
                            .font(AppFont.monoSmall)
                            .foregroundStyle(Palette.textMuted)
                            .textSelection(.enabled)
                    }
                    if let warning = row.warning {
                        Text(warning)
                            .font(AppFont.monoSmall)
                            .foregroundStyle(Palette.warn)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 0)
                if let accessory { accessory }
                if let install = row.install {
                    Button("Copy install command") { Pasteboard.copy(install) }
                        .buttonStyle(.borderless)
                        .font(AppFont.label)
                        .foregroundStyle(Palette.accent)
                        .help(install)
                }
            }
            .padding(Metrics.cardPadding)
            .cardStyle()
        }
    }

    /// The commands to run in Terminal, as the CLI wrote them. Lines starting
    /// `#` are instructions the CLI could not reduce to one command (fetch a
    /// release, build from source); they stay comments so the whole block is
    /// safe to paste.
    private struct SetupCommands: View {
        let commands: [String]

        var body: some View {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text("Run in Terminal")
                        .font(AppFont.label)
                        .labelCaps()
                        .foregroundStyle(Palette.textDim)
                    Spacer()
                    Button("Copy") { Pasteboard.copy(commands.joined(separator: "\n")) }
                        .buttonStyle(.borderless)
                        .font(AppFont.label)
                        .foregroundStyle(Palette.accent)
                }
                ForEach(commands, id: \.self) { command in
                    Text(command)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(command.hasPrefix("#") ? Palette.textDim : Palette.warn)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(Metrics.innerPadding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Palette.surface2.opacity(0.5))
            .clipShape(RoundedRectangle(cornerRadius: Metrics.radiusInner))
        }
    }
}

private extension String {
    var nilIfEmpty: String? { isEmpty ? nil : self }
}
