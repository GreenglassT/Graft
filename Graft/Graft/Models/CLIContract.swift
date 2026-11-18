import Foundation

/// The vocabulary this app shares with the `graft` Python CLI.
///
/// The two sides talk over a process boundary with no schema between them:
/// Swift builds argument vectors out of string literals, the CLI answers with
/// JSON-Lines events, and `String.buildEvents()` decodes each line with `try?`
/// — so a line the switch in `BuildEvent.decode` doesn't recognise is silently
/// dropped and the UI just shows nothing. Flags fail even more quietly: an
/// unrecognised option makes Click abort with a usage error the GUI surfaces
/// as an opaque failure, and (worse, historically) an option in the wrong
/// position is parsed as a positional argument instead.
///
/// This type is where every one of those strings is collected so that a single
/// test can check them against the CLI's own `--describe` output. It is a
/// contract declaration, not an abstraction: `CLIContractTests` asserts each
/// set below is a subset of what the installed CLI actually declares.
///
/// Adding a flag, an event case, or an app-option field means adding it here
/// too. For flags, that is enforced: `CLIContractTests` fails on a bare flag
/// literal anywhere in `Services/` or `Views/` — the two places that build an
/// argument vector.
enum CLIContract {
    /// The argv spelling of every flag the app sends, named once.
    ///
    /// This used to hold only the five per-build toggles, with the other
    /// forty-odd spellings living as literals in the stores' argument vectors
    /// and copied a second time into `flagsUsed` below. That copy is what made
    /// the contract test one layer short of useful: it proved
    /// `flagsUsed ⊆ CLI`, but nothing proved the stores only sent flags from
    /// `flagsUsed`, so a store rewritten to pass `--repo-pkgs` sailed straight
    /// past it and failed at runtime instead.
    ///
    /// Now every caller builds its argv out of these constants and
    /// `flagsUsed` is `all` — one spelling, one place, no parallel list.
    /// `CLIContractTests` nails both ends down: no bare flag literal may
    /// appear in `Services/` or `Views/`, and no constant here may be missing
    /// from `all`.
    enum Flag {
        /// Every command the GUI runs is prefixed with this: it switches the
        /// CLI to JSON-Lines output, the only form `BuildEvent` can decode.
        static let json = "--json"

        // ConfigStore
        static let list = "--list"
        static let addApp = "--add-app"
        static let addAppManual = "--add-app-manual"
        static let name = "--name"
        static let bundleId = "--bundle-id"
        static let displayName = "--display-name"
        static let delivery = "--delivery"
        static let provideIPA = "--provide-ipa"
        static let app = "--app"
        static let cachedIPA = "--cached-ipa"
        static let stagedIPAs = "--staged-ipas"
        static let deleteStaged = "--delete-staged"
        static let force = "--force"
        static let installToMac = "--install-to-mac"
        static let attachTweak = "--attach-tweak"
        static let detachTweak = "--detach-tweak"
        static let removeApp = "--remove-app"
        static let setAppOptions = "--set-app-options"
        /// Global settings — everything the Settings pane could show and not
        /// change. Companions below, all verb-only.
        static let settings = "--settings"
        static let setSettings = "--set-settings"
        static let defaultDevice = "--default-device"
        static let archiveKeep = "--archive-keep"
        static let signingCert = "--signing-cert"
        static let signingProfile = "--signing-profile"
        static let signingTeamId = "--signing-team-id"
        /// Ask the device what it actually holds, then re-report the delivery
        /// state. Scoped with `--app`, targeted with `--device` — neither is
        /// this verb's own flag, and both are already declared above.
        static let reconcile = "--reconcile"
        /// Report every configured app's delivery state on its own, without the
        /// app list `--list` carries alongside it.
        ///
        /// Worth a second subprocess only because of `--device`: it narrows "is
        /// this app current?" to "is it current *on that phone*?", and `--list`
        /// can only ever answer the unscoped question — see
        /// `DeviceDeliveryStore`, the one caller. Left unscoped this verb is
        /// redundant with `--list` and nothing here sends it that way.
        static let deliveryState = "--delivery-state"
        static let preferredVersion = "--preferred-version"
        static let keepBundleId = "--keep-bundle-id"
        static let noKeepBundleId = "--no-keep-bundle-id"
        static let stripExtensions = "--strip-extensions"
        static let noStripExtensions = "--no-strip-extensions"
        static let stripSupportedDevices = "--strip-supported-devices"
        static let noStripSupportedDevices = "--no-strip-supported-devices"
        static let compressionLevel = "--compression-level"

