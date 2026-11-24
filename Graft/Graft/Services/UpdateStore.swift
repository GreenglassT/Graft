import Foundation
import Observation

private typealias Flag = CLIContract.Flag

/// What updating itself needs from the running app. Nil (`UpdateStore.host`)
/// for a copy that mustn't replace itself: a Debug build, one installed from
/// source by `graft-install`, a test host, an Intel Mac.
struct SelfUpdateHost {
    /// This app's bundle: the one an update replaces.
    let appPath: String
    /// This process, which the install helper waits on.
    let pid: Int32
    /// The work that mustn't be interrupted. Nothing installs while it holds.
    let hold: TerminationHold
    /// Quit, through the app's delegate (`QuitGuard`), which hands the quit
    /// to `UpdateStore.handOver`. `GraftApp` passes `NSApp.terminate`.
    let terminate: @MainActor () -> Void
}

/// Whether a newer Graft is out, and getting it: the main window's banner,
/// Help › Check for Updates…, and Settings › Updates' two switches.
///
/// The CLI does all of it, as it does every other request: the app makes no
/// connections of its own. `--check-update` asks GitHub, with a generic
/// User-Agent and nothing that identifies anyone. `--download-update`
/// downloads the new Graft.zip and checks it (`app_update` in the CLI).
/// `--install-update` starts the helper that replaces this app once it has
/// quit.
///
/// **The daily rule.** With the first switch on (the default), the window
/// checks at launch and then once an hour while the app is open, and each of
/// those runs the CLI only when more than a day has passed since the last
/// *successful* check (`isDue`). So a check that failed — offline, or GitHub
/// limiting requests — is tried again an hour later, not a day.
///
/// **A failed check is quiet.** Nobody asked for the daily one, so it changes
/// nothing on screen: the banner keeps whatever the last successful check
/// found. Only Check for Updates… says it failed.
///
/// **Updating** (`phase`). With "Download and install updates automatically"
/// on (the default), a newer release found by any check is downloaded and
/// checked in the background, once per version per launch, and installed
/// when Graft quits, or at once with Restart to Update. With it off, the
/// banner's Update does the same download and check, then restarts into it.
/// What keeps that safe:
/// - Nothing is "ready" until the CLI has checked it. A failed check has
///   already deleted the download; the banner then links the release page.
/// - **A generation counter.** Each download is numbered, and install rights
///   belong to the newest one alone: a new download first takes them back
///   (`ready = nil`), and an older one finishing late neither grants nor
///   revokes anything.
/// - **A pending install** (`installPending`), from the quit taking the
///   install to the helper being started: no download starts in that time,
///   and `QuitGuard` refuses a second quit, so a quit can't slip past the
///   install.
/// - **Install on quit** (`handOver`): only with nothing held
///   (`TerminationHold`), never while work runs.
/// - The helper's own failure (a folder macOS won't let it change) can't be
///   reported by a process that has quit. The attempt is written down before
///   the quit (`Key.attempt`), and a launch that finds itself still the old
///   version says the install didn't take, and links the release page.
///
/// The switches, the time of the last successful check and its answer live
/// in `defaults`, so the banner is back after a relaunch without asking
/// again. An answer is kept only for the version it was about: a newer Graft
/// launching drops it.
///
/// **The release page is built here** (`releasePage`), from a version that
/// is digits and dots and nothing else (`isPlainVersion`). Never from the
/// CLI's `url`, and never from anything read back from `defaults`: any
/// process running as this user can write those (`defaults write`), and a
/// link taken from there would make the banner's Release Page open whatever
/// page it named. A saved answer or attempt whose version isn't plain is
/// dropped at launch.
@MainActor
@Observable
final class UpdateStore: QuitHandover {
    /// How long a successful check lasts.
    static let interval: TimeInterval = 24 * 60 * 60
    /// How often the window asks `checkIfDue` while the app is open.
    static let pollInterval: Duration = .seconds(60 * 60)

    /// The `UserDefaults` keys.
    enum Key {
        static let dailyCheck = "updateCheck.daily"
        static let lastChecked = "updateCheck.lastSuccess"
        static let lastAnswer = "updateCheck.lastAnswer"
        static let autoInstall = "updateInstall.automatic"
        /// The install handed to the helper as Graft quit (`Attempt`), until
        /// the next launch reads it.
        static let attempt = "updateInstall.attempt"
        /// A version that failed its checks or its install: not downloaded on
        /// its own again (Update still tries it).
        static let failedVersion = "updateInstall.failedVersion"
    }

