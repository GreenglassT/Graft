import Foundation
import Observation

private typealias Flag = CLIContract.Flag

/// The apps installed on each connected device — the pick list for decryption
/// — and the state of the dump running against one of them.
///
/// The data half of the decrypt feature: list what a phone has, mark the
/// encrypted ones (an App Store app is worth dumping; a sideload is already
/// decrypted), and let a view target one. The dump backend itself — bagbak,
/// frida-ios-dump against a jailbroken device — is a separate piece; this is
/// what tells it which bundle id to attach to.
///
/// Loading is user-initiated, not automatic: enumerating installed apps takes
/// a real round trip to the device, and a pane that did it on every glance
/// would stall on a phone that is asleep or off the network.
///
/// ## Keyed by udid, like its three siblings
///
/// One instance, owned by `ContentView` beside `DevicesStore`,
/// `DeviceInfoStore` and `DeviceDeliveryStore`. Every field that describes *a
/// phone* — the listing, its end record, whether a listing is in flight, its
/// error, its readiness answer — is a dictionary keyed by udid, because this
/// window shows one phone at a time and switches between them by clicking a
/// sidebar row.
///
/// It used to be single-valued and instantiated twice per pane, once for the
/// dump and once for the listing, and both halves of that cost something real:
///
/// * The listing store had to be `.id(device.udid)`-ed to stop it showing
///   phone A's apps under phone B's name — which threw away an 8.4–10.8s
///   listing on **every** switch between two phones, and made a listing the
///   user had already paid for unrecoverable except by paying again.
/// * The decrypt store was not `.id`-ed (its dump summary is meant to outlive
///   a switch), so for the 2–5s the new phone's readiness probe takes,
///   `DeviceDecryptSection` drew **phone A's readiness under phone B's
///   header** — a card saying "jailbreak: ok" about the wrong handset.
///   `DecryptCheckRecord` carries `device` and `device_name`, and nothing
///   compared them.
///
/// Two concerns is the right observation; two *instances* was the wrong
/// remedy, because neither instance could say which phone it was about. The
/// concerns are kept apart by having two fields rather than two stores: a
/// failed listing lands in `listErrors[udid]`, a failed dump in `lastError`,
/// so `InstalledAppsSection` still cannot draw a banner about a dump it had
/// nothing to do with.
///
/// ## What is deliberately *not* keyed
///
/// The dump: `lastDecrypt`, `lastAttempt`, `transcript`, `notes`, `dumping`,
/// `lastError`. There is one dump at a time across the whole app — both entry
/// points `guard dumping == .idle` — so a dictionary would be a map that can
/// only ever hold one entry, and the card that narrates a dump is meant to go
/// on saying what it staged after you have looked at another phone. Those
/// values name the bundle id they are about, so they read as stale rather than
/// misattributed. `isChecking` is single-valued for the same reason: overlapping
/// readiness probes are already settled by `readinessGeneration`, so only the
/// newest one is ever the one on screen.
@MainActor
@Observable
final class DeviceAppsStore {
    /// The installed-apps listing per device. A udid with no entry has never
    /// been listed; see `lastLoad` for how that differs from a phone with no
    /// apps.
    private(set) var apps: [String: [DeviceAppRecord]] = [:]
    /// Which devices have a listing in flight — the spinner on that phone's
    /// section, and the guard that keeps a second press from starting a
    /// second run.
    ///
    /// A set rather than one `Bool`, because two phones' listings genuinely
    /// overlap: a listing takes ~9s, and switching panes and pressing "List
    /// apps" on the other phone inside that window is ordinary. One flag would
    /// have dropped the second request silently and spun the wrong section.
    private(set) var loading: Set<String> = []
    /// nil until a load has finished *for that udid*, so "no apps" and "not
    /// asked yet" render differently — the same rule the update check follows.
    private(set) var lastLoad: [String: DeviceAppsEndRecord] = [:]
    /// A failed *listing*, per device — not a failed dump. `lastError` is the
    /// dump's, and keeping the two apart is what stops a dump's error banner
    /// appearing under a section that had nothing to do with the dump.
    var listErrors: [String: String] = [:]
    /// A failed dump. One value, because there is one dump.
    var lastError: String?

