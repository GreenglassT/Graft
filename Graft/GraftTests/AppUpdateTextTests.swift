import XCTest
@testable import Graft

/// Spec 4.2–4.4's words and decisions, one state at a time.
final class AppUpdateTextTests: XCTestCase {
    private typealias F = UpdatePlanFixture

    private func delivery(_ kind: DeliveryKind) -> DeliveryStateRecord {
        DeliveryStateRecord(app: "example", kind: kind, summary: "s", reasons: [], pushedAt: nil)
    }

    // MARK: Every device the app is on (contract G)

    /// The iPad needs the install and isn't connected: the CLI says so,
    /// with `install` false (nothing installs there in this run).
    private func waitingPad() -> [String: Any] {
        F.target("Example iPad", udid: F.pad, waiting: F.notConnected("Example iPad"))
    }

    /// The iPhone is current; the iPad needs the install but isn't
    /// connected. Nothing can happen now (spec §11.3, devices that are gone),
    /// but there is something to update once it's back.
    func test_work_waiting_for_a_device_is_not_work_for_now() {
        let plan = F.plan(F.app("example", targets: [F.target("Example iPhone", install: false), waitingPad()]))

        XCTAssertFalse(plan.actionable, "premise: the CLI's word")
        XCTAssertFalse(plan.nothingToDo, "premise: waiting work counts")
        XCTAssertFalse(plan.isActionable)
        XCTAssertTrue(plan.hasUpdate, "something to update once it's back")
        XCTAssertEqual(plan.installTargets, [])
        XCTAssertEqual(plan.waitingTargets.map(\.udid), [F.pad])
    }

    /// The app's own reading holds even if the CLI's `actionable` counted a
    /// waiting device's work as work now.
    func test_a_waiting_devices_work_never_counts_as_work_now() {
        let plan = F.plan(F.app("example", targets: [F.target("Example iPhone", install: false), waitingPad()],
                                actionable: true))

        XCTAssertFalse(plan.isActionable)
    }

    func test_a_new_build_installs_on_every_ready_device_and_waits_for_the_rest() {
        let plan = F.plan(F.app("example", to: "2.0", targets: [
            F.target("Example iPhone"), waitingPad(), F.target("Spare iPhone", udid: F.spare),
        ]))

        XCTAssertTrue(plan.isActionable)
        XCTAssertEqual(plan.installTargets.map(\.udid), [F.phone, F.spare])
        XCTAssertEqual(plan.waitingTargets.map(\.udid), [F.pad], "the new build reaches it once it's back")
    }

    func test_an_install_only_offered_is_not_an_update() {
        let offered = F.app("example", install: false, installSkipped: "Re-installed an older build",
                            manualInstall: true)
        let plan = F.plan(offered)

        XCTAssertTrue(plan.manualInstall)
        XCTAssertFalse(plan.nothingToDo, "premise")
        XCTAssertFalse(plan.isActionable, "no badge, and not in Update All (spec §11.1)")
        XCTAssertFalse(plan.hasUpdate)
        var counted = offered
        counted["actionable"] = true
        XCTAssertFalse(F.plan(counted).isActionable, "even if the CLI counted it")
    }

    /// A device that is there but not ready ("Developer Mode is off…") has
    /// its update shown, and nothing runs.
    func test_a_blocked_or_never_delivered_app_is_not_actionable() {
        let blocked = F.plan(F.app("example", blocked: "Developer Mode is off on Example iPhone"))
        XCTAssertEqual(blocked.blockedReason, "Developer Mode is off on Example iPhone")
        XCTAssertFalse(blocked.install, "premise: nothing installs on a blocked device")
        XCTAssertTrue(blocked.hasUpdate, "a blocked app still has an update to show")
        XCTAssertFalse(blocked.isActionable)
        XCTAssertEqual(blocked.blockedTargets.map(\.udid), [F.phone])

        let never = F.plan(F.app("example", to: "2.0", install: false,
                                 installSkipped: CLIContract.Update.notInstalledYet))
        XCTAssertEqual(never.targets, [])
        XCTAssertFalse(never.hasUpdate, "an app never delivered is left to Build & Install")
        XCTAssertFalse(never.isActionable)
    }

    func test_a_readiness_note_never_blocks() {
        let plan = F.plan(F.app("example", readinessNote: "Developer Mode was off on Example iPhone when Graft last checked"))

        XCTAssertTrue(plan.isActionable)
    }

    // MARK: One indicator per row

