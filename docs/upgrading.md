# Upgrading from Graft 1.x

Graft 2.0 carries its own CLI and tools inside Graft.app, and does setup, App
Store sign-in and decryption set-up in the app. Your config, apps, tweaks,
staged IPAs and build history carry over as they are. This page covers what
changes for someone coming from 1.0 or 1.1, and how to go back.

**The short version:**

1. Check your Mac: 2.0 needs macOS 14 or later. Graft 2.0 needs a Mac with
   Apple silicon. Graft 1.1 may work on an Intel Mac running macOS 15 or
   later, but it was never tested there.
2. Replace `Graft.app` in `/Applications` with 2.0's, and open it.
3. Settings › App Store › **Sign In…** once. It replaces the sign-in your own
   ipatool made under 1.x.
4. Optional: remove the pipx `graft`, then Settings › Command-line tool ›
   **Install graft for Terminal**.
5. Optional: remove the Homebrew tools 2.0 no longer uses.

## Your config, apps and files

2.0 uses the same folders as 1.x: `~/.config/graft`, `~/.cache/graft`,
`~/.local/state/graft` and `~/.local/share/graft`. It reads your
`config.yaml` as it is, and doesn't rewrite it when it starts. Because a
config exists, the first-run setup is skipped and the app opens on your apps.

Most settings mean what they meant in 1.x. These are the differences.

**`appstore.ipatool` and `appstore.unfairplay`.** 2.0 runs the ipatool and
UnfairPlay inside Graft.app unless you chose another copy.
- A value equal to 1.x's default counts as not set: `~/.local/bin/ipatool`
  for ipatool, `~/.local/share/graft/unfairplay/unfairplay` for UnfairPlay.
  Graft.app's own copies run instead. For ipatool, `graft --appstore-status`
  says so; for UnfairPlay, it marks the copy that runs "(bundled)".
