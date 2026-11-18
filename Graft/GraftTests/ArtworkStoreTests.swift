import AppKit
import XCTest
@testable import Graft

final class ArtworkStoreTests: XCTestCase {
    /// A real one-pixel PNG on disk, so `NSImage(contentsOfFile:)` has
    /// something genuine to decode rather than a stub. Built with
    /// `NSBitmapImageRep` rather than embedded bytes, per the task's
    /// instruction — it's the same PNG any icon on disk would be.
    private func makePNGFixture() throws -> URL {
        let size = NSSize(width: 4, height: 4)
        let image = NSImage(size: size)
        image.lockFocus()
        NSColor.systemBlue.setFill()
        NSRect(origin: .zero, size: size).fill()
        image.unlockFocus()

        guard let tiff = image.tiffRepresentation,
              let rep = NSBitmapImageRep(data: tiff),
              let png = rep.representation(using: .png, properties: [:])
        else {
            XCTFail("couldn't render a PNG fixture")
            throw XCTSkip("PNG rendering unavailable")
        }

        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("artwork-fixture-\(UUID().uuidString).png")
        try png.write(to: url)
        return url
    }

    /// One `artwork` event, as a `/bin/sh -c` script — same pattern as
    /// `DevicesStoreTests`, since this project has no stub-runner type.
    ///
    /// `cached` is a parameter because the store branches on it: it is the
    /// CLI saying "I served this from my disk cache without re-resolving",
    /// which is what lets a repeat `load()` skip the decode.
    private func artworkScript(bundleId: String, path: String, cached: Bool = false) -> String {
        let json = #"{"type":"artwork","bundle_id":"\#(bundleId)","path":"\#(path)","source":"ipa","cached":\#(cached)}"#
        return "printf '\(json)\\n'"
    }

    @MainActor
    func test_load_caches_a_real_image_and_leaves_other_bundle_ids_nil() async throws {
        let pngURL = try makePNGFixture()
        defer { try? FileManager.default.removeItem(at: pngURL) }

        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = ArtworkStore(runner: runner)
        let script = artworkScript(bundleId: "com.bravo.app", path: pngURL.path)

        await store.load(argsOverride: ["-c", script])

        XCTAssertNotNil(store.image(for: "com.bravo.app"))
        XCTAssertNil(store.image(for: "com.other.App"), "no event named this bundle id")
    }

    @MainActor
    func test_a_path_that_does_not_exist_is_skipped_without_crashing() async {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = ArtworkStore(runner: runner)
        let script = artworkScript(
            bundleId: "com.missing.App",
            path: "/tmp/does-not-exist-\(UUID().uuidString).png"
        )

        await store.load(argsOverride: ["-c", script])

        XCTAssertNil(store.image(for: "com.missing.App"))
    }

    @MainActor
    func test_a_non_zero_exit_is_swallowed_with_no_image_and_no_crash() async {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = ArtworkStore(runner: runner)

        await store.load(argsOverride: ["-c", "exit 2"])

        XCTAssertNil(store.image(for: "com.bravo.app"))
    }

    @MainActor
    func test_generation_only_advances_when_an_image_is_actually_cached() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = ArtworkStore(runner: runner)
        XCTAssertEqual(store.generation, 0)

        // A load that cached nothing — a missing file — must not bump it.
        await store.load(argsOverride: [
            "-c", artworkScript(bundleId: "com.missing.App", path: "/tmp/nope-\(UUID().uuidString).png"),
        ])
        XCTAssertEqual(store.generation, 0, "nothing was cached, so nothing changed")

        // A load that does cache something must.
        let pngURL = try makePNGFixture()
        defer { try? FileManager.default.removeItem(at: pngURL) }
        await store.load(argsOverride: [
            "-c", artworkScript(bundleId: "com.bravo.app", path: pngURL.path),
        ])
        XCTAssertEqual(store.generation, 1)
    }

    @MainActor
    func test_a_repeat_load_of_an_already_cached_icon_does_not_advance_generation() async throws {
        let pngURL = try makePNGFixture()
        defer { try? FileManager.default.removeItem(at: pngURL) }

        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = ArtworkStore(runner: runner)

        // First run: the CLI resolved it (`cached: false`), so it gets decoded.
        await store.load(argsOverride: [
            "-c", artworkScript(bundleId: "com.bravo.app", path: pngURL.path),
        ])
        XCTAssertEqual(store.generation, 1)

        // Second run, same icon, now reported straight from the CLI's disk
        // cache. `load()` fires on several triggers — a new app, a finished
        // build, a staged IPA — and each one reports every configured app, so
        // without the skip this would re-decode and bump `generation`,
        // invalidating every view drawing an icon for apps the trigger had
        // nothing to do with.
        await store.load(argsOverride: [
            "-c", artworkScript(bundleId: "com.bravo.app", path: pngURL.path, cached: true),
        ])
        XCTAssertEqual(store.generation, 1, "an icon already in the cache is not a change")
        XCTAssertNotNil(store.image(for: "com.bravo.app"), "and it is still cached")
    }

    @MainActor
    func test_a_cached_report_still_loads_an_icon_the_store_does_not_have_yet() async throws {
        let pngURL = try makePNGFixture()
        defer { try? FileManager.default.removeItem(at: pngURL) }

        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = ArtworkStore(runner: runner)

        // The ordinary cold launch: the CLI's disk cache is warm — so every
        // record says `cached: true` — while this store's `NSCache` is empty.
        // Skipping on `cached` alone would leave the app with no icons at all.
        await store.load(argsOverride: [
            "-c", artworkScript(bundleId: "com.bravo.app", path: pngURL.path, cached: true),
        ])

        XCTAssertNotNil(store.image(for: "com.bravo.app"))
        XCTAssertEqual(store.generation, 1)
    }
}

