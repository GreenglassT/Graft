import XCTest
@testable import Graft

/// The four App Store events, decoded from the exact lines the CLI prints.
///
/// Every line here is the shape `graft_ipa/appstore.py` emits, and the
/// Python suite pins that side. What matters most on this side is what a
/// `try?` decoder does with a field that is legitimately absent or null: a
/// record that refused to decode would be dropped without a word, and the pane
/// it feeds would say nothing at all.
final class AppStoreRecordsTests: XCTestCase {
    func test_status_decodes_both_tools_and_the_setup_commands() throws {
        let line = #"{"type":"appstore-status","ipatool":{"path":"/u/.local/bin/ipatool","present":true,"version":"2.8.0","signed_in":false,"warning":null},"unfairplay":{"path":"/u/up","present":true,"version":"dev-d75","enrolled":true,"model_dir":"/u/models"},"setup_commands":["/u/.local/bin/ipatool auth login --email YOUR_APPLE_ID_EMAIL"]}"#
        guard case let .appStoreStatus(status) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .appStoreStatus")
        }
        XCTAssertEqual(status.ipatool.path, "/u/.local/bin/ipatool")
        XCTAssertTrue(status.ipatool.present)
        XCTAssertEqual(status.ipatool.version, "2.8.0")
        XCTAssertEqual(status.ipatool.signedIn, false)
        XCTAssertNil(status.ipatool.warning)
        XCTAssertEqual(status.unfairplay.version, "dev-d75")
        XCTAssertTrue(status.unfairplay.enrolled)
        XCTAssertEqual(status.unfairplay.modelDir, "/u/models")
        XCTAssertEqual(status.setupCommands.count, 1)
    }

    func test_status_with_nothing_installed_still_decodes() throws {
        // Both versions null, both tools absent: the state a fresh Mac is in,
        // and exactly the one the Settings rows exist to explain.
        let line = #"{"type":"appstore-status","ipatool":{"path":"/x","present":false,"version":null,"signed_in":false,"warning":null},"unfairplay":{"path":"/y","present":false,"version":null,"enrolled":false,"model_dir":"/m"},"setup_commands":["install ipatool","build unfairplay"]}"#
        guard case let .appStoreStatus(status) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .appStoreStatus")
        }
        XCTAssertNil(status.ipatool.version)
        XCTAssertFalse(status.unfairplay.present)
    }

    /// The CLI's "couldn't tell" — `auth info` hung or printed no verdict. It
    /// must decode as nil, not fail the whole record (a `Bool` field would
    /// throw on null and the `try?` decoder would drop the Settings pane's
    /// only status line) and not read as signed out.
    func test_an_unknown_sign_in_decodes_as_nil() throws {
        let line = ##"{"type":"appstore-status","ipatool":{"path":"/x","present":true,"version":"2.8.0","signed_in":null,"warning":null},"unfairplay":{"path":"/y","present":true,"version":"v","enrolled":true,"model_dir":"/m"},"setup_commands":["# run '/x auth info' in Terminal to see why ipatool couldn't say whether it is signed in"]}"##
        guard case let .appStoreStatus(status) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .appStoreStatus")
        }
        XCTAssertNil(status.ipatool.signedIn)
        XCTAssertEqual(status.setupCommands.count, 1)
    }

    func test_the_2_8_1_warning_comes_through() throws {
        let line = #"{"type":"appstore-status","ipatool":{"path":"/x","present":true,"version":"2.8.1","signed_in":true,"warning":"ipatool 2.8.1 can't download"},"unfairplay":{"path":"/y","present":true,"version":"v","enrolled":true,"model_dir":"/m"},"setup_commands":[]}"#
        guard case let .appStoreStatus(status) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .appStoreStatus")
        }
        XCTAssertEqual(status.ipatool.warning, "ipatool 2.8.1 can't download")
    }

    func test_version_decodes_a_comparison() throws {
        let line = #"{"type":"appstore-version","app":"example","bundle_id":"com.example.app","store_version":"21.46.3","cached_version":"21.40.0","update_available":true,"error":null}"#
        guard case let .appStoreVersion(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .appStoreVersion")
        }
        XCTAssertEqual(record, AppStoreVersionRecord(
            app: "example", bundleId: "com.example.app",
            storeVersion: "21.46.3", cachedVersion: "21.40.0",
            updateAvailable: true, error: nil
        ))
    }

    func test_a_failed_lookup_decodes_rather_than_vanishing() throws {
        // `store_version: null` with the reason in `error` — the CLI's way of
        // saying "couldn't tell", which must not decode the same as no event.
        let line = #"{"type":"appstore-version","app":"x","bundle_id":"com.x","store_version":null,"cached_version":null,"update_available":false,"error":"iTunes lookup request failed"}"#
        guard case let .appStoreVersion(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .appStoreVersion")
        }
        XCTAssertNil(record.storeVersion)
        XCTAssertEqual(record.error, "iTunes lookup request failed")
    }

    func test_each_stage_decodes_to_its_case() throws {
        for (raw, expected) in [
            ("download", AppStoreStage.download),
            ("decrypt", .decrypt),
            ("stage", .stage),
        ] {
            let line = #"{"type":"appstore-stage","app":"example","stage":"\#(raw)"}"#
            guard case let .appStoreStage(record) = try BuildEvent.decode(line: line) else {
                return XCTFail("expected .appStoreStage for \(raw)")
            }
            XCTAssertEqual(record, AppStoreStageRecord(app: "example", stage: expected))
        }
    }

    func test_a_stage_this_app_has_never_heard_of_is_kept_not_dropped() throws {
        // A new stage on the CLI side should still read as *something*
        // happening, not as a line the decoder threw away.
        let line = #"{"type":"appstore-stage","app":"example","stage":"verify"}"#
        guard case let .appStoreStage(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .appStoreStage")
        }
        XCTAssertEqual(record.stage, .other("verify"))
    }

    func test_decrypted_decodes_the_verification() throws {
        let line = #"{"type":"appstore-decrypted","app":"example","encrypted_images":8,"decrypted_pages":51088,"plaintext_verified":true}"#
        guard case let .appStoreDecrypted(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .appStoreDecrypted")
        }
        XCTAssertEqual(record, AppStoreDecryptedRecord(
            app: "example", encryptedImages: 8, decryptedPages: 51088, plaintextVerified: true
        ))
    }
}
