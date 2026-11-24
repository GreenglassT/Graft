import SwiftUI

enum Palette {
    // Surfaces
    static let bg = Color(hex: 0x0F172A)        // window background
    static let surface = Color(hex: 0x1E293B)   // cards, meta cells, button body
    static let surface2 = Color(hex: 0x020617)  // log panel, deeper recess
    static let line = Color(hex: 0x334155)      // hairline borders

    // Text
    static let text = Color(hex: 0xF8FAFC)
    static let textMuted = Color(hex: 0x94A3B8)
    /// The quietest text in the app — bundle ids, hints, empty states, path
    /// rows — and almost always at `AppFont.monoSmall`, the smallest type here.
    ///
    /// Lightened from #64748B, which was the one color in this palette that
    /// failed WCAG AA everywhere it was used: 3.75:1 on `bg` and 3.49:1 on a
    /// `cardStyle` surface against the 4.5:1 a body text needs. Smallest type
    /// at the lowest contrast is the worst pairing available, and it carried
    /// most of the app's secondary information. This value clears 4.5:1 on
    /// every surface it lands on (bg 5.49, card 5.11, MetaCell 4.81, surface
    /// 4.50) with the hue and saturation unchanged.
    static let textDim = Color(hex: 0x8190A5)

    // Accent + semantic
    static let accent = Color(hex: 0x818CF8)    // app icons, drop-zone hover, log arrows
    static let success = Color(hex: 0x22C55E)   // build complete, Build label color
    static let warn = Color(hex: 0xFBBF24)      // profile expiring, dump in progress
    static let danger = Color(hex: 0xF87171)    // failed build, missing dep

    /// The app's tint: what fills a selected row and a sheet's default button.
    ///
    /// A darker indigo than `accent`, and not interchangeable with it. `accent`
    /// is a *foreground* color — icons, a hover border, arrows in the log —
    /// and using it as the tint put white on #818CF8 at 2.98:1 on every
    /// prominent button and `Palette.text` at 2.85:1 on the selected row. This
    /// carries them at 6.29:1 and 6.01:1.
    ///
    /// It reads as selection against `bg` at 2.84:1 rather than the 3:1 a
    /// meaningful boundary would want. That is the deliberate end of a genuine
    /// trade: a fill light enough to clear 3:1 needs a luminance above 0.126,
    /// and white text on it needs one below 0.183 — a window narrow enough that
    /// nothing in it also carries `Palette.text` at 4.5:1. Legible text on the
    /// row the user has selected wins, and the hue separates it from the navy
    /// background far more strongly than the luminance ratio suggests.
    static let accentSelect = Color(hex: 0x4F46E5)

    /// Secondary text *on* `accentSelect` — the bundle id of the selected app,
    /// the count on the selected sidebar row.
    ///
    /// Necessary because no selection fill can carry `textDim`: 4.5:1 against
    /// it needs a fill below 0.022 luminance, which is darker than `bg` itself,
    /// so the row's quiet text has to change color when selected rather than
    /// the fill bending to accommodate it. 5.10:1 on `accentSelect`.
    static let onAccentMuted = Color(hex: 0xE0E7FF)

    /// The fill behind white text in `ErrorBanner`, and the only reason it is
    /// not `danger`.
    ///
    /// `danger` is a *foreground* color — dots, labels, failure text — and at
    /// 6.45:1 on `bg` it is a good one. Reused as a banner fill under white
    /// text it gives 2.77:1, which fails AA and even AA-large, on the app's
    /// single "that didn't work" surface at 11pt. Darkening it here rather than
    /// darkening `danger` keeps every existing foreground use at its current
    /// contrast: white on this is 4.83:1, and the block itself still reads as a
    /// distinct surface against `bg` at 3.70:1.
    static let dangerSurface = Color(hex: 0xDC2626)
}

/// The spacing, radius and sizing scale.
///
/// These were literals at every call site, and they had drifted into a set
/// rather than a scale: pane gutters of 12, 16 and 20 across six sections;
/// card padding of 8, 10, 12 and 14; corner radii of 6, 7 and 8. Nothing
/// downstream could tell which differences were deliberate, so each new view
/// picked whichever neighbour it was written next to.
enum Metrics {
    /// Gutter between a pane's content and the pane's edges.
    static let paneGutter: CGFloat = 20
    /// Gutter for a narrow master column — the repo list, and anything else
    /// living in a few hundred points. Deliberately not `paneGutter`: 20 a side
    /// out of a 280pt column is a seventh of it. This is the one place the two
    /// differ, and it differs for a reason rather than by drift.
    static let columnGutter: CGFloat = 12
    /// Padding inside a card.
    static let cardPadding: CGFloat = 12
    /// Gutter for a popover or any other small floating container. Between the
    /// two above because it is neither: too cramped for `paneGutter` at 300pt
    /// wide, too airy a context for a master column's `columnGutter`.
    static let popoverGutter: CGFloat = 16

    /// Gap between two sections of a scrolling pane.
    static let sectionGap: CGFloat = 16
    /// Gap between two *named top-level groups* — the Preflight and Paths
    /// blocks in Settings. Larger than `sectionGap` because each group already
    /// spends ~14pt between its own title and its first row, and at 16 the gap
    /// separating the groups was no bigger than the gap inside one: the second
    /// heading read as another row of the first list.
    static let groupGap: CGFloat = 24
    /// Gap between a `SectionTitle` and the content under it.
    static let titleGap: CGFloat = 6
    /// Gap between sibling rows in a list of cards.
    static let rowGap: CGFloat = 8
    /// Gap between two adjacent controls — buttons in a toolbar cluster, cells
    /// in a meta strip.
    ///
    /// Equal to `rowGap` today, and separate from it on purpose. These were one
    /// token briefly, which meant a single edit intended for the spacing of the
    /// Devices list would also have moved the gap between the header's buttons
    /// and the padding inside every card — the coupling this whole enum exists
    /// to prevent, wearing a name that hid it.
    static let controlGap: CGFloat = 8
    /// Padding inside a small component that is not itself a card — a meta
    /// cell, a hint box.
    static let innerPadding: CGFloat = 10

    /// The smallest the window may be resized to.
    ///
    /// Named constants rather than literals on the `.frame`, so the layout
    /// tests can assert against the size the app actually ships instead of
    /// re-typing it. `WindowLayoutTests` measured "below the minimum" against a
    /// hardcoded 700 while this said 900; lowering one and not the other would
    /// have left the real minimum untested and the test's own docstring false.
    static let windowMinWidth: CGFloat = 900
    static let windowMinHeight: CGFloat = 560

    /// Corner radius for a card, a control, or anything sitting directly on the
    /// window background.
    static let radius: CGFloat = 8
    /// Corner radius for something nested *inside* a card. Smaller on purpose:
    /// concentric rounded rectangles only look concentric when the inner radius
    /// shrinks by roughly the padding between them, and reusing 8 inside an 8
    /// made the inner corners read rounder than the outer ones.
    static let radiusInner: CGFloat = 6
}

private extension Color {
    init(hex: UInt32) {
        let r = Double((hex >> 16) & 0xFF) / 255.0
        let g = Double((hex >> 8) & 0xFF) / 255.0
        let b = Double(hex & 0xFF) / 255.0
        self.init(red: r, green: g, blue: b)
    }
}
