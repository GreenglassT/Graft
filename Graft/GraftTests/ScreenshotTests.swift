import AppKit
import SwiftUI
import XCTest
@testable import Graft

/// The marketing screenshots: the app's real views, fed placeholder data by a
/// stand-in `graft`, drawn by AppKit into @2x PNGs.
///
/// **Opt-in.** Skipped unless the test host has `GRAFT_SCREENSHOT_DIR`, the
/// folder the PNGs go to. `xcodebuild` passes a `TEST_RUNNER_`-prefixed
/// variable on without the prefix, so:
///
///     TEST_RUNNER_GRAFT_SCREENSHOT_DIR=/path/to/dir \
///       run-swift-tests.sh <worktree> <derived-data> -only-testing:GraftTests/ScreenshotTests
///
/// Everything on screen is placeholder data: apps `clips`, `forum`, `audio`,
/// `photos` and `video` under `com.example.*`, the tweaks `example-tweak` and
/// `another-tweak.dylib`, a device called "iPhone", and icons drawn here as a
/// colored square with an SF Symbol. Nothing reads the real config, the real
/// CLI, a device or the network: every command the views run goes to the
/// script below, which answers from files this test writes.
///
/// Drawn by AppKit (`cacheDisplay(in:to:)` on the window's frame view, so the
/// title bar comes along) and never captured from the screen: that needs the
/// Screen Recording permission, and asking for it is a dialog. AppKit can't
/// draw what the window server composites, so the sidebar's glass, its
/// selection highlight and its vibrant text are put back by hand
/// (`png(ofWindow:)`). The window draws as the front one without the test host
/// being activated (`FrontWindow`).
///
/// Dark only, because the app is: `GraftApp` sets `.preferredColorScheme(.dark)`
/// and `Palette` is a fixed dark palette.
@MainActor
final class ScreenshotTests: XCTestCase {

    // MARK: The placeholder data

    private struct PlaceholderApp {
        let name: String
        let symbol: String
        let colors: (top: NSColor, bottom: NSColor)
        let delivery: String
        let tweaks: [String]
        let state: String

        var bundleId: String { "com.example.\(name)" }
    }

    private static let apps: [PlaceholderApp] = [
        PlaceholderApp(name: "clips", symbol: "bolt.fill",
                       colors: (rgb(0xFB7185), rgb(0xE11D48)), delivery: "livecontainer",
                       tweaks: ["example-tweak.deb", "another-tweak.dylib"], state: "current"),
        PlaceholderApp(name: "forum", symbol: "bubble.left.and.bubble.right.fill",
                       colors: (rgb(0x4ADE80), rgb(0x16A34A)), delivery: "livecontainer",
                       tweaks: ["example-tweak.deb"], state: "current"),
        PlaceholderApp(name: "audio", symbol: "waveform",
                       colors: (rgb(0xFDBA74), rgb(0xEA580C)), delivery: "livecontainer",
                       tweaks: ["example-tweak.deb"], state: "stale"),
        PlaceholderApp(name: "photos", symbol: "camera.aperture",
                       colors: (rgb(0x7DD3FC), rgb(0x0284C7)), delivery: "sideload",
                       tweaks: ["another-tweak.dylib"], state: "current"),
        PlaceholderApp(name: "video", symbol: "play.fill",
                       colors: (rgb(0xC4B5FD), rgb(0x7C3AED)), delivery: "livecontainer",
                       tweaks: [], state: "never"),
    ]

    private static let selectedApp = "clips"
    private static let udid = "00000000-0000000000000000"
    private static let searchTerm = "example"
    private static let searchCountry = "US"

    private static func rgb(_ hex: Int) -> NSColor {
        NSColor(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255,
                green: CGFloat((hex >> 8) & 0xFF) / 255,
                blue: CGFloat(hex & 0xFF) / 255, alpha: 1)
    }

    // MARK: The shots

