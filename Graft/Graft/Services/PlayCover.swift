import Foundation

/// PlayCover, the free app a build for this Mac is handed to. Graft doesn't
/// include it, and never downloads, installs or launches it: when it isn't
/// installed, Build options says so and Get PlayCover opens its releases page
/// in the browser, where the user gets it themselves.
///
/// **Asked through LaunchServices, for the bundle id the handoff uses.**
/// `--install-to-mac` hands a build over with `open -b io.playcover.PlayCover`,
/// which fails when LaunchServices has no app registered for that id
/// ("LSCopyApplicationURLsForBundleIdentifier() failed"). `lookup` asks the
/// same registry for the same id, so "not installed" here is the case where
/// the handoff would fail for that reason. Not a path such as
/// `/Applications/PlayCover.app`, and not Spotlight: the reasons the CLI's
/// design (§5) gives for not checking those still hold.
///
/// The CLI still learns it from `open -b`'s exit, and says so with its own
/// hint (`PLAYCOVER_RELEASES_URL` in cli.py). This only lets the pane say it
/// before the build starts.
///
/// Both closures are passed in: `GraftApp` names NSWorkspace's, and a test its
/// own, so no test depends on whether this Mac has PlayCover, and none opens a
/// browser (`NoRealDefaultsTests`).
struct PlayCover {
    /// cli.py's `PLAYCOVER_BUNDLE_ID`: the id `--install-to-mac` hands a build
    /// to. `PlayCoverNoticeTests` reads the CLI's value to keep the two equal.
    static let bundleID = "io.playcover.PlayCover"

    /// PlayCover's official releases page on GitHub, which Get PlayCover
    /// opens. The CLI's hint names the same page.
    static let releasesURL = URL(string: "https://github.com/PlayCover/PlayCover/releases")!

    /// Where LaunchServices has the app with this bundle id, or nil when it
    /// has none: `NSWorkspace.urlForApplication(withBundleIdentifier:)` in the
    /// app, an answer of the test's choosing in a test.
    let lookup: (String) -> URL?

    /// Opens a page: the browser in the app, a recorder in a test.
    let openURL: (URL) -> Void

    /// Finds no PlayCover and opens nothing. `ContentView`'s default, which
    /// every test that hosts the window takes.
    static let inert = PlayCover(lookup: { _ in nil }, openURL: { _ in })

    /// Whether LaunchServices has an app for `bundleID`. Asked each time it's
    /// read, so installing PlayCover while Graft is open counts the next time
    /// Build options draws.
    var isInstalled: Bool { lookup(Self.bundleID) != nil }

    /// Get PlayCover: the releases page, in the browser. Nothing is downloaded.
    func openReleasesPage() { openURL(Self.releasesURL) }
}
