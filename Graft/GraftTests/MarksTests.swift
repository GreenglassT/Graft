import XCTest
import AppKit
@testable import Graft

/// What keeps the four custom marks from becoming four holes in the UI.
///
/// The same two halves `SymbolsTests` has, plus a third this set needs and the
/// symbol set does not. One: every asset `Marks` names is really in the catalog
/// — the check that pays for itself, because a missing asset renders *nothing*
/// and nothing else in the build says so. Two: `Marks.all` still lists every
/// accessor, or the first check is only checking what somebody remembered.
/// Three: the artwork is still drawn to the placement contract `Mark`'s doc
/// comment states, which is the one claim in that comment a reader has no way
/// to verify by eye.
///
/// The half that bans a name from reaching SwiftUI outside this vocabulary
/// lives in `SymbolsTests.test_no_asset_name_escapes_the_vocabulary`, beside the
/// symbol scan it is the sibling of.
final class MarksTests: XCTestCase {

    // MARK: - Do the declared assets exist?

    /// Every asset `Marks` names resolves out of the app's catalog, as a
    /// template.
    ///
    /// **This is the check that pays for itself, and it is worse odds than the
    /// symbol one.** A misspelled SF Symbol at least renders a bordered
    /// placeholder box — visible, if anyone looks at that state. A misspelled
    /// asset name renders nothing at all: the frame collapses and the row closes
    /// up, which looks exactly like a row that was never given an icon. Three of
    /// these four draw in places a screenshot is unlikely to reach —
    /// `signingCertificate` needs the p12 preflight check to be failing,
    /// `debPackage`'s app-pane site needs two attached tweaks whose display
    /// names collide — so for those this is the only verification that will ever
    /// happen.
    ///
    /// `isTemplate` is asserted for a separate defect with a separate symptom.
    /// The flag lives in each imageset's `Contents.json`
    /// (`template-rendering-intent`), and an asset that lost it still resolves,
    /// still draws, and still passes every other check here — it simply arrives
    /// as flat black artwork instead of taking `Palette` from the view around
    /// it. On this app's navy surfaces that is a black-on-navy smudge rather
    /// than a missing icon, which is harder to spot and easier to ship.
    func test_every_declared_mark_resolves_from_the_asset_catalog() {
        XCTAssertFalse(Marks.all.isEmpty, "Marks.all is empty; this test reads nothing")
        for mark in Marks.all {
            guard let image = NSImage(named: mark.asset) else {
                XCTFail(
                    """
                    "\(mark.asset)" is not in Assets.xcassets. It draws nothing \
                    wherever it is used — not a placeholder, nothing — and \
                    nothing else in the build says so.
                    """
                )
                continue
            }
            XCTAssertTrue(
                image.isTemplate,
                """
                "\(mark.asset)" resolved but is not a template image. It will \
                draw as flat artwork instead of tinting from Palette. Check \
                "template-rendering-intent": "template" in its Contents.json.
                """
            )
        }
    }

    /// `Marks.all` lists every accessor.
    ///
    /// Same argument as `SymbolsTests.test_every_declared_symbol_is_listed_in_all`:
    /// `all` is hand-written because Swift cannot enumerate a caseless enum's
    /// static members, so it can silently fall behind the accessors above it,
    /// and a mark left out of `all` is an asset name the resolution test never
    /// resolves.
    ///
    /// The scan matches `asset:` literals, which is why `Marks.decrypt` is
    /// spelled as an alias of `decryptedBinary` rather than as a second
    /// `Mark(asset: "decryptedBinary")`. Written the second way it would be a
    /// fifth literal here against four entries in `all`, and this test would be
    /// demanding that one drawing appear twice in a list of the set.
    ///
    /// **That claim was false until the assertion below became an equality**,
    /// which is worth recording because it is the exact failure this suite is
    /// for. Under `>=`, a fifth literal made `declared` 5, `5 >= 4` passed, and
    /// the subtraction found nothing missing because the name was already
    /// listed — so nothing at all enforced the alias spelling that the comment
    /// called load-bearing, and
    /// `test_decrypt_is_an_alias_for_the_decrypted_binary_mark` does not either
    /// (it catches re-pointing and typos, not the rewrite). A doc that names a
    /// constraint no assertion holds is worse than no doc: it is read as proof.
    func test_every_declared_mark_is_listed_in_all() throws {
        let file = try SourceScan.appSources()
            .appendingPathComponent(SymbolsTests.markVocabularyFile)
        let declared = try SourceScan.codeLines(of: file).flatMap { Self.assetNames(in: $0.text) }
        XCTAssertFalse(declared.isEmpty, "found no mark declarations in \(file.path)")
        // Exactly one `asset:` literal per entry in `all`, both directions.
        // This was `>=` and had the same two weaknesses the symbol side's did
        // (see `SymbolsTests.test_every_declared_symbol_is_listed_in_all`), and
        // they were less visible here only because the margin happened to be
        // zero: a `>=` that reads 4 against 4 fails the degradation mutation by
        // luck rather than by design, and it would go back to passing the
        // moment a fifth mark was added. The other half is real today — the
        // subtraction below is over *distinct* asset names, so a second
        // `Mark(asset: "debPackage")` accessor left out of `all` would go
        // unnoticed by it, and this line is what sees it.
        XCTAssertEqual(
            declared.count, Marks.all.count,
            "matched \(declared.count) asset name(s) in \(SymbolsTests.markVocabularyFile) "
            + "against \(Marks.all.count) in Marks.all. More declared than listed means an "
            + "accessor is missing from `all`; fewer means this scan has stopped matching "
            + "the way the file is written. `Marks.decrypt` is an alias and contributes no "
            + "literal, which is why it is spelled as one."
        )

        let listed = Set(Marks.all.map(\.asset))
        let missing = Set(declared).subtracting(listed).sorted()
        XCTAssertTrue(
            missing.isEmpty,
            """
            \(missing.count) asset name(s) declared in \
            \(SymbolsTests.markVocabularyFile) are not reachable from Marks.all: \
            \(missing.joined(separator: ", ")). Add the accessor to `all`, or \
            nothing ever checks that its asset is there.
            """
        )
    }

