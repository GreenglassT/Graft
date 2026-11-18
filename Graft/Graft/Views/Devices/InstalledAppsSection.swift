import SwiftUI

/// Everything installed on this phone — not just the apps this Mac configures.
///
/// `DeviceAppsSection` (Task 10, above this one on the pane) answers "how is
/// *my* app doing on this phone?" for the rows in `config.yaml`. This section
/// answers a different question — "what is actually on this phone at all?" —
/// and the two lists are not the same size or shape: a phone carries system
/// apps, App Store apps nobody built here, and apps this Mac configures too.
/// Reading one as a stand-in for the other is exactly the mistake this pane's
/// whole restructure exists to undo (see `DeviceAppsSection`'s own doc).
///
/// A real icon per row, when the phone had one to give. `--with-icons` (see
/// `DeviceAppsStore.loadArgs`) asks the CLI to resolve each listed app's icon
/// through the same cascade `--artwork` uses and hand back a path to a cached
/// PNG on the `device-app` event. **Absent is not an error** — a
/// LiveContainer-delivered app is not a top-level install, so SpringBoard
/// never had an icon for it to hand back — so `AppIcon`'s lettered tile is the
/// correct, ordinary answer for those rows, not a degraded one.
///
/// ## The listing's fields, the card's fields, one store
///
/// This section and `DeviceDecryptSection` read one `DeviceAppsStore`, which
/// `ContentView` owns. They read it for two different jobs, and the separation
/// that matters is between the *fields*, not between two objects: a failed
/// listing goes to `listErrors[udid]` and a failed dump to `lastError`, so a
/// dump that blew up still cannot put its banner under this section, and a slow
/// listing still cannot spin the decrypt card's "Checking…" label.
///
/// It used to be two instances, one per concern, and that answered the wrong
/// question: neither instance was keyed by udid, so neither could say which
/// phone it was about. This section's copy had to be `.id(device.udid)`-ed to
/// stop it drawing the previous phone's apps, which threw away a ~9s listing on
/// every switch; the card's copy was not, and drew the previous phone's
/// readiness under this phone's name. See `DeviceAppsStore`.
///
/// The per-row Dump keeps the split rather than breaking it: this section owns
/// no dump state at all, it *asks* for one through `onDump` and is handed back
/// the two facts a row needs to draw itself (`dumpingBundleId`, `isDumping`).
/// The dump then runs on the same store, and the decrypt card is where every
/// word it has to say — the CLI's notes, the backend transcript, the success
/// line naming what it stripped, the one offered retry — is already rendered. A
/// row dump on the pane this replaced could only ever show an error banner, and
/// `DevicesView` recorded that as a real loss.
///
/// ## Manual, not automatic
///
/// Loading is a button, not a `.task`. `DeviceAppsStore.load`'s own doc
/// explains why for the plain case — a real round trip to the device — and
/// `--with-icons` makes it slower still (~9s measured against a real iPhone
/// with 67 apps installed). A pane that ran this on every appearance would
/// stall opening a phone that's asleep or off the network for no reason
/// anyone asked for.
///
/// ## A list, not the grid a later plan turns this into
///
/// Search and filtering belong to that future work. The one action here is the
/// Dump on each encrypted row, which is a capability this app already had — it
/// was on `DevicesView`'s installed-app list, aimed at whichever phone a picker
/// named, and it is kept rather than lost in the move.
struct InstalledAppsSection: View {
    /// The phone this section enumerates — the only phone it can enumerate,
    /// same as `DeviceDecryptSection`: this pane is one device, and there is
    /// no picker here to get wrong. It is also the phone every row's Dump aims
    /// at, which is the half of that the picker used to get wrong.
    let device: DeviceRecord

    /// The shared store, handed down by the pane. Every field this section
    /// reads off it is looked up by `device.udid` — which is what lets one
    /// instance serve every phone, and what removed the `.id(device.udid)`
    /// that used to discard this listing on each switch.
    let deviceApps: DeviceAppsStore

    /// The bundle id being dumped right now, handed down by the pane — nil when
    /// nothing is. Drives one row's spinner.
    var dumpingBundleId: String?
    /// Whether any dump is in flight, this list's or the card's. Every Dump
    /// button is disabled while it is: the store takes one dump at a time and
    /// refuses the second silently.
    var isDumping: Bool = false
    /// Start a dump of this app. Defaulted rather than optional, so a caller
    /// that forgets it still renders the button — a layout test measuring rows
    /// with the buttons quietly absent would be measuring a list this app never
    /// draws.
    var onDump: (DeviceAppRecord) -> Void = { _ in }

