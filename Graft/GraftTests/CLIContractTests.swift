import XCTest
@testable import Graft

/// Checks this app's hardcoded CLI vocabulary against the *installed*
/// `graft`, by running its hidden `--describe` flag.
///
/// Every other test in this bundle that touches argument vectors either
/// compares Swift literals to Swift literals or swaps the real argv out for
/// `["-c", script]` against `/bin/sh`. Neither can catch the CLI and the GUI
/// disagreeing about a name — the failure mode that produced this codebase's
/// worst bugs (a dropped `--delivery`, an option parsed as a positional app
/// name, a renamed event silently swallowed by `try?`).
final class CLIContractTests: XCTestCase {
    private struct Contract: Decodable {
        let type: String
        let flags: [String]
        let events: [String]
        let appOptionFields: [String]
        /// Events the *CLI* declares need no GUI surface, with a reason
        /// recorded on its side. Optional because a `graft` older than
        /// this key exists and a missing field must skip rather than fail —
        /// the same treatment `loadContract` gives an unparseable line.
        let cliOnlyEvents: [String]?
        /// Every `name` the CLI's `stage` events can carry. Optional for the
        /// same reason as `cliOnlyEvents`: an older installed `graft`
        /// must skip, not fail.
        let stages: [String]?
        /// Every global setting `--set-settings` writes. Optional for the same
        /// reason as the two above: an older installed CLI must skip, not fail.
        let settingFields: [String]?

        enum CodingKeys: String, CodingKey {
            case type, flags, events, stages
            case appOptionFields = "app_option_fields"
            case settingFields = "setting_fields"
            case cliOnlyEvents = "cli_only_events"
        }
    }

    /// `PATH`, then the pipx default. A machine without the CLI installed
    /// skips rather than fails: this test is a cross-boundary check, not a
    /// reason for a clean checkout to go red.
    private static func locateCLI() -> URL? {
        let fm = FileManager.default
        let pathDirs = (ProcessInfo.processInfo.environment["PATH"] ?? "")
            .split(separator: ":").map(String.init)
        let home = fm.homeDirectoryForCurrentUser.path
        for dir in pathDirs + ["\(home)/.local/bin"] where !dir.isEmpty {
            let candidate = URL(fileURLWithPath: dir).appendingPathComponent("graft")
            if fm.isExecutableFile(atPath: candidate.path) { return candidate }
        }
        return nil
    }

    /// `--describe` exited non-zero. An error rather than an `XCTAssert` so it
    /// can be cached and re-thrown, failing every test that needed the
    /// contract instead of only whichever one ran first.
    private struct DescribeFailed: Error, LocalizedError {
        let path: String
        let status: Int32
        let stderr: String
        var errorDescription: String? {
            "`\(path) --describe` exited \(status): \(stderr)"
        }
    }

    /// The one `--describe` run this class needs, kept across its eight tests.
    ///
    /// `--describe` is a deterministic single line, and spawning the CLI for it
    /// eight times cost ~130–170 ms each. Stored as a `Result` rather than a
    /// `Contract?` so a *skip* is cached too and re-thrown per test — an
    /// XCTSkip swallowed here would turn "no CLI installed" into eight silent
    /// passes, which is the one outcome this whole file exists to prevent.
    private static var contract: Result<Contract, Error>?

    private func loadContract() throws -> Contract {
        if let contract = Self.contract { return try contract.get() }
        let result = Result { try Self.describe() }
        Self.contract = result
        return try result.get()
    }

    private static func describe() throws -> Contract {
        guard let executable = Self.locateCLI() else {
            throw XCTSkip("graft not found on PATH or in ~/.local/bin")
        }

        let proc = Process()
        proc.executableURL = executable
        proc.arguments = ["--describe"]
        // `--describe` must not need a config.yaml, but point HOME/XDG at a
        // scratch directory anyway so this test can never read — let alone
        // touch, or migrate from `…/tweak-build` — the developer's real
        // config, cache, state and data directories.
        let scratch = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-contract-\(UUID().uuidString)")
        var env = ProcessEnvironment.forCLI()
        env["HOME"] = scratch.path
        env["XDG_CONFIG_HOME"] = scratch.appendingPathComponent("config").path
        env["XDG_CACHE_HOME"] = scratch.appendingPathComponent("cache").path
        env["XDG_STATE_HOME"] = scratch.appendingPathComponent("state").path
        env["XDG_DATA_HOME"] = scratch.appendingPathComponent("data").path
        proc.environment = env

        let out = Pipe()
        let err = Pipe()
        proc.standardOutput = out
        proc.standardError = err
        try proc.run()
        let outData = out.fileHandleForReading.readDataToEndOfFile()
        let errData = err.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()

        let stderr = String(data: errData, encoding: .utf8) ?? ""
        guard proc.terminationStatus == 0 else {
            throw DescribeFailed(
                path: executable.path, status: proc.terminationStatus, stderr: stderr
            )
        }

        let lines = (String(data: outData, encoding: .utf8) ?? "")
            .split(separator: "\n", omittingEmptySubsequences: true)
        guard lines.count == 1 else {
            throw XCTSkip(
                "`--describe` produced \(lines.count) lines; expected exactly one. "
                + "Is the installed graft older than this contract?"
            )
        }
        return try JSONDecoder().decode(Contract.self, from: Data(lines[0].utf8))
    }

