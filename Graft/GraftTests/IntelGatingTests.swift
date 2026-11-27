import SwiftUI
import XCTest
@testable import Graft

/// Off the screen with it, and its view dropped, so whatever it started (a
/// hosted `ContentView`'s `.task`s) ends with it: what `HostedWindows` does
/// for every window when its test ends, done as soon as a test here has read
/// its window, so a test that hosts one per Mac shows one at a time.
@MainActor
func takeDown(_ box: Hosted) {
    box.window.orderOut(nil)
    box.window.contentView = nil
}

/// Every Apple-silicon-only action, disabled rather than hidden, with its
/// one line, where this copy can't do it: on an Intel Mac, and for the
/// Intel download on an Apple silicon Mac. And each one enabled, with no
/// line, for the Apple silicon download on Apple silicon.
///
/// `MacSupport` is injected as `\.macSupport`, the way `AppleSiliconGate`
/// passes it down in the app, so nothing here depends on the Mac running the
/// suite. The hosted ones read the accessibility tree, as a user's
/// VoiceOver would: whether each button is there, enabled, and what its
/// help says.
@MainActor
final class IntelGatingTests: XCTestCase {
    /// The three that disable things, and the one that doesn't.
    static let intelMac = MacSupport.intel
    static let intelDownloadOnAppleSilicon = MacSupport(appleSiliconMac: true, build: .flavor(.x86_64))
    static let appleSilicon = MacSupport.appleSilicon

    private func render<V: View>(_ view: V, _ support: MacSupport, width: CGFloat = 560) throws
        -> (nodes: [AccessibilityTree.Node], text: [String])
    {
        let box = hosted(view.environment(\.macSupport, support), width: width)
        addTeardownBlock { takeDown(box) }
        defer { takeDown(box) }
        settle(box, seconds: 0.2)
        let nodes = try AccessibilityTree.nodes(in: box.window)
        let text = nodes.flatMap { [$0.name, ($0.value as? String) ?? ""] }.filter { !$0.isEmpty }
        return (nodes, text)
    }

    private func button(_ name: String, in nodes: [AccessibilityTree.Node]) throws -> AccessibilityTree.Node {
        try XCTUnwrap(nodes.first { $0.role == kAXButtonRole && $0.name == name },
                      "no \(name) button in \(nodes.map(\.name))")
    }

    // MARK: - The lines

    func test_each_mac_and_download_has_its_line() {
        XCTAssertEqual(Self.intelMac.unavailableLine, "Needs a Mac with Apple silicon")
        XCTAssertEqual(Self.intelDownloadOnAppleSilicon.unavailableLine,
                       "Needs the Apple silicon download of Graft (Graft.zip)")
        XCTAssertNil(Self.appleSilicon.unavailableLine)
        XCTAssertEqual(MacSupport.intelBuildBanner,
                       "This is the Intel build. On an Apple silicon Mac, download Graft.zip for everything.")
    }

