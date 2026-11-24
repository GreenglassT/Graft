import SwiftUI
import XCTest
@testable import Graft

/// Task 9 wired one row's Add button through `DeviceDetailView` to the
/// prefilled sheet — two inline closures inside `DeviceDetailView.body`, not
/// a named method either test file above can call directly. What this file
/// pins is the two ends of that wiring that were not already covered:
///
///  1. That the row-to-sheet transformation — `AddAppSheet.Prefill(staged:
///     record, installedVersion: app.version)` — actually uses the *phone's*
///     reported version and not the cache's, which only shows when the two
///     disagree. `InstalledAppsSectionTests.
///     test_the_section_forwards_add_with_the_app_and_record_that_triggered_it`
///     already proves the click reaches `onAdd(app, record)`; it does not
///     construct a `Prefill` at all, so it cannot catch
///     `installedVersion: record.version` — the exact wrong value that would
///     make `Prefill.versionLine` compare a number against itself and never
///     fire.
///  2. That `AddAppSheet.onAdded` — which `DeviceDetailView` reads to drive
///     `onSelectApp(added)` — fires with the CLI's own settled name, not the
///     locally-derived `suggestedName` the sheet showed while typing.
///     `AddAppSheetPrefillTests.
///     test_clicking_add_on_a_prefilled_sheet_sends_the_staged_name_and_bundle_id`
///     proves the CLI *receives* the right `--name`/`--bundle-id`; it never
///     passes an `onAdded` closure, so nothing anywhere previously checked
///     what the sheet hands back.
///
/// **What stays unpinned, and why no test here tries to close it:** the
/// literal closures inside `DeviceDetailView.body` — the two-line
/// `AddAppSheet.Prefill(...)` assignment and the `onSelectApp(added);
/// Task { await artwork.load(app: added) }` body — are not exercised by
/// anything below. `DeviceDetailLayoutTests.Harness` already renders the
/// whole pane inside its shipping `NavigationSplitView`, and every case in
/// that file measures layout rather than dispatches a click, for the reason
/// its own file doc gives for skipping row/button interaction there (see
/// its `onSelectApp: { _ in }` and comment: "this file measures layout, not
/// the `.configured` row's click, which `InstalledAppsSectionTests` already
/// pins").
///
/// A throwaway harness sweeping synthetic clicks across that same full-pane
/// render was tried while writing this file, to settle whether a click can
/// reach a control there at all. It genuinely hung — minutes at near-zero
/// CPU, no output — and was killed rather than left to answer the question
/// by accident.
///
/// **What it does not prove is why**, and one candidate is already ruled
/// out: the worry at the time was that a click landing on the sheet's own
/// Add button would leave `submit()` blocked reading stdin from the
/// harness's unstubbed `/bin/sh`. Measured directly against the exact argv
/// `submit()` builds — `/bin/sh --json --add-app-manual --name exampleplayer
/// --bundle-id com.a.one` — that exits **immediately** (`--json: invalid
/// option`, rc 2), because `sh` rejects the long option before reading a
/// byte. Every call this harness's `submit()` could make starts with
/// `--json`, so none of them can reach an interactive shell; whatever caused
/// the hang, it was not that. (An `NSWindow` presenting a real AppKit sheet
/// under synthetic events is the likelier shape for a multi-minute,
/// near-zero-CPU stall, but that is a guess, not a measurement, and is not
/// asserted as one.)
///
/// **A cleaner reason not to rebuild the probe, and this one is checked
/// rather than guessed:** `DeviceDetailLayoutTests.swift`'s own `Harness`
/// records that in that exact rendering, `staged.load()` with no override
/// runs the real `/bin/sh --json --staged-ipas`, which exits non-zero and
/// leaves `StagedIPAStore.hasLoaded` **false** — so `InstalledAppsSection.
/// rowState` never returns `.addable` for any row, no row ever draws an Add
/// button, and there is nothing for a click sweep to find. A probe built on
/// that harness would need its own separately-stubbed `StagedIPAStore`
/// before a sheet could open at all, which is real machinery this file does
/// not need: what is pinned below is the two transformations
/// `DeviceDetailView` performs, each exercised through the one production
/// type that already renders and receives real clicks safely
/// (`DeviceAppTile`/`InstalledAppsSection` for the first, `AddAppSheet` for
/// the second) — matched against `DeviceDetailView.swift`'s actual closures
/// by reading the two side by side, not by executing `DeviceDetailView`
/// itself.
@MainActor
final class DeviceDetailAddWiringTests: XCTestCase {

