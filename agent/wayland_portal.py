"""The screen and input on a Linux Wayland session, through xdg-desktop-portal.

On Wayland the X11 tools the agent uses elsewhere (mss for capture,
pynput/XTest for input) only reach Xwayland windows. The desktop exposes
the real screen and input to unsandboxed apps through the portal's
RemoteDesktop + ScreenCast interfaces instead (KDE Plasma 6.6, GNOME):

- One portal session carries the keyboard, pointer and touchscreen devices
  plus a screen-cast stream of a monitor. Starting it shows the desktop's
  "Allow remote control" dialog the first time; the restore token it hands
  back (persist_mode 2, kept in ~/.config/sudo/portal_token) lets later
  sessions start without a dialog until the owner revokes it.
- Frames come from PipeWire through GStreamer (pipewiresrc ! videoconvert !
  appsink) as BGRx: the (h, w, 4) layout encode_frame already takes. The
  compositor draws the pointer into them (cursor_mode Embedded), so the
  agent must not paint its own.
- Input goes out as the portal's Notify* calls, sent without waiting for a
  reply (NO_REPLY_EXPECTED), so a burst of touchpad moves never stalls the
  event loop; D-Bus keeps them in order. Calls made before the session is
  up are queued and the session is started. Wayland's scroll axis points
  the other way from pynput's (positive = down).

Needs python3-gi, gir1.2-gstreamer-1.0, gstreamer1.0-pipewire (apt) and
dbus-fast (pip); the venv sees the system's gi (agent/linux/install.sh).
"""

import asyncio
import os
import platform
import secrets
from pathlib import Path

PORTAL = "org.freedesktop.portal.Desktop"
OBJ = "/org/freedesktop/portal/desktop"
RD = "org.freedesktop.portal.RemoteDesktop"
SC = "org.freedesktop.portal.ScreenCast"

KEYBOARD, POINTER, TOUCHSCREEN = 1, 2, 4
SOURCE_MONITOR = 1
CURSOR_EMBEDDED = 2
PERSIST_UNTIL_REVOKED = 2
START_TIMEOUT = 120.0     # the owner may take a while to click Allow
REQUEST_TIMEOUT = 30.0
MAX_PENDING = 256         # input queued while the session starts

# evdev button codes (linux/input-event-codes.h)
BUTTONS = {"left": 0x110, "right": 0x111, "middle": 0x112}

# The agent's key names (agent.SPECIAL_KEYS) as X keysyms.
KEYSYMS = {
    "enter": 0xff0d, "backspace": 0xff08, "tab": 0xff09, "esc": 0xff1b,
    "escape": 0xff1b, "space": 0x20, "up": 0xff52, "down": 0xff54,
    "left": 0xff51, "right": 0xff53, "delete": 0xffff, "home": 0xff50,
    "end": 0xff57, "pageup": 0xff55, "pagedown": 0xff56, "shift": 0xffe1,
    "ctrl": 0xffe3, "alt": 0xffe9, "cmd": 0xffeb, "win": 0xffeb,
    **{f"f{i}": 0xffbe + i - 1 for i in range(1, 13)},
    "media_play_pause": 0x1008ff14, "media_next": 0x1008ff17,
    "media_previous": 0x1008ff16, "media_volume_up": 0x1008ff13,
    "media_volume_down": 0x1008ff11, "media_volume_mute": 0x1008ff12,
}


class PortalError(RuntimeError):
    pass


def available():
    """A Wayland session where the GStreamer and D-Bus bindings import."""
    if platform.system() != "Linux":
        return False
    if os.environ.get("XDG_SESSION_TYPE") != "wayland" and not os.environ.get("WAYLAND_DISPLAY"):
        return False
    try:
        import dbus_fast  # noqa: F401
        _gst()
    except Exception:
        return False
    return True


_GST = None


def _gst():
    global _GST
    if _GST is None:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        _GST = Gst
    return _GST


