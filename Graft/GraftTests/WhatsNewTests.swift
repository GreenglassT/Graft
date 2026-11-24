import SwiftUI
import XCTest
@testable import Graft

/// Help › What's New…: the changelog the app carries, read into blocks and
/// shown in its own window.
///
/// The parser and the inline rendering are pure; the file is read from a
/// resources folder the test names; links go to a recorder; and the
/// after-an-update rule runs against a throwaway preferences suite.
@MainActor
final class WhatsNewTests: XCTestCase {
    private typealias Block = Changelog.Block

    // MARK: - Blocks

    func test_headings_by_level() {
        XCTAssertEqual(Changelog.blocks(in: "# Changelog\n## 2.0 (2026-11-23)\n### Fixes\n#### Deeper"), [
            .heading(level: 1, text: "Changelog"),
            .heading(level: 2, text: "2.0 (2026-11-23)"),
            .heading(level: 3, text: "Fixes"),
            .heading(level: 4, text: "Deeper"),
        ])
    }

    /// A `#` with no space after it is text, as in CommonMark, and so is a
    /// run of seven.
    func test_a_hash_without_a_space_is_not_a_heading() {
        XCTAssertEqual(Changelog.blocks(in: "#hashtag\n\n####### seven"), [
            .paragraph("#hashtag"),
            .paragraph("####### seven"),
        ])
    }

    func test_wrapped_lines_join_and_blank_lines_separate_paragraphs() {
        let text = "What changed in each release,\nnewest first.\n\n\n   \nSecond paragraph."
        XCTAssertEqual(Changelog.blocks(in: text), [
            .paragraph("What changed in each release, newest first."),
            .paragraph("Second paragraph."),
        ])
    }

    func test_bullets_nest_by_indent_and_wrapped_lines_continue_them() {
        let text = """
        - First, wrapped
          onto a second line.
          - Nested, also
            wrapped.
            - Deeper.
          - Nested again.
        * A star bullet.
        + A plus bullet.
        """
        XCTAssertEqual(Changelog.blocks(in: text), [
            .bullet(depth: 0, text: "First, wrapped onto a second line."),
            .bullet(depth: 1, text: "Nested, also wrapped."),
            .bullet(depth: 2, text: "Deeper."),
            .bullet(depth: 1, text: "Nested again."),
            .bullet(depth: 0, text: "A star bullet."),
            .bullet(depth: 0, text: "A plus bullet."),
        ])
    }

    /// Bold at the start of a line is not a bullet, and a paragraph after a
    /// list starts at the left again.
    func test_bold_is_not_a_bullet_and_a_list_ends_at_a_paragraph() {
        let text = """
        - One.
          - Two.

        **Bold** text.

        - Three.
        """
        XCTAssertEqual(Changelog.blocks(in: text), [
            .bullet(depth: 0, text: "One."),
            .bullet(depth: 1, text: "Two."),
            .paragraph("**Bold** text."),
            .bullet(depth: 0, text: "Three."),
        ])
    }

    /// A blank line between two items of one list keeps the nesting.
    func test_a_blank_line_inside_a_list_keeps_its_depth() {
        XCTAssertEqual(Changelog.blocks(in: "- One.\n  - Two.\n\n  - Three."), [
            .bullet(depth: 0, text: "One."),
            .bullet(depth: 1, text: "Two."),
            .bullet(depth: 1, text: "Three."),
        ])
    }

    func test_windows_line_endings_and_tabs() {
        XCTAssertEqual(Changelog.blocks(in: "## 1.0\r\n\r\n- One\r\n\t- Two\r\n  wrapped.\r\n"), [
            .heading(level: 2, text: "1.0"),
            .bullet(depth: 0, text: "One"),
            .bullet(depth: 1, text: "Two wrapped."),
        ])
    }

    func test_nothing_is_no_blocks() {
        XCTAssertEqual(Changelog.blocks(in: ""), [])
        XCTAssertEqual(Changelog.blocks(in: "\n  \n\n"), [])
    }