    /// `Marks.decrypt` is `Marks.decryptedBinary`, not a second drawing.
    ///
    /// The accessor is named for the action (dump a binary off the phone) and
    /// the asset for the thing that action produces, which is the aliasing that
    /// let `Symbols.decrypt` become `Marks.decrypt` at both call sites by
    /// changing one word. Pinned because the two names invite being pointed at
    /// two files by anyone tidying up.
    func test_decrypt_is_an_alias_for_the_decrypted_binary_mark() {
        XCTAssertEqual(Marks.decrypt.asset, Marks.decryptedBinary.asset)
    }

    // MARK: - Is the artwork still drawn to the contract?

    /// The frame `Mark` asks for is sixteen-fifteenths of the point size beside
    /// it.
    ///
    /// The arithmetic half of the placement contract, which is the half a test
    /// can hold. `frame(forPointSize: 15) == 16` is the measured pairing itself;
    /// the other two rows are there because the correction has to be
    /// *proportional* to be right at any other size, and a `+1` would pass the
    /// first row and fail these.
    func test_the_frame_is_the_paired_canvas_for_a_point_size() {
        XCTAssertEqual(Mark.frame(forPointSize: Mark.matchedPointSize), Mark.canvas, accuracy: 0.001)
        XCTAssertEqual(Mark.frame(forPointSize: 30), 32, accuracy: 0.001)
        XCTAssertEqual(Mark.frame(forPointSize: 11), 11 * 16.0 / 15.0, accuracy: 0.001)
        // A mark asked to sit with 11pt symbols must come out larger than 11pt,
        // or the correction is being applied in the wrong direction — which is
        // a mistake that looks like a rounding error and reads as a mark that
        // is quietly too small everywhere.
        XCTAssertGreaterThan(Mark.frame(forPointSize: 11), 11)
    }

    /// Every PDF is still cut at 16×16 with 15.5pt of ink in it.
    ///
    /// **The measurement `Mark`'s doc comment is built on, checked rather than
    /// asserted in prose.** Both numbers matter and they fail differently. The
    /// canvas is what makes `Mark.canvas` true: a PDF re-cut at 24×24 would
    /// still resolve, still tint, and still draw — at two-thirds the intended
    /// ink inside every frame this app gives it, because the frame is fixed and
    /// the artwork scales to fill it. The ink is what makes these sit with SF
    /// Symbols: 15.5pt on the 16pt canvas is the middle of the 14.0–17.1pt
    /// spread SF's own glyphs occupy at pointSize 15, and a regenerated PDF that
    /// changed its padding would land outside that without changing anything a
    /// reader could see in the catalog.
    ///
    /// The larger of the two ink dimensions, because two of these four are not
    /// square inside the square canvas. As this scan measures them at 8×:
    /// `debPackage` and `liveContainer` are 15.5 square, `decryptedBinary` is
    /// 11.25 × 15.5, and `signingCertificate` is 15.5 × 11.75.
    ///
    /// So for three of the four the long side *is* the height, which is what
    /// gets matched to the neighbouring glyphs' ink height. `signingCertificate`
    /// is the exception — a credential card, wider than it is tall — and for
    /// that one this assertion is matching a width against the neighbours'
    /// height. It works out because the constraint that matters is "the mark
    /// fills the same share of its frame as an SF Symbol fills of its own", and
    /// a landscape glyph is bounded by its width; it is worth saying rather than
    /// leaving the reader to notice that `max` is doing two different jobs.
    ///
    /// Measured by alpha-integrating an 8× supersampled render, which is how the
    /// drawing pass measured it — an unsampled 16×16 scan quantises the answer
    /// to whole points and could not tell 15.5 from 16.
    func test_every_mark_is_drawn_to_the_placement_contract() throws {
        for mark in Marks.all {
            guard let image = NSImage(named: mark.asset) else {
                XCTFail("\(mark.asset) is missing; see the resolution test")
                continue
            }
            XCTAssertEqual(
                image.size.width, Mark.canvas, accuracy: 0.01,
                "\(mark.asset) is \(image.size.width)pt wide, not Mark.canvas"
            )
            XCTAssertEqual(
                image.size.height, Mark.canvas, accuracy: 0.01,
                "\(mark.asset) is \(image.size.height)pt tall, not Mark.canvas"
            )

            let ink = try XCTUnwrap(Self.inkExtent(of: image), "\(mark.asset) rendered no ink")
            XCTAssertEqual(
                max(ink.width, ink.height), Self.expectedInk, accuracy: Self.inkTolerance,
                """
                \(mark.asset) carries \(ink.width)×\(ink.height)pt of ink on a \
                \(Mark.canvas)pt canvas; the contract is \(Self.expectedInk)pt on \
                the long side. Beside SF Symbols at \(Mark.matchedPointSize)pt \
                this now reads as the wrong size.
                """
            )
        }
    }

