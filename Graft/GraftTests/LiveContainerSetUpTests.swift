import SwiftUI
import XCTest
@testable import Graft

/// A stand-in `graft` for the `--lc-*` verbs. It appends each argv to
/// `argv.log` and answers each verb as the real one does:
/// - `--lc-status`: one `lc-status` for the iPad, with `certificate_pushed`
///   true while `cert.mark` exists (standing in for the build log's record),
///   the primary listed unless `no-primary` exists, managed by AltStore
///   while `altstore` exists, and held until `hold-status` is gone;
/// - `--lc-install`: with `fail-check`, fails before any progress, as the
///   registered-device check does (Plan E); otherwise `fetch` progress, then
///   with `hang-install` it waits, with `fail-install` it fails, and
///   otherwise it installs the primary;
/// - `--lc-push-cert`: with `fail-push`, fails; otherwise makes `cert.mark`;
/// - `--lc-clean`: removes `cert.mark`;
/// - `--settings`: with `no-key`, settings whose `app_store_connect` is null
///   (no key); otherwise nothing, so the sheet knows nothing of the key and
///   disables nothing;
/// - `--device-info`: Developer Mode off with `devmode-off`, on with
///   `devmode-on`, otherwise null (couldn't tell).
struct FakeLiveContainerCLI {
    static let primary = "com.kdt.livecontainer.ABCDE12345"
    let dir: URL

    var runner: CLIRunner { CLIRunner(executable: dir.appending(path: "graft")) }

    init(for test: XCTestCase, udid: String, freeSteps: [String] = []) throws {
        dir = FileManager.default.temporaryDirectory.appending(path: "lc_setup_\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let made = dir
        test.addTeardownBlock { try? FileManager.default.removeItem(at: made) }
        let steps = freeSteps.map { "\"\($0)\"" }.joined(separator: ",")
        let p = Self.primary
        let script = """
        #!/bin/sh
        D='\(dir.path)'
        echo "$*" >> "$D/argv.log"
        case "$*" in
        *--lc-status*)
          while [ -f "$D/hold-status" ]; do sleep 0.05; done
          if [ -f "$D/cert.mark" ]; then P=true; else P=false; fi
          I='[{"bundle_id":"\(p)","version":"3.8.14","role":"primary","wildcard_signed":false}]'
          if [ -f "$D/no-primary" ]; then I='[]'; fi
          M=graft
          if [ -f "$D/altstore" ]; then M=altstore; fi
          printf '%s\\n' '{"type":"lc-status","udid":"\(udid)","reachable":true,"instances":'"$I"',"managed_by":"'"$M"'","latest":{"version":"3.8.14","channel":"nightly"},"update_available":false,"sources":{},"certificate_pushed":'"$P"',"free_setup_steps":[\(steps)]}'
          ;;
        *--lc-install*)
          if [ -f "$D/fail-check" ]; then
            echo "error: Example iPad isn't registered in your developer account: add it at developer.apple.com, then try again" >&2
            exit 7
          fi
          printf '%s\\n' '{"type":"lc-install-progress","udid":"\(udid)","instance":null,"step":"fetch","detail":"LiveContainer (nightly)"}'
          if [ -f "$D/hang-install" ]; then exec sleep 30; fi
          if [ -f "$D/fail-install" ]; then
            echo "error: installing \(p) failed" >&2
            exit 8
          fi
          printf '%s\\n' '{"type":"lc-installed","udid":"\(udid)","bundle_id":"\(p)","instance":1,"version":"3.8.14","channel":"nightly"}'
          ;;
        *--lc-push-cert*)
          if [ -f "$D/fail-push" ]; then
            echo "error: \(p) already has a Documents/AppleDevelopment.p12 that Graft did not push; not overwriting it" >&2
            exit 21
          fi
          touch "$D/cert.mark"
          printf '%s\\n' '{"type":"lc-cert-pushed","udid":"\(udid)","bundle_id":"\(p)","path":"Documents/AppleDevelopment.p12"}'
          ;;
        *--lc-clean*)
          rm -f "$D/cert.mark"
          printf '%s\\n' '{"type":"lc-cleaned","udid":"\(udid)","bundle_id":"\(p)","removed":["AppleDevelopment.p12"],"kept":0}'
          ;;
        *--settings*)
          if [ -f "$D/no-key" ]; then
            printf '%s\\n' '{"type":"settings","default_device_udid":null,"archive_keep":2,"signing_cert":"/c","signing_profile":"/p","signing_team_id":"ABCDE12345","app_store_connect":null}'
          fi
          ;;
        *--device-info*)
          DM=null
          if [ -f "$D/devmode-off" ]; then DM=false; fi
          if [ -f "$D/devmode-on" ]; then DM=true; fi
          printf '%s\\n' '{"type":"device-info","udid":"\(udid)","reachable":true,"total_bytes":null,"free_bytes":null,"battery_percent":null,"battery_charging":null,"developer_mode":'"$DM"'}'
          ;;
        esac
        """
        let file = dir.appending(path: "graft")
        try script.write(to: file, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: file.path)
    }

    /// Turn a marker on or off: `fail-check`, `fail-install`, `hang-install`,
    /// `fail-push`, `no-primary`, `altstore`, `hold-status`, `no-key`,
    /// `devmode-off`, `devmode-on`, or `cert.mark` (the certificate's record).
    func set(_ marker: String, _ on: Bool = true) throws {
        let file = dir.appending(path: marker)
        if on {
            try Data().write(to: file)
        } else {
            try? FileManager.default.removeItem(at: file)
        }
    }

    /// Every argv the CLI was run with, in order.
    var argv: [String] {
        ((try? String(contentsOf: dir.appending(path: "argv.log"), encoding: .utf8)) ?? "")
            .split(separator: "\n").map(String.init)
    }

    /// Each run's verb (`--lc-install`, …), in order: the word after `--json`.
    var verbs: [String] {
        argv.map { line in line.split(separator: " ").dropFirst().first.map(String.init) ?? "" }
    }
}

/// Set Up LiveContainer (spec §5c): what `--lc-status` says about the
/// certificate and a free Apple ID's steps, the store's Set Up run, the
/// sheet's words and both accounts' variants, the device page's reminder, and
/// the two ways into the sheet.
///
/// The CLI is `FakeLiveContainerCLI`. Hosted views skip when macOS isn't
/// serving the accessibility tree.
@MainActor
final class LiveContainerSetUpTests: XCTestCase {
    static let udid = "00008030-0000000000000002"

    /// One `lc-status` for the iPad, with `tail` after `sources`.
    func status(_ tail: String) -> LCStatusRecord? {
        let line = #"{"type":"lc-status","udid":"\#(Self.udid)","reachable":true,"instances":[],"managed_by":"unknown","latest":null,"update_available":false,"sources":{}\#(tail)}"#
        if case let .lcStatus(record)? = line.buildEvents().first { return record }
        return nil
    }

    /// The iPad, as `--devices` lists it.
    func deviceRecord() -> DeviceRecord? {
        let line = #"{"type":"device","udid":"\#(Self.udid)","name":"Example iPad","model":"iPad13,16","ios_version":"26.1","connection_type":"USB","livecontainer_bundle_id":null,"is_default":false}"#
        if case let .device(record)? = line.buildEvents().first { return record }
        return nil
    }

    // MARK: - What --lc-status says

