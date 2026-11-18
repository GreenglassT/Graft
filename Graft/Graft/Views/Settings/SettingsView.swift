import SwiftUI

struct SettingsView: View {
    @Bindable var store: SettingsStore
    @State private var showPasswordSheet = false
    /// ContentView's, not a second one. A private instance was torn down with
    /// the pane on every section change, so `hasLoaded` reset and `--devices`
    /// was re-spawned on each entry — on top of the `--settings` and `--check`
    /// runs that entry already triggers — and the picker could never be
    /// refreshed from the Devices tab while Settings was open.
    let devicesStore: DevicesStore
    /// ContentView's, shared with the app pane's "Get latest from App Store".
    let appStore: AppStoreStore

    var body: some View {
        ScrollView {
            // `groupGap`, not `sectionGap`: each of these two spends ~14pt
            // between its own title and its first card, so at 16 the space
            // separating the groups was no larger than the space inside one and
            // "Paths" read as another row of the Preflight list.
            VStack(alignment: .leading, spacing: Metrics.groupGap) {
                preflightSection
                settingsSection
                AppStoreSettingsSection(store: appStore)
                pathsSection
            }
            .padding(Metrics.paneGutter)
        }
        .background(Palette.bg)
        .navigationTitle("Settings")
        // The pane's one error channel. Every failure here routed into
        // `store.lastError` and nothing rendered it, so a rejected save — a
        // blanked Team ID, or the reconciliation guard catching a setting the
        // CLI declined — looked exactly like not having clicked Save.
        //
        // Titled for the channel, not for the save: the preflight scan and the
        // password sheet report here too, and "Couldn't save your settings"
        // over a failed `--check` names an operation the user never started.
        .alert("Settings", isPresented: $store.lastError.isPresent()) {
            Button("OK", role: .cancel) { store.lastError = nil }
        } message: {
            Text(store.lastError ?? "")
        }
        // Reseed when the store re-reads — `reseedDraftIfIdle` is a no-op
        // while the user has edits pending, so a background refresh cannot
        // discard what they were halfway through typing.
        .onChange(of: store.settings) { _, _ in store.reseedDraftIfIdle() }
        .task {
            store.reseedDraftIfIdle()
            // Populates the device picker's names; it renders the saved UDID
            // regardless, so a failed scan costs a label, not the setting.
            if !devicesStore.hasLoaded {
                _ = try? await devicesStore.load()
            }
        }
        .sheet(isPresented: $showPasswordSheet) {
            P12PasswordSheet(isSaving: store.isRunning) { password in
                do {
                    try await store.setP12Password(password)
                    try await store.runChecks()
                } catch {
                    store.lastError = error.localizedDescription
                }
            }
        }
    }

    /// Save the draft, and re-read the device list when that moved the default
    /// device.
    ///
    /// **Why the save is not enough on its own.** `default_device_udid` used to
    /// be read from `SettingsStore.settings` — which `saveDraft` replaces from
    /// the write's own answer — so every surface naming the default phone moved
    /// the instant a save landed. It now arrives as `DeviceRecord.isDefault` on
    /// the `--devices` event instead, and nothing re-ran `--devices` when the
    /// setting was written: `saveDraft` touches `settings` and the preflight and
    /// nothing else, this pane's `.task` is gated on `hasLoaded` so re-entering
    /// Settings does not rescan, and `DevicesStore.hasMoved` escalates the 5s
    /// poll only for a changed udid set or transport — `is_default` is not
    /// compared, so the poll never notices this.
    ///
    /// What that left is a loop closed on itself, through the control the app
    /// pane's build-target bar puts on screen for exactly this: amber "Nothing
    /// is set as the default device", Change, pick a phone, Save, back to the
    /// app pane — same amber sentence, same tooltip telling the user to go to
    /// Settings. It self-healed only on a plug/unplug, the device pane's
    /// Refresh, or the Refresh inside the build-options popover. The device
    /// pane's DEFAULT TARGET chip (`DeviceDetailView` → `DeviceHeader`) reads
    /// the same `isDefault` and had the same gap, which is a regression against
    /// `master`, where `ContentView` passed `settings?.defaultDeviceUDID` into
    /// that pane.
    ///
    /// **Here rather than in `SettingsStore`.** The store would have to hold a
    /// `DevicesStore` to do this, and this pane already holds both; keeping it
    /// in the view leaves the two stores independent of each other.
    ///
    /// **`load()`, not `invalidate()` + `load()`.** `invalidate()` clears
    /// `hasLoaded`, which `SidebarView` renders as `DevicesStore.scanningLine`
    /// in place of its device rows and which `BuildTargetBar.target` answers
    /// with `.scanning` — so clearing it would paint "Looking for devices…"
    /// over a listing that is a second old and correct about everything except
    /// the one phone this save moved, for the ~1s a full `--devices` costs.
    /// `invalidate()` is for the Refresh buttons, which un-latch a load-once
    /// gate before something else decides whether to load; this runs the scan
    /// itself, so it has no gate to un-latch. (The build-options popover's
    /// spinner keys off `isLoading`, which any `load()` sets, so it is not part
    /// of this choice.)
    ///
    /// **Gated on the value actually changing**, read off `settings` before and
    /// after rather than off what the draft asked for. A save that only fixed a
    /// signing path must not spend ~1s of lockdown handshakes, and neither must
    /// a save that failed — `saveDraft` leaves `settings` untouched in both
    /// cases, so both compare equal here.
    ///
    /// A failed rescan is swallowed, exactly as the picker's own load above
    /// swallows its failure. It costs this fix for that one save; raising it
    /// would put an alert titled "Settings" about a device scan the user did
    /// not start over a save that succeeded.
    func saveAndRescanIfDefaultDeviceMoved() async {
        let before = store.settings?.defaultDeviceUDID
        await store.saveDraft()
        guard store.settings?.defaultDeviceUDID != before else { return }
        _ = try? await devicesStore.load()
    }

