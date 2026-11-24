import AppKit
import Foundation
import UserNotifications

/// Tells the user a build finished when they are not looking at the app.
///
/// A build runs for minutes — a 134 MB download, cyan, zsign, then a push over
/// USB — and the only thing that ever said it was over was a log panel the user
/// had already tabbed away from. That is the whole feature: the outcome finds
/// the user instead of the user going back to check.
///
/// Split deliberately into a decision and a delivery. `note(for:)` is pure and
/// exhaustively tested; `post(_:)` is the part that talks to
/// `UNUserNotificationCenter`, which cannot be exercised from the test bundle
/// and is therefore kept down to as few branches as possible.
enum BuildNotifier {

    /// How a build ended, from the notifier's point of view.
    ///
    /// `cancelled` is a case rather than a kind of failure because it is the
    /// one outcome that must produce *no* banner: the user stopped the build
    /// themselves, seconds ago, with a confirmation sheet in front of them.
    /// Telling them about it is telling them what they just did.
    enum Outcome: Equatable {
        case succeeded(elapsed: Double?)
        case failed(message: String?)
        case cancelled
    }

    struct Note: Equatable {
        let title: String
        let body: String
    }

    /// What the banner for this build should say, or nil when there shouldn't
    /// be one.
    static func note(
        app: String, outcome: Outcome, delivery: EffectiveDelivery
    ) -> Note? {
        switch outcome {
        case .cancelled:
            return nil

        case let .succeeded(elapsed):
            // The next action, not the fact — a livecontainer push writes a
            // file into Documents/ and nothing has installed anything yet, so
            // "installed" would be false and the user would put the phone down
            // with the tweak not loaded. §3: the import is manual and this
            // tool cannot observe it, which makes the reminder the most useful
            // sentence available.
            let next: String
            switch delivery {
            case .livecontainer:
                next = "Open LiveContainer and import it to finish."
            case .sideload:
                next = "Installed on the device."
            case let .notDelivered(reason):
                next = "\(reason) — nothing was sent to the device."
            case .unknown:
                // Same rule as `BuildProgress.start`'s optional delivery: a
                // surface that cannot represent "unknown" invents an answer.
                next = "Finished."
            }
            return Note(title: "\(app) built \(Self.checkmark)", body: [duration(elapsed), next]
                .compactMap { $0 }
                .joined(separator: " — "))

        case let .failed(message):
            return Note(
                title: "\(app) build failed",
                body: trimmed(message) ?? "No error message was recorded."
            )
        }
    }

    /// Deliver the note, if the user isn't already looking at the app.
    ///
    /// The frontmost check is what keeps this from being noise: with the
    /// window in front of you the log panel, the header state and the delivery
    /// badge have all already said it, and a banner over the top is the app
    /// interrupting you to repeat itself. "You tabbed away" is the entire
    /// condition this feature exists for.
    ///
    /// Authorization is requested here rather than at launch, on purpose. A
    /// permission prompt in the first second of the first run — before the
    /// user has asked for anything, about a thing they have not seen yet — is
    /// the one most likely to be denied, and macOS gives no second chance.
    /// Asked at the end of the first build, the prompt arrives attached to the
    /// event it is about.
    /// - Parameter appIsActive: nil asks `NSApp`, which is what production
    ///   does. Passed explicitly only by a caller that already knows — and by
    ///   nothing else, because there is no `NSApp` to ask from a test.
    /// Set once macOS has said yes, and never unset.
    ///
    /// Not an optimization — it is the fix for banners arriving at
    /// unpredictable times. Every notification used to wait on a
    /// `requestAuthorization` completion, which is an XPC round-trip to
    /// `usernoted` with no latency guarantee, to re-ask a question that was
    /// answered on the first build and can never change while the process
    /// runs. A build that ends while the user is elsewhere is precisely when
    /// this app is idle and least likely to be serviced promptly, so the same
    /// banner could land in milliseconds or minutes with nothing to explain
    /// the difference. The delay was never in the notification; it was in
    /// asking permission again.
    ///
    /// Only `true` is cached. A denial keeps taking the slow path — macOS will
    /// not re-prompt, so it costs nothing — which is what lets a permission
    /// granted in System Settings mid-session start working without a relaunch.
    @MainActor private static var isAuthorized = false

