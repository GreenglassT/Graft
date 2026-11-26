import Foundation
import Combine

private typealias Flag = CLIContract.Flag

/// Loads a single device's storage and battery vitals — the two data cells in
/// the per-device stat strip that come from `--device-info` rather than
/// `--devices`.
///
/// **There is no `lastError` here, deliberately**, the same posture as
/// `ArtworkStore` and for the same reason: every way `load` can come back
/// with nothing — a non-zero exit, a subprocess that never launched, a phone
/// the CLI's own lockdown query couldn't reach — collapses to the same one
/// consequence downstream, the stat strip renders "—" per cell it has no data
/// for. That is not a degraded state worth an alert; the strip already shows
/// it, and a modal interrupting someone over a battery reading that failed to
/// refresh would be worse than the dash.
///
/// The one piece of state this store does keep past a failed attempt is the
/// last-known-good record itself, and that is the real design decision here,
/// not an afterthought:
///
/// `reachable: false` is not this store's error channel — it is a
/// *successfully decoded event*, and the CLI emits it on purpose so that "the
/// phone could not be reached this run" stays distinguishable from "the CLI
/// crashed and said nothing at all." A phone on Wi-Fi drops a beat routinely,
/// and a single missed lockdown handshake is not the same fact as "this
/// phone's battery is now unknown." Overwriting `info[udid]` with an
/// unreachable record on that blip would blank a strip that read 70% a moment
/// ago and then fill it back in — which tells the user less than doing nothing
/// would.
///
/// **One thing polls this store, slowly.** `load` has three callers, all in
/// `DeviceDetailView`: the `.task(id: device.udid)` that fires when the pane
/// is pointed at a phone, the header's Refresh, and — since the follow-up
/// below was built — a 60-second poll of the visible pane's vitals.
///
/// That poll does not change the merge below, and it is worth saying which
/// argument it does and does not support. An earlier version of this comment
/// described a *five-second* poll and reasoned from it, and no such thing has
/// ever existed; the flicker the merge avoids is still not a fast repeating
/// one. It is a Refresh landing on a blip: the user presses the button, the
/// handshake misses, and the strip they were looking at goes blank in
/// response to a gesture that was supposed to confirm it. What the poll adds
/// is that the same blank can now happen *unattended*, with no gesture to
/// blame it on: nobody pressed anything, and the strip empties itself while
/// being looked at. That is a strictly worse version of the bug the merge
/// exists to prevent, so the poll strengthens the case for the merge rather
/// than weakening it.
///
/// So an unreachable result is folded into the *existing* record rather than
/// replacing it: `load` keeps every data field the last reachable read
/// produced and only ever flips `reachable` itself, on whichever record is
/// newer. This was chosen over holding two separate values (last-good plus
/// current-reachability) because every caller here wants exactly one
/// `DeviceInfoRecord` per udid to render a row from, and a second field to
/// consult alongside it is one more place a caller could read the wrong half.
/// The cost is real and accepted: a phone that goes unreachable and *stays*
/// unreachable still shows its last-known battery percentage, stale, forever
/// — there is no "last seen" timestamp in the wire format for this store to
/// age that out with. A future `DeviceInfoRecord` carrying one would be the
/// right place to add a genuine staleness marker; today the tradeoff favors
/// not blanking a strip over a single missed handshake.
///
/// ## The poll this store is read by, and what it is not for
///
/// Storage barely moves, and name, model, iOS version and LiveContainer state
/// do not move at all while a pane is open. **Battery is the one value on this
/// pane that goes stale in ordinary use** — it drains while you look at it,
/// and it changes the moment a cable goes in — so before the poll the strip
/// was only ever as fresh as the last time you switched to this phone or
/// pressed Refresh. `DeviceDetailView.defaultVitalsPollInterval` holds the
/// reasoning for the 60 seconds, and the poll's own `.task` holds the
/// reasoning for the loop's shape; this is the half of it the store states.
///
/// **Re-measured, because the numbers this design was written against were
/// not reproduced.** The version above quoted 0.44s over USB and 0.78s over
/// Wi-Fi. On 2026-08-16, with both bench phones on the network and neither on
/// USB, `--device-info` against the iPhone 15 Pro came back at a **median of
/// 1.02s** — about 30% above the 0.78s figure, not below it. The USB number
/// could not be checked at all that session and is left as inherited rather
/// than restated as measured.
///
/// **Eighteen attempts, not seventeen**, and that correction matters because
/// the first version of this block got its own arithmetic wrong: it gave
/// seventeen runs in a range of "0.87–1.13s" and then called the 77.7-second
/// stall below *one of those seventeen*, which cannot both be true — 77.7 is
/// not in that range. Seventeen runs returned inside 0.87–1.13s, and one
/// stalled; splitting them is the only reading under which both figures
/// survive, and it is what the median is consistent with. The raw timings were
/// not kept, so this reconciles the two numbers rather than re-measuring them.
/// What the loop is built against is the stall, whose magnitude is not in
/// doubt.
///
/// Two things that only showed up on re-measuring, both of which shaped the
/// loop rather than the interval:
///
/// - **One attempt took 77.7 seconds.** A lockdown query over Wi-Fi can stall
///   for far longer than any interval worth choosing, so the poll sleeps
///   *after* its read returns rather than on a wall clock. A stalled read
///   delays the next tick instead of stacking a second query behind the first,
///   and `CLIRunner` SIGTERMs the child when the task is canceled, so a stall
///   does not survive the window losing focus.
///
///   One case that reasoning stops short of, stated rather than fixed: the
///   header's Refresh calls `load(udid:)` too, with no in-flight guard here
///   and no request ordering, so a stalled poll read begun *before* a Refresh
///   can land *after* it and overwrite the fresh reading with a stale one. It
///   self-corrects on the next tick, and the values involved move by about a
///   point a minute, so it is left alone; ordering it would mean a per-udid
///   sequence number and a rule for which of two in-flight answers wins, which
///   is more machinery than a one-tick-old battery percentage is worth.
/// - **A phone can be listed and still be unreachable.** The iPhone 13 Pro
///   answered `--devices` and then returned `reachable: false` on all five
///   `--device-info` attempts, in 0.36–0.46s each. That timing is the cost of
///   a handshake that *fails*, and it is close enough to the inherited 0.44s
///   USB figure to be mistaken for it — they measure different things.
///
/// At the 60s interval that was chosen, 1.02s per read is a **1.7% duty
/// cycle** for the one pane being looked at. Note "looked at": the poll is
/// gated on `controlActiveState` exactly as `ContentView`'s device poll is, so
/// **an app sitting in the background pays nothing.** That much is certain —
/// no window is key while the app is not frontmost.
///
/// What the same gate is worth for a *second window* is left as a mechanism
/// rather than a claim, because two windows were never opened to check. The
/// SDK this builds against (Xcode 26.6) documents `controlActiveState` as,
/// since macOS 15.0, "strictly mapped to and from `EnvironmentValues
/// .appearsActive`" — true reads `.key`, false reads `.inactive`, and
/// `.active` is not produced at all — and documents `appearsActive` as
/// following the focused ("key") window, with exceptions for chrome like a
/// toolbar in the main window. Read together those say a second, non-key
/// window's pane reads `.inactive` and stops, which is what this gate was
/// originally claimed to buy. It is written down here as the reason to expect
/// that, not as an observation of it.
@MainActor
final class DeviceInfoStore: ObservableObject {
    /// Vitals per device, keyed by udid. A udid with no entry means `load`
    /// has never completed for it, not that the phone was checked and found
    /// to have nothing — see the type doc for why an unreachable result
    /// updates an existing entry rather than removing or replacing it.
    @Published private(set) var info: [String: DeviceInfoRecord] = [:]

