import Foundation

/// Builds the environment for the graft CLI subprocess.
///
/// A GUI app launched from the Dock/Finder inherits only the launchd session
/// environment, whose PATH is `/usr/bin:/bin:/usr/sbin:/sbin`. That omits
/// `/opt/homebrew/bin` (Homebrew: zsign, idevice_id, ideviceinstaller) and
/// `~/.local/bin` (pipx: cyan, graft itself), so the CLI's pre-flight
/// tool checks fail and the build dies right after "build start". We prepend
/// the real tool directories so the subprocess can find them.
enum ProcessEnvironment {
    /// Directories where the CLI's tool dependencies actually live, in the
    /// order they should take precedence.
    static func toolDirs(home: String) -> [String] {
        ["\(home)/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"]
    }

    /// The console width the CLI is told to print at, and why it isn't 80.
    ///
    /// `graft` prints its `error:`/`hint:` pair through Rich, which wraps
    /// to `COLUMNS` — 80 when there is no TTY to ask, which is always the case
    /// for a pipe. That wrap *destroys* the information this process then needs
    /// to put the message back together: Rich's `Text.rstrip_end(width)` crops
    /// the space a soft wrap broke on whenever the divided line lands one
    /// column past the width, so a wrapped word boundary and a token folded
    /// mid-way arrive here looking identical — a line of exactly `width`
    /// characters not ending in whitespace. Nothing downstream can tell them
    /// apart, and measured against the pinned rich 15.0.0, 123 of 500
    /// randomly generated wrapped messages contain at least one word wrap in
    /// that shape.
    ///
    /// So rather than infer where Rich broke a line, don't let it break one.
    /// 512 columns is past the longest message this CLI composes — a sentence
    /// with a filesystem path in it — and Rich pads nothing, so a wide console
    /// costs no bytes: the lines simply arrive whole. `CLIFailure.parse` does
    /// not *depend* on this (an older `graft`, or one whose Rich ignores
    /// the variable, still parses); it is what makes its rejoin exact rather
    /// than merely safe.
    static let consoleColumns = 512

    /// Typer and Click rule their *usage* errors into a box the full width of
    /// the console, and read this variable in preference to `COLUMNS`. Pinned
    /// at 80 so widening the message console doesn't turn a flag the installed
    /// CLI doesn't recognise — which is what a GUI/CLI version skew produces —
    /// into a 512-column horizontal rule, which is the line `summary` would
    /// then quote into the banner as "the last thing it said".
    static let usagePanelColumns = 80

    static func augmented(base: [String: String], home: String) -> [String: String] {
        var env = base
        let inherited = (env["PATH"]).map { $0.split(separator: ":").map(String.init) }
            ?? ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        var seen = Set<String>()
        let merged = (toolDirs(home: home) + inherited).filter { seen.insert($0).inserted }
        env["PATH"] = merged.joined(separator: ":")
        // Overwritten, not defaulted: an inherited COLUMNS (this app launched
        // from a terminal) would otherwise decide how the CLI wraps, making
        // the text a user is shown depend on how they started the app.
        env["COLUMNS"] = String(consoleColumns)
        env["TERMINAL_WIDTH"] = String(usagePanelColumns)
        return env
    }

    /// The environment to hand the CLI subprocess: this process's environment
    /// with the tool directories prepended to PATH.
    static func forCLI() -> [String: String] {
        augmented(
            base: ProcessInfo.processInfo.environment,
            home: FileManager.default.homeDirectoryForCurrentUser.path
        )
    }
}

/// Wraps `Process` for running the graft CLI as a subprocess.
///
/// Two modes, differing in what they do with stdout — *not* in whether they
/// can survive a lot of it. Both drain both pipes concurrently with the child
/// (see `drainToEOF`); neither has an output ceiling.
///   - `runOnce` hands back the raw stdout/stderr text once the child exits.
///     Callers that need bytes the JSON-Lines decoder would discard — the
///     keychain helper's plain-text refusals, `--check`'s output under a
///     non-zero exit — need this one.
///   - `stream` decodes JSON-Lines as they arrive and calls back per event, so
///     a running build can paint its log. It keeps only the decoded events, so
///     it is also the cheaper choice for a very large listing.
/// How long `terminationHandler` waits for the pipe drainers after the child
/// exits, before reporting what it has.
///
/// Not a timeout on the command — the child is already gone by then. It bounds
/// only the wait for EOF, which an orphaned grandchild can withhold forever by
/// holding an inherited write end.
///
/// **Short on purpose, and waiting longer buys nothing.** The obvious reasoning
/// — "make it outlast the +2s SIGKILL escalation so descendants are reaped
/// first" — is wrong: that escalation guards on the *parent* still running, and
/// in this scenario the parent is already dead, so it does nothing. A grace of
/// 3s was measured and still failed 10 of 25 iterations, because it simply
/// pushed the resume past the caller's own budget.
///
/// In the ordinary case EOF arrives at once and this is never reached, so the
/// value is a margin for losing that race under load rather than a figure any
/// output size demands. A child that has exited cannot have been blocked on a
/// write, so it leaves at most one pipe buffer of unread bytes behind: what
/// remains to drain after exit is bounded by pipe capacity, not by how much the
/// command printed. Half a second is orders of magnitude more than reading 64 KB
/// needs.
///
/// **Do not read the truncation guards as evidence for this number.** Both
/// (`test_runOnce_drains_large_stdout_without_deadlock` and the `stream`
/// equivalent) pass identically with the grace set to `0.0` — measured, 20
/// iterations, 0 failures — because a healthy run never reaches the wait. They
/// bound the value from neither side. What constrains it is the *cancellation*
/// path, which always spends it in full: 3s there failed 10 of 25.
///
/// The cost of getting it wrong runs the other way, and
/// `test_a_healthy_run_does_not_spend_the_drain_grace` is what catches it — if
/// a drainer ever stops signalling on EOF, every CLI call the app makes pays
/// this out instead, silently. Note the waits are sequential across both pipes,
/// so that regression costs 2× this, measured at 1.04s.
private let drainGrace: TimeInterval = 0.5

