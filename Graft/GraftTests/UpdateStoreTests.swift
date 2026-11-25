import XCTest
@testable import Graft

/// Whether a newer Graft is out: the daily rule, the Settings switch, the
/// banner, and what Help › Check for Updates… says.
///
/// The CLI is a script (`ScriptCLI`) that logs its argv and prints a canned
/// `update-check`, the preferences a throwaway suite, the clock a value the
/// test moves, and the browser a list of the URLs it was asked to open. The
/// version comparison itself is the CLI's (tests/test_check_update.py).
@MainActor
final class UpdateStoreTests: XCTestCase {
    private final class Clock {
        var now = Date(timeIntervalSinceReferenceDate: 800_000_000)
    }

    private let clock = Clock()
    private var opened: [URL] = []
    private static let page = "https://github.com/GreenglassT/Graft/releases/tag/v2.0.1"

    private func throwawayDefaults() throws -> UserDefaults {
        let name = "UpdateStoreTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: name))
        addTeardownBlock { defaults.removePersistentDomain(forName: name) }
        return defaults
    }

    /// One `update-check` line, through `JSONSerialization`.
    private func line(
        ok: Bool = true, current: String = "2.0", latest: String? = "2.0.1",
        available: Bool = true, url: String? = page, message: String? = nil
    ) -> String {
        let payload: [String: Any] = [
            "type": "update-check", "ok": ok, "current": current,
            "latest": (ok ? latest : nil) as Any? ?? NSNull(),
            "url": (ok ? url : nil) as Any? ?? NSNull(),
            "update_available": ok && available,
            "reason": ok ? NSNull() as Any : "offline" as Any,
            "status": NSNull(),
            "message": message as Any? ?? NSNull(),
        ]
        let data = try! JSONSerialization.data(withJSONObject: payload)
        return String(data: data, encoding: .utf8)!
    }

    /// A stand-in graft that prints `output` and exits `status`, after
    /// `delay` seconds.
    private func fakeCLI(_ output: String, status: Int = 0, delay: Double = 0) throws -> ScriptCLI {
        try ScriptCLI("""
        sleep \(delay)
        cat <<'EOF'
        \(output)
        EOF
        exit \(status)
        """, test: self)
    }

    private func store(
        _ cli: ScriptCLI, defaults: UserDefaults, appVersion: String = "2.0"
    ) -> UpdateStore {
        UpdateStore(
            runner: cli.runner, defaults: defaults, now: { [clock] in clock.now },
            appVersion: appVersion, open: { [weak self] in self?.opened.append($0) },
            updateLog: updateLog
        )
    }

    /// Where the store looks for the helper's word on an install: a file in
    /// a temp folder of this test's, never ~/Library/Logs/Graft. Nothing is
    /// there until a test writes it (`writeLog`).
    private lazy var updateLog: URL = {
        let dir = FileManager.default.temporaryDirectory.appending(path: "update_log_\(UUID().uuidString)")
        try! FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        return dir.appending(path: "update.log")
    }()

    /// `text` as the helper's `say` writes it (`app_update.HELPER`): the
    /// time to the second in `zone`, with its offset from UTC ("+1400"),
    /// then the text. The helper's zone is the CLI's, which can differ from
    /// the app's. `offset: false` is how 2.0.2's helper wrote it: no
    /// offset, in this Mac's zone.
    private func logged(_ text: String, at date: Date, in zone: TimeZone = .current, offset: Bool = true) -> String {
        let stamp = DateFormatter()
        stamp.locale = Locale(identifier: "en_US_POSIX")
        stamp.timeZone = offset ? zone : .current
        stamp.dateFormat = offset ? "yyyy-MM-dd HH:mm:ss Z" : "yyyy-MM-dd HH:mm:ss"
        return "\(stamp.string(from: date)) \(text)"
    }

    private func writeLog(_ lines: [String]) throws {
        try (lines.joined(separator: "\n") + "\n").write(to: updateLog, atomically: true, encoding: .utf8)
    }

    private static let offline = "Couldn't reach GitHub to check for a newer Graft. Check this "
        + "Mac's internet connection, then try again."

    // MARK: The daily rule

    func test_the_daily_rule() {
        let now = Date(timeIntervalSinceReferenceDate: 800_000_000)
        let hour: TimeInterval = 60 * 60
        func due(_ on: Bool, _ last: Date?) -> Bool {
            UpdateStore.isDue(dailyCheck: on, lastChecked: last, now: now)
        }
        XCTAssertTrue(due(true, nil), "never checked")
        XCTAssertFalse(due(true, now.addingTimeInterval(-3 * hour)))
        XCTAssertFalse(due(true, now.addingTimeInterval(-24 * hour)), "a day is not more than a day")
        XCTAssertTrue(due(true, now.addingTimeInterval(-24 * hour - 1)))
        XCTAssertTrue(due(true, now.addingTimeInterval(2 * hour)), "a last check in the future")
        XCTAssertFalse(due(false, nil), "the switch is off")
        XCTAssertFalse(due(false, now.addingTimeInterval(-30 * 24 * hour)))
    }

    func test_a_due_check_runs_the_cli_with_this_copys_version_and_raises_the_banner() async throws {
        let cli = try fakeCLI(line())
        let sut = store(cli, defaults: try throwawayDefaults())

        await sut.checkIfDue()

        XCTAssertEqual(cli.argv(), ["--json --check-update --current-version 2.0"])
        XCTAssertEqual(sut.banner?.latest, "2.0.1")
        XCTAssertEqual(sut.lastChecked, clock.now)
        XCTAssertNil(sut.answer, "nobody asked: the daily check puts up no alert")
    }

    func test_within_a_day_of_a_success_nothing_runs_and_after_one_it_does() async throws {
        let cli = try fakeCLI(line(available: false, url: nil))
        let sut = store(cli, defaults: try throwawayDefaults())

        await sut.checkIfDue()
        clock.now += 23 * 60 * 60
        await sut.checkIfDue()
        XCTAssertEqual(cli.argv().count, 1)

        clock.now += 60 * 60 + 1
        await sut.checkIfDue()
        XCTAssertEqual(cli.argv().count, 2)
    }

    func test_a_failed_daily_check_is_quiet_and_is_tried_again() async throws {
        let cli = try fakeCLI(line(ok: false, message: Self.offline), status: 1)
        let sut = store(cli, defaults: try throwawayDefaults())

        await sut.checkIfDue()

        XCTAssertNil(sut.answer)
        XCTAssertNil(sut.banner)
        XCTAssertNil(sut.lastChecked, "only a success starts the day")
        await sut.checkIfDue()
        XCTAssertEqual(cli.argv().count, 2)
    }

    func test_a_failed_check_keeps_what_the_last_good_one_found() async throws {
        let defaults = try throwawayDefaults()
        await store(try fakeCLI(line()), defaults: defaults).checkIfDue()
        clock.now += 25 * 60 * 60

        let failing = try fakeCLI(line(ok: false, message: Self.offline), status: 1)
        let sut = store(failing, defaults: defaults)
        await sut.checkIfDue()

        XCTAssertEqual(failing.argv().count, 1)
        XCTAssertEqual(sut.banner?.latest, "2.0.1")
    }

    // MARK: The switch

    func test_the_switch_is_on_by_default_and_remembered() throws {
        let defaults = try throwawayDefaults()
        let cli = try fakeCLI(line())
        XCTAssertTrue(store(cli, defaults: defaults).dailyCheck)

        store(cli, defaults: defaults).dailyCheck = false

        XCTAssertEqual(defaults.object(forKey: UpdateStore.Key.dailyCheck) as? Bool, false)
        XCTAssertFalse(store(cli, defaults: defaults).dailyCheck)
    }

    func test_with_the_switch_off_nothing_runs_and_no_banner_shows() async throws {
        let cli = try fakeCLI(line())
        let sut = store(cli, defaults: try throwawayDefaults())
        await sut.checkIfDue()
        XCTAssertNotNil(sut.banner)

        sut.dailyCheck = false
        clock.now += 3 * 24 * 60 * 60
        await sut.checkIfDue()

        XCTAssertNil(sut.banner)
        XCTAssertEqual(cli.argv().count, 1)
    }

    // MARK: The banner

    func test_the_banner_comes_back_after_a_relaunch_but_not_after_an_update() async throws {
        let defaults = try throwawayDefaults()
        await store(try fakeCLI(line()), defaults: defaults).checkIfDue()

        let relaunched = try fakeCLI(line())
        let again = store(relaunched, defaults: defaults)
        await again.checkIfDue()
        XCTAssertEqual(again.banner?.latest, "2.0.1")
        XCTAssertEqual(relaunched.argv(), [], "the same day: nothing asked")

        let updated = store(try fakeCLI(line()), defaults: defaults, appVersion: "2.0.1")
        XCTAssertNil(updated.banner, "the answer was about 2.0")
    }

    func test_no_banner_when_this_is_the_latest() async throws {
        let sut = store(try fakeCLI(line(latest: "2.0", available: false)), defaults: try throwawayDefaults())
        await sut.checkIfDue()
        XCTAssertNil(sut.banner)
        XCTAssertNotNil(sut.lastChecked)
    }

    func test_the_banner_says_which_version_and_opens_its_page() async throws {
        let sut = store(try fakeCLI(line()), defaults: try throwawayDefaults())
        await sut.checkIfDue()
        let release = try XCTUnwrap(sut.banner)

        XCTAssertEqual(UpdateNotice.text(version: try XCTUnwrap(release.latest)), "Graft 2.0.1 is available")
        sut.openReleasePage(release)
        XCTAssertEqual(opened, [URL(string: Self.page)!])
    }

