import SwiftUI

struct SettingsView: View {
    @ObservedObject var store: SettingsStore
    @State private var showPasswordSheet = false
    /// ContentView's, not a second one. A private instance was torn down with
    /// the pane on every section change, so `hasLoaded` reset and `--devices`
    /// was re-spawned on each entry — on top of the `--settings` and `--check`
    /// runs that entry already triggers — and the picker could never be
    /// refreshed from the Devices tab while Settings was open.
    @ObservedObject var devicesStore: DevicesStore
    /// ContentView's, shared with the app pane's "Get latest from App Store".
    @PassedThrough var appStore: AppStoreStore
    /// `ConfigStore.account`, the one answer every pane gates on. Nil until
    /// the app list has loaded, and nil disables nothing (`AccountMode`).
    let account: AccountMode?
    /// Settings › Account's flows. Nil hides the account buttons, which only
    /// tests of other sections do.
    @ObservedOptionalObject var accountSetup: AccountSetupStore? = nil
    /// What to re-read once a run that may have changed the account has
    /// finished (`ContentView.afterAccountChange`). Handed to the store
    /// (`onAccountRunFinished`), which outlives this pane, so a run that ends
    /// after the user has left Settings still re-reads.
    var onAccountChanged: () async -> Void = {}
    /// A build is running (`ConfigStore.buildingApp`): it signs with the
    /// account, so the account isn't changed under it.
    var isBuilding: Bool = false
    /// The update switches. Nil hides Updates, which only tests of other
    /// sections do.
    @ObservedOptionalObject var updates: UpdateStore? = nil
    @State private var showAccountSheet = false
    @State private var confirmingSwitchToFree = false

