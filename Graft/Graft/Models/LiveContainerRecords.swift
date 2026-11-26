import Foundation

// The `lc-*` events: LiveContainer itself, as `graft_ipa/lc_manage.py`
// manages it. In their own file rather than in `BuildEvent.swift` because a
// parallel branch adds records there too, and because they are one feature's
// vocabulary — the field-by-field meaning is in `json_output.EVENT_TYPES`.

/// One LiveContainer installed on a device.
struct LCInstanceRecord: Decodable, Hashable, Identifiable {
    let bundleId: String
    let version: String
    /// `primary`, `2`, `3`, or `legacy` — any other LiveContainer, e.g. an old
    /// copy under the bare `com.kdt.livecontainer` id.
    let role: String
    /// Signed with a wildcard application-identifier — the zsign-made copy
    /// whose keychain hook is off, and which iOS refuses to upgrade in place.
    let wildcardSigned: Bool

    var id: String { bundleId }

    /// "LiveContainer", "LiveContainer2", … as the instance calls itself.
    var displayName: String {
        switch role {
        case "primary": return "LiveContainer"
        case "2", "3": return "LiveContainer\(role)"
        default: return bundleId
        }
    }

    enum CodingKeys: String, CodingKey {
        case version, role
        case bundleId = "bundle_id"
        case wildcardSigned = "wildcard_signed"
    }
}

/// What `--lc-status` says about one device.
struct LCStatusRecord: Decodable, Hashable {
    struct Latest: Decodable, Hashable {
        let version: String
        let channel: String
    }

    enum ManagedBy: String, Decodable {
        case graft
        case altstore
        case unknown
    }

    let udid: String
    let reachable: Bool
    /// Why the device could not be asked; nil when it answered.
    let error: String?
    let instances: [LCInstanceRecord]
    let managedBy: ManagedBy
    /// Nil when the upstream lookup failed — not "no release exists".
    let latest: Latest?
    let updateAvailable: Bool
    /// `stable` / `nightly` → the AltStore source URL for that channel.
    let sources: [String: String]
    /// A certificate `--lc-push-cert` put in one of these LiveContainers that
    /// `--lc-clean` hasn't taken off yet: the build log's record, so it
    /// survives a restart and a push from Terminal. The device page's
    /// reminder (spec §5c). False from a CLI too old to say.
    let certificatePushed: Bool
    /// What a free Apple ID sets up on the device, the CLI's one copy
    /// (`config.FREE_DEVICE_STEPS`), with the URLs the steps name. The Set
    /// Up LiveContainer sheet shows them; empty from a CLI too old to send
    /// them.
    let freeSetupSteps: [String]

    /// One of the LiveContainers is the primary, the one `--lc-push-cert`
    /// puts the certificate in.
    var hasPrimary: Bool { instances.contains { $0.role == "primary" } }

    enum CodingKeys: String, CodingKey {
        case udid, reachable, error, instances, latest, sources
        case managedBy = "managed_by"
        case updateAvailable = "update_available"
        case certificatePushed = "certificate_pushed"
        case freeSetupSteps = "free_setup_steps"
    }
}

extension LCStatusRecord {
    /// The synthesized decoding, plus defaults for the two fields an older
    /// CLI doesn't send. In an extension, so the memberwise initializer
    /// stays.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        udid = try c.decode(String.self, forKey: .udid)
        reachable = try c.decode(Bool.self, forKey: .reachable)
        error = try c.decodeIfPresent(String.self, forKey: .error)
        instances = try c.decode([LCInstanceRecord].self, forKey: .instances)
        managedBy = try c.decode(ManagedBy.self, forKey: .managedBy)
        latest = try c.decodeIfPresent(Latest.self, forKey: .latest)
        updateAvailable = try c.decode(Bool.self, forKey: .updateAvailable)
        sources = try c.decode([String: String].self, forKey: .sources)
        certificatePushed = try c.decodeIfPresent(Bool.self, forKey: .certificatePushed) ?? false
        freeSetupSteps = try c.decodeIfPresent([String].self, forKey: .freeSetupSteps) ?? []
    }
}

/// One step of `--lc-install`. `instance` is nil for the shared fetch.
struct LCInstallProgressRecord: Decodable, Hashable {
    let udid: String
    let instance: Int?
    let step: String
    let detail: String
}

/// The install is blocked behind a running LiveContainer on the device.
struct LCInstallWaitingRecord: Decodable, Hashable {
    let udid: String
    let instance: Int
    let bundleId: String
    let message: String

    enum CodingKeys: String, CodingKey {
        case udid, instance, message
        case bundleId = "bundle_id"
    }
}

/// One instance installed (and recorded as ours).
struct LCInstalledRecord: Decodable, Hashable {
    let udid: String
    let bundleId: String
    let instance: Int
    let version: String
    let channel: String

    enum CodingKeys: String, CodingKey {
        case udid, instance, version, channel
        case bundleId = "bundle_id"
    }
}

/// The signing `.p12` is in the primary instance's Documents.
struct LCCertPushedRecord: Decodable, Hashable {
    let udid: String
    let bundleId: String
    let path: String
    /// The CLI's own sentence: the file stays, readable by guest apps, until
    /// it is removed. Optional so an older CLI's event still decodes.
    let note: String?

    enum CodingKeys: String, CodingKey {
        case udid, path, note
        case bundleId = "bundle_id"
    }
}

/// What `--lc-clean` removed, and how many other files it left alone.
struct LCCleanedRecord: Decodable, Hashable {
    let udid: String
    let bundleId: String
    let removed: [String]
    let kept: Int

    enum CodingKeys: String, CodingKey {
        case udid, removed, kept
        case bundleId = "bundle_id"
    }
}
