import SwiftUI
import XCTest
@testable import Graft

/// The decrypt card's own "Add as app" offer — Task 10 of the dump-to-app
/// plan, and the reason it exists: a row's Dump (`InstalledAppsSection`,
/// pinned by `InstalledAppsSectionTests`) needs a ~9s `--device-apps` listing
/// before there is a row to add from at all. This card's one-click button
/// needs no listing, so it is the route where "Add as app" would otherwise be
/// unreachable without ever pressing "List apps" — the card has to offer its
/// own Add rather than lean on a row that may not exist yet.
///
/// What is pinned below, as three separate propositions:
///
///  1. A dumped bundle id that is staged and not already configured offers
///     Add, and clicking it hands `onAdd` that row's own `StagedIPARecord`
///     — specifically *its* record, keyed by its own bundle id, not merely
///     the first thing sitting in the cache.
///  2. A dumped bundle id already claimed by a config key does not offer it.
///     Same map (`DeviceDetailView.configuredByBundleId`, shared with
///     `InstalledAppsSection.rowState`) and, as of this file's own guard
///     rewrite, the identical predicate too — see
///     `test_a_dumped_bundle_id_already_configured_does_not_offer_add`'s own
///     doc, and `DeviceDecryptSection.swift`'s own comment on the guard, for
///     how the two used to disagree on a shape neither can actually
///     construct.
///  3. A dumped bundle id the cache has not staged yet — reachable before
///     `staged.load()` following this dump has landed — does not offer it.
///
/// (2) and (3) are measured by height, not by clicking. A click-based
/// absence check would need to land *exactly* nowhere on the card to prove a
/// negative, which is the coordinate a small layout drift is likeliest to
/// get wrong in exactly the direction that makes the test lie — passing
/// because the click missed, not because the button was never drawn. Height
/// measures the property itself. It is also the safer technique on this
/// specific card, and not on the strength of a borrowed finding: this card's
/// `decryptOptions` carries a real AppKit `Picker`, and
/// `test_a_dumped_staged_unconfigured_bundle_id_offers_add_and_forwards_its_own_record`'s
/// own doc records a synthetic click sweep hanging for real against it —
/// menu tracking a synthetic `mouseUp` never reaches. `DeviceDetailAddWiringTests`
/// records an earlier, separate hang of its own against a full-pane render,
/// but that file is explicit that it never diagnosed a cause ("What it does
/// not prove is why") and its own guess at a mechanism — a real AppKit sheet
/// under synthetic events — is a *different* one than the Picker found here.
/// The two are cited as two data points that clicking a full render is
/// risky, not as one shared, diagnosed mechanism.
///
/// **What stays unpinned, and why no test here tries to close it:** the
/// literal closures `DeviceDetailView.body` builds for this card's `staged:`,
/// `configuredByBundleId:` and `onAdd:` — including that the frontmost route
/// deliberately hands `Prefill` a nil `installedVersion` — are not exercised
/// by anything below. `DeviceDecryptSection` has exactly one production
/// caller and no test file renders it embedded inside `DeviceDetailView`
/// while also driving a click into it; `DeviceDetailAddWiringTests` records
/// the same gap for the row route and the same reason (a full-pane click
/// sweep is the thing that hung). What closes it instead is reading
/// `DeviceDetailView.swift`'s `DeviceDecryptSection(...)` call site directly
/// against this file — the same discipline that file's own doc names.
@MainActor
final class DeviceDecryptSectionTests: XCTestCase {

    private static let udid = "00008030-0000000000000003"
    private static let bundleId = "com.a.one"

    private static func device() throws -> DeviceRecord {
        let json = #"{"udid":"\#(udid)","name":"Test iPhone","model":"iPhone14,2","ios_version":"17.0","connection_type":"USB","is_default":false}"#
        return try JSONDecoder().decode(DeviceRecord.self, from: Data(json.utf8))
    }