final class CLIRunner {
    struct RunResult {
        let exitCode: Int32
        let stdout: String
        let stderr: String
        /// Events decoded from stdout during `stream(...)`. Always populated for
        /// `stream`, always empty for `runOnce`. Collected synchronously, so a
        /// caller that reads this after `await` is guaranteed to see every event
        /// — independent of when the `onEvent` callback Tasks finish running on
        /// MainActor.
        let events: [BuildEvent]

        init(exitCode: Int32, stdout: String, stderr: String, events: [BuildEvent] = []) {
            self.exitCode = exitCode
            self.stdout = stdout
            self.stderr = stderr
            self.events = events
        }
    }

    enum RunError: Error {
        case launchFailed(underlying: Error)
    }

    private let executable: URL
    private let environment: [String: String]?

    /// Called after the handle adopts the `Process` and before it is launched.
    ///
    /// **A seam, and only a test ever sets it.** The window between those two
    /// steps is microseconds wide and is where a dropped cancellation used to
    /// leak a child (see `ProcessHandle.launched()`); without somewhere to
    /// block, a test cannot get a cancellation into it, and the fix stayed
    /// uncovered. An earlier attempt to hit it by racing finished in 0.001s
    /// having forked nothing at all.
    ///
    /// Default nil, so production builds an empty optional and calls nothing.
    private let willLaunch: (@Sendable () -> Void)?

    init(
        executable: URL,
        environment: [String: String]? = nil,
        willLaunch: (@Sendable () -> Void)? = nil
    ) {
        self.executable = executable
        self.environment = environment
        self.willLaunch = willLaunch
    }

    /// Run a command and hand back everything it wrote.
    ///
    /// Both pipes are drained on background queues *while the child runs*.
    /// This used to read them from `terminationHandler` instead, which is a
    /// deadlock for any child that outgrows the 64 KB kernel pipe buffer: it
    /// blocks in `write(2)`, so it never exits, so the handler that would have
    /// unblocked it never runs. `runOnce` then never returns and the app hangs
    /// with no error and nothing to cancel. `--list` reaches that at ~135 apps
    /// (an `app` line plus its `delivery-state` line is ~480 bytes) and
    /// `--build-log` reaches it on any build that logged more than a screenful.
    func runOnce(arguments: [String], stdin: String? = nil) async throws -> RunResult {
        let proc = Process()
        proc.executableURL = executable
        proc.arguments = arguments
        if let env = environment { proc.environment = env }

        let out = Pipe()
        let err = Pipe()
        proc.standardOutput = out
        proc.standardError = err
        let inPipe: Pipe? = stdin == nil ? nil : Pipe()
        if let inPipe { proc.standardInput = inPipe }

        let outBox = DataBox()
        let errBox = DataBox()
        let outDone = DispatchSemaphore(value: 0)
        let errDone = DispatchSemaphore(value: 0)
        drainToEOF(out.fileHandleForReading, into: outBox, done: outDone)
        drainToEOF(err.fileHandleForReading, into: errBox, done: errDone)

        return try await launch(
            proc, out: out, err: err, inPipe: inPipe, stdin: stdin,
            drained: [outDone, errDone]
        ) { status in
            RunResult(exitCode: status, stdout: outBox.string(), stderr: errBox.string())
        }
    }

