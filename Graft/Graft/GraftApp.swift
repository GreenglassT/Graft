import SwiftUI

/// **The composition point.** Everything that can reach past this process
/// with a side effect is named here and handed down, never defaulted: macOS's
/// administrator prompt (`FallbackAdminShell` over `AppleScriptAdminShell` and
/// `OsascriptAdminShell`), the user's login shell (`ShellEnvironment.shared`),
/// the notification center (`BuildNotifier.post`), the rebuild installer and
/// relaunch, the rebuild marker under Caches, the real UserDefaults, and the
/// browser (`AppleSiliconGate`'s download button). A test that forgets a fake
/// then doesn't compile; `NoRealDefaultsTests` keeps a default of any of them
/// from coming back, outside this file.
@main
struct GraftApp: App {
    /// First, before any property below starts a thread or a child (`store`'s
    /// runner starts asking the login shell): take out of this process's
    /// environment what root's `/bin/sh` would import, so Set Up Decryption's
    /// administrator step never hands it over, whatever macOS does about it
    /// (`AdminShell.shellImports`). `unsetenv` isn't safe once another thread
    /// can read the environment, so here and only here. Pinned by
    /// `AdminShellTests`.
    private let shellImportsRemoved = AdminShell.removeShellImports()

    @State private var rebuild = RebuildCoordinator(
        runInstaller: RebuildCoordinator.installFromRepo,
        relaunch: RebuildCoordinator.quitAndReopen
    )
    /// On an Intel Mac `AppleSiliconGate` stands in for every view that would
    /// use this, so nothing runs it, and it is built without `forApp`, whose
    /// `ShellEnvironment.shared` would start asking the login shell.
    @State private var store = ConfigStore(
        runner: AppleSilicon.thisMac
            ? CLIRunner.forApp(shell: .shared)
            : CLIRunner(executable: GraftExecutable.bundled(in: Bundle.main.bundleURL)),
        notify: { BuildNotifier.post($0) }
    )
    /// Set Up Decryption's administrator prompt: in-process NSAppleScript,
    /// and osascript only when that machinery can't run at all.
    private let adminShell: PrivilegedShell = FallbackAdminShell(
        primary: AppleScriptAdminShell(), fallback: OsascriptAdminShell()
    )

    init() {
        // An Intel Mac gets `AppleSiliconGate`'s one screen and nothing else:
        // not even the login shell is asked.
        guard AppleSilicon.thisMac else { return }
        // Before any view reads a preference: settings saved while the app
        // was TweakBuild live under the old bundle id. See `LegacyDefaults`.
        LegacyDefaults.migrateOnLaunch(store: UserDefaults.standard)
        // Start asking the login shell now, off the main thread, so its answer
        // is usually in before the first CLI call waits on it. Touching
        // `shared` is what starts it.
        _ = ShellEnvironment.shared
    }

