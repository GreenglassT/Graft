import SwiftUI
import UniformTypeIdentifiers

/// The Apple account, set up in the app: what `graft --init` asks in Terminal,
/// as a few screens. Every screen is a state of `AccountSetupStore`, and this
/// only draws it.
///
/// On the first-run screen (`SetupView`) it starts at Welcome. In Settings
/// › Account it is a sheet that starts at the form or at the result, with
/// Cancel (`onCancel`).
struct AccountSetupView: View {
    @Bindable var store: AccountSetupStore
    /// The done screen's button: load the app, or close the Settings sheet.
    let onFinished: () -> Void
    /// Settings' Cancel. Nil on the first-run screen.
    var onCancel: (() -> Void)? = nil
    /// The first-run screen's check is still running: Start using Graft waits
    /// for it (ContentView finishes only once it has run).
    var finishing: Bool = false

    @State private var choosing: Choice = .p12
    @State private var showingImporter = false

    enum Choice { case p12, profile }

    static let p12Type = UTType(filenameExtension: "p12") ?? .data
    static let profileType = UTType(filenameExtension: "mobileprovision") ?? .data

    static let welcomeTitle = "Welcome to Graft"
    static let welcomeBody = "Graft adds tweaks to iOS apps and installs them on your iPhone "
        + "or iPad. First, tell it which kind of Apple account you have."
    static let startLabel = "Get started"
    static let questionTitle = "Do you have a paid Apple Developer account?"
    static let questionBody = "With a paid Apple Developer Program membership, Graft signs "
        + "and installs apps itself. With a free Apple ID, Graft delivers apps into "
        + "LiveContainer, which SideStore or iloader keeps installed for you."
    static let freeLabel = "No, I have a free Apple ID"
    static let paidLabel = "Yes"
    /// What VoiceOver's Buttons rotor reads for `paidLabel`, which on its own
    /// names no question.
    static let paidAccessibilityLabel = "Yes, I have a paid Apple Developer account"
    static let formTitle = "Your signing certificate and profile"
    static let p12Note = "Export your Apple Development certificate, with its private key, "
        + "from Keychain Access as a .p12. The password is the one you set then; leave it "
        + "empty if you set none. Graft keeps it in your login keychain, never in a file."
    static let profileNote = "A development provisioning profile from developer.apple.com, "
        + "for the devices you install on."
    static let reviewTitle = "Check these before Graft sets up signing"
    static let setUpLabel = "Set up"
    /// Cancel's place while a setup writes: it can't be stopped halfway.
    static let settingUpLabel = "Setting up…"
    static let warningsTitle = "Worth knowing"
    static let finishLabel = "Start using Graft"
    static let doneLabel = "Done"
    static let freeStepsIntro = "To finish, on your iPhone or iPad:"

