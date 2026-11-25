import SwiftUI
import XCTest
@testable import Graft

/// Which Mac, and which download of Graft (`MacSupport`): the app with
/// everything, the app without what needs Apple silicon (and a banner for the
/// Intel download on Apple silicon), or one screen and nothing else running,
/// for the Apple silicon download on an Intel Mac and for a mixed-up bundle.
@MainActor
final class AppleSiliconGateTests: XCTestCase {
    // MARK: - Detection

    func test_hw_optional_arm64_of_one_is_apple_silicon() {
        var asked: [String] = []
        XCTAssertTrue(AppleSilicon.isPresent(sysctl: { asked.append($0); return 1 }))
        XCTAssertEqual(asked, ["hw.optional.arm64"])
    }

    func test_hw_optional_arm64_of_zero_is_intel() {
        XCTAssertFalse(AppleSilicon.isPresent(sysctl: { _ in 0 }))
    }

    /// Older Intel Macs have no such entry at all.
    func test_no_hw_optional_arm64_is_intel() {
        XCTAssertFalse(AppleSilicon.isPresent(sysctl: { _ in nil }))
    }

    /// The real reader answers nil, not 0 or a crash, for a name this Mac
    /// does not have: what an Intel Mac's missing entry looks like to it.
    func test_the_sysctl_reader_answers_nil_for_an_entry_that_is_not_there() {
        XCTAssertNil(AppleSilicon.readSysctl("hw.optional.graft-test-no-such-entry"))
    }

    // MARK: - Which download this is

    func test_no_key_is_the_apple_silicon_download() {
        XCTAssertEqual(MacSupport.build(infoValue: nil, python: .absent), .flavor(.arm64),
                       "a Debug or Xcode build: no key, no embedded Python")
        XCTAssertEqual(MacSupport.build(infoValue: nil, python: .architectures(["arm64"])), .flavor(.arm64),
                       "2.0.2 and 2.1: arm64 only, from before the key")
    }

    func test_the_key_names_the_download_once_the_python_agrees() {
        XCTAssertEqual(MacSupport.build(infoValue: "x86_64", python: .architectures(["x86_64"])), .flavor(.x86_64))
        XCTAssertEqual(MacSupport.build(infoValue: "arm64", python: .architectures(["arm64"])), .flavor(.arm64))
        XCTAssertEqual(MacSupport.build(infoValue: "x86_64", python: .architectures(["x86_64", "arm64"])),
                       .flavor(.x86_64), "a universal Python agrees with either")
        XCTAssertEqual(MacSupport.build(infoValue: "x86_64", python: .absent), .flavor(.x86_64))
    }

    func test_a_key_that_disagrees_with_the_python_is_mixed_up_never_a_guess() {
        XCTAssertEqual(
            MacSupport.build(infoValue: "x86_64", python: .architectures(["arm64"])),
            .mixedUp("its Info.plist says it is the build for Intel Macs (x86_64), and its embedded Python "
                     + "has code only for arm64")
        )
        for (key, python) in [(nil, ["x86_64"]), ("arm64", ["x86_64"]), ("arm64", ["arm64e"])] as [(String?, [String])] {
            guard case .mixedUp = MacSupport.build(infoValue: key, python: .architectures(python)) else {
                return XCTFail("\(String(describing: key)) with \(python) isn't mixed up")
            }
        }
        let notBuilds: [Any] = ["universal", "x86_64h", "", "ARM64", 5, ["arm64"]]
        for value in notBuilds {
            guard case .mixedUp = MacSupport.build(infoValue: value, python: .architectures(["arm64"])) else {
                return XCTFail("\(value) isn't a build")
            }
        }
        guard case .mixedUp = MacSupport.build(infoValue: "arm64", python: .unreadable) else {
            return XCTFail("an unreadable Python isn't any build")
        }
    }

    // MARK: - The four Macs and downloads

