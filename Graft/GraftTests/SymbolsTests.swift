import XCTest
import AppKit
@testable import Graft

/// What keeps the app's icons from going back to being incidental.
///
/// Two halves, the same two `CLIContractTests` splits the CLI contract into.
/// One checks that every name declared in `Symbols` is a symbol that actually
/// exists — the half that catches a typo, which for an SF Symbol means an empty
/// box in the shipped app and no failure anywhere. The other checks that no
/// name reaches SwiftUI without passing through `Symbols` at all, because the
/// first half is worth nothing if a view can spell its own.
final class SymbolsTests: XCTestCase {

    // MARK: - Do the declared names exist?

    /// Every name `Symbols` can draw resolves to a real SF Symbol.
    ///
    /// `NSImage(systemSymbolName:)` is the same lookup `Image(systemName:)`
    /// performs, and it is the only thing in the toolchain that has an opinion
    /// about whether a glyph name is real: a bad one compiles, renders a blank
    /// rectangle, and passes review because nobody screenshots every state.
    /// Several of these names only draw in a state a screenshot is unlikely to
    /// reach: `Symbols.deviceLocked` needs a phone that answers usbmux and then
    /// refuses lockdown, `Symbols.advisory` needs a readiness check that fails
    /// without blocking. This is the only check those get. (`Symbols.noDevice`
    /// used to be listed here and is not: an unplugged bench is the *default*
    /// state, and that glyph is one of the easiest in the app to see.)
    ///
    /// It covers `selectedName` too, via `Symbol.names`. The filled variant is
    /// the likelier typo of the two: it is derived from a name that already
    /// works, by appending a suffix that does not exist for every symbol.
    func test_every_declared_symbol_resolves() {
        XCTAssertFalse(Symbols.all.isEmpty, "Symbols.all is empty; this test reads nothing")
        for symbol in Symbols.all {
            for name in symbol.names {
                XCTAssertNotNil(
                    NSImage(systemSymbolName: name, accessibilityDescription: nil),
                    """
                    "\(name)" is not an SF Symbol on this system. It renders as \
                    an empty box wherever it is drawn, and nothing else in the \
                    build says so.
                    """
                )
            }
        }
    }

    /// `Symbols.all` lists every accessor.
    ///
    /// The counterpart of `CLIContractTests.test_every_flag_constant_is_declared_used`,
    /// and for the same reason: `all` is hand-written because Swift cannot
    /// enumerate a caseless enum's static members, so it can silently fall
    /// behind the accessors above it. A symbol left out of `all` is a name the
    /// resolution test never resolves — which turns that test from a check into
    /// a check of whatever was remembered.
    func test_every_declared_symbol_is_listed_in_all() throws {
        let file = try SourceScan.appSources().appendingPathComponent(Self.vocabularyFile)
        // By argument label, not by `static let` on the same line: a two-name
        // accessor is wrapped across three lines and its literals never share a
        // line with the `static let`. A line-shaped filter found `all` and none
        // of the names, which is a scan that reads nothing while looking busy.
        let declared = try SourceScan.codeLines(of: file).flatMap { glyphNames(in: $0.text) }
        XCTAssertFalse(declared.isEmpty, "found no symbol declarations in \(file.path)")
        let listed = Symbols.all.flatMap(\.names)
        // Exact, in both directions, and it was `>=` with three units of slack
        // until a review measured the slack. Under `declared.count >=
        // Symbols.all.count` — 37 against 34 — dropping the `selectedName` arm
        // from `glyphName` took `declared` to 34, `34 >= 34` still held, the
        // subtraction below found nothing missing, and the test went green
        // while the scan had stopped reading three declarations. A tripwire
        // with slack in it is a tripwire you can walk under.
        //
        // Equality also closes a hole the subtraction cannot see. `missing` is
        // set arithmetic over *names*, so an accessor whose name duplicates one
        // already in `all` can be added, used, and never listed — and this file
        // has the live exposure for it: `magnifyingglass` is declared three
        // times, `xmark` and `tray.and.arrow.down.fill` twice each. Counting
        // per declaration rather than per distinct name is what catches that.
        XCTAssertEqual(
            declared.count, listed.count,
            "matched \(declared.count) glyph name(s) in \(Self.vocabularyFile) against "
            + "\(listed.count) reachable from Symbols.all. More declared than listed "
            + "means an accessor is missing from `all`; fewer means this scan has "
            + "stopped matching the way the file is written."
        )

        let listedSet = Set(listed)
        let missing = Set(declared).subtracting(listedSet).sorted()
        XCTAssertTrue(
            missing.isEmpty,
            """
            \(missing.count) symbol name(s) declared in \(Self.vocabularyFile) \
            are not reachable from Symbols.all: \(missing.joined(separator: ", ")). \
            Add the accessor to `all`, or nothing ever checks that its name is real.
            """
        )
    }

