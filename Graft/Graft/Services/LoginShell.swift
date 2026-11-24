import Darwin
import Foundation

/// The user's login-shell environment, read once at launch the way editors
/// read it.
///
/// An app started from Finder or the Dock inherits launchd's environment, not
/// the user's shell setup. That cost three kinds of thing the CLI needs:
///
///  - **PATH additions** the fixed tool directories cannot guess: pip's
///    `--user` bin (`~/Library/Python/3.x/bin`), nvm/volta/`~/.npm-global` for
///    bagbak and node, a pipx with a custom `PIPX_BIN_DIR`.
///  - **`XDG_*` directories.** Without them Terminal and the app used
///    different config, cache and state directories — two Graft installs that
///    did not know about each other.
///  - **Graft's own variables**: `GRAFT_*`, `UNFAIRPLAY_MODEL_DIR`, and the
///    `.p12` password variable `signing.cert_password_env` names.
///
/// So the app asks the user's shell. `<shell> -l -i -c <command>` runs the
/// login and interactive startup files a Terminal window runs, and the command
/// prints `env -0` between two markers. Everything around the markers is
/// ignored, which is what makes a shell that prints a banner, a fortune or a
/// prompt-theme warning harmless. (csh and tcsh refuse `-l` beside any other
/// flag, so they get `-i -c`; see `arguments(_:shell:)`.)
///
/// **Silent on failure, by design.** A shell that hangs, exits non-zero,
/// prints no markers or prints garbage leaves the app exactly as it behaved
/// before this existed: launchd's environment plus the fixed tool directories
/// (`ProcessEnvironment.augmented`). Nothing here is worth an alert — the user
/// did not ask for it, and the app still works without it.
enum LoginShell {
    /// How long the shell gets once it is running. An interactive zsh with a
    /// prompt theme and a conda hook measured well under a second here; five
    /// is the ceiling editors settle on.
    static let timeout: TimeInterval = 5

    /// How long the shell may take to start running at all, before `timeout`
    /// starts. macOS checks a program that isn't Apple's (Homebrew's fish or
    /// bash) the first time it runs it: the shell waits inside exec while
    /// syspolicyd looks its notarization up online and XProtect scans it.
    /// `Process.run()` has returned by then. That took about 0.8 s here, and
    /// up to 3 s over a slow network, out of the five, so the startup files
    /// of a shell that was fine every other time could run out of time
    /// (`runProcess`). A shell still not running after this is abandoned, so
    /// one that never starts can't hold the first CLI launch for ever.
    static let startLimit: TimeInterval = 15

    /// How often `waitForAnswer` looks whether the shell has started.
    static let startPollInterval: TimeInterval = 0.02

    /// Runs a shell and hands back what it wrote to stdout between the two
    /// markers — or nil when it could not be started or did not get there in
    /// time. `environment` nil inherits this process's. A seam: the tests pass
    /// one that never starts the user's shell, so no test reads the
    /// developer's own startup files.
    typealias Runner = @Sendable (
        _ shell: String, _ arguments: [String], _ markers: Markers,
        _ environment: [String: String]?, _ timeout: TimeInterval
    ) async -> Data?

    /// The most the reader keeps from the start marker on. macOS bounds the
    /// environment a process can be started with by `ARG_MAX` (1 MiB,
    /// arguments included), so a real `env -0` is never larger; four times
    /// that is margin. Past it the answer is abandoned rather than grown.
    static let outputCap = 4 * 1024 * 1024

    /// Variables a shell sets about *itself* — where it is, how deep, which
    /// terminal it thinks it is in. None describes the user's setup, and a few
    /// are actively wrong for a subprocess with no terminal: `TERM` invites
    /// color codes into a pipe, `PWD` names the directory the shell started
    /// in rather than the CLI's own.
    static let noise: Set<String> = [
        "PWD", "OLDPWD", "SHLVL", "_",
        "TERM", "TERM_SESSION_ID", "SHELL_SESSION_ID",
        "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "COLORTERM",
        "COLUMNS", "LINES",
    ]