    let runner: CLIRunner

    init(runner: CLIRunner) {
        self.runner = runner
    }

    /// Apps worth decrypting on `udid`: the FairPlay-encrypted ones, by name.
    func encryptedApps(on udid: String) -> [DeviceAppRecord] {
        (apps[udid] ?? []).filter(\.encrypted)
    }

    /// What's being dumped right now — one field, so a row dump and a frontmost
    /// dump cannot both look active. The two UIs read it through the derived
    /// views below; the guards test `.idle` directly.
    private enum Dumping: Equatable {
        case idle
        case row(String)   // a specific app's row, keyed by bundle id
        case frontmost     // whatever's on screen; no bundle id until it reports
    }
    private var dumping: Dumping = .idle

    /// The bundle id whose row is dumping, or nil — drives that row's spinner.
    var decrypting: String? {
        if case let .row(bundleId) = dumping { return bundleId }
        return nil
    }
    /// Whether the frontmost dump is running — drives the Devices pane button.
    var decryptingFrontmost: Bool { dumping == .frontmost }

    /// Whether *any* dump is running, a row's or the frontmost one.
    ///
    /// Both entry points below `guard dumping == .idle` and return nil rather
    /// than queueing, so a control left enabled during the other one's dump is a
    /// button that silently does nothing when pressed. That became reachable
    /// when the per-device pane put both controls on one screen over one store:
    /// `DeviceDecryptSection`'s one-click button and the Dump on every encrypted
    /// row of `InstalledAppsSection`.
    ///
    /// Decided here rather than written out as `decrypting != nil ||
    /// decryptingFrontmost` at each call site — that expression was already
    /// copied into two views in the pane this replaces, and it is one `||` away
    /// from a control that re-enables itself mid-dump.
    var isDumping: Bool { dumping != .idle }

    /// The last successful dump, kept so the pane can say where it landed and
    /// that the app is now buildable.
    private(set) var lastDecrypt: DecryptEndRecord?

    /// Dump a decrypted IPA of `app` off the device and stage it for a build.
    ///
    /// Returns the record on success. The CLI verifies cryptid 0 before it
    /// emits `decrypt-end`, so a non-nil return is a promise the staged IPA
    /// will pass the build's own decryption check — not merely that bagbak
    /// wrote a file.
    @discardableResult
    func decrypt(
        _ app: DeviceAppRecord, device: String? = nil,
        extensions: ExtensionPolicy = .automatic, backend: String? = nil,
        verbose: Bool = false, argsOverride: [String]? = nil
    ) async -> DecryptEndRecord? {
        guard dumping == .idle else { return nil }
        dumping = .row(app.bundleId)
        defer { dumping = .idle }
        lastAttempt = Attempt(app: app, device: device, backend: backend, verbose: verbose)
        return await runDecrypt(
            argsOverride ?? Self.decryptArgs(
                bundleId: app.bundleId, device: device,
                extensions: extensions,
                // Deliberately forwarded as-is, nil included. Naming a backend
                // *pins* it, and a pinned dump does not fall back to the other
                // tool — so filling in a default here would have quietly cost
                // the automatic retry to reach the same choice the CLI makes.
                backend: backend,
                verbose: verbose
            ),
            failing: "Decrypt app"
        )
    }

