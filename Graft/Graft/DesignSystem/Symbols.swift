import SwiftUI

/// One icon in the app's vocabulary: which glyph, how heavy, and how it
/// renders.
///
/// The three travel together because they are one decision. An 11pt readiness
/// tick beside `AppFont.monoSmall` and an 18pt drop-zone tray want different
/// weights to sit at the same optical density as the text next to them, and
/// that pairing is a property of the icon's *role*, not of the view that
/// happens to draw it. Held as a value, the decision is made once and every
/// call site inherits it; spelled at the call site, it drifts — which is what
/// this file was written out of.
///
/// The count, recounted rather than quoted: **46 bare glyph-name literals, 32
/// distinct names, across 13 files** at the commit before this one. Not one of
/// them set a rendering mode, and six set a weight. `arrow.clockwise` appears
/// six times and only one of the six said what font it wanted (10pt medium, on
/// the sidebar's locked-phone row); the other five took whatever their
/// container happened to be setting, which is a different answer per container
/// and nothing recording that the six were meant to be one icon.
///
/// **A glyph name is a string the compiler never checks.** A typo renders an
/// empty box, ships, and nothing fails. `SymbolsTests` resolves every name
/// declared here through `NSImage(systemSymbolName:)`, which is the only check
/// that exists for this class of defect — and it can only cover names that come
/// through here, which is why `test_no_symbol_literal_escapes_the_vocabulary`
/// bans the three argument labels that let one in anywhere else: SwiftUI's
/// `systemName:` and `systemImage:`, and AppKit's own `systemSymbolName:` —
/// the route an `NSMenuItem`, an `NSToolbarItem` or a status-item image takes,
/// and one this app can reach today because `Components.AppIcon` already
/// bridges an `NSImage` into SwiftUI with `Image(nsImage:)`.
struct Symbol {
    /// The SF Symbol name.
    let name: String

    /// The glyph for a *selected* row, when the selected form is a different
    /// symbol rather than the same one drawn differently.
    ///
    /// `nil` is not "this symbol has no selected state" — it is "the fill
    /// variant does not exist", and it is a fact about SF Symbols rather than a
    /// gap here. `.symbolVariant(.fill)`, the idiomatic way to do this, silently
    /// leaves a symbol unchanged when it has no `.fill`: of the six section
    /// rows in the sidebar, `puzzlepiece.extension`, `tray.and.arrow.down`,
    /// `archivebox` and `gearshape` have fills and `magnifyingglass` and
    /// `list.bullet` do not (verified against `NSImage(systemSymbolName:)` on
    /// macOS 15, and again for `archivebox` when the Cache row was added).
    /// Applied blindly it would fill four rows out of six and make selection
    /// look like a rendering bug on the other two. So the selected form is declared
    /// per symbol, and `image(selected:)` falls back to a weight step for the
    /// two that cannot have one — see there.
    var selectedName: String? = nil

