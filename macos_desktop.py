"""
macOS-часть Altalk: клавиши — CGEventTap (только слушает, нужен «Мониторинг ввода»), значок —
NSStatusItem в строке меню, буфер обмена — NSPasteboard, уведомления — UserNotifications,
окно настроек — AppKit, автозапуск — LaunchAgent. Всё через PyObjC.
AppKit работает только в главном потоке, поэтому логика трея (asyncio) живёт в соседнем.
"""

import asyncio
import fcntl
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import AppKit
import AVFoundation
import Foundation
import objc
import Quartz
import UserNotifications
from PyObjCTools import AppHelper

app = None  # the icon2 module; icon2.load_desktop() sets it before anything here runs

# Virtual key codes (Carbon kVK_*) of the left Option and the left Control.
HOTKEY_CODES = {"alt": 58, "ctrl": 59}
KEEP_MIC_OPEN = False  # the orange «microphone in use» dot in the menu bar would never go away
BUNDLE_ID = "dev.abogdanov.altalk"
LAUNCH_AGENT_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{BUNDLE_ID}.plist")
LOCK_PATH = os.path.join(tempfile.gettempdir(), "altalk.lock")  # $TMPDIR is per user on macOS
# Device-dependent modifier bits (NX_DEVICE*KEYMASK) tell which of the two keys is down.
MODIFIER_MASKS = {
    59: 0x0001, 62: 0x2000,  # left, right Control
    56: 0x0002, 60: 0x0004,  # left, right Shift
    55: 0x0008, 54: 0x0010,  # left, right Command
    58: 0x0020, 61: 0x0040,  # left, right Option
    63: Quartz.kCGEventFlagMaskSecondaryFn,
}
# Keyboard only: a key pressed while the hotkey is held cancels the recording, the mouse does not.
EVENT_MASK = Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown) | Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
INPUT_ACCESS_POLL_SECONDS = 2.0
STATUS_ICON_POINTS = 18
FIELD_WIDTH = 300
# SF Symbols for the menu items; the history gives GNOME icon names (see icon2.HISTORY_ICONS).
MENU_SYMBOLS = {
    "edit-copy-symbolic": "doc.on.doc",
    "dialog-error-symbolic": "exclamationmark.triangle",
    "dialog-information-symbolic": "info.circle",
    "content-loading-symbolic": "hourglass",
}

_tap = None
_recorder = None
_tray = None  # icon2.TrayIcon once its status item exists
_settings = None  # the open settings window, kept referenced until it closes
_notification_center = None
_notifications_allowed = None  # until macOS answers; refused → osascript


def run(main):
    # AppKit owns the main thread, so the tray logic (asyncio) runs on another one.
    application = AppKit.NSApplication.sharedApplication()
    application.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    delegate = AppDelegate.alloc().init()
    application.setDelegate_(delegate)

    def run_tray():
        try:
            asyncio.run(main)
        finally:
            AppHelper.callAfter(AppHelper.stopEventLoop)

    threading.Thread(target=run_tray, daemon=True).start()
    AppHelper.runEventLoop()


class AppDelegate(Foundation.NSObject):
    def applicationShouldHandleReopen_hasVisibleWindows_(self, sender, has_visible_windows):
        # Altalk.app opened again (Finder, Spotlight): show where it is by opening its settings.
        if _tray:
            _tray.loop.call_soon_threadsafe(_tray.open_settings)
        return True


def _on_main(loop, function, *args):
    # Runs function on the AppKit thread; the returned future resolves on the asyncio loop.
    future = loop.create_future()

    def call():
        try:
            result = function(*args)
        except Exception as e:
            loop.call_soon_threadsafe(future.set_exception, e)
        else:
            loop.call_soon_threadsafe(future.set_result, result)

    AppHelper.callAfter(call)
    return future


def _on_event(proxy, event_type, event, refcon):
    recorder = _recorder
    if event_type in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
        Quartz.CGEventTapEnable(_tap, True)
    elif event_type == Quartz.kCGEventFlagsChanged:
        code = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
        mask = MODIFIER_MASKS.get(code)
        if mask is not None and not Quartz.CGEventGetFlags(event) & mask:
            recorder.on_release(code)
        else:
            recorder.on_press(code)  # a modifier went down (or Caps Lock toggled)
    elif event_type == Quartz.kCGEventKeyDown:
        recorder.on_press(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode))
    return event


