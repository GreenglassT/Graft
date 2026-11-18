import SwiftUI

/// Where the next build of this app is about to land.
///
/// An enum rather than a formatted `String?`, because the answers are not
/// shades of the same sentence. There are three groups of them: one says where
/// the build lands, one says it deliberately lands nowhere, and six say this
/// pane cannot promise a destination — each for a different reason, with a
/// different remedy. Rendering them from one optional string would either lose
/// that distinction or smuggle it back in by inspecting the prose.
///
/// Eight cases is a lot for one line of text, and the count is the point: every
/// one of them was a sentence this bar had been getting wrong. The list grew
/// from three when a review asked what the resolver could actually *see* —
/// `devices` alone cannot tell "nothing is connected" from "nobody has looked
/// yet" or from "something is connected that we could not read", and it knows
/// nothing at all about the options the build will carry.
///
/// **The sentences, longest first, because they are what has to fit one line at
/// the app's 900pt minimum width.** `.noDevice` 58 characters, `.notDelivering`
/// up to 55 when it stays quiet ("Install skipped …"), `.overrideUnreadable`
/// 52, `.overrideMissing` 51, `.notDelivering` 47 when it speaks up about a
/// chosen phone, `.noDefault` 37, `.unreadable` 36 at two phones and one more
/// per decimal digit of the count, `.notDelivering` 36 when it speaks up about
/// an empty bench, `.scanning` 20.
///
/// This inventory is documentation and nothing more — **no test can hold it to
/// account, and one used to claim it could.** Every sentence below renders
/// `lineLimit(1)` with `.truncationMode(.tail)`, and a `Text` that truncates
/// stops demanding width; that is what truncating *is*. So a layout assertion
/// measures the identical pane whether the line fitted or lost its tail, and
/// two cases in `AppDetailLayoutTests` spent their doc comments claiming
/// otherwise. Substituting a 504-character string for either sentence left both
/// of them green. Keep the counts honest by recounting them, not by trusting a
/// suite to fail.
///
/// It also makes the decision testable without touching a view. Asserting on a
/// rendered SwiftUI body is expensive and brittle — the same reason
/// `DeviceSidebarRow.subtitle(for:)` and `DeliveryKind.stateLabel` are
/// members of a type — and the part worth pinning here is a pure function of
/// what the last scan saw and what the next build will carry.
enum BuildTarget: Equatable {
    /// A connected phone is marked as the default, so the destination is known.
    case device(name: String, mode: String)
    /// Phones are connected and none of them is the default.
    ///
    /// **Not a tie to break.** The CLI marks at most one device
    /// (`json_output.py`: `is_default` is set where the udid matches
    /// `default_device_udid`), so nothing marked means the config names a phone
    /// that is not here, or names none at all — and the two have genuinely
    /// different outcomes. `installer.select_device` raises "requested device
    /// … not connected" in the first case and silently takes `devices[0]` in
    /// the second, and this side cannot tell which from what it can see. That
    /// is exactly why the bar must not guess a name: picking the first device
    /// would print a confident destination that is wrong half the time.
    case noDefault
    /// Nothing is connected at all.
    case noDevice
    /// Build options names a phone that is not connected.
    ///
    /// **Not `.noDefault`.** That case means "nothing is marked" — the config
    /// is quiet and the CLI will pick something. This one is the user having
    /// said exactly which phone, and that phone not being here:
    /// `installer.select_device` walks the connected list for the requested
    /// udid and raises "requested device … not connected" when it is missing,
    /// so the build has a known outcome rather than an unpredictable one. It
    /// earns its own case because the two need opposite advice — plug *that*
    /// phone in or clear the pill, versus mark one in Settings.
    ///
    /// Carries the udid, not a name, and that is not a shortcut. The only
    /// source of device names in this process is the `--devices` scan, and this
    /// case is defined by the phone being absent from it — a `name: String?`
    /// would be nil at every call site that can exist. The udid is what is
    /// known, what the pill in the header shows when it cannot resolve a name,
    /// and what the CLI's own error quotes.
    case overrideMissing(udid: String)
    /// The phone chosen in Build options is on the bus, and the CLI could not
    /// describe it.
    ///
    /// **Split out of `.unreadable`, which this situation was violating the
    /// definition of.** That case says "phones are on the bus that the CLI could
    /// not describe, *and none that it could*" — an answer about the bench as a
    /// whole, reached only when `devices` is empty. The override branch was
    /// returning it with `devices` non-empty: chosen phone locked, a perfectly
    /// readable phone on the next port, and the bar said "3 connected phones
    /// couldn't be read." Three facts wrong in one line — the bench was not the
    /// problem, the readable phone was irrelevant, and the count was not even
    /// the reason for the state. The one actionable fact, that the phone *you
    /// chose* is locked, was the one thing not said.
    ///
    /// Carries the udid rather than a count for that reason: the count belongs
    /// to a question nobody asked here. Like `.overrideMissing` it cannot carry
    /// a name — a phone the CLI could not describe has no name to carry, which
    /// is the definition of the case.
    ///
    /// **Not a certain failure**, which is why it stays distinct from
    /// `.overrideMissing` rather than folding into it. `installer.select_device`
    /// walks `list_connected_devices_with_transport()`, and that is
    /// `idevice_id -l -n` — a bare usbmux enumeration with no lockdown query, so
    /// a passcode-locked phone is in the list it walks. The build resolves this
    /// udid and then may fail later, at the install, for a different reason.
    case overrideUnreadable(udid: String)
    /// No device scan has completed, so an empty list means nothing yet.
    ///
    /// `DevicesStore.hasLoaded` exists for exactly this and argues it at
    /// length; `SidebarView` has read it as the difference between "No devices
    /// connected" and "Looking for devices…" since before this bar was written.
    /// A full `--devices` costs 0.90–1.03s per phone in lockdown handshakes, so
    /// this is a second of every launch — and it is not only a moment:
    /// `ContentView`'s listing runs inside `.task(id: controlActiveState)` and
    /// is torn down when the window stops being frontmost, so a scan the user
    /// interrupted by cmd-tabbing leaves the list empty and unearned
    /// indefinitely. Amber "a build has nowhere to install" is the wrong thing
    /// to hold up in either window.
    case scanning
    /// Phones are on the bus that the CLI could not describe, and none that it
    /// could.
    ///
    /// **The case that must not be `.noDevice`.** `DevicesStore.deviceErrors`
    /// exists because a locked or untrusted phone lands there instead of in
    /// `devices`, and its doc says why it matters in the same words: "'no
    /// devices connected' sends them looking for a cable when the cable is
    /// fine and the phone is locked." `SidebarView` guards its own empty state
    /// on `devices.isEmpty && deviceErrors.isEmpty` for this reason; this bar
    /// printed the sentence that store explicitly warns against.
    ///
    /// The "and none that it could" half is load-bearing and was once only
    /// aspirational: see `.overrideUnreadable`, which exists because the
    /// override branch used to return this case with readable phones in the
    /// list. Reaching it now requires `devices.isEmpty`, so the count really is
    /// the whole bench.
    case unreadable(count: Int)
    /// The build carries an option that stops it reaching any phone, so the
    /// destination question is moot.
    ///
    /// **Carries the option, not its prose.** It used to carry
    /// `reason: String` off `EffectiveDelivery.notDeliveringReason`, and that
    /// string is where this case's defect lived: "Dry run" and "Install
    /// skipped" are one type to the resolver and two completely different
    /// promises about the phone. See `BuildOptions.Option.stillResolvesDevice`
    /// — `--skip-install` stops `cli.py` looking for a device at all, while
    /// `--dry-run` resolves one and only then declines to use it. Recovering
    /// the difference by matching the sentence back against those two literals
    /// would make the wording load-bearing, so the option itself is what is
    /// carried and every derived fact hangs off it.
    ///
    /// **Carries the answer it suppressed, too.** `notDelivering` outranks every
    /// device state — rightly, for `--skip-install` — but outranking is not the
    /// same as being irrelevant, and for a dry run it is not irrelevant at all.
    /// `device` is what `target(...)` would have said with the option absent,
    /// obtained by recursing with `notDelivering: nil`, so the bar can be quiet
    /// about a device problem that genuinely cannot bite and speak up about one
    /// that certainly will. `indirect` because of exactly that nesting; the
    /// recursion is one level deep by construction, since the inner call passes
    /// nil.
    indirect case notDelivering(option: BuildOptions.Option, device: BuildTarget)

