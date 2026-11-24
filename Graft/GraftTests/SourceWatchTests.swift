import XCTest
@testable import Graft

/// The staleness check that decides whether the app rebuilds itself at launch.
///
/// Worth real tests despite being small: the failure modes are "never notices a
/// change" (you test the previous build and believe it is the new one — the
/// exact bug `graft-install` has three separate guards against) and "always
/// notices", which relaunches the app forever.
final class SourceWatchTests: XCTestCase {

    private var root: URL!

    override func setUpWithError() throws {
        root = URL.temporaryDirectory.appending(path: "sourcewatch-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: root)
    }

    @discardableResult
    private func write(_ name: String, modified: Date) throws -> URL {
        let url = root.appending(path: name)
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true
        )
        try Data("x".utf8).write(to: url)
        try FileManager.default.setAttributes([.modificationDate: modified], ofItemAtPath: url.path)
        return url
    }

    private let built = Date(timeIntervalSince1970: 1_000_000)
    private var older: Date { built.addingTimeInterval(-60) }
    private var newer: Date { built.addingTimeInterval(60) }

    func test_a_source_file_newer_than_the_binary_is_found() throws {
        try write("Views/Thing.swift", modified: newer)
        XCTAssertNotNil(SourceWatch.firstFile(under: [root], newerThan: built))
    }

    func test_source_older_than_the_binary_is_not_stale() throws {
        try write("Views/Thing.swift", modified: older)
        XCTAssertNil(
            SourceWatch.firstFile(under: [root], newerThan: built),
            "an unchanged tree asked for a rebuild, which relaunches the app on every launch"
        )
    }

    /// The rebuild is what makes the binary newer than the source, so equality
    /// is the state immediately after a successful one.
    func test_a_file_exactly_as_old_as_the_binary_is_not_stale() throws {
        try write("Views/Thing.swift", modified: built)
        XCTAssertNil(SourceWatch.firstFile(under: [root], newerThan: built))
    }

    func test_only_the_watched_extensions_count() throws {
        try write("notes.md", modified: newer)
        try write("Assets.xcassets/icon.png", modified: newer)
        XCTAssertNil(
            SourceWatch.firstFile(under: [root], newerThan: built),
            "editing a README rebuilt and relaunched the app"
        )

        try write("Views/Thing.swift", modified: newer)
        XCTAssertNotNil(SourceWatch.firstFile(under: [root], newerThan: built))
    }

    /// Files named individually are checked even though nothing walks their
    /// directory — the mechanism `project.yml` needs, since it sits one level
    /// above the watched root.
    func test_a_named_file_outside_every_root_is_checked() throws {
        let outside = try write("outside/project.yml", modified: newer)
        XCTAssertNil(
            SourceWatch.firstFile(under: [root.appending(path: "elsewhere")], newerThan: built),
            "premise: the file is not reachable by walking the roots"
        )
        XCTAssertNotNil(
            SourceWatch.firstFile(
                under: [root.appending(path: "elsewhere")], newerThan: built, files: [outside]
            ),
            "project.yml decides what XcodeGen builds, so it changes the binary"
        )
    }

    /// The paths in `watchedRoots`/`watchedFiles` name things that really
    /// exist, resolved from this test file's own location.
    ///
    /// This is the assertion that was missing. `.yml` sat in
    /// `watchedExtensions` for a file that no root could reach, so editing
    /// `project.yml` rebuilt nothing — and the extension test above passed
    /// because it wrote its own `project.yml` into the temp directory it was
    /// scanning, a place production can never look.
    func test_the_watched_paths_exist_in_this_repo() {
        // <repo>/Graft/GraftTests/SourceWatchTests.swift
        let repo = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // GraftTests
            .deletingLastPathComponent()   // Graft
            .deletingLastPathComponent()   // <repo>

        for path in SourceWatch.watchedRoots {
            var isDir: ObjCBool = false
            let url = repo.appending(path: path)
            XCTAssertTrue(
                FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir)
                    && isDir.boolValue,
                "watchedRoots names \(path), which is not a directory in this repo"
            )
        }