    func test_the_badge_replaces_the_stale_dot_and_the_dot_stays_for_never() {
        let stale = F.plan(F.app("example"))
        let never = F.plan(F.app("example", to: "2.0", install: false,
                                 installSkipped: CLIContract.Update.notInstalledYet))
        let nothing = F.plan(F.app("example", install: false, nothingToDo: true))

        XCTAssertEqual(AppUpdateText.indicator(plan: stale, delivery: delivery(.stale)), .update)
        XCTAssertEqual(AppUpdateText.indicator(plan: never, delivery: delivery(.never)), .deliveryDot)
        XCTAssertNil(AppUpdateText.indicator(plan: nothing, delivery: delivery(.current)))
        XCTAssertEqual(AppUpdateText.indicator(plan: nil, delivery: delivery(.stale)), .deliveryDot,
                       "no plan read: today's dot")
    }

    /// One mark per app, raised only when Update can do something now (spec
    /// §11.3, devices that are gone): work that waits for a device that
    /// isn't connected, or for one that isn't ready, raises nothing, and
    /// doesn't leave the stale dot behind either.
    func test_the_mark_is_raised_only_for_work_update_can_do_now() {
        let waitingOnly = F.plan(F.app("example", targets: [
            F.target("Example iPhone", install: false),
            F.target("Example iPad", udid: F.pad, waiting: F.notConnected("Example iPad")),
        ]))
        let blockedOnly = F.plan(F.app("example", to: "2.0", blocked: "Developer Mode is off on Example iPhone"))
        let both = F.plan(F.app("example", to: "2.0", targets: [
            F.target("Example iPhone"), F.target("Example iPad", udid: F.pad, waiting: F.notConnected("Example iPad")),
        ]))

        XCTAssertNil(AppUpdateText.indicator(plan: waitingOnly, delivery: delivery(.stale)))
        XCTAssertNil(AppUpdateText.indicator(plan: blockedOnly, delivery: delivery(.stale)))
        XCTAssertEqual(AppUpdateText.indicator(plan: both, delivery: delivery(.stale)), .update)
    }

    /// The mark's tooltip says what, and on which devices.
    func test_the_marks_tooltip_names_the_devices() {
        let newBuild = F.plan(F.app("example", to: "2.0", targets: [
            F.target("Example iPhone"), F.target("Example iPad", udid: F.pad, waiting: F.notConnected("Example iPad")),
            F.target("Spare iPhone", udid: F.spare),
        ]))
        let installOnly = F.plan(F.app("example", targets: [
            F.target("Example iPhone", install: false), F.target("Example iPad", udid: F.pad),
        ]))

        XCTAssertEqual(AppUpdateText.indicatorHelp(newBuild),
                       "Update available: App Store 2.0\nInstalls on Example iPhone and Spare iPhone")
        XCTAssertEqual(AppUpdateText.indicatorHelp(installOnly),
                       "Update available: the latest build isn't on Example iPad yet")
        XCTAssertEqual(AppUpdateText.indicatorHelp(nil), AppUpdateText.badgeLabel)
    }

    // MARK: The pane's line

    func test_an_app_store_version_and_a_tweak_is_update_and_install() {
        let line = AppUpdateText.paneLine(F.plan(F.app("example", to: "2026.47.0",
                                                       tweaks: [F.tweak("a-tweak.deb", name: "A Tweak")])))

        XCTAssertEqual(line, .init(headline: "Update available: App Store 2026.47.0 · 1 tweak", notes: [],
                                   action: .button("Update & Install"), installsOn: "Installs on Example iPhone"))
    }

    func test_only_tweaks_is_update_tweaks_and_install() {
        let line = AppUpdateText.paneLine(F.plan(F.app("example", tweaks: [F.tweak("a.deb"), F.tweak("b.deb")])))

        XCTAssertEqual(line?.headline, "Update available: 2 tweaks")
        XCTAssertEqual(line?.action, .button("Update Tweaks & Install"))
    }

    func test_only_the_install_is_install_update() {
        let line = AppUpdateText.paneLine(F.plan(F.app("example")))

        XCTAssertEqual(line?.headline, "Update available: the latest build isn't on Example iPhone yet")
        XCTAssertEqual(line?.action, .button("Install Update"))
    }

    func test_on_intel_a_newer_version_shows_with_no_button_for_it() {
        let line = AppUpdateText.paneLine(F.plan(F.app("example", to: "2026.47.0",
                                                       skipped: CLIContract.Update.needsAppleSilicon,
                                                       install: false, nothingToDo: true)))

        XCTAssertEqual(line, .init(headline: nil, notes: ["App Store has 2026.47.0, needs Apple silicon"], action: nil))
    }

