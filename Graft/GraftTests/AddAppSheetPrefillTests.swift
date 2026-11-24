import SwiftUI
import XCTest
@testable import Graft

/// `AddAppSheet.Prefill`'s copy: the sentence a dump's cache entry earns
/// depends on what a strip took and whether that taking was recorded, and
/// this is the one place that precedence is pinned. In particular:
/// `lost == nil` ("unrecorded") and `lost == []` ("recorded, nothing named")
/// must print the identical neutral sentence — see
/// `test_nothing_named_reads_the_same_whether_recorded_or_not` — and an
/// `"essential"` entry in `importance` must outrank everything else, because
/// forcing past that refusal is a different thing to have done than losing an
/// extension no tool could reach, though it does not erase the ordinary count
/// alongside it (`test_essential_and_ordinary_extensions_both_get_counted`).
/// `suggestedName`'s slug is a real port of `config.py`'s `slugify_name`, not
/// a lookalike that only agrees with it on ASCII names — the tests below
/// include the cases (multi-word, punctuation, an all-non-ASCII display name)
/// that a `.lowercased()` or a `.filter(\.isLetter)` would also pass.
///
/// One test at the bottom is not a copy test: `Prefill`'s values are inert
/// until a real click reaches `submit()` with `mode == .manual`, and nothing
/// above proves that wiring — see
/// `test_clicking_add_on_a_prefilled_sheet_sends_the_staged_name_and_bundle_id`.
@MainActor
final class AddAppSheetPrefillTests: XCTestCase {
    func test_the_slug_comes_from_the_display_name_lowercased() throws {
        let prefill = AddAppSheet.Prefill(staged: try stagedIPARecord(), installedVersion: "1.0")
        XCTAssertEqual(prefill.suggestedName, "exampleplayer")
        XCTAssertEqual(prefill.bundleId, "com.a.one")
        XCTAssertEqual(prefill.displayName, "ExamplePlayer")
    }

    /// A single-word name is a weak witness for a regex port: a bare
    /// `.lowercased()` or a `.filter(\.isLetter)` would both pass it too.
    /// Multiple words collapse to one `_` at each gap — including the run of
    /// *two* separator characters (space, then `&`, then space) between
    /// "Maps" and "Live" — which is `config.py`'s `_SLUG_RE =
    /// re.compile(r"[^a-z0-9]+")` collapsing a whole run to a single `_`, not
    /// one `_` per separator character. A wrong port that emitted one `_` per
    /// character would produce `example_maps___live_traffic`, not this.
    ///
    /// The trailing "2" also closes the one charset this file otherwise never
    /// exercises: no other fixture contains a digit, so deleting the digit
    /// range from `slugify`'s ASCII check survived every test here — "Sudoku
    /// 2" would have slugged to `sudoku`, silently dropping a real
    /// disambiguator apps actually use.
    func test_a_multi_word_name_collapses_separator_runs_to_one_underscore() throws {
        let prefill = AddAppSheet.Prefill(
            staged: try stagedIPARecord(displayName: "Example Maps & Live Traffic 2"),
            installedVersion: "1.0"
        )
        XCTAssertEqual(prefill.suggestedName, "example_maps_live_traffic_2")
    }

    /// Leading/trailing separators are trimmed, not turned into leading or
    /// trailing underscores — `config.py`'s `.strip("_")` after the regex
    /// substitution, not a plain substitution alone.
    func test_leading_and_trailing_punctuation_is_trimmed_not_turned_into_underscores() throws {
        let prefill = AddAppSheet.Prefill(
            staged: try stagedIPARecord(displayName: "  Bravo!  "), installedVersion: "1.0"
        )
        XCTAssertEqual(prefill.suggestedName, "bravo")
    }

    /// A single separator character between two letters becomes one `_`, the
    /// same as a run of several would — this is the case a naive
    /// per-character replacement (map each non-alnum char to `"_"` with no
    /// collapsing) would still pass by accident if `" - "`'s three characters
    /// happened to collapse to one by luck; `"A - B"` has exactly three
    /// separator characters between two single letters, so a per-character
    /// replacement would produce `a___b`, not this.
    func test_a_single_separator_does_not_become_three_underscores() throws {
        let prefill = AddAppSheet.Prefill(
            staged: try stagedIPARecord(displayName: "A - B"), installedVersion: "1.0"
        )
        XCTAssertEqual(prefill.suggestedName, "a_b")
    }

