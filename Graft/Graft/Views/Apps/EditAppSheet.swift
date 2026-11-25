import SwiftUI

struct EditAppSheet: View {
    let app: AppRecord
    @ObservedObject var store: ConfigStore
    /// `ContentView`'s, injected through the app pane: the LiveContainer
    /// picker lists what `--lc-status` found on the default device, and the
    /// device pane reads the same store, so a phone already checked there
    /// shows its answer here at once while this sheet re-reads it.
    @ObservedObject var liveContainer: LiveContainerStore
    /// `ContentView`'s polled store, for which phone is the default.
    @ObservedObject var devices: DevicesStore
    /// `ContentView`'s settings, for whether an App Store Connect key is set:
    /// "Create the provisioning profile automatically" needs one to turn on.
    @ObservedObject var settings: SettingsStore
    let onClose: () -> Void

    /// The whole form, plus the baseline it will be diffed against. One list of
    /// fields for the bindings, for Save's enablement, and for the write.
    @State private var draft: AppOptionsDraft
    @State private var saving = false
    @State private var error: String?
    /// What the CLI warned about on a save that worked. The sheet stays open
    /// to show it, with Done in place of Cancel and Save.
    @State private var savedWarnings: [String] = []
    /// Held so the sheet can cancel the write if it goes away underneath it.
    @State private var saveTask: Task<Void, Never>?

    /// - Parameter edit: an edit already made, for a test of what Save does
    ///   with it. A Toggle in a grouped Form has no control a test can click
    ///   reliably, and the draft-to-flags mapping is tested on its own
    ///   (`AppOptionsDraftTests`). The app passes nothing.
    init(
        app: AppRecord, store: ConfigStore, liveContainer: LiveContainerStore,
        devices: DevicesStore, settings: SettingsStore, onClose: @escaping () -> Void,
        edit: AppOptionsDraft? = nil
    ) {
        self.app = app
        self._store = ObservedObject(wrappedValue: store)
        self.liveContainer = liveContainer
        self.devices = devices
        self.settings = settings
        self.onClose = onClose
        _draft = State(initialValue: edit ?? AppOptionsDraft(app: app))
    }

    private var defaultDevice: DeviceRecord? { devices.devices.first(where: \.isDefault) }

    /// Whether the LiveContainer picker is on the sheet: for an app being
    /// delivered into LiveContainer, by a CLI that reports the pin (C-LCPIN).
    /// An older CLI reports neither the pin nor the flag that sets it, so a
    /// picker there would show "Automatic" over a hand-written pin and send a
    /// flag the CLI refuses.
    static func offersLCPin(app: AppRecord, delivery: DeliveryMode) -> Bool {
        app.reportsLCPin && delivery == .livecontainer
    }

    /// One entry in the LiveContainer picker.
    struct LCPinChoice: Hashable {
        let pin: AppOptionsDraft.LCPin
        let label: String
    }

    /// The picker's entries: Automatic, each LiveContainer on the default
    /// device, and the current pin when it names none of them.
    ///
    /// The current pin is matched the way the CLI matches it
    /// (`pinnedInstance`). The matching instance then carries the pin's own
    /// spelling as its tag, so the picker shows it selected rather than
    /// showing nothing; a pin that matches nothing — another phone's, or one
    /// deleted since — stays listed, because dropping it would silently
    /// re-point the app at Automatic on the next save.
    ///
    /// - Parameter instances: what the default device answered, or nil when
    ///   it has not — no default device, its status not read yet, or a phone
    ///   that could not be read. Only an answer can say a pin is "not on" the
    ///   phone; without one the pin is listed by itself, and `lcPinNote` says
    ///   why there is no list.
    static func lcPinChoices(
        instances: [LCInstanceRecord]?, current: String?, deviceName: String?
    ) -> [LCPinChoice] {
        let listed = instances ?? []
        let pinned = current.flatMap { pinnedInstance($0, in: listed) }
        var choices = [LCPinChoice(pin: .automatic, label: "Automatic")]
        for instance in listed {
            let isPinned = pinned == instance
            // The name alone fits the sheet's popup; the bundle id joins it
            // only when the name is not enough — two primaries from two
            // Apple IDs are both "LiveContainer", which is the very case a
            // pin exists to settle.
            let shared = listed.filter { $0.displayName == instance.displayName }.count > 1
            let label = shared && instance.displayName != instance.bundleId
                ? "\(instance.displayName) — \(instance.bundleId)"
                : instance.displayName
            choices.append(LCPinChoice(
                pin: .bundleId(isPinned ? (current ?? instance.bundleId) : instance.bundleId),
                label: label
            ))
        }
        if let current, pinned == nil {
            let label = instances == nil
                ? current
                : deviceName.map { "\(current) (not on \($0))" } ?? current
            choices.append(LCPinChoice(pin: .bundleId(current), label: label))
        }
        return choices
    }