/// `ArtworkStore.image(atPath:)` — the path-in-image-out helper
/// `InstalledAppsSection` uses for `DeviceAppRecord.iconPath`. See that
/// static func's doc for why it has a cache of its own rather than sharing
/// `cache` above, and for the measurements behind having one at all.
final class ArtworkStoreImageAtPathTests: XCTestCase {
    private func makePNGFixture(fill: NSColor = .systemBlue, side: CGFloat = 4) throws -> URL {
        let size = NSSize(width: side, height: side)
        let image = NSImage(size: size)
        image.lockFocus()
        fill.setFill()
        NSRect(origin: .zero, size: size).fill()
        image.unlockFocus()

        guard let tiff = image.tiffRepresentation,
              let rep = NSBitmapImageRep(data: tiff),
              let png = rep.representation(using: .png, properties: [:])
        else {
            XCTFail("couldn't render a PNG fixture")
            throw XCTSkip("PNG rendering unavailable")
        }

        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("artwork-atpath-fixture-\(UUID().uuidString).png")
        try png.write(to: url)
        return url
    }

    func test_a_real_png_decodes() throws {
        let pngURL = try makePNGFixture()
        defer { try? FileManager.default.removeItem(at: pngURL) }

        XCTAssertNotNil(ArtworkStore.image(atPath: pngURL.path))
    }

    /// A LiveContainer-delivered app on a real device listing has no
    /// `icon_path` at all, so this path is never called for that row — but a
    /// stale cached path (the file the CLI wrote since got pruned) must fail
    /// the same quiet way `ArtworkStore.load` does, not crash.
    func test_a_missing_path_returns_nil_without_crashing() {
        XCTAssertNil(
            ArtworkStore.image(atPath: "/tmp/no-such-icon-\(UUID().uuidString).png")
        )
    }

    /// The same path twice hands back the *same* image, not a second decode.
    ///
    /// Identity is the assertion because it is the whole property: two equal
    /// pictures would pass an equality check while still having cost a file
    /// read and an allocation, which is exactly what this cache exists to stop.
    /// `InstalledAppsSection` calls this from a row's `body`, once per row per
    /// re-render — measured at 8.87 ms for 67 rows uncached against 0.07 ms
    /// through the cache, all of it on the main actor.
    func test_the_same_path_is_read_once() throws {
        let pngURL = try makePNGFixture()
        defer { try? FileManager.default.removeItem(at: pngURL) }

        let first = try XCTUnwrap(ArtworkStore.image(atPath: pngURL.path))
        let second = try XCTUnwrap(ArtworkStore.image(atPath: pngURL.path))
        XCTAssertTrue(first === second, "a second call re-read a file that hadn't changed")
    }

    /// A rewritten file at the same path is a different icon, and must be
    /// re-read.
    ///
    /// Not hypothetical: `device_apps.icons_for` rewrites the PNG at an app's
    /// path whenever that rung refreshes its own entry, and `--refresh-artwork`
    /// rewrites it from a rung above. A cache keyed on path alone would serve
    /// the old picture for the rest of the session — the same staleness
    /// `ArtworkStore.load` avoids by not trusting `cached` on its own.
    ///
    /// The two fixtures differ in *size* as well as colour, so the assertion
    /// can name what came back rather than resting on object identity alone.
    func test_a_rewritten_file_is_read_again() throws {
        let pngURL = try makePNGFixture(fill: .systemBlue, side: 4)
        defer { try? FileManager.default.removeItem(at: pngURL) }

        let first = try XCTUnwrap(ArtworkStore.image(atPath: pngURL.path))

        let replacement = try makePNGFixture(fill: .systemRed, side: 8)
        defer { try? FileManager.default.removeItem(at: replacement) }
        try Data(contentsOf: replacement).write(to: pngURL)

        let second = try XCTUnwrap(ArtworkStore.image(atPath: pngURL.path))
        XCTAssertFalse(first === second, "the cache served an icon the file no longer holds")
        // Compared rather than pinned to 4 and 8: `lockFocus` renders at the
        // backing scale, so a fixture's pixel dimensions depend on the display
        // the tests happen to run on. The *ratio* does not.
        XCTAssertGreaterThan(second.size.width, first.size.width)
    }
}
