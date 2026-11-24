import SwiftUI

/// Settings › App Store › Sign In…: Apple ID and password, then the 2FA code
/// if Apple asks for one. It may not: Apple asks only when it doesn't already
/// trust this Mac (controller decision 8).
///
/// The password and code live in `@State` strings only while they are
/// typed. On submit each is turned into bytes, the field is cleared at once,
/// and the bytes go to `AppStoreAccountStore`, which wipes them as it sends
/// them to the CLI's stdin. Both fields are `SecretField`s: no undo, no copy,
/// no cut. Closing the sheet clears both and stops a sign-in still running.
///
/// **Which ipatool, read as it opens** (fix-check #10). The sheet reads
/// status again first (`AppStoreStore.checkBeforeSignIn`), and Sign In waits
/// for that. The "goes to another ipatool" note and the Graft 1.x note come
/// from that read, and so does `--expect-ipatool`, the CLI's refusal to sign
/// in through any other ipatool. After such a refusal ("ipatool-changed"),
/// the CLI's sentence shows, and status, read again, names the new one.
struct AppStoreSignInSheet: View {
    let store: AppStoreStore
    private var account: AppStoreAccountStore { store.account }
    /// From the status read since the sheet opened; nil until then.
    private var ipatool: AppStoreStatusRecord.IPATool? { store.signInIpatool }
    /// What signing in does to a sign-in this ipatool can't read
    /// (`legacy_signin`), when there is one.
    private var legacy: String? { ipatool.flatMap(Self.legacyText(for:)) }
    /// Shown above the fields when the password would go to an ipatool that
    /// isn't Graft's own.
    private var elsewhere: String? { ipatool.flatMap(Self.elsewhereNote(for:)) }

    @State private var email = ""
    @State private var password = ""
    @State private var code = ""
    @Environment(\.dismiss) private var dismiss

    static let explainer = "Graft passes your Apple ID, your password and, if Apple asks for one, the code it sends to your devices to ipatool, the tool that downloads from the App Store. Graft saves none of them. ipatool, which comes with Graft, keeps your sign-in, including your password, in your login Keychain, as it does when you use it on its own."
    /// Controller decision 2: said before the user signs in, when Graft.app's
    /// own ipatool signs in.
    static let legacyNote = "This also signs out any ipatool you installed yourself; it will need to sign in again if you use it in Terminal. (Graft 1 signed in with your own copy of ipatool, and macOS lets only that copy read its sign-in, so Graft removes it first.)"

    /// The same, for any other ipatool: the CLI's status line for it
    /// (`appstore.status`, its `elif legacy:` branch).
    static func otherLegacyNote(path: String) -> String {
        "The ipatool at \(path) can't read the App Store sign-in in your login keychain (another ipatool made it, or a Keychain dialog was denied). Signing in with it saves over that sign-in, and macOS may ask for your login keychain password while it does."
    }

    /// What the sheet says about a sign-in this ipatool can't read, or nil
    /// when status reported none (the try14 delta audit, F2). Only Graft.app's
    /// own ipatool deletes that sign-in before it signs in. Any other one,
    /// config.yaml's or one found on PATH, deletes only the items Graft's
    /// record vouches for (`appstore_account.needs_clearing`), and this
    /// sign-in isn't one of them, so it saves over it. With no source named
    /// (a CLI too old to say), nothing is promised either.
    static func legacyText(for ipatool: AppStoreStatusRecord.IPATool) -> String? {
        guard ipatool.legacySignin == true else { return nil }
        guard ipatool.source == CLIContract.ToolSource.bundled else { return otherLegacyNote(path: ipatool.path) }
        return legacyNote
    }

    /// Decision 9: after a wrong password ipatool still asks for a code, and
    /// Apple sends none. So the prompt never promises one.
    static let codePrompt = "Apple asked for a verification code. Enter the 6-digit code it shows on your iPhone, iPad or other Mac. If no code appears, the password may be wrong: Cancel, check it and sign in again."
    /// While the sheet's own status read runs.
    static let checkingLine = "Checking which ipatool signs in…"

    /// What stands in for the note while the sheet can't yet say which
    /// ipatool gets the password: the read is running, or it failed (with
    /// why). nil once an ipatool is named.
    static func checkLine(checked: Bool, ipatool: AppStoreStatusRecord.IPATool?, statusError: String?) -> String? {
        guard ipatool == nil else { return nil }
        guard checked else { return checkingLine }
        return "Graft couldn't check which ipatool signs in: \(statusError ?? "no App Store status"). Cancel and try again."
    }

    /// Where the Apple ID and the password go, when that isn't Graft's own
    /// ipatool (the review's critic: the sheet never said which binary gets
    /// the password). nil for the bundled one, and when the CLI didn't say.
    static func elsewhereNote(for ipatool: AppStoreStatusRecord.IPATool) -> String? {
        guard let source = ipatool.source, source != CLIContract.ToolSource.bundled else { return nil }
        let lead = "Your Apple ID and password go to the ipatool at \(ipatool.path), not Graft's own copy."
        switch source {
        case CLIContract.ToolSource.config:
            return lead + " config.yaml names it (appstore.ipatool), and that setting comes before Graft's own copy."
        case CLIContract.ToolSource.defaultPath, CLIContract.ToolSource.path:
            return lead + " This Graft has no ipatool of its own, so it uses the one it found on PATH or in ~/.local/bin."
        default:
            return lead
        }
    }

