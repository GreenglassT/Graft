import SwiftUI
import UniformTypeIdentifiers

/// Settings › App Store Connect: the API key Graft uses to make an app's
/// provisioning profile for it (`auto_profile`).
///
/// It used to be Terminal only — `graft --setup-portal`, an interactive
/// wizard. This section runs its non-interactive twin, `--set-portal`, which
/// does the same steps: check the key by listing devices, copy the .p8 beside
/// config.yaml, and write the section. A key App Store Connect turns away is
/// written nowhere, and the section says why under the form.
///
/// Setting a key is paid only, and shown disabled with the reason on a free
/// Apple ID, like the signing fields: there is no provisioning profile to make
/// without a paid membership. Removing a stored key is not (`offersRemove`).
struct AppStoreConnectSection: View {
    @ObservedObject var store: SettingsStore
    /// `ConfigStore.account`; nil disables nothing (`AccountMode`).
    let account: AccountMode?

    @State private var choosingKey = false
    @State private var confirmingRemove = false

    /// Where keys are made: Users and Access › Integrations › App Store
    /// Connect API. The wizard's first sentence names the same page.
    static let keysURL = URL(string: "https://appstoreconnect.apple.com/access/integrations/api")!

    /// What config.yaml says about the key, read off `--settings`.
    enum Status: Equatable {
        /// No settings yet, or a CLI too old to report the key.
        case unknown(String)
        case notSet
        case set(AppStoreConnectSummary)
    }

    /// What the section says while config.yaml doesn't parse. The CLI leaves
    /// the key out of `settings` then, on purpose (`_do_settings`), and read
    /// as "a CLI too old to report it" that told the user to update a graft
    /// that was current, under the notice saying the file is what's broken.
    static let parseErrorLine = "Shown once config.yaml parses again (see above)."

    static func status(of settings: SettingsRecord?) -> Status {
        guard let settings else { return .unknown("Shown once graft has read your config.") }
        if settings.parseError != nil { return .unknown(parseErrorLine) }
        guard settings.reportsAppStoreConnect else {
            return .unknown("This version of graft doesn't report its App Store Connect key. "
                + "Update it to set one here.")
        }
        guard let key = settings.appStoreConnect else { return .notSet }
        return .set(key)
    }

    /// How the result line reads: green, amber, or red.
    enum ResultTone: Equatable {
        case success, warning, failure
    }

    /// The line under the form after Save and test, or a Remove that warned
    /// or failed.
    static func resultLine(_ result: SettingsStore.PortalResult?) -> (text: String, tone: ResultTone)? {
        switch result {
        case nil:
            return nil
        case let .connected(devices, warnings):
            let connected = "✓ Connected. App Store Connect sees \(pluralized(devices, "iOS device"))."
            // Saved, and the CLI still had something to say: amber, so the
            // line about a leftover key isn't read as part of the all-clear.
            if warnings.isEmpty { return (connected, .success) }
            return (([connected] + warningLines(warnings)).joined(separator: "\n"), .warning)
        case let .removed(warnings):
            return (warningLines(warnings).joined(separator: "\n"), .warning)
        case let .failed(message):
            return (message, .failure)
        }
    }

    /// In the app pane's words for a CLI warning, one per line.
    private static func warningLines(_ warnings: [String]) -> [String] {
        warnings.map { "warning: \($0)" }
    }

    static let notSetLine = "Not set up. With a key, Graft can make an app's provisioning "
        + "profile for you."
    static let removeTitle = "Remove the App Store Connect key?"
    static let removeMessage = "Graft stops making provisioning profiles. Apps set to make "
        + "theirs automatically won't build until you add a key again. The .p8 file stays "
        + "in your config folder."

    /// On a free Apple ID: the key can't make a profile there, so removing it
    /// changes nothing about builds.
    static let removeMessageFree = "A free Apple ID can't use it, so your builds don't "
        + "change. The .p8 file stays in your config folder."

    static func removeMessage(account: AccountMode?) -> String {
        account.isFree ? removeMessageFree : removeMessage
    }

    /// Remove… is offered on any account, unlike the form. Setting a key is
    /// paid-only, but the CLI deliberately allows `--clear-portal` on a free
    /// config: a key left over from a paid membership is the user's to take
    /// out. Only a save in flight holds it.
    static func offersRemove(saving: Bool) -> Bool { !saving }

    private var paidReason: String? { account.isFree ? PaidOnly.appStoreConnect : nil }

