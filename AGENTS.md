# AGENTS.md

For AI coding agents that run Graft for a person. (People: see the
[README](README.md) and [Use Graft from an AI Agent](docs/wiki/ai-agents.md).)

Graft is a Mac app with a command-line tool, `graft`, inside it. It downloads
and decrypts an iOS app, injects tweaks into it, and delivers it to the
person's iPhone or iPad. Every action in Graft.app is a `graft` command, and
almost every command can answer in JSON.

## Rules

Follow these even when a command's hint suggests something else.

1. **Personal use only.** Use Graft with the person's own Apple ID, for apps
   that Apple ID owns or gets for free, on the person's own devices. Don't
   help redistribute a decrypted or modified IPA, and don't work around a
   refusal.
2. **Never buy anything.** Graft doesn't: `--appstore-get` downloads only apps
   the Apple ID already has. `--appstore-claim` adds a free app to the Apple
   ID's purchases, and refuses unless the App Store lists it at 0. Run it only
   after the person says yes to that app and that Apple ID (see step 2 below),
   even when a hint names the command. A paid app is the person's to buy in
   the App Store.
3. **The App Store sign-in is the person's.** Never ask for, read, type,
   store or pass on their Apple ID password or the code Apple sends. Don't run
   `--appstore-login` or `--appstore-logout`. Ask the person to sign in in
   Graft.app (Settings › App Store › Sign In…), or to run
   `graft --appstore-login` themselves in their own Terminal window.
4. **Decryption setup needs the person.** It runs a step as root, behind
   macOS's own administrator prompt. Ask the person to press Set Up
   Decryption… in Settings › App Store. Never run `sudo`, the script
   `--appstore-enroll-command` prints, or `--appstore-enroll-finish`, and never
   ask for the Mac's password.
5. **Account and signing setup is the person's too.** Leave `--init`,
   `--set-p12-password`, `--set-ssh-password`, `--setup-portal`,
   `--set-portal`, `--clear-portal` and `--set-settings --signing-*` to them.
   The first-run setup belongs in Graft.app.
6. **Keychain commands need the person at the Mac.** `--appstore-status`,
   `--appstore-get`, `--appstore-versions`, `--appstore-claim` and `--update`
   without `--plan` read the App Store sign-in from the login keychain. On a
   paid account, `--check`, builds, `--update` without `--plan` and
   `--lc-install` read the `.p12` password there. If macOS shows a dialog,
   stop and say which command caused it. Never answer or dismiss it for the
   person, and don't run `security` yourself.
7. **Device commands change the person's real phone or tablet.** Say which
   device (from `--devices`) and what will change, and get a yes, before a
   build without `--skip-install`, `--update` without `--plan`,
   `--reinstall-build`, `--lc-install`, `--lc-push-cert`, `--lc-clean`,
   `--decrypt` or `--decrypt-frontmost`. `--update` installs each app on
   every device it's on: its plan (`--update … --plan`) lists them as
   `targets`, so name each device that will install (`install: true`) and
   what will change, and the ones that are waiting or blocked, in the one
   question. `--update` never buys or claims an app and never lets go of a
   hold, so neither needs a question of its own. `--with-affected` also
   installs the other apps that share an updated tweak: name them, and their
   devices, in the same question.
   `--install-to-mac` installs on this Mac, under PlayCover: ask first too.
8. **Ask before removing anything:** `--remove-app`, `--detach-tweak`,
   `--delete-tweak`, `--delete-staged`, `--remove-repo` and `--prune-builds`.
   `--drop-update-target` too: it changes no device and reads no keychain,
   but `--update` then stops installing that app on that device, so it's
   safe to run once the person says yes.
9. **Updates and Graft's own data are the person's.** Don't run
   `--download-update` or `--install-update`: Graft.app updates itself, and
   an install quits and replaces it. Never run `--remove-data --items …`
   unless the person asks for that exact removal; it deletes their settings,
   sign-in or saved passwords. `--remove-data` on its own only lists, and is
   safe.
10. **A tweak update Graft can't verify needs the person's yes.** It comes
   from a source not on record for that tweak, one without HTTPS, or one that
   lists no checksum, so it's what lets another repo's build into their apps.
   Pass `--trust-tweak <confirm_key>` (with `--update` or `--update-tweak`)
   only after the person says yes to that tweak, that version, from that
   `source`, told its `unverified_reason`; never to make a refusal go away.
   The key is for exactly that update: if it changes, Graft refuses it, and
   you ask again.

