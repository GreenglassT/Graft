import SwiftUI

/// The fixed destinations — everything in this column that is not a *thing*.
///
/// **There is deliberately no `devices` case.** A single Devices pane listing
/// every connected phone was the shape this app had, and it is what the
/// per-device panes replace: each phone is now a destination of its own,
/// `SidebarItem.device`. Removing the case rather than leaving it unused is
/// what forced every `switch` over this enum to be revisited — an unreachable
/// second renderer of device state is exactly how two views drift on what
/// "connected" means, and it is why `DevicesView.swift` is deleted rather than
/// merely unrouted.
enum SidebarSection: Hashable {
    case tweaks
    case canister
    case repos
    /// The IPA cache — every file a dump staged, whether or not any phone or
    /// any configured app has a use for it.
    ///
    /// In sidebar order, like the rest of this enum: fourth under Library,
    /// ahead of Activity's two. Adding the case is also what makes the route
    /// non-optional — `sectionContent`'s `switch` has no `default`, so a case
    /// declared here and not handled there does not compile.
    ///
    /// The compiler forces the arm to *exist*; it has nothing to say about the
    /// arm building the right pane, and for a while nothing else did either —
    /// pointing this case at `ReposView` passed all 691 tests. That is now
    /// `WindowLayoutTests.test_every_section_route_lands_on_the_pane_it_names`,
    /// which reads `NSWindow.title` back from a hosted `ContentView` and covers
    /// every case in this enum. **A section added here without a row in that
    /// file's table is a route nothing checks**, which was the state this one
    /// shipped in.
    case cache
    case builds
    case settings
}

/// A destination in the app's one navigation column: an app, a phone, or a
/// section.
///
/// Apps used to be a section of their own, opening a second list in a second
/// column — which meant a `NavigationSplitView` nested inside another one's
/// detail. On macOS the inner one lays out for a width it does not have, and
/// the content that falls past the window's edge is clipped: Build & Install,
/// Check device, every tweak row's ⊖.
///
/// The one number worth trusting here is the measured one. Hosting this view in
/// a real window and walking the `NSView` tree (`WindowLayoutTests`) put the
/// nested version **120pt past the right edge at 700, 900 and 1470** — a
/// constant, not a fraction of the window. `GraftApp`'s 1200pt minimum was
/// an attempt to outrun it, and could not work for exactly that reason: a
/// bigger window does not make a constant overhang smaller.
///
/// Worth recording what this was *not*, because the first diagnosis was wrong
/// and cost an afternoon: AppKit's autosaved `NSSplitView Subview Frames` show
/// the detail column at the full window width with the sidebar over it
/// (`sidebar 228 + detail 1200` in a 1200pt window). That reads like a 228pt
/// overflow and is not one — it is how AppKit represents this split normally,
/// and it is identical for the single-column layout here.
///
/// One column and one split view removes the nesting rather than sizing around
/// it, and hands most of the second column's width back to the detail pane —
/// which is the only part of this window that has ever wanted more.
enum SidebarItem: Hashable {
    case app(AppRecord.ID)
    /// One connected phone, by UDID.
    ///
    /// The UDID and not a `DeviceRecord`, for the reason `.app` carries an id
    /// rather than a record: the pane looks the phone up in `DevicesStore` on
    /// every draw, so it always renders whatever the store currently holds
    /// rather than a copy pinned when the row was clicked.
    ///
    /// **What that does and does not keep current**, because the two are easy
    /// to conflate and this comment used to. The store's records are replaced
    /// by a `load()` — a full `--devices`, one lockdown handshake per phone —
    /// and the background poll escalates to one only when the *set of UDIDs on
    /// the bus* changes (`DevicesStore.pollAndEscalateIfChanged`). So a phone
    /// plugged in or unplugged is picked up within a tick, and everything that
    /// leaves the set identical is not: renaming the phone in Settings,
    /// installing LiveContainer on it, upgrading iOS, moving it from USB to
    /// Wi-Fi. Those keep showing their old values in the header until
    /// something runs a full listing — the Refresh on `DeviceDetailView`, the
    /// sidebar's retry, or a phone arriving or leaving.
    ///
    /// That is the two-tier design working, not a gap in it: escalating on
    /// every tick would spend ~1s of subprocess per phone every five seconds
    /// to catch a rename. Carrying the record in the selection instead would
    /// be strictly worse — the values would then be stale against the store as
    /// well as against the phone, and a Refresh could not fix them.
    ///
    /// The lookup is also what lets a selection survive a phone dropping off
    /// the bus for a beat; see `sectionContent`.
    case device(String)
    case section(SidebarSection)
}