    /// The page is built from the version, so whatever `url` the CLI sends
    /// is never what opens.
    func test_the_page_opened_is_built_from_the_version_not_the_clis_url() async throws {
        for url in ["file:///Applications/Example.app", "https://example.invalid/phish",
                    "https://github.com/GreenglassT/Graft/releases/../../../example/phish"] {
            opened = []
            let sut = store(try fakeCLI(line(url: url)), defaults: try throwawayDefaults())
            await sut.checkIfDue()
            sut.openReleasePage(try XCTUnwrap(sut.banner))
            sut.openNoticePage()
            XCTAssertEqual(opened, [URL(string: Self.page)!, URL(string: Self.page)!], url)
        }
    }

    /// Any process running as this user can `defaults write` the saved
    /// answer. One naming a foreign page still shows its version, and the
    /// banner opens Graft's page for that version, not the one it named.
    func test_a_forged_saved_answer_with_a_foreign_url_opens_only_graft_s_page() async throws {
        let defaults = try throwawayDefaults()
        let forged = UpdateCheckRecord(
            ok: true, current: "2.0", latest: "2.0.1", url: "https://example.invalid/phish",
            updateAvailable: true, reason: nil, status: nil, message: nil
        )
        defaults.set(try JSONEncoder().encode(forged), forKey: UpdateStore.Key.lastAnswer)
        defaults.set(clock.now, forKey: UpdateStore.Key.lastChecked)
        let cli = try fakeCLI(line())

        let sut = store(cli, defaults: defaults)
        await sut.checkIfDue()

        XCTAssertEqual(cli.argv(), [], "the same day: nothing asked")
        let release = try XCTUnwrap(sut.banner)
        sut.openReleasePage(release)
        sut.openNoticePage()
        XCTAssertEqual(opened, [URL(string: Self.page)!, URL(string: Self.page)!])
    }

    /// A saved answer whose version isn't digits and dots is dropped at
    /// launch, from the banner and from `defaults`; so is a saved install
    /// attempt's.
    func test_a_saved_answer_or_attempt_with_a_bad_version_is_dropped() async throws {
        let bad = [
            "2.0.1/../../../example/phish", "2.0.1?x=1", "2.0.1#x", "2.0.1@example.invalid", "v2.0.1",
            "2.0.1 ", "", ".1", "1.", "1..2", "2.0.1-beta", "\u{0662}.0", "\u{FF12}.0",
            String(repeating: "1", count: 33),
        ]
        for latest in bad {
            let defaults = try throwawayDefaults()
            let forged = UpdateCheckRecord(
                ok: true, current: "2.0", latest: latest, url: Self.page,
                updateAvailable: true, reason: nil, status: nil, message: nil
            )
            defaults.set(try JSONEncoder().encode(forged), forKey: UpdateStore.Key.lastAnswer)
            defaults.set(try JSONEncoder().encode(UpdateStore.Attempt(version: latest, from: "2.0")),
                         forKey: UpdateStore.Key.attempt)

            let sut = updatingStore(try UpdateCLI(test: self), defaults: defaults)

            XCTAssertNil(sut.banner, latest)
            XCTAssertNil(sut.availableVersion, latest)
            XCTAssertNil(sut.notice, latest)
            XCTAssertEqual(sut.phase, .idle, latest)
            XCTAssertNil(defaults.data(forKey: UpdateStore.Key.lastAnswer), latest)
            XCTAssertNil(defaults.data(forKey: UpdateStore.Key.attempt), latest)
            XCTAssertNil(defaults.string(forKey: UpdateStore.Key.failedVersion), latest)
        }
        // Not dropped: a plain answer about another version of Graft.
        let defaults = try throwawayDefaults()
        let other = UpdateCheckRecord(ok: true, current: "1.9", latest: "2.0", url: nil,
                                      updateAvailable: true, reason: nil, status: nil, message: nil)
        defaults.set(try JSONEncoder().encode(other), forKey: UpdateStore.Key.lastAnswer)
        XCTAssertNil(store(try fakeCLI(line()), defaults: defaults).banner)
        XCTAssertNotNil(defaults.data(forKey: UpdateStore.Key.lastAnswer))
        // Nor junk that isn't an answer at all: it goes.
        defaults.set(Data("not json".utf8), forKey: UpdateStore.Key.lastAnswer)
        XCTAssertNil(store(try fakeCLI(line()), defaults: defaults).banner)
        XCTAssertNil(defaults.data(forKey: UpdateStore.Key.lastAnswer))
    }

    /// The CLI's answer gets the same rule: one naming a version that isn't
    /// plain isn't saved or shown, and Check for Updates… says so.
    func test_a_check_that_names_a_bad_version_is_not_kept() async throws {
        let defaults = try throwawayDefaults()
        let sut = store(try fakeCLI(line(latest: "2.0.1/../../example")), defaults: defaults)

        await sut.checkNow()

        XCTAssertEqual(sut.answer, .failed(UpdateStore.unreadableVersion))
        XCTAssertNil(sut.banner)
        XCTAssertNil(sut.lastChecked, "not a successful check")
        XCTAssertNil(defaults.data(forKey: UpdateStore.Key.lastAnswer))
    }

    func test_a_plain_version_is_digits_and_dots() {
        for good in ["2", "2.0", "2.0.1", "10.20.30", "2.0.0.1", String(repeating: "1", count: 32)] {
            XCTAssertTrue(UpdateStore.isPlainVersion(good), good)
            XCTAssertEqual(UpdateStore.releasePage(version: good).absoluteString,
                           "https://github.com/GreenglassT/Graft/releases/tag/v\(good)")
        }
        for bad in ["", "v2", "2.", ".2", "2..0", "2.0/..", "2.0\n", "2,0", "-1", "+1", "1e3", "\u{0663}"] {
            XCTAssertFalse(UpdateStore.isPlainVersion(bad), bad)
            XCTAssertEqual(UpdateStore.releasePage(version: bad).absoluteString,
                           "https://github.com/GreenglassT/Graft/releases", "still Graft's: \(bad)")
        }
        XCTAssertEqual(UpdateStore.releasePage(version: nil).absoluteString,
                       "https://github.com/GreenglassT/Graft/releases")
    }

    // MARK: Check for Updates…

    func test_check_for_updates_runs_with_the_switch_off_and_says_up_to_date() async throws {
        let cli = try fakeCLI(line(latest: "2.0", available: false))
        let sut = store(cli, defaults: try throwawayDefaults())
        sut.dailyCheck = false

        await sut.checkNow()

        XCTAssertEqual(cli.argv(), ["--json --check-update --current-version 2.0"])
        XCTAssertEqual(sut.answer, .upToDate)
        XCTAssertEqual(sut.answerAlert, UpdateStore.CheckAlert(
            title: "You're up to date", message: "Graft 2.0 is the newest version.",
            action: nil, release: nil, cancel: "OK"
        ))
        sut.dismissAnswer()
        XCTAssertNil(sut.answer)
        XCTAssertNil(sut.answerAlert)
    }

    func test_check_for_updates_runs_even_the_same_day() async throws {
        let cli = try fakeCLI(line())
        let sut = store(cli, defaults: try throwawayDefaults())
        await sut.checkIfDue()
        await sut.checkNow()
        XCTAssertEqual(cli.argv().count, 2)
    }

    func test_check_for_updates_says_what_is_available() async throws {
        let sut = store(try fakeCLI(line()), defaults: try throwawayDefaults())

        await sut.checkNow()

        guard case let .available(release)? = sut.answer else {
            return XCTFail("not an offer: \(String(describing: sut.answer))")
        }
        let alert = try XCTUnwrap(sut.answerAlert)
        XCTAssertEqual(alert.title, "Graft 2.0.1 is available")
        XCTAssertTrue(alert.message.hasPrefix("You have Graft 2.0."), alert.message)
        XCTAssertEqual(alert.release, release)
        XCTAssertEqual(sut.banner, release)
        sut.openReleasePage(release)
        XCTAssertEqual(opened, [URL(string: Self.page)!])
    }

    func test_check_for_updates_says_why_it_failed() async throws {
        let sut = store(try fakeCLI(line(ok: false, message: Self.offline), status: 1), defaults: try throwawayDefaults())

        await sut.checkNow()

        XCTAssertEqual(sut.answer, .failed(Self.offline))
        XCTAssertEqual(sut.answerAlert, UpdateStore.CheckAlert(
            title: "Couldn't check for updates", message: Self.offline, action: nil, release: nil, cancel: "OK"
        ))
        XCTAssertNil(sut.lastChecked)
        XCTAssertNil(sut.banner)
    }

    func test_check_for_updates_when_the_cli_says_nothing() async throws {
        let sut = store(try fakeCLI(""), defaults: try throwawayDefaults())
        await sut.checkNow()
        XCTAssertEqual(sut.answer, .failed(UpdateStore.Answer.noAnswer))
    }

    func test_check_for_updates_when_the_cli_cannot_run() async throws {
        let sut = UpdateStore(
            runner: CLIRunner(executable: URL(fileURLWithPath: "/nonexistent/graft")),
            defaults: try throwawayDefaults(), now: { [clock] in clock.now },
            appVersion: "2.0", open: { _ in XCTFail("nothing to open") }, updateLog: updateLog
        )
        await sut.checkNow()
        guard case let .failed(why)? = sut.answer else {
            return XCTFail("no answer: \(String(describing: sut.answer))")
        }
        XCTAssertFalse(why.isEmpty)
    }

    /// Chosen while the daily check is running: no second run, and that
    /// check's result is the answer.
    func test_check_for_updates_during_a_daily_check_answers_with_its_result() async throws {
        let cli = try fakeCLI(line(), delay: 0.5)
        let sut = store(cli, defaults: try throwawayDefaults())

        let daily = Task { await sut.checkIfDue() }
        await eventually({ sut.isChecking }, "the daily check started")
        await sut.checkNow()
        XCTAssertNil(sut.answer, "answered before the check finished")
        await daily.value

        XCTAssertEqual(cli.argv().count, 1)
        guard case .available? = sut.answer else {
            return XCTFail("no answer: \(String(describing: sut.answer))")
        }
    }

