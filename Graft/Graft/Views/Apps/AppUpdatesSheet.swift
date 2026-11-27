import SwiftUI

/// The update's one sheet over the window (spec 4.3–4.4): first the
/// confirmation, from the full plan, then the progress, with Stop, each
/// app's result and Try Again. What it says is `AppUpdateText`'s.
struct AppUpdatesSheet: View {
    @ObservedObject var store: AppUpdatesStore
    /// Display names by app name, for the apps a shared tweak also affects.
    let names: [String: String]

    var body: some View {
        VStack(alignment: .leading, spacing: Metrics.sectionGap) {
            if let progress = store.progress {
                UpdateProgressPane(store: store, progress: progress)
            } else if let confirmation = store.confirmation {
                UpdateConfirmPane(store: store, confirmation: confirmation, names: names)
            }
        }
        .padding(Metrics.paneGutter)
        .frame(width: 520)
        .frame(minHeight: 260, alignment: .top)
        .background(Palette.bg)
        .interactiveDismissDisabled(store.isUpdating)
        // What changes on the sheet, said to VoiceOver (review part 2, #8).
        .onChangeWithPrevious(of: store.confirmation) { previous, current in
            if let line = Self.announcement(from: previous, to: current) { VoiceOverAnnouncement.thisMac.post(line) }
        }
        .onChangeWithPrevious(of: store.progress) { previous, current in
            for line in Self.announcements(from: previous, to: current) { VoiceOverAnnouncement.thisMac.post(line) }
        }
    }

    /// What VoiceOver is told when the confirmation's full plan has been
    /// read: what Update would do, that nothing is left, or why it failed.
    static func announcement(from previous: AppUpdatesStore.Confirmation?,
                             to current: AppUpdatesStore.Confirmation?) -> String? {
        guard previous?.isLoading == true, let current, !current.isLoading else { return nil }
        if let failure = current.failure { return failure }
        if current.isNothingLeft { return AppUpdateText.nothingLeft }
        switch current.runnable.count {
        case 0: return "Nothing can update now"
        case 1: return "Ready to update 1 app"
        default: return "Ready to update \(current.runnable.count) apps"
        }
    }

    /// What VoiceOver is told as the progress changes: each app and device
    /// finishing, a Stop waiting for a step, Force Stop offered, and the
    /// end of the run.
    static func announcements(from previous: AppUpdatesStore.Progress?,
                              to current: AppUpdatesStore.Progress?) -> [String] {
        guard let current else { return [] }
        var lines: [String] = []
        let before = Dictionary((previous?.rows ?? []).map { ($0.app, $0) }, uniquingKeysWith: { first, _ in first })
        for row in current.rows {
            let was = before[row.app]
            for target in row.targets where isEnd(target.phase) {
                // One the run reached: a device already waiting when the
                // plan was read isn't news.
                guard let had = was?.targets.first(where: { $0.udid == target.udid })?.phase,
                      !isEnd(had) else { continue }
                lines.append("\(row.title) on \(target.deviceName ?? "the device"): \(AppUpdateText.phaseText(target.phase))")
            }
            if row.result != nil, was?.result == nil {
                lines.append("\(row.title): \(AppUpdateText.phaseText(row.phase))")
            }
        }
        if let stopping = current.stopping, previous?.stopping == nil {
            lines.append(AppUpdateText.stoppingText(stopping))
        }
        if current.offersForceStop, previous?.offersForceStop != true {
            lines.append("Force Stop is available")
        }
        if !current.isRunning, previous?.isRunning == true {
            lines.append("Update Finished")
        }
        return lines
    }

    /// A device's install has ended, one way or another.
    private static func isEnd(_ phase: UpdatePhase) -> Bool {
        switch phase {
        case .done, .failed, .stopped, .unfinished, .skipped: return true
        default: return false
        }
    }

    /// The tallest the sheet's rows get before they scroll: the
    /// confirmation's and the progress's (review part 2, #6).
    static let rowsMaxHeight: CGFloat = 360

