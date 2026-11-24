# Changelog

What changed in each release of Graft, newest first. Each release's download
is on [GitHub Releases](https://github.com/GreenglassT/Graft/releases).

## 2.0 (2026-11-23)

Graft no longer needs Terminal.

- **Download it, open it, set it up in the app.** Graft.app carries its own
  `graft` CLI, with its own Python, and every tool it uses: cyan, zsign, ldid,
  ipatool, UnfairPlay and the rest. There's no Homebrew, pipx, Python, Go,
  Xcode or libimobiledevice to install, except libimobiledevice for
  decrypting on a jailbroken device.
- **Sign in to the App Store in the app.** Settings › App Store › Sign In…
  takes your Apple ID, your password and, if Apple asks for one, the code it
  sends to your devices. They go only to Apple, through the ipatool inside
  Graft.app, and Graft saves none of them.
- **Get (free).** When your Apple ID doesn't have an app yet and it's free, Get
  latest from App Store in the app's Source IPA section becomes Get (free). It
  asks first, then adds the app to your purchases at no cost and downloads it. A paid app shows Open in App Store instead; Graft
  never buys anything.
- **Setup, decryption and account changes happen in the app.**
  - The first run asks whether you have a paid Apple Developer account. With
    one, you choose your `.p12` and profile and see the team and devices before
    Graft sets anything up.
  - Settings › App Store › Set Up Decryption… enrolls this Mac for App Store
    decryption, behind macOS's administrator prompt. Graft explains the step
    first and never sees the password. Set Up Again… redoes it after you change
    Apple ID.
  - Settings switches between a free Apple ID and a paid account, and replaces
    a paid account's certificate and profile when they expire, keeping your
    apps, repos and settings.
- **App Store search.** Add App opens on Search: type an app's name, pick it,
  and add it. No URL to paste, and no Apple ID needed to search.
- **macOS 14 or later.** Graft.app now opens on macOS 14 Sonoma as well as
  later versions. Graft 2.0 needs a Mac with Apple silicon. Graft 1.1 may work
  on an Intel Mac running macOS 15 or later, but it was never tested there.
  Graft 1.1 needs macOS 15;
  [Going back to 1.1](docs/upgrading.md#going-back-to-11) installs its CLI
  with `@v1.1`.

### Also in 2.0

- Settings › Command-line tool › Install graft for Terminal links the app's own
  `graft` into `~/.local/bin`, so the command updates with the app.
- Each app remembers the App Store it came from, and is checked for updates,
  its icon and its price there. Add App's Search has a Store picker, and Edit
  App can change an app's store.
- The App Store Connect key is set in Settings, and "Create the provisioning
  profile automatically" is in each app's Edit sheet.
- Builds has "Clean up old builds…", which applies your archive setting now.
- A decrypt UnfairPlay can't verify offers "Use it anyway…", which says what
  that risks first.
- Builds for this Mac under PlayCover no longer need Xcode: Graft.app carries
  the stand-in framework some tweaks need.
- Before zsign signs an app, Graft warns when its profile can't grant what the
  app asks for (App Groups, iCloud, push and the like), and says which are
  likely to crash it.
- A phone that has never trusted this Mac is told to tap Trust, and Graft
  waits a minute for it, instead of failing.
- Settings shows the version of the app and of the CLI it runs. Help › Licenses
  lists every bundled component, and each release page carries the source
  archive.

### Fixes

- Re-saving a password no longer asks for your keychain password. In 1.1,
  storing a `.p12` or SSH password over one already there asked for your login
  keychain password, and replaced it even when you canceled.
- The three known issues in 1.1 are fixed:
  - renaming an app's bundle id removes its Apple Watch app, which can't
    follow it, so the install no longer fails;
  - builds with a paid account no longer print a "PKCS#12 … BER" warning;
  - cyan's color codes no longer show in the build log.
- A Stop during a sideload install ends promptly, and removes the IPA it
  staged on the phone.
- A phone that goes silent during an upload or an install is given up on after
  a minute, with a plain message, instead of hanging. A pulled cable gets a
  plain message too. Installing LiveContainer itself (`--lc-install`) is the
  exception: once Graft has asked you to close LiveContainer on the device, it
  waits up to 15 minutes for that.
- A push into LiveContainer lands whole or not at all. A cut-off push leaves
  nothing under the app's name, and the next push or Clean up removes what it
  left.
- Automatic provisioning puts every enabled iPhone and iPad in the profile,
  not the first three. A cached profile is reused only if it lists your current
  certificate and team, and Graft checks the key's team before it changes
  anything at Apple.
- Setting an App Store Connect key no longer rewrites the rest of the
  `app_store_connect:` section.
- An archive left by a failed install no longer takes a keep slot from a build
  you can re-install, and a clean-up never moves an archive a build is about to
  deliver.
- A failed paid setup, or a failed certificate replace, puts every file and
  password back as it was.
- A locked or unanswering login keychain gives a plain message after a minute,
  instead of hanging.
- The decryption check reads every binary itself, fat64 ones included, and
  never counts one it can't read as decrypted.
- App Store links with no app name in them, `geo.itunes.apple.com` links and
  `itms-apps://` links are accepted, and older `itunes.apple.com/gb/...` links
  keep their store.
- Warnings from saving an app's options or an App Store Connect key show in the
  app instead of being lost.

### Changed

- Graft.app always runs the CLI inside it, never a `graft` installed with pipx.
  The pipx install still works on its own, with the tools you install.
- In config.yaml, an `appstore.ipatool` or `appstore.unfairplay` equal to 1.x's
  default counts as not set, so Graft.app's own copy runs, and so does one
  that isn't there any more, with a note in `--appstore-status`. A relative
  path there, or in `appstore.unfairplay_models`, is now ignored with a
  warning and read as not set. 1.x resolved it against the folder the CLI
  started in.
- An app with no store is looked up in the store of this Mac's region, then in
  the US store. 1.x asked only the US store.
- Signing in to the App Store in Graft replaces a sign-in your own ipatool made
  under 1.x. Your own ipatool then needs to sign in again if you use it in
  Terminal.
- The CLI no longer installs frida. Decrypting on a jailbroken device uses
  frida-ios-dump's own.
- The Graft.app download is an aggregate of separate programs, each under its
  own license. The CLI inside it is distributed under GPL-3.0-or-later, and
  Graft's own source code stays MIT.

Coming from 1.x: [Upgrading from Graft 1.x](docs/upgrading.md) covers your
config, your pipx `graft`, the Homebrew tools you no longer need, your App
Store sign-in, and going back to 1.1. Staying on 1.1? Pin its CLI to `@v1.1`
before any `pipx upgrade` or `pipx reinstall`, or they bring in 2.0's: see
[Going back to 1.1](docs/upgrading.md#going-back-to-11).

The first App Store action on a Mac (usually the first sign-in) downloads
ipatool's App Store components (about 320 MB, skipped if a 1.x ipatool
already downloaded them) from Apple and its Unicorn emulator from PyPI, and
can take several minutes. [What Graft connects to](docs/network.md) lists every host.

## 1.1 (2026-11-18)

Graft works with a free Apple ID.

- With a free Apple ID, Graft builds the tweaked IPA unsigned and pushes it
  into LiveContainer, which signs it when you import it. You need
  LiveContainer on the device, installed with SideStore, AltStore, iloader or
  Impactor. `graft --init` asks which kind of account you have, and switching
  to a paid account later keeps your apps, repos and settings.
- The Mac app's setup screen lists every missing tool with its install
  command, and missing tools show as a notice instead of a failed build.
- Finding your iPhone or iPad no longer needs libimobiledevice.
- An app can be pinned to one LiveContainer when the device has several.
- `graft --lc-clean` removes only files Graft pushed, and Graft won't overwrite
  a file in LiveContainer's Documents unless it knows the file is its own.
- `--dry-run` previews only a build and `--prune-builds`. Any other command
  refuses it instead of running for real.
- A stopped build shows as Stopped, adds nothing to Builds and leaves no
  temporary files behind.
- Your Apple ID data is removed from every IPA Graft builds or installs.
- Your `.p12` and SSH passwords no longer show in process listings, and
  Graft's folders and files are readable only by your user account.
- The Mac app picks up changes made in Terminal when you come back to its
  window, and says so when `config.yaml` can't be read.
- A locked phone is asked to unlock, instead of reporting "no LiveContainer
  found".
- The app works with csh and tcsh, or a shell that forces color, and no longer
  hangs on a slow shell startup file.
- For scripts, `--json` output is pure JSON for every command, and
  LiveContainer errors have their own exit code, 21.

Known issues, fixed in 2.0: a renamed bundle id failed on apps with an Apple
Watch app; paid builds printed a "PKCS#12 … BER" warning; some cyan lines
showed raw color codes in the app's build log.

On 2026-11-19 the 1.1 download was rebuilt without the build machine's file
paths and code-coverage data. Same version, no other changes.

## 1.0 (2026-11-17)

The first public release: the `graft` CLI, installed with pipx, and Graft.app,
which runs it. A paid Apple Developer account was required.

- Injects `.deb` and `.dylib` tweaks into a decrypted IPA with cyan, and
  delivers it into LiveContainer, keeping its bundle id, or signs it with
  zsign and installs it as a normal app.
- Gets the source IPA three ways: an IPA you have, a dump from your own
  jailbroken device (frida-ios-dump or bagbak), or the App Store with your
  Apple ID, through ipatool and UnfairPlay.
- Finds tweaks on canister.me and in APT repositories, and checks them for
  updates.
- Keeps a build history, archived builds per app version for rollback, and a
  delivery state that says when a device is behind your config.
- Installs, updates and signs LiveContainer itself, including side-by-side
  instances, and makes per-app profiles through App Store Connect.
- Runs builds on this Mac under PlayCover.
- Needed macOS 15 or later, Python 3.11 or later, and tools from Homebrew and
  the Xcode Command Line Tools.