        // TweaksStore
        static let tweaks = "--tweaks"
        static let tweakUpdates = "--tweak-updates"
        static let updateTweak = "--update-tweak"
        static let deviceApps = "--device-apps"
        static let decrypt = "--decrypt"
        static let decryptFrontmost = "--decrypt-frontmost"
        /// Remove app extensions a dump left encrypted, and stage the rest.
        ///
        /// Not cosmetic: frida-ios-dump cannot decrypt an extension at all —
        /// they run in their own processes, so it never sees them — and bagbak
        /// cannot dump one whose MinimumOSVersion is newer than the device's
        /// iOS, because it can't be launched to be read. Without this the
        /// decrypt of any app carrying a widget or share sheet fails outright,
        /// which is most of them.
        static let stripEncryptedExtensions = "--strip-encrypted-extensions"
        /// Refuse the partial dump. Dropping undumpable extensions is what a
        /// dump does by default, so this is the only one of the pair worth
        /// sending — passing the positive form asks for what already happens.
        static let keepEncryptedExtensions = "--keep-encrypted-extensions"
        static let decryptBackend = "--decrypt-backend"
        static let decryptCheck = "--decrypt-check"
        /// Show the decrypt backend's own output as it runs.
        ///
        /// The backends are the opaque part of a dump: bagbak and
        /// frida-ios-dump each take a minute or more and, from outside, a run
        /// that has pulled two hundred files looks exactly like one that is
        /// wedged. This turns their narration into `log` events.
        static let verbose = "--verbose"
        static let deleteTweak = "--delete-tweak"

        // RepoStore
        static let repos = "--repos"
        static let addRepo = "--add-repo"
        static let removeRepo = "--remove-repo"
        static let repoPackages = "--repo-packages"
        static let refresh = "--refresh"
        static let fetch = "--fetch"
        static let fetchSha = "--fetch-sha"

        // CanisterStore
        static let search = "--search"

        // BuildsStore / BuildLogView / ReinstallSheet / AppDetailView
        static let builds = "--builds"
        static let buildsApp = "--builds-app"
        static let buildsLimit = "--builds-limit"
        static let buildLog = "--build-log"
        static let revealBuild = "--reveal-build"
        static let reinstallBuild = "--reinstall-build"

        // DevicesStore
        static let devices = "--devices"
        /// Pairs with `--devices`: identity and connection only, none of the
        /// lockdown fields a full `device` event carries. Used for the fast
        /// poll that keeps the device list current without paying for a
        /// capacity/battery query on every tick.
        static let brief = "--brief"
        /// A device's capacity and battery, as a `device-info` event.
        ///
        /// **One device, always.** `--device` names it; without that the CLI
        /// falls back to the configured `default_device_udid`, and with neither
        /// it exits fatally (`ConfigError: no device given and no default
        /// device configured`) rather than picking a phone or querying them
        /// all. That is deliberate and unlike its neighbours — `--device-apps`
        /// and four other verbs do let usbmux choose the first device it
        /// enumerates: `device-info`'s `udid` field is echoed back from the
        /// request rather than read off the lockdown session, so with no target
        /// there is nothing truthful to put in it, and `DeviceInfoStore` is
        /// keyed on exactly that field. See `cli._do_device_info`.
        ///
        /// This doc used to say the opposite — "without it, every enumerated
        /// device is queried" — which is the `--artwork` contract below, not
        /// this one. A caller that believed it would ship a `--device-info` run
        /// with no `--device` and get a fatal exit for a pane that only ever
        /// asks about one phone.
        static let deviceInfo = "--device-info"

        // LiveContainerStore — LiveContainer itself, per device. `--device`
        // (declared below) names the device on every one of them.
        static let lcStatus = "--lc-status"
        static let lcInstall = "--lc-install"
        static let lcPushCert = "--lc-push-cert"
        static let lcClean = "--lc-clean"
        /// Pairs with `--lc-install`: which instances, as "1,2,3".
        static let instances = "--instances"
        /// Pairs with `--lc-install` / `--lc-status`: `nightly` or `stable`.
        static let channel = "--channel"
        /// Pairs with `--lc-clean`: remove only the certificate — the step right
        /// after Import Certificate, while the IPAs still wait to be imported.
        static let certOnly = "--cert-only"

