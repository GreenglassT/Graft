# Third-party notices

Graft's own code is licensed under the MIT License (see [LICENSE](LICENSE)).

## Graft.app

`Graft.app` contains no third-party code. Its Swift package dependencies are
used only by the test target and are not distributed with the app:

- Yams 5.4.0 (https://github.com/jpsim/Yams, MIT), to parse YAML fixtures in
  the tests.
- SnapshotTesting 1.19.2 (https://github.com/pointfreeco/swift-snapshot-testing,
  MIT), for snapshot tests.

Versions are pinned in
`Graft/Graft.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved`.

## Python dependencies (not bundled)

The `graft` CLI's dependencies are installed by pip (or pipx) from PyPI when
you install Graft. Graft does not bundle, vendor or redistribute them; each is
under its own licence. The ones whose licences differ most from MIT:

| Package | License |
|---|---|
| pymobiledevice3 | GPL-3.0-or-later |
| paramiko | LGPL-2.1 |
| frida | wxWindows Library Licence 3.1 |

pymobiledevice3 also pulls in several GPL-3.0-or-later packages of its own
(for example developer_disk_image, ipsw-parser and pycrashreport). The
remaining direct dependencies (PyYAML, requests, rich, typer, PyJWT,
cryptography) use MIT, Apache-2.0 or BSD licences.

Because of pymobiledevice3, a bundled or frozen build of the CLI (for example
with PyInstaller) that includes these packages would have to be distributed
under the GPL.

## External tools (not bundled)

Graft runs tools you install yourself, such as cyan (pyzule-rw), zsign,
libimobiledevice, ideviceinstaller, ipatool, UnfairPlay, bagbak,
frida-ios-dump and PlayCover. None of them is included with Graft.
LiveContainer is downloaded from its upstream GitHub releases by
`graft --lc-install` and installed on your own device; it is not redistributed
by Graft.
