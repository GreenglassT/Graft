# Building & Delivery

Build & Install builds an app with its tweaks and pushes it into LiveContainer.
With a paid account, Sideload delivery installs it as a normal app instead.

Press **Build & Install**.
- **LiveContainer delivery:** Graft pushes the IPA into LiveContainer's
  Documents. Open LiveContainer on the device and import it from there. That's
  when LiveContainer signs it and the tweaks start loading.
- **Sideload delivery (paid only):** Graft signs the IPA with your
  certificate and installs it as a normal app.

To sign on the phone instead, or install with TrollStore, **Export IPA…** in
Build options saves the IPA unsigned. See [Exporting an IPA](#exporting-an-ipa).

Builds lists every build, with its log and archive. See [Building](#building).

## Delivery

Each app has a delivery mode, LiveContainer or Sideload. An app added without
one is LiveContainer with a free Apple ID and Sideload with a paid account.
Graft writes the mode whenever it adds an app, so an app keeps its mode if the
config later changes account. Change it in the app's Edit sheet.

### LiveContainer (recommended)

LiveContainer delivery builds the tweaked IPA unsigned and pushes it into
LiveContainer's `Documents` folder over USB or Wi-Fi. You then import it
inside LiveContainer, which runs it as a guest app.

Why this is the main path: a guest app keeps its **original bundle id**, and
some apps tie sign-in to their original bundle id. It also needs no
provisioning profile of its own, so zsign, the profile and the
device-in-profile checks are skipped for these builds.

How a delivery works:

1. Build & Install pushes `Documents/<name>.ipa` into LiveContainer.
2. On the device, open LiveContainer. The IPA is in its Documents; import it
   as an app. LiveContainer signs it on import, and that is when the tweaks
   start loading.
3. Optionally remove the pushed IPAs afterward, with Clean up in the device's
   LiveContainer section.

Delivering is not installing: Graft can see what it put in Documents, not
whether you imported it. See [Delivery state](#delivery-state).

### Sideload (paid only)

Sideload delivery signs the IPA with zsign, using your `.p12` and profile, and
installs it on the device as a normal top-level app. The device's UDID must be
in the profile. Use this for apps that don't care about their bundle id or
signing team.

On a free Apple ID, building, re-installing or adding a sideload app is
refused.

On iOS 16 and later, an app signed with a development profile opens only with
Developer Mode on. Graft checks before building one and stops if it's off. A
real build first makes the switch appear in Settings › Privacy & Security ›
Developer Mode, which iOS hides until a development app is installed: turn it
on there, and confirm after the device restarts. The device's page shows
whether it's on. Ad hoc and enterprise profiles, and LiveContainer delivery,
don't need it.

## Building

Build & Install runs this pipeline. Build options (beside it) picks the
device and can build without installing. It can also export the IPA unsigned
instead (see [Exporting an IPA](#exporting-an-ipa)).

![An app pane with two tweaks attached and the Build & Install button shown, under a yellow bar saying no device is connected.](img/app-ready.png "An app with two tweaks attached. The yellow bar says no device is connected.")

1. **Pre-flight.** Tools, signing files, password, device, and that the device
   is in the profile. Only what the delivery mode needs is checked. Settings ›
   Preflight, and `graft --check` in Terminal, check the same without
   building. There, no device connected is information ("not connected"), not
   a failure; a build that installs still needs one.
2. **Source IPA.** The decrypted IPA staged in `~/.cache/graft/ipas/`.
3. **Decryption check.** The load commands of every Mach-O. Any that is still
   encrypted is reported as a warning naming the binary, and the build carries
   on.
4. **Inject.** cyan, with the app's tweaks and options. The original bundle id
   is kept by default. cyan's output is meant to be re-signed: its modified
   binaries carry stale signatures until then. Every delivery re-signs before
   anything runs: zsign for Sideload, LiveContainer on import, SideStore or
   PlayCover on install. A LiveContainer build that isn't installed is for one
   of those signers.
5. **Sign** (Sideload only). zsign with your `.p12` and profile.
6. **Archive.** Copy the result to `~/.cache/graft/built/`.
7. **Deliver.** Push into LiveContainer, or install on the device.
8. **Record.** Write the build to `~/.local/state/graft/log.sqlite` and apply
   archive retention.

**Stop** ends the build where it is. A stopped build shows as Stopped, not as
a failure, and adds no row to Builds.

### Which device a build goes to

With nothing picked, a build goes to the default device when it's connected,
else to the only other device connected. With several connected and none of
them the default, the app pane's bar asks you to pick one, and Build & Install
waits until you do. **Change** on the bar lists the connected devices, with
their iOS version and, for a LiveContainer app, whether LiveContainer is on
them. Picking one sends the next Build & Install there only ("Next Build &
Install: iPad Air via LiveContainer · then back to iPhone 15 Pro"); the ✕
clears it, and it clears itself once that build starts installing or succeeds.
A build that fails or is stopped before installing keeps it for the retry. A
picked device that's unplugged stops Build & Install too, until you plug it in
or clear the pick. Build options' Device picker sets the same pick. Updates
don't use it: an update installs on every device the app is on. The
menu also makes a device the default. In Terminal, `--device <UDID>` does the
same for one build, and that device must be connected. When a build goes
somewhere other than the default, its log says where and why.

For a LiveContainer app, a device without LiveContainer can't take the build:
the bar says so and offers **Set Up LiveContainer…** for that device, over the
app pane. When a Sideload build signed with a profile Graft makes, or
installing LiveContainer, goes to a device added to your developer account
since you last saved App Store Connect in Settings, Graft reads the account's
devices again first and saves the new list. A profile you made yourself isn't
changed. Graft never registers a device.

## Build history and retention

**Builds** lists every build, with its saved log (View log…). A build's
archive can be delivered again without rebuilding (Re-install…), or shown in
Finder (Reveal in Finder).

Re-install… asks first. It names the app, the build (its version and when it
was made) and the iPhone or iPad it installs on: the one the build went to,
else your default device, else the first one connected. When that device isn't
connected, it says so and installs nothing. Return re-installs, and Escape
cancels.

A re-install goes out the way the archive was built: an unsigned LiveContainer
archive is never installed as a top-level app, and a zsign-signed one is never
pushed into LiveContainer. If the app's delivery mode has changed since,
rebuild it.

After each successful build, all but the newest `archive_keep` archives **per
app version** go to the Trash (default 2; `0` keeps only the build just made).
- Grouping by version means rebuilds of one version never push out the last
  build of an older version, so you can always roll back to a version you
  shipped.
- Builds you can re-install are kept first. An archive left by a build whose
  install failed only keeps a slot none of those needs.
- Archives go to the Trash, not straight to deletion, so the space comes back
  when you empty it. Staged source IPAs are never pruned.

**Clean up old builds…** in Builds does the same at your `archive_keep`: it
says how many archives would go to the Trash, and moves them when you confirm.
Useful after lowering the setting. The button is disabled while a build runs.

A clean-up never moves an archive that is still in use.
- A build holds a lock from the moment it archives its IPA until its history
  entry is written, and a re-install holds it until the archive is delivered.
- A Mac install copies a built archive under the lock and hands PlayCover the
  copy, since PlayCover reads the file after Graft has returned.
- A clean-up you start waits up to 30 seconds for them to finish, then stops
  with an error and moves nothing. A build's own clean-up doesn't wait: if
  another build is delivering, it skips this time and says so, and the next
  build catches up. Two clean-ups at once take turns.

## Delivery state

The app list and each app's pane say whether building now would produce
something different from what was last delivered.

On every delivery Graft records a SHA-256 over what determines the build: the
source IPA's bundle id and version, each tweak's filename and contents, the
cyan options and the delivery mode. Comparing that with what the config would
produce now gives one of:

| State | Means |
|---|---|
| `never` | Nothing has been delivered for this app. |
| `stale` | Something changed since the last delivery; the reasons say what (for example `some-tweak.deb rebuilt since delivery` or `app version changed: 1.2.3 → 1.3.0`). |
| `current` | Matches the last delivery. |
| `unknown` | Graft can't tell (for example a tweak file it can't read, or no staged IPA), and says so instead of guessing. |

Tweak contents are hashed, not just names, because a tweak rebuilt from source
keeps its filename. The source IPA is identified, not hashed.

Limits worth knowing:

- **Delivered is not installed.** For LiveContainer, Graft knows it wrote the
  file, not whether you imported it. Checking the device lists LiveContainer's
  Documents and can only downgrade a state: a missing file means the record is
  out of date, but a present file doesn't prove an import.
- Checking the device leaves sideload apps alone.
- A re-install of an archive that a recorded push shipped, byte for byte
  (usually the newest build), is compared like that push. Any other
  re-installed archive reads `stale`, because what went into it wasn't
  recorded.

None of this blocks a build.

## Updating your apps

Graft checks your apps for updates at launch and once a day while it's open:
a newer version on the App Store, a newer build of a tweak an app uses, or a
build that changed since it was last installed. An app with something to
update shows an arrow mark in the sidebar, and its pane says what: "Update
available: App Store 1.2.0 · 1 tweak".
- **Update & Install** (or **Update Tweaks & Install**, or **Install
  Update**) shows what will change, app by app, and each device it installs
  on, then does it when you choose **Update**: the tweaks, the App Store
  download, one build, and an install on each device.
- **Update All**, above the apps when two or more apps have an update, does
  the same for each of them.
- An update installs on every device the app is on: each iPhone or iPad
  you've delivered it to, and only the ones that need it. It never adds a
  device, and the device picked for the next Build & Install doesn't apply.
- A device that isn't connected, even one unplugged during the update,
  waits: the app's pane says so ("iPad mini isn't connected: it'll update
  when it is"), and the app's other devices still update. When you connect
  it, Graft checks again within 10 minutes and offers its install. An update
  that only waits for a device doesn't mark the app in the sidebar or count
  in Update All.
- For a device you no longer use (sold, reset, or retired), **Stop
  updating** ("Stop updating Example on iPad mini"), in the menu beside its
  line on the pane or in the confirmation, stops updating that app there. It
  changes nothing on the device, and lasts until you install that app on that
  device again.
- Graft never buys or claims an app, and never moves an app off a version
  you're holding it at: that app still gets its tweak updates and install.
- After you re-install an older build on a device, or when the last install
  can't be found there, the pane says so and offers **Install Update** for
  that device (one for each, when there are several), for you to press. It's
  a Build & Install to that device; Graft doesn't install it on its own.
- When a tweak is shared, its other apps need an install too: the
  confirmation says which, and **Also install … now** adds them.
- **Stop** stops at once, except during an install or while a tweak is being
  swapped in, which finish first. **Stop Now** cancels that install, and
  after three minutes **Force Stop** ends it outright. The progress shows
  each device's install under its app, and how it went. **Try Again**, for
  every app or for one device, picks up where it stopped.
- LiveContainer apps end up in LiveContainer's Documents on each device, to
  import there.
- On an Intel Mac, a newer App Store version shows, and can't be downloaded.

Settings › Updates › **Check my apps for updates** turns the daily check off,
and the checks after a get, build, tweak update, update, change to an app's
options or tweaks, or device change.
With it off, Graft checks only when you press **Check for updates now**,
there, in the sidebar or on an app's pane, and the Update buttons show what
that check found. The check sends your apps' bundle IDs to Apple's App
Store lookup and your tweaks' package IDs to canister.me, and nothing that
identifies you or your Mac. It opens no connection to your iPhone or iPad and
reads nothing from the keychain.

In Terminal, `graft --update` does the same; see
[Building](cli-reference.md#building) in the CLI Reference.

## Running builds on this Mac

On an Apple silicon Mac, an app's pane has **Build for this Mac**, which makes
the build loadable under PlayCover and hands it to PlayCover. (On an Intel
Mac, PlayCover can't run, and the button is disabled.) A staged IPA or
an archived build can be handed over too.

Some tweak libraries import iOS private frameworks that macOS doesn't have;
for these Mac builds Graft bundles a minimal stand-in instead. Phone builds
are never changed this way.

Graft.app carries that stand-in prebuilt, so a Mac build needs no Xcode. A
`graft` installed with pipx builds it on demand and needs full Xcode for that;
see [The CLI without the app](cli-install.md#the-cli-without-the-app-pipx).

Some tweaks need an iOS framework that macOS doesn't have at all. Graft won't
build such a tweak for this Mac, or hand over a build that has one, and says
which tweak and which framework. Phone builds can still use it. A framework the
app itself links only gets a warning.

After handing a build over, Graft waits up to two minutes for PlayCover to
finish installing it, and says when it has. If it hasn't by then, check
PlayCover's window; if the install failed there, Install to Mac… on the build's
row in Builds tries again. Stop ends the wait, not the install.

PlayCover has two settings per app that matter here: keymapping, which turns
clicks into touches for games, and PlayChain. On an app's first handoff Graft
turns both off and says so, because keymapping makes a non-game's clicks miss
and PlayChain crashed apps the first time they saved a login. After that Graft
keeps whatever you set in PlayCover: turn keymapping back on for a game, and it
stays on when you rebuild.

Not every iPhone or iPad app runs under PlayCover, even without tweaks: some
open to a blank window, can't open their data, or have buttons that don't
respond. If an app doesn't work, try it without tweaks first (Install to Mac…
on its IPA in the Cache pane); if that fails too, PlayCover can't run that app.

Graft doesn't include PlayCover, and doesn't download or install it. When
PlayCover isn't installed, Build options says so above Build for this Mac, and
**Get PlayCover** opens
[PlayCover's releases page](https://github.com/PlayCover/PlayCover/releases)
on GitHub in your browser. You can still build first: once PlayCover is
installed, Install to Mac… on the build's row in Builds hands it over. If a
handover fails because PlayCover isn't installed, the error names the same
page.

## Exporting an IPA

If you sign apps on the phone, with an on-device signer and a certificate
service, or install them with TrollStore, Graft can give you the IPA instead
of delivering it. **Export IPA…** in Build options builds the app as far as
injecting its tweaks, then saves that IPA where you choose.

- **What the file is.** The app's decrypted IPA with its tweaks injected and
  the app's options applied, with your Apple ID's purchase data removed. It
  isn't signed: the binaries cyan changed need re-signing before iOS runs
  them.
- **What to do with it.** Put it on the phone (in Files, say) and open it in
  your on-device signer, which signs it with your certificate and installs
  it. Or install it with TrollStore.
- **What Graft doesn't do.** It signs nothing and sends nothing to a device,
  whatever the app's delivery mode, so a free Apple ID can export a Sideload
  app too. Graft only saves the file; moving it to the phone is up to you.
- **It isn't a build in Builds.** An export adds no row to the build history
  and keeps no log, and archive clean-up never touches the file.
- **For your own devices.** It's an app your Apple ID owns or got for free.

The Save panel asks before replacing a file that's already there. Graft
builds the file in a temporary folder of its own and moves it to where you
chose once it's done, so a stopped or failed export leaves nothing in your
folder. Graft won't save into its own folders for staged IPAs and archived
builds, which it cleans up itself.

In Terminal, `graft example --export-ipa ~/Desktop/Example.ipa` does the same;
see [Building](cli-reference.md#building) in the CLI Reference.
