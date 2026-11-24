import XCTest
@testable import Graft

/// Settings → Paths shows the directories the CLI resolved, not a hardcoded
/// `~/.config/graft`.
///
/// The CLI half — that `--settings` reports `paths` at all, resolved the way a
/// build resolves them — is `tests/test_cli_settings_paths.py`, plus
/// `CLIContractTests.test_the_settings_event_reports_the_resolved_paths`
/// against the installed CLI. These are the app half: decoding, keeping the
/// answer across a save, and putting each directory on the row that names it.
@MainActor
final class SettingsPathsTests: XCTestCase {
    private static let paths = #""paths":{"config_dir":"/x/cfg/graft","cache_dir":"/x/ipas","built_dir":"/x/built","state_dir":"/x/state/graft","tweaks_dir":"/x/debs"}"#
    private static let settings = #"{"type":"settings","default_device_udid":null,"archive_keep":2,"signing_cert":"/c.p12","signing_profile":"/p.mp","signing_team_id":"TEAM1"}"#

    private static var settingsWithPaths: String {
        String(settings.dropLast()) + "," + paths + "}"
    }

    func test_the_paths_decode_beside_the_settings() throws {
        guard case let .settings(record)? = Self.settingsWithPaths.buildEvents().first else {
            return XCTFail("did not decode")
        }
        XCTAssertEqual(record.signingTeamId, "TEAM1", "the settings still decode")
        XCTAssertEqual(record.paths, SettingsPaths(
            configDir: "/x/cfg/graft", cacheDir: "/x/ipas", builtDir: "/x/built",
            stateDir: "/x/state/graft", tweaksDir: "/x/debs"
        ))
    }

    /// An older CLI, and `--set-settings`' echo, carry no paths. That must not
    /// cost the settings editor.
    func test_a_settings_event_without_paths_still_decodes() throws {
        guard case let .settings(record)? = Self.settings.buildEvents().first else {
            return XCTFail("did not decode")
        }
        XCTAssertNil(record.paths)
        XCTAssertEqual(record.archiveKeep, 2)
    }

    /// Each directory on the row that names it. Two of these are the same kind
    /// of string, so a swap would type-check and show the cache as the tweaks
    /// folder.
    func test_each_row_shows_its_own_directory() {
        let rows = SettingsView.entries(SettingsPaths(
            configDir: "C", cacheDir: "IPA", builtDir: "B", stateDir: "S", tweaksDir: "T"
        ))
        XCTAssertEqual(rows, [
            .init(label: "Config", path: "C"),
            .init(label: "Tweaks", path: "T"),
            .init(label: "IPA cache", path: "IPA"),
            .init(label: "Built IPAs", path: "B"),
            .init(label: "State", path: "S"),
        ])
    }

    // MARK: - The Tweaks pane's empty state

    /// It named `~/.config/graft/tweaks/` whatever the config said. Now it
    /// names the CLI's folder, and before that is known, no folder at all.
    func test_the_empty_tweaks_list_names_the_clis_folder() {
        XCTAssertEqual(
            TweaksView.emptyText(libraryIsEmpty: true, orphansOnly: false, tweaksDir: "/x/debs"),
            "No tweaks in /x/debs."
        )
        let unknown = TweaksView.emptyText(libraryIsEmpty: true, orphansOnly: false, tweaksDir: nil)
        XCTAssertFalse(unknown.contains("/"), "no guessed path: \(unknown)")
        XCTAssertFalse(unknown.contains("~"), unknown)
        XCTAssertEqual(
            TweaksView.emptyText(libraryIsEmpty: false, orphansOnly: false, tweaksDir: "/x/debs"),
            "No tweaks match the filter."
        )
    }

