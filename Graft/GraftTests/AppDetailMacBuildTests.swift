import XCTest
@testable import Graft

/// The two pure decisions behind the app pane's "Build for this Mac".
///
/// Static members of the view rather than a hosted body, for the reason
/// `AppDetailView.identityLine` and `BuildsView`-style row actions are: the
/// interesting part is a function of a build-log row and a subprocess's answer,
/// and asserting it through a rendered SwiftUI body would be slower, more
/// brittle, and would need a real `graft` to produce the row.
final class AppDetailMacBuildTests: XCTestCase {

    // MARK: - Which archive gets handed over

    func test_a_mac_build_hands_over_its_archive() throws {
        XCTAssertEqual(
            AppDetailView.handoffFilename(from: try record(target: "mac")),
            "example_21.16.2_20260818T134203.ipa"
        )
    }

    /// The polarity that matters, and the reason this function exists at all.
    ///
    /// `--install-to-mac` does not check: it hands whatever file it is named to
    /// PlayCover. A device build sent through it installs an app whose tweak
    /// dylib still wants the private `Preferences.framework` macOS does not
    /// have — the mirror of the hazard `--reinstall-build` refuses on the other
    /// side, and the one this whole feature is shaped around.
    func test_a_device_build_is_never_handed_to_playcover() throws {
        XCTAssertNil(AppDetailView.handoffFilename(from: try record(target: "device")))
    }

    /// A failed build's row carries no artifact — `cli.py` NULLs `ipa_path`
    /// unless the build succeeded — so there is nothing to install.
    func test_a_failed_build_hands_over_nothing() throws {
        XCTAssertNil(
            AppDetailView.handoffFilename(from: try record(target: "mac", success: false))
        )
        XCTAssertNil(
            AppDetailView.handoffFilename(from: try record(target: "mac", ipaPath: nil))
        )
    }

    /// The archive may have been swept away by the retention prune, which
    /// clears the path on the row rather than deleting it.
    func test_an_archiveless_row_hands_over_nothing() throws {
        XCTAssertNil(
            AppDetailView.handoffFilename(from: try record(target: "mac", ipaPath: ""))
        )
    }

    /// There is no row at all when the `--builds` read failed, and a build
    /// nothing recorded is not one to hand over.
    func test_no_record_hands_over_nothing() {
        XCTAssertNil(AppDetailView.handoffFilename(from: nil))
    }

    /// A filename, not the path: `--install-to-mac` refuses anything with a
    /// directory in it ("is not a filename"), and resolves the bare name
    /// against `cache_dir` and then `built_dir`.
    func test_the_handoff_takes_the_filename_not_the_path() throws {
        let filename = AppDetailView.handoffFilename(
            from: try record(target: "mac", ipaPath: "/Users/t/.graft/built/x.ipa")
        )
        XCTAssertEqual(filename, "x.ipa")
    }

    // MARK: - A failed handoff is not a failed build

    /// PlayCover refusing the file adds two warnings to the transcript and
    /// nothing else.
    ///
    /// The "nothing else" is the assertion, and it is carried by the type: a
    /// `Handoff` produces `[LogLine]`, which cannot express a failed build.
    /// `buildState` and `store.noteBuildFailure` are the only two things that
    /// paint one and neither is reachable from here — so a run whose handoff
    /// failed keeps the `✓ done` its `build-end` earned, and the archive it
    /// made stays listed for the retry.
    func test_a_refused_handoff_only_adds_transcript() {
        let lines = AppDetailView.Handoff
            .refused(filename: "x.ipa", complaint: "PlayCover is not installed")
            .logLines
        XCTAssertEqual(lines.map(\.kind), [.warn, .warn])
        XCTAssertEqual(
            lines.first?.text, "  ✗ PlayCover is not installed",
            "the CLI's own sentence, carried rather than composed"
        )
        XCTAssertEqual(
            lines.last?.text,
            "The build is fine and x.ipa is kept — Install to Mac on its row in "
            + "Builds is the retry.",
            "a user who reads this as a failed build rebuilds for nothing"
        )
    }

    /// And the success line does not claim an install, because neither side can
    /// see one: `open -b` returns as soon as LaunchServices delivers the event.
    func test_a_completed_handoff_claims_only_the_handoff() {
        let lines = AppDetailView.Handoff.handedOver(filename: "x.ipa").logLines
        XCTAssertEqual(lines.map(\.kind), [.ok])
        XCTAssertEqual(
            lines.first?.text,
            "  ✓ handed x.ipa to PlayCover — it reports its own progress"
        )
        XCTAssertFalse(
            lines.first?.text.contains("installed") ?? true,
            "nothing here has observed an install finishing"
        )
    }

    func test_a_build_with_no_archive_says_so_rather_than_going_quiet() {
        let lines = AppDetailView.Handoff.noArchive.logLines
        XCTAssertEqual(lines.map(\.kind), [.warn])
        XCTAssertEqual(lines.count, 1)
    }

    // MARK: - Fixture

    /// A `--builds` row, decoded the way `BuildEvent` decodes one, so the
    /// `target` field goes through the same `CodingKeys` production does.
    private func record(
        target: String,
        success: Bool = true,
        ipaPath: String? = "/Users/t/.graft/built/example_21.16.2_20260818T134203.ipa"
    ) throws -> BuildRecord {
        let path = ipaPath.map { "\"\($0)\"" } ?? "null"
        let json = """
        {"id":1,"timestamp":"2026-08-18T13:42:03","app":"example",
         "bundle_id":"com.example.app","version":"21.16.2","tweaks":[],
         "ipa_path":\(path),"ipa_sha256":null,"log_path":null,
         "signing_team":null,"udid":null,"success":\(success),"error":null,
         "target":"\(target)"}
        """
        // A plain decoder, which is what `BuildEvent.decode` uses — the field
        // names come from `BuildRecord.CodingKeys`, not from a key strategy.
        return try JSONDecoder().decode(BuildRecord.self, from: Data(json.utf8))
    }
}
