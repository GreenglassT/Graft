import SwiftUI

/// **The composition point.** Everything that can reach past this process
/// with a side effect is named here and handed down, never defaulted: macOS's
/// administrator prompt (`FallbackAdminShell` over `AppleScriptAdminShell` and
/// `OsascriptAdminShell`), the user's login shell (`ShellEnvironment.shared`),
/// the notification center (`BuildNotifier.post`), the rebuild installer and
/// relaunch, the rebuild marker under Caches, the real UserDefaults, the clock,
/// the browser (`AppleSiliconGate`'s releases-page buttons, `UpdateStore`'s
/// release page, What's New's links, Get PlayCover), and LaunchServices'
/// answer for whether PlayCover is installed. A test that forgets a fake
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

    /// What this copy of Graft can do on this Mac (`MacSupport`), read once:
    /// whether the window runs at all (`AppleSiliconGate`), and which
    /// actions need Apple silicon. Every view gets it from the gate, as
    /// `\.macSupport`.
    private let support = MacSupport.thisMac

    /// Answers macOS's quit while Graft works (`GraftAppDelegate`).
    @NSApplicationDelegateAdaptor(GraftAppDelegate.self) private var appDelegate

    @State private var rebuild = RebuildCoordinator(
        runInstaller: RebuildCoordinator.installFromRepo,
        relaunch: RebuildCoordinator.quitAndReopen
    )
    /// Its runner is the app's CLI on an Intel Mac as on Apple silicon
    /// (`runnerChoice`): the Intel download runs its own bundled CLI exactly
    /// as the Apple silicon one does.
    @State private var store: ConfigStore
    /// Whether a newer Graft is out, and updating to it: the window's banner
    /// and daily check, Help › Check for Updates…, and the install on the
    /// way out (`GraftAppDelegate`). Here rather than in `ContentView`
    /// because the menu and the delegate need it. The same runner as
    /// `store`'s.
    @State private var updates: UpdateStore
    /// Help › Remove Graft's Data…: its sheet, over the main window. Here
    /// because the menu reaches it. The same runner as `store`'s.
    @State private var removeData: RemoveDataStore
    /// Set Up Decryption's administrator prompt: in-process NSAppleScript,
    /// and osascript only when that machinery can't run at all.
    private let adminShell: PrivilegedShell = FallbackAdminShell(
        primary: AppleScriptAdminShell(), fallback: OsascriptAdminShell()
    )

    init() {
        // After `shellImportsRemoved`, which as a stored property's initial
        // value runs before this body.
        // A test host is this app under its real bundle id and the real
        // HOME, so its own window must run nothing: in a Debug build the
        // lookup finds ~/.local/bin/graft first, the owner's real CLI, and
        // its App Store status asked macOS for ipatool's keychain item
        // (2.0.1). Its runner points nowhere, and its window is a placeholder.
        let runner: CLIRunner
        switch Self.runnerChoice(insideTests: AdminShell.insideTests, support: support) {
        case .testHost: runner = CLIRunner(executable: Self.testHostExecutable)
        case .app: runner = CLIRunner.forApp(shell: .shared)
        case .unused: runner = CLIRunner(executable: GraftExecutable.bundled(in: Bundle.main.bundleURL))
        }
        _store = State(initialValue: ConfigStore(runner: runner, notify: { BuildNotifier.post($0) }))
        _updates = State(initialValue: UpdateStore(
            runner: runner, defaults: .standard, now: Date.init,
            appVersion: AboutGraft.version(of: .main), open: { NSWorkspace.shared.open($0) },
            updateLog: URL.libraryDirectory.appending(path: "Logs/Graft/update.log"),
            host: SelfUpdateHost.running()
        ))
        _removeData = State(initialValue: RemoveDataStore(
            runner: runner, hold: .shared,
            // The app's own preferences, which the CLI only lists. Never in a
            // test host, which is this app under its real bundle id.
            removeAppSettings: {
                guard !AdminShell.insideTests, let domain = Bundle.main.bundleIdentifier else { return }
                UserDefaults.standard.removePersistentDomain(forName: domain)
            },
            quit: { NSApp.terminate(nil) }
        ))
        // A copy that can't run here gets `AppleSiliconGate`'s one screen and
        // nothing else: not even the login shell is asked. Nor does a test host.
        guard support.runsApp, !AdminShell.insideTests else { return }
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
            if AdminShell.insideTests {
                TestHostWindow()
            } else {
            // Outermost, so where this copy can't run, neither the app nor
            // these tasks do.
            AppleSiliconGate(
                support: support,
                // The releases page, in the browser. Named here, not taken
                // from SwiftUI's environment in the view (delta audit try13, #15).
                openURL: { NSWorkspace.shared.open($0) }
            ) {
                Group {
                    if rebuild.phase == .ready {
                        ContentView(
                            store: store, adminShell: adminShell, updates: updates,
                            // Set Up Decryption's "Open <App>", named here
                            // like `openURL` above (`NoRealDefaultsTests`).
                            openApp: {
                                NSWorkspace.shared.openApplication(at: $0, configuration: NSWorkspace.OpenConfiguration())
                            },
                            // Build options' "This Mac": whether LaunchServices
                            // has PlayCover under the bundle id the handoff
                            // opens, and Get PlayCover's page in the browser.
                            // Nothing here downloads or launches PlayCover.
                            playCover: PlayCover(
                                lookup: { NSWorkspace.shared.urlForApplication(withBundleIdentifier: $0) },
                                openURL: { NSWorkspace.shared.open($0) }
                            )
                        )
                        // A quit installs a checked update on the way out
                        // (`UpdateStore.handOver`). Here, not in the
                        // delegate's init, which `NSApplicationDelegateAdaptor`
                        // calls with no arguments; and not in a test host,
                        // whose window is the placeholder above.
                        .task { appDelegate.handOverQuits(to: updates) }
                        // What's New, once after an update. Not in a test
                        // host, which is this app under its real bundle id:
                        // it would record the test build's version in the
                        // real preferences (as `LegacyDefaults` skips it).
                        .modifier(OpensWhatsNewAfterUpdate(
                            defaults: AdminShell.insideTests ? nil : .standard,
                            version: AboutGraft.version(of: .main)
                        ))
                        // Help › Remove Graft's Data…, over the setup
                        // screen as over the panes: someone who gave up
                        // during setup may want it most.
                        .sheet(isPresented: $removeData.isPresented) {
                            RemoveDataSheet(store: removeData)
                        }
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
                // Says what it found in the window's alert (`UpdateStore.answerAlert`).
                // Not where this copy can't run, which runs no CLI at all.
                Button("Check for Updates…") {
                    Task { await updates.checkNow() }
                }
                .disabled(!support.runsApp)
                WhatsNewCommand()
                Button("Licenses") {
                    if let folder = BundledLicenses.installed { NSWorkspace.shared.open(folder) }
                }
                .disabled(BundledLicenses.installed == nil)
                Divider()
                // Pages Graft opens only when chosen, in the browser (docs/network.md).
                ForEach(SupportLinks.all, id: \.title) { link in
                    Button(link.title) { NSWorkspace.shared.open(link.url) }
                }
                Divider()
                // What Graft keeps outside Graft.app, before moving it to the
                // Trash. Not where this copy can't run, which runs no CLI, nor
                // while a rebuild replaces the app.
                Button(RemoveDataSheet.menuTitle) { removeData.open() }
                    .disabled(!support.runsApp || rebuild.phase != .ready)
            }
        }

        // Help › What's New…: the changelog this copy carries. One window,
        // which the menu opens or brings forward.
        Window(WhatsNew.windowTitle, id: WhatsNew.windowID) {
            WhatsNewView(changelog: Changelog.bundled, open: { NSWorkspace.shared.open($0) })
                .preferredColorScheme(.dark)
        }
        .defaultSize(width: 680, height: 720)
    }
}

/// The app's delegate, for one thing: answering macOS's quit, ⌘Q's and the
/// one a logout, restart or shutdown sends, while Graft works
/// (`QuitGuard`). Named here, with the real alert, the real reply and the
/// real run loop, because `NSApplicationDelegateAdaptor` builds it with no
/// arguments.
@MainActor
final class GraftAppDelegate: NSObject, NSApplicationDelegate {
    private lazy var quitGuard = QuitGuard(
        hold: .shared,
        present: QuitAlert.present,
        reply: { NSApp.reply(toApplicationShouldTerminate: $0) },
        schedule: QuitAlert.afterDelay
    )

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        quitGuard.shouldTerminate()
    }

    /// What a quit does on the way out, once nothing is held: an update's
    /// install (`QuitHandover`).
    func handOverQuits(to handover: QuitHandover) {
        quitGuard.handover = handover
    }
}

