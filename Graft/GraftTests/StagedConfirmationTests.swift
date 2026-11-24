import XCTest
@testable import Graft

/// After a get or a drop finishes, the pane says what it staged.
///
/// It used to return to exactly the state it was in before — the spinner
/// went, the version line refreshed if you were watching it — so a drop that
/// worked looked like a drop that did nothing. The store now keeps one line
/// per app saying what landed, and the next get, drop or build for that app
/// clears it.
@MainActor
final class StagedConfirmationTests: XCTestCase {
    /// A fake CLI answering a get, a drop and a version check the way the real
    /// one does. `encrypted` makes the drop take the decrypt path: a `decrypt`
    /// stage, then `appstore-decrypted`, then `ipa-provided`.
    private func fakeCLI(dropIsEncrypted encrypted: Bool = false) throws -> CLIRunner {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-staged-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let provided = #"{"type":"ipa-provided","app":"example","bundle_id":"com.example.app","version":"2026.46.0","filename":"com.example.app_2026.46.0.ipa","path":"/c/com.example.app_2026.46.0.ipa","size_bytes":1}"#
        let decryptSteps = """
        printf '%s\\n' '{"type":"appstore-stage","app":"example","stage":"decrypt"}'
        printf '%s\\n' '{"type":"appstore-decrypted","app":"example","encrypted_images":1,"decrypted_pages":10,"plaintext_verified":true}'
        printf '%s\\n' '{"type":"appstore-stage","app":"example","stage":"stage"}'
        """
        let script = dir.appendingPathComponent("graft")
        try """
        #!/bin/sh
        case " $* " in
          *" --appstore-get "*)
            printf '%s\\n' '{"type":"appstore-stage","app":"example","stage":"download"}'
            \(decryptSteps)
            printf '%s\\n' '\(provided)' ;;
          *" --provide-ipa "*)
            \(encrypted ? decryptSteps : "true")
            printf '%s\\n' '\(provided)' ;;
          *" --appstore-check "*)
            printf '%s\\n' '{"type":"appstore-version","app":"example","bundle_id":"com.example.app","store_version":"2026.46.0","cached_version":"2026.46.0","update_available":false,"error":null}' ;;
        esac
        exit 0
        """.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
        return CLIRunner(executable: script)
    }

