"""
Windows-часть Altalk: клавиши — Raw Input, значок в трее и его всплывающие уведомления —
Shell_NotifyIcon, буфер обмена, окно настроек — Tk, автозапуск — ключ Run в реестре.
Всё Win32 — через ctypes, без pywin32.
"""

import ctypes
import gc
import os
import subprocess
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import winreg
from ctypes import wintypes
from tkinter import ttk

app = None  # the icon2 module; icon2.load_desktop() sets it before anything here runs

# Scan codes of Left Alt and Left Ctrl; right-hand keys come with the E0 prefix (see key_code).
HOTKEY_CODES = {"alt": 0x38, "ctrl": 0x1D}
HOTKEY_VIRTUAL_KEYS = {0x38: 0xA4, 0x1D: 0xA2}  # VK_LMENU, VK_LCONTROL
KEEP_MIC_OPEN = False
WINDOW_CLASS = "AltalkWindow"
MUTEX_NAME = "Local\\Altalk"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
AUTOSTART_VALUE = "Altalk"
# Unassigned virtual key; tapping it while Alt is down keeps Alt's release from opening the
# focused app's menu bar (the same trick as AutoHotkey's #MenuMaskKey).
MENU_MASK_KEY = 0xE8
MENU_MASK_TIMER = 1
KEY_WATCH_TIMER = 2
KEY_WATCH_MILLISECONDS = 250
CLIPBOARD_ATTEMPTS = 10
ERROR_COLOR = "#c42b1c"

WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_INPUT = 0x00FF
WM_TIMER = 0x0113
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_OPEN_SETTINGS = WM_APP + 2
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_USER, NIIF_NOSOUND, NIIF_LARGE_ICON = 0x04, 0x10, 0x20
RIDEV_REMOVE, RIDEV_INPUTSINK = 0x01, 0x100
RID_INPUT = 0x10000003
RIM_TYPEMOUSE, RIM_TYPEKEYBOARD = 0, 1
RI_KEY_BREAK, RI_KEY_E0, RI_KEY_E1 = 0x01, 0x02, 0x04
RI_MOUSE_BUTTON_DOWNS = 0x0001 | 0x0004 | 0x0010 | 0x0040 | 0x0100  # left, right, middle, 4, 5
RI_MOUSE_WHEELS = 0x0400 | 0x0800
USAGE_PAGE_GENERIC, USAGE_MOUSE, USAGE_KEYBOARD = 0x01, 0x02, 0x06
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
SM_CXICON, SM_CXSMICON = 11, 49
MF_STRING, MF_GRAYED, MF_SEPARATOR = 0x0, 0x1, 0x800
TPM_RIGHTBUTTON, TPM_BOTTOMALIGN, TPM_NONOTIFY, TPM_RETURNCMD = 0x02, 0x20, 0x80, 0x100
KEYEVENTF_KEYUP = 0x02
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x02
ERROR_ALREADY_EXISTS = 183

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON),
    ]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
        ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND),
    ]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM),
    ]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [
        ("MakeCode", wintypes.USHORT), ("Flags", wintypes.USHORT), ("Reserved", wintypes.USHORT),
        ("VKey", wintypes.USHORT), ("Message", wintypes.UINT), ("ExtraInformation", wintypes.ULONG),
    ]


class RAWMOUSE(ctypes.Structure):
    class _Buttons(ctypes.Union):
        class _Flags(ctypes.Structure):
            _fields_ = [("usButtonFlags", wintypes.USHORT), ("usButtonData", wintypes.USHORT)]

        _anonymous_ = ("flags",)
        _fields_ = [("ulButtons", wintypes.ULONG), ("flags", _Flags)]

    _anonymous_ = ("buttons",)
    _fields_ = [
        ("usFlags", wintypes.USHORT), ("buttons", _Buttons), ("ulRawButtons", wintypes.ULONG),
        ("lLastX", wintypes.LONG), ("lLastY", wintypes.LONG), ("ulExtraInformation", wintypes.ULONG),
    ]


