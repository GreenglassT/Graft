import SwiftUI
import XCTest
@testable import Graft

/// Settings › Command-line tool: a link at `~/.local/bin/graft` to the app's
/// own CLI, and Help › Licenses.
///
/// **No test here touches the real `~/.local/bin`.** The logic runs against a
/// filesystem in a dictionary (`FakeFS`); the one adapter test uses a scratch
/// directory standing in for a home folder.
@MainActor
final class CommandLineToolTests: XCTestCase {
    private let home = "/Users/x"
    private let link = "/Users/x/.local/bin/graft"
    private let target = "/Applications/Graft.app/Contents/Helpers/graft"
    private let pipx = "/Users/x/.local/pipx/venvs/graft-ipa/bin/graft"

    private let otherCopy = "/Users/x/Downloads/Graft.app/Contents/Helpers/graft"

    /// What is where, and what was done to it, in call order.
    private final class FakeFS {
        var entries: [String: CommandLineTool.Entry]
        /// Files that are there without an entry of their own: what the links
        /// under test point to. This app's launcher and pipx's venv by default.
        var present: Set<String>
        var log: [String] = []

        init(
            _ entries: [String: CommandLineTool.Entry] = [:],
            present: Set<String> = [
                "/Applications/Graft.app/Contents/Helpers/graft",
                "/Users/x/.local/pipx/venvs/graft-ipa/bin/graft",
            ]
        ) {
            self.entries = entries
            self.present = present
        }

        /// Absolute links only, which is all the fakes use past `status`.
        func exists(_ path: String) -> Bool {
            if present.contains(path) { return true }
            switch entries[path] {
            case nil: return false
            case let .symlink(destination)?: return exists(destination)
            default: return true
            }
        }

        func isRunnable(_ path: String) -> Bool {
            if present.contains(path) { return true }
            switch entries[path] {
            case .file?: return true
            case let .symlink(destination)?: return isRunnable(destination)
            default: return false
            }
        }

        var fs: CommandLineTool.FileSystem {
            CommandLineTool.FileSystem(
                entry: { [unowned self] in self.entries[$0] ?? .none },
                makeDirectory: { [unowned self] path, mode in
                    self.log.append("mkdir \(path) \(String(mode, radix: 8))")
                    self.entries[path] = .directory
                },
                removeFile: { [unowned self] path in
                    self.log.append("rm \(path)")
                    self.entries[path] = nil
                },
                makeSymlink: { [unowned self] path, destination in
                    self.log.append("ln \(path) -> \(destination)")
                    self.entries[path] = .symlink(to: destination)
                },
                exists: { [unowned self] in self.exists($0) },
                isRunnable: { [unowned self] in self.isRunnable($0) }
            )
        }
    }

    // MARK: - What is there

    func test_nothing_there_is_not_installed() {
        let fake = FakeFS()
        XCTAssertEqual(CommandLineTool.status(link: link, target: target, in: fake.fs), .notInstalled)
    }

    func test_a_link_to_this_apps_cli_is_installed() {
        let fake = FakeFS([link: .symlink(to: target)])
        XCTAssertEqual(CommandLineTool.status(link: link, target: target, in: fake.fs), .installed)
    }

    /// A relative link is read from the folder it is in, the way the system
    /// follows it.
    func test_a_relative_link_to_this_apps_cli_is_installed() {
        let relative = "../../../../Applications/Graft.app/Contents/Helpers/graft"
        let fake = FakeFS([link: .symlink(to: relative)])
        XCTAssertEqual(CommandLineTool.status(link: link, target: target, in: fake.fs), .installed)
    }

    /// pipx's `graft` is a link into its venv.
    func test_a_link_somewhere_else_is_another_graft() {
        let fake = FakeFS([link: .symlink(to: pipx)])
        XCTAssertEqual(CommandLineTool.status(link: link, target: target, in: fake.fs), .otherGraft(pipx))
    }

    /// A link to some other copy of Graft.app that is still there is not
    /// this app's either.
    func test_a_link_to_another_copy_of_the_app_is_another_graft() {
        let fake = FakeFS([link: .symlink(to: otherCopy)], present: [target, otherCopy])
        XCTAssertEqual(CommandLineTool.status(link: link, target: target, in: fake.fs), .otherGraft(otherCopy))
    }

    /// #13/#15/#38/#46: the app the link was made from was moved, or ran from
    /// App Translocation's temporary copy, which is gone. Not "another graft",
    /// with a pipx line for a copy pipx never made: a broken link.
    func test_a_link_whose_destination_is_gone_is_broken() {
        let translocated = "/private/var/folders/ab/T/AppTranslocation/0A1B/d/Graft.app/Contents/Helpers/graft"
        for gone in [otherCopy, translocated] {
            let fake = FakeFS([link: .symlink(to: gone)])
            XCTAssertEqual(CommandLineTool.status(link: link, target: target, in: fake.fs), .broken(gone), gone)
        }
    }

