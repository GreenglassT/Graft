import Darwin
import Foundation

/// Settings › Command-line tool: `graft` in Terminal, as a link at
/// `~/.local/bin/graft` to the app's own CLI (`GraftExecutable.bundled`).
///
/// A link, not a copy: the launcher finds the embedded Python from its own
/// real path, and a link keeps Terminal's `graft` the same version as the
/// app's through every update. `~/.local/bin` because pipx puts its `graft`
/// there too, so a user who had the pipx CLI already has it on PATH.
///
/// Pure decisions over a `FileSystem` seam, so tests never touch the real
/// `~/.local/bin`; `FileSystem.real` is the thin adapter the app uses.
enum CommandLineTool {
    /// What is at a path, without following a final link.
    enum Entry: Equatable {
        case none
        case file
        case directory
        case symlink(to: String)
        /// A socket, a FIFO, a device.
        case other
    }

    struct FileSystem {
        var entry: (String) -> Entry
        /// Make the folder at `path`, and any missing above it, with `mode`.
        var makeDirectory: (_ path: String, _ mode: mode_t) throws -> Void
        /// Remove a file or a link. Never a folder.
        var removeFile: (String) throws -> Void
        var makeSymlink: (_ path: String, _ destination: String) throws -> Void
        /// Whether something is at `path`, following links: a link whose
        /// destination is gone is not there.
        var exists: (String) -> Bool
        /// A file this user can run, following links: what a shell would run
        /// for a command found at `path`.
        var isRunnable: (String) -> Bool
    }

    enum Status: Equatable {
        case notInstalled
        /// The link points at this app's `Contents/Helpers/graft`.
        case installed
        /// Some other `graft` is there: a regular file (its own path), or a
        /// link elsewhere (where it points), e.g. pipx's venv or another copy
        /// of Graft.app.
        case otherGraft(String)
        /// A link whose destination (given) is gone: most often this app's
        /// own link, left behind when Graft.app was moved, renamed, or run
        /// from App Translocation's temporary copy. Nothing runs through it,
        /// so Reinstall replaces it without asking.
        case broken(String)
        /// A folder, or something else that is not a file, is in the way.
        case blockedByNonFile
    }

    enum InstallError: LocalizedError, Equatable {
        case wouldReplace(String)
        case blocked(String)

        var errorDescription: String? {
            switch self {
            case let .wouldReplace(path):
                return "Another graft is at \(path). Replace it to use this app's."
            case let .blocked(path):
                return "\(path) is a folder, or something else that isn't a file. "
                    + "Graft won't delete it. Move it out of the way, then try again."
            }
        }
    }

    static func status(link: String, target: String, in fs: FileSystem) -> Status {
        switch fs.entry(link) {
        case .none:
            return .notInstalled
        case .file:
            return .otherGraft(link)
        case .directory, .other:
            return .blockedByNonFile
        case let .symlink(destination):
            let resolved = resolve(destination, from: link)
            // Before the comparison: a link to this app's own path that
            // isn't there any more runs nothing, and "Installed" would say
            // Terminal runs this app's graft.
            guard fs.exists(resolved) else { return .broken(destination) }
            return resolved == (target as NSString).standardizingPath ? .installed : .otherGraft(destination)
        }
    }

    /// Where a link at `link` pointing to `destination` leads: a relative
    /// link is followed from the folder it is in.
    private static func resolve(_ destination: String, from link: String) -> String {
        let base = (link as NSString).deletingLastPathComponent
        let absolute = destination.hasPrefix("/")
            ? destination : (base as NSString).appendingPathComponent(destination)
        return (absolute as NSString).standardizingPath
    }

    /// The `graft` a shell runs instead of the link at `link`, because its
    /// folder comes first on `loginPATH`; nil when none does, when `link`'s
    /// folder isn't on the PATH at all (`pathAdvice` covers that), or when the
    /// login shell never answered.
    ///
    /// A `graft` that is itself a link to `target` is this app's too, so it
    /// shadows nothing.
    static func shadowingGraft(
        loginPATH: String?, link: String, target: String, in fs: FileSystem
    ) -> String? {
        guard let loginPATH else { return nil }
        let bin = (link as NSString).deletingLastPathComponent
        let name = (link as NSString).lastPathComponent
        let ours = (target as NSString).standardizingPath
        let entries = pathEntries(loginPATH)
        guard let position = entries.firstIndex(of: bin) else { return nil }
        var seen = Set<String>()
        for entry in entries[..<position] where entry.hasPrefix("/") && seen.insert(entry).inserted {
            let candidate = (entry as NSString).appendingPathComponent(name)
            guard fs.isRunnable(candidate) else { continue }
            if case let .symlink(destination) = fs.entry(candidate),
               resolve(destination, from: candidate) == ours {
                continue
            }
            return candidate
        }
        return nil
    }

