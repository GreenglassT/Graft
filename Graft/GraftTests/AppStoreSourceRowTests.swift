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
}
