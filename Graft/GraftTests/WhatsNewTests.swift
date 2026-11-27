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

    // MARK: - Versions

    private typealias Version = Changelog.Version

    func test_a_version_is_numbers_between_dots() {
        XCTAssertEqual(Version("2.3.1")?.parts, [2, 3, 1])
        XCTAssertEqual(Version("2")?.parts, [2])
        XCTAssertEqual(Version("2.10")?.parts, [2, 10])
        for text in ["", "v2.3", "2..3", "2.3.", ".2", "2.3b", "2.3 (2026-11-26)", "Unreleased", "?", "２.３"] {
            XCTAssertNil(Version(text), "\"\(text)\" is not a version")
        }
    }

    /// `## <version> (<date>)`: the version is the heading's first word.
    func test_a_release_heading_names_its_version() {
        XCTAssertEqual(Version(heading: "2.3.1 (2026-11-26)"), Version("2.3.1"))
        XCTAssertEqual(Version(heading: "2.0.1"), Version("2.0.1"))
        XCTAssertNil(Version(heading: "Unreleased"))
        XCTAssertNil(Version(heading: "Version 2.3 (2026-11-26)"))
        XCTAssertNil(Version(heading: ""))
    }

    /// Number by number, not as text: 2.10 comes after 2.9.
    func test_versions_compare_by_number() throws {
        func v(_ text: String) throws -> Version { try XCTUnwrap(Version(text)) }
        XCTAssertGreaterThan(try v("2.10"), try v("2.9"))
        XCTAssertGreaterThan(try v("10.0"), try v("9.9"))
        XCTAssertGreaterThan(try v("2.3.1"), try v("2.3"))
        XCTAssertGreaterThan(try v("2.3.0.1"), try v("2.3"))
        XCTAssertLessThan(try v("2.2.1"), try v("2.3"))
        XCTAssertEqual(try v("2.3"), try v("2.3.0"))
        XCTAssertFalse(try v("2.3") < v("2.3.0"))
        XCTAssertFalse(try v("2.3.0") < v("2.3"))
    }

    // MARK: - What the window opens on

    /// A title and intro, then five releases, newest first; 2.3 has a group.
    private static let fiveReleases = """
        # Changelog

        What changed in each release of Graft, newest first.

        ## 2.3.1 (2026-11-26)

        - Fixed one.

        ## 2.3 (2026-11-26)

        ### New

        - Added two.

        ## 2.2.1 (2026-11-25)

        - Fixed three.

        ## 2.2 (2026-11-25)

        - Added four.

        ## 2.1 (2026-11-24)

        - Added five.
        """

    /// The release headings in `blocks`.
    private func releases(_ blocks: [Block]) -> [String] {
        blocks.compactMap { block -> String? in
            if case let .heading(2, text) = block { return text }
            return nil
        }
    }

    /// The releases a changelog of these headings, each with one bullet,
    /// opens on.
    private func opened(_ headings: [String], on opening: WhatsNew.Opening) -> [String] {
        let text = "# Changelog\n\n" + headings.map { "## \($0)\n\n- Something.\n" }.joined(separator: "\n")
        return releases(WhatsNew.page(Changelog.blocks(in: text), opening: opening, expanded: false).blocks)
    }

    /// `blocks` up to the heading `text`, which isn't included.
    private func blocks(_ blocks: [Block], upTo text: String) throws -> [Block] {
        Array(blocks.prefix(upTo: try XCTUnwrap(blocks.firstIndex(of: .heading(level: 2, text: text)))))
    }

    /// After an update from 2.2, which skipped 2.2.1: 2.3.1, 2.3 and 2.2.1,
    /// whole, under the title and intro, with the rest a button away.
    func test_after_an_update_the_releases_since_the_last_version() throws {
        let all = Changelog.blocks(in: Self.fiveReleases)
        let page = WhatsNew.page(all, opening: .since(last: "2.2", current: "2.3.1"), expanded: false)

        XCTAssertEqual(page.blocks, try blocks(all, upTo: "2.2 (2026-11-25)"))
        XCTAssertEqual(releases(page.blocks), ["2.3.1 (2026-11-26)", "2.3 (2026-11-26)", "2.2.1 (2026-11-25)"])
        XCTAssertEqual(Array(page.blocks.prefix(2)), [
            .heading(level: 1, text: "Changelog"),
            .paragraph("What changed in each release of Graft, newest first."),
        ])
        XCTAssertTrue(page.blocks.contains(.heading(level: 3, text: "New")))
        XCTAssertFalse(page.blocks.contains(.bullet(depth: 0, text: "Added four.")))
        XCTAssertTrue(page.hasMore)
    }

    /// Newer than the last version, by number, and no newer than this one.
    func test_the_range_is_bounded_by_number() {
        let headings = ["2.10 (2026-12-03)", "2.9.1 (2026-12-02)", "2.9 (2026-12-01)", "2.8 (2026-11-30)"]
        XCTAssertEqual(opened(headings, on: .since(last: "2.8", current: "2.10")),
                       ["2.10 (2026-12-03)", "2.9.1 (2026-12-02)", "2.9 (2026-12-01)"])
        XCTAssertEqual(opened(headings, on: .since(last: "2.9", current: "2.10")),
                       ["2.10 (2026-12-03)", "2.9.1 (2026-12-02)"])
        XCTAssertEqual(opened(headings, on: .since(last: "2.8", current: "2.9.1")),
                       ["2.9.1 (2026-12-02)", "2.9 (2026-12-01)"])
        XCTAssertEqual(opened(headings, on: .since(last: "2.8.0", current: "2.9.0")), ["2.9 (2026-12-01)"])
    }

    /// A release this copy doesn't have yet (an entry for the next one) is
    /// left out.
    func test_a_release_newer_than_this_one_is_left_out() {
        XCTAssertEqual(opened(["2.4", "2.3.1", "2.3"], on: .since(last: "2.3", current: "2.3.1")), ["2.3.1"])
    }

    /// A heading that doesn't parse counts as older: it isn't newer than
    /// any version, so no update shows it, and it's never the newest.
    func test_a_heading_that_is_not_a_version_counts_as_older() {
        let headings = ["Unreleased", "2.3.1 (2026-11-26)", "Notes", "2.3 (2026-11-26)", "2.2 (2026-11-25)"]
        XCTAssertEqual(opened(headings, on: .since(last: "2.2", current: "2.3.1")),
                       ["2.3.1 (2026-11-26)", "2.3 (2026-11-26)"])
        XCTAssertEqual(opened(headings, on: .newest), ["2.3.1 (2026-11-26)"])
    }

    /// Help › What's New…: the newest release, under the title and intro,
    /// with the rest a button away.
    func test_help_opens_on_the_newest_release_only() throws {
        let all = Changelog.blocks(in: Self.fiveReleases)
        let page = WhatsNew.page(all, opening: .newest, expanded: false)
        XCTAssertEqual(page.blocks, try blocks(all, upTo: "2.3 (2026-11-26)"))
        XCTAssertEqual(releases(page.blocks), ["2.3.1 (2026-11-26)"])
        XCTAssertTrue(page.hasMore)
    }

    /// The newest is the highest version, wherever it is; the first
    /// release only when no heading is a version.
    func test_the_newest_release_is_the_highest_version() {
        XCTAssertEqual(opened(["2.2", "2.3.1", "2.3"], on: .newest), ["2.3.1"])
        XCTAssertEqual(opened(["Next", "Later"], on: .newest), ["Next"])
    }

    /// An update with nothing in its range (back to an older version, or to
    /// one the changelog doesn't have), or a version that doesn't parse,
    /// opens as Help does.
    func test_nothing_since_the_last_version_opens_on_the_newest() {
        let headings = ["2.3.1", "2.3", "2.2"]
        for opening: WhatsNew.Opening in [
            .since(last: "2.4", current: "2.3.1"),
            .since(last: "2.3.1", current: "2.3.2"),
            .since(last: "?", current: "2.3.1"),
            .since(last: "2.2", current: "?"),
            .since(last: "", current: ""),
        ] {
            XCTAssertEqual(opened(headings, on: opening), ["2.3.1"], "\(opening)")
        }
    }

    /// Show earlier releases: the whole changelog, as before 2.3.1, and no
    /// button.
    func test_expanded_is_the_whole_changelog() {
        let all = Changelog.blocks(in: Self.fiveReleases)
        for opening: WhatsNew.Opening in [.newest, .since(last: "2.2", current: "2.3.1"), .since(last: "?", current: "?")] {
            XCTAssertEqual(WhatsNew.page(all, opening: opening, expanded: true), WhatsNew.Page(blocks: all, hasMore: false),
                           "\(opening)")
        }
    }

    /// The button is there only when some release isn't shown.
    func test_nothing_more_when_every_release_is_shown() {
        let one = Changelog.blocks(in: "# Changelog\n\n## 2.3.1\n\n- One.")
        XCTAssertEqual(WhatsNew.page(one, opening: .newest, expanded: false), WhatsNew.Page(blocks: one, hasMore: false))

        let none = Changelog.blocks(in: "# Changelog\n\nNothing yet.")
        XCTAssertEqual(WhatsNew.page(none, opening: .newest, expanded: false), WhatsNew.Page(blocks: none, hasMore: false))

        let all = Changelog.blocks(in: Self.fiveReleases)
        XCTAssertEqual(WhatsNew.page(all, opening: .since(last: "1.0", current: "2.3.1"), expanded: false),
                       WhatsNew.Page(blocks: all, hasMore: false))
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

    /// The lines on screen in `nodes`: each static text's value, and each
    /// heading's (a heading is its own role, not a static text). Empty ones
    /// aren't lines.
    private func lines(_ nodes: [AccessibilityTree.Node]) -> [String] {
        nodes
            .filter { [kAXStaticTextRole, "AXHeading"].contains($0.role) }
            .map { ($0.value as? String).flatMap { $0.isEmpty ? nil : $0 } ?? $0.name }
            .filter { !$0.isEmpty }
    }

    /// The Show earlier releases button in `nodes`.
    private func showEarlier(_ nodes: [AccessibilityTree.Node]) -> AccessibilityTree.Node? {
        nodes.first { $0.role == kAXButtonRole && $0.name == WhatsNew.showEarlier }
    }

    private func hostedWindow(_ view: WhatsNewView) throws -> Hosted {
        try AccessibilityTree.requireServed()
        let pane = hosted(view, size: NSSize(width: 640, height: 600))
        addTeardownBlock { pane.window.orderOut(nil) }
        settle(pane)
        return pane
    }

    private func shown(_ view: WhatsNewView) throws -> [String] {
        lines(try AccessibilityTree.nodes(in: try hostedWindow(view).window))
    }

    private func noLink(_ url: URL) { XCTFail("nothing pressed: \(url)") }

    func test_the_window_shows_the_changelog() throws {
        let texts = try shown(WhatsNewView(
            changelog: "# Changelog\n\n## 2.0.1 (2026-11-24)\n\n- Fixed `this`.\n  - And that.",
            store: WhatsNewStore(), open: noLink
        ))
        for line in ["Changelog", "2.0.1 (2026-11-24)", "Fixed this.", "And that."] {
            XCTAssertTrue(texts.contains(line), "\(line) not in \(texts)")
        }
        XCTAssertFalse(texts.contains(WhatsNew.missing))
    }

    func test_the_window_without_a_changelog_says_so() throws {
        let texts = try shown(WhatsNewView(changelog: nil, store: WhatsNewStore(), open: noLink))
        XCTAssertEqual(texts, [WhatsNew.missing])
    }

    private static let newestLines = [
        "Changelog", "What changed in each release of Graft, newest first.", "2.3.1 (2026-11-26)", "Fixed one.",
    ]
    private static let earlierLines = [
        "2.3 (2026-11-26)", "Added two.", "2.2.1 (2026-11-25)", "Fixed three.", "2.2 (2026-11-25)", "Added four.",
        "2.1 (2026-11-24)", "Added five.",
    ]

    /// As Help opens it: the title and intro, the newest release, and the
    /// button, not the earlier releases.
    func test_the_window_opens_on_the_newest_release_with_the_rest_a_button_away() throws {
        let nodes = try AccessibilityTree.nodes(in: try hostedWindow(WhatsNewView(
            changelog: Self.fiveReleases, store: WhatsNewStore(), open: noLink
        )).window)
        let texts = lines(nodes)
        for line in Self.newestLines {
            XCTAssertTrue(texts.contains(line), "\(line) not in \(texts)")
        }
        for line in Self.earlierLines {
            XCTAssertFalse(texts.contains(line), "\(line) in \(texts)")
        }
        XCTAssertNotNil(showEarlier(nodes), "no \(WhatsNew.showEarlier) button")
        XCTAssertEqual(WhatsNew.showEarlier, "Show earlier releases")
    }

    /// Show earlier releases: every release, in the same window, and the
    /// button goes.
    func test_show_earlier_releases_shows_the_rest_in_the_same_window() throws {
        let store = WhatsNewStore()
        let pane = try hostedWindow(WhatsNewView(changelog: Self.fiveReleases, store: store, open: noLink))
        // Clicked through the window: `AccessibilityTree.press` runs the
        // action off the main thread (`clickButton`).
        try clickButton(named: WhatsNew.showEarlier, in: pane.window)
        settle(pane)

        XCTAssertTrue(store.expanded)
        let nodes = try AccessibilityTree.nodes(in: pane.window)
        let texts = lines(nodes)
        for line in Self.newestLines + Self.earlierLines {
            XCTAssertTrue(texts.contains(line), "\(line) not in \(texts)")
        }
        XCTAssertNil(showEarlier(nodes), "the button is still there")
    }

    /// After an update from 2.2, the releases since; and an opening while
    /// the window is open changes it there.
    func test_the_window_shows_what_its_store_opens_on() throws {
        let store = WhatsNewStore()
        store.show(.since(last: "2.2", current: "2.3.1"))
        let pane = try hostedWindow(WhatsNewView(changelog: Self.fiveReleases, store: store, open: noLink))

        var nodes = try AccessibilityTree.nodes(in: pane.window)
        var texts = lines(nodes)
        for line in Self.newestLines + ["2.3 (2026-11-26)", "Added two.", "2.2.1 (2026-11-25)", "Fixed three."] {
            XCTAssertTrue(texts.contains(line), "\(line) not in \(texts)")
        }
        for line in ["2.2 (2026-11-25)", "Added four.", "2.1 (2026-11-24)", "Added five."] {
            XCTAssertFalse(texts.contains(line), "\(line) in \(texts)")
        }
        XCTAssertNotNil(showEarlier(nodes))

        store.show(.newest)
        settle(pane)
        nodes = try AccessibilityTree.nodes(in: pane.window)
        texts = lines(nodes)
        XCTAssertTrue(texts.contains("2.3.1 (2026-11-26)"), "\(texts)")
        XCTAssertFalse(texts.contains("2.3 (2026-11-26)"), "\(texts)")
        XCTAssertNotNil(showEarlier(nodes))
    }

    // MARK: - Help › What's New…

    private func helpMenu() throws -> NSMenu {
        let items = NSApplication.shared.mainMenu?.items ?? []
        return try XCTUnwrap(items.first { $0.title == "Help" }?.submenu, "no Help menu in \(items.map(\.title))")
    }

    private func whatsNewWindow() -> NSWindow? {
        NSApplication.shared.windows.first { $0.title == WhatsNew.windowTitle && $0.isVisible }
    }

    /// Chooses Help › What's New… in this app's own menu, and waits for the
    /// window.
    private func chooseWhatsNew() throws -> NSWindow {
        let help = try helpMenu()
        let item = try XCTUnwrap(help.items.first { $0.title == WhatsNew.menuTitle })
        help.performActionForItem(at: help.index(of: item))
        let deadline = Date().addingTimeInterval(3)
        while whatsNewWindow() == nil, Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        }
        return try XCTUnwrap(whatsNewWindow(), "no What's New window opened")
    }

    private func pump(_ seconds: TimeInterval = 0.5) {
        let deadline = Date().addingTimeInterval(seconds)
        while Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.02)) }
    }

    func test_help_has_whats_new_after_check_for_updates() throws {
        let titles = try helpMenu().items.map(\.title)
        let check = try XCTUnwrap(titles.firstIndex(of: "Check for Updates…"), "\(titles)")
        XCTAssertEqual(titles.dropFirst(check + 1).first, "What's New…", "\(titles)")
        XCTAssertEqual(WhatsNew.menuTitle, "What's New…")
    }

    /// Chosen from the app's own Help menu, it opens the window.
    func test_whats_new_opens_its_window() throws {
        XCTAssertNil(whatsNewWindow(), "premise: not open yet")
        try chooseWhatsNew().close()
    }

    /// The app's own window, from its own menu, with the changelog it
    /// carries: the newest release, and the button for the rest.
    func test_help_opens_on_the_newest_release() throws {
        try AccessibilityTree.requireServed()
        XCTAssertNil(whatsNewWindow(), "premise: not open yet")
        let blocks = Changelog.blocks(in: try XCTUnwrap(Changelog.bundled))
        let all = releases(blocks)
        XCTAssertGreaterThan(all.count, 1, "premise: the changelog has earlier releases")
        let newest = releases(WhatsNew.page(blocks, opening: .newest, expanded: false).blocks)
        XCTAssertEqual(newest.count, 1)

        let window = try chooseWhatsNew()
        addTeardownBlock { window.close() }
        pump()
        let nodes = try AccessibilityTree.nodes(in: window)
        // Each release heading shown, once: a heading may be read as a
        // static text too.
        var seen = Set<String>()
        XCTAssertEqual(lines(nodes).filter { all.contains($0) && seen.insert($0).inserted }, newest)
        XCTAssertNotNil(showEarlier(nodes), "no \(WhatsNew.showEarlier) button")
    }

    /// Chosen while the window shows more (after an update, or after Show
    /// earlier releases), Help shows the newest release again.
    func test_help_shows_the_newest_release_in_a_window_that_shows_more() throws {
        try AccessibilityTree.requireServed()
        let store = WhatsNewStore()
        store.show(.since(last: "2.2", current: "2.3.1"))
        store.expand()
        // The test host's own What's New window, which the command opens.
        addTeardownBlock { self.whatsNewWindow()?.close() }
        let pane = hosted(WhatsNewCommand(store: store), size: NSSize(width: 200, height: 60))
        addTeardownBlock { pane.window.orderOut(nil) }
        settle(pane)

        try clickButton(named: WhatsNew.menuTitle, in: pane.window)
        pump(0.2)
        XCTAssertEqual(store.opening, .newest)
        XCTAssertFalse(store.expanded)
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
        XCTAssertNil(WhatsNew.opensAfterUpdate(version: "2.0.1", defaults: defaults))
        XCTAssertEqual(defaults.string(forKey: WhatsNew.lastVersionKey), "2.0.1")
        XCTAssertNil(WhatsNew.opensAfterUpdate(version: "2.0.1", defaults: defaults), "the same version again")
    }

    /// Once, on the releases since the version that launched before.
    func test_a_new_version_opens_it_once_on_what_changed_since_the_last() throws {
        let defaults = try throwawayDefaults()
        defaults.set("2.0.1", forKey: WhatsNew.lastVersionKey)

        XCTAssertEqual(WhatsNew.opensAfterUpdate(version: "2.0.2", defaults: defaults),
                       .since(last: "2.0.1", current: "2.0.2"))
        XCTAssertEqual(defaults.string(forKey: WhatsNew.lastVersionKey), "2.0.2")
        XCTAssertNil(WhatsNew.opensAfterUpdate(version: "2.0.2", defaults: defaults), "only once")
    }

    /// The main window's modifier tells the window's store what to open on
    /// before it opens the window.
    func test_the_main_window_opens_it_on_the_releases_since_the_last_version() throws {
        let defaults = try throwawayDefaults()
        defaults.set("2.2", forKey: WhatsNew.lastVersionKey)
        let store = WhatsNewStore()
        // The test host's own What's New window, if `openWindow` reaches it
        // from a hosted view.
        addTeardownBlock { self.whatsNewWindow()?.close() }
        let pane = hosted(
            Color.clear.modifier(OpensWhatsNewAfterUpdate(defaults: defaults, version: "2.3.1", store: store)),
            size: NSSize(width: 40, height: 40)
        )
        addTeardownBlock { pane.window.orderOut(nil) }
        let deadline = Date().addingTimeInterval(3)
        while store.opening == .newest, Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        }
        XCTAssertEqual(store.opening, .since(last: "2.2", current: "2.3.1"))
        XCTAssertEqual(defaults.string(forKey: WhatsNew.lastVersionKey), "2.3.1")
    }

    // MARK: - The window's store

    /// Nothing recorded yet, or a window macOS reopened at launch: the
    /// newest release, as Help opens it.
    func test_a_new_store_opens_on_the_newest_release() {
        let store = WhatsNewStore()
        XCTAssertEqual(store.opening, .newest)
        XCTAssertFalse(store.expanded)
    }

    func test_show_earlier_releases_expands_and_the_next_opening_folds_again() {
        let store = WhatsNewStore()
        store.show(.since(last: "2.2", current: "2.3.1"))
        XCTAssertEqual(store.opening, .since(last: "2.2", current: "2.3.1"))
        XCTAssertFalse(store.expanded)

        store.expand()
        XCTAssertTrue(store.expanded)
        XCTAssertEqual(store.opening, .since(last: "2.2", current: "2.3.1"))

        store.show(.newest)
        XCTAssertEqual(store.opening, .newest)
        XCTAssertFalse(store.expanded, "Help opens on the newest release again")
    }
}
