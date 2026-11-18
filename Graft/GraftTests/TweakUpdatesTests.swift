import XCTest
@testable import Graft

/// Reading `--tweak-updates` into something the library rows can show.
///
/// The comparison itself is the CLI's (`repo.version_key`, Debian semantics,
/// tested in `tests/test_tweak_updates.py`). What is worth pinning here is the
/// joining: which row an answer belongs to, and what the pane is allowed to
/// claim before anybody has asked.
final class TweakUpdatesTests: XCTestCase {

    @MainActor
    private func store(_ script: String) -> TweaksStore {
        TweaksStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
    }

    /// One `tweak-update` line, built through `JSONSerialization` rather than
    /// string concatenation — the hand-assembled version doubled a quote at a
    /// fragment seam and produced JSON that decoded as nothing at all.
    private func line(
        _ filename: String, installed: String, latest: String?, available: Bool,
        pid: String = "com.x", source: String = "canister"
    ) -> String {
        let payload: [String: Any] = [
            "type": "tweak-update",
            "filename": filename,
            "package_id": pid,
            "installed": installed,
            "latest": latest as Any? ?? NSNull(),
            "source": latest == nil ? NSNull() : source as Any,
            "update_available": available,
        ]
        let data = try! JSONSerialization.data(withJSONObject: payload)
        return String(data: data, encoding: .utf8)!
    }

    @MainActor
    func test_updates_are_joined_to_rows_by_filename() async {
        let events = [
            line("a.deb", installed: "1.0", latest: "2.0", available: true),
            line("b.deb", installed: "3.0", latest: "3.0", available: false),
            #"{"type":"tweak-updates-end","checked":2,"outdated":1,"unchecked":0}"#,
        ].joined(separator: #"\n"#)
        let sut = TweaksStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))

        await sut.checkForUpdates(argsOverride: ["-c", "printf '\(events)\\n'"])

