# Building from Source

You can build Graft.app from the repository with xcodegen and Xcode, and run
its tests there.

```bash
brew install xcodegen
cd Graft && xcodegen generate
open Graft.xcodeproj
```

Or from the command line:

```bash
xcodebuild -project Graft/Graft.xcodeproj -scheme Graft -configuration Debug \
  -destination 'platform=macOS' build
```

Build Debug: it runs the `graft` it finds on your PATH, and its setup screen
first shows the commands that install the CLI, each with a Copy button.
Release builds come from `scripts/graft-release`, below. A plain Xcode Release
build has no CLI inside it, so it opens on a screen saying so.

Tests:

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
cd Graft && TEST_RUNNER_PATH="$PWD/../.venv/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  xcodebuild test -scheme Graft -destination 'platform=macOS'
```

The app is SwiftUI. It runs the `graft` CLI as a subprocess for every action
and reads its `--json` events. Both share `config.yaml` and `log.sqlite`, and
there's no daemon.
- A release build carries the CLI and its own Python inside the app.
- A build from Xcode runs the CLI it finds on your PATH, such as the repo's
  venv.

`scripts/graft-release` builds the distributable app: everything bundled
(arm64, with a universal app executable that shows Intel Macs a "needs Apple
silicon" screen), signed with a Developer ID identity and zipped. Then
`scripts/verify-bundle` checks the sealed app. See
[Graft/README.md](../../Graft/README.md), and [docs/network.md](../network.md) for
every host Graft and its tools contact, including what a release build
downloads.