    /// An install handed over at a quit: the version, and the version it
    /// replaces.
    struct Attempt: Codable, Equatable {
        let version: String
        let from: String
    }

    /// What Help › Check for Updates… says.
    enum Answer: Equatable {
        case upToDate
        case available(UpdateCheckRecord)
        /// The CLI's sentence, or the runner's when the CLI couldn't say.
        case failed(String)

        /// The sentence for a run that said nothing at all.
        static let noAnswer = "The command-line tool gave no answer."

        var title: String {
            switch self {
            case .upToDate: return "You're up to date"
            case let .available(record): return "Graft \(record.latest ?? "") is available"
            case .failed: return "Couldn't check for updates"
            }
        }

        /// `canUpdate`: this copy can update itself (`UpdateStore.canUpdate`),
        /// so the alert offers Update.
        func message(appVersion: String, canUpdate: Bool = false) -> String {
            switch self {
            case .upToDate:
                return "Graft \(appVersion) is the newest version."
            case .available where canUpdate:
                return "You have Graft \(appVersion). Update downloads the new version from GitHub, "
                    + "checks that it's Graft, signed by its developer and notarized by Apple, and "
                    + "restarts Graft. Your settings, apps and builds carry over."
            case .available:
                return "You have Graft \(appVersion). Download the new Graft.zip from its release "
                    + "page, then replace Graft.app in Applications. Your settings, apps and "
                    + "builds carry over."
            case let .failed(why):
                return why
            }
        }
    }

    /// Where updating is.
    enum Phase: Equatable {
        case idle
        /// `fraction` is 0 to 1, once the CLI has said how big it is.
        case downloading(version: String, fraction: Double?)
        case verifying(version: String)
        /// Checked, and installs when Graft quits (or Restart to Update).
        case ready(version: String)
        /// Handed to the helper as Graft quits.
        case installing(version: String)
        /// The CLI's sentence; the banner links the release page.
        case failed(version: String, message: String)
    }

    /// What the banner shows: `phase`, or with nothing under way, the last
    /// check's offer.
    struct Notice: Equatable {
        enum State: Equatable {
            case available(canUpdate: Bool)
            case downloading(Double?)
            case verifying
            /// `installsOnQuit`: quitting installs it (the switch, or Update).
            case ready(installsOnQuit: Bool)
            case installing
            case failed(String)
        }

        let version: String
        let state: State
    }

    /// The install failed after Graft quit: the next launch is still the
    /// old version.
    static let installDidNotTake = "Graft couldn't replace itself, so it's still the version "
        + "you had. Download the new Graft.zip from the release page and replace Graft.app "
        + "in Applications. The update log is in ~/Library/Logs/Graft/update.log."

    /// What Check for Updates… says when the CLI's answer named a version
    /// that isn't digits and dots.
    static let unreadableVersion = "The update check's answer didn't name a version Graft can read."

    /// Graft's releases on GitHub: every page the banner and the alert open
    /// is under it.
    nonisolated static let releasesPage = "https://github.com/GreenglassT/Graft/releases"

    /// Whether `text` is a plain dotted version, the way the CLI gives a
    /// release's tag without its "v": ASCII digits in parts joined by single
    /// dots ("2.0.1"), 32 characters at most. Only such a version goes into a
    /// link (`releasePage`), so a "/", "..", "?", "#" or "@" never can.
    nonisolated static func isPlainVersion(_ text: String) -> Bool {
        let bytes = Array(text.utf8)
        guard (1...32).contains(bytes.count) else { return false }
        return bytes.split(separator: UInt8(ascii: "."), omittingEmptySubsequences: false).allSatisfy { part in
            !part.isEmpty && part.allSatisfy { (UInt8(ascii: "0")...UInt8(ascii: "9")).contains($0) }
        }
    }

