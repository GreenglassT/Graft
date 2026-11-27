import Foundation

/// The file operations an export's staging and placement make, one closure
/// each, so a test can fail or watch any of them (`ExportStagingTests`).
/// `live` is `FileManager.default`'s.
///
/// `replaceItem` is the one strategy for a file that's already at the Save
/// panel's URL. Should Replace ever raise a privacy prompt a new file
/// doesn't (`replaceItemAt` may make an item of its own beside the
/// original), moving the old file to the Trash and then `moveItem` is a
/// change to this closure alone.
struct ExportFileOps {
    /// A new folder on `destination`'s volume and outside the folders macOS
    /// protects: `url(for: .itemReplacementDirectory, in: .userDomainMask,
    /// appropriateFor: destination, create: true)`. Some network and FAT
    /// volumes have none.
    var replacementDirectory: (_ destination: URL) throws -> URL
    /// Where the fallback makes its folder when there is no replacement
    /// directory: `FileManager.default.temporaryDirectory`.
    var temporaryDirectory: () -> URL
    var createDirectory: (URL) throws -> Void
    var fileExists: (URL) -> Bool
    /// Put `replacement` where `original` is. With `.usingNewMetadataOnly`,
    /// so the file keeps the mode the CLI gave it (EX3) rather than taking
    /// the old file's, which `replaceItemAt` does by default.
    var replaceItem: (_ original: URL, _ replacement: URL) throws -> Void
    var moveItem: (_ from: URL, _ to: URL) throws -> Void
    /// Copy `from` to the new file `to`. Run off the main actor
    /// (`ExportStaging.place`), and stopped by canceling the task it runs in:
    /// `live`'s throws `CancellationError` and leaves nothing at `to`.
    var copyItem: (_ from: URL, _ to: URL) throws -> Void
    var removeItem: (URL) throws -> Void

    static var live: ExportFileOps {
        let files = FileManager.default
        return ExportFileOps(
            replacementDirectory: {
                try files.url(
                    for: .itemReplacementDirectory, in: .userDomainMask,
                    appropriateFor: $0, create: true
                )
            },
            temporaryDirectory: { files.temporaryDirectory },
            createDirectory: { try files.createDirectory(at: $0, withIntermediateDirectories: true) },
            fileExists: { files.fileExists(atPath: $0.path) },
            replaceItem: {
                _ = try files.replaceItemAt(
                    $0, withItemAt: $1, backupItemName: nil, options: .usingNewMetadataOnly
                )
            },
            moveItem: { try files.moveItem(at: $0, to: $1) },
            copyItem: { try copyStoppably(from: $0, to: $1) },
            removeItem: { try files.removeItem(at: $0) }
        )
    }

    /// `copyfile(3)` with what `FileManager.copyItem` copies of a file (its
    /// data, mode and extended attributes), never over a file at `to`, and
    /// stopped when the task it runs in is canceled: a Stop mid-copy. Between
    /// chunks, copyfile asks its status callback whether to go on, and
    /// `COPYFILE_QUIT` ends the copy with ECANCELED and removes what it wrote.
    ///
    /// - Parameter eachChunk: told how many bytes are copied after each chunk
    ///   is written, just before the callback asks whether to go on: a test's
    ///   way to stop a copy partway through. Nil in the app.
    static func copyStoppably(
        from: URL, to: URL, eachChunk: ((_ copied: Int64) -> Void)? = nil
    ) throws {
        guard let state = copyfile_state_alloc() else {
            throw copyFailure(errno: errno, saving: to)
        }
        defer { copyfile_state_free(state) }
        let goOn: copyfile_callback_t = { what, stage, state, _, _, context in
            if what == COPYFILE_COPY_DATA, stage == COPYFILE_PROGRESS, let context {
                var copied: off_t = 0
                copyfile_state_get(state, UInt32(COPYFILE_STATE_COPIED), &copied)
                Unmanaged<ChunkWatch>.fromOpaque(context).takeUnretainedValue().eachChunk(Int64(copied))
            }
            return Task.isCancelled ? COPYFILE_QUIT : COPYFILE_CONTINUE
        }
        copyfile_state_set(
            state, UInt32(COPYFILE_STATE_STATUS_CB), unsafeBitCast(goOn, to: UnsafeRawPointer.self)
        )
        let watch = eachChunk.map(ChunkWatch.init)
        if let watch {
            copyfile_state_set(
                state, UInt32(COPYFILE_STATE_STATUS_CTX), Unmanaged.passUnretained(watch).toOpaque()
            )
        }
        let (status, code) = withExtendedLifetime(watch) {
            (copyfile(from.path, to.path, state, copyfile_flags_t(COPYFILE_ALL | COPYFILE_EXCL)), errno)
        }
        guard status != 0 else { return }
        if code == ECANCELED { throw CancellationError() }
        throw copyFailure(errno: code, saving: to)
    }