class RAWINPUT(ctypes.Structure):
    class _Data(ctypes.Union):
        _fields_ = [("mouse", RAWMOUSE), ("keyboard", RAWKEYBOARD)]

    _fields_ = [("header", RAWINPUTHEADER), ("data", _Data)]


def _declare(function, restype, *argtypes):
    function.restype = restype
    function.argtypes = argtypes


_declare(user32.RegisterClassW, wintypes.ATOM, ctypes.POINTER(WNDCLASSW))
_declare(user32.CreateWindowExW, wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
         wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
         wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
_declare(user32.DefWindowProcW, LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_declare(user32.GetMessageW, wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
_declare(user32.TranslateMessage, wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
_declare(user32.DispatchMessageW, LRESULT, ctypes.POINTER(wintypes.MSG))
_declare(user32.PostMessageW, wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_declare(user32.PostQuitMessage, None, ctypes.c_int)
_declare(user32.DestroyWindow, wintypes.BOOL, wintypes.HWND)
_declare(user32.RegisterWindowMessageW, wintypes.UINT, wintypes.LPCWSTR)
_declare(user32.FindWindowW, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)
_declare(user32.RegisterRawInputDevices, wintypes.BOOL, ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT)
_declare(user32.GetRawInputData, wintypes.UINT, wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID,
         ctypes.POINTER(wintypes.UINT), wintypes.UINT)
_declare(user32.SetTimer, ctypes.c_size_t, wintypes.HWND, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID)
_declare(user32.KillTimer, wintypes.BOOL, wintypes.HWND, ctypes.c_size_t)
_declare(user32.keybd_event, None, wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t)
_declare(user32.GetAsyncKeyState, ctypes.c_short, ctypes.c_int)
_declare(user32.LoadImageW, wintypes.HANDLE, wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
         ctypes.c_int, ctypes.c_int, wintypes.UINT)
_declare(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
_declare(user32.CreatePopupMenu, wintypes.HMENU)
_declare(user32.AppendMenuW, wintypes.BOOL, wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR)
_declare(user32.TrackPopupMenu, ctypes.c_int, wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
         ctypes.c_int, wintypes.HWND, wintypes.LPVOID)
_declare(user32.DestroyMenu, wintypes.BOOL, wintypes.HMENU)
_declare(user32.SetForegroundWindow, wintypes.BOOL, wintypes.HWND)
_declare(user32.GetCursorPos, wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
_declare(user32.GetWindowThreadProcessId, wintypes.DWORD, wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_declare(user32.AllowSetForegroundWindow, wintypes.BOOL, wintypes.DWORD)
_declare(user32.OpenClipboard, wintypes.BOOL, wintypes.HWND)
_declare(user32.EmptyClipboard, wintypes.BOOL)
_declare(user32.SetClipboardData, wintypes.HANDLE, wintypes.UINT, wintypes.HANDLE)
_declare(user32.CloseClipboard, wintypes.BOOL)
_declare(kernel32.GlobalAlloc, wintypes.HGLOBAL, wintypes.UINT, ctypes.c_size_t)
_declare(kernel32.GlobalLock, wintypes.LPVOID, wintypes.HGLOBAL)
_declare(kernel32.GlobalUnlock, wintypes.BOOL, wintypes.HGLOBAL)
_declare(kernel32.GlobalFree, wintypes.HGLOBAL, wintypes.HGLOBAL)
_declare(kernel32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)
_declare(kernel32.CreateMutexW, wintypes.HANDLE, wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
_declare(kernel32.CloseHandle, wintypes.BOOL, wintypes.HANDLE)
_declare(shell32.Shell_NotifyIconW, wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW))

# Before any window exists: otherwise Windows bitmap-stretches the tray icon and the settings
# window on scaled displays. System-wide DPI, not per-monitor: Tk does not rescale on the fly.
try:
    ctypes.WinDLL("shcore").SetProcessDpiAwareness(1)
except OSError:
    pass


def key_code(keyboard):
    # One number per physical key, comparable with HOTKEY_CODES.
    code = keyboard.MakeCode or 0x1000 | keyboard.VKey
    if keyboard.Flags & RI_KEY_E0:
        code |= 0x100
    if keyboard.Flags & RI_KEY_E1:
        code |= 0x200
    return code


def _icon_path(name):
    return os.path.join(app.ASSETS_DIR, f"{name}.ico")


class Window:
    # A hidden window and its message loop on a thread of their own: the window owns the tray
    # icon and receives raw keyboard and mouse input, so typing never waits for Python.
    def __init__(self):
        self.recorder = None
        self.tray = None  # icon2.TrayIcon once its icon is shown
        self.icon_shown = False
        self.mouse_watched = False
        self.icons = {}
        self.hwnd = None
        self.taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self._wndproc = WNDPROC(self._handle)  # Windows calls it for as long as the window lives
        self._ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait()

    def _run(self):
        hinstance = kernel32.GetModuleHandleW(None)
        window_class = WNDCLASSW(lpfnWndProc=self._wndproc, hInstance=hinstance, lpszClassName=WINDOW_CLASS)
        user32.RegisterClassW(ctypes.byref(window_class))
        # A hidden top-level window rather than a message-only one: only top-level windows
        # get the TaskbarCreated broadcast after Explorer restarts.
        try:
            self.hwnd = user32.CreateWindowExW(0, WINDOW_CLASS, "Altalk", 0, 0, 0, 0, 0, None, None, hinstance, None)
            self._register_raw_input(USAGE_KEYBOARD, RIDEV_INPUTSINK)
        finally:
            self._ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _register_raw_input(self, usage, flags):
        device = RAWINPUTDEVICE(USAGE_PAGE_GENERIC, usage, flags, None if flags & RIDEV_REMOVE else self.hwnd)
        if not user32.RegisterRawInputDevices(ctypes.byref(device), 1, ctypes.sizeof(device)):
            app.log(f"RegisterRawInputDevices failed: error {ctypes.get_last_error()}")

    def _handle(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_INPUT:
                self._on_raw_input(lparam)  # and DefWindowProc below frees the input
            elif msg == WM_TRAY:
                if lparam in (WM_LBUTTONUP, WM_RBUTTONUP):
                    self._show_menu()
                return 0
            elif msg == WM_TIMER and wparam == MENU_MASK_TIMER:
                user32.KillTimer(hwnd, MENU_MASK_TIMER)
                self._mask_menu_key()
                return 0
            elif msg == WM_TIMER and wparam == KEY_WATCH_TIMER:
                self._check_hotkey_held()
                return 0
            elif msg == WM_OPEN_SETTINGS:
                if self.tray:
                    self.tray.loop.call_soon_threadsafe(self.tray.open_settings)
                return 0
            elif msg == self.taskbar_created and self.icon_shown:
                self._notify_icon(NIM_ADD, NIF_MESSAGE | NIF_ICON | NIF_TIP)  # Explorer restarted
                return 0
            elif msg == WM_CLOSE and self.tray:
                # taskkill without /f and the installer close the app this way.
                self.tray.loop.call_soon_threadsafe(self.tray.quit_event.set)
                return 0
            elif msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception as e:
            app.log(f"Window message {msg:#x} failed: {e!r}")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _on_raw_input(self, handle):
        data = RAWINPUT()
        size = wintypes.UINT(ctypes.sizeof(data))
        header_size = ctypes.sizeof(RAWINPUTHEADER)
        if user32.GetRawInputData(handle, RID_INPUT, ctypes.byref(data), ctypes.byref(size), header_size) in (0, 0xFFFFFFFF):
            return
        recorder = self.recorder
        if recorder is None:
            return
        if data.header.dwType == RIM_TYPEKEYBOARD:
            keyboard = data.data.keyboard
            if keyboard.VKey == MENU_MASK_KEY:
                return  # our own key from _mask_menu_key
            code = key_code(keyboard)
            if keyboard.Flags & RI_KEY_BREAK:
                recorder.on_release(code)
            else:
                was_recording = recorder.recording
                recorder.on_press(code)
                if recorder.recording and not was_recording and code == HOTKEY_CODES["alt"]:
                    # Only a hold long enough to be a recording: a short tap of Alt
                    # still opens the menu bar, as Windows users expect.
                    user32.SetTimer(self.hwnd, MENU_MASK_TIMER, int(app.MIN_DURATION_SECONDS * 1000), None)
        elif data.header.dwType == RIM_TYPEMOUSE:
            flags = data.data.mouse.usButtonFlags
            if flags & RI_MOUSE_BUTTON_DOWNS:
                recorder.cancel("Mouse button pressed")
            elif flags & RI_MOUSE_WHEELS:
                recorder.cancel("Mouse wheel scrolled")
        self._follow_recording()

    def _follow_recording(self):
        # The mouse matters only while the hotkey is held; mouse moves are not worth a
        # Python call each the rest of the time.
        recording = self.recorder.recording
        if recording == self.mouse_watched:
            return
        self.mouse_watched = recording
        self._register_raw_input(USAGE_MOUSE, RIDEV_INPUTSINK if recording else RIDEV_REMOVE)
        if recording:
            user32.SetTimer(self.hwnd, KEY_WATCH_TIMER, KEY_WATCH_MILLISECONDS, None)
        else:
            user32.KillTimer(self.hwnd, KEY_WATCH_TIMER)

    def _check_hotkey_held(self):
        # The key-up never arrives when Windows locks or an elevated window takes the keyboard
        # mid-press; without this the microphone would stay on until the next press.
        recorder = self.recorder
        virtual_key = HOTKEY_VIRTUAL_KEYS[recorder.keycode]
        if recorder.recording and not user32.GetAsyncKeyState(virtual_key) & 0x8000:
            app.log(f"{recorder.key_name} is no longer held")
            recorder.on_release(recorder.keycode)
        self._follow_recording()

    def _mask_menu_key(self):
        recorder = self.recorder
        if recorder.recording and not recorder.aborted:
            user32.keybd_event(MENU_MASK_KEY, 0, 0, 0)
            user32.keybd_event(MENU_MASK_KEY, 0, KEYEVENTF_KEYUP, 0)

    def _load_icon(self, name, size):
        key = (name, size)
        if key not in self.icons:
            self.icons[key] = user32.LoadImageW(None, _icon_path(name), IMAGE_ICON, size, size, LR_LOADFROMFILE)
        return self.icons[key]

    def _notify_icon(self, message, flags, **fields):
        data = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self.hwnd, uID=1, uFlags=flags)
        if flags & NIF_MESSAGE:
            data.uCallbackMessage = WM_TRAY
        if flags & NIF_ICON:
            data.hIcon = self._load_icon(self.tray.icon_name, user32.GetSystemMetrics(SM_CXSMICON))
        if flags & NIF_TIP:
            data.szTip = f"Altalk\n{self.tray.hint}"[:127]
        for field, value in fields.items():
            setattr(data, field, value)
        return shell32.Shell_NotifyIconW(message, ctypes.byref(data))

    def show_icon(self, tray):
        self.tray = tray
        self.icon_shown = bool(self._notify_icon(NIM_ADD, NIF_MESSAGE | NIF_ICON | NIF_TIP))
        if self.icon_shown:
            app.log("Tray icon registered")
        else:
            app.log("Tray icon unavailable: Shell_NotifyIcon failed")

    def update_icon(self):
        if self.icon_shown:
            self._notify_icon(NIM_MODIFY, NIF_ICON | NIF_TIP)

    def hide_icon(self):
        if self.icon_shown:
            self._notify_icon(NIM_DELETE, 0)
            self.icon_shown = False

    def balloon(self, title, message):
        large = user32.GetSystemMetrics(SM_CXICON)
        self._notify_icon(
            NIM_MODIFY, NIF_INFO,
            szInfoTitle=title[:63], szInfo=message[:255],
            dwInfoFlags=NIIF_USER | NIIF_LARGE_ICON | NIIF_NOSOUND,
            hBalloonIcon=self._load_icon("altalk", large),
        )

    def _show_menu(self):
        menu = user32.CreatePopupMenu()
        for item_id, props in self.tray.menu_items():
            if props.get("type") == "separator":
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            else:
                flags = MF_STRING if props.get("enabled", True) else MF_STRING | MF_GRAYED
                # "&" marks a mnemonic in Win32 menus; "&&" is a literal ampersand.
                user32.AppendMenuW(menu, flags, item_id, props["label"].replace("&", "&&"))
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        # Without this the menu does not close when the user clicks elsewhere.
        user32.SetForegroundWindow(self.hwnd)
        chosen = user32.TrackPopupMenu(
            menu, TPM_RIGHTBUTTON | TPM_BOTTOMALIGN | TPM_NONOTIFY | TPM_RETURNCMD,
            point.x, point.y, 0, self.hwnd, None,
        )
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if chosen:
            self.tray.loop.call_soon_threadsafe(self.tray.on_click, chosen)


_window = None


def window():
    global _window
    if _window is None:
        _window = Window()
    return _window


def watch_input(recorder):
    window().recorder = recorder


def notify(title, message):
    # A balloon from the tray icon; Windows 10 and 11 show it as a toast.
    if _window and _window.icon_shown:
        _window.balloon(title, message)


def copy_to_clipboard(text):
    data = ctypes.create_unicode_buffer(text)
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, ctypes.sizeof(data))
    ctypes.memmove(kernel32.GlobalLock(handle), data, ctypes.sizeof(data))
    kernel32.GlobalUnlock(handle)
    for _ in range(CLIPBOARD_ATTEMPTS):
        # With no owner window SetClipboardData fails, so the hidden window owns the text.
        if user32.OpenClipboard(window().hwnd):
            break
        time.sleep(0.05)  # another app holds the clipboard for a moment
    else:
        kernel32.GlobalFree(handle)
        app.log("Clipboard is busy, text not copied")
        return
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)  # otherwise the clipboard owns it now
            app.log(f"SetClipboardData failed: error {ctypes.get_last_error()}")
    finally:
        user32.CloseClipboard()


class Tray:
    # TrayIcon as a notification-area icon; its menu is built anew on every click.
    def __init__(self, tray):
        self.tray = tray

    async def start(self):
        window().show_icon(self.tray)

    def icon_changed(self):
        window().update_icon()

    def hint_changed(self):
        window().update_icon()

    def menu_changed(self):
        pass

    async def edit_settings(self, values):
        future = self.tray.loop.create_future()

        def run():
            result = show_settings_dialog(values)
            self.tray.loop.call_soon_threadsafe(future.set_result, result)

        threading.Thread(target=run, daemon=True).start()
        return await future

    def stop(self):
        window().hide_icon()


def check_api_key(api_key, model, texts):
    # The same check as in settings_dialog.py, which Windows does not have.
    if not api_key:
        return texts["enter_key"]
    request = urllib.request.Request(
        f"https://api.openai.com/v1/models/{urllib.parse.quote(model)}",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        urllib.request.urlopen(request, timeout=15).close()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return texts["key_rejected"]
        if e.code in (403, 404):
            return texts["model_unavailable"].format(model=model)
    except OSError:
        pass  # offline: save without the check rather than block the user
    return None


class SettingsDialog:
    # The same fields as the GTK window on Linux, in the native Windows look (ttk's default theme).
    def __init__(self, current):
        self.texts = texts = current["texts"]
        self.result = None
        self.check_result = None
        self.models = list(current.get("models") or [])
        model = current.get("model") or (self.models[0] if self.models else "")
        if model not in self.models:
            self.models.insert(0, model)
        self.hotkeys = current.get("hotkeys") or []
        hotkey_ids = [hotkey for hotkey, _name in self.hotkeys]
        hotkey = current.get("hotkey")
        self.languages = current["languages"]
        language_ids = [language for language, _name in self.languages]

        self.root = root = tk.Tk()
        root.title(texts["dialog_title"])
        root.resizable(False, False)
        if os.path.isfile(_icon_path("altalk")):
            root.iconbitmap(default=_icon_path("altalk"))
        frame = ttk.Frame(root, padding=12)
        frame.grid(sticky="nsew")

        openai = ttk.Labelframe(frame, text="OpenAI", padding=(12, 6, 12, 10))
        openai.grid(row=0, column=0, sticky="ew")
        openai.columnconfigure(1, weight=1)
        ttk.Label(openai, text=texts["api_key"]).grid(row=0, column=0, sticky="w", pady=4)
        self.key = tk.StringVar(root, current.get("key", ""))
        self.key_entry = ttk.Entry(openai, textvariable=self.key, show="•", width=44)
        self.key_entry.grid(row=0, column=1, sticky="ew", padx=(12, 0), pady=4)
        self.show_key = tk.BooleanVar(root, False)
        ttk.Checkbutton(openai, text=texts["show_key"], variable=self.show_key, command=self.toggle_key).grid(
            row=1, column=1, sticky="w", padx=(12, 0))
        ttk.Label(openai, text=texts["model"]).grid(row=2, column=0, sticky="w", pady=(8, 4))
        self.model = ttk.Combobox(openai, values=self.models, state="readonly")
        self.model.set(model)
        self.model.grid(row=2, column=1, sticky="ew", padx=(12, 0), pady=(8, 4))
        ttk.Label(openai, text=texts["speech_language"]).grid(row=3, column=0, sticky="w", pady=4)
        self.language = ttk.Combobox(openai, values=[name for _language, name in self.languages], state="readonly")
        language = current.get("language")
        self.language.current(language_ids.index(language) if language in language_ids else 0)
        self.language.grid(row=3, column=1, sticky="ew", padx=(12, 0), pady=4)
        ttk.Label(openai, text=texts["stored_in"].format(path=current.get("path", "")), foreground="gray").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

        general = ttk.Labelframe(frame, text=texts["general"], padding=(12, 6, 12, 10))
        general.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        general.columnconfigure(1, weight=1)
        ttk.Label(general, text=texts["hotkey"]).grid(row=0, column=0, sticky="w", pady=4)
        self.hotkey = ttk.Combobox(general, values=[name for _hotkey, name in self.hotkeys], state="readonly")
        if self.hotkeys:
            self.hotkey.current(hotkey_ids.index(hotkey) if hotkey in hotkey_ids else 0)
        self.hotkey.grid(row=0, column=1, sticky="ew", padx=(12, 0), pady=4)
        ttk.Label(general, text=texts["hotkey_hint"], foreground="gray").grid(
            row=1, column=1, sticky="w", padx=(12, 0))
        self.autostart = tk.BooleanVar(root, current.get("autostart", False))
        ttk.Checkbutton(general, text=texts["autostart"], variable=self.autostart).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))

        self.error = ttk.Label(frame, foreground=ERROR_COLOR, wraplength=420)
        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, sticky="e", pady=(12, 0))
        self.save_button = ttk.Button(buttons, text=texts["save"], default="active", command=self.on_save)
        self.save_button.grid(row=0, column=0)
        ttk.Button(buttons, text=texts["cancel"], command=root.destroy).grid(row=0, column=1, padx=(8, 0))
        root.bind("<Return>", self.on_save)
        root.bind("<Escape>", lambda _event: root.destroy())

        # Centered and in front: opened from the tray, it would otherwise appear behind other windows.
        root.eval("tk::PlaceWindow . center")
        root.attributes("-topmost", True)
        root.after(300, lambda: root.attributes("-topmost", False))
        root.focus_force()
        self.key_entry.focus_set()

    def toggle_key(self):
        self.key_entry.configure(show="" if self.show_key.get() else "•")

    def on_save(self, _event=None):
        if self.save_button.instate(["disabled"]):
            return
        key, model = self.key.get().strip(), self.model.get()
        self.save_button.state(["disabled"])
        self.save_button.configure(text=self.texts["checking"])
        self.check_result = None
        # The HTTP check runs off the Tk thread so the window keeps redrawing.
        threading.Thread(target=self.check, args=(key, model), daemon=True).start()
        self.root.after(100, self.on_checked, key, model)

    def check(self, key, model):
        self.check_result = [check_api_key(key, model, self.texts)]

    def on_checked(self, key, model):
        if self.check_result is None:
            self.root.after(100, self.on_checked, key, model)
            return
        error = self.check_result[0]
        self.save_button.state(["!disabled"])
        self.save_button.configure(text=self.texts["save"])
        if error:
            self.error.configure(text=error)
            self.error.grid(row=2, column=0, sticky="w", pady=(10, 0))
            self.key_entry.focus_set()
            return
        self.result = {
            "key": key,
            "model": model,
            "language": self.languages[self.language.current()][0],
            "hotkey": self.hotkeys[self.hotkey.current()][0],
            "autostart": self.autostart.get(),
        }
        self.root.destroy()


def show_settings_dialog(current):
    # Runs on a thread of its own. Tk must also be torn down on that thread: an interpreter
    # garbage-collected on another one aborts the process (Tcl_AsyncDelete).
    dialog = SettingsDialog(current)
    dialog.root.mainloop()
    result = dialog.result
    del dialog
    gc.collect()
    return result


def _autostart_command():
    return subprocess.list2cmdline(app.app_command())


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, AUTOSTART_VALUE)
    except OSError:
        return False
    # «Startup apps» in Windows Settings and Task Manager switch an entry off here, keeping it in Run:
    # the first byte is even when it is on, odd when it is off.
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_APPROVED_KEY) as key:
            state, _type = winreg.QueryValueEx(key, AUTOSTART_VALUE)
    except OSError:
        return True
    return not state or state[0] % 2 == 0


def _registered_command():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, AUTOSTART_VALUE)[0]
    except OSError:
        return None


def set_autostart(enabled):
    # Rewritten also when the command differs, e.g. after moving from the portable .exe to the
    # installed one: Windows would otherwise keep starting the old copy at sign-in.
    if enabled == autostart_enabled() and (not enabled or _registered_command() == _autostart_command()):
        return
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, AUTOSTART_VALUE, 0, winreg.REG_SZ, _autostart_command())
        else:
            winreg.DeleteValue(key, AUTOSTART_VALUE)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_APPROVED_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, AUTOSTART_VALUE)
    except OSError:
        pass
    app.log(f"Autostart {'enabled' if enabled else 'disabled'}: HKCU\\{RUN_KEY}\\{AUTOSTART_VALUE}")


class InstanceLock:
    def __init__(self, handle):
        self.handle = handle

    def close(self):
        kernel32.CloseHandle(self.handle)


def acquire_instance_lock():
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        # Started again, e.g. from the Start menu: the running copy opens its settings instead,
        # which also shows where it is when its icon is hidden in the tray overflow.
        hwnd = user32.FindWindowW(WINDOW_CLASS, None)
        if hwnd:
            # Only the process the user just started may bring a window to the front; pass that on.
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            user32.AllowSetForegroundWindow(pid.value)
            user32.PostMessageW(hwnd, WM_OPEN_SETTINGS, 0, 0)
        raise SystemExit("Altalk is already running")
    return InstanceLock(handle)
