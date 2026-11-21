# Graft

Graft: inject tweaks and install IPAs, into LiveContainer with a free Apple ID
or signed with your paid Apple Developer account. An IPA signer for iPhone and
iPad, as a CLI and a native Mac app.

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
pipx install git+https://github.com/GreenglassT/Graft     # brew install pipx, if you need it
pipx ensurepath
```

`pipx ensurepath` adds `~/.local/bin` to your PATH; open a new terminal
afterwards so the `graft` command is found.

The app looks for `graft` in `~/.local/bin` (where pipx puts it),
`/opt/homebrew/bin`, `/usr/local/bin` and `/opt/local/bin`, then on your login
shell's PATH (absolute directories only). If `graft` is there but won't start,
usually because Homebrew upgraded or removed the Python pipx installed it with,
the setup screen says why and offers `pipx reinstall graft-ipa`. The CLI it
starts gets those four directories first on its PATH, then your login shell's
PATH (the system directories, if the shell does not answer), so install the
tools below where one of them can see them.

## Updating

For the CLI, run `pipx reinstall graft-ipa`. Once a new version is tagged on
GitHub, `pipx upgrade graft-ipa` works too. For the Mac app, download the new
`Graft.app` from [GitHub Releases](https://github.com/GreenglassT/Graft/releases)
and replace the one in `/Applications`.

## Requirements

Graft works with either kind of Apple account; `graft --init` asks which you
have. The config records the answer: a config with no `signing:` section is a
free Apple ID.

### Free Apple ID (LiveContainer delivery)

- A free Apple ID, and LiveContainer on the device, installed and refreshed
  every 7 days with that Apple ID by SideStore, iloader, AltStore or Impactor.
  See [With a free Apple ID](#with-a-free-apple-id) for the setup.
- Graft only builds unsigned IPAs and pushes them into LiveContainer, which
  signs them when you import them. It never signs anything itself and never
  uses your Apple ID.
- Every app uses `delivery: livecontainer`. zsign, a `.p12` and a
  provisioning profile are not needed. Features marked **paid only** below
  are not available.

### Paid Apple Developer account (everything)

- A **paid Apple Developer Program membership**, with a development
  certificate exported as a `.p12` and a provisioning profile (a wildcard
  profile is simplest) that includes your device's UDID.
- Adds `delivery: sideload`, installing and signing LiveContainer itself
  (`--lc-install`, `--lc-push-cert`) and per-app profiles from App Store
  Connect (`auto_profile`, `--setup-portal`).

### Both

- **macOS.** The app needs macOS 15 or later and is a universal build, though
  it has only been tested on Apple silicon; the CLI needs Python 3.11 or later
  (the test suite passes on 3.11, 3.12, 3.13 and 3.14). Decrypting App Store
  IPAs with UnfairPlay and running builds under PlayCover need Apple silicon.
- An iPhone or iPad paired with this Mac (connected by USB, or on the same
  network with Wi-Fi sync turned on). Graft finds it, and pushes into
  LiveContainer, through pymobiledevice3, which installs with the CLI; a
  LiveContainer-only (free Apple ID) setup needs nothing from libimobiledevice.
- External tools the CLI runs:

| Tool | Used for | Install |
|---|---|---|
| `cyan` ([pyzule-rw](https://github.com/asdfzxcvbn/pyzule-rw)) | injecting tweaks | `pipx install --force https://github.com/asdfzxcvbn/pyzule-rw/archive/04a40f2aa65ec9cb36127f74d52ae4ef71377c50.zip` (the commit tagged v1.4.4, the release Graft is tested with) |
| `zsign` | signing, for `delivery: sideload` (paid only) | `brew install zsign` |
| `ideviceinstaller`, `iproxy` (libimobiledevice) | installing a sideload build, and the SSH tunnel for `--decrypt` | `brew install libimobiledevice ideviceinstaller` |
| `otool` | checking the IPA is decrypted | `xcode-select --install` |