    /// The launch protocol both modes share: adopt the process so a cancelled
    /// Task can kill it, run it, feed it `stdin`, and resume only once every
    /// drainer has reached EOF — so the result is complete rather than
    /// whatever happened to have arrived when the child exited.
    ///
    /// The invariant worth stating once: every path that does *not* end with a
    /// running child must close our write ends. The drainers are already
    /// parked in `availableData` on pipes nobody will ever write to, and would
    /// otherwise hold two queue threads for the life of the process.
    private func launch(
        _ proc: Process,
        out: Pipe,
        err: Pipe,
        inPipe: Pipe?,
        stdin: String?,
        drained: [DispatchSemaphore],
        makeResult: @escaping @Sendable (Int32) -> RunResult
    ) async throws -> RunResult {
        let handle = ProcessHandle()
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (cont: CheckedContinuation<RunResult, Error>) in
                func releaseDrainers() {
                    try? out.fileHandleForWriting.close()
                    try? err.fileHandleForWriting.close()
                }
                guard handle.adopt(proc) else {
                    // Cancelled during setup — never launch.
                    releaseDrainers()
                    cont.resume(throwing: CancellationError())
                    return
                }
                proc.terminationHandler = { p in
                    handle.markFinished()
                    let status = p.terminationStatus
                    // Off the termination queue: waiting there would block
                    // Process's own bookkeeping.
                    //
                    // **Bounded, because EOF is not guaranteed.** The claim
                    // this replaced was that `terminate()` closes the child's
                    // ends so the drainers see EOF — true only when the child
                    // has no children. `sh -c 'printf …; sleep 30; …'` forks
                    // `sleep`, which inherits the write ends; SIGTERM kills the
                    // shell and orphans it, so those ends stay open, the
                    // drainers never finish, and this block waits for a process
                    // nothing is going to reap. Measured: that is the
                    // `ConfigStoreTests.test_stopping_a_build_kills_the_
                    // subprocess` flake, which failed 14 times in 25 under
                    // `-test-iterations` — the child exits and the *caller*
                    // hangs.
                    //
                    // A cancelled run has no use for the tail of the output
                    // anyway, so giving up on it is not a loss. The buffers are
                    // lock-protected (`DataBox`, `EventsBox`), so reading them while a
                    // drainer is still appending is safe — it just yields
                    // whatever arrived in time.
                    DispatchQueue.global(qos: .userInitiated).async {
                        for done in drained { _ = done.wait(timeout: .now() + drainGrace) }
                        cont.resume(returning: makeResult(status))
                    }
                }
                do {
                    willLaunch?()
                    try proc.run()
                    // Immediately, and before anything else can await: a
                    // cancellation that arrived while this was starting was
                    // dropped on the floor until now. See `launched()`.
                    let killedAtLaunch = handle.launched()
                    if let inPipe, let stdin, !killedAtLaunch {
                        let writer = inPipe.fileHandleForWriting
                        // Append a newline so the CLI's `sys.stdin.readline()` returns —
                        // without it the subprocess hangs waiting for input.
                        //
                        // Skipped entirely when `launched()` just killed the
                        // child: writing to a pipe whose read end is gone
                        // raises `NSFileHandleOperationException` from this
                        // legacy `write(_:)`, and an ObjC exception is not
                        // catchable from Swift — the app would crash rather
                        // than fail. Only reachable since `launched()` began
                        // killing here, so the fix that closed one hole opened
                        // this one; `setP12Password` and `setSSHPassword` are
                        // the callers that pass stdin.
                        if let data = (stdin + "\n").data(using: .utf8) {
                            writer.write(data)
                        }
                        try? writer.close()
                    } else if let inPipe {
                        try? inPipe.fileHandleForWriting.close()
                    }
                } catch {
                    // The child never started, so terminationHandler won't
                    // fire; resume the throw here.
                    handle.markFinished()
                    releaseDrainers()
                    cont.resume(throwing: RunError.launchFailed(underlying: error))
                }
            }
        } onCancel: {
            handle.cancel()
        }
    }
}

/// A `graft` invocation that didn't produce usable output.
///
/// Replaces the `NSError(domain: "<StoreName>", code:)` every store used to
/// build by hand. Two things came out of that: the domain was the Swift type
/// name, which no caller ever read, and the exit status was recoverable only
/// as an untyped `code` on a bridged error — so the one place that cares about
/// a *specific* status (a `--delete-tweak` the CLI refuses because the tweak
/// is still attached, `CLIContract.Exit.tweakInUse`) had no way to ask except
/// by matching prose in the message.
enum CLIError: LocalizedError {
    /// The process exited non-zero. `doing` names the operation, and is used
    /// only for the message the CLI itself declined to provide.
    case exited(doing: String, code: Int32, stderr: String)
    /// The process exited 0, but what it reported back can't be used — a
    /// truncated event stream, or a failure reported inside the stream.
    case reported(String)

    /// One line, for the status banners and alerts that render
    /// `error.localizedDescription` — which is most of them.
    ///
    /// This used to hand back `stderr` verbatim, and `CLIFailure` was wired
    /// into exactly two views (AppDetailView and ReinstallSheet) that ask for
    /// it by name. Every other caller — the Devices, Repos, Tweaks and Apps
    /// banners, all of which reach the CLI through `CLIStore.mutate` or a
    /// store's `catch` — got Rich's terminal output pasted into a SwiftUI
    /// `Text`: an `error:` prefix written for a terminal, a `hint:` line
    /// written for someone with a shell prompt under it, and 80-column hard
    /// wraps that a resizable window re-wraps a second time.
    ///
    /// `CLIFailure.detail` is the same reflow those two views already used for
    /// `_fatal`'s shape. Routing it here means every caller gets it without
    /// knowing it exists, and the two that want the log form still ask for
    /// `logText`.
    ///
    /// `detail` rather than `summary` because most of these banners are the
    /// *only* surface a store's failure has: ReposView, TweaksView, SettingsView
    /// and ContentView have no log panel to fall back to, so a diagnostic this
    /// drops is a diagnostic the user cannot reach at all.
    var errorDescription: String? {
        switch self {
        case let .exited(doing, code, stderr):
            // The CLI's own words win; the label is the fallback for a
            // non-zero exit that said nothing at all. Tested against
            // `stderrLines` rather than `stderr.isEmpty` so that a stderr of
            // nothing but Rich's trailing newline still counts as silence,
            // instead of being reported as a bare "exit 8".
            guard !CLIFailure.stderrLines(stderr).isEmpty else {
                return "\(doing) failed"
            }
            return CLIFailure.detail(exitCode: code, stderr: stderr)
        case let .reported(message):
            return message
        }
    }

    /// The CLI's exit status, or nil when the failure wasn't one.
    var exitCode: Int32? {
        if case let .exited(_, code, _) = self { return code }
        return nil
    }
}

// MARK: - Reading a failure off stderr

