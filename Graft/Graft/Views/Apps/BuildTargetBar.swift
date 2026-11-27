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
/// the app's 900pt minimum width.** Counted with "iPhone 15 Pro" and "iPad Air"
/// where a sentence names a device: `.fallback` 85 characters, `.noDevice` 81,
/// `.overrideUnreadable` 80, `.thisBuild` 77, `.overrideMissing` 72,
/// `.defaultUnreadable` 71, `.liveContainerMissing` 36 (and 41 on a second line),
/// `.fallback` with no default set 65, `.unreadable` 64 at two phones and one
/// more per decimal digit of the count, `.notDelivering` up to 63 when it
/// speaks up about several devices, `.defaultMissing` 60, `.noDefault` 60,
/// `.notDelivering` up to 55 when it stays quiet ("Install skipped …"),
/// `.notDelivering` 47 when it speaks up about a
/// chosen device, `.notDelivering` 36 when it speaks up about
/// an empty bench, `.scanning` 20. The two longest put their parenthetical and
/// their "· then …" at the lowest `layoutPriority`, so those give first.
///
/// A paid account's missing LiveContainer takes two lines, its offer of
/// Sideload under the fact: as one 90-character sentence it was cut, and
/// `AppDetailLayoutTests` reads the two back off the accessibility tree to
/// hold them to the minimum width.
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
    /// A device picked for the next Build & Install only (the bar's Change,
    /// or Build options' Device), connected. `then` is where the build after
    /// it goes: "back to iPhone 15 Pro", or `BuildTargetBar.askEachTime`. The
    /// pick goes when that build ends (`ConfigStore.launchBuild`). An update
    /// never takes it: it goes to the device the app was last delivered to.
    case thisBuild(name: String, mode: String, then: String)
    /// Nothing picked, and spec §5b's table chose: the only device connected,
    /// because the default isn't ("iPhone 15 Pro, your default, isn't
    /// connected") or none is set ("no default device is set").
    case fallback(name: String, mode: String, because: String)
    /// The default is set and not connected, several others are, and none
    /// was picked: the person picks, and Build & Install waits.
    case defaultMissing(name: String)
    /// The default is on the bus and the CLI couldn't describe it. The build
    /// still goes there (the CLI's usbmux listing sees a locked phone) and
    /// may fail at the install.
    case defaultUnreadable(name: String)
    /// A LiveContainer app, and the device the build goes to has none, as a
    /// listing that could read its apps said (`DeviceRecord.livecontainerKnown`).
    /// Build & Install waits. `kind` is "iPhone", "iPad", "iPod touch" or
    /// "device" (`deviceKind`). `sideload`: the account is known to be paid,
    /// so the app could be switched to Sideload instead, and the bar says so.
    case liveContainerMissing(name: String, udid: String, kind: String, sideload: Bool = false)
    /// Several devices are connected, Settings names no default (Ask each
    /// time), and none was picked for Build & Install: the person picks, and
    /// Build & Install waits (spec §5b's table).
    ///
    /// **Not a tie to break.** `installer.resolve_target` raises "several
    /// devices are connected: pick one" here, so the bar must not guess a
    /// name either: picking the first device would print a confident
    /// destination the CLI then refuses. With a default set and absent, the
    /// same row is `.defaultMissing`, which can name it.
    case noDefault
    /// Nothing is connected at all.
    case noDevice
    /// The device picked for Build & Install (Change, or Build options' Device) is not connected.
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
    /// Carries the udid, which is what the CLI's own error quotes, and the
    /// device's name when anything still knows it: the name the pick was
    /// made with (`ConfigStore.devicePickName(for:)`), else the remembered
    /// one (`device-seen`). Build & Install waits on it (`blocksBuild`), with
    /// ✕ as the way out, rather than failing minutes in.
    case overrideMissing(udid: String, name: String? = nil)
    /// The phone picked for Build & Install is on the bus, and the CLI could not
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
    /// walks `list_connected_devices_with_transport()`, and that is a bare
    /// usbmux enumeration through pymobiledevice3 with no lockdown query, so
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
    /// A `delivery: sideload` app in a config for a free Apple ID. Nothing on
    /// the bench matters: the CLI refuses every build of it, dry runs and
    /// skipped installs included, before it looks for a device.
    case needsPaidAccount

    /// Whether this answer wants something done about it — amber on the bar
    /// rather than muted — and the one place that decision is written.
    ///
    /// `tint` and `sentence` both read it instead of switching separately: a
    /// color and a wording that disagree about whether something is wrong is
    /// worse than either being wrong alone. `DeliveryKind.needsAttention`
    /// records what the other direction cost — there, the wording *was* the
    /// decision until a fourth state needed words and nearly badged them all.
    ///
    /// The interesting case is the last. A build that delivers nothing is
    /// normally nothing to fix — it is what the user asked for — but a **dry
    /// run still resolves a device**, so a dry run against an empty bench is a
    /// build that is already certain to die in `select_device`. Reassuring the
    /// user in muted gray that "this build won't be sent to a device" is, for
    /// that run, a lie of omission about a failure that has already been
    /// decided. The usual argument for silence — a false alarm costs more than
    /// a silence, because it is the one that sends someone looking for a cable
    /// — is about *speculative* device problems. This one is not speculative.
    var needsAttention: Bool {
        switch self {
        case .noDefault, .overrideMissing, .overrideUnreadable, .unreadable,
             .needsPaidAccount, .defaultMissing, .defaultUnreadable, .liveContainerMissing:
            return true
        // Nothing is wrong with an empty bench (B4): Build & Install waits for
        // a device, and the bar says so in a neutral voice.
        case .device, .scanning, .thisBuild, .fallback, .noDevice:
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
    /// Three are guaranteed, each by a `raise` in `installer.resolve_target`
    /// with nothing conditional in front of it: an empty list raises "no iOS
    /// devices connected", a `--device` that is not in the list raises
    /// "requested device … not connected", and several devices with none the
    /// default and none picked raise "several devices are connected: pick
    /// one" (`.noDefault`, `.defaultMissing`).
    ///
    /// The rest are deliberately absent, and each for its own reason rather
    /// than for want of checking:
    ///
    ///  - `.unreadable` and `.overrideUnreadable` — the list `select_device`
    ///    walks comes from a usbmux enumeration (`preflight.
    ///    list_connected_devices_with_transport`, through pymobiledevice3)
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
        case .noDefault, .defaultMissing: return "several devices are connected and none was picked."
        case .device, .overrideUnreadable, .scanning, .unreadable, .notDelivering,
             .needsPaidAccount, .thisBuild, .fallback, .defaultUnreadable, .liveContainerMissing:
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

    /// Whether Build & Install waits: a device to pick, a picked device that
    /// isn't connected, no device at all, or no LiveContainer on the device
    /// the build would go to (spec §5b). Only for a build that
    /// installs: a dry run or a skipped install is `.notDelivering`, which
    /// never waits.
    var blocksBuild: Bool {
        switch self {
        // `.noDevice` too (B4): `select_device` raises "no iOS devices
        // connected", which reached the pane as a red failure card.
        case .noDefault, .defaultMissing, .liveContainerMissing, .overrideMissing, .noDevice:
            return true
        case .device, .thisBuild, .fallback, .defaultUnreadable,
             .overrideUnreadable, .scanning, .unreadable, .notDelivering, .needsPaidAccount:
            return false
        }
    }

    /// "iPad", "iPod touch" or "iPhone", from a product type such as
    /// `iPad13,16`: how a sentence names the kind of device ("the iPad's
    /// page"), as the CLI does (`devices.family_from_lockdown`). A model
    /// none of them starts is a "device": not guessed to be an iPhone.
    static func deviceKind(model: String) -> String {
        if model.hasPrefix("iPad") { return "iPad" }
        if model.hasPrefix("iPod") { return "iPod touch" }
        if model.hasPrefix("iPhone") { return "iPhone" }
        return "device"
    }

    /// The bar's sentence for `.noDevice` (B4): both ways forward, in one
    /// line; the tooltip says it in two.
    static let noDeviceLine = "Connect your iPhone or iPad to install, or turn on Skip install in Build options."

    /// The bar's sentences for the devices the CLI couldn't read (B1): what
    /// to do, on the line, not only on a tooltip.
    static func unreadableLine(count: Int) -> String {
        "\(pluralized(count, "connected device")) couldn't be read: unlock \(count == 1 ? "it" : "them") "
            + "and tap Trust."
    }

    static func defaultUnreadableLine(name: String) -> String {
        "\(name), your default, couldn't be read: unlock it and tap Trust."
    }

    static let overrideUnreadableLine = "The device picked for Build & Install couldn't be read: "
        + "unlock it and tap Trust."

    /// The bar's sentence for `.needsPaidAccount`: short, because the bar is
    /// one line; the tooltip has the rest.
    static let needsPaidAccountLine = "Sideload needs a paid Apple Developer account. "
        + "Switch this app to LiveContainer with Edit…"
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
/// the tripwire sits at roughly **85 characters**, measured when the longest
/// sentence was `.noDevice`'s 58; `.fallback` (85) now reaches it, which is why
/// its parenthetical truncates first. That slack is the honest
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
    /// The connected devices, for the Change menu (`DevicesStore.devices`).
    var devices: [DeviceRecord] = []
    /// The app's delivery mode: the menu says whether LiveContainer is on a
    /// device only for a LiveContainer app.
    var delivery: DeliveryMode = .livecontainer
    /// This app's pick for its next Build & Install
    /// (`ConfigStore.devicePick(for:)`), or nil. The ✕ shows while there is one.
    var pick: String? = nil
    /// Pick a device for this app's next Build & Install; nil clears the pick
    /// (`ConfigStore.pickDevice(_:for:)`).
    var onPick: (String?) -> Void = { _ in }
    /// "Make … the default": write `default_device_udid`
    /// (`SettingsStore.makeDefaultDevice`).
    var onMakeDefault: (String) -> Void = { _ in }
    /// Open Set Up LiveContainer for a device (spec §5c), for "LiveContainer
    /// isn't on …": `ContentView` presents the sheet over the app pane. Nil
    /// offers no button.
    var onSetUpLiveContainer: ((String) -> Void)? = nil
    /// What the menu's Open Settings does — in the app, select the Settings section.
    ///
    /// A closure rather than a `Binding<SidebarItem?>` reached for from here:
    /// `ContentView` owns `selection`, and this bar has no other reason to know
    /// that a sidebar exists.
    ///
    /// Open Settings *navigates*; the menu deliberately does not lift Settings'
    /// picker into a popover. That picker is bound to `SettingsStore`'s
    /// **draft** (`$store.draft.edited.defaultDeviceUDID`), which has its own
    /// edit-and-save lifecycle — so an inline copy would mean either driving
    /// another pane's draft from here or building a second save path to the
    /// same config key. "Make … the default" is the one exception, and it
    /// writes that one key and leaves an edited draft alone
    /// (`SettingsStore.makeDefaultDevice`).
    let onChange: () -> Void

    /// Which phone a build started right now would be delivered to.
    ///
    /// The order of the guards is the whole function.
    ///
    /// **`overrideUDID` is answered first**, before "nothing connected" and
    /// before "nothing marked". It is this app's pick for Build & Install (the
    /// bar's Change menu or Build options' Device picker), and it does not
    /// *refine* the default — it replaces it: `installer.resolve_target`
    /// installs on an explicit `--device` and nowhere else, so the pick makes
    /// `default_device_udid` irrelevant for that build. Reading the marked phone first was the defect this parameter
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
    /// Then spec §5b's table, as `installer.resolve_target` reads it: the
    /// marked default; else, when exactly one device is on the bus, that one
    /// (`.fallback`, saying why); else the person picks (`.defaultMissing`
    /// when the absent default can be named from `remembered`, `.noDefault`
    /// when none is set). A locked phone counts, because the CLI's usbmux
    /// listing sees it: `devices.count + deviceErrors.count` is the bench.
    /// `remembered` is `DevicesStore.remembered`, the `device-seen` events.
    /// A LiveContainer app whose target has no LiveContainer, as a listing
    /// that could tell said, is `.liveContainerMissing` instead.
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
    /// than a silence". That defense is about *speculative* device problems;
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
        notDelivering: BuildOptions.Option? = nil,
        account: AccountMode? = nil,
        remembered: [SeenDeviceRecord] = [],
        overrideName: String? = nil
    ) -> BuildTarget {
        // Ahead of everything, the build options included: the refusal is the
        // first thing a build of this app does, so no device and no option
        // changes what Build & Install would do, which is fail.
        if delivery == .sideload, account.isFree { return .needsPaidAccount }
        // Guarded on `suppressesDelivery` rather than on the option merely being
        // present: the parameter's name is a promise about which options belong
        // here, and an option that does not suppress delivery must not produce a
        // bar saying the build lands nowhere.
        if let option = notDelivering, option.suppressesDelivery != nil {
            return .notDelivering(
                option: option,
                device: target(
                    devices: devices, delivery: delivery, overrideUDID: overrideUDID,
                    deviceErrors: deviceErrors, hasLoaded: hasLoaded, notDelivering: nil,
                    account: account, remembered: remembered, overrideName: overrideName
                )
            )
        }
        if let chosen = overrideUDID, !chosen.isEmpty {
            if let match = devices.first(where: { $0.udid == chosen }) {
                if let missing = liveContainerMissing(on: match, delivery: delivery, account: account) {
                    return missing
                }
                // Picking the default is no pick: the build goes there anyway.
                if match.isDefault { return .device(name: match.name, mode: delivery.displayName) }
                return .thisBuild(
                    name: match.name, mode: delivery.displayName,
                    then: afterwards(devices: devices, remembered: remembered)
                )
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
            return .overrideMissing(
                udid: chosen,
                name: overrideName ?? remembered.first(where: { $0.udid == chosen })?.displayName
            )
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
        if let marked = devices.first(where: \.isDefault) {
            return liveContainerMissing(on: marked, delivery: delivery, account: account)
                ?? .device(name: marked.name, mode: delivery.displayName)
        }
        let absentDefault = remembered.first(where: \.isDefault)
        if let absentDefault, deviceErrors.contains(where: { $0.udid == absentDefault.udid }) {
            return .defaultUnreadable(name: absentDefault.displayName)
        }
        if devices.count + deviceErrors.count == 1, let only = devices.first {
            return liveContainerMissing(on: only, delivery: delivery, account: account) ?? .fallback(
                name: only.name, mode: delivery.displayName,
                because: absentDefault.map { "\($0.displayName), your default, isn't connected" }
                    ?? "no default device is set"
            )
        }
        if let absentDefault { return .defaultMissing(name: absentDefault.displayName) }
        return .noDefault
    }

    /// `.liveContainerMissing` for a LiveContainer app on `device`, when a
    /// listing that could read its apps found none; else nil. An unread
    /// listing (`livecontainerKnown` false: a locked phone, or an older CLI)
    /// says nothing, so it never blocks a build. On a paid account, known to
    /// be one, it offers Sideload too (`sideload`).
    static func liveContainerMissing(
        on device: DeviceRecord, delivery: DeliveryMode, account: AccountMode? = nil
    ) -> BuildTarget? {
        guard delivery == .livecontainer, device.livecontainerKnown,
              device.livecontainerBundleId == nil
        else { return nil }
        return .liveContainerMissing(
            name: device.name, udid: device.udid, kind: BuildTarget.deviceKind(model: device.model),
            sideload: account == .paid
        )
    }

    /// Settings' "Ask each time", as the bar's "· then …" says it.
    static let askEachTime = "Ask each time"

    /// Where the build after a picked one goes: "back to" the default by name
    /// when it's connected, "back to the default rules" when it isn't, or
    /// Settings' Ask each time.
    static func afterwards(devices: [DeviceRecord], remembered: [SeenDeviceRecord]) -> String {
        if let marked = devices.first(where: \.isDefault) { return "back to \(marked.name)" }
        // The default is unplugged: the build after doesn't go "back to" it
        // but wherever the table says then.
        if remembered.contains(where: \.isDefault) { return backToTheRules }
        return askEachTime
    }

    /// The "· then …" for a default that isn't connected.
    static let backToTheRules = "back to the default rules"

    var body: some View {
        HStack(spacing: Metrics.controlGap) {
            symbol.image(size: 12)
                .foregroundStyle(tint)
                // The sentence beside it says the same thing in words.
                .accessibilityHidden(true)
            // One element for VoiceOver: the split `Text`s are for truncation,
            // not for reading word by word.
            sentence
                .accessibilityElement(children: .combine)
            // A pick is for one Build & Install, and says so: the ✕ takes it
            // back now (spec §5b). Mid-build too: the build already carries
            // its own `--device`, and the cleanup leaves a changed pick alone.
            if pick != nil {
                Button { onPick(nil) } label: {
                    Symbols.clearOption.image(size: 8)
                }
                .buttonStyle(.plain)
                .foregroundStyle(Palette.textMuted)
                .help("Clear the device picked for Build & Install")
                .accessibilityLabel("Clear the device picked for Build & Install")
            }
            Spacer(minLength: Metrics.controlGap)
            action
            changeMenu
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

    /// The one button a state offers beside the menu: for a device without
    /// LiveContainer, Set Up LiveContainer…, which opens the sheet for it
    /// over this pane. It replaced Plan E's "Open the iPad's Page", which
    /// sent the person away from the build they were about to make.
    @ViewBuilder
    private var action: some View {
        if case let (udid, kind)? = Self.setUpTarget(target), let onSetUpLiveContainer {
            Button(DeviceLiveContainerSection.setUpLabel) { onSetUpLiveContainer(udid) }
                .controlSize(.small)
                .help(Self.setUpHelp(kind: kind))
        }
    }

    /// The device the bar's Set Up LiveContainer… sets up, and its kind
    /// ("iPad"), or nil when the target doesn't lack one.
    /// `liveContainerMissing(on:delivery:)` makes that case only when the
    /// scan could read the device's apps (`livecontainerKnown`), so a locked
    /// phone is never offered a setup.
    static func setUpTarget(_ target: BuildTarget) -> (udid: String, kind: String)? {
        if case let .liveContainerMissing(_, udid, kind, _) = target { return (udid, kind) }
        return nil
    }

    /// The bar's line for `.liveContainerMissing`. The Set Up LiveContainer…
    /// button beside it is the way on.
    static func liveContainerMissingLine(name: String) -> String { "LiveContainer isn't on \(name)" }

    /// The other way on, for a paid account: switch the app to Sideload with
    /// the pane's Edit… (2.3.1 B2). A line of its own under the one above: the
    /// two as one sentence, beside Set Up LiveContainer… and Change, were cut
    /// at the minimum width (`AppDetailLayoutTests`).
    static let sideloadOffer = "Or switch this app to Sideload with Edit…"

    /// Set Up LiveContainer…'s tooltip. The bar doesn't know the account, and
    /// a free Apple ID's sheet installs nothing (SideStore or iloader does),
    /// so it promises no install and no certificate.
    static func setUpHelp(kind: String) -> String {
        "Set up LiveContainer on this \(kind), in one sheet over this pane"
    }

    /// Change: every connected device, the next Build & Install's target
    /// checked (picking one targets that build only; an update doesn't take
    /// it), then "Make … the default" and "Open Settings" (spec §5b). An
    /// inline picker, so the current target carries the menu's own checkmark
    /// and VoiceOver reads it as selected. Nothing waits for a build: the
    /// build in flight already carries its device (spec §11.5).
    private var changeMenu: some View {
        Menu {
            Picker("Device for Build & Install", selection: Binding(
                get: { Self.currentTarget(devices: devices, pick: pick) },
                set: { udid in
                    guard let udid else { return }
                    onPick(Self.pick(udid, devices: devices))
                }
            )) {
                ForEach(devices) { device in
                    Text(Self.menuLabel(device, delivery: delivery)).tag(String?.some(device.udid))
                }
            }
            .pickerStyle(.inline)
            .labelsHidden()
            if !devices.isEmpty { Divider() }
            if let candidate = Self.makeDefaultCandidate(devices: devices, pick: pick) {
                Button("Make \(candidate.name) the default") { onMakeDefault(candidate.udid) }
            }
            Button("Open Settings") { onChange() }
        } label: {
            Text("Change")
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
        .controlSize(.small)
        .help("Pick the device for the next Build & Install, or set the default device")
        .accessibilityLabel("Change the device for Build & Install")
    }

    /// The device the menu checks: the pick when it's connected, else the
    /// connected default, else the only device connected; nil when the next
    /// Build & Install has no single device to go to.
    static func currentTarget(devices: [DeviceRecord], pick: String?) -> String? {
        if let pick, devices.contains(where: { $0.udid == pick }) { return pick }
        if let marked = devices.first(where: \.isDefault) { return marked.udid }
        return devices.count == 1 ? devices.first?.udid : nil
    }

    /// One menu row: "iPad Air — iOS 26.1 · LiveContainer (default)". Whether
    /// LiveContainer is there only for a LiveContainer app, and only when the
    /// scan could tell.
    static func menuLabel(_ device: DeviceRecord, delivery: DeliveryMode) -> String {
        var label = "\(device.name) — iOS \(device.iosVersion)"
        if delivery == .livecontainer, device.livecontainerKnown {
            label += device.livecontainerBundleId == nil ? " · no LiveContainer" : " · LiveContainer"
        }
        if device.isDefault { label += " (default)" }
        return label
    }

    /// What choosing `udid` in the menu sets: a pick, or none for the
    /// connected default, where a build goes anyway.
    static func pick(_ udid: String, devices: [DeviceRecord]) -> String? {
        devices.first(where: { $0.udid == udid })?.isDefault == true ? nil : udid
    }

    /// The device "Make … the default" names, or nil to leave the item out:
    /// the picked device when it isn't the default; else, with no connected
    /// default, the one device connected.
    static func makeDefaultCandidate(devices: [DeviceRecord], pick: String?) -> DeviceRecord? {
        if let pick, let picked = devices.first(where: { $0.udid == pick }), !picked.isDefault {
            return picked
        }
        guard !devices.contains(where: \.isDefault), devices.count == 1 else { return nil }
        return devices.first
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
        case let .thisBuild(name, mode, then):
            // "Next Build & Install: iPad Air via LiveContainer · then back to
            // iPhone 15 Pro" (spec §5b): visibly temporary, and visibly Build
            // & Install's, since an update doesn't take the pick. The
            // "· then" gives first.
            HStack(spacing: 0) {
                Text("Next Build & Install: ")
                    .foregroundStyle(Palette.textMuted)
                Text(name)
                    .foregroundStyle(Palette.text)
                    .fontWeight(.medium)
                    .truncationMode(.tail)
                    .layoutPriority(-1)
                Text(" via \(mode)")
                    .foregroundStyle(Palette.textMuted)
                    .layoutPriority(1)
                Text(" · then \(then)")
                    .foregroundStyle(Palette.textMuted)
                    .truncationMode(.tail)
                    .layoutPriority(-2)
            }
            .font(AppFont.body)
            .lineLimit(1)
        case let .fallback(name, mode, because):
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
                Text(" (\(because))")
                    .foregroundStyle(Palette.textMuted)
                    .truncationMode(.tail)
                    .layoutPriority(-2)
            }
            .font(AppFont.body)
            .lineLimit(1)
        case let .defaultMissing(name):
            missing("\(name), your default, isn't connected: pick a device.")
        case let .defaultUnreadable(name):
            missing(BuildTarget.defaultUnreadableLine(name: name))
        case let .liveContainerMissing(name, _, _, sideload):
            // The button beside it says what to do (`action`), and on a paid
            // account the line under it says the other way.
            if sideload {
                VStack(alignment: .leading, spacing: 2) {
                    missing(Self.liveContainerMissingLine(name: name))
                    Text(Self.sideloadOffer)
                        .font(AppFont.body)
                        .foregroundStyle(Palette.textMuted)
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
            } else {
                missing(Self.liveContainerMissingLine(name: name))
            }
        case .noDefault:
            missing("Several devices are connected: pick one for Build & Install.")
        case .noDevice:
            // Muted, not amber (B4): `needsAttention` is false for it.
            pending(BuildTarget.noDeviceLine)
        case .overrideMissing:
            // The udid stays in the tooltip. It is forty characters of
            // hexadecimal on a single-row bar that must not truncate the
            // Change control off its own right-hand edge, and it names the
            // phone to nobody — the header's `device:` pill already carries it
            // for anyone who wants to read it back.
            if case let .overrideMissing(_, name) = target, let name {
                missing("\(name), picked for Build & Install, isn't connected: plug it in, or ✕.")
            } else {
                missing("The device picked for Build & Install isn't connected: plug it in, or ✕.")
            }
        case .overrideUnreadable:
            // Same reasoning as the case above: the udid stays in the tooltip.
            missing(BuildTarget.overrideUnreadableLine)
        case let .unreadable(count):
            missing(BuildTarget.unreadableLine(count: count))
        case .scanning:
            // The sidebar's words, not a second set — see
            // `DevicesStore.scanningLine`.
            pending(DevicesStore.scanningLine)
        case .needsPaidAccount:
            missing(BuildTarget.needsPaidAccountLine)
        case let .notDelivering(option, device):
            // `??` cannot fire: `target(...)` only builds this case for an
            // option whose `suppressesDelivery` is non-nil. `defaultLabel` is
            // what a sixth suppressing option added without a phrase should
            // print — a word rather than an empty space.
            let reason = option.suppressesDelivery ?? option.defaultLabel
            // Gated on `needsAttention`, the same property `tint` and `symbol`
            // read, rather than on the two facts it is made of — so the amber
            // sentence, the amber color and the warning glyph are one decision
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
        case .device, .thisBuild: return Symbols.device
        // Not the usual device, and the reason may have truncated off the line.
        case .fallback: return Symbols.deviceFallback
        case .noDefault, .defaultMissing: return Symbols.noDefaultDevice
        // A phone that is there and can't take this app: something to fix.
        case .liveContainerMissing: return Symbols.attention
        case .noDevice: return Symbols.noDevice
        // Not `noDevice` again: that one is "no phone", and this state is
        // "a phone, and it isn't here" — an exclamation reads as the thing
        // being wrong rather than absent.
        case .overrideMissing: return Symbols.attention
        // A lock, because that is what it almost always is: the phone is on the
        // bus and the lockdown handshake was refused. Naming the likely cause
        // in the glyph is worth more here than another warning triangle.
        case .unreadable, .overrideUnreadable, .defaultUnreadable: return Symbols.deviceLocked
        case .scanning: return Symbols.scanning
        // "Nothing is going out" is the whole of this state only while the
        // build is going to survive. When it is not — a dry run that will die
        // resolving a device — the glyph has to agree with the amber sentence
        // beside it, or the row says two things at once.
        case .notDelivering: return target.needsAttention ? Symbols.attention : Symbols.notDelivering
        case .needsPaidAccount: return Symbols.attention
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
    /// time, so the color cannot disagree with the wording `sentence` picked
    /// from the same property — one list of which states are amber, read twice,
    /// rather than two lists that agree until they don't. The exhaustiveness
    /// lives there; the only thing left here is
    /// `.device`, which is neither a problem nor a wait but the bar doing its
    /// job, and so takes the accent rather than either.
    private var tint: Color {
        switch target {
        case .device, .thisBuild, .fallback: return Palette.accent
        default: break
        }
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
    private var explanation: String { Self.explanation(for: target) }

    /// `explanation`, as a value a test can read.
    static func explanation(for target: BuildTarget) -> String {
        switch target {
        case let .device(name, mode):
            return "Build & Install sends this app to \(name), your default device, via \(mode). "
                + "Change picks another device for one Build & Install."
        case let .thisBuild(name, mode, then):
            let after = then == askEachTime
                ? "the one after follows Settings' Ask each time"
                : then == backToTheRules
                ? "the one after goes to the default when it's connected, else to the only "
                    + "device connected"
                : "the one after goes \(then)"
            return "The next Build & Install sends this app to \(name) via \(mode), picked for "
                + "that build only. The pick clears when that build ends, if it started installing or "
                + "succeeded, and \(after). A build that fails or is stopped before installing keeps "
                + "it, for the retry. An update doesn't use the pick. The ✕ clears it now."
        case let .fallback(name, mode, because):
            return "\(name) is the only device connected (\(because)), so Build & Install sends "
                + "this app there via \(mode). Change picks another device for one Build & "
                + "Install, or makes this one the default."
        case let .defaultMissing(name):
            return "Settings' default device, \(name), isn't connected, and more than one other "
                + "device is, so Graft won't guess which gets the build. Pick one for Build & "
                + "Install with Change, or plug \(name) in."
        case .noDefault:
            return "Several devices are connected and Settings is set to Ask each time, so Graft "
                + "won't guess which gets the build. Pick one for Build & Install with Change, or "
                + "make one the default."
        case let .defaultUnreadable(name):
            return "Settings' default device, \(name), is connected but couldn't be described: "
                + "usually it's passcode-locked, or hasn't been told to trust this Mac. The build "
                + "still goes to it, since a locked iPhone or iPad still counts as connected, and "
                + "may then fail at the install. Unlock it, or pick another device with Change."
        case let .liveContainerMissing(name, _, kind, sideload):
            let waits = "This app is delivered into LiveContainer, and \(name)'s apps don't include "
                + "it, so Build & Install waits. Set Up LiveContainer… opens its setup for this "
                + "\(kind) without leaving this pane, or pick another device with Change."
            guard sideload else { return waits }
            return waits + " Or switch this app to Sideload with Edit…, and Graft installs it on "
                + "\(name) as a normal app."
        case .noDevice:
            return "Connect your iPhone or iPad to install. To build without installing, turn on "
                + "Skip install in Build options."
        case let .overrideMissing(udid, name):
            let named = name.map { "\($0) (\(udid))" } ?? "device \(udid)"
            return "Build & Install is set to \(named), which isn't connected, so it waits. A "
                + "pick replaces the default rather than narrowing it, so the build would fail "
                + "with \"requested device … not connected\" instead of going to another "
                + "device. Plug that iPhone or iPad in, or clear the pick with ✕ to go back to "
                + "the default rules."
        case let .overrideUnreadable(udid):
            return "Build & Install is set to device \(udid), which is connected but could not "
                + "be described by the CLI — usually an iPhone or iPad that is passcode-locked, "
                + "or that has not been told to trust this Mac. This is not the same as its not "
                + "being here: the build asks usbmuxd for the devices on the bus, which needs no "
                + "lockdown handshake, so the build will very likely still resolve this device "
                + "and may then fail at the install instead. Unlock it, or clear it with ✕."
        case let .unreadable(count):
            // `pluralized` carries the whole noun phrase rather than leaving a
            // verb outside it: "2 devices is connected" is what the obvious
            // spelling produces.
            return "\(pluralized(count, "connected device")) could not be described by "
                + "the CLI — usually one that is passcode-locked, or that has not been "
                + "told to trust this Mac. The scan exits 0 either way, so nothing else "
                + "reports it. The Devices section has a row and a retry for each. A "
                + "build may still reach it, because the installer enumerates over "
                + "usbmux and usbmux can see a locked device — but this pane cannot say "
                + "whose device it is, so it will not claim one."
        case .needsPaidAccount:
            return PaidOnly.sideloadApp + " This config has no signing: section, so it is "
                + "set up for a free Apple ID, and Sideload signs the app with a paid "
                + "account's certificate and profile: graft refuses every build of it, "
                + "dry runs included. LiveContainer delivery signs nothing here; "
                + "LiveContainer signs the app when you import it."
        case .scanning:
            return "The first device scan has not finished — a full listing costs about "
                + "a second per device in lockdown handshakes. Until it lands, an empty "
                + "list is not an answer, and \"no device is connected\" would be a claim "
                + "nothing has checked yet."
        case let .notDelivering(option, device):
            let reason = option.suppressesDelivery ?? option.defaultLabel
            let opening = "\(reason) is set under Build options, so this build makes the "
                + "IPA and stops — nothing is sent to any device, whatever is plugged in. "
            guard option.stillResolvesDevice else {
                return opening
                    + "No device is looked for either: Skip install skips the device lookup "
                    + "and the preflight's device checks alike, so nothing about what is on "
                    + "the bench can make this build fail."
            }
            // The dry-run caveat, answered against the device state this
            // build actually faces: the difference between a footnote and a
            // prediction.
            switch device {
            case .noDevice:
                return opening
                    + "But a dry run is not Skip install: it still resolves a device "
                    + "first, and nothing is connected — so this build will not do "
                    + "nothing quietly, it will stop with \"no iOS devices connected\". "
                    + "Plug an iPhone or iPad in, or use Skip install instead, which never looks."
            case let .overrideMissing(udid, _):
                return opening
                    + "But a dry run is not Skip install: it still resolves a device "
                    + "first, and this build names device \(udid), which is not "
                    + "connected — so this build will stop with \"requested device … not "
                    + "connected\". Clear it with ✕, plug that device in, or use Skip "
                    + "install instead, which never looks."
            case .noDefault, .defaultMissing:
                return opening
                    + "But a dry run is not Skip install: it still resolves a device "
                    + "first, and with several connected and none picked it stops there, "
                    + "with \"several devices are connected: pick one\". Pick one with "
                    + "Change, or use Skip install instead, which never looks."
            // `.needsPaidAccount` never sits under `.notDelivering`: `target`
            // returns it ahead of the options. Listed so the switch stays
            // exhaustive without a `default:` that would hide a new case.
            case .device, .overrideUnreadable, .scanning, .unreadable, .notDelivering,
                 .needsPaidAccount, .thisBuild, .fallback, .defaultUnreadable,
                 .liveContainerMissing:
                return opening
                    + "One caveat worth knowing: a dry run still resolves a device "
                    + "before deciding to do nothing, so it can still fail with \"no iOS "
                    + "devices connected\" on an empty bench. Skip install does not — it "
                    + "never looks."
            }
        }
    }
}
