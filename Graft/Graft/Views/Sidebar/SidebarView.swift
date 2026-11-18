import SwiftUI

/// The app's one navigation column: every connected phone, every app, then
/// every section.
///
/// The apps used to live in a second column of their own, which cost a nested
/// `NavigationSplitView` — see `SidebarItem` for what that nesting did to the
/// window on every launch. Merging them here is what let that second split view
/// go away, and it is also the better shape for the content: this window has
/// one pane worth reading (the app detail) and a handful of destinations, so
/// spending two columns getting to the first was two columns of chrome.
///
/// Everything the apps column owned came with it — the add button, the remove
/// and stop-build confirmations, and the section's one error alert — because
/// all four are about the list, and the list is here now.
///
/// **The phones are at the top, above the apps.** They are the one part of this
/// column that is *live* — rows appear and disappear as hardware is plugged in
/// and unplugged, driven by `ContentView`'s five-second poll — and they are
/// what replaced a Devices section that used to sit at the bottom beside
/// Settings. A phone is a place now, not a screen listing places, so it belongs
/// where the other places are and above the things that get delivered to it.
struct SidebarView: View {
    @Bindable var store: ConfigStore
    @Binding var selection: SidebarItem?
    /// The store, because the lookup is per-app and needs each row's bundle
    /// id. The rows themselves are handed only the resolved image — see the
    /// call below for why that distinction is deliberate.
    let artwork: ArtworkStore
    /// The connected phones, and the ones that could not be described.
    ///
    /// The store rather than the two arrays, because both are `private(set)` on
    /// it and reading them here is what makes this list observe them — the poll
    /// that rewrites `devices` has to repaint this column or the rows are a
    /// snapshot of launch. Nothing here loads or refreshes it; `ContentView`
    /// owns that, so a sidebar rendered in a test spawns no subprocesses.
    let devices: DevicesStore

    @State private var showingAddSheet = false
    @State private var pendingRemove: AppRecord?
    @State private var pendingCancel: AppRecord?