    /// **`.monochrome` everywhere, so far — decided on the icons it changes
    /// most, and then applied uniformly on purpose.**
    ///
    /// `.hierarchical` is the obvious reach for an icon carrying state in its
    /// own color, and the argument against it is about *seven* of the symbols
    /// below rather than all of them. Those seven have an interior mark cut out
    /// of a filled body as a genuine hole — `ok`, `blocked`, `advisory`,
    /// `detach`, `updateAvailable`, `failure` and `checkboxOn`. In monochrome
    /// the hole is the surface color and the body is the state color, which
    /// is what makes a tick and a cross tell each other apart at 11pt across
    /// the width of a card.
    ///
    /// **What hierarchical does to those is not what an earlier draft of this
    /// comment said**, and the difference is the whole reason the note is worth
    /// keeping: it does *not* make the mark vanish. It draws the mark at full
    /// opacity into what was the hole and drops the body to 0.30 — so the mark
    /// still reads, and the body gives up seven tenths of the contrast it had
    /// against the surface. The knockout is spent, not the glyph. Measured on
    /// macOS 26.5 by rendering each glyph both ways and differencing: the hole
    /// is 4–9% of the inked area on all seven, hierarchical fills 100% of it,
    /// and the surviving body sits at alpha 76–77 of 255.
    ///
    /// The decision was taken on those at the sizes they are actually drawn:
    /// 11pt (the decrypt card's three readiness rows), 14pt (`detach`) and
    /// 18pt (`dropIPA`'s tray).
    ///
    /// **The other twenty-eight are not that shape, and they are covered by
    /// policy rather than by that argument.** Same measurement, all 35
    /// accessors classified:
    ///
    /// - **Nineteen are single-layer**, and hierarchical changes nothing about
    ///   them — every inked pixel keeps full opacity and no hole is filled:
    ///   `tweak`, `canisterSection`, `cacheSection`, `settingsSection`, `add`,
    ///   `refresh`, `delete`, `dropExtensions`, `buildOptions`, `dismiss`,
    ///   `clearOption`, `search`, `checkboxOff`, `encrypted`, `note`, `device`,
    ///   `deviceLocked`, `scanning`, `notDelivering`. The field could say
    ///   anything for these and nothing would move. (`gearshape.fill` has a hole
    ///   in it and is still in this group — a hole is not a second layer, and
    ///   hierarchical leaves it alone.)
    ///   `cacheSection` was measured when it was added rather than assumed into
    ///   the group, by the method `cacheSection`'s own doc spells out. Stated
    ///   as the proportion that reproduces rather than as raw subpixel counts,
    ///   which move with the rasteriser: hierarchical leaves **100%** of
    ///   `archivebox`'s full-alpha ink at full alpha, and its total inked area
    ///   unchanged — the same answer `gearshape` and `puzzlepiece.extension`
    ///   give. The two-layer section rows in the same run are unmistakable
    ///   beside it, keeping **16%**: `tray.and.arrow.down` and `list.bullet`
    ///   both give up roughly five sixths of their full-alpha ink to the 0.50
    ///   enclosure.
    /// - **Nine are two-layer with no hole**, and hierarchical is emphatically
    ///   *not* a no-op on them: `reposSection`, `buildsSection`, `checkUpdates`,
    ///   `revealInFinder`, `rowMenu`, `dropIPA`, `attention`, `noDefaultDevice`,
    ///   `noDevice`. It keeps 10–30% of the ink — the interior mark, the
    ///   bullets, the arrow — at full and drops the enclosure to 0.50.
    ///   `dropIPA`'s arrow is a *second layer* and not a knockout, which an
    ///   earlier draft of this comment also had backwards.
    /// - `checkboxOn` is one of the seven above but is a filled *square*, so
    ///   "disc" was never the right word for the shape the argument is about.
    ///
    /// So for `attention` and `noDefaultDevice` — the two of the nine that
    /// carry state in their own color, `Palette.warn` and `BuildTargetBar`'s
    /// tint — a mixed policy is available and is deliberately not taken.
    /// Halving the enclosure would make the glyph quieter than the amber
    /// sentence it sits beside, and more to the point: **a rendering mode that
    /// is `.monochrome` for thirty-three icons and something else for two is a
    /// rule nobody can hold in their head, and the next icon gets whichever one
    /// was copied.** A uniform policy that is slightly suboptimal on two glyphs
    /// beats a correct one that is not followed.
    ///
    /// The field stays because the next icon may not be one of these shapes,
    /// and because rendering mode is a property of the *icon* — a call site
    /// that has to remember to set it is the arrangement this file replaced.
    var rendering: SymbolRenderingMode = .monochrome

