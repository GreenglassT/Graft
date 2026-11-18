import SwiftUI
import XCTest
@testable import Graft

/// The window a `hosted(_:width:)` render lives in, and the size it settled
/// on — handed back together because a caller needs both: the window to send
/// `click(at:in:)` into, the size to aim a coordinate at.
struct Hosted {
    let window: NSWindow
    let size: NSSize
}

/// A window-based click, dispatched through `window`'s own event pipeline
/// rather than simulated at the SwiftUI level — the only way to prove a
/// `Button` actually receives what a real click sends it. SwiftUI has no API
/// for "is this pixel inside some view's hit region"; AppKit's own
/// `sendEvent(_:)` does the real hit-testing a click depends on, the same
/// path a physical mouse takes.
///
/// `NSEvent.mouseEvent(location:)` is in the window's *base* coordinate
/// system, which is unflipped (origin bottom-left) — `NSHostingView`'s own
/// view coordinate space is flipped (origin top-left), and those are two
/// different things. A caller aiming at a control's flipped-space frame
/// without converting it will click nowhere and get no error saying why (see
/// `DeviceDecryptSectionTests` for where this was measured, not assumed,
/// after a first attempt did exactly that).
@MainActor
func click(at point: NSPoint, in window: NSWindow) {
    for type: NSEvent.EventType in [.leftMouseDown, .leftMouseUp] {
        guard let event = NSEvent.mouseEvent(
            with: type, location: point, modifierFlags: [], timestamp: 0,
            windowNumber: window.windowNumber, context: nil,
            eventNumber: 0, clickCount: 1, pressure: type == .leftMouseDown ? 1 : 0
        ) else { continue }
        window.sendEvent(event)
    }
}

/// Renders `view` at a fixed width and its own natural height — sized
/// exactly to its content (`fittingSize`, not a size picked by hand), so
/// there is no slack left for SwiftUI's default placement rules to hide a
/// click target inside. A window taller or wider than its content leaves a
/// real, unanswered question — does the row's content top-align or centre in
/// the extra space? — that a hand-picked coordinate could get wrong
/// silently; sizing to the content removes the question rather than guessing
/// the answer.
///
/// Retains the window for the caller's use of it: an `NSWindow` released
/// mid-test takes its view tree, and any click sent to it, along.
///
/// `makeKeyAndOrderFront` is not cosmetic here — it is the difference
/// between a click that reaches a `Button` and one that reaches nothing.
/// Measured directly: with `window.sendEvent(_:)` alone (no key window), a
/// sweep of every point across a `.configured` tile's full bounds registered
/// zero taps; ordering the window key first, the identical sweep hit on
/// every single point. AppKit's own event routing evidently declines to
/// dispatch to a view whose window has never been made key, headless or not.
///
/// One shared copy, after four independent files arrived at a byte-identical
/// `click(at:in:)` and three of four arrived at this same `hosted(_:width:)`
/// — the fourth (`AddAppSheetPrefillTests`) had already dropped the `width`
/// parameter in favour of a hardcoded 480, a real drift after one file's
/// worth of independent maintenance, and the predicted cost of four copies
/// realised. `internal`, not `private`: every test file that calls this is
/// in the same `GraftTests` target, so no import is needed.
@MainActor
func hosted<V: View>(_ view: V, width: CGFloat) -> Hosted {
    let host = NSHostingView(rootView: view.frame(width: width))
    // A generous first proposal purely to measure: `fittingSize` asks
    // SwiftUI for its own ideal size, and a height too small to begin with
    // would have this reporting its own constraint back.
    host.frame = NSRect(x: 0, y: 0, width: width, height: 2000)
    host.layoutSubtreeIfNeeded()
    let size = host.fittingSize
    let window = NSWindow(
        contentRect: NSRect(x: 0, y: 0, width: size.width, height: size.height),
        styleMask: [.titled], backing: .buffered, defer: false
    )
    window.contentView = host
    host.frame = NSRect(x: 0, y: 0, width: size.width, height: size.height)
    host.layoutSubtreeIfNeeded()
    window.makeKeyAndOrderFront(nil)
    window.displayIfNeeded()
    return Hosted(window: window, size: size)
}

/// The third hosting mode: a render at a **size you name**, for a view that
/// has no height of its own to report.
///
/// `hosted(_:width:)` above asks SwiftUI for `fittingSize`, which is the right
/// question for a card or a row and the wrong one for a pane built around a
/// `List` — a `List` reports no intrinsic height, so that helper would hand
/// back a window with no room to realize a row in and every row-count
/// assertion would read zero. Sized to the window minimum by default, which is
/// the smallest shape the app can actually be in.
///
/// Does not settle: an observation-driven render (a store loaded *after* the
/// pane is on screen, which is the order production runs in) needs `settle`
/// below afterwards, and a static one does not.
@MainActor
func hosted<V: View>(
    _ view: V,
    size: NSSize = NSSize(width: Metrics.windowMinWidth, height: Metrics.windowMinHeight)
) -> Hosted {
    let host = NSHostingView(rootView: view)
    host.frame = NSRect(origin: .zero, size: size)
    let window = NSWindow(
        contentRect: host.frame, styleMask: [.titled], backing: .buffered, defer: false
    )
    window.contentView = host
    window.makeKeyAndOrderFront(nil)
    host.layoutSubtreeIfNeeded()
    window.displayIfNeeded()
    return Hosted(window: window, size: size)
}

