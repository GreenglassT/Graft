import SwiftUI

/// Graft's changelog: the CHANGELOG.md the app carries in
/// `Contents/Resources` (project.yml), read into the blocks Help › What's
/// New… shows.
///
/// A small reader for the Markdown that file uses, not all of Markdown:
/// `#` headings, paragraphs, and `-`, `*` or `+` lists nested by indent,
/// with lines wrapped at any width. Within a block, code spans, bold and
/// links are Foundation's inline Markdown (`inline`). Anything else, such as
/// a numbered list or a fenced block, reads as a paragraph of its own text.
enum Changelog {
    enum Block: Equatable {
        /// `level` is the number of `#`s: 1 for the title, 2 for a release,
        /// 3 for a group within one.
        case heading(level: Int, text: String)
        case paragraph(String)
        /// A list item. `depth` is 0 for a top-level item, 1 for one nested
        /// under it, and so on.
        case bullet(depth: Int, text: String)
    }

    static let fileName = "CHANGELOG.md"

    /// The file's text, or nil when `resourceURL` has none, or it isn't
    /// readable UTF-8.
    static func text(resourceURL: URL?) -> String? {
        guard let file = resourceURL?.appendingPathComponent(fileName, isDirectory: false) else { return nil }
        return try? String(contentsOf: file, encoding: .utf8)
    }

    /// This app's, read once: a bundle's contents don't change under it.
    static let bundled: String? = text(resourceURL: Bundle.main.resourceURL)

    /// `markdown`'s blocks, in the file's order, which is newest release
    /// first. A wrapped line joins the paragraph or item it continues, with
    /// one space.
    static func blocks(in markdown: String) -> [Block] {
        var blocks: [Block] = []
        // The paragraph or list item that a wrapped line would continue.
        var open: Block?
        // The indents of the list items that enclose the next one, outermost
        // first: a deeper indent nests under the last of them.
        var indents: [Int] = []

        func close() {
            if let open { blocks.append(open) }
            open = nil
        }

        for raw in markdown.split(omittingEmptySubsequences: false, whereSeparator: \.isNewline) {
            let line = raw.replacingOccurrences(of: "\t", with: "    ")
            let indent = line.prefix { $0 == " " }.count
            let body = line.trimmingCharacters(in: .whitespaces)

            if body.isEmpty {
                close()
            } else if let heading = heading(body) {
                close()
                indents = []
                blocks.append(heading)
            } else if let item = bullet(body) {
                close()
                while let last = indents.last, last >= indent { indents.removeLast() }
                open = .bullet(depth: indents.count, text: item)
                indents.append(indent)
            } else if case let .paragraph(text)? = open {
                open = .paragraph(text + " " + body)
            } else if case let .bullet(depth, text)? = open {
                open = .bullet(depth: depth, text: text + " " + body)
            } else {
                indents = []
                open = .paragraph(body)
            }
        }
        close()
        return blocks
    }

    /// A heading: one to six `#`s, then a space.
    private static func heading(_ line: String) -> Block? {
        let level = line.prefix { $0 == "#" }.count
        guard (1...6).contains(level) else { return nil }
        let rest = line.dropFirst(level)
        guard rest.first == " " else { return nil }
        return .heading(level: level, text: rest.trimmingCharacters(in: .whitespaces))
    }

    /// A list item's text: a `-`, `*` or `+`, then a space.
    private static func bullet(_ line: String) -> String? {
        guard let marker = line.first, "-*+".contains(marker), line.dropFirst().first == " " else { return nil }
        return line.dropFirst(2).trimmingCharacters(in: .whitespaces)
    }

    /// A release's version, as its heading and `CFBundleShortVersionString`
    /// write it: numbers between dots ("2.3.1"). Compared number by number,
    /// not as text, so 2.10 comes after 2.9; trailing zeros don't count, so
    /// 2.3 is 2.3.0.
    struct Version: Comparable {
        /// The numbers, without trailing zeros.
        let parts: [Int]

