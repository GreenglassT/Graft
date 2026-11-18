import SwiftUI

struct AddAppSheet: View {
    @Bindable var store: ConfigStore
    /// Called with the new app's YAML slug right before the sheet dismisses,
    /// so the caller can select the new row in the sidebar.
    var onAdded: ((String) -> Void)? = nil
    /// What a staged dump already told the device pane, if this sheet was
    /// opened from a row that had one. nil is the ordinary "+ Add App"
    /// button's case; a present value came from `.addable`/`.dumpable` or the
    /// decrypt card. Wiring that button is Task 9 — this only says what the
    /// sheet shows once it has one.
    var prefill: Prefill? = nil
    @Environment(\.dismiss) private var dismiss

    enum Mode: String, CaseIterable, Identifiable {
        case url = "From App Store URL"
        case manual = "Manual"
        var id: String { rawValue }
    }

    /// Everything an add from the device pane already knows, so nothing is
    /// retyped.
    ///
    /// The copy lives here rather than in the body for the reason
    /// `DeviceAppsSection.statusLine(of:in:)` does: these are the sentences
    /// the user reads before deciding, and they belong somewhere a test can
    /// read them too.
    struct Prefill: Equatable, Identifiable {
        let staged: StagedIPARecord
        /// What the phone has installed, when a device listing is behind this.
        /// nil from the decrypt card after a frontmost dump — there is no
        /// listing there, so there is nothing to compare and nothing is said.
        let installedVersion: String?
        /// The phone's own name for this app, when a device listing is behind
        /// this add — `DeviceAppRecord.name`, straight off `--device-apps`,
        /// never `ipa_inspect.py`'s `"Unknown"` fallback the way
        /// `staged.displayName` can be (that comes from the *IPA's* plist,
        /// which a hand-copied or oddly-packaged file can genuinely lack).
        /// nil for the same reason `installedVersion` is nil from the decrypt
        /// card: that route stages what a frontmost dump just found on the
        /// phone, with no listing behind it to have a name from at all.
        ///
        /// Preferred over `staged.displayName` in `.onAppear` below rather
        /// than left for the field to start blank on a real name, on the
        /// same reasoning `installedVersion` already applies to a version
        /// disagreement: this app is genuinely on the phone, so its own name
        /// is worth prefilling even when the dump's own plist has nothing to
        /// say.
        ///
        /// Defaulted to `nil`, unlike `installedVersion` above — deliberately
        /// the one field on this type that is. Every one of this file's own
        /// fixtures, and every other test's, already builds a `Prefill`
        /// without an opinion on the phone's app name; a bare
        /// `let installedName: String?` would have forced `installedName:
        /// nil` onto ~30 call sites testing `stagedLine`/`versionLine`/click
        /// behaviour that have nothing to do with this field, for the same
        /// answer they would have gotten anyway. `installedVersion` earns
        /// its own required-ness by feeding `versionLine`, a sentence most
        /// of those same fixtures *do* mean to pin one way or the other; this
        /// field only ever changes what one `.onAppear` assignment prefers,
        /// and both of this type's two real construction sites
        /// (`DeviceDetailView.swift`) pass it explicitly regardless of the
        /// default, each with its own comment on why.
        ///
        /// `var`, not `let` like every other field here: Swift's synthesized
        /// memberwise initializer drops a `let` that carries a default value
        /// from its parameter list entirely (there being no way to reassign
        /// a `let` after the fact, the compiler treats the default as the
        /// whole story) — which is *not* "an optional parameter", it is no
        /// parameter at all, and both of `DeviceDetailView.swift`'s call
        /// sites that pass `installedName:` explicitly failed to compile
        /// under `let`. `var` is what actually gets the default-valued,
        /// still-overridable parameter every other field on this type gets
        /// for free from being required.
        var installedName: String? = nil

