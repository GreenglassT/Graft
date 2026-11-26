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

    /// The handoff's line, streamed the moment the CLI says it, doesn't claim
    /// an install: `open -b` returns as soon as LaunchServices delivers the
    /// event. The waiting line says what comes next (spec 5d item 7).
    func test_the_handoff_streams_its_line_and_the_wait() throws {
        let handed = try BuildEvent.decode(line: #"{"type":"handed-to-playcover","filename":"x.ipa","path":"/tmp/x.ipa","bundle_id":"com.example.app","display_name":"Example"}"#)
        let lines = MacInstall.streamLines(for: handed, app: "example")
        XCTAssertEqual(lines.map(\.text), [
            "  ✓ handed x.ipa to PlayCover",
            "Waiting for PlayCover to install Example…",
        ])
        XCTAssertFalse(lines[0].text.contains("installed"), "nothing has seen an install finish yet")
    }

    func test_the_wait_names_the_app_by_the_panes_name_when_the_archive_has_none() throws {
        let handed = try BuildEvent.decode(line: #"{"type":"handed-to-playcover","filename":"x.ipa","path":"/tmp/x.ipa","bundle_id":"com.example.app"}"#)
        XCTAssertEqual(MacInstall.streamLines(for: handed, app: "Example").last?.text,
                       "Waiting for PlayCover to install Example…")
    }

    /// An install the CLI confirmed ends the pane's log with it, and the
    /// first-handoff notice — still transcript only.
    func test_a_confirmed_install_ends_the_log_with_it() throws {
        guard case let .playCoverInstalled(record) = try BuildEvent.decode(line: #"{"type":"playcover-installed","bundle_id":"com.example.app","version":"1.0","display_name":"Example","settings_disabled":["keymapping"],"settings_restored":[]}"#)
        else { return XCTFail("fixture") }
        let install = MacInstall(handed: nil, ending: .installed(record))
        let lines = AppDetailView.Handoff.of(install, filename: "x.ipa", app: "example").logLines
        XCTAssertEqual(lines.map(\.text), [
            "  ✓ PlayCover installed Example 1.0. Open it from PlayCover.",
            "Graft turned off PlayCover's keymapping for Example. For a game, turn keymapping "
                + "back on in PlayCover; Graft keeps it from then on.",
        ])
    }

    func test_a_refusal_is_still_the_refusal() {
        let install = MacInstall(handed: nil, ending: .refused("PlayCover is not installed"))
        XCTAssertEqual(
            AppDetailView.Handoff.of(install, filename: "x.ipa", app: "example"),
            .refused(filename: "x.ipa", complaint: "PlayCover is not installed")
        )
    }

    func test_a_stopped_wait_is_one_dim_line_not_a_failure() {
        let lines = AppDetailView.Handoff.of(
            MacInstall(handed: nil, ending: .stopped), filename: "x.ipa", app: "Example"
        ).logLines
        XCTAssertEqual(lines.map(\.kind), [.dim])
    }

    /// The wait keeps the build slot, so Stop is there while it runs (spec
    /// 5d item 7); a phone build's slot goes when the CLI exits, as before.
    func test_only_a_mac_build_holds_the_slot_through_the_handoff() {
        var mac = BuildOptions()
        mac.forPlaycover = true
        XCTAssertTrue(AppDetailView.holdsSlotThroughHandoff(mac))
        XCTAssertFalse(AppDetailView.holdsSlotThroughHandoff(BuildOptions()))
    }

    // MARK: - Stop during the wait

    /// Once the build has handed over and only the wait for PlayCover is
    /// left, Stop says that's what it stops.
    func test_the_stop_confirmation_says_what_it_stops() {
        XCTAssertEqual(StopWords.title(name: "Example", waitingForPlayCover: false), "Stop building Example?")
        XCTAssertEqual(StopWords.keep(waitingForPlayCover: false), "Keep building")
        XCTAssertEqual(StopWords.title(name: "Example", waitingForPlayCover: true), "Stop waiting for PlayCover?")
        XCTAssertEqual(StopWords.keep(waitingForPlayCover: true), "Keep waiting")
    }

    @MainActor
    func test_only_the_building_app_can_be_waiting_and_the_consequence_says_so() {
        let store = ConfigStore(
            runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")), notify: { _ in }
        )
        store.noteWaitingForPlayCover("example")
        XCTAssertNil(store.waitingForPlayCover, "nothing is building, so nothing is waiting")
        XCTAssertFalse(store.cancelConsequence.contains("PlayCover"))
        XCTAssertEqual(
            ConfigStore.waitingConsequence,
            "PlayCover keeps the build and goes on installing it. Graft only stops waiting, "
                + "so it won't say when the install is done."
        )
    }

    // MARK: - What to expect (spec 5d item 4)

    func test_the_expectations_are_the_specs_words() {
        XCTAssertEqual(
            PlayCover.expectations,
            "Not every iPhone or iPad app runs under PlayCover, even without tweaks: some open to "
                + "a blank window, can't open their data, or have buttons that don't respond. If an "
                + "app doesn't work, try it without tweaks first (Install to Mac… on its IPA in the "
                + "Cache pane); if that fails too, PlayCover can't run that app."
        )
    }

    func test_build_for_this_mac_says_what_to_expect() {
        XCTAssertTrue(AppDetailView.macBuildHelp.hasSuffix(PlayCover.expectations))
    }

    func test_the_readme_says_the_same() throws {
        let readme = try SourceScan.appSources()
            .deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("README.md")
        let text = try String(contentsOf: readme, encoding: .utf8)
            .replacingOccurrences(of: "\n", with: " ")
        XCTAssertTrue(text.contains(PlayCover.expectations), "README's Running builds on this Mac")
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