        /// Nil unless `text` is ASCII digits between single dots.
        init?(_ text: String) {
            var parts: [Int] = []
            for piece in text.split(separator: ".", omittingEmptySubsequences: false) {
                guard !piece.isEmpty, piece.allSatisfy({ ("0"..."9").contains($0) }), let number = Int(piece) else {
                    return nil
                }
                parts.append(number)
            }
            while parts.last == 0 { parts.removeLast() }
            self.parts = parts
        }

        /// The version a release heading names, `## <version> (<date>)`: its
        /// first word. Nil for a heading that doesn't start with one.
        init?(heading: String) {
            guard let word = heading.split(separator: " ", maxSplits: 1).first else { return nil }
            self.init(String(word))
        }

        static func < (lhs: Version, rhs: Version) -> Bool {
            lhs.parts.lexicographicallyPrecedes(rhs.parts)
        }
    }

    /// What the changelog's relative links are relative to: the repository,
    /// as GitHub shows the file.
    static let repositoryURL = URL(string: "https://github.com/GreenglassT/Graft/blob/main/")!

    /// One block's text, with its inline Markdown: code spans in
    /// `AppFont.mono`, bold, and links. A link is kept only when it goes to a
    /// web page over https, a relative one resolved against `repositoryURL`;
    /// any other is left as its text. Markdown that doesn't parse is shown
    /// as written.
    static func inline(_ text: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(
            interpretedSyntax: .inlineOnlyPreservingWhitespace, failurePolicy: .returnPartiallyParsedIfPossible
        )
        guard var out = try? AttributedString(markdown: text, options: options, baseURL: repositoryURL) else {
            return AttributedString(text)
        }
        for run in out.runs {
            if let url = run.link {
                if WhatsNew.isWebPage(url) {
                    out[run.range].link = url.absoluteURL
                    out[run.range].underlineStyle = .single
                } else {
                    out[run.range].link = nil
                }
            }
            if run.inlinePresentationIntent?.contains(.code) == true {
                out[run.range].font = AppFont.mono
            }
        }
        return out
    }
}

/// Help › What's New…: the window that shows `Changelog`, which releases it
/// opens on, and when it opens by itself.
enum WhatsNew {
    static let windowID = "whats-new"
    static let windowTitle = "What's New"
    static let menuTitle = "What's New…"
    /// What the window says when this copy of Graft has no changelog, or
    /// can't read it.
    static let missing = "The changelog isn't in this copy of Graft."
    /// The button at the end of the page that shows the rest of the
    /// changelog.
    static let showEarlier = "Show earlier releases"

    /// What the window shows.
    enum Content: Equatable {
        case missing
        case blocks([Changelog.Block])
    }

    /// The changelog's blocks, or `.missing` when there is no text or
    /// nothing in it.
    static func content(of text: String?) -> Content {
        let blocks = text.map(Changelog.blocks(in:)) ?? []
        return blocks.isEmpty ? .missing : .blocks(blocks)
    }

    /// What the window opens on.
    enum Opening: Equatable {
        /// The newest release: Help › What's New…, and any opening with no
        /// version to start from.
        case newest
        /// After an update: the releases newer than `last`, the version
        /// that launched before, up to and including `current`, this one.
        case since(last: String, current: String)
    }

    /// What the window shows of the changelog's `blocks`.
    struct Page: Equatable {
        let blocks: [Changelog.Block]
        /// Whether a release is left out: then Show earlier releases ends
        /// the page.
        let hasMore: Bool
    }