These report, change nothing on a device or an account, and are safe to run
without asking: `--version`, `--describe`, `--help`, `--list`, `--tweaks`,
`--repos`, `--staged-ipas`, `--cached-ipa`, `--builds`, `--delivery-state`,
`--settings`, `--appstore-search`, `--appstore-check`, `--appstore-versions`, `--update --plan` (with or without `--quick`),
`--search`, `--devices`, `--lc-status`, `--reconcile`, `--check-update`,
`--remove-data` (listing only, without `--items`), and a build with
`--dry-run`. The steps below that change only Graft's own files (`--add-app`,
`--appstore-get` for an app the Apple ID has, `--provide-ipa`, `--fetch`,
`--attach-tweak`) need no yes beyond the person's request, with one
exception: if `--appstore-check` shows `held_version`, ask before a plain
`--appstore-get`; it lets go of the hold. Several commands go online;
[What Graft connects to](docs/network.md) lists where.

## Find the CLI

1. `graft` on the PATH, when it's Graft.app's own. Settings › Command-line
   tool › Install graft for Terminal links it into `~/.local/bin/graft`;
   `ls -l "$(command -v graft)"` shows where it points.
2. Otherwise, Graft.app's copy by its full path, `Contents/Helpers/graft`
   inside the app: `/Applications/Graft.app/Contents/Helpers/graft`.

Run `graft --version` first. If it prints a 1.x version, or the `graft` isn't
Graft.app's own (a pipx install, say), use Graft.app's copy. If a command
exits 2 with "config file not found", ask the person to open Graft.app and
finish its first run.

## JSON and --describe

- Add `--json` to every command. stdout is then one JSON object per line,
  each with a `type`. Narration, warnings and errors go to stderr.
