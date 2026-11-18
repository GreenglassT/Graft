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
        isDefault: Bool? = false
    ) -> String {
        let lc = liveContainer.map { "\"\($0)\"" } ?? "null"
        let flag = isDefault.map { ",\"is_default\":\($0)" } ?? ""
        return #"{"type":"device","udid":"\#(udid)","name":"\#(name)","model":"\#(model)","#
            + #""ios_version":"\#(iosVersion)","connection_type":"\#(connection)","#
            + #""livecontainer_bundle_id":\#(lc)\#(flag)}"#
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

    /// One phone connected and unmarked, which is the case most likely to be
    /// "helpfully" resolved: there is no ambiguity about *which* phone, only
    /// about whether it is the configured one. It is not — the config names an
    /// unplugged handset, or names none — and a build would either fail or land
    /// somewhere this pane cannot predict.
    func test_one_unmarked_phone_is_still_no_default() throws {
        let phone = try device(Self.deviceJSON())
        XCTAssertFalse(phone.isDefault, "premise: the only phone is not the default")

        XCTAssertEqual(
            BuildTargetBar.target(devices: [phone], delivery: .livecontainer),
            .noDefault,
            "the only connected phone is not necessarily the configured one"
        )
    }

    /// An older `graft` that does not emit `is_default` at all.
    ///
    /// `DeviceRecord` decodes the field with `decodeIfPresent(...) ?? false`
    /// precisely so the phone still appears; the bar then has to degrade to
    /// "no default" rather than inventing one.
    func test_a_cli_that_omits_is_default_reports_no_default() throws {
        let phone = try device(Self.deviceJSON(isDefault: nil))
        XCTAssertFalse(
            phone.isDefault,
            "premise: an absent is_default decodes as false rather than failing the event"
        )

        XCTAssertEqual(
            BuildTargetBar.target(devices: [phone], delivery: .livecontainer),
            .noDefault
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
            .device(name: "iPhone 13 Pro", mode: "LiveContainer"),
            "Build options replaces the default rather than narrowing it"
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
            .noDefault
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
    /// used to answer it with a muted grey "Dry run — this build won't be sent
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
    /// device … not connected" whatever the dry run intended to do afterwards.
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
    /// `.noDefault` resolves: `select_device(None)` returns `devices[0]`.
    /// `.unreadable` very likely resolves too, because the list it walks comes
    /// from `idevice_id -l -n`, a usbmux enumeration with no lockdown handshake.
    /// `.scanning` is not yet an answer about anything.
    func test_a_dry_run_stays_quiet_for_device_states_it_can_survive() throws {
        let unmarked = try device(Self.deviceJSON())
        let locked = try deviceError(Self.deviceErrorJSON())
        let cases: [(BuildTarget, String)] = [
            (BuildTargetBar.target(devices: [unmarked], delivery: .livecontainer,
                                   notDelivering: .dryRun), "noDefault"),
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
                .notDelivering(option: .dryRun, device: .noDefault),
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
}
