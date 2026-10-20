# CI check of the macOS hotkey: posts Left Option held for 1.5 s with a mouse click and a scroll in
# between (the recording goes on), then Left Option with the A key pressed in between (it is cancelled),
# as if from the keyboard and the mouse. The app log shows what Altalk made of them.
import time

import Quartz

OPTION, A = 58, 0
OPTION_DOWN = Quartz.kCGEventFlagMaskAlternate | 0x20 | Quartz.kCGEventFlagMaskNonCoalesced  # 0x20: the left one
OPTION_UP = Quartz.kCGEventFlagMaskNonCoalesced


def post_option(flags):
    event = Quartz.CGEventCreateKeyboardEvent(None, OPTION, True)
    Quartz.CGEventSetType(event, Quartz.kCGEventFlagsChanged)
    Quartz.CGEventSetFlags(event, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def post_key(keycode):
    for down in (True, False):
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateKeyboardEvent(None, keycode, down))


def post_click_and_scroll():
    for event_type in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        click = Quartz.CGEventCreateMouseEvent(None, event_type, (400, 400), Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, click)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, -3))


post_option(OPTION_DOWN)
time.sleep(0.5)
post_click_and_scroll()
time.sleep(1)
post_option(OPTION_UP)
time.sleep(1)
post_option(OPTION_DOWN)
time.sleep(0.5)
post_key(A)
time.sleep(0.5)
post_option(OPTION_UP)
