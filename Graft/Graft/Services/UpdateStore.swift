import Foundation
import Observation

private typealias Flag = CLIContract.Flag

/// Whether a newer Graft is out: the main window's banner, Help › Check for
/// Updates…, and Settings › Updates' switch.
///
/// The CLI asks (`--check-update`), as it makes every other request: the app
/// makes no connections of its own. It sends GitHub a generic User-Agent and
/// nothing that identifies anyone, and downloads nothing.
///
/// **The daily rule.** With the switch on (the default), the window checks at
/// launch and then once an hour while the app is open, and each of those runs
/// the CLI only when more than a day has passed since the last *successful*
/// check (`isDue`). So a check that failed — offline, or GitHub limiting
/// requests — is tried again an hour later, not a day.
///
/// **A failed check is quiet.** Nobody asked for the daily one, so it changes
/// nothing on screen: the banner keeps whatever the last successful check
/// found. Only Check for Updates… says it failed.
///
/// The switch, the time of the last successful check and its answer live in
/// `defaults`, so the banner is back after a relaunch without asking again.
/// An answer is kept only for the version it was about: a newer Graft
/// launching drops it.
@MainActor
@Observable
final class UpdateStore {
    /// How long a successful check lasts.
    static let interval: TimeInterval = 24 * 60 * 60
    /// How often the window asks `checkIfDue` while the app is open.
    static let pollInterval: Duration = .seconds(60 * 60)

    /// The `UserDefaults` keys.
    enum Key {
        static let dailyCheck = "updateCheck.daily"
        static let lastChecked = "updateCheck.lastSuccess"
        static let lastAnswer = "updateCheck.lastAnswer"
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

        func message(appVersion: String) -> String {
            switch self {
            case .upToDate:
                return "Graft \(appVersion) is the newest version."
            case .available:
                return "You have Graft \(appVersion). Download the new Graft.zip from its release "
                    + "page, then replace Graft.app in Applications. Your settings, apps and "
                    + "builds carry over."
            case let .failed(why):
                return why
            }
        }
    }

    let runner: CLIRunner
    /// This copy's CFBundleShortVersionString: what the CLI compares against.
    let appVersion: String
    private let defaults: UserDefaults
    private let now: () -> Date
    private let open: (URL) -> Void

    /// Settings › Updates' switch: whether the daily check runs. On unless
    /// turned off. Off also takes the banner down; Check for Updates… still
    /// works.
    var dailyCheck: Bool {
        didSet { defaults.set(dailyCheck, forKey: Key.dailyCheck) }
    }

    /// The last successful answer about this version, nil before there is one.
    private(set) var lastAnswer: UpdateCheckRecord?
    private(set) var isChecking = false
    /// What Check for Updates… found, until its alert is dismissed.
    var answer: Answer?
    /// Check for Updates… was chosen while a daily check was running: that
    /// check's result is its answer.
    @ObservationIgnored private var answerWhenDone = false

    /// Every dependency is a parameter: `GraftApp` passes the real
    /// preferences, clock and browser, a test its own (`NoRealDefaultsTests`).
    init(
        runner: CLIRunner, defaults: UserDefaults, now: @escaping () -> Date,
        appVersion: String, open: @escaping (URL) -> Void
    ) {
        self.runner = runner
        self.defaults = defaults
        self.now = now
        self.appVersion = appVersion
        self.open = open
        dailyCheck = defaults.object(forKey: Key.dailyCheck) as? Bool ?? true
        if let data = defaults.data(forKey: Key.lastAnswer),
           let saved = try? JSONDecoder().decode(UpdateCheckRecord.self, from: data),
           saved.ok, saved.current == appVersion {
            lastAnswer = saved
        }
    }

    /// The release the main window's banner names, or nil: the last
    /// successful check found a newer one, and the daily check is on.
    var banner: UpdateCheckRecord? {
        guard dailyCheck, let lastAnswer, lastAnswer.updateAvailable, lastAnswer.latest != nil
        else { return nil }
        return lastAnswer
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
    /// `pollInterval`, and it runs the CLI only when `isDue`.
    func checkIfDue() async {
        guard Self.isDue(dailyCheck: dailyCheck, lastChecked: lastChecked, now: now()) else { return }
        await check(answering: false)
    }

    /// Help › Check for Updates…: now, whatever the switch says, and always
    /// with an answer.
    func checkNow() async {
        await check(answering: true)
    }

    /// The release page, in the browser.
    func openReleasePage(_ record: UpdateCheckRecord) {
        guard let text = record.url, let url = URL(string: text), url.scheme == "https" else { return }
        open(url)
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

        guard let record, record.ok, record.latest != nil else {
            // Quiet unless someone asked: the banner keeps what it had.
            if says { answer = .failed(record?.message ?? failure ?? Answer.noAnswer) }
            return
        }
        defaults.set(now(), forKey: Key.lastChecked)
        if let data = try? JSONEncoder().encode(record) {
            defaults.set(data, forKey: Key.lastAnswer)
        }
        lastAnswer = record
        if says { answer = record.updateAvailable ? .available(record) : .upToDate }
    }
}
