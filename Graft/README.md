# Graft.app (source)

The native macOS app for Graft. What Graft does, how to install it and how to
use it are in the [main README](../README.md); this file is about building the
app from source.

## How it works

- SwiftUI, with the Observation framework for its stores.
- Every action runs the `graft` CLI as a subprocess with `--json` and reads
  its newline-delimited JSON events. There is no daemon, no embedded Python and
  no second copy of the CLI's logic.
- The app and the CLI share `~/.config/graft/config.yaml` and
  `~/.local/state/graft/log.sqlite`.
- At launch, and again on Check again, the app reads your login shell's
  environment (your shell from the user database, run as `-l -i -c`, or
  `-i -c` for csh and tcsh, given 5 seconds), so the CLI sees the same
  `PATH`, `XDG_*` directories, `GRAFT_*` variables and `.p12` password
  variable as it does in Terminal. If the shell does not answer, the app
  carries on without it, and a later re-read that gets no answer keeps the
  last one that did.
- The app looks for `graft` in `~/.local/bin`, `/opt/homebrew/bin`,
  `/usr/local/bin` and `/opt/local/bin`, then on that `PATH`, so the CLI must
  be installed (`pipx install git+https://github.com/GreenglassT/Graft`).
- First-run setup happens in Terminal: until the CLI is installed and
  `graft --init` has written a config, the app shows a setup screen with the
  commands to run and checks again when you come back to it. Once it is set
  up, coming back to the window re-reads the app list, so an account change
  made in Terminal is picked up without a relaunch, and Settings' tool checks
  run again for the new account. A `config.yaml` that did not parse is read
  again too, so fixing it in an editor clears Settings' notice.
  `graft --setup-portal` has no GUI equivalent.

## Requirements

- macOS 15 or later (a Release build is universal, arm64 and x86_64; only
  Apple silicon has been tested)
- Xcode, and [XcodeGen](https://github.com/yonaskolb/XcodeGen) (`brew install xcodegen`)

## Build

The Xcode project is generated from `project.yml`:

```bash
cd Graft
xcodegen generate
open Graft.xcodeproj        # then Cmd+R
```

From the command line:

```bash
xcodebuild -project Graft/Graft.xcodeproj -scheme Graft -configuration Release \
  -destination 'platform=macOS' build
```

A local build is signed ad hoc. If macOS refuses to open the copy you built,
remove the quarantine attribute with `xattr -cr Graft.app`.

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
has talked to it, so without one those tests skip; check the skip count. To
see that skip path on a Mac that does serve the tree, add
`TEST_RUNNER_GRAFT_TESTS_PRETEND_NO_AX=1` to the command.

## Acknowledgements

`Graft/Credits.rtf` is what the standard About window shows (Graft > About
Graft): the licence and a pointer to
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md). The app links no
third-party code; Yams and SnapshotTesting are test-target dependencies only.
If a package is ever added to the app target, add its licence to both files.