    func test_describe_reports_a_cli_contract_event() throws {
        let contract = try loadContract()
        XCTAssertEqual(contract.type, "cli-contract")
        XCTAssertFalse(contract.flags.isEmpty)
        XCTAssertFalse(contract.events.isEmpty)
        XCTAssertFalse(contract.appOptionFields.isEmpty)
    }

    func test_every_flag_the_app_sends_is_accepted_by_the_cli() throws {
        let declared = Set(try loadContract().flags)
        let missing = CLIContract.flagsUsed.subtracting(declared).sorted()
        XCTAssertTrue(
            missing.isEmpty,
            """
            The GUI sends \(missing.count) flag(s) the installed graft \
            does not accept: \(missing.joined(separator: ", ")). \
            Click aborts with a usage error on an unknown option, so every \
            command using one of these fails at runtime.
            """
        )
    }

    func test_every_event_the_app_decodes_is_still_emitted_by_the_cli() throws {
        let declared = Set(try loadContract().events)
        let missing = CLIContract.eventsHandled.subtracting(declared).sorted()
        XCTAssertTrue(
            missing.isEmpty,
            """
            BuildEvent.decode handles \(missing.count) event type(s) the CLI \
            no longer emits: \(missing.joined(separator: ", ")). \
            String.buildEvents() decodes with `try?`, so a renamed event is \
            dropped without a trace and the UI simply shows nothing.
            """
        )
    }

    /// The direction the contract was missing, and the one that shipped a bug.
    ///
    /// `eventsHandled ⊆ CLI` (above) catches the GUI decoding something the CLI
    /// stopped emitting. It cannot catch the reverse — the CLI emitting
    /// something the GUI ignores — because `BuildEvent.decode`'s `default:`
    /// turns an unrecognised type into `.unknown` and every `pick` closure in
    /// `Services/` then filters it out. Nothing fails, nothing logs, and the
    /// feature the event was added for simply does not exist on this side.
    ///
    /// That is precisely what happened to `device-error`: added to the CLI in
    /// the same round that fixed the identical defect for `encrypted-binaries`,
    /// routed nowhere, and the Devices tab kept saying "No devices connected"
    /// about a phone the CLI had just reported by UDID.
    ///
    /// So: every event the CLI declares must be accounted for on this side —
    /// decoded, or named in `cliOnlyEvents` with a reason. A new CLI event
    /// fails this test until someone decides which it is.
    func test_every_event_the_cli_emits_is_routed_or_declared_cli_only() throws {
        let declared = Set(try loadContract().events)
        let accounted = CLIContract.eventsHandled.union(CLIContract.cliOnlyEvents)

        let unrouted = declared.subtracting(accounted).sorted()
        XCTAssertTrue(
            unrouted.isEmpty,
            """
            The CLI emits \(unrouted.count) event type(s) this app neither \
            decodes nor excuses: \(unrouted.joined(separator: ", ")). \
            BuildEvent.decode turns an unhandled type into `.unknown`, which \
            every store's `pick` then drops — so the feature it was added for \
            is invisible here and nothing reports that. Add a `decode` case \
            and list it in CLIContract.eventsHandled, or add it to \
            CLIContract.cliOnlyEvents with a comment saying why the GUI needs \
            no surface for it.
            """
        )

        let phantom = accounted.subtracting(declared).sorted()
        XCTAssertTrue(
            phantom.isEmpty,
            """
            This app accounts for \(phantom.count) event type(s) the installed \
            graft does not emit: \(phantom.joined(separator: ", ")). \
            Together with the check above, the two sets are exactly the CLI's \
            EVENT_TYPES — an equality, so neither half can drift unnoticed.
            """
        )
    }

    /// "This event needs no GUI surface" is a joint claim, so both sides
    /// declare it and the two declarations must be the same list.
    ///
    /// The CLI's Python suite already compares `CLI_ONLY_EVENTS` against the
    /// `cliOnlyEvents` literal it parses out of `CLIContract.swift`. That is a
    /// check between two *source trees*. This is the one only Swift can run:
    /// against the `graft` actually on PATH, which is the binary the GUI
    /// will spawn and need not be the checkout sitting next to it. A developer
    /// with an older pipx install gets the divergence reported here instead of
    /// as an empty Devices tab.
    func test_both_sides_declare_the_same_cli_only_events() throws {
        guard let declared = try loadContract().cliOnlyEvents else {
            throw XCTSkip("installed graft predates `cli_only_events` in --describe")
        }
        XCTAssertEqual(
            Set(declared), CLIContract.cliOnlyEvents,
            """
            The two CLI-only declarations disagree. Only the CLI excuses \
            \(Set(declared).subtracting(CLIContract.cliOnlyEvents).sorted()); \
            only this app excuses \
            \(CLIContract.cliOnlyEvents.subtracting(declared).sorted()). \
            One side is excusing an event the other is relying on.
            """
        )
    }

