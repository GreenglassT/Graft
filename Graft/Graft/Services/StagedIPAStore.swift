import Foundation
import Combine

private typealias Flag = CLIContract.Flag

/// What the IPA cache holds, keyed by bundle id.
///
/// The cache half of the device pane's join. `DeviceAppsStore` says what is
/// installed on a phone; this says which of those apps already has a staged
/// IPA — which is what lets a row offer "Add as app" with the invariant that
/// anything added from that pane can be built immediately. The cache is the
/// only place a build takes its IPA from, so an app added with nothing staged
/// cannot build until something stages one (`--provide-ipa`, `--decrypt` or
/// `--appstore-get`).
///
/// Not a field on `DeviceAppsStore` and not one on `ConfigStore`: the cache is
/// neither per-phone nor part of the config, and both of those stores are
/// already large enough that a third concern would be the one nobody expects
/// to find there.
///
/// **There is no `lastError` here** — a decision earned on its own terms, not
/// borrowed from `ArtworkStore`'s. That store's silence rests on three
/// properties of its fallback that don't hold here: the letter tile it draws
/// instead is legible *as* "no icon resolved," nothing the user could have
/// done is lost, and no action they take afterward is wasted. This store's
/// fallback — a lock and a Dump, the row's pre-feature appearance — reads
/// differently: it positively asserts "checked, nothing is staged." Getting
/// that wrong costs something real: the Add route disappears with nothing on
/// screen naming a cause, and the recovery a user reaches for is a wasted
/// minute (plus getting the app foreground-active for frida) re-dumping an
/// IPA that was already sitting on disk.
///
/// The decision holds anyway, because a red banner over the whole device pane
/// is still disproportionate to a failed *local cache listing* — when this
/// pane has a real problem it is almost always the phone, not this. And it is
/// defensible rather than merely convenient because `load()` is cheap and
/// re-run constantly (every pane appearance, every dump), and every common
/// failure — an unreadable cache directory mid-write, a subprocess that exits
/// non-zero from a momentary I/O condition — is transient and self-heals on
/// the very next call this store already makes unprompted.
///
/// One failure does not self-heal, and is named here rather than left
/// implicit: a `graft` too old to know `--staged-ipas` at all. The CLI
/// and this app deploy separately on this machine, so that skew is not a
/// one-off blip — it stands until someone re-runs `pipx upgrade`, and for as
/// long as it does, `hasLoaded` never becomes true and every row renders
/// exactly as it did before this feature shipped, with nothing anywhere
/// saying why.
///
/// A second kind used to join it: a plist field this decoder requires as a
/// string, reachable as something else. `CFBundleDisplayName` and
/// `CFBundleIdentifier` were both read off a hand-copied file's plist with a
/// bare `plist.get(...)` — `find_in_cache`'s own third match strategy exists
/// precisely to accept one without validating its shape — so a non-string
/// value (a legal plist integer, array or dict) reached this wire format
/// untouched, `StagedIPARecord`'s decode threw, and every future load was
/// refused, permanently, until that one file left the cache. `read_info_plist`
/// now coerces both, symmetrically with `version`, which already did. No
/// undecodable-record path survives that fix.
///
/// Accepted, not overlooked.
@MainActor
final class StagedIPAStore: ObservableObject {
    /// The preferred staged IPA per bundle id — the file a build would reach
    /// for, as the CLI marked it.
    @Published private(set) var byBundleId: [String: StagedIPARecord] = [:]

    /// Every staged IPA, in the order the CLI emitted them.
    ///
    /// `byBundleId` above keeps one record per bundle id — the file a build
    /// would reach for. This keeps all of them, because two versions of one app
    /// are two rows in the Cache pane and deleting the superseded one is half of
    /// what that pane is for.
    ///
    /// Two readings of one listing, not two listings: both are published from
    /// the same `load()`, atomically, so a view reading one cannot see a state
    /// the other has not caught up to.
    @Published private(set) var all: [StagedIPARecord] = []

    /// Whether a listing has ever finished.
    ///
    /// **Not `byBundleId.isEmpty`.** An empty cache is a real answer and never
    /// having asked is not, and every row below reads this rather than the
    /// dictionary's size. Conflating the two is the defect this GUI has now
    /// shipped three times; here it would offer no Add on any row and give no
    /// reason, which reads exactly like the feature being broken.
    @Published private(set) var hasLoaded = false