    /// The shape of the top of CHANGELOG.md: a title, a wrapped intro with a
    /// link, releases newest first, bold leads, nested bullets, wrapped lines
    /// and a `###` group.
    func test_a_changelog_shaped_like_the_real_one() {
        let text = """
        # Changelog

        What changed in each release of Graft, newest first. Each release's download
        is on [GitHub Releases](https://example.com/releases).

        ## 2.0 (2026-11-23)

        Graft no longer needs Terminal.

        - **Download it, open it, set it up in the app.** Graft.app carries its own
          `graft` CLI, with its own Python, and every tool it uses.
        - **Setup happens in the app.**
          - The first run asks whether you have a paid Apple Developer account. With
            one, you choose your `.p12` and profile.
          - Settings › App Store › Set Up Decryption… enrolls this Mac.
        - **macOS 14 or later.** Graft 1.1 needs macOS 15;
          [Going back to 1.1](docs/upgrading.md#going-back-to-11) installs its CLI
          with `@v1.1`.

        ### Also in 2.0

        - Builds has "Clean up old builds…".

        ## 1.1 (2026-11-18)

        Known issues, fixed in 2.0: a renamed bundle id failed on apps with an Apple
        Watch app.
        """
        XCTAssertEqual(Changelog.blocks(in: text), [
            .heading(level: 1, text: "Changelog"),
            .paragraph("What changed in each release of Graft, newest first. Each release's download "
                + "is on [GitHub Releases](https://example.com/releases)."),
            .heading(level: 2, text: "2.0 (2026-11-23)"),
            .paragraph("Graft no longer needs Terminal."),
            .bullet(depth: 0, text: "**Download it, open it, set it up in the app.** Graft.app carries its own "
                + "`graft` CLI, with its own Python, and every tool it uses."),
            .bullet(depth: 0, text: "**Setup happens in the app.**"),
            .bullet(depth: 1, text: "The first run asks whether you have a paid Apple Developer account. With "
                + "one, you choose your `.p12` and profile."),
            .bullet(depth: 1, text: "Settings › App Store › Set Up Decryption… enrolls this Mac."),
            .bullet(depth: 0, text: "**macOS 14 or later.** Graft 1.1 needs macOS 15; "
                + "[Going back to 1.1](docs/upgrading.md#going-back-to-11) installs its CLI with `@v1.1`."),
            .heading(level: 3, text: "Also in 2.0"),
            .bullet(depth: 0, text: "Builds has \"Clean up old builds…\"."),
            .heading(level: 2, text: "1.1 (2026-11-18)"),
            .paragraph("Known issues, fixed in 2.0: a renamed bundle id failed on apps with an Apple Watch app."),
        ])
    }

    // MARK: - Inline

    private func run(
        _ piece: String, in text: AttributedString, file: StaticString = #filePath, line: UInt = #line
    ) throws -> AttributedString.Runs.Run {
        try XCTUnwrap(text.runs.first { String(text[$0.range].characters) == piece },
                      "no run of \"\(piece)\" in \(text.runs.map { String(text[$0.range].characters) })",
                      file: file, line: line)
    }

    func test_code_bold_and_a_web_link() throws {
        let text = Changelog.inline("Run `graft --init`, then **read** [the guide](https://example.com/guide).")

        XCTAssertEqual(String(text.characters), "Run graft --init, then read the guide.")
        let code = try run("graft --init", in: text)
        XCTAssertEqual(code.inlinePresentationIntent, .code)
        XCTAssertEqual(code.font, AppFont.mono)
        XCTAssertEqual(try run("read", in: text).inlinePresentationIntent, .stronglyEmphasized)
        let link = try run("the guide", in: text)
        XCTAssertEqual(link.link, URL(string: "https://example.com/guide"))
        XCTAssertEqual(link.underlineStyle, .single)
        XCTAssertNil(try run("Run ", in: text).link)
    }

    /// The changelog's own links are relative to the repository, as GitHub
    /// shows them: here they go to the same page on GitHub.
    func test_a_relative_link_goes_to_the_repository_on_github() throws {
        let text = Changelog.inline("See [Going back to 1.1](docs/upgrading.md#going-back-to-11).")
        let link = try run("Going back to 1.1", in: text)
        XCTAssertEqual(
            link.link?.absoluteString,
            "https://github.com/GreenglassT/Graft/blob/main/docs/upgrading.md#going-back-to-11"
        )
    }

