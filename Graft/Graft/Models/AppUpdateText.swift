import Foundation

/// Where one app of a running update is (`AppUpdatesStore.Row.phase`).
enum UpdatePhase: Equatable {
    case waiting
    case updatingTweaks
    case getting(String?)
    case building
    case installing
    case done
    case failed
    case notStarted
    case stopped
    /// The plan blocked it ("Developer Mode is off on Example iPad"): not a
    /// failure.
    case blocked(String)
    /// A device's install the run skipped, as its device isn't connected:
    /// the CLI's sentence ("iPad mini isn't connected: it'll update when it
    /// is"). Not a failure either.
    case skipped(String)
    /// The run ended mid-step with no word on it (a Force Stop, or a CLI
    /// that died): "Didn't finish" (review part 2, #4).
    case unfinished
}

/// What the app says about an app's update (spec 4.2–4.4): the sidebar's one
/// indicator, the pane's line and button, Update All, and the confirmation's
/// and the progress sheet's rows. Pure functions of Plan A's records, so
/// every state is checked without a view (`AppUpdateTextTests`).
enum AppUpdateText {
    // MARK: The sidebar

    /// The one mark a sidebar row carries (never both).
    enum Indicator: Equatable {
        /// "Update": the plan has something Update can do now, a stale
        /// delivery included (that is the Install Update case), on a device
        /// that's there (`isActionable`).
        case update
        /// Today's delivery dot: for an app never delivered, which Update
        /// leaves to Build & Install, and whenever no plan has been read.
        case deliveryDot
    }

    static let badge = "Update"
    /// The sidebar's mark is a glyph (`Symbols.updateAvailable`), not a text
    /// chip: the 260 pt row has no room for words (`AppSidebarRow`, review
    /// S8). This is what VoiceOver says for it.
    static let badgeLabel = "Update available"
    /// The quick plan by hand, which the pane, the sidebar and Settings offer,
    /// with the switch off too (spec §11.4).
    static let checkNow = "Check for updates now"

    static func indicator(plan: UpdatePlanApp?, delivery: DeliveryStateRecord?) -> Indicator? {
        if let plan {
            if plan.isActionable { return .update }
            // Work that waits for a device that isn't connected, or isn't
            // ready, raises nothing, the stale dot included: a device that
            // was sold or reset would keep it there for good (spec §11.3,
            // devices that are gone). The pane says what waits.
            if plan.hasUpdate { return nil }
        }
        if let delivery, delivery.kind.needsAttention { return .deliveryDot }
        return nil
    }

    /// The mark's tooltip: what the update is, and on which devices it
    /// installs.
    static func indicatorHelp(_ plan: UpdatePlanApp?) -> String {
        guard let plan, let headline = paneLine(plan)?.headline else { return badgeLabel }
        return ([headline] + [installsOn(plan)].compactMap { $0 }).joined(separator: "\n")
    }

    /// "Installs on iPhone 15 Pro and iPad mini": where a new build goes.
    /// Nil when only installs are due, which the headline names already.
    static func installsOn(_ plan: UpdatePlanApp) -> String? {
        guard plan.changesBuild, !plan.installTargets.isEmpty else { return nil }
        return "Installs on " + devices(plan.installTargets)
    }

    /// The devices' names, as a list.
    static func devices(_ targets: [UpdatePlanApp.Target]) -> String {
        list(targets.map { $0.deviceName ?? "the device" })
    }

    // MARK: The app pane

    struct PaneLine: Equatable {
        /// "Update available: App Store 2026.47.0 · 1 tweak"; nil when there
        /// is only a note to make.
        let headline: String?
        /// A newer App Store version this Mac can't get, or was refused; a
        /// device that isn't ready; what a quick plan remembers.
        let notes: [String]
        let action: Action?
        /// "Installs on iPhone 15 Pro and iPad mini": where a new build goes
        /// (spec §11.3). Nil when only installs are due: the headline names
        /// those devices.
        var installsOn: String? = nil
        /// Each device the update waits for, as it isn't connected, with
        /// its Stop updating.
        var waiting: [Waiting] = []
        /// The installs offered for the person to press, never run by
        /// Update (spec §11.1): one Install Update per device it's offered
        /// on, each a Build & Install to that device.
        var offered: [Offered] = []

