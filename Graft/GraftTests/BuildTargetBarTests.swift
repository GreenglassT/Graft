import XCTest
@testable import Graft

/// What the app pane's build-target bar says, as a value rather than through a
/// rendered view.
///
/// The decision is a pure function of the device list and one config field, so
/// this file exercises `BuildTargetBar.target(devices:delivery:overrideUDID:)`
/// directly.
/// Whether the resulting bar *fits* is a different property, measured where the
/// pane is hosted in a window — see `AppDetailLayoutTests`.
///
/// **Every device here is built by decoding JSON**, and that is deliberate
/// rather than incidental: `DeviceRecord` has no memberwise initializer a test
/// could call, no test in this suite constructs one directly, and going through
/// `BuildEvent.decode(line:)` keeps the fixtures honest — a typo in
/// `DeviceRecord.CodingKeys` (`is_default` is the newest of them) fails here
/// too, instead of these tests passing against a hand-built record the CLI
/// could never produce.
final class BuildTargetBarTests: XCTestCase {

    // MARK: - Fixtures

    private func device(_ json: String) throws -> DeviceRecord {
        guard case let .device(record) = try BuildEvent.decode(line: json) else {
            throw XCTSkip("fixture is not a device event")
        }
        return record
    }

    /// One `device` event, in the shape `--devices --json` emits.
    ///
    /// `isDefault` is `Bool?` rather than `Bool` so a fixture can omit the key
    /// entirely, which is what an older `graft` on PATH actually sends —
    /// see the last test.
    private static func deviceJSON(
        udid: String = "00008030-0000000000000001",
        name: String = "iPhone 15 Pro",
        model: String = "iPhone16,1",
        iosVersion: String = "26.1",
        connection: String = "Network",
        liveContainer: String? = nil,
        isDefault: Bool? = false,
        liveContainerKnown: Bool? = nil
    ) -> String {
        let lc = liveContainer.map { "\"\($0)\"" } ?? "null"
        let flag = isDefault.map { ",\"is_default\":\($0)" } ?? ""
        let known = liveContainerKnown.map { ",\"livecontainer_known\":\($0)" } ?? ""
        return #"{"type":"device","udid":"\#(udid)","name":"\#(name)","model":"\#(model)","#
            + #""ios_version":"\#(iosVersion)","connection_type":"\#(connection)","#
            + #""livecontainer_bundle_id":\#(lc)\#(flag)\#(known)}"#
    }

    // MARK: - A phone is marked

    /// The ordinary case: the bench's default phone, and a LiveContainer app.
    func test_a_marked_phone_is_the_target() throws {
        let phone = try device(Self.deviceJSON(
            liveContainer: "com.kdt.livecontainer.ABCDE12345", isDefault: true
        ))
        // Premise, not decoration: if `is_default` stopped decoding, every
        // assertion in this file would still be satisfiable by returning
        // `.noDefault` everywhere.
        XCTAssertTrue(phone.isDefault, "premise: the fixture's is_default reached the record")

        XCTAssertEqual(
            BuildTargetBar.target(devices: [phone], delivery: .livecontainer),
            .device(name: "iPhone 15 Pro", mode: "LiveContainer")
        )
    }

    /// The mode comes from the *app's* delivery setting, not from what the
    /// phone happens to support.
    ///
    /// This phone has LiveContainer installed, which is exactly the state that
    /// tempts an implementation to read `livecontainerBundleId` and answer
    /// "LiveContainer" for an app configured to sideload. `AppConfig.delivery`
    /// decides what is sent; `livecontainer_bundle_id` only says what the phone
    /// could accept.
    func test_the_mode_is_the_apps_setting_not_the_devices_capability() throws {
        let phone = try device(Self.deviceJSON(
            liveContainer: "com.kdt.livecontainer.ABCDE12345", isDefault: true
        ))
        XCTAssertNotNil(
            phone.livecontainerBundleId,
            "premise: the phone has LiveContainer, or this tests nothing"
        )

        XCTAssertEqual(
            BuildTargetBar.target(devices: [phone], delivery: .sideload),
            .device(name: "iPhone 15 Pro", mode: "Sideload"),
            "an app set to sideload must say sideload even on a LiveContainer-capable phone"
        )
    }

    // MARK: - Nothing to say

    /// Nothing plugged in. Saying nothing here would leave the pane silently
    /// implying the build has somewhere to go.
    func test_no_devices_at_all() {
        XCTAssertEqual(
            BuildTargetBar.target(devices: [], delivery: .livecontainer),
            .noDevice
        )
    }

    /// B4: Build & Install with nothing connected ended in a red failure card.
    /// It waits, in a neutral voice: nothing is wrong yet, and the bar says
    /// both ways forward.
    func test_with_no_device_build_and_install_waits_and_says_how_to_go_on() {
        let target = BuildTargetBar.target(devices: [], delivery: .livecontainer, hasLoaded: true)

        XCTAssertEqual(target, .noDevice)
        XCTAssertTrue(target.blocksBuild)
        XCTAssertFalse(target.needsAttention, "neutral, not amber")
        XCTAssertEqual(BuildTarget.noDeviceLine,
                       "Connect your iPhone or iPad to install, or turn on Skip install in Build options.")
        XCTAssertEqual(BuildTargetBar.explanation(for: .noDevice),
                       "Connect your iPhone or iPad to install. To build without installing, turn on "
                       + "Skip install in Build options.")
    }

    func test_skip_install_with_no_device_still_builds() {
        let target = BuildTargetBar.target(devices: [], delivery: .livecontainer, hasLoaded: true,
                                           notDelivering: .skipInstall)
        XCTAssertFalse(target.blocksBuild)
    }

    /// B1: what to do lived on a 10 pt icon's tooltip; the line says it.
    func test_a_device_that_couldnt_be_read_says_to_unlock_it_and_tap_trust() {
        XCTAssertEqual(BuildTarget.unreadableLine(count: 1),
                       "1 connected device couldn't be read: unlock it and tap Trust.")
        XCTAssertEqual(BuildTarget.unreadableLine(count: 2),
                       "2 connected devices couldn't be read: unlock them and tap Trust.")
        XCTAssertEqual(BuildTarget.defaultUnreadableLine(name: "iPhone 15 Pro"),
                       "iPhone 15 Pro, your default, couldn't be read: unlock it and tap Trust.")
        XCTAssertEqual(BuildTarget.overrideUnreadableLine,
                       "The device picked for Build & Install couldn't be read: unlock it and tap Trust.")
    }

    /// Two phones, neither marked — and deliberately with **different names**,
    /// so this case is falsifying rather than coincidental.
    ///
    /// An implementation that broke the tie by taking `devices.first` would
    /// answer `.device(name: "iPhone 13 Pro", …)` here and fail. With two
    /// identically-named fixtures it could not: the failure would depend on the
    /// name being wrong, and both names would be the same.
    func test_two_phones_and_neither_is_marked() throws {
        let thirteen = try device(Self.deviceJSON(
            udid: "00008030-0000000000000003", name: "iPhone 13 Pro",
            model: "iPhone14,2", iosVersion: "16.3.1", connection: "USB"
        ))
        let fifteen = try device(Self.deviceJSON())
        XCTAssertNotEqual(
            thirteen.name, fifteen.name,
            "premise: the two phones are distinguishable by name, or a first-device "
            + "implementation could pass this by accident"
        )
        XCTAssertFalse(thirteen.isDefault || fifteen.isDefault, "premise: neither is marked")

        XCTAssertEqual(
            BuildTargetBar.target(devices: [thirteen, fifteen], delivery: .livecontainer),
            .noDefault,
            "the CLI marks at most one device, so nothing marked means the config names "
            + "a phone that is not here (or names none) — not that the first one wins"
        )
    }

