import AppKit
import Foundation
import Observation

private typealias Flag = CLIContract.Flag

/// Loads the real app icons `graft --artwork` resolves, and hands them
/// back to `AppIcon`.
///
/// **There is no `lastError` here, deliberately.** Every way this can fail —
/// a `path` the cascade named but the file is missing, JSON the CLI never
/// wrote, a subprocess that exited non-zero, a `graft` too old to know
/// `--artwork` at all — collapses to the same one consequence: `image(for:)`
/// returns nil and `AppIcon` draws the same letter tile it drew before this
/// feature existed. That tile is not a degraded state a user needs telling
/// about; it is the fallback this whole feature was layered on top of, and an
/// alert popping up over a missing icon would interrupt someone about
/// something they can already see on screen and cannot do anything about
/// beyond what a Refresh already offers. So failures are swallowed here, on
/// purpose, all the way down — this store either has an image or it doesn't.
///
/// One asymmetry that swallowing flattens, recorded so it is a decision rather
/// than an oversight: the CLI already distinguishes a *miss* (nothing had this
/// app's icon — silent by design, the letter tile is the right answer) from a
/// *cache-write failure* (a full disk, a root-owned cache directory — reported
/// as a `log` event, because the machine has a real fault). This store drops
/// the second along with the first. Surfacing it would mean a `notes` or
/// `lastError` channel, and the tile is still the correct thing on screen.
@MainActor
@Observable
final class ArtworkStore {
    /// Bumped only when a `load()` actually cached at least one image. The
    /// value itself means nothing; it exists to be read.
    ///
    /// This is the one piece of state `image(for:)` and every view drawing an
    /// icon actually observes — see the comment there for why the cache
    /// itself can't be.
    private(set) var generation = 0

    /// Where loaded icons live. `NSCache`, not a `@Observable` dictionary:
    /// `NSImage` is a reference type so mutating a dictionary's values
    /// wouldn't even trigger `@Observable`'s own change tracking reliably,
    /// and `NSCache` can evict entries under memory pressure at any time —
    /// which would make a dictionary an outright lie about what's still
    /// cached one line after `@Observable` reported it. `generation` is the
    /// thing this type actually promises to keep in sync with reality.
    @ObservationIgnored
    private let cache = NSCache<NSString, NSImage>()

    private let runner: CLIRunner

    init(runner: CLIRunner) {
        self.runner = runner
    }

    /// The cached icon for `bundleId`, or nil when none has been resolved (or
    /// resolution failed, or the cache evicted it — the caller can't tell
    /// these apart and doesn't need to).
    ///
    /// `_ = generation` is not dead code. `NSCache` is invisible to
    /// `@Observable` — mutating it registers no dependency — so without this
    /// touch, a SwiftUI view calling `image(for:)` before an icon arrives
    /// would read nil once, register no dependency on anything, and never be
    /// asked to redraw when `load()` later populates the cache. Reading
    /// `generation` here is what makes that redraw happen: `load()` bumping
    /// it is the write half of the same contract.
    func image(for bundleId: String) -> NSImage? {
        _ = generation
        return cache.object(forKey: bundleId as NSString)
    }

    /// One image per cache file, keyed by path and stamped with what the file
    /// looked like when it was read. See `image(atPath:)`.
    private final class PathEntry {
        let image: NSImage
        let stamp: String

        init(image: NSImage, stamp: String) {
            self.image = image
            self.stamp = stamp
        }
    }

    /// The path-keyed half of this store, for icons the CLI resolved for us.
    ///
    /// Separate from `cache` above rather than shared with it, because the two
    /// are keyed by different things for different callers — bundle id for
    /// *configured* apps walked through the cascade here, path for apps a phone
    /// listed that this Mac mostly does not configure at all. One cache would
    /// have to pick a key and lie to one of them.
    ///
    /// `static`, so it outlives any one pane: opening a phone, looking at an
    /// app, and coming back re-creates the views but not this. Unbounded on
    /// purpose — `NSCache` evicts under memory pressure, which is the whole
    /// reason it is an `NSCache` and not a dictionary, and a count limit picked
    /// out of the air would evict on a bench that has the memory to spare.
    ///
    /// `nonisolated(unsafe)` because a `static` on a `@MainActor` type is
    /// main-actor isolated by default, and `image(atPath:)` is deliberately not
    /// (see its doc). The "unsafe" is discharged rather than waved through:
    /// `NSCache` does its own locking and is documented thread-safe, and every
    /// value in it is immutable once stored. Without the annotation this is a
    /// warning today and an error under the Swift 6 language mode.
    private nonisolated(unsafe) static let pathCache = NSCache<NSString, PathEntry>()