    /// A failed copy's `errno` as FileManager says it, naming `file`, the
    /// file being saved: "You don't have permission to save the file “X.ipa”
    /// in the folder “Exports”". Never the file read, which for Export IPA…
    /// is in the staging folder.
    static func copyFailure(errno code: Int32, saving file: URL) -> CocoaError {
        let cocoa: CocoaError.Code
        switch code {
        case ENOSPC, EDQUOT: cocoa = .fileWriteOutOfSpace
        case EACCES, EPERM: cocoa = .fileWriteNoPermission
        case EROFS: cocoa = .fileWriteVolumeReadOnly
        case EEXIST: cocoa = .fileWriteFileExists
        case ENOENT: cocoa = .fileNoSuchFile
        default: cocoa = .fileWriteUnknown
        }
        return CocoaError(cocoa, userInfo: [
            NSFilePathErrorKey: file.path,
            NSUnderlyingErrorKey: NSError(domain: NSPOSIXErrorDomain, code: Int(code)),
        ])
    }

    /// `copyStoppably`'s `eachChunk`, for copyfile's callback, which takes a
    /// pointer and no closure.
    private final class ChunkWatch {
        let eachChunk: (Int64) -> Void
        init(_ eachChunk: @escaping (Int64) -> Void) { self.eachChunk = eachChunk }
    }
}

/// Where Export IPA… has the CLI write the IPA, and how the app then puts it
/// where the user chose (EX1).
///
/// **The app places the file, not the CLI.** Graft.app isn't sandboxed, and
/// the Save panel's grant covers the URL the user chose, not a new file the
/// CLI, its child, makes beside it. Writing a hidden temp file in Desktop,
/// Documents or Downloads could raise a Files and Folders prompt in the
/// middle of a build, or fail at its end after a past denial. So the CLI
/// writes into a folder of the export's own on the same volume, outside the
/// protected folders, and the app, which holds the grant, moves or replaces
/// the file into place once the build is done. The folder is removed however
/// the run ends (`ConfigStore.launchExport`); a run killed outright leaves it
/// where macOS clears such folders itself.
///
/// The app needs no `--overwrite` either: the Save panel asked before
/// Replace, and the app does the replacing. A dry run writes nothing, so it
/// has no staging folder: the CLI previews the user's own path.
struct ExportStaging {
    /// The Save panel's choice: where the file ends up.
    let destination: URL
    /// The folder the CLI writes into, made for this export alone.
    let folder: URL
    /// `folder` is a plain temp folder, made because the destination's volume
    /// has no replacement directory, and maybe on another volume: the file is
    /// copied into place rather than moved.
    let copies: Bool
    /// The Save panel's answer, as it closed: a file was already at
    /// `destination` and the user chose Replace. Only then is a file there
    /// replaced; one that appears while the build runs is left alone.
    let replacing: Bool
    let ops: ExportFileOps

    /// `--export-ipa`'s path: `folder`, and the name the user chose.
    var stagedFile: URL { folder.appendingPathComponent(destination.lastPathComponent) }

    /// A staging folder for an export to `destination`: the replacement
    /// directory macOS gives for that volume, or a folder of its own in the
    /// temp folder when there isn't one. Throws when neither can be made,
    /// with a sentence to show; it never hands back nothing.
    static func prepare(
        for destination: URL, replacing: Bool = false, ops: ExportFileOps = .live
    ) throws -> ExportStaging {
        if let folder = try? ops.replacementDirectory(destination) {
            return ExportStaging(
                destination: destination, folder: folder, copies: false, replacing: replacing, ops: ops
            )
        }
        let folder = ops.temporaryDirectory()
            .appendingPathComponent("Graft Export \(UUID().uuidString)", isDirectory: true)
        do {
            try ops.createDirectory(folder)
        } catch {
            throw ExportStagingError.noFolder(destination: destination, underlying: error)
        }
        return ExportStaging(
            destination: destination, folder: folder, copies: true, replacing: replacing, ops: ops
        )
    }