    /// The user's login shell from the user database — what Terminal starts —
    /// never `$SHELL`, which is whatever the process that launched the app
    /// happened to export (or nothing at all, from launchd).
    static func userShell() -> String {
        let entry = getpwuid(getuid())
        let recorded = entry.flatMap { $0.pointee.pw_shell }.map { String(cString: $0) }
        return userShell(recorded: recorded, isExecutable: {
            FileManager.default.isExecutableFile(atPath: $0)
        })
    }

    /// `userShell()`'s decision, with the lookup passed in.
    static func userShell(recorded: String?, isExecutable: (String) -> Bool) -> String {
        if let recorded, recorded.hasPrefix("/"), isExecutable(recorded) { return recorded }
        return "/bin/zsh"
    }

    /// The two markers, made unique per run so no banner can contain them.
    struct Markers {
        let start: String
        let end: String

        static func fresh() -> Markers {
            let token = UUID().uuidString.replacingOccurrences(of: "-", with: "")
            return Markers(start: "__GRAFT_ENV_\(token)_START__", end: "__GRAFT_ENV_\(token)_END__")
        }

        /// Each marker printed as two `printf` arguments, so the command line
        /// itself never contains a marker whole. A startup file that echoes
        /// commands (`set -x`, `setopt verbose`) then cannot plant one.
        private static func printed(_ marker: String) -> String {
            let cut = marker.index(marker.startIndex, offsetBy: marker.count / 2)
            return "printf '%s%s' '\(marker[..<cut])' '\(marker[cut...])'"
        }

        /// What the shell runs: a marker, the environment NUL-separated, a
        /// marker. `/usr/bin/env` by absolute path, so an alias or a function
        /// called `env` in a startup file cannot answer instead.
        var command: String {
            "\(Self.printed(start)); /usr/bin/env -0; \(Self.printed(end))"
        }
    }

    /// `-l -i -c <command>`, except for csh and tcsh.
    ///
    /// Both reject `-l` unless it is the only argument ("Unknown option:
    /// `-l'"), so `-l -i -c` exited 1 at once and a tcsh user's environment
    /// was never read. They get `-i -c`: interactive, which reads
    /// `~/.tcshrc` or `~/.cshrc` — where csh users keep their `setenv`s —
    /// but not `~/.login`, which only a login shell reads and there is no way
    /// to ask for alongside a command.
    static func arguments(_ markers: Markers, shell: String) -> [String] {
        isCshFamily(shell) ? ["-i", "-c", markers.command] : ["-l", "-i", "-c", markers.command]
    }

    static func isCshFamily(_ shell: String) -> Bool {
        let name = (shell as NSString).lastPathComponent
        return name == "csh" || name == "tcsh"
    }

    /// The environment between the markers, or nil when the output does not
    /// have them in order.
    ///
    /// Entries are split on NUL, which is the only byte a value cannot hold —
    /// a value with a newline in it survives. One without `=`, with an empty
    /// name, or not valid UTF-8 is dropped rather than failing the whole read.
    static func parse(_ output: Data, markers: Markers) -> [String: String]? {
        guard let start = output.range(of: Data(markers.start.utf8)),
              let end = output.range(of: Data(markers.end.utf8), in: start.upperBound..<output.endIndex)
        else { return nil }
        var env: [String: String] = [:]
        for entry in output[start.upperBound..<end.lowerBound].split(separator: 0) {
            guard let text = String(data: Data(entry), encoding: .utf8),
                  let eq = text.firstIndex(of: "="), eq != text.startIndex
            else { continue }
            env[String(text[..<eq])] = String(text[text.index(after: eq)...])
        }
        return env.isEmpty ? nil : env
    }


    /// Ask `shell` for its environment. Nil on any failure.
    ///
    /// - Parameter environment: what the shell is started with; nil inherits
    ///   this process's, which is what the app wants — the shell then reports
    ///   what the user's startup files make of it. A test passes a fake `HOME`
    ///   here so a real shell reads a scratch startup file, not the
    ///   developer's own.
    ///
    /// `shell` and `run` have no defaults: together they start the user's own
    /// login shell, with their startup files. `ShellEnvironment.shared` names
    /// them; a test passes its own (`NoRealDefaultsTests`).
    static func resolve(
        shell: String,
        timeout: TimeInterval = timeout,
        environment: [String: String]? = nil,
        run: Runner
    ) async -> [String: String]? {
        let markers = Markers.fresh()
        let output = await run(
            shell, arguments(markers, shell: shell), markers, environment, timeout
        )
        guard let output else { return nil }
        return parse(output, markers: markers)
    }

