import XCTest
import SwiftUI
@testable import Graft

/// The device pane's vitals poll: that it repeats, that it starts and stops
/// with the window's focus, that it follows the sidebar onto another phone,
/// that being refocused repeatedly does not starve it, and that it asks the
/// CLI the question it is supposed to ask.
///
/// **The evidence is a log of subprocess argv**, one line per run, appended by
/// a stub the store's `CLIRunner` is pointed at. Two properties come out of the
/// same file. The line *count* is the instrument `AppDetailLayoutTests` uses,
/// and it is here for a sharper version of the same reason:
/// `DeviceInfoStore.info` cannot tell a poll from a pane that loaded once,
/// because every read in these cases returns the same canned record, so the
/// store's contents are byte-for-byte identical after one load and after
/// twenty. The line *contents* are the argv the CLI would have been run with —
/// production output, built by `DeviceInfoStore.load`, not a fixture handed to
/// it. An earlier version of this file injected the args through a `cliArgs:`
/// parameter on the store, which meant no test anywhere could see the real
/// argv; the stub-script pattern (`SettingsStoreTests`, `DeliveryStateTests`,
/// `ConfigStoreTests`, `AppOptionsDraftTests`, `TweakUpdatesTests`) costs no
/// production surface and shows it.
///
/// **The trap this file is written around:** a timer test that asserts "a load
/// happened" proves nothing, because the pane's `.task(id: device.udid)` loads
/// unconditionally on appearance and would satisfy that assertion with the poll
/// deleted. So the positive case below does not assert a count — it asserts
/// **growth between two samples both taken well after the initial load**. An
/// initial load is a one-time event; it contributes to the first sample and can
/// contribute nothing to the difference. A count that keeps climbing after the
/// pane has settled is the timer and nothing else.
///
/// **The second trap, which cost this suite two surviving mutants:** the poll's
/// `.task(id:)` key has two fields, and a case that hosts a pane at one fixed
/// device and one fixed active state never changes either of them. Both halves
/// of `VitalsPollKey` could be frozen to a constant and every case still
/// passed. What the key is *for* is transitions, so the cases below make them:
/// focus goes away and comes back, and the sidebar moves to another phone
/// through the same hosted view.
@MainActor
final class DeviceVitalsPollTests: XCTestCase {

    // MARK: - Fixtures

    private static let udid = "00008030-0000000000000003"
    private static let otherUDID = "00008030-0000000000000001"

    private static func deviceLine(udid: String, name: String, model: String) -> String {
        #"{"type":"device","udid":"\#(udid)","name":"\#(name)","model":"\#(model)","ios_version":"16.3.1","connection_type":"USB","livecontainer_bundle_id":null,"is_default":false}\n"#
    }

    /// Two phones, always. The device-switch case needs a second one to move
    /// to, and the rest simply take the first — a fixture that grows a phone
    /// per case is a fixture nobody reads.
    private static let deviceJSON =
        deviceLine(udid: udid, name: "iPhone 13 Pro", model: "iPhone14,2")
        + deviceLine(udid: otherUDID, name: "iPhone 15 Pro", model: "iPhone16,1")
        + #"{"type":"devices-end","count":2,"failed":0}\n"#

    /// The argv `--device-info` is supposed to be run with, spelled out here
    /// rather than assembled from `CLIContract` — a test that builds the
    /// expected value the same way the code does agrees with the code by
    /// construction, including about a typo.
    private static func vitalsArgv(_ udid: String) -> [String] {
        ["--json", "--device-info", "--device", udid]
    }

    private func sh() -> CLIRunner {
        CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
    }