    /// A device block in a plan is advisory (review S3): Update stays
    /// offered, the reason is a note, and the confirmation's full plan
    /// decides.
    func test_a_device_block_is_a_note_and_update_stays_offered() {
        let line = AppUpdateText.paneLine(F.plan(F.app("example", to: "2.0",
                                                       blocked: "Developer Mode is off on Example iPhone")))

        XCTAssertEqual(line?.action, .button("Update & Install"))
        XCTAssertEqual(line?.notes, ["Developer Mode is off on Example iPhone"])
        XCTAssertEqual(line?.waiting, [])
    }

    // MARK: The pane's line, on every device (spec §11.3)

    private var waitingPadTarget: [String: Any] {
        F.target("Example iPad", udid: F.pad, waiting: F.notConnected("Example iPad"))
    }

    /// The line names the devices a new build installs on, and each device
    /// it waits for, with the CLI's sentence and its Stop updating.
    func test_the_line_names_the_devices_that_install_and_the_ones_that_wait() {
        let line = AppUpdateText.paneLine(F.plan(F.app("example", to: "2.0", targets: [
            F.target("Example iPhone"), waitingPadTarget, F.target("Spare iPhone", udid: F.spare),
        ])), title: "Example+")

        XCTAssertEqual(line, .init(
            headline: "Update available: App Store 2.0", notes: [], action: .button("Update & Install"),
            installsOn: "Installs on Example iPhone and Spare iPhone",
            waiting: [.init(udid: F.pad, deviceName: "Example iPad",
                            sentence: "Example iPad isn't connected: it'll update when it is",
                            stopTitle: "Stop updating Example+ on Example iPad")]
        ))
    }

    /// Only installs due: the headline names the devices ("Install Update:
    /// iPad mini" in the spec's words), and the iPhone that's current isn't
    /// mentioned.
    func test_only_installs_due_names_the_devices_that_need_one() {
        let line = AppUpdateText.paneLine(F.plan(F.app("example", targets: [
            F.target("Example iPhone", install: false), F.target("Example iPad", udid: F.pad),
        ])))

        XCTAssertEqual(line?.headline, "Update available: the latest build isn't on Example iPad yet")
        XCTAssertNil(line?.installsOn, "the headline names it")
        XCTAssertEqual(line?.action, .button("Install Update"))
    }

    /// Only a device that isn't connected needs it: the line says so, with
    /// Stop updating, and has no button (nothing could run).
    func test_work_waiting_for_a_device_has_its_sentence_and_no_button() {
        let line = AppUpdateText.paneLine(F.plan(F.app("example", targets: [
            F.target("Example iPhone", install: false), waitingPadTarget,
        ])))

        XCTAssertEqual(line?.headline, "Update available: the latest build isn't on Example iPad yet")
        XCTAssertEqual(line?.waiting.map(\.udid), [F.pad])
        XCTAssertEqual(line?.waiting.first?.stopTitle, "Stop updating example on Example iPad", "the app's name without a title")
        XCTAssertNil(line?.action)
    }

    func test_each_devices_readiness_note_is_shown() {
        let note = "Developer Mode was off on Example iPad when Graft last checked"
        let line = AppUpdateText.paneLine(F.plan(F.app("example", targets: [
            F.target("Example iPhone"), F.target("Example iPad", udid: F.pad, readinessNote: note),
        ])))

        XCTAssertEqual(line?.notes, [note])
    }

    func test_stop_updating_says_which_app_and_device() {
        XCTAssertEqual(AppUpdateText.stopUpdatingTitle(app: "Example+", device: "Example iPad"),
                       "Stop updating Example+ on Example iPad")
        XCTAssertEqual(AppUpdateText.stopUpdatingTitle(app: "Example+", device: nil),
                       "Stop updating Example+ on this device")
    }

    func test_a_refused_version_is_said_and_the_tweaks_still_offered() {
        let reason = "\(CLIContract.Update.refusedPrefix)2.0; get it in the App Store"
        let line = AppUpdateText.paneLine(F.plan(F.app("example", to: "2.0", skipped: reason,
                                                       tweaks: [F.tweak("a.deb")])))

        XCTAssertEqual(line?.notes, [reason])
        XCTAssertEqual(line?.action, .button("Update Tweaks & Install"))
    }