    /// The real runner: the shell as a child, stdin `/dev/null`, stderr
    /// discarded, stdout read until it closes **or the end marker arrives**.
    ///
    /// The second half is not an optimization. A startup file that starts a
    /// background agent (ssh-agent, gpg-agent, a prompt theme's worker) hands
    /// it the shell's stdout, and that pipe then never reaches EOF while the
    /// agent lives. Waiting for EOF would turn every such setup into a
    /// five-second timeout and a lost environment; the marker says everything
    /// needed has arrived.
    ///
    /// stdin is `/dev/null` so a startup file that asks a question — an update
    /// prompt — reads EOF and moves on instead of waiting for an answer that
    /// cannot come.
    ///
    /// **A timeout kills the shell's whole process group.** `Process` starts
    /// the shell as a group leader (its pgid is its pid), and without job
    /// control — no terminal — everything its startup files run stays in that
    /// group. Signaling the shell alone, as this used to, killed the shell
    /// and left the command it was waiting on: a hung `brew` or `nvm` call, or
    /// anything that ignores SIGTERM, ran on re-parented to launchd, still
    /// holding the write end of the pipe, and the reader thread stayed parked
    /// on it for as long as that process lived. Nothing the shell started is
    /// wanted once its answer is abandoned, so the group gets SIGKILL.
    ///
    /// A shell that *answered* and is lingering is a different case and keeps
    /// the gentler treatment — SIGTERM to the shell, SIGKILL a second later.
    /// What its startup files started may be the agent whose socket the
    /// answer just reported.
    ///
    /// **The reader owns the read end and closes it**, polling so it can be
    /// told to stop: a writer outside the group (one that called `setsid`)
    /// survives the kill, and a blocking `read` would wait on it forever.
    ///
    /// **`timeout` runs from when the shell starts running**, not from
    /// `process.run()`: macOS's first-run check of a shell that isn't Apple's
    /// happens in between (`startLimit`). The shell has started once it has
    /// run any code of its own (`hasRun`), or written anything.
    @Sendable
    static func runProcess(
        shell: String, arguments: [String], markers: Markers,
        environment: [String: String]?, timeout: TimeInterval
    ) async -> Data? {
        await runProcess(
            shell: shell, arguments: arguments, markers: markers, environment: environment,
            timeout: timeout, startLimit: startLimit, hasStarted: hasRun
        )
    }