`graft --check` lists each missing tool with the command that installs it, and
only the tools your apps' delivery modes use: zsign and `ideviceinstaller` only
when some app sideloads (or, on a paid config with no apps yet, since a new app
sideloads by default). The
`brew` commands need [Homebrew](https://brew.sh); `--check` says so when it is
not installed, and the libimobiledevice tools are in MacPorts too
(`sudo port install libimobiledevice ideviceinstaller`).

Optional, for the IPA sources described under [Getting an IPA](#getting-an-ipa):

| Tool | Used for |
|---|---|
| [ipatool](https://github.com/majd/ipatool) 2.8.0 | downloading from the App Store (`--appstore-get`). Use 2.8.0 from its GitHub releases; 2.8.1 has a download bug and Graft refuses it. Graft looks for it at the `appstore.ipatool` path, then `~/.local/bin/ipatool`, then on your PATH (so a Homebrew install is found too). `graft --appstore-status` prints the commands that download the release for your Mac's processor and put it in place. |
| [UnfairPlay](https://github.com/DrWhax/unfairplay) v0.4.0 | decrypting App Store IPAs on an Apple silicon Mac. Build it from source (it is a Go program); default path `~/.local/share/graft/unfairplay/unfairplay`. `graft --appstore-status` prints the clone, checkout and `go build` commands. |
| [frida](https://frida.re) 16.x on a jailbroken device | on-device decryption (`--decrypt`). The CLI pins the frida Python client to 16.x, and the device's frida-server must be the same major version. |
| [bagbak](https://github.com/ChiChou/bagbak) | one `--decrypt` backend. `npm install -g bagbak@4.0.3` for a 16.x frida-server. |
| [frida-ios-dump](https://github.com/AloneMonkey/frida-ios-dump) | the other `--decrypt` backend. Clone it to `~/.local/share/graft/frida-ios-dump`, check out `56e99b2` (its last release for frida 16; the current one needs frida 17), and create a `.venv` there with `pip install 'frida==16.*' paramiko scp tqdm`, or set `GRAFT_FRIDA_IOS_DUMP`. `graft --decrypt-check` prints these commands when it is not set up. Needs OpenSSH on the device. |
| [PlayCover](https://github.com/PlayCover/PlayCover) | running a build on this Mac (`--for-playcover`, `--install-to-mac`). |

The paths for ipatool and UnfairPlay can be changed in an `appstore:` section
of the config (`ipatool:`, `unfairplay:`, `unfairplay_models:`). Without
`unfairplay_models:`, Graft uses UnfairPlay's own default: `UNFAIRPLAY_MODEL_DIR`
if set, else `~/Library/Application Support/unfairplay/models`.

## Quick start

### With a free Apple ID

First, on the device, with your free Apple ID:

1. Install [SideStore](https://sidestore.io), or use
   [iloader](https://github.com/nab138/iloader).
2. Install LiveContainer
   ([guide](https://livecontainer.github.io/docs/installation/lc_sidestore)).
   The LiveContainer+SideStore build uses one app slot for both.
3. In LiveContainer, import the certificate from SideStore, so it can sign the
   apps you import.

Then on the Mac:

```bash
graft --init                        # answer no to the paid-membership question
graft --check                       # tools, the device, and LiveContainer on it
graft --add-app https://apps.apple.com/us/app/example/id123456789   # delivery: livecontainer
graft --provide-ipa ~/Downloads/Example.ipa --app example
graft --attach-tweak some-tweak.deb --app example
graft example                       # build and push into LiveContainer
```

Open LiveContainer and import the IPA from its Documents. Graft finds
LiveContainer on the device by its bundle id, so there is no team id to
configure.

### With a paid Apple Developer account

```bash
# 1. Interactive setup: answer yes to the paid-membership question. It asks
#    where your .p12 and profile are, copies them into ~/.config/graft/,
#    reads the team id from the profile, finds your device, and
#    writes config.yaml. If it could not read a profile it writes team_id
#    REPLACEME; set the real one (graft --set-settings --signing-team-id <TEAM>)
#    before step 2: the Keychain entry is keyed by team id, so
#    --set-p12-password refuses until it is set.
graft --init

# 2. Store the .p12 password in the macOS Keychain
#    (or export the variable named by signing.cert_password_env).
graft --set-p12-password

# 3. Check tools, signing files and the device.
graft --check

# 4. Add an app by its App Store URL or numeric id, delivered into LiveContainer.
graft --add-app https://apps.apple.com/us/app/example/id123456789 --delivery livecontainer
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
Keychain password, **+** in the sidebar to add an app, drop an IPA on it,
attach tweaks and press **Build & Install**. First-run setup (`graft --init`)
and `graft --setup-portal` happen in Terminal. Until the CLI is installed and
`graft --init` has written a config, the app shows a setup screen with the
commands to run, each with a Copy button, and checks again when you come back
to it. Once the config exists, the same screen lists any tool `graft --check`
reports missing, each with its install command (a free Apple ID's list leaves
out the paid-only tools), and you can continue without them. After that,
missing tools show as a notice at the bottom of the window ("2 tools missing —
see Settings") rather than as a failed build. The app re-reads your config
each time its window comes back to the front, so a change made in Terminal
(such as `graft --init` switching the account, which also re-runs the tool
checks) shows without a relaunch. If `config.yaml` does not parse, Settings
says so at the top, shows the default paths and saves nothing until it is
fixed; fix it in an editor and the notice clears when you come back to the
window. A stopped build shows as Stopped,
not as a failure, and adds no row to Builds. With a free Apple ID, Settings
shows the account, and what needs a paid
membership (Sideload delivery, installing LiveContainer or putting a
certificate on the device from Graft, the signing settings and the .p12
password) stays on screen, disabled, with the reason beside it.

## Delivery

Each app has a `delivery` mode. An app with no `delivery:` key, or one added
without `--delivery`, is `livecontainer` with a free Apple ID and `sideload`
with a paid account. Graft writes the key whenever it adds an app, so an app
keeps its mode if the config later changes account.

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

### Sideload (paid only)

`delivery: sideload` (the default for a new app with a paid account) signs the
IPA with zsign using your `.p12` and profile and installs it with
`ideviceinstaller` as a normal top-level app. The device's UDID must be in the
profile. Use this for apps that do not care about their bundle id or signing
team. On a free Apple ID, building, re-installing or adding a sideload app is
refused.

Change an app's mode with:

```bash
graft --set-app-options --app example --delivery livecontainer
```

## LiveContainer

If LiveContainer is already on the device through SideStore, AltStore,
iloader or Impactor, Graft uses it as it is. With a paid account Graft can
also install and update LiveContainer itself, signed with your certificate so
that guest apps keep their logins.

Up to three LiveContainers can be installed side by side, as
`com.kdt.livecontainer.<TEAM>` (the primary), `com.kdt.LiveContainer2.<TEAM>`
and `com.kdt.LiveContainer3.<TEAM>`. Every installer names them this way, with
its team id, so Graft finds them on the device by bundle id: a ten-character
team id after the name, whatever team it is, in any letter case. A free Apple
ID needs no team id in the config. System apps count too, so a LiveContainer
that TrollStore or a jailbreak installed (often as plain
`com.kdt.livecontainer`) is found, and so is one an on-device re-signer gave
some other suffix.

Which one a build pushes into, in this order:

1. the app's `lc_bundle_id:`, if it has one, in any letter case (or the id
   without its team suffix, when that names exactly one). A pin that names
   nothing installed, or an app that is not LiveContainer, stops the build
   with what was found; it is never quietly ignored;
2. the only LiveContainer on the device, whatever its bundle id;
3. with a paid account, the configured team's primary;
4. the only primary (`com.kdt.livecontainer.<TEAM>`, or plain
   `com.kdt.livecontainer`).

Anything else stops the build with a list of what was found. That includes
plain `com.kdt.livecontainer` next to a `com.kdt.livecontainer.<TEAM>`: Graft
can't tell which one you import from, so it asks you to pin one rather than
guess. A build finds out before it injects anything: the push's target, and
whether its `<app>.ipa` name is free there, are checked ahead of cyan.

`graft --devices` (and the Mac app's sidebar) names the LiveContainer a push
would use, found the same way.

```bash
graft --set-app-options --app example --lc-bundle-id com.kdt.LiveContainer2.ABCDE12345
graft --set-app-options --app example --no-lc-bundle-id    # drop the pin
```

```bash
graft --lc-status                     # LiveContainers on each device, who manages them, upstream version
graft --lc-install                    # paid only: install or update all three (default device, or --device)
graft --lc-install --instances 1      # only the primary (comma list: 1,2,3)
graft --lc-install --channel stable   # stable instead of nightly (nightly is what upstream recommends)
graft --lc-push-cert                  # paid only: put your .p12 in the primary's Documents
graft --lc-clean --cert-only          # remove the certificate once imported
graft --lc-clean                      # remove the certificate and the IPAs Graft pushed, wherever each went
```

`--lc-status` and `--lc-clean` work with either account. In the Mac app, a
device's LiveContainer section lists what is installed and who manages it on
either account; with a free Apple ID its Install / Update and Put certificate
on device buttons are disabled, and it points you at the store that installed
LiveContainer. Clean up is there whenever the device has a LiveContainer,
whoever installed it.

Notes:

- `--lc-install` needs an App Store Connect API key (`graft --setup-portal`)
  to fetch profiles, and the App IDs above (plus the primary's extensions)
  registered at developer.apple.com with the App Groups capability and the
  group `group.com.SideStore.SideStore.<TEAM>` assigned. App groups cannot be
  assigned through the API; if one is missing, the error names the App ID.
- On a device where AltStore or SideStore manages LiveContainer,
  `--lc-install` refuses, because replacing those copies would break every
  guest app signed through them. Update through AltStore instead, or pass
  `--force`.
- `--lc-push-cert` is for a LiveContainer Graft installed: it has no AltStore
  to take a certificate from, and its JIT-less mode needs one. Import it in
  LiveContainer's Settings, then run `graft --lc-clean --cert-only`. Until
  then the certificate sits where guest apps can read it.
- With more than one LiveContainer installed, Graft pushes into the primary
  and reports the others. To use a different one for an app, pin it with
  `graft --set-app-options --app <name> --lc-bundle-id <bundle id>` (or set
  `lc_bundle_id:` on that app in `config.yaml`, or choose it under LiveContainer
  in the app's Edit sheet in the Mac app, which lists the default device's
  LiveContainers; Automatic means no pin). If there is no single primary
  (two primaries from two Apple IDs, say, or only secondaries) and no pin, the
  build stops, lists what it found and asks you to set one.
- Graft never deletes or replaces a file in LiveContainer's Documents unless
  it knows the file is its own: a delivery record for that device and that
  LiveContainer, or, with no record (after a state reset, say), the file's
  SHA-256 matching one of that app's archived builds. `--lc-clean` removes
  only the IPAs its deliveries recorded and a certificate `--lc-push-cert`
  recorded, and retires those records, so a file you later put there under
  the same name is yours. An `AppleDevelopment.p12` with no record (one
  pushed by Graft 1.0, or your own) is reported by `--lc-clean` and left in
  place; `--lc-push-cert` replaces it only when its bytes are your configured
  `.p12`. A build whose IPA name (`<app>.ipa`) is already taken by a file
  that is not Graft's stops instead of overwriting it: rename or remove that
  file in the Files app, then build again. A re-push of Graft's own earlier
  delivery replaces it as before.

## Getting an IPA

A build needs a **decrypted** IPA of the app, staged in
`~/.cache/graft/ipas/`. Graft checks every Mach-O binary in it before injecting
and warns if any is still encrypted; the build carries on, but the app will
most likely not launch. There are three ways to get one.

These are for **personal use**: apps you have obtained with your own Apple ID,
on your own devices. Do not redistribute decrypted or modified IPAs.

### Bring your own

```bash
graft --provide-ipa ~/Downloads/Example.ipa --app example
```

Or drop the IPA on the app in the Mac app. Graft checks that the IPA's bundle
id matches the app's and stages it under its canonical name. An IPA that is
still encrypted (straight from the App Store) is decrypted with UnfairPlay on
the way in if it is set up, and refused if it is not.

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

1. `ipatool auth login --email <your Apple ID>`, which asks for your password
   and 2FA code.
2. Enroll this Mac with UnfairPlay. Sign in to the Mac App Store with the same
   Apple ID, install any iPhone or iPad app from it (only an Apple silicon Mac
   can) and open it once, then run
   `sudo unfairplay setup --app "/Applications/<that app>.app"`.

`--appstore-status` prints these commands, with the paths filled in, for
whatever is missing, along with the commands that install ipatool and build
UnfairPlay. It only prints them; Graft runs none of them. In the Mac app,
Settings › App Store shows the same list, with a Copy button for each tool's
install command.

**Privacy.** The downloaded IPA carries your Apple ID and your licence for the
app. Graft keeps it in a private temporary directory, deletes it afterwards,
and runs UnfairPlay with `--strip-account-data`. It does not rely on that
flag: every IPA Graft stages, from any source (`--appstore-get`,
`--provide-ipa` or a `--decrypt` dump), has `iTunesMetadata.plist` (your Apple
ID and account ids) and every `SC_Info/` licence directory removed, and is
checked for them before it goes into the cache. An IPA staged by an earlier
version is cleaned the first time a build uses it, and so is anything
`--reinstall-build` or `--install-to-mac` hands over (an archive built by
Graft 1.0, say). If the cache cannot be written (a full or read-only disk),
the build warns and goes on from a cleaned copy instead; only when no clean
copy can be made does it stop, rather than ship the data. A sideload archive
keeps the licence files its signature seals, since removing them would break
the install: rebuild it to get rid of them. What gets staged, built and put
on a device does not identify you.

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

A `--fetch` by package id is checked against canister's published hash, when
canister has one. A URL is checked only if you pass `--fetch-sha`.

### APT repositories

```bash
graft --add-repo https://repo.example.com
graft --repos
graft --repo-packages https://repo.example.com [--refresh]
graft --remove-repo https://repo.example.com
```

Free repositories only; repos that need a login or payment are not supported.

### Shared libraries

cyan bundles ElleKit as CydiaSubstrate and adds a few common frameworks when a
tweak links them, but not every shared library a tweak may link against
(colour or settings libraries, for example). If a build fails with
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
graft example --skip-install        # build (and sign, for sideload) only
graft example --device <UDID>       # a specific device instead of the default
graft example --for-playcover       # also make the build loadable on this Mac under PlayCover
graft example -v                    # verbose, and keep the temp directory
```

The pipeline:

1. **Pre-flight.** Tools, signing files, password, device, and that the device
   is in the profile. Only what the delivery mode needs is checked.
2. **Source IPA.** The decrypted IPA staged in `~/.cache/graft/ipas/`. With
   nothing staged, the build stops and lists the three ways to get one.
3. **Decryption check.** `otool -l` on every Mach-O; any `cryptid 1` is
   reported as a warning naming the binary, and the build carries on
   (`--skip-decrypt-check` skips the check).
4. **Inject.** cyan, with the app's tweaks and options. The original bundle id
   is kept by default.
5. **Sign** (sideload only). zsign with your `.p12` and profile.
6. **Archive.** Copy the result to `~/.cache/graft/built/`.
7. **Deliver.** Push into LiveContainer, or install with `ideviceinstaller`.
   Skipped with `--skip-install`.
8. **Record.** Write the build to `~/.local/state/graft/log.sqlite` and apply
   archive retention.

A Stop (the Mac app's Stop button, or SIGTERM) ends the build where it is:
nothing is recorded as a failure, and under `--json` the last event is
`{"type": "build-end", "ok": false, "stopped": true, "error": null, ...}`.

## Build history and retention

```bash
graft --builds [--builds-app example] [--builds-limit 20]
graft --build-log 42                # the saved log of build 42 (builds run with --json, as the app does)
graft --reveal-build 42             # show its archived IPA in Finder
graft --reinstall-build 42          # deliver that archive again, without rebuilding (same delivery mode only)
graft --prune-builds --dry-run      # what retention would remove
graft --prune-builds [--keep 1]     # apply it now
```

`--dry-run` previews only a build and `--prune-builds`. Every other command
refuses it, before doing anything, rather than running for real under a flag
that said it wouldn't.

A re-install goes out the way the archive was built: an unsigned
LiveContainer archive is never handed to `ideviceinstaller`, and a
zsign-signed one is never pushed into LiveContainer. If the app's delivery
mode has changed since, rebuild it. For a build recorded before Graft 1.1
noted the mode, the archive itself says (zsign's embedded profile).

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
- A `--reinstall-build` of an archive that a recorded push shipped, byte for
  byte (usually the newest build), is compared like that push. Any other
  re-installed archive reads `stale`, because what went into it was not
  recorded.

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
signing:                                # paid only; leave it out for a free Apple ID
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
`--set-app-options` (with `--preferred-version`, `--display-name`,
`--[no-]keep-bundle-id`, `--[no-]strip-extensions`,
`--[no-]strip-supported-devices`, `--compression-level`, `--delivery`,
`--lc-bundle-id` / `--no-lc-bundle-id`),
`--set-settings` (with `--default-device`, `--archive-keep`,
`--signing-cert`, `--signing-profile`, `--signing-team-id`),
`--add-app-manual --name --bundle-id --display-name` for an app not on the
App Store, and `--remove-app`. `graft --settings` shows the global settings.

**Free Apple ID.** A config with no `signing:` section (or an empty one) is a
free Apple ID. `--set-settings --signing-*` refuses on it rather than writing
part of a section, which would leave a config that no longer loads.

To switch to a paid account, run `graft --init` (with `--config <path>` for a
config somewhere else) and answer yes. On a free config it asks only for the
`.p12` and the profile, then adds a `signing:` section to that config and
keeps its apps, repos and settings. With a paid account an app with no
`delivery:` key is a sideload app, so `--init` writes `delivery: livecontainer`
into each such app and says which; nothing moves until you change one with
`graft --set-app-options --app <name> --delivery sideload`. If you add the
section by hand instead, add `delivery: livecontainer` to those apps yourself.

To go the other way, run `graft --init` and answer no on a paid config: it
offers to switch that config to a free Apple ID, and on yes removes the
`signing:` section and keeps everything else. Apps with no `delivery:` key
then go into LiveContainer; one with an explicit `delivery: sideload` is left
as written, and `--init` and `--check` name it with the command that moves it.

`signing:` keys that end up at the top level (lost indentation) are an error
naming them, not a free config.

**Bundle id.** Some apps tie sign-in to their original bundle id;
`keep_bundle_id: true` (the default) preserves it. With `keep_bundle_id: false`
cyan renames it (to `new_bundle_id`, or the original plus `.plus`) in both
delivery modes, LiveContainer included.

**Per-app profiles (paid only).** `auto_profile: true` has Graft create and
cache a profile for the app through the App Store Connect API (set up with
`graft --setup-portal`); `profile: <path>` points one app at a specific
profile. Both apply to sideload builds only.

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

The Mac app reads these from your login shell when it starts, and again when
you press Check again on the setup screen. It runs the shell as an interactive
login shell, given 5 seconds; csh and tcsh run as interactive shells, which
read `~/.tcshrc` or `~/.cshrc` but not `~/.login`. So a variable exported in
`~/.zshrc` or `~/.zprofile` reaches the CLI it runs, as do your `PATH`
additions and `XDG_*` directories. `PYTHON*` variables and the colour switches
(`FORCE_COLOR`, `TTY_COMPATIBLE`, `TTY_INTERACTIVE`, `PY_COLORS`,
`CLICOLOR_FORCE`) are never passed on. If the shell does not answer, the app
carries on without them (or, on a later Check again, with what it read
before), and a startup file that hangs is stopped along with
everything it started.

### Files

```
~/.config/graft/
├── config.yaml
├── config.yaml.lock
├── AppleDevelopment.p12
├── wildcard.mobileprovision
├── AuthKey_<KEY_ID>.p8  # App Store Connect API key, from --setup-portal
├── profiles/            # profiles made through that API
└── tweaks/              # .deb and .dylib files

~/.cache/graft/
├── ipas/                # staged decrypted IPAs (never pruned)
├── built/               # archived builds (newest archive_keep per app version)
└── artwork/             # cached app icons

~/.local/state/graft/
├── log.sqlite           # build history and delivery records
├── build-logs/          # saved event log of each --json build (so every build from the app)
├── repos/               # cached APT repository indexes
└── tweak-rollback/      # previous versions kept by --update-tweak

~/.local/share/graft/    # tools installed for Graft (UnfairPlay, frida-ios-dump)
```

These four `graft` directories are owner-only (0700): Graft creates them that
way and tightens them on every run if they are not. Staged IPAs, archived
builds, build logs and the `.p12` and profile `--init` copies are written 0600.
Graft changes nothing outside these directories, and leaves a `graft` that is
a symlink you made alone.

## Running builds on this Mac

`graft example --for-playcover` also makes the build loadable under PlayCover
on an Apple silicon Mac, and `graft --install-to-mac <filename>` hands a staged
IPA, or an archived build from `~/.cache/graft/built/`, to PlayCover. Some
tweak libraries import iOS private frameworks that macOS does not have; for
these Mac builds Graft bundles a minimal stand-in instead. Phone builds are
never changed this way.

## Scripting

`--json` turns a command's output into newline-delimited JSON events on
stdout (all but `--version` and the interactive ones: `--init`,
`--setup-portal` and the password prompts): every stdout line is one JSON
object, and any command's narration and warnings go to stderr. Notices from before the command runs (a directory
moved to its new name, an old environment variable still set) are `log`
events with `"source": "startup"`. This is how the Mac app talks to the CLI.
`--list` starts
with one `account` event, `{"type":"account","account":"free"}` or `"paid"`,
even when no apps are configured; the `app` events after it and the
`preflight-end` event from `--check` carry the same `account` field.

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
| `no LiveContainer found on the device` | LiveContainer is not installed, or not under a bundle id starting `com.kdt.livecontainer`. With a free Apple ID, install it through SideStore or iloader; see [With a free Apple ID](#with-a-free-apple-id). With a paid account, `graft --lc-install` also works. |
| `the device listed no apps at all, which is how a locked phone answers` | The phone is locked, or has not been unlocked since it restarted. Unlock it, stay on the home screen, and run the command again. |
| `... already has a Documents/<app>.ipa that Graft did not push` | LiveContainer's Documents already holds a file with the name the push uses, and neither a record nor its bytes say Graft put it there (it may be your own copy). Rename or remove it in the Files app (On My iPhone > LiveContainer), then build again. |
| `lc_bundle_id ... is not installed` | The app is pinned to a LiveContainer that is not on the device. Pin one the error lists with `graft --set-app-options --app <name> --lc-bundle-id <id>`, or drop the pin with `--no-lc-bundle-id`. In the Mac app, use the LiveContainer picker in the app's Edit sheet. |
| `couldn't reach usbmuxd` | macOS's service for talking to iPhones did not answer. Reconnect the phone; if it persists, restart the Mac. |
| `N LiveContainers are installed and Graft will not guess which to use` | There is no single primary: two LiveContainers installed by two Apple IDs, say, or a jailbreak's plain `com.kdt.livecontainer` next to an installer's `com.kdt.livecontainer.<TEAMID>`. Pin the app to one of the ids the error lists: `graft --set-app-options --app <name> --lc-bundle-id <id>`, or the LiveContainer picker in the Mac app's Edit sheet. |
| `... needs a paid Apple Developer Program membership` | The config has no `signing:` section, so it is set up for a free Apple ID, and the command needs a paid one. Use `delivery: livecontainer`, or, with a paid membership, run `graft --init` and answer yes: it adds the `signing:` section to your existing config and keeps your apps (see [Free Apple ID](#configuration) under Configuration). |

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