    /// The ink height every mark is cut to. See `Mark`'s placement contract.
    private static let expectedInk: CGFloat = 15.5

    /// How far a regenerated PDF may drift before this is a different drawing.
    ///
    /// **0.25, and it was 0.5 until a review pointed out what 0.5 admits**: on a
    /// 16pt canvas it accepts anything in [15.0, 16.0], and 16.0 is full bleed —
    /// artwork with no padding at all would have passed the check that exists to
    /// notice padding changing.
    ///
    /// 0.25 is not tight for its own sake; it is set against two measurements.
    /// All four PDFs carry 15.4375pt on the long side when scanned at 32× — so
    /// the real deviation from 15.5 is 0.0625, four times inside this — and
    /// `inkExtent` samples at 8×, which quantises its answer to 0.125pt (it
    /// reports these four as exactly 15.5). The tolerance has to clear the
    /// scan's own resolution or the sampling rate would be deciding the result.
    /// 0.25 is the smallest round number above both.
    ///
    /// Checked by mutation rather than by argument: with `expectedInk` moved to
    /// 15.9 all four marks fail here, and under the old 0.5 all four would have
    /// passed.
    private static let inkTolerance: CGFloat = 0.25

    /// The bounding box of everything non-transparent in `image`, in points.
    ///
    /// Drawn into a square `Mark.canvas` bitmap at `scale`× and scanned for
    /// alpha, which is the only way to ask this question of a vector asset: the
    /// PDF's own MediaBox is the canvas, not the ink, and `NSImage.size` reports
    /// the same. The alpha floor is 8/255 rather than 0 so the antialiased skirt
    /// of a 1.25pt stroke does not read as another quarter-point of drawing on
    /// each side.
    private static func inkExtent(
        of image: NSImage, scale: CGFloat = 8
    ) -> (width: CGFloat, height: CGFloat)? {
        let px = Int(Mark.canvas * scale)
        guard let ctx = CGContext(
            data: nil, width: px, height: px, bitsPerComponent: 8, bytesPerRow: px * 4,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) else { return nil }
        ctx.clear(CGRect(x: 0, y: 0, width: px, height: px))

        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(cgContext: ctx, flipped: false)
        image.draw(
            in: CGRect(x: 0, y: 0, width: CGFloat(px), height: CGFloat(px)),
            from: .zero, operation: .sourceOver, fraction: 1.0
        )
        NSGraphicsContext.restoreGraphicsState()

        guard let data = ctx.data else { return nil }
        let buf = data.bindMemory(to: UInt8.self, capacity: px * px * 4)
        var minX = px, maxX = -1, minY = px, maxY = -1
        for y in 0..<px {
            for x in 0..<px where buf[(y * px + x) * 4 + 3] > 8 {
                minX = min(minX, x); maxX = max(maxX, x)
                minY = min(minY, y); maxY = max(maxY, y)
            }
        }
        guard maxX >= 0 else { return nil }
        return (CGFloat(maxX - minX + 1) / scale, CGFloat(maxY - minY + 1) / scale)
    }

    /// A literal introduced by `Mark`'s own `asset:`.
    ///
    /// Narrowed to the argument label for the reason `SymbolsTests.glyphName`
    /// is: `Marks.swift` is mostly prose, and a scan that took any literal on a
    /// line would report the contents of a doc comment as an asset that does not
    /// resolve.
    private static let assetName =
        try! NSRegularExpression(pattern: #"\basset:\s*"([^"\\]*)""#)

    private static func assetNames(in line: String) -> [String] {
        let ns = line as NSString
        return assetName
            .matches(in: line, range: NSRange(location: 0, length: ns.length))
            .map { ns.substring(with: $0.range(at: 1)) }
    }
}