    /// `runProcess`, with how long the shell may take to start, and how it
    /// is seen to have started, passed in: a seam for the tests, which can't
    /// make macOS hold a shell in exec.
    static func runProcess(
        shell: String, arguments: [String], markers: Markers,
        environment: [String: String]?, timeout: TimeInterval,
        startLimit: TimeInterval, hasStarted: @escaping @Sendable (pid_t) -> Bool
    ) async -> Data? {
        await withCheckedContinuation { (cont: CheckedContinuation<Data?, Never>) in
            DispatchQueue.global(qos: .userInitiated).async {
                let process = Process()
                process.executableURL = URL(fileURLWithPath: shell)
                process.arguments = arguments
                if let environment { process.environment = environment }
                process.standardInput = FileHandle.nullDevice
                process.standardError = FileHandle.nullDevice
                let out = Pipe()
                process.standardOutput = out
                do {
                    try process.run()
                } catch {
                    cont.resume(returning: nil)
                    return
                }
                try? out.fileHandleForWriting.close()

                let collected = MarkedOutput(markers: markers, cap: outputCap)
                let stop = StopFlag()
                let finished = DispatchSemaphore(value: 0)
                DispatchQueue.global(qos: .userInitiated).async {
                    let reader = out.fileHandleForReading
                    readUntilDone(reader.fileDescriptor, into: collected, stop: stop)
                    try? reader.close()
                    finished.signal()
                }
                let pid: pid_t = process.processIdentifier
                let timedOut = waitForAnswer(finished, timeout: timeout, startLimit: startLimit) {
                    collected.receivedAny || hasStarted(pid)
                }
                if timedOut {
                    stop.set()
                    // Descendants first, while the shell is alive to be walked:
                    // a shell that found a terminal (the app started from one)
                    // runs with job control, and each command it starts then
                    // gets a process group of its own, out of reach of the
                    // group kill below.
                    if process.isRunning {
                        for child in ProcessTree.descendants(of: pid) { kill(child, SIGKILL) }
                    }
                    // The group outlives its leader while any member is alive,
                    // and its id cannot be handed to a new process until then,
                    // so this reaches the shell's leftovers even after the
                    // shell itself has gone. `kill(-pid, 0)` first: once the
                    // whole group is gone there is nothing to signal, and a
                    // bare `kill(-pid)` would then be a guess.
                    if kill(-pid, 0) == 0 { kill(-pid, SIGKILL) }
                } else if process.isRunning {
                    // Past the marker and lingering: the shell has no work
                    // left that this app wants.
                    process.terminate()
                    DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 1) {
                        if process.isRunning { kill(pid, SIGKILL) }
                    }
                }
                cont.resume(returning: timedOut ? nil : collected.data)
            }
        }
    }

    /// Wait for `finished`, the reader's end: no more than `startLimit` for
    /// the shell to start (`started`), then `timeout` from when it did.
    /// Whether it timed out. Each look at `started` is
    /// `startPollInterval` apart, so the timeout starts at most that late.
    static func waitForAnswer(
        _ finished: DispatchSemaphore, timeout: TimeInterval, startLimit: TimeInterval,
        started: () -> Bool
    ) -> Bool {
        let startBy = DispatchTime.now() + startLimit
        while !started() {
            let now = DispatchTime.now()
            if now >= startBy { return true }
            if finished.wait(timeout: min(now + startPollInterval, startBy)) == .success { return false }
        }
        return finished.wait(timeout: .now() + timeout) == .timedOut
    }

    /// Whether `pid` has run any code of its own: its user CPU time is above
    /// zero. A child Process starts with posix_spawn, so it runs nothing in
    /// user space before exec, and while macOS checks a program on its first
    /// run the child waits inside exec with none (measured: zero until the
    /// check ends, 0.7 to 0.9 s on a fresh script, then above zero at once).
    /// One that can't be asked about, gone already, has nothing left to wait
    /// for to start.
    @Sendable
    static func hasRun(_ pid: pid_t) -> Bool {
        var usage = rusage_info_v2()
        let result = withUnsafeMutablePointer(to: &usage) { pointer in
            pointer.withMemoryRebound(to: rusage_info_t?.self, capacity: 1) {
                proc_pid_rusage(pid, RUSAGE_INFO_V2, $0)
            }
        }
        return result != 0 || usage.ri_user_time > 0
    }

    /// Read `fd` into `output` until EOF, the end marker, the cap, or `stop`.
    ///
    /// `poll` with a short timeout rather than a blocking `read`, so `stop`
    /// is noticed within a tenth of a second even when no byte ever arrives.
    private static func readUntilDone(_ fd: Int32, into output: MarkedOutput, stop: StopFlag) {
        var buffer = [UInt8](repeating: 0, count: 64 * 1024)
        while !stop.isSet {
            var pfd = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
            let ready = poll(&pfd, 1, 100)
            if ready < 0 {
                if errno == EINTR { continue }
                return
            }
            if ready == 0 { continue }
            let count = buffer.withUnsafeMutableBytes { read(fd, $0.baseAddress, $0.count) }
            if count < 0 {
                if errno == EINTR || errno == EAGAIN { continue }
                return
            }
            if count == 0 { return }
            if output.append(Data(buffer[0..<count])) { return }
        }
    }
}