        /// `Identifiable` because `.sheet(item:)` — the API the pane
        /// presents this through — requires its item type to be. `id` is
        /// `staged.path`, which stays fixed across a restage of the same
        /// file: SwiftUI would not see two `Prefill`s for the same path as
        /// different items, so nothing here forces a rebuild on a restage —
        /// this is only what `.sheet(item:)` needs to know a `Prefill` is
        /// present at all.
        var id: String { staged.path }

        var bundleId: String { staged.bundleId }
        var displayName: String { staged.displayName }

        /// The YAML key, derived and then *shown*: `add_app` dedups to
        /// `name_2` on collision, and seeing that before it is written is why
        /// this flow is a sheet rather than a one-press action.
        ///
        /// Mirrors `slugify_name` in `config.py` (lowercase, non-`[a-z0-9]`
        /// runs collapsed to one `_`, leading/trailing `_` trimmed) so the
        /// name shown here is the one `add_app` would actually choose absent
        /// a collision — but falls back to the bundle id rather than
        /// `config.py`'s `"app"`: a suggestion of literally "app" would look
        /// like a bug, where the bundle id is still a real, useful key.
        ///
        /// The fallback is itself run through `slugify`. A display name with
        /// no ASCII alphanumerics — "微博", "카카오톡" — is an ordinary app, not
        /// a missing field (`ipa_inspect.py` only falls back to `"Unknown"`
        /// when the plist key itself is absent), and its slug comes out
        /// empty. Showing the raw bundle id in that case — `com.example.social` —
        /// would be a display bug against this property's own promise: the
        /// key is derived and then *shown*, and `_do_add_app_manual`
        /// re-slugifies whatever `--name` it receives, so submitting the
        /// unslugified text would silently write `com_example_social` — not the
        /// string the sheet showed.
        var suggestedName: String {
            let slug = Self.slugify(displayName)
            return slug.isEmpty ? Self.slugify(bundleId) : slug
        }

        private static func slugify(_ text: String) -> String {
            var slug = ""
            var lastWasSeparator = false
            for ch in text.lowercased() {
                if let ascii = ch.asciiValue,
                    (0x61...0x7A).contains(ascii) || (0x30...0x39).contains(ascii) {
                    slug.append(ch)
                    lastWasSeparator = false
                } else if !lastWasSeparator {
                    slug.append("_")
                    lastWasSeparator = true
                }
            }
            while slug.hasPrefix("_") { slug.removeFirst() }
            while slug.hasSuffix("_") { slug.removeLast() }
            return slug
        }

        /// Removed bundle paths marked `"essential"`, by basename and sorted
        /// — the one list `stagedLine` and the hover text in `stagedInfo`
        /// both draw from, so the two can never name a different set.
        ///
        /// Joined against `stripped` rather than read straight off
        /// `importance`: the two agree today only because `cli.py:3731`
        /// builds `importance` over the same removed set, and `StrippedNote`'s
        /// own docstring describes the reader as joining `importance` against
        /// `removed`. Filtering `stripped` instead of trusting `importance`
        /// alone means a hand-edited sidecar can't make this sheet name an
        /// extension that was never actually removed.
        private var essentialNames: [String] {
            staged.stripped
                .filter { staged.importance[$0] == "essential" }
                .map { ($0 as NSString).lastPathComponent }
                .sorted()
        }