struct ContentView: View {
    @Bindable var store: ConfigStore
    @State private var tweaksStore: TweaksStore
    @State private var canisterStore: CanisterStore
    @State private var repoStore: RepoStore
    @State private var buildsStore: BuildsStore
    @State private var devicesStore: DevicesStore
    @State private var settingsStore: SettingsStore
    /// Owned here rather than per-view, because four surfaces draw the same
    /// icons — the sidebar rows, the detail header, and the two builds sheets
    /// — and a store each would mean four `--artwork` runs resolving the same
    /// files. One store, and every view below reads the same cache.
    @State private var artworkStore: ArtworkStore
    /// Per-phone vitals, per-phone delivery states and per-phone installed-app
    /// listings, for `DeviceDetailView`.
    ///
    /// Owned here for the same reason `artworkStore` is, and it is the reason
    /// each is keyed by udid rather than holding one phone's answer: this
    /// window shows one phone at a time but switches between them by clicking
    /// a sidebar row, and a store per pane would re-run a lockdown query, a
    /// delivery read and a ten-second app enumeration every time somebody
    /// looked back at a phone they had already opened.
    ///
    /// `deviceAppsStore` is the newest of the three here and was the one built
    /// per pane — twice per pane, in fact, once for the dump and once for the
    /// listing. Neither instance could name the phone it described, so the
    /// listing had to be thrown away on every switch (`.id(device.udid)`) and
    /// the decrypt card showed the previous phone's readiness under the new
    /// phone's name. See `DeviceAppsStore`.
    @State private var deviceInfoStore: DeviceInfoStore
    @State private var deviceDeliveryStore: DeviceDeliveryStore
    @State private var deviceAppsStore: DeviceAppsStore
    /// LiveContainer per device. Owned here for `deviceAppsStore`'s reason:
    /// its status is keyed by udid, so switching phones and back shows the
    /// last answer at once instead of a fresh "Checking…".
    @State private var liveContainerStore: LiveContainerStore
    /// What the IPA cache holds, keyed by bundle id.
    ///
    /// **Not owned here for the reason `deviceAppsStore` is above.** That
    /// store avoids a fresh 8.4–10.8s listing on every phone switch; this
    /// store's own `load()` is ~0.25s and this pane already re-runs it
    /// unconditionally on every appearance and after every dump, so there is
    /// no per-switch cost sharing it avoids — `DeviceDetailView` is built
    /// without `.id(device.udid)`, so SwiftUI reuses one instance across a
    /// switch regardless of who owns the store underneath it. What owning it
    /// here does buy: `hasLoaded` and `byBundleId` survive a detour through
    /// an app or settings pane, the previous join stays on screen during a
    /// ~0.25s reload instead of every row reverting to its pre-feature shape,
    /// and it keeps this store declared and initialized beside its siblings
    /// — `deviceInfoStore`, `deviceDeliveryStore`, `deviceAppsStore` — so a
    /// reader looking for "where the device pane's stores live" finds them
    /// together rather than one singled out below. See `StagedIPAStore`, and
    /// `DeviceDetailView`'s own `staged` doc for the fuller version of this
    /// same reasoning.
    @State private var stagedIPAStore: StagedIPAStore
    /// App Store status, per-app version checks and running gets. Read by
    /// Settings (is this Mac set up?) and by the app pane (Get latest), so it
    /// lives here: one status load per session rather than one per pane entry.
    @State private var appStoreStore: AppStoreStore
    /// The first-run screen's account setup, and Settings › Account's. Two, so
    /// a Settings flow's result never holds up the first-run screen.
    @State private var accountSetupStore: AccountSetupStore
    @State private var settingsAccountStore: AccountSetupStore
    @State private var selection: SidebarItem?
    /// The Cache pane's "Add as app", mid-flight.
    ///
    /// `.sheet(item:)` rather than `.sheet(isPresented:)` for the reason
    /// `DeviceDetailView.addingApp` gives: the payload *is* the presentation,
    /// and a `Bool` beside a separately-held `Prefill` can disagree with it.
    @State private var addingApp: AddAppSheet.Prefill?
    /// Whether this window is in the foreground.
    ///
    /// **The three-state description that stood here is stale on this
    /// deployment target.** It said `.key` while holding keyboard focus,
    /// `.active` while the app is frontmost but a sheet is key, and
    /// `.inactive` otherwise — which is `ControlActiveState`'s classic
    /// meaning and the reason the guard below is written as "anything but
    /// `.inactive`". Since **macOS 15.0** this Xcode's `SwiftUICore.swiftdoc`
    /// says the value is "strictly mapped to and from
    /// `EnvironmentValues.appearsActive`": `true` reads back `.key`, `false`
    /// reads back `.inactive`, and **`.active` is not produced at all**. So
    /// the sheet case the old comment justified cannot arise, and the guard's
    /// real meaning is narrower than it looks — it admits the key window and
    /// nothing else.
    ///
    /// The guard is left as `!= .inactive` rather than `== .key`: it is
    /// correct under both readings, and pinning it to `.key` would bet the
    /// poll on documentation for one OS version. What changed is the claim,
    /// not the code. `DeviceDetailView`'s vitals poll records the same
    /// finding — it is the branch that found it — and both say plainly that
    /// this is read from the SDK's own documentation rather than observed
    /// with two windows open.
    ///
    /// Chosen over `NSApplication.didBecomeActiveNotification` /
    /// `willResignActiveNotification` because those describe the *application*
    /// and this is a per-window question — two Graft windows are two
    /// polls, and only the one you are looking at should be paying for one.
    /// It also needs no `@State` mirror to hold the answer between
    /// notifications, so there is no second copy of the truth to go stale.
    @Environment(\.controlActiveState) private var controlActiveState
    /// Whether `loadApps()` has come back successfully at least once.
    ///
    /// Separates "your config has no apps" from "we never managed to read your
    /// config", which `store.apps.isEmpty` alone cannot — see the `.none`
    /// branch of `sectionContent`.
    @State private var didLoadApps = false
    /// The missing tools the user closed the window's notice on, so it stays
    /// closed for that list and comes back if a different tool goes missing.
    @State private var dismissedToolsNotice: [MissingTool]?
    /// Whether a return refresh is running, and when the last one finished:
    /// returns that come in a burst cost one refresh. See `ReturnRefreshGate`.
    @State private var returnRefresh = ReturnRefreshGate()
    /// Whether a newer Graft is out: the banner under the panes, the daily
    /// check, and Check for Updates…'s alert. `GraftApp`'s, since the Help
    /// menu reaches it too. Nil only in a test that hosts this window for
    /// something else: no check runs and no banner shows.
    private let updates: UpdateStore?
    /// Handed to each app pane, for Build options' "This Mac" section. See
    /// `PlayCover`.
    private let playCover: PlayCover