    // MARK: Updating

    /// A stand-in graft for the self-update: each verb answers from files in
    /// `dir` the test writes, and every run is logged (`ScriptCLI`). The
    /// check runs `check.sh` if there is one, then prints `check<n>` for its
    /// n-th run, or `check`; a download runs `download<n>.sh`, or
    /// `download.sh`; an install runs `install.sh` if there is one, then
    /// prints `install` and exits with `install-status`.
    private final class UpdateCLI {
        let dir: URL
        let script: ScriptCLI
        var runner: CLIRunner { script.runner }

        init(test: XCTestCase) throws {
            dir = FileManager.default.temporaryDirectory.appending(path: "update_cli_\(UUID().uuidString)")
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            let dir = dir
            test.addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
            script = try ScriptCLI("""
            D='\(dir.path)'
            count() { n=$(cat "$D/$1.count" 2>/dev/null || echo 0); n=$((n + 1)); echo $n > "$D/$1.count"; echo $n; }
            case "$*" in
            *--check-update*)
              if [ -e "$D/check.sh" ]; then . "$D/check.sh"; fi
              n=$(count check); if [ -e "$D/check$n" ]; then cat "$D/check$n"; else cat "$D/check"; fi ;;
            *--download-update*)
              n=$(count download); if [ -e "$D/download$n.sh" ]; then . "$D/download$n.sh"; else . "$D/download.sh"; fi ;;
            *--install-update*)
              if [ -e "$D/install.sh" ]; then . "$D/install.sh"; fi
              cat "$D/install"; exit "$(cat "$D/install-status" 2>/dev/null || echo 0)" ;;
            esac
            """, test: test)
        }

        func write(_ name: String, _ text: String) throws {
            try text.write(to: dir.appending(path: name), atomically: true, encoding: .utf8)
        }

        func argv() -> [String] { script.argv() }
        func runs(of verb: String) -> [String] { argv().filter { $0.contains(verb) } }

        /// The test's go-ahead for a download that waits (`wait(for:)`).
        func go(_ name: String) throws { try write(name, "") }
        func wait(for name: String) -> String {
            "n=0; while [ ! -e \"$D/\(name)\" ] && [ $n -lt 600 ]; do /bin/sleep 0.05; n=$((n+1)); done\n"
        }
    }

    private static func json(_ payload: [String: Any?]) -> String {
        let object = payload.mapValues { $0 ?? NSNull() }
        let data = try! JSONSerialization.data(withJSONObject: object)
        return String(data: data, encoding: .utf8)!
    }

    private static func progress(received: Int?, total: Int?, check: String? = nil, version: String = "2.0.1") -> String {
        json(["type": "update-progress", "step": check == nil ? "download" : "verify", "version": version,
              "received": received, "total": total, "check": check])
    }

    private static func downloaded(_ version: String = "2.0.1", path: String = "/cache/a/Graft.app") -> String {
        json(["type": "update-download-end", "ok": true, "current": "2.0", "version": version,
              "app_path": path, "url": "https://github.com/GreenglassT/Graft/releases/tag/v\(version)",
              "reason": nil, "message": nil])
    }

    private static func notDownloaded(
        _ reason: String, _ message: String, version: String = "2.0.1",
        url: String? = nil
    ) -> String {
        json(["type": "update-download-end", "ok": false, "current": "2.0", "version": nil, "app_path": nil,
              "url": url ?? "https://github.com/GreenglassT/Graft/releases/tag/v\(version)",
              "reason": reason, "message": message])
    }

    private static func installed(ok: Bool = true, message: String? = nil, reason: String = "not-writable") -> String {
        json(["type": "update-install", "ok": ok, "version": ok ? "2.0.1" : nil,
              "log": ok ? "/logs/update.log" : nil, "relaunch": false, "url": nil,
              "reason": ok ? nil : reason, "message": message])
    }

    /// `download.sh`'s text: print `lines` and exit `status`.
    private static func prints(_ lines: String..., status: Int = 0) -> String {
        "cat <<'EOF'\n\(lines.joined(separator: "\n"))\nEOF\nexit \(status)\n"
    }

    private let control = TerminationHoldTests.FakeControl()
    private lazy var hold = TerminationHold(control: control)
    private var terminated = 0
    private static let appPath = "/private/var/empty/Example/Graft.app"

    private func updatingStore(
        _ cli: UpdateCLI, defaults: UserDefaults, appVersion: String = "2.0"
    ) -> UpdateStore {
        UpdateStore(
            runner: cli.runner, defaults: defaults, now: { [clock] in clock.now },
            appVersion: appVersion, open: { [weak self] in self?.opened.append($0) },
            updateLog: updateLog,
            host: SelfUpdateHost(
                appPath: Self.appPath, pid: 4242, hold: hold,
                terminate: { [weak self] in self?.terminated += 1 }
            )
        )
    }

    private static let downloadArgs = "--json --download-update --current-version 2.0 --app-path \(appPath)"

    /// An update ready to install, downloaded on its own after a check.
    private func readyStore(_ cli: UpdateCLI, defaults: UserDefaults) async throws -> UpdateStore {
        try cli.write("check", line())
        try cli.write("download.sh", Self.prints(Self.downloaded()))
        let sut = updatingStore(cli, defaults: defaults)
        await sut.checkIfDue()
        await eventually({ sut.phase == .ready(version: "2.0.1") }, "the download is ready")
        return sut
    }

    func test_without_a_host_nothing_is_downloaded_and_the_banner_links_the_page() async throws {
        let cli = try UpdateCLI(test: self)
        try cli.write("check", line())
        let sut = UpdateStore(
            runner: cli.runner, defaults: try throwawayDefaults(), now: { [clock] in clock.now },
            appVersion: "2.0", open: { _ in }, updateLog: updateLog
        )
        await sut.checkIfDue()
        sut.update()
        XCTAssertFalse(sut.canUpdate)
        XCTAssertEqual(cli.runs(of: "--download-update"), [])
        XCTAssertEqual(sut.notice, UpdateStore.Notice(version: "2.0.1", state: .available(canUpdate: false)))
    }

    func test_a_newer_release_is_downloaded_on_its_own_then_ready() async throws {
        let cli = try UpdateCLI(test: self)
        let defaults = try throwawayDefaults()
        XCTAssertTrue(updatingStore(cli, defaults: defaults).autoInstall, "on by default")

        let sut = try await readyStore(cli, defaults: defaults)

        XCTAssertEqual(cli.argv(), ["--json --check-update --current-version 2.0", Self.downloadArgs])
        XCTAssertEqual(sut.notice, UpdateStore.Notice(version: "2.0.1", state: .ready(installsOnQuit: true)))
        XCTAssertEqual(UpdateNotice.text(try XCTUnwrap(sut.notice)),
                       "Graft 2.0.1 is ready. It installs when you quit Graft.")
        XCTAssertEqual(terminated, 0, "nobody asked to restart")
    }

    func test_the_banner_follows_the_download_and_the_checks() async throws {
        let cli = try UpdateCLI(test: self)
        try cli.write("check", line())
        try cli.write("download.sh", Self.prints(Self.progress(received: 0, total: 200),
                                                 Self.progress(received: 100, total: 200))
            .replacingOccurrences(of: "exit 0\n", with: "")
            + cli.wait(for: "go1")
            + Self.prints(Self.progress(received: nil, total: nil, check: "signature")).replacingOccurrences(of: "exit 0\n", with: "")
            + cli.wait(for: "go2")
            + Self.prints(Self.downloaded()))
        let sut = updatingStore(cli, defaults: try throwawayDefaults())
        await sut.checkIfDue()

        await eventually({ sut.phase == .downloading(version: "2.0.1", fraction: 0.5) }, "half way")
        XCTAssertEqual(UpdateNotice.text(try XCTUnwrap(sut.notice)), "Downloading Graft 2.0.1… 50%")
        try cli.go("go1")
        await eventually({ sut.phase == .verifying(version: "2.0.1") }, "checking")
        XCTAssertEqual(UpdateNotice.text(try XCTUnwrap(sut.notice)), "Checking Graft 2.0.1…")
        try cli.go("go2")
        await eventually({ sut.phase == .ready(version: "2.0.1") }, "ready")
    }

    func test_a_download_that_fails_its_checks_falls_back_to_the_release_page() async throws {
        let cli = try UpdateCLI(test: self)
        let defaults = try throwawayDefaults()
        try cli.write("check", line())
        let why = "The downloaded app isn't signed by Graft's developer, so Graft deleted it."
        try cli.write("download.sh", Self.prints(Self.notDownloaded("requirement", why), status: 1))
        let sut = updatingStore(cli, defaults: defaults)

        await sut.checkIfDue()
        await eventually({ sut.phase == .failed(version: "2.0.1", message: why, retryable: false) }, "failed")

        XCTAssertEqual(sut.notice?.state, .failed(why, retryable: false))
        XCTAssertEqual(UpdateNotice.text(try XCTUnwrap(sut.notice)), "Graft couldn't update to 2.0.1")
        sut.openNoticePage()
        XCTAssertEqual(opened, [URL(string: "https://github.com/GreenglassT/Graft/releases/tag/v2.0.1")!])
        XCTAssertFalse(sut.handOver { _ in XCTFail("nothing to install") }, "nothing is ready")

        // Not again on its own: not this launch, nor the next.
        clock.now += 25 * 60 * 60
        await sut.checkIfDue()
        XCTAssertEqual(cli.runs(of: "--download-update").count, 1)
        let relaunched = updatingStore(cli, defaults: defaults)
        await relaunched.checkIfDue()
        XCTAssertEqual(cli.runs(of: "--download-update").count, 1)
        // Update still tries it.
        relaunched.update()
        await eventually({ cli.runs(of: "--download-update").count == 2 }, "Update tries again")
    }

