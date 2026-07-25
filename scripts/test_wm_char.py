"""Probe: can we type into the lock screen PIN field by posting WM_ key
messages directly to LockApp's window (bypasses SendInput filtering)?

Locks the PC, then posts Enter + digits + Enter to every LockApp CoreWindow.
Watch the lock screen: dots in the field = we win.
"""

import ctypes
import ctypes.wintypes as wt
import time

import psutil

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_KEYDOWN, WM_CHAR, WM_KEYUP = 0x0100, 0x0102, 0x0101
VK_RETURN = 0x0D


def lockapp_windows():
    pids = {p.pid for p in psutil.process_iter(["name"])
            if p.info["name"] and p.info["name"].lower() == "lockapp.exe"}
    found = []
    cls = ctypes.create_unicode_buffer(256)

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd):
            user32.GetClassNameW(hwnd, cls, 256)
            found.append((hwnd, cls.value))
        return True

    user32.EnumWindows(cb, 0)
    return found


def vk_of(ch):
    res = user32.VkKeyScanW(ord(ch))
    return res & 0xFF if res != -1 else None


def key_lparam(vk, down=True):
    sc = user32.MapVirtualKeyW(vk, 0)
    lp = 1 | (sc << 16)
    if not down:
        lp |= 0xC0000000  # previous state 1 + transition 1
    return lp


def post_key(hwnd, ch):
    if ch == "\n":
        user32.PostMessageW(hwnd, WM_KEYDOWN, VK_RETURN, key_lparam(VK_RETURN))
        user32.PostMessageW(hwnd, WM_CHAR, VK_RETURN, key_lparam(VK_RETURN))
        user32.PostMessageW(hwnd, WM_KEYUP, VK_RETURN, key_lparam(VK_RETURN, False))
        return
    vk = vk_of(ch)
    if vk is not None:
        user32.PostMessageW(hwnd, WM_KEYDOWN, vk, key_lparam(vk))
    user32.PostMessageW(hwnd, WM_CHAR, ord(ch), 1)
    if vk is not None:
        user32.PostMessageW(hwnd, WM_KEYUP, vk, key_lparam(vk, False))


def main():
    ctypes.windll.wtsapi32  # noqa - ensure present
    import subprocess
    subprocess.run("rundll32.exe user32.dll,LockWorkStation", shell=True)
    time.sleep(4)
    wins = lockapp_windows()
    print("LockApp windows:", wins)
    for hwnd, cls in wins:
        print(f"posting to {hwnd} ({cls})")
        post_key(hwnd, "\n")
        time.sleep(2.5)
        for c in "1234":
            post_key(hwnd, c)
            time.sleep(0.05)
        time.sleep(0.5)
        post_key(hwnd, "\n")


if __name__ == "__main__":
    main()
