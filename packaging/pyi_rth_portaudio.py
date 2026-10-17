# PyInstaller runtime hook: sounddevice looks PortAudio up only through the ldconfig cache,
# so point it at the copy that build.sh bundles next to the executable's other libraries.
import ctypes.util
import os
import sys

_find_library = ctypes.util.find_library


def _find_bundled_library(name):
    if name == "portaudio":
        return os.path.join(sys._MEIPASS, "libportaudio.so.2")
    return _find_library(name)


ctypes.util.find_library = _find_bundled_library
