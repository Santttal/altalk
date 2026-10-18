#!/bin/sh
# macOS build, run on a Mac (GitHub Actions, see .github/workflows/build.yml): Altalk.app for this
# Mac's architecture in dist/altalk-<version>-macos-<arm64|x86_64>.dmg. The app is signed ad hoc only:
# a Developer ID needs a paid Apple account.
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
VERSION=$(sed -n 's/^VERSION=//p' "$root/packaging/build.sh")
arch=$(uname -m)
build="$root/build/macos"
dist="$root/dist"
rm -rf "$build"
mkdir -p "$build/Altalk.iconset" "$dist"

python3 -m venv "$build/venv"
"$build/venv/bin/pip" install -q -r "$root/requirements.txt" pyinstaller

# The app icon in every size macOS asks for.
for size in 16 32 128 256 512; do
    sips -z $size $size "$root/assets/icon.png" --out "$build/Altalk.iconset/icon_${size}x${size}.png" >/dev/null
    sips -z $((size * 2)) $((size * 2)) "$root/assets/icon.png" --out "$build/Altalk.iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$build/Altalk.iconset" -o "$build/Altalk.icns"

ALTALK_VERSION="$VERSION" ALTALK_ICNS="$build/Altalk.icns" "$build/venv/bin/python" -m PyInstaller \
    --noconfirm --clean --log-level WARN \
    --distpath "$build/dist" --workpath "$build/pyinstaller" "$root/packaging/altalk-macos.spec"
# One ad-hoc signature over the whole bundle: macOS ties permissions (microphone, Input Monitoring) to it.
codesign --force --deep --sign - "$build/dist/Altalk.app"
codesign --verify --deep --strict "$build/dist/Altalk.app"

# The disk image: Altalk.app next to a shortcut to Applications, to drag it there.
mkdir "$build/dmg"
ditto "$build/dist/Altalk.app" "$build/dmg/Altalk.app"
ln -s /Applications "$build/dmg/Applications"
hdiutil create -quiet -volname Altalk -srcfolder "$build/dmg" -format UDZO -ov "$dist/altalk-$VERSION-macos-$arch.dmg"
