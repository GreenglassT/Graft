import Combine
import SwiftUI
import XCTest
@testable import Graft

/// The stores are `ObservableObject`s, which macOS 13 has (Observation's
/// `@Observable` is macOS 14's). A view now redraws for a store's change
/// only if the store publishes it and the view observes the store: Observation
/// did both on its own, property by property, so a missed `@Published` or
/// `@ObservedObject` compiles and silently stops a view updating. These read
/// the app's source for both, and check what `@Published` alone can't cover:
/// `AppStoreStore`'s nested stores, a view's optional store
/// (`ObservedOptionalObject`), and `onChange`'s previous value on macOS 13
/// (`ChangeWithPrevious`).
@MainActor
final class ObservableStoresTests: XCTestCase {

    // MARK: The source

    struct Finding: Equatable, CustomStringConvertible {
        let file: String
        let line: Int
        let text: String
        var description: String { "\(file):\(line): \(text)" }
    }

    /// The app's Swift sources, by path under Graft/Graft.
    static func appSources() throws -> [(path: String, source: String)] {
        try sources(in: "Graft")
    }

    /// These tests' own, whose harness views read stores as the app's views
    /// do: one that holds a store it doesn't observe draws it once and never
    /// again, and the test then measures the store's first state.
    static func testSources() throws -> [(path: String, source: String)] {
        try sources(in: "GraftTests")
    }

    static func sources(in folder: String) throws -> [(path: String, source: String)] {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .appending(path: folder)
        let files = try XCTUnwrap(FileManager.default.enumerator(at: root, includingPropertiesForKeys: nil))
            .compactMap { $0 as? URL }
            .filter { $0.pathExtension == "swift" }
        return try files.map {
            (String($0.path.dropFirst(root.path.count + 1)), try String(contentsOf: $0, encoding: .utf8))
        }
    }

    func test_every_store_state_property_is_published_or_says_why_not() throws {
        let sources = try Self.appSources()
        XCTAssertGreaterThan(sources.count, 50, "premise: the app's sources were found")
        let declared = Self.declared(in: sources)
        XCTAssertTrue(
            declared.stores.isSuperset(of: ["ConfigStore", "AppStoreStore", "AppStoreAccountStore", "UpdateStore", "RebuildCoordinator", "DumpLog"]),
            "premise: the stores were found: \(declared.stores.sorted())"
        )
        XCTAssertGreaterThanOrEqual(declared.stores.count, 22)
        let found = sources.flatMap { Self.scan($0.source, file: $0.path, declared: declared).unpublished }
        XCTAssertEqual(found, [], """
            A store's stored property is neither @Published nor marked "/// Unpublished: <why>" \
            in the doc comment above it. A view reading it would not redraw when it changes:
            \(found.map(\.description).joined(separator: "\n"))
            """)
    }

    func test_every_view_observes_the_stores_it_reads() throws {
        let sources = try Self.appSources() + Self.testSources()
        let declared = Self.declared(in: sources)
        XCTAssertTrue(declared.stores.contains("FormBox"), "premise: the tests' own stores were found")
        XCTAssertTrue(declared.views.isSuperset(of: ["ContentView", "AddAppSheet", "GraftApp"]), "premise: the views were found")
        let scans = sources.map { Self.scan($0.source, file: $0.path, declared: declared) }
        XCTAssertGreaterThan(
            scans.map(\.holding).reduce(0, +), 90,
            "premise: the views' store properties were found"
        )
        XCTAssertGreaterThanOrEqual(
            scans.map(\.passedThrough).reduce(0, +), 18,
            "premise: the stores views only hand on were found, and proven"
        )
        let found = scans.flatMap(\.unobserved)
        XCTAssertEqual(found, [], """
            A view holds a store it doesn't observe, and reads it while drawing, so it \
            would not redraw when the store changes. Use @ObservedObject (@StateObject \
            where the view makes it, @ObservedOptionalObject for an optional one). \
            @PassedThrough, or plain @State for a store the view makes, is only for a \
            store the view hands to a child view's initializer or uses inside a Task:
            \(found.map(\.description).joined(separator: "\n"))
            """)
    }

