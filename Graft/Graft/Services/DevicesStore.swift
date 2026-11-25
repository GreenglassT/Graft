import Foundation
import Combine

private typealias Flag = CLIContract.Flag

@MainActor
final class DevicesStore: ObservableObject {
    @Published private(set) var devices: [DeviceRecord] = []

    /// Devices usbmux enumerated that the CLI then failed to describe.
    ///
    /// Kept beside `devices` rather than folded into `lastError`, because it
    /// is not an error in the scan: `--devices` exits 0, the other phones on
    /// the bus are listed normally, and there is nothing to retry at the store
    /// level. It is a *partial* answer, and the difference matters to the
    /// person reading it — "no devices connected" sends them looking for a
    /// cable when the cable is fine and the phone is locked.
    ///
    /// This was invisible in the GUI for one round: `device-error` had no
    /// `BuildEvent` case, so it decoded as `.unknown`, and `load` — which then
    /// went through a single `pick` closure keeping only `.device` — dropped
    /// it. The CLI also warns on stderr, which no caller of a zero-exit
    /// `--devices` ever sees: `checkExit` throws only on a non-zero exit.
    @Published private(set) var deviceErrors: [DeviceErrorRecord] = []

    @Published private(set) var isLoading: Bool = false

    /// Whether a `load()` has *completed* — success or failure, devices or not.
    /// Callers that only want to fetch once must key off the attempt, not the
    /// result: "no device plugged in" is a perfectly successful load that
    /// leaves `devices` empty and `lastError` nil, and a result-shaped guard
    /// (`devices.isEmpty && lastError == nil`) re-spawns `--devices` on every
    /// single check in exactly that, the most common, case.
    ///
    /// **A canceled load is not an attempt.** That reasoning above is about
    /// the *outcome* of a scan that ran; cancellation is the case it did not
    /// consider, and it is not hypothetical — `ContentView`'s first listing
    /// runs inside `.task(id: controlActiveState)`, which is torn down the
    /// moment the window stops being frontmost. Switching away during the ~1s
    /// that listing takes used to set this anyway, and `SidebarView` reads it
    /// as the difference between "Looking for devices…" and "No devices
    /// connected" — so a scan that never finished flipped the sidebar to a
    /// definitive answer it had not earned. See `load()`'s `defer`.
    @Published private(set) var hasLoaded: Bool = false

    /// What to say while the answer above is still false.
    ///
    /// Three views render this moment — the sidebar's Devices section, the
    /// build-options popover's device picker, and the app pane's build-target
    /// bar — and they had two copies of the string between them before the bar
    /// existed. Shared here, on the store that owns `hasLoaded` and
    /// `isLoading`, for the same reason `DeliveryKind.stateLabel` is shared: a
    /// third spelling of a sentence three surfaces show about the same second
    /// of waiting is how they drift.
    ///
    /// The negative half of the pair is deliberately *not* here. The sidebar
    /// says "No devices connected" and the bar says "No device is connected, so
    /// a build has nowhere to install" — the bar names a consequence the
    /// sidebar has no business claiming, so those two are genuinely different
    /// sentences rather than one that drifted.
    static let scanningLine = "Looking for devices…"

    @Published var lastError: String?

    /// What the last full listing found on the bus. Compared, never displayed.
    ///
    /// Set from `load()`, not only from an escalated poll: without that, the
    /// poll after a manual Refresh has nothing to compare against but whatever
    /// the *previous* poll saw, and escalates a second time for a change the
    /// Refresh button already picked up.
    ///
    /// **Two fields rather than one set of `(udid, transport)` pairs**, and the
    /// asymmetry is forced rather than chosen: `--devices --brief` reports
    /// `connection_type` for every phone on the bus, but a phone the full
    /// listing *failed to describe* arrives as a `DeviceErrorRecord`, which
    /// carries no transport at all. A locked phone is still on the bus, so the
    /// brief poll keeps reporting it, and leaving its udid out of the
    /// comparison entirely would re-trigger an escalation every 5s for a phone
    /// whose state has not changed. So its udid is remembered with no transport
    /// to compare, and a transport change is only ever noticed for a phone the
    /// listing could actually read.
    /// Unpublished: a change to it alone redraws nothing.
    private var knownTransports: [String: DeviceRecord.ConnectionType] = [:]
    /// The udids the last listing saw but could not describe. Membership only —
    /// there is no transport on a `DeviceErrorRecord` to remember.
    /// Unpublished: a change to it alone redraws nothing.
    private var knownUnreadable: Set<String> = []

