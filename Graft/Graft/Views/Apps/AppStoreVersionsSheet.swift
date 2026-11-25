import SwiftUI

/// Get an older version…: the versions the App Store still serves this Apple
/// ID for one app, newest first, each with its version, its date and Get.
///
/// The list arrives a row at a time as the CLI reads each version
/// (`AppStoreVersionsStore`), a page at once; Show More reads the next page.
/// Get closes the sheet and starts the get on the pane's App Store row, with
/// the same progress and Stop as Get latest. Getting a version holds the app
/// at it, which the sheet says before anything is picked, with the other
/// limits (`limits`).
struct AppStoreVersionsSheet: View {
    /// The config app's name, which `--app` takes.
    let app: String
    /// The version Graft knows the app is held at, if any, until the list says.
    var held: String? = nil
    /// What the sheet calls the app.
    let displayName: String
    @ObservedObject var store: AppStoreVersionsStore
    /// Why a get can't start now: a sign-in in Settings, or a get or drop
    /// already running for this app. Get is disabled with it as the help.
    let getBlockedReason: String?
    /// A version picked. The pane starts its get; the sheet closes.
    let onGet: (AppStoreReleaseRecord) -> Void
    @Environment(\.dismiss) private var dismiss
    /// Where App Store requests can't run (`MacSupport.unavailableLine`), the
    /// sheet asks for nothing and says why. Get an older version… is disabled
    /// there already; this is the second wall, as `list_versions` runs
    /// ipatool's emulator (`appstore.store_requests_blocker`).
    @Environment(\.macSupport) private var mac

    static let title = "Older Versions"

    /// The three limits, plainly, and what a get does to the app.
    static let limits = "Only apps this Apple ID has. Apple doesn't serve every old version, "
        + "and some apps refuse to run once they're too old. "
        + "Getting one holds the app at that version: its builds use it until you press "
        + "Get latest from App Store."

    static let loadingLine = "Asking the App Store for this app's versions…"

