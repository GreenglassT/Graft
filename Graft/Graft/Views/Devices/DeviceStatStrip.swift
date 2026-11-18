import SwiftUI

/// Four numbers about one phone: storage, battery, LiveContainer, UDID.
///
/// **Each cell falls back on its own**, and that is the design decision in this
/// file rather than an implementation detail. The four values come from three
/// different places — two lockdown domains (`--device-info`) and the device
/// listing (`--devices`) — and a locked or half-awake phone routinely answers
/// one and declines the other. A strip that blanked all four because one came
/// back nil would be throwing away readings it actually has. So every cell
/// reads its own datum and renders "—" for its own absence;
/// `DeviceStatStripValueTests` pins that per cell.
///
/// The record may also be one `DeviceInfoStore` kept across an unreachable
/// poll: a phone on Wi-Fi drops a beat routinely, and the store deliberately
/// preserves the last good numbers rather than blanking them (see its type
/// doc). Nothing here consults `reachable` for that reason — the numbers on the
/// record are the last ones that were true, and showing them beats a strip that
/// flickers to dashes and back every few seconds.
struct DeviceStatStrip: View {
    let device: DeviceRecord
    /// The last thing `DeviceInfoStore` heard about this phone, or nil when it
    /// has never heard anything — a phone that was locked for every poll so
    /// far. Both cases render the same dashes; neither is an error worth
    /// interrupting anyone over.
    let info: DeviceInfoRecord?

    var body: some View {
        // `controlGap`, matching `AppDetailView`'s meta strip: these cells sit
        // directly on the pane background rather than nested inside a card, so
        // they take the default corner radius and the tighter gutter.
        HStack(spacing: Metrics.controlGap) {
            storageCell
            MetaCell(
                label: "Battery",
                value: Self.batteryValue(
                    percent: info?.batteryPercent, charging: info?.batteryCharging
                )
            )
            MetaCell(
                label: "LiveContainer",
                value: Self.liveContainerValue(device.livecontainerBundleId)
            )
            MetaCell(label: "UDID", value: device.udid)
        }
    }

    // MARK: - Storage, with its bar

    /// The bar is an `.overlay`, not another row in the cell, and that is
    /// load-bearing: an overlay cannot change the size of what it covers, so
    /// the storage cell stays exactly as tall as the three beside it and the
    /// strip cannot grow a row taller than the pane expects. It is drawn inside
    /// the cell's own `Metrics.innerPadding`, in the blank the padding already
    /// reserves below the value text.
    private var storageCell: some View {
        MetaCell(
            label: "Storage",
            value: Self.storageValue(free: info?.freeBytes, total: info?.totalBytes)
        )
        .overlay(alignment: .bottom) { usedBar }
    }

    /// Height of the usage bar, and how far it sits above the cell's bottom
    /// edge. Both inside `Metrics.innerPadding` (10pt), which is what keeps the
    /// bar clear of the value text above it.
    private static let barHeight: CGFloat = 3
    private static let barInset: CGFloat = 3

    /// No reading, no bar. An empty track drawn from missing data would claim
    /// the phone is empty, which is a stronger statement than "—" makes.
    @ViewBuilder
    private var usedBar: some View {
        if let used = Self.usedFraction(free: info?.freeBytes, total: info?.totalBytes) {
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Palette.surface2)
                    Capsule()
                        .fill(Palette.accent)
                        .frame(width: geo.size.width * used)
                }
            }
            .frame(height: Self.barHeight)
            .padding(.horizontal, Metrics.innerPadding)
            .padding(.bottom, Self.barInset)
        }
    }

    // MARK: - The values

    /// The strip's "no reading" mark, the same one `AppDetailView`'s meta strip
    /// uses. An em dash rather than "unknown" or an empty cell: it reads as a
    /// deliberate blank at a glance, and it is the same width whatever cell it
    /// lands in.
    private static let missing = "—"


    /// `35.73 GB free of 128 GB`.
    ///
    /// Both halves come from one lockdown reading, so a half-answer is not a
    /// shape the CLI produces today. It still gets the dash rather than a
    /// sentence with a hole in it, because "35.73 GB free of —" would read as a
    /// bug in the app rather than a gap in the data.
    static func storageValue(free: Int?, total: Int?) -> String {
        guard let free, let total else { return missing }
        return "\(human(free)) free of \(human(total))"
    }

    /// `.file` via the shared `fileSize`, so the units are the 1000-based ones
    /// Apple quotes device capacity in — a 128 GB phone reads "128 GB" rather
    /// than "119.2 GB". This held a private cached formatter of its own until
    /// `fileSize` existed; that formatter is still cached, just once for the
    /// whole app rather than once per view that wanted one.
    private static func human(_ bytes: Int) -> String {
        fileSize(bytes)
    }

    /// How full the phone is, 0…1, for the bar — the *used* share, so a nearly
    /// full phone reads as a nearly full bar.
    ///
    /// nil rather than 0 when either number is missing, and clamped rather than
    /// trusted: a zero total would divide by zero, and lockdown on a phone
    /// mid-restore has reported figures that don't add up. A bar drawn past its
    /// own track is a visual defect; a fraction that is `nan` is a crash in
    /// `frame(width:)`.
    static func usedFraction(free: Int?, total: Int?) -> Double? {
        guard let free, let total, total > 0 else { return nil }
        return min(max(Double(total - free) / Double(total), 0), 1)
    }

    /// `70% · charging`, or `70%`.
    ///
    /// An unknown charging state falls back to the bare percentage rather than
    /// to a dash: the percentage is a reading in its own right, and the two
    /// fields arrive independently.
    static func batteryValue(percent: Int?, charging: Bool?) -> String {
        guard let percent else { return missing }
        return charging == true ? "\(percent)% · charging" : "\(percent)%"
    }

    /// The bundle id, or `not installed`.
    ///
    /// **Not a version, because there is no version on the wire.** The record's
    /// only LiveContainer field is `livecontainer_bundle_id`, which the CLI
    /// fills from the installed-app scan — `com.kdt.livecontainer.ABCDE12345`,
    /// where the suffix is the signing team, not a release. Nothing in
    /// `--devices` or `--device-info` reports what version is installed, so a
    /// cell claiming to show one would be inventing it. The id is also the
    /// thing worth having: it is what a build is delivered *into*, and it is
    /// what tells two LiveContainers on the same phone apart.
    ///
    /// nil means the scan looked and found none, not "unknown" — so this says
    /// so, rather than falling back on the strip's dash.
    static func liveContainerValue(_ bundleId: String?) -> String {
        bundleId ?? "not installed"
    }
}
