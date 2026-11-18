import SwiftUI
import XCTest
@testable import Graft

/// The window's content has to fit inside the window.
///
/// Stated that plainly it sounds untestable-because-obvious, and it is what
/// this app shipped broken. `ContentView` used to put a `NavigationSplitView`
/// inside another one's detail column; the inner one lays out for a width it
/// does not have, and whatever falls past the window edge is clipped — Build &
/// Install, Check device, the ⊖ on every tweak row. Measured by this harness
/// against the nested version: **120pt past the right edge at 700, 900 and
/// 1470**, a constant rather than a fraction, which is why the 1200pt window
/// minimum that was tried first could not have cleared it.
///
/// Three things here are load-bearing, and each was wrong in the first draft:
///
///  - **The tests are `async` and wait for the fixture.** `ContentView` loads
///    its apps from a `.task`, and a synchronous `@MainActor` test never yields,
///    so the first version measured an empty sidebar beside a placeholder label
///    — `AppDetailView`, the pane this file exists to protect, was never in the
///    tree at all. `assertNothingIsClipped` now fails outright if the two-app
///    fixture hasn't landed, so the suite cannot go quietly vacuous again.
///  - **Overflow is measured against the window, not the hosting view.**
///    `NSHostingView` grows to fit oversized content, so comparing descendants
///    to `host.bounds` lets the reference edge move with the defect: a 2000pt
///    subview in a 400pt window reported zero. `contentWidth` is captured from
///    the window before layout.
///  - **Both edges.** The recorded failure was the sidebar hanging off the
///    *left* as well as the detail pane clipping on the right, and a walk that
///    only checks `maxX` is blind to half of it.
@MainActor
final class WindowLayoutTests: XCTestCase {

