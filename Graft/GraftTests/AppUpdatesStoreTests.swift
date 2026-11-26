import Combine
import XCTest
@testable import Graft

/// `AppUpdatesStore` against a stand-in `graft` that answers the quick plan,
/// the full plan and a run from canned Plan A lines. No real CLI, phone or
/// preference: a throwaway defaults suite, a fixed clock, a hold over a
/// control that does nothing.
@MainActor
final class AppUpdatesStoreTests: XCTestCase {
    private typealias F = UpdatePlanFixture

    private final class Clock {
        var now = Date(timeIntervalSinceReferenceDate: 800_000_000)
    }

    private final class InertControl: TerminationControl, @unchecked Sendable {
        func disableSuddenTermination() {}
        func enableSuddenTermination() {}
        func disableAutomaticTermination(_ reason: String) {}
        func enableAutomaticTermination(_ reason: String) {}
    }

    private let clock = Clock()
    private let hold = TerminationHold(control: InertControl())

    private func throwawayDefaults() throws -> UserDefaults {
        let name = "AppUpdatesStoreTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: name))
        addTeardownBlock { defaults.removePersistentDomain(forName: name) }
        return defaults
    }

    private func temporaryURL(_ name: String) -> URL {
        let url = FileManager.default.temporaryDirectory.appending(path: "\(name)-\(UUID().uuidString)")
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }

    /// A `graft` that prints `quick` for `--plan --quick`, `full` for
    /// `--plan`, `list` for `--list`, and `run` for anything else, after
    /// `runFirst`. With `failIf`, the quick plan exits 1 once that file
    /// exists.
    private func fakeCLI(
        quick: String = "", full: String = "", fullExit: Int = 0, run: String = "",
        runExit: Int = 0, runFirst: String = "", failIf: URL? = nil, list: String = ""
    ) throws -> ScriptCLI {
        let fail = failIf.map { "[ -e '\($0.path)' ] && exit 1" } ?? ":"
        return try ScriptCLI("""
        case "$*" in
        *--quick*)
        \(fail)
        cat <<'EOF'
        \(quick)
        EOF
        ;;
        *--plan*)
        cat <<'EOF'
        \(full)
        EOF
        exit \(fullExit)
        ;;
        *--list*)
        cat <<'EOF'
        \(list)
        EOF
        ;;
        *)
        \(runFirst.isEmpty ? ":" : runFirst)
        cat <<'EOF'
        \(run)
        EOF
        exit \(runExit)
        ;;
        esac
        """, test: self)
    }

    private func makeStore(_ cli: ScriptCLI, defaults: UserDefaults? = nil) throws -> (AppUpdatesStore, ConfigStore) {
        let config = ConfigStore(runner: cli.runner, notify: { _ in }, hold: hold)
        let clock = self.clock
        let store = AppUpdatesStore(
            runner: cli.runner, config: config, defaults: try defaults ?? throwawayDefaults(),
            now: { clock.now }, hold: hold
        )
        return (store, config)
    }

    // MARK: The daily check

    func test_the_daily_check_is_the_quick_plan_of_every_app_and_keeps_what_it_found() async throws {
        let cli = try fakeCLI(quick: F.planLine([F.app("example", to: "2.0"),
                                                 F.app("bravo", install: false, nothingToDo: true)]))
        let (store, _) = try makeStore(cli)

        await store.checkIfDue()

        XCTAssertEqual(cli.argv(), ["--json --update --all --plan --quick"])
        XCTAssertEqual(store.plans["example"]?.hasUpdate, true)
        XCTAssertEqual(store.plans["bravo"]?.hasUpdate, false)
    }

    func test_it_runs_at_launch_then_once_a_day() async throws {
        let cli = try fakeCLI(quick: F.planLine([F.app("example")]))
        let (store, _) = try makeStore(cli)

        await store.checkIfDue()
        await store.checkIfDue()
        XCTAssertEqual(cli.argv().count, 1, "not again within the day")

        clock.now += AppUpdatesStore.interval + 1
        await store.checkIfDue()
        XCTAssertEqual(cli.argv().count, 2)
    }

    func test_the_switch_is_on_by_default_and_off_checks_nothing_and_clears_the_badges() async throws {
        let cli = try fakeCLI(quick: F.planLine([F.app("example")]))
        let defaults = try throwawayDefaults()
        let (store, _) = try makeStore(cli, defaults: defaults)
        XCTAssertTrue(store.checkApps)
        await store.checkIfDue()
        XCTAssertFalse(store.plans.isEmpty)

        store.checkApps = false

        XCTAssertTrue(store.plans.isEmpty)
        XCTAssertEqual(defaults.object(forKey: AppUpdatesStore.Key.check) as? Bool, false)
        clock.now += AppUpdatesStore.interval + 1
        await store.checkIfDue()
        await store.recheck(apps: ["example"])
        XCTAssertEqual(cli.argv().count, 1, "nothing runs with the switch off")
    }

    /// Off then on (review S5): the next check is due at once, not a day
    /// after the last.
    func test_turning_the_switch_off_and_on_checks_again_at_once() async throws {
        let cli = try fakeCLI(quick: F.planLine([F.app("example")]))
        let (store, _) = try makeStore(cli)
        await store.checkIfDue()
        XCTAssertEqual(cli.argv().count, 1)

        store.checkApps = false
        store.checkApps = true

        await eventually { cli.argv().count == 2 && !store.plans.isEmpty }
        await store.checkIfDue()
        XCTAssertEqual(cli.argv().count, 2, "and that one counts as the day's")
    }

    /// With the switch off there's no automatic check, but "Check for
    /// updates now" still runs (spec §11.4), and what it finds is what the
    /// Update buttons show: it is kept, as it was found.
    func test_with_the_switch_off_check_now_still_runs_and_drives_update() async throws {
        let cli = try fakeCLI(quick: F.planLine([F.app("example", to: "2.0")]))
        let defaults = try throwawayDefaults()
        defaults.set(false, forKey: AppUpdatesStore.Key.check)
        let (store, _) = try makeStore(cli, defaults: defaults)
        await store.checkIfDue()
        XCTAssertEqual(cli.argv(), [], "no automatic check")

        await store.checkNow()

        XCTAssertEqual(cli.argv(), ["--json --update --all --plan --quick"])
        XCTAssertEqual(store.plans["example"]?.store.to, "2.0")
        XCTAssertFalse(store.checkApps)
    }

    /// The switch off means nothing is sent but by Check for updates now
    /// (spec §11.4, review part 1 #3): after one, a later build, get, tweak
    /// update, update or change of devices reads no plan, and the buttons
    /// keep what that check found.
    func test_with_the_switch_off_a_check_now_starts_no_rechecks() async throws {
        let cli = try fakeCLI(quick: waitingPlan, run: F.end(ok: true, updated: ["example"]))
        let defaults = try throwawayDefaults()
        defaults.set(false, forKey: AppUpdatesStore.Key.check)
        let (store, _) = try makeStore(cli, defaults: defaults)
        await store.checkNow()
        XCTAssertEqual(cli.argv().count, 1, "premise: the check by hand ran")
        let found = store.plans
        XCTAssertFalse(found.isEmpty, "premise")

        // A build of the app ended (`ContentView` asks for its plan).
        await store.recheck(apps: ["example"])
        // A tweak update ended, or an update did.
        await store.recheck(apps: nil)
        // A device came or went while the iPad waits.
        await store.deviceSetChanged()
        // An update ran and finished.
        XCTAssertTrue(store.start(apps: ["example"], withAffected: false, plan: nil))
        await eventually { store.lastFinished != nil }
        try await Task.sleep(for: .milliseconds(200))

        XCTAssertEqual(cli.argv().filter { $0.contains("--plan") }, ["--json --update --all --plan --quick"],
                       "nothing but the check by hand asked for a plan")
        XCTAssertEqual(store.plans, found, "the buttons keep what it found")
    }

    /// With the switch off, a Stop updating sends no plan either: the
    /// device leaves the line the buttons show, from what was found.
    func test_with_the_switch_off_stop_updating_reads_no_plan() async throws {
        let cli = try fakeCLI(quick: waitingPlan)
        let defaults = try throwawayDefaults()
        defaults.set(false, forKey: AppUpdatesStore.Key.check)
        let (store, _) = try makeStore(cli, defaults: defaults)
        await store.checkNow()
        XCTAssertEqual(store.plans["example"]?.waitingTargets.map(\.udid), [F.pad], "premise")

        await store.dropTarget(app: "example", udid: F.pad)

        XCTAssertEqual(cli.argv(), [
            "--json --update --all --plan --quick",
            "--json \(CLIContract.Flag.dropUpdateTarget) example --device \(F.pad)",
        ])
        XCTAssertEqual(store.plans["example"]?.targets.map(\.udid), [F.phone])
        XCTAssertNil(AppUpdateText.paneLine(store.plans["example"]), "nothing left to show for it")
    }

    func test_a_check_that_comes_due_while_busy_waits_for_the_slot() async throws {
        let cli = try fakeCLI(quick: F.planLine([F.app("example")]))
        let (store, config) = try makeStore(cli)
        XCTAssertTrue(config.beginUpdate(apps: ["bravo"]))

        await store.checkIfDue()
        XCTAssertEqual(cli.argv(), [], "it waits")

        config.endUpdate()
        await store.slotFreed()
        XCTAssertEqual(cli.argv(), ["--json --update --all --plan --quick"])
    }

    func test_a_failed_check_shows_nothing_and_keeps_the_last_answer() async throws {
        let marker = temporaryURL("update-check-fails")
        let cli = try fakeCLI(quick: F.planLine([F.app("example")]), failIf: marker)
        let (store, _) = try makeStore(cli)
        await store.checkIfDue()
        let found = store.plans

        FileManager.default.createFile(atPath: marker.path, contents: nil)
        clock.now += AppUpdatesStore.interval + 1
        await store.checkIfDue()

        XCTAssertEqual(cli.argv().count, 2)
        XCTAssertEqual(store.plans, found)
    }

    // MARK: A device coming back (spec §11.3)

