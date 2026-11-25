import XCTest
@testable import Graft

/// Export IPA…: the app's half of `--export-ipa`.
///
/// What the pane sends (`BuildOptions`), what it reads back (`ipa-exported`),
/// and what it says. The Save panel itself is AppKit's and is not hosted
/// here; everything it feeds or is fed by is.
final class ExportIPATests: XCTestCase {
    private let path = "/tmp/Example 1.0.ipa"

    // MARK: - the argv

    func test_an_export_sends_the_flag_and_its_path() {
        let options = BuildOptions().exporting(to: path, replacing: false)

        XCTAssertEqual(options.cliArgs, ["--export-ipa", path])
        XCTAssertEqual(options.activeOptions, [.exportIPA(path: path, replacing: false)])
    }

    func test_replacing_sends_overwrite_and_only_then() {
        XCTAssertEqual(
            BuildOptions().exporting(to: path, replacing: true).cliArgs,
            ["--export-ipa", path, "--overwrite"]
        )
        XCTAssertFalse(
            BuildOptions().exporting(to: path, replacing: false).cliArgs.contains("--overwrite")
        )
    }

    /// The CLI refuses `--device`, `--skip-install` and `--for-playcover`
    /// beside `--export-ipa`. A pane left with a phone picked and Skip install
    /// on must still export, so none of them reaches the argv.
    func test_an_export_never_sends_an_option_about_the_device() {
        var pane = BuildOptions()
        pane.skipInstall = true
        pane.forPlaycover = true
        pane.deviceUDID = "00008030-0000000000000001"

        let args = pane.exporting(to: path, replacing: false).cliArgs

        XCTAssertEqual(args, ["--export-ipa", path])
        for flag in [CLIContract.Flag.device, CLIContract.Flag.skipInstall,
                     CLIContract.Flag.forPlaycover] {
            XCTAssertFalse(args.contains(flag), "\(flag) beside --export-ipa")
        }
    }

    func test_dry_run_and_the_decryption_check_still_apply() {
        var pane = BuildOptions()
        pane.dryRun = true
        pane.skipDecryptCheck = true

        XCTAssertEqual(
            pane.exporting(to: path, replacing: false).cliArgs,
            ["--dry-run", "--export-ipa", path, "--skip-decrypt-check"]
        )
    }

    /// Click stops parsing options at the first positional, so the app name
    /// has to come last — `runBuildProcess` puts it there, after these.
    func test_the_path_is_an_options_value_not_a_positional() {
        let args = [CLIContract.Flag.json]
            + BuildOptions().exporting(to: path, replacing: true).cliArgs + ["example"]
        XCTAssertEqual(args.last, "example")
        XCTAssertEqual(args.firstIndex(of: path), args.firstIndex(of: "--export-ipa")! + 1)
    }

    func test_the_panes_own_options_are_left_as_they_were() {
        var pane = BuildOptions()
        pane.deviceUDID = "UDID1"
        _ = pane.exporting(to: path, replacing: true)

        XCTAssertNil(pane.exportPath)
        XCTAssertEqual(pane.cliArgs, ["--device", "UDID1"])
    }

    // MARK: - never delivers

    func test_an_export_delivers_nothing_whatever_the_apps_mode() {
        let options = BuildOptions().exporting(to: path, replacing: false)

        for mode in [DeliveryMode.livecontainer, .sideload] {
            XCTAssertEqual(
                options.effectiveDelivery(for: mode), .notDelivered(reason: "Exported to a file")
            )
        }
        XCTAssertEqual(options.deliverySuppressor, .exportIPA(path: path, replacing: false))
    }

    func test_an_export_never_looks_for_a_device() {
        XCTAssertFalse(BuildOptions.Option.exportIPA(path: path, replacing: false).stillResolvesDevice)
    }

    func test_a_dry_run_export_is_still_named_a_dry_run() {
        var pane = BuildOptions()
        pane.dryRun = true
        XCTAssertEqual(pane.exporting(to: path, replacing: false).deliverySuppressor, .dryRun)
    }

    /// The Stop confirmation and the banner both read this, so an export
    /// running says nothing about a phone it never goes near.
    func test_stopping_an_export_warns_about_no_device() {
        let exposure = DeviceExposure(
            delivery: BuildOptions().exporting(to: path, replacing: false)
                .effectiveDelivery(for: .livecontainer)
        )
        XCTAssertEqual(exposure, .neverWill(reason: "Exported to a file"))
        XCTAssertNil(exposure.advanced(stage: "lc-push", status: "start").cancelAftermath)
        XCTAssertTrue(exposure.cancelWarning.hasPrefix("Exported to a file — nothing is being sent"))
    }

