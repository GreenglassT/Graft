import SwiftUI

/// Dump the app that is on screen on **this** phone, in one click.
///
/// The card launches the app, dumps it and verifies it, and says what it gave
/// up doing so. Everything it puts on screen — which preconditions exist,
/// whether each passed, what to do about a failure — is the CLI's own answer.
/// The GUI re-derives none of it: a second opinion computed from an error
/// string is how the two drift apart, and this feature already had one such bug.
///
/// ## Which phone — and why that is no longer a question
///
/// This card is extracted from `DevicesView`, where it sat above a list of
/// every connected phone and had to be *told* which one to aim at. That target
/// resolved to nil whenever more than one device was attached, and the CLI then
/// fell back on `default_device_udid` — which is the wrong default for this
/// action, and quietly so. The config's default device is the phone builds are
/// *delivered* to, and delivery targets a stock handset running LiveContainer,
/// while a dump needs the jailbroken one. On a bench with both, the button
/// aimed decryption at the handset that can never perform it, and the failure
/// surfaced as a frida error about the wrong device — an answer about a phone
/// the user had not chosen and could not see they had chosen.
///
/// The remedy there was a picker, plus a line of warning text saying that
/// decryption needs the jailbroken phone which is not necessarily the phone you
/// build for. **Neither comes across, and that is the point of the move.** This
/// section belongs to one device: `device.udid` is passed to every call it
/// makes, and there is no state in this file that could name a different one.
/// The defect is structurally impossible here rather than merely fixed — there
/// is nothing left to pick wrong, so there is nothing to warn about either.
/// (`DeviceAppsStore.decryptArgs` records the same hazard from the other side:
/// it passes `--device` rather than letting the CLI choose, for these reasons.)
///
/// ## Every long sentence here is `lineLimit`-ed
///
/// That is not tidiness — it is what stops a pane blanking itself.
/// `.fixedSize(horizontal: false, vertical: true)` pins a `Text` to the ideal
/// height for the width it is *proposed*, and in this card that proposed width
/// came out as effectively nothing: the ~110-character subtitle below resolved
/// to ~78 stacked lines, about 1090pt, and the pane asked for **1391pt inside a
/// 700pt window**. Nothing clipped it usefully, because the excess came off the
/// **top** — the toolbar and the Refresh button were laid out above the
/// window's edge (measured at y = -61 and y = -66 in the running app) and the
/// whole pane read as empty with a phone plugged in. Confirmed by bisection
/// rather than inference: deleting this card, shortening the sentence, or
/// dropping its `fixedSize` each brought the pane back to ~300pt, while
/// `.layoutPriority(1)` on the text column changed nothing. The sibling card
/// carrying the same shape measured **3413pt** — a locked phone blanked the
/// pane while trying to tell you to unlock it.
///
/// **The move changes which half of that failure can still reach the window.**
/// This section is rendered below `DeviceDetailView`'s divider, inside its
/// `ScrollView`, and a scroll view absorbs height without limit — so the
/// vertical demand that took the old pane down cannot take this one down.
/// Measured here, not assumed: an unbounded ~190-character `Text` under
/// `.fixedSize(horizontal: false, vertical: true)`, dropped into this card,
/// **passes all eighteen cases** in `DeviceDetailLayoutTests`, prose and
/// unbreakable alike. What still travels from down here is **width**,
/// straight out through the enclosing `NavigationSplitView`. Measured, with one
/// unbounded ~220-character sentence dropped into this card under a plain
/// `.fixedSize()` (both axes): **1606pt of width demanded in a 900pt window,
/// 706pt of the pane laid out outside it**, and all eighteen cases in that file
/// went red — while the height assertion stayed green at 592pt against a 600pt
/// ceiling, which is the asymmetry stated as a measurement. So the live rule
/// for this file is: no unbounded string may ask for width — no plain
/// `.fixedSize()` on prose, and a `lineLimit` on anything the CLI wrote.
///
/// The `lineLimit`s are kept regardless, and removing one because a sentence
/// looks short enough today would be a mistake even where the window can no
/// longer be taken down by it: these sentences are not a fixed length. Half of
/// them are the CLI's, the readiness fixes and backend reasons among them, and
/// a card that grew to eighty lines inside the scroll view would push the rest
/// of the phone's pane out of sight — a defect the height assertion can no
/// longer catch, which is a reason to keep the bound rather than to trust it.
///
/// ## The one route with no listing behind it, and so no row to add from
///
/// `InstalledAppsSection` below offers "Add as app" on a row once that row's
/// bundle id is staged — but reaching that row needs a ~9s `--device-apps`
/// listing first. This card's whole reason to exist is dumping *without*
/// that listing: one click, whatever is on screen. Press it and never press
/// "List apps" and there is no row anywhere on the pane to add from — the
/// route that asks the least of the user is the one route the add feature
/// could not reach. So the card offers its own Add, under the success line
/// its own dump just wrote, keyed off `deviceApps.lastDecrypt.bundleId`
/// rather than a row's.
struct DeviceDecryptSection: View {
    /// The phone this pane is about, and the only phone this card can aim at.
    let device: DeviceRecord