    /// Not "Installed" for a link to this app's own path when nothing is
    /// there: Terminal would run nothing through it.
    func test_a_link_to_this_apps_path_with_nothing_there_is_broken() {
        let fake = FakeFS([link: .symlink(to: target)], present: [])
        XCTAssertEqual(CommandLineTool.status(link: link, target: target, in: fake.fs), .broken(target))
    }

    /// Reinstall, for a broken link: nothing runs through it, so it is
    /// replaced without the confirmation another graft gets.
    func test_reinstall_replaces_a_broken_link_without_asking() throws {
        let fake = FakeFS(["/Users/x/.local/bin": .directory, link: .symlink(to: otherCopy)])
        let after = try CommandLineTool.install(link: link, target: target, replace: false, in: fake.fs)
        XCTAssertEqual(fake.log, ["rm \(link)", "ln \(link) -> \(target)"])
        XCTAssertEqual(after, .installed)
    }

    func test_a_regular_file_is_another_graft() {
        let fake = FakeFS([link: .file])
        XCTAssertEqual(CommandLineTool.status(link: link, target: target, in: fake.fs), .otherGraft(link))
    }

    func test_a_folder_is_in_the_way() {
        let fake = FakeFS([link: .directory])
        XCTAssertEqual(CommandLineTool.status(link: link, target: target, in: fake.fs), .blockedByNonFile)
    }

    // MARK: - Installing

    func test_install_makes_the_bin_folder_first_when_it_is_missing() throws {
        let fake = FakeFS()
        let after = try CommandLineTool.install(link: link, target: target, replace: false, in: fake.fs)
        XCTAssertEqual(fake.log, [
            "mkdir /Users/x/.local/bin 755",
            "ln \(link) -> \(target)",
        ])
        XCTAssertEqual(after, .installed)
    }

    func test_install_into_an_existing_bin_folder_only_links() throws {
        let fake = FakeFS(["/Users/x/.local/bin": .directory])
        _ = try CommandLineTool.install(link: link, target: target, replace: false, in: fake.fs)
        XCTAssertEqual(fake.log, ["ln \(link) -> \(target)"])
    }

    /// Another graft is only replaced when the user said so.
    func test_install_leaves_another_graft_alone_unless_told_to_replace_it() {
        let fake = FakeFS(["/Users/x/.local/bin": .directory, link: .symlink(to: pipx)])
        XCTAssertThrowsError(
            try CommandLineTool.install(link: link, target: target, replace: false, in: fake.fs)
        ) { error in
            XCTAssertEqual(error as? CommandLineTool.InstallError, .wouldReplace(pipx))
        }
        XCTAssertEqual(fake.log, [], "nothing removed, nothing linked")
        XCTAssertEqual(fake.entries[link], .symlink(to: pipx))
    }

    func test_install_replaces_another_graft_when_told_to() throws {
        let fake = FakeFS(["/Users/x/.local/bin": .directory, link: .symlink(to: pipx)])
        let after = try CommandLineTool.install(link: link, target: target, replace: true, in: fake.fs)
        XCTAssertEqual(fake.log, ["rm \(link)", "ln \(link) -> \(target)"])
        XCTAssertEqual(after, .installed)
    }

    /// Never a folder, whatever `replace` says: it could hold anything.
    func test_install_never_deletes_a_folder() {
        for replace in [false, true] {
            let fake = FakeFS(["/Users/x/.local/bin": .directory, link: .directory])
            XCTAssertThrowsError(
                try CommandLineTool.install(link: link, target: target, replace: replace, in: fake.fs)
            ) { error in
                XCTAssertEqual(error as? CommandLineTool.InstallError, .blocked(self.link))
            }
            XCTAssertEqual(fake.log, [], "replace: \(replace)")
        }
    }

    /// Reinstall: the link is this app's own, so it is made again without
    /// asking.
    func test_reinstall_makes_this_apps_link_again() throws {
        let fake = FakeFS(["/Users/x/.local/bin": .directory, link: .symlink(to: target)])
        let after = try CommandLineTool.install(link: link, target: target, replace: false, in: fake.fs)
        XCTAssertEqual(fake.log, ["rm \(link)", "ln \(link) -> \(target)"])
        XCTAssertEqual(after, .installed)
    }

    // MARK: - PATH

