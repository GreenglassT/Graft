# Installing the CLI

Put the `graft` command on your Mac, either from Graft.app or with pipx.
Graft in Terminal is optional; everything in the User Guide works without it.

## Install graft for Terminal

In Graft.app, open Settings › Command-line tool and press **Install graft for
Terminal**. It links the app's own `graft` into `~/.local/bin`.

- If `~/.local/bin` isn't on your shell's PATH, the section shows the line to
  add to your shell's startup file, with a Copy button. Open a new Terminal
  window afterward.
- The command runs the CLI inside the app, so it updates with the app, and
  Terminal and the app can't drift apart.
- If another `graft` is already in `~/.local/bin` (a pipx install, say), the
  section says where it points and offers **Replace it…**. If a `graft` earlier
  on your PATH would win, it says that one is what Terminal runs.
- Move Graft.app to Applications first: the button won't link a copy running
  from a disk image or from where macOS put a downloaded app it moved aside.

## The CLI without the app (pipx)

To use Graft without the app, or to run it from source, install the CLI with
pipx:

```bash
pipx install git+https://github.com/GreenglassT/Graft     # brew install pipx, if you need it
pipx ensurepath
```

`pipx ensurepath` adds `~/.local/bin` to your PATH, so open a new terminal
afterward for the `graft` command to be found.

- It needs Python 3.11 or later (the test suite passes on 3.11, 3.12, 3.13
  and 3.14).
- Graft.app never uses a pipx install. It always runs the copy inside it.
- To update it, run `pipx reinstall graft-ipa`. Once a new version is tagged on
  GitHub, `pipx upgrade graft-ipa` works too.

### Tools a pipx install needs

A pipx install doesn't include the tools Graft.app carries. Install the ones
you use yourself:

| Tool | Used for | Install |
|---|---|---|
| `cyan` ([pyzule-rw](https://github.com/asdfzxcvbn/pyzule-rw)) | injecting tweaks | `pipx install --force https://github.com/asdfzxcvbn/pyzule-rw/archive/04a40f2aa65ec9cb36127f74d52ae4ef71377c50.zip` (the commit tagged v1.4.4, the release Graft is tested with) |
| `zsign` | signing, for Sideload delivery (paid only) | `brew install zsign` |
| [ipatool](https://github.com/majd/ipatool) 2.8.0 | App Store downloads | `graft --appstore-status` prints the commands that download the release for your Mac's processor and put it at `~/.local/bin/ipatool`. Use 2.8.0: 2.8.1 has a download bug, and Graft refuses it. |
| [UnfairPlay](https://github.com/DrWhax/unfairplay) v0.4.0 | decrypting App Store downloads, on Apple silicon | A Go program, built from source to `~/.local/share/graft/unfairplay/unfairplay`. `graft --appstore-status` prints the clone, checkout and `go build` commands. |
| Full Xcode, selected (`sudo xcode-select -s /Applications/Xcode.app/Contents/Developer`) | a PlayCover build whose tweak links an iOS private framework | The stand-in framework is compiled with Xcode's iOS SDK, which the Command Line Tools don't have. Only such a build needs it, and only at that step; `--check` doesn't look for it. |

`graft --check` lists each missing tool with the command that installs it, and
only the tools your apps' delivery modes use: zsign only when some app
sideloads (or, on a paid config with no apps yet, since a new app sideloads by
default). The `brew` commands need [Homebrew](https://brew.sh), and `--check`
says so when it isn't installed. Neither kind of install needs
libimobiledevice or the Xcode Command Line Tools for phone builds: Graft reads
binaries and installs apps itself, through pymobiledevice3.

### Where each tool comes from

- **cyan and zsign:** Graft.app's own copy, then your PATH. config.yaml has no
  setting for either.
- **ipatool and UnfairPlay:** a path you set in config.yaml, then Graft.app's
  copy, then their usual install location, then your PATH (so a Homebrew
  ipatool is found too).
  - Set them in an `appstore:` section (`ipatool:`, `unfairplay:`,
    `unfairplay_models:`), as full paths starting with `/` or `~/`. A relative
    one is ignored, with a warning each time Graft runs, and read as not set.
  - A path you set that points at nothing (you removed that ipatool, say):
    Graft.app, and the `graft` it links for Terminal, run their own copy
    instead, and `--appstore-status` notes the setting it passed over. Remove
    the key to stop the note. A pipx `graft` reports the tool as missing at
    that path.
  - An `appstore.ipatool` of `~/.local/bin/ipatool`, or an
    `appstore.unfairplay` of `~/.local/share/graft/unfairplay/unfairplay`,
    counts as not set, so Graft.app's copy still comes first.
    `--appstore-status` says so for ipatool, and marks the UnfairPlay that
    runs "(bundled)". To make your own ipatool win, put it somewhere else and
    set the path.
  - Without `unfairplay_models:`, Graft uses UnfairPlay's own default:
    `UNFAIRPLAY_MODEL_DIR` if set, else
    `~/Library/Application Support/unfairplay/models`.
  - Setting up decryption in Graft.app always uses Graft.app's own UnfairPlay,
    because that step runs it as root.

`--check` and `--appstore-status` say which copy they found, and mark
Graft.app's own "(bundled)".

## Troubleshooting a pipx install

| Symptom | Likely cause |
|---|---|
| `killed: 9` from zsign | Quarantine attribute on a zsign you installed: `xattr -cr "$(which zsign)"`. |
| A PlayCover build stops saying it needs Xcode | The tweak links an iOS private framework, and the pipx CLI builds its stand-in with Xcode's iOS SDK. Install and select full Xcode, or build from Graft.app, which carries the stand-in. |
