import Foundation
import XCTest
@testable import Graft

/// The per-device delivery states, and the four sentences a row can say about
/// them.
///
/// The decision under test is the one the whole per-device pane rests on:
/// "delivered" is a question about a *phone*, and this store is the only place
/// in the app that asks it that way. `ConfigStore.deliveryStates` asks the
/// unscoped version and two live views depend on that meaning, so the two
/// answers must be able to disagree — and here they are made to.
@MainActor
final class DeviceDeliveryStoreTests: XCTestCase {

    private static let phoneA = "00008030-0000000000000003"
    private static let phoneB = "00008030-0000000000000001"

    private func sh() -> CLIRunner {
        CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
    }

    private static func stateJSON(
        app: String, kind: String, summary: String, reasons: [String] = []
    ) -> String {
        let joined = reasons.map { "\"\($0)\"" }.joined(separator: ",")
        return #"{"type":"delivery-state","app":"\#(app)","kind":"\#(kind)","summary":"\#(summary)","reasons":[\#(joined)],"pushed_at":null}\n"#
    }

    private func load(
        _ store: DeviceDeliveryStore, udid: String, printing lines: String
    ) async {
        await store.load(udid: udid, argsOverride: ["-c", "printf '\(lines)'"])
    }

    // MARK: - What reaches the CLI

    /// The flag that makes this store worth existing.
    ///
    /// `--delivery-state` without `--device` is not an error — it is a
    /// *successful* run answering the unscoped question, the same one `--list`
    /// already answers. Dropped, this pane would go on rendering, go on showing
    /// summaries, and quietly stop being about the phone whose name is at the
    /// top of it. Nothing else in the app could tell.
    func test_the_argv_scopes_the_query_to_one_phone() {
        XCTAssertEqual(
            DeviceDeliveryStore.stateArgs(udid: Self.phoneA),
            ["--json", "--delivery-state", "--device", Self.phoneA]
        )
    }

    // MARK: - Two phones, two answers

    /// The property the single-state view could not express: the same app,
    /// delivered to one phone and never to the other.
    func test_the_same_app_has_a_different_answer_on_each_phone() async {
        let store = DeviceDeliveryStore(runner: sh())
        await load(
            store, udid: Self.phoneA,
            printing: Self.stateJSON(
                app: "example", kind: "current", summary: "Matches the last delivery."
            )
        )
        await load(
            store, udid: Self.phoneB,
            printing: Self.stateJSON(
                app: "example", kind: "never", summary: "Never delivered to a device."
            )
        )

        XCTAssertEqual(
            DeviceAppsSection.state(of: "example", in: store.readout(for: Self.phoneA))?.kind,
            .current
        )
        XCTAssertEqual(
            DeviceAppsSection.state(of: "example", in: store.readout(for: Self.phoneB))?.kind,
            .never,
            "loading a second phone must not overwrite the first phone's answer — the "
            + "readouts are keyed by udid precisely so both can stand"
        )
    }

    /// A phone nobody has asked about yet is `.pending`, not "no states".
    /// Everything downstream branches on that difference: before the first load
    /// every app is missing from the map, and reading missing as "the CLI said
    /// nothing" would have the pane make claims it hasn't checked.
    func test_an_unloaded_phone_reads_as_pending() {
        let store = DeviceDeliveryStore(runner: sh())
        XCTAssertEqual(store.readout(for: Self.phoneA), .pending)
    }

    /// stdout is the one input this process doesn't control, and
    /// `Dictionary(uniqueKeysWith:)` traps on a repeated key. A duplicated app
    /// should cost a row's status line, not the GUI.
    func test_a_repeated_app_keeps_the_last_line_rather_than_trapping() async {
        let store = DeviceDeliveryStore(runner: sh())
        await load(
            store, udid: Self.phoneA,
            printing: Self.stateJSON(
                app: "example", kind: "never", summary: "Never delivered to a device."
            ) + Self.stateJSON(
                app: "example", kind: "stale", summary: "Changed since the last delivery."
            )
        )
        XCTAssertEqual(
            DeviceAppsSection.state(of: "example", in: store.readout(for: Self.phoneA))?.kind,
            .stale
        )
    }

    // MARK: - When the read fails

