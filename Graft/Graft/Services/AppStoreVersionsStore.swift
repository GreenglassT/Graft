import Foundation
import Combine

private typealias Flag = CLIContract.Flag

/// Get an older version…'s list: the versions the App Store serves this
/// Apple ID for one app, newest first (`--appstore-versions`).
///
/// The CLI reads each version's number and date with a request of its own,
/// so it reads a page of them per run (its default, 10) and remembers what
/// it read. Rows arrive one at a time as the CLI reads them, which is what
/// the sheet shows as they come. Show More runs it again from the end of the
/// last page: the rows already read come back at once, from the CLI's
/// memory, and the next page is read.
///
/// Owned by `AppStoreStore` (`olderVersions`), whose runner it shares, as
/// Search is. The sheet loads it when it opens and resets it when it closes,
/// which also kills a run still reading.
@MainActor
final class AppStoreVersionsStore: ObservableObject {
    /// What the list is doing. `loading` keeps the rows already on screen:
    /// a page being read adds to them.
    enum Phase: Equatable {
        /// Nothing asked: the sheet isn't open.
        case idle
        case loading
        /// The sentinel arrived: every version is listed, the page read.
        case loaded
        /// The CLI's own words: not signed in, no license, offline.
        case failed(String)
    }

    let runner: CLIRunner

    /// The config app being listed, or nil while idle.
    @Published private(set) var app: String?
    /// Every version the CLI has listed, newest first, by `index`.
    @Published private(set) var releases: [AppStoreReleaseRecord] = []
    @Published private(set) var phase: Phase = .idle
    /// The version the app is held at: what the sheet opened with
    /// (`load(app:held:)`), until a sentinel says. A list that fails partway
    /// sends none, and keeps the one it opened with.
    @Published private(set) var heldVersion: String?
    /// The held build's version id, when the hold names one
    /// (`AppStoreVersionRecord.heldVersionId`): then only that row is held,
    /// though another build shares its number. Kept like `heldVersion`.
    @Published private(set) var heldVersionId: String?
    /// Where the page being read, or last read, ends: rows before it whose
    /// version is still unknown are being read (or Apple wouldn't say).
    @Published private(set) var readingUpTo = 0
    /// Where Show More starts, when there is anything left to read.
    @Published private(set) var nextOffset: Int?

    /// Bumped by every run started and by `reset`. A run that finishes under
    /// an older one was replaced or canceled, and writes nothing; its rows
    /// arriving late are dropped the same way.
    /// Unpublished: a change to it alone redraws nothing.
    private var generation = 0
    /// Unpublished: a change to it alone redraws nothing.
    private var task: Task<Void, Never>?

    /// The CLI's page. Only for `readingUpTo` while the first run reads, before
    /// a sentinel says what the page was: the CLI decides, and this app never
    /// sends `--limit`.
    static let assumedPage = 10

    init(runner: CLIRunner) {
        self.runner = runner
    }

    static func args(app: String, offset: Int = 0) -> [String] {
        [Flag.json, Flag.appstoreVersions, Flag.app, app] + (offset > 0 ? [Flag.offset, String(offset)] : [])
    }

    /// The sheet opened for `app`, held at `held` as far as Graft knows
    /// (`AppStoreVersionRecord.heldVersion`): list from the start.
    func load(app: String, held: String? = nil, heldId: String? = nil) {
        start(app: app, offset: 0, keepRows: false, held: held, heldId: heldId)
    }

    /// Show More: read the next page. Only once the last run has finished,
    /// and only when there is one.
    func loadMore() {
        guard let app, phase == .loaded, let nextOffset else { return }
        start(app: app, offset: nextOffset, keepRows: true, held: heldVersion, heldId: heldVersionId)
    }

    /// Try Again, after a failure: the page that failed, rows kept.
    func retry() {
        guard let app, case .failed = phase else { return }
        start(
            app: app, offset: max(0, readingUpTo - Self.assumedPage), keepRows: true,
            held: heldVersion, heldId: heldVersionId
        )
    }

    /// The sheet closed: stop any run (the runner kills its CLI) and forget.
    func reset() {
        task?.cancel()
        task = nil
        generation += 1
        app = nil
        releases = []
        phase = .idle
        heldVersion = nil
        heldVersionId = nil
        readingUpTo = 0
        nextOffset = nil
    }

    private func start(app: String, offset: Int, keepRows: Bool, held: String?, heldId: String?) {
        task?.cancel()
        generation += 1
        let mine = generation
        if !keepRows || self.app != app {
            releases = []
            heldVersion = held
            heldVersionId = heldId
        }
        self.app = app
        phase = .loading
        nextOffset = nil
        readingUpTo = offset + Self.assumedPage
        task = Task { [weak self] in await self?.run(app: app, offset: offset, generation: mine) }
    }

