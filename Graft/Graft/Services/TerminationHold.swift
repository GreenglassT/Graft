import Foundation

/// What `TerminationHold` asks of the process: `ProcessInfo`'s own four
/// calls, so a test can count them instead.
protocol TerminationControl: AnyObject {
    func disableSuddenTermination()
    func enableSuddenTermination()
    func disableAutomaticTermination(_ reason: String)
    func enableAutomaticTermination(_ reason: String)
}

extension ProcessInfo: TerminationControl {}

/// Keeps macOS from ending Graft on its own, without asking, while work it
/// can't take back is running (whole-diff review, app-3). It doesn't keep
/// Graft from quitting when a quit is asked for: see the last paragraph.
///
/// Info.plist says Graft supports sudden and automatic termination, which is
/// true while it is idle: macOS may then kill it at logout, or quit it once
/// its window is closed, without asking. In the middle of a build or a
/// re-install, an App Store get, drop or Get (free), an `--init` setup, an
/// enrollment, a LiveContainer install or a decrypt dump, that killed the app
/// and orphaned the CLI, which then aborted on a broken pipe halfway through
/// signing, staging or installing. Each of those now holds both off from
/// start to end, so macOS neither kills Graft outright nor quits it by
/// itself while one runs.
///
/// Every `begin` is matched by one `end` with the same reason, on every path
/// (a `defer`, or straight after a `do`/`catch` that catches everything):
/// `ProcessInfo` counts them, and one left over would keep the app from ever
/// being terminated that way again.
///
/// **What it doesn't stop** (the try14 delta audit, F4). A logout, a restart
/// or a shutdown still quits Graft in the middle of that work. With the hold
/// on, macOS sends Graft an ordinary quit in place of the kill, but Graft has
/// no `applicationShouldTerminate` to cancel or put off that quit, so it
/// exits at once, as it does on ⌘Q, and the CLI loses its pipes just as
/// before. Answering that quit is left for after 2.0.
final class TerminationHold: @unchecked Sendable {
    /// The app's one hold, on the real `ProcessInfo`. Reaches nothing past
    /// this process, so stores default to it; a test passes its own.
    static let shared = TerminationHold(control: ProcessInfo.processInfo)

    static let building = "Graft is building and installing an app"
    static let gettingFromAppStore = "Graft is getting an app from the App Store"
    static let staging = "Graft is staging an IPA"
    static let claiming = "Graft is getting a free app for this Apple ID"
    static let settingUp = "Graft is setting up the signing account"
    static let enrolling = "Graft is setting up App Store decryption"
    static let liveContainer = "Graft is changing LiveContainer on a device"
    static let reinstalling = "Graft is re-installing a build on a device"
    static let decrypting = "Graft is decrypting an app from a device"

    private let control: TerminationControl
    private let lock = NSLock()

    init(control: TerminationControl) {
        self.control = control
    }

    /// Sudden and automatic termination off, for `reason`, until `end(reason)`.
    func begin(_ reason: String) {
        lock.withLock {
            control.disableSuddenTermination()
            control.disableAutomaticTermination(reason)
        }
    }

    /// Undo one `begin(reason)`.
    func end(_ reason: String) {
        lock.withLock {
            control.enableAutomaticTermination(reason)
            control.enableSuddenTermination()
        }
    }
}