    private func waitUntilIdle(_ store: AppStoreStore, _ app: String) async {
        let deadline = Date().addingTimeInterval(10)
        while store.isBusy(app), Date() < deadline {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    func test_a_finished_get_says_what_it_staged_from_the_app_store() async throws {
        let runner = try fakeCLI()
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        store.startGet(app: "example")
        await waitUntilIdle(store, "example")
        XCTAssertNil(store.errors["example"])
        XCTAssertEqual(store.staged["example"]?.message, "✓ Staged 2026.46.0 from the App Store")
    }

    func test_a_plain_drop_says_it_staged_the_file() async throws {
        let runner = try fakeCLI()
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        store.startProvide(
            app: "example", fileURL: URL(fileURLWithPath: "/tmp/Example.ipa"),
            config: ConfigStore(runner: runner, notify: { _ in })
        )
        await waitUntilIdle(store, "example")
        XCTAssertEqual(store.staged["example"]?.message, "✓ Staged 2026.46.0 from Example.ipa")
    }

    /// The case the user most needs told: the file they dropped is not the
    /// file that was staged — it was decrypted first.
    func test_an_encrypted_drop_says_it_was_decrypted() async throws {
        let runner = try fakeCLI(dropIsEncrypted: true)
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        store.startProvide(
            app: "example", fileURL: URL(fileURLWithPath: "/tmp/Example.ipa"),
            config: ConfigStore(runner: runner, notify: { _ in })
        )
        await waitUntilIdle(store, "example")
        XCTAssertEqual(
            store.staged["example"]?.message, "✓ Decrypted and staged 2026.46.0 from Example.ipa"
        )
    }

    /// "Until the next action": starting another get or drop for the app
    /// takes the line down at once, and so does a build.
    func test_the_next_action_clears_the_confirmation() async throws {
        let runner = try fakeCLI()
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        store.startGet(app: "example")
        await waitUntilIdle(store, "example")
        XCTAssertNotNil(store.staged["example"], "premise")

        store.startProvide(
            app: "example", fileURL: URL(fileURLWithPath: "/tmp/Example.ipa"),
            config: ConfigStore(runner: runner, notify: { _ in })
        )
        XCTAssertNil(store.staged["example"], "a new drop clears the old line while it runs")
        await waitUntilIdle(store, "example")
        XCTAssertNotNil(store.staged["example"])
    }

    // MARK: - A build retires it, whichever button started it

    /// Every build goes through `launchBuild`, and that is where the start is
    /// recorded — "Build for this Mac" included, which used to leave the line
    /// standing because only Build & Install cleared it.
    func test_every_build_launch_records_when_it_started() {
        let config = ConfigStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")), notify: { _ in })
        XCTAssertNil(config.buildStartedAt["example"])
        let before = ContinuousClock.now
        var options = BuildOptions()
        options.forPlaycover = true
        XCTAssertTrue(config.launchBuild(
            appName: "example", delivery: options.effectiveDelivery(for: .livecontainer)
        ) {})
        guard let started = config.buildStartedAt["example"] else {
            return XCTFail("a Mac build recorded no start")
        }
        XCTAssertGreaterThanOrEqual(started, before)
        XCTAssertNil(config.buildStartedAt["other"], "per app")
    }

    /// Explicit instants, never two reads of the clock: back to back, two
    /// `.now`s can be the same instant, which made "a get that finished after
    /// the build started" equal to it, and so retired, now and then under load.
    func test_a_build_after_the_line_landed_retires_it() {
        let landed = ContinuousClock.now
        let staged = AppStoreStore.Staged(source: .appStore, version: "2026.46.0", decrypted: true, at: landed)
        XCTAssertEqual(AppDetailView.stagedConfirmation(staged, buildStartedAt: nil), staged)
        XCTAssertNil(
            AppDetailView.stagedConfirmation(staged, buildStartedAt: landed.advanced(by: .milliseconds(1))),
            "a build that started after the line landed retires it"
        )
        XCTAssertNil(
            AppDetailView.stagedConfirmation(staged, buildStartedAt: landed),
            "so does one that started at the same instant"
        )
        XCTAssertEqual(
            AppDetailView.stagedConfirmation(staged, buildStartedAt: landed.advanced(by: .milliseconds(-1))),
            staged,
            "a get that finished after the build started is news again"
        )
        XCTAssertNil(AppDetailView.stagedConfirmation(nil, buildStartedAt: nil))
    }

    /// Keyed by app, like every other outcome on this store: a get for one
    /// app does not confirm on another's pane.
    func test_the_confirmation_belongs_to_its_own_app() async throws {
        let store = AppStoreStore(runner: try fakeCLI(), shell: NoAdminShell())
        store.startGet(app: "example")
        await waitUntilIdle(store, "example")
        XCTAssertNil(store.staged["other"])
    }

    /// The pane shows the line — read back off the window's accessibility
    /// tree, which is the one way a test can ask whether a SwiftUI `Text` is on
    /// screen — and a build takes it down, a Mac build included.
    func test_the_app_pane_shows_the_confirmation_until_a_build() async throws {
        try AccessibilityTree.requireServed()
        let appLine = #"{"type":"app","name":"example","bundle_id":"com.example.app","preferred_version":"latest","tweaks":[],"display_name":"Example+","delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}"#
        let sh = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let config = ConfigStore(runner: sh, cliArgs: ["-c", "printf '%s\\n' '\(appLine)'"], notify: { _ in })
        let apps = try await config.loadApps()
        let app = try XCTUnwrap(apps.first)
        let appStore = AppStoreStore(runner: try fakeCLI(), shell: NoAdminShell())
        let pane = AppDetailView(
            app: app, runner: sh, store: config, artwork: ArtworkStore(runner: sh),
            devices: DevicesStore(runner: sh), tweaksStore: TweaksStore(runner: sh),
            staged: StagedIPAStore(runner: sh), appStore: appStore,
            liveContainer: LiveContainerStore(runner: sh),
            settings: SettingsStore(runner: sh), showSettings: {}
        )
        let hosted = hosted(pane, size: NSSize(width: 900, height: 900))

        appStore.startGet(app: "example")
        await waitUntilIdle(appStore, "example")
        settle(hosted)
        let message = "✓ Staged 2026.46.0 from the App Store"
        let shown = try AccessibilityTree.texts(in: hosted.window)
        XCTAssertTrue(shown.contains(message), "\(shown)")

        // A Mac build — the button that used to leave the line up.
        var options = BuildOptions()
        options.forPlaycover = true
        config.launchBuild(appName: "example", delivery: options.effectiveDelivery(for: app.delivery)) {}
        settle(hosted)
        // Read first, not inside the assertion: a skip thrown from an
        // assertion's argument is recorded as a failure.
        let after = try AccessibilityTree.texts(in: hosted.window)
        XCTAssertFalse(after.contains(message), "\(after)")
    }

    /// C-UNVERIFIED on the pane: the refusal reads in the app's words with
    /// "Use it anyway…" under it, and once that has run, the CLI's warning
    /// sits under the confirmation.
    func test_the_pane_offers_use_it_anyway_and_then_shows_the_warning() async throws {
        try AccessibilityTree.requireServed()
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-unverified-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let script = dir.appendingPathComponent("graft")
        try """
        #!/bin/sh
        case " $* " in
          *" --appstore-check "*) exit 0 ;;
          *" --allow-unverified "*)
            printf '%s\\n' '{"type":"log","source":"warning","line":"example: staging a decrypt UnfairPlay could not verify"}'
            printf '%s\\n' '{"type":"ipa-provided","app":"example","bundle_id":"com.example.app","version":"1.0","filename":"com.example.app_1.0.ipa","path":"/c/com.example.app_1.0.ipa","size_bytes":1}'
            exit 0 ;;
        esac
        printf '%s\\n' '{"type":"appstore-refused","app":"example","reason":"unverified"}'
        echo "error: refusing to stage it" >&2
        echo "  hint: To accept it anyway, run in Terminal: graft --appstore-get --app example --allow-unverified" >&2
        exit 20
        """.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)

        let appLine = #"{"type":"app","name":"example","bundle_id":"com.example.app","preferred_version":"latest","tweaks":[],"display_name":"Example+","delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}"#
        let sh = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let config = ConfigStore(runner: sh, cliArgs: ["-c", "printf '%s\\n' '\(appLine)'"], notify: { _ in })
        let apps = try await config.loadApps()
        let app = try XCTUnwrap(apps.first)
        let appStore = AppStoreStore(runner: CLIRunner(executable: script), shell: NoAdminShell())
        let pane = AppDetailView(
            app: app, runner: sh, store: config, artwork: ArtworkStore(runner: sh),
            devices: DevicesStore(runner: sh), tweaksStore: TweaksStore(runner: sh),
            staged: StagedIPAStore(runner: sh), appStore: appStore,
            liveContainer: LiveContainerStore(runner: sh),
            settings: SettingsStore(runner: sh), showSettings: {}
        )
        let hosted = hosted(pane, size: NSSize(width: 900, height: 1100))

        appStore.startGet(app: "example")
        await waitUntilIdle(appStore, "example")
        settle(hosted)
        let nodes = try AccessibilityTree.nodes(in: hosted.window)
        XCTAssertTrue(
            nodes.contains { $0.role == kAXButtonRole && $0.name == UnverifiedDecrypt.offerButton },
            "\(nodes.map(\.name))"
        )
        let refused = try AccessibilityTree.texts(in: hosted.window)
        XCTAssertFalse(refused.contains { $0.contains("run in Terminal") }, "\(refused)")

        XCTAssertTrue(appStore.useUnverified(app: "example", config: config))
        await waitUntilIdle(appStore, "example")
        settle(hosted)
        let after = try AccessibilityTree.texts(in: hosted.window)
        XCTAssertTrue(
            after.contains("warning: example: staging a decrypt UnfairPlay could not verify"), "\(after)"
        )
        let afterNodes = try AccessibilityTree.nodes(in: hosted.window)
        XCTAssertFalse(afterNodes.contains { $0.name == UnverifiedDecrypt.offerButton })
    }

    /// A drop that failed confirms nothing.
    func test_a_failed_drop_confirms_nothing() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = AppStoreStore(runner: runner, shell: NoAdminShell())
        let config = ConfigStore(runner: runner, notify: { _ in })
        store.startProvide(
            app: "example", fileURL: URL(fileURLWithPath: "/tmp/Example.ipa"), config: config
        )
        await waitUntilIdle(store, "example")
        XCTAssertNotNil(store.errors["example"], "premise: /bin/sh given --json fails")
        XCTAssertNil(store.staged["example"])
    }
}