    /// An app whose iPad isn't connected: the iPhone is current.
    private var waitingPlan: String {
        F.planLine([F.app("example", targets: [
            F.target("Example iPhone", install: false),
            F.target("Example iPad", udid: F.pad, waiting: F.notConnected("Example iPad")),
        ])])
    }

    /// A device connected or gone, while an update waits for one: the plan
    /// is read again for every app, so the pane offers the install once the
    /// device is back, without a relaunch.
    func test_a_change_in_the_connected_devices_reads_the_plan_again_when_a_device_waits() async throws {
        let cli = try fakeCLI(quick: waitingPlan)
        let (store, _) = try makeStore(cli)
        await store.checkIfDue()

        await store.deviceSetChanged()

        XCTAssertEqual(cli.argv(), ["--json --update --all --plan --quick", "--json --update --all --plan --quick"])
    }

    /// With no update waiting for a device, a change reads nothing: the
    /// check goes online, and nothing is waiting to be offered.
    func test_a_change_in_the_connected_devices_reads_nothing_when_no_device_waits() async throws {
        let cli = try fakeCLI(quick: F.planLine([F.app("example", to: "2.0")]))
        let (store, _) = try makeStore(cli)
        await store.checkIfDue()

        await store.deviceSetChanged()

        XCTAssertEqual(cli.argv(), ["--json --update --all --plan --quick"])
    }

    /// At most once every 10 minutes. A change inside them is read once
    /// they're up, so a device plugged back in soon after another change is
    /// still noticed.
    func test_device_changes_read_the_plan_at_most_once_every_ten_minutes() async throws {
        XCTAssertEqual(AppUpdatesStore.deviceRecheckInterval, 10 * 60)
        let cli = try fakeCLI(quick: waitingPlan)
        let (store, _) = try makeStore(cli)
        store.deviceRecheckTick = .milliseconds(20)
        await store.checkIfDue()
        await store.deviceSetChanged()
        XCTAssertEqual(cli.argv().count, 2, "premise: the first change reads it")

        clock.now += 5 * 60
        await store.deviceSetChanged()
        await store.deviceSetChanged()
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(cli.argv().count, 2, "not within the 10 minutes")

        clock.now += 5 * 60
        await eventually { cli.argv().count == 3 }
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(cli.argv().count, 3, "the changes inside them, read once")

        await store.deviceSetChanged()
        XCTAssertEqual(cli.argv().count, 3, "and that one counts as the last")
    }

    /// A plan from a run that then failed isn't kept, and doesn't count as
    /// the day's check.
    func test_a_check_that_exits_with_an_error_keeps_nothing_it_printed() async throws {
        let cli = try ScriptCLI("""
        echo '\(F.planLine([F.app("example")]))'
        exit 1
        """, test: self)
        let (store, _) = try makeStore(cli)

        await store.checkIfDue()
        XCTAssertTrue(store.plans.isEmpty, "a failed check shows nothing")

        await store.checkIfDue()
        XCTAssertEqual(cli.argv().count, 2, "and the next one tries again")
    }

    func test_a_recheck_reads_the_plan_again_for_those_apps_only() async throws {
        let cli = try fakeCLI(quick: F.planLine([F.app("example", to: "2.0")]))
        let (store, _) = try makeStore(cli)

        await store.recheck(apps: ["example"])

        XCTAssertEqual(cli.argv(), ["--json --update example --plan --quick"])
        XCTAssertEqual(store.plans["example"]?.store.to, "2.0")
    }

    // MARK: A change to an app's config (hands-on of 2.3)

    /// The list as `--list` read it before an edit: example held at 1.2.0,
    /// bravo and charlie as `appLine` makes them.
    private let before = [F.appRecord("bravo"), F.appRecord("charlie"),
                          F.appRecord("example", with: ["preferred_version": "1.2.0"])]

    /// Releasing a hold in Edit… left "Update available" on the pane and
    /// "Update All (3)" in the sidebar while the CLI had nothing to do: an
    /// app's config changed and its plan wasn't read again. Now it is, for
    /// that app alone.
    func test_a_change_to_an_apps_config_reads_its_plan_again() async throws {
        let cli = try fakeCLI(quick: F.planLine([F.app("example", install: false, nothingToDo: true)]))
        let (store, _) = try makeStore(cli)
        var after = before
        after[2] = F.appRecord("example", with: ["preferred_version": "latest"])

        await store.configChanged(from: before, to: after)

        XCTAssertEqual(cli.argv(), ["--json --update example --plan --quick"])
        XCTAssertEqual(store.plans["example"]?.hasUpdate, false)
    }

    /// Each thing an app's plan is worked out from: its version or hold,
    /// tweaks, delivery, LiveContainer pin, signing, cyan options, bundle id
    /// and App Store country.
    func test_every_input_of_an_apps_plan_counts_as_a_change() async throws {
        let changes: [String: Any] = [
            "preferred_version": "2.0", "tweaks": ["example.deb"], "delivery": "sideload",
            "lc_bundle_id": "com.example.livecontainer", "auto_profile": true,
            "profile_override": "profiles/example.mobileprovision", "display_name": "Example+",
            "keep_bundle_id": false, "strip_extensions": true, "strip_supported_devices": false,
            "compression_level": 9, "bundle_id": "com.example.other", "store_country": "gb",
            "account": "free",
        ]
        let cli = try fakeCLI(quick: F.planLine([F.app("example")]))
        let (store, _) = try makeStore(cli)
        let was = F.appRecord("example")

        for (key, value) in changes.sorted(by: { $0.key < $1.key }) {
            await store.configChanged(from: [was], to: [F.appRecord("example", with: [key: value])])
            XCTAssertEqual(cli.argv().last, "--json --update example --plan --quick", "\(key) changed")
        }
        XCTAssertEqual(cli.argv().count, changes.count)
    }

    /// Several apps changed by one reload (a change outside the app, read
    /// when the list was read again): one check, naming each of them, and
    /// none of the apps that didn't change.
    func test_several_apps_changed_at_once_are_one_check() async throws {
        let cli = try fakeCLI(quick: F.planLine([F.app("bravo"), F.app("example")]))
        let (store, _) = try makeStore(cli)
        var after = before
        after[0] = F.appRecord("bravo", with: ["compression_level": 9])
        after[2] = F.appRecord("example", with: ["preferred_version": "latest"])

        await store.configChanged(from: before, to: after)

        XCTAssertEqual(cli.argv(), ["--json --update bravo example --plan --quick"])
    }

    /// A reload that changes nothing about an app reads no plan: after a
    /// build, a delivery state refresh, a hold that was already there. Nor
    /// does one that changes only what isn't config: whether its profile
    /// needs Developer Mode, which a build's own profile changes, and the
    /// build's end reads that app's plan already.
    func test_a_reload_that_changes_no_apps_config_reads_no_plan() async throws {
        let cli = try fakeCLI(quick: F.planLine([F.app("example")]))
        let (store, _) = try makeStore(cli)

        await store.configChanged(from: before, to: before)
        var after = before
        after[2] = F.appRecord("example", with: ["preferred_version": "1.2.0", "needs_developer_mode": true])
        await store.configChanged(from: before, to: after)

        XCTAssertEqual(cli.argv(), [])
    }

    /// An app added is checked; an app removed drops what was found for it,
    /// so a later app of the same name doesn't show it.
    func test_an_app_added_is_checked_and_one_removed_drops_its_result() async throws {
        // Every app for the daily check; the app named for a recheck.
        let cli = try ScriptCLI("""
        case "$*" in
        *--all*)
        cat <<'EOF'
        \(F.planLine([F.app("bravo"), F.app("charlie"), F.app("example")]))
        EOF
        ;;
        *)
        cat <<'EOF'
        \(F.planLine([F.app("delta")]))
        EOF
        ;;
        esac
        """, test: self)
        let (store, _) = try makeStore(cli)
        await store.checkIfDue()
        XCTAssertNotNil(store.plans["charlie"], "premise")

        await store.configChanged(from: before, to: [before[0], F.appRecord("delta"), before[2]])

        XCTAssertEqual(cli.argv(), ["--json --update --all --plan --quick", "--json --update delta --plan --quick"])
        XCTAssertNotNil(store.plans["delta"])
        XCTAssertNil(store.plans["charlie"])
        XCTAssertNotNil(store.plans["example"], "an app that didn't change keeps its result")
    }

    /// With "Check my apps for updates" off, a change to an app's config
    /// sends nothing (spec §11.4): only a check by hand does.
    func test_with_the_switch_off_a_change_to_an_apps_config_reads_no_plan() async throws {
        let cli = try fakeCLI(quick: F.planLine([F.app("example", to: "2.0")]))
        let defaults = try throwawayDefaults()
        defaults.set(false, forKey: AppUpdatesStore.Key.check)
        let (store, _) = try makeStore(cli, defaults: defaults)
        await store.checkNow()
        let found = store.plans
        var after = before
        after[2] = F.appRecord("example", with: ["preferred_version": "latest"])

        await store.configChanged(from: before, to: after)

        XCTAssertEqual(cli.argv(), ["--json --update --all --plan --quick"], "nothing but the check by hand")
        XCTAssertEqual(store.plans, found)
    }

    // MARK: Stop updating on a device (spec §11.3, devices that are gone)

    /// Stop updating drops that device for that app, then reads that app's
    /// plan again.
    func test_stop_updating_drops_that_device_then_plans_that_app_again() async throws {
        let cli = try fakeCLI(quick: F.planLine([F.app("example")]))
        let (store, _) = try makeStore(cli)
        await store.checkIfDue()

        await store.dropTarget(app: "example", udid: F.pad)

        XCTAssertEqual(cli.argv(), [
            "--json --update --all --plan --quick",
            "--json \(CLIContract.Flag.dropUpdateTarget) example --device \(F.pad)",
            "--json --update example --plan --quick",
        ])
        XCTAssertNil(store.dropFailure)
        XCTAssertNil(store.dropping)
    }

