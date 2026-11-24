import SwiftUI

/// One icon in the app's vocabulary that SF Symbols has no glyph for.
///
/// `Symbols` covers everything Apple has already drawn. This covers the four
/// nouns that are this app's own: a `.deb` archive, a container app running
/// another app's bundle inside itself, an App Store binary with FairPlay
/// stripped off it, and the developer identity an IPA is signed with. There are
/// four rather than five because a tweak mark was drawn and lost — seven
/// candidates were tried and none beat `puzzlepiece.extension` at 15pt, so
/// `Symbols.tweak` stays an SF Symbol and there is deliberately no gap here
/// for it.
///
/// **The two types are the same shape at the call site and deliberately not the
/// same shape inside.** `Symbol` carries a weight, a rendering mode and a
/// `.fill` variant because those are the three questions SF Symbols asks of
/// every glyph. A PDF in the asset catalog is asked none of them: its stroke is
/// cut into the artwork at 1.25pt, it has no symbolic rendering modes, and
/// there is no filled twin to swap to on selection. Copying the fields across
/// to look symmetrical would have added three properties that silently do
/// nothing.
///
/// What is mirrored instead is the call-site API — `image`, `image(size:)`,
/// `label(_:)` — **plus one method `Symbol` has no need of**. It is a superset,
/// not a mirror, and the extra member is where the media stop being
/// interchangeable: `label(_:size:)` exists because a `Symbol` inside a `Label`
/// inherits the ambient font and a `Mark` cannot. `Symbols.decrypt` became
/// `Marks.decrypt` at both its call sites by changing one token —
/// `DeviceDecryptSection`'s Decrypt button changed nothing else, and
/// `InstalledAppsSection`'s per-row Dump button also gained a `size: 11` for
/// exactly that reason, because it sits in a `.controlSize(.small)` button.
///
/// ## The placement contract
///
/// Measured by the drawing pass and re-measured against this checkout by
/// alpha-integrating supersampled renders of both sides:
///
/// - Every PDF is **16×16pt natural size carrying 15.5pt of ink**, at a 1.25pt
///   stroke matched to SF Symbols Regular at 15pt. SF's own stroke varies by
///   glyph — `minus` 1.311pt, `circle` 1.248, `iphone` 1.188, `rectangle` 1.179
///   — and 1.25 is the center of that spread.
/// - **A 16pt frame belongs beside an SF Symbol set at 15pt**, which is what
///   `canvas` and `matchedPointSize` name and `frame(forPointSize:)` extends.
///   It looks like an off-by-one and is not: **the point size is not the drawn
///   height**. At pointSize 15 the *ink* inside an SF Symbol runs 14.0pt
///   (`archivebox`) to 17.0pt (`tray.and.arrow.down`) on its long side, and the
///   icon-shaped glyphs cluster at 15.0–15.1 (`lock.open`,
///   `puzzlepiece.extension`, `circle`, `checkmark.circle.fill`, all 15.0 to
///   the 0.125pt this scan resolves). 15.5pt of mark ink on a 16pt frame lands
///   in the middle of that spread; on a 15pt frame it would sit at the bottom
///   of it.
///
///   An earlier draft of this bullet added "an SF Symbol's *canvas* is 16 to
///   19pt across", and that is **not a canvas and not a range**. `NSImage.size`
///   for a symbol is a per-glyph box, not a fixed square: at pointSize 15 the
///   heights do run 16–19 for icon-shaped glyphs, but the widths reach 22
///   (`tray.and.arrow.down`, `puzzlepiece.extension`, `rectangle`) and `minus`
///   reports a height of 4. Nothing here depends on that number; the pairing is
///   ink-to-ink, which is the measurement above.
/// - They are **template** images. The flag is on each imageset
///   (`template-rendering-intent`) *and* `.renderingMode(.template)` is applied
///   here, so a mark tints from `Palette` like the symbols around it rather
///   than arriving as flat black. `MarksTests` checks the catalog half; the
///   modifier is the belt to its braces.
///
/// The last two points are why the frame lives in this file rather than at the
/// call sites. A `Mark` never hands anyone the `Image`, so no call site *can*
/// write `.renderingMode(.original)`, and the only number a view supplies is a
/// point size — never a width and a height, which is the other way to get one
/// of these wrong with no visual warning at 11pt.
struct Mark {

