# CLI Reference

The `graft` commands, grouped by what they do.

Almost every command takes `--json` (see
[Scripting](cli-scripting.md#scripting)), and every one takes
`--config <path>`. `graft --help` lists every flag but `--describe` (see
[Scripting](cli-scripting.md#scripting)).

## Apps

```bash
graft --list                                       # apps: bundle id, pin, tweaks, delivery state, store (--json: account, mode)
graft --appstore-search "example"                  # iPhone apps that match, from this Mac's region's store
graft --appstore-search "example" --country GB --limit 10
graft --add-app https://apps.apple.com/app/id123456789 [--delivery livecontainer]
graft --add-app-manual --name example --bundle-id com.example.app --display-name Example --store-country GB
graft --remove-app example                         # cached IPAs and build history are kept
```

- `--appstore-search` needs no Apple ID. It asks the public iTunes Search API
  and sends only the search term and a country code. Its table ends with the
  `--add-app-manual` line for the store it searched: pass `--store-country` as
  it shows, so the app's update checks ask the same store.
- `--country` takes the two-letter code of a country with an App Store. A
  territory or `UK` is accepted as the store it uses (GB), with a note; a code
  with no store is refused. Without it, Graft uses the store of this Mac's
  region, or the US store if it can't read one or the region has none.
- `--limit` defaults to 25 and stops at 50. `--json` adds each result's icon,
  cached for a week in `~/.cache/graft/appstore-search-icons/`.

## App options

```bash
graft --set-app-options --app example --delivery livecontainer   # or sideload
graft --set-app-options --app example --store-country GB          # the store it's looked up in
graft --set-app-options --app example --lc-bundle-id com.kdt.LiveContainer2.ABCDE12345
graft --set-app-options --app example --no-lc-bundle-id           # drop the pin
graft --set-app-options --app example --auto-profile              # or --no-auto-profile
```

`--set-app-options` also takes `--preferred-version`, `--display-name`,
`--[no-]keep-bundle-id`, `--[no-]strip-extensions`,
`--[no-]strip-supported-devices` and `--compression-level`.
`--auto-profile` on a LiveContainer app still saves it, with a warning that it
has no effect until the app is delivered by sideload.

## App Store

```bash
graft --appstore-status                     # ipatool installed and signed in? UnfairPlay enrolled?
graft --appstore-login                      # asks for the Apple ID, the password, then the code if Apple asks
graft --appstore-logout
graft --appstore-check                      # App Store version vs. your staged IPA, per app
graft --appstore-get --app example          # download, decrypt and stage the latest version (nothing if it's already staged)
graft --appstore-versions --app example     # the versions the App Store has for your Apple ID, newest first, with their ids
graft --appstore-get --app example --version-id <id>   # get that version, and hold the app at it
graft --appstore-claim --app example        # Get (free): only for an app the App Store lists at 0
```

- `--appstore-status` prints the Terminal commands for whatever is missing,
  with the paths filled in.
- `--appstore-login` reads the Apple ID, the password and the code from stdin,
  and asks for each in a terminal (the password without echo). None of them is
  ever an argument.
- `--allow-unverified` with `--appstore-get` or `--provide-ipa` stages a decrypt
  UnfairPlay couldn't verify, with a warning.
- `--again` with `--appstore-get` downloads even when that version is already
  staged.
- `--appstore-versions` reads the number and date of ten versions at a time,
  starting at `--offset` (`--limit` reads up to 50), and remembers them. The
  rest are listed by id.
- `--version-id` holds the app at the version it gets: `preferred_version` pins
  it, and `--appstore-check` doesn't call a newer one an update. A plain
  `--appstore-get` lets go of the hold. See
  [Get an older version](app-store.md#get-an-older-version).

To set up decryption in Terminal, list the apps that can do it, save the
script, run it as root, then finish as yourself:

```bash
graft --appstore-enroll-candidates
graft --appstore-enroll-command --app "/Applications/<that app>.app" > enroll.sh
sudo /bin/sh enroll.sh
graft --appstore-enroll-finish <the folder it prints>
```

A `graft` that isn't Graft.app's own (a pipx install, or a Debug build), or an
ad hoc signed Graft.app, has nothing to check root's UnfairPlay against, so
`--appstore-enroll-command` refuses unless you pass `--allow-unverified-helper`.

## Other IPA sources

```bash
graft --provide-ipa ~/Downloads/Example.ipa --app example
graft --decrypt-check                       # what, if anything, is blocking a dump right now
graft --decrypt com.example.app             # dump an installed app by bundle id
graft --decrypt-frontmost                   # dump whatever app is on screen
graft --decrypt com.example.app --decrypt-backend bagbak
graft --set-ssh-password                    # the device's root SSH password, into the Keychain
graft --staged-ipas                         # every staged IPA, and anything stripped from it
graft --cached-ipa example                  # which IPA a build of "example" would use
graft --delete-staged com.example.app_1.2.3.ipa
```

`--decrypt-mode` accepts only `all`, the default: bagbak's other modes dump
bare binaries or extensions, which a build can't use. Pass
`--keep-encrypted-extensions` to fail a dump instead of dropping extensions it
couldn't decrypt.

## Tweaks and repos

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
graft --add-repo https://repo.example.com
graft --repos
graft --repo-packages https://repo.example.com [--refresh]
graft --remove-repo https://repo.example.com
```

A `--fetch` by package id is checked against Canister's published hash, when
Canister has one. A URL is checked only if you pass `--fetch-sha`.

## Building

```bash
graft example                       # build and deliver
graft example other                 # several apps in turn
graft example --dry-run             # show what would happen without doing it
graft example --skip-install        # build (and sign, for sideload) only
graft example --device <UDID>       # a specific device instead of the default
graft example --for-playcover       # also make the build loadable on this Mac under PlayCover
graft --install-to-mac <filename>   # hand a staged IPA or an archived build to PlayCover
graft example --export-ipa ~/Desktop/Example.ipa   # inject the tweaks, save the IPA unsigned
graft example -v                    # verbose, and keep the temp directory
```

`--export-ipa` signs nothing and touches no device (see
[Exporting an IPA](building-and-delivery.md#exporting-an-ipa)). It takes one
app, refuses `--device`, `--skip-install` and `--for-playcover`, and won't
replace a file that's already there unless you add `--overwrite`. Under
`--json` its last event before `build-end` is `ipa-exported`, with the file's
`path`.

Before it builds, it checks where the file goes, and that it can write a file
in that folder. It writes the IPA to a hidden `.<name>.<random>.partial` file
beside the path, then moves it into place, so a failed or stopped export
leaves nothing behind. If graft is killed outright mid-write, that hidden file
can be left there: it's safe to delete. The file's permissions are a new
file's, as your umask sets them.

How an export ends, by its exit status and, under `--json`, `build-end`'s
`reason`. The reason is null when the export worked, was stopped, or failed
another way (cyan failing, say), which the exit status tells as it does for a
build.

| Exit | `reason` | Meaning |
|---|---|---|
| 0 | null | The IPA was written |
| 2 | `exists` | A file is already at the path. `--overwrite` replaces it |
| 2 | `bad-destination` | The path is a folder, its folder isn't there or is one of Graft's own, or it starts with `~name` for a user this Mac doesn't have |
| 1 | `unwritable` | graft can't write in that folder: its permissions, macOS's privacy controls, or a read-only disk |
| 1 | `no-space` | The disk is full |
| 1 | `write-failed` | Writing the file failed another way |
| 4 | `no-ipa` | No IPA is staged for the app |

`--skip-decrypt-check` skips the decryption check. A Stop (SIGTERM) ends the
build where it is: nothing is recorded as a failure, and under `--json` the
last event is
`{"type": "build-end", "ok": false, "stopped": true, "error": null, ...}`.

## Build history and delivery state

```bash
graft --builds [--builds-app example] [--builds-limit 20]
graft --build-log 42                # the saved log of build 42 (builds run with --json, as the app does)
graft --reveal-build 42             # show its archived IPA in Finder
graft --reinstall-build 42          # deliver that archive again, without rebuilding (same delivery mode only)
graft --prune-builds --dry-run      # what retention would remove
graft --prune-builds [--keep 1]     # apply it now
graft --delivery-state [--app example]   # would building now produce something different?
graft --reconcile [--app example]        # check the device first, then report
```

`--dry-run` previews only a build, `--export-ipa`, `--prune-builds`,
`--init --account` and `--remove-data --items`. Every other command refuses
it, before doing anything, rather than running for real under a flag that
said it wouldn't.

## LiveContainer

```bash
graft --lc-status                     # LiveContainers on each device, who manages them, upstream version
graft --lc-install                    # paid only: install or update all three (default device, or --device)
graft --lc-install --instances 1      # only the primary (comma list: 1,2,3)
graft --lc-install --channel stable   # stable instead of nightly (nightly is what upstream recommends)
graft --lc-install --force            # replace LiveContainers AltStore or SideStore manages
graft --lc-push-cert                  # paid only: put your .p12 in the primary's Documents
graft --lc-clean --cert-only          # remove the certificate once imported
graft --lc-clean                      # remove the certificate and the IPAs Graft pushed, wherever each went
```

`--lc-status` and `--lc-clean` work with either account. `--devices` names the
LiveContainer a push would use.

## Devices

```bash
graft --devices                     # connected devices over USB and Wi-Fi
graft --device-info [--device <UDID>]
graft --device-apps [--with-icons]  # user apps on the device
```

## Settings and account

```bash
graft --settings                    # the global settings
graft --set-settings --default-device <UDID>      # '' to unset
graft --set-settings --archive-keep 3
graft --set-settings --signing-cert <file> --signing-profile <file> --signing-team-id ABCDE12345
graft --set-p12-password
graft --set-portal --asc-key-id ABCDE12345 \
  --asc-issuer-id 00000000-0000-0000-0000-000000000000 \
  --asc-key-file ~/Downloads/AuthKey_ABCDE12345.p8
graft --setup-portal                # the same key, with questions
graft --clear-portal                # remove it (the .p8 stays)
graft --check-update                # is a newer Graft out? (asks GitHub; downloads nothing)
graft --download-update             # download it into Graft's cache and check it (installs nothing)
graft --remove-data                 # what Graft keeps outside Graft.app, with sizes
graft --remove-data --items all     # remove it (see Removing Graft); add --dry-run to preview
```

Graft.app installs a download it checked with `--install-update`, which it
runs as it quits; see [Updating Graft](updating.md).
[Removing Graft](removing.md#what-it-removes) lists every item `--items` takes.

`--set-settings --signing-*` refuses on a free config rather than write part of
a `signing:` section, which would leave a config that no longer loads. To
switch account in Terminal, run `graft --init` again: on a free config it
offers to add a paid account, and on a paid one to switch to a free Apple ID,
keeping apps, repos and settings either way.
