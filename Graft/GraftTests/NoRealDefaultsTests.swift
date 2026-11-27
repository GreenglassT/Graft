import XCTest

/// Nothing in the app defaults to something that reaches past this process.
///
/// Twice on this Mac a test raised a real GUI prompt through a default it
/// never meant to use: the notification permission prompt (`ConfigStore`'s
/// `postNote`), then macOS's administrator dialog, from a test that built an
/// `AppStoreStore` and relied on the code under test to stop before its
/// defaulted `FallbackAdminShell` (2026-11-22). A test's safety can't depend on
/// the code it tests, so every such dependency is now a required parameter,
/// and `GraftApp` is the one place that names the real ones. A test that
/// forgets its fake doesn't compile.
///
/// This keeps a default from coming back. It reads the app's source, outside
/// `GraftApp.swift`, and fails on:
/// - a parameter default (`init`, `func`, enum `case`, multi-line signatures
///   too) whose type is in `sideEffectingTypes` or whose value names one of
///   `sideEffectingValues`;
/// - a stored property's initial value of either kind (`static` singletons
///   only from `singletons`);
/// - a `?? <one of them>` fallback, which is the same default hidden behind nil;
/// - the same fallback spelled as `if let`/`guard let` with an `else` that
///   names one, for a parameter or stored property that defaults to nil
///   (best effort: a ternary, or a `switch` on the optional, isn't seen);
/// - a SwiftUI property wrapper that acts on the real thing unless the view's
///   environment says otherwise (`@AppStorage`, `@SceneStorage`, and
///   `@Environment(\.openURL)`, the browser), other than the ones
///   `allowedWrappers` names, each with its reason;
/// - a SwiftUI `Link`, which opens its URL through that same `openURL`, other
///   than the ones `allowedLinks` names, each with its reason.
///
/// `AdminShell.insideTests` stays as the second layer.
final class NoRealDefaultsTests: XCTestCase {
    /// A parameter or property of one of these types may have no default but nil.
    static let sideEffectingTypes: Set<String> = [
        // macOS's administrator prompt.
        "PrivilegedShell", "FallbackAdminShell", "AppleScriptAdminShell", "OsascriptAdminShell",
        // The user's login shell, with their startup files.
        "ShellEnvironment", "Runner", "LoginShell.Runner",
        // The general pasteboard, the real preferences, the real filesystem
        // the command-line tool writes a link into.
        "NSPasteboard", "UserDefaults", "PersistentDomainStore",
        "FileSystem", "CommandLineTool.FileSystem",
        // The rebuild that replaces and quits the app; the notification center;
        // the network; other apps.
        "RebuildCoordinator", "UNUserNotificationCenter", "URLSession", "NSWorkspace",
        // The graft CLI, which reaches ipatool, the login keychain and the
        // phone; and any child process (`/usr/bin/security` included).
        "CLIRunner", "Process",
    ]

    /// Names of the real things, wherever a default or fallback spells them.
    static let sideEffectingValues: [String] = [
        "FallbackAdminShell", "AppleScriptAdminShell", "OsascriptAdminShell", "NSAppleScript",
        "NSPasteboard.general", "UserDefaults.standard",
        "ShellEnvironment.shared", "runProcess", "userShell(",
        "BuildNotifier.post", "UNUserNotificationCenter",
        "installFromRepo", "quitAndReopen",
        "recordAttempt", "clearAttempt", "attemptMarker", "staleSource",
        "FileSystem.real", "CLIRunner.forApp", "thisApp",
        "URLSession.shared", "NSWorkspace.shared", "SecItem", "SecKeychain",
        // A real graft CLI, and a child process of any kind (the fix-check's
        // #8: a CLIRunner default, or `Process.run` of /usr/bin/security).
        "CLIRunner(", "GraftExecutable.bundled(", "GraftExecutable.resolve(", "GraftExecutable.locate(",
        "Process(", "Process.run",
        // The app's own wrappers: the general pasteboard, another app
        // (`NSWorkspace`), the notification center.
        "Pasteboard.copy", "MacAppStore.open", "BuildNotifier.primeAuthorization",
        // The quit question: an alert on screen, and the reply that quits
        // the app (`QuitGuard`, which `GraftAppDelegate` hands them to).
        "QuitAlert.present", "NSAlert(", "NSApp.reply(",
    ]