    /// Bundle id → every config key that claims it, in `ConfigStore.apps`'
    /// own order. Built by the pane from `ConfigStore.apps`, so this section
    /// does not need the whole config to answer a yes/no question per row.
    ///
    /// A `[String]`, not a `String`, because design spec §4 is explicit that
    /// two config entries can share a bundle id: the chip selects the first
    /// but names both on hover (see `RowState.configured`). The pane must
    /// group rather than unique when it builds this — `Dictionary(_:
    /// uniquingKeysWith:)` collapsing to one name before it reaches here
    /// would make the second entry unrepresentable at this layer, not just
    /// unshown.
    var configuredByBundleId: [String: [String]] = [:]
    /// The cache, keyed by bundle id — `nil` when the listing has never
    /// finished, which is not the same claim as `[:]` (a listing that
    /// finished and found nothing staged). See `rowState`, which is the
    /// reader that has to keep the two apart.
    var staged: [String: StagedIPARecord]?
    /// Open the prefilled add sheet for this app and its staged IPA.
    var onAdd: (DeviceAppRecord, StagedIPARecord) -> Void = { _, _ in }
    /// Select the app that already claims this bundle id, by config key.
    var onSelectApp: (String) -> Void = { _ in }

    /// This phone's listing, or nothing if it has never been asked for.
    private var apps: [DeviceAppRecord] { deviceApps.apps[device.udid] ?? [] }
    private var isLoading: Bool { deviceApps.loading.contains(device.udid) }

    /// What one row offers, decided in one place.
    ///
    /// A static function on the type rather than a chain of conditions inside
    /// `DeviceAppTile`'s body, for the reason
    /// `DeviceAppsSection.statusLine(of:in:)` is one: the rule is the feature,
    /// and a rule spelled inside a `body` is a rule no test can read.
    enum RowState: Equatable {
        /// Already in `config.yaml`, under `primary` — the key a click
        /// selects. `siblings` is every other config key that shares this
        /// bundle id, in `ConfigStore.apps`' order, and may be empty.
        ///
        /// The split into a required primary plus a possibly-empty list,
        /// rather than one `[String]` that is merely never expected to be
        /// empty, is the point: `DeviceAppTile` is `internal` now (see its
        /// own doc), so `.configured([])` was constructible from anywhere in
        /// the module and `body`'s `names[0]` would have crashed on the
        /// click. This shape makes that state unrepresentable instead of
        /// guarding against it a third time — `rowState` already knows the
        /// first name when it builds the value, so asking it to hand back a
        /// non-optional primary costs nothing.
        ///
        /// `siblings` being non-empty is design spec §4's own case: two
        /// config entries can legitimately share a bundle id (an app added
        /// twice under different names, or a deliberate second profile). The
        /// row selects `primary` on a click and names every sibling on
        /// hover, rather than silently discarding them.
        case configured(String, siblings: [String])
        /// Not configured, but the cache holds an IPA — so adding it produces
        /// an app that can build immediately.
        case addable(StagedIPARecord)
        /// FairPlay-encrypted with nothing staged: the pre-feature row.
        case dumpable
        /// Decrypted and unconfigured — a sideload, or a LiveContainer app.
        case inert
    }