    /// The imageset name in `Assets.xcassets`.
    ///
    /// A string the compiler never checks, exactly like `Symbol.name`, and with
    /// a worse failure mode: a misnamed SF Symbol renders a bordered
    /// placeholder box, while a missing asset renders *nothing* — a hole the
    /// width of the frame that reads as spacing. `MarksTests` resolves every
    /// one of these through `NSImage(named:)` for that reason.
    let asset: String

    /// The square canvas every PDF in this set is cut at.
    static let canvas: CGFloat = 16

    /// The SF Symbol point size `canvas` was matched to. See the contract above
    /// for the measurement; the pair is the whole of it.
    static let matchedPointSize: CGFloat = 15

    /// The point size a mark takes when the call site does not name one — 13pt,
    /// which is `AppFont.body` and the font macOS gives a regular-sized control.
    ///
    /// The two bare-form call sites are 13pt for two different reasons, and it
    /// is worth having both written down rather than the one an earlier draft
    /// generalized from. `DeviceDecryptSection`'s Decrypt button is a label
    /// inside a regular-sized `Button`, so 13pt is what macOS gives it. The
    /// other is not a button at all — it is the *heading* of `SettingsView`'s
    /// p12-password sheet, set explicitly to `AppFont.bodyMedium`, which is
    /// also 13. The default is right for both; only the first of them gets it
    /// from the control size.
    static let defaultPointSize: CGFloat = 13

    /// The frame that puts this mark's ink beside an SF Symbol drawn at
    /// `pointSize`.
    ///
    /// **`size:` means the same thing here as it does on `Symbol.image(size:)`,
    /// which is the reason this conversion exists at all.** That method sets
    /// `.font(.system(size:))` — a *point size*, not a frame — and an SF Symbol
    /// at pointSize 11 is not 11pt of ink. So a mark asked for "size 11" is
    /// being asked to sit with 11pt symbols, and the frame it needs to do that
    /// is sixteen-fifteenths of it. Passing the neighbour's number straight
    /// through to `.frame` would have read as matched while being ~7% short.
    ///
    /// **That 7% is a frame-to-frame figure — 16/15 − 1 — and not what the eye
    /// sees.** Said in ink, which is the quantity that actually has to match:
    /// the artwork carries 15.5pt on its 16pt frame, so a mark drawn at frame
    /// *p* shows 0.969*p* of ink, while the icon-shaped SF glyphs beside it
    /// carry about 1.00*p* on their long side at pointSize *p* (measured above).
    /// The naive spelling is therefore about **3% light**, and this correction
    /// lands about **3% heavy**. The sign is right and the magnitude is right;
    /// the 7% belongs to the frames.
    ///
    /// The ratio extends from the one measured pairing by scaling both sides,
    /// which is exact rather than assumed: a PDF scales with its frame, and an
    /// SF Symbol's geometry scales with its point size. Both of those 3%s are
    /// well inside the ±10% SF Symbols varies across its own glyphs at a fixed
    /// point size (14.0 to 17.0 around a 15.5 middle), so this is a correction
    /// of the right sign and roughly the right size rather than a claim of
    /// precision the artwork could not hold.
    static func frame(forPointSize pointSize: CGFloat) -> CGFloat {
        pointSize * canvas / matchedPointSize
    }
}

extension Mark {
    /// The mark at `Mark.defaultPointSize`.
    var image: some View { image(size: Mark.defaultPointSize) }

    /// The mark sized to sit with type and symbols at `size`.
    ///
    /// Size stays at the call site for the reason `Symbol.image(size:)` gives —
    /// it is set by the space the icon sits in, not by the icon's role — with
    /// one thing taken away from it: **the parameter is a point size, and the
    /// frame it produces is always square**. These are drawn on a square canvas
    /// and any other aspect stretches them.
    ///
    /// `.resizable()` is what makes the size mean anything, and it is safe
    /// because `preserves-vector-representation` is on for all four imagesets:
    /// the PDF is re-rasterised at the frame instead of a 16pt bitmap being
    /// scaled. 11pt through 32pt were checked on screen.
    func image(size: CGFloat) -> some View {
        Image(asset)
            .renderingMode(.template)
            .resizable()
            .frame(
                width: Mark.frame(forPointSize: size),
                height: Mark.frame(forPointSize: size)
            )
    }

