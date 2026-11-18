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
- The app looks for `graft` in `~/.local/bin`, `/opt/homebrew/bin` and
  `/usr/local/bin`, so the CLI must be installed (`pipx install
  git+https://github.com/GreenglassT/Graft`).
- First-run setup is terminal only: `graft --init` and `graft --setup-portal`
  have no GUI equivalent.

## Requirements

- macOS 15 or later on Apple silicon
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

## Acknowledgements

`Graft/Credits.rtf` is what the standard About window shows (Graft > About
Graft): the licence and a pointer to
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md). The app links no
third-party code; Yams and SnapshotTesting are test-target dependencies only.
If a package is ever added to the app target, add its licence to both files.
