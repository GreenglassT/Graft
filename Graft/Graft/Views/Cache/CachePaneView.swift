import SwiftUI

/// Everything a dump has staged, and nothing else in this app can see.
///
/// The cache is where every decrypted IPA lands and nothing prunes it — 1.0 GB
/// across 10 files on this machine when the design spec measured it. The device
/// pane already reads the same listing, but it can only join it against *one
/// phone's* installed apps, so three of those ten files have no row anywhere in
/// the app: they are staged for apps that phone does not have installed. This
/// pane is the surface that can see all ten.
///
/// **One row per file, not per bundle id.** `StagedIPAStore` publishes two
/// readings of one listing — `byBundleId`, the file a build would reach for,
/// and `all`, every file — and this pane is the reason `all` exists (see its
/// own doc). Two staged versions of one app are two rows here, because
/// deleting the superseded one is half of what this pane is for, and a pane
/// that collapsed them would hide the very file it is meant to help you get
/// rid of. Which of the two a build would use is not lost in the split: it is
/// the chip, from `preferred`, which the CLI decides.
///
/// **The three actions, and where each half of one lives.** Install to Mac,
/// Add as app and Delete are all pressed here; none of them is *performed*
/// here. This pane owns everything the user reads — which presses ask first
/// (`intent(_:for:)`), what the confirms say, and where a failure lands — and
/// the effects arrive as callbacks, so the pane can be rendered and clicked in
/// a test without a subprocess anywhere near it.
///
/// The two round trips those callbacks reach are `StagedIPAStore.deleteStaged`
/// and `.installToMac`; `ContentView` wires each with a one-line adapter, which
/// is the shape that keeps the transformation this file cannot test down to
/// nothing worth getting wrong.
struct CachePaneView: View {
    /// The cache listing. `ContentView` owns the one instance, shared with the
    /// device pane's join — the cache is not per-phone, so there is nothing to
    /// key a second copy on and nothing to gain from one.
    let staged: StagedIPAStore

    /// Hand this file to PlayCover, and answer with the CLI's complaint when
    /// it could not be handed over — nil when it was.
    ///
    /// **A value comes back because a silent failure here is a dead button.**
    /// Nothing is reported on success and nothing should be: `open` returns on
    /// event delivery, so there is no finished install to narrate and PlayCover
    /// shows its own progress (design spec §2). But PlayCover can be absent,
    /// and then this is the only channel that can say so — `StagedIPAStore`
    /// has no `lastError` by design.
    ///
    /// Required, not defaulted — and the same goes for the two below.
    /// `InstalledAppsSection` defaults its callbacks on purpose, so a caller
    /// that forgets one still renders the list this app actually draws; that
    /// bargain is wrong here, because every control these three feed is
    /// *destructive or navigational* rather than decorative. A defaulted
    /// callback on a live-looking control renders enabled, does nothing when
    /// pressed, and produces no compile error and no failing test to say so.
    ///
    /// **A hazard this codebase has twice closed the same way, not a defect it
    /// has shipped twice** — the distinction is worth keeping straight, since
    /// the two precedents are both cases where the fix landed *before* anyone
    /// could press the dead button. `DeviceDetailView.onSelectApp` was made
    /// required during review (commit `6699614`, whose message puts it in the
    /// conditional: a forgotten wiring "would leave a `.configured` row looking
    /// clickable"), and `DeviceDecryptSection.onAdd` was required from birth,
    /// its own doc calling that a decision rather than a repair. The one
    /// press-it-and-nothing-happens bug this app has actually shipped came from
    /// a different mechanism entirely — a `.disabled()` predicate that named
    /// the wrong flag, `DeviceDecryptSection`'s `.disabled(!ready ||
    /// deviceApps.isDumping)`, which before its fix left the one-click Dump
    /// live during a row dump that the store had already refused. A required
    /// parameter makes forgetting one a build failure at the call site, which
    /// is why the hazard here has stayed hypothetical too.
    let onInstallToMac: @MainActor (StagedIPARecord) async -> String?
    /// Open the prefilled add sheet for this file.
    ///
    /// The one action here with nothing to answer: opening a sheet cannot
    /// fail, and the sheet reports its own add.
    let onAddAsApp: (StagedIPARecord) -> Void
    /// Delete this file and its `.stripped.json` sidecar, forcing past the
    /// CLI's in-use refusal when asked to.
    ///
    /// The `force` parameter is the second half of the two-step design spec §3
    /// describes and `canRetryDroppingExtensions` already uses: the first call
    /// is unforced, and if the CLI objects, the objection is shown *in the
    /// CLI's words* with the one action that answers it — this same call,
    /// forced. Nothing on this side decides whether a delete is safe; the CLI
    /// does, and this pane is never handed the app list it would need to form
    /// a second opinion.
    let onDelete: @MainActor (StagedIPARecord, _ force: Bool) async -> StagedIPAStore.DeleteAnswer