    /// A path the stub appends one line of argv to per run.
    private func runLog() -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("device-info-runs-\(UUID().uuidString)")
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }

    /// Every `--device-info` run so far, as its argv.
    ///
    /// A missing file is no runs — the stub creates it on the first append.
    /// One `printf` per process, built up in a shell variable first, so two
    /// overlapping runs cannot interleave halves of a line into it.
    private func invocations(_ log: URL) -> [[String]] {
        guard let text = try? String(contentsOf: log, encoding: .utf8) else { return [] }
        return text.split(separator: "\n", omittingEmptySubsequences: true).map {
            $0.split(separator: " ", omittingEmptySubsequences: true).map(String.init)
        }
    }

    private func spawns(_ log: URL) -> Int { invocations(log).count }

    /// The vitals store the pane is handed, wired to a stub `graft` that
    /// records what it was asked and answers as the real one would.
    ///
    /// The answer is built from the *last* argument rather than hard-coded, so
    /// the record's udid is the udid that was requested. That is what the CLI
    /// does, and it keeps the device-switch case honest: a poll still querying
    /// the previous phone would file its readings under the previous phone.
    private func infoStore(log: URL) throws -> DeviceInfoStore {
        let path = NSTemporaryDirectory() + "device_info_stub_\(UUID().uuidString).sh"
        let script = """
        #!/bin/sh
        line=""
        last=""
        for arg in "$@"; do line="$line $arg"; last="$arg"; done
        printf '%s\\n' "$line" >> "\(log.path)"
        printf '{"type":"device-info","udid":"%s","reachable":true,"total_bytes":128000000000,"free_bytes":35728093184,"battery_percent":70,"battery_charging":false}\\n' "$last"
        """
        try script.write(toFile: path, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: path)
        addTeardownBlock { try? FileManager.default.removeItem(atPath: path) }
        return DeviceInfoStore(runner: CLIRunner(executable: URL(fileURLWithPath: path)))
    }

    private func devicesStore() async -> DevicesStore {
        let store = DevicesStore(runner: sh())
        _ = try? await store.load(argsOverride: ["-c", "printf '\(Self.deviceJSON)'"])
        return store
    }

    /// The pane, hosted with a device and an active state a test can set.
    ///
    /// `controlActiveState` is injected rather than produced by making the
    /// window key: a test process's window is not reliably frontmost, so the
    /// positive cases could not otherwise depend on the poll running at all,
    /// and the negative ones could pass for the wrong reason. Injecting it
    /// makes the gate itself the thing under test.
    private struct Harness: View {
        let device: DeviceRecord
        let devices: DevicesStore
        let info: DeviceInfoStore
        let config: ConfigStore
        let interval: UInt64
        let active: ControlActiveState

        var body: some View {
            DeviceDetailView(
                device: device,
                store: devices,
                deviceInfo: info,
                config: config,
                delivery: DeviceDeliveryStore(runner: config.runner),
                artwork: ArtworkStore(runner: config.runner),
                deviceApps: DeviceAppsStore(runner: config.runner),
                staged: StagedIPAStore(runner: config.runner),
                liveContainer: LiveContainerStore(runner: config.runner),
                // No sidebar to navigate in this harness — it exists to
                // watch the vitals poll's timing, not row selection.
                onSelectApp: { _ in },
                vitalsPollInterval: interval
            )
            .environment(\.controlActiveState, active)
        }
    }

    /// A hosted pane, held together with the stores behind it so a case can
    /// show it again with one thing changed.
    ///
    /// `show(...)` assigns `host.rootView`, which is `AppDetailLayoutTests`'s
    /// instrument for walking a pane through a sidebar click, and it is the
    /// only way to make the transitions these cases are about: it hands SwiftUI
    /// a new *value* for the same view, which is what a selection change and a
    /// window losing focus both are. Building a second `NSHostingView` instead
    /// would give the pane a fresh identity and restart every task in it
    /// unconditionally — which is the behaviour under test, so it would prove
    /// nothing.
    ///
    /// A case must hold this until it is done: an `NSWindow` released mid-test
    /// takes its view tree, and the running task, with it. Every case ends with
    /// `withExtendedLifetime`. `AppDetailLayoutTests` holds its window for the
    /// same reason by a different mechanism — it returns the window and writes
    /// `_ = window` — so this is the same intent spelled differently, not a
    /// pattern copied from there.
    @MainActor
    private final class Pane {
        let window: NSWindow
        let devices: [DeviceRecord]
        private(set) var device: DeviceRecord
        private let host: NSHostingView<Harness>
        private let store: DevicesStore
        private let info: DeviceInfoStore
        private let config: ConfigStore
        private let interval: UInt64

        init(
            store: DevicesStore, info: DeviceInfoStore, config: ConfigStore,
            interval: UInt64, device: DeviceRecord, active: ControlActiveState
        ) {
            self.store = store
            self.devices = store.devices
            self.info = info
            self.config = config
            self.interval = interval
            self.device = device
            self.host = NSHostingView(
                rootView: Harness(
                    device: device, devices: store, info: info, config: config,
                    interval: interval, active: active
                )
            )
            self.window = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 900, height: 700),
                styleMask: [.titled, .resizable], backing: .buffered, defer: false
            )
            window.contentView = host
            host.frame = NSRect(x: 0, y: 0, width: 900, height: 700)
            host.layoutSubtreeIfNeeded()
            window.displayIfNeeded()
        }

        /// Show a phone at an active state — the two halves of `VitalsPollKey`,
        /// which is the only thing these cases ever change.
        func show(_ device: DeviceRecord, active: ControlActiveState) {
            self.device = device
            host.rootView = Harness(
                device: device, devices: store, info: info, config: config,
                interval: interval, active: active
            )
            host.layoutSubtreeIfNeeded()
            window.displayIfNeeded()
        }
    }

    private func hostPane(
        log: URL, interval: UInt64, active: ControlActiveState
    ) async throws -> Pane {
        let store = await devicesStore()
        XCTAssertEqual(
            store.devices.count, 2,
            "fixture must produce two phones — the device-switch case needs somewhere to go"
        )
        let device = try XCTUnwrap(
            store.devices.first, "fixture produced no device to point the pane at"
        )
        return Pane(
            store: store,
            info: try infoStore(log: log),
            config: ConfigStore(runner: sh(), cliArgs: ["-c", "true"]),
            interval: interval,
            device: device,
            active: active
        )
    }

    /// Poll a condition to a deadline. Polled rather than slept on a fixed
    /// interval, because what it waits for is a subprocess.
    private func waitUntil(
        _ condition: @autoclosure () -> Bool, timeout: TimeInterval = 5
    ) async throws -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() && Date() < deadline {
            try await Task.sleep(nanoseconds: 20_000_000)
        }
        return condition()
    }

    /// The interval the live cases run at: fast enough that a case finishes in
    /// a few seconds, slow enough that `/bin/sh` (a few milliseconds a spawn)
    /// is not what the counts are measuring.
    private static let testInterval: UInt64 = 200_000_000  // 0.2s

    // MARK: - It repeats

    /// The poll keeps reading, which is the whole feature.
    ///
    /// Deliberately **not** asserted as "more than one load happened": the
    /// pane's `.task(id: device.udid)` loads on appearance, so that assertion
    /// is satisfiable by a burst at startup and would survive the poll being
    /// deleted if anything else ever loaded twice. What is asserted instead is
    /// that the count is still climbing in a window that starts a second after
    /// the pane opened — a one-time initial load cannot contribute to a
    /// *difference* between two later samples.
    func test_a_visible_pane_keeps_re_reading_its_vitals() async throws {
        let log = runLog()
        let pane = try await hostPane(log: log, interval: Self.testInterval, active: .key)

        // Seven intervals in, then seven more.
        try await Task.sleep(nanoseconds: 1_400_000_000)
        let first = spawns(log)
        try await Task.sleep(nanoseconds: 1_400_000_000)
        let second = spawns(log)

        XCTAssertGreaterThanOrEqual(
            first, 4,
            "premise: the pane loaded once on appearance and the poll has had seven "
            + "intervals to fire — a count of 1 here is a pane that loads and never "
            + "looks again, which is the defect this poll exists to fix"
        )
        XCTAssertGreaterThanOrEqual(
            second - first, 3,
            "the count must still be climbing a second and a half after the pane "
            + "opened. This is the assertion that distinguishes a running timer from "
            + "the initial load: an initial load is one event, and can add nothing to "
            + "the difference between two samples taken after it"
        )
        // An interval mutated far below the injected one shows up here rather
        // than as a missing tick. Fourteen intervals have elapsed; the loop
        // also awaits a subprocess per tick, so it cannot outrun the clock by
        // much, and a generous ceiling still catches an interval of zero.
        XCTAssertLessThan(
            second, 60,
            "fourteen intervals cannot produce sixty reads — a count this high means "
            + "the interval is not being waited on"
        )
        withExtendedLifetime(pane) {}
    }

    // MARK: - The argv

    /// What the poll actually asks the CLI.
    ///
    /// Nothing anywhere else pins `--device-info`'s argv, and until the
    /// `cliArgs:` seam came off this store nothing *could*: a test that hands
    /// the store its args cannot also check them. Every line in the log is
    /// asserted rather than the last one, so a poll that names the right phone
    /// once and something else afterwards fails here.
    func test_every_vitals_read_sends_the_production_argv() async throws {
        let log = runLog()
        let pane = try await hostPane(log: log, interval: Self.testInterval, active: .key)
        try await Task.sleep(nanoseconds: 800_000_000)

        let runs = invocations(log)
        XCTAssertGreaterThanOrEqual(
            runs.count, 2,
            "premise: the appearance load and at least one poll read have both run"
        )
        XCTAssertEqual(
            runs.filter { $0 != Self.vitalsArgv(pane.device.udid) }, [],
            "every read of this pane's vitals must be exactly "
            + "--json --device-info --device <the selected phone>"
        )
        withExtendedLifetime(pane) {}
    }

    // MARK: - It stops when nobody is looking

    /// A pane in a window that is not frontmost must not poll.
    ///
    /// The count asserted is **exactly one**, not zero, and that is deliberate:
    /// the pane's `.task(id: device.udid)` is ungated on purpose, so a pane
    /// pointed at a phone still fills its strip once even in a background
    /// window. Removing the poll's `controlActiveState` gate turns this 1 into
    /// roughly 11.
    ///
    /// This case and the two below it are three different questions. This one
    /// is about a poll that *starts* unlooked-at; the two transition cases are
    /// about a poll that has to stop and start again, which the gate alone
    /// cannot do — only the key can.
    func test_a_pane_in_an_unfocused_window_does_not_poll() async throws {
        let log = runLog()
        let pane = try await hostPane(log: log, interval: Self.testInterval, active: .inactive)

        let loaded = try await waitUntil(spawns(log) >= 1)
        XCTAssertTrue(
            loaded,
            "premise: a pane pointed at a phone reads its vitals once even unfocused, "
            + "and without this the case below would pass on a pane that never ran"
        )
        // Ten intervals of quiet.
        try await Task.sleep(nanoseconds: 2_000_000_000)

        XCTAssertEqual(
            spawns(log), 1,
            "a window nobody is looking at must not spawn a lockdown query every "
            + "interval — the pane's one load on appearance is the whole budget"
        )
        withExtendedLifetime(pane) {}
    }

    // MARK: - The transitions the key exists for

    /// A window that goes to the background stops polling.
    ///
    /// The `guard` at the top of the poll cannot do this on its own: it runs
    /// once, when the task starts, so it only ever gates a poll that *began*
    /// unfocused. Ending one that is already running takes `.task(id:)` seeing
    /// a changed key, which is what `VitalsPollKey.active` is for — freeze that
    /// field to a constant and a window sent to the background keeps querying
    /// the phone every minute forever, with nothing on screen to say so.
    func test_a_window_going_to_the_background_stops_the_poll() async throws {
        let log = runLog()
        let pane = try await hostPane(log: log, interval: Self.testInterval, active: .key)

        // Five intervals of being watched.
        try await Task.sleep(nanoseconds: 1_000_000_000)
        XCTAssertGreaterThanOrEqual(
            spawns(log), 3,
            "premise: the poll was running before the window lost focus — without this "
            + "the assertion below would pass on a pane that never polled at all"
        )

        pane.show(pane.device, active: .inactive)
        // Two intervals for the read that was in flight to land and the task to
        // come down, so the sample below is the count with nothing running.
        try await Task.sleep(nanoseconds: 400_000_000)
        let atHandover = spawns(log)

        // Ten intervals of not being watched.
        try await Task.sleep(nanoseconds: 2_000_000_000)

        XCTAssertEqual(
            spawns(log), atHandover,
            "a poll that was running when the window went to the background must stop "
            + "— every read past this point is a lockdown query against a phone for a "
            + "strip nobody is looking at"
        )
        withExtendedLifetime(pane) {}
    }

    /// A window that comes forward starts polling.
    ///
    /// The other direction, and the one no `guard` can rescue: a task that
    /// returned at the gate is finished, and only a changed key starts a new
    /// one. Freeze `VitalsPollKey.active` and the pane you switch back to never
    /// polls again for as long as it stays selected.
    func test_a_window_coming_forward_starts_the_poll() async throws {
        let log = runLog()
        let pane = try await hostPane(log: log, interval: Self.testInterval, active: .inactive)

        let loaded = try await waitUntil(spawns(log) >= 1)
        XCTAssertTrue(loaded, "premise: the pane's appearance load ran")
        try await Task.sleep(nanoseconds: 1_000_000_000)
        XCTAssertEqual(
            spawns(log), 1,
            "premise: nothing was polling while the window was in the background"
        )

        pane.show(pane.device, active: .key)
        // Seven intervals of being watched.
        try await Task.sleep(nanoseconds: 1_400_000_000)

        XCTAssertGreaterThanOrEqual(
            spawns(log), 4,
            "bringing the window forward must start the poll — a strip that only "
            + "refreshes if the window happened to be frontmost when the pane opened "
            + "is stale for exactly the person who came back to look at it"
        )
        withExtendedLifetime(pane) {}
    }

    /// Selecting another phone moves the poll to it.
    ///
    /// `ContentView` builds this pane without an `.id(...)`, so a sidebar click
    /// between two phones hands the *same* view a new `device` — which is what
    /// `show(...)` does here. The poll's task closure captured the old value,
    /// so unless the key changes, the loop that is already running keeps
    /// querying the phone you just navigated away from, forever, while the
    /// pane in front of you shows another one. Freezing `VitalsPollKey.udid`
    /// leaves exactly that, and it is invisible in `DeviceInfoStore.info`:
    /// both phones get records, they are simply the wrong ones being refreshed.
    /// The argv is what can see it.
    func test_switching_phones_moves_the_poll_to_the_new_phone() async throws {
        let log = runLog()
        let pane = try await hostPane(log: log, interval: Self.testInterval, active: .key)
        let first = pane.device
        let second = try XCTUnwrap(pane.devices.last)
        XCTAssertNotEqual(first.udid, second.udid, "premise: two distinct phones")

        try await Task.sleep(nanoseconds: 800_000_000)
        let before = invocations(log)
        XCTAssertGreaterThanOrEqual(
            before.count, 3, "premise: the poll is running on the first phone"
        )
        XCTAssertEqual(
            before.filter { $0 != Self.vitalsArgv(first.udid) }, [],
            "premise: everything so far named the first phone"
        )

        pane.show(second, active: .key)
        // Three intervals for the first phone's task to be cancelled and
        // whatever it had in flight to land, so what follows is only what the
        // switch left running.
        try await Task.sleep(nanoseconds: 600_000_000)
        let settled = spawns(log)
        // Seven more.
        try await Task.sleep(nanoseconds: 1_400_000_000)

        let after = Array(invocations(log).dropFirst(settled))
        XCTAssertGreaterThanOrEqual(
            after.count, 3,
            "the poll must still be running after a sidebar click, not left behind "
            + "with the phone it started on"
        )
        XCTAssertEqual(
            after.filter { $0 != Self.vitalsArgv(second.udid) }, [],
            "every read after the switch must name the phone now on screen — a read "
            + "naming the previous one is a loop that outlived its selection"
        )
        withExtendedLifetime(pane) {}
    }

    // MARK: - It sleeps before its first read

    /// The ordering choice, made visible.
    ///
    /// `ContentView`'s device poll reads *before* its first sleep, because it
    /// is the only thing that loads the device list. This poll must do the
    /// opposite: the pane's `.task(id: device.udid)` already reads vitals on
    /// appearance, so a poll that also read before sleeping would run two
    /// simultaneous `--device-info` queries against the same phone on every
    /// pane open.
    ///
    /// A three-second interval against a window of one second: if the poll read
    /// first, the count would be 2 well before this case looks.
    func test_the_poll_sleeps_before_its_first_read() async throws {
        let log = runLog()
        let pane = try await hostPane(log: log, interval: 3_000_000_000, active: .key)

        let loaded = try await waitUntil(spawns(log) >= 1)
        XCTAssertTrue(loaded, "premise: the pane's appearance load ran")
        try await Task.sleep(nanoseconds: 1_000_000_000)

        XCTAssertEqual(
            spawns(log), 1,
            "opening a pane must cost one --device-info, not two. A poll that read "
            + "before its first sleep would double every pane open, racing the "
            + "appearance load for the same reading"
        )
        withExtendedLifetime(pane) {}
    }

    /// The same property, for the other event that starts an appearance load:
    /// a sidebar click onto a different phone.
    ///
    /// This is what `VitalsReading.udid` is for. `nextVitalsDelay()` waits the
    /// *remainder* of the interval measured from the last reading, and after a
    /// switch the only reading it holds belongs to the phone that is no longer
    /// on screen. Carrying that remainder across the switch would fire the
    /// poll's first read for the new phone up to immediately — beside the
    /// appearance load already fetching the same value, which is the double
    /// query the case above exists to prevent.
    ///
    /// Two seconds of interval against a switch made at 1.9s, so a remainder
    /// carried over would be 0.1s and a fresh interval 2.0s; the count is taken
    /// a full second after the earlier of those and a second before the later.
    func test_switching_phones_does_not_double_read_the_new_one() async throws {
        let log = runLog()
        let pane = try await hostPane(log: log, interval: 2_000_000_000, active: .key)
        let second = try XCTUnwrap(pane.devices.last)

        try await Task.sleep(nanoseconds: 1_900_000_000)
        let before = spawns(log)
        XCTAssertEqual(
            before, 1,
            "premise: one appearance load and no poll read yet, so the reading on "
            + "screen is 1.9s of a 2s interval old"
        )

        pane.show(second, active: .key)
        try await Task.sleep(nanoseconds: 1_000_000_000)

        XCTAssertEqual(
            spawns(log) - before, 1,
            "a sidebar click must cost one --device-info for the phone clicked. Two "
            + "means the poll spent the previous phone's remaining interval on this "
            + "one and read it alongside the appearance load"
        )
        withExtendedLifetime(pane) {}
    }

    // MARK: - Refocusing must not starve it

    /// A window whose focus keeps changing must still get readings.
    ///
    /// The defect this pins is the product of two decisions that are each
    /// defensible alone: the poll's task is keyed on focus, so every focus
    /// change restarts it, and it sleeps before its first read. Compose them
    /// with a flat `sleep(interval)` and every restart begins a fresh full
    /// interval — so a window refocused more often than the interval performs
    /// **zero** reads, indefinitely. Not a stale-by-one-interval reading: no
    /// reading at all, on a timer, with nothing on screen to say it has
    /// stopped. Alt-tabbing does it, and so does anything that takes key status
    /// away and gives it back.
    ///
    /// Twelve focus changes across three intervals. Under a fresh-interval
    /// sleep the count stays at the appearance load's 1 forever; under
    /// `nextVitalsDelay()`'s remainder it reaches roughly one read per interval
    /// of wall clock, because time spent unfocused still counts against the
    /// interval.
    func test_repeated_refocus_does_not_starve_the_poll() async throws {
        let log = runLog()
        let pane = try await hostPane(log: log, interval: 1_000_000_000, active: .key)

        let loaded = try await waitUntil(spawns(log) >= 1)
        XCTAssertTrue(loaded, "premise: the pane's appearance load ran")

        for step in 0..<12 {
            try await Task.sleep(nanoseconds: 250_000_000)
            pane.show(pane.device, active: step.isMultiple(of: 2) ? .inactive : .key)
        }

        XCTAssertGreaterThanOrEqual(
            spawns(log), 2,
            "three intervals of wall clock passed and the window was in front for half "
            + "of it, so the strip must have been re-read at least once. A count of 1 "
            + "is a poll whose sleep is restarted from zero by every focus change and "
            + "therefore never fires"
        )
        withExtendedLifetime(pane) {}
    }

    // MARK: - The number itself

    /// The shipped interval is the one the reasoning was written for.
    ///
    /// Thin on purpose, and it guards something the live cases above cannot:
    /// they all inject an interval, so every one of them passes unchanged if
    /// the default is quietly moved to 5 seconds — which would be a lockdown
    /// query every five seconds against a phone on Wi-Fi, and the exact cost
    /// `DeviceInfoStore`'s type doc records as the thing not to do.
    func test_the_shipped_poll_interval_is_sixty_seconds() {
        XCTAssertEqual(
            DeviceDetailView.defaultVitalsPollInterval, 60_000_000_000,
            "60s is argued for in `defaultVitalsPollInterval`'s doc comment against a "
            + "measured 1.02s median read; changing the number means changing that"
        )
    }

    /// …and the app is wired to it.
    ///
    /// The case above pins the constant, which leaves one edit uncaught: the
    /// init's `vitalsPollInterval: UInt64 = DeviceDetailView.defaultVitalsPollInterval`
    /// could be changed to a literal and every other case here still passes,
    /// because `ContentView` is the only caller that omits the argument and no
    /// case here goes through it. This builds the pane the way `ContentView`
    /// does — no interval — and reads back what it got.
    func test_a_pane_built_without_an_interval_polls_at_the_shipped_one() async throws {
        let store = await devicesStore()
        let device = try XCTUnwrap(store.devices.first)
        let config = ConfigStore(runner: sh(), cliArgs: ["-c", "true"])
        let pane = DeviceDetailView(
            device: device,
            store: store,
            deviceInfo: DeviceInfoStore(runner: sh()),
            config: config,
            delivery: DeviceDeliveryStore(runner: config.runner),
            artwork: ArtworkStore(runner: config.runner),
            deviceApps: DeviceAppsStore(runner: config.runner),
            staged: StagedIPAStore(runner: config.runner),
            liveContainer: LiveContainerStore(runner: config.runner),
            // No sidebar to navigate — this case only reads back
            // `vitalsPollInterval`.
            onSelectApp: { _ in }
        )

        XCTAssertEqual(
            pane.vitalsPollInterval, DeviceDetailView.defaultVitalsPollInterval,
            "a pane built the way ContentView builds it must poll at the shipped "
            + "interval — the default is the only thing that puts the argued-for "
            + "number into the app"
        )
    }
}