    /// The failed download's `url` is never what opens either.
    func test_a_failed_download_links_graft_s_page_for_its_version_whatever_url_it_sent() async throws {
        let cli = try UpdateCLI(test: self)
        try cli.write("check", line())
        try cli.write("download.sh", Self.prints(
            Self.notDownloaded("signature", "No.", url: "https://example.invalid/phish"), status: 1
        ))
        let sut = updatingStore(cli, defaults: try throwawayDefaults())

        await sut.checkIfDue()
        await eventually({ sut.phase == .failed(version: "2.0.1", message: "No.", retryable: false) }, "failed")

        sut.openNoticePage()
        XCTAssertEqual(opened, [URL(string: Self.page)!])
    }

    func test_an_offline_download_is_tried_again_next_launch_not_this_one() async throws {
        let cli = try UpdateCLI(test: self)
        let defaults = try throwawayDefaults()
        try cli.write("check", line())
        try cli.write("download.sh", Self.prints(Self.notDownloaded("offline", "Couldn't download the new Graft."), status: 1))
        let sut = updatingStore(cli, defaults: defaults)
        await sut.checkIfDue()
        await eventually({ if case .failed = sut.phase { return true }; return false }, "failed")
        await sut.checkIfDue()
        XCTAssertEqual(cli.runs(of: "--download-update").count, 1)

        let relaunched = updatingStore(cli, defaults: defaults)
        await relaunched.checkIfDue()
        await eventually({ cli.runs(of: "--download-update").count == 2 }, "the next launch tries again")
    }

    func test_with_the_second_switch_off_update_downloads_then_restarts() async throws {
        let cli = try UpdateCLI(test: self)
        let defaults = try throwawayDefaults()
        try cli.write("check", line())
        try cli.write("download.sh", Self.prints(Self.downloaded()))
        let sut = updatingStore(cli, defaults: defaults)
        sut.autoInstall = false
        XCTAssertEqual(defaults.object(forKey: UpdateStore.Key.autoInstall) as? Bool, false)

        await sut.checkIfDue()
        XCTAssertEqual(cli.runs(of: "--download-update"), [], "not on its own")
        XCTAssertEqual(sut.notice, UpdateStore.Notice(version: "2.0.1", state: .available(canUpdate: true)))

        sut.update()
        await eventually({ self.terminated == 1 }, "restarts once it's ready")
        XCTAssertEqual(sut.phase, .ready(version: "2.0.1"))
        XCTAssertEqual(cli.runs(of: "--download-update"), [Self.downloadArgs])
    }

    func test_update_while_work_runs_waits_for_restart_to_update() async throws {
        let cli = try UpdateCLI(test: self)
        try cli.write("check", line())
        try cli.write("download.sh", Self.prints(Self.downloaded()))
        let sut = updatingStore(cli, defaults: try throwawayDefaults())
        sut.autoInstall = false
        await sut.checkIfDue()
        let ticket = hold.begin(TerminationHold.building, stop: nil)
        defer { hold.end(ticket) }

        await sut.startDownload(userAsked: true)?.value

        XCTAssertEqual(sut.phase, .ready(version: "2.0.1"))
        XCTAssertEqual(terminated, 0, "not while a build runs")
        XCTAssertEqual(sut.notice?.state, .ready(installsOnQuit: true), "asked for: a quit installs it")
        sut.restartToUpdate()
        XCTAssertEqual(terminated, 1, "the quit asks about the build, as every quit does")
    }

    func test_turning_the_second_switch_on_starts_the_download() async throws {
        let cli = try UpdateCLI(test: self)
        try cli.write("check", line())
        try cli.write("download.sh", Self.prints(Self.downloaded()))
        let sut = updatingStore(cli, defaults: try throwawayDefaults())
        sut.autoInstall = false
        await sut.checkIfDue()
        XCTAssertEqual(cli.runs(of: "--download-update"), [])
        sut.autoInstall = true
        await eventually({ sut.phase == .ready(version: "2.0.1") }, "downloaded")
    }

    // MARK: Installing on the way out

    func test_a_quit_hands_the_ready_update_to_the_helper() async throws {
        let cli = try UpdateCLI(test: self)
        let defaults = try throwawayDefaults()
        let sut = try await readyStore(cli, defaults: defaults)
        try cli.write("install", Self.installed())
        var replies: [Bool] = []

        XCTAssertTrue(sut.handOver { replies.append($0) })

        XCTAssertTrue(sut.installPending)
        XCTAssertEqual(sut.phase, .installing(version: "2.0.1"))
        XCTAssertEqual(replies, [], "not from inside the call")
        await eventually({ replies == [true] }, "the quit goes on once the helper is started")
        XCTAssertEqual(cli.runs(of: "--install-update"), [
            "--json --install-update /cache/a/Graft.app --app-path \(Self.appPath) --wait-pid 4242",
        ])
        let attempt = try JSONDecoder().decode(
            UpdateStore.Attempt.self, from: try XCTUnwrap(defaults.data(forKey: UpdateStore.Key.attempt))
        )
        XCTAssertEqual(attempt, UpdateStore.Attempt(version: "2.0.1", from: "2.0", at: clock.now))
    }

    func test_restart_to_update_quits_and_the_helper_reopens_graft() async throws {
        let cli = try UpdateCLI(test: self)
        let sut = try await readyStore(cli, defaults: try throwawayDefaults())
        try cli.write("install", Self.installed())

        sut.restartToUpdate()
        XCTAssertEqual(terminated, 1)
        var replies: [Bool] = []
        XCTAssertTrue(sut.handOver { replies.append($0) })
        await eventually({ replies == [true] })
        XCTAssertEqual(cli.runs(of: "--install-update").first?.hasSuffix("--wait-pid 4242 --relaunch"), true)
    }

    func test_keep_working_takes_back_the_restart() async throws {
        let cli = try UpdateCLI(test: self)
        let sut = try await readyStore(cli, defaults: try throwawayDefaults())
        try cli.write("install", Self.installed())
        sut.restartToUpdate()
        sut.quitCanceled()

        var replies: [Bool] = []
        XCTAssertTrue(sut.handOver { replies.append($0) }, "the switch still installs on quit")
        await eventually({ replies == [true] })
        XCTAssertFalse(cli.runs(of: "--install-update").first?.contains("--relaunch") ?? true,
                       "a later quit doesn't reopen")
    }

    func test_nothing_is_installed_while_work_runs() async throws {
        let cli = try UpdateCLI(test: self)
        let sut = try await readyStore(cli, defaults: try throwawayDefaults())
        let ticket = hold.begin(TerminationHold.reinstalling, stop: nil)
        defer { hold.end(ticket) }

        XCTAssertFalse(sut.handOver { _ in XCTFail("nothing handed over") })
        XCTAssertFalse(sut.installPending)
        XCTAssertEqual(cli.runs(of: "--install-update"), [])
    }

    func test_with_the_switch_off_a_quit_installs_only_what_was_asked_for() async throws {
        let cli = try UpdateCLI(test: self)
        let sut = try await readyStore(cli, defaults: try throwawayDefaults())
        sut.autoInstall = false
        XCTAssertEqual(sut.notice?.state, .ready(installsOnQuit: false))
        XCTAssertFalse(sut.handOver { _ in XCTFail("nothing handed over") })
        XCTAssertEqual(cli.runs(of: "--install-update"), [])
    }

    func test_while_an_install_is_pending_no_download_starts() async throws {
        let cli = try UpdateCLI(test: self)
        let sut = try await readyStore(cli, defaults: try throwawayDefaults())
        try cli.write("install", Self.installed())
        try cli.write("check2", line(latest: "2.0.2"))
        XCTAssertTrue(sut.handOver { _ in })
        clock.now += 25 * 60 * 60
        await sut.checkNow()

        XCTAssertNil(sut.startDownload(userAsked: true))
        XCTAssertEqual(cli.runs(of: "--download-update").count, 1, "the helper's download is left alone")
        XCTAssertFalse(sut.handOver { _ in XCTFail("one handover") }, "and a second quit hands over nothing")
    }

    func test_a_helper_that_cannot_start_lets_a_quit_quit_and_says_why() async throws {
        let cli = try UpdateCLI(test: self)
        let defaults = try throwawayDefaults()
        let sut = try await readyStore(cli, defaults: defaults)
        let why = "Graft can't update itself in /Example: this account can't change that folder."
        try cli.write("install", Self.installed(ok: false, message: why))
        try cli.write("install-status", "1")

        var replies: [Bool] = []
        XCTAssertTrue(sut.handOver { replies.append($0) })
        await eventually({ replies == [true] }, "a quit still quits")

        XCTAssertFalse(sut.installPending)
        XCTAssertEqual(sut.phase, .failed(version: "2.0.1", message: why, retryable: false))
        XCTAssertNil(defaults.data(forKey: UpdateStore.Key.attempt))
        XCTAssertEqual(defaults.string(forKey: UpdateStore.Key.failedVersion), "2.0.1")
    }

    func test_restart_to_update_that_cannot_start_the_helper_stays_open() async throws {
        let cli = try UpdateCLI(test: self)
        let sut = try await readyStore(cli, defaults: try throwawayDefaults())
        try cli.write("install", Self.installed(ok: false, message: "No."))
        try cli.write("install-status", "1")
        sut.restartToUpdate()

        var replies: [Bool] = []
        XCTAssertTrue(sut.handOver { replies.append($0) })
        await eventually({ replies == [false] }, "the restart is canceled, to say why")
        XCTAssertEqual(sut.notice?.state, .failed("No.", retryable: false))
    }