/// What `graft` wrote to stderr, turned into something a status line can
/// show.
///
/// This exists because a CLI invocation can fail *before* it emits a single
/// JSON-Lines event: `--reinstall-build` with the iPhone unplugged raises
/// `InstallError("no iOS devices connected", hint: "plug the iPhone in …")`
/// ahead of `build-start`, so a view driven only by `handle(event:)` has
/// nothing to render and falls back to "exit 8". The one thing that *does*
/// carry the reason is `result.stderr`, and every view used to discard it.
/// Same for "archived IPA missing", "app is no longer in config.yaml" and
/// "build N did not produce a signed IPA".
///
/// Parsed rather than shown raw, for one reason: the CLI prints through Rich,
/// which hard-wraps to `COLUMNS` when stderr isn't a TTY. Taking "the line
/// that starts with `error:`" would cut a long message mid-sentence — the same
/// class of mistake as dropping it. So the markers are treated as the *start*
/// of a block and the continuation lines are re-joined.
///
/// The rejoin is a single space, unconditionally. It used to try to tell a
/// word-boundary wrap from a token folded mid-way and fuse the second kind
/// with no separator, which cannot be done: Rich's `Text.rstrip_end` crops the
/// space off a wrap that lands on the width, so both arrive as a
/// width-length line ending in a non-space. See
/// `ProcessEnvironment.consoleColumns`, which is how a folded token is
/// avoided instead — by giving Rich a console wide enough that it has nothing
/// to break.
///
/// Nothing is thrown away in the process. `summary` is the one-line form for a
/// status; `logText` is what a log panel shows, and it falls back to stderr
/// verbatim — indentation, traceback frames and all — the moment the text
/// isn't a shape this understands.
enum CLIFailure {
    /// The `error:` / `hint:` pair `_fatal` prints, reflowed.
    struct Message: Equatable {
        let error: String
        let hint: String?

        /// Both halves on one line. The hint is the actionable half — "plug
        /// the iPhone in via USB and tap 'Trust this Computer'" is the whole
        /// point of surfacing this — so it is never dropped for brevity.
        var summary: String {
            guard let hint, !hint.isEmpty else { return error }
            return "\(error) — hint: \(hint)"
        }
    }

    private static let errorMarker = "error:"
    private static let hintMarker = "hint:"

    /// stderr, line by line: trailing whitespace stripped and blank lines
    /// dropped, but leading indentation kept — it is what makes a traceback
    /// readable.
    static func stderrLines(_ stderr: String) -> [String] {
        stderr.split(separator: "\n", omittingEmptySubsequences: false)
            .map(trimmingTrailingWhitespace)
            .filter { !$0.isEmpty }
    }

    /// The `error:`/`hint:` pair, or nil when stderr isn't one — a traceback,
    /// a subprocess's own chatter, or nothing at all.
    ///
    /// Continuation lines rejoin with one space, and *only* the block's own
    /// lines take part: anything ahead of the first marker (a urllib3
    /// `NotOpenSSLWarning`, a pymobiledevice3 log line, a traceback frame)
    /// belongs to neither block and cannot influence either.
    ///
    /// That "one space" used to be a decision — a line reaching the widest
    /// observed width without trailing whitespace was read as a token Rich had
    /// folded mid-way and fused with no separator, so a path came back
    /// copyable. It is not a decision that can be made from this text. Rich's
    /// `Text.rstrip_end(width)` crops the space a soft wrap broke on when the
    /// divided line lands one past the width, so an ordinary word wrap
    /// produces exactly the shape a fold does, and the fuse turns
    /// `…expired on 2026-01-14 at noon…` into `…at noon…` run together as
    /// `atnoon`. Against rich 15.0.0 that is 123 of 500 random wrapped
    /// messages, and it reached every banner in the app.
    ///
    /// The folded path is answered upstream instead, by
    /// `ProcessEnvironment.consoleColumns` giving Rich a console it has no
    /// reason to break a line in. When that doesn't take — an older CLI, a
    /// Rich that ignores `COLUMNS` — the cost is a space inside a long token,
    /// which is visible and still carries every character. Fusing the wrong
    /// pair is neither.
    static func parse(stderr: String) -> Message? {
        // Which block a continuation line belongs to is not tracked separately:
        // it is whichever of these was opened last, and the two markers can
        // only be opened in this order, so `hint` being non-nil *is* "we are in
        // the hint". A `var field` alongside them was a third variable that had
        // to agree with the other two, in the one function here nobody wants to
        // debug twice.
        var error: String?
        var hint: String?

        for line in stderrLines(stderr) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if error == nil, let rest = remainder(of: trimmed, after: errorMarker) {
                error = rest
            } else if error != nil, hint == nil,
                      let rest = remainder(of: trimmed, after: hintMarker) {
                hint = rest
            } else if let open = hint {
                hint = joined(open, trimmed)
            } else if let open = error {
                // A wrapped continuation of the error block. While `error` is
                // still nil this branch isn't taken at all, which is how
                // anything ahead of the first marker is dropped from the
                // summary. `stderrLines` still has it, and `logText` shows it.
                error = joined(open, trimmed)
            }
        }