    // MARK: - Can a name reach SwiftUI another way?

    /// The two files where an icon may be named.
    ///
    /// Path-relative to the app sources, the way `CLIContractTests` reports its
    /// offenders: "Symbols.swift" alone does not say which of the design
    /// system's files to open. Kept as a `Set` because it is a list of
    /// *declaration sites*, and a file added to it must be one.
    ///
    /// `Marks.swift` is the second because it names the four asset-catalog
    /// marks, and the scan below now covers asset names as well as SF Symbol
    /// names.
    ///
    /// **The test a file has to pass to be in here is not "it draws nothing".**
    /// An earlier draft said that and it is false of both of them —
    /// `Symbols.swift` builds an `Image(systemName:)` in three places
    /// (`image`, `image(size:)`, `image(selected:)`) and `Marks.swift` builds
    /// an `Image(_:)` in `image(size:)`. That is the point of them. The test is
    /// the opposite: **these are the only two places where naming an icon is
    /// legitimate**, because a name written here is a name `Symbols.all` /
    /// `Marks.all` can be made to list and the resolution tests can resolve. A
    /// name written anywhere else is reachable by nothing. So a file belongs in
    /// this set only if it is a *declaration site* whose names those tests
    /// cover — adding a view here to quiet a failure is how this scan stops
    /// scanning.
    static let vocabularyFile = "DesignSystem/Symbols.swift"
    static let markVocabularyFile = "DesignSystem/Marks.swift"
    static let vocabularyFiles: Set<String> = [vocabularyFile, markVocabularyFile]

    /// Both files, for the failure messages.
    ///
    /// Both scans skip both files, so a message that names one of them sends
    /// whoever tripped it to the wrong file half the time — and "declare it in
    /// the vocabulary" is useless advice if the vocabulary named is the one
    /// without the right type in it.
    static let vocabularyList = vocabularyFiles.sorted().joined(separator: " and ")

    /// No view names a symbol itself.
    ///
    /// **It bans the three argument labels, not a literal shaped like a glyph
    /// name**, and that is the whole design of this scan. A flag has `--` in
    /// front of it, so `CLIContractTests` can recognize one on sight; a symbol
    /// name is `"square"` or `"lock"` — indistinguishable from the ordinary
    /// strings these files are full of ("attached", "package", "failed"). The
    /// argument labels are the narrow gate every one of them has to pass
    /// through, so the gate is what is watched — and the gate has to be *all*
    /// of them, which is what `Self.symbolAPI`'s third arm is about.
    ///
    /// Banning the label rather than the literal also catches the three shapes
    /// a scan anchored on the literal misses. All three were in this app when
    /// this was written, and each of them cost the grep that produced the
    /// inventory at least one name — 13 of 46 in total:
    ///
    /// - a name returned from a function — `BuildTargetBar.symbol` was a
    ///   `switch` over eight of them, feeding a `systemName:` 120 lines away
    ///   (lines 453 and 573 of that file as it then stood; `BuildTargetBar`
    ///   quotes the same count beside the property);
    /// - a name chosen by a ternary *inside* the call —
    ///   `Image(systemName: isChecked ? "checkmark.square.fill" : "square")`;
    /// - the second and third arms of a ternary that spilled onto the next
    ///   line, where only the first arm sat beside the argument label.
    ///
    /// The scan is the whole app-source tree rather than two named directories.
    /// `Views/` and `DesignSystem/` are where the icons were, but there is
    /// nothing about `Services/` or `GraftApp.swift` that stops one being
    /// drawn there, and scanning everything costs a directory walk.
    func test_no_symbol_literal_escapes_the_vocabulary() throws {
        let offenders = try Self.offenders(of: Self.symbolAPI, under: SourceScan.appSources())
        XCTAssertTrue(
            offenders.isEmpty,
            """
            \(offenders.count) use(s) of systemName:/systemImage:/\
            systemSymbolName: outside \(Self.vocabularyList): \
            \(offenders.joined(separator: ", ")). \
            Declare the icon in `Symbols` (\(Self.vocabularyFile)) and draw it \
            with `.image`, `.image(size:)` or `.label(_:)`, so its weight and \
            rendering mode are decided once and its name is somewhere a test \
            can resolve it.
            """
        )
    }