    /// #19: "Installed" says Terminal runs this app's graft only when nothing
    /// earlier on the PATH answers to `graft` first.
    func test_a_graft_earlier_on_the_path_shadows_the_link() {
        let homebrew = "/opt/homebrew/bin/graft"
        let fake = FakeFS([homebrew: .file, link: .symlink(to: target)])
        let path = "/usr/bin:/opt/homebrew/bin/:/Users/x/.local/bin:/bin"
        XCTAssertEqual(
            CommandLineTool.shadowingGraft(loginPATH: path, link: link, target: target, in: fake.fs), homebrew
        )
        XCTAssertNil(
            CommandLineTool.shadowingGraft(
                loginPATH: "/Users/x/.local/bin:/opt/homebrew/bin", link: link, target: target, in: fake.fs
            ),
            "the link's folder first"
        )
        XCTAssertNil(
            CommandLineTool.shadowingGraft(loginPATH: "/usr/bin:/opt/homebrew/bin", link: link, target: target, in: fake.fs),
            "the folder is not on the PATH at all: the PATH advice says so"
        )
        XCTAssertNil(CommandLineTool.shadowingGraft(loginPATH: nil, link: link, target: target, in: fake.fs))
    }

    /// Another link to this app's own launcher, or a folder named graft, runs
    /// nothing else.
    func test_this_apps_graft_or_a_folder_earlier_on_the_path_shadows_nothing() {
        let fake = FakeFS([
            "/usr/local/bin/graft": .symlink(to: target),
            "/opt/homebrew/bin/graft": .directory,
            link: .symlink(to: target),
        ])
        XCTAssertNil(CommandLineTool.shadowingGraft(
            loginPATH: "/usr/local/bin:/opt/homebrew/bin:/Users/x/.local/bin", link: link, target: target, in: fake.fs
        ))
    }

    func test_the_installed_line_names_the_graft_that_comes_first() {
        let line = CommandLineToolSection.statusLine(
            .installed, link: "~/.local/bin/graft", shadowedBy: "/opt/homebrew/bin/graft"
        )
        XCTAssertEqual(line, "Installed at ~/.local/bin/graft, but /opt/homebrew/bin/graft comes first "
            + "on your shell's PATH, so graft in Terminal runs that one.")
        XCTAssertFalse(
            CommandLineToolSection.statusLine(.installed, link: "~/.local/bin/graft").contains("comes first")
        )
    }

    /// Whole-diff review, app-1 (which reverses #19's "each time it
    /// appears"): the section reuses the PATH read at launch, so a visit to
    /// Settings doesn't run the user's startup files. Its Check again asks
    /// the shell again, which is how a user who has added the PATH line sees
    /// the advice go; and asks at most once per the shell's interval.
    func test_the_apps_context_reuses_the_launch_path_and_asks_again_only_on_check_again() async {
        // The first answer is read at launch; by the second ask the user has
        // added the line.
        final class Answers: @unchecked Sendable {
            private let lock = NSLock()
            private var asked = 0
            var count: Int { lock.withLock { asked } }
            func next() -> String {
                lock.withLock {
                    asked += 1
                    return asked < 2 ? "/usr/bin:/bin" : "/Users/x/.local/bin:/usr/bin:/bin"
                }
            }
        }
        final class Clock: @unchecked Sendable {
            private let lock = NSLock()
            private var t: TimeInterval = 1_000
            var now: TimeInterval { lock.withLock { t } }
            func advance(_ seconds: TimeInterval) { lock.withLock { t += seconds } }
        }
        let answers = Answers()
        let clock = Clock()
        let shell = ShellEnvironment(minimumInterval: 10, now: { clock.now }) { ["PATH": answers.next()] }
        _ = await shell.value  // the launch-time read, done before the section appears
        let context = CommandLineTool.Context.forApp(
            bundleURL: URL(fileURLWithPath: "/Applications/Graft.app"), shell: shell, home: home,
            fs: FakeFS().fs
        )
        clock.advance(60)
        for _ in 0..<3 {
            let appeared = await context.loginPATH(false)
            XCTAssertNotNil(CommandLineTool.pathAdvice(loginPATH: appeared, shell: "/bin/zsh", home: home))
        }
        XCTAssertEqual(answers.count, 1, "appearing asked the shell again")

        let checked = await context.loginPATH(true)
        XCTAssertNil(
            CommandLineTool.pathAdvice(loginPATH: checked, shell: "/bin/zsh", home: home),
            "Check again read the launch-time PATH again: \(String(describing: checked))"
        )
        XCTAssertEqual(answers.count, 2)
        _ = await context.loginPATH(true)
        XCTAssertEqual(answers.count, 2, "a second Check again within the interval asks nothing")
    }

