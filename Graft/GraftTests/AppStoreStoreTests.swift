import XCTest
@testable import Graft

/// `AppStoreStore`: the GUI's half of getting an app from the App Store.
///
/// Every runner here is a real executable — a `/bin/sh` script that logs its
/// argv and prints canned events — so the argv under test is the one
/// production builds, not a fixture standing in for it (see memory
/// `empty-collection-is-not-a-load-state`, on why the store has no
/// `argsOverride`).
final class AppStoreStoreTests: XCTestCase {
    /// A script that appends its argv to a log file, then runs `body`.
    private func scriptRunner(_ body: String) throws -> (CLIRunner, URL) {
        let dir = FileManager.default.temporaryDirectory
            .appending(path: "appstore_store_\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let log = dir.appending(path: "argv.log")
        let script = dir.appending(path: "tb.sh")
        try """
        #!/bin/sh
        echo "$*" >> '\(log.path)'
        \(body)
        """.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
        return (CLIRunner(executable: script), log)
    }

    private func argv(_ log: URL) throws -> [String] {
        try String(contentsOf: log, encoding: .utf8)
            .split(separator: "\n").map(String.init)
    }

    private static let statusLine = #"{"type":"appstore-status","ipatool":{"path":"/i","present":true,"version":"2.8.0","signed_in":true,"warning":null},"unfairplay":{"path":"/u","present":true,"version":"dev","enrolled":true,"model_dir":"/m"},"setup_commands":[]}"#

    // MARK: status

    @MainActor
    func test_status_asks_for_json_and_the_verb() async throws {
        let (runner, log) = try scriptRunner("printf '%s\\n' '\(Self.statusLine)'")
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())

        await store.loadStatus()

        XCTAssertEqual(try argv(log), ["--json --appstore-status"])
        XCTAssertEqual(store.status?.ipatool.version, "2.8.0")
        XCTAssertTrue(store.hasLoadedStatus)
        XCTAssertNil(store.statusError)
    }

    /// The house defect, in this store's terms. A failed load is a *loaded*
    /// state with an error — not "still checking" forever, and not a reason
    /// for the next appearance of the pane to spawn the CLI again.
    @MainActor
    func test_a_failed_status_load_is_loaded_with_an_error() async throws {
        let (runner, _) = try scriptRunner("echo 'error: No such option: --appstore-status' >&2; exit 2")
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        XCTAssertFalse(store.hasLoadedStatus)

        await store.loadStatus()

        XCTAssertTrue(store.hasLoadedStatus)
        XCTAssertNil(store.status)
        XCTAssertNotNil(store.statusError)
    }

    @MainActor
    func test_a_reload_that_fails_drops_the_old_answer() async throws {
        // A status that says "signed in" from before the failure would be a
        // claim nothing currently supports.
        let flag = FileManager.default.temporaryDirectory
            .appending(path: "appstore_fail_\(UUID().uuidString)")
        addTeardownBlock { try? FileManager.default.removeItem(at: flag) }
        let (runner, _) = try scriptRunner("""
        if [ -e '\(flag.path)' ]; then exit 1; fi
        printf '%s\\n' '\(Self.statusLine)'
        """)
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        await store.loadStatus()
        XCTAssertNotNil(store.status)

        FileManager.default.createFile(atPath: flag.path, contents: Data())
        await store.loadStatus()

        XCTAssertNil(store.status)
        XCTAssertNotNil(store.statusError)
    }

    // MARK: version check

    @MainActor
    func test_check_asks_about_one_app_and_keys_the_answer_by_it() async throws {
        let line = #"{"type":"appstore-version","app":"example","bundle_id":"com.example.app","store_version":"21.46.3","cached_version":"21.40.0","update_available":true,"error":null}"#
        let (runner, log) = try scriptRunner("printf '%s\\n' '\(line)'")
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())

        await store.checkVersion(app: "example")