    private let runner: CLIRunner
    /// Holds the quit while Install to Mac… from Builds or the Cache waits
    /// for PlayCover (`installToMac`'s `holdingQuit`); a test passes its own.
    private let hold: TerminationHold

    /// Bumped on every `load()` call so a stale one can tell it lost a race.
    /// Unpublished: bookkeeping, which nothing draws.
    private var loadGeneration = 0

    init(runner: CLIRunner, hold: TerminationHold = .shared) {
        self.runner = runner
        self.hold = hold
    }

    /// The argv, as a static pure function so a test can assert on it without
    /// a subprocess — the pattern `DeviceAppsStore.loadArgs` established.
    ///
    /// No device and no `--app`: this verb takes neither, unlike the device
    /// pane's other calls — `--device-apps`, `--device-info`,
    /// `--decrypt-check` and `--decrypt` all take `--device`. The cache this
    /// reads is not scoped to any one phone.
    static func loadArgs() -> [String] {
        [Flag.json, Flag.stagedIPAs]
    }

    /// Re-read the cache. Cheap enough to call on every pane appearance and
    /// after every dump: 0.2 ms of work behind a 0.25 s process start.
    func load(argsOverride: [String]? = nil) async {
        // `load` is `@MainActor` with an `await` in the middle, so a second
        // call can start while the first is still suspended — the pane
        // re-runs this on every appearance and after every dump, and those
        // two triggers can land close enough together to overlap for real.
        // Settled by generation rather than by arrival — the same call
        // `DeviceAppsStore.checkReadiness` makes, for the same reason, not a
        // contrasting one: subprocess duration does not reliably track start
        // order there either ("the slower one is often the stale one," in
        // that method's own words), and it does not here — the earlier call
        // can just as easily finish after the later one. Last-to-*finish*-wins
        // would let a load that started just before a dump publish after one
        // that started just after it, overwriting the dump's own fresh
        // listing with the stale pre-dump snapshot: the "Add as app" that had
        // just appeared would silently disappear, with no error channel here
        // to say why.
        // A re-entrancy guard is wrong for a different reason — the one
        // `checkReadiness` rejects one for: it would drop the *second* of two
        // closely-spaced calls, which right after a dump is the one call this
        // store cannot afford to lose.
        loadGeneration += 1
        let generation = loadGeneration

        guard let events = try? await runner.runEvents(
            argsOverride ?? Self.loadArgs(), failing: "List staged IPAs"
        ) else {
            // Left standing rather than blanked, and `hasLoaded` left alone: a
            // failed refresh has learned nothing, and must neither erase the
            // listing on screen nor start claiming the cache is empty.
            return
        }

        var found: [String: StagedIPARecord] = [:]
        var emitted: [StagedIPARecord] = []
        var recordsSeen = 0
        var declaredCount: Int?
        for event in events {
            switch event {
            case let .stagedIPA(record):
                recordsSeen += 1
                emitted.append(record)
                // `preferred` decides, not arrival order. Two versions of one
                // app are both emitted, and the CLI already applied
                // `find_in_cache`'s rule to say which one a build would use.
                if record.preferred || found[record.bundleId] == nil {
                    found[record.bundleId] = record
                }
            case let .stagedIPAsEnd(count):
                declaredCount = count
            default:
                break
            }
        }
        // The sentinel is what makes this a load, and its count is now
        // checked rather than decorative: `declaredCount` stays nil until
        // `staged-ipas-end` arrives, so a run that never reached it answered
        // nothing. And once it arrives, it must equal `recordsSeen` — a raw
        // per-line tally, not `found.count` — because `found` is deduped by
        // bundle id, and two staged versions of one app collapsing to a
        // single entry there would make an ordinary healthy multi-version
        // cache fail this check on every load. `recordsSeen` is the count
        // that actually mirrors the CLI's: a `staged-ipa` line whose field
        // types this decoder rejects — a non-string `bundle_id`, the exact
        // shape `read_info_plist` used to be able to produce before every
        // plist-sourced field was coerced (see this type's own doc) — is
        // exactly the corruption `buildEvents()`'s `compactMap { try? }`
        // silently drops, at exit 0, with the sentinel itself still parsing
        // whole. Contract drift between the two sides, not a truncated
        // stream: a single sequential writer cannot corrupt line k and still
        // emit line k+1 intact, and a reader that hangs up or a pipe that
        // breaks exits non-zero here — `checkExit` throws before a byte of
        // stdout is decoded — rather than truncating silently. This equality
        // guards against the drift kind in general, whatever produces it,
        // which the sentinel's mere presence could not.
        guard let declaredCount, declaredCount == recordsSeen else { return }
        // And only published if this is still the newest call in flight —
        // see the reasoning where `loadGeneration` is bumped above.
        guard generation == loadGeneration else { return }
        byBundleId = found
        all = emitted
        hasLoaded = true
    }