    /// The page for `opening`: the blocks before the first release (the
    /// title and intro), then the releases it opens on, in the changelog's
    /// order, which is newest first. `expanded`, after Show earlier
    /// releases, is every block.
    ///
    /// A release is a `##` heading and what follows it, up to the next.
    /// `.since` is each release whose version is newer than `last` and no
    /// newer than `current`, by number (`Changelog.Version`); a heading that
    /// isn't a version counts as older, so it's never in one. When that's
    /// none, or either version doesn't parse, it opens as `.newest` does: on
    /// the release with the highest version, or the first when no heading
    /// is a version.
    static func page(_ blocks: [Changelog.Block], opening: Opening, expanded: Bool) -> Page {
        // Each release's range in `blocks`, and its version.
        var releases: [(range: Range<Int>, version: Changelog.Version?)] = []
        for (index, block) in blocks.enumerated() {
            guard case let .heading(2, text) = block else { continue }
            if let last = releases.indices.last { releases[last].range = releases[last].range.lowerBound..<index }
            releases.append((index..<blocks.count, Changelog.Version(heading: text)))
        }
        guard !expanded, let first = releases.first else { return Page(blocks: blocks, hasMore: false) }

        var chosen: [Range<Int>] = []
        if case let .since(last, current) = opening,
           let last = Changelog.Version(last), let current = Changelog.Version(current) {
            chosen = releases.filter { release in
                guard let version = release.version else { return false }
                return last < version && version <= current
            }.map(\.range)
        }
        if chosen.isEmpty {
            // `max` keeps the first of equals.
            let versioned = releases.compactMap { release in release.version.map { (release.range, $0) } }
            chosen = [versioned.max { $0.1 < $1.1 }?.0 ?? first.range]
        }
        let preamble = blocks[..<first.range.lowerBound]
        return Page(
            blocks: Array(preamble) + chosen.flatMap { blocks[$0] },
            hasMore: chosen.count < releases.count
        )
    }

    static func isWebPage(_ url: URL) -> Bool {
        url.absoluteURL.scheme?.lowercased() == "https" && url.absoluteURL.host != nil
    }

    /// A link pressed in the window: a web page opens in the browser, through
    /// `open`; anything else is ignored. `inline` already keeps only web
    /// pages, so this is the second check.
    static func follow(_ url: URL, open: (URL) -> Void) -> OpenURLAction.Result {
        guard isWebPage(url) else { return .discarded }
        open(url.absoluteURL)
        return .handled
    }

    // MARK: After an update

    /// The `UserDefaults` key for the version of Graft that last launched.
    static let lastVersionKey = "whatsNew.lastVersion"

    /// What What's New opens on by itself at this launch, or nil when it
    /// doesn't open. It opens when this Mac last launched a different version
    /// of Graft, so once after each update, on the releases since that one.
    /// Not at a first launch, with no version recorded: that one is the
    /// setup screen's. Records `version` either way.
    static func opensAfterUpdate(version: String, defaults: UserDefaults) -> Opening? {
        let last = defaults.string(forKey: lastVersionKey)
        guard last != version else { return nil }
        defaults.set(version, forKey: lastVersionKey)
        return last.map { .since(last: $0, current: version) }
    }
}

/// What the What's New window opens on, and whether Show earlier releases
/// was pressed. One for the app: Help › What's New… and the opening after an
/// update set it before they open the window, which may already be open.
@MainActor
final class WhatsNewStore: ObservableObject {
    /// `.newest` until something opens the window: a window macOS reopens
    /// at launch shows the newest release, as Help does.
    @Published private(set) var opening: WhatsNew.Opening = .newest
    /// Show earlier releases was pressed: the whole changelog shows.
    @Published private(set) var expanded = false

    /// Opens on `opening`, with the earlier releases folded away again.
    func show(_ opening: WhatsNew.Opening) {
        self.opening = opening
        expanded = false
    }

    /// Show earlier releases.
    func expand() {
        expanded = true
    }
}

/// The What's New window's content: the changelog as headings, paragraphs
/// and lists, or one line saying it isn't there. It opens on what `store`
/// says (`WhatsNew.page`): the releases since the last version after an
/// update, the newest from Help, with Show earlier releases for the rest.
///
/// `open` is how a link opens: `GraftApp` passes the browser, a test a
/// recorder. Set as the window's `openURL`, so SwiftUI's own default, the
/// real browser, never applies (`NoRealDefaultsTests`).
struct WhatsNewView: View {
    private let content: WhatsNew.Content
    @ObservedObject private var store: WhatsNewStore
    private let open: (URL) -> Void

    /// Wide enough for a line of about 90 characters at `AppFont.body`, and
    /// no wider: longer lines are harder to follow.
    static let readingWidth: CGFloat = 620
    static let minWidth: CGFloat = 420
    static let minHeight: CGFloat = 320
    /// How far each list level is indented.
    static let indent: CGFloat = 18

    init(changelog: String?, store: WhatsNewStore, open: @escaping (URL) -> Void) {
        content = WhatsNew.content(of: changelog)
        self.store = store
        self.open = open
    }

