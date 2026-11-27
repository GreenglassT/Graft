import SwiftUI

/// The app pane's update line (spec 4.2), under the header: "Update
/// available: App Store 2026.47.0 · 1 tweak" and its button (Update &
/// Install, Update Tweaks & Install or Install Update), with notes for a
/// newer version this Mac can't get and for a device block, which is
/// advisory (review S3). It names the devices a new build installs on, and
/// each device it waits for (spec §11.3), with a menu holding **Stop
/// updating <app> on <device>**. An install only offered (spec §11.1) has
/// an Install Update per device it's offered on, each a Build & Install to
/// that device. What it says is `AppUpdateText.paneLine`'s.
struct AppUpdateLine: View {
    let line: AppUpdateText.PaneLine
    /// The button waits: the busy slot is held, or this app's own Get, tweak
    /// update or build refusal stands in the way (`AppDetailView`, review S9).
    let isBusy: Bool
    /// The button's tooltip (`AppUpdateText.buttonHelp`).
    var help: String?
    /// A Stop updating the CLI refused, said under the line.
    var failure: String? = nil
    /// Stop updating waits: an update runs, or another drop does.
    var stopUpdatingDisabled = false
    /// Stop updating on the device with this udid.
    var onStopUpdating: (String) -> Void = { _ in }
    /// Why the device with this udid can't take a Build & Install now
    /// ("… isn't connected"), or nil: its offered install waits, saying so.
    var offeredBlock: (String) -> String? = { _ in nil }
    /// An offered install's press: Build & Install to its device.
    var onInstallOffered: (AppUpdateText.PaneLine.Offered) -> Void = { _ in }
    /// Open Settings, for a note that says to set up the App Store (B5).
    var onOpenSettings: () -> Void = {}
    let onUpdate: () -> Void

    /// Whether the line wears the update mark: only an update. A line of
    /// notes alone (a newer version this Mac can't get, or a refusal) is
    /// information, and its arrow was there for good (B8).
    static func showsMark(_ line: AppUpdateText.PaneLine) -> Bool {
        line.headline != nil
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Metrics.controlGap) {
            if Self.showsMark(line) {
                Symbols.updateAvailable.image(size: 11)
                    .foregroundStyle(Palette.accent)
                    .accessibilityHidden(true)
            }
            VStack(alignment: .leading, spacing: 2) {
                if let headline = line.headline {
                    Text(headline)
                        .font(AppFont.body)
                        .foregroundStyle(Palette.text)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let installsOn = line.installsOn {
                    Text(installsOn)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textMuted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                ForEach(line.notes, id: \.self) { note in
                    Text(note)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textDim)
                        .fixedSize(horizontal: false, vertical: true)
                }
                ForEach(line.waiting) { waiting in
                    HStack(alignment: .firstTextBaseline, spacing: 4) {
                        Text(waiting.sentence)
                            .font(AppFont.monoSmall)
                            .foregroundStyle(Palette.textDim)
                            .fixedSize(horizontal: false, vertical: true)
                        Menu {
                            Button(waiting.stopTitle) { onStopUpdating(waiting.udid) }
                                .disabled(stopUpdatingDisabled)
                        } label: {
                            // A Label, not the glyph with `.accessibilityLabel`:
                            // a Menu's name comes from its label (`BuildsView`).
                            Label { Text(AppUpdateText.waitingMenuName(device: waiting.deviceName)) } icon: {
                                Symbols.rowMenu.image
                            }
                            .labelStyle(.iconOnly)
                        }
                        .menuStyle(.borderlessButton)
                        .menuIndicator(.hidden)
                        .fixedSize()
                        .help(waiting.stopTitle)
                    }
                }
                if let failure {
                    Text(failure)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.warn)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: Metrics.controlGap)
            if line.opensSettings {
                Button("Open Settings", action: onOpenSettings)
                    .controlSize(.small)
            }
            if let action = line.action {
                Button(action.title, action: onUpdate)
                    .controlSize(.small)
                    .disabled(isBusy)
                    .help(help ?? action.title)
            }
            if !line.offered.isEmpty {
                VStack(alignment: .trailing, spacing: 4) {
                    ForEach(line.offered) { offered in
                        let block = offeredBlock(offered.udid)
                        Button(offered.title) { onInstallOffered(offered) }
                            .controlSize(.small)
                            .disabled(isBusy || block != nil)
                            .help(block ?? offered.help)
                    }
                }
            }
        }
    }
}