    var body: some View {
        List(selection: $selection) {
            devicesSection
            Section {
                ForEach(store.apps) { app in
                    AppSidebarRow(
                        app: app,
                        state: store.deliveryStates[app.name],
                        isSelected: selection == .app(app.id),
                        // The image, not the store. Resolving it here means
                        // this view — the one that owns the `ForEach` — is the
                        // single observer of `ArtworkStore.generation`, so one
                        // load repaints the list once. Handed the store, every
                        // row would observe it separately for an icon that
                        // cannot change without the list being rebuilt anyway.
                        artwork: artwork.image(for: app.bundleId)
                    )
                    .tag(SidebarItem.app(app.id))
                    .contextMenu {
                        // Removing an app mid-build rewrites config.yaml, reloads
                        // `store.apps`, and destroys AppDetailView along with its log
                        // panel — while the CLI subprocess sails on and installs the
                        // IPA minutes later with no progress and nothing to stop it.
                        // Gate the entry and say why, rather than offering an action
                        // whose consequences the UI can't show.
                        Button("Remove \(app.name)…", role: .destructive) {
                            pendingRemove = app
                        }
                        .disabled(store.buildingApp == app.name)
                        if store.buildingApp == app.name {
                            Text("Building — can't remove until the build finishes")
                            // The one cancel that survives navigation. The detail
                            // pane's Stop button goes away with the pane on any
                            // sidebar-section change, and "clicked away, then noticed
                            // it was wedged" is exactly the case a cancel exists for.
                            Button("Stop building \(app.name)…") {
                                pendingCancel = app
                            }
                        }
                    }
                }
            } header: {
                // The count moved off a row and onto the header when "Apps"
                // stopped being a destination. It reads the same and no longer
                // has to survive a selection fill — see `Palette.onAccentMuted`
                // for why that mattered when it did.
                HStack {
                    Text("Apps")
                    Spacer()
                    Text("\(store.apps.count)")
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textDim)
                        .monospacedDigit()
                }
            }
            Section("Library") {
                row(.tweaks, label: "Tweaks", symbol: Symbols.tweak)
                row(.canister, label: "Canister", symbol: Symbols.canisterSection)
                row(.repos, label: "Repos", symbol: Symbols.reposSection)
                // Library, not Activity. The cache holds artifacts you have
                // acquired, which is what Tweaks, Canister and Repos are;
                // Builds and Settings are things you do and things you set.
                // A staged IPA is the same kind of noun as a .deb sitting in a
                // repo — the difference is only that a dump put it there.
                row(.cache, label: "Cache", symbol: Symbols.cacheSection)
            }
            Section("Activity") {
                row(.builds, label: "Builds", symbol: Symbols.buildsSection)
                // No Devices row. The phones are destinations at the top of
                // this column now — see `devicesSection` and `SidebarSection`,
                // which records why the case was removed rather than orphaned.
                row(.settings, label: "Settings", symbol: Symbols.settingsSection)
            }
        }
        .listStyle(.sidebar)
        .scrollContentBackground(.hidden)
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button {
                    showingAddSheet = true
                } label: {
                    Symbols.add.image
                }
                .help("Add a new app")
            }
        }
        .sheet(isPresented: $showingAddSheet) {
            AddAppSheet(store: store) { newName in
                // Tagged straight from the name, with no lookup in `store.apps`
                // first. `AppRecord.id` *is* `name` (Models/BuildEvent.swift),
                // so this already matches a row.
                //
                // And it must not be made conditional on the app having
                // arrived in `store.apps` yet: `addApp` downgrades a failed
                // post-write reload to `lastError` and still reports the name,
                // so this closure can run while the list is one refresh behind.
                // Wrapped in a `first(where:)` it silently selected nothing in
                // exactly that case, leaving the new app unselected once the
                // next refresh landed — no confirmation the add had worked.
                selection = .app(newName)
            }
        }
        .alert(
            "Remove this app?",
            isPresented: $pendingRemove.isPresent(),
            presenting: pendingRemove
        ) { app in
            Button("Cancel", role: .cancel) {}
            Button("Remove", role: .destructive) {
                Task { await remove(app) }
            }
        } message: { app in
            Text("\(app.name) will be removed from your config. Cached IPAs, "
                 + "archived builds, and build history are kept.")
        }
        // Same confirmation the detail pane shows, from the same place — the
        // sentence depends on the stage the build has reached, which only the
        // store knows, and the title must not depend on which control was used.
        .stopBuildConfirmation(
            presenting: $pendingCancel,
            name: \.name,
            consequence: { store.cancelConsequence }
        ) { app in store.cancelBuild(appName: app.name) }
        // `ConfigStore`'s one error channel, and now the app's: this column is
        // mounted in every section, where `AppsListView` was mounted only while
        // Apps was open. That is a deliberate widening — a build finishing
        // while the user is in Devices used to fail its badge refresh into a
        // view that no longer existed, so the error was dropped on the floor —
        // but it does mean a background failure can interrupt an unrelated
        // pane, and that is the cost being accepted here.
        //
        // Routing it here also keeps an error alive across navigation: a local
        // @State on the detail pane would be torn down if the user switched
        // sections mid-remove.
        .alert(
            // Weaker than the "Couldn't apply that change" this used to say,
            // and weaker on purpose. That title was written when the only
            // things on this channel were mutations from the Apps section; it
            // now also carries `refreshDeliveryStates`' failures, which change
            // nothing and which the user did not ask for. Claiming a change was
            // attempted would be wrong for those. Every message on this channel
            // names what actually failed, so the title does not need to.
            "Something didn't work",
            isPresented: $store.lastError.isPresent()
        ) {
            Button("OK", role: .cancel) { store.lastError = nil }
        } message: {
            Text(store.lastError ?? "")
        }
    }

    /// The phones, live.
    ///
    /// Two kinds of row, because the CLI answers with two kinds of record and
    /// they are not the same fact: a phone it described, and a phone usbmux
    /// enumerated that then failed to answer a query. Both are connected. Only
    /// the first can be opened, because only the first has anything for a pane
    /// to render.
    ///
    /// Nothing here refreshes the list on its own. `ContentView`'s poll owns
    /// that, which is what keeps this column paintable in a test without
    /// spawning a subprocess.
    @ViewBuilder
    private var devicesSection: some View {
        Section("Devices") {
            ForEach(devices.devices) { device in
                DeviceSidebarRow(
                    device: device,
                    isSelected: selection == .device(device.udid)
                )
                .tag(SidebarItem.device(device.udid))
                .contextMenu {
                    // Both entries came off `DevicesView`'s device card, which
                    // this row replaces. They are the two strings on a phone
                    // nobody can retype from memory, and the pane shows both in
                    // full — this is the shortcut, not the only copy.
                    Button("Copy UDID") { Pasteboard.copy(device.udid) }
                    if let bundleId = device.livecontainerBundleId {
                        Button("Copy LiveContainer bundle id") { Pasteboard.copy(bundleId) }
                    }
                }
            }
            ForEach(devices.deviceErrors) { failure in
                UndescribableDeviceRow(failure: failure, isRetrying: devices.isLoading) {
                    Task { await retryDeviceListing() }
                }
                .contextMenu {
                    Button("Copy UDID") { Pasteboard.copy(failure.udid) }
                }
            }
            if devices.devices.isEmpty && devices.deviceErrors.isEmpty {
                // Three states, not two, and the third is the reason
                // `DevicesStore.hasLoaded` exists: before the first listing
                // lands, "No devices connected" is a claim nothing has checked.
                // It is a moment rather than a resting state — `ContentView`
                // runs a full listing on activation — but it is the moment
                // every launch passes through.
                Text(devices.hasLoaded ? "No devices connected" : DevicesStore.scanningLine)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
                    .lineLimit(1)
                    .help("Plug an iPhone in over USB, or wake a paired device on the "
                          + "same network.")
            }
        }
    }

    /// Re-describe every phone, after the user asked.
    ///
    /// The failure goes to `ConfigStore.lastError` — this column's alert — and
    /// not to `DevicesStore.lastError`, which is rendered by `DeviceDetailView`
    /// and so has nowhere to draw when no phone can be opened. That is exactly
    /// the case this button exists for: one connected phone, unreadable, no
    /// pane to put a banner on. A retry that fails silently is a button that
    /// appears to do nothing.
    private func retryDeviceListing() async {
        do { _ = try await devices.load() }
        catch { store.lastError = error.localizedDescription }
    }

    /// One section destination.
    ///
    /// The icon knows whether its row is selected, which is the one thing a
    /// `Label` in a `List(selection:)` is not told: SwiftUI paints the row's
    /// fill and leaves the contents alone. Six rows here and only four of
    /// them can be filled — `magnifyingglass` and `list.bullet` have no fill
    /// variant — so `Symbol.image(selected:)` carries both mechanisms and this
    /// row does not have to know which one applies to it.
    @ViewBuilder
    private func row(_ section: SidebarSection, label: String, symbol: Symbol) -> some View {
        symbol.label(label, selected: selection == .section(section))
            .font(AppFont.body)
            .tag(SidebarItem.section(section))
    }

    private func remove(_ app: AppRecord) async {
        // Re-checked at the point of action, not just in the menu: the
        // confirmation alert can sit open across the start of a build, and the
        // menu's `.disabled` is only a snapshot of the moment it was drawn.
        guard store.buildingApp != app.name else {
            store.lastError = "\(app.name) is building right now. "
                + "Wait for the build to finish, then remove it."
            return
        }
        do {
            _ = try await store.removeApp(appName: app.name)
            // Cleared rather than moved to a neighbour. The detail pane for a
            // removed app has nothing to show, and silently selecting the next
            // app down would put a different app's Build & Install under a
            // cursor that was just aimed at a destructive confirmation.
            if selection == .app(app.id) { selection = nil }
        } catch {
            store.lastError = error.localizedDescription
        }
    }
}
