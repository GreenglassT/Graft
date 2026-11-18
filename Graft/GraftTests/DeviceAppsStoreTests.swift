import XCTest
@testable import Graft

/// Decoding `--device-apps` into the pick list for decryption.
///
/// The classification (`encrypted`) is the CLI's, verified against a real
/// device in `tests/test_device_apps.py`. What is pinned here is the joining:
/// the encrypted apps are surfaced as the decrypt targets, a dropped connection
/// leaves the last list standing, and "not asked yet" stays distinct from "no
/// apps".
@MainActor
final class DeviceAppsStoreTests: XCTestCase {

    private func app(_ bundle: String, _ name: String, encrypted: Bool) -> String {
        let payload: [String: Any] = [
            "type": "device-app", "bundle_id": bundle, "name": name,
            "version": "1.0", "encrypted": encrypted,
        ]
        let data = try! JSONSerialization.data(withJSONObject: payload)
        return String(data: data, encoding: .utf8)!
    }

    func test_encrypted_apps_are_the_decrypt_targets() async {
        let lines = [
            app("com.store.one", "Store One", encrypted: true),
            app("com.side.load", "AltStore", encrypted: false),
            app("com.store.two", "Store Two", encrypted: true),
            #"{"type":"device-apps-end","count":3,"encrypted":2}"#,
        ].joined(separator: #"\n"#)
        let sut = DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))

        await sut.load(udid: "UDID-13", argsOverride: ["-c", "printf '\(lines)\\n'"])

        XCTAssertEqual(sut.apps["UDID-13"]?.count, 3)
        XCTAssertEqual(
            sut.encryptedApps(on: "UDID-13").map(\.bundleId),
            ["com.store.one", "com.store.two"]
        )
        XCTAssertEqual(sut.lastLoad["UDID-13"]?.encrypted, 2)
    }

    func test_nothing_is_claimed_before_a_load() {
        let sut = DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        XCTAssertNil(sut.lastLoad["UDID-13"], "the pane would say 'no apps' having asked nothing")
        XCTAssertTrue(sut.apps.isEmpty)
    }

    /// The defect the udid keying exists for, on the listing half: two phones,
    /// one store, and neither answer standing in for the other.
    ///
    /// This was structurally impossible to get right before — the store held one
    /// list, so `DeviceDetailView` had to `.id(device.udid)` the whole section
    /// to stop phone A's apps being drawn under phone B's name, which discarded
    /// an 8.4–10.8s listing on every switch. Both listings surviving one store
    /// is what let that `.id` go.
    func test_two_phones_keep_their_own_listings() async {
        let one = [
            app("com.only.on.13", "Thirteen", encrypted: true),
            #"{"type":"device-apps-end","count":1,"encrypted":1}"#,
        ].joined(separator: #"\n"#)
        let two = [
            app("com.only.on.15", "Fifteen", encrypted: false),
            app("com.also.on.15", "Fifteen Too", encrypted: true),
            #"{"type":"device-apps-end","count":2,"encrypted":1}"#,
        ].joined(separator: #"\n"#)
        let sut = DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))

        await sut.load(udid: "UDID-13", argsOverride: ["-c", "printf '\(one)\\n'"])
        await sut.load(udid: "UDID-15", argsOverride: ["-c", "printf '\(two)\\n'"])

        XCTAssertEqual(sut.apps["UDID-13"]?.map(\.bundleId), ["com.only.on.13"])
        XCTAssertEqual(
            sut.apps["UDID-15"]?.map(\.bundleId), ["com.only.on.15", "com.also.on.15"]
        )
        XCTAssertEqual(sut.lastLoad["UDID-13"]?.count, 1)
        XCTAssertEqual(sut.lastLoad["UDID-15"]?.count, 2)
    }

    /// A phone that dropped off the network mid-session should not erase the
    /// list the user was looking at.
    func test_a_failed_refresh_keeps_the_previous_list() async {
        let lines = [
            app("com.store.one", "Store One", encrypted: true),
            #"{"type":"device-apps-end","count":1,"encrypted":1}"#,
        ].joined(separator: #"\n"#)
        let sut = DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        await sut.load(udid: "UDID-13", argsOverride: ["-c", "printf '\(lines)\\n'"])
        XCTAssertEqual(sut.apps["UDID-13"]?.count, 1)

        await sut.load(udid: "UDID-13", argsOverride: ["-c", "exit 1"])

        XCTAssertNotNil(sut.listErrors["UDID-13"])
        XCTAssertNil(sut.lastError, "a failed listing must not read as a failed dump")
        XCTAssertEqual(
            sut.apps["UDID-13"]?.count, 1,
            "a dropped connection blanked the list it should keep"
        )
        XCTAssertFalse(sut.loading.contains("UDID-13"), "the spinner never stopped")
    }

    /// The two concerns that used to be two store instances, now two fields.
    ///
    /// `InstalledAppsSection` draws `listErrors[udid]` and `DeviceDecryptSection`
    /// draws `lastError`; one store with one error field would have put a failed
    /// dump's banner under a section that had nothing to do with the dump, which
    /// is the loss the second instance existed to prevent.
    func test_a_listing_failure_and_a_dump_failure_land_in_different_fields() async {
        let sut = DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/false")))

        await sut.load(udid: "UDID-13")
        XCTAssertNotNil(sut.listErrors["UDID-13"])
        XCTAssertNil(sut.lastError)

        _ = await sut.decrypt(
            DeviceAppRecord(bundleId: "com.x", name: "X", version: "1.0", encrypted: true)
        )
        XCTAssertNotNil(sut.lastError, "the dump's failure had nowhere of its own to go")
        XCTAssertNotNil(
            sut.listErrors["UDID-13"], "the dump's failure cleared the listing's banner"
        )
    }

    /// A listing that succeeds clears the banner a previous failure left. One
    /// store now outlives the pane, so nothing else ever would.
    func test_a_successful_listing_clears_the_previous_failure() async {
        let lines = [
            app("com.store.one", "Store One", encrypted: true),
            #"{"type":"device-apps-end","count":1,"encrypted":1}"#,
        ].joined(separator: #"\n"#)
        let sut = DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))

        await sut.load(udid: "UDID-13", argsOverride: ["-c", "exit 1"])
        XCTAssertNotNil(sut.listErrors["UDID-13"])

        await sut.load(udid: "UDID-13", argsOverride: ["-c", "printf '\(lines)\\n'"])
        XCTAssertNil(sut.listErrors["UDID-13"])
    }
}