    /// A delete the user has asked for and is still in flight, so the row's
    /// controls can say so rather than looking idle while a subprocess runs.
    @State private var deleting: Set<String> = []
    /// A press that has to be answered before anything happens.
    @State private var pending: Pending?
    /// A delete the CLI refused, and its own sentence for refusing.
    @State private var blocked: Blocked?

    /// - Parameter blocked: a refusal to open on.
    ///
    ///   **A test seam, declared as one** rather than dressed up as a feature —
    ///   the same shape and the same honesty as `ContentView.init`'s
    ///   `selection`. `blocked` is `@State` and private, and the app's one call
    ///   site passes nothing, so in production this parameter does not exist.
    ///
    ///   What it buys is the only route to the second half of the delete
    ///   two-step. Reaching a refusal the way production does means pressing
    ///   the destructive confirm's own button, and an `.alert`'s buttons are
    ///   not reachable from a hosted `NSWindow` — so without this, "Delete
    ///   Anyway" could re-run the delete *unforced*, re-raising the same
    ///   objection forever, and no test in this target would notice. It did
    ///   exactly that, and this is what caught it.
    init(
        staged: StagedIPAStore,
        onInstallToMac: @escaping @MainActor (StagedIPARecord) async -> String?,
        onAddAsApp: @escaping (StagedIPARecord) -> Void,
        onDelete: @escaping @MainActor (StagedIPARecord, Bool) async -> StagedIPAStore.DeleteAnswer,
        blocked: Blocked? = nil
    ) {
        self.staged = staged
        self.onInstallToMac = onInstallToMac
        self.onAddAsApp = onAddAsApp
        self.onDelete = onDelete
        self._blocked = State(initialValue: blocked)
    }
    /// The last failure either verb reported, until dismissed.
    ///
    /// A banner rather than nothing, because both of these actions are pressed
    /// deliberately and both can fail in ways this app cannot fix or hide —
    /// PlayCover absent, a file deleted in Finder between the listing and the
    /// press. `StagedIPAStore` keeps no error channel of its own (see its doc,
    /// which earns that for the *listing* and not for these), so this is where
    /// their failures land.
    @State private var actionError: String?

    /// The three controls a row draws.
    ///
    /// Named rather than left as three separate button actions so the question
    /// "does this press ask first?" is asked in exactly one place — see
    /// `intent(_:for:)`. Three inline closures could each answer it
    /// differently, and the one that must never answer it wrong is Delete.
    enum Control: String, Equatable {
        case installToMac
        case addAsApp
        case delete
    }

    /// What a press does next.
    ///
    /// The sentence rides on `.confirm` rather than being fetched separately,
    /// so pinning the decision pins the copy with it. That is not a stylistic
    /// preference here: this pane has already shipped a distinction pinned at
    /// the enum layer while the words the user reads went unpinned, and
    /// swapping those words left every test green (see `message(for:)`).
    enum Intent: Equatable {
        /// Do it now — nothing to disclose, nothing to lose.
        case perform
        /// Ask first, with this sentence.
        case confirm(String)
    }