    // MARK: Preflight

    private var preflightSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                SectionTitle(text: "Preflight")
                Spacer()
                summaryPill
                Button {
                    Task {
                        do { try await store.runChecks() }
                        catch { store.lastError = error.localizedDescription }
                    }
                } label: {
                    if store.isRunning {
                        HStack(spacing: 6) {
                            ProgressView().controlSize(.small)
                            Text("Running…")
                        }
                    } else {
                        Symbols.refresh.label("Re-run")
                    }
                }
                // `isBusy`, not `isRunning`: a save ends by running the
                // preflight itself, and this button stayed live through it —
                // two concurrent --check processes then raced, and whichever
                // finished last won, so the pane could settle on the pre-save
                // scan and show a red row for a path just fixed.
                .disabled(store.isBusy)
            }
            if store.checks.isEmpty {
                Text("Press Re-run to inspect your environment.")
                    .font(AppFont.body)
                    .foregroundStyle(Palette.textDim)
                    .padding(.vertical, 18)
                    .frame(maxWidth: .infinity)
                    .background(Palette.surface.opacity(0.3))
                    .clipShape(RoundedRectangle(cornerRadius: Metrics.radius))
            } else {
                VStack(spacing: Metrics.rowGap) {
                    ForEach(store.checks) { check in
                        PreflightRow(check: check, onAction: actionFor(check))
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var summaryPill: some View {
        if store.checks.isEmpty {
            EmptyView()
        } else {
            HStack(spacing: 10) {
                pillFragment(count: store.okCount, color: Palette.success, label: "OK")
                pillFragment(count: store.failCount, color: Palette.danger, label: "Fail")
            }
            .font(AppFont.label)
            .foregroundStyle(Palette.textMuted)
        }
    }

    private func pillFragment(count: Int, color: Color, label: String) -> some View {
        HStack(spacing: 4) {
            Circle().fill(color).frame(width: 7, height: 7)
            Text("\(count) \(label)")
        }
    }

    // MARK: Settings

    /// Everything this pane could previously show and not change.
    ///
    /// Before this, the default device, retention and the signing paths all
    /// meant editing ~/.config/graft/config.yaml by hand — which is a
    /// strange thing to require from an app whose whole premise is not having
    /// to. Save sends only the fields that differ from what the CLI reported,
    /// and refuses to claim success for any the CLI didn't apply.
    @ViewBuilder
    private var settingsSection: some View {
        VStack(alignment: .leading, spacing: Metrics.titleGap) {
            HStack(alignment: .firstTextBaseline) {
                SectionTitle(text: "Settings")
                Spacer()
                if store.draft.hasChanges {
                    // `resetDraft`, not `reseedDraftIfIdle`: the latter is the
                    // background path and does nothing while there are edits,
                    // which is the only state this button is ever visible in.
                    Button("Revert") { store.resetDraft() }
                        .controlSize(.small)
                        .disabled(store.isSaving)
                }
                // The whole action is one call to one named method, so what
                // Save does is somewhere a test can run it — a button's closure
                // is reachable only by clicking, and the device rescan that
                // method adds is the half that had no coverage at all.
                Button(store.isSaving ? "Saving…" : "Save") {
                    Task { await saveAndRescanIfDefaultDeviceMoved() }
                }
                    .controlSize(.small)
                    .disabled(!store.draft.hasChanges || store.isSaving)
            }

            if store.settings == nil {
                Text("Couldn't read your settings.")
                    .font(AppFont.body)
                    .foregroundStyle(Palette.textDim)
                    .padding(.vertical, Metrics.cardPadding)
            } else {
                VStack(alignment: .leading, spacing: Metrics.rowGap) {
                    devicePicker
                    keepStepper
                    signingField("Signing certificate (.p12)", $store.draft.edited.signingCert)
                    signingField("Provisioning profile", $store.draft.edited.signingProfile)
                    signingField("Team ID", $store.draft.edited.signingTeamId, mono: false)
                }
                // Frozen for the round trip. `save()` reseeds the draft from
                // what the CLI reports afterwards, so anything typed during
                // those three subprocesses would be silently thrown away —
                // and only Save and Revert were disabled before.
                .disabled(store.isSaving)
                .padding(Metrics.cardPadding)
                .cardStyle()
            }
        }
    }

    /// One settings row: the label column, then whatever edits the value.
    ///
    /// The label stack was written out three times with the same font, colour
    /// and 150pt column — so the column width lived in three places and the
    /// next row would have made it four.
    private func settingRow<Content: View>(
        _ label: String, @ViewBuilder content: () -> Content
    ) -> some View {
        HStack(spacing: Metrics.cardPadding) {
            Text(label)
                .font(AppFont.label).labelCaps()
                .foregroundStyle(Palette.textDim)
                .frame(width: 150, alignment: .leading)
            content()
        }
    }

    /// The persistent counterpart to the per-build device picker. "Ask each
    /// time" is a real choice, so it is a row rather than an absence.
    private var devicePicker: some View {
        settingRow("Default device") {
            Picker("", selection: $store.draft.edited.defaultDeviceUDID) {
                Text("Ask each time").tag("")
                ForEach(devicesStore.devices) { device in
                    Text(device.name).tag(device.udid)
                }
                // A configured device that is not plugged in right now must
                // still be selectable, or opening this pane silently rewrites
                // the setting to whatever happens to be attached.
                if let saved = store.settings?.defaultDeviceUDID,
                   !saved.isEmpty,
                   !devicesStore.devices.contains(where: { $0.udid == saved }) {
                    Text("\(saved) (not connected)").tag(saved)
                }
            }
            .labelsHidden()
            .pickerStyle(.menu)
        }
    }

    private var keepStepper: some View {
        settingRow("Archived builds kept") {
            // Lower bound 0, and no upper bound, because config validation
            // accepts any non-negative int. A 1...20 range could not represent
            // an existing `archive_keep: 0` or `: 50` — the user could walk 50
            // down and never back, and SwiftUI clamping on appearance made the
            // draft diverge from its baseline the moment the pane rendered, so
            // Save lit up for an edit nobody made.
            Stepper(value: $store.draft.edited.archiveKeep, in: 0...Int.max) {
                Text(store.draft.edited.archiveKeep == 0
                     ? "only the newest"
                     : pluralized(store.draft.edited.archiveKeep, "build"))
                    .font(AppFont.mono)
                    .foregroundStyle(Palette.text)
                    .monospacedDigit()
            }
            .fixedSize()
            // The CLI reports null for a value it refuses to read, which is not
            // the same as absent — every build fails on that config. The
            // stepper has to show *something*, so it shows the default and says
            // so, rather than presenting it as what is on disk.
            if store.settings?.archiveKeep == nil {
                Text("config.yaml's value can't be read — saving replaces it")
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.warn)
            } else {
                Text("per app, pruned after each build")
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
            }
        }
    }

    private func signingField(
        _ label: String, _ value: Binding<String>, mono: Bool = true
    ) -> some View {
        settingRow(label) {
            TextField("", text: value)
                .textFieldStyle(.roundedBorder)
                .font(mono ? AppFont.monoSmall : AppFont.body)
        }
    }

    // MARK: Paths

    private var pathsSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionTitle(text: "Paths")
            VStack(spacing: Metrics.rowGap) {
                ForEach(Self.paths, id: \.label) { entry in
                    PathRow(label: entry.label, path: entry.path)
                }
            }
        }
    }

    private func actionFor(_ check: PreflightCheck) -> PreflightRowAction? {
        guard !check.ok, let key = check.actionKey else { return nil }
        switch key {
        case "set-p12-password":
            // The .p12 *is* the signing certificate, so this is one of the two
            // places the app genuinely names that noun — the other is the sheet
            // this button opens. The three `signingField` rows above name it
            // too and deliberately go unmarked: their label column is 150pt and
            // "SIGNING CERTIFICATE (.P12)" already measures 173.7pt at
            // `AppFont.label`, so it wraps to two lines today and a mark in
            // front of it would only make that worse.
            return PreflightRowAction(
                label: "Set Password…",
                mark: Marks.signingCertificate,
                action: { showPasswordSheet = true }
            )
        default:
            return nil
        }
    }

    private static let paths: [PathEntry] = {
        let home = FileManager.default.homeDirectoryForCurrentUser
        return [
            .init(label: "Config", path: home.appending(path: ".config/graft").path),
            .init(label: "Built IPAs", path: home.appending(path: ".cache/graft/built").path),
            .init(label: "State", path: home.appending(path: ".local/state/graft").path),
            .init(label: "Tweaks", path: home.appending(path: ".config/graft/tweaks").path),
        ]
    }()

    private struct PathEntry {
        let label: String
        let path: String
    }
}