    /// Dump whatever app is on screen right now — no bundle id, the CLI reads
    /// the foreground app off the device itself. `device` pins which phone to
    /// read it from; nil lets the CLI use its default device.
    ///
    /// **Not "the reliable path" against a row dump — that was true once and
    /// is stale now.** Both routes attach through the same
    /// `ensure_frontmost(bundle_id, …)` on the CLI side (`decrypt_runner.py`,
    /// called from `_dump_frida_ios_dump`), which launches and foregrounds
    /// *whichever* bundle id it is handed before attaching — a row dump's
    /// target included. What is actually different: this route needs no
    /// bundle id and no listing, since the CLI reads the foreground app
    /// directly rather than the user picking a row; and because its target is
    /// by definition already frontmost, `ensure_frontmost`'s own check
    /// (`frontmost_app(udid) == bundle_id`) short-circuits immediately, so
    /// this route never runs the launch step a row dump's target might need —
    /// the one step with its own failure modes (a refused launch, a death on
    /// spawn, an app that never reaches the front; see `_launch_failure`).
    /// Open the app, press the button. Same `decrypt-end` contract as
    /// `decrypt(_:)`, so a non-nil return is again a staged, cryptid-0 IPA.
    @discardableResult
    func decryptFrontmost(
        device: String? = nil, extensions: ExtensionPolicy = .automatic,
        backend: String? = nil, verbose: Bool = false, argsOverride: [String]? = nil
    ) async -> DecryptEndRecord? {
        guard dumping == .idle else { return nil }
        dumping = .frontmost
        defer { dumping = .idle }
        // Remembered so the pane can offer the one recovery that works without
        // the user retyping anything — see `retryDroppingExtensions`.
        lastAttempt = Attempt(app: nil, device: device, backend: backend, verbose: verbose)
        return await runDecrypt(
            argsOverride ?? Self.decryptArgs(
                bundleId: nil, device: device,
                extensions: extensions, backend: backend,
                verbose: verbose
            ),
            failing: "Decrypt foreground app"
        )
    }

    /// Everything needed to repeat the last dump exactly.
    ///
    /// A retry has to re-run *that* dump, not a fresh one. Keeping only the
    /// device and backend meant `retryDroppingExtensions` always called
    /// `decryptFrontmost` — so a failed *row* dump retried whatever happened to
    /// be on screen, and staged it into the cache under its own bundle id. The
    /// app being dumped is part of the attempt, so it is recorded with it.
    struct Attempt: Equatable {
        /// nil means the frontmost route rather than a named app.
        var app: DeviceAppRecord?
        var device: String?
        var backend: String?
        var verbose: Bool
    }
    private(set) var lastAttempt: Attempt?

    /// The device the last dump aimed at. Read by the pane; kept as a derived
    /// value so callers cannot drift from `lastAttempt`.
    var lastFrontmostDevice: String? { lastAttempt?.device }
    var lastBackend: String? { lastAttempt?.backend }

    /// The backend's own output from the run in progress, newest last.
    ///
    /// Streamed rather than collected at the end: a dump takes a minute and the
    /// only question while it runs is whether it is progressing. bagbak prints
    /// per-file download progress, so this is the difference between a hung
    /// dump and a slow one — which is exactly what "hit or miss" felt like.
    private(set) var transcript: [String] = []

    /// What the CLI decided on the user's behalf during this dump, in order.
    ///
    /// Kept apart from the backend's own output because they are read
    /// differently. The transcript is hundreds of per-file lines, skimmed and
    /// truncated; these are the two or three sentences that explain why the
    /// dump changed tack — and a sentence truncated in the middle is worse than
    /// no sentence, because the reader cannot tell they missed something.
    private(set) var notes: [String] = []