        /// What is already in the cache, and what it cost to get there.
        ///
        /// Precedence, in order — each one wins outright over the ones below
        /// it, not additively:
        ///  1. Nothing stripped: the plain "will use this" line.
        ///  2. Any `"essential"` entry in `importance`: forcing past the
        ///     refusal that protects an extension this app is arguably *for*
        ///     is a different thing to have done than losing one no tool
        ///     could reach, so it is named rather than folded into the plain
        ///     count — but naming it does not drop the count. A dump that
        ///     lost 5 extensions, one of them essential, says both facts:
        ///     `Staged, minus 5 app extensions no tool could decrypt —
        ///     including one this app is arguably for: Tunnel.appex.` When
        ///     the total is exactly 1 the two counts are the same number, so
        ///     that longer form would read as "minus 1 … including one …";
        ///     the single-extension case collapses to the shorter sentence
        ///     instead, which says everything the longer one would. Either
        ///     way this preempts the `lost` sentence below even when `lost`
        ///     is also present, because "the VPN will not work" undersells a
        ///     choice the user made on purpose.
        ///  3. `lost` non-empty (recorded and something was named).
        ///  4. Otherwise: the neutral count, which is also what an
        ///     *unrecorded* `lost` (nil) renders as — see `StagedIPARecord.
        ///     lost`'s doc. Only a recorded, empty `lost` may ever license
        ///     "nothing important was lost", and that is not a sentence this
        ///     ever prints; the neutral count is the honest ceiling.
        ///
        /// Naming here is partial, not exhaustive: branch 2 names only the
        /// essential subset, and branch 3 names capabilities — "Siri and
        /// Shortcuts" — which are not filenames at all. `strippedFilenamesFor
        /// Hover` is what covers the rest; see its own doc for the rule.
        var stagedLine: String {
            guard !staged.stripped.isEmpty else {
                return "Staged: \(staged.filename) — \(Self.sizeString(staged.sizeBytes)). "
                    + "The build will use this."
            }
            let names = essentialNames
            if !names.isEmpty {
                let joined = names.joined(separator: ", ")
                guard staged.stripped.count > 1 else {
                    return "Staged, minus an extension this app is arguably for: \(joined)."
                }
                let essentialPhrase = names.count == 1 ? "one" : "\(names.count)"
                return "Staged, minus \(staged.stripped.count) app extensions no tool "
                    + "could decrypt — including \(essentialPhrase) this app is arguably "
                    + "for: \(joined)."
            }
            let plural = staged.stripped.count != 1
            let noun = plural ? "app extensions" : "app extension"
            let count = "Staged, minus \(staged.stripped.count) \(noun) no tool could decrypt"
            // The join mirrors the CLI's own — `' and '.join(lost)` in
            // cli.py's `_warn` about stripped extensions — so the sentence
            // reads identically whether it came from a dump just now or a
            // sidecar read back a week later.
            if let lost = staged.lost, !lost.isEmpty {
                return count + " — \(lost.joined(separator: " and ")) will not work in the "
                    + "built app."
            }
            return count + "."
        }

        /// The filenames `stagedLine` didn't say, for `stagedInfo` to put on
        /// hover.
        ///
        /// The rule is `stripped.count > essentialNames.count` — not "nothing
        /// essential and nothing lost". A first version of this used the
        /// narrower rule and had a hole it opened itself: the mixed branch
        /// above names one of five stripped extensions ("including one this
        /// app is arguably for: Tunnel.appex"), and the narrower rule read
        /// that as "something was named" and hid all five, the other four
        /// reachable from nowhere. And the `lost` branch names a *capability*
        /// — "Siri and Shortcuts" — never a filename, so it never satisfied
        /// this hover either; `cli.py:3849-3854` prints both units, filenames
        /// and capabilities, in the same warning for the same reason — they
        /// are different facts, and naming one is not naming the other. Only
        /// the two fully-covered branches — nothing stripped, and every
        /// stripped path individually named as essential — end up with
        /// nothing left to show.
        var strippedFilenamesForHover: String? {
            guard staged.stripped.count > essentialNames.count else { return nil }
            return staged.stripped
                .map { ($0 as NSString).lastPathComponent }
                .sorted()
                .joined(separator: ", ")
        }

        // `.file`, matching every other size in this app (`IPADropZone`,
        // further below in this same sheet, plus `DeviceStatStrip`,
        // `TweaksView`, `RepoStore`, `AttachTweaksSheet`,
        // `BuildEvent.RetentionSweep`) — the 1000-based units Apple quotes
        // storage in, not the 1024-based ones a plain byte-count division
        // would give.
        private static func sizeString(_ bytes: Int) -> String {
            fileSize(bytes)
        }