    /// A `DeviceAppsStore` carrying a real, completed frontmost dump of
    /// `bundleId` — the only way to populate `lastDecrypt`, which is
    /// `private(set)`. Same idiom `DeviceAppsStoreTests` and
    /// `DeviceDetailLayoutTests.deviceAppsStore` both use: a stubbed
    /// `/bin/sh -c "printf …"` standing in for the CLI, decoded by the real
    /// event decoder.
    private func dumpedStore() async throws -> DeviceAppsStore {
        let store = DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        let end = #"{"type":"decrypt-end","bundle_id":"\#(Self.bundleId)","version":"1.0","path":"/c/f.ipa","binaries":3}"#
        _ = await store.decryptFrontmost(argsOverride: ["-c", "printf '\(end)\\n'"])
        XCTAssertEqual(
            store.lastDecrypt?.bundleId, Self.bundleId,
            "premise: the fixture dump never landed on the store, so every case below "
            + "would measure a card with no success line on it and prove nothing"
        )
        return store
    }

    // MARK: - Rendering harness
    //
    // `click(at:in:)` and `hosted(_:width:)` are shared from
    // ClickHarness.swift. This file used to carry its own fourth copy of
    // both, deliberately, on the reasoning that extracting one then would
    // have meant editing three other files this task was not asked to touch
    // — the predicted cost paid off exactly once, and not worth paying
    // again on the next file that needed it too.

    /// The card's own fitting height for a given cache/config pair, against
    /// an already-dumped store. `onAdd` is a no-op: every case that reaches
    /// this helper is about whether the row *exists*, and (1) below is what
    /// proves the row it draws is actually wired.
    private func cardHeight(
        deviceApps: DeviceAppsStore, staged: [String: StagedIPARecord],
        configured: [String: [String]]
    ) throws -> CGFloat {
        let section = DeviceDecryptSection(
            device: try Self.device(), deviceApps: deviceApps,
            staged: staged, configuredByBundleId: configured, onAdd: { _ in }
        )
        return hosted(section, width: 500).size.height
    }

    // MARK: - 1. Offered, and wired

    /// The one case that actually clicks. The sweep targets a small band
    /// near `y = 20` in `click`'s own coordinate space — which is *not* the
    /// card's visual bottom despite the small number.
    ///
    /// **`NSEvent.mouseEvent(location:)` is in the window's base coordinate
    /// system, which is unflipped (origin bottom-left) — `NSHostingView`'s
    /// own view coordinate space is flipped (origin top-left), and those are
    /// two different things.** This was measured, not assumed, after a first
    /// version of this test clicked a symmetric top/bottom probe (`y = 8` and
    /// `y = size.height - 8`) and missed both: walking the hosted `NSView`
    /// tree (the technique `DeviceDetailLayoutTests.worstOutOfBounds` uses)
    /// found this button's `_FocusRingView` at flipped-space frame
    /// `(12, 205, 99, 20)` in a 237pt-tall card — visually the lowest control
    /// on it, exactly as expected — which converts to window-base
    /// `y ∈ [12, 32]`, not `[205, 225]`. Clicking the *unconverted* flipped
    /// number is what a mutant introducing this exact bug would also produce:
    /// a click that lands nowhere and a test that reports "onAdd never
    /// fired" for the right button for the wrong reason.
    ///
    /// The same walk found `decryptOptions`' `Picker` — a real AppKit
    /// pop-up button — at window-base `y ∈ [119, 143]`, over 80pt from this
    /// band. That distance is why this test clicks a small, targeted region
    /// instead of the wide sweep tried first: that sweep hung for real,
    /// several minutes at near-zero CPU, landing on the Picker and opening a
    /// real menu-tracking session nothing here dismisses. Diagnosed with
    /// `sample` on the hung process — a cause `DeviceDetailAddWiringTests`'
    /// own earlier, separate hang against a full pane never established;
    /// that file says so itself ("What it does not prove is why"), and its
    /// own guess at a mechanism is a different one than this. The absence
    /// cases below route around the risk entirely rather than trying to aim
    /// more carefully around it.
    ///
    /// **A second, distinct hang was found the same way, while mutation-testing
    /// this test rather than while first writing it.** Deleting the whole `if
    /// let record = …` block (to confirm this test fails without a button to
    /// click) shrinks the card, which slides the success `Text` — the CLI's
    /// sentence just above the button, `.textSelection(.enabled)` — down into
    /// this sweep's band. A `sample` of the hung process during that run showed
    /// the real stack: `NSTextField.mouseDown:` on a `SelectionTextField`
    /// enters its own nested `NSTextView` tracking loop waiting on
    /// `nextEventMatchingMask:`, and the synthetic `mouseUp` this file's
    /// `click` sends next is queued *after* the still-blocked `mouseDown`
    /// call returns — which it never does. Any text carrying
    /// `.textSelection(.enabled)` is a click hazard by the same mechanism the
    /// Picker is, not only menus — worth knowing before widening this sweep or
    /// reusing the technique against another card that mixes clickable
    /// controls with selectable text. The mutation that found it is reverted;
    /// what stays is a band narrow enough (`y ∈ [10, 34]`, the button's own
    /// `[12, 32]` plus 2pt) to clear both hazards **against the shipping
    /// layout**. That qualifier is load-bearing, not throat-clearing: it is
    /// specifically the button's *absence* that slides the success text into
    /// this band, so anyone who deletes the button outright to re-check this
    /// test (the same mutation described above) should expect this run to
    /// hang rather than fail red — not a regression in the test, but the
    /// second hazard recurring exactly as diagnosed.
    func test_a_dumped_staged_unconfigured_bundle_id_offers_add_and_forwards_its_own_record() async throws {
        let record = try stagedIPARecord(
            bundleId: Self.bundleId, displayName: "BookPlayer", filename: "com.a.one_1.0.ipa"
        )
        let store = try await dumpedStore()
        var captured: StagedIPARecord?
        let section = DeviceDecryptSection(
            device: try Self.device(), deviceApps: store,
            staged: [Self.bundleId: record], configuredByBundleId: [:],
            onAdd: { captured = $0 }
        )
        let box = hosted(section, width: 500)

        for y in stride(from: CGFloat(10), through: 34, by: 4) {
            for x in stride(from: CGFloat(14), through: 105, by: 10) {
                click(at: NSPoint(x: x, y: y), in: box.window)
            }
        }

        let result = try XCTUnwrap(
            captured,
            "the click never reached onAdd — either the button did not render, or it "
            + "no longer sits in the window-base y ∈ [12, 32] band this sweep covers"
        )
        XCTAssertEqual(
            result.filename, record.filename,
            "the record this card's own dump just staged, not some other bundle id's"
        )
    }