    /// Whether the last failure is the one a second run can actually fix:
    /// undumpable app extensions that this dump was told to keep.
    ///
    /// Dropping them is what a dump does unasked, so this can only follow an
    /// explicit "keep every extension" — and the fix is to take that back, for
    /// this dump, without the user retyping anything.
    ///
    /// Read off the CLI's own message rather than re-derived here. The CLI
    /// inspected the paths and phrased the hint; a second opinion computed from
    /// an error string is exactly how the two drift apart. What this looks for
    /// is the flag the CLI names, so a reworded hint recommending the same fix
    /// keeps working, and one that stops recommending it stops offering the
    /// button.
    var canRetryDroppingExtensions: Bool {
        guard dumping == .idle, let lastError else { return false }
        // Either flag names this recovery. The CLI mentions
        // `--keep-encrypted-extensions` when the user asked to keep them, and
        // `--strip-encrypted-extensions` when it refused on its own because the
        // extension is the app. Both are undone by the same action, so both
        // offer the same button — and pressing it forces, rather than merely
        // clearing the request, or the refusal would simply happen again.
        return lastError.contains(Flag.keepEncryptedExtensions)
            || lastError.contains(Flag.stripEncryptedExtensions)
    }

    /// Re-run the last frontmost dump, this time removing the extensions that
    /// could not be decrypted.
    ///
    /// Aimed at the same device as the attempt that failed, because "retry"
    /// meaning "retry somewhere else" would be its own bug — and with two
    /// phones connected, only one of them is the jailbroken one.
    @discardableResult
    func retryDroppingExtensions() async -> DecryptEndRecord? {
        guard let attempt = lastAttempt else { return nil }
        // Same app, same device, same backend — only the strip flag changes.
        // Anything else and "retry" would quietly mean "dump something else":
        // the leftovers being reacted to belong to *this* dump.
        if let app = attempt.app {
            return await decrypt(
                app, device: attempt.device, extensions: .force,
                backend: attempt.backend, verbose: attempt.verbose
            )
        }
        return await decryptFrontmost(
            device: attempt.device, extensions: .force,
            backend: attempt.backend, verbose: attempt.verbose
        )
    }

    /// The last readiness probe *per device*, or no entry before one has run
    /// for that phone.
    ///
    /// Keyed, unlike everything else about a dump, and it is the field the
    /// re-keying was worth most for. `DeviceDecryptSection` renders this
    /// directly under the pane's header, and a probe takes 2–5s — so with one
    /// shared value, switching from a jailbroken phone to a stock one drew the
    /// jailbroken phone's "ready to dump" checklist under the stock phone's
    /// name for as long as the new probe ran. The record carries `device` and
    /// `device_name`; nothing compared them, and now nothing has to.
    ///
    /// A phone already probed keeps its answer while it is re-probed, which is
    /// the other half of the same property: coming back to a phone shows what
    /// it said last time rather than a blank card.
    private(set) var readiness: [String: DecryptCheckRecord] = [:]
    private(set) var isChecking = false

    /// Ask whether a dump could run right now, without starting one.
    ///
    /// Cheap and read-only (~2s), so the pane can answer "why can't I decrypt"
    /// before the user spends a minute finding out. Every precondition a dump
    /// has — jailbreak, SSH, an app actually foregrounded — used to be
    /// discovered by starting one and reading a timeout that named none of them.
    ///
    /// Failures clear that phone's entry rather than landing in `lastError`:
    /// not being able to *ask* is not a decrypt failure, and putting it in the
    /// error banner would sit a red bar over a pane whose real problem is that
    /// the phone is asleep.
    /// Bumped per request so a slow probe cannot overwrite a newer one.
    private var readinessGeneration = 0