        guard let error, !error.isEmpty else { return nil }
        return Message(error: error, hint: hint)
    }

    private static func joined(_ text: String, _ part: String) -> String {
        guard !text.isEmpty else { return part }
        guard !part.isEmpty else { return text }
        return text + " " + part
    }

    /// The one line to put in front of the user for a non-zero exit that the
    /// event stream never explained.
    ///
    /// The parsed form deliberately omits the exit code: "no iOS devices
    /// connected — hint: plug the iPhone in…" already says everything the user
    /// can act on, and a number in front of it is noise. The unparsed
    /// fallbacks keep it, because there the text may be a fragment of
    /// something else entirely and the status is the only thing we're sure of.
    static func summary(exitCode: Int32, stderr: String) -> String {
        if let parsed = parse(stderr: stderr) { return parsed.summary }
        // No marker: most likely a traceback, whose last line is the exception
        // — the informative one. The rest is in the log panel. Its indentation
        // goes with it: leading space is what makes a traceback readable in
        // the log and what makes a one-line summary look broken.
        if let last = stderrLines(stderr).last {
            return "exit \(exitCode): \(last.trimmingCharacters(in: .whitespaces))"
        }
        return "exit \(exitCode)"
    }

    /// What an error *banner* shows: `summary`'s headline, and — when stderr
    /// wasn't `_fatal`'s shape — the rest of stderr under it, verbatim.
    ///
    /// `summary` is a single line because its callers are single-line: a build
    /// row's status, a sheet's failure label. `errorDescription` is not. It
    /// reaches `ErrorBanner`, which is a selectable, self-sizing block of text,
    /// and reducing an unhandled exception to its final line there means the
    /// frames naming the file, URL or argument that triggered it exist only
    /// inside a `CLIError.exited` associated value that no view reads. A user
    /// who hits a crash in `--add-repo` then has nothing to copy into a bug
    /// report but "KeyError: 'lc_bundle_id'".
    ///
    /// The exception line leads, because it is the one line that says what
    /// happened; the frames follow in their original order, indentation
    /// included. The last line isn't repeated at the bottom — it is the
    /// headline.
    ///
    /// `_fatal`'s own shape is unchanged: it is already a whole sentence plus
    /// its hint, `logText` says the same reflow is lossless, and a second copy
    /// under it would just be the same sentence with hard wraps in it.
    static func detail(exitCode: Int32, stderr: String) -> String {
        if let parsed = parse(stderr: stderr) { return parsed.summary }
        let raw = stderrLines(stderr)
        guard let last = raw.last else { return "exit \(exitCode)" }
        let headline = "exit \(exitCode): \(last.trimmingCharacters(in: .whitespaces))"
        return ([headline] + raw.dropLast()).joined(separator: "\n")
    }

    /// The lines a build-log panel should show for the same failure.
    ///
    /// For `_fatal`'s shape that is the reflowed error and hint and nothing
    /// else: the reflow is lossless, so repeating the raw text underneath
    /// would print the same sentence a second time with 80-column breaks in
    /// it. For anything else — a traceback, a tool's own diagnostics — it is
    /// stderr verbatim, because nothing here knows which part of that matters.
    static func logText(exitCode: Int32, stderr: String) -> [String] {
        if let parsed = parse(stderr: stderr) {
            var lines = ["✗ \(parsed.error)"]
            if let hint = parsed.hint { lines.append("  hint: \(hint)") }
            return lines
        }
        let raw = stderrLines(stderr)
        guard !raw.isEmpty else {
            return ["✗ exit \(exitCode) — the CLI exited without saying why"]
        }
        return ["✗ exit \(exitCode)"] + raw
    }

    private static func remainder(of line: String, after marker: String) -> String? {
        guard line.hasPrefix(marker) else { return nil }
        return String(line.dropFirst(marker.count)).trimmingCharacters(in: .whitespaces)
    }

    private static func trimmingTrailingWhitespace(_ line: Substring) -> String {
        var end = line.endIndex
        while end > line.startIndex, line[line.index(before: end)].isWhitespace {
            end = line.index(before: end)
        }
        return String(line[line.startIndex..<end])
    }
}

extension CLIRunner.RunResult {
    /// What to show when this run exited non-zero and the event stream said
    /// nothing about why.
    var failureSummary: String {
        CLIFailure.summary(exitCode: exitCode, stderr: stderr)
    }
}

// MARK: - Running one command

/// The shape shared by every one-shot CLI call in the Services layer: run the
/// command, refuse to interpret a non-zero exit as data, then decode stdout as
/// JSON-Lines events and keep the ones the caller asked for.
///
/// Written out at each call site this was four lines of `guard exitCode == 0
/// else throw NSError(...)` plus a `compactMap` with an inline `if case`, and
/// the copies had already drifted: some supplied a fallback message for an
/// empty stderr and some threw a blank one.
extension CLIRunner {
    /// Run a one-shot command and decode its stdout.
    /// Throws `CLIError.exited` if the CLI exits non-zero.
    func runEvents(_ args: [String], failing doing: String) async throws -> [BuildEvent] {
        let result = try await runOnce(arguments: args)
        try Self.checkExit(result.exitCode, stderr: result.stderr, doing: doing)
        return result.stdout.buildEvents()
    }

    /// As `runEvents`, keeping every event `pick` matches.
    func runCollecting<T>(
        _ args: [String], failing doing: String, pick: (BuildEvent) -> T?
    ) async throws -> [T] {
        try await runEvents(args, failing: doing).compactMap(pick)
    }

    /// As `runEvents`, keeping the first event `pick` matches — nil when the
    /// CLI succeeded without emitting one.
    func run<T>(
        _ args: [String], failing doing: String, pick: (BuildEvent) -> T?
    ) async throws -> T? {
        try await runCollecting(args, failing: doing, pick: pick).first
    }

    /// `runEvents` for output that is large *and* discardable.
    ///
    /// This is no longer about deadlock — `runOnce` drains concurrently too,
    /// so both survive any volume. What's left is a memory difference: this
    /// decodes each line as it arrives and never materialises the raw stdout
    /// string, so a repo listing costs one copy instead of two.
    ///
    /// Cancellation is checked before the exit status on purpose: a caller
    /// replaced by a newer `.task(id:)` should unwind quietly rather than
    /// leave an error banner behind for work nobody is waiting on.
    ///
    /// `onEvent` sees each event as it arrives, for a caller that shows
    /// progress (the App Store get's Downloading… / Decrypting…). Those calls
    /// are hops onto the MainActor and can land *after* this returns, so the
    /// result is still read from `result.events`, and a caller whose progress
    /// state must not outlive the call has to guard against a late one.
    func streamEvents(
        _ args: [String],
        failing doing: String,
        onEvent: @escaping @MainActor (BuildEvent) -> Void = { _ in }
    ) async throws -> [BuildEvent] {
        // Read `result.events` rather than the onEvent callback so we don't
        // race the trailing MainActor Tasks scheduled from terminationHandler.
        let result = try await stream(arguments: args, onEvent: onEvent)
        try Task.checkCancellation()
        try Self.checkExit(result.exitCode, stderr: result.stderr, doing: doing)
        return result.events
    }

