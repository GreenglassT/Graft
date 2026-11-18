import Foundation

/// A build stage where stopping costs something on the *device* rather than
/// only locally — and both sentences the user needs about it.
///
/// One type for the pair because the two tenses were written in two places and
/// had already drifted: `ConfigStore.cancelConsequence` authored the warning
/// ("Stopping mid-write leaves a partial file…") while `AppDetailView`
/// re-authored the aftermath ("The .ipa … may be incomplete…") behind a
/// membership guard plus a two-way ternary. Adding a third risky stage let the
/// guard admit it while the ternary silently gave it the sideload sentence.
///
/// Same reasoning as `DeliveryKind.stateLabel` one type below: the copy the GUI
/// authors lives on a model where a test can see it, and the decision is made
/// exactly once. `CLIContract.Stage.dependedOn` derives from `allCases`, so
/// membership and wording cannot disagree either.
enum DeviceRisk: CaseIterable {
    case liveContainerPush
    case sideloadInstall

    /// nil for every stage that costs only work in progress — which is most of
    /// them, and is why the caller phrases that case rather than this type.
    init?(stage: String?) {
        switch stage {
        case CLIContract.Stage.liveContainerPush: self = .liveContainerPush
        case CLIContract.Stage.sideloadInstall: self = .sideloadInstall
        default: return nil
        }
    }

    var stageName: String {
        switch self {
        case .liveContainerPush: return CLIContract.Stage.liveContainerPush
        case .sideloadInstall: return CLIContract.Stage.sideloadInstall
        }
    }

    /// Future tense, for the confirmation shown before the kill.
    var warning: String {
        switch self {
        case .liveContainerPush:
            return "It is writing the .ipa into LiveContainer right now. Stopping "
                + "mid-write leaves a partial file in its Documents folder — delete "
                + "that before you import anything."
        case .sideloadInstall:
            return "It is installing on the device right now. Stopping mid-install "
                + "can leave the app half-written — re-install before opening it."
        }
    }

    /// Past tense, for the log line written after the kill landed — which is
    /// the only advice that can name the stage the build *actually* died at.
    /// The confirmation could not: it was worded when it was presented, and the
    /// build went on advancing while the user read it.
    var aftermath: String {
        switch self {
        case .liveContainerPush:
            return "The .ipa in LiveContainer's Documents may be incomplete — "
                + "delete it before importing."
        case .sideloadInstall:
            return "The app on the device may be half-installed — re-install "
                + "before opening it."
        }
    }
}

/// What this run actually did to the phone — which is not the same as what
/// the app is *configured* to do.
///
/// This distinction is the whole reason the type exists. The banner used to
/// read `AppRecord.delivery` straight out of config.yaml, so a build run
/// with `--dry-run` or `--skip-install` — both of which the CLI honours by
/// skipping the push entirely while still emitting `build-end {ok: true}` —
/// announced "Open LiveContainer and import it to finish." for a run that
/// sent nothing. The user then imports whatever .ipa is still sitting in
/// Documents/ from the *previous* build and runs it believing it is the one
/// that just finished. A notification is read away from the machine, with
/// none of the screen's context to correct it, so it is the last place an
/// unearned claim should appear.
enum EffectiveDelivery: Equatable {
    case livecontainer
    case sideload
    /// The run was configured not to touch the device. `reason` names
    /// which option did it, in the user's words.
    case notDelivered(reason: String)
    /// Nothing said how it delivered. Distinct from `notDelivered`: this is
    /// "we don't know", not "we know it didn't".
    case unknown

    // `notDeliveringReason` — `if case let .notDelivered(reason) = self` as a
    // `String?` member — used to live here. It was added so the app pane's
    // build-target bar could name the option suppressing delivery, and the
    // revision after it replaced that with `BuildOptions.deliverySuppressor`
    // and the typed `Option` itself: "Dry run" and "Install skipped" are two
    // completely different promises about the phone and a `String?` cannot tell
    // a resolver which one it is holding (see `BuildTarget.notDelivering`).
    // That left it with no production caller and only its own tests, which is
    // the verdict this branch already applied to `DeliveryKind.badgeLabel` one
    // type below — a two-line unwrap is cheaper to write again when something
    // wants it than to carry until then. `BuildTargetBarTests` does the unwrap
    // itself, where it is checking that the prose and the typed answer agree.
}