    /// The LiveContainer a pin names, matched exactly as the CLI's
    /// `lc_discovery._pinned` matches it, or nil where the CLI would refuse
    /// the pin.
    ///
    /// Ignoring case; an exact match first — the one spelled as pinned, if
    /// two differ only in case — and only failing that, the one id the pin is
    /// a dotted prefix of. Exact and prefix matches used to be counted
    /// together, so `com.kdt.livecontainer` beside
    /// `com.kdt.livecontainer.ABCDE12345` found two, and the picker listed the
    /// pin a second time as "(not on …)", with the same tag as the row it was
    /// installed as. A pin outside the LiveContainer family matches nothing:
    /// the CLI refuses it before looking.
    static func pinnedInstance(_ pin: String, in instances: [LCInstanceRecord]) -> LCInstanceRecord? {
        let want = pin.lowercased()
        guard want.hasPrefix(liveContainerFamily) else { return nil }
        let exact = instances.filter { $0.bundleId.lowercased() == want }
        if !exact.isEmpty {
            return exact.first { $0.bundleId == pin } ?? exact[0]
        }
        let prefixed = instances.filter { $0.bundleId.lowercased().hasPrefix(want + ".") }
        return prefixed.count == 1 ? prefixed[0] : nil
    }

    /// Every LiveContainer bundle id starts with this, in any case
    /// (`lc_discovery.FAMILY_PREFIX`).
    private static let liveContainerFamily = "com.kdt.livecontainer"

    /// The instances a status lists, or nil when there is no list to trust:
    /// nothing read yet, or a phone that did not answer.
    static func instances(from status: LCStatusRecord?) -> [LCInstanceRecord]? {
        guard let status, status.reachable else { return nil }
        return status.instances
    }

    /// The phone the picker's list is read from, or nil when there is nothing
    /// to read: no picker, or no default device connected.
    ///
    /// The sheet's `.task` is keyed on this, so it runs again whenever the
    /// answer changes. Keyed on whether the picker shows, it ran once as the
    /// sheet opened — and with the phone not plugged in yet, returned and
    /// never ran again.
    static func lcPinLoadTarget(app: AppRecord, delivery: DeliveryMode, defaultUDID: String?) -> String? {
        offersLCPin(app: app, delivery: delivery) ? defaultUDID : nil
    }

    /// The line under the picker: where its list comes from, and what
    /// Automatic does.
    ///
    /// - Parameter otherDevicesConnected: whether any phone is connected. With
    ///   one plugged in and none of them the default — "Ask each time", or a
    ///   default that is another phone — plugging one in is not what is
    ///   missing.
    static func lcPinNote(
        deviceName: String?, hasLoaded: Bool, status: LCStatusRecord?,
        otherDevicesConnected: Bool = false
    ) -> String {
        let automatic = "Automatic uses the only LiveContainer on the phone, or its "
            + "primary. Pin one to push into another, or when there are two primaries."
        guard let deviceName else {
            if otherDevicesConnected {
                return "No connected device is your default. Choose a default device in "
                    + "Settings to list its LiveContainers. " + automatic
            }
            return "Plug in your default device (Settings) to choose from its "
                + "LiveContainers. " + automatic
        }
        guard hasLoaded else { return "Looking for LiveContainers on \(deviceName)…" }
        if let status, !status.reachable {
            return "Couldn't read \(deviceName): \(status.error ?? "no answer"). " + automatic
        }
        return automatic
    }

    /// The line under the auto-provisioning toggle when there is no key to
    /// make a profile with.
    static let autoProfileNeedsKey = "Needs an App Store Connect key. Add one in Settings."

    /// The auto-provisioning toggle's reason on a LiveContainer app.
    static let autoProfileNotUsed = "LiveContainer delivery doesn't use a provisioning profile."

