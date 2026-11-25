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
        for folder in locked {
            try? FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: folder.path)
        }
        try? FileManager.default.removeItem(at: root)
    }

    /// Folders `readOnly` locked, opened again before `root` goes.
    private var locked: [URL] = []

    /// `folder`, made and then made read-only: nothing new can be saved in it.
    private func readOnly(_ name: String) throws -> URL {
        let folder = root.appendingPathComponent(name, isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        locked.append(folder)
        try FileManager.default.setAttributes([.posixPermissions: 0o555], ofItemAtPath: folder.path)
        return folder
    }

    /// The live file operations on a volume with no replacement directory:
    /// a temp folder of the export's own in `root/tmp`, copied in beside.
    private func fallbackLive() -> ExportFileOps {
        var ops = ExportFileOps.live
        ops.replacementDirectory = { _ in throw Refused() }
        let temp = root.appendingPathComponent("tmp", isDirectory: true)
        ops.temporaryDirectory = { temp }
        return ops
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

    /// The error placing `staging` fails with; a failure if it places.
    @MainActor
    private func placingFails(
        _ staging: ExportStaging, file: StaticString = #filePath, line: UInt = #line
    ) async throws -> Error {
        do {
            try await staging.place()
        } catch {
            return error
        }
        XCTFail("placed", file: file, line: line)
        throw Refused()
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

    @MainActor
    func test_nothing_there_is_a_move() async throws {
        let calls = Calls()
        let staging = try ExportStaging.prepare(for: destination, ops: recorded(calls))
        calls.made = []

        let replaced = try await staging.place()

        XCTAssertFalse(replaced)
        XCTAssertEqual(calls.names, ["moveItem"])
        XCTAssertEqual(calls.made.first?.urls, [staging.stagedFile, destination])
    }

    /// The Save panel asked before Replace, so the app replaces, and sends
    /// the CLI no `--overwrite`.
    @MainActor
    func test_a_file_there_is_a_replace() async throws {
        let calls = Calls()
        let staging = try ExportStaging.prepare(
            for: destination, replacing: true, ops: recorded(calls, exists: true)
        )
        calls.made = []

        let replaced = try await staging.place()

        XCTAssertTrue(replaced)
        XCTAssertEqual(calls.names, ["replaceItem"])
        XCTAssertEqual(calls.made.first?.urls, [destination, staging.stagedFile])
    }

    /// A file that appeared at the destination while the build ran, with no
    /// Replace in the Save panel, is left alone, and nothing is saved.
    @MainActor
    func test_a_file_that_appeared_while_building_is_left_alone() async throws {
        let calls = Calls()
        let staging = try ExportStaging.prepare(for: destination, ops: recorded(calls, exists: true))
        calls.made = []

        let error = try await placingFails(staging)
        XCTAssertTrue(error.localizedDescription.contains("left it alone"), error.localizedDescription)
        XCTAssertEqual(calls.names, [], "no replace, move or copy over their file")
    }

    /// Replace was chosen, but the old file was gone by the time the build
    /// finished: a move, and nothing said to be replaced.
    @MainActor
    func test_replace_chosen_with_the_file_gone_is_a_move() async throws {
        let calls = Calls()
        let staging = try ExportStaging.prepare(for: destination, replacing: true, ops: recorded(calls))
        calls.made = []

        let replaced = try await staging.place()
        XCTAssertFalse(replaced)
        XCTAssertEqual(calls.names, ["moveItem"])
    }

    /// On disk: a file written at the destination mid-build keeps its
    /// contents.
    @MainActor
    func test_a_real_file_that_appeared_keeps_its_contents() async throws {
        let staging = try ExportStaging.prepare(for: destination)
        defer { staging.discard() }
        try writeStaged(staging, "ours")
        try Data("theirs".utf8).write(to: destination)

        _ = try await placingFails(staging)
        XCTAssertEqual(try String(contentsOf: destination, encoding: .utf8), "theirs")
    }

    /// Without Graft's settings there's no telling its own folders, so no
    /// export.
    func test_without_settings_an_export_is_refused() {
        XCTAssertEqual(ExportStaging.refusal(for: destination, paths: nil), ExportStaging.noSettings)
    }

    @MainActor
    func test_the_fallback_copies_in_beside_then_moves_or_replaces() async throws {
        for exists in [false, true] {
            let calls = Calls()
            let staging = try ExportStaging.prepare(
                for: destination, replacing: exists,
                ops: recorded(calls, exists: exists, failing: ["replacementDirectory"])
            )
            calls.made = []

            try await staging.place()

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

    @MainActor
    func test_a_failed_copy_leaves_the_old_file_and_no_copy() async throws {
        let calls = Calls()
        let staging = try ExportStaging.prepare(
            for: destination, replacing: true,
            ops: recorded(calls, exists: true, failing: ["replacementDirectory", "copyItem"])
        )
        calls.made = []

        let error = try await placingFails(staging)
        XCTAssertTrue(error.localizedDescription.contains(destination.path))
        XCTAssertEqual(calls.names, ["copyItem", "removeItem"], "no replace over the old file")
    }

    @MainActor
    func test_a_failed_move_says_where_the_file_was_going() async throws {
        let calls = Calls()
        let staging = try ExportStaging.prepare(
            for: destination, ops: recorded(calls, failing: ["moveItem"])
        )

        let error = try await placingFails(staging)
        XCTAssertEqual(
            error.localizedDescription,
            "The IPA was built, but Graft couldn't save it as \(destination.path). "
                + "refused by the test"
        )
    }

    // MARK: - the fallback's copy, off the main actor

    /// What a copy saw, written from the copy's thread.
    private final class CopyProbe: @unchecked Sendable {
        var onMain: Bool?
        var released = false
        var sawStop = false
    }

    /// A big IPA copied onto another volume froze the window, Stop with it:
    /// the copy ran on the main actor. It runs off it, and the main actor
    /// carries on while it does.
    @MainActor
    func test_the_fallbacks_copy_runs_off_the_main_actor_and_the_window_stays_live() async throws {
        let calls = Calls()
        let probe = CopyProbe()
        let started = expectation(description: "the copy started")
        let gate = DispatchSemaphore(value: 0)
        var ops = recorded(calls, failing: ["replacementDirectory"])
        ops.copyItem = { _, _ in
            probe.onMain = Thread.isMainThread
            started.fulfill()
            // Let go by the test, which runs on the main actor: on time only
            // if the main actor was free while this copy ran.
            probe.released = gate.wait(timeout: .now() + 5) == .success
        }
        let staging = try ExportStaging.prepare(for: destination, ops: ops)
        calls.made = []

        let placing = Task { try await staging.place() }
        await fulfillment(of: [started], timeout: 5)
        gate.signal()
        let replaced = try await placing.value

        XCTAssertFalse(replaced)
        XCTAssertEqual(probe.onMain, false, "copied on the main thread")
        XCTAssertTrue(probe.released, "the main actor was held for the whole copy")
        XCTAssertEqual(calls.names, ["moveItem"], "then moved into place")
    }

    /// Stop while the file is copied in: the copy is told (`live`'s copyfile
    /// quits between chunks), what it wrote is removed, nothing is placed,
    /// and the stop comes back as a stop.
    @MainActor
    func test_a_stop_during_the_fallbacks_copy_places_nothing_and_leaves_no_copy() async throws {
        let calls = Calls()
        let probe = CopyProbe()
        let started = expectation(description: "the copy started")
        var ops = recorded(calls, exists: true, failing: ["replacementDirectory"])
        ops.copyItem = { from, to in
            calls.made.append(("copyItem", [from, to]))
            started.fulfill()
            // As copyfile's callback does, between chunks.
            let deadline = Date().addingTimeInterval(10)
            while !Task.isCancelled, Date() < deadline { usleep(1000) }
            probe.sawStop = Task.isCancelled
            if Task.isCancelled { throw CancellationError() }
        }
        let staging = try ExportStaging.prepare(for: destination, replacing: true, ops: ops)
        calls.made = []

        let placing = Task { try await staging.place() }
        await fulfillment(of: [started], timeout: 5)
        placing.cancel()
        let ended = await placing.result

        guard case let .failure(error) = ended else { return XCTFail("placed after a stop") }
        XCTAssertTrue(error is CancellationError, "\(error)")
        XCTAssertTrue(probe.sawStop, "the copy wasn't told of the stop")
        XCTAssertEqual(calls.names, ["copyItem", "removeItem"], "no replace over the old file")
        XCTAssertEqual(calls.made.last?.urls, calls.made.first?.urls.last.map { [$0] })
    }

    /// The live copy, stopped: copyfile quits and leaves no partial file.
    @MainActor
    func test_a_stop_ends_a_real_copy_and_leaves_nothing_beside() async throws {
        var ops = ExportFileOps.live
        ops.replacementDirectory = { _ in throw Refused() }
        let temp = root.appendingPathComponent("tmp", isDirectory: true)
        ops.temporaryDirectory = { temp }
        let staging = try ExportStaging.prepare(for: destination, ops: ops)
        defer { staging.discard() }
        try Data(count: 8 << 20).write(to: staging.stagedFile)

        let placing = Task {
            withUnsafeCurrentTask { $0?.cancel() }
            return try await staging.place()
        }
        let ended = await placing.result

        guard case let .failure(error) = ended else { return XCTFail("placed after a stop") }
        XCTAssertTrue(error is CancellationError, "\(error)")
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: root.path), ["tmp"],
                       "no file, and no copy beside where it would have gone")
    }

    /// The live copy in a task already stopped: copyfile asks before its
    /// first chunk, quits, and leaves no file.
    func test_the_live_copy_stops_when_its_task_is_canceled() async throws {
        let from = root.appendingPathComponent("from.ipa")
        let to = root.appendingPathComponent("to.ipa")
        try Data(count: 8 << 20).write(to: from)

        let copied = await Task.detached { () -> Result<Void, Error> in
            withUnsafeCurrentTask { $0?.cancel() }
            return Result { try ExportFileOps.copyStoppably(from: from, to: to) }
        }.value

        guard case let .failure(error) = copied else { return XCTFail("copied in a stopped task") }
        XCTAssertTrue(error is CancellationError, "\(error)")
        XCTAssertFalse(FileManager.default.fileExists(atPath: to.path))
    }

    /// The live copy never writes over a file, and says why it failed as
    /// FileManager does, naming the file being saved.
    func test_the_live_copy_never_writes_over_a_file() throws {
        let from = root.appendingPathComponent("from.ipa")
        try Data("new".utf8).write(to: from)
        try Data("theirs".utf8).write(to: destination)

        XCTAssertThrowsError(try ExportFileOps.copyStoppably(from: from, to: destination)) { error in
            XCTAssertEqual((error as? CocoaError)?.code, .fileWriteFileExists)
            let said = error.localizedDescription
            XCTAssertTrue(said.contains("“Example 1.0.ipa”"), said)
            XCTAssertFalse(said.contains("from.ipa"), said)
        }
        XCTAssertEqual(try String(contentsOf: destination, encoding: .utf8), "theirs")
    }

    /// A real refusal, from a folder the copy can't save in: it names the
    /// file it was saving and that folder. It named the file it read, so a
    /// refused export said "save the file … in the folder “Graft Export
    /// <UUID>”", the staging folder.
    func test_a_refused_live_copy_names_the_file_being_saved_and_its_folder() throws {
        let staged = root.appendingPathComponent("Graft Export TEST", isDirectory: true)
        try FileManager.default.createDirectory(at: staged, withIntermediateDirectories: true)
        let from = staged.appendingPathComponent("Example 1.0.ipa")
        try Data("new".utf8).write(to: from)
        let exports = try readOnly("Exports")

        XCTAssertThrowsError(try ExportFileOps.copyStoppably(
            from: from, to: exports.appendingPathComponent("Example 1.0.ipa")
        )) { error in
            XCTAssertEqual((error as? CocoaError)?.code, .fileWriteNoPermission)
            let said = error.localizedDescription
            XCTAssertTrue(said.contains("“Example 1.0.ipa” in the folder “Exports”"), said)
            XCTAssertFalse(said.contains("Graft Export"), said)
        }
    }

    /// What every failure the copy maps says, as Foundation words it: the
    /// file being saved, and for most its folder. EPERM and the unknown
    /// ones can't be made to happen here, and read the same way.
    func test_every_copy_failure_names_the_file_being_saved() {
        let saving = root.appendingPathComponent("Exports/Example 1.0.ipa")
        let withFolder: [Int32] = [EACCES, EPERM, EEXIST, EIO, EINVAL]
        for code in withFolder + [ENOSPC, EDQUOT, EROFS, ENOENT] {
            let said = ExportFileOps.copyFailure(errno: code, saving: saving).localizedDescription
            XCTAssertTrue(said.contains("“Example 1.0.ipa”"), "\(code): \(said)")
            XCTAssertFalse(said.contains(root.lastPathComponent), "\(code): \(said)")
            if withFolder.contains(code) {
                XCTAssertTrue(said.contains("folder “Exports”"), "\(code): \(said)")
            }
        }
    }

    /// An export to a folder that refuses the copy beside: the error names
    /// the user's file and folder, never the staging folder or the hidden
    /// copy's name.
    @MainActor
    func test_a_refused_fallback_copy_names_the_users_file_and_folder() async throws {
        let exports = try readOnly("Exports")
        let chosen = exports.appendingPathComponent("Example 1.0.ipa")
        let staging = try ExportStaging.prepare(for: chosen, ops: fallbackLive())
        defer { staging.discard() }
        try writeStaged(staging, "new")

        let said = try await placingFails(staging).localizedDescription

        XCTAssertTrue(
            said.hasPrefix("The IPA was built, but Graft couldn't save it as \(chosen.path). "), said
        )
        XCTAssertTrue(said.contains("save the file “Example 1.0.ipa” in the folder “Exports”"), said)
        XCTAssertFalse(said.contains("Graft Export"), said)
        XCTAssertFalse(said.contains(".graft-"), said)
    }

    // MARK: - a real copy, stopped partway

    /// What a real copy had done when it was stopped, written from its
    /// thread and read once it has ended.
    private final class Partway: @unchecked Sendable {
        var chunks: [Int64] = []
        var besideThen: Bool?
        var stop: (() -> Void)?
    }

    /// copyfile's chunk is the volume's I/O size, 1 MiB on APFS: two of them.
    private let twoChunks = 2 << 20

    /// The live copy, stopped once its first chunk is written: copyfile
    /// quits at once, removes the part it wrote, and says it was stopped.
    func test_the_live_copy_stopped_partway_removes_what_it_wrote() async throws {
        let from = root.appendingPathComponent("from.ipa")
        let to = root.appendingPathComponent("to.ipa")
        try Data(count: twoChunks).write(to: from)
        let partway = Partway()

        let copied = await Task.detached { () -> Result<Void, Error> in
            Result {
                try ExportFileOps.copyStoppably(from: from, to: to) { copied in
                    partway.chunks.append(copied)
                    partway.besideThen = FileManager.default.fileExists(atPath: to.path)
                    withUnsafeCurrentTask { $0?.cancel() }
                }
            }
        }.value

        guard case let .failure(error) = copied else { return XCTFail("copied in full after a stop") }
        XCTAssertTrue(error is CancellationError, "\(error)")
        XCTAssertEqual(partway.chunks.count, 1, "it went on after the stop: \(partway.chunks)")
        XCTAssertLessThan(partway.chunks.first ?? .max, Int64(twoChunks), "not partway")
        XCTAssertEqual(partway.besideThen, true)
        XCTAssertFalse(FileManager.default.fileExists(atPath: to.path))
    }

    /// Stop pressed while a real fallback copy is partway through: the
    /// pane's task is canceled, the copy's own task with it, and the copy
    /// ends at its next chunk. Nothing is left where the user chose, or
    /// beside it.
    @MainActor
    func test_a_stop_partway_through_a_real_copy_leaves_nothing_at_the_destination() async throws {
        let partway = Partway()
        var ops = fallbackLive()
        ops.copyItem = { from, to in
            try ExportFileOps.copyStoppably(from: from, to: to) { copied in
                partway.chunks.append(copied)
                partway.besideThen = FileManager.default.fileExists(atPath: to.path)
                partway.stop?()
            }
        }
        let staging = try ExportStaging.prepare(for: destination, ops: ops)
        defer { staging.discard() }
        try Data(count: twoChunks).write(to: staging.stagedFile)

        let placing = Task { try await staging.place() }
        partway.stop = { placing.cancel() }
        let ended = await placing.result

        guard case let .failure(error) = ended else { return XCTFail("placed after a stop") }
        XCTAssertTrue(error is CancellationError, "\(error)")
        XCTAssertEqual(partway.chunks.count, 1, "it went on after the stop: \(partway.chunks)")
        XCTAssertLessThan(partway.chunks.first ?? .max, Int64(twoChunks), "not partway")
        XCTAssertEqual(partway.besideThen, true)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: root.path), ["tmp"])
    }

    // MARK: - a file that appears while the file is copied in

    /// A file saved at the destination after the check before the copy:
    /// the move refuses to write over it, and that's said as the refusal for
    /// a file that appeared, not as FileManager's "already exists" about a
    /// hidden file. With or without the copy, however the refusal is said.
    @MainActor
    func test_a_file_that_appears_before_the_move_is_the_appeared_refusal() async throws {
        let refusals: [Error] = [
            CocoaError(.fileWriteFileExists, userInfo: [NSFilePathErrorKey: "/elsewhere"]),
            POSIXError(.EEXIST),
        ]
        for copies in [true, false] {
            for refusal in refusals {
                let calls = Calls()
                var ops = recorded(calls, failing: copies ? ["replacementDirectory"] : [])
                ops.moveItem = { from, to in
                    calls.made.append(("moveItem", [from, to]))
                    throw refusal
                }
                let staging = try ExportStaging.prepare(for: destination, ops: ops)
                calls.made = []

                let error = try await placingFails(staging)

                guard case let .appeared(at)? = error as? ExportStagingError else {
                    XCTFail("copies \(copies), \(refusal): \(error)")
                    continue
                }
                XCTAssertEqual(at, destination)
                XCTAssertEqual(
                    calls.names, copies ? ["copyItem", "moveItem", "removeItem"] : ["moveItem"],
                    "the copy beside is removed"
                )
            }
        }
    }

    /// On disk: a file saved where the user chose while the fallback's copy
    /// ran keeps its contents, the refusal says why, and no copy is left
    /// beside it.
    @MainActor
    func test_a_real_file_that_appears_during_the_copy_is_left_alone_and_said() async throws {
        var ops = fallbackLive()
        let copy = ops.copyItem
        let destination = self.destination
        ops.copyItem = { from, to in
            try copy(from, to)
            try Data("theirs".utf8).write(to: destination)
        }
        let staging = try ExportStaging.prepare(for: destination, ops: ops)
        defer { staging.discard() }
        try writeStaged(staging, "ours")

        let said = try await placingFails(staging).localizedDescription

        XCTAssertTrue(said.hasPrefix("A file named Example 1.0.ipa appeared in that folder"), said)
        XCTAssertEqual(try String(contentsOf: destination, encoding: .utf8), "theirs")
        XCTAssertEqual(
            try FileManager.default.contentsOfDirectory(atPath: root.path).sorted(),
            ["Example 1.0.ipa", "tmp"], "no copy left beside it"
        )
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

    @MainActor
    func test_a_real_export_is_moved_into_place_with_the_clis_mode() async throws {
        let staging = try ExportStaging.prepare(for: destination)
        defer { staging.discard() }
        XCTAssertFalse(staging.copies, "this volume has a replacement directory")
        XCTAssertFalse(
            staging.folder.path.hasPrefix(root.path), "staged in the folder macOS gave, not beside"
        )
        try writeStaged(staging, "new")

        let replaced = try await staging.place()
        XCTAssertFalse(replaced)

        XCTAssertEqual(try String(contentsOf: destination, encoding: .utf8), "new")
        XCTAssertEqual(try mode(destination), 0o644)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: root.path), [
            "Example 1.0.ipa",
        ])
    }

    /// `replaceItemAt` keeps the old file's permissions unless told not to,
    /// which would undo EX3 for every Replace.
    @MainActor
    func test_a_replace_keeps_the_clis_mode_not_the_old_files() async throws {
        try Data("old".utf8).write(to: destination)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: destination.path)
        let staging = try ExportStaging.prepare(for: destination, replacing: true)
        defer { staging.discard() }
        try writeStaged(staging, "new")

        let replaced = try await staging.place()
        XCTAssertTrue(replaced)

        XCTAssertEqual(try String(contentsOf: destination, encoding: .utf8), "new")
        XCTAssertEqual(try mode(destination), 0o644)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: root.path), [
            "Example 1.0.ipa",
        ])
    }

    @MainActor
    func test_the_fallbacks_copy_keeps_the_clis_mode_too() async throws {
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

            let replaced = try await staging.place()
            XCTAssertEqual(replaced, exists)
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
    @MainActor
    func test_finish_places_the_file_and_names_it_where_it_went() async throws {
        let calls = Calls()
        let staging = try ExportStaging.prepare(
            for: destination, replacing: true, ops: recorded(calls, exists: true)
        )
        let events: [BuildEvent] = [.ipaExported(exported(path: staging.stagedFile.path))]

        let placed = try await staging.finish(events: events).get()

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

    @MainActor
    func test_a_run_that_never_said_what_it_wrote_places_nothing() async throws {
        let calls = Calls()
        let staging = try ExportStaging.prepare(for: destination, ops: recorded(calls))
        calls.made = []

        guard case let .failure(error) = await staging.finish(events: []) else {
            return XCTFail("placed with no ipa-exported")
        }
        XCTAssertTrue(error.localizedDescription.contains("nothing was saved"))
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

    // MARK: - what the pane hears

    /// The CLI only ever saw the staging path, so its errors name it, and
    /// the failure card read "couldn't write /var/folders/…/NSIRD_Graft_…".
    /// The staged file is the user's file; the folder alone is the export's
    /// temporary folder. As given and as /private/var spells it.
    func test_cli_errors_name_the_users_file_not_the_staging_folder() throws {
        let staging = ExportStaging(
            destination: destination,
            folder: URL(fileURLWithPath: "/var/folders/ab/xyz/T/NSIRD_Graft_1234", isDirectory: true),
            copies: false, replacing: false, ops: recorded(Calls())
        )
        let staged = staging.stagedFile.path
        XCTAssertEqual(staged, "/var/folders/ab/xyz/T/NSIRD_Graft_1234/Example 1.0.ipa")
        let said = "couldn't write \(staged): No space left on device; free some space on the disk "
            + "/private/var/folders/ab/xyz/T/NSIRD_Graft_1234 is on, or choose another folder "
            + "(/private\(staged))"

        XCTAssertEqual(
            staging.userFacing(said),
            "couldn't write \(destination.path): No space left on device; free some space on the disk "
                + "the export's temporary folder is on, or choose another folder (\(destination.path))"
        )
        guard case let .buildEnd(app, ok, error, elapsed, stopped)? = staging.heard(
            .buildEnd(app: "example", ok: false, error: said, elapsedSeconds: 2, stopped: false)
        ) else { return XCTFail("a failure is heard as it comes") }
        XCTAssertEqual(app, "example")
        XCTAssertFalse(ok)
        XCTAssertEqual(error, staging.userFacing(said))
        XCTAssertEqual(elapsed, 2)
        XCTAssertFalse(stopped)
        XCTAssertFalse(error?.contains("NSIRD") ?? true)
        guard case let .log(source, line)? = staging.heard(.log(source: "export", line: "wrote \(staged)"))
        else { return XCTFail("a log line is heard") }
        XCTAssertEqual(source, "export")
        XCTAssertEqual(line, "wrote \(destination.path)")
        let failed = CLIRunner.RunResult(exitCode: 1, stdout: "", stderr: "error: couldn't write \(staged)\n")
        XCTAssertEqual(staging.userFacing(failed).stderr, "error: couldn't write \(destination.path)\n")
        XCTAssertEqual(staging.userFacing("nothing of ours"), "nothing of ours")
    }

    /// What the pane holds back until the file is placed, as `handle` hears
    /// it: the CLI's `ipa-exported`, naming the staging folder, and its
    /// successful `build-end`.
    func test_the_pane_hears_the_clis_record_and_done_only_once_placed() throws {
        let staging = try ExportStaging.prepare(for: destination, ops: recorded(Calls()))
        XCTAssertNil(staging.heard(.ipaExported(exported(path: staging.stagedFile.path))))
        XCTAssertNil(staging.heard(.buildEnd(app: "example", ok: true, error: nil, elapsedSeconds: 3)))
        XCTAssertNotNil(staging.heard(.stage(name: "export", status: "start", fields: [:])))
        XCTAssertNotNil(staging.heard(.buildEnd(app: "example", ok: false, error: nil, elapsedSeconds: 3)))
    }

    private struct Said {
        var events: [BuildEvent] = []
        var names: [String] {
            events.map { event -> String in
                switch event {
                case let .ipaExported(record): return "ipa-exported \(record.path)"
                case let .buildEnd(_, ok, error, elapsed, _):
                    return "build-end \(ok) \(error ?? "-") \(elapsed.map { "\($0)" } ?? "-")"
                default: return "\(event)"
                }
            }
        }
    }

    /// `finishExport`'s run, placed: the file moved, then the line naming it
    /// where the user chose and the `build-end` the pane held back, with the
    /// CLI's time, in that order.
    @MainActor
    func test_a_finished_run_places_then_says_where_and_done() async throws {
        let calls = Calls()
        let staging = try ExportStaging.prepare(for: destination, ops: recorded(calls))
        calls.made = []
        let cli: [BuildEvent] = [
            .ipaExported(exported(path: staging.stagedFile.path)),
            .buildEnd(app: "example", ok: true, error: nil, elapsedSeconds: 12.5),
        ]
        var said = Said()

        let ending = await staging.finishRun(events: cli, app: "example") { event in
            XCTAssertEqual(calls.names, ["moveItem"], "said before the file was placed")
            said.events.append(event)
        }

        XCTAssertEqual(said.names, ["ipa-exported \(destination.path)", "build-end true - 12.5"])
        guard case let .placed(events) = ending else { return XCTFail("\(ending)") }
        XCTAssertEqual(events.count, 3)
        XCTAssertEqual(AppDetailView.exportedRecord(in: events)?.path, destination.path)
    }

    @MainActor
    func test_a_finished_run_that_couldnt_place_says_why_as_its_build_end() async throws {
        let calls = Calls()
        let staging = try ExportStaging.prepare(for: destination, ops: recorded(calls, failing: ["moveItem"]))
        let cli: [BuildEvent] = [
            .ipaExported(exported(path: staging.stagedFile.path)),
            .buildEnd(app: "example", ok: true, error: nil, elapsedSeconds: 4),
        ]
        var said = Said()

        let ending = await staging.finishRun(events: cli, app: "example") { said.events.append($0) }

        guard case .failed = ending else { return XCTFail("\(ending)") }
        XCTAssertEqual(said.names, [
            "build-end false The IPA was built, but Graft couldn't save it as \(destination.path). "
                + "refused by the test 4.0",
        ])
    }

    /// A file saved at the destination while the build ran, on the app's own
    /// path: the pane hears that it was left alone, and it keeps its contents.
    @MainActor
    func test_a_file_that_appeared_mid_build_is_said_and_left_alone_on_the_apps_path() async throws {
        let staging = try ExportStaging.prepare(for: destination)
        defer { staging.discard() }
        try writeStaged(staging, "ours")
        let cli: [BuildEvent] = [
            .ipaExported(exported(path: staging.stagedFile.path)),
            .buildEnd(app: "example", ok: true, error: nil, elapsedSeconds: 4),
        ]
        try Data("theirs".utf8).write(to: destination)
        var said = Said()

        let ending = await staging.finishRun(events: cli, app: "example") { said.events.append($0) }

        guard case .failed = ending else { return XCTFail("\(ending)") }
        guard case let .buildEnd(_, false, error?, _, _)? = said.events.first, said.events.count == 1 else {
            return XCTFail("\(said.names)")
        }
        XCTAssertTrue(error.hasPrefix("A file named Example 1.0.ipa appeared in that folder"), error)
        XCTAssertTrue(error.contains("left it alone"), error)
        XCTAssertEqual(try String(contentsOf: destination, encoding: .utf8), "theirs")
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: root.path), ["Example 1.0.ipa"])
    }

    /// Stopped while the file was copied in: nothing said here, for the pane
    /// says it was stopped.
    @MainActor
    func test_a_finished_run_stopped_while_placing_is_a_stop() async throws {
        let calls = Calls()
        var ops = recorded(calls, failing: ["replacementDirectory"])
        ops.copyItem = { _, _ in throw CancellationError() }
        let staging = try ExportStaging.prepare(for: destination, ops: ops)
        var said = Said()

        let ending = await staging.finishRun(
            events: [.ipaExported(exported(path: staging.stagedFile.path))], app: "example"
        ) { said.events.append($0) }

        guard case .stopped = ending else { return XCTFail("\(ending)") }
        XCTAssertEqual(said.names, [])
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
                if places { _ = try? await staging.place() }
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

    /// Graft's folder as config.yaml may spell it, composed (NFC), while
    /// every path through URL, the destination's included, comes out
    /// decomposed (NFD), as macOS's file system representation is: one
    /// folder, two spellings that differ byte for byte. The refusal goes by
    /// identity, so it holds either way round.
    func test_graft_folders_are_refused_composed_or_decomposed() throws {
        let composed = "Caf\u{E9}"
        let decomposed = "Cafe\u{301}"
        let base = root.resolvingSymlinksInPath().path
        let cacheDir = base + "/" + composed + "/ipas"
        let builtDir = base + "/" + composed + "/built"
        for folder in [cacheDir, builtDir] {
            try FileManager.default.createDirectory(atPath: folder, withIntermediateDirectories: true)
        }
        let destination = URL(fileURLWithPath: cacheDir).appendingPathComponent("x.ipa")
        let seen = destination.deletingLastPathComponent().standardizedFileURL.resolvingSymlinksInPath().path
        guard seen.unicodeScalars.contains("\u{301}") else {
            throw XCTSkip("Foundation kept the composed spelling here: nothing to tell apart")
        }
        XCTAssertFalse(seen.unicodeScalars.elementsEqual(cacheDir.unicodeScalars))

        XCTAssertNotNil(ExportStaging.refusal(for: destination, cacheDir: cacheDir, builtDir: builtDir),
                        "the cache folder, named composed")
        XCTAssertNotNil(ExportStaging.refusal(
            for: URL(fileURLWithPath: builtDir).appendingPathComponent("x.ipa"),
            cacheDir: base + "/" + decomposed + "/ipas", builtDir: builtDir
        ), "the built folder, named composed")
        XCTAssertNotNil(ExportStaging.refusal(
            for: destination, cacheDir: base + "/" + decomposed + "/ipas", builtDir: builtDir
        ), "the cache folder, named decomposed")
        XCTAssertNil(ExportStaging.refusal(
            for: URL(fileURLWithPath: base + "/" + composed).appendingPathComponent("x.ipa"),
            cacheDir: cacheDir, builtDir: builtDir
        ), "their parent is the user's")
    }

    func test_a_graft_folder_that_isnt_there_holds_nothing() {
        XCTAssertNil(ExportStaging.refusal(
            for: destination,
            cacheDir: root.appendingPathComponent("none").path,
            builtDir: root.appendingPathComponent("nothing").path
        ))
    }
}
