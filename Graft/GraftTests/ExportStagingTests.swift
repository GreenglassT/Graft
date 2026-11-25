import XCTest
@testable import Graft

/// Export IPA…'s staging folder, and the app's placement of the CLI's file
/// where the user chose (EX1).
///
/// The CLI writes into a folder of the export's own, and the app, which holds
/// the Save panel's grant, moves the file into place: a hidden temp file the
/// CLI made beside a file in Desktop or Documents could raise a privacy
/// prompt mid-build. Which file operation runs is checked against recorded
/// fakes; what the file ends up as (its bytes and mode) against the real
/// FileManager, in a temp folder of this test's own.
final class ExportStagingTests: XCTestCase {
    private var root: URL!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appendingPathComponent("graft-export-tests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: root)
    }

    private var destination: URL { root.appendingPathComponent("Example 1.0.ipa") }

    // MARK: - recorded file operations

    private struct Refused: LocalizedError {
        var errorDescription: String? { "refused by the test" }
    }

    private final class Calls {
        var made: [(name: String, urls: [URL])] = []
        var names: [String] { made.map(\.name) }
        var onRemove: ((URL) -> Void)?
    }

    /// File operations that do nothing but say what was asked, failing the
    /// ones named in `failing`.
    private func recorded(
        _ calls: Calls, exists: Bool = false, failing: Set<String> = []
    ) -> ExportFileOps {
        let replacement = root.appendingPathComponent("NSIRD_Graft_test", isDirectory: true)
        let temp = root.appendingPathComponent("tmp", isDirectory: true)
        func step(_ name: String, _ urls: URL...) throws {
            calls.made.append((name, urls))
            if failing.contains(name) { throw Refused() }
        }
        return ExportFileOps(
            replacementDirectory: { _ in try step("replacementDirectory"); return replacement },
            temporaryDirectory: { temp },
            createDirectory: { try step("createDirectory", $0) },
            fileExists: { _ in exists },
            replaceItem: { try step("replaceItem", $0, $1) },
            moveItem: { try step("moveItem", $0, $1) },
            copyItem: { try step("copyItem", $0, $1) },
            removeItem: { url in
                calls.onRemove?(url)
                try step("removeItem", url)
            }
        )
    }

    // MARK: - where the CLI writes

    func test_it_stages_in_the_folder_macos_gives_for_replacing_a_file() throws {
        let calls = Calls()

        let staging = try ExportStaging.prepare(for: destination, ops: recorded(calls))

        XCTAssertEqual(staging.folder.lastPathComponent, "NSIRD_Graft_test")
        XCTAssertFalse(staging.copies)
        XCTAssertEqual(staging.stagedFile, staging.folder.appendingPathComponent("Example 1.0.ipa"))
        XCTAssertEqual(calls.names, ["replacementDirectory"])
    }

    /// Some network and FAT volumes have no replacement directory: a temp
    /// folder of the export's own instead, and the file is copied into place.
    func test_without_a_replacement_directory_it_makes_a_temp_folder_and_copies() throws {
        let calls = Calls()

        let staging = try ExportStaging.prepare(
            for: destination, ops: recorded(calls, failing: ["replacementDirectory"])
        )

        XCTAssertTrue(staging.copies)
        XCTAssertEqual(staging.folder.deletingLastPathComponent().lastPathComponent, "tmp")
        XCTAssertTrue(staging.folder.lastPathComponent.hasPrefix("Graft Export "))
        XCTAssertEqual(calls.names, ["replacementDirectory", "createDirectory"])
        XCTAssertEqual(calls.made.last?.urls, [staging.folder])
    }

    /// Never a silent no-op: an export with nowhere to be built says so.
    func test_when_no_folder_can_be_made_it_says_so() {
        let calls = Calls()

        XCTAssertThrowsError(try ExportStaging.prepare(
            for: destination,
            ops: recorded(calls, failing: ["replacementDirectory", "createDirectory"])
        )) { error in
            let said = error.localizedDescription
            XCTAssertTrue(said.hasPrefix("Couldn't export Example 1.0.ipa:"), said)
            XCTAssertTrue(said.contains("refused by the test"), said)
        }
    }

    // MARK: - moving it into place