    /// No view names an *asset* itself either.
    ///
    /// The sibling of the scan above, for the four marks in `Assets.xcassets`,
    /// and it had to find a different thing to anchor on. The task the first one
    /// solved by banning `systemName:` does not have the same answer here:
    /// SwiftUI spells an asset image `Image("debPackage")`, with **no argument
    /// label at all**, and the literal inside it is no more distinguishable from
    /// this app's ordinary strings than `"square"` was — `"debPackage"` is the
    /// same shape as `"displayName"` or `"attached"`.
    ///
    /// **So what is banned is the initialiser, and the allow-list is the one
    /// form of it that takes no name.** Anywhere outside the two vocabulary
    /// files, the only `Image` a view may build is `Image(nsImage:)`, which
    /// takes an already-loaded `NSImage` — `Components.AppIcon` drawing artwork
    /// off disk is the single call site, and it names nothing. Every other
    /// initialiser is a name in some form: `Image(_:)`, `Image(_:bundle:)`,
    /// `Image(decorative:)`, `Label(_:image:)`, `NSImage(named:)`.
    ///
    /// That makes this scan **stronger than one anchored on the literal**, and
    /// it is worth being precise about how, because "it can only check the four
    /// known names" was the answer to beat. Banning the constructor catches a
    /// name that never appears as a literal at all — `Image(chosenName)`, or a
    /// `switch` returning one, which is exactly the shape that cost the symbol
    /// inventory 13 of its 46 names. A scan for `"debPackage"` and its three
    /// siblings would find none of those, and would also go quietly out of date
    /// the day a fifth mark is drawn.
    ///
    /// What it still cannot do, stated so nobody reads more into it:
    ///
    /// - It cannot tell a *good* asset name from a *bad* one. `Marks.swift` is
    ///   exempt by construction, so the four names it declares are checked by
    ///   `MarksTests.test_every_declared_mark_resolves_from_the_asset_catalog`
    ///   and by nothing here.
    /// - It does not cover images that are not catalog assets at all.
    ///   `ArtworkStore` loads icons with `NSImage(contentsOfFile:)`, from paths
    ///   the CLI reports; those are files, not names, and are none of this
    ///   scan's business.
    func test_no_asset_name_escapes_the_vocabulary() throws {
        let offenders = try Self.offenders(of: Self.assetAPI, under: SourceScan.appSources())
        XCTAssertTrue(
            offenders.isEmpty,
            """
            \(offenders.count) image(s) built from a name outside \
            \(Self.vocabularyList): \(offenders.joined(separator: ", ")). \
            Declare the mark in `Marks` (\(Self.markVocabularyFile)) and draw \
            it with `.image`, `.image(size:)`, `.label(_:)` or \
            `.label(_:size:)`, so its frame and its template rendering are \
            decided once and its asset name is somewhere a test can resolve it. \
            `Image(nsImage:)` is the one exception, for an image that was \
            loaded from a path rather than named.
            """
        )
    }

    /// The scan both tests above run: every line under `root` that `api`
    /// matches, outside the two vocabulary files, as `<path below root>:<line>`.
    ///
    /// One function, taking its root, so a test can run it over a tree of its
    /// own (review #32): the exemption is keyed on the path below the root,
    /// and only a root reached through a symlink shows whether that path came
    /// from `SourceScan.relativePath` or from stripping `root.path`.
    static func offenders(of api: NSRegularExpression, under root: URL) throws -> [String] {
        let files = try SourceScan.swiftSources(under: root)
        XCTAssertFalse(files.isEmpty, "no Swift sources under \(root.path)")

        var offenders: [String] = []
        for file in files {
            let shown = SourceScan.relativePath(of: file, under: root)
            guard !vocabularyFiles.contains(shown) else { continue }
            for line in try SourceScan.codeLines(of: file) where api.matches(line.text) {
                offenders.append("\(shown):\(line.number)")
            }
        }
        return offenders
    }

