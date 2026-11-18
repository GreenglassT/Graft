import Foundation

enum DeliveryMode: String, Codable, CaseIterable {
    case sideload
    case livecontainer

    /// How this mode is named in a sentence.
    ///
    /// **Not a new vocabulary — this is the spelling `AddAppSheet`'s delivery
    /// picker has always shown**, lifted out of it so there is one definition
    /// of it rather than a second literal on the app pane's build-target bar.
    ///
    /// Both delivery pickers — `AddAppSheet`'s and `EditAppSheet`'s — now read
    /// this, so the same control is no longer spelled two ways in two sheets.
    ///
    /// `rawValue` has no display site left: the app pane's meta strip used to
    /// show it in `AppFont.mono` under a DELIVERY label, as the token in
    /// config.yaml rather than prose about it, and that cell has since been
    /// replaced by delivery *state*. The mode is said in prose instead — the
    /// build-target bar's "…via LiveContainer" and the app pane header's
    /// identity line (`AppDetailView.identityLine`) — where the lowercase token
    /// would read as a typo instead of as a quoted value. `rawValue` remains
    /// what is written to and read from config.yaml, which is its whole job.
    ///
    /// **That is not the same as this being the only spelling on screen**, and
    /// the doc here used to imply it was. `AppsListView.deliveryChip` renders
    /// `TagChip(label: "LC", …)` for livecontainer and `TagChip(label:
    /// "Sideload")` for the other — an abbreviated third spelling, written
    /// inline, because a sidebar row's chip has room for two characters and not
    /// for thirteen. (The "LC" is now drawn as `Marks.liveContainer` with the
    /// text hidden beneath it, so it is what VoiceOver reads rather than what
    /// the eye sees; the string is still the chip's.) Unifying it would mean
    /// either putting an `LC` abbreviation on this type for one caller or
    /// widening the row, so it is left alone deliberately rather than
    /// overlooked.
    var displayName: String {
        switch self {
        case .sideload: return "Sideload"
        case .livecontainer: return "LiveContainer"
        }
    }
}

struct CyanOptions: Codable, Hashable {
    var displayName: String?
    var keepBundleId: Bool = true
    var newBundleId: String?
    var stripExtensions: Bool = false
    var stripSupportedDevices: Bool = true
    var compressionLevel: Int = 6

    enum CodingKeys: String, CodingKey {
        case displayName = "display_name"
        case keepBundleId = "keep_bundle_id"
        case newBundleId = "new_bundle_id"
        case stripExtensions = "strip_extensions"
        case stripSupportedDevices = "strip_supported_devices"
        case compressionLevel = "compression_level"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        displayName = try c.decodeIfPresent(String.self, forKey: .displayName)
        keepBundleId = try c.decodeIfPresent(Bool.self, forKey: .keepBundleId) ?? true
        newBundleId = try c.decodeIfPresent(String.self, forKey: .newBundleId)
        stripExtensions = try c.decodeIfPresent(Bool.self, forKey: .stripExtensions) ?? false
        stripSupportedDevices = try c.decodeIfPresent(Bool.self, forKey: .stripSupportedDevices) ?? true
        compressionLevel = try c.decodeIfPresent(Int.self, forKey: .compressionLevel) ?? 6
    }
}

struct AppConfig: Codable, Hashable, Identifiable {
    var bundleId: String
    var preferredVersion: String = "latest"
    var tweaks: [String] = []
    var cyan: CyanOptions = CyanOptions()
    var delivery: DeliveryMode = .livecontainer
    var autoProfile: Bool = false
    var profile: String?

    var id: String { bundleId }

    enum CodingKeys: String, CodingKey {
        case tweaks, cyan, delivery, profile
        case bundleId = "bundle_id"
        case preferredVersion = "preferred_version"
        case autoProfile = "auto_profile"
    }

    init(bundleId: String) {
        self.bundleId = bundleId
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        bundleId = try c.decode(String.self, forKey: .bundleId)
        preferredVersion = try c.decodeIfPresent(String.self, forKey: .preferredVersion) ?? "latest"
        tweaks = try c.decodeIfPresent([String].self, forKey: .tweaks) ?? []
        if let cyanValue = try c.decodeIfPresent(CyanOptions.self, forKey: .cyan) {
            cyan = cyanValue
        } else {
            cyan = CyanOptions()
        }
        if let raw = try c.decodeIfPresent(String.self, forKey: .delivery), let d = DeliveryMode(rawValue: raw) {
            delivery = d
        } else {
            // Pre-addendum configs omit delivery — default to sideload for backward compat.
            // The GUI's "Add App" flow constructs new AppConfig instances explicitly setting livecontainer.
            delivery = .sideload
        }
        autoProfile = try c.decodeIfPresent(Bool.self, forKey: .autoProfile) ?? false
        profile = try c.decodeIfPresent(String.self, forKey: .profile)
    }
}