    /// `version`'s release page, `releasesPage/tag/v<version>`, which is how
    /// Graft's releases are tagged. Graft's releases when `version` isn't
    /// plain, which none that reach here should be: still a page of
    /// Graft's.
    nonisolated static func releasePage(version: String?) -> URL {
        if let version, isPlainVersion(version), let url = URL(string: "\(releasesPage)/tag/v\(version)") {
            return url
        }
        return URL(string: releasesPage)!
    }

    /// A saved or fresh answer that can be shown: it worked, and the version
    /// it names is plain.
    nonisolated static func isUsable(_ record: UpdateCheckRecord) -> Bool {
        record.ok && record.latest.map(isPlainVersion) == true
    }

    /// The CLI's reasons for a download that was refused for what it is,
    /// rather than for this Mac's connection: not tried again on its own.
    /// `no-team` is this copy's: it has no Developer ID team to trust
    /// updates from (`app_update.own_team`), and no retry gives it one.
    static let refusedForWhatItIs: Set<String> = [
        "no-asset", "digest", "archive", "bundle", "signature", "requirement", "notarization",
        "not-newer", "no-team",
    ]

    let runner: CLIRunner
    /// This copy's CFBundleShortVersionString: what the CLI compares against.
    let appVersion: String
    /// Nil when this copy can't update itself (`SelfUpdateHost`).
    let host: SelfUpdateHost?
    private let defaults: UserDefaults
    private let now: () -> Date
    private let open: (URL) -> Void

    /// Settings › Updates' first switch: whether the daily check runs. On
    /// unless turned off. Off also takes the offer off the banner; Check for
    /// Updates… still works.
    var dailyCheck: Bool {
        didSet { defaults.set(dailyCheck, forKey: Key.dailyCheck) }
    }

    /// Settings › Updates' second switch: download a newer Graft on its own,
    /// and install it when Graft quits. On unless turned off. Off, the banner
    /// offers Update instead.
    var autoInstall: Bool {
        didSet {
            defaults.set(autoInstall, forKey: Key.autoInstall)
            if autoInstall { startDownloadIfWanted() }
        }
    }

    /// The last successful answer about this version, nil before there is one.
    private(set) var lastAnswer: UpdateCheckRecord?
    private(set) var isChecking = false
    /// What Check for Updates… found, until its alert is dismissed.
    var answer: Answer?
    private(set) var phase: Phase = .idle
    /// From the quit taking the install until the helper is started (or
    /// couldn't be).
    private(set) var installPending = false

    /// Check for Updates… was chosen while a daily check was running: that
    /// check's result is its answer.
    @ObservationIgnored private var answerWhenDone = false
    /// The newest download's number (`ready` must carry it to install).
    @ObservationIgnored private(set) var generation = 0
    /// The checked download, and the download it came from.
    @ObservationIgnored private var ready: (generation: Int, version: String, appPath: String)?
    /// The download now running was asked for (Update): restart into it
    /// when it's ready, and install it on any quit.
    @ObservationIgnored private var userAsked = false
    /// Restart to Update asked for this quit: open Graft again afterward.
    @ObservationIgnored private var relaunchRequested = false
    /// Versions downloaded on their own this launch: once each.
    @ObservationIgnored private var triedOnItsOwn: Set<String> = []
    @ObservationIgnored private var downloadTask: Task<Void, Never>?
    /// How long a quit waits for `--install-update` to start the helper.
    /// It takes a second or two; one that never answers must not keep Graft
    /// from quitting. The attempt is written down first, so the next launch
    /// still says whether it took. A test shortens it.
    @ObservationIgnored var handoverTimeout: Duration = .seconds(15)