    func test_nothing_there_is_a_move() throws {
        let calls = Calls()
        let staging = try ExportStaging.prepare(for: destination, ops: recorded(calls))
        calls.made = []

        let replaced = try staging.place()

        XCTAssertFalse(replaced)
        XCTAssertEqual(calls.names, ["moveItem"])
        XCTAssertEqual(calls.made.first?.urls, [staging.stagedFile, destination])
    }

    /// The Save panel asked before Replace, so the app replaces, and sends
    /// the CLI no `--overwrite`.
    func test_a_file_there_is_a_replace() throws {
        let calls = Calls()
        let staging = try ExportStaging.prepare(
            for: destination, replacing: true, ops: recorded(calls, exists: true)
        )
        calls.made = []

        let replaced = try staging.place()

        XCTAssertTrue(replaced)
        XCTAssertEqual(calls.names, ["replaceItem"])
        XCTAssertEqual(calls.made.first?.urls, [destination, staging.stagedFile])
    }

    /// A file that appeared at the destination while the build ran, with no
    /// Replace in the Save panel, is left alone, and nothing is saved.
    func test_a_file_that_appeared_while_building_is_left_alone() throws {
        let calls = Calls()
        let staging = try ExportStaging.prepare(for: destination, ops: recorded(calls, exists: true))
        calls.made = []

        XCTAssertThrowsError(try staging.place()) { error in
            XCTAssertTrue(error.localizedDescription.contains("left it alone"), error.localizedDescription)
        }
        XCTAssertEqual(calls.names, [], "no replace, move or copy over their file")
    }

    /// Replace was chosen, but the old file was gone by the time the build
    /// finished: a move, and nothing said to be replaced.
    func test_replace_chosen_with_the_file_gone_is_a_move() throws {
        let calls = Calls()
        let staging = try ExportStaging.prepare(for: destination, replacing: true, ops: recorded(calls))
        calls.made = []

        XCTAssertFalse(try staging.place())
        XCTAssertEqual(calls.names, ["moveItem"])
    }

    /// On disk: a file written at the destination mid-build keeps its
    /// contents.
    func test_a_real_file_that_appeared_keeps_its_contents() throws {
        let staging = try ExportStaging.prepare(for: destination)
        defer { staging.discard() }
        try writeStaged(staging, "ours")
        try Data("theirs".utf8).write(to: destination)

        XCTAssertThrowsError(try staging.place())
        XCTAssertEqual(try String(contentsOf: destination, encoding: .utf8), "theirs")
    }

    /// Without Graft's settings there's no telling its own folders, so no
    /// export.
    func test_without_settings_an_export_is_refused() {
        XCTAssertEqual(ExportStaging.refusal(for: destination, paths: nil), ExportStaging.noSettings)
    }

    func test_the_fallback_copies_in_beside_then_moves_or_replaces() throws {
        for exists in [false, true] {
            let calls = Calls()
            let staging = try ExportStaging.prepare(
                for: destination, replacing: exists,
                ops: recorded(calls, exists: exists, failing: ["replacementDirectory"])
            )
            calls.made = []

            try staging.place()

            XCTAssertEqual(calls.names, ["copyItem", exists ? "replaceItem" : "moveItem"])
            let beside = try XCTUnwrap(calls.made.first?.urls.last)
            XCTAssertEqual(beside.deletingLastPathComponent(), destination.deletingLastPathComponent())
            XCTAssertTrue(beside.lastPathComponent.hasPrefix(".Example 1.0.ipa.graft-"))
            XCTAssertEqual(calls.made.first?.urls.first, staging.stagedFile)
            XCTAssertEqual(
                calls.made.last?.urls,
                exists ? [destination, beside] : [beside, destination]
            )
        }
    }

    func test_a_failed_copy_leaves_the_old_file_and_no_copy() throws {
        let calls = Calls()
        let staging = try ExportStaging.prepare(
            for: destination, replacing: true,
            ops: recorded(calls, exists: true, failing: ["replacementDirectory", "copyItem"])
        )
        calls.made = []

        XCTAssertThrowsError(try staging.place()) { error in
            XCTAssertTrue(error.localizedDescription.contains(self.destination.path))
        }
        XCTAssertEqual(calls.names, ["copyItem", "removeItem"], "no replace over the old file")
    }