    /// The dump store — a device *operation*, needing none of the device
    /// listing around it, so it stays a separate piece from `DevicesStore`.
    ///
    /// Handed down by `DeviceDetailView`, which is handed it by `ContentView`.
    /// The Dump button on each row of `InstalledAppsSection` runs through this
    /// same store: a dump started down there narrates itself *here*, in the
    /// notes, transcript, success line and retry row below. One store is also
    /// what makes the two controls agree about being busy — `DeviceAppsStore`
    /// permits one dump at a time and refuses the second, so two instances
    /// would leave both buttons enabled and one of them silently doing nothing.
    ///
    /// The readiness answer this card renders is looked up by `device.udid`,
    /// and that is a correctness fix rather than tidiness: the store used to
    /// hold one, unkeyed, so for the 2–5s a new phone's probe took this card
    /// drew the *previous* phone's checklist under this phone's name.
    let deviceApps: DeviceAppsStore

    /// This phone's readiness answer, or nil before one has landed for it.
    private var readiness: DecryptCheckRecord? { deviceApps.readiness[device.udid] }

    /// The cache, so the card can offer the add its own success line just
    /// earned. Keyed by bundle id, like everything else that reads it — the
    /// same map `InstalledAppsSection` is handed, so a bundle id this card
    /// just staged and a row further down the pane agree about what is
    /// addable without a second `--staged-ipas` read.
    let staged: [String: StagedIPARecord]
    /// Bundle id → the config keys that claim it, so an app already
    /// configured is not offered a second time. Same map, same reason,
    /// as `InstalledAppsSection.configuredByBundleId` — see that type's own
    /// doc for why it is grouped rather than deduped.
    let configuredByBundleId: [String: [String]]

    /// Open the prefilled add sheet for the app this card just dumped.
    ///
    /// **Required, not defaulted — the same call `DeviceDetailView.
    /// onSelectApp` made, for the same reason.** Once a dump lands on a
    /// staged, unconfigured bundle id, the button below renders in full:
    /// enabled, labelled, with a `.help` string promising what a press does.
    /// A caller that forgot to wire this would get exactly that button,
    /// silently doing nothing — the defect `onSelectApp` was made required
    /// to close (see `DeviceDetailView`'s own doc on that parameter), and the
    /// shape here is the same: a control that has already committed, visibly,
    /// to being clickable.
    ///
    /// This does differ from `onSetSSHPassword` a few lines below, which
    /// keeps its default — and that is worth being honest about rather than
    /// pretending the two are decided by one rule. The difference is not that
    /// one failure mode is worse than the other; both are "press it, nothing
    /// happens." It is that this card has exactly one production caller
    /// (`DeviceDetailView`) and, as of this task, exactly one test file that
    /// constructs it directly — the one written alongside this parameter,
    /// which needs a real closure for its own assertions in every case that
    /// matters and loses nothing by supplying a no-op in the rest. Defaulting
    /// here would buy test-file ergonomics nobody yet needs, at the cost of
    /// a production call site that can silently regress. `onSetSSHPassword`
    /// predates this reasoning and is out of this task's scope to revisit.
    let onAdd: (StagedIPARecord) -> Void