    // MARK: - 1. The row builds the right `Prefill`

    private static func installed(_ bundleId: String, version: String) -> DeviceAppRecord {
        DeviceAppRecord(bundleId: bundleId, name: "Example", version: version, encrypted: false)
    }

    // `click(at:in:)` and `hosted(_:width:)` are shared from
    // ClickHarness.swift; `stagedIPARecord(...)` (used below) from
    // StagedIPARecordFixture.swift — this file used to carry two
    // independent `StagedIPARecord` builders of its own
    // (`staged(_:version:filename:)` and
    // `stagedForSheet(bundleId:displayName:)`), differing only in which
    // fields were parameterised, in the same file, unnoticed.

    /// The transformation itself, copied verbatim from `DeviceDetailView`'s
    /// `InstalledAppsSection(...)` call site — this is what the case below
    /// exercises through a real click, and what a reviewer diffs against
    /// `DeviceDetailView.swift` to confirm the production closure matches.
    private func prefill(app: DeviceAppRecord, record: StagedIPARecord) -> AddAppSheet.Prefill {
        AddAppSheet.Prefill(staged: record, installedVersion: app.version)
    }

    /// What this actually pins: `AddAppSheet.Prefill(staged:installedVersion:)`,
    /// fed the phone's reported version against a cache record at a
    /// different one, produces the disagreement sentence — and an
    /// `.addable` tile's Add button really does receive a real click and
    /// hand back its row's record. It does **not** pin that
    /// `DeviceDetailView`'s own closure is the thing supplying that
    /// version — `prefill(app:record:)` above is a local stand-in, and a
    /// regression to `installedVersion: record.version` in the production
    /// closure would not fail this test, because this test never calls that
    /// closure. See the file doc's "matched … by reading the two side by
    /// side" for what closes that gap instead.
    func test_pressing_add_on_a_staged_row_prefills_the_phones_installed_version_not_the_cached_one() throws {
        let app = Self.installed("com.a.one", version: "21.20.1")
        let record = try stagedIPARecord(
            bundleId: "com.a.one", version: "21.16.2", filename: "com.a.one_21.16.2.ipa"
        )
        var captured: AddAppSheet.Prefill?
        let tile = DeviceAppTile(
            app: app,
            state: .addable(record),
            onAdd: { record in captured = self.prefill(app: app, record: record) }
        )
        let box = hosted(tile, width: 500)
        click(at: NSPoint(x: box.size.width - 16, y: box.size.height / 2), in: box.window)

        let result = try XCTUnwrap(captured, "the click never reached onAdd")
        XCTAssertEqual(result.staged.filename, "com.a.one_21.16.2.ipa", "the row's own staged record")
        XCTAssertEqual(
            result.installedVersion, "21.20.1",
            "the phone's reported version (DeviceAppRecord.version), not the cache's"
        )
        XCTAssertEqual(
            result.versionLine,
            "This phone runs 21.20.1; the staged dump is 21.16.2.",
            "proof the two inputs actually disagree, not just that some string was set"
        )
    }

    // MARK: - 2. The sheet hands back the CLI's own name