    let runner: CLIRunner

    init(runner: CLIRunner) {
        self.runner = runner
    }

    /// Drop the "already fetched" mark so the next `load`-if-needed actually
    /// re-runs. This is what a user-facing Refresh button calls: without it
    /// `hasLoaded` would pin the list to whatever was connected at launch and
    /// a phone plugged in afterward could never appear.
    func invalidate() {
        hasLoaded = false
    }

    /// Store `password` as the root SSH login for `udid`, in the Keychain.
    ///
    /// Lives on this store rather than Settings because the credential is
    /// per-device — the jailbroken phone that dumps and whatever else is on the
    /// bench are different machines with different passwords, and one shared
    /// entry would send the wrong one. The pane that lists devices is the pane
    /// that should set it.
    ///
    /// Piped on stdin, never as an argument: an argv is visible in `ps` and, in
    /// a shell, lands in history. The CLI reads one line either way.
    func setSSHPassword(udid: String, password: String) async throws {
        let result = try await runner.runOnce(
            arguments: [Flag.setSSHPassword, Flag.device, udid], stdin: password
        )
        guard result.exitCode == 0 else {
            throw CLIError.exited(
                doing: "Set SSH password",
                code: result.exitCode,
                stderr: result.stderr.isEmpty ? result.stdout : result.stderr
            )
        }
    }

    /// Re-describe every device: `--devices`, a lockdown handshake per phone.
    ///
    /// `clearingDeviceErrorsOnFailure` is what separates a scan the user asked
    /// for from one a timer started — see the `catch` below, which is the only
    /// thing that reads it. `showingProgress` does too: a timer's scan leaves
    /// `isLoading` alone, so the Refresh and retry buttons spin for a scan
    /// someone started.
    ///
    /// Each published value is written only when it differs from what is
    /// there (`setIfChanged`). `@Published` sends a change for every
    /// assignment, equal or not, and every view observing this store (the
    /// window, the sidebar, the app pane) redraws for it.
    @discardableResult
    func load(
        argsOverride: [String]? = nil, clearingDeviceErrorsOnFailure: Bool = true,
        showingProgress: Bool = true
    ) async throws -> [DeviceRecord] {
        let args = argsOverride ?? [Flag.json, Flag.devices]
        if showingProgress { setIfChanged(\.isLoading, true) }
        // The attempt is finished the moment we stop awaiting the CLI, whether
        // it exited 0, exited non-zero, or never launched — so both flags are
        // settled on every exit path.
        //
        // Except a canceled one, which is not an attempt: the CLI was SIGTERMed
        // part-way through and nothing was learned. `hasLoaded` is what
        // `SidebarView` turns into "No devices connected", and claiming that off
        // a scan the user interrupted by switching windows is a wrong answer
        // rather than a stale one — see the property's own doc. Only ever set
        // true here, never back to false, so a canceled *re*-scan leaves the
        // previous attempt's answer standing.
        //
        // `isLoading` is settled unconditionally, cancellation included: it
        // describes this call, and this call is over either way. Leaving it true
        // would spin every Refresh button on the pane forever.
        defer {
            if showingProgress { setIfChanged(\.isLoading, false) }
            if !Task.isCancelled { setIfChanged(\.hasLoaded, true) }
        }
        // `runEvents` rather than `runCollecting`: one scan answers with two
        // kinds of record and a single `pick` closure can only keep one of
        // them. Both are replaced together so a device that starts answering
        // again drops off the problem list in the same pass it rejoins the
        // real one.
        let events: [BuildEvent]
        do {
            events = try await runner.runEvents(args, failing: "Load devices")
        } catch {
            // A scan that never completed replaces neither list, which for
            // `deviceErrors` means the previous scan's failures outlive the
            // scan that would have cleared them. That list is not an
            // inventory, it is an instruction — "unlock the device and tap
            // 'Trust this Computer', then Refresh" — and a user who pressed
            // Refresh is exactly who gets here. Leaving it up tells them to
            // redo the thing they just did, and the sidebar's Devices section
            // gates its empty state on it (see `SidebarView`), so a non-empty
            // stale list also replaces "No devices connected" with a row for a
            // phone that is no longer there.
            //
            // **Only for a scan the user asked for and that actually ran.**
            // That was the whole justification and it stopped holding the
            // moment a background timer became a caller: clearing the list
            // silently deletes a locked phone's row *and its retry button*,
            // and `pollAndEscalateIfChanged` swallows the throw, so nothing is
            // said about it at all. Two ways in, both routine — the escalated
            // load simply failing, and the far likelier one, the user
            // cmd-tabbing away mid-run, which cancels `ContentView`'s poll
            // task and SIGTERMs the child. `CLIRunner` reports that kill as an
            // ordinary non-zero exit, indistinguishable here from a real
            // failure, which is why cancellation is checked rather than
            // inferred from the error. It self-heals within about five seconds
            // of coming back, so what the user sees is a row that blinks out
            // and returns, with no explanation for either.
            //
            // `devices` deliberately survives, on every path: it makes no claim
            // about a problem and nothing keys off it being empty, while
            // dropping it would clear the install target picked in
            // AppDetailView because an unrelated `--devices` run failed. The
            // banner `lastError` raises is what says the list may be stale.
            if clearingDeviceErrorsOnFailure, !Task.isCancelled {
                setIfChanged(\.deviceErrors, [])
            }
            throw error
        }
        setIfChanged(\.devices, events.compactMap { event in
            if case let .device(record) = event { return record }
            return nil
        })
        setIfChanged(\.deviceErrors, events.compactMap { event in
            if case let .deviceError(record) = event { return record }
            return nil
        })
        // Keeps a poll that immediately follows this load from mistaking "the
        // set hasn't changed since I last checked" for "the set has changed",
        // which is what happens if only an escalated poll ever wrote this.
        knownTransports = Dictionary(
            devices.map { ($0.udid, $0.connectionType) }, uniquingKeysWith: { _, latest in latest }
        )
        knownUnreadable = Set(deviceErrors.map(\.udid))
        return devices
    }