    /// Whether this answer wants something done about it — amber on the bar
    /// rather than muted — and the one place that decision is written.
    ///
    /// `tint` and `sentence` both read it instead of switching separately: a
    /// colour and a wording that disagree about whether something is wrong is
    /// worse than either being wrong alone. `DeliveryKind.needsAttention`
    /// records what the other direction cost — there, the wording *was* the
    /// decision until a fourth state needed words and nearly badged them all.
    ///
    /// The interesting case is the last. A build that delivers nothing is
    /// normally nothing to fix — it is what the user asked for — but a **dry
    /// run still resolves a device**, so a dry run against an empty bench is a
    /// build that is already certain to die in `select_device`. Reassuring the
    /// user in muted grey that "this build won't be sent to a device" is, for
    /// that run, a lie of omission about a failure that has already been
    /// decided. The usual argument for silence — a false alarm costs more than
    /// a silence, because it is the one that sends someone looking for a cable
    /// — is about *speculative* device problems. This one is not speculative.
    var needsAttention: Bool {
        switch self {
        case .noDefault, .noDevice, .overrideMissing, .overrideUnreadable, .unreadable:
            return true
        case .device, .scanning:
            return false
        case let .notDelivering(option, device):
            return option.stillResolvesDevice && device.isCertainDeviceFailure
        }
    }