// MARK: - Decrypt action

extension DeviceAppsStoreTests {

    private func encApp(_ bundle: String) -> DeviceAppRecord {
        DeviceAppRecord(bundleId: bundle, name: bundle, version: "1.0", encrypted: true)
    }

    func test_a_successful_decrypt_returns_where_it_landed() async {
        let end = #"{"type":"decrypt-end","bundle_id":"com.x","version":"2.0","path":"/c/com.x_2.0.ipa","binaries":7}"#
        let sut = DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))

        let result = await sut.decrypt(encApp("com.x"), argsOverride: ["-c", "printf '\(end)\\n'"])

        XCTAssertEqual(result?.version, "2.0")
        XCTAssertEqual(result?.path, "/c/com.x_2.0.ipa")
        XCTAssertEqual(result?.binaries, 7)
        XCTAssertEqual(sut.lastDecrypt, result)
        XCTAssertNil(sut.decrypting, "the row's progress never cleared")
    }

    func test_a_failed_decrypt_is_reported_and_leaves_no_result() async {
        let sut = DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/false")))

        let result = await sut.decrypt(encApp("com.x"))

        XCTAssertNil(result)
        XCTAssertNotNil(sut.lastError)
        XCTAssertNil(sut.decrypting)
    }

    /// One dump at a time: each writes into cache_dir and drives one
    /// frida-server, so a second concurrent one is refused.
    func test_a_second_decrypt_while_one_runs_is_refused() async {
        let sut = DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        async let first = sut.decrypt(encApp("com.x"), argsOverride: ["-c", "sleep 0.4"])
        try? await Task.sleep(for: .milliseconds(80))
        let second = await sut.decrypt(encApp("com.y"))
        _ = await first
        XCTAssertNil(second, "a concurrent dump was allowed")
    }

    func test_a_successful_frontmost_decrypt_returns_where_it_landed() async {
        let end = #"{"type":"decrypt-end","bundle_id":"com.front","version":"3.0","path":"/c/com.front_3.0.ipa","binaries":1}"#
        let sut = DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))

        let result = await sut.decryptFrontmost(argsOverride: ["-c", "printf '\(end)\\n'"])

        XCTAssertEqual(result?.bundleId, "com.front")
        XCTAssertEqual(result?.path, "/c/com.front_3.0.ipa")
        XCTAssertEqual(sut.lastDecrypt, result)
        XCTAssertFalse(sut.decryptingFrontmost, "the button never returned to idle")
    }

    /// The frontmost dump and a row dump share one cache_dir and one
    /// frida-server, so a frontmost dump in flight excludes both a second
    /// frontmost dump and a row dump. Each candidate is given args that *would*
    /// succeed, so a nil result proves the guard refused it rather than the
    /// subprocess merely failing.
    func test_a_frontmost_decrypt_excludes_the_other_dumps() async {
        let end = #"{"type":"decrypt-end","bundle_id":"com.x","version":"1.0","path":"/c/x.ipa","binaries":1}"#
        let succeed = ["-c", "printf '\(end)\\n'"]
        let sut = DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))

        async let running = sut.decryptFrontmost(argsOverride: ["-c", "sleep 0.4"])
        try? await Task.sleep(for: .milliseconds(80))
        let secondFrontmost = await sut.decryptFrontmost(argsOverride: succeed)
        let rowDump = await sut.decrypt(encApp("com.x"), argsOverride: succeed)
        _ = await running

        XCTAssertNil(secondFrontmost, "a second frontmost dump ran alongside one")
        XCTAssertNil(rowDump, "a row dump ran alongside a frontmost dump")
    }

    /// The reverse of the exclusion above, and the direction the frontmost-first
    /// test can't reach: a row dump in flight refuses a frontmost dump. The
    /// frontmost candidate is given args that would succeed, so a nil result
    /// proves the guard refused it rather than the subprocess failing.
    func test_a_row_decrypt_in_flight_refuses_a_frontmost_dump() async {
        let end = #"{"type":"decrypt-end","bundle_id":"com.x","version":"1.0","path":"/c/x.ipa","binaries":1}"#
        let sut = DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))

        async let running = sut.decrypt(encApp("com.row"), argsOverride: ["-c", "sleep 0.4"])
        try? await Task.sleep(for: .milliseconds(80))
        let frontmost = await sut.decryptFrontmost(argsOverride: ["-c", "printf '\(end)\\n'"])
        _ = await running

        XCTAssertNil(frontmost, "a frontmost dump ran alongside an in-flight row dump")
    }

    /// Busy means busy, whichever route started it.
    ///
    /// The per-device pane puts two controls over one store — the decrypt
    /// card's one-click button, and a Dump on every encrypted row of the
    /// installed-apps list under it — and the store refuses the second dump
    /// silently, returning nil rather than queueing. The card's button was
    /// gated on `decryptingFrontmost` alone, which is *false* for the whole of
    /// a row dump: it stayed live over a store that would refuse it, so
    /// pressing it did nothing at all and said nothing about why. Both
    /// directions are checked, because a one-directional flag is exactly the
    /// bug being closed.
    func test_any_dump_in_flight_reports_the_store_as_busy() async {
        let sut = DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        XCTAssertFalse(sut.isDumping, "premise: nothing is running yet")

        async let row = sut.decrypt(encApp("com.row"), argsOverride: ["-c", "sleep 0.4"])
        try? await Task.sleep(for: .milliseconds(80))
        XCTAssertTrue(
            sut.isDumping,
            "a row dump reported the store as idle, which leaves the decrypt card's "
            + "button live over a dump it cannot start"
        )
        XCTAssertFalse(
            sut.decryptingFrontmost,
            "premise: this is the row route, so the flag the card used to read is false — "
            + "which is what made the assertion above necessary"
        )
        _ = await row
        XCTAssertFalse(sut.isDumping, "the busy state never cleared after a row dump")

        async let frontmost = sut.decryptFrontmost(argsOverride: ["-c", "sleep 0.4"])
        try? await Task.sleep(for: .milliseconds(80))
        XCTAssertTrue(
            sut.isDumping,
            "a frontmost dump has to disable every row's Dump button as well"
        )
        _ = await frontmost
        XCTAssertFalse(sut.isDumping, "the busy state never cleared after a frontmost dump")
    }
}

