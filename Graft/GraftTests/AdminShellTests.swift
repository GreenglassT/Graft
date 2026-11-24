import CoreServices
import XCTest
@testable import Graft

/// Release gate B.3, the app half. The script itself is the CLI's (built and
/// run as the user in tests/test_enroll_command_b3.py); here, that the app
/// hands it to macOS's administrator prompt as a *parameter* and adds
/// nothing, that the AppleScript compiles and uses no reserved word, and that
/// Cancel is safe. Nothing here runs anything privileged: the AppleScript is
/// compiled, never executed, and osascript is never started (`/bin/sh` and
/// `/usr/bin/env` stand in for it where a child has to run).
final class AdminShellTests: XCTestCase {
    /// AppleScript words that can't be variable names. `up` is the one a
    /// Task 0 spike tripped over (-10003).
    static let reserved: Set<String> = [
        "about", "above", "after", "against", "and", "apart", "around", "as", "aside", "at",
        "back", "before", "beginning", "behind", "below", "beneath", "beside", "between", "but",
        "by", "considering", "contain", "contains", "continue", "copy", "div", "does", "eighth",
        "else", "end", "equal", "equals", "error", "every", "exit", "false", "fifth", "first",
        "for", "fourth", "from", "front", "get", "given", "global", "if", "ignoring", "in",
        "instead", "into", "is", "it", "its", "last", "local", "me", "middle", "mod", "my",
        "ninth", "not", "of", "on", "onto", "or", "out", "over", "prop", "property", "put",
        "ref", "reference", "repeat", "return", "returning", "script", "second", "set", "seventh",
        "since", "sixth", "some", "tell", "tenth", "that", "the", "then", "third", "through",
        "thru", "timeout", "times", "to", "transaction", "true", "try", "until", "up", "where",
        "while", "whose", "with", "without", "result", "prompt",
    ]

    func test_the_script_goes_in_as_a_parameter_never_as_source() throws {
        let script = "set -eu\nD=$(/usr/bin/mktemp -d '/private/var/tmp/x.XXXXXXXX')\necho \"a'b\\\\c\"\n"
        let event = AppleScriptAdminShell.call(AppleScriptAdminShell.handler, script, "the prompt")
        XCTAssertEqual(event.paramDescriptor(forKeyword: AppleScriptAdminShell.snam)?.stringValue, "graftrunasadmin")
        let list = try XCTUnwrap(event.paramDescriptor(forKeyword: AEKeyword(keyDirectObject)))
        XCTAssertEqual(list.atIndex(1)?.stringValue, script, "byte for byte, nothing added")
        XCTAssertEqual(list.atIndex(2)?.stringValue, "the prompt")
        XCTAssertFalse(AppleScriptAdminShell.source.contains("mktemp"), "the source is a constant")
    }

    /// Review part 4, #33: the order `runNow` sends, not one the test built.
    /// The event is built and read back; nothing executes it.
    func test_run_now_sends_the_script_first_and_the_prompt_second() throws {
        let script = "set -eu\necho \"$D\"\n"
        let event = AppleScriptAdminShell.event(script: script, prompt: AdminShell.prompt)
        XCTAssertEqual(event.eventClass, AppleScriptAdminShell.ascr)
        XCTAssertEqual(event.eventID, AppleScriptAdminShell.psbr)
        XCTAssertEqual(event.paramDescriptor(forKeyword: AppleScriptAdminShell.snam)?.stringValue,
                       AppleScriptAdminShell.handler.lowercased())
        let list = try XCTUnwrap(event.paramDescriptor(forKeyword: AEKeyword(keyDirectObject)))
        XCTAssertEqual(list.numberOfItems, 2)
        XCTAssertEqual(list.atIndex(1)?.stringValue, script, "shellText, the handler's first parameter")
        XCTAssertEqual(list.atIndex(2)?.stringValue, AdminShell.prompt, "promptText, its second")
        XCTAssertTrue(AppleScriptAdminShell.source.contains("on graftRunAsAdmin(shellText, promptText)"),
                      "the handler takes them in that order")
    }