    /// Whether the sheet asks the App Store for the list: only where App
    /// Store requests can run (`unavailable` is nil).
    static func loads(unavailable: String?) -> Bool {
        unavailable == nil
    }
    static let emptyLine = "The App Store lists no versions of this app for this Apple ID."

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider().background(Palette.line)
            content
            if case let .failed(message) = store.phase {
                ErrorBanner(message: message, fit: .fullBleed(gutter: 20))
            }
            footer
        }
        .frame(width: 520, height: 540)
        .background(Palette.bg)
        .task {
            guard Self.loads(unavailable: mac.unavailableLine) else { return }
            store.load(app: app, held: held)
        }
        .onDisappear { store.reset() }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(Self.title).font(AppFont.title).foregroundStyle(Palette.text)
            Text("Versions of \(displayName) the App Store still has, newest first.")
                .font(AppFont.monoSmall).foregroundStyle(Palette.textDim)
            Text(Self.limits)
                .font(AppFont.monoSmall).foregroundStyle(Palette.textMuted)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 20).padding(.top, 20).padding(.bottom, 14)
    }

    @ViewBuilder
    private var content: some View {
        if let line = mac.unavailableLine {
            VStack {
                Spacer()
                Text(line).font(AppFont.body).foregroundStyle(Palette.textDim)
                Spacer()
            }
            .frame(maxWidth: .infinity)
        } else if store.releases.isEmpty {
            VStack(spacing: 10) {
                Spacer()
                switch store.phase {
                case .loading, .idle:
                    ProgressView().controlSize(.small)
                    Text(Self.loadingLine)
                case .loaded:
                    Text(Self.emptyLine)
                case .failed:
                    EmptyView()
                }
                Spacer()
            }
            .font(AppFont.body)
            .foregroundStyle(Palette.textDim)
            .frame(maxWidth: .infinity)
        } else {
            let builds = store.duplicateBuilds
            List(store.releases) { release in
                row(release, build: builds[release.externalId])
            }
            .listStyle(.inset)
            .scrollContentBackground(.hidden)
        }
    }

    /// `build`: which of a version's builds this row is, when the App Store
    /// lists more than one under its number (`AppStoreVersionsStore.duplicateBuilds`).
    private func row(
        _ release: AppStoreReleaseRecord, build: AppStoreVersionsStore.DuplicateBuild?
    ) -> some View {
        let state = AppStoreVersionsStore.rowState(release, phase: store.phase, readingUpTo: store.readingUpTo)
        return HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(Self.headline(release, state: state, build: build))
                    .font(AppFont.bodyMedium)
                    .foregroundStyle(state == .ready ? Palette.text : Palette.textDim)
                    .help(ifAny: build?.help)
                Text(Self.detailLine(release, held: store.heldVersion, state: state))
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
                    .lineLimit(2)
                    .truncationMode(.tail)
            }
            Spacer()
            switch Self.accessory(release, in: store) {
            case .reading:
                ProgressView().controlSize(.small)
            case .get:
                Button("Get") {
                    onGet(release)
                    dismiss()
                }
                .disabled(getBlockedReason != nil)
                .help(getBlockedReason ?? Self.getHelp(release, held: store.heldVersion))
            case .nothing:
                EmptyView()
            }
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }

    private var footer: some View {
        HStack(spacing: Metrics.controlGap) {
            if store.phase == .loading, !store.releases.isEmpty {
                ProgressView().controlSize(.small)
                Text("Reading versions…").font(AppFont.monoSmall).foregroundStyle(Palette.textDim)
            }
            if case .failed = store.phase {
                Button("Try Again") { store.retry() }
            }
            if store.phase == .loaded, store.nextOffset != nil {
                Button("Show More") { store.loadMore() }
                    .help("Read the next versions' numbers and dates")
            }
            Spacer()
            Button("Close") { dismiss() }
                .keyboardShortcut(.cancelAction)
        }
        .padding(20)
    }

    // MARK: What each row says

    /// The version, or what stands in for it before it is read. One of two
    /// or more builds under one number says which (`build`): "2026.42.0
    /// (build 1 of 2)".
    static func headline(
        _ release: AppStoreReleaseRecord, state: AppStoreVersionsStore.RowState,
        build: AppStoreVersionsStore.DuplicateBuild? = nil
    ) -> String {
        if let version = release.version { return build.map { "\(version) \($0.suffix)" } ?? version }
        switch state {
        case .reading: return "Reading…"
        case .unavailable: return "Version unknown"
        case .unread, .ready: return "Not read yet"
        }
    }

    /// The date and what is true of this version, then its id.
    static func detailLine(
        _ release: AppStoreReleaseRecord, held: String?, state: AppStoreVersionsStore.RowState
    ) -> String {
        var parts: [String] = []
        if let date = releaseDate(release.releaseDate) { parts.append(date) }
        if release.newest { parts.append("newest") }
        if let version = release.version, version == held { parts.append("held: builds use this one") }
        if release.staged { parts.append("staged") }
        switch state {
        case let .unavailable(why): parts.append(why)
        case .unread: parts.append("Show More reads it")
        case .reading, .ready: break
        }
        parts.append("id \(release.externalId)")
        return parts.joined(separator: " · ")
    }

    /// What a row shows at its end, as `row` draws it.
    enum Accessory: Equatable {
        /// Its version is being read.
        case reading
        /// Get (`offersGet`).
        case get
        case nothing
    }

    /// What `row` shows at the end of `release`'s row, from `store` as the
    /// sheet reads it: the row's state and the held version the list said,
    /// or the one the sheet opened with until it says.
    @MainActor
    static func accessory(_ release: AppStoreReleaseRecord, in store: AppStoreVersionsStore) -> Accessory {
        let state = AppStoreVersionsStore.rowState(release, phase: store.phase, readingUpTo: store.readingUpTo)
        if state == .reading { return .reading }
        return offersGet(release, held: store.heldVersion, state: state) ? .get : .nothing
    }

    /// Whether a row offers Get: once its version is read, unless the app is
    /// held at it and its IPA is staged, when there is nothing to get. The
    /// held version's IPA can be gone (deleted in the Cache pane), and this
    /// is the one way to get it back without letting go of the hold: Get
    /// latest would.
    static func offersGet(
        _ release: AppStoreReleaseRecord, held: String?, state: AppStoreVersionsStore.RowState
    ) -> Bool {
        guard state == .ready else { return false }
        return release.version != held || !release.staged
    }

    static func getHelp(_ release: AppStoreReleaseRecord, held: String? = nil) -> String {
        let version = release.version ?? "this version"
        if release.staged {
            return "\(version) is already staged: hold the app at it, without downloading it again"
        }
        if let held, release.version == held {
            return "The app is held at \(version), and it isn't staged any more: download, decrypt and "
                + "stage it again"
        }
        return "Download, decrypt and stage \(version), and hold the app at it"
    }

    /// ipatool's RFC 3339 date as a day ("Aug 1, 2026"), in UTC so a
    /// midnight release doesn't read as the day before. nil when there is
    /// none, or it won't parse.
    static func releaseDate(_ raw: String?) -> String? {
        guard let raw else { return nil }
        let parser = ISO8601DateFormatter()
        guard let date = parser.date(from: raw) else { return nil }
        let shown = DateFormatter()
        shown.locale = Locale(identifier: "en_US_POSIX")
        shown.timeZone = TimeZone(identifier: "UTC")
        shown.dateFormat = "MMM d, yyyy"
        return shown.string(from: date)
    }
}

private extension View {
    /// `help(_:)` when there is something to say, and nothing otherwise:
    /// a row of a version of its own keeps the help it had.
    @ViewBuilder
    func help(ifAny text: String?) -> some View {
        if let text { help(text) } else { self }
    }
}