    private static let appleSiliconOnAppleSilicon = MacSupport(appleSiliconMac: true, build: .flavor(.arm64))
    private static let intelOnAppleSilicon = MacSupport(appleSiliconMac: true, build: .flavor(.x86_64))
    private static let appleSiliconOnIntel = MacSupport(appleSiliconMac: false, build: .flavor(.arm64))
    private static let intelOnIntel = MacSupport(appleSiliconMac: false, build: .flavor(.x86_64))

    func test_only_the_apple_silicon_download_on_apple_silicon_has_everything() {
        XCTAssertTrue(Self.appleSiliconOnAppleSilicon.appleSiliconFeatures)
        XCTAssertFalse(Self.intelOnAppleSilicon.appleSiliconFeatures, "no UnfairPlay or PlayCover handover in it")
        XCTAssertFalse(Self.intelOnIntel.appleSiliconFeatures)
        XCTAssertFalse(Self.appleSiliconOnIntel.appleSiliconFeatures)
        XCTAssertFalse(MacSupport(appleSiliconMac: true, build: .mixedUp("x")).appleSiliconFeatures)
    }

    func test_which_combinations_run_and_which_get_one_screen() {
        XCTAssertEqual(Self.appleSiliconOnAppleSilicon.verdict, .runs)
        XCTAssertEqual(Self.intelOnAppleSilicon.verdict, .runs)
        XCTAssertEqual(Self.intelOnIntel.verdict, .runs)
        XCTAssertEqual(Self.appleSiliconOnIntel.verdict, .wrongDownload, "its Python can't run there")
        XCTAssertEqual(MacSupport(appleSiliconMac: true, build: .mixedUp("why")).verdict, .mixedUp("why"))
        XCTAssertEqual(MacSupport(appleSiliconMac: false, build: .mixedUp("why")).verdict, .mixedUp("why"))
    }

    func test_only_the_intel_download_on_apple_silicon_gets_the_banner() {
        XCTAssertTrue(Self.intelOnAppleSilicon.isIntelBuildOnAppleSilicon)
        for other in [Self.appleSiliconOnAppleSilicon, Self.intelOnIntel, Self.appleSiliconOnIntel] {
            XCTAssertFalse(other.isIntelBuildOnAppleSilicon, "\(other)")
        }
    }

    /// One runner, whichever Mac: it depends only on whether the app runs.
    func test_the_runner_is_the_apps_cli_wherever_the_app_runs() {
        XCTAssertEqual(GraftApp.runnerChoice(insideTests: false, support: Self.appleSiliconOnAppleSilicon), .app)
        XCTAssertEqual(GraftApp.runnerChoice(insideTests: false, support: Self.intelOnIntel), .app)
        XCTAssertEqual(GraftApp.runnerChoice(insideTests: false, support: Self.intelOnAppleSilicon), .app)
        XCTAssertEqual(GraftApp.runnerChoice(insideTests: false, support: Self.appleSiliconOnIntel), .unused,
                       "nothing runs it, so the login shell isn't asked")
        XCTAssertEqual(GraftApp.runnerChoice(insideTests: false, support: .init(appleSiliconMac: true, build: .mixedUp("x"))),
                       .unused)
        for support in [Self.appleSiliconOnAppleSilicon, Self.intelOnIntel, Self.appleSiliconOnIntel] {
            XCTAssertEqual(GraftApp.runnerChoice(insideTests: true, support: support), .testHost)
        }
    }

    // MARK: - Reading a Mach-O's architectures

    /// <mach/machine.h>, written out here rather than taken from the code
    /// under test.
    private static let arm64: Int32 = 0x0100_000C
    private static let x86_64: Int32 = 0x0100_0007

    /// A 64-bit Mach-O header, little-endian, with no load commands.
    private func thin(_ cputype: Int32, subtype: UInt32 = 0) -> Data {
        var data = Data()
        for word in [0xFEED_FACF, UInt32(bitPattern: cputype), subtype, 2, 0, 0, 0, 0] as [UInt32] {
            withUnsafeBytes(of: word.littleEndian) { data.append(contentsOf: $0) }
        }
        return data
    }