    /// Why "Create the provisioning profile automatically" can't be changed,
    /// or nil when it can (C-AUTOPROFILE).
    ///
    /// Only a sideload is signed, so only a sideload uses a profile: on a
    /// LiveContainer app the toggle does nothing, and that reason wins over
    /// the other two, because neither a paid account nor a key would make it
    /// do anything. Only the control is locked — a saved `auto_profile` stays
    /// as it is, and takes effect if the app is switched back to sideload.
    ///
    /// A free Apple ID has no profile to make. A paid one needs a usable App
    /// Store Connect key to turn it *on* — `settings`' `app_store_connect`,
    /// with its .p8 on disk — but can always turn it off, which is the way out
    /// of an app that no longer builds without one. Nil disables nothing: an
    /// account not known yet, settings not read yet, or a CLI too old to
    /// report the key (`AccountMode`'s rule).
    static func autoProfileLock(
        delivery: DeliveryMode, account: AccountMode?, settings: SettingsRecord?, current: Bool
    ) -> String? {
        if delivery == .livecontainer { return autoProfileNotUsed }
        if account.isFree { return PaidOnly.autoProfile }
        guard !current, let settings, settings.reportsAppStoreConnect else { return nil }
        return settings.appStoreConnect?.isUsable == true ? nil : autoProfileNeedsKey
    }

    /// The sheet's lock: the delivery the picker now shows, which may not be
    /// the app's saved one yet, and the `auto_profile` config holds.
    static func autoProfileLock(
        draft: AppOptionsDraft, account: AccountMode?, settings: SettingsRecord?
    ) -> String? {
        autoProfileLock(
            delivery: draft.edited.delivery, account: account, settings: settings,
            current: draft.original.autoProfile
        )
    }

    private var autoProfileLock: String? {
        Self.autoProfileLock(draft: draft, account: store.account, settings: settings.settings)
    }

    /// The default device's LiveContainers, or nil until it has answered.
    private var defaultInstances: [LCInstanceRecord]? {
        Self.instances(from: defaultDevice.flatMap { liveContainer.status[$0.udid] })
    }

    static let storeLabel = "App Store"
    static let storeUnset = "Not set (this Mac's region's store, then the US)"
    static let storeHelp = "The App Store this app's update check and icon ask. "
        + "Add App's Search sets it to the store it searched."

    /// The store picker's choices: every storefront (`Storefronts`), by name,
    /// and "not set" first only for an app that has no store yet.
    static func storeChoices(current: String?) -> [String?] {
        (current == nil ? [nil] : []) + Storefronts.byName.map { Optional($0) }
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
                .paidOnly(Self.deliveryLocked(original: app.delivery, account: store.account)
                          ? PaidOnly.sideload : nil)
                if Self.offersLCPin(app: app, delivery: draft.edited.delivery) {
                    Picker("LiveContainer", selection: $draft.edited.lcPin) {
                        ForEach(Self.lcPinChoices(
                            instances: defaultInstances, current: app.lcBundleId,
                            deviceName: defaultDevice?.name
                        ), id: \.self) { choice in
                            Text(choice.label).tag(choice.pin)
                        }
                    }
                }
                if app.reportsStoreCountry {
                    Picker(Self.storeLabel, selection: $draft.edited.storeCountry) {
                        ForEach(Self.storeChoices(current: app.storeCountry), id: \.self) { code in
                            Text(code.map(Storefronts.label) ?? Self.storeUnset).tag(code)
                        }
                    }
                    .help(Self.storeHelp)
                }
                // Shown on every account, disabled with the reason where it
                // can't be turned on — the Profile cell on the app pane reads
                // the same field ("auto" / "manual", "not used" when free).
                Toggle("Create the provisioning profile automatically", isOn: $draft.edited.autoProfile)
                    .paidOnly(autoProfileLock)
                Toggle("Keep bundle id", isOn: $draft.edited.keepBundleId)
                Toggle("Strip extensions", isOn: $draft.edited.stripExtensions)
                Toggle("Strip supported devices", isOn: $draft.edited.stripSupportedDevices)
                Stepper("Compression level: \(draft.edited.compressionLevel)",
                        value: $draft.edited.compressionLevel, in: 0...9)
            }
            .formStyle(.grouped)

