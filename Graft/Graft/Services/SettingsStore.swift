import Foundation
import Observation

private typealias Flag = CLIContract.Flag

@MainActor
@Observable
final class SettingsStore {
    private(set) var checks: [PreflightCheck] = []
    /// The editable settings as the CLI currently has them, or nil before the
    /// first read. The pane seeds its fields from this and diffs against it,
    /// so it can send only what the user actually changed.
    private(set) var settings: SettingsRecord?

    /// Where the CLI says this config keeps everything, for Settings → Paths.
    ///
    /// Held apart from `settings` because only `--settings` reports it: a save
    /// replaces `settings` with `--set-settings`' echo, which carries no
    /// paths, and the section would blank on every save. Nothing a save writes
    /// moves a directory, so the last report stays true. Nil until the first
    /// read, and after it against a CLI too old to report them.
    private(set) var paths: SettingsPaths?

    /// Whether `paths` are the defaults rather than this config's own: the
    /// read that reported them could not parse config.yaml (C-SETTINGS), so
    /// a `cache_dir` or `tweaks_dir` it sets was never seen. Kept with `paths`,
    /// which outlives a save for the reason given there.
    private(set) var pathsAreDefaults = false

    /// Why the last `--settings` could not read config.yaml, or nil when it
    /// could — or when the CLI is too old to say (C-SETTINGS).
    ///
    /// Read off `settings` rather than held apart: a save replaces `settings`,
    /// and a save can only land on a config that parses, so the two cannot
    /// disagree.
    var parseError: String? { settings?.parseError }

    /// The pending edit, and whether a save is in flight.
    ///
    /// On the store rather than in `SettingsView`'s `@State` for the reason
    /// `ConfigStore` holds `buildFailures`: `ContentView`'s `switch selection`
    /// destroys the pane on every section change, so a half-typed certificate
    /// path was discarded the moment the user clicked Devices to check a UDID
    /// — silently, with no prompt. The store is owned by ContentView and
    /// outlives the switch.
    var draft = SettingsDraft()
    private(set) var isSaving = false

    /// Whether anything is running that a second `--check` would race.
    ///
    /// The Re-run button was gated on `isRunning`, which only `runChecks`
    /// sets, so it stayed live during a save — and `save()` ends by running
    /// the preflight itself. Two concurrent `--check` processes then raced,
    /// and whichever finished last won `checks`, so the pane could settle on
    /// the *pre-save* scan and show a red row for a path just fixed. The App
    /// Store Connect key's Save and test and Remove end with a scan too, for
    /// the same reason `save()` does, so they hold it as well.
    var isBusy: Bool { isRunning || isSaving || isSavingPortal }
    private(set) var isRunning: Bool = false
    /// The last Re-run found the login shell asked too recently to ask again,
    /// and scanned with its last answer (`ShellEnvironment.askedTooRecentlyNote`).
    private(set) var shellAskedTooRecently = false
    var lastError: String?

    /// The pill's two counts. An `info` row is in neither: a free Apple ID's
    /// account row is `ok` on the wire, and counting it would call "you have
    /// a free account" a check that passed. So the fail count is the failed
    /// rows themselves, not "everything that is not OK", which would count it
    /// there instead.
    var okCount: Int { checks.lazy.filter { $0.ok && !$0.isInfo }.count }
    var failCount: Int { checks.lazy.filter { !$0.ok }.count }

    /// The failed rows that are a tool to install, with its command — what
    /// the setup screen's step 3 and the window's notice list. A failed row
    /// with nothing to install (no phone plugged in, a certificate about to
    /// expire) is not one: it has no command, and Settings is where it is
    /// read.
    var missingTools: [MissingTool] { MissingTool.all(in: checks) }

    /// Whether a `--check` has finished at least once, whatever it found.
    /// `checks.isEmpty` cannot say: it is equally "not run yet" and "the run
    /// failed", and `ContentView` must not run a second launch-time scan for
    /// the first case only.
    private(set) var hasChecked = false

    /// The account `--check` last reported on its `preflight-end`, or nil
    /// before a scan has finished (and from a CLI that predates free mode).
    ///
    /// Not what the pane renders: every surface reads `ConfigStore.account`,
    /// so the app has one answer. This is a second reading, from whichever
    /// scan ran last — the launch scan, a Re-run, the end of a save, a
    /// password set, an App Store Connect key saved or removed, or a return
    /// to the window while tools are missing or once the app list reports
    /// an account other than this one. (Not
    /// from entering Settings: that scans only while the pane has no rows,
    /// which after the launch scan is never.) When it disagrees with the app
    /// list's, config.yaml changed account under the app, and `ContentView`
    /// reloads the list.
    ///
    /// The first reading — and the one that notices an account change made
    /// in Terminal (`graft --init`) — is the app list itself, which
    /// `ContentView` re-reads every time the window comes back to the front.
    private(set) var account: AccountMode?

