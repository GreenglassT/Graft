import AppKit
import SwiftUI

/// LiveContainer on **this** device: what is installed, and installing it
/// properly.
///
/// "Properly" is the point of the section. LiveContainer used to be built
/// like any other app, through zsign, and zsign writes a wildcard profile's
/// keychain *pattern* into the signature as a literal — which switches off
/// LiveContainer's keychain redirection, so no guest app can keep a login.
/// The CLI's `--lc-install` signs it the way AltStore does; this section is
/// the button for that, plus the steps LiveContainer itself needs afterwards.
///
/// Like `DeviceDecryptSection`, it belongs to one device: `device.udid` is
/// passed to every call and nothing here can name another. And like that
/// card, it re-derives nothing — who manages the device, whether an update is
/// available, whether installing is allowed — those are the CLI's answers.
///
/// An AltStore-managed device gets a read-only notice instead of a button.
/// Replacing AltStore's copies would orphan every guest app's JIT-less
/// signature, and AltStore would refresh its own copy back over ours; the CLI
/// refuses for the same reason, and the GUI does not offer what it refuses.
struct DeviceLiveContainerSection: View {
    let device: DeviceRecord
    let store: LiveContainerStore

    @State private var instances: Set<Int> = [1, 2, 3]
    @State private var channel = "nightly"