    /// `udid` is required rather than optional, and it is what the answer is
    /// filed under. There is no honest key for "whichever phone the CLI picks",
    /// and a card that cannot say which phone its checklist describes is the
    /// defect this store was re-keyed to end — every caller is a pane that is
    /// already about exactly one phone.
    ///
    /// `argsOverride` replaces the argv entirely, the same escape hatch `load`
    /// and the two dump paths carry and for the same reason: this probe is the
    /// only thing that fills `readiness`, which is `private(set)`, so without it
    /// a view test can only ever measure the card with no answer on it — an
    /// empty card fits any window, and a layout guard that measures one proves
    /// nothing. See `DeviceDetailLayoutTests`.
    func checkReadiness(udid: String, argsOverride: [String]? = nil) async {
        // Deliberately not guarded on `isChecking`. `.task(id:)` re-fires when
        // the device picker changes, and the cancelled probe has not run its
        // `defer` yet — so a guard dropped the *new* request while the old one
        // resolved to nil, leaving the card blank with the button disabled and
        // no stated reason.
        //
        // Overlap is allowed instead, and settled by generation rather than by
        // arrival: probes take tens of seconds and the slower one is often the
        // stale one, so "last to finish wins" would show the previous phone's
        // answer under the new phone's name.
        readinessGeneration += 1
        let generation = readinessGeneration
        isChecking = true
        defer { if generation == readinessGeneration { isChecking = false } }
        let args = argsOverride ?? [Flag.json, Flag.decryptCheck, Flag.device, udid]
        let result = try? await runner.run(args, failing: "Check decrypt readiness") { event in
            if case let .decryptCheck(record) = event { return record }
            return nil
        }
        guard generation == readinessGeneration else { return }
        // Assigned including nil, which removes the entry: a probe that failed
        // must not leave the previous answer standing as though it had just
        // been confirmed.
        readiness[udid] = result
    }

    /// The argv for a dump: by bundle id when given, otherwise frontmost.
    ///
    /// Built in one place so the two entry points cannot disagree, and pulled
    /// out of them so a test can assert what actually reaches the CLI — the
    /// defect here was an *absent* flag, which a test that stubs the argv can
    /// never see.
    ///
    /// `--device` is passed rather than left to the CLI's default, and that is
    /// a correctness fix rather than a nicety: `default_device_udid` names the
    /// phone builds are *delivered* to, which is not the phone dumps come
    /// *off*. On a bench with both, delivery goes to a stock phone running
    /// LiveContainer while dumping needs the jailbroken one — so the default
    /// aimed decryption at hardware that could never perform it, and said so
    /// as a frida error about the wrong device.
    /// What to do about app extensions no backend can decrypt.
    ///
    /// Three states rather than a switch, because the third one exists: a dump
    /// refuses by default when the extension it would drop *is* the app — a
    /// content blocker, a VPN tunnel — and taking that refusal back is a
    /// different request from "keep everything".
    enum ExtensionPolicy {
        /// Drop what nothing can decrypt, unless the app is nothing without it.
        case automatic
        /// Drop it regardless. A partial app is still useful to me.
        case force
        /// Drop nothing; fail instead.
        case keep
    }

    static func decryptArgs(
        bundleId: String?, device: String?, extensions: ExtensionPolicy = .automatic,
        backend: String? = nil, verbose: Bool = false
    ) -> [String] {
        var args = [Flag.json]
        if let bundleId {
            args += [Flag.decrypt, bundleId]
        } else {
            args.append(Flag.decryptFrontmost)
        }
        if let device { args += [Flag.device, device] }
        if let backend { args += [Flag.decryptBackend, backend] }
        // `.automatic` sends nothing — it is what a dump does unasked.
        switch extensions {
        case .automatic: break
        case .force: args.append(Flag.stripEncryptedExtensions)
        case .keep: args.append(Flag.keepEncryptedExtensions)
        }
        if verbose { args.append(Flag.verbose) }
        return args
    }