struct PreflightRowAction {
    let label: String
    /// The noun this action is about, when the app has a mark for it.
    ///
    /// Optional and defaulted, because a preflight row's action is whatever the
    /// CLI's `actionKey` maps to and most of them will never name one of the
    /// four. Declared before `action` so the existing trailing-closure call
    /// sites are untouched.
    var mark: Mark? = nil
    let action: () -> Void
}

private struct PreflightRow: View {
    let check: PreflightCheck
    var onAction: PreflightRowAction? = nil

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Circle()
                .fill(check.ok ? Palette.success : Palette.danger)
                .frame(width: 9, height: 9)
                .padding(.top, 6)
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(check.name)
                        .font(AppFont.bodyMedium)
                        .foregroundStyle(Palette.text)
                    Spacer()
                    if let onAction {
                        Button(action: onAction.action) {
                            if let mark = onAction.mark {
                                // 11pt: `.controlSize(.small)` draws this
                                // button's label at 11pt, and a mark cannot
                                // inherit that the way a symbol would.
                                mark.label(onAction.label, size: 11)
                            } else {
                                Text(onAction.label)
                            }
                        }
                        .controlSize(.small)
                    }
                }
                if let detail = check.detail, !detail.isEmpty {
                    Text(detail)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textMuted)
                        .textSelection(.enabled)
                }
                if !check.ok, let hint = check.hint, !hint.isEmpty {
                    HStack(alignment: .top, spacing: 6) {
                        Text(hint)
                            .font(AppFont.monoSmall)
                            .foregroundStyle(Palette.warn)
                            .textSelection(.enabled)
                        Spacer()
                        Button("Copy hint") { Pasteboard.copy(hint) }
                            .buttonStyle(.borderless)
                            .font(AppFont.label)
                            .foregroundStyle(Palette.accent)
                    }
                    .padding(Metrics.innerPadding)
                    .background(Palette.surface2.opacity(0.5))
                    .clipShape(RoundedRectangle(cornerRadius: Metrics.radiusInner))
                }
            }
            Spacer(minLength: 0)
        }
        .padding(Metrics.cardPadding)
        .cardStyle()
    }
}

