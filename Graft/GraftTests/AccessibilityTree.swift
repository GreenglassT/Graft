import ApplicationServices
import AppKit
import XCTest

/// A hosted window's accessibility tree, read the way VoiceOver reads it.
///
/// `NSHostingView.accessibilityChildren()` comes back empty in-process, so
/// asking AppKit sees nothing. The Accessibility API sees it: an
/// `AXUIElement` for this very process, queried from a *background* thread
/// while the main thread keeps its run loop turning — the main thread is where
/// SwiftUI answers, so asking from it would wait on itself. It needs no
/// Accessibility permission for one's own process (measured with
/// `AXIsProcessTrusted()` false).
///
/// Shared by `AccessibilityTests` (every control has a name) and by tests that
/// need to know a line of text is on screen, which a hosted SwiftUI view offers
/// no other way to ask.
@MainActor
enum AccessibilityTree {
    struct Node {
        let role: String
        let subrole: String?
        let title: String?
        let description: String?
        let value: Any?
        /// `AXEnabled`, nil for an element that does not report it.
        let enabled: Bool?
        /// `AXHelp`: what AppKit shows as the tooltip, set by `.help(_:)`.
        let help: String?
        let element: AXUIElement

        var name: String {
            [description, title].compactMap { $0 }
                .first { !$0.trimmingCharacters(in: .whitespaces).isEmpty } ?? ""
        }
    }

