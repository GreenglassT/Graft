import Foundation

// The App Store acquisition path's events, decoded.
//
// A file of their own rather than four more structs in `BuildEvent.swift`,
// which keeps the edit to that shared file down to its enum cases and decode
// arms. The CLI side is `graft_ipa/appstore.py`; the field names are its
// spellings.

/// `appstore-status`: what the App Store path needs and whether this Mac has it.
///
/// Credentials never pass through the CLI or this app. `signedIn` is all there
/// is to know about the Apple ID here — the CLI never emits its email or name —
/// and `setupCommands` are the Terminal commands for whatever is missing, for
/// the user to copy and run themselves.
struct AppStoreStatusRecord: Decodable, Equatable {
    struct IPATool: Decodable, Equatable {
        let path: String
        let present: Bool
        /// nil when the binary is absent or would not say.
        let version: String?
        /// nil when ipatool could not say — `auth info` hung or printed no
        /// verdict. Not "signed out": that would send the user to type a
        /// password and a 2FA code for nothing.
        let signedIn: Bool?
        /// Set for a version known to be broken (2.8.1, whose downloads fail).
        let warning: String?

        enum CodingKeys: String, CodingKey {
            case path, present, version, warning
            case signedIn = "signed_in"
        }
    }

    struct UnfairPlay: Decodable, Equatable {
        let path: String
        let present: Bool
        let version: String?
        /// A recovered enrollment model exists. Without one nothing decrypts.
        let enrolled: Bool
        let modelDir: String

        enum CodingKeys: String, CodingKey {
            case path, present, version, enrolled
            case modelDir = "model_dir"
        }
    }

    let ipatool: IPATool
    let unfairplay: UnfairPlay
    let setupCommands: [String]

    enum CodingKeys: String, CodingKey {
        case ipatool, unfairplay
        case setupCommands = "setup_commands"
    }
}

/// `appstore-version`: one app's App Store version against what is cached.
///
/// `storeVersion` nil with `error` set is the CLI saying it could not tell —
/// a different claim from "no update", and the row says so.
struct AppStoreVersionRecord: Decodable, Equatable {
    let app: String
    let bundleId: String
    let storeVersion: String?
    let cachedVersion: String?
    let updateAvailable: Bool
    let error: String?

    enum CodingKeys: String, CodingKey {
        case app, error
        case bundleId = "bundle_id"
        case storeVersion = "store_version"
        case cachedVersion = "cached_version"
        case updateAvailable = "update_available"
    }
}

/// Where an App Store get (or an encrypted drop) has got to.
///
/// `other` keeps a stage name this app does not know yet, so a new step on
/// the CLI side still reads as work in progress rather than a dropped line.
/// Not part of the machine-checked contract — nothing here *branches* on a
/// stage, it only labels it (see `CLIContract.Stage`'s rule).
enum AppStoreStage: Equatable {
    case download
    case decrypt
    case stage
    case other(String)

    init(raw: String) {
        switch raw {
        case "download": self = .download
        case "decrypt": self = .decrypt
        case "stage": self = .stage
        default: self = .other(raw)
        }
    }

    /// The progress line the app pane shows while this stage runs.
    var label: String {
        switch self {
        case .download: return "Downloading…"
        case .decrypt: return "Decrypting…"
        case .stage: return "Staging…"
        case .other: return "Working…"
        }
    }
}

/// `appstore-stage`.
struct AppStoreStageRecord: Equatable {
    let app: String
    let stage: AppStoreStage
}

/// `appstore-decrypted`: what UnfairPlay reported for a decrypt that was kept.
struct AppStoreDecryptedRecord: Decodable, Equatable {
    let app: String
    let encryptedImages: Int
    let decryptedPages: Int
    /// Every decrypted page matched Apple's own pre-encryption hashes. The
    /// GUI never sees `false`: the CLI refuses that unless told otherwise on
    /// the command line, and this app does not offer the override.
    let plaintextVerified: Bool

    enum CodingKeys: String, CodingKey {
        case app
        case encryptedImages = "encrypted_images"
        case decryptedPages = "decrypted_pages"
        case plaintextVerified = "plaintext_verified"
    }
}
