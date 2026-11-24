# Graft Docs

Graft is a Mac app that finds an iOS app, downloads and decrypts it, adds your
tweaks, and sends it to your iPhone or iPad. It works with a free Apple ID.

## User Guide

- [Getting Started](getting-started.md): install Graft and check what it needs, with a page for each of the five steps to installing an app.
- [Your Apple Account](apple-account.md): Graft works with a free Apple ID or a paid Apple Developer account, and Settings can switch between the two later.
- [Adding Apps](adding-apps.md): add an app by searching the App Store, or from its App Store URL or a name and bundle id.
- [Getting an IPA](getting-an-ipa.md): the three ways to get the decrypted IPA a build needs.
- [App Store Downloads](app-store.md): once you've signed in and set up decryption, Graft downloads and decrypts the apps your Apple ID owns or can get for free.
- [Tweaks](tweaks.md): tweaks from Canister or an APT repository go into your library, and you attach them to an app from there.
- [Building & Delivery](building-and-delivery.md): building an app with its tweaks and pushing it into LiveContainer, or with a paid account installing it as a normal app.
- [LiveContainer](livecontainer.md): the LiveContainer already on your device, or one Graft installs and updates with a paid account.

## Terminal

- [Installing the CLI](cli-install.md): put the `graft` command on your Mac, either from Graft.app or with pipx.
- [Quick Start](cli-quick-start.md): the commands for a first build from Terminal, with a free Apple ID or a paid Apple Developer account.
- [CLI Reference](cli-reference.md): the `graft` commands, grouped by what they do.
- [Scripting & Environment](cli-scripting.md): scripts drive `graft` with `--json`, and the environment variables it reads include the ones for a proxy.
- [AI Agents](ai-agents.md): an AI coding agent on your Mac can run the `graft` commands for you, from a request in plain English.

## Reference

- [Configuration & Files](configuration.md): Graft stores its settings and each app's options in `config.yaml`, and its other files in folders of its own.
- [What Graft Downloads](downloads.md): when Graft goes online and what it downloads, including the one-time downloads on the first App Store action.
- [What Graft Connects To](../network.md): every host Graft and its tools contact, why, when, and what is sent.
- [Updating Graft](updating.md): Graft downloads and checks a newer version by itself, and installs it when you restart or quit.
- [Changelog](../../CHANGELOG.md): what changed in each release of Graft, newest first.
- [Troubleshooting](troubleshooting.md): common symptoms and error messages, and their likely cause.
- [Removing Graft](removing.md): what Graft keeps outside the app, and how to remove it from the Help menu or in Terminal.
- [Building from Source](building-from-source.md): you can build Graft.app from the repository with xcodegen and Xcode, and run its tests there.
