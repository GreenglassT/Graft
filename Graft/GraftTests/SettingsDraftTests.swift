import XCTest
@testable import Graft

/// The rule the settings editor turns on: send only what actually differs.
///
/// `--set-settings` is a PATCH, so posting every field would rewrite
/// `signing.cert` each time somebody nudged the retention stepper — and it
/// would hollow out the `changed` reconciliation, since asking for five fields
/// and getting five back proves nothing if four were never edited.
final class SettingsDraftTests: XCTestCase {

    private func record(
        device: String? = "UDID-A",
        keep: Int? = 2,
        cert: String? = "/c.p12",
        profile: String? = "/p.mobileprovision",
        team: String? = "TEAM1"
    ) -> SettingsRecord {
        SettingsRecord(
            defaultDeviceUDID: device, archiveKeep: keep,
            signingCert: cert, signingProfile: profile, signingTeamId: team
        )
    }

    func test_an_untouched_draft_sends_nothing() {
        let draft = SettingsDraft(record())
        XCTAssertFalse(draft.hasChanges)
        XCTAssertNil(draft.changed(\.defaultDeviceUDID))
        XCTAssertNil(draft.changed(\.archiveKeep))
        XCTAssertNil(draft.changed(\.signingCert))
        XCTAssertNil(draft.changed(\.signingProfile))
        XCTAssertNil(draft.changed(\.signingTeamId))
    }

    func test_editing_one_field_sends_only_that_field() {
        var draft = SettingsDraft(record())
        draft.edited.archiveKeep = 5

        XCTAssertEqual(draft.changed(\.archiveKeep), 5)
        XCTAssertNil(draft.changed(\.signingCert), "a stepper nudge rewrote the certificate path")
        XCTAssertNil(draft.changed(\.signingTeamId))
        XCTAssertTrue(draft.hasChanges)
    }

    /// Typing and then undoing is not an edit. This is why the draft holds a
    /// baseline rather than a dirty flag.
    func test_typing_and_reverting_leaves_nothing_to_send() {
        var draft = SettingsDraft(record(team: "TEAM1"))
        draft.edited.signingTeamId = "TEAM2"
        XCTAssertTrue(draft.hasChanges)

        draft.edited.signingTeamId = "TEAM1"
        XCTAssertFalse(draft.hasChanges)
        XCTAssertNil(draft.changed(\.signingTeamId))
    }

    /// "" is the wire form of "no default device" on both sides — the picker's
    /// "Ask each time" row and the CLI's unset. Clearing it has to read as an
    /// edit, not as an absence, or the setting can never be turned off.
    func test_clearing_the_default_device_is_an_edit() {
        var draft = SettingsDraft(record(device: "UDID-A"))
        draft.edited.defaultDeviceUDID = ""

        XCTAssertEqual(draft.changed(\.defaultDeviceUDID), "")
        XCTAssertTrue(draft.hasChanges)
    }

    /// And an already-unset device seeds as "" without looking edited.
    func test_an_unset_device_seeds_empty_and_clean() {
        let draft = SettingsDraft(record(device: nil))
        XCTAssertEqual(draft.edited.defaultDeviceUDID, "")
        XCTAssertFalse(draft.hasChanges)
    }

    /// Before the first successful read there is no baseline to diff against,
    /// so nothing may be sent — the pane refuses to offer editing in that
    /// state, and this is the belt underneath it.
    func test_a_draft_with_no_baseline_sends_nothing() {
        var draft = SettingsDraft()
        draft.edited.signingTeamId = "TYPED"

        XCTAssertNil(draft.changed(\.signingTeamId))
        XCTAssertFalse(draft.hasChanges)
    }

    /// Half a `signing:` block is the state this editor exists to repair, so it
    /// has to arrive as five editable fields rather than as nothing.
    ///
    /// The CLI reports null for every key it can't find — that is the whole
    /// point of reading the YAML raw instead of through `load_config`. While
    /// `SettingsRecord`'s fields were non-optional the line failed to decode
    /// and the pane rendered "Couldn't read your settings." on exactly the
    /// config the feature was written for.
    func test_a_half_filled_signing_block_seeds_empty_and_clean() {
        let draft = SettingsDraft(SettingsRecord(
            defaultDeviceUDID: nil, archiveKeep: 2,
            signingCert: "/c.p12", signingProfile: nil, signingTeamId: nil
        ))

        XCTAssertEqual(draft.edited.signingCert, "/c.p12")
        XCTAssertEqual(draft.edited.signingProfile, "")
        XCTAssertEqual(draft.edited.signingTeamId, "")
        XCTAssertFalse(draft.hasChanges, "reading a config with gaps looked like an edit")
    }