        // DeviceAppsStore
        /// Pairs with `--device-apps`: resolve each listed app's real icon
        /// through the same cascade `--artwork` uses, and carry it on the
        /// `device-app` event as `icon_path`. Opt-in because the *first*
        /// resolve costs real time per app and most callers of `--device-apps`
        /// don't render icons; once the cache is warm the CLI asks the phone
        /// only about ids it has no believable answer for, which measured 1 of
        /// 67 and turned ~3.3s into ~0.5s.
        static let withIcons = "--with-icons"

        // ArtworkStore
        /// Resolve each configured app's real icon through the CLI's cascade
        /// and emit one `artwork` event per hit. `--app` scopes it to a
        /// single app; without it, every configured app is resolved.
        static let artwork = "--artwork"
        /// Same resolve, but ignore the on-disk cache and re-run the cascade.
        /// `--app` scopes this one too.
        ///
        /// It qualifies `--device-apps --with-icons` as well, where the cache
        /// it bypasses is the same one and the only rung below it is the phone
        /// — that listing serves a believably cached icon without a round trip
        /// (1 of 67 ids needed the phone on the test device), so this is the
        /// only way to pick up a home-screen icon that changed under an
        /// unchanged bundle id. Nothing in the GUI sends it there yet: the
        /// listing is already correct, and a Refresh button that also spent
        /// ~3s re-reading icons would be paying for the rarest change on the
        /// pane.
        static let refreshArtwork = "--refresh-artwork"

        // AppStoreStore — getting an app from the App Store and decrypting it
        // on this Mac (ipatool + UnfairPlay). Signing in and enrolling are
        // never sent from here: they take a password, a 2FA code and root, and
        // stay Terminal commands the Settings pane shows for copying.
        // `--allow-unverified` is deliberately absent — the GUI does not offer
        // staging a decrypt that failed Apple's own page-hash check.
        static let appstoreStatus = "--appstore-status"
        /// Scoped with `--app`; this app only ever asks about one at a time.
        static let appstoreCheck = "--appstore-check"
        /// Requires `--app`. Streams `appstore-stage` as it goes, then
        /// `ipa-provided` exactly as a drop does.
        static let appstoreGet = "--appstore-get"

        // SettingsStore
        static let check = "--check"
        static let setP12Password = "--set-p12-password"
        /// Store a device's root SSH password in the Keychain, keyed by UDID.
        ///
        /// The frida-ios-dump backend pulls the bundle over SSH, and the stock
        /// jailbreak password (`alpine`) is the wrong one on any device whose
        /// root password was changed — which is the correct thing to have done.
        /// The Keychain is what makes it reachable from here at all: a
        /// Dock-launched app inherits the launchd environment, so the
        /// `GRAFT_SSH_PASS` escape hatch a shell has does not exist for the GUI.
        static let setSSHPassword = "--set-ssh-password"

        // BuildOptions (per-build toggles). `BuildOptions.Option.cliArgs`
        // renders these.
        static let dryRun = "--dry-run"
        static let skipInstall = "--skip-install"
        static let skipDecryptCheck = "--skip-decrypt-check"
        static let device = "--device"

        /// Build an artifact that loads on *this Mac* under PlayCover.
        ///
        /// It is not the same artifact a phone gets. The tweak's dylib is
        /// retargeted at a bundled *stub* `Preferences.framework`, because
        /// macOS has no such private framework and iOS has the real one — so
        /// the build the CLI produces here is one only this Mac can run, and
        /// the CLI records its row as `target: "mac"`. `BuildRecord.target`
        /// and `BuildRecord.isForThisMac` are the reading side of that.
        ///
        /// Sits beside the `BuildOptions` toggles above and is now rendered
        /// the same way — `BuildOptions.Option.forPlaycover.cliArgs` — but it
        /// is not a toggle: nothing in the UI leaves it set. `AppDetailView`'s
        /// "Build for this Mac" turns it on in a copy of that pane's options
        /// for one launch. This doc used to say `Option.cliArgs` did not
        /// render it, which was true only until that action existed.
        ///
        /// The CLI refuses it without `--skip-install` — it raises rather
        /// than quietly implying the skip, because installing a Mac build on
        /// the phone that run is aimed at points that tweak at the stub. A
        /// caller sends the pair, and `BuildOptions.activeOptions` is where
        /// this app guarantees it does.
        static let forPlaycover = "--for-playcover"