    /// The window, on the Tweaks section, against a CLI with no tweaks and a
    /// `tweaks_dir` of its own: the sentence on screen is that folder.
    func test_the_tweaks_pane_reads_its_folder_from_the_cli() async throws {
        try AccessibilityTree.requireServed()
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-tweaks-dir-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let graft = dir.appendingPathComponent("graft")
        try """
        #!/bin/sh
        echo "$*" >> '\(dir.path)/calls'
        case " $* " in
          *" --settings "*) printf '%s\\n' '\(Self.settingsWithPaths)' ;;
          *" --list "*) printf '%s\\n' '{"type":"account","account":"free"}' ;;
        esac
        exit 0
        """.write(to: graft, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: graft.path)

        let store = ConfigStore(runner: CLIRunner(executable: graft), notify: { _ in })
        let pane = hosted(
            ContentView(store: store, selection: .section(.tweaks), adminShell: NoAdminShell()),
            size: NSSize(width: 1100, height: 700)
        )
        let expected = "No tweaks in /x/debs."
        let deadline = Date().addingTimeInterval(5)
        var shown: [String] = []
        while Date() < deadline {
            // Suspend, not only pump: this test is itself a main-queue job,
            // and the pane's `.task`s are main-actor work that runs only
            // while it is suspended.
            try await Task.sleep(nanoseconds: 100_000_000)
            settle(pane, seconds: 0.05)
            shown = try AccessibilityTree.texts(in: pane.window)
            if shown.contains(expected) { break }
        }
        let calls = (try? String(contentsOf: dir.appendingPathComponent("calls"), encoding: .utf8)) ?? ""
        XCTAssertTrue(shown.contains(expected), "\(shown); the CLI was run as: \(calls)")
    }

    /// `--set-settings` echoes the settings without paths, and a save replaces
    /// `settings` with that echo. The section must not blank on every save.
    func test_a_save_keeps_the_paths_the_last_read_reported() async throws {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-paths-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let cli = dir.appendingPathComponent("graft")
        let script = """
        #!/bin/sh
        for arg in "$@"; do
          case "$arg" in
            --set-settings)
              printf '%s\\n' '{"type":"settings-updated","changed":["signing_team_id"]}'
              printf '%s\\n' '\(Self.settings.replacingOccurrences(of: "TEAM1", with: "TEAM9"))'
              exit 0 ;;
            --settings) printf '%s\\n' '\(Self.settingsWithPaths)'; exit 0 ;;
            --check) exit 0 ;;
          esac
        done
        exit 0
        """
        try script.write(to: cli, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: cli.path)

        let store = SettingsStore(runner: CLIRunner(executable: cli))
        _ = try await store.loadSettings()
        XCTAssertEqual(store.paths?.tweaksDir, "/x/debs", "premise: the read reported them")

        store.reseedDraftIfIdle()
        store.draft.edited.signingTeamId = "TEAM9"
        await store.saveDraft()
        XCTAssertNil(store.lastError)
        XCTAssertEqual(store.settings?.signingTeamId, "TEAM9", "premise: the save landed")
        XCTAssertNil(store.settings?.paths, "premise: the echo carried no paths")
        XCTAssertEqual(store.paths?.tweaksDir, "/x/debs")
    }

    // MARK: - K4 / C-SETTINGS: a config.yaml that does not parse

    /// What `--settings` says about a config.yaml with a YAML error: every
    /// setting null, the default paths, and why.
    private static let unparsed = #"{"type":"settings","default_device_udid":null,"archive_keep":null,"signing_cert":null,"signing_profile":null,"signing_team_id":null,"parse_error":"mapping values are not allowed here (line 3, column 9)","paths_are_defaults":true,"#
        + paths + "}"

    func test_the_parse_error_and_the_default_paths_decode() throws {
        guard case let .settings(record)? = Self.unparsed.buildEvents().first else {
            return XCTFail("did not decode")
        }
        XCTAssertEqual(record.parseError, "mapping values are not allowed here (line 3, column 9)")
        XCTAssertTrue(record.pathsAreDefaults)
        XCTAssertNil(record.archiveKeep)
        XCTAssertNotNil(record.paths)
    }

