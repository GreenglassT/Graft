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
            copyItem: { try files.copyItem(at: $0, to: $1) },
            removeItem: { try files.removeItem(at: $0) }
        )
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
    /// appears at the last moment. The fallback's folder may be on
    /// another volume, so its file is copied in beside the destination first
    /// and moved or replaced from there: a copy that fails leaves the old
    /// file as it was and nothing half-written in its place.
    @discardableResult
    func place() throws -> Bool {
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
                try ops.copyItem(stagedFile, copy)
                source = copy
            }
            if there {
                try ops.replaceItem(destination, source)
            } else {
                try ops.moveItem(source, destination)
            }
        } catch {
            if let beside { try? ops.removeItem(beside) }
            throw ExportStagingError.notPlaced(destination: destination, underlying: error)
        }
        return there
    }

    /// What a finished export wrote, placed: the CLI's `ipa-exported`, moved
    /// to `destination` and said as being there, or why it isn't.
    func finish(events: [BuildEvent]) -> Result<IPAExportedRecord, Error> {
        guard let made = AppDetailView.exportedRecord(in: events) else {
            return .failure(ExportStagingError.notReported(destination: destination))
        }
        do {
            let replaced = try place()
            return .success(made.placed(at: destination, replaced: replaced))
        } catch {
            return .failure(error)
        }
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
        let launched = launchBuild(appName: appName, delivery: delivery) {
            defer { staging?.discard() }
            await work()
        }
        guard launched else {
            staging?.discard()
            lastError = Self.exportNotStarted(appName: appName, busy: busy)
            return false
        }
        return true
    }

    static func exportNotStarted(appName: String, busy: Busy?) -> String {
        let why = busy?.explanation ?? "another build is running — wait for it to finish"
        return "Export IPA didn't start for \(appName): \(why), then export again."
    }
}