def _listen_keyboard():
    # A listen-only tap needs «Input Monitoring»; until the user allows it, ask once and wait.
    global _tap
    if not Quartz.CGPreflightListenEventAccess():
        Quartz.CGRequestListenEventAccess()
        app.log("No Input Monitoring permission yet: waiting for it")
        notify("Altalk", app.tr("notify_no_input_monitoring"))
        while not Quartz.CGPreflightListenEventAccess():
            time.sleep(INPUT_ACCESS_POLL_SECONDS)
    _tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap, Quartz.kCGEventTapOptionListenOnly,
        EVENT_MASK, _on_event, None,
    )
    if _tap is None:
        app.log("CGEventTapCreate failed: the hotkey will not work")
        return
    source = Quartz.CFMachPortCreateRunLoopSource(None, _tap, 0)
    Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), source, Quartz.kCFRunLoopCommonModes)
    Quartz.CGEventTapEnable(_tap, True)
    app.log("Listening to the keyboard")
    Quartz.CFRunLoopRun()


def watch_input(recorder):
    global _recorder
    _recorder = recorder
    threading.Thread(target=_listen_keyboard, daemon=True).start()
    _notifications()  # ask now, so the answer is known before the first notification
    # Ask for the microphone now rather than on the first recording, which the prompt would cut short.
    audio = AVFoundation.AVMediaTypeAudio
    if AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(audio) == AVFoundation.AVAuthorizationStatusNotDetermined:
        AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            audio, lambda granted: app.log(f"Microphone access {'granted' if granted else 'denied'}")
        )


def copy_to_clipboard(text):
    def copy():
        pasteboard = AppKit.NSPasteboard.generalPasteboard()
        pasteboard.clearContents()
        pasteboard.setString_forType_(text, AppKit.NSPasteboardTypeString)

    AppHelper.callAfter(copy)


class NotificationDelegate(Foundation.NSObject):
    def userNotificationCenter_willPresentNotification_withCompletionHandler_(self, center, notification, handler):
        # Shown even while Altalk is the active app (e.g. with its settings open).
        handler(UserNotifications.UNNotificationPresentationOptionBanner | UserNotifications.UNNotificationPresentationOptionList)


_notification_delegate = NotificationDelegate.alloc().init()


def _on_authorization(granted, error):
    global _notifications_allowed
    _notifications_allowed = bool(granted)
    app.log("Notifications allowed" if granted else f"Notifications not allowed, falling back to osascript: {error}")


def _notifications():
    # UserNotifications works only inside an .app bundle; from source there is osascript.
    global _notification_center
    if _notification_center is None and Foundation.NSBundle.mainBundle().bundleIdentifier():
        center = UserNotifications.UNUserNotificationCenter.currentNotificationCenter()
        center.setDelegate_(_notification_delegate)
        center.requestAuthorizationWithOptions_completionHandler_(UserNotifications.UNAuthorizationOptionAlert, _on_authorization)
        _notification_center = center
    return _notification_center


def notify(title, message):
    center = _notifications()
    # macOS may refuse notifications to an app without a Developer ID; osascript still gets through
    # (shown as from Script Editor).
    if center is None or _notifications_allowed is False:
        script = f"display notification {json.dumps(message, ensure_ascii=False)} with title {json.dumps(title, ensure_ascii=False)}"
        subprocess.run(["osascript", "-e", script], check=False)
        return
    content = UserNotifications.UNMutableNotificationContent.alloc().init()
    content.setTitle_(title)
    content.setBody_(message)
    request = UserNotifications.UNNotificationRequest.requestWithIdentifier_content_trigger_(
        str(uuid.uuid4()), content, None
    )
    center.addNotificationRequest_withCompletionHandler_(request, None)


class MenuController(Foundation.NSObject):
    # The status item's menu: rebuilt from TrayIcon.menu_items() each time it opens.
    def menuNeedsUpdate_(self, menu):
        menu.removeAllItems()
        for item_id, props in self.tray.menu_items():
            if props.get("type") == "separator":
                menu.addItem_(AppKit.NSMenuItem.separatorItem())
                continue
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(props["label"], "clicked:", "")
            item.setTarget_(self)
            item.setTag_(item_id)
            item.setEnabled_(props.get("enabled", True))
            symbol = MENU_SYMBOLS.get(props.get("icon-name"))
            if symbol:
                item.setImage_(AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, None))
            menu.addItem_(item)

    def clicked_(self, sender):
        self.tray.loop.call_soon_threadsafe(self.tray.on_click, sender.tag())


