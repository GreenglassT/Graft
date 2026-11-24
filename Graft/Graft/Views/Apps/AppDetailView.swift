import SwiftUI

private typealias Flag = CLIContract.Flag

struct AppDetailView: View {
    let app: AppRecord
    let runner: CLIRunner
    @Bindable var store: ConfigStore
    /// Owned by `ContentView` and shared with the sidebar, so the icon in this
    /// header is the same file the row that opened it drew — not a second
    /// resolve of the same app.
    let artwork: ArtworkStore
    /// `ContentView`'s polled store, injected rather than built here.
    ///
    /// This view used to own one: `@State private var devicesStore =
    /// DevicesStore(...)`, scanned on first popover open via a guard,
    /// `loadDevicesIfNeeded`. That guard was wrong in both directions at once
    /// — with nothing plugged in, a successful scan left its conditions all
    /// still true, so every reopen re-spawned `--devices`; and once a device
    /// *was* found the guard latched shut forever, because the store survived
    /// app switches as `State(initialValue:)`, so a phone plugged in
    /// afterward could never appear in this pane. The polled store carries no
    /// such guard — it is kept current by `ContentView` on a 5s timer while
    /// the window is key — so injecting it is strictly better, and it also
    /// means this pane and the sidebar can never disagree about what is
    /// connected.
    let devices: DevicesStore
    /// `ContentView`'s tweak library, injected for the same reasons `devices`
    /// is — and it arrived the same way, by first being built here.
    ///
    /// It was a `@State` store of this view's own with a test-only
    /// `TweaksStore? = nil` seam, which is how the pane came to run its own
    /// `--tweaks` beside the one `ContentView` already owns, and how it acquired
    /// a `tweaks.isEmpty` load gate — the exact result-shaped guard commit
    /// `8c6ac07` had removed from `devices` one commit earlier. Sharing deletes
    /// all three: the second subprocess, the seam (this is ordinary production
    /// API now, passed by `ContentView` and by the tests alike), and the
    /// `State(initialValue:)` latch, which would have silently ignored anything
    /// a production caller passed after the first construction.
    ///
    /// It also settles what a shared instance is *worth*, which the previous
    /// arrangement had to write off: the Tweaks section reloads this store on
    /// every entry, so a tweak updated there — which renames the .deb — now
    /// reaches these rows. Against a private instance it could not, and the rows
    /// fell back to filenames with nothing to say why.
    ///
    /// Named `tweaksStore` rather than `tweaks`, unlike `devices`: `app.tweaks`
    /// is a list of attached *filenames* out of config.yaml and this is the
    /// library those filenames are joined against, and the two are one line
    /// apart in `attachedTweaks`.
    let tweaksStore: TweaksStore
    /// The store that owns `--install-to-mac`, injected for the same reason
    /// `devices` and `tweaksStore` are: `ContentView` already holds one, and
    /// the Cache pane's Install button is the other caller of the same verb.
    ///
    /// This pane needs it for the second half of "Build for this Mac": the
    /// build produces an archive marked `mac` and the handoff is what puts the
    /// app on this machine. Building the verb's argv here instead would have
    /// been a second place that assembles it, which is the thing
    /// `StagedIPAStore.installArgs` exists to be the only one of.
    let staged: StagedIPAStore
    /// `ContentView`'s App Store store, shared with Settings: the Source IPA
    /// section's version line and **Get latest from App Store** read it. A
    /// get's progress is kept per app on the store, not in this view's
    /// `@State`, because a get takes minutes and this view is reused across
    /// app switches.
    let appStore: AppStoreStore
    /// `ContentView`'s LiveContainer store, for the Edit sheet's LiveContainer
    /// picker (C-LCPIN): it lists the default device's instances from the same
    /// `--lc-status` answers the device pane shows.
    let liveContainer: LiveContainerStore
    /// `ContentView`'s settings, for the Edit sheet's auto-provisioning
    /// toggle, which needs to know whether an App Store Connect key is set.
    let settings: SettingsStore
    /// What the build-target bar's Change control does: in the app, select the
    /// Settings section.
    ///
    /// A closure passed in rather than a `Binding<SidebarItem?>` taken from
    /// here — `ContentView` owns `selection`, and this pane has no other reason
    /// to know the sidebar exists. See `BuildTargetBar.onChange` for why Change
    /// navigates instead of showing the picker inline.
    let showSettings: () -> Void

    @State private var buildState: BuildState = .idle
    @State private var logLines: [LogLine] = []
    @State private var lastBuild: BuildRecord?
    @State private var cachedIPA: CachedIPARecord?
    @State private var ipaError: String?
    @State private var showingAttachSheet: Bool = false
    @State private var tweakError: String?
    @State private var buildOptions = BuildOptions()
    @State private var showingOptions = false
    @State private var showingEditSheet = false
    @State private var deviceCheck: DeviceCheck?
    @State private var pendingCancel: AppRecord?
    /// The app whose unverified decrypt the user is being asked about, while
    /// "Use it anyway…"'s confirmation is up (C-UNVERIFIED).
    @State private var confirmingUnverified: String?

    /// What the last `--reconcile` said about itself, as opposed to what it
    /// changed — which arrives separately, as a re-reported delivery state on
    /// the store. Both halves are needed: a check that reached no device
    /// leaves the states looking exactly like a check that found everything in
    /// order, so without this the button would appear to confirm a delivery it
    /// never looked at.
    private enum DeviceCheck: Equatable {
        case clean
        case warned([String])
    }

    /// Which app the log panel and `buildState` currently describe. Set from
    /// the per-app reset below, read by `handle(event:owner:)` to drop events
    /// from a build belonging to some other app — see the reset's comment.
    @State private var logOwner: String?