    private static func checkExit(_ code: Int32, stderr: String, doing: String) throws {
        guard code != 0 else { return }
        throw CLIError.exited(doing: doing, code: code, stderr: stderr)
    }
}

// MARK: - Running a mutating command

/// Refuse to report success for a field the CLI did not say it applied.
///
/// Both writers that send a set of fields and get a `changed` list back —
/// `--set-app-options` and `--set-settings` — need this, and had it written out
/// twice down to the message. It exists because a silently-dropped field is
/// indistinguishable from a saved one from the pane's side, and the user then
/// trusts a value that is not on disk.
func requireApplied(_ requested: Set<String>, reportedIn changed: [String]) throws {
    let dropped = requested.subtracting(changed)
    guard dropped.isEmpty else {
        throw CLIError.reported(
            "graft didn't apply: " + dropped.sorted().joined(separator: ", ")
        )
    }
}

/// A store that reads a list out of the CLI and can be asked to re-read it.
@MainActor
protocol CLIStore: AnyObject {
    var runner: CLIRunner { get }
    var lastError: String? { get set }
    /// Re-read this store's collection from the CLI, replacing what it holds.
    func reload() async throws
}

extension CLIStore {
    /// Run a *mutating* CLI call, then reload.
    ///
    /// The two rules a new verb should not have to re-derive by reading
    /// whichever neighbour it was pasted next to:
    ///
    ///  - A call that writes config.yaml reloads. One that doesn't goes to the
    ///    runner directly and does not reload, so it can't stomp a list it
    ///    never changed: `--cached-ipa` through `runner.run(...)`, and
    ///    `--provide-ipa` through `runner.streamEvents(...)` — it writes only
    ///    into cache_dir, and streams so a drop the CLI decrypts can report
    ///    its stage.
    ///  - A reload failure never throws. The write already landed on disk, so
    ///    rethrowing here would make the view report failure for a change that
    ///    succeeded; it goes to `lastError` instead, which is exactly the
    ///    "it worked, the list is stale" story that's true.
    ///
    /// - Parameters:
    ///   - doing: names the operation in the error for a non-zero exit with an
    ///     empty stderr, e.g. `"Remove app"` → "Remove app failed".
    ///   - succeeded: names it in the past tense for the reload-failed note,
    ///     e.g. `"App removed"` → "App removed, but couldn't refresh the list: …".
    func mutate<T>(
        _ args: [String],
        failing doing: String,
        succeeded: String,
        pick: (BuildEvent) -> T?
    ) async throws -> T? {
        let record = try await runner.run(args, failing: doing, pick: pick)
        do {
            try await reload()
        } catch {
            lastError = "\(succeeded), but couldn't refresh the list: "
                + error.localizedDescription
        }
        return record
    }
}

extension CLIRunner {
    /// Run the command and call `onEvent` for each JSON-Lines event on stdout.
    /// Returns when the subprocess exits.
    func stream(
        arguments: [String],
        onEvent: @escaping @MainActor (BuildEvent) -> Void
    ) async throws -> RunResult {
        let proc = Process()
        proc.executableURL = executable
        proc.arguments = arguments
        if let env = environment { proc.environment = env }

        let out = Pipe()
        let err = Pipe()
        proc.standardOutput = out
        proc.standardError = err

        // stdout gets its own loop rather than `drainToEOF` because it splits
        // and decodes as it goes — that incremental delivery is the whole
        // point of this mode. The blocking-read discipline is the same, and
        // so is the reason for it.
        let buffer = LineBuffer()
        let eventsBox = EventsBox()
        let stderrBox = DataBox()
        let outDone = DispatchSemaphore(value: 0)
        let errDone = DispatchSemaphore(value: 0)

        DispatchQueue.global(qos: .userInitiated).async {
            let handle = out.fileHandleForReading
            while true {
                let chunk = handle.availableData
                if chunk.isEmpty { break }
                buffer.append(chunk)
                let lines = buffer.drainCompleteLines()
                for line in lines {
                    if let event = try? BuildEvent.decode(line: line) {
                        eventsBox.append(event)
                        Task { @MainActor in onEvent(event) }
                    }
                }
            }
            let remainder = buffer.flushRemainder()
            if !remainder.isEmpty,
               let event = try? BuildEvent.decode(line: remainder) {
                eventsBox.append(event)
                Task { @MainActor in onEvent(event) }
            }
            outDone.signal()
        }

        drainToEOF(err.fileHandleForReading, into: stderrBox, done: errDone)

        return try await launch(
            proc, out: out, err: err, inPipe: nil, stdin: nil,
            drained: [outDone, errDone]
        ) { status in
            RunResult(
                exitCode: status,
                stdout: "",
                stderr: stderrBox.string(),
                events: eventsBox.snapshot()
            )
        }
    }
}

