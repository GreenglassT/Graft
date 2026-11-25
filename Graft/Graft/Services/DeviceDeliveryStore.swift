import Foundation
import Combine

private typealias Flag = CLIContract.Flag

/// Every configured app's delivery state **on one phone**, keyed by udid.
///
/// ## Why this is not a second field on `ConfigStore`
///
/// `ConfigStore.deliveryStates` answers a different question, and the
/// difference is the whole point of this type. That map is built from `--list`,
/// which calls `_delivery_state_for` with no udid — so it means "was this app
/// delivered *anywhere*". Two live views read it with that meaning:
/// `SidebarView` (the warning dot on every app row) and `AppDetailView` (the
/// amber card and its "Check device" copy). Neither names a phone, and neither
/// should start to: the sidebar is the app list, not a place.
///
/// Making that field device-aware in place would therefore have changed what
/// two existing surfaces claim, silently and without a failing test — the rows
/// would keep rendering, they would simply start being about one phone. So the
/// per-device answer gets its own store rather than a second dictionary next to
/// the first, where the only thing separating the two meanings would be a
/// property name.
///
/// Keyed by udid for the reason `DeviceInfoStore` is: switching between two
/// phones' panes should not re-run a subprocess for an answer taken seconds
/// ago, and the two phones' answers genuinely differ.
///
/// ## The answer that looks like a bug
///
/// An app built and pushed to phone A, never to phone B, reads **"Never
/// delivered to a device."** on B's pane while the sidebar shows it delivered
/// and current. Both are true. The sidebar means "anywhere" and this means
/// "here", and surfacing that gap is the entire reason the per-device pane
/// exists — it is what the single global state was hiding. Do not "fix" this by
/// falling back to the unscoped state when the scoped one says `never`: that
/// restores the lie in the one place built to expose it.
@MainActor
final class DeviceDeliveryStore: ObservableObject {

    /// What this store knows about one phone.
    ///
    /// Three cases rather than a dictionary that may be empty, because absence
    /// is ambiguous here in a way it is not on `ConfigStore`: before the first
    /// load, *every* app is missing from the map, and a view that read missing
    /// as "the CLI said nothing about it" would spend the first few hundred
    /// milliseconds telling the user things it had not yet asked. The states an
    /// app can be in — not asked yet, asked and answered, asked and failed —
    /// are what the section's status line switches on.
    enum Readout: Equatable {
        /// No load has completed for this phone. The pane's own `.task` starts
        /// one, so this is a moment, not a resting state.
        case pending
        /// `--delivery-state --device <udid>` answered. The map is keyed by the
        /// app's config name (the event's `app` field), and an app absent from
        /// a `.ready` map is one the CLI declined to describe — an older CLI, a
        /// state it couldn't compute — which is the same meaning absence has on
        /// `ConfigStore.deliveryStates`.
        case ready([String: DeliveryStateRecord])
        /// The run failed and there was nothing better to keep. Carries the
        /// CLI's own message, which is the only thing that says *why*.
        case failed(String)
    }

    /// One readout per phone. A udid with no entry has never been loaded; read
    /// it through `readout(for:)`, which says so as `.pending`.
    @Published private(set) var readouts: [String: Readout] = [:]

    private let runner: CLIRunner

    init(runner: CLIRunner) {
        self.runner = runner
    }

    /// What this store knows about `udid` — `.pending` when it has never been
    /// asked, which is what a caller should render before the first load lands.
    func readout(for udid: String) -> Readout { readouts[udid] ?? .pending }

    /// The argv for one phone's delivery states.
    ///
    /// Pulled out of `load` so a test can assert what actually reaches the CLI,
    /// the same reason `DeviceAppsStore.decryptArgs` is a static function — and
    /// for the same class of defect, which on this verb is the worst one
    /// available. `--delivery-state` without `--device` is a *successful* run
    /// that answers the unscoped question: every app's state anywhere, rendered
    /// under one phone's name. Nothing fails, nothing logs, and the pane built
    /// to expose the per-device gap quietly stops doing so.
    static func stateArgs(udid: String) -> [String] {
        [Flag.json, Flag.deliveryState, Flag.device, udid]
    }

    /// Read one phone's delivery states.
    ///
    /// A failure is recorded rather than swallowed — unlike `DeviceInfoStore`
    /// and `ArtworkStore`, whose failures degrade to a dash and a letter tile
    /// respectively. Here the fallback would be a *claim*: rows with no record
    /// would have to say something about delivery, and the honest options are
    /// "we haven't asked" and "we couldn't". Both need this to survive to the
    /// view.
    ///
    /// Except when there is something better to keep. A refresh that fails over
    /// states this store already holds leaves them standing, exactly as
    /// `ConfigStore.refreshDeliveryStates` does and for its reason: stale is
    /// closer to the truth than absent, and blanking a section the user is
    /// looking at is a worse answer than the one it showed a second ago.
    func load(udid: String, argsOverride: [String]? = nil) async {
        do {
            let events = try await runner.runEvents(
                argsOverride ?? Self.stateArgs(udid: udid), failing: "Read delivery state"
            )
            // `uniquingKeysWith`, not `uniqueKeysWithValues`: the latter traps
            // on a repeated key and this is built from a subprocess's stdout —
            // the one input this process doesn't control. Same call
            // `ConfigStore.loadApps` makes, for the same reason: a duplicated
            // app should cost a row's status line, not the GUI.
            readouts[udid] = .ready(Dictionary(
                events.compactMap { event -> (String, DeliveryStateRecord)? in
                    if case let .deliveryState(record) = event { return (record.app, record) }
                    return nil
                },
                uniquingKeysWith: { _, latest in latest }
            ))
        } catch {
            // Cancellation is not a failure worth reporting: `runOnce` reports
            // the SIGTERM'd child as a non-zero exit, and this load runs from a
            // `.task(id:)` that is canceled the moment the user selects
            // another phone. Recording `.failed` there would leave "couldn't
            // read" waiting on a pane nobody had a problem with.
            guard !Task.isCancelled else { return }
            // States already read for this phone outrank the failure to read
            // them again — see the doc comment above.
            if case .ready = readout(for: udid) { return }
            readouts[udid] = .failed(error.localizedDescription)
        }
    }
}
