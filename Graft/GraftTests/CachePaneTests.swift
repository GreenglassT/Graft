import SwiftUI
import XCTest
@testable import Graft

/// What the Cache pane can say before anything is wired to a button: the one
/// line a row summarizes a file with, the mark on the file a build would
/// actually reach for, and — separately from both — whether the pane has been
/// told anything about the cache at all.
///
/// The pure parts are pinned as values, the way `InstalledAppsSectionTests`
/// pins `rowState`: a summary line and a chip label are functions of one
/// record, and a value comparison says more about them than a rendered `body`
/// could. Three facts about the pane are *not* functions of a record, and each
/// gets its own kind of test:
///
///  - **Waiting is not empty.** `listState(of:)` reads `StagedIPAStore.
///    hasLoaded`, never `all.isEmpty`, because a cache that has answered and
///    holds nothing is a different claim from one that has never answered —
///    the conflation this GUI has shipped three times. Every *finished* state
///    below is reached by running a real `StagedIPAStore.load` against a
///    stubbed `/bin/sh`, so `hasLoaded` and `all` are set together the only
///    way production sets them (both are `private(set)`, so there is no
///    hand-built pair to disagree with itself); the waiting state is a store
///    that has simply never been loaded.
///  - **…and the words have to keep them apart too.** Pinning the enum is not
///    pinning the pane: with the two sentences written inline in `body`,
///    swapping them left every test here green, and so did an unconditional
///    "0 files" in the header over a pane that had never asked. Both are
///    values now (`message(for:)`, `headerCount(for:)`) and both are pinned
///    below — the same move `rowSummary` and `preferredLabel` already make,
///    applied to the copy that was still hiding in the view.
///  - **One row per file, not per bundle id.** Spec §1 is explicit that two
///    versions of one app are two rows, since deleting the stale one is half
///    of what this pane is for — and `StagedIPAStore` publishes both readings
///    (`all` and `byBundleId`), so a `List` over the wrong one compiles and
///    renders a plausible-looking pane that is quietly missing rows. Nothing
///    above can see that, so `test_two_versions_of_one_app_are_two_rows`
///    renders the pane and counts the rows in the backing `NSTableView`.
///
/// The row's copy is deliberately shorter than `AddAppSheet.Prefill.
/// stagedLine`'s sentence — see `CachePaneView.rowSummary`'s own doc for what
/// reusing that sentence verbatim would cost a row.
/// `test_the_row_never_contradicts_the_sentence_it_shortens` is what keeps
/// "shorter" from becoming "different":
/// the size figure and the extension count are tied to the add sheet's own
/// output through the same literal, so a row that started formatting either
/// one its own way fails there rather than drifting quietly.
/// What the delete callback was asked to do, as a reference type.
///
/// Not an `inout [(String, Bool)]`: the callback that records into it is
/// `@escaping` and outlives the helper's frame, and Swift rejects an escaping
/// closure capturing an `inout` parameter outright. It is not a borrow that
/// merely looks risky — the exclusivity rule has no way to extend the access
/// past the call, so there is no version of the value-typed spelling that
/// compiles. A class gives the closure and the assertions the same storage.
/// Which of the objection strip's two buttons a test means.
///
/// Named, not indexed. The strip lays them out **"Delete Anyway" then
/// "Keep It"**, so counting inward from the trailing edge puts Keep It at 0 —
/// the reverse of reading order. An index literal at the call site had them
/// backwards, and the swap is quiet rather than loud: the two cases below are
/// each other's control, so getting it wrong makes one report a miss while the
/// other passes for entirely the wrong reason.
private enum ObjectionButton: Int {
    /// Rightmost: the strip puts the dismissive action last.
    case keepIt = 0
    case deleteAnyway = 1
}

@MainActor
private final class DeleteAttempts {
    var all: [(filename: String, force: Bool)] = []
}

@MainActor
final class CachePaneTests: XCTestCase {
    // 128,299,008 bytes is `stagedIPARecord`'s default size, and **128.3 MB**
    // is what `ByteCountFormatter` with `countStyle: .file` prints for it —
    // measured in this worktree with `xcrun swift`, not assumed. `.file` is
    // 1000-based (128,299,008 / 1000² = 128.299…), the convention every size
    // in this app already uses; see `DeviceStatStrip.byteFormatter` and
    // `AddAppSheetPrefillTests`' own note, which records a previous draft
    // asserting the 1024-based figure. That figure is **122.4 MB** — what
    // `.memory` and `.binary` print for these same bytes — so it is not an
    // abstract wrong answer: it is exactly what this row would say if it
    // reached for the wrong count style, and pinning 128.3 discriminates
    // between the two.
    private static let defaultSize = "128.3 MB"

    // MARK: - The row's summary line

    func test_a_complete_dump_summarises_as_filename_and_size() throws {
        let row = CachePaneView.rowSummary(for: try stagedIPARecord(sizeBytes: 128_299_008))
        XCTAssertEqual(row, "com.a.one_1.0.ipa — 128.3 MB")
    }