    /// The half-sentence naming what is wrong, for the answers a build that
    /// resolves a device is *guaranteed* to fail on — nil for every other
    /// answer, which is what makes it the definition of "guaranteed" as well as
    /// the wording for it.
    ///
    /// Only two are guaranteed, and both by a `raise` in
    /// `installer.select_device` with nothing conditional in front of it: an
    /// empty list raises "no iOS devices connected", and a `preferred_udid` that
    /// is not in the list raises "requested device … not connected".
    ///
    /// The rest are deliberately absent, and each for its own reason rather
    /// than for want of checking:
    ///
    ///  - `.noDefault` — `select_device(None)` returns `devices[0]`. That is the
    ///    *unpredictable* outcome this bar refuses to name a phone for, but it
    ///    is not a failure.
    ///  - `.unreadable` and `.overrideUnreadable` — the list `select_device`
    ///    walks comes from `idevice_id -l -n`, which enumerates over usbmux
    ///    without a lockdown query, so a phone that defeated the descriptive
    ///    scan is still in it. The build resolves and may fail later, at the
    ///    install, which is a different claim and not one to make here.
    ///  - `.scanning` — nothing has looked yet, so there is nothing to be
    ///    certain about.
    ///  - `.notDelivering` — unreachable: the only construction of that case
    ///    recurses with `notDelivering: nil`.
    ///
    /// Phrased to follow a reason ("Dry run, and …") rather than as a standalone
    /// line, because that is its only call site and a clause that reads as a
    /// sentence on its own invites one.
    var certainFailureClause: String? {
        switch self {
        case .noDevice: return "no device is connected."
        case .overrideMissing: return "the chosen device isn't connected."
        case .device, .noDefault, .overrideUnreadable, .scanning, .unreadable, .notDelivering:
            return nil
        }
    }

    /// Whether a build that gets as far as resolving a device is guaranteed to
    /// fail on this answer.
    ///
    /// Derived from the clause rather than switching a second time. The two
    /// were separate switches for one revision of this file, which is one
    /// revision longer than a codebase that has already watched
    /// `DeliveryKind.needsAttention` and its wording drift apart should allow:
    /// a state added to one list and not the other would render amber with no
    /// sentence, or a sentence with no amber.
    var isCertainDeviceFailure: Bool { certainFailureClause != nil }
}

