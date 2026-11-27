import Foundation

/// `update-plan`: what `graft --update` would change, per app (`--plan`), or
/// is about to change (a run's first event). `update_plan.payload` in the CLI.
struct UpdatePlanRecord: Decodable, Equatable {
    /// The background form (`--quick`): readiness is "likely", from what the
    /// last full look at the phone said.
    let quick: Bool
    /// It asked the App Store and canister.me and neither answered, as when
    /// offline (`update_plan.checks_failed`): every app reads up to date and
    /// isn't known to be, so `AppUpdatesStore` keeps what it showed. False
    /// from a CLI too old to say.
    var checksFailed = false
    let apps: [UpdatePlanApp]
}

extension UpdatePlanRecord {
    enum CodingKeys: String, CodingKey {
        case quick, apps
        case checksFailed = "checks_failed"
    }

    /// In an extension, so the memberwise initializer stays.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        quick = try c.decode(Bool.self, forKey: .quick)
        checksFailed = try c.decodeIfPresent(Bool.self, forKey: .checksFailed) ?? false
        apps = try c.decode([UpdatePlanApp].self, forKey: .apps)
    }
}

/// One app's plan.
struct UpdatePlanApp: Decodable, Equatable, Identifiable {
    let app: String
    let displayName: String?
    let store: StoreStep
    let tweaks: [TweakChange]
    let install: Bool
    let installSkippedReason: String?
    /// The install is offered for the person to press, not run on its own
    /// (spec §11.1): after a Re-install of an older build, or when the last
    /// install couldn't be found on the device. `installSkippedReason` says
    /// which.
    let manualInstall: Bool
    /// Set only when no device can take the install now (each is waiting or
    /// blocked): each one's reason, joined with "; ". Each device says its
    /// own in `targets`.
    let blockedReason: String?
    let readiness: String
    /// What a quick plan shows without blocking ("Developer Mode was off on
    /// iPhone 15 Pro when Graft last checked"), or nil.
    let readinessNote: String?
    /// Something to do now, on a device that's there (contract G): a get,
    /// a tweak update or a device's install, and at least one device that
    /// is neither waiting nor blocked. The CLI's word; `isActionable` is the
    /// app's reading of it.
    let actionable: Bool
    /// Every device the app is on (spec §11.3), the default first, then by
    /// name. A device the person stopped updating on is left out.
    let targets: [Target]
    let nothingToDo: Bool

    var id: String { app }

    /// The App Store step: `to` is the App Store's newer version, if any, and
    /// `skippedReason` why it won't be fetched.
    struct StoreStep: Decodable, Equatable {
        let from: String?
        let to: String?
        let skippedReason: String?

        enum CodingKeys: String, CodingKey {
            case from, to
            case skippedReason = "skipped_reason"
        }
    }

    /// One attached tweak with a newer build (`to`), or that couldn't be
    /// checked (`unchecked`). `alsoAffects` names the apps outside this run
    /// that use it and will need an install afterward.
    ///
    /// `source`: where the newer build comes from, the tweak's own source
    /// (2.3.1 A1). `verified`: that source is known, uses HTTPS and
    /// publishes a checksum for it; `unverifiedReason` says why not.
    /// `confirmed`: verified, or the person said yes (`--trust-tweak`); only
    /// a confirmed one is part of an update. Each is false or nil when the
    /// CLI leaves it out: what's missing never reads as trust.
    struct TweakChange: Decodable, Equatable, Identifiable {
        let filename: String
        let name: String?
        let from: String?
        let to: String?
        let unchecked: Bool
        let alsoAffects: [String]
        var source: String? = nil
        var verified = false
        var unverifiedReason: String? = nil
        var confirmed = false
        /// What `--trust-tweak` takes to confirm exactly this update (its
        /// file, source, version and builds, re-verify #2): the person's
        /// yes is for this key, and the run refuses one that changed.
        var confirmKey: String? = nil
        /// A key the run was given for it is for another update of it.
        var trustChanged = false