    /// Open the sheet that stores this phone's root SSH password.
    ///
    /// A closure rather than the sheet itself, because saving goes through
    /// `DevicesStore.setSSHPassword` and this card has no `DevicesStore` — see
    /// `DeviceDetailView`, which presents it.
    ///
    /// The control belongs on *this* card and not somewhere quieter: the SSH
    /// readiness check rendered a few lines below carries the CLI's own fix,
    /// "set the phone's root password with `graft --set-ssh-password` (or
    /// from the device's row in the Devices pane)" — and the Devices pane is
    /// gone. Without a button here that sentence names a screen that does not
    /// exist and a terminal command as the only way out.
    var onSetSSHPassword: () -> Void = {}

    /// An explicit `--decrypt-backend`, or nil to let the CLI choose and
    /// escalate. Naming one pins it and turns the retry off. Nil is the default
    /// because the automatic answer is right nearly always, and the override
    /// exists for the case where it isn't.
    @State private var backendOverride: String?
    /// Whether the user opened the checks on a phone `isUnavailable` says
    /// cannot be dumped. Closed again for each phone.
    @State private var showsUnavailableDetails = false

    /// The readiness answer says this phone cannot be dumped from here.
    private var unavailable: Bool { readiness.map(Self.isUnavailable) ?? false }

    /// Whether the checks, the options and the Decrypt button are on show.
    private var showsDetails: Bool { !unavailable || showsUnavailableDetails }

    /// Nothing on the phone answered SSH — the check every dump backend needs
    /// — so nothing can dump it from this Mac. That is what a phone without a
    /// jailbreak looks like, which is most phones.
    ///
    /// **That one SSH failure, not any.** The check also fails when this Mac
    /// has no `iproxy`, and when a jailbroken phone's OpenSSH answered and
    /// refused the stored root password. Both are fixable from here — the
    /// second by the Set root password button this card carries — and
    /// collapsing them into "needs a jailbroken device" hid that fix under a
    /// sentence that was untrue of the phone. The CLI's `reason` decides
    /// (`CLIContract.DecryptCheck.SSHReason`); a CLI older than that field is
    /// read by its detail sentence for the same outcome.
    ///
    /// SSH and not frida: frida answers on stock iOS 17+ phones over RemoteXPC
    /// with no jailbreak at all (`decrypt_ready.check` records the
    /// measurement), so a frida-green, SSH-red phone is the commonest stock
    /// phone, not a nearly-ready one. A frida failure alone is often this Mac's
    /// own client — a missing module, a version skew — which the user can fix
    /// here, so it keeps the checks on show.
    ///
    /// This card used to draw every check in red with its fix underneath on
    /// such a phone: respring, restart OpenSSH from Sileo, open Dopamine. A
    /// user with no jailbreak has none of those, and read the wall of red as
    /// the app being broken.
    static func isUnavailable(_ readiness: DecryptCheckRecord) -> Bool {
        guard !readiness.ready,
              let ssh = readiness.checks.first(where: { $0.key == CLIContract.DecryptCheck.ssh }),
              !ssh.ok
        else { return false }
        if let reason = ssh.reason {
            return reason == CLIContract.DecryptCheck.SSHReason.noServer
        }
        return ssh.detail.hasPrefix(CLIContract.DecryptCheck.noServerDetail)
    }

    /// What the card says instead of its checks on such a phone.
    static let unavailableLine = "On-device decryption needs a jailbroken device with "
        + "frida-server. To get an IPA without one, use Get latest from App Store on an app."
    /// Refuse the partial dump. Off by default, because dropping extensions no
    /// tool can decrypt is what makes a one-click dump possible at all — and
    /// what went is always named afterwards.
    @State private var keepExtensions = false
    /// Show the backend's own output while it runs.
    ///
    /// Off by default — it is a wall of per-file progress — but the first thing
    /// worth reaching for when a dump seems stuck, because it is the only way
    /// to tell "downloading 200 files" from "wedged".
    @State private var verboseDump = false

