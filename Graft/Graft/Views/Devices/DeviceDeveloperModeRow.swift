import SwiftUI

/// "Developer Mode: On" or "Off", under a device's stat strip (spec §5).
///
/// iOS 16 and later run a development-signed app only with Developer Mode on.
/// The CLI stops such a build before it starts when it's off; this row is
/// where the person can see it ahead of that. An ad hoc or enterprise profile
/// doesn't need it, and nor does a LiveContainer push, so a config with no
/// app signed with a development profile never shows the row.
struct DeviceDeveloperModeRow: View {
    let isOn: Bool

    /// The CLI's hint, word for word (`developer_mode.TURN_ON`).
    static let turnOn = "Turn on Developer Mode on your iPhone or iPad in Settings › Privacy & Security › "
        + "Developer Mode, then restart it."

    static func line(isOn: Bool) -> String {
        "Developer Mode: \(isOn ? "On" : "Off")"
    }

    /// The reading to show for this device, or nil to show no row.
    ///
    /// Any device, not just the default: a build's device pick or `--device`
    /// can send a build to any of them.
    ///
    /// - Parameters:
    ///   - developerMode: `--device-info`'s reading; nil before iOS 16 or unread.
    ///   - apps: the configured apps; only one whose `needsDeveloperMode` is
    ///     true (signed with a development profile) makes it matter. Nil,
    ///     a profile the CLI couldn't read, doesn't.
    static func shown(developerMode: Bool?, apps: [AppRecord]) -> Bool? {
        apps.contains(where: { $0.needsDeveloperMode == true }) ? developerMode : nil
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                (isOn ? Symbols.ok : Symbols.attention).image(size: 11)
                    .foregroundStyle(isOn ? Palette.success : Palette.warn)
                    // The line beside it says it in words.
                    .accessibilityHidden(true)
                Text(Self.line(isOn: isOn))
                    .font(AppFont.body)
                    .foregroundStyle(Palette.text)
                    .lineLimit(1)
            }
            if !isOn {
                // Wraps inside the pane's scroll view; never `fixedSize`
                // (`DeviceDetailLayoutTests`).
                Text(Self.turnOn)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
                    .lineLimit(3)
            }
        }
        .accessibilityElement(children: .combine)
    }
}
