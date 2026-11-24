import XCTest
@testable import Graft

/// `StagedIPAStore`'s two decisions that are easy to get subtly wrong: which
/// record wins when two IPAs claim the same bundle id (`preferred`, not
/// arrival order — see `test_a_preferred_record_still_wins_when_it_arrives_
/// after_the_unpreferred_one`), and that `hasLoaded` is a fact about whether a
/// load ever *finished* — not a synonym for `byBundleId.isEmpty`, which a
/// real empty cache would satisfy just as well as "never asked" would. A
/// `nil` `lost` also has to survive the store rather than being folded into
/// `[]`: the two mean different things on the wire (see `StagedIPARecord`'s
/// own doc), and `InstalledAppsSection.rowState` reads `hasLoaded` for the
/// exact reason recorded here — a row that offered "Add" before the cache had
/// actually answered would be making the same "never asked" mistake this
/// store exists to avoid.
final class StagedIPAStoreTests: XCTestCase {
    @MainActor
    func test_the_argv_asks_for_json_and_the_verb() {
        XCTAssertEqual(StagedIPAStore.loadArgs(), ["--json", "--staged-ipas"])
    }

    @MainActor
    func test_load_keys_the_preferred_record_by_bundle_id() async {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = StagedIPAStore(runner: runner)
        let script = #"printf '\#(stagedIPAStreamLine(bundleId: "com.a.one"))\n\#(stagedIPAStreamLine(bundleId: "com.b.two"))\n\#(stagedIPAsEndLine(2))\n'"#

        await store.load(argsOverride: ["-c", script])

        XCTAssertEqual(store.byBundleId.count, 2)
        XCTAssertEqual(store.byBundleId["com.a.one"]?.displayName, "Example")
        XCTAssertTrue(store.hasLoaded)
    }

    /// Two versions of one app, preferred emitted *first* — the one ordering
    /// where the CLI's real `sorted(glob("*.ipa"))` filename sort and version
    /// order can actually disagree: `21.16.2` sorts before `21.9.1` as a
    /// string, because the middle component's digit count changes (`16` vs
    /// `9`), even though `21.16.2` is the newer version. This pins the
    /// `||`'s second half: deleting the whole condition down to an
    /// unconditional overwrite (last record always wins) would let the
    /// unpreferred second line displace this one, and only an arrangement
    /// where the preferred record arrives first can catch that — see the
    /// sibling test below for the ordering this one cannot catch.
    @MainActor
    func test_an_unpreferred_record_never_displaces_the_preferred_one() async {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = StagedIPAStore(runner: runner)
        let script = #"printf '\#(stagedIPAStreamLine(bundleId: "com.a.one", version: "21.16.2", preferred: true))\n\#(stagedIPAStreamLine(bundleId: "com.a.one", version: "21.9.1", preferred: false))\n\#(stagedIPAsEndLine(2))\n'"#

        await store.load(argsOverride: ["-c", script])

        XCTAssertEqual(store.byBundleId["com.a.one"]?.version, "21.16.2")
    }

    /// The mirror of the test above, and the ordering an *ordinary* version
    /// bump actually produces: for same-digit-count versions like these, the
    /// older file's name sorts first, so a real cache hands this store the
    /// unpreferred record before the preferred one. This is the half the
    /// sibling test cannot pin — its ordering happens to make the naive
    /// "first record wins" reading (deleting `record.preferred ||` entirely,
    /// keeping only `found[record.bundleId] == nil`) pass too, since the
    /// record that arrives first there is also the preferred one. Only this
    /// arrangement, where they disagree, catches that mutation.
    @MainActor
    func test_a_preferred_record_still_wins_when_it_arrives_after_the_unpreferred_one() async {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = StagedIPAStore(runner: runner)
        let script = #"printf '\#(stagedIPAStreamLine(bundleId: "com.a.one", version: "1.0", preferred: false))\n\#(stagedIPAStreamLine(bundleId: "com.a.one", version: "2.0", preferred: true))\n\#(stagedIPAsEndLine(2))\n'"#

        await store.load(argsOverride: ["-c", script])

        XCTAssertEqual(store.byBundleId["com.a.one"]?.version, "2.0")
    }