    /// Written out rather than left to the memberwise one, which the `@State
    /// private var`s below make `private` and so unusable from the pane.
    init(
        device: DeviceRecord,
        deviceApps: DeviceAppsStore,
        staged: [String: StagedIPARecord],
        configuredByBundleId: [String: [String]],
        onAdd: @escaping (StagedIPARecord) -> Void,
        onSetSSHPassword: @escaping () -> Void = {}
    ) {
        self.device = device
        self.deviceApps = deviceApps
        self.staged = staged
        self.configuredByBundleId = configuredByBundleId
        self.onAdd = onAdd
        self.onSetSSHPassword = onSetSSHPassword
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Decrypt an app")
                        .font(AppFont.bodyMedium)
                        .foregroundStyle(Palette.text)
                    // Was "Open the app on the phone and keep it on screen"
                    // — an errand the dump now runs itself. Not said on a
                    // phone that cannot be dumped at all; there it is untrue.
                    if !unavailable {
                        Text("One click. The app is launched on the phone, dumped, and verified.")
                            .font(AppFont.monoSmall)
                            .foregroundStyle(Palette.textDim)
                            .lineLimit(2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer()
                // No device picker beside it any more: see this type's doc.
                // The probe reads *this* phone because there is no other phone
                // this card knows about.
                //
                // The credential button sits beside the probe because the probe
                // is what asks for it — the SSH check below fails with the
                // CLI's own "set the phone's root password" fix, and this is
                // the control that fix now refers to. `fixedSize` for the
                // reason `DeviceHeader`'s chip has it: the label is a constant
                // of this app, and a button reading "Set root pass…" is worse
                // than a subtitle that wraps.
                if showsDetails {
                    Button("Set root password…", action: onSetSSHPassword)
                        .controlSize(.small)
                        .fixedSize()
                        .help("Stored in your login Keychain for this phone. "
                              + "frida-ios-dump pulls the app bundle off over SSH.")
                }
                Button {
                    Task { await deviceApps.checkReadiness(udid: device.udid) }
                } label: {
                    if deviceApps.isChecking {
                        HStack(spacing: 6) {
                            ProgressView().controlSize(.small)
                            Text("Checking…")
                        }
                    } else {
                        Symbols.refresh.label("Re-check")
                    }
                }
                .controlSize(.small)
                .disabled(deviceApps.isChecking)
            }

            if unavailable {
                unavailableNotice
            }
            if showsDetails {
                readinessChecks
                decryptOptions
                decryptAction
            }
            dumpNotes
            dumpTranscript

            if let last = deviceApps.lastDecrypt {
                Text(decryptSummary(last))
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.success)
                    .textSelection(.enabled)
                    // Four, not two: five stripped extension names past the
                    // sentence itself is more than two lines of mono small,
                    // and the truncated half is the half that admits what
                    // the app is missing.
                    .lineLimit(4)
                    .fixedSize(horizontal: false, vertical: true)
                // Below the summary, not beside it. The summary is
                // `lineLimit(4)` and can genuinely reach four lines — a
                // CLI-written sentence plus five stripped extension names —
                // so an `HStack` pairing a one-line button against a block
                // that tall would either pin the button to the top of four
                // lines of text or, center-aligned, float it in the middle
                // of a paragraph. A line of its own reads the same at one
                // stripped extension or five, which an `HStack` cannot.
                //
                // `last.bundleId` is not a guard against a stale
                // cross-phone offer — it can't be one. `StagedIPAStore` is
                // host-wide, not per-phone (see that type's own doc), and
                // `lastDecrypt` is one field on a store `ContentView` owns,
                // so both survive a phone switch: land on a different
                // phone's pane after a dump and this button still draws,
                // still offering the bundle id that was actually last
                // dumped. That is fine, for the same reason the `.task(id:
                // device.udid)` comment below already gives the success
                // line above this button: the line names its own bundle id,
                // so a stale offer is not a misattributed one, only a stale
                // one — this button inherits that same distinction rather
                // than needing a second one.
                //
                // What the key is actually for: `staged` is a dictionary, so
                // reading a record out of it needs some key regardless, and
                // `last.bundleId` is the only one this dump can name — the
                // bundle id it just produced.
                //
                // `(configuredByBundleId[last.bundleId] ?? []).isEmpty` is
                // the narrower, real guard: an app added since this dump
                // landed is not offered a second time. It shares
                // `DeviceDetailView`'s `configuredByBundleId` map, and the
                // same "don't offer twice" intent, with
                // `InstalledAppsSection.rowState` — and, spelled this way,
                // the identical predicate: `rowState` treats this bundle id
                // as configured exactly when `configured[bundleId]?.first`
                // succeeds, i.e. when the array is present *and non-empty*,
                // and `.isEmpty` above is that test's direct negation. An
                // earlier version of this guard read `== nil`, which reads a
                // present-but-empty array as configured — the opposite of
                // `rowState` — and agreed with it only because
                // `Dictionary(grouping:by:)` can never actually build that
                // shape for a present key, a fact about that one builder
                // rather than a rule this line enforced on its own. Not
                // pulled into a shared named predicate with `rowState`:
                // that function already holds the names array in hand for
                // `primary`/`siblings`, so a boolean-only helper would save
                // it nothing, and one that handed back the array would just
                // be this dictionary lookup wearing a name. Unlike
                // `rowState`, there is also no `hasLoadedStaged` input here
                // — none is needed, because an unloaded cache is an empty
                // `staged` dictionary, and `staged[last.bundleId]` below
                // already reads that as "nothing to offer" without being
                // told to.
                if let record = staged[last.bundleId],
                   (configuredByBundleId[last.bundleId] ?? []).isEmpty {
                    Button { onAdd(record) } label: { Symbols.add.label("Add as app") }
                        .controlSize(.small)
                        .fixedSize()
                        .help(
                            "Add \(record.displayName) to your apps, prefilled from "
                            + "the dump you just took."
                        )
                }
            }
            if let error = deviceApps.lastError {
                ErrorBanner(
                    message: error, fit: .inset,
                    onDismiss: { deviceApps.lastError = nil }
                )
                // The one failure the app can fix itself — see
                // `canRetryDroppingExtensions`, which keys off the CLI's
                // own recommendation rather than second-guessing it.
                if deviceApps.canRetryDroppingExtensions {
                    HStack(spacing: 8) {
                        Button {
                            Task { await deviceApps.retryDroppingExtensions() }
                        } label: {
                            Symbols.dropExtensions.label("Drop them anyway and retry")
                        }
                        .controlSize(.small)
                        Text("the built app will not have them")
                            .font(AppFont.monoSmall)
                            .foregroundStyle(Palette.textDim)
                            .lineLimit(2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(Metrics.cardPadding)
        .cardStyle()
        // No padding of its own, horizontal or vertical: it sits in the pane's
        // scrolling column, which already pads the window's gutters and spaces
        // its sections by `Metrics.sectionGap`. Adding either here would inset
        // this card further than the section above it.
        //
        // Probed when the pane appears, and again whenever the pane is pointed
        // at a different phone — the pane that hosts this is built without an
        // `.id(...)` (see `DeviceDetailView`), so SwiftUI reuses one instance
        // across a selection change and the udid is what tells the two apart.
        // The answer is wanted on screen before the user reaches for the button
        // rather than after a dump has already failed.
        //
        // Only the readiness answer is re-taken on that switch. The *last
        // dump's* summary and transcript are the store's and outlive it, which
        // is unchanged from the picker this replaces — the summary names the
        // bundle id it staged, so it is not misattributed, only stale.
        .task(id: device.udid) {
            showsUnavailableDetails = false
            await deviceApps.checkReadiness(udid: device.udid)
        }
    }

    /// One neutral line in place of the checks, and the way to them.
    ///
    /// Neutral on purpose — the info glyph, dim text — because nothing is
    /// wrong: the phone is an ordinary phone. The checks stay one click away
    /// for the user who does have a jailbreak and wants to know which part of
    /// it did not answer (a stopped OpenSSH, a changed root password).
    private var unavailableNotice: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Symbols.advisory.image(size: 11)
                    .foregroundStyle(Palette.textDim)
                    .accessibilityHidden(true)
                Text(Self.unavailableLine)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Button(showsUnavailableDetails ? "Hide details" : "Show details") {
                showsUnavailableDetails.toggle()
            }
            .buttonStyle(.borderless)
            .font(AppFont.label)
            .foregroundStyle(Palette.accent)
        }
    }

    /// The preconditions, each with what to do about it.
    ///
    /// Every line is the CLI's own words. The GUI deliberately re-derives
    /// nothing here: which checks exist, whether each passed, and the fix are
    /// all decided once, by the side that actually performed the probe.
    ///
    /// The branch that used to head this list — "pick which phone to dump
    /// from" — is gone with the picker that made it necessary. There is always
    /// a phone to probe here, so there is never a blank card with every control
    /// greyed out and no stated reason, which is the failure the branch existed
    /// to prevent.
    @ViewBuilder
    private var readinessChecks: some View {
        if readiness == nil && deviceApps.isChecking {
            Text("Checking the device…")
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.textDim)
        } else if let r = readiness {
            VStack(alignment: .leading, spacing: 4) {
                ForEach(r.checks) { check in
                    // Three states, not two. A failing check that is *not*
                    // blocking — "App on screen" while a backend can still
                    // launch the app itself — is a route being unavailable, not
                    // the feature being broken, and drawing it identically to a
                    // dead jailbreak is what made people think it was.
                    let failedHard = !check.ok && check.blocking
                    HStack(alignment: .top, spacing: 6) {
                        (check.ok ? Symbols.ok
                            : (check.blocking ? Symbols.blocked : Symbols.advisory))
                            .image(size: 11)
                            .foregroundStyle(check.ok ? Palette.success
                                             : (check.blocking ? Palette.danger : Palette.textDim))
                            .padding(.top, 1)
                        VStack(alignment: .leading, spacing: 1) {
                            Text("\(check.label): \(check.detail)")
                                .font(AppFont.monoSmall)
                                .foregroundStyle(failedHard ? Palette.text : Palette.textDim)
                                .lineLimit(2)
                                .fixedSize(horizontal: false, vertical: true)
                            if let fix = check.fix {
                                Text(fix)
                                    .font(AppFont.monoSmall)
                                    .foregroundStyle(failedHard ? Palette.warn : Palette.textDim)
                                    .lineLimit(3)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                }
                // Only worth showing when it changes what the user can do: a
                // backend that cannot run, or one carrying a caveat.
                ForEach(Array(Self.backendLines(r.backends).enumerated()), id: \.offset) { _, line in
                    Text(line.text)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textDim)
                        // A note is often the command that fixes the reason
                        // above it; selectable, so it can be copied whole.
                        .textSelection(.enabled)
                        .lineLimit(line.isNote ? 6 : 3)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.leading, line.isNote ? 12 : 0)
                }
            }
        }
    }

    /// One line of the backends' account of themselves.
    struct BackendLine: Equatable {
        let text: String
        /// The backend's `note`, set under its own line rather than in it.
        let isNote: Bool
    }

    /// What the card says about each backend that cannot run, or runs with a
    /// caveat: its reason, and then its note — both, when both are set.
    ///
    /// It was `reason ?? note`, which read as "one or the other" and was
    /// really "never the note" for every backend that is not usable — and that
    /// is the case that sets both: frida-ios-dump not set up has a reason ("is
    /// not set up") *and* a note holding the commands that set it up, and the
    /// commands were the half the card dropped.
    static func backendLines(_ backends: [DecryptCheckRecord.Backend]) -> [BackendLine] {
        backends.flatMap { backend -> [BackendLine] in
            switch (backend.usable ? nil : backend.reason, backend.note) {
            case let (reason?, note?):
                return [BackendLine(text: "\(backend.name): \(reason)", isNote: false),
                        BackendLine(text: note, isNote: true)]
            case let (reason?, nil):
                return [BackendLine(text: "\(backend.name): \(reason)", isNote: false)]
            case let (nil, note?):
                return [BackendLine(text: "\(backend.name): \(note)", isNote: false)]
            case (nil, nil):
                // Unusable with nothing said about why — still worth a line:
                // the button it explains is greyed out.
                return backend.usable ? [] : [BackendLine(text: "\(backend.name): unavailable", isNote: false)]
            }
        }
    }

    /// The one flag worth a control, and deliberately not a third.
    ///
    /// `--decrypt-mode` is omitted: `build_command` drops it on bagbak 4.x, and
    /// 4.x is the major that can talk to a 16.x frida-server, so a mode control
    /// would be a switch that changes nothing on the setup this actually runs on.
    ///
    /// The "keep every extension" checkbox that used to stand beside this
    /// picker is not duplicated here. It exists once, in `decryptAction`, next
    /// to the button it modifies — the two lived in different cards in
    /// `DevicesView` (this one belonged to the installed-app list), and in a
    /// single card the pair would be the same checkbox twice over the same
    /// piece of state.
    ///
    /// `.fixedSize()` on both axes, which is the shape this file's doc warns
    /// about — and is safe for exactly the reason the warning is about
    /// something else: a `Picker`'s three labels are constants of this app, so
    /// what it asks for is a fixed ~150pt and not "however wide the CLI's
    /// sentence is". Without it the menu stretches the width of the card.
    private var decryptOptions: some View {
        HStack(spacing: Metrics.sectionGap) {
            Picker("Backend", selection: $backendOverride) {
                Text("Automatic").tag(String?.none)
                Text("frida-ios-dump").tag(String?.some(CLIContract.DecryptBackend.frida))
                Text("bagbak").tag(String?.some(CLIContract.DecryptBackend.bagbak))
            }
            .pickerStyle(.menu)
            .fixedSize()
            Spacer(minLength: 0)
        }
        .font(AppFont.monoSmall)
        .foregroundStyle(Palette.textDim)
    }

    /// One button, naming the app it will dump.
    @ViewBuilder
    private var decryptAction: some View {
        let ready = readiness?.ready == true
        let appName = readiness?.frontmostName
        HStack(spacing: Metrics.controlGap) {
            Button {
                Task {
                    await deviceApps.decryptFrontmost(
                        device: device.udid,
                        extensions: keepExtensions ? .keep : .automatic,
                        // Only what the user explicitly picked. Naming a
                        // backend *pins* it, and a pinned dump does not escalate
                        // — so forwarding the probe's recommendation, which is
                        // the same choice the CLI makes anyway, silently traded
                        // the automatic retry for nothing.
                        backend: backendOverride,
                        verbose: verboseDump
                    )
                    // The dump changes what is on screen (bagbak spawns, and a
                    // frida dump can leave the app suspended), so the previous
                    // answer is stale the moment it finishes.
                    await deviceApps.checkReadiness(udid: device.udid)
                }
            } label: {
                if deviceApps.decryptingFrontmost {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.small)
                        Text("Decrypting…")
                    }
                } else {
                    // No `size:`: this is a regular-sized button, so its label
                    // is 13pt — which is `Mark.defaultPointSize`. The small
                    // button on an installed-app row below says `size: 11`
                    // because it is 11pt there and a mark cannot inherit a font
                    // the way the symbol this replaced could.
                    Marks.decrypt.label(
                        appName.map { "Decrypt \($0)" } ?? "Decrypt the app on screen"
                    )
                }
            }
            // `isDumping`, not `decryptingFrontmost`: a Dump pressed on a row of
            // the installed-apps list below runs through this same store, and
            // the store permits one dump at a time. Gated on the frontmost flag
            // alone this button stayed live during a row dump and did nothing
            // at all when pressed — `decrypt`/`decryptFrontmost` both return
            // early rather than queueing.
            .disabled(!ready || deviceApps.isDumping)
            .help(ready ? "Dump the app currently on screen"
                        : "Fix the items above first")
            Toggle("Keep every extension (fail instead of dropping)", isOn: $keepExtensions)
                .toggleStyle(.checkbox)
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.textDim)
            Toggle("Verbose", isOn: $verboseDump)
                .toggleStyle(.checkbox)
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.textDim)
                .help("Show bagbak / frida-ios-dump output as the dump runs")
            Spacer(minLength: 0)
        }
    }

    /// The success line, which has to admit what the dump gave up.
    ///
    /// "decrypted — staged for a build" is a true sentence about an app missing
    /// its widgets and share sheet, and the person who notices the missing
    /// widget a week later files it as a bug against the tweak. The CLI says
    /// this on stderr; the pane never saw that, so it said it here or nowhere.
    private func decryptSummary(_ last: DecryptEndRecord) -> String {
        let head = "✓ \(last.bundleId) v\(last.version) decrypted — staged for a build"
        guard !last.stripped.isEmpty else { return head }
        let names = last.stripped
            .map { ($0 as NSString).lastPathComponent }
            .joined(separator: ", ")
        let noun = last.stripped.count == 1 ? "extension" : "extensions"
        return head + ", minus \(last.stripped.count) app \(noun) no tool could "
            + "decrypt: \(names)"
    }

    /// What the dump decided for the user, while it decides it.
    ///
    /// A minute of silence followed by a different tool's output is how
    /// automatic behaviour becomes inexplicable behaviour. These wrap rather
    /// than truncate — there are never more than a couple, and each one is a
    /// whole sentence whose second half is the part that matters ("…and no tool
    /// can decrypt them — dropping them and keeping the rest").
    ///
    /// The `fixedSize` is the vertical-only one, which is what makes wrapping
    /// affordable here: it asks for the height its content needs at the width
    /// it was offered, and never for width. Height is what the pane's
    /// `ScrollView` absorbs.
    @ViewBuilder
    private var dumpNotes: some View {
        if !deviceApps.notes.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                ForEach(Array(deviceApps.notes.enumerated()), id: \.offset) { _, note in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Symbols.note.image(size: 11)
                            .foregroundStyle(Palette.accent)
                        Text(note)
                            .font(AppFont.monoSmall)
                            .foregroundStyle(Palette.text)
                            .fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// The backend's output, while and after a dump.
    ///
    /// Kept after the run rather than cleared: when a dump fails, this is the
    /// evidence — and the failure is exactly when someone wants to read it.
    /// The next dump clears it.
    ///
    /// One line each, truncated in the middle: these are hundreds of per-file
    /// paths from another tool, and they are the one thing on this card that
    /// arrives by the hundred. It is also this pane's only nested `ScrollView`,
    /// with a fixed 140pt height — which is what keeps a long dump's output
    /// from lengthening the card at all.
    @ViewBuilder
    private var dumpTranscript: some View {
        if !deviceApps.transcript.isEmpty {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 1) {
                        ForEach(Array(deviceApps.transcript.enumerated()), id: \.offset) { i, line in
                            Text(line)
                                .font(AppFont.monoSmall)
                                .foregroundStyle(Palette.textDim)
                                .textSelection(.enabled)
                                .lineLimit(1)
                                .truncationMode(.middle)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .id(i)
                        }
                    }
                    .padding(6)
                }
                .frame(height: 140)
                .background(Palette.surface2)
                .clipShape(RoundedRectangle(cornerRadius: Metrics.radiusInner))
                // Follow the tail, which is where a live dump's progress is.
                .onChange(of: deviceApps.transcript.count) { _, count in
                    proxy.scrollTo(count - 1, anchor: .bottom)
                }
            }
        }
    }
}
