import XCTest
@testable import Graft

final class CLIRunnerTests: XCTestCase {
    func test_augmented_environment_prepends_homebrew_and_local_bin() throws {
        // A GUI app launched from the Dock inherits only the launchd PATH,
        // which omits the dirs where cyan / zsign / idevice_id live. The CLI
        // subprocess must be given a PATH that includes them.
        let base = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": "/Users/x"]
        let env = ProcessEnvironment.augmented(base: base, home: "/Users/x")
        let dirs = (env["PATH"] ?? "").split(separator: ":").map(String.init)

        XCTAssertTrue(dirs.contains("/Users/x/.local/bin"))
        XCTAssertTrue(dirs.contains("/opt/homebrew/bin"))
        XCTAssertTrue(dirs.contains("/usr/local/bin"))
        // existing entries preserved
        XCTAssertTrue(dirs.contains("/usr/bin"))
        // tool dirs take precedence over the inherited PATH
        XCTAssertLessThan(dirs.firstIndex(of: "/opt/homebrew/bin")!,
                          dirs.firstIndex(of: "/usr/bin")!)
        // no duplicates
        XCTAssertEqual(dirs.count, Set(dirs).count)
        // other variables are carried through untouched
        XCTAssertEqual(env["HOME"], "/Users/x")
    }

    /// The CLI prints its `error:`/`hint:` pair through Rich, which wraps to
    /// `COLUMNS` — and Rich's wrap cannot be undone downstream:
    /// `Text.rstrip_end(width)` crops the space a soft wrap broke on whenever
    /// the divided line lands one column past the width, so a word wrap and a
    /// token folded mid-way both arrive as a width-length line that doesn't
    /// end in whitespace. The console is therefore made wide enough that Rich
    /// has nothing to break.
    func test_the_cli_is_given_a_console_too_wide_to_wrap_a_message() throws {
        let env = ProcessEnvironment.augmented(
            base: ["PATH": "/usr/bin:/bin", "COLUMNS": "80"], home: "/Users/x"
        )
        XCTAssertEqual(
            env["COLUMNS"], String(ProcessEnvironment.consoleColumns),
            "an inherited COLUMNS — this app launched from a terminal rather than "
            + "the Dock — must not decide how the text a user is shown gets wrapped"
        )
        // Tied to a real message rather than asserted as a round number: the
        // widest thing `_fatal` composes is a sentence with a full cache path
        // in it, and that is what the width has to clear.
        let widest = "error: nothing to re-install for build 41: \(Self.foldedPath) is missing"
        XCTAssertLessThan(
            widest.count, ProcessEnvironment.consoleColumns,
            "the pin has to clear the CLI's longest message or Rich still wraps it"
        )
    }

    /// Typer and Click rule a *usage* error into a box the full width of the
    /// console and read `TERMINAL_WIDTH` in preference to `COLUMNS`. Widening
    /// the message console must not widen that box: its bottom rule is the
    /// last line of stderr, which is exactly what `summary` quotes into the
    /// banner when there is no `error:` marker to find.
    func test_the_usage_error_box_is_not_widened_along_with_the_message() throws {
        let env = ProcessEnvironment.augmented(base: [:], home: "/Users/x")
        XCTAssertEqual(env["TERMINAL_WIDTH"], String(ProcessEnvironment.usagePanelColumns))
        XCTAssertEqual(ProcessEnvironment.usagePanelColumns, 80, "Rich's own default")
    }

    func test_augmented_environment_handles_missing_path() throws {
        let env = ProcessEnvironment.augmented(base: [:], home: "/Users/x")
        let dirs = (env["PATH"] ?? "").split(separator: ":").map(String.init)
        XCTAssertTrue(dirs.contains("/opt/homebrew/bin"))
        XCTAssertTrue(dirs.contains("/usr/bin"))  // sane default floor
    }

