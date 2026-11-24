import XCTest
@testable import Graft

/// Which action a build row offers.
///
/// The polarity is the feature. A Mac build has a tweak dylib retargeted at a
/// bundled stub `Preferences.framework`; iOS has the real one, so offering
/// `Re-install…` on one pushes a stub-linked IPA to a phone and breaks the
/// tweak silently. Both directions are asserted for that reason — a case that
/// only feeds a Mac row passes against code that offers `Install to Mac…`
/// always, which would withhold the phone action from every ordinary build.
@MainActor
final class BuildsViewTests: XCTestCase {
    func test_a_mac_build_offers_the_mac_action() throws {
        XCTAssertEqual(BuildsView.action(for: try record(target: "mac")), .installToMac)
    }

    func test_a_device_build_offers_the_phone_action() throws {
        XCTAssertEqual(BuildsView.action(for: try record(target: "device")), .reinstall)
    }

    func test_a_failed_build_offers_neither() throws {
        XCTAssertEqual(
            BuildsView.action(for: try record(target: "mac", success: false)), .none,
            "a build that failed produced no artifact to install"
        )
    }

    func test_a_build_that_kept_no_archive_offers_neither() throws {
        XCTAssertEqual(
            BuildsView.action(for: try record(target: "device", ipaPath: nil)), .none,
            "the row survives retention; the file it names may not"
        )
    }

    /// A row whose target the CLI could not state reads as a phone build.
    ///
    /// Not a hypothetical: every row in a build log written before this column
    /// existed is one, and `_target_of` normalizes those to `device` on the
    /// Python side. Offering the phone action there is correct — that is what
    /// they are — and the failure this pins is the opposite one, where an
    /// unrecognized value fell to `mac` and withheld the action that worked.
    func test_an_unrecognised_target_offers_the_phone_action() throws {
        XCTAssertEqual(BuildsView.action(for: try record(target: "wat")), .reinstall)
    }

    /// The tag is gated the same way the action is.
    ///
    /// Pinned on `isForThisMac` rather than by rendering, because a `Table`
    /// cell's contents are invisible to an NSView walk in this app
    /// (`ClickHarness.backingTableView`) — so this is the honest level to
    /// assert at, and the view reads the same property one line away.
    func test_only_a_mac_build_is_tagged() throws {
        XCTAssertTrue(try record(target: "mac").isForThisMac)
        XCTAssertFalse(try record(target: "device").isForThisMac)
        XCTAssertFalse(try record(target: "wat").isForThisMac,
                       "an unrecognized target is not a Mac build")
    }

    // MARK: - F15: the menus

    /// The context menu read `success && ipaPath != nil` for itself and so
    /// offered Re-install… on a Mac build. Both menus read `installItem` now,
    /// which reads `action(for:)`.
    func test_a_mac_build_is_never_offered_a_reinstall() throws {
        let item = BuildsView.installItem(for: try record(target: "mac"), refusal: nil)
        XCTAssertEqual(item, .init(title: "Install to Mac…", enabled: true))
        let failed = BuildsView.installItem(for: try record(target: "mac", success: false), refusal: nil)
        XCTAssertFalse(failed.title.hasPrefix("Re-install"), failed.title)
    }

    /// A disabled item says why in its title: a menu draws no tooltip, so the
    /// `.help` reason never reached anyone.
    func test_a_refused_reinstall_says_why_in_its_title() throws {
        let item = BuildsView.installItem(
            for: try record(target: "device"), refusal: PaidOnly.sideloadApp
        )
        XCTAssertEqual(item, .init(title: "Re-install… (needs a paid account)", enabled: false))
        XCTAssertEqual(
            BuildsView.installItem(for: try record(target: "device"), refusal: nil),
            .init(title: "Re-install…", enabled: true)
        )
    }

    func test_an_item_with_nothing_to_act_on_says_why() throws {
        XCTAssertEqual(
            BuildsView.installItem(for: try record(target: "device", success: false), refusal: nil),
            .init(title: "Re-install… (build failed)", enabled: false)
        )
        XCTAssertEqual(
            BuildsView.installItem(for: try record(target: "device", ipaPath: nil), refusal: nil),
            .init(title: "Re-install… (archive not kept)", enabled: false)
        )
        XCTAssertEqual(
            BuildsView.revealItem(for: try record(target: "device", ipaPath: nil)),
            .init(title: "Reveal in Finder (no archive)", enabled: false)
        )
        XCTAssertEqual(
            BuildsView.logItem(for: try record(target: "device")),
            .init(title: "View log… (not saved)", enabled: false),
            "the fixture's log_path is null: a build run from Terminal"
        )
    }

    // MARK: -

    /// A `BuildRecord` as `--builds --json` actually emits one.
    ///
    /// Decoded from JSON rather than constructed, because `BuildRecord` has an
    /// explicit `CodingKeys` enum and no memberwise init — and because a
    /// fixture that skipped the decode would not notice a key the CLI stopped
    /// sending. Note this does *not* set `convertFromSnakeCase`: nothing in
    /// this app does, and setting it would stop those explicit raw values
    /// matching.
    private func record(
        target: String, success: Bool = true, ipaPath: String? = "/cache/built/y.ipa"
    ) throws -> BuildRecord {
        let path = ipaPath.map { "\"\($0)\"" } ?? "null"
        let json = """
        {"type":"build-record","id":1,"timestamp":"2026-08-18T12:00:00Z",\
        "app":"example","bundle_id":"com.example.app","version":"21.16.2",\
        "tweaks":[],"ipa_path":\(path),"ipa_sha256":null,"log_path":null,\
        "signing_team":null,"udid":null,"target":"\(target)",\
        "success":\(success),"error":null}
        """
        return try XCTUnwrap(
            json.buildEvents().compactMap { event -> BuildRecord? in
                if case let .buildRecord(record) = event { return record }
                return nil
            }.first,
            "the fixture did not decode — a key the CLI sends is missing from it"
        )
    }
}