        enum Action: Equatable {
            /// Update & Install, Update Tweaks & Install or Install Update:
            /// the confirmation, then `graft --update`.
            case button(String)

            var title: String {
                switch self {
                case let .button(title): return title
                }
            }
        }

        /// An install offered on one device (spec §11.1): its press picks
        /// that device for the next Build & Install and starts it, so it
        /// installs where the line says, never on the bar's device.
        struct Offered: Equatable, Identifiable {
            let udid: String
            let deviceName: String?
            /// "Install Update", or with several, "Install Update on iPad
            /// mini".
            let title: String
            /// What it does (`offeredHelp`).
            let help: String
            var id: String { udid }
        }

        /// A device the update waits for: the CLI's sentence ("iPad mini
        /// isn't connected: it'll update when it is"), and Stop updating
        /// for it (spec §11.3, devices that are gone).
        struct Waiting: Equatable, Identifiable {
            let udid: String
            let deviceName: String?
            let sentence: String
            /// "Stop updating Example+ on iPad mini".
            let stopTitle: String
            var id: String { udid }
        }
    }

    /// The pane's line for `plan`. `title`: the app's name as the sidebar
    /// shows it, for Stop updating's words.
    static func paneLine(_ plan: UpdatePlanApp?, title: String? = nil) -> PaneLine? {
        guard let plan else { return nil }
        var notes: [String] = []
        if !plan.getsFromStore, let to = plan.store.to, let reason = plan.store.skippedReason {
            if reason == CLIContract.Update.needsAppleSilicon {
                notes.append("App Store has \(to), \(reason)")
            } else if reason.hasPrefix(CLIContract.Update.refusedPrefix) {
                notes.append(reason)
            }
        }
        // A deliberate Re-install of an older build, or an install that
        // couldn't be found on the device (spec §11.1): no badge, and its own
        // line, with an Install Update per device it's offered on, each a
        // Build & Install to that device. With several devices, it says
        // which.
        if plan.manualInstall, !plan.hasUpdate, let offered = offeredInstalls(plan) {
            return PaneLine(headline: offered.headline, notes: offered.notes + notes, action: nil,
                            offered: offered.buttons)
        }
        guard plan.hasUpdate else {
            return notes.isEmpty ? nil : PaneLine(headline: nil, notes: notes, action: nil)
        }
        // A device block is advisory (review S3): the plan that found it may
        // be the quick one, which only guesses at the phone. Update stays
        // offered, and the confirmation's full plan decides.
        let blocked = plan.blockedTargets.compactMap(\.blockedReason)
        notes += blocked
        if blocked.isEmpty, plan.waitingTargets.isEmpty, let reason = plan.blockedReason { notes.append(reason) }
        // What a quick plan remembers about each device ("Developer Mode was
        // off…"): information, never a block.
        for note in [plan.readinessNote] + plan.targets.map(\.readinessNote) {
            if let note, !notes.contains(note) { notes.append(note) }
        }
        var parts: [String] = []
        if plan.getsFromStore, let to = plan.store.to { parts.append("App Store \(to)") }
        let tweaks = plan.tweakUpdates.count
        if tweaks > 0 { parts.append(tweaks == 1 ? "1 tweak" : "\(tweaks) tweaks") }
        if parts.isEmpty {
            let due = [plan.installTargets, plan.waitingTargets, plan.blockedTargets].first { !$0.isEmpty } ?? []
            parts.append("the latest build isn't on \(due.isEmpty ? "the device" : devices(due)) yet")
        }
        let name = title ?? plan.displayName ?? plan.app
        let waiting = plan.waitingTargets.map { target in
            PaneLine.Waiting(udid: target.udid, deviceName: target.deviceName, sentence: target.waiting ?? "",
                             stopTitle: stopUpdatingTitle(app: name, device: target.deviceName))
        }
        // Only devices that aren't connected need it: nothing could run, so
        // no button. A blocked device keeps it (advisory, above).
        let offersUpdate = !plan.installTargets.isEmpty || !plan.blockedTargets.isEmpty
        return PaneLine(headline: "Update available: " + parts.joined(separator: " · "), notes: notes,
                        action: offersUpdate ? .button(buttonTitle(plan)) : nil,
                        installsOn: installsOn(plan), waiting: waiting)
    }

