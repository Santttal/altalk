"""
Linux-часть Altalk: клавиши из /dev/input (evdev), буфер обмена — свой владелец X11 CLIPBOARD,
уведомления и значок в трее — по D-Bus, окно настроек — GTK (settings_dialog.py), автозапуск — XDG.
"""

import asyncio
import fcntl
import json
import os
import select
import shutil
import tempfile
import threading
import time

from evdev import InputDevice, ecodes, list_devices
from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method
from dbus_fast.service import signal as dbus_signal
from Xlib import X, Xatom
from Xlib import display as xdisplay
from Xlib.protocol import event as xevent

app = None  # the icon2 module; icon2.load_desktop() sets it before anything here runs

HOTKEY_CODES = {"alt": ecodes.KEY_LEFTALT, "ctrl": ecodes.KEY_LEFTCTRL}
KEEP_MIC_OPEN = True
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_HOME = os.getenv("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
AUTOSTART_PATH = os.path.join(CONFIG_HOME, "autostart", "altalk.desktop")
LOCK_PATH = os.path.join(os.getenv("XDG_RUNTIME_DIR", tempfile.gettempdir()), "altalk.lock")
# GTK 4 + libadwaita come with Ubuntu's GNOME, but only for the system Python.
SETTINGS_DIALOG = ["/usr/bin/python3", os.path.join(APP_DIR, "settings_dialog.py")]
SETTINGS_DIALOG_UNAVAILABLE = 3
SNI_WATCHER = "org.kde.StatusNotifierWatcher"
DESKTOP_ENTRY = """\
[Desktop Entry]
Type=Application
Name=Altalk
Comment=Hold Left Alt, speak, get the text in the clipboard
Comment[ru]=Зажми Left Alt, говори — текст в буфере обмена
Exec={exec}
Icon=altalk
Terminal=false
Categories=Utility;
StartupNotify=false
StartupWMClass=Altalk
"""
INPUT_RESCAN_INTERVAL_SECONDS = 2.0


async def _notify(title, message):
    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    try:
        await bus.call(
            Message(
                destination="org.freedesktop.Notifications",
                path="/org/freedesktop/Notifications",
                interface="org.freedesktop.Notifications",
                member="Notify",
                signature="susssasa{sv}i",
                body=["Altalk", 0, app.ICON_PATH, title, message, [], {}, -1],
            )
        )
    finally:
        bus.disconnect()


def notify(title, message):
    try:
        asyncio.run(_notify(title, message))
    except Exception:
        pass


class X11Clipboard:
    # Owns the CLIPBOARD selection the way xclip does; GNOME hands it over to Wayland apps.
    def __init__(self):
        self.display = xdisplay.Display()
        self.window = self.display.screen().root.create_window(0, 0, 1, 1, 0, X.CopyFromParent)
        self.selection = self.display.intern_atom("CLIPBOARD")
        self.targets = self.display.intern_atom("TARGETS")
        self.text_targets = [
            self.display.intern_atom("UTF8_STRING"),
            self.display.intern_atom("text/plain;charset=utf-8"),
        ]
        # Without the INCR protocol one property write must fit into a single X request.
        self.max_bytes = self.display.display.info.max_request_length * 4 - 64
        self.data = b""
        self.wakeup_read, self.wakeup_write = os.pipe()
        threading.Thread(target=self._serve, daemon=True).start()

    def copy(self, text):
        data = text.encode("utf-8")
        if len(data) > self.max_bytes:
            app.log(f"Text is too long for the clipboard ({len(data)} bytes), not copied")
            return
        self.data = data
        os.write(self.wakeup_write, b"\0")

    def _serve(self):
        # All X calls stay on this thread; copy() only wakes it up through the pipe.
        while True:
            readable, _, _ = select.select([self.display, self.wakeup_read], [], [])
            if self.wakeup_read in readable:
                os.read(self.wakeup_read, 64)
                self.window.set_selection_owner(self.selection, X.CurrentTime)
                self.display.flush()
            while self.display.pending_events():
                event = self.display.next_event()
                if event.type == X.SelectionRequest:
                    self._answer(event)

    def _answer(self, request):
        prop = request.property or request.target
        if request.target == self.targets:
            request.requestor.change_property(
                prop, Xatom.ATOM, 32, [self.targets, *self.text_targets]
            )
        elif request.target in self.text_targets:
            request.requestor.change_property(prop, request.target, 8, self.data)
        else:
            prop = X.NONE
        request.requestor.send_event(
            xevent.SelectionNotify(
                time=request.time,
                requestor=request.requestor,
                selection=request.selection,
                target=request.target,
                property=prop,
            )
        )
        self.display.flush()


_clipboard = None
_clipboard_lock = threading.Lock()


def copy_to_clipboard(text):
    global _clipboard
    with _clipboard_lock:
        if _clipboard is None:
            _clipboard = X11Clipboard()
    _clipboard.copy(text)


def find_input_devices():
    # Keyboards only: a key pressed while the hotkey is held cancels the recording, the mouse does not.
    devices = []
    for path in list_devices():
        try:
            d = InputDevice(path)
        except (OSError, PermissionError):
            continue
        keys = d.capabilities().get(ecodes.EV_KEY, [])
        if any(code in keys for code in (ecodes.KEY_LEFTALT, ecodes.KEY_LEFTCTRL)):
            devices.append(d)
    return devices


def listen_device(device, recorder):
    try:
        for event in device.read_loop():
            if event.type != ecodes.EV_KEY:
                continue
            if ecodes.BTN_MISC <= event.code < ecodes.KEY_OK:
                continue  # mouse and touchpad buttons of a keyboard with a touchpad or a combined receiver
            if event.value == 1:
                recorder.on_press(event.code)
            elif event.value == 0:
                recorder.on_release(event.code)
    except OSError as e:
        app.log(f"Input listener for {device.path} stopped: {e}")
    finally:
        try:
            device.close()
        except Exception:
            pass


def input_supervisor(recorder):
    active = {}
    while True:
        for path in list(active):
            thread, _ = active[path]
            if not thread.is_alive():
                del active[path]
                app.log(f"Input listener for {path} gone, will rescan")

        try:
            current = find_input_devices()
        except Exception as e:
            app.log(f"Input rescan error: {e}")
            current = []

        for device in current:
            if device.path in active:
                try:
                    device.close()
                except Exception:
                    pass
                continue
            t = threading.Thread(
                target=listen_device, args=(device, recorder), daemon=True
            )
            t.start()
            active[device.path] = (t, device)
            app.log(f"Listening on {device.path} ({device.name})")

        time.sleep(INPUT_RESCAN_INTERVAL_SECONDS)


def run(main):
    asyncio.run(main)


def watch_input(recorder):
    initial = find_input_devices()
    if not initial:
        notify("Altalk", app.tr("notify_no_keyboard"))
        raise SystemExit(
            "No accessible keyboard devices found in /dev/input/. "
            "Ensure your user is in the 'input' group: sudo usermod -aG input $USER"
        )
    for d in initial:
        try:
            d.close()
        except Exception:
            pass
    threading.Thread(target=input_supervisor, args=(recorder,), daemon=True).start()


class StatusNotifierItem(ServiceInterface):
    # The tray icon itself (StatusNotifierItem spec); GNOME shows it via the AppIndicator extension.
    def __init__(self, tray):
        super().__init__("org.kde.StatusNotifierItem")
        self.tray = tray

    @dbus_property(access=PropertyAccess.READ)
    def Category(self) -> "s":
        return "ApplicationStatus"

    @dbus_property(access=PropertyAccess.READ)
    def Id(self) -> "s":
        return "altalk"

    @dbus_property(access=PropertyAccess.READ)
    def Title(self) -> "s":
        return "Altalk"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "Active"

    @dbus_property(access=PropertyAccess.READ)
    def IconName(self) -> "s":
        return self.tray.icon_name

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> "s":
        return app.ASSETS_DIR

    @dbus_property(access=PropertyAccess.READ)
    def ToolTip(self) -> "(sa(iiay)ss)":
        return ["", [], "Altalk", self.tray.hint]

    @dbus_property(access=PropertyAccess.READ)
    def ItemIsMenu(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def Menu(self) -> "o":
        return "/MenuBar"

    @method()
    def Activate(self, x: "i", y: "i"):
        pass

    @method()
    def SecondaryActivate(self, x: "i", y: "i"):
        pass

    @method()
    def ContextMenu(self, x: "i", y: "i"):
        pass

    @method()
    def Scroll(self, delta: "i", orientation: "s"):
        pass

    @dbus_signal()
    def NewIcon(self):
        pass

    @dbus_signal()
    def NewToolTip(self):
        pass


def dbus_menu_properties(props):
    # DBusMenu treats "_" as a mnemonic marker; "__" is a literal underscore.
    return {
        key: Variant("b", value) if isinstance(value, bool)
        else Variant("s", value.replace("_", "__") if key == "label" else value)
        for key, value in props.items()
    }


class DBusMenu(ServiceInterface):
    # The tray menu (com.canonical.dbusmenu); the tray host renders it, items come from TrayIcon.
    def __init__(self, tray):
        super().__init__("com.canonical.dbusmenu")
        self.tray = tray
        self.revision = 1

    def menu_items(self):
        return [(item_id, dbus_menu_properties(props)) for item_id, props in self.tray.menu_items()]

    @dbus_property(access=PropertyAccess.READ)
    def Version(self) -> "u":
        return 3

    @dbus_property(access=PropertyAccess.READ)
    def TextDirection(self) -> "s":
        return "ltr"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "normal"

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> "as":
        return []

    @method()
    def GetLayout(self, parent_id: "i", recursion_depth: "i", property_names: "as") -> "u(ia{sv}av)":
        items = self.menu_items()
        if parent_id != 0:
            return [self.revision, [parent_id, dict(items).get(parent_id, {}), []]]
        children = [Variant("(ia{sv}av)", [item_id, props, []]) for item_id, props in items]
        return [self.revision, [0, {"children-display": Variant("s", "submenu")}, children]]

    @method()
    def GetGroupProperties(self, ids: "ai", property_names: "as") -> "a(ia{sv})":
        items = dict(self.menu_items())
        return [[item_id, items[item_id]] for item_id in ids if item_id in items]

    @method()
    def GetProperty(self, item_id: "i", name: "s") -> "v":
        return dict(self.menu_items()).get(item_id, {}).get(name, Variant("s", ""))

    @method()
    def Event(self, item_id: "i", event_id: "s", data: "v", timestamp: "u"):
        if event_id == "clicked":
            self.tray.on_click(item_id)

    @method()
    def EventGroup(self, events: "a(isvu)") -> "ai":
        for item_id, event_id, _data, _timestamp in events:
            if event_id == "clicked":
                self.tray.on_click(item_id)
        return []

    @method()
    def AboutToShow(self, item_id: "i") -> "b":
        return False

    @method()
    def AboutToShowGroup(self, ids: "ai") -> "aiai":
        return [[], []]

    @dbus_signal()
    def LayoutUpdated(self) -> "ui":
        return [self.revision, 0]


class Tray:
    # TrayIcon on the session bus: StatusNotifierItem for the icon, DBusMenu for its menu.
    def __init__(self, tray):
        self.tray = tray
        self.item = StatusNotifierItem(tray)
        self.menu = DBusMenu(tray)
        self.settings_process = None

    async def start(self):
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        bus.export("/StatusNotifierItem", self.item)
        bus.export("/MenuBar", self.menu)
        name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        await bus.request_name(name)

        def on_message(message):
            # The tray host restarted (e.g. GNOME Shell reloaded the extension): register again.
            if message.member == "NameOwnerChanged" and message.body[0] == SNI_WATCHER and message.body[2]:
                self.tray.loop.create_task(self._register(bus, name))

        bus.add_message_handler(on_message)
        await bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch",
                signature="s",
                body=[f"type='signal',member='NameOwnerChanged',arg0='{SNI_WATCHER}'"],
            )
        )
        await self._register(bus, name)

    async def _register(self, bus, name):
        reply = await bus.call(
            Message(
                destination=SNI_WATCHER,
                path="/StatusNotifierWatcher",
                interface=SNI_WATCHER,
                member="RegisterStatusNotifierItem",
                signature="s",
                body=[name],
            )
        )
        if reply.message_type == MessageType.ERROR:
            app.log(f"No tray host ({reply.error_name}); on GNOME enable the AppIndicator extension")
        else:
            app.log("Tray icon registered")

    def icon_changed(self):
        self.item.NewIcon()

    def hint_changed(self):
        self.item.NewToolTip()

    def menu_changed(self):
        self.menu.revision += 1
        self.menu.LayoutUpdated()

    async def edit_settings(self, values):
        # settings_dialog.py gets the current values on stdin and prints the saved ones.
        try:
            self.settings_process = await asyncio.create_subprocess_exec(
                *SETTINGS_DIALOG,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                # Local-only GIO: GTK otherwise calls gvfsd on startup and waits out
                # its 25 s D-Bus timeout whenever gvfsd hangs (e.g. on a flaky USB disk).
                # Software rendering: a two-row window gains nothing from OpenGL, and a
                # broken GL driver would otherwise crash or hang it.
                env={**app.system_env(), "GIO_USE_VFS": "local", "GSK_RENDERER": "cairo"},
            )
        except OSError:
            code, output = SETTINGS_DIALOG_UNAVAILABLE, b""
        else:
            output, _ = await self.settings_process.communicate(json.dumps(values).encode())
            code = self.settings_process.returncode
            self.settings_process = None
        if code == SETTINGS_DIALOG_UNAVAILABLE:
            app.log("Settings dialog needs /usr/bin/python3 with python3-gi and gir1.2-adw-1")
            try:
                await _notify("Altalk", app.tr("notify_no_gtk", path=app.settings_path()))
            except Exception:
                pass
        return json.loads(output) if code == 0 else None

    def stop(self):
        if self.settings_process and self.settings_process.returncode is None:
            self.settings_process.terminate()