        var id: String { filename }

        /// A newer build that runs only once the person confirms it.
        var awaitsConfirmation: Bool { !unchecked && to != nil && !confirmed }

        enum CodingKeys: String, CodingKey {
            case filename, name, from, to, unchecked, source, verified, confirmed
            case alsoAffects = "also_affects"
            case unverifiedReason = "unverified_reason"
            case confirmKey = "confirm_key"
            case trustChanged = "trust_changed"
        }
    }

    /// One device the app is on (spec §11.3), with what an update would do
    /// there. `waiting`: the sentence when it isn't connected ("iPad mini
    /// isn't connected: it'll update when it is"); a waiting device never
    /// installs in a run. `blockedReason`: any other reason it can't now.
    struct Target: Decodable, Equatable, Identifiable {
        let udid: String
        let deviceName: String?
        let delivery: String
        let lcBundleId: String?
        /// This device gets an install in a run: the build changes, or its
        /// own delivery state calls for one.
        let install: Bool
        let installSkippedReason: String?
        /// An install offered for the person to press (spec §11.1), here.
        let manualInstall: Bool
        let waiting: String?
        let blockedReason: String?
        let readiness: String
        let readinessNote: String?

        var id: String { udid }
        /// Its delivery state ("never", "stale", "current", "unknown") and
        /// whether its device was on usbmux, for every target: the device's
        /// own facts, which `install` and `waiting` aren't alone, being worked
        /// out only for a target with work (`AppUpdatesStore.merged`). Nil
        /// from a CLI too old to send them.
        var kind: String? = nil
        var connected: Bool? = nil

        /// Neither waiting nor blocked: an install can go here now.
        var isReady: Bool { waiting == nil && blockedReason == nil }
        var deliversToLiveContainer: Bool { delivery == DeliveryMode.livecontainer.rawValue }

        enum CodingKeys: String, CodingKey {
            case udid, delivery, install, waiting, readiness, kind, connected
            case deviceName = "device_name"
            case lcBundleId = "lc_bundle_id"
            case installSkippedReason = "install_skipped_reason"
            case manualInstall = "manual_install"
            case blockedReason = "blocked_reason"
            case readinessNote = "readiness_note"
        }
    }

    enum CodingKeys: String, CodingKey {
        case app, store, tweaks, install, readiness, actionable, targets
        case displayName = "display_name"
        case installSkippedReason = "install_skipped_reason"
        case manualInstall = "manual_install"
        case blockedReason = "blocked_reason"
        case readinessNote = "readiness_note"
        case nothingToDo = "nothing_to_do"
    }

    /// A get from the App Store is planned.
    var getsFromStore: Bool { store.to != nil && store.skippedReason == nil }
    /// The tweaks with a newer build to update to, from a source that's
    /// verified or that the person confirmed.
    var tweakUpdates: [TweakChange] { tweaks.filter { !$0.unchecked && $0.to != nil && $0.confirmed } }
    /// The tweaks with a newer build from a source that isn't verified, which
    /// the confirmation asks about ("Trust this source"), off until asked.
    var unconfirmedTweaks: [TweakChange] { tweaks.filter(\.awaitsConfirmation) }
    /// A tweak update waits for the person's OK: shown on the pane and in
    /// the confirmation, but not on its own an update (no mark, not in
    /// Update All).
    var needsConfirmation: Bool { !unconfirmedTweaks.isEmpty }
    /// The get or a tweak update changes the build, so every device needs
    /// the install.
    var changesBuild: Bool { getsFromStore || !tweakUpdates.isEmpty }
    /// The devices an update would install on now, in the plan's order.
    var installTargets: [Target] { targets.filter { $0.isReady && $0.install } }
    /// The devices that need the install but aren't connected: they get it
    /// once they are. The CLI says `waiting` only of a device with work, so
    /// one already current is never here.
    var waitingTargets: [Target] { targets.filter { $0.waiting != nil } }
    /// The devices connected but not ready ("Developer Mode is off on…").
    var blockedTargets: [Target] { targets.filter { $0.waiting == nil && $0.blockedReason != nil } }
    /// Something to update, now or once a device is back or a block clears
    /// (the pane's line, the confirmation's rows): a new build, or a device
    /// that installs, waits or is blocked. An app never delivered is Build
    /// & Install's, not Update's, and an install only offered
    /// (`manualInstall`) is the person's to press.
    var hasUpdate: Bool {
        (!nothingToDo && (changesBuild ? !targets.isEmpty : targets.contains { $0.install || !$0.isReady }))
            || (needsConfirmation && !targets.isEmpty && installSkippedReason == nil)
    }
    /// Something Update would do now, on its own, on a device that's there:
    /// the sidebar's mark, Update All's count, and what the confirmation
    /// runs. Work waiting for a device (spec §11.3, devices that are gone)
    /// and an install only offered (§11.1) aren't.
    var isActionable: Bool { actionable && (changesBuild || !installTargets.isEmpty) }
}

