# What Graft Downloads

When Graft goes online and what it downloads, including the one-time downloads on the first App Store action.

Graft has no telemetry, analytics or crash reporting. Graft.app makes no
connections of its own: its CLI does, for something you asked for or a pane
you opened, to look up the icon of an app that has none yet, at launch and
when the app list changes, once a day to ask GitHub whether a newer Graft is
out, and to download that newer Graft (Settings › Updates turns either off).
Device UDIDs never leave your Mac.

| When | Who it contacts | What for |
|---|---|---|
| You search the App Store, add an app, or open an app's pane; for an app with no icon yet, at launch, when the app list changes, and after a build or App Store get | Apple's iTunes lookup and search, and its artwork CDN | The app's version, price and icon |
| You search, fetch or update tweaks | canister.me, and the repos you add | Tweak listings and downloads |
| A device's pane opens, Edit App opens for a LiveContainer app, or you install LiveContainer | GitHub | LiveContainer's releases |
| Once a day while Graft is open, unless Settings › Updates turns it off, and when you choose Help › Check for Updates… | GitHub (`api.github.com`), with nothing that identifies you or your Mac | Whether a newer Graft is out |
| A newer Graft is out, unless Settings › Updates turns automatic updates off (then when you choose Update) | GitHub's release files (`github.com`), with nothing that identifies you or your Mac | The new `Graft.zip`, about 150 MB, once |
| A paid account with an App Store Connect key makes a profile | Apple's App Store Connect API | Provisioning profiles |
| You sign in, get an app, or get a free one | Apple's App Store, through the bundled ipatool | Your sign-in, the license, the IPA |
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
