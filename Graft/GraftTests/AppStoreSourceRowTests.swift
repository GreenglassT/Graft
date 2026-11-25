import AppKit
import SwiftUI
import XCTest
@testable import Graft

/// The Source IPA section's App Store line, as text.
///
/// Four answers the line has to keep apart: an update to get, nothing to get,
/// nothing cached yet, and "couldn't tell" — the last one being the case that
/// must never render as "up to date".
final class AppStoreSourceRowTests: XCTestCase {
    private func record(
        store: String?, cached: String?, update: Bool, error: String? = nil
    ) -> AppStoreVersionRecord {
        AppStoreVersionRecord(
            app: "example", bundleId: "com.example.app",
            storeVersion: store, cachedVersion: cached,
            updateAvailable: update, error: error
        )
    }

    func test_not_asked_yet() {
        XCTAssertEqual(AppStoreSourceRow.versionLine(nil), "Checking the App Store…")
    }

    func test_an_update() {
        XCTAssertEqual(
            AppStoreSourceRow.versionLine(record(store: "21.46.3", cached: "21.40.0", update: true)),
            "App Store 21.46.3 · cached 21.40.0 — update available"
        )
    }

    func test_up_to_date() {
        XCTAssertEqual(
            AppStoreSourceRow.versionLine(record(store: "21.46.3", cached: "21.46.3", update: false)),
            "App Store 21.46.3 · cached 21.46.3 — up to date"
        )
    }

    func test_nothing_cached() {
        XCTAssertEqual(
            AppStoreSourceRow.versionLine(record(store: "21.46.3", cached: nil, update: true)),
            "App Store 21.46.3 · nothing cached"
        )
    }

    func test_couldnt_tell_is_not_up_to_date() {
        XCTAssertEqual(
            AppStoreSourceRow.versionLine(
                record(store: nil, cached: "21.40.0", update: false, error: "offline")
            ),
            "Couldn't check the App Store: offline"
        )
    }

    func test_the_stage_labels() {
        XCTAssertEqual(AppStoreStage.download.label, "Downloading…")
        XCTAssertEqual(AppStoreStage.decrypt.label, "Decrypting…")
        XCTAssertEqual(AppStoreStage.stage.label, "Staging…")
        XCTAssertEqual(AppStoreStage.other("verify").label, "Working…")
    }

    func test_while_getting_before_the_first_stage_it_says_starting() {
        XCTAssertEqual(AppStoreSourceRow.progressLabel(nil), "Starting…")
        XCTAssertEqual(AppStoreSourceRow.progressLabel(.decrypt), "Decrypting…")
    }

    func test_the_rows_button_follows_the_licence() {
        XCTAssertEqual(AppStoreSourceRow.primaryAction(license: nil), .getLatest)
        XCTAssertEqual(AppStoreSourceRow.primaryAction(
            license: .init(price: 0, formattedPrice: "Free", trackId: 1)), .claimFree)
        XCTAssertEqual(AppStoreSourceRow.primaryAction(
            license: .init(price: 2.99, formattedPrice: "$2.99", trackId: 1)), .openInStore)
        XCTAssertEqual(AppStoreSourceRow.primaryAction(
            license: .init(price: nil, formattedPrice: nil, trackId: nil)), .openInStore)
    }

    /// Get Again only where the line says up to date with the very version
    /// Get latest would fetch. "Couldn't tell" is never "again".
    func test_get_again_is_offered_only_for_the_version_already_staged() {
        let same = record(store: "21.46.3", cached: "21.46.3", update: false)
        XCTAssertEqual(AppStoreSourceRow.primaryAction(license: nil, version: same), .getAgain)
        XCTAssertEqual(AppStoreSourceRow.primaryAction(
            license: nil, version: record(store: "21.46.3", cached: "21.40.0", update: true)), .getLatest)
        XCTAssertEqual(AppStoreSourceRow.primaryAction(
            license: nil, version: record(store: "21.46.3", cached: nil, update: true)), .getLatest)
        XCTAssertEqual(AppStoreSourceRow.primaryAction(
            license: nil, version: record(store: "21.40.0", cached: "21.46.3", update: false)),
            .getLatest, "a newer copy staged: the get fetches another version, not this one again")
        XCTAssertEqual(AppStoreSourceRow.primaryAction(
            license: nil, version: record(store: nil, cached: "21.46.3", update: false, error: "offline")),
            .getLatest)
        XCTAssertEqual(AppStoreSourceRow.primaryAction(license: nil, version: nil), .getLatest)

        // A license refusal still decides.
        XCTAssertEqual(AppStoreSourceRow.primaryAction(
            license: .init(price: 0, formattedPrice: "Free", trackId: 1), version: same), .claimFree)
        XCTAssertEqual(AppStoreSourceRow.primaryAction(
            license: .init(price: 2.99, formattedPrice: "$2.99", trackId: 1), version: same), .openInStore)
    }

