import SwiftUI

struct BuildsView: View {
    @Bindable var store: BuildsStore
    /// Passed straight through to ReinstallSheet: a re-install pushes to the
    /// phone, so it moves the delivery badges the Apps section renders off
    /// this store. Read here only for `reinstallRefusal`.
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
    /// The dry run's answer while "Clean up old builds…" asks to confirm it.
    @State private var pendingPrune: PruneSummary?
    @State private var checkingPrune = false
    @State private var pruneCheckError: String?

    /// The confirmation "Clean up old builds…" shows, from the dry run.
    ///
    /// It states the keep count — config.yaml's `archive_keep`, the number
    /// each build already prunes to — and exactly how many archives would go,
    /// because that is a number the CLI just counted rather than a guess.
    /// Zero is its own answer with nothing to confirm. `archive_keep: 0` says
    /// so in its own words: on demand it keeps none at all, where after a
    /// build it spares the one just made.
    static func pruneConfirmation(_ preview: PruneSummary) -> (title: String, message: String, canConfirm: Bool) {
        guard preview.removed > 0 else {
            return (
                "Nothing to clean up",
                "Every app version has \(preview.keep) or fewer archived builds.",
                false
            )
        }
        let size = fileSize(preview.bytesRemoved)
        if preview.keep == 0 {
            return (
                "Clean up old builds?",
                "Your setting keeps no archived builds, so this moves all \(preview.removed) "
                    + "to the Trash (\(size)). Their Re-install stops working.",
                true
            )
        }
        return (
            "Clean up old builds?",
            "Graft keeps the newest \(pluralized(preview.keep, "build")) of each app version "
                + "(Settings › Archived builds kept). This moves "
                + "\(pluralized(preview.removed, "archived IPA")) to the Trash (\(size)).",
            true
        )
    }

    /// Whether "Clean up old builds…" can be pressed, and its tooltip.
    ///
    /// Not while a build runs in this app: a build archives its IPA first and
    /// pushes or installs it after, and at `archive_keep: 0` a sweep in that
    /// window would move the archive about to be delivered to the Trash. The
    /// CLI's retention lock already makes a clean-up wait for any build that
    /// is delivering, this app's or a Terminal one; this gate only says up
    /// front which build it is waiting for, instead of a wait that ends in
    /// "a build is delivering".
    /// `working` is the button's own check or clean-up in flight.
    static func cleanUpAvailability(buildingApp: String?, working: Bool) -> (enabled: Bool, help: String) {
        if let buildingApp {
            return (
                false,
                "\(buildingApp) is building — clean up when it finishes, so its archive "
                    + "isn't moved to the Trash before it's delivered"
            )
        }
        return (!working, "Move archived IPAs past Settings › Archived builds kept to the Trash")
    }

    /// The line under the header after a clean-up.
    static func pruneResultLine(_ result: BuildsStore.PruneResult?) -> (text: String, failed: Bool)? {
        switch result {
        case nil:
            return nil
        case let .moved(count, bytes):
            return (
                "Moved \(pluralized(count, "archived IPA")) to the Trash (\(fileSize(bytes))). "
                    + "Empty the Trash to free the space.",
                false
            )
        case let .partly(moved, failures):
            return (
                "Moved \(moved) to the Trash. These could not be moved: "
                    + failures.joined(separator: "; "),
                true
            )
        case let .failed(message):
            return (message, true)
        }
    }

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

    /// Why Re-install would be refused for this row, or nil when it would run.
    ///
    /// `--reinstall-build` installs a sideload archive as a normal app under
    /// the paid team's profile, so on a free Apple ID the CLI refuses it for
    /// an app that is still `delivery: sideload` — by the app's mode *now*,
    /// which is what the CLI reads, not the mode it was built with. Disabled
    /// with this as its tooltip rather than offered and failed.
    static func reinstallRefusal(
        for build: BuildRecord, apps: [AppRecord], account: AccountMode?
    ) -> String? {
        guard account.isFree,
              apps.first(where: { $0.name == build.app })?.delivery == .sideload
        else { return nil }
        return PaidOnly.sideloadApp
    }

    private func reinstallRefusal(for build: BuildRecord) -> String? {
        Self.reinstallRefusal(for: build, apps: config.apps, account: config.account)
    }

    /// One item in a row's menu: what it says, and whether it can be chosen.
    ///
    /// **A disabled item carries its reason in its title.** A `.help` on a
    /// menu item is never shown — AppKit draws no tooltip inside an open menu —
    /// so `.paidOnly`, which explains itself only as a tooltip, left
    /// "Re-install…" grayed out with nothing anywhere saying why.
    struct MenuItem: Equatable {
        let title: String
        let enabled: Bool
    }