    /// A failure with nothing to fall back on is recorded, not swallowed.
    ///
    /// The stores that swallow — `ArtworkStore`, `DeviceInfoStore` — degrade to
    /// a letter tile and a dash, neither of which claims anything. A row with
    /// no delivery record has to *say* something, and the only honest options
    /// are "we haven't asked" and "we couldn't". So the failure has to survive
    /// to the view.
    func test_a_failed_read_with_nothing_to_keep_is_recorded() async {
        let store = DeviceDeliveryStore(runner: sh())
        await store.load(
            udid: Self.phoneA,
            argsOverride: ["-c", "printf 'database is locked\\n' >&2; exit 1"]
        )
        guard case let .failed(message) = store.readout(for: Self.phoneA) else {
            return XCTFail("a non-zero exit with no prior states should record .failed")
        }
        XCTAssertTrue(
            message.contains("database is locked"),
            "the CLI's own message is the only thing that says why: \(message)"
        )
    }

    /// A failed *refresh* leaves the states it already had standing.
    ///
    /// Same call `ConfigStore.refreshDeliveryStates` makes, for its reason:
    /// stale is closer to the truth than absent. Blanking a populated section
    /// because a re-read failed replaces four correct sentences with one
    /// apology.
    func test_a_failed_refresh_keeps_the_states_already_read() async {
        let store = DeviceDeliveryStore(runner: sh())
        await load(
            store, udid: Self.phoneA,
            printing: Self.stateJSON(
                app: "example", kind: "stale", summary: "Changed since the last delivery."
            )
        )
        await store.load(
            udid: Self.phoneA,
            argsOverride: ["-c", "printf 'device went away\\n' >&2; exit 1"]
        )
        XCTAssertEqual(
            DeviceAppsSection.state(of: "example", in: store.readout(for: Self.phoneA))?.kind,
            .stale,
            "the failed refresh replaced states that were true a moment ago"
        )
    }

    /// A phone the CLI answered for, with nothing to say about any app, is
    /// `.ready` and empty — not `.pending` and not `.failed`. This is what an
    /// empty config produces, and it is the one case where every row correctly
    /// reads "No delivery state reported for this app."
    func test_an_empty_answer_is_ready_rather_than_pending() async {
        let store = DeviceDeliveryStore(runner: sh())
        await load(store, udid: Self.phoneA, printing: "")
        XCTAssertEqual(store.readout(for: Self.phoneA), .ready([:]))
    }
}

/// The four sentences a row can carry, and which states earn a dot.
///
/// Tested as values rather than through a rendered view, the same way
/// `DeviceStatStripValueTests` treats the strip: asserting on a SwiftUI body is
/// expensive and brittle, and every decision worth pinning here is a pure
/// function of the readout.
///
/// The decision that matters most: **"we haven't asked" must never render as
/// "never delivered".** The section's whole claim is that it says something true
/// about *this* phone, and the one way to make it a liar is to let an unanswered
/// question look like an answer.
@MainActor
final class DeviceAppsSectionCopyTests: XCTestCase {

    private static func record(
        app: String = "example", kind: DeliveryKind, summary: String, reasons: [String] = []
    ) -> DeliveryStateRecord {
        DeliveryStateRecord(
            app: app, kind: kind, summary: summary, reasons: reasons, pushedAt: nil
        )
    }

    /// The answered case renders the CLI's own sentence, unedited. Every word
    /// the user reads about a delivery comes from the CLI — the same rule
    /// `AppDetailView` follows, and the reason `DeliveryKind.stateLabel`'s
    /// strings are not rendered here as a second opinion.
    func test_an_answered_app_shows_the_clis_own_summary() {
        let readout = DeviceDeliveryStore.Readout.ready([
            "example": Self.record(kind: .never, summary: "Never delivered to a device."),
        ])
        XCTAssertEqual(
            DeviceAppsSection.statusLine(of: "example", in: readout),
            "Never delivered to a device."
        )
    }

    /// Asked, answered, and this app wasn't in the answer — an older CLI, or a
    /// state it declined to compute. Deliberately the same wording
    /// `AppDetailView` uses for the same fact.
    func test_an_app_the_cli_did_not_describe_says_so() {
        let readout = DeviceDeliveryStore.Readout.ready([
            "example": Self.record(kind: .current, summary: "Matches the last delivery."),
        ])
        XCTAssertEqual(
            DeviceAppsSection.statusLine(of: "charlie", in: readout),
            "No delivery state reported for this app."
        )
    }