    func test_the_applescript_compiles() throws {
        // Compiling runs nothing: no prompt, no shell.
        let script = try XCTUnwrap(NSAppleScript(source: AppleScriptAdminShell.source))
        var error: NSDictionary?
        XCTAssertTrue(script.compileAndReturnError(&error), "\(String(describing: error))")
        let fallback = try XCTUnwrap(NSAppleScript(source: OsascriptAdminShell.source))
        XCTAssertTrue(fallback.compileAndReturnError(&error), "\(String(describing: error))")
    }

    func test_no_applescript_reserved_word_is_a_variable() {
        for name in AppleScriptAdminShell.variables {
            XCTAssertFalse(Self.reserved.contains(name.lowercased()), name)
            XCTAssertTrue(AppleScriptAdminShell.source.contains("(\(AppleScriptAdminShell.variables.joined(separator: ", ")))"))
        }
        XCTAssertTrue(Self.reserved.contains("up"), "positive control: the spike's word is on the list")
    }

    func test_the_fallback_passes_the_script_as_an_argument() {
        let args = OsascriptAdminShell.arguments(script: "echo 'x'", prompt: "p")
        XCTAssertEqual(args, ["-e", OsascriptAdminShell.source, "echo 'x'", "p"])
        XCTAssertFalse(OsascriptAdminShell.source.contains("echo"))
    }

    /// Review part 4, #17: osascript's message carries the root script's
    /// stderr, which can name an app with "(-128)" in it. Only the number
    /// osascript puts last says Cancel. osascript itself is never started.
    func test_the_fallback_reads_cancel_only_from_the_trailing_error_number() {
        let failed = OsascriptAdminShell.failure(
            status: 1,
            stderr: "0:180: execution error: realpath: /Applications/Foo (-128).app: No such file or directory (1)\n"
        )
        XCTAssertFalse(failed.cancelled, "an app path with (-128) in it is not a Cancel")
        XCTAssertEqual(failed.number, 1)
        XCTAssertEqual(failed.message, "0:180: execution error: realpath: /Applications/Foo (-128).app: No such file or directory (1)")

        let refused = OsascriptAdminShell.failure(
            status: 1, stderr: "0:180: execution error: graft: the app is not in the Applications folder (-128) (64)"
        )
        XCTAssertEqual(refused.number, 64, "the root script's own exit status")

        let cancelled = OsascriptAdminShell.failure(status: 1, stderr: "0:180: execution error: User canceled. (-128)\n")
        XCTAssertTrue(cancelled.cancelled)

        let stopped = OsascriptAdminShell.failure(status: 1, stderr: "0:180: execution error: graft: stopped (67)")
        XCTAssertEqual(stopped.number, 67)

        let bare = OsascriptAdminShell.failure(status: 2, stderr: "osascript: couldn't start\n")
        XCTAssertEqual(bare.number, 2, "no trailing number: osascript's own status")
        XCTAssertFalse(bare.cancelled)
    }

    func test_cancelled_is_said_plainly() {
        let cancelled = AdminShellError(number: AdminShellError.userCancelled, message: "User canceled.")
        XCTAssertTrue(cancelled.cancelled)
        XCTAssertEqual(cancelled.errorDescription, "Canceled: nothing was changed.")
    }

    func test_the_folder_is_the_last_line_printed() {
        XCTAssertEqual(AdminShell.lastLine("noise\n/private/var/tmp/graft-enroll.AbCd1234\n\n"),
                       "/private/var/tmp/graft-enroll.AbCd1234")
        XCTAssertNil(AdminShell.lastLine("\n \n"))
    }

    struct Scripted: PrivilegedShell {
        let error: AdminShellError?
        let via: AdminRun.Via
        let ran: Counter

        final class Counter: @unchecked Sendable { var count = 0 }

        func run(_ script: String, prompt: String) async throws -> AdminRun {
            ran.count += 1
            if let error { throw error }
            return AdminRun(output: "ok", via: via)
        }
    }

    func test_osascript_runs_only_when_applescript_couldnt_run_at_all() async throws {
        let first = Scripted.Counter(), second = Scripted.Counter()
        let shell = FallbackAdminShell(
            primary: Scripted(error: AdminShellError(number: -1743, message: "not permitted", unavailable: true),
                              via: .appleScript, ran: first),
            fallback: Scripted(error: nil, via: .osascript, ran: second)
        )
        let run = try await shell.run("x", prompt: "p")
        XCTAssertEqual(run.via, .osascript, "the path that ran is recorded")
        XCTAssertEqual([first.count, second.count], [1, 1])
    }