    // MARK: - The two verbs the Cache pane presses

    /// What `--delete-staged` answered.
    ///
    /// Three cases and not two, because the middle one is the whole point of
    /// the design spec's §3: a delete the CLI *refuses* is not a delete that
    /// *failed*. The refusal is overridable and names the one action that
    /// answers it; a failure is not and does not. Collapsing them would put
    /// "Delete Anyway" in front of a permissions error, where forcing changes
    /// nothing.
    enum DeleteAnswer: Equatable {
        /// The file is gone, and the listing has been re-read.
        case deleted
        /// The CLI refused: this file is the last staged IPA some configured
        /// app can build from.
        ///
        /// `objection` is the CLI's **own sentence**, carried rather than
        /// composed. The `staged-ipa-in-use` event names the apps as data and
        /// this store reads it — but only to *recognize* the refusal. The
        /// words come off stderr, because the CLI is the side that knows which
        /// apps `find_in_cache` would leave with nothing, and a second
        /// sentence assembled here drifts from that one the first time either
        /// half changes. Nothing downstream is handed the app list, so nothing
        /// downstream is able to compose an alternative.
        case inUse(objection: String)
        /// It failed for a reason no button here can answer — the file already
        /// gone, a permissions problem, a CLI that could not run at all.
        case failed(String)
    }

    /// The argv, as a static pure function, for the reason `loadArgs()` is one.
    ///
    /// `--force` is appended only when asked for, and that is the flag the
    /// CLI's own refusal hint names as the way past it. `StagedIPAStoreTests`
    /// pins both spellings, and — separately, because an argv assertion cannot
    /// tell whether the argv is *used* — runs the whole two-step against a
    /// stub that refuses without the flag exactly as the CLI does.
    static func deleteArgs(filename: String, force: Bool) -> [String] {
        var args = [Flag.json, Flag.deleteStaged, filename]
        if force { args.append(Flag.force) }
        return args
    }

    static func installArgs(filename: String) -> [String] {
        [Flag.json, Flag.installToMac, filename]
    }

    /// Delete one staged IPA and its `.stripped.json` sidecar.
    ///
    /// Lives here rather than at the call site because this store owns the two
    /// things a delete touches: the runner, and the listing that is now wrong.
    /// A successful delete **re-reads the listing before returning**, which is
    /// what makes the row leave the pane — and this store is shared with the
    /// device pane's join, so the same re-read keeps `byBundleId` honest for a
    /// phone the user has not looked at yet. `TweaksStore.deleteTweak` is the
    /// established shape, including that rule.
    ///
    /// **Answers rather than throws**, unlike that method, and unlike `load()`
    /// it does not swallow. This store deliberately has no `lastError` (see
    /// the type's own doc, which earns that for a *listing* — cheap, constantly
    /// re-run, self-healing); a delete is none of those things, so its failure
    /// has to reach the caller. Handing it back as a value rather than
    /// throwing keeps the refusal — the case with a button behind it — from
    /// being one `catch` among several at the call site.
    ///
    /// `runOnce` rather than `runEvents`: the refusal is a non-zero exit *by
    /// design*, and `runEvents` checks the status before returning anything, so
    /// everything the CLI said on its way out would be thrown away. Reading the
    /// events and stderr first is what lets the refusal be data.
    @discardableResult
    func deleteStaged(
        filename: String, force: Bool, argsOverride: [String]? = nil
    ) async -> DeleteAnswer {
        let result: CLIRunner.RunResult
        do {
            result = try await runner.runOnce(
                arguments: argsOverride ?? Self.deleteArgs(filename: filename, force: force)
            )
        } catch {
            return .failed(error.localizedDescription)
        }

        // Two channels, and either one is enough. The event is the CLI's own
        // data channel for this refusal; the exit status is the fallback for a
        // `graft` old enough to raise `StagedIPAInUseError` without
        // emitting the event — the same two-channel reading `TweaksView` does
        // for `--delete-tweak`'s exit 16, and for the same skew reason (the CLI
        // and this app deploy separately on this machine).
        let named = result.stdout.buildEvents().contains {
            if case .stagedIPAInUse = $0 { return true }
            return false
        }
        let described = CLIError.exited(
            doing: "Delete", code: result.exitCode, stderr: result.stderr
        ).localizedDescription
        if named || result.exitCode == CLIContract.Exit.stagedIPAInUse {
            return .inUse(objection: described)
        }
        guard result.exitCode == 0 else { return .failed(described) }

        await load()
        return .deleted
    }