    /// Load an icon off disk by path — memoised, and re-read when the file
    /// underneath it changes.
    ///
    /// For `DeviceAppRecord.iconPath`: `--device-apps --with-icons` resolves
    /// every listed app's icon through the same cascade `--artwork` does, but
    /// hands back the *finished* path on the `device-app` event itself rather
    /// than a bundle id to look up. That is why it does not go through `cache`
    /// above — see `pathCache` — but it was never a reason to skip caching
    /// altogether, and this used to do exactly that, on the argument that
    /// decoding a small local PNG is the cheap half of showing an icon.
    ///
    /// **Measured, because 67 files per re-render is a checkable claim.**
    /// `InstalledAppsSection` draws its rows in a plain `VStack` and reads this
    /// from `DeviceAppTile.body`, so every re-render of the pane — the decrypt
    /// card's `decrypting` flipping at the start and end of a dump, a store's
    /// `isLoading` toggling on an escalated poll — re-read every row. Against
    /// this Mac's real artwork cache (67 PNGs, ~81KB each, page cache warm):
    ///
    /// - uncached `NSImage(contentsOfFile:)` ×67: **8.87 ms** median
    /// - the same, then each drawn at 20pt as a row does: **51.98 ms**
    /// - through this cache: **0.07 ms**, and **17.24 ms** with the draw
    ///
    /// All of it on the main actor, all of it per re-render. The read alone is
    /// ~123× cheaper here; counting the draw, the cache saves ~35 ms, which is
    /// two dropped frames at 60Hz for a list nobody scrolled.
    ///
    /// **Stamped with mtime and size rather than trusted by path**, because
    /// these paths are not stable content: `device_apps.icons_for` rewrites the
    /// PNG at the same path when this rung refreshes its own entry, and
    /// `--refresh-artwork` rewrites it from a different rung. A path-only cache
    /// would serve the old picture for the rest of the session, which is the
    /// failure mode `ArtworkStore.load` guards against with `record.cached` and
    /// the same one this has to. The stamp costs one `stat` per row — 0.049 ms
    /// for all 67, measured above — against the 8.87 ms of reads it avoids.
    ///
    /// A file that cannot be stat-ed is still read and simply not cached: a
    /// stale `icon_path` whose file was pruned must keep returning nil quietly.
    ///
    /// `nonisolated` — touches no *instance* state of this `@MainActor` type,
    /// and `NSCache` is thread-safe. Pinning it to the main actor anyway would
    /// be an arbitrary restriction on a pure path-to-image function.
    nonisolated static func image(atPath path: String) -> NSImage? {
        let key = path as NSString
        let stamp = fileStamp(path)
        if let hit = pathCache.object(forKey: key), let stamp, hit.stamp == stamp {
            return hit.image
        }
        guard let image = NSImage(contentsOfFile: path) else { return nil }
        if let stamp {
            pathCache.setObject(PathEntry(image: image, stamp: stamp), forKey: key)
        }
        return image
    }

    /// What the file at `path` looks like right now, or nil if it isn't there.
    ///
    /// mtime *and* size, and mtime to the nanosecond: an atomic rename over the
    /// same path within one filesystem timestamp tick is exactly what
    /// `artwork._atomic_write` does, and a second-resolution stamp would miss
    /// it. Size alone would miss a re-resolve that happened to land on the same
    /// byte count; together they are as close to content identity as a `stat`
    /// gets, and hashing 5.5MB of PNG per re-render would cost more than the
    /// reads this exists to avoid.
    private nonisolated static func fileStamp(_ path: String) -> String? {
        var info = stat()
        guard stat(path, &info) == 0 else { return nil }
        return "\(info.st_mtimespec.tv_sec).\(info.st_mtimespec.tv_nsec):\(info.st_size)"
    }

    /// Run `--artwork` (or `--refresh-artwork` with `refresh: true`), and
    /// cache every icon it reports.
    ///
    /// `app` is the app's **config name** — the key in `config.yaml`, which is
    /// what `--app` means everywhere in this CLI — and NOT its bundle id, even
    /// though the icons this caches are keyed by bundle id and `image(for:)`
    /// takes one. Passing a bundle id makes the CLI exit non-zero with
    /// "app '…' is not configured", which this method swallows by design, so
    /// the mistake shows up as a silent no-op rather than an error. Nothing
    /// passes `app:` today; the first caller that does should read this line.
    ///
    /// Every failure — a non-zero exit, an event whose `path` doesn't decode
    /// to a real image — is swallowed. See the type doc for why.
    func load(app: String? = nil, refresh: Bool = false, argsOverride: [String]? = nil) async {
        var args = [Flag.json, Flag.artwork]
        if let app { args += [Flag.app, app] }
        if refresh { args.append(Flag.refreshArtwork) }

        guard let events = try? await runner.runEvents(
            argsOverride ?? args, failing: "Load artwork"
        ) else {
            return
        }

        var changed = false
        for event in events {
            guard case let .artwork(record) = event else { continue }
            let key = record.bundleId as NSString
            // Already have this exact icon: skip the disk read, the decode, and
            // — the part that matters — the `changed` flag. `load()` now runs
            // on several triggers, and without this every one of them re-reads
            // and re-decodes every icon the CLI reports, then bumps
            // `generation`, which invalidates every view drawing an icon for
            // apps the trigger had nothing to do with.
            //
            // Both halves of the condition are needed. `record.cached` alone
            // is not enough: it says the *CLI* served the PNG from its disk
            // cache without re-resolving, which is the ordinary state on a
            // cold launch — the disk cache is warm while this `NSCache` is
            // still empty, and trusting it would leave the store with no
            // images at all. And `cache.object(forKey:)` alone is not enough
            // either: a `cached: false` record means the CLI just rewrote that
            // PNG, so whatever is in memory is the old picture.
            if record.cached, cache.object(forKey: key) != nil { continue }
            guard let image = NSImage(contentsOfFile: record.path) else { continue }
            cache.setObject(image, forKey: key)
            changed = true
        }
        // Bumped only when something was actually cached: a load that found
        // nothing (an app with no configured artwork this run, a CLI that
        // answered with zero hits) has nothing new for any view to redraw
        // for, and bumping anyway would invalidate every icon on screen for
        // no reason.
        if changed {
            generation += 1
        }
    }
}