        for path in SourceWatch.watchedFiles {
            let url = repo.appending(path: path)
            XCTAssertTrue(
                FileManager.default.fileExists(atPath: url.path),
                "watchedFiles names \(path), which does not exist in this repo"
            )
            XCTAssertTrue(
                SourceWatch.watchedExtensions.contains(url.pathExtension),
                "\(path) is watched but its extension is not, so it is never checked"
            )
        }
    }

    /// Every watched extension is reachable by something — the check that
    /// `.yml` failed, silently, for the life of the feature.
    func test_every_watched_extension_can_actually_match_something() {
        let repo = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()

        var reachable = Set(SourceWatch.watchedFiles.map {
            repo.appending(path: $0).pathExtension
        })
        for root in SourceWatch.watchedRoots {
            let walk = FileManager.default.enumerator(
                at: repo.appending(path: root), includingPropertiesForKeys: nil,
                options: [.skipsHiddenFiles]
            )
            while let url = walk?.nextObject() as? URL {
                if SourceWatch.watchedExtensions.contains(url.pathExtension) {
                    reachable.insert(url.pathExtension)
                }
            }
        }

        let decorative = SourceWatch.watchedExtensions.subtracting(reachable).sorted()
        XCTAssertTrue(
            decorative.isEmpty,
            """
            \(decorative) are in watchedExtensions but no watched root or file has \
            that extension, so editing such a file rebuilds nothing. Either point \
            a root/file at it or drop it from the set.
            """
        )
    }

    /// A source file dated in the future is one no rebuild can ever satisfy:
    /// the new binary is stamped *now*, which is still older. Treating it as
    /// stale is a rebuild-and-relaunch cycle with no end.
    func test_a_source_file_dated_in_the_future_is_not_stale() throws {
        try write("Views/Thing.swift", modified: built.addingTimeInterval(86_400))
        XCTAssertNil(
            SourceWatch.firstFile(under: [root], newerThan: built, now: built),
            "a file the rebuild can never overtake started an endless rebuild loop"
        )
    }

    func test_nested_directories_are_searched() throws {
        try write("a/b/c/Deep.swift", modified: newer)
        XCTAssertNotNil(SourceWatch.firstFile(under: [root], newerThan: built))
    }

    /// A repo that has moved or lives on an unmounted volume must read as
    /// "nothing to rebuild" — the app still runs, it just stops self-updating.
    func test_a_missing_root_is_not_an_error() {
        let gone = root.appending(path: "does-not-exist")
        XCTAssertNil(SourceWatch.firstFile(under: [gone], newerThan: built))
    }

    func test_a_later_root_is_searched_when_an_earlier_one_is_missing() throws {
        try write("real/Thing.swift", modified: newer)
        let roots = [root.appending(path: "nope"), root.appending(path: "real")]
        XCTAssertNotNil(SourceWatch.firstFile(under: roots, newerThan: built))
    }

    // MARK: - The loop guard

    func test_a_fresh_marker_suppresses_another_attempt() throws {
        let marker = try write("marker", modified: built)
        XCTAssertTrue(
            SourceWatch.recentlyAttempted(within: 180, now: built.addingTimeInterval(10),
                                          marker: marker)
        )
    }

    func test_an_old_marker_allows_another_attempt() throws {
        let marker = try write("marker", modified: built)
        XCTAssertFalse(
            SourceWatch.recentlyAttempted(within: 180, now: built.addingTimeInterval(600),
                                          marker: marker)
        )
    }

    func test_no_marker_at_all_allows_an_attempt() {
        XCTAssertFalse(
            SourceWatch.recentlyAttempted(marker: root.appending(path: "never-written"))
        )
    }

    /// A clock correction can leave the marker a few seconds ahead of `now`.
    /// The window is symmetric so that still reads as "just attempted" — the
    /// asymmetric version treats it as no attempt at all, which is the
    /// direction that loops.
    func test_a_marker_slightly_ahead_of_now_still_suppresses() throws {
        let marker = try write("marker", modified: built.addingTimeInterval(30))
        XCTAssertTrue(SourceWatch.recentlyAttempted(within: 180, now: built, marker: marker))
    }

    /// A marker far in the future — a restored backup, a badly wrong clock —
    /// permits an attempt rather than suppressing forever. The attempt rewrites
    /// the marker with the real time, so one rebuild repairs the state instead
    /// of every rebuild being blocked until the clock catches up.
    func test_a_marker_far_in_the_future_allows_one_attempt_and_self_heals() throws {
        let marker = try write("marker", modified: built.addingTimeInterval(86_400))
        XCTAssertFalse(SourceWatch.recentlyAttempted(within: 180, now: built, marker: marker))

        SourceWatch.recordAttempt(marker: marker)
        XCTAssertTrue(
            SourceWatch.recentlyAttempted(within: 180, marker: marker),
            "the attempt did not restamp the marker, so the state never corrects"
        )
    }
}

// MARK: - The state machine around it

@MainActor
final class RebuildCoordinatorTests: XCTestCase {

    private func coordinator(
        installer: @escaping () async -> String? = { nil },
        relaunch: @escaping () -> Bool = { true }
    ) -> RebuildCoordinator {
        RebuildCoordinator(runInstaller: installer, relaunch: relaunch)
    }

    func test_a_fresh_build_shows_the_app_and_runs_nothing() {
        var ran = false
        let sut = coordinator(installer: { ran = true; return nil })
        sut.startIfStale(isStale: { false }, recentlyAttempted: { false }, recordAttempt: {}, clearAttempt: {})

        XCTAssertEqual(sut.phase, .ready)
        XCTAssertFalse(ran)
    }

    /// The guard that keeps a project which does not compile from making the
    /// app unopenable: after a failed rebuild the source is *still* newer than
    /// the binary, so staleness alone would relaunch forever.
    func test_a_recent_attempt_stops_a_second_one_even_though_it_is_still_stale() {
        var ran = false
        let sut = coordinator(installer: { ran = true; return nil })
        sut.startIfStale(isStale: { true }, recentlyAttempted: { true }, recordAttempt: {}, clearAttempt: {})

        XCTAssertEqual(sut.phase, .ready)
        XCTAssertFalse(ran, "a failing build relaunched the app in a loop")
    }