    /// A refusal ("example isn't on that device") is said, and nothing is
    /// planned again.
    func test_a_refused_stop_updating_says_why() async throws {
        let cli = try fakeCLI(quick: F.planLine([F.app("example")]), runExit: 2,
                              runFirst: "echo \"error: example isn't on that device\" >&2")
        let (store, _) = try makeStore(cli)
        await store.checkIfDue()

        await store.dropTarget(app: "example", udid: F.pad)

        XCTAssertEqual(store.dropFailure?.app, "example")
        XCTAssertTrue(store.dropFailure?.message.contains("isn't on that device") == true,
                      store.dropFailure?.message ?? "nil")
        XCTAssertEqual(cli.argv().count, 2, "no plan after a refusal")
    }

    /// Not while an update runs: its plan is already made.
    func test_stop_updating_waits_for_no_update() async throws {
        let go = temporaryURL("drop-go")
        let cli = try fakeCLI(run: F.end(ok: true, updated: ["example"]),
                              runFirst: "case \"$*\" in *--update*) while [ ! -e '\(go.path)' ]; do /bin/sleep 0.05; done ;; esac")
        let (store, _) = try makeStore(cli)
        store.start(apps: ["example"], withAffected: false, plan: nil)

        await store.dropTarget(app: "example", udid: F.pad)

        XCTAssertFalse(cli.argv().contains { $0.contains(CLIContract.Flag.dropUpdateTarget) })
        FileManager.default.createFile(atPath: go.path, contents: nil)
        await eventually { store.lastFinished != nil }
    }

    /// From the confirmation, Stop updating reads its full plan again.
    func test_stop_updating_in_the_confirmation_reads_its_full_plan_again() async throws {
        let cli = try fakeCLI(full: F.planLine([F.app("example", readiness: "checked")], quick: false))
        let (store, _) = try makeStore(cli)
        store.openConfirmation(apps: ["example", "bravo"])
        await eventually { store.confirmation?.plan != nil }

        await store.dropTarget(app: "example", udid: F.pad)

        XCTAssertEqual(cli.argv(), [
            "--json --update example bravo --plan",
            "--json \(CLIContract.Flag.dropUpdateTarget) example --device \(F.pad)",
            "--json --update example bravo --plan",
        ])
        XCTAssertNotNil(store.confirmation?.plan)
        XCTAssertEqual(store.sheet, .confirm)
    }

    // MARK: The confirmation

    func test_the_confirmation_runs_the_full_plan_and_its_button_waits_for_it() async throws {
        let full = F.planLine([F.app("example", to: "2.0", readiness: "checked"),
                               F.app("bravo", blocked: "Example iPad isn't connected", readiness: "checked")],
                              quick: false)
        let cli = try fakeCLI(full: full)
        let (store, _) = try makeStore(cli)

        store.openConfirmation(apps: ["example", "bravo"])

        XCTAssertEqual(store.sheet, .confirm)
        XCTAssertEqual(store.confirmation?.isLoading, true, "the button waits for the full plan")
        await eventually { store.confirmation?.plan != nil }
        XCTAssertEqual(cli.argv(), ["--json --update example bravo --plan"])
        XCTAssertEqual(store.confirmation?.runnable, ["example"])
        XCTAssertEqual(store.plans["bravo"]?.blockedReason, "Example iPad isn't connected",
                       "the full plan is better news than the quick one")
    }

    func test_update_runs_the_ready_apps_with_affected_only_when_asked() async throws {
        let full = F.planLine([F.app("example", tweaks: [F.tweak("a.deb", also: ["bravo"])], readiness: "checked")],
                              quick: false)
        let cli = try fakeCLI(full: full, run: F.end(ok: true, updated: ["example", "bravo"]))
        let (store, _) = try makeStore(cli)
        store.openConfirmation(apps: ["example"])
        await eventually { store.confirmation?.plan != nil }
        XCTAssertEqual(store.confirmation?.alsoInstall, false, "off by default")

        store.setAlsoInstall(true)
        XCTAssertTrue(store.confirm())

        XCTAssertEqual(store.sheet, .progress)
        await eventually { store.progress?.end != nil }
        XCTAssertTrue(cli.argv().contains("--json --update example --with-affected"))
    }

    /// The confirmation turns into the progress in place: one sheet id for
    /// both, so `.sheet(item:)` doesn't close it and open another (review S7).
    func test_the_sheet_keeps_its_id_from_the_confirmation_to_the_progress() {
        XCTAssertEqual(AppUpdatesStore.Sheet.confirm.id, AppUpdatesStore.Sheet.progress.id)
        XCTAssertNotEqual(AppUpdatesStore.Sheet.confirm, AppUpdatesStore.Sheet.progress)
    }

    func test_cancel_runs_nothing() async throws {
        let cli = try fakeCLI(full: F.planLine([F.app("example", readiness: "checked")], quick: false))
        let (store, config) = try makeStore(cli)
        store.openConfirmation(apps: ["example"])
        await eventually { store.confirmation?.plan != nil }

        store.cancelConfirmation()

        XCTAssertNil(store.sheet)
        XCTAssertNil(config.busy)
        XCTAssertEqual(cli.argv(), ["--json --update example --plan"])
    }

    func test_a_plan_that_failed_says_so_and_offers_no_update() async throws {
        let cli = try fakeCLI(fullExit: 1)
        let (store, _) = try makeStore(cli)

        store.openConfirmation(apps: ["example"])
        await eventually { store.confirmation?.failure != nil }

        XCTAssertFalse(store.confirm())
        XCTAssertNil(store.progress)
    }

    /// A full plan that failed offers Done (review part 2, #5), and Done
    /// closes the sheet rather than doing nothing.
    func test_done_after_a_failed_plan_closes_the_sheet() async throws {
        let cli = try fakeCLI(fullExit: 1)
        let (store, _) = try makeStore(cli)
        store.openConfirmation(apps: ["example"])
        await eventually { store.confirmation?.failure != nil }
        let confirmation = try XCTUnwrap(store.confirmation)
        XCTAssertEqual(AppUpdatesSheet.confirmTitle(confirmation), AppUpdateText.done, "premise: the button is Done")

        XCTAssertFalse(store.confirm())

        XCTAssertNil(store.sheet)
        XCTAssertNil(store.confirmation)
        XCTAssertEqual(cli.argv(), ["--json --update example --plan"], "nothing ran")
    }

    /// An app's own get, drop or tweak update doesn't hold the busy slot,
    /// and an update of it waits for it, as its pane's button does (review
    /// part 2, #2): the confirmation's Update says why, and nothing runs.
    func test_update_waits_for_an_apps_own_get_or_tweak_update() async throws {
        let cli = try fakeCLI(full: F.planLine([F.app("example", to: "2.0", readiness: "checked")], quick: false),
                              run: F.end(ok: true, updated: ["example"]))
        let (store, config) = try makeStore(cli)
        store.openConfirmation(apps: ["example"])
        await eventually { store.confirmation?.plan != nil }
        let getting = "Example+ is getting from the App Store — wait for it to finish"

        store.setAppWork(["example": getting])

        XCTAssertEqual(store.waitsFor(["bravo", "example"]), getting)
        XCTAssertNil(store.waitsFor(["bravo"]))
        XCTAssertFalse(store.confirm())
        XCTAssertEqual(store.confirmation?.refusal, getting)
        XCTAssertFalse(store.start(apps: ["example"], withAffected: false, plan: nil))
        XCTAssertNil(config.busy)
        XCTAssertEqual(cli.argv(), ["--json --update example --plan"], "no run")

        store.setAppWork([:])
        XCTAssertTrue(store.confirm(), "once it's done, Update runs")
        await eventually { store.lastFinished != nil }
    }

    /// What each app is doing, from the window's stores: its get or drop,
    /// and an update of a tweak it uses.
    func test_an_apps_own_work_is_its_get_its_drop_or_a_tweak_it_uses() {
        let apps = [F.appRecord("example"), F.appRecord("bravo", tweaks: ["a.deb"]),
                    F.appRecord("charlie", tweaks: ["b.deb"]), F.appRecord("delta")]
        let names = ["example": "Example+", "bravo": "Bravo+", "charlie": "Charlie+", "delta": "Delta+"]

        XCTAssertEqual(
            AppUpdatesStore.appWork(apps: apps, staging: ["example": .getting, "delta": .providing],
                                    updatingTweak: "a.deb", names: names),
            ["example": "Example+ is getting from the App Store — wait for it to finish",
             "bravo": "A tweak Bravo+ uses is updating — wait for it to finish",
             "delta": "Delta+ is staging an IPA — wait for it to finish"]
        )
        XCTAssertEqual(AppUpdatesStore.appWork(apps: apps, staging: [:], updatingTweak: nil, names: names), [:])
    }

    // MARK: The run

    func test_an_update_holds_the_slot_and_the_quit_until_it_ends_then_reads_the_plan_again() async throws {
        let go = temporaryURL("update-go")
        let cli = try fakeCLI(run: F.end(ok: true, updated: ["example"]),
                              runFirst: "while [ ! -e '\(go.path)' ]; do /bin/sleep 0.05; done")
        let (store, config) = try makeStore(cli)

        XCTAssertTrue(store.start(apps: ["example"], withAffected: false, plan: nil))

        XCTAssertEqual(config.busy, .updating(["example"]))
        XCTAssertEqual(hold.reasons, [TerminationHold.updatingApps], "held from the start, with the slot")
        XCTAssertFalse(store.start(apps: ["bravo"], withAffected: false, plan: nil), "one at a time")
        FileManager.default.createFile(atPath: go.path, contents: nil)
        await eventually { config.busy == nil && store.lastFinished != nil }
        XCTAssertEqual(hold.reasons, [])
        XCTAssertEqual(store.progress?.end?.ok, true)
        XCTAssertEqual(store.lastFinished?.apps, ["example"])
        await eventually { cli.argv().contains("--json --update --all --plan --quick") }
    }