    /// Read the standing permission at launch, so the *first* build of a
    /// session gets the fast path too.
    ///
    /// Without this the cache only helps from the second banner onwards, which
    /// is the wrong half: most sessions are open-the-app, build once, walk
    /// away. `getNotificationSettings` only *reads* the stored decision — it
    /// never prompts — so calling it at launch does not reintroduce the
    /// first-run permission dialog that `post` deliberately defers until a
    /// build the user is watching has finished.
    static func primeAuthorization() {
        UNUserNotificationCenter.current().getNotificationSettings { settings in
            guard settings.authorizationStatus == .authorized else { return }
            Task { @MainActor in isAuthorized = true }
        }
    }

    @MainActor
    static func post(_ note: Note?, appIsActive: Bool? = nil) {
        guard let note else { return }
        let active = appIsActive ?? NSApp?.isActive ?? false

        if isAuthorized {
            guard !active else { return }
            deliver(title: note.title, body: note.body)
            return
        }

        // Authorization is requested even when the app is frontmost, and only
        // *delivery* is suppressed. The guard used to sit above this, which
        // inverted the intent of the comment below: by construction the first
        // build to reach `requestAuthorization` was one the user had tabbed
        // away from, so macOS's single unrepeatable prompt was raised by a
        // backgrounded app and appeared behind whatever they had switched to —
        // and a user who never leaves the window was never asked at all.
        // Asking while they are looking at the build they just started is the
        // moment the prompt makes sense.
        request { granted in
            Task { @MainActor in
                if granted { isAuthorized = true }
                guard granted, !active else { return }
                deliver(title: note.title, body: note.body)
            }
        }
    }

    /// Ask once; macOS answers from its stored decision every time after.
    private static func request(_ then: @escaping @Sendable (Bool) -> Void) {
        UNUserNotificationCenter.current()
            .requestAuthorization(options: [.alert, .sound]) { granted, _ in
                // Deliberately no error surfacing. The user declining
                // notification permission is a preference, not a fault, and a
                // build that succeeded must not grow an error banner because
                // the OS wouldn't let us mention it.
                then(granted)
            }
    }

    /// Hand the banner to the notification center, from whatever thread we are
    /// already on.
    ///
    /// Deliberately *not* wrapped in `Task { @MainActor in … }`, which is how
    /// this was written. `UNUserNotificationCenter` is thread-safe and needs no
    /// such hop, and the hop was the second half of the random-timing problem:
    /// it queued delivery behind whatever the main actor was doing, and the
    /// moment a build ends is exactly when the main actor is draining the last
    /// burst of streamed log events into the transcript view. The busier the
    /// build's tail, the later the banner — which is precisely backwards.
    ///
    /// `trigger: nil` means "as soon as possible", so with nothing in front of
    /// it the delivery is now immediate in the literal sense.
    private static func deliver(title: String, body: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(
                identifier: UUID().uuidString, content: content, trigger: nil
            )
        )
    }

    /// `84.3s` in the CLI's own shape, or nil when it didn't time the build.
    private static func duration(_ elapsed: Double?) -> String? {
        guard let elapsed, elapsed > 0 else { return nil }
        return String(format: "Done in %.1fs", elapsed)
    }

    private static func trimmed(_ message: String?) -> String? {
        guard let message else { return nil }
        let clean = message.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else { return nil }
        // A CLI failure message can carry a multi-line hint. A notification
        // body truncates in the middle of whatever it is given, so cut at a
        // line rather than letting the system cut mid-word.
        return clean.split(separator: "\n").first.map(String.init) ?? clean
    }

    /// Kept out of the format string above so the literal reads as text.
    private static let checkmark = "✓"
}