    private let runner: CLIRunner

    /// One seam, deliberately: this store takes a runner and `load` takes a
    /// per-call `argsOverride`, and that is all.
    ///
    /// A `cliArgs:` parameter briefly lived here — the shape
    /// `ConfigStore(runner:cliArgs:)` and `TweaksStore(runner:cliArgs:)` carry
    /// — so that `DeviceVitalsPollTests` could reach the *poll's* call, which
    /// passes no override and runs on a timer inside a view. It was removed,
    /// and the reasons are worth keeping because the same argument will be
    /// made again:
    ///
    /// - Those two stores' `cliArgs` default to the **production** args, so
    ///   the parameter carries the real value in the app. This one had to be
    ///   `[String]?` and permanently nil, because the real args name a udid
    ///   not known until the call — which made `load`'s expression a
    ///   three-term chain with a dead middle.
    /// - It was a *second* args seam beside `argsOverride`, which is still
    ///   live and has real callers.
    /// - Worst, it was the reason the poll's argv had no coverage: a test
    ///   going through `cliArgs` bypasses the arg-building below, so it can
    ///   only ever assert its own fixture. Pointing `CLIRunner` at a stub
    ///   script instead — the pattern in `SettingsStoreTests`,
    ///   `DeliveryStateTests`, `ConfigStoreTests`, `AppOptionsDraftTests` and
    ///   `TweakUpdatesTests` — costs no production surface and makes the argv
    ///   the tests see the argv the CLI would get. `DeviceVitalsPollTests`
    ///   now pins `--json --device-info --device <udid>` that way.
    init(runner: CLIRunner) {
        self.runner = runner
    }