    /// `staged` is `[String: StagedIPARecord]?` rather than a dictionary plus
    /// a separate "has it loaded yet" flag. Before the cache listing lands a
    /// row must claim *nothing* about the cache — and offering no Add is a
    /// claim — so "never asked" and "asked, found nothing" have to be two
    /// states a caller cannot confuse. Two independent parameters
    /// (`staged: [:]`, `hasLoadedStaged: false`) could disagree with each
    /// other; `nil` makes the "never asked" state the only way to spell
    /// "nothing to offer yet", so there is nothing left to disagree. The real
    /// `StagedIPAStore` this reads from can never produce the incoherent
    /// combination either — `byBundleId` and `hasLoaded` are only ever
    /// written together at the end of a successful `load()` — but a
    /// production caller and a test fixture are both free to pass any two
    /// values by hand, which is exactly the gap this collapses. The listing
    /// is local and lands in a quarter of a second, so the distinction is
    /// invisible in practice and load-bearing in principle.
    static func rowState(
        for app: DeviceAppRecord,
        configured: [String: [String]],
        staged: [String: StagedIPARecord]?
    ) -> RowState {
        // Configured wins: an app already in the config is not offered again.
        // `names.first` rather than `names[0]` — an empty array for a
        // present key is a caller bug this function should not crash on, and
        // falling through to the cache/encrypted checks below is a more
        // honest answer than force-unwrapping a promise the caller broke.
        if let names = configured[app.bundleId], let primary = names.first {
            return .configured(primary, siblings: Array(names.dropFirst()))
        }
        if let record = staged?[app.bundleId] { return .addable(record) }
        return app.encrypted ? .dumpable : .inert
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Metrics.rowGap) {
            header
            // The *listing's* error, not the dump's. `lastError` on the same
            // store is a failed decrypt, and it belongs to the card above —
            // that separation is the whole of what two store instances used to
            // buy, and it is now two fields.
            if let error = deviceApps.listErrors[device.udid] {
                ErrorBanner(
                    message: error, fit: .inset,
                    onDismiss: { deviceApps.listErrors[device.udid] = nil }
                )
            }
            if apps.isEmpty {
                // A static, code-owned sentence — not the CLI's words — so
                // its length is ours to keep short rather than something this
                // pane's own history says must be `lineLimit`-ed against.
                Text(emptyStateMessage)
                    .font(AppFont.body)
                    .foregroundStyle(Palette.textMuted)
                    .lineLimit(1)
            } else {
                VStack(alignment: .leading, spacing: Metrics.rowGap) {
                    ForEach(apps) { app in
                        DeviceAppTile(
                            app: app,
                            state: Self.rowState(
                                for: app,
                                configured: configuredByBundleId,
                                staged: staged
                            ),
                            isDumpingThis: dumpingBundleId == app.bundleId,
                            isBusy: isDumping,
                            onDump: { onDump(app) },
                            onAdd: { record in onAdd(app, record) },
                            onSelectApp: onSelectApp
                        )
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var emptyStateMessage: String {
        isLoading
            ? "Reading everything installed on this phone…"
            : "Not read yet — this listing resolves real icons, so it's slower and manual."
    }

    /// Title, the count once a listing has actually finished, and the
    /// button that starts one.
    ///
    /// The count reads `lastLoad` for this phone, not `apps.count`: a udid
    /// with no `lastLoad` entry has never been listed, which is what keeps
    /// "haven't asked yet" from rendering as "0 installed" — the same
    /// distinction `DeviceAppsStore`'s own doc draws for the update check, and
    /// now per phone rather than for whichever one asked last.
    private var header: some View {
        HStack {
            SectionTitle(text: "Everything on this phone")
            if let lastLoad = deviceApps.lastLoad[device.udid] {
                Text("\(lastLoad.count) installed")
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
                    .lineLimit(1)
            }
            Spacer()
            Button {
                Task { await deviceApps.load(udid: device.udid) }
            } label: {
                if isLoading {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.small)
                        Text("Reading…")
                    }
                } else {
                    Symbols.refresh.label(apps.isEmpty ? "List apps" : "Refresh")
                }
            }
            .controlSize(.small)
            .disabled(isLoading)
        }
    }
}

/// One installed app: its real icon when the phone had one, name, version,
/// and the two facts worth a glance without opening anything else — whether
/// it's FairPlay-encrypted (a decrypt candidate) and whether it's on screen
/// right now.
///
/// **What the trailing edge draws depends on `state`, not on `app` alone.**
/// `InstalledAppsSection.rowState` joins this app against the config and the
/// staged-IPA cache first, and the four answers it can hand back —
/// `.configured`, `.addable`, `.dumpable`, `.inert` — are what `body`
/// switches on below. `.dumpable` is what an *encrypted* row draws with
/// nothing else true of it: **only an encrypted app gets a Dump button.** A
/// sideload, a jailbreak package or a LiveContainer-delivered app is already
/// decrypted, so dumping it is a no-op — worth leaving off the row rather
/// than offering and failing. That is the same rule `DevicesView`'s list
/// applied by filtering itself down to `encryptedApps`; this list shows
/// everything on the phone (that is its whole job), so the rule moves onto
/// the row.
///
/// Not `private`. `InstalledAppsSectionTests` renders this type directly, on
/// a headless `NSHostingView`, to send it real synthetic clicks — the only
/// way to pin that `.configured`'s `Button` actually receives one, which a
/// pure function over `RowState` cannot prove. `private` would have hidden it
/// from `@testable import` along with everything else in this file.
struct DeviceAppTile: View {
    let app: DeviceAppRecord
    /// What this row offers — see `InstalledAppsSection.rowState`.
    ///
    /// Required, not defaulted. `InstalledAppsSection`'s own inputs default,
    /// deliberately, so a caller that forgets one still renders the list this
    /// app actually draws — but that reasoning does not transfer here.
    /// `DeviceAppTile` has exactly one construction site (`InstalledAppsSection.
    /// body`'s `ForEach`), which already computes `state` for every argument
    /// it passes, so no caller can ever "forget" it. A default of `.dumpable`
    /// was tried here and was actively wrong: `.dumpable` draws a lock and a
    /// Dump button, which is a lie for a decrypted app — `.inert` is equally
    /// "today's shape" for those rows, and the type's own headline doc above
    /// says only an encrypted app gets a Dump button. A default that
    /// contradicts the doc six lines above it is worse than no default.
    let state: InstalledAppsSection.RowState
    /// This row's dump is the one running.
    var isDumpingThis: Bool = false
    /// Some dump is running — this row's or another's, on this pane or in the
    /// card above. One at a time, so every button is out.
    var isBusy: Bool = false
    var onDump: () -> Void = {}
    /// Open the prefilled add sheet for this app and its staged IPA.
    var onAdd: (StagedIPARecord) -> Void = { _ in }
    /// Select the app that already claims this bundle id, by config key.
    var onSelectApp: (String) -> Void = { _ in }

    /// **No whole-row `Button` any more.** Every state, `.configured`
    /// included, draws `row` bare below — this is Finding 3's fix, and the
    /// change is deliberate, not a simplification made in passing.
    ///
    /// Before this fix `.configured` alone wrapped `row` in a `Button` that
    /// filled the whole card, and `rowState` returning `.configured` before
    /// any `app.encrypted` test meant that arm drew *only* the "IN YOUR
    /// APPS" chip — an encrypted app already added lost both its lock badge
    /// and its Dump button the moment it was configured, with no other
    /// per-app dump control anywhere in the app to fall back on. Putting
    /// them back by nesting a second `Button` (`dumpButton`) inside the
    /// first one's label was tried and rejected: SwiftUI does not define
    /// which of two overlapping interactive controls a click resolves to,
    /// and the outer `Button` — the one already proven to own hit-testing
    /// across this whole card, `.contentShape` included — is exactly the
    /// element it would have to compete with for a Dump press.
    ///
    /// So the navigation moves to the one control that used to just be a
    /// static label: `configuredChip(primary:siblings:)` below wraps the "IN
    /// YOUR APPS" chip in its own small `Button`, sitting as an ordinary
    /// sibling of `encryptedBadge`/`dumpButton` in `row`'s trailing
    /// `HStack` rather than as their container. Nothing on this row
    /// overlaps anything else, so there is no dispatch ambiguity to resolve.
    /// It also fixes a second, independent complaint: a full-card `Button`
    /// with `.buttonStyle(.plain)` gives no visual signal it is clickable at
    /// all (`isHovering` used to paper over that with a hand-tinted
    /// surface); a small chip shaped like a `Button` reads as one on sight,
    /// and picks up the pointing-hand cursor AppKit already gives every
    /// `Button` on hover for free.
    ///
    /// The cost is `InstalledAppsSectionTests`' own click-based proof that a
    /// press anywhere on a `.configured` row's dead space — the `Spacer`
    /// between the name and the chip — used to reach `onSelectApp`. That is
    /// no longer true by design, and the test was rewritten rather than
    /// deleted to assert the new, narrower affordance instead: see
    /// `test_only_the_configured_chip_not_the_row_answers_a_click`.
    var body: some View {
        row
            .padding(Metrics.cardPadding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .cardStyle()
    }

    private var row: some View {
        HStack(spacing: Metrics.controlGap) {
            // 20pt: a shade smaller than `DeviceAppDeliveryRow`'s 22 in
            // `DeviceAppsSection` above this one. That list is a handful of
            // configured apps; this one runs to everything on the phone, so a
            // slightly denser row is worth it over the length this section
            // actually reaches.
            AppIcon(name: app.name, size: 20, image: icon)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 8) {
                    Text(app.name)
                        .font(AppFont.bodyMedium)
                        .foregroundStyle(Palette.text)
                        .lineLimit(1)
                    Text(app.version)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textDim)
                        .lineLimit(1)
                    if app.frontmost {
                        TagChip(label: "ON SCREEN")
                    }
                }
                bundleIdLine
            }
            Spacer(minLength: Metrics.controlGap)
            switch state {
            case let .configured(primary, siblings: siblings):
                // The same order `.addable` below draws its own badge/action/
                // dump triad in: badge leads, the row's one primary action
                // (here, the chip that selects `primary`) sits in the
                // middle, Dump trails when this app is still encrypted. A
                // `.configured` row that is *also* encrypted lost this badge
                // and this button entirely before this fix — see `body`'s
                // own doc for why, and for why the fix moves the click onto
                // the chip rather than nesting a second `Button` here.
                if app.encrypted { encryptedBadge }
                configuredChip(primary: primary, siblings: siblings)
                if app.encrypted { dumpButton }
            case let .addable(record):
                // Badge leads, Dump trails, Add sits between them — reads as
                // an accident (`app.encrypted` tested twice with a button in
                // the middle) without saying so: it is deliberate. The badge
                // and the Dump button are the two controls that only apply
                // when `app.encrypted`, so they bracket the row in the same
                // order `.dumpable` below draws them; Add is the one control
                // every `.addable` row has, and it sits where the eye lands
                // reading left to right, not pinned to either edge.
                if app.encrypted { encryptedBadge }
                addButton(record)
                // The Dump stays: a newer version on the phone than the one
                // staged is still worth dumping, even though this app could
                // already be added from the older cached copy.
                if app.encrypted { dumpButton }
            case .dumpable:
                encryptedBadge
                dumpButton
            case .inert:
                EmptyView()
            }
        }
    }