    /// A deliberate Re-install of an older build, or an install the device
    /// lost (spec §11.1): no badge, and its own line with an Install Update
    /// that is Build & Install, pressed on purpose.
    func test_an_offered_install_has_its_own_line_and_no_badge() {
        let plan = F.plan(F.app("example", install: false, installSkipped: "Re-installed an older build",
                                manualInstall: true))

        XCTAssertEqual(AppUpdateText.paneLine(plan),
                       .init(headline: "Re-installed an older build", notes: [], action: nil,
                             offered: [.init(udid: F.phone, deviceName: "Example iPhone", title: "Install Update",
                                             help: AppUpdateText.offeredHelp(device: "Example iPhone"))]))
        XCTAssertNil(AppUpdateText.indicator(plan: plan, delivery: delivery(.current)), "no daily badge")
        XCTAssertEqual(AppUpdateText.paneLine(F.plan(F.app(
            "example", install: false, installSkipped: "the last install couldn't be found on the device",
            manualInstall: true)))?.headline, "The last install couldn't be found on the device")
    }

    /// With several devices, the offered install says which one it's about,
    /// and its button installs there: on that device, not the bar's.
    func test_an_offered_install_on_one_of_several_devices_names_it_and_installs_there() {
        let plan = F.plan(F.app("example", targets: [
            F.target("Example iPhone", install: false),
            F.target("Example iPad", udid: F.pad, install: false, installSkipped: "Re-installed an older build",
                     manualInstall: true),
        ]))

        let line = AppUpdateText.paneLine(plan)
        XCTAssertEqual(line?.headline, "Re-installed an older build (Example iPad)")
        XCTAssertNil(line?.action, "not Update's button")
        XCTAssertEqual(line?.offered.map(\.udid), [F.pad])
        XCTAssertEqual(line?.offered.map(\.title), ["Install Update"])
    }

    /// Offered on several devices: one button each, named for its device,
    /// and each reason said once with the devices it's about.
    func test_offered_installs_on_several_devices_have_a_button_each() {
        let older = "Re-installed an older build"
        let lost = "the last install couldn't be found on the device"
        let plan = F.plan(F.app("example", targets: [
            F.target("Example iPhone", install: false, installSkipped: older, manualInstall: true),
            F.target("Example iPad", udid: F.pad, install: false, installSkipped: lost, manualInstall: true),
            F.target("Spare iPhone", udid: F.spare, install: false, installSkipped: older, manualInstall: true),
        ]))

        let line = AppUpdateText.paneLine(plan)
        XCTAssertEqual(line?.headline, "Re-installed an older build (Example iPhone and Spare iPhone)")
        XCTAssertEqual(line?.notes, ["The last install couldn't be found on the device (Example iPad)"])
        XCTAssertEqual(line?.offered.map(\.udid), [F.phone, F.pad, F.spare])
        XCTAssertEqual(line?.offered.map(\.title), ["Install Update on Example iPhone", "Install Update on Example iPad",
                                                    "Install Update on Spare iPhone"])
        XCTAssertEqual(line?.offered.map(\.help), ["Example iPhone", "Example iPad", "Spare iPhone"]
            .map { AppUpdateText.offeredHelp(device: $0) })
    }

    /// The offered install's tooltip says what it is: a Build & Install to
    /// that one device, which it picks for that build (spec §11.1), not
    /// Update's "each device it's on".
    func test_an_offered_installs_help_names_its_device_and_the_pick() {
        let help = AppUpdateText.offeredHelp(device: "Example iPad")
        XCTAssertEqual(help, "Build & Install on Example iPad: Graft picks Example iPad for that build. "
                           + "Update doesn't install it on its own.")
        XCTAssertFalse(help.contains("each device"), help)
        XCTAssertEqual(AppUpdateText.offeredHelp(device: nil),
                       "Build & Install on the device: Graft picks it for that build. "
                           + "Update doesn't install it on its own.")
    }

    /// A quick plan's remembered "Developer Mode off" is information: shown,
    /// and Update stays offered.
    func test_a_readiness_note_is_shown_and_never_blocks() {
        let note = "Developer Mode was off on Example iPad when Graft last checked"
        let plan = F.plan(F.app("example", to: "2.0", readinessNote: note))

        XCTAssertEqual(AppUpdateText.paneLine(plan),
                       .init(headline: "Update available: App Store 2.0", notes: [note], action: .button("Update & Install"),
                             installsOn: "Installs on Example iPhone"))
        XCTAssertEqual(AppUpdateText.confirmRows(F.record([F.app("example", to: "2.0", readinessNote: note)]),
                                                 names: [:]).first?.readinessNote, note)
    }