    /// The partition again, taken entirely off the wire.
    ///
    /// `test_every_event_the_cli_emits_is_routed_or_declared_cli_only` above
    /// subtracts *this app's* `cliOnlyEvents`, so it would pass if the app
    /// excused an event the CLI never agreed was CLI-only. This derives the
    /// must-decode set from `--describe` alone — `events - cli_only_events` —
    /// and asks whether the GUI decodes all of it.
    func test_every_event_the_cli_does_not_excuse_is_decoded() throws {
        let contract = try loadContract()
        guard let cliOnly = contract.cliOnlyEvents else {
            throw XCTSkip("installed graft predates `cli_only_events` in --describe")
        }
        let mustDecode = Set(contract.events).subtracting(cliOnly)
        XCTAssertFalse(mustDecode.isEmpty, "premise: the CLI emits something worth rendering")

        let undecoded = mustDecode.subtracting(CLIContract.eventsHandled).sorted()
        XCTAssertTrue(
            undecoded.isEmpty,
            """
            The installed graft emits \(undecoded.count) event type(s) it \
            expects a GUI to render, and BuildEvent.decode has no case for \
            them: \(undecoded.joined(separator: ", ")). Decoding is `try?`, so \
            each one is dropped without a trace.
            """
        )
    }

    /// An event is decoded or it is excused, never both. Overlap would let a
    /// type stay in `cliOnlyEvents` — "no GUI surface needed" — long after a
    /// panel started depending on it, and the equality above would still pass.
    /// No CLI needed.
    func test_an_event_is_either_handled_or_cli_only_but_not_both() {
        let both = CLIContract.eventsHandled
            .intersection(CLIContract.cliOnlyEvents).sorted()
        XCTAssertTrue(
            both.isEmpty,
            "\(both.joined(separator: ", ")) is declared both handled and CLI-only"
        )
    }

    /// The last unguarded name in the contract, and the same defect shape as
    /// `device-error`: a value one side depends on that the other can change
    /// without anything failing.
    ///
    /// `CLIContract.Stage.decryptionCheck` is `"verify"`, and
    /// `ConfigStore.note(_:forBuildOf:)` treats that stage's `status: "end"` as
    /// the *only* signal the cryptid-0 check reached a verdict — the CLI emits
    /// `encrypted-binaries` when it finds something and nothing when it
    /// doesn't, so a clean IPA is this stage ending without one. Rename the
    /// stage on the CLI side and every test on both sides stayed green while
    /// the `where name == …` in `note` simply never matched: a standing "N
    /// binaries are still encrypted — will crash on launch" card over an IPA
    /// that has been fine for weeks, and no way for the user to clear it.
    ///
    /// A subset, not the equality `eventsHandled` gets. The GUI renders any
    /// stage name it receives verbatim, so a stage the CLI adds needs nothing
    /// here; only the names this app *branches on* can break. See
    /// `CLIContract.Stage` for the full argument.
    func test_every_stage_the_app_depends_on_is_still_emitted_by_the_cli() throws {
        guard let declared = try loadContract().stages else {
            throw XCTSkip("installed graft predates `stages` in --describe")
        }
        // Vacuity guard, not a redundant restatement of the check below: an
        // empty `dependedOn` makes the subtraction empty and this test pass
        // while proving nothing. An empty `declared` needs no guard — it makes
        // the check fail, which is the correct outcome.
        XCTAssertFalse(
            CLIContract.Stage.dependedOn.isEmpty,
            "premise: this app depends on at least one stage name by meaning"
        )

        let missing = CLIContract.Stage.dependedOn.subtracting(declared).sorted()
        XCTAssertTrue(
            missing.isEmpty,
            """
            This app branches on \(missing.count) stage name(s) the installed \
            graft never emits: \(missing.joined(separator: ", ")). \
            A `stage` event with an unexpected name still decodes and still \
            renders in the transcript, so nothing fails and nothing logs — the \
            `where name == …` in ConfigStore.note just stops matching, and the \
            state it gates (the encrypted-binaries card's only all-clear) is \
            never reached again.
            """
        )
    }

