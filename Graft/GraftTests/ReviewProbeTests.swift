import XCTest
@testable import Graft

/// The v1.1 review's probes: each asserts the correct behaviour, and each
/// failed against the code the review read. They stay as regression tests
/// for the six defects they demonstrated.
@MainActor
final class ReviewProbeTests: XCTestCase {
    /// The `graft` the suite runs against: the first on `PATH`, which
    /// `TEST_RUNNER_PATH` points at a checkout's venv. Nil skips.
    private static let installedGraft: URL? = {
        let fm = FileManager.default
        let dirs = (ProcessInfo.processInfo.environment["PATH"] ?? "")
            .split(separator: ":").map(String.init)
        for dir in dirs where dir.hasPrefix("/") {
            let candidate = URL(fileURLWithPath: dir).appendingPathComponent("graft")
            var isDirectory: ObjCBool = false
            if fm.fileExists(atPath: candidate.path, isDirectory: &isDirectory),
               !isDirectory.boolValue, fm.isExecutableFile(atPath: candidate.path) {
                return candidate
            }
        }
        return nil
    }()

    private var scratch: URL!

    override func setUpWithError() throws {
        scratch = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-probe-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: scratch)
    }

    // MARK: 1. A login shell that exports FORCE_COLOR

    /// An isolated home for the real CLI: nothing here can read or write the
    /// developer's own config, cache, state or keychain.
    private func isolatedShell(_ extra: [String: String]) -> [String: String] {
        let home = scratch.path
        return [
            "HOME": home,
            "XDG_CONFIG_HOME": "\(home)/config",
            "XDG_CACHE_HOME": "\(home)/cache",
            "XDG_STATE_HOME": "\(home)/state",
            "XDG_DATA_HOME": "\(home)/data",
            "GRAFT_NO_KEYCHAIN": "1",
            "PATH": "/usr/bin:/bin",
        ].merging(extra) { _, new in new }
    }

    /// `FORCE_COLOR=1` is a common export (npm/jest/CI habits), and Rich
    /// honours `TTY_COMPATIBLE` and `TTY_INTERACTIVE` the same way. None of
    /// them may reach the CLI, from the login shell or from the app's own
    /// environment: coloured stderr is stderr `CLIFailure` cannot read.
    func test_probe_the_colour_variables_never_reach_the_cli() {
        let colour = [
            "FORCE_COLOR": "1", "TTY_COMPATIBLE": "1", "TTY_INTERACTIVE": "1",
            "PY_COLORS": "1", "CLICOLOR_FORCE": "1",
        ]
        let fromShell = ProcessEnvironment.forCLI(
            shell: colour, process: ["PATH": "/usr/bin:/bin"], home: scratch.path
        )
        let fromProcess = ProcessEnvironment.forCLI(
            shell: nil, process: colour.merging(["PATH": "/usr/bin:/bin"]) { a, _ in a },
            home: scratch.path
        )
        for name in colour.keys {
            XCTAssertNil(fromShell[name], "\(name) from the login shell")
            XCTAssertNil(fromProcess[name], "\(name) from the app's environment")
        }
    }

    /// The whole path: a login shell exporting FORCE_COLOR, the real CLI, and
    /// a missing config must still be the setup screen.
    func test_probe_force_color_from_the_login_shell_keeps_the_setup_screen() async throws {
        guard let graft = Self.installedGraft else { throw XCTSkip("no graft on PATH") }
        let env = ProcessEnvironment.forCLI(
            shell: isolatedShell(["FORCE_COLOR": "1"]),
            process: ["PATH": "/usr/bin:/bin"], home: scratch.path
        )
        let store = ConfigStore(runner: CLIRunner(executable: graft, environment: env))
        _ = await store.checkSetup()
        XCTAssertEqual(
            store.setupNeed?.step, 2,
            "a missing config should be the setup screen; got lastError=\(String(reflecting: store.lastError))"
        )
        XCTAssertFalse(store.lastError?.contains("\u{1b}") ?? false, "ANSI escapes reached the alert text")
    }

    /// The backstop: a CLI that colours its stderr anyway — forced past the
    /// environment filter here — still reads as the setup step, and no escape
    /// byte reaches anything the user is shown.
    func test_probe_coloured_stderr_still_parses() async throws {
        guard let graft = Self.installedGraft else { throw XCTSkip("no graft on PATH") }
        var env = ProcessEnvironment.forCLI(
            shell: isolatedShell([:]), process: ["PATH": "/usr/bin:/bin"], home: scratch.path
        )
        env["FORCE_COLOR"] = "1"
        let store = ConfigStore(runner: CLIRunner(executable: graft, environment: env))
        _ = await store.checkSetup()
        XCTAssertEqual(
            store.setupNeed?.step, 2,
            "coloured stderr lost the setup screen; got lastError=\(String(reflecting: store.lastError))"
        )
        XCTAssertFalse(store.lastError?.contains("\u{1b}") ?? false, "ANSI escapes reached the alert text")
    }

    // MARK: 2. A directory called graft on the PATH

    func test_probe_a_directory_named_graft_does_not_shadow_the_real_cli() throws {
        let fm = FileManager.default
        let first = scratch.appendingPathComponent("first")
        let second = scratch.appendingPathComponent("second")
        try fm.createDirectory(at: first.appendingPathComponent("graft"), withIntermediateDirectories: true)
        try fm.createDirectory(at: second, withIntermediateDirectories: true)
        let real = second.appendingPathComponent("graft")
        try "#!/bin/sh\necho real\n".write(to: real, atomically: true, encoding: .utf8)
        try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: real.path)

        let root = scratch.path
        // `isExecutableFile` is true for a directory, which is the predicate
        // this probe was written against. Limited to this scratch dir so a
        // graft in /opt/homebrew/bin on this machine cannot answer.
        let found = GraftExecutable.locate(
            home: scratch.appendingPathComponent("home").path,
            path: "\(first.path):\(second.path)",
            isExecutable: { $0.hasPrefix(root) && fm.isExecutableFile(atPath: $0) }
        )
        XCTAssertEqual(found?.path, real.path, "picked a directory: \(found?.path ?? "nil")")
    }

    // MARK: 3. An installed graft whose interpreter is gone

    /// What a pipx venv looks like after Homebrew removes the Python it was
    /// built on. The file is there; `pipx install` answers "already installed".
    func test_probe_an_installed_but_broken_graft_is_not_the_install_step() async throws {
        let graft = scratch.appendingPathComponent("graft")
        try "#!/opt/homebrew/opt/python@3.11/bin/python3.11-gone\nprint(1)\n"
            .write(to: graft, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: graft.path)

        let store = ConfigStore(runner: CLIRunner(executable: graft))
        _ = await store.checkSetup()
        XCTAssertNotEqual(
            store.setupNeed, .cli,
            "graft exists; the screen tells the user to pipx install it, which is a no-op"
        )
    }

    // MARK: 4. The decrypt card on a jailbroken phone

    private static func readiness(
        sshDetail: String, sshFix: String, reason: String? = nil
    ) throws -> DecryptCheckRecord {
        let reasonField = reason.map { #","reason":"\#($0)""# } ?? ""
        let json = """
        {"type":"decrypt-check","device":"00008030-0000000000000003","device_name":"Example Phone",\
        "ready":false,"can_dump_from_list":false,"recommended_backend":null,"frontmost_bundle_id":null,\
        "frontmost_name":null,"checks":[\
        {"key":"frida","label":"frida reachable","ok":true,"detail":"reachable","fix":null},\
        {"key":"ssh","label":"SSH login","ok":false,"detail":"\(sshDetail)","fix":"\(sshFix)"\(reasonField)}],\
        "backends":[{"name":"bagbak","usable":false,"reason":"needs SSH"}]}
        """
        guard case let .decryptCheck(r)? = json.buildEvents().first else {
            throw XCTSkip("fixture did not decode")
        }
        return r
    }

    /// The phone IS jailbroken and OpenSSH IS answering; only the stored root
    /// password is wrong. The fix ("Set root password…") is the button the
    /// collapse hides, under a sentence saying the phone needs a jailbreak.
    func test_probe_a_rejected_root_login_keeps_the_checks() throws {
        let r = try Self.readiness(
            sshDetail: "SSH-2.0-OpenSSH_9.6, but the login was rejected",
            sshFix: "set the phone's root password with `graft --set-ssh-password`"
        )
        XCTAssertFalse(DeviceDecryptSection.isUnavailable(r), "collapsed into 'needs a jailbroken device'")
    }

    /// A Mac-side problem: libimobiledevice is not installed.
    func test_probe_a_missing_iproxy_keeps_the_checks() throws {
        let r = try Self.readiness(sshDetail: "iproxy is missing", sshFix: "brew install libimobiledevice")
        XCTAssertFalse(DeviceDecryptSection.isUnavailable(r), "collapsed into 'needs a jailbroken device'")
    }

    // MARK: 5. The login-shell timeout and the shell's children

    /// A startup-file command that hangs and ignores TERM/HUP. After the
    /// timeout, nothing the shell started should be left running (or holding
    /// the pipe the reader is parked on).
    func test_probe_the_timeout_kills_the_shells_process_group() async throws {
        let tag = "graftprobe-\(UUID().uuidString.prefix(8))"
        let shell = scratch.appendingPathComponent("fake-shell")
        let script = """
        #!/bin/sh
        /usr/bin/perl -e '$SIG{TERM}=$SIG{HUP}=$SIG{INT}="IGNORE"; sleep 20' \(tag)
        exec /bin/sh -c "$4"
        """
        try script.write(to: shell, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: shell.path)
        defer { Self.sh("/usr/bin/pkill -9 -f \(tag)") }

        let env = await LoginShell.resolve(shell: shell.path, timeout: 0.5)
        XCTAssertNil(env)
        try await Task.sleep(nanoseconds: 2_000_000_000)
        let survivors = Self.sh("/usr/bin/pgrep -f \(tag)")
        XCTAssertEqual(survivors, "", "the shell's child outlived the timeout: pids \(survivors)")
    }

    @discardableResult
    static func sh(_ command: String) -> String {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/sh")
        p.arguments = ["-c", command]
        let out = Pipe()
        p.standardOutput = out
        try? p.run()
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