    /// The dump both entry points share: run the CLI, take the `decrypt-end`
    /// record it emits after its own cryptid-0 check, and remember it. The two
    /// differ only in which app they target and how they show progress.
    private func runDecrypt(_ args: [String], failing: String) async -> DecryptEndRecord? {
        // Fresh slate: exactly one of lastDecrypt/lastError is set by the end of
        // this call, so the card can never show a prior run's success line
        // beside this run's error banner (or the reverse).
        lastError = nil
        lastDecrypt = nil
        transcript = []
        notes = []
        do {
            // `stream`, not `run`: the point of --verbose is seeing the
            // backend narrate *while* it works, and a collected result can only
            // ever show that after the fact.
            let result = try await runner.stream(arguments: args) { [weak self] event in
                // Only the transcript comes through the callback, where being a
                // little late costs nothing. The *result* is read from
                // `result.events` below, which `CLIRunner` collects
                // synchronously — the callback's MainActor Tasks are not
                // guaranteed to have run by the time this await returns, so a
                // dump that really did stage an IPA could otherwise report
                // nothing at all.
                if case let .log(source, line) = event,
                   source != CLIContract.startupLogSource {  // C-NOTICE: not the dump's
                    if source == CLIContract.selfLogSource {
                        self?.notes.append(line)
                    } else {
                        self?.transcript.append(line)
                    }
                }
            }
            let record = result.events.compactMap { event -> DecryptEndRecord? in
                if case let .decryptEnd(r) = event { return r }
                return nil
            }.last
            guard result.exitCode == 0 else {
                throw CLIError.exited(
                    doing: failing, code: result.exitCode,
                    stderr: result.stderr.isEmpty ? result.stdout : result.stderr
                )
            }
            if let record { lastDecrypt = record }
            return record
        } catch {
            lastError = error.localizedDescription
            return nil
        }
    }

    /// The argv for `--device-apps`, always with `--with-icons`.
    ///
    /// A static, pure function — same shape as `decryptArgs` above — so a test
    /// can assert on the built argv directly instead of needing a real
    /// subprocess.
    ///
    /// `--with-icons` is unconditional rather than a parameter. It costs real
    /// time per app on the CLI side, which is exactly why the flag exists as
    /// an opt-in there — but every caller of this store's `apps` on the GUI
    /// side is a view that draws them (`InstalledAppsSection`), so there is no
    /// cheaper mode this store would ever ask for. If a caller appears that
    /// wants the fast, icon-less listing, that is the moment to make this a
    /// parameter rather than the moment to hardcode a second copy of this
    /// argv without the flag.
    static func loadArgs(device: String) -> [String] {
        [Flag.json, Flag.deviceApps, Flag.withIcons, Flag.device, device]
    }

    /// `udid` names which phone to enumerate, and is what the listing is filed
    /// under. Required for both halves of that: the configured default is the
    /// *delivery* target rather than the phone in front of you, so listing
    /// whichever phone the CLI picks would offer dumps that cannot happen —
    /// and an answer this store cannot name a phone for is one it cannot key,
    /// which is how phone A's apps came to be drawn under phone B's header.
    func load(udid: String, argsOverride: [String]? = nil) async {
        // Per phone, not globally: a listing takes ~9s, and starting one on a
        // second phone while the first is still running is an ordinary thing
        // to do from a pane you have just switched to.
        guard !loading.contains(udid) else { return }
        loading.insert(udid)
        defer { loading.remove(udid) }

        do {
            let events = try await runner.runEvents(
                argsOverride ?? Self.loadArgs(device: udid),
                failing: "List device apps"
            )
            var found: [DeviceAppRecord] = []
            for event in events {
                switch event {
                case let .deviceApp(record): found.append(record)
                case let .deviceAppsEnd(record): lastLoad[udid] = record
                default: break
                }
            }
            apps[udid] = found
            // A run that succeeded clears whatever the last one said. Left
            // standing, a banner from a failed attempt would sit over the
            // listing that replaced it.
            listErrors[udid] = nil
        } catch {
            // The listing is left standing rather than blanked: a phone that
            // dropped off the network mid-session should not erase the list the
            // user was looking at, only say it couldn't refresh it.
            //
            // `listErrors`, not `lastError`. That is the whole of what the two
            // instances of this store used to buy: a failed dump's banner must
            // not appear under the installed-apps section, and a failed listing
            // must not appear on the decrypt card.
            listErrors[udid] = error.localizedDescription
        }
    }
}
