import CoreServices
import Foundation

/// Running the CLI's enrollment script as root, behind macOS's administrator
/// prompt (controller decision 7: macOS's own dialog, explained first by
/// `AppStoreEnrollSheet`).
///
/// The script comes from `--appstore-enroll-command` and nowhere else
/// (`graft_ipa/enrollment.py` `root_script`), every value in it already a
/// single-quoted literal, and the app adds nothing to it. Root works only in
/// a folder it makes itself, from its own verified copy of UnfairPlay; the
/// user's model folder is filled afterward, as the user
/// (`--appstore-enroll-finish`).
///
/// The password is typed into macOS's own authorization dialog
/// (SecurityAgent). This process never sees it.
enum AdminShell {
    /// What `with prompt` asks macOS to show. Task 0 found macOS 27 ignores
    /// it and words the dialog itself (spike appendix, "Admin prompt"); it is
    /// passed anyway, for a macOS that shows it.
    static let prompt = "Graft needs an administrator password to set up App Store decryption on this Mac."

    /// macOS's dialog, word for word, as the owner's hands-on run of the
    /// notarized try12 recorded it when Graft.app started the script through
    /// `AppleScriptAdminShell`: it names Graft, offers Touch ID, and has two
    /// buttons, Use Password… and Cancel. It does *not* say "Apple could not
    /// verify this script is free of malware": that sentence came from Task
    /// 0's spike, where a launchd job raised the prompt, and the enroll sheet
    /// no longer quotes it. Through the osascript fallback the dialog would
    /// name osascript instead.
    static let recordedDialogWording = "Allow administrator access for \u{201C}Graft\u{201D}? This will allow \u{201C}Graft\u{201D} to access and modify your data, files, and settings on this Mac. Touch ID or enter your password to continue with \u{201C}Graft\u{201D}."

    /// The dialog's buttons, as recorded: the sheet says Cancel, never
    /// "Don't Allow", which this dialog doesn't have.
    static let recordedDialogButtons = ["Use Password\u{2026}", "Cancel"]

    /// This process is a test host: XCTest is loaded, or Xcode's test runner
    /// configured it. The two shells that can raise macOS's administrator
    /// prompt (`AppleScriptAdminShell`, `OsascriptAdminShell`) refuse to run
    /// here, before building or starting anything.
    ///
    /// Why a check in the app and not only "tests inject a recording shell":
    /// on 2026-11-22 a test built an `AppStoreStore` (whose account store
    /// defaults to `FallbackAdminShell`) and relied on the code under test to
    /// stop before the shell. A mutation run removed that stop, and a real
    /// administrator dialog appeared in front of the owner. A test's safety
    /// can't depend on the code it tests. Never mutate this check.
    static var insideTests: Bool {
        isTestHost(environment: ProcessInfo.processInfo.environment, classNamed: NSClassFromString)
    }

    /// `insideTests`'s predicate, pure, so a test can ask it each way
    /// without changing the real one (the fix-check's critic: it was checked
    /// only by where it sits). Either variable Xcode's test runner sets, or
    /// XCTest's own class loaded in this process.
    static func isTestHost(environment: [String: String], classNamed: (String) -> AnyClass?) -> Bool {
        environment["XCTestConfigurationFilePath"] != nil
            || environment["XCTestSessionIdentifier"] != nil
            || classNamed("XCTestCase") != nil
    }

    /// What the real shells throw in a test host: a failure (not a Cancel,
    /// not `unavailable`, so `FallbackAdminShell` never tries the other).
    static let refusedInTests = AdminShellError(
        number: 0,
        message: "refused: a test reached the real administrator prompt; give the store a recording shell"
    )

    /// What root's `/bin/sh`, which is bash 3.2, would take from the
    /// environment it inherits and act on: exported functions
    /// (`BASH_FUNC_<name>%%`, a `kill` or `cp` of anyone's choosing), the
    /// variables that set its options or run code as it goes (`SHELLOPTS`,
    /// `BASHOPTS`, `PS4`, `ENV`, `BASH_ENV`), and the ones that change what
    /// the script's own words mean (`CDPATH`, `GLOBIGNORE`).
    ///
    /// `do shell script … with administrator privileges` starts root's shell
    /// with this app's environment. macOS sets `_BASH_IMPLICIT_DASH_PEE` on
    /// that path, which puts bash in privileged mode, so it imports none of
    /// these; the whole-diff review checked that an injected `kill` runs
    /// without it and not with it. That is Apple's to change, so Graft, which
    /// needs none of them, doesn't hand them over (`removeShellImports`, at
    /// launch), and gives osascript an environment of its own
    /// (`OsascriptAdminShell.environment`).
    static let shellImportNames: Set<String> = [
        "SHELLOPTS", "BASHOPTS", "PS4", "ENV", "BASH_ENV", "CDPATH", "GLOBIGNORE",
    ]

