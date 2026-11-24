import Foundation

/// Per-build flags chosen in the UI. These modify a single build — they are
/// deliberately NOT persisted to config.yaml. Callers are expected to reset
/// these when the selected app changes.
struct BuildOptions: Equatable {
    var dryRun = false
    var skipInstall = false
    /// Skips the cryptid-0 verification the pipeline runs before injecting.
    /// An escape hatch, not a convenience: if the check ever false-positives
    /// on an IPA, without this the app simply cannot be built from the GUI at
    /// all. Injecting into a still-encrypted binary produces a build that
    /// fails on-device, so it stays off unless deliberately chosen.
    var skipDecryptCheck = false
    /// Build an artifact that loads on *this Mac* under PlayCover, rather than
    /// one for a phone.
    ///
    /// **An action's flag, not a toggle the user leaves set.** `AppDetailView`
    /// sets this on a *copy* of the pane's options for the length of one
    /// launch, so `buildOptions` itself never carries it and no pill for it can
    /// appear in the header. It lives on this type anyway because it changes
    /// the argv, and the argv is what this type is for — the alternative was a
    /// second place that assembles arguments, which is what `CLIContract`'s own
    /// doc says the flag list exists to prevent.
    ///
    /// It implies `skipInstall`, and the implication lives in `activeOptions`
    /// rather than in `cliArgs` — see there.
    var forPlaycover = false
    /// nil (or empty) means "let the CLI pick", which honors
    /// `default_device_udid` from config.yaml. Note nil and "" are equivalent
    /// to `cliArgs` and `isDefault`, but not to `==`.
    var deviceUDID: String?

    /// One active, non-default selection. Typed so consumers can map a pill
    /// back to the field that produced it with an exhaustive switch, rather
    /// than parsing a label string.
    enum Option: Hashable {
        case dryRun
        /// A Mac build, which implies `skipInstall`.
        ///
        /// `deliverySuppressor` is `activeOptions.first { … }`, so whichever
        /// of the two comes first is the reason the UI shows — and it must be
        /// this one: "not delivered: skip-install" describes a phone build
        /// that was skipped, not a build that went somewhere else.
        ///
        /// **That ordering lives in `activeOptions`' append sequence, not
        /// here.** Measured: moving this case below `skipInstall` changes
        /// nothing, while swapping the two appends in `activeOptions` fails
        /// `test_a_mac_build_activates_the_skip_as_a_typed_option_too` and
        /// `test_a_dry_run_still_outranks_a_mac_build`. Declaration order in
        /// this enum is free.
        case forPlaycover
        case skipInstall
        case skipDecryptCheck
        case device(udid: String)

        /// Fallback label. Consumers that can do better — e.g. resolving a
        /// UDID to a device name — should.
        var defaultLabel: String {
            switch self {
            case .dryRun: return "dry-run"
            // The flag's own spelling minus the dashes, which is the rule
            // every label above follows. Nothing renders this one today —
            // the only surface that draws a label is the header's pills,
            // over `AppDetailView.buildOptions`, which never carries
            // `forPlaycover` (see the field). So the sibling convention is
            // the only criterion available, and matching it is what stops a
            // sixth option arriving at a list with two conventions in it.
            case .forPlaycover: return "for-playcover"
            case .skipInstall: return "skip-install"
            case .skipDecryptCheck: return "skip-decrypt-check"
            case .device(let udid): return "device: \(udid)"
            }
        }

        /// Why this option stops the build reaching the phone, or nil.
        ///
        /// Exhaustive on purpose: a sixth option is a decision about whether
        /// it delivers, not a silently-delivering run.
        ///
        /// **`.forPlaycover` gets its own reason rather than borrowing the
        /// skip's.** It implies `.skipInstall`, so leaving it nil here would
        /// still have suppressed delivery correctly and said "Install skipped"
        /// — half true, and the wrong half. The three surfaces that read this
        /// string put it in front of a consequence: the notification says
        /// "\(reason) — nothing was sent to the device.", the cancel warning
        /// says "\(reason) — nothing is being sent to the device on this run",
        /// and `BuildTargetBar` says "\(reason) — this build won't be sent to a
        /// device." All three are true of a Mac build and none of them says
        /// what *did* happen; "Install skipped" reads as a phone build the user
        /// held back, which is the one thing a Mac build is not. Worded as a
        /// tense-neutral phrase because those three sentences are past, present
        /// and future respectively.
        var suppressesDelivery: String? {
            switch self {
            case .dryRun: return "Dry run"
            case .forPlaycover: return "Built for this Mac"
            case .skipInstall: return "Install skipped"
            case .skipDecryptCheck, .device: return nil
            }
        }