extension UpdatePlanApp {
    /// This plan without the device `udid`: what a Stop updating there
    /// leaves, when no plan is read again (the switch off, spec §11.4).
    func dropping(_ udid: String) -> UpdatePlanApp {
        UpdatePlanApp(app: app, displayName: displayName, store: store, tweaks: tweaks, install: install,
                      installSkippedReason: installSkippedReason, manualInstall: manualInstall,
                      blockedReason: blockedReason, readiness: readiness, readinessNote: readinessNote,
                      actionable: actionable, targets: targets.filter { $0.udid != udid }, nothingToDo: nothingToDo)
    }
}

/// `update-step`: one step starting, ending or skipped. `app` is nil for the
/// shared tweak step; `udid` and `deviceName` name the device of an
/// `install` step, and are nil for the others. `waiting`: an install that
/// ended or was skipped because its device wasn't connected, with the
/// waiting sentence as `detail` (review part 2, #9); false when left out.
struct UpdateStepRecord: Decodable, Equatable {
    let app: String?
    let step: String
    let status: String
    let detail: String?
    var udid: String? = nil
    var deviceName: String? = nil
    var waiting = false

    enum CodingKeys: String, CodingKey {
        case app, step, status, detail, udid, waiting
        case deviceName = "device_name"
    }
}

extension UpdatePlanApp.TweakChange {
    /// The trust fields may be missing, and read as untrusted. In an
    /// extension, so the memberwise initializer stays.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        filename = try c.decode(String.self, forKey: .filename)
        name = try c.decodeIfPresent(String.self, forKey: .name)
        from = try c.decodeIfPresent(String.self, forKey: .from)
        to = try c.decodeIfPresent(String.self, forKey: .to)
        unchecked = try c.decode(Bool.self, forKey: .unchecked)
        alsoAffects = try c.decode([String].self, forKey: .alsoAffects)
        source = try c.decodeIfPresent(String.self, forKey: .source)
        verified = try c.decodeIfPresent(Bool.self, forKey: .verified) ?? false
        unverifiedReason = try c.decodeIfPresent(String.self, forKey: .unverifiedReason)
        confirmed = try c.decodeIfPresent(Bool.self, forKey: .confirmed) ?? false
        confirmKey = try c.decodeIfPresent(String.self, forKey: .confirmKey)
        trustChanged = try c.decodeIfPresent(Bool.self, forKey: .trustChanged) ?? false
    }
}

extension UpdateStepRecord {
    /// `waiting` may be missing, and reads as false. In an extension, so the
    /// memberwise initializer stays.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        app = try c.decodeIfPresent(String.self, forKey: .app)
        step = try c.decode(String.self, forKey: .step)
        status = try c.decode(String.self, forKey: .status)
        detail = try c.decodeIfPresent(String.self, forKey: .detail)
        udid = try c.decodeIfPresent(String.self, forKey: .udid)
        deviceName = try c.decodeIfPresent(String.self, forKey: .deviceName)
        waiting = try c.decodeIfPresent(Bool.self, forKey: .waiting) ?? false
    }
}