    /// The names in `environment` that `shellImportNames` and the
    /// `BASH_FUNC_` prefix cover, sorted. Pure: a test asks it about any
    /// environment without touching this process's.
    static func shellImports(in environment: [String: String]) -> [String] {
        environment.keys.filter { $0.hasPrefix("BASH_FUNC_") || shellImportNames.contains($0) }.sorted()
    }

    /// Takes `shellImports` out of this process's environment, and says which
    /// it took. Once, at launch, as `GraftApp`'s first property: `unsetenv`
    /// isn't safe while another thread reads the environment, and the next
    /// property's runner starts the login shell on one. Nothing in Graft sets
    /// any of them again, so no later run of the administrator step, either
    /// way, can hand one to root's shell.
    @discardableResult
    static func removeShellImports() -> [String] {
        let names = shellImports(in: ProcessInfo.processInfo.environment)
        for name in names { unsetenv(name) }
        return names
    }

    /// The script's last non-empty line of output: the enrollment script
    /// prints the folder root made, and only that, last.
    static func lastLine(_ output: String) -> String? {
        output.split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .last { !$0.isEmpty }
    }
}

/// What a privileged run printed, and which way it ran (decision 6 asks for
/// the record: in-process `NSAppleScript`, or the `osascript` fallback).
struct AdminRun: Equatable, Sendable {
    enum Via: String, Equatable, Sendable {
        case appleScript = "NSAppleScript"
        case osascript
        /// Not privileged at all: tests.
        case plain
    }

    let output: String
    let via: Via
}

/// Runs a `/bin/sh` script as root. A protocol so tests can run it unprivileged.
protocol PrivilegedShell: Sendable {
    func run(_ script: String, prompt: String) async throws -> AdminRun
}

struct AdminShellError: LocalizedError, Equatable {
    /// AppleScript's error number: -128 is the user pressing Cancel, and a
    /// positive one is the script's exit status.
    let number: Int
    let message: String
    /// The AppleScript machinery itself couldn't run here (it never got as far
    /// as a dialog), so another way may: `FallbackAdminShell`.
    var unavailable = false

    static let userCancelled = -128

    var cancelled: Bool { number == Self.userCancelled }
    var errorDescription: String? { cancelled ? "Canceled: nothing was changed." : message }

    /// Errors that mean AppleScript couldn't run the handler at all, before
    /// any dialog: OSA system/component errors (-1750, -1751), no such
    /// handler (-1708), and an Apple event this process may not send (-1743).
    /// Never -128, never a script's exit status.
    ///
    /// Not -10000 (errAEEventFailed), AppleScript's catch-all: `do shell
    /// script` can report it after root's script ran, and falling back then
    /// would raise a second administrator prompt and run root's script again,
    /// with a new folder and a second UnfairPlay setup (the fix-check's
    /// critic). A -10000 is a plain failure, with its message, and the user
    /// decides whether to try again.
    static let machineryErrors: Set<Int> = [-1750, -1751, -1708, -1743]
}

/// `do shell script … with administrator privileges`, in-process, through
/// `NSAppleScript`, so the dialog names Graft rather than a helper process.
///
/// The script and the prompt are passed as Apple event *parameters* to a
/// handler in a constant source, never spliced into AppleScript source, so
/// AppleScript's own quoting never comes into it. Each call runs on a private
/// serial queue with its own `NSAppleScript`, so the main thread isn't held
/// while macOS shows the prompt and UnfairPlay works. That this works in the
/// signed, hardened app is Task 21's hands-on check; `OsascriptAdminShell` is
/// the fallback (`FallbackAdminShell`).
///
/// No AppleScript reserved word is a variable name here: `up` is one, and a
/// spike script that used it failed with -10003 before any prompt.
struct AppleScriptAdminShell: PrivilegedShell {
    static let handler = "graftRunAsAdmin"
    static let source = """
    on graftRunAsAdmin(shellText, promptText)
    \treturn do shell script shellText with prompt promptText with administrator privileges without altering line endings
    end graftRunAsAdmin
    """
    /// The handler's parameter names, for the reserved-word check.
    static let variables = ["shellText", "promptText"]