    func test_a_cancel_or_a_failure_never_asks_again_another_way() async {
        for error in [AdminShellError(number: -128, message: "User canceled."),
                      AdminShellError(number: 3, message: "setup failed")] {
            let second = Scripted.Counter()
            let shell = FallbackAdminShell(
                primary: Scripted(error: error, via: .appleScript, ran: Scripted.Counter()),
                fallback: Scripted(error: nil, via: .osascript, ran: second)
            )
            do {
                _ = try await shell.run("x", prompt: "p")
                XCTFail("expected the primary's error")
            } catch let thrown as AdminShellError {
                XCTAssertEqual(thrown, error)
            } catch {
                XCTFail("\(error)")
            }
            XCTAssertEqual(second.count, 0)
        }
    }

    func test_the_machinery_errors_never_include_cancel_or_an_exit_status() {
        XCTAssertFalse(AdminShellError.machineryErrors.contains(AdminShellError.userCancelled))
        XCTAssertTrue(AdminShellError.machineryErrors.allSatisfy { $0 < 0 })
    }

    /// The fix-check's critic: -10000 (errAEEventFailed) is AppleScript's
    /// catch-all, and `do shell script` can report it after root's script
    /// ran. Falling back then would raise a second administrator prompt and
    /// run root's script a second time, with a new folder and a second
    /// UnfairPlay setup. Built the way `runNow` builds a failure, then handed
    /// to the fallback pair; nothing privileged runs.
    func test_a_catch_all_failure_never_runs_the_script_again_through_osascript() async {
        let failure = AppleScriptAdminShell.failure(number: -10000, message: "an Apple event handler failed")
        XCTAssertFalse(failure.unavailable, "-10000 may come after the script ran")
        let first = Scripted.Counter(), second = Scripted.Counter()
        let shell = FallbackAdminShell(
            primary: Scripted(error: failure, via: .appleScript, ran: first),
            fallback: Scripted(error: nil, via: .osascript, ran: second)
        )
        do {
            _ = try await shell.run("x", prompt: "p")
            XCTFail("expected the primary's failure")
        } catch let thrown as AdminShellError {
            XCTAssertEqual(thrown.number, -10000)
            XCTAssertEqual(thrown.message, "an Apple event handler failed")
        } catch {
            XCTFail("\(error)")
        }
        XCTAssertEqual([first.count, second.count], [1, 0], "one prompt, one root run")
    }

    /// The codes that do fall back are each raised before the handler's body
    /// runs, so `do shell script` never started: built the same way.
    func test_only_a_failure_before_the_handler_ran_falls_back() async throws {
        for number in [-1750, -1751, -1708, -1743] {
            let failure = AppleScriptAdminShell.failure(number: number, message: "machinery")
            XCTAssertTrue(failure.unavailable, "\(number)")
            let second = Scripted.Counter()
            let shell = FallbackAdminShell(
                primary: Scripted(error: failure, via: .appleScript, ran: Scripted.Counter()),
                fallback: Scripted(error: nil, via: .osascript, ran: second)
            )
            let run = try await shell.run("x", prompt: "p")
            XCTAssertEqual(run.via, .osascript, "\(number)")
            XCTAssertEqual(second.count, 1, "\(number)")
        }
        for number in [AdminShellError.userCancelled, 1, 67, 68, -10000, 0] {
            XCTAssertFalse(AppleScriptAdminShell.failure(number: number, message: "x").unavailable, "\(number)")
        }
        XCTAssertEqual(AdminShellError.machineryErrors, [-1750, -1751, -1708, -1743])
    }

    /// The owner's run of the notarized try12, word for word: the dialog
    /// names Graft and asks for Touch ID or the password. No malware sentence
    /// (that was Task 0's launchd spike), and no "Don't Allow" button.
    func test_the_dialog_wording_is_the_one_the_hands_on_run_recorded() {
        XCTAssertEqual(AdminShell.recordedDialogWording,
                       "Allow administrator access for \u{201C}Graft\u{201D}? This will allow \u{201C}Graft\u{201D} to access and modify your data, files, and settings on this Mac. Touch ID or enter your password to continue with \u{201C}Graft\u{201D}.")
        XCTAssertEqual(AdminShell.recordedDialogButtons, ["Use Password\u{2026}", "Cancel"])
        XCTAssertFalse(AdminShell.recordedDialogWording.contains("malware"))
    }

