import Foundation
import Combine

private typealias Flag = CLIContract.Flag

@MainActor
final class TweaksStore: ObservableObject, CLIStore {
    @Published private(set) var tweaks: [TweakRecord] = []
    @Published var lastError: String?

    /// Whether a `loadTweaks()` has *completed* — success or failure, records or
    /// not. Exactly `DevicesStore.hasLoaded`, and for exactly its reason, which
    /// that property's doc argues at length: a caller that only wants to fetch
    /// once has to key off the attempt, because the result cannot tell the
    /// three cases apart.
    ///
    /// `tweaks.isEmpty` conflates "not loaded" with two states that are not
    /// loading problems and must not be retried on sight:
    ///
    /// - **The load failed.** `--tweaks` off PATH is a documented failure mode
    ///   for this GUI. A result-shaped guard re-spawns the doomed subprocess on
    ///   every sidebar click onto an app with tweaks attached — and
    ///   `AppDetailView` nils `tweakError` on each app change, so it re-raises
    ///   the error banner too. Five apps clicked through, five subprocesses,
    ///   five banners.
    /// - **The library is genuinely empty.** A `config.yaml` still listing
    ///   tweaks whose `tweaks_dir` moved, or whose .debs were deleted, is a
    ///   perfectly successful load that leaves this array empty forever. Same
    ///   re-spawn, silently.
    ///
    /// **A canceled load is not an attempt**, the correction `DevicesStore`
    /// already carries. `AppDetailView`'s library load lives in a
    /// `.task(id: app.id)`, which is torn down the instant the user clicks
    /// another sidebar row; `CLIRunner` reports the SIGTERM'd child as an
    /// ordinary non-zero exit, so an interrupted load would otherwise latch this
    /// true and the pane would never try again. Only ever set true, never back
    /// to false, so a canceled *re*-load leaves the earlier attempt standing.
    ///
    /// Deliberately not read by the three call sites that reload
    /// unconditionally — `ContentView` on entering the Tweaks section,
    /// `TweaksView`'s Refresh, and `AttachTweaksSheet` on opening — because each
    /// exists to pick up a library that changed underneath. This flag is for the
    /// one caller that only wants a first fill: `AppDetailView`, which needs the
    /// names once and re-runs its task on every sidebar click.
    @Published private(set) var hasLoaded: Bool = false

    let runner: CLIRunner
    private let cliArgs: [String]

    init(runner: CLIRunner, cliArgs: [String] = [Flag.json, Flag.tweaks]) {
        self.runner = runner
        self.cliArgs = cliArgs
    }

    func loadTweaks() async throws -> [TweakRecord] {
        // Settled on every exit path an attempt can take — exited 0, exited
        // non-zero, never launched — and on none of the one it can't: see
        // `hasLoaded`.
        defer { if !Task.isCancelled { hasLoaded = true } }
        let collected = try await runner.runCollecting(cliArgs, failing: "Load tweaks") { event in
            if case let .tweak(record) = event { return record }
            return nil
        }
        self.tweaks = collected.sorted { $0.filename < $1.filename }
        // Answers for files that are no longer there go with them. `updates`
        // is keyed by filename, and a deleted .deb re-fetched under the same
        // name would otherwise inherit the previous file's badge — claiming an
        // update for a version the new file may already be.
        let present = Set(self.tweaks.map(\.filename))
        updates = updates.filter { present.contains($0.key) }
        return self.tweaks
    }

    /// `CLIStore`'s post-mutation refresh: for this store, the tweaks list.
    func reload() async throws {
        _ = try await loadTweaks()
    }

    // MARK: - Newer versions

    /// What the CLI last reported for each file, keyed by filename.
    ///
    /// Keyed by filename rather than package id because that is what
    /// identifies a *row*: two files can carry the same package, which this
    /// library does today — `com.example.btweak` at `1.1.8` and
    /// `1.1.8-jailed` — and they are separate rows with separate answers.
    @Published private(set) var updates: [String: TweakUpdateRecord] = [:]
    @Published private(set) var isCheckingUpdates = false
    /// nil until a check has finished, so "no updates" and "not asked yet"
    /// render differently. The first is worth saying; the second must not
    /// claim anything.
    ///
    /// Read for `checked`/`unchecked` only. The *outdated* figure the header
    /// shows is `outdatedCount`, derived from `updates` — this record is a
    /// snapshot of the moment the check ended, and updating a tweak clears its
    /// badge without changing it, so the summary went on saying "3 updates"
    /// over a list with none left.
    @Published private(set) var lastCheck: TweakUpdatesEndRecord?

    /// Ask what is published for everything in the library.
    ///
    /// Not run automatically on entering the pane. It reaches the network once
    /// per package the cached repo indexes can't answer, and a listing that
    /// silently makes a dozen HTTP calls every time you glance at it is not
    /// what "show me my tweaks" should cost. The button says when.
    func checkForUpdates(argsOverride: [String]? = nil) async {
        guard !isCheckingUpdates else { return }
        isCheckingUpdates = true
        defer { isCheckingUpdates = false }

        do {
            let events = try await runner.runEvents(
                argsOverride ?? [Flag.json, Flag.tweakUpdates], failing: "Check for updates"
            )
            var found: [String: TweakUpdateRecord] = [:]
            for event in events {
                switch event {
                case let .tweakUpdate(record): found[record.filename] = record
                case let .tweakUpdatesEnd(record): lastCheck = record
                default: break
                }
            }
            // Replaced wholesale rather than merged: a file deleted since the
            // last check should stop having an answer, and a stale entry under
            // a filename that now holds a different .deb would claim an update
            // for a version nobody has.
            updates = found
        } catch {
            lastError = error.localizedDescription
        }
    }

