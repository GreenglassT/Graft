import XCTest
@testable import Graft

/// The GUI half of the delivery-state feature: decoding the CLI's
/// `delivery-state` event, and carrying it on `ConfigStore` beside the app list.
///
/// Fixtures here are copied from what `graft --json --list` actually
/// emits, summaries included. That matters twice over: the summary is the
/// CLI's copy and the GUI never authors a parallel version of it, and the
/// summary/reason pairings are the ones `diff_delivery` really produces — a
/// fixture that invents a plausible-looking pairing is documentation of a wire
/// format nobody ships.
final class DeliveryStateTests: XCTestCase {

    // MARK: - Decoding the event

    func test_decodes_a_stale_state() throws {
        let line = #"{"type":"delivery-state","app":"example","kind":"stale","summary":"Changed since the last delivery.","reasons":["d-tweak.deb added"],"pushed_at":"2026-05-13T16:20:00+00:00"}"#
        guard case let .deliveryState(rec) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected a delivery-state event")
        }
        XCTAssertEqual(rec.app, "example")
        XCTAssertEqual(rec.kind, .stale)
        XCTAssertEqual(rec.summary, "Changed since the last delivery.")
        XCTAssertEqual(rec.reasons, ["d-tweak.deb added"])
        XCTAssertEqual(rec.pushedAt, "2026-05-13T16:20:00+00:00")
    }

    func test_decodes_never_with_no_pushed_at() throws {
        let line = #"{"type":"delivery-state","app":"delta","kind":"never","summary":"Never delivered to a device.","reasons":[],"pushed_at":null}"#
        guard case let .deliveryState(rec) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected a delivery-state event")
        }
        XCTAssertEqual(rec.kind, .never)
        XCTAssertNil(rec.pushedAt)
    }

    func test_an_unrecognised_kind_decodes_as_unknown() throws {
        // Forward compatibility: a newer CLI adding a kind must not make the
        // whole event undecodable, because buildEvents() drops what it can't parse.
        let line = #"{"type":"delivery-state","app":"bravo","kind":"something-new","summary":"?","reasons":[],"pushed_at":null}"#
        guard case let .deliveryState(rec) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected a delivery-state event")
        }
        XCTAssertEqual(rec.kind, .unknown)
    }

    /// Same leniency, applied to the one array on the event: a state that
    /// names no cause is still a state, and is exactly what `current` and
    /// `never` look like.
    func test_a_state_without_reasons_decodes_as_reasonless() throws {
        let line = #"{"type":"delivery-state","app":"bravo","kind":"current","summary":"Up to date with the last delivery."}"#
        guard case let .deliveryState(rec) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected a delivery-state event")
        }
        XCTAssertEqual(rec.reasons, [])
        XCTAssertNil(rec.pushedAt)
    }

    /// The same leniency, one layer up: `String.buildEvents()` is where a throw
    /// actually costs a row, and it is silent about it.
    func test_an_unrecognised_kind_still_reaches_buildEvents() {
        let line = #"{"type":"delivery-state","app":"bravo","kind":"something-new","summary":"?","reasons":[],"pushed_at":null}"#
        let states = line.buildEvents().compactMap { event -> DeliveryStateRecord? in
            if case let .deliveryState(rec) = event { return rec }
            return nil
        }
        XCTAssertEqual(states.count, 1, "a kind this GUI predates must degrade, not vanish")
    }

    func test_only_never_and_stale_show_a_badge() {
        XCTAssertTrue(DeliveryKind.never.needsAttention)
        XCTAssertTrue(DeliveryKind.stale.needsAttention)
        XCTAssertFalse(DeliveryKind.current.needsAttention)
        XCTAssertFalse(DeliveryKind.unknown.needsAttention)
    }

    /// All four states have words, because the app pane's meta strip has to
    /// print one — `MetaCell` takes a `String`, not a `String?`.
    ///
    /// This replaces a `test_the_badge_label_names_the_state` that pinned
    /// `DeliveryKind.badgeLabel`, a `needsAttention ? stateLabel : nil` member
    /// with no callers left once every mark in the app became a dot driven off
    /// `needsAttention`. Its test was the only thing referring to it, which is
    /// its own verdict; both halves it composed are pinned here and above.
    ///
    /// The last assertion is the one that matters most — a label for `current`
    /// is not a badge for it, and the day the wording goes back to being the
    /// decision, every app in the sidebar grows a warning dot.
    func test_every_state_has_words_even_the_ones_without_a_badge() {
        XCTAssertEqual(DeliveryKind.never.stateLabel, "not delivered")
        XCTAssertEqual(DeliveryKind.stale.stateLabel, "out of date")
        XCTAssertEqual(DeliveryKind.current.stateLabel, "up to date")
        XCTAssertEqual(DeliveryKind.unknown.stateLabel, "unknown")
        XCTAssertFalse(
            DeliveryKind.current.needsAttention,
            "a state having words must not be what earns it a badge"
        )
    }

    // MARK: - Rendering the CLI's words

    func test_detail_is_the_summary_and_the_reasons() {
        let rec = DeliveryStateRecord(
            app: "example",
            kind: .stale,
            summary: "Changed since the last delivery.",
            reasons: ["d-tweak.deb rebuilt since delivery", "+2 more changes"],
            pushedAt: nil
        )
        XCTAssertEqual(
            rec.reasonLine,
            "d-tweak.deb rebuilt since delivery; +2 more changes"
        )
        XCTAssertEqual(
            rec.detail,
            "Changed since the last delivery. "
            + "d-tweak.deb rebuilt since delivery; +2 more changes"
        )
    }

    func test_detail_of_a_reasonless_state_is_just_the_summary() {
        let rec = DeliveryStateRecord(
            app: "delta", kind: .never,
            summary: "Never delivered to a device.", reasons: [], pushedAt: nil
        )
        XCTAssertNil(rec.reasonLine)
        XCTAssertEqual(rec.detail, "Never delivered to a device.")
    }

    // MARK: - Carrying it on the store

    /// The join is across two different key names: the `app` event calls it
    /// `name` and `delivery-state` calls it `app`. Both are pinned by Python
    /// tests, so the GUI joins on the pair rather than "fixing" either.
    @MainActor
    func test_loadApps_keys_the_state_by_the_app_events_name() async throws {
        let store = ConfigStore(runner: shRunner(), cliArgs: ["-c", Fixture.twoApps])
        let apps = try await store.loadApps()

        XCTAssertEqual(apps.map(\.name), ["delta", "example"])
        XCTAssertEqual(store.deliveryStates[apps[1].name]?.kind, .stale)
        XCTAssertEqual(store.deliveryStates[apps[0].name]?.kind, .never)
        XCTAssertEqual(
            store.deliveryStates["example"]?.reasons, ["d-tweak.deb added"]
        )
    }

    /// An app the CLI lists without a state — an older CLI, or a state it
    /// declined to compute — is simply unbadged. Not defaulted: a filled-in
    /// `never` would badge every app on a machine running last month's CLI,
    /// and a badge that fires on everything is a badge nobody reads.
    @MainActor
    func test_an_app_with_no_state_event_is_left_unbadged() async throws {
        let store = ConfigStore(runner: shRunner(), cliArgs: ["-c", Fixture.oneAppHasNoState])
        let apps = try await store.loadApps()

        XCTAssertEqual(apps.map(\.name), ["bravo", "delta", "example"])
        XCTAssertNil(store.deliveryStates["bravo"])
        XCTAssertEqual(store.deliveryStates.count, 2)
    }

    /// `Dictionary(uniqueKeysWithValues:)` traps on a duplicate key, and this
    /// dictionary is built out of a subprocess's stdout — the one input the GUI
    /// does not control. A repeated app must cost a badge, not the process.
    @MainActor
    func test_a_repeated_state_for_one_app_does_not_trap() async throws {
        let store = ConfigStore(runner: shRunner(), cliArgs: ["-c", Fixture.duplicateState])
        _ = try await store.loadApps()
        XCTAssertEqual(store.deliveryStates.count, 1)
        // Last wins, matching how a config re-read resolves a repeated key.
        XCTAssertEqual(store.deliveryStates["example"]?.kind, .stale)
    }

    /// The map is replaced, not merged: a removed app whose badge outlived it
    /// would be a standing warning about an app that is no longer in the list.
    @MainActor
    func test_a_reload_drops_the_state_of_an_app_that_is_gone() async throws {
        // One store, one argument vector, two different answers — the second
        // run of the script drops example, exactly as a `--remove-app` would.
        let marker = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-shrink-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: marker) }
        let script = """
        if [ -f '\(marker.path)' ]; then \(Fixture.oneApp); \
        else touch '\(marker.path)'; \(Fixture.twoApps); fi
        """

        let store = ConfigStore(runner: shRunner(), cliArgs: ["-c", script])
        _ = try await store.loadApps()
        XCTAssertNotNil(store.deliveryStates["example"])

        _ = try await store.loadApps()
        XCTAssertEqual(store.apps.map(\.name), ["delta"])
        XCTAssertNil(store.deliveryStates["example"])
        XCTAssertNotNil(store.deliveryStates["delta"])
    }

    /// `--list` costs ~250–300 ms in the realistic case (3 apps × 3 cached
    /// versions under a `latest` pin), and the GUI runs it on every refresh.
    /// The badge rides along on the events that call already produces; asking
    /// for it separately would double the wait it was measured against.
    @MainActor
    func test_the_badge_costs_no_second_subprocess() async throws {
        let marker = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-runs-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: marker) }

        let script = "echo run >> '\(marker.path)'\n" + Fixture.twoApps
        let store = ConfigStore(runner: shRunner(), cliArgs: ["-c", script])
        _ = try await store.loadApps()

        XCTAssertFalse(store.deliveryStates.isEmpty, "no states — the fixture didn't run")
        let runs = try String(contentsOf: marker, encoding: .utf8)
            .split(separator: "\n", omittingEmptySubsequences: true).count
        XCTAssertEqual(
            runs, 1,
            "loadApps() spawned \(runs) subprocesses; the badge must arrive with the app list"
        )
    }

    /// The badge doubled `--list`'s output, and output is what deadlocks.
    ///
    /// `--list` emits one `app` line (~330 bytes) per app; the delivery-state
    /// feature added a ~180-byte `delivery-state` line beside each one. That
    /// cut the app count at which the whole listing stops fitting in the 64 KB
    /// kernel stdout pipe buffer from 216 to 135. Past it the CLI
    /// blocks on `write(2)` and never exits, so a `loadApps()` that only reads
    /// stdout *after* the child exits never returns: the sidebar spins forever
    /// with no error, which is the worst thing a GUI can do.
    @MainActor
    func test_loadApps_survives_a_long_app_list() async throws {
        let (script, bytes) = Fixture.manyApps(150)
        XCTAssertGreaterThan(
            bytes, 64 * 1024,
            "fixture must exceed the stdout pipe buffer or it proves nothing"
        )
        let store = ConfigStore(runner: shRunner(), cliArgs: ["-c", script])

        guard let outcome = await finishing(within: 20, { @MainActor in
            try await store.loadApps()
        }) else {
            return XCTFail("loadApps() never returned for a \(bytes)-byte listing — "
                           + "the CLI is blocked on a full stdout pipe")
        }
        let apps = try outcome.get()
        XCTAssertEqual(apps.count, 150, "app lines were lost — stdout was truncated")
        XCTAssertEqual(store.deliveryStates.count, 150, "delivery-state lines were lost")
    }

    // MARK: - Refreshing it after a build

    /// The gap the badge fell into: `deliveryStates` was written only by
    /// `loadApps()`, and `loadApps()` was called exactly once, from
    /// ContentView's `.task`. `mutate` reloads after every *config* write —
    /// which is why add-app and attach-tweak happened to clear a stale badge —
    /// but a build goes through `runner.stream` and writes no config, so
    /// nothing reloaded. The user built, installed, and was still told
    /// "Changed since the last delivery" for the rest of the session; the
    /// obvious response to that is to build again.
    ///
    /// This is also the half of the sidebar-click-kills-the-build fix that a
    /// test can reach: the refresh lives on the store and takes no view state,
    /// so a build the user navigated away from still lands.
    @MainActor
    func test_refreshDeliveryStates_replaces_the_badge_a_build_just_cleared() async throws {
        // One argument vector, two answers: `stale` before the build, `current`
        // after it, exactly as a real `--list` would report either side of a
        // successful push.
        let marker = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-delivered-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: marker) }
        let script = """
        if [ -f '\(marker.path)' ]; then \(Fixture.twoAppsExampleCurrent); \
        else touch '\(marker.path)'; \(Fixture.twoApps); fi
        """

        let store = ConfigStore(runner: shRunner(), cliArgs: ["-c", script])
        _ = try await store.loadApps()
        XCTAssertEqual(store.deliveryStates["example"]?.kind, .stale, "premise")

        await store.refreshDeliveryStates()

        XCTAssertEqual(
            store.deliveryStates["example"]?.kind, .current,
            "the amber card and the sidebar badge survived the build that made "
            + "the phone current"
        )
        XCTAssertNil(store.lastError)
    }

    /// A refresh that fails leaves the badges it couldn't replace standing —
    /// stale is closer to the truth than absent — and says so on the store's
    /// one error channel rather than throwing at a build that already
    /// succeeded.
    @MainActor
    func test_a_failed_refresh_keeps_the_old_badges_and_reports_itself() async throws {
        let marker = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-refresh-fail-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: marker) }
        let script = """
        if [ -f '\(marker.path)' ]; then printf 'error: config.yaml is unreadable\\n' >&2; exit 2; \
        else touch '\(marker.path)'; \(Fixture.twoApps); fi
        """

        let store = ConfigStore(runner: shRunner(), cliArgs: ["-c", script])
        _ = try await store.loadApps()

        await store.refreshDeliveryStates()

        XCTAssertEqual(
            store.deliveryStates["example"]?.kind, .stale,
            "a failed refresh must not blank every badge in the sidebar"
        )
        let reported = store.lastError ?? ""
        XCTAssertTrue(reported.contains("delivery badges"), "got: \(reported)")
        XCTAssertTrue(reported.contains("config.yaml is unreadable"), "got: \(reported)")
    }

    /// Dismissing the re-install sheet cancels the `.task` its refresh was
    /// started from, and `runOnce` reports the SIGTERM'd child as a non-zero
    /// exit. That must not put "Couldn't refresh the delivery badges" in front
    /// of the user for work they walked away from.
    @MainActor
    func test_a_cancelled_refresh_is_silent() async throws {
        let store = ConfigStore(
            runner: shRunner(), cliArgs: ["-c", "sleep 5; " + Fixture.twoApps]
        )
        let task = Task { @MainActor in await store.refreshDeliveryStates() }
        try await Task.sleep(nanoseconds: 500_000_000)
        task.cancel()
        await task.value

        XCTAssertNil(
            store.lastError,
            "a refresh nobody is waiting on must unwind quietly, not leave an alert"
        )
    }

    // MARK: - Checking it against the device

    /// The whole point of the verb: every other path in this file compares an
    /// inputs digest against the delivery log, and this is the only one where
    /// the phone gets a vote. A `current` badge the device contradicts must
    /// come back demoted.
    @MainActor
    func test_a_device_check_folds_the_answer_into_the_badges() async throws {
        let store = ConfigStore(runner: shRunner(), cliArgs: ["-c", Fixture.twoAppsExampleCurrent])
        _ = try await store.loadApps()
        XCTAssertEqual(store.deliveryStates["example"]?.kind, .current, "premise")

        let outcome = try await store.reconcile(
            appName: "example", argsOverride: ["-c", Fixture.checkDemotesExample]
        )

        XCTAssertEqual(store.deliveryStates["example"]?.kind, .stale)
        XCTAssertEqual(
            store.deliveryStates["example"]?.summary,
            "Our record says delivered; the file is not on the device."
        )
        XCTAssertEqual(outcome.states.map(\.app), ["example"])
        XCTAssertTrue(outcome.warnings.isEmpty)
    }

    /// `--reconcile --app example` emits example's state and nothing else —
    /// verified against the real CLI, not assumed. So the states it returns
    /// must be *merged* into the badge map, where `loadApps()` deliberately
    /// assigns over it. Assigning here would blank every other app in the
    /// sidebar: states the CLI never claimed had changed, erased by a check of
    /// one app.
    @MainActor
    func test_a_scoped_check_leaves_every_other_badge_alone() async throws {
        let store = ConfigStore(runner: shRunner(), cliArgs: ["-c", Fixture.twoApps])
        _ = try await store.loadApps()
        XCTAssertEqual(store.deliveryStates.count, 2, "premise")

        _ = try await store.reconcile(
            appName: "example", argsOverride: ["-c", Fixture.checkDemotesExample]
        )

        XCTAssertEqual(
            store.deliveryStates["delta"]?.kind, .never,
            "checking example erased the badge of an app the CLI never mentioned"
        )
        XCTAssertEqual(store.deliveryStates.count, 2)
    }

    /// The failure this feature is one line away from at all times: the verb
    /// is best-effort, so a check that never reached a phone exits 0 and
    /// re-prints the *local* answer — byte-identical to a check that found
    /// everything in order. The warning is the only thing separating them, and
    /// dropping it leaves a button that appears to confirm a delivery nothing
    /// looked at.
    @MainActor
    func test_a_check_that_reached_no_device_says_so() async throws {
        let store = ConfigStore(runner: shRunner(), cliArgs: ["-c", Fixture.twoAppsExampleCurrent])
        _ = try await store.loadApps()

        let outcome = try await store.reconcile(
            appName: "example", argsOverride: ["-c", Fixture.checkWithNoDevice]
        )

        XCTAssertEqual(outcome.warnings, ["no device checked: no iOS devices connected"])
        XCTAssertEqual(
            store.deliveryStates["example"]?.kind, .current,
            "an unreachable device must leave the record it couldn't test standing"
        )
    }

    /// K6(a) / C-NOTICE: a startup notice — written before the verb, about
    /// the machine — is not the check's result. Counted, a renamed
    /// environment variable became every Check device's only "warning", so a
    /// check that did reach the phone read as one that had not.
    @MainActor
    func test_a_startup_notice_is_not_a_device_check_warning() async throws {
        let store = ConfigStore(runner: shRunner(), cliArgs: ["-c", Fixture.twoAppsExampleCurrent])
        _ = try await store.loadApps()

        let clean = try await store.reconcile(
            appName: "example", argsOverride: ["-c", Fixture.checkAfterANotice]
        )
        XCTAssertEqual(clean.warnings, [], "the notice was read as the check's result")

        let warned = try await store.reconcile(
            appName: "example", argsOverride: ["-c", Fixture.checkWithNoDeviceAfterANotice]
        )
        XCTAssertEqual(warned.warnings, ["no device checked: no iOS devices connected"],
                       "the check's own warning still arrives")
    }

    /// Omitting `--device` is not neutral. `_do_reconcile` falls back to
    /// `select_device(None)`, which takes the first connected phone;
    /// `_reconcile_app` then bails on the udid mismatch *silently* — by design,
    /// since a different phone is not evidence — so the run exits 0 with no
    /// warning and the pane reports "the record still stands" about a device it
    /// never looked at. With two phones connected and one picked in Build
    /// options, that is a clean bill of health for the wrong hardware.
    @MainActor
    func test_the_check_targets_the_device_the_user_picked() async throws {
        let args = try await capturedCheckArgs(appName: "example", deviceUDID: "UDID-B")
        XCTAssertEqual(
            args, ["--json", "--reconcile", "--app", "example", "--device", "UDID-B"]
        )
    }

    /// No picked device means no flag — the CLI then honours
    /// `default_device_udid` from config.yaml, which an empty string would not.
    @MainActor
    func test_an_empty_device_selection_sends_no_device_flag() async throws {
        let args = try await capturedCheckArgs(appName: "example", deviceUDID: "")
        XCTAssertEqual(args, ["--json", "--reconcile", "--app", "example"])
    }

    /// The argument vector, checked against the real spelling rather than
    /// inferred from a stub's output. `--reconcile` is a boolean verb flag and
    /// `--app` is the separate option that scopes it; sending the app name as
    /// a positional would make Click read it as an app to *build*.
    @MainActor
    func test_the_check_scopes_itself_with_the_app_flag() async throws {
        let args = try await capturedCheckArgs(appName: "example")
        XCTAssertEqual(args, ["--json", "--reconcile", "--app", "example"])
    }

    /// Unscoped, the verb checks every configured app — so no `--app` may be
    /// sent at all. An empty string would name an app that doesn't exist and
    /// fail the whole call.
    @MainActor
    func test_an_unscoped_check_sends_no_app_flag() async throws {
        let args = try await capturedCheckArgs(appName: nil)
        XCTAssertEqual(args, ["--json", "--reconcile"])
    }

    /// Records the argv `reconcile` builds. Simpler than `AppOptionsDraft`'s
    /// equivalent because there is no reload to separate out: this call writes
    /// to the delivery log, not config.yaml, so it spawns exactly one process.
    @MainActor
    private func capturedCheckArgs(
        appName: String?, deviceUDID: String? = nil
    ) async throws -> [String] {
        let dir = NSTemporaryDirectory()
        let capturePath = dir + "check_args_\(UUID().uuidString).txt"
        let scriptPath = dir + "check_capture_\(UUID().uuidString).sh"
        let script = """
        #!/bin/sh
        for arg in "$@"; do printf '%s\\n' "$arg" >> "\(capturePath)"; done
        """
        try script.write(toFile: scriptPath, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o755], ofItemAtPath: scriptPath
        )
        defer {
            try? FileManager.default.removeItem(atPath: scriptPath)
            try? FileManager.default.removeItem(atPath: capturePath)
        }

        let store = ConfigStore(runner: CLIRunner(executable: URL(fileURLWithPath: scriptPath)))
        _ = try await store.reconcile(appName: appName, deviceUDID: deviceUDID)

        let captured = try String(contentsOfFile: capturePath, encoding: .utf8)
        return captured.split(separator: "\n", omittingEmptySubsequences: true).map(String.init)
    }

    // MARK: - Fixtures

    private func shRunner() -> CLIRunner {
        CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
    }

    private enum Fixture {
        static let exampleApp = #"{"type":"app","name":"example","bundle_id":"com.example.app","preferred_version":"latest","tweaks":["d-tweak.deb"],"display_name":"Example+","delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}"#
        static let exampleState = #"{"type":"delivery-state","app":"example","kind":"stale","summary":"Changed since the last delivery.","reasons":["d-tweak.deb added"],"pushed_at":"2026-05-13T16:20:00+00:00"}"#
        static let deltaApp = #"{"type":"app","name":"delta","bundle_id":"com.delta.app","preferred_version":"44.7.0","tweaks":[],"display_name":null,"delivery":"sideload","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}"#
        static let deltaState = #"{"type":"delivery-state","app":"delta","kind":"never","summary":"Never delivered to a device.","reasons":[],"pushed_at":null}"#
        /// The same app twice, the second carrying a different state.
        static let exampleStateCurrent = #"{"type":"delivery-state","app":"example","kind":"current","summary":"Up to date with the last delivery.","reasons":[],"pushed_at":"2026-05-13T16:20:00+00:00"}"#
        /// Listed, but the CLI said nothing about its delivery.
        static let bravoApp = #"{"type":"app","name":"bravo","bundle_id":"com.bravo.app","preferred_version":"latest","tweaks":[],"display_name":null,"delivery":"sideload","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}"#

        static let twoApps = emit([exampleApp, exampleState, deltaApp, deltaState])
        /// The same listing after a successful build+push: example is current.
        static let twoAppsExampleCurrent = emit(
            [exampleApp, exampleStateCurrent, deltaApp, deltaState]
        )
        /// What `--reconcile` re-reports after finding the delivered file gone
        /// from LiveContainer's Documents/. Wording copied from
        /// `_delivery_state_for`'s `reconcile_ok == 0` branch: it states the
        /// evidence rather than the conclusion, because an imported-then-
        /// cleaned-up IPA is missing from that directory too.
        static let exampleStateDemoted = #"{"type":"delivery-state","app":"example","kind":"stale","summary":"Our record says delivered; the file is not on the device.","reasons":["last delivery is no longer on the device"],"pushed_at":"2026-05-13T16:20:00+00:00"}"#
        /// `_warn`'s route to a machine consumer: stdout carries the JSON event
        /// stream, so prose can only reach the GUI as a `log` event. Copied
        /// from a real `graft --json --reconcile` run with nothing
        /// plugged in.
        static let noDeviceWarning = #"{"type":"log","source":"warning","line":"no device checked: no iOS devices connected"}"#

        /// A scoped check that demoted the app it was given — and, as the real
        /// CLI does, says nothing at all about any other app.
        static let checkDemotesExample = emit([exampleStateDemoted])
        /// A check that exited 0 without ever reaching a phone. Note the state
        /// line is the *unchanged* local answer: this is byte-identical to a
        /// clean check apart from the warning above it.
        static let checkWithNoDevice = emit([noDeviceWarning, exampleStateCurrent])
        /// C-NOTICE: the line `main` writes before any verb while a renamed
        /// environment variable is still set.
        static let startupNotice = #"{"type":"log","source":"startup","line":"TB_SSH_PASS is set but no longer read; use GRAFT_SSH_PASS"}"#
        static let checkAfterANotice = emit([startupNotice, exampleStateCurrent])
        static let checkWithNoDeviceAfterANotice = emit(
            [startupNotice, noDeviceWarning, exampleStateCurrent]
        )

        static let oneApp = emit([deltaApp, deltaState])
        static let duplicateState = emit([exampleApp, exampleStateCurrent, exampleState])
        static let oneAppHasNoState = emit(
            [exampleApp, exampleState, deltaApp, deltaState, bravoApp]
        )

        /// A realistic `--list` for `count` apps: an `app` line and its
        /// `delivery-state` line each, in the shapes the CLI actually emits,
        /// with only the app name varying. Returns the script and the byte
        /// count it will put on stdout, so a test can assert its own premise.
        static func manyApps(_ count: Int) -> (script: String, bytes: Int) {
            var script = ""
            var bytes = 0
            for i in 0..<count {
                let name = String(format: "app%03d", i)
                let app = exampleApp.replacingOccurrences(
                    of: #""name":"example""#, with: #""name":"\#(name)""#)
                let state = exampleState.replacingOccurrences(
                    of: #""app":"example""#, with: #""app":"\#(name)""#)
                script += emit([app, state]) + "\n"
                bytes += app.utf8.count + state.utf8.count + 2  // + the newlines
            }
            return (script, bytes)
        }

        private static func emit(_ lines: [String]) -> String {
            // %s so a JSON body containing a % can never be read as a format.
            "printf '%s\\n' " + lines.map { "'\($0)'" }.joined(separator: " ")
        }
    }
}
