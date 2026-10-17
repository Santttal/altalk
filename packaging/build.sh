#!/bin/sh
# Builds dist/altalk (one self-contained executable) and dist/altalk_<version>_amd64.deb.
set -eu
umask 022

VERSION=0.1.0
root=$(cd "$(dirname "$0")/.." && pwd)
build="$root/build"
dist="$root/dist"
venv="$build/venv"

[ -x "$venv/bin/python" ] || python3 -m venv "$venv"
"$venv/bin/pip" install -q -r "$root/requirements.txt" pyinstaller

portaudio=$(ldconfig -p | awk '/libportaudio\.so\.2 /{print $NF; exit}')
[ -n "$portaudio" ] || { echo "libportaudio2 is needed on the build machine" >&2; exit 1; }

"$venv/bin/pyinstaller" --noconfirm --clean --onefile --name altalk \
    --distpath "$dist" --workpath "$build/pyinstaller" --specpath "$build" \
    --add-data "$root/assets/altalk-tray.png:assets" \
    --add-data "$root/assets/altalk-copied.png:assets" \
    --add-data "$root/assets/altalk-256.png:assets" \
    --add-data "$root/settings_dialog.py:." \
    --exclude-module tkinter \
    --add-binary "$portaudio:." \
    --runtime-hook "$root/packaging/pyi_rth_portaudio.py" \
    --collect-submodules Xlib \
    "$root/icon2.py"

pkg="$build/deb/altalk_${VERSION}_amd64"
rm -rf "$pkg"
install -Dm755 "$dist/altalk" "$pkg/usr/bin/altalk"
install -Dm644 "$root/assets/altalk-256.png" "$pkg/usr/share/icons/hicolor/256x256/apps/altalk.png"
mkdir -p "$pkg/usr/share/applications" "$pkg/DEBIAN"
"$venv/bin/python" -c "import sys; sys.path.insert(0, sys.argv[1]); import icon2; print(icon2.DESKTOP_ENTRY.format(exec='altalk'), end='')" \
    "$root" > "$pkg/usr/share/applications/altalk.desktop"
cat > "$pkg/DEBIAN/control" <<EOF
Package: altalk
Version: $VERSION
Architecture: amd64
Maintainer: Andrei Bogdanov <Santttal@users.noreply.github.com>
Depends: libc6 (>= 2.39), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1
Recommends: ffmpeg
Section: utils
Priority: optional
Homepage: https://github.com/Santttal/altalk
Description: Push-to-talk voice-to-text via OpenAI API
 Hold Left Alt, speak, release: the transcript lands in the clipboard.
 The tray menu keeps the last five transcripts one click away.
EOF
dpkg-deb --build --root-owner-group "$pkg" "$dist/altalk_${VERSION}_amd64.deb"

# Release assets: the .deb, the portable executable and their checksums.
cp "$dist/altalk" "$dist/altalk-linux-x86_64"
(cd "$dist" && sha256sum "altalk_${VERSION}_amd64.deb" altalk-linux-x86_64 > SHA256SUMS)