    static func canSignIn(email: String, password: String) -> Bool {
        appleID(email) != nil && !password.isEmpty && passwordProblem(password) == nil
    }

    /// What goes to the CLI as the Apple ID, or nil for something that is
    /// neither an email nor a phone number (review part 4, #21: the CLI and
    /// ipatool take either). An email is sent as typed, trimmed of spaces and
    /// line breaks. A phone number ("+" optional, then digits, spaces and
    /// dashes, 7 to 15 digits) goes as its digits, with the "+": the CLI
    /// refuses a space in the line.
    static func appleID(_ typed: String) -> String? {
        let trimmed = typed.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.contains("@") {
            return trimmed.contains(where: { $0.isWhitespace }) ? nil : trimmed
        }
        guard trimmed.range(of: #"^\+?[0-9][0-9 -]*[0-9]$"#, options: .regularExpression) != nil else { return nil }
        let digits = trimmed.filter { $0.isASCII && $0.isNumber }
        guard (7...15).contains(digits.count) else { return nil }
        return (trimmed.hasPrefix("+") ? "+" : "") + digits
    }

    /// Why this password can't be sent, or nil.
    static func passwordProblem(_ password: String) -> String? {
        password.contains(where: \.isNewline) ? "A password can't contain a line break." : nil
    }

    /// The ASCII digits of what was typed: a code pasted as "482 913" still counts.
    static func codeBytes(_ typed: String) -> [UInt8] {
        Array(typed.filter { $0.isASCII && $0.isNumber }.utf8)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Metrics.sectionGap) {
            Text("Sign in to the App Store")
                .font(AppFont.bodyMedium)
                .foregroundStyle(Palette.text)
            Text(Self.explainer)
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.textDim)
                .fixedSize(horizontal: false, vertical: true)
            if let line = Self.checkLine(checked: store.hasCheckedForSignIn, ipatool: ipatool, statusError: store.statusError) {
                Text(line)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let elsewhere {
                Text(elsewhere)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.warn)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let legacy {
                Text(legacy)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.warn)
                    .fixedSize(horizontal: false, vertical: true)
            }
            TextField("Apple ID (email or phone)", text: $email)
                .textFieldStyle(.roundedBorder)
                .textContentType(.username)
                .disabled(account.isSigningIn)
            if account.signIn == .needsCode {
                Text(Self.codePrompt)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
                SecretField(placeholder: "6-digit code", text: $code, onSubmit: submitCode)
                    .frame(height: 22)
            } else {
                SecretField(placeholder: "Password", text: $password, onSubmit: submitPassword)
                    .frame(height: 22)
                    .disabled(account.isSigningIn)
                if let problem = Self.passwordProblem(password) {
                    Text(problem).font(AppFont.monoSmall).foregroundStyle(Palette.warn)
                }
            }
            progress
            if !account.isSigningIn, let reason = account.blockedBy() {
                Text(reason)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Spacer()
                Button("Cancel") {
                    if account.isSigningIn { account.cancelSignIn() }
                    dismiss()
                }
                .keyboardShortcut(.cancelAction)
                if account.signIn == .needsCode {
                    Button("Verify", action: submitCode)
                        .keyboardShortcut(.defaultAction)
                        .disabled(Self.codeBytes(code).count != 6)
                } else {
                    Button("Sign In", action: submitPassword)
                        .keyboardShortcut(.defaultAction)
                        .disabled(account.isSigningIn || account.blockedBy() != nil
                                  || ipatool == nil || store.isLoadingStatus
                                  || !Self.canSignIn(email: email, password: password))
                }
            }
        }
        .padding(Metrics.popoverGutter)
        .frame(width: 440)
        .onAppear { account.resetSignIn() }
        .task { await store.checkBeforeSignIn() }
        .onDisappear {
            password = ""
            code = ""
            if account.isSigningIn { account.cancelSignIn() }
        }
        .onChange(of: account.signIn) { _, state in
            if case .signedIn = state { dismiss() }
        }
    }

    @ViewBuilder
    private var progress: some View {
        switch account.signIn {
        case let .working(line):
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(line)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
                    .fixedSize(horizontal: false, vertical: true)
            }
        case let .failed(message, hint):
            Text(message)
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.danger)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            // What to do about it, from the CLI (whole-diff review, signin-4):
            // without it, "sign in again and choose Allow" and the like
            // reached only Terminal.
            if let hint, !hint.isEmpty {
                Text(hint)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
        default:
            EmptyView()
        }
    }

    private func submitPassword() {
        guard !account.isSigningIn, !store.isLoadingStatus, Self.canSignIn(email: email, password: password),
              let appleID = Self.appleID(email) else { return }
        var bytes = Array(password.utf8)
        password = ""
        store.startSignIn(email: appleID, password: &bytes)
    }

    private func submitCode() {
        var bytes = Self.codeBytes(code)
        code = ""
        guard bytes.count == 6 else {
            Secret.wipe(&bytes)
            return
        }
        account.submitCode(&bytes)
    }
}