    /// - Parameter selection: what to open on, for a test that needs to reach a
    ///   pane it cannot click its way to.
    ///
    ///   **A test seam, and it is declared as one rather than dressed up as a
    ///   feature.** `selection` is `@State` and private, and the app has exactly
    ///   one call site (`GraftApp`) which passes nothing — so in production
    ///   this parameter does not exist. What it buys is the only automated check
    ///   that a `SidebarSection` case reaches the pane it names: `.navigationTitle`
    ///   propagates to `NSWindow.title` whenever an `NSHostingView` is the window's
    ///   `contentView`, so a hosted `ContentView` can be *asked* which pane it is
    ///   showing. Nothing else in this app can answer that question — a `List`
    ///   cell tells a test nothing about its contents (`ClickHarness.
    ///   backingTableView(in:)`), and synthetic clicks on the sidebar's rows were
    ///   measured not to move the selection at all.
    ///
    ///   Before this existed, routing `.section(.cache)` to `ReposView` passed
    ///   the entire 691-test suite. See `WindowLayoutTests.
    ///   test_every_section_route_lands_on_the_pane_it_names`, which now closes
    ///   every section arm at once rather than the one that prompted it.
    ///
    ///   Defaulted, so no production call site mentions it and the seam cannot
    ///   quietly become a way to launch the app somewhere unexpected. The launch
    ///   `.task` below still opens on the first app when this is nil, and still
    ///   leaves a non-nil selection alone — which is what makes the injected
    ///   value survive the fixture load rather than being overwritten by it.
    /// `adminShell` is the one Set Up Decryption runs behind macOS's
    /// administrator prompt. No default: `GraftApp` passes the real one, a
    /// test a fake (`NoRealDefaultsTests`).
    /// `openApp` is Set Up Decryption's "Open <App>": `GraftApp` passes
    /// NSWorkspace's, and the default, which every test here takes, opens
    /// nothing.
    /// `playCover` is the app pane's: whether PlayCover is installed, and Get
    /// PlayCover's page. `GraftApp` passes LaunchServices and the browser; the
    /// default, which every test here takes, finds no PlayCover and opens
    /// nothing.
    init(
        store: ConfigStore, selection: SidebarItem? = nil, adminShell: PrivilegedShell,
        updates: UpdateStore? = nil,
        openApp: @escaping @MainActor (URL) -> Void = { _ in },
        playCover: PlayCover = .inert
    ) {
        self._store = Bindable(store)
        self._selection = State(initialValue: selection)
        self.updates = updates
        self.playCover = playCover
        self._tweaksStore = State(initialValue: TweaksStore(runner: store.runner))
        self._canisterStore = State(initialValue: CanisterStore(runner: store.runner))
        self._repoStore = State(initialValue: RepoStore(runner: store.runner))
        self._buildsStore = State(initialValue: BuildsStore(runner: store.runner))
        self._devicesStore = State(initialValue: DevicesStore(runner: store.runner))
        self._settingsStore = State(initialValue: SettingsStore(runner: store.runner))
        self._artworkStore = State(initialValue: ArtworkStore(runner: store.runner))
        self._deviceInfoStore = State(initialValue: DeviceInfoStore(runner: store.runner))
        self._deviceDeliveryStore = State(initialValue: DeviceDeliveryStore(runner: store.runner))
        self._deviceAppsStore = State(initialValue: DeviceAppsStore(runner: store.runner))
        self._liveContainerStore = State(initialValue: LiveContainerStore(runner: store.runner))
        self._stagedIPAStore = State(initialValue: StagedIPAStore(runner: store.runner))
        self._appStoreStore = State(initialValue: AppStoreStore(runner: store.runner, shell: adminShell, openApp: openApp))
        self._accountSetupStore = State(initialValue: AccountSetupStore(runner: store.runner))
        self._settingsAccountStore = State(initialValue: AccountSetupStore(runner: store.runner))
    }