    /// What the sheet says once Stop is pressed, while the run is still
    /// ending; nil before, and once it has ended (review part 2, #4).
    static func stopLine(_ progress: AppUpdatesStore.Progress) -> String? {
        progress.isRunning && progress.stopRequested ? AppUpdateText.stoppingText(progress.stopping) : nil
    }

    /// The confirmation's button: Update while the full plan is read (and
    /// disabled until it is), then Update, Update N Apps, or Done when
    /// nothing can run.
    static func confirmTitle(_ confirmation: AppUpdatesStore.Confirmation) -> String {
        confirmation.isLoading ? AppUpdateText.confirmButton(count: 1)
            : AppUpdateText.confirmButton(count: confirmation.count)
    }

    /// The running sheet's Stop: Stop, then Stop Now while the CLI finishes
    /// an install the first Stop waits for (a second Stop cancels it, spec
    /// §11.2), and nothing more to press once that's asked for too.
    static func stopTitle(_ progress: AppUpdatesStore.Progress) -> String? {
        guard progress.isRunning else { return nil }
        if !progress.stopRequested { return "Stop" }
        return progress.stopping != nil ? "Stop Now" : nil
    }
}

private struct UpdateConfirmPane: View {
    @ObservedObject var store: AppUpdatesStore
    let confirmation: AppUpdatesStore.Confirmation
    let names: [String: String]

    var body: some View {
        Text("Update Apps").font(AppFont.title)
        if let note = confirmation.offlineNote {
            Text(note)
                .font(AppFont.body)
                .foregroundStyle(Palette.textMuted)
                .fixedSize(horizontal: false, vertical: true)
        }
        if let failure = confirmation.failure {
            ErrorBanner(message: failure)
        } else if let plan = confirmation.plan {
            if confirmation.isNothingLeft {
                Text(AppUpdateText.nothingLeft).font(AppFont.body).foregroundStyle(Palette.textMuted)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: Metrics.rowGap) {
                        ForEach(AppUpdateText.confirmRows(plan, names: names)) { row in
                            UpdateConfirmRow(store: store, row: row)
                        }
                    }
                }
                .frame(maxHeight: AppUpdatesSheet.rowsMaxHeight)
                let affected = AppUpdateText.affected(plan)
                if !affected.isEmpty {
                    Toggle(
                        AppUpdateText.alsoInstallTitle(affected.map { names[$0] ?? $0 }),
                        isOn: Binding(get: { confirmation.alsoInstall }, set: { store.setAlsoInstall($0) })
                    )
                    .font(AppFont.body)
                }
            }
        } else {
            HStack(spacing: Metrics.controlGap) {
                ProgressView().controlSize(.small)
                Text(AppUpdateText.checking).font(AppFont.body).foregroundStyle(Palette.textMuted)
            }
        }
        if let refusal = confirmation.refusal {
            Text(refusal).font(AppFont.body).foregroundStyle(Palette.warn)
        }
        if let dropped = store.dropFailure {
            Text(dropped.message).font(AppFont.body).foregroundStyle(Palette.warn)
        }
        HStack {
            Spacer()
            Button("Cancel", role: .cancel) { store.cancelConfirmation() }
                .keyboardShortcut(.cancelAction)
            Button(AppUpdatesSheet.confirmTitle(confirmation)) { store.confirm() }
                .keyboardShortcut(.defaultAction)
                // Only while the full plan is read: once it is, Done (nothing
                // can run) closes the sheet, and Update runs what can.
                .disabled(confirmation.isLoading)
        }
    }
}