    /// Graft.zip needs macOS 14. On macOS 13, an Apple silicon Mac runs the
    /// Intel download through Rosetta, and the banner and lines say Graft.zip
    /// needs macOS 14 rather than send the user to a download that won't open.
    func test_before_macos_14_the_intel_build_says_graft_zip_needs_it() throws {
        var ventura = Self.intelDownloadOnAppleSilicon
        ventura.macOSMajorVersion = 13
        XCTAssertFalse(ventura.opensAppleSiliconDownload)
        XCTAssertEqual(ventura.intelBuildBannerLine,
                       "This is the Intel build. The Apple silicon download, Graft.zip, does everything, "
                           + "but needs macOS 14 or later.")
        XCTAssertEqual(ventura.unavailableLine,
                       "Needs the Apple silicon download of Graft (Graft.zip) and macOS 14 or later")
        let firstRun = try XCTUnwrap(ventura.firstRunLine)
        XCTAssertTrue(firstRun.contains("needs the Apple silicon download (Graft.zip) and macOS 14 or later."), firstRun)

        // From macOS 14, as before.
        for version in [14, 15, 27] {
            var sonoma = Self.intelDownloadOnAppleSilicon
            sonoma.macOSMajorVersion = version
            XCTAssertTrue(sonoma.opensAppleSiliconDownload)
            XCTAssertEqual(sonoma.intelBuildBannerLine, MacSupport.intelBuildBanner)
            XCTAssertEqual(sonoma.unavailableLine, MacSupport.needsAppleSiliconDownload)
            XCTAssertEqual(sonoma.firstRunLine, Self.intelDownloadOnAppleSilicon.firstRunLine)
        }

        // An Intel Mac has nothing to download instead, on any macOS.
        var intel = Self.intelMac
        intel.macOSMajorVersion = 13
        XCTAssertNil(intel.intelBuildBannerLine)
        XCTAssertEqual(intel.unavailableLine, MacSupport.needsAppleSilicon)
        XCTAssertEqual(intel.firstRunLine, Self.intelMac.firstRunLine)
        XCTAssertNil(Self.appleSilicon.intelBuildBannerLine)
    }

    func test_this_mac_is_read_with_its_own_macos() {
        XCTAssertEqual(MacSupport.thisMac.macOSMajorVersion,
                       ProcessInfo.processInfo.operatingSystemVersion.majorVersion)
    }

    // MARK: - App Store gets

    private func row(store: String?, cached: String?, license: AppStoreStore.LicenseNeed? = nil,
                     held: String? = nil) -> AppStoreSourceRow {
        AppStoreSourceRow(
            version: AppStoreVersionRecord(
                app: "example", bundleId: "com.example.app", storeVersion: store, cachedVersion: cached,
                updateAvailable: store != cached, error: nil, heldVersion: held
            ),
            isGetting: false, stage: nil, isBlocked: false,
            onGet: {}, onStop: {}, onRefresh: {}, license: license, appName: "Example"
        )
    }

    func test_without_apple_silicon_every_get_is_disabled_with_its_line_and_search_still_shows_the_store() throws {
        try AccessibilityTree.requireServed()
        for support in [Self.intelMac, Self.intelDownloadOnAppleSilicon] {
            let line = try XCTUnwrap(support.unavailableLine)
            // Get latest, and Get an older version… beside it.
            var (nodes, text) = try render(row(store: "2.0", cached: "1.0"), support)
            for name in ["Get latest from App Store", AppStoreSourceRow.olderVersionsButton] {
                let get = try button(name, in: nodes)
                XCTAssertEqual(get.enabled, false, name)
                XCTAssertEqual(get.help, line, name)
            }
            XCTAssertTrue(text.contains(line), "the line is on screen, not only in a tooltip: \(text)")
            XCTAssertTrue(text.contains("App Store 2.0 · cached 1.0 — update available"), "\(text)")
            // Get Again.
            (nodes, _) = try render(row(store: "2.0", cached: "2.0"), support)
            XCTAssertEqual(try button(AppStoreSourceRow.getAgainButton, in: nodes).enabled, false)
            // Get (free).
            let free = AppStoreStore.LicenseNeed(price: 0, formattedPrice: "Free", trackId: 1)
            (nodes, _) = try render(row(store: "2.0", cached: nil, license: free), support)
            let claim = try button("Get (free)", in: nodes)
            XCTAssertEqual(claim.enabled, false)
            XCTAssertEqual(claim.help, line)
        }
    }

    func test_on_apple_silicon_the_gets_are_enabled_with_no_line() throws {
        try AccessibilityTree.requireServed()
        let (nodes, text) = try render(row(store: "2.0", cached: "1.0"), Self.appleSilicon)
        XCTAssertNotEqual(try button("Get latest from App Store", in: nodes).enabled, false)
        XCTAssertNotEqual(try button(AppStoreSourceRow.olderVersionsButton, in: nodes).enabled, false)
        XCTAssertFalse(text.contains(MacSupport.needsAppleSilicon), "\(text)")
    }