/// One line under the app pane's header saying where Build & Install will put
/// what it makes.
///
/// The pane's oldest defect was that the build target is a setting you cannot
/// see from the screen that acts on it: `default_device_udid` lives in
/// Settings, the button lives here, and with two phones on the bench nothing
/// on this pane said which one was about to receive a 130 MB IPA.
///
/// Pinned above the `ScrollView` rather than placed inside it, because it
/// annotates Build & Install, which is itself pinned in the header — a
/// destination label that scrolls away from the button it describes is no
/// better than no label.
///
/// **Which axis that puts at risk was predicted wrong and then measured, so the
/// measurement is what is recorded here.** The rule this pane's other sections
/// follow — above a scroll view an unbounded *height* demand escapes into the
/// window (a header injection once demanded 2267pt in a 560pt window, pushing
/// the titlebar to y = −52), inside one the scroll view absorbs height and
/// *width* escapes instead — is about a vertical stack of wrapping text. This
/// is a single-row `HStack`, and nothing in it can wrap: an over-wide item
/// makes the row wider, not taller. A 200-character `Text` with a plain
/// `.fixedSize()` injected beside the sentence took the pane to **1590pt of
/// width in a 900pt window** and left its height entirely within tolerance.
///
/// So the guard that matters for this bar is the width assertion in
/// `AppDetailLayoutTests`, which was added for it — the height one that already
/// existed would have passed straight through the injection. Practically: every
/// string here is `lineLimit(1)`, and the one unbounded string (a device name)
/// is the one carrying negative `layoutPriority`.
///
/// **How much that width assertion can actually see, measured rather than
/// assumed, because a review found the surrounding claims were neither.** With
/// `.fixedSize()` injected into `missing`/`pending`, the pane in a 900pt window
/// stays at 900pt for a sentence of 84 characters and reaches 907pt at 86 — so
/// the tripwire sits at roughly **85 characters, about 180pt beyond the longest
/// sentence this bar renders** (`.noDevice`, 58). That slack is the honest
/// scope of the guard: it catches a structural regression that demands width,
/// once the demand is large enough to cross the gap, and it catches nothing at
/// all about a sentence that merely got longer, because `lineLimit(1)` means a
/// longer sentence demands no more width than a short one.
///
/// Two consequences worth keeping: a `.fixedSize()` on a *short* label here
/// would slip through, and the sentence inventory on `BuildTarget` is
/// documentation that has to be maintained by hand.
struct BuildTargetBar: View {
    let target: BuildTarget
    /// What the Change control does — in the app, select the Settings section.
    ///
    /// A closure rather than a `Binding<SidebarItem?>` reached for from here:
    /// `ContentView` owns `selection`, and this bar has no other reason to know
    /// that a sidebar exists.
    ///
    /// Change *navigates*; it deliberately does not lift Settings' picker into
    /// a popover. That picker is bound to `SettingsStore`'s **draft**
    /// (`$store.draft.edited.defaultDeviceUDID`), which has its own
    /// edit-and-save lifecycle — so an inline copy would mean either driving
    /// another pane's draft from here or building a second save path to the
    /// same config key. Both are worse than a navigation to the one screen that
    /// already owns this setting.
    let onChange: () -> Void