    /// Poll the cheap listing: usbmux enumeration only, none of the per-device
    /// lockdown handshake `load()` pays for. Keeps only `.deviceBrief` events
    /// — `--devices --brief` never emits a `.device` one — and returns what
    /// each phone's udid is attached *by*, comparable against
    /// `knownTransports`.
    ///
    /// The transport comes back for free. `_do_devices_brief` already emits
    /// `connection_type` and `BriefDeviceRecord` already decodes it; this used
    /// to reduce the record to its udid and discard the rest, so a phone moving
    /// from USB to Wi-Fi — the same udid, a different transport — left the
    /// header saying "USB" until something else forced a full listing. Noticing
    /// it costs no extra subprocess and no extra round trip.
    ///
    /// Throws exactly as `load` does, on a non-zero exit or a subprocess that
    /// never ran. Unlike `load`, a failure here touches no other state on this
    /// store — `devices`, `deviceErrors`, `knownTransports` and
    /// `knownUnreadable` are all left exactly as they were.
    /// `pollAndEscalateIfChanged` is the only caller today, and it is the one
    /// that decides what a failure here means; see its doc comment for why that
    /// is "nothing," deliberately.
    @discardableResult
    func loadBrief(
        argsOverride: [String]? = nil
    ) async throws -> [String: DeviceRecord.ConnectionType] {
        let args = argsOverride ?? [Flag.json, Flag.devices, Flag.brief]
        let events = try await runner.runEvents(args, failing: "Poll devices")
        return Dictionary(
            events.compactMap { event -> (String, DeviceRecord.ConnectionType)? in
                if case let .deviceBrief(record) = event {
                    return (record.udid, record.connectionType)
                }
                return nil
            },
            // usbmux enumerating one phone twice is not something to trap on:
            // this is a subprocess's stdout, the one input this process does not
            // control. Same posture as `ConfigStore.loadApps`.
            uniquingKeysWith: { _, latest in latest }
        )
    }

