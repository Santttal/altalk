# PyInstaller spec for macOS, run on a Mac by packaging/build-macos.sh: Altalk.app, a menu bar app
# without a Dock icon, for the architecture of the Mac it is built on (NumPy has no universal2 wheels).
import os

root = os.path.dirname(SPECPATH)
version = os.environ["ALTALK_VERSION"]
icons = [
    "altalk-tray", "altalk-busy-1", "altalk-busy-2", "altalk-busy-3",
    "altalk-listening-1", "altalk-listening-2", "altalk-listening-3", "altalk-copied", "altalk-failed", "altalk-256",
]

a = Analysis(
    [os.path.join(root, "icon2.py")],
    datas=[(os.path.join(root, "assets", f"{name}.png"), "assets") for name in icons] + [
        # Texts of the microphone permission prompt in English and Russian.
        (os.path.join(SPECPATH, "macos", "en.lproj"), "en.lproj"),
        (os.path.join(SPECPATH, "macos", "ru.lproj"), "ru.lproj"),
    ],
    excludes=["linux_desktop", "windows_desktop", "tkinter"],
)

pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="altalk", console=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="altalk", upx=False)
app = BUNDLE(
    coll,
    name="Altalk.app",
    icon=os.environ["ALTALK_ICNS"],
    bundle_identifier="dev.abogdanov.altalk",
    version=version,
    info_plist={
        "CFBundleDisplayName": "Altalk",
        "CFBundleDevelopmentRegion": "en",
        "CFBundleLocalizations": ["en", "ru"],
        "LSUIElement": True,  # lives in the menu bar only
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription": "Altalk records your voice while you hold the hotkey and sends it to OpenAI for transcription.",
    },
)
