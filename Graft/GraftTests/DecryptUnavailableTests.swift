import SwiftUI
import XCTest
@testable import Graft

/// The device pane's "Decrypt an app" card on a phone without a jailbreak —
/// which is most phones.
///
/// It drew every readiness check in red with its fix underneath: frida not
/// reachable, SSH not answering, nothing on screen, each with a paragraph about
/// respringing and Sileo, on a phone that has never had either. When the SSH
/// check fails — the line that gates every dump backend — the card now says one
/// neutral sentence and where to get an IPA instead, with the checks behind a
/// disclosure for the user who does have a jailbreak.
@MainActor
final class DecryptUnavailableTests: XCTestCase {
    private static let udid = "00008030-0000000000000003"

    private static func readiness(
        frida: Bool, ssh: Bool, sshDetail: String = "no SSH server answered on the device",
        fridaDetail: String = "frida-server is not reachable: timed out"
    ) -> String {
        let ready = frida && ssh
        return """
        {"type":"decrypt-check","device":"\(udid)","device_name":"Example Phone","ready":\(ready),\
        "can_dump_from_list":\(ready),"recommended_backend":null,"frontmost_bundle_id":null,\
        "frontmost_name":null,"checks":[\
        {"key":"frida","label":"frida reachable","ok":\(frida),"detail":"\(fridaDetail)","fix":"either this phone has no jailbreak"},\
        {"key":"ssh","label":"SSH login","ok":\(ssh),"detail":"\(sshDetail)","fix":"either this phone has no jailbreak"},\
        {"key":"foreground","label":"App on screen","ok":false,"detail":"nothing on screen","fix":"unlock the phone","blocking":true}],\
        "backends":[{"name":"bagbak","usable":\(ready),"reason":"needs SSH"}]}
        """
    }

    private static func record(_ json: String) throws -> DecryptCheckRecord {
        guard case let .decryptCheck(r)? = json.buildEvents().first else {
            throw XCTSkip("fixture did not decode: \(json)")
        }
        return r
    }

    /// B7: the list's Dump was enabled on a stock phone and ended in frida or
    /// brew hints. It waits on what the card above found, saying the card's
    /// words; before the card's probe lands, it's no answer.
    func test_the_lists_dump_waits_on_a_device_the_card_found_unavailable() throws {
        let stock = try Self.record(Self.readiness(frida: false, ssh: false))
        let jailbroken = try Self.record(Self.readiness(frida: true, ssh: true))

        XCTAssertEqual(InstalledAppsSection.dumpUnavailable(stock, appleSilicon: true),
                       DeviceDecryptSection.unavailableLine)
        XCTAssertEqual(InstalledAppsSection.dumpUnavailable(stock, appleSilicon: false),
                       DeviceDecryptSection.unavailableLineWithoutAppStore)
        XCTAssertNil(InstalledAppsSection.dumpUnavailable(jailbroken, appleSilicon: true))
        XCTAssertNil(InstalledAppsSection.dumpUnavailable(nil, appleSilicon: true), "not probed yet")
    }

    func test_a_phone_that_answers_neither_channel_is_unavailable() throws {
        XCTAssertTrue(DeviceDecryptSection.isUnavailable(try Self.record(Self.readiness(frida: false, ssh: false))))
    }

    /// iOS 17+ answers frida over RemoteXPC with no jailbreak at all (see
    /// `decrypt_ready.check`), so a stock modern phone is frida-green and
    /// SSH-red. That is the commonest phone this card meets.
    func test_a_modern_stock_phone_reachable_by_frida_is_still_unavailable() throws {
        let r = try Self.record(Self.readiness(frida: true, ssh: false))
        XCTAssertFalse(r.ready, "premise")
        XCTAssertTrue(DeviceDecryptSection.isUnavailable(r))
    }

    /// A problem on this Mac — the frida client — is something the user can
    /// fix here, so the checks stay on show for it.
    func test_a_ready_or_ssh_reachable_phone_keeps_its_checks() throws {
        XCTAssertFalse(DeviceDecryptSection.isUnavailable(try Self.record(Self.readiness(frida: true, ssh: true))))
        XCTAssertFalse(DeviceDecryptSection.isUnavailable(try Self.record(Self.readiness(
            frida: false, ssh: true, fridaDetail: "frida module is not installed"
        ))))
    }

