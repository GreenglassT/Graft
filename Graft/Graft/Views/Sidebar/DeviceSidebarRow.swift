import SwiftUI

/// One connected phone, as it appears in the sidebar.
///
/// Close kin to `AppSidebarRow` on purpose — a small graphic, a name, a quiet
/// second line, a dot — because both are destinations in the same column and a
/// row that looked like a different kind of object would read as a different
/// list. The graphic is `DeviceSilhouette` rather than an icon: a bench with two
/// phones is told apart by shape before either name is read, and the same
/// drawing appears at 34×52 in `DeviceHeader` so the row and the pane it opens
/// are visibly the same phone.
///
/// **Every string here is bounded to one line, and that is not tidiness.** A
/// device name is whatever the user typed into Settings on the phone, so it is
/// unbounded input — rendered here in a column whose *ideal* width is 260pt.
/// `DeviceDetailLayoutTests` records what an unbounded string did to the pane
/// (1391pt demanded inside a 700pt window, the header clipped off the top), and
/// `SidebarDeviceLayoutTests` measures the same property for this column. The
/// full name is on the tooltip, the same bargain `AppSidebarRow` strikes for a
/// truncated bundle id.
struct DeviceSidebarRow: View {
    let device: DeviceRecord
    /// Drives the quiet text's colour, which has to change on the selection
    /// fill rather than stay unreadable on it — `textDim` on `accentSelect` is
    /// 1.09:1. Same handling, and the same reason, as `AppSidebarRow`.
    let isSelected: Bool

    /// The sidebar size `DeviceSilhouette`'s own doc comment names, against
    /// 34×52 for the detail-pane header. Every dimension inside that view is a
    /// fraction of this, so the two call sites are the same drawing.
    private static let silhouetteSize = CGSize(width: 14, height: 21)

    var body: some View {
        HStack(spacing: 8) {
            DeviceSilhouette(productType: device.model, size: Self.silhouetteSize)
            VStack(alignment: .leading, spacing: 2) {
                Text(device.name)
                    .font(AppFont.bodyMedium)
                    .foregroundStyle(Palette.text)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .help(device.name)
                Text(Self.subtitle(for: device))
                    .font(AppFont.monoSmall)
                    .foregroundStyle(isSelected ? Palette.onAccentMuted : Palette.textDim)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    // What "LC" means, since the row has no space to say it.
                    .help(device.livecontainerBundleId.map {
                        "LiveContainer is installed on this phone: \($0)"
                    } ?? "LiveContainer is not installed on this phone, so a "
                        + "LiveContainer delivery cannot land here.")
            }
            Spacer(minLength: 4)
            liveDot
        }
        .padding(.vertical, 2)
    }

    /// `iOS 26.1 · LC` — the version, and whether the thing that receives a
    /// delivery is on the phone at all.
    ///
    /// A static function so the copy is somewhere a test can read it, the same
    /// reason `DeviceStatStrip`'s cell values and `DeliveryKind.stateLabel` sit
    /// on a type rather than inline in a body.
    ///
    /// LiveContainer is the second fact rather than the model identifier or the
    /// connection type, both of which the pane's own header shows: this row is
    /// where you decide *which phone to push a build to*, and a phone without
    /// LiveContainer cannot take one. `nil` on the record means the scan looked
    /// and found none — not "unknown" — so it is stated as an absence rather
    /// than a dash.
    ///
    /// **"LC", not "LiveContainer", and that is measured rather than a
    /// preference.** Spelled out, this line truncates in the shipped column:
    /// "iOS 26.1 · LiveCont…" in the running app at the default 260pt, with the
    /// clipped half being the whole point of the line. "LC" is also already this
    /// sidebar's word for it — `AppSidebarRow`'s delivery chip reads LC or
    /// Sideload — so the abbreviation is being reused rather than invented, and
    /// the tooltip on this line spells it out with the bundle id.
    static func subtitle(for device: DeviceRecord) -> String {
        let liveContainer = device.livecontainerBundleId == nil ? "no LC" : "LC"
        return "iOS \(device.iosVersion) · \(liveContainer)"
    }

