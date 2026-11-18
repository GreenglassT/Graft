import AppKit
import SwiftUI
import XCTest
@testable import Graft

// Which account config.yaml is for, as the app learns it: `--list`'s
// `account` event (which a config with no apps still emits), the field on
// each `app`, and `--check`'s `preflight-end`. The helpers here are shared
// with `AddAppFreeAccountTests` and `FreeAccountTests`.

let accountTestShell = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))

/// One `app` event line, as `--list --json` prints it.
func accountTestAppLine(
    name: String = "example", bundleId: String = "com.example.app",
    delivery: DeliveryMode = .livecontainer, account: String? = nil
) -> String {
    let accountPart = account.map { #","account":"\#($0)""# } ?? ""
    return #"{"type":"app","name":"\#(name)","bundle_id":"\#(bundleId)","preferred_version":"latest","tweaks":[],"display_name":"Example","delivery":"\#(delivery.rawValue)","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6\#(accountPart)}"#
}

func accountEventLine(_ account: String) -> String {
    #"{"type":"account","account":"\#(account)"}"#
}

/// A `ConfigStore` whose `--list` prints `lines`, loaded once.
@MainActor
func accountTestStore(_ lines: [String]) async throws -> ConfigStore {
    let body = lines.map { "printf '%s\\n' '\($0)'" }.joined(separator: "; ")
    let store = ConfigStore(runner: accountTestShell, cliArgs: ["-c", body.isEmpty ? "true" : body])
    _ = try await store.loadApps()
    return store
}

// MARK: - Decoding

final class AccountDecodingTests: XCTestCase {
    func test_the_account_event_decodes_both_modes() {
        for (raw, mode) in [("free", AccountMode.free), ("paid", .paid)] {
            guard case let .account(decoded)? = accountEventLine(raw).buildEvents().first else {
                return XCTFail("`\(raw)` did not decode as an account event")
            }
            XCTAssertEqual(decoded, mode)
        }
    }

    /// An account spelling this app does not know is dropped, not guessed.
    func test_an_unknown_account_event_is_dropped() {
        XCTAssertTrue(accountEventLine("team").buildEvents().isEmpty)
    }

    func test_the_app_events_account_field_decodes_and_is_optional() throws {
        let events = [accountTestAppLine(account: "free"), accountTestAppLine(account: nil), accountTestAppLine(account: "team")]
            .joined(separator: "\n").buildEvents()
        let accounts = events.compactMap { event -> AccountMode?? in
            if case let .app(record) = event { return record.account }
            return nil
        }
        // Three apps decoded, including the one with a spelling this app does
        // not know: the field is config-wide and must not cost the app.
        XCTAssertEqual(accounts.count, 3)
        XCTAssertEqual(accounts, [.free, nil, nil])
    }

    func test_preflight_end_carries_the_account() {
        let lines = [
            #"{"type":"preflight-end","ok_count":2,"fail_count":3,"account":"free"}"#,
            #"{"type":"preflight-end","ok_count":1,"fail_count":0}"#,
        ]
        let ends = lines.joined(separator: "\n").buildEvents().compactMap { event -> AccountMode?? in
            if case let .preflightEnd(_, _, account) = event { return account }
            return nil
        }
        XCTAssertEqual(ends, [.free, nil])
    }

    func test_an_info_row_decodes_as_information() {
        let lines = [
            #"{"type":"preflight-check","name":"account","ok":true,"detail":"free Apple ID: LiveContainer delivery only","info":true}"#,
            #"{"type":"preflight-check","name":"otool","ok":true,"detail":"/usr/bin/otool"}"#,
        ]
        let rows = lines.joined(separator: "\n").buildEvents().compactMap { event -> PreflightCheck? in
            if case let .preflightCheck(check) = event { return check }
            return nil
        }
        XCTAssertEqual(rows.map(\.isInfo), [true, false])
    }
}

// MARK: - Stores

@MainActor
final class AccountStoreTests: XCTestCase {
    /// The gap the `account` event closes: a fresh free install has no apps,
    /// so nothing else on `--list` could say which account it is.
    func test_a_config_with_no_apps_still_knows_it_is_free() async throws {
        let store = try await accountTestStore([accountEventLine("free")])
        XCTAssertEqual(store.apps.count, 0)
        XCTAssertEqual(store.account, .free)
    }

    func test_the_event_wins_over_the_app_field() async throws {
        let store = try await accountTestStore([accountEventLine("paid"), accountTestAppLine(account: "free")])
        XCTAssertEqual(store.account, .paid)
    }

    /// A CLI from before the event, with apps: their field still answers.
    func test_an_older_cli_is_read_from_its_app_events() async throws {
        let store = try await accountTestStore([accountTestAppLine(account: "free")])
        XCTAssertEqual(store.account, .free)
    }

    /// A CLI from before free mode: not known, which disables nothing.
    func test_a_cli_that_says_nothing_leaves_the_account_unknown() async throws {
        let store = try await accountTestStore([accountTestAppLine(account: nil)])
        XCTAssertNil(store.account)
        XCTAssertFalse(store.account.isFree)
    }

    /// The pill counts a free account's row as neither OK nor Fail, and the
    /// scan's account is recorded for `ContentView` to compare.
    func test_the_preflight_counts_skip_the_info_row_and_record_the_account() async throws {
        let settings = SettingsStore(runner: accountTestShell)
        let lines = [
            #"{"type":"preflight-check","name":"account","ok":true,"detail":"free Apple ID: LiveContainer delivery only","info":true}"#,
            #"{"type":"preflight-check","name":"otool","ok":true,"detail":"/usr/bin/otool"}"#,
            #"{"type":"preflight-check","name":"cyan","ok":false,"detail":null}"#,
            #"{"type":"preflight-end","ok_count":2,"fail_count":1,"account":"free"}"#,
        ]
        let body = lines.map { "printf '%s\\n' '\($0)'" }.joined(separator: "; ")
        try await settings.runChecks(argsOverride: ["-c", body + "; exit 3"])
        XCTAssertEqual(settings.checks.count, 3)
        XCTAssertEqual(settings.okCount, 1, "the account row is not a check that passed")
        XCTAssertEqual(settings.failCount, 1, "nor one that failed")
        XCTAssertEqual(settings.account, .free)
    }
}