    /// One phone connected and unmarked, and no default remembered as absent:
    /// spec §5b's table sends the build there ("no default set, one device"),
    /// as `installer.resolve_target` does, and the bar says why.
    func test_one_unmarked_phone_is_that_phone() throws {
        let phone = try device(Self.deviceJSON())
        XCTAssertFalse(phone.isDefault, "premise: the only phone is not the default")

        XCTAssertEqual(
            BuildTargetBar.target(devices: [phone], delivery: .livecontainer),
            .fallback(name: "iPhone 15 Pro", mode: "LiveContainer", because: "no default device is set")
        )
    }

    /// An older `graft` that does not emit `is_default` at all.
    ///
    /// `DeviceRecord` decodes the field with `decodeIfPresent(...) ?? false`
    /// precisely so the phone still appears; the bar then reads it as no
    /// default set, and the table sends the build to the one phone there.
    func test_a_cli_that_omits_is_default_reports_no_default() throws {
        let phone = try device(Self.deviceJSON(isDefault: nil))
        XCTAssertFalse(
            phone.isDefault,
            "premise: an absent is_default decodes as false rather than failing the event"
        )

        XCTAssertEqual(
            BuildTargetBar.target(devices: [phone], delivery: .livecontainer),
            .fallback(name: "iPhone 15 Pro", mode: "LiveContainer", because: "no default device is set")
        )
    }

    // MARK: - Build options' Device picker

    /// The defect this parameter exists for, as a value.
    ///
    /// Two phones, the *marked* one being the one the override does not name.
    /// That is what makes the case falsifying: a resolver that kept reading
    /// `is_default` first would answer "iPhone 15 Pro" here — which is exactly
    /// what the pane did, forty points from a `device: iPhone 13 Pro` pill.
    /// `cli.py` resolves `select_device(device or cfg.default_device_udid)`, so
    /// the build really does go to the overridden phone.
    func test_a_chosen_device_beats_the_marked_default() throws {
        let fifteen = try device(Self.deviceJSON(isDefault: true))
        let thirteen = try device(Self.deviceJSON(
            udid: "00008030-0000000000000003", name: "iPhone 13 Pro",
            model: "iPhone14,2", iosVersion: "16.3.1", connection: "USB"
        ))
        XCTAssertTrue(fifteen.isDefault, "premise: the *other* phone is the marked one")
        XCTAssertFalse(thirteen.isDefault, "premise: the override names an unmarked phone")

        XCTAssertEqual(
            BuildTargetBar.target(
                devices: [fifteen, thirteen], delivery: .livecontainer,
                overrideUDID: thirteen.udid
            ),
            .thisBuild(name: "iPhone 13 Pro", mode: "LiveContainer", then: "back to iPhone 15 Pro"),
            "a pick replaces the default rather than narrowing it, for this build only"
        )
    }

    /// The override naming a phone that is not on the bench.
    ///
    /// Deliberately *not* `.noDefault`: a phone is marked here and the config
    /// is perfectly well set up, so "nothing is set as the default device"
    /// would be false as well as useless. The build carries `--device` and
    /// `installer.select_device` raises "requested device … not connected".
    func test_a_chosen_device_that_is_not_connected() throws {
        let fifteen = try device(Self.deviceJSON(isDefault: true))
        let absent = "00008030-0000000000000004"
        XCTAssertNotEqual(fifteen.udid, absent, "premise: the chosen phone is not the one here")

        XCTAssertEqual(
            BuildTargetBar.target(
                devices: [fifteen], delivery: .livecontainer, overrideUDID: absent
            ),
            .overrideMissing(udid: absent),
            "a marked phone does not rescue a build that names a different one"
        )
    }

    /// Nothing plugged in *and* an override set — the ordering case.
    ///
    /// `.noDevice` is true here too, which is why the order is a decision. The
    /// override wins because it is the half that survives the obvious remedy:
    /// plug a phone in and `.noDevice` is answered, while `--device X` keeps
    /// failing until X specifically is back.
    func test_an_override_outranks_an_empty_device_list() {
        XCTAssertEqual(
            BuildTargetBar.target(
                devices: [], delivery: .sideload, overrideUDID: "00008030-0000000000000004"
            ),
            .overrideMissing(udid: "00008030-0000000000000004")
        )
    }

    /// An empty string is not a choice.
    ///
    /// `BuildOptions` documents nil and "" as equivalent — `activeOptions`
    /// drops an empty udid, so no `--device` reaches the command line — and
    /// this has to agree, or the bar announces a missing device for a build
    /// that never names one.
    func test_an_empty_override_is_no_override() throws {
        let phone = try device(Self.deviceJSON(isDefault: true))
        XCTAssertEqual(
            BuildTargetBar.target(devices: [phone], delivery: .livecontainer, overrideUDID: ""),
            .device(name: "iPhone 15 Pro", mode: "LiveContainer")
        )
        XCTAssertEqual(
            BuildTargetBar.target(devices: [], delivery: .livecontainer, overrideUDID: ""),
            .noDevice
        )
    }

    /// **No override behaves exactly as it did before the parameter existed.**
    ///
    /// The pin for the five cases above this section: they call the
    /// two-argument form, so a resolver that honoured a nil override — by
    /// coalescing it to "" and matching against it, say — would fail *them*
    /// rather than anything here, and the failure would read as the bar
    /// breaking rather than as the override doing it. This states it directly,
    /// across all three of the old answers, with the argument passed
    /// explicitly.
    func test_no_override_is_the_same_answer_as_before() throws {
        let marked = try device(Self.deviceJSON(isDefault: true))
        let unmarked = try device(Self.deviceJSON())

        XCTAssertEqual(
            BuildTargetBar.target(
                devices: [marked], delivery: .livecontainer, overrideUDID: nil
            ),
            .device(name: "iPhone 15 Pro", mode: "LiveContainer")
        )
        XCTAssertEqual(
            BuildTargetBar.target(
                devices: [unmarked], delivery: .livecontainer, overrideUDID: nil
            ),
            .fallback(name: "iPhone 15 Pro", mode: "LiveContainer", because: "no default device is set")
        )
        XCTAssertEqual(
            BuildTargetBar.target(devices: [], delivery: .livecontainer, overrideUDID: nil),
            .noDevice
        )
    }

    /// The defaulted parameter and an explicit nil are the same call.
    ///
    /// Cheap, and it is what lets every call site that cannot have an override
    /// — `AppDetailLayoutTests`' anti-vacuity check among them — keep using the
    /// two-argument form without that being a second code path.
    func test_the_default_argument_is_a_nil_override() throws {
        let marked = try device(Self.deviceJSON(isDefault: true))
        XCTAssertEqual(
            BuildTargetBar.target(devices: [marked], delivery: .sideload),
            BuildTargetBar.target(devices: [marked], delivery: .sideload, overrideUDID: nil)
        )
    }

    // MARK: - What an empty list is worth

    private func deviceError(_ json: String) throws -> DeviceErrorRecord {
        guard case let .deviceError(record) = try BuildEvent.decode(line: json) else {
            throw XCTSkip("fixture is not a device-error event")
        }
        return record
    }

    /// One `device-error` event: a phone usbmux enumerated that the CLI could
    /// not describe. Same wording the sidebar's fixtures use.
    private static func deviceErrorJSON(
        udid: String = "00008130-001A2B3C0123456D",
        error: String = "lockdown query failed: device is passcode locked"
    ) -> String {
        #"{"type":"device-error","udid":"\#(udid)","error":"\#(error)"}"#
    }

    /// Before the first scan lands, an empty list means nothing.
    ///
    /// This is a second of every launch — a full `--devices` costs ~1s per
    /// phone — and indefinitely long whenever `ContentView`'s listing is torn
    /// down by the window losing focus mid-scan. `.noDevice` there is an
    /// answer nothing has earned.
    func test_an_empty_list_before_the_first_scan_is_not_no_device() {
        XCTAssertEqual(
            BuildTargetBar.target(devices: [], delivery: .livecontainer, hasLoaded: false),
            .scanning
        )
    }