class Tray:
    # TrayIcon as a status item in the menu bar.
    def __init__(self, tray):
        self.tray = tray
        self.item = None
        self.images = {}

    async def start(self):
        global _tray
        await _on_main(self.tray.loop, self._create)
        _tray = self.tray
        app.log("Tray icon registered")

    def _create(self):
        self.controller = MenuController.alloc().init()
        self.controller.tray = self.tray
        menu = AppKit.NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        menu.setDelegate_(self.controller)
        self.item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        self.item.setMenu_(menu)
        self._update()

    def _image(self, name):
        if name not in self.images:
            image = AppKit.NSImage.alloc().initWithContentsOfFile_(os.path.join(app.ASSETS_DIR, f"{name}.png"))
            image.setSize_((STATUS_ICON_POINTS, STATUS_ICON_POINTS))
            self.images[name] = image
        return self.images[name]

    def _update(self):
        if self.item:
            self.item.button().setImage_(self._image(self.tray.icon_name))
            self.item.button().setToolTip_(self.tray.hint)

    def icon_changed(self):
        AppHelper.callAfter(self._update)

    def hint_changed(self):
        AppHelper.callAfter(self._update)

    def menu_changed(self):
        pass

    async def edit_settings(self, values):
        loop = self.tray.loop
        future = loop.create_future()
        AppHelper.callAfter(show_settings, values, lambda result: loop.call_soon_threadsafe(future.set_result, result))
        return await future

    def stop(self):
        def remove():
            if self.item:
                AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(self.item)
                self.item = None

        AppHelper.callAfter(remove)


def _label(text, secondary=False):
    label = AppKit.NSTextField.labelWithString_(text)
    if secondary:
        label.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        label.setFont_(AppKit.NSFont.systemFontOfSize_(AppKit.NSFont.smallSystemFontSize()))
    return label


def _popup(titles, selected):
    popup = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(((0, 0), (FIELD_WIDTH, 26)), False)
    popup.addItemsWithTitles_(titles)
    popup.selectItemAtIndex_(selected)
    return popup


class SettingsWindow(Foundation.NSObject):
    # The same fields as on Linux and Windows, as a native AppKit window.

    @objc.python_method
    def build(self, current, done):
        texts = current["texts"]
        self.texts = texts
        self.done = done
        self.models = list(current.get("models") or [])
        model = current.get("model") or (self.models[0] if self.models else "")
        if model not in self.models:
            self.models.insert(0, model)
        self.languages = current["languages"]
        self.hotkeys = current["hotkeys"]
        language_ids = [language for language, _name in self.languages]
        hotkey_ids = [hotkey for hotkey, _name in self.hotkeys]

        self.key_field = AppKit.NSSecureTextField.alloc().initWithFrame_(((0, 0), (FIELD_WIDTH, 22)))
        self.key_field.setStringValue_(current.get("key", ""))
        self.model_popup = _popup(self.models, self.models.index(model))
        self.language_popup = _popup(
            [name for _language, name in self.languages],
            language_ids.index(current["language"]) if current.get("language") in language_ids else 0,
        )
        self.hotkey_popup = _popup(
            [name for _hotkey, name in self.hotkeys],
            hotkey_ids.index(current["hotkey"]) if current.get("hotkey") in hotkey_ids else 0,
        )
        self.autostart_box = AppKit.NSButton.checkboxWithTitle_target_action_(texts["autostart"], None, None)
        self.autostart_box.setState_(AppKit.NSControlStateValueOn if current.get("autostart") else AppKit.NSControlStateValueOff)
        self.error_label = _label("")
        self.error_label.setTextColor_(AppKit.NSColor.systemRedColor())
        self.error_label.setHidden_(True)
        cancel = AppKit.NSButton.buttonWithTitle_target_action_(texts["cancel"], self, "cancel:")
        cancel.setKeyEquivalent_("\x1b")
        self.save_button = AppKit.NSButton.buttonWithTitle_target_action_(texts["save"], self, "save:")
        self.save_button.setKeyEquivalent_("\r")
        buttons = AppKit.NSStackView.stackViewWithViews_([cancel, self.save_button])

        empty = AppKit.NSGridCell.emptyContentView
        grid = AppKit.NSGridView.gridViewWithViews_([
            [_label(texts["api_key"]), self.key_field],
            [_label(texts["model"]), self.model_popup],
            [_label(texts["speech_language"]), self.language_popup],
            [empty(), _label(texts["stored_in"].format(path=current.get("path", "")), secondary=True)],
            [_label(texts["hotkey"]), self.hotkey_popup],
            [empty(), _label(texts["hotkey_hint"], secondary=True)],
            [empty(), self.autostart_box],
            [empty(), self.error_label],
            [empty(), buttons],
        ])
        grid.setRowSpacing_(10)
        grid.setColumnSpacing_(10)
        grid.setRowAlignment_(AppKit.NSGridRowAlignmentFirstBaseline)
        grid.columnAtIndex_(0).setXPlacement_(AppKit.NSGridCellPlacementTrailing)
        grid.rowAtIndex_(4).setTopPadding_(14)  # «General» starts here
        grid.rowAtIndex_(8).setTopPadding_(10)
        grid.cellForView_(buttons).setXPlacement_(AppKit.NSGridCellPlacementTrailing)
        for control in (self.key_field, self.model_popup, self.language_popup, self.hotkey_popup):
            control.widthAnchor().constraintEqualToConstant_(FIELD_WIDTH).setActive_(True)

        content = AppKit.NSView.alloc().initWithFrame_(((0, 0), (100, 100)))
        grid.setTranslatesAutoresizingMaskIntoConstraints_(False)
        content.addSubview_(grid)
        for anchor, other, constant in (
            (grid.leadingAnchor(), content.leadingAnchor(), 20),
            (grid.topAnchor(), content.topAnchor(), 20),
            (content.trailingAnchor(), grid.trailingAnchor(), 20),
            (content.bottomAnchor(), grid.bottomAnchor(), 20),
        ):
            anchor.constraintEqualToAnchor_constant_(other, constant).setActive_(True)

        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (100, 100)),
            AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self.window.setTitle_(texts["dialog_title"])
        self.window.setReleasedWhenClosed_(False)
        self.window.setDelegate_(self)
        self.window.setContentView_(content)
        self.window.setContentSize_(content.fittingSize())
        self.window.center()
        # Altalk has no Dock icon; without activating it the window would open behind others.
        application = AppKit.NSApp
        if hasattr(application, "activate"):
            application.activate()
        else:
            application.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)
        self.window.makeFirstResponder_(self.key_field)

    def save_(self, sender):
        key = str(self.key_field.stringValue()).strip()
        model = self.models[self.model_popup.indexOfSelectedItem()]
        self.save_button.setEnabled_(False)
        self.save_button.setTitle_(self.texts["checking"])
        # The HTTP check runs off the main thread so the window keeps redrawing.
        threading.Thread(target=self.check, args=(key, model), daemon=True).start()

    @objc.python_method
    def check(self, key, model):
        error = app.check_api_key(key, model)
        AppHelper.callAfter(self.checked, key, model, error)

    @objc.python_method
    def checked(self, key, model, error):
        self.save_button.setEnabled_(True)
        self.save_button.setTitle_(self.texts["save"])
        if error:
            self.error_label.setStringValue_(error)
            self.error_label.setHidden_(False)
            self.window.makeFirstResponder_(self.key_field)
            return
        self.finish({
            "key": key,
            "model": model,
            "language": self.languages[self.language_popup.indexOfSelectedItem()][0],
            "hotkey": self.hotkeys[self.hotkey_popup.indexOfSelectedItem()][0],
            "autostart": self.autostart_box.state() == AppKit.NSControlStateValueOn,
        })

    def cancel_(self, sender):
        self.finish(None)

    def windowWillClose_(self, notification):
        self.finish(None)

    @objc.python_method
    def finish(self, result):
        global _settings
        if self.done is None:
            return  # closing the window after «Save» comes back here
        done, self.done = self.done, None
        self.window.close()
        _settings = None
        done(result)


