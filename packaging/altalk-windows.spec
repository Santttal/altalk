# PyInstaller spec for Windows, run under Wine by packaging/build-windows.sh: a folder build
# for the installer (starts faster, nothing to unpack) and a one-file portable .exe.
import os

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

root = os.path.dirname(SPECPATH)
icons = os.environ["ALTALK_ICONS"]  # .ico files made by build-windows.sh
version = os.environ["ALTALK_VERSION"]
numbers = tuple(int(part) for part in version.split(".")) + (0,)

a = Analysis(
    [os.path.join(root, "icon2.py")],
    datas=[(os.path.join(icons, "*.ico"), "assets")],
    excludes=["linux_desktop"],
)

# Windows shows these as the program's name and publisher, e.g. above its notifications.
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "Andrei Bogdanov"),
            StringStruct("FileDescription", "Altalk"),
            StringStruct("FileVersion", version),
            StringStruct("InternalName", "altalk"),
            StringStruct("LegalCopyright", "Copyright (c) 2026 Andrei Bogdanov, MIT License"),
            StringStruct("OriginalFilename", "altalk.exe"),
            StringStruct("ProductName", "Altalk"),
            StringStruct("ProductVersion", version),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)
app_icon = os.path.join(icons, "altalk.ico")

pyz = PYZ(a.pure)
portable = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="altalk-windows-x64", console=False, icon=app_icon, version=version_info, upx=False,
)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="altalk", console=False, icon=app_icon, version=version_info, upx=False,
)
COLLECT(exe, a.binaries, a.datas, name="altalk", upx=False)
