# Troubleshooting

Common symptoms and error messages, and their likely cause.

| Symptom | Likely cause |
|---|---|
| `ApplicationVerificationFailed` | The `.p12` and profile are from different teams, or the device's UDID isn't in the profile. |
| `MismatchedApplicationIdentifierEntitlement` | The profile doesn't allow this bundle id; use a wildcard profile. Installing LiveContainer: delete an old zsign-signed copy on the device first. |
| `DeviceLocked` | Unlock the device. |
| `Developer Mode is off on <device>` | The device runs iOS 16 or later with Developer Mode off, and the app is signed with a development profile, so it wouldn't open. Turn it on in Settings › Privacy & Security › Developer Mode (a real build has made the switch appear), then confirm after the device restarts. |
| An app, such as an emulator, says it needs JIT | Graft doesn't turn on JIT. [StikDebug](https://github.com/StikDebug/StikDebug), an app on the iPhone, does. On iOS 26 and recent iPhones, only apps built to support it can get JIT, whatever tool you use. |
| `wrong password` from zsign | The `.p12` password is wrong. |
| `Library not loaded: @rpath/libexample.dylib` | Add a copy of that library to the app's tweaks; see [Shared libraries](tweaks.md#shared-libraries). |
| `couldn't extract some-tweak.deb` | The download is probably an HTML error page; fetch it again. |
| A guest app in LiveContainer doesn't remember its login | LiveContainer was signed with a wildcard profile's entitlements (for example by zsign). Reinstall it from Graft with a paid account. |
| A dump from a jailbroken device times out | Check the device's Decrypt card. Usually the frida majors differ, SSH to the device is down, or the app couldn't be brought to the foreground. |
| `no LiveContainer found on the device` | LiveContainer isn't installed, or not under a bundle id starting `com.kdt.livecontainer`. With a free Apple ID, install it through SideStore or iloader; see [Set up your Apple account](apple-account.md#free-apple-id). With a paid account, Graft can install it too. |
| `the device listed no apps at all, which is how a locked device answers` | The iPhone or iPad is locked, or hasn't been unlocked since it restarted. Unlock it, stay on the home screen, and try again. |
| `... already has a Documents/<app>.ipa that Graft did not push` | LiveContainer's Documents already holds a file with the name the push uses, and neither a record nor its bytes say Graft put it there (it may be your own copy). Rename or remove it in the Files app (On My iPhone or On My iPad > LiveContainer), then build again. |
| `lc_bundle_id ... is not installed` | The app is pinned to a LiveContainer that isn't on the device. Pick one in the LiveContainer picker in the app's Edit sheet, or Automatic to drop the pin. |
| Settings › Preflight, or `graft --check`, says the device is "not connected" | No iPhone or iPad is connected. That's information, not a failure: a check installs nothing, so it needs no device. A build that installs still needs one: plug it in with a USB cable and trust this Mac. |
| `couldn't reach usbmuxd` | macOS's service for talking to iPhones and iPads didn't answer. Reconnect the iPhone or iPad; if it persists, restart the Mac. |
| `several devices are connected: pick one` | More than one iPhone or iPad is connected and none is the default device. Pick one with Change on the app's bar (it's for the next Build & Install only), pass `--device <UDID>` in Terminal, or set a default in Settings. |
| `... isn't registered in your developer account` | The device isn't one of your Apple Developer account's devices, so no profile can include it. Add it at developer.apple.com (Certificates, Identifiers & Profiles › Devices), then build again. Graft never registers a device for you. |
| `N LiveContainers are installed and Graft will not guess which to use` | There is no single primary: two LiveContainers installed by two Apple IDs, say, or a jailbreak's plain `com.kdt.livecontainer` next to an installer's `com.kdt.livecontainer.<TEAMID>`. Pin the app to one of the ids the error lists, in the LiveContainer picker in its Edit sheet. |
| `... needs a paid Apple Developer Program membership` | The config has no `signing:` section, so it is set up for a free Apple ID, and the action needs a paid one. Use LiveContainer delivery, or, with a paid membership, Settings › Add a paid account…; it keeps your apps. |
| Settings › App Store says "signed in by another ipatool (Graft 1's?): sign in again", or, with your own ipatool set in `config.yaml`, "a sign-in this ipatool can't read (another ipatool's?): sign in again" | Your sign-in was made by an ipatool this one can't read: with Graft's own, one from another version of Graft; with your own, Graft.app's or another. Press Sign In…. |
| A decrypt fails as the wrong model or the wrong Apple ID | Decryption was set up with another Apple ID. Use the same Apple ID in Graft as in the Mac App Store, then press Set Up Again… in Settings › App Store. |
| Set Up Decryption… or Set Up Again… says this Mac's App Store state isn't there, or stops with "canonical input has no SIDB state" | macOS sometimes clears the state UnfairPlay reads, even for an app you've already opened. Open the app you picked once (the sheet's Open <app> button does it), then set up again. In Terminal, the setup script exits 69 for this. |
| The first sign-in, or the first App Store action, fails with "Apple no longer serves the macOS 27.0.1 (26A434) restore image that ipatool 2.8.0 takes them from" | A known issue since 2.0. The bundled ipatool takes the App Store's frameworks from one macOS restore image, at a fixed Apple address, and checks every file it takes against its source's pins, so no other image can stand in. If Apple removes that file, a Mac that hasn't downloaded them yet can't get them; Apple's server then answers "403 Forbidden" (or "404 Not Found"), which the error's details show. A Mac whose ipatool already did (in `~/Library/Caches/ipatool`) isn't affected. Graft's update notice, or Help › Check for Updates…, says whether a later Graft is out (or look on [GitHub Releases](https://github.com/GreenglassT/Graft/releases)); a later Graft's ipatool will use an image Apple still serves. |
| Settings › App Store says "couldn't tell… your login keychain is locked" | Unlock the login keychain in Keychain Access, then refresh. Until then Settings offers neither Sign In… nor Sign Out, Get latest from App Store and Get (free) refuse, and so do `graft --appstore-login` and `--appstore-logout` in Terminal. |
| "Graft.app is damaged: part of it is missing." | A file inside the app is missing. Download Graft again and replace this copy. |

Still stuck? Ask in the [Graft Discord](https://discord.gg/dnJTnu3X7X), or open a
[GitHub issue](https://github.com/GreenglassT/Graft/issues) with the error and
what you were doing.
