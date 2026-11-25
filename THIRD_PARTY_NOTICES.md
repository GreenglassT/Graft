# Third-party notices

Graft's own code is licensed under the MIT License (see [LICENSE](LICENSE)).

**Graft.app is an aggregate of separate programs and libraries, each under its own license; no one license covers the app as a whole. Graft's own Python program (the graft command-line tool and the Python packages it runs, pymobiledevice3 among them) is distributed as a whole under GPL-3.0-or-later, while each package in it keeps its own license, so paramiko and pylzss stay LGPL. Graft's own source code is MIT. The other components keep their own licenses, among them AGPL-3.0 (ldid), APSL-2.0 (cctools), LGPL (libplist and a module inside ipatool), MPL-2.0 (a module inside ipatool), Apache-2.0, BSD and MIT. Unicorn is not part of Graft.app: the bundled ipatool downloads Unicorn 2.1.4 (GPL-2.0) from PyPI on first use, as upstream ipatool does, checked against the SHA-256 its source pins.** The source archive on each release page (`Graft-<version>-sources.tar.gz`) holds the corresponding source: see its `SOURCES.md`.

In the app, Help › Licenses opens the folder with:
- the GPL-3.0 text;
- every bundled component's license and notice files;
- `INDEX.txt`, one line per component;
- `components.json`, each component's exact source file and its SHA-256.

## Bundled in Graft.app

Everything below is inside the app (`Contents/Helpers`, `Contents/Frameworks` and `Contents/Resources/GraftPython`). The pins are in `bundle/pins.toml`, `bundle/requirements.lock` and `bundle/build-requirements.lock`, and `scripts/graft_bundle.py` checks each download's SHA-256 before using it.