    nonisolated static func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else {
            return nil
        }
        return value
    }

    /// Run `body` off the main thread while this one pumps its run loop, for
    /// at most `timeout` seconds.
    ///
    /// A query that does not come back is a failure, not a skip: macOS not
    /// serving the tree answers at once (see `nodes(in:)`). The one way this
    /// has been seen to time out is a starved GCD pool — 64 workers parked
    /// in window-open animations (see `hosted(_:width:)`) — which hangs every
    /// subprocess the suite runs too, and is a defect to fix, not a machine
    /// condition to step around.
    static func offMain<T: Sendable>(
        timeout: TimeInterval = 10, _ body: @escaping @Sendable () -> T
    ) -> T? {
        let box = OffMainBox<T>()
        // Handed back under a lock, not by hopping to the main queue: an
        // async test is itself running *in* a main-queue block, the main queue
        // does not re-enter, so a hop would wait for this very loop to end.
        DispatchQueue.global(qos: .userInitiated).async {
            box.finish(body())
        }
        let deadline = Date().addingTimeInterval(timeout)
        while !box.isDone, Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.02))
        }
        let value = box.result
        if value == nil {
            XCTFail("the accessibility query did not come back within \(Int(timeout))s")
        }
        return value
    }

    /// **Call first in every test that reads the tree, before hosting
    /// anything.** Throws `XCTSkip` when macOS is not serving this process's
    /// tree (see `nodes(in:)` for when it does), asked once per run with a
    /// throwaway window and remembered.
    ///
    /// A skip is reported as one only when it leaves the test method itself.
    /// Thrown from inside an `XCTAssert…` or `XCTUnwrap` argument, XCTest
    /// catches it there and records "threw error" — a failure — and a test
    /// that skips only once it has hosted a window has already started that
    /// window's work, and paid for it. Asking here, up front, avoids both:
    /// the test skips in its first line, having built nothing.
    static func requireServed() throws {
        if isServed == nil { isServed = probe() }
        guard isServed == true else { throw notServed }
    }

    /// The answer `requireServed` remembers: nil until asked.
    private static var isServed: Bool?

    private static var notServed: XCTSkip {
        XCTSkip(
            "macOS is not serving this process's accessibility tree (no assistive "
                + "client has talked to it), so there is nothing to measure"
        )
    }

    /// `GRAFT_TESTS_PRETEND_NO_AX` in the test host's environment —
    /// `TEST_RUNNER_GRAFT_TESTS_PRETEND_NO_AX=1` on the `xcodebuild test`
    /// line — makes every window read as missing from the tree, which is what
    /// macOS not serving it looks like. So the skip path can be run on a Mac
    /// whose tree *is* served: every test that reads it should skip, quickly,
    /// and none should fail.
    private static let pretendNotServed =
        ProcessInfo.processInfo.environment["GRAFT_TESTS_PRETEND_NO_AX"] != nil

    /// Whether a window of this process's, on screen, is in its tree.
    private static func probe() -> Bool {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 80, height: 40),
            styleMask: [.titled], backing: .buffered, defer: false
        )
        window.isReleasedWhenClosed = false
        window.animationBehavior = .none
        window.orderFront(nil)
        defer { window.orderOut(nil) }
        return (try? Self.element(of: window)) != nil
    }

    /// `window` as the Accessibility API sees it, or nil when it is not in
    /// the tree; throws only when the query itself never came back.
    private struct NoAnswer: Error {}
    private static func element(of window: NSWindow) throws -> AXUIElement? {
        if pretendNotServed { return nil }
        let identifier = "graft-a11y-\(UUID().uuidString)"
        window.setAccessibilityIdentifier(identifier)
        let pid = getpid()
        final class Out: @unchecked Sendable { var target: AXUIElement? }
        guard let out = offMain({ () -> Out in
            let result = Out()
            let app = AXUIElementCreateApplication(pid)
            let windows = (Self.attribute(app, kAXWindowsAttribute) as? [AXUIElement]) ?? []
            result.target = windows.first {
                (Self.attribute($0, kAXIdentifierAttribute) as? String) == identifier
            }
            return result
        }) else { throw NoAnswer() }
        return out.target
    }

    /// Every element in `window`'s tree, minus the scroll bars AppKit draws.
    ///
    /// **Throws `XCTSkip` when macOS is not serving this process's tree.** The
    /// Accessibility API answers for an app's own windows only once its
    /// accessibility server is running, and macOS starts that server when an
    /// assistive client — VoiceOver, Accessibility Inspector, System Events —
    /// has talked to the app. Until then every window comes back titled with
    /// the app's name, without its identifier and without its content.
    /// Measured 2026-11-18: these tests passed for half an hour after such a
    /// client had touched Graft, then saw only that stand-in; a trusted client
    /// polling the test host brought the real tree back. Nothing in-process
    /// was found that starts the server — `AXEnhancedUserInterface` and
    /// `AXManualAccessibility` are refused (-25208, -25205), and activating the
    /// app does not do it.
    ///
    /// So a run without that condition has nothing to measure, and says so as
    /// a skip rather than as a failure about the app. **Read the skip count:**
    /// a green suite with these skipped did not check a single label.
    ///
    /// Tests call `requireServed()` before hosting anything, and that is what
    /// a skip normally comes from. This one is for the server going away
    /// mid-run, which has been seen. When the window is missing it asks again
    /// with a fresh window, so a tree that has gone is remembered and the
    /// rest of the run skips at `requireServed()`; and when the tree is still
    /// served, the skip says it is this window that is missing — one not in
    /// front, which the tree is not served for (`SetupTests.frontWindow`).
    static func nodes(in window: NSWindow) throws -> [Node] {
        guard let target = try Self.element(of: window) else {
            isServed = probe()
            guard isServed == true else { throw notServed }
            throw XCTSkip(
                "macOS is serving this process's accessibility tree, but not for this "
                    + "window — it is not in front"
            )
        }
        final class Out: @unchecked Sendable {
            var nodes: [Node] = []
        }
        let out = offMain { () -> Out in
            let result = Out()
            func walk(_ element: AXUIElement, depth: Int) {
                guard depth < 60 else { return }
                let role = Self.attribute(element, kAXRoleAttribute) as? String ?? ""
                if role == kAXScrollBarRole { return }
                result.nodes.append(Node(
                    role: role,
                    subrole: Self.attribute(element, kAXSubroleAttribute) as? String,
                    title: Self.attribute(element, kAXTitleAttribute) as? String,
                    description: Self.attribute(element, kAXDescriptionAttribute) as? String,
                    value: Self.attribute(element, kAXValueAttribute),
                    enabled: (Self.attribute(element, kAXEnabledAttribute) as? NSNumber)?.boolValue,
                    help: Self.attribute(element, kAXHelpAttribute) as? String,
                    element: element
                ))
                for child in (Self.attribute(element, kAXChildrenAttribute) as? [AXUIElement]) ?? [] {
                    walk(child, depth: depth + 1)
                }
            }
            walk(target, depth: 0)
            return result
        }
        return out?.nodes ?? []
    }

    static func press(_ node: Node) {
        let element = node.element
        let status = offMain { AXUIElementPerformAction(element, kAXPressAction as CFString).rawValue }
        XCTAssertEqual(status, AXError.success.rawValue, "AXPress on \(node.role) \(node.name)")
    }

    /// Every static text's value in `window`, for "is this line on screen".
    static func texts(in window: NSWindow) throws -> [String] {
        try nodes(in: window).filter { $0.role == kAXStaticTextRole }.compactMap { $0.value as? String }
    }
}

/// The answer to an off-main query, handed back across threads.
private final class OffMainBox<T>: @unchecked Sendable {
    private let lock = NSLock()
    private var value: T?
    private var done = false

    func finish(_ result: T) {
        lock.lock(); defer { lock.unlock() }
        value = result
        done = true
    }

    var isDone: Bool { lock.lock(); defer { lock.unlock() }; return done }
    var result: T? { lock.lock(); defer { lock.unlock() }; return value }
}