    /// Which presses ask first, in one place.
    ///
    ///  - **Install to Mac** asks only for a *stripped* dump, and asks in the
    ///    add sheet's own words (design spec §4). A complete dump installs
    ///    with no prompt at all: the sentence exists to disclose something
    ///    that was lost, and there is nothing lost to disclose.
    ///  - **Add as app** never asks. The sheet it opens is itself the
    ///    confirmation, and nothing has happened until that sheet is
    ///    submitted.
    ///  - **Delete** always asks. Spec §3 narrows the CLI's *objection* to the
    ///    last staged IPA a configured app could build from — deleting the
    ///    superseded copy of a two-version app breaks nothing and must not be
    ///    nagged at with one. It does not narrow the ordinary destructive
    ///    confirm, and this verb calls `unlink`: two versions staged or one,
    ///    the file does not come back.
    static func intent(_ control: Control, for record: StagedIPARecord) -> Intent {
        switch control {
        case .addAsApp:
            return .perform
        case .installToMac:
            guard !record.stripped.isEmpty else { return .perform }
            // `Prefill.stagedLine` verbatim, not a shorter form of it. The row
            // above already carries the shorter form and says why
            // (`rowSummary`); this is the point spec §4 puts the full sentence
            // back, where somebody is about to commit to a partial app.
            return .confirm(
                AddAppSheet.Prefill(staged: record, installedVersion: nil).stagedLine
            )
        case .delete:
            let size = fileSize(record.sizeBytes)
            // **Not the Tweaks pane's wording, and the difference is load
            // bearing.** That confirm says "you can recover it from the Trash"
            // because `--delete-tweak` calls `move_to_trash`. `--delete-staged`
            // calls `unlink`. Reusing the sentence would be a promise the CLI
            // does not keep.
            return .confirm(
                "\(record.filename) — \(size). This deletes the file outright; it does "
                + "not go to the Trash. Getting it back means dumping the app from a "
                + "phone again."
            )
        }
    }

    /// The confirm's heading, and its verb.
    ///
    /// Optional because the title is evaluated on every draw, presented or
    /// not — `.alert(_:isPresented:presenting:)` takes it as a value rather
    /// than a builder over the item.
    ///
    /// The two confirms have to be tellable apart at a glance, and a body
    /// alone does not do that: one is a question about an action you asked
    /// for, the other names a disclosure you did not. The verbs differ for the
    /// same reason — "Install" and "Delete", never a shared "OK".
    static func confirmTitle(for control: Control?) -> String? {
        switch control {
        case .installToMac: return "Install a partial dump?"
        case .delete: return "Delete this staged IPA?"
        // `.addAsApp` never reaches a confirm (see `intent(_:for:)`), so it has
        // no title rather than a placeholder one that could be shown by
        // accident.
        case .addAsApp, nil: return nil
        }
    }

    static func confirmVerb(for control: Control?) -> String? {
        switch control {
        case .installToMac: return "Install"
        case .delete: return "Delete"
        case .addAsApp, nil: return nil
        }
    }

    /// A press waiting on its confirm.
    struct Pending: Identifiable, Equatable {
        let control: Control
        let record: StagedIPARecord
        var id: String { control.rawValue + record.path }
    }

    /// A delete the CLI refused, carrying **its** sentence.
    ///
    /// The only way an objection reaches the screen, and the only thing it can
    /// be built from is a `.inUse` answer — which carries one string and
    /// nothing else. Design spec §3 asks that the GUI not compose a local
    /// equivalent of the CLI's refusal, and this is that requirement made
    /// structural rather than remembered: there is no app list here to compose
    /// one out of.
    ///
    /// The failing initialiser also keeps the objection off the other two
    /// answers. Offering "Delete Anyway" over a permissions error would be an
    /// override for a refusal that never happened, and forcing would not fix
    /// it.
    struct Blocked: Identifiable, Equatable {
        let record: StagedIPARecord
        let objection: String
        var id: String { record.path }

        init?(_ answer: StagedIPAStore.DeleteAnswer, record: StagedIPARecord) {
            guard case let .inUse(objection) = answer else { return nil }
            self.record = record
            self.objection = objection
        }
    }

    /// What the pane has to show, decided in one place.
    ///
    /// A separate `.waiting` case rather than an empty `.rows([])`, because the
    /// two are different claims and only one of them may be printed as "the
    /// cache is empty" — see `listState(of:)`.
    enum ListState: Equatable {
        /// No listing has come back. Not the same as an empty cache.
        case waiting
        /// A listing finished and the cache holds nothing.
        case empty
        /// Every staged file, in the order the CLI emitted them.
        case rows([StagedIPARecord])
    }