/// Copy `handle` into `box` until EOF on a background queue, then signal `done`.
///
/// Must be started *before* the child can fill the pipe. The kernel buffer is
/// 64 KB; a child that fills it blocks in `write(2)` until somebody reads, so
/// draining only after the child exits is a deadlock — the child can't exit.
///
/// A blocking `availableData` loop rather than `readabilityHandler`: setting
/// the handler before `Process.run()` is documented as supported but in
/// practice can simply never fire for short-lived high-throughput children,
/// and mixing it with `readDataToEndOfFile` is racy. A plain blocking-read
/// loop on its own queue is the reliable pattern.
private func drainToEOF(_ handle: FileHandle, into box: DataBox, done: DispatchSemaphore) {
    DispatchQueue.global(qos: .userInitiated).async {
        while true {
            let chunk = handle.availableData
            if chunk.isEmpty { break }
            box.append(chunk)
        }
        done.signal()
    }
}

/// Owns a spawned `Process` so a cancelled Task can actually stop it.
///
/// `Process` on its own isn't enough. A cancellation handler runs on an
/// arbitrary thread and can arrive at any point: before `run()`, mid-flight,
/// or after the child already exited and was reaped. Calling `terminate()` on
/// a process that was never launched traps, and signalling a dead pid risks
/// hitting whatever the OS assigned that number next. This serializes those
/// three states behind one lock so each case is handled explicitly.
private final class ProcessHandle: @unchecked Sendable {
    private let lock = NSLock()
    private var process: Process?
    private var finished = false
    private var cancelled = false
    /// Whether a stop has already been issued for this process.
    ///
    /// `cancel()` and `launched()` can both decide to kill: cancel can run
    /// entirely between `run()` returning and `launched()` taking the lock, and
    /// then both see `cancelled && !finished` and the same non-nil `process`.
    /// That ran the whole of `stop()` twice — two descendant walks, two
    /// `terminate()`s, and two SIGKILL timers, doubling the exposure of the
    /// escalation and signalling a pid Foundation may already have reaped.
    ///
    /// **Set only when a stop actually signalled**, never merely when one was
    /// attempted. Setting it on the attempt reintroduces the leak this whole
    /// mechanism exists to prevent: in the launch window `cancel()` finds a
    /// non-nil `process` that is not yet running, `stop()` returns having done
    /// nothing, and `launched()` then skips — so the child starts unkillable
    /// again. Caught immediately by
    /// `test_a_cancellation_inside_the_launch_window_still_kills_the_child`,
    /// which failed at 30.02s, the child's whole sleep.
    private var stopIssued = false