    func test_an_install_that_never_answers_does_not_keep_graft_from_quitting() async throws {
        let cli = try UpdateCLI(test: self)
        let defaults = try throwawayDefaults()
        let sut = try await readyStore(cli, defaults: defaults)
        try cli.write("install", Self.installed(ok: false, message: "Too late to matter."))
        try cli.write("install-status", "1")
        // The stand-in install waits for a go-ahead that comes after the
        // timeout.
        try cli.write("install.sh", cli.wait(for: "go"))
        sut.handoverTimeout = .milliseconds(300)

        var replies: [Bool] = []
        let started = Date()
        XCTAssertTrue(sut.handOver { replies.append($0) })
        await eventually({ replies == [true] }, "quit after the timeout")
        XCTAssertLessThan(Date().timeIntervalSince(started), 3)
        XCTAssertNotNil(defaults.data(forKey: UpdateStore.Key.attempt), "the next launch still says")

        // Its late answer changes nothing: the quit already went ahead.
        try cli.go("go")
        await eventually({ cli.runs(of: "--install-update").count == 1 })
        try await Task.sleep(for: .milliseconds(400))
        XCTAssertEqual(replies, [true])
        XCTAssertEqual(sut.phase, .installing(version: "2.0.1"))
    }

    func test_a_launch_still_on_the_old_version_says_the_install_did_not_take() throws {
        let defaults = try throwawayDefaults()
        let cli = try UpdateCLI(test: self)
        defaults.set(try JSONEncoder().encode(UpdateStore.Attempt(version: "2.0.1", from: "2.0")),
                     forKey: UpdateStore.Key.attempt)

        let sut = updatingStore(cli, defaults: defaults)

        XCTAssertEqual(sut.phase, .failed(version: "2.0.1", message: UpdateStore.installDidNotTake, retryable: false))
        XCTAssertNil(defaults.data(forKey: UpdateStore.Key.attempt), "said once")
        XCTAssertEqual(defaults.string(forKey: UpdateStore.Key.failedVersion), "2.0.1")
    }

    func test_a_launch_of_the_new_version_forgets_the_attempt() throws {
        let defaults = try throwawayDefaults()
        let cli = try UpdateCLI(test: self)
        defaults.set(try JSONEncoder().encode(UpdateStore.Attempt(version: "2.0.1", from: "2.0")),
                     forKey: UpdateStore.Key.attempt)

        let sut = updatingStore(cli, defaults: defaults, appVersion: "2.0.1")

        XCTAssertEqual(sut.phase, .idle)
        XCTAssertNil(defaults.data(forKey: UpdateStore.Key.attempt))
        XCTAssertNil(defaults.string(forKey: UpdateStore.Key.failedVersion))
    }

    // MARK: The helper's log

    /// The helper's line for an install that worked, as `app_update.HELPER`
    /// writes it after its time (tests/test_app_update_helper.py pins it).
    private static func installedLine(_ version: String = "2.0.1") -> String {
        "installed Graft \(version) at \(appPath); the old one is in the Trash at /private/var/empty/Example/Trash/Graft.app"
    }

    private func attempt(at: Date?, in defaults: UserDefaults) throws {
        defaults.set(try JSONEncoder().encode(UpdateStore.Attempt(version: "2.0.1", from: "2.0", at: at)),
                     forKey: UpdateStore.Key.attempt)
    }

    /// The install worked and the old version was put back by hand since
    /// (a downgrade): the helper's log says it installed 2.0.1 after the
    /// attempt was written down. Not a failure, and 2.0.1 isn't marked as
    /// one, so it still downloads on its own.
    func test_a_launch_on_the_old_version_after_a_logged_install_is_not_a_failure() async throws {
        let defaults = try throwawayDefaults()
        let cli = try UpdateCLI(test: self)
        try cli.write("check", line())
        try cli.write("download.sh", Self.prints(Self.downloaded()))
        let at = clock.now
        try attempt(at: at, in: defaults)
        try writeLog([
            logged("waiting for Graft (pid 4242) to quit, to install Graft 2.0.1 at \(Self.appPath)", at: at + 1),
            logged(Self.installedLine(), at: at + 2),
        ])

        let sut = updatingStore(cli, defaults: defaults)

        XCTAssertEqual(sut.phase, .idle)
        XCTAssertNil(sut.notice)
        XCTAssertNil(defaults.data(forKey: UpdateStore.Key.attempt), "read once")
        XCTAssertNil(defaults.string(forKey: UpdateStore.Key.failedVersion))
        await sut.checkIfDue()
        await eventually({ sut.phase == .ready(version: "2.0.1") }, "2.0.1 still downloads on its own")
    }

    /// The helper's times are whole seconds: an install in the same second
    /// as the attempt is logged with that second, which counts.
    func test_an_install_logged_in_the_attempt_s_own_second_counts() throws {
        let defaults = try throwawayDefaults()
        try attempt(at: clock.now + 0.7, in: defaults)
        try writeLog([logged(Self.installedLine(), at: clock.now)])

        let sut = updatingStore(try UpdateCLI(test: self), defaults: defaults)

        XCTAssertEqual(sut.phase, .idle)
    }

    /// A line whose time has an offset (the helper's since 2.1) is read in
    /// that offset, whatever this Mac's zone is: the helper's zone is the
    /// CLI's, which has any TZ the user's shell profile sets. Here zones a
    /// day apart, and one half an hour off the hour: an install two seconds
    /// after the attempt counts, one two seconds before doesn't, and one in
    /// the attempt's own second does.
    func test_an_install_logged_in_another_zone_is_read_by_its_offset() throws {
        let attempt = UpdateStore.Attempt(version: "2.0.1", from: "2.0", at: clock.now + 0.7)
        for name in ["Pacific/Kiritimati", "Etc/GMT+12", "Asia/Kolkata", "UTC"] {
            let zone = try XCTUnwrap(TimeZone(identifier: name))
            func counts(at date: Date) throws -> Bool {
                try writeLog([logged(Self.installedLine(), at: date, in: zone)])
                return UpdateStore.installLogged(attempt, in: updateLog)
            }
            XCTAssertTrue(try counts(at: clock.now + 2), name)
            XCTAssertTrue(try counts(at: clock.now), "the attempt's own second: \(name)")
            XCTAssertFalse(try counts(at: clock.now - 2), name)
        }
        XCTAssertTrue(logged("x", at: clock.now, in: TimeZone(identifier: "Pacific/Kiritimati")!).hasSuffix(" +1400 x"),
                      "the form tested is the helper's")
    }

    /// 2.0.2's helper wrote its times with no offset, in this Mac's zone
    /// (the first update to 2.1 is installed by it): such a line is still
    /// read, as this Mac's time.
    func test_an_install_logged_without_an_offset_is_read_in_this_mac_s_zone() throws {
        let attempt = UpdateStore.Attempt(version: "2.0.1", from: "2.0", at: clock.now + 0.7)
        func counts(at date: Date) throws -> Bool {
            try writeLog([logged(Self.installedLine(), at: date, offset: false)])
            return UpdateStore.installLogged(attempt, in: updateLog)
        }

        XCTAssertTrue(try counts(at: clock.now + 2))
        XCTAssertTrue(try counts(at: clock.now), "the attempt's own second")
        XCTAssertFalse(try counts(at: clock.now - 2))
        XCTAssertFalse(logged("x", at: clock.now, offset: false).contains("+"), "the form tested is 2.0.2's")
    }

    /// A time with anything but a plain ±hhmm after it isn't the helper's,
    /// so the line doesn't count. Each line's time is half an hour before
    /// the attempt in UTC's digits: read with an offset of -0100, it's half
    /// an hour after, and counts (the first line); read with any of the
    /// others, as an offset or as anything, it would count too.
    func test_a_time_with_a_malformed_offset_is_not_read() throws {
        let attempt = UpdateStore.Attempt(version: "2.0.1", from: "2.0", at: clock.now)
        let utc = try XCTUnwrap(TimeZone(identifier: "UTC"))
        let time = String(logged("x", at: clock.now - 30 * 60, in: utc).prefix(19))
        try writeLog(["\(time) -0100 \(Self.installedLine())"])
        XCTAssertTrue(UpdateStore.installLogged(attempt, in: updateLog), "a well-formed -0100")

        for offset in ["-01:00", "0100", "-010", "-01000", "-\u{FF10}\u{FF11}\u{FF10}\u{FF10}", "~0100", "-01a0",
                       "-2400", "-0060", " -0100"] {
            try writeLog(["\(time) \(offset) \(Self.installedLine())"])
            XCTAssertFalse(UpdateStore.installLogged(attempt, in: updateLog), offset)
        }
    }

    /// A genuine failure still says so, whatever else the log holds: the
    /// helper's own "not installed", no log at all, an install of the same
    /// version from before the attempt, or of another version.
    func test_a_launch_on_the_old_version_with_no_install_logged_since_says_it_did_not_take() throws {
        let at = clock.now
        let refused = "not installed: couldn't move Graft 2.0.1 to \(Self.appPath)"
        let logs: [(String, [String]?)] = [
            ("the helper said no", [logged(refused, at: at + 2)]),
            ("no log", nil),
            ("an empty log", []),
            ("an earlier install of 2.0.1", [logged(Self.installedLine(), at: at - 1), logged(refused, at: at + 2)]),
            ("another version", [logged(Self.installedLine("2.0.2"), at: at + 2)]),
            ("a version 2.0.1 begins", [logged(Self.installedLine("2.0.10"), at: at + 2)]),
            ("not the helper's line", [logged("waiting for Graft (pid 4242) to quit, to install Graft 2.0.1 at "
                                              + Self.appPath, at: at + 2),
                                       "x installed Graft 2.0.1 at \(Self.appPath)",
                                       Self.installedLine()]),
        ]
        for (name, lines) in logs {
            let defaults = try throwawayDefaults()
            try attempt(at: at, in: defaults)
            try? FileManager.default.removeItem(at: updateLog)
            if let lines { try writeLog(lines) }

            let sut = updatingStore(try UpdateCLI(test: self), defaults: defaults)

            XCTAssertEqual(sut.phase, .failed(version: "2.0.1", message: UpdateStore.installDidNotTake, retryable: false), name)
            XCTAssertEqual(defaults.string(forKey: UpdateStore.Key.failedVersion), "2.0.1", name)
        }
    }

