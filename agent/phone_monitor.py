"""PhoneMonitor — "phone as a second monitor" orchestration.

Ties the Parsec virtual display (vdisplay) and touch injection (touch)
together into the thing the phone talks to. The idea: the PC user keeps
working on their real screen while one or more apps (Discord, say) live on
a virtual monitor that only the phone sees and drives.

Design decisions worth stating up front:

  * The virtual monitor is parked at a corner of the primary monitor
    (its top-left at the primary's bottom-right), touching only at that
    single pixel. The PC user's mouse therefore can't slide onto it during
    normal use; the phone reaches it through absolute touch coordinates,
    which don't care about adjacency. A ~16Hz cursor fence in the agent
    nudges the cursor back if it somehow lands there anyway.

  * Phone input is TOUCH, not mouse, so the PC cursor never jumps to service
    a tap; after a gesture the cursor and the PC user's focused window are
    restored (toggle: return_focus, default on). Typed text only ever goes
    to a window on the phone monitor — never to whatever the PC user has
    focused.

  * Everything is safe and cheap when the driver is missing: status() just
    reports driver "missing" and nothing touches Win32. The feature then
    offers a one-time, user-approved install from the phone.

  * The driver's keepalive is crash-safety we WANT: if the agent dies, the
    monitor unplugs within a second and Windows moves the apps back to the
    real screen on their own. A failed enable() unplugs what it added, so
    no half-configured monitor is ever left on the PC.

Normalized touch coordinates from the phone are 0..1 inside the phone
monitor; this module maps them to physical pixels before injection.
"""

import os
import platform
import subprocess
import threading
import time

import vdisplay
from touch import TouchInjector, _api as _touch_api, _attach_set_foreground

IS_WINDOWS = platform.system() == "Windows"

# Named sizes -> landscape (width, height) display modes.
SIZE_MODES = {
    "fhd": (1920, 1080),
    "hdplus": (1600, 900),
    "hd": (1280, 720),
}
# 1920x1080: matches a phone's ~20:9 screen far better than 16:9 HD modes,
# and stays wider than apps' minimum window widths (Discord's is ~940px)
# even when rotated to portrait.
DEFAULT_SIZE = "fhd"

_ORIENTATIONS = ("portrait", "landscape")

# A foreground switch to a phone-screen window within this long after phone
# input (tap, typing, moving an app there) is the phone's doing; later ones
# are the PC user reaching for the app (see PhoneMonitor.check_reclaim).
_PHONE_INPUT_GRACE_S = 1.5

# For this long after the phone screen comes up, windows found on it are the
# phone's (Windows moving apps back onto a re-plugged monitor after an agent
# restart takes a few seconds); after that, a new window showing up there with
# no phone input is one the PC user opened (see sweep()).
_ENABLE_ADOPT_S = 6.0

# Apps that can open a second window. When the PC user reaches for one of
# these while its window is on the phone screen, the PC gets a NEW window and
# the phone keeps its own (a movie in one browser window on the phone while
# the PC browses in another). exe name -> arguments that open a new window.
_NEW_WINDOW_ARGS = {
    "chrome.exe": ["--new-window"], "msedge.exe": ["--new-window"],
    "brave.exe": ["--new-window"], "opera.exe": ["--new-window"],
    "vivaldi.exe": ["--new-window"], "firefox.exe": ["-new-window"],
    "code.exe": ["--new-window"], "explorer.exe": [],
}
_TOUCH_ACTIONS = ("down", "move", "up", "cancel")

# SetWindowPos flags / ShowWindow commands used to move apps around.
_SWP_NOZORDER = 0x4
_SWP_NOACTIVATE = 0x10
_SWP_FRAMECHANGED = 0x20
_SW_RESTORE = 9
_SW_SHOWNOACTIVATE = 4
_SW_MAXIMIZE = 3
_GWL_STYLE = -16
_WS_MAXIMIZE = 0x01000000
_WS_MINIMIZE = 0x20000000


