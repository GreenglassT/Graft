import SwiftUI

/// A result picked in Add App's Search, with the store its list came from.
///
/// The store travels with the pick, not read off the search state at submit:
/// the old rows stay clickable while a newer search runs, and a pick made from
/// them belongs to the store they came from, whatever the newer search says.
struct AppStoreSearchPick: Equatable {
    let result: AppStoreSearchResult
    let country: String
}

/// Add App's Search mode: the field, the store, what the search found, and
/// the result picked.
///
/// The sheet owns what is chosen (`term`, `picked`) and sends the add. This
/// view draws the search's state and lets the user pick, with the mouse or
/// with the arrow keys from the field. Every sentence it shows is a static
/// here, so tests read the same words the sheet shows.
struct AppStoreSearchForm: View {
    @ObservedObject var search: AppStoreSearchStore
    @Binding var term: String
    @Binding var picked: AppStoreSearchPick?
    var onSubmit: () -> Void = {}

    // MARK: Copy

    static let placeholder = "Search the App Store"
    static let idle = "Type at least \(AppStoreSearchStore.minimumLength) letters of an app's name."
    static func noResults(term: String, country: String) -> String {
        "No iPhone apps on the \(country) App Store match “\(term)”."
    }
    static let offline = AppStoreSearchOffline.message
    static let rateLimited = AppStoreSearchRateLimited.message
    static func failed(_ message: String) -> String { "The search didn't work: \(message)" }
    static let storeLabel = "Store"
    static let searchingLabel = "Searching"
    static let addedLabel = "Added"
    static let selectedValue = "Selected"
    static let applesOwn = "One of Apple's own apps. iOS ships it, and only Apple can sign it, "
        + "so Graft can't get it from the App Store or build it."
    /// What VoiceOver reads for a row: what the row shows, the "Added" chip
    /// included. Whether it is the picked row is its value and its selected
    /// trait (`selectedValue`).
    static func rowLabel(_ result: AppStoreSearchResult) -> String {
        "\(result.name), \(result.developer), \(price(result))"
            + (result.configuredAs != nil ? ", \(addedLabel)" : "")
    }

    /// The pick after an arrow key: `step` rows on from `picked` (down is
    /// +1), kept inside the list. From no pick, down picks the first row and
    /// up the last. nil only for an empty list.
    static func moved(_ step: Int, from picked: AppStoreSearchResult?,
                      in results: [AppStoreSearchResult]) -> AppStoreSearchResult? {
        guard !results.isEmpty else { return nil }
        guard let picked, let at = results.firstIndex(where: { $0.id == picked.id }) else {
            return step > 0 ? results.first : results.last
        }
        return results[min(max(at + step, 0), results.count - 1)]
    }
    static func storefront(_ country: String) -> String { "From the \(country) App Store" }
    /// A listing with no usable price: never "Free" (the CLI's
    /// `appstore_search.PRICE_UNKNOWN`, pinned by tests/test_appstore_search.py).
    static let priceUnknown = "Price unknown"
    static func price(_ result: AppStoreSearchResult) -> String {
        switch result.acquisition {
        case .free: return result.formattedPrice ?? "Free"
        case let .paid(price): return price
        case .unknown: return priceUnknown
        }
    }
    /// Sub-project 4's hook is `.free` (see `AppStoreSearchResult.Acquisition`).
    static func ownershipNote(_ result: AppStoreSearchResult) -> String? {
        switch result.acquisition {
        case .free:
            return nil
        case let .paid(price):
            return "\(price) on the App Store. Graft can only download apps this Apple ID "
                + "already owns, so buy it in the App Store first."
        case .unknown:
            return "The App Store didn't say what this app costs. Graft can only download "
                + "apps this Apple ID already owns."
        }
    }
    static func alreadyAdded(_ name: String) -> String { "Already added as “\(name)”." }
    static func details(_ result: AppStoreSearchResult) -> String {
        "Version \(result.version) · \(result.bundleId)"
    }