        /// Every constant declared above.
        ///
        /// Swift can't enumerate the static members of a caseless enum, and
        /// the members have to stay `String` because `BuildOptions.cliArgs`
        /// splices them into a `[String]`. So this list is written out — and
        /// `CLIContractTests.test_every_flag_constant_is_declared_used` parses
        /// the declarations above out of this very file and fails if one of
        /// them is missing here.
        static let all: Set<String> = [
            json,
            list, addApp, addAppManual, name, bundleId,
            displayName, delivery, provideIPA, app, cachedIPA, stagedIPAs, deleteStaged, force, installToMac, attachTweak,
            detachTweak, removeApp, setAppOptions, reconcile, deliveryState,
            preferredVersion,
            settings, setSettings, defaultDevice, archiveKeep,
            signingCert, signingProfile, signingTeamId,
            keepBundleId, noKeepBundleId, stripExtensions, noStripExtensions,
            stripSupportedDevices, noStripSupportedDevices, compressionLevel,
            tweaks, tweakUpdates, updateTweak, deviceApps, decrypt, decryptFrontmost,
            stripEncryptedExtensions, keepEncryptedExtensions, decryptBackend,
            decryptCheck, verbose,
            deleteTweak,
            repos, addRepo, removeRepo, repoPackages, refresh, fetch, fetchSha,
            search,
            builds, buildsApp, buildsLimit, buildLog, revealBuild, reinstallBuild,
            devices, brief, deviceInfo, withIcons,
            lcStatus, lcInstall, lcPushCert, lcClean, instances, channel, certOnly,
            artwork, refreshArtwork,
            appstoreStatus, appstoreCheck, appstoreGet,
            check, setP12Password, setSSHPassword,
            dryRun, skipInstall, skipDecryptCheck, device,
            forPlaycover,
        ]
    }

    /// The values `--decrypt-backend` accepts.
    ///
    /// Names, not flags, so they live beside the flag rather than in it. Both
    /// are spelled by the CLI (`decrypt_runner.BAGBAK` / `.FRIDA`) and a typo
    /// here would be a backend the CLI rejects at parse time.
    ///
    /// They are not ranked. frida-ios-dump attaches to a running app and gets
    /// the main binary reliably, but cannot touch app extensions — they run in
    /// their own processes. bagbak spawns the app and dumps extensions too,
    /// which is both why it is the only one that can finish an extension-bearing
    /// app and why it is the flakier one.
    /// `source` on a `log` event the CLI wrote about itself, as opposed to one
    /// it relayed from a backend. The two are shown differently: ours are few
    /// and load-bearing ("I switched tools, here is why"), theirs are hundreds
    /// of per-file progress lines.
    static let selfLogSource = "graft"

    enum DecryptBackend {
        static let bagbak = "bagbak"
        static let frida = "frida-ios-dump"
    }


    /// `stage` event names the GUI reacts to specifically, rather than as
    /// "some stage ran".
    ///
    /// This used to carry a warning that it was the one part of the contract
    /// *not* machine-checked — `--describe` reported flags, events and
    /// app-option fields and said nothing about stage names, so a rename on
    /// the CLI side failed silently here. It reports `stages` now, and
    /// `test_every_stage_the_app_depends_on_is_still_emitted_by_the_cli`
    /// checks this enum against it.
    ///
    /// Note what is *not* asserted, and why this is not the partition
    /// `eventsHandled` gets. Every event must be decoded or excused because an
    /// undecoded one is dropped by `try?` and its feature silently ceases to
    /// exist. Stages have no such failure mode: `BuildLogView`, `AppDetailView`
    /// and `ReinstallSheet` render whatever `name` arrives, verbatim, so a
    /// stage added to the CLI tomorrow shows up in the transcript correctly
    /// with no Swift change at all. Only the names read for *meaning* can
    /// break, and only in one direction — a name this app waits for that the
    /// CLI no longer emits. So the assertion is a subset, and this enum stays
    /// as short as the dependencies are. Listing all seven stages here would
    /// be a maintenance obligation that prevents no defect, and worse: it
    /// would make membership mean "exists" rather than "something depends on
    /// it", which is the only claim that makes the subset check worth running.
    ///
    /// The rule for adding one: name it here when the GUI branches on it, not
    /// when it renders it.
    enum Stage {
        /// Stage 2.5, the cryptid-0 decryption check.
        ///
        /// Its `status: "end"` is the only signal that the check reached a
        /// *verdict*. The CLI emits `encrypted-binaries` when it finds
        /// something and nothing at all when it doesn't, so "the check ran and
        /// the IPA is fine" has no event of its own — it is this stage ending
        /// without one. `ConfigStore.note(_:forBuildOf:)` keys the clearing of
        /// a standing finding off exactly that, which is what stops a build
        /// that died before stage 2.5 from erasing it.
        static let decryptionCheck = "verify"

