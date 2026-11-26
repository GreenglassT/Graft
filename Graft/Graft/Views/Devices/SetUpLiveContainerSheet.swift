import SwiftUI

/// Set Up LiveContainer on one device, in one sheet (spec §5c).
///
/// On a paid account, **Set Up** runs the Mac's side in order
/// (`LiveContainerStore.runSetUp`): `--lc-install`, whose checks come first
/// (it reads the registered devices again when this device is new to them,
/// Plan E, before anything downloads), then `--lc-push-cert`. Each step's row
/// says where it is; a failure stops there and the sheet says what's left.
/// Install / Update and Put certificate on device, the device page's old
/// buttons, are here one step at a time.
///
/// Then the steps on the device, naming it. The .p12's password is the
/// person's: LiveContainer asks for it on the device, and Graft can't type it
/// there. Ticking "I imported the certificate" takes the certificate off at
/// once (`--lc-clean --cert-only`, the path Remove certificate took):
/// LiveContainer's guest apps can read its Documents folder.
///
/// Where SideStore or AltStore is on the device, a paid account gets no Set
/// Up, as on the device's page: `--lc-install` won't install over them. Its
/// steps then start with importing the certificate from that store.
///
/// On a free Apple ID there is no Set Up and no certificate of Graft's: the
/// CLI's own steps (`LCStatusRecord.freeSetupSteps`), with the SideStore and
/// iloader links the first-run screen shows (`FreeStepRow`), then the same
/// steps on the device.
///
/// While a run is going, Close waits (Stop is beside the run), so the
/// download, install and certificate never carry on with nothing on screen.
/// When a run ends, `onRunEnded` lets `ContentView` read the device list
/// again, so the app pane's bar sees what Set Up installed.
///
/// LiveContainer is never bundled: `--lc-install` downloads it, unmodified,
/// from its own GitHub releases.
///
/// `ContentView` presents it, for the device page's Set Up LiveContainer… and
/// for the app pane's "LiveContainer isn't on …" (`BuildTargetBar`).
struct SetUpLiveContainerSheet: View {
    let device: DeviceRecord
    @ObservedObject var store: LiveContainerStore
    /// `ConfigStore.account`. Nil (not known yet) disables nothing.
    let account: AccountMode?
    /// A run started here ended, however it ended. `ContentView` reads the
    /// device list again (`--devices`).
    var onRunEnded: () -> Void = {}

    /// Which LiveContainers an install puts on, and from which channel: the
    /// choices the device page's Install / Update had.
    @State private var instances: Set<Int> = [1, 2, 3]
    @State private var channel = "nightly"
    /// "I imported the certificate" was ticked since the last push. What the
    /// tick shows is `importTicked`.
    @State private var ticked = false
    @Environment(\.dismiss) private var dismiss

    // MARK: Words

    static let setUpLabel = "Set Up"
    static let installLabel = "Install / Update LiveContainer"
    static let pushLabel = "Put certificate on device"
    static let tickLabel = "I imported the certificate"
    static let closeLabel = "Close"
    static let freeIntro = "With your free Apple ID:"
    /// The free steps when the CLI sent none of its own (a CLI too old, or a
    /// device not read yet), the iOS 26 step included.
    static let freeFallback = [
        "Install LiveContainer with SideStore or iloader, using your free Apple ID.",
        "In LiveContainer, open Settings and tap Import Certificate from SideStore, so it can "
            + "sign the apps you import. iloader doesn't do this for you, and on iOS 26 apps "
            + "won't open until it's done.",
    ]
    /// The .p12's password, on the device.
    static let passwordNote = "LiveContainer asks for it on the device; Graft can't type it for you."
    static let iOS26Note = "On iOS 26, apps won't open until it's done."
    /// The first device step where SideStore or AltStore installed LiveContainer.
    static let storeCertificateStep = "Settings › Import Certificate from SideStore (or the store "
        + "that installed LiveContainer), so it can sign the apps you import. " + iOS26Note
    static let closeWaits = "Wait for it to finish, or press Stop: closing now would leave it "
        + "running with nothing on screen."