    let runner: CLIRunner

    init(runner: CLIRunner) {
        self.runner = runner
    }

    /// Not `runner.run(_:failing:pick:)`: this call carries a secret on stdin,
    /// emits no JSON-Lines events, and reports its refusals on *stdout* — the
    /// keychain helper writes there, not to stderr — so it keeps its own body.
    func setP12Password(_ password: String) async throws {
        let result = try await runner.runOnce(arguments: [Flag.setP12Password], stdin: password)
        guard result.exitCode == 0 else {
            throw CLIError.exited(
                doing: "Set .p12 password",
                code: result.exitCode,
                stderr: result.stderr.isEmpty ? result.stdout : result.stderr
            )
        }
    }

    /// Also not `runner.run(...)`: for `--check`, one non-zero exit is data.
    /// Exit 3 means "some checks failed", which is exactly what this pane
    /// exists to display; 0 means none did. Any other exit is the scan not
    /// happening at all — 2 is a config.yaml that will not load — and throws.
    ///
    /// The test used to be "non-zero *and* stdout empty", and stdout is not
    /// empty when the CLI has a startup notice to give (C-NOTICE: a `log`
    /// line written before the verb runs). A broken config plus a renamed
    /// environment variable then reached the pane as an empty check list with
    /// no error at all.
    ///
    /// `askingShellAgain` is Preflight's Re-run, pressed: the login shell is
    /// asked for its PATH again first, so a tool installed into a folder just
    /// added to it is found. Only then: the launch-time answer serves every
    /// other scan (whole-diff review, app-1; `ShellEnvironment`).
    func runChecks(argsOverride: [String]? = nil, askingShellAgain: Bool = false) async throws {
        // Reentrancy guard: two overlapping scans race on `checks`, and the
        // first to finish would clear `isRunning` while the other is still
        // going, stopping the spinner over a live scan.
        guard !isRunning else { return }
        shellAskedTooRecently = askingShellAgain && !runner.refreshEnvironment()
        let args = argsOverride ?? [Flag.json, Flag.check]
        isRunning = true
        let result: CLIRunner.RunResult
        do {
            result = try await runner.runOnce(arguments: args)
        } catch {
            isRunning = false
            if !Task.isCancelled { hasChecked = true }
            throw error
        }
        isRunning = false
        if !Task.isCancelled { hasChecked = true }
        guard result.exitCode == 0 || result.exitCode == 3 else {
            throw CLIError.exited(
                doing: "Preflight checks", code: result.exitCode, stderr: result.stderr
            )
        }
        let events = result.stdout.buildEvents()
        self.checks = events.compactMap { event in
            if case let .preflightCheck(check) = event { return check }
            return nil
        }
        for case let .preflightEnd(_, _, reported) in events {
            account = reported
        }
    }

    /// Write global settings, and refuse to report success for any the CLI
    /// didn't apply.
    ///
    /// Pass only what changed. The reconciliation against `changed` is the
    /// same guard `ConfigStore.updateAppOptions` applies — it exists because a
    /// silently-dropped field is indistinguishable from a saved one from the
    /// pane's side, and the user then trusts a value that is not on disk.
    ///
    /// Field names come from `CLIContract.SettingField`, not literals: they
    /// must match the CLI's config-key spellings exactly or this reports a
    /// phantom failure for a write that landed.
    @discardableResult
    func updateSettings(
        defaultDeviceUDID: String? = nil,
        archiveKeep: Int? = nil,
        signingCert: String? = nil,
        signingProfile: String? = nil,
        signingTeamId: String? = nil,
        argsOverride: [String]? = nil
    ) async throws -> (changed: [String], fresh: SettingsRecord?) {
        // One row per setting: the contract name, the flag that writes it, and
        // the value. Five copies of `if let x { args += [Flag.x, x]; request(.x) }`
        // is the shape where a paste keeps the wrong `request(...)` and the pane
        // reports a phantom failure for a field that landed.
        let sending: [(field: CLIContract.SettingField, flag: String, value: String?)] = [
            (.defaultDeviceUDID, Flag.defaultDevice, defaultDeviceUDID),
            (.archiveKeep, Flag.archiveKeep, archiveKeep.map(String.init)),
            (.signingCert, Flag.signingCert, signingCert),
            (.signingProfile, Flag.signingProfile, signingProfile),
            (.signingTeamId, Flag.signingTeamId, signingTeamId),
        ]
        var args = [Flag.json, Flag.setSettings]
        var requested: Set<String> = []
        for (field, flag, value) in sending {
            guard let value else { continue }
            args += [flag, value]
            requested.insert(field.rawValue)
        }
        guard !requested.isEmpty else { return ([], nil) }

        // Every event from the one process, because `--set-settings` answers
        // both questions: which keys it changed, and what the settings now are.
        // The save used to spawn `--settings` afterward to ask the second —
        // a whole extra interpreter start (~150ms, as much as the write itself)
        // for a value the writer already had in hand.
        let events = try await runner.runEvents(argsOverride ?? args, failing: "Save settings")
        var changed: [String] = []
        var fresh: SettingsRecord?
        for event in events {
            switch event {
            case let .settingsUpdated(record): changed = record.changed
            case let .settings(record): fresh = record
            default: break
            }
        }

        try requireApplied(requested, reportedIn: changed)
        return (changed, fresh)
    }