    /// The other half of the same fact, so the distinction is pinned from both
    /// sides: the identical empty list, once something has looked.
    func test_an_empty_list_after_a_scan_is_no_device() {
        XCTAssertEqual(
            BuildTargetBar.target(devices: [], delivery: .livecontainer, hasLoaded: true),
            .noDevice,
            "hasLoaded is the whole difference between these two cases"
        )
    }

    /// A locked phone is *connected*. `DevicesStore.deviceErrors`' own doc:
    /// "'no devices connected' sends them looking for a cable when the cable is
    /// fine and the phone is locked."
    func test_a_phone_that_could_not_be_read_is_not_no_device() throws {
        let locked = try deviceError(Self.deviceErrorJSON())
        XCTAssertFalse(locked.udid.isEmpty, "premise: the fixture decoded")

        XCTAssertEqual(
            BuildTargetBar.target(devices: [], delivery: .livecontainer, deviceErrors: [locked]),
            .unreadable(count: 1)
        )
    }

    func test_unreadable_phones_are_counted() throws {
        let one = try deviceError(Self.deviceErrorJSON())
        let two = try deviceError(Self.deviceErrorJSON(
            udid: "00008030-0000000000000004", error: "connection refused"
        ))
        XCTAssertEqual(
            BuildTargetBar.target(
                devices: [], delivery: .livecontainer, deviceErrors: [one, two]
            ),
            .unreadable(count: 2)
        )
    }

    /// An unreadable phone only decides the answer when nothing is readable.
    /// With a marked phone present the destination is known, and a locked
    /// handset on the next port changes nothing about it.
    func test_a_readable_phone_outranks_an_unreadable_one() throws {
        let phone = try device(Self.deviceJSON(isDefault: true))
        let locked = try deviceError(Self.deviceErrorJSON())
        XCTAssertEqual(
            BuildTargetBar.target(
                devices: [phone], delivery: .livecontainer, deviceErrors: [locked]
            ),
            .device(name: "iPhone 15 Pro", mode: "LiveContainer")
        )
    }

    /// A rescan with the previous list still standing — `invalidate()` puts
    /// `hasLoaded` back to false without clearing `devices`. The list is stale
    /// but it is evidence, and "Looking for devices…" over a phone we can name
    /// would be coy rather than careful.
    func test_a_rescan_with_a_standing_list_still_names_the_phone() throws {
        let phone = try device(Self.deviceJSON(isDefault: true))
        XCTAssertEqual(
            BuildTargetBar.target(devices: [phone], delivery: .livecontainer, hasLoaded: false),
            .device(name: "iPhone 15 Pro", mode: "LiveContainer")
        )
    }

    // MARK: - The listing states meet the override

    /// The override branch has to make the same three-way distinction, or the
    /// first second of every launch accuses the user's own choice of being
    /// unplugged.
    func test_a_chosen_device_is_not_missing_until_something_has_looked() {
        XCTAssertEqual(
            BuildTargetBar.target(
                devices: [], delivery: .livecontainer,
                overrideUDID: "00008030-0000000000000004", hasLoaded: false
            ),
            .scanning,
            "absence is only evidence once a scan has completed"
        )
    }

    /// The chosen phone is the locked one: connected, undescribable. Not
    /// `.overrideMissing`, which would be false — it is right there.
    func test_a_chosen_device_that_is_connected_but_unreadable() throws {
        let udid = "00008130-001A2B3C0123456D"
        let locked = try deviceError(Self.deviceErrorJSON(udid: udid))
        XCTAssertEqual(
            BuildTargetBar.target(
                devices: [], delivery: .livecontainer,
                overrideUDID: udid, deviceErrors: [locked]
            ),
            .overrideUnreadable(udid: udid),
            "the answer is about the phone the user chose, so it carries that phone"
        )
    }

    /// **The case the one above could not exercise, and the defect it hid.**
    ///
    /// This used to answer `.unreadable(count:)`, whose own doc defines it as
    /// "phones are on the bus that the CLI could not describe, *and none that it
    /// could*". Nothing in the override branch checked the second half. So the
    /// fixture here — the chosen phone locked, a second locked phone beside it,
    /// and a perfectly readable marked phone on the next port — produced "3
    /// connected phones couldn't be read" for a user whose actual problem was
    /// that the one phone they picked is locked.
    ///
    /// Every part of the fixture is load-bearing, so the case is falsifying
    /// rather than decorative:
    ///
    ///  - the readable phone makes `devices` non-empty, which is the invariant
    ///    the old answer broke and which `test_a_chosen_device_that_is_
    ///    connected_but_unreadable` (empty `devices`) could never test;
    ///  - it is also `isDefault`, so a resolver that dropped the override guard
    ///    answers `.device(name: "iPhone 15 Pro", …)` and fails here;
    ///  - the *second* locked phone makes the count 2, so the old
    ///    `.unreadable(count: deviceErrors.count)` cannot coincidentally equal a
    ///    scoped-to-the-chosen-phone count of 1.
    func test_a_chosen_unreadable_device_is_not_a_verdict_on_the_whole_bench() throws {
        let chosen = "00008130-001A2B3C0123456D"
        let locked = try deviceError(Self.deviceErrorJSON(udid: chosen))
        let alsoLocked = try deviceError(Self.deviceErrorJSON(
            udid: "00008030-0000000000000004", error: "connection refused"
        ))
        let readable = try device(Self.deviceJSON(isDefault: true))
        XCTAssertNotEqual(readable.udid, chosen, "premise: the readable phone is not the choice")
        XCTAssertTrue(readable.isDefault, "premise: it is also the marked one")

        XCTAssertEqual(
            BuildTargetBar.target(
                devices: [readable], delivery: .livecontainer,
                overrideUDID: chosen, deviceErrors: [locked, alsoLocked]
            ),
            .overrideUnreadable(udid: chosen),
            "a readable phone in the list means this is not a statement about the "
            + "bench, and the number of other locked phones is not the reason for it"
        )
    }

    /// The other half of the same invariant, from the side that still owns it:
    /// with no override, an unreadable bench is still `.unreadable`, and the
    /// count is still the whole bench.
    func test_the_bench_wide_unreadable_answer_is_unchanged() throws {
        let one = try deviceError(Self.deviceErrorJSON())
        let two = try deviceError(Self.deviceErrorJSON(
            udid: "00008030-0000000000000004", error: "connection refused"
        ))
        XCTAssertEqual(
            BuildTargetBar.target(devices: [], delivery: .livecontainer,
                                  deviceErrors: [one, two]),
            .unreadable(count: 2),
            "splitting the override case out must not have moved this one"
        )
    }

    // MARK: - Builds that install nothing

    /// The bar promised "Installs to iPhone 15 Pro" for a run with Dry run
    /// ticked. A marked phone is present here on purpose: without the option
    /// this is `.device`, so the case is falsifying. It is also what makes the
    /// dry run harmless — the device it will resolve is right there — so this
    /// stays the quiet sentence.
    func test_a_build_that_installs_nothing_says_so() throws {
        let phone = try device(Self.deviceJSON(isDefault: true))
        let answer = BuildTargetBar.target(
            devices: [phone], delivery: .livecontainer, notDelivering: .dryRun
        )
        XCTAssertEqual(
            answer,
            .notDelivering(option: .dryRun,
                           device: .device(name: "iPhone 15 Pro", mode: "LiveContainer")),
            "a resolvable destination does not make an install happen"
        )
        XCTAssertFalse(
            answer.needsAttention,
            "nothing is wrong with this build: the user asked for a dry run and the "
            + "device it will resolve on the way to doing nothing is connected"
        )
    }

    /// And it outranks every device problem at once, which is the ordering
    /// decision: `--skip-install` makes the phone irrelevant — `cli.py` gates
    /// both `select_device` and the preflight's device checks on
    /// `will_install` — so a device alarm here would be a false one.
    ///
    /// The suppressed answer is carried rather than discarded, and here it is
    /// `.scanning`: nothing has loaded, and the chosen phone is not the locked
    /// one. Asserting it pins that the recursion really re-ran the resolver
    /// instead of stashing some placeholder.
    func test_not_delivering_outranks_every_device_problem() throws {
        let locked = try deviceError(Self.deviceErrorJSON())
        XCTAssertEqual(
            BuildTargetBar.target(
                devices: [], delivery: .sideload,
                overrideUDID: "00008030-0000000000000004",
                deviceErrors: [locked], hasLoaded: false,
                notDelivering: .skipInstall
            ),
            .notDelivering(option: .skipInstall, device: .scanning),
            "every other signal here is a device problem, and skip-install means "
            + "no device is consulted at all"
        )
    }

