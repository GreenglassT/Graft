# Removing Graft

What to delete to remove Graft, and what it made outside the app.

Dragging Graft.app to the Trash removes the app and everything inside it. What
it made outside the app stays until you remove it:

| What | Where | To remove it |
|---|---|---|
| Your App Store sign-in | three items in your login keychain, service `ipatool-auth.service` | Before you remove the app, Settings › App Store › **Sign Out**. Then delete any of the three that's left in Keychain Access (search for `ipatool-auth.service`), or in Terminal: `security delete-generic-password -s ipatool-auth.service -a account`, and the same with `-a anisette-v4` and `-a kbsync-v1`, unless you use ipatool on its own: it keeps its sign-in in the same three items. |
| Your `.p12` and SSH passwords | login keychain items with the service `graft` (`p12-<team id>`, `ssh-<device UDID>`), or `tweak-build` from before Graft's rename | Keychain Access: search for `graft`, and delete them. |
| The decryption model | `~/Library/Application Support/unfairplay` | `rm -rf ~/Library/Application\ Support/unfairplay`, unless you use UnfairPlay on its own. |
| ipatool's App Store components and Unicorn | `~/Library/Caches/ipatool` (about 320 MB) | `rm -rf ~/Library/Caches/ipatool`, unless you use ipatool on its own. |
| Your config, `.p12`, profiles, App Store Connect key and tweaks | `~/.config/graft` | `rm -rf ~/.config/graft` |
| Staged IPAs and archived builds, which can take several GB | `~/.cache/graft` | `rm -rf ~/.cache/graft` |
| Build history and logs, repository caches, the App Store sign-in record | `~/.local/state/graft` | `rm -rf ~/.local/state/graft` |
| frida-ios-dump, and tools a pipx install uses | `~/.local/share/graft` | `rm -rf ~/.local/share/graft` |
| `graft` for Terminal | `~/.local/bin/graft`, a link into the app | `rm ~/.local/bin/graft`, if `ls -l ~/.local/bin/graft` shows it points into Graft.app (a pipx `graft` there is removed with `pipx uninstall graft-ipa`). |
| The app's own settings | its preferences, `com.greenglasst.graft` | `defaults delete com.greenglasst.graft` |

If you set `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `XDG_STATE_HOME` or
`XDG_DATA_HOME`, the four `graft` folders are under those instead. The Mac App
Store app you set up decryption with, and LiveContainer and the apps on your
devices, are yours to keep or delete.
