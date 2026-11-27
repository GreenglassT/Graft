import XCTest
@testable import Graft

final class BuildEventTests: XCTestCase {
    func test_decode_stage_start_event() throws {
        let json = #"{"type":"stage","name":"cyan","status":"start"}"#
        let event = try BuildEvent.decode(line: json)
        guard case let .stage(name, status, _) = event else {
            return XCTFail("expected .stage, got \(event)")
        }
        XCTAssertEqual(name, "cyan")
        XCTAssertEqual(status, "start")
    }

    func test_decode_stage_end_event_with_extra_fields() throws {
        let json = #"{"type":"stage","name":"lc-push","status":"end","bundle_id":"com.kdt.livecontainer","size_bytes":1024}"#
        let event = try BuildEvent.decode(line: json)
        guard case let .stage(name, status, fields) = event else {
            return XCTFail("expected .stage")
        }
        XCTAssertEqual(name, "lc-push")
        XCTAssertEqual(status, "end")
        XCTAssertEqual(fields["bundle_id"] as? String, "com.kdt.livecontainer")
    }

    func test_decode_log_event() throws {
        let json = #"{"type":"log","source":"cyan","line":"injecting tweak"}"#
        let event = try BuildEvent.decode(line: json)
        guard case let .log(source, text) = event else {
            return XCTFail("expected .log")
        }
        XCTAssertEqual(source, "cyan")
        XCTAssertEqual(text, "injecting tweak")
    }

    func test_decode_build_start_event() throws {
        let json = #"{"type":"build-start","app":"example","bundle_id":"com.example.app","delivery":"livecontainer"}"#
        let event = try BuildEvent.decode(line: json)
        guard case let .buildStart(app, bundleId, delivery) = event else {
            return XCTFail("expected .buildStart")
        }
        XCTAssertEqual(app, "example")
        XCTAssertEqual(bundleId, "com.example.app")
        XCTAssertEqual(delivery, .livecontainer)
    }

    func test_decode_build_end_event_failure() throws {
        let json = #"{"type":"build-end","app":"example","ok":false,"error":"cyan failed"}"#
        let event = try BuildEvent.decode(line: json)
        guard case let .buildEnd(app, ok, error, _, _, waiting) = event else {
            return XCTFail("expected .buildEnd")
        }
        XCTAssertEqual(app, "example")
        XCTAssertFalse(ok)
        XCTAssertEqual(error, "cyan failed")
        XCTAssertFalse(waiting, "absent is false")
    }

    /// T2-B7: an update's device unplugged mid-push. Its `build-end` says it
    /// waits, in the update's words, and the transcript says so without ✗.
    func test_a_waiting_build_end_reads_as_a_wait_not_a_failure() throws {
        let said = "Example iPad isn't connected: it'll update when it is"
        let json = #"{"type":"build-end","app":"example","ok":false,"stopped":false,"waiting":true,"error":"\#(said)","elapsed_seconds":4.2}"#
        let event = try BuildEvent.decode(line: json)
        guard case let .buildEnd(_, ok, error, _, stopped, waiting) = event else {
            return XCTFail("expected .buildEnd")
        }
        XCTAssertEqual([ok, stopped, waiting], [false, false, true])
        XCTAssertEqual(error, said)

        let lines = BuildLogView.lines(for: event)
        XCTAssertEqual(lines.map(\.text), ["  \(said) (4.2s)"])
        XCTAssertEqual(lines.map(\.kind), [.dim])
    }

    func test_decode_app_record_event() throws {
        let json = #"{"type":"app","name":"example","bundle_id":"com.example.app","preferred_version":"latest","tweaks":["a.deb"],"display_name":"Example+","delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}"#
        let event = try BuildEvent.decode(line: json)
        guard case let .app(record) = event else {
            return XCTFail("expected .app")
        }
        XCTAssertEqual(record.name, "example")
        XCTAssertEqual(record.bundleId, "com.example.app")
        XCTAssertEqual(record.tweaks, ["a.deb"])
        XCTAssertEqual(record.delivery, .livecontainer)
    }

    /// `needs_developer_mode` (spec §5): whether the app is signed with a
    /// development profile, which the app can't read for itself. Null is "its
    /// profile couldn't be read"; a CLI from before 2.3 sends no key. Both are
    /// no answer, and neither drops the app.
    func test_decode_app_needs_developer_mode() throws {
        let base = #"{"type":"app","name":"example","bundle_id":"com.example.app","preferred_version":"latest","tweaks":[],"display_name":null,"delivery":"sideload","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6"#
        let cases: [(String, Bool?)] = [
            (#","needs_developer_mode":true}"#, true),
            (#","needs_developer_mode":false}"#, false),
            (#","needs_developer_mode":null}"#, nil),
            ("}", nil),
        ]
        for (tail, expected) in cases {
            guard case let .app(record) = try BuildEvent.decode(line: base + tail) else {
                return XCTFail("expected .app for \(tail)")
            }
            XCTAssertEqual(record.needsDeveloperMode, expected, tail)
        }
    }

    /// `--reinstall-build` emits `delivery: null` on purpose, for an app that
    /// has since been removed from config.yaml: the guard that raises "no
    /// longer in config.yaml" runs *inside* the build envelope, so
    /// `build-start` has already gone out and there is genuinely no mode to
    /// name. `?? .sideload` turned that into a claim the CLI declined to make
    /// — and for a livecontainer app, the opposite one, printed on the line
    /// above the error.
    func test_decode_build_start_with_a_null_delivery_stays_unknown() throws {
        let json = #"{"type":"build-start","app":"gone","bundle_id":"com.example.gone","delivery":null}"#
        guard case let .buildStart(app, _, delivery) = try BuildEvent.decode(line: json) else {
            return XCTFail("expected .buildStart")
        }
        XCTAssertEqual(app, "gone")
        XCTAssertNil(delivery, "null means the CLI does not know, not sideload")
    }

    func test_build_log_label_for_a_known_delivery_is_the_cli_spelling() {
        XCTAssertEqual(DeliveryMode?.some(.livecontainer).buildLogLabel, "livecontainer")
        XCTAssertEqual(DeliveryMode?.some(.sideload).buildLogLabel, "sideload")
    }

    /// What every panel prints instead of inventing a mode. Must not read as
    /// either real mode, or it is the same lie in different words.
    func test_build_log_label_for_an_unknown_delivery_says_so() {
        let label = DeliveryMode?.none.buildLogLabel
        XCTAssertTrue(label.contains("unknown"), label)
        XCTAssertFalse(label.contains("sideload"), label)
        XCTAssertFalse(label.contains("livecontainer"), label)
    }

    // MARK: - device-error
    //
    // Added to the CLI and routed nowhere: no decode case, so it became
    // `.unknown`, and every `pick` in Services/ dropped it. A phone usbmux
    // enumerated but couldn't describe went unreported everywhere while the
    // Devices tab said "No devices connected".

    func test_decode_device_error_event() throws {
        let line = #"{"type":"device-error","udid":"00008130-001A2B3C0123456D","error":"lockdown query failed: device is passcode locked"}"#
        guard case let .deviceError(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .deviceError — an unhandled type decodes as "
                           + "`.unknown` and every store's pick closure drops it")
        }
        XCTAssertEqual(record.udid, "00008130-001A2B3C0123456D")
        XCTAssertEqual(record.error, "lockdown query failed: device is passcode locked")
    }

    /// It rides in the same stream as the devices that *did* answer, so it has
    /// to survive being decoded alongside them rather than only on its own.
    func test_device_error_survives_a_mixed_devices_stream() {
        let stream = """
        {"type":"device","udid":"GOOD","name":"Test iPhone","model":"iPhone14,2","ios_version":"26.0.1","connection_type":"USB","livecontainer_bundle_id":null}
        {"type":"device-error","udid":"BAD","error":"connection refused"}
        {"type":"devices-end","count":1,"failed":1}
        """
        let events = stream.buildEvents()
        XCTAssertEqual(events.count, 3)
        let failures = events.compactMap { event -> DeviceErrorRecord? in
            if case let .deviceError(record) = event { return record }
            return nil
        }
        XCTAssertEqual(failures.map(\.udid), ["BAD"])
    }

    /// A device problem is not a build problem: it must stay out of the build
    /// panels, like every other non-build event.
    func test_device_error_is_not_part_of_the_build_stream() {
        let record = DeviceErrorRecord(udid: "BAD", error: "connection refused")
        XCTAssertNil(BuildEvent.deviceError(record).progress)
    }

    func test_decode_repo_added() throws {
        let line = #"{"type":"repo-added","url":"https://apt.example.com","name":"Example","origin":"Example","package_count":42}"#
        guard case let .repoAdded(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .repoAdded")
        }
        XCTAssertEqual(record.url, "https://apt.example.com")
        XCTAssertEqual(record.name, "Example")
        XCTAssertEqual(record.packageCount, 42)
        XCTAssertNil(record.lastRefreshed)
    }

    func test_decode_repo_listing() throws {
        let line = #"{"type":"repo","url":"https://apt.example.com","name":"Example","origin":null,"package_count":7,"last_refreshed":"2026-05-22T10:00:00"}"#
        guard case let .repo(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .repo")
        }
        XCTAssertEqual(record.packageCount, 7)
        XCTAssertEqual(record.lastRefreshed, "2026-05-22T10:00:00")
    }

    func test_decode_repo_package() throws {
        let line = #"{"type":"repo-package","package_id":"com.example.alpha","name":"Alpha","version":"1.0","description":"d","author":"a","section":"Tweaks","filename":"debs/alpha.deb","size_bytes":100,"sha256":"abc","depiction":null,"download_url":"https://apt.example.com/debs/alpha.deb"}"#
        guard case let .repoPackage(pkg) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .repoPackage")
        }
        XCTAssertEqual(pkg.packageId, "com.example.alpha")
        XCTAssertEqual(pkg.downloadURL, "https://apt.example.com/debs/alpha.deb")
        XCTAssertEqual(pkg.sizeBytes, 100)
    }

    func test_decode_repo_packages_end() throws {
        let line = #"{"type":"repo-packages-end","url":"https://apt.example.com","count":3}"#
        guard case let .repoPackagesEnd(url, count) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .repoPackagesEnd")
        }
        XCTAssertEqual(url, "https://apt.example.com")
        XCTAssertEqual(count, 3)
    }