    /// A PATH's folders, each without a trailing slash.
    private static func pathEntries(_ path: String) -> [String] {
        path.split(separator: ":").map { entry -> String in
            var entry = String(entry)
            while entry.count > 1 && entry.hasSuffix("/") { entry.removeLast() }
            return entry
        }
    }

    /// Put the link at `link`, pointing at `target`, and say what is there now.
    ///
    /// - The folder above `link` (`~/.local/bin`) is made, 0755, when it is
    ///   missing.
    /// - Another `graft` is replaced only with `replace`, which the section
    ///   passes after the user confirmed.
    /// - A folder is never removed, whatever `replace` says.
    /// - This app's own link is made again (Reinstall), without asking, and so
    ///   is a broken link, which runs nothing.
    @discardableResult
    static func install(link: String, target: String, replace: Bool, in fs: FileSystem) throws -> Status {
        switch status(link: link, target: target, in: fs) {
        case .blockedByNonFile:
            throw InstallError.blocked(link)
        case let .otherGraft(path) where !replace:
            throw InstallError.wouldReplace(path)
        case .otherGraft, .installed, .broken:
            try fs.removeFile(link)
        case .notInstalled:
            let bin = (link as NSString).deletingLastPathComponent
            if fs.entry(bin) == .none {
                try fs.makeDirectory(bin, 0o755)
            }
        }
        try fs.makeSymlink(link, target)
        return status(link: link, target: target, in: fs)
    }

    /// The line to add to the shell's startup file so Terminal finds the
    /// link, or nil when `~/.local/bin` is already on the login shell's PATH.
    ///
    /// Nil too when the login shell never answered (`loginPATH` nil): the
    /// section says nothing rather than claim the folder is missing.
    static func pathAdvice(loginPATH: String?, shell: String, home: String) -> String? {
        guard let loginPATH else { return nil }
        let bin = home + "/.local/bin"
        if pathEntries(loginPATH).contains(bin) { return nil }
        return LoginShell.isCshFamily(shell)
            ? #"setenv PATH "$HOME/.local/bin:$PATH""#
            : #"export PATH="$HOME/.local/bin:$PATH""#
    }

    /// Where the advice goes, by the shell's name.
    static func startupFile(shell: String) -> String {
        switch (shell as NSString).lastPathComponent {
        case "zsh": return "~/.zshrc"
        case "bash": return "~/.bash_profile"
        case "tcsh": return "~/.tcshrc"
        case "csh": return "~/.cshrc"
        default: return "your shell's startup file"
        }
    }

    /// Everything the section needs to know, so a test can hand it a fake.
    struct Context {
        let home: String
        /// This app's `Contents/Helpers/graft`.
        let target: String
        /// The login shell, for the form of the PATH line.
        let shell: String
        let fs: FileSystem
        /// The login shell's PATH: the answer read at launch, or, with
        /// `askShellAgain`, a new one (the section's Check again).
        let loginPATH: (_ askShellAgain: Bool) async -> String?
        /// After `loginPATH(true)`: whether the shell was asked too recently
        /// to ask again, so the PATH is the last answer
        /// (`ShellEnvironment.askedTooRecentlyNote`).
        var askedTooRecently: () -> Bool = { false }
        /// Where this copy of the app runs from: a link into a temporary or
        /// read-only copy is not offered.
        var location: AppLocation = .settled

        var link: String { home + "/.local/bin/graft" }

        /// `path` with the home folder written as `~`.
        func abbreviated(_ path: String) -> String {
            path.hasPrefix(home + "/") ? "~" + path.dropFirst(home.count) : path
        }

        /// The section's context for the app at `bundleURL`.
        ///
        /// `loginPATH(true)` refreshes `shell` before reading it, so a user who
        /// added the PATH line the section suggested can press Check again
        /// rather than relaunch. Not each time the section appears: that ran
        /// the user's startup files on every visit to Settings (whole-diff
        /// review, app-1; `ShellEnvironment`).
        static func forApp(
            bundleURL: URL, shell environment: ShellEnvironment,
            home: String = FileManager.default.homeDirectoryForCurrentUser.path,
            fs: FileSystem
        ) -> Context {
            Context(
                home: home,
                target: GraftExecutable.bundled(in: bundleURL).path,
                shell: LoginShell.userShell(),
                fs: fs,
                loginPATH: { askShellAgain in
                    if askShellAgain { environment.refresh() }
                    return await environment.value?["PATH"]
                },
                askedTooRecently: { environment.lastRefreshWasTooSoon },
                location: AppLocation.of(bundleURL)
            )
        }