    /// Only a web page over https is a link; anything else stays as its text.
    func test_a_link_that_is_not_https_is_only_text() {
        let text = Changelog.inline(
            "[a](http://example.com) [b](file:///etc/hosts) [c](mailto:x@example.com) [d](graft://x) [e](ftp://example.com)"
        )
        XCTAssertEqual(String(text.characters), "a b c d e")
        XCTAssertEqual(text.runs.compactMap(\.link), [])
    }

    /// Markdown that doesn't close is shown as written, not dropped.
    func test_unclosed_markdown_keeps_its_text() {
        let text = Changelog.inline("an unclosed `code and **bold")
        XCTAssertEqual(String(text.characters), "an unclosed `code and **bold")
    }

    // MARK: - Loading

    private func resources(_ contents: Data?) throws -> URL {
        let fm = FileManager.default
        let dir = fm.temporaryDirectory.appendingPathComponent("graft-whatsnew-\(UUID().uuidString)")
        try fm.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? fm.removeItem(at: dir) }
        if let contents {
            try contents.write(to: dir.appendingPathComponent("CHANGELOG.md"))
        }
        return dir
    }

    func test_the_changelog_is_read_from_resources() throws {
        let dir = try resources(Data("# Changelog\n\n## 2.0.1\n".utf8))
        XCTAssertEqual(Changelog.text(resourceURL: dir), "# Changelog\n\n## 2.0.1\n")
        XCTAssertEqual(WhatsNew.content(of: Changelog.text(resourceURL: dir)), .blocks([
            .heading(level: 1, text: "Changelog"),
            .heading(level: 2, text: "2.0.1"),
        ]))
    }

    func test_a_missing_changelog_is_said_plainly() throws {
        XCTAssertNil(Changelog.text(resourceURL: try resources(nil)))
        XCTAssertNil(Changelog.text(resourceURL: nil), "a bundle with no Resources")
        XCTAssertEqual(WhatsNew.content(of: nil), .missing)
        XCTAssertEqual(WhatsNew.missing, "The changelog isn't in this copy of Graft.")
    }

    func test_an_unreadable_or_empty_changelog_is_missing() throws {
        XCTAssertNil(Changelog.text(resourceURL: try resources(Data([0x23, 0x20, 0xC3, 0x28]))), "not UTF-8")

        let folder = try resources(nil)
        try FileManager.default.createDirectory(
            at: folder.appendingPathComponent("CHANGELOG.md"), withIntermediateDirectories: false
        )
        XCTAssertNil(Changelog.text(resourceURL: folder), "a folder of that name")

        XCTAssertEqual(WhatsNew.content(of: Changelog.text(resourceURL: try resources(Data()))), .missing)
        XCTAssertEqual(WhatsNew.content(of: " \n\n"), .missing)
    }

    /// The app this suite runs in carries CHANGELOG.md in its Resources
    /// (project.yml), as the release does.
    func test_this_app_carries_the_changelog() throws {
        let text = try XCTUnwrap(Changelog.text(resourceURL: Bundle.main.resourceURL))
        let blocks = Changelog.blocks(in: text)
        XCTAssertEqual(blocks.first, .heading(level: 1, text: "Changelog"))
        XCTAssertTrue(blocks.contains { if case .heading(2, _) = $0 { true } else { false } }, "no release")
    }

    // MARK: - Links

    func test_only_a_web_page_is_opened() {
        var opened: [URL] = []
        for link in ["https://example.com/a", "http://example.com/b", "file:///etc/hosts", "graft://x"] {
            _ = WhatsNew.follow(URL(string: link)!) { opened.append($0) }
        }
        XCTAssertEqual(opened, [URL(string: "https://example.com/a")!])
    }

    // MARK: - The window

    /// The lines on screen: each static text's value, and each heading's
    /// (a heading is its own role, not a static text). Empty ones aren't
    /// lines.
    private func shown(_ view: WhatsNewView) throws -> [String] {
        try AccessibilityTree.requireServed()
        let pane = hosted(view, size: NSSize(width: 640, height: 600))
        addTeardownBlock { pane.window.orderOut(nil) }
        settle(pane)
        return try AccessibilityTree.nodes(in: pane.window)
            .filter { [kAXStaticTextRole, "AXHeading"].contains($0.role) }
            .map { ($0.value as? String).flatMap { $0.isEmpty ? nil : $0 } ?? $0.name }
            .filter { !$0.isEmpty }
    }

    func test_the_window_shows_the_changelog() throws {
        let texts = try shown(WhatsNewView(
            changelog: "# Changelog\n\n## 2.0.1 (2026-11-24)\n\n- Fixed `this`.\n  - And that.",
            open: { _ in XCTFail("nothing pressed") }
        ))
        for line in ["Changelog", "2.0.1 (2026-11-24)", "Fixed this.", "And that."] {
            XCTAssertTrue(texts.contains(line), "\(line) not in \(texts)")
        }
        XCTAssertFalse(texts.contains(WhatsNew.missing))
    }

    func test_the_window_without_a_changelog_says_so() throws {
        let texts = try shown(WhatsNewView(changelog: nil, open: { _ in XCTFail("nothing pressed") }))
        XCTAssertEqual(texts, [WhatsNew.missing])
    }

    // MARK: - Help › What's New…

    private func helpMenu() throws -> NSMenu {
        let items = NSApplication.shared.mainMenu?.items ?? []
        return try XCTUnwrap(items.first { $0.title == "Help" }?.submenu, "no Help menu in \(items.map(\.title))")
    }

    func test_help_has_whats_new_after_check_for_updates() throws {
        let titles = try helpMenu().items.map(\.title)
        let check = try XCTUnwrap(titles.firstIndex(of: "Check for Updates…"), "\(titles)")
        XCTAssertEqual(titles.dropFirst(check + 1).first, "What's New…", "\(titles)")
        XCTAssertEqual(WhatsNew.menuTitle, "What's New…")
    }

    /// Chosen from the app's own Help menu, it opens the window.
    func test_whats_new_opens_its_window() throws {
        let help = try helpMenu()
        let item = try XCTUnwrap(help.items.first { $0.title == WhatsNew.menuTitle })
        func window() -> NSWindow? {
            NSApplication.shared.windows.first { $0.title == WhatsNew.windowTitle && $0.isVisible }
        }
        XCTAssertNil(window(), "premise: not open yet")

        help.performActionForItem(at: help.index(of: item))
        let deadline = Date().addingTimeInterval(3)
        while window() == nil, Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        }
        let opened = try XCTUnwrap(window(), "no What's New window opened")
        opened.close()
    }

    // MARK: - After an update

    private func throwawayDefaults() throws -> UserDefaults {
        let name = "WhatsNewTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: name))
        addTeardownBlock { defaults.removePersistentDomain(forName: name) }
        return defaults
    }

    /// The first launch is the Welcome screen's: nothing opens, and the
    /// version is remembered for the next.
    func test_the_first_launch_opens_nothing_and_remembers_the_version() throws {
        let defaults = try throwawayDefaults()
        XCTAssertFalse(WhatsNew.opensAfterUpdate(version: "2.0.1", defaults: defaults))
        XCTAssertEqual(defaults.string(forKey: WhatsNew.lastVersionKey), "2.0.1")
        XCTAssertFalse(WhatsNew.opensAfterUpdate(version: "2.0.1", defaults: defaults), "the same version again")
    }

    func test_a_new_version_opens_it_once() throws {
        let defaults = try throwawayDefaults()
        defaults.set("2.0.1", forKey: WhatsNew.lastVersionKey)

        XCTAssertTrue(WhatsNew.opensAfterUpdate(version: "2.0.2", defaults: defaults))
        XCTAssertEqual(defaults.string(forKey: WhatsNew.lastVersionKey), "2.0.2")
        XCTAssertFalse(WhatsNew.opensAfterUpdate(version: "2.0.2", defaults: defaults), "only once")
    }
}