    /// The install item for a row, from the same `action(for:)` the row menu
    /// and the context menu both read — the context menu used to decide for
    /// itself, on `success && ipaPath != nil`, and so offered Re-install… on a
    /// Mac build: the stub-linked archive this type exists to keep off phones
    /// (the CLI refuses it too, after the sheet is already open).
    static func installItem(for build: BuildRecord, refusal: String?) -> MenuItem {
        switch action(for: build) {
        case .reinstall:
            return refusal == nil
                ? MenuItem(title: "Re-install…", enabled: true)
                : MenuItem(title: "Re-install… (needs a paid account)", enabled: false)
        case .installToMac:
            return MenuItem(title: "Install to Mac…", enabled: true)
        case .none:
            // Labeled for the target it *is*, so a failed Mac build never
            // shows the phone verb even grayed out.
            let verb = build.isForThisMac ? "Install to Mac…" : "Re-install…"
            let why = build.success ? "archive not kept" : "build failed"
            return MenuItem(title: "\(verb) (\(why))", enabled: false)
        }
    }

    static func logItem(for build: BuildRecord) -> MenuItem {
        build.logPath == nil
            ? MenuItem(title: "View log… (not saved)", enabled: false)
            : MenuItem(title: "View log…", enabled: true)
    }

    static func revealItem(for build: BuildRecord) -> MenuItem {
        build.ipaPath == nil
            ? MenuItem(title: "Reveal in Finder (no archive)", enabled: false)
            : MenuItem(title: "Reveal in Finder", enabled: true)
    }

    private func install(_ record: BuildRecord) {
        switch Self.action(for: record) {
        case .reinstall: reinstallTarget = record
        case .installToMac: installToMac(record)
        case .none: break
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            header
                .padding(.horizontal, Metrics.paneGutter).padding(.vertical, 10)
            Divider().background(Palette.line)
            if let line = Self.pruneResultLine(store.pruneResult) {
                if line.failed {
                    ErrorBanner(
                        message: line.text, fit: .fullBleed(gutter: Metrics.paneGutter),
                        onDismiss: { store.dismissPruneResult() }
                    )
                } else {
                    Text(line.text)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.success)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, Metrics.paneGutter).padding(.vertical, 8)
                }
            }
            if let pruneCheckError {
                ErrorBanner(
                    message: pruneCheckError, fit: .fullBleed(gutter: Metrics.paneGutter),
                    onDismiss: { self.pruneCheckError = nil }
                )
            }
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
        // The value is passed to the button rather than re-read: dismissing
        // clears the binding (`stopBuildConfirmation`'s trap).
        .alert(
            pendingPrune.map { Self.pruneConfirmation($0).title } ?? "",
            isPresented: $pendingPrune.isPresent(),
            presenting: pendingPrune
        ) { preview in
            if Self.pruneConfirmation(preview).canConfirm {
                Button("Cancel", role: .cancel) {}
                Button("Move to Trash", role: .destructive) {
                    Task { await store.prune() }
                }
            } else {
                Button("OK", role: .cancel) {}
            }
        } message: { preview in
            Text(Self.pruneConfirmation(preview).message)
        }
    }

    /// The title row, with "Clean up old builds…".
    private var header: some View {
        let availability = Self.cleanUpAvailability(
            buildingApp: config.buildingApp, working: checkingPrune || store.isPruning
        )
        return HStack {
            SectionTitle(text: "Builds")
            Spacer()
            Button {
                Task { await checkPrune() }
            } label: {
                if checkingPrune || store.isPruning {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.small)
                        Text(store.isPruning ? "Cleaning up…" : "Checking…")
                    }
                } else {
                    Text("Clean up old builds…")
                }
            }
            .controlSize(.small)
            .disabled(!availability.enabled)
            .help(availability.help)
        }
    }

    /// Ask the CLI what a clean-up would move, then confirm that.
    private func checkPrune() async {
        checkingPrune = true
        defer { checkingPrune = false }
        pruneCheckError = nil
        store.dismissPruneResult()
        do {
            pendingPrune = try await store.previewPrune()
        } catch {
            pruneCheckError = error.localizedDescription
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
                    let log = Self.logItem(for: record)
                    Button(log.title) { logTarget = record }
                        .disabled(!log.enabled)
                    // Disabled rather than missing, so the menu keeps its
                    // shape — with the reason in the title (`MenuItem`).
                    let install = Self.installItem(for: record, refusal: reinstallRefusal(for: record))
                    Button(install.title) { self.install(record) }
                        .disabled(!install.enabled)
                    let reveal = Self.revealItem(for: record)
                    Button(reveal.title) {
                        Task { await store.reveal(record.id) }
                    }
                    .disabled(!reveal.enabled)
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
                // Only an artifact there is something to do with; which verb
                // is `action(for:)`'s, as in the row menu.
                if Self.action(for: record) != .none {
                    let install = Self.installItem(for: record, refusal: reinstallRefusal(for: record))
                    Button(install.title) { self.install(record) }
                        .disabled(!install.enabled)
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