    /// The setting keys the pane reconciles against `changed`.
    ///
    /// This replaces an assertion that compared `SettingField.allCases` to the
    /// same five strings written out again in the same Swift file — an input
    /// that behaves identically whether or not the Python side agrees. Renaming
    /// `signing_team_id` in `SETTING_PATHS` left every test green while every
    /// save reported a phantom failure, because a key the CLI never names in
    /// `changed` is exactly what `updateSettings` throws on.
    func test_every_setting_the_app_writes_is_declared_by_the_cli() throws {
        guard let declared = try loadContract().settingFields else {
            throw XCTSkip("installed graft predates `setting_fields` in --describe")
        }
        let sent = Set(CLIContract.SettingField.allCases.map(\.rawValue))
        let unknown = sent.subtracting(declared).sorted()
        XCTAssertTrue(
            unknown.isEmpty,
            """
            The app writes \(unknown.count) setting(s) the CLI does not declare: \
            \(unknown.joined(separator: ", ")). Every save naming one reports a \
            phantom failure, because it can never appear in `changed`.
            """
        )
    }

    func test_every_app_option_field_the_app_requests_is_reportable_by_the_cli() throws {
        let declared = Set(try loadContract().appOptionFields)
        let missing = CLIContract.appOptionFieldsSent.subtracting(declared).sorted()
        XCTAssertTrue(
            missing.isEmpty,
            """
            ConfigStore.updateAppOptions requests \(missing.count) field(s) the \
            CLI never lists in an app-updated `changed` array: \
            \(missing.joined(separator: ", ")). \
            The reconciliation there throws "graft didn't apply ..." for \
            each one — reporting failure for a write that actually succeeded.
            """
        )
    }

    // MARK: - The local half: does the app only send what it declares?

    /// Locating and reading the app's own sources, shared with the icon scans.
    ///
    /// These three were written here first, copied verbatim into `SymbolsTests`
    /// when the icon scans needed them, and lifted into `SourceScan` when
    /// `MarksTests` was about to be a third copy. Two copies of the same three
    /// helpers is one copy that stops being fixed: the rule in `codeLines(of:)`
    /// is the one with judgement in it — it drops lines whose first non-space
    /// characters are `//` and nothing else, so a flag or a `systemName:`
    /// inside a `/* … */` would still be reported (no app source uses block
    /// comments today, which is why that has never mattered) — and a change to
    /// that rule on one side would silently not reach the other. So this suite
    /// reads through the same helpers rather than its own.
    ///
    /// Nothing about them is icon-specific, which is the test for whether the
    /// shared home was the right one: `appSources()` is the app-source
    /// directory, `swiftSources(under:)` is a directory walk, `codeLines(of:)`
    /// is "the lines a scan should look at". A caller wanting different
    /// judgement about any of the three should take a parameter, not a copy.
    private func sourceRoot() throws -> URL { try SourceScan.appSources() }

    private func codeLines(of file: URL) throws -> [(number: Int, text: String)] {
        try SourceScan.codeLines(of: file)
    }

    private func swiftSources(under dir: URL) throws -> [URL] {
        try SourceScan.swiftSources(under: dir)
    }