    func test_this_mac_comes_before_what_is_running() {
        XCTAssertEqual(AppStoreSourceRow.blockedReason(unavailable: "mac", running: "a get"), "mac")
        XCTAssertEqual(AppStoreSourceRow.blockedReason(unavailable: nil, running: "a get"), "a get")
        XCTAssertNil(AppStoreSourceRow.blockedReason(unavailable: nil, running: nil))
    }

    // MARK: - Set Up Decryption

    private func status(present: Bool, enrolled: Bool = false) -> AppStoreStatusRecord {
        AppStoreStatusRecord(
            ipatool: .init(
                path: "/Applications/Graft.app/Contents/Helpers/ipatool", present: true, version: "2.8.0",
                signedIn: true, warning: nil
            ),
            unfairplay: .init(
                path: "/Applications/Graft.app/Contents/Helpers/unfairplay", present: present,
                version: present ? "dev-d75" : nil, enrolled: enrolled, modelDir: "/u/models", modelProblem: nil
            ),
            setupCommands: present ? [] : ["# App Store decryption can't be set up here: this Mac has an Intel processor."]
        )
    }

    func test_set_up_decryption_is_there_without_unfairplay_where_it_needs_apple_silicon() {
        let line = MacSupport.needsAppleSilicon
        XCTAssertEqual(AppStoreSettingsSection.enrollAction(status(present: false), unavailable: line), .setUp,
                       "shown, disabled: the Intel download has no UnfairPlay to find")
        XCTAssertEqual(AppStoreSettingsSection.enrollAction(status(present: true, enrolled: true), unavailable: line),
                       .setUpAgain)
        XCTAssertNil(AppStoreSettingsSection.enrollAction(status(present: false), unavailable: nil),
                     "on Apple silicon a missing UnfairPlay is a damaged app, as before")
    }

    func test_the_unfairplay_row_says_why_instead_of_a_path_that_looks_damaged() throws {
        let line = MacSupport.needsAppleSilicon
        let rows = AppStoreSettingsSection.rows(for: status(present: false), unavailable: line)
        let up = try XCTUnwrap(rows.first { $0.name == "UnfairPlay" })
        XCTAssertEqual(up.detail, AppStoreSettingsSection.notInThisDownload)
        XCTAssertEqual(up.warning, line)
        XCTAssertNil(up.install)
        XCTAssertFalse(up.ok)
        XCTAssertFalse(AppStoreSettingsSection.showsTerminalCommands(status(present: false), unavailable: line),
                       "no Terminal block for what can't be set up here")

        let asBefore = AppStoreSettingsSection.rows(for: status(present: false))
        XCTAssertEqual(asBefore.first { $0.name == "UnfairPlay" }?.detail,
                       "not found at /Applications/Graft.app/Contents/Helpers/unfairplay")
        XCTAssertTrue(AppStoreSettingsSection.showsTerminalCommands(status(present: false)))
    }

