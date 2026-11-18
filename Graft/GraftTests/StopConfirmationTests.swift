import SwiftUI
import XCTest
@testable import Graft

/// `stopBuildConfirmation`'s consequence sentence still reaches the alert.
///
/// The sentence is passed as a closure rather than a `String`, so that reading
/// `ConfigStore.cancelConsequence` does not make `buildStage` a dependency of
/// the permanently-mounted sidebar (see the modifier's own doc). Deferring an
/// evaluation is only safe if something still performs it, and this is the one
/// destructive confirmation in the app: an unevaluated closure means a Stop
/// dialog with an empty body, on the dialog whose whole job is to say what
/// stopping costs.
@MainActor
final class StopConfirmationTests: XCTestCase {

    private struct Host: View {
        @Binding var pending: String?
        let consequence: () -> String

        var body: some View {
            Text("host")
                .stopBuildConfirmation(
                    presenting: $pending,
                    name: { $0 },
                    consequence: consequence,
                    onStop: { _ in }
                )
        }
    }

    /// Presented, the alert asks for the sentence.
    func test_the_consequence_is_read_when_the_confirmation_is_presented() async {
        var asked = 0
        var pending: String? = "bravo"
        let binding = Binding(get: { pending }, set: { pending = $0 })
        let view = Host(pending: binding, consequence: {
            asked += 1
            return "Nothing has reached the device yet."
        })

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 400, height: 300),
            styleMask: [.titled], backing: .buffered, defer: false
        )
        window.contentView = NSHostingView(rootView: view)
        window.contentView?.layoutSubtreeIfNeeded()
        window.displayIfNeeded()
        // The alert is presented asynchronously off the presentation binding.
        try? await Task.sleep(nanoseconds: 500_000_000)

        XCTAssertGreaterThan(
            asked, 0,
            "the consequence closure was never called, so the Stop confirmation would "
            + "show an empty body — the one dialog that has to say what stopping costs"
        )
    }

    /// Not presented, the alert does not ask — which is the entire point of
    /// passing a closure.
    ///
    /// If SwiftUI evaluated the message builder eagerly, deferring the read
    /// would buy nothing: `ConfigStore.cancelConsequence` would still be
    /// touched during `body`, `buildStage` would still be a dependency of the
    /// whole sidebar, and every stage of every build would still re-evaluate
    /// the app's navigation column. This is the half that proves the change
    /// did what it was made to do.
    func test_the_consequence_is_not_read_while_the_confirmation_is_dismissed() async {
        var asked = 0
        var pending: String?
        let binding = Binding(get: { pending }, set: { pending = $0 })
        let view = Host(pending: binding, consequence: {
            asked += 1
            return "should not be needed"
        })

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 400, height: 300),
            styleMask: [.titled], backing: .buffered, defer: false
        )
        window.contentView = NSHostingView(rootView: view)
        window.contentView?.layoutSubtreeIfNeeded()
        window.displayIfNeeded()
        try? await Task.sleep(nanoseconds: 500_000_000)

        XCTAssertEqual(
            asked, 0,
            "the consequence was read with no confirmation on screen, so passing it "
            + "as a closure defers nothing and the sidebar still depends on buildStage"
        )
    }
}