    func test_the_rows_follow_the_run_and_end_with_each_apps_result() async throws {
        let lines = [
            F.planLine([F.app("example"), F.app("bravo")], quick: false),
            F.step(nil, "tweaks", "skipped", "no tweak updates"),
            F.step("example", "get", "skipped", "nothing newer"),
            F.step("example", "build", "start", "Example iPhone"),
            #"{"type":"stage","name":"lc-push","status":"start","transport":"usb"}"#,
            F.step("example", "build", "end"),
            F.appEnd("example", ok: true, done: ["build"], targets: [F.targetEnd(ok: true)]),
            F.step("bravo", "get", "start", "5.1"),
            F.step("bravo", "get", "end", "received error: something went wrong"),
            F.appEnd("bravo", ok: false, left: ["get", "build"], error: "received error: something went wrong"),
            F.end(ok: false, updated: ["example"], failed: ["bravo"]),
        ].joined(separator: "\n")
        let cli = try fakeCLI(run: lines, runExit: 1)
        let (store, _) = try makeStore(cli)
        let plan = F.record([F.app("example", displayName: "Example+"), F.app("bravo")])

        store.start(apps: ["example", "bravo"], withAffected: false, plan: plan)
        await eventually {
            store.progress?.end != nil && store.progress?.rows.allSatisfy { $0.result != nil } == true
        }

        let rows = try XCTUnwrap(store.progress?.rows)
        XCTAssertEqual(rows.map(\.phase), [.done, .failed])
        guard rows.count == 2 else { return }
        XCTAssertEqual(rows[0].title, "Example+")
        let phone = try XCTUnwrap(rows[0].targets.first)
        XCTAssertEqual(AppUpdateText.targetResultLine(try XCTUnwrap(phone.result), deviceName: phone.deviceName,
                                                      liveContainer: phone.liveContainer),
                       "Open LiveContainer on Example iPhone to import it")
        XCTAssertEqual(store.progress?.unfinished, ["bravo"])
        await eventually({ store.log.lines.contains { $0.text.contains("→ example: build") } }, "the live log")
    }

