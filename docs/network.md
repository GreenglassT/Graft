# What Graft connects to

Every host that Graft.app, the `graft` CLI, or a tool bundled in the app contacts: why, when, and what it sends. It was gathered by reading the code (`graft_ipa/`, `Graft/Graft/`, the bundled Python packages), the pinned sources of ipatool 2.8.0 and UnfairPlay 0.4.0, and the bundled binaries' strings. It covers Graft 2.0.1.

**Short version:**
- Graft has no telemetry, analytics or crash reporting, and it doesn't update itself.
- It does check for updates: once a day while the app is open, the CLI asks GitHub for Graft's latest release, sending nothing that identifies you or your Mac (`api.github.com`, below). Settings › Updates turns that off.
- The app makes no connections of its own. Everything goes through the CLI and the tools it runs.
- Your Apple ID, your password and the 2FA code, if Apple asks for one, go only to Apple, through the bundled ipatool. You type them into Graft's sign-in sheet (Settings › App Store › Sign In…), or `graft --appstore-login` asks for them in Terminal. Graft passes them over stdin to ipatool's own prompts and never stores them. ipatool keeps the sign-in, including the password, in your login keychain.
- The first App Store action on a Mac (usually the first sign-in) downloads ipatool's Unicorn emulator from PyPI and about 320 MB of App Store components from Apple, once.
- Device UDIDs never leave your Mac.

## Graft's own requests

The CLI sends `User-Agent: graft/<version> (+https://github.com/GreenglassT/Graft)` with every request it makes through `requests`. There are two exceptions. Downloading icon images (`--artwork`, and the icons of `--appstore-search` results) sends the library's default User-Agent. The update check sends `User-Agent: Graft`, without the version.

**Bold** marks user data.

