import AppKit
import SwiftUI
import XCTest
@testable import Graft

/// `AppIcon` is the one view that draws real artwork, keeping the lettered
/// tile as the fallback for the apps the cascade never resolves anything for.
/// Asserting on a rendered SwiftUI view is expensive, so these check the two
/// computed properties `AppIcon` exposes for exactly this reason —
/// `initials` and `showsArtwork` — rather than the view's body.
@MainActor
final class AppIconTests: XCTestCase {

    func test_falls_back_to_the_letter_tile_with_no_image() {
        let icon = AppIcon(name: "bravo", size: 28)
        XCTAssertFalse(icon.showsArtwork, "no image supplied — this must be the tile")
        XCTAssertNil(icon.image)
    }

    func test_initials_are_uppercased_and_two_characters() {
        XCTAssertEqual(AppIcon(name: "example", size: 28).initials, "EX")
        XCTAssertEqual(AppIcon(name: "bravo", size: 28).initials, "BR")
        // A one-character name yields one character, not a padded two.
        XCTAssertEqual(AppIcon(name: "x", size: 28).initials, "X")
        XCTAssertEqual(AppIcon(name: "", size: 28).initials, "?")
        XCTAssertEqual(AppIcon(name: "   ", size: 28).initials, "?", "whitespace-only is empty, not two spaces")
    }

    func test_an_image_suppresses_the_tile() {
        let image = NSImage(size: NSSize(width: 4, height: 4))
        let icon = AppIcon(name: "example", size: 28, image: image)
        XCTAssertTrue(icon.showsArtwork)
        XCTAssertNotNil(icon.image)
    }
}
