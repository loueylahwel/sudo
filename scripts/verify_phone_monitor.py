"""Standalone checks for the phone-as-a-second-monitor modules.

Always runs ctypes struct-size self-tests. Then, depending on the machine:

  * Driver INSTALLED: enables the virtual monitor, moves a tkinter window
    this script creates onto it, taps that window with injected touch, and
    checks the window got the click, the PC cursor did not move, and the
    window that had focus before the tap is foreground again. Then disables
    and checks the window is back on the primary monitor and no Parsec
    display is left plugged in.

  * Driver MISSING: runs the touch-injection tests against this script's own
    topmost window on the primary monitor: a tap (lands, cursor restored,
    focus handed back to a second "PC" window this script owns), a long
    press held past Windows' ~0.5s injected-contact timeout (must not drop
    touch), and a cancel (must not click).

SAFETY: every injected touch lands on a window THIS script created and made
topmost, never on the user's apps. The cursor position and the window the
user had focused are saved at start and restored at the end. Each live test
is under two seconds. Run it with the agent venv:

    <venv>/python.exe scripts/verify_phone_monitor.py
"""

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

import touch as touchmod        # noqa: E402
import vdisplay                 # noqa: E402
from phone_monitor import PhoneMonitor  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
_results = []


def check(name, ok, detail=""):
    _results.append(ok)
    tag = PASS if ok else FAIL
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))
    return ok


# ---------------------------------------------------------------- win helpers

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_u32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_u32.GetCursorPos.restype = wintypes.BOOL
_u32.GetForegroundWindow.restype = wintypes.HWND
_u32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_u32.GetAncestor.restype = wintypes.HWND
_u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_u32.GetWindowRect.restype = wintypes.BOOL
_u32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
_u32.SetCursorPos.restype = wintypes.BOOL
_GA_ROOT = 2


def set_cursor(x, y):
    _u32.SetCursorPos(int(x), int(y))


def cursor_pos():
    pt = wintypes.POINT()
    _u32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)


def top_level(child_hwnd):
    return _u32.GetAncestor(child_hwnd, _GA_ROOT)


def window_rect(hwnd):
    r = wintypes.RECT()
    _u32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def pump(root, seconds):
    """Run the tkinter event loop for `seconds` so injected input (which the
    OS delivers as window messages) is actually processed."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.01)


# ---------------------------------------------------------------- struct tests

def test_structs():
    print("ctypes struct-size self-tests (64-bit):")
    bits = ctypes.sizeof(ctypes.c_void_p) * 8
    if bits != 64:
        check("running as 64-bit", False, f"{bits}-bit interpreter")
        return
    for cls, expected in vdisplay.STRUCT_SIZES_64.items():
        got = ctypes.sizeof(cls)
        check(f"vdisplay.{cls.__name__} == {expected}", got == expected,
              "" if got == expected else f"got {got}")
    for cls, expected in touchmod.STRUCT_SIZES_64.items():
        got = ctypes.sizeof(cls)
        check(f"touch.{cls.__name__} == {expected}", got == expected,
              "" if got == expected else f"got {got}")


# ---------------------------------------------------------------- tk window

def make_window(title, x, y, w, h, hits=None):
    import tkinter as tk
    root = tk.Toplevel() if tk._default_root else tk.Tk()
    root.title(title)
    root.geometry(f"{w}x{h}+{x}+{y}")
    root.configure(bg="#102030")
    root.attributes("-topmost", True)
    if hits is not None:
        # Touch on a non-touch-aware window is promoted to mouse messages,
        # which Tk surfaces as Button / ButtonRelease events (a long press
        # may come through as the right button).
        root.bind("<ButtonPress>",
                  lambda e: hits.append(("down", e.x_root, e.y_root)))
        root.bind("<ButtonRelease>",
                  lambda e: hits.append(("up", e.x_root, e.y_root)))
    root.update()
    root.lift()
    root.focus_force()
    root.update()
    return root


def focus(win):
    """Make one of this script's windows foreground (they belong to this
    process, which is foreground already, so no lock to get past)."""
    touchmod._attach_set_foreground(touchmod._api(),
                                    top_level(win.winfo_id()))
    win.update()


def settle(ti, windows, seconds):
    """Pump Tk while the injector's worker finishes a gesture: the promoted
    click (and the activation the focus restore has to undo) is only
    processed when the target's message loop runs — as a real app's does."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        for w in windows:
            w.update()
        time.sleep(0.005)
    while ti.gesture_in_progress():
        for w in windows:
            w.update()
        time.sleep(0.005)