    /// Which phone a build started right now would be delivered to.
    ///
    /// The order of the guards is the whole function.
    ///
    /// **`overrideUDID` is answered first**, before "nothing connected" and
    /// before "nothing marked". It is Build options' Device picker, and it does
    /// not *refine* the default — it replaces it: `cli.py` resolves the target
    /// as `select_device(device or cfg.default_device_udid)` at both install
    /// sites, so `--device` on the invocation makes `default_device_udid`
    /// irrelevant. Reading the marked phone first was the defect this parameter
    /// exists for: with a device chosen in Build options, the bar named the
    /// default while the build went to the other phone, forty points from a
    /// `device: <name>` pill naming the one it was really going to.
    ///
    /// An empty device list satisfies both that guard and the `.noDevice` one,
    /// and of the two only this one survives plugging a phone in — with
    /// `--device X` on the command line the build keeps failing until X
    /// specifically is back, so "no device is connected" would send the user to
    /// find a cable and then say the same thing again about the phone they just
    /// plugged in.
    ///
    /// nil and "" are both "let the CLI pick", the same equivalence
    /// `BuildOptions.activeOptions` and `cliArgs` make — an empty string
    /// treated as a choice would announce a missing device for a build whose
    /// command line carries no `--device` at all.
    ///
    /// Then, unchanged: "nothing connected" before "nothing marked", because an
    /// empty list satisfies both and only the first is the useful thing to say.
    ///
    /// `delivery` is the *app's* configured mode, not anything read off the
    /// device. A phone can have LiveContainer installed and still be the target
    /// of a sideload — `livecontainerBundleId` describes what the phone can
    /// accept, `AppConfig.delivery` decides what this app sends.
    ///
    /// **`notDelivering` outranks everything, including the override.** It is
    /// the option `BuildOptions.deliverySuppressor` names, which
    /// `AppDetailView` already asks about fifty lines from here when it starts
    /// the build — the bar was the one caller reading raw `app.delivery`, so it
    /// promised "Installs to iPhone 15 Pro" for a run with Dry run or Skip
    /// install ticked. It goes first because `--skip-install` makes every
    /// device question moot: `cli.py` gates both `select_device` and the
    /// preflight's device checks on `will_install=not skip_install`, so no
    /// phone is looked for, and an amber "no device is connected" whose own
    /// tooltip recommends Skip install to a user who has already ticked it is
    /// worse than saying nothing.
    ///
    /// **What outranking no longer means is discarding.** `--dry-run` is *not*
    /// `--skip-install`: it still resolves a device — `select_device` sits
    /// inside `if not skip_install` and nothing gates it on `dry_run` — so a
    /// dry run with no phone, or with an absent `--device`, dies in
    /// `select_device` every time. This used to be recorded here as a known
    /// omission and defended with "a false alarm on a status line costs more
    /// than a silence". That defence is about *speculative* device problems;
    /// this one is certain, and a muted "this build won't be sent to a device"
    /// over a build that is already guaranteed to fail is not a silence but a
    /// reassurance. So the ordering is unchanged — `notDelivering` still wins —
    /// and the case now carries `device`, the answer this guard suppressed,
    /// obtained by recursing with `notDelivering: nil`. `--skip-install` gets
    /// the quiet sentence it deserves and a doomed dry run gets told.
    ///
    /// The recursion terminates by construction: the inner call passes nil, so
    /// it cannot re-enter this branch.
    ///
    /// The three signals after it — `overrideUDID`, `deviceErrors`,
    /// `hasLoaded` — are passed as the caller's raw facts rather than as a
    /// pre-digested struct, deliberately: every precedence decision in this
    /// function is a decision this function's tests can see. A struct assembled
    /// by the view would move the first of them into the view.
    ///
    /// **Grouping those three into one `DeviceListing` value was considered and
    /// deferred**, and the reason is worth stating so it is re-decided rather
    /// than re-discovered: it would rewrite thirteen tests that are currently
    /// the strongest evidence this resolver has not regressed, in the same
    /// change that alters what it returns. Note there is exactly *one*
    /// production call site (`AppDetailView.buildTargetBar`), so the usual
    /// argument — "keep call sites stable" — is really "keep the tests stable",
    /// and it is only worth paying once the resolver is otherwise still.
    ///
    /// `overrideUDID` is defaulted, and so are the two that describe how much
    /// the device list is worth — `deviceErrors: []` and `hasLoaded: true`
    /// together mean "this list is a settled, complete answer", which is what
    /// every call that says nothing about them means.
    static func target(
        devices: [DeviceRecord],
        delivery: DeliveryMode,
        overrideUDID: String? = nil,
        deviceErrors: [DeviceErrorRecord] = [],
        hasLoaded: Bool = true,
        notDelivering: BuildOptions.Option? = nil
    ) -> BuildTarget {
        // Guarded on `suppressesDelivery` rather than on the option merely being
        // present: the parameter's name is a promise about which options belong
        // here, and an option that does not suppress delivery must not produce a
        // bar saying the build lands nowhere.
        if let option = notDelivering, option.suppressesDelivery != nil {
            return .notDelivering(
                option: option,
                device: target(
                    devices: devices, delivery: delivery, overrideUDID: overrideUDID,
                    deviceErrors: deviceErrors, hasLoaded: hasLoaded, notDelivering: nil
                )
            )
        }
        if let chosen = overrideUDID, !chosen.isEmpty {
            if let match = devices.first(where: { $0.udid == chosen }) {
                return .device(name: match.name, mode: delivery.displayName)
            }
            // Positive evidence beats both guards below it: a chosen phone
            // sitting in `deviceErrors` *is* connected, so "not connected"
            // would be false and "still looking" pointlessly coy.
            //
            // `.overrideUnreadable` and not `.unreadable(count:)`, which this
            // returned until a review pointed out that it broke that case's own
            // stated invariant — "and none that it could". Nothing here says
            // `devices` is empty, so with the chosen phone locked and a readable
            // phone on the next port the bar announced "3 connected phones
            // couldn't be read": an answer about the bench, when the question
            // was about one phone, carrying a count that was not the reason for
            // the state.
            if deviceErrors.contains(where: { $0.udid == chosen }) {
                return .overrideUnreadable(udid: chosen)
            }
            // Absence is only evidence once something has looked. Without this
            // the first second of every launch accuses the user's own choice of
            // being unplugged.
            guard hasLoaded else { return .scanning }
            return .overrideMissing(udid: chosen)
        }
        guard !devices.isEmpty else {
            // The same three-way split `SidebarView`'s Devices section makes —
            // an empty list is "not yet", "something is there we couldn't
            // read", or "nothing is there", and only the last of the three is
            // what this bar used to say for all of them.
            //
            // Deliberately *not* "and in the same order", which this comment
            // used to claim and the sidebar does not do: `SidebarView` renders
            // an `UndescribableDeviceRow` for every error before it reaches its
            // `hasLoaded` ternary, so it is errors-then-scanning where this is
            // scanning-then-errors. It can afford to be, because a list shows
            // both at once and a one-line bar has to pick.
            //
            // `hasLoaded` is checked before `deviceErrors` because the two can
            // only be false-and-non-empty together after `invalidate()` — a
            // manual Refresh, with the previous scan's failures still standing
            // — and mid-rescan "still looking" is the more honest of the two.
            guard hasLoaded else { return .scanning }
            guard deviceErrors.isEmpty else { return .unreadable(count: deviceErrors.count) }
            return .noDevice
        }
        guard let marked = devices.first(where: \.isDefault) else { return .noDefault }
        return .device(name: marked.name, mode: delivery.displayName)
    }

