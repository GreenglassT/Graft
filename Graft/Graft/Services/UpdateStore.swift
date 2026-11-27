import Foundation
import Combine

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
/// downloads the new Graft.zip (Graft-Intel.zip for the Intel build) and
/// checks it (`app_update` in the CLI).
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
///   version says the install didn't take, and links the release page;
///   unless the helper's log says it installed it after the attempt
///   (`installLogged`): then the old version was put back by hand since.
///
/// **Check for Updates…'s alert** (`answerAlert`) says what the banner says
/// about the release it found, as things stood once any download the check
/// started had started, and is fixed while it's open. It never offers
/// Update for a download already under way.
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
final class UpdateStore: ObservableObject, QuitHandover {
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
        /// A download the CLI refused because this Mac's macOS is older than
        /// the release needs (`FloorRefusal`).
        static let floorRefusal = "updateInstall.macOSFloor"
        /// The release whose "needs a newer macOS" notice was dismissed.
        static let dismissedFloor = "updateCheck.dismissedMacOSFloor"
    }

    /// A download refused for this Mac's macOS: the release, and the macOS
    /// it needs. It lapses once this Mac's macOS is that or later
    /// (`macOSVersion`), or a check offers the release with no floor.
    struct FloorRefusal: Codable, Equatable {
        let version: String
        let needs: String
    }

    /// Whether macOS `have` ([15, 5] for 15.5) is `floor` ("15.4") or later:
    /// number by number, a missing one counting as 0.
    nonisolated static func macOS(_ have: [Int], meets floor: String) -> Bool {
        let need = floor.split(separator: ".").map { Int($0) ?? 0 }
        for index in 0..<max(have.count, need.count) {
            let mine = index < have.count ? have[index] : 0
            let wanted = index < need.count ? need[index] : 0
            if mine != wanted { return mine > wanted }
        }
        return true
    }

    /// A newer release this Mac's macOS can't run, and the line that says
    /// so (`macOSFloor`).
    struct MacOSFloor: Equatable {
        let version: String
        let line: String
    }

    /// The line for a release that needs a newer macOS than this Mac's.
    nonisolated static func needsMacOSLine(version: String, needs: String, current: String) -> String {
        "Graft \(version) needs macOS \(needs) or later; this Mac stays on Graft \(current)."
    }

    /// The macOS a refused download needs, from the CLI's `needs_macos`, or
    /// else from its sentence ("needs macOS 15.0 or later"), and only for
    /// `not-for-this-mac`. Nil for that reason's other meaning, a release
    /// with no zip of this build yet. Plain, or nil: it goes in a sentence
    /// and is kept in `defaults`.
    nonisolated static func macOSFloor(of end: UpdateDownloadRecord) -> String? {
        guard end.reason == "not-for-this-mac" else { return nil }
        if let needs = end.needsMacos { return isPlainVersion(needs) ? needs : nil }
        guard let message = end.message,
              let range = message.range(of: #"needs macOS [0-9]+(\.[0-9]+)*"#, options: .regularExpression)
        else { return nil }
        let needs = String(message[range].dropFirst("needs macOS ".count))
        return isPlainVersion(needs) ? needs : nil
    }

    /// An install handed over at a quit: the version, the version it
    /// replaces, and when (nil in one that 2.0.2 wrote).
    struct Attempt: Codable, Equatable {
        let version: String
        let from: String
        var at: Date?
    }

    /// What Help › Check for Updates… found.
    enum Answer: Equatable {
        case upToDate
        case available(UpdateCheckRecord)
        /// The CLI's sentence, or the runner's when the CLI couldn't say.
        case failed(String)

        /// The sentence for a run that said nothing at all.
        static let noAnswer = "The command-line tool gave no answer."
    }

    /// What Check for Updates…'s alert shows (`alert(for:)`).
    struct CheckAlert: Equatable {
        /// The alert's button that updates (`perform`), as the banner's:
        /// Update and Try Again are its Update, Restart to Update its
        /// Restart to Update.
        enum Action: Equatable {
            /// `update()`: download it, check it, and restart into it; or
            /// join the download already running.
            case update
            /// `update()` again, after a failed try that trying again can
            /// help (`Phase.failed`'s `retryable`).
            case tryAgain
            /// `restartToUpdate()`: quit, install the checked download, and
            /// open it. Asked for this quit only: if the quit is canceled,
            /// a later one installs it only as the second switch says.
            case restartToUpdate

            var title: String {
                switch self {
                case .update: return "Update"
                case .tryAgain: return "Try Again"
                case .restartToUpdate: return "Restart to Update"
                }
            }
        }

        let title: String
        let message: String
        /// The button that updates, if any.
        let action: Action?
        /// The release Open Release Page opens; nil for no such button.
        let release: UpdateCheckRecord?
        /// The cancel button: Later beside an offer, OK otherwise.
        let cancel: String
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
        /// `retryable`: trying again can help (`retryHelps`), so Check for
        /// Updates… offers Try Again.
        case failed(version: String, message: String, retryable: Bool)
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
            /// The sentence, and `Phase.failed`'s `retryable`.
            case failed(String, retryable: Bool)
            /// A newer release this Mac's macOS can't run, its line
            /// (`MacOSFloor`): information, which can be dismissed.
            case needsNewerMacOS(String)
        }

        let version: String
        let state: State
    }

    /// The install failed after Graft quit: the next launch is still the
    /// old version. It names the download for this Mac
    /// (`MacSupport.manualDownload`).
    static func installDidNotTake(_ support: MacSupport) -> String {
        "Graft couldn't replace itself, so it's still the version "
            + "you had. Download the new version from the release page (\(support.manualDownload)) "
            + "and replace Graft.app in Applications. The update "
            + "log is in ~/Library/Logs/Graft/update.log."
    }

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
    /// `architecture` is a download of the other build of Graft, or one
    /// with no code for this build's processor. `no-team` and `mixed-build`
    /// are this copy's: it has no Developer ID team to trust updates from
    /// (`app_update.own_team`), or can't tell which build it is
    /// (`app_update.own_flavor`), and no retry changes either.
    static let refusedForWhatItIs: Set<String> = [
        "no-asset", "digest", "archive", "bundle", "architecture", "signature", "requirement",
        "notarization", "not-newer", "no-team", "mixed-build",
    ]

    /// Of `refusedForWhatItIs`, the one where what arrived may not be what
    /// was published, so another try can work: bytes that didn't match
    /// GitHub's SHA-256 or ran past its size (`digest`: a corrupt or cut-off
    /// transfer). Check for Updates… offers Try Again for it. It still isn't
    /// downloaded again on its own: a release that is broken for everyone
    /// would cost a whole download each time.
    ///
    /// Not `archive`: it comes only after the bytes matched GitHub's SHA-256
    /// and there was room to unpack them, so another download of the same
    /// bytes fails the same way (the 2.2 review).
    static let worthTryingAgain: Set<String> = ["digest"]

    /// The CLI's reasons for a failure that trying again can't help with
    /// this launch, so Check for Updates… doesn't offer Try Again: those
    /// `refusedForWhatItIs` names (the same download meets the same
    /// refusal) but `worthTryingAgain`'s, and where this copy is, which no
    /// retry changes: run from a translocated copy, in a folder this account
    /// can't change, or not Graft.app. Their sentences say what to do
    /// instead.
    ///
    /// Not a release with no zip of this build yet (`not-for-this-mac`: "no
    /// update for this Mac yet"): the zip can be added to the release at any
    /// time, and Try Again then gets it. Nor is it remembered as refused, so
    /// a later check downloads it on its own. The same reason for a release
    /// that needs a newer macOS than this Mac's (`macOSFloor(of:)`) is no
    /// failure at all, and never reaches this: no try helps until macOS is
    /// upgraded, so it's remembered (`floorRefusal`), not downloaded again,
    /// and said once, as information (`macOSFloor`), until this Mac's macOS
    /// meets the floor or a check offers the release with no floor.
    static let retryCantHelp: Set<String> = refusedForWhatItIs.subtracting(worthTryingAgain).union([
        "translocated", "not-writable", "not-graft",
    ])

    /// Whether trying again can help with a failure for `reason`. True for
    /// none: the CLI didn't say why (it stopped, or couldn't run).
    static func retryHelps(_ reason: String?) -> Bool {
        reason.map { !retryCantHelp.contains($0) } ?? true
    }

    let runner: CLIRunner
    /// This copy's CFBundleShortVersionString: what the CLI compares against.
    let appVersion: String
    /// This Mac and this copy of Graft: which download a by-hand update
    /// names (`MacSupport.manualDownload`).
    let support: MacSupport
    /// This Mac's macOS, whole ([15, 5] for 15.5): what a release's macOS
    /// floor is held against, since a floor can be a minor version.
    let macOSVersion: [Int]
    /// Nil when this copy can't update itself (`SelfUpdateHost`).
    let host: SelfUpdateHost?
    private let defaults: UserDefaults
    private let now: () -> Date
    private let open: (URL) -> Void
    /// The helper's log, ~/Library/Logs/Graft/update.log (`app_update.log_path`
    /// in the CLI): read at launch only, when an attempt needs a verdict.
    private let updateLog: URL

    /// Settings › Updates' first switch: whether the daily check runs. On
    /// unless turned off. Off also takes the offer off the banner; Check for
    /// Updates… still works.
    @Published var dailyCheck: Bool {
        didSet { defaults.set(dailyCheck, forKey: Key.dailyCheck) }
    }

    /// Settings › Updates' second switch: download a newer Graft on its own,
    /// and install it when Graft quits. On unless turned off. Off, the banner
    /// offers Update instead.
    @Published var autoInstall: Bool {
        didSet {
            defaults.set(autoInstall, forKey: Key.autoInstall)
            if autoInstall { startDownloadIfWanted() }
        }
    }

    /// The last successful answer about this version, nil before there is one.
    @Published private(set) var lastAnswer: UpdateCheckRecord?
    @Published private(set) var isChecking = false
    /// What Check for Updates… found, until its alert is dismissed
    /// (`dismissAnswer`).
    @Published private(set) var answer: Answer?
    /// What its alert shows: `alert(for: answer)`, as things stood once the
    /// check was done and any download it starts had started. Fixed while
    /// it's open, so no button changes under the pointer (the banner
    /// follows the phase): its words stay true as the phase moves on, and
    /// its button (`perform`) is the banner's.
    @Published private(set) var answerAlert: CheckAlert?
    @Published private(set) var phase: Phase = .idle
    /// The download the CLI refused for this Mac's macOS, for this macOS.
    @Published private(set) var floorRefusal: FloorRefusal?
    /// The release whose "needs a newer macOS" notice was dismissed: not
    /// shown again for it.
    @Published private(set) var dismissedFloor: String?
    /// From the quit taking the install until the helper is started (or
    /// couldn't be).
    @Published private(set) var installPending = false

    /// Check for Updates… was chosen while a daily check was running: that
    /// check's result is its answer.
    /// Unpublished: a change to it alone redraws nothing.
    private var answerWhenDone = false
    /// The newest download's number (`ready` must carry it to install).
    /// Unpublished: a change to it alone redraws nothing.
    private(set) var generation = 0
    /// The checked download, and the download it came from.
    /// Unpublished: a change to it alone redraws nothing.
    private var ready: (generation: Int, version: String, appPath: String)?
    /// The download now running was asked for (Update): restart into it
    /// when it's ready, and install it on any quit.
    /// Unpublished: a change to it alone redraws nothing.
    private var userAsked = false
    /// Restart to Update asked for this quit: open Graft again afterward.
    /// Unpublished: a change to it alone redraws nothing.
    private var relaunchRequested = false
    /// Versions downloaded on their own this launch: once each.
    /// Unpublished: a change to it alone redraws nothing.
    private var triedOnItsOwn: Set<String> = []
    /// Unpublished: a change to it alone redraws nothing.
    private var downloadTask: Task<Void, Never>?
    /// How long a quit waits for `--install-update` to start the helper.
    /// It takes a second or two; one that never answers must not keep Graft
    /// from quitting. The attempt is written down first, so the next launch
    /// still says whether it took. A test shortens it.
    /// Unpublished: a change to it alone redraws nothing.
    var handoverTimeout: Duration = .seconds(15)

    /// Every dependency is a parameter: `GraftApp` passes the real
    /// preferences, clock, browser, update log and host, a test its own
    /// (`NoRealDefaultsTests`). No host, no self-update: the banner then
    /// links the release page, as before 2.0.2. `support` is `MacSupport.thisMac`
    /// in the app; a test's default is everything, on macOS 14.
    /// `macOSVersion` is `ProcessInfo`'s in the app; with none, `support`'s
    /// major version alone.
    init(
        runner: CLIRunner, defaults: UserDefaults, now: @escaping () -> Date,
        appVersion: String, open: @escaping (URL) -> Void, updateLog: URL,
        host: SelfUpdateHost? = nil, support: MacSupport = .appleSilicon,
        macOSVersion: [Int]? = nil
    ) {
        self.runner = runner
        self.support = support
        let macOSVersion = macOSVersion ?? [support.macOSMajorVersion]
        self.macOSVersion = macOSVersion
        self.defaults = defaults
        self.now = now
        self.appVersion = appVersion
        self.open = open
        self.updateLog = updateLog
        self.host = host
        dailyCheck = defaults.object(forKey: Key.dailyCheck) as? Bool ?? true
        autoInstall = defaults.object(forKey: Key.autoInstall) as? Bool ?? true
        if let data = defaults.data(forKey: Key.lastAnswer) {
            // Anyone running as this user can write it: one that doesn't
            // decode, or names a version that isn't plain, is dropped.
            if let saved = try? JSONDecoder().decode(UpdateCheckRecord.self, from: data), Self.isUsable(saved) {
                if let needs = saved.needsMacos, Self.isPlainVersion(needs),
                   Self.macOS(macOSVersion, meets: needs) {
                    // Saved before this Mac's macOS met the release's floor:
                    // asked again now, rather than the banner saying it needs
                    // a newer macOS for up to a day.
                    defaults.removeObject(forKey: Key.lastAnswer)
                    defaults.removeObject(forKey: Key.lastChecked)
                } else if saved.current == appVersion {
                    lastAnswer = saved
                }
            } else {
                defaults.removeObject(forKey: Key.lastAnswer)
            }
        }
        if let data = defaults.data(forKey: Key.floorRefusal) {
            // As `lastAnswer`: written by anyone running as this user, so
            // only plain versions; and only while this Mac's macOS is older
            // than the floor.
            if let saved = try? JSONDecoder().decode(FloorRefusal.self, from: data),
               Self.isPlainVersion(saved.version), Self.isPlainVersion(saved.needs),
               !Self.macOS(macOSVersion, meets: saved.needs) {
                floorRefusal = saved
            } else {
                defaults.removeObject(forKey: Key.floorRefusal)
            }
        }
        dismissedFloor = defaults.string(forKey: Key.dismissedFloor).flatMap { Self.isPlainVersion($0) ? $0 : nil }
        if let data = defaults.data(forKey: Key.attempt) {
            defaults.removeObject(forKey: Key.attempt)
            // Still the version it was to replace: the helper didn't install
            // it, unless its log says it did after the attempt, and this copy
            // was put back by hand since (a downgrade). Any other version
            // means it did, or someone else did. Its version goes in the
            // banner and its link, so only a plain one.
            if let attempt = try? JSONDecoder().decode(Attempt.self, from: data),
               attempt.from == appVersion, Self.isPlainVersion(attempt.version),
               !Self.installLogged(attempt, in: updateLog) {
                defaults.set(attempt.version, forKey: Key.failedVersion)
                // Its sentence says to download it by hand: no Try Again.
                phase = .failed(version: attempt.version, message: Self.installDidNotTake(support), retryable: false)
            }
        }
    }

    /// Whether the helper's log says it installed `attempt`'s version at or
    /// after the time the attempt was written down. False when it can't
    /// tell: an attempt with no time (2.0.2's), or no log that's a file. Only
    /// a line in the helper's own form, its time with an offset, counts
    /// (`loggedTime`).
    ///
    /// The helper's line for an install that worked (`app_update.HELPER`,
    /// pinned by tests/test_app_update_helper.py) is its time, to the
    /// second (`loggedTime`), then "installed Graft <version> at <app>; …".
    /// The helper is the one the attempt's own version started, so it's the
    /// same line this version reads. It's read where it begins, which is
    /// a line's start unless a tool's output before it had no newline
    /// (`loggedTimes`).
    nonisolated static func installLogged(_ attempt: Attempt, in log: URL) -> Bool {
        guard let at = attempt.at, let tail = tail(of: log) else { return false }
        // Whole seconds: an install in the attempt's own second counts.
        let earliest = at.timeIntervalSinceReferenceDate.rounded(.down)
        let said = " installed Graft \(attempt.version) at "
        return lines(of: tail).contains { line in
            loggedTimes(in: line, saying: said).contains { $0.timeIntervalSinceReferenceDate >= earliest }
        }
    }

    /// The length of the helper's time and offset ("2026-11-24 09:30:15
    /// +1400"), which come right before what it says.
    nonisolated static let stampLength = 25

    /// When the helper said `said` in `line`: each place it's there whose
    /// `stampLength` characters before it are the helper's time and offset
    /// (`loggedTime`). At the line's start, as the helper writes it, or after
    /// a tool's output that ended with no newline: 2.3's helper kept
    /// lsregister's "failed to scan …: -10814 from spotlight" so, and its
    /// "installed" line ran on from it (the 2.3.1 review). Its 2.3.1 helper
    /// starts a line of its own whatever came before.
    nonisolated static func loggedTimes(in line: String, saying said: String) -> [Date] {
        var times: [Date] = []
        var from = line.startIndex
        while let found = line.range(of: said, range: from..<line.endIndex) {
            if let start = line.index(found.lowerBound, offsetBy: -stampLength, limitedBy: line.startIndex),
               let when = loggedTime(of: String(line[start...]), saying: said) {
                times.append(when)
            }
            from = found.upperBound
        }
        return times
    }

    /// When a line of the helper's log was written, if what follows its
    /// time and offset is `said`; nil for any other line.
    ///
    /// The time has its offset from UTC ("2026-11-24 09:30:15 +1400",
    /// `date '+%Y-%m-%d %H:%M:%S %z'`), as the helper has written it since
    /// 2.1, and is read in that offset: the helper's zone is the CLI's,
    /// which has any TZ the user's shell profile sets, and can differ from
    /// this app's. Between the time and `said`, only " ±hhmm".
    ///
    /// **A time with no offset is never read.** Only 2.0.2's helper wrote
    /// those ("2026-11-24 09:30:15"), for an update its own app started,
    /// whose attempt has no time and is never compared (`installLogged`);
    /// every attempt with a time is a 2.1 or later app's, whose helper
    /// writes the offset. Read in this Mac's zone, which needn't be the one
    /// it was written in, an old 2.0.2 line could land after a later
    /// attempt and hide its failure: 2.0.2's "installed Graft 2.2", then
    /// 2.1 put back by hand and its own update to 2.2 failing (the 2.1
    /// review).
    nonisolated static func loggedTime(of line: String, saying said: String) -> Date? {
        let time = line.prefix(19)
        guard time.count == 19 else { return nil }
        let rest = line.dropFirst(19)
        let mark = Array(rest.prefix(6))
        let digits = mark.dropFirst(2).compactMap { $0.isASCII ? $0.wholeNumberValue : nil }
        guard mark.count == 6, mark[0] == " ", mark[1] == "+" || mark[1] == "-", digits.count == 4,
              rest.dropFirst(6).hasPrefix(said)
        else { return nil }
        let hours = digits[0] * 10 + digits[1], minutes = digits[2] * 10 + digits[3]
        guard hours < 24, minutes < 60 else { return nil }
        // Seconds east of UTC.
        let offset = (mark[1] == "+" ? 1 : -1) * (hours * 60 * 60 + minutes * 60)
        let stamp = DateFormatter()
        stamp.locale = Locale(identifier: "en_US_POSIX")
        // Read as UTC, then moved back by the offset.
        stamp.timeZone = .gmt
        stamp.dateFormat = "yyyy-MM-dd HH:mm:ss"
        guard let read = stamp.date(from: String(time)) else { return nil }
        return read - TimeInterval(offset)
    }

    /// `data`'s lines, as UTF-8: split at each LF byte, each without the CR
    /// before it in a line that ended CRLF. Bytes, not
    /// `String.split(separator: "\n")`: Swift reads "\r\n" as one Character,
    /// which isn't "\n", so a CRLF line would run on into the next.
    nonisolated static func lines(of data: Data) -> [String] {
        data.split(separator: 0x0A, omittingEmptySubsequences: false).map { line in
            String(decoding: line.last == 0x0D ? line.dropLast() : line, as: UTF8.self)
        }
    }

    /// The last MiB of the log: the newest lines (the helper appends to it
    /// on every update).
    nonisolated static let tailLimit = 1 << 20

    /// `pread(2)`'s shape: read up to `count` bytes at `offset` into
    /// `buffer`; the count read, 0 at the end, or -1 with `errno` set.
    typealias PositionalRead = (_ fd: Int32, _ buffer: UnsafeMutableRawPointer, _ count: Int, _ offset: off_t) -> Int

    /// The last `tailLimit` bytes of `log`, or nil unless it's a regular
    /// file. One open, which follows no symlink (`O_NOFOLLOW`) and doesn't
    /// wait for a FIFO's writer at launch (`O_NONBLOCK`); then the type of
    /// what that open found (`fstat`), and the read, from the same
    /// descriptor. So nothing put at the path between a look and the read
    /// is what's read.
    ///
    /// A read interrupted by a signal (EINTR) is asked again, and a short
    /// one carried on from where it stopped. `read` is `pread`, unless a
    /// test passes its own to interrupt or shorten reads.
    nonisolated static func tail(of log: URL, read readAt: PositionalRead = { Darwin.pread($0, $1, $2, $3) }) -> Data? {
        let fd = Darwin.open(log.path, O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_mode & S_IFMT == S_IFREG else { return nil }
        let size = Int(info.st_size)
        let start = max(size - tailLimit, 0)
        var bytes = [UInt8](repeating: 0, count: size - start)
        var got = 0
        while got < bytes.count {
            let read = bytes.withUnsafeMutableBytes { buffer in
                readAt(fd, buffer.baseAddress! + got, buffer.count - got, off_t(start + got))
            }
            if read < 0, errno == EINTR { continue }
            guard read >= 0 else { return nil }
            if read == 0 { break }  // it got shorter since the fstat
            got += read
        }
        return Data(bytes[..<got])
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

    /// A newer release this Mac's macOS can't run: from the last check
    /// (`needs_macos`), or from a download the CLI refused for it
    /// (`floorRefusal`). Nil once neither applies.
    var macOSFloor: MacOSFloor? {
        if let lastAnswer, let latest = lastAnswer.latest, let needs = lastAnswer.needsMacos,
           Self.isPlainVersion(needs), !Self.macOS(macOSVersion, meets: needs) {
            return MacOSFloor(version: latest,
                              line: Self.needsMacOSLine(version: latest, needs: needs, current: appVersion))
        }
        if let refused = floorRefusal, refused.version == availableVersion,
           !Self.macOS(macOSVersion, meets: refused.needs) {
            return MacOSFloor(version: refused.version,
                              line: Self.needsMacOSLine(version: refused.version, needs: refused.needs,
                                                        current: appVersion))
        }
        return nil
    }

    /// The banner's dismiss on "needs a newer macOS": not shown again for
    /// that release. Check for Updates… still says it.
    func dismissMacOSFloor() {
        guard let floor = macOSFloor else { return }
        dismissedFloor = floor.version
        defaults.set(floor.version, forKey: Key.dismissedFloor)
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
        case let .failed(version, message, retryable):
            // Until a newer release than the one that failed is offered.
            if availableVersion == nil || availableVersion == version {
                return Notice(version: version, state: .failed(message, retryable: retryable))
            }
        }
        // Information rather than an offer: nothing to update to here.
        if let floor = macOSFloor {
            guard dailyCheck, floor.version != dismissedFloor else { return nil }
            return Notice(version: floor.version, state: .needsNewerMacOS(floor.line))
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
        var found: Answer?
        if Self.isDue(dailyCheck: dailyCheck, lastChecked: lastChecked, now: now()) {
            found = await check(answering: false)
        }
        startDownloadIfWanted()
        if let found { present(found) }
    }

    /// Help › Check for Updates…: now, whatever the switch says, and always
    /// with an answer.
    func checkNow() async {
        let found = await check(answering: true)
        startDownloadIfWanted()
        if let found { present(found) }
    }

    /// Check for Updates…'s answer, and its alert as things stand now:
    /// after `startDownloadIfWanted`, so a download the check started is
    /// already under way and the alert doesn't offer it again.
    private func present(_ found: Answer) {
        answer = found
        answerAlert = alert(for: found)
    }

    /// The alert's dismissal, whichever button closed it.
    func dismissAnswer() {
        answer = nil
        answerAlert = nil
    }

    /// The alert's button that updates, as the banner's: Update and Try
    /// Again are `update()`, and Restart to Update is `restartToUpdate()`.
    /// Not `update()`, which would also ask for every later quit to install
    /// it, even after this quit was canceled.
    func perform(_ action: CheckAlert.Action) {
        switch action {
        case .update, .tryAgain: update()
        case .restartToUpdate: restartToUpdate()
        }
    }

    /// What Check for Updates…'s alert says about `answer`, from where
    /// updating is now. About a newer release, it says what the banner says
    /// about it (`notice`), so the two never disagree. While a download runs
    /// or is checked it offers nothing to start, only OK and Open Release
    /// Page, in words that stay true however the download ends. Once that
    /// download is ready it offers Restart to Update, as the banner does;
    /// after a failed one, Try Again, when trying again can help.
    func alert(for answer: Answer) -> CheckAlert {
        switch answer {
        case .upToDate:
            return CheckAlert(title: "You're up to date", message: "Graft \(appVersion) is the newest version.",
                              action: nil, release: nil, cancel: "OK")
        case let .failed(why):
            return CheckAlert(title: "Couldn't check for updates", message: why,
                              action: nil, release: nil, cancel: "OK")
        case let .available(record):
            let version = record.latest ?? ""
            let have = "You have Graft \(appVersion)."
            // Nothing to update to: what it needs, and its page. Said even
            // when the banner's notice was dismissed: this was asked.
            if let floor = macOSFloor, floor.version == version {
                return CheckAlert(title: "Graft \(version) needs a newer macOS", message: floor.line,
                                  action: nil, release: record, cancel: "OK")
            }
            let banner = notice.flatMap { $0.version == version ? $0.state : nil }
            switch banner {
            case .downloading?, .verifying?:
                return CheckAlert(
                    title: "Graft \(version) is available",
                    message: "\(have) Graft has started downloading it in the background, and checks that "
                        + "it's Graft, signed by its developer and notarized by Apple, before it installs it. "
                        + "The banner at the bottom of the window shows how it's going.",
                    action: nil, release: record, cancel: "OK"
                )
            case let .ready(installsOnQuit)?:
                return CheckAlert(
                    title: "Graft \(version) is ready",
                    message: installsOnQuit
                        ? "\(have) Graft \(version) is downloaded and checked, and installs when you quit Graft. "
                            + "Restart to Update installs it now."
                        : "\(have) Graft \(version) is downloaded and checked. Restart to Update quits Graft, "
                            + "puts it in place of this one, and opens it.",
                    action: .restartToUpdate, release: record, cancel: "Later"
                )
            case .installing?:
                return CheckAlert(title: "Installing Graft \(version)…", message: "Graft installs it as it quits.",
                                  action: nil, release: nil, cancel: "OK")
            case let .failed(why, retryable)?:
                return CheckAlert(title: "Graft couldn't update to \(version)", message: why,
                                  action: canUpdate && retryable ? .tryAgain : nil, release: record, cancel: "Later")
            case .available?, .needsNewerMacOS?, nil:
                guard canUpdate else {
                    return CheckAlert(
                        title: "Graft \(version) is available",
                        message: "\(have) Download the new version from its release page "
                            + "(\(support.manualDownload)), then replace Graft.app in "
                            + "Applications. Your settings, apps and builds carry over.",
                        action: nil, release: record, cancel: "Later"
                    )
                }
                return CheckAlert(
                    title: "Graft \(version) is available",
                    message: "\(have) Update downloads the new version from GitHub, checks that it's Graft, "
                        + "signed by its developer and notarized by Apple, and restarts Graft. Your settings, "
                        + "apps and builds carry over.",
                    action: .update, release: record, cancel: "Later"
                )
            }
        }
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

    /// Runs the check. Returns what Check for Updates… says, or nil when
    /// nobody asked: the daily check is quiet, whatever it found.
    private func check(answering: Bool) async -> Answer? {
        guard !isChecking else {
            if answering { answerWhenDone = true }
            return nil
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
            guard says else { return nil }
            return .failed(unreadable ? Self.unreadableVersion : record?.message ?? failure ?? Answer.noAnswer)
        }
        defaults.set(now(), forKey: Key.lastChecked)
        if let data = try? JSONEncoder().encode(record) {
            defaults.set(data, forKey: Key.lastAnswer)
        }
        lastAnswer = record
        // The check offers the release a download was refused for, and names
        // no floor: the refusal is out of date (this Mac's macOS was updated,
        // or the CLI knows better), so the release can be got again.
        if let refused = floorRefusal, record.latest == refused.version,
           record.updateAvailable, record.needsMacos == nil {
            floorRefusal = nil
            defaults.removeObject(forKey: Key.floorRefusal)
        }
        guard says else { return nil }
        // A release this Mac can't run is no "up to date": `alert(for:)`
        // says what it needs.
        let needsNewerMacOS = record.needsMacos.map(Self.isPlainVersion) == true
        return record.updateAvailable || needsNewerMacOS ? .available(record) : .upToDate
    }

    // MARK: Updating

    /// The download on its own: with the second switch on, for a newer
    /// release a check found, once per version per launch, and never for a
    /// version that already failed (`Key.failedVersion`).
    func startDownloadIfWanted() {
        guard autoInstall, host != nil, let latest = availableVersion,
              !triedOnItsOwn.contains(latest),
              defaults.string(forKey: Key.failedVersion) != latest,
              macOSFloor == nil
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
        guard let host, let latest = availableVersion, !installPending, macOSFloor?.version != latest
        else { return nil }
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
            // This Mac's macOS is too old for it: no failure, and no warning.
            // Kept until this Mac's macOS meets the floor, so no launch
            // downloads it again, and the banner says it once (`macOSFloor`).
            if let end, let needs = Self.macOSFloor(of: end) {
                let refused = FloorRefusal(version: version, needs: needs)
                floorRefusal = refused
                if let data = try? JSONEncoder().encode(refused) { defaults.set(data, forKey: Key.floorRefusal) }
                phase = .idle
                return
            }
            if let reason = end?.reason, Self.refusedForWhatItIs.contains(reason) {
                defaults.set(version, forKey: Key.failedVersion)
            }
            phase = .failed(version: version, message: end?.message ?? failure ?? Answer.noAnswer,
                            retryable: Self.retryHelps(end?.reason))
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
        // launch is the only one left to say whether it took. Its time places
        // it in the helper's log (`installLogged`).
        if let data = try? JSONEncoder().encode(Attempt(version: ready.version, from: appVersion, at: now())) {
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
            self.phase = .failed(version: ready.version, message: failure.message,
                                 retryable: Self.retryHelps(failure.reason))
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

    /// Start the helper: nil once it's started, else why not, and the
    /// CLI's reason when it gave one.
    private func install(_ args: [String]) async -> (message: String, reason: String?)? {
        do {
            let result = try await runner.runOnce(arguments: args)
            let record = result.stdout.buildEvents().lazy.compactMap { event -> UpdateInstallRecord? in
                if case let .updateInstall(record) = event { return record }
                return nil
            }.first
            if let record, record.ok { return nil }
            return (record?.message ?? (result.exitCode != 0 ? result.failureSummary : Answer.noAnswer),
                    record?.reason)
        } catch {
            return (error.localizedDescription, nil)
        }
    }
}