    /// SwiftUI property wrappers that act on the real thing unless the view's
    /// environment names another, as patterns, since a key path has more than
    /// one spelling. `@AppStorage` and `@SceneStorage` read and write the real
    /// preferences unless `.defaultAppStorage(_:)` says otherwise;
    /// `@Environment(\.openURL)` opens the URL in another app, the browser,
    /// unless `.environment(\.openURL, …)` does (delta audit try13, #15: a
    /// hosted test that pressed the Graft 1.1 download button would have
    /// opened github.com, and this scan stayed green).
    static let environmentWrappers = [
        #"@AppStorage\b"#,
        #"@SceneStorage\b"#,
        #"@Environment\s*\(\s*\\(?:EnvironmentValues)?\.openURL\s*\)"#,
    ]

    /// SwiftUI's `Link`, which opens its URL through the environment's
    /// `openURL` when pressed: the browser, unless the view's environment
    /// says otherwise. Not `NavigationLink` or `ShareLink`.
    static let link = #"(?<![\w.])Link\s*\("#

    /// The `Link`s allowed, by file, with how many each may hold.
    /// - `AppStoreConnectSection` (Open App Store Connect) and
    ///   `AccountSetupView` (each page a free Apple ID's step names): a
    ///   `Link` opens only when pressed, and its accessibility role is
    ///   `AXLink`, which `clickButton` (buttons only) never presses; no test
    ///   presses one. Kept as SwiftUI's link control for its look and its
    ///   role. A test that has to press one sets `.environment(\.openURL, …)`
    ///   on the view it hosts first, as `AppleSiliconGateTests` does as a
    ///   backstop. A new one is a decision, not an accident.
    static let allowedLinks: [String: Int] = [
        "Views/Settings/AppStoreConnectSection.swift": 1,
        "Views/Setup/AccountSetupView.swift": 1,
    ]

    /// The ones allowed, by `file:property`, each with its reason.
    /// - `AddAppSheet.lastDelivery` (`lastDeliveryMode`): SwiftUI's own seam
    ///   replaces its store, and every test that renders the sheet passes a
    ///   throwaway suite through `.defaultAppStorage` (AddAppDeliveryDefault,
    ///   AddAppFreeAccount, AddAppSheetPrefill, DeviceDetailAddWiring). It
    ///   reaches nothing past this process's preferences, and is written only
    ///   once an add the CLI accepted has gone through.
    static let allowedWrappers: Set<String> = [
        "Views/Apps/AddAppSheet.swift:lastDelivery",
    ]

    /// The composition point: the one file that names the real ones.
    static let compositionPoint = "GraftApp.swift"

    /// `static` singletons that are real by design, each built lazily on first
    /// use and named here so a new one is a decision, not an accident.
    /// - `ShellEnvironment.shared`: the login shell's answer, which `GraftApp`
    ///   starts at launch and passes down.
    /// - `CommandLineTool.Context.thisApp`: only `CommandLineToolSection`
    ///   reads it, and only where `isOffered` (a release build).
    static let singletons: Set<String> = [
        "Services/LoginShell.swift:shared",
        "Services/CommandLineTool.swift:thisApp",
    ]

    struct Finding: Equatable, CustomStringConvertible {
        let file: String
        let line: Int
        let text: String
        var description: String { "\(file):\(line): \(text)" }
    }

    // MARK: The tests

    /// The test host is the Debug build, whose bundle id is its own
    /// (project.yml's Debug configuration). Under the release id, AppKit and
    /// SwiftUI saved the host's window frames and split-view sizes ("NSWindow
    /// Frame SwiftUI.WindowGroup<…>", "NSSplitView Subview Frames …") into
    /// the owner's real Graft preferences, keys no Graft code writes, so no
    /// seam here could keep them out.
    func test_the_test_host_is_not_under_the_release_bundle_id() {
        let id = Bundle.main.bundleIdentifier
        XCTAssertNotNil(id)
        XCTAssertNotEqual(
            id, "com.greenglasst.graft",
            "the test host runs under the release bundle id, so AppKit saves its window frames "
                + "into the real Graft's preferences: give the Debug configuration its own id"
        )
    }

    func test_nothing_outside_the_composition_point_defaults_to_a_real_side_effect() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .appending(path: "Graft")
        let files = try XCTUnwrap(FileManager.default.enumerator(at: root, includingPropertiesForKeys: nil))
            .compactMap { $0 as? URL }
            .filter { $0.pathExtension == "swift" }
        XCTAssertGreaterThan(files.count, 50, "premise: the app's sources were found under \(root.path)")
        XCTAssertTrue(files.contains { $0.lastPathComponent == Self.compositionPoint })

