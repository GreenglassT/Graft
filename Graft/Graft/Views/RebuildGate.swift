import AppKit
import Observation
import SwiftUI

/// Rebuilds the installed app when its source has changed, before the app is
/// used.
///
/// The behavior `scripts/tb-launcher` used to provide, moved inside the real
/// binary. The shell version could rebuild *before* the process started, which
/// was tidier — but it could only do that by being the bundle's declared
/// executable and exec'ing the Swift binary under another name, which cost the
/// app its notification identity (see `SourceWatch`). So the sequence is now
/// launch, notice, rebuild, relaunch, and the window below exists to say so
/// rather than leave the app looking hung for the half minute that takes.
@MainActor
@Observable
final class RebuildCoordinator {
    enum Phase: Equatable {
        /// Nothing to do — run the app.
        case ready
        case rebuilding
        /// The build failed. Carries the installer's output, and the app is
        /// still usable: the old binary works, it is merely out of date.
        case failed(log: String)
    }

    private(set) var phase: Phase = .ready

    /// Where a failed rebuild's output is kept after the window is dismissed.
    private(set) var logPath: String?

    /// Set the first time `startIfStale` runs, so it runs once per *process*.
    ///
    /// The call site is a `.task` on the window's content, which SwiftUI runs
    /// per window rather than per launch. Without this, closing the window and
    /// reopening it from the Dock re-ran the check — and a rebuild that decides
    /// to quit the app is not something to do in the middle of a session the
    /// user is already working in.
    private var hasChecked = false

    /// Injected so a test can drive the whole state machine without building
    /// anything. Returns nil on success, or the installer's output on failure.
    var runInstaller: () async -> String?
    /// Injected for the same reason — terminating the app mid-test is not a
    /// thing a test can come back from. Returns false when it could not
    /// arrange the relaunch, in which case it must *not* have quit.
    var relaunch: () -> Bool

    /// No defaults: the real installer and relaunch (`installFromRepo`,
    /// `quitAndReopen`) are named in `GraftApp` only (`NoRealDefaultsTests`).
    init(
        runInstaller: @escaping () async -> String?,
        relaunch: @escaping () -> Bool
    ) {
        self.runInstaller = runInstaller
        self.relaunch = relaunch
    }

    /// Decide, at launch, whether to rebuild. Everything here is cheap enough
    /// to run before the first frame: an Info.plist read, a marker stat, and a
    /// directory walk that stops at the first newer file.
    ///
    /// No defaults: the real ones read and write the marker under the user's
    /// Caches folder, and `GraftApp` names them (`NoRealDefaultsTests`).
    func startIfStale(
        isStale: () -> Bool,
        recentlyAttempted: () -> Bool,
        recordAttempt: () -> Void,
        clearAttempt: @escaping () -> Void
    ) {
        guard !hasChecked else { return }
        hasChecked = true
        // The marker is checked *before* staleness, not after. A failed build
        // leaves the source still newer than the binary, so the relaunched app
        // is stale again by definition — without this, a project that does not
        // compile makes the app unopenable rather than merely out of date.
        guard !recentlyAttempted(), isStale() else { return }
        recordAttempt()
        phase = .rebuilding
        Task { await run(clearAttempt: clearAttempt) }
    }