    var body: some Scene {
        WindowGroup("Graft") {
            // Outermost, so on an Intel Mac neither the app nor these tasks run.
            AppleSiliconGate(
                isAppleSilicon: AppleSilicon.thisMac,
                // The way to 1.1, in the browser. Named here, not taken
                // from SwiftUI's environment in the view (delta audit try13, #15).
                openURL: { NSWorkspace.shared.open($0) }
            ) {
                Group {
                    if rebuild.phase == .ready {
                        ContentView(store: store, adminShell: adminShell)
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
                .task {
                    let marker = SourceWatch.attemptMarker
                    rebuild.startIfStale(
                        isStale: { SourceWatch.staleSource() != nil },
                        recentlyAttempted: { SourceWatch.recentlyAttempted(marker: marker) },
                        recordAttempt: { SourceWatch.recordAttempt(marker: marker) },
                        clearAttempt: { SourceWatch.clearAttempt(marker: marker) }
                    )
                }
            }
            .preferredColorScheme(.dark)
                // The app's own accent, applied once at the root so it reaches
                // the things a palette constant cannot: list and table
                // selection, and the prominent button in every sheet. Without
                // it those render in the *system* accent — blue by default and
                // changeable in System Settings — so a user with a red or pink
                // accent got that color throughout an indigo design.
                //
                // `accentSelect`, not `accent`. The tint is a *fill* that text
                // sits on; `accent` is a foreground color and putting white on
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
            // The standard panel, with credits built here rather than read
            // from a Credits.rtf, so they can carry this copy's version.
            CommandGroup(replacing: .appInfo) {
                Button("About Graft") {
                    NSApplication.shared.orderFrontStandardAboutPanel(options: AboutGraft.panelOptions())
                }
            }
            CommandGroup(after: .help) {
                Button("Licenses") {
                    if let folder = BundledLicenses.installed { NSWorkspace.shared.open(folder) }
                }
                .disabled(BundledLicenses.installed == nil)
            }
        }
    }
}

/// Graft › About Graft: the standard About panel, with what Graft.app is
/// licensed under and where its source is.
enum AboutGraft {
    /// The license statement, word for word. The release docs quote this
    /// constant, `<version>` and all; `notice(version:)` fills it in.
    ///
    /// Not "Graft is MIT licensed", which it said until 2.0: that is true of
    /// Graft's own code and not of the download, which bundles GPL programs
    /// and is distributed under the GPL (review part 2, #20 and #25). And not
    /// "the source for all of it": the archive holds what the licenses ask
    /// for and the source of every tool the release builds itself, UnfairPlay
    /// and ipatool among them, but not the prebuilt zsign's or CPython's
    /// (combined fix check #12; the try14 delta audit, F10).
    static let licenceNotice = "Graft's own code is MIT licensed. Graft.app also bundles other "
        + "programs, each under its own license — GPL, LGPL, MPL, APSL and AGPL among them — and "
        + "the command-line program inside it is distributed under GPL-3.0-or-later. See Help › "
        + "Licenses; the source for Graft and for the components whose licenses require it is in "
        + "Graft-<version>-sources.tar.gz on the release page."

    /// The release page the notice means: each release carries its source
    /// archive beside Graft.zip.
    static let releasesURL = URL(string: "https://github.com/GreenglassT/Graft/releases")!

    static func notice(version: String) -> String {
        licenceNotice.replacingOccurrences(of: "<version>", with: version)
    }

    /// The version a bundle says it is, as the About panel shows it.
    static func version(of bundle: Bundle) -> String {
        bundle.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?"
    }

    /// The panel's credits: the notice for `version`, then the release page,
    /// linked.
    static func credits(version: String) -> NSAttributedString {
        let font = NSFont.systemFont(ofSize: NSFont.smallSystemFontSize)
        let body: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: NSColor.labelColor]
        let text = NSMutableAttributedString(string: notice(version: version) + "\n\n", attributes: body)
        var link = body
        link[.link] = releasesURL
        text.append(NSAttributedString(string: releasesURL.absoluteString, attributes: link))
        return text
    }

    static func panelOptions(bundle: Bundle = .main) -> [NSApplication.AboutPanelOptionKey: Any] {
        [.credits: credits(version: version(of: bundle))]
    }
}

/// Help › Licenses: the GPL text and every bundled component's license and
/// NOTICE files, which the release build puts in `Contents/Resources/Licenses`.
enum BundledLicenses {
    /// The folder, when it is there; nil in a Debug build, which has none.
    static func folder(resourceURL: URL?, isDirectory: (String) -> Bool) -> URL? {
        guard let folder = resourceURL?.appendingPathComponent("Licenses", isDirectory: true),
              isDirectory(folder.path)
        else { return nil }
        return folder
    }

    /// This app's, looked up once: a bundle's contents don't change under it.
    static let installed: URL? = folder(resourceURL: Bundle.main.resourceURL) { path in
        var isDirectory: ObjCBool = false
        return FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory) && isDirectory.boolValue
    }
}