    /// Put the CLI's file at `destination`. Returns whether it replaced one.
    ///
    /// A replace only when a file is there and the user chose Replace in the
    /// Save panel (`replacing`). A file that appeared at `destination` while
    /// the build ran, with no Replace, is left alone and nothing is saved:
    /// the CLI's own "appeared while building" guard never sees this path.
    /// Otherwise a move, which itself refuses to write over a file that
    /// appears at the last moment, or while the copy below runs: that's
    /// the same refusal, `appeared`. The fallback's folder may be on
    /// another volume, so its file is copied in beside the destination first
    /// and moved or replaced from there: a copy that fails leaves the old
    /// file as it was and nothing half-written in its place.
    ///
    /// On the main actor, as the pane is, but the copy isn't (`copyOffMain`):
    /// a big IPA copied to another volume froze the window, Stop with it.
    /// A Stop during the copy ends it, removes what it wrote, places nothing
    /// and throws `CancellationError`, as itself, so the pane says stopped.
    /// The copy's errors name the user's file and folder, not the hidden
    /// copy's name or the staging folder.
    @MainActor
    @discardableResult
    func place() async throws -> Bool {
        let there = ops.fileExists(destination)
        if there && !replacing {
            throw ExportStagingError.appeared(destination: destination)
        }
        var beside: URL?
        do {
            var source = stagedFile
            if copies {
                let copy = destination.deletingLastPathComponent().appendingPathComponent(
                    ".\(destination.lastPathComponent).graft-\(UUID().uuidString.prefix(8))"
                )
                beside = copy
                do {
                    try await copyOffMain(stagedFile, to: copy)
                } catch let error as CocoaError {
                    throw sayingTheDestination(error)
                }
                // A Stop as the copy finished: still nothing placed.
                try Task.checkCancellation()
                source = copy
            }
            if there {
                try ops.replaceItem(destination, source)
            } else {
                do {
                    try ops.moveItem(source, destination)
                } catch let error where Self.alreadyThere(error) {
                    // A file saved at the destination since the check above,
                    // while the copy ran: the move left it alone.
                    throw ExportStagingError.appeared(destination: destination)
                }
            }
        } catch {
            if let beside { try? ops.removeItem(beside) }
            if error is CancellationError { throw error }
            if case .appeared? = error as? ExportStagingError { throw error }
            throw ExportStagingError.notPlaced(destination: destination, underlying: error)
        }
        return there
    }

    /// A copy's error said as the file the user chose: the copy names the
    /// hidden file it was saving beside it.
    private func sayingTheDestination(_ error: CocoaError) -> CocoaError {
        var info = error.userInfo
        info[NSFilePathErrorKey] = destination.path
        info[NSURLErrorKey] = nil
        return CocoaError(error.code, userInfo: info)
    }

    /// Whether a move failed because a file is already at its destination,
    /// as FileManager says it or as the system call does.
    private static func alreadyThere(_ error: Error) -> Bool {
        let said = error as NSError
        return (said.domain == NSCocoaErrorDomain && said.code == NSFileWriteFileExistsError)
            || (said.domain == NSPOSIXErrorDomain && said.code == Int(EEXIST))
    }

    /// `ops.copyItem`, in a task of its own off the main actor, canceled
    /// when the calling task is (Stop).
    private func copyOffMain(_ from: URL, to: URL) async throws {
        let copyItem = ops.copyItem
        let copying = Task.detached(priority: .userInitiated) { try copyItem(from, to) }
        try await withTaskCancellationHandler {
            try await copying.value
        } onCancel: {
            copying.cancel()
        }
    }

    /// What a finished export wrote, placed: the CLI's `ipa-exported`, moved
    /// to `destination` and said as being there, or why it isn't.
    @MainActor
    func finish(events: [BuildEvent]) async -> Result<IPAExportedRecord, Error> {
        guard let made = AppDetailView.exportedRecord(in: events) else {
            return .failure(ExportStagingError.notReported(destination: destination))
        }
        do {
            let replaced = try await place()
            return .success(made.placed(at: destination, replaced: replaced))
        } catch {
            return .failure(error)
        }
    }

    /// How a staged export's run ended, once the CLI exited 0 (`finishRun`).
    enum Ending {
        /// The file is in place: the stream's events, and the placed record.
        case placed([BuildEvent])
        /// It isn't, and the pane heard why.
        case failed
        /// Stop was pressed while the file was being placed.
        case stopped
    }