    // FourCCs from OpenScripting's ASRegistry.h.
    static let ascr: AEEventClass = 0x6173_6372  // kASAppleScriptSuite
    static let psbr: AEEventID = 0x7073_6272     // kASSubroutineEvent
    static let snam: AEKeyword = 0x736E_616D     // keyASSubroutineName

    private static let queue = DispatchQueue(label: "graft.admin-shell")

    func run(_ script: String, prompt: String) async throws -> AdminRun {
        if AdminShell.insideTests { throw AdminShell.refusedInTests }
        return try await withCheckedThrowingContinuation { continuation in
            Self.queue.async {
                do {
                    let output = try Self.runNow(script, prompt: prompt)
                    continuation.resume(returning: AdminRun(output: output, via: .appleScript))
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
    }

    private static func runNow(_ script: String, prompt: String) throws -> String {
        guard let apple = NSAppleScript(source: source) else {
            throw AdminShellError(number: 0, message: "Graft couldn't load its administrator script", unavailable: true)
        }
        var error: NSDictionary?
        guard apple.compileAndReturnError(&error) else {
            throw AdminShellError(number: 0, message: "Graft couldn't compile its administrator script", unavailable: true)
        }
        let result = apple.executeAppleEvent(event(script: script, prompt: prompt), error: &error)
        if let error {
            throw failure(
                number: (error[NSAppleScript.errorNumber] as? Int) ?? 0,
                message: (error[NSAppleScript.errorMessage] as? String) ?? "the administrator command failed"
            )
        }
        return result.stringValue ?? ""
    }

    /// What a failed `executeAppleEvent` throws: `unavailable`, so
    /// `FallbackAdminShell` tries osascript, only for a machinery error.
    /// Here, not inline, so a test builds the very error `runNow` would.
    static func failure(number: Int, message: String) -> AdminShellError {
        AdminShellError(number: number, message: message, unavailable: AdminShellError.machineryErrors.contains(number))
    }

    /// The event `runNow` executes: `graftRunAsAdmin(shellText, promptText)`,
    /// so the script first and the prompt second, matching the handler's
    /// parameters in `source`. Swapped, macOS would show the script as its
    /// prompt and root would run the prompt sentence through /bin/sh. Built
    /// here, not inline, so a test can check the order without executing
    /// anything (review part 4, #33).
    static func event(script: String, prompt: String) -> NSAppleEventDescriptor {
        call(handler, script, prompt)
    }

    /// `handler(arguments…)` as a subroutine event to this script.
    static func call(_ handler: String, _ arguments: String...) -> NSAppleEventDescriptor {
        let event = NSAppleEventDescriptor(
            eventClass: ascr, eventID: psbr, targetDescriptor: .currentProcess(),
            returnID: AEReturnID(kAutoGenerateReturnID), transactionID: AETransactionID(kAnyTransactionID)
        )
        event.setParam(NSAppleEventDescriptor(string: handler.lowercased()), forKeyword: snam)
        let list = NSAppleEventDescriptor.list()
        for (index, argument) in arguments.enumerated() {
            list.insert(NSAppleEventDescriptor(string: argument), at: index + 1)
        }
        event.setParam(list, forKeyword: AEKeyword(keyDirectObject))
        return event
    }
}

/// The fallback: `/usr/bin/osascript` as a child, the same handler body, the
/// script and the prompt as *arguments* (`argv`), never as source. The dialog
/// then names osascript.
struct OsascriptAdminShell: PrivilegedShell {
    static let source = "on run argv\n\treturn do shell script (item 1 of argv) with prompt (item 2 of argv) with administrator privileges without altering line endings\nend run"

    static func arguments(script: String, prompt: String) -> [String] {
        ["-e", source, script, prompt]
    }

    /// What a failed osascript run means, from its stderr and exit status.
    ///
    /// osascript ends a failure's message with AppleScript's error number:
    /// "execution error: User canceled. (-128)" for Cancel, and for a script
    /// that failed, "execution error: <the script's stderr> (<its exit
    /// status>)". Only that *trailing* number counts. The script's stderr
    /// comes first and can say anything, an app path like
    /// "/Applications/Foo (-128).app" included, and matching "(-128)" anywhere
    /// in it turned a root run that failed into "Canceled: nothing was
    /// changed" (review part 4, #17). The trailing number is also the root
    /// script's own exit status, which is what tells a stop (67) or the time
    /// limit (68) apart. No trailing number: osascript's own exit status.
    static func failure(status: Int32, stderr said: String) -> AdminShellError {
        let message = said.trimmingCharacters(in: .whitespacesAndNewlines)
        return AdminShellError(number: trailingErrorNumber(message) ?? Int(status), message: message)
    }

    static func trailingErrorNumber(_ message: String) -> Int? {
        guard let match = message.firstMatch(of: /\((-?\d+)\)\s*$/) else { return nil }
        return Int(match.1)
    }

    /// osascript's environment, and so root's `/bin/sh`'s: a fixed PATH, and
    /// HOME, USER and LANG from `parent` when it has them. Nothing else, so
    /// nothing in this app's environment (`AdminShell.shellImports`, or
    /// anything else) reaches root's shell through the fallback.
    static func environment(from parent: [String: String]) -> [String: String] {
        var environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin"]
        for name in ["HOME", "USER", "LANG"] {
            if let value = parent[name] { environment[name] = value }
        }
        return environment
    }

    /// What a child printed, and how it ended.
    struct Ran: Equatable {
        let status: Int32
        let output: Data
        let error: Data
    }

    /// Runs `executable` with exactly `environment`, reads its stdout and its
    /// stderr at the same time, each to EOF, then waits for it.
    ///
    /// Not one after the other: reading stdout to EOF first, as this did,
    /// deadlocks once the child has written about 64 KB to stderr. The child
    /// blocks writing it and never closes stdout, and this never gets to
    /// stderr (the whole-diff review). A root run that fails cats UnfairPlay's
    /// log to stderr, which can be that long. As `CLIRunner` drains both
    /// (`drainToEOF`). Generic, so a test runs it on `/bin/sh`, never on
    /// osascript.
    static func runToEnd(_ executable: URL, arguments: [String], environment: [String: String]) throws -> Ran {
        let proc = Process()
        proc.executableURL = executable
        proc.arguments = arguments
        proc.environment = environment
        let out = Pipe(), err = Pipe()
        proc.standardOutput = out
        proc.standardError = err
        try proc.run()
        let drained = DispatchGroup()
        let output = Drained(), said = Drained()
        for (pipe, into) in [(out, output), (err, said)] {
            DispatchQueue.global(qos: .userInitiated).async(group: drained) {
                into.set(pipe.fileHandleForReading.readDataToEndOfFile())
            }
        }
        drained.wait()
        proc.waitUntilExit()
        return Ran(status: proc.terminationStatus, output: output.data, error: said.data)
    }

    /// One pipe's bytes, handed from its reader's thread.
    private final class Drained: @unchecked Sendable {
        private let lock = NSLock()
        private var bytes = Data()

        func set(_ data: Data) {
            lock.lock(); defer { lock.unlock() }
            bytes = data
        }

        var data: Data {
            lock.lock(); defer { lock.unlock() }
            return bytes
        }
    }

    func run(_ script: String, prompt: String) async throws -> AdminRun {
        if AdminShell.insideTests { throw AdminShell.refusedInTests }
        return try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                let ran: Ran
                do {
                    ran = try Self.runToEnd(
                        URL(fileURLWithPath: "/usr/bin/osascript"),
                        arguments: Self.arguments(script: script, prompt: prompt),
                        environment: Self.environment(from: ProcessInfo.processInfo.environment)
                    )
                } catch {
                    return continuation.resume(throwing: AdminShellError(
                        number: 0, message: "couldn't run osascript: \(error.localizedDescription)"
                    ))
                }
                guard ran.status == 0 else {
                    let said = String(decoding: ran.error, as: UTF8.self)
                    return continuation.resume(throwing: Self.failure(status: ran.status, stderr: said))
                }
                continuation.resume(returning: AdminRun(output: String(decoding: ran.output, as: UTF8.self), via: .osascript))
            }
        }
    }
}

/// `primary`, and `fallback` only when the primary's AppleScript machinery
/// couldn't run at all (`AdminShellError.unavailable`): never after a Cancel,
/// a wrong password or the script's own failure, which would ask again.
///
/// Neither shell has a default. The real pair, `AppleScriptAdminShell` then
/// `OsascriptAdminShell`, is named in one place, `GraftApp`; everything else
/// is handed its shell, so a test that forgets its fake doesn't compile
/// (`NoRealDefaultsTests`).
struct FallbackAdminShell: PrivilegedShell {
    let primary: PrivilegedShell
    let fallback: PrivilegedShell

    func run(_ script: String, prompt: String) async throws -> AdminRun {
        do {
            return try await primary.run(script, prompt: prompt)
        } catch let error as AdminShellError where error.unavailable {
            return try await fallback.run(script, prompt: prompt)
        }
    }
}
