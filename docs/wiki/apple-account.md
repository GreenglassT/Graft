# Your Apple Account

Graft works with a free Apple ID or a paid Apple Developer account, and
Settings can switch between the two later.

The first time it opens, Graft asks "Do you have a paid Apple Developer
account?"

## Free Apple ID

**No, I have a free Apple ID.** That's all on the Mac. Graft lists the steps
to do on the iPhone or iPad, with your free Apple ID:

1. Install [SideStore](https://sidestore.io), or use
   [iloader](https://github.com/nab138/iloader).
2. Install LiveContainer
   ([guide](https://livecontainer.github.io/docs/installation/lc_sidestore)).
   The LiveContainer+SideStore build uses one app slot for both.
3. In LiveContainer, open Settings and tap Import Certificate from SideStore, so
   it can sign the apps you import. iloader doesn't do this for you, and on
   iOS 26 every app fails its first launch with "JITLess mode is required since
   iOS 26" until it's done.

Graft finds LiveContainer on the device by its bundle id, so there is no team
id to set up.

## Paid Apple Developer account

**Yes.**

1. Choose your `.p12`.
2. Type its password (leave it empty if you set none).
3. Choose your provisioning profile.

Graft shows the team and the devices the profile covers before it sets
anything up. It keeps the password in your login keychain, never in a file,
and copies both files into its config folder.

## Switching later

The Account row in Settings switches between the two, with **Add a paid
account…** or **Switch to a free Apple ID…**, and keeps your apps, repos and
settings either way.

With a paid account, **Replace certificate and profile…** swaps in a new pair
when the old one expires, which happens every year.
