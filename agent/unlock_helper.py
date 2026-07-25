"""Sudo unlock helper — runs as SYSTEM via a scheduled task.

Windows ignores SendInput keystrokes on the lock screen unless they come
from a SYSTEM/UIAccess context, so the normal agent can't type the PIN
there. This tiny helper listens on loopback only (127.0.0.1:47911 UDP);
the agent forwards unlock requests to it, and because it runs as SYSTEM
its synthetic input reaches the lock screen.

Packet: UTF-8 JSON {"pin": "..."} — that's the entire protocol.
"""

import ctypes
import json
import socket
import time
import traceback

from pynput.keyboard import Controller, Key, KeyCode

HOST, PORT = "127.0.0.1", 47911
LOG = "C:/ProgramData/Sudo/helper.log"
kb = Controller()

_user32 = ctypes.windll.user32


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _vk_press(ch):
    """Press a character via a virtual KEY event (not unicode injection —
    the Windows lock screen silently drops KEYEVENTF_UNICODE input)."""
    res = _user32.VkKeyScanW(ord(ch))
    if res == -1:
        return  # character not mappable on the current layout
    vk, state = res & 0xFF, (res >> 8) & 0xFF
    if state & 1:
        kb.press(Key.shift)
    kb.press(KeyCode.from_vk(vk))
    kb.release(KeyCode.from_vk(vk))
    if state & 1:
        kb.release(Key.shift)


def type_pin(text):
    for ch in text:
        _vk_press(ch)


NUMPAD = {str(i): 0x60 + i for i in range(10)}  # VK_NUMPAD0..9


def _numlock_on():
    return (_user32.GetKeyState(0x90) & 1) == 1  # VK_NUMLOCK


def type_pin(text):
    # Digits go through the numpad: those virtual keys are independent of
    # the keyboard layout (AZERTY digit-row needs Shift on the session
    # layout but not on the lock screen's — a classic wrong-PIN cause).
    was_on = _numlock_on()
    if not was_on:
        kb.press(Key.num_lock)
        kb.release(Key.num_lock)
    try:
        for ch in text:
            if ch in NUMPAD:
                vk = NUMPAD[ch]
                kb.press(KeyCode.from_vk(vk))
                kb.release(KeyCode.from_vk(vk))
            else:
                _vk_press(ch)
    finally:
        if not was_on:
            kb.press(Key.num_lock)
            kb.release(Key.num_lock)


def unlock(pin: str):
    kb.press(Key.enter)
    kb.release(Key.enter)
    log("enter sent (curtain)")
    time.sleep(2.0)
    # The sign-in field is not always focused after the curtain lifts —
    # click where it sits (center, ~3/4 down) before typing.
    try:
        import ctypes.wintypes as wt
        sw = _user32.GetSystemMetrics(0)
        sh = _user32.GetSystemMetrics(1)
        pt = wt.POINT(int(sw / 2), int(sh * 0.75))
        _user32.SetCursorPos(pt.x, pt.y)
        _user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
        _user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
        log(f"clicked {pt.x},{pt.y}")
    except Exception:
        log("click failed:\n" + traceback.format_exc())
    time.sleep(0.3)
    type_pin(pin)
    log(f"typed {len(pin)} chars")
    kb.press(Key.enter)
    kb.release(Key.enter)
    log("final enter sent")


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((HOST, PORT))
    log(f"listening on {HOST}:{PORT}")
    while True:
        data, _ = sock.recvfrom(4096)
        log(f"packet: {len(data)} bytes")
        try:
            pin = str(json.loads(data.decode("utf-8"))["pin"])
        except Exception:
            log("bad packet")
            continue
        if pin:
            if pin == "PROBE":
                # Diagnostic: open the on-screen keyboard (Win+Ctrl+O) —
                # a visible effect proving input reaches the lock screen.
                try:
                    kb.press(Key.cmd)
                    kb.press(Key.ctrl)
                    kb.press(KeyCode.from_char("o"))
                    time.sleep(0.3)
                    kb.release(KeyCode.from_char("o"))
                    kb.release(Key.ctrl)
                    kb.release(Key.cmd)
                    log("PROBE: Win+Ctrl+O sent")
                except Exception:
                    log("probe error:\n" + traceback.format_exc())
                continue
            try:
                unlock(pin)
            except Exception:
                log("unlock error:\n" + traceback.format_exc())


if __name__ == "__main__":
    main()