    /// The defect this GUI has shipped three times. An empty cache is a real
    /// answer; never having asked is not, and no view may read them the same.
    @MainActor
    func test_an_empty_cache_is_loaded_and_not_merely_empty() async {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = StagedIPAStore(runner: runner)

        XCTAssertFalse(store.hasLoaded, "premise: nothing asked yet")
        XCTAssertTrue(store.byBundleId.isEmpty)

        await store.load(argsOverride: ["-c", #"printf '\#(stagedIPAsEndLine(0))\n'"#])

        XCTAssertTrue(store.byBundleId.isEmpty)
        XCTAssertTrue(store.hasLoaded, "an empty cache is an answer, not a silence")
    }

    /// The absence half of the check: a run that produced a `staged-ipa`
    /// record but no `staged-ipas-end` at all, so `declaredCount` stays nil.
    /// However that shape arises — this store does not need to know, only
    /// that a run with no terminator answered nothing — publishing what did
    /// arrive would be worse than publishing nothing: on screen it is
    /// indistinguishable from an honest empty cache, so the rest of the
    /// cache, the apps this run never got to, would render as "nothing
    /// staged" with no way to tell the difference. See the sibling test
    /// below for the other half `declaredCount` catches: a terminator that
    /// *did* arrive but disagrees with what was actually decoded.
    @MainActor
    func test_a_run_that_emits_records_but_never_the_terminator_publishes_nothing() async {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = StagedIPAStore(runner: runner)
        let script = #"printf '\#(stagedIPAStreamLine(bundleId: "com.a.one"))\n'"#

        await store.load(argsOverride: ["-c", script])

        XCTAssertFalse(store.hasLoaded, "the run answered nothing — it never reached the end")
        XCTAssertNil(
            store.byBundleId["com.a.one"],
            "a record without the terminator is a partial answer, and must not be published"
        )
    }

    /// The sentinel can arrive with a count that disagrees with what was
    /// actually decoded. Not from a truncated stream — a single sequential
    /// writer cannot corrupt line k and still emit line k+1 intact, and a
    /// reader that hangs up or a pipe that breaks exits non-zero here rather
    /// than truncating silently — but from contract drift: a `staged-ipa`
    /// line whose field types this decoder rejects, the exact shape a
    /// non-string `bundle_id` or `display_name` used to produce before
    /// `read_info_plist` coerced both (see this store's own type doc). The
    /// sentinel existing is not enough on its own: it must agree with what
    /// was actually counted, and disagreeing must be treated exactly like no
    /// terminator at all. (Checked against a raw per-record tally, not the
    /// published dictionary's size — two staged versions of one app collapse
    /// to a single entry there, which would make an ordinary healthy
    /// multi-version load fail this same check.)
    @MainActor
    func test_a_count_that_disagrees_with_what_was_actually_seen_publishes_nothing() async {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = StagedIPAStore(runner: runner)
        let script = #"printf '\#(stagedIPAStreamLine(bundleId: "com.a.one"))\n\#(stagedIPAStreamLine(bundleId: "com.b.two"))\n\#(stagedIPAsEndLine(3))\n'"#

        await store.load(argsOverride: ["-c", script])

        XCTAssertFalse(store.hasLoaded)
        XCTAssertTrue(store.byBundleId.isEmpty)
        XCTAssertTrue(
            store.all.isEmpty,
            "a count-mismatched listing must not publish into `all` either — "
            + "only `byBundleId` was pinned here before, so hoisting `all`'s "
            + "assignment above this guard passed unnoticed"
        )
    }

    /// A non-zero exit makes `runEvents` throw before it decodes a byte of
    /// stdout — `CLIRunner.checkExit` runs ahead of
    /// `result.stdout.buildEvents()` unconditionally, so this is not "no end
    /// event arrived" but "no event was ever read." `load`'s `try?` catches
    /// that throw and returns without touching `byBundleId` or `hasLoaded`,
    /// so a failed run must not leave the pane believing the cache is empty.
    @MainActor
    func test_a_failed_run_does_not_claim_to_have_loaded() async {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = StagedIPAStore(runner: runner)

        await store.load(argsOverride: ["-c", "exit 3"])

        XCTAssertFalse(store.hasLoaded)
        XCTAssertTrue(store.byBundleId.isEmpty)
    }

    @MainActor
    func test_a_stale_listing_survives_a_failed_refresh() async {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = StagedIPAStore(runner: runner)
        await store.load(
            argsOverride: ["-c", #"printf '\#(stagedIPAStreamLine(bundleId: "com.a.one"))\n\#(stagedIPAsEndLine(1))\n'"#]
        )
        XCTAssertNotNil(store.byBundleId["com.a.one"], "premise: the good listing landed")
        XCTAssertTrue(store.hasLoaded, "premise: the good listing set hasLoaded")

        await store.load(argsOverride: ["-c", "exit 3"])

        XCTAssertNotNil(
            store.byBundleId["com.a.one"],
            "a phone-less local listing that failed should not erase what the pane is showing"
        )
        XCTAssertTrue(
            store.hasLoaded,
            "a failed refresh must not un-answer a question that was already answered"
        )
    }

    /// A refresh must *replace* the listing, not merge into it: an IPA can
    /// leave the cache between two loads — the user deleted it, a duplicate
    /// got pruned — and a merge would go on offering "Add as app" for a file
    /// that no longer exists.
    @MainActor
    func test_a_refresh_drops_entries_for_ipas_that_left_the_cache() async {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = StagedIPAStore(runner: runner)
        await store.load(
            argsOverride: ["-c", #"printf '\#(stagedIPAStreamLine(bundleId: "com.a.one"))\n\#(stagedIPAStreamLine(bundleId: "com.b.two"))\n\#(stagedIPAsEndLine(2))\n'"#]
        )
        XCTAssertEqual(store.byBundleId.count, 2, "premise: both landed")

        await store.load(
            argsOverride: ["-c", #"printf '\#(stagedIPAStreamLine(bundleId: "com.a.one"))\n\#(stagedIPAsEndLine(1))\n'"#]
        )

        XCTAssertNotNil(store.byBundleId["com.a.one"])
        XCTAssertNil(
            store.byBundleId["com.b.two"],
            "no longer staged, and a merge would have kept offering it"
        )
    }

    /// `load` suspends on the subprocess `await`, so a slower call that
    /// started first can finish after a faster one that started second —
    /// exactly the shape a load launched just before a dump racing one
    /// launched just after it produces. Without `loadGeneration`, the slow
    /// call's stale result would land last and silently overwrite the fresh
    /// one. Completion order is dictated, not hoped for: the slow script
    /// sleeps briefly before printing, the fast one returns immediately, and
    /// `Task.yield()` gives the slow call's `Task` a chance to reach its own
    /// suspension point before the fast call runs inline.
    @MainActor
    func test_a_slower_call_that_started_first_does_not_overwrite_a_faster_one_that_started_second() async {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = StagedIPAStore(runner: runner)
        let slowScript = #"sleep 0.5; printf '\#(stagedIPAStreamLine(bundleId: "com.a.one", version: "stale"))\n\#(stagedIPAsEndLine(1))\n'"#
        let fastScript = #"printf '\#(stagedIPAStreamLine(bundleId: "com.a.one", version: "fresh"))\n\#(stagedIPAsEndLine(1))\n'"#

        let slow = Task { await store.load(argsOverride: ["-c", slowScript]) }
        await Task.yield()
        await store.load(argsOverride: ["-c", fastScript])
        _ = await slow.value

        XCTAssertEqual(
            store.byBundleId["com.a.one"]?.version, "fresh",
            "the call that started second must win regardless of which one finished last"
        )
        XCTAssertEqual(
            store.all.map(\.version), ["fresh"],
            "`all` must lose the race the same way `byBundleId` does — "
            + "publishing it ahead of the generation guard would let the slow "
            + "call's stale listing land in `all` after the fresh one already "
            + "published, even though `byBundleId` kept the fresh version"
        )
    }

    /// Not a guard on anything this store does — `StagedIPARecord`'s own
    /// decoder owns the nil-vs-empty distinction (see its doc), and this
    /// store only ever stores whole records without touching `lost`. Kept as
    /// a boundary assertion: proof nothing between the wire and `byBundleId`
    /// quietly collapses the distinction, even though no mutation in this
    /// file could make it fail.
    @MainActor
    func test_lost_survives_the_store_as_nil_rather_than_empty() async {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = StagedIPAStore(runner: runner)
        let script = #"printf '\#(stagedIPAStreamLine(bundleId: "com.a.old", stripped: #"["X.appex"]"#))\n\#(stagedIPAStreamLine(bundleId: "com.a.new", stripped: #"["Y.appex"]"#, lost: "[]"))\n\#(stagedIPAsEndLine(2))\n'"#

        await store.load(argsOverride: ["-c", script])

        XCTAssertNil(store.byBundleId["com.a.old"]?.lost)
        XCTAssertEqual(store.byBundleId["com.a.new"]?.lost, [])
    }

    /// `all` and `byBundleId` are two readings of one listing, and the only
    /// input where they differ is two versions of one app: `byBundleId` keeps
    /// the file a build would use, `all` keeps both, because the Cache pane
    /// exists partly to delete the one a build would not use.
    @MainActor
    func test_all_holds_every_record_while_byBundleId_holds_only_the_preferred_one() async {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = StagedIPAStore(runner: runner)
        let script = #"printf '\#(stagedIPAStreamLine(bundleId: "com.a.one", version: "21.16.2", preferred: true))\n\#(stagedIPAStreamLine(bundleId: "com.a.one", version: "21.9.1", preferred: false))\n\#(stagedIPAsEndLine(2))\n'"#

        await store.load(argsOverride: ["-c", script])

        XCTAssertEqual(store.all.count, 2, "both files")
        XCTAssertEqual(store.byBundleId.count, 1, "one winner per bundle id")
        XCTAssertEqual(store.byBundleId["com.a.one"]?.version, "21.16.2")
        XCTAssertEqual(
            store.all.map(\.version), ["21.16.2", "21.9.1"],
            "in the order the CLI emitted them"
        )
    }

    /// A failed refresh must not blank `all` any more than it blanks
    /// `byBundleId` — the pane keeps showing what it had.
    @MainActor
    func test_a_failed_refresh_leaves_all_standing() async {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = StagedIPAStore(runner: runner)
        await store.load(
            argsOverride: ["-c", #"printf '\#(stagedIPAStreamLine(bundleId: "com.a.one"))\n\#(stagedIPAsEndLine(1))\n'"#]
        )
        XCTAssertEqual(store.all.count, 1, "premise: the good listing landed")

        await store.load(argsOverride: ["-c", "exit 3"])

        XCTAssertEqual(store.all.count, 1)
    }

    // MARK: - The two verbs the Cache pane presses

    @MainActor
    func test_the_delete_argv_names_the_file_and_adds_force_only_when_forced() {
        XCTAssertEqual(
            StagedIPAStore.deleteArgs(filename: "com.a.one_1.0.ipa", force: false),
            ["--json", "--delete-staged", "com.a.one_1.0.ipa"]
        )
        XCTAssertEqual(
            StagedIPAStore.deleteArgs(filename: "com.a.one_1.0.ipa", force: true),
            ["--json", "--delete-staged", "com.a.one_1.0.ipa", "--force"],
            "the flag the CLI's own refusal names as the way past it"
        )
    }

    @MainActor
    func test_the_install_argv_names_the_file() {
        XCTAssertEqual(
            StagedIPAStore.installArgs(filename: "com.a.one_1.0.ipa"),
            ["--json", "--install-to-mac", "com.a.one_1.0.ipa"]
        )
    }

    /// The refusal, and **whose words it is carried in**.
    ///
    /// Design spec §3 is emphatic that the GUI must not compose its own
    /// version of this objection: the CLI knows which configured apps would be
    /// left with nothing to build from, and a second sentence assembled on this
    /// side drifts from it the first time either half changes. So this asserts
    /// the *whole* string, equal to what the stub wrote on stderr — a store
    /// that rebuilt the sentence from the `used_by` list on the
    /// `staged-ipa-in-use` event would produce a different one and fail here.
    ///
    /// The event is what the refusal is *recognized* by (and exit 17 as the
    /// fallback for a CLI too old to emit it, the same two-channel reading
    /// `TweaksView` does for `--delete-tweak`); stderr is what it is *shown*
    /// in. Two channels, two jobs.
    @MainActor
    func test_an_unforced_delete_the_cli_refuses_comes_back_in_the_clis_own_words() async throws {
        let store = StagedIPAStore(runner: CLIRunner(executable: try Self.stubCLI(self)))

        let answer = await store.deleteStaged(filename: Self.stubFilename, force: false)

        XCTAssertEqual(
            answer,
            .inUse(objection: "\(Self.stubObjection) — hint: \(Self.stubHint)"),
            "the objection the pane shows has to be the sentence the CLI wrote, "
            + "not one composed here from used_by"
        )
    }

    /// The other half of the two-step, and the one that proves `--force`
    /// actually reaches the process rather than merely being present in
    /// `deleteArgs`. The stub refuses exactly as the real CLI does — without
    /// the flag — so a `deleteStaged` that dropped it would come back
    /// `.inUse` here instead of `.deleted`.
    @MainActor
    func test_a_forced_delete_gets_past_the_refusal_and_re_reads_the_listing() async throws {
        let store = StagedIPAStore(runner: CLIRunner(executable: try Self.stubCLI(self)))
        XCTAssertFalse(store.hasLoaded, "premise: nothing has read the cache yet")

        let answer = await store.deleteStaged(filename: Self.stubFilename, force: true)

        XCTAssertEqual(answer, .deleted)
        XCTAssertTrue(
            store.hasLoaded,
            "a delete that landed must re-read the listing — the deleted row has to "
            + "leave the pane, and this store is shared with the device pane's join"
        )
        XCTAssertEqual(store.all.count, 2, "the listing the re-read fetched")
    }

    /// A delete that fails for a reason no button can answer is not a refusal
    /// and must not be dressed as one: `.failed`, so the pane banners it
    /// rather than offering "Delete Anyway" over something forcing cannot fix.
    @MainActor
    func test_a_delete_that_fails_for_another_reason_is_not_a_refusal() async {
        let store = StagedIPAStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))

        let answer = await store.deleteStaged(
            filename: "gone.ipa", force: false,
            argsOverride: ["-c", #"echo "error: no staged IPA named gone.ipa" >&2; exit 4"#]
        )

        XCTAssertEqual(answer, .failed("no staged IPA named gone.ipa"))
    }

    /// A handoff that worked says nothing. `open` returns as soon as the event
    /// is delivered, so there is no finished install to report and this store
    /// deliberately does not invent one — see `installToMac`'s own doc.
    @MainActor
    func test_a_handoff_that_worked_has_nothing_to_report() async {
        let store = StagedIPAStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))

        let complaint = await store.installToMac(
            filename: "com.a.one_1.0.ipa", argsOverride: ["-c", "exit 0"]
        )

        XCTAssertNil(complaint)
    }

    /// And one that did not says the CLI's sentence. This is the whole reason
    /// the answer is a value rather than a discarded `Task`: with PlayCover
    /// absent, `--install-to-mac` fails and the button would otherwise be
    /// indistinguishable from a dead one.
    @MainActor
    func test_a_handoff_the_cli_could_not_make_reports_what_it_said() async {
        let store = StagedIPAStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))

        let complaint = await store.installToMac(
            filename: "com.a.one_1.0.ipa",
            argsOverride: ["-c", #"echo "error: PlayCover is not installed, or could not launch" >&2; exit 1"#]
        )

        XCTAssertEqual(complaint, "PlayCover is not installed, or could not launch")
    }

    // MARK: - The stub these four run against

    private static let stubFilename = "com.a.one_21.16.2.ipa"
    private static let stubObjection =
        "com.a.one_21.16.2.ipa is the only staged IPA for: example"
    private static let stubHint =
        "deleting it leaves nothing for the next build to use"

    /// A `graft` that answers both verbs the way the real one does.
    ///
    /// It branches on argv rather than ignoring it — unlike
    /// `CachePaneTests.stubCLI`, which deliberately does not — because the
    /// two things being pinned here *are* argv-dependent: `--delete-staged`
    /// without `--force` refuses and with it succeeds, which is the CLI's own
    /// two-step, and the reload after a successful delete runs
    /// `--staged-ipas` and has to get a listing back rather than the delete's
    /// own event.
    ///
    /// No apostrophes and no `%` in any payload: the JSON is inside a `printf`
    /// *format string* in single quotes. `CachePaneTests.stubCLI` records the
    /// same constraint.
    private static func stubCLI(_ testCase: XCTestCase) throws -> URL {
        let listing = [
            stagedIPAStreamLine(bundleId: "com.a.one", version: "21.16.2", preferred: true),
            stagedIPAStreamLine(bundleId: "com.a.one", version: "21.9.1", preferred: false),
            stagedIPAsEndLine(2),
        ].joined(separator: #"\n"#) + #"\n"#
        let deleted = #"{"type":"staged-ipa-deleted","filename":"\#(stubFilename)","also_removed":[]}"#
        let inUse = #"{"type":"staged-ipa-in-use","filename":"\#(stubFilename)","used_by":["example"]}"#
        let script = """
        #!/bin/sh
        case "$*" in
          *--staged-ipas*)
            printf '\(listing)'
            ;;
          *--delete-staged*)
            case "$*" in
              *--force*)
                printf '\(deleted)\\n'
                ;;
              *)
                printf '\(inUse)\\n'
                echo "error: \(stubObjection)" >&2
                echo "hint: \(stubHint)" >&2
                exit 17
                ;;
            esac
            ;;
        esac
        """
        let path = NSTemporaryDirectory() + "staged_verbs_stub_\(UUID().uuidString).sh"
        try script.write(toFile: path, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o755], ofItemAtPath: path
        )
        testCase.addTeardownBlock { try? FileManager.default.removeItem(atPath: path) }
        return URL(fileURLWithPath: path)
    }
}
