import Foundation

/// The vocabulary this app shares with the `graft` Python CLI.
///
/// The two sides talk over a process boundary with no schema between them:
/// Swift builds argument vectors out of string literals, the CLI answers with
/// JSON-Lines events, and `String.buildEvents()` decodes each line with `try?`
/// — so a line the switch in `BuildEvent.decode` doesn't recognize is silently
/// dropped and the UI just shows nothing. Flags fail even more quietly: an
/// unrecognized option makes Click abort with a usage error the GUI surfaces
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
        /// With `--add-app-manual`: the App Store a Search result came from, so
        /// `--appstore-check` and `--artwork` look the app up there.
        static let storeCountry = "--store-country"
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
        /// The App Store Connect key, without `--setup-portal`'s prompts:
        /// checked against App Store Connect first, written only if that
        /// worked. Settings' "App Store Connect" section sends it with the
        /// three companions below, and `--clear-portal` to remove it.
        static let setPortal = "--set-portal"
        static let clearPortal = "--clear-portal"
        static let ascKeyId = "--asc-key-id"
        static let ascIssuerId = "--asc-issuer-id"
        static let ascKeyFile = "--asc-key-file"
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
        /// The app's LiveContainer pin (`lc_bundle_id:`), and clearing it
        /// (C-LCPIN). The Edit sheet's LiveContainer picker sends one or the
        /// other, never both.
        static let lcBundleId = "--lc-bundle-id"
        static let noLcBundleId = "--no-lc-bundle-id"
        /// The app's `auto_profile:` — make its provisioning profile through
        /// App Store Connect — on or off (C-AUTOPROFILE). The Edit sheet's
        /// toggle sends one or the other.
        static let autoProfile = "--auto-profile"
        static let noAutoProfile = "--no-auto-profile"

        // TweaksStore
        static let tweaks = "--tweaks"
        static let tweakUpdates = "--tweak-updates"
        static let updateTweak = "--update-tweak"
        // AppUpdatesStore: `graft --update` (Plan D). `updateAll` because
        // `all` is this enum's own set.
        static let update = "--update"
        static let updateAll = "--all"
        static let plan = "--plan"
        static let quick = "--quick"
        static let withAffected = "--with-affected"
        /// `--drop-update-target APP --device UDID`: Update stops installing
        /// APP on that device until it's delivered there again (a device
        /// that's gone). Emits `update-target-dropped`.
        static let dropUpdateTarget = "--drop-update-target"
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

        // UpdateStore
        /// Is a newer Graft published? One request to GitHub, made by the CLI
        /// like every other: the app makes no connections of its own.
        static let checkUpdate = "--check-update"
        /// With `--check-update` or `--download-update`: the version to
        /// compare against, the app's own.
        static let currentVersion = "--current-version"
        /// Download the newer release's zip of this build (Graft.zip, or
        /// Graft-Intel.zip for the Intel build) into Graft's cache and check
        /// it (signature, team and bundle id, the build, notarization,
        /// GitHub's SHA-256). Installs nothing.
        static let downloadUpdate = "--download-update"
        /// Start the helper that installs a checked download once this app
        /// has quit. Takes the download's path.
        static let installUpdate = "--install-update"
        /// With either: the Graft.app to replace, this one.
        static let appPath = "--app-path"
        /// With `--install-update`: this app's process id, which the helper
        /// waits on.
        static let waitPid = "--wait-pid"
        /// With `--install-update`: open Graft again once it's installed.
        static let relaunch = "--relaunch"

        // RemoveDataStore
        /// What Graft keeps outside Graft.app, one `data-item` each; with
        /// `items`, removing the ones named. Help › Remove Graft's Data….
        static let removeData = "--remove-data"
        /// With `--remove-data`: the ids to remove, comma-separated. Never
        /// `all`: the app sends exactly what is checked.
        static let items = "--items"

        // BuildsStore / BuildLogView / ReinstallSheet / AppDetailView
        static let builds = "--builds"
        static let buildsApp = "--builds-app"
        static let buildsLimit = "--builds-limit"
        static let buildLog = "--build-log"
        static let revealBuild = "--reveal-build"
        static let reinstallBuild = "--reinstall-build"
        /// Builds' "Clean up old builds…": the retention sweep a build runs,
        /// on demand, at config.yaml's `archive_keep`. First with `--dry-run`
        /// (declared below) to say what would go, then for real.
        static let pruneBuilds = "--prune-builds"

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

        // AppStoreStore and AppStoreAccountStore. Getting an app from the App
        // Store and decrypting it on this Mac (ipatool + UnfairPlay). Since
        // 2.0, signing in, enrolling and claiming free apps too. The Apple ID,
        // the password and the code only ever go to the CLI's stdin.
        static let appstoreStatus = "--appstore-status"
        /// Sign ipatool in. The Apple ID and the password go on stdin, then
        /// the 2FA code if the CLI emits `appstore-login {state: "needs-code"}`
        /// (`AppStoreAccountStore`). None of them is ever an argument.
        static let appstoreLogin = "--appstore-login"
        /// With `--appstore-login`: the ipatool the sign-in sheet named, from
        /// the status it read as it opened (`appstore-status`'s
        /// `ipatool.path`). Sent on every sign-in. If ipatool now resolves
        /// anywhere else, the CLI reads nothing from stdin and fails with
        /// `appstore-login {state: "failed", reason: "ipatool-changed"}`, a
        /// message naming both paths (fix-check #10).
        static let expectIpatool = "--expect-ipatool"
        static let appstoreLogout = "--appstore-logout"
        /// Requires `--app`. The $0 "Get" for an app the CLI's iTunes lookup
        /// says is free; it refuses anything else. Sent only from Get (free).
        static let appstoreClaim = "--appstore-claim"
        /// With `--appstore-claim`: the Apple ID the Get (free) confirmation
        /// named. ipatool buys for whoever is signed in when the CLI runs, so
        /// one signed in since, in Terminal, is refused before anything is
        /// bought: `appstore-refused {reason: "account-changed", email}` (the
        /// try14 delta audit's D14-APP-1 and F5). Not sent when the
        /// confirmation named nobody (signed in, no email).
        static let expectEmail = "--expect-email"
        /// The installed apps that can enroll this Mac (`AppStoreEnrollSheet`).
        static let appstoreEnrollCandidates = "--appstore-enroll-candidates"
        /// With `--app PATH`: the script root runs to enroll this Mac. The app
        /// runs it behind the administrator prompt and adds nothing (`AdminShell`).
        static let appstoreEnrollCommand = "--appstore-enroll-command"
        /// With `--appstore-enroll-command`: hand over a script whose UnfairPlay
        /// nothing checks (an ad hoc Graft.app, or an UnfairPlay that isn't
        /// Graft.app's own). Without it the CLI refuses with `appstore-refused
        /// {reason: "unverified-helper"}`. Sent only from the enroll sheet's
        /// "Run Unchecked Copy as Root…", after that refusal was shown.
        static let allowUnverifiedHelper = "--allow-unverified-helper"
        /// With the folder that script printed: move the model in, as the user.
        static let appstoreEnrollFinish = "--appstore-enroll-finish"
        /// Scoped with `--app`; this app only ever asks about one at a time.
        static let appstoreCheck = "--appstore-check"
        /// Requires `--app`. Streams `appstore-stage` as it goes, then
        /// `ipa-provided` exactly as a drop does; or, when the App Store's
        /// current version is already staged, only `appstore-already-staged`
        /// (see `again`).
        static let appstoreGet = "--appstore-get"
        /// With `--appstore-get` or `--provide-ipa`: stage a decrypt UnfairPlay
        /// could not verify against Apple's page hashes. Sent only from "Use it
        /// anyway…", after a confirmation, to re-run a get or drop the CLI just
        /// refused with `appstore-refused` (C-UNVERIFIED). Never on a first try.
        static let allowUnverified = "--allow-unverified"
        /// With `--appstore-get`: download, decrypt and stage the App Store's
        /// current version even when it is already staged. Without it, a get
        /// of a version already staged downloads nothing and answers
        /// `appstore-already-staged`. Sent only from Get Again, after a
        /// confirmation, and by the re-runs that answer a Get Again ("Use it
        /// anyway…", the get after Get (free)).
        static let again = "--again"
        /// With `--appstore-get`: get the version this id names (one
        /// `--appstore-versions` listed) instead of the latest, and hold the
        /// app at it: the CLI pins its `preferred_version` and answers
        /// `appstore-hold`. A version already staged is held without a
        /// download (`appstore-already-staged`). Sent from the Get an older
        /// version… sheet, and by the "Use it anyway…" re-run of one.
        static let versionId = "--version-id"
        /// Requires `--app`. Lists the versions the App Store serves this
        /// Apple ID, newest first: `appstore-release` each, then
        /// `appstore-releases-end`. Reads the details of a page of them
        /// (the CLI's default, 10) and remembers them (`AppStoreVersionsStore`).
        static let appstoreVersions = "--appstore-versions"
        /// With `--appstore-versions`: where the page whose details are read
        /// starts, counted from the newest. The sheet's Show More sends the
        /// end of the last page. Never `--limit`: the CLI's page is the sheet's.
        static let offset = "--offset"
        /// Search the App Store for iPhone apps (`AppStoreSearchStore`). Sent
        /// attached, `--appstore-search=<term>`, so a term that starts with a
        /// dash is still the term.
        static let appstoreSearch = "--appstore-search"
        /// Pairs with `--appstore-search`: which country's store, the one the
        /// sheet's store picker holds (`Storefronts`), which starts at this
        /// Mac's region. Never `--limit`: the CLI's default of 25 is what the
        /// sheet shows.
        static let country = "--country"

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

        // AccountSetupStore — `--init` without its questions: the first-run
        // screen and Settings › Account. The .p12 password goes on stdin,
        // never in argv.
        static let initSetup = "--init"
        static let account = "--account"
        static let p12 = "--p12"
        static let profile = "--profile"
        /// With `--account paid` on a paid config: swap in a new certificate
        /// and profile (Settings › Account › Replace certificate and profile).
        static let replace = "--replace"

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

        /// Build the app as far as injecting its tweaks, then write that IPA,
        /// unsigned, to the path that follows — for an on-device signer or
        /// TrollStore. The CLI never signs, archives or touches a device for
        /// it, whatever the app's delivery mode, and writes no history row.
        ///
        /// A verb, not a toggle: `AppDetailView`'s Export IPA… sets it on a
        /// copy of the pane's options for one launch, as Build for this Mac
        /// does `forPlaycover`, and `BuildOptions.Option.exportIPA` renders
        /// it. The CLI refuses `--device`, `--skip-install` and
        /// `--for-playcover` beside it, so `BuildOptions.activeOptions` never
        /// sends them with it.
        static let exportIPA = "--export-ipa"
        /// With `--export-ipa`: replace a file already at its path. The CLI
        /// refuses one without it. The app sends it only for a dry run's
        /// preview of a file the user chose to replace in the Save panel: a
        /// real export writes into a new staging folder, and the app replaces
        /// the file itself (`ExportStaging`).
        static let overwrite = "--overwrite"

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
            displayName, delivery, storeCountry, provideIPA, app, cachedIPA, stagedIPAs, deleteStaged, force, installToMac, attachTweak,
            detachTweak, removeApp, setAppOptions, reconcile, deliveryState,
            preferredVersion,
            settings, setSettings, defaultDevice, archiveKeep,
            signingCert, signingProfile, signingTeamId,
            setPortal, clearPortal, ascKeyId, ascIssuerId, ascKeyFile,
            keepBundleId, noKeepBundleId, stripExtensions, noStripExtensions,
            stripSupportedDevices, noStripSupportedDevices, compressionLevel,
            lcBundleId, noLcBundleId, autoProfile, noAutoProfile,
            tweaks, tweakUpdates, updateTweak, update, updateAll, plan, quick, withAffected, dropUpdateTarget,
            deviceApps, decrypt, decryptFrontmost,
            stripEncryptedExtensions, keepEncryptedExtensions, decryptBackend,
            decryptCheck, verbose,
            deleteTweak,
            repos, addRepo, removeRepo, repoPackages, refresh, fetch, fetchSha,
            search,
            checkUpdate, currentVersion, downloadUpdate, installUpdate, appPath, waitPid, relaunch,
            removeData, items,
            builds, buildsApp, buildsLimit, buildLog, revealBuild, reinstallBuild, pruneBuilds,
            devices, brief, deviceInfo, withIcons,
            lcStatus, lcInstall, lcPushCert, lcClean, instances, channel, certOnly,
            artwork, refreshArtwork,
            appstoreStatus, appstoreCheck, appstoreGet, allowUnverified, again, versionId,
            appstoreVersions, offset, appstoreSearch, country,
            appstoreLogin, expectIpatool, appstoreLogout, appstoreClaim, expectEmail, appstoreEnrollCandidates, appstoreEnrollCommand,
            appstoreEnrollFinish, allowUnverifiedHelper,
            check, setP12Password, setSSHPassword,
            initSetup, account, p12, profile, replace,
            dryRun, skipInstall, skipDecryptCheck, device,
            forPlaycover, exportIPA, overwrite,
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

    /// `source` on the `log` events the CLI writes *before* the verb runs:
    /// a data directory moved from its pre-rename name, an environment
    /// variable still set by its old name (C-NOTICE). They describe the
    /// machine, not the verb, so no store reads them as that verb's result —
    /// a device check whose only "warning" is a renamed variable did check
    /// the device. A build transcript still shows them, as the context line
    /// they are.
    static let startupLogSource = "startup"

    /// `source` on the `log` events `_warn` writes: a warning about the verb
    /// that ran, which carried on anyway. The App Store path reads these off a
    /// get or drop so the line the CLI printed for an unverified decrypt is
    /// still on screen after "Use it anyway…".
    static let warningLogSource = "warning"

    /// `source` on the `log` events the CLI writes while it waits on the phone
    /// itself: "Unlock the phone and tap Trust on the 'Trust This Computer?'
    /// prompt (waiting up to 60 s)". Information, for while the wait lasts. It
    /// is never a verb's result: a check that went on to succeed after the
    /// user tapped Trust succeeded (fix-check #19, #21).
    static let deviceLogSource = "device"

    /// `source` on the `log` events `--prune-builds` writes for an archive it
    /// could not move to the Trash, one per file: "<name>: <why>".
    static let retentionLogSource = "retention"

    /// `appstore-refused`'s `reason` values the app branches on (C-UNVERIFIED).
    /// Pinned from the Python side: tests/test_cli_appstore.py reads this
    /// literal against `appstore.REFUSED_UNVERIFIED`.
    enum AppStoreRefusal {
        /// UnfairPlay decrypted it but couldn't verify it against Apple's page
        /// hashes. The app pane offers "Use it anyway…" for this one.
        static let unverified = "unverified"
        /// The Apple ID doesn't own the app. The App Store row offers Get (free)
        /// when the refusal's `price` is 0, and Open in App Store otherwise.
        static let needsLicense = "needs-license"
        /// `--appstore-claim` on an app that isn't free.
        static let paid = "paid"
        /// `--appstore-claim --expect-email` while ipatool is signed in as
        /// another Apple ID, named by the event's `email`. Nothing was bought.
        static let accountChanged = "account-changed"
        /// `--appstore-enroll-command` without `--allow-unverified-helper`,
        /// for a script whose UnfairPlay nothing would check before root runs
        /// it. The event carries `kind` (`UnverifiedHelperKind`), `helper` and
        /// the CLI's `message`.
        static let unverifiedHelper = "unverified-helper"
    }

    /// `appstore-status`'s `ipatool.source`: where the ipatool that gets the
    /// Apple ID and password was found (`graft_ipa/tools.py` `SOURCE_*`).
    enum ToolSource {
        /// Graft.app's own copy.
        static let bundled = "bundled"
        /// config.yaml's `appstore.ipatool`, which comes before the bundled one.
        static let config = "config"
        /// The old default place, ~/.local/bin, in a Graft with none of its own.
        static let defaultPath = "default"
        /// Found on PATH, in a Graft with none of its own.
        static let path = "PATH"
    }

    /// The enrollment script's own exit statuses (`graft_ipa/enrollment.py`
    /// `EXIT_*`), as AppleScript reports them: the `do shell script` error
    /// number. 67, 68 and 69 all mean the trap removed root's folder and
    /// nothing was changed.
    enum EnrolExit {
        static let notInApplications = 64
        static let notRoots = 65
        static let writable = 66
        /// The app created the command's `cancel_file`.
        static let stopped = 67
        /// The run reached `time_limit_seconds`.
        static let timedOut = 68
        /// Neither folder UnfairPlay reads this Mac's FairPlay state from
        /// holds an `SC Info.sidb`, so root stopped before it copied
        /// anything. Opening the picked app once writes it again.
        static let noFairPlayState = 69
    }

    /// `kind` on an `unverified-helper` refusal, and `unverified_kind` on the
    /// `appstore-enroll-command` that follows `--allow-unverified-helper`.
    enum UnverifiedHelperKind {
        /// This Graft.app has no team signature to check UnfairPlay against.
        static let adHoc = "ad-hoc"
        /// The CLI isn't Graft.app's own (a Debug build running the repo's
        /// CLI), so the UnfairPlay is someone's own build. A Release build
        /// refuses it outright (`AppStoreAccountStore.mayRunUnchecked`).
        static let notBundled = "not-bundled"
    }

    /// `appstore-login`'s `state` values (`graft_ipa/appstore_login.py`
    /// `STATE_*`), pinned from the Python side by tests/test_appstore_login.py.
    enum AppStoreLogin {
        static let preparing = "preparing"
        static let waiting = "waiting"
        static let needsCode = "needs-code"
        static let verifying = "verifying"
        static let signedIn = "signed-in"
        static let failed = "failed"
    }

    /// `appstore-status`'s `ipatool.unknown_reason` values the app words
    /// itself when the CLI sends no `unknown_message` (`graft_ipa/ipatool.py`
    /// `UNKNOWN_*`). Any other reason, "keychain-locked" included, shows the
    /// CLI's `unknown_message`, or the reason itself.
    enum UnknownSignIn {
        /// `auth info` hung, crashed or printed no verdict.
        static let noVerdict = "no-verdict"
        /// The login keychain didn't answer when Graft asked about ipatool's items.
        static let keychainNoAnswer = "keychain-no-answer"
        /// Graft.app's own ipatool made this sign-in, and the ipatool status
        /// ran (one set in config.yaml, or found on PATH) can't read it without
        /// Keychain dialogs, so the CLI didn't ask it. Sign In… gives that
        /// ipatool its own sign-in (the CLI clears Graft's items first).
        static let graftIpatoolSignin = "graft-ipatool-signin"
    }

    /// Settings › App Store's button labels that the CLI's hints name, so a
    /// hint and the button it sends the user to can be pinned to one spelling
    /// (the fix-check's #9 and #13: a hint naming a button that isn't on
    /// screen). Each ends in the ellipsis character, never three full stops.
    enum SettingsLabel {
        /// Beside the ipatool row when nobody is signed in, or only Graft
        /// 1.x's ipatool is.
        static let signIn = "Sign In…"
        /// Beside Sign Out while signed in: the same sheet, for a sign-in
        /// that has expired but still reads as signed in.
        static let signInAgain = "Sign In Again…"
        /// Beside the UnfairPlay row with no model this user can use.
        static let setUpDecryption = "Set Up Decryption…"
        /// Beside the UnfairPlay row on an enrolled Mac.
        static let setUpAgain = "Set Up Again…"
    }

    /// `appstore-search-failed`'s `reason` values the app branches on. Pinned
    /// from the Python side: tests/test_itunes_search.py reads these literals
    /// against `itunes.OFFLINE` and `itunes.RATE_LIMITED`.
    enum AppStoreSearchFailure {
        /// itunes.apple.com couldn't be reached. Add App's Search shows its own
        /// offline state for this, and the CLI's message for every other reason
        /// but `rateLimited`.
        static let offline = "offline"
        /// Apple answered 403, 429 or 503: its per-IP limit on the Search API.
        /// Search shows `AppStoreSearchRateLimited.message` for it.
        static let rateLimited = "rate-limited"
    }

    enum DecryptBackend {
        static let bagbak = "bagbak"
        static let frida = "frida-ios-dump"
    }

    /// `key` values on `--decrypt-check`'s checks that the GUI branches on
    /// (`decrypt_ready.check`), under `Stage`'s rule: named here when
    /// something depends on one, not because it exists.
    enum DecryptCheck {
        /// SSH login to the phone — the check every dump backend needs, and so
        /// the one whose failure means "this phone cannot be dumped from here"
        /// (`DeviceDecryptSection.isUnavailable`). frida is no such signal:
        /// it answers on stock iOS 17+ phones too.
        static let ssh = "ssh"

        /// The `ssh` check's `reason` (`decrypt_ready.SSH_*`). Only
        /// `noServer` means the phone cannot be dumped from here; the other
        /// two are a jailbroken phone with something fixable in the way.
        /// Pinned from both sides: tests/test_decrypt_check_keys.py reads
        /// these literals, and `DecryptCheckContractTests` reads the Python.
        enum SSHReason {
            /// Nothing answered on the phone's port 22: no jailbreak, or
            /// OpenSSH stopped.
            static let noServer = "no_server"
            /// This Mac has no `iproxy` (libimobiledevice).
            static let noIproxy = "no_iproxy"
            /// OpenSSH answered and refused the stored root password.
            static let loginRejected = "login_rejected"
        }

        /// The `ssh` check's detail for `noServer`, from a CLI older than
        /// `reason`. The fallback only; the structured field decides when
        /// it is there.
        static let noServerDetail = "no SSH server answered"
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

    /// `graft --update`'s vocabulary (`update_plan` and `update_run` in the
    /// CLI): the steps `update-step` and `update-app-end` name, a step's
    /// status, a plan's readiness, and the skip reasons the app words a line
    /// of its own for. Pinned against the Python constants by
    /// tests/test_update_plan.py. The deferred steps `update-stopping` names
    /// are `Stage.liveContainerPush`, `Stage.sideloadInstall` and
    /// `stepTweaks`.
    enum Update {
        static let stepTweaks = "tweaks"
        static let stepGet = "get"
        static let stepBuild = "build"
        /// One device's install, once per target (`update_plan.STEP_INSTALL`,
        /// spec §11.3): `start` then `end`, or `skipped` with why. The first
        /// runs inside the build.
        static let stepInstall = "install"
        static let statusStart = "start"
        static let statusEnd = "end"
        static let statusSkipped = "skipped"
        static let readinessChecked = "checked"
        static let readinessLikely = "likely"
        static let needsAppleSilicon = "needs Apple silicon"
        static let notInstalledYet = "not installed yet"
        /// `update-app-end`'s `error` for an app a Stop kept from starting
        /// (`update_run.NOT_STARTED`).
        static let notStarted = "not started"
        /// How the reason for a version the Apple ID was refused begins.
        static let refusedPrefix = "Your Apple ID doesn't have "
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

        /// `ConfigError`: the config is missing, unreadable or incomplete.
        /// Shared by every config problem — and by Click's own usage errors,
        /// which also exit 2 — so the status alone never decides anything
        /// here; `Message.configNotFound` says which one it was.
        static let configError: Int32 = 2

        /// The app's own launcher, `Contents/Helpers/graft`, started but found
        /// part of Graft.app missing, such as its Python (`bundle/launcher.c`).
        /// A shell exits 126 for a command it can't execute as well, so
        /// `Message.bundleDamaged` decides.
        static let bundleDamaged: Int32 = 126
    }

    /// Sentences the CLI prints that the GUI branches on.
    ///
    /// Text, not structure, and kept to the one case where nothing structured
    /// exists: a `--list` with no config.yaml fails before the CLI has an
    /// event stream to report into, so `error:` on stderr is all there is.
    /// `CLIContractTests.test_a_missing_config_is_reported_the_way_the_setup_
    /// screen_expects` runs the installed CLI against an empty home and checks
    /// this prefix, and the Python suite pins the same sentence from its side.
    enum Message {
        /// `config._load_raw`'s message for a config.yaml that does not exist,
        /// up to the path it names — the setup screen's "create your config".
        static let configNotFound = "config file not found at "

        /// What the launcher prints first on stderr with `Exit.bundleDamaged`,
        /// followed by what is missing.
        static let bundleDamaged = "graft: Graft.app is damaged:"
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
        "account",
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
        "device-seen",
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
        "playcover-installed",
        "playcover-install-unconfirmed",
        "ipa-exported",
        "tweak-attached",
        "tweak-detached",
        "settings",
        "settings-updated",
        "portal-set",
        "portal-cleared",
        "tweak-deleted",
        "tweak-in-use",
        "appstore-status",
        "appstore-version",
        "appstore-stage",
        "appstore-decrypted",
        "appstore-already-staged",
        "appstore-refused",
        "appstore-login",
        "appstore-logout",
        "appstore-enroll-candidate",
        "appstore-enroll-candidates-end",
        "appstore-enroll-command",
        "appstore-enrolled",
        "appstore-claimed",
        "appstore-search-result",
        "appstore-search-end",
        "appstore-search-failed",
        "appstore-release",
        "appstore-releases-end",
        "appstore-hold",
        "init-step",
        "init-result",
        "update-check",
        "data-item",
        "data-items-end",
        "data-removed",
        "data-removal-end",
        "update-progress",
        "update-download-end",
        "update-install",
        "update-plan",
        "update-step",
        "update-stopping",
        "update-app-end",
        "update-end",
        "update-target-dropped",
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
    /// Membership here is not a license to ignore an event forever: it is a
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
        /// `--lc-bundle-id` and `--no-lc-bundle-id` both report this one name
        /// in `changed` (C-LCPIN).
        case lcBundleId = "lc_bundle_id"
        /// `--auto-profile` and `--no-auto-profile` both report this
        /// (C-AUTOPROFILE).
        case autoProfile = "auto_profile"
        /// `--store-country`: the App Store the app's lookups ask.
        case storeCountry = "store_country"
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