    private func run(clearAttempt: @escaping () -> Void) async {
        let outcome = await withTimeout(Self.installTimeout) { [runInstaller] in
            await runInstaller()
        }
        switch outcome {
        case .none:
            // Timed out. Deliberately does not kill the installer: it stages
            // its work and swaps at the end, so letting it finish is harmless,
            // while killing it mid-`codesign` is not.
            phase = .failed(log: """
                The rebuild has been running for \(Int(Self.installTimeout / 60)) minutes \
                and has not finished. It is most likely waiting on something invisible — \
                an Xcode license prompt, or a keychain dialog this window is covering.

                Run scripts/graft-install from a terminal to see what it is asking for.
                """)
        case let .some(log?):
            logPath = Self.persist(log: log)
            phase = .failed(log: log)
        case .some(nil):
            // Cleared only on success, and before the relaunch that ends this
            // process — so an edit made a minute later rebuilds immediately
            // rather than being suppressed by the window that exists for
            // repeated *failures*.
            clearAttempt()
            guard relaunch() else {
                // The new binary is installed but nothing can reopen the app,
                // so the running process is the only one there is. Say so
                // instead of quitting into nothing or spinning forever.
                phase = .failed(log: """
                    The rebuild finished, but the app could not schedule its own \
                    relaunch, so it is still running the previous build.

                    Quit and open Graft again to pick up the new one.
                    """)
                return
            }
        }
    }

    /// `operation`'s result, or nil if it outlasted `seconds`.
    private func withTimeout(
        _ seconds: TimeInterval, _ operation: @escaping () async -> String?
    ) async -> String?? {
        await withTaskGroup(of: String??.self) { group in
            group.addTask { .some(await operation()) }
            group.addTask {
                try? await Task.sleep(for: .seconds(seconds))
                return nil
            }
            let first = await group.next() ?? .some(nil)
            group.cancelAll()
            return first
        }
    }

    /// Generous on purpose: a cold `xcodebuild` after a derived-data wipe is
    /// minutes, and a spurious timeout on a build that was going to succeed
    /// would be worse than waiting.
    static let installTimeout: TimeInterval = 600

    /// Give up on the rebuild and use the app as it is.
    func dismissFailure() {
        phase = .ready
    }

    // MARK: - The real implementations

    /// Keep the build output somewhere the window closing cannot destroy.
    ///
    /// The failure panel's default button is "Open the previous build", so
    /// Return discards the log — and it was the only copy. The deleted
    /// `tb-launcher` at least wrote a log under `/tmp`; nothing
    /// replaced it until now.
    @discardableResult
    static func persist(log: String) -> String? {
        let url = URL.libraryDirectory.appending(path: "Logs/Graft/rebuild.log")
        do {
            try FileManager.default.createDirectory(
                at: url.deletingLastPathComponent(), withIntermediateDirectories: true
            )
            try log.write(to: url, atomically: true, encoding: .utf8)
            return url.path
        } catch {
            return nil
        }
    }

