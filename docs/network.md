# What Graft connects to

Every host that Graft.app, the `graft` CLI, or a tool bundled in the app contacts: why, when, and what it sends. It was gathered by reading the code (`graft_ipa/`, `Graft/Graft/`, the bundled Python packages), the pinned sources of ipatool 2.8.0 and UnfairPlay 0.4.0, and the bundled binaries' strings. It covers Graft 2.0.2.

**Short version:**
- Graft has no telemetry, analytics or crash reporting.
- It checks for updates: once a day while the app is open, the CLI asks GitHub for Graft's latest release, sending nothing that identifies you or your Mac (`api.github.com`, below). When there's a newer one, the CLI downloads its zip of your build (`Graft.zip`, or `Graft-Intel.zip` for the Intel build) from GitHub (`github.com`, below), checks it, and installs it when you quit Graft. Settings › Updates turns either off.
- It checks your apps for updates: at launch and once a day while the app is open, after a get, a build, a tweak update or an update, and when a device connects or disconnects while an update waits for one (at most every 10 minutes), the CLI looks up your apps' **bundle ids** in Apple's public App Store lookup (one request per App Store country) and your attached tweaks' **package ids** at canister.me, only for tweaks no repo index cached in the last 24 hours, with no Apple ID and nothing that identifies your Mac (below). The check opens no connection to your iPhone or iPad and reads nothing from the keychain. Settings › Updates › Check my apps for updates turns all of these off; Check for updates now still checks, whether it's on or off.
- The app makes no connections of its own. Everything goes through the CLI and the tools it runs.
- Your Apple ID, your password and the 2FA code, if Apple asks for one, go only to Apple, through the bundled ipatool. You type them into Graft's sign-in sheet (Settings › App Store › Sign In…), or `graft --appstore-login` asks for them in Terminal. Graft passes them over stdin to ipatool's own prompts and never stores them. ipatool keeps the sign-in, including the password, in your login keychain.
- The first App Store action on a Mac (usually the first sign-in) downloads ipatool's Unicorn emulator from PyPI and about 320 MB of App Store components from Apple, once.
- On an Intel Mac, and in the Intel build (`Graft-Intel.zip`) anywhere, Graft makes no App Store sign-in or download at all, so none of the ipatool hosts below is contacted. Searching the App Store and adding an app by name use Apple's public lookup and need no sign-in.
- Device UDIDs never leave your Mac.

## Graft's own requests

The CLI sends `User-Agent: graft/<version> (+https://github.com/GreenglassT/Graft)` with every request it makes through `requests`. There are two exceptions. Downloading icon images (`--artwork`, and the icons of `--appstore-search` results) sends the library's default User-Agent. The update check and the update download send `User-Agent: Graft`, without the version.

**Bold** marks user data.

