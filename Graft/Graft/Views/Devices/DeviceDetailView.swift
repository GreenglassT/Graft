import SwiftUI

/// One connected phone, as a place of its own.
///
/// This pane is composition and loading only: the header, the stat strip, your
/// apps on this phone, the decrypt card, and the installed-app list at the
/// bottom each own their own body. Keeping this file thin is deliberate.
/// `DevicesView`, the pane this replaces, grew every section inline
/// and reached 861 lines, at which point the card that blanked the whole window
/// was several screens away from the toolbar it pushed off the top.
///
/// The structure is `AppDetailView`'s, for the same reason: a fixed head above
/// a divider, and everything else inside a `ScrollView`. A section that sizes
/// the pane instead of scrolling inside it is what pushes the header past the
/// window's top edge — see `DeviceDetailLayoutTests`, which measures that
/// property, and `DeviceDecryptSection`, which records the bug and is the card
/// that caused it.
struct DeviceDetailView: View {
    let device: DeviceRecord
    /// The listing this device came from. Held for two things: the Refresh
    /// button re-runs it, and `lastError` is where a failed refresh lands.
    @ObservedObject var store: DevicesStore
    /// Vitals for every phone, keyed by udid. Shared rather than per-pane, so
    /// switching between two devices doesn't re-run a lockdown query for a
    /// reading taken seconds ago.
    @ObservedObject var deviceInfo: DeviceInfoStore
    /// The configured app list, for "Your apps on this phone".
    ///
    /// Note what this pane reads off it and what it does not: `apps`, and never
    /// `deliveryStates`. That field is `--list`'s unscoped answer — "delivered
    /// anywhere" — which is the right thing for the sidebar and the wrong thing
    /// here. See `DeviceDeliveryStore`.
    @ObservedObject var config: ConfigStore
    /// Delivery states scoped to one phone, keyed by udid — the store that
    /// makes this pane's app section say something the app list cannot.
    @ObservedObject var delivery: DeviceDeliveryStore
    /// Real app icons, resolved centrally by `ContentView` and shared with the
    /// sidebar and the app pane; this pane hands it to the sections that
    /// draw them.
    @PassedThrough var artwork: ArtworkStore

    /// Installed apps per phone, and the state of the one dump that can be
    /// running — shared, keyed by udid, and owned by `ContentView` beside its
    /// three siblings.
    ///
    /// **One store for both the decrypt card and the installed-apps list**, and
    /// for two separate reasons that used to pull against each other:
    ///
    /// The dump has to be shared, because two controls start one — the card's
    /// one-click button and the Dump on each encrypted row below it.
    /// `DeviceAppsStore` allows exactly one dump at a time (both entry points
    /// `guard dumping == .idle`), so two instances would let the two controls
    /// each believe they were idle while the CLI refused the second run. It is
    /// also what makes the card the place a row dump narrates itself — notes,
    /// transcript, success line, the one offered retry — which `DevicesView`
    /// recorded losing as a real loss.
    ///
    /// The *listing* used to be a second instance, so that a failed dump's
    /// banner could not appear under a section that had nothing to do with the
    /// dump. That concern is real and is kept; it is now kept by two fields
    /// (`lastError` for the dump, `listErrors[udid]` for the listing) rather
    /// than two objects. Two instances answered the wrong question: neither
    /// could say which phone it was about, which is why one of them needed
    /// `.id(device.udid)` — throwing away a 9s listing on every switch — and
    /// the other showed the previous phone's readiness under this phone's name.
    ///
    /// Shared across panes rather than built here, for the reason `deviceInfo`
    /// is: coming back to a phone you have already listed should not re-run a
    /// ten-second enumeration of it.
    @ObservedObject var deviceApps: DeviceAppsStore