    func test_the_banner_for_a_finished_export() {
        let note = BuildNotifier.note(
            app: "example", outcome: .succeeded(elapsed: 12),
            delivery: BuildOptions().exporting(to: path, replacing: false)
                .effectiveDelivery(for: .sideload)
        )
        XCTAssertEqual(note?.body.hasSuffix("Exported to a file — nothing was sent to the device."), true)
    }

    // MARK: - the event

    private let exportedLine = #"{"type":"ipa-exported","app":"example","version":"1.0","path":"/tmp/Example 1.0.ipa","filename":"Example 1.0.ipa","size_bytes":4096,"tweaks":["some-tweak.deb"],"replaced":false,"dry_run":false}"#

    func test_ipa_exported_decodes() throws {
        guard case let .ipaExported(record) = try BuildEvent.decode(line: exportedLine) else {
            return XCTFail("expected .ipaExported")
        }
        XCTAssertEqual(record.app, "example")
        XCTAssertEqual(record.version, "1.0")
        XCTAssertEqual(record.path, "/tmp/Example 1.0.ipa")
        XCTAssertEqual(record.filename, "Example 1.0.ipa")
        XCTAssertEqual(record.sizeBytes, 4096)
        XCTAssertEqual(record.tweaks, ["some-tweak.deb"])
        XCTAssertFalse(record.replaced)
        XCTAssertFalse(record.dryRun)
    }

    func test_a_dry_runs_has_no_size() throws {
        let line = #"{"type":"ipa-exported","app":"example","version":"1.0","path":"/tmp/x.ipa","filename":"x.ipa","size_bytes":null,"tweaks":[],"replaced":true,"dry_run":true}"#
        guard case let .ipaExported(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .ipaExported")
        }
        XCTAssertNil(record.sizeBytes)
        XCTAssertTrue(record.dryRun)
        XCTAssertTrue(record.replaced)
    }

    func test_it_is_part_of_the_build_stream() throws {
        let event = try BuildEvent.decode(line: exportedLine)
        guard case let .exported(record)? = event.progress else {
            return XCTFail("expected .exported, got \(String(describing: event.progress))")
        }
        XCTAssertEqual(record.filename, "Example 1.0.ipa")
    }

    func test_an_exports_build_start_decodes_with_no_delivery() throws {
        let line = #"{"type":"build-start","app":"example","bundle_id":"com.example.app","delivery":null,"export_to":"/tmp/x.ipa"}"#
        XCTAssertEqual(try BuildEvent.decode(line: line).progress, .start(app: "example", delivery: nil))
    }

    // MARK: - the transcript

    func test_the_transcript_says_where_it_went() throws {
        let lines = BuildLogView.lines(for: try BuildEvent.decode(line: exportedLine))
        XCTAssertEqual(lines.map(\.text), ["  ✓ wrote /tmp/Example 1.0.ipa (unsigned, 4 KB)"])
        XCTAssertEqual(lines.map(\.kind), [.ok])
    }

    func test_a_dry_runs_says_nothing_was_written() throws {
        let line = #"{"type":"ipa-exported","app":"example","version":"1.0","path":"/tmp/x.ipa","filename":"x.ipa","size_bytes":null,"tweaks":[],"replaced":false,"dry_run":true}"#
        let lines = BuildLogView.lines(for: try BuildEvent.decode(line: line))
        XCTAssertEqual(lines.map(\.text), ["    would write /tmp/x.ipa, unsigned — dry run, nothing was written"])
    }

    func test_an_exports_first_line_says_export_not_delivery_unknown() {
        let start = BuildEvent.buildStart(app: "example", bundleId: "com.example.app", delivery: nil)
        XCTAssertEqual(
            BuildLogView.lines(for: start, startVerb: "export", startDetail: "unsigned").map(\.text),
            ["→ export: example (unsigned)"]
        )
        // A build's is as it was.
        XCTAssertEqual(
            BuildLogView.lines(for: start).map(\.text), ["→ build start: example (delivery unknown)"]
        )
    }