    // MARK: - 2. Already configured: not offered a second time

    /// The minimum height difference a suppressed case must show against a
    /// shown one — the button's own row, not a rounding artifact.
    ///
    /// The measured delta between a card with the button and one without is
    /// 30pt (its own ~20pt plus the outer `VStack`'s 10pt spacing).
    /// `XCTAssertLessThan` alone accepts a 0.5pt gap just as happily as a
    /// real one, which is a floor a near-no-op mutation could slip under —
    /// this pins the *size* of what disappears, not merely that something,
    /// anything, got shorter.
    private static let suppressedRowMinimumDelta: CGFloat = 20

    /// The guard that stops an app already added from being offered again.
    /// Shares `DeviceDetailView`'s `configuredByBundleId` map with
    /// `InstalledAppsSection.rowState`, and now the same predicate too:
    /// `rowState` requires `names.first` to succeed, so a config key present
    /// with an empty name list reads as *not* configured, and this card's
    /// guard — `(configuredByBundleId[last.bundleId] ?? []).isEmpty` —
    /// reads that same shape the same way. See `DeviceDecryptSection.swift`'s
    /// own comment on the guard for why an earlier `== nil` spelling did not,
    /// and only agreed with `rowState` because `Dictionary(grouping:by:)`
    /// cannot actually build a present-but-empty array.
    func test_a_dumped_bundle_id_already_configured_does_not_offer_add() async throws {
        let store = try await dumpedStore()
        let record = try stagedIPARecord(bundleId: Self.bundleId)

        let shown = try cardHeight(deviceApps: store, staged: [Self.bundleId: record], configured: [:])
        let suppressed = try cardHeight(
            deviceApps: store, staged: [Self.bundleId: record],
            configured: [Self.bundleId: ["some_existing_app"]]
        )

        XCTAssertLessThan(
            suppressed, shown,
            "a bundle id a config key already claims must draw a shorter card than one "
            + "offering Add — a stray Add here would let the same dump be added twice"
        )
        XCTAssertGreaterThanOrEqual(
            shown - suppressed, Self.suppressedRowMinimumDelta,
            "the gap (\(shown - suppressed)pt) is too small to be the button's own row "
            + "disappearing — expected roughly 30pt, not a rounding difference"
        )
    }

