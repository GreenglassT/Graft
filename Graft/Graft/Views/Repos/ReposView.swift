import SwiftUI

struct ReposView: View {
    @ObservedObject var store: RepoStore
    @State private var selectedRepo: RepoRecord.ID?
    @State private var newRepoURL: String = ""
    @State private var filter: String = ""

    var body: some View {
        // HStack instead of a nested NavigationSplitView: the inner sidebar of
        // a NavigationSplitView is user-collapsible (macOS shows a toggle in
        // the toolbar), and collapsing it hides the add-URL bar, the repo
        // list AND any error banner — leaving the view looking inexplicably
        // blank. Keeping the two panes in an HStack means the controls are
        // always visible.
        HStack(spacing: 0) {
            repoColumn
                .frame(width: 280)
                .frame(maxHeight: .infinity)
            Divider().background(Palette.line)
            packagesPane
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .navigationTitle("Repos")
        .task(id: selectedRepo) {
            filter = ""
            store.fetchResult = nil
            guard let url = selectedRepo else { return }
            await loadPackages(url: url, refresh: false)
        }
    }

    // MARK: - Repo column

    private var repoColumn: some View {
        VStack(alignment: .leading, spacing: 0) {
            addBar
                .padding(.horizontal, Metrics.columnGutter).padding(.vertical, 10)
            Divider().background(Palette.line)
            if let errorMsg = store.lastError {
                ErrorBanner(
                    message: errorMsg,
                    // Deliberately not "Couldn't add repo": `store.lastError`
                    // is this whole section's one error channel — remove,
                    // load-packages and download all set it too, and so does
                    // an add that landed but whose refresh failed. A title
                    // naming an operation it can't know happened is simply
                    // wrong three times out of four; the message carries the
                    // specifics.
                    title: "Couldn't apply that change",
                    fit: .fullBleed(gutter: Metrics.columnGutter),
                    onDismiss: { store.lastError = nil }
                )
            }
            if store.repos.isEmpty {
                Text("No repos yet. Paste an APT repo URL above.")
                    .font(AppFont.body)
                    .foregroundStyle(Palette.textDim)
                    .padding(Metrics.columnGutter)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            } else {
                List(store.repos, selection: $selectedRepo) { repo in
                    repoRow(repo)
                        .tag(repo.id)
                        .contextMenu {
                            Button("Remove", role: .destructive) {
                                Task { await removeRepo(url: repo.url) }
                            }
                        }
                }
                .listStyle(.sidebar)
                .scrollContentBackground(.hidden)
            }
        }
        .background(Palette.bg)
    }

    private var addBar: some View {
        HStack(spacing: 6) {
            SearchField(placeholder: "https://apt.repo.url", text: $newRepoURL, onSubmit: addRepo)
            Button("Add", action: addRepo)
                .disabled(newRepoURL.trimmingCharacters(in: .whitespaces).isEmpty || store.isAdding)
            if store.isAdding {
                ProgressView().controlSize(.small)
            }
        }
    }

    private func repoRow(_ repo: RepoRecord) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(repo.name).font(AppFont.bodyMedium).foregroundStyle(Palette.text)
            Text(pluralized(repo.packageCount, "package")).font(AppFont.monoSmall)
                .foregroundStyle(Palette.textDim)
        }
        .padding(.vertical, 2)
    }

    // MARK: - Packages pane

    @ViewBuilder
    private var packagesPane: some View {
        if let url = selectedRepo {
            VStack(alignment: .leading, spacing: 0) {
                packagesToolbar(url: url)
                    .padding(.horizontal, Metrics.paneGutter).padding(.vertical, 10)
                Divider().background(Palette.line)
                packagesContent
            }
            .background(Palette.bg)
        } else {
            Text("Select a repo to browse its packages.")
                .foregroundStyle(Palette.textMuted)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Palette.bg)
        }
    }

    private func packagesToolbar(url: String) -> some View {
        HStack {
            SearchField(placeholder: "Filter packages", text: $filter, onSubmit: {})
            Button("Refresh") {
                Task { await loadPackages(url: url, refresh: true) }
            }
            .disabled(store.isRefreshing)
            if store.isRefreshing || store.isLoading {
                ProgressView().controlSize(.small)
            }
            Spacer()
            if let status = store.fetchResult {
                Text(status).font(AppFont.monoSmall).foregroundStyle(Palette.textDim)
            } else {
                Text(pluralized(filteredPackages.count, "package"))
                    .font(AppFont.monoSmall).foregroundStyle(Palette.textDim)
            }
        }
    }

    @ViewBuilder
    private var packagesContent: some View {
        if filteredPackages.isEmpty {
            Text(store.isLoading || store.isRefreshing ? "Loading…" : "No packages.")
                .font(AppFont.body)
                .foregroundStyle(Palette.textDim)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            Table(filteredPackages) {
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
                TableColumn("Section") { pkg in
                    TagChip(label: pkg.section.isEmpty ? "—" : pkg.section)
                }
                .width(110)
                // Titled, as Canister's Fetch is: an empty header is a sort
                // button VoiceOver can't name.
                TableColumn("Download") { pkg in
                    Button("Download") {
                        Task { await download(pkg) }
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                }
                .width(96)
            }
            .tableStyle(.inset(alternatesRowBackgrounds: false))
            .scrollContentBackground(.hidden)
            .background(Palette.bg)
        }
    }

    private var filteredPackages: [RepoPackage] {
        let q = filter.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return store.packages }
        return store.packages.filter {
            $0.name.lowercased().contains(q) || $0.packageId.lowercased().contains(q)
        }
    }

    // MARK: - Actions

    private func addRepo() {
        let url = newRepoURL
        Task {
            store.lastError = nil
            do {
                try await store.addRepo(url: url)
                newRepoURL = ""
            } catch {
                store.lastError = error.localizedDescription
            }
        }
    }

    private func removeRepo(url: String) async {
        store.lastError = nil
        do {
            try await store.removeRepo(url: url)
            if selectedRepo == url { selectedRepo = nil }
        } catch {
            store.lastError = error.localizedDescription
        }
    }

    private func loadPackages(url: String, refresh: Bool) async {
        store.lastError = nil
        do {
            _ = try await store.loadPackages(repoURL: url, refresh: refresh)
        } catch is CancellationError {
            // superseded by a newer selection — ignore
        } catch {
            store.lastError = error.localizedDescription
        }
    }

    private func download(_ pkg: RepoPackage) async {
        store.lastError = nil
        do {
            try await store.download(pkg)
        } catch {
            store.lastError = error.localizedDescription
        }
    }
}