    /// The GUI's live-sidebar heartbeat: poll the cheap listing every tick,
    /// pay for the expensive one only when what usbmux sees has changed.
    ///
    /// Measured on this machine: one phone costs 0.90–1.03s through
    /// `--devices` — almost all of it a lockdown handshake per device —
    /// against 0.17s for `--devices --brief`, which is usbmux enumeration
    /// only. A sidebar row that appears and disappears as phones are plugged
    /// in has to poll to notice that at all, and polling the expensive call
    /// on a 5s timer would mean a subprocess paying a near-1s lockdown query
    /// on almost every tick for a device set that, most ticks, hasn't moved.
    /// Polling the brief call and escalating to `load()` only on an actual
    /// change is what makes that affordable.
    ///
    /// This is deliberately not usbmux's own `listen` /
    /// `receive_device_state_update`, which would be push rather than poll and
    /// cost nothing at rest — strictly the better answer. It needs a
    /// long-lived streaming subprocess with a lifecycle this app does not have
    /// today: every CLI invocation here runs to completion and exits. That is
    /// the known upgrade path, worth taking if 5s of poll latency ever reads
    /// as laggy in use — it is not something this change builds.
    ///
    /// A failed brief poll is swallowed here, not surfaced, and so is a
    /// failure of the escalated `load()` it triggers. Nothing about this
    /// method is something the user asked for — it is a timer firing in the
    /// background — and `lastError` is a modal alert only its own OK button
    /// clears. Latching an error onto it from a poll the user never noticed
    /// running would interrupt them over a hiccup in an unrelated part of the
    /// app, exactly the failure mode `ContentView`'s `.task(id: selection)`
    /// avoids for a canceled section load, and the same reason `load()`
    /// leaves `devices` untouched on a failed rescan rather than clearing it.
    /// `devices` is left exactly as the last successful `load()` — manual or
    /// escalated — found it, and so is `deviceErrors`: swallowing a failure and
    /// then acting on it are two different things, and `load()`'s own `catch`
    /// clears that list on the assumption that a person is watching. Nobody is
    /// watching this one, which is why it opts out — see `load()`'s `catch` for
    /// the row that used to vanish because of it.
    func pollAndEscalateIfChanged(argsOverride: [String]? = nil) async {
        guard let seen = try? await loadBrief(argsOverride: argsOverride) else { return }
        guard hasMoved(seen) else { return }
        _ = try? await load(argsOverride: argsOverride, clearingDeviceErrorsOnFailure: false, showingProgress: false)
    }

    /// Write `value` only when it differs: an equal one would publish a
    /// change all the same (`load`).
    private func setIfChanged<Value: Equatable>(_ keyPath: ReferenceWritableKeyPath<DevicesStore, Value>, _ value: Value) {
        if self[keyPath: keyPath] != value { self[keyPath: keyPath] = value }
    }

    /// Whether the bus looks different from what the last full listing found.
    ///
    /// Two questions, because the brief poll answers two. The first is the one
    /// this design was built for: a phone plugged in or unplugged changes the
    /// set of udids. The second is a phone that is still there but attached
    /// differently — USB pulled from a Wi-Fi-paired handset, or a cable put
    /// back in — which the header renders and which nothing else would notice
    /// until some other change forced a listing.
    ///
    /// Only the phones the listing could *describe* have a transport to compare
    /// against: a `DeviceErrorRecord` carries none, so a locked phone is
    /// checked for presence alone. That is the whole of the asymmetry in
    /// `knownTransports`/`knownUnreadable`, and it points the safe way — the
    /// worst it can cost is one stale "USB" on a phone the pane could not read
    /// anyway.
    private func hasMoved(_ seen: [String: DeviceRecord.ConnectionType]) -> Bool {
        if Set(seen.keys) != Set(knownTransports.keys).union(knownUnreadable) { return true }
        return knownTransports.contains { udid, transport in seen[udid] != transport }
    }
}