    /// A stand-in CLI whose App Store status is `line`.
    private func statusStore(_ line: String) async throws -> AppStoreStore {
        let dir = FileManager.default.temporaryDirectory.appending(path: "intel_gating_\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        try (line + "\n").write(to: dir.appending(path: "status.json"), atomically: true, encoding: .utf8)
        let file = dir.appending(path: "graft.sh")
        try """
        #!/bin/sh
        case "$*" in
          *--appstore-status*) /bin/cat '\(dir.path)/status.json' ;;
        esac
        """.write(to: file, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: file.path)
        let store = AppStoreStore(runner: CLIRunner(executable: file), shell: NoAdminShell())
        await store.loadStatus()
        XCTAssertNotNil(store.status, "the stand-in's status decoded")
        return store
    }

    /// The Intel download's status: no UnfairPlay in it, and the CLI's
    /// reason. Signed in, so nothing else in the section is waiting.
    static let intelStatus = ##"{"type":"appstore-status","ipatool":{"path":"/i","source":"bundled","present":true,"version":"2.8.0","signed_in":true,"email":"someone@example.invalid","legacy_signin":false,"warning":null},"unfairplay":{"path":"/u","present":false,"version":null,"enrolled":false,"model_dir":"/m"},"setup_commands":["# App Store decryption can't be set up here: this Mac has an Intel processor."]}"##
    /// Apple silicon, UnfairPlay there and not enrolled yet: Set Up Decryption… to press.
    static let unenrolledStatus = ##"{"type":"appstore-status","ipatool":{"path":"/i","source":"bundled","present":true,"version":"2.8.0","signed_in":true,"email":"someone@example.invalid","legacy_signin":false,"warning":null},"unfairplay":{"path":"/u","present":true,"version":"v0.4.0","enrolled":false,"model_dir":"/m"},"setup_commands":[]}"##

    /// Set Up Decryption… can't be pressed where it needs Apple silicon,
    /// whatever else would let it; elsewhere it follows the account's
    /// buttons. Without hosting, so it holds when macOS isn't serving the
    /// accessibility tree and the test below skips.
    func test_set_up_decryption_is_disabled_where_it_needs_apple_silicon() {
        for line in [MacSupport.needsAppleSilicon, MacSupport.needsAppleSiliconDownload] {
            XCTAssertFalse(AppStoreSettingsSection.enrollEnabled(actionsEnabled: true, unavailable: line), line)
            XCTAssertFalse(AppStoreSettingsSection.enrollEnabled(actionsEnabled: false, unavailable: line), line)
        }
        XCTAssertTrue(AppStoreSettingsSection.enrollEnabled(actionsEnabled: true, unavailable: nil))
        XCTAssertFalse(AppStoreSettingsSection.enrollEnabled(actionsEnabled: false, unavailable: nil),
                       "a get running, or status being read")
    }

    // MARK: - Settings › App Store's one card (a user's report)

    /// Where no App Store get can run, the section's one line: what can get
    /// apps, and what to do instead. The Intel download says which download
    /// can; before macOS 14, where Graft.zip doesn't open, it doesn't send
    /// anyone to it.
    func test_the_app_store_line_says_what_can_get_apps_and_what_to_do_instead() {
        let ventura = MacSupport(appleSiliconMac: true, build: .flavor(.x86_64), macOSMajorVersion: 13)

        XCTAssertEqual(Self.intelMac.appStoreLine,
                       "Getting apps from the App Store needs a Mac with Apple silicon. Add apps from a decrypted "
                       + "IPA instead: drop one on Source IPA in an app's pane.")
        XCTAssertEqual(Self.intelDownloadOnAppleSilicon.appStoreLine,
                       "Getting apps from the App Store needs the Apple silicon download of Graft, Graft.zip. Add "
                       + "apps from a decrypted IPA instead: drop one on Source IPA in an app's pane.")
        XCTAssertEqual(ventura.appStoreLine,
                       "Getting apps from the App Store needs Graft's Apple silicon download, which needs macOS 14 "
                       + "or later. Add apps from a decrypted IPA instead: drop one on Source IPA in an app's pane.")
        XCTAssertFalse(ventura.appStoreLine?.contains("Graft.zip") ?? true)
        XCTAssertNil(Self.appleSilicon.appStoreLine)
    }

    /// The card offers Sign Out only for a sign-in status read as there, so
    /// nobody's stuck signed in; with no status, no buttons. None on Apple
    /// silicon, where the rows are as they were.
    func test_the_card_offers_sign_out_only_when_signed_in() throws {
        let signedIn = try decoded(Self.intelStatus)
        let signedOut = try decoded(Self.signedOutIntelStatus)

        XCTAssertEqual(AppStoreSettingsSection.unavailableCard(Self.intelMac, status: signedIn),
                       .init(line: try XCTUnwrap(Self.intelMac.appStoreLine), signOut: true))
        XCTAssertEqual(AppStoreSettingsSection.unavailableCard(Self.intelMac, status: signedOut)?.signOut, false)
        XCTAssertEqual(AppStoreSettingsSection.unavailableCard(Self.intelMac, status: nil)?.signOut, false,
                       "no status read yet: the line, without Sign Out")
        XCTAssertNil(AppStoreSettingsSection.unavailableCard(Self.appleSilicon, status: signedIn))
    }

    private func decoded(_ line: String) throws -> AppStoreStatusRecord {
        guard case let .appStoreStatus(status) = try BuildEvent.decode(line: line) else {
            throw XCTSkip("fixture is not an appstore-status")
        }
        return status
    }

    private func buttons(_ nodes: [AccessibilityTree.Node]) -> [String] {
        nodes.filter { $0.role == kAXButtonRole }.map(\.name)
    }

    /// On screen, in Settings › App Store, where it needs Apple silicon: the
    /// card's line, as text; no rows, no Set Up Decryption…, no Sign In… or
    /// Sign In Again…, no Terminal block; Sign Out, for the sign-in there.
    func test_without_apple_silicon_settings_shows_one_card_and_only_sign_out() async throws {
        try AccessibilityTree.requireServed()
        for support in [Self.intelMac, Self.intelDownloadOnAppleSilicon] {
            let line = try XCTUnwrap(support.appStoreLine)
            let store = try await statusStore(Self.intelStatus)
            let (nodes, text) = try render(AppStoreSettingsSection(store: store), support, width: 720)
            XCTAssertTrue(text.contains(line), "\(support): \(text)")
            let shown = buttons(nodes)
            for gone in [CLIContract.SettingsLabel.setUpDecryption, CLIContract.SettingsLabel.signIn,
                         CLIContract.SettingsLabel.signInAgain] {
                XCTAssertFalse(shown.contains(gone), "\(support): \(shown)")
            }
            XCTAssertNotEqual(try button("Sign Out", in: nodes).enabled, false, "\(support)")
            XCTAssertFalse(text.contains { $0.contains(AppStoreSettingsSection.notInThisDownload) }, "no rows: \(text)")
            XCTAssertFalse(text.contains { $0.contains("can't be set up here") }, "no Terminal block: \(text)")
        }
    }

    /// The control: on Apple silicon, with UnfairPlay and no enrollment,
    /// Set Up Decryption… can be pressed and no line says otherwise.
    func test_set_up_decryption_can_be_pressed_on_apple_silicon() async throws {
        try AccessibilityTree.requireServed()
        let store = try await statusStore(Self.unenrolledStatus)
        let (nodes, text) = try render(AppStoreSettingsSection(store: store), Self.appleSilicon, width: 720)
        XCTAssertNotEqual(try button(CLIContract.SettingsLabel.setUpDecryption, in: nodes).enabled, false)
        XCTAssertFalse(text.contains(MacSupport.needsAppleSilicon), "\(text)")
    }

    // MARK: - App Store sign-in and the older versions

    /// Sign In… and Sign In Again… can't be pressed where no App Store
    /// request may run (the Intel build can't run ipatool's emulator),
    /// whatever else would let them; elsewhere they follow the account's
    /// buttons. Without hosting, as `enrollEnabled` is.
    func test_sign_in_is_disabled_where_store_requests_cant_run() {
        for line in [MacSupport.needsAppleSilicon, MacSupport.needsAppleSiliconDownload] {
            XCTAssertFalse(AppStoreSettingsSection.signInEnabled(actionsEnabled: true, unavailable: line), line)
        }
        XCTAssertTrue(AppStoreSettingsSection.signInEnabled(actionsEnabled: true, unavailable: nil))
        XCTAssertFalse(AppStoreSettingsSection.signInEnabled(actionsEnabled: false, unavailable: nil))
    }

    static let signedOutIntelStatus = ##"{"type":"appstore-status","ipatool":{"path":"/i","source":"bundled","present":true,"version":"2.8.0","signed_in":false,"email":null,"legacy_signin":false,"warning":null},"unfairplay":{"path":"/u","present":false,"version":null,"enrolled":false,"model_dir":"/m"},"setup_commands":[]}"##

    /// Signed out, the card has no buttons at all: nothing grayed out to
    /// read as a fault.
    func test_signed_out_without_apple_silicon_the_card_has_no_buttons() async throws {
        try AccessibilityTree.requireServed()
        for support in [Self.intelMac, Self.intelDownloadOnAppleSilicon] {
            let store = try await statusStore(Self.signedOutIntelStatus)
            let (nodes, text) = try render(AppStoreSettingsSection(store: store), support, width: 720)
            XCTAssertTrue(text.contains(try XCTUnwrap(support.appStoreLine)), "\(support): \(text)")
            XCTAssertEqual(buttons(nodes).filter { $0 != "Refresh" }, [], "\(support)")
        }
    }

    /// Before any status read, where it needs Apple silicon: the line, not
    /// "Checking ipatool and UnfairPlay…" (nothing on the card waits for it).
    func test_without_apple_silicon_the_card_never_says_checking() throws {
        try AccessibilityTree.requireServed()
        let store = AppStoreStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/false")),
                                  shell: NoAdminShell())
        let (_, text) = try render(AppStoreSettingsSection(store: store), Self.intelMac, width: 720)

        XCTAssertTrue(text.contains(MacSupport.appStoreNeedsAppleSilicon), "\(text)")
        XCTAssertFalse(text.contains("Checking ipatool and UnfairPlay…"), "\(text)")
    }