    /// Both scans, over a tree reached through a symlink, as a checkout under
    /// `/tmp` is (review #32).
    ///
    /// `SourceScanPathTests` checks `SourceScan.relativePath` alone; nothing
    /// checked that the scans call it. In a checkout with no symlink on its
    /// path, stripping `root.path` (the expression still in `CLIContractTests`
    /// for display) gives the same names, and every test passed; through a
    /// symlink, the enumerator's resolved paths kept the link's spelling, the
    /// two vocabulary files stopped matching their exemption, and both scans
    /// reported them. Here each vocabulary file draws both ways, so neither
    /// scan can pass by skipping it for its own reason, and one view per scan
    /// is a real offender, so neither can pass by reporting nothing.
    func test_both_scans_name_files_below_a_symlinked_root() throws {
        let fm = FileManager.default
        let scratch = fm.temporaryDirectory.appendingPathComponent("symbolscan-\(UUID().uuidString)")
        let real = scratch.appendingPathComponent("real")
        defer { try? fm.removeItem(at: scratch) }
        let tree = [
            Self.vocabularyFile: "Image(systemName: name)\nImage(name)\n",
            Self.markVocabularyFile: "Image(systemName: name)\nImage(name)\n",
            "Views/Row.swift": "Image(systemName: \"trash\")\nImage(\"debPackage\")\n",
        ]
        for (path, text) in tree {
            let file = real.appendingPathComponent("Graft").appendingPathComponent(path)
            try fm.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
            try text.write(to: file, atomically: true, encoding: .utf8)
        }
        let link = scratch.appendingPathComponent("link")
        try fm.createSymbolicLink(at: link, withDestinationURL: real)
        // The link in the middle, as `/tmp` is in `/tmp/…/Graft`.
        let root = link.appendingPathComponent("Graft")

        XCTAssertEqual(try Self.offenders(of: Self.symbolAPI, under: root), ["Views/Row.swift:1"])
        XCTAssertEqual(try Self.offenders(of: Self.assetAPI, under: root), ["Views/Row.swift:2"])
    }

    /// Every way an image can be built from a *name*, minus the one way it
    /// cannot.
    ///
    /// `\bImage\(` does not match inside `NSImage(` — there is no word boundary
    /// between the `S` and the `I` — so AppKit's own constructor is matched by
    /// its own alternative below, and only in its name-taking `named:` form.
    /// `systemName:` is excluded here rather than reported twice; the scan above
    /// owns it, and a line reported by both tests names one mistake in two
    /// voices. (`systemSymbolName:` needs no exclusion: it only ever appears
    /// inside `NSImage(`, which `\bImage\(` cannot reach and `named:` does not
    /// match.)
    ///
    /// **`Label(_:image:)` gets two arms, and the pair is the interesting bit.**
    /// `\bimage:\s*"` catches any `image:` argument given a *literal*, wherever
    /// it appears. That alone left `Label("Dump", image: chosenName)` — the
    /// variable form, the same shape that cost the symbol inventory 13 of its
    /// 46 names — escaping, while the doc above claimed `Label(_:image:)` was
    /// covered. The second arm, `\bLabel\([^)]*\bimage:`, closes it by anchoring
    /// on the constructor instead, and takes anything in the argument. It has to
    /// be scoped to `Label(` rather than banning `image:` outright: this app
    /// passes `image:` to `Components.AppIcon` at six call sites and to
    /// `ArtworkStore.PathEntry` at two, none of which is naming a catalog asset.
    ///
    /// What is still uncovered, said plainly rather than implied away: a
    /// `Label(_:image:)` broken across lines so that `Label(` and `image:` are
    /// not on the same one. `SourceScan.codeLines` is line-based, so that is a
    /// limit of the whole scan and not of this pattern — the offender doc above
    /// says the same about a ternary that spills.
    private static let assetAPI = try! NSRegularExpression(
        pattern: #"\bImage\((?!\s*(?:nsImage|systemName):)"#
        + #"|\bimage:\s*""#
        + #"|\bLabel\([^)]*\bimage:"#
        + #"|\bNSImage\(\s*named:"#
    )

