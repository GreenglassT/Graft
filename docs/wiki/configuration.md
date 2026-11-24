# Configuration & Files

Graft stores its settings and each app's options in `config.yaml`, and its
other files in folders of its own.

Settings holds what most people change: the default device, how many archives
to keep, the signing files on a paid account, the App Store Connect key, the
App Store sign-in, and the paths Graft uses. Each app's Edit sheet holds its
options. Everything is stored in `~/.config/graft/config.yaml`
(`$XDG_CONFIG_HOME/graft` if set), which you can also edit by hand. A
configured app looks like this:

```yaml
signing:                                # paid only; leave it out for a free Apple ID
  cert: ~/.config/graft/AppleDevelopment.p12
  cert_password_env: GRAFT_P12_PASS     # when set, used instead of the Keychain's
  profile: ~/.config/graft/wildcard.mobileprovision
  team_id: ABCDE12345

cache_dir: ~/.cache/graft/ipas
tweaks_dir: ~/.config/graft/tweaks
archive_keep: 2                         # archived builds kept per app version
# default_device_udid: <UDID>

apps:
  example:
    bundle_id: com.example.app
    preferred_version: latest
    delivery: livecontainer             # or sideload
    store_country: "GB"                 # the App Store it's looked up in
    # lc_bundle_id: com.kdt.LiveContainer2.ABCDE12345   # pin a LiveContainer
    tweaks:
      - some-tweak.deb                  # files in tweaks_dir
    cyan:
      display_name: Example
      keep_bundle_id: true              # some apps tie sign-in to their original bundle id
      strip_extensions: false
      strip_supported_devices: true
      compression_level: 6
```

The app re-reads your config each time its window comes back to the front, so
a change made in an editor or in Terminal shows without a relaunch. If
`config.yaml` doesn't parse, Settings says so at the top, shows the default
paths and saves nothing until it's fixed. Fix it in an editor, and the notice
clears when you come back to the window.

**Free or paid.** A config with no `signing:` section (or an empty one) is a
free Apple ID. `signing:` keys that end up at the top level (lost
indentation) are an error naming them, not a free config.
- **Add a paid account…** asks only for the `.p12` and the profile, then adds
  a `signing:` section to the config and keeps its apps, repos and settings.
  With a paid account an app with no delivery mode is a sideload app, so Graft
  writes LiveContainer delivery into each such app and says which. Nothing
  moves until you change one.
- **Switch to a free Apple ID…** removes the `signing:` section and keeps
  everything else. Apps with no delivery mode then go into LiveContainer. One
  set to Sideload stays set, and won't build until you switch it to
  LiveContainer.
- **Replace certificate and profile…** swaps in a new pair, its Keychain
  password and the `signing:` section, keeps everything else, and puts the old
  ones back if any step fails. When the new pair belongs to another team, the
  old team's Keychain password is removed.
- The paid setup checks that the `.p12` opens with its password and that the
  profile lists its certificate and hasn't expired. It copies both files into
  the config folder (0600, never through a link).

**Bundle id.** Some apps tie sign-in to their original bundle id;
`keep_bundle_id: true` (the default) preserves it. With `keep_bundle_id: false`
cyan renames it (to `new_bundle_id`, or the original plus `.plus`) in both
delivery modes, LiveContainer included. Renaming the bundle id also removes any
Apple Watch app, which can't install under a new id.

**Per-app profiles (paid only).** "Create the provisioning profile
automatically" in an app's Edit sheet (`auto_profile: true`) has Graft create
and cache a profile for the app through the App Store Connect API;
`profile: <path>` points one app at a specific profile. Both apply to sideload
builds only, and the option is locked on a LiveContainer app, which uses no
profile.
- Make an API key at
  [App Store Connect](https://appstoreconnect.apple.com/access/integrations/api),
  then give it to Graft in Settings › App Store Connect. Graft checks the key
  by listing your devices, and only then copies the `.p8` beside
  `config.yaml`.
- Setting a key replaces only the key and its devices. A `profile_cache_dir`
  you set by hand stays, and `device_udids` is refreshed every time with every
  enabled iPhone, iPad and iPod touch App Store Connect lists. To keep a list
  you curated, add `device_udids_pinned: true` to the `app_store_connect:`
  section.

**The lock file.** Graft takes a lock on `config.yaml.lock` beside the config
while it edits it, so the app and the CLI never write over each other. If you
keep the config in a dotfiles repository and symlink it, add
`config.yaml.lock` to that repository's `.gitignore`.

## Files

```
~/.config/graft/
├── config.yaml
├── config.yaml.lock
├── AppleDevelopment.p12
├── wildcard.mobileprovision
├── AuthKey_<KEY_ID>.p8  # App Store Connect API key
├── profiles/            # profiles made through that API
└── tweaks/              # .deb and .dylib files

~/.cache/graft/
├── ipas/                # staged decrypted IPAs (never pruned)
├── built/               # archived builds (newest archive_keep per app version)
├── artwork/             # cached app icons
└── updates/             # a newer Graft, downloaded and checked, until it's installed

~/.local/state/graft/
├── log.sqlite           # build history and delivery records
├── build-logs/          # saved event log of each --json build (so every build from the app)
├── repos/               # cached APT repository indexes
└── tweak-rollback/      # previous versions kept when a tweak is updated

~/.local/share/graft/    # tools you install for Graft (frida-ios-dump; UnfairPlay, for a pipx install)
```

These four `graft` directories are owner-only (0700): Graft creates them that
way and tightens them on every run if they aren't. Staged IPAs, archived
builds, build logs and the `.p12` and profile the setup copies are written
0600. Graft changes nothing outside these directories, and leaves alone a
`graft` that is a symlink you made. The one exception is updating itself: it
replaces its own Graft.app, moves the old one to the Trash, and writes what it
did to `~/Library/Logs/Graft/update.log`.

Outside them, the App Store tools keep their own files: ipatool's sign-in in
your login keychain, its session cookies in `~/.ipatool` and its downloads in
`~/Library/Caches/ipatool`, and UnfairPlay's model in
`~/Library/Application Support/unfairplay/models`. [Removing
Graft](removing.md) lists everything.