    /// Rows show without scrolling up to this many; more scroll.
    private static let visibleRows = 5
    private static let rowHeight: CGFloat = 46

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: Metrics.controlGap) {
                SearchField(
                    placeholder: Self.placeholder, text: $term, onSubmit: submitFromField,
                    onMove: move
                )
                if search.isSearching {
                    ProgressView().controlSize(.small).accessibilityLabel(Self.searchingLabel)
                }
            }
            storePicker
            stateView
            if let picked { pickedDetails(picked.result) }
        }
    }

    /// The results on screen and the store they came from, or nil.
    private var shown: (results: [AppStoreSearchResult], country: String)? {
        if case let .results(results, _, country) = search.state { return (results, country) }
        return nil
    }

    /// Up and down from the field move the pick through the results.
    private func move(_ step: Int) -> Bool {
        guard let shown, let next = Self.moved(step, from: picked?.result, in: shown.results) else {
            return false
        }
        picked = AppStoreSearchPick(result: next, country: shown.country)
        return true
    }

    /// Return in the field picks the first result when nothing is picked yet,
    /// and adds the pick (the sheet's submit) when something is.
    private func submitFromField() {
        if picked == nil, let shown, let first = shown.results.first {
            picked = AppStoreSearchPick(result: first, country: shown.country)
            return
        }
        onSubmit()
    }

    private var storePicker: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(Self.storeLabel).font(AppFont.bodyMedium).foregroundStyle(Palette.text)
                Picker(Self.storeLabel, selection: Binding(
                    get: { search.country },
                    set: { search.select(country: $0, term: term) }
                )) {
                    ForEach(Storefronts.byName, id: \.self) { code in
                        Text(Storefronts.label(code)).tag(code)
                    }
                }
                .labelsHidden()
                .fixedSize()
            }
            if let note = search.note { line(note, color: Palette.textDim) }
        }
    }

    @ViewBuilder
    private var stateView: some View {
        switch search.state {
        case .idle:
            line(Self.idle, color: Palette.textDim)
        case let .noResults(term, country):
            line(Self.noResults(term: term, country: country), color: Palette.textDim)
        case .offline:
            retryLine(Self.offline, color: Palette.warn)
        case .rateLimited:
            retryLine(Self.rateLimited, color: Palette.warn)
        case let .failed(message):
            retryLine(Self.failed(message), color: Palette.danger)
        case let .results(results, _, country):
            VStack(alignment: .leading, spacing: 4) {
                ScrollView {
                    LazyVStack(spacing: 2) { ForEach(results) { row($0, country: country) } }
                }
                .frame(height: CGFloat(min(results.count, Self.visibleRows)) * Self.rowHeight)
                line(Self.storefront(country), color: Palette.textDim)
            }
        }
    }

    private func line(_ text: String, color: Color) -> some View {
        Text(text)
            .font(AppFont.monoSmall)
            .foregroundStyle(color)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func retryLine(_ text: String, color: Color) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Metrics.controlGap) {
            line(text, color: color)
            Spacer(minLength: Metrics.controlGap)
            Button("Try Again") { search.retry(term: term) }
        }
    }

    private func row(_ result: AppStoreSearchResult, country: String) -> some View {
        let isPicked = picked?.result.id == result.id
        return Button { picked = AppStoreSearchPick(result: result, country: country) } label: {
            HStack(spacing: 10) {
                AppIcon(name: result.name, size: 32,
                        image: result.iconPath.flatMap(ArtworkStore.image(atPath:)))
                VStack(alignment: .leading, spacing: 2) {
                    Text(result.name).font(AppFont.bodyMedium).foregroundStyle(Palette.text).lineLimit(1)
                    Text(result.developer).font(AppFont.monoSmall).foregroundStyle(Palette.textDim).lineLimit(1)
                }
                Spacer(minLength: Metrics.controlGap)
                if result.configuredAs != nil { TagChip(label: Self.addedLabel) }
                Text(Self.price(result))
                    .font(AppFont.monoSmall)
                    .foregroundStyle(result.acquisition == .free ? Palette.textDim : Palette.warn)
            }
            .padding(.horizontal, 8).padding(.vertical, 6)
            .frame(height: Self.rowHeight - 2)
            .background(isPicked ? Palette.surface : Color.clear)
            .clipShape(RoundedRectangle(cornerRadius: Metrics.radiusInner))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        // The explicit label replaces the one SwiftUI would build from the
        // content, so it says everything the row shows, "Added" included.
        .accessibilityLabel(Self.rowLabel(result))
        .accessibilityValue(isPicked ? Self.selectedValue : "")
        .accessibilityAddTraits(isPicked ? .isSelected : [])
    }

    private func pickedDetails(_ result: AppStoreSearchResult) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(Self.details(result))
                .font(AppFont.monoSmall).foregroundStyle(Palette.textDim)
                .lineLimit(1).truncationMode(.middle)
            if let name = result.configuredAs {
                line(Self.alreadyAdded(name), color: Palette.warn)
            }
            if result.isApplesOwn {
                line(Self.applesOwn, color: Palette.warn)
            } else if let note = Self.ownershipNote(result) {
                line(note, color: Palette.warn)
            }
        }
    }
}