            if Self.offersLCPin(app: app, delivery: draft.edited.delivery) {
                Text(Self.lcPinNote(
                    deviceName: defaultDevice?.name,
                    hasLoaded: defaultDevice.map { liveContainer.hasLoaded.contains($0.udid) } ?? false,
                    status: defaultDevice.flatMap { liveContainer.status[$0.udid] },
                    otherDevicesConnected: !devices.devices.isEmpty
                ))
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.textDim)
                .fixedSize(horizontal: false, vertical: true)
            }

            if autoProfileLock == Self.autoProfileNeedsKey {
                Text(Self.autoProfileNeedsKey)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
                    .fixedSize(horizontal: false, vertical: true)
            }

            // The reason, whenever the account is free. Warn-colored while
            // the draft is still Sideload: that is an app no build will accept,
            // and the picker above is how it gets out of that state.
            if store.account.isFree {
                Text(PaidOnly.sideload)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(draft.edited.delivery == .sideload ? Palette.warn : Palette.textDim)
                    .fixedSize(horizontal: false, vertical: true)
            }

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

            if !savedWarnings.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text(Self.savedWithWarnings)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textDim)
                    // In the app pane's words for a CLI warning.
                    ForEach(savedWarnings, id: \.self) { warning in
                        Text("warning: \(warning)")
                            .font(AppFont.monoSmall)
                            .foregroundStyle(Palette.warn)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }

            HStack {
                Spacer()
                if savedWarnings.isEmpty {
                    // Disabled during the write — this also disables the Escape
                    // binding attached to it. Canceling used to dismiss the sheet
                    // while `--set-app-options` was mid-flight, so the user was
                    // told nothing happened while config.yaml was being rewritten.
                    // There is no "undo the write" to offer here; the honest
                    // behavior is to make them wait for the CLI to answer.
                    Button("Cancel") { onClose() }
                        .keyboardShortcut(.cancelAction)
                        .disabled(saving)
                    Button("Save") { startSave() }
                        .keyboardShortcut(.defaultAction)
                        .disabled(saving || !draft.hasChanges)
                } else {
                    // The change is on disk; all that is left is to read why
                    // it may not do what was asked.
                    Button(Self.doneLabel) { onClose() }
                        .keyboardShortcut(.defaultAction)
                }
            }
        }
        .padding(20)
        .frame(width: 460)
        .background(Palette.bg)
        // The sheet can still be torn down without Cancel — the app selection
        // changes, or the window closes. Cancel the Task so its result can't
        // be written into @State that no longer exists.
        .onDisappear { saveTask?.cancel() }
        // Re-read each time the picker appears rather than once per session:
        // installing LiveContainer2 on the device pane and coming straight
        // here is the way someone arrives wanting to pin it. The store keeps
        // its last answer on screen while this runs. Read again whenever the
        // default device changes too — plugged in with the sheet open, or
        // another phone made the default (`lcPinLoadTarget`).
        .task(id: Self.lcPinLoadTarget(
            app: app, delivery: draft.edited.delivery, defaultUDID: defaultDevice?.udid
        )) {
            guard let udid = Self.lcPinLoadTarget(
                app: app, delivery: draft.edited.delivery, defaultUDID: defaultDevice?.udid
            ), !liveContainer.loading.contains(udid) else { return }
            await liveContainer.load(udid: udid)
        }
        // Whether a key is set comes from Settings' read, which may not have
        // run yet this session; once is enough, since Settings re-reads it.
        .task {
            if settings.settings == nil { _ = try? await settings.loadSettings() }
        }
        // A delivery switch hides or locks controls: Sideload hides the
        // LiveContainer picker, and LiveContainer locks "Create the
        // provisioning profile automatically". An edit made to either before
        // the switch must not ride along on the save unseen.
        .onChange(of: draft.edited.delivery) { delivery in
            draft.revertEditsHiddenByDelivery(
                lcPinShown: Self.offersLCPin(app: app, delivery: delivery)
            )
        }
    }

    /// Above the CLI's warnings after a save that worked.
    static let savedWithWarnings = "Saved. graft said:"
    static let doneLabel = "Done"

    /// Whether the delivery picker is disabled: on a free Apple ID, for an app
    /// that is already LiveContainer, since Sideload is the only other choice
    /// and the CLI refuses it.
    ///
    /// A *sideload* app in a free config keeps the picker live. It is the way
    /// out — no build of it will run until it is LiveContainer — and choosing
    /// Sideload there is no change, so nothing refused can be sent.
    static func deliveryLocked(original: DeliveryMode, account: AccountMode?) -> Bool {
        account.isFree && original == .livecontainer
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
    /// Canceling `saveTask` *does* kill the subprocess — `CLIRunner.runOnce`
    /// SIGTERMs it — so a canceled save may or may not have landed, and the
    /// call can come back either with a result or with a `CancellationError`.
    /// Either way the sheet is gone by then, so the outcome goes to
    /// `store.lastError` — which outlives this view and is already surfaced by
    /// SidebarView — instead of into a `@State` nobody will ever see.
    private func save() async {
        defer { saving = false }
        do {
            let updated = try await store.updateAppOptions(appName: app.name, draft: draft)
            guard !Task.isCancelled else { return }
            // A save the CLI warned about stays open to say so: turning on
            // "Create the provisioning profile automatically" for an app
            // with its own `profile:` saves, and changes nothing until that
            // line goes, which only the warning says (fix-check #6/#26).
            let warnings = updated?.warnings ?? []
            if warnings.isEmpty { onClose() } else { savedWarnings = warnings }
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
