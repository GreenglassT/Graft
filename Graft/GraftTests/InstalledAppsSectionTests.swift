import SwiftUI
import XCTest
@testable import Graft

/// The four states one installed-app row can be in, and — separately — that
/// the controls those states draw are the ones a real click actually reaches.
///
/// `rowState` is pinned as values: a pure function of an installed app, a
/// config, a cache and one boolean, the same treatment
/// `DeviceAppsSectionCopyTests` gives `statusLine(of:in:kind:)`, for the same
/// reason — asserting on a rendered `body` is expensive and brittle where a
/// value comparison is neither. What a value comparison cannot prove is
/// whether the `Button` `.configured` draws is actually clickable where it
/// looks clickable, or whether a click that lands correctly is then wired to
/// the right callback — both are facts about AppKit hit-testing and view
/// construction, not about the state. The "Rendering" section below renders
/// `DeviceAppTile` directly — it is not `private`, exactly so this file can —
/// and, once, the whole `InstalledAppsSection`, on a headless `NSHostingView`,
/// and sends them real `NSEvent`s, the same way a reviewer once measured, by
/// hand, that a click on the dead space between a short app name and its
/// chip reached nothing without `.contentShape(Rectangle())`.
@MainActor
final class InstalledAppsSectionTests: XCTestCase {
    private static func installed(
        _ bundleId: String, encrypted: Bool = true, version: String = "1.0"
    ) -> DeviceAppRecord {
        DeviceAppRecord(
            bundleId: bundleId, name: "Example", version: version, encrypted: encrypted
        )
    }

    // MARK: - What state one row resolves to

    func test_an_encrypted_app_with_nothing_staged_is_dumpable() {
        let state = InstalledAppsSection.rowState(
            for: Self.installed("com.a.one"),
            configured: [:], staged: [:]
        )
        XCTAssertEqual(state, .dumpable)
    }

    func test_a_decrypted_app_nobody_configured_offers_nothing() {
        let state = InstalledAppsSection.rowState(
            for: Self.installed("com.a.one", encrypted: false),
            configured: [:], staged: [:]
        )
        XCTAssertEqual(state, .inert)
    }

    func test_a_staged_app_can_be_added() throws {
        let state = InstalledAppsSection.rowState(
            for: Self.installed("com.a.one"),
            configured: [:],
            staged: ["com.a.one": try stagedIPARecord(bundleId: "com.a.one")]
        )
        guard case let .addable(record) = state else {
            return XCTFail("expected .addable, got \(state)")
        }
        XCTAssertEqual(record.filename, "com.a.one_1.0.ipa")
    }

    /// The other half of the state a `.addable` row can carry: nothing about
    /// the join requires the app to still be encrypted. `DeviceAppTile`'s own
    /// rendering of that combination — Add with no lock and no Dump — is
    /// pinned separately below, since it is a fact about the view's
    /// conditionals and not about this decision.
    func test_a_decrypted_app_with_something_staged_is_still_addable() throws {
        let state = InstalledAppsSection.rowState(
            for: Self.installed("com.a.one", encrypted: false),
            configured: [:],
            staged: ["com.a.one": try stagedIPARecord(bundleId: "com.a.one")]
        )
        guard case .addable = state else {
            return XCTFail("expected .addable, got \(state)")
        }
    }

    func test_a_configured_app_says_so_instead_of_offering_a_second_copy() throws {
        let state = InstalledAppsSection.rowState(
            for: Self.installed("com.a.one"),
            configured: ["com.a.one": ["example"]],
            staged: ["com.a.one": try stagedIPARecord(bundleId: "com.a.one")]
        )
        XCTAssertEqual(state, .configured("example", siblings: []))
    }

    /// Design spec §4: two config entries can share a bundle id. The join
    /// must carry both rather than collapsing to one — that is what makes the
    /// second name representable at all, for `configuredHelp` to name on
    /// hover and for a future Task 7 to select past.
    func test_two_config_entries_sharing_a_bundle_id_are_both_carried_by_the_row() {
        let state = InstalledAppsSection.rowState(
            for: Self.installed("com.a.one"),
            configured: ["com.a.one": ["example", "example-beta"]],
            staged: [:]
        )
        XCTAssertEqual(state, .configured("example", siblings: ["example-beta"]))
    }

    /// The gate. Before the cache listing lands, a row must claim nothing
    /// about it — and "nothing staged" is a claim. `staged: nil` on its own —
    /// no dictionary at all, not even one holding this bundle id — is now
    /// what stands for "never asked"; there is no separate flag left to
    /// disagree with it.
    func test_before_the_listing_lands_no_row_is_addable() {
        let state = InstalledAppsSection.rowState(
            for: Self.installed("com.a.one"),
            configured: [:],
            staged: nil
        )
        XCTAssertEqual(state, .dumpable, "the pre-feature shape, until the cache has answered")
    }

    // MARK: - Rendering
    //
    // `click(at:in:)` and `hosted(_:width:)` are shared from ClickHarness.swift
    // — see that file's own doc for the AppKit mechanics (window-key,
    // fittingSize, flipped vs. unflipped coordinates) this section's tests
    // depend on.

