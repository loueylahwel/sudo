"""Touch / keyboard injection for "phone as a second monitor".

Phone taps land on the virtual monitor as real Windows touch input, not as
mouse clicks, so the PC user's own cursor never jumps across the screen to
service a tap on the phone. Two things make the illusion hold:

  * the mouse position is saved before a gesture and, if touch-to-mouse
    promotion dragged it onto the phone monitor, put back after the gesture
    ends (a cursor the PC user moved themselves is left alone), and
  * the window the PC user was working in is remembered before the first
    touch and re-focused after touch-up (toggleable; on by default), so a
    tap on the phone doesn't steal the PC user's keyboard focus.

Everything that pokes the touch injection API runs on ONE dedicated thread:
InitializeTouchInjection is per-thread, and InjectTouchInput rejects frames
that arrive from a different thread than the one that owns the contacts.
Requests cross onto that thread through a queue; the agent's event loop
never blocks on an injection.

Windows cancels an injected contact that goes ~0.5s without a frame
(measured: the touch-up of a 0.6s hold fails with ERROR_TIMEOUT), and the
phone sends nothing while a finger rests still. So while any contact is
down the worker re-injects UPDATE "hold" frames every _HOLD_S; a contact
that hears nothing from the phone for _STALE_S (its up was lost with the
connection) is lifted as cancelled.

A mouse fallback (absolute SendInput over the virtual desktop) covers
machines where touch injection is unavailable, so the feature still works
there — just with a visible cursor.

Off Windows every operation is a cheap no-op and available() is False.
"""

import ctypes
import platform
import queue
import threading
import time
from ctypes import wintypes

IS_WINDOWS = platform.system() == "Windows"

# ---- touch injection constants (winuser.h) ----
_MAX_CONTACTS = 2
TOUCH_FEEDBACK_DEFAULT = 0x1
TOUCH_FEEDBACK_INDIRECT = 0x2
TOUCH_FEEDBACK_NONE = 0x3

_PT_TOUCH = 0x2

_POINTER_FLAG_NONE = 0x0
_POINTER_FLAG_NEW = 0x1
_POINTER_FLAG_INRANGE = 0x2
_POINTER_FLAG_INCONTACT = 0x4
_POINTER_FLAG_CANCELED = 0x8000
_POINTER_FLAG_DOWN = 0x10000
_POINTER_FLAG_UPDATE = 0x20000
_POINTER_FLAG_UP = 0x40000

_TOUCH_MASK_CONTACTAREA = 0x1
_TOUCH_MASK_ORIENTATION = 0x2
_TOUCH_MASK_PRESSURE = 0x4

# ---- timing ----
_HOLD_S = 0.08          # re-send held contacts well inside the ~0.5s timeout
_STALE_S = 8.0          # finger "down" with no phone frames: the up was lost
_SETTLE_MIN_S = 0.08    # promotion moves the cursor ~12ms after touch-up
_SETTLE_MAX_S = 0.25    # never fight the PC user's mouse/focus for longer
_MAX_FAILURES = 3       # consecutive failed gestures before the mouse fallback

# ---- SendInput constants ----
_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_HWHEEL = 0x1000
_MOUSEEVENTF_ABSOLUTE = 0x8000
_MOUSEEVENTF_VIRTUALDESK = 0x4000
_WHEEL_DELTA = 120
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_VK_MENU = 0x12  # Alt
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79
_GA_ROOT = 2
_GA_ROOTOWNER = 3

# A handful of virtual-key codes for press_key() (type_text covers the rest
# through Unicode packets).
_VK = {
    "enter": 0x0D, "return": 0x0D, "backspace": 0x08, "tab": 0x09,
    "esc": 0x1B, "escape": 0x1B, "space": 0x20, "delete": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}


# ---------------------------------------------------------------- structs