    var body: some View {
        NavigationSplitView {
            SidebarView(
                store: store, selection: $selection,
                artwork: artworkStore, devices: devicesStore,
                // Add App stages a chosen IPA through it, as the app pane's
                // drop zone does.
                appStore: appStoreStore
            )
                // Wider than the 200/220/260 this column used to get, because
                // it now carries the app rows the second column used to: an
                // icon, a name, a bundle id, a delivery dot and a mode chip.
                //
                // The two columns it replaces were 200/220/260 and 280/320/380
                // — 540pt of ideal between them, against 260 here. So a little
                // under half, and ~280pt of ideal width moves to the detail
                // pane. (Not "a third", and not the ~330pt an earlier draft of
                // this comment claimed: that figure forgot this column was
                // widened at the same time.)
                .navigationSplitViewColumnWidth(min: 220, ideal: 260, max: 360)
        } detail: {
            detailContent
                // Under the setup screen and every pane alike: a newer Graft
                // may be the fix for whatever is on screen. Quiet, like the
                // tools notice, and below it.
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    if let updates, let notice = updates.notice {
                        UpdateNotice(
                            notice: notice,
                            onUpdate: { updates.update() },
                            onRestart: { updates.restartToUpdate() },
                            onOpenPage: { updates.openNoticePage() }
                        )
                    }
                }
        }
        .task {
            await checkSetup(.launch)
        }
        // Coming back to the window is the moment a user who just ran the
        // command in Terminal expects the app to have noticed. On the setup
        // screen that is the setup check again, but with the login shell's
        // environment from launch: asking the shell again is Check again's
        // alone (`SetupCheck.asksShellAgain`).
        //
        // Once set up, it is a quiet re-read of the app list (`refreshOnReturn`).
        // This used to do nothing at all past setup, and the list is what
        // carries the account every pane gates on — so `graft --init` in
        // Terminal turning a free config paid (or a hand edit removing
        // `signing:`) went unnoticed until an app was added or removed, or
        // until Settings' Re-run happened to be pressed: entering Settings
        // runs `--check` only while it has no rows, which after the launch
        // scan is never.
        //
        // Only a return from `.inactive` (`isReturn`), and the refresh is
        // coalesced (`returnRefresh`): a sheet or an alert taking key is not
        // the window coming back, and a run of them must not be a run of
        // subprocesses.
        .onChange(of: controlActiveState) { previous, active in
            guard Self.isReturn(from: previous, to: active) else { return }
            if store.setupNeed != nil {
                Task { await checkSetup(.windowReturn) }
            } else if didLoadApps, returnRefresh.begin() {
                Task {
                    await Self.refreshOnReturn(config: store, settings: settingsStore)
                    returnRefresh.end()
                }
            }
        }
        // No launch-time `--settings` read for the DEFAULT TARGET chip any
        // more. That used to be the only channel `default_device_udid`
        // reached this app through, and it was read here — detached, failure
        // swallowed — purely so a device pane opened before anyone visited
        // Settings still drew the chip. Now `--devices` echoes `is_default`
        // onto the device's own event (`DeviceRecord.isDefault`), so the chip
        // is correct from the very first listing and there is no gap left to
        // work around. `settingsStore` is still built above and still used by
        // the Settings pane below; only this launch-time read is gone.
        // The sidebar's device rows, kept live.
        //
        // Two tiers, because the honest listing is expensive: `--devices` pays
        // a lockdown handshake per phone (0.90–1.03s measured here) where
        // `--devices --brief` is usbmux enumeration alone (0.17s).
        // `pollAndEscalateIfChanged` runs the cheap one and pays for the
        // expensive one only when the set of UDIDs on the bus has actually
        // moved — see its doc comment, which also records why a failure here is
        // swallowed rather than surfaced.
        //
        // **Keyed on `controlActiveState`, which is what stops it.** `.task(id:)`
        // cancels and restarts its body whenever the id changes, so the loop
        // below is torn down the moment this window stops being frontmost and
        // started again when it comes back. A window sitting behind Xcode
        // spawning a subprocess every five seconds forever is a background cost
        // nobody asked for, and this app is a GUI over a CLI — every poll is a
        // real process.
        //
        // The poll runs *before* the first sleep rather than after it, so
        // returning to the app is itself a refresh: plug a phone in while
        // reading a diff, come back, and the row is there rather than up to
        // five seconds later.
        //
        // This is also the only thing that loads the device list at all now.
        // `--devices` used to be run by the Devices section's entry; with that
        // section gone the sidebar would have stayed empty forever without
        // this. `AppDetailView` and `SettingsView` both read this same store
        // rather than owning one apiece — see `SettingsView.devicesStore`'s
        // doc comment and `AppDetailView.devices`'s for what a private one
        // cost each of them before this.
        .task(id: controlActiveState) {
            guard controlActiveState != .inactive else { return }
            // The first listing is the expensive one, and it is unconditional.
            // The poll escalates only when the *set* of UDIDs on the bus
            // changes, and with nothing plugged in that set never changes from
            // empty — so `hasLoaded` would stay false forever and the sidebar
            // could not tell "no phone is connected" from "we haven't looked
            // yet". One `--devices` per launch buys that distinction; the flag
            // is set on every exit path, so a failure costs one attempt rather
            // than one per five seconds.
            //
            // Swallowed for the reason the poll's own failures are (see
            // `pollAndEscalateIfChanged`): nobody asked for this, and
            // `lastError` is a modal alert only its own OK button clears. A CLI
            // that cannot run at all is already reported by `loadApps` above.
            if !devicesStore.hasLoaded {
                _ = try? await devicesStore.load()
            }
            while !Task.isCancelled {
                await devicesStore.pollAndEscalateIfChanged()
                // `try?` swallows the cancellation this throws on teardown; the
                // loop condition above is what actually ends it.
                try? await Task.sleep(nanoseconds: 5_000_000_000)
            }
        }
        // Keyed on readiness as well as the selection. While the setup screen
        // is up no pane is on screen, and a section's load would only fail
        // into that pane's `lastError` — an alert waiting for whoever opens it
        // after setup. Keyed this way, the load runs when setup finishes for
        // whatever is selected by then.
        .task(id: [AnyHashable(selection), AnyHashable(store.setupNeed == nil)]) {
            guard store.setupNeed == nil else { return }
            // Selecting an app or a phone is a selection change too now that
            // both share this column, so this task restarts on every one of
            // them. Nothing below it wants to run for either — `AppDetailView`
            // and `DeviceDetailView` each load their own — so bail before any
            // of it rather than letting the comparisons fall through.
            guard case let .section(section) = selection else { return }
            // Every catch below routes through this rather than assigning
            // `lastError` directly, because leaving a section *cancels* this
            // task — and a canceled load is not a failed one.
            //
            // The distinction is invisible from the error itself. Canceling
            // SIGTERMs the child, `CLIRunner` reports the kill as an ordinary
            // non-zero exit, and the resulting RunError is indistinguishable
            // from a real failure. Written to the store it *latches*: nothing
            // clears `lastError` but the alert's OK button, so the next visit
            // to that section opened with a modal describing a subprocess the
            // user killed by clicking somewhere else.
            //
            // Merging the columns is what made this routine. An app row and a
            // section row are now one click apart in the same list, so
            // navigating away mid-load is an ordinary gesture rather than a
            // deliberate section switch. `ConfigStore.refreshDeliveryStates`
            // guards its own cancellation for the same reason.
            func report(_ error: Error, to assign: (String) -> Void) {
                guard !Task.isCancelled else { return }
                assign(error.localizedDescription)
            }
            // Tweaks reloads on *every* entry, not just when the list is empty
            // like the sections below, and not on `TweaksStore.hasLoaded` like
            // the app pane's first fill. Attaching or detaching a tweak happens
            // in the Apps section — against this very store now that
            // `AppDetailView` is handed it, which was not always true — and
            // what it changes is each record's `usedBy`, which nothing
            // recomputes short of re-running `--tweaks`: `ConfigStore`'s
            // attach/detach rewrite config.yaml and reload the *apps*. So the
            // list here still goes stale with no signal, and the Tweaks rows
            // gate a destructive action on exactly that field: the trash
            // confirmation only warns "attached to X" when `usedBy` is
            // populated, and the Orphans-only filter only hides the row when
            // it isn't. Serving a stale snapshot walks the user into a delete
            // that only the CLI's exit 16 stops. One extra subprocess per
            // section entry is the cheaper mistake.
            if section == .tweaks {
                do { _ = try await tweaksStore.loadTweaks() }
                catch { report(error) { tweaksStore.lastError = $0 } }
                // The folder the empty state names, as the CLI resolves it.
                // Read once; a failure costs the path in one sentence, which
                // then names no folder rather than a wrong one.
                if settingsStore.paths == nil {
                    _ = try? await settingsStore.loadSettings()
                }
            }
            if section == .repos && repoStore.repos.isEmpty {
                do { _ = try await repoStore.loadRepos() }
                catch { report(error) { repoStore.lastError = $0 } }
            }
            if section == .builds && buildsStore.builds.isEmpty {
                do { _ = try await buildsStore.load() }
                catch { report(error) { buildsStore.lastError = $0 } }
            }
            // No `.devices` arm any more, and nothing replaces it here: the
            // device list is loaded and kept current by the poll above, which
            // runs whatever is selected — the sidebar's rows need it even while
            // the user is looking at an app.
            //
            // No `.cache` arm either, and that one is a placement decision
            // rather than an absence. `CachePaneView` carries its own `.task`,
            // the way `AppDetailView` and `DeviceDetailView` do, because
            // `stagedIPAStore` is the one store up here that is *not* this
            // pane's alone — the device pane reads it too, and reloads it after
            // every dump. A load driven from here would be a second, invisible
            // owner of a store two panes already write. The behavior is the
            // same either way: this task and that one both restart when the
            // selection moves onto the pane, so a cache that changed on disk
            // while the user was elsewhere is re-read on the way back in.
            if section == .settings {
                // Concurrently, because they are independent: the preflight
                // writes `checks` and shells out to every tool it inspects, the
                // read writes `settings` and touches one YAML file. Awaited in
                // sequence they added up (~205ms + ~150ms); started together
                // the pane waits for the slower one alone.
                //
                // Unlike the sections above, the settings read is *not* gated
                // on being empty: the fields are seeded from it, and a stale
                // seed is an edit posted over someone else's change. Worth
                // saying plainly that it isn't free — it is ~150ms, three
                // quarters of what the preflight costs, and since the preflight
                // only runs on first entry this read is the entire cost of
                // every entry after that. It buys not overwriting a change made
                // outside the app, which is worth more.
                async let preflight: Void = settingsStore.checks.isEmpty
                    ? settingsStore.runChecks() : ()
                async let settings = settingsStore.loadSettings()
                // Awaited in *separate* do/catch blocks, not as one tuple.
                // `try await (preflight, settings)` propagates the first
                // failure out of the scope, which cancels the sibling still in
                // flight — and `--check` is the one that fails on a config too
                // broken to load, which is exactly when the settings read
                // matters most. That combination put "Couldn't read your
                // settings." on the pane built to repair that config, with the
                // read canceled rather than failed. Catching each on its own
                // keeps them as independent as they were when sequential.
                do { try await preflight }
                catch { report(error) { settingsStore.lastError = $0 } }
                do { _ = try await settings }
                catch { report(error) { settingsStore.lastError = $0 } }
            }
        }
        // This covers launch *and* every later change to the app list, which
        // is why `.task` above no longer starts a run of its own: the first
        // `loadApps()` moves `store.apps` from `[]` to the real list, and that
        // is a genuine value change this fires on.
        //
        // Adding an app is the plain later case: the row appears, `SidebarView`
        // selects it, and both the row and the detail header would draw the
        // lettered tile until the app was next relaunched. A finished build is
        // the subtler one — it can replace the cached IPA, and rung one reads
        // the icon out of that IPA, so the correct icon can change without the
        // app list changing at all (hence the second `.onChange` below).
        //
        // Cheap enough to do on all of them. A warm `--artwork` is ~0.7ms per
        // app (it compares size and mtime rather than hashing), so a run where
        // nothing changed costs a subprocess and no I/O; only an app whose IPA
        // actually moved gets re-resolved.
        //
        // Detached rather than awaited: icons are decoration on a list that is
        // already correct without them, and a cold `--artwork` run can spend a
        // network round trip per app. Awaited, that delay would be the first
        // thing the user sees on every launch.
        //
        // Keyed on the bundle ids rather than on `store.apps` itself, so an
        // unrelated field changing on a record — a delivery state refresh, an
        // options edit — doesn't spawn a subprocess. Bundle id and not name,
        // because that is what artwork is actually keyed by, both in the cache
        // on disk and in `image(for:)`: renaming an app in config.yaml changes
        // no icon, and repointing one at a different bundle id changes every
        // icon it draws.
        .onChange(of: store.apps.map(\.bundleId)) { _, _ in
            Task { await artworkStore.load() }
        }
        // Fires when a build *ends* (the store clears `buildingApp`), not when
        // one starts — mid-build the IPA is exactly what it was.
        .onChange(of: store.buildingApp) { previous, current in
            guard previous != nil, current == nil else { return }
            Task { await artworkStore.load() }
        }
        // A drop or App Store get staged a new IPA for one app. That file is
        // rung one of the artwork cascade (`icon_from_ipa`), and neither
        // trigger above can see it: the app list is untouched and no build ran.
        // Watched here rather than in the app pane so it fires even when that
        // app's pane is no longer the one open — a get takes minutes.
        //
        // `app:` takes the *config* name — see `ArtworkStore.load`, where the
        // bundle id is a silent no-op — and `refresh: true` because the input
        // genuinely moved.
        .onChange(of: appStoreStore.lastCompleted) { _, completed in
            guard let completed else { return }
            Task { await artworkStore.load(app: completed.app, refresh: true) }
        }
        // A get held an app at a chosen version, or Get latest let go. The
        // hold is the app's `preferred_version` in config.yaml, which the
        // pane's subtitle shows and its cached-IPA card resolves by, so the
        // list is read again; the pane re-reads its card on the new pin.
        .onChange(of: appStoreStore.lastHoldChange) { _, changed in
            guard changed != nil else { return }
            Task { try? await store.reload() }
        }
        // Settings' `--check` reported a different account from the one the
        // app list was read under: config.yaml changed account while the app
        // was open (`graft --init` in Terminal). Re-read the list, which
        // carries the account every pane gates on, and the delivery of every
        // app without a `delivery:` key, which follows the account.
        .onChange(of: settingsStore.account) { _, reported in
            guard let reported, reported != store.account else { return }
            Task { try? await store.reload() }
        }
        // Check for Updates…'s answer, as it stood once any download the
        // check started had started (`UpdateStore.answerAlert`): it says what
        // the banner says, and never offers Update for a download under way.
        // The daily check never sets one. Its buttons are the banner's
        // (`UpdateStore.perform`): Update and Try Again are `update()`, as
        // the banner's Update is, and Restart to Update is
        // `restartToUpdate()`, as the banner's is.
        .alert(
            updates?.answerAlert?.title ?? "",
            isPresented: Binding(
                get: { updates?.answerAlert != nil },
                set: { if !$0 { updates?.dismissAnswer() } }
            ),
            presenting: updates?.answerAlert
        ) { alert in
            if let action = alert.action {
                Button(action.title) { updates?.perform(action) }
            }
            if let release = alert.release {
                Button("Open Release Page") { updates?.openReleasePage(release) }
            }
            Button(alert.cancel, role: .cancel) {}
        } message: { alert in
            Text(alert.message)
        }
        // The daily check: now, then hourly while the app is open, each a
        // no-op until a day has passed since the last success
        // (`UpdateStore.isDue`). Not keyed on `controlActiveState` like the
        // device poll: "while the app is open" includes behind other windows,
        // and a wake with nothing to do starts no subprocess.
        .task {
            guard let updates else { return }
            while !Task.isCancelled {
                await updates.checkIfDue()
                try? await Task.sleep(for: UpdateStore.pollInterval)
            }
        }
    }

    /// The setup screen, or the panes with the tools notice under them.
    @ViewBuilder
    private var detailContent: some View {
        // Over every pane, not only the app pane: each of them runs the
        // same CLI against the same missing config, and each would put up
        // its own alert about it. The sidebar stays — empty, but usable.
        if let need = Self.setupScreen(need: store.setupNeed, holding: accountSetupStore.holdsSetupScreen) {
            SetupView(
                need: need, isChecking: store.isCheckingSetup,
                onCheckAgain: { Task { await checkSetup(.checkAgain) } },
                checkAgainNote: store.shellAskedTooRecently ? ShellEnvironment.askedTooRecentlyNote : nil,
                onContinueWithoutTools: { store.continueWithoutTools() },
                accountSetup: accountSetupStore,
                onAccountSetupFinished: {
                    Task {
                        // A check already in flight (a window return
                        // starts one) would make this one return at once,
                        // and finish() would put Welcome back up until it
                        // landed. Wait it out, then check.
                        while store.isCheckingSetup {
                            try? await Task.sleep(for: .milliseconds(50))
                        }
                        await checkSetup(.accountSetupFinished)
                        accountSetupStore.finish()
                    }
                }
            )
        } else {
            sectionContent
                // Non-modal and below the pane, not over it: a missing
                // tool is worth knowing before a build fails on it, and
                // not worth interrupting anything else for.
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    if let missing = Self.toolsNotice(
                        missing: settingsStore.missingTools,
                        dismissed: dismissedToolsNotice,
                        showingSettings: selection == .section(.settings)
                    ) {
                        MissingToolsNotice(
                            count: missing.count,
                            onShow: { selection = .section(.settings) },
                            onDismiss: { dismissedToolsNotice = missing }
                        )
                    }
                }
        }
    }

    /// What started a setup check.
    enum SetupCheck: Equatable {
        case launch
        /// The setup screen's Check again, pressed.
        case checkAgain
        /// The window coming back while the setup screen is up.
        case windowReturn
        /// A free or paid account setup finished on the setup screen.
        case accountSetupFinished

        /// Whether it asks the login shell for its environment again
        /// (`ConfigStore.checkSetup(refreshingEnvironment:)`): only Check
        /// again. A return to the window used to as well, and a startup file
        /// that runs a Command Line Tools stub put up macOS's install dialog
        /// again each time it was dismissed, since dismissing it is a return
        /// (whole-diff review, app-1).
        var asksShellAgain: Bool { self == .checkAgain }
    }

    /// The first load of the app list — and the setup screen's Check again,
    /// and the re-check on coming back to the window.
    ///
    /// `ConfigStore.checkSetup` decides between the setup screen and the
    /// alert; what is left here is what only this view owns.
    private func checkSetup(_ trigger: SetupCheck) async {
        let loaded = await store.checkSetup(
            refreshingEnvironment: trigger.asksShellAgain,
            toolChecks: {
                // The same scan, and the same store, Settings shows: one
                // `--check` answers the setup step, the notice and the pane.
                do { try await settingsStore.runChecks() } catch { return nil }
                return settingsStore.checks
            }
        )
        guard let apps = loaded else { return }
        didLoadApps = true
        // The window's notice, for a user who did not come through the setup
        // screen: one scan per launch, in the background of a list that is
        // already showing. Not when the setup step just ran one.
        if !settingsStore.hasChecked {
            Task { try? await settingsStore.runChecks() }
        }
        // Artwork is deliberately *not* kicked off here. The line that
        // used to do it ran alongside the `.onChange` below, which the
        // very same `loadApps()` also fires — `store.apps` genuinely
        // moves from `[]` to the real list, and `initial: false`
        // suppresses only the appearance fire, not a real value change.
        // Two subprocesses per launch, ~150ms of duplicated work and
        // two NSCache repopulations, for one set of icons.
        //
        // Open on an app rather than on a placeholder. There is no
        // "Apps" row to land on any more — the apps *are* the top of
        // the sidebar — so with no selection the first thing the user
        // sees would be an empty pane beside a list of them.
        //
        // Only when nothing is selected yet: this task also re-runs on
        // a new window, and stealing an in-progress selection back to
        // the first app would be a navigation the user didn't make.
        if selection == nil, let first = apps.first {
            selection = .app(first.id)
        }
    }

    /// After Settings › Account changed the account. Re-read the list, which
    /// carries the account every pane gates on; the settings, where the signing
    /// fields unlock or lock; and the checks, since a paid account has .p12 rows
    /// a free one doesn't. Quiet, like `refreshOnReturn`: the change itself
    /// landed, and a failed re-read is staleness the next visit fixes.
    static func afterAccountChange(config: ConfigStore, settings: SettingsStore) async {
        try? await config.reload()
        _ = try? await settings.loadSettings()
        try? await settings.runChecks()
    }

    /// What the detail area shows instead of the panes, if anything: the setup
    /// need, or the account setup's own result after the load that cleared the
    /// need, until the user has read it and pressed Start using Graft.
    /// Without this, a window return between writing config.yaml and reading
    /// the free Apple ID's steps took the steps away.
    static func setupScreen(need: SetupNeed?, holding: Bool) -> SetupNeed? {
        // A missing tool waits for the result to be read: it is the next
        // step, and showing it now took the free device steps away mid-read.
        if holding, case .tools = need { return SetupNeed.config(path: nil) }
        if let need { return need }
        return holding ? SetupNeed.config(path: nil) : nil
    }

    /// What returning to a set-up window re-reads: the app list, which carries
    /// the account; the preflight, while the window's notice says tools are
    /// missing — so installing one in Terminal takes the notice down — or
    /// when the account the list now reports is not the one the last scan
    /// was for; and the settings, while they say config.yaml does not parse.
    ///
    /// The account case is F2's second half. `graft --init` in Terminal
    /// turning a free config paid, with no tool missing, was picked up by the
    /// list and by nothing else: Settings' Preflight rows went on showing the
    /// free set, the notice never mentioned the paid-only tools, and the
    /// first build failed at preflight. The parse-error case is K4's: the
    /// notice says "fix it in a text editor, then come back to Settings", and
    /// coming back read only the list, so the notice and the frozen editor
    /// stayed up over a config that now parsed until the pane was re-entered.
    ///
    /// Quiet, like the device poll: nobody pressed anything, and `lastError`
    /// is a modal alert. A config broken in Terminal leaves the list as it was
    /// read last, and the next build or Settings visit says what is wrong.
    /// Each is one subprocess (~150–300ms), and the second and third run only
    /// when they have something to clear.
    ///
    /// Static, taking the stores, so a test can drive it without a window.
    static func refreshOnReturn(config: ConfigStore, settings: SettingsStore) async {
        try? await config.reload()
        if settings.parseError != nil {
            _ = try? await settings.loadSettings()
        }
        let accountMoved = settings.account != nil && config.account != nil
            && settings.account != config.account
        if !settings.missingTools.isEmpty || accountMoved, !settings.isBusy {
            try? await settings.runChecks()
        }
    }

    /// Whether a change of `controlActiveState` is the window coming back to
    /// the front: a change *from* `.inactive`.
    ///
    /// Anything but `.inactive` used to count, so `.key` → `.active` — a sheet
    /// or an alert taking key, on the classic reading of the value — and back
    /// was two refreshes per sheet. On this deployment target `.active` is
    /// not produced at all (see `controlActiveState`), so the two rules agree
    /// in practice; this is the one that is right under both readings.
    static func isReturn(from previous: ControlActiveState, to current: ControlActiveState) -> Bool {
        previous == .inactive && current != .inactive
    }

    @ViewBuilder
    private var sectionContent: some View {
        switch selection {
        case let .app(id):
            // Looked up rather than carried in the selection, so the pane
            // always renders the store's current record: an options edit
            // rewrites `store.apps` in place, and a selection holding a stale
            // copy would keep showing the version pin the edit just changed.
            if let app = store.apps.first(where: { $0.id == id }) {
                AppDetailView(
                    app: app, runner: store.runner, store: store,
                    artwork: artworkStore, devices: devicesStore,
                    // The same library the Tweaks section and the Attach sheet
                    // read, not a second one. The pane names its attached rows
                    // from it, and it used to build its own — a duplicate
                    // `--tweaks` per app pane, and a second copy of the list
                    // that could disagree with this one. See
                    // `AppDetailView.tweaksStore`.
                    tweaksStore: tweaksStore,
                    // The same store the Cache pane's Install button uses, for
                    // the same verb: the pane's "Build for this Mac" hands its
                    // finished archive to `--install-to-mac`.
                    staged: stagedIPAStore,
                    // Shared with Settings, which shows the same tools' status.
                    appStore: appStoreStore,
                    // The Edit sheet's LiveContainer picker reads the same
                    // `--lc-status` answers the device pane shows.
                    liveContainer: liveContainerStore,
                    // The Edit sheet's auto-provisioning toggle asks whether
                    // an App Store Connect key is set.
                    settings: settingsStore,
                    // The build-target bar's Change control. `selection` is
                    // owned here, so the pane is handed the navigation rather
                    // than a binding into this view's state — it has no other
                    // business knowing what a `SidebarItem` is.
                    showSettings: { selection = .section(.settings) },
                    // Build options says when PlayCover isn't installed, and
                    // offers Get PlayCover.
                    playCover: playCover
                )
            } else {
                // A removal clears the selection, so this is the narrow window
                // where config.yaml changed under us — edited outside the app,
                // or a reload that dropped it.
                placeholder(text: "That app is no longer in your config.")
            }
        case let .device(udid):
            // Looked up on every draw rather than carried in the selection, for
            // the same reason an app is: a full listing rewrites
            // `devicesStore.devices` wholesale — the poll escalating to one on
            // a set change, or any manual Refresh — and a selection holding a
            // record would keep showing the iOS version, connection type and
            // LiveContainer state the store has since replaced. What a lookup
            // does *not* do is make those fields track the phone between
            // listings; see `SidebarItem.device` for which changes trigger one.
            if let device = devicesStore.devices.first(where: { $0.udid == udid }) {
                DeviceDetailView(
                    device: device,
                    store: devicesStore,
                    deviceInfo: deviceInfoStore,
                    config: store,
                    delivery: deviceDeliveryStore,
                    artwork: artworkStore,
                    deviceApps: deviceAppsStore,
                    staged: stagedIPAStore,
                    liveContainer: liveContainerStore,
                    appStore: appStoreStore,
                    onSelectApp: { selection = .app($0) }
                )
            } else {
                // The phone went away — unplugged, or a Wi-Fi device that
                // dropped a beat. The selection is deliberately *not* cleared
                // when that happens: a network phone falling off one poll and
                // rejoining the next is routine, and moving the user somewhere
                // else would be a navigation they did not make. The sidebar row
                // is already gone, which is the honest signal; this pane says
                // the same thing in words, and both come back on their own.
                placeholder(text: "That device isn't connected right now.")
            }
        case .section(.tweaks):
            TweaksView(store: tweaksStore, config: store, tweaksDir: settingsStore.paths?.tweaksDir)
        case .section(.canister):
            CanisterView(store: canisterStore)
        case .section(.repos):
            ReposView(store: repoStore)
        case .section(.cache):
            // Three adapters, each as close to nothing as it can be. The pane
            // decides which presses ask first and what every sentence says;
            // the store owns the subprocess and the listing a delete
            // invalidates. What is left here is the join between them, kept to
            // one line apiece precisely because it is the part no test in
            // `CachePaneTests` can reach — the same standard
            // `DeviceDetailAddWiringTests` sets for `DeviceDetailView`'s own
            // two inline closures.
            CachePaneView(
                staged: stagedIPAStore,
                onInstallToMac: { await stagedIPAStore.installToMac(filename: $0.filename) },
                // `installedVersion: nil` — no device listing is behind this
                // add. That is not a shortcut: this pane can see staged files
                // for apps no connected phone has installed at all (three of
                // ten, when the design spec measured this cache), so there is
                // frequently no installed version in existence to compare
                // against, and `Prefill.versionLine` omits the comparison
                // rather than inventing one. `installedName` defaults to nil
                // for the same reason — the sheet falls back to the IPA's own
                // plist name, which is the only name this route ever has.
                onAddAsApp: { addingApp = AddAppSheet.Prefill(staged: $0, installedVersion: nil) },
                onDelete: { record, force in
                    await stagedIPAStore.deleteStaged(filename: record.filename, force: force)
                }
            )
            // Presented from here rather than from the pane because the sheet
            // writes config.yaml through `ConfigStore` and then wants the new
            // row selected — and this view owns both. `DeviceDetailView` makes
            // the same split for the same sheet.
            //
            // `onAdded:` is a labeled argument, not a trailing closure; see
            // `DeviceDetailView`'s own call site for why that does not compile
            // the other way.
            .sheet(item: $addingApp) { prefill in
                AddAppSheet(store: store, appStore: appStoreStore, onAdded: { added in
                    // The CLI's settled key, not the name the sheet showed:
                    // `add_app` dedups a collision to `name_2`, and selecting
                    // the locally-derived name would select nothing.
                    selection = .app(added)
                }, prefill: prefill)
            }
        case .section(.builds):
            BuildsView(store: buildsStore, config: store, artwork: artworkStore, staged: stagedIPAStore)
        case .section(.settings):
            SettingsView(
                store: settingsStore, devicesStore: devicesStore, appStore: appStoreStore,
                account: store.account,
                accountSetup: settingsAccountStore,
                onAccountChanged: { await Self.afterAccountChange(config: store, settings: settingsStore) },
                isBuilding: store.buildingApp != nil,
                updates: updates
            )
        case .none:
            // Three ways to be here, and they are not interchangeable: before
            // the first load finishes, after a load that *failed*, and after
            // removing the selected app (`SidebarView.remove` clears the
            // selection deliberately — see its comment).
            //
            // Gated on `didLoadApps`, not on `store.apps.isEmpty`. Those two
            // agree only when the load succeeded. When it didn't — the CLI
            // missing from PATH is a documented failure for this GUI, and an
            // unparseable config.yaml is the other — `apps` is empty because
            // nothing was read, and "No apps yet" then tells a user with a full
            // config that it is empty, and invites them to re-add apps they
            // already have. The error itself arrives separately, as the
            // sidebar's alert.
            placeholder(text: didLoadApps && store.apps.isEmpty
                        ? "No apps yet. Use + in the sidebar to add one."
                        : "Pick an app, a phone, or a section.")
        }
    }

    private func placeholder(text: String) -> some View {
        Text(text)
            .foregroundStyle(Palette.textMuted)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Palette.bg)
    }

    /// The tools the window's notice is about, or nil when it should not
    /// show: none missing, the user closed it on this same list, or Settings
    /// — which lists them in full — is the pane on screen.
    static func toolsNotice(
        missing: [MissingTool], dismissed: [MissingTool]?, showingSettings: Bool
    ) -> [MissingTool]? {
        guard !missing.isEmpty, !showingSettings, missing != dismissed else { return nil }
        return missing
    }
}