    /// The load hasn't landed. This is the assertion the section exists to keep
    /// honest: an unanswered question must not read as `never`.
    func test_a_pending_read_says_it_is_still_reading() {
        let line = DeviceAppsSection.statusLine(of: "example", in: .pending)
        XCTAssertEqual(line, "Reading this phone's delivery log…")
        XCTAssertFalse(
            line.lowercased().contains("never"),
            "a state nobody has read yet must never be phrased as a delivery verdict"
        )
    }

    /// The read failed. The reason is on screen once, above every row, so the
    /// row itself carries the app's established mark for "no reading" rather
    /// than the same apology N times.
    func test_a_failed_read_leaves_the_row_blank_rather_than_guessing() {
        XCTAssertEqual(
            DeviceAppsSection.statusLine(of: "example", in: .failed("database is locked")),
            "—"
        )
    }

    /// The dot is driven by `DeliveryKind.needsAttention` through
    /// `state(of:in:)`, so every state nobody has read yet produces no record
    /// and therefore no dot. A mark the user is meant to act on must not appear
    /// for a question that was never asked.
    func test_no_record_is_produced_for_a_state_that_was_never_read() {
        XCTAssertNil(DeviceAppsSection.state(of: "example", in: .pending))
        XCTAssertNil(DeviceAppsSection.state(of: "example", in: .failed("nope")))
    }

    /// The dot's predicate is `DeliveryKind`'s, not a second copy of it. This
    /// pins the pairing the row relies on: the two kinds that earn a mark, and
    /// the two that stay quiet.
    func test_the_dot_follows_the_models_own_decision() {
        XCTAssertTrue(DeliveryKind.never.needsAttention)
        XCTAssertTrue(DeliveryKind.stale.needsAttention)
        XCTAssertFalse(DeliveryKind.current.needsAttention)
        XCTAssertFalse(
            DeliveryKind.unknown.needsAttention,
            "`unknown` means we genuinely can't say — marking it trains the user to "
            + "ignore the mark that matters"
        )
    }

    // MARK: - The regression this section is one edit away from

    /// **The device pane must not read `ConfigStore.deliveryStates`.**
    ///
    /// That map is `--list`'s unscoped answer: "delivered anywhere". Two live
    /// views mean it that way — `SidebarView`'s warning dot and
    /// `AppDetailView`'s delivery card — and neither names a phone.
    ///
    /// The regression this guards is not hypothetical, it is the *obvious fix*:
    /// a row on phone B reading "Never delivered to a device." for an app the
    /// sidebar shows as delivered looks exactly like a bug, and the one-line
    /// repair is to reach for the map that agrees with the sidebar. Nothing
    /// else here would fail — the rows would keep rendering, the section would
    /// simply stop being about a phone, which is the entire feature. So the
    /// scan is the test: `Views/Devices/` may not name that field at all.
    ///
    /// Source-scanned for the same reason `CLIContractTests` scans for bare
    /// flag literals: the property is about which *identifier* a caller reaches
    /// for, and no runtime assertion can see that choice.
    func test_the_device_pane_does_not_read_the_unscoped_delivery_states() throws {
        let dir = URL(fileURLWithPath: #filePath)   // …/GraftTests/…Tests.swift
            .deletingLastPathComponent()            // …/GraftTests
            .deletingLastPathComponent()            // …/Graft (project dir)
            .appendingPathComponent("Graft/Views/Devices")
        guard FileManager.default.fileExists(atPath: dir.path) else {
            throw XCTSkip("app sources not found at \(dir.path)")
        }
        let files = try FileManager.default
            .contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "swift" }
        XCTAssertFalse(files.isEmpty, "no Swift sources under \(dir.path) — scanned nothing")

        var offenders: [String] = []
        for file in files {
            let lines = try String(contentsOf: file, encoding: .utf8)
                .components(separatedBy: .newlines)
                .enumerated()
                // Comments dropped, so the doc comments that *explain* this
                // rule aren't reported as breaking it.
                .filter { !$0.element.trimmingCharacters(in: .whitespaces).hasPrefix("//") }
            for (offset, text) in lines where text.contains("deliveryStates") {
                offenders.append("\(file.lastPathComponent):\(offset + 1)")
            }
        }
        XCTAssertTrue(
            offenders.isEmpty,
            """
            \(offenders.joined(separator: ", ")) reads ConfigStore.deliveryStates, which \
            answers "delivered anywhere". The per-device pane must read \
            DeviceDeliveryStore instead — an app delivered to another phone genuinely \
            reads as never delivered here, and that disagreement is the feature.
            """
        )
    }
}