        /// The two stages where stopping the build costs something on the
        /// *device* rather than only locally.
        ///
        /// `lc-push` streams the finished .ipa into LiveContainer's Documents/
        /// and `install` hands a sideload to the installation proxy. The CLI
        /// installs no SIGTERM handler, so a kill lands on Python's default
        /// disposition: the process dies where it stands, `_build_one`'s
        /// `finally: shutil.rmtree(tmp_root)` never runs, and whatever had
        /// been written to the phone stays written — a partial file the user
        /// is one tap away from importing.
        ///
        /// The GUI branches on both to word its cancel confirmation, which is
        /// exactly the membership rule this enum documents: named here because
        /// something depends on them, not because they exist.
        static let liveContainerPush = "lc-push"
        static let sideloadInstall = "install"



        /// Every constant declared above, for the same reason `Flag.all`
        /// exists: Swift cannot enumerate a caseless enum's static members, so
        /// the set the CLI gets checked against is written out by hand and
        /// `test_every_stage_constant_is_declared_used` parses the
        /// declarations out of this file to prove none was left off.
        ///
        /// Without that, adding a second stage dependency and forgetting this
        /// line would leave it in exactly the unchecked state this whole enum
        /// was just rescued from — one constant guarded, the next not, and no
        /// visible difference between them.
        /// `DeviceRisk` owns which stages carry device risk and what to say
        /// about them, so the set is derived from it rather than restated —
        /// this was briefly a separate `costlyToCancel` constant whose only
        /// remaining consumer was this line.
        static var dependedOn: Set<String> {
            Set(DeviceRisk.allCases.map(\.stageName)).union([decryptionCheck])
        }
    }

    /// Exit statuses the GUI reacts to specifically, rather than as "the CLI
    /// failed". `CLIError.exitCode` is how a caller reads one back.
    enum Exit {
        /// `--delete-tweak` refused: the tweak is still attached to an app,
        /// and stderr names which. Distinguishing this from a generic failure
        /// is what lets the Tweaks view offer a detach instead of an alert.
        static let tweakInUse: Int32 = 16

        /// `--delete-staged` refused: the file is the last staged IPA some
        /// configured app can build from, and the `staged-ipa-in-use` event
        /// names which apps. `Stage`'s own rule above ("name it here when the
        /// GUI branches on it, not when it renders it") applies just as well
        /// here: the Cache pane's delete confirmation is exactly that branch.
        static let stagedIPAInUse: Int32 = 17
    }

    /// Every flag the app passes to the CLI.
    static let flagsUsed: Set<String> = Flag.all

    /// Every `"type"` string `BuildEvent.decode(line:)` has a case for,
    /// excluding the `default:` fallback that produces `.unknown`.
    ///
    /// Duplicated from that switch by hand — Swift can't enumerate a switch's
    /// patterns, and restructuring `decode` into a table just to enable this
    /// would trade a readable decoder for a lookup indirection. The copy is
    /// checked instead:
    /// `CLIContractTests.test_every_event_decode_has_a_case_for_is_declared_handled`
    /// parses the `case "…":` lines out of `decode` and asserts this set is
    /// exactly them. Without it, every assertion below resolves "the GUI
    /// handles this" to a literal nothing compared against the decoder — so an
    /// event registered here and in the CLI, but missing its `case`, passed all
    /// of them while decoding as `.unknown`.
    ///
    /// Checked in *both* directions, which is the part this started without.
    /// `eventsHandled ⊆ CLI` catches an event the GUI still decodes but the
    /// CLI no longer emits — a feature that silently displays nothing. The
    /// direction it was missing, `CLI ⊆ eventsHandled ∪ cliOnlyEvents`, is the
    /// one that shipped a bug: `device-error` was added on the CLI side and
    /// routed nowhere, so a phone usbmux could see but not describe decoded as
    /// `.unknown`, was filtered out of `DevicesStore.load`, and the Devices tab
    /// said "No devices connected".
    ///
    // MARK: eventsHandled — one quoted event per line; the Python side parses
    // this literal. Keep the formatting regular.
    static let eventsHandled: Set<String> = [
        "app",
        "tweak",
        "tweak-update",
        "tweak-updates-end",
        "tweak-updated",
        "device-app",
        "device-apps-end",
        "decrypt-start",
        "decrypt-end",
        "decrypt-check",
        "build-start",
        "log",
        "build-end",
        "stage",
        "build-record",
        "build-pruned",
        "prune-end",
        "encrypted-binaries",
        "livecontainer-choice",
        "device",
        "device-brief",
        "device-error",
        "device-info",
        "lc-status",
        "lc-install-progress",
        "lc-install-waiting",
        "lc-installed",
        "lc-cert-pushed",
        "lc-cleaned",
        "devices-end",
        "preflight-check",
        "preflight-end",
        "canister-result",
        "canister-search-end",
        "canister-fetch-start",
        "canister-fetch-end",
        "repo-added",
        "repo-removed",
        "repo",
        "repo-refresh-start",
        "repo-package",
        "repo-packages-end",
        "repos-end",
        "app-added",
        "app-removed",
        "app-updated",
        "artwork",
        "delivery-state",
        "cached-ipa",
        "ipa-provided",
        "staged-ipa",
        "staged-ipas-end",
        "staged-ipa-deleted",
        "staged-ipa-in-use",
        "handed-to-playcover",
        "tweak-attached",
        "tweak-detached",
        "settings",
        "settings-updated",
        "tweak-deleted",
        "tweak-in-use",
        "appstore-status",
        "appstore-version",
        "appstore-stage",
        "appstore-decrypted",
    ]