        /// The real one: `.shared`'s login shell and the real filesystem,
        /// which Install writes a link into. Offered only where
        /// `CommandLineToolSection.isOffered` (a release build), so a Debug
        /// test host never builds it.
        static let thisApp = forApp(bundleURL: Bundle.main.bundleURL, shell: .shared, fs: .real)
    }
}

/// Where a copy of Graft.app is running from, as far as a link to it cares.
///
/// A quarantined app opened where it was unzipped — Graft.zip, in Downloads —
/// is run by Gatekeeper from a randomized, read-only copy under
/// `/private/var/folders/…/AppTranslocation/`, which goes away when the app
/// quits or the Mac restarts. `Bundle.main.bundleURL` is that copy. A link
/// into it dangles a day later, and one into a disk image dangles once it is
/// ejected, so neither is offered.
enum AppLocation: Equatable {
    case settled
    /// App Translocation's temporary copy.
    case translocated
    /// A read-only volume: a disk image, a locked share.
    case readOnly

    static func of(
        _ bundleURL: URL,
        isTranslocated: (URL) -> Bool = AppLocation.isTranslocated,
        isReadOnly: (String) -> Bool = AppLocation.isReadOnly
    ) -> AppLocation {
        if isTranslocated(bundleURL) { return .translocated }
        if isReadOnly(bundleURL.path) { return .readOnly }
        return .settled
    }

    /// A path through `/AppTranslocation/`, or the system's own answer
    /// (`SecTranslocateIsTranslocatedURL`) for any other.
    ///
    /// The Security function has no Swift declaration, so it is looked up
    /// by name, and a lookup that fails leaves the path to decide.
    static func isTranslocated(_ url: URL) -> Bool {
        if url.path.contains("/AppTranslocation/") { return true }
        guard let check = secTranslocateIsTranslocatedURL else { return false }
        var translocated = false
        return check(url as CFURL, &translocated, nil) != 0 && translocated
    }

    /// `Boolean SecTranslocateIsTranslocatedURL(CFURLRef, bool *, CFErrorRef *)`.
    typealias IsTranslocatedURL = @convention(c) (
        CFURL, UnsafeMutablePointer<Bool>, UnsafeMutablePointer<Unmanaged<CFError>?>?
    ) -> UInt8

    /// Looked up once; nil if this macOS has no such symbol.
    static let secTranslocateIsTranslocatedURL: IsTranslocatedURL? = {
        guard let security = dlopen("/System/Library/Frameworks/Security.framework/Security", RTLD_LAZY),
              let symbol = dlsym(security, "SecTranslocateIsTranslocatedURL")
        else { return nil }
        return unsafeBitCast(symbol, to: IsTranslocatedURL.self)
    }()

    /// Whether nothing can be written where `path` is: a volume mounted
    /// read-only, or a write check that answers "read-only file system".
    /// A folder the user may not write to for want of permission is not
    /// read-only: an app an administrator put in /Applications is settled.
    static func isReadOnly(_ path: String) -> Bool {
        var volume = statfs()
        if statfs(path, &volume) == 0, volume.f_flags & UInt32(MNT_RDONLY) != 0 { return true }
        return access(path, W_OK) != 0 && errno == EROFS
    }
}

extension CommandLineTool.FileSystem {
    /// The disk.
    static let real = CommandLineTool.FileSystem(
        entry: { path in
            var info = stat()
            guard lstat(path, &info) == 0 else { return .none }
            switch info.st_mode & S_IFMT {
            case S_IFREG: return .file
            case S_IFDIR: return .directory
            case S_IFLNK:
                let destination = (try? FileManager.default.destinationOfSymbolicLink(atPath: path)) ?? ""
                return .symlink(to: destination)
            default: return .other
            }
        },
        makeDirectory: { path, mode in
            try FileManager.default.createDirectory(
                atPath: path, withIntermediateDirectories: true,
                attributes: [.posixPermissions: Int(mode)]
            )
        },
        // `unlink`, which refuses a folder, rather than `removeItem`, which
        // would empty one first: the guarantee holds here as well as in
        // `install`.
        removeFile: { path in
            guard unlink(path) == 0 else {
                throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno), userInfo: [NSFilePathErrorKey: path])
            }
        },
        makeSymlink: { path, destination in
            try FileManager.default.createSymbolicLink(atPath: path, withDestinationPath: destination)
        },
        exists: { path in
            var info = stat()
            return stat(path, &info) == 0
        },
        isRunnable: GraftExecutable.isRunnableFile
    )
}