    /// The stripped state, in the add sheet's units but not its sentence: a
    /// count and a noun, no "no tool could decrypt" tail and no capability
    /// clause. **The size survives**, which is the reason this is a shorter
    /// form rather than `stagedLine` itself — that sentence drops the filename
    /// and the size entirely the moment anything was stripped, and a cache
    /// pane whose stripped rows stopped saying how big they are would have
    /// lost the fact the pane exists for.
    func test_a_stripped_dump_appends_the_count_of_what_was_removed() throws {
        let record = try stagedIPARecord(stripped: #"["a.appex","b.appex"]"#)
        XCTAssertEqual(
            CachePaneView.rowSummary(for: record),
            "com.a.one_1.0.ipa — 128.3 MB, minus 2 app extensions"
        )
    }

    /// One is singular, the same as `stagedLine`'s own count branch. Without
    /// this, a hardcoded "app extensions" reads correctly on every fixture
    /// above and wrong on the one case a real cache produces most often.
    func test_one_stripped_extension_is_singular() throws {
        let record = try stagedIPARecord(stripped: #"["a.appex"]"#)
        XCTAssertEqual(
            CachePaneView.rowSummary(for: record),
            "com.a.one_1.0.ipa — 128.3 MB, minus 1 app extension"
        )
    }

    /// Shorter, never different. Two facts appear in both the row and the add
    /// sheet's sentence, and this ties each of them to one literal that both
    /// have to produce:
    ///
    ///  - The **size**, through `ByteCountFormatter`'s `.file` style. A row
    ///    that reached for `.memory` would print `122.4 MB` for these bytes
    ///    while the sheet went on printing `128.3 MB` — two different sizes
    ///    for one file, in two places a user sees within one click of each
    ///    other.
    ///  - The **count**, on the fixture where a wrong reading is most
    ///    tempting: five stripped extensions of which one is marked
    ///    `essential`. `stagedLine` names that one specially ("including one
    ///    this app is arguably for"), and a row that took its count from
    ///    `importance` rather than `stripped` would say "minus 1 app
    ///    extension" — a smaller, wronger number that no fixture with an
    ///    empty `importance` can catch.
    func test_the_row_never_contradicts_the_sentence_it_shortens() throws {
        let complete = try stagedIPARecord()
        let completeSheet = AddAppSheet.Prefill(staged: complete, installedVersion: nil)
        XCTAssertTrue(
            completeSheet.stagedLine.contains(Self.defaultSize),
            "premise: the add sheet no longer prints this size, so the tie below "
            + "would hold two unrelated strings together — got \(completeSheet.stagedLine)"
        )
        XCTAssertTrue(
            CachePaneView.rowSummary(for: complete).contains(Self.defaultSize),
            "the row must print the same size the add sheet does, from the same "
            + "count style"
        )

        let mixed = try stagedIPARecord(
            stripped: #"["a.appex","b.appex","c.appex","d.appex","Tunnel.appex"]"#,
            importance: #"{"Tunnel.appex":"essential"}"#
        )
        let mixedSheet = AddAppSheet.Prefill(staged: mixed, installedVersion: nil)
        XCTAssertTrue(
            mixedSheet.stagedLine.contains("minus 5 app extensions"),
            "premise: the add sheet's own count phrase changed — got \(mixedSheet.stagedLine)"
        )
        XCTAssertTrue(
            CachePaneView.rowSummary(for: mixed).contains("minus 5 app extensions"),
            "the row counts what was stripped, not what was marked essential — got "
            + CachePaneView.rowSummary(for: mixed)
        )
    }

    // MARK: - The mark on the file a build would reach for

    /// Two files, one bundle id: the chip is the only thing telling the rows
    /// apart, and `preferred` is the CLI's answer because it owns
    /// `find_in_cache`'s version rule.
    func test_the_file_a_build_would_reach_for_is_marked() throws {
        let listing = try twoVersionsOfOneApp()
        XCTAssertEqual(
            CachePaneView.preferredLabel(for: listing[0], among: listing),
            "BUILD USES THIS"
        )
    }

    /// The other half: the superseded sibling gets no chip.
    func test_a_superseded_file_carries_no_mark() throws {
        let listing = try twoVersionsOfOneApp()
        XCTAssertNil(CachePaneView.preferredLabel(for: listing[1], among: listing))
    }

    /// **The case the whole marker turns on, and the one every fixture here
    /// used to miss.**
    ///
    /// `preferred` is true for the file a build would use — so with one file
    /// per bundle id it is true of *every* file. Marking on that flag alone
    /// puts the chip on every row, where it distinguishes nothing. Both cases
    /// above pass under that bug, because both feed a listing that really does
    /// hold two versions; the real cache holds ten unique bundle ids and wore
    /// the chip ten times before a hand check caught it.
    ///
    /// A single record is therefore not an incidental input here. It is the
    /// only input that can fail.
    func test_a_bundle_id_with_only_one_staged_file_is_not_marked() throws {
        let lone = try stagedIPARecord()
        XCTAssertTrue(lone.preferred, "premise: the CLI calls a lone file preferred")
        XCTAssertNil(
            CachePaneView.preferredLabel(for: lone, among: [lone]),
            "nothing to distinguish it from, so the chip says nothing"
        )
    }

    /// Two records for one bundle id, newest first, as `StagedIPAStore.all`
    /// would hold them.
    private func twoVersionsOfOneApp() throws -> [StagedIPARecord] {
        [
            try stagedIPARecord(version: "21.16.2", filename: "com.a.one_21.16.2.ipa"),
            try stagedIPARecord(
                version: "21.9.1", filename: "com.a.one_21.9.1.ipa", preferred: false
            ),
        ]
    }

    // MARK: - Waiting is not empty

    /// The gate. A pane that has never been told anything about the cache must
    /// not say the cache is empty — and `all.isEmpty` is true of both states,
    /// so it cannot be what decides. This is the one test the plan's own
    /// mutation targets: keying the empty state off `all.isEmpty` instead of
    /// `hasLoaded` turns this `.waiting` into `.empty`.
    func test_before_a_listing_lands_the_pane_has_not_asked_rather_than_found_nothing() {
        let store = StagedIPAStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        XCTAssertEqual(CachePaneView.listState(of: store), .waiting)
    }

    /// The state the one above is not: a listing that finished and found
    /// nothing. Reached through a real `load()` whose only output is the
    /// sentinel, because that is the only way `hasLoaded` becomes true —
    /// `all` and `hasLoaded` are `private(set)` and published together.
    func test_a_listing_that_finished_with_nothing_in_it_says_the_cache_is_empty() async {
        let store = StagedIPAStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        await store.load(argsOverride: ["-c", #"printf '{"type":"staged-ipas-end","count":0}\n'"#])

        XCTAssertTrue(store.hasLoaded, "premise: the sentinel-only listing never finished")
        XCTAssertEqual(CachePaneView.listState(of: store), .empty)
    }

    /// And the ordinary state: a finished listing hands the pane every record
    /// it emitted, in the CLI's own order.
    func test_a_finished_listing_becomes_the_pane_s_rows() async {
        let store = StagedIPAStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        await store.load(argsOverride: ["-c", "printf '\(Self.twoVersionListing)'"])

        guard case let .rows(records) = CachePaneView.listState(of: store) else {
            return XCTFail("expected .rows, got \(CachePaneView.listState(of: store))")
        }
        XCTAssertEqual(records.map(\.version), ["21.16.2", "21.9.1"])
    }

    // MARK: - The sentence and the count the user actually reads

    /// The enum keeping waiting and empty apart buys nothing if both branches
    /// print the same words, and until these were values that was unpinned:
    /// swapping the two sentences in the body left all ten tests green.
    /// Measured, not supposed — it is the mutation this test exists for.
    ///
    /// Pinned as literals rather than "these two differ", because "differ" is
    /// satisfied by two sentences that are both wrong, and the specific thing
    /// each has to avoid saying is the point: the waiting sentence must make no
    /// claim about what the cache holds, since it also covers a listing that
    /// failed and one that never ran.
    func test_the_two_empty_states_do_not_say_the_same_thing() {
        XCTAssertEqual(
            CachePaneView.message(for: .waiting),
            "Not read yet — no cache listing has come back."
        )
        XCTAssertEqual(
            CachePaneView.message(for: .empty),
            "Nothing staged. Dumping an app from a phone puts its IPA here."
        )
    }

    /// A pane with rows shows them; there is no sentence in the way.
    func test_a_pane_with_rows_shows_no_sentence_instead_of_them() throws {
        XCTAssertNil(CachePaneView.message(for: .rows([try stagedIPARecord()])))
    }

    /// "0 files" over a pane that has never asked is the same false claim as
    /// "Nothing staged", in smaller type — and an unconditional
    /// `pluralized(staged.all.count, "file")` in the header shipped green
    /// before this was a value. `.empty` withholds the count too, which is a
    /// deliberate divergence from `InstalledAppsSection`'s "0 installed"; see
    /// `headerCount(for:)` for why this pane can afford to and that one
    /// cannot.
    func test_the_header_counts_nothing_until_there_is_something_to_count() throws {
        XCTAssertNil(CachePaneView.headerCount(for: .waiting))
        XCTAssertNil(CachePaneView.headerCount(for: .empty))
        XCTAssertEqual(
            CachePaneView.headerCount(for: .rows([try stagedIPARecord()])), "1 file"
        )
    }

    // MARK: - One row per file

    /// Spec §1: two versions of one app are two rows, because deleting the
    /// stale one is half of what this pane is for. `StagedIPAStore` publishes
    /// both readings of the same listing — `all` (every file) and
    /// `byBundleId` (the one a build would use) — so a `List` over the wrong
    /// one compiles, renders, and quietly shows one row where the cache holds
    /// two. Nothing in a value-level test can see that, so this renders the
    /// pane and counts the rows the backing `NSTableView` actually has.
    ///
    /// Not a text probe, and not because the window is detached — this one is
    /// made key. `SidebarDeviceLayoutTests` records the narrower version of
    /// this fact (no accessibility attributes in a *detached, non-key*
    /// window); the broader one was measured for this pane and is written down
    /// on `ClickHarness.backingTableView(in:)`: keying the window changes nothing,
    /// `accessibilityChildren()` is nil either way, and a `List` cell's
    /// SwiftUI contents create no child views to walk. `numberOfRows` is the
    /// one fact about a rendered `List` that is reliably readable from here.
    func test_two_versions_of_one_app_are_two_rows() async {
        let premise = StagedIPAStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        await premise.load(argsOverride: ["-c", "printf '\(Self.twoVersionListing)'"])
        XCTAssertEqual(
            premise.byBundleId.count, 1,
            "premise: the two fixture records no longer share a bundle id, so a pane "
            + "listing byBundleId would draw two rows too and this proves nothing"
        )

        let table = await Self.renderedTable(loading: Self.twoVersionListing)
        XCTAssertEqual(
            table?.numberOfRows, 2,
            "both staged versions of one app get a row — one row per file, not per "
            + "bundle id"
        )
    }

    // MARK: - The pane asks for the listing itself

    /// Task 5's one piece of behavior, as opposed to its declarations.
    ///
    /// A sidebar row and a `switch` arm are checked by the compiler and by a
    /// row count (`SidebarSectionRowTests`, `SidebarDeviceLayoutTests.
    /// fixedRows`). The `.task` is not checked by either: without it the pane
    /// is perfectly reachable and permanently `.waiting`, showing "Not read yet"
    /// over a 1.0 GB cache, and every other case in this file still passes
    /// because they all hand the store a listing themselves.
    ///
    /// **So this one must not hand it a listing**, which is why it is the only
    /// case here that reaches the store through a stub *executable* rather than
    /// `load(argsOverride:)`. The pane's `.task` calls `staged.load()` with no
    /// arguments — that is the whole point of it — so the only way to make the
    /// production call answer with a fixture is to make the thing it runs one.
    /// The stub-script pattern is `DeviceVitalsPollTests`', and the same
    /// property is what both need: proof that a view spawned a subprocess
    /// nobody asked it to.
    ///
    /// **Polled with `Task.sleep`, not `settle`.** `WindowLayoutTests` measured
    /// this and wrote it down: `RunLoop.main.run(until:)` blocks the main actor,
    /// so a `.task` awaiting a subprocess makes no progress across it (twelve
    /// pumps totalling 600 ms got nowhere, one `Task.sleep` got there at once).
    /// `settle` afterward is for the render, which is a different problem.
    func test_the_pane_reads_the_cache_when_it_appears() async throws {
        let store = StagedIPAStore(runner: CLIRunner(executable: try stubCLI()))
        XCTAssertFalse(
            store.hasLoaded,
            "premise: nothing has read the cache before the pane is on screen"
        )

        let box = hosted(CachePaneView(
            staged: store,
            // Inert effects: these two cases are about what the pane *renders*,
            // and nothing here presses a control. The click section above is
            // where the callbacks are exercised.
            onInstallToMac: { _ in MacInstall(handed: nil, ending: .handedOnly) }, onAddAsApp: { _ in },
            onDelete: { _, _ in .deleted }
        ))
        let deadline = Date().addingTimeInterval(5)
        while !store.hasLoaded && Date() < deadline {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        settle(box)

        XCTAssertTrue(
            store.hasLoaded,
            "the pane never read the cache. Nothing else in the app does it either — "
            + "ContentView has no .cache arm in its section loader, deliberately — so "
            + "the pane is reachable and permanently 'Not read yet'"
        )
        XCTAssertEqual(
            backingTableView(in: box)?.numberOfRows, 2,
            "the listing the pane fetched for itself never became rows"
        )
    }

    // MARK: - The three controls a row draws
    //
    // `CacheRow` is rendered directly here, not through the pane, for the
    // reason `InstalledAppsSectionTests` renders `DeviceAppTile` directly: a
    // row hosted on its own *is* the window, so a coordinate lands where the
    // arithmetic below says it does, and there is no `List` inset, header
    // height or scroll offset in between to be wrong about. It is also the
    // shape this codebase has measured a synthetic click to be safe in — a
    // sweep across a full pane render hung for minutes at near-zero CPU
    // (`DeviceDetailAddWiringTests`' file doc), and nothing here sweeps.
    //
    // Every case passes all three callbacks and asserts on all three. A test
    // that only checked "the callback I pressed fired" would pass just as well
    // against a row whose three controls all call the same closure, or one
    // where a click anywhere fires everything; the two that must *not* fire
    // are what says the click reached the control it was aimed at.

    /// The center of one of the row's three controls, counted from the
    /// trailing edge — index 0 is the trailing-most.
    ///
    /// Derived from `CacheRow`'s own two geometry constants rather than a
    /// hand-picked pixel, so a row that changes its control size stays
    /// targetable. The arithmetic proves nothing on its own; what the cases
    /// below prove is which callback answers at each of the three points, and
    /// that is not a constant.
    private static func controlCentre(_ index: Int, in box: Hosted) -> NSPoint {
        let step = CacheRow.controlSize + CacheRow.controlSpacing
        return NSPoint(
            x: box.size.width - CacheRow.controlSize / 2 - CGFloat(index) * step,
            y: box.size.height / 2
        )
    }

    /// One row, its three callbacks recorded by filename.
    private struct Pressed {
        var installed: [String] = []
        var added: [String] = []
        var deleted: [String] = []
    }

    private func pressRow(
        _ record: StagedIPARecord, at index: Int, width: CGFloat = 500
    ) -> Pressed {
        var pressed = Pressed()
        let row = CacheRow(
            record: record,
            onInstall: { pressed.installed.append($0.filename) },
            onAdd: { pressed.added.append($0.filename) },
            onDelete: { pressed.deleted.append($0.filename) }
        )
        let box = hosted(row, width: width)
        click(at: Self.controlCentre(index, in: box), in: box.window)
        return pressed
    }

    /// Third from the trailing edge. The order — hand it over, add it, delete
    /// it — puts the destructive control furthest from the two that are not,
    /// and this pins the order as much as the wiring: a row that swapped
    /// Install and Delete passes no case in this section.
    func test_pressing_install_to_mac_reaches_only_that_rows_install() throws {
        let record = try stagedIPARecord(bundleId: "com.a.two", filename: "com.a.two_9.9.ipa")
        let pressed = pressRow(record, at: 2)
        XCTAssertEqual(pressed.installed, ["com.a.two_9.9.ipa"])
        XCTAssertEqual(pressed.added, [], "Add sits inboard of Install and must not answer here")
        XCTAssertEqual(pressed.deleted, [], "nothing destructive may answer the Install control")
    }

    func test_pressing_add_as_app_reaches_only_that_rows_add() throws {
        let record = try stagedIPARecord(bundleId: "com.a.two", filename: "com.a.two_9.9.ipa")
        let pressed = pressRow(record, at: 1)
        XCTAssertEqual(pressed.added, ["com.a.two_9.9.ipa"])
        XCTAssertEqual(pressed.installed, [])
        XCTAssertEqual(pressed.deleted, [], "the delete control is trailing of this one")
    }

    func test_pressing_delete_reaches_only_that_rows_delete() throws {
        let record = try stagedIPARecord(bundleId: "com.a.two", filename: "com.a.two_9.9.ipa")
        let pressed = pressRow(record, at: 0)
        XCTAssertEqual(pressed.deleted, ["com.a.two_9.9.ipa"])
        XCTAssertEqual(pressed.installed, [])
        XCTAssertEqual(pressed.added, [])
    }

    /// The negative control the three above need to mean anything: a click on
    /// the row's *text*, where the name and the summary line are, reaches none
    /// of them. Without this, a row that had accidentally become one big
    /// `Button` — the defect `InstalledAppsSectionTests` closed on
    /// `DeviceAppTile` — would satisfy every case above, since a whole-row
    /// button answers at the trailing edge too.
    func test_a_click_on_the_rows_text_reaches_none_of_the_three_controls() throws {
        var pressed = Pressed()
        let row = CacheRow(
            record: try stagedIPARecord(),
            onInstall: { pressed.installed.append($0.filename) },
            onAdd: { pressed.added.append($0.filename) },
            onDelete: { pressed.deleted.append($0.filename) }
        )
        let box = hosted(row, width: 500)
        click(at: NSPoint(x: 60, y: box.size.height / 2), in: box.window)
        XCTAssertEqual(pressed.installed, [])
        XCTAssertEqual(pressed.added, [])
        XCTAssertEqual(pressed.deleted, [])
    }

    /// Each row hands up *its own* record, which every case above would also
    /// pass if the row closed over one fixed value — there is only ever one
    /// row in those windows. Two rows, two records, one control.
    func test_each_row_hands_up_its_own_record() throws {
        let first = try stagedIPARecord(bundleId: "com.a.one", filename: "com.a.one_1.0.ipa")
        let second = try stagedIPARecord(bundleId: "com.b.two", filename: "com.b.two_2.0.ipa")
        XCTAssertEqual(pressRow(first, at: 1).added, ["com.a.one_1.0.ipa"])
        XCTAssertEqual(pressRow(second, at: 1).added, ["com.b.two_2.0.ipa"])
    }

    // MARK: - Which presses ask first

    /// Spec §4's other half, and the one that makes the confirm mean
    /// something: a complete dump goes straight to PlayCover. A pane that
    /// confirmed every install would satisfy the stripped case below and be
    /// wrong.
    /// The Install to Mac control's tooltip says what to expect of any app
    /// under PlayCover (spec 5d item 4).
    func test_install_to_mac_says_what_to_expect() throws {
        let help = CachePaneView.installHelp(for: try stagedIPARecord())
        XCTAssertTrue(help.hasPrefix("Install ExamplePlayer on this Mac with PlayCover. "))
        XCTAssertTrue(help.hasSuffix(PlayCover.expectations))
    }

    func test_a_complete_dump_installs_with_no_prompt() throws {
        XCTAssertEqual(
            CachePaneView.intent(.installToMac, for: try stagedIPARecord()), .perform
        )
    }

    /// Spec §4: a stripped IPA discloses before it installs, **in the add
    /// sheet's own sentence** rather than a second grammar for the same fact.
    /// Tied to `Prefill.stagedLine`'s output rather than to a literal, so a
    /// pane that started phrasing this itself fails here even if what it
    /// phrased happened to read well.
    func test_a_stripped_dump_repeats_the_add_sheets_disclosure_before_installing() throws {
        let record = try stagedIPARecord(stripped: #"["a.appex","b.appex"]"#)
        let sheetSentence = AddAppSheet.Prefill(
            staged: record, installedVersion: nil
        ).stagedLine
        XCTAssertTrue(
            sheetSentence.contains("minus 2 app extensions"),
            "premise: the add sheet's disclosure changed shape — got \(sheetSentence)"
        )
        XCTAssertEqual(
            CachePaneView.intent(.installToMac, for: record), .confirm(sheetSentence)
        )
    }

    /// Nothing is lost by opening a sheet, and the sheet is itself the
    /// confirmation — a prompt in front of it would be a confirm to reach a
    /// confirm.
    func test_adding_as_an_app_never_asks() throws {
        XCTAssertEqual(CachePaneView.intent(.addAsApp, for: try stagedIPARecord()), .perform)
    }

    /// **Every** delete asks, including the one nothing depends on. Spec §3
    /// narrows the CLI's *objection* to the last staged IPA a configured app
    /// can build from — it does not narrow the ordinary destructive confirm,
    /// and this pane deletes a file outright.
    ///
    /// The fixture is deliberately the case the objection must not fire for: a
    /// superseded copy, complete, that a build would not reach for. It still
    /// gets a confirm; what it must not get is an objection, and nothing here
    /// can hand it one — the objection only ever arrives from the CLI (see
    /// `CachePaneView.Blocked`).
    func test_every_delete_asks_first_even_for_a_file_no_build_would_use() throws {
        let superseded = try stagedIPARecord(preferred: false)
        guard case let .confirm(sentence) = CachePaneView.intent(.delete, for: superseded) else {
            return XCTFail("a delete must never go straight through")
        }
        XCTAssertTrue(
            sentence.contains(superseded.filename),
            "the confirm names the file it is about — got \(sentence)"
        )
    }

    /// What the ordinary confirm says, pinned because it makes a claim the
    /// tweak pane's confirm cannot: `--delete-staged` calls `unlink`, where
    /// `--delete-tweak` calls `move_to_trash`. Copying the tweak confirm's
    /// "you can recover it from the Trash" over here would be false.
    func test_the_delete_confirm_does_not_promise_a_trip_to_the_trash() throws {
        let record = try stagedIPARecord()
        XCTAssertEqual(
            CachePaneView.intent(.delete, for: record),
            .confirm(
                "com.a.one_1.0.ipa — 128.3 MB. This deletes the file outright; it does "
                + "not go to the Trash. Getting it back means dumping the app from a "
                + "phone again."
            )
        )
    }

    /// The two confirms are told apart by their titles and their verbs, not
    /// only by their bodies — a user who reads one word of a dialog reads the
    /// button.
    func test_the_two_confirms_do_not_present_themselves_the_same_way() {
        XCTAssertEqual(CachePaneView.confirmTitle(for: .installToMac), "Install a partial dump?")
        XCTAssertEqual(CachePaneView.confirmTitle(for: .delete), "Delete this staged IPA?")
        XCTAssertEqual(CachePaneView.confirmVerb(for: .installToMac), "Install")
        XCTAssertEqual(CachePaneView.confirmVerb(for: .delete), "Delete")
        XCTAssertNil(
            CachePaneView.confirmTitle(for: nil),
            "the title is read on every draw, presented or not"
        )
    }

    // MARK: - The CLI states the objection, the pane offers the answer

    /// Spec §3, structurally: `Blocked` is the only thing that puts an
    /// objection on screen, and the only thing it can be built from is the
    /// CLI's own sentence. It is never handed the app list, so there is
    /// nothing here to compose a local equivalent *out of* — which is the
    /// point, since a composed one drifts from the CLI's the first time either
    /// side changes.
    ///
    /// Two different sentences, because one would be satisfied by a constant.
    func test_a_refused_delete_carries_the_clis_sentence_unaltered() throws {
        let record = try stagedIPARecord()
        for objection in [
            "com.a.one_1.0.ipa is the only staged IPA for: example",
            "something else entirely",
        ] {
            XCTAssertEqual(
                CachePaneView.Blocked(.inUse(objection: objection), record: record)?.objection,
                objection
            )
        }
    }

    /// The other two answers are not objections and must not raise the
    /// "Delete Anyway" alert: forcing cannot fix a delete that failed for some
    /// other reason, and offering it over one would be an override for a
    /// refusal that never happened.
    func test_only_a_refusal_raises_the_objection() throws {
        let record = try stagedIPARecord()
        XCTAssertNil(CachePaneView.Blocked(.deleted, record: record))
        XCTAssertNil(CachePaneView.Blocked(.failed("couldn't delete it"), record: record))
    }

    // MARK: - The pane's own rewrap of those three callbacks

    /// The one claim no value-level case above can make: on a **rendered
    /// pane**, pressing Delete does not delete.
    ///
    /// Everything between the row's `onDelete` and the effect is the pane's —
    /// `press(_:_:)` consults `intent(_:for:)` and parks the record in
    /// `pending` instead — and that hop is invisible to `CacheRow`'s own click
    /// cases, which stop at the callback. A pane wired `onDelete: { onDelete($0,
    /// false) }`, skipping the confirm, passes every one of them.
    ///
    /// **The Add press is not decoration.** Without it the delete assertion is
    /// satisfied by a click that reached nothing at all — the coordinates being
    /// wrong, the `List` swallowing the event, the row never having been
    /// realized — and would read as "the confirm intercepted it". Add is
    /// pressed first, on the same row, from the same cell rect, and must land.
    ///
    /// Coordinates come from the realized cell's own frame rather than from
    /// the window's width: `.listStyle(.inset)` insets the cell inside the
    /// table and nothing here should be guessing by how much.
    func test_in_the_pane_a_delete_press_asks_before_anything_is_deleted() async throws {
        let store = StagedIPAStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        var added: [String] = []
        var deleted: [String] = []
        let box = hosted(CachePaneView(
            staged: store,
            onInstallToMac: { _ in MacInstall(handed: nil, ending: .handedOnly) },
            onAddAsApp: { added.append($0.filename) },
            onDelete: { record, _ in
                deleted.append(record.filename)
                return .deleted
            }
        ))
        await store.load(argsOverride: ["-c", "printf '\(Self.twoVersionListing)'"])
        XCTAssertTrue(store.hasLoaded, "premise: the listing never reached the store")
        settle(box)

        let table = try XCTUnwrap(backingTableView(in: box), "the pane drew no List")
        let cell = try XCTUnwrap(
            table.view(atColumn: 0, row: 0, makeIfNecessary: true),
            "row 0 was never realized, so there is nothing to click"
        )
        let frame = cell.convert(cell.bounds, to: nil)
        let step = CacheRow.controlSize + CacheRow.controlSpacing
        func centre(_ index: Int) -> NSPoint {
            NSPoint(
                x: frame.maxX - CacheRow.controlSize / 2 - CGFloat(index) * step,
                y: frame.midY
            )
        }

        click(at: centre(1), in: box.window)
        XCTAssertEqual(
            added, ["com.a.one_21.16.2.ipa"],
            "the positive control: a press on this row's Add must reach the pane's "
            + "callback. Without it the delete assertion below cannot tell an "
            + "intercepted press from a click that landed nowhere"
        )

        click(at: centre(0), in: box.window)
        // **Yield before asserting, or this proves nothing.** Performing a
        // delete is `Task { await onDelete(...) }`, and a `Task` started on the
        // main actor does not run while this method is still on it — so an
        // assertion made straight after the click reads `[]` whether the
        // confirm intercepted the press or the delete was merely still
        // queued. Measured, not supposed: with this loop removed, a pane
        // rewired to skip the confirm entirely (`case .confirm: perform(...)`)
        // passed all 49 cases in this file and `StagedIPAStoreTests` together.
        // Sleeping rather than `settle` for the reason
        // `test_the_pane_reads_the_cache_when_it_appears` records —
        // `RunLoop.main.run(until:)` blocks the main actor, so a queued Task
        // makes no progress across it.
        for _ in 0..<10 { try? await Task.sleep(nanoseconds: 20_000_000) }
        XCTAssertEqual(
            deleted, [],
            "Delete must raise the confirm, not run. Every delete asks first — this "
            + "verb unlinks the file"
        )
    }

    /// Install to Mac waits up to two minutes for PlayCover's install, and
    /// the strip had no Stop (review 23-2, §11). Its Stop ends the wait the
    /// press started, and the strip then says how it ended.
    func test_the_install_strip_has_a_stop_that_ends_the_wait() async throws {
        try AccessibilityTree.requireServed()
        let store = StagedIPAStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        var stopped = false
        let box = hosted(CachePaneView(
            staged: store,
            onInstallToMac: { _ in
                while !Task.isCancelled { try? await Task.sleep(nanoseconds: 20_000_000) }
                stopped = true
                return MacInstall(handed: nil, ending: .stopped)
            },
            onAddAsApp: { _ in }, onDelete: { _, _ in .deleted }
        ))
        await store.load(argsOverride: ["-c", "printf '\(Self.twoVersionListing)'"])
        XCTAssertTrue(store.hasLoaded, "premise: the listing never reached the store")
        settle(box)
        let table = try XCTUnwrap(backingTableView(in: box), "the pane drew no List")
        let cell = try XCTUnwrap(table.view(atColumn: 0, row: 0, makeIfNecessary: true),
                                 "row 0 was never realized, so there is nothing to click")
        let frame = cell.convert(cell.bounds, to: nil)
        let step = CacheRow.controlSize + CacheRow.controlSpacing
        click(at: NSPoint(x: frame.maxX - CacheRow.controlSize / 2 - 2 * step, y: frame.midY), in: box.window)

        try await Self.press("Stop", whenShownIn: box)
        await eventually({ stopped }, "the strip's Stop never reached the install")
        let line = MacInstall(handed: nil, ending: .stopped).endingLines(app: "Example", retry: .cacheRow)[0].text
        try await Self.waitForText(line, in: box)
    }

    /// Click the button named `name` once the tree shows it.
    static func press(_ name: String, whenShownIn box: Hosted, timeout: TimeInterval = 5) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while true {
            let shown = try AccessibilityTree.nodes(in: box.window)
            if shown.contains(where: { $0.role == kAXButtonRole && $0.name == name }) { break }
            guard Date() < deadline else { return XCTFail("no \(name) button came up") }
            try await Task.sleep(nanoseconds: 100_000_000)
            settle(box, seconds: 0.05)
        }
        try clickButton(named: name, in: box.window)
    }

    /// Wait for a static text reading `text`, trimmed as the strip trims it.
    static func waitForText(_ text: String, in box: Hosted, timeout: TimeInterval = 5) async throws {
        let wanted = text.trimmingCharacters(in: .whitespaces)
        let deadline = Date().addingTimeInterval(timeout)
        var shown: [String] = []
        while Date() < deadline {
            shown = try AccessibilityTree.texts(in: box.window)
            if shown.contains(wanted) { return }
            try await Task.sleep(nanoseconds: 100_000_000)
            settle(box, seconds: 0.05)
        }
        XCTFail("“\(wanted)” never showed: \(shown)")
    }

    /// The second half of the two-step: **"Delete Anyway" forces.**
    ///
    /// The whole point of the objection is that `--force` is the one action
    /// that answers it, so a retry that ran the same unforced delete again
    /// would be refused again — the same strip, forever, with no way out but
    /// the Finder. That mutation survived every other case in this file while
    /// the objection was an `.alert`, whose buttons a hosted window cannot
    /// press; it is why the objection is a strip and why this pane has a
    /// `blocked:` seam (see `CachePaneView.init`).
    ///
    /// The record is checked too, not just the flag: forcing a delete of *some
    /// other file* would satisfy a flag-only assertion.
    func test_delete_anyway_re_runs_the_delete_with_the_force_the_cli_named() async throws {
        let attempts = DeleteAttempts()
        let box = try objectionPane(recording: attempts)

        Self.press(.deleteAnyway, in: box)
        for _ in 0..<10 { try? await Task.sleep(nanoseconds: 20_000_000) }

        XCTAssertEqual(attempts.all.count, 1, "one retry, not none and not a loop")
        XCTAssertEqual(attempts.all.first?.filename, "com.a.one_1.0.ipa")
        XCTAssertEqual(
            attempts.all.first?.force, true,
            "unforced, the CLI raises the same objection again and the strip never "
            + "clears — the flag is the whole answer"
        )
    }

    /// Its neighbour, and what says the press above landed on the button it
    /// was aimed at rather than anywhere on the strip: the other button
    /// deletes nothing, and takes the strip with it.
    ///
    /// The second press is the positive control. "Keep It" clears the refusal,
    /// so the force button is no longer on screen and the same column that
    /// forced a delete in the case above now finds nothing. Had the first
    /// press missed, the strip would still be up and that second column would
    /// have forced one.
    func test_keeping_the_file_deletes_nothing_and_puts_the_offer_away() async throws {
        let attempts = DeleteAttempts()
        let box = try objectionPane(recording: attempts)

        Self.press(.keepIt, in: box)
        for _ in 0..<10 { try? await Task.sleep(nanoseconds: 20_000_000) }
        XCTAssertEqual(attempts.all.map(\.filename), [], "Keep It deletes nothing")

        Self.press(.deleteAnyway, in: box)
        for _ in 0..<10 { try? await Task.sleep(nanoseconds: 20_000_000) }
        XCTAssertEqual(
            attempts.all.map(\.filename), [],
            "the offer is gone with the refusal. If this fired, the press above "
            + "reached nothing and the case proved only that a miss deletes nothing"
        )
    }

    /// A pane already showing a refusal, with the delete effect recorded.
    ///
    /// Seeded through the `blocked:` seam rather than reached by pressing the
    /// destructive confirm, which is an `.alert` and so unreachable from here.
    /// The store is left unloaded on purpose: with no rows, the strip is the
    /// only thing in the pane with a control in it, so nothing below can be
    /// what a click found.
    private func objectionPane(recording attempts: DeleteAttempts) throws -> Hosted {
        let record = try stagedIPARecord()
        let refusal = try XCTUnwrap(
            CachePaneView.Blocked(
                .inUse(objection: "com.a.one_1.0.ipa is the only staged IPA for: example"),
                record: record
            )
        )
        let box = hosted(CachePaneView(
            staged: StagedIPAStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))),
            onInstallToMac: { _ in MacInstall(handed: nil, ending: .handedOnly) },
            onAddAsApp: { _ in },
            onDelete: { record, force in
                attempts.all.append((record.filename, force))
                return .deleted
            },
            blocked: refusal
        ))
        settle(box, seconds: 0.1)
        return box
    }

    /// Press down one of the objection strip's two buttons, located by
    /// counting button-widths inward from the trailing edge. See
    /// `ObjectionButton` for why that order is not the reading order.
    ///
    /// **The x is exact and the y is scanned**, which is not a hedge: the
    /// buttons have a fixed width and the strip a fixed gutter, so the columns
    /// are known, but the header above them does not, and hardcoding a height
    /// nobody wrote down is how a case starts passing on a click that landed
    /// nowhere. A short scan down the top of the pane finds the band instead.
    ///
    /// Bounded, and clear of the two things a click must not land on in this
    /// app: `.textSelection(.enabled)` text and a `Picker`, either of which
    /// takes AppKit into a tracking loop that does not return
    /// (`DeviceDecryptSectionTests`). The pane a scan runs against here has
    /// neither — its store is never loaded, so it draws the "Not read yet"
    /// sentence and no rows at all, and `ErrorBanner`, which *is* selectable,
    /// is not on screen.
    private static func press(_ button: ObjectionButton, in box: Hosted) {
        let width = CachePaneView.objectionButtonWidth
        let x = box.size.width - Metrics.paneGutter - width / 2
            - CGFloat(button.rawValue) * width
        for offset in stride(from: CGFloat(2), through: 100, by: 4) {
            click(at: NSPoint(x: x, y: box.size.height - offset), in: box.window)
            // Let SwiftUI redraw between presses, which is what makes a scan
            // safe to point at a button that removes itself. Without it every
            // offset in the band lands on a control that is logically gone but
            // still on screen — `blocked` is nil after the first hit, and the
            // hosting view does not act on that until the run loop turns — so
            // one press was recorded six times and the case read as a retry
            // loop. A real pointer cannot outrun a redraw; this stops the
            // harness pretending it can.
            settle(box, seconds: 0.01)
        }
    }

    /// A `graft` that ignores its arguments and prints the two-version
    /// listing.
    ///
    /// It ignores them on purpose: the argv `StagedIPAStore.load` builds is
    /// pinned by `StagedIPAStoreTests` against `loadArgs()`, and a stub that
    /// re-checked it here would be a second, weaker copy of that assertion
    /// standing between this case and the thing it is actually about.
    ///
    /// No apostrophes and no `%` in the payload — it is the `printf` *format
    /// string*, inside single quotes, so an apostrophe would close the quote
    /// early and a `%` would be read as a conversion. Neither is escaped here
    /// and neither needs to be: the JSON these builders emit contains only
    /// double quotes, and the `\n` separators are exactly the escape `printf`
    /// is being used for. `SidebarDeviceLayoutTests.unreadableDevice` records
    /// the same constraint after a fixture tripped it.
    private func stubCLI() throws -> URL {
        let path = NSTemporaryDirectory() + "staged_ipas_stub_\(UUID().uuidString).sh"
        let script = """
        #!/bin/sh
        printf '\(Self.twoVersionListing)'
        """
        try script.write(toFile: path, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o755], ofItemAtPath: path
        )
        addTeardownBlock { try? FileManager.default.removeItem(atPath: path) }
        return URL(fileURLWithPath: path)
    }

    // MARK: - Fixtures

    /// Two staged versions of one app, the newer one preferred — the shape a
    /// real cache produces once a phone's app has been dumped twice, and the
    /// only shape in which this pane's central claim (one row per *file*) says
    /// anything.
    ///
    /// Built from `stagedIPAStreamLine`, which `StagedIPAStoreTests` shares:
    /// the fields here are the wire contract, and a second hand-written copy
    /// of them is a place that contract can go stale in silence, since
    /// `buildEvents()` drops an unparseable line with `try?` rather than
    /// failing.
    private static let twoVersionListing = [
        stagedIPAStreamLine(bundleId: "com.a.one", version: "21.16.2", preferred: true),
        stagedIPAStreamLine(bundleId: "com.a.one", version: "21.9.1", preferred: false),
        stagedIPAsEndLine(2),
    ].joined(separator: #"\n"#) + #"\n"#

    /// The pane on screen, the listing arriving *after* it, and the
    /// `NSTableView` behind its `List` handed back.
    ///
    /// **That order is the production order and the reason this is not two
    /// lines at the call site.** Task 5 puts `.task { await staged.load() }` on
    /// this pane, so the listing always lands into a pane that is already
    /// rendered, and a test that loaded first would only ever exercise the
    /// first-render path — leaving the observation-driven update, which is the
    /// one SwiftUI can get wrong, untested.
    ///
    /// **Two loads run here, and the premise below is what keeps that from
    /// becoming a trap.** The pane's own `.task` fires inside `hosted(...)` —
    /// measured by logging `StagedIPAStore.loadGeneration`, not inferred: the
    /// production call starts first (generation 1, against `/bin/sh`, which
    /// fails), and the explicit load below is generation 2 and publishes. The
    /// store settles concurrent loads by generation and **refuses to publish a
    /// stale one**, so an ordering where those two swap leaves `hasLoaded`
    /// false and the table nil — with the row-count assertion at the call site
    /// blaming `byBundleId` for it. That is not hypothetical: deleting the
    /// `layoutSubtreeIfNeeded`/`displayIfNeeded` pair from `hosted(_:size:)`,
    /// an ordinary-looking tidy of a helper four files now share, produces
    /// exactly that and reports it as "both staged versions of one app get a
    /// row". Asserting the store actually published makes a refused publish
    /// fail as itself instead.
    ///
    /// `hosted(_:size:)`, `settle` and `backingTableView(in:)` are `ClickHarness`'.
    /// The fixed size is what a `List` needs — see that helper's own doc.
    private static func renderedTable(
        loading script: String, file: StaticString = #filePath, line: UInt = #line
    ) async -> NSTableView? {
        let store = StagedIPAStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        let box = hosted(CachePaneView(
            staged: store,
            // Inert effects: these two cases are about what the pane *renders*,
            // and nothing here presses a control. The click section above is
            // where the callbacks are exercised.
            onInstallToMac: { _ in MacInstall(handed: nil, ending: .handedOnly) }, onAddAsApp: { _ in },
            onDelete: { _, _ in .deleted }
        ))
        await store.load(argsOverride: ["-c", "printf '\(script)'"])
        XCTAssertTrue(
            store.hasLoaded,
            "premise: the listing never reached the store, so the row count below is "
            + "measuring an empty pane rather than the join it names. The likeliest "
            + "cause is not the fixture: this pane's own .task runs a second, "
            + "competing load, and StagedIPAStore refuses to publish whichever one "
            + "is no longer the newest in flight",
            file: file, line: line
        )
        settle(box)
        return backingTableView(in: box)
    }
}
