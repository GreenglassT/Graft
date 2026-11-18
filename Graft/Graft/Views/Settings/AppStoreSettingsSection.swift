import SwiftUI

/// Settings › App Store: whether this Mac can get and decrypt App Store apps.
///
/// Two rows — ipatool (installed, version, signed in, and the 2.8.1 warning)
/// and UnfairPlay (installed, version, enrolled) — and, when anything is
/// missing, the exact Terminal commands that fix it, for copying.
///
/// **Copying is the whole interaction, on purpose.** Signing in to ipatool
/// takes an Apple ID password and a 2FA code; enrolling UnfairPlay takes root.
/// Neither passes through this app or the CLI — the decision recorded in the
/// spec — so this pane reports and hands over the command, and Refresh is how
/// it notices the user has run it.
struct AppStoreSettingsSection: View {
    let store: AppStoreStore

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
    }

    private static func account(_ signedIn: Bool?) -> String {
        switch signedIn {
        case true?: return "signed in"
        case false?: return "not signed in"
        case nil: return "couldn't tell whether it's signed in"
        }
    }

    static func rows(for status: AppStoreStatusRecord) -> [Row] {
        let ipa = status.ipatool
        let up = status.unfairplay
        return [
            Row(
                name: "ipatool",
                // Signed in is not enough: 2.8.1 signs in and then cannot
                // download, so a warning keeps the dot red.
                ok: ipa.present && ipa.signedIn == true && ipa.warning == nil,
                detail: ipa.present
                    ? "\(ipa.version ?? "unknown version") · \(Self.account(ipa.signedIn))"
                    : "not found at \(ipa.path)",
                warning: ipa.warning
            ),
            Row(
                name: "UnfairPlay",
                ok: up.present && up.enrolled,
                detail: up.present
                    ? "\(up.version ?? "unknown version") · \(up.enrolled ? "enrolled" : "not enrolled")"
                    : "not found at \(up.path)",
                warning: nil
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
                ForEach(Self.rows(for: status), id: \.name) { row in
                    StatusRow(row: row)
                }
                if !status.setupCommands.isEmpty {
                    SetupCommands(commands: status.setupCommands)
                }
            }
        }
    }

    private struct StatusRow: View {
        let row: Row

        var body: some View {
            HStack(alignment: .top, spacing: 12) {
                Circle()
                    .fill(row.ok ? Palette.success : Palette.danger)
                    .frame(width: 9, height: 9)
                    .padding(.top, 6)
                VStack(alignment: .leading, spacing: 4) {
                    Text(row.name)
                        .font(AppFont.bodyMedium)
                        .foregroundStyle(Palette.text)
                    Text(row.detail)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textMuted)
                        .textSelection(.enabled)
                    if let warning = row.warning {
                        Text(warning)
                            .font(AppFont.monoSmall)
                            .foregroundStyle(Palette.warn)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 0)
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
