import Darwin
import Foundation

/// What a new user still has to do before the app can work.
///
/// Read off the failure of the app's first CLI call (`ConfigStore.checkSetup`)
/// rather than probed for separately, because that call is the one that has
/// to work: an app that checked for a config file on its own could find one
/// the CLI cannot read, or miss one at an `XDG_CONFIG_HOME` the CLI honours.
///
/// Both cases used to be modal alerts — "Couldn't run the graft command-line
/// tool…" and "Something didn't work: config file not found at … hint: run
/// `graft --init`" — which is the first thing a stranger saw on opening the
/// app, phrased as a fault. They are a setup step, and `SetupView` shows them
/// as one.
///
/// A release build never asks for Terminal: `.cli` and `.brokenCLI` can't
/// arise from its bundled CLI, and its config step is the in-app account
/// setup (`SetupView`).
enum SetupNeed: Equatable {
    /// The `graft` CLI could not be started: it is not installed anywhere the
    /// app looks.
    case cli
    /// Something is installed where graft should be, and it would not start.
    ///
    /// Not `.cli`, because the remedy differs and the old one was a loop: a
    /// pipx venv whose Python Homebrew has since removed still has its
    /// `graft` script, `pipx install` answers "already installed", and the
    /// screen asked again. What fixes it is a reinstall, and what the user
    /// needs to see is which file it was and why it would not run.
    case brokenCLI(BrokenCLI)
    /// Release builds: the app's own CLI, `Contents/Helpers/graft`, would not
    /// start. Nothing installed in Terminal is ever run in its place
    /// (`GraftExecutable.resolve`), and neither `.cli`'s install nor
    /// `.brokenCLI`'s reinstall reaches inside the bundle. Usually part of
    /// Graft.app is missing or broken, and the remedy is a fresh copy; the
    /// payload says when it is something else (`BundledCLIFailure`).
    case damagedApp(BundledCLIFailure = .damaged)
    /// The CLI runs but has no config.yaml. `path` is where it looked, as it
    /// said so, or nil if the message carried none.
    case config(path: String?)
    /// The config exists, and `--check` reports tools missing that a build
    /// needs. Only on the way through setup — see `ConfigStore.checkSetup`;
    /// once the app is in use, the same rows are a notice, not a screen.
    case tools([MissingTool])

    /// The setup step this failure means, or nil when it is an ordinary error
    /// that belongs in the alert it has always gone to.
    ///
    /// Structured where a structure exists: a launch failure is its own error
    /// case. A missing config is not — `ConfigError` exits 2 for every config
    /// problem, and Click's usage errors exit 2 too — so the CLI's own sentence
    /// decides, matched as a prefix of the parsed `error:` line, never as a
    /// substring of raw stderr (where a path or a hint could contain it).
    init?(error: Error) {
        if case let CLIRunner.RunError.launchFailed(executable, underlying) = error {
            // Before `diagnose`: a bundled launcher that is there but will not
            // start is still the app's damage, not a pipx venv to rebuild.
            if GraftExecutable.isBundled(executable) {
                self = .damagedApp(BundledCLIFailure.diagnose(executable: executable))
            } else if let broken = BrokenCLI.diagnose(path: executable, underlying: underlying) {
                self = .brokenCLI(broken)
            } else {
                self = .cli
            }
            return
        }
        // The launcher started and could not start the app's Python: it says
        // so in its own words and status (`bundle/launcher.c`). The status
        // alone is a shell's "found but not executable" too, so the sentence
        // decides, as the first thing on stderr.
        if case let CLIError.exited(_, code, stderr) = error,
           code == CLIContract.Exit.bundleDamaged,
           stderr.drop(while: \.isWhitespace).hasPrefix(CLIContract.Message.bundleDamaged) {
            self = .damagedApp(.damaged)
            return
        }
        guard case let CLIError.exited(_, code, stderr) = error,
              code == CLIContract.Exit.configError,
              let message = CLIFailure.parse(stderr: stderr),
              message.error.hasPrefix(CLIContract.Message.configNotFound)
        else { return nil }
        let path = String(message.error.dropFirst(CLIContract.Message.configNotFound.count))
            .trimmingCharacters(in: .whitespaces)
        self = .config(path: path.isEmpty ? nil : path)
    }