    var body: some View {
        HStack(spacing: Metrics.controlGap) {
            symbol.image(size: 12)
                .foregroundStyle(tint)
                // The sentence beside it says the same thing in words.
                .accessibilityHidden(true)
            sentence
            Spacer(minLength: Metrics.controlGap)
            Button("Change") { onChange() }
                .controlSize(.small)
                .help("Open Settings, where the default device is chosen")
        }
        .padding(.horizontal, Metrics.paneGutter)
        .padding(.vertical, Metrics.controlGap)
        // Explicit rather than left to the `Spacer` above, which yields to
        // `minLength` under pressure — and the fill has to reach both edges
        // whatever the sentence does, or the band stops looking like one.
        .frame(maxWidth: .infinity, alignment: .leading)
        // A band rather than a card: it spans the pane edge to edge between two
        // dividers, so it reads as part of the header's chrome instead of as
        // the first item in the scrolling column below it.
        .background(Palette.surface.opacity(0.5))
        .help(explanation)
    }

    /// The sentence, in the three shapes it comes in.
    ///
    /// Separate `Text` views inside an `HStack` rather than one concatenated
    /// `Text`, and that is not stylistic: a concatenation truncates as a single
    /// run, and the device name sits in the *middle* of the sentence — so a
    /// long name would take "via LiveContainer" off the end with it. Split,
    /// `layoutPriority` decides what gives, the same trick the header above
    /// uses to stop a build-options pill truncating the primary button. The
    /// name is the only unbounded string here (it is whatever the user typed
    /// into Settings on the phone), so it is the one that shrinks.
    @ViewBuilder
    private var sentence: some View {
        switch target {
        case let .device(name, mode):
            HStack(spacing: 0) {
                Text("Installs to ")
                    .foregroundStyle(Palette.textMuted)
                Text(name)
                    .foregroundStyle(Palette.text)
                    .fontWeight(.medium)
                    .truncationMode(.tail)
                    .layoutPriority(-1)
                Text(" via \(mode)")
                    .foregroundStyle(Palette.textMuted)
                    .layoutPriority(1)
            }
            .font(AppFont.body)
            .lineLimit(1)
        case .noDefault:
            missing("Nothing is set as the default device.")
        case .noDevice:
            missing("No device is connected, so a build has nowhere to install.")
        case .overrideMissing:
            // The udid stays in the tooltip. It is forty characters of
            // hexadecimal on a single-row bar that must not truncate the
            // Change control off its own right-hand edge, and it names the
            // phone to nobody — the header's `device:` pill already carries it
            // for anyone who wants to read it back.
            missing("The device chosen in Build options isn't connected.")
        case .overrideUnreadable:
            // Same reasoning as the case above: the udid stays in the tooltip.
            missing("The device chosen in Build options couldn't be read.")
        case let .unreadable(count):
            missing("\(pluralized(count, "connected phone")) couldn't be read.")
        case .scanning:
            // The sidebar's words, not a second set — see
            // `DevicesStore.scanningLine`.
            pending(DevicesStore.scanningLine)
        case let .notDelivering(option, device):
            // `??` cannot fire: `target(...)` only builds this case for an
            // option whose `suppressesDelivery` is non-nil. `defaultLabel` is
            // what a sixth suppressing option added without a phrase should
            // print — a word rather than an empty space.
            let reason = option.suppressesDelivery ?? option.defaultLabel
            // Gated on `needsAttention`, the same property `tint` and `symbol`
            // read, rather than on the two facts it is made of — so the amber
            // sentence, the amber colour and the warning glyph are one decision
            // taken once. `needsAttention` is true here only when the clause is
            // non-nil, which is what makes the `else` below unreachable for a
            // build that is going to fail.
            if target.needsAttention, let clause = device.certainFailureClause {
                missing("\(reason), and \(clause)")
            } else {
                pending("\(reason) — this build won't be sent to a device.")
            }
        }
    }