    /// The weight the glyph is drawn at, chosen against the type beside it.
    ///
    /// **The weights here run the opposite way to the heuristic that was
    /// written down for this work, and that is on purpose — recorded because
    /// the next reader is otherwise going to "fix" it back.** The instruction
    /// said an 11pt label wants a *lighter* symbol than a 13pt one. Every
    /// weight below does the reverse, and consistently: `clearOption` is
    /// `.bold` at the app's smallest icon size (8pt), `encrypted` and `refresh`
    /// are `.medium` at 10pt, `buildOptions` is `.semibold` at 10pt, and the
    /// glyphs that inherit an ambient 13pt are mostly `.regular`.
    ///
    /// Heavier-as-smaller is the optically conventional direction: stroke width
    /// shrinks with point size, so a `.regular` stroke that reads correctly at
    /// 13pt is a fainter mark than the type beside it at 10pt, and fainter
    /// still at 8. Each accessor's own doc gives the specific case — the
    /// `arrow.clockwise` ring at 10pt in `Palette.textMuted` was the faintest
    /// thing in the sidebar, the × is two thin diagonals with nothing else to
    /// it. The heuristic was the thing that was wrong here, not the code.
    var weight: Font.Weight = .regular
}

extension Symbol {
    /// The glyph at whatever font size it inherits.
    ///
    /// For the call sites sitting inside a `.font(...)` scope that already sets
    /// the size — a notice strip at `AppFont.monoSmall`, a toolbar button at the
    /// system default. `fontWeight` overrides only the weight of the resolved
    /// font, so this keeps the inherited size and applies this symbol's weight
    /// on top of it.
    var image: some View {
        Image(systemName: name)
            .symbolRenderingMode(rendering)
            .fontWeight(weight)
    }

    /// The glyph at an explicit point size.
    ///
    /// Size is the one property of an icon that stays at the call site: it is
    /// set by the space the icon sits in — an 8pt × inside a capsule pill, a
    /// 10pt lock in a list row — and two views drawing the same role at
    /// different sizes are not disagreeing about anything. Weight and rendering
    /// mode are; those live on the symbol.
    func image(size: CGFloat) -> some View {
        Image(systemName: name)
            .symbolRenderingMode(rendering)
            .font(.system(size: size, weight: weight))
    }

    /// The glyph paired with a title, for a button or a sidebar row.
    ///
    /// `Label(title:icon:)` rather than `Label(_:systemImage:)` because the
    /// latter takes the name back as a string and throws the weight away. The
    /// rendered type is still a `Label`, so `.labelStyle`, toolbar adaptation
    /// and the automatic icon-only collapse in a compressed toolbar all behave
    /// as they did.
    func label(_ title: String) -> some View {
        Label { Text(title) } icon: { image }
    }

    /// The glyph for a row that may be selected.
    ///
    /// Two mechanisms, because SF Symbols only offers the first for some of
    /// them (see `selectedName`): swap to the filled form when there is one,
    /// and otherwise step the weight up. The step is `.semibold`, which is the
    /// smallest change that still reads as a different state at 13pt — the
    /// weight below it, `.medium`, is not distinguishable from `.regular` at
    /// that size, and the one above, `.bold`, makes the two unfilled rows
    /// louder than the four filled ones rather than merely equal to them.
    /// 13pt throughout: `SidebarView.row` sets `AppFont.body`, which is 13, and
    /// the glyph inherits it. (A `Label` at pointSize 13 draws a *canvas* wider
    /// than 13pt — `Mark`'s placement contract spells that out — but the number
    /// being chosen against here is the point size, which is what both the type
    /// and the symbol are set at.)
    ///
    /// **`.semibold` is a constant, not a step *from* `weight`, and that is an
    /// assumption rather than a derivation.** It is right for both symbols that
    /// can reach it — `canisterSection` and `buildsSection` are the two sidebar
    /// sections with no `.fill` variant, and both are `.regular` — and it would
    /// be *backwards* for an accessor declared heavier than `.semibold`, which
    /// would get lighter on selection. Nothing declares one today. Anything
    /// that does, and that is drawn through here, needs this line changed
    /// rather than the accessor: no test can catch it, because a weight is not
    /// a name and nothing resolves it.
    func image(selected: Bool) -> some View {
        Image(systemName: selected ? (selectedName ?? name) : name)
            .symbolRenderingMode(rendering)
            .fontWeight(selected && selectedName == nil ? .semibold : weight)
    }

