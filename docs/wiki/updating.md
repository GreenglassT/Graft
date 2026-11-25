# Updating Graft

Graft downloads and checks a newer version by itself, and installs it when you
restart or quit.

## How Graft updates

Once a day while it's open (or when you choose Help › Check for Updates…),
Graft asks GitHub whether a newer version is out. If one is, it downloads it
in the background, then shows **Restart to Update** in a banner at the bottom
of the main window. If you don't restart, it installs the update when you quit
Graft.
- Each download updates from its own zip: `Graft.zip` for Apple silicon,
  `Graft-Intel.zip` for the Intel build. A release without yours has no
  update for your Mac yet, and Graft never installs the other one.
- Before it calls a download ready, Graft checks it: the SHA-256 GitHub lists
  for it, then that the app inside is Graft, newer than yours, the same build
  as yours, signed with the same developer's Developer ID as yours and
  notarized by Apple. A download
  that fails any check is deleted, and the banner links to the release page
  instead. A Graft you built yourself, which has no Developer ID, doesn't
  update itself.
- The old Graft.app goes to the Trash, so you can put it back.
- Graft never installs while it's working (a build, an install, an App Store
  download and so on). It waits until that's done, or until you quit.
- Your config, apps and builds carry over, and so does your App Store
  sign-in.
- The Terminal command points into the app, so it updates too.
- The check and the download send nothing that identifies you or your Mac.
  [What Graft connects to](../network.md) shows what they send.

## Settings › Updates

Settings › Updates has two switches, both on to start with:
- **Check for updates automatically** is the daily check. With it off, Help ›
  Check for Updates… still asks when you choose it.
- **Download and install updates automatically** is the background download.
  With it off, the banner offers **Update**, which downloads, checks and
  restarts when you choose it.

## When Graft can't update itself

Graft can't update itself when it runs straight from Downloads (macOS runs a
read-only copy of an app it hasn't moved), or from a folder your account
can't change. The banner then says so and links to
[GitHub Releases](https://github.com/GreenglassT/Graft/releases): download the
new `Graft.zip` (or `Graft-Intel.zip`) there and replace the `Graft.app` in
`/Applications`.

If an update can't be installed, Graft keeps the version you have, says so the
next time it opens, and writes what happened to
`~/Library/Logs/Graft/update.log`.

## In Terminal

`graft --check-update` asks GitHub whether a newer Graft is out, and downloads
nothing. `graft --download-update` downloads it into Graft's cache and checks
it, and installs nothing.

## What's new

Help › What's New… shows what changed in each release. It also opens by
itself once, the first time a new version of Graft starts. The
[Changelog](../../CHANGELOG.md) has the same list.