    /// **The muted reassurance for a certain failure.**
    ///
    /// `--dry-run` is not `--skip-install`. `cli.py` wraps `select_device` in
    /// `if not skip_install:` (`:4877` guarding `:4883`, `:4969` guarding
    /// `:4973`) and gates nothing on `dry_run`, and `installer.select_device`
    /// raises "no iOS devices connected" on an empty list before any dry-run
    /// consideration. So this build is already guaranteed to fail, and the bar
    /// used to answer it with a muted gray "Dry run — this build won't be sent
    /// to a device."
    func test_a_dry_run_with_nothing_connected_is_not_reassuring() {
        let answer = BuildTargetBar.target(
            devices: [], delivery: .livecontainer, notDelivering: .dryRun
        )
        XCTAssertEqual(answer, .notDelivering(option: .dryRun, device: .noDevice),
                       "the device answer the guard suppressed is carried, not dropped")
        XCTAssertTrue(
            answer.needsAttention,
            "a build that will stop in select_device must not be rendered muted"
        )
    }

    /// The same defect through the other certain failure: the phone named in
    /// Build options is not on the bench, so `select_device` raises "requested
    /// device … not connected" whatever the dry run intended to do afterward.
    func test_a_dry_run_naming_an_absent_device_is_not_reassuring() throws {
        let present = try device(Self.deviceJSON(isDefault: true))
        let absent = "00008030-0000000000000004"
        XCTAssertNotEqual(present.udid, absent, "premise: the chosen phone is not here")

        let answer = BuildTargetBar.target(
            devices: [present], delivery: .livecontainer,
            overrideUDID: absent, notDelivering: .dryRun
        )
        XCTAssertEqual(answer, .notDelivering(option: .dryRun,
                                              device: .overrideMissing(udid: absent)))
        XCTAssertTrue(answer.needsAttention,
                      "a marked phone does not rescue a dry run that names a different one")
    }

    /// **The half that must stay quiet**, and the reason the fix is a property
    /// of the option rather than of the device state.
    ///
    /// Identical bench to `test_a_dry_run_with_nothing_connected_is_not_
    /// reassuring` — nothing connected — and the opposite answer, because
    /// `--skip-install` never looks for a phone. Without this case the amber
    /// could have been implemented as "any suppressed build with no device" and
    /// nothing would have failed; that is exactly the false alarm the ordering
    /// was designed to avoid, and it would fire on every skip-install build run
    /// away from the bench.
    func test_a_skipped_install_with_nothing_connected_stays_quiet() {
        let answer = BuildTargetBar.target(
            devices: [], delivery: .livecontainer, notDelivering: .skipInstall
        )
        XCTAssertEqual(answer, .notDelivering(option: .skipInstall, device: .noDevice))
        XCTAssertFalse(
            answer.needsAttention,
            "skip-install gates select_device and the preflight's device checks alike, "
            + "so an empty bench cannot make this build fail"
        )
    }

    /// The device states a dry run survives stay quiet too — it is *certain*
    /// failure that earns the amber, not any device imperfection.
    ///
    /// A lone unmarked phone resolves: the table sends the build to it (`.fallback`).
    /// `.unreadable` very likely resolves too, because the list it walks comes
    /// from a usbmux enumeration (pymobiledevice3) with no lockdown handshake.
    /// `.scanning` is not yet an answer about anything.
    func test_a_dry_run_stays_quiet_for_device_states_it_can_survive() throws {
        let unmarked = try device(Self.deviceJSON())
        let locked = try deviceError(Self.deviceErrorJSON())
        let cases: [(BuildTarget, String)] = [
            (BuildTargetBar.target(devices: [unmarked], delivery: .livecontainer,
                                   notDelivering: .dryRun), "fallback"),
            (BuildTargetBar.target(devices: [], delivery: .livecontainer,
                                   deviceErrors: [locked], notDelivering: .dryRun),
             "unreadable"),
            (BuildTargetBar.target(devices: [], delivery: .livecontainer,
                                   hasLoaded: false, notDelivering: .dryRun), "scanning"),
        ]
        for (answer, label) in cases {
            XCTAssertFalse(
                answer.needsAttention,
                "\(label) is not a certain select_device failure, so claiming one would "
                + "be the false alarm this bar's ordering exists to avoid"
            )
        }
        XCTAssertEqual(
            cases.map(\.0),
            [
                .notDelivering(option: .dryRun, device: .fallback(
                    name: "iPhone 15 Pro", mode: "LiveContainer", because: "no default device is set"
                )),
                .notDelivering(option: .dryRun, device: .unreadable(count: 1)),
                .notDelivering(option: .dryRun, device: .scanning),
            ],
            "premise: those are the three device states the loop above measured"
        )
    }

    /// The carried answer is *the resolver's own*, not a second opinion.
    ///
    /// Stated as an identity over every device state this file builds, so a
    /// future edit that computes the suppressed answer some cheaper way — or
    /// forgets to pass one of the arguments down — fails here rather than in
    /// whichever single case happened to be asserted literally.
    func test_the_suppressed_answer_is_what_the_resolver_would_have_said() throws {
        let marked = try device(Self.deviceJSON(isDefault: true))
        let locked = try deviceError(Self.deviceErrorJSON())
        let benches: [([DeviceRecord], String?, [DeviceErrorRecord], Bool)] = [
            ([], nil, [], true),
            ([], nil, [], false),
            ([], nil, [locked], true),
            ([marked], nil, [], true),
            ([marked], "00008030-0000000000000004", [], true),
            ([], "00008130-001A2B3C0123456D", [locked], true),
        ]
        for (devices, override, errors, loaded) in benches {
            let suppressed = BuildTargetBar.target(
                devices: devices, delivery: .sideload, overrideUDID: override,
                deviceErrors: errors, hasLoaded: loaded, notDelivering: nil
            )
            for option in [BuildOptions.Option.dryRun, .skipInstall] {
                XCTAssertEqual(
                    BuildTargetBar.target(
                        devices: devices, delivery: .sideload, overrideUDID: override,
                        deviceErrors: errors, hasLoaded: loaded, notDelivering: option
                    ),
                    .notDelivering(option: option, device: suppressed),
                    "the suppressed answer must equal the one this resolver gives with "
                    + "the option absent — bench \(devices.map(\.udid)), override "
                    + "\(override ?? "nil"), loaded \(loaded)"
                )
            }
        }
    }

    /// An option that does not suppress delivery is not a "not delivering"
    /// answer, whatever it is handed to this parameter.
    ///
    /// The guard exists because the parameter is typed widely enough to accept
    /// one: `BuildOptions.Option` has five cases and only three of them stop
    /// the build reaching a phone. Without it, ticking Skip decryption check
    /// would blank the destination this bar exists to show.
    func test_an_option_that_still_delivers_is_not_a_suppression() throws {
        let phone = try device(Self.deviceJSON(isDefault: true))
        for option in [BuildOptions.Option.skipDecryptCheck,
                       .device(udid: "00008030-0000000000000001")] {
            XCTAssertNil(option.suppressesDelivery, "premise: \(option) delivers")
            XCTAssertEqual(
                BuildTargetBar.target(
                    devices: [phone], delivery: .livecontainer, notDelivering: option
                ),
                .device(name: "iPhone 15 Pro", mode: "LiveContainer"),
                "only the options that suppress delivery may take over this bar"
            )
        }
    }