    static func title(name: String) -> String { "Set Up LiveContainer on \(name)" }

    static func paidIntro(name: String) -> String {
        "Set Up checks \(name) against your developer account, installs LiveContainer from its "
            + "own GitHub releases, signed with your certificate so its apps keep their logins, "
            + "and puts the certificate on \(name) for LiveContainer to import."
    }

    static func altStoreNote(name: String) -> String {
        "SideStore or AltStore is on \(name), so Graft won't install LiveContainer there. "
            + "Install or update it with that store, then follow the steps below."
    }

    static func checklistTitle(name: String) -> String { "Then, in LiveContainer on \(name):" }

    static func tickNote(name: String) -> String {
        "Selecting it takes the certificate off \(name) at once: LiveContainer's apps can read "
            + "its Documents folder."
    }

    static func tickWaits(name: String) -> String {
        "There's no certificate of Graft's on \(name) to take off. Set Up puts one there."
    }

    /// The check's tooltip. The registered devices are read again only when
    /// the device is new to the saved list, and never when that list is
    /// pinned (`device_udids_pinned`), which the app can't see: so it says
    /// when, rather than promising it.
    static func checkHelp(name: String) -> String {
        "Before anything downloads, Graft checks that the .p12 password is in your keychain, "
            + "that \(name) is unlocked, that SideStore or AltStore isn't on it, and that your "
            + "developer account can provision it. If \(name) is new to the device list Graft "
            + "saved, and you haven't pinned that list, Graft reads it again from App Store "
            + "Connect first."
    }

    static func showsSetUp(account: AccountMode?) -> Bool { !account.isFree }

    /// Whether Set Up can start: the device was read, and isn't one where
    /// SideStore or AltStore keeps LiveContainer (`--lc-install` refuses it,
    /// so it isn't offered, as on the device's page). A resume puts the
    /// certificate on only, so it needs no LiveContainer ticked.
    static func setUpEnabled(
        status: LCStatusRecord?, busy: Bool, instances: Set<Int>, resumes: Bool
    ) -> Bool {
        guard let status, status.reachable, status.managedBy != .altstore, !busy else { return false }
        return resumes || !instances.isEmpty
    }

    /// Put certificate on device, alone, only where `--lc-push-cert` has
    /// somewhere to put it: the primary, Graft's to sign, with no store
    /// managing it.
    static func offersPushCert(status: LCStatusRecord?) -> Bool {
        guard let status, status.reachable, status.managedBy != .altstore else { return false }
        return status.hasPrimary
    }

    /// One step on the device.
    struct DeviceStep: Hashable {
        let text: String
        /// The step "I imported the certificate" ticks.
        var removesCertificate = false
    }

    /// The steps on the device. `kind` is "iPad" or "iPhone"
    /// (`BuildTarget.deviceKind`), for the Files app's "On My iPad". Where
    /// SideStore or AltStore manages LiveContainer (`managedBy`), the
    /// certificate comes from that store, not from Graft.
    static func deviceSteps(
        account: AccountMode?, kind: String, managedBy: LCStatusRecord.ManagedBy? = nil
    ) -> [DeviceStep] {
        let rest = [
            DeviceStep(text: "Settings › JIT-Less Diagnose: \"keychain-access-groups correct\" "
                       + "should say Yes."),
            DeviceStep(text: "Import your apps from the Documents folder."),
            DeviceStep(text: "Long-press each app › Convert to Shared App."),
        ]
        if account.isFree { return rest }
        if managedBy == .altstore { return [DeviceStep(text: storeCertificateStep)] + rest }
        return [
            DeviceStep(
                text: "Settings › Import Certificate: pick AppleDevelopment.p12 (On My \(kind) › "
                    + "LiveContainer) and enter your .p12's password. " + passwordNote + " "
                    + iOS26Note,
                removesCertificate: true
            ),
        ] + rest
    }