| Component | Version | License | What it is |
|---|---|---|---|
| CPython ([python-build-standalone](https://github.com/astral-sh/python-build-standalone)) | 3.13.18 (build 20261026), aarch64 | PSF-2.0 | The `graft` CLI's own Python |
| [OpenSSL](https://github.com/openssl/openssl) | 3.5.9, built from the pinned source release | Apache-2.0 | `libssl`, `libcrypto` and the legacy provider, for zsign |
| [ipatool](https://github.com/majd/ipatool) | 2.8.0, built from its source (commit `72c47d5`) with Go 1.26.5, with its reads of an older version's details made in a few large range requests (`bundle/patches/ipatool-range-readahead.patch`) | MIT; its Go modules' licenses (BSD, MIT, Apache-2.0, MPL-2.0 for golang-lru, LGPL-3.0 with a linking exception for retry.v1, the Unlicense for mpb, public domain for xi2/xz) are in `Licenses/ipatool-modules/`, and those of the Rust crates in its 1Password SDK core (MIT, Apache-2.0, BSD-3-Clause, ISC, Unicode-3.0, MPL-2.0 for nom-supreme) in `Licenses/rust-crates/ipatool/` | App Store downloads |
| [UnfairPlay](https://github.com/DrWhax/unfairplay) | 0.4.0, built from its unpatched source (commit `d750308`, the tag's) with Go 1.26.5 and the flags of upstream's release; its Go build info names that commit, which the release checks | MIT, with Apache-2.0 code adapted from DarkPhoenix, as its `NOTICE` says; its `NOTICE`, `LICENSE-DARKPHOENIX.txt` and `LICENSE-MACOSFAIRFREE.txt` are shipped with it, and its Go modules' licenses (Apache-2.0 for cobra, BSD-3-Clause for pflag and plist) are in `Licenses/unfairplay-modules/` | Decrypting App Store IPAs |
| [zsign](https://github.com/zhlynn/zsign) | 1.1.3, macOS arm64 release | MIT | Signing, for sideload delivery |
| cyan ([pyzule-rw](https://github.com/asdfzxcvbn/pyzule-rw)) | 1.4.4, commit `04a40f2` | Unlicense | Injecting tweaks |

**Not bundled: Unicorn.** ipatool runs the App Store's own code on [Unicorn](https://github.com/unicorn-engine/unicorn) 2.1.4 (GPL-2.0), which Graft.app does not ship. As upstream ipatool does, the bundled ipatool downloads Unicorn's macOS wheel from PyPI (`files.pythonhosted.org`) the first time an App Store action needs it, checks the wheel's and the library's SHA-256 against the ones its source pins (`internal/sap/unicorn/artifact.go`), keeps it in `~/Library/Caches/ipatool/unicorn/`, and checks the library's hash again before every load. That download is why ipatool, and only ipatool, is signed with `com.apple.security.cs.disable-library-validation` beside `allow-jit`: the downloaded library is ad hoc signed. ipatool checks the file and then loads it by its path, as upstream ipatool does, so code already running as you could swap the file in between and run inside ipatool, which holds your App Store sign-in.

**cyan's helper binaries.** cyan's archive carries prebuilt copies of five tools with no record of how they were built. The release builds each from pinned upstream source instead (arm64, macOS 14.0 or later) and puts it in their place, under the name cyan runs it by; the stage refuses to finish if any of cyan's original bytes is left. Each source archive is pinned in `bundle/pins.toml`, and `SOURCES.md` in the release's source archive gives the exact build commands.

| Helper | Built from | License |
|---|---|---|
| `ldid` | [ProcursusTeam/ldid](https://github.com/ProcursusTeam/ldid) v2.1.5-procursus7, linking the bundled OpenSSL 3.5.9 and libplist | AGPL-3.0 |
| `libplist-2.0.dylib`, in `Contents/Frameworks`, for ldid | [libplist](https://github.com/libimobiledevice/libplist) 2.8.0, a shared library of its own so it can be replaced. To replace it, work on a copy of Graft.app: put your build in its place, then re-sign ldid ad hoc, `codesign --force --sign -`, since the hardened runtime loads only the release team's libraries (the same for pylzss's module and `python3.13`). Last, re-seal the whole app ad hoc, `codesign --force --sign - Graft.app`, or macOS reports it as damaged. That replaces the release team's signature, so Set Up Decryption can no longer check its copy of UnfairPlay against the team, and runs it only after you press Run Unchecked Copy as Root… | LGPL-2.1-or-later |
| `lipo`, `otool`, `install_name_tool` | Apple cctools 986, from [cctools-port](https://github.com/tpoechtrager/cctools-port) commit `e101a69`, with its fake-sign hook compiled out (`bundle/patches/cctools-port-no-fake-sign.patch`) | APSL-2.0 |
| `insert_dylib` | [Tyilo/insert_dylib](https://github.com/Tyilo/insert_dylib) commit `ce8b08c` | MIT |

cyan's archive also carries iOS frameworks that cyan injects into an IPA when a tweak needs one. They ship as cyan's archive carries them, byte for byte and unsigned by the release (verify-bundle checks both), each with its license text in `Licenses/cyan-extras/`:

| Framework | What it is | License |
|---|---|---|
| `CydiaSubstrate.framework` | [ElleKit](https://github.com/tealbathingsuit/ellekit) 1.1.3 | BSD-3-Clause |
| `Cephei.framework`, `CepheiPrefs.framework`, `CepheiUI.framework` | [Cephei](https://github.com/hbang/libcephei) 1.17 (its Info.plists say 1.0.0) | Apache-2.0, with its credits and the MIT notice for CompactConstraint |
| `Orion.framework` | [Orion](https://github.com/theos/orion) 1.0.2 | MIT |

**PlayCover's stub.** `Preferences.framework`, a stand-in for the iOS private framework a few settings tweaks link, is Graft's own (`graft_ipa/stubs/preferences-stub/`, MIT), built at release with Xcode's iOS SDK.

**Code linked into the binaries above.** Help › Licenses also holds the notices of code compiled into them: `python-<library>-LICENSE.txt` for each library python-build-standalone builds into Python (bzip2, libffi, mpdecimal, liblzma, expat, SQLite, libuuid, OpenSSL and HACL\*); `ipatool-modules/` and `unfairplay-modules/` for every Go module in each, read from each binary's own build info, whose source is in the source archive too (in ipatool, golang-lru is MPL-2.0, retry.v1 LGPL-3.0 with a static-linking exception, mpb under the Unlicense and xi2/xz in the public domain); `go-LICENSE` for the Go standard library; `rust-crates/<package>/` for every Rust crate in pydantic_core, qh3, cryptography and bcrypt, each checked against the package's `Cargo.lock` (bcrypt's wheel has no SBOM, so every crate its `Cargo.lock` pins is there, build-only ones too); and `rust-crates/ipatool/` for the crates.io crates ipatool's embedded 1Password SDK core shows it holds. That core is a prebuilt WebAssembly module, and 1Password publishes no `Cargo.lock` for it, so the list is read out of the binary: each crate whose source path, function names or wasm-bindgen record it carries, pinned with its checksum in `bundle/ipatool-core-crates.lock`. Where the core carries no version, the lock's header says how the one pinned was inferred; a crate that left none of those traces can't be seen. One of those crates, nom-supreme, is MPL-2.0 with no other license to choose, so its source is in the source archive too. The release refuses a bundled binary that shows a Rust crate with no notice here, at the version pinned.

### Python packages

The embedded Python holds 100 distributions. Two of them are graft itself, built from the release's source, and cyan. The other 98 are installed from PyPI as wheels, each checked against its hash; the compiled modules of the few universal2 wheels are then cut down to their arm64 slice. `INDEX.txt` and `components.json` list every one, with its version and license, and each package's own license files are in `Licenses/python-packages/`.

The copyleft and weak-copyleft ones:

| Package | License |
|---|---|
| pymobiledevice3 | GPL-3.0-or-later |
| ipsw-parser, opack2, pygnuutils, pyiosbackup, pytun-pmd3, pmd-net-addr, pmd-net-proto, pmd-pytcp, remotezip2 (pymobiledevice3's dependencies) | GPL-3.0 or later |
| paramiko | LGPL-2.1 |
| pylzss | LGPL-3.0 |
| certifi | MPL-2.0 |

The rest use MIT, BSD, Apache-2.0, ISC, PSF or similar permissive licenses. Three of them, apple-compress, ASGIMiddlewareStaticFile and ASGIWebDAV, state no license in their metadata, and each ships an MIT license file. Three others, bpylist2, enum-compat and loguru, say MIT in their metadata but ship no license file; `Licenses/python-packages/` carries each one's text from its upstream repository at the installed version (`bundle/licenses/`, each naming its source).

## The app's own code

`Contents/MacOS/Graft`, the SwiftUI app, links no third-party code. Its Swift package dependencies are used only by the test target, and they aren't distributed:

- Yams 5.4.0 (https://github.com/jpsim/Yams, MIT), to parse YAML fixtures in the tests.
- SnapshotTesting 1.19.2 (https://github.com/pointfreeco/swift-snapshot-testing, MIT), for snapshot tests.

Versions are pinned in `Graft/Graft.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved`.

## The CLI on its own (pipx)

A `pipx install` of the CLI bundles nothing. pip fetches the same dependencies from PyPI, each under its own license, and the tools are ones you install yourself.

## Not bundled

Graft can also run tools you install yourself: iproxy (libimobiledevice), bagbak, frida-ios-dump and PlayCover. None of them is included with Graft, and neither is frida: Graft asks frida through frida-ios-dump's own virtualenv.

LiveContainer isn't redistributed by Graft. `graft --lc-install` downloads it from its upstream GitHub releases and installs it on your own device.