    func test_decode_repos_end() throws {
        let line = #"{"type":"repos-end","count":4}"#
        guard case let .reposEnd(count) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .reposEnd")
        }
        XCTAssertEqual(count, 4)
    }

    func test_decode_app_added() throws {
        let line = #"{"type":"app-added","name":"bravo","bundle_id":"com.bravo.app","display_name":"Bravo","delivery":"sideload"}"#
        guard case let .appAdded(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .appAdded")
        }
        XCTAssertEqual(record.name, "bravo")
        XCTAssertEqual(record.bundleId, "com.bravo.app")
        XCTAssertEqual(record.displayName, "Bravo")
        XCTAssertEqual(record.delivery, .sideload)
    }

    func test_decode_cached_ipa_exists() throws {
        let line = #"{"type":"cached-ipa","app":"example","exists":true,"bundle_id":"com.example.app","version":"21.16.2","filename":"com.example.app_21.16.2.ipa","path":"/x/y.ipa","size_bytes":12345}"#
        guard case let .cachedIPA(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .cachedIPA")
        }
        XCTAssertEqual(record.app, "example")
        XCTAssertTrue(record.exists)
        XCTAssertEqual(record.bundleId, "com.example.app")
        XCTAssertEqual(record.version, "21.16.2")
        XCTAssertEqual(record.filename, "com.example.app_21.16.2.ipa")
        XCTAssertEqual(record.path, "/x/y.ipa")
        XCTAssertEqual(record.sizeBytes, 12345)
    }

    func test_decode_cached_ipa_missing() throws {
        let line = #"{"type":"cached-ipa","app":"example","exists":false}"#
        guard case let .cachedIPA(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .cachedIPA")
        }
        XCTAssertEqual(record.app, "example")
        XCTAssertFalse(record.exists)
        XCTAssertEqual(record.bundleId, "")
        XCTAssertEqual(record.version, "")
        XCTAssertEqual(record.filename, "")
        XCTAssertEqual(record.path, "")
        XCTAssertEqual(record.sizeBytes, 0)
    }

    func test_decode_ipa_provided() throws {
        let line = #"{"type":"ipa-provided","app":"example","bundle_id":"com.example.app","version":"21.16.2","filename":"com.example.app_21.16.2.ipa","path":"/x/y.ipa","size_bytes":12345}"#
        guard case let .ipaProvided(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .ipaProvided")
        }
        XCTAssertEqual(record.app, "example")
        XCTAssertTrue(record.exists)
        XCTAssertEqual(record.version, "21.16.2")
    }

    func test_decode_tweak_attached() throws {
        let line = #"{"type":"tweak-attached","app":"example","filename":"newtweak.deb","tweaks":["existing.deb","newtweak.deb"]}"#
        guard case let .tweakAttached(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .tweakAttached")
        }
        XCTAssertEqual(record.app, "example")
        XCTAssertEqual(record.filename, "newtweak.deb")
        XCTAssertEqual(record.tweaks, ["existing.deb", "newtweak.deb"])
    }

    func test_decode_tweak_detached() throws {
        let line = #"{"type":"tweak-detached","app":"example","filename":"old.deb","tweaks":["kept.deb"]}"#
        guard case let .tweakDetached(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .tweakDetached")
        }
        XCTAssertEqual(record.app, "example")
        XCTAssertEqual(record.filename, "old.deb")
        XCTAssertEqual(record.tweaks, ["kept.deb"])
    }

    // MARK: - encrypted-binaries
    //
    // The event that turned a crash-on-launch into a fully green build. It has
    // to decode, it has to reach a panel, and what it says has to be something
    // a user can act on.

    private static let encryptedLine = #"""
    {"type":"encrypted-binaries","app":"example","count":2,"binaries":[{"path":"Payload/Example.app/Example","cryptids":[1]},{"path":"Payload/Example.app/Frameworks/Core.framework/Core","cryptids":[1,1]}]}
    """#

    private func encryptedRecord(
        app: String = "example", count: Int = 2, paths: [String] = ["a", "b"]
    ) -> EncryptedBinariesRecord {
        EncryptedBinariesRecord(
            app: app,
            count: count,
            binaries: paths.map { EncryptedBinariesRecord.Binary(path: $0, cryptids: [1]) }
        )
    }

    func test_decode_encrypted_binaries_event() throws {
        guard case let .encryptedBinaries(record) =
                try BuildEvent.decode(line: Self.encryptedLine) else {
            return XCTFail("expected .encryptedBinaries — an unhandled type "
                           + "decodes as .unknown and every panel drops it")
        }
        XCTAssertEqual(record.app, "example")
        XCTAssertEqual(record.count, 2)
        XCTAssertEqual(
            record.binaries.map(\.path),
            ["Payload/Example.app/Example", "Payload/Example.app/Frameworks/Core.framework/Core"]
        )
        XCTAssertEqual(record.binaries.last?.cryptids, [1, 1])
    }

    /// The half the finding was actually about: the warning has to reach a
    /// panel, and `progress` is the only thing the panels look at.
    func test_progress_carries_the_encrypted_binaries_record() throws {
        let event = try BuildEvent.decode(line: Self.encryptedLine)
        guard case let .encrypted(record) = event.progress else {
            return XCTFail("expected .encrypted, got \(String(describing: event.progress))")
        }
        XCTAssertEqual(record.count, 2)
    }

    /// It arrives mid-build and must still be there after `build-end` says the
    /// build was fine — the case that produced a fully green crash.
    func test_progress_survives_a_green_build_end_in_the_same_stream() {
        let stream = """
        {"type":"build-start","app":"example","bundle_id":"com.example.app","delivery":"livecontainer"}
        {"type":"stage","name":"verify","status":"start"}
        \(Self.encryptedLine)
        {"type":"stage","name":"verify","status":"end","binaries":24,"bad":1}
        {"type":"build-end","app":"example","ok":true,"elapsed_seconds":84.3}
        """
        let progress = stream.buildEvents().compactMap(\.progress)
        guard let index = progress.firstIndex(where: {
            if case .encrypted = $0 { return true }
            return false
        }) else {
            return XCTFail("the encrypted-binaries line never reached the panel")
        }
        XCTAssertEqual(progress.last, .finished(ok: true, error: nil, elapsed: 84.3))
        XCTAssertLessThan(index, progress.count - 1, "must precede the green finish")
    }

    /// The user reads a consequence, not a diagnostic. "cryptid 1" is the
    /// tool's word for it and tells nobody what to do.
    func test_encrypted_summary_names_the_app_and_the_consequence() {
        let summary = encryptedRecord(app: "example", count: 2).summary
        XCTAssertTrue(summary.contains("example"), summary)
        XCTAssertTrue(summary.contains("2"), summary)
        XCTAssertTrue(summary.contains("crash on launch"), summary)
        XCTAssertFalse(summary.lowercased().contains("cryptid"), summary)
    }

    func test_encrypted_summary_is_singular_for_one_binary() {
        let summary = encryptedRecord(count: 1, paths: ["a"]).summary
        XCTAssertTrue(summary.contains("1 binary is still encrypted"), summary)
    }

    func test_encrypted_summary_is_plural_for_several() {
        let summary = encryptedRecord(count: 3, paths: ["a", "b", "c"]).summary
        XCTAssertTrue(summary.contains("3 binaries are still encrypted"), summary)
    }

    /// The cause is always the same one thing, and the fix is a control this
    /// app actually has. Both have to be in the copy.
    func test_encrypted_advice_says_where_to_get_a_decrypted_ipa() {
        let advice = encryptedRecord().advice
        // The two ways the app itself can produce one. "App Store" alone
        // would prove nothing: the opening sentence names it either way.
        XCTAssertTrue(advice.contains("Get one from the App Store and decrypt it on this Mac"), advice)
        XCTAssertTrue(advice.contains("dump it from a jailbroken device"), advice)
        XCTAssertTrue(advice.contains("Source IPA"), advice)
        XCTAssertFalse(advice.lowercased().contains("cryptid"), advice)
    }

    func test_encrypted_path_line_names_every_binary() {
        let record = encryptedRecord(paths: ["Payload/Example.app/Example", "Payload/Example.app/x.dylib"])
        XCTAssertEqual(record.pathLine, "Payload/Example.app/Example · Payload/Example.app/x.dylib")
    }

    /// A CLI that counted without naming leaves the card with nothing to put
    /// in its third row, and an empty row is a layout bug.
    func test_encrypted_path_line_is_nil_when_no_binary_is_named() {
        XCTAssertNil(encryptedRecord(count: 2, paths: []).pathLine)
        XCTAssertEqual(encryptedRecord(count: 2, paths: []).logLine,
                       "✗ 2 binaries are still encrypted — example will crash on launch")
    }

    func test_encrypted_log_line_carries_the_paths() {
        let line = encryptedRecord(count: 1, paths: ["Payload/Example.app/Example"]).logLine
        XCTAssertTrue(line.hasPrefix("✗ "), line)
        XCTAssertTrue(line.hasSuffix(": Payload/Example.app/Example"), line)
    }

    // MARK: - livecontainer-choice
    //
    // LiveContainer has shipped under several bundle ids and they sit side by
    // side on one phone. A push writes the IPA into exactly one container's
    // Documents/, and the import is manual — so opening the other one shows no
    // new IPA and the build looks like it silently did nothing. This is the
    // project's oldest reported gotcha and it had no machine-readable channel.

    private static let lcChoiceLine = #"""
    {"type":"livecontainer-choice","app":"example","bundle_id":"com.kdt.livecontainer.ABCDE12345","others":["com.kdt.LiveContainer2.AB12CD34EF"]}
    """#

    func test_decode_livecontainer_choice_event() throws {
        guard case let .liveContainerChoice(record) =
                try BuildEvent.decode(line: Self.lcChoiceLine) else {
            return XCTFail("expected .liveContainerChoice")
        }
        XCTAssertEqual(record.app, "example")
        XCTAssertEqual(record.bundleId, "com.kdt.livecontainer.ABCDE12345")
        XCTAssertEqual(record.others, ["com.kdt.LiveContainer2.AB12CD34EF"])
    }

    /// It has to reach a panel, which means `progress` — the projection is all
    /// the panels look at.
    func test_progress_carries_the_livecontainer_choice() throws {
        let event = try BuildEvent.decode(line: Self.lcChoiceLine)
        guard case let .liveContainerChoice(record) = event.progress else {
            return XCTFail("expected .liveContainerChoice, got "
                           + String(describing: event.progress))
        }
        XCTAssertEqual(record.bundleId, "com.kdt.livecontainer.ABCDE12345")
    }

    /// The line has to name the container to open. A count of how many are
    /// installed is not an instruction.
    func test_livecontainer_choice_line_names_the_container_to_import_from() throws {
        guard case let .liveContainerChoice(record) =
                try BuildEvent.decode(line: Self.lcChoiceLine) else {
            return XCTFail("expected .liveContainerChoice")
        }
        let line = record.logLine
        XCTAssertTrue(line.contains("com.kdt.livecontainer.ABCDE12345"), line)
        XCTAssertTrue(line.contains("com.kdt.LiveContainer2.AB12CD34EF"), line)
        XCTAssertTrue(line.contains("example"), line)
    }

    func test_livecontainer_choice_line_is_plural_for_several_others() {
        let record = LiveContainerChoiceRecord(
            app: "example",
            bundleId: "com.kdt.livecontainer.A",
            others: ["com.kdt.LiveContainer2.B", "com.kdt.LiveContainer3.C"]
        )
        XCTAssertTrue(record.logLine.contains("2 other LiveContainers"), record.logLine)
    }

    // MARK: - the replayed build log
    //
    // `ConfigStore.encryptedBinaries` is session-scoped — the CLI records
    // nothing about the finding, so there is nothing to read back at launch.
    // The build log on disk is teed from the same stream, which makes this
    // replay the only place the warning can survive a relaunch. It switched
    // over `BuildEvent` with a `default: return []`, so when the new
    // `BuildProgress` case went in — with a docstring saying every panel that
    // ships an IPA to a phone would have to decide about it — this panel
    // compiled unchanged and dropped it.

    private func replayed(_ line: String) throws -> [LogLine] {
        BuildLogView.lines(for: try BuildEvent.decode(line: line))
    }

    func test_replayed_log_renders_the_encrypted_binaries_finding() throws {
        let lines = try replayed(Self.encryptedLine)
        XCTAssertEqual(lines.count, 1, "the only trace of this that outlives a relaunch")
        XCTAssertEqual(lines.first?.kind, .warn)
        XCTAssertTrue(
            lines.first?.text.contains("crash on launch") ?? false,
            lines.first?.text ?? "<nothing>"
        )
    }

    /// The transcript it has to sit in is otherwise entirely green — the build
    /// succeeded, which is the whole shape of the defect.
    func test_replayed_log_keeps_the_finding_above_the_green_finish() {
        let stream = """
        {"type":"build-start","app":"example","bundle_id":"com.example.app","delivery":"livecontainer"}
        {"type":"stage","name":"verify","status":"start"}
        \(Self.encryptedLine)
        {"type":"stage","name":"verify","status":"end","binaries":24,"bad":1}
        {"type":"build-end","app":"example","ok":true,"elapsed_seconds":84.3}
        """
        let texts = stream.buildEvents().flatMap { BuildLogView.lines(for: $0) }.map(\.text)
        guard let warned = texts.firstIndex(where: { $0.contains("still encrypted") }) else {
            return XCTFail("the finding never reached the replayed log: \(texts)")
        }
        guard let done = texts.firstIndex(where: { $0.hasPrefix("✓ done") }) else {
            return XCTFail("expected the green finish: \(texts)")
        }
        XCTAssertLessThan(warned, done)
    }

    func test_replayed_log_labels_a_null_delivery_rather_than_guessing() throws {
        let lines = try replayed(
            #"{"type":"build-start","app":"gone","bundle_id":"com.example.gone","delivery":null}"#
        )
        XCTAssertEqual(lines.first?.text, "→ build start: gone (delivery unknown)")
    }

    /// Events outside the build stream stay out of the transcript: a replayed
    /// log contains `build-record` and one `build-pruned` per swept archive,
    /// and rendering those would bury the build under housekeeping.
    func test_replayed_log_ignores_events_outside_the_build_stream() throws {
        XCTAssertTrue(try replayed(
            #"{"type":"build-pruned","app":"example","filename":"example-1.ipa","path":"/b/example-1.ipa","size_bytes":10,"trash_path":null,"build_ids":[],"dry_run":true}"#
        ).isEmpty)
        XCTAssertTrue(BuildLogView.lines(
            for: .unknown(type: "brand-new", raw: ["type": "brand-new"])
        ).isEmpty)
    }

    // MARK: - BuildProgress projection
    //
    // `progress` is what the build panels switch over instead of switching
    // over all of BuildEvent. The mapped four must survive; everything else
    // must stay nil, so that adding a BuildEvent case never reaches a view.

    func test_progress_maps_build_start_and_drops_bundle_id() {
        let event = BuildEvent.buildStart(
            app: "example",
            bundleId: "com.example.app",
            delivery: .livecontainer
        )
        XCTAssertEqual(event.progress, .start(app: "example", delivery: .livecontainer))
    }

    func test_progress_maps_stage_and_drops_fields() {
        let event = BuildEvent.stage(
            name: "lc-push",
            status: "end",
            fields: ["bundle_id": "com.kdt.livecontainer", "size_bytes": 1024]
        )
        XCTAssertEqual(event.progress, .stage(name: "lc-push", status: "end"))
    }

    func test_progress_maps_log() {
        let event = BuildEvent.log(source: "cyan", line: "injecting tweak")
        XCTAssertEqual(event.progress, .log(source: "cyan", line: "injecting tweak"))
    }

    // MARK: - Archive retention

    func test_decode_build_pruned_event() throws {
        let json = #"{"type":"build-pruned","app":"example","filename":"example-19.1.ipa","path":"/b/example-19.1.ipa","size_bytes":412000000,"trash_path":"/T/example-19.1.ipa","build_ids":[3,4],"dry_run":false}"#
        let event = try BuildEvent.decode(line: json)
        guard case let .buildPruned(record) = event else {
            return XCTFail("expected .buildPruned, got \(event)")
        }
        XCTAssertEqual(record.app, "example")
        XCTAssertEqual(record.filename, "example-19.1.ipa")
        XCTAssertEqual(record.sizeBytes, 412_000_000)
        XCTAssertEqual(record.trashPath, "/T/example-19.1.ipa")
        XCTAssertEqual(record.buildIds, [3, 4])
        XCTAssertFalse(record.dryRun)
    }

    /// A dry run reports `trash_path: null` — nothing moved, so there is
    /// nowhere for it to have landed.
    func test_decode_build_pruned_event_dry_run_has_no_trash_path() throws {
        let json = #"{"type":"build-pruned","app":"example","filename":"example-1.ipa","path":"/b/example-1.ipa","size_bytes":10,"trash_path":null,"build_ids":[],"dry_run":true}"#
        let event = try BuildEvent.decode(line: json)
        guard case let .buildPruned(record) = event else {
            return XCTFail("expected .buildPruned")
        }
        XCTAssertNil(record.trashPath)
        XCTAssertTrue(record.dryRun)
    }

    func test_decode_prune_end_event() throws {
        let json = #"{"type":"prune-end","keep":5,"dry_run":false,"removed":2,"kept":5,"bytes_removed":824000000,"unparsed":1,"failed":0}"#
        let event = try BuildEvent.decode(line: json)
        guard case let .pruneEnd(summary) = event else {
            return XCTFail("expected .pruneEnd, got \(event)")
        }
        XCTAssertEqual(summary.keep, 5)
        XCTAssertEqual(summary.removed, 2)
        XCTAssertEqual(summary.kept, 5)
        XCTAssertEqual(summary.bytesRemoved, 824_000_000)
        XCTAssertEqual(summary.unparsed, 1)
        XCTAssertEqual(summary.failed, 0)
    }

    /// The overwhelmingly common case after a build: the policy had nothing to
    /// remove. A line saying so every single time is noise, so there isn't one.
    func test_prune_summary_renders_no_line_when_nothing_happened() {
        let summary = PruneSummary(
            keep: 5, dryRun: false, removed: 0, kept: 3,
            bytesRemoved: 0, unparsed: 0, failed: 0
        )
        XCTAssertNil(summary.logSummary)
    }

    func test_prune_summary_line_names_count_size_and_policy() throws {
        let summary = PruneSummary(
            keep: 5, dryRun: false, removed: 2, kept: 5,
            bytesRemoved: 824_000_000, unparsed: 0, failed: 0
        )
        let line = try XCTUnwrap(summary.logSummary)
        XCTAssertTrue(line.contains("moved 2 archives"), line)
        XCTAssertFalse(line.contains("would move"), line)
        XCTAssertTrue(line.contains("keeping the newest 5 per app"), line)
        // Whatever the locale's byte formatting is, the magnitude must show.
        XCTAssertTrue(line.contains("MB") || line.contains("GB"), line)
    }

    func test_prune_summary_line_says_would_move_on_a_dry_run() throws {
        let summary = PruneSummary(
            keep: 1, dryRun: true, removed: 1, kept: 1,
            bytesRemoved: 1024, unparsed: 0, failed: 0
        )
        let line = try XCTUnwrap(summary.logSummary)
        XCTAssertTrue(line.contains("would move 1 archive"), line)
    }

    /// A sweep that couldn't move a file still gets a line, even when it moved
    /// nothing: the CLI's per-file `log` events explain why, and a silent
    /// summary would leave those looking unexplained.
    func test_prune_summary_line_reports_failures() throws {
        let summary = PruneSummary(
            keep: 5, dryRun: false, removed: 0, kept: 4,
            bytesRemoved: 0, unparsed: 0, failed: 2
        )
        let line = try XCTUnwrap(summary.logSummary)
        XCTAssertTrue(line.contains("2 couldn't be moved"), line)
    }

    /// The sweep runs inside a successful build, ahead of `build-end` — so the
    /// summary is part of the build stream, and the per-file events are not.
    func test_progress_carries_the_prune_summary_but_not_each_file() {
        let stream = """
        {"type":"build-start","app":"example","bundle_id":"com.example.app","delivery":"sideload"}
        {"type":"build-pruned","app":"example","filename":"example-19.0.ipa","path":"/b/example-19.0.ipa","size_bytes":5,"trash_path":"/T/example-19.0.ipa","build_ids":[1],"dry_run":false}
        {"type":"prune-end","keep":1,"dry_run":false,"removed":1,"kept":1,"bytes_removed":5,"unparsed":0,"failed":0}
        {"type":"build-end","app":"example","ok":true,"elapsed_seconds":3.0}
        """
        let progress = stream.buildEvents().compactMap(\.progress)
        XCTAssertEqual(progress, [
            .start(app: "example", delivery: .sideload),
            .pruned(PruneSummary(
                keep: 1, dryRun: false, removed: 1, kept: 1,
                bytesRemoved: 5, unparsed: 0, failed: 0
            )),
            .finished(ok: true, error: nil, elapsed: 3.0),
        ])
    }

    func test_progress_maps_build_end_success_and_drops_app() {
        let event = BuildEvent.buildEnd(app: "example", ok: true, error: nil, elapsedSeconds: 12.5)
        XCTAssertEqual(event.progress, .finished(ok: true, error: nil, elapsed: 12.5))
    }

    func test_progress_maps_build_end_failure() {
        let event = BuildEvent.buildEnd(app: "example", ok: false, error: "cyan failed", elapsedSeconds: nil)
        XCTAssertEqual(event.progress, .finished(ok: false, error: "cyan failed", elapsed: nil))
    }

    func test_progress_is_nil_for_non_build_stream_events() {
        let tweak = TweakRecord(filename: "a.deb", sizeBytes: 10, usedBy: ["example"])
        XCTAssertNil(BuildEvent.tweak(tweak).progress)

        let removed = AppRemovedRecord(name: "bravo", bundleId: "com.bravo.app")
        XCTAssertNil(BuildEvent.appRemoved(removed).progress)

        XCTAssertNil(BuildEvent.devicesEnd(count: 2).progress)
        XCTAssertNil(BuildEvent.unknown(type: "brand-new", raw: ["type": "brand-new"]).progress)
    }

    // MARK: - artwork
    //
    // The CLI's icon cascade, one event per resolved app. `source` is a bare
    // String on purpose — nothing here branches on it — which is the thing
    // these tests exist to pin.

    func test_decode_artwork_event() throws {
        let line = #"{"type":"artwork","bundle_id":"com.example.app","path":"/tmp/artwork/com.example.app.png","source":"ipa","cached":false}"#
        guard case let .artwork(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .artwork")
        }
        XCTAssertEqual(record.bundleId, "com.example.app")
        XCTAssertEqual(record.path, "/tmp/artwork/com.example.app.png")
        XCTAssertEqual(record.source, "ipa")
        XCTAssertFalse(record.cached)
    }

    /// `source` must stay a `String`: an unrecognized value still has to
    /// decode, because the CLI is free to add a fifth source without an older
    /// GUI build refusing the whole line for it. This is what would break if
    /// `source` were ever turned into an enum.
    func test_decode_artwork_event_with_an_unrecognised_source() throws {
        let line = #"{"type":"artwork","bundle_id":"com.example.new","path":"/tmp/artwork/com.example.new.png","source":"future-source","cached":true}"#
        guard case let .artwork(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .artwork to decode regardless of source")
        }
        XCTAssertEqual(record.source, "future-source")
        XCTAssertTrue(record.cached)
    }

    /// Same idiom as `device`: `ArtworkRecord` has no custom `init(from:)`, so
    /// a missing required field fails the whole decode rather than being
    /// filled in with a fabricated default.
    func test_decode_artwork_event_missing_a_required_field_throws() {
        let line = #"{"type":"artwork","bundle_id":"com.example.new","path":"/tmp/artwork/com.example.new.png","source":"ipa"}"#
        XCTAssertThrowsError(try BuildEvent.decode(line: line))
    }

    /// Decoded lines, not hand-built cases — the projection has to survive the
    /// real path from CLI stdout to panel.
    func test_progress_over_a_decoded_build_stream() {
        let stream = """
        {"type":"build-start","app":"example","bundle_id":"com.example.app","delivery":"sideload"}
        {"type":"app-removed","name":"bravo","bundle_id":"com.bravo.app"}
        {"type":"stage","name":"cyan","status":"start"}
        {"type":"log","source":"cyan","line":"working"}
        {"type":"build-end","app":"example","ok":true,"elapsed_seconds":3.0}
        """
        let progress = stream.buildEvents().compactMap(\.progress)
        XCTAssertEqual(progress, [
            .start(app: "example", delivery: .sideload),
            .stage(name: "cyan", status: "start"),
            .log(source: "cyan", line: "working"),
            .finished(ok: true, error: nil, elapsed: 3.0),
        ])
    }

    // MARK: - device / is_default

    func test_decode_device_event_with_is_default() throws {
        let line = #"{"type":"device","udid":"U","name":"iPhone 15 Pro","model":"iPhone16,1","ios_version":"26.1","connection_type":"Network","livecontainer_bundle_id":null,"is_default":true}"#
        guard case let .device(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .device")
        }
        XCTAssertTrue(record.isDefault)
    }

    /// An older `graft` on PATH does not emit the field. The row must still
    /// decode — a phone vanishing from the list because of a missing chip would be
    /// far worse than a missing chip, and this app already spent months on the
    /// class of bug where a device silently fails to appear.
    func test_a_device_event_without_is_default_still_decodes() throws {
        let line = #"{"type":"device","udid":"U","name":"iPhone 15 Pro","model":"iPhone16,1","ios_version":"26.1","connection_type":"Network","livecontainer_bundle_id":null}"#
        guard case let .device(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .device")
        }
        XCTAssertFalse(record.isDefault)
    }

    // MARK: - device-brief / device-info
    //
    // Lines captured live against an attached iPhone 15 Pro, not hand-built —
    // a snake_case typo in a hand-written fixture agrees with itself.

    func test_decode_device_brief_event() throws {
        let line = #"{"type":"device-brief","udid":"00008030-0000000000000001","connection_type":"Network"}"#
        guard case let .deviceBrief(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .deviceBrief")
        }
        XCTAssertEqual(record.udid, "00008030-0000000000000001")
        XCTAssertEqual(record.connectionType, .network)
    }

    func test_decode_device_info_event_fully_populated() throws {
        let line = #"{"type":"device-info","udid":"00008030-0000000000000001","reachable":true,"total_bytes":128000000000,"free_bytes":35728093184,"battery_percent":70,"battery_charging":false}"#
        guard case let .deviceInfo(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .deviceInfo")
        }
        XCTAssertEqual(record.udid, "00008030-0000000000000001")
        XCTAssertTrue(record.reachable)
        XCTAssertEqual(record.totalBytes, 128_000_000_000)
        XCTAssertEqual(record.freeBytes, 35_728_093_184)
        XCTAssertEqual(record.batteryPercent, 70)
        XCTAssertEqual(record.batteryCharging, false)
    }

    /// The locked/absent-phone case: `reachable: false` plus four nulls. This
    /// must decode, not throw — the CLI emits the event precisely so silence
    /// stays distinguishable from a crashed CLI.
    func test_decode_device_info_event_unreachable_with_nulls() throws {
        let line = #"{"type":"device-info","udid":"DOES-NOT-EXIST","reachable":false,"total_bytes":null,"free_bytes":null,"battery_percent":null,"battery_charging":null}"#
        guard case let .deviceInfo(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .deviceInfo to decode even when every optional is null")
        }
        XCTAssertEqual(record.udid, "DOES-NOT-EXIST")
        XCTAssertFalse(record.reachable)
        XCTAssertNil(record.totalBytes)
        XCTAssertNil(record.freeBytes)
        XCTAssertNil(record.batteryPercent)
        XCTAssertNil(record.batteryCharging)
    }

    /// `developer_mode` (spec §5): true or false on iOS 16 and later, null
    /// before. A CLI from before 2.3 sends no key, which is no reading, not a
    /// decode failure.
    func test_decode_device_info_developer_mode() throws {
        let base = #"{"type":"device-info","udid":"00008030-0000000000000001","reachable":true,"total_bytes":1,"free_bytes":1,"battery_percent":1,"battery_charging":false"#
        let cases: [(String, Bool?)] = [
            (#","developer_mode":true}"#, true),
            (#","developer_mode":false}"#, false),
            (#","developer_mode":null}"#, nil),
            ("}", nil),
        ]
        for (tail, expected) in cases {
            guard case let .deviceInfo(record) = try BuildEvent.decode(line: base + tail) else {
                return XCTFail("expected .deviceInfo for \(tail)")
            }
            XCTAssertEqual(record.developerMode, expected, tail)
        }
    }

    /// Pins the separate-type decision itself: a brief event must not decode
    /// as `.device`, and a full `device` event must not decode as
    /// `.deviceBrief`. If someone later merges `BriefDeviceRecord` into
    /// `DeviceRecord` behind a presence check, one of these two switches stops
    /// matching and this test fails.
    func test_device_brief_and_full_device_are_not_interchangeable() throws {
        let briefLine = #"{"type":"device-brief","udid":"00008030-0000000000000001","connection_type":"Network"}"#
        let fullLine = #"{"type":"device","udid":"GOOD","name":"Test iPhone","model":"iPhone14,2","ios_version":"26.0.1","connection_type":"USB","livecontainer_bundle_id":null}"#

        let briefEvent = try BuildEvent.decode(line: briefLine)
        if case .device = briefEvent {
            XCTFail("a device-brief line decoded as .device")
        }
        guard case .deviceBrief = briefEvent else {
            return XCTFail("expected .deviceBrief")
        }

        let fullEvent = try BuildEvent.decode(line: fullLine)
        if case .deviceBrief = fullEvent {
            XCTFail("a full device line decoded as .deviceBrief")
        }
        guard case .device = fullEvent else {
            return XCTFail("expected .device")
        }
    }

    // MARK: - device-app icon_path

    /// `icon_path` is absent, not null, when `--with-icons` wasn't asked for
    /// or the cascade found nothing — the row must still decode.
    func test_decode_device_app_event_without_icon_path() throws {
        let line = #"{"type":"device-app","bundle_id":"com.alpha.app","name":"Alpha","version":"27.13.0","encrypted":true,"frontmost":false}"#
        guard case let .deviceApp(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .deviceApp")
        }
        XCTAssertNil(record.iconPath)
    }

    func test_decode_device_app_event_with_icon_path() throws {
        let line = #"{"type":"device-app","bundle_id":"com.alpha.app","name":"Alpha","version":"27.13.0","encrypted":true,"frontmost":false,"icon_path":"/Users/exampleuser/.cache/graft/artwork/com.alpha.app.png"}"#
        guard case let .deviceApp(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .deviceApp")
        }
        XCTAssertEqual(record.iconPath, "/Users/exampleuser/.cache/graft/artwork/com.alpha.app.png")
    }

    // MARK: - staged-ipa

    func test_a_staged_ipa_event_decodes_every_field() throws {
        let line = #"{"type":"staged-ipa","bundle_id":"com.example.app","version":"1.2.3","display_name":"Example","filename":"com.example.app_1.2.3.ipa","path":"/c/com.example.app_1.2.3.ipa","size_bytes":4096,"preferred":true,"stripped":["Payload/Fake.app/PlugIns/Widget.appex"],"importance":{"Payload/Fake.app/PlugIns/Widget.appex":"cosmetic"}}"#
        let event = try BuildEvent.decode(line: line)
        guard case let .stagedIPA(record) = event else {
            return XCTFail("expected .stagedIPA, got \(event)")
        }
        XCTAssertEqual(record.bundleId, "com.example.app")
        XCTAssertEqual(record.version, "1.2.3")
        XCTAssertEqual(record.displayName, "Example")
        XCTAssertEqual(record.filename, "com.example.app_1.2.3.ipa")
        XCTAssertEqual(record.path, "/c/com.example.app_1.2.3.ipa")
        XCTAssertEqual(record.sizeBytes, 4096)
        XCTAssertTrue(record.preferred)
        XCTAssertEqual(record.stripped, ["Payload/Fake.app/PlugIns/Widget.appex"])
        XCTAssertNil(record.lost, "no key means unrecorded, and nil is how that is spelled")
        // A populated `importance` round-tripping is what would catch a
        // renamed key or a type change: the `?? [:]` default below degrades
        // silently, so a test that only ever sees the absent-key case could
        // never fail if the decode itself broke.
        XCTAssertEqual(record.importance, ["Payload/Fake.app/PlugIns/Widget.appex": "cosmetic"])
    }

    /// The distinction the whole sidecar change exists for, asserted at the
    /// decoder: an absent `lost` must not arrive as an empty array.
    func test_an_absent_lost_key_is_nil_and_an_empty_one_is_empty() throws {
        let absent = #"{"type":"staged-ipa","bundle_id":"a","version":"1","display_name":"A","filename":"a.ipa","path":"/a.ipa","size_bytes":1,"preferred":true,"stripped":["X.appex"]}"#
        let empty = #"{"type":"staged-ipa","bundle_id":"a","version":"1","display_name":"A","filename":"a.ipa","path":"/a.ipa","size_bytes":1,"preferred":true,"stripped":["X.appex"],"lost":[]}"#

        let decodedAbsent = try BuildEvent.decode(line: absent)
        let decodedEmpty = try BuildEvent.decode(line: empty)
        guard case let .stagedIPA(unrecorded) = decodedAbsent,
              case let .stagedIPA(recorded) = decodedEmpty else {
            return XCTFail("both lines should decode as .stagedIPA")
        }
        XCTAssertNil(unrecorded.lost)
        XCTAssertEqual(recorded.lost, [])
    }

    func test_a_staged_ipas_end_event_carries_the_count() throws {
        let line = #"{"type":"staged-ipas-end","count":10}"#
        guard case let .stagedIPAsEnd(count) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .stagedIPAsEnd")
        }
        XCTAssertEqual(count, 10)
    }

    func test_the_setup_events_decode() throws {
        let step = try BuildEvent.decode(line: #"{"type":"init-step","step":"config","detail":"wrote /tmp/x/config.yaml"}"#)
        guard case let .initStep(record) = step else { return XCTFail("\(step)") }
        XCTAssertEqual(record.detail, "wrote /tmp/x/config.yaml")

        let line = #"{"type":"init-result","account":"paid","config":"/tmp/x/config.yaml","change":"created","dry_run":true,"summary":"Everything checks out.","team_id":"ABCDE12345","profile_name":"Test Wildcard","certificate":"Apple Development: Test Person (XYZ9876543)","device_count":2,"devices":["00008130-001A2B3C0123456D","00008110-000E1C2A3B4C5D6E"],"expires":"2027-06-01T12:00:00Z","pinned":[],"sideloads":[],"steps":[]}"#
        guard case let .initResult(result) = try BuildEvent.decode(line: line) else { return XCTFail(line) }
        XCTAssertEqual(result.account, .paid)
        XCTAssertTrue(result.dryRun)
        XCTAssertEqual(result.teamId, "ABCDE12345")
        XCTAssertEqual(result.devices.count, 2)
        XCTAssertNotNil(result.expiryDate)
    }

    func test_a_free_step_names_each_page_it_sends_you_to() {
        let step = "Install SideStore (https://sidestore.io), or use iloader "
            + "(https://github.com/nab138/iloader), with your free Apple ID."
        XCTAssertEqual(FreeSetupStep.links(in: step).map(\.absoluteString),
                       ["https://sidestore.io", "https://github.com/nab138/iloader"])
        XCTAssertEqual(FreeSetupStep.links(in: "Install LiveContainer: https://example.com/guide.").map(\.absoluteString),
                       ["https://example.com/guide"])
        XCTAssertEqual(FreeSetupStep.links(in: "In LiveContainer, open Settings."), [])
    }

    // MARK: graft --update

    /// An app on two devices (contract G): the iPhone installs, the iPad
    /// waits for its device.
    private static let planLine = #"{"type":"update-plan","quick":true,"apps":[{"app":"example","display_name":"Example+","store":{"from":"1.0","to":"2.0","skipped_reason":null},"tweaks":[{"filename":"a-tweak.deb","name":"A Tweak","from":"1.0","to":"1.1","unchecked":false,"also_affects":["bravo"],"source":"https://repo.example.com","verified":true,"unverified_reason":null,"confirmed":true,"confirm_key":"a-tweak.deb@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","trust_changed":false},{"filename":"b-tweak.deb","name":null,"from":"2.0","to":null,"unchecked":true,"also_affects":[],"source":null,"verified":false,"unverified_reason":null,"confirmed":false}],"needs_confirmation":false,"install":true,"install_skipped_reason":null,"manual_install":false,"blocked_reason":null,"readiness":"likely","readiness_note":null,"actionable":true,"targets":[{"udid":"00000000-0000000000000001","device_name":"Example iPhone","delivery":"livecontainer","lc_bundle_id":"com.example.livecontainer","install":true,"install_skipped_reason":null,"manual_install":false,"waiting":null,"blocked_reason":null,"readiness":"checked","readiness_note":null},{"udid":"00000000-0000000000000002","device_name":"Example iPad","delivery":"livecontainer","lc_bundle_id":null,"install":false,"install_skipped_reason":null,"manual_install":false,"waiting":"Example iPad isn't connected: it'll update when it is","blocked_reason":null,"readiness":"likely","readiness_note":null}],"nothing_to_do":false}]}"#

    func test_decode_update_plan() throws {
        guard case let .updatePlan(record) = try BuildEvent.decode(line: Self.planLine) else {
            return XCTFail("expected .updatePlan")
        }
        XCTAssertTrue(record.quick)
        let app = try XCTUnwrap(record.apps.first)
        XCTAssertEqual(app.app, "example")
        XCTAssertEqual(app.displayName, "Example+")
        XCTAssertEqual(app.store.to, "2.0")
        XCTAssertTrue(app.getsFromStore)
        XCTAssertEqual(app.tweakUpdates.map(\.filename), ["a-tweak.deb"], "an unchecked tweak is not an update")
        XCTAssertEqual(app.tweaks.first?.alsoAffects, ["bravo"])
        XCTAssertEqual(app.tweaks.first?.source, "https://repo.example.com")
        XCTAssertEqual(app.tweaks.first?.verified, true)
        XCTAssertEqual(app.tweaks.first?.confirmed, true)
        XCTAssertNil(app.tweaks.first?.unverifiedReason)
        XCTAssertEqual(app.tweaks.first?.confirmKey, UpdatePlanFixture.key("a-tweak.deb"))
        XCTAssertEqual(app.tweaks.first?.trustChanged, false)
        XCTAssertNil(app.tweaks.last?.confirmKey, "an unchecked tweak has nothing to confirm")
        XCTAssertFalse(app.needsConfirmation)
        XCTAssertEqual(app.readiness, CLIContract.Update.readinessLikely)
        XCTAssertFalse(app.manualInstall)
        XCTAssertNil(app.readinessNote)
        XCTAssertTrue(app.actionable)
        XCTAssertTrue(app.isActionable)
        XCTAssertTrue(app.hasUpdate)
    }

    /// A tweak whose update isn't verified (2.3.1 A1): its source and why,
    /// and it isn't one of the app's updates until the person confirms it.
    func test_decode_an_unverified_tweak_update() throws {
        let line = UpdatePlanFixture.planLine([UpdatePlanFixture.app("example", tweaks: [
            UpdatePlanFixture.tweak("a.deb", name: "A Tweak", source: "http://repo.example.com", verified: false,
                                    reason: "http://repo.example.com doesn't use HTTPS"),
        ], install: false)])
        guard case let .updatePlan(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .updatePlan")
        }
        let app = try XCTUnwrap(record.apps.first)
        let tweak = try XCTUnwrap(app.tweaks.first)
        XCTAssertEqual(tweak.source, "http://repo.example.com")
        XCTAssertFalse(tweak.verified)
        XCTAssertFalse(tweak.confirmed)
        XCTAssertEqual(tweak.unverifiedReason, "http://repo.example.com doesn't use HTTPS")
        XCTAssertEqual(app.tweakUpdates, [], "not an update without the person's OK")
        XCTAssertEqual(app.unconfirmedTweaks.map(\.filename), ["a.deb"])
        XCTAssertTrue(app.needsConfirmation)
        XCTAssertFalse(app.isActionable, "nor in Update All")
        XCTAssertTrue(app.hasUpdate, "but shown")
    }

    /// A tweak entry that doesn't say it's verified or confirmed is neither:
    /// what's missing never reads as trust.
    func test_a_tweak_entry_without_the_trust_fields_is_not_confirmed() throws {
        let line = #"{"type":"update-plan","quick":true,"apps":[{"app":"example","display_name":null,"store":{"from":"1.0","to":null,"skipped_reason":null},"tweaks":[{"filename":"a.deb","name":null,"from":"1.0","to":"1.1","unchecked":false,"also_affects":[]}],"install":true,"install_skipped_reason":null,"manual_install":false,"blocked_reason":null,"readiness":"likely","readiness_note":null,"actionable":true,"targets":[],"nothing_to_do":false}]}"#
        guard case let .updatePlan(record) = try BuildEvent.decode(line: line) else {
            return XCTFail("expected .updatePlan")
        }
        let tweak = try XCTUnwrap(record.apps.first?.tweaks.first)
        XCTAssertFalse(tweak.verified)
        XCTAssertFalse(tweak.confirmed)
        XCTAssertNil(tweak.source)
        XCTAssertEqual(record.apps.first?.tweakUpdates, [])
    }

    /// Each device the app is on, in the CLI's order (the default first).
    func test_decode_update_plan_targets() throws {
        guard case let .updatePlan(record) = try BuildEvent.decode(line: Self.planLine) else {
            return XCTFail("expected .updatePlan")
        }
        let targets = try XCTUnwrap(record.apps.first?.targets)
        XCTAssertEqual(targets.map(\.udid), ["00000000-0000000000000001", "00000000-0000000000000002"])
        guard targets.count == 2 else { return }
        XCTAssertEqual(targets[0].deviceName, "Example iPhone")
        XCTAssertEqual(targets[0].delivery, DeliveryMode.livecontainer.rawValue)
        XCTAssertEqual(targets[0].lcBundleId, "com.example.livecontainer")
        XCTAssertTrue(targets[0].install)
        XCTAssertTrue(targets[0].isReady)
        XCTAssertTrue(targets[0].deliversToLiveContainer)
        XCTAssertEqual(targets[0].readiness, CLIContract.Update.readinessChecked)
        XCTAssertNil(targets[0].waiting)
        XCTAssertEqual(targets[1].waiting, "Example iPad isn't connected: it'll update when it is")
        XCTAssertFalse(targets[1].isReady)
        XCTAssertNil(targets[1].lcBundleId)
        XCTAssertNil(targets[1].blockedReason)
        XCTAssertNil(targets[1].installSkippedReason)
        XCTAssertFalse(targets[1].manualInstall)
    }

    func test_decode_the_run_events() throws {
        guard case let .updateStep(step) = try BuildEvent.decode(
            line: #"{"type":"update-step","app":null,"step":"tweaks","status":"start","detail":"1 tweak","udid":null,"device_name":null}"#
        ) else { return XCTFail("expected .updateStep") }
        XCTAssertNil(step.app)
        XCTAssertEqual(step.step, CLIContract.Update.stepTweaks)
        XCTAssertEqual(step.status, CLIContract.Update.statusStart)
        XCTAssertNil(step.udid)
        XCTAssertNil(step.deviceName)

        guard case let .updateStep(install) = try BuildEvent.decode(
            line: #"{"type":"update-step","app":"example","step":"install","status":"skipped","detail":"Example iPad isn't connected: it'll update when it is","udid":"00000000-0000000000000002","device_name":"Example iPad"}"#
        ) else { return XCTFail("expected .updateStep") }
        XCTAssertEqual(install.step, CLIContract.Update.stepInstall)
        XCTAssertEqual(install.udid, "00000000-0000000000000002")
        XCTAssertEqual(install.deviceName, "Example iPad")

        guard case let .updateStopping(stopping) = try BuildEvent.decode(
            line: #"{"type":"update-stopping","app":"example","step":"lc-push"}"#
        ) else { return XCTFail("expected .updateStopping") }
        XCTAssertEqual(stopping.step, CLIContract.Stage.liveContainerPush)

        guard case let .updateAppEnd(end) = try BuildEvent.decode(
            line: #"{"type":"update-app-end","app":"example","ok":false,"stopped":false,"done":["get"],"left":["build"],"error":"2.0 is downloaded; the build failed: cyan exited with code 1","targets":[{"udid":"00000000-0000000000000001","device_name":"Example iPhone","ok":false,"error":null,"left":["install"]}]}"#
        ) else { return XCTFail("expected .updateAppEnd") }
        XCTAssertEqual(end.done, [CLIContract.Update.stepGet])
        XCTAssertEqual(end.left, [CLIContract.Update.stepBuild])
        XCTAssertEqual(end.targets, [UpdateAppEndRecord.Target(udid: "00000000-0000000000000001",
                                                               deviceName: "Example iPhone", ok: false,
                                                               error: nil, left: [CLIContract.Update.stepInstall])])

        guard case let .updateEnd(summary) = try BuildEvent.decode(
            line: #"{"type":"update-end","ok":false,"stopped":true,"updated":["example"],"failed":[],"blocked":["bravo"],"not_started":["delta"],"waiting":2}"#
        ) else { return XCTFail("expected .updateEnd") }
        XCTAssertTrue(summary.stopped)
        XCTAssertEqual(summary.notStarted, ["delta"])
        XCTAssertEqual(summary.waiting, 2)
    }

    /// An `update-app-end` or `update-end` without the per-device fields
    /// still decodes: a Stop's re-armed kill and a quit's wait key off
    /// `update-app-end` (`RunStopper`), and must not hang on a list a CLI
    /// left out.
    func test_the_run_ends_decode_without_their_per_device_fields() throws {
        guard case let .updateAppEnd(end) = try BuildEvent.decode(
            line: #"{"type":"update-app-end","app":"example","ok":true,"stopped":false,"done":["build"],"left":[],"error":null}"#
        ) else { return XCTFail("expected .updateAppEnd") }
        XCTAssertEqual(end.targets, [])

        guard case let .updateEnd(summary) = try BuildEvent.decode(
            line: #"{"type":"update-end","ok":true,"stopped":false,"updated":[],"failed":[],"blocked":[],"not_started":[]}"#
        ) else { return XCTFail("expected .updateEnd") }
        XCTAssertEqual(summary.waiting, 0)
    }

    /// `waiting` (review part 2, #9): an install whose device went away
    /// during the run, on its step's end and its device's result. Left out,
    /// it reads as false, as an older CLI leaves it.
    func test_decode_waiting_on_an_install_step_and_a_devices_result() throws {
        guard case let .updateStep(step) = try BuildEvent.decode(
            line: #"{"type":"update-step","app":"example","step":"install","status":"end","detail":"Example iPad isn't connected: it'll update when it is","udid":"00000000-0000000000000002","device_name":"Example iPad","waiting":true}"#
        ) else { return XCTFail("expected .updateStep") }
        XCTAssertTrue(step.waiting)

        guard case let .updateStep(plain) = try BuildEvent.decode(
            line: #"{"type":"update-step","app":"example","step":"install","status":"end","detail":null,"udid":"00000000-0000000000000001","device_name":"Example iPhone"}"#
        ) else { return XCTFail("expected .updateStep") }
        XCTAssertFalse(plain.waiting)

        guard case let .updateAppEnd(end) = try BuildEvent.decode(
            line: #"{"type":"update-app-end","app":"example","ok":true,"stopped":false,"done":["build"],"left":[],"error":null,"targets":[{"udid":"00000000-0000000000000001","device_name":"Example iPhone","ok":true,"error":null,"left":[]},{"udid":"00000000-0000000000000002","device_name":"Example iPad","ok":false,"error":"Example iPad isn't connected: it'll update when it is","left":["install"],"waiting":true}]}"#
        ) else { return XCTFail("expected .updateAppEnd") }
        XCTAssertEqual(end.targets.map(\.waiting), [false, true])
    }

    func test_update_events_are_not_part_of_the_build_stream() throws {
        for line in [
            Self.planLine,
            #"{"type":"update-end","ok":true,"stopped":false,"updated":[],"failed":[],"blocked":[],"not_started":[],"waiting":0}"#,
            #"{"type":"update-target-dropped","app":"example","udid":"00008130-FAKE00000000PHONE2","device_name":null}"#,
        ] {
            XCTAssertNil(try BuildEvent.decode(line: line).progress)
        }
    }

    /// `--drop-update-target`'s one event (spec §11.3, "Devices that are
    /// gone"): the app, the device, and its name when Graft knows it.
    func test_decode_update_target_dropped() throws {
        guard case let .updateTargetDropped(named) = try BuildEvent.decode(
            line: #"{"type":"update-target-dropped","app":"example","udid":"00008130-FAKE00000000PHONE2","device_name":"Example iPad"}"#
        ) else { return XCTFail("expected .updateTargetDropped") }
        XCTAssertEqual(named, UpdateTargetDroppedRecord(
            app: "example", udid: "00008130-FAKE00000000PHONE2", deviceName: "Example iPad"))

        guard case let .updateTargetDropped(unnamed) = try BuildEvent.decode(
            line: #"{"type":"update-target-dropped","app":"example","udid":"00008130-FAKE00000000PHONE2","device_name":null}"#
        ) else { return XCTFail("expected .updateTargetDropped") }
        XCTAssertNil(unnamed.deviceName)
    }
}

// MARK: - C-STOP: a stopped build-end is a stop, not a failure

extension BuildEventTests {
    /// The CLI's own word that it was stopped (SIGTERM/SIGINT). `ok` is false
    /// on the wire and `error` null, and the old reading of exactly that pair
    /// was "failed" — the red "Last build failed / 143" this contract ends.
    func test_a_stopped_build_end_is_a_stop_not_a_failure() throws {
        let line = #"{"type":"build-end","app":"example","ok":false,"stopped":true,"error":null,"elapsed_seconds":4.2}"#
        let event = try BuildEvent.decode(line: line)
        guard case let .buildEnd(_, ok, _, _, stopped, _) = event else {
            return XCTFail("expected .buildEnd")
        }
        XCTAssertFalse(ok)
        XCTAssertTrue(stopped)
        XCTAssertEqual(event.progress, .stopped(elapsed: 4.2))
        XCTAssertTrue(BuildProgress.stopReported(in: [event]))
    }

    /// Absent is not stopped: every CLI from before C-STOP, and every real
    /// failure after it, still reads as the verdict it always was.
    func test_a_build_end_without_stopped_is_still_a_verdict() throws {
        let absent = try BuildEvent.decode(
            line: #"{"type":"build-end","app":"example","ok":false,"error":"cyan failed"}"#
        )
        XCTAssertEqual(absent.progress, .finished(ok: false, error: "cyan failed", elapsed: nil))
        let explicit = try BuildEvent.decode(
            line: #"{"type":"build-end","app":"example","ok":true,"stopped":false,"elapsed_seconds":3.0}"#
        )
        XCTAssertEqual(explicit.progress, .finished(ok: true, error: nil, elapsed: 3.0))
        XCTAssertFalse(BuildProgress.stopReported(in: [absent, explicit]))
    }

    /// The stop is found wherever it sits in the stream, and only a
    /// `build-end` can report one.
    func test_a_stop_is_found_among_the_rest_of_the_stream() {
        let stream = """
        {"type":"build-start","app":"example","bundle_id":"com.example.app","delivery":"livecontainer"}
        {"type":"stage","name":"cyan","status":"start"}
        {"type":"log","source":"cyan","line":"stopped"}
        {"type":"build-end","app":"example","ok":false,"stopped":true,"error":null}
        """
        XCTAssertTrue(BuildProgress.stopReported(in: stream.buildEvents()))
        XCTAssertFalse(BuildProgress.stopReported(in: Array(stream.buildEvents().dropLast())))
    }

    /// Replayed, a stop is amber and says so — never the `✗` of a failure.
    func test_a_replayed_stop_is_not_drawn_as_a_failure() throws {
        let event = try BuildEvent.decode(
            line: #"{"type":"build-end","app":"example","ok":false,"stopped":true,"error":null,"elapsed_seconds":4.2}"#
        )
        let lines = BuildLogView.lines(for: event)
        XCTAssertEqual(lines.map(\.text), ["■ stopped (4.2s)"])
        XCTAssertEqual(lines.first?.kind, .warn)
    }
}

// MARK: - K12 / C-DRYRUN: where a dry run would have pushed

extension BuildEventTests {
    func test_a_dry_runs_push_target_is_in_the_transcript() throws {
        let event = try BuildEvent.decode(line: #"{"type":"stage","name":"lc-push","status":"end","bundle_id":"com.kdt.livecontainer.ABCDE12345","size_bytes":117000000,"others":[],"dry_run":true,"documents_path":"Documents/example.ipa"}"#)
        let texts = BuildLogView.lines(for: event).map(\.text)
        XCTAssertEqual(texts.first, "  ✓ lc-push")
        XCTAssertEqual(texts.count, 2, "\(texts)")
        XCTAssertTrue(texts[1].hasPrefix("    would push "), texts[1])
        XCTAssertTrue(texts[1].hasSuffix(" to Documents/example.ipa in com.kdt.livecontainer.ABCDE12345"),
                      texts[1])
    }

    /// A real push, and a CLI from before C-DRYRUN, add nothing.
    func test_only_a_dry_run_says_would_push() throws {
        for line in [
            #"{"type":"stage","name":"lc-push","status":"end","bundle_id":"com.kdt.livecontainer.ABCDE12345","size_bytes":117000000,"others":[],"dry_run":false,"documents_path":"Documents/example.ipa"}"#,
            #"{"type":"stage","name":"lc-push","status":"end","bundle_id":"com.kdt.livecontainer.ABCDE12345","size_bytes":117000000,"others":[]}"#,
        ] {
            XCTAssertEqual(BuildLogView.lines(for: try BuildEvent.decode(line: line)).map(\.text),
                           ["  ✓ lc-push"], line)
        }
    }
}