    // MARK: The test-host tripwire (2026-11-22)

    /// A test host is recognized, so the real shells refuse here. Checked by
    /// the predicate alone: a test that *ran* a real shell to see it refuse
    /// would raise the dialog the moment the check broke.
    func test_a_test_host_is_recognised() {
        XCTAssertTrue(AdminShell.insideTests)
        XCTAssertFalse(AdminShell.refusedInTests.cancelled)
        XCTAssertFalse(AdminShell.refusedInTests.unavailable, "never a reason to try the other shell")
    }

    /// The fix-check's critic: the predicate was only ever checked by where
    /// it sits. Here it is evaluated, each way, through the pure function
    /// `insideTests` calls with the real environment and `NSClassFromString`.
    /// Nothing here reaches a shell.
    func test_the_test_host_predicate_answers_each_way() {
        let noClass: (String) -> AnyClass? = { _ in nil }
        var asked: [String] = []
        let lookup: (String) -> AnyClass? = { name in
            asked.append(name)
            return name == "XCTestCase" ? NSObject.self : nil
        }
        typealias A = AdminShell
        XCTAssertTrue(A.isTestHost(environment: ["XCTestConfigurationFilePath": "/t/x.xctestconfiguration"], classNamed: noClass))
        XCTAssertTrue(A.isTestHost(environment: ["XCTestSessionIdentifier": "ABCDEF01-2345"], classNamed: noClass))
        XCTAssertTrue(A.isTestHost(environment: ["XCTestConfigurationFilePath": "", "XCTestSessionIdentifier": ""],
                                   classNamed: noClass), "set, even to nothing, is set")
        XCTAssertTrue(A.isTestHost(environment: [:], classNamed: lookup), "XCTest loaded, no Xcode variables")
        XCTAssertEqual(asked, ["XCTestCase"])
        XCTAssertFalse(A.isTestHost(environment: [:], classNamed: noClass), "an ordinary launch")
        XCTAssertFalse(A.isTestHost(
            environment: ["PATH": "/usr/bin", "XCTestConfiguration": "x", "xctestsessionidentifier": "x", "XCTest": "x"],
            classNamed: { $0 == "XCTest" ? NSObject.self : nil }
        ), "only the exact names count")
    }

