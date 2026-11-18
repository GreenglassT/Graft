import SwiftUI

struct EditAppSheet: View {
    let app: AppRecord
    @Bindable var store: ConfigStore
    let onClose: () -> Void

    /// The whole form, plus the baseline it will be diffed against. One list of
    /// fields for the bindings, for Save's enablement, and for the write.
    @State private var draft: AppOptionsDraft
    @State private var saving = false
    @State private var error: String?
    /// Held so the sheet can cancel the write if it goes away underneath it.
    @State private var saveTask: Task<Void, Never>?

    init(app: AppRecord, store: ConfigStore, onClose: @escaping () -> Void) {
        self.app = app
        self._store = Bindable(wrappedValue: store)
        self.onClose = onClose
        _draft = State(initialValue: AppOptionsDraft(app: app))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Edit \(app.name)")
                .font(AppFont.bodyMedium)
                .foregroundStyle(Palette.text)
            Text(app.bundleId)
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.textDim)

            Divider().background(Palette.line)

            Form {
                TextField("Display name", text: $draft.edited.displayName)
                TextField("Preferred version", text: $draft.edited.preferredVersion)
                    .help("A version string like 21.16.2, or 'latest'")
                // `displayName`, matching `AddAppSheet`'s picker. The same
                // control spelled "livecontainer" here and "LiveContainer"
                // there read as two different settings. Only the label moves:
                // the selection is bound to the enum and the tag is the enum,
                // so what `AppOptionsDraft` diffs and what `--set-app-options`
                // receives are untouched.
                Picker("Delivery", selection: $draft.edited.delivery) {
                    ForEach(DeliveryMode.allCases, id: \.self) { mode in
                        Text(mode.displayName).tag(mode)
                    }
                }
                Toggle("Keep bundle id", isOn: $draft.edited.keepBundleId)
                Toggle("Strip extensions", isOn: $draft.edited.stripExtensions)
                Toggle("Strip supported devices", isOn: $draft.edited.stripSupportedDevices)
                Stepper("Compression level: \(draft.edited.compressionLevel)",
                        value: $draft.edited.compressionLevel, in: 0...9)
            }
            .formStyle(.grouped)

            if !draft.edited.keepBundleId {
                Text("Some apps tie sign-in to their original bundle id, so changing it "
                     + "can break sign-in. Keep it on unless you know the app "
                     + "doesn't authenticate against its bundle id.")
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.warn)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if let error {
                // No dismiss control on purpose: the next Save clears it, and
                // there is nothing else the user could do with the X.
                ErrorBanner(message: error)
            }

            HStack {
                Spacer()
                // Disabled during the write — this also disables the Escape
                // binding attached to it. Cancelling used to dismiss the sheet
                // while `--set-app-options` was mid-flight, so the user was
                // told nothing happened while config.yaml was being rewritten.
                // There is no "undo the write" to offer here; the honest
                // behaviour is to make them wait for the CLI to answer.
                Button("Cancel") { onClose() }
                    .keyboardShortcut(.cancelAction)
                    .disabled(saving)
                Button("Save") { startSave() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(saving || !draft.hasChanges)
            }
        }
        .padding(20)
        .frame(width: 460)
        .background(Palette.bg)
        // The sheet can still be torn down without Cancel — the app selection
        // changes, or the window closes. Cancel the Task so its result can't
        // be written into @State that no longer exists.
        .onDisappear { saveTask?.cancel() }
    }

    private func startSave() {
        guard !saving, draft.hasChanges else { return }
        saving = true
        error = nil
        saveTask = Task { await save() }
    }

    /// Send only what changed — the CLI patches exactly the fields it receives.
    /// Which fields those are is `AppOptionsDraft`'s business, and tested there.
    ///
    /// Cancelling `saveTask` *does* kill the subprocess — `CLIRunner.runOnce`
    /// SIGTERMs it — so a cancelled save may or may not have landed, and the
    /// call can come back either with a result or with a `CancellationError`.
    /// Either way the sheet is gone by then, so the outcome goes to
    /// `store.lastError` — which outlives this view and is already surfaced by
    /// SidebarView — instead of into a `@State` nobody will ever see.
    private func save() async {
        defer { saving = false }
        do {
            _ = try await store.updateAppOptions(appName: app.name, draft: draft)
            if !Task.isCancelled { onClose() }
        } catch {
            if Task.isCancelled {
                store.lastError =
                    "Couldn't save changes to \(app.name): \(error.localizedDescription)"
            } else {
                self.error = error.localizedDescription
            }
        }
    }
}