    /// Run `--device-info --device <udid>` and store whatever it reports.
    ///
    /// A run that emits no `device-info` event — a non-zero exit, a
    /// subprocess that never launched, a CLI too old to know the flag —
    /// leaves `info` exactly as it was. See the type doc for why that is
    /// swallowed rather than surfaced, and for why a `reachable: false` event
    /// merges into the prior record instead of overwriting it.
    func load(udid: String, argsOverride: [String]? = nil) async {
        let args = argsOverride ?? [Flag.json, Flag.deviceInfo, Flag.device, udid]
        guard let events = try? await runner.runEvents(args, failing: "Load device info") else {
            return
        }
        for event in events {
            guard case let .deviceInfo(record) = event else { continue }
            if record.reachable {
                if record.developerMode == nil, let known = info[record.udid]?.developerMode {
                    // A phone that answered but couldn't say (locked, or too
                    // slow on that one key) keeps the last Developer Mode
                    // reading, as a blip keeps every field below: otherwise
                    // the row comes and goes with the vitals poll. The rest
                    // is the new reading.
                    info[record.udid] = DeviceInfoRecord(
                        udid: record.udid,
                        reachable: true,
                        totalBytes: record.totalBytes,
                        freeBytes: record.freeBytes,
                        batteryPercent: record.batteryPercent,
                        batteryCharging: record.batteryCharging,
                        developerMode: known
                    )
                } else {
                    info[record.udid] = record
                }
            } else if let previous = info[record.udid] {
                // Keep every data field the last reachable read produced;
                // only `reachable` itself moves, so the strip keeps showing
                // the last-known values instead of blanking on a blip.
                info[record.udid] = DeviceInfoRecord(
                    udid: previous.udid,
                    reachable: false,
                    totalBytes: previous.totalBytes,
                    freeBytes: previous.freeBytes,
                    batteryPercent: previous.batteryPercent,
                    batteryCharging: previous.batteryCharging,
                    developerMode: previous.developerMode
                )
            } else {
                // No prior reading to preserve — this is the first thing
                // this store has ever heard about this udid, so the
                // unreachable record (all-nil data fields) is stored as-is.
                info[record.udid] = record
            }
        }
    }
}
