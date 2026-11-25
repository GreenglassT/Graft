import SwiftUI

struct TweaksView: View {
    @ObservedObject var store: TweaksStore
    /// Detaching lives on `ConfigStore` because it writes config.yaml and the
    /// apps list has to reflect it. Passed in for the one action this pane
    /// offers that isn't about the file — same shape as `BuildsView(store:config:)`.
    @ObservedObject var config: ConfigStore
    /// Where this config keeps its tweaks, as the CLI resolved it
    /// (`--settings`' `paths.tweaks_dir`), or nil before that has been read.
    /// Named by the empty state, which used to say `~/.config/graft/tweaks/`
    /// whatever the config's own `tweaks_dir` or `XDG_CONFIG_HOME` said.
    var tweaksDir: String? = nil
    @State private var query: String = ""
    @State private var orphansOnly: Bool = false
    @State private var pendingDelete: TweakRecord?
    /// A delete the CLI refused because the tweak is still attached.
    ///
    /// Held separately from `store.lastError` because it is the one failure in
    /// this pane the user can act on from here, and `CLIContract.Exit.tweakInUse`
    /// exists precisely so it can be told apart from a generic one. Before
    /// this, `delete` caught every error identically and the exit code the
    /// contract declares — and `TweaksStoreTests` asserts the store surfaces —
    /// was read by nothing.
    @State private var blockedDelete: BlockedDelete?

    /// A refused delete, with the blocking apps as the *CLI* named them.
    struct BlockedDelete: Identifiable {
        let tweak: TweakRecord
        let apps: [String]
        var id: String { tweak.filename }
    }
    @State private var reloading: Bool = false
    /// What the last update did, kept until the next one.
    ///
    /// Not routed through `store.lastError`: swapping a tweak *succeeded*, and
    /// the part worth saying — which apps now need rebuilding — is not a
    /// failure. It is also the only place that fact appears, since this tool
    /// deliberately does not start a five-minute build on your behalf.
    @State private var updateNotice: String?

    private var tweaks: [TweakRecord] { store.tweaks }

    private var filtered: [TweakRecord] {
        tweaks.filter { record in
            if orphansOnly && !record.usedBy.isEmpty { return false }
            if query.isEmpty { return true }
            // Every string the row puts on screen is searchable. Matching only
            // the filename meant typing a tweak's actual name — the thing now
            // rendered largest in the row — found nothing.
            return [record.filename, record.name, record.version,
                    record.section, record.author]
                .compactMap { $0 }
                .contains { $0.localizedCaseInsensitiveContains(query) }
        }
    }

    var body: some View {
        // Evaluate the filter once per render and pass it down. It runs a
        // locale-aware compare per record and body re-runs on every keystroke
        // in the search field, so reading `filtered` from three places meant
        // three passes over the list per character typed.
        let rows = filtered
        return VStack(alignment: .leading, spacing: 0) {
            toolbar(rows)
                .padding(.horizontal, Metrics.paneGutter).padding(.vertical, 10)
            Divider().background(Palette.line)
            if let lastError = store.lastError {
                ErrorBanner(
                    message: lastError,
                    fit: .fullBleed(gutter: Metrics.paneGutter),
                    onDismiss: { store.lastError = nil }
                )
            }
            if let updateNotice {
                HStack(spacing: 8) {
                    Symbols.ok.image
                    // Bounded, and for the same reason `ErrorBanner` bounds its
                    // message. This string is `"\(name): \(result.reason)"` and
                    // the reason is the CLI's — the one input this process does
                    // not control. `.fixedSize(vertical: true)` pins a Text to
                    // its ideal height for whatever width it is proposed, and
                    // this strip is *outside* any scroll view, so a reason with
                    // nowhere to wrap (a 404's URL is the documented case, see
                    // `ErrorBanner.messageLimit`) grows the pane past the window
                    // instead of scrolling. That is what blanked the Devices
                    // pane: the overflow comes off the top and takes the
                    // toolbar with it.
                    //
                    // Two lines, because this is a one-line notice with a
                    // dismiss X beside it, and `.help` so a truncated reason is
                    // still readable.
                    Text(updateNotice)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                        .help(updateNotice)
                    Spacer()
                    Button {
                        self.updateNotice = nil
                    } label: {
                        Symbols.dismiss.image
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(Palette.textDim)
                }
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.accent)
                .padding(.horizontal, Metrics.paneGutter)
                .padding(.vertical, 10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Palette.surface2.opacity(0.5))
            }
            list(rows)
        }
        .background(Palette.bg)
        .navigationTitle("Tweaks")
        .alert(
            "Still attached",
            isPresented: $blockedDelete.isPresent(),
            presenting: blockedDelete
        ) { blocked in
            Button("Cancel", role: .cancel) {}
            Button("Detach and Delete", role: .destructive) {
                Task { await detachAndDelete(blocked) }
            }
        } message: { blocked in
            Text(Self.blockedMessage(blocked))
        }
        .alert(
            "Move this tweak to the Trash?",
            isPresented: $pendingDelete.isPresent(),
            presenting: pendingDelete
        ) { tweak in
            Button("Cancel", role: .cancel) {}
            Button("Move to Trash", role: .destructive) { Task { await delete(tweak) } }
        } message: { tweak in
            if tweak.usedBy.isEmpty {
                Text("\(tweak.filename) — you can recover it from the Trash, but it won't have a Put Back option.")
            } else {
                Text("\(tweak.filename) is attached to \(tweak.usedBy.joined(separator: ", ")). Deleting will be refused until you detach it.")
            }
        }
    }