    /// The row is live: this phone answered the last poll, seconds ago.
    ///
    /// Green rather than the amber `AppSidebarRow` uses, because it means the
    /// opposite thing — that one marks a row needing attention, this one marks
    /// a row that is *there*. Which is worth saying at all only because these
    /// rows come and go on their own: a list that changes under you needs to
    /// look like it is describing hardware rather than remembering it.
    private var liveDot: some View {
        Circle()
            .fill(Palette.success)
            .frame(width: 6, height: 6)
            .help(device.connectionType == .usb
                  ? "Connected over USB"
                  : "Connected over the network")
    }
}

/// A phone that answered usbmux and then would not describe itself.
///
/// It is plugged in, paired and on the bus — `--devices` exits 0 and lists the
/// other phones normally — but the lockdown query that reads its name, model
/// and iOS version failed, usually because the phone is locked or has not been
/// trusted. So there is no `DeviceRecord`, no pane to open, and nothing to
/// select: this row is untagged deliberately, and the UDID is all there is to
/// identify it by, because the query that would have produced a name is the one
/// that failed.
///
/// **It has to be said somewhere, or the sidebar lies.** With one phone attached
/// and failing, a Devices section that only listed `devices` would render "No
/// devices connected" and send the user to check a cable that is already fine.
///
/// **Bounded harder than the card it replaces, and that is the point.** This
/// content lived in `DevicesView.undescribableDevices` as a card of prose with
/// `.fixedSize(horizontal: false, vertical: true)` on every line, and in that
/// shape it measured **3413pt** — a locked phone blanked the entire pane while
/// trying to tell you to unlock it. Here every string is one line with the
/// explanation on a tooltip, which cannot grow at all. The `lineLimit`s are the
/// bound; keeping them is not optional, and neither is keeping the CLI's own
/// sentence off the row and on the `help`.
struct UndescribableDeviceRow: View {
    let failure: DeviceErrorRecord
    /// Mirrors `DevicesStore.isLoading`, so the retry can say it is working
    /// rather than just going dead.
    let isRetrying: Bool
    /// Re-run the full listing. See `SidebarView.retryDeviceListing` for why
    /// the failure of *that* run goes to the app's alert channel.
    let onRetry: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            // The generic silhouette, which is what an unrecognised product
            // type resolves to — and there is no product type here at all,
            // which is the same amount of knowledge. A plain body with no face
            // detail is truthful about what is not known; see
            // `DeviceSilhouette.Face.generic`.
            DeviceSilhouette(productType: "", size: CGSize(width: 14, height: 21))
                .opacity(0.6)
            VStack(alignment: .leading, spacing: 2) {
                Text(failure.udid)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.text)
                    .lineLimit(1)
                    // From the middle: a UDID's two halves are both
                    // distinguishing, same treatment `AppSidebarRow` gives a
                    // bundle id.
                    .truncationMode(.middle)
                Text("connected, but couldn't be read")
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
                    .lineLimit(1)
            }
            Spacer(minLength: 4)
            // The CLI's own sentence about this phone, on the dot rather than
            // in the row. It is the one string here whose length is not ours —
            // and the one somebody pastes into a bug report, which is why the
            // pane's version of this was `textSelection(.enabled)`. A tooltip
            // cannot be selected; the context menu's Copy UDID is what carries
            // that over, since the UDID is the half that identifies the phone.
            Circle()
                .fill(Palette.warn)
                .frame(width: 6, height: 6)
                .help(failure.summary)
            retryButton
        }
        .padding(.vertical, 2)
    }

    /// The only way back from this state, and it needs to be on the row.
    ///
    /// The poll cannot recover it on its own: it escalates to a full listing
    /// when the *set* of UDIDs on the bus changes, and a phone that is locked is
    /// still on the bus with the same UDID — so unlocking it and tapping "Trust
    /// this Computer" changes nothing the poll can see. Every other refresh in
    /// the app is attached to a device pane, and this row has no pane.
    private var retryButton: some View {
        Button(action: onRetry) {
            Symbols.refresh.image(size: 10)
                .foregroundStyle(Palette.textMuted)
        }
        .buttonStyle(.plain)
        .disabled(isRetrying)
        .help("Read this device again — unlock it and tap 'Trust this Computer' first.")
    }
}
