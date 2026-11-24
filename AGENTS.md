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
   `--appstore-get` and `--appstore-claim` read the App Store sign-in from the
   login keychain. On a paid account, `--check`, builds and `--lc-install` read
   the `.p12` password there. If macOS shows a dialog, stop and say which
   command caused it. Never answer or dismiss it for the person, and don't run
   `security` yourself.
7. **Device commands change the person's real phone or tablet.** Say which
   device (from `--devices`) and what will change, and get a yes, before a
   build without `--skip-install`, `--reinstall-build`, `--lc-install`,
   `--lc-push-cert`, `--lc-clean`, `--decrypt` or `--decrypt-frontmost`.
   `--install-to-mac` installs on this Mac, under PlayCover: ask first too.
8. **Ask before removing anything:** `--remove-app`, `--detach-tweak`,
   `--delete-tweak`, `--delete-staged`, `--remove-repo` and `--prune-builds`.
9. **Updates and Graft's own data are the person's.** Don't run
   `--download-update` or `--install-update`: Graft.app updates itself, and
   an install quits and replaces it. Never run `--remove-data --items …`
   unless the person asks for that exact removal; it deletes their settings,
   sign-in or saved passwords. `--remove-data` on its own only lists, and is
   safe.

These report, change nothing on a device or an account, and are safe to run
without asking: `--version`, `--describe`, `--help`, `--list`, `--tweaks`,
`--repos`, `--staged-ipas`, `--cached-ipa`, `--builds`, `--delivery-state`,
`--settings`, `--appstore-search`, `--appstore-check`, `--search`,
`--devices`, `--lc-status`, `--reconcile`, `--check-update`, `--remove-data`
(listing only, without `--items`), and a build with
`--dry-run`. The steps below that change only Graft's own files (`--add-app`,
`--appstore-get` for an app the Apple ID has, `--provide-ipa`, `--fetch`,
`--attach-tweak`) need no yes beyond the person's request. Several commands go
online; [What Graft connects to](docs/network.md) lists where.

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
graft --devices --json    # device {udid, name, model, is_default, livecontainer_bundle_id}, then devices-end
```

A failing `preflight-check` row carries a `hint`. A `free` account delivers
only into LiveContainer, which must already be on the device.

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
  the person (rules 3 and 4).
- A get emits `appstore-stage` (`download`, `decrypt`, `stage`) and ends with
  `ipa-provided` {bundle_id, version, filename}. If that version is already
  staged, it emits `appstore-already-staged` and downloads nothing; `--again`
  downloads it anyway.
- `appstore-refused` with `reason: "needs-license"` means the Apple ID
  doesn't have the app. With `price` 0, ask the person whether to add it to
  the purchases of the Apple ID in `ipatool.email`, at no cost. Only on a
  yes, run `graft --appstore-claim --app example --json` (it ends with
  `appstore-claimed`), then the get again. With a price above 0, stop: the
  person can buy it in the App Store with the same Apple ID.
- `reason: "unverified"` means UnfairPlay couldn't verify the decrypt.
  `--allow-unverified` stages it anyway, and the app may crash at launch: ask
  first.
- The first App Store action on a Mac downloads about 320 MB and can take
  several minutes.

From a decrypted IPA the person gives you:

```bash
graft --provide-ipa ~/Downloads/Example.ipa --app example --json   # ipa-provided
```

### 3. Find and fetch tweaks

```bash
graft --search "some tweak" --json              # canister-result {package_id, name, version, ...}, canister-search-end
graft --fetch com.example.sometweak --json      # canister-fetch-end {ok, filename, path}
graft --tweaks --json                           # the tweaks library
```

From an APT repository the person added, `graft --repo-packages <URL> --json`
lists `repo-package` events with `download_url` and `sha256`; fetch one with
`graft --fetch <download_url> --fetch-sha <sha256> --json`. Add a repository
only when the person gives you its URL (`--add-repo <URL>`). Use rootless
tweaks; RootHide builds don't load outside a jailbreak.

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

- A build emits `build-start`, a `stage` event {name, status} at the start
  and end of each stage, then exactly one `build-end` {app, ok, stopped,
  elapsed_seconds, error}.
- LiveContainer delivery puts the IPA in LiveContainer's Documents on the
  device. The person then opens LiveContainer and imports it: tell them so.
  You can't do that step, and Graft can't see it.
- `--skip-install` builds without touching the device.

### 6. Check status

```bash
graft --delivery-state --app example --json   # delivery-state {kind, summary, reasons}
graft --reconcile --app example --json        # looks on the device first, then reports
graft --builds --builds-app example --json    # build-record {id, success, error, ...}
graft --appstore-check --json                 # appstore-version: App Store version vs. the staged IPA
graft --tweak-updates --json                  # tweak-update: newer published versions
```

`kind` is `current` (matches the last delivery), `stale` (something changed;
`reasons` says what), `never` or `unknown`.

## Reading results

- Exit status 0 is success. Otherwise stderr ends with `error: <message>`,
  often followed by `hint: <what to do>`.
- A build exits 1 when any app failed, whatever the cause. Read each
  `build-end`: its `error` holds the message and the hint.
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
| 9, 12 | Canister; an APT repository |
| 10, 11 | App Store Connect; a dump from a jailbroken device |
| 13 | An App Store lookup or search, or an App Store URL it couldn't read |
| 14 | A provided IPA didn't validate |
| 15, 16 | No such tweak in the library; the tweak is still attached to an app |
| 17 | The staged IPA is the last one an app can build from |
| 20 | App Store download or decryption (ipatool or UnfairPlay) |
| 21 | LiveContainer |
| 64 | A build with no app named |
| 141 | Whatever read stdout stopped reading |
| 129, 143 | Stopped by a hang-up or by SIGTERM (the app's Stop) |

## Troubleshooting

- The README's [Troubleshooting](README.md#troubleshooting) table lists
  common error messages and their likely causes; so does
  <https://graftipa.com/wiki/troubleshooting/>. The wiki,
  <https://graftipa.com/wiki/>, also has the user guide and the CLI reference.
- `graft --check --json` checks the tools, the device and LiveContainer.
- `graft --builds --json` gives build ids; `graft --build-log <id>` prints
  that build's saved log.
- A phone that lists no apps is usually locked: ask the person to unlock it
  and stay on the home screen.
