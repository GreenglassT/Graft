# Use Graft from an AI Agent

Graft is fully scriptable: every action in the app is a `graft` command, and
almost every command can answer in JSON. So an AI coding agent that runs
Terminal commands on your Mac, such as Claude Code, can drive Graft for you
from a request in plain English.

## What it can do

An agent can take the same steps you would, in order:

- search the App Store and add an app;
- get the app's IPA from the App Store, the latest version or an older one,
  or stage an IPA you give it;
- search Canister or your repos for tweaks, download them and attach them;
- build the app and deliver it to your iPhone or iPad, or export its IPA
  unsigned, for an on-device signer;
- update your apps: a newer App Store version and newer tweaks, then the
  build and the install;
- check what's out of date, what was last delivered, and why a build failed.

It reads Graft's results and its hints as it goes, so it can tell you what
went wrong and what to do next. `graft --describe` gives it the full list of
commands, flags and events.

## Set it up

1. Set up Graft.app first: its first run, and for App Store downloads,
   [Sign in](app-store.md#sign-in) and
   [Set up decryption](app-store.md#set-up-decryption).
2. In Graft.app, open Settings › Command-line tool and press **Install graft
   for Terminal** (see [Installing the CLI](cli-install.md#install-graft-for-terminal)).
   In a new Terminal window, `graft --version` prints Graft's version.
3. Make a folder for the agent to work in, and save
   [AGENTS.md](../../AGENTS.md) from Graft's repository into it. It tells an
   agent how to call `graft`, how to read its answers, and what to leave to
   you. Some agents read a file with that name by themselves; with others,
   start by asking the agent to read it.
4. Open your agent in that folder.

The agent runs `graft` as you, with your config, apps and settings, just as
if you typed the commands yourself.

## Example requests

- "Add Example App from the App Store, attach the tweak com.example.sometweak
  from Canister, and install it to my iPhone through LiveContainer."
- "Search Canister for tweaks for Example App, show me what you find, and
  attach the one I pick."
- "Check whether any of my apps has a newer version on the App Store, or any
  of my tweaks a newer release, tell me what you'd update, and update the ones
  I pick."
- "My last build of Example App failed. Find out why and tell me what to do."
- "The new version of Example App broke my tweak. Get the version before it
  from the App Store, and rebuild it once I say yes."

## What it does, and what it leaves to you

AGENTS.md lets an agent search, list and check things, add an app, download
an app your Apple ID already has, fetch and attach tweaks, and preview a
build with `--dry-run`, without asking.

It exports an IPA only when you ask, saves it only where you say, and never
sends the file anywhere. It asks before it replaces a file that's already
there.

It tells the agent to ask you first before:

- anything that changes your iPhone or iPad: a build that delivers to it, an
  update, installing or cleaning up LiveContainer, or a re-install;
- getting a free app your Apple ID doesn't have yet. Graft never buys
  anything: the agent asks before it adds a free app to your Apple ID's
  purchases, and a paid app is yours to buy in the App Store;
- getting the latest version of an app that's held at an older one, or at a
  preferred version you set in Edit App: that lets go of the hold (see
  [Get an older version](app-store.md#get-an-older-version));
- updating a tweak from a source Graft can't verify (it doesn't know where
  the tweak came from, the source doesn't use HTTPS, or it lists no
  checksum);
- removing anything: an app, a tweak, a staged IPA or old builds.

And it tells the agent never to:

- sign in to the App Store for you, or ask for your Apple ID password or the
  code Apple sends. You sign in in Graft.app;
- set up decryption, which needs your administrator password in macOS's own
  dialog. You press **Set Up Decryption…** yourself;
- ask for or type any other password: your Mac's, your `.p12`'s or your
  device's SSH password;
- answer a keychain or password dialog for you. If one appears while the
  agent works, it says which command caused it.
- install a Graft update, or remove Graft's data, unless you ask for that
  yourself. Graft.app updates itself.

Before an update (`graft --update`), the agent tells you what will change for
each app and each device it installs on: every device that app was installed
on, and no other. A device that isn't connected waits. The agent asks before
`--drop-update-target`, which stops updating an app on a device that's gone.
An update never buys or claims an app, and never lets go of a hold.

An agent can still get things wrong. Read what it's about to do to your
device before you say yes.

LiveContainer delivery puts the IPA in LiveContainer's Documents on your
device. Opening LiveContainer and importing it is still your step, as it is
when you build from the app.

On iOS 16 and later, a Sideload build signed with a development profile stops
when Developer Mode is off on the device. Turning it on is yours to do, on the
device (see [Sideload](building-and-delivery.md#sideload-paid-only)).

Graft is for personal use, whoever runs it: your own Apple ID, apps you own
or get for free, on your own devices. Don't redistribute decrypted or
modified IPAs.

## Privacy

The agent runs on your Mac and sees what Graft prints: your apps and tweaks,
your devices' names and UDIDs, file paths in your home folder, and, from
`graft --appstore-status`, the email of the Apple ID you signed in with. Your
passwords stay in your login keychain and aren't part of that output.

If the agent's AI model runs online, what the agent reads is sent to that
service, and that service's terms apply to it.

See [Scripting & Environment](cli-scripting.md) for `--json`, and the
[CLI Reference](cli-reference.md) for every command.