    /// The offered installs: each reason, once, with the devices it's about
    /// when the app is on several (the first is the headline, the rest
    /// notes), and a button per device, named for it when there are
    /// several.
    private static func offeredInstalls(_ plan: UpdatePlanApp)
        -> (headline: String, notes: [String], buttons: [PaneLine.Offered])? {
        let targets = plan.targets.filter(\.manualInstall)
        var reasons: [String] = []
        for target in targets {
            if let reason = target.installSkippedReason, !reasons.contains(reason) { reasons.append(reason) }
        }
        if reasons.isEmpty, let reason = plan.installSkippedReason { reasons.append(reason) }
        let lines = reasons.map { reason in
            let about = targets.filter { $0.installSkippedReason == reason }
            guard plan.targets.count > 1, !about.isEmpty else { return sentence(reason) }
            return "\(sentence(reason)) (\(devices(about)))"
        }
        guard let headline = lines.first else { return nil }
        let buttons = targets.map { target in
            PaneLine.Offered(udid: target.udid, deviceName: target.deviceName,
                             title: targets.count > 1 ? "Install Update on \(target.deviceName ?? "the device")"
                                 : "Install Update",
                             help: offeredHelp(device: target.deviceName))
        }
        return (headline, Array(lines.dropFirst()), buttons)
    }

    /// An offered install's tooltip: a Build & Install to its device, which
    /// it picks for that build (spec §11.1, §11.5).
    static func offeredHelp(device: String?) -> String {
        let picks = device.map { "Graft picks \($0)" } ?? "Graft picks it"
        return "Build & Install on \(device ?? "the device"): \(picks) for that build. "
            + "Update doesn't install it on its own."
    }

    /// "Stop updating Example+ on iPad mini": the waiting device's action,
    /// on the pane's menu and the confirmation's row.
    static func stopUpdatingTitle(app: String, device: String?) -> String {
        "Stop updating \(app) on \(device ?? "this device")"
    }

    /// The name of the menu that holds a waiting device's Stop updating.
    static func waitingMenuName(device: String?) -> String {
        "Options for \(device ?? "this device")"
    }

    /// The pane's Update button's tooltip (review S6): where it installs,
    /// and that the device picked for the next Build & Install doesn't apply.
    static func buttonHelp(_ plan: UpdatePlanApp) -> String {
        let due = plan.installTargets.isEmpty ? plan.blockedTargets : plan.installTargets
        let via = !due.isEmpty && due.allSatisfy(\.deliversToLiveContainer) ? " via LiveContainer" : ""
        return "Installs on \(due.isEmpty ? "each device" : devices(due))\(via): each device it's on that needs it. "
            + "The device picked for the next Build & Install doesn't apply to Update."
    }

    /// `text` with its first letter capitalized, for a reason the CLI words
    /// to follow a colon.
    static func sentence(_ text: String) -> String {
        text.prefix(1).uppercased() + text.dropFirst()
    }

    static func buttonTitle(_ plan: UpdatePlanApp) -> String {
        if plan.getsFromStore { return "Update & Install" }
        if !plan.tweakUpdates.isEmpty { return "Update Tweaks & Install" }
        return "Install Update"
    }

    // MARK: Update All

    /// The apps Update All offers, in sidebar order: every app Update can do
    /// something for now (`isActionable`), as the sidebar marks them. Work
    /// that waits for a device isn't counted (spec §11.3).
    static func offered(_ plans: [String: UpdatePlanApp], order: [String]) -> [String] {
        order.filter { plans[$0]?.isActionable == true }
    }

    static func updateAllTitle(count: Int) -> String { "Update All (\(count))" }

    // MARK: The confirmation

    static let checking = "Checking each app and its devices…"

    /// What a LiveContainer install leaves for the person to do.
    static func importNote(device: String?) -> String {
        "You'll import it in LiveContainer on \(device ?? "the device")"
    }