    /// A retention value the CLI refuses to read is null, not absent — there is
    /// a value on disk that every build rejects. The pane says "saving replaces
    /// it" over the stepper, so a plain Save has to actually send it.
    ///
    /// It didn't. Seeding `original` and `edited` from the same fallback made
    /// them equal, so `changed` was always nil and no `--archive-keep` ever went
    /// out; the only way to repair the config was to step the value away and
    /// back, which nothing tells the user to do.
    func test_an_unreadable_retention_is_sent_so_a_save_repairs_it() {
        let draft = SettingsDraft(record(keep: nil))

        XCTAssertEqual(draft.edited.archiveKeep, SettingsDraft.defaultArchiveKeep)
        XCTAssertEqual(
            draft.changed(\.archiveKeep), SettingsDraft.defaultArchiveKeep,
            "Save sent nothing for a value the pane promised to replace"
        )
        XCTAssertTrue(draft.hasChanges, "Save was disabled on a config that needs repairing")
        // And only that field: the rest still match what the CLI reported.
        XCTAssertNil(draft.changed(\.signingCert))
        XCTAssertNil(draft.changed(\.signingTeamId))
    }

    /// K4 / C-SETTINGS: a null retention because the *file* does not parse is
    /// not a value a save replaces — `--set-settings` refuses that file — so it
    /// is not a pending edit, and the pane does not light Revert over it.
    func test_a_config_that_does_not_parse_has_nothing_pending() {
        var unparsed = record(keep: nil)
        unparsed.parseError = "mapping values are not allowed here (line 3, column 9)"
        let draft = SettingsDraft(unparsed)
        XCTAssertFalse(draft.hasChanges)
        XCTAssertNil(draft.changed(\.archiveKeep))
    }

    /// The ordinary case is untouched — a readable value that nobody edited is
    /// still not sent, which is what keeps `--set-settings` a PATCH.
    func test_a_readable_retention_is_not_sent_when_untouched() {
        let draft = SettingsDraft(record(keep: 5))
        XCTAssertNil(draft.changed(\.archiveKeep))
        XCTAssertFalse(draft.hasChanges)
    }

    /// 0 is a legal on-disk value, so it must round-trip without looking edited.
    func test_zero_retention_seeds_clean() {
        let draft = SettingsDraft(record(keep: 0))
        XCTAssertEqual(draft.edited.archiveKeep, 0)
        XCTAssertFalse(draft.hasChanges)
    }

    /// Every field, set and read back through `changed`.
    ///
    /// This subsumes a 22-line test that poked each field separately to prove
    /// `hasChanges` noticed it — necessary while `hasChanges` was a
    /// hand-written five-way disjunction that could silently lose a term, and
    /// dead weight now that it is `edited != original` over a synthesized `==`.
    /// What still needs asserting is the part a compiler cannot check: that
    /// `init` seeds each field from the matching record key.
    func test_every_editable_field_is_reachable() {
        var draft = SettingsDraft(record())
        draft.edited.defaultDeviceUDID = "UDID-B"
        draft.edited.archiveKeep = 9
        draft.edited.signingCert = "/new.p12"
        draft.edited.signingProfile = "/new.mobileprovision"
        draft.edited.signingTeamId = "TEAM9"

        XCTAssertEqual(draft.changed(\.defaultDeviceUDID), "UDID-B")
        XCTAssertEqual(draft.changed(\.archiveKeep), 9)
        XCTAssertEqual(draft.changed(\.signingCert), "/new.p12")
        XCTAssertEqual(draft.changed(\.signingProfile), "/new.mobileprovision")
        XCTAssertEqual(draft.changed(\.signingTeamId), "TEAM9")
    }

}
