#!/bin/sh
# Build the Preferences.framework stub that lets a cyan-injected IPA launch
# under PlayCover on macOS. See README.md for why this exists.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
out=${1:-"$here/Preferences.framework"}
rm -rf "$out" && mkdir -p "$out"
xcrun -sdk iphoneos clang -arch arm64 -dynamiclib \
  -install_name @rpath/Preferences.framework/Preferences \
  -framework UIKit -framework Foundation -mios-version-min=11.0 \
  -o "$out/Preferences" "$here/stub.m"
cp "$here/Info.plist" "$out/Info.plist"
codesign -f -s - "$out"
echo "built $out"