/// Give SwiftUI a chance to apply an observation and lay the result out.
///
/// A store written to while a view is on screen publishes on the next
/// main-queue turn, so a synchronous `layoutSubtreeIfNeeded` alone measures
/// the view as it was *before* the change — the trap this exists to avoid.
/// Pumping the run loop, rather than sleeping, is what lets that turn happen
/// at all on the main actor.
@MainActor
func settle(_ hosted: Hosted, seconds: TimeInterval = 0.4) {
    let root = hosted.window.contentView
    root?.layoutSubtreeIfNeeded()
    hosted.window.displayIfNeeded()
    let deadline = Date().addingTimeInterval(seconds)
    while Date() < deadline {
        RunLoop.main.run(until: Date().addingTimeInterval(0.02))
    }
    root?.layoutSubtreeIfNeeded()
    hosted.window.displayIfNeeded()
}

/// The `NSTableView` a SwiftUI `List` is drawn by, found by walking the view
/// tree.
///
/// `numberOfRows` on it is the one fact about a rendered `List` that is
/// reliably readable from a test: the cell contents are not. A `List` cell is
/// a single `NSView` whose SwiftUI contents create no child views to walk, and
/// the accessibility tree is not populated either — `accessibilityChildren()`
/// comes back nil and an in-process `AXUIElement` query returns -25208,
/// measured, with or without the window made key. So a row is countable and
/// its text is not.
///
/// One definition, after four independent copies of this same six-line walk
/// arrived in this target. `SidebarDeviceLayoutTests.listTable` is now a
/// one-line forwarder — it takes that file's own private `Hosted` type, which
/// is not this one — and the two other copies are gone.
///
/// Named `backingTableView` rather than `tableView`: an `XCTestCase` is an
/// `NSObject`, and a bare `tableView(in:)` at file scope loses to an AppKit
/// instance method of that name inside a test class ("use of 'tableView'
/// refers to instance method rather than global function"). Measured, not
/// guessed — it is what the first version of this compiled to.
@MainActor
func backingTableView(in view: NSView?) -> NSTableView? {
    guard let view else { return nil }
    if let table = view as? NSTableView { return table }
    for sub in view.subviews {
        if let found = backingTableView(in: sub) { return found }
    }
    return nil
}

@MainActor
func backingTableView(in hosted: Hosted) -> NSTableView? {
    backingTableView(in: hosted.window.contentView)
}

/// A view hosted at a fixed content size for **measurement**, not for clicking.
///
/// Distinct from `Hosted` above, and the difference is not cosmetic. That one
/// calls `makeKeyAndOrderFront` because a synthetic click is ignored by a
/// non-key window; this one deliberately does not, because a layout
/// measurement has no reason to steal focus from whatever is running the
/// suite. It is also `.resizable`, since some of these cases resize the window
/// to probe a breakpoint.
struct HostedLayout {
    let root: NSView
    /// The window's content size, captured **before** layout. Never
    /// `root.bounds`: `NSHostingView` grows to fit content that demands more
    /// than it was given, so measuring against the host moves the reference
    /// edge along with the defect, and a view that overflows its column
    /// measures as fitting perfectly.
    let contentWidth: CGFloat
    let contentHeight: CGFloat
    /// Retains the window for the lifetime of the measurement; an NSWindow
    /// released mid-test takes its view tree with it.
    let window: NSWindow
}

/// Host `view` at exactly `width` × `height`, lay it out, and hand back the
/// pieces a layout assertion needs.
///
/// Three test files built this identically — `SidebarDeviceLayoutTests`,
/// `DeviceDetailLayoutTests` and `WindowLayoutTests` — each with its own
/// private `Hosted` struct and the same eleven lines after
/// `NSHostingView(rootView:)`. Only the view being hosted ever differed. The
/// duplicated part includes the comment above about not measuring against
/// `root.bounds`, which is the one thing here that is easy to get wrong and
/// was therefore the worst thing to keep three copies of.
@MainActor
func laidOutForMeasurement<V: View>(
    _ view: V, width: CGFloat, height: CGFloat
) -> HostedLayout {
    let host = NSHostingView(rootView: view)
    let window = NSWindow(
        contentRect: NSRect(x: 0, y: 0, width: width, height: height),
        styleMask: [.titled, .resizable], backing: .buffered, defer: false
    )
    window.contentView = host
    host.frame = NSRect(x: 0, y: 0, width: width, height: height)
    host.layoutSubtreeIfNeeded()
    window.displayIfNeeded()
    return HostedLayout(
        root: host, contentWidth: width, contentHeight: height, window: window
    )
}