    /// Every dependency is a parameter: `GraftApp` passes the real
    /// preferences, clock, browser and host, a test its own
    /// (`NoRealDefaultsTests`). No host, no self-update: the banner then
    /// links the release page, as before 2.0.2.
    init(
        runner: CLIRunner, defaults: UserDefaults, now: @escaping () -> Date,
        appVersion: String, open: @escaping (URL) -> Void, host: SelfUpdateHost? = nil
    ) {
        self.runner = runner
        self.defaults = defaults
        self.now = now
        self.appVersion = appVersion
        self.open = open
        self.host = host
        dailyCheck = defaults.object(forKey: Key.dailyCheck) as? Bool ?? true
        autoInstall = defaults.object(forKey: Key.autoInstall) as? Bool ?? true
        if let data = defaults.data(forKey: Key.lastAnswer) {
            // Anyone running as this user can write it: one that doesn't
            // decode, or names a version that isn't plain, is dropped.
            if let saved = try? JSONDecoder().decode(UpdateCheckRecord.self, from: data), Self.isUsable(saved) {
                if saved.current == appVersion { lastAnswer = saved }
            } else {
                defaults.removeObject(forKey: Key.lastAnswer)
            }
        }
        if let data = defaults.data(forKey: Key.attempt) {
            defaults.removeObject(forKey: Key.attempt)
            // Still the version it was to replace: the helper didn't install
            // it. Any other version means it did, or someone else did. Its
            // version goes in the banner and its link, so only a plain one.
            if let attempt = try? JSONDecoder().decode(Attempt.self, from: data),
               attempt.from == appVersion, Self.isPlainVersion(attempt.version) {
                defaults.set(attempt.version, forKey: Key.failedVersion)
                phase = .failed(version: attempt.version, message: Self.installDidNotTake)
            }
        }
    }

    /// The release the main window's banner offers, or nil: the last
    /// successful check found a newer one, and the daily check is on.
    var banner: UpdateCheckRecord? {
        guard dailyCheck, let lastAnswer, lastAnswer.updateAvailable, lastAnswer.latest != nil
        else { return nil }
        return lastAnswer
    }

    /// Whether this copy can update itself.
    var canUpdate: Bool { host != nil }

    /// The newer version the last successful check found, switch or no switch.
    var availableVersion: String? {
        guard let lastAnswer, lastAnswer.updateAvailable else { return nil }
        return lastAnswer.latest
    }

    /// What the banner shows, or nil for no banner.
    var notice: Notice? {
        switch phase {
        case .idle:
            break
        case let .downloading(version, fraction):
            return Notice(version: version, state: .downloading(fraction))
        case let .verifying(version):
            return Notice(version: version, state: .verifying)
        case let .ready(version):
            return Notice(version: version, state: .ready(installsOnQuit: autoInstall || userAsked))
        case let .installing(version):
            return Notice(version: version, state: .installing)
        case let .failed(version, message):
            // Until a newer release than the one that failed is offered.
            if availableVersion == nil || availableVersion == version {
                return Notice(version: version, state: .failed(message))
            }
        }
        guard let release = banner, let latest = release.latest else { return nil }
        return Notice(version: latest, state: .available(canUpdate: canUpdate))
    }

    var lastChecked: Date? { defaults.object(forKey: Key.lastChecked) as? Date }

    /// Whether the daily check should run now: the switch is on, and more
    /// than a day has passed since the last successful check, or there never
    /// was one. A last check in the future (the clock was set back) can't be
    /// trusted, so that is due too.
    static func isDue(dailyCheck: Bool, lastChecked: Date?, now: Date) -> Bool {
        guard dailyCheck else { return false }
        guard let lastChecked else { return true }
        return now < lastChecked || now.timeIntervalSince(lastChecked) > interval
    }

    /// The daily check: the window calls this at launch and every
    /// `pollInterval`, and it runs the CLI only when `isDue`. Either way it
    /// then starts the download, when one is wanted
    /// (`startDownloadIfWanted`): the offer may be from a check before this
    /// launch.
    func checkIfDue() async {
        if Self.isDue(dailyCheck: dailyCheck, lastChecked: lastChecked, now: now()) {
            await check(answering: false)
        }
        startDownloadIfWanted()
    }

    /// Help › Check for Updates…: now, whatever the switch says, and always
    /// with an answer.
    func checkNow() async {
        await check(answering: true)
        startDownloadIfWanted()
    }

    /// The alert's Open Release Page: the page of the release on offer,
    /// built from its version, never the record's `url` (`releasePage`).
    func openReleasePage(_ record: UpdateCheckRecord) {
        open(Self.releasePage(version: record.latest))
    }

    /// The banner's Release Page, for whatever it shows: the page of the
    /// release that failed, or of the one on offer.
    func openNoticePage() {
        open(Self.releasePage(version: notice?.version ?? availableVersion))
    }

    var arguments: [String] {
        [Flag.json, Flag.checkUpdate, Flag.currentVersion, appVersion]
    }