/// The login-shell environment, started once and awaited by every CLI launch.
///
/// A `Task` rather than a cached optional, so "not resolved yet" and "resolved
/// to nothing" stay different states: a launch that arrives first waits for
/// the answer instead of racing ahead with launchd's environment and running
/// against the wrong config directory. The wait is bounded, because the
/// resolution is: `LoginShell.timeout` once the shell is running, after at most
/// `LoginShell.startLimit` for it to start.
///
/// **Re-resolvable, when asked.** The setup screen's Check again is pressed by
/// someone who just changed their setup in Terminal — `pipx ensurepath`, a
/// `PATH` line in `~/.zshrc` — and an answer read once at launch could never
/// see it: the CLI stayed "not installed" until the app was relaunched.
/// `refresh()` starts a new resolution; launches after it wait for that one.
///
/// **Only when asked, and not often** (whole-diff review, app-1). Each ask runs
/// the user's startup files, `<shell> -l -i -c`. It used to happen on every
/// visit to Settings and on every return to the window during setup, and a
/// startup file that runs a Command Line Tools stub (`git`, `python3`) with
/// the tools not installed then put up macOS's "install the command line
/// developer tools" dialog each time: dismissing it returned focus, which
/// asked again. Each ask also made the next CLI launches wait for it. So the
/// launch-time answer is reused, a new one is asked for only by an explicit
/// Check again or Re-run, and `refresh()` asks at most once per
/// `minimumInterval`.
final class ShellEnvironment: @unchecked Sendable {
    /// The least time between two asks of the shell: launch, then each
    /// `refresh()` that asked. A refresh sooner than this keeps the answer
    /// the last ask gets.
    static let minimumRefreshInterval: TimeInterval = 10

    /// Said beside Check again or Re-run when `refresh()` kept the last
    /// answer: otherwise a second press soon after saving a startup file
    /// looked as if the shell had been asked and the new line was wrong (the
    /// try14 delta audit's D14-APP-3).
    static let askedTooRecentlyNote =
        "Graft asked your shell less than \(Int(minimumRefreshInterval)) seconds ago, so it used "
        + "that answer. If you just saved a change to a startup file, wait a few seconds and try again."

    /// Seconds on a clock that only moves forward and keeps counting while
    /// the Mac sleeps (CLOCK_MONOTONIC). Not the wall clock: one set back
    /// after launch, by hand or by a time server, made the interval since
    /// the last ask negative, and every refresh was refused until the wall
    /// clock caught up again, however long that was (D14-APP-3).
    static func monotonicNow() -> TimeInterval {
        TimeInterval(clock_gettime_nsec_np(CLOCK_MONOTONIC)) / 1_000_000_000
    }

    private let resolve: @Sendable () async -> [String: String]?
    private let minimumInterval: TimeInterval
    private let now: @Sendable () -> TimeInterval
    private let lock = NSLock()
    private var task: Task<[String: String]?, Never>
    /// When the shell was last asked, on `now`'s clock.
    private var askedAt: TimeInterval
    /// Whether the last `refresh()` kept the last answer for the interval.
    private var tooSoon = false

    /// `now` and `minimumInterval` are for the tests of the interval.
    init(
        minimumInterval: TimeInterval = ShellEnvironment.minimumRefreshInterval,
        now: @escaping @Sendable () -> TimeInterval = { ShellEnvironment.monotonicNow() },
        resolve: @escaping @Sendable () async -> [String: String]?
    ) {
        self.resolve = resolve
        self.minimumInterval = minimumInterval
        self.now = now
        askedAt = now()
        task = Self.start(resolve)
    }

    /// Detached: started from the main actor at launch, and it must not run
    /// there. The child process and its pipe are read on GCD queues.
    private static func start(
        _ resolve: @escaping @Sendable () async -> [String: String]?
    ) -> Task<[String: String]?, Never> {
        Task.detached(priority: .userInitiated) { await resolve() }
    }

    /// The environment the shell reported, or nil when it could not be read.
    var value: [String: String]? {
        get async {
            let current = lock.withLock { task }
            return await current.value
        }
    }