    /// The step this need is, counting from one: the setup screen shows all
    /// of them and marks which the user is on.
    var step: Int {
        switch self {
        case .cli, .brokenCLI, .damagedApp: return 1
        case .config: return 2
        case .tools: return 3
        }
    }

    /// How many steps the setup screen lists.
    static let stepCount = 3

    /// The app's own CLI would not start, for whatever reason.
    var isDamagedApp: Bool {
        if case .damagedApp = self { return true }
        return false
    }

    /// What `.damagedApp(.damaged)` says, on the setup screen and in any
    /// banner a failed launch of the bundled CLI puts up.
    /// Where to get it as every doc says: GitHub Releases, and the project's
    /// own site (whole-diff review, docs-5).
    static let damagedAppMessage = "Graft.app is damaged: part of it is missing. "
        + "Download Graft again from GitHub Releases (github.com/GreenglassT/Graft/releases) "
        + "or graftipa.com and replace this copy."
}

/// Why a release build's own CLI, `Contents/Helpers/graft`, would not start,
/// read off the bundle it is in. Each has a different remedy, and "download
/// Graft again" is the right one only for the first.
enum BundledCLIFailure: Equatable {
    /// Part of Graft.app is missing or broken: a fresh copy is the remedy.
    case damaged
    /// The app has no `Contents/Helpers` at all. That is a plain Xcode
    /// Release build, not one `scripts/graft-release` made: a Release build
    /// only ever runs the CLI inside it, and only the release script puts
    /// one there.
    case builtWithoutCLI
    /// Nothing is at the path the app was opened from any more: it was
    /// moved or renamed while open. The app finds its CLI from the path it
    /// was opened at (`CLIRunner.forApp`), so reopening it is the remedy.
    case moved

    /// `executable` is `<app>/Contents/Helpers/graft`.
    static func diagnose(
        executable: String, exists: (String) -> Bool = { FileManager.default.fileExists(atPath: $0) }
    ) -> BundledCLIFailure {
        let contents = ((executable as NSString).deletingLastPathComponent as NSString)
            .deletingLastPathComponent
        if !exists((contents as NSString).appendingPathComponent("Info.plist")) { return .moved }
        if !exists((contents as NSString).appendingPathComponent("Helpers")) { return .builtWithoutCLI }
        return .damaged
    }

    /// The setup screen's step, and the start of every banner.
    var message: String {
        switch self {
        case .damaged:
            return SetupNeed.damagedAppMessage
        case .builtWithoutCLI:
            return Self.builtWithoutCLIMessage
        case .moved:
            return Self.movedMessage
        }
    }

    var stepTitle: String {
        switch self {
        case .damaged: return "Download Graft again"
        case .builtWithoutCLI: return "Build Graft with its command-line tool"
        case .moved: return "Quit Graft and open it again"
        }
    }

    static let builtWithoutCLIMessage = "This copy of Graft.app has no command-line tool inside it "
        + "(no Contents/Helpers), which is what a plain Xcode Release build looks like. "
        + "The download is built by scripts/graft-release. To run a build of your own, build "
        + "the Debug configuration, which runs the graft it finds on your PATH."

    static let movedMessage = "Graft.app was moved or renamed while it was open, so it can't "
        + "find its command-line tool. Quit Graft and open it again."
}

/// An installed `graft` that would not start, and why.
struct BrokenCLI: Equatable {
    /// The file the app tried to run.
    let path: String
    /// The system's error number for the failed launch, when the launch
    /// reported one. Foundation checks for a missing or non-executable file
    /// itself and reports those without one.
    let errno: Int32?
    /// What is wrong with the file, in a sentence fragment that follows "it
    /// won't start: ".
    let reason: String

    /// `errno` the way a user can search for it: "error 2: No such file or
    /// directory". Nil when the launch carried no number.
    var errnoLine: String? {
        errno.map { "error \($0): \(String(cString: strerror($0)))" }
    }