    /// `label(_:)` for a row that may be selected.
    func label(_ title: String, selected: Bool) -> some View {
        Label { Text(title) } icon: { image(selected: selected) }
    }

    /// Every glyph name this symbol can draw, for the resolution test.
    var names: [String] { [name] + (selectedName.map { [$0] } ?? []) }
}

/// The app's icons, named for what they mean rather than what they look like.
///
/// **The names are roles on purpose, and it is what keeps the set swappable.**
/// Three separate accessors here are `magnifyingglass` — a search field's
/// affordance, the Canister destination, and "still looking for a phone" in the
/// build-target bar — and one day two of those will want to stop being the same
/// drawing. A call site that says `Symbols.scanning` can be re-pointed at
/// anything, including an image out of the asset catalog, by editing one
/// declaration; a call site that says `"magnifyingglass"` has to be found first,
/// and the other two sites tell you nothing about whether they meant the same
/// thing. `Symbols.decrypt` was the live case, and it is the reason this file
/// no longer has one: the mark it stood in for is drawn and wired up, so the
/// accessor moved wholesale to `Marks.decrypt`.
///
/// **What that move actually cost, stated precisely, because an earlier draft
/// of this paragraph rounded it to "one word each" and rested the case for role
/// naming on it.** Both call sites kept their shape — `DeviceDecryptSection`'s
/// Decrypt button is still `<accessor>.label(<title>)` and
/// `InstalledAppsSection`'s per-row Dump button is still
/// `<accessor>.label("Dump", …)` — and neither had to learn that the drawing is
/// now a PDF of an open padlock instead of `lock.open`. That is the property
/// role naming buys, and it held.
/// It was not free at both sites: the second one gained a `size: 11`, because
/// it sits in a `.controlSize(.small)` button and a `Mark` cannot inherit an
/// ambient font the way a `Symbol` can. That is a fact about the *media*, not
/// about the naming — see `Mark.label(_:size:)`, which exists for it — but it
/// is the difference between "the call sites did not care what the icon is" and
/// "the call sites did not change", and only the first of those is true.
///
/// One decision did vanish silently in the move, and is recorded here because
/// nothing else records it: `Symbols.decrypt` was `Symbol(name: "lock.open",
/// weight: .medium)`, and a `Mark` has no weight — its stroke is cut into the
/// artwork at 1.25pt, matched to SF Symbols *Regular*. So both decrypt icons
/// are now optically a step lighter than the symbol they replaced. That is
/// inherent to a PDF and not a regression to fix at the call site; the remedy,
/// if it ever reads too light, is a heavier stroke in the artwork.
enum Symbols {

    // MARK: - Destinations

    /// The Tweaks section, and the bullet on one attached tweak in the app
    /// pane. Both mean "a tweak", so both are one accessor: a change to what a
    /// tweak looks like should not be able to land in one place and not the
    /// other.
    static let tweak = Symbol(
        name: "puzzlepiece.extension",
        selectedName: "puzzlepiece.extension.fill"
    )

    /// Canister — a search across repo indexes the user has not added.
    ///
    /// Deliberately a separate accessor from `search`, which is the glyph *in*
    /// a filter field. They are the same drawing and not the same thing: one is
    /// a place, one is a control, and the place is the one that would take a
    /// mark of its own.
    static let canisterSection = Symbol(name: "magnifyingglass")

    /// Repos — the tray a .deb is pulled down into.
    static let reposSection = Symbol(
        name: "tray.and.arrow.down",
        selectedName: "tray.and.arrow.down.fill"
    )