    // MARK: - Reading the sources

    /// The three argument labels a glyph name can arrive through.
    ///
    /// `systemSymbolName:` is AppKit's, and it was missing from this pattern
    /// until a review ran the hole: `Image(nsImage: NSImage(systemSymbolName:
    /// "trash", accessibilityDescription: nil)!)` passed the scan. **It is a
    /// reachable route and not a theoretical one** — `Components.AppIcon`
    /// already bridges an `NSImage` into SwiftUI with `Image(nsImage:)`, and
    /// `NSImage(systemSymbolName:)` is the ordinary way to give an
    /// `NSMenuItem`, an `NSToolbarItem` or a status item its image on macOS.
    /// Nothing in the app used it when the arm was added, so widening the
    /// pattern was a pure tightening; it is here to keep it that way.
    ///
    /// The order inside the group is presentational. Alternation is ordered and
    /// a shorter alternative can shadow a longer one that starts the same way,
    /// but these three differ at their first character, so none of them can
    /// consume a prefix of another.
    private static let symbolAPI =
        try! NSRegularExpression(pattern: #"system(Name|Image|SymbolName):"#)

    /// A literal introduced by `Symbol`'s own `name:` or `selectedName:`.
    ///
    /// Narrowed to the argument labels rather than taking any literal on the
    /// line, for the same reason the offender scan bans a label rather than a
    /// name: `Symbols.swift` is also full of ordinary strings, and a scan that
    /// swept them all would report the contents of an error message as a glyph
    /// that does not resolve.
    private static let glyphName =
        try! NSRegularExpression(pattern: #"\b(?:selectedName|name):\s*"([^"\\]*)""#)

    private func glyphNames(in line: String) -> [String] {
        let ns = line as NSString
        return Self.glyphName
            .matches(in: line, range: NSRange(location: 0, length: ns.length))
            .map { ns.substring(with: $0.range(at: 1)) }
    }
}

extension NSRegularExpression {
    func matches(_ line: String) -> Bool {
        firstMatch(in: line, range: NSRange(location: 0, length: (line as NSString).length)) != nil
    }
}

/// Reading the app's own source tree, for the scans that check what the
/// compiler cannot.
///
/// Written in `CLIContractTests` first, copied verbatim into `SymbolsTests`,
/// and lifted here when `MarksTests` was about to be the third copy. All three
/// suites now read through this one — `CLIContractTests` keeps three one-line
/// wrappers so its own call sites read as they did.
///
/// Shared rather than copied, because the comment-stripping rule in
/// `codeLines(of:)` is the subtle one: a copy that forgot it would report every
/// doc comment discussing `Image(` or quoting a `--flag` as an offender, and
/// two scans disagreeing about what counts as a source line is how one of them
/// quietly stops scanning.
enum SourceScan {

    /// The Swift sources this bundle was compiled from, located relative to
    /// this file. Absent (a build from an archived source tree) means skip
    /// rather than fail — the bargain every caller here strikes, and the same
    /// one the CLI half of `CLIContractTests` strikes for an absent binary: a
    /// source scan is a check, not a build gate.
    static func appSources() throws -> URL {
        let root = URL(fileURLWithPath: #filePath)   // …/GraftTests/SymbolsTests.swift
            .deletingLastPathComponent()             // …/GraftTests
            .deletingLastPathComponent()             // …/Graft (project dir)
            .appendingPathComponent("Graft")    // …/Graft/Graft (app sources)
        guard FileManager.default.fileExists(atPath: root.path) else {
            throw XCTSkip("app sources not found at \(root.path)")
        }
        return root
    }

    /// Every `.swift` file under `dir`, at any depth. `Views/` is a tree of
    /// per-section subdirectories, so the flat `contentsOfDirectory` the flag
    /// scan started as walked exactly one file (`ContentView.swift`) and
    /// reported a clean sweep over the eight folders that actually held the
    /// argument vectors.
    static func swiftSources(under dir: URL) throws -> [URL] {
        guard let walker = FileManager.default.enumerator(
            at: dir, includingPropertiesForKeys: nil
        ) else {
            throw XCTSkip("couldn't enumerate \(dir.path)")
        }
        return walker.compactMap { $0 as? URL }
            .filter { $0.pathExtension == "swift" }
            .sorted { $0.path < $1.path }
    }

    /// `file`'s path below `root`, both compared as real paths.
    ///
    /// `root` comes from `#filePath`, which keeps whatever spelling the project
    /// was opened through, while the enumerator hands back resolved URLs. So
    /// under `/tmp` (really `/private/tmp`), or any symlinked component,
    /// stripping `root.path` left `/privateDesignSystem/Symbols.swift`, and the
    /// exemptions keyed on `DesignSystem/Symbols.swift` stopped matching.
    /// `realpath(3)` rather than `resolvingSymlinksInPath()`, which strips a
    /// leading `/private` and so resolves the wrong way here.
    static func relativePath(of file: URL, under root: URL) -> String {
        let base = realPath(root) + "/"
        let path = realPath(file)
        return path.hasPrefix(base) ? String(path.dropFirst(base.count)) : path
    }

    static func realPath(_ url: URL) -> String {
        guard let resolved = realpath(url.path, nil) else { return url.standardizedFileURL.path }
        defer { free(resolved) }
        return String(cString: resolved)
    }

    /// Source lines with `//` comments dropped, so a doc comment that explains
    /// why `systemName:` is banned is not itself an offender. Three files
    /// discuss it in prose — `Symbols.swift`, `Marks.swift` and
    /// `BuildTargetBar` — and `Marks.swift`'s doc quotes `Image(` too.
    static func codeLines(of file: URL) throws -> [(number: Int, text: String)] {
        try String(contentsOf: file, encoding: .utf8)
            .components(separatedBy: .newlines)
            .enumerated()
            .map { (number: $0.offset + 1, text: $0.element) }
            .filter { !$0.text.trimmingCharacters(in: .whitespaces).hasPrefix("//") }
    }
}


/// Review #35: the exemptions in `SymbolsTests` are keyed on paths below the
/// app sources, and a checkout reached through a symlink (`/tmp` is one)
/// made every file's path come out with the link's spelling left on it.
final class SourceScanPathTests: XCTestCase {
    func test_a_file_reached_through_a_symlinked_root_is_named_from_the_root() throws {
        let scratch = FileManager.default.temporaryDirectory
            .appendingPathComponent("sourcescan-\(UUID().uuidString)")
        let real = scratch.appendingPathComponent("real")
        let link = scratch.appendingPathComponent("link")
        try FileManager.default.createDirectory(
            at: real.appendingPathComponent("Graft/DesignSystem"), withIntermediateDirectories: true
        )
        defer { try? FileManager.default.removeItem(at: scratch) }
        try "let x = 1\n".write(
            to: real.appendingPathComponent("Graft/DesignSystem/Symbols.swift"),
            atomically: true, encoding: .utf8
        )
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: real)
        // The link in the middle, as `/tmp` is in `/tmp/…/Graft`.
        let root = link.appendingPathComponent("Graft")

        let files = try SourceScan.swiftSources(under: root)

        XCTAssertEqual(files.count, 1, "premise: the walk reaches the file")
        XCTAssertNotEqual(files.first?.path.replacingOccurrences(of: root.path + "/", with: ""),
                          "DesignSystem/Symbols.swift",
                          "premise: the enumerator hands back the resolved spelling")
        XCTAssertEqual(files.map { SourceScan.relativePath(of: $0, under: root) },
                       ["DesignSystem/Symbols.swift"])
    }

    func test_the_temp_directory_s_own_symlink_does_not_leak_into_the_name() throws {
        let scratch = FileManager.default.temporaryDirectory
            .appendingPathComponent("sourcescan-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: scratch) }
        let file = scratch.appendingPathComponent("Marks.swift")
        try "let y = 2\n".write(to: file, atomically: true, encoding: .utf8)
        // `/var` -> `/private/var`: the same mismatch `/tmp` makes.
        let spelled = URL(fileURLWithPath: scratch.path.replacingOccurrences(of: "/private/var/", with: "/var/"))

        XCTAssertEqual(SourceScan.relativePath(of: file, under: spelled), "Marks.swift")
        XCTAssertEqual(SourceScan.relativePath(
            of: URL(fileURLWithPath: SourceScan.realPath(file)), under: spelled
        ), "Marks.swift")
    }
}