    // MARK: Get (free) asks first (fix-check #11), naming whom a fresh read found (get-4)

    /// What the confirmation's own Get (free) was given, in place of the claim.
    @MainActor
    private final class Claims {
        var given: [AppStoreStore.ClaimConfirmation] = []
    }

    /// The row as `AppDetailView` wires it, over a store whose stand-in CLI
    /// says `someone@example.invalid` is signed in and refuses the get for
    /// want of a license, at price 0. The claim itself is only recorded.
    @MainActor
    private struct Wired: View {
        @ObservedObject var store: AppStoreStore
        let claims: Claims

        var body: some View {
            AppStoreSourceRow(
                version: nil, isGetting: false, stage: nil, isBlocked: false,
                onGet: {}, onStop: {}, onRefresh: {},
                license: store.licenseNeeded["example"],
                isClaiming: store.isClaiming("example"),
                isCheckingClaim: store.checkingClaim.contains("example"),
                claimConfirmation: store.claimConfirmations["example"],
                onCheckClaim: { Task { await store.checkBeforeClaim(app: "example") } },
                onClaim: { confirmation in
                    claims.given.append(confirmation)
                    store.cancelClaim(app: "example")
                },
                onCancelClaim: { store.cancelClaim(app: "example") },
                appName: "Example"
            )
        }
    }