    /// Cache — the box a dump staged an IPA into and nothing has taken it out
    /// of.
    ///
    /// Declared here rather than appended at the end of the enum, and listed at
    /// the matching position in `all`: this group is in *sidebar order* —
    /// Tweaks, Canister, Repos under Library, then Builds and Settings under
    /// Activity — and Cache is the fourth Library row. `all` mirrors the
    /// declaration order group for group, so the two can be read side by side.
    ///
    /// **`.regular` and a `selectedName`, where the plan for this row asked for
    /// `.medium` and no fill.** Both were changed against a measurement rather
    /// than a preference. Method, stated so it can be re-run: each glyph
    /// rendered through `NSImage(systemSymbolName:)` at **13pt `.regular`** —
    /// the size and weight this row actually ships at, `SidebarView.row`'s
    /// `AppFont.body` — rasterised at 4×, counting subpixels with alpha above
    /// 1%. Absolute counts move with the rasteriser and are not quoted; the
    /// proportions below held across two independent runs.
    ///
    /// - `archivebox` inks **34%** of its canvas, against `gearshape` 36%,
    ///   `puzzlepiece.extension` 30% and `tray.and.arrow.down` 27%. That is
    ///   mid-range for this group — a little lighter than `gearshape`, heavier
    ///   than the other two — so the heavier-as-smaller compensation
    ///   `Symbol.weight` describes has nothing to correct here. The decision is
    ///   really the simpler one: every other row in this group is `.regular`,
    ///   and a `.medium` Cache row would be the one Library entry drawn heavier
    ///   than its neighbours for no reason anyone could reconstruct.
    /// - `archivebox.fill` inks **54%**, between `gearshape.fill` (47%) and
    ///   `puzzlepiece.extension.fill` (57%) — an ordinary fill, not a blob. So
    ///   the fill exists and reads, and taking it keeps `image(selected:)`'s
    ///   rule intact: the filled form wherever SF Symbols has one, the weight
    ///   step only for `canisterSection` and `buildsSection`, which have none.
    static let cacheSection = Symbol(
        name: "archivebox",
        selectedName: "archivebox.fill"
    )

    /// Builds — the log of what has been built, as a list.
    ///
    /// One of the two sidebar sections with no `.fill` variant (the other is
    /// `canisterSection`), so its selected state is the weight step in
    /// `image(selected:)` rather than a second glyph. A list rather than a
    /// hammer or a box: the section is a *table of past builds*, and the icon
    /// that says "a build" would be wrong for a destination that is a record of
    /// them.
    static let buildsSection = Symbol(name: "list.bullet")

    static let settingsSection = Symbol(
        name: "gearshape",
        selectedName: "gearshape.fill"
    )

    // MARK: - Actions

    /// Add an app, attach a tweak.
    ///
    /// `.medium` because one of the two sites is the sidebar's toolbar button,
    /// where the plus is the entire control — no label beside it, nothing else
    /// in the row to carry the affordance.
    static let add = Symbol(name: "plus", weight: .medium)

    /// Re-read something from disk, the phone, or the network.
    ///
    /// The app's most-repeated icon: six sites, from a 10pt bare glyph on a
    /// locked-phone sidebar row to a full `Label` in the Settings header.
    /// `.medium` is set by the smallest of them — `arrow.clockwise` is a thin
    /// open ring, and at 10pt in `Palette.textMuted` the `.regular` stroke was
    /// the faintest mark in the sidebar.
    static let refresh = Symbol(name: "arrow.clockwise", weight: .medium)

    /// Move a tweak to the Trash — and, on a Cache row, delete a staged IPA
    /// outright. The two are not the same act (`--delete-tweak` trashes,
    /// `--delete-staged` unlinks) and each confirm says which; the *icon* for
    /// "get rid of this" is one drawing either way, which is what this
    /// vocabulary is for.
    static let delete = Symbol(name: "trash", weight: .medium)

    /// Hand a staged IPA to PlayCover, to run on this Mac.
    ///
    /// A Mac, not a download arrow or an app icon: the fact this control adds
    /// is *where* the app ends up, and the row it sits on already says the
    /// file is here. `.medium` for the reason `add` is — it is drawn as a bare
    /// 24pt glyph button with no label beside it, so the glyph is the whole
    /// control.
    static let installToMac = Symbol(name: "desktopcomputer", weight: .medium)