    /// The "missing" states, worded as what is absent rather than as an error.
    /// Amber and not `danger`: none of them is a failure that has happened, and
    /// `--skip-install` makes every one of them a perfectly buildable state to
    /// be in.
    private func missing(_ text: String) -> some View {
        Text(text)
            .font(AppFont.body)
            .foregroundStyle(Palette.warn)
            .lineLimit(1)
            .truncationMode(.tail)
    }

    /// The two states that are not a problem: a scan still running, and a build
    /// the user has deliberately told not to install.
    ///
    /// Muted rather than amber, and that is the whole reason this exists beside
    /// `missing` — amber is a claim that something wants attention. Neither of
    /// these does. `--skip-install` in amber would be the app second-guessing a
    /// switch the user just flipped, and a spinner's worth of waiting in amber
    /// would make every launch look like a warning.
    private func pending(_ text: String) -> some View {
        Text(text)
            .font(AppFont.body)
            .foregroundStyle(Palette.textMuted)
            .lineLimit(1)
            .truncationMode(.tail)
    }

    /// A `Symbol` rather than the glyph name this used to return.
    ///
    /// Eight symbol names lived in here, and none of them was reachable by the
    /// `Image(systemName: "…")` grep that found every other icon in the app —
    /// they were `return` values feeding a `systemName:` **120 lines away**.
    /// Counted, not estimated, and counted in the file as it then stood: at the
    /// commit before the vocabulary landed this property was `symbol: String`
    /// on line 573 and the `Image(systemName: symbol)` it fed was on line 453.
    /// `SymbolsTests` quotes the same 120. That is the case the vocabulary in
    /// `Symbols` exists for, and the case `SymbolsTests` bans by scanning for
    /// the argument label rather than for a literal shaped like a glyph.
    private var symbol: Symbol {
        switch target {
        case .device: return Symbols.device
        case .noDefault: return Symbols.noDefaultDevice
        case .noDevice: return Symbols.noDevice
        // Not `noDevice` again: that one is "no phone", and this state is
        // "a phone, and it isn't here" — an exclamation reads as the thing
        // being wrong rather than absent.
        case .overrideMissing: return Symbols.attention
        // A lock, because that is what it almost always is: the phone is on the
        // bus and the lockdown handshake was refused. Naming the likely cause
        // in the glyph is worth more here than another warning triangle.
        case .unreadable, .overrideUnreadable: return Symbols.deviceLocked
        case .scanning: return Symbols.scanning
        // "Nothing is going out" is the whole of this state only while the
        // build is going to survive. When it is not — a dry run that will die
        // resolving a device — the glyph has to agree with the amber sentence
        // beside it, or the row says two things at once.
        case .notDelivering: return target.needsAttention ? Symbols.attention : Symbols.notDelivering
        }
    }

    /// Amber for the states that want something done, muted for the ones that
    /// don't.
    ///
    /// None of the amber ones is `danger`: nothing has failed yet, and
    /// `--skip-install` makes every one of them a buildable state to be in —
    /// `cli.py` only calls `select_device` inside `if not skip_install`, so an
    /// unresolvable udid costs nothing on a build that was never going to
    /// install.
    ///
    /// Derived from `BuildTarget.needsAttention` rather than switching a second
    /// time, so the colour cannot disagree with the wording `sentence` picked
    /// from the same property — one list of which states are amber, read twice,
    /// rather than two lists that agree until they don't. The exhaustiveness
    /// lives there; the only thing left here is
    /// `.device`, which is neither a problem nor a wait but the bar doing its
    /// job, and so takes the accent rather than either.
    private var tint: Color {
        if case .device = target { return Palette.accent }
        return target.needsAttention ? Palette.warn : Palette.textMuted
    }

