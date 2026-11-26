import Foundation

/// Builds the environment for the graft CLI subprocess.
///
/// A GUI app launched from the Dock/Finder inherits only the launchd session
/// environment, whose PATH is `/usr/bin:/bin:/usr/sbin:/sbin`. That omits
/// `/opt/homebrew/bin` (Homebrew: zsign, ideviceinstaller, iproxy) and
/// `~/.local/bin` (pipx: cyan, graft itself), so the CLI's pre-flight
/// tool checks fail and the build dies right after "build start". We prepend
/// the real tool directories so the subprocess can find them.
enum ProcessEnvironment {
    /// Directories where the CLI's tool dependencies actually live, in the
    /// order they should take precedence: pipx, Homebrew on Apple silicon,
    /// Homebrew on Intel, MacPorts (which packages libimobiledevice too).
    static func toolDirs(home: String) -> [String] {
        ["\(home)/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"]
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
    /// CLI doesn't recognize — which is what a GUI/CLI version skew produces —
    /// into a 512-column horizontal rule, which is the line `summary` would
    /// then quote into the banner as "the last thing it said".
    static let usagePanelColumns = 80

    /// Variables that make Rich (and Typer, which wraps it) write color
    /// codes into a pipe. `FORCE_COLOR` is a common export — npm, jest and
    /// CI setups set it — and the login-shell merge used to hand it to the
    /// CLI: stderr then arrived as `\e[31merror:\e[0m …`, `CLIFailure.parse`
    /// found no line starting with `error:`, the setup screen was lost to an
    /// alert, and every banner showed raw escape codes.
    ///
    /// Rich reads `TTY_COMPATIBLE` and `TTY_INTERACTIVE` the same way, and
    /// Typer forces a terminal on `PY_COLORS`. `CLICOLOR_FORCE` is the BSD
    /// spelling, honoured by tools the CLI runs. None says anything about the
    /// user's setup that a subprocess with no terminal should act on.
    static let colourVariables: Set<String> = [
        "FORCE_COLOR", "TTY_COMPATIBLE", "TTY_INTERACTIVE", "PY_COLORS", "CLICOLOR_FORCE",
    ]

    /// Whether `name` is a Python interpreter variable — `PYTHONPATH`,
    /// `PYTHONHOME`, `PYTHONSTARTUP`, `PYTHONEXECUTABLE`, `PYTHONUSERBASE` and
    /// the rest of the family.
    ///
    /// The CLI is a Python program in its own pipx venv, and every one of
    /// these changes what that interpreter runs: a `PYTHONPATH` the user set
    /// for some other project puts that project's modules ahead of the CLI's
    /// own dependencies, `PYTHONHOME` points the interpreter at another
    /// standard library, `PYTHONSTARTUP` and `PYTHONUSERBASE` run or import
    /// code the CLI never chose. Terminal users are exposed to that too, but
    /// a Terminal user can see their own exports; the app merging them in
    /// from a login shell is invisible. Dropped from every source, not only
    /// the shell's: a `PYTHONPATH` in the app's own environment (started from
    /// a terminal) is the same hazard.
    static func isPythonVariable(_ name: String) -> Bool {
        name.hasPrefix("PYTHON")
    }

    static func augmented(base: [String: String], home: String) -> [String: String] {
        var env = base.filter { name, _ in
            !colourVariables.contains(name) && !isPythonVariable(name)
        }
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
        ensureUTF8(&env)
        return env
    }

    /// The locale the CLI's output is encoded in, made UTF-8 when it isn't.
    ///
    /// Everything this app reads from the CLI is decoded as UTF-8, and the CLI
    /// prints `✓`, `✗` and `…`. From launchd there is no locale at all, and
    /// Python coerces that C locale to UTF-8 on its own. The login shell's
    /// environment can bring a real one, and a non-UTF-8 one there would encode
    /// every one of those characters into something this side cannot read.
    ///
    /// Only the character-type category is touched, and only when the
    /// effective one (`LC_ALL`, then `LC_CTYPE`, then `LANG`) does not already
    /// name UTF-8 — so a user's own language, dates and number formats reach
    /// the CLI as they set them.
    static let utf8Locale = "en_US.UTF-8"

    private static func ensureUTF8(_ env: inout [String: String]) {
        func set(_ name: String) -> String? { env[name].flatMap { $0.isEmpty ? nil : $0 } }
        let effective = set("LC_ALL") ?? set("LC_CTYPE") ?? set("LANG")
        let normalized = effective?.uppercased().replacingOccurrences(of: "-", with: "")
        guard normalized?.contains("UTF8") != true else { return }
        if set("LC_ALL") != nil {
            env["LC_ALL"] = utf8Locale
        } else {
            env["LC_CTYPE"] = utf8Locale
        }
    }

    /// This process's environment with the login shell's laid over it.
    ///
    /// The shell's value wins where both have one: the shell inherits this
    /// process's environment before its startup files run, so anything it
    /// reports differently is something the user's setup changed on purpose.
    /// `LoginShell.noise` — the shell describing itself — is left out.
    ///
    /// Nil `shell` (no answer, or a failed one) returns `process` unchanged,
    /// which is how the app behaved before it asked.
    static func merged(process: [String: String], shell: [String: String]?) -> [String: String] {
        guard let shell else { return process }
        var env = process
        for (name, value) in shell where !LoginShell.noise.contains(name) {
            env[name] = value
        }
        return env
    }

    /// The environment to hand the CLI subprocess: this process's environment,
    /// the login shell's over it when there is one, and this app's own
    /// augmentation over both — the tool directories on PATH, the console
    /// widths, a UTF-8 locale.
    static func forCLI(
        shell: [String: String]? = nil,
        process: [String: String] = ProcessInfo.processInfo.environment,
        home: String = FileManager.default.homeDirectoryForCurrentUser.path
    ) -> [String: String] {
        augmented(base: merged(process: process, shell: shell), home: home)
    }
}

/// Where the app finds the `graft` CLI it runs for everything it does.
enum GraftExecutable {
    /// The README's install line, quoted by the launch failure so a user who
    /// installed the app without the CLI is told the one command to run.
    static let installCommand = "pipx install git+https://github.com/GreenglassT/Graft"

