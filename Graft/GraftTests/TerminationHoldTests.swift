import AppKit
import XCTest
@testable import Graft

/// Whole-diff review, app-3: macOS mustn't end Graft (sudden or automatic
/// termination) while a build or a re-install, an App Store get, drop or Get
/// (free), an `--init` setup, an enrollment, a LiveContainer install or a
/// decrypt dump runs (the re-install and the dump since the try14 delta
/// audit, D14-APP-2); and each hold is let go on every path, or the app
/// could never be terminated that way again. Every store gets a
/// `FakeControl` in place of `ProcessInfo`, and a stand-in CLI that waits for
/// the test's go-ahead, so the hold can be seen while the work runs.
@MainActor
final class TerminationHoldTests: XCTestCase {
    /// What `ProcessInfo` would have been asked, by reason.
    final class FakeControl: TerminationControl, @unchecked Sendable {
        private let lock = NSLock()
        private var sudden = 0
        private var automatic: [String: Int] = [:]
        private var taken: [String: Int] = [:]
        private var wentNegative = false

        func disableSuddenTermination() { lock.withLock { sudden += 1 } }
        func enableSuddenTermination() {
            lock.withLock {
                sudden -= 1
                if sudden < 0 { wentNegative = true }
            }
        }
        func disableAutomaticTermination(_ reason: String) {
            lock.withLock {
                automatic[reason, default: 0] += 1
                taken[reason, default: 0] += 1
            }
        }
        func enableAutomaticTermination(_ reason: String) {
            lock.withLock {
                automatic[reason, default: 0] -= 1
                if automatic[reason, default: 0] < 0 { wentNegative = true }
            }
        }

        /// Both are off, `reason` among the automatic ones.
        func isHeld(_ reason: String) -> Bool {
            lock.withLock { sudden > 0 && automatic[reason, default: 0] > 0 }
        }

        /// `reason` was held, and has been let go as often as it was taken.
        func isLetGo(_ reason: String) -> Bool {
            lock.withLock { taken[reason, default: 0] > 0 && automatic[reason, default: 0] == 0 }
        }

        /// Nothing is held, and nothing was let go that wasn't taken.
        var isBalanced: Bool {
            lock.withLock { sudden == 0 && automatic.values.allSatisfy { $0 == 0 } && !wentNegative }
        }
    }

