import SwiftUI
import UniformTypeIdentifiers

/// Drag-drop target + Choose button + status row for a source IPA.
/// The component is stateless beyond its hover flag; callers pass the
/// currently-cached record (or nil) and an `onPick(URL)` callback.
struct IPADropZone: View {
    let title: String
    let cached: CachedIPARecord?
    let isWorking: Bool
    let onPick: (URL) -> Void
    /// Said beside the spinner while `isWorking`, when the caller knows more
    /// than "busy" — the app pane passes "Decrypting…" while the CLI runs an
    /// App Store IPA through UnfairPlay, which takes seconds where a copy takes
    /// none.
    var workingLabel: String? = nil

    @State private var isHovering: Bool = false
    @State private var isShowingPicker: Bool = false
    @State private var localError: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            // Rendered only when there is one. `AppDetailView` passes "" —
            // the pane already has a "Source IPA" section title above this —
            // and an empty `Text` is not nothing: it reserved a full line box
            // plus this VStack's 8pt of spacing, which is why that one section
            // sat 35pt below its title where every other section sits 6–13pt
            // below its own.
            if !title.isEmpty {
                Text(title)
                    .font(AppFont.bodyMedium)
                    .foregroundStyle(Palette.text)
            }
            dropTarget
            statusRow
            if let localError {
                Text(localError)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.danger)
            }
        }
    }

    private var dropTarget: some View {
        HStack(spacing: 10) {
            Symbols.dropIPA.image(size: 18)
                .foregroundStyle(Palette.textDim)
            VStack(alignment: .leading, spacing: 2) {
                Text("Drop a .ipa here")
                    .font(AppFont.bodyMedium)
                    .foregroundStyle(Palette.text)
                Text("or pick one with the button →")
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
            }
            Spacer()
            Button("Choose IPA…") { isShowingPicker = true }
                .disabled(isWorking)
            if isWorking {
                if let workingLabel {
                    Text(workingLabel)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textDim)
                        .lineLimit(1)
                }
                ProgressView().controlSize(.small)
            }
        }
        .padding(Metrics.cardPadding)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: Metrics.radius)
                .strokeBorder(
                    isHovering ? Palette.accent : Palette.line,
                    style: StrokeStyle(lineWidth: 1.5, dash: [6, 4])
                )
                .background(
                    RoundedRectangle(cornerRadius: Metrics.radius)
                        .fill(isHovering ? Palette.accent.opacity(0.08) : Color.clear)
                )
        )
        .onDrop(of: [.fileURL], isTargeted: $isHovering) { providers in
            handleDrop(providers: providers)
        }
        .fileImporter(
            isPresented: $isShowingPicker,
            allowedContentTypes: [
                UTType(filenameExtension: "ipa") ?? UTType.data,
            ],
            allowsMultipleSelection: false
        ) { result in
            switch result {
            case .success(let urls):
                if let url = urls.first { dispatch(url: url) }
            case .failure:
                break  // user canceled or system error; nothing to surface
            }
        }
    }

    @ViewBuilder
    private var statusRow: some View {
        HStack(spacing: 8) {
            Text("IPA:")
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.textDim)
            if let cached, cached.exists {
                Text(cached.filename)
                    .font(AppFont.mono)
                    .foregroundStyle(Palette.text)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text("(\(fileSize(cached.sizeBytes)))")
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
            } else {
                Text("missing")
                    .font(AppFont.mono)
                    .foregroundStyle(Palette.textDim)
            }
            Spacer()
        }
    }

    // MARK: - Drop handling

    private func handleDrop(providers: [NSItemProvider]) -> Bool {
        guard providers.count == 1, let provider = providers.first else {
            localError = "drop one .ipa file at a time"
            return false
        }
        _ = provider.loadDataRepresentation(forTypeIdentifier: UTType.fileURL.identifier) { data, _ in
            guard
                let data,
                let url = URL(dataRepresentation: data, relativeTo: nil, isAbsolute: true)
            else { return }
            DispatchQueue.main.async { dispatch(url: url) }
        }
        return true
    }

    private func dispatch(url: URL) {
        localError = nil
        guard url.pathExtension.lowercased() == "ipa" else {
            localError = "not a .ipa file: \(url.lastPathComponent)"
            return
        }
        onPick(url)
    }
}
