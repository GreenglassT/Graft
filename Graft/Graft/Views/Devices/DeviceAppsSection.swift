import SwiftUI

/// Your configured apps, each with its delivery state **on this phone**.
///
/// This is the section the whole per-device restructure exists for. Everywhere
/// else in the app, "is this app current?" is asked without naming a device —
/// `ConfigStore.deliveryStates` comes from `--list`, which answers *anywhere* —
/// so a bench with two phones had one badge for both of them, and it described
/// whichever one happened to be pushed to last.
///
/// **So a row here can honestly disagree with the sidebar, and that is not a
/// bug.** An app built and pushed to phone A and never to phone B reads "Never
/// delivered to a device." on B's pane while B's owner can see a green,
/// delivered app in the sidebar two columns to the left. Both statements are
/// true of different questions. The temptation to reconcile them — to fall back
/// on the unscoped state when the scoped one says `never`, or to hide the row —
/// puts back exactly the thing this section was built to surface. See
/// `DeviceDeliveryStore`, which holds the scoped answer precisely so that the
/// unscoped one keeps its meaning for the surfaces that want it.
///
/// The one wording seam, recorded because it is the thing a reader notices
/// first: the CLI's `never` sentence is "Never delivered to a device", not "to
/// this device". That string serves both the scoped and unscoped call and is
/// not ours to re-author per caller — every word about a delivery on this pane
/// comes from the CLI, the same rule `AppDetailView` follows. What scopes it is
/// the caption under the title, said once for the whole section rather than
/// re-worded into every row.
///
/// **Every long string here is `lineLimit`-ed, and nothing here is under
/// `fixedSize`.** A CLI summary is prose we don't choose the length of ("An
/// archived build was re-installed; what went into it was never recorded."),
/// and this pane's predecessor resolved one such sentence to ~78 stacked lines
/// and demanded 1391pt inside a 700pt window, rendering the whole app blank.
///
/// This section sits inside the pane's `ScrollView`, which changes *which* half
/// of that failure can still reach the window — and the answer is not the
/// obvious one. Height is absorbed down here, so a row cannot blow the pane up
/// vertically; **width still travels**, straight through the enclosing
/// `NavigationSplitView`. Measured, with one row carrying a 210-character
/// unbreakable run under a plain `.fixedSize()`: 1796pt demanded in a 900pt
/// window, 896pt of the pane laid out outside it. `DeviceDetailLayoutTests`
/// records the experiment and measures the property with this section
/// populated; the `lineLimit`s here, and the absence of any `fixedSize`, are
/// what uphold it.
struct DeviceAppsSection: View {
    // Deliberately no `DeviceRecord`. The phone is named by the header above
    // and is already baked into `readout`, which the caller looked up by udid;
    // holding the record here would only offer this section the one string on
    // the pane that is unbounded user input — the device name — with nothing to
    // do with it but put it on screen a second time.

    /// Every configured app, in `ConfigStore`'s order (sorted by name).
    let apps: [AppRecord]
    /// This phone's delivery states, or why there are none yet.
    let readout: DeviceDeliveryStore.Readout
    /// The icon cache. Held as the store rather than resolved by the caller so
    /// that reading `image(for:)` inside each row registers the dependency on
    /// `ArtworkStore.generation` that makes icons appear when a load finishes —
    /// see that method's doc comment for why the `NSCache` alone cannot.
    let artwork: ArtworkStore

    var body: some View {
        VStack(alignment: .leading, spacing: Metrics.titleGap) {
            SectionTitle(text: "Your apps on this phone")
            caption
            if case let .failed(message) = readout {
                // The failure belongs to the phone, not to any one app, so it
                // is stated once here rather than repeated down every row.
                // `ErrorBanner` bounds both the message and any unbreakable
                // token in it (see its `tokenLimit`), which is what lets a raw
                // CLI error be shown in a pane with this one's history.
                ErrorBanner(
                    message: message,
                    title: "Couldn't read this phone's delivery log.",
                    fit: .inset
                )
            }
            if apps.isEmpty {
                Text("No apps are configured yet.")
                    .font(AppFont.body)
                    .foregroundStyle(Palette.textMuted)
                    .lineLimit(1)
            } else {
                VStack(alignment: .leading, spacing: Metrics.rowGap) {
                    ForEach(apps) { app in
                        DeviceAppDeliveryRow(
                            app: app,
                            state: Self.state(of: app.name, in: readout),
                            status: Self.statusLine(of: app.name, in: readout),
                            artwork: artwork.image(for: app.bundleId)
                        )
                    }
                }
            }
        }
    }

    /// What "on this phone" means, said once.
    ///
    /// Deliberately a constant string with no device name interpolated into it.
    /// The name is whatever the user typed into Settings on the phone — the one
    /// unbounded input this pane takes — and it is already on screen in the
    /// header, bounded there. The fuller explanation lives in the tooltip
    /// rather than on the pane, because the sentence that actually explains the
    /// disagreement is long and this is not the pane to put a long sentence on.
    private var caption: some View {
        Text("Delivery state below is for this phone alone.")
            .font(AppFont.monoSmall)
            .foregroundStyle(Palette.textDim)
            .lineLimit(1)
            .help(
                "An app delivered to another phone and never to this one reads as "
                + "never delivered here. The app list's own badge means \"delivered "
                + "anywhere\", which is why the two can disagree."
            )
    }

