import AppKit
import SwiftUI

/// LiveContainer on **this** device: what is installed, who manages it, and
/// the way into installing it properly.
///
/// "Properly" is the point of the section. LiveContainer used to be built
/// like any other app, through zsign, and zsign writes a wildcard profile's
/// keychain *pattern* into the signature as a literal — which switches off
/// LiveContainer's keychain redirection, so no guest app can keep a login.
/// The CLI's `--lc-install` signs it the way AltStore does.
///
/// **The setup is one sheet** (`SetUpLiveContainerSheet`, spec §5c), which
/// **Set Up LiveContainer…** opens: the install, the certificate and the
/// steps LiveContainer needs afterward. Install / Update, Put certificate on
/// device and Remove certificate moved into it. What stays here is what the
/// section reports, Clean up, and a reminder while a certificate Graft put on
/// the device is still there.
///
/// Like `DeviceDecryptSection`, it belongs to one device: `device.udid` is
/// passed to every call and nothing here can name another. And like that
/// card, it re-derives nothing — who manages the device, whether an update is
/// available, whether a certificate is still there — those are the CLI's
/// answers (`--lc-status`).
///
/// An AltStore-managed device gets a read-only notice. Replacing AltStore's
/// copies would orphan every guest app's JIT-less signature, and AltStore
/// would refresh its own copy back over ours; the CLI refuses for the same
/// reason, so a paid account is offered no Set Up there. A free Apple ID's
/// sheet is the steps a SideStore phone takes anyway, so it is.
///
/// `--lc-status` and `--lc-clean` work on either account, so the listing,
/// Re-check and Clean up are the same on both, and Clean up is there for
/// every LiveContainer the phone has, whoever installed it
/// (`blocks(for:account:)`).
struct DeviceLiveContainerSection: View {
    let device: DeviceRecord
    @ObservedObject var store: LiveContainerStore
    /// `ConfigStore.account`. Nil (not known yet) disables nothing.
    let account: AccountMode?
    /// Open Set Up LiveContainer for this device. `ContentView` presents the
    /// sheet, through `DeviceDetailView.onSetUpLiveContainer`, which requires
    /// it; this default is for the tests that host the section alone.
    var onSetUp: () -> Void = {}

    static let setUpLabel = "Set Up LiveContainer…"
    static let removeItLabel = "Remove it"

    /// The reminder's words, before its Remove it button (spec §5c).
    nonisolated static func reminder(name: String) -> String {
        "The certificate is still on \(name):"
    }