    /// The SSH check's three failures, as the CLI names them. Only "nothing
    /// answered" is a phone that cannot be dumped; a missing iproxy and a
    /// rejected root login keep the checks — and the Set root password button
    /// — on show.
    func test_only_no_server_collapses_the_card() throws {
        func readiness(reason: String, detail: String) throws -> DecryptCheckRecord {
            try Self.record(Self.readiness(frida: true, ssh: false, sshDetail: detail)
                .replacingOccurrences(
                    of: #""fix":"either this phone has no jailbreak"},{"key":"foreground""#,
                    with: #""fix":"x","reason":"\#(reason)"},{"key":"foreground""#
                ))
        }
        let noServer = try readiness(
            reason: CLIContract.DecryptCheck.SSHReason.noServer,
            detail: "no SSH server answered on the device"
        )
        XCTAssertEqual(
            noServer.checks.first { $0.key == CLIContract.DecryptCheck.ssh }?.reason,
            "no_server", "premise: the reason decodes"
        )
        XCTAssertTrue(DeviceDecryptSection.isUnavailable(noServer))
        XCTAssertFalse(DeviceDecryptSection.isUnavailable(try readiness(
            reason: CLIContract.DecryptCheck.SSHReason.noIproxy, detail: "iproxy is missing"
        )))
        XCTAssertFalse(DeviceDecryptSection.isUnavailable(try readiness(
            reason: CLIContract.DecryptCheck.SSHReason.loginRejected,
            detail: "SSH-2.0-OpenSSH_9.6, but the login was rejected"
        )))
        // The structured field decides over the sentence when both are there.
        XCTAssertFalse(DeviceDecryptSection.isUnavailable(try readiness(
            reason: CLIContract.DecryptCheck.SSHReason.loginRejected,
            detail: "no SSH server answered on the device"
        )))
    }

    /// A CLI from before `reason`: the detail sentence for the one outcome
    /// that collapses, and nothing else does.
    func test_an_older_cli_collapses_on_its_no_server_sentence_only() throws {
        XCTAssertTrue(DeviceDecryptSection.isUnavailable(try Self.record(
            Self.readiness(frida: true, ssh: false, sshDetail: "no SSH server answered on the device")
        )))
        for detail in ["iproxy is missing", "SSH-2.0-OpenSSH_9.6, but the login was rejected"] {
            XCTAssertFalse(DeviceDecryptSection.isUnavailable(try Self.record(
                Self.readiness(frida: true, ssh: false, sshDetail: detail)
            )), detail)
        }
    }

    func test_the_sentence_says_what_to_do_instead() {
        XCTAssertEqual(
            DeviceDecryptSection.unavailableLine,
            "On-device decryption needs a jailbroken device with frida-server. To get an IPA "
                + "without one, use Get latest from App Store on an app."
        )
    }