# ---------------------------------------------------------------- missing path

def test_touch_on_own_window():
    print("touch injection against this script's own windows (primary monitor):")
    prim = vdisplay.primary_monitor()
    if prim is None:
        check("found a primary monitor", False)
        return
    pw, ph = prim["right"] - prim["left"], prim["bottom"] - prim["top"]
    w, h = 460, 340
    x = prim["left"] + (pw - w) // 2
    y = prim["top"] + (ph - h) // 2
    hits = []
    # A fixed anchor far from the tap point, so "restored" vs "left at the
    # tap" is unambiguous and independent of where the user's mouse happens
    # to be. The test owns the cursor briefly; main() puts it back.
    anchor = (prim["left"] + 5, prim["top"] + 5)
    # Stand-in for "the app the PC user was typing in": it holds focus
    # before each tap and must hold it again afterwards.
    pc_win = make_window("Sudo verify — PC window", prim["left"] + 40,
                         prim["top"] + 40, 300, 160)
    root = make_window("Sudo verify — touch target", x, y, w, h, hits)
    wins = (pc_win, root)
    ti = touchmod.TouchInjector()
    try:
        pump(root, 0.2)
        hwnd = top_level(root.winfo_id())
        pc_hwnd = top_level(pc_win.winfo_id())
        l, t, r, b = window_rect(hwnd)
        cx, cy = (l + r) // 2, (t + b) // 2

        avail = ti.available()
        check("InitializeTouchInjection succeeded", avail,
              "" if avail else "will exercise the mouse fallback instead")

        # ---- 1. a tap ----
        root.lift()  # re-assert topmost so the promoted click hits our window
        focus(pc_win)
        pump(root, 0.05)
        set_cursor(*anchor)
        time.sleep(0.03)
        ti.touch("down", 0, cx, cy)
        pump(root, 0.12)
        ti.touch("up", 0, cx, cy)
        settle(ti, wins, 0.15)
        now = cursor_pos()
        fg = _u32.GetForegroundWindow()
        pump(root, 0.1)

        check("the window received the injected tap",
              any(a == "down" for a, *_ in hits)
              and any(a == "up" for a, *_ in hits), f"events={hits}")
        if hits:
            _, hx, hy = hits[-1]
            near = abs(hx - cx) <= 6 and abs(hy - cy) <= 6
            check("tap landed at the window center", near,
                  f"hit=({hx},{hy}) center=({cx},{cy})")

        # The injector captures the real pre-tap cursor at touch-down and
        # restores it there. The anchor only parks the cursor away from the
        # tap first; the PC user (live on this machine) may nudge the mouse
        # in the millisecond capture window, so the ground truth is the
        # injector's own captured value, not the intended anchor. Two
        # invariants, both robust to that nudging:
        #   1. it captured a sane position, well away from the tap point, and
        #   2. the cursor ended at that captured position — proving save +
        #      restore ran and the cursor was not left at the tap.
        saved = ti.last_saved_cursor
        captured_off_tap = saved is not None and (
            abs(saved[0] - cx) > 50 or abs(saved[1] - cy) > 50)
        check("injector captured the pre-gesture cursor (off the tap point)",
              captured_off_tap, f"anchor={anchor} captured={saved} "
              f"tap=({cx},{cy})")
        off_touch = abs(now[0] - cx) > 6 or abs(now[1] - cy) > 6
        at_saved = saved is not None and abs(now[0] - saved[0]) <= 8 \
            and abs(now[1] - saved[1]) <= 8
        check("PC cursor restored to where it was (not left at the tap)",
              off_touch and at_saved,
              f"captured={saved} now={now} tap=({cx},{cy})")
        check("focus returned to the PC window after the tap",
              fg == pc_hwnd, f"fg={fg} pc={pc_hwnd} target={hwnd}")

        if not avail:
            return  # the rest is about touch-injection specifics

        # ---- 2. a long press, past the ~0.5s injected-contact timeout ----
        hits.clear()
        focus(pc_win)
        ti.touch("down", 0, cx, cy)
        pump(root, 0.9)  # no phone frames: the worker's hold frames only
        ti.touch("up", 0, cx, cy)
        settle(ti, wins, 0.15)
        pump(root, 0.1)
        check("a 0.9s hold keeps touch injection alive",
              ti._touch_ok and ti._failures == 0,
              f"touch_ok={ti._touch_ok} failures={ti._failures}")
        check("the held press was delivered", bool(hits), f"events={hits}")

        # ---- 3. a cancel must not click ----
        hits.clear()
        focus(pc_win)
        ti.touch("down", 0, cx, cy)
        pump(root, 0.1)
        ti.touch("cancel", 0)  # no coordinates: lifts where it was
        settle(ti, wins, 0.15)
        pump(root, 0.15)
        check("a cancelled touch delivers no click",
              not any(a == "up" for a, *_ in hits) and ti._failures == 0,
              f"events={hits} failures={ti._failures}")

        # ---- 4. typing reaches the target only; focus comes back ----
        # (Tk runs both windows on this one thread, so keep pumping while
        # the worker types: a real app has its own thread for that.)
        import tkinter as tk
        entry_t, entry_p = tk.Entry(root), tk.Entry(pc_win)
        entry_t.pack()
        entry_p.pack()
        focus(root)
        entry_t.focus_set()
        focus(pc_win)
        entry_p.focus_set()
        pump(root, 0.05)
        ti.type_text("Sudo é1", hwnd=hwnd)
        ti.press_key("backspace", hwnd=hwnd)
        pump(root, 0.5)
        typed, stray = entry_t.get(), entry_p.get()
        fg = _u32.GetForegroundWindow()
        check("typed text reached the target window only",
              typed == "Sudo é" and stray == "",
              f"target={typed!r} pc_window={stray!r}")
        check("focus returned to the PC window after typing",
              fg == pc_hwnd, f"fg={fg} pc={pc_hwnd}")
        ti.type_text("x", hwnd=None)  # no target: must type nowhere
        pump(root, 0.15)
        check("text without a target window is dropped",
              entry_p.get() == "" and entry_t.get() == "Sudo é",
              f"target={entry_t.get()!r} pc_window={entry_p.get()!r}")
    finally:
        ti.stop()
        for wnd in (root, pc_win):
            try:
                wnd.destroy()
            except Exception:  # noqa: BLE001 - cleanup must never mask results
                pass


