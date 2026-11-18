import SwiftUI

@main
struct GraftApp: App {
    @State private var rebuild = RebuildCoordinator()
    @State private var store = ConfigStore(
        runner: CLIRunner(
            executable: defaultGraftExecutable(),
            environment: ProcessEnvironment.forCLI()
        )
    )

    init() {
        // Before any view reads a preference: settings saved while the app
        // was TweakBuild live under the old bundle id. See `LegacyDefaults`.
        LegacyDefaults.migrateOnLaunch()
    }

    var body: some Scene {
        WindowGroup("Graft") {
            Group {
                if rebuild.phase == .ready {
                    ContentView(store: store)
                } else {
                    // Not an overlay: while a rebuild is running the app is
                    // about to be replaced and quit, so letting ContentView
                    // appear behind it would invite starting a build that this
                    // process will not survive to finish.
                    RebuildGate(coordinator: rebuild)
                }
            }
            // Reads the standing notification permission — no prompt — so
            // the first build's completion banner isn't queued behind an
            // authorization round-trip. See `BuildNotifier.isAuthorized`.
            .task { BuildNotifier.primeAuthorization() }
            // Rebuild the installed app if source changed since it was built.
            // A no-op unless `graft-install` put a repo path in Info.plist, which
            // is what keeps it out of Xcode runs and the test bundle.
            //
            // SwiftUI runs this per *window*, not per launch, so the
            // coordinator latches after the first call — reopening a closed
            // window must not decide to rebuild and quit out from under a
            // session the user is already working in.
            .task { rebuild.startIfStale() }
            .preferredColorScheme(.dark)
                // The app's own accent, applied once at the root so it reaches
                // the things a palette constant cannot: list and table
                // selection, and the prominent button in every sheet. Without
                // it those render in the *system* accent — blue by default and
                // changeable in System Settings — so a user with a red or pink
                // accent got that colour throughout an indigo design.
                //
                // `accentSelect`, not `accent`. The tint is a *fill* that text
                // sits on; `accent` is a foreground colour and putting white on
                // it gives 2.98:1 on every sheet's default button. See
                // `Palette.accentSelect`.
                .tint(Palette.accentSelect)
                // 900, down from 1200. That 1200 was not a design minimum; it
                // was an attempt to outrun the nested split view's overflow by
                // making the window big enough that the clipping fell off the
                // far side of the screen. It never worked — the overflow is
                // exactly one sidebar wide at *every* window size — and with
                // the nesting gone (see `SidebarItem`) there is nothing left
                // for it to buy.
                //
                // What remains is a real limit: one 220pt sidebar minimum plus
                // enough pane for the detail header's control cluster, which is
                // widest mid-build when Build & Install reads "Building —
                // lc-push" and a Stop button appears beside it. 900 leaves that
                // cluster ~680pt and the four-cell meta strip ~165 apiece.
                //
                // The height comes down with it because nothing was holding it
                // at 600: the pane scrolls, and 560 keeps the header, meta
                // strip and delivery card on screen without one.
                .frame(minWidth: Metrics.windowMinWidth, minHeight: Metrics.windowMinHeight)
        }
        .windowResizability(.contentSize)
        .commands {
            CommandGroup(replacing: .newItem) {}
        }
    }
}

private func defaultGraftExecutable() -> URL {
    let home = FileManager.default.homeDirectoryForCurrentUser
    let candidates = [
        home.appendingPathComponent(".local/bin/graft").path,
        "/opt/homebrew/bin/graft",
        "/usr/local/bin/graft",
    ]
    let fm = FileManager.default
    if let found = candidates.first(where: { fm.isExecutableFile(atPath: $0) }) {
        return URL(fileURLWithPath: found)
    }
    return URL(fileURLWithPath: "/usr/bin/env")
}