    /// What rebuilds an installed graft that will not start — most often a
    /// pipx venv whose Python was upgraded or removed under it, where
    /// `installCommand` only answers "already installed". `graft-ipa` is the
    /// package name in pyproject.toml, which is what pipx knows it by.
    static let reinstallCommand = "pipx reinstall graft-ipa"

    /// Whether the app runs its own CLI, `Contents/Helpers/graft`, rather than
    /// looking for one.
    ///
    /// Release builds do: the CLI ships inside Graft.app, so the app and the
    /// CLI it runs are the same version, and a pipx `graft` on PATH can
    /// neither shadow it nor stand in for it. It is found from the path the
    /// app was opened at, for as long as it runs (#17): a copy moved while
    /// open says to reopen it (`BundledCLIFailure.moved`), and one replaced
    /// in place while open runs the new copy's CLI until it is reopened. Debug builds — Xcode runs, and
    /// the test suite, which points `TEST_RUNNER_PATH` at a worktree's venv —
    /// keep the PATH lookup: there is no bundled CLI in a Debug build.
    #if DEBUG
    static let preferBundledDefault = false
    #else
    static let preferBundledDefault = true
    #endif

    /// Where Graft.app at `bundleURL` keeps its own CLI: the compiled
    /// launcher that runs the embedded Python's `-m graft_ipa`.
    static func bundled(in bundleURL: URL) -> URL {
        bundleURL.appendingPathComponent("Contents/Helpers/graft")
    }

    /// `path` is an app's own CLI, so failing to start it means the app is
    /// missing part of itself: nothing the user installs can fix that, and
    /// neither "install the CLI" nor "reinstall it with pipx" is the remedy.
    static func isBundled(_ path: String) -> Bool {
        path.hasSuffix("/Contents/Helpers/graft")
    }

    /// The first executable `graft` in `ProcessEnvironment.toolDirs`, then in
    /// `path` — the PATH the CLI itself will be given.
    ///
    /// The tool dirs come first because they are the same directories the CLI
    /// is handed for *its* tools, so graft and cyan are found the same way.
    /// `path` is the login shell's PATH when the shell answered (see
    /// `ShellEnvironment`), so a pipx with a custom `PIPX_BIN_DIR` is found
    /// from Finder too, not only when the app was started from a shell.
    ///
    /// **Absolute directories only.** A relative PATH entry (`.`, `bin`, an
    /// empty one, which the shell reads as `.`) names wherever the app's
    /// working directory happens to be — `/` from Finder, anything at all
    /// from a terminal — and a `graft` found there is whatever file sits in
    /// that directory, run with the user's signing credentials in its
    /// environment.
    ///
    /// **Never a directory**, whatever `isExecutable` says. It is a seam, and
    /// `FileManager.isExecutableFile` — what it used to be in production — is
    /// true for any directory with its search bit set, so a `graft/` folder
    /// early on the PATH shadowed the real CLI and every launch failed with
    /// "permission denied". The refusal is not part of the seam so that no
    /// predicate, injected or not, can reintroduce that.
    static func locate(
        home: String, path: String?, isExecutable: (String) -> Bool
    ) -> URL? {
        let inherited = path.map { $0.split(separator: ":").map(String.init) } ?? []
        var seen = Set<String>()
        for dir in ProcessEnvironment.toolDirs(home: home) + inherited
        where dir.hasPrefix("/") && seen.insert(dir).inserted {
            let candidate = URL(fileURLWithPath: dir).appendingPathComponent("graft")
            if !existsAsNonRegularFile(candidate.path) && isExecutable(candidate.path) {
                return candidate
            }
        }
        return nil
    }