/// The argv the decrypt actions build, and the one recovery the pane offers.
///
/// `--device` was never passed at all, so the CLI fell back to
/// `default_device_udid` — which names the phone builds are *delivered* to,
/// not the one dumps come *off*. With a stock phone running LiveContainer as
/// the delivery target and a jailbroken phone as the dump source, the GUI
/// aimed decryption at hardware that could not perform it, and the failure
/// read as a frida error about the wrong device.
///
/// These assert the argv itself rather than stubbing it: the defect was an
/// *absent* flag, and a test that supplies its own argv can never see one.
@MainActor
final class DeviceAppsDecryptArgsTests: XCTestCase {

    private func store() -> DeviceAppsStore {
        DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
    }

    func test_a_frontmost_dump_names_the_device() {
        let args = DeviceAppsStore.decryptArgs(
            bundleId: nil, device: "UDID-13", extensions: .automatic
        )
        XCTAssertEqual(args, ["--json", "--decrypt-frontmost", "--device", "UDID-13"])
    }

    func test_a_row_dump_names_the_device_too() {
        /// The path that passed no device at all.
        let args = DeviceAppsStore.decryptArgs(
            bundleId: "com.x", device: "UDID-13", extensions: .automatic
        )
        XCTAssertEqual(args, ["--json", "--decrypt", "com.x", "--device", "UDID-13"])
    }

    func test_no_device_means_no_flag_rather_than_an_empty_one() {
        /// An empty `--device ""` would be worse than omitting it: the CLI
        /// would take it as a UDID and match nothing.
        let args = DeviceAppsStore.decryptArgs(
            bundleId: nil, device: nil, extensions: .automatic
        )
        XCTAssertEqual(args, ["--json", "--decrypt-frontmost"])
    }

    func test_refusing_the_partial_dump_is_the_opt_in() {
        /// Dropping extensions no tool can decrypt is what a dump does — it is
        /// what makes one click possible for an app that has any — so the GUI
        /// sends nothing for the common case and only speaks up to refuse.
        XCTAssertFalse(
            DeviceAppsStore.decryptArgs(
                bundleId: nil, device: "U", extensions: .automatic
            ).contains("--keep-encrypted-extensions"),
            "the default needs no flag"
        )
        XCTAssertFalse(
            DeviceAppsStore.decryptArgs(
                bundleId: nil, device: "U", extensions: .automatic
            ).contains("--strip-encrypted-extensions"),
            "asking for the default would be noise, and pins nothing"
        )
        XCTAssertTrue(
            DeviceAppsStore.decryptArgs(
                bundleId: nil, device: "U", extensions: .keep
            ).contains("--keep-encrypted-extensions")
        )
    }