    /// A held app (review S10): its newer version is on the App Store row,
    /// not a note here; its tweaks and install are still offered.
    func test_a_held_app_offers_its_tweaks_without_a_note_for_the_hold() {
        let line = AppUpdateText.paneLine(F.plan(F.app("example", to: "2.0", skipped: "held at 1.0",
                                                       tweaks: [F.tweak("a.deb")])))

        XCTAssertEqual(line, .init(headline: "Update available: 1 tweak", notes: [],
                                   action: .button("Update Tweaks & Install"), installsOn: "Installs on Example iPhone"))
    }

    /// An `unknown` delivery (review S10) earns no mark of its own; an
    /// update still does.
    func test_an_unknown_delivery_has_no_mark_unless_there_is_an_update() {
        XCTAssertNil(AppUpdateText.indicator(plan: nil, delivery: delivery(.unknown)))
        XCTAssertEqual(AppUpdateText.indicator(plan: F.plan(F.app("example", to: "2.0")), delivery: delivery(.unknown)),
                       .update)
    }

    /// Update's tooltip says where it installs, and that the bar's pick for
    /// the next Build & Install doesn't apply (review S6).
    func test_the_update_buttons_help_says_the_bars_pick_does_not_apply() {
        XCTAssertEqual(AppUpdateText.buttonHelp(F.plan(F.app("example", device: "Example iPad"))),
                       "Installs on Example iPad via LiveContainer: each device it's on that needs it. "
                           + "The device picked for the next Build & Install doesn't apply to Update.")
        XCTAssertEqual(AppUpdateText.buttonHelp(F.plan(F.app("example", to: "2.0", targets: [
            F.target("Example iPhone"), F.target("Spare iPhone", udid: F.spare, delivery: "sideload"),
        ]))), "Installs on Example iPhone and Spare iPhone: each device it's on that needs it. "
            + "The device picked for the next Build & Install doesn't apply to Update.")
    }

    func test_nothing_to_do_and_never_delivered_show_no_line() {
        XCTAssertNil(AppUpdateText.paneLine(F.plan(F.app("example", install: false, nothingToDo: true))))
        XCTAssertNil(AppUpdateText.paneLine(F.plan(F.app("example", install: false,
                                                         installSkipped: CLIContract.Update.notInstalledYet))))
        XCTAssertNil(AppUpdateText.paneLine(nil))
    }

    // MARK: Update All

    /// Every app with an update counts, one a plan says is blocked too
    /// (review S3), in sidebar order.
    /// Update All counts the apps Update can do something for now, in
    /// sidebar order: not one whose work waits for a device that isn't
    /// connected, or isn't ready.
    func test_update_all_counts_the_apps_update_can_run_now() {
        let plans = [
            "example": F.plan(F.app("example")),
            "bravo": F.plan(F.app("bravo", to: "2.0", blocked: "Developer Mode is off on Example iPhone")),
            "charlie": F.plan(F.app("charlie", to: "2.0", waiting: F.notConnected("Example iPhone"))),
            "delta": F.plan(F.app("delta", tweaks: [F.tweak("a.deb")])),
            "foxtrot": F.plan(F.app("foxtrot", install: false, nothingToDo: true)),
        ]

        XCTAssertEqual(AppUpdateText.offered(plans, order: ["bravo", "charlie", "delta", "example", "foxtrot"]),
                       ["delta", "example"])
        XCTAssertEqual(AppUpdateText.updateAllTitle(count: 2), "Update All (2)")
    }

    // MARK: The confirmation

