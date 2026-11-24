import Foundation

/// `init-step`: one thing `graft --init --account …` did, or under
/// `--dry-run` would do, in a sentence the app shows as it is.
struct InitStepRecord: Decodable, Hashable {
    /// "profile", "certificate", "keychain", "files" or "config". Rendered,
    /// never branched on: the sentence is `detail`.
    let step: String
    let detail: String
}

/// `init-result`: what `graft --init --account …` came to. Exactly one per
/// run that worked; a refusal exits non-zero, says why on stderr, and emits
/// none. Never carries the password.
struct InitResultRecord: Decodable, Hashable {
    let account: AccountMode
    /// config.yaml's path.
    let config: String
    /// "created", "upgraded", "switched" or "unchanged".
    let change: String
    let dryRun: Bool
    /// The CLI's own sentence for what happened: the done screen's headline.
    let summary: String
    // A paid account's; nil on a free Apple ID.
    let teamId: String?
    let profileName: String?
    /// The .p12 certificate's common name.
    let certificate: String?
    let deviceCount: Int?
    /// The UDIDs the profile covers.
    let devices: [String]
    /// When the profile expires, ISO 8601 in UTC; nil when it doesn't say.
    let expires: String?
    /// Apps an upgrade gave `delivery: livecontainer`, so none moved.
    let pinned: [String]
    /// Apps still set to Sideload after a switch to a free Apple ID.
    let sideloads: [String]
    /// A free Apple ID's steps on the device (`config.FREE_DEVICE_STEPS`).
    let steps: [String]
    /// The team `signing:` named before a replace (`change` "replaced"); nil
    /// for every other change, and from a CLI too old to say.
    let previousTeamId: String?
    /// What the user should know although it worked: a stale
    /// GRAFT_P12_PASS, the old team's Keychain item kept, an App Store
    /// Connect key for the old team, a certificate not checked against the
    /// profile. Nil from a CLI too old to say (`notes` reads it).
    let warnings: [String]?

    enum CodingKeys: String, CodingKey {
        case account, config, change, summary, certificate, devices, expires, pinned, sideloads, steps
        case warnings
        case dryRun = "dry_run"
        case teamId = "team_id"
        case profileName = "profile_name"
        case deviceCount = "device_count"
        case previousTeamId = "previous_team_id"
    }

    var expiryDate: Date? { expires.flatMap(DateHelpers.parseISO8601) }

    /// `warnings`, or none.
    var notes: [String] { warnings ?? [] }

    /// The team before, when a replace moved the account to another one.
    var movedFromTeam: String? {
        guard change == "replaced", let previousTeamId, previousTeamId != teamId else { return nil }
        return previousTeamId
    }
}

/// A free Apple ID's device step, which names the pages it sends you to.
enum FreeSetupStep {
    /// Every `https://` URL in `text`, in order, without the punctuation a
    /// sentence puts after one.
    static func links(in text: String) -> [URL] {
        guard let pattern = try? NSRegularExpression(pattern: #"https://[^\s)]+"#) else { return [] }
        let range = NSRange(text.startIndex..., in: text)
        return pattern.matches(in: text, range: range).compactMap { match in
            guard let found = Range(match.range, in: text) else { return nil }
            var url = String(text[found])
            while let last = url.last, ".,;:".contains(last) { url.removeLast() }
            return URL(string: url)
        }
    }
}