    /// Ask the shell again, unless it was asked less than `minimumInterval`
    /// ago: then nothing starts, and `value` stays that ask's answer. Returns
    /// whether it asked. Callers that are already waiting keep waiting for the
    /// answer they asked for; every later `value` is the new one.
    ///
    /// **A refresh that gets no answer keeps the last one that did.** The
    /// shell is asked under a timeout, and a slow `~/.zshrc` on a busy Mac can
    /// miss it; replacing a good environment with nil then dropped every
    /// launch after it back to launchd's bare PATH — so Check again, pressed
    /// to *find* a newly installed `graft`, could lose the one the app had
    /// been running all along. An environment that was right a moment ago is
    /// a better answer than none.
    @discardableResult
    func refresh() -> Bool {
        let resolve = self.resolve
        return lock.withLock {
            let at = now()
            let since = at - askedAt
            // Backward only with a clock that isn't the default one: read as
            // long enough rather than refusing until it catches up.
            tooSoon = since >= 0 && since < minimumInterval
            guard !tooSoon else { return false }
            askedAt = at
            let previous = task
            task = Task.detached(priority: .userInitiated) { () -> [String: String]? in
                if let fresh = await resolve() { return fresh }
                return await previous.value
            }
            return true
        }
    }

    /// Whether the last `refresh()` kept the answer it already had, because
    /// the shell was asked less than `minimumInterval` before
    /// (`askedTooRecentlyNote`). False before any refresh.
    var lastRefreshWasTooSoon: Bool { lock.withLock { tooSoon } }

    /// The app's one resolution, started the first time anything asks —
    /// `GraftApp.init`, so it runs while the window is still being built.
    static let shared = ShellEnvironment {
        await LoginShell.resolve(shell: LoginShell.userShell(), run: LoginShell.runProcess)
    }
}

/// What the shell printed from the start marker on, bounded.
///
/// **Nothing before the start marker is kept** beyond the few bytes that could
/// be the first half of it. A startup file can print anything before the
/// command runs — a banner, a `brew` warning, or, wrongly, a `cat` of
/// something enormous — and this used to buffer all of it for up to the whole
/// timeout. From the marker on the environment itself is bounded by
/// `ARG_MAX`, so the cap (`LoginShell.outputCap`) is a ceiling a real answer
/// never reaches; one that does is abandoned.
final class MarkedOutput: @unchecked Sendable {
    private let start: Data
    private let end: Data
    private let cap: Int
    private var buffer = Data()
    private var foundStart = false
    private var overflowed = false
    private var anything = false
    private let lock = NSLock()

    init(markers: LoginShell.Markers, cap: Int) {
        self.start = Data(markers.start.utf8)
        self.end = Data(markers.end.utf8)
        self.cap = cap
    }

    /// Appends `chunk`, and reports whether reading can stop: the end marker
    /// has arrived, or the cap was passed.
    func append(_ chunk: Data) -> Bool {
        lock.lock(); defer { lock.unlock() }
        if !chunk.isEmpty { anything = true }
        guard !overflowed else { return true }
        buffer.append(chunk)
        if !foundStart {
            guard let marker = buffer.range(of: start) else {
                // Only enough to complete a marker split across two reads.
                let keep = start.count - 1
                if buffer.count > keep { buffer = Data(buffer.suffix(keep)) }
                return false
            }
            buffer = Data(buffer[marker.lowerBound...])
            foundStart = true
        }
        if buffer.count > cap {
            overflowed = true
            buffer = Data()
            return true
        }
        // Only the tail can newly contain it: the chunk plus enough before it
        // to catch a marker split across two reads.
        let from = max(buffer.startIndex, buffer.endIndex - chunk.count - end.count)
        return buffer.range(of: end, in: from..<buffer.endIndex) != nil
    }

    /// The bytes from the start marker on; empty when it never arrived or
    /// the cap was passed, which `LoginShell.parse` reads as no answer.
    var data: Data {
        lock.lock(); defer { lock.unlock() }
        return foundStart && !overflowed ? buffer : Data()
    }

    /// Whether the shell has written anything at all, marker or not: then it
    /// is running (`LoginShell.waitForAnswer`).
    var receivedAny: Bool {
        lock.lock(); defer { lock.unlock() }
        return anything
    }

    /// How many bytes are held right now — for the test that the cap holds.
    var bufferedCount: Int {
        lock.lock(); defer { lock.unlock() }
        return buffer.count
    }
}

/// Set once, read from another thread.
private final class StopFlag: @unchecked Sendable {
    private var value = false
    private let lock = NSLock()

    var isSet: Bool { lock.withLock { value } }
    func set() { lock.withLock { value = true } }
}