extension SelfUpdateHost {
    /// This app, when it may replace itself: a release build that runs on
    /// this Mac (`MacSupport.runsApp`: either download, on a Mac that can run
    /// it; the CLI fetches this download's own zip, `app_update.own_flavor`),
    /// outside a test host, and not one `graft-install` built from source
    /// (`SourceWatch.repoRoot`), which its own rebuild replaces. Nil
    /// otherwise, and the banner links the release page instead.
    static func running() -> SelfUpdateHost? {
        #if DEBUG
        return nil
        #else
        guard MacSupport.thisMac.runsApp, !AdminShell.insideTests, SourceWatch.repoRoot == nil else { return nil }
        return SelfUpdateHost(
            appPath: Bundle.main.bundleURL.path,
            pid: ProcessInfo.processInfo.processIdentifier,
            hold: .shared,
            terminate: { NSApp.terminate(nil) }
        )
        #endif
    }
}

/// `QuitGuard`'s question as an app-modal alert, and its clock as run-loop
/// timers.
enum QuitAlert {
    /// Runs `block` once, `delay` seconds from now, on a main run-loop timer
    /// in the common modes. Not a `Task` or a main-queue block, for two
    /// reasons. While macOS waits for `.terminateLater`'s reply it runs the
    /// run loop in the modal-panel mode, one of the common ones. And an
    /// alert run from inside a main-queue block keeps the main queue, and
    /// every main-actor task with it (the running work's own), from running
    /// until the alert closes; a timer's callout is not such a block.
    static func afterDelay(_ delay: TimeInterval, _ block: @escaping @MainActor () -> Void) {
        let timer = Timer(timeInterval: delay, repeats: false) { _ in
            MainActor.assumeIsolated { block() }
        }
        RunLoop.main.add(timer, forMode: .common)
    }