| Host | Contacted by | When | What's sent |
|---|---|---|---|
| `itunes.apple.com` (`/lookup`) | CLI: `--add-app`, `--appstore-check`, `--artwork`, the free-price check before `--appstore-claim`, and the version check before `--appstore-get` | `--add-app`: when you add an app. The app also runs the other two **on its own**: `--appstore-check` when an app's detail pane opens; `--artwork` at launch, when the app list changes, and after a build or App Store get, but only for apps with no icon cached or in a cached IPA. The price check: when a get is refused for want of a license, and before Get (free). The version check: before Get latest from App Store, only when the app has an IPA staged (not with `--again`, or Get Again). | The App Store id from the URL you gave, or the app's **bundle id**, and the two-letter store the app came from (the one Search asked, or the one its URL names; else this Mac's region's store, then the US store if that one doesn't sell it). No account. |
| `itunes.apple.com` (`/search`) | CLI: `--appstore-search` | When you type in Add App › Search: once you pause for half a second, for 2 or more characters, and only for a term not already searched in that sheet. Never on its own. | **The search term**, and a two-letter country code (the sheet's Store picker, which starts at this Mac's region; or `--country`). No account. |
| Apple's artwork CDN (whatever image URL the lookup returns) | CLI: `--artwork`, `--appstore-search` | After a lookup that found an icon; and for each search result's small icon not already cached (kept a week) | The request only |
| `api.canister.me` | CLI: `--search` (Canister), `--fetch <package id>`, `--tweak-updates` (the Tweaks update check), `--update-tweak` | When you search, fetch or check for tweak updates. The app never checks for tweak updates on its own. | **Search terms**; the **package ids of tweaks in your library** that the cached repo indexes don't cover |
| APT repository hosts (whichever repos you add, or the one Canister names for a package) | CLI: `--add-repo` (`Release`, `Packages.*`), `--repo-packages` (on a cache miss or `--refresh`), `--fetch <URL>`, `--update-tweak`, and the `.deb` download after a Canister result | When you add or refresh a repo, or fetch or update a tweak. Redirects are followed. | The request only |
| `api.appstoreconnect.apple.com` | CLI, paid accounts only: Settings › App Store Connect (`--set-portal`), `--setup-portal`, a sideload build with automatic provisioning, paid `--lc-install` | During setup, and when a build has to create or refresh a provisioning profile | An ES256 **JWT signed with your App Store Connect API key** (.p8), naming its issuer and key id; **bundle ids** (or wildcard patterns) and profile names. Graft reads the team's device list and matches UDIDs on the Mac; it never uploads one. |
| `api.github.com` (`/repos/GreenglassT/Graft/releases/latest`) | CLI: `--check-update` | **On its own**, at most once a day while the app is open: at launch, and every hour after, the app runs it if the last successful check was more than 24 hours ago or there hasn't been one. A failed check is tried again an hour later. Turn it off in Settings › Updates (Check for a newer Graft once a day). Also when you choose Help › Check for Updates…, whether that's on or off. | The request only, with `User-Agent: Graft`: no version, account, cookie or device data, and not a `~/.netrc` login. It compares the answer with the app's version on your Mac. Graft downloads and installs nothing; the banner links to the release page. |
| `github.com`, `api.github.com`, and `release-assets.githubusercontent.com`, where GitHub redirects the release files (the LiveContainer project's) | CLI: `--lc-status`, `--lc-install` | `--lc-status` runs **on its own** when a device's pane opens, and in Edit App for a LiveContainer app. `--lc-install` runs when you install LiveContainer. | The request only |

## Through the bundled ipatool

Graft runs ipatool 2.8.0 for everything that needs your Apple ID. ipatool 2.8 runs every App Store request through an emulator that runs Apple's own Store framework on Unicorn. Its source pins every host it signs in to and talks to with your session: an authenticated request goes only to `apple.com`, `*.apple.com`, `*.mzstatic.com` or `*.cdn-apple.com` (`internal/store/emulator/client.go`). A download goes to whatever URL Apple's answer names.

| What | When | Hosts | What's sent |
|---|---|---|---|
| Sign in (`--appstore-login`, which runs `ipatool auth login`) | When you press Sign In… in Settings › App Store, or run `graft --appstore-login`. Never on its own. | Apple's sign-in hosts, among them `init.itunes.apple.com` (the store's configuration), `auth.itunes.apple.com` and `gsa.apple.com` (sign-in, and this Mac's device provisioning), `*-buy.itunes.apple.com`, and `www.apple.com` (Apple's root certificate, for the sign-in connection) | Your **Apple ID, password and 2FA code**, to Apple only, and a machine identifier ipatool derives from this Mac |
| Get (`--appstore-get`, which runs `ipatool download -b <bundle id>`) | When you press Get latest from App Store (unless that version is already staged) or Get Again, and when you press Add in Add App › Search with "Get it from the App Store after adding" checked (it starts checked for a free app, and is shown only once the App Store is set up) | Apple's App Store hosts, among them `init.itunes.apple.com`, `*-buy.itunes.apple.com`, `uclient-api.itunes.apple.com`, `apps.apple.com` and `downloaddispatch.itunes.apple.com`, then the download URL Apple returns | Your **Apple ID session** (the token ipatool keeps in the keychain), the **bundle id**, and the machine identifier. If Apple has expired the session, ipatool signs in again with the password it keeps. |
| Get (free) (`--appstore-claim`, which runs `ipatool purchase -b <bundle id>`) | When you press Get (free) on an app's pane and confirm. Only for an app the iTunes lookup says is free; ipatool refuses a paid one too. Graft then starts the get. | As for a get | As for a get. This adds the app to your Apple ID's purchases, at no cost. |
| `updates.cdn-apple.com` (a macOS restore image, read in parts) and `wkms-public.apple.com` (the key for its encrypted parts) | The first App Store action on a Mac, while `~/Library/Caches/ipatool` lacks the Store framework the emulator runs: about 320 MB, kept there afterward. The image's address is fixed in ipatool's source (`internal/sap/assets/assets.go`), for macOS 27.0.1 (26A434). | as named | The request only |
| `files.pythonhosted.org` (Unicorn 2.1.4's macOS wheel) | The first App Store action on a Mac, while `~/Library/Caches/ipatool/unicorn/` lacks the library the emulator runs on; again only for a new Unicorn version | as named | The request only, with `User-Agent: ipatool Unicorn runtime downloader` |

About the first-use downloads:
- Unicorn isn't in Graft.app. The bundled ipatool downloads it from PyPI as upstream ipatool does, and Graft builds ipatool unpatched. It checks the wheel's and the library's SHA-256 against the ones its source pins, and the library's again before each load. As with upstream ipatool, other code running as you could swap the cached library between that last check and the load. PyPI sees your IP address and ipatool's User-Agent, nothing else.
- Offline, that first action fails with "ipatool needs to download its App Store emulator (Unicorn, from PyPI) the first time; connect to the internet and try again."
- A first sign-in can take several minutes while these download; the sign-in sheet says so.

What stays on the Mac:
- `--appstore-status` (Settings › App Store, and Add App › Search once you pick a result it could offer to get; never when the sheet opens, and never while you type) runs `ipatool auth info`. From ipatool's source, that only reads its keychain item; it doesn't contact Apple.
- Sign Out (`--appstore-logout`, `ipatool auth revoke`) only deletes keychain items.
- Graft asks `security` whether ipatool's keychain items exist, and deletes some of them, since reading another program's is a Keychain dialog. With Graft.app's own ipatool, it deletes the ones Graft's record doesn't show it made: another ipatool's (one you use in Terminal). With any other ipatool, it deletes only the ones Graft's record shows Graft.app's own made; a sign-in a third ipatool made is saved over instead, and macOS may ask for your login keychain password while it is. Before a sign-in, that is any of its three items. Before every Get and Get (free), whichever ipatool runs, it is the download cache, `kbsync-v1`, which ipatool fills again. That's local too.

## Your iPhone or iPad

Over USB or the local network, never the internet.

| What | Contacted by | When | What's sent |
|---|---|---|---|
| Your iPhone or iPad, through macOS's usbmuxd | CLI: device features (pymobiledevice3) | Whenever a device feature runs: listing, installing, pushing into LiveContainer, reading apps and icons | Commands and files for the device |
| A jailbroken device, over SSH and frida | CLI: `--decrypt`, `--decrypt-frontmost`, `--decrypt-check` (SSH through a local `iproxy` port forward, frida over USB) | When you dump an app, or the Decrypt card checks the device | Commands to the device, and the **device's SSH password**, sent only to the device |

## The bundled tools

- **ipatool 2.8.0:** as above. It is built from its pinned source, unpatched, and its only entitlements are `allow-jit` and `disable-library-validation`, which its emulator needs.
- **UnfairPlay 0.4.0:** no network code. It is built from its pinned source, unpatched, with no entitlements. Its Go build info lists only cobra, pflag and a plist library, with cgo off, and it imports no socket calls. Graft runs `unfairplay --version` and `unfairplay decrypt`, and Set Up Decryption runs `unfairplay setup` as root, offline.
- **zsign 1.1.3:** offline as Graft runs it (`-k -p -m -o`). It has an OCSP certificate check behind `-C/--check`, which Graft never passes.
- **cyan** and its helpers (ldid, insert_dylib, lipo, install_name_tool, otool): no network code.
- **pymobiledevice3:** everything Graft calls is local, through usbmuxd:
  - device listing and lockdown values;
  - InstallationProxy, AFC, HouseArrest and SpringBoardServices.

  The library's own network code isn't on any path Graft calls: personalization (TSS to `gs.apple.com`), developer disk image downloads, restore, activation and tunnels. Graft never mounts a developer image.
- **frida:** not bundled. Graft asks frida-ios-dump's own frida (`graft_ipa/frida_probe.py`) about local and USB devices only. Graft never uses remote frida.
- **Python and the other packages:** nothing connects on import, and nothing checks for updates.

## The app itself

Graft.app has no networking code: no `URLSession`, web views or remote images. It runs the bundled CLI, and the rows above are those CLI runs. Search results' icons, too, are downloaded by the CLI and drawn from its cache.

It opens these only when you click them, in your browser or in the app that handles them:
- App Store Connect's API keys page, from Settings › App Store Connect;
- a page in Graft's repository on GitHub, from the button on the screen an Intel Mac shows;
- Graft's releases page on GitHub (`github.com/GreenglassT/Graft/releases`), from the link under the license notice in Graft › About Graft;
- a newer Graft's release page on GitHub, from Release Page on the main window's update banner, or Open Release Page after Help › Check for Updates…;
- a page a link in Help › What's New… points to: Graft's releases, or a page of its documentation, on `github.com`;
- the guides the free-account setup's device steps link, each from its Open button: `sidestore.io`, iloader on `github.com`, and LiveContainer's installation guide on `livecontainer.github.io`;
- an app's `apps.apple.com` page, from Open in App Store on an app's pane, or the Mac App Store when Graft has no page for the app;
- the Mac App Store, from Open the Mac App Store in Set Up Decryption.

Help › Licenses opens a folder inside the app. Other URLs in Graft, such as install hints, only appear as text you can copy. Graft never fetches any of these pages.

## Building a release

`scripts/graft-release` and the scripts it runs download all of this. None of it happens on a user's Mac.

| Host | By | What for |
|---|---|---|
| `github.com`, redirecting to GitHub's download hosts, and `raw.githubusercontent.com` | `scripts/graft_bundle.py` | Every GitHub artifact in `bundle/pins.toml`, each checked against its SHA-256: python-build-standalone and its source (for license texts), zsign, the cyan archive, the sources of ipatool, UnfairPlay, OpenSSL, libplist, ldid, cctools-port and insert_dylib, and CPython's HACL* file (for its license). And, with `git fetch --depth=1`, UnfairPlay's v0.4.0 tag, for the commit its build info records (git runs with no configuration and no credential helper) |
| `go.dev` | `scripts/graft_bundle.py` | The Go 1.26.5 toolchain ipatool and UnfairPlay are built with, checked against its SHA-256 |
| `proxy.golang.org`, `sum.golang.org` | Go, in `graft_bundle.py` | ipatool's and UnfairPlay's Go modules (`go mod vendor`), each checked against its go.sum and the checksum database |
| `pypi.org`, `files.pythonhosted.org` | pip, in `graft_bundle.py`; pip-compile, in `scripts/lock-bundle`; `graft_bundle.py sources` | The packages in `bundle/requirements.lock` and `bundle/build-requirements.lock`: wheels only, `--require-hashes`, chosen for macOS 14 on arm64. graft and cyan install with `--no-index`. And, for the source archive and the Rust crates' licenses, each package's JSON at `pypi.org/pypi/<name>/<version>/json` and its source distribution, checked against the lock. Unicorn isn't fetched: the app doesn't carry it. |
| `static.crates.io` | `graft_bundle.py` | For its license, each Rust crate in pydantic_core's, qh3's and cryptography's SBOMs and in bcrypt's Cargo.lock, checked against its Cargo.lock checksum, and each one ipatool's 1Password SDK core shows it holds, checked against `bundle/ipatool-core-crates.lock`; and for the source archive, the one of those under MPL-2.0 only (nom-supreme) |
| `github.com` | xcodebuild (Swift Package Manager) | The app's package dependencies, when they aren't already cached |
| `timestamp.apple.com` | `codesign --timestamp` | A secure timestamp for each signed file |

Notarization (`xcrun notarytool`) is a separate, manual step, and it uploads the zip to Apple.