    func test_each_row_says_what_changes_where_it_installs_and_why_a_step_is_skipped() {
        let plan = F.record([
            F.app("example", displayName: "Example+", to: "2.0",
                  tweaks: [F.tweak("a-tweak.deb", name: "A Tweak", also: ["bravo"]),
                           F.tweak("c.deb", name: "C", to: nil, unchecked: true)],
                  device: "Example iPad"),
            F.app("delta", to: "3.0", skipped: "held at 1.0", device: "Spare iPhone", delivery: "sideload"),
            F.app("echo", blocked: "Developer Mode is off on Example iPhone"),
            F.app("foxtrot", install: false, nothingToDo: true),
        ])

        let rows = AppUpdateText.confirmRows(plan, names: ["bravo": "Bravo"])

        XCTAssertEqual(rows.map(\.app), ["example", "delta", "echo"], "nothing to do: no row")
        guard rows.count == 3 else { return }
        XCTAssertEqual(rows[0].title, "Example+")
        XCTAssertEqual(rows[0].changes, ["App Store 1.0 → 2.0", "A Tweak 1.0 → 1.1"])
        XCTAssertEqual(rows[0].targets, [.init(udid: F.phone, line: "Example+ → Example iPad via LiveContainer",
                                               status: .installs(importNote: "You'll import it in LiveContainer on Example iPad"))])
        XCTAssertEqual(rows[0].alsoUsed, ["also used by Bravo: it will need an install afterward"])
        XCTAssertEqual(rows[0].skipped, ["C: couldn't check"])
        XCTAssertEqual(rows[1].targets, [.init(udid: F.phone, line: "delta → Spare iPhone", status: .installs(importNote: nil))],
                       "a sideload needs no import")
        XCTAssertEqual(rows[1].skipped, ["App Store 3.0: held at 1.0"])
        XCTAssertEqual(rows[2].targets, [.init(udid: F.phone, line: "echo → Example iPhone via LiveContainer",
                                               status: .blocked("Developer Mode is off on Example iPhone"))],
                       "a blocked app installs nowhere: its reason says why")
        XCTAssertNil(rows[2].blocked, "said on its device, not twice")
    }

    /// A version the Apple ID was refused is said as the CLI words it, once:
    /// not "App Store 1.2.0: Your Apple ID doesn't have 1.2.0; …" (review
    /// part 1, nits).
    func test_a_refused_version_is_said_once_in_the_confirmation() {
        let refused = "\(CLIContract.Update.refusedPrefix)1.2.0; get it in the App Store"
        let rows = AppUpdateText.confirmRows(F.record([
            F.app("example", to: "1.2.0", skipped: refused, tweaks: [F.tweak("a.deb")]),
        ]), names: [:])

        XCTAssertEqual(rows.first?.skipped, [refused])
    }

    /// A row per app and device (spec §11.3): "Example+ → Example iPhone via
    /// LiveContainer", then "→ Example iPad via LiveContainer", each with
    /// its import note, its waiting sentence and Stop updating, or its
    /// block. A device that's current and has nothing coming isn't listed.
    func test_each_card_has_a_line_per_device_with_its_own_state() {
        let old = "00000000-0000000000000004"
        let plan = F.record([F.app("example", displayName: "Example+", to: "2.0", targets: [
            F.target("Example iPhone"),
            F.target("Example iPad", udid: F.pad, waiting: F.notConnected("Example iPad")),
            F.target("Spare iPhone", udid: F.spare, delivery: "sideload"),
            F.target("Old iPad", udid: old, blocked: "Developer Mode is off on Old iPad"),
        ])])

        let row = try? XCTUnwrap(AppUpdateText.confirmRows(plan, names: [:]).first)

        XCTAssertEqual(row?.targets, [
            .init(udid: F.phone, line: "Example+ → Example iPhone via LiveContainer",
                  status: .installs(importNote: "You'll import it in LiveContainer on Example iPhone")),
            .init(udid: F.pad, line: "→ Example iPad via LiveContainer",
                  status: .waiting(F.notConnected("Example iPad"), stopTitle: "Stop updating Example+ on Example iPad")),
            .init(udid: F.spare, line: "→ Spare iPhone", status: .installs(importNote: nil)),
            .init(udid: old, line: "→ Old iPad via LiveContainer", status: .blocked("Developer Mode is off on Old iPad")),
        ])
        XCTAssertEqual(row?.changes, ["App Store 1.0 → 2.0"])

        let installOnly = F.record([F.app("example", displayName: "Example+", targets: [
            F.target("Example iPhone", install: false), F.target("Example iPad", udid: F.pad),
        ])])
        XCTAssertEqual(AppUpdateText.confirmRows(installOnly, names: [:]).first?.targets.map(\.line),
                       ["Example+ → Example iPad via LiveContainer"], "the current iPhone has nothing coming")
    }

    func test_the_toggle_and_the_button_say_who_and_how_many() {
        let plan = F.record([
            F.app("example", tweaks: [F.tweak("a.deb", also: ["bravo", "delta"])]),
            F.app("echo", tweaks: [F.tweak("b.deb", also: ["delta"])]),
        ])

        XCTAssertEqual(AppUpdateText.affected(plan), ["bravo", "delta"])
        XCTAssertEqual(AppUpdateText.alsoInstallTitle(["Bravo", "Delta"]), "Also install Bravo and Delta now")
        XCTAssertEqual(AppUpdateText.confirmButton(count: 1), "Update")
        XCTAssertEqual(AppUpdateText.confirmButton(count: 3), "Update 3 Apps")
    }