    /// The stub CLI is a temp script that captures its own argv and answers
    /// every invocation with one `app-added` event naming a *different* key
    /// than whatever `--name` it was sent — the shape `add_app_manual`
    /// produces on a collision (`exampleplayer` → `exampleplayer_2`). The idiom —
    /// script-with-sentinel, poll for two invocations (the add, then
    /// `mutate`'s reload) — is `AddAppSheetPrefillTests.
    /// test_clicking_add_on_a_prefilled_sheet_sends_the_staged_name_and_bundle_id`'s
    /// own, reused rather than re-invented.
    func test_onAdded_hands_back_the_CLIs_settled_name_not_the_locally_suggested_one() async throws {
        let suiteName = "DeviceDetailAddWiringTests.\(UUID().uuidString)"
        guard let testDefaults = UserDefaults(suiteName: suiteName) else {
            return XCTFail("could not create an isolated UserDefaults suite for this render")
        }
        defer { testDefaults.removePersistentDomain(forName: suiteName) }

        let dir = NSTemporaryDirectory()
        let capturePath = dir + "device_detail_add_wiring_captured_\(UUID().uuidString).txt"
        let scriptPath = dir + "device_detail_add_wiring_capture_\(UUID().uuidString).sh"
        let sentinel = "__INVOCATION_END__"
        // Answers "exampleplayer_2" regardless of what --name it was sent —
        // standing in for `add_app_manual` deduping past an existing
        // "exampleplayer" key, which is exactly the case where forwarding the
        // sheet's own `suggestedName` instead of this answer would select a
        // config key that does not exist.
        let script = """
        #!/bin/sh
        for arg in "$@"; do printf '%s\\n' "$arg" >> "\(capturePath)"; done
        printf '%s\\n' "\(sentinel)" >> "\(capturePath)"
        printf '{"type":"app-added","name":"exampleplayer_2","bundle_id":"com.a.one","display_name":"ExamplePlayer","delivery":"sideload"}\\n'
        """
        try script.write(toFile: scriptPath, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: scriptPath)
        defer {
            try? FileManager.default.removeItem(atPath: scriptPath)
            try? FileManager.default.removeItem(atPath: capturePath)
        }

        let store = ConfigStore(runner: CLIRunner(executable: URL(fileURLWithPath: scriptPath)), notify: { _ in })
        let prefill = AddAppSheet.Prefill(staged: try stagedIPARecord(), installedVersion: "1.0")
        XCTAssertEqual(
            prefill.suggestedName, "exampleplayer",
            "premise: the locally-derived name and the CLI's answer must actually differ, "
            + "or this test cannot tell the two apart"
        )
        var captured: String?
        // The same shape `DeviceDetailView` builds: `onAdded:` labeled
        // rather than trailing — see that file's own comment on why the
        // trailing form does not compile once `prefill:` is also supplied.
        let sheet = AddAppSheet(
            store: store, appStore: AppStoreStore(runner: store.runner, shell: NoAdminShell()),
            onAdded: { added in captured = added }, prefill: prefill
        )
        .defaultAppStorage(testDefaults)
        let box = hosted(sheet, width: 480)

        for y in stride(from: CGFloat(4), through: 40, by: 4) {
            for x in stride(from: box.size.width - 70, through: box.size.width - 4, by: 6) {
                click(at: NSPoint(x: x, y: y), in: box.window)
            }
        }

        // Polls the artifact this test actually asserts on, not a proxy for
        // it. A first version of this test polled the capture file's
        // sentinel count instead — a proxy that is written by the *second*
        // subprocess close to its start, well before that subprocess exits,
        // its stdout is drained, `reload()` returns, `mutate` and
        // `addAndReload` unwind, and `submit()` finally reaches `onAdded?`
        // and sets `captured`. `AddAppSheetPrefillTests.
        // test_clicking_add_on_a_prefilled_sheet_sends_the_staged_name_and_bundle_id`
        // gets away with the sentinel-count idiom because what it asserts on
        // — the capture file's own contents — *is* what the sentinel
        // guards; here the two are different things, and nothing forced a
        // yield between "sentinel seen" and "assert" for `captured` to have
        // caught up. Waiting on `captured` directly closes that gap instead
        // of hoping the race resolves the same way it did in nine local
        // runs.
        let deadline = Date().addingTimeInterval(5)
        while captured == nil, Date() < deadline {
            try await Task.sleep(nanoseconds: 50_000_000)
        }

        guard let captured else {
            let seen = (try? String(contentsOfFile: capturePath, encoding: .utf8))
                ?? "(the capture file never appeared — the stub CLI itself never ran)"
            return XCTFail("onAdded never fired within 5s; captured so far: \(seen)")
        }
        XCTAssertEqual(
            captured, "exampleplayer_2",
            "onAdded must hand back the CLI's own settled key, not the sheet's "
            + "locally-derived suggestion — a caller selecting the latter would select "
            + "nothing, since no row exists under it"
        )
    }
}