    /// The bundle id, tooltipped with itself.
    ///
    /// Unconditionally now — before Finding 3's fix, a `.configured` row's
    /// *whole card* was one `Button` carrying its own `.help` in `body`, and
    /// SwiftUI resolves the innermost `.help` under the pointer, so this
    /// text's own tooltip would have shadowed that explanation over most of
    /// the area the row was clickable. Nothing plays that role at the row
    /// level any more — `.configured` draws no row-covering `Button` at all,
    /// only `configuredChip` below is one, and it is nowhere near this text
    /// — so there is no longer anything for this tooltip to shadow.
    private var bundleIdLine: some View {
        // Bundle ids don't wrap usefully and aren't prose to bound with
        // `lineLimit` beyond one line — the same treatment `AppSidebarRow`
        // gives this exact field.
        Text(app.bundleId)
            .font(AppFont.monoSmall)
            .foregroundStyle(Palette.textDim)
            .lineLimit(1)
            .truncationMode(.middle)
            .help(app.bundleId)
    }

    /// This app on the phone is FairPlay-encrypted — a decrypt candidate.
    ///
    /// Extracted rather than repeated: `.dumpable` and `.addable` (when
    /// `app.encrypted`) drew the identical glyph, size, colour and
    /// 62-character `.help` string by two call sites staying in sync by
    /// coincidence, not by sharing code. A third arm was the reason to stop
    /// trusting that.
    private var encryptedBadge: some View {
        Symbols.encrypted.image(size: 10)
            .foregroundStyle(Palette.textDim)
            .help("FairPlay-encrypted — a candidate for the decrypt card above")
    }