    private var status: LCStatusRecord? { store.status[device.udid] }
    private var run: LiveContainerStore.Run? { store.run(for: device.udid) }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            content
            if let run {
                LiveContainerRunReport(
                    run: run, instances: status?.instances ?? [], onStop: { store.stop() }
                )
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(Metrics.cardPadding)
        .cardStyle()
        .task(id: device.udid) {
            store.remember(device)
            await store.load(udid: device.udid)
        }
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                Text("LiveContainer")
                    .font(AppFont.bodyMedium)
                    .foregroundStyle(Palette.text)
                Text(subtitle)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.textDim)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
            Button {
                Task { await store.load(udid: device.udid) }
            } label: {
                if store.loading.contains(device.udid) {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.small)
                        Text("Checking…")
                    }
                } else {
                    Symbols.refresh.label("Re-check")
                }
            }
            .controlSize(.small)
            .disabled(store.loading.contains(device.udid) || store.isBusy)
        }
    }

    private var subtitle: String {
        guard let latest = status?.latest else {
            return "Runs apps without re-signing them, so their logins keep working."
        }
        return "Latest: \(latest.version) (\(latest.channel))"
            + ((status?.updateAvailable ?? false) ? " — update available" : "")
    }

    @ViewBuilder
    private var content: some View {
        if let elsewhere = store.busyElsewhere(than: device.udid) {
            dim("Busy with LiveContainer on \(store.name(of: elsewhere)) — this device's actions "
                + "wait for it.")
        }
        // Shown whenever the last check failed, stale status or not: the
        // reading below may be from before the device went away, and saying
        // nothing about a failed Re-check reads as a successful one.
        if let error = store.loadErrors[device.udid] {
            ErrorBanner(message: error, fit: .inset,
                        onDismiss: { store.loadErrors[device.udid] = nil })
        }
        if !store.hasLoaded.contains(device.udid) {
            dim("Checking the device…")
        } else if let status {
            if !status.reachable {
                dim("Couldn't read the device: \(status.error ?? "no answer")")
            } else {
                instanceList(status)
                ForEach(Self.blocks(for: status, account: account), id: \.self) { block in
                    switch block {
                    case .certificateReminder: certificateReminder
                    case .altStoreNotice: altStoreNotice(status)
                    case .freeNotes:
                        ForEach(Self.freeAccountNotes(for: status), id: \.self) { dim($0) }
                    case .setUp: setUpButton
                    case .cleanUp: cleanUpButton
                    }
                }
            }
        }
    }

    /// The parts of the section below the instance list, in order.
    enum Block: Hashable {
        /// A certificate Graft put on the device is still there (spec §5c).
        case certificateReminder
        case altStoreNotice
        case freeNotes
        /// Set Up LiveContainer…, which opens the sheet with every install
        /// and certificate control.
        case setUp
        case cleanUp
    }

    /// Which parts a reachable device's section shows.
    ///
    /// **The reminder comes first**, on either account, whenever the CLI says
    /// a certificate Graft put there is still there (`certificatePushed`):
    /// the guest apps can read it, and Remove it works on either account.
    ///
    /// **Clean up is offered wherever there is a LiveContainer to clean**, on
    /// either account, because `--lc-clean` works on either and on every
    /// instance. It used to ride only inside a checklist, and the checklists
    /// were gated on a store-free device *with a primary* — so a free Apple
    /// ID on a SideStore phone, which is the usual free setup, got neither,
    /// and nor did a phone with only LiveContainer2 or the bare upstream id.
    ///
    /// **Set Up is offered everywhere but a paid account's AltStore phone**,
    /// where `--lc-install` refuses to install, LiveContainer or not. A free
    /// Apple ID's sheet installs nothing: it is SideStore's steps.
    nonisolated static func blocks(for status: LCStatusRecord, account: AccountMode?) -> [Block] {
        var blocks: [Block] = status.certificatePushed ? [.certificateReminder] : []
        if status.managedBy == .altstore {
            blocks.append(.altStoreNotice)
            if account.isFree { blocks.append(.setUp) }
        } else {
            if account.isFree { blocks.append(.freeNotes) }
            blocks.append(.setUp)
        }
        if !status.instances.isEmpty { blocks.append(.cleanUp) }
        return blocks
    }

    @ViewBuilder
    private func instanceList(_ status: LCStatusRecord) -> some View {
        if status.instances.isEmpty {
            dim("Not installed.")
        } else {
            VStack(alignment: .leading, spacing: 4) {
                ForEach(status.instances) { instance in
                    HStack(alignment: .top, spacing: 6) {
                        (instance.wildcardSigned ? Symbols.attention : Symbols.ok)
                            .image(size: 11)
                            .foregroundStyle(instance.wildcardSigned ? Palette.warn : Palette.success)
                            .padding(.top, 1)
                        VStack(alignment: .leading, spacing: 1) {
                            Text("\(instance.displayName) \(instance.version)")
                                .font(AppFont.monoSmall)
                                .foregroundStyle(Palette.text)
                            if instance.wildcardSigned {
                                Text("Signed with a wildcard: guest apps can't keep a login. "
                                     + "It has to be deleted on the device before Set Up can "
                                     + "install over it.")
                                    .font(AppFont.monoSmall)
                                    .foregroundStyle(Palette.warn)
                                    .lineLimit(3)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                }
            }
        }
    }

    private func altStoreNotice(_ status: LCStatusRecord) -> some View {
        let source = status.sources[status.latest?.channel ?? "nightly"]
            ?? status.sources["nightly"] ?? ""
        return VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Symbols.advisory.image(size: 11).foregroundStyle(Palette.accent)
                // `managed_by: altstore` is AltStore *or* SideStore on the
                // device (`lc_manage.altstore_present`), so both are named,
                // and it says so with LiveContainer installed or not: the
                // Set Up sheet's meaning (`SetUpLiveContainerSheet.altStoreNote`).
                Text("SideStore or AltStore is on this device: install or update LiveContainer with it.")
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.text)
            }
            Text("Add this source there, then install or update LiveContainer from it:")
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.textDim)
            HStack(spacing: Metrics.controlGap) {
                Text(source)
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.text)
                    .textSelection(.enabled)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Button("Copy") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(source, forType: .string)
                }
                .controlSize(.small)
                .fixedSize()
            }
        }
    }

    /// While a certificate Graft put on the device is still there: the
    /// sheet was closed before "I imported the certificate", or it came
    /// from Terminal. Remove it is the tick's path, `--lc-clean --cert-only`.
    private var certificateReminder: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Symbols.attention.image(size: 11)
                .foregroundStyle(Palette.warn)
                .accessibilityHidden(true)
            Text(Self.reminder(name: device.name))
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.warn)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
            Button(Self.removeItLabel) { store.startRemoveCert(udid: device.udid) }
                .controlSize(.small)
                .fixedSize()
                .disabled(store.isBusy)
                .help("Take the certificate off \(device.name): LiveContainer's guest apps can "
                      + "read its Documents folder.")
            Spacer(minLength: 0)
        }
    }

    private var setUpButton: some View {
        HStack(spacing: Metrics.controlGap) {
            Button(Self.setUpLabel) { onSetUp() }
                .controlSize(.small)
                .fixedSize()
                .help(Self.setUpHelp(account: account, name: device.name))
            Spacer(minLength: 0)
        }
    }

    /// Set Up LiveContainer…'s tooltip, by account: a free Apple ID's sheet
    /// installs nothing.
    nonisolated static func setUpHelp(account: AccountMode?, name: String) -> String {
        account.isFree
            ? "SideStore or iloader, and the steps on \(name), in one sheet"
            : "Install LiveContainer on \(name) and put its certificate there, then the steps on "
                + "the device, in one sheet"
    }

    /// What installing will and will not do to the copies already there —
    /// said before the button is pressed, because neither caveat is
    /// recoverable afterward. The Set Up sheet shows these.
    ///
    /// An in-place upgrade keeps LiveContainer's data, guest apps included.
    /// But a copy installed elsewhere (by hand, by a script) signed its guest
    /// apps under that installer's certificate, and they need re-signing
    /// under ours afterward. And a wildcard-signed copy — the zsign one —
    /// cannot be upgraded in place at all (iOS refuses a changed
    /// application-identifier), so it has to be deleted first, and its data
    /// goes with it. That line names only the instances it applies to.
    nonisolated static func installNotes(for status: LCStatusRecord) -> [String] {
        var notes: [String] = []
        if status.managedBy == .unknown, !status.instances.isEmpty {
            notes.append(
                "Installed outside Graft. Installing upgrades it in place and "
                + "keeps its data, but apps signed under another certificate need "
                + "re-signing afterward (in LiveContainer: the app › Force Sign, or "
                + "renew the certificate)."
            )
        }
        let wildcard = status.instances.filter(\.wildcardSigned).map(\.displayName)
        if !wildcard.isEmpty {
            notes.append(
                "\(wildcard.joined(separator: ", ")) was signed with a wildcard and "
                + "can't be upgraded in place: delete it on the device first, which "
                + "loses its data, then install."
            )
        }
        return notes
    }

    /// Who manages LiveContainer on this device and where to update it, on a
    /// free Apple ID. AltStore-managed devices are not asked: they get
    /// `altStoreNotice`, on either account.
    nonisolated static func freeAccountNotes(for status: LCStatusRecord) -> [String] {
        guard !status.instances.isEmpty else {
            return ["Install LiveContainer with SideStore or iloader, using your free "
                    + "Apple ID, then Re-check."]
        }
        switch status.managedBy {
        case .graft:
            return ["Installed by Graft with a paid account. Updating it from Graft needs "
                    + "that account again; SideStore or iloader can install one with your "
                    + "free Apple ID instead."]
        case .unknown, .altstore:
            return ["Installed outside Graft, by SideStore, AltStore, iloader or Impactor. "
                    + "Update LiveContainer in the one that installed it."]
        }
    }

    private var cleanUpButton: some View {
        HStack(spacing: Metrics.controlGap) {
            Button {
                store.startClean(udid: device.udid)
            } label: {
                Symbols.delete.label("Clean up")
            }
            .controlSize(.small)
            .fixedSize()
            .disabled(store.isBusy)
            .help("Remove the certificate and the IPAs Graft pushed from "
                  + "LiveContainer's Documents. Nothing else is touched.")
            Spacer(minLength: 0)
        }
    }

    /// What a clean did, one line per LiveContainer instance it reported on.
    ///
    /// A single record — the primary alone, which is every `--cert-only` run
    /// and every device with one instance — keeps the sentence this section
    /// has always shown. With more than one, each line leads with the instance
    /// (its own name from the status when listed, its bundle id otherwise),
    /// because "Removed example.ipa" twice says nothing about where.
    static func cleanedLines(
        _ records: [LCCleanedRecord], instances: [LCInstanceRecord]
    ) -> [String] {
        if records.count == 1, let only = records.first {
            return [only.removed.isEmpty
                    ? "✓ Nothing to clean up"
                    : "✓ Removed \(only.removed.joined(separator: ", "))"]
        }
        return records.map { record in
            let name = instances.first { $0.bundleId == record.bundleId }?.displayName
                ?? record.bundleId
            return record.removed.isEmpty
                ? "✓ \(name): nothing to clean up"
                : "✓ \(name): removed \(record.removed.joined(separator: ", "))"
        }
    }

    private func dim(_ text: String) -> some View {
        Text(text)
            .font(AppFont.monoSmall)
            .foregroundStyle(Palette.textDim)
            .lineLimit(3)
            .fixedSize(horizontal: false, vertical: true)
    }
}
