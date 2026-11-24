import Foundation
import XCTest
@testable import Graft

/// A stand-in `graft` for the App Store search tests: a `/bin/sh` script that
/// appends each invocation's argv to a log, one line per run, then runs `body`.
///
/// The shape of `AppStoreStoreTests`' private `scriptRunner`, shared because
/// two test files need it. The argv under test is the one production code
/// builds, never a fixture standing in for it.
struct ScriptCLI {
    let runner: CLIRunner
    let log: URL

    init(_ body: String, test: XCTestCase) throws {
        let dir = FileManager.default.temporaryDirectory
            .appending(path: "script_cli_\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        test.addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let logURL = dir.appending(path: "argv.log")
        let script = dir.appending(path: "graft.sh")
        try """
        #!/bin/sh
        echo "$*" >> '\(logURL.path)'
        \(body)
        """.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
        runner = CLIRunner(executable: script)
        log = logURL
    }

    /// Every invocation so far, one argv per element. Empty before the first.
    func argv() -> [String] {
        ((try? String(contentsOf: log, encoding: .utf8)) ?? "")
            .split(separator: "\n").map(String.init)
    }
}

/// Poll `condition` every 20 ms until it holds, and fail the test, at the
/// caller's line, if `timeout` passes first.
///
/// It used to return either way, so a wait whose condition never held passed
/// silently and the assertions after it ran against whatever state happened to
/// be there (review part 5, finding 5). `message` says what was being waited for.
@MainActor
func eventually(
    _ condition: () -> Bool, timeout: TimeInterval = 5, _ message: String = "",
    file: StaticString = #filePath, line: UInt = #line
) async {
    let deadline = Date().addingTimeInterval(timeout)
    while !condition() {
        guard Date() < deadline else {
            let what = message.isEmpty ? "" : ": \(message)"
            return XCTFail("eventually: the condition never held within \(timeout) s\(what)",
                           file: file, line: line)
        }
        try? await Task.sleep(nanoseconds: 20_000_000)
    }
}

/// The administrator shell for every test that has no business reaching one:
/// it runs nothing, and reaching it fails the test. A test that drives Set Up
/// Decryption passes a recording shell instead (`AppStoreAccountStoreTests`).
///
/// Passed explicitly at every `AppStoreStore`, `AppStoreAccountStore` and
/// `ContentView` a test builds: none of them has a default shell any more,
/// so a forgotten fake is a compile error, not macOS's administrator prompt
/// (`NoRealDefaultsTests`).
struct NoAdminShell: PrivilegedShell {
    func run(_ script: String, prompt: String) async throws -> AdminRun {
        XCTFail("a test reached the administrator shell; pass a recording shell if it means to")
        throw AdminShell.refusedInTests
    }
}
