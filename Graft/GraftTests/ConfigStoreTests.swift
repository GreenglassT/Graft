import XCTest
@testable import Graft

final class ConfigStoreTests: XCTestCase {
    @MainActor
    func test_loadApps_returns_records_from_cli_output() async throws {
        // Stub CLIRunner with a /bin/sh that prints two JSON-Lines app events.
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let script = #"""
        printf '{"type":"app","name":"example","bundle_id":"com.example.app","preferred_version":"latest","tweaks":["a.deb"],"display_name":"Example+","delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}\n{"type":"app","name":"delta","bundle_id":"com.delta.app","preferred_version":"44.7.0","tweaks":["v.deb"],"display_name":null,"delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}\n'
        """#
        let store = ConfigStore(
            runner: runner,
            cliArgs: ["-c", script]
        )

        let apps = try await store.loadApps()
        XCTAssertEqual(apps.count, 2)
        XCTAssertEqual(apps.map(\.name).sorted(), ["delta", "example"])
        XCTAssertEqual(apps.first(where: { $0.name == "example" })?.delivery, .livecontainer)
    }

    @MainActor
    func test_addApp_url_invokes_cli_and_reloads() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        // First script: --add-app emits app-added.
        // Second script: --list (reload) emits one `app` event.
        let addScript = #"""
        printf '{"type":"app-added","name":"bravo","bundle_id":"com.bravo.app","display_name":"Bravo","delivery":"sideload"}\n'
        """#
        let reloadScript = #"""
        printf '{"type":"app","name":"bravo","bundle_id":"com.bravo.app","preferred_version":"latest","tweaks":[],"display_name":"Bravo","delivery":"sideload","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}\n'
        """#
        let store = ConfigStore(runner: runner, cliArgs: ["-c", reloadScript])
        try await store.addApp(
            appStoreURL: "https://apps.apple.com/us/app/bravo/id100000001",
            delivery: .sideload,
            addArgsOverride: ["-c", addScript]
        )
        XCTAssertEqual(store.apps.count, 1)
        XCTAssertEqual(store.apps[0].name, "bravo")
    }

    @MainActor
    func test_addAppManual_invokes_cli_and_reloads() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let addScript = #"""
        printf '{"type":"app-added","name":"customapp","bundle_id":"com.example.x","display_name":"customapp","delivery":"sideload"}\n'
        """#
        let reloadScript = #"""
        printf '{"type":"app","name":"customapp","bundle_id":"com.example.x","preferred_version":"latest","tweaks":[],"display_name":null,"delivery":"sideload","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}\n'
        """#
        let store = ConfigStore(runner: runner, cliArgs: ["-c", reloadScript])
        try await store.addAppManual(
            name: "customapp",
            bundleId: "com.example.x",
            displayName: nil,
            delivery: .sideload,
            addArgsOverride: ["-c", addScript]
        )
        XCTAssertEqual(store.apps.first?.name, "customapp")
    }

    @MainActor
    func test_cachedIPA_returns_record_when_exists() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let script = #"""
        printf '{"type":"cached-ipa","app":"example","exists":true,"bundle_id":"com.example.app","version":"21.16.2","filename":"com.example.app_21.16.2.ipa","path":"/x/y.ipa","size_bytes":12345}\n'
        """#
        let store = ConfigStore(runner: runner, cliArgs: ["-c", "true"])
        let rec = try await store.cachedIPA(appName: "example", argsOverride: ["-c", script])
        XCTAssertNotNil(rec)
        XCTAssertEqual(rec?.exists, true)
        XCTAssertEqual(rec?.filename, "com.example.app_21.16.2.ipa")
        XCTAssertEqual(rec?.sizeBytes, 12345)
    }

    @MainActor
    func test_cachedIPA_returns_nil_when_missing() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let script = #"""
        printf '{"type":"cached-ipa","app":"example","exists":false}\n'
        """#
        let store = ConfigStore(runner: runner, cliArgs: ["-c", "true"])
        let rec = try await store.cachedIPA(appName: "example", argsOverride: ["-c", script])
        XCTAssertNotNil(rec)
        XCTAssertFalse(rec?.exists ?? true)
    }

    @MainActor
    func test_provideIPA_returns_record_on_success() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let script = #"""
        printf '{"type":"ipa-provided","app":"example","bundle_id":"com.example.app","version":"21.16.2","filename":"com.example.app_21.16.2.ipa","path":"/x/y.ipa","size_bytes":12345}\n'
        """#
        let store = ConfigStore(runner: runner, cliArgs: ["-c", "true"])
        let rec = try await store.provideIPA(
            appName: "example",
            fileURL: URL(fileURLWithPath: "/tmp/whatever.ipa"),
            argsOverride: ["-c", script]
        )
        XCTAssertEqual(rec?.record.app, "example")
        XCTAssertTrue(rec?.record.exists ?? false)
        XCTAssertEqual(rec?.record.version, "21.16.2")
    }

    @MainActor
    func test_attachTweak_invokes_cli_and_returns_record() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        // First script: --attach-tweak emits tweak-attached.
        // Second script: --list (reload) emits one `app` event with the new tweak.
        let attachScript = #"""
        printf '{"type":"tweak-attached","app":"example","filename":"new.deb","tweaks":["a.deb","new.deb"]}\n'
        """#
        let reloadScript = #"""
        printf '{"type":"app","name":"example","bundle_id":"com.x.example","preferred_version":"latest","tweaks":["a.deb","new.deb"],"display_name":null,"delivery":"sideload","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}\n'
        """#
        let store = ConfigStore(runner: runner, cliArgs: ["-c", reloadScript])
        let rec = try await store.attachTweak(
            appName: "example", filename: "new.deb",
            argsOverride: ["-c", attachScript]
        )
        XCTAssertEqual(rec?.app, "example")
        XCTAssertEqual(rec?.filename, "new.deb")
        XCTAssertEqual(rec?.tweaks, ["a.deb", "new.deb"])
        // The reload populated store.apps from the second script.
        XCTAssertEqual(store.apps.first?.tweaks, ["a.deb", "new.deb"])
    }

    @MainActor
    func test_detachTweak_invokes_cli_and_returns_record() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let detachScript = #"""
        printf '{"type":"tweak-detached","app":"example","filename":"old.deb","tweaks":["a.deb"]}\n'
        """#
        let reloadScript = #"""
        printf '{"type":"app","name":"example","bundle_id":"com.x.example","preferred_version":"latest","tweaks":["a.deb"],"display_name":null,"delivery":"sideload","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}\n'
        """#
        let store = ConfigStore(runner: runner, cliArgs: ["-c", reloadScript])
        let rec = try await store.detachTweak(
            appName: "example", filename: "old.deb",
            argsOverride: ["-c", detachScript]
        )
        XCTAssertEqual(rec?.tweaks, ["a.deb"])
        XCTAssertEqual(store.apps.first?.tweaks, ["a.deb"])
    }

    @MainActor
    func test_appRecord_decodes_the_cyan_fields() throws {
        let line = #"{"type":"app","name":"example","bundle_id":"com.example.app","preferred_version":"latest","tweaks":[],"display_name":"Example+","delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":9}"#
        guard case let .app(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected an app event")
        }
        XCTAssertEqual(record.keepBundleId, true)
        XCTAssertEqual(record.stripExtensions, false)
        XCTAssertEqual(record.stripSupportedDevices, true)
        XCTAssertEqual(record.compressionLevel, 9)
    }

    @MainActor
    func test_appRecord_rejects_a_payload_missing_a_cyan_field() throws {
        // Inverted from an earlier test that asserted these defaulted when
        // absent. AppRecord is now EditAppSheet's write-back baseline: a
        // defaulted compression_level of 6 sitting in front of a config that
        // says 9 turns the user's nudge to 7 into a silent 9 → 7. Failing the
        // decode is the only outcome that can't lie about the config, so each
        // of the four keys is required exactly like preferred_version and
        // delivery. The CLI's --list handler emits all four unconditionally.
        let complete = #"{"type":"app","name":"example","bundle_id":"com.example.app","preferred_version":"latest","tweaks":[],"display_name":null,"delivery":"sideload","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}"#
        // Sanity: the complete payload decodes, so each failure below is
        // caused by the one key that was dropped and nothing else.
        guard case .app = try BuildEvent.decode(line: complete) else {
            return XCTFail("expected the complete payload to decode")
        }

        for key in ["keep_bundle_id", "strip_extensions", "strip_supported_devices", "compression_level"] {
            var json = try XCTUnwrap(
                try JSONSerialization.jsonObject(with: Data(complete.utf8)) as? [String: Any]
            )
            json.removeValue(forKey: key)
            let line = try XCTUnwrap(
                String(data: JSONSerialization.data(withJSONObject: json), encoding: .utf8)
            )
            XCTAssertThrowsError(
                try BuildEvent.decode(line: line),
                "a payload without \(key) must not decode to a fabricated default"
            )
        }
    }

    @MainActor
    func test_removeApp_invokes_cli_and_reloads() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let removeScript = #"""
        printf '{"type":"app-removed","name":"delta","bundle_id":"com.delta.app"}\n'
        """#
        let reloadScript = #"""
        printf '{"type":"app","name":"example","bundle_id":"com.example.app","preferred_version":"latest","tweaks":[],"display_name":null,"delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}\n'
        """#
        let store = ConfigStore(runner: runner, cliArgs: ["-c", reloadScript])

        let removed = try await store.removeApp(
            appName: "delta",
            argsOverride: ["-c", removeScript]
        )

        XCTAssertEqual(removed?.name, "delta")
        XCTAssertEqual(store.apps.map(\.name), ["example"])
    }

    @MainActor
    func test_updateAppOptions_reports_changed_keys() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let updateScript = #"""
        printf '{"type":"app-updated","name":"example","changed":["compression_level","preferred_version"]}\n'
        """#
        let reloadScript = #"""
        printf '{"type":"app","name":"example","bundle_id":"com.example.app","preferred_version":"21.16.2","tweaks":[],"display_name":null,"delivery":"livecontainer","auto_profile":false,"compression_level":9,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true}\n'
        """#
        let store = ConfigStore(runner: runner, cliArgs: ["-c", reloadScript])

        let updated = try await store.updateAppOptions(
            appName: "example",
            argsOverride: ["-c", updateScript]
        )

        XCTAssertEqual(updated?.changed, ["compression_level", "preferred_version"])
        XCTAssertEqual(store.apps.first?.preferredVersion, "21.16.2")
    }

    @MainActor
    func test_updateAppOptions_sends_only_changed_flags() async throws {
        // Guard against updateAppOptions leaking a default/no-op flag (e.g.
        // --delivery) when the caller only changed one field — that would
        // silently rewrite fields the caller never touched.
        let dir = NSTemporaryDirectory()
        let capturePath = dir + "captured_args_\(UUID().uuidString).txt"
        let scriptPath = dir + "capture_args_\(UUID().uuidString).sh"
        let script = """
        #!/bin/sh
        for arg in "$@"; do printf '%s\\n' "$arg" >> "\(capturePath)"; done
        printf '{"type":"app-updated","name":"example","changed":["compression_level"]}\\n'
        """
        try script.write(toFile: scriptPath, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: scriptPath)
        defer {
            try? FileManager.default.removeItem(atPath: scriptPath)
            try? FileManager.default.removeItem(atPath: capturePath)
        }

        // cliArgs is only consulted by the post-update reload; give it a
        // single, easily-distinguished argument so it can't be confused with
        // the update call's own arguments in the capture file.
        let runner = CLIRunner(executable: URL(fileURLWithPath: scriptPath))
        let store = ConfigStore(runner: runner, cliArgs: ["--reload-marker"])

        _ = try await store.updateAppOptions(appName: "example", compressionLevel: 9)

        let captured = try String(contentsOfFile: capturePath, encoding: .utf8)
        let lines = captured.split(separator: "\n", omittingEmptySubsequences: true).map(String.init)
        XCTAssertEqual(
            Array(lines.prefix(6)),
            ["--json", "--set-app-options", "--app", "example", "--compression-level", "9"]
        )
    }

    @MainActor
    func test_updateAppOptions_throws_when_cli_drops_a_requested_field() async {
        // Regression test for the silent-data-loss class of bug: the CLI can
        // report success (exit 0, an `app-updated` event) while having quietly
        // declined to apply one of the fields the caller asked to change —
        // exactly what happened with `--delivery sideload` before the typer
        // default was fixed. The stub CLI here reports back only
        // "preferred_version" as changed even though the caller also asked
        // for "delivery"; updateAppOptions must catch that mismatch and throw
        // rather than returning normally.
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let updateScript = #"""
        printf '{"type":"app-updated","name":"example","changed":["preferred_version"]}\n'
        """#
        let reloadScript = #"""
        printf '{"type":"app","name":"example","bundle_id":"com.example.app","preferred_version":"21.16.2","tweaks":[],"display_name":null,"delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}\n'
        """#
        let store = ConfigStore(runner: runner, cliArgs: ["-c", reloadScript])

        do {
            _ = try await store.updateAppOptions(
                appName: "example",
                preferredVersion: "21.16.2",
                delivery: .sideload,
                argsOverride: ["-c", updateScript]
            )
            XCTFail("expected a throw when the CLI dropped a requested field")
        } catch {
            XCTAssertTrue(
                error.localizedDescription.contains("delivery"),
                "expected the dropped field name in the error, got: \(error.localizedDescription)"
            )
        }
    }

    @MainActor
    func test_removeApp_throws_with_cli_stderr() async {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let failScript = #"""
        printf "app 'ghost' not found\n" >&2; exit 2
        """#
        let store = ConfigStore(runner: runner)
        do {
            _ = try await store.removeApp(appName: "ghost", argsOverride: ["-c", failScript])
            XCTFail("expected a throw")
        } catch {
            XCTAssertTrue(error.localizedDescription.contains("not found"))
        }
    }

    // MARK: - buildingApp

    @MainActor
    func test_buildingApp_is_claimed_and_released() {
        let store = makeStore()
        XCTAssertNil(store.buildingApp)

        startBuild(store, "example")
        XCTAssertEqual(store.buildingApp, "example",
                       "the sidebar reads this to disable Remove while a build runs")

        store.endBuild(appName: "example")
        XCTAssertNil(store.buildingApp)
    }

    @MainActor
    func test_endBuild_only_releases_the_build_that_claimed_it() {
        let store = makeStore()

        // A build for `example` finishes late, after `delta` has already
        // claimed the slot — its deferred endBuild must not unmark delta, or
        // Remove would re-enable underneath a build that is still running.
        startBuild(store, "example")
        startBuild(store, "delta")
        store.endBuild(appName: "example")

        XCTAssertEqual(store.buildingApp, "delta",
                       "a stale endBuild must not clear a newer build's claim")

        store.endBuild(appName: "delta")
        XCTAssertNil(store.buildingApp)
    }

    // MARK: - encrypted binaries
    //
    // The finding lives here rather than in the detail pane's @State because
    // it has to outlive both the build that found it and an app switch. What
    // clears it is the point of this section: a *verdict* from the decryption
    // check, never the mere fact that a build was started.

    private func encrypted(_ app: String) -> EncryptedBinariesRecord {
        EncryptedBinariesRecord(
            app: app,
            count: 1,
            binaries: [.init(path: "Payload/Example.app/Example", cryptids: [1])]
        )
    }

    /// The decryption check reaching its verdict, as the build stream spells
    /// it: the CLI emits this stage's `end` whether or not it found anything.
    private var decryptionCheckEnded: BuildProgress {
        .stage(name: CLIContract.Stage.decryptionCheck, status: "end")
    }

    @MainActor
    private func note(_ record: EncryptedBinariesRecord, on store: ConfigStore) {
        store.note(.encrypted(record), forBuildOf: record.app)
    }

    /// The whole shape of the defect: the build that finds this *succeeds*, so
    /// nothing on the success path may clear it.
    @MainActor
    func test_encrypted_finding_survives_the_end_of_a_successful_build() {
        let store = makeStore()

        startBuild(store, "example")
        note(encrypted("example"), on: store)
        store.note(decryptionCheckEnded, forBuildOf: "example")
        store.note(.finished(ok: true, error: nil, elapsed: 84.3), forBuildOf: "example")
        store.endBuild(appName: "example")

        XCTAssertEqual(store.encryptedBinaries["example"]?.count, 1,
                       "cleared by the very green build-end that hides it")
    }

    @MainActor
    func test_encrypted_finding_is_per_app() {
        let store = makeStore()
        note(encrypted("example"), on: store)
        XCTAssertNotNil(store.encryptedBinaries["example"])
        XCTAssertNil(store.encryptedBinaries["delta"],
                     "one app's encrypted IPA must not warn on another's")
    }

    /// A check that ran and found nothing gets to answer for itself —
    /// otherwise the card outlives the decrypted IPA that fixed it.
    @MainActor
    func test_a_clean_decryption_check_clears_the_previous_finding() {
        let store = makeStore()
        note(encrypted("example"), on: store)

        startBuild(store, "example")
        store.note(decryptionCheckEnded, forBuildOf: "example")

        XCTAssertNil(store.encryptedBinaries["example"],
                     "the check ran over a decrypted IPA and said so")
    }

    /// The regression this round: clearing at `beginBuild` meant *starting*
    /// work counted as a verdict. A build that dies before stage 2.5 — the
    /// phone unplugged, the IPA download failing, a bad --provide-ipa path —
    /// has re-examined nothing, and the source IPA it would have checked is
    /// the same file it was before. Erasing the card there sends the user off
    /// to re-install the archived build with no warning that it crashes.
    @MainActor
    func test_a_build_that_dies_before_the_check_keeps_the_previous_finding() {
        let store = makeStore()
        note(encrypted("example"), on: store)

        startBuild(store, "example")
        store.note(.stage(name: "acquire", status: "start"), forBuildOf: "example")
        store.note(
            .finished(ok: false, error: "no iOS devices connected", elapsed: 0.4),
            forBuildOf: "example"
        )
        store.endBuild(appName: "example")

        XCTAssertEqual(store.encryptedBinaries["example"]?.count, 1,
                       "nothing re-examined the binary, so the warning stands")
    }

    /// "Skip decryption check" is the same story told structurally: the CLI
    /// emits no `verify` stage at all, so there is no verdict and nothing
    /// clears. Clearing here would hand back the original bug in one click —
    /// see the warning, assume the check is wrong, skip it, get a green build
    /// over an app that still crashes.
    @MainActor
    func test_a_build_that_skips_the_check_keeps_the_previous_finding() {
        let store = makeStore()
        note(encrypted("example"), on: store)

        startBuild(store, "example")
        store.note(.stage(name: "cyan", status: "end"), forBuildOf: "example")
        store.note(.finished(ok: true, error: nil, elapsed: 12.0), forBuildOf: "example")

        XCTAssertEqual(store.encryptedBinaries["example"]?.count, 1,
                       "nothing re-examined the binary, so the warning stands")
    }

    /// The ordering trap: `encrypted-binaries` arrives *before* the stage end
    /// that closes the check, so the all-clear must not wipe the finding the
    /// same build just made.
    @MainActor
    func test_the_stage_end_does_not_clear_this_builds_own_finding() {
        let store = makeStore()

        startBuild(store, "example")
        note(encrypted("example"), on: store)
        store.note(decryptionCheckEnded, forBuildOf: "example")

        XCTAssertEqual(store.encryptedBinaries["example"]?.count, 1)
    }

    /// …and the marker that makes that work is per-build, not permanent: the
    /// *next* build's clean check must still be able to clear it.
    @MainActor
    func test_a_later_clean_build_clears_a_finding_from_an_earlier_one() {
        let store = makeStore()

        startBuild(store, "example")
        note(encrypted("example"), on: store)
        store.note(decryptionCheckEnded, forBuildOf: "example")
        store.endBuild(appName: "example")

        startBuild(store, "example")
        store.note(decryptionCheckEnded, forBuildOf: "example")
        store.endBuild(appName: "example")

        XCTAssertNil(store.encryptedBinaries["example"],
                     "the source IPA was replaced and the new check found nothing")
    }

    /// Clearing is scoped to the app being built, like the finding itself.
    @MainActor
    func test_a_check_clears_only_its_own_apps_finding() {
        let store = makeStore()
        note(encrypted("example"), on: store)
        note(encrypted("delta"), on: store)

        startBuild(store, "example")
        store.note(decryptionCheckEnded, forBuildOf: "example")

        XCTAssertNil(store.encryptedBinaries["example"])
        XCTAssertNotNil(store.encryptedBinaries["delta"])
    }

    // MARK: - terminal build outcome
    //
    // AppDetailView is removed from the hierarchy on any sidebar-section
    // change, so a build still running when the user clicks "Devices"
    // finishes into a view whose @State has been deallocated. Everything the
    // stream callback writes there repaints nothing. These records are the
    // half that has somewhere to land.

    @MainActor
    func test_a_failed_build_is_recorded_where_a_torn_down_pane_cannot_hide_it() {
        let store = makeStore()

        startBuild(store, "example")
        store.note(
            .finished(ok: false, error: "cyan failed: no such file", elapsed: 3.0),
            forBuildOf: "example"
        )
        store.endBuild(appName: "example")

        XCTAssertEqual(store.buildFailures["example"], "cyan failed: no such file")
    }

    /// The failure the event stream never explains — the CLI raising ahead of
    /// `build-start`, so `handle(event:)` fires zero times and the only
    /// account is stderr.
    @MainActor
    func test_a_failure_with_no_events_at_all_is_still_recorded() {
        let store = makeStore()

        startBuild(store, "example")
        store.noteBuildFailure(
            appName: "example",
            message: "no iOS devices connected — hint: plug the iPhone in via USB"
        )
        store.endBuild(appName: "example")

        XCTAssertEqual(
            store.buildFailures["example"],
            "no iOS devices connected — hint: plug the iPhone in via USB"
        )
    }

    /// Deliberately *no* `beginBuild` between the failure and the green
    /// finish. This test had one, and it made the test useless: `beginBuild`
    /// clears the record too, so the assertion below passed no matter what
    /// `.finished` did — it survived a mutation that deleted the clearing
    /// outright. The premise assertion is what keeps it honest if somebody
    /// puts a `beginBuild` back.
    @MainActor
    func test_a_successful_build_clears_the_previous_failure() {
        let store = makeStore()
        store.noteBuildFailure(appName: "example", message: "cyan failed")
        XCTAssertNotNil(store.buildFailures["example"],
                        "premise: there has to be a failure for the green build to clear")

        store.note(.finished(ok: true, error: nil, elapsed: 84.3), forBuildOf: "example")

        XCTAssertNil(store.buildFailures["example"],
                     "a red card under a green build is the failure being reported twice")
    }

    /// A build in flight must not carry the last one's red card: the header
    /// says "running" and a stale message under it reads as this attempt's.
    @MainActor
    func test_starting_a_build_clears_the_previous_failure() {
        let store = makeStore()
        store.noteBuildFailure(appName: "example", message: "cyan failed")

        startBuild(store, "example")

        XCTAssertNil(store.buildFailures["example"])
    }

    /// Two apps can each have failed, and neither recording nor clearing may
    /// touch the other one's row.
    ///
    /// The first version of this recorded only one failure, so a
    /// `buildFailures = [appName: message]` that wiped every other app read
    /// exactly the same — there was no other app to wipe. Both apps have to be
    /// in the dictionary before anything is asserted about scope.
    @MainActor
    func test_a_build_failure_is_per_app() {
        let store = makeStore()
        store.noteBuildFailure(appName: "example", message: "cyan failed")
        store.noteBuildFailure(appName: "delta", message: "no iOS devices connected")

        XCTAssertEqual(store.buildFailures["example"], "cyan failed",
                       "recording delta's failure must not drop example's")
        XCTAssertEqual(store.buildFailures["delta"], "no iOS devices connected")

        store.note(.finished(ok: true, error: nil, elapsed: 1.0), forBuildOf: "delta")

        XCTAssertNil(store.buildFailures["delta"])
        XCTAssertEqual(store.buildFailures["example"], "cyan failed",
                       "another app's successful build must not clear this one's failure")
    }

    // MARK: - delivery badge refresh

    /// The re-install sheet re-enables Close the moment `build-end` lands, and
    /// dismissing it cancels the `.task` the refresh was running in — while
    /// `--list` is still ~250–300 ms from answering. `refreshDeliveryStates`
    /// swallows cancellation by design, so the badges silently kept whatever
    /// they said before the push: a green "current" over a phone holding an
    /// older build.
    @MainActor
    func test_a_detached_refresh_survives_its_caller_being_cancelled() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        // Slow enough that the cancel below lands mid-flight, the way a click
        // on Close does.
        let script = #"""
        sleep 0.4
        printf '{"type":"app","name":"example","bundle_id":"com.example.app","preferred_version":"latest","tweaks":[],"display_name":null,"delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}\n{"type":"delivery-state","app":"example","kind":"stale","summary":"Changed since the last delivery","reasons":["rebuilt since delivery"],"pushed_at":"2026-02-01T10:15:00"}\n'
        """#
        let store = ConfigStore(runner: runner, cliArgs: ["-c", script])

        // Stands in for ReinstallSheet's `.task`: it starts the refresh and is
        // then cancelled by the sheet being dismissed.
        final class Handle: @unchecked Sendable { var task: Task<Void, Never>? }
        let handle = Handle()
        let sheetTask = Task { @MainActor in
            handle.task = store.refreshDeliveryStatesDetached()
            try? await Task.sleep(nanoseconds: 10_000_000_000)
        }
        while handle.task == nil { await Task.yield() }
        sheetTask.cancel()

        await handle.task?.value

        XCTAssertEqual(
            store.deliveryStates["example"]?.kind, .stale,
            "the push already happened — reading back what it did is not the "
            + "sheet's to cancel"
        )
        XCTAssertNil(store.lastError, "a completed refresh has nothing to report")
    }

    // MARK: - per-app records are bounded by the app list
    //
    // `deliveryStates` is rebuilt wholesale by `loadApps`, so a removed app
    // drops out of it for free. `encryptedBinaries`, `buildFailures` and the
    // per-build marker behind them were purged by nothing at all — and
    // `removeApp` → `mutate` → `reload()` → `loadApps` is the path that has to
    // do it, because re-adding under the same YAML key is the ordinary
    // fix-a-botched-add loop and the CLI dedups straight back to that key.

    /// The card the last round moved onto the store so it would outlive the
    /// pane — which is worth nothing if it also outlives the app.
    @MainActor
    func test_a_removed_app_takes_its_build_failure_with_it() async throws {
        let counter = Self.counterFile()
        defer { try? FileManager.default.removeItem(at: counter) }
        let store = ConfigStore(
            runner: shRunner(),
            cliArgs: listings([["example", "delta"], ["delta"]], counter: counter)
        )
        _ = try await store.loadApps()
        store.noteBuildFailure(appName: "example", message: "cyan failed: no such file")
        XCTAssertNotNil(store.buildFailures["example"], "premise: there is a card to strand")

        // example is gone from config.yaml; the next `--list` says so, and
        // `removeApp` → `mutate` → `reload()` is what runs it.
        _ = try await store.loadApps()

        XCTAssertEqual(store.apps.map(\.name), ["delta"], "premise: the app is gone")
        XCTAssertNil(
            store.buildFailures["example"],
            "re-add under the same key and this red card describes the app that was removed"
        )
    }

    @MainActor
    func test_a_removed_app_takes_its_encrypted_finding_with_it() async throws {
        let counter = Self.counterFile()
        defer { try? FileManager.default.removeItem(at: counter) }
        let store = ConfigStore(
            runner: shRunner(),
            cliArgs: listings([["example", "delta"], ["delta"]], counter: counter)
        )
        _ = try await store.loadApps()
        note(encrypted("example"), on: store)
        XCTAssertNotNil(store.encryptedBinaries["example"], "premise: there is a finding to strand")

        _ = try await store.loadApps()

        XCTAssertNil(
            store.encryptedBinaries["example"],
            #""will crash on launch" about an IPA that belonged to a different app"#
        )
    }

    /// The whole loop, because "remove it and add it again" is what someone
    /// does about a botched add — and the CLI dedups the re-add back to the
    /// same YAML key, which is what turns two apps' records into one app's.
    ///
    /// There is deliberately no test for `encryptedReportedThisBuild` being
    /// purged alongside these two. It is per-build state that `beginBuild`
    /// already clears per app, so no sequence the UI can reach observes a
    /// stale one: a test would pass with the purge deleted, which is the
    /// failure mode this round exists to stop shipping.
    @MainActor
    func test_a_reused_config_key_does_not_inherit_the_previous_apps_cards() async throws {
        let counter = Self.counterFile()
        defer { try? FileManager.default.removeItem(at: counter) }
        let store = ConfigStore(
            runner: shRunner(),
            cliArgs: listings(
                [["example", "delta"], ["delta"], ["example", "delta"]], counter: counter
            )
        )
        _ = try await store.loadApps()
        store.noteBuildFailure(appName: "example", message: "cyan failed: no such file")
        note(encrypted("example"), on: store)

        _ = try await store.loadApps()          // example removed
        _ = try await store.loadApps()          // …and re-added under the same key
        XCTAssertEqual(store.apps.map(\.name), ["delta", "example"], "premise: it is back")

        XCTAssertNil(
            store.buildFailures["example"],
            "the new app's pane opens on a red card about a build it never ran"
        )
        XCTAssertNil(
            store.encryptedBinaries["example"],
            "…and a 'will crash on launch' warning about an IPA it never had"
        )
    }

    /// Records for apps that are still listed must survive a reload, or the
    /// purge becomes "any `--list` clears every card" — worse than the bug,
    /// since every build ends with an unconditional `refreshDeliveryStates()`.
    @MainActor
    func test_a_reload_keeps_the_records_of_apps_that_are_still_listed() async throws {
        let counter = Self.counterFile()
        defer { try? FileManager.default.removeItem(at: counter) }
        let store = ConfigStore(
            runner: shRunner(),
            cliArgs: listings([["example", "delta"]], counter: counter)
        )
        _ = try await store.loadApps()
        store.noteBuildFailure(appName: "example", message: "cyan failed")
        note(encrypted("delta"), on: store)

        // What `refreshDeliveryStates()` does at the end of every build.
        _ = try await store.loadApps()

        XCTAssertEqual(store.buildFailures["example"], "cyan failed")
        XCTAssertNotNil(store.encryptedBinaries["delta"])
    }

    // MARK: - Fixtures

    private static func counterFile() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-listing-\(UUID().uuidString)")
    }

    /// One argv whose successive runs answer with successive `stages`, so a
    /// single store can watch an app disappear and come back under the same
    /// key. The marker-file idiom `DeliveryStateTests` uses, generalised past
    /// two answers; the last stage repeats for any run beyond it.
    private func listings(_ stages: [[String]], counter: URL) -> [String] {
        func listing(_ names: [String]) -> String {
            let events = names.map { name in
                #"{"type":"app","name":"\#(name)","bundle_id":"com.example.\#(name)","preferred_version":"latest","tweaks":[],"display_name":null,"delivery":"sideload","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}"#
            }
            return "printf '" + events.joined(separator: #"\n"#) + #"\n'"#
        }
        let branches = stages.enumerated().map { index, names in
            // The last stage catches every run past it, so a test that reloads
            // once more than it planned to sees the final list rather than
            // silently nothing.
            let pattern = index == stages.count - 1 ? "*" : String(index)
            return "\(pattern)) \(listing(names));;"
        }
        return ["-c", """
        n=$(cat '\(counter.path)' 2>/dev/null || echo 0)
        echo $((n+1)) > '\(counter.path)'
        case $n in
        \(branches.joined(separator: "\n"))
        esac
        """]
    }

    // MARK: - Telling the user a build ended

    /// The one outcome that must produce no banner. The user stopped it
    /// themselves, seconds ago, with a confirmation sheet in front of them —
    /// a notification is the app telling them what they just did.
    func test_a_stopped_build_is_not_worth_a_notification() {
        XCTAssertNil(
            BuildNotifier.note(app: "example", outcome: .cancelled, delivery: .livecontainer)
        )
    }

    /// A livecontainer push writes a file into Documents/ and installs
    /// nothing: the import is manual and this tool cannot observe it (§3). So
    /// the banner names the next action rather than claiming an outcome —
    /// "installed" here would send the user away with the tweak not loaded.
    func test_a_livecontainer_build_says_what_is_left_to_do() throws {
        let note = try XCTUnwrap(
            BuildNotifier.note(
                app: "example", outcome: .succeeded(elapsed: 84.3), delivery: .livecontainer
            )
        )
        XCTAssertEqual(note.title, "example built ✓")
        XCTAssertEqual(note.body, "Done in 84.3s — Open LiveContainer and import it to finish.")
    }

    /// A sideload really is installed by the time the build ends, so the same
    /// sentence would be wrong in the other direction.
    func test_a_sideload_build_says_it_is_installed() throws {
        let note = try XCTUnwrap(
            BuildNotifier.note(
                app: "delta", outcome: .succeeded(elapsed: 12), delivery: .sideload
            )
        )
        XCTAssertEqual(note.body, "Done in 12.0s — Installed on the device.")
    }

    /// `.unknown` is the notifier saying it doesn't know, and the banner has to
    /// be able to represent that — the same rule `BuildProgress.start`'s
    /// optional delivery encodes. Inventing either answer here is a claim
    /// about a phone nothing looked at.
    func test_an_unknown_delivery_claims_neither_outcome() throws {
        let note = try XCTUnwrap(
            BuildNotifier.note(app: "bravo", outcome: .succeeded(elapsed: nil), delivery: .unknown)
        )
        XCTAssertEqual(note.body, "Finished.")
        XCTAssertFalse(note.body.contains("LiveContainer"))
        XCTAssertFalse(note.body.contains("Installed"))
    }

    /// A CLI failure message can carry a multi-line hint. The notification
    /// body truncates whatever it is handed mid-word, so the first line is cut
    /// deliberately rather than left to the system.
    func test_a_failure_banner_carries_one_line_of_the_error() throws {
        let note = try XCTUnwrap(
            BuildNotifier.note(
                app: "example",
                outcome: .failed(message: "cyan failed: no such file\n  hint: run --provide-ipa"),
                delivery: .livecontainer
            )
        )
        XCTAssertEqual(note.title, "example build failed")
        XCTAssertEqual(note.body, "cyan failed: no such file")
    }

    func test_a_failure_with_no_message_still_says_something() throws {
        let note = try XCTUnwrap(
            BuildNotifier.note(app: "example", outcome: .failed(message: "   "), delivery: .unknown)
        )
        XCTAssertEqual(note.body, "No error message was recorded.")
    }

    // MARK: - Stopping a build

    /// The defect this whole path exists to avoid: a killed CLI exits non-zero
    /// with whatever it wrote to stderr, which at the point the pane reads it
    /// is indistinguishable from a build that broke. Without the flag, pressing
    /// Stop paints a red Retry and a "Last build failed" card for something the
    /// user deliberately did a second earlier.
    @MainActor
    func test_a_stopped_build_is_distinguishable_from_a_failed_one() {
        let store = makeStore()
        startBuild(store, "example")

        store.cancelBuild()

        XCTAssertTrue(store.wasCancelled("example"))
        XCTAssertFalse(store.wasCancelled("delta"), "the flag names one build, not any build")
    }

    /// Both confirmations can sit open across the end of one build and the
    /// start of another — the menu that opened them was a snapshot. A Stop
    /// that then killed whatever happened to be running would be the most
    /// destructive action in the app firing on a target the user never chose.
    /// Same re-check as `SidebarView.remove`.
    @MainActor
    func test_stopping_a_build_that_already_ended_does_not_kill_its_successor() {
        let store = makeStore()
        startBuild(store, "example")
        store.endBuild(appName: "example")
        // The user's confirmation was still on screen while this started.
        startBuild(store, "delta")

        store.cancelBuild(appName: "example")

        XCTAssertFalse(store.wasCancelled("delta"), "stopped a build the user never selected")
        XCTAssertFalse(store.wasCancelled("example"))
    }

    /// A cancel must not outlive the build it stopped. Left standing, the next
    /// build reports itself as cancelled the moment it ends — however it ended
    /// — so a genuine failure would land with no card and no banner.
    @MainActor
    func test_the_next_build_is_not_inheriting_the_last_ones_cancel() {
        let store = makeStore()
        startBuild(store, "example")
        store.cancelBuild()
        XCTAssertTrue(store.wasCancelled("example"), "premise")

        startBuild(store, "example")

        XCTAssertFalse(store.wasCancelled("example"))
    }

    /// A stopped build clears the failure card rather than writing one — the
    /// user knows, they pressed the button, and "Last build failed" is both
    /// alarming and less use than the log line already on screen.
    /// A stop must not erase the reason a build was already failing. The user
    /// stopped something that was breaking anyway, and that reason is the
    /// useful thing left on screen — clearing it leaves an empty pane over a
    /// build that genuinely broke.
    @MainActor
    func test_stopping_a_failing_build_keeps_the_reason_it_failed() {
        let store = makeStore()
        startBuild(store, "example")
        store.note(
            .finished(ok: false, error: "cyan failed: no such file", elapsed: nil),
            forBuildOf: "example"
        )

        store.noteBuildCancelled(appName: "example")

        XCTAssertEqual(store.buildFailures["example"], "cyan failed: no such file")
        XCTAssertTrue((store.lastStop(of: "example") != nil))
    }

    /// The marker, not a nil `buildFailures` entry, is what suppresses the
    /// card. Nilling it did not remove the card at all — `buildFailureMessage`
    /// falls through to `lastBuild` — it revealed the *previous* build's
    /// failure under this build's "Stopped."
    @MainActor
    func test_a_stopped_build_is_marked_rather_than_having_its_record_wiped() {
        let store = makeStore()
        startBuild(store, "example")
        store.noteBuildCancelled(appName: "example")
        XCTAssertTrue((store.lastStop(of: "example") != nil))

        startBuild(store, "example")
        XCTAssertFalse((store.lastStop(of: "example") != nil), "a new build is not the last one's stop")
    }

    /// The confirmation has to say what stopping *now* costs, and that changes
    /// with the stage. Getting it wrong in the reassuring direction is the bad
    /// one: telling the user nothing has reached the device while the CLI is
    /// mid-write into LiveContainer's Documents leaves a partial .ipa there
    /// that they will then import.
    ///
    /// Driven one delivery mode at a time, because a build does `lc-push` OR
    /// `install` — never both. An earlier version of this test fed it both in
    /// sequence, which no real build produces, and so asserted a behaviour the
    /// state machine has no reason to have.
    @MainActor
    func test_the_cancel_warning_names_the_livecontainer_risk() {
        let store = makeStore()
        startBuild(store, "example")
        XCTAssertTrue(store.cancelConsequence.contains("Nothing has reached the device"))

        store.note(
            .stage(name: CLIContract.Stage.liveContainerPush, status: "start"),
            forBuildOf: "example"
        )
        XCTAssertTrue(
            store.cancelConsequence.contains("LiveContainer"),
            "mid-push, got: \(store.cancelConsequence)"
        )
        XCTAssertFalse(store.cancelConsequence.contains("Nothing has reached the device"))
    }

    /// S7: the warning used to say graft is killed outright and leaves its
    /// temp directory behind. Neither is true now: the whole build runs under
    /// `lc_manage.exit_on_termination`, so the Stop's SIGTERM unwinds it and
    /// `_build_one`'s `finally` removes `tmp_root`. What is left is the one
    /// way it can still be left behind — cleanup outlasting `CLIRunner`'s
    /// grace before SIGKILL (`cliStopGrace`).
    @MainActor
    func test_the_cancel_warning_says_graft_removes_its_temp_files() {
        let store = makeStore()
        startBuild(store, "example")
        let warning = store.cancelConsequence
        XCTAssertFalse(warning.contains("killed outright"), warning)
        XCTAssertTrue(warning.contains("removes its temp files"), warning)
        XCTAssertTrue(warning.contains("\(Int(cliStopGrace)) seconds"), "names the grace: \(warning)")
    }

    @MainActor
    func test_the_cancel_warning_names_the_sideload_risk() {
        let store = makeStore()
        startBuild(store, "delta")
        store.note(
            .stage(name: CLIContract.Stage.sideloadInstall, status: "start"),
            forBuildOf: "delta"
        )
        XCTAssertTrue(
            store.cancelConsequence.contains("installing on the device"),
            "mid-install, got: \(store.cancelConsequence)"
        )
    }

    /// An `end` event must clear the stage, not just `endBuild`.
    ///
    /// This test previously called `endBuild` and never sent a `status: "end"`
    /// at all — so it passed with the end-handling entirely absent, which is
    /// exactly how the bug shipped. After `lc-push end` the CLI still runs
    /// `_record_delivery_if_pushed`, retention pruning and the history write
    /// before `build-end`; for that whole window the warning went on telling
    /// the user to delete an .ipa that had been pushed correctly.
    @MainActor
    func test_a_finished_stage_stops_being_the_current_one() {
        let store = makeStore()
        startBuild(store, "example")
        store.note(
            .stage(name: CLIContract.Stage.liveContainerPush, status: "start"),
            forBuildOf: "example"
        )
        XCTAssertEqual(store.buildStage, CLIContract.Stage.liveContainerPush, "premise")

        store.note(
            .stage(name: CLIContract.Stage.liveContainerPush, status: "end"),
            forBuildOf: "example"
        )

        XCTAssertNil(store.buildStage, "the push finished; the warning must stop naming it")
        // It must stop naming the *mid-write* risk — but not swing to claiming
        // the phone is untouched, which is what this assertion originally said
        // and what finding 7 caught. A completed push is still a delivery.
        XCTAssertFalse(store.cancelConsequence.contains("Stopping mid-write"))
        XCTAssertFalse(store.cancelConsequence.contains("Nothing has reached the device"))
    }

    /// An ordinary stage ending is the case that *does* mean nothing reached
    /// the device — the pair above only holds for a device-touching stage.
    @MainActor
    func test_an_ordinary_stage_ending_still_reads_as_untouched() {
        let store = makeStore()
        startBuild(store, "example")
        store.note(.stage(name: "cyan", status: "start"), forBuildOf: "example")
        store.note(.stage(name: "cyan", status: "end"), forBuildOf: "example")

        XCTAssertTrue(
            store.cancelConsequence.contains("Nothing has reached the device"),
            "got: \(store.cancelConsequence)"
        )
    }

    /// A late `end` from a stage the CLI has already moved past must not blank
    /// the one now running — that would under-warn, which is the dangerous
    /// direction.
    @MainActor
    func test_a_stale_stage_end_cannot_clear_the_current_stage() {
        let store = makeStore()
        startBuild(store, "example")
        store.note(.stage(name: "cyan", status: "start"), forBuildOf: "example")
        store.note(
            .stage(name: CLIContract.Stage.liveContainerPush, status: "start"),
            forBuildOf: "example"
        )

        store.note(.stage(name: "cyan", status: "end"), forBuildOf: "example")

        XCTAssertEqual(store.buildStage, CLIContract.Stage.liveContainerPush)
        XCTAssertTrue(store.cancelConsequence.contains("LiveContainer"))
    }

    /// A refusal the user cannot see is worse than no button. The confirmation
    /// warned that stopping may leave a partial .ipa on the phone; if the build
    /// finished while that sheet was open, silence leaves the user believing it
    /// did — and the next thing they do is delete a file that arrived intact.
    @MainActor
    func test_stopping_a_finished_build_says_so_instead_of_doing_nothing() {
        let store = makeStore()
        store.launchBuild(appName: "example", delivery: .livecontainer) {}
        store.endBuild(appName: "example")

        store.cancelBuild(appName: "example")

        let reported = store.lastError ?? ""
        XCTAssertTrue(reported.contains("already finished"), "got: \(reported)")
        XCTAssertTrue(reported.contains("intact"), "got: \(reported)")
    }

    @MainActor
    func test_stopping_the_wrong_app_says_which_one_was_left_alone() {
        let store = makeStore()
        store.launchBuild(appName: "delta", delivery: .livecontainer) {}

        store.cancelBuild(appName: "example")

        let reported = store.lastError ?? ""
        XCTAssertTrue(reported.contains("delta"), "got: \(reported)")
        XCTAssertFalse(store.wasCancelled("delta"))
    }

    /// The two tenses of the same fact live on one type so they cannot drift.
    /// They were authored in two files before this — `cancelConsequence` wrote
    /// the warning, `noteCancelled` re-wrote the aftermath behind a
    /// `costlyToCancel.contains` guard plus a two-way ternary that would have
    /// silently given a third risky stage the sideload sentence.
    func test_every_risky_stage_has_both_tenses_and_is_declared_costly() {
        for risk in DeviceRisk.allCases {
            XCTAssertFalse(risk.warning.isEmpty, "\(risk) has no warning")
            XCTAssertFalse(risk.aftermath.isEmpty, "\(risk) has no aftermath")
            XCTAssertNotEqual(risk.warning, risk.aftermath)
            XCTAssertTrue(
                CLIContract.Stage.dependedOn.contains(risk.stageName),
                "\(risk.stageName) carries device advice but isn't declared costly"
            )
        }
        XCTAssertNil(DeviceRisk(stage: "cyan"), "an ordinary stage must carry no device risk")
        XCTAssertNil(DeviceRisk(stage: nil))
    }

    // MARK: - One device check at a time

    /// Two concurrent `--reconcile` processes race on `log.sqlite`, and
    /// `mark_reconciled` is a write — the loser surfaces as "database is
    /// locked", an alarming line about a check that was only redundant. The
    /// slot lives on the store because the pane's `@State` was reset by every
    /// app switch, so clicking away and back re-enabled the button.
    @MainActor
    func test_only_one_device_check_runs_at_a_time() {
        let store = makeStore()
        XCTAssertTrue(store.beginDeviceCheck(appName: "example"))
        XCTAssertFalse(store.beginDeviceCheck(appName: "example"))
        XCTAssertFalse(store.beginDeviceCheck(appName: "delta"))

        store.endDeviceCheck(appName: "example")
        XCTAssertTrue(store.beginDeviceCheck(appName: "delta"))
    }

    /// "A build and a device check may not run at once" is a store invariant,
    /// and it used to live only in three `.disabled(...)` modifiers across two
    /// views — so the store itself would start either during the other, and no
    /// test could express the rule because it wasn't there.
    @MainActor
    func test_a_build_and_a_device_check_exclude_each_other() {
        let store = makeStore()
        startBuild(store, "example")
        XCTAssertFalse(store.beginDeviceCheck(appName: "example"), "checked during a build")
        XCTAssertNotNil(store.busy)

        store.endBuild(appName: "example")
        XCTAssertTrue(store.beginDeviceCheck(appName: "example"))
        XCTAssertFalse(
            store.launchBuild(appName: "example", delivery: .unknown) {},
            "built during a device check"
        )
        XCTAssertNotNil(store.busy)

        store.endDeviceCheck(appName: "example")
        XCTAssertNil(store.busy)
    }

    /// Only the check that claimed the slot may release it, so a late finisher
    /// cannot free a slot a newer check is holding.
    @MainActor
    func test_a_late_check_does_not_release_someone_elses_slot() {
        let store = makeStore()
        store.beginDeviceCheck(appName: "example")
        store.endDeviceCheck(appName: "delta")
        XCTAssertEqual(store.checkingApp, "example")
    }

    /// The decryption check's all-clear used to live in a `where`-guarded case
    /// that a second `.stage` case could never have run beside. Folding stage
    /// tracking into the same case is only correct if the verdict still lands.
    @MainActor
    func test_tracking_the_stage_did_not_cost_the_decryption_verdict() {
        let store = makeStore()
        startBuild(store, "example")
        store.note(
            .encrypted(EncryptedBinariesRecord(
                app: "example", count: 1,
                binaries: [.init(path: "Payload/Example.app/Example", cryptids: [1])]
            )),
            forBuildOf: "example"
        )
        XCTAssertNotNil(store.encryptedBinaries["example"], "premise")

        // A *different* app's clean check must not clear example's finding,
        // and example's own must — the same pair the guarded case encoded.
        store.note(
            .stage(name: CLIContract.Stage.decryptionCheck, status: "end"),
            forBuildOf: "delta"
        )
        XCTAssertNotNil(store.encryptedBinaries["example"])

        startBuild(store, "example")
        store.note(
            .stage(name: CLIContract.Stage.decryptionCheck, status: "end"),
            forBuildOf: "example"
        )
        XCTAssertNil(store.encryptedBinaries["example"], "a clean check must clear the finding")
    }

    // MARK: - Stop actually kills the subprocess

    /// The half of Stop that every other test in this file was missing.
    ///
    /// The three cancel tests above assert on `wasCancelled` and
    /// `buildFailures` — bookkeeping. Delete `buildTask?.cancel()` from
    /// `cancelBuild` and every one of them still passed, while Stop became a
    /// no-op: the CLI runs to completion, signs, pushes the IPA to the phone,
    /// and the pane paints "Stopped." over a build that installed. This spawns
    /// a real long-running child through the real runner and asserts it dies.
    @MainActor
    func test_stopping_a_build_kills_the_subprocess() async throws {
        let store = makeStore()
        // A marker the child only writes if it is allowed to finish sleeping.
        let marker = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-survived-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: marker) }

        // The child announces itself rather than the test guessing with a
        // sleep. That matters twice: it cuts ~1.5s of wall clock to a few ms,
        // and it is stronger evidence — an emitted event proves the child
        // reached its own code, where a timer only assumed it. The distinction
        // is load-bearing: cancelling before the child launches takes a
        // completely different path (`ProcessHandle.adopt` refuses and no
        // process is ever forked), which would prove nothing about the kill.
        let started = expectation(description: "subprocess is running")
        let ended = expectation(description: "stream returned")
        var exitCode: Int32?

        store.launchBuild(appName: "example", delivery: .unknown) {
            do {
                let result = try await store.runner.stream(
                    arguments: ["-c",
                        #"printf '{"type":"log","source":"info","line":"running"}\n'; "#
                        + "sleep 30; touch '\(marker.path)'"]
                ) { _ in started.fulfill() }
                exitCode = result.exitCode
            } catch {
                exitCode = -1
            }
            ended.fulfill()
        }
        await fulfillment(of: [started], timeout: 5)

        store.cancelBuild(appName: "example")

        // 30s sleep, so anything under that proves the kill landed. SIGTERM is
        // immediate; the SIGKILL escalation is at +2s.
        await fulfillment(of: [ended], timeout: 5)
        XCTAssertNotNil(exitCode, "the stream never returned — the child outlived the cancel")
        XCTAssertNotEqual(exitCode, 0, "a killed child cannot exit cleanly")
        XCTAssertFalse(
            FileManager.default.fileExists(atPath: marker.path),
            "the subprocess ran to completion — Stop did not reach it"
        )
    }

    /// The slot is claimed synchronously, so the second of two activations in
    /// the same main-actor turn cannot start a build. Claimed inside the Task
    /// body instead, `buildTask` was reassigned to the newcomer and the first
    /// Task's handle was dropped — two CLI processes signing and pushing the
    /// same app, with Stop able to reach only one.
    @MainActor
    func test_a_second_launch_in_the_same_turn_is_refused() {
        let store = makeStore()
        var ran = 0
        let first = store.launchBuild(appName: "example", delivery: .unknown) { ran += 1 }
        let second = store.launchBuild(appName: "example", delivery: .unknown) { ran += 1 }

        XCTAssertTrue(first)
        XCTAssertFalse(second, "a double-click started a second concurrent build")
        XCTAssertEqual(store.buildingApp, "example")
    }

    // MARK: - What the banner says a build did

    /// The defect the review caught and my own dry-run testing reproduced: the
    /// banner read the app's *configured* delivery mode, so a run with
    /// `--dry-run` or `--skip-install` — both of which the CLI honours by
    /// skipping the push while still emitting `build-end {ok: true}` —
    /// announced "Open LiveContainer and import it to finish." for a run that
    /// sent nothing. The user then imports the previous build's .ipa.
    func test_a_dry_run_banner_does_not_claim_a_delivery() throws {
        var options = BuildOptions()
        options.dryRun = true
        let note = try XCTUnwrap(
            BuildNotifier.note(
                app: "example",
                outcome: .succeeded(elapsed: 2.1),
                delivery: options.effectiveDelivery(for: .livecontainer)
            )
        )
        XCTAssertEqual(note.body, "Done in 2.1s — Dry run — nothing was sent to the device.")
        XCTAssertFalse(note.body.contains("import"), "claimed a push that never happened")
    }

    func test_skip_install_banner_does_not_claim_an_install() throws {
        var options = BuildOptions()
        options.skipInstall = true
        let note = try XCTUnwrap(
            BuildNotifier.note(
                app: "delta",
                outcome: .succeeded(elapsed: nil),
                delivery: options.effectiveDelivery(for: .sideload)
            )
        )
        XCTAssertEqual(note.body, "Install skipped — nothing was sent to the device.")
        XCTAssertFalse(note.body.contains("Installed"))
    }

    /// Only an unmodified run may describe a delivery.
    func test_a_plain_build_still_names_the_next_step() {
        XCTAssertEqual(
            BuildOptions().effectiveDelivery(for: .livecontainer), .livecontainer
        )
        XCTAssertEqual(BuildOptions().effectiveDelivery(for: .sideload), .sideload)
        // An option that changes nothing about delivery must not suppress it.
        var options = BuildOptions()
        options.skipDecryptCheck = true
        XCTAssertEqual(options.effectiveDelivery(for: .livecontainer), .livecontainer)
    }

    /// The store must carry the *launch-time* answer through to the banner,
    /// not re-derive it from config when the build ends.
    @MainActor
    func test_the_banner_uses_the_options_the_build_was_launched_with() async throws {
        let store = makeStore()
        var posted: [BuildNotifier.Note] = []
        store.postNote = { if let note = $0 { posted.append(note) } }

        var options = BuildOptions()
        options.dryRun = true
        store.launchBuild(
            appName: "example", delivery: options.effectiveDelivery(for: .livecontainer)
        ) {}
        store.note(.finished(ok: true, error: nil, elapsed: 2.0), forBuildOf: "example")

        XCTAssertEqual(posted.count, 1)
        XCTAssertEqual(
            posted.first?.body, "Done in 2.0s — Dry run — nothing was sent to the device."
        )
    }

    /// One build, one banner — even when a non-zero exit follows a `build-end`
    /// that already reported success.
    @MainActor
    func test_one_build_posts_at_most_one_banner() {
        let store = makeStore()
        var posted: [BuildNotifier.Note] = []
        store.postNote = { if let note = $0 { posted.append(note) } }

        store.launchBuild(appName: "example", delivery: .livecontainer) {}
        store.note(.finished(ok: true, error: nil, elapsed: 1.0), forBuildOf: "example")
        store.noteBuildFailure(appName: "example", message: "retention sweep failed")

        XCTAssertEqual(posted.count, 1, "the same build banner'd twice, with contradictory news")
        XCTAssertEqual(posted.first?.title, "example built ✓")
    }

    /// A stopped build says nothing: the user did it, seconds ago, with a
    /// confirmation sheet in front of them.
    @MainActor
    func test_a_stopped_build_posts_no_banner() {
        let store = makeStore()
        var posted: [BuildNotifier.Note] = []
        store.postNote = { if let note = $0 { posted.append(note) } }

        store.launchBuild(appName: "example", delivery: .livecontainer) {}
        store.cancelBuild(appName: "example")
        // The kill lands and the CLI's dying `build-end` still arrives.
        store.note(.finished(ok: false, error: "terminated", elapsed: nil), forBuildOf: "example")

        XCTAssertTrue(posted.isEmpty, "banner'd a build the user stopped themselves")
    }

    /// Start a build the way production does.
    ///
    /// `beginBuild` is private now: `launchBuild` is the only door, because it
    /// is the one that claims the slot synchronously and captures the run's
    /// effective delivery. A test calling `beginBuild` directly was bypassing
    /// both — inheriting the previous build's delivery and skipping the refusal
    /// that stops a double-click starting two builds.
    @discardableResult
    @MainActor
    private func startBuild(
        _ store: ConfigStore, _ appName: String,
        delivery: EffectiveDelivery = .unknown
    ) -> Bool {
        // Production releases the slot in `runBuildProcess`'s `defer` before
        // any next build; mirror that rather than letting the claim refuse.
        if let running = store.buildingApp { store.endBuild(appName: running) }
        return store.launchBuild(appName: appName, delivery: delivery) {}
    }

    /// The stub every test that never actually spawns the CLI uses: these
    /// drive `note`, `beginBuild` and friends directly, and `ConfigStore`
    /// simply requires a runner. Named after `DeliveryStateTests.shRunner`.
    private func shRunner() -> CLIRunner {
        CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
    }

    @MainActor
    private func makeStore() -> ConfigStore {
        ConfigStore(runner: shRunner())
    }
}

// MARK: - Review round 2: what the cancel warning may claim

extension ConfigStoreTests {

    /// `--dry-run` does NOT imply `--skip-install`; the CLI still enters the
    /// push branch and still emits `lc-push start`, passing `dry_run` down so
    /// nothing is written. Reading the stage alone told a dry-run user to go
    /// and delete a file a previous, real build had delivered correctly.
    @MainActor
    func test_a_dry_run_never_warns_about_the_device() {
        let store = makeStore()
        var options = BuildOptions()
        options.dryRun = true
        store.launchBuild(
            appName: "example", delivery: options.effectiveDelivery(for: .livecontainer)
        ) {}
        store.note(
            .stage(name: CLIContract.Stage.liveContainerPush, status: "start"),
            forBuildOf: "example"
        )

        XCTAssertFalse(
            store.cancelConsequence.contains("delete"),
            "told a dry run to delete a file: \(store.cancelConsequence)"
        )
        XCTAssertTrue(
            store.cancelConsequence.contains("nothing is being sent to the device"),
            "got: \(store.cancelConsequence)"
        )
    }

    /// The mirror image of the stage-clearing fix. Between `lc-push end` and
    /// `build-end` the CLI still records the delivery, prunes and writes
    /// history — and for that window the warning claimed nothing had reached
    /// the phone, when a complete .ipa had just landed on it. Stopping there is
    /// the one moment that costs the delivery *row* for a real file.
    @MainActor
    func test_the_window_after_a_push_does_not_claim_the_device_is_untouched() {
        let store = makeStore()
        store.launchBuild(appName: "example", delivery: .livecontainer) {}
        store.note(
            .stage(name: CLIContract.Stage.liveContainerPush, status: "start"),
            forBuildOf: "example"
        )
        store.note(
            .stage(name: CLIContract.Stage.liveContainerPush, status: "end"),
            forBuildOf: "example"
        )

        XCTAssertFalse(
            store.cancelConsequence.contains("Nothing has reached the device"),
            "claimed the phone was untouched after a completed push: \(store.cancelConsequence)"
        )
        XCTAssertTrue(store.cancelConsequence.contains("never delivered"))
    }

    /// And it must reset per build, or every later build inherits the claim.
    @MainActor
    func test_the_delivered_flag_does_not_survive_into_the_next_build() {
        let store = makeStore()
        store.launchBuild(appName: "example", delivery: .livecontainer) {}
        store.note(.stage(name: CLIContract.Stage.liveContainerPush, status: "start"),
                   forBuildOf: "example")
        store.note(.stage(name: CLIContract.Stage.liveContainerPush, status: "end"),
                   forBuildOf: "example")
        store.endBuild(appName: "example")

        store.launchBuild(appName: "example", delivery: .livecontainer) {}

        XCTAssertTrue(store.cancelConsequence.contains("Nothing has reached the device"))
    }

    /// One Stop used to hide a genuine earlier failure for the rest of the
    /// session: the marker was cleared only by that app's next *successful*
    /// launch, and the pane's whole fall-through to `lastBuild` was gated on it.
    @MainActor
    func test_a_stop_does_not_permanently_suppress_an_older_failure() {
        let store = makeStore()
        startBuild(store, "example")
        store.noteBuildCancelled(appName: "example")

        // A history row from *before* the stop is still worth showing.
        XCTAssertTrue(
            store.stopSupersedes("example", buildTimestamp: Date().addingTimeInterval(-3600))
        )
        // A row from after it would belong to a later build, so it stands alone.
        XCTAssertFalse(
            store.stopSupersedes("example", buildTimestamp: Date().addingTimeInterval(3600))
        )
        // And an app that was never stopped is never affected.
        XCTAssertFalse(store.stopSupersedes("delta", buildTimestamp: nil))
    }

    @MainActor
    func test_a_new_build_clears_the_stop_marker() {
        let store = makeStore()
        startBuild(store, "example")
        store.noteBuildCancelled(appName: "example")
        XCTAssertTrue(store.stopSupersedes("example", buildTimestamp: nil), "premise")

        startBuild(store, "example")

        XCTAssertFalse(store.stopSupersedes("example", buildTimestamp: nil))
    }
}

// MARK: - Review round 2: the refused delete

extension ConfigStoreTests {

    /// A detach-and-delete that dies partway has written some config and not
    /// the rest. The old code reported only the throwing call's message,
    /// leaving the tweak silently gone from the apps already reached.
    func test_a_partial_detach_names_what_it_changed_and_what_it_did_not() {
        let tweak = TweakRecord(
            filename: "some-tweak.deb", sizeBytes: 10, usedBy: [], name: "Some Tweak"
        )
        let message = TweaksView.partialDetachMessage(
            tweak: tweak,
            detached: ["example"],
            remaining: ["delta", "bravo"],
            error: CLIError.reported("config.yaml is not writable")
        )
        XCTAssertTrue(message.contains("Some Tweak"))
        XCTAssertTrue(message.contains("config.yaml is not writable"))
        XCTAssertTrue(message.contains("already detached from example"))
        XCTAssertTrue(message.contains("Still attached to delta, bravo"))
        XCTAssertTrue(message.contains("was not deleted"))
    }

    /// Failing on the very first app has nothing to report as applied — and
    /// must not imply it did.
    func test_a_detach_that_failed_immediately_claims_no_changes() {
        let tweak = TweakRecord(filename: "some-tweak.deb", sizeBytes: 10, usedBy: [])
        let message = TweaksView.partialDetachMessage(
            tweak: tweak, detached: [], remaining: ["example"],
            error: CLIError.reported("nope")
        )
        XCTAssertFalse(message.contains("already detached"))
        XCTAssertTrue(message.contains("Still attached to example"))
    }
}

extension ConfigStoreTests {
}

// MARK: - The exposure state machine

extension ConfigStoreTests {

    /// The bug this type exists to make impossible: the confirmation warned
    /// about the post-push window while the post-mortem, reading a different
    /// flag that is nil in exactly that window, wrote nothing. Every state must
    /// answer both questions from one value.
    func test_every_exposure_that_warns_about_the_device_also_has_an_aftermath() {
        let states: [DeviceExposure] = [
            .neverWill(reason: "Dry run"),
            .notYet,
            .inProgress(.liveContainerPush),
            .inProgress(.sideloadInstall),
            .delivered(.liveContainerPush),
            .delivered(.sideloadInstall),
        ]
        for state in states {
            XCTAssertFalse(state.cancelWarning.isEmpty, "\(state) has no warning")
            switch state {
            case .neverWill, .notYet:
                XCTAssertNil(state.cancelAftermath, "\(state) has nothing on the device")
            case .inProgress, .delivered:
                XCTAssertNotNil(
                    state.cancelAftermath,
                    "\(state) warns about the device but says nothing afterwards"
                )
            }
        }
    }

    /// A run that will never deliver absorbs every stage — including the
    /// `lc-push` the CLI still emits for a dry run.
    func test_a_never_will_run_absorbs_every_stage() {
        var state = DeviceExposure(delivery: .notDelivered(reason: "Dry run"))
        state = state.advanced(stage: CLIContract.Stage.liveContainerPush, status: "start")
        state = state.advanced(stage: CLIContract.Stage.liveContainerPush, status: "end")
        XCTAssertEqual(state, .neverWill(reason: "Dry run"))
        XCTAssertNil(state.cancelAftermath)
    }

    func test_the_push_transitions_run_in_order() {
        var state = DeviceExposure(delivery: .livecontainer)
        XCTAssertEqual(state, .notYet)
        state = state.advanced(stage: "cyan", status: "start")
        XCTAssertEqual(state, .notYet, "an ordinary stage is not device exposure")
        state = state.advanced(stage: CLIContract.Stage.liveContainerPush, status: "start")
        XCTAssertEqual(state, .inProgress(.liveContainerPush))
        state = state.advanced(stage: CLIContract.Stage.liveContainerPush, status: "end")
        XCTAssertEqual(state, .delivered(.liveContainerPush))
        // Nothing after a delivery walks it back.
        state = state.advanced(stage: "archive", status: "end")
        XCTAssertEqual(state, .delivered(.liveContainerPush))
    }

    /// An `end` for a stage that never started must not fabricate a delivery.
    func test_a_stray_end_does_not_claim_a_delivery() {
        let state = DeviceExposure(delivery: .livecontainer)
            .advanced(stage: CLIContract.Stage.liveContainerPush, status: "end")
        XCTAssertEqual(state, .notYet)
    }

    /// The store threads it through, so the pane and the dialog read one value.
    @MainActor
    func test_the_store_folds_the_stage_stream_into_the_exposure() {
        let store = makeStore()
        store.launchBuild(appName: "example", delivery: .livecontainer) {}
        store.note(.stage(name: CLIContract.Stage.liveContainerPush, status: "start"),
                   forBuildOf: "example")
        store.note(.stage(name: CLIContract.Stage.liveContainerPush, status: "end"),
                   forBuildOf: "example")

        XCTAssertEqual(store.deviceExposure, .delivered(.liveContainerPush))
        XCTAssertNotNil(store.deviceExposure.cancelAftermath)
    }

    /// `busy` has to carry which operation holds the store, because the
    /// disabled control's tooltip is derived from it — a Bool sent the Check
    /// device button's tooltip through the build button's and it read
    /// "Build and install example" on a disabled control.
    @MainActor
    func test_busy_names_the_operation_that_holds_the_store() {
        let store = makeStore()
        XCTAssertNil(store.busy)

        store.beginDeviceCheck(appName: "example")
        XCTAssertEqual(store.busy, .checking("example"))
        XCTAssertTrue(store.busy!.explanation.contains("Checking the device"))
        XCTAssertFalse(store.busy!.explanation.contains("Build and install"))

        store.endDeviceCheck(appName: "example")
        startBuild(store, "example")
        XCTAssertEqual(store.busy, .building("example"))
        XCTAssertTrue(store.busy!.explanation.contains("is building"))
    }

    /// The option names the user sees must come from one place. They had
    /// already drifted: the header pill said `dry-run`, the banner `Dry run`.
    func test_the_delivery_suppressing_options_are_named_once() {
        XCTAssertEqual(BuildOptions.Option.dryRun.suppressesDelivery, "Dry run")
        XCTAssertEqual(BuildOptions.Option.skipInstall.suppressesDelivery, "Install skipped")
        XCTAssertNil(BuildOptions.Option.skipDecryptCheck.suppressesDelivery)
        XCTAssertNil(BuildOptions.Option.device(udid: "x").suppressesDelivery)
    }
}

// MARK: - C-STOP: the CLI reporting a stop

extension ConfigStoreTests {
    /// A `build-end` with `stopped: true` is a stop, whoever sent the signal:
    /// no failure card, no "failed" banner, and the build reads as cancelled
    /// so the pane drops whatever the stream still has queued.
    @MainActor
    func test_a_build_end_reporting_a_stop_writes_no_failure_and_no_banner() {
        let store = makeStore()
        var posted: [BuildNotifier.Note] = []
        store.postNote = { if let note = $0 { posted.append(note) } }
        store.launchBuild(appName: "example", delivery: .livecontainer) {}

        store.note(.stopped(elapsed: 3.0), forBuildOf: "example")

        XCTAssertNil(store.buildFailures["example"], "a stop is not a failure card")
        XCTAssertTrue(posted.isEmpty, "banner'd a build that was stopped: \(posted)")
        XCTAssertTrue(store.wasCancelled("example"), "a stop this app did not send is still a stop")
    }

    /// Control: the same store, the same build, a real failure — so the test
    /// above is measuring the stop, not a store that never records anything.
    @MainActor
    func test_a_failed_build_end_still_writes_the_failure() {
        let store = makeStore()
        store.launchBuild(appName: "example", delivery: .livecontainer) {}
        store.note(.finished(ok: false, error: "143", elapsed: nil), forBuildOf: "example")
        XCTAssertEqual(store.buildFailures["example"], "143")
        XCTAssertFalse(store.wasCancelled("example"))
    }

    /// A stop reported for a build that no longer holds the slot marks
    /// nothing: the next build must not start life cancelled.
    @MainActor
    func test_a_late_stop_from_a_finished_build_cancels_nothing() {
        let store = makeStore()
        store.launchBuild(appName: "delta", delivery: .livecontainer) {}

        store.note(.stopped(elapsed: nil), forBuildOf: "example")

        XCTAssertFalse(store.wasCancelled("example"))
        XCTAssertFalse(store.wasCancelled("delta"))
    }
}