    /// The mark paired with a title, for a button or a row.
    ///
    /// `Label(title:icon:)` for the same reason `Symbol.label(_:)` uses it: the
    /// `Label(_:image:)` spelling takes the asset name back as a string, which
    /// would put a bare glyph literal at the call site and hand the frame and
    /// the rendering mode back to whatever SwiftUI decided.
    func label(_ title: String) -> some View {
        Label { Text(title) } icon: { image }
    }

    /// `label(_:)` where the type beside it is not 13pt.
    ///
    /// A `Symbol` inside a `Label` inherits the ambient font and resizes with
    /// it; a `Mark` cannot, because an asset image has no font to inherit from.
    /// So a mark sharing a row with type that is not the default says so — a
    /// `.controlSize(.small)` button draws its label at 11pt — and without this
    /// the mark would be the one thing in that row that did not shrink.
    func label(_ title: String, size: CGFloat) -> some View {
        Label { Text(title) } icon: { image(size: size) }
    }
}

/// The app's custom marks, named for what they mean rather than what they look
/// like — the same rule `Symbols` follows, and load-bearing in the same way. A
/// call site that says `Marks.decrypt` does not care that the drawing is an
/// open padlock, which is what let `Symbols.decrypt` become this without either
/// call site changing anything but the word in front of the dot.
enum Marks {

    /// A `.deb` archive: a carton with an overhanging lid.
    ///
    /// **Not the same noun as `Symbols.tweak`, and the two legitimately share a
    /// row**, because a tweak ships inside a `.deb`. The line each surface draws
    /// is the one that matters: a row *titled* with a tweak's name is a tweak
    /// and takes `Symbols.tweak`, while the mono line under it that names the
    /// file — the string `config.yaml`, the build log and the cyan command all
    /// use — is the archive, and takes this. `AppDetailView`'s attached-tweak
    /// row draws both, one per line, which is the arrangement this comment
    /// exists to keep deliberate.
    static let debPackage = Mark(asset: "debPackage")

    /// LiveContainer: a guest square nested inside a host square.
    ///
    /// The delivery mode where the built IPA is handed to a container app that
    /// runs it inside its own sandbox, rather than being installed as an app of
    /// its own. The nesting is the whole drawing, because the nesting is the
    /// whole difference from a sideload.
    static let liveContainer = Mark(asset: "liveContainer")

    /// An App Store binary with FairPlay stripped: a file whose lock stands
    /// open.
    ///
    /// **The lock body is filled rather than outlined, and that is deliberate.**
    /// An outlined body draws a counter under the open shackle, and at 11pt the
    /// shackle and that counter read together as a lowercase "a". It is the only
    /// solid mass in the set; if it ever reads heavy in place the fix is a
    /// smaller body, not an outlined one.
    static let decryptedBinary = Mark(asset: "decryptedBinary")

    /// The Apple developer identity an IPA is signed with: a credential card
    /// with a seal.
    static let signingCertificate = Mark(asset: "signingCertificate")

    /// Dump an app's decrypted binary off the phone.
    ///
    /// An alias, not a fifth mark, and `Marks.all` lists the drawing once —
    /// which is why it is spelled as an alias rather than a second
    /// `Mark(asset:)` with the same string. It exists because `Symbols.decrypt`
    /// did: the accessor is named for the *action* both call sites take, and
    /// the artwork is named for the *thing* that action produces. Two names for
    /// one drawing is what made the change at those call sites one token, and
    /// it is the case `Symbols`' own doc comment predicted.
    static let decrypt = decryptedBinary

    /// Every mark above.
    ///
    /// Hand-written for the reason `Symbols.all` is: Swift cannot enumerate a
    /// caseless enum's static members, and the tests that resolve these names
    /// need something to iterate. Kept honest from the other side by
    /// `MarksTests.test_every_declared_mark_is_listed_in_all`, which reads this
    /// file's own source — a mark left out of here is an asset name nothing
    /// ever resolves, and a missing asset draws nothing at all.
    static let all: [Mark] = [
        debPackage, liveContainer, decryptedBinary, signingCertificate,
    ]
}
