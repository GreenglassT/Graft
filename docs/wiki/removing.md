# Removing Graft

What Graft keeps outside the app, and how to remove it from the Help menu or in
Terminal.

Dragging Graft.app to the Trash removes the app and everything inside it. What
it made outside the app stays until you remove it.

## In the app

Help › Remove Graft's Data… lists all of it, with sizes. Check what to remove,
then press Remove…. It waits while Graft is building, installing, downloading
or setting something up. When it's done, it says what it removed: quit Graft,
then move Graft.app to the Trash. Removing can't be undone.

## In Terminal

```bash
graft --remove-data                          # list it all, with sizes; removes nothing
graft --remove-data --items all --dry-run    # what --items all would remove
graft --remove-data --items all              # every item marked "removed with all"
graft --remove-data --items all,passwords    # and your passwords
graft --remove-data --items cache,temp       # only these
```

## What it removes

This is the whole list, by the name `--items` takes:

| Item | What | Where |
|---|---|---|
| `config` | Your setup: `config.yaml`, the `.p12` and profile Graft copied in when you set it up, your App Store Connect key and the profiles made with it, and your tweaks. The files you picked during setup stay where they were. | `~/.config/graft` |
| `cache` | Staged IPAs, archived builds and app icons, often several GB | `~/.cache/graft` |
| `state` | Build history and logs, repository indexes, earlier versions of tweaks you updated, Graft's record of its App Store sign-in, the names of the iPhones and iPads Graft has seen, the PlayCover settings it keeps for your apps, which devices you stopped updating an app on, and which App Store versions your Apple ID was refused | `~/.local/state/graft` |
| `data` | frida-ios-dump, and tools a pipx install of Graft uses | `~/.local/share/graft` |
| `legacy` | `tweak-build` folders from before Graft's rename that Graft left beside its own, because both held files. Only when you name it. | `~/.config/tweak-build` and the like |
| `appstore-signin` | Your App Store sign-in: three items in your login keychain (service `ipatool-auth.service`, accounts `account`, `anisette-v4` and `kbsync-v1`) and ipatool's session cookies. Off when another ipatool made the sign-in. | the keychain, and `~/.ipatool` (or `$XDG_STATE_HOME/ipatool`) |
| `ipatool-cache` | ipatool's App Store components and its Unicorn emulator, about 320 MB | `~/Library/Caches/ipatool`, and `ipatool-*` folders in your temp folder |
| `unfairplay-model` | The decryption model Set Up Decryption made | `~/Library/Application Support/unfairplay` |
| `passwords` | Your `.p12` and SSH passwords. Only when you name it. | your login keychain, service `graft` (or `tweak-build` from before Graft's rename), accounts `p12-<team id>` and `ssh-<device UDID>` |
| `graft-link` | `graft` for Terminal, when it's a link into Graft.app | `~/.local/bin/graft` |
| `app-settings` | The app's own settings. The app removes these; in Terminal, `defaults delete com.greenglasst.graft`. | `~/Library/Preferences/com.greenglasst.graft.plist` |
| `update-log` | The update log: what Graft wrote each time it updated itself, and the last rebuild's output for an app built from source | `~/Library/Logs/Graft` |
| `temp` | Temporary folders a stopped build, signing or install left behind, more than an hour old, and the rebuild marker of an app built from source | your temp folder (`$TMPDIR`), `~/Library/Caches/Graft` |

Keychain items are found and removed by their service and account only, and
nothing is asked of the keychain while it's locked. If you use ipatool or
UnfairPlay on their own, they use the same sign-in, components and model, and
removing them affects those too.

## What stays

Never removed, and listed under "Not removed": anything `config.yaml` points
to outside these folders (`cache_dir`, `built_dir`, `tweaks_dir`, `state_dir`,
the signing files, the App Store Connect key and its profiles), a model folder
set by `appstore.unfairplay_models` or `UNFAIRPLAY_MODEL_DIR`, a `graft` folder
that is a link you made, and a `~/.local/bin/graft` that isn't Graft.app's (a
pipx one is removed with `pipx uninstall graft-ipa`).

If you set `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `XDG_STATE_HOME` or
`XDG_DATA_HOME`, the four `graft` folders are under those instead. Builds and
tweaks Graft moved to the Trash stay there until you empty it. The Mac App
Store app you set up decryption with, PlayCover and the apps you installed on
this Mac with it, and LiveContainer and the apps on your devices, are yours to
keep or delete.

## By hand

To do it by hand instead, delete what the table lists. For the keychain items,
search Keychain Access for `ipatool-auth.service` or `graft`.
