import Foundation
import Combine

private typealias Flag = CLIContract.Flag

/// Help › Remove Graft's Data…: what Graft keeps outside Graft.app, and
/// removing what the user chooses.
///
/// The CLI lists and removes (`--remove-data`, `graft_ipa/remove_data.py`).
/// It knows where everything is, and the same command works in Terminal.
/// The app adds three things:
/// - It refuses while Graft is working (`TerminationHold`): a build or a
///   download whose folders went from under it would fail halfway, and its
///   cleanup could put some of them back.
/// - It holds the quit while the CLI removes (`TerminationHold.removingData`),
///   with no Stop: a removal stopped halfway is worse than one that finishes.
/// - It removes its own preferences (`removeAppSettings`), which the CLI only
///   lists: they are this process's, and it would write them again anyway.
@MainActor
final class RemoveDataStore: ObservableObject {
    enum Phase: Equatable {
        case loading
        case listed
        case removing
        case done(Summary)
        /// The listing failed, with the sentence to show.
        case failed(String)
    }

    /// What a removal did, for the sheet's last screen.
    struct Summary: Equatable {
        struct Removed: Equatable {
            let title: String
            let freedBytes: Int
        }

        struct Failed: Equatable {
            let title: String
            let reasons: [String]
        }

        var removed: [Removed] = []
        var failed: [Failed] = []
        var freedBytes = 0
        /// One of Graft's own folders went. While the app stays open, the
        /// CLI commands behind its background checks make those folders
        /// again, empty (`data_dirs.secure_app_dirs`), so the sheet says to
        /// quit.
        var removedFolders = false
    }

    /// The items whose removal takes Graft's own folders.
    static let folderIDs: Set<String> = ["config", "cache", "state", "data"]

    let runner: CLIRunner
    let hold: TerminationHold
    /// Removes Graft.app's own preferences. `GraftApp` passes the real
    /// domain's removal, a test a closure that counts.
    private let removeAppSettings: @MainActor () -> Void
    /// Quits Graft. `GraftApp` passes `NSApp.terminate`.
    private let quit: @MainActor () -> Void

    @Published var isPresented = false
    @Published private(set) var phase: Phase = .loading
    /// Every item the CLI listed, in its order.
    @Published private(set) var items: [DataItemRecord] = []
    /// `data-items-end`'s `keychain`: whether the keychain items were checked.
    @Published private(set) var keychainStatus: String?
    /// The ids checked.
    @Published var chosen: Set<String> = []
    /// What Graft is doing, as of the last `refreshWork`
    /// (`TerminationHold.reasons`). Anything here stops a removal.
    @Published private(set) var working: [String] = []
    /// Unpublished: bookkeeping, which nothing draws.
    private var appSettingsRemoved = false

    init(
        runner: CLIRunner, hold: TerminationHold,
        removeAppSettings: @escaping @MainActor () -> Void,
        quit: @escaping @MainActor () -> Void
    ) {
        self.runner = runner
        self.hold = hold
        self.removeAppSettings = removeAppSettings
        self.quit = quit
    }

    // MARK: Opening

    /// Help › Remove Graft's Data…: the sheet, and a fresh listing.
    func open() {
        refreshWork()
        // The menu still works under the sheet: chosen again mid-removal,
        // it shows the sheet and starts nothing.
        guard phase != .removing else {
            isPresented = true
            return
        }
        // Before the sheet shows: a second opening never flashes the last
        // removal's result.
        phase = .loading
        isPresented = true
        Task { await load() }
    }

    func close() {
        isPresented = false
    }

    var listArguments: [String] { [Flag.json, Flag.removeData] }

    static func removeArguments(_ ids: [String]) -> [String] {
        [Flag.json, Flag.removeData, Flag.items, ids.joined(separator: ",")]
    }

    func load() async {
        phase = .loading
        items = []
        chosen = []
        keychainStatus = nil
        do {
            let result = try await runner.runOnce(arguments: listArguments)
            var listed: [DataItemRecord] = []
            var end: DataItemsEndRecord?
            for event in result.stdout.buildEvents() {
                switch event {
                case let .dataItem(record): listed.append(record)
                case let .dataItemsEnd(record): end = record
                default: break
                }
            }
            guard let end, result.exitCode == 0 else {
                phase = .failed(result.exitCode != 0 ? result.failureSummary : Self.noAnswer)
                return
            }
            items = listed
            keychainStatus = end.keychain
            chosen = Set(listed.filter { $0.present == true && $0.isDefault && $0.canRemoveHere }.map(\.id))
            phase = .listed
        } catch {
            phase = .failed(error.localizedDescription)
        }
    }

    static let noAnswer = "The command-line tool gave no answer."

    // MARK: What the sheet shows