    /// **Reads `hasLoaded`, never `all.isEmpty`.**
    ///
    /// An empty cache is a real answer; never having asked is not, and
    /// `all.isEmpty` is true of both. Conflating them is the defect this GUI
    /// has now shipped three times, and here it would put "Nothing staged" on
    /// screen for a pane that has not been told anything — including the one
    /// failure `StagedIPAStore` records as *not* self-healing, a `graft`
    /// too old to know `--staged-ipas`, where the false sentence would stand
    /// until someone re-ran `pipx upgrade`.
    ///
    /// Takes the store rather than the two facts separately, so the reading is
    /// pinned where it happens: a signature of `(hasLoaded:all:)` would leave a
    /// caller free to pass `hasLoaded: !staged.all.isEmpty` and reintroduce the
    /// conflation one line outside anything a test looks at. The store itself
    /// can never hand back the incoherent pair — `all` and `hasLoaded` are
    /// `private(set)` and published together at the end of a successful
    /// `load()`.
    static func listState(of staged: StagedIPAStore) -> ListState {
        guard staged.hasLoaded else { return .waiting }
        return staged.all.isEmpty ? .empty : .rows(staged.all)
    }

    /// The sentence the pane shows in place of a list, and nil when it shows
    /// the list instead.
    ///
    /// A value rather than a `Text` inline in `content`, and the reason is the
    /// same one `listState` exists for. The enum keeps waiting and empty apart,
    /// but the enum is not what the user reads — the sentence is, and with the
    /// sentences buried in the body, swapping the two branches' copy left all
    /// ten tests green. That is the three-times-shipped defect pinned at every
    /// layer except the one it is visible at. The rendered `Text` cannot be
    /// read back (see `ClickHarness.backingTableView(in:)` for what a hosted SwiftUI
    /// view will and will not tell a test), so the sentence has to become a
    /// value before it can be pinned at all.
    static func message(for state: ListState) -> String? {
        switch state {
        case .waiting:
            // Says only what is true of all three ways this state is reached —
            // nothing asked yet, a listing still in flight, and a listing that
            // failed (this store keeps no error channel; see its own doc for
            // why). None of them licenses a word about what the cache holds.
            return "Not read yet — no cache listing has come back."
        case .empty:
            return "Nothing staged. Dumping an app from a phone puts its IPA here."
        case .rows:
            return nil
        }
    }

    /// The count beside the title, and nil when there is no count worth
    /// printing.
    ///
    /// **`.waiting` must not print one**, for exactly the reason `.waiting`
    /// must not print "Nothing staged": "0 files" over a pane that has never
    /// been told anything is the same false claim in smaller type. Unguarding
    /// this — an unconditional `pluralized(staged.all.count, "file")` — also
    /// left all ten tests green before this became a value, which is why it is
    /// one.
    ///
    /// `.empty` prints nothing either, and that is a **divergence from
    /// `InstalledAppsSection`**, which does show "0 installed" for a listing
    /// that finished and found nothing. Deliberate, and it is about what else
    /// is on screen rather than about the count: an empty listing in that
    /// section sits above a one-line "nothing here" note in a pane full of
    /// other cards, where the count is the only thing distinguishing "empty"
    /// from "unread". Here the whole pane is the answer — `message(for:)`
    /// above is already saying "Nothing staged." in body type across the
    /// content area — so a "0 files" beside the title repeats it and adds a
    /// second number to reconcile.
    static func headerCount(for state: ListState) -> String? {
        guard case let .rows(records) = state else { return nil }
        return pluralized(records.count, "file")
    }