    /// Why this delivery sends nothing, or nil when it sends something.
    ///
    /// `EffectiveDelivery.notDeliveringReason` was this, as a member. It was
    /// deleted with its last production caller — the bar carries the typed
    /// `BuildOptions.Option` now, because "Dry run" and "Install skipped" are
    /// one string and two different promises about the phone — and the two
    /// cases below are what wanted it. Written here rather than kept on the
    /// model, per that deletion's note in `DeliveryState.swift`.
    private func notDeliveringReason(_ delivery: EffectiveDelivery) -> String? {
        if case let .notDelivered(reason) = delivery { return reason }
        return nil
    }

    /// The wiring, from the other end: what `AppDetailView` actually passes.
    ///
    /// `deliverySuppressor` and `effectiveDelivery(for:)` are two readings of
    /// one fact and must not drift, which is the defect that put
    /// `effectiveDelivery` on `activeOptions` in the first place — the header
    /// pill said `dry-run` while the notification said `Dry run`.
    func test_the_suppressor_and_the_effective_delivery_agree() {
        let cases: [BuildOptions] = [
            BuildOptions(),
            BuildOptions(dryRun: true),
            BuildOptions(skipInstall: true),
            BuildOptions(dryRun: true, skipInstall: true),
            BuildOptions(skipDecryptCheck: true),
            BuildOptions(deviceUDID: "00008030-0000000000000001"),
        ]
        for options in cases {
            XCTAssertEqual(
                options.deliverySuppressor?.suppressesDelivery,
                notDeliveringReason(options.effectiveDelivery(for: .livecontainer)),
                "the typed answer and the prose one are the same fact, read twice"
            )
        }
        XCTAssertEqual(
            BuildOptions(dryRun: true, skipInstall: true).deliverySuppressor, .dryRun,
            "with both set the dry run is named — activeOptions' order — and it is also "
            + "the more cautious of the two, being the one that still resolves a device"
        )
    }

    /// The words are the model's, not the bar's — `BuildOptions` already
    /// decides them for the completion banner, and this is the wiring that
    /// makes the bar ask the same question rather than re-deriving it.
    func test_the_reason_is_whatever_effectiveDelivery_says() {
        XCTAssertEqual(
            notDeliveringReason(BuildOptions(dryRun: true).effectiveDelivery(for: .livecontainer)),
            "Dry run"
        )
        XCTAssertEqual(
            notDeliveringReason(BuildOptions(skipInstall: true).effectiveDelivery(for: .sideload)),
            "Install skipped"
        )
        XCTAssertNil(
            notDeliveringReason(BuildOptions().effectiveDelivery(for: .livecontainer)),
            "an ordinary build delivers, so the bar keeps naming the phone"
        )
        XCTAssertNil(
            notDeliveringReason(.unknown),
            "\"nothing said\" is not \"nothing will be sent\""
        )
    }

    /// **The defaults are a settled, delivering build.**
    ///
    /// The pin for every call above that names only two or three arguments —
    /// including the five cases this file shipped with. A resolver that treated
    /// an unspecified listing as unscanned, or an unspecified build as
    /// non-delivering, would fail *those* rather than anything here, and the
    /// failure would read as the bar breaking rather than as a default doing
    /// it.
    func test_the_defaults_are_a_settled_delivering_build() throws {
        let phone = try device(Self.deviceJSON(isDefault: true))
        XCTAssertEqual(
            BuildTargetBar.target(devices: [phone], delivery: .livecontainer),
            BuildTargetBar.target(
                devices: [phone], delivery: .livecontainer,
                overrideUDID: nil, deviceErrors: [], hasLoaded: true, notDelivering: nil
            )
        )
        XCTAssertEqual(
            BuildTargetBar.target(devices: [], delivery: .livecontainer),
            .noDevice,
            "an unspecified listing is a finished one, or every existing call site "
            + "silently becomes \"still looking\""
        )
    }

    // MARK: - The spelling

    /// The prose spelling is the one `AddAppSheet`'s picker has always shown,
    /// and there is now one definition of it. Pinned because the bar's sentence
    /// reads "…via LiveContainer": the config token (`rawValue`) is lowercase,
    /// and a regression to it would look like a typo on the pane.
    func test_the_prose_spelling_of_each_mode() {
        XCTAssertEqual(DeliveryMode.livecontainer.displayName, "LiveContainer")
        XCTAssertEqual(DeliveryMode.sideload.displayName, "Sideload")
        XCTAssertEqual(
            DeliveryMode.allCases.count, 2,
            "a third delivery mode needs a prose spelling and a line in this test"
        )
    }

    // MARK: - F10: what the tooltip claims

    /// A LiveContainer build signs nothing — LiveContainer signs the app when
    /// it is imported — and the installer no longer shells out to
    /// `idevice_id`. Every tooltip is read for both claims, because the
    /// sentences that made them were in three different cases.
    func test_no_tooltip_claims_a_signature_or_names_idevice_id() {
        let targets: [BuildTarget] = [
            .device(name: "Example Phone", mode: "LiveContainer"), .noDefault, .noDevice,
            .overrideMissing(udid: "U"), .overrideUnreadable(udid: "U"), .scanning,
            .unreadable(count: 2), .needsPaidAccount,
            .notDelivering(option: .skipInstall, device: .noDevice),
            .notDelivering(option: .dryRun, device: .noDevice),
            .notDelivering(option: .dryRun, device: .noDefault),
        ]
        for target in targets {
            let text = BuildTargetBar.explanation(for: target)
            XCTAssertFalse(text.contains("sign an IPA") || text.contains("signs an IPA"),
                           "\(target): \(text)")
            XCTAssertFalse(text.contains("idevice_id"), "\(target): \(text)")
        }
        XCTAssertTrue(BuildTargetBar.explanation(for: .noDevice).contains("Skip install"))
        XCTAssertTrue(BuildTargetBar.explanation(for: .overrideUnreadable(udid: "U"))
            .contains("asks usbmuxd"))
    }
}

// MARK: - Spec §5b's table

extension BuildTargetBarTests {
    private static let phoneUDID = "00008030-0000000000000001"
    private static let ipadUDID = "00008030-0000000000000002"

    private func remembered(_ udid: String, name: String?, isDefault: Bool = true) throws -> SeenDeviceRecord {
        let shown = name.map { "\"\($0)\"" } ?? "null"
        let line = #"{"type":"device-seen","udid":"\#(udid)","name":\#(shown),"model":null,"ios_version":null,"is_default":\#(isDefault)}"#
        guard case let .deviceSeen(record) = try BuildEvent.decode(line: line) else {
            throw XCTSkip("fixture is not a device-seen event")
        }
        return record
    }

    private func ipad(liveContainer: String? = "com.kdt.livecontainer.ABCDE12345",
                      known: Bool? = true) throws -> DeviceRecord {
        try device(Self.deviceJSON(
            udid: Self.ipadUDID, name: "iPad Air", model: "iPad13,16",
            liveContainer: liveContainer, isDefault: false, liveContainerKnown: known
        ))
    }

    private func spare() throws -> DeviceRecord {
        try device(Self.deviceJSON(udid: "00008030-0000000000000003", name: "Spare iPad",
                                   model: "iPad13,1"))
    }

    func test_the_default_connected_is_the_target_whatever_else_is() throws {
        let phone = try device(Self.deviceJSON(isDefault: true))
        XCTAssertEqual(
            BuildTargetBar.target(devices: [try ipad(), phone], delivery: .livecontainer),
            .device(name: "iPhone 15 Pro", mode: "LiveContainer")
        )
    }

    func test_the_default_missing_and_one_other_is_that_one_and_says_why() throws {
        let target = BuildTargetBar.target(
            devices: [try ipad()], delivery: .livecontainer,
            remembered: [try remembered(Self.phoneUDID, name: "iPhone 15 Pro")]
        )
        XCTAssertEqual(target, .fallback(
            name: "iPad Air", mode: "LiveContainer", because: "iPhone 15 Pro, your default, isn't connected"
        ))
        XCTAssertFalse(target.blocksBuild)
        XCTAssertFalse(target.needsAttention)
    }