    /// Hand one staged IPA or Mac build to PlayCover, and follow it to the
    /// end of the CLI's wait for the install (spec 5d items 2 and 5).
    ///
    /// Streamed, not run once: `handed-to-playcover` comes the moment the file
    /// is handed over, and `onEvent` hears it then, so the pane can say
    /// "Waiting for PlayCover to install …" for the up to two minutes the CLI
    /// waits. What comes back is read off the whole run (`MacInstall`).
    ///
    /// **A Stop ends the wait, never the handoff.** Canceling the Task stops
    /// the CLI (SIGTERM), which exits 143 with the handoff already said; that
    /// is `.stopped`, never a failure.
    ///
    /// The failure is not optional to report, though: with PlayCover absent,
    /// `--install-to-mac` fails, and a button that then did nothing visible
    /// would be indistinguishable from a dead one — the hazard
    /// `CachePaneView`'s required callbacks were written against.
    ///
    /// The listing is not re-read: installing changes the Mac, not the cache.
    ///
    /// With `holdingQuit`, the quit is held until it ends
    /// (`TerminationHold.waitingForPlayCover`), and Quit Anyway stops it as
    /// Stop does: Builds' and the Cache's Install to Mac…, which nothing
    /// else holds for, so ⌘Q during the wait quit without asking (review
    /// 23-2, §11). The app pane's handoff runs inside its Mac build, whose
    /// hold already covers it (`ConfigStore.launchBuild`).
    func installToMac(
        filename: String, argsOverride: [String]? = nil, holdingQuit: Bool = false,
        onEvent: @escaping @MainActor (BuildEvent) -> Void = { _ in }
    ) async -> MacInstall {
        let args = argsOverride ?? Self.installArgs(filename: filename)
        guard holdingQuit else { return await handOver(args, onEvent: onEvent) }
        // In a task of its own, which Quit Anyway cancels; canceling the
        // caller's task, its Stop, cancels it too (`TerminationHold.holding`).
        // The work throws nothing, so neither does this.
        return (try? await hold.holding(TerminationHold.waitingForPlayCover) {
            await self.handOver(args, onEvent: onEvent)
        }) ?? MacInstall(handed: nil, ending: .stoppedBeforeHandoff)
    }

    /// `--install-to-mac` with `args`, read off as `MacInstall`.
    private func handOver(
        _ args: [String], onEvent: @escaping @MainActor (BuildEvent) -> Void
    ) async -> MacInstall {
        let result: CLIRunner.RunResult
        do {
            result = try await runner.stream(arguments: args, onEvent: onEvent)
        } catch {
            // Stopped before the CLI started: nothing was handed over, and
            // nothing refused it.
            if Task.isCancelled { return MacInstall(handed: nil, ending: .stoppedBeforeHandoff) }
            return MacInstall(handed: nil, ending: .refused(error.localizedDescription))
        }
        return MacInstall(
            events: result.events, exitCode: result.exitCode, stderr: result.stderr,
            stopped: Task.isCancelled
        )
    }
}

