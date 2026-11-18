import SwiftUI
import XCTest
@testable import Graft

/// The banner has to survive whatever string an error arrived with.
///
/// This is the app's one general-purpose failure surface, and the errors it
/// shows are mostly made of URLs and filesystem paths — long tokens with no
/// space to wrap at. A `Text` that demands its ideal width for one of those
/// propagates that demand up through the enclosing `NavigationSplitView`, and
/// the window stops drawing entirely: no sidebar, no list, just chrome over an
/// empty pane, until the app is relaunched.
///
/// Found by clicking Update on a tweak whose published .deb 404s, which is an
/// ordinary upstream state rather than a rare one.
@MainActor
final class ErrorBannerLayoutTests: XCTestCase {

    /// Wider than any window this app opens in, so a pass here is not an
    /// accident of the default window size.
    private let ceiling: CGFloat = 1200

    private func fittingWidth(_ message: String) -> CGFloat {
        let host = NSHostingView(
            rootView: ErrorBanner(message: message, fit: .fullBleed(gutter: 24), onDismiss: {})
        )
        return host.fittingSize.width
    }

    func test_a_long_url_does_not_demand_more_width_than_a_window() {
        // The real one, verbatim.
        let message = "download failed: 404 Client Error: Not Found for url: "
            + "https://example.github.io/repo/debs/example/sometweak/com.example.sometweak_1.7.20_iphoneos-arm64.deb"
        XCTAssertLessThan(
            fittingWidth(message), ceiling,
            "the banner demanded more width than the window, which stops the whole view drawing"
        )
    }

    func test_a_pathological_token_is_still_bounded() {
        // No spaces at all, far longer than anything real — the property is
        // "bounded", not "bounded for the messages we happen to produce".
        XCTAssertLessThan(fittingWidth(String(repeating: "a", count: 4000)), ceiling)
    }

    func test_a_long_filesystem_path_is_bounded() {
        let path = "couldn't write /Users/someone/Library/Application Support/"
            + String(repeating: "nested/", count: 40) + "config.yaml"
        XCTAssertLessThan(fittingWidth(path), ceiling)
    }

    /// And an ordinary message is unaffected — the fix must not have made every
    /// banner render as one clipped line.
    func test_a_short_message_still_lays_out_normally() {
        XCTAssertGreaterThan(fittingWidth("Couldn't reach the device."), 0)
    }
}

/// What the banner must *keep*, not just what it must survive.
///
/// The width hazard is an unwrappable token, never the character count — and
/// bounding the count instead cost real information. A decrypt failure arrives
/// as a short summary plus the sentence saying what to do about it; shortening
/// that pair from the middle at 150 characters ate the end of one and the start
/// of the other, and what reached the user began mid-word.
@MainActor
final class ErrorBannerContentTests: XCTestCase {

    /// The real message, verbatim, from a frida-ios-dump run against Alpha.
    private let realFailure =
        "5 of 128 binaries are still encrypted — all of them app extensions "
        + "hint: these are all app extensions. `--strip-encrypted-extensions` removes "
        + "them and stages the rest, which is usually what you want for a tweaked build "
        + "— the app keeps working, minus its widgets and share sheet. frida-ios-dump "
        + "cannot decrypt extensions at all (they run in their own processes), so "
        + "retrying will not change this."

    func test_the_advice_survives_intact() {
        let shown = ErrorBanner.displayed(realFailure)
        XCTAssertTrue(shown.contains("--strip-encrypted-extensions"),
                      "the flag that fixes it must reach the user")
        XCTAssertTrue(shown.hasSuffix("retrying will not change this."),
                      "the sentence must not be cut off mid-thought")
        XCTAssertFalse(shown.contains("…"), "nothing here needs shortening")
    }

    func test_a_long_path_is_shortened_but_its_sentence_is_not() {
        /// Per-token, so prose around an over-long path keeps its meaning —
        /// where a whole-message cut would take the words too.
        let message = "couldn't write /Users/someone/Library/Application/"
            + String(repeating: "nested/", count: 30)
            + "config.yaml — check the folder exists and is writable"
        let shown = ErrorBanner.displayed(message)
        XCTAssertTrue(shown.hasPrefix("couldn't write "))
        XCTAssertTrue(shown.hasSuffix("check the folder exists and is writable"))
        XCTAssertTrue(shown.contains("…"), "the path itself should be shortened")
    }

    func test_a_wall_of_text_is_still_bounded() {
        /// The overall cap still exists; it is just no longer the thing doing
        /// the damage to ordinary messages.
        let shown = ErrorBanner.displayed(String(repeating: "word ", count: 400))
        XCTAssertLessThanOrEqual(shown.count, ErrorBanner.messageLimit + 1)
    }
}

/// The refusal a dump raises when the extension it would drop *is* the app.
///
/// This is the longest message the pane can be handed, and its last sentence is
/// the only one that tells the user what to do about it. A ceiling that ate the
/// tail would leave a message that states a problem and withholds the remedy.
extension ErrorBannerContentTests {

    private var essentialRefusal: String {
        """
        com.example.blocker cannot be decrypted without losing what it is for: \
        ContentBlocker iOS.appex
          hint: these are not widgets — the app is a shell without them \
        (com.apple.Safari.content-blocker). No backend can decrypt an extension \
        that cannot be launched on demand, so retrying will not help. \
        `--strip-encrypted-extensions` removes them anyway if a partial app is \
        still useful to you.
        """
    }

    func test_the_refusal_keeps_both_the_reason_and_the_remedy() {
        let shown = ErrorBanner.displayed(essentialRefusal)
        XCTAssertTrue(shown.contains("cannot be decrypted without losing what it is for"),
                      "the reason must survive")
        XCTAssertTrue(shown.contains("--strip-encrypted-extensions"),
                      "the remedy must survive — it is the whole point of the message")
        XCTAssertTrue(shown.contains("ContentBlocker iOS.appex"),
                      "and which extension it is about")
    }

    func test_the_extension_point_is_not_shortened_as_a_long_token() {
        /// `com.apple.Safari.content-blocker` is the evidence for the claim
        /// being made; a token ceiling that cut it would leave an assertion
        /// with its citation elided.
        XCTAssertTrue(
            ErrorBanner.displayed(essentialRefusal).contains("com.apple.Safari.content-blocker")
        )
    }
}