    /// The one line under a row's name: what the file is called, how much room
    /// it is taking, and what a strip took out of it.
    ///
    /// **Shorter than `AddAppSheet.Prefill.stagedLine`, and it has to be** —
    /// this is not a decision made for length alone. That sentence drops the
    /// filename and the size entirely the moment anything was stripped
    /// ("Staged, minus 2 app extensions no tool could decrypt."), which is
    /// right for a sheet that has already named the file above it and wrong
    /// for a pane whose whole subject is which files are taking up a gigabyte.
    /// Reusing it would mean a cache pane where the stripped rows are the ones
    /// that stop saying how big they are. It also leads with "Staged", a word
    /// that says nothing on a pane where every row is staged by definition,
    /// and it runs to a full sentence naming capabilities and individual
    /// `.appex` bundles — fine once, in a sheet you opened on purpose, long
    /// down a list of ten.
    ///
    /// So: a shorter form, and `CachePaneTests.test_the_row_never_contradicts_
    /// the_sentence_it_shortens` is what keeps shorter from becoming different.
    /// Both facts this shares with the sheet — the size, through the same
    /// `.file` count style, and the extension count, from `stripped` rather
    /// than `importance` — are tied to the sheet's own output there. The full
    /// sentence is not lost: spec §4 puts it back at the point it matters, in
    /// the confirm before Install to Mac, where `Prefill.stagedLine` is reused
    /// verbatim rather than restated.
    ///
    /// The count style is `.file`, matching every other size in this app
    /// (`DeviceStatStrip`, `TweaksView`, `RepoStore`, `AttachTweaksSheet`,
    /// `IPADropZone`, `BuildEvent.RetentionSweep`, and `Prefill` itself). It is
    /// 1000-based: 128,299,008 bytes prints as **128.3 MB**, measured, where
    /// the 1024-based styles print 122.4 MB for the same file.
    static func rowSummary(for record: StagedIPARecord) -> String {
        let size = fileSize(record.sizeBytes)
        let head = "\(record.filename) — \(size)"
        guard !record.stripped.isEmpty else { return head }
        // Counted off `stripped`, the removed bundle paths themselves — not
        // `importance`, which only holds the subset somebody classified, and
        // is empty on a sidecar that predates that field.
        return head + ", minus \(pluralized(record.stripped.count, "app extension"))"
    }

