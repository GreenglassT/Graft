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

    func test_only_a_web_page_is_opened() async throws {
        let sut = store(try fakeCLI(line(url: "file:///Applications/Example.app")), defaults: try throwawayDefaults())
        await sut.checkIfDue()
        sut.openReleasePage(try XCTUnwrap(sut.banner))
        XCTAssertEqual(opened, [])
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
}