    /// Place the CLI's file, then tell the pane (`say`, its event handler)
    /// how the run ended: the line naming the file where the user chose it
    /// and the successful `build-end` it held back (`holdsUntilPlaced`), or
    /// a failed `build-end` saying why the file isn't there, with the CLI's
    /// time. A Stop during the placing says nothing here: the pane says it
    /// was stopped (`AppDetailView.finishExport`).
    @MainActor
    func finishRun(events: [BuildEvent], app: String, say: (BuildEvent) -> Void) async -> Ending {
        let elapsed = events.lazy.reversed().compactMap { event -> Double? in
            if case let .buildEnd(_, _, _, elapsedSeconds, _, _) = event { return elapsedSeconds }
            return nil
        }.first
        switch await finish(events: events) {
        case let .success(placed):
            say(.ipaExported(placed))
            say(.buildEnd(app: app, ok: true, error: nil, elapsedSeconds: elapsed))
            return .placed(events + [.ipaExported(placed)])
        case let .failure(error) where error is CancellationError:
            return .stopped
        case let .failure(error):
            say(.buildEnd(app: app, ok: false, error: error.localizedDescription, elapsedSeconds: elapsed))
            return .failed
        }
    }

    /// A CLI event as the pane hears it during a staged export, or nil while
    /// it waits for the file to be placed (`holdsUntilPlaced`). Its words
    /// name the user's file, not the staging folder (`userFacing`).
    func heard(_ event: BuildEvent) -> BuildEvent? {
        if let progress = event.progress, Self.holdsUntilPlaced(progress) { return nil }
        switch event {
        case let .buildEnd(app, ok, error, elapsed, stopped, waiting):
            return .buildEnd(
                app: app, ok: ok, error: error.map(userFacing), elapsedSeconds: elapsed, stopped: stopped,
                waiting: waiting
            )
        case let .log(source, line):
            return .log(source: source, line: userFacing(line))
        default:
            return event
        }
    }

    /// `text` with the staging paths the CLI was given said as the user's:
    /// the staged file as the file they chose, and the folder alone as "the
    /// export's temporary folder". The CLI's errors name the only path it
    /// knows, and a failure card naming /var/folders/…/NSIRD_Graft_… tells
    /// the user nothing. Each path as given and as /private/var spells it.
    func userFacing(_ text: String) -> String {
        var said = text
        for (path, words) in [
            (stagedFile.path, destination.path), (folder.path, "the export's temporary folder"),
        ] {
            for spelling in Self.spellings(of: path) {
                said = said.replacingOccurrences(of: spelling, with: words)
            }
        }
        return said
    }

    /// A run's result with `userFacing` stderr, for the failure card and the
    /// transcript when the CLI fails without a `build-end` of its own.
    func userFacing(_ result: CLIRunner.RunResult) -> CLIRunner.RunResult {
        CLIRunner.RunResult(
            exitCode: result.exitCode, stdout: result.stdout, stderr: userFacing(result.stderr),
            events: result.events
        )
    }

    /// `path`, and the same path through /private, which macOS's /var, /tmp
    /// and /etc are links to: longest first, so neither is half-replaced.
    private static func spellings(of path: String) -> [String] {
        let privately = "/private"
        if path.hasPrefix(privately + "/") {
            return [path, String(path.dropFirst(privately.count))]
        }
        if ["/var/", "/tmp/", "/etc/"].contains(where: path.hasPrefix) {
            return [privately + path, path]
        }
        return [path]
    }

    /// Remove the staging folder and whatever is left in it. Quiet: once the
    /// run is over there is nothing to do about a folder that won't go, and
    /// macOS clears these itself.
    func discard() {
        try? ops.removeItem(folder)
    }

    /// Whether a staged export's pane holds this event back until the file is
    /// in place: the CLI's `ipa-exported`, which names the staging folder,
    /// and its successful `build-end`. Neither the banner nor the pane may say
    /// "done" over a file that isn't where the user chose yet; `finish`'s
    /// answer is said in their place.
    static func holdsUntilPlaced(_ progress: BuildProgress) -> Bool {
        switch progress {
        case .exported, .finished(true, _, _): return true
        default: return false
        }
    }

    /// Why Export IPA won't save at `destination`, or nil: it's in Graft's
    /// own folder for staged IPAs or archived builds, which Graft cleans up
    /// itself. The CLI refuses those, but it never sees `destination` now, so
    /// the app asks too.
    ///
    /// By file identity, up the resolved folder's ancestors, as the CLI does
    /// (EX2): another letter case or a decomposed name is the same folder.
    ///
    /// The ancestors are the path's own components, shortest last, rather
    /// than `deletingLastPathComponent` until it stops changing: that never
    /// stops, since "/" becomes "/..".
    /// `refusal(for:cacheDir:builtDir:)` against the settings' paths, or a
    /// refusal when there are none: without them Graft can't tell whether
    /// `destination` is one of its own folders, and the CLI never sees it.
    static func refusal(for destination: URL, paths: SettingsPaths?) -> String? {
        guard let paths else { return noSettings }
        return refusal(for: destination, cacheDir: paths.cacheDir, builtDir: paths.builtDir)
    }

