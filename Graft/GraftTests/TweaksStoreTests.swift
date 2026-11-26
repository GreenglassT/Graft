import XCTest
@testable import Graft

final class TweaksStoreTests: XCTestCase {
    @MainActor
    func test_loadTweaks_returns_records_from_cli_output() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let script = #"""
        printf '{"type":"tweak","filename":"a-tweak.deb","size_bytes":97270,"used_by":["example"]}\n{"type":"tweak","filename":"unused.deb","size_bytes":50,"used_by":[]}\n'
        """#
        let store = TweaksStore(runner: runner, cliArgs: ["-c", script])
        let tweaks = try await store.loadTweaks()
        XCTAssertEqual(tweaks.count, 2)
        XCTAssertEqual(tweaks.first(where: { $0.filename == "a-tweak.deb" })?.usedBy, ["example"])
        XCTAssertTrue(tweaks.first(where: { $0.filename == "unused.deb" })?.usedBy.isEmpty == true)
    }

    /// The Tweaks section re-runs `loadTweaks()` on every entry (ContentView),
    /// because attaching a tweak in the Apps section mutates `used_by` and
    /// nothing recomputes it: `ConfigStore`'s attach and detach rewrite
    /// config.yaml and reload the *apps*, and the field lives on this store's
    /// records. (It used to be a different store instance as well; the app pane
    /// is handed this one now, which changes who holds the stale field but not
    /// that it is stale.) The reload only helps if it *replaces* the cached rows
    /// — a row that kept `used_by: []` would still read as an orphan and still
    /// offer a Trash confirmation the CLI is going to refuse.
    @MainActor
    func test_loadTweaks_replaces_stale_rows_with_fresh_used_by() async throws {
        let path = FileManager.default.temporaryDirectory
            .appendingPathComponent("tweaks-\(UUID().uuidString).jsonl")
        defer { try? FileManager.default.removeItem(at: path) }
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = TweaksStore(runner: runner, cliArgs: ["-c", "cat '\(path.path)'"])

        try #"{"type":"tweak","filename":"some-tweak.deb","size_bytes":10,"used_by":[]}"#
            .write(to: path, atomically: true, encoding: .utf8)
        _ = try await store.loadTweaks()
        XCTAssertEqual(store.tweaks.first?.usedBy, [], "starts life as an orphan")

        // Somebody attached it elsewhere — the Apps section, or another window.
        try #"{"type":"tweak","filename":"some-tweak.deb","size_bytes":10,"used_by":["example"]}"#
            .write(to: path, atomically: true, encoding: .utf8)
        _ = try await store.loadTweaks()

        XCTAssertEqual(store.tweaks.count, 1, "a reload replaces, it doesn't append")
        XCTAssertEqual(store.tweaks.first?.usedBy, ["example"],
                       "the row must stop rendering as an orphan after a reload")
    }

    @MainActor
    func test_deleteTweak_returns_record_and_reloads() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let deleteScript = #"""
        printf '{"type":"tweak-deleted","filename":"orphan.deb","trash_path":"/Users/x/.Trash/orphan.deb"}\n'
        """#
        let reloadScript = #"""
        printf '{"type":"tweak","filename":"kept.deb","size_bytes":10,"used_by":["example"]}\n'
        """#
        let store = TweaksStore(runner: runner, cliArgs: ["-c", reloadScript])

        let record = try await store.deleteTweak(
            filename: "orphan.deb",
            argsOverride: ["-c", deleteScript]
        )

        XCTAssertEqual(record?.filename, "orphan.deb")
        XCTAssertEqual(record?.trashPath, "/Users/x/.Trash/orphan.deb")
        XCTAssertEqual(store.tweaks.map { $0.filename }, ["kept.deb"],
                       "the list must refresh so the deleted row disappears")
    }

    @MainActor
    func test_deleteTweak_throws_with_cli_stderr_when_in_use() async {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        // Exit 16 == TweakInUseError.
        let deleteScript = #"""
        printf 'some-tweak.deb is attached to: example\n' >&2; exit 16
        """#
        let store = TweaksStore(runner: runner)

        do {
            _ = try await store.deleteTweak(
                filename: "some-tweak.deb",
                argsOverride: ["-c", deleteScript]
            )
            XCTFail("expected a throw for an in-use tweak")
        } catch {
            XCTAssertTrue(
                error.localizedDescription.contains("attached to: example"),
                "got: \(error.localizedDescription)"
            )
        }
    }

    /// The same refusal as above, read the way a view should read it. The
    /// test above matches the CLI's prose because that prose is what the user
    /// sees; deciding *what kind* of failure this is shouldn't need a
    /// substring match, and with `CLIError` it doesn't — the exit status
    /// survives the throw, so "still attached" is distinguishable from any
    /// other non-zero exit by `CLIContract.Exit.tweakInUse`.
    @MainActor
    func test_deleteTweak_carries_the_cli_exit_status() async {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let deleteScript = #"""
        printf 'some-tweak.deb is attached to: example\n' >&2; exit 16
        """#
        let store = TweaksStore(runner: runner)

        do {
            _ = try await store.deleteTweak(
                filename: "some-tweak.deb",
                argsOverride: ["-c", deleteScript]
            )
            XCTFail("expected a throw for an in-use tweak")
        } catch let error as CLIError {
            XCTAssertEqual(error.exitCode, CLIContract.Exit.tweakInUse)
        } catch {
            XCTFail("expected a CLIError, got \(type(of: error)): \(error)")
        }
    }

    @MainActor
    func test_deleteTweak_reports_reload_failure_without_throwing() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let deleteScript = #"printf '{"type":"tweak-deleted","filename":"o.deb","trash_path":"/t/o.deb"}\n'"#
        let store = TweaksStore(runner: runner, cliArgs: ["-c", "printf 'boom\n' >&2; exit 1"])

        let record = try await store.deleteTweak(filename: "o.deb", argsOverride: ["-c", deleteScript])

        XCTAssertEqual(record?.filename, "o.deb", "the delete itself succeeded")
        XCTAssertTrue(store.lastError?.contains("couldn't refresh") == true, "got: \(store.lastError ?? "nil")")
    }
}

// MARK: - The refusal, as data

extension TweaksStoreTests {

    /// `runEvents` checks the exit status before returning anything, so on a
    /// refusal — a non-zero exit by design — everything the CLI said on its way
    /// out used to be discarded, and the view recovered the app list by finding
    /// "is attached to:" in stderr. `deleteTweak` reads the events first.
    @MainActor
    func test_a_refused_delete_throws_the_apps_the_cli_named() async throws {
        let script = #"""
        printf '{"type":"tweak-in-use","filename":"some-tweak.deb","used_by":["example","delta"]}\n'
        exit 16
        """#
        let store = TweaksStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")), cliArgs: ["-c", "true"])
        do {
            _ = try await store.deleteTweak(filename: "some-tweak.deb", argsOverride: ["-c", script])
            XCTFail("expected the refusal to throw")
        } catch let refusal as TweaksStore.TweakInUse {
            XCTAssertEqual(refusal.record.filename, "some-tweak.deb")
            XCTAssertEqual(refusal.record.usedBy, ["example", "delta"])
        }
    }

    /// An older `graft` exits 16 without the event. That must still be a
    /// distinguishable refusal, not a generic failure — the exit code is the
    /// contract that predates the event.
    @MainActor
    func test_an_older_cli_still_reports_a_refusal_by_exit_code() async throws {
        let script = #"""
        printf 'error: some-tweak.deb is attached to: example\n' >&2
        exit 16
        """#
        let store = TweaksStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")), cliArgs: ["-c", "true"])
        do {
            _ = try await store.deleteTweak(filename: "some-tweak.deb", argsOverride: ["-c", script])
            XCTFail("expected a throw")
        } catch let error as CLIError {
            XCTAssertEqual(error.exitCode, CLIContract.Exit.tweakInUse)
        }
    }

    /// A successful delete still returns its record and still reloads.
    @MainActor
    func test_a_successful_delete_still_returns_its_record() async throws {
        let script = #"""
        printf '{"type":"tweak-deleted","filename":"some-tweak.deb","trash_path":"/x/some-tweak.deb"}\n'
        """#
        let reload = #"printf '{"type":"tweak","filename":"other.deb","size_bytes":1,"used_by":[]}\n'"#
        let store = TweaksStore(runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")), cliArgs: ["-c", reload])
        let record = try await store.deleteTweak(
            filename: "some-tweak.deb", argsOverride: ["-c", script]
        )
        XCTAssertEqual(record?.trashPath, "/x/some-tweak.deb")
        XCTAssertEqual(store.tweaks.map { $0.filename }, ["other.deb"], "the list reloaded")
    }
}

// MARK: - hasLoaded, which is the attempt and not the result
//
// `AppDetailView` fills this store once per session and must not re-run
// `--tweaks` on every sidebar click. It used to gate on `tweaks.isEmpty`, which
// answers "did the last load find anything" rather than "has one happened" —
// and the two states below are exactly where those answers part company. The
// pane's own end-to-end cases live in `AppDetailLayoutTests`; these pin the flag
// the pane reads.

extension TweaksStoreTests {

    @MainActor
    private func store(_ script: String) -> TweaksStore {
        TweaksStore(
            runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")),
            cliArgs: ["-c", script]
        )
    }

    @MainActor
    func test_hasLoaded_is_false_until_a_load_finishes() async throws {
        let sut = store(#"printf '{"type":"tweak","filename":"a.deb","size_bytes":1,"used_by":[]}\n'"#)
        XCTAssertFalse(sut.hasLoaded, "nothing has run yet")
        _ = try await sut.loadTweaks()
        XCTAssertTrue(sut.hasLoaded)
    }

    /// A `--tweaks` that exits non-zero — the CLI off PATH, a config.yaml it
    /// cannot parse — is a finished attempt. The list stays empty, so a caller
    /// reading the list would retry it forever.
    @MainActor
    func test_a_failed_load_still_counts_as_an_attempt() async {
        let sut = store("printf 'boom\n' >&2; exit 1")
        do {
            _ = try await sut.loadTweaks()
            XCTFail("expected the fixture to fail")
        } catch {
            // the throw is the point; what it left behind is what is asserted
        }
        XCTAssertTrue(sut.tweaks.isEmpty, "premise: a failure leaves nothing in the list")
        XCTAssertTrue(sut.hasLoaded,
                      "a failed load has still happened — retrying it on every navigation "
                      + "is a doomed subprocess per click")
    }

    /// So is a successful load of an empty directory: a `tweaks_dir` that moved
    /// under a config.yaml still listing tweaks exits 0 with nothing to say.
    @MainActor
    func test_an_empty_library_counts_as_an_attempt() async throws {
        let sut = store("true")
        _ = try await sut.loadTweaks()
        XCTAssertTrue(sut.tweaks.isEmpty, "premise: the library really is empty")
        XCTAssertTrue(sut.hasLoaded, "an empty library is an answer, not a missing one")
    }

    /// **A canceled load is not an attempt.** The pane's load lives in a
    /// `.task(id: app.id)`, torn down the instant another sidebar row is
    /// clicked, and `CLIRunner` reports the SIGTERM'd child as an ordinary
    /// non-zero exit — indistinguishable from the failure above. Latching the
    /// flag off one would leave the library permanently empty with no way back.
    @MainActor
    func test_a_cancelled_load_is_not_an_attempt() async throws {
        let sut = store("sleep 5")
        let task = Task { try await sut.loadTweaks() }
        // Long enough for the child to be running, short enough to be nowhere
        // near the 5s it would take to finish on its own.
        try await Task.sleep(nanoseconds: 200_000_000)
        task.cancel()
        _ = try? await task.value
        XCTAssertFalse(sut.hasLoaded,
                       "a load the user interrupted by clicking elsewhere learned nothing, "
                       + "so the next pane that needs the library must still be allowed to "
                       + "ask for it")
    }
}

/// The Tweaks pane's toolbar at the narrowest the window lets it be.
@MainActor
final class TweaksPaneLayoutTests: XCTestCase {
    /// The filter's placeholder read "…author or filena": too long for the
    /// field it's in. At the window's minimum width, with the sidebar at its
    /// widest, all of it fits.
    func test_the_filter_placeholder_fits_its_field() throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/true"))
        let config = ConfigStore(runner: runner, notify: { _ in })
        let pane = TweaksView(store: TweaksStore(runner: runner), config: config)
        // 900pt window, 360pt sidebar (`navigationSplitViewColumnWidth`'s max).
        let box = hosted(pane, size: NSSize(width: Metrics.windowMinWidth - 360, height: Metrics.windowMinHeight))
        settle(box, seconds: 0.3)

        func fields(in view: NSView) -> [NSTextField] {
            ((view as? NSTextField).map { [$0] } ?? []) + view.subviews.flatMap(fields(in:))
        }
        let all = fields(in: try XCTUnwrap(box.window.contentView))
        let field = try XCTUnwrap(
            all.first { ($0.placeholderString ?? $0.placeholderAttributedString?.string) == TweaksView.filterPlaceholder },
            "no field with the placeholder: \(all.map { $0.placeholderString ?? $0.stringValue })"
        )
        let font = try XCTUnwrap(field.font)
        let needs = (TweaksView.filterPlaceholder as NSString).size(withAttributes: [.font: font]).width
        // The cell's own insets come out of the field's width.
        let room = field.cell?.drawingRect(forBounds: field.bounds).width ?? field.bounds.width
        XCTAssertLessThanOrEqual(needs, room, "the placeholder needs \(needs)pt and the field has \(room)pt")
    }
}