    func test_no_advice_when_the_bin_folder_is_on_the_login_path() {
        for path in ["/usr/bin:/Users/x/.local/bin:/bin", "/Users/x/.local/bin/:/usr/bin"] {
            XCTAssertNil(CommandLineTool.pathAdvice(loginPATH: path, shell: "/bin/zsh", home: home), path)
        }
    }

    func test_the_line_to_add_for_zsh_and_bash() {
        for shell in ["/bin/zsh", "/bin/bash", "/opt/homebrew/bin/bash"] {
            XCTAssertEqual(
                CommandLineTool.pathAdvice(loginPATH: "/usr/bin:/bin", shell: shell, home: home),
                #"export PATH="$HOME/.local/bin:$PATH""#, shell
            )
        }
    }

    func test_the_line_to_add_for_csh_and_tcsh() {
        for shell in ["/bin/csh", "/bin/tcsh"] {
            XCTAssertEqual(
                CommandLineTool.pathAdvice(loginPATH: "/usr/bin:/bin", shell: shell, home: home),
                #"setenv PATH "$HOME/.local/bin:$PATH""#, shell
            )
        }
    }

    /// A shell that never answered says nothing about its PATH, and the
    /// section does not claim the folder is missing from it.
    func test_no_advice_when_the_login_path_is_unknown() {
        XCTAssertNil(CommandLineTool.pathAdvice(loginPATH: nil, shell: "/bin/zsh", home: home))
    }

    // MARK: - The real filesystem, in a scratch home