    /// Recorded first, so a crash *during* the build still counts as an
    /// attempt rather than being retried on every launch afterward.
    ///
    /// The previous version of this test could not fail: the installer runs in
    /// an unstructured Task, the body never awaited it, so `ranAfterRecording`
    /// was still nil at the assertion and `XCTAssertNotEqual(nil, false)`
    /// passed for any ordering. It now waits for the installer to actually run
    /// and asserts what it observed.
    func test_the_attempt_is_recorded_before_the_build_starts() async {
        var recorded = false
        let observed = expectation(description: "installer ran")
        var recordedWhenInstallerRan: Bool?
        let sut = coordinator(installer: {
            recordedWhenInstallerRan = recorded
            observed.fulfill()
            return nil
        })
        sut.startIfStale(isStale: { true }, recentlyAttempted: { false },
                         recordAttempt: { recorded = true }, clearAttempt: {})

        XCTAssertTrue(recorded)
        await fulfillment(of: [observed], timeout: 2)
        XCTAssertEqual(recordedWhenInstallerRan, true,
                       "the build started before the attempt was recorded")
    }

    /// The guard is per process, not per window: SwiftUI re-runs the `.task`
    /// when a closed window is reopened, and a rebuild quits the app.
    func test_the_check_runs_once_per_process() {
        var checks = 0
        let sut = coordinator()
        for _ in 0..<3 {
            sut.startIfStale(isStale: { checks += 1; return false },
                             recentlyAttempted: { false }, recordAttempt: {}, clearAttempt: {})
        }
        XCTAssertEqual(checks, 1, "reopening a window re-ran the staleness check")
    }

    /// Cleared on success, so the window only throttles repeated *failures*.
    /// Left in place it suppressed every rebuild for three minutes after one
    /// that worked — silently launching the previous binary.
    func test_a_successful_rebuild_clears_the_attempt_marker() async {
        let cleared = expectation(description: "cleared")
        let sut = coordinator(installer: { nil }, relaunch: { true })
        sut.startIfStale(isStale: { true }, recentlyAttempted: { false },
                         recordAttempt: {}, clearAttempt: { cleared.fulfill() })

        await fulfillment(of: [cleared], timeout: 2)
    }

    func test_a_failed_rebuild_keeps_the_attempt_marker() async {
        let sut = coordinator(installer: { "boom" })
        var cleared = false
        sut.startIfStale(isStale: { true }, recentlyAttempted: { false },
                         recordAttempt: {}, clearAttempt: { cleared = true })

        try? await Task.sleep(for: .milliseconds(200))
        XCTAssertFalse(cleared, "clearing after a failure reopens the rebuild loop")
    }

    /// A relaunch that cannot be scheduled must not quit into nothing, and must
    /// not leave the spinner up forever either.
    func test_a_relaunch_that_cannot_be_scheduled_is_reported() async {
        let sut = coordinator(installer: { nil }, relaunch: { false })
        sut.startIfStale(isStale: { true }, recentlyAttempted: { false }, recordAttempt: {}, clearAttempt: {})

        try? await Task.sleep(for: .milliseconds(300))
        guard case let .failed(log) = sut.phase else {
            return XCTFail("expected a reported failure, got \(sut.phase)")
        }
        XCTAssertTrue(log.contains("previous build"))
    }

    func test_a_successful_rebuild_relaunches() async {
        let relaunched = expectation(description: "relaunched")
        let sut = coordinator(installer: { nil }, relaunch: { relaunched.fulfill(); return true })
        sut.startIfStale(isStale: { true }, recentlyAttempted: { false }, recordAttempt: {}, clearAttempt: {})

        await fulfillment(of: [relaunched], timeout: 2)
        XCTAssertEqual(sut.phase, .rebuilding, "the window must not flash back before quitting")
    }

    func test_a_failed_rebuild_reports_the_log_and_does_not_relaunch() async {
        var relaunched = false
        let sut = coordinator(
            installer: { "error: cannot find 'Thing' in scope" },
            relaunch: { relaunched = true; return true }
        )
        sut.startIfStale(isStale: { true }, recentlyAttempted: { false }, recordAttempt: {}, clearAttempt: {})

        try? await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(sut.phase, .failed(log: "error: cannot find 'Thing' in scope"))
        XCTAssertFalse(relaunched, "relaunching into a build that failed changes nothing")
    }

    /// The old build still works — it is only out of date — so a compile error
    /// must not be the thing that stops the app opening.
    func test_the_previous_build_stays_usable_after_a_failure() async {
        let sut = coordinator(installer: { "boom" })
        sut.startIfStale(isStale: { true }, recentlyAttempted: { false }, recordAttempt: {}, clearAttempt: {})
        try? await Task.sleep(for: .milliseconds(200))

        sut.dismissFailure()
        XCTAssertEqual(sut.phase, .ready)
    }
}