    /// The IPA cache, keyed by bundle id — owned by `ContentView` beside
    /// `deviceApps` rather than built here.
    ///
    /// **Not for the reason `deviceApps` is owned there.** That store is
    /// shared because its listing is manual, keyed by udid, and 8.4–10.8s —
    /// a cost worth avoiding on a phone switch. This store's own `load()` is
    /// unkeyed, unconditional and ~0.25s, and this pane already re-runs it on
    /// every appearance (`.task` below) and after every dump, so sharing it
    /// avoids nothing on the phone-switch path a per-pane instance would not
    /// also avoid — `ContentView` builds this pane without `.id(device.udid)`,
    /// so SwiftUI already reuses one `DeviceDetailView` instance across a
    /// switch, and a `@State` store on it would survive intact regardless of
    /// who owns it.
    ///
    /// What owning it in `ContentView` actually buys: `hasLoaded` and
    /// `byBundleId` survive a detour through an app or settings pane instead
    /// of resetting to "never asked" each time this view is rebuilt; the
    /// previous join stays on screen for the ~0.25s a reload takes rather than
    /// every row reverting to its pre-feature shape; and it sits declared and
    /// initialized beside its siblings — `deviceInfoStore`,
    /// `deviceDeliveryStore`, `deviceAppsStore` — in `ContentView`, rather
    /// than being the one device-pane store singled out for a different home.
    /// Read by `InstalledAppsSection` below to decide which row is
    /// `.addable`; see `StagedIPAStore`'s own doc for why it carries no error
    /// channel.
    @ObservedObject var staged: StagedIPAStore
    @PassedThrough var liveContainer: LiveContainerStore
    /// Only for "Add as app"'s sheet, which takes it whatever it was opened
    /// from (`AddAppSheet.appStore`). A prefilled sheet has nothing to stage
    /// through it: its IPA is already in the cache.
    @PassedThrough var appStore: AppStoreStore
    /// Select the app that already claims a row's bundle id, by config key —
    /// forwarded to `InstalledAppsSection`'s `onSelectApp`.
    ///
    /// Required, not defaulted, unlike `InstalledAppsSection`'s own parameter
    /// of the same name. A `.configured` row already commits to looking
    /// clickable — pointer cursor, hover tint, a `.help` string and an
    /// accessibility label both promising "select it to see its delivery
    /// state" — so a caller that forgets to wire this produces a control that
    /// looks alive and does nothing, silently. `InstalledAppsSection` and
    /// `DeviceAppTile` keep their own defaults, because those are what let
    /// their own test files construct them without wiring navigation; this is
    /// the one production link in the chain, and `ContentView` is its only
    /// production caller. The test harnesses that build this view directly
    /// (`DeviceDetailLayoutTests`, `DeviceVitalsPollTests`) pass an explicit
    /// no-op instead, which says "no navigation to test here" at the call
    /// site rather than leaving it to a default declared 500 lines away.
    let onSelectApp: (String) -> Void

    /// Bundle id → every config key that claims it, in `ConfigStore.apps`'
    /// own order — read by both `DeviceDecryptSection` and
    /// `InstalledAppsSection` to decide whether a bundle id is already
    /// configured.
    ///
    /// A computed property rather than built inline at each call site, which
    /// is what it was until this task added the second reader: two literal
    /// copies of `Dictionary(grouping:by:).mapValues { … }` are two places a
    /// future edit — say, switching the grouping key — can be made to one and
    /// not the other, and the two call sites disagreeing about which bundle
    /// ids are configured is not a compiler error, only a card and a row
    /// underneath it quietly answering the same question differently.
    ///
    /// Grouped, not deduped: two config entries can share a bundle id — two
    /// tweak sets against one app is a real thing to want — and
    /// `InstalledAppsSection`'s row names every one of them on hover while
    /// selecting the first. `mapValues` keeps every name per bundle id;
    /// collapsing with `uniquingKeysWith:` would silently drop the second
    /// entry before it ever reached either reader.
    ///
    /// `config.apps` is in `ConfigStore`'s own order, sorted by name, so "the
    /// first" is stable.
    private var configuredByBundleId: [String: [String]] {
        Dictionary(grouping: config.apps, by: \.bundleId).mapValues { $0.map(\.name) }
    }

    /// How long the vitals poll waits between reads, in nanoseconds.
    ///
    /// **60 seconds, and the interval is the smaller half of this decision.**
    /// The range this was to be picked from was 30–60s. What settles it is
    /// asking what the poll is actually able to serve, because the two values
    /// it refreshes are served very differently:
    ///
    /// **Battery percentage is served well enough at 60s to make 30s a poor
    /// trade, though not a worthless one.** The value has 1% resolution, so
    /// the most a poll can do is catch each step. Across the reads timed while
    /// choosing this number the iPhone 15 Pro stepped 56% to 55% once, across
    /// roughly 80 seconds of wall clock — one observation, so the drain *rate*
    /// is not established, but it is the right order of magnitude to reason
    /// from and it is slower than a 60s poll. With steps that far apart,
    /// neither interval leaves the strip more than one percentage point
    /// behind, which is the resolution of the number itself.
    ///
    /// What 30s *would* buy is duration, not magnitude, and the honest version
    /// of this argument says so: against ~80s steps, a 60s poll shows the
    /// superseded value for up to 60s of each step and a 30s poll for up to
    /// 30s. Halving that is a real improvement — it is just a small one, over
    /// a one-point error, for twice the standing cost: one `--device-info`
    /// measured at a **median of 1.02s** over Wi-Fi on this bench (seventeen
    /// runs inside 0.87–1.13s, 2026-08-16; see `DeviceInfoStore` for the
    /// eighteenth) is a 1.7% duty cycle at 60s and 3.4% at 30s.
    ///
    /// **Charging state is served badly, and no affordable interval fixes
    /// that** — this is worth stating plainly rather than picking a number
    /// that implies otherwise. Plugging a cable in is a physical act the user
    /// performs and then looks at the pane to confirm. At 60s that
    /// confirmation is up to 60s late; at 30s it is up to 30s late. Both are
    /// far past the point where someone assumes it did not work and reaches
    /// for Refresh. Making it feel immediate needs something on the order of a
    /// 2s interval, which at 1.02s a read is a lockdown handshake running
    /// essentially continuously against a phone over Wi-Fi — not affordable,
    /// and not what the timer is for. So the poll's job is to keep a pane
    /// that has been open for minutes from showing a reading from when it
    /// opened; **Refresh remains the answer to "I just did something to this
    /// phone"**, and the interval is chosen for the value the poll can serve.
    ///
    /// Between two intervals that both fail at charging state, the cheaper one
    /// wins, so: 60s.
    static let defaultVitalsPollInterval: UInt64 = 60_000_000_000