/// Whether the window's return refresh may run now: not while one is
/// running, and not within two seconds of the last one finishing.
///
/// Nothing coalesced these, so focus flicking away and back — a sheet, an
/// alert, a click into another app and straight back — was one `--list` per
/// flick, plus a `--check` per flick while tools were missing. Two seconds is
/// shorter than anyone takes to switch to Terminal, run a command and come
/// back, which is the change a return refresh exists to notice.
struct ReturnRefreshGate {
    static let quietPeriod: Duration = .seconds(2)
    private var running = false
    private var finishedAt: ContinuousClock.Instant?

    /// Claim the refresh, or refuse it. A claim must be ended with `end`.
    mutating func begin(at now: ContinuousClock.Instant = .now) -> Bool {
        guard !running else { return false }
        if let finishedAt, now - finishedAt < Self.quietPeriod { return false }
        running = true
        return true
    }

    mutating func end(at now: ContinuousClock.Instant = .now) {
        running = false
        finishedAt = now
    }
}

/// "2 tools missing — see Settings": the main window's word that `--check`
/// found tools a build needs, before a build fails for want of one.
///
/// A strip under the pane, in the window's own colors with a warning mark:
/// not the red `ErrorBanner`, because nothing has failed yet, and not an
/// alert, because nothing needs answering.
struct MissingToolsNotice: View {
    let count: Int
    let onShow: () -> Void
    let onDismiss: () -> Void