- A path that isn't there any more, because you removed that ipatool or
  UnfairPlay as [Tools you no longer need](#tools-you-no-longer-need) allows,
  doesn't count either: Graft.app runs its own copy, and
  `graft --appstore-status` notes the setting it passed over. Remove the key
  from `config.yaml` to stop the note.
- Any other full path that is there still wins, for downloads and decrypts
  alike. To keep using your own ipatool, put it somewhere other than
  `~/.local/bin/ipatool` and set `appstore.ipatool` to that path. Signing
  in with it in Graft first deletes the sign-in Graft.app's own ipatool
  made, so going back to that one means signing in again. A sign-in
  another ipatool made is saved over instead, and macOS may ask for your
  login keychain password while it is.
- Setting up decryption in Graft.app always uses Graft.app's own UnfairPlay,
  whatever the config says, because that step runs it as root.
- A relative path (one that doesn't start with `/` or `~/`) is now ignored:
  the config still loads, each run warns that the setting isn't a full path,
  and Graft reads it as not set. Graft.app then runs its own copy, and a pipx
  `graft` looks in the usual places. 1.x resolved it against whatever folder
  the CLI started in. Fix the path, or remove it, to stop the warning.
- `appstore.unfairplay_models` works as before, but for the same rule: a
  relative path is ignored with a warning, and the default models folder is
  used.

**cyan and zsign.** config.yaml never had a setting for either. 2.0 runs
Graft.app's own copies, so the ones you installed with pipx and Homebrew are
no longer used by the app.

**Apps added in 1.x have no `store_country`.** 2.0 records the App Store an
app came from, and looks it up there. An app with none is now looked up in
the store of this Mac's region first, then in the US store, which is the only
one 1.x asked. See [App Store lookups](#app-store-lookups).

**`app_store_connect:`.** Setting a key in 2.0 replaces only the key and its
device list. A `profile_cache_dir` you set by hand stays, and the list holds
every enabled iPhone, iPad and iPod touch, not the first three. To keep a list
you curated, add `device_udids_pinned: true` to the section.

**New keys.** 2.0 may write `store_country` on an app. It reads
`device_udids_pinned` in `app_store_connect:`, which only you add, and keeps
it when it writes that section. 1.1 ignores both and keeps them when it edits
the config.

**Passwords in the Keychain.** Your `.p12` password and your device's SSH
password stay where 1.x stored them, and 2.0 reads them without asking. One
1.1 bug is fixed: storing a password again over one already there asked for
your login keychain password, and replaced it even when you canceled. 2.0
doesn't ask.

## The pipx graft from 1.x

Graft.app 2.0 never runs a pipx `graft`. It always runs the copy inside it.
You can keep the pipx one for Terminal, or replace it.

**To replace it** with the app's own:

```bash
pipx uninstall graft-ipa
```

Then, in Graft.app, Settings › Command-line tool › **Install graft for
Terminal**. That links the app's `graft` into `~/.local/bin`, where pipx kept
its own, and the command then updates with the app. If you'd rather keep the
pipx install around, the same section offers **Replace it…** instead, which
repoints `~/.local/bin/graft` at the app and leaves the pipx copy installed.

**If you keep it,** both read and write the same config and files.
- Terminal runs whichever `graft` comes first on your PATH. Settings ›
  Command-line tool says which one that is.
- A 1.1 `graft` asks every app's version and icon in the US store only.
- 1.1's `graft --setup-portal` rewrites the whole `app_store_connect:`
  section: it keeps only the first three enabled devices, and drops
  `device_udids_pinned` and a `profile_cache_dir` you set by hand.
- 1.1's App Store commands run your own ipatool, which can't read the sign-in
  Graft.app makes without a Keychain dialog. See
  [Your App Store sign-in](#your-app-store-sign-in).
- `pipx reinstall graft-ipa` brings a pipx install up to 2.0, unless you
  pinned it to `@v1.1` to go back (see [Coming back to 2.0](#coming-back-to-20)).
  It's still a separate copy: it runs the tools on your PATH and your own
  ipatool, never Graft.app's.

## Tools you no longer need

Graft.app 2.0 brings its own copy of each tool it uses, or does the job
itself. None of these is needed for anything the app does:

| 1.x needed | For | 2.0 |
|---|---|---|
| Python 3.11+ and pipx | the CLI | Graft.app carries its own Python and CLI. Going back to 1.1 needs them again. |
| `cyan`, installed with pipx | injecting tweaks | bundled |
| `zsign`, from Homebrew | Sideload signing | bundled |
| `ideviceinstaller` and `idevice_id`, from libimobiledevice | installing and finding devices | done through pymobiledevice3, inside the CLI |
| `otool`, from the Xcode Command Line Tools | checking an IPA is decrypted | done by Graft's own Mach-O reader |
| ipatool 2.8.0 at `~/.local/bin/ipatool` | App Store downloads | bundled |
| UnfairPlay, built with Go into `~/.local/share/graft/unfairplay/` | decrypting App Store downloads | bundled |
| full Xcode | a PlayCover build whose tweak links an iOS private framework | Graft.app carries the stand-in framework prebuilt |

You can remove them if nothing else of yours uses them, for example
`pipx uninstall cyan`, `brew uninstall zsign ideviceinstaller`, and
`~/.local/share/graft/unfairplay/`. Keep them if you keep a pipx `graft`,
which still needs them.

Before removing your own ipatool, read
[Your App Store sign-in](#your-app-store-sign-in): it's what holds your 1.x
sign-in.

**What 2.0 still uses, if you use it:** decrypting on a jailbroken device
still needs tools you install yourself.
- `iproxy`, from libimobiledevice (`brew install libimobiledevice`), for the
  SSH tunnel to the device. So if you dump apps from a jailbroken device, keep
  libimobiledevice.
- frida-ios-dump, with its own virtualenv and frida 16, and bagbak, as in 1.x.
  The 2.0 CLI has no frida of its own; it asks frida-ios-dump's. So if you
  used only bagbak, set frida-ios-dump up too: without it, Decrypt the app on
  screen (`--decrypt-frontmost`) doesn't work, and the decrypt check can't
  reach frida-server.
- PlayCover, to run builds on this Mac.

## Your App Store sign-in

**What 1.x left.** Under 1.x you signed in with your own ipatool in Terminal
(`ipatool auth login`). ipatool keeps that sign-in in your login keychain as
three items under the service `ipatool-auth.service`: `account`,
`anisette-v4` and `kbsync-v1`. macOS lets only the program that made an item
read it without asking, and Graft.app's ipatool is a different program from
yours.

**Before you sign in.** Settings › App Store reads "signed in by another
ipatool (Graft 1's?): sign in again". Graft looks only at whether the items
are there, never inside them, so opening Settings shows no Keychain dialog.
Get latest from App Store refuses until you sign in, and says why.

**Signing in.** Press **Sign In…**. The sheet says what will happen: "This
also signs out any ipatool you installed yourself; it will need to sign in
again if you use it in Terminal."
1. Graft deletes those three items, by their exact service and account name,
   and nothing else. Deleting them shows no dialog.
2. Graft's ipatool then signs in. Your Apple ID and password, and the code if
   Apple asks for one, are typed into its prompts and go only to Apple,
   through that ipatool. Graft saves none of them. ipatool keeps the new
   sign-in, password included, in your login keychain, and reads it from then
   on without asking, across Graft updates too.

If your login keychain is locked, or doesn't answer, Graft says it couldn't
check it and doesn't sign in. Unlock it in Keychain Access and try again.

**The first App Store action** on a Mac (usually the first sign-in)
downloads ipatool's App Store components (about 320 MB, skipped if a 1.x
ipatool already downloaded them) and its Unicorn emulator (a few MB), once.
See [What Graft connects to](network.md).

**Your own ipatool afterward.** In Terminal, it's signed out. If you run it,
macOS shows two Keychain dialogs about the items Graft's ipatool made, because
they aren't its own: "ipatool wants to use your confidential information
stored in “ipatool-auth.service”…" and "ipatool wants to access key
“ipatool-auth.service”…". Both ask for your login keychain password, even to
Allow. If you Deny them, your ipatool prints "…could not be found in the
keyring", and Graft.app stays signed in. Signing your ipatool in over those
items can ask for the password too. If you don't need your own ipatool any
more, leave it signed out.

**2.0 test builds.** If you signed in with a 2.0 test build, sign in once
more. Their record of which items Graft's ipatool made is in an older format,
which 2.0 doesn't trust, so Settings reads "signed in by another ipatool".

## Setting up decryption

1.x set up decryption in Terminal, with `sudo unfairplay setup --app
"/Applications/<app>.app"`. 2.0 does it in Settings › App Store › **Set Up
Decryption…**, behind macOS's administrator prompt. See
[Set up decryption](../README.md#set-up-decryption).

**If you set up decryption in 1.x,** you don't need to again. Both versions
run UnfairPlay 0.4.0 and keep its model in the same place
(`~/Library/Application Support/unfairplay/models`). Settings › App Store
shows UnfairPlay as enrolled, as long as the model is yours: a model or folder
another user owns, such as root, is reported with what to do. Setting up again
on the same Mac with the same Apple ID makes the same model, byte for byte, so
there's nothing to gain from it.

**The Apple ID must match.** The model only decrypts apps downloaded with the
Apple ID that installed the Mac App Store app it came from. Use the same
Apple ID in Graft as in the Mac App Store. If you sign in to Graft with a
different Apple ID from the one you used in 1.x, press **Set Up Again…** with
an app installed under the new one. Older models stay beside the new one.

## App Store lookups

Every lookup of an app now goes through one rule: the update check, its icon,
and the price Get (free) checks.
- An app with a `store_country` is looked up in that store only.
- An app with none is looked up in the store of this Mac's region (System
  Settings › General › Language & Region), then in the US store. The first
  store that sells it answers, for that lookup only; nothing is written to the
  config. When neither has it, the error names both.

Apps added in 1.x have no `store_country`, so:
- On a Mac whose region uses the US store, or whose region Graft can't read,
  nothing changes.
- Elsewhere, an app your region's store sells is now checked there, so its
  version, price and icon come from that store. An app only the US store
  sells is still found, in the US store.

To pin an app to one store, set App Store in its Edit sheet, or run `graft
--set-app-options --app <name> --store-country US`.

## Going back to 1.1

**Graft 1.1 needs macOS 15 or later:** its app doesn't open on macOS 14.
Graft 2.0 needs a Mac with Apple silicon. Graft 1.1 may work on an Intel Mac
running macOS 15 or later, but it was never tested there.

Graft 1.1 stays available:
- the app: `Graft.zip` on the
  [v1.1 release](https://github.com/GreenglassT/Graft/releases/tag/v1.1);
- its CLI, which the 1.1 app runs for everything. It needs Python 3.11 or
  later and pipx, which 2.0 let you remove. Homebrew's pipx brings a Python
  with it:

  ```bash
  brew install pipx            # if you removed it, or never had it
  pipx ensurepath              # then open a new Terminal window
  pipx install --force "git+https://github.com/GreenglassT/Graft@v1.1"
  ```

  Keep the `@v1.1`. The 1.1 app's own setup screen and its release page say
  `pipx install git+https://github.com/GreenglassT/Graft`, with no tag: since
  2.0, that installs 2.0's CLI, which the 1.1 app was never tested with.
- the tools it needs (cyan, zsign, libimobiledevice and ideviceinstaller, the
  Xcode Command Line Tools, and for App Store downloads ipatool 2.8.0 and
  UnfairPlay), which its own
  [README](https://github.com/GreenglassT/Graft/blob/v1.1/README.md) lists.

Replace `Graft.app` in `/Applications` with 1.1's. The 1.1 app looks for
`graft` in `~/.local/bin` first. If you installed `graft` for Terminal from
2.0, its link was there, and the `--force` in the install command above has
already replaced it with the 1.1 CLI's, so there's nothing to remove.

**Staying on 1.1.** If you never moved to 2.0 and installed 1.1's CLI as its
own README says, with no tag, `pipx upgrade graft-ipa` and `pipx reinstall
graft-ipa` now bring 2.0's CLI in under your 1.1 app. Run the `pipx install
--force …@v1.1` command above before either: pipx then keeps the 1.1 CLI (see
[Coming back to 2.0](#coming-back-to-20)).

**What 1.1 makes of 2.0's state:**

| 2.0 left | 1.1 |
|---|---|
| `config.yaml` | Loads and edits it. It ignores `store_country` and `device_udids_pinned`, keeps them when it writes, and asks every app's version in the US store. Its `--setup-portal` rewrites `app_store_connect:` as above. A config that 2.0 wrote is otherwise one 1.1 understands. |
| Staged IPAs, archived builds, tweaks, repos and `log.sqlite` | Uses them as they are. |
| The decryption model | Uses it as it is: same UnfairPlay version, same folder. |
| The sign-in record, `~/.local/state/graft/appstore-signin.json` | Doesn't know it, and ignores it. |
| The App Store sign-in 2.0's ipatool made | Can't read it without asking. 1.1 runs your own ipatool, so macOS shows two Keychain dialogs when it reads those items, in Settings › App Store and on a get, each asking for your login keychain password (see [Your own ipatool afterward](#your-app-store-sign-in)), and signing your ipatool in over them can ask for it too. |

To give 1.1 a clean start with the App Store, delete those items first, then
sign your own ipatool in as 1.1 says:

```bash
security delete-generic-password -s ipatool-auth.service -a account
security delete-generic-password -s ipatool-auth.service -a anisette-v4
security delete-generic-password -s ipatool-auth.service -a kbsync-v1
```

### Coming back to 2.0

Graft.app 2.0 reads "signed in by another ipatool" again, and Sign In…
replaces that sign-in as described above.

pipx remembers the `@v1.1`, so `pipx reinstall graft-ipa` and `pipx upgrade
graft-ipa` keep the 1.1 CLI. To un-pin it, either remove it and use the app's
own:

```bash
pipx uninstall graft-ipa
```

then Settings › Command-line tool › **Install graft for Terminal**; or, to
keep a pipx CLI of 2.0, install it again without the tag:

```bash
pipx install --force git+https://github.com/GreenglassT/Graft
```