    /// The row's one interactive control when nothing else is a `Button`:
    /// wraps the "IN YOUR APPS" chip so it — and only it — selects `primary`
    /// on a click. See `body`'s own doc for why this replaced a whole-row
    /// `Button` rather than gaining a second, nested one.
    ///
    /// `.buttonStyle(.plain)` keeps `TagChip`'s own look (a capsule, not
    /// AppKit's default button chrome) while still being a real `Button` —
    /// which is what a synthetic click needs to land on
    /// (`InstalledAppsSectionTests` proves it does), and what gives this
    /// control the pointing-hand cursor on hover for free.
    private func configuredChip(primary: String, siblings: [String]) -> some View {
        Button {
            onSelectApp(primary)
        } label: {
            TagChip(label: "IN YOUR APPS")
        }
        .buttonStyle(.plain)
        .help(Self.configuredHelp(primary: primary, siblings: siblings))
        // The chip's own accessibility element, replacing what VoiceOver
        // would otherwise read off `TagChip`'s bare text ("IN YOUR APPS,
        // button") with what a click actually does. Scoped to just this
        // control now rather than concatenating the whole row into one
        // announcement the way wrapping `row` itself in a `Button` used to
        // — every other `Text` in this row (name, version, bundle id) is
        // its own separate element again, the same as `.addable`/
        // `.dumpable`/`.inert` already read.
        .accessibilityLabel(Self.configuredAccessibilityLabel(app: app, primary: primary))
    }