    /// Detach a tweak from an app. See `Symbol.rendering` for why this is one
    /// of the filled discs that has to stay monochrome.
    static let detach = Symbol(name: "minus.circle.fill", weight: .regular)

    // `decrypt` used to be here, as `lock.open`. It is `Marks.decrypt` now —
    // an asset-catalog PDF, which `Symbol` has no field for and deliberately
    // did not grow one for. `Symbol.name` feeds
    // `SymbolsTests.test_every_declared_symbol_resolves` through
    // `NSImage(systemSymbolName:)`, and a catalog asset is not resolvable by
    // that route: putting "decryptedBinary" in this enum would have failed that
    // test with a message about a missing SF Symbol, which is the wrong
    // diagnosis of a correct asset. `Mark` is a separate type with a separate
    // resolution test through `NSImage(named:)` for exactly that reason, and
    // four marks is the whole budget — a source discriminator on `Symbol` would
    // be machinery built for a second case that is not coming.

    /// Retry a dump with the app extensions cut out of it.
    static let dropExtensions = Symbol(name: "scissors", weight: .medium)

    /// Ask the repos and Canister whether anything is newer.
    static let checkUpdates = Symbol(name: "arrow.up.circle", weight: .medium)

    /// Show a path in the Finder.
    static let revealInFinder = Symbol(name: "arrow.up.right.square", weight: .medium)

    /// The per-row menu in the builds table.
    static let rowMenu = Symbol(name: "ellipsis.circle", weight: .regular)

    /// Open the build-options popover. `.semibold` at 10pt: it is the smallest
    /// glyph in the app-pane header cluster and sits beside 13pt button labels,
    /// so it needs the weight to hold the same line as they do.
    static let buildOptions = Symbol(name: "chevron.down", weight: .semibold)

    /// Dismiss a banner or a notice strip.
    ///
    /// `.bold` at every size it is used at (10, 11, ambient). An × is two thin
    /// diagonals with nothing else to it, so it is the glyph that loses most
    /// from being drawn small — and both call sites are deliberately quiet,
    /// one at 80% white and one in `Palette.textDim`.
    static let dismiss = Symbol(name: "xmark", weight: .bold)

    /// Clear one active build option — the × inside an amber pill.
    ///
    /// Same glyph as `dismiss`, a different thing: this one removes a setting
    /// from the next build, where `dismiss` closes a message. It is also drawn
    /// at 8pt, the smallest icon in the app, which is why it is not simply
    /// `dismiss` reused: any future change to how a message is dismissed must
    /// not silently follow the × into a build-option pill.
    static let clearOption = Symbol(name: "xmark", weight: .bold)

    /// The glyph inside a filter field.
    static let search = Symbol(name: "magnifyingglass", weight: .medium)

    /// The drop target for a source .ipa.
    static let dropIPA = Symbol(name: "tray.and.arrow.down.fill", weight: .regular)

    /// The two states of the checkbox in the attach-tweaks sheet.
    static let checkboxOn = Symbol(name: "checkmark.square.fill", weight: .regular)
    static let checkboxOff = Symbol(name: "square", weight: .regular)

    // MARK: - States

    /// It worked: a readiness check that passed, a tweak update that landed.
    ///
    /// This one and the two below are the three the decrypt card draws side by
    /// side at 11pt, and the row where `Symbol.rendering`'s knockout argument
    /// was actually checked against a running app.
    static let ok = Symbol(name: "checkmark.circle.fill", weight: .regular)

    /// A readiness check that failed *and* blocks the dump.
    static let blocked = Symbol(name: "xmark.circle.fill", weight: .regular)

    /// A readiness check that failed and does not block — a route that is
    /// unavailable, not a feature that is broken. See `DeviceDecryptSection`,
    /// where drawing this identically to `blocked` is what made people think
    /// the decrypt card was dead.
    static let advisory = Symbol(name: "info.circle.fill", weight: .regular)