/// What one `--install-to-mac` did, read off its events and its exit (spec 5d
/// items 2 and 5). The pane's log, Builds and the Cache all say it the same
/// way, through `streamLines` and `endingLines`.
struct MacInstall: Equatable {
    enum Ending: Equatable {
        /// PlayCover's copy of the app carries the handed version.
        case installed(PlayCoverInstalledRecord)
        /// The CLI's wait ran out: PlayCover still installing, or failed.
        case unconfirmed(PlayCoverInstallUnconfirmedRecord)
        /// The person stopped the wait. The handoff stands.
        case stopped
        /// Handed over, and nothing more said, with a clean exit: a CLI from
        /// before 2.3.
        case handedOnly
        /// The person stopped it before anything was handed over: Builds'
        /// and the Cache's Stop show from the press, while the CLI still
        /// gets the file ready. Not a refusal.
        case stoppedBeforeHandoff
        /// Handed over, then the CLI failed before saying how the install
        /// went: the reason, from its stderr. The handoff stands.
        case died(String)
        /// Nothing was handed over: the CLI's own sentence.
        case refused(String)
    }

    /// Where Install to Mac… is tried again, which the unconfirmed line names.
    enum Retry {
        /// A Mac build, from Builds or the app's pane.
        case buildsRow
        /// A staged IPA, from the Cache pane.
        case cacheRow
    }

    var handed: HandedToPlayCoverRecord?
    var ending: Ending
    /// The CLI's warnings before the handoff: an app's own binary needing an
    /// iOS framework macOS lacks.
    var warnings: [String] = []
    /// Its notes after it: a bundle id it couldn't follow the install of,
    /// or what a Stop's settings step did.
    var notes: [String] = []

    init(handed: HandedToPlayCoverRecord?, ending: Ending,
         warnings: [String] = [], notes: [String] = []) {
        self.handed = handed
        self.ending = ending
        self.warnings = warnings
        self.notes = notes
    }

    /// Read off a finished run. `stopped` is the caller's own cancel.
    init(events: [BuildEvent], exitCode: Int32, stderr: String, stopped: Bool) {
        var handed: HandedToPlayCoverRecord?
        var ending: Ending?
        var warnings: [String] = []
        var notes: [String] = []
        for event in events {
            switch event {
            case let .handedToPlayCover(record): handed = record
            case let .playCoverInstalled(record): ending = .installed(record)
            case let .playCoverInstallUnconfirmed(record): ending = .unconfirmed(record)
            case let .log(source, line) where source == "warning": warnings.append(line)
            case let .log(source, line) where source == "playcover": notes.append(line)
            default: break
            }
        }
        if ending == nil {
            if handed == nil, stopped, exitCode != 0 {
                ending = .stoppedBeforeHandoff
            } else if handed == nil {
                ending = exitCode == 0
                    ? .handedOnly
                    : .refused(CLIError.exited(
                        doing: "Install to Mac", code: exitCode, stderr: stderr
                    ).localizedDescription)
            } else if stopped || [129, 130, 143].contains(exitCode) {
                // 143 is a SIGTERM, 129 a hang-up, 130 Ctrl-C: all a Stop.
                ending = .stopped
            } else if exitCode == 0 {
                ending = .handedOnly
            } else {
                ending = .died(Self.reason(stderr: stderr, exitCode: exitCode))
            }
        }
        self.init(handed: handed, ending: ending ?? .handedOnly, warnings: warnings, notes: notes)
    }

    /// Why a CLI died after the handoff: its `error:` line when it wrote one,
    /// else its stderr's last line (a traceback's names the exception), else
    /// its exit status.
    static func reason(stderr: String, exitCode: Int32) -> String {
        let lines = stderr.split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        if let error = lines.first(where: { $0.hasPrefix("error: ") }) {
            return String(error.dropFirst("error: ".count))
        }
        return lines.last ?? "exit status \(exitCode)"
    }

    /// The CLI's sentence when nothing was handed over, else nil.
    var complaint: String? {
        if case let .refused(sentence) = ending { return sentence }
        return nil
    }

    /// The app's name for the lines: the archive's own, as the handoff or
    /// the event that ended the wait said it, else `fallback`.
    func appName(or fallback: String) -> String {
        let said: String?
        switch ending {
        case let .installed(record): said = record.displayName
        case let .unconfirmed(record): said = record.displayName
        default: said = nil
        }
        return [handed?.displayName, said].compactMap { $0 }.first { !$0.isEmpty } ?? fallback
    }