    func test_the_versions_sheet_asks_only_where_store_requests_can_run() {
        XCTAssertFalse(AppStoreVersionsSheet.loads(unavailable: MacSupport.needsAppleSilicon))
        XCTAssertFalse(AppStoreVersionsSheet.loads(unavailable: MacSupport.needsAppleSiliconDownload))
        XCTAssertTrue(AppStoreVersionsSheet.loads(unavailable: nil))
    }

    /// The sheet itself, opened anyway: where App Store requests can't run,
    /// it never runs the CLI's `--appstore-versions` (ipatool's
    /// `list-versions`); on Apple silicon it does. No accessibility tree
    /// needed: the stand-in CLI records every run.
    func test_the_versions_sheet_runs_nothing_without_apple_silicon() throws {
        let dir = FileManager.default.temporaryDirectory.appending(path: "intel_versions_\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let log = dir.appending(path: "argv.txt")
        let file = dir.appending(path: "graft.sh")
        try """
        #!/bin/sh
        printf '%s\\n' "$*" >> '\(log.path)'
        """.write(to: file, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: file.path)
        for (support, runs) in [(Self.intelMac, false), (Self.intelDownloadOnAppleSilicon, false),
                                (Self.appleSilicon, true)] {
            try? FileManager.default.removeItem(at: log)
            let store = AppStoreVersionsStore(runner: CLIRunner(executable: file))
            let sheet = AppStoreVersionsSheet(app: "example", heldId: nil, displayName: "Example", store: store,
                                              getBlockedReason: nil, onGet: { _ in })
            let box = hosted(sheet.environment(\.macSupport, support), size: NSSize(width: 520, height: 540))
            addTeardownBlock { takeDown(box) }
            let deadline = Date().addingTimeInterval(runs ? 5 : 1.5)
            while !FileManager.default.fileExists(atPath: log.path), Date() < deadline {
                RunLoop.main.run(until: Date().addingTimeInterval(0.05))
            }
            takeDown(box)
            XCTAssertEqual(FileManager.default.fileExists(atPath: log.path), runs, "\(support)")
            if !runs { XCTAssertEqual(store.phase, .idle, "\(support)") }
        }
    }

    // MARK: - Build for this Mac, Get PlayCover

    private func macSection(playCoverInstalled: Bool) -> some View {
        MacBuildSection(
            playCover: PlayCover(lookup: { _ in playCoverInstalled ? URL(fileURLWithPath: "/Applications/PlayCover.app") : nil },
                                 openURL: { _ in }),
            isBuildDisabled: false, buildHelp: "Build for this Mac", onBuild: {}
        )
        .font(AppFont.body)
        .padding(Metrics.popoverGutter)
    }

    func test_without_apple_silicon_build_for_this_mac_and_get_playcover_are_disabled_with_the_line() throws {
        try AccessibilityTree.requireServed()
        for support in [Self.intelMac, Self.intelDownloadOnAppleSilicon] {
            let line = try XCTUnwrap(support.unavailableLine)
            let (nodes, text) = try render(macSection(playCoverInstalled: false), support, width: 300)
            for name in [MacBuildSection.buildLabel, MacBuildSection.getLabel] {
                let control = try button(name, in: nodes)
                XCTAssertEqual(control.enabled, false, name)
                XCTAssertEqual(control.help, line, name)
            }
            XCTAssertTrue(text.contains(line), "\(text)")
            XCTAssertFalse(text.contains(MacBuildSection.missingMessage),
                           "not \"install PlayCover\", which wouldn't help: \(text)")
        }
    }

    func test_on_apple_silicon_build_for_this_mac_and_get_playcover_are_enabled() throws {
        try AccessibilityTree.requireServed()
        let (nodes, text) = try render(macSection(playCoverInstalled: false), Self.appleSilicon, width: 300)
        XCTAssertNotEqual(try button(MacBuildSection.buildLabel, in: nodes).enabled, false)
        XCTAssertNotEqual(try button(MacBuildSection.getLabel, in: nodes).enabled, false)
        XCTAssertTrue(text.contains(MacBuildSection.missingMessage))
    }

    // MARK: - Install to Mac

    /// A finished build for this Mac, as `--builds` lists one.
    private func macBuild() throws -> BuildRecord {
        let json = """
        {"type":"build-record","id":1,"timestamp":"2026-11-24T12:00:00Z",\
        "app":"example","bundle_id":"com.example.app","version":"2.1",\
        "tweaks":[],"ipa_path":"/cache/built/example-mac.ipa","ipa_sha256":null,"log_path":null,\
        "signing_team":null,"udid":null,"target":"mac","success":true,"error":null}
        """
        return try XCTUnwrap(json.buildEvents().compactMap { event -> BuildRecord? in
            if case let .buildRecord(record) = event { return record }
            return nil
        }.first)
    }

    func test_install_to_mac_in_builds_says_why_it_is_disabled() throws {
        let build = try macBuild()
        XCTAssertEqual(
            BuildsView.installItem(for: build, refusal: nil, unavailable: MacSupport.needsAppleSilicon),
            BuildsView.MenuItem(title: "Install to Mac… (needs a Mac with Apple silicon)", enabled: false)
        )
        XCTAssertEqual(
            BuildsView.installItem(for: build, refusal: nil, unavailable: Self.intelDownloadOnAppleSilicon.unavailableLine),
            BuildsView.MenuItem(title: "Install to Mac… (needs the Apple silicon download of Graft (Graft.zip))",
                                enabled: false)
        )
        XCTAssertEqual(BuildsView.installItem(for: build, refusal: nil, unavailable: nil),
                       BuildsView.MenuItem(title: "Install to Mac…", enabled: true))
    }

    func test_install_to_mac_in_the_cache_is_disabled_and_a_click_does_nothing() throws {
        try AccessibilityTree.requireServed()
        final class Pressed { var installed = 0; var added = 0 }
        for (support, works) in [(Self.intelMac, false), (Self.intelDownloadOnAppleSilicon, false),
                                 (Self.appleSilicon, true)] {
            let pressed = Pressed()
            let row = CacheRow(
                record: try stagedIPARecord(),
                onInstall: { _ in pressed.installed += 1 },
                onAdd: { _ in pressed.added += 1 },
                onDelete: { _ in }
            )
            .environment(\.macSupport, support)
            let box = hosted(row, width: 500)
            addTeardownBlock { takeDown(box) }
            defer { takeDown(box) }
            settle(box, seconds: 0.2)
            // Left to right: Install to Mac, Add, Delete (`CacheRow.controls`).
            func centre(_ fromTrailing: Int) -> NSPoint {
                let step = CacheRow.controlSize + CacheRow.controlSpacing
                return NSPoint(x: box.size.width - CacheRow.controlSize / 2 - CGFloat(fromTrailing) * step,
                               y: box.size.height / 2)
            }
            click(at: centre(2), in: box.window)
            click(at: centre(1), in: box.window)
            settle(box, seconds: 0.2)
            XCTAssertEqual(pressed.installed, works ? 1 : 0, "\(support)")
            XCTAssertEqual(pressed.added, 1, "Add stays: \(support)")
        }
    }

    // MARK: - Add App's get after adding

    func test_the_add_sheet_never_offers_a_get_or_asks_status_where_no_get_can_run() {
        let line = MacSupport.needsAppleSilicon
        let pick = AppStoreSearchResult(
            trackId: 100000001, bundleId: "com.example.app", name: "Example Pro", developer: "Dev",
            version: "2.1", price: 0, formattedPrice: "Free", currency: "USD",
            artworkUrl: nil, iconPath: nil, configuredAs: nil
        )
        XCTAssertEqual(AddAppSheet.getOffer(status: nil, hasLoaded: true, unavailable: line), .unavailable)
        XCTAssertEqual(AddAppSheet.getOffer(status: nil, hasLoaded: false, unavailable: line), .unavailable)
        let ready = status(present: true, enrolled: true)
        XCTAssertEqual(AddAppSheet.getOffer(status: ready, hasLoaded: true), .ready, "the control")
        XCTAssertEqual(AddAppSheet.getOffer(status: ready, hasLoaded: true, unavailable: line), .unavailable,
                       "whatever status says")
        XCTAssertFalse(AddAppSheet.wantsStatus(mode: .search, picked: pick, unavailable: line),
                       "no keychain read for a get that can't run")
        XCTAssertTrue(AddAppSheet.wantsStatus(mode: .search, picked: pick))
    }

    // MARK: - First run

    func test_welcome_says_what_this_mac_skips_and_why() throws {
        try AccessibilityTree.requireServed()
        for support in [Self.intelMac, Self.intelDownloadOnAppleSilicon, Self.appleSilicon] {
            let view = AccountSetupView(store: AccountSetupStore(runner: accountTestShell), onFinished: {})
                .padding(Metrics.paneGutter)
            let (_, text) = try render(view, support, width: 600)
            XCTAssertTrue(text.contains(AccountSetupView.welcomeTitle), "\(text)")
            if let line = support.firstRunLine {
                XCTAssertTrue(text.contains(line), "\(text)")
            } else {
                XCTAssertFalse(text.contains { $0.contains("Setup skips") || $0.contains("setup skips") }, "\(text)")
            }
        }
        XCTAssertEqual(Self.intelMac.firstRunLine,
                       "Setup skips getting apps from the App Store: decrypting them needs a Mac with Apple "
                       + "silicon. Graft works with a decrypted IPA you already have, or one dumped from your "
                       + "own jailbroken iPhone or iPad.")
        XCTAssertNotNil(Self.intelDownloadOnAppleSilicon.firstRunLine?.range(of: "Graft.zip"))
        XCTAssertNil(Self.appleSilicon.firstRunLine)
    }

    // MARK: - A jailbroken phone's decrypt card

    func test_the_decrypt_card_offers_the_app_store_only_where_it_works() {
        XCTAssertTrue(DeviceDecryptSection.unavailableLine.contains("Get latest from App Store"))
        XCTAssertFalse(DeviceDecryptSection.unavailableLineWithoutAppStore.contains("App Store"))
    }
}