    /// The interval this instance polls at — the constant above in the app,
    /// and a few hundred milliseconds under test.
    ///
    /// Injectable because the alternative is a test that waits a real minute
    /// to watch a timer tick twice, and a test that slow gets deleted by
    /// whoever next has to sit through it. Nothing in the app passes this;
    /// `ContentView` builds the pane on the default.
    let vitalsPollInterval: UInt64

    /// The phone whose SSH password is being set, if the sheet is up.
    ///
    /// Always this pane's own device — the sheet is opened from the decrypt
    /// card, which cannot name another phone. Held as `DeviceRecord?` rather
    /// than `Bool` so `.sheet(item:)` rebuilds the sheet if the listing
    /// refreshes the record underneath it.
    @State private var sshPasswordFor: DeviceRecord?

    /// The add sheet's prefill, or nil when the sheet is closed.
    ///
    /// `.sheet(item:)` rather than `.sheet(isPresented:)`, because the
    /// `Prefill` a press of "Add as app" builds — the staged record plus the
    /// phone's installed version — *is* everything the sheet needs to open.
    /// A `Bool` would have to sit beside a separately-held `Prefill` and can
    /// only ever drift out of sync with it (opened with no prefill set, or a
    /// stale one left behind after close); holding the value itself, and
    /// letting its presence mean "open", leaves no second piece of state to
    /// keep in sync at all.
    @State private var addingApp: AddAppSheet.Prefill?

    /// Whether this window is being looked at, and so whether the vitals poll
    /// below should be running at all.
    ///
    /// Read here for the reason `ContentView` reads it: it is a *per-window*
    /// value rather than an application-wide one. **What it certainly buys is
    /// that a backgrounded app polls nothing** — no window is key while the
    /// app is not frontmost, so every pane reads `.inactive` and every poll
    /// stops.
    ///
    /// Whether it also stops a *second window* of an app that is frontmost is
    /// the claim this comment used to make and no longer does, because nobody
    /// has opened two windows and watched. The mechanism is documented, and
    /// points that way: this Xcode's SDK says `controlActiveState` has, since
    /// macOS 15.0, been "strictly mapped to and from
    /// `EnvironmentValues.appearsActive`" — `true` reads back `.key`, `false`
    /// reads back `.inactive`, and **`.active` is not produced at all any
    /// more** — and `appearsActive` follows the focused ("key") window, with
    /// exceptions for chrome such as a toolbar in the main window. On that
    /// reading a non-key window's pane is `.inactive` and does stop. Written
    /// down as the reason to expect it, not as a measurement of it.
    ///
    /// The gate below is therefore `!= .inactive` rather than `== .key`: on
    /// this OS those are the same test, and the older three-state spelling
    /// keeps working if `.active` ever comes back.
    @Environment(\.controlActiveState) private var controlActiveState

    /// When this pane last came into possession of a vitals reading, and for
    /// which phone.
    ///
    /// Exists so that a poll whose task restarts can sleep the *remainder* of
    /// its interval instead of a fresh full one — see `nextVitalsDelay()`,
    /// which is where the reasoning for that lives. Written by both automatic
    /// readers: the appearance task below, and each turn of the poll.
    ///
    /// **The header's Refresh deliberately does not touch it.** It could — the
    /// strip does get a fresh reading from it — and the only cost of leaving
    /// it alone is that the poll may re-read up to one interval after a manual
    /// Refresh rather than one interval after the read the Refresh performed.
    /// That is at worst one extra ~1s query per press of a button a user
    /// pressed because they wanted a query.
    @State private var lastVitalsReading: VitalsReading?