    // MARK: - What each row says

    /// This phone's record for one app, or nil when there isn't one — because
    /// the load hasn't landed, because it failed, or because the CLI described
    /// every app but this one.
    ///
    /// The dot beside the row is driven from this, through
    /// `DeliveryKind.needsAttention`, so all three of those cases correctly
    /// produce no dot: a mark the user is meant to act on must not appear for a
    /// state nobody has read yet.
    static func state(
        of appName: String, in readout: DeviceDeliveryStore.Readout
    ) -> DeliveryStateRecord? {
        guard case let .ready(states) = readout else { return nil }
        return states[appName]
    }

    /// The row's one line about delivery.
    ///
    /// A static function, and the second-longest thing on the row, so that the
    /// copy is somewhere a test can read it — the same reason
    /// `DeliveryKind.stateLabel` and `DeviceStatStrip`'s value functions sit on
    /// a type rather than inline in a body.
    ///
    /// Only one of these four sentences is ours. The answered case renders the
    /// CLI's own `summary`, exactly as `AppDetailView` does; the other three
    /// are the states the CLI never gets to describe, and each says which one
    /// it is rather than collapsing to a shrug. That distinction is the point:
    /// "we haven't asked yet" must never render as "never delivered", which is
    /// the one wrong answer this section can give.
    static func statusLine(
        of appName: String, in readout: DeviceDeliveryStore.Readout
    ) -> String {
        switch readout {
        case .pending:
            return "Reading this phone's delivery log…"
        case let .ready(states):
            guard let state = states[appName] else {
                // Same meaning, and now literally the same string, as
                // `AppDetailView`'s fall-through: the CLI described the other
                // apps and said nothing about this one. The two panes each had
                // their own copy of the sentence, each commented as
                // deliberately matching the other.
                return DeliveryStateRecord.noneReported
            }
            return state.summary
        case .failed:
            // The reason is already on screen once, above every row. Repeating
            // it here would be the same sentence N times, and the em dash is
            // this app's established mark for "no reading" — the stat strip
            // above, the builds table, the app pane's meta strip all use it.
            return "—"
        }
    }
}

/// One configured app, and where its delivery stands on this phone.
///
/// Close kin to `AppSidebarRow` on purpose — icon, name, a quiet second line, a
/// warn dot — because it is the same app in a different place, and a row that
/// looked like a different kind of object would read as a different list. The
/// differences are the two that matter here: the second line is this phone's
/// delivery state rather than the bundle id, and there is no delivery-mode chip,
/// which describes the app's configuration and not this phone.
private struct DeviceAppDeliveryRow: View {
    let app: AppRecord
    /// This phone's record, or nil — see `DeviceAppsSection.state(of:in:)`.
    /// Drives the dot only; the words come from `status`.
    let state: DeliveryStateRecord?
    /// The line under the name, already decided by
    /// `DeviceAppsSection.statusLine(of:in:)`.
    let status: String
    /// nil draws the lettered tile, which is a fallback and not an error — see
    /// `AppIcon`.
    let artwork: NSImage?

    var body: some View {
        HStack(spacing: Metrics.controlGap) {
            // 22pt: this is a scannable list inside a pane that also carries a
            // header, a stat strip and (from Task 11) a decrypt card, so the
            // row is smaller than the sidebar's 28 rather than larger.
            // `app.name` and not the display name, matching `AppSidebarRow`:
            // the tile's letters come from the config key, so the same app
            // draws the same tile in both lists.
            AppIcon(name: app.name, size: 22, image: artwork)
            VStack(alignment: .leading, spacing: 2) {
                Text(app.displayName ?? app.name)
                    .font(AppFont.bodyMedium)
                    .foregroundStyle(Palette.text)
                    .lineLimit(1)
                    .truncationMode(.tail)
                // The CLI's sentence, and the one string on this row whose
                // length is not ours. One line, tail-truncated, with the full
                // text — summary *and* the reasons behind it — on hover: the
                // same bargain `AppSidebarRow` strikes for the bundle id, and
                // what keeps a summary the CLI made arbitrarily long from
                // asking this pane for arbitrary width. `state?.detail` is
                // `DeliveryStateRecord`'s own join of the two, so the tooltip
                // cannot punctuate them differently from the one in the
                // sidebar.
                Text(status)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .help(state?.detail ?? status)
            }
            Spacer(minLength: Metrics.controlGap)
            // `needsAttention`, not a comparison against the kinds — the same
            // call `AppSidebarRow` makes, and for the reason that property's
            // doc gives: which states earn a mark is decided exactly once, on
            // the model. Writing the predicate out here would make this the
            // third place that decides it, and the one most likely to drift,
            // since this pane asks a narrower question than the other two.
            if let state, state.kind.needsAttention {
                Circle()
                    .fill(Palette.warn)
                    .frame(width: 6, height: 6)
                    .help(state.detail)
            }
        }
    }
}
