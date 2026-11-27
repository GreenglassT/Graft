# Quick Start

The commands for a first build from Terminal, once with a free Apple ID and
once with a paid Apple Developer account.

## With a free Apple ID

Do the device steps under
[Set up your Apple account](apple-account.md#free-apple-id) first.

```bash
graft --init                        # answer no to the paid-membership question
graft --check                       # tools, the device, and LiveContainer on it
graft --appstore-search "example"   # find it: name, developer, price, bundle id
graft --add-app https://apps.apple.com/app/id123456789   # delivery: livecontainer
graft --provide-ipa ~/Downloads/Example.ipa --app example
graft --attach-tweak some-tweak.deb --app example
graft example                       # build and push into LiveContainer
```

## With a paid Apple Developer account

```bash
graft --init                        # answer yes: asks for the .p12 and profile, copies
                                    # them into ~/.config/graft/ and writes config.yaml
graft --set-p12-password            # store the .p12 password in the Keychain
graft --check
graft --add-app https://apps.apple.com/app/id123456789 --delivery livecontainer
graft --list                        # shows the name it was added under, e.g. "example"
graft --provide-ipa ~/Downloads/Example.ipa --app example
graft --search "some tweak"
graft --fetch com.example.sometweak         # downloads into ~/.config/graft/tweaks/
graft --attach-tweak some-tweak.deb --app example
graft example
```

### If `--init` couldn't read the team id

If `--init` couldn't read the team id from the profile, it writes
`team_id: REPLACEME`. Set the real one with
`graft --set-settings --signing-team-id <TEAM>` before `--set-p12-password`:
the Keychain entry is keyed by team id, so `--set-p12-password` refuses until
it's set.

Instead of the Keychain, you can export the variable named by `signing.cert_password_env`. When that
variable is set and not empty, builds use it rather than the Keychain's
password, so unset it once you store a new password in the Keychain.

## Running --init without the questions

`graft --init --account free` and
`graft --init --account paid --p12 <file> --profile <file>` do what answering
the questions does, and are what the app runs.

- The paid one reads the `.p12` password from stdin (one line), or asks for it
  without echo in Terminal, and makes the same checks as the app.
- Neither touches a config that doesn't parse.
- On a paid config, `--account paid` refuses unless given `--replace` (the
  app's Replace certificate and profile…), and `--account free` switches it
  to a free Apple ID (the app's Switch to a free Apple ID…): it removes the
  `signing:` section and keeps the apps, repos and settings. On a free config,
  `--account paid` adds the paid account (Add a paid account…).
- `--dry-run` checks everything and writes nothing.
- The interactive `--init` copies the files it's given as they are, so an
  expired profile shows up in `--check`, and a certificate the profile doesn't
  list only when the device refuses the install.
- `--config <path>` works with any of these, for a config somewhere else. The
  app always uses the default one.
