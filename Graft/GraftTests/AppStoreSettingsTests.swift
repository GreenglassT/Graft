import AppKit
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
        commands: [String] = [],
        email: String? = nil,
        legacy: Bool? = nil,
        modelProblem: String? = nil
    ) -> AppStoreStatusRecord {
        AppStoreStatusRecord(
            ipatool: .init(
                path: "/u/.local/bin/ipatool", present: ipatoolPresent, version: version,
                signedIn: signedIn, warning: warning, email: email, legacySignin: legacy
            ),
            unfairplay: .init(
                path: "/u/up", present: unfairplayPresent, version: "dev-d75",
                enrolled: enrolled, modelDir: "/u/models", modelProblem: modelProblem
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

    // MARK: - K12: each tool's own install command

    /// `appstore-status` carries one install string per tool for a Copy
    /// button; each row takes its own, and a CLI without them still decodes.
    func test_each_row_carries_its_tools_install_command() throws {
        let line = ##"{"type":"appstore-status","ipatool":{"path":"/u/.local/bin/ipatool","present":false,"version":null,"signed_in":false,"warning":null,"install":"mkdir -p /u/.local/bin && curl -fL https://example.invalid/ipatool.tar.gz | tar -xzf -"},"unfairplay":{"path":"/u/up","present":false,"version":null,"enrolled":false,"model_dir":"/u/models","install":"git clone https://example.invalid/unfairplay && go -C unfairplay build ./cmd/unfairplay"},"setup_commands":["# install ipatool"]}"##
        guard case let .appStoreStatus(record)? = line.buildEvents().first else {
            return XCTFail("did not decode")
        }
        let rows = AppStoreSettingsSection.rows(for: record)
        XCTAssertEqual(rows.map(\.install), [
            "mkdir -p /u/.local/bin && curl -fL https://example.invalid/ipatool.tar.gz | tar -xzf -",
            "git clone https://example.invalid/unfairplay && go -C unfairplay build ./cmd/unfairplay",
        ])

        let old = #"{"type":"appstore-status","ipatool":{"path":"/i","present":true,"version":"2.8.0","signed_in":true,"warning":null},"unfairplay":{"path":"/u","present":true,"version":"v0.4.0","enrolled":true,"model_dir":"/m"},"setup_commands":[]}"#
        guard case let .appStoreStatus(older)? = old.buildEvents().first else {
            return XCTFail("a status without `install` must still decode")
        }
        XCTAssertEqual(AppStoreSettingsSection.rows(for: older).map(\.install), [nil, nil])
    }

    // MARK: The account, since 2.0

    func test_signed_in_shows_the_email_and_offers_sign_out() {
        let s = status(email: "someone@example.invalid")
        XCTAssertEqual(AppStoreSettingsSection.rows(for: s)[0].detail, "2.8.0 · signed in as someone@example.invalid")
        XCTAssertEqual(AppStoreSettingsSection.accountAction(for: s), .signOut)
    }

    func test_signed_out_offers_sign_in() {
        XCTAssertEqual(AppStoreSettingsSection.accountAction(for: status(signedIn: false)), .signIn)
    }

    /// Fix-check #13: the CLI's hint for an expired sign-in whose renewal
    /// needs a code sends the user to Settings › App Store, where status
    /// still reads signed in. Sign In Again… is the way in from there.
    func test_signed_in_offers_sign_in_again_beside_sign_out() {
        typealias S = AppStoreSettingsSection
        XCTAssertEqual(S.accountButtons(for: status(email: "someone@example.invalid")), [.signInAgain, .signOut])
        XCTAssertEqual(S.accountButtons(for: status(signedIn: false)), [.signIn])
        XCTAssertEqual(S.accountButtons(for: status(signedIn: nil, legacy: true)), [.signIn])
        XCTAssertEqual(S.accountButtons(for: status(signedIn: nil)), [])
        XCTAssertEqual(S.accountButtons(for: status(ipatoolPresent: false, signedIn: false)), [])
        XCTAssertEqual(S.AccountButton.signInAgain.label, "Sign In Again\u{2026}", "the CLI's hint names it, ellipsis and all")
        XCTAssertEqual(S.AccountButton.signIn.label, "Sign In\u{2026}")
        XCTAssertEqual(S.AccountButton.signOut.label, "Sign Out")
        XCTAssertEqual(CLIContract.SettingsLabel.setUpDecryption, "Set Up Decryption\u{2026}")
        XCTAssertEqual(CLIContract.SettingsLabel.setUpAgain, "Set Up Again\u{2026}")
    }

    /// Until the sheet's own status read has named an ipatool, it says what
    /// it is waiting for, or why it can't say.
    func test_the_sheet_says_it_is_checking_which_ipatool_and_why_it_couldnt() {
        typealias Sheet = AppStoreSignInSheet
        let named = status().ipatool
        XCTAssertEqual(Sheet.checkLine(checked: false, ipatool: nil, statusError: nil), Sheet.checkingLine)
        XCTAssertNil(Sheet.checkLine(checked: true, ipatool: named, statusError: nil))
        let failed = Sheet.checkLine(checked: true, ipatool: nil, statusError: "exit 1: no such option")
        XCTAssertEqual(failed, "Graft couldn't check which ipatool signs in: exit 1: no such option. Cancel and try again.")
    }

    /// Sign In Again… opens the sign-in sheet, and the sheet reads status
    /// again before it says where the password goes. Clicked through the
    /// window; the stand-in CLI answers status only.
    @MainActor
    func test_sign_in_again_opens_the_sign_in_sheet_which_reads_status_first() async throws {
        try AccessibilityTree.requireServed()
        let dir = FileManager.default.temporaryDirectory.appending(path: "settings_signin_\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let file = dir.appending(path: "graft.sh")
        try """
        #!/bin/sh
        printf '%s\\n' "$*" >> '\(dir.path)/argv.txt'
        case "$*" in
          *--appstore-status*)
            printf '%s\\n' '{"type":"appstore-status","ipatool":{"path":"/u/x/ipatool","source":"config","present":true,"version":"2.8.0","signed_in":true,"email":"someone@example.invalid","legacy_signin":false,"warning":null},"unfairplay":{"path":"/u","present":true,"version":"v0.4.0","enrolled":true,"model_dir":"/m"},"setup_commands":[]}' ;;
        esac
        """.write(to: file, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: file.path)
        let store = AppStoreStore(runner: CLIRunner(executable: file), shell: NoAdminShell())
        await store.loadStatus()
        func statusReads() throws -> Int {
            try String(contentsOf: dir.appending(path: "argv.txt"), encoding: .utf8)
                .components(separatedBy: "--appstore-status").count - 1
        }
        XCTAssertEqual(try statusReads(), 1)

        let box = hosted(AppStoreSettingsSection(store: store), width: 720)
        settle(box)
        try clickButton(named: CLIContract.SettingsLabel.signInAgain, in: box.window)
        let deadline = Date().addingTimeInterval(3)
        while box.window.attachedSheet == nil, Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.02))
        }
        let sheet = try XCTUnwrap(box.window.attachedSheet, "Sign In Again… opens a sheet")
        await eventually({ store.hasCheckedForSignIn }, "the sheet reads status as it opens")
        XCTAssertEqual(try statusReads(), 2)
        settle(box)

        let texts = try AccessibilityTree.texts(in: box.window)
        XCTAssertTrue(texts.contains("Sign in to the App Store"), "the sign-in sheet: \(texts)")
        let note = try XCTUnwrap(AppStoreSignInSheet.elsewhereNote(for: try XCTUnwrap(store.signInIpatool)))
        XCTAssertTrue(texts.contains(note), "it names the ipatool the password goes to: \(texts)")
        box.window.endSheet(sheet)
    }

    func test_couldnt_tell_offers_neither() {
        XCTAssertNil(AppStoreSettingsSection.accountAction(for: status(signedIn: nil)))
    }

    /// The CLI's "graft-ipatool-signin": Graft.app's own ipatool holds the
    /// sign-in, and the configured ipatool can't read it. The CLI's sentence
    /// says Sign In… fixes that, so the row must offer it, and without the
    /// CLI's sentence the app's own words still say so.
    func test_a_sign_in_only_graft_s_ipatool_can_read_offers_sign_in() throws {
        let json = #"{"ipatool":{"path":"/u/bin/ipatool","present":true,"version":"2.8.0","signed_in":null,"warning":null,"unknown_reason":"graft-ipatool-signin"},"unfairplay":{"path":"/u","present":true,"version":"dev","enrolled":true,"model_dir":"/m"},"setup_commands":[]}"#
        let s = try JSONDecoder().decode(AppStoreStatusRecord.self, from: Data(json.utf8))
        XCTAssertEqual(AppStoreSettingsSection.accountAction(for: s), .signIn)
        let line = try XCTUnwrap(AppStoreSettingsSection.unknownLine(s.ipatool))
        XCTAssertTrue(line.contains("Sign In\u{2026}"), line)
        XCTAssertTrue(line.contains("/u/bin/ipatool"), line)
    }

    func test_no_ipatool_offers_neither() {
        XCTAssertNil(AppStoreSettingsSection.accountAction(for: status(ipatoolPresent: false, signedIn: false)))
    }

    func test_a_1x_sign_in_is_not_ok_says_why_and_offers_sign_in() {
        let s = status(signedIn: nil, legacy: true)
        let row = AppStoreSettingsSection.rows(for: s)[0]
        XCTAssertFalse(row.ok)
        XCTAssertTrue(row.detail.contains("sign in again"))
        XCTAssertEqual(AppStoreSettingsSection.accountAction(for: s), .signIn)
    }

    func test_the_terminal_block_is_only_for_a_missing_tool() {
        XCTAssertFalse(AppStoreSettingsSection.showsTerminalCommands(
            status(signedIn: false, commands: ["graft --appstore-login"])
        ))
        XCTAssertTrue(AppStoreSettingsSection.showsTerminalCommands(
            status(ipatoolPresent: false, commands: ["# install ipatool"])
        ))
        XCTAssertTrue(AppStoreSettingsSection.showsTerminalCommands(
            status(unfairplayPresent: false, commands: ["# build UnfairPlay"])
        ))
    }

    func test_a_model_problem_is_the_unfairplay_rows_warning() {
        let row = AppStoreSettingsSection.rows(for: status(enrolled: false, modelProblem: "belongs to root"))[1]
        XCTAssertEqual(row.warning, "belongs to root")
    }

    func test_sign_in_needs_an_email_and_a_password() {
        XCTAssertTrue(AppStoreSignInSheet.canSignIn(email: " someone@example.invalid ", password: "x"))
        XCTAssertFalse(AppStoreSignInSheet.canSignIn(email: "someone", password: "x"))
        XCTAssertFalse(AppStoreSignInSheet.canSignIn(email: "someone@example.invalid", password: ""))
    }

    /// Review part 4, #20 and #21.
    func test_an_apple_id_is_an_email_or_a_phone_number_and_a_password_has_no_line_break() {
        typealias Sheet = AppStoreSignInSheet
        XCTAssertEqual(Sheet.appleID(" someone@example.invalid\n"), "someone@example.invalid")
        XCTAssertNil(Sheet.appleID("some one@example.invalid"))
        XCTAssertEqual(Sheet.appleID("+1 555-555-0100"), "+15555550100", "sent without the spaces the CLI refuses")
        XCTAssertEqual(Sheet.appleID("5555550100"), "5555550100")
        XCTAssertEqual(Sheet.appleID(" +44 20 7946 0000 "), "+442079460000")
        XCTAssertNil(Sheet.appleID("someone"))
        XCTAssertNil(Sheet.appleID("555"), "too short to be a phone number")
        XCTAssertNil(Sheet.appleID("+1 (555) 555-0100"), "digits, spaces and dashes only")
        XCTAssertNil(Sheet.appleID("1234567890123456"), "longer than any phone number")
        XCTAssertNil(Sheet.appleID("+"))
        XCTAssertTrue(Sheet.canSignIn(email: "+1 555-555-0100", password: "x"))
        XCTAssertFalse(Sheet.canSignIn(email: "someone@example.invalid", password: "pass\nword"))
        XCTAssertFalse(Sheet.canSignIn(email: "someone@example.invalid", password: "pass\rword"))
        XCTAssertEqual(Sheet.passwordProblem("pass\nword"), "A password can't contain a line break.")
        XCTAssertNil(Sheet.passwordProblem("Example-Passw0rd"))
    }

    func test_a_typed_code_is_its_digits() {
        XCTAssertEqual(AppStoreSignInSheet.codeBytes("482 913"), Array("482913".utf8))
        XCTAssertEqual(AppStoreSignInSheet.codeBytes("48-29-13x"), Array("482913".utf8))
        XCTAssertEqual(AppStoreSignInSheet.codeBytes("٤٨٢"), [], "ASCII digits only")
    }

    func test_the_code_prompt_never_promises_a_code() {
        // Decision 9: after a wrong password Apple sends no code at all.
        XCTAssertTrue(AppStoreSignInSheet.codePrompt.contains("If no code appears, the password may be wrong"))
    }

    func test_the_sheet_says_what_ipatool_keeps_and_what_signing_in_replaces() {
        // Decisions 1 and 2, in the words the controller chose.
        XCTAssertTrue(AppStoreSignInSheet.explainer.contains(
            "keeps your sign-in, including your password, in your login Keychain"
        ))
        XCTAssertTrue(AppStoreSignInSheet.legacyNote.hasPrefix(
            "This also signs out any ipatool you installed yourself; it will need to sign in again if you use it in Terminal."
        ))
    }

    /// The critic: a model made for another Apple ID left an enrolled Mac
    /// with no way to enroll again but Terminal. Set Up Again… is that way.
    func test_set_up_decryption_when_not_enrolled_and_set_up_again_when_enrolled() {
        XCTAssertEqual(AppStoreSettingsSection.enrollAction(status(enrolled: false)), .setUp)
        XCTAssertEqual(AppStoreSettingsSection.enrollAction(status(enrolled: true)), .setUpAgain)
        XCTAssertNil(AppStoreSettingsSection.enrollAction(status(unfairplayPresent: false, enrolled: false)))
        XCTAssertTrue(AppStoreSettingsSection.setUpAgainHelp.contains("another Apple ID"))
    }

    func test_the_enrol_sheet_says_to_use_the_mac_app_stores_apple_id() {
        XCTAssertTrue(AppStoreEnrollSheet.intro.contains("works only for the Apple ID that installed that app"))
        XCTAssertTrue(AppStoreEnrollSheet.intro.contains("use the same Apple ID in Graft as in the Mac App Store"))
    }

    // MARK: Which ipatool gets the password (the review's critic)

    private func ipatool(source: String?) -> AppStoreStatusRecord.IPATool {
        .init(path: "/u/.local/bin/ipatool", present: true, version: "2.8.0", signedIn: false, warning: nil, source: source)
    }

    func test_the_sheet_says_when_the_password_goes_to_another_ipatool_and_why() throws {
        XCTAssertNil(AppStoreSignInSheet.elsewhereNote(for: ipatool(source: "bundled")))
        XCTAssertNil(AppStoreSignInSheet.elsewhereNote(for: ipatool(source: nil)), "a CLI too old to say")
        let config = try XCTUnwrap(AppStoreSignInSheet.elsewhereNote(for: ipatool(source: "config")))
        XCTAssertTrue(config.hasPrefix("Your Apple ID and password go to the ipatool at /u/.local/bin/ipatool, not Graft's own copy."))
        XCTAssertTrue(config.contains("config.yaml"))
        for source in ["default", "PATH"] {
            let found = try XCTUnwrap(AppStoreSignInSheet.elsewhereNote(for: ipatool(source: source)))
            XCTAssertTrue(found.contains("found on PATH or in ~/.local/bin"), source)
        }
        XCTAssertEqual(AppStoreSignInSheet.elsewhereNote(for: ipatool(source: "something-new")),
                       "Your Apple ID and password go to the ipatool at /u/.local/bin/ipatool, not Graft's own copy.")
    }

    /// The try14 delta audit, F2: only Graft.app's own ipatool deletes a
    /// sign-in it can't read before it signs in. Any other one's sign-in
    /// clears only what Graft's record vouches for (the CLI's
    /// `appstore_account.needs_clearing`), so it saves over that sign-in and
    /// macOS may ask for the login keychain password: the CLI's own status
    /// line for it. A CLI too old to name the source gets no promise either.
    func test_the_legacy_note_promises_removing_it_first_only_for_graft_s_own_ipatool() throws {
        func legacy(_ source: String?, _ value: Bool? = true) -> AppStoreStatusRecord.IPATool {
            .init(path: "/u/.local/bin/ipatool", present: true, version: "2.8.0", signedIn: nil, warning: nil,
                  legacySignin: value, source: source)
        }
        let sources: [String?] = [CLIContract.ToolSource.bundled, CLIContract.ToolSource.config,
                                  CLIContract.ToolSource.defaultPath, CLIContract.ToolSource.path,
                                  nil, "something-new"]
        for source in sources {
            XCTAssertNil(AppStoreSignInSheet.legacyText(for: legacy(source, false)), "not legacy: \(source ?? "nil")")
            XCTAssertNil(AppStoreSignInSheet.legacyText(for: legacy(source, nil)), "not said: \(source ?? "nil")")
        }
        XCTAssertEqual(AppStoreSignInSheet.legacyText(for: legacy(CLIContract.ToolSource.bundled)),
                       AppStoreSignInSheet.legacyNote)
        XCTAssertTrue(AppStoreSignInSheet.legacyNote.hasSuffix("so Graft removes it first.)"), "premise")
        for source in sources where source != CLIContract.ToolSource.bundled {
            let note = try XCTUnwrap(AppStoreSignInSheet.legacyText(for: legacy(source)), source ?? "nil")
            XCTAssertEqual(note, AppStoreSignInSheet.otherLegacyNote(path: "/u/.local/bin/ipatool"), source ?? "nil")
            XCTAssertFalse(note.contains("removes it"), "nothing is removed first: \(source ?? "nil")")
            XCTAssertFalse(note.contains("signs out"), source ?? "nil")
        }
        XCTAssertEqual(
            AppStoreSignInSheet.otherLegacyNote(path: "/u/.local/bin/ipatool"),
            "The ipatool at /u/.local/bin/ipatool can't read the App Store sign-in in your login keychain "
                + "(another ipatool made it, or a Keychain dialog was denied). Signing in with it saves over "
                + "that sign-in, and macOS may ask for your login keychain password while it does."
        )
    }

    func test_status_carries_the_ipatools_source() throws {
        let line = #"{"type":"appstore-status","ipatool":{"path":"/u/.local/bin/ipatool","present":true,"version":"2.8.0","signed_in":false,"warning":null,"source":"config"},"unfairplay":{"path":"/u/up","present":true,"version":"v0.4.0","enrolled":true,"model_dir":"/m"},"setup_commands":[]}"#
        guard case let .appStoreStatus(record)? = line.buildEvents().first else { return XCTFail("did not decode") }
        XCTAssertEqual(record.ipatool.source, CLIContract.ToolSource.config)
    }

    // MARK: The owner's try12 run (2026-11-22)

    /// A stand-in CLI: `body` is the case arms after the argv line; the
    /// status calls are counted in `count` first, so a test can tell them apart.
    private func standIn(_ body: (URL) -> String) throws -> (CLIRunner, URL) {
        let dir = FileManager.default.temporaryDirectory.appending(path: "settings_try12_\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let file = dir.appending(path: "graft.sh")
        try """
        #!/bin/sh
        case "$*" in
          *--appstore-status*) n=$(/bin/cat '\(dir.path)/count' 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > '\(dir.path)/count' ;;
        esac
        printf '%s\\n' "$*" >> '\(dir.path)/argv.txt'
        \(body(dir))
        """.write(to: file, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: file.path)
        return (CLIRunner(executable: file), dir)
    }

    static let signedInLine = #"{"type":"appstore-status","ipatool":{"path":"/i","source":"bundled","present":true,"version":"2.8.0","signed_in":true,"email":"someone@example.invalid","legacy_signin":false,"warning":null},"unfairplay":{"path":"/u","present":true,"version":"v0.4.0","enrolled":true,"model_dir":"/m"},"setup_commands":[]}"#
    static let legacyLine = #"{"type":"appstore-status","ipatool":{"path":"/i","source":"bundled","present":true,"version":"2.8.0","signed_in":null,"email":null,"legacy_signin":true,"warning":null},"unfairplay":{"path":"/u","present":true,"version":"v0.4.0","enrolled":true,"model_dir":"/m"},"setup_commands":[]}"#
    static let legacyDetail = "2.8.0 · signed in by another ipatool (Graft 1's?): sign in again"
    static let otherLegacyDetail = "2.8.0 · a sign-in this ipatool can't read (another ipatool's?): sign in again"

    /// Status answers `first` on its first call and `later` after that; the
    /// call numbered `slow` sleeps `seconds` before it answers.
    private func statusCLI(first: String, later: String, slow: Int, seconds: Int) throws -> (CLIRunner, URL) {
        try standIn { dir in
            try? (first + "\n").write(to: dir.appending(path: "first.json"), atomically: true, encoding: .utf8)
            try? (later + "\n").write(to: dir.appending(path: "later.json"), atomically: true, encoding: .utf8)
            return """
            case "$*" in
              *--appstore-status*)
                [ "$n" = \(slow) ] && /bin/sleep \(seconds)
                if [ "$n" = 1 ]; then /bin/cat '\(dir.path)/first.json'; else /bin/cat '\(dir.path)/later.json'; fi ;;
            esac
            """
        }
    }

    /// (c) and (d): while status is read again, both rows say Checking… and
    /// show nothing of the last answer, so a stale "signed in" can't show.
    func test_while_status_is_read_again_the_rows_say_checking_and_nothing_of_the_last_answer() {
        let rows = AppStoreSettingsSection.rows(for: status(email: "someone@example.invalid"), checking: true)
        XCTAssertEqual(rows.map(\.name), ["ipatool", "UnfairPlay"])
        for row in rows {
            XCTAssertEqual(row.detail, AppStoreSettingsSection.checkingDetail)
            XCTAssertTrue(row.checking)
            XCTAssertFalse(row.ok, "never green while checking")
            XCTAssertNil(row.warning)
            XCTAssertNil(row.install)
        }
        XCTAssertEqual(AppStoreSettingsSection.checkingDetail, "Checking\u{2026}")
        XCTAssertFalse(AppStoreSettingsSection.rows(for: status()).contains { $0.checking })
    }

    /// (c) and (d), on screen: Refresh, then while the read runs the rows say
    /// Checking…, Refresh can't be pressed again, and "signed in" is gone;
    /// then the new answer (legacy) shows.
    @MainActor
    func test_refresh_shows_checking_on_both_rows_and_never_the_last_answer() async throws {
        try AccessibilityTree.requireServed()
        let (runner, _) = try statusCLI(first: Self.signedInLine, later: Self.legacyLine, slow: 2, seconds: 3)
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        await store.loadStatus()
        let box = hosted(AppStoreSettingsSection(store: store), width: 720)
        settle(box)
        let before = try AccessibilityTree.texts(in: box.window)
        XCTAssertTrue(before.contains("2.8.0 · signed in as someone@example.invalid"), "premise: \(before)")

        try clickButton(named: "Refresh", in: box.window)
        await eventually({ store.isLoadingStatus }, "Refresh starts a read")
        settle(box)
        let during = try AccessibilityTree.texts(in: box.window)
        XCTAssertGreaterThanOrEqual(during.filter { $0 == AppStoreSettingsSection.checkingDetail }.count, 2, "\(during)")
        XCTAssertFalse(during.contains { $0.contains("signed in as") }, "no stale answer while checking: \(during)")
        let nodes = try AccessibilityTree.nodes(in: box.window)
        XCTAssertFalse(nodes.contains { $0.role == kAXButtonRole && $0.name == "Refresh" && $0.enabled != false },
                       "Refresh can't be pressed again while it runs")
        // The critic's gap (406e4fb): every account button is there and
        // grayed out while the read runs; none can act on the last answer.
        let account = [CLIContract.SettingsLabel.signInAgain, "Sign Out", CLIContract.SettingsLabel.setUpAgain]
        for name in account {
            let button = nodes.first { $0.role == kAXButtonRole && $0.name == name }
            XCTAssertNotNil(button, "\(name) during the read")
            XCTAssertEqual(button?.enabled, false, "\(name) can't be pressed while status is read again")
        }

        await eventually({ !store.isLoadingStatus }, timeout: 10, "the read ends")
        settle(box)
        let after = try AccessibilityTree.texts(in: box.window)
        XCTAssertTrue(after.contains(Self.legacyDetail), "\(after)")
        XCTAssertFalse(after.contains(AppStoreSettingsSection.checkingDetail), "\(after)")
        // The new answer's buttons, pressable: legacy offers Sign In… only.
        let settled = try AccessibilityTree.nodes(in: box.window).filter { $0.role == kAXButtonRole }
        XCTAssertEqual(settled.first { $0.name == CLIContract.SettingsLabel.signIn }?.enabled, true, "\(settled.map(\.name))")
        XCTAssertEqual(settled.first { $0.name == CLIContract.SettingsLabel.setUpAgain }?.enabled, true)
        XCTAssertFalse(settled.contains { $0.name == "Sign Out" || $0.name == CLIContract.SettingsLabel.signInAgain },
                       "nothing of the last answer's buttons: \(settled.map(\.name))")
    }

    /// (d): a read that started first and finishes last never puts its older
    /// answer over the newer one.
    @MainActor
    func test_an_older_status_read_that_finishes_last_is_dropped() async throws {
        let (runner, dir) = try statusCLI(first: Self.signedInLine, later: Self.legacyLine, slow: 1, seconds: 2)
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        let older = Task { await store.loadStatus() }
        await eventually { (try? String(contentsOf: dir.appending(path: "argv.txt"), encoding: .utf8)) != nil }
        await store.loadStatus()
        XCTAssertEqual(store.status?.ipatool.legacySignin, true, "the newer read's answer")
        XCTAssertTrue(store.isLoadingStatus, "the older read is still running")
        await older.value
        XCTAssertEqual(store.status?.ipatool.legacySignin, true, "not the older read's signed in")
        XCTAssertNil(store.status?.ipatool.email)
        XCTAssertFalse(store.isLoadingStatus)
    }

    /// (b): Cancel in macOS's dialog closed the enroll sheet in the owner's
    /// run, so its "Canceled" line was never seen. Settings says how the
    /// last run ended under the rows, whether or not the sheet stayed up.
    func test_the_enrolment_outcome_has_a_line_for_settings() {
        typealias S = AppStoreSettingsSection
        XCTAssertEqual(S.enrolOutcomeLine(.cancelled), "Setting up decryption was canceled: nothing was changed.")
        XCTAssertEqual(S.enrolOutcomeLine(.stopped), "Setting up decryption was stopped: nothing was changed.")
        XCTAssertEqual(S.enrolOutcomeLine(.timedOut(limitSeconds: 900)), AppStoreEnrollSheet.timedOutLine(limitSeconds: 900))
        XCTAssertEqual(S.enrolOutcomeLine(.failed("setup failed")), "Setting up decryption failed: setup failed")
        let noSIDB = "error: normalize capture: canonical input has no SIDB state"
        XCTAssertEqual(S.enrolOutcomeLine(.failed(noSIDB)),
                       "Setting up decryption failed: " + noSIDB + "\n"
                       + "This Mac's App Store state, which UnfairPlay reads, isn't there right now: macOS sometimes "
                       + "clears it, even for an app you've already opened. Open the app you picked once, then set up again.",
                       "the row gives the sheet's advice too, once the sheet has closed, on a line of its own")
        for quiet in [AppStoreAccountStore.Enrol.idle, .preparing, .running, .done] {
            XCTAssertNil(S.enrolOutcomeLine(quiet), "\(quiet)")
        }
    }

    @MainActor
    func test_a_cancelled_enrolment_is_said_in_settings_once_its_sheet_has_closed() async throws {
        try AccessibilityTree.requireServed()
        let command = #"{"type":"appstore-enroll-command","app":"/Applications/Example.app","model_dir":"/u/m","script":"set -eu\necho x\n","verified":true}"#
        let (runner, _) = try standIn { dir in
            try? (Self.signedInLine + "\n").write(to: dir.appending(path: "status.json"), atomically: true, encoding: .utf8)
            try? (command + "\n").write(to: dir.appending(path: "command.json"), atomically: true, encoding: .utf8)
            return """
            case "$*" in
              *--appstore-status*) /bin/cat '\(dir.path)/status.json' ;;
              *--appstore-enroll-command*) /bin/cat '\(dir.path)/command.json' ;;
            esac
            """
        }
        let shell = AppStoreAccountStoreTests.RecordingShell()
        shell.error = AdminShellError(number: AdminShellError.userCancelled, message: "User canceled.")
        let store = AppStoreStore(runner: runner, shell: shell)
        await store.loadStatus()
        await store.account.prepareEnrol(appPath: "/Applications/Example.app")
        await store.account.confirmEnrol()
        XCTAssertEqual(store.account.enrol, .cancelled, "premise: Cancel in macOS's dialog")

        let box = hosted(AppStoreSettingsSection(store: store), width: 720)
        settle(box)
        let texts = try AccessibilityTree.texts(in: box.window)
        XCTAssertTrue(texts.contains("Setting up decryption was canceled: nothing was changed."), "\(texts)")
    }

    // MARK: Why status couldn't tell (f)

    private func unknown(reason: String?, message: String?, signedIn: Bool? = nil, legacy: Bool? = false) -> AppStoreStatusRecord.IPATool {
        .init(path: "/i", present: true, version: "2.8.0", signedIn: signedIn, warning: nil,
              legacySignin: legacy, unknownReason: reason, unknownMessage: message)
    }

    /// Status couldn't tell whether ipatool is signed in: the row shows the
    /// CLI's own sentence (`unknown_message`), whatever the reason, a new one
    /// ("keychain-locked") included; without one, this app's words for a
    /// reason it knows, or the reason itself.
    func test_an_unknown_sign_in_shows_the_clis_sentence_or_says_why_itself() throws {
        typealias S = AppStoreSettingsSection
        let locked = "Your login keychain is locked. Unlock it in Keychain Access, then Refresh."
        XCTAssertEqual(S.unknownLine(unknown(reason: "keychain-locked", message: locked)), locked)
        XCTAssertEqual(S.unknownLine(unknown(reason: CLIContract.UnknownSignIn.keychainNoAnswer, message: "the CLI's words")),
                       "the CLI's words")
        XCTAssertTrue(S.unknownLine(unknown(reason: "keychain-locked", message: nil))?.contains("(keychain-locked)") == true)
        XCTAssertTrue(S.unknownLine(unknown(reason: CLIContract.UnknownSignIn.keychainNoAnswer, message: nil))?
            .contains("login keychain didn't answer") == true)
        XCTAssertTrue(S.unknownLine(unknown(reason: CLIContract.UnknownSignIn.noVerdict, message: nil))?
            .contains("/i auth info") == true)
        XCTAssertNil(S.unknownLine(unknown(reason: nil, message: nil)))
        XCTAssertNil(S.unknownLine(unknown(reason: "keychain-locked", message: locked, legacy: true)), "legacy says its own")
        XCTAssertNil(S.unknownLine(unknown(reason: nil, message: "x", signedIn: true)), "only when status couldn't tell")

        let line = #"{"type":"appstore-status","ipatool":{"path":"/i","present":true,"version":"2.8.0","signed_in":null,"email":null,"legacy_signin":false,"unknown_reason":"keychain-locked","unknown_message":"Your login keychain is locked. Unlock it in Keychain Access, then Refresh.","warning":null,"source":"bundled"},"unfairplay":{"path":"/u","present":true,"version":"v0.4.0","enrolled":true,"model_dir":"/m"},"setup_commands":[]}"#
        guard case let .appStoreStatus(record)? = line.buildEvents().first else { return XCTFail("did not decode") }
        XCTAssertEqual(record.ipatool.unknownReason, "keychain-locked")
        let row = try XCTUnwrap(S.rows(for: record).first)
        XCTAssertEqual(row.warning, locked, "the row shows the CLI's sentence")
        XCTAssertFalse(row.ok)
        XCTAssertEqual(row.detail, "2.8.0 · couldn't tell whether it's signed in")
    }

    // MARK: Each state's row and buttons (delta audit try13, the critic's gap on 406e4fb)

    /// One case of the ipatool row's truth table.
    private struct Case {
        let name: String
        let ipatool: AppStoreStatusRecord.IPATool
        let ok: Bool
        let detail: String
        /// A fragment the row's warning holds, or nil for none at all.
        let warning: String?
        let buttons: [AppStoreSettingsSection.AccountButton]
    }

    private static let lockedMessage = "Your login keychain is locked. Unlock it in Keychain Access, then Refresh."

    private static func ipa(signedIn: Bool?, email: String? = nil, legacy: Bool? = false,
                            reason: String? = nil, message: String? = nil, present: Bool = true,
                            source: String? = nil) -> AppStoreStatusRecord.IPATool {
        .init(path: "/i", present: present, version: present ? "2.8.0" : nil, signedIn: signedIn, warning: nil,
              email: email, legacySignin: legacy, source: source, unknownReason: reason, unknownMessage: message)
    }

    private static let cases: [Case] = [
        Case(name: "signed in", ipatool: ipa(signedIn: true, email: "someone@example.invalid"),
             ok: true, detail: "2.8.0 · signed in as someone@example.invalid", warning: nil,
             buttons: [.signInAgain, .signOut]),
        Case(name: "signed out", ipatool: ipa(signedIn: false),
             ok: false, detail: "2.8.0 · not signed in", warning: nil, buttons: [.signIn]),
        // Only Graft.app's own ipatool guesses Graft 1 (delta audit try15,
        // d15-app-1): any other, or a CLI too old to name its source, says
        // what the CLI and the sign-in sheet say.
        Case(name: "legacy, Graft's own ipatool",
             ipatool: ipa(signedIn: nil, legacy: true, source: CLIContract.ToolSource.bundled),
             ok: false, detail: legacyDetail, warning: nil, buttons: [.signIn]),
        Case(name: "legacy, config.yaml's ipatool",
             ipatool: ipa(signedIn: nil, legacy: true, source: CLIContract.ToolSource.config),
             ok: false, detail: otherLegacyDetail, warning: nil, buttons: [.signIn]),
        Case(name: "legacy, no source named", ipatool: ipa(signedIn: nil, legacy: true),
             ok: false, detail: otherLegacyDetail, warning: nil, buttons: [.signIn]),
        Case(name: "no verdict", ipatool: ipa(signedIn: nil, reason: CLIContract.UnknownSignIn.noVerdict),
             ok: false, detail: "2.8.0 · couldn't tell whether it's signed in", warning: "Refresh", buttons: []),
        Case(name: "keychain no answer", ipatool: ipa(signedIn: nil, reason: CLIContract.UnknownSignIn.keychainNoAnswer),
             ok: false, detail: "2.8.0 · couldn't tell whether it's signed in", warning: "Refresh", buttons: []),
        Case(name: "locked", ipatool: ipa(signedIn: nil, reason: "keychain-locked", message: lockedMessage),
             ok: false, detail: "2.8.0 · couldn't tell whether it's signed in", warning: lockedMessage, buttons: []),
        Case(name: "no ipatool", ipatool: ipa(signedIn: false, present: false),
             ok: false, detail: "not found at /i", warning: nil, buttons: []),
    ]

    /// The ipatool row and the buttons beside it, state by state: signed in,
    /// signed out, legacy, couldn't tell (each reason), locked and missing;
    /// then the same states while status is read again. A state with no
    /// verdict offers no account button, because Sign In… would ask for a
    /// password and a code for nothing; its line says to Refresh, which is
    /// the section's own button.
    @MainActor
    func test_each_sign_in_state_has_its_row_and_its_buttons() {
        typealias S = AppStoreSettingsSection
        for c in Self.cases {
            let status = AppStoreStatusRecord(
                ipatool: c.ipatool,
                unfairplay: .init(path: "/u", present: true, version: "v0.4.0", enrolled: true, modelDir: "/m"),
                setupCommands: []
            )
            let row = S.rows(for: status)[0]
            XCTAssertEqual(row.ok, c.ok, c.name)
            XCTAssertEqual(row.detail, c.detail, c.name)
            XCTAssertFalse(row.checking, c.name)
            if let fragment = c.warning {
                XCTAssertTrue(row.warning?.contains(fragment) == true, "\(c.name): \(row.warning ?? "nil")")
            } else {
                XCTAssertNil(row.warning, c.name)
            }
            XCTAssertEqual(S.accountButtons(for: status), c.buttons, c.name)
            XCTAssertEqual(S.enrollAction(status), .setUpAgain, c.name)
            XCTAssertTrue(S.accountActionsEnabled(checking: false, blockedReason: nil), c.name)

            // While status is read again: both rows Checking…, nothing of
            // this answer, and no button that can be pressed.
            let checking = S.rows(for: status, checking: true)
            XCTAssertEqual(checking.map(\.detail), [S.checkingDetail, S.checkingDetail], c.name)
            XCTAssertEqual(checking.map(\.ok), [false, false], c.name)
            XCTAssertEqual(checking.map(\.warning), [nil, nil], c.name)
            XCTAssertFalse(S.accountActionsEnabled(checking: true, blockedReason: nil), c.name)
        }
        // A get or Get (free) running holds them too, checking or not.
        XCTAssertFalse(S.accountActionsEnabled(checking: false, blockedReason: AppStoreStore.getRunningReason))
        XCTAssertFalse(S.accountActionsEnabled(checking: true, blockedReason: AppStoreStore.getRunningReason))
    }

    // MARK: A configured path that gave way to Graft.app's own (delta audit try13, #7)

    static let ipatoolNote = "appstore.ipatool in config.yaml is /opt/example/bin/ipatool, which isn't there any more, so Graft.app's own ipatool runs instead; remove appstore.ipatool from config.yaml to stop this note"
    static let unfairplayNote = "appstore.unfairplay in config.yaml is /opt/example/bin/unfairplay, which isn't there any more, so Graft.app's own UnfairPlay runs instead; remove appstore.unfairplay from config.yaml to stop this note"

    /// `appstore-status` carries `ipatool.note` and `unfairplay.note`: a
    /// configured path that isn't there any more, so Graft.app's own copy ran.
    /// The CLI used to say so only in a `log` line, which the app drops, so an
    /// app user never learned the key was being ignored, or that a reinstall
    /// at that path would take over again. Each row shows its own note under
    /// itself, as it shows `unknown_message`.
    func test_each_rows_note_from_status_is_shown_under_it() throws {
        let line = #"{"type":"appstore-status","ipatool":{"path":"/Applications/Graft.app/Contents/Helpers/bin/ipatool","source":"bundled","present":true,"version":"2.8.0","signed_in":true,"email":"someone@example.invalid","legacy_signin":false,"unknown_reason":null,"unknown_message":null,"warning":null,"install":null,"note":"appstore.ipatool in config.yaml is /opt/example/bin/ipatool, which isn't there any more, so Graft.app's own ipatool runs instead; remove appstore.ipatool from config.yaml to stop this note"},"unfairplay":{"path":"/Applications/Graft.app/Contents/Helpers/bin/unfairplay","source":"bundled","present":true,"version":"v0.4.0","enrolled":true,"model_dir":"/m","model_problem":null,"install":null,"note":"appstore.unfairplay in config.yaml is /opt/example/bin/unfairplay, which isn't there any more, so Graft.app's own UnfairPlay runs instead; remove appstore.unfairplay from config.yaml to stop this note"},"setup_commands":[]}"#
        guard case let .appStoreStatus(record)? = line.buildEvents().first else { return XCTFail("did not decode") }
        XCTAssertEqual(record.ipatool.note, Self.ipatoolNote)
        XCTAssertEqual(record.unfairplay.note, Self.unfairplayNote)

        let rows = AppStoreSettingsSection.rows(for: record)
        XCTAssertEqual(rows[0].warning, Self.ipatoolNote, "under the ipatool row")
        XCTAssertEqual(rows[1].warning, Self.unfairplayNote, "under the UnfairPlay row")
        XCTAssertTrue(rows[0].ok, "a note is no failure: the bundled copy runs, signed in")
        XCTAssertTrue(rows[1].ok)
        XCTAssertEqual(rows[0].detail, "2.8.0 · signed in as someone@example.invalid")

        // Checking says nothing of the last answer, its notes included.
        XCTAssertEqual(AppStoreSettingsSection.rows(for: record, checking: true).map(\.warning), [nil, nil])
    }

    /// Null, or missing (a CLI too old to send it), decodes as no note, and
    /// the rows say nothing more than they did.
    func test_a_null_or_missing_note_is_no_note() throws {
        let null = #"{"type":"appstore-status","ipatool":{"path":"/i","present":true,"version":"2.8.0","signed_in":true,"warning":null,"note":null},"unfairplay":{"path":"/u","present":true,"version":"v0.4.0","enrolled":true,"model_dir":"/m","note":null},"setup_commands":[]}"#
        let missing = #"{"type":"appstore-status","ipatool":{"path":"/i","present":true,"version":"2.8.0","signed_in":true,"warning":null},"unfairplay":{"path":"/u","present":true,"version":"v0.4.0","enrolled":true,"model_dir":"/m"},"setup_commands":[]}"#
        for line in [null, missing] {
            guard case let .appStoreStatus(record)? = line.buildEvents().first else { return XCTFail("did not decode: \(line)") }
            XCTAssertNil(record.ipatool.note)
            XCTAssertNil(record.unfairplay.note)
            XCTAssertEqual(AppStoreSettingsSection.rows(for: record).map(\.warning), [nil, nil])
        }
    }

    /// A note joins what the row already says, one thing per line: the 2.8.1
    /// warning and why status couldn't tell on the ipatool row, the model's
    /// problem on UnfairPlay's. An empty note adds no blank line.
    func test_a_note_joins_the_rows_other_warnings_one_per_line() {
        let broken = "ipatool 2.8.1 can't download"
        let locked = "Your login keychain is locked. Unlock it in Keychain Access, then Refresh."
        let s = AppStoreStatusRecord(
            ipatool: .init(path: "/i", present: true, version: "2.8.1", signedIn: nil, warning: broken,
                           legacySignin: false, unknownReason: "keychain-locked", unknownMessage: locked,
                           note: Self.ipatoolNote),
            unfairplay: .init(path: "/u", present: true, version: "v0.4.0", enrolled: false, modelDir: "/m",
                              modelProblem: "belongs to root", note: Self.unfairplayNote),
            setupCommands: []
        )
        let rows = AppStoreSettingsSection.rows(for: s)
        XCTAssertEqual(rows[0].warning, [broken, locked, Self.ipatoolNote].joined(separator: "\n"))
        XCTAssertEqual(rows[1].warning, ["belongs to root", Self.unfairplayNote].joined(separator: "\n"))

        let empty = AppStoreStatusRecord(
            ipatool: .init(path: "/i", present: true, version: "2.8.0", signedIn: true, warning: nil, note: ""),
            unfairplay: .init(path: "/u", present: true, version: "v0.4.0", enrolled: true, modelDir: "/m",
                              modelProblem: "belongs to root", note: ""),
            setupCommands: []
        )
        XCTAssertEqual(AppStoreSettingsSection.rows(for: empty).map(\.warning), [nil, "belongs to root"])
    }
}
