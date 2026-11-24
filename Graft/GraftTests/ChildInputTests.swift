import CryptoKit
import XCTest
@testable import Graft

/// `ChildInput`: a child's stdin that stays open, so a later line (the 2FA
/// code) can follow the first ones (the Apple ID and the password) after the
/// child asks for it.
final class ChildInputTests: XCTestCase {
    private func script(_ body: (URL) -> String) throws -> (CLIRunner, URL) {
        let dir = FileManager.default.temporaryDirectory
            .appending(path: "child_input_\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let file = dir.appending(path: "child.sh")
        try "#!/bin/sh\n\(body(dir))\n".write(to: file, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: file.path)
        return (CLIRunner(executable: file), dir)
    }

    static func sha256(_ text: String) -> String {
        SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    private func read(_ dir: URL, _ name: String) throws -> String {
        try String(contentsOf: dir.appending(path: name), encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    @MainActor
    func test_a_second_line_can_follow_the_first_after_the_child_asks() async throws {
        let (runner, dir) = try script { dir in """
            IFS= read -r first
            printf '%s' "$first" | /usr/bin/shasum -a 256 | /usr/bin/cut -d' ' -f1 > '\(dir.path)/first.sha'
            printf '%s\\n' '{"type":"appstore-login","state":"needs-code"}'
            IFS= read -r second
            printf '%s' "$second" | /usr/bin/shasum -a 256 | /usr/bin/cut -d' ' -f1 > '\(dir.path)/second.sha'
            printf '%s\\n' '{"type":"appstore-login","state":"signed-in","email":"someone@example.invalid"}'
            """ }
        let input = ChildInput()
        var first = Array("first-secret".utf8)
        XCTAssertTrue(input.send(&first), "a line sent before the launch is held, then written")
        XCTAssertTrue(first.isEmpty, "the caller's bytes are wiped once handed over")

        let events = try await runner.streamEvents(["x"], failing: "test", input: input) { event in
            if case let .appStoreLogin(record) = event,
               record.state == CLIContract.AppStoreLogin.needsCode {
                var second = Array("482913".utf8)
                input.send(&second)
            }
        }

        XCTAssertEqual(events.count, 2)
        XCTAssertEqual(try read(dir, "first.sha"), Self.sha256("first-secret"))
        XCTAssertEqual(try read(dir, "second.sha"), Self.sha256("482913"))
    }

    @MainActor
    func test_two_lines_sent_before_the_launch_arrive_in_order() async throws {
        let (runner, dir) = try script { dir in """
            IFS= read -r one; IFS= read -r two
            printf '%s|%s' "$one" "$two" > '\(dir.path)/both'
            """ }
        let input = ChildInput()
        var one = Array("someone@example.invalid".utf8)
        var two = Array("Example-Passw0rd".utf8)
        XCTAssertTrue(input.send(&one))
        XCTAssertTrue(input.send(&two))
        _ = try await runner.stream(arguments: ["x"], input: input) { _ in }
        XCTAssertEqual(try read(dir, "both"), "someone@example.invalid|Example-Passw0rd")
    }

    @MainActor
    func test_sending_after_the_child_is_gone_is_false_not_a_crash() async throws {
        let (runner, _) = try script { _ in "exit 0" }
        let input = ChildInput()
        _ = try await runner.stream(arguments: ["x"], input: input) { _ in }
        var late = Array("too late".utf8)
        XCTAssertFalse(input.send(&late))
        XCTAssertTrue(late.isEmpty)
    }

    /// The window `close()` can't cover: the child is alive but has shut its
    /// stdin, so a write meets a pipe with no reader. That must be a `false`,
    /// not the SIGPIPE that would kill the app (`F_SETNOSIGPIPE`).
    @MainActor
    func test_a_child_that_closed_its_stdin_costs_a_false_not_a_sigpipe() async throws {
        let (runner, _) = try script { _ in """
            exec 0<&-
            printf '%s\\n' '{"type":"appstore-login","state":"needs-code"}'
            /bin/sleep 2
            """ }
        let input = ChildInput()
        var sent: Bool?
        _ = try await runner.streamEvents(["x"], failing: "test", input: input) { event in
            if case .appStoreLogin = event {
                var code = Array("482913".utf8)
                sent = input.send(&code)
            }
        }
        XCTAssertEqual(sent, false)
    }

    @MainActor
    func test_closing_the_input_is_end_of_input_for_the_child() async throws {
        let (runner, dir) = try script { dir in """
            IFS= read -r first
            printf '%s\\n' '{"type":"appstore-login","state":"needs-code"}'
            if IFS= read -r second; then echo more > '\(dir.path)/result'; else echo eof > '\(dir.path)/result'; fi
            """ }
        let input = ChildInput()
        var first = Array("one".utf8)
        input.send(&first)
        // Closed once the child has read the first line and asked: an
        // ordering, not a sleep.
        _ = try await runner.streamEvents(["x"], failing: "test", input: input) { _ in
            input.close()
        }
        XCTAssertEqual(try read(dir, "result"), "eof")
    }

    func test_a_line_too_long_to_hold_before_the_launch_is_refused_and_wiped() {
        let input = ChildInput()
        var huge = [UInt8](repeating: 0x41, count: 4096)
        XCTAssertFalse(input.send(&huge))
        XCTAssertTrue(huge.isEmpty)
    }

    /// Review part 4, #20: a line break inside a value would shift every
    /// later line of the Apple ID, password and code protocol. Refused, wiped,
    /// and nothing of it reaches the child.
    @MainActor
    func test_a_value_with_a_line_break_in_it_is_refused_wiped_and_never_written() async throws {
        let (runner, dir) = try script { dir in """
            IFS= read -r first; IFS= read -r second
            printf '%s|%s' "$first" "$second" > '\(dir.path)/lines'
            """ }
        let input = ChildInput()
        for broken in ["someone@example.invalid\n", "pass\nword", "pass\rword", "\r\n"] {
            var bytes = Array(broken.utf8)
            XCTAssertFalse(input.send(&bytes), "refused: \(broken.debugDescription)")
            XCTAssertTrue(bytes.isEmpty, "wiped even when refused")
        }
        var one = Array("one".utf8)
        var two = Array("two".utf8)
        XCTAssertTrue(input.send(&one))
        XCTAssertTrue(input.send(&two))
        _ = try await runner.stream(arguments: ["x"], input: input) { _ in }
        XCTAssertEqual(try read(dir, "lines"), "one|two", "only the two good lines, in order")
        XCTAssertTrue(ChildInput.isOneLine(Array("Example-Passw0rd".utf8)))
        XCTAssertFalse(ChildInput.isOneLine([0x61, 0x0D]))
    }

    func test_zero_overwrites_every_byte() {
        var bytes = Array("hunter2".utf8)
        Secret.zero(&bytes)
        XCTAssertEqual(bytes, [UInt8](repeating: 0, count: 7))
        Secret.wipe(&bytes)
        XCTAssertTrue(bytes.isEmpty)
    }
}