    func test_status_says_whether_a_certificate_of_grafts_is_on_the_device() throws {
        XCTAssertTrue(try XCTUnwrap(status(#","certificate_pushed":true"#)).certificatePushed)
        XCTAssertFalse(try XCTUnwrap(status(#","certificate_pushed":false"#)).certificatePushed)
        XCTAssertFalse(try XCTUnwrap(status("")).certificatePushed, "an older CLI says nothing: no reminder")
    }

    func test_status_carries_a_free_apple_ids_device_steps() throws {
        let steps = ["Install SideStore (https://example.invalid/sidestore).", "Install LiveContainer."]
        let tail = #","free_setup_steps":["Install SideStore (https://example.invalid/sidestore).","Install LiveContainer."]"#
        XCTAssertEqual(try XCTUnwrap(status(tail)).freeSetupSteps, steps)
        XCTAssertEqual(try XCTUnwrap(status("")).freeSetupSteps, [])
    }

    /// The premise every test below stands on.
    /// The checkbox is selected, in American English, not ticked (final
    /// review part 1, #4), as the README and the CLI's note say it.
    func test_the_certificate_checkbox_note_says_selecting() {
        XCTAssertEqual(SetUpLiveContainerSheet.tickNote(name: "Example iPad"),
                       "Selecting it takes the certificate off Example iPad at once: LiveContainer's apps can "
                           + "read its Documents folder.")
    }

    func test_the_stand_in_cli_answers_as_the_real_one() async throws {
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        let store = LiveContainerStore(runner: cli.runner)

        await store.load(udid: Self.udid)
        XCTAssertEqual(store.status[Self.udid]?.certificatePushed, false)
        try cli.set("cert.mark")
        await store.load(udid: Self.udid)

        XCTAssertEqual(store.status[Self.udid]?.certificatePushed, true)
        XCTAssertEqual(cli.verbs, ["--lc-status", "--lc-status"])
    }

    // MARK: - The store's Set Up

    func test_set_up_installs_then_puts_the_certificate_on() async throws {
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        let store = LiveContainerStore(runner: cli.runner)

        await store.runSetUp(udid: Self.udid, instances: [1], channel: "nightly")

        XCTAssertEqual(cli.verbs, ["--lc-install", "--lc-status", "--lc-push-cert", "--lc-status"],
                       "install, then the certificate, each followed by a fresh status")
        XCTAssertEqual(cli.argv.first,
                       "--json --lc-install --device \(Self.udid) --instances 1 --channel nightly")
        let setUp = try XCTUnwrap(store.setUp)
        XCTAssertEqual(setUp.states, [.check: .done, .install: .done, .pushCert: .done])
        XCTAssertEqual(setUp.left, [])
        XCTAssertTrue(setUp.finished)
        XCTAssertEqual(store.status[Self.udid]?.certificatePushed, true, "the reminder's fact, from the CLI")
        XCTAssertFalse(store.isBusy)
    }

    /// Plan E's refresh found the device isn't registered: `--lc-install`
    /// stops before any progress, which is Set Up's first step.
    func test_a_device_the_account_doesnt_have_stops_set_up_at_the_check() async throws {
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        try cli.set("fail-check")
        let store = LiveContainerStore(runner: cli.runner)

        await store.runSetUp(udid: Self.udid, instances: [1], channel: nil)

        XCTAssertEqual(cli.verbs, ["--lc-install", "--lc-status"], "the certificate step never ran")
        let setUp = try XCTUnwrap(store.setUp)
        guard case let .failed(message)? = setUp.states[.check] else {
            return XCTFail("the check should have failed: \(setUp.states)")
        }
        XCTAssertTrue(message.contains("isn't registered in your developer account"), message)
        XCTAssertEqual(setUp.states[.install], .notRun)
        XCTAssertEqual(setUp.states[.pushCert], .notRun)
        XCTAssertEqual(setUp.left, [.check, .install, .pushCert])
    }

    func test_a_failed_install_stops_set_up_after_the_check() async throws {
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        try cli.set("fail-install")
        let store = LiveContainerStore(runner: cli.runner)

        await store.runSetUp(udid: Self.udid, instances: [1], channel: nil)

        XCTAssertEqual(cli.verbs, ["--lc-install", "--lc-status"])
        let setUp = try XCTUnwrap(store.setUp)
        XCTAssertEqual(setUp.states[.check], .done, "it got past the check to the download")
        guard case let .failed(message)? = setUp.states[.install] else {
            return XCTFail("the install should have failed: \(setUp.states)")
        }
        XCTAssertTrue(message.contains("installing"), message)
        XCTAssertEqual(setUp.states[.pushCert], .notRun)
        XCTAssertEqual(setUp.left, [.install, .pushCert])
    }

    func test_a_failed_certificate_push_leaves_only_the_push() async throws {
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        try cli.set("fail-push")
        let store = LiveContainerStore(runner: cli.runner)

        await store.runSetUp(udid: Self.udid, instances: [1], channel: nil)

        XCTAssertEqual(cli.verbs, ["--lc-install", "--lc-status", "--lc-push-cert", "--lc-status"])
        let setUp = try XCTUnwrap(store.setUp)
        XCTAssertEqual(setUp.states[.check], .done)
        XCTAssertEqual(setUp.states[.install], .done)
        guard case let .failed(message)? = setUp.states[.pushCert] else {
            return XCTFail("the push should have failed: \(setUp.states)")
        }
        XCTAssertTrue(message.contains("not overwriting it"), message)
        XCTAssertEqual(setUp.left, [.pushCert])
        XCTAssertTrue(setUp.resumesAtCertificate)
        XCTAssertTrue(store.resumesAtCertificate(udid: Self.udid, instances: [1], channel: nil))
        XCTAssertEqual(store.status[Self.udid]?.certificatePushed, false)
    }

    func test_set_up_again_after_a_failed_push_puts_the_certificate_on_only() async throws {
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        try cli.set("fail-push")
        let store = LiveContainerStore(runner: cli.runner)
        await store.runSetUp(udid: Self.udid, instances: [1], channel: nil)
        try cli.set("fail-push", false)

        await store.runSetUp(udid: Self.udid, instances: [1], channel: nil)

        XCTAssertEqual(Array(cli.verbs.dropFirst(4)), ["--lc-push-cert", "--lc-status"], "no second install")
        XCTAssertEqual(store.setUp?.states, [.check: .done, .install: .done, .pushCert: .done])
        XCTAssertEqual(store.status[Self.udid]?.certificatePushed, true)
    }

    // MARK: When Set Up again starts over (review amendment 3)

    /// After a failed push, `runSetUp` again for the same udid, instances
    /// and channel; true when it went straight to the certificate.
    private func resumed(
        _ cli: FakeLiveContainerCLI, _ store: LiveContainerStore,
        udid: String = LiveContainerSetUpTests.udid, instances: Set<Int> = [1], channel: String? = nil,
        between: () async throws -> Void = {}
    ) async throws -> Bool {
        try cli.set("fail-push")
        await store.runSetUp(udid: Self.udid, instances: [1], channel: nil)
        XCTAssertTrue(store.setUp?.resumesAtCertificate == true, "premise: the push failed after the install")
        try await between()
        try cli.set("fail-push", false)
        let before = cli.verbs.count
        await store.runSetUp(udid: udid, instances: instances, channel: channel)
        return !cli.verbs.dropFirst(before).contains("--lc-install")
    }

    /// The premise for the guards below: unchanged, it resumes.
    func test_set_up_again_unchanged_resumes() async throws {
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        let resumed = try await resumed(cli, LiveContainerStore(runner: cli.runner))
        XCTAssertTrue(resumed)
    }

    func test_set_up_on_another_device_runs_in_full() async throws {
        let other = "00008030-00000000000000FF"
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        let store = LiveContainerStore(runner: cli.runner)
        let resumed = try await resumed(cli, store, udid: other, between: {
            await store.load(udid: other)
            XCTAssertEqual(store.status[other]?.hasPrimary, true, "premise: it has a primary too")
        })
        XCTAssertFalse(resumed, "the install that's done is on the other device")
    }

    func test_changed_choices_mean_a_full_run() async throws {
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        let more = try await resumed(cli, LiveContainerStore(runner: cli.runner), instances: [1, 2])
        XCTAssertFalse(more, "another LiveContainer to install")
        let other = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        let stable = try await resumed(other, LiveContainerStore(runner: other.runner), channel: "stable")
        XCTAssertFalse(stable, "another channel to install from")
    }

    /// The LiveContainer it installed was deleted since: the push has
    /// nowhere to go until it's installed again.
    func test_set_up_resumes_only_while_the_primary_is_still_there() async throws {
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        let store = LiveContainerStore(runner: cli.runner)
        let resumed = try await resumed(cli, store, between: {
            try cli.set("no-primary")
            await store.load(udid: Self.udid)
            XCTAssertEqual(store.status[Self.udid]?.instances, [], "premise")
        })
        XCTAssertFalse(resumed)
    }

    /// The last run is no longer the failed push: a removal ran since.
    func test_set_up_resumes_only_straight_after_its_failed_push() async throws {
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        let store = LiveContainerStore(runner: cli.runner)
        let resumed = try await resumed(cli, store, between: {
            await store.removeCert(udid: Self.udid)
            XCTAssertNotNil(store.setUp, "premise: a removal (the tick) doesn't end Set Up")
        })
        XCTAssertFalse(resumed)
    }

    /// Install, Put certificate and Clean up, one step at a time, end Set Up:
    /// its rows go, and the next Set Up runs in full.
    func test_a_one_step_action_ends_set_up() async throws {
        for start in [
            { (s: LiveContainerStore) in s.startInstall(udid: Self.udid, instances: [1], channel: nil) },
            { (s: LiveContainerStore) in s.startPushCert(udid: Self.udid) },
            { (s: LiveContainerStore) in s.startClean(udid: Self.udid) },
        ] {
            let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
            let store = LiveContainerStore(runner: cli.runner)
            let resumed = try await resumed(cli, store, between: {
                try cli.set("fail-push", false)
                start(store)
                await store.currentAction?.value
                XCTAssertNil(store.setUp)
            })
            XCTAssertFalse(resumed)
        }
    }

    /// A resumed push that fails again doesn't make every later Set Up skip
    /// the install: the next one starts over, from the check.
    func test_a_resumed_push_that_fails_again_starts_the_next_one_over() async throws {
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        let store = LiveContainerStore(runner: cli.runner)
        try cli.set("fail-push")
        await store.runSetUp(udid: Self.udid, instances: [1], channel: nil)
        await store.runSetUp(udid: Self.udid, instances: [1], channel: nil)
        XCTAssertEqual(Array(cli.verbs.dropFirst(4)), ["--lc-push-cert", "--lc-status"], "premise: it resumed")
        XCTAssertEqual(store.setUp?.left, [.pushCert])

        XCTAssertEqual(store.setUp?.resumesAtCertificate, false)
        let before = cli.verbs.count
        await store.runSetUp(udid: Self.udid, instances: [1], channel: nil)
        XCTAssertEqual(cli.verbs.dropFirst(before).first, "--lc-install")
    }

    func test_stop_during_the_install_ends_set_up_there() async throws {
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        try cli.set("hang-install")
        let store = LiveContainerStore(runner: cli.runner)

        store.startSetUp(udid: Self.udid, instances: [1], channel: nil)
        let deadline = Date().addingTimeInterval(10)
        while store.run(for: Self.udid)?.progress.isEmpty ?? true, Date() < deadline {
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        XCTAssertTrue(store.isBusy, "premise: the install is running")
        let action = store.currentAction
        store.stop()
        await action?.value

        let setUp = try XCTUnwrap(store.setUp)
        XCTAssertEqual(setUp.states[.check], .done)
        XCTAssertEqual(setUp.states[.install], .stopped)
        XCTAssertEqual(setUp.states[.pushCert], .notRun)
        XCTAssertTrue(setUp.stopped)
        XCTAssertFalse(cli.verbs.contains("--lc-push-cert"))
        XCTAssertFalse(store.isBusy)
    }

    /// The reminder follows a removal or a clean up at once.
    func test_a_removal_or_clean_up_reads_the_status_again() async throws {
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        try cli.set("cert.mark")
        let store = LiveContainerStore(runner: cli.runner)
        await store.load(udid: Self.udid)
        XCTAssertEqual(store.status[Self.udid]?.certificatePushed, true, "premise")

        await store.removeCert(udid: Self.udid)
        XCTAssertEqual(Array(cli.verbs.suffix(2)), ["--lc-clean", "--lc-status"])
        XCTAssertEqual(store.status[Self.udid]?.certificatePushed, false)

        try cli.set("cert.mark")
        await store.load(udid: Self.udid)
        await store.clean(udid: Self.udid)
        XCTAssertEqual(Array(cli.verbs.suffix(2)), ["--lc-clean", "--lc-status"])
        XCTAssertEqual(store.status[Self.udid]?.certificatePushed, false)
    }

    /// Between Set Up's two runs (here, the status read after the install)
    /// nothing runs, and still every button that starts one waits, here and
    /// on another device's page.
    func test_set_up_is_busy_between_its_runs_and_says_where() async throws {
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        try cli.set("hold-status")
        let store = LiveContainerStore(runner: cli.runner)

        store.startSetUp(udid: Self.udid, instances: [1], channel: nil)
        let deadline = Date().addingTimeInterval(10)
        while !cli.verbs.contains("--lc-status"), Date() < deadline {
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        try await Task.sleep(nanoseconds: 200_000_000)

        XCTAssertEqual(cli.verbs, ["--lc-install", "--lc-status"], "premise: between the two runs")
        XCTAssertFalse(store.isRunning, "premise: the install's run is over")
        XCTAssertTrue(store.isBusy)
        XCTAssertEqual(store.busyElsewhere(than: "00008030-00000000000000FF"), Self.udid)
        XCTAssertNil(store.busyElsewhere(than: Self.udid))
        try cli.set("hold-status", false)
        await store.currentAction?.value
        XCTAssertFalse(store.isBusy)
    }

    // MARK: - The sheet's words and variants

    /// One `lc-status` for the iPad, reachable or not, with or without the
    /// primary, managed by `managedBy`.
    func lcStatus(
        managedBy: String = "unknown", reachable: Bool = true, primary: Bool = true
    ) -> LCStatusRecord? {
        let list = primary
            ? #"[{"bundle_id":"\#(FakeLiveContainerCLI.primary)","version":"3.8.14","role":"primary","wildcard_signed":false}]"#
            : "[]"
        let error = reachable ? "" : #","error":"Device not found""#
        let line = #"{"type":"lc-status","udid":"\#(Self.udid)","reachable":\#(reachable)\#(error),"instances":\#(list),"managed_by":"\#(managedBy)","latest":null,"update_available":false,"sources":{}}"#
        if case let .lcStatus(record)? = line.buildEvents().first { return record }
        return nil
    }

    func test_only_a_paid_account_gets_the_set_up_button() {
        XCTAssertTrue(SetUpLiveContainerSheet.showsSetUp(account: .paid))
        XCTAssertTrue(SetUpLiveContainerSheet.showsSetUp(account: nil), "not known yet disables nothing")
        XCTAssertFalse(SetUpLiveContainerSheet.showsSetUp(account: .free))
    }

    /// The password is the person's, and Settings › Set .p12 password keeps
    /// it in the keychain: "Graft never asks for it" wasn't so.
    func test_the_paid_device_steps_name_the_device_and_leave_the_password_to_its_owner() {
        let steps = SetUpLiveContainerSheet.deviceSteps(account: .paid, kind: "iPad")
        XCTAssertEqual(steps.map(\.removesCertificate), [true, false, false, false])
        XCTAssertTrue(steps[0].text.contains("On My iPad › LiveContainer"), steps[0].text)
        // A model Graft can't name is a "device" (`BuildTarget.deviceKind`);
        // the Files app has no "On My device".
        let unnamed = SetUpLiveContainerSheet.deviceSteps(account: .paid, kind: "device")
        XCTAssertTrue(unnamed[0].text.contains("(On My iPhone or iPad › LiveContainer)"), unnamed[0].text)
        // An iPod touch's folder is "On My iPod", as the Files app names it.
        let pod = SetUpLiveContainerSheet.deviceSteps(account: .paid, kind: BuildTarget.deviceKind(model: "iPod9,1"))
        XCTAssertTrue(pod[0].text.contains("(On My iPod › LiveContainer)"), pod[0].text)
        XCTAssertTrue(steps[0].text.contains("LiveContainer asks for it on the device; Graft can't type it for you."),
                      steps[0].text)
        XCTAssertFalse(steps[0].text.contains("never asks"), steps[0].text)
        XCTAssertTrue(steps[0].text.hasSuffix("On iOS 26, apps won't open until it's done."), steps[0].text)
        XCTAssertTrue(steps[1].text.contains("JIT-Less Diagnose"), steps[1].text)
        XCTAssertEqual(steps[2].text, "Import your apps from the Documents folder.")
        XCTAssertEqual(steps[3].text, "Long-press each app › Convert to Shared App.")
        XCTAssertEqual(SetUpLiveContainerSheet.checklistTitle(name: "Example iPad"),
                       "Then, in LiveContainer on Example iPad:")
    }

    func test_the_free_device_steps_have_no_certificate_of_grafts() {
        let steps = SetUpLiveContainerSheet.deviceSteps(account: .free, kind: "iPad")
        XCTAssertFalse(steps.contains(where: \.removesCertificate))
        XCTAssertFalse(steps.contains { $0.text.contains("AppleDevelopment.p12") })
        XCTAssertEqual(steps.map(\.text), Array(SetUpLiveContainerSheet.deviceSteps(account: .paid, kind: "iPad")
            .dropFirst().map(\.text)), "the same device steps, without Graft's certificate")
    }

    /// A paid account's phone where SideStore installed LiveContainer: its
    /// certificate comes from SideStore, which on iOS 26 nothing works
    /// without, and Graft put none there to tick off.
    func test_a_paid_sidestore_phone_imports_the_certificate_from_sidestore() {
        let steps = SetUpLiveContainerSheet.deviceSteps(account: .paid, kind: "iPad", managedBy: .altstore)
        XCTAssertFalse(steps.contains(where: \.removesCertificate))
        XCTAssertTrue(steps[0].text.hasPrefix("Settings › Import Certificate from SideStore"), steps[0].text)
        XCTAssertTrue(steps[0].text.contains("On iOS 26, apps won't open until it's done."), steps[0].text)
        XCTAssertFalse(steps.contains { $0.text.contains("AppleDevelopment.p12") })
        XCTAssertEqual(steps.dropFirst().map(\.text), Array(SetUpLiveContainerSheet.deviceSteps(
            account: .paid, kind: "iPad").dropFirst().map(\.text)))
    }

    /// With no steps from the CLI (an older one, or the device not read
    /// yet), the free side still says the step iOS 26 needs.
    func test_the_free_fallback_still_says_to_import_sidestores_certificate() {
        let fallback = SetUpLiveContainerSheet.freeFallback
        XCTAssertTrue(fallback[0].contains("SideStore or iloader"), fallback[0])
        XCTAssertTrue(fallback.contains { $0.contains("Import Certificate from SideStore") && $0.contains("iOS 26") },
                      "\(fallback)")
    }

    /// Not read yet, unreachable, SideStore's or AltStore's, busy, or nothing
    /// to install: no Set Up. A resume installs nothing, so it needs no
    /// LiveContainer ticked.
    func test_set_up_waits_for_a_device_graft_can_install_on() throws {
        typealias Sheet = SetUpLiveContainerSheet
        let ok = try XCTUnwrap(lcStatus())
        XCTAssertTrue(Sheet.setUpEnabled(status: ok, busy: false, instances: [1], resumes: false))
        XCTAssertFalse(Sheet.setUpEnabled(status: nil, busy: false, instances: [1], resumes: false), "not read")
        XCTAssertFalse(Sheet.setUpEnabled(status: try XCTUnwrap(lcStatus(reachable: false)), busy: false,
                                          instances: [1], resumes: false), "unreachable")
        XCTAssertFalse(Sheet.setUpEnabled(status: try XCTUnwrap(lcStatus(managedBy: "altstore", primary: false)),
                                          busy: false, instances: [1], resumes: false),
                       "SideStore is there and LiveContainer isn't: the CLI won't install")
        XCTAssertFalse(Sheet.setUpEnabled(status: try XCTUnwrap(lcStatus(managedBy: "altstore")),
                                          busy: false, instances: [1], resumes: false))
        XCTAssertFalse(Sheet.setUpEnabled(status: ok, busy: true, instances: [1], resumes: false))
        XCTAssertFalse(Sheet.setUpEnabled(status: ok, busy: false, instances: [], resumes: false))
        XCTAssertTrue(Sheet.setUpEnabled(status: ok, busy: false, instances: [], resumes: true))
    }

    /// The old gate: Put certificate on its own only with a primary Graft
    /// can put it in.
    func test_put_certificate_alone_is_offered_only_where_graft_can_put_it() throws {
        typealias Sheet = SetUpLiveContainerSheet
        XCTAssertTrue(Sheet.offersPushCert(status: try XCTUnwrap(lcStatus())))
        XCTAssertFalse(Sheet.offersPushCert(status: try XCTUnwrap(lcStatus(primary: false))))
        XCTAssertFalse(Sheet.offersPushCert(status: try XCTUnwrap(lcStatus(managedBy: "altstore"))))
        XCTAssertFalse(Sheet.offersPushCert(status: try XCTUnwrap(lcStatus(reachable: false))))
        XCTAssertFalse(Sheet.offersPushCert(status: nil))
    }

    func test_each_step_says_where_it_is() {
        typealias Sheet = SetUpLiveContainerSheet
        XCTAssertEqual(Sheet.stepLine(.check, .running, name: "Example iPad"),
                       "Check Example iPad and your developer account…")
        XCTAssertEqual(Sheet.stepLine(.check, .failed("Example iPad isn't registered"), name: "Example iPad"),
                       "Check Example iPad and your developer account: failed")
        XCTAssertEqual(Sheet.stepLine(.install, .done, name: "Example iPad"),
                       "Install LiveContainer from its GitHub releases: done")
        XCTAssertEqual(Sheet.stepLine(.install, .stopped, name: "Example iPad"),
                       "Install LiveContainer from its GitHub releases: stopped")
        XCTAssertEqual(Sheet.stepLine(.pushCert, .notRun, name: "Example iPad"),
                       "Put the certificate on Example iPad: not run")
        XCTAssertEqual(Sheet.stepLine(.pushCert, .waiting, name: "Example iPad"),
                       "Put the certificate on Example iPad")
    }

    /// The check's tooltip says when the registered devices are read
    /// again, and doesn't promise it: a pinned list never is.
    func test_the_check_says_when_it_reads_the_registered_devices_again() {
        let help = SetUpLiveContainerSheet.checkHelp(name: "Example iPad")
        XCTAssertTrue(help.contains("If Example iPad is new to the device list Graft saved, and you "
                                    + "haven't pinned that list"), help)
    }

    func test_a_failure_says_what_is_left_and_where_set_up_picks_up() {
        typealias Sheet = SetUpLiveContainerSheet
        var setUp = LiveContainerStore.SetUp(udid: Self.udid, instances: [1], channel: nil)
        setUp.states = [.check: .failed("x"), .install: .notRun, .pushCert: .notRun]
        setUp.finished = true
        XCTAssertEqual(Sheet.leftLine(setUp, name: "Example iPad"),
                       "Left: check Example iPad, install LiveContainer, put the certificate on "
                       + "Example iPad. Set Up again starts from the check.")

        setUp.states = [.check: .done, .install: .done, .pushCert: .failed("x")]
        XCTAssertEqual(Sheet.leftLine(setUp, name: "Example iPad"),
                       "Left: put the certificate on Example iPad. Set Up again puts it on without "
                       + "installing again.")

        setUp.resumed = true
        XCTAssertEqual(Sheet.leftLine(setUp, name: "Example iPad"),
                       "Left: put the certificate on Example iPad. Set Up again starts from the check.",
                       "a resumed push that failed again starts over")

        setUp.states = [.check: .done, .install: .done, .pushCert: .done]
        XCTAssertNil(Sheet.leftLine(setUp, name: "Example iPad"), "nothing is left")

        setUp.states = [.check: .done, .install: .running, .pushCert: .waiting]
        setUp.finished = false
        XCTAssertNil(Sheet.leftLine(setUp, name: "Example iPad"), "not while it runs")
    }

    /// VoiceOver hears each step as it starts, and how Set Up ended.
    func test_voiceover_hears_each_step_and_the_end() {
        typealias Sheet = SetUpLiveContainerSheet
        var setUp = LiveContainerStore.SetUp(udid: Self.udid, instances: [1], channel: nil)
        XCTAssertNil(Sheet.announcement(setUp, name: "Example iPad"), "nothing has started")
        setUp.states = [.check: .running, .install: .waiting, .pushCert: .waiting]
        XCTAssertEqual(Sheet.announcement(setUp, name: "Example iPad"),
                       "Check Example iPad and your developer account…")
        setUp.states = [.check: .done, .install: .running, .pushCert: .waiting]
        XCTAssertEqual(Sheet.announcement(setUp, name: "Example iPad"),
                       "Install LiveContainer from its GitHub releases…")
        setUp.states = [.check: .done, .install: .done, .pushCert: .done]
        setUp.finished = true
        XCTAssertEqual(Sheet.announcement(setUp, name: "Example iPad"),
                       "Set Up is done: LiveContainer and the certificate are on Example iPad.")
        setUp.states = [.check: .done, .install: .failed("x"), .pushCert: .notRun]
        XCTAssertEqual(Sheet.announcement(setUp, name: "Example iPad"), Sheet.leftLine(setUp, name: "Example iPad"))
    }

    func test_the_tick_shows_what_became_of_the_certificate() {
        typealias Sheet = SetUpLiveContainerSheet
        XCTAssertFalse(Sheet.importTicked(ticked: false, certificatePushed: true, busy: false), "untouched")
        XCTAssertTrue(Sheet.importTicked(ticked: true, certificatePushed: true, busy: true), "coming off")
        XCTAssertTrue(Sheet.importTicked(ticked: true, certificatePushed: false, busy: false), "off")
        XCTAssertFalse(Sheet.importTicked(ticked: true, certificatePushed: true, busy: false),
                       "the removal failed: it can be ticked again")
    }

    /// The tick's action is today's Remove certificate: the certificate only.
    func test_ticking_takes_the_certificate_off_and_nothing_else() async throws {
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        try cli.set("cert.mark")
        let store = LiveContainerStore(runner: cli.runner)

        SetUpLiveContainerSheet.tick(false, store: store, udid: Self.udid)
        XCTAssertNil(store.currentAction, "unticking puts nothing back")

        SetUpLiveContainerSheet.tick(true, store: store, udid: Self.udid)
        await store.currentAction?.value

        XCTAssertEqual(cli.argv.first, "--json --lc-clean --cert-only --device \(Self.udid)",
                       "the certificate only: the IPAs still wait to be imported")
        XCTAssertEqual(cli.verbs, ["--lc-clean", "--lc-status"])
        XCTAssertEqual(store.status[Self.udid]?.certificatePushed, false)
    }

    /// "Busy with LiveContainer on Example iPad", not on its UDID.
    func test_a_busy_device_is_named() throws {
        let store = LiveContainerStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        XCTAssertEqual(store.name(of: Self.udid), Self.udid, "never shown: its UDID is all there is")
        store.remember(try XCTUnwrap(deviceRecord()))
        XCTAssertEqual(store.name(of: Self.udid), "Example iPad")
    }

    // MARK: - New-user dead ends (2.3.1 B6)

    private func settings(_ part: String) throws -> SettingsRecord {
        let line = #"{"type":"settings","default_device_udid":null,"archive_keep":2,"signing_cert":"/c","signing_profile":"/p","signing_team_id":"T"\#(part)}"#
        guard case let .settings(record)? = line.buildEvents().first else { throw XCTSkip("fixture") }
        return record
    }

    /// Without an App Store Connect key `--lc-install` can't make
    /// LiveContainer's profiles, so Set Up says so before it's pressed. Not
    /// known (settings unread, a CLI too old to say, the account unknown)
    /// disables nothing, as everywhere (`AccountMode`).
    func test_set_up_needs_an_app_store_connect_key() throws {
        typealias Sheet = SetUpLiveContainerSheet
        let key = #","app_store_connect":{"key_id":"K","issuer_id":"I","key_file":"/k.p8","key_file_exists":true}"#
        let lost = #","app_store_connect":{"key_id":"K","issuer_id":"I","key_file":"/k.p8","key_file_exists":false}"#
        XCTAssertEqual(Sheet.needsKeyLine,
                       "Set up an App Store Connect key in Settings › App Store Connect first.")
        XCTAssertEqual(Sheet.needsKey(account: .paid, settings: try settings(#","app_store_connect":null"#)),
                       Sheet.needsKeyLine)
        XCTAssertEqual(Sheet.needsKey(account: .paid, settings: try settings(lost)), Sheet.needsKeyLine,
                       "a key whose .p8 is gone makes no profile either")
        XCTAssertNil(Sheet.needsKey(account: .paid, settings: try settings(key)))
        XCTAssertNil(Sheet.needsKey(account: .paid, settings: try settings("")), "a CLI too old to say")
        XCTAssertNil(Sheet.needsKey(account: .paid, settings: nil), "not read yet")
        let store = LCStatusRecord(
            udid: Self.udid, reachable: true, error: nil, instances: [], managedBy: .altstore, latest: nil,
            updateAvailable: false, sources: [:], certificatePushed: false, freeSetupSteps: [])
        XCTAssertNil(Sheet.needsKey(account: .paid, settings: try settings(#","app_store_connect":null"#),
                                    status: store),
                     "SideStore or AltStore manages it: Graft installs nothing there, key or not")
        XCTAssertEqual(Sheet.needsKey(account: .paid, settings: try settings(#","app_store_connect":null"#),
                                      status: try XCTUnwrap(status(""))), Sheet.needsKeyLine)
        XCTAssertNil(Sheet.needsKey(account: .free, settings: try settings(#","app_store_connect":null"#)),
                     "a free Apple ID installs nothing")
        XCTAssertEqual(Sheet.needsKey(account: nil, settings: try settings(#","app_store_connect":null"#)),
                       Sheet.needsKeyLine, "the paid side shows for an account not known yet")

        let status = try XCTUnwrap(status(""))
        XCTAssertFalse(Sheet.setUpEnabled(status: status, busy: false, instances: [1], resumes: false,
                                          keyMissing: true))
        XCTAssertTrue(Sheet.setUpEnabled(status: status, busy: false, instances: [1], resumes: true,
                                         keyMissing: true), "putting the certificate on needs no key")
        XCTAssertTrue(Sheet.setUpEnabled(status: status, busy: false, instances: [1], resumes: false,
                                         keyMissing: false))
    }

    /// Graft's LiveContainer is signed with a development profile, so on iOS 16
    /// and later it opens only with Developer Mode on. The sheet says so where
    /// Graft installs it: not on a free Apple ID (SideStore or iloader installs
    /// it), not where SideStore or AltStore manages it, and not before iOS 16.
    func test_developer_mode_is_shown_where_graft_installs_livecontainer() throws {
        typealias Sheet = SetUpLiveContainerSheet
        let graft = try XCTUnwrap(status(""))
        let store = LCStatusRecord(
            udid: Self.udid, reachable: true, error: nil, instances: [], managedBy: .altstore, latest: nil,
            updateAvailable: false, sources: [:], certificatePushed: false, freeSetupSteps: [])
        XCTAssertEqual(Sheet.developerMode(account: .paid, status: graft, iosVersion: "26.1", reading: false), .off)
        XCTAssertEqual(Sheet.developerMode(account: .paid, status: graft, iosVersion: "26.1", reading: true), .on)
        XCTAssertEqual(Sheet.developerMode(account: .paid, status: graft, iosVersion: "26.1", reading: nil), .unknown)
        XCTAssertEqual(Sheet.developerMode(account: nil, status: nil, iosVersion: "16.0", reading: nil), .unknown)
        XCTAssertNil(Sheet.developerMode(account: .free, status: graft, iosVersion: "26.1", reading: false))
        XCTAssertNil(Sheet.developerMode(account: .paid, status: store, iosVersion: "26.1", reading: false))
        XCTAssertNil(Sheet.developerMode(account: .paid, status: graft, iosVersion: "15.8", reading: nil))
        let note = Sheet.developerModeNote(name: "Example iPad")
        XCTAssertTrue(note.contains("Settings › Privacy & Security › Developer Mode"), note)
        XCTAssertTrue(note.contains("Example iPad"), note)
    }

    /// One LiveContainer unless more are there already: an update still
    /// updates each one Graft finds.
    func test_set_up_starts_with_one_livecontainer() throws {
        typealias Sheet = SetUpLiveContainerSheet
        XCTAssertEqual(Sheet.defaultInstances(status: nil), [1])
        XCTAssertEqual(Sheet.defaultInstances(status: try XCTUnwrap(status(""))), [1])
        let two = LCStatusRecord(udid: Self.udid, reachable: true, error: nil, instances: [
                LCInstanceRecord(bundleId: FakeLiveContainerCLI.primary, version: "3.8.14", role: "primary",
                                 wildcardSigned: false),
                LCInstanceRecord(bundleId: "com.kdt.LiveContainer2.ABCDE12345", version: "3.8.14", role: "2",
                                 wildcardSigned: false),
                LCInstanceRecord(bundleId: "com.kdt.livecontainer", version: "3.0", role: "legacy",
                                 wildcardSigned: false),
            ], managedBy: .graft, latest: nil, updateAvailable: false, sources: [:],
            certificatePushed: false, freeSetupSteps: [])
        XCTAssertEqual(Sheet.defaultInstances(status: two), [1, 2])
        XCTAssertEqual(Sheet.instanceLabel(1), "LiveContainer (the primary)")
        XCTAssertEqual(Sheet.instanceLabel(2), "LiveContainer2")
        XCTAssertEqual(Sheet.instanceLabel(3), "LiveContainer3")
    }

    /// A free Apple ID with LiveContainer already on the device is told so,
    /// and has one step left, importing the certificate; without it, every
    /// step.
    func test_a_free_sheet_with_livecontainer_there_has_only_the_certificate_step() throws {
        typealias Sheet = SetUpLiveContainerSheet
        let given = ["Install SideStore.", "Install LiveContainer.", "Import Certificate from SideStore."]
        let none = try XCTUnwrap(status(""))
        let there = LCStatusRecord(udid: Self.udid, reachable: true, error: nil, instances: [
                LCInstanceRecord(bundleId: "com.kdt.livecontainer.ABCDE12345", version: "3.8.14",
                                 role: "primary", wildcardSigned: false),
            ], managedBy: .unknown, latest: nil, updateAvailable: false, sources: [:],
            certificatePushed: false, freeSetupSteps: given)
        XCTAssertEqual(Sheet.freeSteps(given: given, status: none), given)
        XCTAssertEqual(Sheet.freeSteps(given: given, status: there), ["Import Certificate from SideStore."])
        XCTAssertEqual(Sheet.freeSteps(given: [], status: there), [Sheet.freeFallback.last!],
                       "a CLI too old to send its steps")
        XCTAssertEqual(Sheet.freeSteps(given: [], status: nil), Sheet.freeFallback)
        XCTAssertNil(Sheet.foundLine(status: none, name: "Example iPad"))
        XCTAssertEqual(Sheet.foundLine(status: there, name: "Example iPad"),
                       "Graft found LiveContainer 3.8.14 on Example iPad.")
    }

    // MARK: - The sheet, hosted

    private func hostedSheet(
        account: AccountMode, cli: FakeLiveContainerCLI, onRunEnded: @escaping () -> Void = {},
        onOpenSettings: (() -> Void)? = nil
    ) async throws -> (Hosted, LiveContainerStore) {
        try AccessibilityTree.requireServed()
        let store = LiveContainerStore(runner: cli.runner)
        await store.load(udid: Self.udid)
        let device = try XCTUnwrap(deviceRecord())
        let box = hosted(
            SetUpLiveContainerSheet(
                device: device, store: store, settings: SettingsStore(runner: cli.runner),
                deviceInfo: DeviceInfoStore(runner: cli.runner), account: account,
                onRunEnded: onRunEnded, onOpenSettings: onOpenSettings
            ),
            size: NSSize(width: 600, height: 900)
        )
        try await Task.sleep(nanoseconds: 400_000_000)
        settle(box)
        return (box, store)
    }

    func test_the_paid_sheet_sets_up_and_its_tick_takes_the_certificate_off() async throws {
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        try cli.set("cert.mark")
        let (box, store) = try await hostedSheet(account: .paid, cli: cli)
        let nodes = try AccessibilityTree.nodes(in: box.window)
        let buttons = nodes.filter { $0.role == kAXButtonRole }
        let setUp = try XCTUnwrap(buttons.first { $0.name == SetUpLiveContainerSheet.setUpLabel },
                                  "\(buttons.map(\.name))")
        XCTAssertNotEqual(setUp.enabled, false)
        XCTAssertNotNil(buttons.first { $0.name == SetUpLiveContainerSheet.installLabel }, "one step at a time")
        XCTAssertNotNil(buttons.first { $0.name == SetUpLiveContainerSheet.pushLabel })
        XCTAssertTrue(nodes.contains { node in
            node.role == "AXHeading"
                && ((node.value as? String) == "Set Up LiveContainer on Example iPad"
                    || node.name == "Set Up LiveContainer on Example iPad")
        }, "the title is a heading, for VoiceOver's rotor: \(nodes.map { ($0.role, $0.name) })")
        let tick = try XCTUnwrap(
            nodes.first { $0.role == kAXCheckBoxRole && $0.name == SetUpLiveContainerSheet.tickLabel },
            "\(nodes.map(\.name))"
        )
        XCTAssertNotEqual(tick.enabled, false, "a certificate is on the device to take off")

        try clickButton(named: SetUpLiveContainerSheet.tickLabel, in: box.window, role: kAXCheckBoxRole)
        let deadline = Date().addingTimeInterval(10)
        while store.status[Self.udid]?.certificatePushed != false, Date() < deadline {
            try await Task.sleep(nanoseconds: 50_000_000)
        }

        XCTAssertTrue(cli.argv.contains("--json --lc-clean --cert-only --device \(Self.udid)"), "\(cli.argv)")
        XCTAssertFalse(cli.argv.contains("--json --lc-clean --device \(Self.udid)"), "the IPAs stay")
        XCTAssertEqual(store.status[Self.udid]?.certificatePushed, false)
    }

    func test_the_free_sheet_has_no_set_up_and_no_certificate_of_grafts() async throws {
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid, freeSteps: [
            "Install SideStore (https://example.invalid/sidestore) with your free Apple ID.",
            "Install LiveContainer with it.",
            "In LiveContainer, import the certificate from SideStore.",
        ])
        try cli.set("no-primary")
        let (box, _) = try await hostedSheet(account: .free, cli: cli)
        let nodes = try AccessibilityTree.nodes(in: box.window)
        let names = nodes.map(\.name)

        for label in [SetUpLiveContainerSheet.setUpLabel, SetUpLiveContainerSheet.installLabel,
                      SetUpLiveContainerSheet.pushLabel] {
            XCTAssertFalse(nodes.contains { $0.role == kAXButtonRole && $0.name == label }, "\(label): \(names)")
        }
        XCTAssertFalse(nodes.contains { $0.role == kAXCheckBoxRole && $0.name == SetUpLiveContainerSheet.tickLabel })
        XCTAssertTrue(nodes.contains { $0.role == "AXLink" && $0.name == "Open example.invalid" },
                      "the CLI's steps, with their links: \(names)")
        let texts = try AccessibilityTree.texts(in: box.window)
        XCTAssertFalse(texts.contains { $0.contains("needs a paid Apple Developer account") },
                       "the steps say what to do; no refusal: \(texts)")
        XCTAssertTrue(texts.contains("Then, in LiveContainer on Example iPad:"), "\(texts)")
        XCTAssertTrue(texts.contains("Long-press each app › Convert to Shared App."), "\(texts)")
    }

    /// No App Store Connect key: Set Up and Install / Update wait, with the
    /// line saying what to do and Open Settings beside it.
    func test_a_paid_sheet_without_a_key_says_where_to_set_one_up() async throws {
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        try cli.set("no-key")
        final class Opened { var count = 0 }
        let opened = Opened()
        let (box, _) = try await hostedSheet(account: .paid, cli: cli, onOpenSettings: { opened.count += 1 })
        let nodes = try AccessibilityTree.nodes(in: box.window)
        let buttons = nodes.filter { $0.role == kAXButtonRole }
        XCTAssertEqual(buttons.first { $0.name == SetUpLiveContainerSheet.setUpLabel }?.enabled, false,
                       "\(buttons.map(\.name))")
        XCTAssertEqual(buttons.first { $0.name == SetUpLiveContainerSheet.installLabel }?.enabled, false)
        let texts = try AccessibilityTree.texts(in: box.window)
        XCTAssertTrue(texts.contains(SetUpLiveContainerSheet.needsKeyLine), "\(texts)")
        XCTAssertTrue(cli.argv.contains("--json --settings"), "the sheet read the settings: \(cli.argv)")

        try clickButton(named: SetUpLiveContainerSheet.openSettingsLabel, in: box.window)
        XCTAssertEqual(opened.count, 1)
        XCTAssertFalse(cli.verbs.contains("--lc-install"))
    }

    /// Developer Mode off on the device: said, with what to do, and read
    /// again on Check again.
    func test_the_paid_sheet_says_developer_mode_is_off() async throws {
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        try cli.set("devmode-off")
        let (box, _) = try await hostedSheet(account: .paid, cli: cli)
        let texts = try AccessibilityTree.texts(in: box.window)
        XCTAssertTrue(texts.contains(DeviceDeveloperModeRow.line(isOn: false)), "\(texts)")
        XCTAssertTrue(texts.contains(SetUpLiveContainerSheet.developerModeNote(name: "Example iPad")), "\(texts)")
        XCTAssertTrue(cli.argv.contains("--json --device-info --device \(Self.udid)"), "\(cli.argv)")

        try cli.set("devmode-off", false)
        try cli.set("devmode-on")
        try clickButton(named: SetUpLiveContainerSheet.checkAgainLabel, in: box.window)
        let deadline = Date().addingTimeInterval(10)
        var after: [String] = []
        repeat {
            try await Task.sleep(nanoseconds: 100_000_000)
            settle(box)
            after = try AccessibilityTree.texts(in: box.window)
        } while !after.contains(DeviceDeveloperModeRow.line(isOn: true)) && Date() < deadline
        XCTAssertTrue(after.contains(DeviceDeveloperModeRow.line(isOn: true)), "\(after)")
        XCTAssertFalse(after.contains(SetUpLiveContainerSheet.developerModeNote(name: "Example iPad")), "\(after)")
    }

    /// The three LiveContainer checkboxes and the channel picker say what
    /// they are to VoiceOver, and only the primary starts checked.
    func test_the_install_choices_are_labeled_and_start_with_one() async throws {
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        try cli.set("no-primary")
        let (box, _) = try await hostedSheet(account: .paid, cli: cli)
        let nodes = try AccessibilityTree.nodes(in: box.window)
        let boxes = nodes.filter { $0.role == kAXCheckBoxRole }
        for n in 1...3 {
            let node = try XCTUnwrap(boxes.first { $0.name == SetUpLiveContainerSheet.instanceLabel(n) },
                                     "\(boxes.map(\.name))")
            XCTAssertEqual((node.value as? NSNumber)?.boolValue, n == 1, "LiveContainer \(n)")
        }
        XCTAssertTrue(nodes.contains { $0.role == kAXPopUpButtonRole && $0.name == SetUpLiveContainerSheet.channelLabel },
                      "\(nodes.filter { $0.role == kAXPopUpButtonRole }.map(\.name))")
    }

    /// A free Apple ID with LiveContainer already on the device: no refusal
    /// first, Graft says what it found, and the one step left.
    func test_the_free_sheet_with_livecontainer_there_says_so() async throws {
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid, freeSteps: [
            "Install SideStore (https://example.invalid/sidestore) with your free Apple ID.",
            "Install LiveContainer with it.",
            "In LiveContainer, import the certificate from SideStore.",
        ])
        let (box, _) = try await hostedSheet(account: .free, cli: cli)
        let texts = try AccessibilityTree.texts(in: box.window)
        let found = "Graft found LiveContainer 3.8.14 on Example iPad."
        XCTAssertTrue(texts.contains(found), "\(texts)")
        XCTAssertFalse(texts.contains { $0.contains("Install SideStore") }, "\(texts)")
        XCTAssertTrue(texts.contains { $0.contains("import the certificate from SideStore") }, "\(texts)")
        XCTAssertFalse(texts.contains { $0.contains("needs a paid Apple Developer account") },
                       "no refusal: \(texts)")
    }

    /// A paid account's SideStore phone: no Set Up, no Put certificate, no
    /// tick, as on the device's page; the certificate comes from SideStore.
    func test_a_paid_sidestore_phone_is_offered_no_set_up() async throws {
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        try cli.set("altstore")
        let (box, _) = try await hostedSheet(account: .paid, cli: cli)
        let nodes = try AccessibilityTree.nodes(in: box.window)
        let buttons = nodes.filter { $0.role == kAXButtonRole }
        XCTAssertEqual(buttons.first { $0.name == SetUpLiveContainerSheet.setUpLabel }?.enabled, false,
                       "\(buttons.map(\.name))")
        XCTAssertEqual(buttons.first { $0.name == SetUpLiveContainerSheet.installLabel }?.enabled, false)
        XCTAssertNil(buttons.first { $0.name == SetUpLiveContainerSheet.pushLabel })
        XCTAssertFalse(nodes.contains { $0.role == kAXCheckBoxRole && $0.name == SetUpLiveContainerSheet.tickLabel })
        let texts = try AccessibilityTree.texts(in: box.window)
        XCTAssertTrue(texts.contains(SetUpLiveContainerSheet.altStoreNote(name: "Example iPad")), "\(texts)")
        XCTAssertTrue(texts.contains(SetUpLiveContainerSheet.storeCertificateStep), "\(texts)")
    }

    /// The same phone with no App Store Connect key: Set Up is off for
    /// SideStore's sake, so the key isn't what to fix, and isn't said.
    func test_a_paid_sidestore_phone_says_nothing_of_the_key() async throws {
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        try cli.set("altstore")
        try cli.set("no-key")
        let (box, _) = try await hostedSheet(account: .paid, cli: cli, onOpenSettings: {})
        let texts = try AccessibilityTree.texts(in: box.window)
        XCTAssertTrue(texts.contains(SetUpLiveContainerSheet.altStoreNote(name: "Example iPad")), "\(texts)")
        XCTAssertTrue(cli.argv.contains("--json --settings"), "premise: the settings were read: \(cli.argv)")
        XCTAssertFalse(texts.contains(SetUpLiveContainerSheet.needsKeyLine), "\(texts)")
        XCTAssertFalse(try AccessibilityTree.nodes(in: box.window).contains {
            $0.role == kAXButtonRole && $0.name == SetUpLiveContainerSheet.openSettingsLabel
        })
    }

    /// While a run goes, Close waits (Stop is beside it), so nothing carries
    /// on out of sight; and the devices are read again when the run ends,
    /// not when the sheet closes.
    func test_close_waits_for_the_run_and_its_end_reads_the_devices_again() async throws {
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        try cli.set("hang-install")
        final class Ended { var count = 0 }
        let ended = Ended()
        let (box, store) = try await hostedSheet(account: .paid, cli: cli, onRunEnded: { ended.count += 1 })
        XCTAssertNotEqual(try AccessibilityTree.nodes(in: box.window)
            .first { $0.role == kAXButtonRole && $0.name == SetUpLiveContainerSheet.closeLabel }?.enabled,
            false, "premise: nothing runs, Close closes")

        try clickButton(named: SetUpLiveContainerSheet.setUpLabel, in: box.window)
        var deadline = Date().addingTimeInterval(10)
        while store.run(for: Self.udid)?.progress.isEmpty ?? true, Date() < deadline {
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        settle(box)
        let running = try AccessibilityTree.nodes(in: box.window)
        XCTAssertEqual(running.first { $0.role == kAXButtonRole && $0.name == SetUpLiveContainerSheet.closeLabel }?
            .enabled, false, "the install is running")
        XCTAssertEqual(ended.count, 0)

        try clickButton(named: "Stop", in: box.window)
        deadline = Date().addingTimeInterval(10)
        while store.isBusy, Date() < deadline {
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        settle(box)
        XCTAssertNotEqual(try AccessibilityTree.nodes(in: box.window)
            .first { $0.role == kAXButtonRole && $0.name == SetUpLiveContainerSheet.closeLabel }?.enabled, false)
        XCTAssertEqual(ended.count, 1, "the run ended: read the devices once")
    }

    // MARK: - The device page's reminder

    func test_the_reminder_shows_while_a_certificate_of_grafts_is_on_the_device() throws {
        let on = try XCTUnwrap(status(#","certificate_pushed":true"#))
        let off = try XCTUnwrap(status(""))
        XCTAssertEqual(DeviceLiveContainerSection.blocks(for: on, account: .paid).first, .certificateReminder)
        XCTAssertEqual(DeviceLiveContainerSection.blocks(for: on, account: .free).first, .certificateReminder,
                       "a push from a paid account before: Remove it works on either")
        XCTAssertFalse(DeviceLiveContainerSection.blocks(for: off, account: .paid).contains(.certificateReminder))
        XCTAssertEqual(DeviceLiveContainerSection.reminder(name: "Example iPad"),
                       "The certificate is still on Example iPad:")
        XCTAssertEqual(DeviceLiveContainerSection.removeItLabel, "Remove it")
    }

    /// A free Apple ID's sheet installs nothing and puts no certificate on.
    func test_set_up_livecontainers_tooltip_suits_the_account() {
        let free = DeviceLiveContainerSection.setUpHelp(account: .free, name: "Example iPad")
        XCTAssertFalse(free.contains("Install LiveContainer"), free)
        XCTAssertFalse(free.contains("certificate"), free)
        XCTAssertTrue(free.contains("SideStore or iloader"), free)
        let paid = DeviceLiveContainerSection.setUpHelp(account: .paid, name: "Example iPad")
        XCTAssertTrue(paid.hasPrefix("Install LiveContainer on Example iPad"), paid)
    }

    func test_the_device_page_reminds_and_its_buttons_open_set_up_and_remove_it() async throws {
        try AccessibilityTree.requireServed()
        let cli = try FakeLiveContainerCLI(for: self, udid: Self.udid)
        try cli.set("cert.mark")
        let store = LiveContainerStore(runner: cli.runner)
        await store.load(udid: Self.udid)
        final class Opened { var count = 0 }
        let opened = Opened()
        let device = try XCTUnwrap(deviceRecord())
        let box = hosted(
            DeviceLiveContainerSection(device: device, store: store, account: .paid,
                                       onSetUp: { opened.count += 1 }),
            width: 700
        )
        try await Task.sleep(nanoseconds: 400_000_000)
        settle(box)
        let before = try AccessibilityTree.texts(in: box.window)
        XCTAssertTrue(before.contains("The certificate is still on Example iPad:"), "\(before)")

        try clickButton(named: DeviceLiveContainerSection.setUpLabel, in: box.window)
        XCTAssertEqual(opened.count, 1, "Set Up LiveContainer… opens the sheet")

        try clickButton(named: DeviceLiveContainerSection.removeItLabel, in: box.window)
        let deadline = Date().addingTimeInterval(10)
        while store.status[Self.udid]?.certificatePushed != false, Date() < deadline {
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        XCTAssertTrue(cli.argv.contains("--json --lc-clean --cert-only --device \(Self.udid)"), "\(cli.argv)")
        XCTAssertFalse(cli.argv.contains("--json --lc-clean --device \(Self.udid)"), "the certificate only")
        settle(box)
        let after = try AccessibilityTree.texts(in: box.window)
        XCTAssertFalse(after.contains("The certificate is still on Example iPad:"), "\(after)")
    }

    // MARK: - The app pane's way in

    /// Plan E makes `.liveContainerMissing` only when `--devices` could read
    /// the phone's apps (`livecontainerKnown`), so the button is offered only
    /// when LiveContainer is known to be absent.
    func test_only_a_target_without_livecontainer_offers_set_up() {
        let missing = BuildTarget.liveContainerMissing(name: "Example iPad", udid: Self.udid, kind: "iPad")
        XCTAssertEqual(BuildTargetBar.setUpTarget(missing)?.udid, Self.udid)
        XCTAssertEqual(BuildTargetBar.setUpTarget(missing)?.kind, "iPad")
        XCTAssertNil(BuildTargetBar.setUpTarget(.noDevice))
        XCTAssertNil(BuildTargetBar.setUpTarget(.device(name: "Example iPad", mode: "LiveContainer")))
        let why = BuildTargetBar.explanation(for: missing)
        XCTAssertTrue(why.contains("Set Up LiveContainer…"), why)
        XCTAssertFalse(why.contains("page"), "the button sets it up here, not on the device's page: \(why)")
    }

    /// The bar doesn't know the account, and a free Apple ID's sheet
    /// installs nothing and puts no certificate on: its words promise
    /// neither.
    func test_the_bars_words_suit_a_free_apple_id_too() {
        let missing = BuildTarget.liveContainerMissing(name: "Example iPad", udid: Self.udid, kind: "iPad")
        let why = BuildTargetBar.explanation(for: missing)
        let help = BuildTargetBar.setUpHelp(kind: "iPad")
        for words in [why, help] {
            XCTAssertFalse(words.contains("installs it"), words)
            XCTAssertFalse(words.contains("Install LiveContainer"), words)
            XCTAssertFalse(words.contains("certificate"), words)
        }
        XCTAssertTrue(help.contains("this iPad"), help)
    }

    func test_the_bars_button_opens_set_up_for_that_device() async throws {
        try AccessibilityTree.requireServed()
        final class Asked { var udids: [String] = [] }
        let asked = Asked()
        let box = hosted(
            BuildTargetBar(
                target: .liveContainerMissing(name: "Example iPad", udid: Self.udid, kind: "iPad"),
                onSetUpLiveContainer: { asked.udids.append($0) },
                onChange: {}
            ),
            width: 900
        )
        settle(box)
        let texts = try AccessibilityTree.texts(in: box.window)
        XCTAssertTrue(texts.contains("LiveContainer isn't on Example iPad"), "no trailing period: \(texts)")
        XCTAssertFalse(texts.contains(BuildTargetBar.sideloadOffer), "not known to be paid: \(texts)")

        try clickButton(named: DeviceLiveContainerSection.setUpLabel, in: box.window)

        XCTAssertEqual(asked.udids, [Self.udid])
    }
}
