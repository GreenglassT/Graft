import Foundation
import Observation

private typealias Flag = CLIContract.Flag

/// Add App's Search: the App Store, searched as you type.
///
/// The CLI does the searching (the iTunes Search API, with no Apple ID and
/// no credentials) and downloads each result's icon, because this app makes
/// no connections of its own (docs/network.md). This store waits for the
/// typing to pause and runs one search at a time. A newer term cancels the
/// older run, which kills its CLI. It holds what the last search said.
///
/// Apple limits how often one Mac may search (roughly 20 a minute), so a
/// search is only ever sent when it could say something new: a pause of
/// `defaultDebounce`, one run in flight, nothing for a term and store already
/// running or on screen, and a term this sheet already searched in this store
/// answered from `cache`.
///
/// Owned by `AppStoreStore` (`search`), whose runner it shares. The sheet
/// resets it when it opens and when it closes, which also empties the cache:
/// an add changes which results say "Added".
@MainActor
@Observable
final class AppStoreSearchStore {
    /// What the sheet shows under the search field (`AppStoreSearchForm`'s copy).
    ///
    /// `idle` means "nothing to search yet", never "no results". A search
    /// that found nothing is `noResults`, and only the CLI's
    /// `appstore-search-end` sentinel can say so.
    enum State: Equatable {
        case idle
        case results([AppStoreSearchResult], term: String, country: String)
        case noResults(term: String, country: String)
        case offline
        /// Apple answered with its rate limit (`AppStoreSearchRateLimited`).
        case rateLimited
        case failed(String)
    }

    /// A shorter term searches nothing: one letter matches half the store.
    static let minimumLength = 2
    /// How long typing must pause before a search runs. Half a second: each
    /// search is a request against Apple's per-Mac limit.
    /// `nonisolated`: it is `init`'s default argument, which is evaluated
    /// outside the main actor.
    nonisolated static let defaultDebounce: Duration = .milliseconds(500)

    private(set) var state: State = .idle
    /// A search is waiting out the pause or running. The last state stays on
    /// screen meanwhile, so results don't flash away on every keystroke.
    private(set) var isSearching = false
    /// The store searched: the sheet's store picker. It starts at this Mac's
    /// region (`Storefronts.forRegion`), and `reset` puts it back there.
    private(set) var country: String
    /// What the CLI said about how its store was chosen (`note` on the last
    /// sentinel or failure), or nil.
    private(set) var cliNote: String?

    let runner: CLIRunner
    /// The pause in force. Production's is `defaultDebounce`
    /// (`AppStoreSearchStoreTests.test_the_shipped_pause_is_at_least_half_a_second`).
    let debounce: Duration
    private let region: () -> String?
    @ObservationIgnored private var task: Task<Void, Never>?
    /// Bumped by every search started and by `reset`. A run that finishes
    /// under an older one has been replaced or canceled, and writes nothing.
    /// This is the one guard: a killed CLI arrives as an ordinary failure.
    @ObservationIgnored private var generation = 0
    /// The term and store running, or last shown; a repeat of it is no search.
    @ObservationIgnored private var current: Key?
    /// Every answer this sheet has had, by term and store. Only answers:
    /// a failure is asked again.
    @ObservationIgnored private var cache: [Key: State] = [:]

    private struct Key: Hashable {
        let term: String
        let country: String
    }

    /// `debounce` and `region` exist for the tests; production passes neither.
    init(
        runner: CLIRunner,
        debounce: Duration = AppStoreSearchStore.defaultDebounce,
        region: @escaping () -> String? = { Locale.current.region?.identifier }
    ) {
        self.runner = runner
        self.debounce = debounce
        self.region = region
        country = Storefronts.forRegion(region()).code
    }

    /// The store this Mac's region shops in, and what to say about it.
    var regionStore: Storefronts.Store { Storefronts.forRegion(region()) }

    /// The line under the picker about how its store was chosen: the CLI's,
    /// else "your region has no App Store" while the picker is still on the
    /// store that fallback chose.
    var note: String? {
        if let cliNote { return cliNote }
        let home = regionStore
        return country == home.code ? home.note : nil
    }

    static func args(term: String, country: String?) -> [String] {
        [Flag.json, "\(Flag.appstoreSearch)=\(term)"] + (country.map { [Flag.country, $0] } ?? [])
    }

    /// The field changed. Search `raw` once typing pauses, or go back to idle
    /// for a term too short to search. A term whose trimmed text is the one
    /// already running or shown changes nothing: a trailing space is not a
    /// new search.
    func update(term raw: String) {
        let term = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard term.count >= Self.minimumLength else {
            task?.cancel()
            task = nil
            generation += 1
            current = nil
            isSearching = false
            state = .idle
            return
        }
        search(Key(term: term, country: country), force: false)
    }

    /// Search another store: the picker changed. The term on screen is asked
    /// again there, or answered from the cache.
    func select(country code: String, term raw: String) {
        guard let store = Storefronts.store(for: code), store != country else { return }
        country = store
        cliNote = nil
        update(term: raw)
    }

    /// Try Again: the same term and store, asked again whatever is on screen.
    func retry(term raw: String) {
        let term = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard term.count >= Self.minimumLength else { return update(term: raw) }
        search(Key(term: term, country: country), force: true)
    }

    private func search(_ key: Key, force: Bool) {
        if !force, key == current { return }
        task?.cancel()
        generation += 1
        current = key
        if !force, let answer = cache[key] {
            task = nil
            isSearching = false
            state = answer
            return
        }
        isSearching = true
        let mine = generation
        let pause = debounce
        task = Task { [weak self] in
            try? await Task.sleep(for: pause)
            guard !Task.isCancelled else { return }
            await self?.run(key, generation: mine)
        }
    }

    /// Back to the empty state and this Mac's own store, canceling anything
    /// running, with nothing remembered.
    func reset() {
        task?.cancel()
        task = nil
        generation += 1
        current = nil
        cache = [:]
        cliNote = nil
        country = regionStore.code
        isSearching = false
        state = .idle
    }

    private func run(_ key: Key, generation mine: Int) async {
        let outcome: State
        var note: String?
        do {
            let events = try await runner.streamEvents(
                Self.args(term: key.term, country: key.country),
                failing: "App Store search",
                refusal: .appStoreSearch
            )
            outcome = Self.state(from: events, term: key.term)
            note = Self.note(in: events)
        } catch is AppStoreSearchOffline {
            outcome = .offline
        } catch is AppStoreSearchRateLimited {
            outcome = .rateLimited
        } catch {
            outcome = .failed(error.localizedDescription)
        }
        guard mine == generation else { return }
        state = outcome
        cliNote = note
        isSearching = false
        switch outcome {
        case .results, .noResults: cache[key] = outcome
        default: current = nil  // a failure is no answer: the same term may ask again
        }
    }

    /// What a finished search's events say. Results without the sentinel are
    /// a truncated stream, not an answer.
    static func state(from events: [BuildEvent], term: String) -> State {
        var results: [AppStoreSearchResult] = []
        var end: AppStoreSearchEndRecord?
        for event in events {
            switch event {
            case let .appStoreSearchResult(result): results.append(result)
            case let .appStoreSearchEnd(record): end = record
            default: break
            }
        }
        guard let end else { return .failed("graft ended the search without reporting its results") }
        return results.isEmpty
            ? .noResults(term: term, country: end.country)
            : .results(results, term: term, country: end.country)
    }

    /// The sentinel's `note`, when it has one.
    static func note(in events: [BuildEvent]) -> String? {
        for case let .appStoreSearchEnd(record) in events { return record.note }
        return nil
    }
}
