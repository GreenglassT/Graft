import XCTest
@testable import Graft

/// Reading the user's login-shell environment, and handing it to the CLI.
///
/// No test here reads the developer's own startup files. The parsing tests
/// inject a runner that returns canned bytes; most process tests run a *fake*
/// shell — a `/bin/sh` script in a scratch directory that checks it was called
/// as `-l -i -c <command>` and then runs the command — so the real marker
/// command goes through a real child process and a real pipe. The real-shell
/// tests (zsh, bash, sh, and tcsh where present) start the shell with nothing
/// inherited but a scratch `HOME` and a system `PATH`, so the startup files
/// they read are the ones the test wrote.
final class LoginShellTests: XCTestCase {
    private var scratch: URL!

    override func setUpWithError() throws {
        scratch = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-login-shell-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: scratch)
    }

    // MARK: - Canned output through an injected runner

    /// A runner that answers with whatever `body` builds from the markers it
    /// was asked to print — so a test can wrap the real markers in anything.
    private func canned(
        _ body: @escaping @Sendable (_ start: String, _ end: String) -> Data?
    ) -> LoginShell.Runner {
        { _, arguments, markers, _, _ in
            XCTAssertTrue(arguments.last?.contains("/usr/bin/env -0") ?? false)
            return body(markers.start, markers.end)
        }
    }

    private static func env(_ pairs: [String]) -> Data {
        Data(pairs.joined(separator: "\0").utf8) + Data([0])
    }

    func test_the_environment_between_the_markers_is_read() async {
        let run = canned { start, end in
            Data(start.utf8) + Self.env(["PATH=/a:/b", "GRAFT_SSH_USER=root", "MULTI=one\ntwo"])
                + Data(end.utf8)
        }
        let env = await LoginShell.resolve(shell: "/bin/zsh", run: run)
        XCTAssertEqual(env?["PATH"], "/a:/b")
        XCTAssertEqual(env?["GRAFT_SSH_USER"], "root")
        XCTAssertEqual(env?["MULTI"], "one\ntwo", "NUL-separated, so a newline in a value survives")
    }

    /// A shell that greets, or a prompt theme that warns, writes before the
    /// command runs. Everything outside the markers is not the environment.
    func test_a_banner_before_the_marker_is_ignored() async {
        let run = canned { start, end in
            Data("Last login: today\nWelcome!\nNOT_ENV=1\0".utf8)
                + Data(start.utf8) + Self.env(["XDG_CONFIG_HOME=/x/config"]) + Data(end.utf8)
                + Data("logout\0STRAY=2\0".utf8)
        }
        let env = await LoginShell.resolve(shell: "/bin/zsh", run: run)
        XCTAssertEqual(env, ["XDG_CONFIG_HOME": "/x/config"])
    }

    func test_garbage_without_the_markers_is_no_answer() async {
        let run = canned { _, _ in Data([0xFF, 0xFE, 0x00, 0x41, 0x3D, 0x42, 0x00, 0x0A]) }
        let env = await LoginShell.resolve(shell: "/bin/zsh", run: run)
        XCTAssertNil(env)
    }

    func test_markers_in_the_wrong_order_are_no_answer() async {
        let run = canned { start, end in
            Data(end.utf8) + Self.env(["A=1"]) + Data(start.utf8)
        }
        let env = await LoginShell.resolve(shell: "/bin/zsh", run: run)
        XCTAssertNil(env)
    }

    /// The runner reports a timeout as nil; resolution passes that on as "no
    /// answer", which the app treats as launchd's environment.
    func test_a_runner_that_timed_out_is_no_answer() async {
        let env = await LoginShell.resolve(shell: "/bin/zsh", run: { _, _, _, _, _ in nil })
        XCTAssertNil(env)
    }

    private final class Seen: @unchecked Sendable { var shell = ""; var args: [String] = [] }

    private func arguments(for shell: String) async -> Seen {
        let seen = Seen()
        _ = await LoginShell.resolve(shell: shell, run: { shell, args, _, _, _ in
            seen.shell = shell
            seen.args = args
            return nil
        })
        return seen
    }

    func test_the_shell_is_started_as_a_login_and_interactive_shell() async {
        for shell in ["/opt/example/bin/fish", "/bin/zsh", "/bin/bash", "/bin/sh"] {
            let seen = await arguments(for: shell)
            XCTAssertEqual(seen.shell, shell)
            XCTAssertEqual(Array(seen.args.prefix(3)), ["-l", "-i", "-c"], shell)
            XCTAssertEqual(seen.args.count, 4, shell)
        }
    }

    /// csh and tcsh refuse `-l` next to any other flag ("Unknown option:
    /// `-l'"), so `-l -i -c` read nothing at all from a csh user's setup.
    /// They are started interactive, which reads `~/.tcshrc` / `~/.cshrc`.
    func test_csh_and_tcsh_are_started_without_the_login_flag() async {
        for shell in ["/bin/tcsh", "/bin/csh", "/opt/example/bin/tcsh"] {
            let seen = await arguments(for: shell)
            XCTAssertEqual(Array(seen.args.prefix(2)), ["-i", "-c"], shell)
            XCTAssertEqual(seen.args.count, 3, shell)
            XCTAssertFalse(seen.args.contains("-l"), shell)
        }
        XCTAssertFalse(LoginShell.isCshFamily("/opt/example/bin/tcshx"))
        XCTAssertFalse(LoginShell.isCshFamily("/opt/csh/bin/zsh"))
    }

    /// The command line must not contain a marker whole: a startup file that
    /// echoes commands (`set -x`) would otherwise print one early.
    func test_the_command_line_never_contains_a_marker_whole() {
        let markers = LoginShell.Markers.fresh()
        XCTAssertFalse(markers.command.contains(markers.start))
        XCTAssertFalse(markers.command.contains(markers.end))
        XCTAssertNotEqual(LoginShell.Markers.fresh().start, markers.start, "fresh per run")
    }

    // MARK: - Which shell

    func test_the_shell_comes_from_the_user_database_entry() {
        XCTAssertEqual(
            LoginShell.userShell(recorded: "/opt/example/bin/fish", isExecutable: { _ in true }),
            "/opt/example/bin/fish"
        )
        XCTAssertEqual(LoginShell.userShell(recorded: nil, isExecutable: { _ in true }), "/bin/zsh")
        XCTAssertEqual(LoginShell.userShell(recorded: "fish", isExecutable: { _ in true }), "/bin/zsh")
        XCTAssertEqual(
            LoginShell.userShell(recorded: "/gone/bash", isExecutable: { _ in false }), "/bin/zsh"
        )
    }

    // MARK: - A real child process

    /// A stand-in for a login shell: refuses anything but `-l -i -c <cmd>`,
    /// runs `prelude`, then the command.
    ///
    /// It is run once with no arguments before it is returned. It refuses
    /// that (exit 64) before the prelude, and macOS's check of a new
    /// executable is then paid here, not inside a case's timeout
    /// (`warmUpFreshExecutable`). Without the warm-up, a 0.5 s timeout killed
    /// the shell before it ran a line. The cases that check what a timeout
    /// kills could then pass with nothing started to kill.
    private func fakeShell(prelude: String) throws -> String {
        let url = scratch.appendingPathComponent("fake-shell-\(UUID().uuidString)")
        let script = """
        #!/bin/sh
        [ "$1" = -l ] && [ "$2" = -i ] && [ "$3" = -c ] || exit 64
        \(prelude)
        exec /bin/sh -c "$4"
        """
        try script.write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: url.path)
        try warmUpFreshExecutable(url.path, exitingWith: 64)
        return url.path
    }

    func test_a_real_shells_environment_comes_back_through_the_pipe() async throws {
        let shell = try fakeShell(prelude: """
        echo 'Welcome to the example shell'
        export GRAFT_EXAMPLE_FROM_RC=yes
        export UNFAIRPLAY_MODEL_DIR=/x/models
        """)
        let env = await LoginShell.resolve(shell: shell, timeout: 10, run: LoginShell.runProcess)
        XCTAssertEqual(env?["GRAFT_EXAMPLE_FROM_RC"], "yes")
        XCTAssertEqual(env?["UNFAIRPLAY_MODEL_DIR"], "/x/models")
    }

    func test_a_shell_that_hangs_is_abandoned_at_the_timeout() async throws {
        let shell = try fakeShell(prelude: "sleep 30")
        let started = Date()
        let env = await LoginShell.resolve(shell: shell, timeout: 0.5, run: LoginShell.runProcess)
        XCTAssertNil(env)
        XCTAssertLessThan(Date().timeIntervalSince(started), 5, "bounded by the timeout, not the sleep")
    }

    /// A startup file that starts a background agent hands it the shell's
    /// stdout, so the pipe never reaches EOF. The end marker is the signal
    /// to stop reading, not EOF — otherwise this costs the whole timeout and
    /// loses the environment.
    func test_a_background_child_holding_stdout_does_not_cost_the_timeout() async throws {
        let shell = try fakeShell(prelude: """
        sleep 30 &
        export GRAFT_EXAMPLE_AGENT=started
        """)
        let started = Date()
        let env = await LoginShell.resolve(shell: shell, timeout: 8, run: LoginShell.runProcess)
        XCTAssertEqual(env?["GRAFT_EXAMPLE_AGENT"], "started")
        XCTAssertLessThan(Date().timeIntervalSince(started), 4)
    }

    func test_a_shell_that_cannot_be_started_is_no_answer() async {
        let env = await LoginShell.resolve(shell: scratch.appendingPathComponent("absent").path, run: LoginShell.runProcess)
        XCTAssertNil(env)
    }

    /// A startup file that writes far more than any environment before the
    /// command runs: the answer still comes back, and (see the buffer test
    /// below) none of the noise is held on to.
    func test_megabytes_of_noise_before_the_marker_do_not_cost_the_answer() async throws {
        let shell = try fakeShell(prelude: """
        /usr/bin/head -c 33554432 /dev/zero
        export GRAFT_EXAMPLE_AFTER_NOISE=yes
        """)
        let env = await LoginShell.resolve(shell: shell, timeout: 10, run: LoginShell.runProcess)
        XCTAssertEqual(env?["GRAFT_EXAMPLE_AFTER_NOISE"], "yes")
    }

    // MARK: - What the reader holds on to

    func test_nothing_before_the_start_marker_is_buffered() {
        let markers = LoginShell.Markers.fresh()
        let output = MarkedOutput(markers: markers, cap: 1024)
        for _ in 0..<64 {
            XCTAssertFalse(output.append(Data(repeating: 0x41, count: 64 * 1024)))
            XCTAssertLessThan(output.bufferedCount, markers.start.utf8.count, "pre-marker bytes kept")
        }
        // A marker split across two reads is still found.
        let start = Data(markers.start.utf8)
        XCTAssertFalse(output.append(Data("noise".utf8) + start.prefix(10)))
        XCTAssertFalse(output.append(start.dropFirst(10) + Data("A=1\0".utf8)))
        XCTAssertTrue(output.append(Data(markers.end.utf8)))
        XCTAssertEqual(LoginShell.parse(output.data, markers: markers), ["A": "1"])
    }

    /// Past the start marker the environment is bounded by `ARG_MAX`, so the
    /// cap is a ceiling no real answer reaches; one that does is no answer.
    func test_an_answer_past_the_cap_is_abandoned() {
        let markers = LoginShell.Markers.fresh()
        let output = MarkedOutput(markers: markers, cap: 1024)
        XCTAssertFalse(output.append(Data(markers.start.utf8)))
        XCTAssertTrue(output.append(Data(repeating: 0x41, count: 2048)), "the reader stops")
        XCTAssertEqual(output.bufferedCount, 0)
        XCTAssertNil(LoginShell.parse(output.data, markers: markers))
        XCTAssertLessThan(LoginShell.outputCap, 64 * 1024 * 1024)
        XCTAssertGreaterThanOrEqual(LoginShell.outputCap, 1024 * 1024, "at least ARG_MAX")
    }

    // MARK: - After a timeout

    /// The timeout reaches everything the shell started, in its process group:
    /// a real zsh whose `~/.zshrc` hangs on a command that ignores SIGTERM.
    func test_a_real_shells_hung_startup_command_does_not_outlive_the_timeout() async throws {
        let tag = "graftleft-\(UUID().uuidString.prefix(8))"
        let home = try fakeHome([
            ".zshrc": "/usr/bin/perl -e '$SIG{TERM}=$SIG{HUP}=$SIG{INT}=\"IGNORE\"; sleep 30' \(tag)\n",
        ])
        defer { Self.sh("/usr/bin/pkill -9 -f \(tag)") }
        let env = await LoginShell.resolve(
            shell: "/bin/zsh", timeout: 1, environment: Self.bareEnvironment(home: home), run: LoginShell.runProcess)
        XCTAssertNil(env)
        try await Task.sleep(nanoseconds: 1_500_000_000)
        let survivors = Self.sh("/usr/bin/pgrep -f \(tag)")
        XCTAssertEqual(survivors, "", "left running after the timeout: \(survivors)")
    }

    /// The two halves of the timeout's kill, each on its own: an orphan the
    /// shell's startup file double-forked is no longer its descendant but is
    /// still in its process group, and a child that took a group of its own
    /// (as every job does under job control) is still its descendant.
    func test_the_timeout_reaches_an_orphan_in_the_group_and_a_child_outside_it() async throws {
        let orphan = "graftorphan-\(UUID().uuidString.prefix(8))"
        let own = "graftowngroup-\(UUID().uuidString.prefix(8))"
        let shell = try fakeShell(prelude: """
        /usr/bin/perl -e 'exit if fork; $SIG{TERM}=$SIG{HUP}="IGNORE"; sleep 30' \(orphan)
        /usr/bin/perl -e 'setpgrp(0, 0); $SIG{TERM}=$SIG{HUP}="IGNORE"; sleep 30' \(own)
        """)
        defer {
            Self.sh("/usr/bin/pkill -9 -f \(orphan)")
            Self.sh("/usr/bin/pkill -9 -f \(own)")
        }
        let env = await LoginShell.resolve(shell: shell, timeout: 0.5, run: LoginShell.runProcess)
        XCTAssertNil(env)
        try await Task.sleep(nanoseconds: 1_500_000_000)
        XCTAssertEqual(Self.sh("/usr/bin/pgrep -f \(orphan)"), "", "the group kill missed the orphan")
        XCTAssertEqual(Self.sh("/usr/bin/pgrep -f \(own)"), "", "the descendant walk missed the child")
    }

    /// A writer that left the shell's process tree *and* its process group —
    /// double-forked, then `setpgrp` — survives the kill; nothing can reach
    /// it safely. The reader still lets go of the pipe: it closes its end, so
    /// that writer's next write fails instead of landing in a buffer nobody
    /// reads, and no thread is left parked on it.
    func test_the_read_end_is_closed_after_a_timeout() async throws {
        let tag = "graftpipe-\(UUID().uuidString.prefix(8))"
        let verdict = scratch.appendingPathComponent("verdict")
        let shell = try fakeShell(prelude: """
        /usr/bin/perl -e 'exit if fork; setpgrp(0, 0); $SIG{TERM}=$SIG{HUP}=$SIG{PIPE}="IGNORE"; \
        sleep 2; $| = 1; my $ok = print STDOUT ("x" x 10000); open(my $f, ">", $ARGV[0]); \
        print $f ($ok ? "open" : "closed"); close $f; sleep 20' '\(verdict.path)' \(tag)
        sleep 30
        """)
        defer { Self.sh("/usr/bin/pkill -9 -f \(tag)") }
        let env = await LoginShell.resolve(shell: shell, timeout: 0.5, run: LoginShell.runProcess)
        XCTAssertNil(env)
        let deadline = Date().addingTimeInterval(6)
        while !FileManager.default.fileExists(atPath: verdict.path), Date() < deadline {
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        XCTAssertEqual(try? String(contentsOf: verdict, encoding: .utf8), "closed")
    }

    /// `command` under `/bin/sh`, its stdout trimmed.
    @discardableResult
    private static func sh(_ command: String) -> String {
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

    // MARK: - Real shells

    /// A scratch home holding `files`, by name relative to it.
    private func fakeHome(_ files: [String: String]) throws -> URL {
        let home = scratch.appendingPathComponent("home-\(UUID().uuidString.prefix(8))")
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        for (name, body) in files {
            try body.write(to: home.appendingPathComponent(name), atomically: true, encoding: .utf8)
        }
        return home
    }

    /// What a real shell is started with in these tests: the scratch home and
    /// a system PATH, and nothing inherited — a `ZDOTDIR` or `ENV` in the
    /// developer's environment would otherwise point the shell back at their
    /// own startup files.
    private static func bareEnvironment(home: URL) -> [String: String] {
        ["HOME": home.path, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"]
    }

    /// Each shell this Mac ships, started the way the app starts the user's,
    /// against a home whose startup files export a marker: the marker comes
    /// back. zsh, bash and sh are part of macOS; tcsh is checked when present.
    ///
    /// `login` names the file only a login shell reads and `rc` the one only an
    /// interactive shell reads, so the matrix also shows which of the two
    /// flags each shell honoured.
    func test_every_shell_macos_ships_reports_its_startup_files() async throws {
        struct Case {
            let shell: String
            let files: [String: String]
            let expected: [String: String]
        }
        var cases = [
            Case(shell: "/bin/zsh", files: [
                ".zprofile": "export GRAFT_EXAMPLE_LOGIN=zsh\n",
                ".zshrc": "export GRAFT_EXAMPLE_RC=zsh\n",
            ], expected: ["GRAFT_EXAMPLE_LOGIN": "zsh", "GRAFT_EXAMPLE_RC": "zsh"]),
            Case(shell: "/bin/bash", files: [
                ".bash_profile": "export GRAFT_EXAMPLE_LOGIN=bash\n",
            ], expected: ["GRAFT_EXAMPLE_LOGIN": "bash"]),
            Case(shell: "/bin/sh", files: [
                ".profile": "GRAFT_EXAMPLE_LOGIN=sh; export GRAFT_EXAMPLE_LOGIN\n",
            ], expected: ["GRAFT_EXAMPLE_LOGIN": "sh"]),
        ]
        if FileManager.default.isExecutableFile(atPath: "/bin/tcsh") {
            cases.append(Case(shell: "/bin/tcsh", files: [
                ".tcshrc": "setenv GRAFT_EXAMPLE_RC tcsh\n",
            ], expected: ["GRAFT_EXAMPLE_RC": "tcsh"]))
        }
        for c in cases {
            XCTAssertTrue(FileManager.default.isExecutableFile(atPath: c.shell), "\(c.shell) is part of macOS")
            let home = try fakeHome(c.files)
            let env = await LoginShell.resolve(
                shell: c.shell, timeout: 10, environment: Self.bareEnvironment(home: home), run: LoginShell.runProcess)
            XCTAssertNotNil(env, "\(c.shell) gave no answer")
            for (name, value) in c.expected {
                XCTAssertEqual(env?[name], value, "\(c.shell): \(name)")
            }
            XCTAssertEqual(env?["HOME"], home.path, "\(c.shell) read the scratch home")
        }
    }

    // MARK: - Asking again

    /// Check again re-reads the shell: a later `value` is a new answer, while
    /// a caller already waiting keeps the one it asked for.
    func test_a_refreshed_shell_environment_is_a_new_answer() async {
        final class Count: @unchecked Sendable {
            private var n = 0
            private let lock = NSLock()
            func next() -> Int { lock.withLock { n += 1; return n } }
        }
        let count = Count()
        let shell = ShellEnvironment(minimumInterval: 0) { ["ANSWER": String(count.next())] }
        let first = await shell.value
        XCTAssertEqual(first?["ANSWER"], "1")
        let again = await shell.value
        XCTAssertEqual(again?["ANSWER"], "1", "read once until asked again")
        shell.refresh()
        let refreshed = await shell.value
        XCTAssertEqual(refreshed?["ANSWER"], "2")
    }

    /// R7: a refresh the shell does not answer — it timed out — keeps the
    /// environment the last good read found, rather than dropping every
    /// launch after it back to launchd's bare PATH.
    func test_a_refresh_with_no_answer_keeps_the_last_good_one() async {
        final class Answers: @unchecked Sendable {
            let lock = NSLock()
            var queue: [[String: String]?] = [["PATH": "/first"], nil, ["PATH": "/third"]]
            func next() -> [String: String]? { lock.withLock { queue.removeFirst() } }
        }
        let answers = Answers()
        let shell = ShellEnvironment(minimumInterval: 0) { answers.next() }
        let first = await shell.value
        XCTAssertEqual(first?["PATH"], "/first")

        shell.refresh()  // the shell times out: nil
        let kept = await shell.value
        XCTAssertEqual(kept?["PATH"], "/first", "a timed-out refresh threw away a good environment")

        shell.refresh()  // and a later answer still replaces it
        let replaced = await shell.value
        XCTAssertEqual(replaced?["PATH"], "/third")
    }

    /// Nil to begin with stays nil: there is nothing to keep.
    func test_a_shell_that_never_answered_still_reads_as_no_answer() async {
        let shell = ShellEnvironment(minimumInterval: 0) { nil }
        shell.refresh()
        let value = await shell.value
        XCTAssertNil(value)
    }

    /// Whole-diff review, app-1: each ask runs the user's startup files, so
    /// a refresh sooner than `minimumInterval` after the last ask (launch
    /// included) asks nothing and keeps that answer; one after it asks.
    func test_the_shell_is_asked_again_at_most_once_per_interval() async {
        final class Count: @unchecked Sendable {
            private var n = 0
            private let lock = NSLock()
            func next() -> Int { lock.withLock { n += 1; return n } }
            var asked: Int { lock.withLock { n } }
        }
        final class Clock: @unchecked Sendable {
            private var t = Date(timeIntervalSinceReferenceDate: 0)
            private let lock = NSLock()
            var now: Date { lock.withLock { t } }
            func advance(_ seconds: TimeInterval) { lock.withLock { t += seconds } }
        }
        let count = Count()
        let clock = Clock()
        let shell = ShellEnvironment(minimumInterval: 10, now: { clock.now }) {
            ["ANSWER": String(count.next())]
        }
        let launch = await shell.value
        XCTAssertEqual(launch?["ANSWER"], "1")

        clock.advance(9.9)
        XCTAssertFalse(shell.refresh(), "within the interval of launch")
        let kept = await shell.value
        XCTAssertEqual(kept?["ANSWER"], "1")
        XCTAssertEqual(count.asked, 1, "the shell wasn't run")

        clock.advance(0.1)
        XCTAssertTrue(shell.refresh())
        let asked = await shell.value
        XCTAssertEqual(asked?["ANSWER"], "2")

        XCTAssertFalse(shell.refresh(), "within the interval of that ask")
        clock.advance(5)
        XCTAssertFalse(shell.refresh())
        let still = await shell.value
        XCTAssertEqual(still?["ANSWER"], "2")
        XCTAssertEqual(count.asked, 2)
        XCTAssertEqual(ShellEnvironment.minimumRefreshInterval, 10)
    }

    /// The whole Check again path: graft is only on a PATH the user added to
    /// their startup files after the app opened. The first check is the
    /// install step; Check again re-reads the shell and finds it.
    func test_check_again_rereads_the_login_shell() async throws {
        final class Answers: @unchecked Sendable {
            var path = "/usr/bin:/bin"
            let lock = NSLock()
        }
        let graft = scratch.appendingPathComponent("added-later/graft")
        try FileManager.default.createDirectory(
            at: graft.deletingLastPathComponent(), withIntermediateDirectories: true
        )
        try "#!/bin/sh\nprintf '%s\\n' '{\"type\":\"account\",\"account\":\"free\"}'\n"
            .write(to: graft, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: graft.path)

        let answers = Answers()
        let shell = ShellEnvironment(minimumInterval: 0) { answers.lock.withLock { ["PATH": answers.path] } }
        let runner = CLIRunner.forApp(
            shell: shell, process: ["PATH": "/usr/bin:/bin"],
            home: scratch.appendingPathComponent("home").path, isExecutable: scratchOnly()
        )
        let store = await ConfigStore(runner: runner, notify: { _ in })
        _ = await store.checkSetup()
        let first = await store.setupNeed
        XCTAssertEqual(first, .cli, "premise: not on any PATH the shell reported")

        answers.lock.withLock { answers.path = "\(graft.deletingLastPathComponent().path):/usr/bin:/bin" }
        _ = await store.checkSetup()
        let withoutRefresh = await store.setupNeed
        XCTAssertEqual(withoutRefresh, .cli, "control: the launch answer is cached")

        _ = await store.checkSetup(refreshingEnvironment: true)
        let refreshed = await store.setupNeed
        XCTAssertNil(refreshed, "Check again should have found the new PATH")
    }

    // MARK: - Merging into what the CLI is given

    private static let process = [
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": "/Users/x",
        "XDG_CONFIG_HOME": "/from/launchd",
    ]

    private static let shell = [
        "PATH": "/Users/x/Library/Python/3.14/bin:/Users/x/.volta/bin:/usr/bin:/bin",
        "HOME": "/Users/x",
        "XDG_CONFIG_HOME": "/Users/x/dotfiles/config",
        "XDG_CACHE_HOME": "/Users/x/.cache-elsewhere",
        "GRAFT_SSH_USER": "root",
        "UNFAIRPLAY_MODEL_DIR": "/Users/x/models",
        "EXAMPLE_P12_PASS": "secret",
        "PWD": "/Users/x",
        "OLDPWD": "/",
        "SHLVL": "1",
        "_": "/usr/bin/env",
        "TERM_SESSION_ID": "w0t0p0:ABC",
        "TERM": "xterm-256color",
        "COLUMNS": "80",
    ]

    func test_the_shells_variables_reach_the_cli() {
        let env = ProcessEnvironment.forCLI(shell: Self.shell, process: Self.process, home: "/Users/x")
        XCTAssertEqual(env["GRAFT_SSH_USER"], "root")
        XCTAssertEqual(env["UNFAIRPLAY_MODEL_DIR"], "/Users/x/models")
        XCTAssertEqual(env["EXAMPLE_P12_PASS"], "secret", "the variable cert_password_env names")
        XCTAssertEqual(env["XDG_CACHE_HOME"], "/Users/x/.cache-elsewhere")
        XCTAssertEqual(
            env["XDG_CONFIG_HOME"], "/Users/x/dotfiles/config",
            "the shell's value wins — Terminal and the app must use one config directory"
        )
    }

    func test_the_shell_describing_itself_is_left_out() {
        let env = ProcessEnvironment.forCLI(shell: Self.shell, process: Self.process, home: "/Users/x")
        for name in ["PWD", "OLDPWD", "SHLVL", "_", "TERM_SESSION_ID", "TERM"] {
            XCTAssertNil(env[name], name)
        }
    }

    /// The app's own augmentation is applied over the shell's answer, not
    /// replaced by it: the tool directories still lead PATH, the console is
    /// still pinned wide, and the shell's own PATH entries follow.
    func test_the_apps_augmentation_still_applies_on_top() throws {
        let env = ProcessEnvironment.forCLI(shell: Self.shell, process: Self.process, home: "/Users/x")
        let dirs = (env["PATH"] ?? "").split(separator: ":").map(String.init)
        XCTAssertEqual(Array(dirs.prefix(4)), ProcessEnvironment.toolDirs(home: "/Users/x"))
        XCTAssertTrue(dirs.contains("/Users/x/Library/Python/3.14/bin"), "pip --user's bin")
        XCTAssertTrue(dirs.contains("/Users/x/.volta/bin"), "where node and bagbak live")
        XCTAssertEqual(env["COLUMNS"], String(ProcessEnvironment.consoleColumns))
        XCTAssertEqual(env["LC_CTYPE"], ProcessEnvironment.utf8Locale)
    }

    /// The CLI's output is decoded as UTF-8 here, and the shell can bring a
    /// locale that isn't. Only the character type is corrected, and only when
    /// it needs to be — a user's own language setting is otherwise theirs.
    func test_the_cli_always_gets_a_utf8_character_type() {
        func env(_ vars: [String: String]) -> [String: String] {
            ProcessEnvironment.forCLI(shell: vars, process: [:], home: "/Users/x")
        }
        let latin1 = env(["LANG": "en_US.ISO8859-1"])
        XCTAssertEqual(latin1["LC_CTYPE"], ProcessEnvironment.utf8Locale)
        XCTAssertEqual(latin1["LANG"], "en_US.ISO8859-1", "the language itself is left alone")

        let french = env(["LANG": "fr_FR.UTF-8"])
        XCTAssertNil(french["LC_CTYPE"], "already UTF-8: nothing to correct")
        XCTAssertEqual(french["LANG"], "fr_FR.UTF-8")

        XCTAssertNil(env(["LC_CTYPE": "de_DE.utf8"])["LC_ALL"])
        XCTAssertEqual(env(["LC_CTYPE": "de_DE.utf8"])["LC_CTYPE"], "de_DE.utf8")

        let overriding = env(["LC_ALL": "C", "LANG": "en_US.UTF-8"])
        XCTAssertEqual(
            overriding["LC_ALL"], ProcessEnvironment.utf8Locale,
            "LC_ALL outranks LC_CTYPE, so setting LC_CTYPE beside it would change nothing"
        )
    }

    /// A user's `PYTHONPATH` must not change what the CLI imports, and the
    /// rest of the family changes the interpreter itself. None of them reach
    /// the CLI from the login shell or from the app's own environment.
    func test_python_variables_never_reach_the_cli() {
        let python = [
            "PYTHONPATH": "/Users/x/other-project/src",
            "PYTHONHOME": "/opt/example/python",
            "PYTHONSTARTUP": "/Users/x/.pythonrc",
            "PYTHONEXECUTABLE": "/opt/example/bin/python3",
            "PYTHONUSERBASE": "/Users/x/.pyuser",
            "PYTHONSAFEPATH": "1",
        ]
        let fromShell = ProcessEnvironment.forCLI(
            shell: Self.shell.merging(python) { a, _ in a }, process: Self.process, home: "/Users/x"
        )
        let fromProcess = ProcessEnvironment.forCLI(
            shell: nil, process: Self.process.merging(python) { a, _ in a }, home: "/Users/x"
        )
        for name in python.keys {
            XCTAssertNil(fromShell[name], "\(name) from the login shell")
            XCTAssertNil(fromProcess[name], "\(name) from the app's environment")
        }
        XCTAssertEqual(fromShell["GRAFT_SSH_USER"], "root", "the rest of the shell still merges")
    }

    func test_no_answer_from_the_shell_is_todays_behaviour() {
        XCTAssertEqual(
            ProcessEnvironment.forCLI(shell: nil, process: Self.process, home: "/Users/x"),
            ProcessEnvironment.augmented(base: Self.process, home: "/Users/x")
        )
    }

    // MARK: - The runner the app uses

    /// A `graft` that prints the variable a test is looking for, in a
    /// directory only the (fake) login shell's PATH names.
    private func fakeGraft(printing variable: String) throws -> URL {
        let bin = scratch.appendingPathComponent("shell-only-bin")
        try FileManager.default.createDirectory(at: bin, withIntermediateDirectories: true)
        let graft = bin.appendingPathComponent("graft")
        try "#!/bin/sh\nprintf '%s' \"$\(variable)\"\n".write(to: graft, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: graft.path)
        return graft
    }

    /// Only files under this test's scratch directory count, so a `graft` in
    /// `/opt/homebrew/bin` on the machine running the suite cannot answer.
    private func scratchOnly() -> @Sendable (String) -> Bool {
        let root = scratch.path
        return { $0.hasPrefix(root) && FileManager.default.isExecutableFile(atPath: $0) }
    }

    /// The first launch waits for the shell rather than racing ahead with
    /// launchd's environment — and `graft` is found on the PATH the shell
    /// reported, which no fixed directory names.
    func test_the_first_launch_waits_for_the_login_shell_and_uses_its_path() async throws {
        let graft = try fakeGraft(printing: "GRAFT_EXAMPLE")
        let bin = graft.deletingLastPathComponent().path
        let shell = ShellEnvironment {
            try? await Task.sleep(nanoseconds: 300_000_000)
            return ["PATH": "\(bin):/usr/bin:/bin", "GRAFT_EXAMPLE": "from-the-shell"]
        }
        let runner = CLIRunner.forApp(
            shell: shell, process: ["PATH": "/usr/bin:/bin"],
            home: scratch.appendingPathComponent("home").path, isExecutable: scratchOnly()
        )
        let result = try await runner.runOnce(arguments: [])
        XCTAssertEqual(result.exitCode, 0)
        XCTAssertEqual(result.stdout, "from-the-shell")
    }

    /// The setup screen's Check again: the CLI is installed while the app is
    /// open, and the *same* runner finds it on the next launch — the path is
    /// not decided once at startup.
    func test_a_graft_installed_after_launch_is_found_without_relaunching() async throws {
        let home = scratch.appendingPathComponent("home")
        let runner = CLIRunner.forApp(
            shell: ShellEnvironment { nil }, process: ["PATH": "/usr/bin:/bin"],
            home: home.path, isExecutable: scratchOnly()
        )
        do {
            _ = try await runner.runOnce(arguments: [])
            XCTFail("premise: nothing is installed yet")
        } catch CLIRunner.RunError.launchFailed {
            // expected
        }

        let localBin = home.appendingPathComponent(".local/bin")
        try FileManager.default.createDirectory(at: localBin, withIntermediateDirectories: true)
        let graft = localBin.appendingPathComponent("graft")
        try "#!/bin/sh\nprintf installed\n".write(to: graft, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: graft.path)

        let result = try await runner.runOnce(arguments: [])
        XCTAssertEqual(result.stdout, "installed")
    }

    /// No answer from the shell leaves the app where it was: the process's
    /// own environment, augmented.
    func test_a_shell_with_no_answer_falls_back_to_the_process_environment() async throws {
        let graft = try fakeGraft(printing: "GRAFT_EXAMPLE")
        let bin = graft.deletingLastPathComponent().path
        let runner = CLIRunner.forApp(
            shell: ShellEnvironment { nil },
            process: ["PATH": "\(bin):/usr/bin:/bin", "GRAFT_EXAMPLE": "from-the-process"],
            home: scratch.appendingPathComponent("home").path, isExecutable: scratchOnly()
        )
        let result = try await runner.runOnce(arguments: [])
        XCTAssertEqual(result.stdout, "from-the-process")
    }
}