        XCTAssertEqual(try argv(log), ["--json --appstore-check --app example"])
        XCTAssertEqual(store.versions["example"]?.storeVersion, "21.46.3")
    }

    @MainActor
    func test_a_check_the_cli_could_not_run_is_an_answer_with_an_error() async throws {
        // An older CLI without the verb, or no network: the row says it could
        // not tell, rather than showing nothing (which reads as "not asked").
        let (runner, _) = try scriptRunner("echo 'error: offline' >&2; exit 13")
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())

        await store.checkVersion(app: "example")

        let record = try XCTUnwrap(store.versions["example"])
        XCTAssertNil(record.storeVersion)
        XCTAssertNotNil(record.error)
        XCTAssertFalse(record.updateAvailable)
    }

    // MARK: get

    private static let getScript = """
    printf '%s\\n' '{"type":"appstore-stage","app":"example","stage":"download"}'
    sleep 1
    printf '%s\\n' '{"type":"appstore-stage","app":"example","stage":"decrypt"}'
    printf '%s\\n' '{"type":"appstore-decrypted","app":"example","encrypted_images":8,"decrypted_pages":5,"plaintext_verified":true}'
    printf '%s\\n' '{"type":"appstore-stage","app":"example","stage":"stage"}'
    printf '%s\\n' '{"type":"ipa-provided","app":"example","bundle_id":"com.example.app","version":"21.46.3","filename":"com.example.app_21.46.3.ipa","path":"/c/com.example.app_21.46.3.ipa","size_bytes":10}'
    """

    @MainActor
    private func waitUntil(_ condition: () -> Bool, timeout: TimeInterval = 5) async {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition(), Date() < deadline {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    @MainActor
    func test_get_publishes_each_stage_as_it_arrives_then_clears_it() async throws {
        let (runner, log) = try scriptRunner(Self.getScript)
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())

        XCTAssertTrue(store.startGet(app: "example"))
        await waitUntil { store.stages["example"] == .download }
        XCTAssertEqual(store.stages["example"], .download, "the first stage is visible while the CLI runs")
        XCTAssertTrue(store.isGetting("example"))

        await waitUntil { store.completions["example"] != nil }

        XCTAssertEqual(store.completions["example"], 1)
        XCTAssertNil(store.stages["example"])
        XCTAssertFalse(store.isGetting("example"))
        XCTAssertEqual(try argv(log).first, "--json --appstore-get --app example")
    }

    @MainActor
    func test_a_failed_get_records_the_clis_message_and_clears_the_stage() async throws {
        let (runner, _) = try scriptRunner("""
        printf '%s\\n' '{"type":"appstore-stage","app":"example","stage":"download"}'
        echo "error: this Apple ID doesn't own com.example.app yet" >&2
        echo "  hint: tap Get for this app on an iPhone or iPad" >&2
        exit 20
        """)
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())

        XCTAssertTrue(store.startGet(app: "example"))
        await waitUntil { !store.isBusy("example") }

        let message = try XCTUnwrap(store.errors["example"])
        XCTAssertTrue(message.contains("doesn't own"), message)
        XCTAssertTrue(message.contains("tap Get"), message)
        XCTAssertNil(store.stages["example"])
        XCTAssertFalse(store.isGetting("example"))
    }

    /// `onEvent` hops to the MainActor and can land after the call returned.
    /// A stage arriving then must not put "Downloading…" back on a row whose
    /// get has finished.
    @MainActor
    func test_a_stage_for_a_get_that_is_not_running_is_ignored() {
        let store = AppStoreStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/true")), shell: NoAdminShell())

        store.note(.appStoreStage(AppStoreStageRecord(app: "example", stage: .download)), forGetOf: "example")

        XCTAssertNil(store.stages["example"])
    }

    // MARK: ConfigStore.provideIPA(onStage:)

    @MainActor
    func test_a_drop_reports_decrypting_when_the_cli_says_so() async throws {
        let store = ConfigStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")), notify: { _ in })
        let script = """
        printf '%s\\n' '{"type":"appstore-stage","app":"example","stage":"decrypt"}'
        sleep 1
        printf '%s\\n' '{"type":"ipa-provided","app":"example","bundle_id":"b","version":"1","filename":"b_1.ipa","path":"/c/b_1.ipa","size_bytes":1}'
        """
        var seen: [AppStoreStage] = []

        let record = try await store.provideIPA(
            appName: "example", fileURL: URL(fileURLWithPath: "/tmp/x.ipa"),
            argsOverride: ["-c", script], onStage: { seen.append($0) }
        )

        XCTAssertEqual(record?.record.filename, "b_1.ipa")
        XCTAssertEqual(record?.decrypted, false, "a decrypt stage alone is not the CLI saying it decrypted")
        XCTAssertEqual(seen, [.decrypt])
    }

    // MARK: "Use it anyway…" (C-UNVERIFIED)

    /// A fake CLI that refuses an unverified decrypt the way the real one does
    /// — the `appstore-refused` event, then `_fatal`'s Terminal hint, exit 20 —
    /// unless it was given `--allow-unverified`, when it warns and stages.
    private static let unverifiedScript = """
    case " $* " in
      *" --appstore-check "*)
        printf '%s\\n' '{"type":"appstore-version","app":"example","bundle_id":"com.example.app","store_version":"1.0","cached_version":"1.0","update_available":false,"error":null}'
        exit 0 ;;
      *" --allow-unverified "*)
        printf '%s\\n' '{"type":"appstore-stage","app":"example","stage":"decrypt"}'
        printf '%s\\n' '{"type":"log","source":"warning","line":"example: staging a decrypt UnfairPlay could not verify against Apple'"'"'s page hashes (--allow-unverified)"}'
        printf '%s\\n' '{"type":"appstore-decrypted","app":"example","encrypted_images":1,"decrypted_pages":4,"plaintext_verified":false}'
        printf '%s\\n' '{"type":"ipa-provided","app":"example","bundle_id":"com.example.app","version":"1.0","filename":"com.example.app_1.0.ipa","path":"/c/com.example.app_1.0.ipa","size_bytes":1}'
        exit 0 ;;
    esac
    printf '%s\\n' '{"type":"appstore-stage","app":"example","stage":"decrypt"}'
    printf '%s\\n' '{"type":"appstore-refused","app":"example","reason":"unverified"}'
    echo "error: UnfairPlay decrypted encrypted.ipa but couldn't verify it against Apple's own page hashes (PlaintextVerified: false) — refusing to stage it" >&2
    echo "  hint: older apps without those hashes legitimately report this. To accept it anyway, run in Terminal: graft --appstore-get --app example --allow-unverified" >&2
    exit 20
    """

    @MainActor
    func test_an_unverified_get_offers_use_it_anyway_in_the_apps_own_words() async throws {
        let (runner, _) = try scriptRunner(Self.unverifiedScript)
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())

        XCTAssertTrue(store.startGet(app: "example"))
        await waitUntil { !store.isBusy("example") }

        let message = try XCTUnwrap(store.errors["example"])
        XCTAssertEqual(message, UnverifiedDecrypt.message)
        XCTAssertFalse(message.contains("Terminal"), "the CLI's hint is for a Terminal user")
        XCTAssertEqual(store.unverifiedRetry["example"], .get)
    }

    @MainActor
    func test_use_it_anyway_reruns_the_get_with_allow_unverified_and_keeps_the_warning() async throws {
        let (runner, log) = try scriptRunner(Self.unverifiedScript)
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        let config = ConfigStore(runner: runner, notify: { _ in })
        store.startGet(app: "example")
        await waitUntil { !store.isBusy("example") }

        XCTAssertTrue(store.useUnverified(app: "example", config: config))
        await waitUntil { store.completions["example"] != nil }

        XCTAssertEqual(
            Array(try argv(log).prefix(2)),
            ["--json --appstore-get --app example",
             "--json --appstore-get --app example --allow-unverified"]
        )
        XCTAssertNil(store.errors["example"])
        XCTAssertNil(store.unverifiedRetry["example"], "an offer is used once")
        let staged = try XCTUnwrap(store.staged["example"])
        XCTAssertEqual(staged.warnings.count, 1)
        XCTAssertTrue(staged.warnings[0].contains("could not verify"), "\(staged.warnings)")
    }

    @MainActor
    func test_use_it_anyway_reruns_the_same_drop() async throws {
        let (runner, log) = try scriptRunner(Self.unverifiedScript)
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        let config = ConfigStore(runner: runner, notify: { _ in })
        let file = URL(fileURLWithPath: "/tmp/Example.ipa")
        store.startProvide(app: "example", fileURL: file, config: config)
        await waitUntil { !store.isBusy("example") }
        XCTAssertEqual(store.errors["example"], UnverifiedDecrypt.message)
        XCTAssertEqual(store.unverifiedRetry["example"], .drop(file))

        XCTAssertTrue(store.useUnverified(app: "example", config: config))
        await waitUntil { store.completions["example"] != nil }

        XCTAssertEqual(
            Array(try argv(log).prefix(2)),
            ["--json --provide-ipa /tmp/Example.ipa --app example",
             "--json --provide-ipa /tmp/Example.ipa --app example --allow-unverified"]
        )
        XCTAssertEqual(store.staged["example"]?.warnings.count, 1)
    }

    /// Only the unverified refusal has a button behind it. Every other failure
    /// keeps the CLI's own message and offers nothing.
    @MainActor
    func test_other_failures_offer_no_retry() async throws {
        let (runner, _) = try scriptRunner("""
        echo "error: this Apple ID doesn't own com.example.app yet" >&2
        exit 20
        """)
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        let config = ConfigStore(runner: runner, notify: { _ in })

        store.startGet(app: "example")
        await waitUntil { !store.isBusy("example") }

        XCTAssertNotNil(store.errors["example"])
        XCTAssertNil(store.unverifiedRetry["example"])
        XCTAssertFalse(store.useUnverified(app: "example", config: config))
    }

    @MainActor
    func test_dismissing_the_error_withdraws_the_offer() async throws {
        let (runner, _) = try scriptRunner(Self.unverifiedScript)
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        store.startGet(app: "example")
        await waitUntil { !store.isBusy("example") }

        store.dismissError(app: "example")

        XCTAssertNil(store.unverifiedRetry["example"])
    }

    // MARK: Add App's IPA (`AddAppSheet.add`)

    /// `unverifiedScript` behind a CLI that also answers Add App: the
    /// `app-added` event, and an empty `--list` for the reload after it.
    private static let addThenUnverifiedScript = """
    case " $* " in
      *" --add-app-manual "*)
        printf '%s\\n' '{"type":"app-added","name":"example","bundle_id":"com.example.app","display_name":"Example","delivery":"sideload"}'
        exit 0 ;;
      *" --list "*) exit 0 ;;
    esac
    """ + "\n" + unverifiedScript

    private static let manualAdd = AddAppSheet.Request.manual(
        name: "example", bundleId: "com.example.app", displayName: nil,
        delivery: .sideload, ipa: URL(fileURLWithPath: "/tmp/Example.ipa")
    )

    /// Add App stages its IPA the way a drop on the app pane does. It used to
    /// run its own copy of the refusal → "Use it anyway…" → retry, which threw
    /// the CLI's warning away: the IPA was staged unverified and nothing said
    /// so. Now the refusal is the app pane's offer, and the warning lands on
    /// the app's record under its staged line, as a drop's does.
    @MainActor
    func test_a_sheet_add_used_anyway_keeps_the_warning_on_the_apps_record() async throws {
        let (runner, log) = try scriptRunner(Self.addThenUnverifiedScript)
        let config = ConfigStore(runner: runner, notify: { _ in })
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        let file = URL(fileURLWithPath: "/tmp/Example.ipa")

        let added = try await AddAppSheet.add(Self.manualAdd, store: config, appStore: store)

        XCTAssertEqual(added, "example")
        await waitUntil { store.unverifiedRetry["example"] != nil }
        XCTAssertEqual(store.errors["example"], UnverifiedDecrypt.message, "the app pane's banner")
        XCTAssertEqual(store.unverifiedRetry["example"], .drop(file), "and its Use it anyway…")

        XCTAssertTrue(store.useUnverified(app: "example", config: config))
        await waitUntil { store.completions["example"] != nil }

        XCTAssertEqual(try argv(log).filter { $0.contains("--provide-ipa") }, [
            "--json --provide-ipa /tmp/Example.ipa --app example",
            "--json --provide-ipa /tmp/Example.ipa --app example --allow-unverified",
        ])
        let staged = try XCTUnwrap(store.staged["example"])
        XCTAssertEqual(staged.source, .drop(filename: "Example.ipa"))
        XCTAssertEqual(staged.warnings.count, 1, "\(staged.warnings)")
        XCTAssertTrue(staged.warnings.first?.contains("could not verify") ?? false, "\(staged.warnings)")
    }

    /// Any other failure is the app pane's plain banner, with nothing to
    /// retry — and the app stays added, as it always did.
    @MainActor
    func test_a_sheet_add_whose_ipa_fails_otherwise_offers_nothing() async throws {
        let (runner, _) = try scriptRunner("""
        case " $* " in
          *" --add-app-manual "*)
            printf '%s\\n' '{"type":"app-added","name":"example","bundle_id":"com.example.app","display_name":"Example","delivery":"sideload"}'
            exit 0 ;;
          *" --list "*) exit 0 ;;
        esac
        echo 'error: not an IPA' >&2; exit 2
        """)
        let config = ConfigStore(runner: runner, notify: { _ in })
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())

        let added = try await AddAppSheet.add(Self.manualAdd, store: config, appStore: store)

        XCTAssertEqual(added, "example")
        await waitUntil { store.errors["example"] != nil }
        XCTAssertTrue(store.errors["example"]?.contains("not an IPA") ?? false, "\(String(describing: store.errors))")
        XCTAssertNil(store.unverifiedRetry["example"])
        XCTAssertNil(store.staged["example"])
    }

    /// No IPA chosen, nothing staged: the add is the whole of it.
    @MainActor
    func test_a_sheet_add_without_an_ipa_stages_nothing() async throws {
        let (runner, log) = try scriptRunner(Self.addThenUnverifiedScript)
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())

        let added = try await AddAppSheet.add(
            .manual(name: "example", bundleId: "com.example.app", displayName: nil,
                    delivery: .sideload, ipa: nil),
            store: ConfigStore(runner: runner, notify: { _ in }), appStore: store
        )

        XCTAssertEqual(added, "example")
        XCTAssertFalse(store.isBusy("example"))
        XCTAssertEqual(try argv(log).filter { $0.contains("--provide-ipa") }, [])
    }

    /// A first try never sends the flag, whatever happened before.
    @MainActor
    func test_a_plain_get_never_allows_unverified() {
        XCTAssertFalse(AppStoreStore.getArgs(app: "example").contains(CLIContract.Flag.allowUnverified))
        XCTAssertEqual(
            AppStoreStore.getArgs(app: "example", allowUnverified: true).last,
            CLIContract.Flag.allowUnverified
        )
    }

    // MARK: A version already staged, and Get Again

    /// A CLI whose App Store version is already staged: a plain get answers
    /// `appstore-already-staged` and nothing else, exit 0, as the real one
    /// does; `--again` downloads and stages, refusing an unverified decrypt
    /// unless `--allow-unverified` came too when `unverified` is set.
    private static func alreadyStagedScript(unverified: Bool = false) -> String {
        let refuse = unverified ? """
            case " $* " in *" --allow-unverified "*) ;; *)
              printf '%s\\n' '{"type":"appstore-refused","app":"example","reason":"unverified"}'
              echo "error: UnfairPlay decrypted encrypted.ipa but couldn't verify it" >&2
              exit 20 ;;
            esac
            """ : ""
        return """
        case " $* " in
          *" --appstore-check "*)
            printf '%s\\n' '{"type":"appstore-version","app":"example","bundle_id":"com.example.app","store_version":"21.46.3","cached_version":"21.46.3","update_available":false,"error":null}'
            exit 0 ;;
          *" --again "*)
            printf '%s\\n' '{"type":"appstore-stage","app":"example","stage":"download"}'
            \(refuse)
            printf '%s\\n' '{"type":"appstore-decrypted","app":"example","encrypted_images":8,"decrypted_pages":5,"plaintext_verified":true}'
            printf '%s\\n' '{"type":"ipa-provided","app":"example","bundle_id":"com.example.app","version":"21.46.3","filename":"com.example.app_21.46.3.ipa","path":"/c/com.example.app_21.46.3.ipa","size_bytes":10}'
            exit 0 ;;
        esac
        printf '%s\\n' '{"type":"appstore-already-staged","app":"example","bundle_id":"com.example.app","version":"21.46.3","filename":"com.example.app_21.46.3.ipa","path":"/c/com.example.app_21.46.3.ipa"}'
        """
    }

    /// The owner's report: a get of the version already staged downloaded it
    /// again. Now the CLI says it has it, and the pane says so too: no error,
    /// no "Staged", no completion (the cache didn't change), and the version
    /// line asked again.
    @MainActor
    func test_a_get_of_the_version_already_staged_says_so_and_stages_nothing() async throws {
        let (runner, log) = try scriptRunner(Self.alreadyStagedScript())
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())

        XCTAssertTrue(store.startGet(app: "example"))
        await waitUntil { !store.isBusy("example") && store.versions["example"] != nil }

        XCTAssertNil(store.errors["example"])
        let staged = try XCTUnwrap(store.staged["example"])
        XCTAssertTrue(staged.alreadyStaged)
        XCTAssertEqual(staged.message, "✓ Already have 21.46.3, the App Store's current version: nothing downloaded")
        XCTAssertNil(store.completions["example"], "nothing was staged")
        XCTAssertNil(store.lastCompleted)
        XCTAssertEqual(try argv(log), ["--json --appstore-get --app example", "--json --appstore-check --app example"])
        XCTAssertEqual(store.versions["example"]?.storeVersion, "21.46.3")
    }

    @MainActor
    func test_get_again_sends_again_and_stages_as_any_get() async throws {
        let (runner, log) = try scriptRunner(Self.alreadyStagedScript())
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())

        XCTAssertTrue(store.startGet(app: "example", again: true))
        await waitUntil { store.completions["example"] != nil }

        XCTAssertEqual(try argv(log).first, "--json --appstore-get --app example --again")
        XCTAssertEqual(store.staged["example"]?.message, "✓ Staged 21.46.3 from the App Store")
        XCTAssertEqual(store.staged["example"]?.alreadyStaged, false)
    }

    /// "Use it anyway…" re-runs the get it answers. After Get Again that is
    /// Get Again: without `--again` the retry would only find the old copy.
    @MainActor
    func test_use_it_anyway_after_get_again_gets_again() async throws {
        let (runner, log) = try scriptRunner(Self.alreadyStagedScript(unverified: true))
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        let config = ConfigStore(runner: runner, notify: { _ in })
        store.startGet(app: "example", again: true)
        await waitUntil { !store.isBusy("example") }
        XCTAssertEqual(store.errors["example"], UnverifiedDecrypt.message)
        XCTAssertEqual(store.unverifiedRetry["example"], .getAgain)

        XCTAssertTrue(store.useUnverified(app: "example", config: config))
        await waitUntil { store.completions["example"] != nil }

        XCTAssertEqual(
            Array(try argv(log).prefix(2)),
            ["--json --appstore-get --app example --again",
             "--json --appstore-get --app example --again --allow-unverified"]
        )
        XCTAssertEqual(store.staged["example"]?.alreadyStaged, false)
    }

    @MainActor
    func test_only_get_again_sends_again() {
        XCTAssertFalse(AppStoreStore.getArgs(app: "example").contains(CLIContract.Flag.again))
        XCTAssertFalse(AppStoreStore.getArgs(app: "example", allowUnverified: true).contains(CLIContract.Flag.again))
        XCTAssertTrue(AppStoreStore.getArgs(app: "example", again: true).contains(CLIContract.Flag.again))
    }
}