    struct ConfirmRow: Equatable, Identifiable {
        let app: String
        let title: String
        /// The version and each tweak.
        let changes: [String]
        /// "also used by …" for each shared tweak.
        let alsoUsed: [String]
        /// Steps that won't run, with why.
        let skipped: [String]
        /// A line per device it installs on, waits for, or is blocked on
        /// (spec §11.3), in the plan's order.
        var targets: [TargetLine] = []
        /// The app's own block, said only when no device says why.
        let blocked: String?
        /// What the plan says about the phone, for information
        /// (`readinessNote`).
        var readinessNote: String? = nil
        var id: String { app }
    }

    /// One device of a confirmation's row: "Example+ → iPhone 15 Pro via
    /// LiveContainer" for the first, "→ iPad mini via LiveContainer" after,
    /// and what happens there.
    struct TargetLine: Equatable, Identifiable {
        let udid: String
        let line: String
        let status: Status
        /// What a quick plan remembers about this device.
        var note: String? = nil
        var id: String { udid }

        enum Status: Equatable {
            /// It installs here, with LiveContainer's import note.
            case installs(importNote: String?)
            /// It isn't connected: the CLI's sentence, and Stop updating.
            case waiting(String, stopTitle: String)
            /// It's connected but not ready: why.
            case blocked(String)
        }
    }

    /// One row per app with something to update, ready or not.
    static func confirmRows(_ plan: UpdatePlanRecord, names: [String: String]) -> [ConfirmRow] {
        plan.apps.filter(\.hasUpdate).map { app in
            var changes: [String] = []
            if app.getsFromStore, let to = app.store.to {
                changes.append("App Store \(app.store.from ?? "nothing staged") → \(to)")
            }
            var alsoUsed: [String] = []
            for tweak in app.tweakUpdates {
                changes.append("\(tweak.name ?? tweak.filename) \(tweak.from ?? "?") → \(tweak.to ?? "?")")
                if let line = alsoUsedLine(tweak, names: names) { alsoUsed.append(line) }
            }
            var skipped: [String] = []
            if !app.getsFromStore, let reason = app.store.skippedReason {
                // A refusal is a sentence that names its version already.
                if reason.hasPrefix(CLIContract.Update.refusedPrefix) {
                    skipped.append(reason)
                } else {
                    skipped.append(app.store.to.map { "App Store \($0): \(reason)" } ?? "App Store: \(reason)")
                }
            }
            for tweak in app.tweaks where tweak.unchecked {
                skipped.append("\(tweak.name ?? tweak.filename): couldn't check")
            }
            let title = app.displayName ?? names[app.app] ?? app.app
            let targets = targetLines(app, title: title)
            return ConfirmRow(
                app: app.app, title: title, changes: changes, alsoUsed: alsoUsed, skipped: skipped,
                targets: targets, blocked: targets.isEmpty ? app.blockedReason : nil,
                readinessNote: app.readinessNote
            )
        }
    }

    /// The devices of one app's row: each it installs on, waits for or is
    /// blocked on, in the plan's order. A device that's current and has
    /// nothing coming isn't listed.
    static func targetLines(_ app: UpdatePlanApp, title: String) -> [TargetLine] {
        let listed = app.targets.filter { $0.isReady ? $0.install : true }
        return listed.enumerated().map { index, target in
            let status: TargetLine.Status
            if let waiting = target.waiting {
                status = .waiting(waiting, stopTitle: stopUpdatingTitle(app: title, device: target.deviceName))
            } else if let blocked = target.blockedReason {
                status = .blocked(blocked)
            } else {
                status = .installs(importNote: target.deliversToLiveContainer ? importNote(device: target.deviceName) : nil)
            }
            let via = target.deliversToLiveContainer ? " via LiveContainer" : ""
            let line = (index == 0 ? "\(title) " : "") + "→ \(target.deviceName ?? "the device")\(via)"
            return TargetLine(udid: target.udid, line: line, status: status, note: target.readinessNote)
        }
    }