def desktop_entry():
    return DESKTOP_ENTRY.format(exec=" ".join(app.app_command()))


def autostart_enabled():
    # Session managers skip an entry that is hidden or switched off (e.g. by GNOME Tweaks).
    if not os.path.isfile(AUTOSTART_PATH):
        return False
    entry = app.read_env_file(AUTOSTART_PATH)
    return entry.get("Hidden") != "true" and entry.get("X-GNOME-Autostart-enabled") != "false"


def set_autostart(enabled):
    # XDG autostart: GNOME, KDE and the rest start ~/.config/autostart/*.desktop at login.
    # Rewritten also when the app has moved (e.g. from ~/.local/bin into a package).
    registered = app.read_env_file(AUTOSTART_PATH).get("Exec")
    if enabled == autostart_enabled() and (not enabled or registered == " ".join(app.app_command())):
        return
    if enabled:
        os.makedirs(os.path.dirname(AUTOSTART_PATH), exist_ok=True)
        with open(AUTOSTART_PATH, "w") as f:
            f.write(desktop_entry())
        app.log(f"Autostart enabled: {AUTOSTART_PATH}")
    else:
        os.remove(AUTOSTART_PATH)
        app.log("Autostart disabled")


def install_desktop_entry():
    data_home = os.getenv("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    icon_dir = os.path.join(data_home, "icons", "hicolor", "256x256", "apps")
    apps_dir = os.path.join(data_home, "applications")
    os.makedirs(icon_dir, exist_ok=True)
    os.makedirs(apps_dir, exist_ok=True)
    shutil.copyfile(app.ICON_PATH, os.path.join(icon_dir, "altalk.png"))
    entry_path = os.path.join(apps_dir, "altalk.desktop")
    with open(entry_path, "w") as f:
        f.write(desktop_entry())
    print(f"Installed {entry_path}")


def acquire_instance_lock():
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        notify("Altalk", app.tr("notify_running"))
        raise SystemExit("Altalk is already running")
    return lock_file