| Host | Contacted by | When | What's sent |
|---|---|---|---|
| `itunes.apple.com` (`/lookup`) | CLI: `--add-app`, `--appstore-check`, `--update` (and `--update --plan`), `--artwork`, the free-price check before `--appstore-claim`, and the version check before `--appstore-get` | `--add-app`: when you add an app. The app also runs the other two **on its own**: `--appstore-check` when an app's detail pane opens; `--artwork` at launch, when the app list changes, and after a build or App Store get, but only for apps with no icon cached or in a cached IPA. The price check: when a get is refused for want of a license, and before Get (free). The version check: before Get latest from App Store, only when the app has an IPA staged (not with `--again`, or Get Again). `--update` asks once per store country, naming every app of that store in one request (`bundleId=a,b,c`). | The App Store id from the URL you gave, or the app's **bundle id**, and the two-letter store the app came from (the one Search asked, or the one its URL names; else this Mac's region's store, then the US store if that one doesn't sell it). No account. |
| `itunes.apple.com` (`/search`) | CLI: `--appstore-search` | When you type in Add App › Search: once you pause for half a second, for 2 or more characters, and only for a term not already searched in that sheet. Never on its own. | **The search term**, and a two-letter country code (the sheet's Store picker, which starts at this Mac's region; or `--country`). No account. |
| Apple's artwork CDN (whatever image URL the lookup returns) | CLI: `--artwork`, `--appstore-search` | After a lookup that found an icon; and for each search result's small icon not already cached (kept a week) | The request only |
| `api.canister.me` | CLI: `--search` (Canister), `--fetch <package id>`, `--tweak-updates` (the Tweaks update check), `--update-tweak`, `--update` (and `--update --plan`) | When you search, fetch or check for tweak updates. On its own, only in the daily check for app updates (below). | **Search terms**; the **package ids of tweaks in your library** that the cached repo indexes don't cover (for `--update`, only those attached to the apps it covers) |
| APT repository hosts (whichever repos you add, or the one Canister names for a package) | CLI: `--add-repo` (`Release`, `Packages.*`), `--repo-packages` (on a cache miss or `--refresh`), `--fetch <URL>`, `--update-tweak`, `--update`, and the `.deb` download after a Canister result | When you add or refresh a repo, or fetch or update a tweak. Redirects are followed. | The request only |
| `api.appstoreconnect.apple.com` | CLI, paid accounts only: Settings › App Store Connect (`--set-portal`), `--setup-portal`, a sideload build with automatic provisioning, paid `--lc-install` | During setup, and when a build has to create or refresh a provisioning profile, or a build signed with a profile Graft makes, or a LiveContainer install, targets a device missing from the saved list (Graft lists your registered devices; it never registers one) | An ES256 **JWT signed with your App Store Connect API key** (.p8), naming its issuer and key id; **bundle ids** (or wildcard patterns) and profile names. Graft reads the team's device list and matches UDIDs on the Mac; it never uploads one. |
| `api.github.com` (`/repos/GreenglassT/Graft/releases/latest`) | CLI: `--check-update`, `--download-update` | `--check-update` runs **on its own**, at most once a day while the app is open: at launch, and every hour after, the app runs it if the last successful check was more than 24 hours ago or there hasn't been one. A failed check is tried again an hour later. Turn it off in Settings › Updates (Check for updates automatically). Also when you choose Help › Check for Updates…, whether that's on or off. `--download-update` asks again, for the release's files and their SHA-256s, right before it downloads (next row). | The request only, with `User-Agent: Graft`: no version, account, cookie or device data, and not a `~/.netrc` login. It compares the answer with the app's version on your Mac. |
| `github.com` (`/GreenglassT/Graft/releases/download/<tag>/Graft.zip`, or `Graft-Intel.zip` for the Intel build), redirecting to GitHub's file host (`release-assets.githubusercontent.com`) | CLI: `--download-update` | **On its own**, when a check (the daily one, or Help › Check for Updates…) found a newer Graft and Settings › Updates' Download and install updates automatically is on (it is by default): once per new version each time Graft is open, and not again on its own for a version that failed its checks. With that off, only when you choose Update on the banner or in Check for Updates…. Not when Graft can't replace itself where it is (a copy macOS runs from Downloads, or a folder your account can't change); the banner then links to the release page. About 150 MB, kept in Graft's cache (`~/.cache/graft/updates`) until it's installed. | The request only, with `User-Agent: Graft`, and not a `~/.netrc` login. Before it calls the download ready, Graft checks that it matches the SHA-256 GitHub lists for it, then that the app inside is Graft (its bundle id), newer than yours, the same build as yours (Apple silicon or Intel: its Info.plist says so, and its main executable and embedded Python have code for that processor), signed by the same developer as your Graft (its Developer ID team) and notarized by Apple, with macOS's `codesign` and `spctl`. A download that fails any check is deleted. `spctl` is Gatekeeper, which may ask Apple whether that signature or ticket has been revoked, as it does when you open any newly downloaded app. Installing it needs no connection. |
| `github.com`, `api.github.com`, and `release-assets.githubusercontent.com`, where GitHub redirects the release files (the LiveContainer project's) | CLI: `--lc-status`, `--lc-install` | `--lc-status` runs **on its own** when a device's pane or its Set Up LiveContainer… sheet opens, after each LiveContainer install, certificate push or removal and clean-up on a device, and in Edit App for a LiveContainer app. `--lc-install` runs when you press Set Up or Install / Update LiveContainer in Set Up LiveContainer…. | The request only |

### Checking your apps for updates

| When | Who it contacts | What for |
|---|---|---|
| At launch and once a day while Graft is open, and after a get, build, tweak update, update or change to an app's options or tweaks, or when a device connects or disconnects while an update waits for one (at most every 10 minutes), unless Settings › Updates › Check my apps for updates is off; and when you press Check for updates now | Apple's public iTunes lookup (one request per App Store country), and canister.me for attached tweaks with no recently cached repo index | Your apps' bundle IDs and your attached tweaks' package IDs, to see whether newer versions are out. No Apple ID. |

The check opens no connection to your iPhone or iPad and reads nothing from the keychain.

## Through the bundled ipatool

Graft runs ipatool 2.8.0 for everything that needs your Apple ID. ipatool 2.8 runs every App Store request through an emulator that runs Apple's own Store framework on Unicorn. Its source pins every host it signs in to and talks to with your session: an authenticated request goes only to `apple.com`, `*.apple.com`, `*.mzstatic.com` or `*.cdn-apple.com` (`internal/store/emulator/client.go`). A download goes to whatever URL Apple's answer names.

| What | When | Hosts | What's sent |
|---|---|---|---|
| Sign in (`--appstore-login`, which runs `ipatool auth login`) | When you press Sign In… in Settings › App Store, or run `graft --appstore-login`. Never on its own. | Apple's sign-in hosts, among them `init.itunes.apple.com` (the store's configuration), `auth.itunes.apple.com` and `gsa.apple.com` (sign-in, and this Mac's device provisioning), `*-buy.itunes.apple.com`, and `www.apple.com` (Apple's root certificate, for the sign-in connection) | Your **Apple ID, password and 2FA code**, to Apple only, and a machine identifier ipatool derives from this Mac |
| Get (`--appstore-get`, which runs `ipatool download -b <bundle id>`) | When you press Get latest from App Store (unless that version is already staged) or Get Again, and when you press Add in Add App › Search with "Get it from the App Store after adding" checked (it starts checked for a free app, and is shown only once the App Store is set up) | Apple's App Store hosts, among them `init.itunes.apple.com`, `*-buy.itunes.apple.com`, `uclient-api.itunes.apple.com`, `apps.apple.com` and `downloaddispatch.itunes.apple.com`, then the download URL Apple returns | Your **Apple ID session** (the token ipatool keeps in the keychain), the **bundle id**, and the machine identifier. If Apple has expired the session, ipatool signs in again with the password it keeps. |
| Older versions (`--appstore-versions`, which runs `ipatool list-versions -b <bundle id>`, then `ipatool get-version-metadata -b <bundle id> --external-version-id <id>` for each version it reads) | When Get an older version… opens on an app's pane, and when you press Show More there: one list, then one request per version not read before (ten at a time). Graft remembers what it read. | As for a get. `get-version-metadata` reads that version's `Info.plist` out of its IPA at the download URL Apple returns, by range requests: the last MB of the IPA, where its list of files is, and the MB around the `Info.plist`. That's about 2 MB per version, a few more for an app with tens of thousands of files. | As for a get, and the **version ids**. |
| Get an older version (`--appstore-get --version-id <id>`, which runs `ipatool download -b <bundle id> --external-version-id <id>`) | When you press Get on a version in Get an older version…, unless Graft already staged that version | As for a get | As for a get, and the **version id**. |
| Get (free) (`--appstore-claim`, which runs `ipatool purchase -b <bundle id>`) | When you press Get (free) on an app's pane and confirm. Only for an app the iTunes lookup says is free; ipatool refuses a paid one too. Graft then starts the get. | As for a get | As for a get. This adds the app to your Apple ID's purchases, at no cost. |
| `updates.cdn-apple.com` (a macOS restore image, read in parts) and `wkms-public.apple.com` (the key for its encrypted parts) | The first App Store action on a Mac, while `~/Library/Caches/ipatool` lacks the Store framework the emulator runs: about 320 MB, kept there afterward. The image's address is fixed in ipatool's source (`internal/sap/assets/assets.go`), for macOS 27.0.1 (26A434). | as named | The request only |
| `files.pythonhosted.org` (Unicorn 2.1.4's macOS wheel) | The first App Store action on a Mac, while `~/Library/Caches/ipatool/unicorn/` lacks the library the emulator runs on; again only for a new Unicorn version | as named | The request only, with `User-Agent: ipatool Unicorn runtime downloader` |

About the first-use downloads:
- Unicorn isn't in Graft.app. The bundled ipatool downloads it from PyPI as upstream ipatool does; Graft's one patch to ipatool doesn't touch that. It checks the wheel's and the library's SHA-256 against the ones its source pins, and the library's again before each load. As with upstream ipatool, other code running as you could swap the cached library between that last check and the load. PyPI sees your IP address and ipatool's User-Agent, nothing else.
- Offline, that first action fails with "ipatool needs to download its App Store emulator (Unicorn, from PyPI) the first time; connect to the internet and try again."
- ipatool checks every file it takes from the restore image against a size and SHA-256 its source pins, so only that one image will do. If Apple stops serving it, that first action fails with "Apple no longer serves the macOS 27.0.1 (26A434) restore image that ipatool 2.8.0 takes them from"; a newer Graft, whose ipatool uses an image Apple still serves, fixes it.
- A first sign-in can take several minutes while these download; the sign-in sheet says so.

What stays on the Mac:
- `--appstore-status` (Settings › App Store, and Add App › Search once you pick a result it could offer to get; never when the sheet opens, and never while you type) runs `ipatool auth info`. From ipatool's source, that only reads its keychain item; it doesn't contact Apple.
- Sign Out (`--appstore-logout`, `ipatool auth revoke`) only deletes keychain items.
- Graft asks `security` whether ipatool's keychain items exist, and deletes some of them, since reading another program's is a Keychain dialog. With Graft.app's own ipatool, it deletes the ones Graft's record doesn't show it made: another ipatool's (one you use in Terminal). With any other ipatool, it deletes only the ones Graft's record shows Graft.app's own made; a sign-in a third ipatool made is saved over instead, and macOS may ask for your login keychain password while it is. Before a sign-in, that is any of its three items. Before every Get and Get (free), whichever ipatool runs, it is the download cache, `kbsync-v1`, which ipatool fills again. That's local too.

## Your iPhone or iPad

Over USB or the local network, never the internet.

| What | Contacted by | When | What's sent |
|---|---|---|---|
| Your iPhone or iPad, through macOS's usbmuxd | CLI: device features (pymobiledevice3) | Whenever a device feature runs: listing, installing, pushing into LiveContainer, reading apps and icons. `--update --plan --quick` asks usbmuxd which devices are connected and nothing more. | Commands and files for the device |
| A jailbroken device, over SSH and frida | CLI: `--decrypt`, `--decrypt-frontmost`, `--decrypt-check` (SSH through a local `iproxy` port forward, frida over USB) | When you dump an app, or the Decrypt card checks the device | Commands to the device, and the **device's SSH password**, sent only to the device |

## The bundled tools

- **ipatool 2.8.0:** as above. It is built from its pinned source with one patch, to how it reads an older version's `Info.plist` (above), and its only entitlements are `allow-jit` and `disable-library-validation`, which its emulator needs.
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
- Graft's latest release on GitHub (`github.com/GreenglassT/Graft/releases/latest`), from the button on the screen `Graft.zip` shows on an Intel Mac, and from the banner `Graft-Intel.zip` shows on an Apple silicon Mac;
- Graft's releases page on GitHub (`github.com/GreenglassT/Graft/releases`), from the link under the license notice in Graft › About Graft;
- Graft's page on GitHub (`github.com/GreenglassT/Graft`), from Help › Star Graft on GitHub, the same button in Settings › Version, or the same link in Graft › About Graft;
- Graft's Ko-fi page (`ko-fi.com/greenglasst`), from Help › Support Graft on Ko-fi, the same button in Settings › Version, or the same link in Graft › About Graft;
- the Graft Discord's invite (`discord.gg/dnJTnu3X7X`), from Help › Join the Graft Discord, the same button in Settings › Version, or the same link in Graft › About Graft;
- a newer Graft's release page on GitHub, from Release Page on the main window's update banner, or Open Release Page after Help › Check for Updates…;
- a page a link in Help › What's New… points to: Graft's releases, or a page of its documentation, on `github.com`;
- the new Graft, once Restart to Update has installed it: the update's helper opens it (`open`), as Finder would;
- the guides the free-account setup's device steps link, each from its Open button (on the first-run screen, and in Set Up LiveContainer… on a free Apple ID): `sidestore.io`, iloader on `github.com`, and LiveContainer's installation guide on `livecontainer.github.io`;
- PlayCover's releases page on GitHub (`github.com/PlayCover/PlayCover/releases`), from Get PlayCover in an app's Build options, which shows only when PlayCover isn't installed;
- an app's `apps.apple.com` page, from Open in App Store on an app's pane, or the Mac App Store when Graft has no page for the app;
- the Mac App Store, from Open the Mac App Store in Set Up Decryption.

Help › Licenses opens a folder inside the app. Other URLs in Graft, such as install hints, only appear as text you can copy. Graft never fetches any of these pages.

## Building a release

`scripts/graft-release` and the scripts it runs download all of this. None of it happens on a user's Mac.

| Host | By | What for |
|---|---|---|
| `github.com`, redirecting to GitHub's download hosts, and `raw.githubusercontent.com` | `scripts/graft_bundle.py` | Every GitHub artifact in `bundle/pins.toml`, each checked against its SHA-256: python-build-standalone (arm64, and x86_64 for the Intel bundle) and its source (for license texts), zsign (the arm64 release, and its v1.1.3 source, which the Intel bundle builds), the cyan archive, the sources of ipatool, UnfairPlay, OpenSSL, libplist, ldid, cctools-port and insert_dylib, and CPython's HACL* file (for its license). And, with `git fetch --depth=1`, UnfairPlay's v0.4.0 tag, for the commit its build info records (git runs with no configuration and no credential helper) |
| `go.dev` | `scripts/graft_bundle.py` | The Go 1.26.5 toolchain ipatool and UnfairPlay are built with, checked against its SHA-256 |
| `proxy.golang.org`, `sum.golang.org` | Go, in `graft_bundle.py` | ipatool's and UnfairPlay's Go modules (`go mod vendor`), each checked against its go.sum and the checksum database |
| `pypi.org`, `files.pythonhosted.org` | pip, in `graft_bundle.py`; pip-compile, in `scripts/lock-bundle`; `graft_bundle.py sources` | The packages in `bundle/requirements.lock` (`bundle/requirements-x86_64.lock` for the Intel bundle) and `bundle/build-requirements.lock`: wheels only, `--require-hashes`, chosen for macOS 14 on arm64 (x86_64 for the Intel bundle), each downloaded once per Mac into pip's cache under `~/Library/Caches/graft-release` and checked against the lock's hashes on every use. graft and cyan install with `--no-index`. And, for the source archive and the Rust crates' licenses, each package's JSON at `pypi.org/pypi/<name>/<version>/json` and its source distribution, checked against the lock. Unicorn isn't fetched: the app doesn't carry it. |
| `static.crates.io` | `graft_bundle.py` | For its license, each Rust crate in pydantic_core's, qh3's and cryptography's SBOMs and in bcrypt's Cargo.lock, checked against its Cargo.lock checksum, and each one ipatool's 1Password SDK core shows it holds, checked against `bundle/ipatool-core-crates.lock`; and for the source archive, the one of those under MPL-2.0 only (nom-supreme) |
| `github.com` | xcodebuild (Swift Package Manager) | The app's package dependencies, when they aren't already cached |
| `timestamp.apple.com` | `codesign --timestamp` | A secure timestamp for each signed file |

Notarization (`xcrun notarytool`) is a separate, manual step, and it uploads the zip to Apple.