    private var dir: URL!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appending(path: "hold_\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    /// The test's go-ahead to the stand-in: until it exists, `wait` loops.
    private func go() throws { try Data().write(to: dir.appending(path: "go")) }
    private var wait: String { "while [ ! -e '\(dir.path)/go' ]; do /bin/sleep 0.05; done" }

    private func cli(_ body: String) throws -> CLIRunner {
        let file = dir.appending(path: "graft.sh")
        try "#!/bin/sh\n\(body)\n".write(to: file, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: file.path)
        return CLIRunner(executable: file)
    }

    // MARK: The hold itself

    func test_begin_turns_both_off_and_end_turns_them_back_on() {
        let control = FakeControl()
        let hold = TerminationHold(control: control)
        hold.begin("one")
        hold.begin("two")
        XCTAssertTrue(control.isHeld("one"))
        hold.end("one")
        XCTAssertTrue(control.isHeld("two"), "the other is still held")
        hold.end("two")
        XCTAssertTrue(control.isBalanced)
        XCTAssertTrue(control.isLetGo("one") && control.isLetGo("two"))
    }

    // MARK: A build

    @MainActor
    private final class Gate {
        var open = false
        var finished = false
    }

    func test_a_build_holds_until_it_ends_whether_it_finishes_or_is_stopped() async throws {
        for stop in [false, true] {
            let control = FakeControl()
            let store = ConfigStore(
                runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/true")),
                notify: { _ in }, hold: TerminationHold(control: control)
            )
            let gate = Gate()
            let started = store.launchBuild(appName: "example", delivery: .unknown) {
                while !gate.open, !Task.isCancelled { try? await Task.sleep(nanoseconds: 10_000_000) }
                gate.finished = true
            }
            XCTAssertTrue(started)
            await eventually { control.isHeld(TerminationHold.building) }
            XCTAssertFalse(control.isBalanced, "held while it runs")
            if stop { store.cancelBuild(appName: "example") } else { gate.open = true }
            await eventually { gate.finished }
            await eventually({ control.isLetGo(TerminationHold.building) && control.isBalanced },
                             "stopped: \(stop)")
        }
    }

    // MARK: App Store: get, drop, Get (free)

    private static let provided = #"{"type":"ipa-provided","app":"example","bundle_id":"com.example.app","version":"2.0","filename":"com.example.app_2.0.ipa","path":"/c/com.example.app_2.0.ipa","size_bytes":1}"#
    private static let version = #"{"type":"appstore-version","app":"example","bundle_id":"com.example.app","store_version":"2.0","cached_version":"2.0","update_available":false,"error":null}"#

    func test_a_get_and_a_drop_hold_until_they_end_however_they_end() async throws {
        // A get that stages, one that fails, one that is stopped; and a drop.
        for ending in ["stages", "fails", "stopped", "drop"] {
            try? FileManager.default.removeItem(at: dir.appending(path: "go"))
            let control = FakeControl()
            let runner = try cli("""
                case "$*" in
                  *--appstore-check*) printf '%s\\n' '\(Self.version)' ;;
                  *--appstore-get*|*--provide-ipa*)
                    \(wait)
                    if [ '\(ending)' = fails ]; then echo "error: the download failed" >&2; exit 20; fi
                    printf '%s\\n' '\(Self.provided)' ;;
                esac
                """)
            let store = AppStoreStore(runner: runner, shell: NoAdminShell(), hold: TerminationHold(control: control))
            let reason: String
            if ending == "drop" {
                let config = ConfigStore(runner: runner, notify: { _ in })
                XCTAssertTrue(store.startProvide(app: "example", fileURL: URL(fileURLWithPath: "/u/Example.ipa"), config: config))
                reason = TerminationHold.staging
            } else {
                XCTAssertTrue(store.startGet(app: "example"))
                reason = TerminationHold.gettingFromAppStore
            }
            await eventually({ control.isHeld(reason) }, ending)
            if ending == "stopped" { store.stopGet(app: "example") } else { try go() }
            await eventually({ !store.isBusy("example") }, ending)
            await eventually({ control.isLetGo(reason) && control.isBalanced }, ending)
            XCTAssertEqual(store.errors["example"] != nil, ending == "fails", ending)
        }
    }

    func test_get_free_holds_until_the_claim_ends() async throws {
        for stop in [false, true] {
            try? FileManager.default.removeItem(at: dir.appending(path: "go"))
            try? FileManager.default.removeItem(at: dir.appending(path: "claimed"))
            let control = FakeControl()
            let runner = try cli("""
                case "$*" in
                  *--appstore-status*)
                    printf '%s\\n' '{"type":"appstore-status","ipatool":{"path":"/i","present":true,"version":"2.8.0","signed_in":true,"warning":null,"email":"a@example.invalid","legacy_signin":false,"source":"bundled"},"unfairplay":{"path":"/u","present":true,"version":"v0.4.0","enrolled":true,"model_dir":"/m"},"setup_commands":[]}' ;;
                  *--appstore-check*) printf '%s\\n' '\(Self.version)' ;;
                  *--appstore-claim*)
                    \(wait)
                    touch '\(dir.path)/claimed'
                    printf '%s\\n' '{"type":"appstore-claimed","app":"example","bundle_id":"com.example.app","already_owned":false}' ;;
                  *--appstore-get*)
                    if [ -e '\(dir.path)/claimed' ]; then printf '%s\\n' '\(Self.provided)'; exit 0; fi
                    printf '%s\\n' '{"type":"appstore-refused","app":"example","reason":"needs-license","price":0,"formatted_price":"Free","track_id":100000001}'
                    exit 20 ;;
                esac
                """)
            let store = AppStoreStore(runner: runner, shell: NoAdminShell(), hold: TerminationHold(control: control))
            store.startGet(app: "example")
            await eventually { !store.isBusy("example") }
            XCTAssertEqual(store.licenseNeeded["example"]?.offersClaim, true, "premise")
            await store.checkBeforeClaim(app: "example")
            XCTAssertTrue(store.startClaim(app: "example", confirmed: try XCTUnwrap(store.claimConfirmations["example"])))
            await eventually { control.isHeld(TerminationHold.claiming) }
            if stop { store.stopGet(app: "example") } else { try go() }
            await eventually({ !store.isBusy("example") }, "stopped: \(stop)")
            await eventually({ control.isLetGo(TerminationHold.claiming) && control.isBalanced }, "stopped: \(stop)")
            XCTAssertEqual(store.completions["example"], stop ? nil : 1, "claimed, then got: stopped \(stop)")
        }
    }

    // MARK: --init

    private static let freeResult = #"{"type":"init-result","account":"free","config":"/tmp/example/config.yaml","change":"created","dry_run":false,"summary":"Set up for a free Apple ID.","team_id":null,"profile_name":null,"certificate":null,"device_count":null,"devices":[],"expires":null,"pinned":[],"sideloads":[],"steps":[]}"#

    func test_an_init_setup_holds_until_it_ends_whether_it_works_or_not() async throws {
        for works in [true, false] {
            try? FileManager.default.removeItem(at: dir.appending(path: "go"))
            let control = FakeControl()
            let runner = try cli("""
                \(wait)
                if \(works ? "true" : "false"); then printf '%s\\n' '\(Self.freeResult)'; exit 0; fi
                echo "error: config.yaml is not writable" >&2
                exit 2
                """)
            let store = AccountSetupStore(runner: runner, hold: TerminationHold(control: control))
            let run = Task { await store.chooseFree() }
            await eventually({ control.isHeld(TerminationHold.settingUp) }, "works: \(works)")
            try go()
            await run.value
            XCTAssertTrue(control.isLetGo(TerminationHold.settingUp), "works: \(works)")
            XCTAssertTrue(control.isBalanced, "works: \(works)")
            XCTAssertEqual(store.error == nil, works)
        }
    }

    // MARK: Enrollment

    /// Notes whether the hold was on when the root run began, and ends that
    /// run however the test says.
    final class SeeingShell: PrivilegedShell, @unchecked Sendable {
        let control: FakeControl
        var error: AdminShellError?
        private(set) var heldWhenRun: Bool?

        init(control: FakeControl) { self.control = control }

        func run(_ script: String, prompt: String) async throws -> AdminRun {
            heldWhenRun = control.isHeld(TerminationHold.enrolling)
            if let error { throw error }
            return AdminRun(output: "{\"success\":true}\n/private/var/tmp/graft-enroll.AbCd1234\n", via: .plain)
        }
    }

    func test_an_enrolment_holds_through_the_root_run_and_the_finish_however_it_ends() async throws {
        let command = #"{"type":"appstore-enroll-command","app":"/Applications/Example.app","model_dir":"/u/m","script":"echo ok\n","verified":true}"#
        try (command + "\n").write(to: dir.appending(path: "command.json"), atomically: true, encoding: .utf8)
        let runner = try cli("""
            case "$*" in
              *--appstore-enroll-command*) /bin/cat '\(dir.path)/command.json' ;;
              *--appstore-enroll-finish*) printf '%s\\n' '{"type":"appstore-enrolled","model_dir":"/u/m","count":1}' ;;
            esac
            """)
        for cancelled in [false, true] {
            let control = FakeControl()
            let shell = SeeingShell(control: control)
            if cancelled { shell.error = AdminShellError(number: AdminShellError.userCancelled, message: "User canceled.") }
            let store = AppStoreAccountStore(runner: runner, shell: shell, hold: TerminationHold(control: control))
            await store.prepareEnrol(appPath: "/Applications/Example.app")
            guard case .ready = store.enrol else { return XCTFail("expected .ready, got \(store.enrol)") }
            await store.confirmEnrol()
            XCTAssertEqual(store.enrol, cancelled ? .cancelled : .done)
            XCTAssertEqual(shell.heldWhenRun, true, "held while root runs: canceled \(cancelled)")
            XCTAssertTrue(control.isLetGo(TerminationHold.enrolling), "cancelled \(cancelled)")
            XCTAssertTrue(control.isBalanced, "cancelled \(cancelled)")
        }
    }

    // MARK: LiveContainer

    func test_a_livecontainer_install_holds_until_it_ends_whether_it_works_fails_or_is_stopped() async throws {
        for ending in ["works", "fails", "stopped"] {
            try? FileManager.default.removeItem(at: dir.appending(path: "go"))
            let control = FakeControl()
            let runner = try cli("""
                case "$*" in
                  *--lc-install*)
                    \(wait)
                    if [ '\(ending)' = fails ]; then echo "error: the device went away" >&2; exit 8; fi ;;
                esac
                """)
            let store = LiveContainerStore(runner: runner, hold: TerminationHold(control: control))
            store.startInstall(udid: "U", instances: [1], channel: nil)
            await eventually({ control.isHeld(TerminationHold.liveContainer) }, ending)
            if ending == "stopped" { store.stop() } else { try go() }
            await store.currentAction?.value
            await eventually({ control.isLetGo(TerminationHold.liveContainer) && control.isBalanced }, ending)
            XCTAssertEqual(store.run(for: "U")?.error != nil, ending != "works", ending)
        }
    }

    // MARK: Re-install (the try14 delta audit, D14-APP-2)

    private static let reinstallEnd = #"{"type":"build-end","app":"example","ok":true,"stopped":false,"error":null}"#

    /// `--reinstall-build` pushes to the phone and records a delivery, as a
    /// build does. It holds until it ends: it works, it fails, it is stopped
    /// (the sheet's `.task` canceled), or the CLI can't even start, which
    /// throws.
    func test_a_reinstall_holds_until_it_ends_however_it_ends() async throws {
        for ending in ["works", "fails", "stopped", "unstartable"] {
            try? FileManager.default.removeItem(at: dir.appending(path: "go"))
            try? FileManager.default.removeItem(at: dir.appending(path: "started"))
            let control = FakeControl()
            let runner: CLIRunner
            if ending == "unstartable" {
                runner = CLIRunner(executable: dir.appending(path: "no-such-graft"))
            } else {
                runner = try cli("""
                    case "$*" in
                      *"--reinstall-build 7"*)
                        touch '\(dir.path)/started'
                        \(wait)
                        if [ '\(ending)' = fails ]; then echo "error: no iOS devices connected" >&2; exit 8; fi
                        printf '%s\\n' '\(Self.reinstallEnd)' ;;
                    esac
                    """)
            }
            let hold = TerminationHold(control: control)
            let run = Task { @MainActor in
                try await ReinstallSheet.reinstall(build: 7, runner: runner, hold: hold) { _ in }
            }
            if ending != "unstartable" {
                await eventually({ control.isHeld(TerminationHold.reinstalling) }, ending)
                // Stopped once the CLI runs: a stop before it starts throws,
                // which "unstartable" covers.
                let started = dir.appending(path: "started").path
                await eventually({ FileManager.default.fileExists(atPath: started) }, ending)
                if ending == "stopped" { run.cancel() } else { try go() }
            }
            let result = try? await run.value
            XCTAssertTrue(control.isLetGo(TerminationHold.reinstalling), ending)
            XCTAssertTrue(control.isBalanced, ending)
            XCTAssertEqual(result?.exitCode == 0, ending == "works", ending)
            XCTAssertEqual(result == nil, ending == "unstartable", "only that one throws: \(ending)")
        }
    }

    /// The sheet's own run goes through that hold: hosted, with the
    /// stand-in waiting, Graft is held until the re-install ends.
    func test_the_reinstall_sheet_holds_while_its_run_goes() async throws {
        let runner = try cli("""
            case "$*" in
              *"--reinstall-build 7"*)
                \(wait)
                printf '%s\\n' '\(Self.reinstallEnd)' ;;
            esac
            """)
        let line = #"{"type":"build-record","id":7,"timestamp":"2026-11-17T10:00:00Z","app":"example","bundle_id":"com.example.app","version":"1.0","tweaks":[],"ipa_path":"/b/x.ipa","ipa_sha256":null,"log_path":null,"signing_team":null,"udid":null,"target":"device","success":true,"error":null}"#
        guard case let .buildRecord(build)? = line.buildEvents().first else {
            return XCTFail("build-record fixture did not decode")
        }
        let control = FakeControl()
        let sh = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let sheet = ReinstallSheet(
            build: build, runner: runner,
            config: ConfigStore(runner: sh, cliArgs: ["-c", "true"], notify: { _ in }),
            artwork: ArtworkStore(runner: sh), hold: TerminationHold(control: control), onClose: {}
        )
        let box = hosted(sheet, size: NSSize(width: 640, height: 460))
        await eventually { control.isHeld(TerminationHold.reinstalling) }
        try go()
        await eventually { control.isLetGo(TerminationHold.reinstalling) && control.isBalanced }
        box.window.orderOut(nil)
    }

    // MARK: Decrypt (the try14 delta audit, D14-APP-2)

    private static let decryptEnd = #"{"type":"decrypt-end","bundle_id":"com.example.app","version":"2.0","path":"/c/com.example.app_2.0.ipa","binaries":1}"#

    /// A dump stages an IPA and takes minutes, and closing the window doesn't
    /// stop it. It holds until it ends, a row's dump or the frontmost app's:
    /// it stages, it fails, or it is stopped.
    func test_a_decrypt_dump_holds_until_it_ends_however_it_ends() async throws {
        let app = DeviceAppRecord(bundleId: "com.example.app", name: "Example", version: "2.0", encrypted: true)
        for ending in ["stages", "frontmost", "fails", "stopped"] {
            try? FileManager.default.removeItem(at: dir.appending(path: "go"))
            let control = FakeControl()
            let runner = try cli("""
                case "$*" in
                  *--decrypt*)
                    \(wait)
                    if [ '\(ending)' = fails ]; then echo "error: the app quit during the dump" >&2; exit 1; fi
                    printf '%s\\n' '\(Self.decryptEnd)' ;;
                esac
                """)
            let store = DeviceAppsStore(runner: runner, hold: TerminationHold(control: control))
            let run = Task { @MainActor () -> DecryptEndRecord? in
                if ending == "frontmost" { return await store.decryptFrontmost(device: "U") }
                return await store.decrypt(app, device: "U")
            }
            await eventually({ control.isHeld(TerminationHold.decrypting) }, ending)
            if ending == "stopped" { run.cancel() } else { try go() }
            let record = await run.value
            XCTAssertTrue(control.isLetGo(TerminationHold.decrypting), ending)
            XCTAssertTrue(control.isBalanced, ending)
            XCTAssertEqual(record != nil, ending == "stages" || ending == "frontmost", ending)
            XCTAssertFalse(store.isDumping, ending)
        }
    }
}
