# Updating Graft

Graft tells you when a newer version is out, and you install it by hand from
GitHub Releases.

## When a new version is out

Once a day while it's open, Graft asks GitHub whether a newer version is out.
If there is one, a banner at the bottom of the main window says so, and its
**Release Page** button opens that release on GitHub. Help › Check for
Updates… asks at any time.
- The check is one request to GitHub, with nothing that identifies you or
  your Mac. [What Graft connects to](../network.md) shows what it sends.
- It never downloads or installs anything. Graft doesn't update itself.
- Settings › Updates turns the daily check off. Help › Check for Updates…
  still asks when you choose it.
- In Terminal, `graft --check-update` asks the same question.

## Install it

Download the new `Graft.zip` from
[GitHub Releases](https://github.com/GreenglassT/Graft/releases) and replace
the `Graft.app` in `/Applications`.
- Your config, apps and builds carry over, and so does your App Store
  sign-in.
- The Terminal command points into the app, so it updates too.

## What's new

Help › What's New… shows what changed in each release. It also opens by
itself once, the first time a new version of Graft starts. The
[Changelog](../../CHANGELOG.md) has the same list.
