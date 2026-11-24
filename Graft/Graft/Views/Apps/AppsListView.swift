import SwiftUI

/// One app, as it appears in the sidebar.
///
/// This used to be a row in a 320pt column of its own. It now shares the one
/// navigation column with the sections, at 260pt — so everything on it either
/// shrank or learned to give way. The bundle id is the only thing here that
/// gives: the icon, the name, the warning dot and the delivery chip all keep
/// their intrinsic size, because each of them is unreadable at less than it
/// and the bundle id is the one element that shortens gracefully and has a
/// tooltip carrying the rest.
struct AppSidebarRow: View {
    let app: AppRecord
    /// What the CLI last said about this app's delivery, if anything. Absent
    /// means it said nothing — see `ConfigStore.deliveryStates`.
    let state: DeliveryStateRecord?
    /// Drives the quiet text's color, which has to change on the selection
    /// fill rather than stay unreadable on it.
    let isSelected: Bool
    /// This app's real icon, already resolved by the caller. nil draws the
    /// lettered tile, which is a fallback and not an error — see `AppIcon`.
    let artwork: NSImage?

    var body: some View {
        HStack(spacing: 8) {
            // 28, down from 34. The icon is the row's fixed cost and the column
            // lost 60pt; taking six of them back here is cheaper than taking
            // them off the bundle id, which is the only place the full one is
            // shown.
            AppIcon(name: app.name, size: 28, image: artwork)
            VStack(alignment: .leading, spacing: 2) {
                // One line. The badges beside this are `.fixedSize()` so they
                // can't wrap, which means the row's shortfall lands here
                // instead — and unlimited, a long name wrapped to two or three
                // lines, making badged rows taller than unbadged ones.
                Text(app.displayName ?? app.name)
                    .font(AppFont.bodyMedium)
                    .foregroundStyle(Palette.text)
                    .lineLimit(1)
                    .truncationMode(.tail)
                // `textDim` on the selection fill is 1.09:1 — no fill light
                // enough to be visible can carry it (see
                // `Palette.onAccentMuted`), so the quiet text changes color
                // when the row is selected rather than staying unreadable on it.
                Text(app.bundleId)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(isSelected ? Palette.onAccentMuted : Palette.textDim)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    // The column is draggable, but its default 260pt truncates
                    // every bundle id longer than about twenty characters, and
                    // this is the only place the full one is shown.
                    .help(app.bundleId)
            }
            Spacer(minLength: 4)
            // A dot, not the "not delivered" text badge the wider column could
            // afford. At 260pt that label took a third of the row and pushed
            // the bundle id down to nothing — and it was never the words that
            // were doing the work, it was that the row was marked at all. The
            // tooltip carries the CLI's full sentence, and the detail pane
            // spells it out in full the moment the row is clicked.
            // `needsAttention`, and not the kinds written out. Which states
            // earn a mark is decided exactly once, on the model — it used to
            // be decided by a nullable label instead, which is how `current`
            // came within a word of growing a dot on every app in the list.
            // This is one of the three calls that keeps that true.
            if let state, state.kind.needsAttention {
                Circle()
                    .fill(Palette.warn)
                    .frame(width: 6, height: 6)
                    .help(state.detail)
            }
            // Pinned, not deprioritised. Given a negative layout priority this
            // *compressed* rather than yielding — "LC" rendered as "L" on every
            // row, a two-letter label truncated to one, which reads as a
            // different word rather than as a shortened one. A chip is a label:
            // it fits or it should not be here. So it takes its intrinsic width
            // and the shortfall goes to the bundle id above, which truncates
            // from the middle and keeps its tooltip.
            deliveryChip
                .fixedSize()
        }
        .padding(.vertical, 2)
    }

    /// Which of the two delivery modes this app uses.
    ///
    /// **LiveContainer draws its mark; a sideload keeps its word**, and the
    /// asymmetry is the honest state of the icon set rather than an oversight:
    /// four marks were drawn, none of them a sideload, and inventing a fifth to
    /// make the pair match is how a vocabulary grows glyphs nobody chose.
    ///
    /// The mark goes *in place of* "LC" rather than in front of it because this
    /// row has no width to give. The 260pt column already traded a "not
    /// delivered" text badge for a 6pt dot (see the comment on that dot above),
    /// and "LC" existed only because "LiveContainer" never fit. Swapping the two
    /// letters for the mark is width-neutral — 11pt of `AppFont.label` measures
    /// ~14pt for "LC" against an 11.7pt frame for the mark — and `TagChip` keeps
    /// the hidden label in the layout so the capsule does not change size at all.
    /// The full word moves to the tooltip, where it was not before.
    private var deliveryChip: some View {
        Group {
            if app.delivery == .livecontainer {
                TagChip(label: "LC", mark: Marks.liveContainer)
                    .help("Delivered through LiveContainer")
            } else {
                TagChip(label: "Sideload")
            }
        }
        .lineLimit(1)
    }
}
