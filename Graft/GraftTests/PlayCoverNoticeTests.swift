import SwiftUI
import XCTest
@testable import Graft

/// Build options' "This Mac" section, with and without PlayCover.
///
/// Every lookup here is the test's own, so nothing depends on whether this Mac
/// has PlayCover, and every opener is a recorder, so nothing opens a browser.
@MainActor
final class PlayCoverNoticeTests: XCTestCase {
    /// Where a lookup that finds PlayCover says it is. Never read: only
    /// whether there is an answer counts.
    private static let installedAt = URL(fileURLWithPath: "/Applications/PlayCover.app")

    /// Who was asked what, and which pages were opened.
    private final class Recorder {
        var asked: [String] = []
        var opened: [URL] = []
        var viaEnvironment: [URL] = []
        var builds = 0
    }

    private func playCover(installed: Bool, _ recorder: Recorder) -> PlayCover {
        PlayCover(
            lookup: { id in
                recorder.asked.append(id)
                return installed ? Self.installedAt : nil
            },
            openURL: { recorder.opened.append($0) }
        )
    }

    // MARK: - Detection

    /// The lookup is asked for PlayCover's bundle id, and its answer is the
    /// whole of the decision: nil is not installed, any app is installed.
    func test_the_lookup_asks_for_playcovers_bundle_id() {
        let recorder = Recorder()
        XCTAssertFalse(playCover(installed: false, recorder).isInstalled)
        XCTAssertTrue(playCover(installed: true, recorder).isInstalled)
        XCTAssertEqual(recorder.asked, ["io.playcover.PlayCover", "io.playcover.PlayCover"])
        XCTAssertEqual(recorder.opened, [], "asking opens nothing")
    }

    /// The default `ContentView` hands down finds no PlayCover and opens
    /// nothing.
    func test_the_inert_one_finds_nothing_and_opens_nothing() {
        XCTAssertFalse(PlayCover.inert.isInstalled)
        PlayCover.inert.openReleasesPage()
    }

    /// The id the app asks LaunchServices about is the one the CLI hands a
    /// build to with `open -b`, and the page is the one the CLI's hint names.
    /// Read from cli.py's own assignments, not its comments.
    func test_the_bundle_id_and_the_page_are_the_clis() throws {
        let cli = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appending(path: "graft_ipa/cli.py")
        let source = try String(contentsOf: cli, encoding: .utf8)
        func assigned(_ name: String) throws -> String {
            let pattern = try NSRegularExpression(
                pattern: "^\(name) = \"([^\"]+)\"$", options: [.anchorsMatchLines]
            )
            let ns = source as NSString
            let matches = pattern.matches(in: source, range: NSRange(location: 0, length: ns.length))
            XCTAssertEqual(matches.count, 1, "one assignment of \(name) in cli.py")
            let match = try XCTUnwrap(matches.first, "no \(name) in cli.py")
            return ns.substring(with: match.range(at: 1))
        }
        XCTAssertEqual(try assigned("PLAYCOVER_BUNDLE_ID"), PlayCover.bundleID)
        XCTAssertEqual(try assigned("PLAYCOVER_RELEASES_URL"), PlayCover.releasesURL.absoluteString)
        XCTAssertEqual(PlayCover.releasesURL.absoluteString, "https://github.com/PlayCover/PlayCover/releases")
    }

    // MARK: - The section

    /// The section as Build options draws it: its font, its gutter, its
    /// 300pt width.
    private func section(_ playCover: PlayCover, _ recorder: Recorder) -> Hosted {
        let view = MacBuildSection(
            playCover: playCover, isBuildDisabled: false, buildHelp: "Build for this Mac",
            onBuild: { recorder.builds += 1 }
        )
        .font(AppFont.body)
        .padding(Metrics.popoverGutter)
        // A backstop: had Get PlayCover gone through SwiftUI's openURL, whose
        // default is the real browser, the press would land here.
        .environment(\.openURL, OpenURLAction { url in
            recorder.viaEnvironment.append(url)
            return .handled
        })
        let pane = hosted(view, width: 300)
        settle(pane, seconds: 0.2)
        return pane
    }

    private func shown(in pane: Hosted) throws -> (nodes: [AccessibilityTree.Node], text: [String]) {
        let nodes = try AccessibilityTree.nodes(in: pane.window)
        let text = nodes.flatMap { [$0.name, ($0.value as? String) ?? ""] }.filter { !$0.isEmpty }
        return (nodes, text)
    }

    private func button(_ name: String, in nodes: [AccessibilityTree.Node]) -> AccessibilityTree.Node? {
        nodes.first { $0.role == kAXButtonRole && $0.name == name }
    }