    /// Absent from every CLI before C-SETTINGS, and from `--set-settings`'
    /// echo: no error, and paths that are this config's.
    func test_a_settings_event_without_them_is_a_config_that_parsed() throws {
        guard case let .settings(record)? = Self.settingsWithPaths.buildEvents().first else {
            return XCTFail("did not decode")
        }
        XCTAssertNil(record.parseError)
        XCTAssertFalse(record.pathsAreDefaults)
    }

    func test_the_store_carries_the_error_and_marks_the_paths_as_defaults() async throws {
        let store = SettingsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        _ = try await store.loadSettings(argsOverride: ["-c", "printf '%s\\n' '\(Self.unparsed)'"])
        XCTAssertEqual(store.parseError, "mapping values are not allowed here (line 3, column 9)")
        XCTAssertTrue(store.pathsAreDefaults)

        // Fixed in an editor, and read again: both clear.
        _ = try await store.loadSettings(argsOverride: ["-c", "printf '%s\\n' '\(Self.settingsWithPaths)'"])
        XCTAssertNil(store.parseError)
        XCTAssertFalse(store.pathsAreDefaults)
    }

    /// Never "saving replaces it" over a file no save can write.
    func test_the_retention_caption_never_offers_a_save_that_cannot_land() {
        let broken = SettingsView.archiveKeepNote(parseError: "bad", archiveKeep: nil)
        XCTAssertFalse(broken.text.contains("saving replaces it"), broken.text)
        XCTAssertTrue(broken.warns)
        XCTAssertEqual(
            SettingsView.archiveKeepNote(parseError: nil, archiveKeep: nil).text,
            "config.yaml's value can't be read — saving replaces it",
            "a value the CLI refuses in a file that parses is one a save does replace"
        )
        XCTAssertEqual(SettingsView.archiveKeepNote(parseError: nil, archiveKeep: 2).text,
                       "per app, pruned after each build")
    }

    /// A save is not attempted at all: `--set-settings` exits 2 on that file.
    func test_a_config_that_does_not_parse_is_never_saved_to() async throws {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-unparsed-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let calls = dir.appendingPathComponent("calls")
        let cli = dir.appendingPathComponent("graft")
        try """
        #!/bin/sh
        echo "$*" >> '\(calls.path)'
        case " $* " in
          *" --settings "*) printf '%s\\n' '\(Self.unparsed)'; exit 0 ;;
        esac
        exit 2
        """.write(to: cli, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: cli.path)
        let store = SettingsStore(runner: CLIRunner(executable: cli))
        _ = try await store.loadSettings()
        store.reseedDraftIfIdle()
        store.draft.edited.archiveKeep = 5
        await store.saveDraft()
        let log = (try? String(contentsOf: calls, encoding: .utf8)) ?? ""
        XCTAssertFalse(log.contains("--set-settings"), log)
    }

    /// The pane says it, at the top, and labels the paths.
    func test_the_pane_shows_the_parse_error_and_labels_the_paths() async throws {
        try AccessibilityTree.requireServed()
        let store = SettingsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        _ = try await store.loadSettings(argsOverride: ["-c", "printf '%s\\n' '\(Self.unparsed)'"])
        store.reseedDraftIfIdle()
        let sh = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let pane = SettingsView(
            store: store, devicesStore: DevicesStore(runner: sh),
            appStore: AppStoreStore(runner: sh, shell: NoAdminShell()), account: .paid
        )
        let box = hosted(pane, size: NSSize(width: 900, height: 1600))
        try await Task.sleep(nanoseconds: 300_000_000)
        settle(box)
        let shown = try AccessibilityTree.texts(in: box.window)
        let lines = SettingsView.parseErrorLines("mapping values are not allowed here (line 3, column 9)")
        XCTAssertTrue(shown.contains(lines.title), "\(shown)")
        XCTAssertTrue(shown.contains(SettingsView.pathsAreDefaultsNote), "\(shown)")
        XCTAssertFalse(shown.contains { $0.contains("saving replaces it") }, "\(shown)")
    }
}