    /// The chip on the file a build would actually reach for — but only when
    /// that is a choice, i.e. when some other staged file claims the same
    /// bundle id.
    ///
    /// **A marker on every row says nothing, and this shipped saying nothing.**
    /// The paragraph below already made that argument, and the code under it
    /// still marked on `preferred` alone; `preferred` is true for the file a
    /// build would use, and with one file per bundle id that is *every* file.
    /// Every fixture here has two versions of one app — the case the marker
    /// exists for — so no test could see it. The real cache, checked by hand,
    /// has ten unique bundle ids and wore the chip ten times. Sibling-gated
    /// now: the chip appears only where it distinguishes something.
    ///
    /// A `TagChip`, the same marker `InstalledAppsSection` puts on a row the
    /// config already claims ("IN YOUR APPS") — one visual grammar for "this
    /// row is the one", not a second invented here. Returned as a `String?`
    /// rather than drawn inline so the polarity is a value a test can compare:
    /// a marker that appeared on every row would say nothing, and in this
    /// pane's only interesting case — two versions of one app — it is the sole
    /// thing distinguishing the two rows.
    ///
    /// `preferred` is the CLI's word, from `find_in_cache`'s version rule. This
    /// side does not re-derive it, and the reason is written down where it was
    /// measured — `StagedIPAStoreTests.test_an_unpreferred_record_never_
    /// displaces_the_preferred_one`, which notes that `21.16.2` sorts *before*
    /// `21.9.1` as a string even though it is the newer version, so the
    /// filename order a real cache arrives in can disagree with version order.
    /// `StagedIPAStore.load` trusts the flag over arrival order for that
    /// reason; this trusts it for the same one.
    static func preferredLabel(
        for record: StagedIPARecord, among all: [StagedIPARecord]
    ) -> String? {
        guard record.preferred else { return nil }
        // Compared on `path`, not on identity or filename: `path` is the only
        // field unique per row by construction, which is why the `List` keys
        // on it too.
        let hasSibling = all.contains {
            $0.bundleId == record.bundleId && $0.path != record.path
        }
        return hasSibling ? "BUILD USES THIS" : nil
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
                .padding(.horizontal, Metrics.paneGutter).padding(.vertical, 10)
            Divider().background(Palette.line)
            if let actionError {
                ErrorBanner(
                    message: actionError,
                    fit: .fullBleed(gutter: Metrics.paneGutter),
                    onDismiss: { self.actionError = nil }
                )
            }
            if let blocked {
                objectionBar(blocked)
            }
            content
        }
        .background(Palette.bg)
        .navigationTitle("Cache")
        // One alert for both confirms, because there is one question — "this
        // press wants an answer first, here is why" — and `intent(_:for:)` is
        // where which presses ask is decided. Two alerts would be two places to
        // teach about a third control.
        .alert(
            Self.confirmTitle(for: pending?.control) ?? "",
            isPresented: $pending.isPresent(),
            presenting: pending
        ) { press in
            Button("Cancel", role: .cancel) {}
            // Passed the value rather than reading `pending` back: dismissing
            // writes the `isPresented` binding, which clears it, so a closure
            // that re-read it could find nil and silently do nothing. The Stop
            // confirmation records the same trap.
            Button(
                Self.confirmVerb(for: press.control) ?? "",
                role: press.control == .delete ? .destructive : nil
            ) {
                perform(press.control, press.record)
            }
        } message: { press in
            if case let .confirm(sentence) = Self.intent(press.control, for: press.record) {
                Text(sentence)
            }
        }
        // Bare, not `.task(id:)`. The cache is not per-phone and not per-app —
        // there is one of it, so there is no identity for a key to name, and
        // the two panes that key their loads (`AppDetailView`, `DeviceDetailView`)
        // do it because SwiftUI reuses one view instance across a switch between
        // two apps or two phones. Nothing switches this pane between two caches.
        //
        // It still re-runs on every entry, which is the behaviour that matters
        // here: `ContentView`'s detail `switch` builds this view only while
        // `.section(.cache)` is selected, so leaving the pane tears it down and
        // coming back makes a new one. A dump from the device pane, a file
        // deleted in Finder, or a build that consumed a staged IPA all change
        // this listing while the user is somewhere else, and none of them tell
        // us. One local subprocess at ~0.25s per visit is the cheaper mistake —
        // the same trade `DeviceDetailView` states for the same call.
        //
        // `load()` does not throw and this store keeps no error channel (see
        // `StagedIPAStore`), so there is nothing here to catch or report; a
        // failed listing leaves the pane in `.waiting`, whose sentence is
        // written to cover exactly that.
        .task { await staged.load() }
    }

    /// The title, and the count when there is one — see `headerCount(for:)`,
    /// which decides that and is where the rule is pinned.
    private var header: some View {
        HStack {
            SectionTitle(text: "Staged IPAs")
            if let count = Self.headerCount(for: Self.listState(of: staged)) {
                Text(count)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
                    .lineLimit(1)
            }
            Spacer()
        }
    }

    @ViewBuilder
    private var content: some View {
        let state = Self.listState(of: staged)
        if let sentence = Self.message(for: state) {
            messageView(sentence)
        } else if case let .rows(records) = state {
            // Keyed by `path`, which is unique per file where `bundleId` is
            // not. The symptom of getting this wrong is *not* a missing row —
            // measured: `id: \.bundleId` on the two-version fixture still
            // renders two rows and leaves every test in `CachePaneTests`
            // green, because SwiftUI draws what the collection holds. What
            // breaks is identity across an update — diffing, animation, and
            // any future selection — where two rows claiming one id is
            // undefined. Nothing here catches that, which is worth knowing
            // before reading the suite's silence as coverage.
            List(records, id: \.path) { record in
                CacheRow(
                    record: record,
                    preferredLabel: Self.preferredLabel(for: record, among: records),
                    busy: deleting.contains(record.path),
                    onInstall: { press(.installToMac, $0) },
                    onAdd: { press(.addAsApp, $0) },
                    onDelete: { press(.delete, $0) }
                )
            }
            .listStyle(.inset)
            .scrollContentBackground(.hidden)
        }
    }

    /// A control was pressed. Ask first, or do it.
    ///
    /// The only route from a row's button to an effect, so the answer to
    /// "which presses ask?" cannot be given twice — see `intent(_:for:)`.
    private func press(_ control: Control, _ record: StagedIPARecord) {
        switch Self.intent(control, for: record) {
        case .perform:
            perform(control, record)
        case .confirm:
            pending = Pending(control: control, record: record)
        }
    }

    /// The effect itself, reached either straight from `press` or from the
    /// confirm's own button.
    private func perform(_ control: Control, _ record: StagedIPARecord) {
        switch control {
        case .installToMac:
            actionError = nil
            // Nothing is shown when this succeeds, and that is the honest
            // ceiling: `open` returns on delivery, so a "done" here would
            // claim an install neither side can see.
            Task { actionError = await onInstallToMac(record) }
        case .addAsApp:
            onAddAsApp(record)
        case .delete:
            Task { await runDelete(record, force: false) }
        }
    }

    /// Run the delete and route what the CLI answered.
    ///
    /// A refusal becomes the objection alert — in the CLI's words, which is
    /// all `Blocked` can be built from. Anything else that failed becomes the
    /// banner, because forcing would not fix it. A delete that landed shows
    /// nothing: the store re-read the listing, so the row is already gone, and
    /// that *is* the confirmation.
    private func runDelete(_ record: StagedIPARecord, force: Bool) async {
        actionError = nil
        // Cleared before the run, not after it. This is also the *forced*
        // path's entry point, and a strip left standing over a file that has
        // just been deleted would go on offering to delete it — the CLI's
        // second answer would be "no staged IPA named …".
        blocked = nil
        deleting.insert(record.path)
        let answer = await onDelete(record, force)
        deleting.remove(record.path)
        if let refusal = Blocked(answer, record: record) {
            blocked = refusal
            return
        }
        if case let .failed(message) = answer { actionError = message }
    }

    /// The CLI's objection, and the one action that answers it.
    ///
    /// **A strip, not a second alert, and both halves of that are deliberate.**
    ///
    /// It is the shape `canRetryDroppingExtensions` already uses, which design
    /// spec §3 names as the model for this exact two-step: the CLI's own
    /// sentence beside a button that undoes the objection, sitting in the pane
    /// rather than over it. The decrypt card puts its "Drop them anyway and
    /// retry" there for the same reason — the refusal is not a warning, it is
    /// an offer, and the warning already happened one dialog ago.
    ///
    /// It also removes a presentation race this had while it was an alert.
    /// The confirm's own Delete button starts a `Task`, and that Task can set
    /// `blocked` while the first alert is still dismissing — a second `.alert`
    /// raised from inside the first one's action on the same view is a
    /// well-known way to have it silently dropped. A strip cannot be dropped.
    ///
    /// And it is what makes the two-step reachable by a test at all: an
    /// alert's buttons are not clickable from a hosted window, so
    /// `Delete Anyway` re-running the delete *unforced* — an objection that
    /// re-raises itself forever — survived every case in `CachePaneTests` while
    /// this was an alert. Measured, not supposed.
    ///
    /// Fixed height so a test can find it: it sits directly above the `List`,
    /// so the row band and this band are a known distance apart. Nothing else
    /// depends on the number.
    private func objectionBar(_ refusal: Blocked) -> some View {
        HStack(spacing: 8) {
            Symbols.attention.image
                .foregroundStyle(Palette.warn)
            // The CLI's sentence, verbatim and nothing added. **Not
            // `.textSelection(.enabled)`**, which `ErrorBanner` is: selectable
            // text takes a synthetic click into a nested `NSTextView`
            // tracking loop that does not come back (`DeviceDecryptSection
            // Tests`), and this strip is the one thing in this pane a test
            // has to click through.
            Text(refusal.objection)
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.text)
                .lineLimit(2)
                .truncationMode(.middle)
                .help(refusal.objection)
            Spacer(minLength: 8)
            Button("Delete Anyway") {
                // Cleared here, synchronously, and *not* left to `runDelete`
                // to do — that runs inside a `Task`, so its `blocked = nil`
                // does not land until the next actor hop, and every click
                // arriving before that hop starts another forced delete of
                // the same file. A double-click ran two: the second answers
                // "no staged IPA named …" and surfaces as an error over a
                // delete that in fact succeeded. Clearing it here takes the
                // button off screen on the first press.
                blocked = nil
                Task { await runDelete(refusal.record, force: true) }
            }
            .frame(width: Self.objectionButtonWidth)
            Button("Keep It") { blocked = nil }
                .frame(width: Self.objectionButtonWidth)
        }
        .padding(.horizontal, Metrics.paneGutter)
        .frame(height: Self.objectionBarHeight)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.surface2.opacity(0.5))
    }

    /// The strip's height and its two buttons' width — fixed so a synthetic
    /// click can be aimed at them without sweeping. See `objectionBar`.
    static let objectionBarHeight: CGFloat = 40
    static let objectionButtonWidth: CGFloat = 110

    private func messageView(_ text: String) -> some View {
        Text(text)
            .font(AppFont.body)
            .foregroundStyle(Palette.textDim)
            .padding(Metrics.paneGutter)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

}