    /// The production `isExecutable`: a regular file — after following
    /// symlinks, because pipx's `~/.local/bin/graft` is one, into its venv —
    /// that this user may execute.
    static func isRunnableFile(_ path: String) -> Bool {
        var info = stat()
        guard stat(path, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG else { return false }
        return access(path, X_OK) == 0
    }

    /// Something is at `path` and it is not a regular file: a directory, a
    /// socket, a FIFO. False for a path where nothing is, so a test's fake
    /// filesystem (paths that exist only in its predicate) is unaffected.
    private static func existsAsNonRegularFile(_ path: String) -> Bool {
        var info = stat()
        guard stat(path, &info) == 0 else { return false }
        return (info.st_mode & S_IFMT) != S_IFREG
    }

    /// What the app runs.
    ///
    /// **`preferBundled` (release builds):** `bundled(in: bundleURL)`, always,
    /// whether or not it is there. Its existence is the launch's business: a
    /// missing one fails to start, and `SetupNeed.damagedApp` says the app is
    /// damaged. Falling back to a `graft` on PATH instead would run a CLI of
    /// some other version, and hide the damage until that one disagreed.
    ///
    /// **Otherwise (Debug):** `locate`'s answer, or, when there is none, where
    /// pipx installs graft by default. That path does not exist, and that is
    /// the point: launching it fails, and `CLIRunner.RunError` turns the
    /// failure into the install command. The fallback this replaces was
    /// `/usr/bin/env` handed graft's own arguments with no `graft` in front of
    /// them, so every pane said "exit 1: usage: env [-0iv] …" and nothing
    /// about what was missing.
    static func resolve(
        home: String, path: String?,
        bundleURL: URL = Bundle.main.bundleURL,
        preferBundled: Bool = preferBundledDefault,
        isExecutable: (String) -> Bool
    ) -> URL {
        if preferBundled {
            return bundled(in: bundleURL)
        }
        return locate(home: home, path: path, isExecutable: isExecutable)
            ?? URL(fileURLWithPath: home).appendingPathComponent(".local/bin/graft")
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
/// a drainer ever stops signaling on EOF, every CLI call the app makes pays
/// this out instead, silently. Note the waits are sequential across both pipes,
/// so that regression costs 2× this, measured at 1.04s.
private let drainGrace: TimeInterval = 0.5

/// How long a stopped CLI gets between SIGTERM and SIGKILL.
///
/// SIGTERM is graft's cue to unwind: `_build_one`'s `finally` deletes the
/// build's work dir, which for a real app holds an unpacked copy of it (cyan's,
/// ~22,000 files) and took 2.1-3.1s to remove. Two seconds cut that off: the
/// app saw exit 9 with no `build-end`, and 442 MB stayed in $TMPDIR. Ten
/// leaves room for a bigger app on a slower disk; a CLI that is genuinely wedged
/// is still killed, only later.
let cliStopGrace: TimeInterval = 10

/// A Stop a caller sends a streamed run itself, whose SIGKILL the CLI can
/// hold off.
///
/// Canceling the Task stops a run as every Stop does: SIGTERM to the CLI and
/// its children, then SIGKILL `cliStopGrace` later (`ProcessHandle.stop`).
/// `graft --update` finishes an install or a tweak swap it has begun before
/// it stops, which can take minutes, and says so (`update-stopping`). A
/// SIGKILL in the middle would leave a half-written install on the phone, so
/// the kill is held while that step finishes (`read`, on the reader's
/// thread), and armed again, with its whole `grace` from then:
/// - when the step it was held for has ended: that app's `update-app-end`,
///   the shared tweak step's end, or `update-end`;
/// - on a second Stop (spec §11.2), which also sends a second SIGTERM: the
///   CLI cancels a held push or install on it, as `device_session` does on
///   a SIGTERM. Nothing is held after that.
///
/// `forceKill` is Force Stop: SIGKILL now, held or not.
///
/// `grace` is `cliStopGrace` in the app; a test shortens it.
final class RunStopper: @unchecked Sendable {
    let grace: TimeInterval
    private let lock = NSLock()
    private var handle: ProcessHandle?
    /// How many Stops were asked for.
    private var stops = 0
    private var killHeld = false
    /// The step the kill is held for (`update-stopping`'s), when the CLI
    /// said which.
    private var heldFor: UpdateStoppingRecord?
    /// Numbers the kill timers: only the newest may fire, so a kill armed
    /// again gets its whole grace from then.
    private var armed = 0
    private var forced = false

    init(grace: TimeInterval) {
        self.grace = grace
    }

    /// SIGTERM now; SIGKILL after `grace` unless the kill is held by then.
    /// A second Stop sends SIGTERM again and arms the kill again, held or
    /// not.
    func stop() {
        let (target, generation, again): (ProcessHandle?, Int, Bool) = lock.withLock {
            stops += 1
            if stops > 1 {
                killHeld = false
                heldFor = nil
            }
            armed += 1
            return (handle, armed, stops > 1)
        }
        if again {
            target?.stopAgain(grace: grace, mayKill: mayKill(generation))
        } else {
            target?.cancel(grace: grace, mayKill: mayKill(generation))
        }
    }

    /// The CLI said it is finishing a step: no SIGKILL for this run, until
    /// the kill is armed again.
    func holdKill() {
        hold(for: nil)
    }

    /// Arm the kill again: SIGKILL `grace` from now, unless it is held again
    /// by then. Nothing when it isn't held.
    func rearmKill() {
        let (target, generation): (ProcessHandle?, Int?) = lock.withLock {
            guard killHeld else { return (nil, nil) }
            killHeld = false
            heldFor = nil
            armed += 1
            return (handle, armed)
        }
        guard let target, let generation else { return }
        target.scheduleKill(after: grace, mayKill: mayKill(generation))
    }

    /// Force Stop: SIGKILL the CLI and its children now.
    func forceKill() {
        let target: ProcessHandle? = lock.withLock {
            forced = true
            killHeld = false
            heldFor = nil
            return handle
        }
        target?.killNow()
    }

    var isKillHeld: Bool { lock.withLock { killHeld } }
    var wasStopped: Bool { lock.withLock { stops > 0 } }
    var wasForced: Bool { lock.withLock { forced } }

    /// Each event the run writes, as `stream`'s reader decodes it, on the
    /// reader's thread. `update-stopping` holds the kill here and not after
    /// a hop to the main actor: a main actor busy past `grace` would let the
    /// SIGKILL win, and cut off the install the CLI is finishing. The end of
    /// the step it was held for arms it again.
    func read(_ event: BuildEvent) {
        switch event {
        case let .updateStopping(record):
            hold(for: record)
        case let .updateAppEnd(record):
            if lock.withLock({ heldFor.map { $0.app != nil && $0.app == record.app } ?? false }) { rearmKill() }
        case let .updateStep(step) where step.app == nil && step.status != CLIContract.Update.statusStart:
            if lock.withLock({ heldFor.map { $0.app == nil } ?? false }) { rearmKill() }
        case .updateEnd:
            rearmKill()
        default:
            break
        }
    }

    private func hold(for step: UpdateStoppingRecord?) {
        lock.withLock {
            // After a second Stop nothing is held again: the person asked
            // for the run to end, whatever it was finishing.
            guard stops < 2 else { return }
            killHeld = true
            heldFor = step
        }
    }

    /// Whether the kill timer numbered `generation` may fire: the newest,
    /// and not held.
    private func mayKill(_ generation: Int) -> @Sendable () -> Bool {
        { [weak self] in
            guard let self else { return true }
            return self.lock.withLock { !self.killHeld && self.armed == generation }
        }
    }

    /// The runner hands over the run's handle as it makes it. A Stop asked
    /// for before then stops the run as it starts, so it never launches.
    fileprivate func attach(_ handle: ProcessHandle) {
        let (stopNow, generation): (Bool, Int) = lock.withLock {
            self.handle = handle
            return (stops > 0, armed)
        }
        if stopNow {
            handle.cancel(grace: grace, mayKill: mayKill(generation))
        }
    }
}

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

    enum RunError: LocalizedError {
        case launchFailed(executable: String, underlying: Error)

        /// Every pane shows `localizedDescription`, and without this a CLI
        /// that isn't installed read "The operation couldn't be completed.
        /// (Graft.CLIRunner.RunError error 0.)". Not being able to start graft
        /// at all almost always means it isn't installed where the app looks,
        /// so the message names the path and the command that installs it.
        var errorDescription: String? {
            switch self {
            case let .launchFailed(executable, underlying):
                // The app's own CLI: no install or reinstall reaches it.
                if GraftExecutable.isBundled(executable) {
                    return "\(BundledCLIFailure.diagnose(executable: executable).message) "
                        + "(\(executable) would not start: \(underlying.localizedDescription))"
                }
                // Something is there and would not start: installing again is
                // the advice that loops ("already installed"). Say what is
                // wrong with the file and how to rebuild it.
                if let broken = BrokenCLI.diagnose(path: executable, underlying: underlying) {
                    return "Couldn't run the graft command-line tool at \(executable): "
                        + "\(broken.reason). Reinstall it: \(GraftExecutable.reinstallCommand)"
                }
                let dirs = ProcessEnvironment.toolDirs(home: "~").joined(separator: ", ")
                return "Couldn't run the graft command-line tool at \(executable). "
                    + "\(underlying.localizedDescription) Graft.app needs the CLI installed: "
                    + "\(GraftExecutable.installCommand) — the app looks for graft in "
                    + "\(dirs), then on PATH."
            }
        }
    }

    /// What a launch runs: the executable and the environment it gets.
    struct Target: Sendable {
        let executable: URL
        /// Nil inherits this process's environment unchanged.
        let environment: [String: String]?
    }

    /// Decided at each launch, not at construction.
    ///
    /// A fixed pair is what every test runner is, and what this used to be for
    /// the app too — decided in `GraftApp`'s initialiser, before anything could
    /// have asked the login shell, and never again. Two things need it decided
    /// later: the login shell's environment (`ShellEnvironment`) arrives
    /// asynchronously, and the first launch must wait for it rather than run
    /// against launchd's; and the setup screen's Check again has to find a
    /// graft installed after the app started, which a path chosen at launch
    /// never could.
    private let target: @Sendable () async -> Target

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

    /// What `refreshEnvironment()` does: ask the login shell again, for the
    /// app's runner, and say whether it asked; nothing, for a runner with a
    /// fixed target.
    private let refresh: (@Sendable () -> Bool)?

    init(
        executable: URL,
        environment: [String: String]? = nil,
        willLaunch: (@Sendable () -> Void)? = nil
    ) {
        let fixed = Target(executable: executable, environment: environment)
        self.target = { fixed }
        self.willLaunch = willLaunch
        self.refresh = nil
    }

    /// A runner whose target is worked out afresh for every launch.
    ///
    /// - Parameter refresh: re-reads what `target` is built from, for
    ///   `refreshEnvironment()`.
    init(
        resolving target: @escaping @Sendable () async -> Target,
        refresh: (@Sendable () -> Bool)? = nil
    ) {
        self.target = target
        self.willLaunch = nil
        self.refresh = refresh
    }

    /// Re-read the environment launches are built from, so the next one sees
    /// a setup the user just changed in Terminal. The setup screen's Check
    /// again calls this; see `ShellEnvironment.refresh()`. False when the
    /// shell was asked too recently to ask again, and the last answer stands
    /// (`ShellEnvironment.askedTooRecentlyNote`); true otherwise, a runner
    /// with nothing to re-read included.
    @discardableResult
    func refreshEnvironment() -> Bool {
        refresh?() ?? true
    }

    /// The app's runner: `graft`, wherever it is now, in the user's
    /// environment.
    ///
    /// Each launch waits for the login shell's environment — bounded by
    /// `LoginShell.startLimit` and `LoginShell.timeout`, and immediate after
    /// the first — then merges it under this app's own augmentation and looks
    /// for `graft` on the PATH that produces. Looking again every launch costs
    /// a handful of `stat`s against a Python interpreter's startup, and it is
    /// what lets the setup screen's Check again find a CLI installed after the
    /// app opened.
    ///
    /// A release build skips the lookup and runs its own
    /// `Contents/Helpers/graft` (`GraftExecutable.resolve`), still in the
    /// login shell's environment: the CLI's own tools and the user's settings
    /// come from there.
    ///
    /// Everything is a parameter so a test can stand in a fake shell answer, a
    /// fake home and a fake filesystem. `shell` has no default: `.shared`
    /// starts the user's own login shell, so `GraftApp` names it and a test
    /// passes its own answer (`NoRealDefaultsTests`).
    static func forApp(
        shell: ShellEnvironment,
        process: [String: String] = ProcessInfo.processInfo.environment,
        home: String = FileManager.default.homeDirectoryForCurrentUser.path,
        bundleURL: URL = Bundle.main.bundleURL,
        preferBundled: Bool = GraftExecutable.preferBundledDefault,
        isExecutable: @escaping @Sendable (String) -> Bool = GraftExecutable.isRunnableFile
    ) -> CLIRunner {
        CLIRunner(resolving: {
            let env = ProcessEnvironment.forCLI(
                shell: await shell.value, process: process, home: home
            )
            let executable = GraftExecutable.resolve(
                home: home, path: env["PATH"], bundleURL: bundleURL,
                preferBundled: preferBundled, isExecutable: isExecutable
            )
            return Target(executable: executable, environment: env)
        }, refresh: { shell.refresh() })
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
        let target = await self.target()
        let proc = Process()
        proc.executableURL = target.executable
        proc.arguments = arguments
        if let env = target.environment { proc.environment = env }

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
            proc, out: out, err: err, inPipe: inPipe, stdin: stdin, input: nil, stopper: nil,
            drained: [outDone, errDone]
        ) { status in
            RunResult(exitCode: status, stdout: outBox.string(), stderr: errBox.string())
        }
    }

    /// The launch protocol both modes share: adopt the process so a canceled
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
        input: ChildInput?,
        stopper: RunStopper?,
        drained: [DispatchSemaphore],
        makeResult: @escaping @Sendable (Int32) -> RunResult
    ) async throws -> RunResult {
        let handle = ProcessHandle()
        stopper?.attach(handle)
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (cont: CheckedContinuation<RunResult, Error>) in
                func releaseDrainers() {
                    try? out.fileHandleForWriting.close()
                    try? err.fileHandleForWriting.close()
                }
                guard handle.adopt(proc) else {
                    // Canceled during setup — never launch.
                    input?.close()
                    releaseDrainers()
                    cont.resume(throwing: CancellationError())
                    return
                }
                proc.terminationHandler = { p in
                    // The child is gone: a line sent now is a `false`, not a write.
                    input?.close()
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
                    // A canceled run has no use for the tail of the output
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
                    if let input, let inPipe {
                        // A two-way exchange: the caller writes as the child asks.
                        if killedAtLaunch {
                            input.close()
                            try? inPipe.fileHandleForWriting.close()
                        } else {
                            input.attach(inPipe.fileHandleForWriting)
                        }
                    } else if let inPipe, let stdin, !killedAtLaunch {
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
                        // this one; `setP12Password`, `setSSHPassword` and
                        // `AccountSetupStore` are the callers that pass stdin.
                        if var data = (stdin + "\n").data(using: .utf8) {
                            writer.write(data)
                            // The one buffer of the secret this method made (a
                            // .p12 password: `AccountSetupStore`) is zeroed, not
                            // left in freed memory. The caller's String is the
                            // caller's to drop; Swift can't promise to wipe one.
                            data.resetBytes(in: 0..<data.count)
                        }
                        try? writer.close()
                    } else if let inPipe {
                        try? inPipe.fileHandleForWriting.close()
                    }
                } catch {
                    // The child never started, so terminationHandler won't
                    // fire; resume the throw here.
                    handle.markFinished()
                    input?.close()
                    releaseDrainers()
                    cont.resume(throwing: RunError.launchFailed(
                        executable: proc.executableURL?.path ?? "graft", underlying: error
                    ))
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
    ///
    /// Terminal escape sequences are removed first. The environment filter
    /// (`ProcessEnvironment.colourVariables`) is what keeps Rich from
    /// coloring a pipe; this is the backstop for whatever it misses — an
    /// older app's CLI, a tool that colors regardless — because one escape in
    /// front of `error:` is enough to lose the parse, and every surface built
    /// on these lines would otherwise paste `\e[31m` into a SwiftUI `Text`.
    static func stderrLines(_ stderr: String) -> [String] {
        strippingTerminalEscapes(stderr)
            .split(separator: "\n", omittingEmptySubsequences: false)
            .map(trimmingTrailingWhitespace)
            .filter { !$0.isEmpty }
    }

    /// `text` without ANSI/VT escape sequences: CSI (`ESC [ … final`, colors
    /// and cursor moves), OSC (`ESC ] … BEL` or `ESC ] … ESC \`, which is how
    /// Rich writes a hyperlink), and the two-byte `ESC <char>` forms. A lone
    /// ESC at the very end goes too. Everything else is kept byte for byte.
    static func strippingTerminalEscapes(_ text: String) -> String {
        guard text.unicodeScalars.contains("\u{1B}") else { return text }
        var out = String.UnicodeScalarView()
        let scalars = Array(text.unicodeScalars)
        var i = 0
        while i < scalars.count {
            guard scalars[i] == "\u{1B}" else {
                out.append(scalars[i])
                i += 1
                continue
            }
            i += 1
            guard i < scalars.count else { break }
            switch scalars[i] {
            case "[":
                // Parameter and intermediate bytes, then one final byte in
                // 0x40–0x7E.
                i += 1
                while i < scalars.count, !(0x40...0x7E).contains(scalars[i].value) { i += 1 }
                i += 1
            case "]":
                // Until BEL, or the two-byte string terminator ESC \.
                i += 1
                while i < scalars.count {
                    if scalars[i] == "\u{07}" { i += 1; break }
                    if scalars[i] == "\u{1B}", i + 1 < scalars.count, scalars[i + 1] == "\\" {
                        i += 2
                        break
                    }
                    i += 1
                }
            default:
                i += 1
            }
        }
        return String(out)
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
    /// Throws `CLIError.exited` if the CLI exits non-zero. `stdin` is written
    /// as one line and closed (`runOnce`): a secret goes there, never in `args`.
    func runEvents(_ args: [String], failing doing: String, stdin: String? = nil) async throws -> [BuildEvent] {
        let result = try await runOnce(arguments: args, stdin: stdin)
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
    ///
    /// `refusal` reads a *failed* run's events before its stderr becomes the
    /// error: a refusal the CLI also reported as an event (C-UNVERIFIED's
    /// `appstore-refused`) is thrown as the caller's own error, so a button
    /// can be offered for it without matching the message. A successful run
    /// never consults it.
    ///
    /// `input` keeps the child's stdin open for lines sent while it runs
    /// (`ChildInput`: `--appstore-login`'s 2FA code). It isn't function-typed,
    /// so an unlabeled trailing closure still binds to `onEvent`.
    func streamEvents(
        _ args: [String],
        failing doing: String,
        refusal: Refusal = .none,
        input: ChildInput? = nil,
        onEvent: @escaping @MainActor (BuildEvent) -> Void = { _ in }
    ) async throws -> [BuildEvent] {
        // Read `result.events` rather than the onEvent callback so we don't
        // race the trailing MainActor Tasks scheduled from terminationHandler.
        let result = try await stream(arguments: args, input: input, onEvent: onEvent)
        try Task.checkCancellation()
        if result.exitCode != 0, let refused = refusal.error(result.events) { throw refused }
        try Self.checkExit(result.exitCode, stderr: result.stderr, doing: doing)
        return result.events
    }

    /// What `streamEvents` throws for a failed run whose events carry a
    /// refusal, in place of its stderr: `error` reads the events, and nil
    /// means the stderr stands.
    ///
    /// A type of its own, not a closure parameter, because of where it sits.
    /// `onEvent` is `streamEvents`' trailing closure, and a defaulted
    /// function-typed parameter in front of it is one an unlabeled trailing
    /// closure binds to under forward-scan matching (SE-0286):
    /// `streamEvents(args, failing: x) { event in … }` compiled only through
    /// the deprecated backward scan, with a warning, and a Swift 6 build reads
    /// that closure as the refusal. Forward scan skips a parameter that is not
    /// function-like, so the trailing closure is always `onEvent`.
    struct Refusal {
        let error: ([BuildEvent]) -> Error?

        init(_ error: @escaping ([BuildEvent]) -> Error?) {
            self.error = error
        }

        /// Nothing in the events is a refusal: every failure is its stderr.
        static var none: Refusal { Refusal { _ in nil } }
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
        try await mutateEvents(args, failing: doing, succeeded: succeeded).compactMap(pick).first
    }

    /// As `mutate`, handing back every event the call wrote, for a caller
    /// that wants its warnings as well as its record.
    func mutateEvents(
        _ args: [String],
        failing doing: String,
        succeeded: String
    ) async throws -> [BuildEvent] {
        let events = try await runner.runEvents(args, failing: doing)
        do {
            try await reload()
        } catch {
            lastError = "\(succeeded), but couldn't refresh the list: "
                + error.localizedDescription
        }
        return events
    }
}

extension CLIRunner {
    /// Run the command and call `onEvent` for each JSON-Lines event on stdout.
    /// Returns when the subprocess exits. With an `input`, the child's stdin
    /// is a pipe the caller writes to as it runs (`ChildInput`).
    ///
    /// `stopper` is a Stop of the caller's own beside canceling the Task, one
    /// whose SIGKILL the CLI can hold off (`RunStopper`).
    func stream(
        arguments: [String],
        input: ChildInput? = nil,
        stopper: RunStopper? = nil,
        onEvent: @escaping @MainActor (BuildEvent) -> Void
    ) async throws -> RunResult {
        let target = await self.target()
        let proc = Process()
        proc.executableURL = target.executable
        proc.arguments = arguments
        if let env = target.environment { proc.environment = env }

        let out = Pipe()
        let err = Pipe()
        proc.standardOutput = out
        proc.standardError = err
        // Only with an input: otherwise the child inherits the app's
        // stdin (/dev/null for a GUI app), as it always has.
        let inPipe: Pipe? = input == nil ? nil : Pipe()
        if let inPipe { proc.standardInput = inPipe }

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
                        // Here, before the hop: see `RunStopper.read`.
                        stopper?.read(event)
                        Task { @MainActor in onEvent(event) }
                    }
                }
            }
            let remainder = buffer.flushRemainder()
            if !remainder.isEmpty,
               let event = try? BuildEvent.decode(line: remainder) {
                eventsBox.append(event)
                stopper?.read(event)
                Task { @MainActor in onEvent(event) }
            }
            outDone.signal()
        }

        drainToEOF(err.fileHandleForReading, into: stderrBox, done: errDone)

        return try await launch(
            proc, out: out, err: err, inPipe: inPipe, stdin: nil, input: input, stopper: stopper,
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

/// Owns a spawned `Process` so a canceled Task can actually stop it.
///
/// `Process` on its own isn't enough. A cancellation handler runs on an
/// arbitrary thread and can arrive at any point: before `run()`, mid-flight,
/// or after the child already exited and was reaped. Calling `terminate()` on
/// a process that was never launched traps, and signaling a dead pid risks
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
    /// escalation and signaling a pid Foundation may already have reaped.
    ///
    /// **Set only when a stop actually signaled**, never merely when one was
    /// attempted. Setting it on the attempt reintroduces the leak this whole
    /// mechanism exists to prevent: in the launch window `cancel()` finds a
    /// non-nil `process` that is not yet running, `stop()` returns having done
    /// nothing, and `launched()` then skips — so the child starts unkillable
    /// again. Caught immediately by
    /// `test_a_cancellation_inside_the_launch_window_still_kills_the_child`,
    /// which failed at 30.02s, the child's whole sleep.
    private var stopIssued = false

    /// The grace and the kill check the stop uses: `cliStopGrace` and always,
    /// unless a `RunStopper` says otherwise.
    private var stopGrace: TimeInterval = cliStopGrace
    private var mayKill: @Sendable () -> Bool = { true }

    /// Take ownership just before launching. Returns false when cancellation
    /// already arrived, in which case the caller must not launch at all —
    /// otherwise a Task canceled during setup still spawns a subprocess that
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

    func cancel(grace: TimeInterval = cliStopGrace, mayKill: @escaping @Sendable () -> Bool = { true }) {
        lock.lock()
        cancelled = true
        stopGrace = grace
        self.mayKill = mayKill
        let victim = (finished || stopIssued) ? nil : process
        lock.unlock()
        if Self.stop(victim, grace: grace, mayKill: mayKill) { markStopped() }
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
    /// not work: canceling the `Task` immediately lands *before* the body
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
        let grace = stopGrace
        let mayKill = self.mayKill
        lock.unlock()
        let killed = Self.stop(victim, grace: grace, mayKill: mayKill)
        if killed { markStopped() }
        return killed
    }

    private func markStopped() {
        lock.lock(); defer { lock.unlock() }
        stopIssued = true
    }

    /// The process while it hasn't finished, to signal.
    private var unfinished: Process? {
        lock.lock(); defer { lock.unlock() }
        return finished ? nil : process
    }

    /// A second Stop (`RunStopper`): SIGTERM again, and a kill `grace` later.
    func stopAgain(grace: TimeInterval, mayKill: @escaping @Sendable () -> Bool) {
        if Self.stop(unfinished, grace: grace, mayKill: mayKill) { markStopped() }
    }

    /// A kill armed again (`RunStopper.rearmKill`): SIGKILL `grace` from now.
    func scheduleKill(after grace: TimeInterval, mayKill: @escaping @Sendable () -> Bool) {
        guard let victim = unfinished, victim.isRunning else { return }
        Self.scheduleKill(victim, after: grace, mayKill: mayKill)
    }

    /// Force Stop (`RunStopper.forceKill`): SIGKILL now.
    func killNow() {
        guard let victim = unfinished, victim.isRunning else { return }
        Self.killTree(victim)
    }

    /// Returns whether it actually signaled anything.
    @discardableResult
    private static func stop(
        _ victim: Process?, grace: TimeInterval = cliStopGrace,
        mayKill: @escaping @Sendable () -> Bool = { true }
    ) -> Bool {
        guard let victim, victim.isRunning else { return false }
        // Descendants first, and while the parent is still alive to be walked:
        // once it is reaped its children are re-parented to launchd and there
        // is nothing left to enumerate from.
        let orphans = descendants(of: victim.processIdentifier)
        victim.terminate()  // SIGTERM: let the CLI unwind and clean up temp files.
        for pid in orphans { kill(pid, SIGTERM) }

        // zsign and cyan spend long stretches in tight native loops and can be
        // slow to notice a signal, and the whole point of canceling is that
        // the work is no longer wanted. Escalate rather than leaving the CPU
        // spinning on output nobody will read. `isRunning` is re-checked under
        // Process's own bookkeeping, which holds the pid until the child is
        // reaped, so this can't signal a recycled pid.
        scheduleKill(victim, after: grace, mayKill: mayKill)
        return true
    }

    /// SIGKILL `victim` and its children `grace` from now, if it still runs
    /// then and `mayKill` says yes.
    private static func scheduleKill(
        _ victim: Process, after grace: TimeInterval, mayKill: @escaping @Sendable () -> Bool
    ) {
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + grace) {
            // Re-walked here, never the `cliStopGrace`-old `orphans` snapshot.
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
            //
            // `mayKill`: a `RunStopper` whose CLI said it is finishing an
            // install says no, so the install isn't cut off halfway.
            guard victim.isRunning, mayKill() else { return }
            killTree(victim)
        }
    }

    /// SIGKILL `victim`'s children, re-walked now, then `victim`.
    private static func killTree(_ victim: Process) {
        for pid in descendants(of: victim.processIdentifier) {
            kill(pid, SIGKILL)
        }
        kill(victim.processIdentifier, SIGKILL)
    }

    /// Every descendant of `pid`, deepest last.
    ///
    /// **Signaling the child alone is not enough, and the failure is silent.**
    /// A `graft` invocation spawns `cyan` and `zsign`; terminating the
    /// CLI leaves those running, still working and still holding the write end
    /// of the pipe this runner is draining. No EOF arrives, so `stream` never
    /// returns and the caller waits out work it already canceled.
    ///
    /// Measured directly: `/bin/sh -c 'printf …; sleep 30; …'` SIGTERMed at the
    /// shell leaves `sleep` alive and re-parented. That is exactly the shape of
    /// `ConfigStoreTests.test_stopping_a_build_kills_the_subprocess`, whose
    /// intermittent failure was the stream never returning inside 5s.
    ///
    /// Best effort by construction: a pid can exit between listing and
    /// signaling, and `kill` on a dead pid is a harmless ESRCH. Nothing here
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
    /// Closing that window needs a single `kill(-pgid)` that reaches
    /// everything regardless of timing. (This said `Foundation.Process` gives
    /// no way to get a process group. It does not need asking: measured on
    /// macOS 15+, a `Process` child's pgid is its own pid — `LoginShell`
    /// relies on that for its timeout. Using it here is a change to this
    /// cancellation path that has not been made.)
    private static func descendants(of pid: pid_t) -> [pid_t] {
        ProcessTree.descendants(of: pid)
    }
}

/// The processes under a pid, for stopping what a child started.
enum ProcessTree {
    /// Every descendant of `pid`, deepest last. See `ProcessHandle` for why
    /// this is best effort: a pid can exit between listing and signaling.
    static func descendants(of pid: pid_t) -> [pid_t] {
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