    /// The main window on an app: the sidebar with a phone connected, the
    /// App Store line, the attached tweaks, the delivery card and Build & Install.
    func test_main_window() async throws {
        let out = try Self.outputDirectory()
        let cli = try fakeCLI()
        let defaults = try scratchDefaults()
        let store = ConfigStore(runner: cli.runner, notify: { _ in })
        let root = ContentView(store: store, selection: .app(Self.selectedApp), adminShell: NoAdminShell())
            .modifier(AppChrome(defaults: defaults))
            // The device poll runs only in the front window. This one draws as
            // the front window (`FrontWindow`) without the test host being
            // brought in front of whatever the Mac is doing.
            .environment(\.controlActiveState, .key)
        let window = Self.documentWindow(root, size: NSSize(width: 900, height: 612))
        defer { window.orderOut(nil) }

        try await waitFor("the app list") { store.apps.count == Self.apps.count }
        try await waitFor("the app pane's loads") {
            let argv = cli.argv().map { " \($0) " }
            return [" --devices ", " --artwork ", " --cached-ipa ", " --appstore-check ", " --tweaks ", " --builds "]
                .allSatisfy { flag in argv.contains { $0.contains(flag) } }
        }
        try await settle(window, seconds: 2)
        print("ScreenshotTests ran:\n" + cli.argv().joined(separator: "\n"))

        let url = out.appending(path: "graft-main-window.png")
        try Self.png(ofWindow: window).write(to: url)
        print("ScreenshotTests wrote \(url.path)")
    }

    /// Add App's Search, with placeholder results and the first one picked.
    func test_add_app_search() async throws {
        let out = try Self.outputDirectory()
        let cli = try fakeCLI()
        let defaults = try scratchDefaults()
        let store = ConfigStore(runner: cli.runner, notify: { _ in })
        let appStore = AppStoreStore(runner: cli.runner, shell: NoAdminShell())
        let root = AddAppSheet(store: store, appStore: appStore)
            .modifier(AppChrome(defaults: defaults))
        let window = Self.sheetWindow(root, width: 480)
        defer { window.orderOut(nil) }
        try await settle(window, seconds: 0.5)

        // The sheet resets the search as it appears, back to this Mac's region:
        // pin the store, so the picture doesn't depend on where it was taken.
        appStore.search.select(country: Self.searchCountry, term: "")
        // Typed, as a user would: into the field's editor, so the sheet's own
        // binding carries the term to the search.
        let field = try XCTUnwrap(Self.textFields(in: window.contentView).first, "the search field")
        XCTAssertTrue(window.makeFirstResponder(field))
        let editor = try XCTUnwrap(window.firstResponder as? NSTextView, "the field editor")
        editor.insertText(Self.searchTerm, replacementRange: NSRange(location: NSNotFound, length: 0))
        try await waitFor("the search results") {
            if case .results = appStore.search.state { return true }
            return false
        }
        // Return in the field picks the first result, which offers Get.
        editor.insertNewline(nil)
        try await waitFor("the App Store status for Get") { appStore.hasLoadedStatus }
        window.makeFirstResponder(nil)
        try await settle(window, seconds: 1)
        Self.fitHeight(window)
        try await settle(window, seconds: 0.5)
        print("ScreenshotTests ran:\n" + cli.argv().joined(separator: "\n"))

        let url = out.appending(path: "graft-add-app-search.png")
        try Self.png(ofSheet: window).write(to: url)
        print("ScreenshotTests wrote \(url.path)")
    }

