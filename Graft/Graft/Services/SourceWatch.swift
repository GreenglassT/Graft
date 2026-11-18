import Foundation

/// Whether the installed app is older than the source it was built from.
///
/// This replaces `scripts/tb-launcher`, a shell script that sat at
/// `Contents/MacOS/Graft` — the path `CFBundleExecutable` names — checked
/// the same mtimes, and then `exec`'d the real binary under a different name.
/// It worked, and it cost the app its notification identity: macOS identifies
/// an app to UserNotifications by its main executable, so a process running as
/// `<app>-bin` was not the executable the bundle declared and got a blank
/// authorization status. Doing the check *inside* the real binary keeps the
/// bundle's declared executable and the running process the same file, which is
/// the whole point.
enum SourceWatch {

    /// The repo this app was installed from, written into Info.plist by
    /// `scripts/graft-install`.
    ///
    /// Its absence is the off switch, and deliberately so: a build run straight
    /// from Xcode and the test bundle both lack the key, so neither can trigger
    /// a rebuild. Nothing else needs to know it is in a test.
    static var repoRoot: URL? {
        guard let path = Bundle.main.object(forInfoDictionaryKey: "GraftRepoPath") as? String,
              !path.isEmpty
        else { return nil }
        return URL(fileURLWithPath: path)
    }

    /// What can change the installed binary.
    ///
    /// `tb-launcher` also watched `.yaml`, and nothing under either watched
    /// path has ever had that extension — as `.yml` didn't either, until
    /// `watchedFiles` gave it something to match.
    /// `test_every_watched_extension_can_actually_match_something` is what
    /// keeps this set honest rather than aspirational.
    static let watchedExtensions: Set<String> = ["swift", "py", "yml"]

    /// The source trees that decide staleness, relative to the repo root.
    static let watchedRoots = ["Graft/Graft", "graft_ipa"]

    /// Individual files outside those trees that still change the binary.
    ///
    /// `project.yml` is the reason `.yml` is in `watchedExtensions`, and it
    /// sits at `Graft/project.yml` — one level *above* the watched root,
    /// so nothing under `watchedRoots` has ever had a `.yml` extension and the
    /// entry was decorative. Editing the file that tells XcodeGen which sources
    /// exist changed the binary and rebuilt nothing.
    ///
    /// Listed as a file rather than by widening the root to `Graft/`,
    /// which would pull in `GraftTests` — those are not in the app binary,
    /// so every test edit would rebuild and relaunch the app for a product that
    /// did not change.
    static let watchedFiles = ["Graft/project.yml"]

    /// The first file under `roots` modified after `reference`, or nil.
    ///
    /// Returns on the first hit rather than finding the newest — `find -newer
    /// -quit` for the same reason the shell version used it. Nothing needs to
    /// know *which* file is newest, only whether one exists, and stopping early
    /// keeps a cold launch from walking the whole tree.
    ///
    /// Unreadable directories are skipped rather than raised: a missing repo
    /// (moved, on an unmounted volume) must read as "nothing to rebuild", not
    /// as an error on a path the user did not ask anything of.
    static func firstFile(
        under roots: [URL],
        newerThan reference: Date,
        extensions: Set<String> = watchedExtensions,
        files: [URL] = [],
        now: Date = Date()
    ) -> URL? {
        // A source file dated in the *future* is one no rebuild can ever make
        // the binary newer than, so treating it as stale is a rebuild-relaunch
        // cycle with no end — only throttled by the attempt marker, three
        // minutes apart, forever. Backups restored with skewed times and
        // archives extracted without `-m` both produce it. Ignoring such a file
        // means the app may run slightly stale, which the user can fix by
        // touching the file or running the script; the loop they cannot.
        func isStale(_ url: URL) -> Bool {
            guard let values = try? url.resourceValues(
                forKeys: [.contentModificationDateKey, .isRegularFileKey]
            ), values.isRegularFile == true, let modified = values.contentModificationDate
            else { return false }
            return modified > reference && modified <= now
        }

        for file in files where extensions.contains(file.pathExtension) {
            if isStale(file) { return file }
        }

        for root in roots {
            guard let walk = FileManager.default.enumerator(
                at: root,
                includingPropertiesForKeys: [.contentModificationDateKey, .isRegularFileKey],
                options: [.skipsHiddenFiles]
            ) else { continue }

            for case let url as URL in walk {
                guard extensions.contains(url.pathExtension) else { continue }
                if isStale(url) { return url }
            }
        }
        return nil
    }

