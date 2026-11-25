# Graft.app (source)

The native macOS app for Graft. What Graft does, how to install it and how to
use it are in the [main README](../README.md); this file is about building the
app from source.

## How it works

- SwiftUI, with the Observation framework for its stores.
- Every action runs the `graft` CLI as a subprocess with `--json` and reads its newline-delimited JSON events. There's no daemon, and no second copy of the CLI's logic.
- **Which `graft` it runs:**
  - A Release build always runs the one inside it, `Contents/Helpers/graft`: a compiled launcher for the embedded Python in `Contents/Resources/GraftPython`.
    - It never falls back to a `graft` on PATH.
    - If that file is missing, the setup screen says the app is damaged. If the whole `Contents/Helpers` folder is missing, it says this is a build with no CLI inside it (see Build, below), and if the app was moved while it was open, it says to quit and reopen it.
    - Settings › Command-line tool links that `graft` into `~/.local/bin` for Terminal. It won't while the app runs from App Translocation's temporary copy or a read-only disk image: move it to Applications first.
  - A Debug build (Xcode's Run, the test suite) looks the CLI up instead, as described below.
- The app and the CLI share `~/.config/graft/config.yaml` and
  `~/.local/state/graft/log.sqlite`.
- At launch, and again on Check again or Preflight's Re-run (at most once every 10 seconds), the app reads your login shell's
  environment (your shell from the user database, run as `-l -i -c`, or
  `-i -c` for csh and tcsh, given 5 seconds), so the CLI sees the same
  `PATH`, `XDG_*` directories, `GRAFT_*` variables and `.p12` password
  variable as it does in Terminal. If the shell does not answer, the app
  carries on without it, and a later re-read that gets no answer keeps the
  last one that did.
- A Debug build looks for `graft` in `~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin` and `/opt/local/bin`, then on that `PATH`. So for development, the CLI must be installed, for example as the repo's venv on `TEST_RUNNER_PATH` or with pipx.
- First-run setup happens in the app: Welcome, then "Do you have a paid Apple Developer account?" (`AccountSetupView`, running `graft --init --account …`). A Debug build first shows the commands that install the CLI. Once it is set up, coming back to the window re-reads the app list, so an account change made in Terminal is picked up without a relaunch, and Settings' tool checks run again for the new account. A `config.yaml` that did not parse is read again too, so fixing it in an editor clears Settings' notice.
- Everything else the CLI does has a place in the app, including the App
  Store Connect key (Settings, which runs `--set-portal`). What stays in
  Terminal: `--config`,
  `--verbose`, `--describe`, and building tools from source for the pipx
  install. App Store sign-in, Set Up Decryption and Get (free) are in
  Settings › App Store and the app pane.

## Requirements

- macOS 13 or later. That is `project.yml`'s deployment target, which `LSMinimumSystemVersion` follows; a release sets it for each download, macOS 14 for `Graft.zip` and 13 for `Graft-Intel.zip`. The app's own executable is universal (arm64 and x86_64) in both downloads; Info.plist's `GraftBundleArch` says which download a bundle is (`MacSupport`, checked against the embedded Python's Mach-O header; no key means arm64, as in a Debug build). `Graft.zip` bundles arm64 tools, and on an Intel Mac opens only to a screen that names `Graft-Intel.zip`. `Graft-Intel.zip` runs on either Mac, with App Store sign-in (Sign In… and Sign In Again…), App Store gets and Get an older version…, Set Up Decryption, Build for this Mac, Install to Mac and Get PlayCover disabled (no ipatool App Store request runs at all): on an Intel Mac each says "Needs a Mac with Apple silicon", and on an Apple silicon Mac "Needs the Apple silicon download of Graft (Graft.zip)", under a banner pointing to `Graft.zip`. On macOS 13, where `Graft.zip` doesn't open, both say it needs macOS 14 or later (`MacSupport.macOSMajorVersion`).
- Xcode, and [XcodeGen](https://github.com/yonaskolb/XcodeGen) (`brew install xcodegen`)

Xcode is for building the app. Running a release build needs neither Xcode
nor its Command Line Tools, nor libimobiledevice: the CLI it drives reads
binaries and installs apps itself, and the app carries PlayCover's stand-in
framework prebuilt. Decrypting on a jailbroken device is the exception: it
needs `iproxy` from libimobiledevice, and frida-ios-dump or bagbak. A Debug
build runs a CLI from outside the app, which builds that stand-in on demand,
with full Xcode's iOS SDK.

## Build

The Xcode project is generated from `project.yml`:

```bash
cd Graft
xcodegen generate
open Graft.xcodeproj        # then Cmd+R
```

From the command line:

```bash
xcodebuild -project Graft/Graft.xcodeproj -scheme Graft -configuration Debug \
  -destination 'platform=macOS' build
```

A local build is signed ad hoc and contains no CLI, so build Debug: a Debug build runs the CLI it finds. A Debug build's bundle id is `com.greenglasst.graft.debug`, so its preferences, the test suite's included, are its own and never the installed Graft's (`com.greenglasst.graft`, the Release id). Release builds come from `scripts/graft-release` (below). A plain Xcode Release build has no bundled CLI, and since a Release build only ever runs the one inside it, it opens on a screen saying it has none. If macOS refuses to open the copy you built, remove the quarantine attribute with `xattr -cr Graft.app`.

### The distributable app

`scripts/graft-release` builds the download: a Release with the CLI, its own Python and the tools inside it, signed with a Developer ID identity. By default it builds `Graft.zip`, for Apple silicon; `--arch x86_64` builds `Graft-Intel.zip`, for Intel Macs: the same app with its Python, tools and OpenSSL built for x86_64, and no UnfairPlay. The app's own executable is universal in both; everything else is the bundle's one architecture, and Info.plist's `GraftBundleArch` (`arm64` or `x86_64`, written before anything is signed) says which. Each bundle is built for its own oldest macOS (`MACOSX_DEPLOYMENT_TARGET` 14.0 for `Graft.zip`, 13.0 for `Graft-Intel.zip`), which sets Info.plist's `LSMinimumSystemVersion`; `verify-bundle` checks it and every Mach-O's minimum against that. The release also runs the Go tests of Graft's ipatool patch, and stops if one fails. An Intel release stops before building if the app's own Swift doesn't read `GraftBundleArch` (in code that's compiled, not in a comment or an `#if false` block), since its Intel Macs would otherwise see the old "needs Apple silicon" screen.

```bash
GRAFT_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" scripts/graft-release [--arch arm64|x86_64] [output-dir]
```

It runs from the repo's venv (`pip install -e '.[dev]'`). It downloads the pinned artifacts and PyPI wheels, and `codesign --timestamp` needs the network too; see [docs/network.md](../docs/network.md).

In order, it:
1. builds the app with coverage off and debug symbols stripped, so no local path ends up in the download;
2. stages everything else with `scripts/graft_bundle.py`, outside any `.app`;
3. assembles the app;
4. signs every Mach-O, then the app, with the hardened runtime and a secure timestamp;
5. checks the seal;
6. zips the app;
7. runs `scripts/verify-bundle` on the sealed app. A failure removes the zip;
8. writes the source archive (`scripts/graft_bundle.py sources`). It needs a checkout with no uncommitted changes, since it is HEAD's source.

The output directory (`build/release`, or `build/release-intel` for x86_64, by default) gets `Graft.app`, `Graft.zip` (or `Graft-Intel.zip`), `components.json` and `Graft-<version>-sources.tar.gz`. The source archive goes on the release page beside the zips: Graft's source, the pinned source of every binary either bundle builds from source, the PyPI source distribution of every copyleft Python package, and `SOURCES.md` with the build commands for both. Both releases write the same source archive.

**Nothing inside the app runs before `codesign --verify --deep --strict` passes.** Running code from an app with an incomplete seal makes macOS show "“Graft” is damaged" and block until someone answers.

`scripts/verify-bundle <Graft.app>` runs the bundled CLI, Python and tools in a sandbox: a temporary HOME, a system-only PATH, no keychain and no device. Use it on its own as well. For the Intel bundle, give `--arch x86_64` first; its checks run under Rosetta.

`scripts/verify-bundle --zip Graft.zip` checks a zip as a user receives it: it unpacks it, quarantines it as a browser would, and asks Gatekeeper. It runs nothing from a copy Gatekeeper rejects, and it always rejects a zip that isn't notarized.

The release isn't notarized; that's a separate step (see the comments in `scripts/graft-release`).

## Test

The tests run the real CLI in a few places, so point them at a venv that has
it:

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cd Graft
TEST_RUNNER_PATH="$PWD/../.venv/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  xcodebuild test -scheme Graft -destination 'platform=macOS'
```

A few tests (`AccessibilityTests` and the ones that read text off a hosted
window) go through the macOS Accessibility API. macOS serves an app's tree
only after an assistive client such as VoiceOver or Accessibility Inspector
has talked to it, so without one those tests skip; check the skip count. A
test that finds the tree not served asks again, three times half a second
apart, and so does the next one: only "served" is remembered, so a moment
when the tree isn't served skips only the tests that met it. To
see that skip path on a Mac that does serve the tree, add
`TEST_RUNNER_GRAFT_TESTS_PRETEND_NO_AX=1` to the command.

## Acknowledgements

`AboutGraft` in `Graft/GraftApp.swift` is what the standard About window shows (Graft > About
Graft): a short notice, `AboutGraft.licenceNotice`, saying Graft's own code is MIT, the app bundles programs under their own licenses, the CLI inside it is GPL-3.0-or-later, and the source is in `Graft-<version>-sources.tar.gz` on the release page, with this copy's version filled in, then a link to the releases page (`AboutGraft.releasesURL`). Release notes that quote it should quote that constant. [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) has the details.
- The app's own executable links no third-party code. Yams and SnapshotTesting are test-target dependencies only. If a package is ever added to the app target, add its license to both files.
- A release build bundles the components listed under "Bundled in Graft.app" in THIRD_PARTY_NOTICES.md.
  - `scripts/graft_bundle.py` collects their licenses into `Contents/Resources/Licenses`, with `INDEX.txt` and `components.json`.
  - Help › Licenses opens that folder.
