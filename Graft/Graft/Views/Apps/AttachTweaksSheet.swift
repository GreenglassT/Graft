import SwiftUI

struct AttachTweaksSheet: View {
    @Bindable var store: ConfigStore
    let app: AppRecord
    @Bindable var tweaksStore: TweaksStore
    @Environment(\.dismiss) private var dismiss

    @State private var selected: Set<String> = []
    @State private var isWorking: Bool = false
    @State private var errorMessage: String?

    /// Filenames already attached to this app — pre-checked and disabled.
    private var alreadyAttached: Set<String> {
        Set(app.tweaks)
    }

    /// Newly-checked filenames (selected MINUS already-attached).
    private var pendingAttach: [String] {
        selected.subtracting(alreadyAttached).sorted()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider().background(Palette.line)
            content
            if let errorMessage {
                ErrorBanner(
                    message: errorMessage,
                    fit: .fullBleed(gutter: 20),
                    onDismiss: { self.errorMessage = nil }
                )
            }
            footer
        }
        .frame(width: 480, height: 520)
        .background(Palette.bg)
        .task {
            // Pre-select the already-attached entries synchronously, BEFORE
            // any async work — otherwise a slow loadTweaks could let the user
            // toggle rows that then get stomped by the assignment.
            selected = alreadyAttached
            // Load the tweaks library every time the sheet appears, and
            // deliberately not through `TweaksStore.hasLoaded`: this is the
            // screen you *pick* from, so a .deb downloaded in the Repos or
            // Canister section since the window opened has to be in the list,
            // and the pane behind this sheet may have filled the store hours
            // ago. That store is `ContentView`'s — the same one the Tweaks
            // section shows — so the refresh also lands on the rows behind
            // this sheet rather than in a private copy of the list.
            do { _ = try await tweaksStore.loadTweaks() }
            catch { errorMessage = error.localizedDescription }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Attach Tweaks").font(AppFont.title).foregroundStyle(Palette.text)
            Text("Pick tweaks from your library to attach to \(app.displayName ?? app.name).")
                .font(AppFont.monoSmall).foregroundStyle(Palette.textDim)
        }
        .padding(.horizontal, 20).padding(.top, 20).padding(.bottom, 14)
    }

    @ViewBuilder
    private var content: some View {
        if tweaksStore.tweaks.isEmpty {
            VStack {
                Spacer()
                Text("No tweaks in your library yet.\nDownload some from the Repos or Canister tab.")
                    .multilineTextAlignment(.center)
                    .foregroundStyle(Palette.textDim)
                    .font(AppFont.body)
                Spacer()
            }
            .frame(maxWidth: .infinity)
        } else {
            List(tweaksStore.tweaks) { tweak in
                row(for: tweak)
            }
            .listStyle(.inset)
            .scrollContentBackground(.hidden)
        }
    }

    private func row(for tweak: TweakRecord) -> some View {
        let isAttached = alreadyAttached.contains(tweak.filename)
        let isChecked = selected.contains(tweak.filename)
        return HStack(spacing: 10) {
            (isChecked ? Symbols.checkboxOn : Symbols.checkboxOff)
                .image(size: 16)
                .foregroundStyle(isAttached ? Palette.textDim : (isChecked ? Palette.accent : Palette.textMuted))
            VStack(alignment: .leading, spacing: 2) {
                // The tweak's own name, same as the library row. This is the
                // surface where the metadata earns the most: picking between
                // `com.example.btweak_1.1.8-jailed_iphoneos-arm64.deb`
                // and `com.example.btweak_1.1.8_iphoneos-arm64.deb` by
                // filename alone is exactly the case it was read for.
                Text(tweak.displayName)
                    .font(AppFont.bodyMedium)
                    .foregroundStyle(Palette.text)
                if let detail = tweak.detailLine {
                    Text(detail)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textMuted)
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
                // Kept, and kept in mono: this is the identity config.yaml, the
                // build log and the cyan command all use, so it has to stay
                // findable from the screen where you choose.
                //
                // Marked as the `.deb` it is, matching the same line in
                // `TweaksView` — the row's headline above is the tweak, this is
                // the archive. It earns more here than anywhere else: this is
                // the screen the comment above is about, where two builds of one
                // tweak are told apart by filename alone.
                HStack(spacing: 5) {
                    Marks.debPackage.image(size: 11)
                    Text("\(tweak.filename) · "
                         + fileSize(tweak.sizeBytes))
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.textDim)
            }
            Spacer()
            if isAttached {
                Text("attached")
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
            }
        }
        .contentShape(Rectangle())
        .opacity(isAttached ? 0.6 : 1.0)
        .onTapGesture {
            // Already-attached rows can't be unchecked from here (use the
            // per-row detach button in AppDetailView for that).
            guard !isAttached else { return }
            if isChecked {
                selected.remove(tweak.filename)
            } else {
                selected.insert(tweak.filename)
            }
        }
    }

    private var footer: some View {
        HStack {
            Spacer()
            Button("Cancel") { dismiss() }
                .keyboardShortcut(.cancelAction)
                .disabled(isWorking)
            Button(action: attach) {
                if isWorking {
                    ProgressView().controlSize(.small)
                } else {
                    Text(pendingAttach.isEmpty ? "Attach" : "Attach \(pendingAttach.count)")
                }
            }
            .keyboardShortcut(.defaultAction)
            .disabled(isWorking || pendingAttach.isEmpty)
        }
        .padding(20)
    }

    private func attach() {
        let toAttach = pendingAttach
        guard !toAttach.isEmpty, !isWorking else { return }
        errorMessage = nil
        isWorking = true
        Task {
            defer { isWorking = false }
            for filename in toAttach {
                do {
                    _ = try await store.attachTweak(appName: app.name, filename: filename)
                } catch {
                    errorMessage = "Failed to attach \(filename): \(error.localizedDescription)"
                    return  // leave already-attached ones in place; user can retry
                }
            }
            dismiss()
        }
    }
}