    /// Finding 3's fix, measured rather than theorised: the whole-row
    /// `Button` this row used to draw for `.configured` is gone, and the
    /// "IN YOUR APPS" chip is now the only thing on the row that answers a
    /// click. This single test pins both halves of that claim on one
    /// fixture — a decrypted, unconfigured-elsewhere app so the chip is the
    /// row's *only* trailing control, nothing to its left or right to
    /// confuse which element a coordinate landed on:
    ///
    ///  1. A click on the `Spacer` between the name and the chip — most of a
    ///     short row's width, and exactly where the old whole-row `Button`
    ///     used to answer — now reaches nothing.
    ///  2. A click on the chip itself, at the trailing edge, still selects
    ///     the row's primary config name — including with a sibling
    ///     present, design spec §4's multi-name shape, exercised here rather
    ///     than only in a value-level test.
    func test_only_the_configured_chip_not_the_row_answers_a_click() {
        var selected: [String] = []
        let tile = DeviceAppTile(
            app: Self.installed("com.a.one", encrypted: false),
            state: .configured("example", siblings: ["example-beta"]),
            onSelectApp: { selected.append($0) }
        )
        let box = hosted(tile, width: 500)
        click(at: NSPoint(x: box.size.width / 2, y: box.size.height / 2), in: box.window)
        XCTAssertEqual(
            selected, [],
            "the dead space between the name and the chip must no longer reach "
            + "onSelectApp — only the chip itself is a Button now"
        )
        click(at: NSPoint(x: box.size.width - 16, y: box.size.height / 2), in: box.window)
        XCTAssertEqual(
            selected, ["example"],
            "a click on the chip — the row's only trailing control here — must "
            + "still select the row's primary config name"
        )
    }

    /// Finding 3's own regression, closed: a `.configured` row that is still
    /// FairPlay-encrypted draws the lock badge and Dump again, alongside its
    /// chip rather than instead of it. Before this fix `rowState` returning
    /// `.configured` before any `app.encrypted` test meant this arm drew
    /// only the chip — an app already added lost its only re-dump route the
    /// moment it was configured, with no other per-app dump control
    /// anywhere in the app. Dump sits at the trailing edge here, the same
    /// position `.dumpable`'s and `.addable`'s own trailing-edge tests
    /// check, and it — not the chip, which sits inboard of it now that the
    /// badge and Dump both draw — is what a trailing-edge click must reach.
    func test_an_encrypted_configured_row_still_offers_the_lock_badge_and_dump() {
        var dumped = false
        var selected: [String] = []
        let tile = DeviceAppTile(
            app: Self.installed("com.a.one", encrypted: true),
            state: .configured("example", siblings: []),
            onDump: { dumped = true },
            onSelectApp: { selected.append($0) }
        )
        let box = hosted(tile, width: 500)
        click(at: NSPoint(x: box.size.width - 16, y: box.size.height / 2), in: box.window)
        XCTAssertTrue(
            dumped,
            "Dump is the rightmost control on an encrypted .configured row — the "
            + "affordance this row lost entirely before this fix"
        )
        XCTAssertEqual(
            selected, [],
            "the trailing-edge click landed on Dump, not the inboard chip — "
            + "onSelectApp must not also fire"
        )
    }

    /// Guards against a mutant that draws `dumpButton` for `.inert` the way
    /// `.dumpable` does: nothing at the trailing edge of a truly inert row
    /// may answer a click.
    func test_clicking_the_trailing_edge_of_an_inert_row_does_nothing() {
        var dumped = false
        let tile = DeviceAppTile(
            app: Self.installed("com.a.one", encrypted: false),
            state: .inert,
            onDump: { dumped = true }
        )
        let box = hosted(tile, width: 500)
        click(at: NSPoint(x: box.size.width - 16, y: box.size.height / 2), in: box.window)
        XCTAssertFalse(
            dumped,
            "an .inert row draws no Dump control — a click where one would sit on "
            + "a .dumpable row must not reach onDump"
        )
    }

    /// The `.addable` arm most worth pinning, because it is the one with a
    /// conditional in it (`if app.encrypted`, twice) rather than a fixed set
    /// of controls: a decrypted sideload or LiveContainer app with something
    /// staged offers Add and nothing else. Clicking the trailing edge — where
    /// Dump sits on an encrypted row — must reach Add, not Dump, because Add
    /// is the only control there.
    func test_a_decrypted_addable_row_offers_add_with_no_dump_at_the_trailing_edge() throws {
        var added: StagedIPARecord?
        var dumped = false
        let record = try stagedIPARecord(bundleId: "com.a.one")
        let tile = DeviceAppTile(
            app: Self.installed("com.a.one", encrypted: false),
            state: .addable(record),
            onDump: { dumped = true },
            onAdd: { added = $0 }
        )
        let box = hosted(tile, width: 500)
        click(at: NSPoint(x: box.size.width - 16, y: box.size.height / 2), in: box.window)
        XCTAssertEqual(
            added?.filename, record.filename,
            "Add is the rightmost — and only — control on a decrypted addable row"
        )
        XCTAssertFalse(dumped, "a decrypted app draws no Dump button even when addable")
    }