    /// Read the settings the pane can edit.
    @discardableResult
    func loadSettings(argsOverride: [String]? = nil) async throws -> SettingsRecord? {
        let record = try await runner.run(
            argsOverride ?? [Flag.json, Flag.settings], failing: "Load settings"
        ) { event -> SettingsRecord? in
            if case let .settings(record) = event { return record }
            return nil
        }
        settings = record
        if let record, let reported = record.paths {
            paths = reported
            pathsAreDefaults = record.pathsAreDefaults
        }
        return record
    }

    /// Write the draft's edits, re-read, and refresh the preflight.
    ///
    /// Owned by the store because the pane it was in does not survive a
    /// section change, and because `isSaving` has to be visible to the
    /// Preflight section's Re-run button — which would otherwise race the
    /// scan this ends with.
    func saveDraft() async {
        // Nothing to save into: `--set-settings` refuses a config it cannot
        // parse (exit 2), and the pane says why instead of offering Save.
        guard draft.hasChanges, !isSaving, parseError == nil else { return }
        isSaving = true
        defer { isSaving = false }
        lastError = nil

        let fresh: SettingsRecord?
        do {
            fresh = try await updateSettings(
                defaultDeviceUDID: draft.changed(\.defaultDeviceUDID),
                archiveKeep: draft.changed(\.archiveKeep),
                signingCert: draft.changed(\.signingCert),
                signingProfile: draft.changed(\.signingProfile),
                signingTeamId: draft.changed(\.signingTeamId)
            ).fresh
        } catch {
            lastError = error.localizedDescription
            return
        }

        // Past the write, so nothing below may report the save as failed —
        // `CLIStore.mutate`'s rule: the write already landed on disk, so
        // rethrowing here would report failure for a change that succeeded.
        if let fresh {
            settings = fresh
            resetDraft()
        } else {
            // An installed graft too old to report the post-write state.
            // Costs the extra process rather than reseeding the draft from
            // values the save just replaced.
            do {
                _ = try await loadSettings()
                resetDraft()
            } catch {
                // Deliberately *not* reseeding. `settings` still holds the
                // pre-save record, so resetting here would put the old values
                // back into fields the user successfully saved — the write is
                // on disk and the pane would be showing that it isn't, which is
                // worse than showing a draft that still reads as unsaved.
                lastError = "Saved, but couldn't re-read your settings: "
                    + error.localizedDescription
            }
        }
        // The signing paths are exactly what preflight checks, so a save that
        // fixes one clears its red row without being asked.
        try? await runChecks()
    }

    /// Seed the draft from the last read, unless the user is mid-edit.
    ///
    /// The background path: a re-read that landed while the pane was open must
    /// not discard what somebody was halfway through typing.
    func reseedDraftIfIdle() {
        if !draft.hasChanges { resetDraft() }
    }

    // MARK: App Store Connect key

    /// What the last Save and test (or Remove) came to, for the App Store
    /// Connect section to show under its form. Nil before one, and after a
    /// Remove that worked with nothing to warn about.
    enum PortalResult: Equatable {
        /// The key works: App Store Connect listed this many iOS devices.
        /// `warnings` are the CLI's on the way, such as an earlier
        /// `AuthKey_<id>.p8` left in the config folder that no setting uses
        /// now (fix-check #7/#27).
        case connected(devices: Int, warnings: [String] = [])
        /// The key is gone, and the CLI warned on the way out: which sideload
        /// apps with `auto_profile` now fail to build without one.
        case removed(warnings: [String])
        /// The CLI's message and hint, on one line.
        case failed(String)
    }

    private(set) var portalResult: PortalResult?
    private(set) var isSavingPortal = false

    /// The section's three fields, held here for `draft`'s reason: the pane
    /// is torn down on every section change, and a half-typed Issuer ID
    /// should survive a trip to the Devices tab. Cleared once a key saves.
    struct PortalForm: Equatable {
        var keyId = ""
        var issuerId = ""
        var keyFile = ""