    private func toolbar(_ rows: [TweakRecord]) -> some View {
        HStack {
            SearchField(placeholder: "Filter by name, version, section, author or filename", text: $query)
            Toggle("Orphans only", isOn: $orphansOnly)
                .toggleStyle(.switch)
                .font(AppFont.body)
            Spacer()
            Text("\(rows.count) of \(tweaks.count)")
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.textDim)
            updatesControl
            // The section reloads on entry, but the list can still go stale
            // while it's open — an attach done in another window, or the user
            // editing config.yaml by hand. `usedBy` decides what the delete
            // confirmation says, so give them a way to re-read it on demand.
            Button {
                Task { await reload() }
            } label: {
                if reloading {
                    ProgressView().controlSize(.small)
                } else {
                    Symbols.refresh.image
                }
            }
            .buttonStyle(.plain)
            .foregroundStyle(Palette.textDim)
            .frame(width: 22, height: 22)
            .contentShape(Rectangle())
            .disabled(reloading)
            .keyboardShortcut("r", modifiers: .command)
            .help("Reload tweaks from disk")
        }
    }

    /// Check for newer versions, and say what the last check found.
    ///
    /// User-initiated rather than automatic on entry: the check costs one HTTP
    /// call per package the cached repo indexes can't answer, and a list you
    /// glance at should not quietly make a dozen of them.
    @ViewBuilder
    private var updatesControl: some View {
        if let last = store.lastCheck, !store.isCheckingUpdates {
            // The count matters more than the word: "3" next to rows that are
            // individually marked is the thing to act on, and "all N up to
            // date" is the only way a *quiet* list is distinguishable from one
            // that was never checked.
            // `store.outdatedCount`, not `last.outdated`: the record is a
            // snapshot of when the check ended, and updating a tweak clears its
            // badge without touching it — so the header went on claiming "3
            // updates" over a list with no badged row in it.
            //
            // And never "all up to date" when some lookups could not be made.
            // Offline, every answer is nothing and the count is 0, which reads
            // exactly like having checked and being sure.
            Text(summary(last, outdated: store.outdatedCount))
                .font(AppFont.monoSmall)
                .foregroundStyle(
                    store.outdatedCount > 0 ? Palette.accent
                        : (last.unchecked > 0 ? Palette.warn : Palette.textDim)
                )
        }
        Button {
            Task { await store.checkForUpdates() }
        } label: {
            if store.isCheckingUpdates {
                ProgressView().controlSize(.small)
            } else {
                Symbols.checkUpdates.image
            }
        }
        .buttonStyle(.plain)
        .foregroundStyle(Palette.textDim)
        .frame(width: 22, height: 22)
        .contentShape(Rectangle())
        .disabled(store.isCheckingUpdates)
        .help("Check canister and your repos for newer versions")
    }

    /// The row line for a lookup the CLI could not make (`tweak-update`'s
    /// `unchecked`): offline, or the service refused.
    static let uncheckedLine = "Couldn't check for a newer version"

    /// Whether a row says its lookup could not be made. Not when an update is
    /// on offer: that row already has the line that matters.
    static func couldNotCheck(_ update: TweakUpdateRecord?) -> Bool {
        guard let update else { return false }
        return update.unchecked && !update.updateAvailable
    }

    private func summary(_ last: TweakUpdatesEndRecord, outdated: Int) -> String {
        if outdated > 0 { return "\(outdated) update\(outdated == 1 ? "" : "s")" }
        if last.unchecked > 0 { return "\(last.unchecked) couldn't be checked" }
        return "all \(last.checked) up to date"
    }

    /// Swap one tweak for its newest build, then say what that changed.
    private func runUpdate(_ tweak: TweakRecord) async {
        updateNotice = nil
        guard let result = await store.updateTweak(tweak) else { return }
        guard result.updated else {
            // A badge the user clicked after the library moved on under them.
            updateNotice = "\(tweak.displayName): \(result.reason ?? "nothing to update")"
            return
        }
        // `--update-tweak` rewrote `apps.<name>.tweaks` in config.yaml, and
        // ConfigStore is what holds those lists for every other pane. Without
        // this the Apps pane keeps showing the previous filename, and a user
        // acting on that view detaches a name that is no longer there.
        try? await config.loadApps()

        var parts = ["Updated \(tweak.displayName) to \(result.latest ?? "the newest build")"]
        if let error = result.error {
            // A swap that got partway: the download landed and some apps were
            // re-pointed. Saying which is the only way out of it.
            parts = ["Updated \(tweak.displayName), but not everywhere: \(error)"]
        }
        if let apps = result.apps, !apps.isEmpty {
            // The whole reason this is worth a sentence: the .deb on disk is
            // new, and every built .ipa containing the old one is now stale.
            parts.append("— rebuild \(apps.joined(separator: ", ")) to pick it up")
        }
        updateNotice = parts.joined(separator: " ")
    }

    /// A repo URL reduced to its host; `canister` passes through.
    ///
    /// The row has one line for this and a full URL would push the version
    /// delta — the part being acted on — off the end of it.
    private func shortSource(_ source: String) -> String {
        URL(string: source)?.host() ?? source
    }

    private func list(_ rows: [TweakRecord]) -> some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: Metrics.rowGap) {
                if rows.isEmpty {
                    Text(Self.emptyText(
                        libraryIsEmpty: tweaks.isEmpty, orphansOnly: orphansOnly, tweaksDir: tweaksDir
                    ))
                        .font(AppFont.body)
                        .foregroundStyle(Palette.textDim)
                        .padding(.vertical, 30)
                        .frame(maxWidth: .infinity)
                } else {
                    ForEach(rows) { tweak in
                        row(tweak)
                    }
                }
            }
            .padding(Metrics.paneGutter)
        }
    }

    private func row(_ tweak: TweakRecord) -> some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                // The tweak's own name when the .deb declared one, else the
                // filename — which is all this row ever showed, and is still
                // the fallback for a file the CLI couldn't parse.
                Text(tweak.displayName)
                    .font(AppFont.bodyMedium)
                    .foregroundStyle(Palette.text)
                // Version · section · author. The Repos browser shows all
                // three the moment before you download, and the library used
                // to drop them on the floor: two builds of the same .deb a
                // month apart looked identical here.
                if let detail = tweak.detailLine {
                    Text(detail)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textMuted)
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
                // Only when there is actually something newer. A row that has
                // been checked and is current says nothing: the pane's summary
                // already reports that all of them were checked, and a green
                // "up to date" on every line is noise the eye learns to skip
                // — which is how the one row that isn't gets missed.
                if let update = store.update(for: tweak), update.updateAvailable,
                   let latest = update.latest {
                    HStack(spacing: 5) {
                        Symbols.updateAvailable.image
                        Text("\(update.installed ?? "?") → \(latest)")
                            .monospacedDigit()
                        if let source = update.source {
                            Text("· \(shortSource(source))")
                                .foregroundStyle(Palette.textDim)
                        }
                        if store.updatingFilename == tweak.filename {
                            ProgressView().controlSize(.small)
                        } else {
                            Button("Update") {
                                Task { await runUpdate(tweak) }
                            }
                            .controlSize(.small)
                            // One at a time: each one rewrites config.yaml's
                            // tweak lists, and two concurrent writers would
                            // race on the same file.
                            .disabled(store.updatingFilename != nil)
                        }
                    }
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.accent)
                }
                // Per row, which lookups could not be made. The header's
                // "N couldn't be checked" said how many and not which, so the
                // quiet rows under it were indistinguishable: current, or
                // never asked.
                if Self.couldNotCheck(store.update(for: tweak)) {
                    HStack(spacing: 5) {
                        Symbols.attention.image(size: 11)
                        Text(Self.uncheckedLine)
                    }
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.warn)
                }
                // Kept even when a name is shown: it is what the app's tweak
                // list, the build log and the cyan command all name, so it has
                // to stay findable from here.
                //
                // The mark is `debPackage` and not `Symbols.tweak`, and the
                // distinction is the whole reason the mark exists: the headline
                // three lines up is the *tweak* — the thing you attach — and
                // this is the *archive it arrived in*. They are one row about
                // two nouns, and this line is the one that names a file. The
                // same line in `AttachTweaksSheet` and in the app pane's
                // attached-tweak rows takes the same mark for the same reason.
                //
                // 11pt, matching `AppFont.monoSmall` beside it. The glyph sits
                // at the line's left edge so the three stacked lines still share
                // a left margin — it indents the text, not the line.
                HStack(spacing: 5) {
                    Marks.debPackage.image(size: 11)
                    Text(tweak.filename)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.textDim)
                if tweak.usedBy.isEmpty {
                    Text("Orphan — not attached to any app")
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textDim)
                } else {
                    HStack(spacing: 4) {
                        ForEach(tweak.usedBy, id: \.self) { app in
                            TagChip(label: app)
                        }
                    }
                }
            }
            Spacer()
            Text(humanSize(tweak.sizeBytes))
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.textDim)
                .monospacedDigit()
            Button {
                pendingDelete = tweak
            } label: {
                Symbols.delete.image
            }
            .buttonStyle(.plain)
            .foregroundStyle(Palette.textDim)
            .frame(width: 22, height: 22)
            .contentShape(Rectangle())
            .help(attachedNote(tweak) ?? "Move to Trash")
        }
        .padding(Metrics.cardPadding)
        .cardStyle()
        .contextMenu {
            if let note = attachedNote(tweak) { Text(note) }
            Button("Move to Trash") { pendingDelete = tweak }
        }
    }

    /// What the refused delete offers to do about itself.
    ///
    /// Built here rather than inline in the alert: the same string concatenated
    /// in a `message:` builder pushed the type checker past its budget, and a
    /// view builder is the wrong place to discover that.
    private static func blockedMessage(_ blocked: BlockedDelete) -> String {
        let apps = blocked.apps.joined(separator: ", ")
        let which = blocked.apps.count == 1 ? "that app" : "those apps"
        return "\(blocked.tweak.displayName) is still attached to \(apps). "
            + "Detach it from \(which) and move it to the Trash?"
    }

    /// What actually happened when a detach-and-delete died partway.
    ///
    /// Static and pure so a test can read it: this is the only message in the
    /// pane describing a partially-applied change, and leaving it vague sends
    /// the user hunting through three apps' configs by hand.
    static func partialDetachMessage(
        tweak: TweakRecord, detached: [String], remaining: [String], error: Error
    ) -> String {
        var parts = ["Couldn't finish removing \(tweak.displayName): "
                     + error.localizedDescription]
        if !detached.isEmpty {
            parts.append("It was already detached from \(detached.joined(separator: ", "))"
                         + " — that has been applied.")
        }
        if !remaining.isEmpty {
            parts.append("Still attached to \(remaining.joined(separator: ", ")).")
        }
        parts.append("The file was not deleted.")
        return parts.joined(separator: " ")
    }

    /// One phrasing of "this tweak is spoken for", shared by the row tooltip
    /// and the context menu so they can't drift apart. nil when unattached.
    private func attachedNote(_ tweak: TweakRecord) -> String? {
        guard !tweak.usedBy.isEmpty else { return nil }
        return "Attached to \(tweak.usedBy.joined(separator: ", ")) — detach first"
    }

    /// What the list says when it has no rows. The folder is the CLI's, never
    /// a default this app guesses at; before it is known the sentence names
    /// no path rather than a wrong one.
    static func emptyText(libraryIsEmpty: Bool, orphansOnly: Bool, tweaksDir: String?) -> String {
        if libraryIsEmpty {
            guard let tweaksDir, !tweaksDir.isEmpty else { return "No tweaks in your tweaks folder yet." }
            return "No tweaks in \(tweaksDir)."
        }
        if orphansOnly { return "No orphan tweaks — every file is attached to at least one app." }
        return "No tweaks match the filter."
    }

    /// **Not the shared `fileSize`, deliberately.** `allowedUnits` caps this at
    /// MB so a tweak `.deb` — kilobytes to a few megabytes, always — never
    /// reads in GB. `fileSize` has no cap because it also formats phone
    /// capacity and 400 MB IPAs. Same count style, different question.
    private static let byteFormatter: ByteCountFormatter = {
        let fmt = ByteCountFormatter()
        fmt.allowedUnits = [.useKB, .useMB]
        fmt.countStyle = .file
        return fmt
    }()

    private func humanSize(_ bytes: Int) -> String {
        Self.byteFormatter.string(fromByteCount: Int64(bytes))
    }

    private func reload() async {
        guard !reloading else { return }
        reloading = true
        defer { reloading = false }
        store.lastError = nil
        do {
            _ = try await store.loadTweaks()
        } catch {
            store.lastError = error.localizedDescription
        }
    }

    private func delete(_ tweak: TweakRecord) async {
        store.lastError = nil
        do {
            _ = try await store.deleteTweak(filename: tweak.filename)
        } catch let refusal as TweaksStore.TweakInUse {
            // The apps arrive as data on a `tweak-in-use` event now, so there
            // is nothing to parse and nothing to fall back to.
            blockedDelete = BlockedDelete(
                tweak: tweak, apps: refusal.record.usedBy
            )
        } catch let error as CLIError where error.exitCode == CLIContract.Exit.tweakInUse {
            // Not an error banner: the CLI refused for a reason the user can
            // undo, and it named the apps. Offer to do it rather than telling
            // them to go elsewhere and come back.
            // Exit 16 with no event: a `graft` older than the
            // `tweak-in-use` event. The row's `usedBy` is the only list
            // available, stale as it may be — better than an empty alert.
            _ = error
            blockedDelete = BlockedDelete(tweak: tweak, apps: tweak.usedBy)
        } catch {
            store.lastError = error.localizedDescription
        }
    }

    /// Detach from every app holding it, then delete.
    ///
    /// Detach-then-delete rather than a CLI `--force`: each detach is a config
    /// write the user can see and undo in the Apps pane, and a flag that did
    /// both at once would silently rewrite several apps' tweak lists from a
    /// button in a different section.
    ///
    /// `usedBy` is re-read from the record the refusal carried rather than
    /// trusted from the row, because that list is the one the CLI just
    /// disagreed with us about.
    private func detachAndDelete(_ blocked: BlockedDelete) async {
        store.lastError = nil
        // Tracked so a failure partway can say which apps it changed. Detaching
        // from three apps is three separate config writes with no transaction
        // around them; the old loop reported only the message of the one that
        // threw, leaving the tweak silently gone from the apps it had already
        // reached and no way to tell which.
        var detached: [String] = []
        do {
            for app in blocked.apps {
                _ = try await config.detachTweak(appName: app, filename: blocked.tweak.filename)
                detached.append(app)
            }
            _ = try await store.deleteTweak(filename: blocked.tweak.filename)
        } catch {
            // Deliberately not rolled back: re-attaching is another write that
            // can fail the same way, and a half-undone undo is worse than an
            // accurate account of what landed. Deliberately not re-offering
            // either — if the delete fails after a clean detach, something
            // changed underneath us and a second attempt would be guessing.
            store.lastError = Self.partialDetachMessage(
                tweak: blocked.tweak, detached: detached,
                remaining: blocked.apps.filter { !detached.contains($0) },
                error: error
            )
            // The apps list on screen is now wrong for every app we did reach.
            try? await config.reload()
        }
    }
}