        var found: [Finding] = []
        for file in files where file.lastPathComponent != Self.compositionPoint {
            let relative = String(file.path.dropFirst(root.path.count + 1))
            found += Self.findings(in: try String(contentsOf: file, encoding: .utf8), file: relative)
        }
        XCTAssertEqual(found, [], """
            A default, stored value or ?? fallback in the app names something that \
            reaches past this process. Make it a required parameter and pass the real \
            one from GraftApp, so a test that forgets its fake doesn't compile:
            \(found.map(\.description).joined(separator: "\n"))
            """)
    }

    /// The scanner sees each default the 2026-11-22 refactor removed, in the
    /// shapes they had: single and multi-line signatures, stored properties,
    /// leading-dot defaults, and a nil default made real by `??`.
    func test_the_scan_finds_every_default_that_was_removed() {
        let removed = """
        struct FallbackAdminShell: PrivilegedShell {
            var primary: PrivilegedShell = AppleScriptAdminShell()
            var fallback: PrivilegedShell = OsascriptAdminShell()
        }
        final class AppStoreAccountStore {
            init(runner: CLIRunner, shell: PrivilegedShell = FallbackAdminShell()) {}
        }
        final class AppStoreStore {
            init(runner: CLIRunner, shell: PrivilegedShell = FallbackAdminShell(), now: @escaping () -> Date = Date.init) {}
        }
        final class ConfigStore {
            @ObservationIgnored var postNote: (BuildNotifier.Note?) -> Void = { BuildNotifier.post($0) }
        }
        enum Runners {
            static func forApp(
                shell: ShellEnvironment = .shared,
                process: [String: String] = ProcessInfo.processInfo.environment
            ) -> CLIRunner { fatalError() }
        }
        enum LoginShell {
            static func resolve(
                shell: String = userShell(),
                run: Runner = runProcess
            ) async -> [String: String]? { nil }
        }
        struct Context {
            static func forApp(bundleURL: URL, fs: FileSystem = .real) -> Context { fatalError() }
        }
        final class RebuildCoordinator {
            init(runInstaller: (() async -> String?)? = nil) {
                self.runInstaller = runInstaller ?? RebuildCoordinator.installFromRepo
            }
            func startIfStale(clearAttempt: @escaping () -> Void = { SourceWatch.clearAttempt() }) {}
        }
        enum LegacyDefaults {
            static func migrate(into domain: String, store: PersistentDomainStore = UserDefaults.standard) -> Bool { true }
        }
        enum SourceWatch {
            static func recordAttempt(marker: URL? = nil) {
                let url = marker ?? attemptMarker
            }
        }
        """
        let lines = Self.findings(in: removed, file: "removed.swift").map(\.line)
        for line in [2, 3, 6, 9, 12, 16, 22, 23, 27, 31, 33, 36, 40] {
            XCTAssertTrue(lines.contains(line), "line \(line) of the removed defaults wasn't found: \(lines)")
        }
        XCTAssertFalse(lines.contains(17), "a read of the environment is no side effect")
    }

    /// Review part 4 fix-check, #8: the shapes the scan missed, each one a
    /// real CLI, a child process, the app's own pasteboard and workspace
    /// wrappers, an environment-backed preference, or a nil default made real
    /// with if-let/else or guard-let/else. Every one must be found.
    func test_the_scan_finds_runners_processes_wrappers_and_if_let_fallbacks() {
        let missed = """
        final class Store {
            init(runner: CLIRunner = CLIRunner(executable: GraftExecutable.bundled(in: Bundle.main.bundleURL))) {}
            let stored = CLIRunner(executable: URL(fileURLWithPath: "/usr/local/bin/graft"))
            var typed: CLIRunner = .forApp(shell: .shared)
            var lookup: () throws -> Void = { try Process.run(URL(fileURLWithPath: "/usr/bin/security"), arguments: []) }
            let child = Process()
            var copy: (String) -> Void = Pasteboard.copy
            func copier(copy: @escaping (String) -> Void = Pasteboard.copy) {}
            var open: () -> Void = MacAppStore.open
            func opener(open: @escaping () -> Void = { MacAppStore.open() }) {}
            @AppStorage("lastMode") private var lastMode = 0
            static func resolved(executable: URL = GraftExecutable.resolve(home: "/u", path: nil, isExecutable: { _ in true })) {}
            func spawn(child: Process = makeChild()) {}
            var executable: URL = GraftExecutable.bundled(in: Bundle.main.bundleURL)
            static func found(at: URL? = GraftExecutable.locate(home: "/u", path: nil, isExecutable: { _ in true })) {}
            var prime: () -> Void = BuildNotifier.primeAuthorization
        }
        final class Fallbacks {
            init(runner: CLIRunner? = nil, shell: PrivilegedShell? = nil) {
                if let runner {
                    self.runner = runner
                } else {
                    self.runner = CLIRunner.forApp(shell: .shared)
                }
                guard let shell = shell else {
                    self.shell = FallbackAdminShell(primary: AppleScriptAdminShell(), fallback: OsascriptAdminShell())
                    return
                }
            }
            var pasteboard: ((String) -> Void)? = nil
            func put(_ text: String) {
                if let paste = self.pasteboard { paste(text) } else { Pasteboard.copy(text) }
            }
        }
        """
        let lines = Self.findings(in: missed, file: "missed.swift").map(\.line)
        for line in [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 20, 25, 32] {
            XCTAssertTrue(lines.contains(line), "line \(line) of the missed shapes wasn't found: \(lines)")
        }
    }

    /// The allowed `@AppStorage` is allowed only in its own file, under its
    /// own name; anything else of the kind is a finding.
    func test_only_the_named_environment_wrapper_is_allowed() {
        let sheet = """
        struct AddAppSheet: View {
            @AppStorage("lastDeliveryMode") private var lastDelivery: DeliveryMode?
            @SceneStorage("tab") var tab = 0
        }
        """
        XCTAssertEqual(Self.findings(in: sheet, file: "Views/Apps/AddAppSheet.swift").map(\.line), [3])
        XCTAssertEqual(Self.findings(in: sheet, file: "Views/Apps/Other.swift").map(\.line), [2, 3])
    }

    /// Delta audit try13, #15. The positive control: every spelling of
    /// `@Environment(\.openURL)` is found, on the declaration's line or the
    /// one before it, and so is each `Link` past the file's allowance. A plain
    /// `"@Environment(\\.openURL)"` in the old list would have matched
    /// nothing: the pattern ended in `\b`, which can't follow `)`.
    func test_the_scan_finds_the_environments_url_opener_and_links() {
        let openers = #"""
        struct Gate: View {
            @Environment(\.openURL) private var openURL
            @Environment( \.openURL ) var open
            @Environment(\EnvironmentValues.openURL) var opener
            @Environment(\.openURL)
            private var onNextLine
            @Environment(\.dismiss) private var dismiss
            @Environment(\.openWindow) private var openWindow
            var body: some View {
                Link("Open", destination: url)
                NavigationLink("Next", value: 1)
                ShareLink(item: url)
                Button("Open") { openURL(url) }
            }
        }
        """#
        XCTAssertEqual(Self.findings(in: openers, file: "Views/Gate.swift").map(\.line), [2, 3, 4, 5, 10])
        XCTAssertEqual(Self.findings(in: openers, file: "Views/Settings/AppStoreConnectSection.swift").map(\.line),
                       [2, 3, 4, 5], "one Link allowed there")
        let two = """
        struct Section: View {
            var body: some View {
                Link("One", destination: a)
                Link("Two", destination: b)
            }
        }
        """
        XCTAssertEqual(Self.findings(in: two, file: "Views/Settings/AppStoreConnectSection.swift").map(\.line), [4],
                       "a second Link in an allowed file is a finding")
        XCTAssertEqual(Self.environmentWrappers.count, 3)
    }

    func test_the_scan_leaves_ordinary_defaults_and_bodies_alone() {
        let ordinary = """
        final class Store {
            private let lock = NSLock()
            var count: Int = 0
            var onChange: () async -> Void = {}
            private var shell: PrivilegedShell? = nil
            init(runner: CLIRunner, cliArgs: [String] = ["--json", "--list"], now: Date = Date()) {}
            func copy(_ text: String) {
                let pb = NSPasteboard.general
                pb.setString(text, forType: .string)
            }
            // init(shell: PrivilegedShell = FallbackAdminShell())
            static let message = "FallbackAdminShell = UserDefaults.standard"
            func build() async -> Bool { let built = await runBuildProcess(); return built }
            func copyButton(_ text: String) { Pasteboard.copy(text) }
            init(label: String? = nil) {
                if let label { self.label = label } else { self.label = "none" }
                if let other = otherThing { use(other) } else { Pasteboard.copy("x") }
            }
            var fallback: String = savedName ?? runBuildProcess()
            @Environment(\\.dismiss) private var dismiss
        }
        """
        XCTAssertEqual(Self.findings(in: ordinary, file: "ordinary.swift"), [],
                       "comments, strings, locals in bodies and harmless defaults are not findings")
    }

    /// A raw string with quotes inside, and an interpolation with a string
    /// literal inside, must neither end early nor hide what follows: the
    /// property after them is still in its type's scope, and still found.
    func test_the_scan_survives_raw_strings_and_interpolations() {
        let tricky = ##"""
        struct Tricky {
            static let line = #"{"type":"x","shell":"FallbackAdminShell()"}"#
            var label: String { "\(name ?? "none") {" }
            static let regexish = "}}}"
            var shell: PrivilegedShell = FallbackAdminShell()
        }
        """##
        XCTAssertEqual(Self.findings(in: tricky, file: "tricky.swift").map(\.line), [5])
    }

    // MARK: The scanner

    static func findings(in source: String, file: String) -> [Finding] {
        let code = blankCommentsAndStrings(source)
        let characters = Array(code)
        var found: [Finding] = []
        func line(at index: Int) -> Int { characters[..<index].reduce(1) { $1 == "\n" ? $0 + 1 : $0 } }
        func flag(_ index: Int, _ text: String) {
            found.append(Finding(file: file, line: line(at: index), text: text.split(whereSeparator: \.isWhitespace).joined(separator: " ")))
        }

        // Names that default to nil, for the if-let/else check below.
        var nilDefaulted = Set<String>()

        // Parameter defaults.
        let signature = try! NSRegularExpression(
            pattern: #"\b(?:init[?!]?|func\s+[^\s(<]+|case\s+\w+|subscript)\s*(?:<[^>{}]*>)?\s*\("#
        )
        let ns = code as NSString
        for match in signature.matches(in: code, range: NSRange(location: 0, length: ns.length)) {
            let open = match.range.location + match.range.length
            guard let close = matchingClose(characters, from: open) else { continue }
            var start = open
            for fragment in topLevelSplit(characters, from: open, to: close, on: ",") {
                defer { start += fragment.count + 1 }
                guard let equals = topLevelEquals(fragment) else { continue }
                let declaration = String(fragment[..<equals])
                let value = String(fragment[(equals + 1)...]).trimmingCharacters(in: .whitespacesAndNewlines)
                guard let colon = declaration.firstIndex(of: ":") else { continue }
                let type = bareType(String(declaration[declaration.index(after: colon)...]))
                if value == "nil", let name = declaration[..<colon].split(whereSeparator: \.isWhitespace).last {
                    nilDefaulted.insert(String(name))
                }
                if (value != "nil" && sideEffectingTypes.contains(type)) || namesARealOne(value) {
                    flag(start + (fragment.firstIndex { !$0.isWhitespace } ?? 0), String(fragment))
                }
            }
        }

        // Stored properties, in a type's own scope.
        let property = try! NSRegularExpression(pattern: #"""
            ^\s*(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:private|fileprivate|internal|public|open|static|class|nonisolated|lazy|weak|unowned|final|override)(?:\(set\))?\s+)*(?:var|let)\s+(\w+)\s*(?::\s*([^=]+?))?\s*=(?!=)\s*(.*)$
            """#)
        var scopes: [Bool] = []  // true: a type's body
        var header = ""
        var index = 0
        while index < characters.count {
            let c = characters[index]
            if c == "{" {
                scopes.append(isTypeHeader(header))
                header = ""
            } else if c == "}" {
                _ = scopes.popLast()
                header = ""
            } else if c == ";" {
                header = ""
            } else {
                header.append(c)
            }
            if c == "\n", scopes.last == true {
                let end = characters[(index + 1)...].firstIndex(of: "\n") ?? characters.count
                let text = String(characters[(index + 1)..<end])
                if let match = property.firstMatch(in: text, range: NSRange(location: 0, length: (text as NSString).length)) {
                    let t = text as NSString
                    let name = t.substring(with: match.range(at: 1))
                    let type = match.range(at: 2).location == NSNotFound ? "" : bareType(t.substring(with: match.range(at: 2)))
                    let value = continued(characters, from: index + 1 + t.substring(to: match.range(at: 3).location).count)
                    if value.trimmingCharacters(in: .whitespaces) == "nil" { nilDefaulted.insert(name) }
                    let isSingleton = text.contains("static ") && singletons.contains("\(file):\(name)")
                    if !isSingleton, (value.trimmingCharacters(in: .whitespaces) != "nil" && sideEffectingTypes.contains(type))
                        || namesARealOne(value) {
                        flag(index + 1, text)
                    }
                }
            }
            index += 1
        }

        // `?? <a real one>`: a nil default made real in the body. Any prefix
        // ends in a dot, so `?? runBuildProcess(` isn't `Process(`.
        for value in sideEffectingValues {
            let pattern = #"\?\?\s*(?:[\w.]*\.)?"# + NSRegularExpression.escapedPattern(for: value)
            let fallback = try! NSRegularExpression(pattern: pattern)
            for match in fallback.matches(in: code, range: NSRange(location: 0, length: ns.length)) {
                let offset = ns.substring(to: match.range.location).count
                flag(offset, ns.substring(with: match.range))
            }
        }

        // The same fallback as `if let`/`guard let`, for a name that defaults
        // to nil: the branch taken without one names a real one.
        for name in nilDefaulted.sorted() {
            let n = NSRegularExpression.escapedPattern(for: name)
            let unwrap = try! NSRegularExpression(
                pattern: #"\b(if|guard)\b[^{}]*?\blet\s+(?:"# + n + #"\b(?!\s*=)|\w+\s*=\s*(?:self\.)?"# + n + #"\b)"#
            )
            let isNil = try! NSRegularExpression(pattern: #"\bif\s+(?:self\.)?"# + n + #"\s*==\s*nil\b"#)
            for match in unwrap.matches(in: code, range: NSRange(location: 0, length: ns.length)) {
                let start = ns.substring(to: match.range.location).count
                let end = start + ns.substring(with: match.range).count
                let fallback = ns.substring(with: match.range(at: 1)) == "guard"
                    ? guardElse(characters, from: end) : ifElse(characters, from: end)
                if let fallback, namesARealOne(fallback) {
                    flag(start, String(characters[start..<min(end, characters.count)]))
                }
            }
            for match in isNil.matches(in: code, range: NSRange(location: 0, length: ns.length)) {
                let start = ns.substring(to: match.range.location).count
                let end = start + ns.substring(with: match.range).count
                if let block = block(characters, from: end), namesARealOne(block) {
                    flag(start, String(characters[start..<end]))
                }
            }
        }

        // SwiftUI property wrappers backed by the real thing, up to the
        // declaration they're on (the next line, too).
        let wrapper = try! NSRegularExpression(
            pattern: #"("# + environmentWrappers.joined(separator: "|") + #")[^;{}]*?\b(?:var|let)\s+(\w+)"#
        )
        for match in wrapper.matches(in: code, range: NSRange(location: 0, length: ns.length)) {
            let name = ns.substring(with: match.range(at: 2))
            guard !allowedWrappers.contains("\(file):\(name)") else { continue }
            flag(ns.substring(to: match.range.location).count, ns.substring(with: match.range))
        }

        // `Link`s past the number `allowedLinks` gives the file.
        let links = try! NSRegularExpression(pattern: link).matches(in: code, range: NSRange(location: 0, length: ns.length))
        for match in links.dropFirst(allowedLinks[file] ?? 0) {
            flag(ns.substring(to: match.range.location).count, ns.substring(with: match.range))
        }
        return found
    }

    /// A value names a real one: one of `sideEffectingValues`, not as the
    /// tail of a longer name (`runBuildProcess(` isn't `Process(`).
    static func namesARealOne(_ value: String) -> Bool {
        sideEffectingValues.contains { real in
            var search = value.startIndex..<value.endIndex
            while let found = value.range(of: real, range: search) {
                if found.lowerBound == value.startIndex { return true }
                let before = value[value.index(before: found.lowerBound)]
                if !(before.isLetter || before.isNumber || before == "_") { return true }
                search = found.upperBound..<value.endIndex
            }
            return false
        }
    }

    /// The `{ … }` that opens at the first `{` from `from`, or nil.
    static func block(_ characters: [Character], from: Int) -> String? {
        guard let open = characters[from...].firstIndex(of: "{"),
              let close = matchingClose(characters, from: open + 1) else { return nil }
        return String(characters[open...close])
    }

    /// After `if let …`: the `else` branch's text (an `else if` chain
    /// included), or nil when there is none.
    static func ifElse(_ characters: [Character], from: Int) -> String? {
        guard let open = characters[from...].firstIndex(of: "{"),
              let close = matchingClose(characters, from: open + 1) else { return nil }
        var i = close + 1
        while i < characters.count, characters[i].isWhitespace { i += 1 }
        guard i + 4 <= characters.count, String(characters[i..<(i + 4)]) == "else" else { return nil }
        var end = i + 4
        // An `else if …` chain: every branch after the first.
        while let next = characters[end...].firstIndex(of: "{"), let shut = matchingClose(characters, from: next + 1) {
            end = shut + 1
            var k = end
            while k < characters.count, characters[k].isWhitespace { k += 1 }
            guard k + 4 <= characters.count, String(characters[k..<(k + 4)]) == "else" else { break }
            end = k + 4
        }
        return String(characters[i..<end])
    }

    /// After `guard let …`: its `else` block.
    static func guardElse(_ characters: [Character], from: Int) -> String? {
        var i = from
        while i + 4 <= characters.count {
            if String(characters[i..<(i + 4)]) == "else" { return block(characters, from: i + 4) }
            if characters[i] == "{" { return nil }
            i += 1
        }
        return nil
    }

    /// `@escaping @MainActor (X) -> Y`, `Foo?`, `inout Foo` to the type's name.
    static func bareType(_ declared: String) -> String {
        var type = declared.trimmingCharacters(in: .whitespacesAndNewlines)
        for prefix in ["@escaping", "@Sendable", "@MainActor", "inout", "some", "any"] {
            while type.hasPrefix(prefix + " ") { type = String(type.dropFirst(prefix.count + 1)).trimmingCharacters(in: .whitespaces) }
        }
        while type.hasSuffix("?") || type.hasSuffix("!") { type.removeLast() }
        return type
    }

    /// A `{` opens a type's body when what came before it, since the last
    /// brace, ends in a `struct`, `class`, `enum`, `extension`, `actor` or
    /// `protocol` declaration (allowing one continuation line, e.g. `where`).
    static func isTypeHeader(_ header: String) -> Bool {
        let lines = header.split(separator: "\n").map(String.init).filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
        guard let last = lines.last else { return false }
        let typeDecl = #"(^|\s)(struct|class|enum|extension|actor|protocol)\s+[A-Za-z_]"#
        let statement = #"\b(func|init|var|let|if|for|while|switch|guard|return|case|else|do|catch|defer|repeat)\b|=|\bin\s*$"#
        if last.range(of: typeDecl, options: .regularExpression) != nil,
           last.range(of: #"\b(func|var|let)\b|="#, options: .regularExpression) == nil {
            return true
        }
        guard lines.count >= 2, last.range(of: statement, options: .regularExpression) == nil else { return false }
        let previous = lines[lines.count - 2]
        return previous.range(of: typeDecl, options: .regularExpression) != nil
            && previous.range(of: #"\b(func|var|let)\b|="#, options: .regularExpression) == nil
    }

    /// The value starting at `from`, through any brackets it leaves open.
    static func continued(_ characters: [Character], from: Int) -> String {
        var depth = 0
        var end = from
        while end < characters.count {
            let c = characters[end]
            if "([{".contains(c) { depth += 1 }
            if ")]}".contains(c) { depth -= 1 }
            if c == "\n", depth <= 0 { break }
            end += 1
        }
        return String(characters[from..<end])
    }

    static func matchingClose(_ characters: [Character], from: Int) -> Int? {
        var depth = 1
        var i = from
        while i < characters.count {
            if "([{".contains(characters[i]) { depth += 1 }
            if ")]}".contains(characters[i]) {
                depth -= 1
                if depth == 0 { return i }
            }
            i += 1
        }
        return nil
    }

    /// `characters[from..<to]` split on top-level `separator`: not inside
    /// brackets, nor a generic's angle brackets (`->` is not one).
    static func topLevelSplit(_ characters: [Character], from: Int, to: Int, on separator: Character) -> [[Character]] {
        var parts: [[Character]] = [[]]
        var depth = 0
        for i in from..<to {
            let c = characters[i]
            if "([{".contains(c) { depth += 1 }
            if ")]}".contains(c) { depth -= 1 }
            if c == "<", i > 0, characters[i - 1].isLetter || characters[i - 1].isNumber { depth += 1 }
            if c == ">", i > 0, characters[i - 1] != "-", depth > 0 { depth -= 1 }
            if c == separator, depth == 0 { parts.append([]) } else { parts[parts.count - 1].append(c) }
        }
        return parts
    }

    /// The default's `=`, at the fragment's top level, never `==` or `=>`.
    static func topLevelEquals(_ fragment: [Character]) -> Int? {
        var depth = 0
        for (i, c) in fragment.enumerated() {
            if "([{".contains(c) { depth += 1 }
            if ")]}".contains(c) { depth -= 1 }
            guard c == "=", depth == 0 else { continue }
            let next = i + 1 < fragment.count ? fragment[i + 1] : " "
            let previous = i > 0 ? fragment[i - 1] : " "
            if next != "=", previous != "=", previous != "!", previous != "<", previous != ">" { return i }
        }
        return nil
    }

    /// Comments and string literals as spaces, newlines kept, so names in
    /// either never count and line numbers still do. Raw strings end at their
    /// own number of `#`s, and an interpolation (`"\\(x ?? "none")"`) is code
    /// again, nested literals and all, so a quote inside one can't end the
    /// string early and throw the brace count off.
    static func blankCommentsAndStrings(_ source: String) -> String {
        Lexer(Array(source)).run()
    }

    private final class Lexer {
        let s: [Character]
        var out: [Character] = []
        var i = 0

        init(_ s: [Character]) {
            self.s = s
            out.reserveCapacity(s.count)
        }

        func run() -> String {
            code(untilClose: false)
            return String(out)
        }

        private func at(_ k: Int) -> Character? { k < s.count ? s[k] : nil }
        private func blank(_ c: Character) -> Character { c == "\n" ? "\n" : " " }

        /// Code, until the `)` that closes an interpolation when `untilClose`.
        private func code(untilClose: Bool) {
            var depth = 0
            while i < s.count {
                let c = s[i]
                if c == "/", at(i + 1) == "/" {
                    while i < s.count, s[i] != "\n" { out.append(" "); i += 1 }
                } else if c == "/", at(i + 1) == "*" {
                    blockComment()
                } else if c == "\"" || (c == "#" && startsRawString()) {
                    string()
                } else {
                    if untilClose {
                        if c == "(" { depth += 1 }
                        if c == ")" {
                            if depth == 0 { out.append(c); i += 1; return }
                            depth -= 1
                        }
                    }
                    out.append(c); i += 1
                }
            }
        }

        private func startsRawString() -> Bool {
            var k = i
            while at(k) == "#" { k += 1 }
            return at(k) == "\""
        }

        private func blockComment() {
            var depth = 0
            repeat {
                if s[i] == "/", at(i + 1) == "*" { depth += 1; out += [" ", " "]; i += 2; continue }
                if s[i] == "*", at(i + 1) == "/" { depth -= 1; out += [" ", " "]; i += 2; continue }
                out.append(blank(s[i])); i += 1
            } while i < s.count && depth > 0
        }

        private func string() {
            var hashes = 0
            while s[i] == "#" { out.append("#"); hashes += 1; i += 1 }
            let triple = at(i + 1) == "\"" && at(i + 2) == "\""
            let quotes = triple ? 3 : 1
            out += Array(repeating: "\"", count: quotes); i += quotes
            while i < s.count {
                if closes(quotes: quotes, hashes: hashes) {
                    out += Array(repeating: "\"", count: quotes) + Array(repeating: "#", count: hashes)
                    i += quotes + hashes
                    return
                }
                if s[i] == "\\", (0..<hashes).allSatisfy({ at(i + 1 + $0) == "#" }) {
                    let after = i + 1 + hashes
                    out += Array(repeating: " ", count: 1 + hashes); i = after
                    if at(i) == "(" {
                        out.append("("); i += 1
                        code(untilClose: true)
                    } else if i < s.count {
                        out.append(blank(s[i])); i += 1
                    }
                    continue
                }
                out.append(blank(s[i])); i += 1
            }
        }

        private func closes(quotes: Int, hashes: Int) -> Bool {
            (0..<quotes).allSatisfy { at(i + $0) == "\"" } && (0..<hashes).allSatisfy { at(i + quotes + $0) == "#" }
        }
    }
}
