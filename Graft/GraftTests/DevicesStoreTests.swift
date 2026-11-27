import XCTest
@testable import Graft

final class DevicesStoreTests: XCTestCase {
    @MainActor
    func test_load_parses_device_records() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let script = #"""
        printf '{"type":"device","udid":"00008130-001A2B3C0123456D","name":"Test iPhone","model":"iPhone14,2","ios_version":"26.0.1","connection_type":"USB","livecontainer_bundle_id":"com.kdt.livecontainer.ABCDE12345"}\n{"type":"device","udid":"00008101-000ABCDEF0123456","name":"Spare","model":"iPad13,1","ios_version":"17.5","connection_type":"Network","livecontainer_bundle_id":null}\n{"type":"devices-end","count":2}\n'
        """#
        let store = DevicesStore(runner: runner)
        let devices = try await store.load(argsOverride: ["-c", script])
        XCTAssertEqual(devices.count, 2)
        XCTAssertEqual(devices[0].udid, "00008130-001A2B3C0123456D")
        XCTAssertEqual(devices[0].livecontainerBundleId, "com.kdt.livecontainer.ABCDE12345")
        XCTAssertEqual(devices[0].connectionType, .usb)
        XCTAssertEqual(devices[1].connectionType, .network)
        XCTAssertNil(devices[1].livecontainerBundleId)
        XCTAssertTrue(store.hasLoaded)
    }

    /// The regression this guards: a successful scan that finds nothing is the
    /// common case (no phone plugged in), and it leaves `devices` empty and
    /// `lastError` nil. A result-shaped "have we loaded?" test reads identically
    /// before and after that scan, so the caller re-spawns the CLI forever.
    @MainActor
    func test_hasLoaded_is_true_after_a_successful_but_empty_scan() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = DevicesStore(runner: runner)
        XCTAssertFalse(store.hasLoaded, "nothing has been attempted yet")

        let devices = try await store.load(
            argsOverride: ["-c", #"printf '{"type":"devices-end","count":0}\n'"#]
        )

        XCTAssertTrue(devices.isEmpty)
        XCTAssertNil(store.lastError)
        XCTAssertTrue(store.hasLoaded)
        XCTAssertFalse(store.isLoading)
    }

    // MARK: - devices usbmux saw but couldn't describe
    //
    // `--devices` exits 0 in this case and lists the phones it *could* read,
    // so there is no failure for a caller to notice, and the CLI's stderr
    // warning is discarded by a zero-exit run. The `device-error` event is the
    // only channel that reaches the GUI — and for one release it reached
    // nothing: no `BuildEvent.decode` case, so it became `.unknown`, and
    // `load` kept only `.device`.

    /// A locked phone, alone on the bus. Everything downstream reads an empty
    /// `devices` as "nothing is plugged in", which is the wrong problem and
    /// the wrong fix.
    private static let lockedPhoneOnly = #"""
    printf '{"type":"device-error","udid":"00008130-001A2B3C0123456D","error":"lockdown query failed: device is passcode locked"}\n{"type":"devices-end","count":0,"failed":1}\n'
    """#

    @MainActor
    func test_load_reports_a_device_it_could_not_describe() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = DevicesStore(runner: runner)

        let devices = try await store.load(argsOverride: ["-c", Self.lockedPhoneOnly])

        XCTAssertTrue(devices.isEmpty, "premise: it couldn't be described, so it isn't listed")
        XCTAssertNil(store.lastError, "premise: --devices exited 0, so nothing threw")
        XCTAssertEqual(
            store.deviceErrors.map(\.udid), ["00008130-001A2B3C0123456D"],
            "an empty devices list plus no error reads as 'no phone is plugged in' — "
            + "which sends the user looking for a cable that is already fine"
        )
        XCTAssertEqual(
            store.deviceErrors.first?.error,
            "lockdown query failed: device is passcode locked"
        )
    }

    /// The mixed case: one phone answers, one doesn't. The working one must
    /// still land in `devices` (it is what the build picker offers), and the
    /// broken one must not be silently absorbed into "1 connected".
    @MainActor
    func test_load_keeps_both_halves_of_a_partial_scan() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let script = #"""
        printf '{"type":"device","udid":"GOOD-UDID","name":"Test iPhone","model":"iPhone14,2","ios_version":"26.0.1","connection_type":"USB","livecontainer_bundle_id":null}\n{"type":"device-error","udid":"BAD-UDID","error":"connection refused"}\n{"type":"devices-end","count":1,"failed":1}\n'
        """#
        let store = DevicesStore(runner: runner)

        let devices = try await store.load(argsOverride: ["-c", script])

        XCTAssertEqual(devices.map(\.udid), ["GOOD-UDID"])
        XCTAssertEqual(store.deviceErrors.map(\.udid), ["BAD-UDID"])
    }

    /// A device that starts answering again has to drop off the problem list
    /// in the same pass it rejoins the real one — otherwise Refresh only ever
    /// adds warnings.
    @MainActor
    func test_a_rescan_replaces_the_previous_failures() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = DevicesStore(runner: runner)
        _ = try await store.load(argsOverride: ["-c", Self.lockedPhoneOnly])
        XCTAssertFalse(store.deviceErrors.isEmpty)

        let unlocked = #"""
        printf '{"type":"device","udid":"00008130-001A2B3C0123456D","name":"Test iPhone","model":"iPhone14,2","ios_version":"26.0.1","connection_type":"USB","livecontainer_bundle_id":null}\n{"type":"devices-end","count":1,"failed":0}\n'
        """#
        _ = try await store.load(argsOverride: ["-c", unlocked])

        XCTAssertEqual(store.devices.count, 1)
        XCTAssertTrue(store.deviceErrors.isEmpty, "the phone answered this time")
    }

    /// A rescan that *fails* has to drop them too.
    ///
    /// `deviceErrors` is not an inventory, it is an instruction — "unlock the
    /// device and tap 'Trust this Computer', then Refresh" — and pressing
    /// Refresh is how the user arrives here. When that run exits non-zero for
    /// an unrelated reason (a config.yaml the CLI now rejects, a missing
    /// `idevice_id`) neither list is reassigned, so the amber card stays up
    /// next to the red banner telling them to redo what they just did. It also
    /// gates the sidebar's Devices empty state: left non-empty, "No devices
    /// connected" is replaced by a row for a phone that is not there, carrying
    /// a retry for a scan that already ran.
    @MainActor
    func test_a_failed_rescan_drops_the_previous_scans_device_errors() async {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = DevicesStore(runner: runner)
        _ = try? await store.load(argsOverride: ["-c", Self.lockedPhoneOnly])
        XCTAssertFalse(store.deviceErrors.isEmpty, "premise: the locked phone was reported")

        do {
            _ = try await store.load(
                argsOverride: ["-c", "echo 'error: config.yaml is not readable' >&2; exit 2"]
            )
            XCTFail("premise: the refresh has to fail")
        } catch {
            XCTAssertTrue(error.localizedDescription.contains("config.yaml"))
        }

        XCTAssertTrue(
            store.deviceErrors.isEmpty,
            "the scan that would have cleared these never ran, so the card describes "
            + "a condition nothing has checked since — and suppresses the empty state"
        )
    }

    /// The deliberate other half: `devices` survives the same failure. It
    /// claims no problem and nothing keys off it being empty, while dropping
    /// it would clear the install target picked in AppDetailView because an
    /// unrelated `--devices` run failed. `lastError` is what says the list may
    /// be stale.
    @MainActor
    func test_a_failed_rescan_keeps_the_devices_the_last_scan_found() async {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let script = #"""
        printf '{"type":"device","udid":"GOOD-UDID","name":"Test iPhone","model":"iPhone14,2","ios_version":"26.0.1","connection_type":"USB","livecontainer_bundle_id":null}\n{"type":"devices-end","count":1,"failed":0}\n'
        """#
        let store = DevicesStore(runner: runner)
        _ = try? await store.load(argsOverride: ["-c", script])
        XCTAssertEqual(store.devices.map(\.udid), ["GOOD-UDID"], "premise")

        _ = try? await store.load(argsOverride: ["-c", "echo 'no usbmuxd' >&2; exit 3"])

        XCTAssertEqual(store.devices.map(\.udid), ["GOOD-UDID"])
    }

    /// The summary is what both surfaces render, so it has to name the device
    /// and say why it isn't in the list — the UDID is all we have, because the
    /// query that would have produced a name is the one that failed.
    func test_device_error_summary_names_the_udid_and_the_consequence() {
        let record = DeviceErrorRecord(udid: "00008130-XYZ", error: "connection refused")
        XCTAssertTrue(record.summary.contains("00008130-XYZ"), record.summary)
        XCTAssertTrue(record.summary.contains("connection refused"), record.summary)
        XCTAssertTrue(record.summary.contains("connected"), record.summary)
    }

    /// A failed attempt is still an attempt — otherwise a broken CLI turns
    /// every popover open into another doomed subprocess launch. Recovery is
    /// the explicit Retry/Refresh button, which calls `invalidate()`.
    @MainActor
    func test_hasLoaded_is_true_after_a_failed_scan_and_invalidate_clears_it() async {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = DevicesStore(runner: runner)

        do {
            _ = try await store.load(argsOverride: ["-c", "echo 'no usbmuxd' >&2; exit 3"])
            XCTFail("expected a non-zero exit to throw")
        } catch {
            XCTAssertTrue(error.localizedDescription.contains("no usbmuxd"))
        }

        XCTAssertTrue(store.hasLoaded, "a failed attempt still counts as attempted")
        XCTAssertFalse(store.isLoading)

        store.invalidate()
        XCTAssertFalse(store.hasLoaded)
    }

    // MARK: - Two-tier polling
    //
    // `pollAndEscalateIfChanged` and `loadBrief` share the same
    // `argsOverride` for both the cheap brief poll and, when escalating, the
    // `load()` it triggers — in production, `argsOverride` is nil on both
    // calls and each method falls back to its own real flags, but in a test
    // it means one canned script has to answer for both roles. Several of
    // these scripts print *both* a `device-brief` line (what `loadBrief`
    // reads) and a full `device` line (what an escalated `load()` reads), so
    // the very same subprocess output can prove or disprove escalation.

    @MainActor
    func test_loadBrief_returns_each_udid_with_how_it_is_attached() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = DevicesStore(runner: runner)
        let script = #"""
        printf '{"type":"device-brief","udid":"UDID-ONE","connection_type":"USB"}\n{"type":"device-brief","udid":"UDID-TWO","connection_type":"Network"}\n{"type":"devices-end","count":2,"failed":0}\n'
        """#

        let seen = try await store.loadBrief(argsOverride: ["-c", script])

        // The transport, not just the udid. `--devices --brief` has always put
        // `connection_type` on the wire and this reduced it away, which is what
        // made a USB-to-Wi-Fi swap invisible until something else forced a full
        // listing.
        XCTAssertEqual(seen, ["UDID-ONE": .usb, "UDID-TWO": .network])
    }

    private static func fullDeviceLine(
        udid: String, connection: String = "USB"
    ) -> String {
        #"{"type":"device","udid":"\#(udid)","name":"n","model":"m","ios_version":"1.0","connection_type":"\#(connection)","livecontainer_bundle_id":null}"#
    }

    /// The same phone, attached differently: same udid, so a comparison on
    /// udids alone sees no change at all and the header goes on saying "USB"
    /// until something unrelated forces a full listing. The transport is
    /// already on the wire — `--devices --brief` emits `connection_type` and
    /// `BriefDeviceRecord` decodes it — so catching this costs nothing beyond
    /// comparing what was already there.
    ///
    /// Falsifying by construction, the same way its two neighbours are: the
    /// poll script's `device` line is the only place "Network" appears, so
    /// `store.devices` can only report it if the escalated `load()` really ran.
    @MainActor
    func test_pollAndEscalateIfChanged_escalates_when_a_phone_changes_transport() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = DevicesStore(runner: runner)
        let seedScript = #"""
        printf '\#(Self.fullDeviceLine(udid: "SAME-UDID", connection: "USB"))\n{"type":"devices-end","count":1,"failed":0}\n'
        """#
        _ = try await store.load(argsOverride: ["-c", seedScript])
        XCTAssertEqual(store.devices.first?.connectionType, .usb, "premise: the seed load landed")

        let pollScript = #"""
        printf '{"type":"device-brief","udid":"SAME-UDID","connection_type":"Network"}\n\#(Self.fullDeviceLine(udid: "SAME-UDID", connection: "Network"))\n{"type":"devices-end","count":1,"failed":0}\n'
        """#
        await store.pollAndEscalateIfChanged(argsOverride: ["-c", pollScript])

        XCTAssertEqual(
            store.devices.first?.connectionType, .network,
            "the phone moved from USB to Wi-Fi and the poll did not escalate, so the header "
            + "kept naming a transport the phone is no longer on"
        )
    }

    /// Every view observing this store (the window, the sidebar, an open app
    /// pane) redraws for each change it publishes, and `@Published` publishes
    /// every assignment, equal or not. So a background scan that finds what
    /// the last one found publishes nothing: here the brief poll sees the
    /// phone on Wi-Fi, which escalates, and the full listing still says USB.
    @MainActor
    func test_a_poll_that_finds_the_same_devices_publishes_nothing() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = DevicesStore(runner: runner)
        let listing = #"\#(Self.fullDeviceLine(udid: "SAME-UDID", connection: "USB"))\n{"type":"devices-end","count":1,"failed":0}\n"#
        _ = try await store.load(argsOverride: ["-c", "printf '\(listing)'"])
        XCTAssertEqual(store.devices.map(\.udid), ["SAME-UDID"], "premise: the seed load landed")
        let changes = ObservableStoresTests.Changes(of: store)

        await store.pollAndEscalateIfChanged(argsOverride: [
            "-c", #"printf '{"type":"device-brief","udid":"SAME-UDID","connection_type":"USB"}\n'"#,
        ])
        XCTAssertEqual(changes.count, 0, "a poll that didn't escalate published")

        let escalating = #"{"type":"device-brief","udid":"SAME-UDID","connection_type":"Network"}\n"# + listing
        await store.pollAndEscalateIfChanged(argsOverride: ["-c", "printf '\(escalating)'"])
        XCTAssertEqual(store.devices.first?.connectionType, .usb, "premise: the escalated listing found the same phone")
        XCTAssertEqual(changes.count, 0, "a scan that found nothing new redrew every view observing the store")
    }

    /// And a scan that finds a change publishes it, once: the phone's new
    /// transport. A timer's scan doesn't spin the Refresh buttons
    /// (`isLoading`): someone's own Refresh does.
    @MainActor
    func test_a_poll_that_finds_a_change_publishes_it() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = DevicesStore(runner: runner)
        let seed = #"\#(Self.fullDeviceLine(udid: "SAME-UDID", connection: "USB"))\n{"type":"devices-end","count":1,"failed":0}\n"#
        _ = try await store.load(argsOverride: ["-c", "printf '\(seed)'"])
        let changes = ObservableStoresTests.Changes(of: store)

        let moved = #"{"type":"device-brief","udid":"SAME-UDID","connection_type":"Network"}\n\#(Self.fullDeviceLine(udid: "SAME-UDID", connection: "Network"))\n{"type":"devices-end","count":1,"failed":0}\n"#
        await store.pollAndEscalateIfChanged(argsOverride: ["-c", "printf '\(moved)'"])
        XCTAssertEqual(store.devices.first?.connectionType, .network, "the change didn't show")
        XCTAssertEqual(changes.count, 1, "one change, published once, with no spinner for a timer's scan")
        XCTAssertFalse(store.isLoading)
    }

    /// B1: a phone waiting on Trust is on the bus the whole time, so a poll
    /// that compared presence alone never read it again after the person
    /// tapped Trust, and its row stayed "couldn't be read". While any phone
    /// can't be read, every poll reads again: a cost paid only while one
    /// can't, and only while the window is key (`ContentView`'s timer).
    @MainActor
    func test_a_phone_that_could_not_be_described_is_read_again_on_the_next_poll() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = DevicesStore(runner: runner)
        let seedScript = #"""
        printf '\#(Self.fullDeviceLine(udid: "READABLE"))\n{"type":"device-error","udid":"LOCKED","error":"PasswordRequiredError"}\n{"type":"devices-end","count":1,"failed":1}\n'
        """#
        _ = try await store.load(argsOverride: ["-c", seedScript])
        XCTAssertEqual(store.deviceErrors.map(\.udid), ["LOCKED"], "premise: the seed load landed")

        // The same two phones on the bus, and this time the listing reads both.
        let trusted = #"""
        printf '{"type":"device-brief","udid":"READABLE","connection_type":"USB"}\n{"type":"device-brief","udid":"LOCKED","connection_type":"USB"}\n\#(Self.fullDeviceLine(udid: "READABLE"))\n\#(Self.fullDeviceLine(udid: "LOCKED"))\n{"type":"devices-end","count":2,"failed":0}\n'
        """#
        await store.pollAndEscalateIfChanged(argsOverride: ["-c", trusted])

        XCTAssertEqual(store.devices.map(\.udid), ["READABLE", "LOCKED"],
                       "the bus didn't change, so the poll never asked the phone again")
        XCTAssertEqual(store.deviceErrors, [])
    }

    /// B1: the row said "connected, but couldn't be read" under a raw UDID,
    /// with what to do on a 6 pt dot's tooltip. It says what to do, under
    /// the device's name when Graft has seen it before.
    func test_an_unreadable_row_says_what_to_do_under_the_name_graft_knows() {
        XCTAssertEqual(UndescribableDeviceRow.subtitle, "Unlock it and tap Trust")
        XCTAssertEqual(UndescribableDeviceRow.title(udid: "00008030-0000000000000009", remembered: "Example iPad"),
                       "Example iPad")
        XCTAssertEqual(UndescribableDeviceRow.title(udid: "00008030-0000000000000009", remembered: nil),
                       "00008030-0000000000000009")
    }

    /// The re-verify's nit: a phone that stays locked all day cost a full
    /// listing every five seconds, lockdown to every readable phone included.
    /// Each poll for the first few, then every `unreadableBackoff`th: a
    /// phone tapped Trust on a minute later still shows within half a minute.
    @MainActor
    func test_a_phone_that_stays_unreadable_is_read_again_less_often() async throws {
        let log = FileManager.default.temporaryDirectory.appending(path: "devices-\(UUID().uuidString).log")
        addTeardownBlock { try? FileManager.default.removeItem(at: log) }
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = DevicesStore(runner: runner)
        let locked = #"{"type":"device-brief","udid":"LOCKED","connection_type":"USB"}\n{"type":"device-error","udid":"LOCKED","error":"locked"}\n{"type":"devices-end","count":0,"failed":1}\n"#
        let script = "echo run >> '\(log.path)'; printf '\(locked)'"
        _ = try await store.load(argsOverride: ["-c", script])
        func runs() -> Int {
            ((try? String(contentsOf: log, encoding: .utf8)) ?? "").split(separator: "\n").count
        }
        var escalated: [Int] = []
        for poll in 1...(2 * DevicesStore.unreadableBackoff) {
            let before = runs()
            await store.pollAndEscalateIfChanged(argsOverride: ["-c", script])
            // A poll that escalates runs the script twice: brief, then full.
            if runs() - before == 2 { escalated.append(poll) }
        }

        let first = Array(1...DevicesStore.unreadableEveryPoll)
        XCTAssertEqual(escalated, first + [DevicesStore.unreadableBackoff, 2 * DevicesStore.unreadableBackoff]
            .filter { $0 > DevicesStore.unreadableEveryPoll })
        XCTAssertEqual(store.deviceErrors.map(\.udid), ["LOCKED"])
    }

    /// Once every phone reads, the poll is back to comparing presence: no
    /// listing every five seconds for a bench with nothing to re-read.
    @MainActor
    func test_with_every_phone_read_the_poll_compares_presence_again() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = DevicesStore(runner: runner)
        let seed = #"\#(Self.fullDeviceLine(udid: "READABLE"))\n{"type":"devices-end","count":1,"failed":0}\n"#
        _ = try await store.load(argsOverride: ["-c", "printf '\(seed)'"])

        // No `device` line: an escalation would empty the list.
        await store.pollAndEscalateIfChanged(argsOverride: [
            "-c", #"printf '{"type":"device-brief","udid":"READABLE","connection_type":"USB"}\n'"#,
        ])

        XCTAssertEqual(store.devices.map(\.udid), ["READABLE"])
    }

    /// Read again and still locked: the same answer publishes nothing.
    @MainActor
    func test_a_phone_still_unreadable_on_the_next_poll_publishes_nothing() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = DevicesStore(runner: runner)
        let listing = #"{"type":"device-error","udid":"LOCKED","error":"locked"}\n{"type":"devices-end","count":0,"failed":1}\n"#
        _ = try await store.load(argsOverride: ["-c", "printf '\(listing)'"])
        let changes = ObservableStoresTests.Changes(of: store)

        let again = #"{"type":"device-brief","udid":"LOCKED","connection_type":"USB"}\n"# + listing
        await store.pollAndEscalateIfChanged(argsOverride: ["-c", "printf '\(again)'"])

        XCTAssertEqual(store.deviceErrors.map(\.udid), ["LOCKED"])
        XCTAssertEqual(changes.count, 0)
    }

    /// This is the assertion the two-tier design rests on: a poll that finds
    /// the same UDIDs `knownUDIDs` already holds must not pay for `load()`.
    /// Falsifying, not just non-crashing: the script handed to the poll
    /// carries a `device-brief` line (what a correct, non-escalating poll
    /// reads) but *no* `device` line at all — so if the guard were broken and
    /// `load()` ran against this same script anyway, it would find zero
    /// `.device` events and stomp the distinctive seeded value in `devices`
    /// down to empty. A test that only checked "no crash" or "devices is
    /// non-nil" would pass whether or not escalation happened; this one does
    /// not.
    @MainActor
    func test_pollAndEscalateIfChanged_does_not_escalate_when_the_set_matches() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = DevicesStore(runner: runner)
        let seedScript = #"""
        printf '\#(Self.fullDeviceLine(udid: "SAME-UDID"))\n{"type":"devices-end","count":1,"failed":0}\n'
        """#
        _ = try await store.load(argsOverride: ["-c", seedScript])
        XCTAssertEqual(store.devices.map(\.udid), ["SAME-UDID"], "premise: the seed load landed")

        let pollScript = #"""
        printf '{"type":"device-brief","udid":"SAME-UDID","connection_type":"USB"}\n{"type":"devices-end","count":1,"failed":0}\n'
        """#
        await store.pollAndEscalateIfChanged(argsOverride: ["-c", pollScript])

        XCTAssertEqual(
            store.devices.map(\.udid), ["SAME-UDID"],
            "the brief set matched what load() already found — load() must not have re-run, "
            + "because the poll script it would have re-run carries no .device line at all"
        )
    }

    /// The other half: a brief set that differs from `knownUDIDs` has to
    /// escalate. Falsifying by construction: `store.devices` only ever
    /// contains "NEW-UDID" if the escalated `load()` actually ran, since the
    /// seed load left "OLD-UDID" behind and nothing else in this test ever
    /// mentions "NEW-UDID" except the poll script's own `device` line.
    @MainActor
    func test_pollAndEscalateIfChanged_escalates_when_the_set_differs() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = DevicesStore(runner: runner)
        let seedScript = #"""
        printf '\#(Self.fullDeviceLine(udid: "OLD-UDID"))\n{"type":"devices-end","count":1,"failed":0}\n'
        """#
        _ = try await store.load(argsOverride: ["-c", seedScript])
        XCTAssertEqual(store.devices.map(\.udid), ["OLD-UDID"], "premise")

        let pollScript = #"""
        printf '{"type":"device-brief","udid":"NEW-UDID","connection_type":"USB"}\n\#(Self.fullDeviceLine(udid: "NEW-UDID"))\n{"type":"devices-end","count":1,"failed":0}\n'
        """#
        await store.pollAndEscalateIfChanged(argsOverride: ["-c", pollScript])

        XCTAssertEqual(
            store.devices.map(\.udid), ["NEW-UDID"],
            "the brief set differed, so the escalated load() had to run and replace the seed"
        )
    }

    /// A failed brief poll must leave `devices` untouched and must not raise
    /// the modal `lastError` alert — a poll firing on a 5s timer is not
    /// something the user asked for, and latching an error onto it would
    /// interrupt whatever they're doing over a background hiccup. See
    /// `pollAndEscalateIfChanged`'s doc comment.
    @MainActor
    func test_a_failed_brief_poll_leaves_devices_untouched_and_lastError_nil() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = DevicesStore(runner: runner)
        let seedScript = #"""
        printf '\#(Self.fullDeviceLine(udid: "SEEDED-UDID"))\n{"type":"devices-end","count":1,"failed":0}\n'
        """#
        _ = try await store.load(argsOverride: ["-c", seedScript])
        XCTAssertEqual(store.devices.map(\.udid), ["SEEDED-UDID"], "premise")

        await store.pollAndEscalateIfChanged(argsOverride: ["-c", "exit 2"])

        XCTAssertEqual(store.devices.map(\.udid), ["SEEDED-UDID"])
        XCTAssertNil(store.lastError)
    }

    /// `load()` has to update `knownUDIDs` itself, not just an escalated
    /// poll — otherwise the very next poll after a manual Refresh sees an
    /// empty `knownUDIDs`, reads the unchanged set as a change, and
    /// escalates a second time for nothing. Falsifying the same way as the
    /// "matches" test above: the poll script carries no `.device` line, so a
    /// wrongly-triggered `load()` would empty out `devices`.
    @MainActor
    func test_load_updates_knownUDIDs_so_an_immediate_poll_does_not_escalate() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = DevicesStore(runner: runner)
        let seedScript = #"""
        printf '\#(Self.fullDeviceLine(udid: "REFRESHED-UDID"))\n{"type":"devices-end","count":1,"failed":0}\n'
        """#
        _ = try await store.load(argsOverride: ["-c", seedScript])
        XCTAssertEqual(store.devices.map(\.udid), ["REFRESHED-UDID"], "premise: the manual load landed")

        let pollScript = #"""
        printf '{"type":"device-brief","udid":"REFRESHED-UDID","connection_type":"USB"}\n{"type":"devices-end","count":1,"failed":0}\n'
        """#
        await store.pollAndEscalateIfChanged(argsOverride: ["-c", pollScript])

        XCTAssertEqual(
            store.devices.map(\.udid), ["REFRESHED-UDID"],
            "load() must have populated knownUDIDs with the same set the poll just saw, "
            + "or this poll would have wrongly re-run the .device-line-free poll script"
        )
    }

    // MARK: - A background scan may not delete what a foreground one reported
    //
    // `load()`'s catch clears `deviceErrors` on failure, which is right for the
    // Refresh button that produced it — the list is an instruction, and leaving
    // it up tells the user to redo what they just did. It is wrong for the 5s
    // poll, which is not something anyone asked for: the escalated load fails
    // or is canceled, `pollAndEscalateIfChanged` swallows the throw, and a
    // locked phone's row *and its retry button* vanish with nothing said.

    /// The escalation simply failing.
    ///
    /// The script answers the brief poll once and then refuses, which is what
    /// makes this falsifying: `pollAndEscalateIfChanged` hands the same
    /// `argsOverride` to both calls, so a marker file is the only way to have
    /// one subprocess succeed as the poll and the next fail as the escalation.
    /// The brief set it reports is deliberately *different* from the seeded
    /// one, because a poll that doesn't escalate proves nothing here.
    @MainActor
    func test_a_failed_escalation_keeps_the_device_errors_it_could_not_recheck() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = DevicesStore(runner: runner)
        _ = try await store.load(argsOverride: ["-c", Self.lockedPhoneOnly])
        XCTAssertFalse(store.deviceErrors.isEmpty, "premise: the locked phone was reported")

        let marker = FileManager.default.temporaryDirectory
            .appendingPathComponent("escalation-marker-\(UUID().uuidString)").path
        defer { try? FileManager.default.removeItem(atPath: marker) }
        let script = """
        if [ -e '\(marker)' ]; then echo 'usbmuxd went away' >&2; exit 3; fi
        touch '\(marker)'
        printf '{"type":"device-brief","udid":"NEW-UDID","connection_type":"USB"}\\n\
        {"type":"devices-end","count":1,"failed":0}\\n'
        """

        await store.pollAndEscalateIfChanged(argsOverride: ["-c", script])

        XCTAssertEqual(
            store.deviceErrors.map(\.udid), ["00008130-001A2B3C0123456D"],
            "the escalated scan never ran, so it learned nothing about this phone — "
            + "and dropping the row takes its retry button with it, silently"
        )
        XCTAssertNil(store.lastError, "a background poll must not raise the modal alert")
    }

    /// The likelier half: the user cmd-tabs away mid-scan.
    ///
    /// `ContentView`'s poll lives in `.task(id: controlActiveState)`, which is
    /// torn down the moment the window stops being frontmost — so this is an
    /// ordinary gesture, not an edge case. It has to be checked as
    /// *cancellation* rather than inferred from the error: canceling SIGTERMs
    /// the child and `CLIRunner` reports that as an ordinary non-zero exit,
    /// indistinguishable here from a real failure.
    ///
    /// Asserted against a plain `load()` rather than through the poll, because
    /// that is where the guard lives and because it covers the other canceled
    /// caller too — `ContentView`'s first unconditional listing.
    @MainActor
    func test_a_cancelled_scan_keeps_the_device_errors_it_could_not_recheck() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = DevicesStore(runner: runner)
        _ = try await store.load(argsOverride: ["-c", Self.lockedPhoneOnly])
        XCTAssertFalse(store.deviceErrors.isEmpty, "premise: the locked phone was reported")

        await Self.cancelAScanInFlight(on: store)

        XCTAssertEqual(
            store.deviceErrors.map(\.udid), ["00008130-001A2B3C0123456D"],
            "nothing was learned by a scan the user interrupted"
        )
    }

    /// `hasLoaded` is what `SidebarView` turns into "No devices connected", so
    /// a scan that never finished must not set it.
    ///
    /// Launch the app and switch away inside the ~1s the first listing takes
    /// and the sidebar used to flip from "Looking for devices…" to a definitive
    /// answer for a scan that was killed part-way through. The flag's own doc
    /// explains why it keys off the attempt rather than the result; that is
    /// still right, and cancellation is simply not an attempt.
    @MainActor
    func test_a_cancelled_scan_is_not_a_completed_attempt() async {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = DevicesStore(runner: runner)
        XCTAssertFalse(store.hasLoaded, "premise: nothing has been attempted yet")

        await Self.cancelAScanInFlight(on: store)

        XCTAssertFalse(store.hasLoaded, "a scan that was killed part-way learned nothing")
        XCTAssertFalse(store.isLoading, "this call is over either way — the spinner must stop")
    }

    /// Start a `load()` that will not finish on its own, wait until it is
    /// really in flight, then cancel it.
    ///
    /// `exec sleep` rather than a script with a trailing `printf`: `sh` would
    /// fork for that, and SIGTERM would then kill the shell while the orphaned
    /// `sleep` held the stdout pipe open for its full duration. `exec` makes
    /// the child *be* the sleep, so terminating it closes the pipe at once.
    @MainActor
    private static func cancelAScanInFlight(on store: DevicesStore) async {
        let task = Task { @MainActor in
            _ = try? await store.load(argsOverride: ["-c", "exec sleep 30"])
        }
        var waited = 0
        while !store.isLoading && waited < 3000 {
            try? await Task.sleep(nanoseconds: 1_000_000)
            waited += 1
        }
        XCTAssertTrue(store.isLoading, "premise: the scan is actually in flight")
        task.cancel()
        await task.value
    }
}