    func test_a_failed_move_says_where_the_file_was_going() throws {
        let calls = Calls()
        let staging = try ExportStaging.prepare(
            for: destination, ops: recorded(calls, failing: ["moveItem"])
        )

        XCTAssertThrowsError(try staging.place()) { error in
            XCTAssertEqual(
                error.localizedDescription,
                "The IPA was built, but Graft couldn't save it as \(self.destination.path). "
                    + "refused by the test"
            )
        }
    }

    // MARK: - the real thing, in a temp folder

    private func mode(_ url: URL) throws -> Int {
        try XCTUnwrap(
            FileManager.default.attributesOfItem(atPath: url.path)[.posixPermissions] as? Int
        )
    }

    /// The file as the CLI leaves it: `0666 & ~umask` (EX3), 0644 here.
    private func writeStaged(_ staging: ExportStaging, _ text: String, mode: Int = 0o644) throws {
        try Data(text.utf8).write(to: staging.stagedFile)
        try FileManager.default.setAttributes(
            [.posixPermissions: mode], ofItemAtPath: staging.stagedFile.path
        )
    }

    func test_a_real_export_is_moved_into_place_with_the_clis_mode() throws {
        let staging = try ExportStaging.prepare(for: destination)
        defer { staging.discard() }
        XCTAssertFalse(staging.copies, "this volume has a replacement directory")
        XCTAssertFalse(
            staging.folder.path.hasPrefix(root.path), "staged in the folder macOS gave, not beside"
        )
        try writeStaged(staging, "new")

        XCTAssertFalse(try staging.place())

        XCTAssertEqual(try String(contentsOf: destination, encoding: .utf8), "new")
        XCTAssertEqual(try mode(destination), 0o644)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: root.path), [
            "Example 1.0.ipa",
        ])
    }

    /// `replaceItemAt` keeps the old file's permissions unless told not to,
    /// which would undo EX3 for every Replace.
    func test_a_replace_keeps_the_clis_mode_not_the_old_files() throws {
        try Data("old".utf8).write(to: destination)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: destination.path)
        let staging = try ExportStaging.prepare(for: destination, replacing: true)
        defer { staging.discard() }
        try writeStaged(staging, "new")

        XCTAssertTrue(try staging.place())

        XCTAssertEqual(try String(contentsOf: destination, encoding: .utf8), "new")
        XCTAssertEqual(try mode(destination), 0o644)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: root.path), [
            "Example 1.0.ipa",
        ])
    }

    func test_the_fallbacks_copy_keeps_the_clis_mode_too() throws {
        var ops = ExportFileOps.live
        ops.replacementDirectory = { _ in throw Refused() }
        let temp = root.appendingPathComponent("tmp", isDirectory: true)
        ops.temporaryDirectory = { temp }
        for exists in [false, true] {
            if exists {
                try Data("old".utf8).write(to: destination)
                try FileManager.default.setAttributes(
                    [.posixPermissions: 0o600], ofItemAtPath: destination.path
                )
            }
            let staging = try ExportStaging.prepare(for: destination, replacing: exists, ops: ops)
            XCTAssertTrue(staging.copies)
            try writeStaged(staging, "new", mode: 0o640)

            XCTAssertEqual(try staging.place(), exists)
            staging.discard()

            XCTAssertEqual(try String(contentsOf: destination, encoding: .utf8), "new")
            XCTAssertEqual(try mode(destination), 0o640)
            XCTAssertFalse(FileManager.default.fileExists(atPath: staging.folder.path))
            XCTAssertEqual(
                try FileManager.default.contentsOfDirectory(atPath: root.path).sorted(),
                ["Example 1.0.ipa", "tmp"], "no copy left beside it"
            )
            try FileManager.default.removeItem(at: destination)
        }
    }

    func test_discard_removes_the_folder_and_whatever_is_left_in_it() throws {
        let staging = try ExportStaging.prepare(for: destination)
        try writeStaged(staging, "never placed")

        staging.discard()

        XCTAssertFalse(FileManager.default.fileExists(atPath: staging.folder.path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: destination.path))
    }

    // MARK: - finishing a run

    private func exported(path: String) -> IPAExportedRecord {
        IPAExportedRecord(
            app: "example", version: "1.0", path: path, filename: "Example 1.0.ipa",
            sizeBytes: 4096, tweaks: ["some-tweak.deb"], replaced: false, dryRun: false
        )
    }

    /// The confirmation, Show in Finder and the transcript name the file
    /// where the user chose it, not the staging folder the CLI named.
    func test_finish_places_the_file_and_names_it_where_it_went() throws {
        let calls = Calls()
        let staging = try ExportStaging.prepare(
            for: destination, replacing: true, ops: recorded(calls, exists: true)
        )
        let events: [BuildEvent] = [.ipaExported(exported(path: staging.stagedFile.path))]

        let placed = try staging.finish(events: events).get()

        XCTAssertEqual(placed.path, destination.path)
        XCTAssertEqual(placed.filename, "Example 1.0.ipa")
        XCTAssertTrue(placed.replaced)
        XCTAssertEqual(placed.sizeBytes, 4096)
        XCTAssertEqual(calls.names.last, "replaceItem")
        XCTAssertEqual(
            AppDetailView.exportedRecord(in: events + [.ipaExported(placed)])?.path,
            destination.path, "the pane's card reads the placed record"
        )
    }

    func test_a_run_that_never_said_what_it_wrote_places_nothing() throws {
        let calls = Calls()
        let staging = try ExportStaging.prepare(for: destination, ops: recorded(calls))
        calls.made = []

        XCTAssertThrowsError(try staging.finish(events: []).get()) { error in
            XCTAssertTrue(error.localizedDescription.contains("nothing was saved"))
        }
        XCTAssertEqual(calls.names, [])
    }

    /// The CLI's `ipa-exported` names the staging folder and its successful
    /// `build-end` would paint "done" before the file is in place; both wait.
    /// A failure, a stop and every stage go through as they come.
    func test_what_waits_for_the_file_to_be_placed() {
        let record = exported(path: "/tmp/Example 1.0.ipa")
        XCTAssertTrue(ExportStaging.holdsUntilPlaced(.exported(record)))
        XCTAssertTrue(ExportStaging.holdsUntilPlaced(.finished(ok: true, error: nil, elapsed: 3)))
        XCTAssertFalse(ExportStaging.holdsUntilPlaced(.finished(ok: false, error: "x", elapsed: 3)))
        XCTAssertFalse(ExportStaging.holdsUntilPlaced(.stopped(elapsed: 3)))
        XCTAssertFalse(ExportStaging.holdsUntilPlaced(.stage(name: "export", status: "end")))
    }

    // MARK: - launching it

    @MainActor
    private func makeStore() -> ConfigStore {
        ConfigStore(
            runner: CLIRunner(executable: URL(fileURLWithPath: "/usr/bin/true")), notify: { _ in }
        )
    }

    /// The slot can be taken while the Save panel is up. `launchBuild` then
    /// returned false and Export IPA… did nothing at all.
    @MainActor
    func test_a_taken_build_slot_says_why_and_removes_the_folder() throws {
        let store = makeStore()
        XCTAssertTrue(store.beginDeviceCheck(appName: "other"))
        let calls = Calls()
        let staging = try ExportStaging.prepare(for: destination, ops: recorded(calls))
        var ran = false

        let launched = store.launchExport(
            appName: "example", delivery: .unknown, staging: staging
        ) { ran = true }

        XCTAssertFalse(launched)
        XCTAssertFalse(ran)
        XCTAssertEqual(
            store.lastError,
            "Export IPA didn't start for example: Checking the device — wait for it to finish, "
                + "then export again."
        )
        XCTAssertEqual(calls.made.last?.name, "removeItem")
        XCTAssertEqual(calls.made.last?.urls, [staging.folder])
    }

    @MainActor
    func test_another_build_running_is_named() {
        let store = makeStore()
        XCTAssertTrue(store.launchBuild(appName: "other", delivery: .unknown) {})

        XCTAssertFalse(store.launchExport(appName: "example", delivery: .unknown, staging: nil) {})

        XCTAssertEqual(
            store.lastError,
            "Export IPA didn't start for example: other is building — wait for it to finish, "
                + "then export again."
        )
    }

    /// Placed or not, the folder goes once the run is over.
    @MainActor
    func test_the_folder_goes_when_the_run_ends_placed_or_failed() async throws {
        for places in [true, false] {
            let store = makeStore()
            let calls = Calls()
            let staging = try ExportStaging.prepare(for: destination, ops: recorded(calls))
            let removed = expectation(description: "folder removed")
            calls.onRemove = { if $0 == staging.folder { removed.fulfill() } }

            XCTAssertTrue(store.launchExport(
                appName: "example", delivery: .unknown, staging: staging
            ) {
                if places { _ = try? staging.place() }
            })

            await fulfillment(of: [removed], timeout: 5)
            XCTAssertEqual(calls.names.last, "removeItem")
            XCTAssertEqual(calls.names.contains("moveItem"), places)
        }
    }

    @MainActor
    func test_the_folder_goes_when_the_export_is_stopped() async throws {
        let store = makeStore()
        let calls = Calls()
        let staging = try ExportStaging.prepare(for: destination, ops: recorded(calls))
        let running = expectation(description: "the run started")
        let removed = expectation(description: "folder removed")
        calls.onRemove = { if $0 == staging.folder { removed.fulfill() } }

        store.launchExport(appName: "example", delivery: .unknown, staging: staging) {
            running.fulfill()
            try? await Task.sleep(nanoseconds: 30_000_000_000)
        }
        await fulfillment(of: [running], timeout: 5)
        XCTAssertFalse(calls.names.contains("removeItem"), "removed while still running")

        store.cancelBuild(appName: "example")

        await fulfillment(of: [removed], timeout: 5)
        XCTAssertFalse(calls.names.contains("moveItem"), "a stopped export places nothing")
    }

    // MARK: - Graft's own folders

    /// The CLI refuses them, but it never sees the user's path now; the app
    /// asks, by file identity, so another spelling is the same folder (EX2).
    func test_graft_folders_are_refused_however_spelled() throws {
        let cache = root.appendingPathComponent("graft/ipas", isDirectory: true)
        let built = root.appendingPathComponent("graft/built", isDirectory: true)
        for folder in [cache.appendingPathComponent("sub"), built] {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        }
        func refusal(_ folder: String) -> String? {
            ExportStaging.refusal(
                for: root.appendingPathComponent(folder).appendingPathComponent("x.ipa"),
                cacheDir: cache.path, builtDir: built.path
            )
        }

        XCTAssertEqual(
            refusal("graft/ipas"),
            "Export IPA won't save in \(cache.path): it's Graft's own folder for staged IPAs, "
                + "which Graft cleans up itself. Choose another folder."
        )
        XCTAssertNotNil(refusal("graft/ipas/sub"))
        XCTAssertTrue(refusal("graft/built")?.contains("archived builds") == true)
        if FileManager.default.fileExists(atPath: root.appendingPathComponent("graft/IPAS").path) {
            XCTAssertNotNil(refusal("graft/IPAS"), "the same folder in other letter case")
            XCTAssertNotNil(refusal("GRAFT/IPAS/SUB"))
        }
        // Graft's folder as config.yaml may spell it: through a link, or in
        // other letter case. A destination's own path resolves to the disk's
        // spelling, so only identity tells these apart.
        let linked = root.appendingPathComponent("linked-cache")
        try FileManager.default.createSymbolicLink(at: linked, withDestinationURL: cache)
        XCTAssertNotNil(ExportStaging.refusal(
            for: cache.appendingPathComponent("x.ipa"), cacheDir: linked.path, builtDir: built.path
        ), "Graft's folder named through a link")
        let upper = root.appendingPathComponent("GRAFT/IPAS")
        if FileManager.default.fileExists(atPath: upper.path) {
            XCTAssertNotNil(ExportStaging.refusal(
                for: cache.appendingPathComponent("x.ipa"), cacheDir: upper.path, builtDir: built.path
            ), "Graft's folder named in other letter case")
        }
        try FileManager.default.createDirectory(
            at: root.appendingPathComponent("graft/ipas-exports"), withIntermediateDirectories: true
        )
        XCTAssertNil(refusal("graft/ipas-exports"), "beside it is the user's")
        XCTAssertNil(refusal(""))
    }

    func test_a_graft_folder_that_isnt_there_holds_nothing() {
        XCTAssertNil(ExportStaging.refusal(
            for: destination,
            cacheDir: root.appendingPathComponent("none").path,
            builtDir: root.appendingPathComponent("nothing").path
        ))
    }
}
