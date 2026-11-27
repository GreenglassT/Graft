# What Graft Downloads

When Graft goes online and what it downloads, including the one-time downloads on the first App Store action.

Graft has no telemetry, analytics or crash reporting. Graft.app makes no
connections of its own: its CLI does, for something you asked for or a pane
you opened; to look up the icon of an app that has none yet; once a day to
ask GitHub whether a newer Graft is out, and to download it; and once a day to
check whether your apps and tweaks have updates. Settings › Updates turns
off the daily checks and the download. Device names and UDIDs never leave
your Mac.

| When | Who it contacts | What for |
|---|---|---|
| You search the App Store, add an app, open an app's pane, or plan or run an update; for an app with no icon yet, at launch, when the app list changes, and after a build or App Store get | Apple's iTunes lookup and search, and its artwork CDN | The app's version, price and icon |
| You search, fetch or update tweaks, or update an app | canister.me, and the repos you add | Tweak listings and downloads |
| A device's pane or its Set Up LiveContainer… sheet opens, Edit App opens for a LiveContainer app, or a LiveContainer install, certificate or clean-up on a device ends | GitHub | LiveContainer's releases |
| Once a day while Graft is open, unless Settings › Updates turns it off, and when you choose Help › Check for Updates… | GitHub (`api.github.com`), with nothing that identifies you or your Mac | Whether a newer Graft is out |
| At launch and once a day while Graft is open, and after a get, build, tweak update, update or change to an app's options or tweaks, or when a device connects or disconnects while an update waits for one (at most every 10 minutes), unless Settings › Updates › Check my apps for updates is off; and when you press Check for updates now | Apple's public iTunes lookup (one request per App Store country), and canister.me for attached tweaks that didn't come from one of your repos | Your apps' bundle IDs and your attached tweaks' package IDs, to see whether newer versions are out. No Apple ID. |
| A newer Graft is out, unless Settings › Updates turns automatic updates off (then when you choose Update) | GitHub's release files (`github.com`), with nothing that identifies you or your Mac | The new `Graft.zip` (`Graft-Intel.zip` for the Intel build), about 150 MB, once |
| A paid account with an App Store Connect key makes a profile, or a build signed with a profile Graft makes, or a LiveContainer install, targets a device missing from the saved list (Graft lists your registered devices; it never registers one) | Apple's App Store Connect API | Provisioning profiles |
| You sign in, get an app or an older version of one, list an app's older versions, or get a free one | Apple's App Store, through the bundled ipatool | Your sign-in, the license, the IPA, the versions' numbers and dates |
| The first App Store action on a Mac (usually the first sign-in) | PyPI (`files.pythonhosted.org`), and Apple's software CDN | ipatool's Unicorn emulator (a few MB) and its App Store components (about 320 MB), once |

About that first App Store action:
- Graft.app doesn't include Unicorn. The bundled ipatool downloads Unicorn
  2.1.4 from PyPI on first use, as upstream ipatool does, and checks it
  against the SHA-256 its source pins.
- Both downloads are cached in `~/Library/Caches/ipatool`, so they happen
  once, and again only for a new version.
- Offline, that first App Store action stops with "ipatool needs to download its App
  Store emulator (Unicorn, from PyPI) the first time; connect to the internet
  and try again."

UnfairPlay has no network code, and talking to your iPhone or iPad stays on
your Mac and your local network. [What Graft connects to](../network.md)
lists every host, when, and what is sent.
