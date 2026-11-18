import SwiftUI

enum AppFont {
    // SF Pro (system) for UI
    static let title = Font.system(size: 16, weight: .semibold).leading(.tight)
    static let body = Font.system(size: 13)
    static let bodyMedium = Font.system(size: 13, weight: .medium)
    static let label = Font.system(size: 11, weight: .semibold).smallCaps()

    // JetBrains Mono for identifiers, filenames, logs
    static let mono = Font.system(size: 12, design: .monospaced)
    static let monoSmall = Font.system(size: 11, design: .monospaced)
}

extension View {
    func tightTitle() -> some View {
        font(AppFont.title)
            .tracking(-0.3)
            .foregroundStyle(Palette.text)
    }

    func labelCaps() -> some View {
        font(AppFont.label)
            .tracking(0.6)
            .textCase(.uppercase)
            .foregroundStyle(Palette.textMuted)
    }
}
