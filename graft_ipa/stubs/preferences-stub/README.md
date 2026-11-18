# `Preferences.framework` stub

A minimal `Preferences.framework` stand-in for tweak settings libraries under
PlayCover. Extend the symbol list if a tweak needs more.

## The problem

Some tweaks link a settings library that in turn links
`/System/Library/PrivateFrameworks/Preferences.framework/Preferences`, an iOS
*private* framework. macOS has it in neither `/System/Library/PrivateFrameworks`
nor `/System/iOSSupport/...`, so dyld aborts the process at launch:

```
DYLD: Library missing
Library not loaded: /System/Library/PrivateFrameworks/Preferences.framework/Preferences
Referenced from: .../Example.app/Frameworks/libexample.dylib
```

Substrate is not the problem: cyan already ships ElleKit as
`CydiaSubstrate.framework` and injects it, so a cyan build carries its own.

## Why a stub can be enough

dyld only needs the symbols a library imports to exist. List what yours imports:

```sh
nm -u libexample.dylib | grep 'PS[A-Z]'
```

The stub in `stub.m` declares what the libraries seen so far import: the
`PSTableCell` class, its metaclass and its `_specifier` ivar. `PSTableCell` is a
`UITableViewCell` subclass that is only ever built for a Settings pane, which
does not exist off-device, so declaring it is enough for dyld to bind.

If a tweak imports more (another `PS*` class, a constant), add it to `stub.m`
and rebuild. The stub makes the library loadable; it does not make the settings
UI work.

## Use

`graft --for-playcover` applies the framework swap and the rpath for you (and
`--install-to-mac` applies the two PlayCover settings below). By hand:

```sh
./build.sh                       # produces Preferences.framework
# then, inside the IPA's Payload/<App>.app/Frameworks/:
install_name_tool -change \
  /System/Library/PrivateFrameworks/Preferences.framework/Preferences \
  @rpath/Preferences.framework/Preferences \
  libexample.dylib
# a tweak dylib injected by cyan has no rpath of its own; skip if it already has one
install_name_tool -add_rpath @loader_path libexample.dylib
codesign -f -s - libexample.dylib
cp -R Preferences.framework .
codesign -f -s - Preferences.framework
```

## Two PlayCover settings also have to change

Both are per-app, both default to the value that breaks it, and neither has
anything to do with the stub. `graft --install-to-mac` turns both off for you;
by hand:

```sh
S=~/Library/Containers/io.playcover.PlayCover/"App Settings"/<bundle-id>.plist
plutil -replace playChain  -bool false "$S"   # else it can crash on the first keychain write
plutil -replace keymapping -bool false "$S"   # else clicks may never land
```

- **`keymapping: false`** is what makes an ordinary app usable. With it on, the
  default, PlayCover translates the mouse into mapped touch events for
  game-style controls, so on an ordinary app clicks silently go nowhere: the app
  renders and looks hung.
- **`playChain: false`** avoids a crash inside PlayCover's own keychain shim
  (`PlayTools`, `PlayKeychain.add(_:result:)`) the first time the app writes to
  the keychain.

The settings file is rewritten on reinstall. Set both again after any
`open -b io.playcover.PlayCover <ipa>` (`--install-to-mac` does this each time).

## Caveats

Hooked is not the same as working: with the stub, the tweaks load and ElleKit
swizzles real classes inside a stock macOS process, but each tweak's behaviour
still has to be checked end to end. PlayCover itself can also crash
intermittently while installing an IPA; retry before concluding anything.