    var body: some View {
        Group {
            switch content {
            case .missing:
                Text(WhatsNew.missing)
                    .font(AppFont.body)
                    .foregroundStyle(Palette.textMuted)
                    .padding(Metrics.paneGutter)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case let .blocks(blocks):
                let page = WhatsNew.page(blocks, opening: store.opening, expanded: store.expanded)
                ScrollView {
                    VStack(alignment: .leading, spacing: Metrics.titleGap) {
                        ForEach(Array(page.blocks.enumerated()), id: \.offset) { index, block in
                            ChangelogBlockView(block: block, isFirst: index == 0)
                        }
                        if page.hasMore {
                            Button(WhatsNew.showEarlier) { store.expand() }
                                .padding(.top, Metrics.sectionGap)
                        }
                    }
                    .frame(maxWidth: Self.readingWidth, alignment: .leading)
                    .padding(Metrics.paneGutter)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
        .frame(minWidth: Self.minWidth, minHeight: Self.minHeight)
        .background(Palette.bg)
        // Links take the tint: `accent`, a foreground color, not the
        // app's `accentSelect`, which is a fill (`Palette.accentSelect`).
        .tint(Palette.accent)
        .environment(\.openURL, OpenURLAction { WhatsNew.follow($0, open: open) })
    }
}

/// One block of the changelog.
private struct ChangelogBlockView: View {
    let block: Changelog.Block
    let isFirst: Bool

    var body: some View {
        switch block {
        case let .heading(level, text):
            heading(level: level, Text(Changelog.inline(text)))
                .accessibilityAddTraits(.isHeader)
        case let .paragraph(text):
            prose(text)
        case let .bullet(depth, text):
            HStack(alignment: .firstTextBaseline, spacing: Metrics.titleGap) {
                Text(depth == 0 ? "•" : "◦")
                    .font(AppFont.body)
                    .foregroundStyle(Palette.textMuted)
                    .accessibilityHidden(true)
                prose(text)
            }
            .padding(.leading, CGFloat(depth) * WhatsNewView.indent)
        }
    }

    private func prose(_ text: String) -> some View {
        Text(Changelog.inline(text))
            .font(AppFont.body)
            .foregroundStyle(Palette.text)
            .lineSpacing(2)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// The title, then each release under a hairline, then the groups
    /// within a release in the app's section-label style.
    @ViewBuilder
    private func heading(level: Int, _ text: Text) -> some View {
        switch level {
        case 1:
            text.tightTitle()
                .padding(.bottom, Metrics.titleGap)
        case 2:
            VStack(alignment: .leading, spacing: Metrics.sectionGap) {
                if !isFirst {
                    Rectangle().fill(Palette.line).frame(height: 1)
                }
                text.font(AppFont.title)
                    .tracking(-0.3)
                    .foregroundStyle(Palette.accent)
            }
            .padding(.top, isFirst ? 0 : Metrics.sectionGap)
            .padding(.bottom, Metrics.titleGap)
        default:
            text.labelCaps()
                .padding(.top, Metrics.rowGap)
        }
    }
}

/// Help › What's New…: opens the window on the newest release, or brings it
/// forward and shows the newest release there.
struct WhatsNewCommand: View {
    @ObservedObject var store: WhatsNewStore
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Button(WhatsNew.menuTitle) {
            store.show(.newest)
            openWindow(id: WhatsNew.windowID)
        }
    }
}

/// Opens What's New once after an update (`WhatsNew.opensAfterUpdate`), when
/// the main window first appears, on the releases since the version that
/// launched before. Nil `defaults` turns it off: `GraftApp` passes nil in a
/// test host, which is this app.
struct OpensWhatsNewAfterUpdate: ViewModifier {
    let defaults: UserDefaults?
    let version: String
    /// The window's, told what to open on before it opens.
    @PassedThrough var store: WhatsNewStore
    @Environment(\.openWindow) private var openWindow

    func body(content: Content) -> some View {
        content.task {
            guard let defaults, let opening = WhatsNew.opensAfterUpdate(version: version, defaults: defaults) else {
                return
            }
            store.show(opening)
            openWindow(id: WhatsNew.windowID)
        }
    }
}