    /// Take ownership just before launching. Returns false when cancellation
    /// already arrived, in which case the caller must not launch at all —
    /// otherwise a Task cancelled during setup still spawns a subprocess that
    /// nobody is waiting on.
    func adopt(_ proc: Process) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard !cancelled else { return false }
        process = proc
        return true
    }

    /// Called from `terminationHandler` (or the launch-failure path) before
    /// the continuation resumes, so a late cancel can't signal a reaped pid.
    func markFinished() {
        lock.lock(); defer { lock.unlock() }
        finished = true
        process = nil
    }

    func cancel() {
        lock.lock()
        cancelled = true
        let victim = (finished || stopIssued) ? nil : process
        lock.unlock()
        if Self.stop(victim) { markStopped() }
    }

    /// Called immediately after `Process.run()` succeeds.
    ///
    /// **Closes the window between `adopt` and `run`.** `cancel()` can only
    /// signal a process that has actually started, so a cancellation arriving
    /// after the handle adopted the `Process` but before it was launched found
    /// `isRunning == false`, returned without doing anything, and left `run()`
    /// to start a child nothing would ever kill. The caller then waited for a
    /// child that outlived its own cancellation.
    ///
    /// The window is real by inspection — `cancel()` returns early on
    /// `!isRunning`, and nothing between `adopt` and `run` makes that true —
    /// and it fits the observed flake: `DevicesStoreTests` cancels a scan
    /// running `exec sleep 30`, and the intermittent failure was a **30.4s**
    /// test, the full sleep to the tenth of a second, under parallel load.
    ///
    /// **And it is covered, after two attempts that were not.** Racing it does
    /// not work: cancelling the `Task` immediately lands *before* the body
    /// starts, so `adopt` refuses and nothing is forked — eight iterations
    /// spawning `sleep 30` finished in 0.001s and stayed green with this call
    /// deleted. That test was thrown away rather than kept.
    ///
    /// `CLIRunnerTests.test_a_cancellation_inside_the_launch_window_still_
    /// kills_the_child` holds the window open through the runner's
    /// `willLaunch` seam and cancels inside it. Delete this call and it fails
    /// in **30.02s** — the child's whole sleep, which is the same shape as the
    /// 30.4s flake that started this.
    /// Returns whether it actually killed the child, so the caller can skip
    /// writing to a pipe whose read end is now gone.
    @discardableResult
    func launched() -> Bool {
        lock.lock()
        let victim = (cancelled && !finished && !stopIssued) ? process : nil
        lock.unlock()
        let killed = Self.stop(victim)
        if killed { markStopped() }
        return killed
    }

    private func markStopped() {
        lock.lock(); defer { lock.unlock() }
        stopIssued = true
    }

    /// Returns whether it actually signalled anything.
    @discardableResult
    private static func stop(_ victim: Process?) -> Bool {
        guard let victim, victim.isRunning else { return false }
        // Descendants first, and while the parent is still alive to be walked:
        // once it is reaped its children are re-parented to launchd and there
        // is nothing left to enumerate from.
        let orphans = descendants(of: victim.processIdentifier)
        victim.terminate()  // SIGTERM: let the CLI unwind and clean up temp files.
        for pid in orphans { kill(pid, SIGTERM) }

        // zsign and cyan spend long stretches in tight native loops and can be
        // slow to notice a signal, and the whole point of cancelling is that
        // the work is no longer wanted. Escalate rather than leaving the CPU
        // spinning on output nobody will read. `isRunning` is re-checked under
        // Process's own bookkeeping, which holds the pid until the child is
        // reaped, so this can't signal a recycled pid.
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 2) {
            // Re-walked here, never the two-second-old `orphans` snapshot.
            // Those pids are very likely dead and reaped by now, and a reaped
            // pid is free to be handed to something else — on a machine forking
            // hundreds of short-lived processes a second, SIGKILLing a stale
            // pid list is a lottery ticket on someone else's shell. Re-walking
            // from a `victim` Foundation still owns is the only form of this
            // that cannot signal a stranger.
            //
            // The cost is honest: if `victim` has already exited, its children
            // are re-parented to launchd and unreachable from here, so they get
            // the SIGTERM they were already sent and nothing more. Leaking a
            // process that ignores SIGTERM is a smaller harm than killing an
            // unrelated one.
            guard victim.isRunning else { return }
            for pid in descendants(of: victim.processIdentifier) {
                kill(pid, SIGKILL)
            }
            kill(victim.processIdentifier, SIGKILL)
        }
        return true
    }

    /// Every descendant of `pid`, deepest last.
    ///
    /// **Signalling the child alone is not enough, and the failure is silent.**
    /// A `graft` invocation spawns `cyan` and `zsign`; terminating the
    /// CLI leaves those running, still working and still holding the write end
    /// of the pipe this runner is draining. No EOF arrives, so `stream` never
    /// returns and the caller waits out work it already cancelled.
    ///
    /// Measured directly: `/bin/sh -c 'printf …; sleep 30; …'` SIGTERMed at the
    /// shell leaves `sleep` alive and re-parented. That is exactly the shape of
    /// `ConfigStoreTests.test_stopping_a_build_kills_the_subprocess`, whose
    /// intermittent failure was the stream never returning inside 5s.
    ///
    /// Best effort by construction: a pid can exit between listing and
    /// signalling, and `kill` on a dead pid is a harmless ESRCH. Nothing here
    /// waits on the result.
    ///
    /// **And best effort is not the same as complete.** A child that has not
    /// forked its own children *yet* has nothing to enumerate, so a
    /// cancellation arriving in that window still orphans whatever the child
    /// spawns next. Measured: `ConfigStoreTests`'
    /// `test_stopping_a_build_kills_the_subprocess` cancels on the shell's
    /// first line of output — before it has forked `sleep` — and still leaves
    /// a stray process behind. That test fails **14 times in 25** under
    /// `-test-iterations`, with and without this method, so this is not what
    /// makes it flaky and removing it will not make it worse.
    ///
    /// Closing that window needs the child in its own process group so a
    /// single `kill(-pgid)` reaches everything regardless of timing, and
    /// `Foundation.Process` exposes no way to ask for one.
    private static func descendants(of pid: pid_t) -> [pid_t] {
        var found: [pid_t] = []
        var frontier = [pid]
        // Bounded rather than recursive-until-empty: a pid table that changes
        // under us must not be able to spin this forever.
        var rounds = 0
        while !frontier.isEmpty, rounds < 8 {
            rounds += 1
            var next: [pid_t] = []
            for parent in frontier {
                let count = proc_listchildpids(parent, nil, 0)
                guard count > 0 else { continue }
                var kids = [pid_t](repeating: 0, count: Int(count))
                let bytes = Int32(Int(count) * MemoryLayout<pid_t>.size)
                let got = proc_listchildpids(parent, &kids, bytes)
                guard got > 0 else { continue }
                next.append(contentsOf: kids.prefix(Int(got)).filter { $0 > 0 })
            }
            found.append(contentsOf: next)
            frontier = next
        }
        return found
    }
}

private final class LineBuffer: @unchecked Sendable {
    private var pending = Data()
    private let lock = NSLock()

    func append(_ data: Data) {
        lock.lock(); defer { lock.unlock() }
        pending.append(data)
    }

    func drainCompleteLines() -> [String] {
        lock.lock(); defer { lock.unlock() }
        var out: [String] = []
        while let nlIdx = pending.firstIndex(of: 0x0a) {
            let lineData = pending.subdata(in: pending.startIndex..<nlIdx)
            if let s = String(data: lineData, encoding: .utf8) {
                out.append(s)
            }
            pending.removeSubrange(pending.startIndex...nlIdx)
        }
        return out
    }

    func flushRemainder() -> String {
        lock.lock(); defer { lock.unlock() }
        defer { pending.removeAll() }
        return String(data: pending, encoding: .utf8) ?? ""
    }
}

/// Thread-safe byte accumulator for a drained pipe. Written on the drainer's
/// GCD queue, read on the queue that resumes the continuation.
private final class DataBox: @unchecked Sendable {
    private var data = Data()
    private let lock = NSLock()

    func append(_ chunk: Data) {
        lock.lock(); defer { lock.unlock() }
        data.append(chunk)
    }

    func string() -> String {
        lock.lock(); defer { lock.unlock() }
        return String(data: data, encoding: .utf8) ?? ""
    }
}

/// Thread-safe ordered accumulator for events decoded by `stream(...)`.
/// Writes happen on the read pipe's GCD queue and on the terminationHandler
/// queue; reads happen on the caller's actor after `await` resumes.
private final class EventsBox: @unchecked Sendable {
    private var events: [BuildEvent] = []
    private let lock = NSLock()

    func append(_ event: BuildEvent) {
        lock.lock(); defer { lock.unlock() }
        events.append(event)
    }

    func snapshot() -> [BuildEvent] {
        lock.lock(); defer { lock.unlock() }
        return events
    }
}