class POINTER_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerType", wintypes.DWORD), ("pointerId", wintypes.DWORD),
        ("frameId", wintypes.DWORD), ("pointerFlags", wintypes.DWORD),
        ("sourceDevice", wintypes.HANDLE), ("hwndTarget", wintypes.HWND),
        ("ptPixelLocation", wintypes.POINT),
        ("ptHimetricLocation", wintypes.POINT),
        ("ptPixelLocationRaw", wintypes.POINT),
        ("ptHimetricLocationRaw", wintypes.POINT),
        ("dwTime", wintypes.DWORD), ("historyCount", wintypes.DWORD),
        ("InputData", ctypes.c_int32), ("dwKeyStates", wintypes.DWORD),
        ("PerformanceCount", ctypes.c_uint64), ("ButtonChangeType", ctypes.c_int),
    ]


class POINTER_TOUCH_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerInfo", POINTER_INFO), ("touchFlags", wintypes.DWORD),
        ("touchMask", wintypes.DWORD), ("rcContact", wintypes.RECT),
        ("rcContactRaw", wintypes.RECT), ("orientation", wintypes.DWORD),
        ("pressure", wintypes.DWORD),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


STRUCT_SIZES_64 = {
    POINTER_INFO: 96, POINTER_TOUCH_INFO: 144, INPUT: 40,
}

# ---------------------------------------------------------------- dlls

_fns = None


def _api():
    global _fns
    if _fns is not None:
        return _fns
    if not IS_WINDOWS:
        raise RuntimeError("touch injection is only supported on Windows")
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def bind(dll, name, restype, *argtypes):
        fn = getattr(dll, name)
        fn.restype = restype
        fn.argtypes = list(argtypes)
        return fn

    class _F:
        pass

    f = _F()
    f.InitializeTouchInjection = bind(
        u32, "InitializeTouchInjection", wintypes.BOOL, wintypes.UINT,
        wintypes.DWORD)
    f.InjectTouchInput = bind(
        u32, "InjectTouchInput", wintypes.BOOL, wintypes.UINT,
        ctypes.POINTER(POINTER_TOUCH_INFO))
    f.SendInput = bind(u32, "SendInput", wintypes.UINT, wintypes.UINT,
                       ctypes.POINTER(INPUT), ctypes.c_int)
    f.GetCursorPos = bind(u32, "GetCursorPos", wintypes.BOOL,
                          ctypes.POINTER(wintypes.POINT))
    f.SetCursorPos = bind(u32, "SetCursorPos", wintypes.BOOL, ctypes.c_int,
                          ctypes.c_int)
    f.GetForegroundWindow = bind(u32, "GetForegroundWindow", wintypes.HWND)
    f.SetForegroundWindow = bind(u32, "SetForegroundWindow", wintypes.BOOL,
                                 wintypes.HWND)
    f.BringWindowToTop = bind(u32, "BringWindowToTop", wintypes.BOOL,
                              wintypes.HWND)
    f.IsWindow = bind(u32, "IsWindow", wintypes.BOOL, wintypes.HWND)
    f.IsIconic = bind(u32, "IsIconic", wintypes.BOOL, wintypes.HWND)
    f.ShowWindow = bind(u32, "ShowWindow", wintypes.BOOL, wintypes.HWND,
                        ctypes.c_int)
    f.GetWindowThreadProcessId = bind(
        u32, "GetWindowThreadProcessId", wintypes.DWORD, wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD))
    f.AttachThreadInput = bind(u32, "AttachThreadInput", wintypes.BOOL,
                               wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
    f.GetWindowRect = bind(u32, "GetWindowRect", wintypes.BOOL, wintypes.HWND,
                           ctypes.POINTER(wintypes.RECT))
    f.WindowFromPoint = bind(u32, "WindowFromPoint", wintypes.HWND,
                             wintypes.POINT)
    f.GetAncestor = bind(u32, "GetAncestor", wintypes.HWND, wintypes.HWND,
                         wintypes.UINT)
    f.GetSystemMetrics = bind(u32, "GetSystemMetrics", ctypes.c_int,
                              ctypes.c_int)
    f.GetCurrentThreadId = bind(k32, "GetCurrentThreadId", wintypes.DWORD)
    _fns = f
    return f


# ---------------------------------------------------------------- helpers

def _rect_contains(rect, x, y):
    l, t, r, b = rect
    return l <= x < r and t <= y < b


def _attach_set_foreground(f, hwnd):
    """Focus hwnd past Windows' foreground lock, the AttachThreadInput way
    (copied from activate_window in agent.py)."""
    if not hwnd or not f.IsWindow(hwnd):
        return
    if f.IsIconic(hwnd):
        f.ShowWindow(hwnd, 9)  # SW_RESTORE
    fg = f.GetForegroundWindow()
    if fg == hwnd:
        return
    fg_thread = f.GetWindowThreadProcessId(fg, None) if fg else 0
    target_thread = f.GetWindowThreadProcessId(hwnd, None)
    cur = f.GetCurrentThreadId()
    att_fg = att_t = False
    if fg_thread and fg_thread != cur:
        att_fg = bool(f.AttachThreadInput(cur, fg_thread, True))
    if target_thread and target_thread != cur:
        att_t = bool(f.AttachThreadInput(cur, target_thread, True))
    try:
        f.BringWindowToTop(hwnd)
        f.SetForegroundWindow(hwnd)
    finally:
        if att_fg:
            f.AttachThreadInput(cur, fg_thread, False)
        if att_t:
            f.AttachThreadInput(cur, target_thread, False)
    if f.GetForegroundWindow() != hwnd:
        # Still locked out: a window that just activated itself (an app
        # maximized onto the phone screen) holds the foreground lock, and the
        # agent, a background process, may not take it. A tapped Alt key lifts
        # the lock for the next SetForegroundWindow (the same trick
        # AutoHotkey's WinActivate uses); down and up land on different
        # windows, so no app sees a lone Alt and opens its menu bar.
        u32 = ctypes.WinDLL("user32")
        u32.keybd_event(_VK_MENU, 0, 0, 0)
        try:
            f.SetForegroundWindow(hwnd)
        finally:
            u32.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, 0)