/// What the running build has done to the phone so far.
///
/// One value, driven by the stage stream, replacing three flags that two
/// consumers read differently — and did disagree about. The confirmation
/// warned "stopping now kills graft before it records that" for the
/// window between `lc-push end` and `build-end`, while the post-mortem read
/// `buildStage` (nil in exactly that window) and wrote no advice at all. A
/// warning whose own follow-up declines to repeat it is worse than either.
///
/// The transitions are total and pure, so the whole thing is testable without
/// a subprocess: a run that will never deliver absorbs every stage; otherwise
/// a risky stage starting moves to `inProgress` and that same stage ending
/// moves to `delivered`.
enum DeviceExposure: Equatable {
    /// `--dry-run` or `--skip-install`: nothing is going to the phone whatever
    /// stage events arrive, and the CLI does still emit `lc-push` for both.
    case neverWill(reason: String)
    case notYet
    case inProgress(DeviceRisk)
    /// Sent, but the CLI has not yet written the delivery row — the one window
    /// where stopping costs the *record* of a file that is really on the phone.
    case delivered(DeviceRisk)

    init(delivery: EffectiveDelivery) {
        if case let .notDelivered(reason) = delivery {
            self = .neverWill(reason: reason)
        } else {
            self = .notYet
        }
    }

    /// Fold one stage event in. Unknown stages leave it unchanged, which is
    /// what makes `acquire`/`cyan`/`sign` free to come and go.
    func advanced(stage: String, status: String) -> DeviceExposure {
        if case .neverWill = self { return self }
        guard let risk = DeviceRisk(stage: stage) else { return self }
        switch (self, status) {
        case (.notYet, "start"): return .inProgress(risk)
        case let (.inProgress(current), "end") where current == risk: return .delivered(risk)
        default: return self
        }
    }

    /// What stopping right now costs, in the future tense the confirmation
    /// needs. Exhaustive: a fifth case is a decision, not a default.
    var cancelWarning: String {
        switch self {
        case let .neverWill(reason):
            return "\(reason) — nothing is being sent to the device on this run, "
                + "so this only discards the work in progress."
        case .notYet:
            return "Nothing has reached the device yet, so this only discards the "
                + "work in progress."
        case let .inProgress(risk):
            return risk.warning
        case .delivered:
            return "The build already finished sending to the device — stopping now "
                + "kills graft before it records that, so the app will show as "
                + "never delivered even though the file is there."
        }
    }

    /// What was actually left behind, in the past tense the log needs after the
    /// kill has landed — nil when there is nothing on the device to act on.
    /// This is the half that used to go missing.
    var cancelAftermath: String? {
        switch self {
        case .neverWill, .notYet: return nil
        case let .inProgress(risk): return risk.aftermath
        case .delivered:
            return "The file did reach the device, but graft was killed before "
                + "recording it — the app will show it as never delivered."
        }
    }
}


/// Whether building right now would produce something different from what was
/// last delivered.
///
/// Note what this is *not*: nothing here has looked at the phone. The CLI
/// compares an inputs digest against its own record of the last push, and for
/// a livecontainer app that push only writes a file into Documents/ — the
/// import that installs it is manual and unverifiable. Every string this type
/// and `DeliveryStateRecord` put on screen therefore describes the record,
/// never the device.
enum DeliveryKind: String, Decodable {
    case never
    case stale
    case current
    case unknown

    /// What this state is called, in a word or three — for every kind, not only
    /// the two that earn a badge.
    ///
    /// This is the GUI's own vocabulary; every other word the user reads about
    /// a delivery comes from the CLI. It lives here rather than inline in a
    /// view so a test can see it, and so there is one set of words rather than
    /// one per surface.
    ///
    /// The app pane's meta strip is why all four are spelled: a `MetaCell` has
    /// to print *something* under DELIVERY STATE, where a mark is free to be
    /// absent. `current` and `unknown` therefore get words here and still no
    /// mark — see `needsAttention`, which is the decision, not the wording.
    ///
    /// "up to date" and "out of date" are deliberately the same phrase in two
    /// polarities: the pair is read at a glance, and two unrelated adjectives
    /// would have to be read.
    var stateLabel: String {
        switch self {
        case .never: return "not delivered"
        case .stale: return "out of date"
        case .current: return "up to date"
        case .unknown: return "unknown"
        }
    }