/// One staged file: what it is, and the three things that can be done to it.
///
/// **Not `private`, and not nested**, for the reason `DeviceAppTile` is
/// neither: a row hosted on its own is the only shape in this app in which a
/// synthetic click has been measured to land where the arithmetic says it
/// does. Inside the pane's `List` a cell reports nothing about its contents
/// (`ClickHarness.backingTableView(in:)`) and a full-pane click sweep is the
/// thing that once hung for minutes at near-zero CPU
/// (`DeviceDetailAddWiringTests`' file doc). So the three controls are pinned
/// here, and the pane's one-line rewrap of them is matched by reading.
///
/// **The controls are always drawn, never revealed on hover.** A hover-only
/// control on a `List` row is invisible to every one of those click tests and
/// invisible to a user who is not already pointing at the row — and one of
/// these three deletes a file.
struct CacheRow: View {
    /// The side of one control's square, and the gap between two of them.
    ///
    /// Public constants rather than literals in the `HStack` because
    /// `CachePaneTests` aims at their centres: a row that changed its control
    /// size stays targetable instead of quietly moving out from under three
    /// hardcoded coordinates. The constants say where to click; what the tests
    /// prove is which callback answers there, which is not a constant.
    static let controlSize: CGFloat = 24
    static let controlSpacing: CGFloat = 6

