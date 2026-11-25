import SwiftUI

struct AttachTweaksSheet: View {
    @PassedThrough var store: ConfigStore
    let app: AppRecord
    @ObservedObject var tweaksStore: TweaksStore
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

    /// Whether a tweak is ticked, as a binding a `Toggle` can own.
    ///
    /// An already-attached tweak cannot be unticked here — the per-row detach
    /// on the app pane is where that happens — so the setter ignores it as
    /// well as the control being disabled.
    private func isSelected(_ tweak: TweakRecord) -> Binding<Bool> {
        Binding(
            get: { selected.contains(tweak.filename) },
            set: { on in
                guard !alreadyAttached.contains(tweak.filename) else { return }
                if on { selected.insert(tweak.filename) } else { selected.remove(tweak.filename) }
            }
        )
    }

    /// One tweak, as a real checkbox.
    ///
    /// It used to be an image of a checkbox and a tap gesture on the row,
    /// which VoiceOver met as an `AXImage` called "Selected" with no action:
    /// it could read the state and not change it. A `Toggle` is a checkbox to
    /// the Accessibility API — named for the tweak, carrying its state,
    /// pressable — and `TweakCheckboxStyle` keeps the row looking and clicking
    /// as it did.
    private func row(for tweak: TweakRecord) -> some View {
        let isAttached = alreadyAttached.contains(tweak.filename)
        return Toggle(isOn: isSelected(tweak)) {
            rowLabel(for: tweak, isAttached: isAttached)
        }
        .toggleStyle(TweakCheckboxStyle(isAttached: isAttached))
        .disabled(isAttached)
        .accessibilityLabel(tweak.displayName)
        .accessibilityHint(isAttached ? "Already attached to this app" : "")
        .opacity(isAttached ? 0.6 : 1.0)
    }

    private func rowLabel(for tweak: TweakRecord, isAttached: Bool) -> some View {
        HStack(spacing: 10) {
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

/// The library checkbox: the app's own checkbox glyph, then the row, and a
/// click anywhere on the row toggles it — the row it replaced, as a `Toggle`.
private struct TweakCheckboxStyle: ToggleStyle {
    let isAttached: Bool

    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 10) {
            (configuration.isOn ? Symbols.checkboxOn : Symbols.checkboxOff)
                .image(size: 16)
                .foregroundStyle(
                    isAttached ? Palette.textDim
                        : (configuration.isOn ? Palette.accent : Palette.textMuted)
                )
                // The glyph is drawing, not a second control; the row's own
                // checkbox carries the state.
                .accessibilityHidden(true)
            configuration.label
        }
        .contentShape(Rectangle())
        .onTapGesture { configuration.isOn.toggle() }
    }
}
