import XCTest
@testable import Graft

/// The device page's Developer Mode row (spec §5): shown on any phone with a
/// reading, when some configured app is signed with a development profile
/// (`needs_developer_mode`, the CLI's `developer_mode.needed`).
final class DeviceDeveloperModeRowTests: XCTestCase {
    private typealias Row = DeviceDeveloperModeRow

    /// `AppRecord` exists only as decoded CLI output, so it is built that way.
    /// `needs` is the event's `needs_developer_mode`: true, false, nil (null),
    /// or absent, from a CLI before 2.3.
    private func app(
        _ name: String, delivery: DeliveryMode = .sideload, needs: Bool?, absent: Bool = false
    ) throws -> AppRecord {
        let value = needs.map { $0 ? "true" : "false" } ?? "null"
        let field = absent ? "" : #","needs_developer_mode":\#(value)"#
        let line = #"{"type":"app","name":"\#(name)","bundle_id":"com.example.\#(name)","preferred_version":"latest","tweaks":[],"display_name":null,"delivery":"\#(delivery.rawValue)","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6\#(field)}"#
        guard case let .app(record) = try BuildEvent.decode(line: line) else {
            throw XCTSkip("fixture is not an app event")
        }
        return record
    }

    func test_the_row_says_on_or_off() {
        XCTAssertEqual(Row.line(isOn: true), "Developer Mode: On")
        XCTAssertEqual(Row.line(isOn: false), "Developer Mode: Off")
    }

    /// `developer_mode.TURN_ON`, word for word.
    func test_the_hint_is_the_clis_words() {
        XCTAssertEqual(
            Row.turnOn,
            "Turn on Developer Mode on your iPhone or iPad in Settings › Privacy & Security › "
                + "Developer Mode, then restart it."
        )
    }

    func test_an_app_that_needs_it_shows_the_reading() throws {
        let apps = [try app("one", delivery: .livecontainer, needs: false), try app("two", needs: true)]
        XCTAssertEqual(Row.shown(developerMode: false, apps: apps), false)
        XCTAssertEqual(Row.shown(developerMode: true, apps: apps), true)
    }

    /// LiveContainer, an ad hoc or enterprise profile (false), a profile the
    /// CLI couldn't read (null), an older CLI (absent), and no apps at all.
    func test_no_app_that_needs_it_shows_nothing() throws {
        let apps = [
            try app("lc", delivery: .livecontainer, needs: false),
            try app("adhoc", needs: false),
            try app("unread", needs: nil),
            try app("old", needs: nil, absent: true),
        ]
        XCTAssertNil(Row.shown(developerMode: false, apps: apps))
        XCTAssertNil(Row.shown(developerMode: false, apps: []))
    }

    func test_no_reading_shows_nothing() throws {
        XCTAssertNil(Row.shown(developerMode: nil, apps: [try app("two", needs: true)]))
    }
}