private struct P12PasswordSheet: View {
    let isSaving: Bool
    let onSave: (String) async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var password: String = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            // The sheet's whole subject is the signing certificate, so it takes
            // the mark — and it is the other half of the pair with the
            // "Set Password…" button that opens it, which carries the same one.
            // No `size:`: `AppFont.bodyMedium` is 13pt, which is
            // `Mark.defaultPointSize`.
            Marks.signingCertificate.label("Set .p12 password")
                .font(AppFont.bodyMedium)
                .foregroundStyle(Palette.text)
            Text("Stored in the login keychain under service ‘graft’. Used by the signing pipeline whenever you build & install from this app.")
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.textMuted)
                .fixedSize(horizontal: false, vertical: true)
            SecureField("Password", text: $password)
                .textFieldStyle(.roundedBorder)
                .disabled(isSaving)
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                    .disabled(isSaving)
                Button(isSaving ? "Saving…" : "Save") {
                    Task {
                        await onSave(password)
                        dismiss()
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(password.isEmpty || isSaving)
            }
        }
        .padding(Metrics.paneGutter)
        .frame(width: 420)
        .background(Palette.bg)
    }
}

private struct PathRow: View {
    let label: String
    let path: String

    var body: some View {
        HStack(spacing: 12) {
            Text(label)
                .font(AppFont.label)
                .labelCaps()
                .foregroundStyle(Palette.textDim)
                .frame(width: 90, alignment: .leading)
            Text(path)
                .font(AppFont.mono)
                .foregroundStyle(Palette.textMuted)
                .textSelection(.enabled)
                .lineLimit(1)
                .truncationMode(.middle)
            Spacer()
            Button {
                NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
            } label: {
                Symbols.revealInFinder.image
            }
            .buttonStyle(.borderless)
            .foregroundStyle(Palette.accent)
        }
        .padding(.horizontal, Metrics.cardPadding).padding(.vertical, Metrics.innerPadding)
        .cardStyle()
    }
}