    /// What is there, or couldn't be checked: an item with nothing there
    /// isn't shown.
    var shown: [DataItemRecord] { items.filter { $0.present != false } }

    /// `shown`, under each heading, in the order the CLI listed them.
    var groups: [(title: String, items: [DataItemRecord])] {
        var order: [String] = []
        var byGroup: [String: [DataItemRecord]] = [:]
        var titles: [String: String] = [:]
        for item in shown {
            if byGroup[item.group] == nil { order.append(item.group) }
            byGroup[item.group, default: []].append(item)
            titles[item.group] = item.groupTitle
        }
        return order.map { (titles[$0] ?? $0, byGroup[$0] ?? []) }
    }

    /// Whether `item` has a checkbox that can be checked: it is there, and
    /// the CLI or the app removes it.
    static func isChoosable(_ item: DataItemRecord) -> Bool {
        item.present == true && item.canRemoveHere
    }

    /// The checked items' total size.
    var chosenBytes: Int {
        items.filter { chosen.contains($0.id) }.reduce(0) { $0 + ($1.sizeBytes ?? 0) }
    }

    var canRemove: Bool {
        phase == .listed && working.isEmpty && !chosen.isEmpty
    }

    /// Why Remove is off while Graft works, in the quit question's words.
    static func refusal(_ reasons: [String]) -> String? {
        guard !reasons.isEmpty else { return nil }
        var seen = Set<String>()
        let doing = reasons.compactMap { TerminationHold.activities[$0] }.filter { seen.insert($0).inserted }
        let what = doing.isEmpty ? QuitQuestion.working : QuitQuestion.list(doing)
        return "Graft is \(what). Wait for it to finish, then remove its data."
    }

    /// The keychain line, when its items couldn't be checked.
    var keychainNote: String? {
        guard let keychainStatus, keychainStatus != DataItemsEndRecord.keychainChecked else { return nil }
        return "Your login keychain is locked, or didn't answer, so Graft couldn't check it for "
            + "the App Store sign-in or your passwords. Unlock it in Keychain Access, then open "
            + "this again."
    }

    /// Read what Graft is doing now. The sheet calls this every second, and
    /// `remove` once more before it starts.
    func refreshWork() {
        let now = hold.reasons
        if now != working { working = now }
    }

    // MARK: Removing

    func remove() async {
        refreshWork()
        guard canRemove else { return }
        let picked = items.filter { chosen.contains($0.id) && Self.isChoosable($0) }
        guard !picked.isEmpty else { return }
        phase = .removing
        var summary = Summary()
        let titles = Dictionary(items.map { ($0.id, $0.title) }, uniquingKeysWith: { first, _ in first })
        let forCLI = picked.filter(\.removable).map(\.id)
        if !forCLI.isEmpty {
            // No Stop: Quit Anyway waits for it to finish, and the quit
            // question says Graft quits when it does (`QuitGuard`).
            let ticket = hold.begin(TerminationHold.removingData, stop: nil)
            defer { hold.end(ticket) }
            do {
                let result = try await runner.runOnce(arguments: Self.removeArguments(forCLI))
                var reported = Set<String>()
                for event in result.stdout.buildEvents() {
                    guard case let .dataRemoved(record) = event else { continue }
                    reported.insert(record.id)
                    let title = titles[record.id] ?? record.id
                    if record.ok {
                        summary.removed.append(.init(title: title, freedBytes: record.freedBytes))
                        if Self.folderIDs.contains(record.id) { summary.removedFolders = true }
                    } else {
                        summary.failed.append(.init(
                            title: title, reasons: record.failed.map { "\($0.what): \($0.reason)" }
                        ))
                    }
                    summary.freedBytes += record.freedBytes
                }
                let unreported = forCLI.filter { !reported.contains($0) }
                if !unreported.isEmpty {
                    let why = result.exitCode != 0 ? result.failureSummary : Self.noAnswer
                    summary.failed += unreported.map { .init(title: titles[$0] ?? $0, reasons: [why]) }
                }
            } catch {
                summary.failed += forCLI.map {
                    .init(title: titles[$0] ?? $0, reasons: [error.localizedDescription])
                }
            }
        }
        if let settings = picked.first(where: \.isAppSettings) {
            removeAppSettings()
            appSettingsRemoved = true
            summary.removed.append(.init(title: settings.title, freedBytes: settings.sizeBytes ?? 0))
            summary.freedBytes += settings.sizeBytes ?? 0
        }
        phase = .done(summary)
    }

    /// The last screen's Quit Graft. Its own settings once more first: the
    /// app may have saved some since (the update check's time, a window's
    /// size).
    func quitGraft() {
        if appSettingsRemoved { removeAppSettings() }
        quit()
    }
}