    func test_a_retry_reuses_the_same_device() async {
        let sut = store()
        _ = await sut.decryptFrontmost(device: "UDID-13", argsOverride: ["-c", ":"])
        _ = await sut.retryDroppingExtensions()
        XCTAssertEqual(sut.lastFrontmostDevice, "UDID-13",
                       "\"retry\" must not quietly mean \"retry against a different phone\" — "
                       + "with two connected, only one is the jailbroken one")
    }

    func test_the_retry_is_offered_only_when_the_cli_recommends_it() {
        let sut = store()
        XCTAssertFalse(sut.canRetryDroppingExtensions, "no failure yet")

        sut.lastError = "11 of 36 binaries are still encrypted after the dump: … "
            + "these are all app extensions, and you asked to keep them. Without "
            + "`--keep-encrypted-extensions` they are dropped and the rest is staged"
        XCTAssertTrue(sut.canRetryDroppingExtensions)

        // A failure the flag cannot fix must not offer it.
        sut.lastError = "couldn't reach frida-server on the device"
        XCTAssertFalse(sut.canRetryDroppingExtensions)
    }

    func test_the_offer_keys_off_the_flag_name_not_a_reworded_sentence() {
        // The CLI owns the decision and phrases the hint; a second opinion
        // computed here is how the two drift. Rewording keeps this working,
        // withdrawing the recommendation withdraws the button.
        let sut = store()
        sut.lastError = "some future wording that still says --keep-encrypted-extensions"
        XCTAssertTrue(sut.canRetryDroppingExtensions)
    }
}

/// The argv `load` builds, and specifically that it always asks for icons.
///
/// `InstalledAppsSection` is the only reader of this store's `apps` left once
/// `DevicesView` is gone, and it draws every row's real icon from
/// `iconPath` — so a listing that forgot `--with-icons` would silently draw
/// the lettered tile for every app on the phone rather than the ones that
/// actually resolve to nothing. Pinned as a pure function, same shape as
/// `decryptArgs` above, so this doesn't need a real subprocess to check.
@MainActor
final class DeviceAppsLoadArgsTests: XCTestCase {

    func test_a_scoped_listing_asks_for_icons() {
        XCTAssertEqual(
            DeviceAppsStore.loadArgs(device: "UDID-13"),
            ["--json", "--device-apps", "--with-icons", "--device", "UDID-13"]
        )
    }

    /// There is no unscoped listing any more, and its absence is the point.
    ///
    /// `loadArgs` used to take `String?` and omit `--device` when it was nil,
    /// which let the CLI enumerate whichever phone usbmux reported first — an
    /// answer the store then had to file under some key it could not name.
    /// A listing keyed by udid cannot be built from a udid that does not exist,
    /// so the parameter is required and this branch is gone rather than
    /// untested.
    func test_a_listing_cannot_be_built_without_a_phone_to_name() {
        XCTAssertEqual(
            DeviceAppsStore.loadArgs(device: "UDID-15").filter { $0 == "--device" }.count, 1,
            "--device is unconditional now"
        )
    }
}

/// Who chooses the backend — and, since this pane stopped choosing, that the
/// choice reaches the CLI unmade.
///
/// The GUI used to decide from the row's own `frontmost` flag: attach to what
/// is on screen, spawn everything else. That rule became wrong when a dump
/// learned to launch the app itself, and the pane had no way to know — which is
/// the argument for not holding the rule at all. Naming a backend also *pins*
/// it, turning off the CLI's fallback to the other tool, so an unasked-for
/// default here costs the retry as well as the choice.
@MainActor
final class DecryptBackendChoiceTests: XCTestCase {

    private func app(_ bundle: String, frontmost: Bool) -> DeviceAppRecord {
        DeviceAppRecord(
            bundleId: bundle, name: bundle, version: "1.0",
            encrypted: true, frontmost: frontmost
        )
    }

    func test_a_row_dump_names_no_backend_of_its_own() async {
        let args = DeviceAppsStore.decryptArgs(
            bundleId: "com.x", device: "U", extensions: .automatic, backend: nil
        )
        XCTAssertFalse(args.contains(CLIContract.Flag.decryptBackend),
                       "an unpinned dump must leave the choice — and the fallback — to the CLI")

        let sut = DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        _ = await sut.decrypt(app("com.x", frontmost: false), device: "U",
                              argsOverride: ["-c", ":"])
        XCTAssertNil(sut.lastAttempt?.backend,
                     "nothing was pinned, so nothing should be recorded as pinned")
    }