    /// Written out rather than left to the synthesised memberwise initializer,
    /// which is what this file already did. The one thing worth noticing here
    /// is what is *absent*: no parameter has a default. `tweaksStore` used to
    /// have one — `= nil`, falling back to a store built on the spot — and a
    /// dependency a view can be constructed without is a dependency it will
    /// quietly hold a second copy of.
    init(
        app: AppRecord,
        runner: CLIRunner,
        store: ConfigStore,
        artwork: ArtworkStore,
        devices: DevicesStore,
        tweaksStore: TweaksStore,
        staged: StagedIPAStore,
        appStore: AppStoreStore,
        liveContainer: LiveContainerStore,
        settings: SettingsStore,
        showSettings: @escaping () -> Void
    ) {
        self.app = app
        self.runner = runner
        self._store = Bindable(wrappedValue: store)
        self.artwork = artwork
        self.devices = devices
        self.tweaksStore = tweaksStore
        self.staged = staged
        self.appStore = appStore
        self.liveContainer = liveContainer
        self.settings = settings
        self.showSettings = showSettings
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            head
                .padding(.horizontal, Metrics.paneGutter).padding(.vertical, Metrics.sectionGap)
            Divider().background(Palette.line)

            // Above the ScrollView, not inside it, so it stays beside the
            // button it describes: Build & Install is pinned in the header, and
            // a destination label that scrolls out of view answers "where is
            // this going" only until you scroll. What that costs is measured in
            // `BuildTargetBar`'s own comment — an unbounded string on a
            // single-row bar escapes as *width*, not as the height this pane's
            // scrolling sections risk — which is why everything on it is
            // `lineLimit(1)`.
            buildTargetBar
            Divider().background(Palette.line)

            ScrollView {
                VStack(alignment: .leading, spacing: Metrics.sectionGap) {
                    metaStrip
                    encryptedBinariesCard
                    lastBuildFailureCard
                    deliverySection
                    ipaSection
                    tweaksSection
                    logSection
                }
                .padding(.horizontal, Metrics.paneGutter).padding(.vertical, Metrics.sectionGap)
            }
        }
        .background(Palette.bg)
        .navigationTitle(app.displayName ?? app.name)
        .task(id: app.id) {
            await refreshLastBuild()
        }
        // Every piece of per-app state resets together on a selection change.
        // ContentView builds us without an `.id(app.id)`, so SwiftUI reuses
        // this view across app switches and *all* @State survives — which we
        // can't fix from here, only compensate for. Missing `buildState` and
        // `logLines` from this list was the actual defect: `buildOptions` was
        // cleared, so the pills describing the running build vanished, while
        // `isBuilding` (derived from the leaked `buildState`) stayed true and
        // froze the pills and the options chevron for an app with no build
        // running at all, its log still filling the pane.
        //
        // Trade-off, stated plainly: a build started for app A and then
        // navigated away from loses its visible log — switching back shows an
        // empty panel, not A's output. That is the lesser evil versus
        // attributing A's log and A's frozen controls to app B. The build
        // itself is deliberately NOT canceled here: it is a real subprocess
        // that will finish signing and installing, and killing it on a stray
        // sidebar click would be worse than losing the transcript. It stays
        // visible where it matters instead — `store.buildingApp` keeps the
        // sidebar's Remove disabled until it finishes (see `runBuild`).
        .task(id: app.id) {
            logOwner = app.id
            ipaError = nil
            tweakError = nil
            buildOptions = BuildOptions()
            buildState = .idle
            logLines = []
            // The note belongs to the app it was run for, so it goes. The
            // *spinner* deliberately does not live here any more: it is
            // `store.checkingApp`, because a view-local flag reset on every
            // selection change told the user a check had finished merely
            // because they clicked away and back — re-enabling the button and
            // letting a second `--reconcile` race the first on log.sqlite.
            deviceCheck = nil
        }
        // Keyed on the version pin as well as the app, because the Source IPA
        // card answers "which IPA will the next build use", and `preferred_version`
        // is exactly what decides that. `app.id` is `app.name`, which an options
        // edit can never change, so a `latest` → `20.1` edit used to leave the
        // card confidently naming the IPA resolved for the old pin — directly
        // under the field that had just invalidated it, and the difference
        // between a cached build and re-downloading 134 MB. One key covers both
        // triggers, so first appearance still fetches exactly once.
        .task(id: "\(app.id)\u{0}\(app.preferredVersion)") {
            cachedIPA = nil  // don't show the old app's / old pin's answer while re-resolving
            await refreshCachedIPA()
            // Same key, for the same reason: `cached_version` is the file the
            // pin selects. After the cache read, so the CLI's two answers about
            // the cache are never from different moments.
            await appStore.checkVersion(app: app.name, context: versionContext)
        }
        // A drop or get for this app staged a new file: the card is now stale.
        // Artwork is `ContentView`'s, which watches every app's completions so
        // an icon refreshes even when its pane is not the one open.
        .onChange(of: appStore.completions[app.name]) { _, _ in
            Task { await refreshCachedIPA() }
        }
        // The tweak library, which is where the attached rows get a name and a
        // version instead of a filename.
        //
        // Loaded here even though the store is `ContentView`'s, because nothing
        // else fills it in time: `ContentView` loads it on entering the Tweaks
        // *section*, and the app the window opens on is selected before the user
        // has been anywhere. A user who never visits Tweaks would see filenames
        // for the whole session and then watch them improve the moment they
        // happened to open the Attach sheet, which loads this same store.
        //
        // Its own `.task` rather than a line added to one above, because
        // neither wants to wait on the other: `refreshLastBuild` is a
        // `--builds` subprocess and this is a `--tweaks` one, and in sequence
        // the names would land after the build history rather than beside it.
        // The pane does not wait for either — every row is already correct with
        // the filename it has always shown, and the metadata arrives on top.
        //
        // **Gated on `hasLoaded`, never on the list being empty.** This view is
        // built without an `.id(app.id)`, so it is reused across app switches
        // and this task re-runs on every sidebar click; the store outlives all
        // of them. `tweaks.isEmpty` cannot tell "not loaded yet" from "the load
        // failed" or "the library really is empty", and both of those are
        // states this app reaches — `--tweaks` off PATH is documented, and a
        // moved `tweaks_dir` under a config.yaml that still lists tweaks is the
        // other. Either one turns a result-shaped guard into a doomed
        // subprocess per navigation, plus a re-raised banner per navigation
        // because the reset task above nils `tweakError` on each app change.
        // That is the defect `8c6ac07` fixed for `devices` in this same file;
        // `TweaksStore.hasLoaded` is the same answer.
        //
        // The gate costs almost nothing in staleness now that the store is
        // shared. Everything that changes what a .deb is *called* lives in the
        // Tweaks section — Update renames the file, Delete removes it — and
        // `ContentView` reloads this store on every entry there; the Attach
        // sheet, the only way to add a row, reloads it on open. What is left is
        // a .deb replaced outside the app between two loads, which shows one
        // stale name until the next visit to either — and which showed a
        // filename before any of this existed.
        .task(id: app.id) {
            // An app with nothing attached has no row to name, so it does not
            // pay for the library: the Attach sheet loads this same store when
            // it opens, which is the only place an app in that state can get a
            // tweak from.
            guard !app.tweaks.isEmpty, !tweaksStore.hasLoaded else { return }
            do { _ = try await tweaksStore.loadTweaks() }
            catch {
                // A canceled load is not a failed one — `ContentView`'s
                // stance, and it holds here for the same reason: leaving the
                // pane cancels this task, `CLIRunner` reports the SIGTERM'd
                // child as an ordinary non-zero exit, and the error that comes
                // back is indistinguishable from a real failure. Clicking
                // another row in the merged sidebar is an ordinary gesture and
                // must not leave a banner behind.
                guard !Task.isCancelled else { return }
                // Said rather than swallowed, because the failure is otherwise
                // invisible: the rows degrade to exactly what they used to
                // show, so the user sees filenames here and full names one
                // click away in the Tweaks section with nothing to explain it.
                tweakError = "Couldn't read your tweak library, so these rows show "
                    + "filenames: \(error.localizedDescription)"
            }
        }
        // Deliberately no `.onDisappear { buildTask?.cancel() }`. It read as
        // tidy-up and was a kill switch: this view is removed from the
        // hierarchy on any sidebar-section change (ContentView.sectionContent
        // switches this whole branch out), so clicking "Devices"
        // during a livecontainer build SIGTERM'd the CLI and SIGKILL'd it two
        // seconds later, mid-`set_file_contents`. That leaves LiveContainer
        // holding a truncated .ipa, skips `_build_one`'s `finally:
        // shutil.rmtree(tmp_root)` so a multi-hundred-MB temp dir leaks, and
        // writes no delivery row — so the app then badges "Never delivered"
        // over a corrupt file sitting on the phone. A navigation gesture is
        // the least deliberate input in the app and that was the most
        // destructive thing it can do, with no confirmation and no undo.
        //
        // The Task it canceled is safe to outlive us. Every write it makes to
        // this view's state goes through the `logOwner == owner` guard, so it
        // repaints nothing; `store.endBuild(appName:)` only releases the slot
        // it claimed; and `store` is a reference the Task legitimately
        // co-owns — it is where an in-flight build is *supposed* to be visible
        // once this view is gone (`SidebarView` reads `buildingApp` to keep
        // Remove disabled). `runBuild`'s tail refreshes the delivery badges on
        // the store unconditionally for the same reason: a build that finishes
        // while the user is in another section must still update them.
        .sheet(isPresented: $showingEditSheet) {
            EditAppSheet(
                app: app, store: store, liveContainer: liveContainer, devices: devices,
                settings: settings
            ) { showingEditSheet = false }
        }
        // Confirmed rather than immediate, because what a cancel costs depends
        // on where the build has got to and the user cannot see that from the
        // button. Shared with the sidebar's copy so the two cannot drift.
        .stopBuildConfirmation(
            presenting: $pendingCancel,
            name: \.name,
            consequence: { store.cancelConsequence }
        ) { store.cancelBuild(appName: $0.name) }
    }

    private var head: some View {
        HStack(spacing: 12) {
            AppIcon(name: app.name, size: 44, image: artwork.image(for: app.bundleId))

            VStack(alignment: .leading, spacing: 2) {
                // One line, like the bundle id below it. The control cluster to
                // the right holds priority, so this column is where a narrow
                // header's shortfall lands — and the widest that cluster ever
                // gets is mid-build, when the button reads "Building — lc-push"
                // and a Stop button appears beside it. Unlimited, "Example+"
                // wrapped to "YouT / ube+" for the whole build and the header
                // grew a line while it ran.
                Text(app.displayName ?? app.name)
                    .tightTitle()
                    .lineLimit(1)
                    .truncationMode(.tail)
                // The header's shock absorber. Everything to the right of the
                // Spacer is now pinned to its intrinsic width, so this is what
                // gives when the window narrows — and a truncated bundle id is
                // the cheapest thing on this row to lose. `.middle` is what
                // makes that true, and `identityLine` orders the three tokens
                // around it.
                Text(Self.identityLine(app))
                    .font(AppFont.mono)
                    .foregroundStyle(Palette.textDim)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }

            Spacer(minLength: Metrics.controlGap)

            // Two sizes on this row, and only two: the primary action at its
            // own size, every secondary control at `.small`. It used to be
            // three — Edit… at 20pt beside a 30pt Build & Install and a 30pt
            // options chevron — with the chevron the odd one out, a secondary
            // affordance drawn at the primary's height.
            //
            // Levelling *up* to 30pt was the other option and is worse in both
            // directions: it flattens the hierarchy on the one row where the
            // app has a clear primary, and the extra width pushes the cluster
            // past the pane at 1000pt, truncating "Edit…" to "E…".
            HStack(spacing: Metrics.controlGap) {
                Button("Edit…") { showingEditSheet = true }
                    .controlSize(.small)

                ForEach(buildOptions.activeOptions, id: \.self) { option in
                    Button {
                        clear(option)
                    } label: {
                        HStack(spacing: 4) {
                            Text(label(for: option)).font(AppFont.monoSmall).lineLimit(1)
                            Symbols.clearOption.image(size: 8)
                        }
                        .padding(.horizontal, 7).padding(.vertical, 3)
                        .background(Palette.warn.opacity(0.18))
                        .clipShape(Capsule())
                        .contentShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(Palette.warn)
                    .help("Clear \(label(for: option))")
                    .disabled(isBuilding)
                    // Lowest priority in the cluster, so the pills are what
                    // give when the header runs out of room. Up to five of
                    // these can be showing at once — and one of them is named
                    // after the device, so its width is a phone's name — which
                    // is enough to overrun the header at any window size. Left
                    // at the cluster's own priority they took that space from
                    // Build & Install, truncating the primary action to "B…"
                    // while a pill sat beside it at full width. Each pill keeps
                    // `lineLimit(1)` and a `.help` with its full label, so what
                    // it loses is recoverable.
                    .layoutPriority(-1)
                }

                // Gated on the *shared* flag, not the local `buildState`: after
                // an app switch `buildState` is reset (see the per-app task) so
                // this button would otherwise happily start a second concurrent
                // build, and there is exactly one `buildingApp` slot and one log
                // panel to hold it.
                // Launched through the store, not a local `Task {}`, so
                // something other than this pane can stop it: the pane is torn
                // down by any sidebar-section change, and a build that wedges
                // after the user clicks away used to be unstoppable short of
                // quitting the app. See `ConfigStore.launchBuild`.
                BuildButton(title: "Build & Install", state: buildState) {
                    // The store claims the slot synchronously and refuses a
                    // second launch, so a double-click cannot start two builds.
                    store.launchBuild(
                        appName: app.name,
                        delivery: buildOptions.effectiveDelivery(for: app.delivery)
                    ) { await runBuild(options: buildOptions) }
                }
                // Gated on the build slot only, not `isBusy`. A device check
                // has no Stop button, no cancellation handle and no timeout,
                // so a phone wedged in a bad USB state would leave
                // `checkingApp` set forever and disable Build & Install for
                // *every* app with no way to clear it short of quitting. The
                // check is short and read-mostly; letting a build start
                // alongside one is the lesser risk, and `beginDeviceCheck`
                // still refuses the reverse order.
                // Nor while this app's source IPA is being replaced: a build
                // started mid-get would read whichever file happened to sit at
                // the canonical name.
                .disabled(store.buildingApp != nil || appStore.isBusy(app.name)
                          || buildRefusal != nil)
                .help(buildButtonHelp)

                if store.buildingApp == app.name {
                    Button("Stop") { pendingCancel = app }
                        .controlSize(.small)
                        .help("Stop this build")
                }

                Button {
                    showingOptions = true
                } label: {
                    Symbols.buildOptions.image(size: 10)
                }
                .controlSize(.small)
                .help("Build options")
                // The glyph's own name is "Go Down", which is what VoiceOver
                // read for this button; `.help` is a tooltip, not a name.
                .accessibilityLabel("Build options")
                .disabled(isBuilding)
                .popover(isPresented: $showingOptions, arrowEdge: .bottom) {
                    buildOptionsPopover
                }
            }
            // Satisfy this cluster before the name/bundle-id column beside it.
            // Without it SwiftUI splits the shortfall between the two, and the
            // half that lands here truncates the primary action to "B…" while
            // the bundle id above still has room to give. Priority rather than
            // `fixedSize()`: fixing the size makes this HStack rigid, which
            // makes the whole detail pane rigid — the pane's outer VStack takes
            // the header's width — and the pane then overflows its column
            // instead of the label overflowing the button.
            .layoutPriority(1)
        }
    }

    /// The header's second line: how this app is delivered, what it is, and
    /// which version is pinned.
    ///
    /// **The delivery mode is here because it had stopped being anywhere on
    /// this pane.** No single change removed it; three did, each defensible on
    /// its own:
    ///
    ///  1. The meta strip's first cell used to be the mode. It became delivery
    ///     *state*, justified — see `metaStrip` — by the build-target bar four
    ///     points above saying the same thing in a sentence.
    ///  2. That was true when `BuildTarget` had four cases. It has eight, and
    ///     only `.device` renders the mode. `.scanning` is the first second of
    ///     every launch and stays indefinitely after a cmd-tab mid-scan;
    ///     `.notDelivering` is every Dry run and every Skip install, and does
    ///     not print the mode even though it carries the resolved `.device`
    ///     answer inside it.
    ///  3. The sidebar chip's `"LC"` became `Marks.liveContainer`, with the
    ///     word hidden underneath for VoiceOver (`TagChip.content`).
    ///
    /// So for a LiveContainer app in seven of the bar's eight states, the word
    /// appeared nowhere on screen — only in the sidebar chip's tooltip, and in
    /// the Edit… sheet's picker. This line is drawn in every one of those
    /// states, because it is part of the header rather than of anything that
    /// depends on what is plugged in.
    ///
    /// `DeliveryMode.displayName` rather than a fourth spelling: it is the one
    /// the two delivery pickers show, and the one the bar's sentence ends with.
    ///
    /// **The mode leads and the bundle id sits in the middle**, which is the
    /// truncation order rather than a reading order. This `Text` is
    /// `.truncationMode(.middle)` — head and tail survive, the middle is what
    /// goes — and the bundle id is the token this row's own comment calls the
    /// cheapest thing to lose. Trailing the mode instead ("id · version ·
    /// mode") costs two tokens rather than one at the same width: the head
    /// keeps the start of the id and the tail keeps the mode, so the version
    /// disappears *and* the id is cut anyway.
    ///
    /// A static function taking the record, for the reason `deliveryStateValue`
    /// is one: a test can read what this line says without hosting the pane.
    static func identityLine(_ app: AppRecord) -> String {
        "\(app.delivery.displayName) · \(app.bundleId) · \(app.preferredVersion)"
    }

    /// Where Build & Install would send this app, resolved from the polled
    /// device list on every draw.
    ///
    /// Read straight off `devices` rather than cached in `@State`: the store is
    /// `ContentView`'s, kept current on a 5s timer while the window is key, so
    /// a phone plugged in or unplugged moves this line within a tick. A cached
    /// copy would be exactly the stale one-shot answer this pane was just
    /// rescued from — see `devices`'s doc comment.
    ///
    /// Everything passed here is a fact this view already holds, handed over
    /// undigested. The bar's own resolver makes every decision about what they
    /// mean together, so its tests can see all of them.
    ///
    /// `buildOptions.deviceUDID` is passed in because it is what the build
    /// actually does: it goes onto the invocation as `--device <udid>` (see
    /// `BuildOptions.Option.cliArgs`), and `cli.py` resolves the target as
    /// `select_device(device or cfg.default_device_udid)` — the picker
    /// *replaces* the default. Without it this bar named the default phone
    /// while the header showed a `device: <other phone>` pill forty points
    /// away, and one of the two was lying on every build with the picker set.
    ///
    /// `deviceErrors` and `hasLoaded` are the two facts that decide whether an
    /// empty `devices` array means anything. Both are already read elsewhere in
    /// this app for exactly that purpose — the popover below shows the first,
    /// `SidebarView` gates its empty state on the pair — and the bar was the
    /// one surface that took `devices.isEmpty` at face value.
    ///
    /// `notDelivering` is `buildOptions.deliverySuppressor`, the typed half of
    /// the same question `launchBuild` asks with these options a few hundred
    /// lines down via `effectiveDelivery(for:)`. Reading `app.delivery` raw here
    /// is what made this bar promise an install for a run with Dry run ticked.
    ///
    /// The *option* rather than the reason string that call's answer carries,
    /// and that is the difference between two sentences on this bar: Dry run and
    /// Install skipped suppress delivery identically and treat the phone
    /// completely differently, and a `String?` cannot tell the resolver which
    /// one it is holding. See `BuildOptions.Option.stillResolvesDevice`.
    private var buildTargetBar: some View {
        BuildTargetBar(
            target: BuildTargetBar.target(
                devices: devices.devices,
                delivery: app.delivery,
                overrideUDID: buildOptions.deviceUDID,
                deviceErrors: devices.deviceErrors,
                hasLoaded: devices.hasLoaded,
                notDelivering: buildOptions.deliverySuppressor,
                account: store.account
            ),
            onChange: showSettings
        )
    }

    private var buildButtonHelp: String {
        buildRefusal ?? store.busy?.explanation ?? "Build and install \(app.name)"
    }

    /// Why no build of this app can run on this account, or nil when one can.
    ///
    /// A `delivery: sideload` app left in a config for a free Apple ID —
    /// added while the account was paid, or by hand. The CLI refuses every
    /// build of it (`PaidAccountRequiredError`), so Build & Install and Build
    /// for this Mac are disabled with this as their tooltip, and the build
    /// target bar beneath says the same, rather than offering a press that
    /// can only fail. The way out is Edit…, whose picker stays live for it.
    private var buildRefusal: String? {
        Self.buildRefusal(delivery: app.delivery, account: store.account)
    }

    static func buildRefusal(delivery: DeliveryMode, account: AccountMode?) -> String? {
        account.isFree && delivery == .sideload ? PaidOnly.sideloadApp : nil
    }

    /// Pills and the options chevron freeze while a build runs, so the header
    /// never describes a build other than the one in flight — clearing a pill
    /// mid-build would otherwise make a running --dry-run look like a normal
    /// build to anyone glancing at the header.
    private var isBuilding: Bool {
        if case .running = buildState { return true }
        return false
    }

    private var buildOptionsPopover: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Build options")
                .labelCaps()

            Toggle("Dry run", isOn: $buildOptions.dryRun)
                .help("Show what the build would run and where it would send the "
                      + "IPA, without doing any of it")
            // Not "build and sign": a LiveContainer build signs nothing —
            // LiveContainer signs the app when it is imported.
            Toggle("Skip install", isOn: $buildOptions.skipInstall)
                .help("Build the IPA only — don't send it to the device")
            Toggle("Skip decryption check", isOn: $buildOptions.skipDecryptCheck)
                .help("Skip the cryptid-0 verification. Only if the check is "
                      + "wrong about a known-decrypted IPA — injecting into an "
                      + "encrypted binary builds an app that fails on-device.")

            Divider().background(Palette.line)

            Picker("Device", selection: $buildOptions.deviceUDID) {
                Text("Auto").tag(String?.none)
                ForEach(devices.devices) { device in
                    Text(device.name).tag(String?.some(device.udid))
                }
            }
            .pickerStyle(.menu)

            if devices.isLoading {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text(DevicesStore.scanningLine)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textDim)
                }
            } else if let deviceError = devices.lastError {
                VStack(alignment: .leading, spacing: 4) {
                    Text(deviceError)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.danger)
                        .textSelection(.enabled)
                    refreshDevicesButton(title: "Retry")
                }
            } else {
                VStack(alignment: .leading, spacing: 4) {
                    // The refresh affordance has to exist here, not just in
                    // the error branch: "no devices" is the state you're in
                    // when you open the popover, notice the phone isn't
                    // plugged in, plug it in, and look again. The poll picks
                    // it up on its own within five seconds, but this button is
                    // what makes it not feel like waiting.
                    HStack(spacing: 8) {
                        Text(devices.devices.isEmpty
                             ? "No devices connected."
                             : "\(devices.devices.count) connected")
                            .font(AppFont.monoSmall)
                            .foregroundStyle(Palette.textDim)
                        Spacer(minLength: 0)
                        refreshDevicesButton(title: "Refresh")
                    }
                    // A phone usbmux enumerated but couldn't describe is
                    // missing from the picker above with no other trace: the
                    // scan exits 0, so `lastError` is nil and the branch above
                    // reads "No devices connected" about a plugged-in phone.
                    // The Devices tab carries the detail; this says enough to
                    // stop the user hunting for a cable.
                    //
                    // **The same words the build-target bar uses**, which is
                    // one gesture away on this same pane: "N connected phones
                    // couldn't be read." The two said the same thing in two
                    // vocabularies — this line's own, and the bar's — and they
                    // are read seconds apart. Same `pluralized` helper too,
                    // which is what the hand-written singular branch here was.
                    //
                    // **"more" is the one word that stays, and it carries the
                    // difference in what is being counted.** The bar reaches
                    // its sentence only with `devices` empty, so its N is the
                    // whole bench; this line sits under a count of the phones
                    // that *did* answer, so its N is additive to that. Dropping
                    // "more" would put "2 connected phones couldn't be read"
                    // directly under "3 connected" with nothing saying whether
                    // three is the total or the readable ones.
                    if !devices.deviceErrors.isEmpty {
                        Text("\(pluralized(devices.deviceErrors.count, "more connected phone")) "
                             + "couldn't be read — see Devices.")
                            .font(AppFont.monoSmall)
                            .foregroundStyle(Palette.warn)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }

            HStack {
                Spacer()
                Button("Reset") { buildOptions = BuildOptions() }
                    .controlSize(.small)
                    .disabled(buildOptions.isDefault)
            }

            Divider().background(Palette.line)

            // Below everything above and behind a rule of its own, because it
            // is not one of them. The options above *modify* the build the
            // header's primary action starts; this **is** an action, and the
            // artifact it produces is one only this Mac can run — the tweak's
            // dylib is retargeted at a bundled stub Preferences.framework that
            // iOS has for real.
            //
            // The two shapes it is deliberately not:
            //
            //  - a fourth control on the header row. See `head`'s own comment,
            //    "Two sizes on this row, and only two", and the width it
            //    records: the extra control pushes the cluster past the pane
            //    at 1000pt and truncates "Edit…" to "E…".
            //  - a fifth checkbox up there. A checkbox is a modifier the user
            //    can leave ticked, and the next Build & Install would then
            //    quietly build for the wrong machine — with `--skip-install`
            //    implied, so nothing would reach the phone and nothing would
            //    say why. `startMacBuild` sets the flag on a copy for one
            //    launch, which is what keeps that impossible.
            VStack(alignment: .leading, spacing: 6) {
                Text("This Mac")
                    .labelCaps()
                Button("Build for this Mac") { startMacBuild() }
                    .controlSize(.small)
                    // The same gate as Build & Install, and for the same
                    // reason: there is one build slot. Not `isBuilding`, which
                    // is this pane's own state and says nothing about a build
                    // running for another app. Also not while this app's
                    // source IPA is being replaced, as Build & Install.
                    .disabled(store.buildingApp != nil || appStore.isBusy(app.name)
                              || buildRefusal != nil)
                    // A Mac build of a sideload app is refused too: the
                    // refusal is the build's first step, whatever its target.
                    .help(buildRefusal ?? ("Build with the stub framework PlayCover needs, then "
                          + "hand it to PlayCover to install here. Nothing is "
                          + "sent to the phone, and the artifact is not one a "
                          + "phone can use."))
            }
        }
        .font(AppFont.body)
        .toggleStyle(.switch)
        // `popoverGutter`, not `paneGutter`. This is padded *before* the 300pt
        // frame, so a pane-sized inset takes the usable width down to 260 —
        // enough to give the device-error warning an extra line and truncate
        // the device Picker's label earlier. A 300pt popover is not a pane.
        .padding(Metrics.popoverGutter)
        .frame(width: 300)
    }

    /// The staged line to show, or nil once a build has started since it
    /// landed — whichever button started it. See `ConfigStore.buildStartedAt`.
    static func stagedConfirmation(
        _ staged: AppStoreStore.Staged?, buildStartedAt: ContinuousClock.Instant?
    ) -> AppStoreStore.Staged? {
        guard let staged else { return nil }
        if let buildStartedAt, buildStartedAt >= staged.at { return nil }
        return staged
    }

    /// Build this app for this Mac, then hand the result to PlayCover.
    ///
    /// **The flag goes on a copy.** `buildOptions` is the pane's state and
    /// survives until the app selection changes; setting `forPlaycover` there
    /// would turn a one-press action into a mode, and the next Build & Install
    /// would build for the wrong machine with `--skip-install` implied — a
    /// press that reaches no phone and says nothing about why. The copy also
    /// keeps the header's pills honest: they iterate
    /// `buildOptions.activeOptions`, which never contains this.
    ///
    /// Everything else the user set still applies. A Mac build with the
    /// decrypt check skipped is a coherent request; only the target changes.
    ///
    /// Started through `store.launchBuild` like the primary action, not a local
    /// `Task {}`: there is one build slot, one Stop button and one log panel,
    /// and a second path to starting a build is a second path that has to
    /// remember all three.
    private func startMacBuild() {
        showingOptions = false
        var options = buildOptions
        options.forPlaycover = true
        store.launchBuild(
            appName: app.name,
            // The options this run will actually carry, not the pane's — which
            // is the difference between "Built for this Mac" and a banner
            // promising a LiveContainer import that is never going to happen.
            delivery: options.effectiveDelivery(for: app.delivery)
        ) { await runBuild(options: options) }
    }

    /// Re-run the full scan directly, the same call the sidebar's own retry
    /// makes on `devices` — there is no "already fetched" guard to clear any
    /// more, because there is no per-popover fetch: the store this reads is
    /// kept current by `ContentView`'s poll regardless of whether this popover
    /// has ever been opened.
    ///
    /// `lastError` is still cleared before the round-trip, not after: `load()`
    /// itself never touches it on a success, only this call site does, and
    /// left standing a stale message from the *previous* attempt would outlive
    /// a Retry that actually worked — the error branch has priority over the
    /// "N connected" branch below, so the popover would keep reporting a
    /// failure the scan had already recovered from.
    private func refreshDevicesButton(title: String) -> some View {
        Button(title) {
            devices.lastError = nil
            Task {
                do {
                    _ = try await devices.load()
                } catch {
                    devices.lastError = error.localizedDescription
                }
            }
        }
        .controlSize(.small)
        .help("Re-scan for connected devices")
    }

    /// Prefer the device's friendly name over the raw UDID when we have it —
    /// the picker two inches away shows the name, so the pill should match.
    private func label(for option: BuildOptions.Option) -> String {
        if case let .device(udid) = option,
           let match = devices.devices.first(where: { $0.udid == udid }) {
            return "device: \(match.name)"
        }
        return option.defaultLabel
    }

    /// Exhaustive over BuildOptions.Option, so adding a sixth option is a
    /// compile error here rather than a pill that silently clears the device.
    ///
    /// `.forPlaycover` draws no pill today and so cannot be pressed: the pills
    /// iterate `buildOptions.activeOptions`, and `startMacBuild` sets that flag
    /// on a copy. The branch is the correct clearing action for the option all
    /// the same, which is the answer this switch asks each case for.
    private func clear(_ option: BuildOptions.Option) {
        switch option {
        case .dryRun: buildOptions.dryRun = false
        case .forPlaycover: buildOptions.forPlaycover = false
        case .skipInstall: buildOptions.skipInstall = false
        case .skipDecryptCheck: buildOptions.skipDecryptCheck = false
        case .device: buildOptions.deviceUDID = nil
        }
    }

    /// Four facts about this app, one glance wide.
    ///
    /// The first cell used to be the delivery *mode* — the `livecontainer` /
    /// `sideload` token out of config.yaml. Delivery *state* — what the sidebar
    /// dot and the amber card below are about — had no cell at all, and it is
    /// the fact that changes between visits, so it took the slot.
    ///
    /// **The reason given for evicting the mode has since stopped being true,
    /// and this doc kept asserting it.** It read: `buildTargetBar`, four points
    /// above this strip, now says the same thing in a sentence with the phone's
    /// name in it ("Installs to iPhone 15 Pro via LiveContainer"), so the cell
    /// was repeating a neighbour. That held while `BuildTarget` had four cases.
    /// It has eight, and only `.device` names the mode — so on any launch
    /// before the first scan lands, and on every build with Dry run or Skip
    /// install ticked, the sentence above says nothing about how this app is
    /// delivered.
    ///
    /// The cell is still delivery state, and the mode is now on the header's
    /// identity line, which is drawn in every bar state — see `identityLine`.
    /// A fifth cell was the other option and is worse: these four are
    /// `frame(maxWidth: .infinity)` and split the row evenly, so a fifth
    /// narrows all of them, and the narrowest cell is already what
    /// `test_the_longest_meta_label_does_not_wrap_at_the_narrowest_cell` pins.
    private var metaStrip: some View {
        HStack(spacing: Metrics.controlGap) {
            MetaCell(
                label: "Delivery state",
                value: Self.deliveryStateValue(store.deliveryStates[app.name])
            )
            MetaCell(label: "Tweaks", value: "\(app.tweaks.count)")
            MetaCell(label: "Profile", value: Self.profileValue(app, account: store.account))
            MetaCell(label: "Last build", value: Self.lastBuildSummary(lastBuild))
        }
    }

    /// The Profile cell's value. A free Apple ID has no provisioning profile
    /// at all (LiveContainer signs the app on import), and a LiveContainer
    /// app on a paid one is never signed by Graft either, so "manual" or
    /// "auto" there would name a setting that does nothing.
    /// An app with its own `profile:` signs with that file whatever
    /// `auto_profile` says, so it reads as that file's name, not "auto".
    static func profileValue(_ app: AppRecord, account: AccountMode?) -> String {
        if account.isFree || app.delivery == .livecontainer { return "not used" }
        if let override = app.profileOverride, !override.isEmpty {
            return (override as NSString).lastPathComponent
        }
        return app.autoProfile ? "auto" : "manual"
    }

    /// The delivery-state cell's value.
    ///
    /// **This is "delivered *anywhere*", not "delivered to the phone the bar
    /// above names."** `ConfigStore.deliveryStates` is `--list`'s unscoped
    /// answer: the CLI compares an inputs digest against its own record of the
    /// last push, and that record names no device. So this cell can read "up to
    /// date" for an app that has never been near the phone in the sentence four
    /// points above it, and that is correct rather than a bug to fix here. The
    /// per-device answer exists — `DeviceDeliveryStore`, scoped by udid — and
    /// belongs to the device pane, which is forbidden from reading this map at
    /// all (`DeviceDeliveryStoreTests` source-scans `Views/Devices/` for the
    /// identifier). The scan does not cover `Views/Apps/`, deliberately: the
    /// unscoped map is the *right* one here, where nothing on screen claims a
    /// phone.
    ///
    /// A static function taking the record rather than a computed property
    /// reading the store, for the same reason `DeviceStatStrip`'s cell values
    /// are: a test can then pin what the cell says without hosting a view, and
    /// a cell quietly reduced to a dash fails something.
    ///
    /// Absent is the em dash the device pane's stat strip uses for a reading it
    /// doesn't have — not "unknown", which is a state the CLI actually reports
    /// and which this cell spells out. Absent means the CLI said nothing at
    /// all; `deliveryState` below words that case in full.
    static func deliveryStateValue(_ state: DeliveryStateRecord?) -> String {
        state?.kind.stateLabel ?? "—"
    }

    /// The Last build cell's value. Static for `deliveryStateValue`'s reason:
    /// a test pins what the tile says without hosting the pane.
    static func lastBuildSummary(_ lastBuild: BuildRecord?, now: Date = .now) -> String {
        guard let lb = lastBuild else { return "—" }
        let date = lb.date ?? .distantPast
        let rel = DateHelpers.relativeToNow(date, now: now)
        return lb.success ? rel : "failed \(rel)"
    }

    /// The build succeeded and produced an app that will not launch.
    ///
    /// First in the column, above even the failed-build card, because it is
    /// the only thing here that contradicts what the rest of the pane says: a
    /// green `Done` in the header, `✓ done in 84.3s` at the foot of the log.
    /// A build that *fails* is already unmistakable; this one isn't, and the
    /// user's next move is to open LiveContainer and import an IPA that
    /// crashes.
    ///
    /// Card vocabulary, and non-dismissible, for the same reason as the two
    /// cards below it: this is a standing property of the artifact, not an
    /// event in this session. An X would make it go away without making the
    /// IPA any more decrypted. Danger dot rather than the delivery card's
    /// amber, because "out of date" still runs and this does not.
    ///
    /// The header's `BuildState` is deliberately left `.ok`. Flipping it to
    /// `.failed` would paint a red "Retry" over a build the CLI reports as a
    /// success — it signs, it installs, `--skip-decrypt-check` exists — and it
    /// would silence `report(unexplained:)`, which bails when the state is
    /// already `.failed` and is the only path that puts a later non-zero
    /// exit's stderr into the log.
    @ViewBuilder
    private var encryptedBinariesCard: some View {
        if let record = store.encryptedBinaries[app.name] {
            HStack(alignment: .top, spacing: 10) {
                Circle().fill(Palette.danger).frame(width: 9, height: 9).padding(.top, 5)
                VStack(alignment: .leading, spacing: 4) {
                    Text(record.summary)
                        .font(AppFont.bodyMedium)
                        .foregroundStyle(Palette.text)
                        .fixedSize(horizontal: false, vertical: true)
                    Text(record.advice)
                        .font(AppFont.body)
                        .foregroundStyle(Palette.textMuted)
                        .fixedSize(horizontal: false, vertical: true)
                    if let paths = record.pathLine {
                        Text(paths)
                            .font(AppFont.monoSmall)
                            .foregroundStyle(Palette.warn)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 0)
            }
            .padding(Metrics.cardPadding)
            .cardStyle()
        }
    }

    /// Why the last build of this app failed — this session's, if there was
    /// one, otherwise whatever the build log remembers.
    ///
    /// The store's copy wins because it covers the failures the log doesn't
    /// have: a CLI that raised before `build-start` writes no row, and so does
    /// a subprocess that never launched. It also survives this view being torn
    /// down and rebuilt, which `refreshLastBuild()` alone does not — a build
    /// finishing after a sidebar-section switch lands in `@State` that has
    /// already been deallocated.
    ///
    /// `beginBuild` clears the store's entry and a `build-end` with `ok: true`
    /// clears it again, so a success can never leave a stale red card behind.
    /// What the failure card should say, and whose failure it is.
    ///
    /// An enum rather than a `String?` because the distinction has to reach the
    /// *title*. Returning a prefixed string left the card rendering its literal
    /// "Last build failed" header above "An earlier build failed: cyan failed…"
    /// — the two halves disagreeing inside one card.
    enum FailureCard: Equatable {
        case none
        case thisBuild(reason: String)
        /// A failure from before the build the user just stopped. A killed CLI
        /// writes no history row, so anything `lastBuild` holds is necessarily
        /// older — worth showing, but not worth attributing to this run.
        case earlierBuild(reason: String)

        var reason: String? {
            switch self {
            case .none: return nil
            case let .thisBuild(reason), let .earlierBuild(reason): return reason
            }
        }

        var title: String {
            switch self {
            case .none, .thisBuild: return "Last build failed"
            case .earlierBuild: return "An earlier build failed"
            }
        }
    }

    private var failureCard: FailureCard {
        if let recorded = store.buildFailures[app.name] { return .thisBuild(reason: recorded) }
        guard let lb = lastBuild, !lb.success else { return .none }
        let reason = lb.error ?? "no error message recorded"
        // Only compare when a stop was actually recorded — `lastStop` is a
        // dictionary read, where parsing the timestamp costs ~26 µs and is
        // discarded in every session where nothing was stopped.
        guard let stopped = store.lastStop(of: app.name) else { return .thisBuild(reason: reason) }
        guard let built = lb.date else { return .earlierBuild(reason: reason) }
        return built < stopped ? .earlierBuild(reason: reason) : .thisBuild(reason: reason)
    }

    /// Deliberately *not* an `ErrorBanner`. Every red banner in the app reports
    /// something that just went wrong, in this session, with an action the user
    /// can retry — and each is dismissible because dismissing it is honest once
    /// it's been read. This is a record of how the last build ended: it is a
    /// property of that build rather than of anything the view did, and an X
    /// that made it go away would be a lie the next build immediately redecides.
    /// The card vocabulary says "state" where the red strip says "event".
    @ViewBuilder
    private var lastBuildFailureCard: some View {
        let card = failureCard
        if let message = card.reason {
            HStack(alignment: .top, spacing: 10) {
                Circle().fill(Palette.danger).frame(width: 9, height: 9).padding(.top, 5)
                VStack(alignment: .leading, spacing: 4) {
                    Text(card.title)
                        .font(AppFont.bodyMedium)
                        .foregroundStyle(Palette.text)
                    Text(message)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.warn)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
                Button("Copy error") {
                    Pasteboard.copy(message)
                }
                .controlSize(.small)
            }
            .padding(Metrics.cardPadding)
            .cardStyle()
        }
    }

    /// What we believe about the phone, and the one control that can test it.
    ///
    /// The state line and the Check-device button have to share a section
    /// because they answer the same question from two sources, and because of
    /// where each is needed: the amber card below renders only when the state
    /// `needsAttention`, so a button living inside it would be missing in
    /// precisely the case the check exists for — a confident "Up to date with
    /// the last delivery" that the device would contradict.
    ///
    /// Titled, like Source IPA and Tweaks, rather than a bare card: this now
    /// holds an action, and every other action in the pane sits under a
    /// section title beside it.
    private var deliverySection: some View {
        VStack(alignment: .leading, spacing: Metrics.titleGap) {
            HStack {
                SectionTitle(text: "Delivery")
                Spacer()
                checkDeviceButton
            }
            deliveryState
            deviceCheckNote
        }
    }

    /// Whether a build right now would ship something other than what was last
    /// delivered. Same card vocabulary as the failure above it, for the same
    /// reason: this is a record read back out of the delivery log rather than
    /// anything that happened in this session, so there is no X on it — the
    /// next refresh decides it again from scratch.
    ///
    /// Both lines are the CLI's own words, deliberately not paraphrased. Even
    /// after a device check they are the CLI's: `--reconcile` re-reports the
    /// state through the same `delivery-state` event, so a demote arrives here
    /// already phrased ("Our record says delivered; the file is not on the
    /// device"), and this view never has to describe a phone it cannot see.
    ///
    /// A wide card rather than a third line in `head`: a stale app's reasons
    /// run past a hundred characters ("Changed since the last delivery. …
    /// rebuilt since delivery; app version changed: 21.16.2 → 21.17.1"), and
    /// in the header's leading VStack that text competes for width with the
    /// Build & Install cluster. Here it has the pane. This is the first thing
    /// under `metaStrip`, so it is on screen without scrolling either way.
    ///
    /// The quiet branch is new, and is not a badge: `DeliveryKind.needsAttention`
    /// is still false for `current` and `unknown`, so the sidebar stays as
    /// silent about them as it was. One dim line in the pane the user has
    /// deliberately opened is what gives the button above it something to
    /// refer to — a bare "Check device" over nothing says neither what is
    /// being checked nor against what.
    @ViewBuilder
    private var deliveryState: some View {
        let state = store.deliveryStates[app.name]
        if let state, state.kind.needsAttention {
            HStack(alignment: .top, spacing: 10) {
                Circle().fill(Palette.warn).frame(width: 9, height: 9).padding(.top, 5)
                VStack(alignment: .leading, spacing: 4) {
                    Text(state.summary)
                        .font(AppFont.bodyMedium)
                        .foregroundStyle(Palette.text)
                        .fixedSize(horizontal: false, vertical: true)
                    if let reasons = state.reasonLine {
                        Text(reasons)
                            .font(AppFont.monoSmall)
                            .foregroundStyle(Palette.warn)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 0)
            }
            .padding(Metrics.cardPadding)
            .cardStyle()
        } else {
            // Absent means the CLI said nothing about this app — an older CLI,
            // or a state it declined to compute. Deliberately not defaulted to
            // a made-up state: see `ConfigStore.deliveryStates`. The sentence
            // is shared with the device pane's rows, which say the same thing
            // about the same absence — see `DeliveryStateRecord.noneReported`.
            //
            // No padding of its own. It used to carry `.horizontal, 10`, which
            // put it 11pt to the right of every other element in this pane —
            // including the amber *card* branch above, which says the same kind
            // of thing about the same app. The line moved sideways depending on
            // what the delivery state happened to be.
            Text(state?.summary ?? DeliveryStateRecord.noneReported)
                .font(AppFont.body)
                .foregroundStyle(Palette.textMuted)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// Ask the phone whether the last delivery is still there.
    ///
    /// Disabled for a sideload rather than hidden, so the explanation has
    /// somewhere to live: the check is a permanent skip there, not an
    /// oversight, and a user who cannot see the control cannot find out why.
    private var checkDeviceButton: some View {
        Button {
            Task { await checkDevice() }
        } label: {
            if store.checkingApp == app.name {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text("Checking…")
                }
            } else {
                Symbols.device.label("Check device")
            }
        }
        .controlSize(.small)
        .disabled(app.delivery != .livecontainer || store.busy != nil)
        .help(checkDeviceHelp)
    }

    private var checkDeviceHelp: String {
        if app.delivery != .livecontainer {
            return "Only LiveContainer apps can be checked. A sideload is installed "
                + "under a bundle id cyan rewrites at build time and nothing records, "
                + "so there is nothing on the phone to look for."
        }
        if let busy = store.busy { return busy.explanation }
        return "Look on the connected device for the file this app last delivered"
    }

    /// What the last check had to say, beyond whatever it changed above.
    ///
    /// The all-clear is worded as weakly as it is on purpose. `--reconcile`
    /// demotes only on evidence, and it returns without a word in three cases:
    /// a delivery row written before the feature (no container or filename to
    /// look for), a row pushed to a *different* phone than the one attached,
    /// and the sideload arm it skips outright. In all three nothing on the
    /// device was consulted, and the run still ends clean — so "the file is
    /// there" or "the device confirms this" would be false exactly when the
    /// user most needs it not to be. "The record still stands" claims only
    /// what this side actually knows: the check raised no objection.
    @ViewBuilder
    private var deviceCheckNote: some View {
        // While the check waits on the phone, what it is waiting for, in the
        // color of information: "tap Trust" is an instruction, not a fault.
        if store.checkingApp == app.name, let live = store.deviceCheckStatus {
            Text(live)
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.textDim)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        } else {
            deviceCheckResult
        }
    }

    @ViewBuilder
    private var deviceCheckResult: some View {
        switch deviceCheck {
        case .none:
            EmptyView()
        case .some(.clean):
            Text("Checked — the record still stands.")
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.textDim)
        case let .some(.warned(lines)):
            VStack(alignment: .leading, spacing: 2) {
                ForEach(lines, id: \.self) { line in
                    Text(line)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.warn)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    /// Run the check, and record what it said about itself.
    ///
    /// The states it re-reports land on the store unconditionally (inside
    /// `reconcile`) because the sidebar's badges are shared and a demote is
    /// worth having whatever pane is open by the time it arrives. Only the
    /// note is guarded by `logOwner` — it is this pane's `@State`, and an
    /// answer about example must not appear under delta.
    @MainActor
    private func checkDevice() async {
        let owner = app.name
        guard store.beginDeviceCheck(appName: owner) else { return }
        defer { store.endDeviceCheck(appName: owner) }
        // No scan to kick off here any more: `devices` is `ContentView`'s
        // polled store, current whether or not the popover below has ever
        // been opened, so resolving a target just reads it.
        // Cleared before the round-trip, not after it. Left standing, the
        // previous run's amber "no device checked: no iOS devices connected"
        // sat under a "Checking…" spinner for the whole of the next check —
        // the screen asserting a failure about a check that was at that moment
        // succeeding, which a user who walks away carries off as the verdict.
        deviceCheck = nil
        do {
            // Resolve a target before asking. With no `--device` the CLI takes
            // the *first* connected phone, and `_reconcile_app` then bails
            // silently on a udid mismatch — by design, since another phone is
            // not evidence — so the run exits 0 with no warnings and the pane
            // prints "the record still stands" about hardware it never
            // consulted. `reconcile`'s own doc names this exact failure.
            let target = buildOptions.deviceUDID.flatMap { $0.isEmpty ? nil : $0 }
                ?? (devices.devices.count == 1 ? devices.devices.first?.udid : nil)
            let outcome = try await store.reconcile(appName: owner, deviceUDID: target)
            guard canApplyCheck(owner) else { return }
            // A clean run that could not have been aimed anywhere is not a
            // clean bill of health.
            var warnings = outcome.warnings
            if warnings.isEmpty && target == nil && devices.devices.count != 1 {
                warnings = ["Couldn't tell which device to check — "
                            + (devices.devices.isEmpty
                               ? "none is connected."
                               : "pick one under Build options.")]
            }
            deviceCheck = warnings.isEmpty ? .clean : .warned(warnings)
        } catch {
            // A non-zero exit, or a subprocess that never launched. Same
            // channel as the CLI's own warnings: this is the check reporting
            // that it couldn't do its job, and the distinction between "the
            // CLI said no device" and "the CLI didn't run" is not one the user
            // acts on differently.
            guard canApplyCheck(owner) else { return }
            deviceCheck = .warned([error.localizedDescription])
        }
    }

    /// Whether a finished check may still write its verdict.
    ///
    /// The pane must still be showing that app — and no build may have started
    /// meanwhile. A check launched before Build & Install returns *after* the
    /// build has begun replacing the very delivery it inspected, and
    /// `runBuildProcess` clearing the note at line one cannot help: the check
    /// simply re-arms it a second later, so "Checked — the record still
    /// stands." reappears under a build that is making it untrue.
    private func canApplyCheck(_ owner: String) -> Bool {
        logOwner == owner && store.buildingApp == nil
    }

    /// What this pane is showing about the app's cache, so a reused App Store
    /// answer is never one about a different pin or a file since deleted.
    private var versionContext: AppStoreStore.VersionContext {
        .init(
            pin: app.preferredVersion,
            cachedVersion: cachedIPA?.exists == true ? cachedIPA?.version : nil
        )
    }

    @ViewBuilder
    private var ipaSection: some View {
        VStack(alignment: .leading, spacing: Metrics.titleGap) {
            SectionTitle(text: "Source IPA")
            // Drops and gets both run on `appStore`, keyed by app, rather than
            // in this view: the view is reused across sidebar clicks and a get
            // takes minutes, so an outcome held here landed on whichever app was
            // open when it finished. Read for `app.name`, they cannot. One slot
            // per app, too — a drop and a get stage the same cache file, so the
            // drop zone reads busy while a get runs and the other way round.
            IPADropZone(
                title: "",
                cached: cachedIPA,
                isWorking: appStore.isBusy(app.name),
                onPick: { url in
                    appStore.startProvide(app: app.name, fileURL: url, config: store)
                },
                workingLabel: appStore.stages[app.name]?.label
            )
            AppStoreSourceRow(
                version: appStore.versions[app.name],
                isGetting: appStore.isGetting(app.name),
                stage: appStore.stages[app.name],
                isBlocked: appStore.activity[app.name] == .providing
                    || store.buildingApp == app.name,
                onGet: { appStore.startGet(app: app.name) },
                onStop: { appStore.stopGet(app: app.name) },
                onRefresh: {
                    Task {
                        await appStore.checkVersion(
                            app: app.name, context: versionContext, force: true
                        )
                    }
                },
                onGetAgain: { appStore.startGet(app: app.name, again: true) },
                license: appStore.licenseNeeded[app.name],
                isClaiming: appStore.isClaiming(app.name),
                isCheckingClaim: appStore.checkingClaim.contains(app.name),
                claimConfirmation: appStore.claimConfirmations[app.name],
                onCheckClaim: { Task { await appStore.checkBeforeClaim(app: app.name) } },
                onClaim: { appStore.startClaim(app: app.name, confirmed: $0) },
                onCancelClaim: { appStore.cancelClaim(app: app.name) },
                onOpenInStore: {
                    if let url = appStore.licenseNeeded[app.name]?.storeURL {
                        NSWorkspace.shared.open(url)
                    } else {
                        MacAppStore.open()
                    }
                },
                appName: app.displayName ?? app.name,
                claimBlockedReason: appStore.claimBlockedReason,
                getBlockedReason: appStore.getBlockedReason
            )
            // What the last get or drop staged, until the next action — a
            // drop that worked used to look exactly like one that did nothing.
            if let staged = Self.stagedConfirmation(
                appStore.staged[app.name], buildStartedAt: store.buildStartedAt[app.name]
            ) {
                Text(staged.message)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.success)
                    .textSelection(.enabled)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                // The CLI's own warning, after "Use it anyway…": the decrypt
                // that was staged was never verified, and that stays said for
                // as long as the confirmation does.
                ForEach(staged.warnings, id: \.self) { warning in
                    Text("warning: \(warning)")
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.warn)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if let ipaError {
                ErrorBanner(message: ipaError, onDismiss: { self.ipaError = nil })
            }
            if let failed = appStore.errors[app.name] {
                ErrorBanner(message: failed, onDismiss: { appStore.dismissError(app: app.name) })
                if appStore.unverifiedRetry[app.name] != nil {
                    Button(UnverifiedDecrypt.offerButton) { confirmingUnverified = app.name }
                        .controlSize(.small)
                        .disabled(appStore.isBusy(app.name) || store.buildingApp == app.name)
                }
            }
        }
        // Asked first: what it stages is a decrypt nothing could check, and
        // the sentence says what that can cost. The value is passed to the
        // button rather than re-read, for the reason `stopBuildConfirmation`
        // gives — dismissing clears the binding.
        .alert(
            UnverifiedDecrypt.confirmTitle,
            isPresented: $confirmingUnverified.isPresent(),
            presenting: confirmingUnverified
        ) { name in
            Button("Cancel", role: .cancel) {}
            Button(UnverifiedDecrypt.confirmButton) {
                appStore.useUnverified(app: name, config: store)
            }
        } message: { _ in
            Text(UnverifiedDecrypt.confirmMessage)
        }
    }

    /// One row of the attached list: the filename `config.yaml` carries, and
    /// whatever the library knows about the file it names.
    ///
    /// `record` is nil in two different situations that render identically and
    /// are both correct: a .deb the CLI could not parse (a truncated download,
    /// a renamed zip), and the second or so before the library load below
    /// lands. Neither is an error state — the filename is what this list showed
    /// for its whole life, and it is still the identity `config.yaml`, the
    /// build log and the CLI all use.
    struct AttachedTweak: Identifiable {
        let filename: String
        let record: TweakRecord?
        /// Whether another row on this pane renders the same headline. Decided
        /// by the join, because it is a property of the *list* and no row can
        /// see the others; see `filenameLine`.
        let sharesHeadline: Bool

        var id: String { filename }

        /// The headline: the tweak's own name where the library has one.
        var displayName: String { record?.displayName ?? filename }

        /// `1.2.3 · Tweaks · someone`, or nil when there is nothing to say.
        var detailLine: String? { record?.detailLine }

        /// Whether the headline above is a filename rather than a name, which
        /// is what decides the face it is set in. A filename in the body face
        /// reads as a badly chosen name; in mono it reads as what it is.
        ///
        /// **This pane is deliberately the only list that does this**, and it
        /// is worth saying so because the divergence is one click wide:
        /// `AttachTweaksSheet` and `TweaksView` both set `displayName` in
        /// `AppFont.bodyMedium` unconditionally, fallback included, so the same
        /// unparseable .deb renders in two faces depending on where you look at
        /// it. Those two lists carry a separate mono filename line under every
        /// row, so their fallback is already legible as a filename from the line
        /// below it; this pane moved the filename off the row (see
        /// `filenameLine`), so the face is the only thing left to say it. The
        /// alternative — a permanent third line here, matching them — is the
        /// arrangement this change was made to get rid of.
        var isUnnamed: Bool { record?.name == nil }

        /// The filename, on screen, for the rows that need it there.
        ///
        /// Normally it is not: it lives in the detach button's `.help`, and for
        /// a named row that is enough, because the headline and version already
        /// identify the tweak and the filename is only wanted when matching this
        /// pane against config.yaml or a build log.
        ///
        /// It stops being enough the moment two attached rows share a headline,
        /// which is not hypothetical — this library carries
        /// `com.example.btweak` at `1.1.8` and `1.1.8-jailed`, and an
        /// app can have both attached. Then the pane shows two identical rows
        /// with two identical-looking ⊖ buttons, and the only thing telling
        /// them apart is a tooltip you have to hover to read, in front of a
        /// control that removes something. The version in `detailLine` does not
        /// always separate them either — same package, same version, different
        /// build.
        ///
        /// Suppressed for an unnamed row, where the headline *is* the filename
        /// and this would print it twice.
        var filenameLine: String? {
            guard sharesHeadline, !isUnnamed else { return nil }
            return filename
        }
    }

    /// Join the app's attached filenames against the tweak library.
    ///
    /// The app's list is the side that is iterated, and that is the whole
    /// design: `config.yaml` decides which rows exist, and the library is a
    /// directory that may hold fifty other .debs — every one of them a row this
    /// app must not grow. A filename with no matching record keeps its row and
    /// falls back to itself rather than disappearing; a library that failed to
    /// load, or a .deb whose control stanza the CLI could not read, must not be
    /// able to make an attached tweak vanish from the app it is attached to.
    ///
    /// A static function taking both lists rather than a computed property
    /// reading `app` and the store, for the same reason `deliveryStateValue`
    /// is one: the join is the part with a rule in it, and this way a test can
    /// pin all three of its answers without hosting a view.
    static func attachedTweaks(
        _ filenames: [String], from library: [TweakRecord]
    ) -> [AttachedTweak] {
        // Indexed rather than searched per row. A `first(where:)` per filename
        // is an n×m scan across a library that grows without bound — it is the
        // whole tweaks directory, not this app's share of it — and this runs on
        // every evaluation of the pane's body, which a build streams dozens of.
        //
        // `uniquingKeysWith` because a duplicate filename is not this view's
        // problem to report: the library is a directory listing, so two records
        // under one name would already be a CLI defect, and crashing the pane
        // — which the `uniqueKeysWithValues` initializer does — is a worse
        // answer than showing the first.
        let byFilename = Dictionary(
            library.map { ($0.filename, $0) }, uniquingKeysWith: { first, _ in first }
        )
        let rows = filenames.map {
            AttachedTweak(filename: $0, record: byFilename[$0], sharesHeadline: false)
        }
        // A second pass over the *rows*, not the library: whether a headline
        // identifies a row is a question about the other rows on this pane, and
        // two builds of one tweak sitting in the library is not a problem until
        // an app attaches both. Iterating twice is free at this size — this list
        // is one app's attached tweaks, not the directory — and reuses
        // `displayName` rather than restating the fallback rule beside it.
        let headlines = Dictionary(rows.map { ($0.displayName, 1) }, uniquingKeysWith: +)
        return rows.map {
            AttachedTweak(
                filename: $0.filename, record: $0.record,
                sharesHeadline: headlines[$0.displayName, default: 0] > 1
            )
        }
    }

    private var tweaksSection: some View {
        VStack(alignment: .leading, spacing: Metrics.titleGap) {
            HStack {
                SectionTitle(text: "Tweaks — \(app.tweaks.count) attached")
                Spacer()
                Button {
                    showingAttachSheet = true
                } label: {
                    Symbols.add.label("Attach…")
                }
                .controlSize(.small)
            }
            if app.tweaks.isEmpty {
                Text("No tweaks attached. Click Attach… to pick from your tweak library.")
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
            } else {
                VStack(spacing: 4) {
                    ForEach(Self.attachedTweaks(app.tweaks, from: tweaksStore.tweaks)) { tweak in
                        HStack(spacing: 10) {
                            // The row's left column was `Text("::")` — two
                            // colons standing in for a mark, in a pane whose
                            // every other list already draws the thing it is
                            // listing. It is the app's tweak glyph now, the
                            // same one the sidebar's Tweaks row uses, so a
                            // tweak looks like a tweak in both places.
                            //
                            // 14pt, which is what the `::` it replaces was set
                            // at and one step up from the first thing tried
                            // here. At 12 in `textDim` the glyph was the
                            // faintest mark on a row whose *identity* it
                            // carries — quieter than the 11pt mono detail line
                            // under the name — and screenshotting it beside
                            // the 13pt semibold headline is the only way that
                            // shows.
                            Symbols.tweak.image(size: 14)
                                .foregroundStyle(Palette.textDim)
                            VStack(alignment: .leading, spacing: 2) {
                                // What the row used to be was
                                // `com.example.sometweak_1.7.20_iphoneos-arm64.deb` — a
                                // string in which the only part the user
                                // chose is the one they can't read. The name
                                // and version come out of the .deb's own
                                // control stanza, which the CLI already sends
                                // and the Attach sheet already renders; this
                                // list is where they were still missing.
                                Text(tweak.displayName)
                                    .font(tweak.isUnnamed ? AppFont.mono : AppFont.bodyMedium)
                                    .foregroundStyle(Palette.text)
                                    // Both lines are pinned to one, and that
                                    // is load-bearing rather than tidy: these
                                    // rows sit inside the pane's `ScrollView`,
                                    // which absorbs their height and passes
                                    // their *width* demand straight out to the
                                    // window. A name or an author is whatever
                                    // the packager typed, and an unwrapped one
                                    // takes the pane past its own minimum
                                    // width — measured at 1590–1651pt in a
                                    // 900pt window when the pane's other
                                    // unbounded strings were checked. What
                                    // gets clipped is the right-hand edge:
                                    // this row's own detach control.
                                    .lineLimit(1)
                                    .truncationMode(.tail)
                                if let detail = tweak.detailLine {
                                    Text(detail)
                                        .font(AppFont.monoSmall)
                                        .foregroundStyle(Palette.textMuted)
                                        .lineLimit(1)
                                        .truncationMode(.tail)
                                }
                                // Only for a row two of these headlines cannot
                                // tell apart — see `AttachedTweak.filenameLine`.
                                // Truncated from the middle, like the sheet's
                                // filename line: what distinguishes two builds
                                // of one tweak is the version in the middle of
                                // the name, not either end.
                                //
                                // This row draws two marks, one per line, and
                                // that is deliberate rather than a leak. The
                                // bullet above is `Symbols.tweak` because the
                                // row *is* a tweak — the same accessor the
                                // sidebar's Tweaks destination uses, and that
                                // pairing is an invariant its doc comment
                                // states. This line is not the tweak: it is the
                                // `.deb` the tweak arrived in, shown only when
                                // two headlines cannot be told apart, which is
                                // precisely a question about *files*. A tweak
                                // ships inside a `.deb`, so the two nouns share
                                // a row honestly.
                                if let filename = tweak.filenameLine {
                                    HStack(spacing: 5) {
                                        Marks.debPackage.image(size: 11)
                                        Text(filename)
                                            .lineLimit(1)
                                            .truncationMode(.middle)
                                    }
                                    .font(AppFont.monoSmall)
                                    .foregroundStyle(Palette.textDim)
                                }
                            }
                            Spacer()
                            Button {
                                Task { await detachTweak(tweak.filename) }
                            } label: {
                                Symbols.detach.image(size: 14)
                                    .foregroundStyle(Palette.textDim)
                            }
                            .buttonStyle(.plain)
                            // Names the file, not the tweak, and deliberately:
                            // for a named row this tooltip is where the
                            // filename went, and it is what the user needs to
                            // match this pane against config.yaml or a build
                            // log. It is *not* asked to carry the case where
                            // two rows are otherwise identical — a tooltip you
                            // have to hover to read is a poor last defense in
                            // front of a destructive control, so that row puts
                            // its filename on screen instead
                            // (`AttachedTweak.filenameLine`).
                            .help("Detach \(tweak.filename)")
                        }
                        .padding(.horizontal, 10).padding(.vertical, 6)
                        .background(Palette.surface.opacity(0.5))
                        .clipShape(RoundedRectangle(cornerRadius: Metrics.radius))
                    }
                }
            }
            if let tweakError {
                ErrorBanner(message: tweakError, onDismiss: { self.tweakError = nil })
            }
        }
        .sheet(isPresented: $showingAttachSheet) {
            AttachTweaksSheet(store: store, app: app, tweaksStore: tweaksStore)
        }
    }

    private var logSection: some View {
        VStack(alignment: .leading, spacing: Metrics.titleGap) {
            SectionTitle(text: "Last build log")
            BuildLogPanel(lines: logLines, emptyMessage: Self.emptyLogMessage(lastBuild: lastBuild))
                .frame(minHeight: 160)
        }
    }

    /// What the log panel says before this session has built the app.
    ///
    /// The panel holds only this session's output, so "No build yet" was true
    /// of the panel and false of the app whenever the CLI had a build on
    /// record — the tile beside it said "21h ago". Which sentence is right
    /// depends on that record: a build run from Terminal without `--json`
    /// saved no log (`log_path` null), and one that did save a log can be
    /// replayed from Builds.
    static func emptyLogMessage(lastBuild: BuildRecord?) -> String {
        guard let lastBuild else { return "No build yet for this app." }
        guard lastBuild.logPath != nil else {
            return "The last build ran from Terminal, so its log wasn't saved. "
                + "Builds started here keep theirs."
        }
        return "Nothing built since Graft opened. The last build's log is in Builds."
    }

    /// - Parameter options: the run's own options, passed rather than read off
    ///   `buildOptions` at the moment they are needed. Build & Install hands
    ///   over the pane's own; `startMacBuild` hands over a copy with
    ///   `forPlaycover` set, which is what keeps the Mac target an action
    ///   instead of a state the pane can be left in.
    @MainActor
    private func runBuild(options: BuildOptions) async {
        // Captured once: `self` is a value copy, so `app` inside the event
        // callback would still name the app that started the build even after
        // the user selects a different one. That's what we want for `owner`,
        // and it's why the "is this still the displayed app" test has to read
        // `logOwner` (@State, shared storage) instead of `app`.
        let owner = app.name
        // The subprocess gets a scope of its own so its `defer` releases
        // `buildingApp` — and with it the Build button and the sidebar's
        // Remove — before the two refreshes below go out. Neither of them is
        // part of the build, and neither should extend how long the app looks
        // like one is running.
        let built = await runBuildProcess(owner: owner, options: options)
        // One read of the build log serving both things that want it: the meta
        // strip, and — for a Mac build — the filename PlayCover is handed. Two
        // `--builds` runs would be two answers about one build, and the second
        // could see a row the first did not.
        //
        // Still skipped when neither wants it, which is what the `logOwner`
        // guard used to say on its own: a build the user navigated away from
        // has no meta strip to fill, and its record would land in the other
        // app's.
        let handingOver = built && options.forPlaycover
        // Read outside this Task when a Stop canceled it: `runOnce` in a
        // canceled Task kills its own child at once, so the read came back
        // nil and "Last build" went from "just now" to "—" over a history the
        // stop had not touched (a stop writes no row). An unstructured Task
        // does not inherit the cancellation.
        let finished: BuildRecord?
        if logOwner == owner || handingOver {
            finished = Task.isCancelled
                ? await Task { await lastBuildRecord(of: owner) }.value
                : await lastBuildRecord(of: owner)
        } else {
            finished = nil
        }
        if logOwner == owner {
            lastBuild = finished
        }
        // Before the delivery refresh below, which is about phones and has
        // nothing to say about this run: the press asked for an app on this
        // Mac, and it should not wait behind a subprocess that cannot affect it.
        if handingOver {
            await handOffToPlayCover(record: finished, owner: owner)
        }
        // Not guarded by `logOwner`, unlike everything above: `deliveryStates`
        // is the store's, read by the sidebar badge and by this pane's warning
        // card for whichever app is showing. A build the user navigated away
        // from is exactly the case that needs it — otherwise the amber
        // "Changed since the last delivery" card outlives the build that made
        // it current, and the user rebuilds to try to clear it.
        //
        // Detached when this Task was canceled, because awaiting it there
        // does nothing at all: `refreshDeliveryStates` checks `Task.isCancelled`
        // and unwinds silently, by design, so a Stop pressed during `lc-push`
        // would leave the badge reading "up to date" over a phone now holding
        // a half-written file. That is the single case where the badge is most
        // wrong and most worth correcting, so it must outlive the cancel that
        // caused it.
        if Task.isCancelled {
            store.refreshDeliveryStatesDetached()
        } else {
            await store.refreshDeliveryStates()
        }
    }

    /// - Returns: whether the CLI finished this build cleanly — not canceled,
    ///   no throw, exit 0. The three `return false` sites below are exactly the
    ///   three that already report a failure, so the answer cannot drift from
    ///   what the pane paints.
    ///
    ///   Only `runBuild`'s PlayCover handoff reads it, and it has to: after a
    ///   cancel or a failure the newest row in the build log belongs to some
    ///   *earlier* run, which for an app built for this Mac yesterday is a
    ///   perfectly good Mac archive — handing that over would install a
    ///   stale app and look like it worked.
    @MainActor
    @discardableResult
    private func runBuildProcess(owner: String, options: BuildOptions) async -> Bool {
        logLines = []
        buildState = .running(stage: "starting")
        // The note describes a check of the *previous* delivery, and this build
        // is about to replace it. Leaving "the record still stands" under a
        // freshly pushed file would attach a device check to a delivery no
        // device has seen.
        deviceCheck = nil
        // `store.launchBuild` already claimed the slot, synchronously, before
        // this Task existed — that is what makes a double-click unable to
        // start a second build. This only has to release it.
        defer { store.endBuild(appName: owner) }
        do {
            // Options MUST precede the positional app name. The CLI is a Click
            // Group, so option parsing stops at the first positional and every
            // later token is swallowed as another app name — a trailing
            // --dry-run silently became an app called "--dry-run" while the
            // real build ran for real, installing to the device.
            let arguments = [Flag.json] + options.cliArgs + [app.name]
            let result = try await runner.stream(arguments: arguments) { event in
                handle(event: event, owner: owner)
            }
            // Ordered on the cancel, not on the exit status. A Stop pressed
            // after the child has already exited 0 — the continuation waits for
            // both drainers to hit EOF, so the slot is still claimed and Stop
            // is still on screen — reaps nothing, so `exitCode` is 0 and the
            // old `if result.exitCode != 0` took neither branch. The queued
            // `.finished` event is meanwhile dropped by `handle`'s canceled
            // guard, so the pane sat painted as a running build forever.
            // A stop the CLI reported counts too (C-STOP), read off the
            // collected events rather than the callback: a stop nobody pressed
            // here — `kill` from Terminal — exits non-zero, and without this it
            // painted a red Retry over a build that only did as it was told.
            // Ahead of the exit status for the same reason as the cancel: a
            // stopped Mac build must never fall through to the PlayCover
            // handoff, which would hand over an *earlier* build's archive.
            if store.wasCancelled(owner) || BuildProgress.stopReported(in: result.events) {
                noteCancelled(result: result, owner: owner)
                return false
            } else if result.exitCode != 0 {
                report(unexplained: result, owner: owner)
                return false
            }
            return true
        } catch {
            // `stream` throws rather than returning only when the Task was
            // already canceled before the child launched — `ProcessHandle`
            // refuses to adopt and resumes with `CancellationError`, so no
            // subprocess is spawned at all. Same reading as the branch above.
            if store.wasCancelled(owner) || error is CancellationError {
                noteCancelled(result: nil, owner: owner)
            } else {
                // Recorded on the store first and unconditionally, for the same
                // reason `handle(event:owner:)` does: this view may no longer
                // exist. `setBuildState` writes to `@State` and is a no-op once
                // the section has been switched away from.
                store.noteBuildFailure(appName: owner, message: error.localizedDescription)
                setBuildState(.failed(message: error.localizedDescription), owner: owner)
            }
            return false
        }
    }

    /// The archive a finished Mac build should hand to PlayCover, or nil.
    ///
    /// Static and pure, for the reason `identityLine` is: every refusal here is
    /// checkable without hosting the pane or spawning a subprocess, and this is
    /// the one place a phone artifact could reach `--install-to-mac`.
    ///
    /// `isForThisMac` is the load-bearing guard and is read off the **build
    /// log**, not off the options the run was started with. Those two can
    /// disagree — a `graft` predating the `target` column, a row written
    /// by a build that raced this one — and the CLI's record is the side that
    /// knows what the artifact actually is. `--install-to-mac` itself does not
    /// check: it hands whatever file it is named to PlayCover, so a phone
    /// build sent through here installs an app whose tweak still wants the
    /// private framework macOS does not have.
    ///
    /// `success` because a failed build's row carries no `ipa_path` at all
    /// (`cli.py` NULLs it), so this would be a nil unwrap away from the same
    /// answer — but stating it makes the guard survive that changing.
    static func handoffFilename(from record: BuildRecord?) -> String? {
        guard let record, record.success, record.isForThisMac else { return nil }
        guard let path = record.ipaPath else { return nil }
        let filename = (path as NSString).lastPathComponent
        return filename.isEmpty ? nil : filename
    }

    /// What handing a finished Mac build to PlayCover did — and the whole of
    /// what that is allowed to contribute to the pane.
    ///
    /// **A failed handoff is not a failed build**, and this type is where that
    /// stops being a rule somebody has to remember. Its only member is
    /// `logLines`: a handoff can add transcript and nothing else, because
    /// `[LogLine]` has no way to express a failed build. `buildState` and
    /// `store.noteBuildFailure` — the only two things that paint one — are out
    /// of reach from here by construction rather than by care.
    ///
    /// That matters because PlayCover crashes on roughly three of eight
    /// installs on any IPA, tweaked or not, and by the time it is asked the
    /// archive has been built, signed, prepared and written to the build log. A
    /// row marked failed is one the user rebuilds for nothing; the retry is
    /// that row's own Install to Mac, against the file already on disk.
    ///
    /// The transcript rather than a banner of its own: it is the channel
    /// `noteCancelled` already uses for news that arrives after a build has
    /// ended, and it sits directly under the lines this continues.
    enum Handoff: Equatable {
        /// The build finished, but the log has no Mac archive to hand over.
        case noArchive
        case handedOver(filename: String)
        /// PlayCover would not take it. `complaint` is the CLI's own sentence,
        /// carried rather than composed — the same rule
        /// `StagedIPAStore.DeleteAnswer.inUse` follows.
        case refused(filename: String, complaint: String)

        var logLines: [LogLine] {
            switch self {
            case .noArchive:
                return [LogLine(kind: .warn, text:
                    "The build finished, but graft recorded no Mac archive to "
                    + "hand over — check the Builds pane.")]
            case let .handedOver(filename):
                // The CLI's own wording for this verb, and it stops short of
                // claiming an install for the reason
                // `StagedIPAStore.installToMac` gives: `open -b` returns as
                // soon as LaunchServices has delivered the event, so there is
                // no finished install for either side to see.
                return [LogLine(kind: .ok, text:
                    "  ✓ handed \(filename) to PlayCover — it reports its own progress")]
            case let .refused(filename, complaint):
                return [
                    LogLine(kind: .warn, text: "  ✗ \(complaint)"),
                    // Names the file, because the sentence's whole job is to
                    // stop a rebuild: the artifact this run made is still
                    // there, and the row that holds it is where the retry is.
                    LogLine(kind: .warn, text:
                        "The build is fine and \(filename) is kept — Install to Mac "
                        + "on its row in Builds is the retry."),
                ]
            }
        }
    }

    /// Hand the archive a Mac build just produced to PlayCover. See `Handoff`.
    ///
    /// **Two things here are not covered by a test, and both were measured
    /// rather than assumed** — each mutated on its own and the whole suite
    /// re-run, 735 passing either way:
    ///
    ///  - `runBuild`'s `built` gate. Drop it and a *canceled* Mac build hands
    ///    over the previous run's archive, which `handoffFilename` accepts
    ///    quite correctly — it is a successful Mac row — so the app that
    ///    appears is yesterday's and nothing says so.
    ///  - the absence of a `setBuildState(.failed:)` here. `Handoff` puts a
    ///    build failure out of reach of the value this returns, not of the
    ///    function that returns it.
    ///
    /// Both need the pane hosted against a real `graft` build to assert,
    /// which is why they are written down instead of covered.
    @MainActor
    private func handOffToPlayCover(record: BuildRecord?, owner: String) async {
        guard let filename = Self.handoffFilename(from: record) else {
            append(.noArchive, owner: owner)
            return
        }
        // Before the await, so the transcript shows the wait rather than
        // stopping at `✓ done` for however long PlayCover takes to answer.
        // Shaped like the CLI's own stage lines because it is one more step in
        // the same list, even though no `stage` event carries it.
        appendLog(.arrow, "→ playcover", owner: owner)
        let complaint = await staged.installToMac(filename: filename)
        append(complaint.map { .refused(filename: filename, complaint: $0) }
               ?? .handedOver(filename: filename), owner: owner)
    }

    @MainActor
    private func append(_ handoff: Handoff, owner: String) {
        guard logOwner == owner else { return }
        logLines += handoff.logLines
    }

    /// Append one line to this pane's transcript, if the pane still shows the
    /// build it belongs to. The same guard every other write to `logLines`
    /// carries, and for the same reason: a line about example must not appear
    /// under delta.
    @MainActor
    private func appendLog(_ kind: LogLine.Kind, _ text: String, owner: String) {
        guard logOwner == owner else { return }
        logLines.append(LogLine(kind: kind, text: text))
    }

    /// Record a build the user stopped, in both places a build's ending lands.
    ///
    /// The store's half is unguarded and the panel's half isn't, the same
    /// split every other completion path here uses: `buildFailures` is read by
    /// whichever pane is on screen later, `logLines` and `buildState` belong to
    /// this one.
    @MainActor
    private func noteCancelled(result: CLIRunner.RunResult?, owner: String) {
        // Read before the store is told, because `noteBuildCancelled` is what
        // ends the build as far as everything else is concerned.
        let stage = store.buildStage
        // Read before the store is told: this is the exposure as of the kill.
        let aftermath = store.deviceExposure.cancelAftermath
        store.noteBuildCancelled(appName: owner)
        guard logOwner == owner else { return }
        logLines += Self.stopTranscript(stage: stage, aftermath: aftermath, result: result)
        buildState = .cancelled
    }

    /// What a stopped build adds to the log, under whatever the stream had
    /// already put there.
    ///
    /// Static and pure, like `handoffFilename`, so the sentence a Stop leaves
    /// is checkable without hosting the pane or running a build.
    ///
    /// - Parameters:
    ///   - stage: the stage the build was in when it was stopped, read before
    ///     the store was told.
    ///   - aftermath: what the stop left on the phone, from the same folded
    ///     value the confirmation used.
    ///   - result: the subprocess's result, or nil when no subprocess ran.
    static func stopTranscript(
        stage: String?, aftermath: String?, result: CLIRunner.RunResult?
    ) -> [LogLine] {
        // Names the stage it actually died at, which the confirmation could
        // not: that sheet was written when it was presented, and the build
        // went on advancing while the user read it. A cancel that says "Stopped
        // during lc-push" is the only version of this the user can act on,
        // because it is the only one taken after the fact.
        var lines = [LogLine(kind: .warn, text: stage.map { "Stopped during \($0)." } ?? "Stopped.")]
        // From the same folded value the confirmation used, so the two cannot
        // disagree. Reading `buildStage` here was the bug: it is nil in exactly
        // the window the warning had just described, so the sentence the user
        // was warned about was the one the log then declined to write.
        if let aftermath {
            lines.append(LogLine(kind: .warn, text: aftermath))
        }
        // The CLI's own last words reach the log when they can explain
        // something. Skipping this whole path on a cancel threw away the
        // explanation for a build that had genuinely failed a second before
        // the kill landed — the phone unplugged, the app gone from config.yaml
        // — leaving an empty pane and no hint. It is rendered as transcript,
        // not as a failure: the header says `.cancelled`, and
        // `report(unexplained:)` is still the only thing that paints a build
        // red.
        //
        // **Not when the CLI reported the stop (C-STOP).** Then it ended
        // where it was told to, and its stderr has no `error:` block in it —
        // under `--json` it is the build's own narration, which the CLI sends
        // there to keep stdout for events. Dumping it put "✗ exit 143" under
        // "Stopped during cyan." and then replayed every stage line the panel
        // had already shown. A build that failed before the kill reports
        // `build-end ok: false`, not `stopped: true`, so it still gets here.
        if let result, result.exitCode != 0, !BuildProgress.stopReported(in: result.events) {
            lines += CLIFailure
                .logText(exitCode: result.exitCode, stderr: result.stderr)
                .map { LogLine(kind: .warn, text: $0) }
        }
        return lines
    }

    /// A non-zero exit the event stream never accounted for.
    ///
    /// The CLI can fail before `build-start` — the app gone from config.yaml,
    /// an unplugged phone — in which case `handle(event:)` never fires and the
    /// whole explanation is on stderr, which this view used to throw away in
    /// favour of "exit N" over an empty log panel.
    ///
    /// The log panel is what actually shows it. `BuildState.failed`'s message
    /// is carried but not rendered here — BuildButton turns any failure into
    /// "Retry" — and a hint runs to two lines anyway, which is a log's shape
    /// and not a button's.
    @MainActor
    private func report(unexplained result: CLIRunner.RunResult, owner: String) {
        // Ahead of the ownership guard, like `handle(event:owner:)`'s call to
        // `store.note(_:forBuildOf:)` and for the same reason: a build that
        // failed while the user was in another section has to leave a trace
        // somewhere that still renders. `note` has already recorded a
        // `build-end` failure if there was one; this covers the case there
        // wasn't — the CLI raising before `build-start`, which is the failure
        // this whole method exists for.
        if store.buildFailures[owner] == nil {
            store.noteBuildFailure(appName: owner, message: result.failureSummary)
        }
        guard logOwner == owner else { return }
        // `build-end` already said what went wrong — its message is the CLI's
        // structured one and beats anything stderr has.
        if case .failed = buildState { return }
        logLines += CLIFailure
            .logText(exitCode: result.exitCode, stderr: result.stderr)
            .map { LogLine(kind: .warn, text: $0) }
        buildState = .failed(message: result.failureSummary)
    }

    /// Guard for every write the build path makes to the shared log/state:
    /// the stream callback keeps firing long after a selection change (and
    /// after a cancel — `CLIRunner.stream` blocks in a continuation that only
    /// resumes on process exit), so an unowned event must be dropped rather
    /// than repainting whichever app the user is now looking at.
    private func setBuildState(_ state: BuildState, owner: String) {
        guard logOwner == owner else { return }
        buildState = state
    }

    @MainActor
    private func refreshLastBuild() async {
        lastBuild = await lastBuildRecord(of: app.name)
    }

    /// The newest row `--builds` has for one app, or nil.
    ///
    /// Takes the name rather than reading `app.name`, because `runBuild` calls
    /// it about the build's *owner* — which is the displayed app only until the
    /// user clicks another one mid-build.
    ///
    /// Answers nil where `refreshLastBuild` used to leave `lastBuild` untouched
    /// on a failed subprocess. That difference is real but confined: the one
    /// caller that assigns it does so straight after a build, where a
    /// `--builds` run that could not even start is not evidence the previous
    /// record still describes anything.
    @MainActor
    private func lastBuildRecord(of appName: String) async -> BuildRecord? {
        let result = try? await runner.runOnce(
            arguments: [
                Flag.json, Flag.builds, Flag.buildsApp, appName, Flag.buildsLimit, "1",
            ]
        )
        guard let stdout = result?.stdout else { return nil }
        for event in stdout.buildEvents() {
            if case let .buildRecord(record) = event { return record }
        }
        return nil
    }

    /// Re-read which IPA the next build would use — and keep the answer only
    /// if this pane still shows the app it was asked for.
    ///
    /// `app` is this struct copy's, so it always names the app the read
    /// started for; `logOwner` is shared `@State` storage and names the app on
    /// screen *now* (the distinction `runBuild` draws). Both callers can be
    /// overtaken by a sidebar click: the per-app `.task`, and the completion
    /// watcher a drop or get finishing just after a switch triggers.
    private func refreshCachedIPA() async {
        let owner = app.name
        do {
            let record = try await store.cachedIPA(appName: owner)
            guard logOwner == nil || logOwner == owner else { return }
            cachedIPA = record
        } catch {
            guard logOwner == nil || logOwner == owner else { return }
            ipaError = error.localizedDescription
        }
    }

    private func detachTweak(_ filename: String) async {
        tweakError = nil
        do {
            _ = try await store.detachTweak(appName: app.name, filename: filename)
        } catch {
            tweakError = error.localizedDescription
        }
    }

    @MainActor
    private func handle(event: BuildEvent, owner: String) {
        // Everything outside the build stream is not this panel's business.
        guard let progress = event.progress else { return }
        // Recorded ahead of the ownership guard, and the only line here that
        // is: these records belong to the store, not to the panel, and it is
        // the build the user navigated away from that needs them most — that
        // user is about to import an IPA that crashes, or is looking at a pane
        // with no sign the build failed at all. Same reasoning as `runBuild`'s
        // unguarded `refreshDeliveryStates()`.
        store.note(progress, forBuildOf: owner)
        // Drop everything from a build the pane is no longer showing. Clearing
        // `logLines` on an app switch is not enough on its own: the subprocess
        // is still alive and its events would refill the panel under the new
        // app's name a second later.
        guard logOwner == owner else { return }
        // And everything from a build the user has stopped. `stream`'s
        // continuation resumes on the MainActor while the stdout reader thread
        // may still have decoded events queued as unstructured
        // `Task { @MainActor in onEvent(event) }` jobs — the same ordering
        // `CLIRunner.streamEvents` sidesteps by reading `result.events`. One
        // of those landing after the cancel repaints `buildState` back to
        // `.running(stage:)` and strands the pane showing a live build for a
        // process that is already dead.
        guard !store.wasCancelled(owner) else { return }
        // The header's state machine, which is the only part of this stream
        // that is this panel's own. The transcript below is not: it is the
        // same mapping the re-install sheet and the replayed build log render,
        // and keeping a third copy of it here is what let a failed build read
        // `✗ <error>` in this panel and `✗ <error> (12.4s)` in the sheet that
        // replays the very same build-end off disk.
        switch progress {
        case .stage(let name, "start"):
            buildState = .running(stage: name)
        case .finished(let ok, let error, _):
            buildState = ok ? .ok : .failed(message: error ?? "failed")
        case .stopped:
            // `noteCancelled` writes the transcript line, naming the stage.
            buildState = .cancelled
            return
        default:
            break
        }
        logLines += BuildLogView.lines(for: event)
    }
}