    /// The real installer. Named only by `GraftApp`, the composition point.
    static func installFromRepo() async -> String? {
        guard let repoRoot = SourceWatch.repoRoot else { return "no repo path in Info.plist" }
        let script = repoRoot.appending(path: "scripts/graft-install")

        return await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                let process = Process()
                process.executableURL = script
                // `forCLI`, for the reason every other subprocess here uses it:
                // an app launched from Finder inherits a PATH with none of the
                // tool directories on it, and `xcodebuild` is not the only
                // thing this script needs to find.
                var environment = ProcessEnvironment.forCLI()
                // graft-install's first act is to quit every running Graft,
                // which here is the process that started it — it would kill its
                // own parent and then be killed as its child.
                environment["GRAFT_SKIP_QUIT"] = "1"
                process.environment = environment

                let pipe = Pipe()
                process.standardOutput = pipe
                process.standardError = pipe
                do {
                    try process.run()
                } catch {
                    continuation.resume(returning: error.localizedDescription)
                    return
                }
                // Read to EOF before waiting: the script's output is small, but
                // a full pipe buffer with nobody draining it deadlocks the
                // child, and this one holds an xcodebuild log on failure.
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                let text = String(data: data, encoding: .utf8) ?? ""
                continuation.resume(returning: process.terminationStatus == 0 ? nil : text)
            }
        }
    }

    /// Quit, then reopen once this process is actually gone.
    ///
    /// The waiting is what makes it work: `open` on a bundle whose app is still
    /// terminating activates the dying instance instead of starting the new
    /// one, and the user is left staring at a window that is about to vanish.
    /// The helper is a detached `/bin/sh`, so it outlives the app that spawned
    /// it — which is the entire trick, since nothing inside this process can
    /// run after this process ends.
    /// The real relaunch. Named only by `GraftApp`, the composition point.
    static func quitAndReopen() -> Bool {
        // This process's pid, not `pgrep -x Graft`. A debug build running
        // from Xcode carries the same process name and the same bundle id, so
        // matching by name waited on *that* one, burned the whole timeout, and
        // then reopened anyway — the case the wait exists to prevent. `kill -0`
        // on a known pid asks the only question that matters: has the process
        // that spawned this helper gone away.
        let pid = ProcessInfo.processInfo.processIdentifier
        // `SourceWatch.installedBundle`, not `Bundle.main`: graft-install writes
        // to /Applications unconditionally, so that is the bundle that now
        // holds the new binary. `staleSource` already refuses to rebuild from
        // anywhere else, and this keeps the two answers consistent.
        let bundle = SourceWatch.installedBundle.path

        let helper = Process()
        helper.executableURL = URL(fileURLWithPath: "/bin/sh")
        helper.arguments = [
            "-c",
            // Bounded, so a process that refuses to exit leaves a stale app
            // running rather than a shell spinning forever.
            """
            for _ in $(seq 150); do
              kill -0 \(pid) 2>/dev/null || break
              sleep 0.2
            done
            open \(shellQuoted(bundle))
            """,
        ]
        do {
            try helper.run()
        } catch {
            // Quitting now would take the app away with nothing scheduled to
            // bring it back — from the user's side, launching it made it
            // vanish. The new binary is already installed, so staying on the
            // old one until the next launch is the mild failure.
            return false
        }
        NSApp.terminate(nil)
        return true
    }

    /// Single-quote for `sh`, closing and reopening around any embedded quote.
    /// The bundle path is `Bundle.main`'s and not user input, but it is a path
    /// and paths are the thing that eventually contains a space.
    private static func shellQuoted(_ value: String) -> String {
        "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }
}

/// What the window shows while the app is replacing itself.
struct RebuildGate: View {
    let coordinator: RebuildCoordinator

    var body: some View {
        VStack(spacing: 16) {
            switch coordinator.phase {
            case .ready:
                EmptyView()

            case .rebuilding:
                ProgressView().controlSize(.large)
                Text("Rebuilding Graft…")
                    .font(AppFont.bodyMedium)
                    .foregroundStyle(Palette.text)
                Text("Your source changed since this build was installed. "
                     + "The app will reopen when it's done.")
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textMuted)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)

            case let .failed(log):
                Text("Rebuild failed")
                    .font(AppFont.bodyMedium)
                    .foregroundStyle(Palette.danger)
                // The old shell version put this in an `osascript` alert that
                // showed a log *path*. The text is what anybody actually wants,
                // and a build error is many lines of it.
                ScrollView {
                    Text(log.isEmpty ? "The installer produced no output." : log)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textMuted)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: 260)
                .padding(Metrics.innerPadding)
                .background(Palette.surface2.opacity(0.5))
                .clipShape(RoundedRectangle(cornerRadius: Metrics.radiusInner))

                // Dismissing drops the string, so say where it also lives.
                if let path = coordinator.logPath {
                    Text("Saved to \(path)")
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.textDim)
                        .textSelection(.enabled)
                }

                HStack {
                    Button("Copy log") { Pasteboard.copy(log) }
                    // Not "Quit": the previous build still runs, it is only out
                    // of date, and refusing to open at all over a compile error
                    // in a file the user may be about to fix is worse than
                    // saying so and carrying on.
                    Button("Open the previous build") { coordinator.dismissFailure() }
                        .keyboardShortcut(.defaultAction)
                }
            }
        }
        .padding(Metrics.paneGutter)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Palette.bg)
    }
}