    /// The card itself: the one line, and none of the red fix text until the
    /// disclosure is opened. Read off the accessibility tree, which skips when
    /// macOS is not serving it (`AccessibilityTree`).
    func test_the_card_shows_one_line_instead_of_the_checks() async throws {
        try AccessibilityTree.requireServed()
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-decrypt-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let cli = dir.appendingPathComponent("graft")
        let json = Self.readiness(frida: true, ssh: false).replacingOccurrences(of: "'", with: "")
        try "#!/bin/sh\nprintf '%s\\n' '\(json)'\n".write(to: cli, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: cli.path)

        let device = try JSONDecoder().decode(DeviceRecord.self, from: Data(
            #"{"udid":"\#(Self.udid)","name":"Example Phone","model":"iPhone16,1","ios_version":"26.1","connection_type":"USB","is_default":false}"#.utf8
        ))
        let store = DeviceAppsStore(runner: CLIRunner(executable: cli))
        let hosted = hosted(
            DeviceDecryptSection(
                device: device, deviceApps: store, staged: [:], configuredByBundleId: [:],
                onAdd: { _ in }
            ),
            width: 600
        )
        let deadline = Date().addingTimeInterval(5)
        while store.readiness[Self.udid] == nil, Date() < deadline {
            try await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTAssertNotNil(store.readiness[Self.udid], "premise: the probe answered")
        try await Task.sleep(nanoseconds: 200_000_000)
        settle(hosted)

        let texts = try AccessibilityTree.texts(in: hosted.window)
        XCTAssertTrue(texts.contains(DeviceDecryptSection.unavailableLine), "\(texts)")
        XCTAssertFalse(
            texts.contains { $0.contains("no SSH server answered") },
            "the checks are behind the disclosure: \(texts)"
        )
    }
}

/// The SSH reasons the card branches on, checked against the CLI's own
/// module rather than a copy of it: the repo venv's Python imports
/// `graft_ipa.decrypt_ready` and prints the values. The Python suite pins the
/// same pair from its side (tests/test_decrypt_check_keys.py).
///
/// Not `--decrypt-check` against the installed CLI: that probes for a phone
/// over frida, which does not honour the test's usbmux isolation, and costs
/// eleven seconds with nothing plugged in.
final class DecryptCheckContractTests: XCTestCase {
    func test_the_ssh_reasons_are_the_clis_own() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // GraftTests
            .deletingLastPathComponent()   // Graft
            .deletingLastPathComponent()   // repo root
        let python = root.appendingPathComponent(".venv/bin/python")
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            throw XCTSkip("no .venv/bin/python in this checkout")
        }
        let proc = Process()
        proc.executableURL = python
        proc.currentDirectoryURL = root
        proc.arguments = ["-c", """
            import json
            from graft_ipa import decrypt_ready as d
            print(json.dumps({"no_server": d.SSH_NO_SERVER, "no_iproxy": d.SSH_NO_IPROXY,
                              "login_rejected": d.SSH_LOGIN_REJECTED}))
            """]
        proc.environment = ProcessEnvironment.forCLI()
        let out = Pipe()
        let err = Pipe()
        proc.standardOutput = out
        proc.standardError = err
        try proc.run()
        let data = out.fileHandleForReading.readDataToEndOfFile()
        let errors = String(decoding: err.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        proc.waitUntilExit()
        XCTAssertEqual(proc.terminationStatus, 0, errors)
        let values = try JSONDecoder().decode([String: String].self, from: data)
        XCTAssertEqual(values["no_server"], CLIContract.DecryptCheck.SSHReason.noServer)
        XCTAssertEqual(values["no_iproxy"], CLIContract.DecryptCheck.SSHReason.noIproxy)
        XCTAssertEqual(values["login_rejected"], CLIContract.DecryptCheck.SSHReason.loginRejected)
    }
}

/// R11: a backend that cannot run, with a note — the case that sets both —
/// shows both. `reason ?? note` dropped the note every time.
final class DecryptBackendLinesTests: XCTestCase {
    private func backends(_ json: String) throws -> [DecryptCheckRecord.Backend] {
        let line = #"{"type":"decrypt-check","device":"U","device_name":null,"ready":false,"can_dump_from_list":false,"recommended_backend":null,"frontmost_bundle_id":null,"frontmost_name":null,"checks":[],"backends":\#(json)}"#
        guard case let .decryptCheck(record)? = line.buildEvents().first else {
            throw XCTSkip("decrypt-check fixture did not decode")
        }
        return record.backends
    }

    func test_a_backend_with_a_reason_and_a_note_shows_both() throws {
        let lines = DeviceDecryptSection.backendLines(try backends(
            #"[{"name":"frida-ios-dump","usable":false,"reason":"frida-ios-dump is not set up","note":"git clone https://example.invalid/frida-ios-dump /x/frida-ios-dump"},{"name":"bagbak","usable":false,"reason":"bagbak is not installed","note":null}]"#
        ))
        XCTAssertEqual(lines, [
            .init(text: "frida-ios-dump: frida-ios-dump is not set up", isNote: false),
            .init(text: "git clone https://example.invalid/frida-ios-dump /x/frida-ios-dump", isNote: true),
            .init(text: "bagbak: bagbak is not installed", isNote: false),
        ])
    }

    func test_a_usable_backend_shows_only_its_caveat() throws {
        let lines = DeviceDecryptSection.backendLines(try backends(
            #"[{"name":"bagbak","usable":true,"reason":null,"note":"app extensions may need a second try"},{"name":"frida-ios-dump","usable":true,"reason":null,"note":null}]"#
        ))
        XCTAssertEqual(lines, [.init(text: "bagbak: app extensions may need a second try", isNote: false)])
    }
}