    /// The tooltip, which is where the consequence lives.
    ///
    /// Every sentence above is `lineLimit(1)` with `.truncationMode(.tail)`, so
    /// a line too long for the pane loses its end silently — and *nothing here
    /// can catch that*: `assertFits` measures the same pane whether the text
    /// truncated or not, because truncating is precisely how a `Text` avoids
    /// demanding width. This doc used to assert the lines "stay short enough
    /// never to truncate at the app's minimum width", which was a prediction
    /// with no measurement behind it and no test that could fail if it stopped
    /// being true. What is actually true is weaker and worth knowing: the
    /// tooltip is the only part of this bar guaranteed to be readable in full,
    /// so anything a user *must* be able to read belongs in it.
    ///
    /// It is also where the reasoning goes that no line has room for — two
    /// sentences for `.noDefault`, where the CLI behaves differently depending
    /// on which of the two causes applies and only the user can tell them
    /// apart, and the dry-run caveat under `.notDelivering`.
    private var explanation: String {
        switch target {
        case let .device(name, mode):
            return "Build & Install sends this app to \(name) via \(mode). "
                + "Change the default device in Settings."
        case .noDefault:
            return "No connected phone is marked as the default: either Settings is set "
                + "to 'Ask each time', or the phone it names is not plugged in. A build "
                + "that names no device goes to whichever phone is enumerated first, and "
                + "one naming an absent phone fails outright — so this pane cannot say "
                + "where the next build lands."
        case .noDevice:
            return "No iOS device is connected. A build can still sign an IPA with "
                + "Skip install under Build options, but it has nothing to deliver to."
        case let .overrideMissing(udid):
            return "Build options is set to device \(udid), which is not connected. "
                + "That choice replaces the default rather than narrowing it, so the "
                + "build fails with \"requested device … not connected\" instead of "
                + "falling back to another phone. Clear the device pill in Build "
                + "options, or plug that phone in."
        case let .overrideUnreadable(udid):
            return "Build options is set to device \(udid), which is connected but could "
                + "not be described by the CLI — usually a phone that is passcode-locked, "
                + "or that has not been told to trust this Mac. This is not the same as "
                + "its not being here: the installer enumerates over usbmux with "
                + "`idevice_id -l -n`, which needs no lockdown handshake, so the build "
                + "will very likely still resolve this phone and may then fail at the "
                + "install instead. Unlock it, or clear the device pill in Build options."
        case let .unreadable(count):
            // `pluralized` carries the whole noun phrase rather than leaving a
            // verb outside it: "2 phones is connected" is what the obvious
            // spelling produces.
            return "\(pluralized(count, "connected phone")) could not be described by "
                + "the CLI — usually one that is passcode-locked, or that has not been "
                + "told to trust this Mac. The scan exits 0 either way, so nothing else "
                + "reports it. The Devices section has a row and a retry for each. A "
                + "build may still reach it, because the installer enumerates over "
                + "usbmux and usbmux can see a locked phone — but this pane cannot say "
                + "whose phone it is, so it will not claim one."
        case .scanning:
            return "The first device scan has not finished — a full listing costs about "
                + "a second per phone in lockdown handshakes. Until it lands, an empty "
                + "list is not an answer, and \"no device is connected\" would be a claim "
                + "nothing has checked yet."
        case let .notDelivering(option, device):
            let reason = option.suppressesDelivery ?? option.defaultLabel
            let opening = "\(reason) is set under Build options, so this build signs an "
                + "IPA and stops — nothing is sent to any phone, whatever is plugged in. "
            guard option.stillResolvesDevice else {
                return opening
                    + "No phone is looked for either: `cli.py` gates `select_device` and "
                    + "the preflight's device checks alike on Skip install, so nothing "
                    + "about what is on the bench can make this build fail."
            }
            // The dry-run caveat, which used to be stated as a generality
            // whatever the bench looked like. It is now answered against the
            // device state this build actually faces, because that is the
            // difference between a footnote and a prediction.
            switch device {
            case .noDevice:
                return opening
                    + "But a dry run is not Skip install: it still resolves a device "
                    + "first, and nothing is connected — so this build will not do "
                    + "nothing quietly, it will stop with \"no iOS devices connected\". "
                    + "Plug a phone in, or use Skip install instead, which never looks."
            case let .overrideMissing(udid):
                return opening
                    + "But a dry run is not Skip install: it still resolves a device "
                    + "first, and Build options names device \(udid), which is not "
                    + "connected — so this build will stop with \"requested device … not "
                    + "connected\". Clear the device pill, plug that phone in, or use "
                    + "Skip install instead, which never looks."
            case .device, .noDefault, .overrideUnreadable, .scanning, .unreadable,
                 .notDelivering:
                return opening
                    + "One caveat worth knowing: a dry run still resolves a device "
                    + "before deciding to do nothing, so it can still fail with \"no iOS "
                    + "devices connected\" on an empty bench. Skip install does not — it "
                    + "never looks."
            }
        }
    }
}