    /// What is at `path`, or nil when nothing is — the plain not-installed
    /// case, which stays `.cli`.
    ///
    /// Reads the filesystem: a launch failure says only that `posix_spawn`
    /// refused, and the reasons it refuses for an *existing* file need
    /// different words. ENOENT for a file that is plainly there is the
    /// interpreter on its `#!` line being gone, which is what a pipx venv
    /// looks like after Homebrew upgrades the Python under it.
    static func diagnose(path: String, underlying: Error) -> BrokenCLI? {
        var link = stat()
        guard lstat(path, &link) == 0 else { return nil }
        let reported = underlying as NSError
        let errno: Int32? = reported.domain == NSPOSIXErrorDomain ? Int32(reported.code) : nil

        var info = stat()
        guard stat(path, &info) == 0 else {
            let target = (try? FileManager.default.destinationOfSymbolicLink(atPath: path))
                .map { " to \($0)" } ?? ""
            return BrokenCLI(
                path: path, errno: errno ?? ENOENT,
                reason: "it is a link\(target), and what it points to is gone"
            )
        }
        switch info.st_mode & S_IFMT {
        case S_IFREG: break
        case S_IFDIR:
            return BrokenCLI(path: path, errno: errno, reason: "it is a folder, not a program")
        default:
            return BrokenCLI(path: path, errno: errno, reason: "it is not a regular file")
        }
        guard access(path, X_OK) == 0 else {
            return BrokenCLI(path: path, errno: errno ?? EACCES, reason: "it is not marked as executable")
        }

        let head = (FileHandle(forReadingAtPath: path).flatMap { try? $0.read(upToCount: 512) }) ?? Data()
        if head.starts(with: Data("#!".utf8)) {
            let line = String(decoding: head.dropFirst(2).prefix { $0 != 0x0A }, as: UTF8.self)
            let interpreter = line.trimmingCharacters(in: .whitespaces)
                .split(separator: " ", maxSplits: 1).first.map(String.init) ?? ""
            if !interpreter.isEmpty, !isRunnable(interpreter) {
                return BrokenCLI(
                    path: path, errno: errno,
                    reason: "the Python it runs with, \(interpreter), is gone — usually because "
                        + "Homebrew upgraded or removed the Python pipx installed graft with"
                )
            }
        } else if !isMachO(head) {
            return BrokenCLI(
                path: path, errno: errno,
                reason: "it is neither a program nor a script with a #! line"
            )
        }
        return BrokenCLI(
            path: path, errno: errno,
            reason: errno.map { String(cString: strerror($0)) } ?? underlying.localizedDescription
        )
    }

    private static func isRunnable(_ path: String) -> Bool {
        var info = stat()
        return stat(path, &info) == 0 && (info.st_mode & S_IFMT) == S_IFREG && access(path, X_OK) == 0
    }

    /// The four Mach-O magics and the universal-binary one, either byte order.
    private static func isMachO(_ head: Data) -> Bool {
        guard head.count >= 4 else { return false }
        let magic = head.prefix(4).reduce(UInt32(0)) { $0 << 8 | UInt32($1) }
        let known: Set<UInt32> = [
            0xFEEDFACE, 0xFEEDFACF, 0xCEFAEDFE, 0xCFFAEDFE, 0xCAFEBABE, 0xBEBAFECA,
        ]
        return known.contains(magic)
    }
}

/// A tool `--check` reports missing, and the command that installs it.
struct MissingTool: Equatable, Identifiable, Hashable {
    let name: String
    let command: String

    var id: String { name }

    /// A failed row that names an install command; nil for every other row —
    /// a passing one, an information row, and a failure that is not a missing
    /// tool (no phone plugged in, a certificate about to expire), which has
    /// nothing to install and belongs in Settings.
    init?(check: PreflightCheck) {
        guard !check.ok, !check.isInfo, let command = check.installCommand else { return nil }
        self.name = check.name
        self.command = command
    }

    init(name: String, command: String) {
        self.name = name
        self.command = command
    }

    /// Every missing tool in a `--check`, in the CLI's order.
    static func all(in checks: [PreflightCheck]) -> [MissingTool] {
        checks.compactMap(MissingTool.init(check:))
    }
}