    var body: some View {
        ScrollView {
            // `groupGap`, not `sectionGap`: each of these two spends ~14pt
            // between its own title and its first card, so at 16 the space
            // separating the groups was no larger than the space inside one and
            // "Paths" read as another row of the Preflight list.
            VStack(alignment: .leading, spacing: Metrics.groupGap) {
                if let parseError = store.parseError {
                    parseErrorNotice(parseError)
                }
                preflightSection
                settingsSection
                AppStoreConnectSection(store: store, account: account)
                AppStoreSettingsSection(store: appStore)
                if CommandLineToolSection.isOffered {
                    CommandLineToolSection(context: .thisApp)
                }
                pathsSection
                if let updates {
                    UpdatesSection(updates: updates)
                }
                versionSection
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
        .onChange(of: store.settings) { _ in store.reseedDraftIfIdle() }
        .task {
            accountSetup?.onAccountRunFinished = onAccountChanged
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

    // MARK: A config.yaml that does not parse

    /// The pane-level notice for a config.yaml the CLI could not parse
    /// (C-SETTINGS' `parse_error`).
    ///
    /// The CLI still answers `--settings` for that file — every setting null,
    /// the default paths — so the pane has something to show, and before this
    /// it showed exactly that with nothing to say why: empty fields that read
    /// as "nothing is set", a Paths section naming directories the config may
    /// not use, and a retention caption saying "saving replaces it" over a
    /// Save that `--set-settings` refuses (exit 2) on a file it cannot parse.
    static func parseErrorLines(_ error: String) -> (title: String, detail: String) {
        (
            "config.yaml can't be read: \(error)",
            "Fix it in a text editor, then come back to Settings. Until it parses, "
                + "nothing here can be saved, the settings below show as unset, and "
                + "Paths shows the defaults."
        )
    }

    private func parseErrorNotice(_ error: String) -> some View {
        let lines = Self.parseErrorLines(error)
        return HStack(alignment: .top, spacing: 8) {
            Symbols.attention.image(size: 12)
                .foregroundStyle(Palette.warn)
                .padding(.top, 2)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text(lines.title)
                    .font(AppFont.bodyMedium)
                    .foregroundStyle(Palette.text)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                Text(lines.detail)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
                    .fixedSize(horizontal: false, vertical: true)
                // Nothing here resets or rewrites it: the fix is the user's,
                // in a text editor, and this is the way to the file.
                if let file = Self.configFile(store.paths) {
                    Button(Self.showConfigLabel) {
                        NSWorkspace.shared.activateFileViewerSelecting([file])
                    }
                    .controlSize(.small)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(Metrics.cardPadding)
        .cardStyle()
    }

    static let showConfigLabel = "Show config.yaml in Finder"

    /// config.yaml, in the config folder `--settings` reported (the app's
    /// config is always the default one).
    static func configFile(_ paths: SettingsPaths?) -> URL? {
        paths.map { URL(fileURLWithPath: $0.configDir).appendingPathComponent("config.yaml") }
    }

    /// The caption beside the retention stepper.
    ///
    /// `archive_keep` null means the CLI could not read the value, and the
    /// stepper shows the default. Which of two things that is depends on why:
    /// a value the CLI refuses (a string, a negative) is one a save overwrites,
    /// but a file that does not parse at all is one no save can touch — and
    /// "saving replaces it" there sent the user to a Save that fails.
    static func archiveKeepNote(parseError: String?, archiveKeep: Int?) -> (text: String, warns: Bool) {
        if parseError != nil { return ("config.yaml doesn't parse — see above", true) }
        if archiveKeep == nil { return ("config.yaml's value can't be read — saving replaces it", true) }
        return ("per app, pruned after each build", false)
    }

    /// Said under Paths when they are the defaults rather than this config's.
    static let pathsAreDefaultsNote = "These are the defaults: config.yaml doesn't parse, "
        + "so any directories it sets aren't applied here."

    // MARK: Preflight

    private var preflightSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                SectionTitle(text: "Preflight")
                Spacer()
                summaryPill
                Button {
                    Task {
                        do { try await store.runChecks(askingShellAgain: true) }
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
            if store.shellAskedTooRecently {
                Text(ShellEnvironment.askedTooRecentlyNote)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.warn)
                    .fixedSize(horizontal: false, vertical: true)
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
                    .disabled(!store.draft.hasChanges || store.isSaving || store.parseError != nil)
            }

            if let account {
                accountRow(account)
                    .padding(Metrics.cardPadding)
                    .cardStyle()
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
                    // Shown and disabled on a free Apple ID, never hidden: the
                    // CLI refuses `--signing-*` on a config with no `signing:`
                    // section rather than write half of one, and a field that
                    // vanished would leave the user wondering where it went.
                    Group {
                        signingField("Signing certificate (.p12)", $store.draft.edited.signingCert)
                        signingField("Provisioning profile", $store.draft.edited.signingProfile)
                        signingField("Team ID", $store.draft.edited.signingTeamId, mono: false)
                    }
                    .paidOnly(account.isFree ? PaidOnly.signingSettings : nil)
                    if account.isFree {
                        Text(PaidOnly.signingSettings)
                            .font(AppFont.monoSmall)
                            .foregroundStyle(Palette.textDim)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                // Frozen for the round trip. `save()` reseeds the draft from
                // what the CLI reports afterward, so anything typed during
                // those three subprocesses would be silently thrown away —
                // and only Save and Revert were disabled before. And frozen
                // while config.yaml does not parse: an edit there has nowhere
                // to go (the notice at the top says why).
                .disabled(store.isSaving || store.parseError != nil)
                .padding(Metrics.cardPadding)
                .cardStyle()
            }
        }
    }

    /// One settings row: the label column, then whatever edits the value.
    ///
    /// The label stack was written out three times with the same font, color
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

    /// Which account config.yaml is for, what that means, and the way to the
    /// other one. Both ways run `graft --init --account …` (`AccountSetupStore`),
    /// which keeps every app, repo and setting.
    private func accountRow(_ account: AccountMode) -> some View {
        settingRow("Account") {
            VStack(alignment: .leading, spacing: 2) {
                Text(account.summary)
                    .font(AppFont.body)
                    .foregroundStyle(Palette.text)
                Text(account.detail)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
            if let accountSetup {
                if isBuilding {
                    Text(AccountMode.waitForBuild)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textDim)
                }
                Button(account.changeLabel) {
                    switch account {
                    case .free:
                        accountSetup.startUpgrade()
                        showAccountSheet = true
                    case .paid:
                        confirmingSwitchToFree = true
                    }
                }
                .controlSize(.small)
                .disabled(accountSetup.isRunning || isBuilding)
                .help(isBuilding ? AccountMode.waitForBuild : "")
                if account == .paid {
                    // A development certificate and profile expire every year:
                    // the new pair goes in here, with `--replace`.
                    Button(AccountMode.replaceLabel) {
                        accountSetup.startReplace()
                        showAccountSheet = true
                    }
                    .controlSize(.small)
                    .disabled(accountSetup.isRunning || isBuilding)
                    .help(isBuilding ? AccountMode.waitForBuild : "")
                }
            }
        }
        // A switch to a free Apple ID that finished while the user was in
        // another pane: its result, and the free device steps, are shown on
        // the way back rather than lost with the pane that started it.
        .onAppear {
            if let accountSetup, case .done = accountSetup.step { showAccountSheet = true }
        }
        .confirmationDialog(AccountMode.switchToFreeTitle, isPresented: $confirmingSwitchToFree) {
            Button("Switch to a free Apple ID") {
                Task { await switchToFree() }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(AccountMode.switchToFreeMessage)
        }
        .sheet(isPresented: $showAccountSheet, onDismiss: accountSheetClosed) {
            if let accountSetup {
                AccountSetupView(
                    store: accountSetup,
                    onFinished: { showAccountSheet = false },
                    onCancel: { showAccountSheet = false }
                )
                .padding(Metrics.paneGutter)
                .frame(width: 560)
                .background(Palette.bg)
                // Escape and a click outside can't close it while it writes.
                .interactiveDismissDisabled(accountSetup.isWriting)
            }
        }
    }

    /// The switch runs from the confirmation, then shows its result, and for a
    /// free Apple ID the steps on the device, in the sheet. A refusal goes to
    /// the pane's alert.
    private func switchToFree() async {
        guard let accountSetup, !isBuilding else { return }
        await accountSetup.switchToFree()
        if case .done = accountSetup.step {
            showAccountSheet = true
        } else if let error = accountSetup.error {
            store.lastError = error
            accountSetup.cancel()
        }
    }

    /// However the sheet closed: reset the flow for next time. What the run
    /// changed has already been re-read, when it finished, success or not
    /// (`AccountSetupStore.onAccountRunFinished`).
    private func accountSheetClosed() {
        accountSetup?.finish()
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
            let note = Self.archiveKeepNote(
                parseError: store.parseError, archiveKeep: store.settings?.archiveKeep
            )
            Text(note.text)
                .font(AppFont.monoSmall)
                .foregroundStyle(note.warns ? Palette.warn : Palette.textDim)
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

    // MARK: Version

    /// This app's version and the CLI's, on one line: "Graft 1.2 · graft
    /// 1.1.0". They ship separately, so a bug report wants both.
    ///
    /// The CLI's comes off `--settings` (`graft_version`), which this pane
    /// already reads, rather than a `--version` of its own.
    static func versionLine(app: String?, cli: String?, hasSettings: Bool) -> String {
        let cliPart: String
        if let cli {
            cliPart = "graft \(cli)"
        } else if hasSettings {
            cliPart = "graft: this version doesn't say which it is"
        } else {
            cliPart = "graft: shown once graft has read your config"
        }
        guard let app else { return cliPart }
        return "Graft \(app) · \(cliPart)"
    }

    private static var appVersion: String? {
        Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String
    }

    /// The versions, and beside them Star on GitHub and Support on Ko-fi
    /// (`SupportLinks`), as in the Help menu and About Graft: pages Graft
    /// opens only when they're clicked, in the browser.
    private var versionSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionTitle(text: "Version")
            VStack(alignment: .leading, spacing: 10) {
                Text(Self.versionLine(
                    app: Self.appVersion, cli: store.settings?.cliVersion,
                    hasSettings: store.settings != nil
                ))
                .font(AppFont.mono)
                .foregroundStyle(Palette.textMuted)
                .textSelection(.enabled)
                HStack(spacing: 10) {
                    ForEach(SupportLinks.all, id: \.title) { link in
                        Button(link.title) { NSWorkspace.shared.open(link.url) }
                    }
                }
            }
            .controlSize(.small)
            .padding(.horizontal, Metrics.cardPadding).padding(.vertical, Metrics.innerPadding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .cardStyle()
        }
    }

    // MARK: Paths

    /// The directories the CLI resolved for this config, not the defaults.
    ///
    /// This used to be four hardcoded `~/.config/graft`-style paths, which
    /// named the wrong directory for anyone with `XDG_*_HOME` set or a
    /// `cache_dir` / `tweaks_dir` of their own — the pane pointed Reveal at a
    /// folder the build never used. They come from `--settings` now
    /// (`SettingsStore.paths`), so they are whatever a build would use.
    private var pathsSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionTitle(text: "Paths")
            if let paths = store.paths {
                if store.pathsAreDefaults {
                    Text(Self.pathsAreDefaultsNote)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.warn)
                        .fixedSize(horizontal: false, vertical: true)
                }
                VStack(spacing: Metrics.rowGap) {
                    ForEach(Self.entries(paths), id: \.label) { entry in
                        PathRow(label: entry.label, path: entry.path)
                    }
                }
            } else {
                // Not a guess at the defaults: a wrong path with a Reveal
                // button is what this section used to be.
                Text(store.settings == nil
                     ? "Shown once graft has read your config."
                     : "This version of graft doesn't report its paths. "
                        + "Update it to see them here.")
                    .font(AppFont.body)
                    .foregroundStyle(Palette.textDim)
                    .padding(.vertical, Metrics.cardPadding)
            }
        }
    }

    /// One row per directory, in the order a user goes looking: the config
    /// they edit, then the files a build reads and writes, then its records.
    static func entries(_ paths: SettingsPaths) -> [PathEntry] {
        [
            .init(label: "Config", path: paths.configDir),
            .init(label: "Tweaks", path: paths.tweaksDir),
            .init(label: "IPA cache", path: paths.cacheDir),
            .init(label: "Built IPAs", path: paths.builtDir),
            .init(label: "State", path: paths.stateDir),
        ]
    }

    private func actionFor(_ check: PreflightCheck) -> PreflightRowAction? {
        guard !check.ok, let key = check.actionKey else { return nil }
        switch key {
        case "set-p12-password" where account.isFree:
            // `--set-p12-password` is refused on a free Apple ID, whose
            // `--check` has no .p12 row to carry this key. Disabled with the
            // reason rather than dropped, should one ever arrive.
            return PreflightRowAction(
                label: "Set Password…",
                mark: Marks.signingCertificate,
                disabledReason: PaidOnly.p12Password,
                action: {}
            )
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

    struct PathEntry: Equatable {
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
    /// Why the button is disabled, shown as its tooltip; nil leaves it live.
    var disabledReason: String? = nil
    let action: () -> Void
}

private struct PreflightRow: View {
    let check: PreflightCheck
    var onAction: PreflightRowAction? = nil

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            // An `info` row reports a fact, not a pass: no green dot for
            // "you have a free Apple ID", which is neither good nor bad.
            if check.isInfo {
                Symbols.advisory.image(size: 11)
                    .foregroundStyle(Palette.accent)
                    .padding(.top, 4)
                    .accessibilityLabel("Information")
            } else {
                Circle()
                    .fill(check.ok ? Palette.success : Palette.danger)
                    .frame(width: 9, height: 9)
                    .padding(.top, 6)
            }
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
                        .paidOnly(onAction.disabledReason)
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
            // An icon alone names nothing to VoiceOver; the symbol's own
            // description would be "arrow up right square".
            .accessibilityLabel("Show \(label) in Finder")
            .help("Show in Finder")
        }
        .padding(.horizontal, Metrics.cardPadding).padding(.vertical, Metrics.innerPadding)
        .cardStyle()
    }
}

/// Settings › Updates: the daily check's switch, the automatic update's,
/// and what each does.
struct UpdatesSection: View {
    @ObservedObject var updates: UpdateStore

    static let toggleTitle = "Check for updates automatically"
    static let installToggleTitle = "Download and install updates automatically"
    static let note = "Graft asks GitHub for its latest release once a day while it's open, with "
        + "nothing that identifies you or this Mac, and shows a banner when there's a newer one. "
        + "Help › Check for Updates… asks now, whether this is on or off."
    static let installNote = "Graft downloads a newer version from GitHub, checks that it's "
        + "Graft, signed by its developer and notarized by Apple, and installs it when you quit "
        + "Graft, or right away with Restart to Update. The old version goes to the Trash. With "
        + "this off, the banner's Update does the same when you choose it."
    /// For a copy that can't replace itself (`UpdateStore.canUpdate`).
    static let cannotInstallNote = "This copy of Graft can't update itself: the banner links the "
        + "release page instead."

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionTitle(text: "Updates")
            VStack(alignment: .leading, spacing: Metrics.rowGap) {
                Toggle(Self.toggleTitle, isOn: $updates.dailyCheck)
                    .font(AppFont.body)
                Text(Self.note)
                    .font(AppFont.body)
                    .foregroundStyle(Palette.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
                Toggle(Self.installToggleTitle, isOn: $updates.autoInstall)
                    .font(AppFont.body)
                    .disabled(!updates.canUpdate)
                Text(updates.canUpdate ? Self.installNote : Self.cannotInstallNote)
                    .font(AppFont.body)
                    .foregroundStyle(Palette.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.horizontal, Metrics.cardPadding).padding(.vertical, Metrics.innerPadding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .cardStyle()
        }
    }
}
