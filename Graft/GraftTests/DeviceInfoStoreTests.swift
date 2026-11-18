import XCTest
@testable import Graft

final class DeviceInfoStoreTests: XCTestCase {
    private static func reachableLine(
        udid: String, totalBytes: Int = 128_000_000_000, freeBytes: Int = 35_728_093_184,
        batteryPercent: Int = 70, batteryCharging: Bool = false
    ) -> String {
        #"""
        {"type":"device-info","udid":"\#(udid)","reachable":true,"total_bytes":\#(totalBytes),"free_bytes":\#(freeBytes),"battery_percent":\#(batteryPercent),"battery_charging":\#(batteryCharging)}
        """#
    }

    private static func unreachableLine(udid: String) -> String {
        #"{"type":"device-info","udid":"\#(udid)","reachable":false,"total_bytes":null,"free_bytes":null,"battery_percent":null,"battery_charging":null}"#
    }

    @MainActor
    func test_load_stores_a_reachable_record_with_all_fields_intact() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let script = #"printf '\#(Self.reachableLine(udid: "GOOD-UDID"))\n'"#
        let store = DeviceInfoStore(runner: runner)

        await store.load(udid: "GOOD-UDID", argsOverride: ["-c", script])

        let record = try XCTUnwrap(store.info["GOOD-UDID"])
        XCTAssertTrue(record.reachable)
        XCTAssertEqual(record.totalBytes, 128_000_000_000)
        XCTAssertEqual(record.freeBytes, 35_728_093_184)
        XCTAssertEqual(record.batteryPercent, 70)
        XCTAssertEqual(record.batteryCharging, false)
    }

    @MainActor
    func test_load_holds_two_udids_independently() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = DeviceInfoStore(runner: runner)

        await store.load(
            udid: "UDID-ONE",
            argsOverride: ["-c", #"printf '\#(Self.reachableLine(udid: "UDID-ONE", batteryPercent: 90))\n'"#]
        )
        await store.load(
            udid: "UDID-TWO",
            argsOverride: ["-c", #"printf '\#(Self.reachableLine(udid: "UDID-TWO", batteryPercent: 15))\n'"#]
        )

        XCTAssertEqual(store.info["UDID-ONE"]?.batteryPercent, 90)
        XCTAssertEqual(store.info["UDID-TWO"]?.batteryPercent, 15)
    }

    /// The judgement call this whole task rests on: a `reachable: false`
    /// result arriving after a good reading must not blank the strip. If
    /// `load` overwrote unconditionally, `info["UDID"]?.batteryPercent` would
    /// read nil after the second call — this assertion fails exactly then.
    @MainActor
    func test_an_unreachable_result_does_not_lose_a_prior_good_reading() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = DeviceInfoStore(runner: runner)

        await store.load(
            udid: "UDID",
            argsOverride: ["-c", #"printf '\#(Self.reachableLine(udid: "UDID", batteryPercent: 70))\n'"#]
        )
        XCTAssertEqual(store.info["UDID"]?.batteryPercent, 70, "premise: the good reading landed")

        await store.load(
            udid: "UDID",
            argsOverride: ["-c", #"printf '\#(Self.unreachableLine(udid: "UDID"))\n'"#]
        )

        let record = try XCTUnwrap(store.info["UDID"])
        XCTAssertFalse(record.reachable, "the blip itself is recorded")
        XCTAssertEqual(
            record.batteryPercent, 70,
            "a transient unreachable read must not blank a strip that read 70% moments ago"
        )
        XCTAssertEqual(record.totalBytes, 128_000_000_000)
        XCTAssertEqual(record.freeBytes, 35_728_093_184)
        XCTAssertEqual(record.batteryCharging, false)
    }

    @MainActor
    func test_a_run_that_emits_nothing_leaves_no_entry_and_no_error() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = DeviceInfoStore(runner: runner)

        await store.load(udid: "UDID", argsOverride: ["-c", "true"])

        XCTAssertNil(store.info["UDID"])
        XCTAssertTrue(store.info.isEmpty)
    }

    @MainActor
    func test_a_nonzero_exit_is_swallowed() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = DeviceInfoStore(runner: runner)

        await store.load(udid: "UDID", argsOverride: ["-c", "exit 2"])

        XCTAssertNil(store.info["UDID"])
        XCTAssertTrue(store.info.isEmpty)
    }

    /// The locked-phone shape: `reachable: true` but every data field nil —
    /// distinct from `reachable: false`. Neither field should be lost.
    @MainActor
    func test_all_four_data_fields_nil_decodes_and_stores_without_loss() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let script = #"""
        printf '{"type":"device-info","udid":"UDID","reachable":true,"total_bytes":null,"free_bytes":null,"battery_percent":null,"battery_charging":null}\n'
        """#
        let store = DeviceInfoStore(runner: runner)

        await store.load(udid: "UDID", argsOverride: ["-c", script])

        let record = try XCTUnwrap(store.info["UDID"])
        XCTAssertTrue(record.reachable)
        XCTAssertNil(record.totalBytes)
        XCTAssertNil(record.freeBytes)
        XCTAssertNil(record.batteryPercent)
        XCTAssertNil(record.batteryCharging)
    }
}
