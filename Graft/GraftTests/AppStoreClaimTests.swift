import XCTest
@testable import Graft

/// A get the Apple ID has no license for, then Get (free), then the get again,
/// against a stand-in CLI that refuses `--appstore-get` until `--appstore-claim` has run.
final class AppStoreClaimTests: XCTestCase {
    /// `claim` is the stand-in's answer to `--appstore-claim`; by default it
    /// claims, after which the get stages. `getsAfterClaim` false keeps the
    /// get refusing needs-license even then.
    private func cli(price: String, claim: String? = nil, getsAfterClaim: Bool = true) throws -> (CLIRunner, URL) {
        let dir = FileManager.default.temporaryDirectory.appending(path: "claim_\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let file = dir.appending(path: "graft.sh")
        let claimed = getsAfterClaim ? "[ -e '\(dir.path)/claimed' ]" : "false"
        let claim = claim ?? """
            touch '\(dir.path)/claimed'
            printf '%s\\n' '{"type":"appstore-claimed","app":"example","bundle_id":"com.example.app","already_owned":false}'
            """
        try """
        #!/bin/sh
        printf '%s\\n' "$*" >> '\(dir.path)/argv.txt'
        case "$*" in
          *--appstore-get*)
            if \(claimed); then
              printf '%s\\n' '{"type":"appstore-stage","app":"example","stage":"download"}'
            else
              printf '%s\\n' '{"type":"appstore-refused","app":"example","reason":"needs-license","price":\(price),"formatted_price":"Free","track_id":100000001}'
              echo "error: this Apple ID doesn't own com.example.app yet" >&2
              exit 20
            fi ;;
          *--appstore-claim*)
            \(claim) ;;
          *--appstore-status*)
            printf '%s\\n' '{"type":"appstore-status","ipatool":{"path":"/i","present":true,"version":"2.8.0","signed_in":true,"warning":null,"email":"a@example.invalid","legacy_signin":false,"source":"bundled"},"unfairplay":{"path":"/u","present":true,"version":"v0.4.0","enrolled":true,"model_dir":"/m"},"setup_commands":[]}' ;;
          *--appstore-check*)
            printf '%s\\n' '{"type":"appstore-version","app":"example","bundle_id":"com.example.app","store_version":"2.0","cached_version":"2.0","update_available":false,"error":null}' ;;
          *--provide-ipa*)
            if [ -e '\(dir.path)/drop-fails' ]; then
              echo "error: /u/Example.ipa isn't an IPA" >&2
              exit 2
            fi
            printf '%s\\n' '{"type":"ipa-provided","app":"example","bundle_id":"com.example.app","version":"2.0","filename":"com.example.app_2.0.ipa","path":"/c/com.example.app_2.0.ipa","size_bytes":1}' ;;
        esac
        """.write(to: file, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: file.path)
        return (CLIRunner(executable: file), dir)
    }

    /// Get (free) as the row runs it: status read again
    /// (`checkBeforeClaim`), then the confirmation's own Get (free), for the
    /// Apple ID it named. Whether a claim started.
    @MainActor
    private func tryClaim(
        _ store: AppStoreStore, _ expected: Bool, _ message: String = "",
        file: StaticString = #filePath, line: UInt = #line
    ) async {
        await store.checkBeforeClaim(app: "example")
        let started = store.claimConfirmations["example"].map {
            store.startClaim(app: "example", confirmed: $0)
        } ?? false
        XCTAssertEqual(started, expected, message, file: file, line: line)
    }

    private func argv(_ dir: URL) throws -> [String] {
        let file = dir.appending(path: "argv.txt")
        guard FileManager.default.fileExists(atPath: file.path) else { return [] }
        return try String(contentsOf: file, encoding: .utf8).split(separator: "\n").map(String.init)
    }

    @MainActor
    func test_a_free_app_not_owned_offers_get_free_then_claims_then_gets() async throws {
        let (runner, dir) = try cli(price: "0")
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())

        store.startGet(app: "example")
        await eventually { !store.isBusy("example") }
        XCTAssertEqual(store.licenseNeeded["example"]?.isFree, true)
        XCTAssertEqual(store.errors["example"], NeedsLicense.freeMessage)

        await tryClaim(store, true)
        await eventually { (try? self.argv(dir).contains { $0.contains("--appstore-check") }) == true }

        let runs = try argv(dir)
        XCTAssertEqual(runs.filter { !$0.contains("--appstore-check") }, [
            "--json --appstore-get --app example",
            "--json --appstore-status",
            "--json --appstore-claim --app example --expect-email a@example.invalid",
            "--json --appstore-get --app example",
        ], "status is read again before the confirmation, so before the claim, which names its Apple ID")
        XCTAssertNil(store.licenseNeeded["example"])
        XCTAssertNil(store.errors["example"])
    }

    /// Get Again, refused for want of a license, then Get (free): the get
    /// after the claim is Get Again too. A plain get there would find the old
    /// copy staged and download nothing.
    @MainActor
    func test_get_free_after_get_again_gets_again() async throws {
        let (runner, dir) = try cli(price: "0")
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())

        store.startGet(app: "example", again: true)
        await eventually { !store.isBusy("example") }
        XCTAssertEqual(store.licenseNeeded["example"]?.isFree, true)

        await tryClaim(store, true)
        await eventually { (try? self.argv(dir).contains { $0.contains("--appstore-check") }) == true }