    /// What hovering the chip says. Named because it always reads "IN YOUR
    /// APPS" regardless of which config key a click actually opens —
    /// `primary` — and, per design spec §4, two config entries can share a
    /// bundle id, so a tooltip that only ever said the primary's name would
    /// make a sibling's presence here invisible.
    private static func configuredHelp(primary: String, siblings: [String]) -> String {
        guard !siblings.isEmpty else {
            return "Already added as \"\(primary)\" — select it to see its delivery state."
        }
        let others = siblings.map { "\"\($0)\"" }.joined(separator: ", ")
        return "Already added as \"\(primary)\" (also configured as \(others)) "
            + "— select it to see its delivery state."
    }

    /// What the chip announces in place of `TagChip`'s own bare text.
    private static func configuredAccessibilityLabel(app: DeviceAppRecord, primary: String) -> String {
        "\(app.name), version \(app.version), already added as \(primary). "
            + "Select to view its delivery state."
    }

    /// `fixedSize` for the same reason `dumpButton` has it: it shares this row
    /// with an app name of unknown length, and a button reading "Add as…" is
    /// worse than a name that truncates.
    private func addButton(_ record: StagedIPARecord) -> some View {
        Button { onAdd(record) } label: { Symbols.add.label("Add as app") }
            .controlSize(.small)
            .fixedSize()
            .help(
                "Add \(app.name) to your apps. \(record.filename) is already "
                + "staged, so it can be built without downloading anything."
            )
    }

    /// Launch this app on the phone and dump it.
    ///
    /// **It deliberately does not name the backend it will use**, where the row
    /// this replaces did ("will be launched and dumped with bagbak"). That name
    /// was `readiness.recommendedBackend` from a `--decrypt-check` probe, and
    /// this section runs none — the probe belongs to the decrypt card above,
    /// which is where the answer is on screen for this phone already. A row that
    /// says the reason out loud and says the *wrong* one is worse than a row
    /// that says nothing, and a second probe per pane to fill in a subtitle is
    /// not worth ~2s per device.
    ///
    /// `fixedSize` so the button keeps its intrinsic width: it shares this row
    /// with an app name of unknown length, and a Dump that reads "Du…" is worse
    /// than a name that truncates. Same bargain the header strikes with the
    /// device name.
    private var dumpButton: some View {
        Button(action: onDump) {
            if isDumpingThis {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text("Dumping…")
                }
            } else {
                // 11pt, matching `.controlSize(.small)` below. See
                // `Mark.frame(forPointSize:)`: the number is the point size of
                // the type beside it, not the frame the mark is drawn in.
                Marks.decrypt.label("Dump", size: 11)
            }
        }
        .controlSize(.small)
        .disabled(isBusy)
        .fixedSize()
        .help("Launch \(app.name) on the phone, dump it, and stage it for a build. "
              + "Progress and the result appear in the decrypt card above.")
    }

    /// Resolved from `iconPath` on each draw, through
    /// `ArtworkStore.image(atPath:)`'s own path-keyed cache — which is what
    /// makes drawing it on each draw affordable.
    ///
    /// This body runs for every row on every re-render of the pane: the rows
    /// are a plain `VStack`, not a `List`, and `DeviceAppTile` carries a
    /// closure (`onDump`) that SwiftUI cannot compare, so nothing is elided.
    /// Uncached that was 67 file reads and 67 fresh `NSImage`s on the main
    /// actor every time the decrypt card started or finished a dump — 8.9 ms of
    /// reads, ~52 ms once the rows were actually drawn. The measurements and
    /// the invalidation rule are recorded on `image(atPath:)`; this row keeps
    /// the naive read because the cache makes it the cheap thing it always
    /// claimed to be.
    private var icon: NSImage? {
        app.iconPath.flatMap(ArtworkStore.image(atPath:))
    }
}