    private func check(answering: Bool) async {
        guard !isChecking else {
            if answering { answerWhenDone = true }
            return
        }
        isChecking = true
        defer { isChecking = false }

        var record: UpdateCheckRecord?
        var failure: String?
        do {
            let result = try await runner.runOnce(arguments: arguments)
            record = result.stdout.buildEvents().lazy.compactMap { event -> UpdateCheckRecord? in
                if case let .updateCheck(record) = event { return record }
                return nil
            }.first
            if record == nil, result.exitCode != 0 { failure = result.failureSummary }
        } catch {
            failure = error.localizedDescription
        }
        let says = answering || answerWhenDone
        answerWhenDone = false

        guard let record, Self.isUsable(record) else {
            // Quiet unless someone asked: the banner keeps what it had.
            let unreadable = record?.ok == true && record?.latest != nil
            if says {
                answer = .failed(unreadable ? Self.unreadableVersion : record?.message ?? failure ?? Answer.noAnswer)
            }
            return
        }
        defaults.set(now(), forKey: Key.lastChecked)
        if let data = try? JSONEncoder().encode(record) {
            defaults.set(data, forKey: Key.lastAnswer)
        }
        lastAnswer = record
        if says { answer = record.updateAvailable ? .available(record) : .upToDate }
    }

    // MARK: Updating

    /// The download on its own: with the second switch on, for a newer
    /// release a check found, once per version per launch, and never for a
    /// version that already failed (`Key.failedVersion`).
    func startDownloadIfWanted() {
        guard autoInstall, host != nil, let latest = availableVersion,
              !triedOnItsOwn.contains(latest),
              defaults.string(forKey: Key.failedVersion) != latest
        else { return }
        startDownload(userAsked: false)
    }

    /// The banner's and the alert's Update: download and check it, then
    /// restart into it; or restart now, when it's ready.
    func update() {
        startDownload(userAsked: true)
    }

    /// Start downloading the offered release, unless that's already under
    /// way. Returns the download's task, for a test to wait on.
    @discardableResult
    func startDownload(userAsked asked: Bool) -> Task<Void, Never>? {
        guard let host, let latest = availableVersion, !installPending else { return nil }
        switch phase {
        case let .downloading(version, _) where version == latest:
            if asked { userAsked = true }
            return downloadTask
        case let .verifying(version) where version == latest:
            if asked { userAsked = true }
            return downloadTask
        case let .ready(version) where version == latest && ready?.generation == generation:
            if asked {
                userAsked = true
                restartToUpdate()
            }
            return nil
        case .installing:
            return nil
        default:
            break
        }
        // A new download: install rights go until it earns them, and the one
        // it replaces is stopped (the CLI deletes what it had).
        downloadTask?.cancel()
        generation += 1
        let mine = generation
        ready = nil
        userAsked = asked
        triedOnItsOwn.insert(latest)
        phase = .downloading(version: latest, fraction: nil)
        let task = Task { [weak self] in
            guard let self else { return }
            await self.download(latest, generation: mine, host: host)
        }
        downloadTask = task
        return task
    }

    func downloadArguments(_ host: SelfUpdateHost) -> [String] {
        [Flag.json, Flag.downloadUpdate, Flag.currentVersion, appVersion, Flag.appPath, host.appPath]
    }

    private func download(_ version: String, generation mine: Int, host: SelfUpdateHost) async {
        var end: UpdateDownloadRecord?
        var failure: String?
        do {
            let result = try await runner.stream(arguments: downloadArguments(host)) { [weak self] event in
                guard let self, self.generation == mine, case let .updateProgress(step) = event else { return }
                self.note(step, version: version)
            }
            end = result.events.lazy.compactMap { event -> UpdateDownloadRecord? in
                if case let .updateDownloadEnd(record) = event { return record }
                return nil
            }.first
            if end == nil, result.exitCode != 0 { failure = result.failureSummary }
        } catch {
            failure = error.localizedDescription
        }
        // A newer download took over while this one ran: it neither grants
        // install rights nor takes them back.
        guard generation == mine else { return }
        downloadTask = nil
        guard let end, end.ok, let path = end.appPath, let got = end.version else {
            if let reason = end?.reason, Self.refusedForWhatItIs.contains(reason) {
                defaults.set(version, forKey: Key.failedVersion)
            }
            phase = .failed(version: version, message: end?.message ?? failure ?? Answer.noAnswer)
            return
        }
        ready = (mine, got, path)
        phase = .ready(version: got)
        // Asked for: restart into it now, unless work is running. Then the
        // banner's Restart to Update, or the next quit, installs it.
        if userAsked, !host.hold.isHolding { restartToUpdate() }
    }