    static func text(count: Int) -> String {
        "\(pluralized(count, "tool")) missing — see Settings"
    }

    var body: some View {
        HStack(spacing: Metrics.controlGap) {
            Symbols.attention.image(size: 12)
                .foregroundStyle(Palette.warn)
                .accessibilityHidden(true)
            Text(Self.text(count: count))
                .font(AppFont.body)
                .foregroundStyle(Palette.text)
            Spacer(minLength: Metrics.controlGap)
            Button("Open Settings", action: onShow)
                .buttonStyle(.borderless)
                .font(AppFont.body)
                .foregroundStyle(Palette.accent)
            Button(action: onDismiss) {
                Symbols.dismiss.image(size: 10)
                    .foregroundStyle(Palette.textMuted)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Dismiss")
            .help("Hide until a different tool goes missing")
        }
        .padding(.horizontal, Metrics.paneGutter)
        .padding(.vertical, Metrics.innerPadding)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.surface)
        .overlay(alignment: .top) {
            Rectangle().fill(Palette.line).frame(height: 1)
        }
        .accessibilityElement(children: .contain)
    }
}

/// "Graft 2.0.1 is available": the main window's word that a newer Graft is
/// out, and where updating to it is (`UpdateStore.notice`).
///
/// `MissingToolsNotice`'s strip, with the accent rather than a warning:
/// nothing is wrong, except when updating failed, which says why and links
/// the release page. No dismiss button: Settings › Updates turns the check,
/// and the offer with it, off.
struct UpdateNotice: View {
    let notice: UpdateStore.Notice
    let onUpdate: () -> Void
    let onRestart: () -> Void
    let onOpenPage: () -> Void

