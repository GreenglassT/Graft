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

    /// Bumped on every `load()` call so a stale one can tell it lost a race.
    /// Unpublished: bookkeeping, which nothing draws.
    private var loadGeneration = 0

    init(runner: CLIRunner) {
        self.runner = runner
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

    /// Hand one staged IPA to PlayCover. nil when it was handed over, and the
    /// CLI's own sentence when it could not be.
    ///
    /// **Nothing to report on success, deliberately.** `open -b` returns as
    /// soon as LaunchServices has delivered the event, so there is no finished
    /// install to announce — the CLI's event is named `handed-to-playcover`
    /// for exactly that reason, and inventing a confirmation here would claim
    /// more than either side can see. PlayCover shows its own progress.
    ///
    /// The failure is not optional to report, though, and that is why this
    /// answers at all rather than being a discarded `Task`: with PlayCover
    /// absent, `--install-to-mac` fails, and a button that then did nothing
    /// visible would be indistinguishable from a dead one — the hazard
    /// `CachePaneView`'s required callbacks were written against.
    ///
    /// The listing is not re-read: installing changes the Mac, not the cache.
    func installToMac(filename: String, argsOverride: [String]? = nil) async -> String? {
        do {
            let result = try await runner.runOnce(
                arguments: argsOverride ?? Self.installArgs(filename: filename)
            )
            guard result.exitCode != 0 else { return nil }
            return CLIError.exited(
                doing: "Install to Mac", code: result.exitCode, stderr: result.stderr
            ).localizedDescription
        } catch {
            return error.localizedDescription
        }
    }
}