    private func scratchHome() throws -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-cli-link-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        return dir
    }

    func test_the_real_adapter_makes_a_0755_folder_and_the_link() throws {
        let home = try scratchHome()
        let link = home.appendingPathComponent(".local/bin/graft").path
        let target = "/nonexistent/Graft.app/Contents/Helpers/graft"

        XCTAssertEqual(CommandLineTool.status(link: link, target: target, in: .real), .notInstalled)
        XCTAssertEqual(
            try CommandLineTool.install(link: link, target: target, replace: false, in: .real), .broken(target),
            "made, and a link to a launcher that is not there runs nothing"
        )

        let bin = home.appendingPathComponent(".local/bin").path
        let mode = try FileManager.default.attributesOfItem(atPath: bin)[.posixPermissions] as? Int
        XCTAssertEqual(mode, 0o755)
        XCTAssertEqual(try FileManager.default.destinationOfSymbolicLink(atPath: link), target)

        let launcher = home.appendingPathComponent("Graft.app/Contents/Helpers/graft")
        try FileManager.default.createDirectory(
            at: launcher.deletingLastPathComponent(), withIntermediateDirectories: true
        )
        try Data().write(to: launcher)
        XCTAssertEqual(
            try CommandLineTool.install(link: link, target: launcher.path, replace: false, in: .real), .installed,
            "Reinstall, over the broken link, without asking"
        )
    }

    func test_the_real_adapter_reads_files_and_folders_and_never_removes_a_folder() throws {
        let home = try scratchHome()
        let bin = home.appendingPathComponent(".local/bin")
        let link = bin.appendingPathComponent("graft")
        try FileManager.default.createDirectory(at: link, withIntermediateDirectories: true)
        let inside = link.appendingPathComponent("keep")
        try Data("x".utf8).write(to: inside)

        XCTAssertEqual(CommandLineTool.FileSystem.real.entry(link.path), .directory)
        XCTAssertThrowsError(try CommandLineTool.FileSystem.real.removeFile(link.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: inside.path))

        let file = bin.appendingPathComponent("other")
        try Data("#!/bin/sh\n".utf8).write(to: file)
        XCTAssertEqual(CommandLineTool.FileSystem.real.entry(file.path), .file)
        XCTAssertEqual(CommandLineTool.FileSystem.real.entry(bin.appendingPathComponent("none").path), .none)
    }

    // MARK: - Where the app runs from

    func test_where_the_app_runs_from() {
        let url = URL(fileURLWithPath: "/Users/x/Downloads/Graft.app")
        XCTAssertEqual(AppLocation.of(url, isTranslocated: { _ in true }, isReadOnly: { _ in false }), .translocated)
        XCTAssertEqual(AppLocation.of(url, isTranslocated: { _ in false }, isReadOnly: { _ in true }), .readOnly)
        XCTAssertEqual(AppLocation.of(url, isTranslocated: { _ in false }, isReadOnly: { _ in false }), .settled)
    }

    /// The system's own check is found, and the path's shape decides as well.
    func test_translocation_is_read_from_the_system_and_the_path() {
        XCTAssertNotNil(AppLocation.secTranslocateIsTranslocatedURL, "SecTranslocateIsTranslocatedURL not found")
        XCTAssertTrue(AppLocation.isTranslocated(
            URL(fileURLWithPath: "/private/var/folders/ab/T/AppTranslocation/0A1B-2C3D/d/Graft.app")
        ))
        XCTAssertFalse(AppLocation.isTranslocated(Bundle.main.bundleURL), "the test host isn't translocated")
    }

    /// The sealed system volume is mounted read-only; the temporary folder
    /// is not.
    func test_a_read_only_volume_is_read_only() throws {
        XCTAssertTrue(AppLocation.isReadOnly("/System/Library/CoreServices/Finder.app"))
        XCTAssertFalse(AppLocation.isReadOnly(try scratchHome().path))
    }

    // MARK: - The section

    private func section(
        _ fake: FakeFS, loginPATH: String? = "/usr/bin:/Users/x/.local/bin", location: AppLocation = .settled
    ) -> some View {
        section(fake, loginPATH: { _ in loginPATH }, location: location)
    }

    private func section(
        _ fake: FakeFS, loginPATH: @escaping (Bool) async -> String?, location: AppLocation = .settled,
        askedTooRecently: @escaping () -> Bool = { false }
    ) -> some View {
        CommandLineToolSection(context: CommandLineTool.Context(
            home: home, target: target, shell: "/bin/zsh", fs: fake.fs, loginPATH: loginPATH,
            askedTooRecently: askedTooRecently, location: location
        ))
        .frame(width: 700)
    }

    private func shown(_ pane: Hosted) async throws -> [AccessibilityTree.Node] {
        // The section reads the login PATH in a `.task`.
        for _ in 0..<5 {
            try await Task.sleep(nanoseconds: 50_000_000)
            settle(pane, seconds: 0.05)
        }
        return try AccessibilityTree.nodes(in: pane.window)
    }

    private static func strings(_ nodes: [AccessibilityTree.Node]) -> [String] {
        nodes.flatMap { [$0.name, ($0.value as? String) ?? ""] }.filter { !$0.isEmpty }
    }

    /// Install, pressed, links this app's CLI and the line says so.
    func test_the_section_installs_and_then_says_installed() async throws {
        try AccessibilityTree.requireServed()
        let fake = FakeFS()
        let pane = hosted(section(fake), size: NSSize(width: 700, height: 400))
        let nodes = try await shown(pane)
        XCTAssertTrue(nodes.contains {
            $0.role == kAXButtonRole && $0.name == CommandLineToolSection.installLabel
        }, "\(Self.strings(nodes))")
        // Clicked through the window, not `AccessibilityTree.press`: that
        // performs the action on the query's background thread, and a
        // SwiftUI Button traps off the main actor (see `clickButton`).
        try clickButton(named: CommandLineToolSection.installLabel, in: pane.window)
        let after = try await shown(pane)

        XCTAssertEqual(fake.entries[link], .symlink(to: target))
        let text = Self.strings(after)
        XCTAssertTrue(text.contains { $0.contains(CommandLineToolSection.statusLine(.installed, link: "~/.local/bin/graft")) }, "\(text)")
        XCTAssertTrue(after.contains { $0.role == kAXButtonRole && $0.name == CommandLineToolSection.reinstallLabel })
    }

    /// Another graft: where it is, the optional pipx line, and Replace behind
    /// a confirmation. Nothing is replaced by showing it.
    func test_the_section_names_another_graft_and_offers_to_replace_it() async throws {
        try AccessibilityTree.requireServed()
        let fake = FakeFS(["/Users/x/.local/bin": .directory, link: .symlink(to: pipx)])
        let pane = hosted(section(fake), size: NSSize(width: 700, height: 400))
        let nodes = try await shown(pane)
        let text = Self.strings(nodes)

        XCTAssertTrue(text.contains { $0.contains(pipx) }, "\(text)")
        XCTAssertTrue(text.contains { $0.contains(CommandLineToolSection.pipxUninstall) }, "\(text)")
        XCTAssertTrue(nodes.contains { $0.role == kAXButtonRole && $0.name == CommandLineToolSection.replaceLabel })
        XCTAssertEqual(fake.log, [])
    }

    /// #13/#15/#38/#46: run from App Translocation's copy or a disk image,
    /// Install is there but disabled, with what to do instead, and nothing is
    /// linked.
    func test_a_translocated_or_read_only_app_is_told_to_move_to_applications_first() async throws {
        try AccessibilityTree.requireServed()
        for location in [AppLocation.translocated, .readOnly] {
            let fake = FakeFS()
            let pane = hosted(section(fake, location: location), size: NSSize(width: 700, height: 400))
            let nodes = try await shown(pane)
            let install = nodes.first { $0.role == kAXButtonRole && $0.name == CommandLineToolSection.installLabel }
            XCTAssertEqual(install?.enabled, false, "\(location)")
            XCTAssertTrue(Self.strings(nodes).contains { $0.contains(CommandLineToolSection.moveFirst) },
                          "\(location): \(Self.strings(nodes))")
            try clickButton(named: CommandLineToolSection.installLabel, in: pane.window)
            _ = try await shown(pane)
            XCTAssertEqual(fake.log, [], "\(location): nothing linked")
        }
    }

    /// The same section, settled: Install is enabled and the line is absent.
    func test_a_settled_app_offers_install() async throws {
        try AccessibilityTree.requireServed()
        let fake = FakeFS()
        let pane = hosted(section(fake), size: NSSize(width: 700, height: 400))
        let nodes = try await shown(pane)
        let install = nodes.first { $0.role == kAXButtonRole && $0.name == CommandLineToolSection.installLabel }
        XCTAssertEqual(install?.enabled, true)
        XCTAssertFalse(Self.strings(nodes).contains { $0.contains(CommandLineToolSection.moveFirst) })
        XCTAssertEqual(fake.log, [], "showing it links nothing")
    }

    /// A broken link: the line says so, Reinstall is offered, and there is
    /// no pipx line for a copy pipx never made.
    func test_the_section_shows_a_broken_link_with_reinstall() async throws {
        try AccessibilityTree.requireServed()
        let fake = FakeFS(["/Users/x/.local/bin": .directory, link: .symlink(to: otherCopy)])
        let pane = hosted(section(fake), size: NSSize(width: 700, height: 400))
        let nodes = try await shown(pane)
        let text = Self.strings(nodes)
        XCTAssertTrue(text.contains {
            $0.contains(CommandLineToolSection.statusLine(.broken(otherCopy), link: "~/.local/bin/graft"))
        }, "\(text)")
        XCTAssertFalse(text.contains { $0.contains(CommandLineToolSection.pipxUninstall) }, "\(text)")
        try clickButton(named: CommandLineToolSection.reinstallLabel, in: pane.window)
        _ = try await shown(pane)
        XCTAssertEqual(fake.entries[link], .symlink(to: target))
    }

    /// #19: installed, but a graft earlier on the login PATH is what
    /// Terminal runs, and the line says which.
    func test_the_section_says_when_another_graft_comes_first_on_path() async throws {
        try AccessibilityTree.requireServed()
        let fake = FakeFS([
            "/opt/homebrew/bin/graft": .file, "/Users/x/.local/bin": .directory, link: .symlink(to: target),
        ])
        let pane = hosted(
            section(fake, loginPATH: "/opt/homebrew/bin:/Users/x/.local/bin"), size: NSSize(width: 700, height: 400)
        )
        let text = Self.strings(try await shown(pane))
        XCTAssertTrue(text.contains { $0.contains(CommandLineToolSection.statusLine(
            .installed, link: "~/.local/bin/graft", shadowedBy: "/opt/homebrew/bin/graft"
        )) }, "\(text)")
    }

    func test_the_section_shows_the_path_line_when_the_folder_is_not_on_path() async throws {
        try AccessibilityTree.requireServed()
        let fake = FakeFS()
        let pane = hosted(section(fake, loginPATH: "/usr/bin:/bin"), size: NSSize(width: 700, height: 400))
        let text = Self.strings(try await shown(pane))
        XCTAssertTrue(text.contains { $0.contains(#"export PATH="$HOME/.local/bin:$PATH""#) }, "\(text)")
    }

    /// Whole-diff review, app-1: appearing reads the launch-time PATH (no
    /// new ask of the shell); the PATH advice carries a Check again, which
    /// asks, and the advice goes once the shell's PATH has the folder.
    func test_check_again_beside_the_path_line_asks_the_shell_again() async throws {
        try AccessibilityTree.requireServed()
        @MainActor final class Asks { var made: [Bool] = [] }
        let asks = Asks()
        let fake = FakeFS()
        let pane = hosted(section(fake, loginPATH: { again in
            await MainActor.run { asks.made.append(again) }
            return again ? "/Users/x/.local/bin:/usr/bin:/bin" : "/usr/bin:/bin"
        }), size: NSSize(width: 700, height: 400))
        let before = Self.strings(try await shown(pane))
        XCTAssertTrue(before.contains { $0.contains(#"export PATH="$HOME/.local/bin:$PATH""#) }, "\(before)")
        XCTAssertTrue(before.contains(CommandLineToolSection.checkAgainCaption), "\(before)")
        XCTAssertEqual(asks.made, [false], "appearing doesn't ask the shell again")

        try clickButton(named: CommandLineToolSection.checkAgainLabel, in: pane.window)
        let after = Self.strings(try await shown(pane))
        XCTAssertEqual(asks.made, [false, true])
        XCTAssertFalse(after.contains { $0.contains(#"export PATH="$HOME/.local/bin:$PATH""#) }, "\(after)")
        XCTAssertFalse(after.contains(CommandLineToolSection.checkAgainCaption), "\(after)")
    }

    /// The try14 delta audit's D14-APP-3: Check again within the shell's
    /// interval served the last answer with no sign of it, and the advice
    /// stayed as if the new line were wrong. It says so now.
    func test_check_again_the_shell_wasnt_asked_for_says_so() async throws {
        try AccessibilityTree.requireServed()
        let fake = FakeFS()
        let pane = hosted(section(
            fake, loginPATH: { _ in "/usr/bin:/bin" }, askedTooRecently: { true }
        ), size: NSSize(width: 700, height: 500))
        let before = Self.strings(try await shown(pane))
        XCTAssertFalse(before.contains(ShellEnvironment.askedTooRecentlyNote), "not on appearing: \(before)")

        try clickButton(named: CommandLineToolSection.checkAgainLabel, in: pane.window)
        let after = Self.strings(try await shown(pane))
        XCTAssertTrue(after.contains(ShellEnvironment.askedTooRecentlyNote), "\(after)")
    }

    /// Replace says what happens to what is there: a link is repointed, a
    /// regular file is deleted.
    func test_the_replace_confirmation_says_what_happens_to_the_other_graft() {
        let tilde: (String) -> String = { $0.replacingOccurrences(of: "/Users/x", with: "~") }
        let repointed = CommandLineToolSection.replaceMessage(other: pipx, link: link, shown: tilde)
        XCTAssertTrue(repointed.contains("~/.local/pipx/venvs/graft-ipa/bin/graft itself stays"), repointed)
        let deleted = CommandLineToolSection.replaceMessage(other: link, link: link, shown: tilde)
        XCTAssertTrue(deleted.contains("~/.local/bin/graft is a file, not a link. It will be deleted"), deleted)
    }

    /// The suite is a Debug build, which has no bundled CLI to link: the
    /// section is not offered.
    func test_debug_builds_do_not_offer_the_section() {
        XCTAssertFalse(CommandLineToolSection.isOffered)
    }

    // MARK: - Help › Licenses

    func test_licenses_is_the_folder_in_resources_when_it_is_there() {
        let resources = URL(fileURLWithPath: "/Applications/Graft.app/Contents/Resources")
        XCTAssertEqual(
            BundledLicenses.folder(resourceURL: resources, isDirectory: { _ in true })?.path,
            "/Applications/Graft.app/Contents/Resources/Licenses"
        )
        XCTAssertNil(BundledLicenses.folder(resourceURL: resources, isDirectory: { _ in false }))
        XCTAssertNil(BundledLicenses.folder(resourceURL: nil, isDirectory: { _ in true }))
    }

    /// A Debug build has no Licenses folder, so the menu item is disabled.
    func test_a_debug_build_has_no_licenses_folder() {
        XCTAssertNil(BundledLicenses.installed)
    }

    // MARK: - About

    /// #20/#25: the statement the About panel makes, in the one constant the
    /// release docs quote, with the version filled in.
    func test_the_about_notice_names_the_gpl_and_the_source_archive() {
        XCTAssertEqual(
            AboutGraft.notice(version: "2.0"),
            "Graft's own code is MIT licensed. Graft.app also bundles other programs, each under "
                + "its own license — GPL, LGPL, MPL, APSL and AGPL among them — and the command-line "
                + "program inside it is distributed under GPL-3.0-or-later. See Help › Licenses; the "
                + "source for Graft and for the components whose licenses require it is in "
                + "Graft-2.0-sources.tar.gz on the release page."
        )
        XCTAssertFalse(AboutGraft.licenceNotice.contains("all of it"), "the archive is not all of it")
        XCTAssertEqual(AboutGraft.licenceNotice.components(separatedBy: "<version>").count, 2,
                       "one placeholder, for the version")
        XCTAssertEqual(AboutGraft.releasesURL.absoluteString, "https://github.com/GreenglassT/Graft/releases")
    }

    /// The panel's credits carry this copy's own version, from its bundle,
    /// and link the release page.
    func test_the_about_panel_shows_the_notice_for_this_copys_version() throws {
        let version = try XCTUnwrap(Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String)
        XCTAssertEqual(AboutGraft.version(of: Bundle.main), version)
        let credits = try XCTUnwrap(AboutGraft.panelOptions()[.credits] as? NSAttributedString)
        XCTAssertTrue(credits.string.hasPrefix(AboutGraft.notice(version: version)), credits.string)
        XCTAssertFalse(credits.string.contains("Graft is MIT licensed"), credits.string)
        let range = (credits.string as NSString).range(of: AboutGraft.releasesURL.absoluteString)
        XCTAssertNotEqual(range.location, NSNotFound, credits.string)
        XCTAssertEqual(credits.attribute(.link, at: range.location, effectiveRange: nil) as? URL, AboutGraft.releasesURL)
    }

    /// Star on GitHub and Support on Ko-fi: the two pages, exactly, under
    /// the titles the Help menu and About Graft use.
    func test_the_support_links_are_the_repository_and_ko_fi() {
        XCTAssertEqual(SupportLinks.repository.absoluteString, "https://github.com/GreenglassT/Graft")
        XCTAssertEqual(SupportLinks.koFi.absoluteString, "https://ko-fi.com/greenglasst")
        XCTAssertEqual(SupportLinks.starTitle, "Star Graft on GitHub")
        XCTAssertEqual(SupportLinks.supportTitle, "Support Graft on Ko-fi")
    }

    /// About Graft links both pages under their titles, after the notice
    /// and the release page, which keep their place.
    func test_the_about_panel_links_github_and_ko_fi() throws {
        let credits = AboutGraft.credits(version: "2.1")
        let text = credits.string as NSString
        XCTAssertTrue(credits.string.hasPrefix(AboutGraft.notice(version: "2.1")), credits.string)
        let release = text.range(of: AboutGraft.releasesURL.absoluteString)
        for (title, url) in [(SupportLinks.starTitle, SupportLinks.repository), (SupportLinks.supportTitle, SupportLinks.koFi)] {
            let range = text.range(of: title)
            XCTAssertNotEqual(range.location, NSNotFound, credits.string)
            XCTAssertGreaterThan(range.location, release.location, "\(title) comes after the release page")
            XCTAssertEqual(credits.attribute(.link, at: range.location, effectiveRange: nil) as? URL, url, title)
        }
    }

    /// The Help menu offers both. Only the titles are checked: choosing one
    /// would open a browser.
    func test_the_help_menu_offers_star_and_ko_fi() throws {
        let help = try XCTUnwrap(NSApplication.shared.mainMenu?.items.first { $0.title == "Help" }?.submenu, "no Help menu")
        let titles = help.items.map(\.title)
        XCTAssertTrue(titles.contains(SupportLinks.starTitle), "\(titles)")
        XCTAssertTrue(titles.contains(SupportLinks.supportTitle), "\(titles)")
    }

    /// The old Credits.rtf, which said "Graft is MIT licensed", no longer
    /// ships: nothing can show it, and nothing in the bundle says it.
    func test_the_old_credits_file_is_not_in_the_app() {
        XCTAssertNil(Bundle.main.url(forResource: "Credits", withExtension: "rtf"))
    }

    /// Graft › About Graft, chosen from the app's own menu, opens the
    /// standard panel with that text.
    func test_the_about_menu_item_shows_the_notice() throws {
        let appMenu = try XCTUnwrap(NSApplication.shared.mainMenu?.items.first?.submenu, "no app menu")
        let item = try XCTUnwrap(appMenu.items.first { $0.title == "About Graft" },
                                 "\(appMenu.items.map(\.title))")
        let before = Set(NSApplication.shared.windows.map(ObjectIdentifier.init))
        appMenu.performActionForItem(at: appMenu.index(of: item))
        let deadline = Date().addingTimeInterval(3)
        var panel: NSWindow?
        while panel == nil, Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
            panel = NSApplication.shared.windows.first { !before.contains(ObjectIdentifier($0)) && $0.isVisible }
        }
        let shown = try XCTUnwrap(panel, "no About panel opened")
        defer { shown.close() }
        func texts(_ view: NSView) -> [String] {
            ((view as? NSTextView).map { [$0.string] } ?? []) + view.subviews.flatMap(texts)
        }
        let all = shown.contentView.map(texts) ?? []
        let version = AboutGraft.version(of: Bundle.main)
        XCTAssertTrue(all.contains { $0.contains(AboutGraft.notice(version: version)) }, "\(all)")
    }
}
