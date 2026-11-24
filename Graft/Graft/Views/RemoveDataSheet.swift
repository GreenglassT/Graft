import SwiftUI

/// Help › Remove Graft's Data…: everything Graft keeps outside Graft.app,
/// grouped, each with a checkbox, its size and what it is. Removing what is
/// checked can't be undone, and is refused while Graft is working
/// (`RemoveDataStore`).
struct RemoveDataSheet: View {
    @Bindable var store: RemoveDataStore
    @State private var confirming = false

    static let title = "Remove Graft's Data"
    static let menuTitle = "Remove Graft's Data…"
    static let intro = "Moving Graft.app to the Trash removes only the app. This is what it "
        + "keeps elsewhere on this Mac. Check what to remove."
    static let warning = "Removing can't be undone."
    static let afterwards = "Graft.app is still on this Mac. To remove it too, quit Graft, then "
        + "move Graft.app to the Trash."
    static let foldersComeBack = "Until you quit, Graft's background checks may make its "
        + "folders again, empty."
    static let nothingHere = "Graft keeps nothing outside Graft.app on this Mac."

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: Metrics.titleGap) {
                Text(Self.title).font(AppFont.title).foregroundStyle(Palette.text)
                Text(Self.intro)
                    .font(AppFont.body)
                    .foregroundStyle(Palette.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Divider().background(Palette.line)
            content
            footer
        }
        .padding(20)
        .frame(width: 620, height: 600)
        .background(Palette.bg)
        // TerminationHold isn't observable: what Graft is doing is read
        // again every second while the sheet is up.
        .task {
            while !Task.isCancelled {
                store.refreshWork()
                try? await Task.sleep(for: .seconds(1))
            }
        }
        .alert("Remove what you checked?", isPresented: $confirming) {
            Button("Remove", role: .destructive) { Task { await store.remove() } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(Self.confirmation(count: store.chosen.count, bytes: store.chosenBytes))
        }
    }

    static func confirmation(count: Int, bytes: Int) -> String {
        let what = count == 1 ? "1 item" : "\(count) items"
        return "This removes \(what) (\(fileSize(bytes))) for good. \(warning)"
    }

    // MARK: Content

    @ViewBuilder
    private var content: some View {
        switch store.phase {
        case .loading:
            busy("Looking for what Graft keeps…")
        case .removing:
            busy("Removing…")
        case let .failed(message):
            ErrorBanner(message: message, title: "Couldn't list Graft's data")
            Spacer()
        case .listed:
            listing
        case let .done(summary):
            done(summary)
        }
    }

    private func busy(_ text: String) -> some View {
        VStack {
            Spacer()
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(text).font(AppFont.body).foregroundStyle(Palette.textMuted)
            }
            Spacer()
        }
        .frame(maxWidth: .infinity)
    }

    @ViewBuilder
    private var listing: some View {
        if let refusal = RemoveDataStore.refusal(store.working) {
            notice(refusal, symbol: Symbols.waiting)
        }
        if let note = store.keychainNote {
            notice(note, symbol: Symbols.keychainLocked)
        }
        if store.shown.isEmpty {
            Text(Self.nothingHere).font(AppFont.body).foregroundStyle(Palette.textMuted)
            Spacer()
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: Metrics.sectionGap) {
                    ForEach(store.groups, id: \.title) { group in
                        VStack(alignment: .leading, spacing: Metrics.rowGap) {
                            SectionTitle(text: group.title)
                            ForEach(group.items) { item in
                                row(item)
                            }
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    private func notice(_ text: String, symbol: Symbol) -> some View {
        HStack(alignment: .top, spacing: 8) {
            symbol.image.foregroundStyle(Palette.warn).accessibilityHidden(true)
            Text(text)
                .font(AppFont.body)
                .foregroundStyle(Palette.text)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(Metrics.innerPadding)
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardStyle()
    }

    private func checked(_ id: String) -> Binding<Bool> {
        Binding(
            get: { store.chosen.contains(id) },
            set: { on in
                if on { store.chosen.insert(id) } else { store.chosen.remove(id) }
            }
        )
    }

    /// The size column: the files' total, or what stands in for one.
    static func sizeText(_ item: DataItemRecord) -> String {
        if item.present == nil { return "Couldn't check" }
        // Nothing for a link, which has no size to speak of.
        if let bytes = item.sizeBytes { return bytes > 0 ? fileSize(bytes) : "" }
        return item.keychain.isEmpty ? "" : "In your keychain"
    }

    private func row(_ item: DataItemRecord) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                if RemoveDataStore.isChoosable(item) {
                    Toggle(isOn: checked(item.id)) {
                        Text(item.title).font(AppFont.bodyMedium).foregroundStyle(Palette.text)
                    }
                    .toggleStyle(.checkbox)
                } else {
                    Text(item.title).font(AppFont.bodyMedium).foregroundStyle(Palette.textMuted)
                }
                Spacer()
                Text(Self.sizeText(item)).font(AppFont.monoSmall).foregroundStyle(Palette.textMuted)
            }
            Group {
                Text(item.detail)
                    .font(AppFont.body)
                    .foregroundStyle(Palette.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
                ForEach(item.paths, id: \.self) { path in
                    Text((path as NSString).abbreviatingWithTildeInPath)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textDim)
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .textSelection(.enabled)
                        .help(path)
                }
                ForEach(item.keychain, id: \.self) { entry in
                    Text("Keychain: \(entry.service), \(entry.account)")
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textDim)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                if let note = item.note {
                    Text(note)
                        .font(AppFont.body)
                        .foregroundStyle(Palette.warn)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if item.group == DataItemRecord.keptGroup, let how = item.how {
                    Text("To remove it: \(how)")
                        .font(AppFont.body)
                        .foregroundStyle(Palette.textDim)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            // In line with the title, past the checkbox.
            .padding(.leading, RemoveDataStore.isChoosable(item) ? 20 : 0)
        }
        .padding(Metrics.innerPadding)
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardStyle()
    }

    // MARK: Done

    private func done(_ summary: RemoveDataStore.Summary) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Metrics.sectionGap) {
                if !summary.removed.isEmpty {
                    VStack(alignment: .leading, spacing: Metrics.rowGap) {
                        SectionTitle(text: "Removed, \(fileSize(summary.freedBytes))")
                        ForEach(summary.removed, id: \.title) { removed in
                            HStack {
                                Symbols.ok.image.foregroundStyle(Palette.success)
                                    .accessibilityHidden(true)
                                Text(removed.title).font(AppFont.body).foregroundStyle(Palette.text)
                                Spacer()
                                Text(fileSize(removed.freedBytes))
                                    .font(AppFont.monoSmall).foregroundStyle(Palette.textMuted)
                            }
                        }
                    }
                }
                ForEach(summary.failed, id: \.title) { failed in
                    ErrorBanner(
                        message: failed.reasons.joined(separator: "\n"),
                        title: "\(failed.title) wasn't removed completely"
                    )
                }
                Text(Self.afterwards)
                    .font(AppFont.body)
                    .foregroundStyle(Palette.text)
                    .fixedSize(horizontal: false, vertical: true)
                if summary.removedFolders {
                    Text(Self.foldersComeBack)
                        .font(AppFont.body)
                        .foregroundStyle(Palette.textMuted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    // MARK: Buttons

    @ViewBuilder
    private var footer: some View {
        if case .done = store.phase {
            HStack {
                Spacer()
                Button("Close") { store.close() }
                    .keyboardShortcut(.cancelAction)
                Button("Quit Graft") { store.quitGraft() }
                    .keyboardShortcut(.defaultAction)
            }
        } else {
            HStack(alignment: .firstTextBaseline) {
                if store.phase == .listed && !store.shown.isEmpty {
                    Symbols.attention.image
                        .foregroundStyle(Palette.warn)
                        .accessibilityHidden(true)
                    Text("\(Self.warning) Checked: \(fileSize(store.chosenBytes)).")
                        .font(AppFont.body)
                        .foregroundStyle(Palette.textMuted)
                }
                Spacer()
                Button("Cancel") { store.close() }
                    .keyboardShortcut(.cancelAction)
                    .disabled(store.phase == .removing)
                Button("Remove…", role: .destructive) { confirming = true }
                    .disabled(!store.canRemove)
            }
        }
    }
}