    static let noSettings = "Export IPA didn't start: Graft couldn't read its settings, so it "
        + "can't tell where it keeps its own files. Open Settings, then export again."

    static func refusal(for destination: URL, cacheDir: String, builtDir: String) -> String? {
        let components = destination.deletingLastPathComponent().standardizedFileURL
            .resolvingSymlinksInPath().pathComponents
        let ancestors = (1...max(components.count, 1)).reversed().map {
            NSString.path(withComponents: Array(components.prefix($0)))
        }
        for (folder, holds) in [(cacheDir, "staged IPAs"), (builtDir, "archived builds")] {
            guard let wanted = identity(of: folder) else { continue }
            if ancestors.contains(where: { identity(of: $0) == wanted }) {
                return "Export IPA won't save in \(folder): it's Graft's own folder for "
                    + "\(holds), which Graft cleans up itself. Choose another folder."
            }
        }
        return nil
    }

    private struct Identity: Equatable {
        let device: Int32
        let inode: UInt64
    }

    private static func identity(of path: String) -> Identity? {
        var info = stat()
        guard stat(path, &info) == 0 else { return nil }
        return Identity(device: info.st_dev, inode: info.st_ino)
    }
}

/// Why an export's staging or placement failed, said for the pane.
enum ExportStagingError: LocalizedError {
    /// Neither a replacement directory nor a temp folder could be made.
    case noFolder(destination: URL, underlying: Error)
    /// The CLI's file couldn't be put at the destination.
    case notPlaced(destination: URL, underlying: Error)
    /// The CLI finished without an `ipa-exported`.
    case notReported(destination: URL)
    /// A file appeared at the destination while the build ran, and the Save
    /// panel hadn't said Replace.
    case appeared(destination: URL)

    var errorDescription: String? {
        switch self {
        case let .noFolder(destination, underlying):
            return "Couldn't export \(destination.lastPathComponent): Graft couldn't make a "
                + "temporary folder to build it in. \(underlying.localizedDescription)"
        case let .notPlaced(destination, underlying):
            return "The IPA was built, but Graft couldn't save it as \(destination.path). "
                + underlying.localizedDescription
        case let .appeared(destination):
            return "A file named \(destination.lastPathComponent) appeared in that folder while "
                + "Graft was building, so Graft left it alone and saved nothing. Export again, "
                + "and choose Replace if you want to replace it."
        case let .notReported(destination):
            return "The export finished without saying where it wrote the IPA, so nothing "
                + "was saved as \(destination.path)."
        }
    }
}

extension IPAExportedRecord {
    /// This record, for the file now at `url`: what the pane's confirmation,
    /// Show in Finder and transcript name, rather than the staging folder.
    func placed(at url: URL, replaced: Bool) -> IPAExportedRecord {
        IPAExportedRecord(
            app: app, version: version, path: url.path, filename: url.lastPathComponent,
            sizeBytes: sizeBytes, tweaks: tweaks, replaced: replaced, dryRun: dryRun
        )
    }
}

extension ConfigStore {
    /// `launchBuild` for Export IPA…, whose staging folder is removed however
    /// the run ends: after `work` returns, whether it placed the file, failed
    /// or was stopped.
    ///
    /// When the build slot is taken (something started while the Save panel
    /// was up), the folder is removed now and `lastError` says why the export
    /// didn't start. `launchBuild` alone returned false and Export IPA… did
    /// nothing at all.
    @discardableResult
    func launchExport(
        appName: String,
        delivery: EffectiveDelivery,
        staging: ExportStaging?,
        _ work: @escaping @MainActor () async -> Void
    ) -> Bool {
        let launched = launchBuild(appName: appName, delivery: delivery, reason: TerminationHold.exporting) {
            defer { staging?.discard() }
            await work()
        }
        guard launched else {
            staging?.discard()
            lastError = Self.exportNotStarted(appName: shownName(of: appName), busy: busy)
            return false
        }
        return true
    }

    static func exportNotStarted(appName: String, busy: Busy?) -> String {
        let why = busy?.explanation ?? "another build is running — wait for it to finish"
        return "Export IPA didn't start for \(appName): \(why), then export again."
    }
}
