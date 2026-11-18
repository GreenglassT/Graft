import XCTest
@testable import Graft

/// The Settings pane's App Store rows, as data.
///
/// The view renders exactly these values, so the decisions worth pinning —
/// what a missing tool, a signed-out ipatool, a broken version and a failed
/// status read each look like — are pinned here without hosting a view.
final class AppStoreSettingsTests: XCTestCase {
    private func status(
        ipatoolPresent: Bool = true,
        version: String? = "2.8.0",
        signedIn: Bool? = true,
        warning: String? = nil,
        unfairplayPresent: Bool = true,
        enrolled: Bool = true,
        commands: [String] = []
    ) -> AppStoreStatusRecord {
        AppStoreStatusRecord(
            ipatool: .init(
                path: "/u/.local/bin/ipatool", present: ipatoolPresent, version: version,
                signedIn: signedIn, warning: warning
            ),
            unfairplay: .init(
                path: "/u/up", present: unfairplayPresent, version: "dev-d75",
                enrolled: enrolled, modelDir: "/u/models"
            ),
            setupCommands: commands
        )
    }

    func test_everything_ready_is_two_green_rows() {
        let rows = AppStoreSettingsSection.rows(for: status())

        XCTAssertEqual(rows, [
            .init(name: "ipatool", ok: true, detail: "2.8.0 · signed in", warning: nil),
            .init(name: "UnfairPlay", ok: true, detail: "dev-d75 · enrolled", warning: nil),
        ])
    }

    func test_signed_out_is_not_ok() {
        let row = AppStoreSettingsSection.rows(for: status(signedIn: false))[0]

        XCTAssertFalse(row.ok)
        XCTAssertEqual(row.detail, "2.8.0 · not signed in")
    }

    func test_an_unanswered_sign_in_check_says_so_rather_than_signed_out() {
        let row = AppStoreSettingsSection.rows(for: status(signedIn: nil))[0]

        XCTAssertFalse(row.ok)
        XCTAssertEqual(row.detail, "2.8.0 · couldn't tell whether it's signed in")
    }

    func test_a_broken_version_is_not_ok_even_when_signed_in() {
        // 2.8.1 signs in fine and then cannot download — green would be a lie.
        let row = AppStoreSettingsSection.rows(
            for: status(version: "2.8.1", warning: "ipatool 2.8.1 can't download")
        )[0]

        XCTAssertFalse(row.ok)
        XCTAssertEqual(row.warning, "ipatool 2.8.1 can't download")
    }

    func test_a_missing_tool_names_where_it_was_looked_for() {
        let rows = AppStoreSettingsSection.rows(
            for: status(ipatoolPresent: false, version: nil, signedIn: false, unfairplayPresent: false)
        )

        XCTAssertEqual(rows[0].detail, "not found at /u/.local/bin/ipatool")
        XCTAssertEqual(rows[1].detail, "not found at /u/up")
        XCTAssertFalse(rows[0].ok)
        XCTAssertFalse(rows[1].ok)
    }

    func test_not_enrolled_is_not_ok() {
        let row = AppStoreSettingsSection.rows(for: status(enrolled: false))[1]

        XCTAssertFalse(row.ok)
        XCTAssertEqual(row.detail, "dev-d75 · not enrolled")
    }

    // MARK: load state

    func test_before_the_first_answer_it_says_checking() {
        XCTAssertEqual(
            AppStoreSettingsSection.state(hasLoaded: false, status: nil, error: nil),
            .checking
        )
    }

    /// The house defect: nil status after a load is the failure, and must not
    /// read as "still checking".
    func test_a_failed_load_says_so_rather_than_checking() {
        XCTAssertEqual(
            AppStoreSettingsSection.state(hasLoaded: true, status: nil, error: "No such option"),
            .failed("No such option")
        )
    }

    func test_a_loaded_status_is_shown() {
        let s = status()
        XCTAssertEqual(
            AppStoreSettingsSection.state(hasLoaded: true, status: s, error: nil),
            .loaded(s)
        )
    }
}