    static let buttonTitle = "Release Page"
    static let updateTitle = "Update"
    static let restartTitle = "Restart to Update"

    static func text(version: String) -> String {
        "Graft \(version) is available"
    }

    /// The strip's line for `notice`.
    static func text(_ notice: UpdateStore.Notice) -> String {
        let version = notice.version
        switch notice.state {
        case .available:
            return text(version: version)
        case let .downloading(fraction?):
            return "Downloading Graft \(version)… \(Int((fraction * 100).rounded(.down)))%"
        case .downloading(nil):
            return "Downloading Graft \(version)…"
        case .verifying:
            return "Checking Graft \(version)…"
        case .ready(installsOnQuit: true):
            return "Graft \(version) is ready. It installs when you quit Graft."
        case .ready(installsOnQuit: false):
            return "Graft \(version) is ready to install"
        case .installing:
            return "Installing Graft \(version)…"
        case .failed:
            return "Graft couldn't update to \(version)"
        }
    }

    var body: some View {
        HStack(spacing: Metrics.controlGap) {
            symbol
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(Self.text(notice))
                    .font(AppFont.body)
                    .foregroundStyle(Palette.text)
                if case let .failed(why, _) = notice.state {
                    Text(why)
                        .font(AppFont.body)
                        .foregroundStyle(Palette.textMuted)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
            }
            Spacer(minLength: Metrics.controlGap)
            buttons
        }
        .padding(.horizontal, Metrics.paneGutter)
        .padding(.vertical, Metrics.innerPadding)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.surface)
        .overlay(alignment: .top) {
            Rectangle().fill(Palette.line).frame(height: 1)
        }
        .accessibilityElement(children: .contain)
    }

    @ViewBuilder
    private var symbol: some View {
        switch notice.state {
        case .downloading, .verifying, .installing:
            ProgressView().controlSize(.small)
        case .failed:
            Symbols.attention.image(size: 12).foregroundStyle(Palette.warn)
        default:
            Symbols.updateAvailable.image(size: 12).foregroundStyle(Palette.accent)
        }
    }

    @ViewBuilder
    private var buttons: some View {
        switch notice.state {
        case let .available(canUpdate):
            if canUpdate {
                Button(Self.updateTitle, action: onUpdate)
                    .buttonStyle(.borderless)
                    .font(AppFont.body)
                    .foregroundStyle(Palette.accent)
                    .help("Download it, check it's Graft from its developer, and restart into it")
            }
            pageButton
        case .ready:
            Button(Self.restartTitle, action: onRestart)
                .buttonStyle(.borderless)
                .font(AppFont.body)
                .foregroundStyle(Palette.accent)
                .help("Quit Graft, put the new version in its place, and open it")
        case .failed:
            pageButton
        case .downloading, .verifying, .installing:
            EmptyView()
        }
    }

    private var pageButton: some View {
        Button(Self.buttonTitle, action: onOpenPage)
            .buttonStyle(.borderless)
            .font(AppFont.body)
            .foregroundStyle(Palette.accent)
            .help("Open this release's page on GitHub, where the new version is")
    }
}