        var isReady: Bool { SettingsStore.portalFormReady(keyId: keyId, issuerId: issuerId, keyFile: keyFile) }

        /// The Key ID a downloaded key's filename carries (`AuthKey_<id>.p8`),
        /// for the field to start from when the user picks the file first.
        static func keyId(fromFilename name: String) -> String? {
            guard name.hasPrefix("AuthKey_"), name.hasSuffix(".p8") else { return nil }
            let id = String(name.dropFirst("AuthKey_".count).dropLast(".p8".count))
            return id.isEmpty ? nil : id
        }
    }

    var portalForm = PortalForm()

    /// Whether Save and test has something to send: all three fields.
    nonisolated static func portalFormReady(keyId: String, issuerId: String, keyFile: String) -> Bool {
        ![keyId, issuerId, keyFile].contains { $0.trimmingCharacters(in: .whitespaces).isEmpty }
    }

    static func setPortalArgs(keyId: String, issuerId: String, keyFile: String) -> [String] {
        [
            Flag.json, Flag.setPortal,
            Flag.ascKeyId, keyId.trimmingCharacters(in: .whitespaces),
            Flag.ascIssuerId, issuerId.trimmingCharacters(in: .whitespaces),
            Flag.ascKeyFile, keyFile,
        ]
    }

    /// Check the key against App Store Connect and, if it works, save it —
    /// `--set-portal`, which writes nothing when the check fails.
    ///
    /// Answers in `portalResult` rather than `lastError`: the section shows
    /// the outcome under the form it came from, and a refused key is the
    /// common case worth a sentence there, not an alert over the pane.
    func setPortal(keyId: String, issuerId: String, keyFile: String) async {
        guard !isSavingPortal else { return }
        isSavingPortal = true
        defer { isSavingPortal = false }
        do {
            let events = try await runner.runEvents(
                Self.setPortalArgs(keyId: keyId, issuerId: issuerId, keyFile: keyFile),
                failing: "Save the App Store Connect key"
            )
            var devices: Int?
            var fresh: SettingsRecord?
            for event in events {
                switch event {
                case let .portalSet(record): devices = record.devices
                case let .settings(record): fresh = record
                default: break
                }
            }
            guard let devices else {
                portalResult = .failed("graft didn't say whether the key was saved")
                return
            }
            portalResult = .connected(devices: devices, warnings: events.warningLines)
            portalForm = PortalForm()
            await adopt(fresh)
            // An app with `auto_profile` has a signing profile row that checks
            // for a key, so a key that works clears it without a Re-run —
            // `saveDraft`'s rule, and its `try?`: the key is saved either way.
            try? await runChecks()
        } catch {
            portalResult = .failed(error.localizedDescription)
        }
    }

    /// Remove the key from config.yaml (`--clear-portal`). The .p8 stays.
    ///
    /// The CLI warns, in a `log` event sourced `warning`, which sideload apps
    /// with `auto_profile` will no longer build; that goes to `portalResult`
    /// for the section's result line. A startup notice is not about Remove,
    /// so it is not shown there.
    func clearPortal() async {
        guard !isSavingPortal else { return }
        isSavingPortal = true
        defer { isSavingPortal = false }
        do {
            let events = try await runner.runEvents(
                [Flag.json, Flag.clearPortal], failing: "Remove the App Store Connect key"
            )
            let warnings = events.warningLines
            portalResult = warnings.isEmpty ? nil : .removed(warnings: warnings)
            let fresh = events.lazy.compactMap { event -> SettingsRecord? in
                if case let .settings(record) = event { return record }
                return nil
            }.first
            await adopt(fresh)
            // The same row, the other way: without a key it fails, and it
            // must not stay green over one that is gone.
            try? await runChecks()
        } catch {
            portalResult = .failed(error.localizedDescription)
        }
    }

    /// Take a writer's `settings` echo as the current state, keeping `paths`
    /// (which only `--settings` reports). Without an echo, read it — the
    /// write landed, and a failed re-read must not report it as failed.
    private func adopt(_ fresh: SettingsRecord?) async {
        if let fresh {
            settings = fresh
        } else {
            _ = try? await loadSettings()
        }
    }

    /// Throw the pending edit away and start again from the last read.
    ///
    /// Separate from `reseedDraftIfIdle` because the two want opposite things
    /// and sharing one method made the Revert button dead: it renders only when
    /// `hasChanges` is true, and the method it called did nothing unless
    /// `hasChanges` was false, so the one control whose entire job is
    /// discarding edits could never discard one.
    func resetDraft() {
        draft = SettingsDraft(settings)
    }
}