        XCTAssertEqual(try argv(dir).filter { !$0.contains("--appstore-check") }, [
            "--json --appstore-get --app example --again",
            "--json --appstore-status",
            "--json --appstore-claim --app example --expect-email a@example.invalid",
            "--json --appstore-get --app example --again",
        ])
    }

    @MainActor
    func test_a_paid_app_is_never_claimed() async throws {
        let (runner, dir) = try cli(price: "2.99")
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        store.startGet(app: "example")
        await eventually { !store.isBusy("example") }
        XCTAssertEqual(store.licenseNeeded["example"]?.isFree, false)
        await tryClaim(store, false)
        XCTAssertNil(store.claimConfirmations["example"], "nothing to confirm")
        // Nor with a confirmation from somewhere else: the claim's own check.
        await store.loadStatus()
        XCTAssertFalse(store.startClaim(app: "example", confirmed: .init(email: "a@example.invalid")))
        XCTAssertFalse(try argv(dir).contains { $0.contains("--appstore-claim") })
    }

    @MainActor
    func test_an_unknown_price_is_never_claimed() async throws {
        let (runner, dir) = try cli(price: "null")
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        store.startGet(app: "example")
        await eventually { !store.isBusy("example") }
        XCTAssertNotNil(store.licenseNeeded["example"])
        await tryClaim(store, false)
        await store.loadStatus()
        XCTAssertFalse(store.startClaim(app: "example", confirmed: .init(email: "a@example.invalid")))
        XCTAssertFalse(try argv(dir).contains { $0.contains("--appstore-claim") })
    }

    @MainActor
    func test_dismissing_the_error_drops_the_licence_button() async throws {
        let (runner, _) = try cli(price: "0")
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        store.startGet(app: "example")
        await eventually { !store.isBusy("example") }
        store.dismissError(app: "example")
        XCTAssertNil(store.licenseNeeded["example"])
    }

    @MainActor
    private func refusedFree(_ store: AppStoreStore) async {
        store.startGet(app: "example")
        await eventually { !store.isBusy("example") }
        XCTAssertEqual(store.licenseNeeded["example"]?.offersClaim, true, "premise: Get (free) is on offer")
    }

    /// Review part 4, #7: the price changed, or the claim's lookup sees it
    /// paid. The row stops calling it free.
    @MainActor
    func test_a_claim_refused_as_paid_offers_open_in_app_store_and_never_claims_again() async throws {
        let (runner, dir) = try cli(price: "0", claim: """
            printf '%s\\n' '{"type":"appstore-refused","app":"example","reason":"paid","price":1.99,"formatted_price":"$1.99","track_id":100000001}'
            echo "error: Example costs $1.99: Graft gets only free apps" >&2
            exit 20
            """)
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        await refusedFree(store)
        await tryClaim(store, true)
        await eventually { !store.isBusy("example") }

        let need = try XCTUnwrap(store.licenseNeeded["example"])
        XCTAssertEqual(need.price, 1.99)
        XCTAssertEqual(need.claim, .refusedPaid)
        XCTAssertEqual(AppStoreSourceRow.primaryAction(license: need), .openInStore)
        XCTAssertEqual(need.storeURL?.absoluteString, "https://apps.apple.com/app/id100000001")
        XCTAssertEqual(store.errors["example"], ClaimRefusedPaid(price: 1.99, formattedPrice: "$1.99", trackId: 100000001).errorDescription)
        XCTAssertTrue(store.errors["example"]?.contains("($1.99)") == true)
        await tryClaim(store, false, "no second claim of a paid app")
        XCTAssertEqual(try argv(dir).filter { $0.contains("--appstore-claim") }.count, 1)
    }

    /// ipatool's own "purchasing paid apps is not supported" reaches the app
    /// as stderr only (exit 20, no event), like any other failed claim. None
    /// is offered again: the row says Open in App Store, keeping the link.
    @MainActor
    func test_a_claim_that_fails_without_an_event_offers_open_in_app_store() async throws {
        let (runner, _) = try cli(price: "0", claim: """
            echo "error: com.example.app isn't free, so ipatool won't get it" >&2
            echo "hint: buy it in the App Store with the same Apple ID" >&2
            exit 20
            """)
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        await refusedFree(store)
        await tryClaim(store, true)
        await eventually { !store.isBusy("example") }

        let need = try XCTUnwrap(store.licenseNeeded["example"])
        XCTAssertEqual(need.claim, .failed)
        XCTAssertEqual(AppStoreSourceRow.primaryAction(license: need), .openInStore)
        XCTAssertEqual(need.trackId, 100000001, "the App Store link survives")
        XCTAssertTrue(store.errors["example"]?.contains("isn't free, so ipatool won't get it") == true, "the CLI's own words")
        await tryClaim(store, false)

        store.dismissError(app: "example")
        XCTAssertEqual(AppStoreSourceRow.primaryAction(license: store.licenseNeeded["example"]), .getLatest,
                       "dismissing starts over from Get latest")
    }

    /// Review part 4, #9: the claim says the Apple ID already owns it and the
    /// get still wants a license. No loop back to "It's free: Get (free)".
    @MainActor
    func test_already_owned_and_still_refused_says_so_and_offers_no_second_claim() async throws {
        let (runner, dir) = try cli(price: "0", claim: """
            printf '%s\\n' '{"type":"appstore-claimed","app":"example","bundle_id":"com.example.app","already_owned":true}'
            """, getsAfterClaim: false)
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        await refusedFree(store)
        await tryClaim(store, true)
        await eventually { (try? self.argv(dir).filter { $0.contains("--appstore-get") }.count) == 2 && !store.isBusy("example") }

        XCTAssertEqual(store.errors["example"], NeedsLicense.ownedButRefusedMessage)
        XCTAssertNotEqual(store.errors["example"], NeedsLicense.freeMessage)
        XCTAssertEqual(store.licenseNeeded["example"]?.claim, .alreadyOwned)
        XCTAssertEqual(AppStoreSourceRow.primaryAction(license: store.licenseNeeded["example"]), .openInStore)
        await tryClaim(store, false)
        XCTAssertTrue(NeedsLicense.ownedButRefusedMessage.contains("already owns"))
    }

    /// Review part 4, fix-check #14 (Ask to Buy): a claim that said it worked,
    /// not already owned, then a get still refused for want of a license.
    /// Apple took the request but the app isn't on the Apple ID, as when a
    /// parent hasn't approved it yet. "It's free: Get (free)" again would be
    /// false, and each confirm could send the parent another request, so the
    /// row says so and offers Open in App Store. Dismissing the banner starts
    /// over from Get latest, whose refusal offers Get (free) once more.
    @MainActor
    func test_a_claim_that_worked_then_a_refused_get_says_a_parent_may_need_to_approve_it() async throws {
        let (runner, dir) = try cli(price: "0", getsAfterClaim: false)
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        await refusedFree(store)
        await tryClaim(store, true)
        await eventually { (try? self.argv(dir).filter { $0.contains("--appstore-get") }.count) == 2 && !store.isBusy("example") }

        XCTAssertEqual(store.errors["example"], NeedsLicense.requestedButRefusedMessage)
        XCTAssertNotEqual(store.errors["example"], NeedsLicense.freeMessage)
        XCTAssertEqual(store.licenseNeeded["example"]?.claim, .requested)
        XCTAssertEqual(store.licenseNeeded["example"]?.trackId, 100000001, "the App Store link")
        XCTAssertEqual(AppStoreSourceRow.primaryAction(license: store.licenseNeeded["example"]), .openInStore)
        await tryClaim(store, false, "no second request from the row")
        XCTAssertEqual(try argv(dir).filter { $0.contains("--appstore-claim") }.count, 1)
        for words in ["accepted", "isn't on this Apple ID yet", "Ask to Buy", "parent"] {
            XCTAssertTrue(NeedsLicense.requestedButRefusedMessage.contains(words), words)
        }

        store.dismissError(app: "example")
        await refusedFree(store)
    }

    /// When `.requested` clears (delta audit try13, the critic's gap on
    /// 74cec17). Not on its own: re-checking the App Store, or the row's
    /// version refresh, leaves it, and the row offers no second request.
    /// Once a parent has approved, dismissing the banner brings back Get
    /// latest, and that get goes through. An account switch clears it too
    /// (`forgetLicences`, covered by the account tests below).
    @MainActor
    func test_after_ask_to_buy_is_approved_dismissing_the_banner_brings_back_get_latest_and_it_works() async throws {
        // The claim is accepted but adds nothing: the get keeps refusing
        // until the test approves it (the `claimed` file).
        let (runner, dir) = try cli(price: "0", claim: """
            printf '%s\\n' '{"type":"appstore-claimed","app":"example","bundle_id":"com.example.app","already_owned":false}'
            """)
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        await refusedFree(store)
        await tryClaim(store, true)
        await eventually { (try? self.argv(dir).filter { $0.contains("--appstore-get") }.count) == 2 && !store.isBusy("example") }
        XCTAssertEqual(store.licenseNeeded["example"]?.claim, .requested, "premise")

        await store.checkVersion(app: "example", force: true)
        XCTAssertEqual(store.licenseNeeded["example"]?.claim, .requested, "a version check doesn't clear it")
        XCTAssertEqual(store.errors["example"], NeedsLicense.requestedButRefusedMessage)
        XCTAssertEqual(AppStoreSourceRow.primaryAction(license: store.licenseNeeded["example"]), .openInStore)
        await tryClaim(store, false)

        // A parent approves; the user closes the banner.
        try Data().write(to: dir.appending(path: "claimed"))
        store.dismissError(app: "example")
        XCTAssertNil(store.licenseNeeded["example"])
        XCTAssertNil(store.errors["example"])
        XCTAssertEqual(AppStoreSourceRow.primaryAction(license: store.licenseNeeded["example"]), .getLatest)

        XCTAssertTrue(store.startGet(app: "example"))
        await eventually { !store.isBusy("example") }
        XCTAssertNil(store.errors["example"], "the get after approval goes through")
        XCTAssertNil(store.licenseNeeded["example"])
        XCTAssertEqual(store.completions["example"], 1)
        XCTAssertEqual(try argv(dir).filter { $0.contains("--appstore-claim") }.count, 1, "no second request")
    }

    @MainActor
    func test_stop_ends_a_claim_without_an_error_or_a_get() async throws {
        let (runner, dir) = try cli(price: "0", claim: "exec /bin/sleep 30")
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        await refusedFree(store)
        await tryClaim(store, true)
        XCTAssertTrue(store.isClaiming("example"))
        store.stopGet(app: "example")
        await eventually { !store.isBusy("example") }
        XCTAssertNil(store.errors["example"], "the user asked for it")
        XCTAssertEqual(store.licenseNeeded["example"]?.offersClaim, true, "still on offer, as before")
        XCTAssertEqual(try argv(dir).filter { $0.contains("--appstore-get") }.count, 1, "no get after a stopped claim")
    }

    /// Review part 4, #11: a claim stages nothing, so a drop refused during
    /// one says what is really running.
    @MainActor
    func test_a_drop_during_a_claim_says_a_claim_is_running() async throws {
        let (runner, _) = try cli(price: "0", claim: "exec /bin/sleep 30")
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        await refusedFree(store)
        await tryClaim(store, true)
        let config = ConfigStore(runner: runner, notify: { _ in })
        XCTAssertFalse(store.startProvide(app: "example", fileURL: URL(fileURLWithPath: "/u/Example.ipa"), config: config))
        XCTAssertEqual(store.errors["example"], AppStoreStore.busyRefusal(.claiming))
        XCTAssertTrue(store.errors["example"]?.contains("Get (free)") == true)
        XCTAssertFalse(store.errors["example"]?.contains("another IPA") == true)
        store.stopGet(app: "example")
        await eventually { !store.isBusy("example") }
    }

    /// Whole-diff review, get-1: a dropped IPA that stages ends the license
    /// question for the copy in the cache, so the row's Get (free) or Open in
    /// App Store gives way to Get latest again. A drop that fails staged
    /// nothing, and keeps them.
    @MainActor
    func test_a_drop_that_stages_brings_back_get_latest_and_one_that_fails_does_not() async throws {
        for (price, before) in [("0", AppStoreSourceRow.PrimaryAction.claimFree), ("2.99", .openInStore)] {
            let (runner, dir) = try cli(price: price)
            let store = AppStoreStore(runner: runner, shell: NoAdminShell())
            let config = ConfigStore(runner: runner, notify: { _ in })
            let drop = URL(fileURLWithPath: "/u/Example.ipa")
            store.startGet(app: "example")
            await eventually { !store.isBusy("example") }
            XCTAssertEqual(AppStoreSourceRow.primaryAction(license: store.licenseNeeded["example"]), before,
                           "premise, price \(price)")

            try Data().write(to: dir.appending(path: "drop-fails"))
            XCTAssertTrue(store.startProvide(app: "example", fileURL: drop, config: config))
            await eventually { !store.isBusy("example") }
            XCTAssertNotNil(store.errors["example"], "premise: the drop failed")
            XCTAssertEqual(AppStoreSourceRow.primaryAction(license: store.licenseNeeded["example"]), before,
                           "a drop that staged nothing changes nothing, price \(price)")

            try FileManager.default.removeItem(at: dir.appending(path: "drop-fails"))
            XCTAssertTrue(store.startProvide(app: "example", fileURL: drop, config: config))
            await eventually { !store.isBusy("example") }
            XCTAssertEqual(store.completions["example"], 1, "premise: the drop staged")
            XCTAssertNil(store.licenseNeeded["example"], "price \(price)")
            XCTAssertNil(store.errors["example"])
            XCTAssertEqual(AppStoreSourceRow.primaryAction(license: store.licenseNeeded["example"]), .getLatest,
                           "Get latest is back, price \(price)")
        }
    }

    // MARK: The account behind the license (review part 4, #8)

    /// A stand-in whose status names whoever `who` holds (an email, or
    /// "signed-out"), and whose get is refused needs-license at price 0.
    private func accountCLI() throws -> (CLIRunner, URL) {
        let dir = FileManager.default.temporaryDirectory.appending(path: "claim_account_\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        try "a@example.invalid".write(to: dir.appending(path: "who"), atomically: true, encoding: .utf8)
        let file = dir.appending(path: "graft.sh")
        try """
        #!/bin/sh
        printf '%s\\n' "$*" >> '\(dir.path)/argv.txt'
        who=$(/bin/cat '\(dir.path)/who')
        case "$*" in
          *--appstore-status*)
            if [ -e '\(dir.path)/status-slow' ]; then /bin/sleep 2; fi
            if [ "$who" = signed-out ]; then
              printf '%s\\n' '{"type":"appstore-status","ipatool":{"path":"/i","present":true,"version":"2.8.0","signed_in":false,"warning":null,"legacy_signin":false,"source":"bundled"},"unfairplay":{"path":"/u","present":true,"version":"v0.4.0","enrolled":true,"model_dir":"/m"},"setup_commands":[]}'
            elif [ "$who" = no-answer ]; then
              printf '%s\\n' '{"type":"appstore-status","ipatool":{"path":"/i","present":true,"version":"2.8.0","signed_in":null,"email":null,"legacy_signin":false,"unknown_reason":"keychain-no-answer","warning":null,"source":"bundled"},"unfairplay":{"path":"/u","present":true,"version":"v0.4.0","enrolled":true,"model_dir":"/m"},"setup_commands":[]}'
            elif [ "$who" = no-email ]; then
              printf '%s\\n' '{"type":"appstore-status","ipatool":{"path":"/i","present":true,"version":"2.8.0","signed_in":true,"email":null,"legacy_signin":false,"warning":null,"source":"bundled"},"unfairplay":{"path":"/u","present":true,"version":"v0.4.0","enrolled":true,"model_dir":"/m"},"setup_commands":[]}'
            elif [ "$who" = fails ]; then
              echo "error: config.yaml doesn't parse" >&2
              exit 2
            elif [ "$who" = legacy ]; then
              printf '%s\\n' '{"type":"appstore-status","ipatool":{"path":"/i","present":true,"version":"2.8.0","signed_in":null,"email":null,"legacy_signin":true,"warning":null,"source":"bundled"},"unfairplay":{"path":"/u","present":true,"version":"v0.4.0","enrolled":true,"model_dir":"/m"},"setup_commands":[]}'
            else
              printf '{"type":"appstore-status","ipatool":{"path":"/i","present":true,"version":"2.8.0","signed_in":true,"warning":null,"email":"%s","legacy_signin":false,"source":"bundled"},"unfairplay":{"path":"/u","present":true,"version":"v0.4.0","enrolled":true,"model_dir":"/m"},"setup_commands":[]}\\n' "$who"
            fi ;;
          *--appstore-get*)
            printf '%s\\n' '{"type":"appstore-refused","app":"example","reason":"needs-license","price":0,"formatted_price":"Free","track_id":100000001}'
            echo "error: this Apple ID doesn't own com.example.app yet" >&2
            exit 20 ;;
          *--appstore-claim*)
            # As the CLI does: `--expect-email` naming anyone but who is
            # signed in now buys nothing.
            expected=""; previous=""
            for arg in "$@"; do
              [ "$previous" = --expect-email ] && expected=$arg
              previous=$arg
            done
            if [ -n "$expected" ] && [ "$expected" != "$who" ]; then
              printf '{"type":"appstore-refused","app":"example","reason":"account-changed","email":"%s"}\\n' "$who"
              echo "error: ipatool is now signed in to the App Store as $who, not $expected" >&2
              exit 20
            fi
            printf '%s\\n' '{"type":"appstore-claimed","app":"example","bundle_id":"com.example.app","already_owned":false}' ;;
          *--appstore-login*)
            if [ -e '\(dir.path)/login-hangs' ]; then exec /bin/sleep 30; fi
            IFS= read -r id; IFS= read -r pw
            printf '%s\\n' "$id" > '\(dir.path)/who'
            printf '{"type":"appstore-login","state":"signed-in","email":"%s"}\\n' "$id" ;;
          *--appstore-logout*)
            echo signed-out > '\(dir.path)/who'
            printf '%s\\n' '{"type":"appstore-logout","signed_out":true}' ;;
        esac
        """.write(to: file, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: file.path)
        return (CLIRunner(executable: file), dir)
    }

    @MainActor
    func test_signing_out_drops_the_old_apple_ids_licence_and_its_banner() async throws {
        let (runner, _) = try accountCLI()
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        // No status before: the sign-out itself is what drops it, not a
        // status that happens to differ afterward.
        await refusedFree(store)
        await store.account.signOut()
        XCTAssertNil(store.licenseNeeded["example"])
        XCTAssertNil(store.errors["example"], "the banner described the Apple ID that signed out")
        XCTAssertEqual(store.claimBlockedReason, AppStoreStore.signedOutReason)
    }

    @MainActor
    func test_signing_in_drops_the_old_licence() async throws {
        let (runner, _) = try accountCLI()
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        await refusedFree(store)
        var password = Array("Example-Passw0rd".utf8)
        XCTAssertTrue(store.account.startSignIn(email: "b@example.invalid", password: &password, expectedIpatool: "/i"))
        await eventually { !store.account.isSigningIn }
        XCTAssertNil(store.licenseNeeded["example"])
        XCTAssertNil(store.errors["example"])
    }

    @MainActor
    func test_status_naming_another_apple_id_drops_the_licence_and_the_same_one_keeps_it() async throws {
        let (runner, dir) = try accountCLI()
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        await store.loadStatus()
        await refusedFree(store)
        await store.loadStatus()
        XCTAssertNotNil(store.licenseNeeded["example"], "the same Apple ID: still true")

        try "b@example.invalid".write(to: dir.appending(path: "who"), atomically: true, encoding: .utf8)
        await store.loadStatus()
        XCTAssertNil(store.licenseNeeded["example"], "switched in Terminal, then Refresh")
        XCTAssertNil(store.errors["example"])
    }

    /// The fix-check's critic: a login keychain that doesn't answer gives a
    /// status of signed_in null and no email. That is no switch of Apple ID,
    /// so a flap of it keeps the claim's banner and its offer.
    @MainActor
    func test_a_keychain_that_doesnt_answer_is_no_switch_of_apple_id() async throws {
        let (runner, dir) = try accountCLI()
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        func who(_ value: String) throws {
            try value.write(to: dir.appending(path: "who"), atomically: true, encoding: .utf8)
        }
        await store.loadStatus()
        await refusedFree(store)

        try who("no-answer")
        await store.loadStatus()
        XCTAssertNil(store.status?.ipatool.signedIn, "premise: the keychain didn't answer")
        XCTAssertEqual(store.licenseNeeded["example"]?.offersClaim, true, "locked, then Refresh")
        XCTAssertEqual(store.errors["example"], NeedsLicense.freeMessage)

        try who("a@example.invalid")
        await store.loadStatus()
        XCTAssertEqual(store.licenseNeeded["example"]?.offersClaim, true, "unlocked, the same Apple ID")
        XCTAssertEqual(store.errors["example"], NeedsLicense.freeMessage)

        // Another Apple ID behind the gap is still a switch.
        try who("no-answer")
        await store.loadStatus()
        try who("b@example.invalid")
        await store.loadStatus()
        XCTAssertNil(store.licenseNeeded["example"], "A, then no answer, then B")
        XCTAssertNil(store.errors["example"])
    }

    /// Graft 1.x's sign-in (legacy) replaced by Graft's own is a switch;
    /// signed in, then signed out, is one too, even with no answer between.
    @MainActor
    func test_legacy_then_signed_in_and_signed_in_then_out_are_switches() async throws {
        let (runner, dir) = try accountCLI()
        func who(_ value: String) throws {
            try value.write(to: dir.appending(path: "who"), atomically: true, encoding: .utf8)
        }
        let legacy = AppStoreStore(runner: runner, shell: NoAdminShell())
        try who("legacy")
        await legacy.loadStatus()
        await refusedFree(legacy)
        try who("a@example.invalid")
        await legacy.loadStatus()
        XCTAssertNil(legacy.licenseNeeded["example"], "legacy, then Graft's own sign-in")

        let out = AppStoreStore(runner: runner, shell: NoAdminShell())
        await out.loadStatus()
        await refusedFree(out)
        try who("no-answer")
        await out.loadStatus()
        XCTAssertNotNil(out.licenseNeeded["example"])
        try who("signed-out")
        await out.loadStatus()
        XCTAssertNil(out.licenseNeeded["example"], "signed in, no answer, then signed out")
    }

    func test_only_a_real_change_of_apple_id_is_a_switch() {
        typealias A = AppStoreStore.SignedInAccount
        let a = A(signedIn: true, email: "a@example.invalid", legacy: false)
        let b = A(signedIn: true, email: "b@example.invalid", legacy: false)
        let out = A(signedIn: false, email: nil, legacy: false)
        let unknown = A(signedIn: nil, email: nil, legacy: false)
        let legacy = A(signedIn: nil, email: nil, legacy: true)
        let isSwitch = AppStoreStore.isSwitch(from:to:)
        XCTAssertTrue(isSwitch(a, b))
        XCTAssertTrue(isSwitch(a, out))
        XCTAssertTrue(isSwitch(legacy, a))
        XCTAssertFalse(isSwitch(a, a))
        XCTAssertFalse(isSwitch(a, unknown), "to no answer")
        XCTAssertFalse(isSwitch(unknown, a), "from no answer")
        XCTAssertFalse(isSwitch(unknown, out), "nothing known before")
        XCTAssertFalse(isSwitch(out, unknown))
        XCTAssertFalse(isSwitch(legacy, unknown), "legacy, then no answer: nobody signed in yet")
        XCTAssertFalse(isSwitch(A(signedIn: true, email: nil, legacy: false), a), "an email first seen is no switch")

        // What the next status is compared with: the last answer for each
        // thing a status couldn't say.
        XCTAssertEqual(AppStoreStore.remembered(a, unknown), a)
        XCTAssertEqual(AppStoreStore.remembered(a, out), out, "signed out has no email")
        XCTAssertEqual(AppStoreStore.remembered(nil, unknown), unknown)
    }

    /// The rest of the truth table (delta audit try13, the critic's gap on
    /// 19811eb): every pair of states, each way, and what a run of three
    /// remembers. Legacy names nobody, so A, then legacy, is no switch by
    /// itself; what A's answers would carry into is caught on the way out:
    /// legacy, then Graft's own sign-in (anyone) is one, and so is legacy,
    /// then another Apple ID, through what `remembered` kept of A.
    func test_the_switch_truth_table_each_way_and_through_a_gap() {
        typealias A = AppStoreStore.SignedInAccount
        let a = A(signedIn: true, email: "a@example.invalid", legacy: false)
        let b = A(signedIn: true, email: "b@example.invalid", legacy: false)
        let out = A(signedIn: false, email: nil, legacy: false)
        let unknown = A(signedIn: nil, email: nil, legacy: false)
        let legacy = A(signedIn: nil, email: nil, legacy: true)
        let isSwitch = AppStoreStore.isSwitch(from:to:)
        let all = ["a": a, "b": b, "out": out, "unknown": unknown, "legacy": legacy]
        // Exactly these ordered pairs are switches.
        let switches: Set<String> = ["a>b", "b>a", "a>out", "b>out", "legacy>a", "legacy>b"]
        for (from, old) in all {
            for (to, new) in all {
                XCTAssertEqual(isSwitch(old, new), switches.contains("\(from)>\(to)"), "\(from) then \(to)")
            }
        }

        // Through what is remembered: the answer before the gap counts.
        func run(_ states: [A]) -> [Bool] {
            var last: A?
            var seen: [Bool] = []
            for now in states {
                if let last { seen.append(isSwitch(last, now)) }
                last = AppStoreStore.remembered(last, now)
            }
            return seen
        }
        XCTAssertEqual(run([a, legacy, b]), [false, true], "A, legacy, then B: B is a switch from A")
        XCTAssertEqual(run([a, legacy, a]), [false, true], "legacy, then Graft's own sign-in, is one whoever it is")
        XCTAssertEqual(run([a, unknown, b]), [false, true])
        XCTAssertEqual(run([a, unknown, a]), [false, false])
        XCTAssertEqual(run([a, out, b]), [true, false], "signing out already dropped A's answers")
        XCTAssertEqual(run([legacy, unknown, a]), [false, false], "under legacy nothing could get, so nothing to drop")
        XCTAssertEqual(AppStoreStore.remembered(a, legacy), A(signedIn: true, email: "a@example.invalid", legacy: true))
    }

    /// A, then a status with legacy_signin true (signed_in and email null):
    /// no switch, so A's license banner stays, but nothing can act on it.
    /// Get (free) is refused while legacy, and the way out is a sign-in,
    /// which drops it (the critic's case, delta audit try13).
    @MainActor
    func test_a_then_legacy_keeps_the_banner_but_refuses_its_claim_until_the_sign_in_drops_it() async throws {
        let (runner, dir) = try accountCLI()
        func who(_ value: String) throws {
            try value.write(to: dir.appending(path: "who"), atomically: true, encoding: .utf8)
        }
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        await store.loadStatus()
        await refusedFree(store)

        try who("legacy")
        await store.loadStatus()
        XCTAssertEqual(store.status?.ipatool.legacySignin, true, "premise")
        XCTAssertEqual(store.licenseNeeded["example"]?.offersClaim, true, "legacy names nobody: no switch")
        XCTAssertEqual(store.claimBlockedReason, AppStoreStore.signedOutReason)
        await tryClaim(store, false, "but no claim under another ipatool's sign-in")
        XCTAssertFalse(try argv(dir).contains { $0.contains("--appstore-claim") })

        try who("b@example.invalid")
        await store.loadStatus()
        XCTAssertNil(store.licenseNeeded["example"], "A, legacy, then B")
        XCTAssertNil(store.errors["example"])
    }

    @MainActor
    func test_get_free_is_refused_while_signed_out() async throws {
        let (runner, dir) = try accountCLI()
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        await refusedFree(store)
        try "signed-out".write(to: dir.appending(path: "who"), atomically: true, encoding: .utf8)
        await store.loadStatus()
        XCTAssertEqual(store.claimBlockedReason, AppStoreStore.signedOutReason)
        await tryClaim(store, false)
        XCTAssertFalse(try argv(dir).contains { $0.contains("--appstore-claim") })
    }

    // MARK: Who Get (free) buys for (whole-diff review, get-4)

    private func who(_ value: String, in dir: URL) throws {
        try value.write(to: dir.appending(path: "who"), atomically: true, encoding: .utf8)
    }

    private func claims(_ dir: URL) throws -> Int {
        try argv(dir).filter { $0.contains("--appstore-claim") }.count
    }

    /// The confirmation names the Apple ID a status read made as Get (free)
    /// was pressed finds, never one cached from before: ipatool claims for
    /// whoever it is signed in as when it runs. Here the session's status
    /// named nobody (signed in, no email), and the Apple ID behind it is
    /// b@example.invalid by the time the button is pressed.
    @MainActor
    func test_the_confirmation_names_the_apple_id_a_fresh_status_read_finds() async throws {
        let (runner, dir) = try accountCLI()
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        try who("no-email", in: dir)
        await store.loadStatus()
        await refusedFree(store)
        XCTAssertNil(store.status?.ipatool.email, "premise: the cached status names nobody")

        try who("b@example.invalid", in: dir)
        await store.checkBeforeClaim(app: "example")
        let asked = try XCTUnwrap(store.claimConfirmations["example"], "Get (free) asks")
        XCTAssertEqual(asked.email, "b@example.invalid")
        XCTAssertTrue(AppStoreStore.claimConfirmation(app: "Example", email: asked.email).message
            .contains("the Apple ID b@example.invalid's purchases"))
        XCTAssertFalse(store.checkingClaim.contains("example"))
        XCTAssertEqual(try claims(dir), 0, "asking claims nothing")

        store.cancelClaim(app: "example")
        XCTAssertNil(store.claimConfirmations["example"])
        XCTAssertEqual(try claims(dir), 0, "Cancel claims nothing")

        await store.checkBeforeClaim(app: "example")
        XCTAssertTrue(store.startClaim(app: "example", confirmed: try XCTUnwrap(store.claimConfirmations["example"])))
        XCTAssertNil(store.claimConfirmations["example"], "answered")
        await eventually { (try? self.claims(dir)) == 1 }
        store.stopGet(app: "example")
        await eventually { !store.isBusy("example") }
    }

    /// An Apple ID changed in Terminal since the get was refused: the
    /// license answer was about the other one, so nothing is asked, the
    /// banner says why in words, and the row is back on Get latest.
    @MainActor
    func test_get_free_is_refused_in_words_when_the_apple_id_changed_since() async throws {
        let (runner, dir) = try accountCLI()
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        await store.loadStatus()
        await refusedFree(store)

        try who("b@example.invalid", in: dir)
        await store.checkBeforeClaim(app: "example")
        XCTAssertNil(store.claimConfirmations["example"], "nothing is asked")
        XCTAssertEqual(store.errors["example"], AppStoreStore.ClaimRefusal.switched(to: "b@example.invalid").message)
        XCTAssertTrue(store.errors["example"]?.contains("changed (it's now b@example.invalid)") == true)
        XCTAssertNil(store.licenseNeeded["example"])
        XCTAssertEqual(AppStoreSourceRow.primaryAction(license: store.licenseNeeded["example"]), .getLatest)
        XCTAssertEqual(try claims(dir), 0)
    }

    /// Signed out (or only Graft 1.x's sign-in), status that can't say who
    /// is signed in, and a status read that failed: each is refused in the
    /// banner's words, nothing is asked, and nothing is claimed.
    @MainActor
    func test_get_free_is_refused_in_words_when_nobody_or_nobody_known_is_signed_in() async throws {
        let cases: [(String, AppStoreStore.ClaimRefusal)] = [
            ("signed-out", .signedOut),
            ("legacy", .signedOut),
            ("no-answer", .couldNotTell(nil)),
        ]
        for (state, refusal) in cases {
            let (runner, dir) = try accountCLI()
            let store = AppStoreStore(runner: runner, shell: NoAdminShell())
            await refusedFree(store)
            try who(state, in: dir)
            await store.checkBeforeClaim(app: "example")
            XCTAssertNil(store.claimConfirmations["example"], state)
            XCTAssertEqual(store.errors["example"], refusal.message, state)
            XCTAssertEqual(try claims(dir), 0, state)
        }

        let (runner, dir) = try accountCLI()
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        await refusedFree(store)
        try who("fails", in: dir)
        await store.checkBeforeClaim(app: "example")
        XCTAssertNil(store.claimConfirmations["example"])
        XCTAssertTrue(store.errors["example"]?.hasPrefix("Not claimed: Graft couldn't check which Apple ID") == true,
                      store.errors["example"] ?? "nil")
        XCTAssertTrue(store.errors["example"]?.contains("config.yaml doesn't parse") == true, "and why")
        XCTAssertEqual(store.licenseNeeded["example"]?.offersClaim, true, "still on offer: try again")
        XCTAssertEqual(try claims(dir), 0)

        XCTAssertTrue(AppStoreStore.ClaimRefusal.signedOut.message.contains(AppStoreStore.signedOutReason))
        XCTAssertTrue(AppStoreStore.ClaimRefusal.couldNotTell("Unlock it.").message.hasSuffix(" Unlock it."))
    }

    /// The dialog named one Apple ID; status names another by the time its
    /// Get (free) is pressed (a Refresh in Settings saw it). No claim, in
    /// words: once where status saw a switch, and once where it couldn't
    /// (the dialog named nobody, signed in with no email).
    @MainActor
    func test_a_confirmation_never_claims_for_an_apple_id_it_did_not_name() async throws {
        let (runner, dir) = try accountCLI()
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        await store.loadStatus()
        await refusedFree(store)
        await store.checkBeforeClaim(app: "example")
        let asked = try XCTUnwrap(store.claimConfirmations["example"])
        XCTAssertEqual(asked.email, "a@example.invalid")
        try who("b@example.invalid", in: dir)
        await store.loadStatus()
        XCTAssertNil(store.claimConfirmations["example"], "the switch takes the dialog down")
        XCTAssertFalse(store.startClaim(app: "example", confirmed: asked))
        XCTAssertEqual(store.errors["example"], AppStoreStore.ClaimRefusal.switched(to: "b@example.invalid").message)

        let (runner2, dir2) = try accountCLI()
        let unnamed = AppStoreStore(runner: runner2, shell: NoAdminShell())
        try who("no-email", in: dir2)
        await unnamed.loadStatus()
        await refusedFree(unnamed)
        await unnamed.checkBeforeClaim(app: "example")
        let nobody = try XCTUnwrap(unnamed.claimConfirmations["example"])
        XCTAssertNil(nobody.email, "premise: the dialog said \"this Apple ID\"")
        try who("b@example.invalid", in: dir2)
        await unnamed.loadStatus()
        XCTAssertEqual(unnamed.licenseNeeded["example"]?.offersClaim, true, "premise: no switch status could see")
        XCTAssertFalse(unnamed.startClaim(app: "example", confirmed: nobody))
        XCTAssertEqual(unnamed.errors["example"], AppStoreStore.ClaimRefusal.switched(to: "b@example.invalid").message)
        XCTAssertNil(unnamed.licenseNeeded["example"], "its license answer was about the other one")
        XCTAssertEqual(try claims(dir) + claims(dir2), 0)
    }

    // MARK: The Apple ID changed while the confirmation was open (try14 delta audit, D14-APP-1 and F5)

    /// The dialog named a@; with it still open, ipatool was signed in as b@
    /// in Terminal, and nothing in the app read status. The claim names a@
    /// (`--expect-email`), the CLI refuses it, nothing is bought, and the
    /// banner says the Apple ID changed, from a status read again.
    @MainActor
    func test_a_sign_in_changed_while_the_confirmation_is_open_buys_nothing() async throws {
        let (runner, dir) = try accountCLI()
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        await store.loadStatus()
        await refusedFree(store)
        await store.checkBeforeClaim(app: "example")
        let asked = try XCTUnwrap(store.claimConfirmations["example"])
        XCTAssertEqual(asked.email, "a@example.invalid")

        try who("b@example.invalid", in: dir)
        XCTAssertTrue(store.startClaim(app: "example", confirmed: asked), "premise: the app's status is still a@")
        await eventually { !store.isBusy("example") }

        let claim = try XCTUnwrap(try argv(dir).first { $0.contains("--appstore-claim") })
        XCTAssertEqual(claim, "--json --appstore-claim --app example --expect-email a@example.invalid")
        XCTAssertEqual(store.errors["example"], AppStoreStore.ClaimRefusal.switched(to: "b@example.invalid").message)
        XCTAssertNil(store.licenseNeeded["example"], "its license answer was about a@")
        XCTAssertEqual(AppStoreSourceRow.primaryAction(license: store.licenseNeeded["example"]), .getLatest)
        XCTAssertEqual(store.status?.ipatool.email, "b@example.invalid", "status read again")
        XCTAssertEqual(try argv(dir).filter { $0.contains("--appstore-get") }.count, 1, "and no get after it")
    }

    /// A confirmation that named nobody (signed in, no email) has nothing to
    /// expect: the claim goes as before.
    @MainActor
    func test_a_confirmation_that_named_nobody_expects_no_apple_id() async throws {
        let (runner, dir) = try accountCLI()
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        try who("no-email", in: dir)
        await refusedFree(store)
        await tryClaim(store, true)
        await eventually { (try? self.claims(dir)) == 1 }
        XCTAssertEqual(try argv(dir).first { $0.contains("--appstore-claim") }, "--json --appstore-claim --app example")
        store.stopGet(app: "example")
        await eventually { !store.isBusy("example") }
    }

    /// Get (free) pressed, then another app picked while status was read:
    /// the pane gives the check up (`cancelClaim`, from AppDetailView's
    /// `onChange(of: app.id)`), so no dialog waits to open by itself when
    /// the user comes back.
    @MainActor
    func test_a_check_given_up_on_asks_nothing_when_it_ends() async throws {
        let (runner, dir) = try accountCLI()
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        await refusedFree(store)
        // A status read slow enough to leave the app during.
        let slow = dir.appending(path: "status-slow")
        try Data().write(to: slow)
        let check = Task { await store.checkBeforeClaim(app: "example") }
        await eventually { store.checkingClaim.contains("example") }
        store.cancelClaim(app: "example")
        XCTAssertFalse(store.checkingClaim.contains("example"), "the row is back on Get (free) at once")
        await check.value
        XCTAssertNil(store.claimConfirmations["example"], "nothing is asked")
        XCTAssertNil(store.errors["example"].flatMap { $0.hasPrefix("Not claimed") ? $0 : nil })
        XCTAssertEqual(store.licenseNeeded["example"]?.offersClaim, true, "still on offer")
        XCTAssertEqual(try claims(dir), 0)

        // Pressed again on coming back, it asks as before.
        try FileManager.default.removeItem(at: slow)
        await store.checkBeforeClaim(app: "example")
        XCTAssertEqual(store.claimConfirmations["example"]?.email, "a@example.invalid")
    }

    /// Confirmed, but a sign-in started in Settings meanwhile: not claimed,
    /// and the banner says why rather than nothing happening.
    @MainActor
    func test_a_confirmed_claim_that_must_wait_says_why() async throws {
        let (runner, dir) = try accountCLI()
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        await refusedFree(store)
        await store.checkBeforeClaim(app: "example")
        let asked = try XCTUnwrap(store.claimConfirmations["example"])
        try Data().write(to: dir.appending(path: "login-hangs"))
        var password = Array("example-password".utf8)
        XCTAssertTrue(store.account.startSignIn(
            email: "a@example.invalid", password: &password, expectedIpatool: "/i"
        ))
        XCTAssertFalse(store.startClaim(app: "example", confirmed: asked))
        XCTAssertEqual(store.errors["example"], AppStoreStore.accountBusyReason)
        XCTAssertEqual(try claims(dir), 0)
        store.account.cancelSignIn()
        await eventually { !store.account.isSigningIn }
    }

    @MainActor
    func test_the_account_changed_refusal_is_read_off_a_failed_claim() throws {
        let events: [BuildEvent] = [
            .appStoreRefused(AppStoreRefusalRecord(app: "example", reason: "account-changed", email: "b@example.invalid")),
        ]
        let found = try XCTUnwrap(CLIRunner.Refusal.appStoreClaim.error(events) as? ClaimAccountChanged)
        XCTAssertEqual(found, ClaimAccountChanged(email: "b@example.invalid"))
        XCTAssertEqual(found.localizedDescription, AppStoreStore.ClaimRefusal.switched(to: "b@example.invalid").message)
        XCTAssertEqual(CLIContract.AppStoreRefusal.accountChanged, "account-changed")
        XCTAssertEqual(
            AppStoreStore.claimArgs(app: "example", expectEmail: "a@example.invalid"),
            ["--json", "--appstore-claim", "--app", "example", "--expect-email", "a@example.invalid"]
        )
        XCTAssertEqual(AppStoreStore.claimArgs(app: "example"), ["--json", "--appstore-claim", "--app", "example"])
    }

    func test_the_confirmation_names_the_app_the_apple_id_and_that_it_is_permanent_and_free() {
        let named = AppStoreStore.claimConfirmation(app: "Example", email: "someone@example.invalid")
        XCTAssertEqual(named.title, "Get Example for free?")
        XCTAssertTrue(named.message.contains("Graft adds Example to the Apple ID someone@example.invalid's purchases"))
        XCTAssertTrue(named.message.contains("at no cost"))
        XCTAssertTrue(named.message.contains("permanent"))
        XCTAssertTrue(AppStoreStore.claimConfirmation(app: "Example", email: nil).message
            .contains("to this Apple ID's purchases"))
    }

    func test_the_store_link_is_the_apps_page() {
        let need = AppStoreStore.LicenseNeed(price: 2.99, formattedPrice: "$2.99", trackId: 100000001)
        XCTAssertEqual(need.storeURL?.absoluteString, "https://apps.apple.com/app/id100000001")
    }
}