    /// An attempt with no time (2.0.2 wrote them so) can't be placed in the
    /// log: as before, a launch still on the old version says it didn't
    /// take.
    func test_an_attempt_with_no_time_does_not_read_the_log() throws {
        let defaults = try throwawayDefaults()
        try attempt(at: nil, in: defaults)
        try writeLog([logged(Self.installedLine(), at: clock.now)])

        let sut = updatingStore(try UpdateCLI(test: self), defaults: defaults)

        XCTAssertEqual(sut.phase, .failed(version: "2.0.1", message: UpdateStore.installDidNotTake, retryable: false))
    }

    /// Only the log's last stretch is read: the helper appends to it on
    /// every update, so the newest lines are at its end.
    func test_an_install_logged_after_a_long_log_still_counts() throws {
        let defaults = try throwawayDefaults()
        try attempt(at: clock.now, in: defaults)
        let filler = Array(repeating: logged("not installed: an older try", at: clock.now - 86_400), count: 40_000)
        try writeLog(filler + [logged(Self.installedLine(), at: clock.now + 2)])

        let sut = updatingStore(try UpdateCLI(test: self), defaults: defaults)

        XCTAssertEqual(sut.phase, .idle)
    }

    /// Something that isn't a file where the log goes (here a FIFO, whose
    /// open would wait for a writer forever) isn't read: the launch doesn't
    /// hang on it, and can't tell, so it says the install didn't take.
    func test_a_log_that_is_not_a_file_is_not_read() async throws {
        try? FileManager.default.removeItem(at: updateLog)
        XCTAssertEqual(mkfifo(updateLog.path, 0o600), 0)
        let attempt = UpdateStore.Attempt(version: "2.0.1", from: "2.0", at: clock.now)
        let log = updateLog
        final class Reply: @unchecked Sendable {
            private let lock = NSLock()
            private var value: Bool?
            func set(_ new: Bool) { lock.withLock { value = new } }
            func get() -> Bool? { lock.withLock { value } }
        }
        let reply = Reply()
        let read = Task.detached { reply.set(UpdateStore.installLogged(attempt, in: log)) }
        let deadline = Date() + 3
        while reply.get() == nil, Date() < deadline {
            try await Task.sleep(for: .milliseconds(20))
        }
        let answered = reply.get()
        if answered == nil {
            // A reader stuck in open() goes once the FIFO's other end opens.
            let fd = open(log.path, O_WRONLY | O_NONBLOCK)
            if fd >= 0 { close(fd) }
        }
        await read.value
        XCTAssertEqual(answered, false, "read the FIFO, or hung on it")
    }

    /// A symlink where the log goes isn't followed, even to a file that
    /// says it installed: one open that refuses it (`O_NOFOLLOW`), so
    /// nothing can be swapped in between a look at the path and the read.
    func test_a_symlink_at_the_log_path_is_not_followed() throws {
        let elsewhere = updateLog.deletingLastPathComponent().appending(path: "elsewhere.log")
        try (logged(Self.installedLine(), at: clock.now + 2) + "\n").write(to: elsewhere, atomically: true, encoding: .utf8)
        try? FileManager.default.removeItem(at: updateLog)
        try FileManager.default.createSymbolicLink(at: updateLog, withDestinationURL: elsewhere)
        let attempt = UpdateStore.Attempt(version: "2.0.1", from: "2.0", at: clock.now)

        XCTAssertTrue(UpdateStore.installLogged(attempt, in: elsewhere), "the file itself says so")
        XCTAssertFalse(UpdateStore.installLogged(attempt, in: updateLog), "read through a symlink")
    }

    /// The read is the log's last MiB, to the byte: a line that starts
    /// exactly a MiB from the end is read whole; one a byte further back
    /// loses its first byte, and isn't the helper's line any more.
    func test_only_the_log_s_last_mib_is_read() throws {
        let mib = 1 << 20
        let installed = logged(Self.installedLine(), at: clock.now + 2) + "\n"
        let attempt = UpdateStore.Attempt(version: "2.0.1", from: "2.0", at: clock.now)
        // Before the line, a stretch that says nothing, so the log is longer
        // than a MiB either way; after it, `after` bytes that say nothing.
        func log(after: Int) throws {
            let before = String(repeating: "y", count: 99) + "\n"
            let filler = String(repeating: "x", count: after - 1) + "\n"
            try (before + installed + filler).write(to: updateLog, atomically: true, encoding: .utf8)
        }

        try log(after: mib - installed.utf8.count)
        XCTAssertTrue(UpdateStore.installLogged(attempt, in: updateLog), "starts a MiB from the end")
        try log(after: mib - installed.utf8.count + 1)
        XCTAssertFalse(UpdateStore.installLogged(attempt, in: updateLog), "starts a MiB and a byte from the end")
    }

    /// A log whose lines end CRLF (an editor saved it so) is still read a
    /// line at a time: the helper's line after another one counts.
    func test_a_log_with_crlf_lines_is_read_line_by_line() throws {
        let defaults = try throwawayDefaults()
        try attempt(at: clock.now, in: defaults)
        let lines = [
            logged("waiting for Graft (pid 4242) to quit, to install Graft 2.0.1 at \(Self.appPath)", at: clock.now + 1),
            logged(Self.installedLine(), at: clock.now + 2),
        ]
        try (lines.joined(separator: "\r\n") + "\r\n").write(to: updateLog, atomically: true, encoding: .utf8)

        let sut = updatingStore(try UpdateCLI(test: self), defaults: defaults)

        XCTAssertEqual(sut.phase, .idle)
    }

    /// The log's lines: split at LF bytes, with a CR dropped only where it
    /// ends a line (CRLF), one at most.
    func test_the_log_s_lines_split_at_lf_and_drop_a_crlf_s_cr() {
        XCTAssertEqual(UpdateStore.lines(of: Data("a\r\nb\nc\r\n".utf8)), ["a", "b", "c", ""])
        XCTAssertEqual(UpdateStore.lines(of: Data("a\rb\r\r\n\n".utf8)), ["a\rb\r", "", ""])
        XCTAssertEqual(UpdateStore.lines(of: Data()), [""])
    }

    /// The helper's line for an install that worked, word for word as
    /// `app_update.HELPER` writes it, but for its second and the folder it
    /// ran in (here /private/var/empty/Example). Its offset is the one the
    /// helper writes in the zone that test runs it in (+1400). A plain
    /// literal: tests/test_app_update_helper.py reads it from this file,
    /// runs the real helper, and checks the two match.
    static let helperInstalledLine = "2026-11-24 09:30:15 +1400 installed Graft 2.0.2 at /private/var/empty/Example/Applications/Graft.app; the old one is in the Trash at /private/var/empty/Example/Trash/Graft.app"

    /// The round trip: the helper's own line, read by the app. It counts for
    /// an attempt written down before it, or in its second, and not for one
    /// after it, nor for another version's. Its time, 09:30:15 at +1400,
    /// is 19:30:15 UTC the day before, whatever this Mac's zone is.
    func test_the_helper_s_own_line_is_read_as_its_install() throws {
        try writeLog([Self.helperInstalledLine])
        var utc = Calendar(identifier: .gregorian)
        utc.timeZone = try XCTUnwrap(TimeZone(identifier: "UTC"))
        let then = try XCTUnwrap(utc.date(from: DateComponents(
            year: 2026, month: 11, day: 23, hour: 19, minute: 30, second: 15
        )))
        XCTAssertTrue(Self.helperInstalledLine.hasPrefix("2026-11-24 09:30:15 +1400 "))
        func counts(_ version: String, at: Date) -> Bool {
            UpdateStore.installLogged(UpdateStore.Attempt(version: version, from: "2.0.1", at: at), in: updateLog)
        }

        XCTAssertTrue(counts("2.0.2", at: then - 1))
        XCTAssertTrue(counts("2.0.2", at: then + 0.5), "the same second")
        XCTAssertFalse(counts("2.0.2", at: then + 1), "an install before the attempt")
        XCTAssertFalse(counts("2.0.3", at: then - 1), "another version")
    }

    // MARK: The generation counter

    /// A download superseded by a newer release's: its late success can't
    /// grant install rights over the newer one, nor its late failure take
    /// them back. The first stand-in ignores the cancel's SIGTERM and
    /// finishes when told, after the second is ready.
    func test_a_superseded_download_neither_grants_nor_revokes() async throws {
        for late in [Self.downloaded("2.0.1", path: "/cache/old/Graft.app"),
                     Self.notDownloaded("signature", "Late failure.")] {
            let cli = try UpdateCLI(test: self)
            try cli.write("check1", line())
            try cli.write("check2", line(latest: "2.0.2"))
            try cli.write("download1.sh", "trap '' TERM\n" + Self.prints(Self.progress(received: 1, total: 10))
                .replacingOccurrences(of: "exit 0\n", with: "")
                + cli.wait(for: "go") + Self.prints(late))
            try cli.write("download2.sh", Self.prints(Self.downloaded("2.0.2", path: "/cache/new/Graft.app")))
            try cli.write("install", Self.installed())
            let sut = updatingStore(cli, defaults: try throwawayDefaults())

            await sut.checkIfDue()
            await eventually({ sut.phase == .downloading(version: "2.0.1", fraction: 0.1) }, "the first runs")
            let first = sut.generation
            await sut.checkNow()
            await eventually({ sut.phase == .ready(version: "2.0.2") }, "the second is ready")
            XCTAssertEqual(sut.generation, first + 1)

            try cli.go("go")
            // Long enough for the first to finish and be read.
            try await Task.sleep(for: .milliseconds(600))
            XCTAssertEqual(sut.phase, .ready(version: "2.0.2"), "late: \(late)")

            var replies: [Bool] = []
            XCTAssertTrue(sut.handOver { replies.append($0) })
            await eventually({ replies == [true] })
            XCTAssertEqual(cli.runs(of: "--install-update").first?.contains("/cache/new/Graft.app"), true)
        }
    }