    func test_the_default_missing_and_several_others_asks() throws {
        let target = BuildTargetBar.target(
            devices: [try ipad(), try spare()], delivery: .livecontainer,
            remembered: [try remembered(Self.phoneUDID, name: "iPhone 15 Pro")]
        )
        XCTAssertEqual(target, .defaultMissing(name: "iPhone 15 Pro"))
        XCTAssertTrue(target.blocksBuild, "Build waits for a pick")
        XCTAssertTrue(target.needsAttention)
        XCTAssertEqual(target.certainFailureClause, "several devices are connected and none was picked.")
    }

    func test_a_default_never_seen_is_named_by_its_udid() throws {
        XCTAssertEqual(
            BuildTargetBar.target(devices: [try ipad(), try spare()], delivery: .livecontainer,
                                  remembered: [try remembered(Self.phoneUDID, name: nil)]),
            .defaultMissing(name: Self.phoneUDID)
        )
    }

    func test_no_default_and_several_asks() throws {
        let target = BuildTargetBar.target(devices: [try ipad(), try spare()], delivery: .livecontainer)
        XCTAssertEqual(target, .noDefault)
        XCTAssertTrue(target.blocksBuild)
    }

    /// The CLI's usbmux listing counts a locked phone, so the bar does: one
    /// readable phone and one locked one is "several".
    func test_a_locked_phone_counts_as_connected() throws {
        let locked = try deviceError(Self.deviceErrorJSON())
        XCTAssertEqual(
            BuildTargetBar.target(devices: [try ipad()], delivery: .livecontainer, deviceErrors: [locked]),
            .noDefault
        )
    }

    func test_a_locked_default_is_still_the_target() throws {
        let locked = try deviceError(Self.deviceErrorJSON())
        let target = BuildTargetBar.target(
            devices: [try ipad()], delivery: .livecontainer, deviceErrors: [locked],
            remembered: [try remembered(locked.udid, name: "iPhone 15 Pro")]
        )
        XCTAssertEqual(target, .defaultUnreadable(name: "iPhone 15 Pro"))
        XCTAssertFalse(target.blocksBuild, "the build still goes there, and may fail at the install")
    }

    func test_none_connected_is_still_no_device() throws {
        XCTAssertEqual(
            BuildTargetBar.target(devices: [], delivery: .livecontainer,
                                  remembered: [try remembered(Self.phoneUDID, name: "iPhone 15 Pro")]),
            .noDevice
        )
    }

    func test_a_pick_says_this_build_and_where_the_next_one_goes() throws {
        let phone = try device(Self.deviceJSON(isDefault: true))
        XCTAssertEqual(
            BuildTargetBar.target(devices: [phone, try ipad()], delivery: .livecontainer,
                                  overrideUDID: Self.ipadUDID),
            .thisBuild(name: "iPad Air", mode: "LiveContainer", then: "back to iPhone 15 Pro")
        )
        XCTAssertEqual(
            BuildTargetBar.target(devices: [try ipad()], delivery: .livecontainer,
                                  overrideUDID: Self.ipadUDID,
                                  remembered: [try remembered(Self.phoneUDID, name: "iPhone 15 Pro")]),
            .thisBuild(name: "iPad Air", mode: "LiveContainer", then: BuildTargetBar.backToTheRules),
            "the default unplugged: the build after follows the table, not a device that isn't there"
        )
        XCTAssertEqual(
            BuildTargetBar.target(devices: [try ipad()], delivery: .livecontainer,
                                  overrideUDID: Self.ipadUDID),
            .thisBuild(name: "iPad Air", mode: "LiveContainer", then: BuildTargetBar.askEachTime)
        )
    }

    func test_picking_the_default_reads_as_the_default() throws {
        let phone = try device(Self.deviceJSON(isDefault: true))
        XCTAssertEqual(
            BuildTargetBar.target(devices: [phone, try ipad()], delivery: .livecontainer,
                                  overrideUDID: phone.udid),
            .device(name: "iPhone 15 Pro", mode: "LiveContainer")
        )
    }

    func test_livecontainer_known_missing_blocks_the_build() throws {
        let target = BuildTargetBar.target(
            devices: [try ipad(liveContainer: nil, known: true)], delivery: .livecontainer,
            overrideUDID: Self.ipadUDID
        )
        XCTAssertEqual(target, .liveContainerMissing(name: "iPad Air", udid: Self.ipadUDID, kind: "iPad"))
        XCTAssertTrue(target.blocksBuild)
        XCTAssertTrue(target.needsAttention)
    }

    func test_livecontainer_missing_on_the_default_and_on_the_fallback() throws {
        let phone = try device(Self.deviceJSON(liveContainer: nil, isDefault: true, liveContainerKnown: true))
        XCTAssertEqual(
            BuildTargetBar.target(devices: [phone], delivery: .livecontainer),
            .liveContainerMissing(name: "iPhone 15 Pro", udid: phone.udid, kind: "iPhone")
        )
        XCTAssertEqual(
            BuildTargetBar.target(devices: [try ipad(liveContainer: nil, known: true)],
                                  delivery: .livecontainer),
            .liveContainerMissing(name: "iPad Air", udid: Self.ipadUDID, kind: "iPad")
        )
    }

    /// B2: on a paid account a LiveContainer app that can't go to this device
    /// can go as a sideload instead, so the bar offers that too. Never on a
    /// free account, which can't sideload, nor before the account is known.
    func test_a_paid_account_is_offered_sideload_where_livecontainer_is_missing() throws {
        let bare = try ipad(liveContainer: nil, known: true)
        for (account, offered) in [(AccountMode.paid, true), (.free, false), (nil, false)] {
            XCTAssertEqual(
                BuildTargetBar.target(devices: [bare], delivery: .livecontainer,
                                      overrideUDID: Self.ipadUDID, account: account),
                .liveContainerMissing(name: "iPad Air", udid: Self.ipadUDID, kind: "iPad", sideload: offered),
                "\(String(describing: account))"
            )
        }
        XCTAssertEqual(
            BuildTargetBar.target(devices: [bare], delivery: .livecontainer, account: .paid),
            .liveContainerMissing(name: "iPad Air", udid: Self.ipadUDID, kind: "iPad", sideload: true),
            "the only device, not picked"
        )
        XCTAssertEqual(
            BuildTargetBar.target(devices: [bare], delivery: .livecontainer, overrideUDID: Self.ipadUDID,
                                  notDelivering: .dryRun, account: .paid),
            .notDelivering(option: .dryRun, device: .liveContainerMissing(
                name: "iPad Air", udid: Self.ipadUDID, kind: "iPad", sideload: true)),
            "a dry run still resolves the device, on the same account"
        )
    }

    /// The offer is a line of its own, under the fact: on one line, beside
    /// Set Up LiveContainer… and Change, it didn't fit the minimum width
    /// (`AppDetailLayoutTests`).
    func test_the_missing_livecontainer_line_offers_sideload_only_when_it_can() {
        XCTAssertEqual(BuildTargetBar.liveContainerMissingLine(name: "iPad Air"),
                       "LiveContainer isn't on iPad Air")
        XCTAssertEqual(BuildTargetBar.sideloadOffer, "Or switch this app to Sideload with Edit…")
        let offered = BuildTargetBar.explanation(
            for: .liveContainerMissing(name: "iPad Air", udid: Self.ipadUDID, kind: "iPad", sideload: true))
        XCTAssertTrue(offered.contains("Or switch this app to Sideload with Edit…"), offered)
        let free = BuildTargetBar.explanation(
            for: .liveContainerMissing(name: "iPad Air", udid: Self.ipadUDID, kind: "iPad"))
        XCTAssertFalse(free.contains("Sideload"), free)
    }

