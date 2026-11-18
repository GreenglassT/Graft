import SwiftUI

struct BuildsView: View {
    @Bindable var store: BuildsStore
    /// Passed straight through to ReinstallSheet: a re-install pushes to the
    /// phone, so it moves the delivery badges the Apps section renders off
    /// this store. Nothing else in this view reads it.
    let config: ConfigStore
    /// Also passed straight through, and to both sheets rather than one: the
    /// build-log and re-install headers each draw the app's icon. Nothing in
    /// this view's own table does.
    let artwork: ArtworkStore
    /// Only for `Install to Mac…`, which is `--install-to-mac` — the same verb
    /// the Cache pane presses, so the two surfaces cannot drift on what
    /// "install here" means or on the PlayCover settings it fixes up.
    let staged: StagedIPAStore
    @State private var selected: BuildRecord.ID?
    @State private var handoffComplaint: String?
    @State private var reinstallTarget: BuildRecord?
    @State private var logTarget: BuildRecord?

    /// What a build row can do with its artifact.
    ///
    /// **One value, not two booleans.** Two booleans admit a state where both
    /// are true, and that state is the one this whole feature exists to make
    /// unreachable: a Mac build has a tweak dylib retargeted at a bundled stub
    /// `Preferences.framework`, and iOS has the real one, so re-installing one
    /// to a phone points the tweak at the stub and breaks it silently.
    ///
    /// The wrong action is *absent*, not disabled. A disabled control still
    /// says "this is a thing you might do here", and for the phone action on a
    /// Mac build that is not true at any point.
    enum RowAction: Equatable {
        case reinstall
        case installToMac
        /// Nothing to install: the build failed, or kept no archive.
        case none
    }

    static func action(for build: BuildRecord) -> RowAction {
        guard build.success, build.ipaPath != nil else { return .none }
        return build.isForThisMac ? .installToMac : .reinstall
    }

    var body: some View {
        VStack(spacing: 0) {
            if store.builds.isEmpty {
                Text("No builds yet.")
                    .font(AppFont.body)
                    .foregroundStyle(Palette.textDim)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                table
            }
        }
        .background(Palette.bg)
        .navigationTitle("Builds")
        .sheet(item: $reinstallTarget) { target in
            ReinstallSheet(build: target, runner: store.runner, config: config, artwork: artwork) {
                reinstallTarget = nil
                Task { _ = try? await store.load() }
            }
        }
        .sheet(item: $logTarget) { target in
            BuildLogView(build: target, runner: store.runner, artwork: artwork) {
                logTarget = nil
            }
        }
    }

    private var table: some View {
        Table(store.builds, selection: $selected) {
            TableColumn("When") { record in
                Text(humanTimestamp(record.timestamp))
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textMuted)
            }
            .width(160)
            TableColumn("App") { record in
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text(record.app).font(AppFont.bodyMedium).foregroundStyle(Palette.text)
                        // Only Mac rows are marked. A tag on every row would
                        // say nothing — the same reason the Cache pane's
                        // "BUILD USES THIS" chip is gated on having a sibling.
                        // `TagChip` is the component `InstalledAppsSection` and
                        // `CachePaneView` already use for "this row is the
                        // one"; a second visual grammar for it would be worse
                        // than none.
                        if record.isForThisMac { TagChip(label: "MAC") }
                    }
                    Text(record.version).font(AppFont.monoSmall).foregroundStyle(Palette.textDim)
                }
            }
            TableColumn("Tweaks") { record in
                Text(tweaksSummary(record.tweaks))
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textMuted)
            }
            TableColumn("Status") { record in
                statusView(for: record)
            }
            .width(180)
            TableColumn("") { record in
                Menu {
                    Button("View log…") { logTarget = record }
                        .disabled(record.logPath == nil)
                    switch Self.action(for: record) {
                    case .reinstall:
                        Button("Re-install…") { reinstallTarget = record }
                    case .installToMac:
                        Button("Install to Mac…") { installToMac(record) }
                    case .none:
                        // Disabled rather than missing, so the menu keeps its
                        // shape — but labelled for the target it *is*, so a
                        // failed Mac build never shows the phone verb even
                        // greyed out.
                        Button(record.isForThisMac ? "Install to Mac…" : "Re-install…") {}
                            .disabled(true)
                    }
                    Button("Reveal in Finder") {
                        Task { await store.reveal(record.id) }
                    }
                    .disabled(record.ipaPath == nil)
                    Button("Copy build command") {
                        Pasteboard.copy("graft \(record.app)")
                    }
                } label: {
                    Symbols.rowMenu.image
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
            }
            .width(40)
        }
        .tableStyle(.inset(alternatesRowBackgrounds: false))
        .scrollContentBackground(.hidden)
        .background(Palette.bg)
        .contextMenu(forSelectionType: BuildRecord.ID.self) { ids in
            if let id = ids.first, let record = store.builds.first(where: { $0.id == id }) {
                if record.logPath != nil {
                    Button("View log…") { logTarget = record }
                }
                if record.success && record.ipaPath != nil {
                    Button("Re-install…") { reinstallTarget = record }
                }
            }
        } primaryAction: { ids in
            if let id = ids.first, let record = store.builds.first(where: { $0.id == id }), record.logPath != nil {
                logTarget = record
            }
        }
    }

    @ViewBuilder
    private func statusView(for record: BuildRecord) -> some View {
        if record.success {
            HStack(spacing: 6) {
                Circle().fill(Palette.success).frame(width: 7, height: 7)
                Text("Built")
                    .font(AppFont.body)
                    .foregroundStyle(Palette.textMuted)
            }
        } else {
            HStack(spacing: 6) {
                Circle().fill(Palette.danger).frame(width: 7, height: 7)
                Text(record.error?.split(separator: "\n").first.map(String.init) ?? "Failed")
                    .font(AppFont.body)
                    .foregroundStyle(Palette.textMuted)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
        }
    }

    private func humanTimestamp(_ raw: String) -> String {
        guard let date = DateHelpers.parseISO8601(raw) else { return raw }
        return DateHelpers.mediumDateShortTime.string(from: date)
    }

    private func tweaksSummary(_ tweaks: [String]) -> String {
        if tweaks.isEmpty { return "—" }
        if tweaks.count <= 2 { return tweaks.joined(separator: ", ") }
        return "\(tweaks[0]), \(tweaks[1]) +\(tweaks.count - 2)"
    }

    /// Hand a Mac build's archive to PlayCover.
    ///
    /// Takes the filename, not the path: `--install-to-mac` resolves names in
    /// `cache_dir` and `built_dir` and refuses anything with a separator in it.
    ///
    /// Reports only a *failure*. There is no success message because there is
    /// no observable success — `open -b` returns once LaunchServices has
    /// delivered the event, so neither this app nor the CLI can see whether
    /// PlayCover finished. Announcing one would be the claim `handed-to-
    /// playcover` is named to avoid.
    private func installToMac(_ record: BuildRecord) {
        guard let path = record.ipaPath else { return }
        let filename = (path as NSString).lastPathComponent
        Task { handoffComplaint = await staged.installToMac(filename: filename) }
    }

}
