import Foundation

/// Which kind of Apple account config.yaml is set up for: the CLI's
/// `Config.account`, carried by `--list`'s `account` event.
///
/// **Free** is a config with no `signing:` section. Graft then signs nothing:
/// it builds unsigned IPAs and pushes them into a LiveContainer that SideStore,
/// AltStore, iloader or Impactor installed with the user's free Apple ID, and
/// LiveContainer signs each one when it is imported. Everything that signs is
/// refused by the CLI (`PaidAccountRequiredError`), and this app disables it
/// with the reason in `PaidOnly` rather than offering a button that fails.
///
/// **Paid** is everything, as before.
///
/// Read as `AccountMode?` everywhere, and nil means *not known*: before the
/// first `--list` has landed, or a CLI too old to say. Nil disables nothing.
/// Guessing free would grey out a paid user's controls on a stale CLI, and
/// guessing paid costs at most a refusal the CLI words itself, which every
/// banner already shows.
enum AccountMode: String, Decodable, Hashable {
    case free
    case paid

    /// The Settings pane's Account row.
    var summary: String {
        switch self {
        case .free: return "Free Apple ID: LiveContainer delivery only"
        case .paid: return "Paid Apple Developer account"
        }
    }

    /// The line under it: what the mode means, and how to change it.
    var detail: String {
        switch self {
        case .free:
            return "Graft builds unsigned IPAs and LiveContainer signs them when you "
                + "import them. To use a paid membership, run graft --init in Terminal "
                + "and answer yes."
        case .paid:
            return "Graft signs with your certificate and profile, for sideload "
                + "delivery and for installing LiveContainer."
        }
    }
}

/// The one-line reasons a paid-only control is disabled on a free Apple ID.
///
/// One place, so the same refusal reads the same on every surface, and so a
/// test can hold a view to the sentence it shows. Each one says what is needed
/// and, where there is one, what to do instead.
enum PaidOnly {
    /// The delivery pickers: Add App and Edit.
    static let sideload = "Sideload needs a paid Apple Developer account. With a free "
        + "Apple ID, apps go into LiveContainer."

    /// A `delivery: sideload` app left in a free config: every build of it,
    /// and every re-install of an archive of it, is refused.
    static let sideloadApp = "This app is set to Sideload, which needs a paid Apple "
        + "Developer account. Switch it to LiveContainer with Edit…"

    /// The device pane's Install / Update LiveContainer. Where to update it
    /// instead is the section's own note (`freeAccountNotes`), which knows who
    /// installed it; this says only why the button is off.
    static let installLiveContainer = "Installing or updating LiveContainer from Graft "
        + "needs a paid Apple Developer account."

    /// The device pane's Put certificate on device.
    static let pushCertificate = "Putting a certificate on the device needs a paid Apple "
        + "Developer account. Import the certificate from SideStore in LiveContainer instead."

    /// Settings: the .p12 password.
    static let p12Password = "A .p12 password is only used with a paid Apple Developer "
        + "account."

    /// Settings: the signing certificate, profile and team id.
    static let signingSettings = "Signing is only used with a paid Apple Developer "
        + "account. Run graft --init and answer yes to set it up."
}

extension Optional where Wrapped == AccountMode {
    /// A free Apple ID, known to be one. Nil is not free: see `AccountMode`.
    var isFree: Bool { self == .free }
}
