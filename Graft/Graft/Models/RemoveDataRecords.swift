import Foundation

/// `data-item`: one place Graft keeps something outside Graft.app, from
/// `--remove-data` (`graft_ipa/remove_data.py`, which says where each one
/// comes from).
struct DataItemRecord: Decodable, Hashable, Identifiable {
    struct KeychainEntry: Decodable, Hashable {
        let service: String
        let account: String
    }

    /// What `--items` takes.
    let id: String
    let group: String
    /// The heading it is listed under.
    let groupTitle: String
    let title: String
    /// What it is, in a sentence or two.
    let detail: String
    /// A caution, such as another program sharing it.
    let note: String?
    /// How to remove it by hand, for one the CLI doesn't remove.
    let how: String?
    let paths: [String]
    let keychain: [KeychainEntry]
    /// Nil for keychain items, and for what couldn't be checked.
    let sizeBytes: Int?
    /// Nil when the keychain was locked or didn't answer, so it wasn't checked.
    let present: Bool?
    /// Chosen unless unchecked. False for the passwords.
    let isDefault: Bool
    /// Whether the CLI removes it. False for what the user chose (group
    /// `kept`) and for Graft.app's own settings, which the app removes.
    let removable: Bool

    enum CodingKeys: String, CodingKey {
        case id, group, title, detail, note, how, paths, keychain, present, removable
        case groupTitle = "group_title"
        case sizeBytes = "size_bytes"
        case isDefault = "default"
    }

    /// Graft.app's own preferences: listed by the CLI, removed by the app.
    static let appSettingsID = "app-settings"
    /// What the user chose to keep outside Graft's folders: listed, never removed.
    static let keptGroup = "kept"

    var isAppSettings: Bool { id == Self.appSettingsID }

    /// Whether the sheet offers a checkbox for it: the CLI removes it, or
    /// it is the app's own settings, which the app removes.
    var canRemoveHere: Bool { removable || isAppSettings }
}

/// `data-items-end`: the listing's sentinel.
struct DataItemsEndRecord: Decodable, Hashable {
    let count: Int
    let totalBytes: Int
    /// "checked", "locked" or "no-answer".
    let keychain: String

    enum CodingKeys: String, CodingKey {
        case count, keychain
        case totalBytes = "total_bytes"
    }

    static let keychainChecked = "checked"
}

/// `data-removed`: what removing one item did.
struct DataRemovedRecord: Decodable, Hashable {
    struct Failure: Decodable, Hashable {
        let what: String
        let reason: String
    }

    let id: String
    let ok: Bool
    let dryRun: Bool
    /// Each path, and each keychain item as "<service> <account>", that went.
    let removed: [String]
    let failed: [Failure]
    let freedBytes: Int

    enum CodingKeys: String, CodingKey {
        case id, ok, removed, failed
        case dryRun = "dry_run"
        case freedBytes = "freed_bytes"
    }
}

/// `data-removal-end`: the removal's sentinel.
struct DataRemovalEndRecord: Decodable, Hashable {
    let ok: Bool
    let dryRun: Bool
    let removed: [String]
    let failed: [String]
    let freedBytes: Int

    enum CodingKeys: String, CodingKey {
        case ok, removed, failed
        case dryRun = "dry_run"
        case freedBytes = "freed_bytes"
    }
}