    @MainActor
    private func offeredStore() async throws -> AppStoreStore {
        let dir = FileManager.default.temporaryDirectory.appending(path: "row_claim_\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let file = dir.appending(path: "graft.sh")
        try """
        #!/bin/sh
        case "$*" in
          *--appstore-status*)
            printf '%s\\n' '{"type":"appstore-status","ipatool":{"path":"/i","present":true,"version":"2.8.0","signed_in":true,"warning":null,"email":"someone@example.invalid","legacy_signin":false,"source":"bundled"},"unfairplay":{"path":"/u","present":true,"version":"v0.4.0","enrolled":true,"model_dir":"/m"},"setup_commands":[]}' ;;
          *--appstore-get*)
            printf '%s\\n' '{"type":"appstore-refused","app":"example","reason":"needs-license","price":0,"formatted_price":"Free","track_id":100000001}'
            exit 20 ;;
        esac
        """.write(to: file, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: file.path)
        let store = AppStoreStore(runner: CLIRunner(executable: file), shell: NoAdminShell())
        store.startGet(app: "example")
        await eventually { !store.isBusy("example") }
        XCTAssertEqual(store.licenseNeeded["example"]?.offersClaim, true, "premise: Get (free) is on offer")
        XCTAssertNil(store.status, "premise: no status read yet, so nothing cached names anyone")
        return store
    }

    /// The confirmation `.alert` puts over the window, once it is there.
    @MainActor
    private func confirmation(over window: NSWindow) -> NSWindow? {
        let deadline = Date().addingTimeInterval(3)
        while window.attachedSheet == nil, Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.02))
        }
        return window.attachedSheet
    }

    @MainActor
    private func views(in view: NSView?) -> [NSView] {
        guard let view else { return [] }
        return [view] + view.subviews.flatMap { views(in: $0) }
    }

    @MainActor
    private func button(_ title: String, in sheet: NSWindow) -> NSButton? {
        views(in: sheet.contentView).compactMap { $0 as? NSButton }.first { $0.title == title }
    }

    @MainActor
    private func waitUntilGone(_ sheet: NSWindow, from window: NSWindow) {
        let deadline = Date().addingTimeInterval(3)
        while window.attachedSheet === sheet, Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.02))
        }
    }

    /// One click on Get (free) is a permanent entry in an Apple ID's purchase
    /// history, so the click only asks. It reads status first, and the
    /// question names the Apple ID that read found (whole-diff review,
    /// get-4). Nothing claims until the alert's own Get (free) is pressed, and
    /// Cancel claims nothing. Clicked through the window's own event pipeline,
    /// as a user's click would be.
    @MainActor
    func test_get_free_reads_status_then_asks_naming_that_apple_id_and_claims_only_once_confirmed() async throws {
        try AccessibilityTree.requireServed()
        let store = try await offeredStore()
        let claims = Claims()
        let box = hosted(Wired(store: store, claims: claims), width: 560)
        settle(box)

        try clickButton(named: "Get (free)", in: box.window)
        // The read runs on the main actor, which the run loop pumping below
        // doesn't give a turn to: wait for it here.
        await eventually { store.claimConfirmations["example"] != nil }
        let asked = confirmation(over: box.window)
        XCTAssertEqual(claims.given, [], "one click only asks")
        let sheet = try XCTUnwrap(asked, "Get (free) shows the confirmation")
        let texts = views(in: sheet.contentView).compactMap { ($0 as? NSTextField)?.stringValue }
        let words = AppStoreStore.claimConfirmation(app: "Example", email: "someone@example.invalid")
        XCTAssertTrue(texts.contains(words.title), "\(texts)")
        XCTAssertTrue(texts.contains(words.message), "the Apple ID the read found: \(texts)")
        XCTAssertEqual(store.status?.ipatool.email, "someone@example.invalid", "status was read")

        try XCTUnwrap(button("Cancel", in: sheet)).performClick(nil)
        waitUntilGone(sheet, from: box.window)
        settle(box)
        XCTAssertEqual(claims.given, [], "Cancel claims nothing")
        XCTAssertNil(box.window.attachedSheet)
        XCTAssertNil(store.claimConfirmations["example"])

        try clickButton(named: "Get (free)", in: box.window)
        await eventually { store.claimConfirmations["example"] != nil }
        let again = try XCTUnwrap(confirmation(over: box.window), "asked again")
        XCTAssertEqual(claims.given, [])
        try XCTUnwrap(button("Get (free)", in: again), "the alert's own Get (free)").performClick(nil)
        waitUntilGone(again, from: box.window)
        settle(box)
        XCTAssertEqual(claims.given, [.init(email: "someone@example.invalid")],
                       "confirmed: the claim runs, once, for the Apple ID it named")
    }

    // MARK: Get Again asks first

    @MainActor
    private final class Gets {
        var latest = 0
        var again = 0
    }

    /// Get Again downloads the whole app again, which is what the owner's
    /// hotspot paid for once already, so one click only asks. Nothing is got
    /// until the alert's own Get Again, and Cancel gets nothing.
    @MainActor
    func test_get_again_asks_first_and_gets_again_only_once_confirmed() async throws {
        try AccessibilityTree.requireServed()
        let gets = Gets()
        let row = AppStoreSourceRow(
            version: record(store: "21.46.3", cached: "21.46.3", update: false),
            isGetting: false, stage: nil, isBlocked: false,
            onGet: { gets.latest += 1 }, onStop: {}, onRefresh: {},
            onGetAgain: { gets.again += 1 },
            appName: "Example"
        )
        let box = hosted(row, width: 560)
        settle(box)

        try clickButton(named: AppStoreSourceRow.getAgainButton, in: box.window)
        let sheet = try XCTUnwrap(confirmation(over: box.window), "Get Again shows the confirmation")
        let texts = views(in: sheet.contentView).compactMap { ($0 as? NSTextField)?.stringValue }
        let words = AppStoreSourceRow.getAgainConfirmation(app: "Example", version: "21.46.3")
        XCTAssertTrue(texts.contains(words.title), "\(texts)")
        XCTAssertTrue(texts.contains(words.message), "\(texts)")
        XCTAssertEqual(gets.again, 0, "one click only asks")

        try XCTUnwrap(button("Cancel", in: sheet)).performClick(nil)
        waitUntilGone(sheet, from: box.window)
        settle(box)
        XCTAssertEqual(gets.again, 0, "Cancel gets nothing")

        try clickButton(named: AppStoreSourceRow.getAgainButton, in: box.window)
        let again = try XCTUnwrap(confirmation(over: box.window), "asked again")
        try XCTUnwrap(button(AppStoreSourceRow.getAgainButton, in: again)).performClick(nil)
        waitUntilGone(again, from: box.window)
        settle(box)
        XCTAssertEqual(gets.again, 1, "confirmed: Get Again runs, once")
        XCTAssertEqual(gets.latest, 0, "and never the plain get")
    }
}