    /// A store whose CLI prints two apps and exits, so nothing here shells out
    /// to the real `graft`.
    private func stubStore() -> ConfigStore {
        let script = #"""
        printf '{"type":"app","name":"example","bundle_id":"com.example.app","preferred_version":"latest","tweaks":["a.deb"],"display_name":"Example+","delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}\n{"type":"app","name":"delta","bundle_id":"com.delta.app","preferred_version":"44.7.0","tweaks":["v.deb"],"display_name":null,"delivery":"sideload","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}\n'
        """#
        return ConfigStore(
            runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")),
            cliArgs: ["-c", script]
        )
    }

    /// The shared measurement harness plus the store behind it.
    ///
    /// The layout half is `HostedLayout` — the same one `SidebarDeviceLayout
    /// Tests` and `DeviceDetailLayoutTests` measure against; this file kept a
    /// private copy of it until the three were folded together. What is
    /// genuinely local is `store`: these cases assert against the apps
    /// `ContentView` loaded, so the fixture has to hand it back.
    private struct Hosted {
        let layout: HostedLayout
        let store: ConfigStore
        var root: NSView { layout.root }
        var contentWidth: CGFloat { layout.contentWidth }
        var window: NSWindow { layout.window }
    }

    /// `ContentView` in a real window at a real size, with its apps loaded.
    ///
    /// A window rather than a bare `NSHostingView`, because a split view only
    /// divides a width it has actually been given.
    private func laidOut(
        width: CGFloat,
        height: CGFloat,
        store: ConfigStore? = nil,
        expectsApps: Bool = true,
        selection: SidebarItem? = nil
    ) async -> Hosted {
        let store = store ?? stubStore()
        let layout = laidOutForMeasurement(
            ContentView(store: store, selection: selection), width: width, height: height
        )
        let host = layout.root
        let window = layout.window

        // Let ContentView's `.task` run: it spawns /bin/sh, parses the fixture
        // and assigns the selection that puts AppDetailView on screen. Polled
        // rather than slept-on so a fast machine doesn't pay for a slow one's
        // margin; the ceiling is generous because it is a real subprocess.
        //
        // `await`, specifically — not `RunLoop.main.run(until:)`. Pumping the
        // runloop instead blocks the main actor, and the load never progresses
        // at all: measured, `apps` stayed at 0 across twelve pumps totalling
        // 600ms, where a single `Task.sleep` gets there in one.
        //
        // A store that legitimately reports *no* apps — the negative control in
        // `test_the_harness_measures_the_app_detail_pane` — can never satisfy
        // the exit condition, so it says so with `expectsApps` rather than
        // sitting out the timeout. It still gets a fixed settle, because the
        // task has to run for the placeholder to be the *answer* rather than
        // just what was on screen before the load finished.
        let settled = Date().addingTimeInterval(0.3)
        let deadline = Date().addingTimeInterval(5)
        while Date() < settled || (expectsApps && store.apps.isEmpty && Date() < deadline) {
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        // A second layout pass: the first ran before any of that existed.
        host.layoutSubtreeIfNeeded()
        window.displayIfNeeded()
        return Hosted(layout: layout, store: store)
    }

    /// How far the furthest-out descendant sticks past either vertical edge of
    /// the window, and what it was — the name is what makes a failure
    /// diagnosable, since the answer is a view deep inside AppKit's hierarchy.
    ///
    /// Every descendant, not only the ones we wrote: the clipped content in the
    /// real bug was inside SwiftUI's `NavigationSplitRepresentable`, which was
    /// the thing laying out at the wrong width.
    private func worstOverflow(_ hosted: Hosted) -> (points: CGFloat, view: String) {
        var worst: CGFloat = 0
        var who = "nothing"
        func walk(_ view: NSView, _ path: String) {
            // Hidden subtrees are skipped entirely rather than merely
            // unmeasured. AppKit parks full-size views under a hidden parent —
            // a collapsed split-view item's content, a detached hosting view
            // for a sheet — and recursing into them reported offscreen
            // scaffolding as clipped content, which is a red CI on a correct
            // change and the fastest way to get a layout guard deleted.
            guard !view.isHidden else { return }
            let frame = view.convert(view.bounds, to: hosted.root)
            // Degenerate frames aren't on screen to be clipped, and SwiftUI
            // leaves plenty of them in the tree.
            if frame.width > 1, frame.height > 1 {
                // Against the window's content width, never `root.bounds`: the
                // host expands to fit oversized content, so measuring against
                // it moves the goalposts with the defect.
                let over = max(frame.maxX - hosted.contentWidth, -frame.minX)
                if over > worst {
                    worst = over
                    who = "\(path) at \(frame)"
                }
            }
            for (i, sub) in view.subviews.enumerated() {
                walk(sub, path + "/" + String(describing: type(of: sub)) + "[\(i)]")
            }
        }
        walk(hosted.root, "root")
        return (worst, who)
    }

    /// 2pt of tolerance, for rounding on a divider or a hairline. The defect
    /// this guards against was 120, so nothing needs a generous margin.
    private func assertNothingIsClipped(
        width: CGFloat,
        height: CGFloat,
        file: StaticString = #filePath,
        line: UInt = #line
    ) async {
        let hosted = await laidOut(width: width, height: height)
        // The anti-vacuity check. Without it every assertion below passes
        // against an empty sidebar and a placeholder label, which is exactly
        // how the first version of this file managed to guard nothing.
        XCTAssertEqual(
            hosted.store.apps.count, 2,
            "the two-app fixture never loaded, so this measured an empty window — "
            + "the assertions below would pass no matter what the layout did",
            file: file, line: line
        )
        // The host must not have grown past the window either; that is the
        // failure mode that used to hide overflow rather than report it.
        XCTAssertLessThanOrEqual(
            hosted.root.bounds.width, width + 2,
            "the hosting view grew to \(Int(hosted.root.bounds.width))pt inside a "
            + "\(Int(width))pt window — content is demanding more width than it has",
            file: file, line: line
        )
        let overflow = worstOverflow(hosted)
        XCTAssertLessThanOrEqual(
            overflow.points, 2,
            "\(Int(overflow.points))pt of the window's content is laid out past its "
            + "edge in a \(Int(width))pt window, where it is clipped and "
            + "unreachable: \(overflow.view)",
            file: file, line: line
        )
    }

    /// The size the window actually ships at, read from the same constants
    /// `GraftApp` applies — not a literal that can drift away from them.
    func test_content_is_not_clipped_at_the_minimum_window_size() async {
        await assertNothingIsClipped(
            width: Metrics.windowMinWidth,
            height: Metrics.windowMinHeight
        )
    }

    /// The full width of the display this was reported on.
    func test_content_is_not_clipped_at_full_screen_width() async {
        await assertNothingIsClipped(width: 1470, height: 922)
    }

    /// Genuinely below the app's own minimum, however that minimum is later
    /// retuned. A window cannot normally get here, but the property under test
    /// is "the content fits", and a layout that only holds above a magic number
    /// is what failed last time.
    func test_content_is_not_clipped_below_the_window_minimum() async {
        await assertNothingIsClipped(
            width: Metrics.windowMinWidth - 200,
            height: Metrics.windowMinHeight - 60
        )
    }

    /// One navigation column, not two.
    ///
    /// The structural half of the same story. The measurements above are the
    /// property that must hold; this names the cause, so a regression reads
    /// "someone nested a split view again" rather than only "something is 120pt
    /// too wide".
    func test_the_window_has_a_single_split_view() async {
        func splitViews(under view: NSView) -> [NSSplitView] {
            ((view as? NSSplitView).map { [$0] } ?? []) + view.subviews.flatMap(splitViews(under:))
        }
        let hosted = await laidOut(width: 1470, height: 922)
        XCTAssertEqual(
            splitViews(under: hosted.root).count, 1,
            "a NavigationSplitView inside another one's detail column lays out for "
            + "a width it does not have, and clips whatever falls past the edge"
        )
    }

    /// The detail pane is actually on screen when the measurements run.
    ///
    /// `apps.count == 2` proves the fixture loaded; this proves the load went
    /// on to select an app, so the tree being measured contains the pane whose
    /// clipping started all of this rather than the "pick an app" placeholder.
    ///
    /// The signal is "the detail column contains an `NSScrollView`":
    /// `AppDetailView` wraps its body in a `ScrollView` and the placeholder is
    /// a bare `Text`. Deliberately not a text search — SwiftUI populates no
    /// accessibility attributes in a detached, non-key window, so a probe for
    /// "Build & Install" finds nothing whatever is on screen, which is a test
    /// that always fails rather than one that discriminates. Also deliberately
    /// not a view-count threshold: the counts (2 for the placeholder, ~25 for
    /// the pane) are a SwiftUI implementation detail with no meaning to pin.
    ///
    /// The empty-store half is the point of the test. It is the negative
    /// control that shows the probe can tell the two apart — without it, an
    /// assertion that "there is a scroll view somewhere" could be passing for
    /// reasons unrelated to the pane being present.
    func test_the_harness_measures_the_app_detail_pane() async {
        func detailHasScrollView(_ hosted: Hosted) -> Bool {
            func splitViews(under view: NSView) -> [NSSplitView] {
                ((view as? NSSplitView).map { [$0] } ?? [])
                    + view.subviews.flatMap(splitViews(under:))
            }
            func containsScrollView(_ view: NSView) -> Bool {
                view is NSScrollView || view.subviews.contains(where: containsScrollView)
            }
            guard let detail = splitViews(under: hosted.root).first?.arrangedSubviews.last
            else { return false }
            return containsScrollView(detail)
        }

        let withApps = await laidOut(width: 1470, height: 922)
        XCTAssertTrue(
            detailHasScrollView(withApps),
            "the detail column has no scroll view, so AppDetailView is not in the "
            + "measured tree — the layout assertions are inspecting a placeholder"
        )

        // Negative control: a CLI that reports no apps leaves the selection nil,
        // so the detail column holds the placeholder and the probe must say so.
        let empty = ConfigStore(
            runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")),
            cliArgs: ["-c", "true"]
        )
        let withoutApps = await laidOut(
            width: 1470, height: 922, store: empty, expectsApps: false
        )
        XCTAssertFalse(
            detailHasScrollView(withoutApps),
            "the probe reports the detail pane present even with no apps loaded, so "
            + "it cannot distinguish the pane from the placeholder and proves nothing"
        )
    }

    // MARK: - Does each section reach the pane it names?

    /// Every `SidebarSection`, and the title the pane it routes to sets.
    ///
    /// Hand-written for the reason `Symbols.all` and `CLIContract.flagsUsed`
    /// are: the point is to state independently what each case *should* reach,
    /// so a route that changes has to be changed here too. Deriving either half
    /// from `sectionContent` would be the switch agreeing with itself.
    ///
    /// The strings are the panes' own `navigationTitle` literals. A pane that
    /// renames itself fails here, which is correct and cheap to fix — and the
    /// alternative, reading the title off the view under test, is no test.
    private static let sectionTitles: [(SidebarSection, String)] = [
        (.tweaks, "Tweaks"),
        (.canister, "Canister"),
        (.repos, "Repos"),
        (.cache, "Cache"),
        (.builds, "Builds"),
        (.settings, "Settings"),
    ]

    /// Each sidebar section reaches the pane it names.
    ///
    /// **This is the assertion that did not exist while the Cache route was
    /// written, and its absence was measured rather than assumed: pointing
    /// `case .section(.cache)` at `ReposView` passed all 691 tests.** Every
    /// other guard on that route is structural — the compiler forces the case to
    /// be *handled* (`sectionContent` has no `default`), and
    /// `SidebarSectionRowTests` proves the row carries the right tag — and
    /// nothing between the tag and the pane was checked at all.
    ///
    /// **The instrument is `NSWindow.title`.** SwiftUI propagates
    /// `.navigationTitle` to the hosting window's title whenever an
    /// `NSHostingView` is its `contentView`, which is exactly what `laidOut`
    /// already builds — so the window can be asked which pane it is showing.
    /// That is worth spelling out because the surrounding file documents the
    /// opposite for everything else: SwiftUI populates no accessibility
    /// attributes in a detached window, so a text probe for "Build & Install"
    /// finds nothing whatever is on screen (see
    /// `test_the_harness_measures_the_app_detail_pane`). The title is a
    /// different channel and it does work in process.
    ///
    /// Reaching a section needs `ContentView`'s `selection` seam: it is private
    /// `@State`, and synthetic clicks on the sidebar's rows were measured not to
    /// move it. See that initialiser's own doc for why the parameter is
    /// defaulted and why production never passes it.
    ///
    /// All six cases, not just `.cache`. The gap this closes was never specific
    /// to the route that exposed it, and a table of one is a table somebody adds
    /// the seventh section without.
    func test_every_section_route_lands_on_the_pane_it_names() async {
        for (section, title) in Self.sectionTitles {
            let hosted = await laidOut(
                width: 1200, height: 800, selection: .section(section)
            )
            XCTAssertEqual(
                hosted.window.title, title,
                "selecting .section(.\(section)) put a pane titled "
                + "\"\(hosted.window.title)\" on screen, not \"\(title)\" — the "
                + "sidebar row and the detail switch disagree about where this "
                + "section goes"
            )
        }
    }

    /// The negative control for the instrument above.
    ///
    /// Without it, `window.title` could be reporting something that has nothing
    /// to do with the selection — a constant, or the app name — and all six
    /// assertions above would be one string compared against itself. Opening on
    /// an *app* rather than a section takes the title from
    /// `AppDetailView.navigationTitle`, which is the record's display name: a
    /// value no section pane can produce, from the fixture rather than from a
    /// literal in this file.
    func test_the_window_title_follows_the_selection() async {
        let hosted = await laidOut(width: 1200, height: 800, selection: .app("example"))
        XCTAssertEqual(
            hosted.window.title, "Example+",
            "the window title does not track which pane is showing, so every route "
            + "assertion above is comparing a constant with itself"
        )
    }
}