    private static let flagLiteral = try! NSRegularExpression(pattern: #""(--[a-z][a-z0-9-]*)""#)

    /// Any double-quoted literal. Stage names have no distinguishing prefix
    /// the way flags have `--`, so the scan below narrows by *where* it looks
    /// instead: the body of one enum, `static let` lines only.
    private static let stringLiteral = try! NSRegularExpression(pattern: #""([^"\\]*)""#)

    private func matches(_ regex: NSRegularExpression, in line: String) -> [String] {
        let ns = line as NSString
        return regex.matches(in: line, range: NSRange(location: 0, length: ns.length))
            .map { ns.substring(with: $0.range(at: 1)) }
    }

    private func flagLiterals(in line: String) -> [String] {
        matches(Self.flagLiteral, in: line)
    }

    /// The lines inside the `{ … }` opened by the first line containing
    /// `header`, comments already dropped.
    ///
    /// Brace-counted rather than regex-matched: the blocks this reads hold doc
    /// comments full of prose, a `Set<String>` whose angle brackets a lazy
    /// pattern would have to care about, and (in `decode`) forty case bodies —
    /// and getting the extent wrong silently narrows what the scan sees, which
    /// is how a source scan passes by reading nothing.
    private func bracedBody(_ header: String, in file: URL) throws -> [(number: Int, text: String)] {
        let lines = try codeLines(of: file)
        guard let start = lines.firstIndex(where: { $0.text.contains(header) }) else {
            XCTFail("no `\(header)` in \(file.path); this scan is reading nothing")
            return []
        }
        var depth = 0
        var body: [(number: Int, text: String)] = []
        for line in lines[start...] {
            // The header line itself is skipped: depth is still 0 when it is
            // seen, and only rises after it is counted.
            if depth > 0 { body.append(line) }
            depth += line.text.filter { $0 == "{" }.count
            depth -= line.text.filter { $0 == "}" }.count
            if depth <= 0 { break }
        }
        XCTAssertFalse(body.isEmpty, "parsed an empty `\(header)` body")
        return body
    }

    /// The lines inside `enum <name> { … }`.
    private func enumBody(_ name: String, in file: URL) throws -> [(number: Int, text: String)] {
        try bracedBody("enum \(name) {", in: file)
    }

    /// The half the contract was missing: `flagsUsed ⊆ CLI` was proven, but
    /// nothing proved `callers ⊆ flagsUsed`, so a store rewritten to send
    /// `--repo-pkgs` passed every test and failed at runtime.
    ///
    /// The app now builds its argv out of `CLIContract.Flag`, and this is what
    /// keeps it there: a bare flag literal anywhere in `Services/` or `Views/`
    /// fails, because a literal is exactly how a flag reaches the CLI without
    /// passing through the contract.
    ///
    /// `Views/` is in scope because the Services layer is not the only caller:
    /// the live build stream, the re-install sheet and the build-log sheet all
    /// hold a `CLIRunner` and assemble their own arguments, and those four
    /// call sites kept seven literals outside the contract for as long as this
    /// scan stopped at `Services/`.
    func test_no_flag_literal_escapes_the_contract() throws {
        let root = try sourceRoot()
        var offenders: [String] = []
        for dirName in ["Services", "Views"] {
            let dir = root.appendingPathComponent(dirName)
            let files = try swiftSources(under: dir)
            XCTAssertFalse(files.isEmpty, "no Swift sources under \(dir.path)")
            for file in files {
                // Path from the app-source root, not the basename: the offender
                // sits in one of eight per-section folders, and
                // "BuildLogView.swift" alone doesn't say which one to open.
                let shown = file.path.replacingOccurrences(
                    of: root.path + "/", with: ""
                )
                for line in try codeLines(of: file) {
                    for flag in flagLiterals(in: line.text) {
                        offenders.append("\(shown):\(line.number) \(flag)")
                    }
                }
            }
        }
        XCTAssertTrue(
            offenders.isEmpty,
            """
            \(offenders.count) flag literal(s) in Services/ or Views/ bypass \
            CLIContract: \(offenders.joined(separator: ", ")). \
            Declare the spelling in CLIContract.Flag and reference it, so the \
            flag the app sends is the flag the contract test checks against \
            the CLI.
            """
        )
    }

    /// `flagsUsed` is `Flag.all`, and `Flag.all` is hand-written because Swift
    /// can't enumerate a caseless enum's static members. This closes that gap
    /// from the other side: every `static let x = "--y"` declared in
    /// CLIContract.swift must be in `flagsUsed`, so a constant added for a new
    /// store call can't be left out of the set the CLI is checked against.
    func test_every_flag_constant_is_declared_used() throws {
        let file = try sourceRoot()
            .appendingPathComponent("Models/CLIContract.swift")
        var declared: [String] = []
        for line in try codeLines(of: file) where line.text.contains("static let") {
            declared.append(contentsOf: flagLiterals(in: line.text))
        }
        XCTAssertFalse(declared.isEmpty, "found no flag constants in \(file.path)")

        let missing = Set(declared).subtracting(CLIContract.flagsUsed).sorted()
        XCTAssertTrue(
            missing.isEmpty,
            """
            CLIContract.Flag declares \(missing.count) flag(s) missing from \
            flagsUsed: \(missing.joined(separator: ", ")). \
            Add them to Flag.all — otherwise the CLI is never asked whether it \
            still accepts them.
            """
        )
    }

    /// `Stage.dependedOn` is hand-written for the same reason `Flag.all` is,
    /// and rots the same way. Every `static let x = "y"` in `enum Stage` must
    /// be in it, so a second stage dependency added next to `decryptionCheck`
    /// cannot be left out of the set the CLI is checked against — which would
    /// leave it in precisely the unguarded state this round exists to fix,
    /// sitting one line below a constant that *is* guarded.
    func test_every_stage_constant_is_declared_used() throws {
        let file = try sourceRoot()
            .appendingPathComponent("Models/CLIContract.swift")
        var declared: [String] = []
        for line in try enumBody("Stage", in: file) where line.text.contains("static let") {
            declared.append(contentsOf: matches(Self.stringLiteral, in: line.text))
        }
        XCTAssertFalse(declared.isEmpty, "found no stage constants in \(file.path)")

        let missing = Set(declared).subtracting(CLIContract.Stage.dependedOn).sorted()
        XCTAssertTrue(
            missing.isEmpty,
            """
            CLIContract.Stage declares \(missing.count) stage name(s) missing \
            from dependedOn: \(missing.joined(separator: ", ")). \
            Add them — otherwise the CLI is never asked whether it still emits \
            them, and the constant is decorative.
            """
        )
    }

    /// The header of the switch every event on the wire passes through. Held
    /// as a constant because it is the one string tying this scan to the
    /// decoder: if `decode` is restructured, `bracedBody` fails naming it
    /// rather than reading nothing.
    private static let decodeSwitchHeader = "switch head.type {"

    /// The scan the other four event assertions were all missing.
    ///
    /// `eventsHandled` is what every one of them resolves "the GUI handles
    /// this" to — the `--describe` equality, the not-both check, the Python
    /// partition. Not one of them ever compared it to `BuildEvent.decode`,
    /// the switch it claims to describe and is hand-copied from. The two had
    /// 36 entries each by luck.
    ///
    /// Which leaves the original defect intact one layer down: register a new
    /// event in `EVENT_TYPES` and in `eventsHandled` — both of which the other
    /// tests demand — then typo or forget the `decode` case, and all four stay
    /// green while the event falls through to `.unknown`, gets filtered out of
    /// every store, and its feature silently does not exist. That is
    /// `device-error` exactly, walking past the assertions written to stop it.
    ///
    /// An equality, not a subset, in both directions for the same reason the
    /// `--describe` check is: a `case` missing from `eventsHandled` is an event
    /// never checked against the CLI, and an `eventsHandled` entry with no
    /// `case` is the bug above.
    func test_every_event_decode_has_a_case_for_is_declared_handled() throws {
        let file = try sourceRoot().appendingPathComponent("Models/BuildEvent.swift")
        var decoded: Set<String> = []
        for line in try bracedBody(Self.decodeSwitchHeader, in: file) {
            // `case "…":` only. The case *bodies* are full of string literals
            // — JSON key names, fallback messages — and none of them is an
            // event type.
            let trimmed = line.text.trimmingCharacters(in: .whitespaces)
            guard trimmed.hasPrefix("case \"") else { continue }
            decoded.formUnion(matches(Self.stringLiteral, in: trimmed))
        }
        XCTAssertFalse(
            decoded.isEmpty,
            "found no `case \"…\":` in \(file.path)'s decoder; this scan is reading nothing"
        )

        let undeclared = decoded.subtracting(CLIContract.eventsHandled).sorted()
        let unimplemented = CLIContract.eventsHandled.subtracting(decoded).sorted()
        XCTAssertEqual(
            decoded, CLIContract.eventsHandled,
            """
            BuildEvent.decode and CLIContract.eventsHandled disagree. \
            decode has a case for \(undeclared.count) type(s) eventsHandled \
            omits: \(undeclared.joined(separator: ", ")) — those are never \
            checked against the CLI's --describe, so the CLI can drop them \
            silently. eventsHandled claims \(unimplemented.count) type(s) \
            decode has no case for: \(unimplemented.joined(separator: ", ")) — \
            those reach `default:` as `.unknown`, every store's filter drops \
            them, and the four assertions above stay green while the feature \
            does not exist. eventsHandled is hand-copied from that switch; this \
            is the only thing that checks the copy.
            """
        )
    }

    /// Guards the local half of the contract: the sets above must stay in step
    /// with the code they claim to describe. This one needs no CLI.
    func test_build_options_flags_are_declared_in_the_contract() {
        var options = BuildOptions()
        options.dryRun = true
        options.skipInstall = true
        options.skipDecryptCheck = true
        options.deviceUDID = "UDID1"
        let rendered = options.cliArgs.filter { $0.hasPrefix("-") }
        for flag in rendered {
            XCTAssertTrue(
                CLIContract.flagsUsed.contains(flag),
                "BuildOptions renders \(flag), which is missing from CLIContract.flagsUsed"
            )
        }
    }

    /// `CLIContract.Exit.tweakInUse` (16) has `TweaksStoreTests` to exercise
    /// it end-to-end, against a fake `/bin/sh` double standing in for the
    /// CLI. `--delete-staged` has no store to do that with yet — that lands
    /// with the Cache pane's delete action — so this pins the bare literal
    /// against the Python side's own pin
    /// (`test_staged_ipa_in_use_error_exit_code_is_17`) in the meantime, the
    /// same number `StagedIPAInUseError.exit_code` is. A weaker check than
    /// `TweaksStoreTests`' — it cannot catch the two sides disagreeing about
    /// what the CLI actually exits with, only about what each side claims —
    /// but it is what stops this constant from being edited on one side
    /// alone and having nothing notice.
    func test_staged_ipa_in_use_exit_code_is_17() {
        XCTAssertEqual(CLIContract.Exit.stagedIPAInUse, 17)
    }

    /// The installed CLI, run once in an empty scratch home — no config, cache,
    /// state or data directory — with `GRAFT_NO_KEYCHAIN` set, the same
    /// isolation `describe()` gives `--describe`. usbmux is pointed at a socket
    /// that does not exist, so a `--check` finds no phone rather than asking a
    /// real one.
    private static func runInEmptyHome(
        _ arguments: [String], prepare: (URL) throws -> Void = { _ in }
    ) throws -> (status: Int32, stdout: String, stderr: String, scratch: URL) {
        guard let executable = Self.locateCLI() else {
            throw XCTSkip("graft not found on PATH or in ~/.local/bin")
        }
        let scratch = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-contract-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)
        try prepare(scratch)
        var env = ProcessEnvironment.forCLI()
        env["HOME"] = scratch.path
        env["XDG_CONFIG_HOME"] = scratch.appendingPathComponent("config").path
        env["XDG_CACHE_HOME"] = scratch.appendingPathComponent("cache").path
        env["XDG_STATE_HOME"] = scratch.appendingPathComponent("state").path
        env["XDG_DATA_HOME"] = scratch.appendingPathComponent("data").path
        env["GRAFT_NO_KEYCHAIN"] = "1"
        env["USBMUXD_SOCKET_ADDRESS"] = scratch.appendingPathComponent("no-usbmuxd.sock").path

        let proc = Process()
        proc.executableURL = executable
        proc.arguments = arguments
        proc.environment = env
        let out = Pipe()
        let err = Pipe()
        proc.standardOutput = out
        proc.standardError = err
        try proc.run()
        let outData = out.fileHandleForReading.readDataToEndOfFile()
        let errData = err.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        return (
            proc.terminationStatus,
            String(data: outData, encoding: .utf8) ?? "",
            String(data: errData, encoding: .utf8) ?? "",
            scratch
        )
    }

    /// Settings → Paths shows what `--settings` reports, so the installed CLI
    /// has to report it: a config with its own `tweaks_dir`, under a moved
    /// `XDG_CONFIG_HOME`, must come back as those directories and not as the
    /// `~/.config/graft` the pane used to hardcode.
    func test_the_settings_event_reports_the_resolved_paths() throws {
        let run = try Self.runInEmptyHome([CLIContract.Flag.json, CLIContract.Flag.settings]) { scratch in
            let dir = scratch.appendingPathComponent("config/graft")
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            try """
            signing:
              cert: \(dir.path)/AppleDevelopment.p12
              cert_password_env: GRAFT_P12_PASS
              profile: \(dir.path)/wildcard.mobileprovision
              team_id: ABCDE12345
            tweaks_dir: \(scratch.path)/debs-here
            apps: {}
            """.write(to: dir.appendingPathComponent("config.yaml"), atomically: true, encoding: .utf8)
        }
        defer { try? FileManager.default.removeItem(at: run.scratch) }
        XCTAssertEqual(run.status, 0, run.stderr)

        let record = run.stdout.buildEvents().lazy.compactMap { event -> SettingsRecord? in
            if case let .settings(record) = event { return record }
            return nil
        }.first
        guard let paths = record?.paths else {
            return XCTFail("`--settings` reported no paths: \(run.stdout)")
        }
        func resolved(_ path: String) -> String {
            URL(fileURLWithPath: path).resolvingSymlinksInPath().path
        }
        let scratch = run.scratch
        XCTAssertEqual(resolved(paths.configDir), resolved(scratch.appendingPathComponent("config/graft").path))
        XCTAssertEqual(resolved(paths.tweaksDir), resolved(scratch.appendingPathComponent("debs-here").path))
        XCTAssertEqual(resolved(paths.builtDir), resolved(scratch.appendingPathComponent("cache/graft/built").path))
        XCTAssertEqual(resolved(paths.cacheDir), resolved(scratch.appendingPathComponent("cache/graft/ipas").path))
        XCTAssertEqual(resolved(paths.stateDir), resolved(scratch.appendingPathComponent("state/graft").path))
    }

    /// The setup screen's second step is chosen by matching the CLI's own
    /// sentence for a missing config.yaml (`CLIContract.Message.configNotFound`)
    /// under `ConfigError`'s status — there is no event stream yet to carry
    /// anything structured. So the installed CLI is asked, in an empty home,
    /// exactly what the app asks first, and its answer must read as that step.
    /// Reword the sentence or renumber the status on the CLI side and this
    /// fails, instead of every new user getting a modal alert again.
    func test_a_missing_config_is_reported_the_way_the_setup_screen_expects() throws {
        let run = try Self.runInEmptyHome([CLIContract.Flag.json, CLIContract.Flag.list])
        defer { try? FileManager.default.removeItem(at: run.scratch) }
        XCTAssertEqual(run.status, CLIContract.Exit.configError, run.stderr)

        let failure = CLIError.exited(doing: "Load apps", code: run.status, stderr: run.stderr)
        let expected = run.scratch.appendingPathComponent("config/graft/config.yaml").path
        guard case let .config(path)? = SetupNeed(error: failure) else {
            return XCTFail("not read as the config step: \(run.stderr)")
        }
        // Compared after resolving symlinks: the scratch directory lives under
        // /var, which the CLI may report as /private/var.
        XCTAssertEqual(
            path.map { URL(fileURLWithPath: $0).resolvingSymlinksInPath().path },
            URL(fileURLWithPath: expected).resolvingSymlinksInPath().path
        )
    }

    // MARK: Free Apple ID

    /// A config for a free Apple ID: no `signing:` section. `apps` is the YAML
    /// under `apps:`, `{}` for none.
    private static func writeFreeConfig(_ scratch: URL, apps: String) throws {
        let dir = scratch.appendingPathComponent("config/graft")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try """
        cache_dir: \(scratch.path)/cache/graft/ipas
        tweaks_dir: \(dir.path)/tweaks
        apps: \(apps)
        """.write(to: dir.appendingPathComponent("config.yaml"), atomically: true, encoding: .utf8)
    }

    private static let oneLiveContainerApp = """

      example:
        bundle_id: com.example.app
        delivery: livecontainer
    """

    /// What `ConfigStore` runs at launch, on a fresh free install: no apps, so
    /// only the `account` event can say which account this is.
    func test_list_reports_a_free_account_with_no_apps() throws {
        let run = try Self.runInEmptyHome([CLIContract.Flag.json, CLIContract.Flag.list]) {
            try Self.writeFreeConfig($0, apps: "{}")
        }
        defer { try? FileManager.default.removeItem(at: run.scratch) }
        XCTAssertEqual(run.status, 0, run.stderr)
        let events = run.stdout.buildEvents()
        XCTAssertEqual(events.count, 1, run.stdout)
        guard case .account(.free)? = events.first else {
            return XCTFail("not a free account event: \(run.stdout)")
        }
    }

    func test_list_reports_a_free_account_first_and_on_each_app() throws {
        let run = try Self.runInEmptyHome([CLIContract.Flag.json, CLIContract.Flag.list]) {
            try Self.writeFreeConfig($0, apps: Self.oneLiveContainerApp)
        }
        defer { try? FileManager.default.removeItem(at: run.scratch) }
        XCTAssertEqual(run.status, 0, run.stderr)
        let events = run.stdout.buildEvents()
        guard case .account(.free)? = events.first else {
            return XCTFail("the account is not the first event: \(run.stdout)")
        }
        let apps = events.compactMap { event -> AppRecord? in
            if case let .app(record) = event { return record }
            return nil
        }
        XCTAssertEqual(apps.map(\.name), ["example"])
        XCTAssertEqual(apps.map(\.account), [.free])
        XCTAssertEqual(apps.map(\.delivery), [.livecontainer])
    }

    /// `--check` on the same config: `preflight-end` says free, and the
    /// account row is the one marked as information.
    func test_check_reports_a_free_account_and_its_row_is_information() throws {
        let run = try Self.runInEmptyHome([CLIContract.Flag.json, CLIContract.Flag.check]) {
            try Self.writeFreeConfig($0, apps: Self.oneLiveContainerApp)
        }
        defer { try? FileManager.default.removeItem(at: run.scratch) }
        // 0 or 3: whether this Mac has every tool is not the question.
        XCTAssertTrue([0, 3].contains(run.status), "exit \(run.status): \(run.stderr)")
        let events = run.stdout.buildEvents()
        let ends = events.compactMap { event -> AccountMode?? in
            if case let .preflightEnd(_, _, account) = event { return account }
            return nil
        }
        XCTAssertEqual(ends, [.free], run.stdout)
        let rows = events.compactMap { event -> PreflightCheck? in
            if case let .preflightCheck(check) = event { return check }
            return nil
        }
        XCTAssertEqual(rows.filter(\.isInfo).map(\.name), ["account"], run.stdout)
        XCTAssertTrue(rows.filter(\.isInfo).allSatisfy(\.ok))
    }

    /// A paid-only verb that reaches the CLI anyway: the refusal, read the way
    /// every banner reads a failure, is the CLI's own sentence.
    func test_a_sideload_add_on_a_free_account_is_refused_in_words_a_banner_shows() throws {
        let run = try Self.runInEmptyHome([
            CLIContract.Flag.json, CLIContract.Flag.addAppManual,
            CLIContract.Flag.name, "other", CLIContract.Flag.bundleId, "com.example.other",
            CLIContract.Flag.delivery, DeliveryMode.sideload.rawValue,
        ]) {
            try Self.writeFreeConfig($0, apps: "{}")
        }
        defer { try? FileManager.default.removeItem(at: run.scratch) }
        XCTAssertEqual(run.status, CLIContract.Exit.configError, run.stderr)
        let message = CLIError.exited(doing: "Add app", code: run.status, stderr: run.stderr)
            .localizedDescription
        XCTAssertTrue(
            message.hasPrefix("--delivery sideload needs a paid Apple Developer Program membership"),
            message
        )
    }
}
