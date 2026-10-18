# CI check of the macOS hotkey: posts Left Option held for 1.5 s, then Left Option with the A key
# pressed in between, as if from the keyboard. The app log shows what Altalk made of them.
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


post_option(OPTION_DOWN)
time.sleep(1.5)
post_option(OPTION_UP)
time.sleep(1)
post_option(OPTION_DOWN)
time.sleep(0.5)
post_key(A)
time.sleep(0.5)
post_option(OPTION_UP)