    static func waitingLine(app: String) -> String {
        "Waiting for PlayCover to install \(app)…"
    }

    /// What one streamed event adds to the pane's log as it arrives: the
    /// handoff and the wait it starts, a warning, a note.
    static func streamLines(for event: BuildEvent, app fallback: String) -> [LogLine] {
        switch event {
        case let .handedToPlayCover(record):
            let app = record.displayName.flatMap { $0.isEmpty ? nil : $0 } ?? fallback
            return [
                LogLine(kind: .ok, text: "  ✓ handed \(record.filename) to PlayCover"),
                LogLine(kind: .plain, text: waitingLine(app: app)),
            ]
        case let .log(source, line) where source == "warning":
            return [LogLine(kind: .warn, text: line)]
        case let .log(source, line) where source == "playcover":
            // Plain, not dim: after a Stop it carries the first-handoff notice.
            return [LogLine(kind: .plain, text: line)]
        default:
            return []
        }
    }

    /// How it ended, once the CLI is done: the install or the wait running
    /// out, then what Graft did to the app's PlayCover settings.
    func endingLines(app fallback: String, retry: Retry) -> [LogLine] {
        let app = appName(or: fallback)
        switch ending {
        case let .installed(record):
            return [LogLine(kind: .ok, text: "  ✓ PlayCover installed \(app) \(record.version). "
                            + "Open it from PlayCover.")]
                + record.settings.lines(app: app)
        case let .unconfirmed(record):
            let whereTo = retry == .buildsRow ? "on the build's row in Builds" : "on its row"
            return [LogLine(kind: .warn, text: "PlayCover hasn't finished installing \(app). Check "
                            + "PlayCover's window; if the install failed there, Install to Mac… "
                            + "\(whereTo) tries again.")]
                + record.settings.lines(app: app)
        case .stopped:
            return [LogLine(kind: .dim, text: "Stopped waiting. PlayCover may still be installing \(app).")]
        case .stoppedBeforeHandoff:
            return [LogLine(kind: .dim, text: "Stopped before Graft handed \(app) to PlayCover.")]
        case .handedOnly:
            return []
        case let .died(reason):
            return [LogLine(kind: .warn, text: "Graft stopped following PlayCover's install: \(reason)")]
        case let .refused(complaint):
            return [LogLine(kind: .warn, text: "  ✗ \(complaint)")]
        }
    }

    /// Everything after the press, for a surface that shows it all at once
    /// (Builds, the Cache): the warnings, the notes, then how it ended.
    func summaryLines(app fallback: String, retry: Retry) -> [LogLine] {
        warnings.map { LogLine(kind: .warn, text: $0) }
            + notes.map { LogLine(kind: .plain, text: $0) }
            + endingLines(app: fallback, retry: retry)
    }
}

extension PlayCoverSettingsChange {
    /// How PlayCover's own window names each key, in the order the
    /// sentences name them.
    private static let named: [(key: String, name: String)] = [
        ("keymapping", "keymapping"), ("playChain", "PlayChain"),
    ]

    private static func names(_ keys: [String]) -> String {
        named.filter { keys.contains($0.key) }.map(\.name).joined(separator: " and ")
    }

    /// The first-handoff notice, naming only the keys turned off; the CLI's
    /// console says the same (`playcover_handoff.disabled_notice`).
    func disabledNotice(app: String) -> String? {
        guard !disabled.isEmpty else { return nil }
        var line = "Graft turned off PlayCover's \(Self.names(disabled)) for \(app)."
        if disabled.contains("keymapping") {
            line += " For a game, turn keymapping back on in PlayCover; Graft keeps it from then on."
        }
        return line
    }

    /// A put-back setting: a dim line, not a notice.
    func restoredLine(app: String) -> String? {
        guard !restored.isEmpty else { return nil }
        let noun = restored.count > 1 ? "settings" : "setting"
        return "Graft put back your PlayCover \(Self.names(restored)) \(noun) for \(app)."
    }

    func lines(app: String) -> [LogLine] {
        [disabledNotice(app: app).map { LogLine(kind: .plain, text: $0) },
         restoredLine(app: app).map { LogLine(kind: .dim, text: $0) }].compactMap { $0 }
    }
}