    /// A progress event, while the download it is about runs. One that lands
    /// late (the runner's callbacks can) changes nothing.
    private func note(_ step: UpdateProgressRecord, version: String) {
        switch phase {
        case .downloading, .verifying: break
        default: return
        }
        phase = step.step == UpdateProgressRecord.download
            ? .downloading(version: version, fraction: step.fraction)
            : .verifying(version: version)
    }

    /// The banner's Restart to Update: quit, which installs the checked
    /// download on the way out and opens the new Graft (`handOver`). With
    /// work running, the quit asks first, as every quit does.
    func restartToUpdate() {
        guard let host, let ready, ready.generation == generation, !installPending else { return }
        relaunchRequested = true
        host.terminate()
    }

    func installArguments(appPath: String, host: SelfUpdateHost, relaunch: Bool) -> [String] {
        var args = [
            Flag.json, Flag.installUpdate, appPath, Flag.appPath, host.appPath,
            Flag.waitPid, String(host.pid),
        ]
        if relaunch { args.append(Flag.relaunch) }
        return args
    }

    /// `QuitHandover`: the quit is going ahead with nothing held. Install
    /// the checked download on the way out, when it's this generation's and
    /// installing on quit is wanted (the switch, or Update, or Restart to
    /// Update), by starting the helper; then let the quit go on.
    func handOver(_ done: @escaping @MainActor (Bool) -> Void) -> Bool {
        guard let host, let ready, ready.generation == generation, !installPending,
              autoInstall || userAsked || relaunchRequested,
              !host.hold.isHolding
        else {
            relaunchRequested = false
            return false
        }
        installPending = true
        let relaunch = relaunchRequested
        phase = .installing(version: ready.version)
        // Written before the helper exists: once Graft has quit, the next
        // launch is the only one left to say whether it took.
        if let data = try? JSONEncoder().encode(Attempt(version: ready.version, from: appVersion)) {
            defaults.set(data, forKey: Key.attempt)
        }
        let args = installArguments(appPath: ready.appPath, host: host, relaunch: relaunch)
        let once = Once(done)
        Task { [timeout = handoverTimeout] in
            try? await Task.sleep(for: timeout)
            once.call(true)
        }
        Task { [weak self] in
            guard let self else { return once.call(true) }
            guard let failure = await self.install(args) else { return once.call(true) }
            // Too late: the quit went ahead without waiting.
            guard !once.called else { return }
            self.installPending = false
            self.relaunchRequested = false
            self.ready = nil
            self.defaults.removeObject(forKey: Key.attempt)
            self.defaults.set(ready.version, forKey: Key.failedVersion)
            self.phase = .failed(version: ready.version, message: failure)
            // A quit still quits. Restart to Update stays open, to say why.
            once.call(!relaunch)
        }
        return true
    }

    /// The handover's `done`, called once: by the install, or by the timeout.
    @MainActor
    private final class Once {
        private let done: @MainActor (Bool) -> Void
        private(set) var called = false

        init(_ done: @escaping @MainActor (Bool) -> Void) { self.done = done }

        func call(_ quit: Bool) {
            guard !called else { return }
            called = true
            done(quit)
        }
    }

    func quitCanceled() {
        relaunchRequested = false
    }

    /// Start the helper: nil once it's started, else why not.
    private func install(_ args: [String]) async -> String? {
        do {
            let result = try await runner.runOnce(arguments: args)
            let record = result.stdout.buildEvents().lazy.compactMap { event -> UpdateInstallRecord? in
                if case let .updateInstall(record) = event { return record }
                return nil
            }.first
            if let record, record.ok { return nil }
            return record?.message ?? (result.exitCode != 0 ? result.failureSummary : Answer.noAnswer)
        } catch {
            return error.localizedDescription
        }
    }
}