    /// `insideTests` asks the pure function with the real inputs, not a
    /// stand-in: read from the source, never by changing it.
    func test_inside_tests_asks_with_the_real_environment_and_class_lookup() throws {
        let source = try String(contentsOf: URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .appending(path: "Graft/Services/AdminShell.swift"), encoding: .utf8)
        let lines = source.split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }
        let start = try XCTUnwrap(lines.firstIndex(of: "static var insideTests: Bool {"))
        XCTAssertEqual(lines[start + 1],
                       "isTestHost(environment: ProcessInfo.processInfo.environment, classNamed: NSClassFromString)")
        XCTAssertEqual(lines[start + 2], "}")
    }

    /// Both shells that can raise macOS's administrator prompt refuse in a
    /// test host before doing anything: the first statement of each `run` is
    /// the refusal. Read from the source, not run.
    func test_each_real_shell_refuses_first_thing() throws {
        let source = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .appending(path: "Graft/Services/AdminShell.swift")
        let lines = try String(contentsOf: source, encoding: .utf8)
            .split(separator: "\n", omittingEmptySubsequences: false)
            .map { $0.trimmingCharacters(in: .whitespaces) }
        let guardLine = "if AdminShell.insideTests { throw AdminShell.refusedInTests }"
        for shell in ["struct AppleScriptAdminShell", "struct OsascriptAdminShell"] {
            let start = try XCTUnwrap(lines.firstIndex { $0.hasPrefix(shell) }, shell)
            let run = try XCTUnwrap(
                lines[start...].firstIndex { $0.hasPrefix("func run(_ script: String, prompt: String)") }, shell
            )
            XCTAssertEqual(lines[run + 1], guardLine, "\(shell).run must refuse in a test host first")
        }
        XCTAssertEqual(lines.filter { $0 == guardLine }.count, 2)
    }

    // MARK: Root's shell gets nothing to import (whole-diff review, 2026-11-23)

    private static func source(_ path: String) throws -> [String] {
        try String(contentsOf: URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .appending(path: path), encoding: .utf8)
            .split(separator: "\n", omittingEmptySubsequences: false)
            .map { $0.trimmingCharacters(in: .whitespaces) }
    }

    /// Bash 3.2, root's `/bin/sh`, imports exported functions and a few
    /// variables from its environment. Every `BASH_FUNC_` name and each of
    /// those variables is found; nothing else, and no near miss, is.
    func test_shell_imports_are_the_names_root_bash_would_act_on() {
        let environment = [
            "BASH_FUNC_kill%%": "() { /bin/echo pwned; }",
            "BASH_FUNC_cp%%": "() { :; }",
            "BASH_FUNC_x": "() { :; }",
            "SHELLOPTS": "xtrace", "BASHOPTS": "extdebug", "PS4": "$(/usr/bin/id)",
            "ENV": "/tmp/x", "BASH_ENV": "/tmp/x", "CDPATH": "/tmp", "GLOBIGNORE": "*",
            "PATH": "/usr/bin:/bin", "HOME": "/Users/example", "LANG": "en_US.UTF-8", "USER": "example",
            "BASH": "/bin/bash", "BASH_FUNC": "x", "MY_BASH_FUNC_x": "x", "ps4": "x", "XPC_FLAGS": "0x0",
        ]
        XCTAssertEqual(AdminShell.shellImports(in: environment), [
            "BASHOPTS", "BASH_ENV", "BASH_FUNC_cp%%", "BASH_FUNC_kill%%", "BASH_FUNC_x",
            "CDPATH", "ENV", "GLOBIGNORE", "PS4", "SHELLOPTS",
        ])
        XCTAssertEqual(AdminShell.shellImports(in: ["PATH": "/usr/bin", "HOME": "/h"]), [])
        XCTAssertEqual(AdminShell.shellImportNames,
                       ["SHELLOPTS", "BASHOPTS", "PS4", "ENV", "BASH_ENV", "CDPATH", "GLOBIGNORE"])
    }

    /// The one call site: `GraftApp`'s first property, before any that starts
    /// a thread or a child, and it removes exactly `shellImports` of this
    /// process's own environment with `unsetenv`. Read from the source, the
    /// way the guard lines are; the test never changes its own environment.
    func test_the_imports_are_removed_first_thing_at_launch() throws {
        let app = try Self.source("Graft/GraftApp.swift")
        let start = try XCTUnwrap(app.firstIndex(of: "struct GraftApp: App {"))
        let first = try XCTUnwrap(app[(start + 1)...].firstIndex { !$0.isEmpty && !$0.hasPrefix("//") })
        XCTAssertEqual(app[first], "private let shellImportsRemoved = AdminShell.removeShellImports()")
        XCTAssertEqual(app.filter { $0.contains("AdminShell.removeShellImports()") }.count, 1)
        for later in ["ShellEnvironment.shared", "CLIRunner", "@State"] {
            let use = try XCTUnwrap(app.firstIndex { !$0.hasPrefix("//") && $0.contains(later) }, later)
            XCTAssertGreaterThan(use, first, "\(later) comes after the environment is cleaned")
        }

        let shell = try Self.source("Graft/Services/AdminShell.swift")
        let body = try XCTUnwrap(shell.firstIndex(of: "static func removeShellImports() -> [String] {"))
        XCTAssertEqual(Array(shell[(body + 1)...(body + 4)]), [
            "let names = shellImports(in: ProcessInfo.processInfo.environment)",
            "for name in names { unsetenv(name) }",
            "return names",
            "}",
        ])
    }

    /// The osascript fallback hands its child a fixed PATH and HOME, USER and
    /// LANG, and nothing else of this app's environment.
    func test_osascript_gets_only_path_home_user_and_lang() {
        let parent = [
            "PATH": "/opt/example/bin:/usr/bin", "HOME": "/Users/example", "USER": "example", "LANG": "en_US.UTF-8",
            "BASH_FUNC_kill%%": "() { :; }", "SHELLOPTS": "xtrace", "PS4": "$(/usr/bin/id)", "BASH_ENV": "/tmp/x",
            "DYLD_INSERT_LIBRARIES": "/tmp/x.dylib", "TMPDIR": "/tmp/",
        ]
        XCTAssertEqual(OsascriptAdminShell.environment(from: parent), [
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": "/Users/example", "USER": "example", "LANG": "en_US.UTF-8",
        ])
        XCTAssertEqual(OsascriptAdminShell.environment(from: [:]), ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin"])
    }

    /// `run` starts osascript through `runToEnd`, with that environment, and
    /// reads nothing itself. Read from the source: running it would be the
    /// administrator prompt.
    func test_the_fallback_runs_osascript_through_run_to_end_with_its_own_environment() throws {
        let lines = try Self.source("Graft/Services/AdminShell.swift")
        let shell = try XCTUnwrap(lines.firstIndex { $0.hasPrefix("struct OsascriptAdminShell") })
        let run = try XCTUnwrap(lines[shell...].firstIndex { $0.hasPrefix("func run(_ script: String, prompt: String)") })
        let end = try XCTUnwrap(lines[run...].firstIndex { $0.hasPrefix("struct FallbackAdminShell") })
        let body = lines[run..<end]
        for line in [
            "ran = try Self.runToEnd(",
            "URL(fileURLWithPath: \"/usr/bin/osascript\"),",
            "environment: Self.environment(from: ProcessInfo.processInfo.environment)",
        ] {
            XCTAssertTrue(body.contains(line), line)
        }
        XCTAssertFalse(body.contains { $0.contains("readDataToEndOfFile") || $0.contains("Process()") },
                       "the pipes are runToEnd's to read")
        let helper = try XCTUnwrap(lines.firstIndex { $0.hasPrefix("static func runToEnd(") })
        XCTAssertTrue(lines[helper...].prefix(8).contains("proc.environment = environment"))
    }

    /// The child sees exactly the environment it was given, nothing of the
    /// test host's.
    func test_run_to_end_hands_the_child_exactly_its_environment() throws {
        let ran = try OsascriptAdminShell.runToEnd(
            URL(fileURLWithPath: "/usr/bin/env"), arguments: [],
            environment: ["PATH": "/usr/bin:/bin", "HOME": "/Users/example", "LANG": "C"]
        )
        XCTAssertEqual(ran.status, 0)
        XCTAssertEqual(String(decoding: ran.output, as: UTF8.self).split(separator: "\n").sorted(),
                       ["HOME=/Users/example", "LANG=C", "PATH=/usr/bin:/bin"])
    }

    /// The whole-diff review: osascript's stdout was read to EOF before its
    /// stderr, so a failing root run that wrote more than a pipe holds to
    /// stderr (UnfairPlay's log) hung both sides. A child that writes 200 KB
    /// to stderr first, then its folder line to stdout, finishes, and both
    /// arrive whole. On a queue with a deadline, so a hang fails here rather
    /// than holding the suite.
    func test_run_to_end_drains_a_long_stderr_and_stdout_together() {
        let finished = expectation(description: "the child ran to the end")
        final class Box: @unchecked Sendable { var ran: OsascriptAdminShell.Ran?; var error: Error? }
        let box = Box()
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                box.ran = try OsascriptAdminShell.runToEnd(
                    URL(fileURLWithPath: "/bin/sh"),
                    arguments: ["-c", "/usr/bin/head -c 204800 /dev/zero | /usr/bin/tr '\\000' e >&2; echo /private/var/tmp/graft-enrol.example; exit 3"],
                    environment: ["PATH": "/usr/bin:/bin"]
                )
            } catch {
                box.error = error
            }
            finished.fulfill()
        }
        wait(for: [finished], timeout: 30)
        XCTAssertNil(box.error)
        XCTAssertEqual(box.ran?.status, 3)
        XCTAssertEqual(box.ran?.error.count, 204_800)
        XCTAssertEqual(box.ran.map { String(decoding: $0.output, as: UTF8.self) }, "/private/var/tmp/graft-enrol.example\n")
        XCTAssertEqual(box.ran.map { OsascriptAdminShell.failure(status: $0.status, stderr: String(decoding: $0.error, as: UTF8.self)).number }, 3)
    }
}