        /// Said, not resolved. Adding an app whose dump is older than what the
        /// phone runs is still correct — the build uses what is staged — so
        /// this states the fact and offers nothing.
        var versionLine: String? {
            guard let installedVersion, installedVersion != staged.version else { return nil }
            return "This phone runs \(installedVersion); the staged dump is \(staged.version)."
        }

        /// What `.onAppear` below prefills `manualDisplayName` with — pulled
        /// out as its own pure property, the same treatment `suggestedName`
        /// and `stagedLine` already get, so the guard this exists for
        /// (Finding 4) is something a test can call directly rather than
        /// only observe through a rendered `TextField`.
        ///
        /// `installedName` wins outright when there is one: it comes from
        /// the phone's own listing and is never `ipa_inspect.py`'s
        /// `"Unknown"` sentinel, where `displayName` — the IPA's own plist —
        /// can genuinely be. Only when there is no phone-sourced name at all
        /// does this fall back to `displayName`, and only past the same
        /// `"Unknown"` check `title` above already applies: submitting that
        /// sentinel as `--display-name` is what silently renamed the built
        /// app "Unknown" on the home screen before this fix, and an empty
        /// string here just leaves the field for the user to fill in
        /// themselves, same as an app the sheet was never prefilled for.
        var prefilledDisplayName: String {
            installedName ?? (displayName == "Unknown" ? "" : displayName)
        }
    }

    @State private var mode: Mode = .url
    @State private var urlDelivery: DeliveryMode = AddAppSheet.defaultDelivery
    @State private var appStoreURL: String = ""
    @State private var manualName: String = ""
    @State private var manualBundleId: String = ""
    @State private var manualDisplayName: String = ""
    @State private var manualDelivery: DeliveryMode = AddAppSheet.defaultDelivery
    @State private var stagedIPAURL: URL?
    @State private var stagedIPASize: Int = 0
    @State private var isWorking: Bool = false
    @State private var errorMessage: String?
    /// The last delivery mode an add actually used, so the next one starts
    /// where the last one left off. Shared with the unprefilled sheet: it is a
    /// fact about this bench (a jailbroken phone runs LiveContainer; a stock
    /// one is sideloaded), not about which surface asked.
    @AppStorage("lastDeliveryMode") private var lastDelivery: DeliveryMode = AddAppSheet.defaultDelivery

    /// Where the picker starts before any add has gone through: LiveContainer,
    /// the mode the README recommends. It was Sideload, so the first app a new
    /// user added went the other way from the instructions they had just read.
    static let defaultDelivery: DeliveryMode = .livecontainer

    /// The delivery an add actually sends, for both add paths: on a free
    /// Apple ID always LiveContainer, whatever the picker or `lastDeliveryMode`
    /// holds, because the CLI refuses `--delivery sideload` there; otherwise
    /// what was chosen.
    ///
    /// Applied when the picker is drawn *and* when the add is sent, not only
    /// to seed the picker. A `lastDeliveryMode` of sideload remembered from a
    /// paid account outlives a switch to a free one, and seeding alone would
    /// send it from a sheet opened before the first `--list` had said which
    /// account this is.
    static func delivery(chosen: DeliveryMode, account: AccountMode?) -> DeliveryMode {
        account.isFree ? .livecontainer : chosen
    }