    /// The app's one general-purpose failure surface.
    ///
    /// Monochrome and `.semibold`: it is white on `Palette.dangerSurface`, a
    /// pairing chosen for a 4.83:1 contrast (see `Palette.dangerSurface`), and
    /// hierarchical would draw the exclamation at a fraction of that white —
    /// spending the contrast the banner was rebuilt to get.
    static let failure = Symbol(name: "exclamationmark.triangle.fill", weight: .semibold)

    /// Something wants doing before the next build. The hollow triangle, not
    /// the filled one: nothing has failed in these states, and the build-target
    /// bar draws it in `Palette.warn` beside a sentence that says what to do.
    static let attention = Symbol(name: "exclamationmark.triangle", weight: .medium)

    /// This app on the phone is FairPlay-encrypted — a decrypt candidate.
    /// `.medium` because it is drawn at 10pt in `Palette.textDim`, which is the
    /// app's smallest size at its quietest color.
    static let encrypted = Symbol(name: "lock.fill", weight: .medium)

    /// A newer build of this tweak exists, or a newer Graft (`UpdateNotice`).
    static let updateAvailable = Symbol(name: "arrow.up.circle.fill", weight: .regular)

    /// The backend chose a different route than asked — one line of "here is
    /// what actually happened" under a dump.
    static let note = Symbol(name: "arrow.triangle.branch", weight: .medium)

    // MARK: - The build target

    /// The six glyphs `BuildTargetBar` chooses between, plus `attention` above,
    /// which it shares with nothing else. (Eight `BuildTarget` cases map onto
    /// seven glyphs: the two unreadable cases share `deviceLocked`, and
    /// `.notDelivering` picks between `attention` and `notDelivering` depending
    /// on whether the build will survive.)
    ///
    /// Separate accessors rather than one `device` with modifiers because they
    /// are separate answers to "where is this build going", and two of them
    /// (`scanning`, `deviceLocked`) reuse a glyph the app already means
    /// something else by.

    /// A phone, named and reachable: the resolved target, and the Check-device
    /// button in the app pane.
    static let device = Symbol(name: "iphone", weight: .medium)

    /// Phones are connected; none is marked default.
    static let noDefaultDevice = Symbol(name: "questionmark.circle", weight: .medium)

    /// Nothing is plugged in.
    static let noDevice = Symbol(name: "iphone.slash", weight: .medium)

    /// A phone answered usbmux and refused the lockdown query — almost always
    /// locked. Naming the likely cause in the glyph beats a third warning
    /// triangle; see `BuildTargetBar.symbol`.
    static let deviceLocked = Symbol(name: "lock", weight: .medium)

    /// The first listing has not come back yet.
    static let scanning = Symbol(name: "magnifyingglass", weight: .medium)

    /// `--skip-install`: the build will happen and nothing will go out.
    static let notDelivering = Symbol(name: "nosign", weight: .medium)

    /// Every symbol above.
    ///
    /// Hand-written for the same reason `CLIContract.flagsUsed` is: Swift
    /// cannot enumerate a caseless enum's static members, and the tests that
    /// resolve these names need something to iterate. Kept honest from the
    /// other side by `test_every_declared_symbol_is_listed_in_all`, which reads
    /// this file's own source — an accessor left out of this array is a name
    /// nothing ever resolves, which is exactly the empty box the resolution
    /// test exists to catch.
    static let all: [Symbol] = [
        tweak, canisterSection, reposSection, cacheSection, buildsSection,
        settingsSection,
        add, refresh, delete, installToMac, detach, dropExtensions, checkUpdates,
        revealInFinder, rowMenu, buildOptions, dismiss, clearOption, search,
        dropIPA, checkboxOn, checkboxOff,
        ok, blocked, advisory, failure, attention, encrypted, updateAvailable,
        note,
        device, noDefaultDevice, noDevice, deviceLocked, scanning, notDelivering,
    ]
}