class PhoneMonitor:
    """One phone monitor for the agent. Not thread-safe across its mutating
    methods (enable/disable/move); the agent drives them one at a time from
    its event loop via asyncio.to_thread. touch()/fence_cursor() are the
    hot-path exceptions and are safe to call concurrently.

    When the phone's connection drops the agent should call
    release_touches(): a finger that was down can't be lifted over a dead
    socket (the injector also lifts contacts that go quiet for 8s)."""

    def __init__(self, settings=None, persist=None):
        self.persist = persist  # optional callback(settings dict) to save
        self.settings = {
            "size": DEFAULT_SIZE,
            # A second PC monitor, not a phone-shaped one: landscape
            # 1920x1080 by default (portrait stays one menu tap away).
            "orientation": "landscape",
            "scale": 100,
            "return_focus": True,
        }
        if settings:
            # Persisted values come from a config file: keep only sane ones
            # so a hand-edited/corrupt entry can't break enable().
            if settings.get("size") in SIZE_MODES:
                self.settings["size"] = settings["size"]
            if settings.get("orientation") in _ORIENTATIONS:
                self.settings["orientation"] = settings["orientation"]
            if "scale" in settings:
                self.settings["scale"] = _clamp_scale(settings["scale"])
            if "return_focus" in settings:
                self.settings["return_focus"] = bool(settings["return_focus"])
        self._vdd = None
        self._index = None          # driver display index of our monitor
        self._device = None         # GDI name, e.g. \\.\DISPLAY2
        self._enabled = False
        self._here = {}             # hwnd -> original placement for restore
        self._setup = None          # last install progress, for status()
        self._install_lock = threading.Lock()
        # Last phone-driven input / window move (monotonic s), and the PC's
        # foreground window at the last reclaim check: see check_reclaim().
        self._phone_input_at = 0.0
        self._last_fg = None
        self._enabled_at = 0.0
        self._iconic = set()        # phone-screen windows seen minimized
        self._touch = TouchInjector(
            monitor_rect=self.rect, return_focus=self.settings["return_focus"])
        self._win = _win_api() if IS_WINDOWS else None

    # ------------------------------------------------------------ status

    @property
    def enabled(self):
        """Cheap check for the agent's hot paths (status() enumerates
        windows)."""
        return self._enabled

    def driver_state(self):
        """"installed" | "missing" | "error" — flattened for the phone. A
        disabled/restart-required driver is surfaced as "error" so the UI
        can show the detail from status()."""
        if not IS_WINDOWS:
            return "missing"
        st = vdisplay.driver_status().get("state")
        if st == "ok":
            return "installed"
        if st == "missing":
            return "missing"
        return "error"

    def status(self):
        """The full snapshot the phone renders from (see
        phone_monitor_page.dart)."""
        drv = self.driver_state()
        out = {
            "driver": drv,
            "enabled": self._enabled,
            "settings": dict(self.settings),
            "windows": self.windows_here() if self._enabled else [],
        }
        if drv == "error":
            out["driver_detail"] = vdisplay.driver_status()
        if self._setup is not None:
            out["setup"] = dict(self._setup)
        if self._enabled:
            r = self.rect()
            idx = self.monitor_index()
            if r is not None and idx is not None:
                out["monitor"] = {"index": idx, "left": r[0], "top": r[1],
                                  "width": r[2] - r[0], "height": r[3] - r[1]}
        return out

    # ------------------------------------------------------------ geometry

    def rect(self):
        """Physical pixel rect (l, t, r, b) of the phone monitor, or None."""
        if not self._enabled or not self._device:
            return None
        mon = vdisplay.monitor_for_device(self._device)
        if mon is None:
            return None
        return (mon["left"], mon["top"], mon["right"], mon["bottom"])

    def work_rect(self):
        if not self._enabled or not self._device:
            return None
        mon = vdisplay.monitor_for_device(self._device)
        return None if mon is None else mon["work"]

    def monitor_index(self):
        """Index of the phone monitor in mss().monitors (1-based: monitors[0]
        is the virtual bounding box, monitors[1:] follow EnumDisplayMonitors
        order), so the streamer can grab exactly this monitor."""
        if not self._enabled or not self._device:
            return None
        for i, m in enumerate(vdisplay.monitors()):
            if m["device"].lower() == self._device.lower():
                return i + 1  # mss prepends the "all monitors" entry
        return None

    def _other_monitors(self):
        """Every active monitor except the phone one (the PC's real ones)."""
        dev = (self._device or "").lower()
        return [m for m in vdisplay.monitors() if m["device"].lower() != dev]

    # ------------------------------------------------------------ enable

    def enable(self, size=None, orientation=None, scale=None):
        """Plug in the virtual monitor, apply mode/orientation/position/
        scale, and start the touch injector. Idempotent: re-applies settings
        on an already-enabled monitor. Returns status()."""
        if not IS_WINDOWS:
            raise vdisplay.DisplayError(
                "virtual displays are only supported on Windows")
        if size in SIZE_MODES:
            self.settings["size"] = size
        if orientation in _ORIENTATIONS:
            self.settings["orientation"] = orientation
        if scale is not None:
            self.settings["scale"] = _clamp_scale(scale)
        vdisplay.ensure_dpi_aware()
        if self._enabled and self.rect() is None:
            # Our monitor vanished under us (driver reset, starved
            # keepalive): forget it and plug in a fresh one.
            self._teardown_display()
            self._enabled = False

        try:
            if self._vdd is None:
                self._vdd = vdisplay.ParsecVdd().open()
            if self._index is None:
                before = {d["device"] for d in vdisplay.parsec_displays()
                          if d["attached"]}
                before |= {m["device"] for m in vdisplay.monitors()}
                self._index = self._vdd.add()
                self._device = self._wait_for_display(before)
                if self._device is None:
                    raise vdisplay.DisplayError(
                        "the virtual monitor did not appear")
            self._apply_geometry()
        except (vdisplay.DisplayError, OSError):
            if not self._enabled:
                # First bring-up failed part way: unplug what we added so
                # the PC isn't left with a stray, unparked monitor.
                self._teardown_display()
            raise

        self._touch.return_focus = self.settings["return_focus"]
        self._phone_input_at = time.monotonic()
        if not self._enabled:
            self._enabled_at = time.monotonic()
        self._touch.start()
        self._enabled = True
        self._save()
        return self.status()

    def _wait_for_display(self, before):
        """Wait up to 5s for the freshly-added Parsec output to show up as an
        attached GDI display, and return its \\\\.\\DISPLAYn name. Outputs in
        `before` were attached already (e.g. the Parsec app's own virtual
        displays) and are never ours to reconfigure."""
        # Windows remembers a topology per set of connected screens. When it
        # last saw this set in Duplicate mode, the new virtual monitor just
        # MIRRORS the PC's own screen: no new monitor appears, and the
        # primary's source drives both the panel and the Parsec monitor.
        # Taking that at face value made the "phone screen" the PC's own
        # screen (the phone streamed the PC screen; "moved" apps never left
        # it). So: a candidate must be a non-primary monitor that didn't
        # exist before and shows ONLY the Parsec monitor, seen twice in a
        # row; if none shows up, switch Windows to Extend once.
        start = time.monotonic()
        last = None
        extended = False
        while time.monotonic() - start < 8:
            candidate = None
            for m in vdisplay.monitors():
                if m["primary"] or m["device"] in before:
                    continue
                if vdisplay.is_parsec_monitor(m["device"]):
                    candidate = m["device"]
                    break
            if candidate is not None and candidate == last:
                return candidate
            last = candidate
            if candidate is None and not extended \
                    and time.monotonic() - start > 1.0:
                extended = True
                try:
                    vdisplay.force_extend()
                except vdisplay.DisplayError:
                    pass  # keep waiting; enable fails cleanly at the end
            time.sleep(0.2)
        return None

    def _is_ours(self, device):
        """Safety net for every mode change: only ever reconfigure a non-
        primary monitor that shows nothing but the Parsec virtual screen —
        never the PC's real screen."""
        if not device:
            return False
        mon = vdisplay.monitor_for_device(device)
        return (mon is not None and not mon["primary"]
                and vdisplay.is_parsec_monitor(device))

    def _apply_geometry(self):
        """Stage mode + orientation + corner position for the phone monitor,
        apply in one shot, then set its DPI scale."""
        if not self._is_ours(self._device):
            raise vdisplay.DisplayError(
                f"refusing to reconfigure {self._device}: not the phone "
                "screen")
        w, h = SIZE_MODES[self.settings["size"]]
        portrait = self.settings["orientation"] == "portrait"
        orient = vdisplay.DMDO_270 if portrait else vdisplay.DMDO_DEFAULT
        pos = self._corner_position(*((h, w) if portrait else (w, h)))
        try:
            vdisplay.stage_mode(self._device, width=w, height=h,
                                orientation=orient, position=pos)
            vdisplay.apply_staged()
        except vdisplay.DisplayError:
            if not portrait:
                raise
            # Some IddCx builds refuse rotated modes: a landscape phone
            # screen beats no phone screen.
            self.settings["orientation"] = "landscape"
            pos = self._corner_position(w, h)
            vdisplay.stage_mode(self._device, width=w, height=h,
                                orientation=vdisplay.DMDO_DEFAULT,
                                position=pos)
            vdisplay.apply_staged()
        # Re-position after the mode set: a resolution change can nudge the
        # origin, and we need the exact corner so the fence math is right.
        cur = vdisplay.current_mode(self._device)
        if cur and (cur["x"], cur["y"]) != pos:
            try:
                vdisplay.stage_mode(self._device, position=pos)
                vdisplay.apply_staged()
            except vdisplay.DisplayError:
                pass
        # Always applied, 100% included: skipping the default would leave a
        # monitor stuck at the previous scale after "Text size -> 100%".
        try:
            vdisplay.set_dpi_scale(self._device, self.settings["scale"])
        except (vdisplay.DisplayError, OSError):
            pass  # scale is cosmetic; never fail enable over it

    def _corner_position(self, width, height):
        """Top-left origin that parks the phone monitor at the primary's
        bottom-right corner (single-pixel touch, so the mouse can't wander
        over). If another real monitor already occupies that spot, park
        past the bottom-right corner of the whole desktop instead — Windows
        refuses overlapping monitors."""
        others = self._other_monitors()
        if not others:
            return (1920, 1080)
        prim = next((m for m in others if m["primary"]), others[0])
        x, y = prim["right"], prim["bottom"]
        mine = (x, y, x + width, y + height)
        if any(_overlaps(mine, (m["left"], m["top"], m["right"], m["bottom"]))
               for m in others):
            x = max(m["right"] for m in others)
            y = max(m["bottom"] for m in others)
        return (x, y)

    # ------------------------------------------------------------ disable

    def disable(self):
        """Move every window back to the primary monitor, unplug the display,
        stop the injector. Returns status()."""
        # Lift any finger still down while its monitor still exists.
        self._touch.stop()
        for hwnd in list(self._here):
            try:
                self.move_window_back(hwnd)
            except (OSError, vdisplay.DisplayError):
                self._here.pop(hwnd, None)
        self._teardown_display()
        self._enabled = False
        return self.status()

    def _teardown_display(self):
        """Unplug our display and close the driver handle (stopping the
        keepalive). Never raises."""
        vdd, idx = self._vdd, self._index
        self._vdd = self._index = self._device = None
        if vdd is None:
            return
        if idx is not None:
            try:
                vdd.remove(idx)
            except (OSError, vdisplay.DisplayError):
                pass  # close() retries; a dead keepalive unplugs it anyway
        vdd.close()

    def close(self):
        """Hard teardown for agent shutdown / client drop."""
        try:
            if self._enabled:
                self.disable()
        finally:
            self._touch.stop()
            self._teardown_display()

    # ------------------------------------------------------------ settings

    def update_settings(self, size=None, orientation=None, scale=None,
                        return_focus=None):
        """Apply any subset of the settings. Geometry-affecting ones re-stage
        the live monitor; return_focus just flips the injector flag. Returns
        status(). A rejected mode change leaves the settings as they were."""
        old = dict(self.settings)
        geom = False
        if size in SIZE_MODES:
            self.settings["size"] = size
            geom = True
        if orientation in _ORIENTATIONS:
            self.settings["orientation"] = orientation
            geom = True
        if scale is not None:
            self.settings["scale"] = _clamp_scale(scale)
            geom = True
        if return_focus is not None:
            self.settings["return_focus"] = bool(return_focus)
        self._touch.return_focus = self.settings["return_focus"]
        if geom and self._enabled and self._device:
            try:
                self._apply_geometry()
            except (vdisplay.DisplayError, OSError):
                self.settings = old
                self._touch.return_focus = old["return_focus"]
                raise
        self._save()
        return self.status()

    def _save(self):
        if self.persist:
            try:
                self.persist(dict(self.settings))
            except Exception:
                pass

    # ------------------------------------------------------------ windows

    def _style(self, hwnd):
        return self._win.GetWindowLongW(hwnd, _GWL_STYLE)

    def _window_rect(self, hwnd):
        w = self._win
        r = w.RECT()
        w.GetWindowRect(hwnd, w.byref(r))
        return (r.left, r.top, r.right, r.bottom)

    def move_window_here(self, hwnd):
        """Move one top-level window onto the phone monitor and maximize it.
        Remembers where it was so move_window_back can restore it."""
        if not self._enabled or not self._device:
            raise vdisplay.DisplayError("the phone monitor is not enabled")
        hwnd = int(hwnd)
        w = self._win
        if not w.IsWindow(hwnd):
            raise vdisplay.DisplayError("that window no longer exists")
        # Maximizing it activates it for a moment: that's the phone's doing,
        # not the PC user reaching for it (see check_reclaim).
        self._phone_input_at = time.monotonic()
        work = self.work_rect() or self.rect()
        if work is None:
            raise vdisplay.DisplayError("the phone monitor has no area yet")
        fg = _touch_api().GetForegroundWindow()
        maximized = bool(self._style(hwnd) & _WS_MAXIMIZE)
        minimized = bool(w.IsIconic(hwnd))
        # Un-maximize/-minimize first: SetWindowPos is ignored on a maximized
        # window, so it would never leave the primary monitor otherwise.
        if maximized or minimized:
            w.ShowWindow(hwnd, _SW_RESTORE)
        if hwnd not in self._here:
            # Read AFTER the restore: that is the window's normal rect in
            # screen coordinates, exactly what move_window_back needs (a
            # maximized rect would come back as a screen-sized window).
            self._here[hwnd] = {"rect": self._window_rect(hwnd),
                                "maximized": maximized,
                                "minimized": minimized}
        l, t, r, b = work
        w.SetWindowPos(hwnd, None, l, t, r - l, b - t,
                       _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_FRAMECHANGED)
        w.ShowWindow(hwnd, _SW_MAXIMIZE)
        # Restoring/maximizing activates the app on the (invisible to the PC)
        # phone monitor; give the keyboard back to the PC user's window.
        if self.settings["return_focus"] and fg and fg != hwnd \
                and w.IsWindow(fg) and not self._on_phone(fg):
            _attach_set_foreground(_touch_api(), fg)
            # Maximizing is asynchronous: apps (Explorer, browsers) often
            # activate themselves a beat AFTER the restore above, which would
            # leave the PC user typing into a window they can't see.
            end = time.monotonic() + 0.6
            while time.monotonic() < end:
                time.sleep(0.05)
                cur = _touch_api().GetForegroundWindow()
                if cur and cur != fg and self._on_phone(cur):
                    _attach_set_foreground(_touch_api(), fg)
        if not self._on_phone(hwnd):
            # Windows silently ignores moves of an elevated app's window
            # (UIPI) and of some shell-hosted ones: say so rather than
            # reporting success and dropping it from the list a moment later.
            self._here.pop(hwnd, None)
            raise vdisplay.DisplayError(
                "that window can't be moved (it may be running as "
                "administrator)")
        return self.status()

    def move_window_back(self, hwnd):
        """Return a window to the PC (its saved placement, or a centered
        default on the primary) and forget it."""
        hwnd = int(hwnd)
        w = self._win
        saved = self._here.pop(hwnd, None)
        if not w.IsWindow(hwnd):
            return self.status()
        if w.IsIconic(hwnd) or self._style(hwnd) & _WS_MAXIMIZE:
            w.ShowWindow(hwnd, _SW_RESTORE)
        rect = saved["rect"] if saved else None
        # Guard against restoring onto the phone monitor itself or a real
        # monitor that has since been unplugged.
        if rect is None or not self._on_real_monitor(rect):
            prim = vdisplay.primary_monitor()
            rect = _centered_default(prim) if prim else None
        if rect is not None:
            l, t, r, b = rect
            w.SetWindowPos(hwnd, None, l, t, max(1, r - l), max(1, b - t),
                           _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_FRAMECHANGED)
        if saved and saved["maximized"]:
            w.ShowWindow(hwnd, _SW_MAXIMIZE)
        return self.status()

    def _on_phone(self, hwnd):
        rect = self.rect()
        return rect is not None and _mostly_inside(self._window_rect(hwnd),
                                                   rect)

    def _on_real_monitor(self, rect):
        return any(_mostly_inside(rect, (m["left"], m["top"], m["right"],
                                         m["bottom"]))
                   for m in self._other_monitors())

    def windows_here(self):
        """The windows currently on the phone monitor: [{id, title, process}].
        Prunes any that were closed or moved off since we placed them."""
        if not self._enabled or self._win is None:
            return []
        rect = self.rect()
        out = []
        for hwnd in list(self._here):
            if not self._win.IsWindow(hwnd):
                self._here.pop(hwnd, None)
                continue
            # A minimized window reports a far-off rect; it is still ours.
            if rect is not None and not self._win.IsIconic(hwnd) and \
                    not _mostly_inside(self._window_rect(hwnd), rect):
                # The PC user dragged it back themselves: stop tracking it.
                self._here.pop(hwnd, None)
                continue
            title, proc = self._win.window_text_process(hwnd)
            out.append({"id": hwnd, "title": title, "process": proc})
        return out

    def window_pids(self):
        """Owning process of every app on the phone screen (for phone
        audio: each one's process tree is captured)."""
        if not self._enabled or self._win is None:
            return []
        pids = set()
        for hwnd in self._here:
            if self._win.IsWindow(hwnd):
                pid = self._win.window_pid(hwnd)
                if pid:
                    pids.add(pid)
        return sorted(pids)

    def _all_windows(self):
        try:
            import winapps
            return winapps.list_windows(False)["windows"]
        except Exception:
            return []

    def _box_on_phone(self, win, rect):
        r = win.get("rect") or {}
        box = (r.get("left", 0), r.get("top", 0),
               r.get("left", 0) + r.get("width", 0),
               r.get("top", 0) + r.get("height", 0))
        return _mostly_inside(box, rect)

    def sweep(self):
        """Windows that land on the phone screen without the phone moving them
        (agent's ~1/s watcher). Right after the screen comes up (Windows
        moving apps back onto a re-plugged monitor) or right after phone
        input (a phone app opening a second window) they are the phone's and
        get tracked. Any other new window there is one the PC user just
        opened (apps reopen where they last were, i.e. on the invisible
        screen): it goes to the PC screen, in front. Returns True when the
        phone's window list changed."""
        if not self._enabled or self._win is None:
            return False
        rect = self.rect()
        if rect is None:
            return False
        now = time.monotonic()
        phones_doing = (now - self._enabled_at < _ENABLE_ADOPT_S
                        or now - self._phone_input_at < _PHONE_INPUT_GRACE_S
                        or self._touch.gesture_in_progress())
        changed = False
        for win in self._all_windows():
            hwnd = int(win["id"])
            if hwnd in self._here or win.get("minimized") \
                    or not self._box_on_phone(win, rect):
                continue
            if phones_doing:
                self._here[hwnd] = None  # placement unknown: send-back centers it
            else:
                self._to_pc(hwnd)
            changed = True
        return changed

    def _to_pc(self, hwnd, maximize=False):
        """Put a window the phone doesn't own on the primary monitor and bring
        it to the front for the PC user."""
        w = self._win
        prim = vdisplay.primary_monitor()
        if prim is None or not w.IsWindow(hwnd):
            return
        if w.IsIconic(hwnd) or self._style(hwnd) & _WS_MAXIMIZE:
            w.ShowWindow(hwnd, _SW_RESTORE)
        l, t, r, b = _centered_default(prim)
        w.SetWindowPos(hwnd, None, l, t, r - l, b - t,
                       _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_FRAMECHANGED)
        if maximize:
            w.ShowWindow(hwnd, _SW_MAXIMIZE)
        _attach_set_foreground(_touch_api(), hwnd)
        self._last_fg = _touch_api().GetForegroundWindow()

    # ------------------------------------------------------------ reclaim

    def check_reclaim(self):
        """Cheap (agent's ~16Hz watcher): the phone-screen window the PC user
        just brought to the front, or None.

        An app on the phone screen is invisible to the PC user, but still
        theirs: clicking it in the taskbar, Alt-Tabbing to it or opening it
        again (which just activates the existing window) focuses it on a
        screen they can't see — it looks like the app broke. So when the
        foreground SWITCHES to a phone-screen window and the phone hasn't
        touched/typed/moved anything in the last moment, that's the PC user
        reaching for it, and the agent hands it back (reclaim()). A switch
        caused by phone input is left alone, as is a window that simply stays
        focused (return_focus off)."""
        if not self._enabled or self._win is None:
            return None
        fg = _touch_api().GetForegroundWindow()
        changed = fg != self._last_fg
        self._last_fg = fg
        if not changed or not fg or self._touch.gesture_in_progress():
            return None
        if time.monotonic() - self._phone_input_at < _PHONE_INPUT_GRACE_S:
            return None
        if not self._on_phone(fg):
            return None
        return fg

    def check_minimized(self):
        """Cheap (agent's ~16Hz watcher): phone-screen windows the PC just
        minimized. Clicking the taskbar button of the ACTIVE window minimizes
        it, and a phone-screen app is often the active window right after the
        phone touched it: the PC user's "open Brave" then hides the phone's
        movie AND shows nothing on the PC. Minimizes done by the phone itself
        (within the input grace) are left alone."""
        if not self._enabled or self._win is None:
            return []
        phones_doing = (time.monotonic() - self._phone_input_at
                        < _PHONE_INPUT_GRACE_S
                        or self._touch.gesture_in_progress())
        out = []
        for hwnd in list(self._here):
            if not self._win.IsWindow(hwnd):
                self._iconic.discard(hwnd)
                continue
            iconic = bool(self._win.IsIconic(hwnd))
            if iconic and hwnd not in self._iconic:
                self._iconic.add(hwnd)
                if not phones_doing:
                    out.append(hwnd)
            elif not iconic:
                self._iconic.discard(hwnd)
        return out

    def _show_desktop_active(self):
        """True when every PC-screen app window is minimized too: the PC user
        pressed Show desktop (Win+D / Win+M), they didn't reach for an app."""
        pc = [w for w in self._all_windows() if int(w["id"]) not in self._here]
        return bool(pc) and all(w.get("minimized") for w in pc)

    def restore_on_phone(self, hwnd):
        """Un-minimize a phone-screen window where it is, without taking the PC
        user's focus, so the phone keeps showing it. If the PC user minimized
        it by reaching for the app (its taskbar button), also give the PC its
        own window of that app (reclaim()). Returns status()."""
        api = _touch_api()
        w = self._win
        fg = api.GetForegroundWindow()
        show_desktop = self._show_desktop_active()
        w.ShowWindow(hwnd, _SW_SHOWNOACTIVATE)
        work = self.work_rect() or self.rect()
        if work is not None and not self._style(hwnd) & _WS_MAXIMIZE:
            l, t, r, b = work
            w.SetWindowPos(hwnd, None, l, t, r - l, b - t,
                           _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_FRAMECHANGED)
        self._iconic.discard(hwnd)
        if show_desktop:
            if api.GetForegroundWindow() == hwnd and fg and fg != hwnd \
                    and w.IsWindow(fg):
                _attach_set_foreground(api, fg)
            self._last_fg = api.GetForegroundWindow()
            return self.status()
        return self.reclaim(hwnd)

    def reclaim(self, hwnd):
        """The PC user reached for an app whose window is on the phone screen.
        Leave the phone's window alone whenever the PC can have its own:
          1. the app already has another window on the PC screen: that one
             comes to the front;
          2. the app can open more windows (browsers, Explorer, VS Code): a
             new window opens on the PC screen;
          3. otherwise (one-window apps like Spotify or Discord, which can
             only be in one place) the window itself comes back to the PC.
        Returns status()."""
        rect = self.rect()
        wins = self._all_windows()
        me = next((w for w in wins if int(w["id"]) == int(hwnd)), None)
        exe = (me or {}).get("exe") or ""
        if exe and rect is not None:
            same = [w for w in wins if int(w["id"]) != int(hwnd)
                    and (w.get("exe") or "").lower() == exe.lower()]
            on_pc = [w for w in same if not self._box_on_phone(w, rect)]
            if on_pc:
                target = int(on_pc[0]["id"])
                if self._win.IsIconic(target):
                    self._win.ShowWindow(target, _SW_RESTORE)
                _attach_set_foreground(_touch_api(), target)
                self._last_fg = _touch_api().GetForegroundWindow()
                return self.status()
            args = _NEW_WINDOW_ARGS.get(os.path.basename(exe).lower())
            if args is not None and self._open_new_window(exe, args, wins,
                                                          rect):
                return self.status()
        status = self.move_window_back(hwnd)
        if self._win.IsWindow(hwnd):
            _attach_set_foreground(_touch_api(), hwnd)
        self._last_fg = _touch_api().GetForegroundWindow()
        return status

    def _open_new_window(self, exe, args, wins_before, rect):
        """Launch a second window of a multi-window app for the PC user and
        make sure it opens on the PC screen (apps tend to open new windows
        where their last one was: the phone screen). True on success."""
        before = {int(w["id"]) for w in wins_before}
        try:
            subprocess.Popen(
                [exe, *args], close_fds=True,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        except OSError:
            return False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            time.sleep(0.15)
            new = [w for w in self._all_windows()
                   if int(w["id"]) not in before
                   and (w.get("exe") or "").lower() == exe.lower()]
            if new:
                hwnd = int(new[0]["id"])
                self._here.pop(hwnd, None)
                if self._box_on_phone(new[0], rect):
                    self._to_pc(hwnd, maximize=True)
                else:
                    _attach_set_foreground(_touch_api(), hwnd)
                    self._last_fg = _touch_api().GetForegroundWindow()
                return True
        return False

    # ------------------------------------------------------------ cursor fence

    def fence_cursor(self):
        """If the PC cursor has strayed onto the phone monitor and no phone
        gesture is in progress, warp it to the nearest point just inside the
        primary monitor. Cheap enough for the agent's ~16Hz watcher. No-op
        when disabled or during a phone gesture (so touch promotion's own
        cursor moves aren't fought)."""
        if not self._enabled or self._win is None:
            return
        if self._touch.gesture_in_progress():
            return
        rect = self.rect()
        if rect is None:
            return
        pt = self._win.POINT()
        if not self._win.GetCursorPos(self._win.byref(pt)):
            return
        if not (rect[0] <= pt.x < rect[2] and rect[1] <= pt.y < rect[3]):
            return
        prim = vdisplay.primary_monitor()
        if prim is None:
            return
        nx = min(max(pt.x, prim["left"]), prim["right"] - 1)
        ny = min(max(pt.y, prim["top"]), prim["bottom"] - 1)
        self._win.SetCursorPos(nx, ny)

    # ------------------------------------------------------------ input

    def _to_pixel(self, nx, ny):
        """Normalized 0..1 inside the phone monitor -> physical pixel,
        clamped to the monitor so a rounding error can't land on another
        screen. None when there is no monitor or no usable coordinate."""
        rect = self.rect()
        if rect is None:
            return None
        try:
            fx = max(0.0, min(1.0, float(nx)))
            fy = max(0.0, min(1.0, float(ny)))
        except (TypeError, ValueError):
            return None
        l, t, r, b = rect
        return (int(round(l + fx * (r - l - 1))),
                int(round(t + fy * (b - t - 1))))

    def touch(self, action, pointer_id, nx=None, ny=None):
        """Inject a touch frame from normalized phone coordinates. up/cancel
        may omit them (the contact lifts where it last was). Silently
        ignored when disabled or malformed: it is fire-and-forget input, and
        a late packet after disable is harmless."""
        if not self._enabled or action not in _TOUCH_ACTIONS:
            return
        try:
            pointer_id = int(pointer_id)
        except (TypeError, ValueError):
            return
        px = self._to_pixel(nx, ny)
        if px is None:
            if action in ("down", "move"):
                return
            px = (None, None)
        self._phone_input_at = time.monotonic()
        self._touch.touch(action, pointer_id, px[0], px[1])

    def _focus_target(self):
        """The window on the phone monitor keyboard input goes to: the one
        the phone user last touched, else the most recently placed one. None
        when there is none — text is then dropped, never typed into the PC
        user's foreground window."""
        last = self._touch.last_touched
        if last and self._win.IsWindow(last) and self._on_phone(last):
            return last
        wins = self.windows_here()
        return wins[-1]["id"] if wins else None

    def text(self, text):
        if not self._enabled or not text:
            return
        target = self._focus_target()
        if target:
            self._phone_input_at = time.monotonic()
            self._touch.type_text(text, hwnd=target)

    def key(self, name):
        if not self._enabled or not name:
            return
        target = self._focus_target()
        if target:
            self._phone_input_at = time.monotonic()
            self._touch.press_key(name, hwnd=target)

    def wheel(self, nx, ny, dx, dy):
        if not self._enabled:
            return
        px = self._to_pixel(nx, ny)
        if px is None:
            return
        self._phone_input_at = time.monotonic()
        try:
            self._touch.wheel(px[0], px[1], float(dx or 0), float(dy or 0))
        except (TypeError, ValueError):
            pass

    def release_touches(self):
        """Lift every phone finger still down (the phone dropped mid-
        gesture). Safe to call any time."""
        self._touch.cancel_all()

    # ------------------------------------------------------------ install

    def install(self, progress=None):
        """One-time driver install (user-approved on the PC). Delegates to
        vdisplay.install; returns its final progress dict. Progress is also
        kept for status() so a phone that reopens the page mid-install sees
        where it is. A second call while one runs returns at once (busy)
        instead of racing the first over the same installer file."""
        if not self._install_lock.acquire(blocking=False):
            return dict(self._setup or {"stage": "downloading"}, busy=True)

        def report(state):
            self._setup = {k: state[k] for k in ("stage", "error")
                           if k in state}
            if progress:
                progress(state)

        try:
            return vdisplay.install(progress=report)
        finally:
            self._install_lock.release()


# ---------------------------------------------------------------- helpers

def _clamp_scale(scale):
    try:
        v = int(scale)
    except (TypeError, ValueError):
        return 100
    return max(100, min(v, 200))


def _overlaps(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _mostly_inside(rect, bounds):
    """True when the center of rect falls inside bounds."""
    cx = (rect[0] + rect[2]) // 2
    cy = (rect[1] + rect[3]) // 2
    return bounds[0] <= cx < bounds[2] and bounds[1] <= cy < bounds[3]


def _centered_default(prim):
    """A sane centered 1024x720 box on the primary monitor."""
    w = min(1024, prim["right"] - prim["left"] - 80)
    h = min(720, prim["bottom"] - prim["top"] - 80)
    l = prim["left"] + ((prim["right"] - prim["left"]) - w) // 2
    t = prim["top"] + ((prim["bottom"] - prim["top"]) - h) // 2
    return (l, t, l + w, t + h)


def _win_api():
    """The small set of user32 window-placement calls PhoneMonitor needs,
    bound on private WinDLL instances (never the shared ctypes.windll that
    agent.py configures)."""
    import ctypes
    from ctypes import wintypes
    import psutil

    u32 = ctypes.WinDLL("user32", use_last_error=True)

    def bind(name, restype, *argtypes):
        fn = getattr(u32, name)
        fn.restype = restype
        fn.argtypes = list(argtypes)
        return fn

    class _W:
        RECT = wintypes.RECT
        POINT = wintypes.POINT
        byref = staticmethod(ctypes.byref)

    w = _W()
    w.IsWindow = bind("IsWindow", wintypes.BOOL, wintypes.HWND)
    w.IsIconic = bind("IsIconic", wintypes.BOOL, wintypes.HWND)
    w.ShowWindow = bind("ShowWindow", wintypes.BOOL, wintypes.HWND, ctypes.c_int)
    w.GetWindowRect = bind("GetWindowRect", wintypes.BOOL, wintypes.HWND,
                           ctypes.POINTER(wintypes.RECT))
    w.SetWindowPos = bind("SetWindowPos", wintypes.BOOL, wintypes.HWND,
                          wintypes.HWND, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, wintypes.UINT)
    # GetWindowLongPtrW only exists on 64-bit; _GWL_STYLE fits a LONG anyway.
    _get_long = getattr(u32, "GetWindowLongPtrW", u32.GetWindowLongW)
    _get_long.restype = ctypes.c_ssize_t
    _get_long.argtypes = [wintypes.HWND, ctypes.c_int]
    w.GetWindowLongW = _get_long
    w.GetCursorPos = bind("GetCursorPos", wintypes.BOOL,
                          ctypes.POINTER(wintypes.POINT))
    w.SetCursorPos = bind("SetCursorPos", wintypes.BOOL, ctypes.c_int,
                          ctypes.c_int)
    _gwtpid = bind("GetWindowThreadProcessId", wintypes.DWORD, wintypes.HWND,
                   ctypes.POINTER(wintypes.DWORD))
    _text_len = bind("GetWindowTextLengthW", ctypes.c_int, wintypes.HWND)
    _text = bind("GetWindowTextW", ctypes.c_int, wintypes.HWND,
                 wintypes.LPWSTR, ctypes.c_int)

    def window_text_process(hwnd):
        n = _text_len(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        _text(hwnd, buf, n + 1)
        pid = wintypes.DWORD()
        _gwtpid(hwnd, ctypes.byref(pid))
        try:
            proc = psutil.Process(pid.value).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            proc = ""
        return buf.value.strip(), proc

    w.window_text_process = window_text_process

    def window_pid(hwnd):
        pid = wintypes.DWORD()
        _gwtpid(hwnd, ctypes.byref(pid))
        return pid.value

    w.window_pid = window_pid
    return w