    private var status: LCStatusRecord? { store.status[device.udid] }
    private var run: LiveContainerStore.Run? { store.run(for: device.udid) }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            content
            if let run { runReport(run) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(Metrics.cardPadding)
        .cardStyle()
        .task(id: device.udid) {
            // This view is reused across devices, so its choices would carry
            // over to the next phone; each device starts from the defaults.
            instances = [1, 2, 3]
            channel = "nightly"
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
            .disabled(store.loading.contains(device.udid) || store.isRunning)
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
            dim("Busy with LiveContainer on \(elsewhere) — this device's actions wait for it.")
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
                if status.managedBy == .altstore {
                    altStoreNotice(status)
                } else {
                    installControls(status)
                    if status.instances.contains(where: { $0.role == "primary" }) {
                        checklist
                    }
                }
            }
        }
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
                                     + "It has to be deleted before installing — see below.")
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
                Text("Managed by AltStore — update it there")
                    .font(AppFont.monoSmall)
                    .foregroundStyle(Palette.text)
            }
            Text("Add this source in AltStore, then update LiveContainer from it:")
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

    private func installControls(_ status: LCStatusRecord) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Self.installNotes(for: status), id: \.self) { dim($0) }
            HStack(spacing: Metrics.controlGap) {
                Button {
                    store.startInstall(udid: device.udid, instances: instances, channel: channel)
                } label: {
                    if store.isRunning, run?.action == .install {
                        HStack(spacing: 6) {
                            ProgressView().controlSize(.small)
                            Text("Installing…")
                        }
                    } else {
                        Symbols.checkUpdates.label("Install / Update LiveContainer")
                    }
                }
                .disabled(!store.canInstall(udid: device.udid) || store.isRunning
                          || instances.isEmpty)
                ForEach([1, 2, 3], id: \.self) { n in
                    Toggle(n == 1 ? "1" : "\(n)", isOn: Binding(
                        get: { instances.contains(n) },
                        set: { on in if on { instances.insert(n) } else { instances.remove(n) } }
                    ))
                    .toggleStyle(.checkbox)
                    .help(n == 1 ? "LiveContainer (the primary)" : "LiveContainer\(n)")
                }
                Picker("", selection: $channel) {
                    Text("nightly").tag("nightly")
                    Text("stable").tag("stable")
                }
                .pickerStyle(.menu)
                .labelsHidden()
                .fixedSize()
                .help("nightly is what upstream recommends")
                Spacer(minLength: 0)
            }
            .font(AppFont.monoSmall)
            .foregroundStyle(Palette.textDim)
        }
    }

    /// What installing will and will not do to the copies already there —
    /// said before the button is pressed, because neither caveat is
    /// recoverable afterwards.
    ///
    /// An in-place upgrade keeps LiveContainer's data, guest apps included.
    /// But a copy installed elsewhere (by hand, by a script) signed its guest
    /// apps under that installer's certificate, and they need re-signing
    /// under ours afterwards. And a wildcard-signed copy — the zsign one —
    /// cannot be upgraded in place at all (iOS refuses a changed
    /// application-identifier), so it has to be deleted first, and its data
    /// goes with it. That line names only the instances it applies to.
    nonisolated static func installNotes(for status: LCStatusRecord) -> [String] {
        var notes: [String] = []
        if status.managedBy == .unknown, !status.instances.isEmpty {
            notes.append(
                "Installed outside Graft. Installing upgrades it in place and "
                + "keeps its data, but apps signed under another certificate need "
                + "re-signing afterwards (in LiveContainer: the app › Force Sign, or "
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

    /// What LiveContainer needs once it is on the device. Its JIT-less mode
    /// signs guest apps with a certificate; one Graft installed has no
    /// AltStore to borrow one from, so it is imported by hand.
    private var checklist: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Then, in LiveContainer on the device:")
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.textDim)
            step(1, "Settings › Import Certificate — pick AppleDevelopment.p12 "
                 + "(On My iPad › LiveContainer) and enter the .p12 password.") {
                Button("Put certificate on device") {
                    store.startPushCert(udid: device.udid)
                }
                .controlSize(.small)
                .fixedSize()
                .disabled(store.isRunning)
            }
            // Straight after the import, not folded into Clean up: guest apps
            // run in LiveContainer's sandbox and can read its Documents, so
            // the .p12 should not wait there for step 3's IPAs to be imported.
            step(2, "Once imported, take the certificate off the device — LiveContainer's "
                 + "guest apps can read its Documents folder.") {
                Button("Remove certificate") {
                    store.startRemoveCert(udid: device.udid)
                }
                .controlSize(.small)
                .fixedSize()
                .disabled(store.isRunning)
            }
            step(3, "Settings › JIT-Less Diagnose — \"keychain-access-groups correct\" "
                 + "should say Yes.") { EmptyView() }
            step(4, "Import your apps from the Documents folder.") { EmptyView() }
            step(5, "Long-press each app › Convert to Shared App.") { EmptyView() }
            HStack(spacing: Metrics.controlGap) {
                Button {
                    store.startClean(udid: device.udid)
                } label: {
                    Symbols.delete.label("Clean up")
                }
                .controlSize(.small)
                .fixedSize()
                .disabled(store.isRunning)
                .help("Remove the certificate and the IPAs Graft pushed from "
                      + "LiveContainer's Documents. Nothing else is touched.")
                Spacer(minLength: 0)
            }
        }
    }

    private func step<Accessory: View>(
        _ n: Int, _ text: String, @ViewBuilder accessory: () -> Accessory
    ) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text("\(n).")
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.textDim)
            Text(text)
                .font(AppFont.monoSmall)
                .foregroundStyle(Palette.text)
                .lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
            accessory()
        }
    }

    @ViewBuilder
    private func runReport(_ run: LiveContainerStore.Run) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            if !run.finished {
                HStack(spacing: Metrics.controlGap) {
                    if let last = run.progress.last {
                        dim(progressLine(last))
                    }
                    Spacer(minLength: 0)
                    // Cancels the run; the CLI is SIGTERMed and takes its
                    // throwaway keychain off the search list on the way out.
                    Button("Stop") { store.stop() }
                        .controlSize(.small)
                        .fixedSize()
                }
            }
            if let waiting = run.waitingMessage {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Symbols.attention.image(size: 11).foregroundStyle(Palette.warn)
                    Text(waiting.prefix(1).uppercased() + waiting.dropFirst())
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.warn)
                }
            }
            ForEach(run.installed, id: \.bundleId) { r in
                success("✓ \(r.bundleId) \(r.version) installed")
            }
            if let pushed = run.pushed {
                success("✓ Certificate copied to \(pushed.path)")
                if let note = pushed.note {
                    Text(note)
                        .font(AppFont.monoSmall)
                        .foregroundStyle(Palette.warn)
                        .lineLimit(3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if let cleaned = run.cleaned {
                success(cleaned.removed.isEmpty
                        ? "✓ Nothing to clean up"
                        : "✓ Removed \(cleaned.removed.joined(separator: ", "))")
            }
            if let error = run.error {
                ErrorBanner(message: error, fit: .inset, onDismiss: nil)
            }
        }
    }

    private func progressLine(_ p: LCInstallProgressRecord) -> String {
        let who = p.instance.map { $0 == 1 ? "LiveContainer" : "LiveContainer\($0)" }
            ?? "LiveContainer"
        let verb = [
            "fetch": "downloading", "prepare": "preparing", "profile": "provisioning",
            "sign": "signing", "install": "installing",
        ][p.step] ?? p.step
        return "\(who): \(verb)…"
    }

    private func dim(_ text: String) -> some View {
        Text(text)
            .font(AppFont.monoSmall)
            .foregroundStyle(Palette.textDim)
            .lineLimit(3)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func success(_ text: String) -> some View {
        Text(text)
            .font(AppFont.monoSmall)
            .foregroundStyle(Palette.success)
            .textSelection(.enabled)
            .lineLimit(2)
            .fixedSize(horizontal: false, vertical: true)
    }
}