    var body: some View {
        VStack(alignment: .leading, spacing: Metrics.sectionGap) {
            switch store.step {
            case .welcome: welcome
            case .question: question
            case .paidForm: paidForm
            case let .paidReview(result): review(result)
            case let .done(result): done(result)
            }
            if let error = store.error {
                Text(error)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.danger)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        // A refusal appears under the form with nothing else changing, so
        // VoiceOver says it (WCAG 4.1.3); the field it cleared is silent.
        .onChange(of: store.error) { _, error in
            if let error { AccessibilityNotification.Announcement(error).post() }
        }
        .fileImporter(
            isPresented: $showingImporter,
            allowedContentTypes: [choosing == .profile ? Self.profileType : Self.p12Type],
            allowsMultipleSelection: false
        ) { result in
            guard case let .success(urls) = result, let url = urls.first else { return }
            switch choosing {
            case .p12: store.form.p12Path = url.path
            case .profile: store.form.profilePath = url.path
            }
        }
    }

    // MARK: Screens

    private var welcome: some View {
        VStack(alignment: .leading, spacing: Metrics.rowGap) {
            title(Self.welcomeTitle)
            prose(Self.welcomeBody)
            Button(Self.startLabel) { store.begin() }
                .keyboardShortcut(.defaultAction)
        }
    }

    private var question: some View {
        VStack(alignment: .leading, spacing: Metrics.rowGap) {
            title(Self.questionTitle)
            prose(Self.questionBody)
            HStack(spacing: Metrics.controlGap) {
                Button(Self.freeLabel) { Task { await store.chooseFree() } }
                Button(Self.paidLabel) { store.choosePaid() }
                    .keyboardShortcut(.defaultAction)
                    .accessibilityLabel(Self.paidAccessibilityLabel)
                if store.isRunning {
                    ProgressView().controlSize(.small)
                        .accessibilityLabel("Setting up a free Apple ID")
                }
            }
            .disabled(store.isRunning)
        }
    }

    private var paidForm: some View {
        VStack(alignment: .leading, spacing: Metrics.rowGap) {
            Marks.signingCertificate.label(Self.formTitle)
                .font(AppFont.bodyMedium)
                .foregroundStyle(Palette.text)
            // Frozen while the review runs, so what it checks is what Set up sends.
            fileRow("Certificate (.p12)", path: store.form.p12Path) { choose(.p12) }
                .disabled(store.isRunning)
            note(Self.p12Note)
            Group {
                row("Password") {
                    SecureField("Password", text: $store.form.password)
                        .textFieldStyle(.roundedBorder)
                }
                fileRow("Provisioning profile", path: store.form.profilePath) { choose(.profile) }
            }
            .disabled(store.isRunning)
            note(Self.profileNote)
            HStack(spacing: Metrics.controlGap) {
                // Enabled while the review runs: it writes nothing, and
                // Cancel and Back stop it (and its CLI).
                if let onCancel {
                    Button("Cancel") { store.cancel(); onCancel() }
                        .keyboardShortcut(.cancelAction)
                } else {
                    Button("Back") { store.back() }
                }
                Spacer()
                if store.isRunning {
                    ProgressView().controlSize(.small)
                        .accessibilityLabel("Checking the signing files")
                }
                Button("Continue") { store.startReview() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!store.form.isReady || store.isRunning)
            }
        }
    }

    private func review(_ result: InitResultRecord) -> some View {
        VStack(alignment: .leading, spacing: Metrics.rowGap) {
            Text(Self.reviewTitle)
                .font(AppFont.bodyMedium)
                .foregroundStyle(Palette.text)
            if store.replacing { note(Self.replaceNote(result)) }
            row("Team ID") { value(result.teamId ?? "Not stated") }
            row("Certificate") { value(result.certificate ?? "Not stated") }
            row("Profile") { value(result.profileName ?? "Not named") }
            row("Expires") { value(Self.expiryLine(result)) }
            row("Devices") { value(Self.devicesLine(result)) }
            deviceList(result.devices)
            warnings(result.notes)
            HStack(spacing: Metrics.controlGap) {
                // While the setup writes there is nothing to cancel or go back
                // to: it can't be stopped halfway, and the CLI undoes a failure.
                if let onCancel {
                    if store.isWriting {
                        Button(Self.settingUpLabel) {}
                            .disabled(true)
                    } else {
                        Button("Cancel") { store.cancel(); onCancel() }
                            .keyboardShortcut(.cancelAction)
                    }
                }
                Button("Back") { store.back() }
                    .disabled(store.isWriting)
                Spacer()
                if store.isRunning {
                    ProgressView().controlSize(.small)
                        .accessibilityLabel("Setting up signing")
                }
                Button(Self.setUpLabel) { Task { await store.confirm() } }
                    .keyboardShortcut(.defaultAction)
                    .disabled(store.isRunning)
            }
        }
    }

    /// One row per UDID, scrolling past a few: a profile can cover a hundred
    /// devices, and the sheet in Settings would otherwise push its buttons
    /// off the screen.
    @ViewBuilder
    private func deviceList(_ devices: [String]) -> some View {
        if !devices.isEmpty {
            ScrollView {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(devices, id: \.self) { udid in note(udid) }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: devices.count > 6 ? 120 : nil)
            .fixedSize(horizontal: false, vertical: devices.count <= 6)
        }
    }

    /// The CLI's warnings: what to know although it worked (or would).
    @ViewBuilder
    private func warnings(_ notes: [String]) -> some View {
        if !notes.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                Text(Self.warningsTitle)
                    .font(AppFont.label).labelCaps()
                    .foregroundStyle(Palette.warn)
                ForEach(Array(notes.enumerated()), id: \.offset) { _, text in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Symbols.attention.image(size: 11)
                            .foregroundStyle(Palette.warn)
                            .accessibilityHidden(true)
                        note(text)
                    }
                }
            }
        }
    }

    private func done(_ result: InitResultRecord) -> some View {
        VStack(alignment: .leading, spacing: Metrics.rowGap) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Symbols.ok.image(size: 14)
                    .foregroundStyle(Palette.success)
                    .accessibilityHidden(true)
                Text(result.summary)
                    .font(AppFont.bodyMedium)
                    .foregroundStyle(Palette.text)
                    .fixedSize(horizontal: false, vertical: true)
            }
            warnings(result.notes)
            if !result.pinned.isEmpty { note(Self.pinnedLine(result.pinned)) }
            if !result.sideloads.isEmpty { note(Self.sideloadsLine(result.sideloads)) }
            if !result.steps.isEmpty {
                prose(Self.freeStepsIntro)
                ForEach(Array(result.steps.enumerated()), id: \.offset) { index, text in
                    FreeStepRow(number: index + 1, text: text)
                }
            }
            HStack {
                Spacer()
                if finishing {
                    ProgressView().controlSize(.small).accessibilityLabel("Loading your apps")
                }
                Button(onCancel == nil ? Self.finishLabel : Self.doneLabel, action: onFinished)
                    .keyboardShortcut(.defaultAction)
                    .disabled(finishing)
            }
        }
    }

    // MARK: Words

    static func devicesLine(_ result: InitResultRecord) -> String {
        let count = result.deviceCount ?? result.devices.count
        return count == 0 ? "None listed" : "Covers " + pluralized(count, "device")
    }

    static func expiryLine(_ result: InitResultRecord) -> String {
        guard let date = result.expiryDate else { return "Not stated" }
        return date.formatted(date: .abbreviated, time: .omitted)
    }

    /// What a replace swaps, said before it runs.
    static func replaceNote(_ result: InitResultRecord) -> String {
        let moved = result.movedFromTeam.map { " It moves the account from team \($0)." } ?? ""
        return "This replaces the certificate and profile in use now, and the password in "
            + "your login keychain. Your apps, repos and settings stay." + moved
    }

    static func pinnedLine(_ apps: [String]) -> String {
        "Set to LiveContainer, so they stay where they are: " + apps.joined(separator: ", ") + "."
    }

    static func sideloadsLine(_ apps: [String]) -> String {
        "Still set to Sideload, which a free Apple ID can't build: "
            + apps.joined(separator: ", ") + ". Switch each to LiveContainer with Edit…"
    }

    // MARK: Pieces

    private func choose(_ choice: Choice) {
        choosing = choice
        showingImporter = true
    }

    private func title(_ text: String) -> some View {
        Text(text)
            .font(AppFont.title)
            .foregroundStyle(Palette.text)
            .fixedSize(horizontal: false, vertical: true)
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

    private func value(_ text: String) -> some View {
        Text(text)
            .font(AppFont.mono)
            .foregroundStyle(Palette.text)
            .textSelection(.enabled)
    }

    private func row<Content: View>(_ label: String, @ViewBuilder content: () -> Content) -> some View {
        HStack(spacing: Metrics.cardPadding) {
            Text(label)
                .font(AppFont.label).labelCaps()
                .foregroundStyle(Palette.textDim)
                .frame(width: 150, alignment: .leading)
            content()
        }
    }

    private func fileRow(_ label: String, path: String, choose: @escaping () -> Void) -> some View {
        row(label) {
            Text(path.isEmpty ? "None chosen" : path)
                .font(AppFont.monoSmall)
                .foregroundStyle(path.isEmpty ? Palette.textDim : Palette.text)
                .lineLimit(1)
                .truncationMode(.middle)
                .textSelection(.enabled)
            Spacer(minLength: Metrics.controlGap)
            Button("Choose…", action: choose)
                .controlSize(.small)
                .accessibilityLabel("Choose \(label)")
        }
    }
}

/// One of a free Apple ID's device steps, with a link for each page it names.
private struct FreeStepRow: View {
    let number: Int
    let text: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text("\(number).")
                .font(AppFont.bodyMedium)
                .foregroundStyle(Palette.textDim)
            VStack(alignment: .leading, spacing: 4) {
                Text(text)
                    .font(AppFont.body)
                    .foregroundStyle(Palette.text)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                ForEach(FreeSetupStep.links(in: text), id: \.self) { url in
                    Link("Open \(url.host ?? url.absoluteString)", destination: url)
                        .font(AppFont.label)
                }
            }
        }
    }
}