    func test_livecontainer_unread_or_not_needed_never_blocks() throws {
        XCTAssertEqual(
            BuildTargetBar.target(devices: [try ipad(liveContainer: nil, known: false)],
                                  delivery: .livecontainer, overrideUDID: Self.ipadUDID),
            .thisBuild(name: "iPad Air", mode: "LiveContainer", then: BuildTargetBar.askEachTime),
            "a listing that couldn't read the apps says nothing about LiveContainer"
        )
        let bare = try ipad(liveContainer: nil, known: true)
        XCTAssertEqual(
            BuildTargetBar.target(devices: [bare], delivery: .sideload, overrideUDID: Self.ipadUDID),
            .thisBuild(name: "iPad Air", mode: "Sideload", then: BuildTargetBar.askEachTime),
            "a sideload app needs no LiveContainer"
        )
        XCTAssertFalse(
            BuildTargetBar.target(devices: [bare], delivery: .livecontainer, overrideUDID: Self.ipadUDID,
                                  notDelivering: .skipInstall).blocksBuild,
            "a skipped install needs no LiveContainer"
        )
    }

    func test_the_kind_of_device_comes_from_its_model() {
        XCTAssertEqual(BuildTarget.deviceKind(model: "iPad13,16"), "iPad")
        // As the CLI says (`devices.family_from_lockdown`).
        XCTAssertEqual(BuildTarget.deviceKind(model: "iPod9,1"), "iPod touch")
        XCTAssertEqual(BuildTarget.deviceKind(model: "iPhone16,1"), "iPhone")
        // A model nothing names isn't guessed to be an iPhone (2.3.1 §3.1).
        XCTAssertEqual(BuildTarget.deviceKind(model: "unknown"), "device")
    }

    func test_the_new_states_have_tooltips_and_no_cli_jargon() {
        let states: [BuildTarget] = [
            .thisBuild(name: "iPad Air", mode: "LiveContainer", then: "back to iPhone 15 Pro"),
            .thisBuild(name: "iPad Air", mode: "LiveContainer", then: BuildTargetBar.askEachTime),
            .fallback(name: "iPad Air", mode: "LiveContainer", because: "iPhone 15 Pro, your default, isn't connected"),
            .defaultMissing(name: "iPhone 15 Pro"),
            .defaultUnreadable(name: "iPhone 15 Pro"),
            .liveContainerMissing(name: "iPad Air", udid: Self.ipadUDID, kind: "iPad"),
            .notDelivering(option: .dryRun, device: .defaultMissing(name: "iPhone 15 Pro")),
        ]
        for state in states {
            let text = BuildTargetBar.explanation(for: state)
            XCTAssertFalse(text.isEmpty, "\(state)")
            XCTAssertFalse(text.contains("select_device"), "\(state): \(text)")
            XCTAssertFalse(text.contains("idevice_id"), "\(state): \(text)")
        }
    }

    /// The pick is Build & Install's alone: an update (`--update`) takes no
    /// `--device` and goes where the CLI's table says, so the bar and its
    /// tooltips name Build & Install wherever they speak of the pick.
    func test_the_pick_is_worded_as_build_and_install_s() {
        let picked = BuildTarget.thisBuild(name: "iPad Air", mode: "LiveContainer",
                                           then: "back to iPhone 15 Pro")
        let tooltip = BuildTargetBar.explanation(for: picked)
        XCTAssertTrue(tooltip.contains("Build & Install"), tooltip)
        XCTAssertTrue(tooltip.contains("An update doesn't use the pick"), tooltip)
        for state in [BuildTarget.overrideMissing(udid: "U"), .overrideUnreadable(udid: "U"), .noDefault] {
            XCTAssertTrue(BuildTargetBar.explanation(for: state).contains("Build & Install"), "\(state)")
        }
    }

    /// How long a pick lasts (spec §11.5, review part 1 #1): used up only
    /// once its build starts installing or succeeds; a build that fails or
    /// is stopped before installing keeps it, for the retry. The tooltip
    /// says that, not that every ending clears it.
    func test_the_picks_tooltip_says_how_long_it_lasts() {
        let picked = BuildTarget.thisBuild(name: "iPad Air", mode: "LiveContainer", then: "back to iPhone 15 Pro")
        XCTAssertEqual(
            BuildTargetBar.explanation(for: picked),
            "The next Build & Install sends this app to iPad Air via LiveContainer, picked for that build "
                + "only. The pick clears when that build ends, if it started installing or succeeded, and "
                + "the one after goes back to iPhone 15 Pro. A build that fails or is stopped before "
                + "installing keeps it, for the retry. An update doesn't use the pick. The ✕ clears it now."
        )
        XCTAssertFalse(BuildTargetBar.explanation(for: picked).contains("however it ends"))
    }
}

// MARK: - The Change menu

extension BuildTargetBarTests {
    func test_a_menu_row_names_the_device_its_ios_and_livecontainer() throws {
        let phone = try device(Self.deviceJSON(isDefault: true, liveContainerKnown: true))
        let withLC = try device(Self.deviceJSON(
            udid: "00008030-0000000000000002", name: "iPad Air", model: "iPad13,16",
            liveContainer: "com.kdt.livecontainer.ABCDE12345", liveContainerKnown: true
        ))
        let unread = try device(Self.deviceJSON(
            udid: "00008030-0000000000000002", name: "iPad Air", model: "iPad13,16",
            liveContainerKnown: false
        ))

        XCTAssertEqual(BuildTargetBar.menuLabel(withLC, delivery: .livecontainer),
                       "iPad Air — iOS 26.1 · LiveContainer")
        XCTAssertEqual(BuildTargetBar.menuLabel(phone, delivery: .livecontainer),
                       "iPhone 15 Pro — iOS 26.1 · no LiveContainer (default)")
        XCTAssertEqual(BuildTargetBar.menuLabel(phone, delivery: .sideload),
                       "iPhone 15 Pro — iOS 26.1 (default)", "a sideload app needs no LiveContainer")
        XCTAssertEqual(BuildTargetBar.menuLabel(unread, delivery: .livecontainer),
                       "iPad Air — iOS 26.1", "an unread listing says nothing")
    }

    func test_picking_the_default_is_no_pick() throws {
        let phone = try device(Self.deviceJSON(isDefault: true))
        let other = try device(Self.deviceJSON(udid: "00008030-0000000000000002", name: "iPad Air"))

        XCTAssertNil(BuildTargetBar.pick(phone.udid, devices: [phone, other]))
        XCTAssertEqual(BuildTargetBar.pick(other.udid, devices: [phone, other]), other.udid)
    }

    func test_make_default_names_the_pick_or_the_only_device() throws {
        let phone = try device(Self.deviceJSON(isDefault: true))
        let other = try device(Self.deviceJSON(udid: "00008030-0000000000000002", name: "iPad Air"))
        let spare = try device(Self.deviceJSON(udid: "00008030-0000000000000003", name: "Spare iPad"))

        XCTAssertEqual(
            BuildTargetBar.makeDefaultCandidate(devices: [phone, other], pick: other.udid)?.udid,
            other.udid
        )
        XCTAssertNil(BuildTargetBar.makeDefaultCandidate(devices: [phone, other], pick: nil),
                     "the default is connected and nothing is picked")
        XCTAssertEqual(BuildTargetBar.makeDefaultCandidate(devices: [other], pick: nil)?.udid,
                       other.udid, "no connected default, one device")
        XCTAssertNil(BuildTargetBar.makeDefaultCandidate(devices: [other, spare], pick: nil),
                     "several and nothing picked: pick first")
    }
}

// MARK: - Review fixes (spec §11.5)