    /// The alert, once `applicationShouldTerminate` has returned, as
    /// `QuitGuard.Present` asks. Keep Working is the default button, so
    /// Return keeps Graft working.
    ///
    /// Never on screen in a test host (`AdminShell.insideTests`: a test's
    /// safety can't rest on the code it tests): answered Quit Anyway, so the
    /// host can still end.
    @MainActor
    static func present(
        _ question: QuitQuestion, answer: @escaping @MainActor (QuitAnswer) -> Void
    ) -> QuitGuard.TakeDown {
        if AdminShell.insideTests {
            afterDelay(0) { answer(.quitAnyway) }
            return { then in then() }
        }
        let shown = Shown()
        afterDelay(0) {
            guard !shown.takenDown else { return }
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = question.title
            alert.informativeText = question.detail
            alert.addButton(withTitle: QuitQuestion.keepWorking)
            alert.addButton(withTitle: QuitQuestion.quitAnyway).hasDestructiveAction = true
            shown.alert = alert
            NSApp.activate()
            let response = alert.runModal()
            shown.alert = nil
            if let then = shown.afterTakeDown {
                shown.afterTakeDown = nil
                return then()
            }
            answer(response == .alertSecondButtonReturn ? .quitAnyway : .keepWorking)
        }
        return { then in
            shown.takenDown = true
            if let alert = shown.alert, NSApp.modalWindow === alert.window {
                // Ended once `runModal` has returned, above: abortModal is
                // the way out of a modal loop from a timer's callout.
                shown.afterTakeDown = then
                NSApp.abortModal()
            } else {
                then()
            }
        }
    }

