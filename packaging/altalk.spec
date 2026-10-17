# PyInstaller spec for the one-file `altalk` executable; run by packaging/build.sh.
import os

from PyInstaller.utils.hooks import collect_submodules

root = os.path.dirname(SPECPATH)

a = Analysis(
    [os.path.join(root, "icon2.py")],
    binaries=[(os.environ["ALTALK_PORTAUDIO"], ".")],
    datas=[
        (os.path.join(root, "assets", "altalk-tray.png"), "assets"),
        (os.path.join(root, "assets", "altalk-copied.png"), "assets"),
        (os.path.join(root, "assets", "altalk-256.png"), "assets"),
        (os.path.join(root, "settings_dialog.py"), "."),
    ],
    hiddenimports=collect_submodules("Xlib"),
    runtime_hooks=[os.path.join(SPECPATH, "pyi_rth_portaudio.py")],
    excludes=["tkinter"],
)
# ALSA has to come from the target system: its PipeWire/PulseAudio plugins live in
# distro-specific directories that a bundled libasound would not look in.
a.binaries = [entry for entry in a.binaries if not os.path.basename(entry[0]).startswith("libasound.so")]

pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="altalk", console=True, upx=False)