    /// Events the CLI emits that the GUI deliberately does *not* decode, each
    /// with the reason it needs no surface.
    ///
    /// The point of this set is that it must be written down. A new CLI event
    /// fails `test_every_event_the_cli_emits_is_routed_or_declared_cli_only`
    /// until somebody puts it on one side or the other — decoded, or excused
    /// here in a sentence. "Nobody noticed" stops being an available outcome,
    /// which is exactly how `device-error` reached a release.
    ///
    /// Membership here is not a licence to ignore an event forever: it is a
    /// claim that no user-visible state depends on it. If that stops being
    /// true, move it to `eventsHandled` and give it a case.
    ///
    // MARK: cliOnlyEvents — same format as eventsHandled, same parser.
    static let cliOnlyEvents: Set<String> = [
        // `--describe`'s own single line: the schema, not a fact about a
        // build, a device or an app. It is emitted only when something asks
        // the CLI to describe itself, which in this codebase is
        // `CLIContractTests` — and that test decodes it with its own
        // `Contract` struct rather than through `BuildEvent`. Giving it a
        // `BuildEvent` case would put the vocabulary listing into whatever
        // panel happened to be reading the stream.
        "cli-contract",
    ]

    /// The field names `ConfigStore.updateAppOptions` puts in its
    /// `requestedFields` set and reconciles against the `changed` array of an
    /// `app-updated` event.
    ///
    /// These are the CLI's own config-key spellings, not the Swift parameter
    /// names. A mismatch is not cosmetic: `updateAppOptions` throws
    /// "graft didn't apply X" when a field it asked for is absent from
    /// `changed`, so a stale spelling reports failure for a write that
    /// actually landed on disk.
    enum AppOptionField: String, CaseIterable {
        case preferredVersion = "preferred_version"
        case displayName = "display_name"
        case delivery
        case keepBundleId = "keep_bundle_id"
        case stripExtensions = "strip_extensions"
        case stripSupportedDevices = "strip_supported_devices"
        case compressionLevel = "compression_level"
    }

    static let appOptionFieldsSent: Set<String> = Set(
        AppOptionField.allCases.map(\.rawValue)
    )

    /// The setting names `SettingsStore.updateSettings` puts in its requested
    /// set and reconciles against a `settings-updated` event's `changed` array
    /// — and, via `CodingKey`, the keys `SettingsRecord` decodes.
    ///
    /// The CLI's own key spellings, exactly as `AppOptionField` holds them and
    /// for the same reason: a stale spelling here reports failure for a write
    /// that landed on disk. `SettingsRecord.CodingKeys` used to write the same
    /// five out a second time, and only this copy is checked against the CLI
    /// (`test_every_setting_the_app_writes_is_declared_by_the_cli`) — so a
    /// rename that reached only the other one decoded nothing and blanked the
    /// pane, with every test still green.
    enum SettingField: String, CaseIterable, CodingKey {
        case defaultDeviceUDID = "default_device_udid"
        case archiveKeep = "archive_keep"
        case signingCert = "signing_cert"
        case signingProfile = "signing_profile"
        case signingTeamId = "signing_team_id"
    }
}
