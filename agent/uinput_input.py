"""Mouse and keyboard through a virtual input device (/dev/uinput).

For Wayland desktops whose portal shares the screen but offers no remote
control (Sway and the other wlroots desktops, Hyprland, niri): the kernel
sees a USB-like mouse and keyboard, so every compositor takes their events.
The installer lets the logged-in user open /dev/uinput (a udev uaccess
rule, agent/linux/install.sh). Pure Python (ioctl), no evdev package.

Text goes through `wtype` when it is installed (wlroots' virtual keyboard
types any character in any layout); otherwise through key codes, which
assumes a US layout and covers ASCII only.
"""

import fcntl
import os
import shutil
import struct
import subprocess
import time

EV_SYN, EV_KEY, EV_REL = 0, 1, 2
SYN_REPORT = 0
REL_X, REL_Y, REL_HWHEEL, REL_WHEEL = 0, 1, 6, 8
BTN = {"left": 0x110, "right": 0x111, "middle": 0x112}

# _IOW('U', n, int) / _IO('U', n) / _IOW('U', 3, struct uinput_setup)
UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_RELBIT = 0x40045564, 0x40045565, 0x40045566
UI_DEV_SETUP, UI_DEV_CREATE, UI_DEV_DESTROY = 0x405C5503, 0x5501, 0x5502
BUS_VIRTUAL = 0x06
EVENT = struct.Struct("llHHi")  # struct input_event (timeval, type, code, value)

# linux/input-event-codes.h for the agent's key names (agent.SPECIAL_KEYS).
KEYS = {
    "esc": 1, "escape": 1, "backspace": 14, "tab": 15, "enter": 28,
    "ctrl": 29, "shift": 42, "alt": 56, "space": 57, "home": 102, "up": 103,
    "pageup": 104, "left": 105, "right": 106, "end": 107, "down": 108,
    "pagedown": 109, "delete": 111, "cmd": 125, "win": 125,
    "media_volume_mute": 113, "media_volume_down": 114,
    "media_volume_up": 115, "media_next": 163, "media_play_pause": 164,
    "media_previous": 165,
    **{f"f{i}": 58 + i for i in range(1, 11)}, "f11": 87, "f12": 88,
}
SHIFT = 42

# US layout: character -> (key code, shift).
_ROWS = [("1234567890-=", 2), ("qwertyuiop[]", 16), ("asdfghjkl;'", 30),
         ("zxcvbnm,./", 44)]
_SHIFTED = [("!@#$%^&*()_+", 2), ("QWERTYUIOP{}", 16), ('ASDFGHJKL:"', 30),
            ("ZXCVBNM<>?", 44)]
CHARS = {" ": (57, False), "\n": (28, False), "\t": (15, False),
         "`": (41, False), "~": (41, True), "\\": (43, False), "|": (43, True)}
for _row, _first in _ROWS:
    CHARS.update({c: (_first + i, False) for i, c in enumerate(_row)})
for _row, _first in _SHIFTED:
    CHARS.update({c: (_first + i, True) for i, c in enumerate(_row)})


class UinputError(RuntimeError):
    pass


def available():
    return os.access("/dev/uinput", os.W_OK)


class VirtualInput:
    """One virtual mouse + keyboard; created on first use, gone on close()."""

    def __init__(self):
        self._fd = None
        self._wtype = shutil.which("wtype")

    def _open(self):
        if self._fd is not None:
            return self._fd
        try:
            fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            raise UinputError(
                "This desktop gives no remote control through its portal, and "
                f"the virtual mouse/keyboard can't be created ({e.strerror}): "
                "re-run agent/linux/install.sh, then log out and in.") from e
        try:
            for ev in (EV_SYN, EV_KEY, EV_REL):
                fcntl.ioctl(fd, UI_SET_EVBIT, ev)
            for code in list(range(1, 249)) + list(BTN.values()):
                fcntl.ioctl(fd, UI_SET_KEYBIT, code)
            for rel in (REL_X, REL_Y, REL_HWHEEL, REL_WHEEL):
                fcntl.ioctl(fd, UI_SET_RELBIT, rel)
            setup = struct.pack("HHHH80sI", BUS_VIRTUAL, 0x5344, 0x0001, 1,
                                b"Sudo remote input", 0)
            fcntl.ioctl(fd, UI_DEV_SETUP, setup)
            fcntl.ioctl(fd, UI_DEV_CREATE)
        except OSError:
            os.close(fd)
            raise
        self._fd = fd
        # The compositor needs a moment to pick up a new device before its
        # first events count.
        time.sleep(0.2)
        return fd

    def _emit(self, events):
        fd = self._open()
        now = time.time()
        sec, usec = int(now), int((now % 1) * 1_000_000)
        data = b"".join(EVENT.pack(sec, usec, t, c, v) for t, c, v in events)
        os.write(fd, data + EVENT.pack(sec, usec, EV_SYN, SYN_REPORT, 0))

    def move_rel(self, dx_px, dy_px):
        dx, dy = round(dx_px), round(dy_px)
        if dx or dy:
            self._emit([(EV_REL, REL_X, dx), (EV_REL, REL_Y, dy)])

    def button(self, name, pressed):
        self._emit([(EV_KEY, BTN.get(name, BTN["left"]), 1 if pressed else 0)])

    def scroll(self, dx, dy):
        """pynput convention: dy > 0 = up, dx > 0 = right (as the wheel)."""
        events = []
        if dy:
            events.append((EV_REL, REL_WHEEL, int(dy)))
        if dx:
            events.append((EV_REL, REL_HWHEEL, int(dx)))
        if events:
            self._emit(events)

    def tap(self, code, shift=False):
        if shift:
            self._emit([(EV_KEY, SHIFT, 1)])
        self._emit([(EV_KEY, code, 1)])
        self._emit([(EV_KEY, code, 0)])
        if shift:
            self._emit([(EV_KEY, SHIFT, 0)])

    def key(self, name):
        if len(name) == 1:
            self.text(name)
            return
        code = KEYS.get(name.lower())
        if code is None:
            raise ValueError(f"unknown key: {name}")
        self.tap(code)

    def text(self, text):
        if self._wtype:
            try:
                subprocess.run([self._wtype, "--", text], check=True, timeout=10,
                               capture_output=True)
                return
            except (OSError, subprocess.SubprocessError):
                self._wtype = None  # no virtual keyboard here: key codes
        missing = sorted({c for c in text if c not in CHARS})
        for ch in text:
            if ch in CHARS:
                self.tap(*CHARS[ch])
        if missing:
            raise ValueError("can't type " + "".join(missing)
                             + " here (install wtype for every character)")

    def close(self):
        if self._fd is not None:
            try:
                fcntl.ioctl(self._fd, UI_DEV_DESTROY)
            except OSError:
                pass
            os.close(self._fd)
            self._fd = None