    /// Where `graft-install` puts the app, and therefore the only bundle a rebuild
    /// can actually replace.
    static let installedBundle = URL(fileURLWithPath: "/Applications/Graft.app")

    /// Whether this process is the installed copy that `graft-install` writes to.
    ///
    /// `GraftRepoPath` is baked into the bundle, so it travels with any *copy* of
    /// the app. A copy run from ~/Applications or an external drive would
    /// notice the same stale source, rebuild — into /Applications, since that
    /// is the only place the script installs — and reopen itself, still stale,
    /// on every launch forever. Rebuilding is only meaningful for the bundle
    /// the rebuild lands on.
    static var isInstalledCopy: Bool {
        Bundle.main.bundleURL.standardizedFileURL == installedBundle.standardizedFileURL
    }

    /// The source file that makes the running binary stale, or nil if it isn't.
    static func staleSource(now: Date = Date()) -> URL? {
        guard isInstalledCopy,
              let repoRoot,
              let binary = Bundle.main.executableURL,
              let built = try? binary.resourceValues(forKeys: [.contentModificationDateKey])
                  .contentModificationDate
        else { return nil }
        // A binary dated in the future would make everything look stale
        // forever; treat it as fresh rather than rebuilding in a loop.
        guard built <= now else { return nil }
        return firstFile(
            under: watchedRoots.map { repoRoot.appending(path: $0) },
            newerThan: built,
            files: watchedFiles.map { repoRoot.appending(path: $0) },
            now: now
        )
    }

    // MARK: - The loop guard

    /// Touched before a rebuild starts, consulted before starting one, and
    /// **cleared as soon as one succeeds**.
    ///
    /// Without it a *failing* build is an infinite loop: the rebuild leaves the
    /// binary untouched, so the relaunched app is still stale, tries again, and
    /// the machine never gets back to a usable app.
    ///
    /// Clearing it on success is what keeps that guard from becoming a second
    /// bug. Left in place it suppressed every rebuild for three minutes after
    /// *any* attempt, including one that worked — so editing a file a minute
    /// after a successful rebuild launched the previous binary silently, which
    /// is the failure this whole mechanism exists to prevent. `tb-launcher` had
    /// no window at all; it ran `find -newer` on every launch. The window is
    /// only for the case where trying again would fail the same way.
    static var attemptMarker: URL {
        URL.cachesDirectory.appending(path: "Graft/last-rebuild-attempt")
    }

    /// Whether a rebuild was attempted recently enough that another would be
    /// the same attempt repeating rather than a new one.
    static func recentlyAttempted(
        within window: TimeInterval = 180,
        now: Date = Date(),
        marker: URL? = nil
    ) -> Bool {
        let url = marker ?? attemptMarker
        // `FileManager`, not `url.resourceValues`: a `URL` caches the resource
        // values it has already been asked for, so re-reading the same URL
        // instance after `recordAttempt` rewrote the file hands back the
        // timestamp from before the write. Production happens to construct a
        // fresh URL every call and would not notice, which is exactly the kind
        // of accident that stops being true later.
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: url.path),
              let stamped = attributes[.modificationDate] as? Date
        else { return false }
        // `abs`, so the window is symmetric: a marker stamped slightly *ahead*
        // of now — the ordinary case after an NTP correction — still counts as
        // the attempt that just happened rather than as no attempt at all,
        // which is the direction that loops.
        //
        // A marker far in the future (a restored backup, a badly wrong clock)
        // falls outside the window and permits an attempt. That is deliberate
        // and self-healing: the attempt rewrites the marker with the current
        // time, so the state corrects itself after one rebuild instead of
        // suppressing every rebuild until the clock catches up.
        return abs(now.timeIntervalSince(stamped)) < window
    }

    static func recordAttempt(marker: URL? = nil) {
        let url = marker ?? attemptMarker
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true
        )
        try? Data().write(to: url)
    }

    /// Forget the attempt, so the next stale source rebuilds immediately.
    static func clearAttempt(marker: URL? = nil) {
        try? FileManager.default.removeItem(at: marker ?? attemptMarker)
    }
}
