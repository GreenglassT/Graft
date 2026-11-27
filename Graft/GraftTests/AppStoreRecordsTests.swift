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

    func test_a_held_version_comes_through_and_an_older_cli_reads_as_not_held() throws {
        let held = #"{"type":"appstore-version","app":"example","bundle_id":"com.example.app","store_version":"1.4.0","cached_version":"1.2.0","update_available":false,"held_version":"1.2.0","error":null}"#
        guard case let .appStoreVersion(record) = try BuildEvent.decode(line: held) else {
            return XCTFail("expected .appStoreVersion")
        }
        XCTAssertEqual(record.heldVersion, "1.2.0")
        XCTAssertFalse(record.updateAvailable)

        let older = #"{"type":"appstore-version","app":"x","bundle_id":"com.x","store_version":"1.4.0","cached_version":"1.2.0","update_available":true,"error":null}"#
        guard case let .appStoreVersion(plain) = try BuildEvent.decode(line: older) else {
            return XCTFail("expected .appStoreVersion")
        }
        XCTAssertNil(plain.heldVersion)
    }

    /// `--appstore-versions`' rows, read and not yet read, and its sentinel:
    /// the exact lines `appstore.list_versions` prints.
    func test_a_listed_version_decodes_read_or_not() throws {
        let read = #"{"type":"appstore-release","app":"example","bundle_id":"com.example.app","external_id":"800400","index":0,"newest":true,"version":"1.4.0","release_date":"2026-10-01T07:00:00Z","staged":true,"error":null}"#
        guard case let .appStoreRelease(row) = try BuildEvent.decode(line: read) else {
            return XCTFail("expected .appStoreRelease")
        }
        XCTAssertEqual(row, AppStoreReleaseRecord(
            app: "example", bundleId: "com.example.app", externalId: "800400", index: 0, newest: true,
            version: "1.4.0", releaseDate: "2026-10-01T07:00:00Z", staged: true, error: nil
        ))
        XCTAssertEqual(row.id, "800400")

        let unread = #"{"type":"appstore-release","app":"example","bundle_id":"com.example.app","external_id":"800100","index":3,"newest":false,"version":null,"release_date":null,"staged":false,"error":"Apple didn't describe this version (ipatool: received error: This item is no longer available.)"}"#
        guard case let .appStoreRelease(gone) = try BuildEvent.decode(line: unread) else {
            return XCTFail("expected .appStoreRelease")
        }
        XCTAssertNil(gone.version)
        XCTAssertNil(gone.releaseDate)
        XCTAssertNotNil(gone.error)
    }

    func test_the_listing_sentinel_and_a_hold_decode() throws {
        let end = #"{"type":"appstore-releases-end","app":"example","bundle_id":"com.example.app","count":4,"offset":0,"limit":10,"held_version":null}"#
        guard case let .appStoreReleasesEnd(record) = try BuildEvent.decode(line: end) else {
            return XCTFail("expected .appStoreReleasesEnd")
        }
        XCTAssertEqual(record, AppStoreReleasesEndRecord(
            app: "example", bundleId: "com.example.app", count: 4, offset: 0, limit: 10, heldVersion: nil
        ))

        let held = #"{"type":"appstore-hold","app":"example","bundle_id":"com.example.app","version":"1.2.0","previous":null}"#
        guard case let .appStoreHold(hold) = try BuildEvent.decode(line: held) else {
            return XCTFail("expected .appStoreHold")
        }
        XCTAssertEqual(hold, AppStoreHoldRecord(app: "example", bundleId: "com.example.app", version: "1.2.0", previous: nil))

        let released = #"{"type":"appstore-hold","app":"example","bundle_id":"com.example.app","version":null,"previous":"1.2.0"}"#
        guard case let .appStoreHold(gone) = try BuildEvent.decode(line: released) else {
            return XCTFail("expected .appStoreHold")
        }
        XCTAssertNil(gone.version)
        XCTAssertEqual(gone.previous, "1.2.0")

        let byBuild = #"{"type":"appstore-hold","app":"example","bundle_id":"com.example.app","version":"1.2.0","version_id":"800200","previous":null}"#
        guard case let .appStoreHold(build) = try BuildEvent.decode(line: byBuild) else {
            return XCTFail("expected .appStoreHold")
        }
        XCTAssertEqual(build.versionId, "800200")
    }

    /// A hold by build (spec §6): which build, and which of its version's
    /// builds. An older CLI sends neither.
    func test_a_hold_by_build_comes_through() throws {
        let line = #"{"type":"appstore-version","app":"example","bundle_id":"com.example.app","store_version":"1.4.0","cached_version":"1.2.0","update_available":false,"held_version":"1.2.0","held_version_id":"800200","held_build":{"number":1,"count":2},"error":null}"#
        guard case let .appStoreVersion(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .appStoreVersion")
        }
        XCTAssertEqual(record.heldVersionId, "800200")
        XCTAssertEqual(record.heldBuild, AppStoreVersionRecord.HeldBuild(number: 1, count: 2))

        let unique = #"{"type":"appstore-version","app":"example","bundle_id":"com.example.app","store_version":"1.4.0","cached_version":"1.2.0","update_available":false,"held_version":"1.2.0","held_version_id":"800200","held_build":null,"error":null}"#
        guard case let .appStoreVersion(one) = try BuildEvent.decode(line: unique) else {
            return XCTFail("expected .appStoreVersion")
        }
        XCTAssertNil(one.heldBuild)

        let end = #"{"type":"appstore-releases-end","app":"example","bundle_id":"com.example.app","count":3,"offset":0,"limit":10,"held_version":"1.2.0","held_version_id":"800200"}"#
        guard case let .appStoreReleasesEnd(sentinel) = try BuildEvent.decode(line: end) else {
            return XCTFail("expected .appStoreReleasesEnd")
        }
        XCTAssertEqual(sentinel.heldVersionId, "800200")
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

    func test_already_staged_decodes_the_staged_ipa() throws {
        let line = #"{"type":"appstore-already-staged","app":"example","bundle_id":"com.example.app","version":"21.46.3","filename":"com.example.app_21.46.3.ipa","path":"/c/com.example.app_21.46.3.ipa"}"#
        guard case let .appStoreAlreadyStaged(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .appStoreAlreadyStaged")
        }
        XCTAssertEqual(record, AppStoreAlreadyStagedRecord(
            app: "example", bundleId: "com.example.app", version: "21.46.3",
            filename: "com.example.app_21.46.3.ipa", path: "/c/com.example.app_21.46.3.ipa"
        ))
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

    // MARK: App Store search

    func test_a_search_result_decodes_every_field() throws {
        let line = #"{"type":"appstore-search-result","track_id":100000001,"bundle_id":"com.example.app","name":"Example","developer":"Example Developer","version":"2.1","price":4.99,"formatted_price":"$4.99","currency":"USD","artwork_url":"https://is1.example.invalid/512x512bb.jpg","icon_path":"/c/graft/appstore-search-icons/ab.img","configured_as":"example"}"#
        guard case let .appStoreSearchResult(r) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .appStoreSearchResult")
        }
        XCTAssertEqual(r.trackId, 100000001)
        XCTAssertEqual(r.bundleId, "com.example.app")
        XCTAssertEqual(r.name, "Example")
        XCTAssertEqual(r.developer, "Example Developer")
        XCTAssertEqual(r.version, "2.1")
        XCTAssertEqual(r.price, 4.99)
        XCTAssertEqual(r.formattedPrice, "$4.99")
        XCTAssertEqual(r.iconPath, "/c/graft/appstore-search-icons/ab.img")
        XCTAssertEqual(r.configuredAs, "example")
        XCTAssertEqual(r.acquisition, .paid(price: "$4.99"))
    }

    /// Every optional field null: a result still decodes. A `try?` decoder
    /// would otherwise drop it, and Search would show one result fewer, unexplained.
    func test_a_search_result_with_nothing_optional_still_decodes() throws {
        let line = #"{"type":"appstore-search-result","track_id":100000002,"bundle_id":"com.example.two","name":"Two","developer":"","version":"1.0","price":null,"formatted_price":null,"currency":null,"artwork_url":null,"icon_path":null,"configured_as":null}"#
        guard case let .appStoreSearchResult(r) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .appStoreSearchResult")
        }
        XCTAssertNil(r.price)
        XCTAssertNil(r.iconPath)
        XCTAssertNil(r.configuredAs)
        // No price is "price unknown", never free (the CLI's table agrees).
        XCTAssertEqual(r.acquisition, .unknown)
    }

    func test_a_search_end_or_failure_carries_the_clis_note() throws {
        let end = #"{"type":"appstore-search-end","term":"x","country":"US","count":0,"note":"Searching the US store: your region has no App Store."}"#
        guard case let .appStoreSearchEnd(record) = try BuildEvent.decode(line: end) else {
            return XCTFail("expected .appStoreSearchEnd")
        }
        XCTAssertEqual(record.note, Storefronts.noStoreNote)
        let failed = #"{"type":"appstore-search-failed","term":"x","reason":"rate-limited","note":null}"#
        guard case let .appStoreSearchFailed(failure) = try BuildEvent.decode(line: failed) else {
            return XCTFail("expected .appStoreSearchFailed")
        }
        XCTAssertNil(failure.note)
        XCTAssertTrue(AppStoreSearchRefusal.error(in: [.appStoreSearchFailed(failure)]) is AppStoreSearchRateLimited)
    }

    func test_a_free_result_is_free_and_a_priced_one_without_a_label_still_says_its_price() {
        func result(_ price: Double?, _ formatted: String?) -> AppStoreSearchResult {
            AppStoreSearchResult(
                trackId: 1, bundleId: "com.example.app", name: "Example", developer: "", version: "1",
                price: price, formattedPrice: formatted, currency: nil, artworkUrl: nil,
                iconPath: nil, configuredAs: nil
            )
        }
        XCTAssertEqual(result(0, "Free").acquisition, .free)
        XCTAssertEqual(result(4.99, nil).acquisition, .paid(price: "4.99"))
    }

    func test_the_search_end_carries_the_store_and_the_count() throws {
        let line = #"{"type":"appstore-search-end","term":"example","country":"GB","count":0}"#
        guard case let .appStoreSearchEnd(end) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .appStoreSearchEnd")
        }
        XCTAssertEqual(end, AppStoreSearchEndRecord(term: "example", country: "GB", count: 0))
    }

    func test_only_an_offline_failure_is_the_offline_refusal() throws {
        func failed(_ reason: String) throws -> BuildEvent {
            try BuildEvent.decode(line: #"{"type":"appstore-search-failed","term":"example","reason":""# + reason + #""}"#)
        }
        XCTAssertNotNil(AppStoreSearchOffline.refusal(in: [try failed("offline")]))
        XCTAssertNil(AppStoreSearchOffline.refusal(in: [try failed("http")]))
        XCTAssertNil(AppStoreSearchOffline.refusal(in: [try failed("malformed")]))
        XCTAssertNil(AppStoreSearchOffline.refusal(in: []))
    }

    // MARK: The App Store account (sub-project 4)

    func test_the_account_events_decode() throws {
        guard case let .appStoreLogin(login) = try BuildEvent.decode(
            line: #"{"type":"appstore-login","state":"failed","reason":"wrong-code","message":"Apple didn't accept that code."}"#
        ) else { return XCTFail("expected .appStoreLogin") }
        XCTAssertEqual(login, AppStoreLoginRecord(
            state: "failed", reason: "wrong-code", message: "Apple didn't accept that code."
        ))

        // Whole-diff review, signin-4: the hint that comes with a failure.
        guard case let .appStoreLogin(hinted) = try BuildEvent.decode(
            line: #"{"type":"appstore-login","state":"failed","reason":"keychain","message":"No.","hint":"sign in again and choose Allow"}"#
        ) else { return XCTFail("expected .appStoreLogin") }
        XCTAssertEqual(hinted.hint, "sign in again and choose Allow")
        XCTAssertEqual(
            SignInFailed.refusal(in: [.appStoreLogin(hinted)]),
            SignInFailed(reason: "keychain", message: "No.", hint: "sign in again and choose Allow")
        )
        XCTAssertNil(login.hint, "none sent, none read")

        guard case let .appStoreLogout(logout) = try BuildEvent.decode(
            line: #"{"type":"appstore-logout","signed_out":true}"#
        ) else { return XCTFail("expected .appStoreLogout") }
        XCTAssertTrue(logout.signedOut)

        guard case let .appStoreEnrollCandidate(candidate) = try BuildEvent.decode(
            line: #"{"type":"appstore-enroll-candidate","path":"/Applications/Example.app","name":"Example","bundle_id":"com.example.app","opened":false}"#
        ) else { return XCTFail("expected .appStoreEnrollCandidate") }
        XCTAssertEqual(candidate.bundleId, "com.example.app")
        XCTAssertFalse(candidate.opened)

        guard case let .appStoreEnrollCandidatesEnd(end) = try BuildEvent.decode(
            line: #"{"type":"appstore-enroll-candidates-end","count":0}"#
        ) else { return XCTFail("expected .appStoreEnrollCandidatesEnd") }
        XCTAssertEqual(end.count, 0)

        guard case let .appStoreEnrollCommand(command) = try BuildEvent.decode(
            line: #"{"type":"appstore-enroll-command","app":"/Applications/Example.app","model_dir":"/u/m","script":"set -eu\necho \"$D\"\n","verified":false}"#
        ) else { return XCTFail("expected .appStoreEnrollCommand") }
        XCTAssertEqual(command.script, "set -eu\necho \"$D\"\n")
        XCTAssertEqual(command.modelDir, "/u/m")
        XCTAssertFalse(command.verified)

        guard case let .appStoreEnrolled(enrolled) = try BuildEvent.decode(
            line: #"{"type":"appstore-enrolled","model_dir":"/u/m","count":1}"#
        ) else { return XCTFail("expected .appStoreEnrolled") }
        XCTAssertEqual(enrolled, EnrolledRecord(modelDir: "/u/m", count: 1))

        guard case let .appStoreClaimed(claimed) = try BuildEvent.decode(
            line: #"{"type":"appstore-claimed","app":"example","bundle_id":"com.example.app","already_owned":false}"#
        ) else { return XCTFail("expected .appStoreClaimed") }
        XCTAssertFalse(claimed.alreadyOwned)
    }

    // MARK: The enrollment events, as the CLI writes them
    //
    // Each line below is byte for byte what `JsonEmitter.emit` writes for the
    // verb (compact separators, the fields in `_verb_appstore_enroll_*`'s
    // order), with the script from `enrollment.root_script` for a bundled,
    // team-checked UnfairPlay. The 2.0 fields after `verified` are the
    // review-part-4 contract; the base's lines without them must still decode.

    private static let rootScript = #"set -eu\numask 077\nD=$(/usr/bin/mktemp -d '/private/var/tmp/graft-enroll.XXXXXXXX')\ntrap '/bin/rm -rf \"$D\"' EXIT\n/bin/cp '/Applications/Graft.app/Contents/Helpers/bin/unfairplay' \"$D/unfairplay\"\n/usr/bin/codesign --verify --strict -R '=anchor apple generic and identifier \"unfairplay\" and certificate leaf[subject.OU] = \"ABCDE12345\"' \"$D/unfairplay\"\nA=$(/bin/realpath '/Applications/Example.app')\ncase \"$A\" in '/Applications'/*) ;; *) echo \"graft: the app is not in the Applications folder\" >&2; exit 64 ;; esac\n[ -d \"$A\" ] && [ \"$(/usr/bin/stat -f %u \"$A\")\" = 0 ] || { echo \"graft: the app is not the system's\" >&2; exit 65; }\ncase \"$(/usr/bin/stat -f %Sp \"$A\")\" in ?????w*|????????w*) echo \"graft: others can change the app\" >&2; exit 66 ;; esac\n\"$D/unfairplay\" setup --app \"$A\" --model-dir \"$D/models\" 1>&2\n/bin/rm -f \"$D/unfairplay\"\n/usr/sbin/chown -R -h '501:20' \"$D\"\ntrap - EXIT\necho \"$D\"\n"#

    func test_an_enrol_candidate_decodes_from_the_clis_payload() throws {
        let line = #"{"type":"appstore-enroll-candidate","path":"/Applications/Example.app","name":"Example","bundle_id":"com.example.app","opened":true}"#
        guard case let .appStoreEnrollCandidate(candidate) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .appStoreEnrollCandidate")
        }
        XCTAssertEqual(candidate, EnrollCandidateRecord(
            path: "/Applications/Example.app", name: "Example", bundleId: "com.example.app", opened: true
        ))
        XCTAssertEqual(candidate.id, "/Applications/Example.app")
    }

    func test_the_base_enrol_command_decodes_without_the_2_0_fields() throws {
        let line = #"{"type":"appstore-enroll-command","app":"/Applications/Example.app","model_dir":"/u/Library/Application Support/unfairplay/models","script":""# + Self.rootScript + #"","verified":true}"#
        guard case let .appStoreEnrollCommand(command) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .appStoreEnrollCommand")
        }
        XCTAssertEqual(command.app, "/Applications/Example.app")
        XCTAssertEqual(command.modelDir, "/u/Library/Application Support/unfairplay/models")
        XCTAssertTrue(command.verified)
        XCTAssertTrue(command.script.hasPrefix("set -eu\numask 077\n"))
        XCTAssertTrue(command.script.contains(#"certificate leaf[subject.OU] = "ABCDE12345"' "$D/unfairplay""#),
                      "the codesign line, quotes and all")
        XCTAssertTrue(command.script.hasSuffix("trap - EXIT\necho \"$D\"\n"))
        XCTAssertNil(command.helper)
        XCTAssertNil(command.cancelFile)
        XCTAssertNil(command.timeLimitSeconds)
    }

    func test_the_2_0_enrol_command_decodes_every_field() throws {
        let line = #"{"type":"appstore-enroll-command","app":"/Applications/Example.app","model_dir":"/u/m","script":""# + Self.rootScript + #"","verified":true,"helper":"/Applications/Graft.app/Contents/Helpers/bin/unfairplay","unverified_kind":null,"unverified_reason":null,"cancel_file":"/u/Library/Caches/graft/enroll-stop.AbCd1234","time_limit_seconds":900}"#
        guard case let .appStoreEnrollCommand(command) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .appStoreEnrollCommand")
        }
        XCTAssertEqual(command.helper, "/Applications/Graft.app/Contents/Helpers/bin/unfairplay")
        XCTAssertNil(command.unverifiedKind)
        XCTAssertNil(command.unverifiedReason)
        XCTAssertEqual(command.cancelFile, "/u/Library/Caches/graft/enroll-stop.AbCd1234")
        XCTAssertEqual(command.timeLimitSeconds, 900)

        let unchecked = #"{"type":"appstore-enroll-command","app":"/Applications/Example.app","model_dir":"/u/m","script":"set -eu\n","verified":false,"helper":"/Applications/Graft.app/Contents/Helpers/bin/unfairplay","unverified_kind":"ad-hoc","unverified_reason":"This Graft.app has no developer team signature to check UnfairPlay against.","cancel_file":"/u/c","time_limit_seconds":900}"#
        guard case let .appStoreEnrollCommand(adHoc) = try BuildEvent.decode(line: unchecked) else {
            return XCTFail("expected .appStoreEnrollCommand")
        }
        XCTAssertFalse(adHoc.verified)
        XCTAssertEqual(adHoc.unverifiedKind, CLIContract.UnverifiedHelperKind.adHoc)
        XCTAssertEqual(UncheckedHelper(adHoc), UncheckedHelper(
            kind: "ad-hoc", helper: "/Applications/Graft.app/Contents/Helpers/bin/unfairplay",
            message: "This Graft.app has no developer team signature to check UnfairPlay against."
        ))
    }

    func test_enrolled_decodes_from_the_clis_payload() throws {
        let line = #"{"type":"appstore-enrolled","model_dir":"/u/Library/Application Support/unfairplay/models","count":2}"#
        guard case let .appStoreEnrolled(enrolled) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .appStoreEnrolled")
        }
        XCTAssertEqual(enrolled, EnrolledRecord(modelDir: "/u/Library/Application Support/unfairplay/models", count: 2))
    }

    /// The CLI's shape (graft_ipa/cli.py, pinned by tests/test_enroll_command_b3.py):
    /// `app` is the enrolling app's resolved path, not a configured app's name
    /// (fix-check #12, #15). Without `app` the line still decodes.
    func test_the_unverified_helper_refusal_decodes_and_is_read_off_the_events() throws {
        let line = #"{"type":"appstore-refused","app":"/Applications/Example.app","reason":"unverified-helper","kind":"not-bundled","helper":"/u/src/unfairplay/unfairplay","message":"This UnfairPlay isn't the one inside Graft.app, so nothing can check it before root runs it."}"#
        let events = line.buildEvents()
        guard case let .appStoreRefused(record)? = events.first else { return XCTFail("did not decode") }
        XCTAssertEqual(record.app, "/Applications/Example.app")
        XCTAssertEqual(record.kind, CLIContract.UnverifiedHelperKind.notBundled)
        XCTAssertEqual(UncheckedHelper.refusal(in: events), UncheckedHelper(
            kind: "not-bundled", helper: "/u/src/unfairplay/unfairplay",
            message: "This UnfairPlay isn't the one inside Graft.app, so nothing can check it before root runs it."
        ))
        XCTAssertNil(NeedsLicense.refusal(in: events))
        XCTAssertNil(UncheckedHelper.refusal(in: #"{"type":"appstore-refused","app":"a","reason":"unverified"}"#.buildEvents()))
        let bare = #"{"type":"appstore-refused","reason":"unverified-helper","kind":"ad-hoc","helper":"/h","message":"m"}"#
        XCTAssertEqual(UncheckedHelper.refusal(in: bare.buildEvents()), UncheckedHelper(kind: "ad-hoc", helper: "/h", message: "m"),
                       "a refusal without app still decodes")
    }

    func test_a_needs_license_refusal_carries_the_price() throws {
        guard case let .appStoreRefused(refusal) = try BuildEvent.decode(
            line: #"{"type":"appstore-refused","app":"example","reason":"needs-license","price":0,"formatted_price":"Free","track_id":100000001}"#
        ) else { return XCTFail("expected .appStoreRefused") }
        XCTAssertEqual(refusal.price, 0)
        XCTAssertEqual(refusal.formattedPrice, "Free")
        XCTAssertEqual(refusal.trackId, 100000001)
    }

    func test_status_carries_the_email_the_1x_note_and_a_model_problem() throws {
        let line = #"{"type":"appstore-status","ipatool":{"path":"/i","present":true,"version":"2.8.0","signed_in":true,"warning":null,"email":"someone@example.invalid","legacy_signin":false},"unfairplay":{"path":"/u","present":true,"version":"v0.4.0","enrolled":false,"model_dir":"/m","model_problem":"the model belongs to root"},"setup_commands":[]}"#
        guard case let .appStoreStatus(status) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .appStoreStatus")
        }
        XCTAssertEqual(status.ipatool.email, "someone@example.invalid")
        XCTAssertEqual(status.ipatool.legacySignin, false)
        XCTAssertEqual(status.unfairplay.modelProblem, "the model belongs to root")
    }

    // MARK: canGet — what Add App's "Get it after adding" needs

    private func status(signedIn: Bool? = true, warning: String? = nil, upPresent: Bool = true,
                        enrolled: Bool = true) -> AppStoreStatusRecord {
        AppStoreStatusRecord(
            ipatool: .init(path: "/i", present: true, version: "2.8.0", signedIn: signedIn, warning: warning),
            unfairplay: .init(path: "/u", present: upPresent, version: "v0.4.0", enrolled: enrolled, modelDir: "/m"),
            setupCommands: []
        )
    }

    func test_a_get_can_run_only_with_both_tools_ready() {
        XCTAssertTrue(status().canGet)
        XCTAssertFalse(status(signedIn: false).canGet)
        XCTAssertFalse(status(signedIn: nil).canGet, "couldn't tell is not signed in")
        XCTAssertFalse(status(warning: "2.8.1 can't download").canGet)
        XCTAssertFalse(status(upPresent: false).canGet)
        XCTAssertFalse(status(enrolled: false).canGet)
    }

    /// B5: what stops Get latest and Update & Install before the App Store
    /// is set up. Only what status *says* is missing, or a known-bad ipatool
    /// (`canGet`'s too): "couldn't tell" (a locked keychain) isn't setup, and
    /// the get says what it is.
    func test_setup_is_missing_only_when_status_says_a_part_is() {
        XCTAssertFalse(status().setUpMissing)
        XCTAssertTrue(status(signedIn: false).setUpMissing)
        XCTAssertTrue(status(upPresent: false).setUpMissing)
        XCTAssertTrue(status(enrolled: false).setUpMissing)
        XCTAssertFalse(status(signedIn: nil).setUpMissing, "couldn't tell isn't signed out")
        XCTAssertTrue(status(warning: "2.8.1 can't download").setUpMissing,
                      "a known-bad ipatool: Settings › App Store says what to do, as Add App does")
        let legacy = AppStoreStatusRecord(
            ipatool: .init(path: "/i", present: true, version: "2.8.0", signedIn: nil, warning: nil,
                           legacySignin: true),
            unfairplay: .init(path: "/u", present: true, version: "v0.4.0", enrolled: true, modelDir: "/m"),
            setupCommands: []
        )
        XCTAssertTrue(legacy.setUpMissing, "Graft 1.x's sign-in: Sign In again")
    }

    /// The re-verify: `canGet` (Add App's offer) needs no known-bad ipatool,
    /// and `setUpMissing` (the pane's Get) didn't look, so the two disagreed.
    /// They now agree on every status but one: "couldn't tell" (a locked
    /// keychain) isn't setup to do, so the pane leaves it to the CLI to say.
    func test_setup_missing_is_cant_get_but_for_couldnt_tell() {
        for signedIn in [true, false, nil] as [Bool?] {
            for warning in [nil, "2.8.1 can't download"] as [String?] {
                for upPresent in [true, false] {
                    for enrolled in [true, false] {
                        let s = status(signedIn: signedIn, warning: warning, upPresent: upPresent, enrolled: enrolled)
                        let couldntTell = signedIn == nil && warning == nil && upPresent && enrolled
                        XCTAssertEqual(s.setUpMissing, couldntTell ? false : !s.canGet,
                                       "\(String(describing: signedIn)) \(String(describing: warning)) \(upPresent) \(enrolled)")
                    }
                }
            }
        }
    }
}