    static func alsoUsedLine(_ tweak: UpdatePlanApp.TweakChange, names: [String: String]) -> String? {
        guard !tweak.alsoAffects.isEmpty else { return nil }
        let who = list(tweak.alsoAffects.map { names[$0] ?? $0 })
        let need = tweak.alsoAffects.count == 1 ? "it will need an install" : "they will need an install"
        return "also used by \(who): \(need) afterward"
    }

    /// Every app outside the run that shares a tweak it updates, once each.
    static func affected(_ plan: UpdatePlanRecord) -> [String] {
        var seen: [String] = []
        for app in plan.apps where app.isActionable {
            for tweak in app.tweakUpdates {
                for name in tweak.alsoAffects where !seen.contains(name) { seen.append(name) }
            }
        }
        return seen
    }

    static func alsoInstallTitle(_ names: [String]) -> String { "Also install \(list(names)) now" }

    /// The confirmation when its full plan finds nothing to update (Try
    /// Again after it was fixed meanwhile, say).
    static let nothingLeft = "Nothing left to update"
    static let done = "Done"

    /// Update, Update 3 Apps, or Done when nothing can run.
    static func confirmButton(count: Int) -> String {
        switch count {
        case 0: return done
        case 1: return "Update"
        default: return "Update \(count) Apps"
        }
    }

    // MARK: The progress sheet

    static func phaseText(_ phase: UpdatePhase) -> String {
        switch phase {
        case .waiting: return "Waiting"
        case .updatingTweaks: return "Updating tweaks"
        case let .getting(version): return version.map { "Getting \($0)" } ?? "Getting from the App Store"
        case .building: return "Building"
        case .installing: return "Installing"
        case .done: return "Done"
        case .failed: return "Failed"
        case .notStarted: return "Not started"
        case .stopped: return "Stopped"
        case .unfinished: return "Didn't finish"
        case let .blocked(reason): return "Blocked: \(reason)"
        case let .skipped(sentence): return sentence
        }
    }

    /// What the sheet says once Stop is pressed.
    static func stoppingText(_ stopping: UpdateStoppingRecord?) -> String {
        guard let stopping else { return "Stopping…" }
        if stopping.step == CLIContract.Update.stepTweaks { return "Finishing the tweak update, then stopping…" }
        return "Finishing the install, then stopping…"
    }

    /// What the quit says while it waits for the step a Stop deferred to
    /// (spec 4.5, `TerminationHold.finishing`). `deviceName`: where the
    /// install goes, when the plan said.
    static func quittingText(_ stopping: UpdateStoppingRecord, deviceName: String?) -> String {
        if stopping.step == CLIContract.Update.stepTweaks { return "Finishing the tweak update, then quitting…" }
        return "Finishing the install\(deviceName.map { " on \($0)" } ?? ""), then quitting…"
    }

    static func phase(for end: UpdateAppEndRecord) -> UpdatePhase {
        if end.ok { return .done }
        if end.stopped { return .stopped }
        if end.error == CLIContract.Update.notStarted { return .notStarted }
        return .failed
    }

    static func leftText(_ left: [String]) -> String? {
        let words = left.map { step -> String in
            switch step {
            case CLIContract.Update.stepTweaks: return "its tweak updates"
            case CLIContract.Update.stepGet: return "the App Store download"
            case CLIContract.Update.stepBuild: return "the build and install"
            case CLIContract.Update.stepInstall: return "the install"
            default: return step
            }
        }
        return words.isEmpty ? nil : "Left to do: \(list(words))."
    }

    /// An app's line once its update has ended. `blocked`: the plan's reason
    /// it couldn't run, which is said in place of a failure.
    static func resultLine(
        _ end: UpdateAppEndRecord, deviceName: String?, liveContainer: Bool, blocked: String? = nil
    ) -> String {
        if end.ok {
            return liveContainer ? "Open LiveContainer on \(deviceName ?? "the device") to import it" : "Installed"
        }
        if let blocked { return phaseText(.blocked(blocked)) }
        let why: String? = end.stopped || end.error == CLIContract.Update.notStarted ? nil : end.error
        // A sentence of its own before "Left to do", whatever the CLI ended it with.
        let said = why.map { $0.hasSuffix(".") ? $0 : $0 + "." }
        return [said, leftText(end.left)].compactMap { $0 }.joined(separator: " ")
    }