def char_keysym(ch):
    """X keysym for one character: Latin-1 maps 1:1, the rest is 0x1000000+U."""
    if ch == "\n":
        return KEYSYMS["enter"]
    if ch == "\t":
        return KEYSYMS["tab"]
    code = ord(ch)
    if 0x20 <= code <= 0x7e or 0xa0 <= code <= 0xff:
        return code
    return 0x1000000 + code


class Portal:
    """The agent's one portal session. Lives on the agent's event loop;
    capture threads read frames through open_frames()."""

    def __init__(self, token_path):
        self.token_path = Path(token_path)
        self.rect = None          # {left, top, width, height} of the stream
        self.generation = 0       # bumps on every (re)start: readers reopen
        self._bus = None
        self._session = None      # session object path while started
        self._node = None         # PipeWire node id of the monitor stream
        self._fd = None           # PipeWire remote fd (readers dup it)
        self._lock = None
        self._responses = {}      # request path -> future
        self._pending = []        # input sent before the session was up
        self._starting = None

    @property
    def active(self):
        return self._session is not None

    # ------------------------------------------------------------ session

    async def ensure(self):
        """Starts the session if it isn't running (may show the Allow dialog)."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._session is None:
                await self._start()
        return self

    async def close(self):
        if self._lock is None:
            return
        async with self._lock:
            session = self._session
            self._mark_closed()
            if session is not None:
                await self._close_session(session)

    async def _connect(self):
        if self._bus is not None and self._bus.connected:
            return self._bus
        from dbus_fast import BusType, Message
        from dbus_fast.aio import MessageBus
        bus = await MessageBus(bus_type=BusType.SESSION, negotiate_unix_fd=True).connect()
        bus.add_message_handler(self._on_message)
        for rule in ("type='signal',interface='org.freedesktop.portal.Request',member='Response'",
                     "type='signal',interface='org.freedesktop.portal.Session',member='Closed'"):
            await bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                   interface="org.freedesktop.DBus", member="AddMatch",
                                   signature="s", body=[rule]))
        self._bus = bus
        return bus

    def _on_message(self, msg):
        from dbus_fast import MessageType
        if msg.message_type != MessageType.SIGNAL:
            return
        if msg.member == "Response":
            fut = self._responses.pop(msg.path, None)
            if fut is not None and not fut.done():
                fut.set_result(msg.body)
        elif msg.member == "Closed" and msg.path == self._session:
            # The owner stopped sharing (Plasma's tray icon) or the
            # compositor ended it: the next screen.start/input restarts.
            print("[agent] portal session closed by the desktop")
            self._mark_closed()

    def _mark_closed(self):
        self._session = None
        self._node = None
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        self.generation += 1

    async def _call(self, iface, member, signature, body):
        from dbus_fast import Message, MessageType
        reply = await self._bus.call(Message(destination=PORTAL, path=OBJ, interface=iface,
                                             member=member, signature=signature, body=body))
        if reply.message_type == MessageType.ERROR:
            raise PortalError(f"{member}: {reply.error_name}: {reply.body}")
        return reply

    async def _request(self, iface, member, signature, args, options, timeout=REQUEST_TIMEOUT):
        """The portal Request pattern: listen on the predictable handle
        path, call, then wait for its Response signal."""
        from dbus_fast import Variant
        token = "sudo" + secrets.token_hex(6)
        sender = self._bus.unique_name[1:].replace(".", "_")
        path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        fut = asyncio.get_running_loop().create_future()
        self._responses[path] = fut
        try:
            await self._call(iface, member, signature,
                             [*args, dict(options, handle_token=Variant("s", token))])
            code, results = await asyncio.wait_for(fut, timeout)
        finally:
            self._responses.pop(path, None)
        if code == 1:
            raise PortalError("screen sharing was not allowed on the PC")
        if code != 0:
            raise PortalError(f"the desktop refused screen sharing ({member})")
        return {k: getattr(v, "value", v) for k, v in results.items()}

    async def _start(self):
        from dbus_fast import Variant
        await self._connect()
        r = await self._request(RD, "CreateSession", "a{sv}", [],
                                {"session_handle_token": Variant("s", "sudo" + secrets.token_hex(4))})
        session = r["session_handle"]
        try:
            devices = {"types": Variant("u", KEYBOARD | POINTER | TOUCHSCREEN),
                       "persist_mode": Variant("u", PERSIST_UNTIL_REVOKED)}
            token = self._load_token()
            if token:
                devices["restore_token"] = Variant("s", token)
            await self._request(RD, "SelectDevices", "oa{sv}", [session], devices)
            await self._request(SC, "SelectSources", "oa{sv}", [session],
                                {"types": Variant("u", SOURCE_MONITOR),
                                 "multiple": Variant("b", False),
                                 "cursor_mode": Variant("u", CURSOR_EMBEDDED)})
            print("[agent] starting the portal session (the PC may ask to allow remote control)")
            r = await self._request(RD, "Start", "osa{sv}", [session, ""], {}, timeout=START_TIMEOUT)
            if r.get("restore_token"):
                self._save_token(r["restore_token"])
            streams = r.get("streams") or []
            if not streams:
                raise PortalError("the desktop shared no screen")
            node, props = streams[0]
            props = {k: getattr(v, "value", v) for k, v in props.items()}
            reply = await self._call(SC, "OpenPipeWireRemote", "oa{sv}", [session, {}])
            fd = reply.body[0]
            if reply.unix_fds and isinstance(fd, int) and 0 <= fd < len(reply.unix_fds):
                fd = reply.unix_fds[fd]
        except BaseException:
            await self._close_session(session)
            raise
        x, y = props.get("position") or (0, 0)
        w, h = props.get("size") or (0, 0)
        self._session, self._node, self._fd = session, int(node), int(fd)
        self.rect = {"left": int(x), "top": int(y), "width": int(w), "height": int(h)}
        self.generation += 1
        print(f"[agent] portal session up: screen {w}x{h} at {x},{y}, devices {r.get('devices')}")
        pending, self._pending = self._pending, []
        for send in pending:
            send()

    async def _close_session(self, session):
        from dbus_fast import Message
        try:
            await self._bus.call(Message(destination=PORTAL, path=session,
                                         interface="org.freedesktop.portal.Session", member="Close"))
        except Exception:
            pass

    def _load_token(self):
        try:
            return self.token_path.read_text().strip() or None
        except OSError:
            return None

    def _save_token(self, token):
        try:
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(token)
        except OSError as e:
            print(f"[agent] could not save the portal token: {e}")

    # -------------------------------------------------------------- frames

    def open_frames(self):
        """A PortalFrames reader for the current stream (call on the capture
        thread that will read it)."""
        if self._session is None:
            raise PortalError("the portal session is not running")
        return PortalFrames(self._fd, self._node)

    # --------------------------------------------------------------- input

    def _notify(self, build):
        """Sends one Notify* call built by build() -> (member, signature, args)
        once the session is up; starts it if needed. Fire-and-forget."""
        def send():
            if self._session is None:
                return
            from dbus_fast import Message, MessageFlag
            member, signature, args = build()
            fut = self._bus.send(Message(destination=PORTAL, path=OBJ, interface=RD, member=member,
                                         signature="oa{sv}" + signature,
                                         body=[self._session, {}, *args],
                                         flags=MessageFlag.NO_REPLY_EXPECTED))
            if fut is not None:
                fut.add_done_callback(lambda f: f.exception())  # never "unretrieved"
        if self._session is not None:
            send()
            return
        if len(self._pending) < MAX_PENDING:
            self._pending.append(send)
        if self._starting is None or self._starting.done():
            self._starting = asyncio.ensure_future(self.ensure())
            self._starting.add_done_callback(_log_start_failure)

    def move_rel(self, dx, dy):
        """dx/dy: normalized to the shared monitor (the agent's convention)."""
        self._notify(lambda: ("NotifyPointerMotion", "dd",
                              [float(dx) * self.rect["width"], float(dy) * self.rect["height"]]))

    def move_abs(self, fx, fy):
        fx, fy = min(max(float(fx), 0.0), 1.0), min(max(float(fy), 0.0), 1.0)
        self._notify(lambda: ("NotifyPointerMotionAbsolute", "udd",
                              [self._node, fx * (self.rect["width"] - 1),
                               fy * (self.rect["height"] - 1)]))

    def button(self, name, pressed):
        code = BUTTONS.get(name, BUTTONS["left"])
        self._notify(lambda: ("NotifyPointerButton", "iu", [code, 1 if pressed else 0]))

    def click(self, name="left", count=1):
        for _ in range(max(1, int(count))):
            self.button(name, True)
            self.button(name, False)

    def scroll(self, dx, dy):
        """pynput convention in (dy > 0 = up, dx > 0 = right), Wayland out."""
        if dy:
            self._notify(lambda: ("NotifyPointerAxisDiscrete", "ui", [0, -int(dy)]))
        if dx:
            self._notify(lambda: ("NotifyPointerAxisDiscrete", "ui", [1, int(dx)]))

    def keysym(self, sym, pressed):
        self._notify(lambda: ("NotifyKeyboardKeysym", "iu", [int(sym), 1 if pressed else 0]))

    def key(self, name):
        """An agent key name (KEYSYMS) or one character: press + release."""
        sym = KEYSYMS.get(name.lower()) if len(name) > 1 else char_keysym(name)
        if sym is None:
            raise ValueError(f"unknown key: {name}")
        self.keysym(sym, True)
        self.keysym(sym, False)

    def text(self, text):
        for ch in text:
            sym = char_keysym(ch)
            self.keysym(sym, True)
            self.keysym(sym, False)


def _log_start_failure(task):
    if not task.cancelled() and task.exception() is not None:
        print(f"[agent] portal session failed to start: {task.exception()}")


def _pipeline(fd, node):
    """GStreamer pipeline for the portal stream (tests swap the source)."""
    return (f"pipewiresrc fd={fd} path={int(node)} always-copy=true "
            "! videoconvert ! video/x-raw,format=BGRx "
            "! appsink name=sink max-buffers=1 drop=true sync=false")


class PortalFrames:
    """Reads the portal's monitor stream on one capture thread.
    grab(timeout) returns the newest frame as a (h, w, 4) BGRx uint8 array,
    or None when nothing new arrived within the timeout (the compositor only
    sends frames when the screen changes)."""

    def __init__(self, fd, node):
        Gst = _gst()
        self._fd = os.dup(fd)
        try:
            self._pipe = Gst.parse_launch(_pipeline(self._fd, node))
            self._sink = self._pipe.get_by_name("sink")
            if self._pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise PortalError("the PipeWire screen stream would not start")
        except Exception:
            self.close()
            raise

    def grab(self, timeout_s):
        Gst = _gst()
        import numpy as np
        sample = self._sink.emit("try-pull-sample", int(timeout_s * Gst.SECOND))
        if sample is None:
            msg = self._pipe.get_bus().pop_filtered(Gst.MessageType.ERROR)
            if msg is not None:
                err, _ = msg.parse_error()
                raise PortalError(f"screen stream error: {err.message}")
            return None
        caps = sample.get_caps().get_structure(0)
        w, h = caps.get_value("width"), caps.get_value("height")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            data = np.frombuffer(info.data, np.uint8)
            stride = len(data) // h
            return data[:stride * h].reshape(h, stride)[:, :w * 4].reshape(h, w, 4).copy()
        finally:
            buf.unmap(info)

    def close(self):
        pipe = getattr(self, "_pipe", None)
        if pipe is not None:
            pipe.set_state(_gst().State.NULL)
            self._pipe = None
        if getattr(self, "_fd", None) is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