    /// Written out rather than left to the memberwise one, which `@State
    /// private var sshPasswordFor` makes `private` and so unusable from
    /// `ContentView`. Every parameter is a store this pane is handed; none is
    /// built here any more.
    init(
        device: DeviceRecord,
        store: DevicesStore,
        deviceInfo: DeviceInfoStore,
        config: ConfigStore,
        delivery: DeviceDeliveryStore,
        artwork: ArtworkStore,
        deviceApps: DeviceAppsStore,
        staged: StagedIPAStore,
        liveContainer: LiveContainerStore,
        appStore: AppStoreStore,
        onSelectApp: @escaping (String) -> Void,
        vitalsPollInterval: UInt64 = DeviceDetailView.defaultVitalsPollInterval
    ) {
        self.device = device
        self.store = store
        self.deviceInfo = deviceInfo
        self.config = config
        self.delivery = delivery
        self.artwork = artwork
        self.deviceApps = deviceApps
        self.staged = staged
        self.liveContainer = liveContainer
        self.appStore = appStore
        self.onSelectApp = onSelectApp
        self.vitalsPollInterval = vitalsPollInterval
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            DeviceHeader(
                device: device,
                isDefaultTarget: device.isDefault,
                isRefreshing: store.isLoading,
                onRefresh: { Task { await refresh() } }
            )
            .padding(.horizontal, Metrics.paneGutter)
            .padding(.vertical, Metrics.sectionGap)

            Divider().background(Palette.line)

            // A failed Refresh has to land somewhere visible *in this pane*,
            // and this is now the only place `DevicesStore.lastError` is drawn
            // at all — the pane that used to show it is deleted. An error with
            // nowhere to draw is a button that appears to do nothing. (The one
            // failure that can happen with no pane open, a retry from the
            // sidebar's unreadable-device row, is deliberately routed elsewhere;
            // see `SidebarView.retryDeviceListing`.) `ErrorBanner` bounds both
            // the message and any unbreakable token inside it, so it cannot be
            // the thing that blows this pane's height up.
            if let lastError = store.lastError {
                ErrorBanner(
                    message: lastError,
                    fit: .fullBleed(gutter: Metrics.paneGutter),
                    onDismiss: { store.lastError = nil }
                )
            }

            ScrollView {
                VStack(alignment: .leading, spacing: Metrics.sectionGap) {
                    DeviceStatStrip(device: device, info: deviceInfo.info[device.udid])
                    DeviceAppsSection(
                        apps: config.apps,
                        // Read here rather than inside the section, so the
                        // dependency on this store lands on the pane that owns
                        // the `.task` refreshing it.
                        readout: delivery.readout(for: device.udid),
                        artwork: artwork
                    )
                    // Under the apps, not above them: this is an action on the
                    // phone rather than a description of it, and the pane reads
                    // top-down from what the phone *is* to what can be done to
                    // it.
                    DeviceDecryptSection(
                        device: device,
                        deviceApps: deviceApps,
                        staged: staged.byBundleId,
                        configuredByBundleId: configuredByBundleId,
                        onAdd: { record in
                            // installedVersion: nil — not because this pane
                            // necessarily lacks a listing. `deviceApps.apps`
                            // survives a phone switch the same as `staged`
                            // does, so if "List apps" was ever pressed here
                            // the installed version is sitting right there.
                            // Nil is still right, for the same reason the
                            // sibling `onAdd` below hands
                            // `InstalledAppsSection` `app.version` rather
                            // than `record.version`: a frontmost dump
                            // necessarily stages what is installed *right
                            // now*, so any disagreement with
                            // `deviceApps.apps[device.udid]` is that listing
                            // being stale — refreshed only by a manual "List
                            // apps" press, never re-run after a dump — not
                            // the phone actually running an older version.
                            // Printing it would make `Prefill.versionLine`
                            // assert "This phone runs 1.0; the staged dump
                            // is 2.0" — a false statement about the phone at
                            // the moment it is made. nil omits the line
                            // rather than asserting something false. This
                            // route also never needed a listing to reach
                            // this point at all (see this card's own file
                            // doc), so skipping one here keeps that true all
                            // the way to the sheet.
                            // installedName: nil for the same reason
                            // installedVersion is above — this route stages
                            // what a frontmost dump just found on the phone,
                            // with no `--device-apps` listing behind it to
                            // have a name from at all. `record.displayName`
                            // (the IPA's own plist) is what `AddAppSheet`
                            // falls back to, "Unknown" included.
                            addingApp = AddAppSheet.Prefill(
                                staged: record, installedVersion: nil, installedName: nil
                            )
                        },
                        onSetSSHPassword: { sshPasswordFor = device }
                    )
                    // Last on the pane: what's configured, then what can be
                    // decrypted, then everything else on the phone — top-down
                    // from the narrowest question to the broadest one.
                    DeviceLiveContainerSection(
                        device: device, store: liveContainer, account: config.account
                    )
                    InstalledAppsSection(
                        device: device,
                        deviceApps: deviceApps,
                        // The dump's two facts, handed down rather than read by
                        // the section: a row's Dump runs through the card above,
                        // so that is where the row's spinner and the busy state
                        // live. Unkeyed on purpose — there is one dump.
                        dumpingBundleId: deviceApps.decrypting,
                        isDumping: deviceApps.isDumping,
                        onDump: { app in
                            Task {
                                await deviceApps.decrypt(
                                    app,
                                    // This phone, always. The pane is one
                                    // device and there is nothing here that
                                    // could name another — the picker this
                                    // replaces resolved to nil with two phones
                                    // attached and let `default_device_udid`
                                    // decide, which aimed dumps at the delivery
                                    // target rather than the jailbroken phone.
                                    device: device.udid,
                                    // The defaults, deliberately, rather than a
                                    // second copy of the card's two controls
                                    // over the same piece of state. `.automatic`
                                    // drops only what no tool can decrypt and
                                    // names what went; a nil backend lets the
                                    // CLI choose *and* escalate, where naming
                                    // one pins it and turns the retry off. The
                                    // card above is where either is overridden.
                                    extensions: .automatic,
                                    backend: nil
                                )
                            }
                        },
                        // See `configuredByBundleId`'s own doc: shared with
                        // `DeviceDecryptSection` above rather than built
                        // twice, since a second copy is a second place the
                        // grouping rule can drift from this one.
                        configuredByBundleId: configuredByBundleId,
                        // `nil` until `staged.hasLoaded` — see
                        // `InstalledAppsSection.staged`'s own doc for why a
                        // dictionary plus a separate loaded flag collapses
                        // into one optional.
                        staged: staged.hasLoaded ? staged.byBundleId : nil,
                        // `app.version` — what the phone reports installed —
                        // not `record.version` — what the cache holds.
                        // Handing it `record.version` would make
                        // `Prefill.versionLine` compare a value against
                        // itself and never fire.
                        //
                        // The two disagreeing is not a dump lagging the
                        // phone — a dump necessarily dumps what is installed
                        // *right then*, so the staged version is always
                        // current as of that dump. What can lag is `app`
                        // itself: this listing is refreshed only by a button
                        // press, nothing re-runs it after a dump, so a stale
                        // listing is what disagrees with a fresh one. List at
                        // 1.0, the app updates to 2.0 on the phone, dump
                        // (which stages 2.0), add: the sheet reads "This
                        // phone runs 1.0; the staged dump is 2.0" — the
                        // listing behind, the dump current. `app.version` is
                        // still the right field to hand over — the
                        // alternative is a ~9s device round trip at the
                        // moment of the press, and `lastLoad` carries no
                        // timestamp this could use to decide the listing is
                        // stale and skip it — the direction of the
                        // disagreement is just the reverse of what this
                        // comment used to claim.
                        onAdd: { app, record in
                            // `app.name` — the phone's own name for this
                            // installed app, from `--device-apps` — not
                            // `record.displayName`, the IPA's own plist,
                            // which `ipa_inspect.py` falls back to
                            // "Unknown" for when neither `CFBundleDisplayName`
                            // nor `CFBundleName` is present. This route has a
                            // real listing behind it (unlike the decrypt
                            // card's own `onAdd` above), so there is always a
                            // real name here to prefer.
                            addingApp = AddAppSheet.Prefill(
                                staged: record, installedVersion: app.version,
                                installedName: app.name
                            )
                        },
                        onSelectApp: onSelectApp
                    )
                    // No `.id(device.udid)` here any more, and its removal is
                    // the point of the change above it. It was doing real work:
                    // the section's listing lived in a store it owned, keyed by
                    // nothing, so without the `.id` SwiftUI reused one instance
                    // across a selection change and drew the first phone's
                    // installed apps under the second phone's name. But the
                    // remedy discarded the whole section on every switch — an
                    // 8.4–10.8s listing thrown away for a glance at another
                    // phone, and no way back but pressing the button again.
                    // `DeviceAppsStore` is keyed by udid now, so the section
                    // reads *this* phone's listing out of a store that kept the
                    // other one too.
                }
                .padding(.horizontal, Metrics.paneGutter)
                .padding(.vertical, Metrics.sectionGap)
            }
        }
        .background(Palette.bg)
        .navigationTitle(device.name)
        // Per device, because the credential is: the Keychain entry is keyed by
        // UDID, and the jailbroken phone that dumps is not the phone builds are
        // delivered to. Presented from the pane rather than from the card that
        // opens it, because saving goes through `DevicesStore` — which this pane
        // holds and that card, which takes only a runner, does not.
        //
        // The failure lands on `store.lastError`, which this pane already draws
        // as a banner above the scroll view. A credential that silently failed
        // to save is a dump that fails ten minutes later for a reason nobody can
        // connect to this sheet.
        .sheet(item: $sshPasswordFor) { device in
            SSHPasswordSheet(device: device) { password in
                do {
                    try await store.setSSHPassword(udid: device.udid, password: password)
                } catch {
                    store.lastError = error.localizedDescription
                }
                sshPasswordFor = nil
            }
        }
        // "Add as app" from a staged row, prefilled so nothing already known
        // — bundle id, display name, what the dump cost — is retyped. See
        // `AddAppSheet.Prefill` for what it fills and `addingApp`'s own doc
        // for why this is `.sheet(item:)`.
        //
        // `onAdded:` is spelled out as a labeled argument rather than a
        // trailing closure, and that is not just style: it does not compile the
        // other way. `AddAppSheet` has no custom initialiser, so its synthesised
        // one is `store:appStore:onAdded:prefill:`, in the order its stored
        // properties are declared — `onAdded` *before* `prefill`. Writing
        // `AddAppSheet(store: config, appStore: appStore, prefill: prefill) {
        // added in … }` gives the call the argument list
        // `store:appStore:prefill:_:`: the first three are matched to their own
        // labels, `onAdded` is skipped for its default in between, and nothing
        // re-associates an argument already bound to a later label back onto an
        // earlier, still-unmentioned parameter — so the trailing closure has
        // nothing left to attach to. `SidebarView`'s own `AddAppSheet(store:
        // store, appStore: appStore) { newName in … }` compiles as a trailing
        // closure only because it never names `prefill:` at all, so nothing has
        // been matched past `onAdded` for the closure to be appended after.
        // Confirmed by trying the trailing form here, before `appStore:` joined
        // the list: `swiftc` rejected it with "incorrect argument label in call
        // (have 'store:prefill:_:', expected 'store:onAdded:prefill:')" — and a
        // minimal struct reproducing this exact shape (a function-typed
        // defaulted parameter declared before a later, explicitly-labeled one)
        // fails identically, while the same struct with the function-typed
        // parameter moved to the true last position compiles clean, confirming
        // the position, not the type, is what matters.
        .sheet(item: $addingApp) { prefill in
            AddAppSheet(store: config, appStore: appStore, onAdded: { added in
                // `added` is the CLI's own answer — the key `add_app_manual`
                // actually wrote, which `addAndReload` reads back off the
                // `app-added` event rather than echoing the `--name` this
                // sheet sent. Those two can differ for more than one reason
                // — the field is editable and the CLI re-slugifies whatever
                // it receives — but the case that matters most is silent:
                // for example when `Prefill.suggestedName` collided with an
                // existing key and the CLI deduped it to `name_2`, selecting
                // the locally-derived name instead would select nothing,
                // since no row exists under it.
                onSelectApp(added)
            }, prefill: prefill)
        }
        // A dump just changed the cache — this is the moment a row becomes
        // addable — and there are three call sites that can make one happen:
        // this pane's own row Dump (`onDump` above), the decrypt card's
        // one-click frontmost button, and the card's offered retry. All three
        // funnel through `DeviceAppsStore.runDecrypt`, and `isDumping` is
        // true for exactly the span of any of them, so watching its
        // true→false transition here catches all three from one site — a
        // fourth dump route added later cannot forget to reload, because it
        // does not need to know this exists. An earlier version of this
        // wiring called `staged.load()` inline after the row Dump only, which
        // covered that one path and silently missed the other two: the
        // card's button and its offered retry stage IPAs exactly as a row
        // dump does, and neither would have refreshed this pane's rows —
        // "Add as app" would stay absent until the user left this phone and
        // came back, with nothing on screen saying why.
        //
        // Fires on both the true and the false edge, not only the one this
        // cares about — `now` is `true` when a dump starts and `false` when
        // it ends, and only the `false` case means the cache could have
        // changed. Unconditional on whether the dump succeeded: reading a
        // reload the cache didn't need is a wasted ~0.25s, and skipping one it
        // did need is a row that stays a lock and a Dump until the user
        // happens to leave this phone and come back — cheaper to always ask.
        //
        // `isDumping` is a computed property over a `@Published private var
        // dumping` on this store, not a stored property `onChange` reads
        // directly — confirmed to still fire correctly, both empirically
        // (a headless-window test observed `[true, false]` across a real
        // dump) and by precedent already shipping: `DeviceDecryptSection`'s
        // transcript auto-scroll keys an `.onChange` off
        // `deviceApps.transcript.count`, the identical shape.
        .onChange(of: deviceApps.isDumping) { nowDumping in
            if !nowDumping { Task { await staged.load() } }
        }
        // Keyed on the udid, not bare: `ContentView` builds this pane without
        // an `.id(...)`, so SwiftUI reuses one view across a selection change
        // between two phones. Unkeyed, this would load once and the second
        // phone's strip would show the first one's battery.
        .task(id: device.udid) {
            // Beside each other, not one after the other. The two are
            // independent — one asks lockdown about the phone, the other reads
            // the local delivery log — and the comment that used to sit here
            // claimed "neither waits on the other's result", which two
            // sequential `await`s make false: the delivery read did not start
            // until the vitals read had come back. Measured, medians of five:
            // **1.03s in sequence against 0.83s together** on the Wi-Fi phone,
            // 0.52s against 0.38s on the USB one. Smaller than Refresh's saving
            // because only two queries are involved and one of them is local,
            // but it is the same defect and the same one-line fix.
            //
            // Neither call throws, so there is no `do`/`catch` here — see
            // `refresh()` for why the one that does gets its own.
            async let vitals: Void = deviceInfo.load(udid: device.udid)
            async let states: Void = delivery.load(udid: device.udid)
            // Started alongside the other two rather than awaited after them.
            // `staged.load()` is local and ~0.25s on its own, so it runs on
            // every appearance rather than being cached across them — a dump
            // from another window, a build, or a file deleted in Finder all
            // change this and none of them tell us. But `CLIRunner` has no
            // timeout, and `deviceInfo.load` is a lockdown query against a
            // phone that can be asleep or off Wi-Fi indefinitely: awaited
            // sequentially after `vitals`, this local disk listing would sit
            // gated behind a hang it has nothing to do with, and if the user
            // switches phones before that hang resolves, this task is
            // canceled and the cache never gets read at all for this pane.
            // Starting it concurrently means it finishes on its own ~0.25s,
            // independent of whatever the phone does.
            async let stagedLoad: Void = staged.load()
            await vitals
            // Stamped here, not only in the poll, and this is what makes the
            // poll's clock mean "how old is the reading on screen" rather than
            // "how long has this task been alive". Two things follow from it:
            // pointing the pane at a phone starts a full interval (so the poll
            // never races this load for the same reading), and a window that
            // sat in the background past an interval reads immediately when it
            // comes forward instead of showing what it had on the way out.
            lastVitalsReading = VitalsReading(udid: device.udid, at: Date())
            await states
            await stagedLoad
        }
        // The vitals poll: the battery reading, kept from going stale under a
        // pane somebody is looking at.
        //
        // **Keyed on the window's active state as well as the udid, which is
        // what stops it.** `.task(id:)` cancels and restarts its body whenever
        // the id changes, so this loop is torn down the moment the window stops
        // being frontmost — the same mechanism `ContentView`'s device poll uses
        // and for the same reason. `.task(id:)` needs one `Equatable` value and
        // Swift tuples are not `Equatable`, so the pair goes through the small
        // struct below rather than a joined string; a string would key
        // correctly and describe nothing.
        //
        // **This sleeps before its first read, where `ContentView`'s poll reads
        // before its first sleep, and the inversion is deliberate.** That poll
        // is the only thing that loads the device list at all, so its first
        // pass *is* the initial load and returning to the app is a refresh.
        // Here the initial load already has an owner — the `.task(id:
        // device.udid)` directly above, which fires when the pane is pointed at
        // a phone. A poll that also read before sleeping would run a second,
        // simultaneous `--device-info` against the same phone on every pane
        // open: two lockdown handshakes for one reading, the second landing on
        // `info[udid]` with the same answer the first just wrote, paid on every
        // sidebar click onto a device.
        //
        // **What sleeping first cost, before `nextVitalsDelay()` existed, was
        // the whole poll under ordinary use.** Restarting on focus and sleeping
        // a fresh full interval compose into starvation, not staleness: alt-tab
        // away and back more often than the interval — and anything else that
        // moves key status around does the same — and the loop never once
        // reaches its read. The task
        // above is keyed on the udid alone and does not re-fire on refocus, so
        // nothing else refreshes the strip either; it sits at its pane-open
        // value indefinitely, and it is a *timer* that is broken, so nothing on
        // screen says so. That is unbounded, not the "up to one interval old"
        // this comment used to concede, and it is the direct product of sleep
        // first plus a key that includes focus.
        //
        // The fix keeps both and drops the fresh sleep: the pane remembers when
        // it last got a reading, and a restart waits the *remainder* of the
        // interval measured from that reading. See `nextVitalsDelay()`.
        .task(id: VitalsPollKey(udid: device.udid, active: controlActiveState)) {
            guard controlActiveState != .inactive else { return }
            while !Task.isCancelled {
                // The interval is waited *between* reads rather than against a
                // wall clock, which is what keeps a stalled query from stacking
                // another behind it: one `--device-info` attempt timed on this
                // bench took **77.7 seconds** over Wi-Fi, well past any
                // interval worth choosing. Because the next delay is computed
                // from the moment the previous read *returned*, a stall delays
                // the next tick instead of overlapping it. Nor does a stall
                // outlive the window's focus — `CLIRunner`'s cancellation
                // handler SIGTERMs the child (escalating to SIGKILL after two
                // seconds), and losing focus cancels this task.
                //
                // `try?` swallows the cancellation the sleep throws on
                // teardown.
                try? await Task.sleep(nanoseconds: nextVitalsDelay())
                // Re-checked rather than left to the loop condition, because
                // sleeping first puts that swallowed cancellation immediately
                // before the work instead of immediately before the condition.
                // `ContentView`'s poll gets this check for free by sleeping
                // last; this one has to write it out.
                //
                // It is not what stops a canceled task from spawning a
                // subprocess — `CLIRunner` already does that, and would whether
                // this guard existed or not: `ProcessHandle.adopt` returns
                // false once cancellation has arrived, `launch` throws
                // `CancellationError` without calling `run()`, and `load`
                // swallows the throw. What the guard buys is skipping work
                // already known to be wasted — building the argv, the Process,
                // the pipes and the drainers for a call that will refuse to
                // launch — one branch to avoid it.
                guard !Task.isCancelled else { break }
                // Vitals only. The delivery states loaded beside them above are
                // a local log read that changes when a build is pushed, not
                // with time, and the header's Refresh is what re-reads them.
                await deviceInfo.load(udid: device.udid)
                lastVitalsReading = VitalsReading(udid: device.udid, at: Date())
            }
        }
    }

    /// The pair the vitals poll restarts on.
    ///
    /// A tuple would say this more directly, but `.task(id:)` takes one
    /// `Equatable` value and Swift tuples do not conform. Two fields with
    /// names beat `"\(udid)-\(active)"`, which conforms by accident and reads
    /// as a cache key rather than as the two questions it is: which phone, and
    /// is anyone looking.
    private struct VitalsPollKey: Equatable {
        let udid: String
        let active: ControlActiveState
    }

    /// When a vitals reading was taken, and off which phone.
    ///
    /// The udid is half the value, not a label. It is what tells a restarting
    /// poll whether the timestamp it is holding describes the phone it is about
    /// to query or the one the sidebar just moved off — and those want opposite
    /// answers. See `nextVitalsDelay()`.
    private struct VitalsReading {
        let udid: String
        let at: Date
    }

    /// How long the poll waits before its next read: a full interval when this
    /// pane has just taken a reading, and the *remainder* of one when it is
    /// restarting over a reading that is already part-way through its life.
    ///
    /// **This is what makes the poll survive being restarted.** Its task is
    /// keyed on focus as well as the udid, so every focus change tears it down
    /// and starts it again; with a flat `sleep(interval)` each of those started
    /// the clock over, and anything that changes focus more often than the
    /// interval — alt-tabbing certainly, and whatever else moves key status
    /// about — produced a poll that never read at all, indefinitely and
    /// silently. Sleeping the remainder makes a restart free: the reading still
    /// arrives one interval after the last one, whoever canceled what in
    /// between.
    ///
    /// It also retires a cost the first version of this poll wrote down as
    /// accepted. Time spent inactive counts against the interval, because the
    /// question the clock answers is "how old is the reading on screen", not
    /// "how long has this loop been awake" — so a window that has been in the
    /// background longer than an interval reads *immediately* when it comes
    /// forward, rather than showing what it had on the way out for another
    /// minute.
    ///
    /// Two guards on the arithmetic:
    ///
    /// - **A stamp from another phone is no stamp at all**, and gets a full
    ///   interval. Clicking a phone in the sidebar re-fires the appearance
    ///   task, which reads vitals immediately; a remainder carried over from
    ///   the previous phone would let the poll read the new one a moment later
    ///   for the same value, which is the double-read this whole design is
    ///   arranged to avoid.
    /// - **The result is clamped into `0...vitalsPollInterval`.** `Date` is a
    ///   wall clock and can step backwards under NTP; without the upper clamp
    ///   that would mean a sleep longer than the interval, and the lower one is
    ///   what expresses "this reading is already overdue — read now".
    private func nextVitalsDelay() -> UInt64 {
        guard let last = lastVitalsReading, last.udid == device.udid else {
            return vitalsPollInterval
        }
        let elapsed = Date().timeIntervalSince(last.at) * 1_000_000_000
        let remaining = Double(vitalsPollInterval) - elapsed
        return UInt64(max(0, min(remaining, Double(vitalsPollInterval))))
    }

    /// Every part of what this pane shows, because the header's Refresh is the
    /// only control on it and "refresh" plainly means all of it: the listing
    /// (name, iOS version, connection, LiveContainer), the vitals (storage,
    /// battery) and this phone's delivery states come from three different CLI
    /// invocations.
    ///
    /// The delivery states in particular have to be here rather than only on
    /// the pane's `.task`. That task is keyed on the udid, so it fires when the
    /// user *changes* phones and never again — and the states it read go stale
    /// on the ordinary sequence of building an app and coming back to look at
    /// the phone it was pushed to. Refresh is the one gesture that says "tell
    /// me again", and a section it left untouched would answer with what was
    /// true when the pane opened. Same defect `ConfigStore.refreshDeliveryStates`
    /// exists for, one pane over.
    ///
    /// Every load is unconditional rather than skipped when an earlier one
    /// fails. They are independent queries, and the case where the listing
    /// fails is exactly the case where a still-answering phone is worth
    /// showing. `DeviceInfoStore.load` swallows its own failures by design, and
    /// `DeviceDeliveryStore.load` records its own.
    private func refresh() async {
        // Started together, not one after another. Three independent
        // subprocesses — a lockdown listing of every phone, a lockdown vitals
        // read of this one, and a local delivery-log read — and awaited in
        // sequence they simply added up. Measured against the bench pair with
        // the Wi-Fi phone selected, medians over repeated runs: **1.8–2.0s
        // serial against 0.83–0.92s concurrent** (per verb: `--devices` 0.81s,
        // `--device-info` 0.84s, `--delivery-state` 0.19s). The button now
        // waits for the slowest query rather than for the sum of all three.
        //
        // `try await` in its *own* `do`, with the other two awaited outside it,
        // is the same shape — and the same reason — as `ContentView`'s settings
        // pane: a failure escaping this scope cancels the `async let` siblings
        // still in flight. `store.load()` is the one that throws, and it throws
        // exactly when a phone has dropped off the bus, which is the moment the
        // vitals and delivery states this pane can still show are worth most.
        // Canceling them to report the listing's failure would trade three
        // answers for one error.
        async let listing = store.load()
        async let vitals: Void = deviceInfo.load(udid: device.udid)
        async let states: Void = delivery.load(udid: device.udid)

        do { _ = try await listing }
        catch { store.lastError = error.localizedDescription }
        await vitals
        await states
    }
}
