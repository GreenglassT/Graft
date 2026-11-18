import SwiftUI

/// Ask for a device's root SSH password and hand it back.
///
/// Its own sheet rather than a field on a card: a password is not something to
/// leave sitting on screen, and the CLI takes it on stdin precisely so it never
/// becomes an argument anyone can read out of `ps`.
///
/// Deliberately says what it is for. "SSH password" alone is a question most
/// people cannot answer about their own phone; naming the jailbreak default
/// turns it into one they can.
///
/// **This moved here from `DevicesView`, and moving it was not optional.** That
/// pane is deleted, and the sheet was the only way to set the credential from
/// the GUI — while `decrypt_ready.py` still tells the user, in the readiness
/// check rendered by `DeviceDecryptSection`, to "set the phone's root password
/// with `graft --set-ssh-password` (or from the device's row in the
/// Devices pane)". Deleting the sheet with the pane would have left that
/// sentence pointing at a screen that no longer exists. It is now opened from
/// the decrypt card itself — the card that renders the failing check that asks
/// for it.
struct SSHPasswordSheet: View {
    let device: DeviceRecord
    let onSave: (String) async -> Void

    @State private var password = ""
    @State private var saving = false
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: Metrics.sectionGap) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Root SSH password")
                    .font(AppFont.bodyMedium)
                    .foregroundStyle(Palette.text)
                Text("\(device.name) — needed by the frida-ios-dump decrypt backend, "
                     + "which pulls the app bundle off the phone over SSH. On a stock "
                     + "jailbreak this is `alpine`; if you changed it, use yours.")
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
                    .lineLimit(4)
                    .fixedSize(horizontal: false, vertical: true)
            }
            SecureField("Password", text: $password)
                .textFieldStyle(.roundedBorder)
                .onSubmit { save() }
            Text("Stored in your login Keychain for this device only, so the app can "
                 + "use it without a shell environment.")
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.textDim)
                .lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button(saving ? "Saving…" : "Save") { save() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(password.isEmpty || saving)
            }
        }
        .padding(Metrics.popoverGutter)
        // A fixed width, which is what keeps the two `fixedSize` sentences above
        // safe: they ask for the height their content needs at the width they
        // are proposed, and here that width is a constant of this file rather
        // than something the enclosing pane has to absorb. This is a sheet, not
        // a section of `DeviceDetailView` — see that pane's layout tests for
        // what an unbounded sentence does when it *is* in the pane.
        .frame(width: 420)
    }

    private func save() {
        guard !password.isEmpty, !saving else { return }
        saving = true
        Task {
            await onSave(password)
            saving = false
            dismiss()
        }
    }
}