    static func stepTitle(_ step: LiveContainerStore.SetUp.Step, name: String) -> String {
        switch step {
        case .check: return "Check \(name) and your developer account"
        case .install: return "Install LiveContainer from its GitHub releases"
        case .pushCert: return "Put the certificate on \(name)"
        }
    }

    /// One step's row. A failure's own words are in the run report under
    /// the rows, and in the row's tooltip.
    static func stepLine(
        _ step: LiveContainerStore.SetUp.Step, _ state: LiveContainerStore.SetUp.State, name: String
    ) -> String {
        let title = stepTitle(step, name: name)
        switch state {
        case .waiting: return title
        case .running: return title + "…"
        case .done: return title + ": done"
        case .failed: return title + ": failed"
        case .stopped: return title + ": stopped"
        case .notRun: return title + ": not run"
        }
    }

    /// What's left once Set Up ended short, and where Set Up again picks
    /// up; nil when it's all done or still running.
    static func leftLine(_ setUp: LiveContainerStore.SetUp, name: String) -> String? {
        guard setUp.finished, !setUp.left.isEmpty else { return nil }
        let steps = setUp.left.map { step -> String in
            switch step {
            case .check: return "check \(name)"
            case .install: return "install LiveContainer"
            case .pushCert: return "put the certificate on \(name)"
            }
        }
        let again = setUp.resumesAtCertificate
            ? "Set Up again puts it on without installing again."
            : "Set Up again starts from the check."
        return "Left: " + steps.joined(separator: ", ") + ". " + again
    }

    /// What VoiceOver hears as Set Up moves: each step as it starts, then
    /// how it ended. Nil when nothing is starting or ending.
    static func announcement(_ setUp: LiveContainerStore.SetUp, name: String) -> String? {
        if setUp.finished {
            return leftLine(setUp, name: name)
                ?? "Set Up is done: LiveContainer and the certificate are on \(name)."
        }
        guard let step = LiveContainerStore.SetUp.Step.allCases.first(where: {
            setUp.states[$0] == .running
        }) else { return nil }
        return stepLine(step, .running, name: name)
    }

    /// Whether the tick shows ticked: from the tick until its removal fails,
    /// and for good once the certificate is off.
    static func importTicked(ticked: Bool, certificatePushed: Bool, busy: Bool) -> Bool {
        ticked && (busy || !certificatePushed)
    }

    /// The tick's action. Ticked, the certificate comes off the device now,
    /// and nothing else does. Unticking does nothing: it isn't put back.
    @MainActor
    static func tick(_ on: Bool, store: LiveContainerStore, udid: String) {
        if on { store.startRemoveCert(udid: udid) }
    }

    // MARK: Body