    // MARK: The progress sheet

    func test_the_phases_and_the_stop_wording() {
        XCTAssertEqual(AppUpdateText.phaseText(.getting("2026.47.0")), "Getting 2026.47.0")
        XCTAssertEqual(AppUpdateText.phaseText(.installing), "Installing")
        XCTAssertEqual(AppUpdateText.phaseText(.notStarted), "Not started")
        XCTAssertEqual(AppUpdateText.phaseText(.blocked("Example iPad isn't connected")),
                       "Blocked: Example iPad isn't connected", "not Failed")
        XCTAssertEqual(AppUpdateText.stoppingText(UpdateStoppingRecord(app: "example", step: "install")),
                       "Finishing the install, then stopping…")
        XCTAssertEqual(AppUpdateText.stoppingText(UpdateStoppingRecord(app: "example", step: "lc-push")),
                       "Finishing the install, then stopping…")
        XCTAssertEqual(AppUpdateText.stoppingText(UpdateStoppingRecord(app: nil, step: "tweaks")),
                       "Finishing the tweak update, then stopping…")
        XCTAssertEqual(AppUpdateText.stoppingText(nil), "Stopping…")
    }

    /// Spec 4.5: what the quit says while it waits for a deferred step.
    func test_the_quit_says_which_install_it_waits_for() {
        XCTAssertEqual(AppUpdateText.quittingText(UpdateStoppingRecord(app: "example", step: "lc-push"),
                                                  deviceName: "Example iPad"),
                       "Finishing the install on Example iPad, then quitting…")
        XCTAssertEqual(AppUpdateText.quittingText(UpdateStoppingRecord(app: "example", step: "install"),
                                                  deviceName: nil),
                       "Finishing the install, then quitting…")
        XCTAssertEqual(AppUpdateText.quittingText(UpdateStoppingRecord(app: nil, step: "tweaks"),
                                                  deviceName: nil),
                       "Finishing the tweak update, then quitting…")
    }

    func test_each_result_says_what_is_left_or_where_to_import() {
        let ok = UpdateAppEndRecord(app: "example", ok: true, stopped: false, done: ["build"], left: [], error: nil)
        let failed = UpdateAppEndRecord(app: "example", ok: false, stopped: false, done: ["get"], left: ["build"],
                                        error: "2.0 is downloaded; the build failed: cyan exited with code 1")
        let notStarted = UpdateAppEndRecord(app: "bravo", ok: false, stopped: false, done: [], left: ["get", "build"],
                                            error: CLIContract.Update.notStarted)

        XCTAssertEqual(AppUpdateText.resultLine(ok, deviceName: "Example iPhone", liveContainer: true),
                       "Open LiveContainer on Example iPhone to import it")
        XCTAssertEqual(AppUpdateText.resultLine(ok, deviceName: nil, liveContainer: false), "Installed")
        XCTAssertEqual(AppUpdateText.resultLine(failed, deviceName: nil, liveContainer: false),
                       "2.0 is downloaded; the build failed: cyan exited with code 1. Left to do: the build and install.")
        let blocked = UpdateAppEndRecord(app: "bravo", ok: false, stopped: false, done: [], left: ["build"],
                                         error: "Example iPad isn't connected")
        XCTAssertEqual(AppUpdateText.resultLine(blocked, deviceName: nil, liveContainer: false,
                                                blocked: "Example iPad isn't connected"),
                       "Blocked: Example iPad isn't connected")
        XCTAssertEqual(AppUpdateText.resultLine(notStarted, deviceName: nil, liveContainer: false),
                       "Left to do: the App Store download and the build and install.")
        XCTAssertEqual(AppUpdateText.phase(for: notStarted), .notStarted)
        XCTAssertEqual(AppUpdateText.phase(for: failed), .failed)
        XCTAssertEqual(AppUpdateText.phase(for: ok), .done)
    }

    // MARK: The progress sheet, per device (spec §11.3)

    private func targetEnd(_ ok: Bool, error: String? = nil, left: [String] = []) -> UpdateAppEndRecord.Target {
        UpdateAppEndRecord.Target(udid: F.pad, deviceName: "Example iPad", ok: ok, error: error, left: left)
    }