    func test_an_explicit_backend_is_passed_through() async {
        let args = DeviceAppsStore.decryptArgs(
            bundleId: "com.x", device: "U", extensions: .automatic,
            backend: CLIContract.DecryptBackend.bagbak
        )
        XCTAssertTrue(args.contains(CLIContract.DecryptBackend.bagbak))
        XCTAssertTrue(args.contains(CLIContract.Flag.decryptBackend))
    }

    func test_the_frontmost_dump_also_leaves_the_choice_open() async {
        let sut = DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        _ = await sut.decryptFrontmost(device: "U", argsOverride: ["-c", ":"])
        XCTAssertNil(sut.lastAttempt?.backend)
    }
}

/// The readiness probe the pane leans on before offering a dump.
///
/// The point of it is that the *CLI* decides: which preconditions exist,
/// whether each passed, and what to do about it. The GUI re-derives none of
/// that — a second opinion computed from an error string is how the two drift,
/// and this pane already had one such bug.
@MainActor
final class DecryptReadinessTests: XCTestCase {

    private func store(emitting line: String) async -> DeviceAppsStore {
        let s = DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        await s.checkReadiness(udid: "UDID-13")
        return s
    }

    private func decode(_ line: String) throws -> DecryptCheckRecord {
        guard case let .decryptCheck(r) = try BuildEvent.decode(line: line) else {
            throw XCTSkip("expected a decrypt-check event")
        }
        return r
    }

    private static let readyLine = #"{"type":"decrypt-check","device":"U","device_name":"iPhone 13 Pro","ready":true,"recommended_backend":"frida-ios-dump","frontmost_bundle_id":"com.alpha.app","frontmost_name":"Alpha","checks":[{"key":"frida","label":"Jailbreak (frida-server)","ok":true,"detail":"reachable, 429 processes","fix":null},{"key":"ssh","label":"SSH login","ok":true,"detail":"authenticated as root","fix":null},{"key":"foreground","label":"App on screen","ok":true,"detail":"Alpha","fix":null}],"backends":[{"name":"frida-ios-dump","usable":true,"reason":null,"note":null},{"name":"bagbak","usable":true,"reason":null,"note":null}]}"#

    private static let blockedLine = #"{"type":"decrypt-check","device":"U","device_name":"iPhone 13 Pro","ready":false,"recommended_backend":null,"frontmost_bundle_id":null,"frontmost_name":null,"checks":[{"key":"frida","label":"Jailbreak (frida-server)","ok":true,"detail":"reachable","fix":null},{"key":"ssh","label":"SSH login","ok":false,"detail":"no SSH server answered","fix":"install OpenSSH from Sileo"},{"key":"foreground","label":"App on screen","ok":false,"detail":"nothing on screen","fix":"unlock the phone and open the app"}],"backends":[{"name":"frida-ios-dump","usable":false,"reason":"it pulls the app over SSH","note":null},{"name":"bagbak","usable":true,"reason":null,"note":"bagbak bundles frida 16.7.19, the phone runs 16.6.6"}]}"#

    func test_a_ready_device_names_the_app_it_will_dump() throws {
        let r = try decode(Self.readyLine)
        XCTAssertTrue(r.ready)
        XCTAssertEqual(r.frontmostName, "Alpha")
        XCTAssertEqual(r.recommendedBackend, "frida-ios-dump")
        XCTAssertTrue(r.checks.allSatisfy(\.ok))
    }

    func test_a_blocked_device_carries_a_fix_for_every_failure() throws {
        /// The whole reason this exists: a blocker with no fix is the timeout
        /// it replaced, only faster.
        let r = try decode(Self.blockedLine)
        XCTAssertFalse(r.ready)
        for check in r.checks where !check.ok {
            XCTAssertNotNil(check.fix, "\(check.key) failed with no way to act on it")
            XCTAssertFalse(check.fix!.isEmpty)
        }
    }

    func test_a_passing_check_carries_no_fix() throws {
        /// Advice attached to something that is working reads as a problem.
        let r = try decode(Self.readyLine)
        for check in r.checks where check.ok {
            XCTAssertNil(check.fix)
        }
    }

    func test_a_backend_note_is_not_a_reason_it_cannot_run() throws {
        /// bagbak carries a version-skew caveat while remaining usable. Folding
        /// the two together would either hide the caveat or refuse a backend on
        /// a suspicion.
        let r = try decode(Self.blockedLine)
        let bagbak = r.backends.first { $0.name == "bagbak" }
        XCTAssertEqual(bagbak?.usable, true)
        XCTAssertNil(bagbak?.reason)
        XCTAssertNotNil(bagbak?.note)

        let frida = r.backends.first { $0.name == "frida-ios-dump" }
        XCTAssertEqual(frida?.usable, false)
        XCTAssertNotNil(frida?.reason)
    }

    func test_the_probe_survives_a_cli_that_says_nothing() async {
        /// Not being able to *ask* is not a decrypt failure, so it must not
        /// raise a red banner over a pane whose real problem is a sleeping
        /// phone.
        let s = await store(emitting: "")
        XCTAssertNil(s.readiness["UDID-13"])
        XCTAssertNil(s.lastError)
    }