    /// The published answer for a row, if there is one.
    func update(for tweak: TweakRecord) -> TweakUpdateRecord? {
        updates[tweak.filename]
    }

    var outdatedCount: Int { updates.values.filter(\.updateAvailable).count }

    /// Which file an update is running for, so only that row shows a spinner.
    @Published private(set) var updatingFilename: String?

    /// Download the newest build of `tweak` and re-point the apps using it.
    ///
    /// One CLI call, not three. `--fetch` then `--detach-tweak` then
    /// `--attach-tweak` from here would make a download that lands and an
    /// attach that fails into three subprocesses and no single answer about
    /// which half happened.
    ///
    /// Returns the record so the caller can say what changed — the filename
    /// usually changes, and the apps that now point at the new one are the
    /// ones needing a rebuild.
    /// `--update-tweak`'s arguments; with `trustKey`, the person's OK for an
    /// update whose source isn't verified: the row's `confirm_key`, so the
    /// yes is for the update they were shown (`--trust-tweak`, 2.3.1 A1).
    static func updateArguments(_ tweak: TweakRecord, trustKey: String?) -> [String] {
        [Flag.json, Flag.updateTweak, tweak.filename] + (trustKey.map { [Flag.trustTweak, $0] } ?? [])
    }

    @discardableResult
    func updateTweak(
        _ tweak: TweakRecord, trustKey: String? = nil, argsOverride: [String]? = nil
    ) async -> TweakUpdatedRecord? {
        guard updatingFilename == nil else { return nil }
        updatingFilename = tweak.filename
        defer { updatingFilename = nil }

        let record: TweakUpdatedRecord?
        do {
            record = try await runner.run(
                argsOverride ?? Self.updateArguments(tweak, trustKey: trustKey),
                failing: "Update tweak"
            ) { event -> TweakUpdatedRecord? in
                if case let .tweakUpdated(record) = event { return record }
                return nil
            }
        } catch {
            lastError = error.localizedDescription
            return nil
        }

        // Everything below is past the write — `CLIStore.mutate`'s rule. The
        // .deb is downloaded and config.yaml is rewritten, so a refresh that
        // fails must not discard the record or report the swap as failed; it
        // reports a stale *list*, which is what is actually true.
        do {
            // The library changed underneath: a new file exists and an app's
            // tweak list may have been rewritten, so both are re-read rather
            // than patched from here.
            try await reload()
        } catch {
            lastError = "Tweak updated, but couldn't refresh the list: "
                + error.localizedDescription
        }
        if record != nil {
            // Cleared for `updated: false` too. That answer means the CLI just
            // looked and found nothing newer — the badge was stale, which is
            // the whole reason the CLI carries a `reason` for it. Leaving it up
            // would keep advertising an update that provably does not exist,
            // on a button that now does nothing.
            updates[tweak.filename] = nil
        }
        return record
    }

    /// Thrown when `--delete-tweak` refused because the tweak is still
    /// attached, carrying the apps the CLI named.
    ///
    /// A typed error rather than a code on `CLIError`, because the useful part
    /// is the *list*, and the view offers "Detach and Delete" off it.
    struct TweakInUse: Error {
        let record: TweakInUseRecord
    }

    /// Delete a tweak from tweaks_dir. Throws `TweakInUse` when the CLI refuses
    /// because an app still references it.
    ///
    /// Deliberately not `mutate`. That routes through `runEvents`, which checks
    /// the exit status *before* returning anything — so on the refusal, which
    /// is a non-zero exit by design, everything the CLI said on its way out was
    /// thrown away and the view had to recover the app list by finding
    /// "is attached to:" in stderr. Reading the events first is what lets the
    /// refusal be data.
    @discardableResult
    func deleteTweak(
        filename: String,
        argsOverride: [String]? = nil
    ) async throws -> TweakDeletedRecord? {
        let args = argsOverride ?? [Flag.json, Flag.deleteTweak, filename]
        let result = try await runner.runOnce(arguments: args)
        let events = result.stdout.buildEvents()

        if let refusal = events.compactMap({ event -> TweakInUseRecord? in
            if case let .tweakInUse(record) = event { return record }
            return nil
        }).first {
            throw TweakInUse(record: refusal)
        }
        guard result.exitCode == 0 else {
            throw CLIError.exited(doing: "Delete", code: result.exitCode, stderr: result.stderr)
        }

        let deleted = events.compactMap { event -> TweakDeletedRecord? in
            if case let .tweakDeleted(record) = event { return record }
            return nil
        }.first
        // Same rule as `mutate`: the write already landed, so a failed reload
        // is "it worked, the list is stale" rather than a thrown failure.
        do {
            try await reload()
        } catch {
            lastError = "Tweak deleted, but couldn't refresh the list: "
                + error.localizedDescription
        }
        return deleted
    }
}
