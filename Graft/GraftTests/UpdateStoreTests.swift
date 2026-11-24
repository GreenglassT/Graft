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
            appVersion: appVersion, open: { [weak self] in self?.opened.append($0) }
        )
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
        XCTAssertEqual(sut.answer?.title, "You're up to date")
        XCTAssertEqual(sut.answer?.message(appVersion: sut.appVersion), "Graft 2.0 is the newest version.")
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
        XCTAssertEqual(sut.answer?.title, "Graft 2.0.1 is available")
        XCTAssertTrue(sut.answer?.message(appVersion: "2.0").hasPrefix("You have Graft 2.0.") == true)
        XCTAssertEqual(sut.banner, release)
        sut.openReleasePage(release)
        XCTAssertEqual(opened, [URL(string: Self.page)!])
    }

    func test_check_for_updates_says_why_it_failed() async throws {
        let sut = store(try fakeCLI(line(ok: false, message: Self.offline), status: 1), defaults: try throwawayDefaults())

        await sut.checkNow()

        XCTAssertEqual(sut.answer, .failed(Self.offline))
        XCTAssertEqual(sut.answer?.title, "Couldn't check for updates")
        XCTAssertEqual(sut.answer?.message(appVersion: "2.0"), Self.offline)
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
            appVersion: "2.0", open: { _ in XCTFail("nothing to open") }
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
    /// check prints `check<n>` for its n-th run, or `check`; a download
    /// runs `download<n>.sh`, or `download.sh`; an install runs
    /// `install.sh` if there is one, then prints `install` and exits with
    /// `install-status`.
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

    private static func installed(ok: Bool = true, message: String? = nil) -> String {
        json(["type": "update-install", "ok": ok, "version": ok ? "2.0.1" : nil,
              "log": ok ? "/logs/update.log" : nil, "relaunch": false, "url": nil,
              "reason": ok ? nil : "not-writable", "message": message])
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
            appVersion: "2.0", open: { _ in }
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
        await eventually({ sut.phase == .failed(version: "2.0.1", message: why) }, "failed")

        XCTAssertEqual(sut.notice?.state, .failed(why))
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
        await eventually({ sut.phase == .failed(version: "2.0.1", message: "No.") }, "failed")

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
        XCTAssertEqual(attempt, UpdateStore.Attempt(version: "2.0.1", from: "2.0"))
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
        XCTAssertEqual(sut.phase, .failed(version: "2.0.1", message: why))
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
        XCTAssertEqual(sut.notice?.state, .failed("No."))
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

        XCTAssertEqual(sut.phase, .failed(version: "2.0.1", message: UpdateStore.installDidNotTake))
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

    func test_the_alert_offers_update_only_to_a_copy_that_can() {
        let record = UpdateCheckRecord(ok: true, current: "2.0", latest: "2.0.1", url: Self.page,
                                       updateAvailable: true, reason: nil, status: nil, message: nil)
        let answer = UpdateStore.Answer.available(record)
        XCTAssertTrue(answer.message(appVersion: "2.0", canUpdate: true).contains("Update downloads"))
        XCTAssertTrue(answer.message(appVersion: "2.0").contains("Download the new Graft.zip"))
        XCTAssertEqual(UpdatesSection.toggleTitle, "Check for updates automatically")
        XCTAssertEqual(UpdatesSection.installToggleTitle, "Download and install updates automatically")
    }
}
