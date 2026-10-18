#!/bin/sh
# Builds the release assets into dist/: the one-file `altalk` executable, a .deb (Ubuntu 22.04+,
# Debian 12+), an .rpm (Fedora, RHEL 9), an Arch package, the Windows installer and portable .exe,
# plus SHA256SUMS. Linux builds in a Debian 11 container (see Dockerfile), Windows under Wine
# (Dockerfile.windows, build-windows.sh); the host only needs Docker.
set -eu
umask 022

VERSION=0.2.0
MAINTAINER="Andrei Bogdanov <hello@abogdanov.dev>"
DESCRIPTION="Push-to-talk voice-to-text via OpenAI API"
root=$(cd "$(dirname "$0")/.." && pwd)

if [ "${1:-}" != "--in-container" ]; then
    docker build -q -t altalk-build "$root/packaging" >/dev/null
    docker build -q -t altalk-build-windows -f "$root/packaging/Dockerfile.windows" "$root/packaging" >/dev/null
    mkdir -p "$root/build/pip-cache" "$root/build/pip-cache-windows"
    docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
        -v "$root:/src" -v "$root/build/pip-cache:/tmp/.cache/pip" \
        altalk-build sh /src/packaging/build.sh --in-container
    docker run --rm -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
        -e PIP_CACHE_DIR='Z:\cache' -e PYTHONDONTWRITEBYTECODE=1 \
        -v "$root:/src" -v "$root/build/pip-cache-windows:/cache" \
        altalk-build-windows sh /src/packaging/build-windows.sh "$VERSION"

    # Release assets: packages, the portable executables and their checksums.
    cd "$root/dist"
    cp altalk altalk-linux-x86_64
    sha256sum "altalk_${VERSION}_amd64.deb" "altalk-$VERSION-1.x86_64.rpm" \
        "altalk-$VERSION-1-x86_64.pkg.tar.zst" altalk-linux-x86_64 \
        "altalk-$VERSION-setup-x64.exe" altalk-windows-x64.exe > SHA256SUMS
    exit
fi

src=/src
build=/tmp/build
dist="$src/dist"
mkdir -p "$build" "$dist"

python3 -m venv /tmp/venv
/tmp/venv/bin/pip install -q -r "$src/requirements.txt" pyinstaller
ALTALK_PORTAUDIO=$(ldconfig -p | awk '/libportaudio\.so\.2 /{print $NF; exit}') \
    /tmp/venv/bin/pyinstaller --noconfirm --clean --log-level WARN \
    --distpath "$dist" --workpath "$build/pyinstaller" "$src/packaging/altalk.spec"

# Files shared by all packages.
stage="$build/root"
install -Dm755 "$dist/altalk" "$stage/usr/bin/altalk"
install -Dm644 "$src/assets/altalk-256.png" "$stage/usr/share/icons/hicolor/256x256/apps/altalk.png"
install -Dm644 "$src/LICENSE" "$stage/usr/share/licenses/altalk/LICENSE"
mkdir -p "$stage/usr/share/applications"
/tmp/venv/bin/python -c "import sys; sys.path.insert(0, sys.argv[1]); import linux_desktop; print(linux_desktop.DESKTOP_ENTRY.format(exec='altalk'), end='')" \
    "$src" > "$stage/usr/share/applications/altalk.desktop"

# .deb
deb="$build/deb"
cp -a "$stage" "$deb"
# Debian keeps the license as /usr/share/doc/<package>/copyright instead.
rm -r "$deb/usr/share/licenses"
install -Dm644 "$src/LICENSE" "$deb/usr/share/doc/altalk/copyright"
mkdir -p "$deb/DEBIAN"
cat > "$deb/DEBIAN/control" <<EOF
Package: altalk
Version: $VERSION
Architecture: amd64
Maintainer: $MAINTAINER
Installed-Size: $(du -sk "$deb/usr" | cut -f1)
Depends: libc6 (>= 2.31), libasound2 | libasound2t64, python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1
Recommends: ffmpeg
Suggests: gnome-shell-extension-appindicator
Section: utils
Priority: optional
Homepage: https://github.com/Santttal/altalk
Description: $DESCRIPTION
 Hold Left Alt, speak, release: the transcript lands in the clipboard.
 The tray menu keeps the last five transcripts one click away.
EOF
dpkg-deb --build --root-owner-group "$deb" "$dist/altalk_${VERSION}_amd64.deb" >/dev/null

# .rpm — no stripping or debuginfo: either would break the archive appended to the executable.
cat > "$build/altalk.rpmspec" <<EOF
%global debug_package %{nil}
%global __os_install_post %{nil}
%global _build_id_links none

Name:        altalk
Version:     $VERSION
Release:     1
Summary:     $DESCRIPTION
License:     MIT
URL:         https://github.com/Santttal/altalk
Packager:    $MAINTAINER
BuildArch:   x86_64
AutoReqProv: no
Requires:    glibc >= 2.31
Requires:    alsa-lib
Recommends:  python3-gobject
Recommends:  gobject-introspection
Recommends:  gtk4
Recommends:  libadwaita
Recommends:  (gnome-shell-extension-appindicator if gnome-shell)

%description
Hold Left Alt, speak, release: the transcript lands in the clipboard.
The tray menu keeps the last five transcripts one click away.

%install
cp -a $stage/. %{buildroot}/

%files
/usr/bin/altalk
/usr/share/applications/altalk.desktop
/usr/share/icons/hicolor/256x256/apps/altalk.png
%license /usr/share/licenses/altalk/LICENSE
EOF
rpmbuild -bb --quiet --define "_topdir $build/rpm" "$build/altalk.rpmspec"
cp "$build/rpm/RPMS/x86_64/altalk-$VERSION-1.x86_64.rpm" "$dist/"

# Arch package (.pkg.tar.zst), laid out the way makepkg does it.
pkg="$build/arch"
cp -a "$stage" "$pkg"
cat > "$pkg/.PKGINFO" <<EOF
pkgname = altalk
pkgbase = altalk
pkgver = $VERSION-1
pkgdesc = $DESCRIPTION
url = https://github.com/Santttal/altalk
builddate = $(date +%s)
packager = $MAINTAINER
size = $(du -sb "$pkg/usr" | cut -f1)
arch = x86_64
license = MIT
depend = glibc
depend = alsa-lib
depend = python-gobject
depend = gtk4
depend = libadwaita
optdepend = ffmpeg: transcribe audio files dropped into the inbox folder
optdepend = gnome-shell-extension-appindicator: tray icon on GNOME
EOF
owner="--uid 0 --gid 0 --uname root --gname root"
(
    cd "$pkg"
    LANG=C bsdtar -czf .MTREE --format=mtree $owner \
        --options '!all,use-set,type,uid,gid,mode,time,size,md5,sha256,link' .PKGINFO usr
    LANG=C bsdtar -cf - $owner .MTREE .PKGINFO usr | zstd -q -19 -T0 > "$dist/altalk-$VERSION-1-x86_64.pkg.tar.zst"
)
