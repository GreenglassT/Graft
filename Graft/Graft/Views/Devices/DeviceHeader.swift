import SwiftUI

/// The top of a device's own pane: which phone this is, at a glance.
///
/// The silhouette is the reason this reads as a *place* rather than a row —
/// two phones on a bench are told apart by shape before either name is read.
/// It is drawn from the product type, the only hardware signal available; see
/// `DeviceSilhouette` for why it is deliberately not tinted.
///
/// **Every string here is `lineLimit`-ed, and that is not tidiness.** A device
/// name is whatever the user typed into Settings on the phone, so it is
/// unbounded input rendered in a pane with a recorded history of blanking
/// itself over exactly that: an unbounded `Text` with
/// `.fixedSize(horizontal: false, vertical: true)` in `DevicesView` resolved
/// ~110 characters to ~78 stacked lines, and the pane asked for 1391pt inside
/// a 700pt window with the excess clipped off the *top*. `lineLimit` is what
/// bounds it, because it caps the ideal height whatever width is proposed.
/// `DeviceDetailLayoutTests` measures the property; the limits here are what
/// uphold it, and at any real pane width neither is reached.
struct DeviceHeader: View {
    let device: DeviceRecord

    /// Whether this is the phone builds are delivered to when none is named.
    ///
    /// Passed in rather than read here, still — this view has no business
    /// owning a store. But it is now `device.isDefault`, not a separate
    /// lookup: `--devices` echoes `default_device_udid` back onto each
    /// device's own event, so the fact arrives with the phone it describes.
    ///
    /// That used to be a second channel. `default_device_udid` reached the
    /// GUI only through `SettingsStore`, which loaded lazily — nothing asked
    /// for it until someone visited the Settings pane — so `ContentView` had
    /// to force a launch-time `--settings` read just to keep a fresh window's
    /// device pane from drawing no chip on a phone that *is* the default.
    /// That was a wrong answer standing in for a missing one, worked around
    /// with an extra subprocess at every launch. Carrying the bit on the
    /// device event itself closes the gap instead of racing it: there is no
    /// window where this pane can be open and the answer not yet be here.
    var isDefaultTarget: Bool = false

    /// Mirrors `DevicesStore.isLoading`, so the button can say it is working
    /// rather than just going dead.
    var isRefreshing: Bool = false
    var onRefresh: () -> Void = {}

    /// The size `DeviceSilhouette`'s own doc comment names for a detail-pane
    /// header, against 14×21 for a sidebar row. Every dimension inside it is a
    /// fraction of this, so the two call sites stay the same drawing.
    private static let silhouetteSize = CGSize(width: 34, height: 52)

    var body: some View {
        HStack(alignment: .top, spacing: Metrics.sectionGap) {
            DeviceSilhouette(productType: device.model, size: Self.silhouetteSize)
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: Metrics.controlGap) {
                    Text(device.name)
                        .tightTitle()
                        .lineLimit(1)
                        // The name is user-supplied and can be long enough to
                        // be truncated here; hovering gets all of it, the same
                        // bargain `MetaCell` strikes for a truncated UDID.
                        .help(device.name)
                    if isDefaultTarget {
                        // `fixedSize`, so a long name truncates rather than
                        // squeezing the chip: both are `Text` in one HStack,
                        // and without this SwiftUI is free to compress the
                        // two-word label into "Default t…". Safe on this one
                        // because the string is a constant — it is unbounded
                        // *user* text under `fixedSize` that blanks this pane.
                        defaultTargetChip.fixedSize()
                    }
                }
                Text(subtitle)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
                    .lineLimit(1)
            }
            Spacer(minLength: Metrics.controlGap)
            // Same reason as the chip: the button shares this row with a name
            // of unknown length, and a Refresh that reads "Refr…" is worse
            // than a name that truncates.
            refreshButton.fixedSize()
        }
    }

    /// `iPhone14,2 · iOS 16.3.1 · USB` — the product type rather than a
    /// marketing name, because that is what the CLI reports and what every
    /// other surface in this app (the silhouette's classification, the device
    /// card, a bug report) is keyed by. Translating it to "iPhone 13 Pro" here
    /// would mean maintaining a table that goes stale every September and
    /// would be silently wrong for anything newer than the last update.
    private var subtitle: String {
        "\(device.model) · iOS \(device.iosVersion) · \(device.connectionType.rawValue)"
    }

    /// Filled with the app's tint rather than drawn like the connection pill
    /// beside it, because this one is a *claim about configuration* — the one
    /// phone a build lands on when nothing else is named — and it should be
    /// findable across a bench of several panes that otherwise look alike.
    ///
    /// Not `labelCaps()`, which is otherwise exactly this typography:
    /// that modifier also fixes the color at `Palette.textMuted`, and muted on
    /// `accentSelect` is 2.5:1. `onAccentMuted` exists for precisely this — see
    /// its doc comment — and carries 5.10:1 on this fill.
    private var defaultTargetChip: some View {
        Text("Default target")
            .font(AppFont.label)
            .tracking(0.6)
            .textCase(.uppercase)
            .foregroundStyle(Palette.onAccentMuted)
            .padding(.horizontal, 8).padding(.vertical, 3)
            .background(Palette.accentSelect)
            .clipShape(Capsule())
            .help("Builds are delivered to this device unless another is chosen.")
    }

    /// Re-reads the device list *and* this phone's vitals — see
    /// `DeviceDetailView.refresh`. Kept in the shape the deleted Devices pane's
    /// toolbar Refresh had, deliberately: it is the same gesture in the pane
    /// that replaced it, and a control that changed appearance across the move
    /// would read as a different action.
    private var refreshButton: some View {
        Button(action: onRefresh) {
            if isRefreshing {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text("Refreshing…")
                }
            } else {
                Symbols.refresh.label("Refresh")
            }
        }
        .disabled(isRefreshing)
    }
}