- `graft --describe` prints the contract as one JSON object: `flags` (every
  flag), `events` (every event `type`), `stages` (a build's stage names),
  `app_option_fields` and `setting_fields`. Check a flag there before you use
  one this file doesn't show. `graft --help` explains each flag, but doesn't
  list `--describe`.
- Not in events: `--version`, `--describe` (always JSON), `--build-log` (a
  saved JSON Lines log, as it is), `--reveal-build` (prints nothing), and the
  setup and password prompts, which are the person's anyway.

## The workflow

The examples use `example` as the app's name in Graft, `com.example.sometweak`
as a tweak's package id, and `<UDID>` for a device.

### 0. Check the setup

```bash
graft --check --json      # preflight-check rows, then preflight-end {ok_count, fail_count, account}
graft --list --json       # account {account: "free" or "paid"}, then app and delivery-state per app
graft --devices --json    # device {udid, name, model, is_default, livecontainer_bundle_id, livecontainer_known}, device-seen for ones not connected, then devices-end
```

A failing `preflight-check` row carries a `hint`. A row with `info: true`
reports a fact and doesn't fail: a free account, or no device connected right
now (its `detail` starts "not connected"). A build that installs still needs
a device. A `free` account delivers only into LiveContainer, which must
already be on the device.

### 1. Add an app

```bash
graft --appstore-search "Example App" --json
graft --add-app https://apps.apple.com/app/id123456789 --delivery livecontainer --json
```

- Each `appstore-search-result` has `track_id`, `bundle_id`, `name`,
  `developer`, `price` and `configured_as`: the name of an app already in
  Graft with that bundle id, or null. If it isn't null, use that app; adding
  it again makes a second entry.
- `--add-app` takes the App Store URL or the numeric track id. Its
  `app-added` event gives `name`, the app's name in Graft, which `--app` and
  the build take.
- `--delivery sideload` needs a paid Apple Developer account.

### 2. Get the IPA

From the App Store:

```bash
graft --appstore-status --json             # ipatool.signed_in, ipatool.email, unfairplay.enrolled
graft --appstore-get --app example --json
```

- If `ipatool.signed_in` or `unfairplay.enrolled` isn't true, stop and ask
  the person (rules 3 and 4). `--appstore-check`, which reads no keychain,
  says the same from files alone: its `setup_missing` is "decryption isn't
  set up on this Mac", "not signed in to the App Store", or null.
- A get emits `appstore-stage` (`download`, `decrypt`, `stage`) and ends with
  `ipa-provided` {bundle_id, version, filename}. If what it would get is
  already staged, it emits `appstore-already-staged` and downloads nothing;
  `--again` downloads it anyway. For a plain get that's the current version
  as `<bundle id>_<version>.ipa`. For a `--version-id` get it's that build's
  own file, or the plain file only while no other build of that number has
  been listed: with a twin listed, it downloads.
- `appstore-refused` with `reason: "needs-license"` means the Apple ID
  doesn't have the app. With `price` 0, ask the person whether to add it to
  the purchases of the Apple ID in `ipatool.email`, at no cost. Only on a
  yes, run `graft --appstore-claim --app example --json` (it ends with
  `appstore-claimed`), then the get again. With a price above 0, stop: the
  person can buy it in the App Store with the same Apple ID.
- For an older version, `graft --appstore-versions --app example --json`
  lists `appstore-release` {external_id, version, release_date} newest first,
  then `appstore-releases-end`; `version` is null until read (`--offset`
  reads the next page). `--appstore-get --app example --version-id <id>`
  gets that one and holds the app at it (`appstore-hold`): its builds use it
  until a plain `--appstore-get`, which lets go. A `preferred_version` the
  person set by hand is a hold too, and `--appstore-check` shows either as
  `held_version`: ask before a plain get lets go of it. A hold by `--version-id`
  also names the build (`appstore-hold`'s `version_id`, `--appstore-check`'s
  `held_version_id`, and `held_build` {number, count} when the App Store has
  more than one build of that number); its builds use only that build's file,
  `<bundle id>_<version>_<version id>.ipa`. `--set-app-options --app example
  --preferred-version <v>` holds `<v>` by version, which lets go of a build.
- `reason: "unverified"` means UnfairPlay couldn't verify the decrypt.
  `--allow-unverified` stages it anyway, and the app may crash at launch: ask
  first.
- The first App Store action on a Mac downloads about 320 MB and can take
  several minutes.

From a decrypted IPA the person gives you:

```bash
graft --provide-ipa ~/Downloads/Example.ipa --app example --json   # ipa-provided
```

- If the app is held by build (`held_version_id` on `--appstore-check`) at the
  version this IPA is, nothing says the IPA is that build. The hold becomes
  one by version, so builds use this IPA: `appstore-hold` with `version_id`
  null, and a warning. `--decrypt` does the same for every app of its bundle
  id. Tell the person; a `--version-id` get of that build holds it by build
  again.

### 3. Find and fetch tweaks

```bash
graft --search "some tweak" --json              # canister-result {package_id, name, version, repository_id, ...}, canister-search-end
graft --fetch com.example.sometweak --fetch-repository <repository_id> --json   # canister-fetch-end {ok, filename, path}
graft --tweaks --json                           # the tweaks library
```

Pass the result's `repository_id` as `--fetch-repository`: the fetch then takes
only that repository's build, the one the result showed. Without it, a fetch by
package id that more than one Canister repository lists downloads nothing and
exits 2, naming each repository and its address: ask the person which one,
then pass it.

From an APT repository the person added, `graft --repo-packages <URL> --json`
lists `repo-package` events with `download_url` and `sha256`; fetch one with
`graft --fetch <download_url> --fetch-sha <sha256> --json`. Add a repository
only when the person gives you its URL (`--add-repo <URL>`). Use rootless
tweaks; RootHide builds don't load outside a jailbreak.

Graft names the file itself, `<package id>_<version>[_<architecture>].deb`,
from the .deb's own control file. A fetch exits 9 when the download isn't a
.deb (or, by package id, isn't that package), or when a different file of that
name is already in the library; it never replaces one. It records where the
tweak came from (the Canister repository, or the added repo a URL is under),
and the tweak's updates come only from there.

### 4. Attach them

```bash
graft --attach-tweak some-tweak.deb --app example --json    # tweak-attached {tweaks}
```

Use the `filename` from `canister-fetch-end`.

### 5. Build and deliver

```bash
graft example --dry-run --json           # what would happen; changes nothing
graft example --json                     # build, then deliver to the default device
graft example --device <UDID> --json     # to another device
```

Pass `--device` for the device you named to the person (rule 7), so the
build goes where they said yes to, whatever else is plugged in. With no
`--device`, a build goes to the default device when it's connected, else to
the only device connected. With several connected and none of them the
default, it stops before building with "several devices are connected: pick
one" (exit 22): ask the person which (from `--devices`), then pass
`--device`. A `--device` that isn't connected stops it before building too
("requested device … not connected"). When a build goes somewhere other than
the default, a `log` event with `source: "graft"` says where and why. A
build signed with a profile Graft makes, or `--lc-install`, to a device that
isn't registered in the person's developer account stops with "… isn't
registered in your developer account": that's theirs to add at
developer.apple.com. Graft never registers a device.

- A build emits `build-start`, a `stage` event {name, status} at the start
  and end of each stage, then exactly one `build-end` {app, ok, stopped,
  elapsed_seconds, error}.
- LiveContainer delivery puts the IPA in LiveContainer's Documents on the
  device. The person then opens LiveContainer and imports it: tell them so.
  You can't do that step, and Graft can't see it.
- A build signed with a development profile stops before building when the
  device (iOS 16 or later) has Developer Mode off: its `build-end` error says
  so, with the hint. A real build first makes the switch appear in Settings;
  a dry run doesn't. Only the person can turn it on (Settings › Privacy &
  Security › Developer Mode, then confirm after the device restarts).
  `graft --device-info --json` reports it as `developer_mode`: true, false,
  or null before iOS 16 or when it couldn't be read. `--list` says which apps
  need it, as `needs_developer_mode` on each `app`.
- `--skip-install` builds without touching the device.
- `graft example --export-ipa <path>.ipa --json` builds as far as injecting
  the tweaks and saves that IPA, unsigned, at the path, for the person's
  on-device signer or TrollStore. It signs nothing and touches no device, and
  its last event before `build-end` is `ipa-exported` {path, size_bytes}.
  Save it only where the person asks, and don't send the file anywhere (rule
  1). It refuses a file already at the path; ask before adding
  `--overwrite`, which replaces it.