    /// The companion case: still encrypted, so Dump stays alongside Add — a
    /// newer version on the phone than the one staged is still worth
    /// dumping. Without this, a fix for the case above could overcorrect and
    /// drop Dump unconditionally rather than only when decrypted.
    func test_an_encrypted_addable_row_still_offers_dump_at_the_trailing_edge() throws {
        var dumped = false
        let record = try stagedIPARecord(bundleId: "com.a.one")
        let tile = DeviceAppTile(
            app: Self.installed("com.a.one", encrypted: true),
            state: .addable(record),
            onDump: { dumped = true }
        )
        let box = hosted(tile, width: 500)
        click(at: NSPoint(x: box.size.width - 16, y: box.size.height / 2), in: box.window)
        XCTAssertTrue(
            dumped,
            "Dump is still the rightmost control on an encrypted addable row"
        )
    }

    // MARK: - The section's own wiring

    private static let sectionUdid = "00008030-0000000000000003"

    private static func deviceRecord() throws -> DeviceRecord {
        let json = #"{"udid":"\#(sectionUdid)","name":"Test iPhone","model":"iPhone14,2","ios_version":"17.0","connection_type":"USB","is_default":false}"#
        return try JSONDecoder().decode(DeviceRecord.self, from: Data(json.utf8))
    }

    /// One real `DeviceAppsStore.load`, against a stubbed `/bin/sh`, the same
    /// idiom `DeviceDetailLayoutTests.deviceAppsStore` uses — `apps` is
    /// `private(set)`, so populating it for a test means running a load, not
    /// assigning it.
    private func seededDeviceAppsStore(bundleId: String, name: String, encrypted: Bool) async -> DeviceAppsStore {
        let store = DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        let line = #"{"type":"device-app","bundle_id":"\#(bundleId)","name":"\#(name)","version":"1.0","encrypted":\#(encrypted),"frontmost":false}\n"#
            + #"{"type":"device-apps-end","count":1,"encrypted":\#(encrypted ? 1 : 0)}\n"#
        await store.load(udid: Self.sectionUdid, argsOverride: ["-c", "printf '\(line)'"])
        return store
    }

    /// The third mutation the coordinator's own review found surviving:
    /// `InstalledAppsSection.body`'s `ForEach` rewraps `DeviceAppTile`'s
    /// single-argument `onAdd` into the section's two-argument one —
    /// `onAdd: { record in onAdd(app, record) }` — and nothing above pins
    /// that rewrap. Every other test in this file renders `DeviceAppTile`
    /// directly, which cannot see it; this one renders the section itself.
    ///
    /// The vertical sweep, rather than a single computed point, is
    /// deliberate: this render carries the section's own header above the
    /// row (title, count, a Refresh button), and this test does not know
    /// that header's exact height in points the way the direct-`DeviceAppTile`
    /// tests above know their whole window is the row. Sweeping the bottom
    /// half of the window reaches the Add button regardless, without
    /// depending on a number nobody wrote down — the header is the *first*
    /// thing in the section's `VStack` and the row is the *last*, so the
    /// bottom half is the row's for any realistic header height. It
    /// deliberately stays clear of the header's own Refresh button, which
    /// starts a real subprocess load on each click. The x coordinate stays
    /// fixed 16pt in from the trailing edge, as the direct-`DeviceAppTile`
    /// tests do — safe here because the fixture is decrypted, so Add is the
    /// row's only trailing control and there is no Dump beyond it to land on
    /// instead. Measured directly, both against this decrypted shape and an
    /// encrypted one where Add sits inboard of Dump: the fixed x only lands
    /// on Add for the decrypted case, which is why it is the one this test uses.
    @MainActor
    func test_the_section_forwards_add_with_the_app_and_record_that_triggered_it() async throws {
        let device = try Self.deviceRecord()
        // Decrypted: `.addable` then draws Add alone, with no lock badge and
        // no Dump button ahead of it to complicate where the sweep lands.
        let deviceApps = await seededDeviceAppsStore(bundleId: "com.a.one", name: "Example", encrypted: false)
        XCTAssertEqual(
            deviceApps.apps[Self.sectionUdid]?.count, 1,
            "premise: the fixture app never reached the store, so this test would "
            + "measure an empty section and prove nothing"
        )

        let record = try stagedIPARecord(bundleId: "com.a.one")
        var captured: (bundleId: String, filename: String)?
        let view = InstalledAppsSection(
            device: device, deviceApps: deviceApps,
            staged: ["com.a.one": record],
            onAdd: { app, record in captured = (app.bundleId, record.filename) }
        )
        let box = hosted(view, width: 500)
        for y in stride(from: CGFloat(4), through: box.size.height / 2, by: 4) {
            click(at: NSPoint(x: box.size.width - 16, y: y), in: box.window)
        }

        XCTAssertEqual(captured?.bundleId, "com.a.one", "the app the section's row was actually about")
        XCTAssertEqual(captured?.filename, record.filename, "the record staged for that same app")
    }
}