/// One app of the confirmation: a line per device (spec §11.3), "Example+ →
/// iPhone 15 Pro via LiveContainer" then "→ iPad mini via LiveContainer",
/// each with its import note, its waiting sentence and Stop updating, or
/// its block; then what changes.
private struct UpdateConfirmRow: View {
    @ObservedObject var store: AppUpdatesStore
    let row: AppUpdateText.ConfirmRow

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            if row.targets.isEmpty {
                Text(row.title).font(AppFont.bodyMedium).foregroundStyle(Palette.text)
            }
            ForEach(row.targets) { target in
                Text(target.line).font(AppFont.bodyMedium).foregroundStyle(Palette.text)
                switch target.status {
                case let .installs(importNote):
                    if let importNote {
                        Text(importNote).font(AppFont.monoSmall).foregroundStyle(Palette.textMuted)
                    }
                case let .waiting(sentence, stopTitle):
                    Text(sentence).font(AppFont.monoSmall).foregroundStyle(Palette.warn)
                        .fixedSize(horizontal: false, vertical: true)
                    Button(stopTitle) {
                        Task { await store.dropTarget(app: row.app, udid: target.udid) }
                    }
                    .controlSize(.small)
                    .disabled(store.dropping != nil || store.isUpdating)
                    .help("Graft stops updating it there until you install it there again")
                case let .blocked(reason):
                    Text(reason).font(AppFont.monoSmall).foregroundStyle(Palette.warn)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let note = target.note {
                    Text(note).font(AppFont.monoSmall).foregroundStyle(Palette.textDim)
                }
            }
            ForEach(row.changes, id: \.self) { line in
                Text(line).font(AppFont.monoSmall).foregroundStyle(Palette.text)
            }
            // A tweak update whose source isn't verified (2.3.1 A1): where it
            // comes from, why, and the person's OK, off until they give it.
            ForEach(row.trust) { trust in
                Text(trust.line).font(AppFont.monoSmall).foregroundStyle(Palette.text)
                Text(trust.reason).font(AppFont.monoSmall).foregroundStyle(Palette.warn)
                    .fixedSize(horizontal: false, vertical: true)
                Toggle(AppUpdateText.trustTitle, isOn: Binding(
                    get: { store.confirmation?.trusted.contains(trust.filename) ?? false },
                    set: { store.setTrusted(trust.filename, $0) }
                ))
                .font(AppFont.body)
                .disabled(store.isUpdating)
                .help(AppUpdateText.trustHelp)
                .accessibilityLabel(trust.toggleName)
            }
            ForEach(row.alsoUsed, id: \.self) { line in
                Text(line).font(AppFont.monoSmall).foregroundStyle(Palette.warn)
            }
            ForEach(row.skipped, id: \.self) { line in
                Text(line).font(AppFont.monoSmall).foregroundStyle(Palette.textDim)
            }
            if let blocked = row.blocked {
                Text(blocked).font(AppFont.monoSmall).foregroundStyle(Palette.warn)
            }
            if let note = row.readinessNote {
                Text(note).font(AppFont.monoSmall).foregroundStyle(Palette.textDim)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, Metrics.cardPadding).padding(.vertical, Metrics.innerPadding)
        .cardStyle()
    }
}

private struct UpdateProgressPane: View {
    @ObservedObject var store: AppUpdatesStore
    let progress: AppUpdatesStore.Progress

