#!/bin/sh
# Windows half of packaging/build.sh, run inside the altalk-build-windows container (Dockerfile.windows):
# the portable altalk-windows-x64.exe and the installer altalk-<version>-setup-x64.exe into dist/.
# Wine refuses a prefix owned by someone else, so this runs as root and hands its files back at the end.
set -eu
VERSION=$1
src=/src
build=/tmp/build
dist="$src/dist"
python='C:\Python\python.exe'
mkdir -p "$build/icons" "$dist"

# Windows wants .ico files with several sizes inside, so every display scale gets a sharp icon.
python3 - "$src/assets" "$build/icons" <<'EOF'
import sys
from PIL import Image

assets, out = sys.argv[1:]
tray_sizes = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64)]
for name in ("altalk-tray", "altalk-busy-1", "altalk-busy-2", "altalk-busy-3", "altalk-copied", "altalk-failed"):
    Image.open(f"{assets}/{name}.png").save(f"{out}/{name}.ico", sizes=tray_sizes)
Image.open(f"{assets}/altalk-256.png").save(f"{out}/altalk.ico", sizes=tray_sizes + [(128, 128), (256, 256)])
EOF

wine "$python" -m pip install -q --no-warn-script-location -r 'Z:\src\requirements.txt' pyinstaller
ALTALK_ICONS='Z:\tmp\build\icons' ALTALK_VERSION="$VERSION" wine "$python" -m PyInstaller \
    --noconfirm --clean --log-level WARN \
    --distpath 'Z:\tmp\build\dist' --workpath 'Z:\tmp\build\pyinstaller' 'Z:\src\packaging\altalk-windows.spec'
xvfb-run -a wine 'C:\InnoSetup\ISCC.exe' /Q \
    "/DAppVersion=$VERSION" '/DSourceDir=Z:\tmp\build\dist\altalk' \
    '/DIconFile=Z:\tmp\build\icons\altalk.ico' '/DOutputDir=Z:\tmp\build\dist' 'Z:\src\packaging\altalk.iss'
wineserver -w

cp "$build/dist/altalk-windows-x64.exe" "$build/dist/altalk-$VERSION-setup-x64.exe" "$dist/"
chown "$HOST_UID:$HOST_GID" "$dist" "$dist/altalk-windows-x64.exe" "$dist/altalk-$VERSION-setup-x64.exe"
chown -R "$HOST_UID:$HOST_GID" /cache