extension BuildTargetBarTests {
    /// A picked device that isn't connected stops Build & Install, named as
    /// it was picked, else as remembered, with ✕ as the way out.
    func test_a_picked_device_that_is_unplugged_blocks_and_is_named() throws {
        let phone = try device(Self.deviceJSON(isDefault: true))
        let gone = "00008030-0000000000000009"

        let picked = BuildTargetBar.target(devices: [phone], delivery: .livecontainer,
                                           overrideUDID: gone, overrideName: "Spare iPad")
        XCTAssertEqual(picked, .overrideMissing(udid: gone, name: "Spare iPad"))
        XCTAssertTrue(picked.blocksBuild)
        XCTAssertTrue(BuildTargetBar.explanation(for: picked).contains("✕"))
        XCTAssertTrue(BuildTargetBar.explanation(for: picked).contains("Spare iPad"))

        let seen = try remembered(gone, name: "Old iPad", isDefault: false)
        XCTAssertEqual(
            BuildTargetBar.target(devices: [phone], delivery: .livecontainer, overrideUDID: gone,
                                  remembered: [seen]),
            .overrideMissing(udid: gone, name: "Old iPad")
        )
        XCTAssertEqual(
            BuildTargetBar.target(devices: [phone], delivery: .livecontainer, overrideUDID: gone),
            .overrideMissing(udid: gone), "nothing knows its name: the udid, in the tooltip"
        )
    }

    /// The menu checks where the next Build & Install goes.
    func test_the_menu_checks_the_current_target() throws {
        let phone = try device(Self.deviceJSON(isDefault: true))
        let other = try device(Self.deviceJSON(udid: "00008030-0000000000000002", name: "iPad Air"))
        let spare = try device(Self.deviceJSON(udid: "00008030-0000000000000003", name: "Spare iPad"))

        XCTAssertEqual(BuildTargetBar.currentTarget(devices: [phone, other], pick: nil), phone.udid)
        XCTAssertEqual(BuildTargetBar.currentTarget(devices: [phone, other], pick: other.udid), other.udid)
        XCTAssertEqual(BuildTargetBar.currentTarget(devices: [other], pick: nil), other.udid)
        XCTAssertNil(BuildTargetBar.currentTarget(devices: [other, spare], pick: nil),
                     "no single device: nothing checked")
        XCTAssertEqual(BuildTargetBar.currentTarget(devices: [phone], pick: "gone"), phone.udid,
                       "a pick that isn't here isn't checked")
    }

    /// The words this plan wrote say "iPhone or iPad", never "phone" or "No
    /// iOS device", and none names usbmuxd but the one that explains it.
    func test_the_switcher_s_tooltips_say_iphone_or_ipad() {
        let states: [BuildTarget] = [
            .device(name: "iPhone 15 Pro", mode: "LiveContainer"),
            .thisBuild(name: "iPad Air", mode: "LiveContainer", then: BuildTargetBar.backToTheRules),
            .fallback(name: "iPad Air", mode: "LiveContainer", because: "no default device is set"),
            .defaultMissing(name: "iPhone 15 Pro"), .defaultUnreadable(name: "iPhone 15 Pro"),
            .liveContainerMissing(name: "iPad Air", udid: "U", kind: "iPad"), .noDefault, .noDevice,
            .overrideMissing(udid: "U", name: "iPad Air"),
        ]
        for state in states {
            let text = BuildTargetBar.explanation(for: state)
            let words = text.replacingOccurrences(of: "iPhone", with: "")
            XCTAssertFalse(words.lowercased().contains("phone"), "\(state): \(text)")
            XCTAssertFalse(text.contains("No iOS device"), "\(state): \(text)")
            XCTAssertFalse(text.contains("usbmux"), "\(state): \(text)")
        }
    }
}

/// What the app calls a device in the text a person reads (2.3.1 §3.1): an
/// iPhone or iPad, the device, or its kind when it's known; never a bare
/// "phone", which on an iPad's page reads as a mistake. A scan of every string
/// literal in the app's sources, comments left out.
final class DeviceWordsTests: XCTestCase {
    /// A whole-word "phone" or "phones": not "iPhone", and not an Apple ID's
    /// "email or phone".
    static let barePhone = try! NSRegularExpression(
        pattern: #"(?<![A-Za-z])phones?\b(?<!email or phone)"#, options: [.caseInsensitive]
    )
    /// "the iPhone", "an iPhone", "your iPhone", "On My iPhone": one kind for
    /// every device. A sentence split over lines is read whole
    /// (`offenders`), so one that goes on to "or iPad" on the next line is
    /// let off by what it says there, not by where the line ends.
    static let oneKind = try! NSRegularExpression(
        pattern: #"\b(?:the|an|your|On My) iPhone\b(?! or iPad|, iPad|/iPad)"#
    )
    /// A string literal on one line, escapes and all.
    static let literal = try! NSRegularExpression(pattern: #""(?:[^"\\]|\\.)*""#)

    static func offends(_ text: String) -> Bool {
        let range = NSRange(text.startIndex..., in: text)
        return [barePhone, oneKind].contains { $0.firstMatch(in: text, range: range) != nil }
    }

    static func literals(in line: String) -> [String] {
        literal.matches(in: line, range: NSRange(line.startIndex..., in: line)).compactMap {
            Range($0.range, in: line).map { String(line[$0]) }
        }
    }

    /// Each string literal in `lines` that offends, as "line: literal". A
    /// line's last literal that the next line carries on with `+ "…"` is
    /// joined to that one first, and named by the line it starts on.
    static func offenders(in lines: [(number: Int, text: String)]) -> [String] {
        var found: [(number: Int, text: String)] = []
        var carriesOn = false
        for line in lines {
            let code = line.text.trimmingCharacters(in: .whitespaces)
            var here = literals(in: line.text).map { (number: line.number, text: $0) }
            if carriesOn, code.hasPrefix("+ \""), !here.isEmpty, let open = found.popLast() {
                here[0] = (open.number, String(open.text.dropLast()) + here[0].text.dropFirst())
            }
            found += here
            carriesOn = code.hasSuffix("\"")
        }
        return found.filter { offends($0.text) }.map { "\($0.number): \($0.text)" }
    }

    func test_no_string_in_the_app_says_phone() throws {
        let root = try SourceScan.appSources()
        var offenders: [String] = []
        for file in try SourceScan.swiftSources(under: root) {
            let path = SourceScan.relativePath(of: file, under: root)
            offenders += Self.offenders(in: try SourceScan.codeLines(of: file)).map { "\(path):\($0)" }
        }
        XCTAssertEqual(offenders, [], offenders.joined(separator: "\n"))
    }

    /// The patterns themselves, so a scan that matches nothing can't pass.
    func test_the_scan_tells_a_phone_from_an_iphone() {
        XCTAssertTrue(Self.offends(#""Pick an app, a phone, or a section.""#))
        XCTAssertTrue(Self.offends(#""connected phone""#))
        XCTAssertTrue(Self.offends(#""Plug an iPhone in over USB""#))
        XCTAssertFalse(Self.offends(#""Apple ID (email or phone)""#))
        XCTAssertFalse(Self.offends(#""Plug an iPhone or iPad in over USB""#))
        XCTAssertFalse(Self.offends(#""on your iPhone, iPad or other Mac""#))
        XCTAssertEqual(Self.literals(in: #"Text("a \"phone\"") // the phone"#), [#""a \"phone\"""#])
    }

    /// A sentence split over two lines, `"… your iPhone "` then `+ "…"`, is
    /// read as the one sentence it makes: ending where the line does
    /// doesn't let "your iPhone" off unless the next line says "or iPad".
    func test_a_sentence_split_over_lines_is_read_whole() {
        let split: [(number: Int, text: String)] = [
            (number: 7, text: #"    static let body = "Unlock your iPhone ""#),
            (number: 8, text: #"        + "and try again.""#),
        ]
        XCTAssertEqual(Self.offenders(in: split), [#"7: "Unlock your iPhone and try again.""#])
        let either: [(number: Int, text: String)] = [
            (number: 7, text: #"    static let body = "Installs them on your iPhone ""#),
            (number: 8, text: #"        + "or iPad.""#),
        ]
        XCTAssertEqual(Self.offenders(in: either), [])
        // Carried on with something not a literal: nothing says "or iPad".
        let unsaid: [(number: Int, text: String)] = [
            (number: 7, text: #"    static let body = "Unlock your iPhone ""#),
            (number: 8, text: #"        + name"#),
        ]
        XCTAssertEqual(Self.offenders(in: unsaid), [#"7: "Unlock your iPhone ""#])
    }
}