def _utf16_units(text):
    """UTF-16 code units of text: SendInput's Unicode packets carry one
    unit each, so emoji etc. go through as their surrogate pair."""
    units = []
    for ch in text:
        code = ord(ch)
        if code <= 0xFFFF:
            units.append(code)
        else:
            code -= 0x10000
            units += [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]
    return units


class TouchInjector:
    """Serializes all touch/keyboard injection onto one worker thread.

    Public methods are called from the agent's event loop (or any thread);
    they enqueue work and return without waiting. The worker owns every
    Win32 injection call. `monitor_rect` is a callable returning the phone
    monitor's pixel rect (l, t, r, b) so the focus/cursor checks always
    track a monitor that may be re-staged; it may return None when no phone
    monitor exists.
    """

    def __init__(self, monitor_rect=None, return_focus=True):
        self._monitor_rect = monitor_rect or (lambda: None)
        self.return_focus = return_focus
        self._q = queue.Queue()
        self._thread = None
        self._lock = threading.Lock()  # start/stop lifecycle only
        self._available = None
        self._touch_ok = False
        # Per-gesture state, owned by the worker thread.
        self._active = {}          # pointer_id -> last (x, y) pixel
        self._last_point = None    # latest contact position (the "tap")
        self._last_inject = 0.0    # monotonic time of the last touch frame
        self._last_input = 0.0     # monotonic time of the last phone frame
        self._saved_cursor = None  # cursor to put back after the gesture
        self._saved_fg = None      # window to re-focus after the gesture
        self._mouse_down = False   # fallback: left button held for a drag
        self._settling = False     # post-gesture cursor/focus restore running
        self._failures = 0         # consecutive failed injections
        # Last cursor position captured at a gesture's first contact-down,
        # i.e. the point the injector restores the cursor to. Left in place
        # after the gesture for diagnostics (the verify script reads it).
        self.last_saved_cursor = None
        # Top-level window under the latest gesture's first contact: where
        # the phone user is looking, so typed text goes there.
        self.last_touched = None

    # ---- lifecycle ----

    def start(self):
        if not IS_WINDOWS:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, name="touch-injector", daemon=True)
            self._thread.start()

    def stop(self):
        """Lifts any contact still down (cancelled, so no stray click) and
        ends the worker. A later touch() starts a fresh one."""
        with self._lock:
            t, self._thread = self._thread, None
        if t is not None:
            self._q.put(("_quit", ()))
            t.join(timeout=2)

    def available(self):
        """True when touch injection initialised on this machine. Blocks
        briefly on the first call until the worker has tried to init."""
        if not IS_WINDOWS:
            return False
        self.start()
        for _ in range(50):
            if self._available is not None:
                break
            time.sleep(0.01)
        return bool(self._available)

    def gesture_in_progress(self):
        """True while a contact is down or its aftermath (cursor/focus
        restore) is still running — the cursor fence must leave the cursor
        alone during an active phone gesture."""
        return bool(self._active) or self._mouse_down or self._settling

    # ---- enqueue (any thread) ----

    def touch(self, action, pointer_id, x=None, y=None):
        """Queue one touch frame at pixel (x, y). action is down/move/up/
        cancel; pointer_id picks the contact (0 or 1). x/y may be None for
        up/cancel: the contact lifts where it last was."""
        if not IS_WINDOWS:
            return
        self.start()
        pt = None if x is None or y is None else (int(x), int(y))
        self._q.put(("touch", (str(action), int(pointer_id), pt)))

    def cancel_all(self):
        """Lift every contact as cancelled (client gone, monitor going)."""
        if IS_WINDOWS and self._thread is not None:
            self._q.put(("cancel_all", ()))

    def type_text(self, text, hwnd=None):
        """Type text into hwnd (focused briefly). Without a window nothing
        is typed: the foreground window is the PC user's, never a target."""
        if not IS_WINDOWS:
            return
        self.start()
        self._q.put(("type", (str(text), hwnd)))

    def press_key(self, name, hwnd=None):
        if not IS_WINDOWS:
            return
        self.start()
        self._q.put(("key", (str(name), hwnd)))

    def wheel(self, x, y, dx, dy):
        if not IS_WINDOWS:
            return
        self.start()
        self._q.put(("wheel", (int(x), int(y), float(dx), float(dy))))

    # ---- worker thread ----

    def _run(self):
        try:
            f = _api()
            self._touch_ok = bool(
                f.InitializeTouchInjection(_MAX_CONTACTS, TOUCH_FEEDBACK_NONE))
        except OSError:
            self._touch_ok = False
        self._available = self._touch_ok
        while True:
            timeout = None
            if self._active or self._mouse_down:
                now = time.monotonic()
                if now - self._last_input > _STALE_S:
                    print("[touch] no phone input mid-gesture; lifting contacts")
                    self._cancel_all()
                    continue
                if self._active:
                    due = self._last_inject + _HOLD_S - now
                    if due <= 0:
                        self._frame()  # hold: keep resting fingers alive
                        continue
                    timeout = due
                else:
                    timeout = _HOLD_S
            try:
                op, args = self._q.get(timeout=timeout)
            except queue.Empty:
                continue
            if op == "_quit":
                self._cancel_all()
                return
            try:
                if op == "touch":
                    self._do_touch(*args)
                elif op == "cancel_all":
                    self._cancel_all()
                elif op == "type":
                    self._do_type(*args)
                elif op == "key":
                    self._do_key(*args)
                elif op == "wheel":
                    self._do_wheel(*args)
            except Exception as e:
                # Never let one bad request kill the worker: a dead worker
                # means no phone input at all until the agent restarts.
                print(f"[touch] {op} failed: {e}")

    def _cursor_dragged(self, rect, x, y):
        """True when the cursor sits where touch promotion (or our own
        fallback) put it — on the phone monitor or at the touch point —
        rather than somewhere the PC user moved it themselves."""
        if rect is not None and _rect_contains(rect, x, y):
            return True
        tap = self._last_point
        return tap is not None and abs(x - tap[0]) <= 2 and abs(y - tap[1]) <= 2

    def _begin_gesture(self, pt):
        """Remember the cursor and the PC user's window at the very first
        contact-down of a gesture. The foreground window is captured only
        when it is NOT on the phone monitor, so returning focus restores the
        PC user's app, never a window living on the phone."""
        f = _api()
        self._last_point = pt
        self._saved_cursor = None
        cur = wintypes.POINT()
        if f.GetCursorPos(ctypes.byref(cur)) and \
                not self._cursor_dragged(self._monitor_rect(), cur.x, cur.y):
            self._saved_cursor = (cur.x, cur.y)
            self.last_saved_cursor = self._saved_cursor
        self._saved_fg = None
        fg = f.GetForegroundWindow()
        if fg and f.IsWindow(fg) and not self._window_on_phone(fg):
            self._saved_fg = fg
        hit = f.WindowFromPoint(wintypes.POINT(*pt))
        if hit:
            self.last_touched = f.GetAncestor(hit, _GA_ROOT) or hit

    def _window_on_phone(self, hwnd):
        rect = self._monitor_rect()
        if rect is None:
            return False
        r = wintypes.RECT()
        if not _api().GetWindowRect(hwnd, ctypes.byref(r)):
            return False
        cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
        return _rect_contains(rect, cx, cy)

    def _end_gesture(self):
        """After the last contact lifts: put back the cursor the PC user
        left behind and, when enabled, hand focus back to their window.

        Both are undone by the system AFTER the inject returns: touch-to-
        mouse promotion drags the cursor to the tap point ~12ms later
        (measured) and activates the tapped window with it, so a single
        restore right after touch-up loses the race. Re-check every ~12ms,
        fixing only what the tap changed (a cursor the PC user moved off the
        phone monitor is theirs and left alone), and stop once the promotion
        has clearly passed (>=80ms) and both held for two ticks — bounded so
        it never fights the PC user for more than ~250ms. Touch-aware apps
        like Chromium/Discord don't promote, so this is mostly for focus."""
        f = _api()
        saved_cur, saved_fg = self._saved_cursor, self._saved_fg
        self._saved_cursor = self._saved_fg = None
        if not (self.return_focus and saved_fg and f.IsWindow(saved_fg)):
            saved_fg = None
        if saved_cur is None and saved_fg is None:
            return
        self._settling = True
        try:
            rect = self._monitor_rect()
            pt = wintypes.POINT()
            start = time.monotonic()
            settled = 0
            while True:
                ok = True
                if saved_cur is not None and f.GetCursorPos(ctypes.byref(pt)) \
                        and self._cursor_dragged(rect, pt.x, pt.y):
                    f.SetCursorPos(*saved_cur)
                    ok = False
                if saved_fg is not None and f.GetForegroundWindow() != saved_fg:
                    _attach_set_foreground(f, saved_fg)
                    ok = False
                settled = settled + 1 if ok else 0
                elapsed = time.monotonic() - start
                if elapsed >= _SETTLE_MAX_S or \
                        (elapsed >= _SETTLE_MIN_S and settled >= 2):
                    break
                time.sleep(0.012)
        finally:
            self._settling = False

    def _make_contact(self, pointer_id, x, y, flags, bounds=None):
        info = POINTER_TOUCH_INFO()
        info.pointerInfo.pointerType = _PT_TOUCH
        info.pointerInfo.pointerId = pointer_id
        info.pointerInfo.ptPixelLocation.x = x
        info.pointerInfo.ptPixelLocation.y = y
        info.pointerInfo.pointerFlags = flags
        info.touchFlags = 0
        info.touchMask = (_TOUCH_MASK_CONTACTAREA | _TOUCH_MASK_ORIENTATION
                          | _TOUCH_MASK_PRESSURE)
        info.orientation = 90
        info.pressure = 32000
        # A small 8px contact square so hit-testing picks a sensible target,
        # kept inside the desktop for touches on the monitor's very edge.
        l, t, r, b = x - 4, y - 4, x + 4, y + 4
        if bounds is not None:
            l, t = max(l, bounds[0]), max(t, bounds[1])
            r, b = min(r, bounds[2]), min(b, bounds[3])
        info.rcContact.left, info.rcContact.top = l, t
        info.rcContact.right, info.rcContact.bottom = r, b
        return info

    def _desktop_bounds(self, f):
        vx = f.GetSystemMetrics(_SM_XVIRTUALSCREEN)
        vy = f.GetSystemMetrics(_SM_YVIRTUALSCREEN)
        return (vx, vy, vx + f.GetSystemMetrics(_SM_CXVIRTUALSCREEN),
                vy + f.GetSystemMetrics(_SM_CYVIRTUALSCREEN))

    def _inject(self, contacts):
        """Inject one frame: every contact, in one call, as Windows requires
        (an UP frame still carries the other fingers' hold frames)."""
        f = _api()
        n = len(contacts)
        arr = (POINTER_TOUCH_INFO * n)(*contacts)
        if not f.InjectTouchInput(n, arr):
            raise OSError(ctypes.get_last_error(), "InjectTouchInput failed")

    def _frame(self, down=None, lift=None, cancel=False):
        """Inject the current frame for every active contact: `down` gets
        DOWN, `lift` gets UP (UP|CANCELED for a cancel — no click), the
        rest an UPDATE hold. Returns False when Windows rejected it."""
        bounds = self._desktop_bounds(_api())
        contacts = []
        for pid, (px, py) in self._active.items():
            if pid == lift:
                flags = _POINTER_FLAG_UP | (_POINTER_FLAG_CANCELED if cancel
                                            else 0)
            elif pid == down:
                flags = (_POINTER_FLAG_DOWN | _POINTER_FLAG_INRANGE
                         | _POINTER_FLAG_INCONTACT)
            else:
                flags = (_POINTER_FLAG_UPDATE | _POINTER_FLAG_INRANGE
                         | _POINTER_FLAG_INCONTACT)
            contacts.append(self._make_contact(pid, px, py, flags, bounds))
        if not contacts:
            return True
        try:
            self._inject(contacts)
        except OSError as e:
            self._touch_failed(e)
            return False
        self._last_inject = time.monotonic()
        self._failures = 0
        return True

    def _touch_failed(self, err):
        """Windows rejected a frame (it already dropped our contacts, or the
        frame was invalid). Lift whatever may still be pressed, forget the
        contacts so the next 'down' starts a clean gesture instead of a chain
        of invalid frames, and only after repeated failures give up on touch
        for the mouse fallback."""
        self._failures += 1
        if self._failures in (1, 10):
            print(f"[touch] injection failed: {err}")
        if self._active:
            bounds = self._desktop_bounds(_api())
            for pid, (px, py) in list(self._active.items()):
                try:
                    self._inject([self._make_contact(
                        pid, px, py,
                        _POINTER_FLAG_UP | _POINTER_FLAG_CANCELED, bounds)])
                except OSError:
                    pass
            self._active.clear()
            self._end_gesture()
        if self._failures >= _MAX_FAILURES and self._touch_ok:
            print("[touch] touch injection keeps failing; using the mouse")
            self._touch_ok = False
            self._available = False

    def _cancel_all(self):
        """Lift every contact as cancelled (no click) and release a held
        fallback button, then run the usual cursor/focus restore."""
        if self._active:
            lift = list(self._active)
            bounds = self._desktop_bounds(_api())
            try:
                self._inject([self._make_contact(
                    pid, px, py, _POINTER_FLAG_UP | _POINTER_FLAG_CANCELED,
                    bounds) for pid, (px, py) in self._active.items()])
            except OSError:
                pass
            for pid in lift:
                self._active.pop(pid, None)
            self._end_gesture()
        if self._mouse_down:
            self._mouse_down = False
            f = _api()
            self._send_mouse(f, [(_MOUSEEVENTF_LEFTUP, 0, 0, 0)])
            self._end_gesture()

    def _do_touch(self, action, pointer_id, pt):
        if action not in ("down", "move", "up", "cancel") \
                or not 0 <= pointer_id < _MAX_CONTACTS:
            return
        self._last_input = time.monotonic()
        if not self._touch_ok:
            self._do_touch_mouse_fallback(action, pointer_id, pt)
            return
        if action == "down":
            if pt is None:
                return
            if pointer_id in self._active:
                # A second down for a contact still pressed: its up got lost.
                # Lift the stale one (cancelled, no click) and start over.
                if self._frame(lift=pointer_id, cancel=True):
                    self._active.pop(pointer_id, None)
            if not self._active:
                self._begin_gesture(pt)
            self._active[pointer_id] = pt
            self._last_point = pt
            self._frame(down=pointer_id)
            return
        if pointer_id not in self._active:
            return  # no matching down (or Windows dropped it): ignore
        if pt is not None:
            self._active[pointer_id] = pt
            self._last_point = pt
        if action == "move":
            self._frame()
            return
        # up / cancel. On failure _touch_failed has already cleaned up.
        if self._frame(lift=pointer_id, cancel=action == "cancel"):
            self._active.pop(pointer_id, None)
            if not self._active:
                self._end_gesture()

    # ---- mouse fallback (visible cursor) ----

    def _abs_point(self, f, x, y):
        """Pixel (x, y) -> 0..65535 absolute coords over the VIRTUAL desktop
        (so a phone monitor off to the side is addressable)."""
        vx = f.GetSystemMetrics(_SM_XVIRTUALSCREEN)
        vy = f.GetSystemMetrics(_SM_YVIRTUALSCREEN)
        vw = max(1, f.GetSystemMetrics(_SM_CXVIRTUALSCREEN))
        vh = max(1, f.GetSystemMetrics(_SM_CYVIRTUALSCREEN))
        ax = int((x - vx) * 65535 / vw)
        ay = int((y - vy) * 65535 / vh)
        return ax, ay

    def _send_mouse(self, f, events):
        """One SendInput call for [(flags, ax, ay, data)], so the system
        delivers them back to back, never interleaved with the PC user's
        own mouse input."""
        arr = (INPUT * len(events))()
        for inp, (flags, ax, ay, data) in zip(arr, events):
            inp.type = _INPUT_MOUSE
            inp.u.mi = _MOUSEINPUT(ax, ay, data & 0xFFFFFFFF, flags, 0, None)
        f.SendInput(len(events), arr, ctypes.sizeof(INPUT))

    def _do_touch_mouse_fallback(self, action, pointer_id, pt):
        # Only the primary contact drives the cursor; a second finger in
        # fallback mode is meaningless and ignored.
        if pointer_id != 0:
            return
        f = _api()
        base = _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK
        if action == "down":
            if pt is None:
                return
            if self._mouse_down:  # lost up: release before pressing again
                self._send_mouse(f, [(_MOUSEEVENTF_LEFTUP, 0, 0, 0)])
            else:
                self._begin_gesture(pt)
            self._mouse_down = True
            self._last_point = pt
            ax, ay = self._abs_point(f, *pt)
            self._send_mouse(f, [(base | _MOUSEEVENTF_MOVE, ax, ay, 0),
                                 (base | _MOUSEEVENTF_LEFTDOWN, ax, ay, 0)])
            return
        if not self._mouse_down:
            return
        if pt is not None:
            self._last_point = pt
        ax, ay = self._abs_point(f, *self._last_point)
        if action == "move":
            self._send_mouse(f, [(base | _MOUSEEVENTF_MOVE, ax, ay, 0)])
            return
        self._send_mouse(f, [(base | _MOUSEEVENTF_MOVE, ax, ay, 0),
                             (base | _MOUSEEVENTF_LEFTUP, ax, ay, 0)])
        self._mouse_down = False
        self._end_gesture()

    # ---- keyboard ----

    def _focus_for_input(self, f, hwnd):
        """Focus hwnd and confirm it took. False means keystrokes would land
        in whatever the PC user has focused, so the caller must drop them."""
        if not hwnd or not f.IsWindow(hwnd):
            return False
        _attach_set_foreground(f, hwnd)
        for _ in range(10):
            fg = f.GetForegroundWindow()
            if fg and (fg == hwnd or f.GetAncestor(fg, _GA_ROOTOWNER) == hwnd):
                return True
            time.sleep(0.01)
        print("[touch] could not focus the phone window; input dropped")
        return False

    def _send_keys(self, f, prev, hwnd, keys):
        """SendInput [(vk, scan, flags)] in ONE call — atomic, so the PC
        user's own typing can't interleave with the phone's — then hand
        focus back. SendInput only queues: the system routes each key to
        whichever window is foreground when it gets to it, so give it a
        moment before switching focus away, or the tail of the text would
        land in the PC user's window."""
        arr = (INPUT * len(keys))()
        for inp, (vk, scan, flags) in zip(arr, keys):
            inp.type = _INPUT_KEYBOARD
            inp.u.ki = _KEYBDINPUT(vk, scan, flags, 0, None)
        f.SendInput(len(keys), arr, ctypes.sizeof(INPUT))
        if self.return_focus and prev and prev != hwnd and f.IsWindow(prev):
            time.sleep(min(0.4, 0.04 + 0.0005 * len(keys)))
            _attach_set_foreground(f, prev)

    def _do_type(self, text, hwnd):
        f = _api()
        units = _utf16_units(text)
        if not units:
            return
        prev = f.GetForegroundWindow()
        if not self._focus_for_input(f, hwnd):
            return
        keys = []
        for unit in units:
            keys.append((0, unit, _KEYEVENTF_UNICODE))
            keys.append((0, unit, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP))
        self._send_keys(f, prev, hwnd, keys)

    def _do_key(self, name, hwnd):
        vk = _VK.get(name.lower())
        if vk is None:
            if len(name) == 1:
                # A single character types as text: the Unicode path gets
                # shifted symbols right where a bare virtual key would not.
                self._do_type(name, hwnd)
            return
        f = _api()
        prev = f.GetForegroundWindow()
        if not self._focus_for_input(f, hwnd):
            return
        self._send_keys(f, prev, hwnd, [(vk, 0, 0), (vk, 0, _KEYEVENTF_KEYUP)])

    def _do_wheel(self, x, y, dx, dy):
        f = _api()
        ax, ay = self._abs_point(f, x, y)
        base = _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK
        # Move (no button) so the wheel targets the control under the point,
        # then restore the PC user's cursor — after a beat, because the
        # system routes the queued wheel by the cursor position at the time
        # it processes it, not when SendInput returned.
        saved = wintypes.POINT()
        had = bool(f.GetCursorPos(ctypes.byref(saved)))
        events = [(base | _MOUSEEVENTF_MOVE, ax, ay, 0)]
        if dy:
            events.append((_MOUSEEVENTF_WHEEL, 0, 0, int(dy * _WHEEL_DELTA)))
        if dx:
            events.append((_MOUSEEVENTF_HWHEEL, 0, 0, int(dx * _WHEEL_DELTA)))
        self._settling = True  # keep the cursor fence off meanwhile
        try:
            self._send_mouse(f, events)
            time.sleep(0.03)
            if had:
                f.SetCursorPos(saved.x, saved.y)
        finally:
            self._settling = False