    private var status: LCStatusRecord? { store.status[device.udid] }
    private var kind: String { BuildTarget.deviceKind(model: device.model) }
    private var mine: LiveContainerStore.SetUp? {
        store.setUp?.udid == device.udid ? store.setUp : nil
    }
    private var resumes: Bool {
        store.resumesAtCertificate(udid: device.udid, instances: instances, channel: channel)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: Metrics.sectionGap) {
                    Text(Self.title(name: device.name))
                        .font(AppFont.title)
                        .foregroundStyle(Palette.text)
                        .accessibilityAddTraits(.isHeader)
                    deviceState
                    if Self.showsSetUp(account: account) { paidSide } else { freeSide }
                    Divider().background(Palette.line)
                    checklist
                }
                .padding(Metrics.paneGutter)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            Divider().background(Palette.line)
            HStack {
                Spacer()
                Button(Self.closeLabel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                    .disabled(store.isBusy)
                    .help(store.isBusy ? Self.closeWaits : "Close this sheet")
            }
            .padding(Metrics.paneGutter)
        }
        .frame(minWidth: 560, idealWidth: 600, minHeight: 520, idealHeight: 680)
        .background(Palette.bg)
        // Esc and a click outside wait too, as Close does.
        .interactiveDismissDisabled(store.isBusy)
        .task(id: device.udid) {
            store.remember(device)
            await store.load(udid: device.udid)
        }
        .onChange(of: store.isBusy) { busy in
            if !busy { onRunEnded() }
        }
        .onChange(of: mine) { setUp in
            if let setUp, let line = Self.announcement(setUp, name: device.name) {
                VoiceOverAnnouncement.thisMac.post(line)
            }
        }
    }

    @ViewBuilder
    private var deviceState: some View {
        if let elsewhere = store.busyElsewhere(than: device.udid) {
            dim("Busy with LiveContainer on \(store.name(of: elsewhere)) — this device's actions "
                + "wait for it.")
        }
        if let error = store.loadErrors[device.udid] {
            ErrorBanner(message: error, fit: .inset, onDismiss: { store.loadErrors[device.udid] = nil })
        }
        if !store.hasLoaded.contains(device.udid) {
            dim("Checking \(device.name)…")
        } else if let status, !status.reachable {
            dim("Couldn't read \(device.name): \(status.error ?? "no answer")")
        } else if let status, status.managedBy == .altstore, !account.isFree {
            dim(Self.altStoreNote(name: device.name))
        }
    }

    private var paidSide: some View {
        VStack(alignment: .leading, spacing: Metrics.rowGap) {
            dim(Self.paidIntro(name: device.name))
            if let status {
                ForEach(DeviceLiveContainerSection.installNotes(for: status), id: \.self) { dim($0) }
            }
            installChoices
            HStack(spacing: Metrics.controlGap) {
                Button(Self.setUpLabel) {
                    ticked = false
                    store.startSetUp(udid: device.udid, instances: instances, channel: channel)
                }
                .disabled(!Self.setUpEnabled(status: status, busy: store.isBusy,
                                             instances: instances, resumes: resumes))
                .help(setUpHelp)
                Spacer(minLength: 0)
            }
            if let mine {
                ForEach(LiveContainerStore.SetUp.Step.allCases, id: \.self) { step in
                    stepRow(step, mine.states[step] ?? .waiting)
                }
                if let left = Self.leftLine(mine, name: device.name) {
                    Text(left)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.warn)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if let run = store.run(for: device.udid), run.action != .removeCert {
                LiveContainerRunReport(run: run, instances: status?.instances ?? [], onStop: { store.stop() })
            }
            oneStepAtATime
        }
    }

    private var setUpHelp: String {
        if let status, status.managedBy == .altstore { return Self.altStoreNote(name: device.name) }
        if resumes {
            return "Put the certificate on \(device.name): LiveContainer is already installed"
        }
        return "Check \(device.name), install LiveContainer and put the certificate on, in that order"
    }

    /// Which LiveContainers, and which channel: for Set Up and Install /
    /// Update alike.
    private var installChoices: some View {
        HStack(spacing: Metrics.controlGap) {
            Text("LiveContainers:")
            ForEach([1, 2, 3], id: \.self) { n in
                Toggle(n == 1 ? "1" : "\(n)", isOn: Binding(
                    get: { instances.contains(n) },
                    set: { on in if on { instances.insert(n) } else { instances.remove(n) } }
                ))
                .toggleStyle(.checkbox)
                .help(n == 1 ? "LiveContainer (the primary)" : "LiveContainer\(n)")
            }
            Picker("", selection: $channel) {
                Text("nightly").tag("nightly")
                Text("stable").tag("stable")
            }
            .pickerStyle(.menu)
            .labelsHidden()
            .fixedSize()
            .help("nightly is what upstream recommends")
            Spacer(minLength: 0)
        }
        .disabled(store.isBusy || status?.managedBy == .altstore)
        .font(AppFont.monoSmall)
        .foregroundStyle(Palette.textDim)
    }

    private func stepRow(
        _ step: LiveContainerStore.SetUp.Step, _ state: LiveContainerStore.SetUp.State
    ) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Group {
                switch state {
                case .running: ProgressView().controlSize(.small)
                case .done: Symbols.ok.image(size: 11).foregroundStyle(Palette.success)
                case .failed, .stopped: Symbols.failure.image(size: 11).foregroundStyle(Palette.danger)
                case .waiting, .notRun: Symbols.waiting.image(size: 11).foregroundStyle(Palette.textDim)
                }
            }
            // The words beside it say the same.
            .accessibilityHidden(true)
            Text(Self.stepLine(step, state, name: device.name))
                .font(AppFont.monoSmall)
                .foregroundStyle(state == .waiting || state == .notRun ? Palette.textDim : Palette.text)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .help(stepHelp(step, state))
    }

    private func stepHelp(_ step: LiveContainerStore.SetUp.Step, _ state: LiveContainerStore.SetUp.State) -> String {
        if case let .failed(message) = state { return message }
        return step == .check ? Self.checkHelp(name: device.name) : Self.stepTitle(step, name: device.name)
    }

    /// The device page's old buttons, for an update or a certificate alone.
    private var oneStepAtATime: some View {
        HStack(spacing: Metrics.controlGap) {
            Text("One step at a time:")
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.textDim)
            Button {
                store.startInstall(udid: device.udid, instances: instances, channel: channel)
            } label: {
                Symbols.checkUpdates.label(Self.installLabel)
            }
            .controlSize(.small)
            .fixedSize()
            .disabled(!Self.setUpEnabled(status: status, busy: store.isBusy,
                                         instances: instances, resumes: false))
            if Self.offersPushCert(status: status) {
                Button(Self.pushLabel) {
                    ticked = false
                    store.startPushCert(udid: device.udid)
                }
                .controlSize(.small)
                .fixedSize()
                .disabled(store.isBusy)
            }
            Spacer(minLength: 0)
        }
    }

    private var freeSide: some View {
        let given = status?.freeSetupSteps ?? []
        let steps = given.isEmpty ? Self.freeFallback : given
        return VStack(alignment: .leading, spacing: Metrics.rowGap) {
            dim(PaidOnly.installLiveContainer)
            Text(Self.freeIntro)
                .font(AppFont.bodyMedium)
                .foregroundStyle(Palette.text)
            ForEach(Array(steps.enumerated()), id: \.offset) { index, text in
                FreeStepRow(number: index + 1, text: text)
            }
        }
    }

    private var checklist: some View {
        let given = status?.freeSetupSteps.count ?? 0
        let first = account.isFree ? (given == 0 ? Self.freeFallback.count : given) + 1 : 1
        let steps = Self.deviceSteps(account: account, kind: kind, managedBy: status?.managedBy)
        return VStack(alignment: .leading, spacing: Metrics.rowGap) {
            Text(Self.checklistTitle(name: device.name))
                .font(AppFont.bodyMedium)
                .foregroundStyle(Palette.text)
            ForEach(Array(steps.enumerated()), id: \.offset) { index, step in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text("\(first + index).")
                        .font(AppFont.bodyMedium)
                        .foregroundStyle(Palette.textDim)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(step.text)
                            .font(AppFont.body)
                            .foregroundStyle(Palette.text)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                        if step.removesCertificate { importTick }
                    }
                }
            }
        }
    }

    private var importTick: some View {
        let pushed = status?.certificatePushed ?? false
        return VStack(alignment: .leading, spacing: 4) {
            Toggle(Self.tickLabel, isOn: Binding(
                get: { Self.importTicked(ticked: ticked, certificatePushed: pushed, busy: store.isBusy) },
                set: { on in
                    ticked = on
                    Self.tick(on, store: store, udid: device.udid)
                }
            ))
            .toggleStyle(.checkbox)
            .disabled(!pushed || store.isBusy)
            .help(pushed ? Self.tickNote(name: device.name) : Self.tickWaits(name: device.name))
            dim(Self.tickNote(name: device.name))
            if let run = store.run(for: device.udid), run.action == .removeCert {
                LiveContainerRunReport(run: run, instances: status?.instances ?? [], onStop: { store.stop() })
            }
        }
    }

    private func dim(_ text: String) -> some View {
        Text(text)
            .font(AppFont.monoSmall)
            .foregroundStyle(Palette.textDim)
            .fixedSize(horizontal: false, vertical: true)
    }
}