    /// The shape `Dictionary(grouping:by:)` can never actually build for a
    /// present key, and so the one the two guards used to disagree about
    /// silently: a config key present with *no* names at all. This pins the
    /// guard's current shape against it directly rather than trusting that
    /// invariant to keep the two aligned forever — a present-but-empty array
    /// must read exactly like an absent key, matching
    /// `InstalledAppsSection.rowState`'s own `names.first` test, not like a
    /// real claim on the bundle id.
    func test_a_present_but_empty_configured_entry_still_offers_add() async throws {
        let store = try await dumpedStore()
        let record = try stagedIPARecord(bundleId: Self.bundleId)

        let shown = try cardHeight(deviceApps: store, staged: [Self.bundleId: record], configured: [:])
        let withEmptyEntry = try cardHeight(
            deviceApps: store, staged: [Self.bundleId: record], configured: [Self.bundleId: []]
        )

        XCTAssertEqual(
            withEmptyEntry, shown,
            "a config key present with an empty name list must not suppress Add — "
            + "it must read exactly like no entry at all"
        )
    }

    // MARK: - 3. Not yet staged: not offered before there is anything to add

    /// Reachable in practice for a moment after a fresh dump: `DeviceDetailView`
    /// reloads `staged.load()` on `deviceApps.isDumping`'s true→false edge, but
    /// that reload is asynchronous and this card can redraw before it lands —
    /// `deviceApps.lastDecrypt` updates the instant the dump's own `await`
    /// returns, ahead of the separate `Task` the reload runs in.
    ///
    /// The suppressed case stages *a different bundle id*, not an empty
    /// dictionary — `["com.other.app": otherRecord]`, not `[:]`. That is not
    /// cosmetic: an empty cache is what `staged: [:]` used to fixture here,
    /// and against it, `staged[last.bundleId]` and a mutant reading
    /// `staged.values.first` are indistinguishable — both find nothing, so
    /// both suppress the button, and the test passed whether the production
    /// code read the right key or no key at all. Confirmed by actually
    /// making that change and rerunning the file: with the lookup widened to
    /// `.values.first`, this test failed on both assertions as expected.
    /// (1) did **not** fail alongside it, which is worth recording rather
    /// than glossing over — its own `staged` dictionary also holds exactly
    /// one entry, so `.values.first` is indistinguishable from the correct
    /// lookup there too, for the identical reason this test's old `[:]`
    /// fixture was. Catching this mutation is this test's job alone, not a
    /// side effect (1) happens to share. Reverted after confirming. A cache
    /// holding some *other* app's record while this bundle id has none is
    /// also the more realistic shape: `StagedIPAStore` is host-wide, so a
    /// cache with entries for other apps but not yet this one is the
    /// ordinary state moments after a fresh dump, not an edge case invented
    /// for the test.
    func test_a_dumped_bundle_id_missing_from_the_cache_does_not_offer_add() async throws {
        let store = try await dumpedStore()
        let record = try stagedIPARecord(bundleId: Self.bundleId)
        let otherRecord = try stagedIPARecord(
            bundleId: "com.other.app", displayName: "Other App", filename: "com.other.app_1.0.ipa"
        )

        let shown = try cardHeight(deviceApps: store, staged: [Self.bundleId: record], configured: [:])
        let suppressed = try cardHeight(
            deviceApps: store, staged: ["com.other.app": otherRecord], configured: [:]
        )

        XCTAssertLessThan(
            suppressed, shown,
            "a bundle id the cache has not staged yet must draw a shorter card than one "
            + "offering Add for a record it does not have"
        )
        XCTAssertGreaterThanOrEqual(
            shown - suppressed, Self.suppressedRowMinimumDelta,
            "the gap (\(shown - suppressed)pt) is too small to be the button's own row "
            + "disappearing — expected roughly 30pt, not a rounding difference"
        )
    }
}