    var body: some View {
        VStack(alignment: .leading, spacing: Metrics.titleGap) {
            HStack(alignment: .firstTextBaseline) {
                SectionTitle(text: "App Store Connect")
                Spacer()
                Link("Open App Store Connect", destination: Self.keysURL)
                    .font(AppFont.label)
                    .help(Self.keysURL.absoluteString)
            }
            VStack(alignment: .leading, spacing: Metrics.rowGap) {
                current
                // The form only for a CLI that reports the key: one that
                // doesn't predates `--set-portal`, and Save would fail with a
                // usage error. The sentence above says to update it.
                if Self.offersForm(Self.status(of: store.settings)) {
                    form
                }
                if let paidReason {
                    Text(paidReason)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textDim)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(Metrics.cardPadding)
            .cardStyle()
        }
        .fileImporter(
            isPresented: $choosingKey,
            allowedContentTypes: [UTType(filenameExtension: "p8") ?? .data],
            allowsMultipleSelection: false
        ) { result in
            guard case let .success(urls) = result, let url = urls.first else { return }
            store.portalForm.keyFile = url.path
            if store.portalForm.keyId.trimmingCharacters(in: .whitespaces).isEmpty,
               let id = SettingsStore.PortalForm.keyId(fromFilename: url.lastPathComponent) {
                store.portalForm.keyId = id
            }
        }
        .alert(Self.removeTitle, isPresented: $confirmingRemove) {
            Button("Cancel", role: .cancel) {}
            Button("Remove", role: .destructive) {
                Task { await store.clearPortal() }
            }
        } message: {
            Text(Self.removeMessage(account: account))
        }
    }

    private static func color(of tone: ResultTone) -> Color {
        switch tone {
        case .success: return Palette.success
        case .warning: return Palette.warn
        case .failure: return Palette.danger
        }
    }

    static func offersForm(_ status: Status) -> Bool {
        if case .unknown = status { return false }
        return true
    }

    /// Key ID, Issuer ID, the .p8, and Save and test, with its outcome.
    @ViewBuilder
    private var form: some View {
        Group {
            field("Key ID", $store.portalForm.keyId, prompt: "10 characters")
            field("Issuer ID", $store.portalForm.issuerId, prompt: "shown above your keys")
            keyFileRow
            HStack {
                Spacer()
                Button(store.isSavingPortal ? "Testing…" : "Save and test") {
                    let form = store.portalForm
                    Task {
                        await store.setPortal(
                            keyId: form.keyId, issuerId: form.issuerId, keyFile: form.keyFile
                        )
                    }
                }
                .controlSize(.small)
                .disabled(!store.portalForm.isReady || store.isSavingPortal)
            }
        }
        .disabled(store.isSavingPortal)
        .paidOnly(paidReason)
        if let line = Self.resultLine(store.portalResult) {
            Text(line.text)
                .font(AppFont.monoSmall)
                .foregroundStyle(Self.color(of: line.tone))
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// What is set now, and Remove.
    @ViewBuilder
    private var current: some View {
        switch Self.status(of: store.settings) {
        case let .unknown(sentence):
            Text(sentence)
                .font(AppFont.body)
                .foregroundStyle(Palette.textDim)
        case .notSet:
            Text(Self.notSetLine)
                .font(AppFont.body)
                .foregroundStyle(Palette.textDim)
                .fixedSize(horizontal: false, vertical: true)
        case let .set(key):
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Key \(key.keyId ?? "?") · issuer \(key.issuerId ?? "?")")
                        .font(AppFont.body)
                        .foregroundStyle(Palette.text)
                        .textSelection(.enabled)
                    if let file = key.keyFile {
                        Text(file)
                            .font(AppFont.monoSmall)
                            .foregroundStyle(Palette.textDim)
                            .textSelection(.enabled)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                    if !key.keyFileExists {
                        Text("The .p8 file is missing. Choose it again below.")
                            .font(AppFont.monoSmall)
                            .foregroundStyle(Palette.warn)
                    }
                }
                Spacer(minLength: 0)
                Button("Remove…") { confirmingRemove = true }
                    .controlSize(.small)
                    // Not `.paidOnly`: see `offersRemove`.
                    .disabled(!Self.offersRemove(saving: store.isSavingPortal))
            }
        }
    }

    private func label(_ text: String) -> some View {
        Text(text)
            .font(AppFont.label).labelCaps()
            .foregroundStyle(Palette.textDim)
            .frame(width: 150, alignment: .leading)
    }

    private func field(_ title: String, _ value: Binding<String>, prompt: String) -> some View {
        HStack(spacing: Metrics.cardPadding) {
            label(title)
            TextField("", text: value, prompt: Text(prompt))
                .textFieldStyle(.roundedBorder)
                .font(AppFont.monoSmall)
        }
    }

    private var keyFileRow: some View {
        HStack(spacing: Metrics.cardPadding) {
            label("Key file (.p8)")
            Text(store.portalForm.keyFile.isEmpty ? "None chosen" : store.portalForm.keyFile)
                .font(AppFont.monoSmall)
                .foregroundStyle(store.portalForm.keyFile.isEmpty ? Palette.textDim : Palette.text)
                .lineLimit(1)
                .truncationMode(.middle)
            Spacer(minLength: 0)
            Button("Choose…") { choosingKey = true }
                .controlSize(.small)
        }
    }
}