def show_settings(current, done):
    global _settings
    _settings = SettingsWindow.alloc().init()
    _settings.build(current, done)


def _launch_command():
    # Through LaunchServices, like a double-click, so macOS treats it as the same Altalk.app.
    if getattr(sys, "frozen", False):
        return ["/usr/bin/open", os.path.abspath(os.path.join(os.path.dirname(sys.executable), "..", ".."))]
    return app.app_command()


def _launch_agent():
    try:
        with open(LAUNCH_AGENT_PATH, "rb") as f:
            return plistlib.load(f)
    except (OSError, plistlib.InvalidFileException):
        return None


def autostart_enabled():
    agent = _launch_agent()
    return bool(agent) and not agent.get("Disabled", False)


def set_autostart(enabled):
    # A LaunchAgent rather than SMAppService: that one is unreliable for apps without a Developer ID.
    # Rewritten also when Altalk.app has moved, so that login starts this copy.
    agent = _launch_agent() or {}
    if enabled == autostart_enabled() and (not enabled or agent.get("ProgramArguments") == _launch_command()):
        return
    if enabled:
        os.makedirs(os.path.dirname(LAUNCH_AGENT_PATH), exist_ok=True)
        with open(LAUNCH_AGENT_PATH, "wb") as f:
            plistlib.dump({"Label": BUNDLE_ID, "ProgramArguments": _launch_command(), "RunAtLoad": True}, f)
        app.log(f"Autostart enabled: {LAUNCH_AGENT_PATH}")
    else:
        os.remove(LAUNCH_AGENT_PATH)
        app.log("Autostart disabled")


def acquire_instance_lock():
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        notify("Altalk", app.tr("notify_running"))
        raise SystemExit("Altalk is already running")
    return lock_file