    /// The correctness defect the udid keying exists for, on the readiness half.
    ///
    /// `DeviceDecryptSection` draws this checklist directly under the pane's
    /// header, and a probe takes 2-5s. With one unkeyed value, switching from a
    /// jailbroken phone to a stock one left the jailbroken phone's "ready to
    /// dump" answer on screen under the stock phone's name for the whole of
    /// that window — a card asserting a capability about the wrong handset.
    /// `DecryptCheckRecord` carries `device` and `device_name`, and nothing
    /// compared them; now nothing has to.
    func test_a_second_phones_probe_does_not_answer_for_the_first() async {
        let s = DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))

        await s.checkReadiness(
            udid: "UDID-13", argsOverride: ["-c", "printf '\(Self.readyLine)\\n'"]
        )
        XCTAssertEqual(s.readiness["UDID-13"]?.ready, true)

        // The stock phone: the CLI says no, and it must not be able to overwrite
        // — or be overwritten by — the jailbroken one's answer.
        await s.checkReadiness(
            udid: "UDID-15", argsOverride: ["-c", "printf '\(Self.blockedLine)\\n'"]
        )
        XCTAssertEqual(s.readiness["UDID-15"]?.ready, false)
        XCTAssertEqual(
            s.readiness["UDID-13"]?.ready, true,
            "the second phone's probe answered for the first"
        )
    }

    /// A probe that fails clears *that phone's* answer rather than leaving the
    /// last good one standing as though it had just been confirmed — and
    /// leaves the other phone's alone.
    func test_a_failed_probe_clears_only_its_own_phone() async {
        let s = DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        await s.checkReadiness(
            udid: "UDID-13", argsOverride: ["-c", "printf '\(Self.readyLine)\\n'"]
        )
        await s.checkReadiness(
            udid: "UDID-15", argsOverride: ["-c", "printf '\(Self.blockedLine)\\n'"]
        )

        await s.checkReadiness(udid: "UDID-15", argsOverride: ["-c", "exit 1"])

        XCTAssertNil(s.readiness["UDID-15"])
        XCTAssertEqual(s.readiness["UDID-13"]?.ready, true)
    }
}

/// Verbose: the backend's own output, and where it goes.
@MainActor
final class DecryptVerboseTests: XCTestCase {

    func test_verbose_is_opt_in() {
        XCTAssertFalse(
            DeviceAppsStore.decryptArgs(
                bundleId: nil, device: "U", extensions: .automatic
            ).contains("--verbose"),
            "a wall of per-file progress should not be the default"
        )
    }

    func test_verbose_reaches_the_cli() {
        let args = DeviceAppsStore.decryptArgs(
            bundleId: "com.x", device: "U", extensions: .automatic,
            backend: "bagbak", verbose: true
        )
        XCTAssertEqual(args, ["--json", "--decrypt", "com.x", "--device", "U",
                              "--decrypt-backend", "bagbak", "--verbose"])
    }

