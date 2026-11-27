# Changelog

What changed in each release of Graft, newest first. Each release's download
is on [GitHub Releases](https://github.com/GreenglassT/Graft/releases).

## 2.3.1 (2026-11-26)

### Security

- A tweak updates only from where it came from: the repo or Canister
  repository you fetched it from. Another repo that publishes the same
  package can't replace it in your apps.
- When Graft can't verify a tweak update (Graft has no record of where the
  tweak came from, the source doesn't use HTTPS, or it lists no checksum),
  the update confirmation says why and where the update comes from, and the
  tweak updates only if you turn on **Trust this source**. You're trusting
  the update the confirmation shows: if the source offers something else by
  the time it runs, Graft doesn't update that app, and says so. The Tweaks
  pane asks first too.
- Graft names each tweak it downloads (`<package id>_<version>.deb`, with
  its architecture when the package names one), keeps a download only if
  it's a .deb of the package it asked for, and never replaces a different
  tweak. Fetching from a Canister search takes the build from the repository
  the result shows. In Terminal, a fetch by package id that several Canister
  repositories list names them and asks you to pick one.
- A Canister repository recorded as a tweak's source whose address has moved
  counts as unverified.
- A copy of the LiveContainer certificate left in the temporary folder by a
  push that was killed is removed the next time Graft runs.

### Changed

- With a paid account, Add App starts a new app on Sideload, as the setup
  screen and the docs say, then on what you chose last. A line under the
  picker says what each mode does. With a free Apple ID, Add App shows
  Delivery: LiveContainer.

### Fixes

- ⌘Q and Graft › Quit Graft work while a sheet is open: an update's
  progress, Edit…, a confirmation. With work running, Graft asks first, as
  it does with no sheet open, and quitting during an update's install waits
  for it to finish. With nothing running, Graft quits, and an open sheet's
  unsaved changes are discarded.
- What's New shows what's new to you: after an update, the releases since
  the version you used before; from Help › What's New…, the latest release.
  **Show earlier releases** shows the rest, in the same window.
- Check device says which device it checked ("Checked iPad mini — the record
  still stands"), and says so when the delivered file is gone from it.
- In an update's progress, a device whose install Stop Now canceled says
  Stopped, with Try Again. Not started is for devices the update never
  reached.
- Build for this Mac names a tweak it can't build by its name in the Tweaks
  library.
- Hints and messages about a device say iPhone or iPad, and LiveContainer's
  folder in the Files app is On My iPad on an iPad.
- After a get that changes an app's hold, Graft checks just that app for
  updates again.
- `graft --update` in Terminal names the tweak step as the app does: "→
  tweaks: 1 tweak".
- A device that's locked, or asking whether to trust this Mac, says "Unlock
  it and tap Trust" in the sidebar and on the app's bar, and Graft reads it
  again on its own, within a few seconds at first and every half minute
  after.
- Build & Install, Build for this Mac and Export IPA… wait, and say what to
  do, until the app has a decrypted IPA. Build & Install also waits for a
  connected iPhone or iPad, or for Skip install.
- Before the App Store is set up, Get latest from App Store, Get an older
  version… and Update wait and say where to set it up, with Open Settings.
- When a check can't reach the App Store or canister.me, as when your Mac is
  offline, the update marks stay and Graft checks again within the hour.
  Check for updates now says how it went, and VoiceOver reads it.
- After a LiveContainer build, the app's pane says where the IPA went and
  what's left ("Sent to LiveContainer on iPhone 15 Pro. Open LiveContainer
  and import Example to finish.").
- Set Up LiveContainer says what would stop it before you press Set Up: an
  App Store Connect key that isn't set up yet (with Open Settings), or
  Developer Mode, which a LiveContainer from Graft needs on iOS 16 and
  later. It installs one LiveContainer by default, and with a free Apple ID
  and LiveContainer already on the device, lists only Import Certificate.
  With a paid account, when LiveContainer isn't on the device, the bar also
  offers switching the app to Sideload.
- Dump in a device's list of apps waits on a device without a jailbreak, as
  the Decrypt card says. On an Intel Mac, a newer App Store version says to
  drop a newer decrypted IPA on Source IPA, without the update arrow.
- A full disk says "Your Mac is out of disk space: free some and press Try
  Again" wherever it stops a get, a build, an update or a tweak update. An
  expired App Store sign-in says it has expired and points to Sign In
  Again….
- When GitHub is slow to answer Graft's own update check, Graft waits up to
  30 seconds, then says "GitHub didn't answer in time".
- An app held at a build whose file was deleted says so ("held at 1.2 (build
  1 of 2), which isn't downloaded"). A device unplugged mid-update reads as
  waiting in the log too.
- A PlayCover handoff that fails or is stopped removes its copy of the IPA
  from the temporary folder. A damaged PlayCover settings file, a damaged
  `.ipa-info.json` in the cache, or a version with square brackets in it is
  handled without stopping anything.
- Fetching and updating tweaks works on volumes without hard links, such as
  exFAT or a network share.
- Graft's self-update finds its install in the update log even after a
  system tool's message with no line break.
- When Graft can't write its log after the device already has LiveContainer
  or the certificate, Set Up LiveContainer says what wasn't recorded, and
  the install and the reminder carry on.
- On an Apple silicon Mac running Graft-Intel.zip, a newer App Store version
  says to use Graft.zip, or to drop a newer decrypted IPA on Source IPA.
- On an Intel Mac, Settings › App Store says in one line that getting apps
  from the App Store needs a Mac with Apple silicon, and to add apps from a
  decrypted IPA, with no sign-in or setup to do. With Graft-Intel.zip on an
  Apple silicon Mac, it says which download can.
- Terminal: `--trust-tweak <confirm_key>` confirms an unverified tweak
  update for `--update` and `--update-tweak`, and `--fetch-repository ID`
  pins `--fetch` to one Canister repository. The update plan adds
  `checks_failed`, each tweak's `source`, `verified`, `unverified_reason`,
  `confirmed`, `confirm_key` and `trust_changed`, and each app's
  `needs_confirmation`; `tweak-update` adds `confirm_key`.

## 2.3 (2026-11-26)

### New

- **Updates for your apps.** Graft now says when an app has something to update:
  a newer version on the App Store, a newer build of a tweak it uses, or a
  build that changed since it was last installed. The app shows an arrow
  mark in the sidebar, and its pane says what ("Update available: App Store
  1.2.0 · 1 tweak"). Update & Install shows what will change and where it
  installs, then updates the tweaks, gets the App Store version, builds and
  installs, showing its progress as it goes. Update All, when two or more
  apps have an update, does the same for each of them.
- An update installs on every device the app is on: one build, then the same
  build on each. The confirmation lists each device, and the progress shows
  each install with its own result and Try Again. A device that isn't
  connected waits ("iPad mini isn't connected: it'll update when it is")
  while the others install, and its install is offered again once it's
  connected. A device that can't take the build, with Developer Mode off
  say, doesn't stop the others. An update that only waits for a device doesn't mark the app in
  the sidebar, and Stop updating ("Stop updating Example on iPad mini") stops
  waiting for a device you no longer use. Graft never buys or claims an app,
  and never moves an app off a version you're holding it at. When an updated
  tweak is shared, the confirmation names the other apps that use it, and
  Also install … now installs them too. After you re-install an older build
  on a device, the pane offers Install Update for that device instead of
  installing on its own.
- Stop during an update stops at once, except during an install or while a
  tweak is being swapped in, which finish first. Stop Now cancels that
  install, and after three minutes Force Stop ends it. Try Again picks up
  where it stopped. Quitting during an install waits for it to finish.
- **A daily check of your apps.** At launch, once a day while it's open, and
  after a get, build, tweak update or update, Graft checks your apps and
  their tweaks for updates. It sends your apps' bundle IDs to Apple's App
  Store lookup and your tweaks' package IDs to canister.me, and nothing that
  identifies you or your Mac. Settings › Updates › Check my apps for updates
  turns these checks off, and Check for updates now still checks when you
  want.
- **Pick the device for a build.** The app's bar says where the next Build &
  Install goes, and why. Change sends the next Build & Install to another
  connected device, then goes back to the default; a build that fails or is
  stopped before installing keeps the pick for the retry. With the default
  device unplugged, a build goes to the only other device connected, and
  with several connected, the bar asks you to pick one. Settings names a
  default device that isn't connected.
- A build signed with a profile Graft makes, or a LiveContainer install, to
  a device added to your developer account since you saved App Store
  Connect, reads the account's devices again first, so the profile includes
  it. A device that isn't registered is named in the error; Graft never
  registers one.
- **Set Up LiveContainer.** A device's pane sets up LiveContainer in one
  sheet. With a paid account, Set Up checks the device against your
  developer account, installs LiveContainer and puts the certificate on the
  device. The sheet then lists the steps to do on the device: importing the
  certificate, JIT-Less Diagnose, importing your apps, and Convert to Shared
  App. LiveContainer asks for your `.p12`'s password on the device; Graft
  can't type it for you. With a free Apple ID, it links to SideStore and
  iloader instead. While a certificate Graft put on the device is still
  there, the device's pane says so, with Remove it. For a LiveContainer app,
  the bar offers Set Up LiveContainer… when the device it's building for has
  none.
- **Developer Mode.** On iOS 16 and later, an app signed with a development
  profile opens only with Developer Mode on. Graft now checks before it
  builds one. If it's off, the build stops, after making the switch appear
  on the device in Settings › Privacy & Security › Developer Mode, which iOS
  hides until then. The device's page shows whether it's on. In Terminal,
  `--device-info` reports `developer_mode`, and `--list` reports
  `needs_developer_mode`.
- **Holds by build.** When the App Store has two builds of one version,
  getting one holds the app at that build: the pane says "Held at 1.2.0
  (build 1 of 2)", and builds use that build even when the other is staged
  too. Moving between those builds marks the app out of date, and the Cache
  pane marks the file the build uses. Dropping an IPA of the held version,
  or decrypting it from a jailbroken device, holds the app at that version
  by number, with a warning.
- **Updates in Terminal.** `graft --update <app>`, or `--all`, updates the
  app's tweaks, gets the App Store's newer version, then builds once and
  installs on every device the app was installed on. `--plan` says what it
  would do, device by device, and changes nothing. It never buys or claims
  an app or lets go of a hold, and installs only what changed: after you
  re-install an older build, it leaves that install to you. Run it again to
  pick up where a failed or stopped update left off; it installs only where
  the app isn't up to date. A Stop during an install lets the install
  finish, and a second Stop ends it. `graft --drop-update-target <app>
  --device <UDID>` stops updating an app on a device that's gone.
  [AGENTS.md](AGENTS.md) tells an AI agent how to use it.
- **PlayCover keeps your settings.** Graft turns off PlayCover's keymapping
  and PlayChain only on an app's first handoff, and says so. After that it
  keeps what you set in PlayCover: turn keymapping on for a game and it
  stays on when you rebuild. If PlayCover changes either one anyway, Graft
  puts yours back.
- **Graft waits for PlayCover's install.** After handing a build over, Graft
  waits up to two minutes and says "PlayCover installed Example 1.2.0".
  Build for this Mac, and Install to Mac… in Builds and the Cache, show
  "Waiting for PlayCover to install Example…" with Stop, which ends the
  wait, not the install.
- **A tweak that can't run on macOS is refused.** Build for this Mac and
  Install to Mac… refuse a tweak that needs an iOS framework macOS doesn't
  have, naming the tweak and the framework. A framework the app itself links
  only gets a warning. A tweak's own frameworks now get the stand-in
  Preferences framework too, as its libraries already did. The tooltips say
  that not every iPhone or iPad app runs under PlayCover, even without
  tweaks, and how to tell.

### Fixes

- An iPad's page in the sidebar says iPad where it used to say phone.
- Check device on a LiveContainer app's pane looks on the device the app was
  last delivered to. It used to ask whichever device was connected and say
  the record still stood.
- Install to Mac… in Builds says why when the handover fails; it used to say
  nothing.
- Re-install… asks before it installs on a phone. The confirmation names the
  app, the build and the iPhone or iPad it goes to, and says so when that
  device isn't connected. Return re-installs, and Escape cancels.
- Graft no longer installs an update that needs a newer macOS than this Mac
  has. It says which macOS the update needs, offers no Try Again, and
  doesn't download it again at every launch; once the Mac is upgraded, the
  update goes ahead.
- When an update can't install itself, its message names the right
  download: `Graft-Intel.zip` on an Intel Mac, or on an Apple silicon Mac
  with macOS 13.
- The Builds list shows a build as soon as it ends, without relaunching
  Graft.
- A window opened behind another app lists the connected devices right away,
  instead of saying "Looking for devices…" until you switch to it.
- An iPhone or iPad newer than Graft's list of models shows its own name, or
  "iPad" or "iPhone", instead of its long device ID.
- Settings › Preflight shows "not connected" as information when no iPhone
  or iPad is plugged in, not as a failure. A build that installs still
  needs one.
- Edit, Remove, Stop building, Re-install, the Builds list and the build log
  use an app's display name, not its short name.
- The quit question names what's running: exporting an IPA, building an
  app, or building and installing one.
- The build log's header and the Builds list show a local date and time.
- A device's LiveContainer section no longer says it's managed by SideStore
  or AltStore when LiveContainer isn't installed. It says SideStore or
  AltStore is on the device, and to install or update LiveContainer with it.
- A get by version id downloads the build you picked when another build
  shares its version number. It used to take a staged copy of that number,
  which could be the other build.
- Every icon-only button has a VoiceOver name, such as "Update" or "Delete"
  plus the tweak's name. The Tweaks filter's placeholder fits its field.
- A device dump's output opens on its newest line.
- For people building Graft: the release check that the Intel build reads
  `GraftBundleArch` now reads Swift the way a Release build compiles it.

## 2.2.1 (2026-11-25)

### New

- **macOS 13 on Intel Macs.** `Graft-Intel.zip` now runs on macOS 13
  Ventura, as well as macOS 14 and later. It's still a test build.
  `Graft.zip`, for Apple silicon, still needs macOS 14. A Mac with Apple
  silicon on macOS 13 can run `Graft-Intel.zip` too, and its banner says
  `Graft.zip` needs macOS 14 or later. On macOS 13, the up and down arrow keys
  don't move through Add App's search results: click a result, or press
  Return to pick the first.

### Fixes

- Get an older version… tells apart builds the App Store has under the same
  version number. They used to look like the same row twice. Now they read
  "1.2.0 (build 1 of 2)" and "1.2.0 (build 2 of 2)", numbered from the
  oldest, and hovering over one says which it is. Show More renumbers them
  when it finds an older one.
- Graft finds its own Python by name, whatever its version, as its updater
  already does. A later release built on a newer Python would otherwise skip
  the check that the app and its Python come from the same download, Apple
  silicon or Intel.
- For people building Graft: a release build runs the tests of Graft's
  ipatool patch and stops if one fails; each download is built for its own
  oldest macOS and checked against it; and an Intel release no longer passes
  its check when the app names `GraftBundleArch` only in a comment or an
  `#if false` block. The test suites also no longer depend on the Mac they
  run on, and a test build of the app keeps its settings apart from the
  Graft you use.

## 2.2 (2026-11-25)

### New

- **Intel Macs, as a test build.** `Graft-Intel.zip`, beside `Graft.zip` on
  the release page, runs on an Intel Mac with macOS 14 or later. It stays a
  test build until it has run on a real Intel Mac. Everything works except
  what needs Apple silicon: App Store sign-in, getting and decrypting App
  Store apps (older versions and free ones included), and Build for this Mac
  with PlayCover. Those buttons are still there, disabled, with "Needs a Mac
  with Apple silicon". On an Intel Mac, use a decrypted IPA you already have,
  or dump one from a jailbroken device. App Store search still works, for
  adding an app by name, and needs no sign-in.
- Each download updates itself from its own zip, and checks that the update
  is the same build before installing it. If a release doesn't have your
  build's zip yet, Graft says there's no update for this Mac yet.
- `Graft.zip` opened on an Intel Mac says to download `Graft-Intel.zip`, with
  a button to the releases page. `Graft-Intel.zip` opened on an Apple silicon
  Mac runs, with a banner saying to download `Graft.zip` for everything.
- Help › Join the Graft Discord, the same button in Settings › Version, and a
  link in Graft › About Graft open the Graft Discord, for questions and help.

### Fixes

- Get an older version… stops when the connection stalls. It used to wait
  about a minute on the version it was reading, mark that version as
  unavailable, and go on to the next one, up to a minute each. Now the list
  stops there and says reading that version timed out. The versions it had
  read are kept, so Try Again picks up where it stopped. If that version
  times out again, it's marked as unavailable and the list goes on.
- Get an older version… copes better with slow connections: a request for a
  version's details now fails only after 30 seconds with nothing arriving,
  not after 30 seconds in all.
- Export IPA… no longer freezes the window when it saves to a drive where
  it has to copy the file into place, such as some network and FAT drives.
  The window stays responsive while the file is copied, and Stop ends the
  copy and leaves nothing in the folder. If a file with the same name
  appears in the folder while the file is copied, Graft leaves it alone and
  says so.
- When an export fails, the error names the file you chose, not the
  temporary folder Graft built it in.

## 2.1 (2026-11-24)

### New

- **Get an older version.** When a new version of an app breaks a tweak, Get
  an older version… on the app's pane lists the versions the App Store still
  has for your Apple ID, newest first, with their dates, and gets the one you
  pick. Graft then holds the app at that version: builds use it, and the pane
  says "Held at 1.2.0 · newest 1.4.0" instead of "update available", until you
  press Get latest from App Store. Only apps your Apple ID has, Apple doesn't
  serve every old version, and some apps refuse to run once they're too old.
  In Terminal: `graft --appstore-versions --app <name>`, then
  `graft --appstore-get --app <name> --version-id <id>`.
- A preferred version set in Edit App is now a hold too: the pane says "Held
  at", not "update available", and Get latest from App Store sets it back to
  latest.
- If PlayCover isn't installed, Build options on an app's pane now says so
  above Build for this Mac, with a Get PlayCover button that opens
  PlayCover's releases page on GitHub. Graft doesn't download or install
  PlayCover. When handing a build to PlayCover fails because it isn't
  installed, the error names that page too.
- **Export IPA…** in Build options saves the app's IPA with its tweaks
  injected, unsigned, where you choose, for an on-device signer or TrollStore
  on your own devices. Nothing is signed or sent to a device, whatever the
  app's delivery mode, and the export doesn't appear in Builds. In Terminal:
  `graft example --export-ipa <file>.ipa`, which checks it can write there
  before it builds. Under `--json`, a failed export's `build-end` says why in
  `reason`, such as `exists` or `unwritable`.

- Help › Star Graft on GitHub and Help › Support Graft on Ko-fi open those
  pages in your browser. The same two buttons are in Settings › Version, and
  Graft › About Graft links them too.

### Fixes

- Help › Check for Updates… no longer offers Update for an update Graft is
  already downloading. It used to, and still said the update was available
  after the banner said it was ready. Now it says the download has started
  and the banner shows how it's going. For an update that's ready it offers
  Restart to Update, as the banner does, and after a failed try it says why,
  and offers Try Again when trying again can help.
- If you put an older Graft back after an update installed, Graft no longer
  says it couldn't update, as long as the copy you put back is 2.1 or later.
  It now checks the update log before it says an update failed. The log's
  times now include their offset from UTC.

## 2.0.2 (2026-11-24)

Graft now updates itself.

- **Automatic updates.** When a newer Graft is out, Graft downloads it from
  GitHub in the background, checks it, and shows Restart to Update in the
  banner at the bottom of the window. If you don't restart, it installs the
  update when you quit. The old Graft.app goes to the Trash, so you can put it
  back.
- **Checked before it's installed.** The download must match the SHA-256
  GitHub lists for it, and the app inside must be Graft, newer than yours,
  signed with the same developer's Developer ID as yours and notarized by
  Apple. If any check fails, Graft deletes the download and the banner links
  to the release page.
- **Never in the middle of work.** Graft doesn't install an update while it's
  building, installing or downloading an app. It waits until that's done, or
  until you quit.
- **Settings › Updates** has a second switch, Download and install updates
  automatically, on by default. With it off, the banner offers Update, which
  downloads, checks and restarts when you choose it. The first switch is now
  called Check for updates automatically.
- **When it can't.** Graft can't replace itself when it runs straight from
  Downloads, or from a folder your account can't change. The banner says so
  and links to the release page. If an install fails after Graft quits, Graft
  keeps the version you have, says so the next time it opens, and writes what
  happened to `~/Library/Logs/Graft/update.log`.
- `graft --download-update` downloads and checks a newer Graft from Terminal.
  [What Graft connects to](docs/network.md) lists the download.
- **Remove Graft's Data.** Help › Remove Graft's Data… lists everything Graft
  keeps outside Graft.app, each with its size and what it is: its folders,
  your App Store sign-in, ipatool's App Store components, the decryption
  model, your `.p12` and SSH passwords, the `graft` link for Terminal, the
  app's own settings, the update log, and temporary files a stopped run left
  behind. Check what to remove, and it removes that and nothing else. Your
  passwords stay unless you check them, and anything `config.yaml` points to
  outside Graft's folders is never removed. It waits while Graft is building
  or downloading, and when it's done it says what it removed, so you can quit
  Graft and move it to the Trash. In Terminal, `graft --remove-data` lists
  the same, and `--items` removes what you name.
  [Removing Graft](README.md#removing-graft) has the whole list.

### Fixes

- **If Apple stops serving the macOS restore image** that the bundled ipatool
  takes the App Store's frameworks from, the first sign-in or App Store action
  on a Mac now says so in plain words: a newer Graft fixes it, and the update
  notice (or Help › Check for Updates…) says when one is out. It used to show
  only ipatool's own error, such as "server returned 403 Forbidden". A Mac
  that already has those frameworks in `~/Library/Caches/ipatool` isn't
  affected.
- Get (free) no longer adds an app to an Apple ID the question didn't name. If
  you signed in as someone else in Terminal while "Get … for free?" was open,
  the app went into that Apple ID's purchases. Graft now checks again as it
  gets the app, and if the Apple ID changed it says so and buys nothing. The
  question also no longer opens by itself when you come back to an app you
  left while Graft was checking.
- If you've made a keychain other than login your default in Keychain Access,
  Graft no longer reads only the login keychain to tell whether it's locked.
  With that default keychain locked, the App Store sign-in could ask for its
  password.
- Check again, in Settings and on the setup screen, and Preflight's Re-run say
  when Graft asked your shell less than 10 seconds before and used that answer,
  instead of looking as if they had asked again. Setting the Mac's clock back
  no longer holds them off.
- A login shell from Homebrew no longer costs Graft your shell's settings the
  first time it runs. macOS checks a new program before it starts it, which
  can take seconds on a slow network, and that time counted against the five
  seconds Graft gives your shell.
- A push into LiveContainer that the phone stopped answering, or that you
  stopped, just as the old copy was moved aside no longer leaves the app
  missing from LiveContainer's Documents. The old copy is put back.
- A phone that goes silent while Graft connects to it, lists its apps, or reads
  or writes LiveContainer's files is given up on after a minute, instead of
  waiting until you press Stop.
- An app that uses its developer's iCloud container now gets the "will probably
  crash at launch" warning when your profile has iCloud but not that
  container.
- A `.p12`, profile or App Store Connect key your user can't read (one copied
  with sudo belongs to root) gets a hint about its owner and permissions, not
  the Privacy & Security one. That hint now names Terminal too, for `graft`
  run there.
- A paid account setup that failed, then was stopped while it put things back,
  still says why it failed.
- A pipx `graft` whose `appstore.ipatool` names Graft.app's own ipatool no
  longer deletes Graft.app's App Store sign-in when it signs in. Neither does
  Graft.app when the setting spells that path another way: in another case,
  through a hard link, or under /System/Volumes/Data.
- When macOS's Keychain refuses a pipx `graft`'s own ipatool, the hint says to
  sign in again with it in Terminal. It sent you to Graft.app, whose sign-in
  doesn't help that ipatool.
- **Set Up Decryption's check for the App Store state** passes over a folder
  that is a link, and Cancel and the time limit stop it like the rest of the
  step. Another user of this Mac could put a link in place of that state's
  folder in the Mac's Shared folder, to a network folder that is slow to
  answer, and the check, which runs as root, could keep waiting on it past
  Cancel and the time limit. It never read or changed anything there.
- Release Page on the update banner, and Open Release Page after Help › Check
  for Updates…, now open only Graft's own page for the version on offer.
  Graft keeps the last update check's answer between launches, and another
  program on your Mac could change it so the button opened a page somewhere
  else. Graft now builds the link from the version number, and drops a saved
  answer whose version isn't one. `graft --check-update` also names only that
  release's page, or Graft's latest release.
- Quit Anyway no longer leaves a stopped command running after Graft has
  gone. Graft gives a stopped command 10 seconds to clean up before it ends
  it outright, but Quit Anyway waited only 5, so a slow one, and the tools it
  had started, could run on without Graft. Quit Anyway now waits up to 13
  seconds, and Graft still quits as soon as the work has stopped.
- Quitting while Graft sets up your signing account or removes its data now
  says Graft can't stop that partway and will quit when it finishes, and Quit
  Anyway does just that. It used to say Quit Anyway would stop it, then quit
  after 5 seconds and leave it running. Quitting while Graft checks your
  signing files stops the check, as Cancel does.

## 2.0.1 (2026-11-24)

Graft now tells you when a new version is out, and has a new icon.

- **Update notice.** Once a day while it's open, Graft asks GitHub whether a
  newer version is out, through its own CLI (`graft --check-update`), and shows
  a banner if there is one. That's one request, sent as `User-Agent: Graft`,
  with nothing that identifies you or your Mac, and it never downloads or
  installs anything. Help › Check for Updates… asks any time, and Settings ›
  Updates turns the daily check off. [What Graft connects to](docs/network.md)
  lists it.
- **What's New.** Help › What's New… shows this changelog inside the app.
- **A new icon:** two pieces joined on the diagonal and taped, with a leaf at
  the tip.
- **Get Again.** When the version the App Store has is the one already staged,
  Get latest from App Store downloads nothing and says "Already have
  <version>". Get Again, or `--again` with `--appstore-get`, downloads it
  anyway.

### Fixes

- **Set Up Decryption** suggests opening the app you picked first, with a
  button that does it: macOS sometimes clears the App Store state that step
  reads, even for an app you've already opened, and opening the app writes it
  again. If the state still isn't there, root stops before it copies or
  changes anything (the script exits 69) and the sheet says which app to open.
  The time limit root works under no longer measures an app through a link.
- **Quitting, logging out or restarting while Graft is working** (a build or
  install, an App Store download, setting up decryption or your signing
  account, a LiveContainer change, or a decrypt from a device) asks first. Keep
  Working cancels the quit; Quit Anyway stops the work cleanly, then quits.
- **Installing LiveContainer** waits up to 15 minutes for you to close
  LiveContainer only when it's updating a copy already on the device. A first
  install keeps the usual minute and the dropped-connection hint.

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