# ---------------------------------------------------------------- installed path

def test_with_driver():
    print("virtual monitor end-to-end (driver installed):")
    import tkinter as tk
    pmon = PhoneMonitor()
    prim = vdisplay.primary_monitor()
    # A stand-in for "the app the PC user was using": stays on the primary
    # monitor and should regain focus after the phone tap.
    pc_win = make_window("Sudo verify — PC window", prim["left"] + 60,
                         prim["top"] + 60, 360, 240)
    target = None
    try:
        try:
            status = pmon.enable(orientation="landscape")
        except vdisplay.DisplayError as e:
            status = {"driver": f"enable failed: {e}"}
        enabled = status.get("enabled") and status.get("monitor")
        check("enable() plugged in the virtual monitor", bool(enabled),
              f"status={status.get('driver')} monitor={status.get('monitor')}")
        if not enabled:
            return
        try:
            print(f"  (driver minor version {pmon._vdd.version()})")
        except (OSError, vdisplay.DisplayError) as e:
            print(f"  (driver version query failed: {e})")
        rect = pmon.rect()
        idx = pmon.monitor_index()
        check("monitor has a rect and an mss index",
              rect is not None and idx is not None, f"rect={rect} index={idx}")

        # A window to move onto the phone monitor.
        target = make_window("Sudo verify — phone app",
                             prim["left"] + 200, prim["top"] + 200, 420, 300)
        pump(target, 0.1)
        thwnd = top_level(target.winfo_id())
        focus(pc_win)  # the PC user is working elsewhere while the app moves
        pump(target, 0.05)
        pmon.move_window_here(thwnd)
        pump(target, 0.3)
        cx = (window_rect(thwnd)[0] + window_rect(thwnd)[2]) // 2
        cy = (window_rect(thwnd)[1] + window_rect(thwnd)[3]) // 2
        on_phone = rect[0] <= cx < rect[2] and rect[1] <= cy < rect[3]
        check("move_window_here put the window on the phone monitor", on_phone,
              f"center=({cx},{cy}) rect={rect}")

        pc_hwnd = top_level(pc_win.winfo_id())
        check("moving the app left focus with the PC window",
              _u32.GetForegroundWindow() == pc_hwnd,
              f"fg={_u32.GetForegroundWindow()} pc={pc_hwnd}")

        # Give the PC window focus, then tap the phone app.
        focus(pc_win)
        pump(pc_win, 0.1)
        hits = []
        target.bind("<ButtonRelease>",
                    lambda e: hits.append(("up", e.x_root, e.y_root)))
        target.bind("<ButtonPress>",
                    lambda e: hits.append(("down", e.x_root, e.y_root)))
        pmon.touch("down", 0, 0.5, 0.5)
        pump(target, 0.15)
        pmon.touch("up", 0)  # the phone may omit x/y on lift
        settle(pmon._touch, (target, pc_win), 0.15)
        pump(target, 0.2)
        check("the phone app received the injected tap", bool(hits),
              f"events={hits}")

        now = cursor_pos()
        check("PC cursor not left on the phone monitor",
              not (rect[0] <= now[0] < rect[2] and rect[1] <= now[1] < rect[3]),
              f"cursor={now} phone={rect}")

        fg = _u32.GetForegroundWindow()
        check("focus returned to the PC window after the tap",
              fg == pc_hwnd, f"fg={fg} pc={pc_hwnd}")

        # Disable and confirm cleanup.
        pmon.disable()
        pump(target, 0.3)
        tcx = (window_rect(thwnd)[0] + window_rect(thwnd)[2]) // 2
        tcy = (window_rect(thwnd)[1] + window_rect(thwnd)[3]) // 2
        back = (prim["left"] <= tcx < prim["right"]
                and prim["top"] <= tcy < prim["bottom"])
        check("disable() moved the window back to the primary monitor", back,
              f"center=({tcx},{tcy})")
        left = [d for d in vdisplay.parsec_displays() if d["attached"]]
        check("no Parsec display remains plugged in", not left,
              f"attached={[d['device'] for d in left]}")
    finally:
        try:
            pmon.close()
        except Exception as e:  # noqa: BLE001 - cleanup must never mask results
            print(f"  (cleanup: {e})")
        for wnd in (target, pc_win):
            try:
                if wnd is not None:
                    wnd.destroy()
            except tk.TclError:
                pass


# ---------------------------------------------------------------- main

def main():
    print("=" * 60)
    print("phone-monitor verification")
    print(f"  platform : {sys.platform}")
    vdisplay.ensure_dpi_aware()
    installed = vdisplay.is_installed()
    print(f"  driver   : {'installed' if installed else 'missing'} "
          f"({vdisplay.driver_status()})")
    print("=" * 60)

    test_structs()
    print()
    # The user is live on this machine: whatever they had focused and
    # wherever their cursor was goes back exactly as it was.
    user_fg, user_cursor = _u32.GetForegroundWindow(), cursor_pos()
    try:
        if installed:
            test_with_driver()
        else:
            test_touch_on_own_window()
    finally:
        set_cursor(*user_cursor)
        if user_fg:
            touchmod._attach_set_foreground(touchmod._api(), user_fg)

    print()
    total, passed = len(_results), sum(1 for r in _results if r)
    print(f"RESULT: {passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