    // MARK: Through the quit

    func test_a_quit_with_an_update_ready_waits_for_the_helper_then_quits() async throws {
        let cli = try UpdateCLI(test: self)
        let sut = try await readyStore(cli, defaults: try throwawayDefaults())
        try cli.write("install", Self.installed())
        var replies: [Bool] = []
        let quitGuard = QuitGuard(
            hold: hold,
            present: { _, _ in XCTFail("nothing is held: nothing to ask"); return { $0() } },
            reply: { replies.append($0) },
            schedule: { _, _ in }
        )
        quitGuard.handover = sut

        XCTAssertEqual(quitGuard.shouldTerminate(), .terminateLater)
        XCTAssertEqual(quitGuard.shouldTerminate(), .terminateCancel, "a second quit can't skip the install")
        await eventually({ replies == [true] })
        XCTAssertEqual(cli.runs(of: "--install-update").count, 1)
        XCTAssertEqual(quitGuard.shouldTerminate(), .terminateNow,
                       "the helper is started: a later quit has nothing more to hand over")
    }

    // MARK: Settings and the alert

    func test_the_alert_offers_update_only_to_a_copy_that_can() async throws {
        let cli = try UpdateCLI(test: self)
        try cli.write("check", line())
        let can = updatingStore(cli, defaults: try throwawayDefaults())
        can.autoInstall = false
        await can.checkNow()
        XCTAssertEqual(can.answerAlert?.action, .update)
        XCTAssertTrue(can.answerAlert?.message.contains("Update downloads") == true)

        let cannot = store(try fakeCLI(line()), defaults: try throwawayDefaults())
        await cannot.checkNow()
        XCTAssertNil(cannot.answerAlert?.action)
        XCTAssertEqual(cannot.answerAlert?.cancel, "Later")
        XCTAssertNotNil(cannot.answerAlert?.release, "Open Release Page")
        XCTAssertTrue(cannot.answerAlert?.message.contains("Download the new Graft.zip") == true)

        XCTAssertEqual(UpdatesSection.toggleTitle, "Check for updates automatically")
        XCTAssertEqual(UpdatesSection.installToggleTitle, "Download and install updates automatically")
    }

    // MARK: Check for Updates… while updating

    /// A stand-in download that says how far it got (`first`), then waits
    /// for the test's "go" before it's done.
    private static func waits(_ cli: UpdateCLI, after first: String) -> String {
        prints(first).replacingOccurrences(of: "exit 0\n", with: "") + cli.wait(for: "go") + prints(downloaded())
    }

    /// The bug: the alert said "available" and offered Update while the
    /// download the check had just started ran, and still did once the
    /// banner said Restart to Update. It now says the download has started,
    /// offers only OK and Open Release Page, and stays as it was (true, and
    /// in step with the banner) when the download is done. With the daily
    /// check off too: the banner shows a download whatever that switch says.
    func test_check_for_updates_while_its_download_runs_offers_nothing_to_start() async throws {
        for daily in [true, false] {
            let cli = try UpdateCLI(test: self)
            try cli.write("check", line())
            try cli.write("download.sh", Self.waits(cli, after: Self.progress(received: 10, total: 100)))
            let sut = updatingStore(cli, defaults: try throwawayDefaults())
            sut.dailyCheck = daily

            await sut.checkNow()

            guard case let .downloading(version, _) = sut.phase, version == "2.0.1" else {
                return XCTFail("not downloading: \(sut.phase)")
            }
            let alert = try XCTUnwrap(sut.answerAlert)
            XCTAssertEqual(alert.title, "Graft 2.0.1 is available")
            XCTAssertNil(alert.action, "nothing to start: it's downloading")
            XCTAssertEqual(alert.cancel, "OK")
            XCTAssertEqual(alert.release?.latest, "2.0.1", "Open Release Page")
            XCTAssertTrue(alert.message.hasPrefix("You have Graft 2.0. Graft has started downloading it"), alert.message)
            XCTAssertTrue(alert.message.contains("The banner at the bottom of the window shows how it's going."))
            XCTAssertEqual(sut.notice?.version, "2.0.1", "the banner it points to is there")

            try cli.go("go")
            await eventually({ sut.phase == .ready(version: "2.0.1") }, "ready")
            XCTAssertEqual(sut.answerAlert, alert, "fixed while it's open: no button changes under the pointer")
            XCTAssertEqual(sut.notice?.state, .ready(installsOnQuit: true))
            XCTAssertEqual(cli.runs(of: "--download-update").count, 1)
            XCTAssertEqual(terminated, 0, "nobody asked to restart")
        }
    }

    /// Being checked is the same: the download is under way.
    func test_check_for_updates_while_the_download_is_checked_offers_nothing_to_start() async throws {
        let cli = try UpdateCLI(test: self)
        try cli.write("check", line())
        try cli.write("download.sh", Self.waits(cli, after: Self.progress(received: nil, total: nil, check: "signature")))
        let sut = updatingStore(cli, defaults: try throwawayDefaults())
        await sut.checkIfDue()
        await eventually({ sut.phase == .verifying(version: "2.0.1") }, "checking")

        await sut.checkNow()

        XCTAssertNil(sut.answerAlert?.action)
        XCTAssertEqual(sut.answerAlert?.cancel, "OK")
        XCTAssertTrue(sut.answerAlert?.message.contains("has started downloading it") == true)
        try cli.go("go")
        await eventually({ sut.phase == .ready(version: "2.0.1") }, "ready")
        XCTAssertEqual(cli.runs(of: "--download-update").count, 1)
    }

    /// Check for Updates… chosen while the daily check runs gets that
    /// check's answer (`checkIfDue`), built once the download that check
    /// starts has started: it offers nothing to start, as when Check for
    /// Updates… runs the check itself.
    func test_check_for_updates_during_a_daily_check_that_starts_the_download_offers_nothing_to_start() async throws {
        let cli = try UpdateCLI(test: self)
        try cli.write("check", line())
        try cli.write("check.sh", cli.wait(for: "checked"))
        try cli.write("download.sh", Self.waits(cli, after: Self.progress(received: 10, total: 100)))
        let sut = updatingStore(cli, defaults: try throwawayDefaults())

        let daily = Task { await sut.checkIfDue() }
        await eventually({ sut.isChecking }, "the daily check started")
        await sut.checkNow()
        XCTAssertNil(sut.answerAlert, "answered before the check finished")
        try cli.go("checked")
        await daily.value

        guard case .downloading(version: "2.0.1", _) = sut.phase else {
            return XCTFail("not downloading: \(sut.phase)")
        }
        let alert = try XCTUnwrap(sut.answerAlert)
        XCTAssertNil(alert.action, "nothing to start: it's downloading")
        XCTAssertEqual(alert.cancel, "OK")
        XCTAssertTrue(alert.message.contains("Graft has started downloading it"), alert.message)
        XCTAssertEqual(cli.runs(of: "--check-update").count, 1)
        try cli.go("go")
        await eventually({ sut.phase == .ready(version: "2.0.1") }, "ready")
    }

    /// Ready: the banner's Restart to Update, and the banner's words. The
    /// button restarts into the download it has: no second one.
    func test_check_for_updates_when_it_is_ready_offers_restart_to_update() async throws {
        let cli = try UpdateCLI(test: self)
        let sut = try await readyStore(cli, defaults: try throwawayDefaults())

        await sut.checkNow()

        let alert = try XCTUnwrap(sut.answerAlert)
        XCTAssertEqual(alert.title, "Graft 2.0.1 is ready")
        XCTAssertEqual(alert.action, .restartToUpdate)
        XCTAssertEqual(alert.cancel, "Later")
        XCTAssertNotNil(alert.release)
        XCTAssertEqual(alert.message, "You have Graft 2.0. Graft 2.0.1 is downloaded and checked, and installs "
            + "when you quit Graft. Restart to Update installs it now.")
        sut.perform(.restartToUpdate)
        XCTAssertEqual(terminated, 1, "restarts into it")
        XCTAssertEqual(cli.runs(of: "--download-update").count, 1, "no second download")

        // With the second switch off, a quit doesn't install it, so the
        // alert doesn't say it does, nor does the banner.
        let other = try UpdateCLI(test: self)
        let off = try await readyStore(other, defaults: try throwawayDefaults())
        off.autoInstall = false
        await off.checkNow()
        XCTAssertEqual(off.notice?.state, .ready(installsOnQuit: false))
        XCTAssertEqual(off.answerAlert?.action, .restartToUpdate)
        XCTAssertEqual(off.answerAlert?.message, "You have Graft 2.0. Graft 2.0.1 is downloaded and checked. "
            + "Restart to Update quits Graft, puts it in place of this one, and opens it.")
    }