    let record: StagedIPARecord
    /// The "BUILD USES THIS" chip, or nil when this row does not need one.
    ///
    /// Decided by the pane, not here, because it is not a property of this
    /// record — it depends on whether any *other* staged file claims the same
    /// bundle id, which only the whole listing knows. See
    /// `CachePaneView.preferredLabel(for:among:)`.
    var preferredLabel: String?
    /// A delete for this file is in flight. The controls go inert rather than
    /// disappearing — a row that lost its buttons mid-press would read as the
    /// delete having already happened.
    var busy: Bool = false
    let onInstall: (StagedIPARecord) -> Void
    let onAdd: (StagedIPARecord) -> Void
    let onDelete: (StagedIPARecord) -> Void

    var body: some View {
        // Bound once: the summary is drawn and then repeated as its own
        // tooltip, and there is no reason to build the string twice per row
        // per frame. The formatter behind it is no longer the cost — `fileSize`
        // caches one for the whole app — but the string still is.
        let summary = CachePaneView.rowSummary(for: record)
        return HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 8) {
                    Text(record.displayName)
                        .font(AppFont.bodyMedium)
                        .foregroundStyle(Palette.text)
                        .lineLimit(1)
                    Text(record.version)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textDim)
                        .lineLimit(1)
                    if let preferredLabel {
                        TagChip(label: preferredLabel)
                            .help(
                                "This bundle id has more than one staged file. "
                                + "A build would use this one."
                            )
                    }
                }
                // Bundle ids don't wrap usefully and aren't prose — the same
                // treatment `AppSidebarRow` and `DeviceAppTile` give this field,
                // tooltipped with themselves so a middle-truncated one is still
                // readable.
                Text(record.bundleId)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .help(record.bundleId)
                Text(summary)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textMuted)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .help(summary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            controls
        }
        .padding(.vertical, 2)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// Hand it over, add it, delete it — in that order, left to right.
    ///
    /// The destructive one is trailing-most, which is where this app already
    /// puts a row's primary control (`DeviceAppTile`'s Dump), and the two that
    /// are not destructive sit inboard of it. The order is pinned: no case in
    /// `CachePaneTests`' click section passes if two of these swap.
    private var controls: some View {
        HStack(spacing: Self.controlSpacing) {
            control(Symbols.installToMac, help: "Install \(record.displayName) on this Mac with PlayCover.") {
                onInstall(record)
            }
            control(Symbols.add, help: "Add \(record.displayName) to your apps, prefilled from this dump.") {
                onAdd(record)
            }
            control(Symbols.delete, help: "Delete \(record.filename) from the cache.") {
                onDelete(record)
            }
        }
        .disabled(busy)
        .opacity(busy ? 0.4 : 1)
    }

    /// The shape every one of the three takes — the same
    /// `.buttonStyle(.plain)` + fixed frame + `.contentShape(Rectangle())`
    /// idiom `TweaksView`'s toolbar Refresh uses, which is what makes the
    /// whole square answer a click rather than only the inked pixels of the
    /// glyph.
    private func control(
        _ symbol: Symbol, help: String, action: @escaping () -> Void
    ) -> some View {
        Button(action: action) { symbol.image }
            .buttonStyle(.plain)
            .foregroundStyle(Palette.textDim)
            .frame(width: Self.controlSize, height: Self.controlSize)
            .contentShape(Rectangle())
            .help(help)
    }
}