    /// The live log is its own object (`UpdateLog`), published in batches:
    /// a streamed line redraws only the sheet's log panel, never the views
    /// that observe the store (the sidebar, each pane's update line).
    func test_the_live_log_is_batched_apart_from_the_store() async throws {
        let lines = (1...50).map { #"{"type":"log","source":"cyan","line":"line \#($0)"}"# }
            + [F.end(ok: true, updated: ["example"])]
        let cli = try fakeCLI(run: lines.joined(separator: "\n"))
        let (store, _) = try makeStore(cli)
        var storeChanges = 0
        var logChanges = 0
        let watching = [store.objectWillChange.sink { storeChanges += 1 },
                        store.log.objectWillChange.sink { logChanges += 1 }]

        store.start(apps: ["example"], withAffected: false, plan: nil)
        await eventually { store.lastFinished != nil }

        XCTAssertEqual(store.log.lines.filter { $0.text.contains("[cyan] line") }.count, 50, "every line, flushed at the end")
        XCTAssertLessThan(storeChanges, 25, "a log line doesn't change the store")
        XCTAssertLessThan(logChanges, 10, "the lines arrive in batches")
        withExtendedLifetime(watching) {}
    }

    func test_stop_waits_for_the_install_when_the_cli_says_it_is_finishing() async throws {
        let dir = temporaryURL("update-stop")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try (#"{"type":"update-stopping","app":"example","step":"install"}"# + "\n")
            .write(to: dir.appending(path: "stopping"), atomically: true, encoding: .utf8)
        try (F.appEnd("example", ok: true, done: ["build"]) + "\n" + F.end(ok: false, stopped: true, updated: ["example"]) + "\n")
            .write(to: dir.appending(path: "end"), atomically: true, encoding: .utf8)
        let cli = try ScriptCLI("""
        case "$*" in *--plan*) exit 0 ;; esac
        trap "cat '\(dir.path)/stopping'" TERM
        echo '\(F.step("example", "build", "start"))'
        i=0
        while [ $i -lt 50 ]; do /bin/sleep 0.05; i=$((i+1)); done
        cat '\(dir.path)/end'
        """, test: self)
        let (store, _) = try makeStore(cli)
        store.stopGrace = 1

        store.start(apps: ["example"], withAffected: false, plan: nil)
        await eventually { store.progress?.current == "example" }
        store.stop()

        XCTAssertEqual(store.progress?.stopRequested, true)
        await eventually { store.progress?.stopping != nil }
        XCTAssertEqual(AppUpdateText.stoppingText(store.progress?.stopping), "Finishing the install, then stopping…")
        await eventually({ store.progress?.end != nil }, timeout: 15, "the CLI finished the install, then stopped")
        XCTAssertEqual(store.progress?.end?.stopped, true)
        XCTAssertEqual(store.progress?.rows.first?.phase, .done)
    }

    /// Spec 4.5: Quit Anyway during a deferred install waits for that app's
    /// `update-app-end` (`QuitGuard.finishGrace`). The hold says it is
    /// finishing, in words naming the phone, from `update-stopping` until
    /// that app's `update-app-end`, while the update still holds.
    func test_quit_anyway_during_an_install_waits_for_that_apps_end() async throws {
        let dir = temporaryURL("update-quit")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try (#"{"type":"update-stopping","app":"example","step":"lc-push"}"# + "\n")
            .write(to: dir.appending(path: "stopping"), atomically: true, encoding: .utf8)
        try (F.appEnd("example", ok: true, done: ["build"]) + "\n")
            .write(to: dir.appending(path: "appEnd"), atomically: true, encoding: .utf8)
        try (F.end(ok: false, stopped: true, updated: ["example"]) + "\n")
            .write(to: dir.appending(path: "end"), atomically: true, encoding: .utf8)
        let installed = dir.appending(path: "installed")
        let exited = dir.appending(path: "exit")
        // Each wait gives up after 20 s, so a failing test leaves no stand-in.
        let cli = try ScriptCLI("""
        case "$*" in *--plan*) exit 0 ;; esac
        trap "cat '\(dir.path)/stopping'" TERM
        echo '\(F.step("example", "build", "start"))'
        i=0
        while [ ! -e '\(installed.path)' ] && [ $i -lt 400 ]; do /bin/sleep 0.05; i=$((i+1)); done
        cat '\(dir.path)/appEnd'
        i=0
        while [ ! -e '\(exited.path)' ] && [ $i -lt 400 ]; do /bin/sleep 0.05; i=$((i+1)); done
        cat '\(dir.path)/end'
        """, test: self)
        let (store, _) = try makeStore(cli)
        store.stopGrace = 1
        let plan = F.record([F.app("example", device: "Example iPad")])

        store.start(apps: ["example"], withAffected: false, plan: plan)
        await eventually { store.progress?.current == "example" }
        XCTAssertEqual(hold.finishingNotes, [], "nothing deferred yet")

        hold.stopAll()  // Quit Anyway's stop, which is the update's own

        await eventually { self.hold.finishingNotes == ["Finishing the install on Example iPad, then quitting…"] }
        FileManager.default.createFile(atPath: installed.path, contents: nil)
        await eventually { store.progress?.rows.first?.result != nil }
        XCTAssertEqual(hold.finishingNotes, [], "the install is done: that app's update-app-end")
        XCTAssertEqual(hold.reasons, [TerminationHold.updatingApps], "while the update still holds")

        FileManager.default.createFile(atPath: exited.path, contents: nil)
        await eventually { store.lastFinished != nil }
        XCTAssertEqual(hold.reasons, [])
        XCTAssertEqual(store.progress?.end?.stopped, true)
    }

    func test_try_again_confirms_the_apps_that_did_not_finish() async throws {
        let run = [F.appEnd("example", ok: true),
                   F.appEnd("bravo", ok: false, left: ["build"], error: "cyan exited with code 1"),
                   F.end(ok: false, updated: ["example"], failed: ["bravo"])].joined(separator: "\n")
        let cli = try fakeCLI(full: F.planLine([F.app("bravo", readiness: "checked")], quick: false),
                              run: run, runExit: 1)
        let (store, _) = try makeStore(cli)
        store.start(apps: ["example", "bravo"], withAffected: false, plan: nil)
        await eventually { store.lastFinished != nil }

        store.tryAgain()

        XCTAssertEqual(store.sheet, .confirm, "Update always confirms")
        XCTAssertEqual(store.confirmation?.apps, ["bravo"])
        await eventually { store.confirmation?.plan != nil }
        XCTAssertTrue(cli.argv().contains("--json --update bravo --plan"))
    }

    func test_a_run_that_ends_without_a_summary_says_why() async throws {
        let cli = try fakeCLI(runExit: 1, runFirst: "echo 'error: the delivery log is locked' >&2")
        let (store, _) = try makeStore(cli)

        store.start(apps: ["example"], withAffected: false, plan: nil)
        await eventually { store.lastFinished != nil }

        XCTAssertEqual(store.progress?.isRunning, false)
        XCTAssertTrue(store.progress?.failure?.contains("the delivery log is locked") == true)
        XCTAssertEqual(store.progress?.unfinished, ["example"])
    }

    /// A run that ends with no word on a row (review part 2, #4): a CLI
    /// that died mid-install leaves that app "Didn't finish", not
    /// "Installing", and the app after it "Not started", not "Waiting".
    func test_rows_a_dead_run_left_mid_step_settle_when_it_ends() async throws {
        let cli = try fakeCLI(runExit: 1, runFirst: """
        echo '\(F.step("example", "build", "start"))'
        echo '\(F.step("example", "install", "start", udid: F.phone, device: "Example iPhone"))'
        echo 'error: the phone went away' >&2
        """)
        let (store, _) = try makeStore(cli)
        let plan = F.record([F.app("example"), F.app("bravo")])

        store.start(apps: ["example", "bravo"], withAffected: false, plan: plan)
        await eventually { store.lastFinished != nil }

        XCTAssertEqual(store.progress?.rows.map(\.phase), [.unfinished, .notStarted])
        XCTAssertEqual(store.progress?.rows.first?.targets.map(\.phase), [.unfinished])
        XCTAssertEqual(AppUpdateText.phaseText(.unfinished), "Didn't finish")
        XCTAssertEqual(store.progress?.rows.first?.targets.first?.canTryAgain, true)
    }

    /// After a Stop that cut a step off with no word on it, the row reads
    /// "Stopped", and the sheet no longer says it is stopping.
    func test_rows_a_stopped_run_left_mid_step_read_stopped_and_the_stop_line_goes() async throws {
        let cli = try ScriptCLI("""
        case "$*" in *--plan*) exit 0 ;; esac
        trap "exit 143" TERM
        echo '\(F.step("example", "build", "start"))'
        i=0
        while [ $i -lt 100 ]; do /bin/sleep 0.05; i=$((i+1)); done
        """, test: self)
        let (store, _) = try makeStore(cli)
        store.start(apps: ["example"], withAffected: false, plan: nil)
        await eventually { store.progress?.current == "example" }
        XCTAssertEqual(store.progress?.rows.first?.phase, .building, "premise")

        store.stop()
        await eventually { store.lastFinished != nil }

        XCTAssertEqual(store.progress?.rows.first?.phase, .stopped)
        let progress = try XCTUnwrap(store.progress)
        XCTAssertNil(AppUpdatesSheet.stopLine(progress), "no orange Stopping… under Update Finished")
    }

    // MARK: A held kill (spec §11.2)

    /// A stand-in run that defers its Stop: on SIGTERM it counts the TERM
    /// and says `update-stopping` for example's install, then goes on, and
    /// says nothing more for 20 s.
    private func deferringRun() throws -> (cli: ScriptCLI, terms: URL) {
        let dir = temporaryURL("update-held")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try (#"{"type":"update-stopping","app":"example","step":"install"}"# + "\n")
            .write(to: dir.appending(path: "stopping"), atomically: true, encoding: .utf8)
        let terms = dir.appending(path: "terms")
        let cli = try ScriptCLI("""
        case "$*" in *--plan*) exit 0 ;; esac
        trap "echo x >> '\(terms.path)'; cat '\(dir.path)/stopping'" TERM
        echo '\(F.step("example", "build", "start"))'
        i=0
        while [ $i -lt 400 ]; do /bin/sleep 0.05; i=$((i+1)); done
        """, test: self)
        return (cli, terms)
    }

    func test_after_three_minutes_held_force_stop_is_offered_and_ends_the_run_at_once() async throws {
        let (cli, _) = try deferringRun()
        let (store, _) = try makeStore(cli)
        // A grace no test waits out: only Force Stop can end this run in time.
        store.stopGrace = 30
        store.forceStopTick = .milliseconds(20)
        store.start(apps: ["example"], withAffected: false, plan: nil)
        await eventually { store.progress?.current == "example" }

        store.stop()
        await eventually { store.progress?.heldSince != nil }
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(store.progress?.offersForceStop, false, "not before three minutes")

        clock.now += AppUpdatesStore.forceStopAfter
        await eventually { store.progress?.offersForceStop == true }
        XCTAssertEqual(AppUpdatesStore.forceStopAfter, 180, "spec §11.2")

        let pressed = Date()
        store.forceStop()
        await eventually { store.lastFinished != nil }
        XCTAssertLessThan(Date().timeIntervalSince(pressed), 3, "at once, not after the install")
        XCTAssertEqual(store.progress?.offersForceStop, false)
        XCTAssertNotNil(store.progress?.failure)
    }

    func test_a_second_stop_cancels_the_install_it_waited_for() async throws {
        let (cli, terms) = try deferringRun()
        let (store, _) = try makeStore(cli)
        store.stopGrace = 0.3
        store.start(apps: ["example"], withAffected: false, plan: F.record([F.app("example", device: "Example iPad")]))
        await eventually { store.progress?.current == "example" }

        store.stop()
        await eventually { store.progress?.stopping != nil && !self.hold.finishingNotes.isEmpty }
        store.stop()

        XCTAssertNil(store.progress?.stopping, "nothing is being finished now")
        XCTAssertNil(store.progress?.heldSince)
        XCTAssertEqual(hold.finishingNotes, [], "a quit no longer waits for it")
        await eventually { store.lastFinished != nil }
        let sent = (try? String(contentsOf: terms, encoding: .utf8)) ?? ""
        XCTAssertEqual(sent.split(separator: "\n").count, 2, "the second Stop reached the CLI")
    }

    /// Quit Anyway after the person's own Stop (review part 2, #1) is no
    /// second Stop: the install that Stop waits for isn't canceled, and the
    /// quit waits for it, saying so (spec 4.5).
    func test_quit_anyway_after_a_stop_waits_for_the_install_and_sends_nothing_more() async throws {
        let (cli, terms) = try deferringRun()
        let (store, _) = try makeStore(cli)
        store.stopGrace = 0.3
        store.start(apps: ["example"], withAffected: false, plan: F.record([F.app("example", device: "Example iPad")]))
        await eventually { store.progress?.current == "example" }
        store.stop()
        await eventually { store.progress?.stopping != nil && !self.hold.finishingNotes.isEmpty }

        hold.stopAll()  // Quit Anyway

        try await Task.sleep(for: .milliseconds(500))
        XCTAssertNotNil(store.progress?.stopping, "the install is still being finished")
        XCTAssertEqual(store.progress?.stoppedAgain, false)
        XCTAssertEqual(hold.finishingNotes, ["Finishing the install on Example iPad, then quitting…"],
                       "the quit waits for it")
        let sent = (try? String(contentsOf: terms, encoding: .utf8)) ?? ""
        XCTAssertEqual(sent.split(separator: "\n").count, 1, "one SIGTERM: the person's Stop's")
        store.forceStop()
        await eventually { store.lastFinished != nil }
    }

    /// A CLI that dies while it stops says why, beside "Stopped."; one that
    /// unwinds as a Stop asks (SIGTERM's 143) says only that.
    func test_a_cli_that_dies_while_stopping_shows_what_it_said() async throws {
        func stoppedRun(_ onTerm: String) async throws -> String? {
            let cli = try ScriptCLI("""
            case "$*" in *--plan*) exit 0 ;; esac
            trap "\(onTerm)" TERM
            echo '\(F.step("example", "build", "start"))'
            i=0
            while [ $i -lt 100 ]; do /bin/sleep 0.05; i=$((i+1)); done
            """, test: self)
            let (store, _) = try makeStore(cli)
            store.start(apps: ["example"], withAffected: false, plan: nil)
            await eventually { store.progress?.current == "example" }
            store.stop()
            await eventually { store.lastFinished != nil }
            return store.progress?.failure
        }

        let died = try await stoppedRun(
            "echo 'Traceback (most recent call last):' >&2; echo 'RuntimeError: the phone went away' >&2; exit 1"
        )
        XCTAssertTrue(died?.contains("RuntimeError: the phone went away") == true, died ?? "nil")
        XCTAssertTrue(died?.hasPrefix("Stopped") == true, died ?? "nil")

        let clean = try await stoppedRun("exit 143")
        XCTAssertEqual(clean, AppUpdatesStore.stopped)
    }

    // MARK: Each device's install (spec §11.3)

    /// Each device's install sits under its app: the waiting one is skipped
    /// first, the first install runs inside the build, the next after it,
    /// and `update-app-end` settles each one.
    func test_each_devices_install_follows_the_run_and_ends_with_its_own_result() async throws {
        let go = temporaryURL("update-devices")
        let old = "00000000-0000000000000004"
        let plan = F.planLine([F.app("example", to: "2.0", targets: [
            F.target("Example iPhone"), F.target("Example iPad", udid: F.pad),
            F.target("Old iPad", udid: old, waiting: F.notConnected("Old iPad")),
        ])], quick: false)
        let first = [
            plan,
            F.step("example", "install", "skipped", F.notConnected("Old iPad"), udid: old, device: "Old iPad",
                   waiting: true),
            F.step("example", "get", "start", "2.0"),
            F.step("example", "get", "end"),
            F.step("example", "build", "start", "Example iPhone"),
            F.step("example", "install", "start", udid: F.phone, device: "Example iPhone"),
            #"{"type":"stage","name":"lc-push","status":"start","transport":"usb"}"#,
            F.step("example", "install", "end", udid: F.phone, device: "Example iPhone"),
            F.step("example", "build", "end"),
            F.step("example", "install", "start", udid: F.pad, device: "Example iPad"),
        ].joined(separator: "\n")
        let rest = [
            F.step("example", "install", "end", "the install failed", udid: F.pad, device: "Example iPad"),
            F.appEnd("example", ok: false, done: ["get", "build"], left: ["install"], error: "the install failed", targets: [
                F.targetEnd(F.phone, device: "Example iPhone", ok: true),
                F.targetEnd(F.pad, device: "Example iPad", ok: false, error: "the install failed", left: ["install"]),
                F.targetEnd(old, device: "Old iPad", ok: false, left: ["install"]),
            ]),
            F.end(ok: false, failed: ["example"], waiting: 1),
        ].joined(separator: "\n")
        let cli = try ScriptCLI("""
        case "$*" in *--plan*) exit 0 ;; esac
        cat <<'EOF'
        \(first)
        EOF
        i=0
        while [ ! -e '\(go.path)' ] && [ $i -lt 200 ]; do /bin/sleep 0.05; i=$((i+1)); done
        cat <<'EOF'
        \(rest)
        EOF
        exit 1
        """, test: self)
        let (store, _) = try makeStore(cli)

        store.start(apps: ["example"], withAffected: false, plan: F.record([F.app("example", to: "2.0", targets: [
            F.target("Example iPhone"), F.target("Example iPad", udid: F.pad),
            F.target("Old iPad", udid: old, waiting: F.notConnected("Old iPad")),
        ])]))
        await eventually {
            store.progress?.rows.first?.targets.map(\.phase) == [.done, .installing, .skipped(F.notConnected("Old iPad"))]
        }
        XCTAssertEqual(store.progress?.rows.first?.phase, .installing)
        FileManager.default.createFile(atPath: go.path, contents: nil)
        await eventually { store.lastFinished != nil }

        let targets = try XCTUnwrap(store.progress?.rows.first?.targets)
        XCTAssertEqual(targets.map(\.udid), [F.phone, F.pad, old])
        XCTAssertEqual(targets.map(\.phase), [.done, .failed, .skipped(F.notConnected("Old iPad"))],
                       "the device that isn't connected stays waiting, not failed")
        XCTAssertEqual(targets.map { $0.result?.ok }, [true, false, false])
        XCTAssertEqual(store.progress?.unfinished, ["example"])
        XCTAssertEqual(store.progress?.end?.waiting, 1)
    }

    /// A device a Stop kept the install from reads "Not started".
    /// A device unplugged during the run (review part 2, #9) waits for
    /// it, as one not connected from the start does: its sentence, no Try
    /// Again, and its app done.
    func test_a_device_unplugged_during_the_run_waits_and_offers_no_try_again() async throws {
        let gone = F.notConnected("Example iPad")
        let run = [
            F.step("example", "install", "start", udid: F.phone, device: "Example iPhone"),
            F.step("example", "install", "end", udid: F.phone, device: "Example iPhone"),
            F.step("example", "install", "start", udid: F.pad, device: "Example iPad"),
            F.step("example", "install", "end", gone, udid: F.pad, device: "Example iPad", waiting: true),
            F.appEnd("example", ok: true, done: ["build"], targets: [
                F.targetEnd(F.phone, device: "Example iPhone", ok: true),
                F.targetEnd(F.pad, device: "Example iPad", ok: false, error: gone, left: ["install"], waiting: true),
            ]),
            F.end(ok: true, updated: ["example"], waiting: 1),
        ].joined(separator: "\n")
        let cli = try fakeCLI(run: run)
        let (store, _) = try makeStore(cli)

        store.start(apps: ["example"], withAffected: false, plan: F.record([F.app("example", to: "2.0", targets: [
            F.target("Example iPhone"), F.target("Example iPad", udid: F.pad),
        ])]))
        await eventually { store.lastFinished != nil }

        let row = try XCTUnwrap(store.progress?.rows.first)
        XCTAssertEqual(row.phase, .done)
        XCTAssertEqual(row.targets.map(\.phase), [.done, .skipped(gone)], "waiting, not failed")
        XCTAssertEqual(row.targets.map(\.canTryAgain), [false, false])
        XCTAssertNil(AppUpdateText.targetResultLine(try XCTUnwrap(row.targets[1].result), deviceName: "Example iPad",
                                                    liveContainer: true), "the sentence is said once")
        XCTAssertEqual(store.progress?.unfinished, [])
    }

    /// A device the build was refused for (Developer Mode off at build time,
    /// a stranger's IPA in LiveContainer's Documents) has its install
    /// skipped with the error and no `waiting`, and its result not ok: it
    /// failed, with Try Again, while the next device installs (final review
    /// part 3, #3). One skipped because it isn't connected waits.
    func test_a_device_refused_at_build_time_fails_and_one_unplugged_before_its_turn_waits() throws {
        var progress = AppUpdatesStore.Progress(rows: [])
        progress.adopt(F.record([F.app("example", to: "2.0", targets: [
            F.target("Example iPhone"), F.target("Example iPad", udid: F.pad),
            F.target("Spare iPhone", udid: F.spare),
        ])]))
        let refused = "Developer Mode is off on Example iPhone"
        let gone = F.notConnected("Spare iPhone")
        func step(_ line: String) throws -> UpdateStepRecord {
            guard case let .updateStep(record) = try BuildEvent.decode(line: line) else {
                throw XCTSkip("not an update-step line")
            }
            return record
        }
        progress.installStep(try step(F.step("example", "install", "skipped", refused, udid: F.phone,
                                             device: "Example iPhone", waiting: false)))
        progress.installStep(try step(F.step("example", "install", "skipped", gone, udid: F.spare,
                                             device: "Spare iPhone", waiting: true)))
        XCTAssertEqual(progress.rows.first?.targets.map(\.phase), [.failed, .waiting, .skipped(gone)])

        guard case let .updateAppEnd(end) = try BuildEvent.decode(line: F.appEnd(
            "example", ok: false, done: ["build"], left: ["install"], error: refused, targets: [
                F.targetEnd(F.phone, device: "Example iPhone", ok: false, error: refused, left: ["install"],
                            waiting: false),
                F.targetEnd(F.pad, device: "Example iPad", ok: true, waiting: false),
                F.targetEnd(F.spare, device: "Spare iPhone", ok: false, error: gone, left: ["install"], waiting: true),
            ])) else { return XCTFail("not an update-app-end line") }
        progress.finish(end)

        let targets = try XCTUnwrap(progress.rows.first?.targets)
        XCTAssertEqual(targets.map(\.phase), [.failed, .done, .skipped(gone)])
        XCTAssertEqual(targets.map(\.canTryAgain), [true, false, false])
        XCTAssertEqual(AppUpdateText.targetResultLine(try XCTUnwrap(targets[0].result), deviceName: "Example iPhone",
                                                      liveContainer: true),
                       "Developer Mode is off on Example iPhone. Left to do: the install.")
    }

    /// The app's end has the last word: a device the plan had waiting whose
    /// result is neither ok nor waiting, with an error, failed, with Try
    /// Again; one with no word beyond "not ok" keeps waiting.
    func test_a_result_neither_ok_nor_waiting_fails_a_device_the_plan_had_waiting() throws {
        var progress = AppUpdatesStore.Progress(rows: [])
        progress.adopt(F.record([F.app("example", to: "2.0", targets: [
            F.target("Example iPad", udid: F.pad, waiting: F.notConnected("Example iPad")),
            F.target("Spare iPhone", udid: F.spare, waiting: F.notConnected("Spare iPhone")),
        ])]))
        guard case let .updateAppEnd(end) = try BuildEvent.decode(line: F.appEnd(
            "example", ok: false, left: ["install"], error: "the install failed", targets: [
                F.targetEnd(F.pad, device: "Example iPad", ok: false, error: "the install failed", left: ["install"],
                            waiting: false),
                F.targetEnd(F.spare, device: "Spare iPhone", ok: false, left: ["install"]),
            ])) else { return XCTFail("not an update-app-end line") }

        progress.finish(end)

        XCTAssertEqual(progress.rows.first?.targets.map(\.phase),
                       [.failed, .skipped(F.notConnected("Spare iPhone"))])
    }

    /// A device's result with `waiting` reads waiting even when no install
    /// step said so first (its install never started: unplugged before its
    /// turn), with no Try Again and no line of its own.
    func test_a_devices_waiting_result_reads_waiting_without_its_step() throws {
        var progress = AppUpdatesStore.Progress(rows: [])
        progress.adopt(F.record([F.app("example", to: "2.0", targets: [
            F.target("Example iPhone"), F.target("Example iPad", udid: F.pad),
        ])]))
        let gone = F.notConnected("Example iPad")
        let end = try XCTUnwrap({ () -> UpdateAppEndRecord? in
            guard case let .updateAppEnd(record) = try? BuildEvent.decode(line: F.appEnd(
                "example", ok: true, done: ["build"], targets: [
                    F.targetEnd(F.phone, device: "Example iPhone", ok: true),
                    F.targetEnd(F.pad, device: "Example iPad", ok: false, error: gone, left: ["install"], waiting: true),
                ])) else { return nil }
            return record
        }())

        progress.finish(end)

        XCTAssertEqual(progress.rows.first?.targets.map(\.phase), [.done, .skipped(gone)])
        XCTAssertEqual(progress.rows.first?.targets.map(\.canTryAgain), [false, false])
    }

    /// An install step's end with `waiting` alone, before its app's end,
    /// already reads as waiting; one without it is a failure as before.
    func test_an_install_that_ended_waiting_reads_waiting_before_its_apps_end() {
        var progress = AppUpdatesStore.Progress(rows: [])
        progress.adopt(F.record([F.app("example", to: "2.0", targets: [
            F.target("Example iPhone"), F.target("Example iPad", udid: F.pad),
        ])]))
        let gone = F.notConnected("Example iPad")
        progress.installStep(UpdateStepRecord(app: "example", step: "install", status: "end", detail: gone,
                                              udid: F.pad, deviceName: "Example iPad", waiting: true))
        progress.installStep(UpdateStepRecord(app: "example", step: "install", status: "end", detail: "it failed",
                                              udid: F.phone, deviceName: "Example iPhone"))

        XCTAssertEqual(progress.rows.first?.targets.map(\.phase), [.failed, .skipped(gone)])
    }

    func test_a_device_a_stop_kept_from_its_install_is_not_started() async throws {
        let run = [
            F.planLine([F.app("example", targets: [F.target("Example iPhone"), F.target("Example iPad", udid: F.pad)])],
                       quick: false),
            F.step("example", "install", "start", udid: F.phone, device: "Example iPhone"),
            F.step("example", "install", "end", udid: F.phone, device: "Example iPhone"),
            F.appEnd("example", ok: false, stopped: true, done: ["build"], left: ["install"], targets: [
                F.targetEnd(F.phone, device: "Example iPhone", ok: true),
                F.targetEnd(F.pad, device: "Example iPad", ok: false, left: ["install"]),
            ]),
            F.end(ok: false, stopped: true),
        ].joined(separator: "\n")
        let (store, _) = try makeStore(try fakeCLI(run: run, runExit: 143))

        store.start(apps: ["example"], withAffected: false, plan: nil)
        await eventually { store.lastFinished != nil }

        XCTAssertEqual(store.progress?.rows.first?.targets.map(\.phase), [.done, .notStarted])
    }

    /// The quit says which device's install it waits for: the one being
    /// installed on, not the app's first (spec 4.5).
    func test_the_quit_names_the_device_whose_install_it_waits_for() async throws {
        let dir = temporaryURL("update-quit-device")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try (#"{"type":"update-stopping","app":"example","step":"install"}"# + "\n")
            .write(to: dir.appending(path: "stopping"), atomically: true, encoding: .utf8)
        let started = [
            F.step("example", "install", "start", udid: F.phone, device: "Example iPhone"),
            F.step("example", "install", "end", udid: F.phone, device: "Example iPhone"),
            F.step("example", "install", "start", udid: F.pad, device: "Example iPad"),
        ].joined(separator: "\n")
        let cli = try ScriptCLI("""
        case "$*" in *--plan*) exit 0 ;; esac
        trap "cat '\(dir.path)/stopping'" TERM
        cat <<'EOF'
        \(started)
        EOF
        i=0
        while [ $i -lt 100 ]; do /bin/sleep 0.05; i=$((i+1)); done
        """, test: self)
        let (store, _) = try makeStore(cli)
        store.stopGrace = 0.3
        store.start(apps: ["example"], withAffected: false, plan: F.record([F.app("example", targets: [
            F.target("Example iPhone"), F.target("Example iPad", udid: F.pad),
        ])]))
        await eventually { store.progress?.rows.first?.targets.last?.phase == .installing }

        hold.stopAll()

        await eventually { self.hold.finishingNotes == ["Finishing the install on Example iPad, then quitting…"] }
        await eventually({ store.lastFinished != nil }, timeout: 10)
    }

    /// A device's own Try Again confirms its app again: the CLI installs
    /// only where it's still needed.
    func test_try_again_on_one_device_confirms_its_app() async throws {
        let run = [F.appEnd("example", ok: true), F.appEnd("bravo", ok: false, left: ["install"], error: "the install failed"),
                   F.end(ok: false, updated: ["example"], failed: ["bravo"])].joined(separator: "\n")
        let cli = try fakeCLI(full: F.planLine([F.app("bravo", readiness: "checked")], quick: false), run: run, runExit: 1)
        let (store, _) = try makeStore(cli)
        store.start(apps: ["example", "bravo", "delta"], withAffected: false, plan: nil)
        await eventually { store.lastFinished != nil }

        store.tryAgain(apps: ["bravo"])

        XCTAssertEqual(store.sheet, .confirm)
        XCTAssertEqual(store.confirmation?.apps, ["bravo"], "that app alone, not every unfinished one")
    }

    // MARK: The run's own plan (review S4)

    /// `--with-affected` adds apps the confirmation didn't name. The run's
    /// first event, its own plan, names them: each gets a row with its name,
    /// phone and delivery, the busy slot covers it, and `lastFinished` names
    /// it for the window's refreshes.
    func test_the_runs_own_plan_names_the_rows_and_the_slot_with_the_affected_apps() async throws {
        let go = temporaryURL("update-affected")
        let plan = F.planLine([F.app("example", tweaks: [F.tweak("a.deb", also: ["bravo"])]),
                               F.app("bravo", displayName: "Bravo+", device: "Example iPad", delivery: "sideload")],
                              quick: false)
        let rest = [F.appEnd("example", ok: true), F.appEnd("bravo", ok: true),
                    F.end(ok: true, updated: ["example", "bravo"])].joined(separator: "\n")
        let cli = try ScriptCLI("""
        case "$*" in *--plan*) exit 0 ;; esac
        echo '\(plan)'
        i=0
        while [ ! -e '\(go.path)' ] && [ $i -lt 200 ]; do /bin/sleep 0.05; i=$((i+1)); done
        cat <<'EOF'
        \(rest)
        EOF
        """, test: self)
        let (store, config) = try makeStore(cli)

        store.start(apps: ["example"], withAffected: true, plan: F.record([F.app("example", displayName: "Example+")]))
        await eventually { store.progress?.rows.count == 2 }

        let rows = try XCTUnwrap(store.progress?.rows)
        XCTAssertEqual(rows.map(\.app), ["example", "bravo"])
        // A failure here must fail the test, not crash the run.
        guard rows.count == 2 else { return }
        XCTAssertEqual(rows[0].title, "Example+", "the confirmation's name stands")
        XCTAssertEqual(rows[1].title, "Bravo+")
        XCTAssertEqual(rows[1].targets.map(\.deviceName), ["Example iPad"])
        XCTAssertEqual(rows[1].targets.first?.liveContainer, false)
        XCTAssertEqual(config.updatingApps, ["example", "bravo"], "the slot covers them too")

        FileManager.default.createFile(atPath: go.path, contents: nil)
        await eventually { store.lastFinished != nil }
        XCTAssertEqual(store.lastFinished?.apps, ["example", "bravo"])
    }

    func test_also_install_counts_the_affected_apps_on_the_button() async throws {
        let full = F.planLine([F.app("example", tweaks: [F.tweak("a.deb", also: ["bravo", "delta"])], readiness: "checked"),
                               F.app("echo", readiness: "checked")], quick: false)
        let cli = try fakeCLI(full: full)
        let (store, _) = try makeStore(cli)
        store.openConfirmation(apps: ["example", "echo"])
        await eventually { store.confirmation?.plan != nil }

        XCTAssertEqual(store.confirmation?.count, 2)
        store.setAlsoInstall(true)
        XCTAssertEqual(store.confirmation?.count, 4)
        XCTAssertEqual(AppUpdateText.confirmButton(count: 4), "Update 4 Apps")
    }

    // MARK: Try Again and a changed plan (review S11)

    private var bravoFails: String {
        [F.appEnd("example", ok: true),
         F.appEnd("bravo", ok: false, left: ["build"], error: "cyan exited with code 1"),
         F.end(ok: false, updated: ["example"], failed: ["bravo"])].joined(separator: "\n")
    }

    /// Try Again whose full plan finds nothing left (fixed meanwhile): the
    /// confirmation says so and its button is Done, which runs nothing.
    func test_try_again_with_nothing_left_says_so_and_offers_done() async throws {
        let cli = try fakeCLI(
            full: F.planLine([F.app("bravo", install: false, readiness: "checked", nothingToDo: true)], quick: false),
            run: bravoFails, runExit: 1
        )
        let (store, _) = try makeStore(cli)
        store.start(apps: ["example", "bravo"], withAffected: false, plan: nil)
        await eventually { store.lastFinished != nil }

        store.tryAgain()
        await eventually { store.confirmation?.plan != nil }

        XCTAssertEqual(store.confirmation?.isNothingLeft, true)
        XCTAssertEqual(AppUpdateText.nothingLeft, "Nothing left to update")
        XCTAssertEqual(AppUpdateText.confirmButton(count: try XCTUnwrap(store.confirmation?.count)), "Done")
        XCTAssertFalse(store.confirm(), "Done runs nothing")
        XCTAssertNil(store.sheet, "and closes the sheet")
        XCTAssertEqual(cli.argv().filter { !$0.contains("--plan") }.count, 1, "one run only")
    }

    func test_try_again_after_every_app_finished_has_nothing_left_and_asks_nothing() async throws {
        let cli = try fakeCLI(run: [F.appEnd("example", ok: true), F.end(ok: true, updated: ["example"])]
            .joined(separator: "\n"))
        let (store, _) = try makeStore(cli)
        store.start(apps: ["example"], withAffected: false, plan: nil)
        await eventually { store.lastFinished != nil }
        let runs = cli.argv().filter { $0.contains("--plan") && !$0.contains("--quick") }.count

        store.tryAgain()

        XCTAssertEqual(store.sheet, .confirm)
        XCTAssertEqual(store.confirmation?.isNothingLeft, true)
        XCTAssertEqual(cli.argv().filter { $0.contains("--plan") && !$0.contains("--quick") }.count, runs,
                       "no plan to ask for")
    }

    /// The CLI refuses a whole plan that names an app it doesn't know: one
    /// removed since the last plan is left out, once the app list is read.
    func test_an_app_removed_since_the_last_plan_is_left_out() async throws {
        let cli = try fakeCLI(full: F.planLine([F.app("example", readiness: "checked")], quick: false),
                              list: F.appLine("example"))
        let (store, config) = try makeStore(cli)
        _ = try await config.loadApps()

        store.openConfirmation(apps: ["example", "gone"])
        await eventually { store.confirmation?.plan != nil || store.confirmation?.failure != nil }

        XCTAssertTrue(cli.argv().contains("--json --update example --plan"), "\(cli.argv())")
        XCTAssertNil(store.confirmation?.failure)
        XCTAssertEqual(store.confirmation?.apps, ["example"])
    }

    func test_try_again_keeps_also_install() async throws {
        let cli = try fakeCLI(full: F.planLine([F.app("bravo", readiness: "checked")], quick: false),
                              run: bravoFails, runExit: 1)
        let (store, _) = try makeStore(cli)
        store.start(apps: ["example", "bravo"], withAffected: true, plan: nil)
        await eventually { store.lastFinished != nil }

        store.tryAgain()

        XCTAssertEqual(store.confirmation?.apps, ["bravo"])
        XCTAssertEqual(store.confirmation?.alsoInstall, true, "as the run had it")
    }

    // MARK: The review's nits

    private final class Gate: @unchecked Sendable {
        var open = false
    }

    /// The sheet's running state is the process's: `update-end` arrives a
    /// moment before the CLI exits, and Done waits for the exit.
    func test_the_run_is_running_until_the_cli_has_exited() async throws {
        let go = temporaryURL("update-exit")
        let cli = try ScriptCLI("""
        case "$*" in *--plan*) exit 0 ;; esac
        echo '\(F.end(ok: true, updated: ["example"]))'
        i=0
        while [ ! -e '\(go.path)' ] && [ $i -lt 200 ]; do /bin/sleep 0.05; i=$((i+1)); done
        """, test: self)
        let (store, _) = try makeStore(cli)
        store.start(apps: ["example"], withAffected: false, plan: nil)
        await eventually { store.progress?.end != nil }

        XCTAssertEqual(store.progress?.isRunning, true, "the CLI is still there")
        store.closeSheet()
        XCTAssertEqual(store.sheet, .progress)

        FileManager.default.createFile(atPath: go.path, contents: nil)
        await eventually { store.progress?.isRunning == false }
        store.closeSheet()
        XCTAssertNil(store.sheet)
    }

    /// Update refused because a build took the slot meanwhile says why.
    func test_update_says_why_when_a_build_holds_the_slot() async throws {
        let cli = try fakeCLI(full: F.planLine([F.app("example", readiness: "checked")], quick: false))
        let (store, config) = try makeStore(cli)
        let gate = Gate()
        store.openConfirmation(apps: ["example"])
        await eventually { store.confirmation?.plan != nil }
        XCTAssertTrue(config.launchBuild(appName: "bravo", delivery: .unknown) {
            while !gate.open { try? await Task.sleep(nanoseconds: 10_000_000) }
            config.endBuild(appName: "bravo")
        })

        XCTAssertFalse(store.confirm())

        XCTAssertEqual(store.confirmation?.refusal, "bravo is building — wait for it to finish")
        XCTAssertEqual(store.sheet, .confirm)
        gate.open = true
        await eventually { config.busy == nil }
        XCTAssertTrue(store.confirm())
        await eventually { store.lastFinished != nil }
    }

    /// An app the run's plan blocks didn't fail: its row says why.
    func test_a_blocked_app_reads_blocked_not_failed() async throws {
        let run = [
            F.planLine([F.app("example"), F.app("bravo", blocked: "Example iPad isn't connected")], quick: false),
            F.appEnd("example", ok: true),
            F.appEnd("bravo", ok: false, left: ["build"], error: "Example iPad isn't connected"),
            F.end(ok: false, updated: ["example"], blocked: ["bravo"]),
        ].joined(separator: "\n")
        let cli = try fakeCLI(run: run, runExit: 1)
        let (store, _) = try makeStore(cli)

        store.start(apps: ["example", "bravo"], withAffected: false, plan: nil)
        await eventually { store.lastFinished != nil }

        XCTAssertEqual(store.progress?.rows.map(\.phase), [.done, .blocked("Example iPad isn't connected")])
    }

    /// The shared tweak step shows on the rows whose tweaks it updates.
    func test_the_shared_tweak_step_shows_on_the_rows_it_updates() async throws {
        let go = temporaryURL("update-tweaks")
        let plan = F.planLine([F.app("example", tweaks: [F.tweak("a.deb")]), F.app("bravo")], quick: false)
        let cli = try ScriptCLI("""
        case "$*" in *--plan*) exit 0 ;; esac
        echo '\(plan)'
        echo '\(F.step(nil, "tweaks", "start"))'
        i=0
        while [ ! -e '\(go.path)' ] && [ $i -lt 200 ]; do /bin/sleep 0.05; i=$((i+1)); done
        echo '\(F.step(nil, "tweaks", "end"))'
        echo '\(F.end(ok: true, updated: ["example", "bravo"]))'
        """, test: self)
        let (store, _) = try makeStore(cli)
        store.start(apps: ["example", "bravo"], withAffected: false, plan: nil)

        await eventually { store.progress?.rows.map(\.phase) == [.updatingTweaks, .waiting] }
        FileManager.default.createFile(atPath: go.path, contents: nil)
        await eventually { store.lastFinished != nil }
        // Done with the tweaks: back to waiting, then, as the run ended with
        // no word on either app, not started (review part 2, #4). A row the
        // tweak step had left updating would read "Didn't finish".
        XCTAssertEqual(store.progress?.rows.map(\.phase), [.notStarted, .notStarted])
    }

    // MARK: The window

    func test_after_an_update_the_window_reads_again_what_it_changed() async throws {
        let cli = try fakeCLI()
        let config = ConfigStore(runner: cli.runner, notify: { _ in }, hold: hold)
        let builds = BuildsStore(runner: cli.runner)
        let artwork = ArtworkStore(runner: cli.runner)
        let appStore = AppStoreStore(runner: cli.runner, shell: NoAdminShell())
        let tweaks = TweaksStore(runner: cli.runner)
        let staged = StagedIPAStore(runner: cli.runner)
        // A device page each was opened for this session: the update may have
        // installed on either (review 23-2, item 7). UDID-2 is unplugged now.
        let delivery = DeviceDeliveryStore(runner: cli.runner)
        let liveContainer = LiveContainerStore(runner: cli.runner, hold: hold)
        for udid in ["UDID-1", "UDID-2"] {
            await delivery.load(udid: udid)
            await liveContainer.load(udid: udid)
        }
        let before = cli.argv().count

        await ContentView.afterUpdate(["example"], config: config, builds: builds, artwork: artwork,
                                      appStore: appStore, tweaks: tweaks, staged: staged,
                                      deviceDelivery: delivery, liveContainer: liveContainer,
                                      connected: ["UDID-1"])

        let argv = Array(cli.argv().dropFirst(before))
        XCTAssertTrue(argv.contains { $0.hasPrefix(CLIContract.Flag.json + " " + CLIContract.Flag.list) },
                      "the list, with its delivery states: \(argv)")
        XCTAssertTrue(argv.contains { $0.contains(CLIContract.Flag.builds) }, "the Builds list")
        XCTAssertTrue(argv.contains { $0.contains(CLIContract.Flag.artwork) }, "artwork")
        XCTAssertTrue(argv.contains { $0.contains(CLIContract.Flag.tweaks) }, "the tweak library")
        XCTAssertTrue(argv.contains { $0.contains(CLIContract.Flag.appstoreCheck) && $0.contains("example") },
                      "the version line")
        XCTAssertTrue(argv.contains { $0.contains(CLIContract.Flag.stagedIPAs) }, "the Cache pane: \(argv)")
        XCTAssertEqual(argv.filter { $0.contains(CLIContract.Flag.deliveryState) },
                       ["UDID-1", "UDID-2"].map { DeviceDeliveryStore.stateArgs(udid: $0).joined(separator: " ") },
                       "each device page's delivery rows, read on this Mac: \(argv)")
        XCTAssertEqual(argv.filter { $0.contains(CLIContract.Flag.lcStatus) },
                       [LiveContainerStore.statusArgs(udid: "UDID-1").joined(separator: " ")],
                       "LiveContainer on the connected device, and not on the one that's gone: \(argv)")
    }

    func test_the_sheet_names_apps_as_the_sidebar_does() {
        XCTAssertEqual(ContentView.displayNames([F.appRecord("example", displayName: "Example+"), F.appRecord("bravo")]),
                       ["example": "Example+", "bravo": F.appRecord("bravo").shownName])
    }

    // MARK: The quit and the hang (review S10)

    /// A run that defers its Stop for example's install: on SIGTERM it says
    /// so and goes on; `install` seconds after it starts it ends that app,
    /// then hangs for `hang` seconds (ignoring SIGTERM) before it ends the run.
    private func deferringInstall(install: Double, hang: Double) throws -> ScriptCLI {
        let dir = temporaryURL("update-install")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try (#"{"type":"update-stopping","app":"example","step":"lc-push"}"# + "\n")
            .write(to: dir.appending(path: "stopping"), atomically: true, encoding: .utf8)
        try (F.appEnd("example", ok: true, done: ["build"]) + "\n")
            .write(to: dir.appending(path: "appEnd"), atomically: true, encoding: .utf8)
        try (F.end(ok: false, stopped: true, updated: ["example"]) + "\n")
            .write(to: dir.appending(path: "end"), atomically: true, encoding: .utf8)
        return try ScriptCLI("""
        case "$*" in *--plan*) exit 0 ;; esac
        trap "cat '\(dir.path)/stopping'" TERM
        echo '\(F.step("example", "build", "start"))'
        i=0
        while [ $i -lt \(Int(install / 0.05)) ]; do /bin/sleep 0.05; i=$((i+1)); done
        cat '\(dir.path)/appEnd'
        i=0
        while [ $i -lt \(Int(hang / 0.05)) ]; do /bin/sleep 0.05; i=$((i+1)); done
        cat '\(dir.path)/end'
        """, test: self)
    }

    /// Quit Anyway during an install the update is finishing: the quit waits
    /// for that app's `update-app-end`, saying where, then quits.
    func test_quit_anyway_waits_for_the_install_the_update_is_finishing() async throws {
        let cli = try deferringInstall(install: 1, hang: 0)
        let (store, _) = try makeStore(cli)
        store.stopGrace = 0.3
        store.start(apps: ["example"], withAffected: false, plan: F.record([F.app("example", device: "Example iPad")]))
        await eventually { store.progress?.current == "example" }
        final class Seen { var notices: [String?] = []; var replied = false; var endedFirst = false }
        let seen = Seen()
        let quitGuard = QuitGuard(
            hold: hold,
            present: { _, answer in
                Task { @MainActor in answer(.quitAnyway) }
                return { $0() }
            },
            reply: { _ in
                seen.replied = true
                seen.endedFirst = store.progress?.rows.first?.result != nil
            },
            schedule: { delay, block in
                Task { @MainActor in
                    try? await Task.sleep(for: .seconds(delay))
                    block()
                }
            },
            notice: { seen.notices.append($0) }
        )

        XCTAssertEqual(quitGuard.shouldTerminate(), .terminateLater)
        await eventually({ seen.replied }, timeout: 10, "Graft quits")

        XCTAssertTrue(seen.endedFirst, "only once that app's install had ended")
        XCTAssertEqual(seen.notices.first, "Finishing the install on Example iPad, then quitting…")
        XCTAssertEqual(seen.notices.last, .some(nil), "the notice came down")
    }

    /// A CLI that hangs after finishing the install a Stop waited for is
    /// killed once its kill is armed again (spec §11.2), and the row keeps
    /// what it did.
    func test_a_cli_that_hangs_after_the_install_it_finished_is_ended() async throws {
        let cli = try deferringInstall(install: 0.3, hang: 20)
        let (store, _) = try makeStore(cli)
        store.stopGrace = 0.5
        store.start(apps: ["example"], withAffected: false, plan: nil)
        await eventually { store.progress?.current == "example" }

        store.stop()
        let stopped = Date()
        await eventually({ store.lastFinished != nil }, timeout: 8, "not after the hang")

        XCTAssertLessThan(Date().timeIntervalSince(stopped), 5)
        XCTAssertEqual(store.progress?.rows.first?.phase, .done)
        XCTAssertTrue(store.progress?.failure?.hasPrefix(AppUpdatesStore.stopped) == true,
                      store.progress?.failure ?? "nil")
    }
}