    /// A universal file of `slices`, each (fat table cputype, slice bytes).
    private func fat(_ slices: [(Int32, Data)]) -> Data {
        var head = Data()
        func be(_ word: UInt32) { withUnsafeBytes(of: word.bigEndian) { head.append(contentsOf: $0) } }
        be(0xCAFE_BABE)
        be(UInt32(slices.count))
        var offset = UInt32(4096)
        var body = Data()
        for (cputype, data) in slices {
            be(UInt32(bitPattern: cputype)); be(0); be(offset); be(UInt32(data.count)); be(12)
            var padded = data
            padded.append(Data(count: 4096 - data.count))
            body.append(padded)
            offset += 4096
        }
        head.append(Data(count: 4096 - head.count))
        return head + body
    }

    private func file(_ data: Data) throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("graft-macho-\(UUID().uuidString)")
        try data.write(to: url)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }

    func test_the_architectures_of_thin_and_universal_files() throws {
        XCTAssertEqual(MachOArchitectures.read(try file(thin(Self.arm64))), ["arm64"])
        XCTAssertEqual(MachOArchitectures.read(try file(thin(Self.x86_64, subtype: 3))), ["x86_64"])
        XCTAssertEqual(MachOArchitectures.read(try file(thin(Self.arm64, subtype: 2))), ["arm64e"])
        XCTAssertEqual(MachOArchitectures.read(try file(thin(Self.arm64, subtype: 0x8000_0002))), ["arm64e"],
                       "the top byte is capability bits")
        XCTAssertEqual(
            MachOArchitectures.read(try file(fat([(Self.x86_64, thin(Self.x86_64)), (Self.arm64, thin(Self.arm64))]))),
            ["x86_64", "arm64"]
        )
    }

    /// A 64-bit fat file: its table entries are 32 bytes, offsets 64-bit.
    private func fat64(_ slices: [(Int32, Data)]) -> Data {
        var head = Data()
        func be32(_ word: UInt32) { withUnsafeBytes(of: word.bigEndian) { head.append(contentsOf: $0) } }
        func be64(_ word: UInt64) { withUnsafeBytes(of: word.bigEndian) { head.append(contentsOf: $0) } }
        be32(0xCAFE_BABF)
        be32(UInt32(slices.count))
        var offset = UInt64(4096)
        var body = Data()
        for (cputype, data) in slices {
            be32(UInt32(bitPattern: cputype)); be32(0); be64(offset); be64(UInt64(data.count)); be32(12); be32(0)
            body.append(data + Data(count: 4096 - data.count))
            offset += 4096
        }
        head.append(Data(count: 4096 - head.count))
        return head + body
    }

    /// A 64-bit Mach-O header stored big-endian, as a PowerPC64 file was.
    private func thinBigEndian(_ cputype: Int32) -> Data {
        var data = Data()
        for word in [0xFEED_FACF, UInt32(bitPattern: cputype), 0, 2, 0, 0, 0, 0] as [UInt32] {
            withUnsafeBytes(of: word.bigEndian) { data.append(contentsOf: $0) }
        }
        return data
    }

    /// The start of an `ar` archive: what a universal static library's
    /// slices are, and no Mach-O.
    private static let archive = Data("!<arch>\n/               0           0     0     644     4         `\n".utf8)

    func test_a_fat64_file_a_big_endian_header_and_a_slice_that_is_no_mach_o() throws {
        XCTAssertEqual(
            MachOArchitectures.read(try file(fat64([(Self.x86_64, thin(Self.x86_64)), (Self.arm64, thin(Self.arm64))]))),
            ["x86_64", "arm64"]
        )
        XCTAssertNil(MachOArchitectures.read(try file(fat64([(Self.x86_64, thin(Self.arm64))]))),
                     "a fat64 table that disagrees with its slice")
        XCTAssertEqual(MachOArchitectures.read(try file(thinBigEndian(Self.x86_64))), ["x86_64"])
        XCTAssertEqual(MachOArchitectures.read(try file(thinBigEndian(Self.arm64))), ["arm64"])
        XCTAssertEqual(MachOArchitectures.read(try file(fat([(Self.x86_64, Self.archive), (Self.arm64, thin(Self.arm64))]))),
                       ["arm64"], "the slice that is no Mach-O is left out, the other read")
        XCTAssertNil(MachOArchitectures.read(try file(fat([(Self.x86_64, Self.archive), (Self.arm64, Self.archive)]))),
                     "a universal static library: no Mach-O at all")
    }

    func test_what_isnt_a_readable_mach_o_has_no_architectures() throws {
        XCTAssertNil(MachOArchitectures.read(try file(Data("#!/bin/sh\nexit 0\n".utf8))))
        XCTAssertNil(MachOArchitectures.read(try file(Data())))
        XCTAssertNil(MachOArchitectures.read(try file(thin(Self.arm64).prefix(8))), "a truncated header")
        XCTAssertNil(MachOArchitectures.read(try file(fat([(Self.x86_64, thin(Self.arm64))]))),
                     "a fat table that disagrees with its slice")
        XCTAssertNil(MachOArchitectures.read(FileManager.default.temporaryDirectory.appendingPathComponent("graft-none")))
    }

    /// A real file: the test host's own executable has code for the
    /// processor this test runs as.
    func test_the_running_executable_has_code_for_this_process() throws {
        #if arch(arm64)
        let running = "arm64"
        #else
        let running = "x86_64"
        #endif
        let found = try XCTUnwrap(MachOArchitectures.read(try XCTUnwrap(Bundle.main.executableURL)))
        XCTAssertTrue(found.contains(running), "\(found)")
    }

    func test_a_bundles_embedded_python_is_read_where_the_cli_keeps_it() throws {
        let app = FileManager.default.temporaryDirectory.appendingPathComponent("graft-app-\(UUID().uuidString)/Graft.app")
        addTeardownBlock { try? FileManager.default.removeItem(at: app.deletingLastPathComponent()) }
        XCTAssertEqual(MacSupport.embeddedPython(in: app), .absent)
        let python = app.appending(path: "Contents/Resources/GraftPython/bin/python3.13")
        try FileManager.default.createDirectory(at: python.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("#!/bin/sh\n".utf8).write(to: python)
        XCTAssertEqual(MacSupport.embeddedPython(in: app), .unreadable)
        try thin(Self.x86_64).write(to: python)
        XCTAssertEqual(MacSupport.embeddedPython(in: app), .architectures(["x86_64"]))
    }

    /// A temporary Graft.app with a `bin` folder where the CLI keeps its
    /// Python, and that folder.
    private func appWithBin() throws -> (app: URL, bin: URL) {
        let app = FileManager.default.temporaryDirectory.appendingPathComponent("graft-app-\(UUID().uuidString)/Graft.app")
        addTeardownBlock { try? FileManager.default.removeItem(at: app.deletingLastPathComponent()) }
        let bin = app.appending(path: "Contents/Resources/GraftPython/bin")
        try FileManager.default.createDirectory(at: bin, withIntermediateDirectories: true)
        return (app, bin)
    }

    /// The interpreter is found by name, python3.<1 to 3 digits>, as the
    /// CLI's updater finds it (`app_update.INTERPRETER_RE`), not as this
    /// release's python3.13: the first Graft on a newer Python is still read.
    func test_a_bundles_embedded_python_is_found_by_its_name_whatever_the_minor_version() throws {
        for name in ["python3.14", "python3.9", "python3.100"] {
            let (app, bin) = try appWithBin()
            try thin(Self.arm64).write(to: bin.appending(path: name))
            XCTAssertEqual(MacSupport.embeddedPython(in: app), .architectures(["arm64"]), name)
        }
    }

    /// Names that only look like it are not the interpreter: a free-threaded
    /// build, a config script, the unversioned name, a patch version, a
    /// minor of four digits. With only those, there is no Python, as in a
    /// Debug build, even when each is a Mach-O.
    func test_a_name_that_only_looks_like_the_interpreter_is_not_it() throws {
        let lookalikes = ["python3.13t", "python3.13-config", "python3", "python3.13.1", "python3.1000",
                          "python3.", "python3.13 ", ".python3.13", "python3.1\u{0663}"]
        let (app, bin) = try appWithBin()
        XCTAssertEqual(MacSupport.embeddedPython(in: app), .absent, "an empty bin folder")
        for name in lookalikes {
            try thin(Self.arm64).write(to: bin.appending(path: name))
        }
        XCTAssertEqual(MacSupport.embeddedPython(in: app), .absent)
        try thin(Self.x86_64).write(to: bin.appending(path: "python3.13"))
        XCTAssertEqual(MacSupport.embeddedPython(in: app), .architectures(["x86_64"]),
                       "the one real name among them is the one read")
    }

    /// A `bin` folder there that can't be listed is a mixed-up bundle, as the
    /// CLI holds (`MixedBuild`), not a Debug build with no Python.
    func test_an_interpreter_folder_that_cannot_be_listed_is_unreadable() throws {
        let (app, bin) = try appWithBin()
        try thin(Self.arm64).write(to: bin.appending(path: "python3.13"))
        try FileManager.default.setAttributes([.posixPermissions: 0o000], ofItemAtPath: bin.path)
        addTeardownBlock { try? FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: bin.path) }
        XCTAssertEqual(MacSupport.embeddedPython(in: app), .unreadable)
    }

    /// Two interpreters is a mixed-up bundle, whichever is right: the CLI
    /// refuses it too.
    func test_more_than_one_interpreter_is_unreadable() throws {
        let (app, bin) = try appWithBin()
        try thin(Self.arm64).write(to: bin.appending(path: "python3.13"))
        try thin(Self.arm64).write(to: bin.appending(path: "python3.14"))
        XCTAssertEqual(MacSupport.embeddedPython(in: app), .unreadable)
    }

    func test_an_interpreter_that_is_a_folder_or_no_mach_o_is_unreadable() throws {
        let (app, bin) = try appWithBin()
        try FileManager.default.createDirectory(at: bin.appending(path: "python3.13"), withIntermediateDirectories: false)
        XCTAssertEqual(MacSupport.embeddedPython(in: app), .unreadable, "a folder")

        let (other, otherBin) = try appWithBin()
        try Data("#!/bin/sh\nexit 0\n".utf8).write(to: otherBin.appending(path: "python3.14"))
        XCTAssertEqual(MacSupport.embeddedPython(in: other), .unreadable, "a script")
    }

    /// A bundle's own code is its own files, as the CLI holds
    /// (`app_update.embedded_interpreter`): a folder on the way that is a
    /// link, or an interpreter that is one, is a mixed-up bundle even when
    /// what it points to reads.
    func test_an_interpreter_reached_through_a_link_is_unreadable() throws {
        let fm = FileManager.default
        for linked in ["Contents", "Contents/Resources", "Contents/Resources/GraftPython",
                       "Contents/Resources/GraftPython/bin"] {
            let (app, bin) = try appWithBin()
            try thin(Self.arm64).write(to: bin.appending(path: "python3.13"))
            XCTAssertEqual(MacSupport.embeddedPython(in: app), .architectures(["arm64"]), linked)
            let real = app.appending(path: linked)
            let moved = app.deletingLastPathComponent().appending(path: "moved-\(UUID().uuidString)")
            try fm.moveItem(at: real, to: moved)
            try fm.createSymbolicLink(at: real, withDestinationURL: moved)
            XCTAssertEqual(MacSupport.embeddedPython(in: app), .unreadable, linked)
        }

        let (app, bin) = try appWithBin()
        let elsewhere = app.deletingLastPathComponent().appending(path: "python")
        try thin(Self.arm64).write(to: elsewhere)
        try fm.createSymbolicLink(at: bin.appending(path: "python3.13"), withDestinationURL: elsewhere)
        XCTAssertEqual(MacSupport.embeddedPython(in: app), .unreadable, "the interpreter itself a link")
    }

    // MARK: - The gate

    func test_the_wrong_download_says_which_one_this_mac_needs() {
        XCTAssertEqual(
            WrongDownloadView.message,
            "This download is for Macs with Apple silicon. For this Mac, download Graft-Intel.zip from Graft's releases page."
        )
        XCTAssertEqual(WrongDownloadView.buttonLabel, "Open the Releases Page")
        XCTAssertEqual(WrongDownloadView.releasesURL.absoluteString, "https://github.com/GreenglassT/Graft/releases/latest")
        XCTAssertTrue(WrongDownloadView.mixedUpMessage("its plist says so").hasPrefix(
            "This copy of Graft.app is mixed up: its plist says so. Download Graft again"))
    }

    /// A stand-in CLI that notes every call, so a test can tell whether
    /// anything ran at all: the first load, the setup check, device polling
    /// and every other store go through the one runner `ContentView` shares.
    private func recordingCLI() throws -> (runner: CLIRunner, calls: URL) {
        let fm = FileManager.default
        let dir = fm.temporaryDirectory.appendingPathComponent("graft-gate-\(UUID().uuidString)")
        try fm.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? fm.removeItem(at: dir) }
        let calls = dir.appendingPathComponent("calls")
        let graft = dir.appendingPathComponent("graft")
        try """
        #!/bin/sh
        [ -z "${GRAFT_GATE_WARM_UP-}" ] || exit 0
        echo "$*" >> '\(calls.path)'
        printf 'error: config file not found at /tmp/example/config.yaml\\n' >&2
        exit 2
        """.write(to: graft, atomically: true, encoding: .utf8)
        try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: graft.path)
        // macOS checks a new executable on its first exec, which over a phone
        // hotspot can outlast the 1.5 s `window` waits (`warmUpFreshExecutable`).
        // The warm-up run exits before it records anything.
        try warmUpFreshExecutable(graft.path, environment: ["GRAFT_GATE_WARM_UP": "1"])
        return (CLIRunner(executable: graft), calls)
    }

    private func window(support: MacSupport, store: ConfigStore) async throws -> (Hosted, [String]) {
        let pane = hosted(
            AppleSiliconGate(support: support, openURL: { _ in }) {
                ContentView(store: store, adminShell: NoAdminShell())
            },
            size: NSSize(width: Metrics.windowMinWidth, height: Metrics.windowMinHeight)
        )
        // Long enough for ContentView's first-load `.task` to have run the
        // CLI, when there is a ContentView: the control below sees it do so.
        let deadline = Date().addingTimeInterval(1.5)
        while Date() < deadline {
            try await Task.sleep(nanoseconds: 100_000_000)
            settle(pane, seconds: 0.05)
        }
        addTeardownBlock { takeDown(pane) }
        let nodes = try AccessibilityTree.nodes(in: pane.window)
        let shown = nodes.flatMap { [$0.name, ($0.value as? String) ?? ""] }.filter { !$0.isEmpty }
        return (pane, shown)
    }

    func test_the_apple_silicon_download_on_an_intel_mac_shows_only_the_gate_and_runs_nothing() async throws {
        try AccessibilityTree.requireServed()
        let (runner, calls) = try recordingCLI()
        let store = ConfigStore(runner: runner, notify: { _ in })
        let (pane, shown) = try await window(support: Self.appleSiliconOnIntel, store: store)

        XCTAssertTrue(shown.contains(WrongDownloadView.message), "\(shown)")
        let nodes = try AccessibilityTree.nodes(in: pane.window)
        XCTAssertTrue(nodes.contains {
            $0.role == kAXButtonRole && $0.name == WrongDownloadView.buttonLabel
        }, "the button to the releases page")
        XCTAssertFalse(FileManager.default.fileExists(atPath: calls.path),
                       "the CLI ran: \((try? String(contentsOf: calls, encoding: .utf8)) ?? "")")
        XCTAssertNil(store.setupNeed, "no setup screen")
        XCTAssertNotEqual(pane.window.title, SetupView.title)
    }

    /// Delta audit try13, #15: the download button opens the page through
    /// the opener it was given, which in the app is the browser and here is a
    /// recorder, never through SwiftUI's `openURL`, whose default is the real
    /// browser. The hosted view's `openURL` is overridden too, as a backstop
    /// this test owns: had the button gone back to the environment, the click
    /// would land in `viaEnvironment` and fail here, not open a browser.
    func test_the_download_button_opens_the_page_through_the_opener_it_was_given() throws {
        try AccessibilityTree.requireServed()
        final class Opened { var given: [URL] = []; var viaEnvironment: [URL] = [] }
        let opened = Opened()
        let pane = hosted(
            AppleSiliconGate(support: Self.appleSiliconOnIntel, openURL: { opened.given.append($0) }) { Text("the app") }
                .environment(\.openURL, OpenURLAction { url in
                    opened.viaEnvironment.append(url)
                    return .handled
                }),
            size: NSSize(width: Metrics.windowMinWidth, height: Metrics.windowMinHeight)
        )
        addTeardownBlock { takeDown(pane) }
        settle(pane)
        XCTAssertEqual(opened.given, [], "nothing opens until the button is pressed")

        try clickButton(named: WrongDownloadView.buttonLabel, in: pane.window)
        settle(pane, seconds: 0.2)
        XCTAssertEqual(opened.given, [WrongDownloadView.releasesURL])
        XCTAssertEqual(opened.viaEnvironment, [], "not through SwiftUI's openURL")
    }

    /// The test host is Graft.app itself (its Debug build), with the real
    /// HOME. Its own window must run nothing: in a Debug build the CLI
    /// lookup finds ~/.local/bin/graft first, the owner's real CLI, and its
    /// App Store status had macOS ask for ipatool's keychain item during a
    /// test run (2.0.1). So the window is a placeholder, and its runner
    /// points at nothing that exists.
    @MainActor
    func test_the_test_hosts_own_window_runs_nothing() throws {
        try AccessibilityTree.requireServed()
        XCTAssertTrue(AdminShell.insideTests)
        XCTAssertFalse(FileManager.default.fileExists(atPath: GraftApp.testHostExecutable.path))
        // Every window the host has, whatever it's titled: the real UI names
        // its window after the selected item.
        let deadline = Date().addingTimeInterval(5)
        var shown: [[String]] = []
        repeat {
            RunLoop.main.run(until: Date().addingTimeInterval(0.1))
            shown = try NSApp.windows.filter { $0.isVisible && !($0 is NSPanel) }.map { window in
                try AccessibilityTree.nodes(in: window)
                    .flatMap { [$0.name, ($0.value as? String) ?? ""] }.filter { !$0.isEmpty }
            }
        } while !shown.contains(where: { $0.contains(TestHostWindow.text) }) && Date() < deadline
        XCTAssertTrue(shown.contains(where: { $0.contains(TestHostWindow.text) }), "\(shown)")
        // Not "no window shows Devices": other cases here host a ContentView
        // of their own, which may still be open. The app's own window
        // showing the placeholder is what proves its real UI never came up.
    }

    /// The control: the same window where this copy runs is the app, and its
    /// first load runs the CLI — so "nothing ran" above is the gate's doing.
    /// The Intel download runs on an Intel Mac exactly as the Apple silicon
    /// one does on Apple silicon; only the Intel download on Apple silicon
    /// adds the banner.
    func test_where_this_copy_runs_it_gets_the_app_and_runs_its_cli() async throws {
        try AccessibilityTree.requireServed()
        for support in [Self.appleSiliconOnAppleSilicon, Self.intelOnIntel, Self.intelOnAppleSilicon] {
            let (runner, calls) = try recordingCLI()
            let store = ConfigStore(runner: runner, notify: { _ in })
            let (pane, shown) = try await window(support: support, store: store)
            takeDown(pane)

            XCTAssertFalse(shown.contains(WrongDownloadView.message), "\(support): \(shown)")
            XCTAssertTrue(FileManager.default.fileExists(atPath: calls.path), "\(support): the first load ran the CLI")
            XCTAssertEqual(store.setupNeed?.step, 2, "\(support): the CLI's missing config is the setup screen")
            XCTAssertEqual(shown.contains(MacSupport.intelBuildBanner), support == Self.intelOnAppleSilicon,
                           "\(support): \(shown)")
        }
    }

    func test_a_mixed_up_bundle_shows_only_the_gate_and_runs_nothing() async throws {
        try AccessibilityTree.requireServed()
        let (runner, calls) = try recordingCLI()
        let store = ConfigStore(runner: runner, notify: { _ in })
        let why = "its Info.plist says it is the build for Intel Macs (x86_64), and its embedded Python has code only for arm64"
        let (_, shown) = try await window(support: MacSupport(appleSiliconMac: true, build: .mixedUp(why)), store: store)
        XCTAssertTrue(shown.contains(WrongDownloadView.mixedUpMessage(why)), "\(shown)")
        XCTAssertFalse(FileManager.default.fileExists(atPath: calls.path), "the CLI ran")
    }

    /// What the gate passes down is what every view reads: the one source.
    func test_the_gate_hands_its_support_to_the_app() throws {
        try AccessibilityTree.requireServed()
        struct Probe: View {
            @Environment(\.macSupport) private var mac
            var body: some View { Text("line: \(mac.unavailableLine ?? "none")") }
        }
        for support in [Self.appleSiliconOnAppleSilicon, Self.intelOnIntel, Self.intelOnAppleSilicon] {
            let pane = hosted(AppleSiliconGate(support: support, openURL: { _ in }) { Probe() }, width: 700)
            addTeardownBlock { takeDown(pane) }
            defer { takeDown(pane) }
            settle(pane, seconds: 0.2)
            let shown = try AccessibilityTree.texts(in: pane.window)
            XCTAssertTrue(shown.contains("line: \(support.unavailableLine ?? "none")"), "\(shown)")
        }
    }

    /// On macOS 13 the banner says Graft.zip needs macOS 14, rather than
    /// send the user to it (`MacSupport.intelBuildBannerLine`).
    func test_before_macos_14_the_banner_says_graft_zip_needs_it() throws {
        try AccessibilityTree.requireServed()
        var ventura = Self.intelOnAppleSilicon
        ventura.macOSMajorVersion = 13
        let pane = hosted(
            AppleSiliconGate(support: ventura, openURL: { _ in }) { Text("the app") },
            size: NSSize(width: Metrics.windowMinWidth, height: Metrics.windowMinHeight)
        )
        addTeardownBlock { takeDown(pane) }
        settle(pane)
        let shown = try AccessibilityTree.texts(in: pane.window)
        XCTAssertTrue(shown.contains("the app"), "\(shown)")
        XCTAssertTrue(shown.contains(MacSupport.intelBuildBannerBeforeMacOS14), "\(shown)")
        XCTAssertFalse(shown.contains(MacSupport.intelBuildBanner), "\(shown)")
    }

    /// The banner's button opens the releases page through the gate's
    /// opener, like the gate's own.
    func test_the_banners_button_opens_the_releases_page() throws {
        try AccessibilityTree.requireServed()
        final class Opened { var given: [URL] = [] }
        let opened = Opened()
        let pane = hosted(
            AppleSiliconGate(support: Self.intelOnAppleSilicon, openURL: { opened.given.append($0) }) { Text("the app") },
            size: NSSize(width: Metrics.windowMinWidth, height: Metrics.windowMinHeight)
        )
        addTeardownBlock { takeDown(pane) }
        settle(pane)
        let shown = try AccessibilityTree.texts(in: pane.window)
        XCTAssertTrue(shown.contains("the app"), "\(shown)")
        try clickButton(named: WrongDownloadView.buttonLabel, in: pane.window)
        settle(pane, seconds: 0.2)
        XCTAssertEqual(opened.given, [WrongDownloadView.releasesURL])
    }
}