    /// `AppStoreStore` sends on only `account`'s changes. A view reading
    /// `appStore.search.state` would observe only `appStore`, and miss every
    /// change Search makes: it must observe `search` itself.
    func test_no_view_reads_a_nested_store_its_outer_store_does_not_send_on() throws {
        let sources = try Self.appSources() + Self.testSources()
        let declared = Self.declared(in: sources)
        let nested = try XCTUnwrap(declared.nested["AppStoreStore"], "premise: AppStoreStore's nested stores were found")
        XCTAssertEqual(
            Dictionary(uniqueKeysWithValues: nested.map { ($0.name, $0.forwarded) }),
            ["search": false, "account": true, "olderVersions": false]
        )
        let found = sources.flatMap { Self.scan($0.source, file: $0.path, declared: declared).readsNested }
        XCTAssertEqual(found, [], """
            A view reads a nested store's state through a store that doesn't send that \
            nested store's changes on, so it would not redraw: observe the nested store \
            directly.
            \(found.map(\.description).joined(separator: "\n"))
            """)
    }

    func test_the_scan_finds_what_a_view_or_store_gets_wrong() {
        let source = """
        @MainActor
        final class SampleStore: ObservableObject, CLIStore {
            @Published private(set) var shown = 0
            private(set) var missed: String?
            /// Unpublished: a change to it alone redraws nothing,
            /// on two lines.
            private var task: Task<Void, Never>?
            var withObserver: Bool {
                didSet { print("x = {") }
            }
            var computed: Int { shown + 1 }
            var getter: Int {
                get { shown }
            }
            static var shared = 0
            let runner: Int
            struct Nested { var free = 0 }
            func work() { var local = 0; local += 1 }
        }
        final class OuterStore: ObservableObject {
            let inner: SampleStore
            let sent: SampleStore
            /// Unpublished: set once.
            private var forwarding: Any?
            init() { forwarding = sent.objectWillChange.sink { _ in } }
        }
        struct SampleView: View {
            @ObservedObject var good: SampleStore
            let bad: SampleStore
            var optional: SampleStore? = nil
            @State private var made = SampleStore()
            @StateObject private var owned = SampleStore()
            @ObservedOptionalObject var maybe: SampleStore? = nil
            private var derived: SampleStore { good }
            var body: some View { Text("\\(made.shown)") }
        }
        struct NotAView { let store: SampleStore }
        struct PassingView: View {
            @PassedThrough var handed: SampleStore
            @State private var kept: SampleStore
            @PassedThrough var leaky: SampleStore
            @PassedThrough var called: SampleStore
            @PassedThrough var bound: SampleStore
            @ObservedObject var outer: OuterStore
            init(handed: SampleStore, leaky: SampleStore, called: SampleStore, bound: SampleStore, outer: OuterStore) {
                self.handed = handed
                _kept = State(initialValue: SampleStore())
                self.leaky = leaky
                self.called = called
                self.bound = bound
                self.outer = outer
            }
            var body: some View {
                VStack {
                    SampleChild(store: handed)
                    SampleChild(store: self.kept)
                    Text("\\(leaky.shown)")
                    Text(label(called))
                    Toggle("", isOn: $bound.flag)
                    Text("\\(outer.inner.shown)")
                    Text("\\(outer.sent.shown)")
                    SampleChild(store: outer.inner)
                }
                .task { await kept.work() }
                .onAppear { Task { outer.inner.work() } }
            }
        }
        struct SampleChild: View {
            @ObservedObject var store: SampleStore
            var body: some View { Text("") }
        }
        """
        let declared = Self.declared(in: [("Sample.swift", source)])
        XCTAssertEqual(declared.stores, ["SampleStore", "OuterStore"])
        XCTAssertEqual(declared.views, ["SampleView", "PassingView", "SampleChild"])
        let scan = Self.scan(source, file: "Sample.swift", declared: declared)
        XCTAssertEqual(scan.unpublished.map(\.text), ["private(set) var missed: String?", "var withObserver: Bool {"])
        XCTAssertEqual(scan.unobserved.map(\.text), [
            "let bad: SampleStore", "var optional: SampleStore? = nil", "@State private var made = SampleStore()",
            "@PassedThrough var leaky: SampleStore", "@PassedThrough var called: SampleStore",
            "@PassedThrough var bound: SampleStore",
        ])
        XCTAssertEqual(scan.passedThrough, 2, "handed and kept are only handed on")
        XCTAssertEqual(scan.readsNested.map(\.text), [#"Text("\(outer.inner.shown)")"#])
        XCTAssertEqual(scan.holding, 13)
    }

    /// What `declared(in:)` found across every source, which a file's scan
    /// needs: what is a store, what is a view, and what each store nests.
    struct Declared {
        var stores: Set<String> = []
        var views: Set<String> = []
        /// A store's `let`s that are stores too, and whether it sends their
        /// changes on as its own (`<name>.objectWillChange`).
        var nested: [String: [(name: String, forwarded: Bool)]] = [:]
    }

    /// What `scan` found in one file.
    struct Scan {
        /// Stores' stored `var`s neither `@Published` nor marked.
        var unpublished: [Finding] = []
        /// View, view modifier and App properties typed as a store.
        var holding = 0
        /// Those held without observing, and proven only handed on.
        var passedThrough = 0
        /// Those held without observing that the view reads.
        var unobserved: [Finding] = []
        /// `outer.nested.member` reads, where `outer` doesn't send `nested`'s
        /// changes on.
        var readsNested: [Finding] = []
    }

    private enum Kind { case store, view, other }

    private static let header = try! NSRegularExpression(pattern: #"\b(class|struct|enum|extension|actor|protocol)\s+(\w+)([^{]*)"#)
    private static let member = try! NSRegularExpression(pattern:
        #"^\s*((?:@\w+(?:\([^)]*\))?\s+)*)((?:(?:private|fileprivate|internal|public|nonisolated|static|class|lazy|weak|unowned|final|override)(?:\(\w+\))?\s+)*)(let|var)\s+(\w+)(.*)$"#)

    /// The type declared on this line, if it opens one: its kind, name and
    /// what follows the name (its conformances).
    private static func typeHeader(_ line: String) -> (kind: Kind, name: String)? {
        let range = NSRange(line.startIndex..., in: line)
        guard line.contains("{"), let m = header.firstMatch(in: line, range: range) else { return nil }
        let text = { (i: Int) in Range(m.range(at: i), in: line).map { String(line[$0]) } ?? "" }
        let conformances = text(3)
        if text(1) == "class", conformances.contains("ObservableObject") { return (.store, text(2)) }
        if text(1) == "struct",
           conformances.range(of: #"\b(View|ViewModifier|App)\b"#, options: .regularExpression) != nil {
            return (.view, text(2))
        }
        return (.other, text(2))
    }

    /// A member declared on this line: attributes, modifiers, `let` or `var`,
    /// name, and the rest of the line.
    private static func memberDeclaration(_ line: String) -> (attributes: String, modifiers: String, keyword: String, name: String, rest: String)? {
        guard let m = member.firstMatch(in: line, range: NSRange(line.startIndex..., in: line)) else { return nil }
        let text = { (i: Int) in Range(m.range(at: i), in: line).map { String(line[$0]) } ?? "" }
        return (text(1), text(2), text(3), text(4), text(5))
    }

    static func declared(in sources: [(path: String, source: String)]) -> Declared {
        var declared = Declared()
        var lets: [(store: String, name: String, type: String, forwarded: Bool)] = []
        for (_, source) in sources {
            let blank = NoRealDefaultsTests.blankCommentsAndStrings(source)
            let code = blank.components(separatedBy: "\n")
            var scopes: [(kind: Kind, name: String, depth: Int)] = []
            var depth = 0
            for line in code {
                let before = depth
                if let top = scopes.last, top.kind == .store, before == top.depth,
                   let m = memberDeclaration(line), m.keyword == "let", let type = storeType(m.rest) {
                    lets.append((top.name, m.name, type, blank.contains("\(m.name).objectWillChange")))
                }
                if let found = typeHeader(line) {
                    if found.kind == .store { declared.stores.insert(found.name) }
                    if found.kind == .view { declared.views.insert(found.name) }
                    scopes.append((found.kind, found.name, before + 1))
                }
                depth += line.filter { $0 == "{" }.count - line.filter { $0 == "}" }.count
                while let top = scopes.last, top.depth > depth { scopes.removeLast() }
            }
        }
        for entry in lets where declared.stores.contains(entry.type) {
            declared.nested[entry.store, default: []].append((entry.name, entry.forwarded))
        }
        return declared
    }

    /// Reads type bodies by brace depth, in the source with its comments and
    /// strings blanked (`NoRealDefaultsTests.blankCommentsAndStrings`), and the
    /// marker from the original lines above.
    static func scan(_ source: String, file: String, declared: Declared) -> Scan {
        let original = source.components(separatedBy: "\n")
        let code = NoRealDefaultsTests.blankCommentsAndStrings(source).components(separatedBy: "\n")
        struct Held { let name: String; let type: String; let attributes: String; let line: Int }
        var scopes: [(kind: Kind, depth: Int, opened: Int, held: [Held])] = []
        var scan = Scan()
        var depth = 0
        func finding(_ line: Int) -> Finding {
            Finding(file: file, line: line + 1, text: original[line].trimmingCharacters(in: .whitespaces))
        }
        for (index, line) in code.enumerated() {
            let before = depth
            if let top = scopes.last, before == top.depth, top.kind != .other, let m = memberDeclaration(line) {
                var attributes = m.attributes
                // An attribute on a line of its own above.
                if index > 0, code[index - 1].range(of: #"^\s*(@\w+(\([^)]*\))?\s*)+$"#, options: .regularExpression) != nil {
                    attributes += code[index - 1]
                }
                let next = index + 1 < code.count ? code[index + 1] : ""
                let stored = !m.modifiers.contains("static") && !m.modifiers.contains("class ")
                    && !m.modifiers.contains("lazy") && !isComputed(m.rest, next: next)
                if stored, top.kind == .store, m.keyword == "var", !attributes.contains("@Published"),
                   !docComment(above: index, in: original).contains(where: { $0.hasPrefix("/// Unpublished") }) {
                    scan.unpublished.append(finding(index))
                }
                if stored, top.kind == .view, let type = storeType(m.rest), declared.stores.contains(type) {
                    scopes[scopes.count - 1].held.append(Held(name: m.name, type: type, attributes: attributes, line: index))
                }
            }
            if let found = typeHeader(line) {
                scopes.append((found.kind, before + 1, index, []))
            }
            depth += line.filter { $0 == "{" }.count - line.filter { $0 == "}" }.count
            while let top = scopes.last, top.depth > depth {
                scopes.removeLast()
                guard top.kind == .view else { continue }
                for held in top.held {
                    scan.holding += 1
                    // The view's own code, without the property's declaration.
                    var lines = Array(code[top.opened...index])
                    lines[held.line - top.opened] = ""
                    let body = lines.joined(separator: "\n")
                    let observes = ["@ObservedObject", "@StateObject", "@ObservedOptionalObject"]
                    if observes.contains(where: { held.attributes.contains($0) }) {
                        // Observed: fine.
                    } else if held.attributes.contains("@PassedThrough") || held.attributes.contains("@State"),
                              reads(of: held.name, in: body, views: declared.views).isEmpty {
                        scan.passedThrough += 1
                    } else {
                        scan.unobserved.append(finding(held.line))
                    }
                    let unsent = declared.nested[held.type, default: []].filter { !$0.forwarded }.map(\.name)
                    for nested in unsent {
                        let pattern = #"(?<![\w$])"# + held.name + #"\s*\.\s*"# + nested + #"\s*\.\s*\w+\b(?!\s*\()"#
                        let regex = try! NSRegularExpression(pattern: pattern)
                        let text = body as NSString
                        for m in regex.matches(in: body, range: NSRange(location: 0, length: text.length)) {
                            let line = text.substring(to: m.range.location).filter { $0 == "\n" }.count
                            scan.readsNested.append(finding(top.opened + line))
                        }
                    }
                }
            }
        }
        return scan
    }

    /// Where `code` (a view's own, its declaration of `name` removed) uses
    /// `name` other than to hand it on, as UTF-16 offsets. Handing it on is a
    /// label (`name:`); the initializer's assignment (`self.name = name`,
    /// `_name = …`); an argument to a view's initializer
    /// (`AddAppSheet(appStore: name)`); or a use inside a `Task { }` or
    /// `.task { }`, which runs after drawing, never during it.
    static func reads(of name: String, in code: String, views: Set<String>) -> [Int] {
        let text = code as NSString
        let whole = NSRange(location: 0, length: text.length)
        func at(_ i: Int) -> Character? {
            i >= 0 && i < text.length ? Character(UnicodeScalar(text.character(at: i)) ?? " ") : nil
        }
        var actions: [Range<Int>] = []
        let action = try! NSRegularExpression(pattern: #"(?<![\w.])Task\s*(?:\([^)]*\))?\s*\{|\.task\s*(?:\([^)]*\))?\s*\{"#)
        for m in action.matches(in: code, range: whole) {
            let open = m.range.location + m.range.length - 1
            var depth = 0, j = open
            while let c = at(j) {
                if c == "{" { depth += 1 }
                if c == "}" { depth -= 1; if depth == 0 { break } }
                j += 1
            }
            actions.append(open..<j)
        }
        /// An argument directly inside a view's initializer's parentheses.
        func viewArgument(from start: Int, to end: Int) -> Bool {
            var j = end
            while let c = at(j), c.isWhitespace { j += 1 }
            guard let next = at(j), next == "," || next == ")" else { return false }
            var depth = 0
            j = start - 1
            while let c = at(j) {
                if ")]}".contains(c) { depth += 1 }
                if "([{".contains(c) {
                    if depth > 0 { depth -= 1 } else {
                        guard c == "(" else { return false }
                        var k = j - 1, callee = ""
                        while let d = at(k), d.isLetter || d.isNumber || d == "_" || d == "." { callee.insert(d, at: callee.startIndex); k -= 1 }
                        return views.contains(String(callee.split(separator: ".").last ?? ""))
                    }
                }
                j -= 1
            }
            return false
        }
        var found: [Int] = []
        let use = try! NSRegularExpression(pattern: #"(?<![\w$])[$_]?"# + name + #"\b"#)
        let assignment = try! NSRegularExpression(pattern: #"^\s*(self\.)?_?"# + name + #"\s*="#)
        for m in use.matches(in: code, range: whole) {
            let start = m.range.location, end = start + m.range.length
            let lineStart = text.range(of: "\n", options: .backwards, range: NSRange(location: 0, length: start)).upperBound
            let lineEnd = text.range(of: "\n", range: NSRange(location: start, length: text.length - start)).location
            let line = text.substring(with: NSRange(location: lineStart, length: (lineEnd == NSNotFound ? text.length : lineEnd) - lineStart))
            if assignment.firstMatch(in: line, range: NSRange(location: 0, length: (line as NSString).length)) != nil { continue }
            if actions.contains(where: { $0.contains(start) }) { continue }
            if at(start) == "$" { found.append(start); continue }
            if at(end) == ":", at(end + 1) != ":" { continue }
            let from = text.substring(with: NSRange(location: max(0, start - 5), length: min(5, start))) == "self." ? start - 5 : start
            if at(start) != "_", viewArgument(from: from, to: end) { continue }
            found.append(start)
        }
        return found
    }

    /// The `///` lines directly above `index`, trimmed, skipping an
    /// attribute on a line of its own.
    static func docComment(above index: Int, in lines: [String]) -> [String] {
        var doc: [String] = []
        var i = index - 1
        while i >= 0 {
            let line = lines[i].trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("///") {
                doc.append(line)
            } else if !(line.hasPrefix("@") && doc.isEmpty) {
                break
            }
            i -= 1
        }
        return doc
    }

    /// A body after the name and type with no `=` before it, which isn't
    /// `didSet` or `willSet`: a getter.
    static func isComputed(_ rest: String, next: String) -> Bool {
        guard let brace = rest.firstIndex(of: "{"), !rest[..<brace].contains("=") else { return false }
        var after = rest[rest.index(after: brace)...].trimmingCharacters(in: .whitespaces)
        if after.isEmpty { after = next.trimmingCharacters(in: .whitespaces) }
        return !(after.hasPrefix("didSet") || after.hasPrefix("willSet"))
    }

    /// The declared type's bare name (`: ConfigStore`, `: UpdateStore?`), or
    /// the type an untyped property is made from (`= RebuildCoordinator(`).
    static func storeType(_ rest: String) -> String? {
        let pattern = #"^\s*(?::\s*([\w.]+)\??|=\s*([\w.]+)\()"#
        guard let m = try! NSRegularExpression(pattern: pattern)
            .firstMatch(in: rest, range: NSRange(rest.startIndex..., in: rest)) else { return nil }
        for i in [1, 2] {
            if let r = Range(m.range(at: i), in: rest) { return String(rest[r]) }
        }
        return nil
    }

    // MARK: What @Published alone doesn't cover

    /// Counts an object's `objectWillChange`.
    final class Changes {
        private(set) var count = 0
        private var subscription: AnyCancellable?

        init<Object: ObservableObject>(of object: Object) {
            subscription = object.objectWillChange.sink { [weak self] _ in self?.count += 1 }
        }
    }

    private static let sh = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))

    /// `AppStoreStore` sends on its account's changes, which views read
    /// through it, and not Search's or the older versions list's, which
    /// change with every keystroke and every row read: every view observing
    /// it (the window, the app pane) would redraw for each. A view that holds
    /// it only to hand it to Add App's sheet doesn't observe it at all
    /// (`@PassedThrough`, proven by the scan above).
    func test_the_app_store_store_sends_on_only_its_accounts_changes() {
        let appStore = AppStoreStore(runner: Self.sh, shell: NoAdminShell())
        let changes = Changes(of: appStore)
        let search = Changes(of: appStore.search)
        let olderVersions = Changes(of: appStore.olderVersions)

        appStore.search.update(term: "e")
        appStore.search.reset()
        XCTAssertGreaterThan(search.count, 0, "premise: Search published its changes")
        appStore.olderVersions.reset()
        XCTAssertGreaterThan(olderVersions.count, 0, "premise: the older versions list published its change")
        XCTAssertEqual(changes.count, 0, "a keystroke or a row redrew every view observing the App Store store")

        appStore.account.resetSignIn()
        XCTAssertEqual(changes.count, 1, "the account's change is the App Store store's")
    }

    /// A dump's lines arrive by the hundred. Published on `DeviceAppsStore`,
    /// each one redrew every view observing it: the device pane, the
    /// installed-apps list and each row's icon. They're published on the
    /// dump's own `DumpLog`, a batch at a time.
    func test_a_dumps_lines_publish_the_dump_log_a_batch_at_a_time_not_the_store() async {
        func dump(lines: Int) async -> (store: Int, log: Int, shown: Int) {
            let store = DeviceAppsStore(runner: Self.sh)
            let storeChanges = Changes(of: store), logChanges = Changes(of: store.dumpLog)
            let script = (0..<lines)
                .map { #"printf '{"type":"log","source":"bagbak","line":"file \#($0)"}\n'"# }
                .joined(separator: "; ")
            _ = await store.decryptFrontmost(device: "U", argsOverride: ["-c", script])
            return (storeChanges.count, logChanges.count, store.dumpLog.lines.count)
        }
        let one = await dump(lines: 1)
        let many = await dump(lines: 60)
        XCTAssertEqual(many.shown, 60, "premise: every line arrived")
        XCTAssertEqual(many.store, one.store, "the store published for the lines")
        XCTAssertLessThan(many.log, 10, "the log published line by line, not a batch at a time")
    }

    func test_the_dump_logs_lines_wait_then_are_published_together() async {
        let log = DumpLog(interval: .milliseconds(200))
        let changes = Changes(of: log)
        log.append(line: "one")
        log.append(line: "two")
        log.append(line: "three")
        XCTAssertEqual(log.lines, [], "published at once, not after the interval")
        XCTAssertEqual(changes.count, 0)
        await eventually({ log.lines.count == 3 }, "the waiting lines were published")
        XCTAssertEqual(log.lines, ["one", "two", "three"])
        XCTAssertEqual(changes.count, 1, "three lines, one publish")

        log.append(line: "four")
        log.flush()
        XCTAssertEqual(log.lines, ["one", "two", "three", "four"], "flush publishes what is waiting now")
        log.append(note: "a note")
        XCTAssertEqual(log.notes, ["a note"], "a note is published as it comes")
        log.append(line: "five")
        log.clear()
        try? await Task.sleep(for: .milliseconds(400))
        XCTAssertEqual(log.lines, [], "clear drops what was waiting, too")
        XCTAssertEqual(log.notes, [])
    }

    /// macOS 14 and later keep SwiftUI's announcement, as 2.2 posted it;
    /// AppKit's is macOS 13's.
    func test_macos_14_and_later_announce_through_swiftui() {
        XCTAssertGreaterThanOrEqual(
            ProcessInfo.processInfo.operatingSystemVersion.majorVersion, 14, "premise: this Mac runs macOS 14 or later"
        )
        XCTAssertEqual(VoiceOverAnnouncement.thisMac, .swiftUI)
    }

    /// An App evaluates its whole body for every change of a store it
    /// observes. `GraftApp` only hands its ConfigStore and UpdateStore on, to
    /// the window, the Help menu's `Task` and the delegate, so it holds them
    /// as plain `@State`: the scan above proves it never reads them.
    func test_the_app_holds_the_windows_stores_without_observing_them() throws {
        let app = try XCTUnwrap(Self.appSources().first { $0.path == "GraftApp.swift" }).source
        XCTAssertTrue(app.contains("    @State private var store: ConfigStore\n"))
        XCTAssertTrue(app.contains("    @State private var updates: UpdateStore\n"))
    }

    /// The properties views bind or read that a test can set directly:
    /// each change is published.
    func test_the_settable_state_views_read_is_published() {
        let config = ConfigStore(runner: Self.sh, cliArgs: ["-c", "true"], notify: { _ in })
        let settings = SettingsStore(runner: Self.sh)
        let account = AccountSetupStore(runner: Self.sh)
        let devices = DevicesStore(runner: Self.sh)
        let deviceApps = DeviceAppsStore(runner: Self.sh)
        let liveContainer = LiveContainerStore(runner: Self.sh)
        let builds = BuildsStore(runner: Self.sh)
        let repos = RepoStore(runner: Self.sh)
        let canister = CanisterStore(runner: Self.sh)
        let tweaks = TweaksStore(runner: Self.sh)
        let cases: [(String, AnyObject, () -> Void)] = [
            ("ConfigStore.lastError", config, { config.lastError = "x" }),
            ("SettingsStore.draft", settings, { settings.draft = SettingsDraft() }),
            ("SettingsStore.lastError", settings, { settings.lastError = "x" }),
            ("SettingsStore.portalForm", settings, { settings.portalForm = settings.portalForm }),
            ("AccountSetupStore.form", account, { account.form = account.form }),
            ("AccountSetupStore.error", account, { account.error = "x" }),
            ("DevicesStore.lastError", devices, { devices.lastError = "x" }),
            ("DeviceAppsStore.listErrors", deviceApps, { deviceApps.listErrors["UDID"] = "x" }),
            ("DeviceAppsStore.lastError", deviceApps, { deviceApps.lastError = "x" }),
            ("LiveContainerStore.loadErrors", liveContainer, { liveContainer.loadErrors["UDID"] = "x" }),
            ("BuildsStore.lastError", builds, { builds.lastError = "x" }),
            ("RepoStore.fetchResult", repos, { repos.fetchResult = "x" }),
            ("RepoStore.lastError", repos, { repos.lastError = "x" }),
            ("CanisterStore.lastError", canister, { canister.lastError = "x" }),
            ("TweaksStore.lastError", tweaks, { tweaks.lastError = "x" }),
        ]
        for (name, object, change) in cases {
            let changes = Self.changes(of: object)
            change()
            XCTAssertEqual(changes?.count, 1, "\(name) changed without publishing")
        }
    }

    private static func changes(of object: AnyObject) -> Changes? {
        switch object {
        case let o as ConfigStore: return Changes(of: o)
        case let o as SettingsStore: return Changes(of: o)
        case let o as AccountSetupStore: return Changes(of: o)
        case let o as DevicesStore: return Changes(of: o)
        case let o as DeviceAppsStore: return Changes(of: o)
        case let o as LiveContainerStore: return Changes(of: o)
        case let o as BuildsStore: return Changes(of: o)
        case let o as RepoStore: return Changes(of: o)
        case let o as CanisterStore: return Changes(of: o)
        case let o as TweaksStore: return Changes(of: o)
        default: return nil
        }
    }

    /// Something to change, for the hosted views below.
    final class Box: ObservableObject {
        @Published var value = 0
    }

    func test_an_optional_stores_relay_follows_the_store_it_is_given() {
        let relay = ObservedOptionalObject<Box>.Relay()
        let changes = Changes(of: relay)
        let first = Box(), second = Box()

        relay.follow(first)
        first.value = 1
        XCTAssertEqual(changes.count, 1, "the store's change is sent on")

        relay.follow(second)
        first.value = 2
        XCTAssertEqual(changes.count, 1, "the store it no longer has is not followed")
        second.value = 1
        XCTAssertEqual(changes.count, 2, "the new store is")

        relay.follow(nil)
        second.value = 2
        XCTAssertEqual(changes.count, 2, "nil follows nothing")
    }

    /// What a hosted view saw.
    final class Seen {
        var values: [Int?] = []
        var changes: [[Int]] = []
    }

    private struct OptionalHolder: View {
        @ObservedOptionalObject var box: Box?
        let seen: Seen

        var body: some View {
            seen.values.append(box?.value)
            return Text("\(box?.value ?? -1)")
        }
    }

    func test_a_view_redraws_when_its_optional_store_changes() {
        let box = Box(), seen = Seen()
        let host = hosted(OptionalHolder(box: box, seen: seen), width: 200)
        settle(host)
        XCTAssertEqual(seen.values.last, 0, "premise: the view drew the store's value")

        box.value = 7
        settle(host)
        XCTAssertEqual(seen.values.last, 7, "the view didn't redraw when its optional store changed")
    }

    private struct ChangeLogger: View {
        @ObservedObject var box: Box
        let seen: Seen

        var body: some View {
            Text("\(box.value)")
                .onChangeWithPrevious(of: box.value) { previous, current in
                    seen.changes.append([previous, current])
                }
        }
    }

    func test_on_change_with_previous_passes_the_value_before_each_change() {
        let box = Box(), seen = Seen()
        box.value = 3
        let host = hosted(ChangeLogger(box: box, seen: seen), width: 200)
        settle(host)
        XCTAssertEqual(seen.changes, [], "premise: appearing is not a change")

        box.value = 4
        settle(host)
        box.value = 9
        settle(host)
        XCTAssertEqual(seen.changes, [[3, 4], [4, 9]])
    }
}