- A failed export's `build-end` says why in `reason`: `exists` (exit 2; ask
  before `--overwrite`), `bad-destination` (2: a folder, a missing folder,
  one of Graft's own, or a `~name` path for a user this Mac doesn't have), `unwritable`, `no-space` or `write-failed` (1: ask the
  person for another folder, or to free space), or `no-ipa` (4: get the IPA,
  step 2). It's null for any other failure, which the exit status describes
  as for a build.

### 6. Check status

```bash
graft --delivery-state --app example --json   # delivery-state {kind, summary, reasons}
graft --reconcile --app example --json        # looks on the device first, then reports
graft --builds --builds-app example --json    # build-record {id, success, error, ...}
graft --appstore-check --json                 # appstore-version: App Store version vs. the staged IPA
graft --tweak-updates --json                  # tweak-update: newer published versions
```

Each `tweak-update` has `source` (where its newer version comes from: the
tweak's own repo or Canister repository) and `verified`, with
`unverified_reason` when it isn't, and `confirm_key`, what `--trust-tweak`
takes for exactly that update (rule 10).

`kind` is `current` (matches the last delivery), `stale` (something changed;
`reasons` says what), `never` or `unknown`. `--reconcile` emits
`reconcile-checked` {app, udid, device_name, found} before an app's
`delivery-state` once a device answered; with none, no device was checked,
and a `log` warning says why when there's a reason to give.

### 7. Bring apps up to date

```bash
graft --update --all --plan --json    # update-plan: what would change, per app and device; changes nothing
graft --update example --json         # after a yes: its tweaks, the App Store get, then build and install
graft --drop-update-target example --device <UDID> --json   # after a yes: stop updating it on a device that's gone
```

- `update-plan` has one entry per app: `store` {from, to, skipped_reason}
  (a get is planned when `to` is set and `skipped_reason` isn't), `tweaks`
  (each with `from`, `to`, `unchecked`, `also_affects`, `source`,
  `verified`, `unverified_reason`, `confirmed`, `confirm_key` and
  `trust_changed`), `needs_confirmation`,
  `install`, `install_skipped_reason`, `manual_install`, `targets`,
  `blocked_reason`, `readiness_note`, `nothing_to_do` and `actionable`
  (there's something to do now, on a device that can take it). Ask with the
  plan in hand (rule 7). The plan's `checks_failed` is true when it asked the
  App Store and canister.me and neither answered, as when offline: every app
  then looks up to date, and isn't known to be. Say so, and ask again later.
- A tweak's newer build comes only from its own `source`. One that isn't
  `verified` is `confirmed` only when the plan was made with `--trust-tweak`
  and its `confirm_key`; until then it isn't part of the update, and the app
  has `needs_confirmation`. On its own it's nothing to do: `--update` leaves
  the app out. `unverified_reason` says why:
  - "source not confirmed yet: …": a tweak from before Graft 2.3.1, or whose
    record was lost. Graft found where it most likely came from, and asks
    once; after a confirmed update, that source is on record.
  - "source unknown: …": no index lists the file, or several do.
  - "its source moved from … to …": the Canister repository on record now
    points somewhere else.
  - "… doesn't use HTTPS" or "… lists no checksum for …".

  Ask the person (rule 10), then pass `--trust-tweak <confirm_key>` to both
  the plan and the update. A `confirm_key` is for one update: the source, the
  version and its builds. If the update changed since (a new version, build
  or source), the plan says `trust_changed`, and `--update` fails that app
  before doing anything for it, with "… changed since you confirmed it: it's
  now …". Show the person the new plan and ask again.
- `targets` is every device the app was installed on, the default first,
  then by name: {udid, device_name, delivery, lc_bundle_id, install,
  install_skipped_reason, manual_install, kind, connected, waiting,
  blocked_reason, readiness, readiness_note}. `kind` (the delivery state) and
  `connected` are there for every device; `waiting` and the readiness fields
  only for one with something to do. An update never installs anywhere
  else. A device that isn't connected is `waiting` ("iPad mini isn't
  connected: it'll update when it is") and the others still install; it
  installs on a later run, once connected. An app is blocked only when none
  of its devices with something to do can take it.
- A device that's gone (sold, retired, reset) waits forever. With the
  person's yes, `--drop-update-target APP --device UDID` stops updating that
  app there until it's installed there again; it emits
  `update-target-dropped` {app, udid, device_name}, and exits 2 with "<app>
  isn't on that device" for a device the app was never installed on.
- An update runs the tweak updates first, then each app's get, one build,
  and an install on each device in turn: the build's own on the first, then
  the same build on each other one. `update-step` says which step starts,
  ends or is skipped (`install` once per device, with its `udid`,
  `device_name` and `waiting`), and the get's and the build's own events
  come through as they do on their own. Each later device's install has its
  own `build-start` and `build-end`, unless its device isn't connected by
  then: that install is skipped, with `waiting` true. A device unplugged
  during its build or install ends that `build-end` with `waiting` true and
  the waiting sentence as `error`: it waits, as its `update-step` says.
- One device's failure doesn't stop the others. A device that refuses the
  build before it starts (Developer Mode off, or a file of the same name in
  its LiveContainer that Graft didn't put there) fails alone, and the build
  goes to the next device.
- A get refused with `needs-license` stops that app ("your Apple ID doesn't
  have this app"). An update never claims it: for a free app, follow step 2
  with the person's yes, then run `--update` again.
- `--update` again resumes where a run failed or was stopped, and installs
  only on the devices where the app isn't up to date.
- LiveContainer delivery ends as a build's does: the person imports the IPA
  in LiveContainer.

## Reading results

- Exit status 0 is success. Otherwise stderr ends with `error: <message>`,
  often followed by `hint: <what to do>`.
- A build exits 1 when any app failed, whatever the cause. Read each
  `build-end`: its `error` holds the message and the hint. An export
  (`--export-ipa`) exits with its failure's own code, and its `build-end`
  has `reason` too (step 5).
- `--update --plan` emits only `update-plan`. `--update` emits `update-plan`
  first and `update-end` {ok, stopped, updated, failed, blocked, not_started,
  waiting} last, with one `update-app-end` {app, ok, stopped, done, left,
  error, targets} between for each app it covers, and exits 1 when any app
  failed or was blocked. It covers each app in the plan that has something
  to do: an app with nothing to do gets no `update-app-end`. `--all` also
  leaves out an app whose `install_skipped_reason` is set, and one whose
  only work is on devices that are waiting. A device that's only waiting isn't a
  failure, nor is one unplugged during the run, before or during its
  install, which waits too: `update-end`'s `waiting` counts them. A Stop
  while it plans emits only `update-end`.
- `update-stopping` {app, step}: a Stop came during a push or an install
  (`step` `lc-push` or `install`) or a tweak's swap into the library
  (`tweaks`, `app` null). That step finishes, then the run stops. A second
  Stop cancels a push or an install.
- A target's `waiting` says its device isn't connected, and its
  `blocked_reason` why else it can't install now (no LiveContainer on it,
  Developer Mode off, or not in the profile that signs the app). A plan's
  `blocked_reason` is set only when no device with something to do can take
  it, and joins their reasons. `store.skipped_reason` says why the App Store
  step won't run (`held at …`, `needs Apple silicon`, `decryption isn't set
  up on this Mac`, `not signed in to the App Store`, `couldn't check the App
  Store`, or the Apple ID doesn't have that version). The two about setup are
  the person's (rules 3 and 4).
- `install_skipped_reason` is `not installed yet` for an app never delivered
  (build it instead), `Update was stopped on every device it's on` after
  `--drop-update-target` dropped them all, "Build & Install <app> once, so
  Graft knows which device it's on" for an app whose installs never
  recorded a device Graft can name, and `the delivery log can't be read`
  when Graft couldn't open its delivery records, so it knows no device.
  With `manual_install` true it's an install only offered on that device
  (`Re-installed an older build`, or the last install couldn't be found on
  the device): a build does it, on the person's yes, with `--device`.
- `update-app-end`'s `left` is the app's steps still to do, and each of its
  `targets` {udid, device_name, ok, error, left, waiting} says how that
  device went. A device a Stop kept from starting has `ok` false, `error`
  null and `left` ["install"]. One that isn't connected has `waiting` true,
  `ok` false, the waiting sentence as `error` and `left` ["install"], and
  isn't a failure. `graft --update <app>` again does what's left.
- `--update-tweak` refuses a tweak update that isn't verified (exit 2, with
  the reason, the source and its `confirm_key` in the hint) unless
  `--trust-tweak` gives that update's `confirm_key`; one that changed since
  that key was read exits 2 too. Its download is refused with exit 9, and
  nothing changes, when it isn't a .deb of that package at that version, or a
  different file has its name. `--trust-tweak` with a bare filename, or
  anything but a `confirm_key`, exits 2; one naming a file the library doesn't
  have exits 15.
- `--install-to-mac` emits `handed-to-playcover` once PlayCover has the IPA,
  then `playcover-installed` {bundle_id, version, settings_disabled,
  settings_restored} when PlayCover has installed it, or
  `playcover-install-unconfirmed` {waited_s} when the wait ran out first.
  Neither is a failure. When `settings_disabled` isn't empty, tell the
  person: Graft turned those PlayCover settings off on the app's first
  handoff, and keeps whatever they set in PlayCover after that.
- Listings end with a sentinel event (`devices-end`, `preflight-end`,
  `canister-search-end`, `appstore-search-end`, `staged-ipas-end` and the
  like), even when they found nothing. No sentinel means the output was cut
  off.
- Warnings are `log` events with `"source": "warning"`, and also go to stderr.
- Hints are written for the person and often name a button in Graft.app.
  Pass them on in plain words, and follow one yourself only when the rules
  above allow it.

| Exit | Meaning |
|---|---|
| 1 | A failure with no code of its own, or a build in which any app failed |
| 2 | A config problem (including "needs a paid Apple Developer Program membership"), or a usage error |
| 3 | A missing tool, certificate, profile or device; `--check` with a failing row |
| 4 | No staged IPA to build from |
| 5 | The IPA is malformed or failed its decryption check |
| 6, 7, 8 | cyan couldn't inject the tweaks; signing failed; installing on the device failed |
| 9, 12 | Canister, or a tweak download Graft refused (not a .deb of that package, or another file's name); an APT repository |
| 10, 11 | App Store Connect; a dump from a jailbroken device |
| 13 | An App Store lookup or search, or an App Store URL it couldn't read |
| 14 | A provided IPA didn't validate |
| 15, 16 | No such tweak in the library; the tweak is still attached to an app |
| 17 | The staged IPA is the last one an app can build from |
| 20 | App Store download or decryption (ipatool or UnfairPlay) |
| 21 | LiveContainer |
| 22 | Several devices are connected and none was named: ask the person which, then pass `--device` |
| 64 | A build with no app named |
| 141 | Whatever read stdout stopped reading |
| 129, 130, 143 | Stopped by a hang-up, by Ctrl-C (`--update`), or by SIGTERM (the app's Stop) |

## Troubleshooting

- The README's [Troubleshooting](README.md#troubleshooting) table lists
  common error messages and their likely causes; so does
  <https://graftipa.com/wiki/troubleshooting/>. The wiki,
  <https://graftipa.com/wiki/>, also has the user guide and the CLI reference.
- `graft --check --json` checks the tools, the device and LiveContainer.
- `graft --builds --json` gives build ids; `graft --build-log <id>` prints
  that build's saved log.
- An iPhone or iPad that lists no apps is usually locked: ask the person to
  unlock it and stay on the home screen.