    func test_log_lines_land_in_the_transcript() async {
        /// The store reads `log` events off the stream, so the panel fills as
        /// the dump talks rather than after it finishes — which is the whole
        /// reason for the flag.
        let script = #"""
        printf '{"type":"log","source":"bagbak","line":"pulling app bundle"}\n{"type":"log","source":"bagbak","line":"downloaded 12 files"}\n'
        """#
        let sut = DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        _ = await sut.decryptFrontmost(device: "U", argsOverride: ["-c", script])
        XCTAssertEqual(sut.transcript, ["pulling app bundle", "downloaded 12 files"])
    }

    func test_a_new_dump_clears_the_previous_transcript() async {
        /// Two runs' output stacked together reads as one long confusing run.
        let first = #"printf '{"type":"log","source":"bagbak","line":"run one"}\n'"#
        let second = #"printf '{"type":"log","source":"bagbak","line":"run two"}\n'"#
        let sut = DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        _ = await sut.decryptFrontmost(device: "U", argsOverride: ["-c", first])
        _ = await sut.decryptFrontmost(device: "U", argsOverride: ["-c", second])
        XCTAssertEqual(sut.transcript, ["run two"])
    }

    func test_the_transcript_survives_a_failure() async {
        /// A failed dump is exactly when someone wants to read the output, so
        /// it must not be cleared on the way out.
        let script = #"""
        printf '{"type":"log","source":"bagbak","line":"spawned app pid => 16280"}\n' ; exit 1
        """#
        let sut = DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        _ = await sut.decryptFrontmost(device: "U", argsOverride: ["-c", script])
        XCTAssertNotNil(sut.lastError)
        XCTAssertEqual(sut.transcript, ["spawned app pid => 16280"])
    }
}

/// Fixes from the xhigh review of the decrypt work.
@MainActor
final class DecryptReviewFixTests: XCTestCase {

    private func app(_ bundle: String, frontmost: Bool = false) -> DeviceAppRecord {
        DeviceAppRecord(bundleId: bundle, name: bundle, version: "1.0",
                        encrypted: true, frontmost: frontmost)
    }

    private func store() -> DeviceAppsStore {
        DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
    }

    func test_a_retry_after_a_row_dump_targets_that_row() async {
        /// The bug: `retryDroppingExtensions` always re-ran the *frontmost*
        /// dump, so a failed row retried whatever was on the phone's screen and
        /// staged it under its own bundle id.
        let sut = store()
        _ = await sut.decrypt(app("com.alpha.app"), device: "U",
                              argsOverride: ["-c", ":"])
        XCTAssertEqual(sut.lastAttempt?.app?.bundleId, "com.alpha.app")
        _ = await sut.retryDroppingExtensions()
        XCTAssertEqual(sut.lastAttempt?.app?.bundleId, "com.alpha.app",
                       "the retry must dump the app that failed, not the screen")
    }

    func test_a_retry_after_a_frontmost_dump_stays_frontmost() async {
        let sut = store()
        _ = await sut.decryptFrontmost(device: "U", argsOverride: ["-c", ":"])
        _ = await sut.retryDroppingExtensions()
        XCTAssertNil(sut.lastAttempt?.app, "the frontmost route has no named app")
        XCTAssertEqual(sut.lastAttempt?.device, "U")
    }

    func test_a_retry_before_any_dump_does_nothing() async {
        let sut = store()
        let result = await sut.retryDroppingExtensions()
        XCTAssertNil(result, "with no attempt to repeat there is nothing to retry")
    }

    func test_blocking_defaults_to_true_on_an_older_cli() throws {
        /// A CLI predating the field must keep its checks conservative rather
        /// than silently softening a real blocker into advice.
        let line = #"{"type":"decrypt-check","device":"U","device_name":"P","ready":false,"can_dump_from_list":false,"recommended_backend":null,"frontmost_bundle_id":null,"frontmost_name":null,"checks":[{"key":"ssh","label":"SSH","ok":false,"detail":"no","fix":"install it"}],"backends":[]}"#
        guard case let .decryptCheck(r) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected a decrypt-check event")
        }
        XCTAssertTrue(r.checks[0].blocking)
    }

    func test_a_non_blocking_check_is_decoded_as_such() throws {
        /// The regression this guards: dropping `blocking` drew the advisory
        /// "App on screen" line as a hard failure, undoing commit 79f6251.
        let line = #"{"type":"decrypt-check","device":"U","device_name":"P","ready":false,"can_dump_from_list":true,"recommended_backend":"bagbak","frontmost_bundle_id":null,"frontmost_name":null,"checks":[{"key":"foreground","label":"App on screen","ok":false,"detail":"nothing on screen","fix":"open one","blocking":false}],"backends":[{"name":"bagbak","usable":true,"reason":null,"note":null}]}"#
        guard case let .decryptCheck(r) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected a decrypt-check event")
        }
        XCTAssertFalse(r.checks[0].blocking)
        XCTAssertTrue(r.canDumpFromList, "a backend can still launch the app itself")
    }
}

extension DecryptReviewFixTests {

    func test_an_older_cli_still_renders_its_checks() throws {
        /// Skew is real here — the app and the pipx CLI are deployed
        /// separately. A missing `can_dump_from_list` used to throw, which
        /// blanked the whole card rather than losing one row.
        let line = #"{"type":"decrypt-check","device":"U","device_name":"P","ready":false,"recommended_backend":null,"frontmost_bundle_id":null,"frontmost_name":null,"checks":[{"key":"ssh","label":"SSH login","ok":false,"detail":"no","fix":"install it"}],"backends":[]}"#
        guard case let .decryptCheck(r) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected a decrypt-check event")
        }
        XCTAssertEqual(r.checks.count, 1, "the checks that did arrive still render")
        XCTAssertFalse(r.canDumpFromList, "never claim a route the CLI did not offer")
    }
}

/// The CLI's own narration, kept apart from the backend's output.
///
/// A dump now changes tack on its own — different tool, dropped extensions —
/// and a minute of silence followed by a different tool's output is how
/// automatic behaviour becomes inexplicable behaviour.
@MainActor
final class DecryptNoteTests: XCTestCase {

    private func store() -> DeviceAppsStore {
        DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
    }

    private func script(_ lines: [String]) -> [String] {
        ["-c", lines.map { "echo '\($0)'" }.joined(separator: "; ")]
    }

    func test_the_clis_own_lines_are_kept_out_of_the_backend_transcript() async {
        let sut = store()
        _ = await sut.decryptFrontmost(device: nil, argsOverride: script([
            #"{"type":"log","source":"graft","line":"the main binary came back still encrypted — trying bagbak"}"#,
            #"{"type":"log","source":"bagbak","line":"downloaded 12 files"}"#,
        ]))
        XCTAssertEqual(sut.notes.count, 1)
        XCTAssertEqual(sut.transcript, ["downloaded 12 files"])
    }

    func test_a_note_survives_intact() async {
        /// The transcript truncates from the middle at one line, which would
        /// eat the half of the sentence that says what was done about it.
        let sut = store()
        let sentence = "5 app extensions could not be decrypted, and no tool can "
            + "decrypt them — dropping them and keeping the rest"
        _ = await sut.decryptFrontmost(device: nil, argsOverride: script([
            #"{"type":"log","source":"graft","line":"\#(sentence)"}"#,
        ]))
        XCTAssertEqual(sut.notes.first, sentence)
    }

    func test_a_new_dump_starts_with_a_clean_slate() async {
        let sut = store()
        _ = await sut.decryptFrontmost(device: nil, argsOverride: script([
            #"{"type":"log","source":"graft","line":"trying bagbak"}"#,
        ]))
        XCTAssertFalse(sut.notes.isEmpty)
        _ = await sut.decryptFrontmost(device: nil, argsOverride: ["-c", ":"])
        XCTAssertTrue(sut.notes.isEmpty, "a previous run's reasoning must not explain this one")
    }
}

/// What a dump admits it gave up.
///
/// Dropping undumpable extensions is automatic now, so the report is the only
/// thing standing between the user and an app quietly missing its widgets.
final class DecryptStrippedReportTests: XCTestCase {

    private func decode(_ line: String) throws -> DecryptEndRecord {
        guard case let .decryptEnd(r) = try BuildEvent.decode(line: line) else {
            throw XCTSkip("expected a decrypt-end event")
        }
        return r
    }

    func test_what_was_dropped_is_decoded() throws {
        let line = #"{"type":"decrypt-end","bundle_id":"com.alpha.app","version":"27.11.8","path":"/x.ipa","binaries":123,"stripped":["Payload/Alpha.app/PlugIns/QuickLaunchWidgets.appex"]}"#
        XCTAssertEqual(try decode(line).stripped.count, 1)
    }

    func test_a_complete_dump_reports_nothing_dropped() throws {
        let line = #"{"type":"decrypt-end","bundle_id":"com.x","version":"1.0","path":"/x.ipa","binaries":3,"stripped":[]}"#
        XCTAssertTrue(try decode(line).stripped.isEmpty)
    }

    func test_an_older_cli_is_read_as_having_stripped_nothing() throws {
        /// The safe reading: that CLI never stripped without being asked.
        let line = #"{"type":"decrypt-end","bundle_id":"com.x","version":"1.0","path":"/x.ipa","binaries":3}"#
        XCTAssertTrue(try decode(line).stripped.isEmpty)
    }
}

/// Three states, because the third one exists: taking back the CLI's own
/// refusal is a different request from "keep everything".
@MainActor
final class ExtensionPolicyTests: XCTestCase {

    private func args(_ policy: DeviceAppsStore.ExtensionPolicy) -> [String] {
        DeviceAppsStore.decryptArgs(bundleId: "com.x", device: "U", extensions: policy)
    }

    func test_the_default_sends_nothing() {
        let a = args(.automatic)
        XCTAssertFalse(a.contains(CLIContract.Flag.stripEncryptedExtensions))
        XCTAssertFalse(a.contains(CLIContract.Flag.keepEncryptedExtensions))
    }

    func test_forcing_says_strip_rather_than_unsaying_keep() {
        /// A dump refuses on its own when the extension *is* the app, so
        /// clearing "keep" is not enough — the refusal would just happen again.
        XCTAssertTrue(args(.force).contains(CLIContract.Flag.stripEncryptedExtensions))
        XCTAssertFalse(args(.force).contains(CLIContract.Flag.keepEncryptedExtensions))
    }

    func test_keeping_refuses_the_partial_app() {
        XCTAssertTrue(args(.keep).contains(CLIContract.Flag.keepEncryptedExtensions))
    }
}

@MainActor
final class DroppingRetryTests: XCTestCase {

    private func store() -> DeviceAppsStore {
        DeviceAppsStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
    }

    func test_both_refusals_offer_the_same_recovery() {
        let sut = store()
        sut.lastError = "you asked to keep them — without `--keep-encrypted-extensions` …"
        XCTAssertTrue(sut.canRetryDroppingExtensions)

        sut.lastError = "the app is a shell without them — `--strip-encrypted-extensions` "
            + "removes them anyway"
        XCTAssertTrue(sut.canRetryDroppingExtensions,
                      "the CLI's own refusal is undone by the same button")
    }

    func test_an_unrelated_failure_offers_nothing() {
        let sut = store()
        sut.lastError = "couldn't reach frida-server on the device"
        XCTAssertFalse(sut.canRetryDroppingExtensions)
    }

    func test_the_retry_forces_rather_than_repeating_the_default() async {
        /// Retrying with the default would hit the same refusal and look like
        /// a button that does nothing.
        let sut = store()
        _ = await sut.decryptFrontmost(device: "U", argsOverride: ["-c", ":"])
        let args = DeviceAppsStore.decryptArgs(bundleId: nil, device: "U", extensions: .force)
        XCTAssertTrue(args.contains(CLIContract.Flag.stripEncryptedExtensions))
    }
}