    private func run(app: String, offset: Int, generation mine: Int) async {
        let outcome: Result<[BuildEvent], Error>
        do {
            outcome = .success(try await runner.streamEvents(
                Self.args(app: app, offset: offset),
                failing: "App Store versions",
                refusal: .appStoreGet
            ) { [weak self] event in
                self?.note(event, generation: mine)
            })
        } catch {
            outcome = .failure(error)
        }
        guard mine == generation, !Task.isCancelled else { return }
        task = nil
        switch outcome {
        case let .success(events):
            // The returned events, not only the streamed ones: a MainActor hop
            // for the last rows can land after this.
            for case let .appStoreRelease(row) in events { merge(row) }
            guard let end = events.lazy.compactMap({ event -> AppStoreReleasesEndRecord? in
                if case let .appStoreReleasesEnd(record) = event { return record }
                return nil
            }).first else {
                phase = .failed("graft ended the list of versions without finishing it")
                return
            }
            heldVersion = end.heldVersion
            heldVersionId = end.heldVersionId
            readingUpTo = end.offset + end.limit
            nextOffset = Self.nextOffset(releases, after: readingUpTo)
            phase = .loaded
        case let .failure(error):
            phase = .failed(error is NeedsLicense ? Self.needsLicenseMessage : error.localizedDescription)
        }
    }

    /// The listing's `needs-license` refusal, in the sheet's words: the
    /// get's own (`NeedsLicense`) names Get (free), which isn't here.
    static let needsLicenseMessage = "This Apple ID doesn't have this app, and the App Store lists "
        + "versions only of apps an Apple ID has. Close this and get the app with the button "
        + "on its pane first."

    /// One streamed row, from the run this generation started.
    func note(_ event: BuildEvent, generation mine: Int) {
        guard mine == generation, case let .appStoreRelease(row) = event else { return }
        merge(row)
    }

    /// Put `row` in its place, replacing the same version's row. A row the
    /// CLI couldn't read this time keeps what an earlier one said.
    private func merge(_ row: AppStoreReleaseRecord) {
        if let at = releases.firstIndex(where: { $0.externalId == row.externalId }) {
            let old = releases[at]
            releases[at] = row.version == nil && old.version != nil ? old : row
        } else {
            releases.append(row)
            releases.sort { $0.index < $1.index }
        }
    }

    /// Where Show More starts: the end of the page, when any version after
    /// it is still unread (no version, and no reason Apple gave). nil when
    /// there is nothing left to read.
    nonisolated static func nextOffset(_ releases: [AppStoreReleaseRecord], after end: Int) -> Int? {
        releases.contains { $0.index >= end && $0.version == nil && $0.error == nil } ? end : nil
    }

    // MARK: Builds that share a version

    /// One of two or more rows read with the same version number. The App
    /// Store can serve more than one build under one version, and ipatool
    /// gives only the number and the date, so the sheet tells them apart by
    /// a count. It goes from the oldest, as build numbers do, though the rows
    /// stay newest first. Only rows read so far count: Show More reading an
    /// older build of the same number renumbers the rest.
    struct DuplicateBuild: Equatable {
        /// The version number, trimmed.
        let version: String
        /// 1 is the oldest read so far.
        let number: Int
        /// How many rows read so far have this number.
        let count: Int

        /// What follows the row's version: "(build 1 of 2)".
        var suffix: String { "(build \(number) of \(count))" }

        /// Which build this is, plainly: the row's help.
        var help: String {
            let which: String
            if count == 2 {
                which = number == 2 ? "This is the newer one (build 2)." : "This is the older one (build 1)."
            } else if number == count {
                which = "This is the newest (build \(number))."
            } else if number == 1 {
                which = "This is the oldest (build 1)."
            } else {
                which = "This is build \(number), counting from the oldest."
            }
            return "The App Store has \(count) builds numbered \(version). \(which)"
        }
    }

    /// The listed rows that share their version with another, by
    /// `externalId` (`duplicateBuilds(_:)`).
    var duplicateBuilds: [String: DuplicateBuild] { Self.duplicateBuilds(releases) }

    /// Every row whose version, trimmed, is exactly another read row's, by
    /// `externalId`, numbered from the oldest (the highest `index`). A row
    /// not read yet counts for nothing: its number may be another one
    /// altogether. A version listed once has no entry.
    nonisolated static func duplicateBuilds(_ releases: [AppStoreReleaseRecord]) -> [String: DuplicateBuild] {
        var byVersion: [String: [AppStoreReleaseRecord]] = [:]
        for row in releases {
            guard let version = row.version?.trimmingCharacters(in: .whitespacesAndNewlines),
                  !version.isEmpty
            else { continue }
            byVersion[version, default: []].append(row)
        }
        var builds: [String: DuplicateBuild] = [:]
        for (version, rows) in byVersion where rows.count > 1 {
            for (at, row) in rows.sorted(by: { $0.index > $1.index }).enumerated() {
                builds[row.externalId] = DuplicateBuild(version: version, number: at + 1, count: rows.count)
            }
        }
        return builds
    }

    /// How a row reads its state, for the sheet.
    enum RowState: Equatable {
        /// Version known: it can be got.
        case ready
        /// On the page being read, not read yet.
        case reading
        /// Beyond the page: Show More reads it.
        case unread
        /// Apple wouldn't describe it, in the CLI's words.
        case unavailable(String)
    }

    nonisolated static func rowState(
        _ row: AppStoreReleaseRecord, phase: Phase, readingUpTo: Int
    ) -> RowState {
        if row.version != nil { return .ready }
        if let error = row.error { return .unavailable(error) }
        return phase == .loading && row.index < readingUpTo ? .reading : .unread
    }
}