    var body: some View {
        // Let the sheet size to content; the previous fixed height clipped
        // multi-line error banners (e.g. an ITunesError with a `hint:` second
        // line) right in URL mode where there was no room to grow.
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider().background(Palette.line)
            content
                .padding(20)
            if let errorMessage {
                ErrorBanner(
                    message: errorMessage,
                    fit: .fullBleed(gutter: 20),
                    onDismiss: { self.errorMessage = nil }
                )
            }
            footer
        }
        .frame(width: 480)
        .background(Palette.bg)
        // `@State` initial values are only honoured on first construction, so
        // an `init`-based fill was the alternative to this. It would have
        // worked just as well: `dismiss()` sets the presenting item back to
        // nil, which tears this sheet's content down, so the next
        // presentation — same `prefill` or a different one — is a fresh
        // `AddAppSheet`, not a reused one. The one real difference is smaller
        // than that: `mode` starts `.url`, so a prefilled sheet's first body
        // evaluation would briefly build the URL form before `.onAppear` runs
        // and switches it to `.manual` — a one-frame hitch an `init`-based
        // fill would not have had.
        .onAppear {
            guard let prefill else {
                urlDelivery = lastDelivery
                manualDelivery = lastDelivery
                return
            }
            mode = .manual
            manualBundleId = prefill.bundleId
            manualName = prefill.suggestedName
            // This assignment used to be `prefill.displayName` unguarded —
            // "Unknown" (`ipa_inspect.py`'s own sentinel for a plist with
            // neither `CFBundleDisplayName` nor `CFBundleName`) flowed
            // straight through into `add_app_manual`'s `--display-name`,
            // into `config.yaml`, and out through `cyan_runner.py`'s `-n
            // Unknown` — silently renaming the built app "Unknown" on the
            // home screen. See `Prefill.prefilledDisplayName`'s own doc for
            // what replaced it and why.
            manualDisplayName = prefill.prefilledDisplayName
            manualDelivery = lastDelivery
        }
    }

    // MARK: - Header

    private var header: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title)
                .font(AppFont.title).foregroundStyle(Palette.text)
            // Hidden when prefilled: a prefilled add already has its bundle
            // id and version from the cache, so an iTunes lookup would ask a
            // question already answered — and a picker left on screen could
            // silently throw the prefill away by switching back to `.url`,
            // which is worse than no picker at all.
            if prefill == nil {
                Picker("", selection: $mode) {
                    ForEach(Mode.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
            }
        }
        .padding(.horizontal, 20).padding(.top, 20).padding(.bottom, 14)
    }

    private var title: String {
        guard let prefill else { return "Add App" }
        // `ipa_inspect.py`'s own fallback when an IPA's plist has neither
        // `CFBundleDisplayName` nor `CFBundleName` — "Add Unknown" would read
        // as a bug in this sheet rather than as "this app has no name to show".
        guard prefill.displayName != "Unknown" else { return "Add app from this phone" }
        return "Add \(prefill.displayName)"
    }

    // MARK: - Content (mode-dependent)

    @ViewBuilder
    private var content: some View {
        switch mode {
        case .url:
            urlForm
        case .manual:
            manualForm
        }
    }

    private var urlForm: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Paste an App Store URL")
                .font(AppFont.bodyMedium).foregroundStyle(Palette.text)
            SearchField(
                placeholder: "https://apps.apple.com/us/app/.../id1234567890",
                text: $appStoreURL,
                onSubmit: submit
            )
            Text("Graft will look up the app's bundle ID and version via iTunes.")
                .font(AppFont.monoSmall).foregroundStyle(Palette.textDim)
            deliveryPicker(selection: $urlDelivery)
        }
    }

    private var manualForm: some View {
        VStack(alignment: .leading, spacing: 12) {
            field("Name (shortname)", text: $manualName, placeholder: "example")
            field("Bundle ID", text: $manualBundleId, placeholder: "com.example.app")
            field("Display Name (optional)", text: $manualDisplayName, placeholder: "Example")
            if let prefill {
                // A drop zone here would ask the user to answer a question
                // the cache already answered — this IPA is already staged, so
                // dropping one on top of it would be redundant at best and
                // contradictory at worst. `stagedLine`/`versionLine` say what
                // is already known instead.
                stagedInfo(prefill)
            } else {
                IPADropZone(
                    title: "Source IPA (optional)",
                    cached: stagedIPAURL.map { url in
                        CachedIPARecord(
                            exists: true,
                            app: "",
                            bundleId: "",
                            version: "",
                            filename: url.lastPathComponent,
                            path: url.path,
                            sizeBytes: stagedIPASize
                        )
                    },
                    isWorking: false,
                    onPick: { url in
                        stagedIPAURL = url
                        // Stat once on pick so the SwiftUI body doesn't hit the
                        // filesystem on every keystroke in the form.
                        stagedIPASize = (try? FileManager.default
                            .attributesOfItem(atPath: url.path)[.size] as? Int) ?? 0
                    }
                )
            }
            deliveryPicker(selection: $manualDelivery)
        }
    }

    /// The two lines a prefilled add shows in place of the drop zone: what is
    /// staged and what it cost (`Palette.warn` when something was stripped,
    /// `Palette.textDim` for a complete dump), then the version disagreement
    /// beneath it when there is one to state.
    private func stagedInfo(_ prefill: Prefill) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            stagedLineText(prefill)
            if let versionLine = prefill.versionLine {
                // A fixed sentence this view composes, not CLI prose — short
                // and bounded by construction, so `.lineLimit(1)` costs
                // nothing today. Kept anyway, matching every other line here:
                // nothing in this sheet gets to demand unbounded height.
                Text(versionLine)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
                    .lineLimit(1)
            }
        }
    }

    /// Split out from `stagedInfo` only so the conditional `.help(...)` —
    /// shown exactly when `stagedLine` didn't already name what was removed,
    /// per `Prefill.strippedFilenamesForHover` — has somewhere to attach
    /// without falling back to a bare `?? ""`, which would leave every other
    /// branch drawing an empty tooltip bubble on hover.
    @ViewBuilder
    private func stagedLineText(_ prefill: Prefill) -> some View {
        // Vertical-only `fixedSize`, on the longest string this sheet draws
        // (the "Siri and Shortcuts" line is ~104 characters at
        // `AppFont.monoSmall` inside this sheet's ~440pt content width, and
        // does need to wrap).
        //
        // Not a guard against the house's one recorded blanking incident —
        // the other direction. `AppDetailLayoutTests.swift`'s own doc: that
        // pane went blank because `.fixedSize(horizontal: false, vertical:
        // true)` "pins a `Text` to its ideal height for the width it is
        // proposed, and where that proposed width comes out as nothing, a
        // long sentence resolves to one character per line and hundreds of
        // points of height" — the modifier *caused* it, given a degenerate
        // proposed width, not prevented it. `DeviceDetailView.swift` names
        // `DeviceDecryptSection` as "the card that caused it", and that
        // card's own `dumpNotes` comment makes no blanking claim at all — it
        // only says the vertical-only form "is what makes wrapping
        // affordable" inside its `ScrollView`, which absorbs the height.
        //
        // What actually rules that failure out here: `body` pins `.frame
        // (width: 480)` at the top of this view's tree, so the width
        // proposed down to this `Text` is bounded by construction and cannot
        // degenerate toward zero the way an ambient, negotiated width — a
        // `NavigationSplitView` detail column's, in the recorded incident —
        // evidently could. The precondition for that bug cannot arise here,
        // independent of whether this modifier is present.
        //
        // Whether it is load-bearing for wrapping at all in this specific
        // layout is a separate, measured question — see
        // `test_the_stripped_line_measurably_wraps_rather_than_compresses`.
        let text = Text(prefill.stagedLine)
            .font(AppFont.monoSmall)
            .foregroundStyle(prefill.staged.stripped.isEmpty ? Palette.textDim : Palette.warn)
            .fixedSize(horizontal: false, vertical: true)
        if let hover = prefill.strippedFilenamesForHover {
            text.help(hover)
        } else {
            text
        }
    }

    /// On a free Apple ID the picker shows LiveContainer and is disabled, with
    /// the reason under it: Sideload stays on screen, greyed, so the user can
    /// see the option exists and why it is not theirs, rather than wonder
    /// where it went.
    private func deliveryPicker(selection: Binding<DeliveryMode>) -> some View {
        let account = store.account
        let shown = Binding(
            get: { Self.delivery(chosen: selection.wrappedValue, account: account) },
            set: { selection.wrappedValue = $0 }
        )
        return VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("Delivery").font(AppFont.bodyMedium).foregroundStyle(Palette.text)
                Picker("", selection: shown) {
                    // Through `displayName` rather than as literals, because the
                    // app pane's build-target bar now says the same two words in a
                    // sentence and the two must not drift apart.
                    Text(DeliveryMode.sideload.displayName).tag(DeliveryMode.sideload)
                    Text(DeliveryMode.livecontainer.displayName).tag(DeliveryMode.livecontainer)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .paidOnly(account.isFree ? PaidOnly.sideload : nil)
            }
            if account.isFree {
                Text(PaidOnly.sideload)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func field(_ label: String, text: Binding<String>, placeholder: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(AppFont.monoSmall).foregroundStyle(Palette.textDim)
            TextField(placeholder, text: text)
                .textFieldStyle(.roundedBorder)
        }
    }

    // MARK: - Footer

    private var footer: some View {
        HStack {
            Spacer()
            Button("Cancel") { dismiss() }
                .keyboardShortcut(.cancelAction)
                .disabled(isWorking)
            Button(action: submit) {
                if isWorking {
                    ProgressView().controlSize(.small)
                } else {
                    Text("Add")
                }
            }
            .keyboardShortcut(.defaultAction)
            .disabled(isWorking || !canSubmit)
        }
        .padding(20)
    }

    private var canSubmit: Bool {
        switch mode {
        case .url:
            return !appStoreURL.trimmingCharacters(in: .whitespaces).isEmpty
        case .manual:
            return !manualName.trimmingCharacters(in: .whitespaces).isEmpty
                && !manualBundleId.trimmingCharacters(in: .whitespaces).isEmpty
        }
    }

    // MARK: - Submit

    private func submit() {
        guard canSubmit, !isWorking else { return }
        errorMessage = nil
        isWorking = true
        Task {
            defer { isWorking = false }
            let addedName: String?
            do {
                switch mode {
                case .url:
                    addedName = try await store.addApp(
                        appStoreURL: appStoreURL,
                        delivery: Self.delivery(chosen: urlDelivery, account: store.account)
                    )
                case .manual:
                    let trimmedDisplay = manualDisplayName
                        .trimmingCharacters(in: .whitespaces)
                    addedName = try await store.addAppManual(
                        name: manualName.trimmingCharacters(in: .whitespaces),
                        bundleId: manualBundleId.trimmingCharacters(in: .whitespaces),
                        displayName: trimmedDisplay.isEmpty ? nil : trimmedDisplay,
                        delivery: Self.delivery(chosen: manualDelivery, account: store.account)
                    )
                }
            } catch {
                errorMessage = error.localizedDescription
                return
            }
            // Manual mode + a staged IPA: now that the app exists with a real
            // bundle_id in config, try to copy the IPA. A copy failure leaves
            // the app in config (already created) — surface a clear message
            // but don't roll back.
            if mode == .manual, let stagedURL = stagedIPAURL, let name = addedName {
                do {
                    try await store.provideIPA(appName: name, fileURL: stagedURL)
                } catch {
                    errorMessage = "App added, but IPA copy failed: \(error.localizedDescription)"
                    return
                }
            }
            if let addedName {
                onAdded?(addedName)
            }
            // Written only once the add has actually gone through — a
            // cancelled sheet, or a manual add whose IPA copy failed after
            // the app itself was created (the early `return` above), leaves
            // this untouched, so neither can change what the next sheet
            // defaults to.
            //
            // The picker's state, not what was sent. On a free Apple ID the two
            // differ: LiveContainer is sent whatever the picker holds, and the
            // disabled picker still holds the choice a paid account made, which
            // a free add must not overwrite. Writing `delivery(chosen:account:)`
            // here would.
            lastDelivery = mode == .url ? urlDelivery : manualDelivery
            dismiss()
        }
    }
}