    var body: some View {
        Text(progress.isRunning ? "Updating Apps" : "Update Finished").font(AppFont.title)
        if let shared = progress.shared {
            Text(shared).font(AppFont.body).foregroundStyle(Palette.textMuted)
        }
        // Scrolled, as the confirmation's rows are, so Update All across many
        // apps and devices keeps Stop, Force Stop and Done on screen (review
        // part 2, #6).
        ScrollView {
            VStack(alignment: .leading, spacing: Metrics.rowGap) {
                ForEach(progress.rows) { row in
                    rowView(row)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxHeight: AppUpdatesSheet.rowsMaxHeight)
        // Its own object, observed only here (review S1).
        UpdateLogView(log: store.log)
        if let failure = progress.failure {
            ErrorBanner(message: failure)
        }
        if let line = AppUpdatesSheet.stopLine(progress) {
            Text(line).font(AppFont.body).foregroundStyle(Palette.warn)
        }
        HStack {
            Spacer()
            if progress.isRunning {
                if progress.offersForceStop {
                    Button("Force Stop") { store.forceStop() }
                        .help("Stop at once, whatever the update is finishing")
                }
                if let title = AppUpdatesSheet.stopTitle(progress) {
                    Button(title) { store.stop() }
                }
            } else {
                if !progress.unfinished.isEmpty {
                    Button("Try Again") { store.tryAgain() }
                }
                Button("Done") { store.closeSheet() }
                    .keyboardShortcut(.defaultAction)
            }
        }
    }

    /// One app's row, with its devices under it.
    private func rowView(_ row: AppUpdatesStore.Row) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack {
                Text(row.title).font(AppFont.bodyMedium).foregroundStyle(Palette.text)
                Spacer()
                Text(AppUpdateText.phaseText(row.phase)).font(AppFont.monoSmall)
                    .foregroundStyle(Self.isTrouble(row.phase) ? Palette.warn : Palette.textMuted)
            }
            if let result = row.result {
                // With its devices under it, the app says only what is its
                // own; without (no plan named them), all of it.
                let line = row.targets.isEmpty
                    ? AppUpdateText.resultLine(result, deviceName: nil, liveContainer: false, blocked: row.blocked)
                    : AppUpdateText.appResultLine(result)
                if let line, !line.isEmpty {
                    Text(line)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(result.ok ? Palette.success : Palette.warn)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            ForEach(row.targets) { target in
                UpdateTargetProgressRow(store: store, app: row, target: target, isRunning: progress.isRunning)
            }
        }
    }

    static func isTrouble(_ phase: UpdatePhase) -> Bool {
        switch phase {
        case .failed, .blocked, .unfinished: return true
        default: return false
        }
    }
}

/// One device's install under its app (spec §11.3): where it is, how it
/// went ("Open LiveContainer on iPad mini to import it"), and its own Try
/// Again once the run has ended.
private struct UpdateTargetProgressRow: View {
    @ObservedObject var store: AppUpdatesStore
    let app: AppUpdatesStore.Row
    let target: AppUpdatesStore.TargetRow
    let isRunning: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline) {
                Text(AppUpdateText.deviceLabel(target.deviceName)).font(AppFont.body).foregroundStyle(Palette.text)
                Spacer()
                Text(AppUpdateText.phaseText(target.phase)).font(AppFont.monoSmall)
                    .foregroundStyle(UpdateProgressPane.isTrouble(target.phase) ? Palette.warn : Palette.textMuted)
                    .multilineTextAlignment(.trailing)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let result = target.result,
               let line = AppUpdateText.targetResultLine(result, deviceName: target.deviceName,
                                                         liveContainer: target.liveContainer) {
                Text(line)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(result.ok ? Palette.success : Palette.warn)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !isRunning, target.canTryAgain {
                Button("Try Again") { store.tryAgain(apps: [app.app]) }
                    .controlSize(.small)
                    .accessibilityLabel(AppUpdateText.tryAgainName(app: app.title, device: target.deviceName))
                    .help("Confirm \(app.title) again: Graft installs only where it's still needed")
            }
        }
        .padding(.leading, Metrics.controlGap)
    }
}

/// The progress sheet's live log: the whole run's transcript, from
/// `UpdateLog`, which only this observes.
private struct UpdateLogView: View {
    @ObservedObject var log: UpdateLog

    var body: some View {
        BuildLogPanel(lines: log.lines, emptyMessage: "Starting…")
            .frame(height: 180)
    }
}

/// The update's sheet over the window, kept out of `ContentView` (review
/// S1): this observes `AppUpdatesStore`, so its changes redraw this and not
/// the window. One sheet id for the confirmation and the progress
/// (`AppUpdatesStore.Sheet.id`), so the one turns into the other in place.
struct AppUpdatesPresenter: View {
    @ObservedOptionalObject var store: AppUpdatesStore?
    /// Each app's name as the sidebar shows it, by config name.
    let names: [String: String]

    var body: some View {
        Color.clear
            .frame(width: 0, height: 0)
            .sheet(item: Binding(
                get: { store?.sheet },
                set: { if $0 == nil { store?.closeSheet() } }
            )) { _ in
                if let store {
                    AppUpdatesSheet(store: store, names: names)
                }
            }
    }
}