        XCTAssertEqual(sut.update(for: .init(filename: "a.deb", sizeBytes: 0, usedBy: []))?.latest,
                       "2.0")
        XCTAssertTrue(
            sut.update(for: .init(filename: "a.deb", sizeBytes: 0, usedBy: []))?.updateAvailable
                == true
        )
        XCTAssertFalse(
            sut.update(for: .init(filename: "b.deb", sizeBytes: 0, usedBy: []))?.updateAvailable
                == true
        )
        XCTAssertNil(sut.update(for: .init(filename: "gone.deb", sizeBytes: 0, usedBy: [])))
        XCTAssertEqual(sut.outdatedCount, 1)
        XCTAssertEqual(sut.lastCheck?.checked, 2)
    }

    /// Two files carrying the same package is a real state — this library holds
    /// `1.1.8` and `1.1.8-jailed` of the same tweak — so the join has to be by
    /// file, not by package id, or one row answers for the other.
    @MainActor
    func test_two_files_of_the_same_package_are_answered_for_separately() async {
        let events = [
            line("plain.deb", installed: "1.1.8", latest: "1.2.0", available: true),
            line("jailed.deb", installed: "1.1.8-jailed", latest: "1.2.0", available: true),
            #"{"type":"tweak-updates-end","checked":2,"outdated":2,"unchecked":0}"#,
        ].joined(separator: #"\n"#)
        let sut = TweaksStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))

        await sut.checkForUpdates(argsOverride: ["-c", "printf '\(events)\\n'"])

        XCTAssertEqual(
            sut.update(for: .init(filename: "plain.deb", sizeBytes: 0, usedBy: []))?.installed,
            "1.1.8"
        )
        XCTAssertEqual(
            sut.update(for: .init(filename: "jailed.deb", sizeBytes: 0, usedBy: []))?.installed,
            "1.1.8-jailed"
        )
    }

    /// Anything built from source is published nowhere. The row must show no
    /// badge, and — critically — that must not read as an update.
    @MainActor
    func test_a_tweak_nothing_publishes_shows_nothing() async {
        let events = [
            line("mine.deb", installed: "1.0.0", latest: nil, available: false),
            #"{"type":"tweak-updates-end","checked":1,"outdated":0,"unchecked":0}"#,
        ].joined(separator: #"\n"#)
        let sut = TweaksStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))

        await sut.checkForUpdates(argsOverride: ["-c", "printf '\(events)\\n'"])

        let record = sut.update(for: .init(filename: "mine.deb", sizeBytes: 0, usedBy: []))
        XCTAssertNotNil(record, "a file with no published version got no answer at all")
        XCTAssertNil(record?.latest)
        XCTAssertFalse(record?.updateAvailable == true)
        XCTAssertEqual(sut.outdatedCount, 0)
    }

    /// Before anything is asked, the pane may not claim the library is current.
    @MainActor
    func test_nothing_is_claimed_before_a_check_has_run() {
        let sut = TweaksStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        XCTAssertNil(sut.lastCheck, "the pane would say 'all up to date' having asked nobody")
        XCTAssertEqual(sut.outdatedCount, 0)
    }

    /// A file deleted since the last check must stop having an answer, and a
    /// filename that now holds a different .deb must not inherit the old one.
    @MainActor
    func test_a_second_check_replaces_the_first_rather_than_merging() async {
        let sut = TweaksStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")))
        let first = [
            line("a.deb", installed: "1.0", latest: "2.0", available: true),
            line("stale.deb", installed: "1.0", latest: "2.0", available: true),
            #"{"type":"tweak-updates-end","checked":2,"outdated":2,"unchecked":0}"#,
        ].joined(separator: #"\n"#)
        await sut.checkForUpdates(argsOverride: ["-c", "printf '\(first)\\n'"])
        XCTAssertEqual(sut.outdatedCount, 2)

        let second = [
            line("a.deb", installed: "2.0", latest: "2.0", available: false),
            #"{"type":"tweak-updates-end","checked":1,"outdated":0,"unchecked":0}"#,
        ].joined(separator: #"\n"#)
        await sut.checkForUpdates(argsOverride: ["-c", "printf '\(second)\\n'"])

        XCTAssertNil(
            sut.update(for: .init(filename: "stale.deb", sizeBytes: 0, usedBy: [])),
            "a deleted file kept claiming an update from the previous check"
        )
        XCTAssertEqual(sut.outdatedCount, 0)
    }

    /// A failed check reports and leaves the previous answers alone rather than
    /// blanking the badges the user is looking at.
    @MainActor
    func test_a_failed_check_is_reported() async {
        let sut = TweaksStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/false")))
        await sut.checkForUpdates()

        XCTAssertNotNil(sut.lastError)
        XCTAssertFalse(sut.isCheckingUpdates, "the spinner never stopped")
    }
}

// MARK: - Performing the update

extension TweakUpdatesTests {

    /// A stub `graft` that answers all three calls an update involves:
    /// `--tweak-updates`, `--update-tweak`, and the `--tweaks` reload that
    /// follows the swap.
    ///
    /// A runner that answers only the update makes these fail on the reload —
    /// real behaviour, correctly reported, but not what they are asking about.
    private func stub(
        check: String = "", updated: String, library: [String] = []
    ) throws -> (CLIRunner, () -> Void) {
        let path = NSTemporaryDirectory() + "tbupd_\(UUID().uuidString).sh"
        // `--tweaks` lists `library`, because `updateTweak` reloads and the
        // reload prunes answers for files that are no longer there. A stub
        // that reported an empty library made every badge vanish for the right
        // reason and the wrong one.
        let tweaks = library
            .map { #"{"type":"tweak","filename":"\#($0)","size_bytes":1,"used_by":[]}"# }
            .joined(separator: "\n")
        let script = """
        #!/bin/sh
        for arg in "$@"; do
          case "$arg" in
            --tweaks) printf '%s\\n' '\(tweaks)'; exit 0 ;;
            --tweak-updates) printf '%s\\n' '\(check)'; exit 0 ;;
          esac
        done
        printf '%s\\n' '\(updated)'
        """
        try script.write(toFile: path, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: path)
        return (
            CLIRunner(executable: URL(fileURLWithPath: path)),
            { try? FileManager.default.removeItem(atPath: path) }
        )
    }

    private func updatedLine(
        _ filename: String, new: String?, updated: Bool,
        apps: [String] = [], reason: String? = nil, latest: String? = nil
    ) -> String {
        var payload: [String: Any] = [
            "type": "tweak-updated",
            "filename": filename,
            "updated": updated,
            "apps": apps,
        ]
        payload["new_filename"] = new as Any? ?? NSNull()
        payload["reason"] = reason as Any? ?? NSNull()
        payload["latest"] = latest as Any? ?? NSNull()
        let data = try! JSONSerialization.data(withJSONObject: payload)
        return String(data: data, encoding: .utf8)!
    }

    /// After a swap the badge belongs to a different file, so the row that was
    /// clicked must stop claiming an update it no longer has.
    @MainActor
    func test_a_completed_update_clears_the_badge_for_the_old_file() async throws {
        let check = [
            line("old.deb", installed: "1.0", latest: "2.0", available: true),
            #"{"type":"tweak-updates-end","checked":1,"outdated":1,"unchecked":0}"#,
        ].joined(separator: "\n")
        let done = updatedLine("old.deb", new: "new.deb", updated: true,
                               apps: ["example"], latest: "2.0")
        let (runner, cleanup) = try stub(check: check, updated: done)
        defer { cleanup() }
        let sut = TweaksStore(runner: runner)

        await sut.checkForUpdates()
        XCTAssertEqual(sut.outdatedCount, 1, "premise: the row is badged before the update")

        let result = await sut.updateTweak(
            .init(filename: "old.deb", sizeBytes: 0, usedBy: ["example"])
        )

        XCTAssertEqual(result?.newFilename, "new.deb")
        XCTAssertEqual(result?.apps, ["example"])
        XCTAssertNil(
            sut.update(for: .init(filename: "old.deb", sizeBytes: 0, usedBy: [])),
            "the replaced file still advertises an update it no longer has"
        )
        XCTAssertEqual(sut.outdatedCount, 0)
    }

    /// "Nothing newer" is an answer, not a failure — a badge clicked after the
    /// library moved on under it.
    @MainActor
    func test_an_update_with_nothing_to_do_is_not_an_error() async throws {
        let done = updatedLine("cur.deb", new: nil, updated: false,
                               reason: "already the newest published version")
        let (runner, cleanup) = try stub(updated: done)
        defer { cleanup() }
        let sut = TweaksStore(runner: runner)

        let result = await sut.updateTweak(.init(filename: "cur.deb", sizeBytes: 0, usedBy: []))

        XCTAssertEqual(result?.updated, false)
        XCTAssertEqual(result?.reason, "already the newest published version")
        XCTAssertNil(sut.lastError, "a no-op update was reported as a failure")
    }

    /// The swap already landed on disk, so a reload that fails afterwards must
    /// report a stale *list* — not discard the result and claim the update
    /// failed. Same rule as `CLIStore.mutate`.
    @MainActor
    func test_a_failed_reload_after_a_successful_swap_still_reports_the_swap() async throws {
        let done = updatedLine("old.deb", new: "new.deb", updated: true, latest: "2.0")
        let path = NSTemporaryDirectory() + "tbupd_\(UUID().uuidString).sh"
        // Succeeds at the update, fails the reload that follows it.
        let script = """
        #!/bin/sh
        for arg in "$@"; do
          if [ "$arg" = "--tweaks" ]; then exit 1; fi
        done
        printf '%s\\n' '\(done)'
        """
        try script.write(toFile: path, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: path)
        defer { try? FileManager.default.removeItem(atPath: path) }

        let sut = TweaksStore(runner: CLIRunner(executable: URL(fileURLWithPath: path)))
        let result = await sut.updateTweak(.init(filename: "old.deb", sizeBytes: 0, usedBy: []))

        XCTAssertEqual(result?.newFilename, "new.deb", "a stale list discarded a landed swap")
        XCTAssertTrue(sut.lastError?.contains("couldn't refresh") == true)
    }

    @MainActor
    func test_a_failed_update_is_reported_and_leaves_the_spinner_off() async {
        let sut = TweaksStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/false")))
        let result = await sut.updateTweak(.init(filename: "a.deb", sizeBytes: 0, usedBy: []))

        XCTAssertNil(result)
        XCTAssertNotNil(sut.lastError)
        XCTAssertNil(sut.updatingFilename, "the row's spinner never stopped")
    }
}

// MARK: - What the review found

extension TweakUpdatesTests {

    /// The header is derived from the badges, so the two cannot disagree.
    /// Reading `lastCheck.outdated` — a snapshot of when the check ended —
    /// left it claiming "3 updates" over a list with no badged row in it.
    @MainActor
    func test_the_outdated_count_falls_as_rows_are_updated() async throws {
        let check = [
            line("a.deb", installed: "1.0", latest: "2.0", available: true),
            line("b.deb", installed: "1.0", latest: "2.0", available: true),
            #"{"type":"tweak-updates-end","checked":2,"outdated":2,"unchecked":0}"#,
        ].joined(separator: "\n")
        let done = updatedLine("a.deb", new: "a2.deb", updated: true, latest: "2.0")
        let (runner, cleanup) = try stub(check: check, updated: done, library: ["a.deb", "b.deb"])
        defer { cleanup() }
        let sut = TweaksStore(runner: runner)

        await sut.checkForUpdates()
        XCTAssertEqual(sut.outdatedCount, 2)
        XCTAssertEqual(sut.lastCheck?.outdated, 2)

        _ = await sut.updateTweak(.init(filename: "a.deb", sizeBytes: 0, usedBy: []))

        XCTAssertEqual(sut.outdatedCount, 1, "the header would still have said 2")
        // The snapshot is deliberately untouched — which is exactly why the
        // header must not read from it.
        XCTAssertEqual(sut.lastCheck?.outdated, 2)
    }

    /// "Already the newest published version" means the badge was stale. It
    /// has to go, or the row keeps advertising an update that provably does
    /// not exist, on a button that now does nothing.
    @MainActor
    func test_a_no_op_update_clears_the_stale_badge_too() async throws {
        let check = [
            line("a.deb", installed: "1.0", latest: "2.0", available: true),
            #"{"type":"tweak-updates-end","checked":1,"outdated":1,"unchecked":0}"#,
        ].joined(separator: "\n")
        let done = updatedLine("a.deb", new: nil, updated: false,
                               reason: "already the newest published version")
        let (runner, cleanup) = try stub(check: check, updated: done, library: ["a.deb"])
        defer { cleanup() }
        let sut = TweaksStore(runner: runner)

        await sut.checkForUpdates()
        XCTAssertEqual(sut.outdatedCount, 1, "premise: the row is badged")

        _ = await sut.updateTweak(.init(filename: "a.deb", sizeBytes: 0, usedBy: []))

        XCTAssertNil(sut.update(for: .init(filename: "a.deb", sizeBytes: 0, usedBy: [])))
        XCTAssertEqual(sut.outdatedCount, 0)
    }

    /// A deleted .deb re-fetched under the same name must not inherit the old
    /// file's badge — `updates` is keyed by filename, and the file behind that
    /// key changed.
    @MainActor
    func test_reloading_the_library_drops_answers_for_files_that_are_gone() async throws {
        let path = NSTemporaryDirectory() + "tbupd_\(UUID().uuidString).sh"
        // `--tweaks` lists only b.deb; `--tweak-updates` badges both.
        let check = [
            line("a.deb", installed: "1.0", latest: "2.0", available: true),
            line("b.deb", installed: "1.0", latest: "2.0", available: true),
            #"{"type":"tweak-updates-end","checked":2,"outdated":2,"unchecked":0}"#,
        ].joined(separator: "\n")
        let script = """
        #!/bin/sh
        for arg in "$@"; do
          case "$arg" in
            --tweaks) printf '%s\\n' '{"type":"tweak","filename":"b.deb","size_bytes":1,"used_by":[]}'; exit 0 ;;
            --tweak-updates) printf '%s\\n' '\(check)'; exit 0 ;;
          esac
        done
        exit 1
        """
        try script.write(toFile: path, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: path)
        defer { try? FileManager.default.removeItem(atPath: path) }

        let sut = TweaksStore(runner: CLIRunner(executable: URL(fileURLWithPath: path)))
        await sut.checkForUpdates()
        XCTAssertEqual(sut.outdatedCount, 2)

        _ = try await sut.loadTweaks()

        XCTAssertNil(
            sut.update(for: .init(filename: "a.deb", sizeBytes: 0, usedBy: [])),
            "a file that is no longer in the library kept its badge"
        )
        XCTAssertEqual(sut.outdatedCount, 1)
    }

    /// Offline, every lookup fails, `outdated` is 0 — and the pane must not
    /// say the library is up to date on the strength of that.
    @MainActor
    func test_unchecked_lookups_are_carried_through_to_the_summary() async throws {
        let check = [
            line("a.deb", installed: "1.0", latest: nil, available: false),
            #"{"type":"tweak-updates-end","checked":1,"outdated":0,"unchecked":1}"#,
        ].joined(separator: "\n")
        let (runner, cleanup) = try stub(check: check, updated: "")
        defer { cleanup() }
        let sut = TweaksStore(runner: runner)

        await sut.checkForUpdates()

        XCTAssertEqual(sut.lastCheck?.unchecked, 1)
        XCTAssertEqual(sut.outdatedCount, 0)
    }
}