/// `update-stopping`: a Stop is waiting for `step` to finish.
struct UpdateStoppingRecord: Decodable, Equatable {
    let app: String?
    let step: String
}

/// `update-app-end`: one app's result. `ok` when every device that wasn't
/// waiting got its install; `targets` says how each one went.
struct UpdateAppEndRecord: Decodable, Equatable {
    let app: String
    let ok: Bool
    let stopped: Bool
    let done: [String]
    let left: [String]
    let error: String?
    var targets: [Target] = []

    /// One device's result, in plan order. One a Stop kept the install from
    /// has `ok` false, no `error`, and `left` `["install"]`. One `waiting`
    /// (its device went away during the run, review part 2, #9) has `ok`
    /// false, the waiting sentence as `error`, and `left` `["install"]`, and
    /// doesn't make its app not ok; false when left out.
    struct Target: Decodable, Equatable, Identifiable {
        let udid: String
        let deviceName: String?
        let ok: Bool
        let error: String?
        let left: [String]
        var waiting = false

        var id: String { udid }

        enum CodingKeys: String, CodingKey {
            case udid, ok, error, left, waiting
            case deviceName = "device_name"
        }

        init(udid: String, deviceName: String?, ok: Bool, error: String?, left: [String], waiting: Bool = false) {
            self.udid = udid
            self.deviceName = deviceName
            self.ok = ok
            self.error = error
            self.left = left
            self.waiting = waiting
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            udid = try c.decode(String.self, forKey: .udid)
            deviceName = try c.decodeIfPresent(String.self, forKey: .deviceName)
            ok = try c.decode(Bool.self, forKey: .ok)
            error = try c.decodeIfPresent(String.self, forKey: .error)
            left = try c.decode([String].self, forKey: .left)
            waiting = try c.decodeIfPresent(Bool.self, forKey: .waiting) ?? false
        }
    }

    enum CodingKeys: String, CodingKey {
        case app, ok, stopped, done, left, error, targets
    }
}

extension UpdateAppEndRecord {
    /// `targets` may be missing, and reads as none: a Stop's re-armed kill
    /// and a quit's wait key off this event (`RunStopper`), and must not
    /// hang on a list a CLI left out. In an extension, so the memberwise
    /// initializer stays.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        app = try c.decode(String.self, forKey: .app)
        ok = try c.decode(Bool.self, forKey: .ok)
        stopped = try c.decode(Bool.self, forKey: .stopped)
        done = try c.decode([String].self, forKey: .done)
        left = try c.decode([String].self, forKey: .left)
        error = try c.decodeIfPresent(String.self, forKey: .error)
        targets = try c.decodeIfPresent([Target].self, forKey: .targets) ?? []
    }
}

/// `update-end`: the run's summary.
struct UpdateEndRecord: Decodable, Equatable {
    let ok: Bool
    let stopped: Bool
    let updated: [String]
    let failed: [String]
    let blocked: [String]
    let notStarted: [String]
    /// How many devices weren't connected, across the run (none when the
    /// CLI leaves it out).
    let waiting: Int

    enum CodingKeys: String, CodingKey {
        case ok, stopped, updated, failed, blocked, waiting
        case notStarted = "not_started"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = try c.decode(Bool.self, forKey: .ok)
        stopped = try c.decode(Bool.self, forKey: .stopped)
        updated = try c.decode([String].self, forKey: .updated)
        failed = try c.decode([String].self, forKey: .failed)
        blocked = try c.decode([String].self, forKey: .blocked)
        notStarted = try c.decode([String].self, forKey: .notStarted)
        waiting = try c.decodeIfPresent(Int.self, forKey: .waiting) ?? 0
    }
}