    /// An app's own line once its update has ended, beside its devices'
    /// (spec §11.3): what failed that no device says, a get or a build, and
    /// what's left of it. Nil when every device says it all.
    static func appResultLine(_ end: UpdateAppEndRecord) -> String? {
        guard !end.ok else { return nil }
        let why: String? = end.stopped || end.error == CLIContract.Update.notStarted ? nil : end.error
        let own = why.flatMap { error in end.targets.contains { $0.error == error } ? nil : error }
        let said = own.map { $0.hasSuffix(".") ? $0 : $0 + "." }
        let left = leftText(end.left.filter { $0 != CLIContract.Update.stepInstall })
        let line = [said, left].compactMap { $0 }.joined(separator: " ")
        return line.isEmpty ? nil : line
    }

    /// A device's line once its install has ended: where to import it, or
    /// what failed and what's left.
    static func targetResultLine(_ end: UpdateAppEndRecord.Target, deviceName: String?, liveContainer: Bool) -> String? {
        // Waiting for its device: the phase says the sentence already.
        if end.waiting { return nil }
        if end.ok {
            return liveContainer ? "Open LiveContainer on \(deviceName ?? "the device") to import it" : "Installed"
        }
        let said = end.error.map { $0.hasSuffix(".") ? $0 : $0 + "." }
        let line = [said, leftText(end.left)].compactMap { $0 }.joined(separator: " ")
        return line.isEmpty ? nil : line
    }

    /// A device's phase from its result: a device not reached (no error,
    /// its install left: a Stop, or a failure before it) wasn't started. One
    /// that went away during the run waits for it (review part 2, #9): the
    /// CLI's sentence, with no Try Again, as one not connected from the
    /// start.
    static func targetPhase(_ end: UpdateAppEndRecord.Target) -> UpdatePhase {
        if end.waiting { return .skipped(end.error ?? waitingFallback(end.deviceName)) }
        if end.ok { return .done }
        if end.error == nil, end.left.contains(CLIContract.Update.stepInstall) { return .notStarted }
        return .failed
    }

    /// The waiting sentence, for a CLI that sent none.
    static func waitingFallback(_ deviceName: String?) -> String {
        "\(deviceName ?? "The device") isn't connected: it'll update when it is"
    }

    /// A device's row under its app in the progress sheet.
    static func deviceLabel(_ deviceName: String?) -> String { "→ \(deviceName ?? "the device")" }

    /// The name of a device's own Try Again.
    static func tryAgainName(app: String, device: String?) -> String {
        "Try again: \(app) on \(device ?? "the device")"
    }

    /// The transcript's line for a step, with its device for an install:
    /// "→ example: install on Example iPad". The shared tweak step has no
    /// app, and is named once: "→ tweaks: 1 tweak", not "→ tweaks: tweaks 1
    /// tweak".
    static func logLines(_ step: UpdateStepRecord) -> [LogLine] {
        let what = step.step + (step.deviceName.map { " on \($0)" } ?? "")
        let start: String
        let skipped: String
        if let app = step.app {
            start = "→ \(app): \(what)" + (step.detail.map { " \($0)" } ?? "")
            skipped = "  \(app): \(what) skipped"
        } else {
            start = "→ \(what)" + (step.detail.map { ": \($0)" } ?? "")
            skipped = "  \(what) skipped"
        }
        switch step.status {
        case CLIContract.Update.statusStart:
            return [LogLine(kind: .arrow, text: start)]
        case CLIContract.Update.statusSkipped:
            return [LogLine(kind: .dim, text: skipped + (step.detail.map { ": \($0)" } ?? ""))]
        default:
            return []
        }
    }

    static func list(_ items: [String]) -> String {
        switch items.count {
        case 0: return ""
        case 1: return items[0]
        case 2: return "\(items[0]) and \(items[1])"
        default: return items.dropLast().joined(separator: ", ") + ", and " + items[items.count - 1]
        }
    }
}
