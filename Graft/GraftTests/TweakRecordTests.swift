import XCTest
@testable import Graft

final class TweakRecordTests: XCTestCase {
    func test_decode_tweak_event() throws {
        let line = #"{"type":"tweak","filename":"foo.deb","size_bytes":12345,"used_by":["example","delta"]}"#
        let event = try BuildEvent.decode(line: line)
        guard case let .tweak(record) = event else {
            return XCTFail("expected .tweak case, got \(event)")
        }
        XCTAssertEqual(record.filename, "foo.deb")
        XCTAssertEqual(record.sizeBytes, 12345)
        XCTAssertEqual(record.usedBy, ["example", "delta"])
    }

    func test_decode_tweak_event_with_empty_used_by() throws {
        let line = #"{"type":"tweak","filename":"orphan.deb","size_bytes":50,"used_by":[]}"#
        let event = try BuildEvent.decode(line: line)
        guard case let .tweak(record) = event else {
            return XCTFail("expected .tweak case")
        }
        XCTAssertTrue(record.usedBy.isEmpty)
    }
}

// MARK: - Control metadata read out of the .deb

extension TweakRecordTests {

    /// The Repos browser shows a package's name, version and section, and the
    /// moment it was downloaded the library showed a filename and a byte
    /// count. These four fields close that gap; the CLI reads them from the
    /// .deb's own control stanza, so they work for a hand-copied file too.
    func test_decodes_the_control_fields_the_cli_now_sends() throws {
        let line = #"{"type":"tweak","filename":"some-tweak.deb","size_bytes":6144,"used_by":["example"],"name":"Some Tweak","version":"1.7.19","section":"Tweaks","author":"example"}"#
        guard case let .tweak(rec) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected a tweak event")
        }
        XCTAssertEqual(rec.displayName, "Some Tweak")
        XCTAssertEqual(rec.detailLine, "1.7.19 · Tweaks · example")
    }

    /// The degradation that matters: a .deb the CLI couldn't parse — a
    /// truncated download, a renamed zip — still lists, with exactly the three
    /// keys this event has always carried. Decoding must not become strict.
    func test_a_tweak_with_no_control_fields_still_decodes() throws {
        let line = #"{"type":"tweak","filename":"broken.deb","size_bytes":12,"used_by":[]}"#
        guard case let .tweak(rec) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected a tweak event")
        }
        XCTAssertNil(rec.name)
        XCTAssertNil(rec.detailLine, "a lonely separator is worse than no line")
        XCTAssertEqual(rec.displayName, "broken.deb", "the row falls back to what it always showed")
    }

    /// Partial metadata is the common case — plenty of tweaks declare a
    /// Version and no Section — so the line is built from whatever exists
    /// rather than requiring all three.
    func test_the_detail_line_is_built_from_whatever_parts_exist() throws {
        let line = #"{"type":"tweak","filename":"x.deb","size_bytes":1,"used_by":[],"version":"2.0"}"#
        guard case let .tweak(rec) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected a tweak event")
        }
        XCTAssertEqual(rec.detailLine, "2.0")
    }

    /// Two files whose *names* collide but whose versions don't — the exact
    /// case the library could not previously tell apart.
    func test_two_builds_of_one_tweak_are_distinguishable() throws {
        let a = #"{"type":"tweak","filename":"com.example.btweak_1.1.8-jailed_iphoneos-arm64.deb","size_bytes":38826,"used_by":["bravo"],"name":"BTweak","version":"1.1.8-jailed"}"#
        let b = #"{"type":"tweak","filename":"com.example.btweak_1.1.8_iphoneos-arm64.deb","size_bytes":19154,"used_by":[],"name":"BTweak","version":"1.1.8"}"#
        guard case let .tweak(one) = try BuildEvent.decode(line: a),
              case let .tweak(two) = try BuildEvent.decode(line: b) else {
            return XCTFail("expected two tweak events")
        }
        XCTAssertEqual(one.displayName, two.displayName, "premise: the names collide")
        XCTAssertNotEqual(one.detailLine, two.detailLine)
    }
}

// MARK: - Counting things in English

extension TweakRecordTests {
    func test_pluralized_says_one_package_and_two_packages() {
        XCTAssertEqual(pluralized(0, "package"), "0 packages")
        XCTAssertEqual(pluralized(1, "package"), "1 package")
        XCTAssertEqual(pluralized(2, "package"), "2 packages")
    }

    /// The irregular case the default `+ "s"` cannot reach — present so the
    /// parameter exists before something needs it, not after.
    func test_pluralized_takes_an_explicit_plural() {
        XCTAssertEqual(pluralized(1, "entry", "entries"), "1 entry")
        XCTAssertEqual(pluralized(3, "entry", "entries"), "3 entries")
    }
}