    /// The fallback this task exists to fix: a display name with no ASCII
    /// alphanumerics is an ordinary app (Example Social), not a missing field —
    /// `ipa_inspect.py` only falls back to `"Unknown"` when the plist key
    /// itself is absent — so its slug comes out empty and `suggestedName`
    /// must fall back to the bundle id *slugified*, not shown raw. Showing
    /// `com.example.social` (dots and all) would be a display bug: `add_app`
    /// dedups and writes whatever `slugify_name` makes of the text this
    /// property shows, which is `com_example_social`, not `com.example.social`.
    func test_an_all_non_ascii_name_falls_back_to_the_slugified_bundle_id() throws {
        let prefill = AddAppSheet.Prefill(
            staged: try stagedIPARecord(bundleId: "com.example.social", displayName: "微博"),
            installedVersion: "1.0"
        )
        XCTAssertEqual(prefill.suggestedName, "com_example_social")
    }

    // 128,299,008 bytes through `ByteCountFormatter`'s `.file` style — decimal
    // (1000-based) units, not binary. This is not a free choice: it is the
    // formatter every other size in this app already uses (`IPADropZone` in
    // this very file, `DeviceStatStrip`, `TweaksView`, `RepoStore`,
    // `AttachTweaksSheet`, `BuildEvent.RetentionSweep`), and `DeviceStatStrip`
    // says why — "the 1000-based ones Apple quotes device capacity in", the
    // same convention Finder uses. A prior draft of this test assumed binary
    // division (1024²) and expected "122.4 MB"; running the formula this app
    // actually uses produces "128.3 MB", and that is what is pinned here.
    func test_a_complete_dump_says_the_build_will_use_it() throws {
        let prefill = AddAppSheet.Prefill(staged: try stagedIPARecord(), installedVersion: "1.0")
        XCTAssertEqual(
            prefill.stagedLine,
            "Staged: com.a.one_1.0.ipa — 128.3 MB. The build will use this."
        )
    }

    func test_a_named_capability_is_what_the_line_leads_with() throws {
        let prefill = AddAppSheet.Prefill(
            staged: try stagedIPARecord(
                stripped: #"["a.appex","b.appex","c.appex"]"#,
                lost: #"["Siri and Shortcuts"]"#
            ),
            installedVersion: "1.0"
        )
        XCTAssertEqual(
            prefill.stagedLine,
            "Staged, minus 3 app extensions no tool could decrypt — Siri and Shortcuts will not work in the built app."
        )
    }

