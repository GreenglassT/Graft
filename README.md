# Graft

Graft: inject tweaks, sign with your paid Apple Developer account, and install
IPAs. An IPA signer for iPhone and iPad, as a CLI and a native Mac app.

Graft takes a decrypted IPA, injects `.deb` or `.dylib` tweaks into it, and
delivers the result to your iPhone or iPad over USB or Wi-Fi.

- Delivers into [LiveContainer](https://github.com/LiveContainer/LiveContainer),
  so an app keeps its original bundle id, or signs and installs it as a
  normal app with your own certificate and provisioning profile.
- Gets the source IPA three ways: an IPA you already have, a dump from your own
  jailbroken device, or the App Store with your Apple ID.
- Finds tweaks on canister.me and in APT repositories, and checks them for
  updates.
- Keeps a build history and archived builds for rollback, and tells you when
  a device is behind your config.
- Installs, updates and signs LiveContainer itself, including side-by-side
  instances.

![Graft.app](docs/screenshot.png)

## Install

**Mac app.** Download the notarized `Graft.app` from
[GitHub Releases](https://github.com/GreenglassT/Graft/releases) and move it
to `/Applications`. The app runs the `graft` CLI for everything it does, so
install the CLI as well.

**CLI.**

```bash
pipx install git+https://github.com/GreenglassT/Graft
```

The app looks for `graft` in `~/.local/bin` (where pipx puts it),
`/opt/homebrew/bin` and `/usr/local/bin`.

## Requirements

- A **paid Apple Developer account**, with a development certificate exported
  as a `.p12` and a provisioning profile (a wildcard profile is simplest) that
  includes your device's UDID.
- **macOS on Apple silicon.** The app needs macOS 15 or later; the CLI needs
  Python 3.11 or later (the test suite passes on 3.11, 3.12, 3.13 and 3.14).
- An iPhone or iPad paired with this Mac (connected by USB, or on the same
  network with Wi-Fi sync turned on).
- External tools the CLI runs:

| Tool | Used for | Install |
|---|---|---|
| `cyan` ([pyzule-rw](https://github.com/asdfzxcvbn/pyzule-rw)) | injecting tweaks | `pipx install --force https://github.com/asdfzxcvbn/pyzule-rw/archive/main.zip` |
| `zsign` | signing, for `delivery: sideload` | `brew install zsign` |
| `ideviceinstaller`, `idevice_id`, `iproxy` (libimobiledevice) | installing, finding devices, and the SSH tunnel for `--decrypt` | `brew install libimobiledevice ideviceinstaller` |
| `otool` | checking the IPA is decrypted | `xcode-select --install` |

Optional, for the IPA sources described under [Getting an IPA](#getting-an-ipa):

| Tool | Used for |
|---|---|
| [ipatool](https://github.com/majd/ipatool) 2.8.0 | downloading from the App Store (`--appstore-get`). Use 2.8.0, not 2.8.1, whose downloads fail. Default path `~/.local/bin/ipatool`. |
| [UnfairPlay](https://github.com/DrWhax/unfairplay) v0.4.0 | decrypting App Store IPAs on this Mac. Build it from source; default path `~/.local/share/graft/unfairplay/unfairplay`. |
| [frida](https://frida.re) 16.x on a jailbroken device | on-device decryption (`--decrypt`). The CLI pins the frida Python client to 16.x, and the device's frida-server must be the same major version. |
| [bagbak](https://github.com/ChiChou/bagbak) | one `--decrypt` backend. `npm install -g bagbak@4.0.3` for a 16.x frida-server. |
| [frida-ios-dump](https://github.com/AloneMonkey/frida-ios-dump) | the other `--decrypt` backend. Clone it to `~/.local/share/graft/frida-ios-dump` with a `.venv` inside, or set `GRAFT_FRIDA_IOS_DUMP`. Needs OpenSSH on the device. |
| [PlayCover](https://github.com/PlayCover/PlayCover) | running a build on this Mac (`--for-playcover`, `--install-to-mac`). |

The paths for ipatool and UnfairPlay can be changed in an `appstore:` section
of the config (`ipatool:`, `unfairplay:`, `unfairplay_models:`). Without
`unfairplay_models:`, Graft uses UnfairPlay's own default: `UNFAIRPLAY_MODEL_DIR`
if set, else `~/Library/Application Support/unfairplay/models`.

## Quick start

```bash
# 1. Interactive setup: copies your .p12 and profile into ~/.config/graft/,
#    reads the team id from the profile, finds your device, writes config.yaml.
graft --init

# 2. Store the .p12 password in the macOS Keychain
#    (or export the variable named by signing.cert_password_env).
graft --set-p12-password

# 3. Check tools, signing files and the device.
graft --check

# 4. Add an app by its App Store URL or numeric id, delivered into LiveContainer.
graft --add-app https://apps.apple.com/app/id123456789 --delivery livecontainer
graft --list                        # shows the name it was added under, e.g. "example"

# 5. Give it a decrypted IPA (see "Getting an IPA" for the other sources).
graft --provide-ipa ~/Downloads/Example.ipa --app example

# 6. Get a tweak and attach it.
graft --search "some tweak"
graft --fetch com.example.sometweak         # downloads into ~/.config/graft/tweaks/
graft --attach-tweak some-tweak.deb --app example

# 7. Build and deliver.
graft example
```

Then open LiveContainer on the device: the new IPA is in its Documents, ready to
import. If LiveContainer is not on the device yet, see
[LiveContainer](#livecontainer) first.

In the Mac app the same steps are: **Settings** for the checks and the
Keychain password, **Apps** to add an app, drop an IPA on it, attach tweaks and
press **Build & Install**. First-run setup (`graft --init`) and
`graft --setup-portal` are terminal only.

## Delivery

Each app has a `delivery` mode.

### LiveContainer (recommended)

`delivery: livecontainer` builds the tweaked IPA unsigned and pushes it into
LiveContainer's `Documents` folder over USB or Wi-Fi. You then import it inside
LiveContainer, which runs it as a guest app.

Why this is the main path: a guest app keeps its **original bundle id**, and
some apps tie sign-in to their original bundle id. It also needs no
provisioning profile of its own, so zsign, the profile and the device-in-profile
checks are skipped for these builds.

How a delivery works:

1. `graft example` pushes `Documents/<name>.ipa` into LiveContainer.
2. On the device, open LiveContainer. The IPA is in its Documents; import it
   as an app. LiveContainer signs it on import, and that is when the tweaks
   start loading.
3. Optionally remove the pushed IPAs afterwards with `graft --lc-clean`.

Delivering is not installing: Graft can see what it put in Documents, not
whether you imported it. See [Delivery state](#delivery-state).

### Sideload

`delivery: sideload` (the default for a new app) signs the IPA with zsign
using your `.p12` and profile and installs it with `ideviceinstaller` as a
normal top-level app. The device's UDID must be in the profile. Use this for
apps that do not care about their bundle id or signing team.

Change an app's mode with:

```bash
graft --set-app-options --app example --delivery livecontainer
```

## LiveContainer

If LiveContainer is already on the device through AltStore or SideStore,
Graft uses it as it is. Graft can also install and update LiveContainer
itself, signed with your certificate so that guest apps keep their logins.

Up to three LiveContainers can be installed side by side, as
`com.kdt.livecontainer.<TEAM>` (the primary), `com.kdt.LiveContainer2.<TEAM>`
and `com.kdt.LiveContainer3.<TEAM>`.

```bash
graft --lc-status                     # LiveContainers on each device, who manages them, upstream version
graft --lc-install                    # install or update all three (default device, or --device)
graft --lc-install --instances 1      # only the primary (comma list: 1,2,3)
graft --lc-install --channel stable   # stable instead of nightly (nightly is what upstream recommends)
graft --lc-push-cert                  # put your .p12 in LiveContainer's Documents
graft --lc-clean --cert-only          # remove the certificate once imported
graft --lc-clean                      # remove the certificate and the IPAs Graft pushed
```

Notes:

- `--lc-install` needs an App Store Connect API key (`graft --setup-portal`)
  to fetch profiles, and the App IDs above registered in your developer account
  with the App Groups capability. App groups cannot be assigned through the
  API; if one is missing, the error says what to register.
- On a device where AltStore or SideStore manages LiveContainer,
  `--lc-install` refuses, because replacing those copies would break every
  guest app signed through them. Update through AltStore instead, or pass
  `--force`.
- `--lc-push-cert` is for a LiveContainer Graft installed: it has no AltStore
  to take a certificate from, and its JIT-less mode needs one. Import it in
  LiveContainer's Settings, then run `graft --lc-clean --cert-only`. Until
  then the certificate sits where guest apps can read it.
- With more than one LiveContainer installed, Graft pushes into the primary
  and reports the others. To use a different one for an app, set
  `lc_bundle_id: <exact bundle id>` on that app in `config.yaml`. If several
  are installed and none matches, the build stops and asks you to set it.

## Getting an IPA

A build needs a **decrypted** IPA of the app, staged in
`~/.cache/graft/ipas/`. Graft checks every Mach-O binary in it before injecting
and stops if any is still encrypted. There are three ways to get one.

These are for **personal use**: apps you have obtained with your own Apple ID,
on your own devices. Do not redistribute decrypted or modified IPAs.

### Bring your own

```bash
graft --provide-ipa ~/Downloads/Example.ipa --app example
```

Or drop the IPA on the app in the Mac app. Graft checks that the IPA's bundle
id matches the app's and stages it under its canonical name. An IPA that is
still encrypted (straight from the App Store) is decrypted with UnfairPlay on
the way in, if it is set up.

### Decrypt on a jailbroken device

```bash
graft --decrypt-check                       # what, if anything, is blocking a dump right now
graft --decrypt com.example.app             # dump an installed app by bundle id
graft --decrypt-frontmost                   # dump whatever app is on screen
graft --decrypt com.example.app --decrypt-backend bagbak
```

Two backends. Unless you pass `--decrypt-backend`, Graft picks the one that can
run right now, preferring frida-ios-dump:

- **frida-ios-dump** attaches to the running app. Graft brings it to the
  foreground first, launching it if needed. It copies the bundle over SSH:
  store the device's root password with `graft --set-ssh-password`, or set
  `GRAFT_SSH_KEY` (and `GRAFT_SSH_USER` if it is not `root`).
- **bagbak** spawns the app itself. bagbak 4.x (the version that works with a
  16.x frida-server) also copies the bundle over SSH, using the same stored
  root password (`--set-ssh-password`); it cannot use `GRAFT_SSH_KEY`.

`--decrypt-mode all|main|ext` chooses what bagbak dumps; it applies only to
bagbak 5.x, since 4.x always dumps the whole app. A dump that leaves app
extensions encrypted (an extension built for a newer iOS than the device runs)
drops them and stages the rest, and says what it removed; pass
`--keep-encrypted-extensions` to fail instead.

### App Store with UnfairPlay

```bash
graft --appstore-status                     # ipatool installed and signed in? UnfairPlay enrolled?
graft --appstore-check                      # App Store version vs. your staged IPA, per app
graft --appstore-get --app example          # download, decrypt and stage the latest version
```

ipatool downloads the App Store's encrypted IPA for an app your Apple ID has
already obtained, and UnfairPlay decrypts it on this Mac. Two one-time steps
are yours to run in Terminal, and Graft never runs them for you:
`ipatool auth login` (your Apple ID password and 2FA code) and
`sudo unfairplay setup` (enrolls this Mac). `--appstore-status` prints the exact
commands for whatever is missing.

**Privacy.** The downloaded IPA carries your Apple ID and your licence for the
app. Graft keeps it in a private temporary directory, deletes it afterwards,
and always runs UnfairPlay with `--strip-account-data`, which removes the
licence files and the Apple ID fields from the decrypted copy. What gets
staged, built and put on a device does not identify you.

UnfairPlay verifies the decrypted pages against Apple's own hashes. If it
cannot (some older apps), the IPA is refused unless you pass
`--allow-unverified`.

### Staged IPAs

```bash
graft --staged-ipas                         # every staged IPA, and anything stripped from it
graft --cached-ipa example                  # which IPA a build of "example" would use
graft --delete-staged com.example.app_1.2.3.ipa
```

## Tweaks

```bash
graft --search "some tweak"                 # search canister.me
graft --fetch com.example.sometweak         # download a package by id (SHA-256 checked)
graft --fetch https://example.com/some-tweak.deb --fetch-sha <sha256>
graft --tweaks                              # list what is in tweaks_dir
graft --attach-tweak some-tweak.deb --app example
graft --detach-tweak some-tweak.deb --app example
graft --delete-tweak some-tweak.deb         # to the Trash; refused while an app uses it
graft --tweak-updates                       # newer published versions?
graft --update-tweak some-tweak.deb         # download the newest and re-point the apps using it
```

A `--fetch` by package id is checked against canister's published hash. A URL
is checked only if you pass `--fetch-sha`.

### APT repositories

```bash
graft --add-repo https://repo.example.com
graft --repos
graft --repo-packages https://repo.example.com [--refresh]
graft --remove-repo https://repo.example.com
```

Free repositories only; repos that need a login or payment are not supported.

### Shared libraries

cyan bundles ElleKit as CydiaSubstrate, but not the other shared libraries a
tweak may link against (preference, colour or settings libraries, for
example). If a build fails with
`Library not loaded: @rpath/libexample.dylib`, put a sandbox-compatible copy of
that library in `tweaks_dir` and add its filename to the app's `tweaks:` list,
as you would a `.deb`.

### Rootless tweaks only

Tweaks built for RootHide depend on `libroothide.dylib`, which does not exist
outside a jailbreak. Use the rootless build from the developer's own repository
or releases.

## Building

```bash
graft example                       # build and deliver
graft example other                 # several apps in turn
graft example --dry-run             # show what would happen without doing it
graft example --skip-install        # build (and sign) only
graft example --device <UDID>       # a specific device instead of the default
graft example --for-playcover       # also make the build loadable on this Mac under PlayCover
graft example -v                    # verbose, and keep the temp directory
```

The pipeline:

1. **Pre-flight.** Tools, signing files, password, device, and that the device
   is in the profile. Only what the delivery mode needs is checked.
2. **Source IPA.** The decrypted IPA staged in `~/.cache/graft/ipas/`. With
   nothing staged, the build stops and lists the three ways to get one.
3. **Decryption check.** `otool -l` on every Mach-O; any `cryptid 1` stops the
   build.
4. **Inject.** cyan, with the app's tweaks and options. The original bundle id
   is kept by default.
5. **Sign** (sideload only). zsign with your `.p12` and profile.
6. **Deliver.** Push into LiveContainer, or install with `ideviceinstaller`.
7. **Archive.** Copy the result to `~/.cache/graft/built/`, record it in
   `~/.local/state/graft/log.sqlite`, and apply archive retention.

## Build history and retention

```bash
graft --builds [--builds-app example] [--builds-limit 20]
graft --build-log 42                # the saved log of build 42
graft --reveal-build 42             # show its archived IPA in Finder
graft --reinstall-build 42          # deliver that archive again, without rebuilding
graft --prune-builds --dry-run      # what retention would remove
graft --prune-builds [--keep 1]     # apply it now
```

After each successful build, all but the newest `archive_keep` archives **per
app version** go to the Trash (default 2; `0` keeps only the build just made).
Grouping by version means rebuilds of one version never push out the last build
of an older version, so you can always roll back to a version you shipped.
Archives go to the Trash, not straight to deletion, so the space comes back when
you empty it. Staged source IPAs are never pruned.

## Delivery state

```bash
graft --delivery-state [--app example]   # would building now produce something different?
graft --reconcile [--app example]        # check the device first, then report
```

`--list` shows it too. On every delivery Graft records a SHA-256 over what
determines the build: the source IPA's bundle id and version, each tweak's
filename and contents, the cyan options and the delivery mode. Comparing that
with what the config would produce now gives one of:

| State | Means |
|---|---|
| `never` | Nothing has been delivered for this app. |
| `stale` | Something changed since the last delivery; the reasons say what (for example `some-tweak.deb rebuilt since delivery` or `app version changed: 1.2.3 → 1.3.0`). |
| `current` | Matches the last delivery. |
| `unknown` | Graft cannot tell (for example a tweak file it cannot read, or no staged IPA), and says so instead of guessing. |

Tweak contents are hashed, not just names, because a tweak rebuilt from source
keeps its filename. The source IPA is identified, not hashed.

Limits worth knowing:

- **Delivered is not installed.** For LiveContainer, Graft knows it wrote the
  file, not whether you imported it. `--reconcile` lists LiveContainer's
  Documents and can only downgrade a state: a missing file means the record is
  out of date, but a present file does not prove an import.
- `--reconcile` leaves sideload apps alone.
- A `--reinstall-build` always reads `stale`, because what went into an old
  archive was not recorded.

None of this blocks a build.

## Devices

```bash
graft --devices                     # connected devices over USB and Wi-Fi
graft --device-info [--device <UDID>]
graft --device-apps [--with-icons]  # user apps on the device
```

Set a default device with `graft --set-settings --default-device <UDID>`.

## Configuration

The config lives at `~/.config/graft/config.yaml` (`$XDG_CONFIG_HOME/graft`
if set). `graft --init` writes a starter file with no apps. A configured app
looks like this:

```yaml
signing:
  cert: ~/.config/graft/AppleDevelopment.p12
  cert_password_env: GRAFT_P12_PASS     # or use the Keychain: graft --set-p12-password
  profile: ~/.config/graft/wildcard.mobileprovision
  team_id: ABCDE12345

cache_dir: ~/.cache/graft/ipas
tweaks_dir: ~/.config/graft/tweaks
archive_keep: 2                         # archived builds kept per app version
# default_device_udid: <UDID>

apps:
  example:
    bundle_id: com.example.app
    preferred_version: latest
    delivery: livecontainer             # or sideload
    # lc_bundle_id: com.kdt.LiveContainer2.ABCDE12345   # pin a LiveContainer
    tweaks:
      - some-tweak.deb                  # files in tweaks_dir
    cyan:
      display_name: Example
      keep_bundle_id: true              # some apps tie sign-in to their original bundle id
      strip_extensions: false
      strip_supported_devices: true
      compression_level: 6
```

Most of this can be edited without opening the file:
`--set-app-options` (with `--preferred-version`, `--[no-]keep-bundle-id`,
`--[no-]strip-extensions`, `--[no-]strip-supported-devices`,
`--compression-level`, `--delivery`), `--set-settings` (with
`--default-device`, `--archive-keep`, `--signing-cert`, `--signing-profile`,
`--signing-team-id`), `--add-app-manual --name --bundle-id --display-name`
for an app not on the App Store, and `--remove-app`. `graft --settings`
shows the global settings.

**Bundle id.** Some apps tie sign-in to their original bundle id;
`keep_bundle_id: true` (the default) preserves it. LiveContainer delivery keeps
it in every case.

**Per-app profiles.** `auto_profile: true` has Graft create and cache a profile
for the app through the App Store Connect API (set up with
`graft --setup-portal`); `profile: <path>` points one app at a specific
profile.

**The lock file.** Graft takes a lock on `config.yaml.lock` beside the config
while it edits it, so the app and the CLI never write over each other. If you
keep the config in a dotfiles repository and symlink it, add
`config.yaml.lock` to that repository's `.gitignore`.

### Environment variables

| Variable | Meaning |
|---|---|
| the one named by `signing.cert_password_env` | `.p12` password, if not in the Keychain |
| `GRAFT_SSH_USER`, `GRAFT_SSH_PASS`, `GRAFT_SSH_KEY` | SSH to a jailbroken device for `--decrypt` |
| `GRAFT_FRIDA_IOS_DUMP` | where frida-ios-dump is cloned |
| `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `XDG_STATE_HOME`, `XDG_DATA_HOME` | move Graft's directories |

### Files

```
~/.config/graft/
├── config.yaml
├── config.yaml.lock
├── AppleDevelopment.p12
├── wildcard.mobileprovision
└── tweaks/              # .deb and .dylib files

~/.cache/graft/
├── ipas/                # staged decrypted IPAs (never pruned)
├── built/               # archived builds (newest archive_keep per app version)
└── artwork/             # cached app icons

~/.local/state/graft/
├── log.sqlite           # build history and delivery records
├── build-logs/          # saved event log of each --json build (every app build)
├── repos/               # cached APT repository indexes
└── tweak-rollback/      # previous versions kept by --update-tweak

~/.local/share/graft/    # tools installed for Graft (UnfairPlay, frida-ios-dump)
```

## Running builds on this Mac

`graft example --for-playcover` also makes the build loadable under PlayCover
on an Apple silicon Mac, and `graft --install-to-mac <staged IPA filename>` hands
a staged IPA to PlayCover. Some tweak libraries import iOS private frameworks
that macOS does not have; for these Mac builds Graft bundles a minimal stand-in
instead. Phone builds are never changed this way.

## Scripting

`--json` turns any command's output into newline-delimited JSON events on
stdout. This is how the Mac app talks to the CLI.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `ApplicationVerificationFailed` | The `.p12` and profile are from different teams, or the device's UDID is not in the profile. |
| `MismatchedApplicationIdentifierEntitlement` | The profile does not allow this bundle id; use a wildcard profile. Installing LiveContainer: delete an old zsign-signed copy on the device first. |
| `DeviceLocked` | Unlock the device. |
| `wrong password` from zsign | The `.p12` password is wrong. |
| `killed: 9` from zsign | Quarantine attribute: `xattr -cr "$(which zsign)"`. |
| `Library not loaded: @rpath/libexample.dylib` | Add a copy of that library to the app's tweaks; see [Shared libraries](#shared-libraries). |
| `couldn't extract some-tweak.deb` | The download is probably an HTML error page; fetch it again. |
| A guest app in LiveContainer does not remember its login | LiveContainer was signed with a wildcard profile's entitlements (for example by zsign). Reinstall it with `graft --lc-install`. |
| `--decrypt` times out | Run `graft --decrypt-check`. Usually the frida majors differ, SSH to the device is down, or the app could not be brought to the foreground. |

## Building the app from source

```bash
brew install xcodegen
cd Graft && xcodegen generate
open Graft.xcodeproj
```

Or from the command line:

```bash
xcodebuild -project Graft/Graft.xcodeproj -scheme Graft -configuration Release \
  -destination 'platform=macOS' build
```

Tests:

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
cd Graft && TEST_RUNNER_PATH="$PWD/../.venv/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  xcodebuild test -scheme Graft -destination 'platform=macOS'
```

The app is SwiftUI. It runs the `graft` CLI as a subprocess for every action
and reads its `--json` events; both share `config.yaml` and `log.sqlite`, and
there is no daemon or embedded interpreter. See [Graft/README.md](Graft/README.md).

## Disclaimer

Graft is not affiliated with, endorsed by, or sponsored by Apple Inc. Apple,
iPhone, iPad, App Store and macOS are trademarks of Apple Inc., used here only
to identify the products Graft works with.

## License

Graft's code is MIT; it depends on pymobiledevice3, which is GPL-3.0-or-later.
See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Support

If Graft is useful to you, you can support it at
[ko-fi.com/greenglasst](https://ko-fi.com/greenglasst).