    func test_runOnce_captures_stdout_of_echo() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/echo"))
        let result = try await runner.runOnce(arguments: ["hello world"])
        XCTAssertEqual(result.exitCode, 0)
        XCTAssertEqual(result.stdout.trimmingCharacters(in: .whitespacesAndNewlines), "hello world")
    }

    func test_runOnce_captures_nonzero_exit() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let result = try await runner.runOnce(arguments: ["-c", "echo err >&2; exit 42"])
        XCTAssertEqual(result.exitCode, 42)
        XCTAssertTrue(result.stderr.contains("err"))
    }

    func test_stream_emits_events_line_by_line() async throws {
        let script = """
        printf '{"type":"stage","name":"a","status":"start"}\\n'
        sleep 0.05
        printf '{"type":"log","source":"a","line":"hello"}\\n'
        sleep 0.05
        printf '{"type":"stage","name":"a","status":"end"}\\n'
        """
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))

        actor Collector {
            var events: [BuildEvent] = []
            func append(_ e: BuildEvent) { events.append(e) }
            func snapshot() -> [BuildEvent] { events }
        }
        let collector = Collector()

        let result = try await runner.stream(arguments: ["-c", script]) { event in
            Task { await collector.append(event) }
        }

        // Brief settle so the actor tasks scheduled from termination flush.
        try await Task.sleep(nanoseconds: 100_000_000)

        let collected = await collector.snapshot()
        XCTAssertEqual(result.exitCode, 0)
        XCTAssertEqual(collected.count, 3)
        if case let .stage(name1, status1, _) = collected[0] {
            XCTAssertEqual(name1, "a")
            XCTAssertEqual(status1, "start")
        } else { XCTFail("expected stage start at index 0") }
        if case let .log(source, line) = collected[1] {
            XCTAssertEqual(source, "a")
            XCTAssertEqual(line, "hello")
        } else { XCTFail("expected log at index 1") }
    }

    /// Regression for a hang listing a large APT repo: a subprocess that dumps >64 KB of
    /// stdout (the macOS pipe buffer) must not deadlock stream(). The old
    /// implementation set readabilityHandler before Process.run() and could
    /// silently never fire for short-lived high-throughput children, leaving
    /// the kernel pipe full and the child blocked on its next write.
    func test_stream_drains_large_stdout_without_deadlock() async throws {
        // Build a script that emits 200 fat repo-package events (~600 bytes
        // each ≈ 120 KB total — well over the 64 KB stdout pipe buffer) and
        // a terminator, then exits. Modelled on a real APT repo's
        // listing.
        let pkgLine = #"{"type":"repo-package","package_id":"com.example.tweak.PADXX","name":"Tweak PADXX","version":"1.2.3","description":"some description here padded out to roughly match what an APT Packages stanza looks like in practice","author":"someone","section":"Tweaks","filename":"debs/com.example.tweak.PADXX_1.2.3_iphoneos-arm.deb","size_bytes":54321,"sha256":"deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef","depiction":"https://example.com/depiction/PADXX","download_url":"https://repo.example.com/debs/com.example.tweak.PADXX_1.2.3_iphoneos-arm.deb"}"#
        var script = ""
        for i in 0..<200 {
            let stamp = String(format: "%03d", i)
            script += "printf '\(pkgLine.replacingOccurrences(of: "PADXX", with: stamp))\\n'\n"
        }
        script += "printf '{\"type\":\"repo-packages-end\",\"url\":\"https://repo.example.com\",\"count\":200}\\n'\n"
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        // If the drainer ever deadlocks the test would hang forever; xctest
        // would eventually time it out, but be explicit by giving stream() a
        // generous-but-finite wall-clock budget via a Task.
        let started = Date()
        let result = try await runner.stream(arguments: ["-c", script]) { _ in }
        let elapsed = Date().timeIntervalSince(started)
        XCTAssertEqual(result.exitCode, 0)
        XCTAssertLessThan(elapsed, 10.0, "stream() took \(elapsed)s — pipe drainer is likely deadlocked")
        // result.events must contain every emitted event (synchronously
        // collected inside stream, so unaffected by MainActor Task timing).
        let pkgCount = result.events.filter {
            if case .repoPackage = $0 { return true } else { return false }
        }.count
        XCTAssertEqual(pkgCount, 200, "lost packages mid-stream — drainer didn't keep up")
        let sawEnd = result.events.contains {
            if case .repoPackagesEnd = $0 { return true } else { return false }
        }
        XCTAssertTrue(sawEnd, "repo-packages-end was dropped — drainer or termination raced")
    }

    // MARK: - Pipe-buffer deadlock

    /// A `sh` script emitting `count` lines of `bytes`-ish payload, to `fd`.
    /// Built as a loop over one shell variable rather than `count` literal
    /// `printf`s so the script itself stays small.
    private func floodScript(count: Int, width: Int, fd: String) -> (script: String, bytes: Int) {
        let pad = String(repeating: "x", count: width)
        let redirect = fd == "2" ? " >&2" : ""
        let script = """
        PAD='\(pad)'
        i=0
        while [ $i -lt \(count) ]; do printf '%s\\n' "$PAD"\(redirect); i=$((i+1)); done
        """
        return (script, count * (width + 1))
    }

    /// The `runOnce` twin of `test_stream_drains_large_stdout_without_deadlock`.
    ///
    /// `runOnce` used to hand stdout to `readDataToEndOfFile()` from inside
    /// `terminationHandler` — i.e. it only started reading *after* the child
    /// exited. A child that fills the 64 KB kernel pipe buffer blocks on its
    /// next `write(2)`, so it never exits, `terminationHandler` never fires,
    /// and `runOnce` never returns. The user-visible symptom is a GUI that
    /// hangs with no error and no timeout.
    ///
    /// `--list` is the caller that reaches this first: it emits an `app` line
    /// plus a ~250-byte `delivery-state` line per app, so ~100 apps clears the
    /// buffer. See `DeliveryStateTests.test_loadApps_survives_a_long_app_list`.
    func test_runOnce_drains_large_stdout_without_deadlock() async throws {
        let (script, bytes) = floodScript(count: 600, width: 255, fd: "1")
        XCTAssertGreaterThan(bytes, 64 * 1024, "fixture must exceed the stdout pipe buffer")
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))

        guard let outcome = await finishing(within: 15, {
            try await runner.runOnce(arguments: ["-c", script])
        }) else {
            return XCTFail("runOnce never returned for \(bytes) bytes of stdout — deadlocked "
                           + "on the pipe buffer, which is a silent hang in the app")
        }
        let result = try outcome.get()
        XCTAssertEqual(result.exitCode, 0)
        XCTAssertEqual(
            result.stdout.split(separator: "\n").count, 600,
            "stdout was truncated — the drainer didn't reach EOF"
        )
    }

    /// `drainGrace` is a margin for losing a race, not a step in the normal
    /// path: a child that has exited leaves at most one pipe buffer of unread
    /// bytes behind — it could not have exited while blocked on a write — so
    /// the drainers hit EOF and signal almost at once.
    ///
    /// Nothing pinned that. The two truncation guards above pass identically
    /// with the grace set to `0.0` (measured, 20 iterations, 0 failures),
    /// because a healthy run never reaches the wait at all. They therefore say
    /// nothing about the value, and cannot catch the regression that actually
    /// costs something: a drainer that stops signalling on EOF makes *every*
    /// CLI call the app issues pay the full grace, silently, forever. A build
    /// that got half a second slower is not going to fail anyone's test.
    ///
    /// So this asserts the timing directly. The fixture floods past the pipe
    /// buffer so there really are bytes in flight when the child exits;
    /// healthy is ~0.02s against a 0.5s grace, and the threshold sits between.
    func test_a_healthy_run_does_not_spend_the_drain_grace() async throws {
        let (script, bytes) = floodScript(count: 600, width: 255, fd: "1")
        XCTAssertGreaterThan(bytes, 64 * 1024, "fixture must leave bytes in flight at exit")
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))

        let started = Date()
        let result = try await runner.runOnce(arguments: ["-c", script])
        let elapsed = Date().timeIntervalSince(started)

        XCTAssertEqual(
            result.stdout.split(separator: "\n").count, 600,
            "output was truncated, which makes the timing below meaningless"
        )
        XCTAssertLessThan(
            elapsed, 0.25,
            "a healthy run took \(elapsed)s — it is now waiting out the drain grace "
            + "instead of being signalled at EOF, which taxes every CLI call in the app"
        )
    }

    /// A naive fix — drain stdout to EOF, *then* stderr — still deadlocks:
    /// stderr's buffer is 16 KB, so a child interleaving both blocks on
    /// stderr while we're still parked on stdout. The two drains have to be
    /// concurrent with each other as well as with the child.
    func test_runOnce_drains_both_pipes_concurrently() async throws {
        let pad = String(repeating: "x", count: 255)
        let script = """
        PAD='\(pad)'
        i=0
        while [ $i -lt 400 ]; do
          printf '%s\\n' "$PAD"
          printf '%s\\n' "$PAD" >&2
          i=$((i+1))
        done
        exit 7
        """
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))

        guard let outcome = await finishing(within: 15, {
            try await runner.runOnce(arguments: ["-c", script])
        }) else {
            return XCTFail("runOnce never returned with ~100 KB on each pipe — "
                           + "stdout and stderr must be drained concurrently")
        }
        let result = try outcome.get()
        XCTAssertEqual(result.exitCode, 7, "exit status must survive the drain")
        XCTAssertEqual(result.stdout.split(separator: "\n").count, 400)
        XCTAssertEqual(result.stderr.split(separator: "\n").count, 400)
    }

    // MARK: - Cancellation

    /// Whether any process has `marker` in its argv. The assertion that matters
    /// is about the OS, not about whether our Task returned — a Task can finish
    /// promptly while the child keeps burning CPU, which is exactly the bug
    /// these cover.
    private func processExists(_ marker: String) -> Bool {
        let probe = Process()
        probe.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
        probe.arguments = ["-f", marker]
        let out = Pipe()
        probe.standardOutput = out
        probe.standardError = Pipe()
        try? probe.run()
        probe.waitUntilExit()
        return !out.fileHandleForReading.readDataToEndOfFile().isEmpty
    }

    /// Belt-and-braces so a regression can't leak a 30s sleeper into later tests.
    private func reap(_ marker: String) {
        let killer = Process()
        killer.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        killer.arguments = ["-9", "-f", marker]
        killer.standardOutput = Pipe()
        killer.standardError = Pipe()
        try? killer.run()
        killer.waitUntilExit()
    }

    // NOTE: deliberately does NOT `await task.value` after cancelling. Awaiting
    // blocks until the child exits on its own, so the probe would run after the
    // sleep had already elapsed and would pass against the unfixed code — this
    // test did exactly that before being rewritten. Probe while the child would
    // still be alive if nothing killed it.
    func test_runOnce_cancellation_kills_the_subprocess() async throws {
        let marker = "tb-cancel-once-\(UUID().uuidString)"
        defer { reap(marker) }
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))

        let task = Task {
            try await runner.runOnce(arguments: ["-c", "echo \(marker); sleep 30"])
        }
        try await Task.sleep(nanoseconds: 800_000_000)
        XCTAssertTrue(processExists(marker), "subprocess should be running before we cancel")

        task.cancel()
        try await Task.sleep(nanoseconds: 3_500_000_000)  // SIGTERM, then the 2s SIGKILL escalation

        XCTAssertFalse(processExists(marker),
                       "cancelling the Task must terminate the subprocess, not orphan it")
    }

    func test_stream_cancellation_kills_the_subprocess() async throws {
        let marker = "tb-cancel-stream-\(UUID().uuidString)"
        defer { reap(marker) }
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))

        let task = Task {
            try await runner.stream(arguments: ["-c", "echo \(marker); sleep 30"]) { _ in }
        }
        try await Task.sleep(nanoseconds: 800_000_000)
        XCTAssertTrue(processExists(marker), "subprocess should be running before we cancel")

        task.cancel()
        try await Task.sleep(nanoseconds: 3_500_000_000)

        XCTAssertFalse(processExists(marker),
                       "a cancelled build must not keep signing and installing")
    }

    func test_cancelling_before_launch_does_not_spawn_anything() async throws {
        let marker = "tb-cancel-early-\(UUID().uuidString)"
        defer { reap(marker) }
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))

        let task = Task {
            try await runner.runOnce(arguments: ["-c", "echo \(marker); sleep 30"])
        }
        task.cancel()  // before the Task body has had a chance to run
        try await Task.sleep(nanoseconds: 1_500_000_000)

        XCTAssertFalse(processExists(marker),
                       "a Task cancelled during setup must not leave a subprocess behind")
    }

    func test_uncancelled_run_still_completes_normally() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let result = try await runner.runOnce(arguments: ["-c", "echo hello; exit 3"])
        XCTAssertEqual(result.exitCode, 3, "cancellation plumbing must not disturb normal exits")
        XCTAssertTrue(result.stdout.contains("hello"))
    }

    // MARK: - Reading a failure off stderr

    /// Byte-for-byte what `graft --json --reinstall-build N` writes to
    /// stderr with the iPhone unplugged, captured by running the CLI's own
    /// `_fatal` through the CLI's own `Console(stderr=True)` with stderr
    /// redirected to a file.
    ///
    /// The wrap is the point. Rich hard-wraps at 80 columns when stderr isn't
    /// a TTY, so the hint arrives split after "or enable " — with a trailing
    /// space — and its remainder starts at column 0 on the next line, looking
    /// exactly like a line of its own. Built by joining rather than as a
    /// multi-line literal so that trailing space survives an editor.
    private static let unpluggedIPhoneStderr = [
        "error: no iOS devices connected",
        "  hint: plug the iPhone in via USB and tap 'Trust this Computer', or enable ",
        "Wi-Fi sync in Finder",
        "",
    ].joined(separator: "\n")

    private static let unpluggedIPhoneHint =
        "plug the iPhone in via USB and tap 'Trust this Computer', "
        + "or enable Wi-Fi sync in Finder"

    /// The failure this whole path exists for: the CLI raises before it emits
    /// `build-start`, so the event stream is empty and stderr is the only
    /// account of what happened. Reported as "exit 8" for as long as views
    /// read the status and dropped `result.stderr`.
    func test_summary_reflows_the_clis_error_and_hint() {
        XCTAssertEqual(
            CLIFailure.summary(exitCode: 8, stderr: Self.unpluggedIPhoneStderr),
            "no iOS devices connected — hint: \(Self.unpluggedIPhoneHint)",
            "the hint is the actionable half and Rich wrapped it — taking only "
            + "the line the marker is on cuts it mid-sentence"
        )
    }

    func test_parse_separates_the_error_from_the_hint() throws {
        let parsed = try XCTUnwrap(CLIFailure.parse(stderr: Self.unpluggedIPhoneStderr))
        XCTAssertEqual(parsed.error, "no iOS devices connected")
        XCTAssertEqual(parsed.hint, Self.unpluggedIPhoneHint)
    }

    /// A long *message* wraps for exactly the same reason a long hint does.
    func test_summary_reflows_a_wrapped_message_with_no_hint() {
        let stderr = [
            "error: build 41 did not produce a signed IPA, so there is nothing here ",
            "to re-install",
        ].joined(separator: "\n")
        XCTAssertEqual(
            CLIFailure.summary(exitCode: 1, stderr: stderr),
            "build 41 did not produce a signed IPA, so there is nothing here to re-install"
        )
    }

    /// Most `_fatal` calls carry no hint, and the exit code adds nothing to a
    /// sentence that already says what went wrong.
    func test_summary_of_an_error_without_a_hint_is_just_the_error() {
        XCTAssertEqual(
            CLIFailure.summary(exitCode: 1, stderr: "error: no build with id 999999\n"),
            "no build with id 999999"
        )
    }

    /// The CLI is not the only thing that can write to stderr, and the other
    /// agent's traceback fix does not make this case impossible.
    func test_summary_falls_back_to_the_last_line_of_a_traceback() {
        let stderr = """
        Traceback (most recent call last):
          File "/x/graft_ipa/cli.py", line 2423, in _build_one
            _record_delivery_if_pushed(
        KeyError: 'lc_bundle_id'
        """
        XCTAssertEqual(
            CLIFailure.summary(exitCode: 1, stderr: stderr),
            "exit 1: KeyError: 'lc_bundle_id'",
            "the exception line is the informative one; the status stays because "
            + "a fragment of someone else's output may not be self-explanatory"
        )
    }

    func test_summary_of_a_silent_nonzero_exit_is_the_status() {
        XCTAssertEqual(CLIFailure.summary(exitCode: 8, stderr: ""), "exit 8")
        XCTAssertEqual(CLIFailure.summary(exitCode: 8, stderr: "\n  \n"), "exit 8")
    }

    /// `hint:` with no `error:` above it isn't `_fatal`'s shape and must not be
    /// promoted to a message — otherwise a stray line from a subprocess gets
    /// presented as the CLI's own verdict.
    func test_a_hint_with_no_error_is_not_parsed_as_one() {
        XCTAssertNil(CLIFailure.parse(stderr: "  hint: try turning it off and on\n"))
        XCTAssertEqual(
            CLIFailure.summary(exitCode: 3, stderr: "  hint: try turning it off and on\n"),
            "exit 3: hint: try turning it off and on"
        )
    }

    /// `_fatal` prints the hint *after* the error it belongs to. A `hint:` line
    /// ahead of one came from something else, and attaching it would be the
    /// GUI inventing advice the CLI never gave for this failure.
    func test_a_hint_printed_before_the_error_is_not_attached_to_it() throws {
        let stderr = [
            "  hint: left over from some other tool",
            "error: archived IPA missing",
            "",
        ].joined(separator: "\n")
        let parsed = try XCTUnwrap(CLIFailure.parse(stderr: stderr))
        XCTAssertEqual(parsed.error, "archived IPA missing")
        XCTAssertNil(parsed.hint)
    }

    /// The log panel gets the reflowed pair and nothing else: the reflow loses
    /// nothing, so printing the raw text under it would repeat the same
    /// sentence with 80-column breaks in it.
    func test_logText_unwraps_the_error_and_hint_without_repeating_them() {
        XCTAssertEqual(
            CLIFailure.logText(exitCode: 8, stderr: Self.unpluggedIPhoneStderr),
            ["✗ no iOS devices connected", "  hint: \(Self.unpluggedIPhoneHint)"]
        )
    }

    /// Anything that isn't `_fatal`'s shape goes in verbatim, frames and all —
    /// nothing here knows which line of it matters.
    func test_logText_keeps_an_unrecognised_stderr_verbatim() {
        let stderr = """
        Traceback (most recent call last):
          File "/x/graft_ipa/cli.py", line 2423, in _build_one
        KeyError: 'lc_bundle_id'
        """
        XCTAssertEqual(
            CLIFailure.logText(exitCode: 1, stderr: stderr),
            [
                "✗ exit 1",
                "Traceback (most recent call last):",
                #"  File "/x/graft_ipa/cli.py", line 2423, in _build_one"#,
                "KeyError: 'lc_bundle_id'",
            ]
        )
    }

    func test_logText_says_something_even_when_stderr_is_empty() {
        XCTAssertEqual(
            CLIFailure.logText(exitCode: 8, stderr: ""),
            ["✗ exit 8 — the CLI exited without saying why"]
        )
    }

    // MARK: - a long token folded mid-wrap
    //
    // Rich has three ways of ending a line — a soft wrap at a word boundary, a
    // token folded at the width, a newline inside the message — and they are
    // indistinguishable by the time this process sees them. The previous round
    // tried to tell the first two apart by geometry ("reaches the widest line
    // seen, doesn't end in whitespace") and fused what it judged a fold.
    // `Text.rstrip_end(width)` makes that unwinnable: it crops the space a
    // wrap broke on whenever the divided line lands one column past the width,
    // so an ordinary word wrap produces exactly the same shape.
    //
    // So the fold is prevented rather than detected. The CLI is run with
    // `COLUMNS` pinned wide enough that Rich has nothing to break, and the
    // rejoin is one space, always. What that costs when the pin doesn't take
    // is asserted below rather than left to be discovered.

    /// Byte-for-byte what Rich produces for a message containing a cache path,
    /// verified by running the real `Console` with a non-TTY stream *at 80
    /// columns* — the width Rich falls back to when nothing pins it. The break
    /// falls at column 80, inside the filename, with no space anywhere near
    /// it.
    ///
    /// The path is 81 characters on purpose — one more than the width — and
    /// the user name is padded to keep it there. Rich only folds a word longer
    /// than the line, so a path that fits in 80 wraps cleanly at the space
    /// before it and the real-Rich control below stops measuring anything:
    /// shortening `tweak-build` to `graft` here did exactly that.
    private static let foldedPathStderr = [
        "error: nothing to re-install for build 41: ",
        "/Users/exampleuser/Library/Caches/graft/built/example_19.16.3_20260201T101500.ip",
        "a is missing",
        "",
    ].joined(separator: "\n")

    private static let foldedPath =
        "/Users/exampleuser/Library/Caches/graft/built/example_19.16.3_20260201T101500.ipa"

    /// The same message at the width the app pins, which is one line and needs
    /// no rejoining at all. This is what the user's CLI actually emits.
    private static let unfoldedPathStderr =
        "error: nothing to re-install for build 41: "
        + "/Users/exampleuser/Library/Caches/graft/built/example_19.16.3_20260201T101500.ipa"
        + " is missing\n"

    /// Byte-for-byte from rich 15.0.0 at 80 columns, and the defect the fold
    /// inference shipped with: an ordinary sentence whose word-boundary wrap
    /// lands one column past the width, so `rstrip_end` crops the space Rich
    /// broke on. Line one ends "…2026-01-14 at" with no trailing space — the
    /// exact shape a folded path has — and the inference fused it into
    /// "atnoon". Against real Rich that happens in 123 of 500 randomly
    /// generated wrapped messages, and since `errorDescription` was rewired it
    /// reaches every banner in the app.
    private static let croppedWordWrapStderr = [
        "error: could not sign example: the provisioning profile expired on 2026-01-14 at",
        "noon and the certificate it was issued against is no longer in the login ",
        "keychain",
        "",
    ].joined(separator: "\n")

    func test_parse_does_not_fuse_a_word_wrap_whose_space_rich_cropped() throws {
        XCTAssertEqual(
            Self.croppedWordWrapStderr.split(separator: "\n").first?.count, 80,
            "premise: the wrap has to land exactly on the width, which is the only "
            + "place a cropped space is mistakable for a fold"
        )
        let parsed = try XCTUnwrap(CLIFailure.parse(stderr: Self.croppedWordWrapStderr))
        XCTAssertEqual(
            parsed.error,
            "could not sign example: the provisioning profile expired on 2026-01-14 "
            + "at noon and the certificate it was issued against is no longer in the "
            + "login keychain"
        )
        XCTAssertFalse(parsed.error.contains("atnoon"), "two words run together: \(parsed.error)")
    }

    /// A urllib3 `NotOpenSSLWarning`, byte-for-byte off macOS's LibreSSL —
    /// 230-odd characters, on stderr, before the CLI prints anything of its
    /// own. A pymobiledevice3 log line or a traceback frame path does the same
    /// thing.
    private static let opensslWarning =
        "/opt/homebrew/lib/python3.13/site-packages/urllib3/__init__.py:35: "
        + "NotOpenSSLWarning: urllib3 v2 only supports OpenSSL 1.1.1+, currently "
        + "the 'ssl' module is compiled with 'LibreSSL 2.8.3'. See: "
        + "https://github.com/urllib3/urllib3/issues/3020"

    /// Nothing outside the block may change the block. The inference this
    /// replaced took its width from *every* non-blank stderr line, so one
    /// unrelated line — the warning below, a `pymobiledevice3` log line, a
    /// traceback frame — moved the inferred width above Rich's and silently
    /// changed how the message was put back together.
    func test_noise_before_the_marker_cannot_change_the_parsed_message() throws {
        XCTAssertGreaterThan(
            Self.opensslWarning.count, 80,
            "premise: the noise has to be wider than the console the CLI wrapped to"
        )
        let clean = try XCTUnwrap(CLIFailure.parse(stderr: Self.foldedPathStderr))
        let noisy = try XCTUnwrap(
            CLIFailure.parse(stderr: Self.opensslWarning + "\n" + Self.foldedPathStderr)
        )
        XCTAssertEqual(
            noisy, clean,
            "a warning the CLI didn't write changed the message it did"
        )
    }

    /// At the pinned width there is no fold, so the path is whole and
    /// copyable. This is the property the fold inference was reaching for, and
    /// it is now held by arranging the input rather than by guessing about it.
    func test_a_message_that_was_never_wrapped_keeps_its_path() throws {
        let parsed = try XCTUnwrap(CLIFailure.parse(stderr: Self.unfoldedPathStderr))
        XCTAssertEqual(
            parsed.error,
            "nothing to re-install for build 41: \(Self.foldedPath) is missing"
        )
        XCTAssertEqual(
            CLIFailure.summary(exitCode: 1, stderr: Self.unfoldedPathStderr),
            "nothing to re-install for build 41: \(Self.foldedPath) is missing"
        )
    }

    /// What the trade costs, stated instead of discovered. When the pin
    /// doesn't take — a `graft` older than it, a Rich that ignores
    /// `COLUMNS`, the CLI reached some other way — a folded path is rejoined
    /// with the same single space as everything else and stops being
    /// copyable.
    ///
    /// It is the lesser half of a choice with no clean side: every character
    /// survives in order and the break is visible, where fusing the wrong pair
    /// produces a word that was never in the message and is unrecoverable
    /// by eye. And it is the rarer half — a fold needs a token longer than the
    /// line, where the false positive needs only prose.
    func test_a_fold_that_reaches_us_anyway_is_spaced_rather_than_fused() throws {
        let parsed = try XCTUnwrap(CLIFailure.parse(stderr: Self.foldedPathStderr))
        XCTAssertEqual(
            parsed.error,
            "nothing to re-install for build 41: "
            + "/Users/exampleuser/Library/Caches/graft/built/example_19.16.3_20260201T101500.ip"
            + " a is missing"
        )
        XCTAssertTrue(
            parsed.error.replacingOccurrences(of: " ", with: "")
                .contains(Self.foldedPath.replacingOccurrences(of: " ", with: "")),
            "lossy in spacing is the trade; losing characters is not"
        )
    }

    /// A newline *inside* the message ends a line short and without trailing
    /// whitespace, exactly as a fold does — and this is the test that was
    /// supposed to say so.
    ///
    /// Its fixture used to be 18, 25 and 8 characters wide, which put the
    /// inferred console width at 25, under the 40-column floor the inference
    /// refused to guess below. Every separator was a space before the fold
    /// branch was ever consulted, so deleting the width condition, inverting
    /// it, or removing the branch outright all left this green — which is how
    /// the inference reached a release. The premise is asserted here so a
    /// fixture that shrinks back under a guard fails instead of passing.
    func test_parse_still_spaces_a_message_that_contains_its_own_newlines() throws {
        let lines = [
            "error: cyan exited 1 while repacking the payload",
            "stderr was: unable to open the archive for writing",
            "and the build has been abandoned",
        ]
        let widest = lines.map(\.count).max() ?? 0
        XCTAssertGreaterThanOrEqual(
            widest, 40,
            "premise: the fixture has to be wide enough to reach a width-based rule at all"
        )
        XCTAssertGreaterThan(
            widest, lines.last?.count ?? 0,
            "premise: the widest line must not be the last one, or nothing joins onto it"
        )

        let parsed = try XCTUnwrap(CLIFailure.parse(stderr: (lines + [""]).joined(separator: "\n")))
        XCTAssertEqual(
            parsed.error,
            "cyan exited 1 while repacking the payload "
            + "stderr was: unable to open the archive for writing "
            + "and the build has been abandoned"
        )
    }

    /// `logText` shares the reflow, so it shares the rule — and it is what the
    /// build panels print, where a path is most likely to be copied.
    func test_logText_shares_the_reflow_with_parse() {
        XCTAssertEqual(
            CLIFailure.logText(exitCode: 1, stderr: Self.unfoldedPathStderr),
            ["✗ nothing to re-install for build 41: \(Self.foldedPath) is missing"]
        )
    }

    // MARK: - against the Rich the CLI actually runs
    //
    // Everything above is a fixture. This is the wrap itself, from the pinned
    // rich 15.0.0, at the width `ProcessEnvironment` sets — which is the only
    // way to know the pin does anything, and the way the `rstrip_end`
    // collision was found in the first place.

    /// The repo's venv, or nil on a checkout that hasn't made one. Skips
    /// rather than fails, like `CLIContractTests.locateCLI`.
    private static func locateRichPython() -> URL? {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // GraftTests
            .deletingLastPathComponent()   // Graft
            .deletingLastPathComponent()   // repo root
        let python = root.appendingPathComponent(".venv/bin/python")
        return FileManager.default.isExecutableFile(atPath: python.path) ? python : nil
    }

    /// `_fatal`'s two prints, made the way the CLI makes them: one `Console`
    /// on stderr, markup for the marker, the untrusted half escaped.
    private static let richFatalScript = #"""
    import sys
    from rich.console import Console
    from rich.markup import escape
    err = Console(stderr=True)
    err.print(f"[red]error:[/red] {escape(sys.argv[1])}")
    err.print(f"  [yellow]hint:[/yellow] {escape(sys.argv[2])}")
    """#

    func test_real_rich_does_not_wrap_at_the_width_the_app_pins() async throws {
        guard let python = Self.locateRichPython() else {
            throw XCTSkip("no .venv/bin/python in this checkout")
        }
        let message = "nothing to re-install for build 41: \(Self.foldedPath) is missing"
        let hint = "run the build again, or pick a different build"
        let args = ["-c", Self.richFatalScript, message, hint]

        let pinnedEnv = ProcessEnvironment.forCLI()
        let pinned = try await CLIRunner(executable: python, environment: pinnedEnv)
            .runOnce(arguments: args)
        guard pinned.exitCode == 0 else {
            throw XCTSkip("rich isn't importable from the venv: \(pinned.stderr)")
        }

        XCTAssertEqual(
            CLIFailure.stderrLines(pinned.stderr).count, 2,
            "an error and a hint, one line each — Rich had nothing to wrap: \(pinned.stderr)"
        )
        let parsed = try XCTUnwrap(CLIFailure.parse(stderr: pinned.stderr))
        XCTAssertEqual(parsed.error, message)
        XCTAssertEqual(parsed.hint, hint)

        // The control, without which the assertion above would also pass on a
        // message that simply happened to be short: the same Rich, the same
        // message, at the 80 columns it falls back to.
        var unpinned = pinnedEnv
        unpinned["COLUMNS"] = "80"
        let wrapped = try await CLIRunner(executable: python, environment: unpinned)
            .runOnce(arguments: args)
        XCTAssertGreaterThan(
            CLIFailure.stderrLines(wrapped.stderr).count, 2,
            "control: unpinned, Rich folds this message — so the pin is what the "
            + "assertion above is measuring"
        )
        XCTAssertFalse(
            CLIFailure.parse(stderr: wrapped.stderr)?.error.contains(Self.foldedPath) ?? false,
            "control: once folded the path cannot be rejoined, which is why the "
            + "width is pinned instead of the break being guessed at"
        )
    }

    // MARK: - what a store's error banner actually shows
    //
    // `CLIError.exited` is what every store throws, and most callers render
    // `error.localizedDescription` straight into an alert or a banner. It used
    // to hand back Rich's terminal output verbatim: an `error:` prefix, a
    // `hint:` line written for someone with a shell prompt under it, and
    // 80-column hard wraps that a resizable window then re-wraps.

    private func message(exitCode: Int32, stderr: String) -> String {
        CLIError.exited(doing: "Load devices", code: exitCode, stderr: stderr)
            .localizedDescription
    }

    func test_exited_error_reads_as_one_sentence_not_as_terminal_output() {
        let shown = message(exitCode: 8, stderr: Self.unpluggedIPhoneStderr)
        XCTAssertEqual(
            shown,
            "no iOS devices connected — hint: \(Self.unpluggedIPhoneHint)"
        )
        XCTAssertFalse(shown.contains("\n"), "a status line is one line: \(shown)")
        XCTAssertFalse(shown.hasPrefix("error:"), "the marker is terminal furniture")
    }

    /// The hint is the actionable half and survives; it is just no longer
    /// hard-wrapped at whatever width the CLI's stderr happened to be.
    func test_exited_error_keeps_the_hint() {
        XCTAssertTrue(
            message(exitCode: 8, stderr: Self.unpluggedIPhoneStderr)
                .contains("Trust this Computer"),
            "dropping the hint is the same mistake as showing it raw"
        )
    }

    /// stderr that isn't `_fatal`'s shape reaches the user *whole*.
    ///
    /// The exception line leads because it is the one that says what happened.
    /// The frames under it name the file, the URL, the argument that triggered
    /// it — and most of these banners are the only surface their failure has:
    /// ReposView, TweaksView, SettingsView and ContentView have no log panel
    /// to go and read instead. Reduced to its last line, a crash in
    /// `--add-repo` leaves the diagnostic reachable only inside the
    /// `CLIError.exited` associated value, which no view reads.
    func test_exited_error_keeps_every_frame_of_an_unhandled_traceback() {
        let stderr = """
        Traceback (most recent call last):
          File "/x/graft_ipa/cli.py", line 2423, in _add_repo
            resp = urlopen(url + "/Release")
        KeyError: 'lc_bundle_id'
        """
        XCTAssertEqual(message(exitCode: 1, stderr: stderr), """
        exit 1: KeyError: 'lc_bundle_id'
        Traceback (most recent call last):
          File "/x/graft_ipa/cli.py", line 2423, in _add_repo
            resp = urlopen(url + "/Release")
        """)
    }

    /// The frames are what a user can act on or paste into a report; the
    /// assertion above would still pass if they arrived mangled, so name the
    /// one that matters.
    func test_exited_error_names_the_operation_the_traceback_died_in() {
        let stderr = """
        Traceback (most recent call last):
          File "/x/graft_ipa/cli.py", line 2423, in _add_repo
        KeyError: 'lc_bundle_id'
        """
        XCTAssertTrue(message(exitCode: 1, stderr: stderr).contains("_add_repo"))
    }

    /// `summary` stays one line even where `detail` doesn't: its callers are a
    /// build row's status and a sheet's failure label, which have room for
    /// one. Routing them both through the same function is the change that
    /// would quietly undo either half.
    func test_summary_stays_one_line_where_the_banner_form_does_not() {
        let stderr = """
        Traceback (most recent call last):
          File "/x/graft_ipa/cli.py", line 2423, in _add_repo
        KeyError: 'lc_bundle_id'
        """
        XCTAssertEqual(
            CLIFailure.summary(exitCode: 1, stderr: stderr),
            "exit 1: KeyError: 'lc_bundle_id'"
        )
        XCTAssertTrue(CLIFailure.detail(exitCode: 1, stderr: stderr).contains("\n"))
    }

    /// A non-zero exit that said nothing at all is the one case where the
    /// operation's own name is all there is to report. Rich's trailing newline
    /// still counts as silence.
    func test_exited_error_names_the_operation_when_stderr_is_silent() {
        XCTAssertEqual(message(exitCode: 8, stderr: ""), "Load devices failed")
        XCTAssertEqual(message(exitCode: 8, stderr: "\n  \n"), "Load devices failed")
    }

    /// End to end through a real subprocess: a CLI that fails before it emits
    /// its first event. `events` is empty — so every view driven by
    /// `handle(event:)` has nothing at all — and the reason is reachable only
    /// through `result.stderr`.
    func test_a_run_that_emits_no_events_still_explains_itself() async throws {
        let script = #"""
        printf 'error: no iOS devices connected\n' >&2
        printf "  hint: plug the iPhone in via USB and tap 'Trust this Computer', or enable \n" >&2
        printf 'Wi-Fi sync in Finder\n' >&2
        exit 8
        """#
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let result = try await runner.stream(arguments: ["-c", script]) { _ in }

        XCTAssertEqual(result.exitCode, 8)
        XCTAssertTrue(
            result.events.isEmpty,
            "premise: the failure happened before build-start, so the stream is empty"
        )
        XCTAssertEqual(
            result.failureSummary,
            "no iOS devices connected — hint: \(Self.unpluggedIPhoneHint)"
        )
    }
    /// A cancellation landing *during* launch still kills the child.
    ///
    /// Note where this lives: inside the class, not in the `extension
    /// XCTestCase` further down. It was written into that extension first and
    /// silently never ran — a test method there belongs to no test class, so
    /// XCTest never discovers it. The suite count stayed at 717 and
    /// `CLIRunnerTests` at 39, which is the only thing that gave it away;
    /// `-only-testing` with its exact name still reported "1 test, 0 failures"
    /// in 0.002s, matching nothing and passing vacuously.
    ///
    /// The window between `ProcessHandle.adopt` and `Process.run()` is
    /// microseconds wide, and a cancellation arriving inside it used to be
    /// dropped: `cancel()` only signals a running process, found
    /// `isRunning == false`, and returned — leaving `run()` to start a child
    /// nothing would ever kill. The caller inherited its whole lifetime.
    ///
    /// Consistent with the flake that prompted the fix: `DevicesStoreTests`
    /// cancels a scan running `exec sleep 30` and failed intermittently at
    /// **30.4s** — the full sleep, to the tenth of a second.
    ///
    /// Racing it does not work. An earlier attempt cancelled the `Task`
    /// immediately and finished eight iterations in 0.001s, because that lands
    /// before the body starts, `adopt` refuses, and nothing is forked. So the
    /// runner takes a `willLaunch` seam and this blocks in it, which is the
    /// only way to hold the window open long enough to aim at.
    func test_a_cancellation_inside_the_launch_window_still_kills_the_child() async throws {
        let inWindow = DispatchSemaphore(value: 0)
        let mayProceed = DispatchSemaphore(value: 0)
        let runner = CLIRunner(
            executable: URL(fileURLWithPath: "/bin/sh"),
            willLaunch: {
                inWindow.signal()        // "the handle has adopted; run() is next"
                mayProceed.wait()        // held open until the test has cancelled
            }
        )

        let began = Date()
        let task = Task {
            _ = try? await runner.stream(arguments: ["-c", "exec sleep 30"]) { _ in }
        }

        // Block *this* thread until the runner is genuinely inside the window.
        XCTAssertEqual(inWindow.wait(timeout: .now() + 10), .success,
                       "the runner never reached the launch window")
        task.cancel()
        mayProceed.signal()
        await task.value

        let elapsed = Date().timeIntervalSince(began)
        // The child sleeps 30s and SIGKILL escalation is at +2s, so anything
        // in single digits means the kill landed. Before the fix this waited
        // the full 30.
        XCTAssertLessThan(
            elapsed, 12,
            "waited \(elapsed)s — the child outlived a cancellation that arrived while it launched"
        )
    }
}

/// Wall-clock deadline for anything that talks to a subprocess.
///
/// A pipe-buffer deadlock does not *fail* — it never returns, and the plain
/// `try await` spelling of the test would hang the entire suite until xctest's
/// global timeout rather than reporting which assertion broke. Racing the call
/// against a clock is the only way to turn "never returned" into a failure.
///
/// Shared (rather than duplicated per test file) because the same hazard shows
/// up at both altitudes: `CLIRunner.runOnce` directly, and `ConfigStore.loadApps`
/// through it.
extension XCTestCase {
    /// The call's outcome, or nil if `seconds` elapsed first. The losing task
    /// is cancelled so a child wedged on a full pipe gets SIGTERM'd instead of
    /// leaking into whatever test runs next.
    func finishing<T: Sendable>(
        within seconds: Double,
        _ body: @escaping @Sendable () async throws -> T
    ) async -> Result<T, Error>? {
        await withTaskGroup(of: Result<T, Error>?.self) { group in
            group.addTask {
                do { return .success(try await body()) } catch { return .failure(error) }
            }
            group.addTask {
                try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
                return nil
            }
            let first = await group.next() ?? nil
            group.cancelAll()
            return first
        }
    }


}