    /// Which kinds are worth a mark the user has not asked for.
    ///
    /// `current` is the quiet common case, and `unknown` means we genuinely
    /// can't say — badging either would train the user to ignore the one that
    /// matters. Exhaustive on purpose: a fifth kind is a decision, not a
    /// default.
    ///
    /// This used to be spelled `badgeLabel != nil`, with the wording carrying
    /// the decision — a `badgeLabel` that returned nil for `current` and
    /// `unknown`, and a predicate derived from it. That inverted once the meta
    /// strip needed words for all four: giving `current` a label would have
    /// turned the sidebar dot on for every app in the list. So the decision
    /// became this `switch`, and the wording stopped deciding anything.
    ///
    /// `badgeLabel` itself is gone. Once it stopped being the decision, nothing
    /// rendered it: all three marks in the app are dots driven straight off
    /// this property (`AppsListView`, `AppDetailView`, `DeviceAppsSection`),
    /// and the one surface that prints a *word* for this — the app pane's meta
    /// strip, via `AppDetailView.deliveryStateValue` — prints `stateLabel` for
    /// all four states rather than a nullable label for two. (This said "the
    /// two surfaces" and there is one: `DeviceAppsSection`'s row renders the
    /// CLI's own `summary`, and its own doc says why.) `badgeLabel` survived a
    /// while as a pre-written `needsAttention ? stateLabel : nil` for a badging
    /// surface that never arrived; a composition of two tested members, with no
    /// callers, is cheaper to write when it is wanted than to carry until then.
    var needsAttention: Bool {
        switch self {
        case .never, .stale: return true
        case .current, .unknown: return false
        }
    }

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        // A newer CLI adding a kind must not make the event undecodable:
        // String.buildEvents() drops anything that throws, so a strict decode
        // would silently lose the whole record rather than degrade. `kind` is
        // an open string on the wire and the CLI is lenient in exactly this
        // way (`_DELIVERY_MARKS` is looked up with `.get`, never indexed).
        self = DeliveryKind(rawValue: raw) ?? .unknown
    }
}

struct DeliveryStateRecord: Decodable, Hashable {
    /// The app's config key. Spelled `app` here and `name` on the `app` event
    /// — both pinned by Python tests, so `ConfigStore` joins across the pair
    /// rather than "fixing" either side.
    let app: String
    let kind: DeliveryKind
    let summary: String
    let reasons: [String]
    let pushedAt: String?

    enum CodingKeys: String, CodingKey {
        case app, kind, summary, reasons
        case pushedAt = "pushed_at"
    }

    init(
        app: String, kind: DeliveryKind, summary: String,
        reasons: [String], pushedAt: String?
    ) {
        self.app = app
        self.kind = kind
        self.summary = summary
        self.reasons = reasons
        self.pushedAt = pushedAt
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        app = try c.decode(String.self, forKey: .app)
        kind = try c.decode(DeliveryKind.self, forKey: .kind)
        summary = try c.decode(String.self, forKey: .summary)
        reasons = try c.decodeIfPresent([String].self, forKey: .reasons) ?? []
        pushedAt = try c.decodeIfPresent(String.self, forKey: .pushedAt)
    }

    /// What the CLI blamed, as one line — nil when it named nothing.
    ///
    /// The reasons arrive already capped and phrased for a badge
    /// (`diff_delivery` keeps two, then "+N more changes"), so all that is left
    /// is how to join them. Decided here, once: the sidebar's tooltip and the
    /// detail pane's card both render this list and would otherwise drift into
    /// two punctuations of the same record.
    var reasonLine: String? {
        reasons.isEmpty ? nil : reasons.joined(separator: "; ")
    }

    /// Summary and reasons as a single line, for a tooltip — where there is no
    /// typography available to separate the state from what caused it.
    var detail: String {
        reasonLine.map { summary + " " + $0 } ?? summary
    }

    /// What to say about an app the CLI answered for and then said nothing
    /// about — an older CLI, or a state it declined to compute.
    ///
    /// Two panes render this, and had a literal each: the app pane's delivery
    /// section (`AppDetailView`) and the device pane's app rows
    /// (`DeviceAppsSection.statusLine`). Both carried a comment saying the
    /// wording was deliberately the same as the other's, which is the agreement
    /// without the mechanism — and only one of the two was pinned by a test, so
    /// rewording the app pane's copy would have broken the agreement silently.
    /// Shared for the reason `DevicesStore.scanningLine` is shared: a second
    /// spelling of one sentence about one fact is how two surfaces drift.
    ///
    /// **Absence, not a state.** `DeliveryKind.unknown` is a state the CLI does
    /// report and `stateLabel` spells it "unknown"; this is the CLI having said
    /// nothing at all, which is why it is a sentence rather than a word and why
    /// it lives beside the record rather than on the kind.
    ///
    /// The sentence a `nil` *readout* wants is deliberately not here.
    /// `DeviceAppsSection` distinguishes "we haven't asked this phone yet" and
    /// "the ask failed" from this, and its own doc argues that keeping those
    /// apart is the one thing that section must not get wrong.
    static let noneReported = "No delivery state reported for this app."
}