    /// Each device says how its install went: where to import it, what
    /// failed, or what's left after a Stop.
    func test_each_device_says_how_its_install_went() {
        XCTAssertEqual(AppUpdateText.targetResultLine(targetEnd(true), deviceName: "Example iPad", liveContainer: true),
                       "Open LiveContainer on Example iPad to import it")
        XCTAssertEqual(AppUpdateText.targetResultLine(targetEnd(true), deviceName: "Example iPad", liveContainer: false),
                       "Installed")
        XCTAssertEqual(AppUpdateText.targetResultLine(targetEnd(false, error: "the install failed", left: ["install"]),
                                                      deviceName: "Example iPad", liveContainer: true),
                       "the install failed. Left to do: the install.", "as the CLI said it, as the app's line is")
        XCTAssertEqual(AppUpdateText.targetResultLine(targetEnd(false, left: ["install"]),
                                                      deviceName: "Example iPad", liveContainer: true),
                       "Left to do: the install.", "a Stop kept it from starting")
    }

    func test_a_devices_phase_from_its_result() {
        XCTAssertEqual(AppUpdateText.targetPhase(targetEnd(true)), .done)
        XCTAssertEqual(AppUpdateText.targetPhase(targetEnd(false, error: "the install failed")), .failed)
        XCTAssertEqual(AppUpdateText.targetPhase(targetEnd(false, left: ["install"])), .notStarted,
                       "the device a Stop kept the install from")
        XCTAssertEqual(AppUpdateText.phaseText(.skipped(F.notConnected("Example iPad"))), F.notConnected("Example iPad"))
    }

    /// With each device saying its own, the app's line says only what's the
    /// app's: a get or a build that failed, not an install a device says.
    func test_the_apps_line_says_only_what_no_device_says() {
        let installFailed = UpdateAppEndRecord(app: "example", ok: false, stopped: false, done: ["get", "build"],
                                               left: ["install"], error: "the install failed",
                                               targets: [targetEnd(false, error: "the install failed", left: ["install"])])
        let buildFailed = UpdateAppEndRecord(app: "example", ok: false, stopped: false, done: ["get"],
                                             left: ["build", "install"], error: "cyan exited with code 1",
                                             targets: [targetEnd(false, left: ["install"])])
        let ok = UpdateAppEndRecord(app: "example", ok: true, stopped: false, done: ["build"], left: [], error: nil,
                                    targets: [targetEnd(true)])

        XCTAssertNil(AppUpdateText.appResultLine(installFailed))
        XCTAssertEqual(AppUpdateText.appResultLine(buildFailed), "cyan exited with code 1. Left to do: the build and install.")
        XCTAssertNil(AppUpdateText.appResultLine(ok))
        XCTAssertEqual(AppUpdateText.tryAgainName(app: "Example+", device: "Example iPad"),
                       "Try again: Example+ on Example iPad")
    }

    // MARK: The progress sheet's log

    /// The shared tweak step has no app, so it's named once: "→ tweaks: 1
    /// tweak", not "→ tweaks: tweaks 1 tweak" (hands-on of 2.3).
    func test_the_shared_tweak_steps_line_names_the_step_once() {
        let start = UpdateStepRecord(app: nil, step: CLIContract.Update.stepTweaks,
                                     status: CLIContract.Update.statusStart, detail: "1 tweak")
        let skipped = UpdateStepRecord(app: nil, step: CLIContract.Update.stepTweaks,
                                       status: CLIContract.Update.statusSkipped, detail: "no tweak updates")

        XCTAssertEqual(AppUpdateText.logLines(start).map(\.text), ["→ tweaks: 1 tweak"])
        XCTAssertEqual(AppUpdateText.logLines(skipped).map(\.text), ["  tweaks skipped: no tweak updates"])
    }

    /// An app's own step keeps the app's name, and an install its device.
    func test_an_apps_own_step_names_the_app() {
        let build = UpdateStepRecord(app: "example", step: CLIContract.Update.stepBuild,
                                     status: CLIContract.Update.statusStart, detail: nil)
        let install = UpdateStepRecord(app: "example", step: CLIContract.Update.stepInstall,
                                       status: CLIContract.Update.statusSkipped, detail: "up to date",
                                       udid: F.pad, deviceName: "Example iPad")

        XCTAssertEqual(AppUpdateText.logLines(build).map(\.text), ["→ example: build"])
        XCTAssertEqual(AppUpdateText.logLines(install).map(\.text),
                       ["  example: install on Example iPad skipped: up to date"])
    }
}
