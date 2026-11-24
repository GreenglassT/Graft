import XCTest
@testable import Graft

final class SettingsStoreTests: XCTestCase {
    @MainActor
    func test_runChecks_parses_preflight_events() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let script = #"""
        printf '{"type":"preflight-check","name":"cyan","ok":true,"detail":"/opt/homebrew/bin/cyan","hint":null}\n{"type":"preflight-check","name":"zsign","ok":false,"detail":"not found","hint":"brew install zsign"}\n{"type":"preflight-end","ok_count":1,"fail_count":1}\n'
        """#
        let store = SettingsStore(runner: runner)
        try await store.runChecks(argsOverride: ["-c", script])
        XCTAssertEqual(store.checks.count, 2)
        XCTAssertEqual(store.checks[0].name, "cyan")
        XCTAssertTrue(store.checks[0].ok)
        XCTAssertEqual(store.checks[1].hint, "brew install zsign")
        XCTAssertNil(store.checks[0].actionKey)
        XCTAssertEqual(store.okCount, 1)
        XCTAssertEqual(store.failCount, 1)
        XCTAssertFalse(store.isRunning)
    }

    /// Whole-diff review, app-1: Preflight's Re-run, pressed, asks the login
    /// shell for its PATH again before the scan, so a tool installed into a
    /// folder just added to it is found; every other scan uses the answer
    /// already held.
    @MainActor
    func test_only_a_rerun_asks_the_login_shell_again() async throws {
        final class Asks: @unchecked Sendable {
            private let lock = NSLock()
            private var n = 0
            var count: Int { lock.withLock { n } }
            func add() { lock.withLock { n += 1 } }
        }
        let asks = Asks()
        let runner = CLIRunner(resolving: {
            CLIRunner.Target(executable: URL(fileURLWithPath: "/bin/sh"), environment: nil)
        }, refresh: { asks.add() })
        let store = SettingsStore(runner: runner)
        let script = #"printf '%s\n' '{"type":"preflight-end","ok_count":0,"fail_count":0}'"#
        try await store.runChecks(argsOverride: ["-c", script])
        XCTAssertEqual(asks.count, 0, "a scan on its own")
        try await store.runChecks(argsOverride: ["-c", script], askingShellAgain: true)
        XCTAssertEqual(asks.count, 1, "Re-run")
    }

    @MainActor
    func test_runChecks_decodes_action_key() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let script = #"""
        printf '{"type":"preflight-check","name":".p12 password","ok":false,"detail":"(missing)","hint":"run set-p12-password","action_key":"set-p12-password"}\n{"type":"preflight-end","ok_count":0,"fail_count":1}\n'
        """#
        let store = SettingsStore(runner: runner)
        try await store.runChecks(argsOverride: ["-c", script])
        XCTAssertEqual(store.checks.first?.actionKey, "set-p12-password")
    }

    /// Exit 3 is "some checks failed" — the pane's whole subject — and must
    /// still parse, notice line and all.
    @MainActor
    func test_a_scan_with_failures_is_data_not_an_error() async throws {
        let store = SettingsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        let script = #"""
        printf '%s\n' '{"type":"log","source":"startup","line":"TB_SSH_PASS is set but no longer read; use GRAFT_SSH_PASS"}' '{"type":"preflight-check","name":"zsign","ok":false,"detail":"not found","hint":"brew install zsign"}' '{"type":"preflight-end","ok_count":0,"fail_count":1}'
        exit 3
        """#
        try await store.runChecks(argsOverride: ["-c", script])
        XCTAssertEqual(store.checks.map(\.name), ["zsign"], "the notice is not a check row")
    }

    /// K6(b): a config that will not load exits 2, and the startup notice
    /// in front of it used to make stdout non-empty — so the pane got no
    /// checks and no error. It is an error.
    @MainActor
    func test_a_scan_that_could_not_run_throws_even_with_a_notice_on_stdout() async throws {
        let store = SettingsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        let script = #"""
        printf '%s\n' '{"type":"log","source":"startup","line":"TB_SSH_PASS is set but no longer read; use GRAFT_SSH_PASS"}'
        printf 'error: config.yaml: mapping values are not allowed here\n' >&2
        exit 2
        """#
        do {
            try await store.runChecks(argsOverride: ["-c", script])
            XCTFail("a scan that never ran reported success")
        } catch {
            XCTAssertTrue(error.localizedDescription.contains("mapping values are not allowed here"),
                          error.localizedDescription)
        }
        XCTAssertTrue(store.hasChecked, "it finished, badly")
        XCTAssertFalse(store.isRunning)
    }
}

// MARK: - Writing settings

extension SettingsStoreTests {

    @MainActor
    private func shStore() -> SettingsStore {
        SettingsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
    }

    /// The guard that makes the whole pane trustworthy: a field the CLI
    /// silently declined is indistinguishable from a saved one from here, and
    /// the user then trusts a value that is not on disk.
    @MainActor
    func test_a_field_the_cli_did_not_apply_is_reported_as_a_failure() async throws {
        // Asked for two; the CLI reports one.
        let script = #"printf '{"type":"settings-updated","changed":["archive_keep"]}\n'"#
        let store = shStore()
        do {
            _ = try await store.updateSettings(
                archiveKeep: 4, signingTeamId: "TEAM9", argsOverride: ["-c", script]
            )
            XCTFail("expected the dropped field to throw")
        } catch let error as CLIError {
            XCTAssertTrue(
                error.localizedDescription.contains("signing_team_id"),
                "the error must name what was dropped, got: \(error.localizedDescription)"
            )
            XCTAssertFalse(error.localizedDescription.contains("archive_keep"))
        }
    }

    @MainActor
    func test_a_fully_applied_save_returns_what_changed() async throws {
        let script = #"""
        printf '{"type":"settings-updated","changed":["archive_keep","signing_team_id"]}\n'
        """#
        let store = shStore()
        let result = try await store.updateSettings(
            archiveKeep: 4, signingTeamId: "TEAM9", argsOverride: ["-c", script]
        )
        XCTAssertEqual(result.changed, ["archive_keep", "signing_team_id"])
    }

    /// Nothing to save must not spawn the CLI at all — the pane's Save button
    /// is disabled then, but the store is the thing that guarantees it.
    @MainActor
    func test_saving_nothing_runs_no_subprocess() async throws {
        let marker = FileManager.default.temporaryDirectory
            .appendingPathComponent("settings-noop-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: marker) }
        let store = shStore()

        let result = try await store.updateSettings(
            argsOverride: ["-c", "touch '\(marker.path)'"]
        )

        XCTAssertEqual(result.changed, [])
        XCTAssertFalse(
            FileManager.default.fileExists(atPath: marker.path),
            "an empty save still spawned graft"
        )
    }

    /// The argv, checked against the real spellings rather than inferred.
    @MainActor
    func test_the_save_sends_only_the_flags_for_the_fields_given() async throws {
        let dir = NSTemporaryDirectory()
        let capture = dir + "settings_args_\(UUID().uuidString).txt"
        let scriptPath = dir + "settings_stub_\(UUID().uuidString).sh"
        let script = """
        #!/bin/sh
        for arg in "$@"; do printf '%s\\n' "$arg" >> "\(capture)"; done
        printf '{"type":"settings-updated","changed":["archive_keep"]}\\n'
        """
        try script.write(toFile: scriptPath, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o755], ofItemAtPath: scriptPath
        )
        defer {
            try? FileManager.default.removeItem(atPath: scriptPath)
            try? FileManager.default.removeItem(atPath: capture)
        }

        let store = SettingsStore(runner: CLIRunner(executable: URL(fileURLWithPath: scriptPath)))
        _ = try await store.updateSettings(archiveKeep: 7)

        let args = try String(contentsOfFile: capture, encoding: .utf8)
            .split(separator: "\n", omittingEmptySubsequences: true).map(String.init)
        XCTAssertEqual(args, ["--json", "--set-settings", "--archive-keep", "7"])
    }

    /// Reading back what the CLI has, which is what the pane's fields are
    /// seeded from — an unset device must survive as nil, not as "".
    @MainActor
    func test_loading_settings_keeps_an_unset_device_nil() async throws {
        let script = #"""
        printf '{"type":"settings","default_device_udid":null,"archive_keep":2,"signing_cert":"/c.p12","signing_profile":"/p.mp","signing_team_id":"T1"}\n'
        """#
        let store = shStore()
        let record = try await store.loadSettings(argsOverride: ["-c", script])

        XCTAssertNil(record?.defaultDeviceUDID)
        XCTAssertEqual(record?.archiveKeep, 2)
        XCTAssertEqual(store.settings, record)
    }

    /// The lenient read, decoded — the half of the contract that was never
    /// widened.
    ///
    /// `read_settings` bypasses config validation on purpose so a config with
    /// gaps still reports the fields it has. While `SettingsRecord`'s signing
    /// fields were non-optional this line failed to decode, `buildEvents()`'s
    /// `try?` dropped it, `loadSettings` returned nil, and the pane showed
    /// "Couldn't read your settings." on the one config the editor was built
    /// to repair. The Python side asserted the null; nothing decoded it.
    @MainActor
    func test_a_settings_line_with_missing_fields_still_decodes() async throws {
        let script = #"""
        printf '{"type":"settings","default_device_udid":null,"archive_keep":null,"signing_cert":"/c.p12","signing_profile":null,"signing_team_id":null}\n'
        """#
        let store = shStore()
        let record = try await store.loadSettings(argsOverride: ["-c", script])

        XCTAssertNotNil(record, "a config with gaps read as no settings at all")
        XCTAssertEqual(record?.signingCert, "/c.p12")
        XCTAssertNil(record?.signingTeamId)
        XCTAssertNil(record?.archiveKeep, "an unreadable retention is null, not the default")
    }
}

// MARK: - The pane's pending edit

extension SettingsStoreTests {

    /// A store that has read its settings, holding a clean draft over them.
    ///
    /// Seeded through `loadSettings` rather than by assigning `draft`, because
    /// `resetDraft` restores from `settings` — the store's last read — and a
    /// test that set only the draft would be reverting to a baseline the store
    /// does not have.
    @MainActor
    private func seeded() async throws -> SettingsStore {
        let store = shStore()
        let script = #"""
        printf '{"type":"settings","default_device_udid":null,"archive_keep":2,"signing_cert":"/c.p12","signing_profile":"/p.mp","signing_team_id":"TEAM1"}\n'
        """#
        _ = try await store.loadSettings(argsOverride: ["-c", script])
        store.reseedDraftIfIdle()
        return store
    }

    /// The write reports the resulting state, so a save is one process.
    ///
    /// `--set-settings` emits `settings-updated` *and* `settings`. The save used
    /// to spawn `--settings` afterward to learn what it had just written — a
    /// whole extra interpreter start for a value the writer already held.
    @MainActor
    func test_a_save_takes_the_new_state_from_the_write() async throws {
        let script = #"""
        printf '{"type":"settings-updated","changed":["signing_team_id"]}\n{"type":"settings","default_device_udid":null,"archive_keep":2,"signing_cert":"/c.p12","signing_profile":"/p.mp","signing_team_id":"TEAM9"}\n'
        """#

        let result = try await shStore().updateSettings(
            signingTeamId: "TEAM9", argsOverride: ["-c", script]
        )

        XCTAssertEqual(result.changed, ["signing_team_id"])
        XCTAssertEqual(
            result.fresh?.signingTeamId, "TEAM9",
            "the write did not report the state it produced, so the save must re-read"
        )
    }

    /// An older installed graft reports only what changed. The save has
    /// to notice and pay for the extra read rather than reseed the draft from
    /// values it just replaced.
    @MainActor
    func test_a_write_that_reports_no_new_state_is_detectable() async throws {
        let script = #"printf '{"type":"settings-updated","changed":["signing_team_id"]}\n'"#

        let result = try await shStore().updateSettings(
            signingTeamId: "TEAM9", argsOverride: ["-c", script]
        )

        XCTAssertEqual(result.changed, ["signing_team_id"])
        XCTAssertNil(result.fresh)
    }

    /// Revert's entire job. It renders only when there are changes, so routing
    /// it through `reseedDraftIfIdle` — which does nothing unless there are
    /// none — left the button dead: clicking it did exactly nothing, silently.
    @MainActor
    func test_reverting_discards_a_pending_edit() async throws {
        let store = try await seeded()
        store.draft.edited.signingTeamId = "TYPED"
        XCTAssertTrue(store.draft.hasChanges, "premise: there is something to revert")

        store.resetDraft()

        XCTAssertFalse(store.draft.hasChanges, "Revert left the edit in place")
        XCTAssertEqual(store.draft.edited.signingTeamId, "TEAM1")
    }

    /// And the background path keeps its opposite guarantee: a re-read landing
    /// while somebody is typing must not throw their work away.
    @MainActor
    func test_a_background_reseed_keeps_a_pending_edit() async throws {
        let store = try await seeded()
        store.draft.edited.signingTeamId = "HALF-TYP"

        store.reseedDraftIfIdle()

        XCTAssertEqual(store.draft.edited.signingTeamId, "HALF-TYP")
    }

    /// A refused save reports why and leaves the edit where the user can fix
    /// it — retyping a blanked Team ID from memory is not a recovery story.
    @MainActor
    func test_a_refused_save_keeps_the_draft_and_reports_the_error() async throws {
        // The seeding read goes through `-c`; the save does not, so `/bin/sh`
        // gets `--json --set-settings …` and rejects it with exit 2 and a line
        // on stderr — the shape of a `--set-settings` that refused a value.
        let store = try await seeded()
        store.draft.edited.signingTeamId = "TEAM9"

        await store.saveDraft()

        XCTAssertNotNil(store.lastError, "a rejected save looked like not clicking Save")
        XCTAssertEqual(
            store.draft.edited.signingTeamId, "TEAM9",
            "the rejected edit was thrown away, so there is nothing left to correct"
        )
        XCTAssertFalse(store.isSaving, "the pane stayed frozen after a failed save")
    }

    /// A save that lands but whose re-read fails must leave the saved values on
    /// screen.
    ///
    /// The write is already on disk. Reseeding the draft here pulls from
    /// `settings`, which still holds the *pre-save* record because the re-read
    /// is what failed — so the fields silently snap back to the old values and
    /// the user, seeing their edit gone, types it again.
    @MainActor
    func test_a_save_whose_re_read_fails_keeps_the_saved_values_on_screen() async throws {
        let path = NSTemporaryDirectory() + "settings_stub_\(UUID().uuidString).sh"
        // Writes fine; refuses to read back. That is the old-CLI path — no
        // `settings` event on the write — followed by a failing `--settings`.
        let script = """
        #!/bin/sh
        for arg in "$@"; do
          if [ "$arg" = "--set-settings" ]; then
            printf '{"type":"settings-updated","changed":["signing_team_id"]}\\n'
            exit 0
          fi
        done
        exit 1
        """
        try script.write(toFile: path, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: path)
        defer { try? FileManager.default.removeItem(atPath: path) }

        let store = SettingsStore(runner: CLIRunner(executable: URL(fileURLWithPath: path)))
        store.draft = SettingsDraft(SettingsRecord(
            defaultDeviceUDID: nil, archiveKeep: 2,
            signingCert: "/c.p12", signingProfile: "/p.mp", signingTeamId: "TEAM1"
        ))
        store.draft.edited.signingTeamId = "TEAM9"

        await store.saveDraft()

        XCTAssertEqual(
            store.draft.edited.signingTeamId, "TEAM9",
            "the re-read failed and reverted fields whose write had already landed"
        )
        XCTAssertNotNil(store.lastError, "the stale re-read was not mentioned at all")
    }

    /// Nothing to save must not spawn anything, and must not clear the error
    /// the previous attempt left on screen.
    @MainActor
    func test_saving_a_clean_draft_does_nothing() async throws {
        let store = try await seeded()
        store.lastError = "left from earlier"

        await store.saveDraft()

        XCTAssertEqual(store.lastError, "left from earlier")
    }
}

// MARK: - The default device, after the save

/// What has to happen to the *device list* when a save moves the default
/// device — which is a different store from the one the save wrote to, and was
/// the gap this pane shipped with.
///
/// The fact moved between commits and nothing followed it. `default_device_udid`
/// used to be read from `SettingsStore.settings`, which `saveDraft` replaces
/// from the write's own answer; it is now `DeviceRecord.isDefault`, delivered by
/// `--devices`, and no part of a save re-ran `--devices`. The result closed a
/// loop through the app pane's own button: the build-target bar says "Nothing is
/// set as the default device", its Change control opens this pane, the user
/// picks a phone and saves, and the bar still says it. The device pane's DEFAULT
/// TARGET chip reads the same `isDefault` and was stale in the same way — a
/// regression against `master`, where that chip was fed from `settings`.
///
/// **These cases are about staleness surviving a save, not about a reload
/// happening.** Asserting "something called `load()`" would pass against a
/// reload that fetched the same answer twice; what broke is that the *old*
/// answer was still on screen afterward, so both cases assert on what the two
/// surfaces would render — `BuildTargetBar.target` for the bar's sentence, and
/// `isDefault` itself for the chip.
@MainActor
final class SettingsDefaultDeviceRescanTests: XCTestCase {

    private static let phoneA = "00008130-000A1111AAAA001A"
    private static let phoneB = "00008130-000B2222BBBB001B"

    /// A stub `graft` that remembers which phone is the default.
    ///
    /// Stateful rather than a canned transcript, because the whole question
    /// here is whether a *second* `--devices` is run and what it says: a script
    /// that printed a fixed listing would answer "iPhone A is the default" to
    /// the rescan as well, and the case could never fail. So `--set-settings`
    /// writes the udid it was handed to a file, and `--devices` marks whichever
    /// phone that file names — the one behavior of the real CLI this suite
    /// depends on (`json_output.py` sets `is_default` where the udid matches
    /// `default_device_udid`).
    ///
    /// Both stores are given the same runner, as `ContentView` gives them the
    /// same one, so the script sees the whole conversation and the scan counter
    /// counts every `--devices` either store spawns. The counter is a file the
    /// script appends to, the way `AppDetailLayoutTests` counts `--tweaks`
    /// runs: a subprocess that was never spawned leaves no other trace.
    ///
    /// Nothing is passed through `argsOverride` anywhere below. The point of
    /// the fixture is that `saveDraft` and `load()` build their own argument
    /// vectors — the production ones — and the script answers those.
    private func stubCLI() throws -> (runner: CLIRunner, deviceScans: () -> Int) {
        let id = UUID().uuidString
        let dir = NSTemporaryDirectory()
        let statePath = dir + "tb_default_device_\(id)"
        let teamPath = dir + "tb_team_id_\(id)"
        let counterPath = dir + "tb_device_scans_\(id)"
        let scriptPath = dir + "tb_stub_\(id).sh"
        try Self.phoneA.write(toFile: statePath, atomically: true, encoding: .utf8)
        try "TEAM1".write(toFile: teamPath, atomically: true, encoding: .utf8)
        let script = """
        #!/bin/sh
        state='\(statePath)'
        team='\(teamPath)'
        counter='\(counterPath)'
        emit_settings() {
          printf '{"type":"settings","default_device_udid":"%s","archive_keep":2,\
        "signing_cert":"/c.p12","signing_profile":"/p.mp","signing_team_id":"%s"}\\n' \
        "$(cat "$state")" "$(cat "$team")"
        }
        emit_device() {
          if [ "$1" = "$(cat "$state")" ]; then flag=true; else flag=false; fi
          printf '{"type":"device","udid":"%s","name":"%s","model":"iPhone16,1",\
        "ios_version":"26.1","connection_type":"USB",\
        "livecontainer_bundle_id":"com.kdt.livecontainer.ABCDE12345","is_default":%s}\\n' \
        "$1" "$2" "$flag"
        }
        case " $* " in
          *' --set-settings '*)
            changed=''
            prev=''
            for arg in "$@"; do
              case "$prev" in
                --default-device)
                  printf '%s' "$arg" > "$state"
                  changed="$changed\\"default_device_udid\\","
                  ;;
                --signing-team-id)
                  printf '%s' "$arg" > "$team"
                  changed="$changed\\"signing_team_id\\","
                  ;;
              esac
              prev="$arg"
            done
            printf '{"type":"settings-updated","changed":[%s]}\\n' "${changed%,}"
            emit_settings
            ;;
          *' --settings '*)
            emit_settings
            ;;
          *' --devices '*)
            printf 'scan\\n' >> "$counter"
            emit_device '\(Self.phoneA)' 'iPhone A'
            emit_device '\(Self.phoneB)' 'iPhone B'
            printf '{"type":"devices-end","count":2,"failed":0}\\n'
            ;;
        esac
        exit 0
        """
        try script.write(toFile: scriptPath, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o755], ofItemAtPath: scriptPath
        )
        addTeardownBlock {
            for path in [statePath, teamPath, counterPath, scriptPath] {
                try? FileManager.default.removeItem(atPath: path)
            }
        }
        return (
            CLIRunner(executable: URL(fileURLWithPath: scriptPath)),
            {
                guard let text = try? String(contentsOfFile: counterPath, encoding: .utf8)
                else { return 0 }
                return text.split(separator: "\n", omittingEmptySubsequences: true).count
            }
        )
    }

    /// The pane as `ContentView` builds it, over two stores that have both read
    /// once — which is the state a user who has opened Settings is in.
    private func pane(
        _ cli: (runner: CLIRunner, deviceScans: () -> Int)
    ) async throws -> (view: SettingsView, settings: SettingsStore, devices: DevicesStore) {
        let settings = SettingsStore(runner: cli.runner)
        let devices = DevicesStore(runner: cli.runner)
        _ = try await settings.loadSettings()
        settings.reseedDraftIfIdle()
        _ = try await devices.load()
        return (
            SettingsView(
                store: settings, devicesStore: devices,
                appStore: AppStoreStore(runner: cli.runner, shell: NoAdminShell()),
                account: .paid
            ),
            settings, devices
        )
    }

    /// Pick a different phone, save, and the surfaces that name the default
    /// device must be naming the new one.
    func test_saving_a_new_default_device_re_reads_the_device_list() async throws {
        let cli = try stubCLI()
        let (view, settings, devices) = try await pane(cli)

        XCTAssertEqual(
            BuildTargetBar.target(devices: devices.devices, delivery: .livecontainer),
            .device(name: "iPhone A", mode: "LiveContainer"),
            "premise: before the save, the bar names the phone the first scan marked"
        )
        settings.draft.edited.defaultDeviceUDID = Self.phoneB
        XCTAssertTrue(settings.draft.hasChanges, "premise: there is something to save")

        await view.saveAndRescanIfDefaultDeviceMoved()

        XCTAssertEqual(
            settings.settings?.defaultDeviceUDID, Self.phoneB,
            "premise: the save itself landed, so what follows is about the device list"
        )
        XCTAssertEqual(
            BuildTargetBar.target(devices: devices.devices, delivery: .livecontainer),
            .device(name: "iPhone B", mode: "LiveContainer"),
            "the app pane's build-target bar is still reading the pre-save listing — "
            + "which is the loop this pane's own Change button walks the user into"
        )
        XCTAssertEqual(
            devices.devices.first(where: \.isDefault)?.name, "iPhone B",
            "the device pane's DEFAULT TARGET chip reads this same flag, and stayed "
            + "on the old phone"
        )
        XCTAssertEqual(
            cli.deviceScans(), 2,
            "one listing for the pane's own load and one for the save; more than that "
            + "is a rescan running twice"
        )
    }

    /// And the gate: a save that leaves the default alone must not pay for a
    /// listing. `--devices` is a lockdown handshake per phone, ~1s, and every
    /// other field on this pane is a path or a number that no device knows
    /// anything about.
    func test_a_save_that_leaves_the_default_alone_does_not_rescan() async throws {
        let cli = try stubCLI()
        let (view, settings, _) = try await pane(cli)
        XCTAssertEqual(cli.deviceScans(), 1, "premise: the pane's own listing has run")

        settings.draft.edited.signingTeamId = "TEAM9"
        XCTAssertTrue(settings.draft.hasChanges, "premise: there is something to save")

        await view.saveAndRescanIfDefaultDeviceMoved()

        XCTAssertEqual(
            settings.settings?.signingTeamId, "TEAM9",
            "premise: the save landed, so a scan that did not happen is a decision "
            + "rather than a save that never ran"
        )
        XCTAssertEqual(
            cli.deviceScans(), 1,
            "editing a signing field spent a device scan on a fact no device reports"
        )
    }
}

// MARK: - App Store Connect key (--set-portal / --clear-portal)

extension SettingsStoreTests {
    /// A script that logs its argv, then runs `body`.
    @MainActor
    private func portalStore(_ body: String) throws -> (SettingsStore, URL) {
        let dir = FileManager.default.temporaryDirectory
            .appending(path: "settings_portal_\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let log = dir.appending(path: "argv.log")
        let script = dir.appending(path: "graft")
        try """
        #!/bin/sh
        echo "$*" >> '\(log.path)'
        \(body)
        """.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
        return (SettingsStore(runner: CLIRunner(executable: script)), log)
    }

    private static let settingsWithKey = #"{"type":"settings","default_device_udid":null,"archive_keep":2,"signing_cert":"/c.p12","signing_profile":"/p.mobileprovision","signing_team_id":"ABCDE12345","parse_error":null,"paths_are_defaults":false,"app_store_connect":{"key_id":"KEY1234567","issuer_id":"ISS","key_file":"/cfg/AuthKey_KEY1234567.p8","key_file_exists":true}}"#
    private static let settingsWithout = #"{"type":"settings","default_device_udid":null,"archive_keep":2,"signing_cert":"/c.p12","signing_profile":"/p.mobileprovision","signing_team_id":"ABCDE12345","parse_error":null,"paths_are_defaults":false,"app_store_connect":null}"#
    private static let settingsFromOldCLI = #"{"type":"settings","default_device_udid":null,"archive_keep":2,"signing_cert":"/c.p12","signing_profile":"/p.mobileprovision","signing_team_id":"ABCDE12345"}"#

    /// Review #6: on a config that doesn't parse the CLI leaves the key out,
    /// and the section read that as an old CLI and said to update graft.
    @MainActor
    func test_an_unparseable_config_does_not_say_update_graft() throws {
        let broken = #"{"type":"settings","default_device_udid":null,"archive_keep":null,"signing_cert":null,"signing_profile":null,"signing_team_id":null,"parse_error":"while parsing a flow sequence","paths_are_defaults":true,"graft_version":"2.0.0"}"#
        guard case let .settings(record) = try XCTUnwrap(broken.buildEvents().first)
        else { return XCTFail() }
        XCTAssertFalse(record.reportsAppStoreConnect, "premise: the key is left out")

        let status = AppStoreConnectSection.status(of: record)

        XCTAssertEqual(status, .unknown(AppStoreConnectSection.parseErrorLine))
        guard case let .unknown(sentence) = status else { return XCTFail() }
        XCTAssertFalse(sentence.contains("Update"), sentence)
        XCTAssertFalse(AppStoreConnectSection.offersForm(status), "nothing can be saved to it")
    }

    @MainActor
    func test_an_old_cli_still_gets_the_update_sentence() throws {
        guard case let .settings(old) = try XCTUnwrap(Self.settingsFromOldCLI.buildEvents().first)
        else { return XCTFail() }
        guard case let .unknown(sentence) = AppStoreConnectSection.status(of: old)
        else { return XCTFail() }
        XCTAssertTrue(sentence.contains("Update"), sentence)
    }

    @MainActor
    func test_settings_decode_the_key_and_tell_unset_from_unreported() throws {
        let with = try XCTUnwrap(Self.settingsWithKey.buildEvents().first)
        guard case let .settings(record) = with else { return XCTFail("\(with)") }
        XCTAssertEqual(record.appStoreConnect?.keyId, "KEY1234567")
        XCTAssertEqual(record.appStoreConnect?.keyFileExists, true)
        XCTAssertTrue(record.reportsAppStoreConnect)

        guard case let .settings(unset) = try XCTUnwrap(Self.settingsWithout.buildEvents().first)
        else { return XCTFail() }
        XCTAssertNil(unset.appStoreConnect)
        XCTAssertTrue(unset.reportsAppStoreConnect, "null is an answer: no key is set")

        guard case let .settings(old) = try XCTUnwrap(Self.settingsFromOldCLI.buildEvents().first)
        else { return XCTFail() }
        XCTAssertFalse(old.reportsAppStoreConnect, "absent is no answer at all")
    }

    @MainActor
    func test_save_and_test_sends_the_three_fields_and_reports_the_device_count() async throws {
        let (store, log) = try portalStore("""
        printf '%s\\n' '{"type":"portal-set","key_id":"KEY1234567","issuer_id":"ISS","key_file":"/cfg/AuthKey_KEY1234567.p8","devices":4}'
        printf '%s\\n' '\(Self.settingsWithKey)'
        """)

        await store.setPortal(keyId: " KEY1234567 ", issuerId: "ISS", keyFile: "/Downloads/AuthKey_KEY1234567.p8")

        XCTAssertEqual(
            try String(contentsOf: log, encoding: .utf8).split(separator: "\n").map(String.init),
            ["--json --set-portal --asc-key-id KEY1234567 --asc-issuer-id ISS --asc-key-file /Downloads/AuthKey_KEY1234567.p8",
             "--json --check"]
        )
        XCTAssertEqual(store.portalResult, .connected(devices: 4))
        XCTAssertEqual(store.settings?.appStoreConnect?.keyId, "KEY1234567", "the echo is the new state")
        XCTAssertFalse(store.isSavingPortal)
    }

    /// Fix-check #7/#27: setting a key warns about an earlier
    /// AuthKey_<id>.p8 left in the config folder. The result line under the
    /// form says so, after the all-clear and in amber; a startup notice is
    /// not about the key.
    @MainActor
    func test_save_and_test_shows_the_warning_about_an_earlier_key() async throws {
        let warning = "AuthKey_AAAAAAAAAA.p8 is still in /cfg: a private key no setting uses. "
            + "Delete it once you have revoked it in App Store Connect"
        let (store, _) = try portalStore("""
        case " $* " in
          *" --set-portal "*)
            printf '%s\\n' '{"type":"log","source":"startup","line":"TB_SSH_PASS is set but no longer read; use GRAFT_SSH_PASS"}'
            printf '%s\\n' '{"type":"log","source":"warning","line":"\(warning)"}'
            printf '%s\\n' '{"type":"portal-set","key_id":"BBBBBBBBBB","issuer_id":"ISS","key_file":"/cfg/AuthKey_BBBBBBBBBB.p8","devices":4}'
            printf '%s\\n' '\(Self.settingsWithKey)' ;;
        esac
        """)

        await store.setPortal(keyId: "BBBBBBBBBB", issuerId: "ISS", keyFile: "/Downloads/AuthKey_BBBBBBBBBB.p8")

        XCTAssertEqual(store.portalResult, .connected(devices: 4, warnings: [warning]))
        let line = try XCTUnwrap(AppStoreConnectSection.resultLine(store.portalResult))
        XCTAssertEqual(line.text, "✓ Connected. App Store Connect sees 4 iOS devices.\nwarning: " + warning)
        XCTAssertEqual(line.tone, .warning)
        XCTAssertEqual(
            AppStoreConnectSection.resultLine(.connected(devices: 4))?.tone, .success,
            "no warning, the all-clear stays green"
        )
    }

    @MainActor
    func test_a_key_app_store_connect_refuses_reports_the_message_and_the_hint() async throws {
        let (store, _) = try portalStore("""
        echo "error: App Store Connect rejected the JWT (401)" >&2
        echo "  hint: check key_id / issuer_id / .p8 file; the key may have been revoked" >&2
        exit 10
        """)

        await store.setPortal(keyId: "KEY1234567", issuerId: "ISS", keyFile: "/k.p8")

        guard case let .failed(message) = store.portalResult else {
            return XCTFail("\(String(describing: store.portalResult))")
        }
        XCTAssertTrue(message.contains("rejected the JWT"), message)
        XCTAssertTrue(message.contains("may have been revoked"), message)
        XCTAssertNil(store.settings, "a refused key changes nothing")
    }

    @MainActor
    func test_remove_clears_the_key_and_takes_the_new_state() async throws {
        let (store, log) = try portalStore("""
        printf '%s\\n' '{"type":"portal-cleared","removed":true,"key_file":"/cfg/AuthKey_KEY1234567.p8"}'
        printf '%s\\n' '\(Self.settingsWithout)'
        """)

        await store.clearPortal()

        XCTAssertEqual(try String(contentsOf: log, encoding: .utf8), "--json --clear-portal\n--json --check\n")
        XCTAssertNil(store.settings?.appStoreConnect)
        XCTAssertEqual(store.settings?.reportsAppStoreConnect, true)
        XCTAssertNil(store.portalResult)
        XCTAssertNil(store.lastError)
    }

    /// Remove names, in a `log` warning, the sideload apps with
    /// `auto_profile` whose builds now fail for want of a key. The section
    /// dropped it, so the one change here that breaks builds didn't say which.
    @MainActor
    func test_remove_shows_the_warning_naming_the_apps_it_breaks() async throws {
        let warning = "example, other still create their provisioning profile automatically "
            + "(auto_profile); their builds fail until a key is set again or that is turned off"
        let (store, _) = try portalStore("""
        case " $* " in
          *" --clear-portal "*)
            printf '%s\\n' '{"type":"log","source":"startup","line":"TB_SSH_PASS is set but no longer read; use GRAFT_SSH_PASS"}'
            printf '%s\\n' '{"type":"log","source":"warning","line":"\(warning)"}'
            printf '%s\\n' '{"type":"portal-cleared","removed":true,"key_file":"/cfg/AuthKey_KEY1234567.p8"}'
            printf '%s\\n' '\(Self.settingsWithout)' ;;
        esac
        """)

        await store.clearPortal()

        XCTAssertNil(store.settings?.appStoreConnect, "premise: the key is gone")
        XCTAssertEqual(store.portalResult, .removed(warnings: [warning]),
                       "the CLI's warning, and not the startup notice, which is not about Remove")
        let line = try XCTUnwrap(AppStoreConnectSection.resultLine(store.portalResult))
        XCTAssertEqual(line.text, "warning: " + warning)
        XCTAssertEqual(line.tone, .warning)
    }

    @MainActor
    func test_a_save_keeps_the_paths_the_echo_does_not_carry() async throws {
        let withPaths = #"{"type":"settings","default_device_udid":null,"archive_keep":2,"signing_cert":"/c.p12","signing_profile":"/p","signing_team_id":"T","paths":{"config_dir":"/cfg","cache_dir":"/c","built_dir":"/b","state_dir":"/s","tweaks_dir":"/t"},"parse_error":null,"paths_are_defaults":false,"app_store_connect":null}"#
        let (store, _) = try portalStore("""
        case " $* " in
          *" --settings "*) printf '%s\\n' '\(withPaths)' ;;
          *) printf '%s\\n' '{"type":"portal-set","key_id":"K","issuer_id":"I","key_file":"/cfg/AuthKey_K.p8","devices":0}'
             printf '%s\\n' '\(Self.settingsWithKey)' ;;
        esac
        """)
        _ = try await store.loadSettings()

        await store.setPortal(keyId: "K", issuerId: "I", keyFile: "/k.p8")

        XCTAssertEqual(store.paths?.configDir, "/cfg")
        XCTAssertEqual(store.portalResult, .connected(devices: 0))
    }

    @MainActor
    func test_the_cli_version_is_read_off_the_settings_event() throws {
        let line = #"{"type":"settings","default_device_udid":null,"archive_keep":2,"signing_cert":null,"signing_profile":null,"signing_team_id":null,"graft_version":"1.1.0"}"#
        guard case let .settings(record)? = line.buildEvents().first else { return XCTFail() }
        XCTAssertEqual(record.cliVersion, "1.1.0")
        guard case let .settings(old)? = Self.settingsFromOldCLI.buildEvents().first else { return XCTFail() }
        XCTAssertNil(old.cliVersion)
    }

    func test_the_version_line_names_both_halves() {
        XCTAssertEqual(SettingsView.versionLine(app: "1.2", cli: "1.1.0", hasSettings: true),
                       "Graft 1.2 · graft 1.1.0")
        XCTAssertEqual(SettingsView.versionLine(app: "1.2", cli: nil, hasSettings: false),
                       "Graft 1.2 · graft: shown once graft has read your config")
        XCTAssertEqual(SettingsView.versionLine(app: "1.2", cli: nil, hasSettings: true),
                       "Graft 1.2 · graft: this version doesn't say which it is")
        XCTAssertEqual(SettingsView.versionLine(app: nil, cli: "1.1.0", hasSettings: true),
                       "graft 1.1.0")
    }

    /// The pane's Save and test: nothing to send until all three are there.
    func test_the_form_is_ready_only_with_every_field() {
        XCTAssertFalse(SettingsStore.portalFormReady(keyId: "K", issuerId: "", keyFile: "/k.p8"))
        XCTAssertFalse(SettingsStore.portalFormReady(keyId: " ", issuerId: "I", keyFile: "/k.p8"))
        XCTAssertFalse(SettingsStore.portalFormReady(keyId: "K", issuerId: "I", keyFile: ""))
        XCTAssertTrue(SettingsStore.portalFormReady(keyId: "K", issuerId: "I", keyFile: "/k.p8"))
    }

    /// A CLI whose preflight has the row an `auto_profile` app adds: its
    /// signing profile, green while a key is set and red once there is none.
    /// `keySet` is the state it starts in.
    @MainActor
    private func keyDependentChecks(keySet: Bool) throws -> (SettingsStore, URL) {
        let flag = FileManager.default.temporaryDirectory
            .appending(path: "settings_portal_key_\(UUID().uuidString)")
        if keySet { FileManager.default.createFile(atPath: flag.path, contents: Data()) }
        addTeardownBlock { try? FileManager.default.removeItem(at: flag) }
        return try portalStore("""
        case " $* " in
          *" --set-portal "*)
            touch '\(flag.path)'
            printf '%s\\n' '{"type":"portal-set","key_id":"KEY1234567","issuer_id":"ISS","key_file":"/cfg/AuthKey_KEY1234567.p8","devices":4}'
            printf '%s\\n' '\(Self.settingsWithKey)' ;;
          *" --clear-portal "*)
            rm -f '\(flag.path)'
            printf '%s\\n' '{"type":"portal-cleared","removed":true,"key_file":"/cfg/AuthKey_KEY1234567.p8"}'
            printf '%s\\n' '\(Self.settingsWithout)' ;;
          *" --check "*)
            if [ -e '\(flag.path)' ]; then
              printf '%s\\n' '{"type":"preflight-check","name":"signing profile","ok":true,"detail":"example: made through App Store Connect","hint":null}' '{"type":"preflight-end","ok_count":1,"fail_count":0}'
            else
              printf '%s\\n' '{"type":"preflight-check","name":"signing profile","ok":false,"detail":"example: auto_profile needs an App Store Connect key","hint":"set one in Settings"}' '{"type":"preflight-end","ok_count":0,"fail_count":1}'
              exit 3
            fi ;;
        esac
        """)
    }

    private func argv(_ log: URL) throws -> [String] {
        try String(contentsOf: log, encoding: .utf8).split(separator: "\n").map(String.init)
    }

    /// The setup checks depend on the key: an `auto_profile` app's signing
    /// profile row is red without one. `saveDraft` re-runs them; Save and
    /// test didn't, so the row a working key had just fixed stayed red until
    /// a manual Re-run.
    @MainActor
    func test_saving_a_key_re_runs_the_checks() async throws {
        let (store, log) = try keyDependentChecks(keySet: false)
        try await store.runChecks()
        XCTAssertEqual(store.checks.map(\.ok), [false], "premise: no key, so the row is red")

        await store.setPortal(keyId: "KEY1234567", issuerId: "ISS", keyFile: "/k.p8")

        XCTAssertEqual(store.portalResult, .connected(devices: 4), "premise: the key saved")
        XCTAssertEqual(store.checks.map(\.ok), [true],
                       "the signing profile row is still the scan from before the key")
        XCTAssertEqual(try argv(log).last, "--json --check")
        XCTAssertFalse(store.isRunning)
    }

    /// And the other way: after Remove the row stayed green over a key that
    /// is gone, which is the one build-breaking change this section makes.
    @MainActor
    func test_removing_the_key_re_runs_the_checks() async throws {
        let (store, log) = try keyDependentChecks(keySet: true)
        try await store.runChecks()
        XCTAssertEqual(store.checks.map(\.ok), [true], "premise: a key is set, so the row is green")

        await store.clearPortal()

        XCTAssertNil(store.settings?.appStoreConnect, "premise: the key is gone")
        XCTAssertEqual(store.checks.map(\.ok), [false],
                       "the signing profile row still says a key that is gone works")
        XCTAssertEqual(try argv(log).last, "--json --check")
    }

    /// A key App Store Connect turned away wrote nothing, so there is nothing
    /// new for a scan to find.
    @MainActor
    func test_a_refused_key_runs_no_checks() async throws {
        let (store, log) = try portalStore("echo 'error: App Store Connect rejected the JWT (401)' >&2; exit 10")

        await store.setPortal(keyId: "KEY1234567", issuerId: "ISS", keyFile: "/k.p8")

        let calls = try argv(log)
        XCTAssertEqual(calls.count, 1, "\(calls)")
        XCTAssertFalse(store.hasChecked)
    }

    /// The scan a save or Remove ends with must not race a Re-run, for the
    /// reason `isBusy` gives for `saveDraft`: whichever `--check` finished
    /// last would win, and could be the one from before the write.
    @MainActor
    func test_the_preflight_is_busy_while_the_key_saves() async throws {
        let (store, _) = try portalStore("""
        sleep 1
        printf '%s\\n' '{"type":"portal-set","key_id":"K","issuer_id":"I","key_file":"/cfg/AuthKey_K.p8","devices":0}'
        """)
        let save = Task { await store.setPortal(keyId: "K", issuerId: "I", keyFile: "/k.p8") }
        let deadline = Date().addingTimeInterval(5)
        while !store.isSavingPortal, Date() < deadline { try await Task.sleep(nanoseconds: 10_000_000) }
        XCTAssertTrue(store.isSavingPortal, "premise: the save is in flight")

        XCTAssertTrue(store.isBusy, "Re-run is live over a save that ends with its own scan")
        await save.value
    }
}