    /// Wait for `condition`, and fail the test *and stop it* if it never
    /// holds: `eventually` records the failure and carries on, and a run whose
    /// data never loaded would then write a picture of an empty window over a
    /// good one.
    private func waitFor(
        _ what: String, timeout: TimeInterval = 20, _ condition: () -> Bool
    ) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() {
            guard Date() < deadline else {
                throw NotReady(description: "\(what) never came within \(Int(timeout)) s, so no picture was written")
            }
            try await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    private struct NotReady: Error, CustomStringConvertible {
        let description: String
    }

    // MARK: The stand-in CLI

    /// A `graft` that answers every command the two shots run from a file of
    /// canned events, and anything else with nothing.
    private func fakeCLI() throws -> ScriptCLI {
        let dir = FileManager.default.temporaryDirectory
            .appending(path: "screenshot_cli_\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }

        let icons = dir.appending(path: "icons")
        try FileManager.default.createDirectory(at: icons, withIntermediateDirectories: true)
        for app in Self.apps {
            try Self.iconPNG(symbol: app.symbol, colors: app.colors)
                .write(to: icons.appending(path: "\(app.bundleId).png"))
        }

        let responses = Self.responses(icons: icons.path)
        for (name, lines) in responses {
            try (lines.joined(separator: "\n") + "\n")
                .write(to: dir.appending(path: "\(name).jsonl"), atomically: true, encoding: .utf8)
        }
        let r = dir.path
        // Most specific first: `--devices --brief` before `--devices`.
        return try ScriptCLI("""
        case " $* " in
          *" --brief "*) /bin/cat '\(r)/brief.jsonl' ;;
          *" --devices "*) /bin/cat '\(r)/devices.jsonl' ;;
          *" --list "*) /bin/cat '\(r)/list.jsonl' ;;
          *" --check "*) /bin/cat '\(r)/check.jsonl' ;;
          *" --artwork "*) /bin/cat '\(r)/artwork.jsonl' ;;
          *" --cached-ipa "*) /bin/cat '\(r)/cached.jsonl' ;;
          *" --appstore-check "*) /bin/cat '\(r)/version.jsonl' ;;
          *" --appstore-status "*) /bin/cat '\(r)/status.jsonl' ;;
          *" --appstore-search="*) /bin/sleep 0.2; /bin/cat '\(r)/search.jsonl' ;;
          *" --builds "*) /bin/cat '\(r)/builds.jsonl' ;;
          *" --tweaks "*) /bin/cat '\(r)/tweaks.jsonl' ;;
          *" --lc-status "*) /bin/cat '\(r)/lc.jsonl' ;;
          *" --device-info "*) /bin/cat '\(r)/info.jsonl' ;;
          *" --delivery-state "*) /bin/cat '\(r)/states.jsonl' ;;
          *" --settings "*) /bin/cat '\(r)/settings.jsonl' ;;
        esac
        exit 0
        """, test: self)
    }

    /// One file of JSON-Lines per command, in the shapes `CLIContractTests` and
    /// the decoders pin.
    private static func responses(icons: String) -> [String: [String]] {
        let device = #"{"type":"device","udid":"\#(udid)","name":"iPhone","model":"iPhone17,1","ios_version":"26.1","connection_type":"USB","livecontainer_bundle_id":"com.example.livecontainer","is_default":true}"#
        let devicesEnd = #"{"type":"devices-end","count":1,"failed":0}"#

        var list = [#"{"type":"account","account":"paid"}"#]
        var states: [String] = []
        for app in apps {
            let tweaks = app.tweaks.map { "\"\($0)\"" }.joined(separator: ",")
            list.append(#"{"type":"app","name":"\#(app.name)","bundle_id":"\#(app.bundleId)","preferred_version":"latest","tweaks":[\#(tweaks)],"display_name":null,"delivery":"\#(app.delivery)","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6,"account":"paid","lc_bundle_id":null,"profile_override":null,"store_country":"US"}"#)
            states.append(deliveryState(app))
        }
        list += states

        let artwork = apps.map {
            #"{"type":"artwork","bundle_id":"\#($0.bundleId)","path":"\#(icons)/\#($0.bundleId).png","source":"ipa","cached":false}"#
        }

        let clips = "com.example.clips"
        let version = "12.3.1"
        let ipa = "\(clips)_\(version).ipa"
        let builtAt = ISO8601DateFormatter().string(from: Date().addingTimeInterval(-120))

        let search: [String] = [
            searchResult(1, "photos", "Example Photos", price: "0", formatted: "Free", icon: "\(icons)/com.example.photos.png"),
            searchResult(2, "clips", "Example Clips", price: "0", formatted: "Free", icon: "\(icons)/com.example.clips.png", configuredAs: "clips"),
            searchResult(3, "audio", "Example Audio", price: "0", formatted: "Free", icon: "\(icons)/com.example.audio.png"),
            searchResult(4, "video", "Example Video", price: "2.99", formatted: "$2.99", icon: "\(icons)/com.example.video.png"),
            searchResult(5, "forum", "Example Forum", price: "0", formatted: "Free", icon: "\(icons)/com.example.forum.png"),
            #"{"type":"appstore-search-end","term":"\#(searchTerm)","country":"\#(searchCountry)","count":5}"#,
        ]

        return [
            "brief": [#"{"type":"device-brief","udid":"\#(udid)","connection_type":"USB"}"#, devicesEnd],
            "devices": [device, devicesEnd],
            "list": list,
            "states": states,
            "check": [#"{"type":"preflight-end","ok_count":12,"fail_count":0,"account":"paid"}"#],
            "artwork": artwork,
            "cached": [#"{"type":"cached-ipa","app":"clips","exists":true,"bundle_id":"\#(clips)","version":"\#(version)","filename":"\#(ipa)","path":"/cache/\#(ipa)","size_bytes":86245376}"#],
            "version": [#"{"type":"appstore-version","app":"clips","bundle_id":"\#(clips)","store_version":"\#(version)","cached_version":"\#(version)","update_available":false,"error":null}"#],
            "status": [#"{"type":"appstore-status","ipatool":{"path":"/ipatool","present":true,"version":"2.8.0","signed_in":true,"warning":null,"email":"someone@example.com","legacy_signin":false,"source":"bundled"},"unfairplay":{"path":"/unfairplay","present":true,"version":"v0.4.0","enrolled":true,"model_dir":"/model"},"setup_commands":[]}"#],
            "search": search,
            "builds": [#"{"type":"build-record","id":7,"timestamp":"\#(builtAt)","app":"clips","bundle_id":"\#(clips)","version":"\#(version)","tweaks":["example-tweak.deb","another-tweak.dylib"],"ipa_path":"/built/\#(ipa)","ipa_sha256":"0","log_path":"/logs/7.log","signing_team":null,"udid":"\#(udid)","target":"device","success":true,"error":null}"#],
            "tweaks": [
                #"{"type":"tweak","filename":"example-tweak.deb","size_bytes":19154,"used_by":["clips","forum","audio"],"name":"example-tweak","version":"1.0.0","section":"Tweaks","author":"Example"}"#,
                #"{"type":"tweak","filename":"another-tweak.dylib","size_bytes":53210,"used_by":["clips","photos"]}"#,
            ],
            "lc": [#"{"type":"lc-status","udid":"\#(udid)","reachable":true,"error":null,"instances":[{"bundle_id":"com.example.livecontainer","version":"3.4.0","role":"primary","wildcard_signed":true}],"managed_by":"graft","latest":null,"update_available":false,"sources":{}}"#],
            "info": [#"{"type":"device-info","udid":"\#(udid)","reachable":true,"total_bytes":256000000000,"free_bytes":91728093184,"battery_percent":82,"battery_charging":false}"#],
            "settings": [#"{"type":"settings","default_device_udid":"\#(udid)","archive_keep":2,"signing_cert":"/signing/development.p12","signing_profile":"/signing/wildcard.mobileprovision","signing_team_id":"EXAMPLE123"}"#],
        ]
    }

    private static func deliveryState(_ app: PlaceholderApp) -> String {
        let pushed = #""pushed_at":"2026-11-20T16:20:00+00:00""#
        switch app.state {
        case "current":
            return #"{"type":"delivery-state","app":"\#(app.name)","kind":"current","summary":"Up to date with the last delivery.","reasons":[],\#(pushed)}"#
        case "stale":
            return #"{"type":"delivery-state","app":"\#(app.name)","kind":"stale","summary":"Changed since the last delivery.","reasons":["example-tweak.deb updated"],\#(pushed)}"#
        default:
            return #"{"type":"delivery-state","app":"\#(app.name)","kind":"never","summary":"Never delivered to a device.","reasons":[],"pushed_at":null}"#
        }
    }

    private static func searchResult(
        _ n: Int, _ slug: String, _ name: String, price: String, formatted: String,
        icon: String, configuredAs: String? = nil
    ) -> String {
        let added = configuredAs.map { "\"\($0)\"" } ?? "null"
        return #"{"type":"appstore-search-result","track_id":10000000\#(n),"bundle_id":"com.example.\#(slug)","name":"\#(name)","developer":"Example, Inc.","version":"\#(n + 1).\#(n * 2).0","price":\#(price),"formatted_price":"\#(formatted)","currency":"USD","artwork_url":null,"icon_path":"\#(icon)","configured_as":\#(added)}"#
    }

    // MARK: Icons

    /// A placeholder app icon: a colored square with a white SF Symbol on it.
    /// Square, because `AppIcon` rounds it.
    private static func iconPNG(symbol: String, colors: (top: NSColor, bottom: NSColor)) throws -> Data {
        let side = 256
        let rep = try bitmap(width: side, height: side, size: NSSize(width: side, height: side))
        NSGraphicsContext.saveGraphicsState()
        defer { NSGraphicsContext.restoreGraphicsState() }
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
        let rect = NSRect(x: 0, y: 0, width: side, height: side)
        NSGradient(starting: colors.top, ending: colors.bottom)?.draw(in: rect, angle: -90)
        let config = NSImage.SymbolConfiguration(pointSize: 110, weight: .semibold)
            .applying(NSImage.SymbolConfiguration(paletteColors: [.white]))
        let glyph = try XCTUnwrap(
            NSImage(systemSymbolName: symbol, accessibilityDescription: nil)?
                .withSymbolConfiguration(config), "no SF Symbol \(symbol)")
        let size = glyph.size
        glyph.draw(in: NSRect(x: (CGFloat(side) - size.width) / 2, y: (CGFloat(side) - size.height) / 2,
                              width: size.width, height: size.height))
        return try XCTUnwrap(rep.representation(using: .png, properties: [:]))
    }

    // MARK: Hosting and drawing

    /// What `GraftApp` puts around `ContentView`: dark, and the app's accent.
    /// Plus a throwaway defaults suite, so no `@AppStorage` reads the real app's.
    private struct AppChrome: ViewModifier {
        let defaults: UserDefaults
        func body(content: Content) -> some View {
            content
                .preferredColorScheme(.dark)
                .tint(Palette.accentSelect)
                .defaultAppStorage(defaults)
        }
    }

    private func scratchDefaults() throws -> UserDefaults {
        let name = "ScreenshotTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: name))
        addTeardownBlock { defaults.removePersistentDomain(forName: name) }
        defaults.set(DeliveryMode.livecontainer.rawValue, forKey: "lastDeliveryMode")
        return defaults
    }

    /// A window that draws as the front one — colored window buttons, tinted
    /// controls — without activating the test host, which would take the
    /// keyboard from whatever the Mac is being used for.
    private final class FrontWindow: NSWindow {
        override var isKeyWindow: Bool { true }
        override var isMainWindow: Bool { true }
        // What the title bar asks. Private to AppKit, so overridden by name;
        // on a macOS without them they are never called, and the window
        // buttons come out gray.
        @objc(_hasActiveAppearance) func hasActiveAppearance() -> Bool { true }
        @objc(_hasActiveAppearanceIgnoringKeyFocus) func hasActiveAppearanceIgnoringKeyFocus() -> Bool { true }
        @objc(_hasActiveAppearanceForStandardWindowButton:)
        func hasActiveAppearance(forStandardWindowButton button: Int) -> Bool { true }
        @objc(_hasKeyAppearance) func hasKeyAppearancePrivate() -> Bool { true }
        @objc(_hasMainAppearance) func hasMainAppearancePrivate() -> Bool { true }
        @objc(hasKeyAppearance) func hasKeyAppearance() -> Bool { true }
        @objc(hasMainAppearance) func hasMainAppearance() -> Bool { true }
    }

    /// A document window like `WindowGroup`'s: full-size content under a
    /// unified toolbar, with the toolbar and title bridged from SwiftUI.
    private static func documentWindow<V: View>(_ view: V, size: NSSize) -> NSWindow {
        let controller = NSHostingController(rootView: view)
        controller.sceneBridgingOptions = .all
        let window = FrontWindow(
            contentRect: NSRect(origin: .zero, size: size),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered, defer: false
        )
        window.animationBehavior = .none
        window.appearance = NSAppearance(named: .darkAqua)
        window.toolbarStyle = .unified
        window.contentViewController = controller
        window.setContentSize(size)
        window.center()
        window.orderFront(nil)
        return window
    }

    /// A sheet's own window: no title bar, sized to the sheet's content.
    private static func sheetWindow<V: View>(_ view: V, width: CGFloat) -> NSWindow {
        let host = NSHostingView(rootView: view)
        let window = FrontWindow(
            contentRect: NSRect(x: 0, y: 0, width: width, height: 600),
            styleMask: [.borderless], backing: .buffered, defer: false
        )
        window.animationBehavior = .none
        window.appearance = NSAppearance(named: .darkAqua)
        window.contentView = host
        window.center()
        window.orderFront(nil)
        fitHeight(window)
        return window
    }

    /// Resize a window to what its SwiftUI content asks for, keeping the top.
    private static func fitHeight(_ window: NSWindow) {
        guard let host = window.contentView else { return }
        host.layoutSubtreeIfNeeded()
        let height = window.frameRect(forContentRect: NSRect(origin: .zero, size: host.fittingSize)).height
        var frame = window.frame
        frame.origin.y += frame.height - height
        frame.size.height = height
        window.setFrame(frame, display: true)
    }

    private static func textFields(in view: NSView?) -> [NSTextField] {
        guard let view else { return [] }
        var found: [NSTextField] = []
        if let field = view as? NSTextField, field.isEditable { found.append(field) }
        for sub in view.subviews { found += textFields(in: sub) }
        return found
    }

    private static func descendants(of view: NSView) -> [NSView] {
        view.subviews + view.subviews.flatMap(descendants(of:))
    }

    private static func className(_ view: NSView) -> String {
        String(describing: type(of: view))
    }

    private func settle(_ window: NSWindow, seconds: Double) async throws {
        let deadline = Date().addingTimeInterval(seconds)
        while Date() < deadline {
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        window.contentView?.layoutSubtreeIfNeeded()
        window.displayIfNeeded()
    }

    private static let scale: CGFloat = 2

    /// The window's corner radius, for cutting the corners out of the picture.
    private static let cornerRadius: CGFloat = 16

    /// The sidebar's Liquid Glass over this app's dark window, as macOS
    /// composites it: sampled from a screen capture of this sidebar (the 1.1
    /// README screenshot). Glass is a backdrop the window server draws, so
    /// AppKit's own drawing leaves it out.
    private static let sidebarColor = NSColor(srgbRed: 30 / 255, green: 35 / 255, blue: 51 / 255, alpha: 1)

    /// A selected sidebar row's highlight, which is likewise a material: the
    /// app's tint (`GraftApp`'s `.tint(Palette.accentSelect)`), in the shape
    /// macOS gives it.
    private static let selectionColor = NSColor(Palette.accentSelect)
    private static let selectionRadius: CGFloat = 10

    private static func bitmap(width: Int, height: Int, size: NSSize) throws -> NSBitmapImageRep {
        let rep = try XCTUnwrap(NSBitmapImageRep(
            bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: height,
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
        ))
        rep.size = size
        return rep
    }

    /// AppKit's own drawing of `view` and everything in it, at 2x, with the
    /// view's appearance current so dynamic colors resolve as on screen.
    private static func drawing(of view: NSView) throws -> NSBitmapImageRep {
        let bounds = view.bounds
        let rep = try bitmap(width: Int((bounds.width * scale).rounded()),
                             height: Int((bounds.height * scale).rounded()), size: bounds.size)
        view.effectiveAppearance.performAsCurrentDrawingAppearance {
            view.cacheDisplay(in: bounds, to: rep)
        }
        return rep
    }

    /// Core Animation's drawing of `layer` into `rep`, the right way up.
    /// The context is already in points: `rep.size` is.
    private static func render(_ layer: CALayer, into rep: NSBitmapImageRep) throws {
        let context = try XCTUnwrap(NSGraphicsContext(bitmapImageRep: rep)).cgContext
        if layer.contentsAreFlipped() {
            context.translateBy(x: 0, y: layer.bounds.height)
            context.scaleBy(x: 1, y: -1)
        }
        layer.render(in: context)
        context.flush()
    }

    /// Hide `views` while `body` runs.
    private static func hiding<T>(_ views: [NSView], _ body: () throws -> T) rethrows -> T {
        let shown = views.filter { !$0.isHidden }
        shown.forEach { $0.isHidden = true }
        defer { shown.forEach { $0.isHidden = false } }
        return try body()
    }

    /// The whole window, title bar included.
    ///
    /// AppKit draws it all but the materials, which the window server
    /// composites: the sidebar's glass, the selected row's highlight, the
    /// sidebar's scroll-edge blur, and the vibrant coloring of the sidebar's
    /// text and symbols. Those are left out of the drawing and put back by
    /// hand: the glass as the color macOS shows it, the highlight in the app's
    /// tint, the vibrant text through its own color matrix (`Vibrant`). The
    /// blur stays out; over an unscrolled list it shows nothing on screen
    /// either.
    ///
    /// So it is drawn in three layers, back to front: the window without its
    /// sidebar, the sidebar, and the title bar again where it overlaps the
    /// sidebar.
    private static func png(ofWindow window: NSWindow) throws -> Data {
        let frameView = try XCTUnwrap(window.contentView?.superview, "the window's frame view")
        let bounds = frameView.bounds
        let glass = descendants(of: frameView).first {
            className($0) == "NSGlassEffectView" && $0.frame.width > 100
        }
        let sidebar = try XCTUnwrap(glass?.superview, "the sidebar")
        // What the glass holds. The glass itself draws an opaque stand-in for
        // its material when AppKit draws it, so it is drawn from inside.
        let sidebarContent = try XCTUnwrap(
            glass?.subviews.first { className($0) == "ContentHolderView" }?.subviews.first,
            "the sidebar's content")
        let titleBar = try XCTUnwrap(
            frameView.subviews.first { className($0) == "NSTitlebarContainerView" }, "the title bar")
        let list = try XCTUnwrap(
            descendants(of: sidebar).compactMap { $0 as? NSOutlineView }.first, "the sidebar's list")
        let listLayer = try XCTUnwrap(list.layer, "the sidebar list's layer")
        let clip = try XCTUnwrap(list.enclosingScrollView?.contentView, "the sidebar's clip view")
        // The list's scroll-edge blur.
        let blur = descendants(of: sidebar).filter { ["BackdropView", "NSScrollPocket"].contains(className($0)) }
        let highlights = list.subviews.flatMap { row in row.subviews.compactMap { $0 as? NSVisualEffectView } }
        let vibrant = Vibrant.layers(in: listLayer)

        // Transparent rather than hidden, so the split view keeps its layout.
        let back = try transparent([sidebar]) { try drawing(of: frameView) }
        let side = try hiding(blur + highlights) {
            try Vibrant.hiding(vibrant) { try drawing(of: sidebarContent) }
        }
        let top = try drawing(of: titleBar)
        let marks = try vibrant.map { item in
            (try item.colored(),
             list.convert(listLayer.convert(item.layer.bounds, from: item.layer), to: frameView),
             item.additive)
        }

        let picture = try bitmap(width: back.pixelsWide, height: back.pixelsHigh, size: bounds.size)
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: picture)
        NSBezierPath(roundedRect: bounds, xRadius: cornerRadius, yRadius: cornerRadius).addClip()
        over(back, in: bounds)
        let sidebarRect = sidebar.convert(sidebar.bounds, to: frameView)
        sidebarColor.setFill()
        sidebarRect.fill()
        NSGraphicsContext.saveGraphicsState()
        NSBezierPath(rect: clip.convert(clip.bounds, to: frameView)).addClip()
        selectionColor.setFill()
        for highlight in highlights {
            let rect = highlight.convert(highlight.bounds, to: frameView)
            NSBezierPath(roundedRect: rect, xRadius: selectionRadius, yRadius: selectionRadius).fill()
        }
        NSGraphicsContext.restoreGraphicsState()
        over(side, in: sidebarContent.convert(sidebarContent.bounds, to: frameView))
        for (mark, rect, additive) in marks {
            over(mark, in: rect, operation: additive ? .plusLighter : .sourceOver)
        }
        // The title bar again, over the sidebar: its buttons sit on the glass.
        NSBezierPath(rect: sidebarRect).addClip()
        over(top, in: titleBar.convert(titleBar.bounds, to: frameView))
        NSGraphicsContext.restoreGraphicsState()
        return try pngData(picture)
    }

    /// Draw `views` fully transparent while `body` runs.
    private static func transparent<T>(_ views: [NSView], _ body: () throws -> T) rethrows -> T {
        let alphas = views.map(\.alphaValue)
        views.forEach { $0.alphaValue = 0 }
        defer { zip(views, alphas).forEach { $0.alphaValue = $1 } }
        return try body()
    }

    /// A layer SwiftUI draws vibrant in the sidebar: alpha-only text, or a
    /// template symbol, colored by a `vibrantColorMatrix` filter. Offscreen
    /// drawing skips the filter, so without this every label comes out black.
    @MainActor
    private struct Vibrant {
        let layer: CALayer
        /// The filter's 4×5 matrix, row by row: r' = m[0]r + m[1]g + m[2]b + m[3]a + m[4].
        let matrix: [Double]
        /// Added to what is behind it rather than laid over it. Text's matrix
        /// is a `filters` entry and comes out added (a header's (51, 51, 51)
        /// over the glass's (30, 35, 51) is the (81, 86, 102) a screen capture
        /// shows); a symbol's is a "…SourceOver" compositing filter.
        let additive: Bool

        static func layers(in root: CALayer) -> [Vibrant] {
            var found: [Vibrant] = []
            func walk(_ layer: CALayer) {
                if !layer.isHidden, let (matrix, additive) = matrix(of: layer) {
                    found.append(Vibrant(layer: layer, matrix: matrix, additive: additive))
                }
                for sub in layer.sublayers ?? [] { walk(sub) }
            }
            walk(root)
            return found
        }

        private static func matrix(of layer: CALayer) -> ([Double], Bool)? {
            let filters: [(Any, Bool)] = (layer.filters ?? []).map { ($0, true) }
                + (layer.compositingFilter.map { [($0, false)] } ?? [])
            for (filter, additive) in filters {
                let object = filter as AnyObject
                guard String(describing: object).contains("vibrantColorMatrix"),
                      let value = object.value(forKey: "inputColorMatrix") as? NSValue
                else { continue }
                var size = 0
                NSGetSizeAndAlignment(value.objCType, &size, nil)
                if size == 20 * MemoryLayout<Float>.size {
                    var m = [Float](repeating: 0, count: 20)
                    m.withUnsafeMutableBytes { value.getValue($0.baseAddress!, size: size) }
                    return (m.map(Double.init), additive)
                }
                if size == 20 * MemoryLayout<Double>.size {
                    var m = [Double](repeating: 0, count: 20)
                    m.withUnsafeMutableBytes { value.getValue($0.baseAddress!, size: size) }
                    return (m, additive)
                }
            }
            return nil
        }

        static func hiding<T>(_ items: [Vibrant], _ body: () throws -> T) rethrows -> T {
            items.forEach { $0.layer.isHidden = true }
            defer { items.forEach { $0.layer.isHidden = false } }
            return try body()
        }

        /// The layer drawn on its own, then through its matrix.
        func colored() throws -> NSBitmapImageRep {
            let size = layer.bounds.size
            let rep = try ScreenshotTests.bitmap(
                width: max(1, Int((size.width * ScreenshotTests.scale).rounded(.up))),
                height: max(1, Int((size.height * ScreenshotTests.scale).rounded(.up))), size: size)
            try ScreenshotTests.render(layer, into: rep)
            let pixels = try XCTUnwrap(rep.bitmapData)
            for i in stride(from: 0, to: rep.bytesPerRow * rep.pixelsHigh, by: 4) {
                let a = Double(pixels[i + 3]) / 255
                guard a > 0 else { continue }
                let r = Double(pixels[i]) / 255 / a, g = Double(pixels[i + 1]) / 255 / a
                let b = Double(pixels[i + 2]) / 255 / a
                func row(_ n: Int) -> Double {
                    let m = Array(matrix[(n * 5)..<(n * 5 + 5)])
                    return min(1, max(0, m[0] * r + m[1] * g + m[2] * b + m[3] * a + m[4]))
                }
                let alpha = row(3)
                pixels[i] = UInt8((row(0) * alpha * 255).rounded())
                pixels[i + 1] = UInt8((row(1) * alpha * 255).rounded())
                pixels[i + 2] = UInt8((row(2) * alpha * 255).rounded())
                pixels[i + 3] = UInt8((alpha * 255).rounded())
            }
            return rep
        }
    }

    /// A sheet: its content, with the corners cut out.
    private static func png(ofSheet window: NSWindow) throws -> Data {
        let view = try XCTUnwrap(window.contentView, "the sheet's content")
        let drawn = try drawing(of: view)
        let picture = try bitmap(width: drawn.pixelsWide, height: drawn.pixelsHigh, size: view.bounds.size)
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: picture)
        NSBezierPath(roundedRect: view.bounds, xRadius: cornerRadius, yRadius: cornerRadius).addClip()
        over(drawn, in: view.bounds)
        NSGraphicsContext.restoreGraphicsState()
        return try pngData(picture)
    }

    /// Draw `rep` over what is there. `draw(in:)` copies, transparent pixels
    /// and all.
    private static func over(
        _ rep: NSBitmapImageRep, in rect: NSRect, operation: NSCompositingOperation = .sourceOver
    ) {
        rep.draw(in: rect, from: .zero, operation: operation, fraction: 1,
                 respectFlipped: false, hints: nil)
    }

    /// sRGB, for the web, and 144 dpi, so it reads as @2x.
    private static func pngData(_ rep: NSBitmapImageRep) throws -> Data {
        let srgb = try XCTUnwrap(rep.converting(to: .sRGB, renderingIntent: .default))
        srgb.size = rep.size
        return try XCTUnwrap(srgb.representation(using: .png, properties: [:]))
    }

    private static func outputDirectory() throws -> URL {
        guard let path = ProcessInfo.processInfo.environment["GRAFT_SCREENSHOT_DIR"], !path.isEmpty else {
            throw XCTSkip("Set TEST_RUNNER_GRAFT_SCREENSHOT_DIR to a folder to write the screenshots")
        }
        let url = URL(fileURLWithPath: path, isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }
}