        /// Whether a build carrying this option still resolves a device — and
        /// therefore still fails when there is no device to resolve.
        ///
        /// **The distinction the two suppressing options do not share, and the
        /// reason `BuildTargetBar` needs the option rather than
        /// `suppressesDelivery`'s prose.** Both of them stop anything reaching
        /// the phone, so both are "not delivering"; only one of them stops the
        /// build *looking* for a phone first.
        ///
        ///  - `--skip-install` gates the lookup. `cli.py` wraps `select_device`
        ///    in `if not skip_install:` in both delivery branches (`cli.py:4877`
        ///    guarding `:4883`, and `:4969` guarding `:4973`), and the preflight
        ///    is told the same thing — `run_all_checks(…, will_install=not
        ///    skip_install)` at `cli.py:4725`, which gates its device checks on
        ///    it. Nothing is looked for, so nothing about the bench can make
        ///    this build fail.
        ///  - `--dry-run` gates neither. `select_device` runs *before* the push
        ///    and the install are handed `dry_run=dry_run` and told to do
        ///    nothing, and `installer.select_device` raises unconditionally on
        ///    an empty list ("no iOS devices connected") and on a `--device`
        ///    that is not in it ("requested device … not connected"). A dry run
        ///    on an empty bench is a guaranteed failure, not a quiet no-op.
        ///
        ///  - `--for-playcover` gates it the same way `--skip-install` does,
        ///    because it cannot be sent without one: `cli.py:5413` raises on
        ///    the pair being incomplete, and `activeOptions` below is what
        ///    guarantees this app never sends it that way. So a Mac build is
        ///    always also a skipped install, and never looks for a phone.
        ///
        /// The non-suppressing two answer true for the ordinary reason: they
        /// deliver, so of course they resolve a device. Exhaustive so a sixth
        /// option has to state which it is.
        var stillResolvesDevice: Bool {
            switch self {
            case .dryRun: return true
            case .forPlaycover, .skipInstall: return false
            case .skipDecryptCheck, .device: return true
            }
        }

        /// Spelling comes from `CLIContract.Flag`, which `CLIContractTests`
        /// checks against the CLI's own `--describe` output — so these can't
        /// drift from `graft_ipa/cli.py` unnoticed.
        var cliArgs: [String] {
            switch self {
            case .dryRun: return [CLIContract.Flag.dryRun]
            case .forPlaycover: return [CLIContract.Flag.forPlaycover]
            case .skipInstall: return [CLIContract.Flag.skipInstall]
            case .skipDecryptCheck: return [CLIContract.Flag.skipDecryptCheck]
            case .device(let udid): return [CLIContract.Flag.device, udid]
            }
        }
    }

    /// Active selections, in the same order as `cliArgs`. This is the single
    /// source of truth for which options are active — `cliArgs` and
    /// `isDefault` both derive from it, so they cannot disagree.
    ///
    /// **`forPlaycover` implies `skipInstall`, and the implication is written
    /// here for exactly that reason.** `cli.py:5413` raises on `--for-playcover`
    /// without `--skip-install` — deliberately, rather than implying it, because
    /// the flag changes what the artifact *is* — so a Mac build that sent only
    /// the one flag would die before doing any work. Putting the `if` in
    /// `cliArgs` instead would have produced the right argv and the wrong
    /// everything else: `deliverySuppressor`, `effectiveDelivery`,
    /// `DeviceExposure` and the header pills all read this list, and a build
    /// whose argv said "not installing" while this list did not would be the
    /// drift `effectiveDelivery`'s own doc records happening once already.
    var activeOptions: [Option] {
        var options: [Option] = []
        if dryRun { options.append(.dryRun) }
        if forPlaycover { options.append(.forPlaycover) }
        if skipInstall || forPlaycover { options.append(.skipInstall) }
        if skipDecryptCheck { options.append(.skipDecryptCheck) }
        if let udid = deviceUDID, !udid.isEmpty { options.append(.device(udid: udid)) }
        return options
    }

    /// "Default" means "renders no arguments" — so this stays correct
    /// automatically as options are added, and agrees with `cliArgs` about
    /// edge cases like an empty UDID string.
    var isDefault: Bool { activeOptions.isEmpty }

    /// Order is fixed so tests can assert on it and so copied build commands
    /// read consistently.
    var cliArgs: [String] { activeOptions.flatMap(\.cliArgs) }

    /// What a build started with these options will actually do to the phone,
    /// given the app's configured mode.
    ///
    /// Derived from `activeOptions`, which the comment above calls "the single
    /// source of truth for which options are active — `cliArgs` and `isDefault`
    /// both derive from it, so they cannot disagree." This read `dryRun` and
    /// `skipInstall` directly and was the one member that could, and it had
    /// already drifted: the header pill said `dry-run` while the notification
    /// said `Dry run`, twelve lines apart.
    ///
    /// The config-only answer is a lie the banner used to tell: `cli.py` gates
    /// the LiveContainer push and the sideload install behind `--skip-install`,
    /// and `--dry-run` executes nothing at all, yet both still emit
    /// `build-end {ok: true}`.
    func effectiveDelivery(for mode: DeliveryMode) -> EffectiveDelivery {
        if let reason = deliverySuppressor?.suppressesDelivery {
            return .notDelivered(reason: reason)
        }
        switch mode {
        case .livecontainer: return .livecontainer
        case .sideload: return .sideload
        }
    }

    /// Which active option stops this build reaching the phone, or nil.
    ///
    /// The typed half of `effectiveDelivery(for:)`, which derives from it. It
    /// exists because the three suppressing options are *not* interchangeable
    /// once a consumer wants to say what happens next — see
    /// `Option.stillResolvesDevice` — and `EffectiveDelivery.notDelivered`
    /// carries only `reason: String`, which is prose meant for reading rather
    /// than a value meant for deciding. `BuildTargetBar` needs to decide, and
    /// recovering "was that Dry run or Install skipped?" by comparing the
    /// sentence back against these strings would make the wording load-bearing:
    /// changing "Dry run" to "Dry-run" would silently change behavior on
    /// another screen.
    ///
    /// `activeOptions` order puts `dryRun` first, which is the more complete
    /// description when both are set — and, since a dry run is the one that
    /// still resolves a device, also the most cautious of the three. It puts
    /// `forPlaycover` ahead of `skipInstall` for the other reason: the skip is
    /// that option's own implication, so naming it would be answering "why is
    /// nothing going to the phone?" with a consequence instead of the cause.
    var deliverySuppressor: Option? {
        activeOptions.first { $0.suppressesDelivery != nil }
    }
}