    /// An export's events are folded into the store like a build's, and none
    /// of them is a failure: the card that a failed build leaves isn't left.
    @MainActor
    func test_the_store_reads_a_finished_export_as_a_success() throws {
        var notes: [BuildNotifier.Note?] = []
        let store = ConfigStore(
            runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/true")),
            notify: { notes.append($0) }
        )
        let stream = [
            #"{"type":"build-start","app":"example","bundle_id":"com.example.app","delivery":null,"export_to":"/tmp/x.ipa"}"#,
            #"{"type":"stage","name":"export","status":"start"}"#,
            #"{"type":"stage","name":"export","status":"end"}"#,
            exportedLine,
            #"{"type":"build-end","app":"example","ok":true,"stopped":false,"elapsed_seconds":3.0}"#,
        ]
        for line in stream {
            if let progress = try BuildEvent.decode(line: line).progress {
                store.note(progress, forBuildOf: "example")
            }
        }
        XCTAssertNil(store.buildFailures["example"])
        XCTAssertEqual(notes.count, 1)
    }

    // MARK: - the pane

    func test_the_suggested_name_is_the_app_and_its_version() {
        XCTAssertEqual(AppDetailView.exportFilename(name: "Example", version: "1.2.3"), "Example 1.2.3.ipa")
        XCTAssertEqual(AppDetailView.exportFilename(name: "Example+", version: nil), "Example+.ipa")
        XCTAssertEqual(AppDetailView.exportFilename(name: "Example", version: ""), "Example.ipa")
    }

    func test_the_suggested_name_is_a_name_finder_can_save() {
        XCTAssertEqual(
            AppDetailView.exportFilename(name: "A/B: Example", version: "1.0/2"), "A-B- Example 1.0-2.ipa"
        )
        XCTAssertEqual(AppDetailView.exportFilename(name: ".hidden", version: "1"), "hidden 1.ipa")
        XCTAssertEqual(AppDetailView.exportFilename(name: "  ", version: "1"), "App 1.ipa")
    }

    func test_the_confirmation_names_the_file_a_real_export_wrote() throws {
        let events = [
            try BuildEvent.decode(line: exportedLine),
            try BuildEvent.decode(line: #"{"type":"build-end","app":"example","ok":true,"stopped":false,"elapsed_seconds":3.0}"#),
        ]
        let record = try XCTUnwrap(AppDetailView.exportedRecord(in: events))
        XCTAssertEqual(AppDetailView.exportedTitle(record), "Exported Example 1.0.ipa")
        XCTAssertEqual(record.path, "/tmp/Example 1.0.ipa")
    }

    /// A dry run wrote nothing, so there is nothing to show in Finder.
    func test_a_dry_run_has_no_confirmation() throws {
        let line = #"{"type":"ipa-exported","app":"example","version":"1.0","path":"/tmp/x.ipa","filename":"x.ipa","size_bytes":null,"tweaks":[],"replaced":false,"dry_run":true}"#
        XCTAssertNil(AppDetailView.exportedRecord(in: [try BuildEvent.decode(line: line)]))
        XCTAssertNil(AppDetailView.exportedRecord(in: []))
    }

    /// The menu's words stay inside the rules: a Save panel, your own
    /// devices, and nothing about sending the file anywhere.
    func test_the_menus_words() {
        XCTAssertEqual(AppDetailView.exportLabel, "Export IPA…")
        XCTAssertTrue(AppDetailView.exportBlurb.contains("unsigned"))
        XCTAssertTrue(AppDetailView.exportBlurb.contains("your own devices"))
        for text in [AppDetailView.exportBlurb, AppDetailView.exportHelp, AppDetailView.exportedNote] {
            for word in ["AirDrop", "share", "upload", "link"] {
                XCTAssertFalse(text.localizedCaseInsensitiveContains(word), "\(word) in \(text)")
            }
        }
    }

    /// The Save panel is a sheet on Graft's window, even in the moment after
    /// the popover closes when there is no main window yet.
    func test_the_save_panel_attaches_to_the_main_window_or_the_frontmost_usable_one() {
        XCTAssertEqual(AppDetailView.sheetParent(main: "main", frontToBack: ["front"], usable: { _ in true }), "main")
        XCTAssertEqual(
            AppDetailView.sheetParent(main: nil, frontToBack: ["popover", "graft", "other"], usable: { $0 != "popover" }),
            "graft"
        )
        XCTAssertNil(AppDetailView.sheetParent(main: nil, frontToBack: ["popover"], usable: { $0 != "popover" }))
    }
}