    /// Failed: what the banner says, and Try Again, which downloads it
    /// again (the banner's Release Page is there too).
    func test_check_for_updates_after_a_failed_download_says_why_and_offers_try_again() async throws {
        let cli = try UpdateCLI(test: self)
        let why = "Couldn't download the new Graft. Check this Mac's internet connection, then try again."
        try cli.write("check", line())
        try cli.write("download1.sh", Self.prints(Self.notDownloaded("offline", why), status: 1))
        try cli.write("download2.sh", Self.prints(Self.downloaded()))
        let sut = updatingStore(cli, defaults: try throwawayDefaults())
        await sut.checkIfDue()
        await eventually({ sut.phase == .failed(version: "2.0.1", message: why, retryable: true) }, "failed")

        await sut.checkNow()

        XCTAssertEqual(sut.answerAlert, UpdateStore.CheckAlert(
            title: "Graft couldn't update to 2.0.1", message: why, action: .tryAgain,
            release: sut.lastAnswer, cancel: "Later"
        ))
        XCTAssertEqual(UpdateNotice.text(try XCTUnwrap(sut.notice)), "Graft couldn't update to 2.0.1")
        XCTAssertEqual(cli.runs(of: "--download-update").count, 1, "not again on its own")
        sut.perform(.tryAgain)
        await eventually({ self.terminated == 1 }, "tried again, and restarts into it")
        XCTAssertEqual(cli.runs(of: "--download-update").count, 2)
    }

    /// Try Again only where trying again can help. Not after a download
    /// refused for what it is (this copy having no team to trust, a
    /// signature that doesn't check out), nor for where this copy is
    /// (translocated, a folder it can't change): the same try meets the
    /// same refusal. After one that went offline, ran out of space or got
    /// no answer at all, Try Again is offered. Either way the alert says
    /// why, and links the release page.
    func test_check_for_updates_offers_try_again_only_when_it_can_help() async throws {
        let cases: [(reason: String?, retryable: Bool)] = [
            ("no-team", false), ("signature", false), ("digest", false), ("translocated", false),
            ("not-writable", false), ("not-graft", false), ("offline", true), ("http", true),
            ("disk", true), ("busy", true), (nil, true),
        ]
        for (reason, retryable) in cases {
            let name = reason ?? "no answer"
            let cli = try UpdateCLI(test: self)
            try cli.write("check", line())
            try cli.write("download.sh", reason.map { Self.prints(Self.notDownloaded($0, "No."), status: 1) }
                ?? "echo 'Traceback' >&2\nexit 1\n")
            let sut = updatingStore(cli, defaults: try throwawayDefaults())
            await sut.checkIfDue()
            await eventually({ if case .failed = sut.phase { return true }; return false }, name)
            guard case let .failed(_, message, got) = sut.phase else { return XCTFail(name) }
            XCTAssertEqual(got, retryable, name)

            await sut.checkNow()

            XCTAssertEqual(sut.answerAlert, UpdateStore.CheckAlert(
                title: "Graft couldn't update to 2.0.1", message: message, action: retryable ? .tryAgain : nil,
                release: sut.lastAnswer, cancel: "Later"
            ), name)
        }
    }

    /// An install that didn't take (the next launch is still the old
    /// version) says to download it by hand: no Try Again. An install the
    /// helper couldn't start is the same as a download: Try Again unless
    /// its reason says no try can help.
    func test_check_for_updates_after_a_failed_install_offers_try_again_only_when_it_can_help() async throws {
        let defaults = try throwawayDefaults()
        try attempt(at: nil, in: defaults)
        let cli = try UpdateCLI(test: self)
        try cli.write("check", line())
        let relaunched = updatingStore(cli, defaults: defaults)

        await relaunched.checkNow()

        XCTAssertEqual(relaunched.answerAlert, UpdateStore.CheckAlert(
            title: "Graft couldn't update to 2.0.1", message: UpdateStore.installDidNotTake, action: nil,
            release: relaunched.lastAnswer, cancel: "Later"
        ))

        for (reason, retryable) in [("not-writable", false), ("no-team", false), ("helper", true)] {
            let cli = try UpdateCLI(test: self)
            let sut = try await readyStore(cli, defaults: try throwawayDefaults())
            try cli.write("install", Self.installed(ok: false, message: "No.", reason: reason))
            try cli.write("install-status", "1")
            var replies: [Bool] = []
            XCTAssertTrue(sut.handOver { replies.append($0) }, reason)
            await eventually({ replies == [true] }, reason)
            XCTAssertEqual(sut.phase, .failed(version: "2.0.1", message: "No.", retryable: retryable), reason)

            await sut.checkNow()

            XCTAssertEqual(sut.answerAlert?.action, retryable ? .tryAgain : nil, reason)
        }
    }

    /// Restart to Update in the alert is the banner's Restart to Update
    /// (`restartToUpdate`): it asks for this quit, and Keep Working takes
    /// that back. With the second switch off, a later quit then installs
    /// nothing, after either; with it on, a later quit installs it without
    /// reopening, after either. (The alert's was `update()`, which asked
    /// for every later quit to install it, so after a canceled quit each
    /// one did, whatever the switch said.)
    func test_restart_to_update_in_the_alert_is_the_banner_s() async throws {
        struct Outcome: Equatable {
            let quits: Int
            let afterKeepWorking: UpdateStore.Notice.State?
            let laterQuitInstalls: [String]
        }
        func press(fromAlert: Bool, autoInstall: Bool) async throws -> Outcome {
            let cli = try UpdateCLI(test: self)
            let sut = try await readyStore(cli, defaults: try throwawayDefaults())
            sut.autoInstall = autoInstall
            try cli.write("install", Self.installed())
            let before = terminated
            if fromAlert {
                await sut.checkNow()
                let alert = try XCTUnwrap(sut.answerAlert)
                XCTAssertEqual(alert.action, .restartToUpdate)
                sut.perform(try XCTUnwrap(alert.action))
                sut.dismissAnswer()
            } else {
                sut.restartToUpdate()
            }
            let quits = terminated - before
            sut.quitCanceled()
            let state = sut.notice?.state
            var replies: [Bool] = []
            if sut.handOver({ replies.append($0) }) {
                await eventually({ replies == [true] }, "the later quit")
            }
            return Outcome(quits: quits, afterKeepWorking: state, laterQuitInstalls: cli.runs(of: "--install-update"))
        }

        for autoInstall in [false, true] {
            let banner = try await press(fromAlert: false, autoInstall: autoInstall)
            let alert = try await press(fromAlert: true, autoInstall: autoInstall)
            XCTAssertEqual(alert, banner, "switch \(autoInstall)")
            XCTAssertEqual(banner, Outcome(
                quits: 1, afterKeepWorking: .ready(installsOnQuit: autoInstall),
                laterQuitInstalls: autoInstall
                    ? ["--json --install-update /cache/a/Graft.app --app-path \(Self.appPath) --wait-pid 4242"]
                    : []
            ), "switch \(autoInstall)")
        }
    }

    /// The alert's buttons are named as the banner's are.
    func test_the_alert_s_buttons_are_named_as_the_banner_s() {
        XCTAssertEqual(UpdateStore.CheckAlert.Action.update.title, UpdateNotice.updateTitle)
        XCTAssertEqual(UpdateStore.CheckAlert.Action.restartToUpdate.title, UpdateNotice.restartTitle)
        XCTAssertEqual(UpdateStore.CheckAlert.Action.tryAgain.title, "Try Again")
    }

    /// With the second switch on, a version that failed in an earlier
    /// launch isn't downloaded on its own: nothing is under way, and the
    /// alert offers Update, as the banner does.
    func test_check_for_updates_for_a_version_that_failed_before_offers_update() async throws {
        let cli = try UpdateCLI(test: self)
        let defaults = try throwawayDefaults()
        defaults.set("2.0.1", forKey: UpdateStore.Key.failedVersion)
        try cli.write("check", line())
        let sut = updatingStore(cli, defaults: defaults)

        await sut.checkNow()

        XCTAssertEqual(sut.phase, .idle)
        XCTAssertEqual(sut.notice?.state, .available(canUpdate: true))
        XCTAssertEqual(sut.answerAlert?.action, .update)
        XCTAssertTrue(sut.answerAlert?.message.contains("Update downloads") == true)
        XCTAssertEqual(cli.runs(of: "--download-update"), [])
    }

    /// An alert that offered Update before any download ran (the second
    /// switch off), pressed after the switch went on and its download
    /// started: Update joins that download, and restarts into it. No
    /// second one.
    func test_update_from_an_alert_shown_before_the_download_started_joins_it() async throws {
        let cli = try UpdateCLI(test: self)
        try cli.write("check", line())
        try cli.write("download.sh", Self.waits(cli, after: Self.progress(received: 10, total: 100)))
        let sut = updatingStore(cli, defaults: try throwawayDefaults())
        sut.autoInstall = false
        await sut.checkNow()
        XCTAssertEqual(sut.answerAlert?.action, .update)

        sut.autoInstall = true
        await eventually({ cli.runs(of: "--download-update").count == 1 }, "the switch started it")
        sut.perform(.update)
        try cli.go("go")

        await eventually({ self.terminated == 1 }, "restarts into it: Update was asked for")
        XCTAssertEqual(cli.runs(of: "--download-update").count, 1)
    }

    /// Installing, as Graft quits: nothing to offer.
    func test_check_for_updates_while_it_installs_offers_nothing() async throws {
        let cli = try UpdateCLI(test: self)
        let sut = try await readyStore(cli, defaults: try throwawayDefaults())
        try cli.write("install", Self.installed())
        XCTAssertTrue(sut.handOver { _ in })

        await sut.checkNow()

        XCTAssertEqual(sut.answerAlert, UpdateStore.CheckAlert(
            title: "Installing Graft 2.0.1…", message: "Graft installs it as it quits.",
            action: nil, release: nil, cancel: "OK"
        ))
    }
}
