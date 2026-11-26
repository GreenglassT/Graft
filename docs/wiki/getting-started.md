# Getting Started

Install Graft and check what it needs. Each of the five steps to installing an
app has its own page.

![Graft's main window with five example apps in the sidebar and one app's pane open.](img/getting-started-main.png "The main window: apps in the sidebar, the selected app's pane on the right.")

## Install

1. Download `Graft.zip` from [GitHub Releases](https://github.com/GreenglassT/Graft/releases).
   On an Intel Mac, download `Graft-Intel.zip` instead (a test build; see
   [Requirements](#requirements)).
2. Unzip it, move `Graft.app` to `/Applications`, and open it.

Graft.app carries everything it needs:
- the `graft` CLI, with its own Python;
- cyan, zsign, ldid, ipatool, UnfairPlay and the other helpers.

You don't need Homebrew, pipx, Python, Go, Xcode or libimobiledevice. The
one exception is decrypting on a jailbroken device, which still needs
libimobiledevice's `iproxy` and tools you install yourself (see
[Decrypt on a jailbroken device](getting-an-ipa.md#decrypt-on-a-jailbroken-device)).

## Requirements

- **A Mac with macOS 14 or later,** or macOS 13 or later for the Intel
  download. On a Mac with Apple silicon, download `Graft.zip`, which does
  everything.
- **On an Intel Mac,** download `Graft-Intel.zip`, which also runs on
  macOS 13. It's a test build until it has run on a real Intel Mac.
  Everything works except what needs Apple silicon:
  - App Store sign-in, and getting and decrypting App Store apps, older
    versions and free ones included (search still works, for adding an app
    by name, and needs no sign-in);
  - Set Up Decryption;
  - Build for this Mac, and handing builds to PlayCover.

  Those buttons are there but disabled, with "Needs a Mac with Apple
  silicon". Use a decrypted IPA you already have, or dump one from a
  jailbroken device. `Graft.zip` opened on an Intel Mac only says to download
  `Graft-Intel.zip`; `Graft-Intel.zip` on an Apple silicon Mac runs, with a
  banner saying to download `Graft.zip` for everything.
- **An iPhone or iPad** paired with this Mac, connected by USB, or on the
  same network with Wi-Fi sync turned on.
- **An Apple account,** of one of two kinds. The first run asks which you
  have, and Settings switches later.
  - A **free Apple ID**, with LiveContainer on the device. Graft builds
    unsigned IPAs and pushes them into LiveContainer, which signs them when
    you import them. Graft never signs anything itself on this account.
  - A **paid Apple Developer Program membership**, with a development
    certificate exported as a `.p12` and a provisioning profile (a wildcard
    profile is simplest) that includes your device.

### What each feature needs

| To | You need |
|---|---|
| Build and push into LiveContainer | LiveContainer on the device. With a free Apple ID, install it with SideStore, AltStore, iloader or Impactor, which also refresh it every 7 days. |
| Install an app as a normal app (Sideload) | A paid membership, its `.p12` and a profile that includes the device. |
| Install or update LiveContainer from Graft | A paid membership and an App Store Connect API key. |
| Make each app's provisioning profile automatically | A paid membership and an App Store Connect API key. |
| Search the App Store | Nothing. No Apple ID is involved. |
| Download from the App Store | A Mac with Apple silicon, and an Apple ID signed in in Graft. Any Apple ID works, free or paid; it's separate from the account above. The app must be one that Apple ID owns, or a free one Graft can get for it. |
| Decrypt App Store downloads | A Mac with Apple silicon, and an iPhone or iPad app installed on it from the Mac App Store, with the same Apple ID, and opened once. Any free one will do. |
| Decrypt on a jailbroken device | A jailbroken iPhone or iPad with frida-server 16.x and OpenSSH, plus tools you install yourself (see [Decrypt on a jailbroken device](getting-an-ipa.md#decrypt-on-a-jailbroken-device)). |
| Run a build on this Mac | A Mac with Apple silicon, and [PlayCover](https://github.com/PlayCover/PlayCover/releases), a free app you install yourself. Graft doesn't include it. |

## The five steps

Each step has its own page:

1. [Set up your Apple account](apple-account.md)
2. [Add an app](adding-apps.md)
3. [Give it a decrypted IPA](getting-an-ipa.md)
4. [Attach tweaks](tweaks.md)
5. [Build and install](building-and-delivery.md)

After that, Graft checks your apps for updates at launch and once a day while
it's open. An app with something to update shows an arrow mark in the
sidebar; see [Updating your apps](building-and-delivery.md#updating-your-apps).
