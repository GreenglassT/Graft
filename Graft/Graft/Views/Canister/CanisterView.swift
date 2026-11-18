import SwiftUI

struct CanisterView: View {
    @Bindable var store: CanisterStore
    @State private var query: String = ""
    @State private var selected: CanisterPackage.ID?
    @State private var searchTask: Task<Void, Never>?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            searchBar
                .padding(.horizontal, Metrics.paneGutter).padding(.vertical, 10)
            Divider().background(Palette.line)
            content
        }
        .background(Palette.bg)
        .navigationTitle("Canister")
    }

    private var searchBar: some View {
        HStack {
            SearchField(placeholder: "Search canister.me", text: $query, onSubmit: triggerSearch)
            Button("Search", action: triggerSearch)
                .keyboardShortcut(.return, modifiers: [])
                .disabled(query.trimmingCharacters(in: .whitespaces).isEmpty || store.isSearching)
            if store.isSearching {
                ProgressView().controlSize(.small)
            }
            Spacer()
            statusText
        }
    }

    @ViewBuilder
    private var statusText: some View {
        if let status = store.fetchResult {
            Text(status).font(AppFont.monoSmall).foregroundStyle(Palette.textDim)
        } else if !store.results.isEmpty {
            Text(pluralized(store.results.count, "result"))
                .font(AppFont.monoSmall).foregroundStyle(Palette.textDim)
        }
    }

    @ViewBuilder
    private var content: some View {
        if store.results.isEmpty {
            Text(emptyText)
                .font(AppFont.body)
                .foregroundStyle(Palette.textDim)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            Table(store.results, selection: $selected) {
                TableColumn("Package") { pkg in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(pkg.name).font(AppFont.bodyMedium).foregroundStyle(Palette.text)
                        Text(pkg.packageId).font(AppFont.monoSmall).foregroundStyle(Palette.textDim)
                    }
                }
                TableColumn("Version") { pkg in
                    Text(pkg.version).font(AppFont.mono).foregroundStyle(Palette.textMuted)
                }
                .width(80)
                TableColumn("Repo") { pkg in
                    TagChip(label: pkg.repositoryId)
                }
                .width(90)
                // Named, though the buttons below already say "Fetch": an
                // empty header is a sort button with no name, and VoiceOver
                // announces the column by it on every row.
                TableColumn("Fetch") { pkg in
                    Button("Fetch") {
                        Task { await runFetch(pkg.packageId) }
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                }
                .width(70)
            }
            .tableStyle(.inset(alternatesRowBackgrounds: false))
            .scrollContentBackground(.hidden)
            .background(Palette.bg)
        }
    }

    private var emptyText: String {
        if store.hasSearched && !store.isSearching {
            return "No results."
        }
        return "Type a query and press Search."
    }

    private func triggerSearch() {
        searchTask?.cancel()
        searchTask = Task { await runSearch() }
    }

    private func runSearch() async {
        do {
            _ = try await store.search(query: query)
        } catch is CancellationError {
            // Superseded by a newer search; drop silently.
        } catch {
            store.lastError = error.localizedDescription
        }
    }

    private func runFetch(_ packageId: String) async {
        do {
            try await store.fetch(packageId)
        } catch {
            store.lastError = error.localizedDescription
        }
    }
}
