import XCTest
@testable import Graft

/// "LAST BUILD in 0s": right after a build the tile read in the *future*,
/// because the CLI stamps the record with its own clock a moment after — or,
/// rounded, a moment ahead of — the app's `now`. Within a minute either way
/// the honest answer is "just now".
final class DateHelpersTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_800_000_000)

    func test_a_timestamp_a_moment_in_the_future_is_just_now() {
        XCTAssertEqual(DateHelpers.relativeToNow(now.addingTimeInterval(0.4), now: now), "just now")
        XCTAssertEqual(DateHelpers.relativeToNow(now.addingTimeInterval(45), now: now), "just now")
    }

    func test_a_timestamp_a_moment_ago_is_just_now() {
        XCTAssertEqual(DateHelpers.relativeToNow(now, now: now), "just now")
        XCTAssertEqual(DateHelpers.relativeToNow(now.addingTimeInterval(-59), now: now), "just now")
    }

    /// Past the minute it is the ordinary relative phrase, in the past tense.
    func test_older_timestamps_keep_the_relative_phrase() {
        let twoMinutes = DateHelpers.relativeToNow(now.addingTimeInterval(-120), now: now)
        XCTAssertNotEqual(twoMinutes, "just now")
        XCTAssertEqual(
            twoMinutes,
            DateHelpers.relative.localizedString(for: now.addingTimeInterval(-120), relativeTo: now)
        )
        XCTAssertFalse(twoMinutes.hasPrefix("in "), twoMinutes)
    }

    /// The tile itself, not only the helper: a build stamped a second after
    /// the app's clock reads "just now", successful or not.
    func test_the_last_build_tile_says_just_now_for_a_build_that_just_ended() throws {
        func record(_ timestamp: Date, success: Bool) throws -> BuildRecord {
            let stamp = DateHelpers.iso8601.string(from: timestamp)
            let line = "{\"type\":\"build-record\",\"id\":1,\"timestamp\":\"\(stamp)\","
                + "\"app\":\"example\",\"bundle_id\":\"com.example.app\",\"version\":\"1.0\","
                + "\"tweaks\":[],\"ipa_path\":null,\"ipa_sha256\":null,\"signing_team\":null,"
                + "\"udid\":null,\"target\":\"device\",\"success\":\(success),\"error\":null}"
            guard case let .buildRecord(r)? = line.buildEvents().first else {
                throw XCTSkip("fixture did not decode: \(line)")
            }
            return r
        }
        XCTAssertEqual(
            AppDetailView.lastBuildSummary(try record(now.addingTimeInterval(1), success: true), now: now),
            "just now"
        )
        XCTAssertEqual(
            AppDetailView.lastBuildSummary(try record(now.addingTimeInterval(-5), success: false), now: now),
            "failed just now"
        )
        XCTAssertEqual(AppDetailView.lastBuildSummary(nil, now: now), "—")
    }
}
