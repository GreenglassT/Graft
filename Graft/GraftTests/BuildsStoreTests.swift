import XCTest
@testable import Graft

final class BuildsStoreTests: XCTestCase {
    @MainActor
    func test_load_parses_build_records() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let script = #"""
        printf '{"type":"build-record","id":2,"timestamp":"2026-05-13T20:00:00Z","app":"delta","bundle_id":"com.delta.app","version":"44.7.0","tweaks":["EchoV1.73.deb"],"ipa_path":null,"ipa_sha256":null,"signing_team":null,"udid":null,"target":"device","success":false,"error":"cyan failed"}\n{"type":"build-record","id":1,"timestamp":"2026-05-13T19:00:00Z","app":"example","bundle_id":"com.example.app","version":"21.16.2","tweaks":["a.deb","b.deb"],"ipa_path":"/cache/y.ipa","ipa_sha256":"abc","signing_team":"TEAM","udid":"udid","target":"device","success":true,"error":null}\n'
        """#
        let store = BuildsStore(runner: runner)
        let records = try await store.load(argsOverride: ["-c", script])
        XCTAssertEqual(records.count, 2)
        XCTAssertEqual(records[0].app, "delta")
        XCTAssertEqual(records[0].success, false)
        XCTAssertEqual(records[1].app, "example")
        XCTAssertEqual(records[1].tweaks, ["a.deb", "b.deb"])
    }

    /// `target` is what lets a row be offered a Mac action or a phone action
    /// and never both, so it is decoded here rather than assumed.
    ///
    /// It also pins the non-optional choice. A row that reaches this side
    /// without a `target` key is not decoded as some default — `buildEvents()`
    /// drops the undecodable line with `try?` and the record disappears
    /// entirely, so the count assertion is what catches that, not the field
    /// assertions below it.
    @MainActor
    func test_a_mac_build_is_told_apart_from_a_device_build() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let script = #"""
        printf '{"type":"build-record","id":4,"timestamp":"2026-08-18T13:42:03Z","app":"example","bundle_id":"com.example.app","version":"21.16.2","tweaks":["some-tweak.deb"],"ipa_path":"/built/y_mac.ipa","ipa_sha256":"mac","signing_team":null,"udid":null,"target":"mac","success":true,"error":null}\n{"type":"build-record","id":3,"timestamp":"2026-08-18T12:00:00Z","app":"example","bundle_id":"com.example.app","version":"21.16.2","tweaks":["some-tweak.deb"],"ipa_path":"/built/y.ipa","ipa_sha256":"dev","signing_team":"TEAM","udid":"udid","target":"device","success":true,"error":null}\n'
        """#
        let store = BuildsStore(runner: runner)
        let records = try await store.load(argsOverride: ["-c", script])
        XCTAssertEqual(records.count, 2, "a row missing `target` is dropped, not defaulted")
        XCTAssertEqual(records[0].target, "mac")
        XCTAssertTrue(records[0].isForThisMac)
        XCTAssertEqual(records[1].target, "device")
        XCTAssertFalse(records[1].isForThisMac)
    }
}