    /// One alert's state, between the timer that shows it and the call
    /// that takes it down.
    @MainActor
    private final class Shown {
        var alert: NSAlert?
        var takenDown = false
        var afterTakeDown: (@MainActor () -> Void)?
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
    /// linked, then each of `SupportLinks.all`.
    static func credits(version: String) -> NSAttributedString {
        let font = NSFont.systemFont(ofSize: NSFont.smallSystemFontSize)
        let body: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: NSColor.labelColor]
        let text = NSMutableAttributedString(string: notice(version: version) + "\n\n", attributes: body)
        var link = body
        link[.link] = releasesURL
        text.append(NSAttributedString(string: releasesURL.absoluteString, attributes: link))
        text.append(NSAttributedString(string: "\n\n", attributes: body))
        for (index, (title, url)) in SupportLinks.all.enumerated() {
            if index > 0 { text.append(NSAttributedString(string: "  ·  ", attributes: body)) }
            var support = body
            support[.link] = url
            text.append(NSAttributedString(string: title, attributes: support))
        }
        return text
    }

    static func panelOptions(bundle: Bundle = .main) -> [NSApplication.AboutPanelOptionKey: Any] {
        [.credits: credits(version: version(of: bundle))]
    }
}

/// Help's Star on GitHub, Support on Ko-fi and the community invite (the Graft Discord),
/// with the same links in About Graft and Settings › Version. Graft opens them
/// only when they're chosen, in the browser, like every other page it links
/// (docs/network.md).
enum SupportLinks {
    static let starTitle = "Star Graft on GitHub"
    static let supportTitle = "Support Graft on Ko-fi"
    static let repository = URL(string: "https://github.com/GreenglassT/Graft")!
    static let koFi = URL(string: "https://ko-fi.com/greenglasst")!
    /// Always "Graft Discord" in full: the release scans allow that exact
    /// phrase (`#public-allow`), not the word on its own.
    static let communityTitle = "Join the Graft Discord"
    static let communityInvite = URL(string: "https://discord.gg/dnJTnu3X7X")!

    /// Each, in the order the Help menu, About Graft and Settings show them.
    static let all: [(title: String, url: URL)] = [
        (starTitle, repository), (supportTitle, koFi), (communityTitle, communityInvite),
    ]
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

extension GraftApp {
    /// What a test host's runner points at: nothing, so a call from its own
    /// window fails at once instead of running a real `graft`. Out here, so
    /// `shellImportsRemoved` stays the first thing `GraftApp` declares.
    static let testHostExecutable = URL(fileURLWithPath: "/var/empty/graft-test-host-runs-nothing")

    /// Which runner the app's stores share.
    enum RunnerChoice: Equatable {
        /// A test host's: `testHostExecutable`, which runs nothing.
        case testHost
        /// The app's own CLI, as `CLIRunner.forApp` finds it in the login
        /// shell's environment: on an Intel Mac as on Apple silicon, since
        /// the Intel download runs its CLI just as the other one does.
        case app
        /// A copy that can't run here (`MacSupport.runsApp`): the gate stands
        /// in for every view that would use the runner, so nothing does.
        /// Built without `forApp`, whose `ShellEnvironment.shared` would start
        /// asking the login shell for nothing.
        case unused
    }

    /// The runner for `support`: it depends on whether the app runs at all,
    /// never on which Mac it runs on.
    static func runnerChoice(insideTests: Bool, support: MacSupport) -> RunnerChoice {
        if insideTests { return .testHost }
        return support.runsApp ? .app : .unused
    }
}

/// The app's own window in a test host: what it says, and nothing else.
/// No store loads, polls or asks anything here (`GraftApp.init`).
struct TestHostWindow: View {
    static let text = "Graft test host: this window runs nothing."

    var body: some View {
        Text(Self.text)
            .frame(minWidth: 320, minHeight: 120)
    }
}