    /// Not installed: the section says so, and Get PlayCover opens the
    /// releases page through the opener it was given, and nothing else.
    func test_not_installed_says_so_and_get_playcover_opens_the_releases_page() throws {
        try AccessibilityTree.requireServed()
        let recorder = Recorder()
        let pane = section(playCover(installed: false, recorder), recorder)
        let (nodes, text) = try shown(in: pane)

        XCTAssertTrue(text.contains(MacBuildSection.missingMessage), "\(text)")
        XCTAssertEqual(
            MacBuildSection.missingMessage,
            "PlayCover isn't installed. Graft hands a build for this Mac to PlayCover, "
                + "a free app you install yourself."
        )
        let get = try XCTUnwrap(button("Get PlayCover", in: nodes), "no Get PlayCover button: \(text)")
        XCTAssertEqual(get.help, "Open PlayCover's releases page on GitHub")
        XCTAssertNotNil(button("Build for this Mac", in: nodes), "the build is still offered")
        XCTAssertTrue(recorder.asked.allSatisfy { $0 == PlayCover.bundleID } && !recorder.asked.isEmpty)
        XCTAssertEqual(recorder.opened, [], "nothing opens until the button is pressed")

        try clickButton(named: "Get PlayCover", in: pane.window)
        settle(pane, seconds: 0.2)
        XCTAssertEqual(recorder.opened, [URL(string: "https://github.com/PlayCover/PlayCover/releases")!])
        XCTAssertEqual(recorder.viaEnvironment, [], "not through SwiftUI's openURL")
        XCTAssertEqual(recorder.builds, 0, "Get PlayCover starts no build")
    }

    /// Not installed doesn't stop a build: the archive can be handed over
    /// from Builds once PlayCover is there.
    func test_not_installed_still_builds() throws {
        try AccessibilityTree.requireServed()
        let recorder = Recorder()
        let pane = section(playCover(installed: false, recorder), recorder)
        let (nodes, _) = try shown(in: pane)
        XCTAssertNotEqual(button("Build for this Mac", in: nodes)?.enabled, false)

        try clickButton(named: "Build for this Mac", in: pane.window)
        settle(pane, seconds: 0.2)
        XCTAssertEqual(recorder.builds, 1)
        XCTAssertEqual(recorder.opened, [])
    }

    /// Both buttons fit Build options' width side by side, with nothing cut
    /// off at its right edge.
    func test_not_installed_fits_build_options() throws {
        try AccessibilityTree.requireServed()
        let recorder = Recorder()
        let pane = section(playCover(installed: false, recorder), recorder)
        let (nodes, _) = try shown(in: pane)
        for name in ["Build for this Mac", "Get PlayCover"] {
            let frame = try XCTUnwrap(frame(of: try XCTUnwrap(button(name, in: nodes))), name)
            XCTAssertLessThanOrEqual(frame.maxX, pane.window.frame.maxX - Metrics.popoverGutter + 0.5, name)
        }
        XCTAssertEqual(pane.size.width, 300)
    }

    /// Installed: nothing new. The label and Build for this Mac, and no
    /// message or Get PlayCover.
    func test_installed_shows_nothing_new() throws {
        try AccessibilityTree.requireServed()
        let recorder = Recorder()
        let pane = section(playCover(installed: true, recorder), recorder)
        let (nodes, text) = try shown(in: pane)

        XCTAssertFalse(text.contains(MacBuildSection.missingMessage), "\(text)")
        XCTAssertFalse(text.contains { $0.contains("PlayCover isn't installed") }, "\(text)")
        XCTAssertNil(button("Get PlayCover", in: nodes), "\(text)")
        XCTAssertNotNil(button("Build for this Mac", in: nodes))
        XCTAssertFalse(recorder.asked.isEmpty, "the section did ask")
        XCTAssertEqual(recorder.opened, [])
    }

    /// An element's frame, in the top-left-based screen coordinates the
    /// accessibility API uses, turned into the window's bottom-left ones.
    private func frame(of node: AccessibilityTree.Node) -> CGRect? {
        let element = node.element
        let onScreen = AccessibilityTree.offMain { () -> CGRect? in
            guard let position = AccessibilityTree.attribute(element, kAXPositionAttribute),
                  let size = AccessibilityTree.attribute(element, kAXSizeAttribute)
            else { return nil }
            var origin = CGPoint.zero
            var extent = CGSize.zero
            AXValueGetValue(position as! AXValue, .cgPoint, &origin)
            AXValueGetValue(size as! AXValue, .cgSize, &extent)
            return CGRect(origin: origin, size: extent)
        } ?? nil
        guard let onScreen else { return nil }
        let primaryHeight = NSScreen.screens.first?.frame.height ?? 0
        return CGRect(
            x: onScreen.minX, y: primaryHeight - onScreen.maxY,
            width: onScreen.width, height: onScreen.height
        )
    }
}