    /// Recorded-and-empty and never-recorded print the same sentence, because
    /// with nothing to name the honest line is the count. They stay different
    /// values so only one of them may ever carry a reassurance.
    func test_nothing_named_reads_the_same_whether_recorded_or_not() throws {
        let unrecorded = AddAppSheet.Prefill(
            staged: try stagedIPARecord(stripped: #"["a.appex","b.appex"]"#),
            installedVersion: "1.0"
        )
        let recordedEmpty = AddAppSheet.Prefill(
            staged: try stagedIPARecord(stripped: #"["a.appex","b.appex"]"#, lost: "[]"),
            installedVersion: "1.0"
        )
        let expected = "Staged, minus 2 app extensions no tool could decrypt."
        XCTAssertEqual(unrecorded.stagedLine, expected)
        XCTAssertEqual(recordedEmpty.stagedLine, expected)
    }

    /// The tooltip `stagedInfo` shows beside the neutral count — the one
    /// branch of `stagedLine` that names nothing, so `staged.stripped` has to
    /// be read from somewhere other than the sentence. Sorted, matching the
    /// order `stagedLine`'s own named branches use, so a reader who compares
    /// the two never sees the same set ordered two different ways.
    func test_hover_names_the_filenames_when_the_sentence_names_nothing() throws {
        let prefill = AddAppSheet.Prefill(
            staged: try stagedIPARecord(stripped: #"["b.appex","a.appex"]"#),
            installedVersion: "1.0"
        )
        XCTAssertEqual(prefill.strippedFilenamesForHover, "a.appex, b.appex")
    }

    /// `lost` names a *capability* — "Siri and Shortcuts" — never a
    /// filename, so `stagedLine`'s sentence for this branch has not actually
    /// named anything `stripped` describes; the hover must still fire. A
    /// first version of this rule read "lost is non-empty" as "something was
    /// named" and hid the filename here, which is the CLI's own answer to
    /// this exact question: `cli.py:3849-3854` prints both the filenames and
    /// the capabilities lost in one warning, because they are different
    /// facts.
    func test_hover_fires_when_the_sentence_names_a_capability_not_a_filename() throws {
        let prefill = AddAppSheet.Prefill(
            staged: try stagedIPARecord(
                stripped: #"["a.appex"]"#, lost: #"["Siri and Shortcuts"]"#
            ),
            installedVersion: "1.0"
        )
        XCTAssertEqual(prefill.strippedFilenamesForHover, "a.appex")
    }

    /// The hole `test_essential_and_ordinary_extensions_both_get_counted`'s
    /// own fix opened: `stagedLine` names 1 of 5 stripped extensions here
    /// ("including one this app is arguably for: Tunnel.appex"), and a first
    /// version of this rule read "an essential entry exists" as "something
    /// was named" and hid all five — the other four reachable from nowhere
    /// in the UI. The rule is `stripped.count > essentialNames.count`, not
    /// "no essential entry", so this case still fires and lists everything
    /// `stripped` holds, the already-named one included.
    func test_hover_fires_for_the_mixed_essential_case_naming_what_the_sentence_did_not() throws {
        let prefill = AddAppSheet.Prefill(
            staged: try stagedIPARecord(
                stripped: #"["a.appex","b.appex","c.appex","d.appex","Tunnel.appex"]"#,
                importance: #"{"Tunnel.appex":"essential"}"#
            ),
            installedVersion: "1.0"
        )
        XCTAssertEqual(
            prefill.strippedFilenamesForHover,
            "Tunnel.appex, a.appex, b.appex, c.appex, d.appex"
        )
    }

    /// The two branches where the sentence really has said everything
    /// `stripped` holds: one essential extension out of one stripped, and
    /// two essential extensions out of two stripped. Nothing is left for a
    /// hover to add in either case, so both stay nil.
    func test_hover_is_nil_when_the_sentence_named_every_stripped_extension() throws {
        let oneOfOne = AddAppSheet.Prefill(
            staged: try stagedIPARecord(
                stripped: #"["Tunnel.appex"]"#,
                importance: #"{"Tunnel.appex":"essential"}"#
            ),
            installedVersion: "1.0"
        )
        XCTAssertNil(oneOfOne.strippedFilenamesForHover)

        let twoOfTwo = AddAppSheet.Prefill(
            staged: try stagedIPARecord(
                stripped: #"["a/Tunnel.appex","a/Blocker.appex"]"#,
                importance: #"{"a/Tunnel.appex":"essential","a/Blocker.appex":"essential"}"#
            ),
            installedVersion: "1.0"
        )
        XCTAssertNil(twoOfTwo.strippedFilenamesForHover)
    }

    func test_hover_is_nil_for_a_complete_dump() throws {
        let prefill = AddAppSheet.Prefill(staged: try stagedIPARecord(), installedVersion: "1.0")
        XCTAssertNil(prefill.strippedFilenamesForHover)
    }

    /// Forcing past the essential-extension refusal is a different thing to
    /// have done, and the line leads with it. This is the only consumer of
    /// `importance`, which is why it is recorded at all.
    ///
    /// The total here is 1 and the essential count is also 1, so this is the
    /// single-extension collapse — the shorter sentence, with no separate
    /// "including" clause, because the two counts have nothing left to
    /// disagree about. `test_essential_and_ordinary_extensions_both_get_
    /// counted` below is the case where they do.
    func test_an_essential_extension_leads_the_line() throws {
        let prefill = AddAppSheet.Prefill(
            staged: try stagedIPARecord(
                stripped: #"["Payload/X.app/PlugIns/Tunnel.appex"]"#,
                lost: #"["the VPN"]"#,
                importance: #"{"Payload/X.app/PlugIns/Tunnel.appex":"essential"}"#
            ),
            installedVersion: "1.0"
        )
        XCTAssertEqual(
            prefill.stagedLine,
            "Staged, minus an extension this app is arguably for: Tunnel.appex."
        )
    }

    /// Both essential, and both of the total — the count and the essential
    /// tally are the same number here, but unlike the single-extension case
    /// that is not enough to collapse to the shorter sentence: with two
    /// extensions gone there is a real "how many" fact distinct from "how
    /// many named", and this pins that the longer form still states it.
    func test_two_essential_extensions_are_both_named() throws {
        let prefill = AddAppSheet.Prefill(
            staged: try stagedIPARecord(
                stripped: #"["a/Tunnel.appex","a/Blocker.appex"]"#,
                importance: #"{"a/Tunnel.appex":"essential","a/Blocker.appex":"essential"}"#
            ),
            installedVersion: "1.0"
        )
        XCTAssertEqual(
            prefill.stagedLine,
            "Staged, minus 2 app extensions no tool could decrypt — including 2 this app "
            + "is arguably for: Blocker.appex, Tunnel.appex."
        )
    }

    /// The mixed case a strip actually produces: the essential-extension
    /// refusal is forced past for one bundle, and a handful of ordinary
    /// undecryptable extensions come along with it. Before this test, the
    /// essential branch swallowed the whole sentence and reported "minus an
    /// extension" even though five were actually gone — losing four facts to
    /// gain one. This pins that both survive: the total, and which one was
    /// forced.
    func test_essential_and_ordinary_extensions_both_get_counted() throws {
        let prefill = AddAppSheet.Prefill(
            staged: try stagedIPARecord(
                stripped: #"["a.appex","b.appex","c.appex","d.appex","Tunnel.appex"]"#,
                importance: #"{"Tunnel.appex":"essential"}"#
            ),
            installedVersion: "1.0"
        )
        XCTAssertEqual(
            prefill.stagedLine,
            "Staged, minus 5 app extensions no tool could decrypt — including one this "
            + "app is arguably for: Tunnel.appex."
        )
    }

    func test_one_extension_is_singular() throws {
        let prefill = AddAppSheet.Prefill(
            staged: try stagedIPARecord(stripped: #"["a.appex"]"#),
            installedVersion: "1.0"
        )
        XCTAssertEqual(
            prefill.stagedLine,
            "Staged, minus 1 app extension no tool could decrypt."
        )
    }

    func test_a_version_disagreement_is_stated() throws {
        let prefill = AddAppSheet.Prefill(
            staged: try stagedIPARecord(version: "21.16.2", filename: "com.a.one_21.16.2.ipa"),
            installedVersion: "21.20.1"
        )
        XCTAssertEqual(
            prefill.versionLine,
            "This phone runs 21.20.1; the staged dump is 21.16.2."
        )
    }

    /// The decrypt-card case: a frontmost dump has no device listing behind
    /// it, so there is no installed version to compare against and the line is
    /// omitted rather than guessed.
    func test_no_installed_version_means_no_version_line() throws {
        let prefill = AddAppSheet.Prefill(staged: try stagedIPARecord(), installedVersion: nil)
        XCTAssertNil(prefill.versionLine)
    }

    func test_matching_versions_say_nothing() throws {
        let prefill = AddAppSheet.Prefill(
            staged: try stagedIPARecord(version: "1.0"), installedVersion: "1.0"
        )
        XCTAssertNil(prefill.versionLine)
    }

    // MARK: - Finding 4: the "Unknown" sentinel must never reach the field
    // `.onAppear` prefills, which `add_app_manual --display-name` submits
    // verbatim and `cyan_runner.py` passes straight to `-n`.

    /// The ordinary case: a real plist name, no device listing behind the
    /// add (`installedName` defaults to nil). Passes through unchanged.
    func test_a_real_display_name_is_used_as_is() throws {
        let prefill = AddAppSheet.Prefill(
            staged: try stagedIPARecord(displayName: "ExamplePlayer"), installedVersion: "1.0"
        )
        XCTAssertEqual(prefill.prefilledDisplayName, "ExamplePlayer")
    }

    /// The bug this finding pins: before the fix this property did not
    /// exist and `.onAppear` assigned `prefill.displayName` — the plist's
    /// own "Unknown" sentinel — straight into `manualDisplayName` with no
    /// guard at all. `title`'s own guard is the one this mirrors; this test
    /// would not tell an implementation that forgot the guard apart from one
    /// that has it, if it accepted "Unknown" as a value here.
    func test_the_unknown_sentinel_is_never_prefilled() throws {
        let prefill = AddAppSheet.Prefill(
            staged: try stagedIPARecord(displayName: "Unknown"), installedVersion: "1.0"
        )
        XCTAssertEqual(
            prefill.prefilledDisplayName, "",
            "the plist's own no-name sentinel must never reach a submitted "
            + "--display-name"
        )
    }

    /// The enhancement half of the fix: when a device listing *is* behind
    /// the add, the phone's own name for the app wins outright — even over
    /// a perfectly good plist name, since the phone's name is at least as
    /// authoritative and this is the field a build actually renames the app
    /// to.
    func test_the_phones_own_name_wins_over_the_plists_when_both_are_present() throws {
        let prefill = AddAppSheet.Prefill(
            staged: try stagedIPARecord(displayName: "ExamplePlayer"),
            installedVersion: "1.0", installedName: "ExamplePlayer Pro"
        )
        XCTAssertEqual(prefill.prefilledDisplayName, "ExamplePlayer Pro")
    }

    /// The case Finding 4 specifically calls out `installedName` for: the
    /// plist has nothing to say (`"Unknown"`), but the phone's own listing
    /// does. Without `installedName` winning here, this add would still
    /// leave the field blank for the user to type in by hand, even though
    /// the app's real name was sitting right there in `--device-apps`.
    func test_the_phones_own_name_fills_the_gap_the_unknown_sentinel_leaves() throws {
        let prefill = AddAppSheet.Prefill(
            staged: try stagedIPARecord(displayName: "Unknown"),
            installedVersion: "1.0", installedName: "Bravo"
        )
        XCTAssertEqual(prefill.prefilledDisplayName, "Bravo")
    }

    // MARK: - Rendering
    //
    // `click(at:in:)` and `hosted(_:width:)` are shared from ClickHarness.swift.
    // This file's own copy of `hosted` had already drifted from the other
    // three (it dropped the `width` parameter and hardcoded 480) — the
    // predicted cost of four independent copies, realized — so every call
    // below now passes `width: 480` explicitly to reproduce that same value
    // through the shared signature.

    /// The wiring the copy tests above cannot see: that `.onAppear` actually
    /// ran and switched `mode` to `.manual` — without it a click on "Add"
    /// runs the iTunes-lookup path (`store.addApp`, not `addAppManual`) and
    /// the CLI never sees `--name`/`--bundle-id` at all — and that
    /// `canSubmit` was satisfied by the fields it filled in. Confirmed by
    /// mutation: killing `mode = .manual`, killing the `.onAppear` fill, and
    /// forcing `canSubmit` to `false` each independently fail this test.
    ///
    /// The stub CLI is a temp script that captures its own argv (the idiom
    /// `ConfigStoreTests.test_updateAppOptions_sends_only_changed_flags`,
    /// `DeliveryStateTests` and `SettingsStoreTests` all use) and answers
    /// every invocation — this sheet's add call, then `mutate`'s reload
    /// afterward — with one `app-added` event. The reload's own args land in
    /// the same capture file but carry neither `--name` nor `--bundle-id`
    /// (it runs with `ConfigStore`'s default `--json --list`), so the two
    /// calls can't be confused by grepping for those flags.
    ///
    /// The script appends a sentinel line *after* each invocation's own argv,
    /// and this waits for two sentinels — one per invocation — rather than
    /// for the capture file to exist. A first version polled `fileExists`
    /// alone and failed under suite load with `captured: []`: `>>` creates
    /// the file on its first write, and a read landing between that create
    /// and the write itself observed an existing, empty file. Waiting for
    /// content, not presence, closes that race — and waiting for *two*
    /// sentinels rather than one means this also waits out `submit()`'s
    /// whole `Task`, past the point where it writes `lastDeliveryMode` and
    /// calls `dismiss()`, rather than racing that write the way a
    /// first-sentinel wait still would.
    ///
    /// `submit()` writes real `@AppStorage("lastDeliveryMode")` on a
    /// successful add. `View.defaultAppStorage(_:)` — not
    /// `EnvironmentValues.defaultAppStorage`, which this SDK's `SwiftUI`
    /// genuinely has no such property for, confirmed by grepping the
    /// installed SDK's `.swiftinterface` — redirects that write to a
    /// throwaway suite, torn down in `defer`, so this render never touches
    /// the app host's actual `UserDefaults.standard`.
    func test_clicking_add_on_a_prefilled_sheet_sends_the_staged_name_and_bundle_id() async throws {
        let suiteName = "AddAppSheetPrefillTests.\(UUID().uuidString)"
        guard let testDefaults = UserDefaults(suiteName: suiteName) else {
            return XCTFail("could not create an isolated UserDefaults suite for this render")
        }
        defer { testDefaults.removePersistentDomain(forName: suiteName) }

        let dir = NSTemporaryDirectory()
        let capturePath = dir + "add_app_sheet_captured_args_\(UUID().uuidString).txt"
        let scriptPath = dir + "add_app_sheet_capture_\(UUID().uuidString).sh"
        let sentinel = "__INVOCATION_END__"
        let script = """
        #!/bin/sh
        for arg in "$@"; do printf '%s\\n' "$arg" >> "\(capturePath)"; done
        printf '%s\\n' "\(sentinel)" >> "\(capturePath)"
        printf '{"type":"app-added","name":"exampleplayer","bundle_id":"com.a.one","display_name":"ExamplePlayer","delivery":"sideload"}\\n'
        """
        try script.write(toFile: scriptPath, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: scriptPath)
        defer {
            try? FileManager.default.removeItem(atPath: scriptPath)
            try? FileManager.default.removeItem(atPath: capturePath)
        }

        let store = ConfigStore(runner: CLIRunner(executable: URL(fileURLWithPath: scriptPath)), notify: { _ in })
        let prefill = AddAppSheet.Prefill(staged: try stagedIPARecord(), installedVersion: "1.0")
        let sheet = AddAppSheet(store: store, appStore: AppStoreStore(runner: store.runner, shell: NoAdminShell()), prefill: prefill)
            .defaultAppStorage(testDefaults)
        let box = hosted(sheet, width: 480)

        // "Add" is the trailing-most control in the footer's bottom-right
        // corner. Swept rather than pinned to one point, the same reason
        // `InstalledAppsSectionTests` sweeps its own trailing-edge clicks:
        // this test does not know the footer's exact height by hand, and a
        // sweep reaches the button regardless. Kept narrow on the x-axis so
        // it cannot drift left onto "Cancel".
        for y in stride(from: CGFloat(4), through: 40, by: 4) {
            for x in stride(from: box.size.width - 70, through: box.size.width - 4, by: 6) {
                click(at: NSPoint(x: x, y: y), in: box.window)
            }
        }

        // `submit()` runs the CLI call inside its own `Task`; poll for the
        // second sentinel rather than assume a fixed delay, since both
        // subprocesses this launches are real even though the script itself
        // is a few lines.
        let deadline = Date().addingTimeInterval(5)
        while true {
            let sentinelCount = (try? String(contentsOfFile: capturePath, encoding: .utf8))
                .map { $0.components(separatedBy: sentinel + "\n").count - 1 } ?? 0
            if sentinelCount >= 2 || Date() >= deadline { break }
            try await Task.sleep(nanoseconds: 50_000_000)
        }

        // Asserted before the read it guards, not just before the read that
        // follows it: `try String(contentsOfFile:)` on a path that was never
        // written throws `NSCocoaErrorDomain` code 4, which reports as an
        // opaque thrown error rather than any of the messages below — this
        // is what lets a mutation that breaks the wiring (so the file never
        // appears at all) still say why, instead of just crashing the test.
        guard FileManager.default.fileExists(atPath: capturePath) else {
            return XCTFail("the stub CLI's capture file never appeared — submit() never ran the CLI")
        }
        let captured = try String(contentsOfFile: capturePath, encoding: .utf8)
        let lines = captured.split(separator: "\n", omittingEmptySubsequences: true)
            .map(String.init)
            .filter { $0 != sentinel }
        XCTAssertTrue(
            lines.contains("--add-app-manual"),
            "a prefilled sheet must submit through the manual path, not the iTunes-lookup "
            + "one — captured: \(lines)"
        )
        guard let nameIdx = lines.firstIndex(of: "--name"), nameIdx + 1 < lines.count else {
            return XCTFail("CLI never received --name; captured: \(lines)")
        }
        XCTAssertEqual(lines[nameIdx + 1], "exampleplayer")
        guard let bundleIdx = lines.firstIndex(of: "--bundle-id"), bundleIdx + 1 < lines.count else {
            return XCTFail("CLI never received --bundle-id; captured: \(lines)")
        }
        XCTAssertEqual(lines[bundleIdx + 1], "com.a.one")
    }

    /// Settles item 3 of the coordinator's review by measurement rather than
    /// by reading: does the staged line actually wrap onto multiple lines in
    /// this sheet's real layout?
    ///
    /// A prior version of this test compared the *whole sheet's* height
    /// against a lone reference `Text` — not like against like. The sheet
    /// carries a header, a divider, four text fields, a picker and a footer
    /// on top of whatever `stagedLineText` draws, so that comparison passed
    /// whether the staged line wrapped, was clipped to one line, or was not
    /// drawn at all; `EmptyView()` in `stagedLineText`'s place survived it.
    /// This version instead renders the same sheet twice, differing only in
    /// which `Prefill` it carries: one whose staged line is the ~104-character
    /// stripped sentence that cannot fit `AddAppSheet`'s ~440pt content width
    /// on one line, and one whose staged line is the short complete-dump
    /// sentence. Every other part of the two renders — header, fields,
    /// picker, footer — is identical, so any height difference between them
    /// is the staged line's own wrapped height, and a sheet whose `Text` had
    /// been silently compressed to one line (clipped, not wrapped) would
    /// measure the same as the short-line sheet instead of taller.
    func test_the_stripped_line_measurably_wraps_rather_than_compresses() throws {
        let strippedPrefill = AddAppSheet.Prefill(
            staged: try stagedIPARecord(
                stripped: #"["a.appex","b.appex","c.appex"]"#,
                lost: #"["Siri and Shortcuts"]"#
            ),
            installedVersion: "1.0"
        )
        let completePrefill = AddAppSheet.Prefill(staged: try stagedIPARecord(), installedVersion: "1.0")

        // A throwaway preferences suite, as every render of the sheet has:
        // `lastDeliveryMode` is an `@AppStorage` (NoRealDefaultsTests'
        // `allowedWrappers`), and this render must not read the host's own.
        let suiteName = "AddAppSheetPrefillTests.height.\(UUID().uuidString)"
        let testDefaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defer { testDefaults.removePersistentDomain(forName: suiteName) }
        func sheetHeight(_ prefill: AddAppSheet.Prefill) -> CGFloat {
            let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
            return hosted(AddAppSheet(
                store: ConfigStore(runner: runner, notify: { _ in }), appStore: AppStoreStore(runner: runner, shell: NoAdminShell()),
                prefill: prefill
            ).defaultAppStorage(testDefaults), width: 480).size.height
        }
        let strippedHeight = sheetHeight(strippedPrefill)
        let completeHeight = sheetHeight(completePrefill)
        // Measured directly: 438pt against 424pt, a 14pt gap — one wrapped
        // line at `AppFont.monoSmall`. The +10 threshold sits below that
        // measured gap with room for the same font to round a point or two
        // differently, while staying well above zero, which is what a
        // clipped-to-one-line or undrawn staged line would report instead.
        XCTAssertGreaterThan(
            strippedHeight, completeHeight + 10,
            "the stripped line is ~104 characters and the complete-dump line is "
            + "~65 — at AppFont.monoSmall inside a ~440pt content width, the two "
            + "sheets should differ by roughly one wrapped line's height if the "
            + "long line actually wraps rather than being clipped or omitted"
        )
    }
}
