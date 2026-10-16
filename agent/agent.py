"""pc-remote PC agent.

Runs on the PC you want to control. Connects OUTBOUND to the relay server,
so it works behind NAT / on any network without port forwarding.

First run generates a persistent pairing code (stored in config.json next to
this file). Enter that code in the mobile app once; after that the phone
reconnects with one tap.

Usage:
    python agent.py                 # uses config.json / default relay
    RELAY_URL=wss://example.com python agent.py
"""

import asyncio
import base64
import io
import math
import json
import os
import platform
import re
import secrets
import shutil
import socket
import string
import subprocess
import sys
import threading
import time
import uuid
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlparse

if platform.system() == "Windows":
    # Hybrid (NVIDIA Optimus) laptops: DXGI must list the panel under the
    # integrated GPU that drives it, or Desktop Duplication (the 60 fps
    # screen capture, dxgi_capture.py) fails with DXGI_ERROR_UNSUPPORTED.
    # Read when the process creates its first DXGI factory, so it is set
    # before anything can; ignored on PCs without Optimus. Overrides an
    # inherited dGPU value (seen here: 0x810000001).
    os.environ["SHIM_MCCOMPAT"] = "0x800000000"

import mss
import psutil
import pyperclip
import websockets
from PIL import Image, ImageDraw
try:
    from pynput.keyboard import Controller as KbController, Key
    from pynput.mouse import Button, Controller as MouseController
except Exception as _input_error:  # Linux with no X display reachable
    raise SystemExit(
        f"[agent] can't reach the desktop for mouse/keyboard control "
        f"({_input_error}). Start the agent inside the desktop session "
        "(the systemd user unit from agent/linux/install.sh does).")

from brightness import BrightnessControl
import kdeapps
import wayland_portal
from remote_access import is_lan_address

if getattr(sys, "frozen", False) and sys.stdout is None:
    # noconsole exe: print() would crash on a None stdout
    sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")

HERE = Path(__file__).resolve().parent
if getattr(sys, "frozen", False):
    # PyInstaller onefile: __file__ lives in a temp extraction dir that is
    # deleted on exit — never store persistent state there.
    HERE = Path(sys.executable).resolve().parent

_MUTEX_HANDLE = None


def ensure_single_instance():
    """Windows named mutex: only one PCocket agent may run at a time.
    A second process (startup shortcut + manual launch, rebuild races, ...)
    exits quietly instead of fighting over the relay registration."""
    global _MUTEX_HANDLE
    if platform.system() != "Windows":
        # Linux/macOS: an exclusive lock on a file in the config folder,
        # released by the OS when the process dies.
        import fcntl
        path = _default_config_path().parent / "agent.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        _MUTEX_HANDLE = open(path, "w")
        try:
            fcntl.flock(_MUTEX_HANDLE, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True
    import ctypes
    _MUTEX_HANDLE = ctypes.windll.kernel32.CreateMutexW(
        None, False, "Global\\PCocketAgentMutex")
    return bool(_MUTEX_HANDLE) and \
        ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def _default_config_path():
    if platform.system() == "Windows":
        base = Path(os.environ.get("APPDATA", str(Path.home())))
        new = base / "Sudo" / "config.json"
        if not new.exists():
            # one-time migration chain: PCocket -> Rein -> Sudo
            for old_name in ("Rein", "PCocket"):
                old = base / old_name / "config.json"
                if old.exists():
                    try:
                        new.parent.mkdir(parents=True, exist_ok=True)
                        new.write_text(old.read_text())
                    except OSError:
                        return old
                    break
        return new
    return Path.home() / ".config" / "sudo" / "config.json"


# A config.json next to the script/exe wins (dev flow); otherwise use the
# per-user location so the pairing code survives reboots and rebuilds.
CONFIG_PATH = (HERE / "config.json") if (HERE / "config.json").exists() \
    else _default_config_path()
DEFAULT_RELAY = "ws://127.0.0.1:8080"
CHUNK = 256 * 1024  # fs transfer chunk size
DISCOVERY_PORT = 47809  # UDP beacon the phone app listens on

mouse = MouseController()
kb = KbController()
# Wayland: input goes through the agent's portal session instead of
# pynput, which only reaches Xwayland windows there (set by Agent).
PORTAL = None

# ---------------------------------------------------------------- config

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def load_config():
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            # utf-8-sig: editors (and PowerShell's Set-Content -Encoding
            # utf8) add a BOM, which plain json.loads rejects.
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if not isinstance(cfg, dict):
                raise ValueError("config.json is not a JSON object")
        except Exception as e:
            # Never silently replace an unreadable config: it holds the
            # pairing code and every phone's trust token. Keep a copy.
            print(f"[agent] config.json unreadable ({e}); "
                  "saved a copy as config.json.bad")
            try:
                shutil.copyfile(CONFIG_PATH,
                                CONFIG_PATH.with_suffix(".json.bad"))
            except OSError:
                pass
            cfg = {}
    changed = False
    if not cfg.get("device_id"):
        cfg["device_id"] = str(uuid.uuid4())
        changed = True
    if not cfg.get("code"):
        cfg["code"] = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        changed = True
    if not cfg.get("name"):
        cfg["name"] = platform.node() or "My PC"
        changed = True
    cfg["relay"] = os.environ.get("RELAY_URL") or cfg.get("relay") or DEFAULT_RELAY
    if changed or not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    return cfg


def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# ---------------------------------------------------------------- screen

MONITORS_TTL = 3.0  # seconds a queried monitor list is trusted
_monitors_lock = threading.Lock()
_monitors_cache = None  # (monotonic time, [mss monitor dicts])


def wayland_session():
    """True on a Linux Wayland session: X11 capture and input (mss, pynput)
    only reach Xwayland windows there. SUDO_FORCE_X11=1 overrides (tests,
    or a compositor that mirrors the desktop into Xwayland)."""
    if platform.system() != "Linux" or os.environ.get("SUDO_FORCE_X11"):
        return False
    return (os.environ.get("XDG_SESSION_TYPE") == "wayland"
            or bool(os.environ.get("WAYLAND_DISPLAY")))


def new_mss():
    """mss >= 10.2 renamed the factory to mss.MSS (mss.mss() is deprecated
    and warns); 9.x only has mss.mss()."""
    return (getattr(mss, "MSS", None) or mss.mss)()


def list_monitors(max_age=MONITORS_TTL):
    """mss().monitors: [0] is the whole virtual screen, [1:] each monitor.

    Re-queried at most every `max_age` seconds. Monitors come and go at
    runtime (a virtual display for the phone, a dock), so caching forever
    is wrong — but a fresh mss instance per input event caused
    intermittent failures under rapid touchpad input, so not per call
    either. Callable from any thread."""
    global _monitors_cache
    with _monitors_lock:
        now = time.monotonic()
        if _monitors_cache is None or now - _monitors_cache[0] > max_age:
            with new_mss() as sct:
                _monitors_cache = (now, [dict(m) for m in sct.monitors])
        return _monitors_cache[1]


def primary_monitor(monitors):
    """The monitor whose rect starts at (0,0): Windows anchors the virtual
    desktop's origin on the primary display, while mss's index 1 is merely
    the first monitor the OS enumerated."""
    for mon in monitors[1:]:
        if mon["left"] == 0 and mon["top"] == 0:
            return mon
    for mon in monitors[1:]:
        if mon.get("is_primary"):
            return mon
    return monitors[1] if len(monitors) > 1 else monitors[0]


def monitor_rect(mon):
    return {k: int(mon[k]) for k in ("left", "top", "width", "height")}


def screen_size():
    """Primary monitor size in pixels (cached briefly, see list_monitors)."""
    mon = primary_monitor(list_monitors())
    return mon["width"], mon["height"]


_cv_modules = None


def _cv():
    """(cv2, numpy), or (None, None) when OpenCV is missing (or too old to
    set JPEG subsampling). Imported lazily — on the capture thread, off the
    event loop — because only screen streaming needs it and the import
    costs ~0.3 s. OpenCV resizes 5-10x faster than PIL with equal or better
    sharpness, and unlike PIL it releases the GIL while converting and
    encoding — PIL holding it for ~20 ms per 1080p frame stretched the
    concurrent grab from ~36 to ~44 ms (scripts/bench_capture.py). Same
    libjpeg-turbo underneath: identical JPEG bytes for the same settings.
    PIL stays as the fallback."""
    global _cv_modules
    if _cv_modules is None:
        try:
            import cv2
            import numpy
            if not hasattr(cv2, "IMWRITE_JPEG_SAMPLING_FACTOR"):
                raise ImportError("OpenCV < 4.5.5")
            # OpenCV's worker pool spins between calls: at 60 fps it burned
            # 0.7-1.3 cores here for a ~3 ms faster resize. One thread
            # resizes a frame in ~4 ms, well inside a 16.7 ms frame.
            cv2.setNumThreads(1)
            _cv_modules = (cv2, numpy)
        except ImportError:
            _cv_modules = (None, None)
    return _cv_modules


def cursor_polygon(mon, pos, width, height):
    """Arrow-pointer outline for a cursor at screen position pos, scaled
    into a width x height frame of monitor mon; None when the cursor
    is not on that monitor (or pos is None: no cursor to draw)."""
    if pos is None:
        return None
    cx, cy = pos
    x = (cx - mon["left"]) * width / mon["width"]
    y = (cy - mon["top"]) * height / mon["height"]
    if not (0 <= x < width and 0 <= y < height):
        return None
    s = max(10, width // 90)
    return [(x, y), (x, y + s * 1.4), (x + s * 0.38, y + s * 1.05),
            (x + s * 0.58, y + s * 1.5), (x + s * 0.78, y + s * 1.42),
            (x + s * 0.58, y + s * 0.98), (x + s * 0.95, y + s * 0.95)]


def draw_cursor(img, mon, pos):
    """mss does not capture the pointer, so paint it on the frame ourselves —
    the phone user needs to see where the PC cursor is. pos is the cursor
    position read when the frame was grabbed; it is only drawn when it lies
    inside the streamed monitor mon. img is a PIL image or an OpenCV
    BGRA array."""
    if isinstance(img, Image.Image):
        pts = cursor_polygon(mon, pos, img.width, img.height)
        if pts:
            ImageDraw.Draw(img).polygon(pts, fill="white", outline="black")
        return
    cv2, np = _cv()
    pts = cursor_polygon(mon, pos, img.shape[1], img.shape[0])
    if pts:
        poly = np.array([pts], dtype=np.int32)
        cv2.fillPoly(img, poly, (255, 255, 255, 255))
        cv2.polylines(img, poly, True, (0, 0, 0, 255), 1, cv2.LINE_AA)


def frame_thumb(shot, pos):
    """Fingerprint of a grabbed frame: a 32x18 grid of sampled pixels plus
    the cursor position quantized to 2px. An unchanged fingerprint skips the
    frame before any resize/encode/send, so an idle desktop costs almost no
    CPU or bandwidth, while cursor-only movement still counts as a change."""
    w, h = shot.size
    _, np = _cv()
    if np is not None:
        px = np.frombuffer(shot.raw, np.uint8).reshape(h, w, 4)
        sy, sx = max(1, h // 18), max(1, w // 32)
        grid = px[sy // 2::sy, sx // 2::sx, :3].tobytes()
    else:
        grid = Image.frombuffer("RGB", (w, h), shot.raw, "raw", "BGRX", 0,
                                1).resize((32, 18), Image.NEAREST).tobytes()
    # signed: monitors left of / above the primary have negative coords.
    # pos None: portal frames, whose pixels already include the pointer.
    qx, qy = (int(pos[0]) // 2, int(pos[1]) // 2) if pos is not None else (0, 0)
    return (grid + qx.to_bytes(4, "little", signed=True)
            + qy.to_bytes(4, "little", signed=True))


class DuplicatedShot:
    """A Desktop Duplication image (numpy BGRA) dressed as an mss
    ScreenShot (.raw, .size) for frame_thumb/encode_frame. `shared`: the
    capture reuses its buffers and re-hands the last one out, so
    encode_frame must not paint the cursor into it."""

    shared = True

    def __init__(self, img):
        self.raw = img
        self.size = (img.shape[1], img.shape[0])


def encode_frame(shot, mon, pos, width, quality, subsampling):
    """Resize a grabbed frame to width (never up), draw the cursor and
    JPEG-encode it with subsampling "420"/"422"/"444". Returns (jpeg,
    w, h). Runs on a streamer's encode thread, concurrently with the next
    grab on the capture thread."""
    w0, h0 = shot.size
    width = max(1, min(int(width), w0))
    height = max(1, round(h0 * width / w0))
    cv2, np = _cv()
    if cv2 is not None:
        # shot.raw is a private bytearray copy (mss copies the DIB out), so
        # the cursor may be painted straight into it at native width.
        img = np.frombuffer(shot.raw, np.uint8).reshape(h0, w0, 4)
        if width == w0 and getattr(shot, "shared", False):
            img = img.copy()  # never paint the cursor into a reused buffer
        if width != w0:
            # INTER_AREA (box average) from 2x down: alias-free and the
            # cheapest. Under 2x it softens text, where INTER_CUBIC is
            # sharper (bench at 1280w: PSNR 33.0 vs 32.7) for ~2.5 ms —
            # PIL's LANCZOS needs ~30 ms for the same result.
            interp = cv2.INTER_AREA if w0 >= 2 * width else cv2.INTER_CUBIC
            img = cv2.resize(img, (width, height), interpolation=interp)
        draw_cursor(img, mon, pos)
        factor = {"420": cv2.IMWRITE_JPEG_SAMPLING_FACTOR_420,
                  "422": cv2.IMWRITE_JPEG_SAMPLING_FACTOR_422,
                  "444": cv2.IMWRITE_JPEG_SAMPLING_FACTOR_444}[subsampling]
        # BGRA in: the encoder drops alpha itself, no separate conversion.
        ok, buf = cv2.imencode(".jpg", img, [
            cv2.IMWRITE_JPEG_QUALITY, int(quality),
            cv2.IMWRITE_JPEG_SAMPLING_FACTOR, factor])
        if not ok:
            raise RuntimeError("JPEG encode failed")
        return buf.tobytes(), width, height
    img = Image.frombuffer("RGB", (w0, h0), shot.raw, "raw", "BGRX", 0, 1)
    if w0 // width >= 2:
        img = img.reduce(w0 // width)  # cheap box pre-pass, like AREA
    if img.size != (width, height):
        img = img.resize((width, height), Image.BICUBIC)
    draw_cursor(img, mon, pos)
    buf = io.BytesIO()
    # optimize=False: Huffman optimisation costs a second pass for ~5%.
    img.save(buf, "JPEG", quality=quality,
             subsampling=SUBSAMPLING[subsampling], optimize=False)
    return buf.getvalue(), width, height


# screen.start presets, tuned with scripts/bench_capture.py on the dev
# laptop (Ryzen 5 5600H, 1080p panel): persistent-mss grab ~22 ms, OpenCV
# resize 1-3 ms, JPEG encode 1-5 ms (1080p 4:4:4 q90: ~11 ms). Grab and
# encode run on separate threads, so every preset fits its 1/fps budget
# (the pipeline is grab-bound at ~45 fps). max_width None = the monitor's
# native width. Explicit fps / quality / max_width / subsampling override.
SCREEN_PRESETS = {
    "saver": {"fps": 12, "quality": 55, "max_width": 960,
              "subsampling": "420"},
    "balanced": {"fps": 20, "quality": 70, "max_width": 1280,
                 "subsampling": "420"},
    "hd": {"fps": 24, "quality": 80, "max_width": None, "subsampling": "420"},
    # 4:4:4 keeps coloured text (syntax highlighting, red/blue UI) crisp;
    # 4:2:0 halves the chroma resolution and smears it.
    "max": {"fps": 30, "quality": 90, "max_width": None,
            "subsampling": "444"},
}
# No preset and no params: the pre-preset defaults, so old apps get exactly
# what they always got.
SCREEN_DEFAULTS = {"fps": 10, "quality": 50, "max_width": 960,
                   "subsampling": "420"}
# `auto` (width, quality) rungs, lowest first. It starts on the top rung
# (= balanced) for phones on the LAN, on AUTO_START_INTERNET for phones
# from the internet, walks down when the client's link backs up or can't
# carry the frames, and back up after AUTO_UP_AFTER seconds of headroom.
# The two rungs under 640w are for mobile data at 1-2 Mbit/s, where even a
# 640w frame takes 150 ms to arrive.
AUTO_LADDER = [(360, 35), (480, 40), (640, 45), (800, 50), (960, 55),
               (1120, 62), (1280, 70)]
# 800w: a 1280w frame (60-150 KB) takes a second on 1 Mbit/s mobile data,
# so internet phones start mid-ladder and climb once the link shows it can.
AUTO_START_INTERNET = 3
# Queueing alone (the busy rule below) never takes the stream under 640w,
# the ladder's floor before the small rungs: those are for links whose
# measured rate can't carry 640w (the bandwidth rule).
AUTO_BUSY_FLOOR = 2
# The link is judged by how much of the last AUTO_BUSY_WINDOW seconds the
# stream spent blocked in sends — not by single sends: the kernel absorbs a
# send buffer's worth instantly and then blocks for seconds, so a saturated
# link shows as one very slow send among fast ones.
AUTO_BUSY_WINDOW = 2.0
AUTO_DOWN_BUSY = 0.6   # blocked >60% of the window: step a rung down
AUTO_UP_BUSY = 0.25    # under 25% for AUTO_UP_AFTER s (and 20+ frames): up
AUTO_UP_AFTER = 6.0
# Bandwidth rule (ScreenStreamer._rate_sample / _check_rate): on a slow
# link one big frame's transfer IS most of the round trip, so nothing looks
# queued and the busy rule never fires. The frames/s the link carries at a
# rung = measured delivery rate / that rung's frame size. Under
# AUTO_MIN_FPS while the link sets the pace, jump down to the biggest rung
# that carries AUTO_TARGET_FPS; step up only to a rung that would still
# carry AUTO_UP_FPS. AUTO_RATE_WINDOW: seconds of streaming (idle time
# doesn't count) the delivery-rate max filter remembers.
AUTO_MIN_FPS = 12
AUTO_TARGET_FPS = 16
AUTO_UP_FPS = 20
AUTO_RATE_WINDOW = 3.0
SUBSAMPLING = {"420": 2, "422": 1, "444": 0}  # PIL's values for each


def screen_settings(msg, native_width, internet=False):
    """Effective JPEG stream settings for a screen.start message: the preset
    (or the legacy defaults without one), explicit params on top, clamped.
    `internet`: the phone connects from outside the LAN, so `auto` starts on
    AUTO_START_INTERNET instead of the top rung (`level`).
    Raises ValueError for an unknown preset or subsampling."""
    preset = str(msg.get("preset") or "").lower() or None
    explicit = {k: msg[k] for k in ("fps", "quality", "max_width",
                                    "subsampling") if msg.get(k) is not None}
    adaptive = False
    if preset is None:
        base = dict(SCREEN_DEFAULTS)
    elif preset == "auto":
        # The top rung is the cap the ladder climbs to, wherever it starts.
        width, quality = AUTO_LADDER[-1]
        base = {"fps": SCREEN_PRESETS["balanced"]["fps"], "quality": quality,
                "max_width": width, "subsampling": "420"}
        # A pinned width or quality is a fixed choice, not a starting point.
        adaptive = "quality" not in explicit and "max_width" not in explicit
        if not adaptive:
            preset = "custom"
    elif preset in SCREEN_PRESETS:
        base = dict(SCREEN_PRESETS[preset])
    else:
        raise ValueError(f"unknown screen preset: {preset}")
    base.update(explicit)
    sub = str(base["subsampling"]).replace(":", "")
    if sub not in SUBSAMPLING:
        raise ValueError(f"unknown subsampling: {base['subsampling']}")
    width = native_width if base["max_width"] is None \
        else max(320, min(int(base["max_width"]), 3840))
    return {
        "preset": preset or "custom",
        "fps": max(1, min(int(base["fps"]), 60)),
        "quality": max(20, min(int(base["quality"]), 95)),
        "max_width": min(width, native_width),  # never upscale
        "subsampling": sub,
        "adaptive": adaptive,
        "level": AUTO_START_INTERNET if internet else len(AUTO_LADDER) - 1,
    }


class ScreenStreamer:
    """Streams JPEG frames of one monitor to one client until stopped.

    A three-stage pipeline: the capture thread grabs frame N+1 while the
    encode thread resizes/encodes frame N and the event loop sends frame
    N-1. The capture thread keeps ONE mss instance for the stream's life
    (mss objects are thread-affine, and re-creating one per frame cost
    ~10 ms of every grab). At most two frames are past the grab stage at
    once, so a slow link throttles capture instead of queueing stale frames.

    binary=True sends raw PJF1 packets (no base64, ~33% smaller); binary=False
    sends legacy base64 JSON events. Frames identical to the last one passed
    on are skipped before any resize/encode — an idle desktop costs almost
    no bandwidth or CPU.

    settings come from screen_settings(); with `adaptive` (the auto preset)
    the width/quality walk AUTO_LADDER with this client's link.
    """

    def __init__(self, agent, client_id, settings, rect, monitor="primary",
                 binary=False, acks=False, client_cursor=False):
        self.agent = agent
        self.client_id = client_id
        self.preset = settings["preset"]
        self.fps = settings["fps"]
        self.quality = settings["quality"]
        self.max_width = settings["max_width"]
        self.subsampling = settings["subsampling"]
        self.adaptive = settings["adaptive"]
        self.rect = rect             # capture rect, re-resolved periodically
        self.monitor = monitor       # the screen.start spec it came from
        self.binary = binary
        self._rect_at = time.monotonic()
        self._sct = None             # capture thread's mss instance
        self._dxgi = None            # capture thread's DXGICapture
        self._dxgi_errors = 0        # consecutive Desktop Duplication errors
        self._dxgi_retry_at = 0.0    # mss until then, after repeated errors
        self._base = None            # newest full image (DXGI, GDI, portal)
        self._base_rect = None       # the rect _base was captured for
        # Wayland: frames come from the agent's portal session instead.
        portal = getattr(agent, "portal", None)
        self._portal = portal if portal is not None and wayland_session() \
            else None
        self._pframes = None         # capture thread's PortalFrames
        self._pgen = None            # portal session generation it reads
        # screen.start {cursor: "client"}: the phone draws the pointer
        # itself from `cursor` events (Agent.send_cursor), so it moves the
        # moment the finger does, whatever the link's latency. Frames then
        # carry no pointer, and pointer-only movement sends no frame. Not
        # on the portal, whose frames already contain the pointer.
        self.client_cursor = bool(client_cursor) and self._portal is None
        # Flow control (screen.start {ack: true}): the phone acks every frame
        # it receives, and at most MAX_UNACKED frames are ever out (grabbed
        # but not yet acked). Nothing can queue anywhere between the PC
        # screen and the phone's, however slow or bursty the Wi-Fi.
        self.acks = acks
        self._outstanding = 0
        self._ack_event = asyncio.Event()
        # How many frames may be out: grows with the link's round trip so
        # a slow route (the internet tunnel, 100-300 ms) still gets full
        # fps, and shrinks when acks come back later than the route's
        # base round trip (frames queueing). See _rtt_sample.
        self._window = self.MIN_UNACKED
        self._window_at = 0.0
        # Frames sent and not acked yet, oldest first: _Sent records (send
        # time, bytes, and the delivery state when it left, for the rate).
        self._sent_at = deque()
        # The oldest _sent_at entries written off after ACK_TIMEOUT: their
        # acks still come (a stalled mobile link delivers them late) and
        # are absorbed, never paired with newer frames.
        self._written_off = 0
        # Acks still owed to the stream this one replaced on the same
        # socket (Agent.h_screen_start), absorbed until _stale_until.
        self._stale_acks = 0
        self._stale_until = 0.0
        self._rtts = deque()         # (time, seconds) ack round trips
        self._min_rtt = None         # base round trip, last RTT_WINDOW s
        self._srtt = None            # smoothed round trip
        self._queue = 0.0            # frames queued on the route (Vegas)
        # Delivery rate (see _rate_sample): bytes acked so far, when the
        # last ack came (or the send that restarted an empty pipe), the
        # send time of the newest frame acked, the streaming clock samples
        # age on, and the max filter's samples / its value (bytes/s).
        self._delivered = 0
        self._delivered_at = 0.0
        self._first_sent_at = 0.0
        self._last_ack = None
        self._flight_acked = False   # an ack came since the pipe was empty
        self._limited_since = None   # acks link-limited since (_check_rate)
        self._late = False           # acks written off, not caught up yet
        self._active = 0.0
        self._rates = deque()        # (active time, bytes/s), max first
        self._rate = None
        # Probing the base round trip (see _rtt_sample): when the next
        # probe is due, and the window to go back to after one.
        self._probe_at = time.monotonic() + self.PROBE_EVERY
        self._probe_until = 0.0
        self._probe_window = None
        self._last_sent = 0.0        # when the last frame went out
        self._stats_at = time.monotonic()
        self._stats_frames = 0
        self._last_thumb = None      # capture thread only
        # Set when a frame was dropped after its thumb was taken: the next
        # grab goes through even if unchanged, so the drop is never final.
        self._force = False
        self._wake = asyncio.Event()  # any cursor movement nudges a frame
        # Client cursor: a frame is owed this soon after input or pointer
        # movement (see wake).
        self._refresh_at = 0.0
        self._refresh_crc = None      # checksum of the last refresh passed on
        self._hot_until = 0.0         # stay at full rate briefly after change
        self._grab_pool = ThreadPoolExecutor(1, "sudo-screen-grab")
        self._enc_pool = ThreadPoolExecutor(1, "sudo-screen-encode")
        self._errors = 0
        # auto preset state: current AUTO_LADDER rung + link bookkeeping
        self._level = settings.get("level", len(AUTO_LADDER) - 1)
        self._size = None            # average frame bytes at this rung
        self._size_fresh = False     # _size is a guess (see _set_level)
        self._sends = deque()        # (end time, seconds blocked) per frame
        self._good_since = time.monotonic()
        self._good_frames = 0
        self._level_at = time.monotonic()
        self._closed = False
        self.task = None

    def frame_size(self):
        """(w, h) of the frames this stream is sending right now."""
        width = max(1, min(self._current()[0], self.rect["width"]))
        return width, max(1, round(self.rect["height"] * width
                                   / self.rect["width"]))

    # Client cursor: pointer moves no longer change the frame thumb, and
    # its sparse grid can miss small changes (a hover highlight, a text
    # selection, a checkbox). So input or pointer movement owes one forced
    # frame this long after: ~12/s at most while moving, always after the
    # last move.
    REFRESH_AFTER = 0.08

    def wake(self):
        if self.client_cursor and not self._refresh_at:
            self._refresh_at = time.monotonic() + self.REFRESH_AFTER
        self._wake.set()

    def unacked_sent(self):
        """Frames this stream sent whose acks haven't come yet (written off
        or not): a replacement stream on the same socket gets them first."""
        return len(self._sent_at) + self._stale_acks

    # Frames that may be out at once with acks on: at least one being shown
    # on the phone while the next travels, and up to MAX_UNACKED when the
    # round trip is long (see _rtt_sample). ACK_TIMEOUT: acks that stop
    # coming (an app bug) resync the count instead of freezing the stream.
    MIN_UNACKED = 2
    MAX_UNACKED = 8
    ACK_TIMEOUT = 1.0
    RTT_WINDOW = 10.0   # the base round trip is the minimum over this long
    # Log line while frames flow (journalctl diagnostics); the
    # SUDO_STATS_EVERY environment variable overrides it for test runs.
    STATS_EVERY = float(os.environ.get("SUDO_STATS_EVERY") or 15.0)

    def ack(self):
        """The phone received a frame (screen.ack). Acks come in frame
        order, so the oldest unacked send time is this frame's."""
        now = time.monotonic()
        if self._stale_acks > 0:
            if now < self._stale_until:
                self._stale_acks -= 1  # the replaced stream's frame
                return
            self._stale_acks = 0
        if self._written_off > 0:
            # A frame written off after ACK_TIMEOUT: already out of
            # _outstanding, and its round trip includes the stall. Its bytes
            # did arrive, so they count towards the delivery rate: on a link
            # too slow for its frames, late acks are all there is.
            self._written_off -= 1
            if self._sent_at:
                self._rate_sample(self._sent_at.popleft(), now)
                self._check_rate(now)
            return
        if self._sent_at:
            self._late = False  # caught up: every written-off ack is in
            sent = self._sent_at.popleft()
            self._rtt_sample(now - sent.t, now)
            self._rate_sample(sent, now)
            self._check_rate(now)
        if self._outstanding > 0:
            self._outstanding -= 1
        self._ack_event.set()

    class _Sent:
        """One frame on its way: when it went out, its size, and the
        delivery state then (see _rate_sample). `limited`: it filled the
        window, so its ack measures the link, not how often the screen
        changes."""
        __slots__ = ("t", "size", "delivered", "delivered_at",
                     "first_sent", "limited")

        def __init__(self, t, size, delivered, delivered_at, first_sent,
                     limited):
            self.t = t
            self.size = size
            self.delivered = delivered
            self.delivered_at = delivered_at
            self.first_sent = first_sent
            self.limited = limited

    def _sent(self, t, size):
        """A frame of `size` bytes goes out now (t): its _Sent record. An
        empty pipe restarts the delivery clock, so the idle time before
        this frame doesn't count as slow delivery. Frames sent before the
        first ack of such a fresh flight don't count as filling the window
        (as in BBR): their samples include the trip of the flight's first
        frame into the empty route, not just the link's pace."""
        if not self._sent_at:
            self._delivered_at = self._first_sent_at = t
            self._flight_acked = False
        return self._Sent(t, size, self._delivered, self._delivered_at,
                          self._first_sent_at,
                          self._outstanding >= self._window
                          and self._flight_acked and not self.probing(t))

    # Shortest interval a delivery-rate sample may span: under it, timer
    # noise dominates.
    RATE_MIN_INTERVAL = 0.005
    # The clock the rate filter ages on runs only while frames fill the
    # window, and by at most this much between two acks (a stall).
    RATE_IDLE_STEP = 0.5

    def _rate_sample(self, sent, now):
        """One acked frame's delivery-rate sample, as BBR takes them: the
        bytes acked from the last ack before the frame went out up to its
        own ack, over that time — or over the time those bytes took to send,
        if longer, so acks arriving in a bunch can't fake a fast link.
        Frame sizes are taken at send time.

        The estimate (self._rate, bytes/s) is the max over the last
        AUTO_RATE_WINDOW s of window-limited streaming. Only frames that
        filled the window set it and age it: a frame sent into an idle link
        (the screen changed once) measures the round trip more than the
        link, so such samples may only raise it, and a still or quiet
        screen, however long, never makes the link look slow. A stall
        (acks late, see _acks_late) only lowers samples, which the max
        ignores while faster ones are fresh."""
        self._delivered += sent.size
        limited = sent.limited
        if limited and self._last_ack is not None:
            self._active += min(now - self._last_ack, self.RATE_IDLE_STEP)
        self._last_ack = now
        self._delivered_at = now
        self._first_sent_at = sent.t
        self._flight_acked = True
        interval = max(now - sent.delivered_at, sent.t - sent.first_sent)
        if interval < self.RATE_MIN_INTERVAL:
            return
        rate = (self._delivered - sent.delivered) / interval
        rates = self._rates
        while rates and self._active - rates[0][0] > AUTO_RATE_WINDOW:
            rates.popleft()
        if limited or not rates or rate > rates[0][1]:
            # Monotonic deque: an older sample at or below a newer one can
            # never be the max again.
            while rates and rates[-1][1] <= rate:
                rates.pop()
            rates.append((self._active, rate))
        self._rate = rates[0][1] if rates else None

    def _link_fps(self, level=None):
        """Frames/s the link carries at `level` (default: the current rung):
        the measured delivery rate over that rung's frame size. None until
        both are known."""
        size = self._size_at(self._level if level is None else level)
        if self._rate is None or not size:
            return None
        return self._rate / size

    def _size_at(self, level):
        """Expected frame bytes at `level`: the recent average at the
        current rung, scaled by the rungs' relative cost (see _cost)."""
        if self._size is None:
            return None
        return self._size * self._cost(level) / self._cost(self._level)

    def _cost(self, level):
        """Relative JPEG size of a rung's frames: ~pixels^0.8 (a smaller
        frame packs more detail into each pixel) x quality^0.7. Measured
        here on two desktops (a terminal and IDE; a code editor, text page
        and video), the real ratios along the ladder are within 20% of it.
        The rung the stream lands on measures its own frames, so a miss is
        corrected a round trip later."""
        width, quality = AUTO_LADDER[level]
        width = max(1, min(width, self.max_width, self.rect["width"]))
        return width ** 1.6 * quality ** 0.7

    def _link_limited(self):
        """The link, not the window, sets the pace: a frame or more waits in
        a queue on the route (see _rtt_sample), so the link never idles and
        the delivery rate is its rate — or acks come later than
        ACK_TIMEOUT (one frame takes over a second: a fast link that merely
        stalled still shows its rate in the filter, so it doesn't jump). A
        window still growing towards the round trip (a long route found
        fresh, or after idle) leaves the link idle part of the time: its
        acks measure window / round trip, not the link, and must not read
        as a slow link."""
        return self._late or self._queue >= self.QUEUE_LOW

    def _check_rate(self, now):
        """auto preset, on every ack: when the link carries fewer than
        AUTO_MIN_FPS frames/s at this rung and it is the link that limits
        (frames have queued for a round trip, see _link_limited: a moment's
        queue is just a window that grew, and the acks of the frames that
        kept the link busy that long have set the rate), jump straight to
        the biggest rung that carries AUTO_TARGET_FPS — several at once if
        need be (360w frames are 10-15x smaller than 1280w ones) — at most
        once per round trip, so the frames of the new size are measured
        before the next jump."""
        if not self.adaptive:
            return
        if not self._link_limited():
            self._limited_since = None
            return
        if self._limited_since is None:
            # Acks later than ACK_TIMEOUT are a second of evidence already:
            # every big frame sent meanwhile is seconds of backlog.
            self._limited_since = now - (self.ACK_TIMEOUT if self._late
                                         else 0.0)
        wait = max(self._srtt or 0.0, 0.25)
        if self._level == 0 or now - self._limited_since < wait \
                or now - self._level_at < wait:
            return
        fps = self._link_fps()
        if fps is None or fps >= AUTO_MIN_FPS:
            return
        level = self._level - 1
        while level > 0 and self._link_fps(level) < AUTO_TARGET_FPS:
            level -= 1
        self._set_level(level, now, f"link carries {fps:.1f} fps")

    def _acks_late(self):
        """ACK_TIMEOUT with frames out: write off the ones already sent so
        the stream moves on. Their acks are late, not lost, so they are
        absorbed when they come (ack) instead of being paired with the
        frames sent next, which would fake tiny round trips. Frames still
        being encoded stay counted."""
        fresh = len(self._sent_at) - self._written_off
        self._written_off = len(self._sent_at)
        self._outstanding = max(0, self._outstanding - fresh)
        if fresh > 0:
            self._late = True
        if self._written_off > 4 * self.MAX_UNACKED:
            # Acks really stopped coming (an app bug): forget them.
            self._sent_at.clear()
            self._written_off = 0

    def _queued(self):
        """True when acks come back clearly later than the route's base
        round trip: frames are queueing on the link (too much data), as
        opposed to a route that is merely long."""
        if self._srtt is None or self._min_rtt is None:
            return False
        return self._srtt - self._min_rtt > max(0.02, 0.3 * self._min_rtt)

    # Frames allowed to sit in a queue somewhere on the route (TCP Vegas):
    # the window grows while fewer are queued, and is cut by the excess
    # when more are.
    QUEUE_LOW = 1.0
    QUEUE_HIGH = 2.0
    # A link that is busy all the time never shows its true round trip:
    # every sample includes the standing queue, the base looks longer than
    # it is, and the window would keep that queue. So every PROBE_EVERY s
    # the window drops to MIN_UNACKED for about a round trip (like BBR's
    # ProbeRTT), the queue drains, and the samples show the real base.
    PROBE_EVERY = 4.0
    # After this long without a frame, the next burst starts from
    # MIN_UNACKED again (like TCP's restart after idle): the link may have
    # slowed meanwhile, and a full window sent into it would queue.
    IDLE_RESTART = 1.0

    def probing(self, now=None):
        return (now or time.monotonic()) < self._probe_until

    def _rtt_sample(self, rtt, now):
        """One frame's ack round trip. The frames queued on the route are
        estimated as window x (rtt - base) / rtt. Above QUEUE_HIGH the
        window is cut by the excess at once (at most once per base round
        trip, judged on the latest sample), so a link that slows down, as
        mobile data does, is answered within a round trip. Below QUEUE_LOW
        it grows one step per round trip towards what covers the base
        round trip at the target fps."""
        self._rtts.append((now, rtt))
        while now - self._rtts[0][0] > self.RTT_WINDOW:
            self._rtts.popleft()
        self._min_rtt = min(r for _, r in self._rtts)
        self._srtt = rtt if self._srtt is None \
            else 0.75 * self._srtt + 0.25 * rtt
        if self._probe_window is not None:
            if now < self._probe_until:
                return
            # Probe over: back to the window from before, judged afresh
            # against the base the probe found.
            self._window = self._probe_window
            self._probe_window = None
            self._srtt = rtt
            self._window_at = now
            return
        if now >= self._probe_at:
            self._probe_at = now + self.PROBE_EVERY
            if self._window > self.MIN_UNACKED:
                self._probe_window = self._window
                self._probe_until = now + max(self._srtt, 0.1)
                self._window = self.MIN_UNACKED
                return
        recent = max(rtt, self._srtt)
        queued = self._window * (recent - self._min_rtt) / recent \
            if recent > 0 else 0.0
        self._queue = queued
        since = now - self._window_at
        if queued > self.QUEUE_HIGH:
            if since >= max(self._min_rtt, 0.05):
                self._window_at = now
                cut = math.ceil(queued - self.QUEUE_HIGH)
                self._window = max(self.MIN_UNACKED, self._window - cut)
            return
        if since < max(self._srtt, 0.05):
            return
        self._window_at = now
        need = self._need()
        if queued < self.QUEUE_LOW and self._window < need:
            self._window += 1
        elif self._window > need:
            self._window = need

    def _need(self):
        """The window that covers the base round trip at the target fps."""
        return min(self.MAX_UNACKED,
                   max(self.MIN_UNACKED,
                       math.ceil((self._min_rtt or 0.0) * self.fps) + 1))

    def _stats(self, now):
        """Every STATS_EVERY s while frames flow: what the phone gets."""
        self._stats_frames += 1
        if now - self._stats_at < self.STATS_EVERY:
            return
        fps = self._stats_frames / (now - self._stats_at)
        self._stats_at, self._stats_frames = now, 0
        width, quality = self._current()
        rtt = "" if self._srtt is None else \
            f", rtt {self._srtt * 1000:.0f} ms (base " \
            f"{self._min_rtt * 1000:.0f}), window {self._window}"
        if self._rate is not None:
            rtt += f", rate {self._rate * 8 / 1e6:.1f} Mbit/s"
        print(f"[agent] stream: {fps:.0f} fps, {width}w q{quality}{rtt}")

    def _frame_gone(self):
        """A grabbed frame was dropped or failed before reaching the phone:
        it will never be acked."""
        if self.acks and self._outstanding > 0:
            self._outstanding -= 1
            self._ack_event.set()

    def start(self):
        self.task = asyncio.create_task(self._run())

    async def stop(self):
        if self._closed:
            return
        self._closed = True
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        # Release the GDI handles on the thread that created them, then let
        # both worker threads exit.
        try:
            await asyncio.get_running_loop().run_in_executor(
                self._grab_pool, self._close_capture)
        except Exception:
            pass
        self._grab_pool.shutdown(wait=False)
        self._enc_pool.shutdown(wait=False)

    def _close_capture(self):
        if self._pframes is not None:
            self._pframes.close()
            self._pframes = None
        if self._sct is not None:
            self._sct.close()
            self._sct = None
        if self._dxgi is not None:
            self._dxgi.close()
            self._dxgi = None

    def _current(self):
        """(width, quality) for the next frame: the auto ladder's current
        rung when adaptive, else the fixed settings."""
        if self.adaptive:
            width, quality = AUTO_LADDER[self._level]
            return min(width, self.max_width), quality
        return self.max_width, self.quality

    def _refresh_rect(self, now):
        # Follow resolution changes / monitors being re-arranged; the
        # monitor list itself is cached, so this is cheap.
        if self._portal is not None:
            if self._portal.rect:
                self.rect = dict(self._portal.rect)
            return
        if now - self._rect_at < MONITORS_TTL:
            return
        self._rect_at = now
        try:
            self.rect = self.agent.resolve_monitor(self.monitor)
        except Exception:
            pass  # monitor gone: keep the last rect; capture errors recover

    def _grab(self, rect, force, refresh=False):
        """Capture-thread stage: grab `rect`. Returns (shot, cursor pos,
        rect), or None when nothing changed since the last frame passed on
        (and no resend is forced)."""
        if self._sct is None:
            _cv()  # warm the lazy OpenCV import here, off the event loop
            self._sct = new_mss()
        shot = self._sct.grab(rect)
        pos = None if self.client_cursor else mouse.position
        if not self._changed(shot, pos, force, refresh):
            return None
        return shot, pos, rect

    def _changed(self, shot, pos, force, refresh):
        """Whether a grab goes on to the phone: its thumb changed, a resend
        is owed (force), or an owed refresh (see wake) finds the picture
        different from the last refresh passed on. The refresh compares a
        checksum of every pixel (~5 ms for 1080p, at most ~12/s), so a
        still screen under a moving pointer costs one frame, not 12/s."""
        thumb = frame_thumb(shot, pos)
        if thumb != self._last_thumb or force:
            self._last_thumb = thumb
            self._refresh_crc = None
            return True
        if not refresh:
            return False
        try:
            crc = zlib.crc32(shot.raw)
        except (TypeError, ValueError, BufferError):
            crc = None  # not one flat buffer: just send it
        if crc is not None and crc == self._refresh_crc:
            return False
        self._refresh_crc = crc
        return True

    # Ring of capture buffers: an image stays valid until this many newer
    # ones were grabbed — more than can be past the grab stage at once
    # (two in flight in _run, one being grabbed).
    DXGI_RING = 4
    # Longest one Desktop Duplication wait for the screen to change. Any
    # change ends it at once; a resend owed after a dropped frame (force)
    # waits at most this long, so keep it at about one frame.
    DXGI_WAIT_MS = 16

    def _grab_dxgi(self, rect, force, not_before, refresh=False):
        """Capture-thread stage via Desktop Duplication (dxgi_capture.py):
        a GPU copy, ~3 ms for 1080p where mss's GDI grab takes 30-40 ms
        here, so 60 fps fits. Paces itself: sleeps until `not_before`
        (time.sleep is ~1 ms precise; asyncio's ~15.6 ms timer ticks could
        not hold 60 fps), then waits in AcquireNextFrame for the next
        change — a frame is grabbed the moment the PC presents it.

        Duplication only hands out an image when something is presented:
        opening it takes 0.1-0.6 s and its first image comes with the next
        screen update (never, on a still desktop). So the stream's first
        frame is a GDI grab, taken at once, and that grab stays the base
        image the cursor is painted on until DXGI delivers a newer one.
        Same return as _grab."""
        delay = not_before - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        if self._base is not None and self._base_rect != rect:
            # Other monitor or resolution: start over from a GDI grab.
            self._base = None
            if self._dxgi is not None:
                self._dxgi.close()
                self._dxgi = None
        try:
            if self._dxgi is None:
                if self._base is None:
                    return self._grab_base(rect, force, refresh)
                import dxgi_capture
                dx = dxgi_capture.DXGICapture(rect, ring=self.DXGI_RING)
                dx.FIRST_IMAGE_MS = 0  # never block waiting for a present
                dx.open()
                self._dxgi = dx
                # Duplicating now: re-grab the base so whatever changed while
                # it opened is in; every later change comes from DXGI.
                return self._grab_base(rect, force, refresh)
            reopens = self._dxgi.reopens
            img = self._dxgi.grab(rect, timeout_ms=self.DXGI_WAIT_MS)
            if self._dxgi.reopens != reopens:
                # Access lost (UAC, mode change): re-open and re-base, the
                # screen may have changed while duplication was down.
                self._dxgi.open()
                return self._grab_base(rect, force, refresh)
        except Exception:
            # Release it here, on the thread that made it (D3D11 contexts
            # are single-threaded); the next grab re-opens.
            if self._dxgi is not None:
                self._dxgi.close()
                self._dxgi = None
            raise
        if img is not None:
            self._base = DuplicatedShot(img)
            self._base_rect = rect
        return self._pass_frame(self._base, rect, force, refresh)

    def _grab_base(self, rect, force, refresh=False):
        """One GDI grab as the base image (stream start, DXGI re-opened)."""
        if self._sct is None:
            _cv()  # warm the lazy OpenCV import here, off the event loop
            self._sct = new_mss()
        shot = self._sct.grab(rect)
        import numpy as np
        w, h = shot.size
        self._base = DuplicatedShot(
            np.frombuffer(shot.raw, np.uint8).reshape(h, w, 4))
        self._base_rect = rect
        return self._pass_frame(self._base, rect, force, refresh)

    def _pass_frame(self, shot, rect, force, refresh=False):
        """(shot, cursor pos, rect) unless neither the image nor the cursor
        changed since the last frame passed on (and no resend is owed). The
        pointer isn't in captured images (we paint our own), so the thumb,
        which includes its position, tells changed from not. With a client
        cursor the pointer is neither painted nor part of the thumb."""
        pos = None if self.client_cursor else mouse.position
        if not self._changed(shot, pos, force, refresh):
            return None
        return shot, pos, rect

    # Longest wait for the portal's next frame once streaming: PipeWire only
    # delivers when the compositor repaints, so this is the idle poll.
    PORTAL_WAIT_S = 0.016

    def _grab_portal(self, rect, force, not_before):
        """Capture-thread stage via the Wayland portal (wayland_portal.py):
        PipeWire hands over a frame whenever the compositor repaints the
        monitor, pointer included. Paces itself like _grab_dxgi. Same return
        as _grab, with pos None: the pointer is already in the image."""
        delay = not_before - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        portal = self._portal
        if not portal.active:
            # Sharing was stopped from the desktop, or closed while no phone
            # watched: start it again (no dialog thanks to the restore token).
            asyncio.run_coroutine_threadsafe(portal.ensure(), self.agent.loop) \
                .result(wayland_portal.START_TIMEOUT + 5)
        if self._pframes is None or self._pgen != portal.generation:
            if self._pframes is not None:
                self._pframes.close()
            self._pframes, self._base = None, None
            _cv()  # warm the lazy OpenCV import here, off the event loop
            self._pgen = portal.generation
            self._pframes = portal.open_frames()
        img = self._pframes.grab(1.0 if self._base is None
                                 else self.PORTAL_WAIT_S)
        if img is not None:
            self._base = DuplicatedShot(img)
            self._base_rect = rect
        if self._base is None:
            return None
        thumb = frame_thumb(self._base, None)
        if thumb == self._last_thumb and not force:
            return None
        self._last_thumb = thumb
        return self._base, None, rect

    def _use_dxgi(self, now):
        return platform.system() == "Windows" and now >= self._dxgi_retry_at

    def _dxgi_failed(self, err, now):
        """A Desktop Duplication error: after 3 in a row (no DXGI on this
        output, Remote Desktop, a driver that refuses), use mss for a
        minute. One-offs (UAC's secure desktop) just retry."""
        self._dxgi_errors += 1
        if self._dxgi_errors >= 3:
            self._dxgi_errors = 0
            self._dxgi_retry_at = now + 60
            print(f"[agent] fast capture unavailable ({err}); "
                  "using GDI capture for a minute")

    async def _run(self):
        loop = asyncio.get_running_loop()
        inflight = deque()  # encode+send tasks, oldest first
        errors = 0
        deadline = time.monotonic()  # when the current grab was due
        try:
            while True:
                if self.acks and self._outstanding >= self._window:
                    # The phone hasn't got the last frames yet: grab nothing
                    # until it acks, then grab the freshest screen. Time
                    # spent waiting is what a too-slow link costs, so the
                    # auto ladder counts it as busy.
                    t = time.monotonic()
                    self._ack_event.clear()
                    try:
                        await asyncio.wait_for(self._ack_event.wait(),
                                               self.ACK_TIMEOUT)
                    except asyncio.TimeoutError:
                        self._acks_late()
                    # Waiting out a long but uncongested route is not a
                    # slow link: only waits while frames queue count, or
                    # the auto ladder would shrink the picture for nothing.
                    waited = time.monotonic() - t
                    self._link_sample(waited if self._queued()
                                      and not self.probing() else 0.0)
                    continue
                grab_at = time.monotonic()
                self._refresh_rect(grab_at)
                force, self._force = self._force, False
                refresh = False
                if self._refresh_at and grab_at >= self._refresh_at:
                    refresh, self._refresh_at = True, 0.0
                portal = self._portal is not None
                dxgi = not portal and self._use_dxgi(grab_at)
                try:
                    if portal:
                        grabbed = await loop.run_in_executor(
                            self._grab_pool, self._grab_portal, self.rect,
                            force, deadline)
                    elif dxgi:
                        grabbed = await loop.run_in_executor(
                            self._grab_pool, self._grab_dxgi, self.rect,
                            force, deadline, refresh)
                        self._dxgi_errors = 0
                    else:
                        grabbed = await loop.run_in_executor(
                            self._grab_pool, self._grab, self.rect, force,
                            refresh)
                    errors = 0
                except Exception as e:
                    # Transient capture hiccup (display sleep, UAC secure
                    # desktop, GDI churn, a monitor being unplugged): skip
                    # the frame and keep streaming. NEVER return here — a
                    # dead stream is a frozen phone screen.
                    self._force = self._force or force
                    if refresh and not self._refresh_at:
                        self._refresh_at = time.monotonic()  # still owed
                    if dxgi:
                        self._dxgi_failed(e, time.monotonic())
                    errors += 1
                    if errors == 25:
                        print(f"[agent] screen capture failing repeatedly: {e}")
                    await asyncio.sleep(0.2)
                    continue
                if grabbed is not None:
                    # Stay hot for a second after the last change so
                    # stop-start movement doesn't oscillate hot/idle.
                    self._hot_until = time.monotonic() + 1.0
                    while inflight and inflight[0].done():
                        inflight.popleft()
                    if len(inflight) >= 2:
                        # Pipeline full: the link (or the encoder) is the
                        # bottleneck. Wait for the oldest frame to go out
                        # rather than piling up frames that are stale by
                        # the time they'd be sent.
                        await inflight.popleft()
                    prev = inflight[-1] if inflight else None
                    if self.acks:
                        self._outstanding += 1
                    inflight.append(asyncio.create_task(
                        self._encode_and_send(grabbed, prev)))
                if dxgi or portal:
                    # _grab_dxgi paces itself: it sleeps until `deadline`,
                    # then blocks until the screen (or pointer) changes.
                    # Next deadline: one interval after this one, never
                    # banking more than one interval of lag (no bursts).
                    now = time.monotonic()
                    interval = 1.0 / self.fps
                    deadline = max(deadline + interval if grabbed is not None
                                   else deadline, now - interval)
                    self._wake.clear()
                    continue
                # Adaptive rate (GDI capture): full fps while the screen changes (and for a
                # grace second after), a gentle tick when idle — but any
                # cursor movement (phone input or the physical mouse, via
                # the watcher) wakes us immediately, so idle never adds
                # cursor latency.
                idle_fps = max(3, self.fps // 3)
                now = time.monotonic()
                interval = 1.0 / (self.fps if now < self._hot_until
                                  else idle_fps)
                # Deadline pacing: the next grab is due one interval after
                # this one was DUE, not after it finished. asyncio waits on
                # Windows are quantized to ~15.6 ms, which stretched every
                # frame (20 fps came out at 17); against a fixed schedule
                # the oversleeps average out. Never bank more than one
                # interval of lag, so a stall can't cause a burst.
                deadline = max(deadline + interval, now - interval)
                if self._refresh_at:
                    # Don't idle past an owed refresh (see wake).
                    deadline = min(deadline, max(now, self._refresh_at))
                if deadline > now:
                    try:
                        await asyncio.wait_for(self._wake.wait(),
                                               timeout=deadline - now)
                        # Woken by cursor movement: grab as soon as the
                        # full-rate interval allows — at once after an idle
                        # tick, but a moving mouse (woken ~16x/s) must not
                        # push e.g. Data saver past its 12 fps.
                        deadline = max(time.monotonic(),
                                       grab_at + 1.0 / self.fps)
                        if deadline > time.monotonic():
                            await asyncio.sleep(deadline - time.monotonic())
                    except asyncio.TimeoutError:
                        pass
                self._wake.clear()
        finally:
            for task in inflight:
                task.cancel()

    async def _encode_and_send(self, grabbed, prev):
        """Encode one grabbed frame (overlapping the previous frame's send),
        then send it once `prev` — the frame before it — is out."""
        shot, pos, rect = grabbed
        level = self._level
        width, quality = self._current()
        sent = None
        try:
            jpeg, w, h = await asyncio.get_running_loop().run_in_executor(
                self._enc_pool, encode_frame, shot, rect, pos, width,
                quality, self.subsampling)
            if self.binary:
                header = json.dumps({
                    "w": w, "h": h, "src": "screen",
                    # effective settings, for the app's stats overlay
                    # (older apps ignore unknown keys)
                    "q": quality, "preset": self.preset,
                }).encode()
                packet = b"PJF1" + len(header).to_bytes(4, "little") \
                    + header + jpeg
                size = len(packet)
            else:
                packet = {"event": "screen.frame",
                          "data": {"jpeg": base64.b64encode(jpeg).decode(),
                                   "w": w, "h": h}}
                size = len(packet["data"]["jpeg"]) + 64
            if prev is not None:
                await prev  # frames leave in capture order
            if self.agent.congested(self.client_id):
                # Drop rather than queue behind a backed-up socket; the
                # forced next grab re-sends the current screen once the
                # link drains, so this change is never lost to the thumb.
                self._force = True
                self._frame_gone()
                self._link_sample(None)
                return
            if level == self._level:
                # A frame encoded before a rung change says nothing about
                # the new rung's size.
                self._frame_bytes(size)
            t = time.monotonic()
            if self.acks:
                if t - self._last_sent > self.IDLE_RESTART \
                        and self._probe_window is None:
                    self._window = self.MIN_UNACKED
                self._last_sent = t
                # Before the send: its ack may be handled before this
                # coroutine resumes from the send.
                sent = self._sent(t, size)
                self._sent_at.append(sent)
            if self.binary:
                await self.agent.send_bytes_to(self.client_id, packet)
            else:
                await self.agent.send_to(self.client_id, packet)
            now = time.monotonic()
            self._link_sample(now - t)
            self._stats(now)
            self._errors = 0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            gone = True
            index = next((i for i, s in enumerate(self._sent_at)
                          if s is sent), None) if sent is not None else None
            if index is not None:
                # Never sent: no ack will come for it.
                del self._sent_at[index]
                if index < self._written_off:
                    # Written off already (and out of _outstanding).
                    self._written_off -= 1
                    gone = False
            self._force = True
            if gone:
                self._frame_gone()
            self._errors += 1
            if self._errors == 25:
                print(f"[agent] screen encode/send failing repeatedly: {e}")

    def _link_sample(self, seconds):
        """auto preset: one frame went out after blocking `seconds` in the
        send (None = dropped because the socket was congested, counted as a
        whole frame interval). Steps down a rung (not under AUTO_BUSY_FLOOR)
        when the stream spent over AUTO_DOWN_BUSY of the last
        AUTO_BUSY_WINDOW seconds blocked; back up only after AUTO_UP_AFTER
        seconds (and 20+ frames) under AUTO_UP_BUSY, and only to a rung the
        measured delivery rate carries (_fits). The gap between the
        thresholds plus the hold times keeps a marginal link from
        oscillating. Slow links are the bandwidth rule's (_check_rate)."""
        if not self.adaptive:
            return
        now = time.monotonic()
        self._sends.append((now, 1.0 / self.fps if seconds is None
                            else seconds))
        while now - self._sends[0][0] > AUTO_BUSY_WINDOW:
            self._sends.popleft()
        busy = sum(s for _, s in self._sends) / AUTO_BUSY_WINDOW
        if busy >= AUTO_UP_BUSY:
            self._good_since, self._good_frames = now, 0
        else:
            self._good_frames += 1
        if busy > AUTO_DOWN_BUSY and self._level > AUTO_BUSY_FLOOR \
                and now - self._level_at >= 1.0:
            self._set_level(self._level - 1, now)
        elif self._level < len(AUTO_LADDER) - 1 \
                and now - self._good_since >= AUTO_UP_AFTER \
                and self._good_frames >= 20 \
                and now - self._level_at >= AUTO_UP_AFTER \
                and self._fits(self._level + 1):
            self._set_level(self._level + 1, now)

    def _fits(self, level):
        """Whether the link's measured rate would still carry AUTO_UP_FPS
        frames/s at `level`. With nothing measured, the busy rule alone
        decides, as it always did: without acks, and on a route so short
        (USB through adb reverse: 2-4 ms) that every frame is acked before
        the next goes out, under RATE_MIN_INTERVAL, so no rate sample is
        ever taken. A slow link always has samples by the time the hold
        times allow a step up."""
        if not self.acks:
            return True
        fps = self._link_fps(level)
        return fps is None or fps >= AUTO_UP_FPS

    def _frame_bytes(self, size):
        """A frame of `size` bytes, encoded at the current rung, goes out:
        the rung's average frame size follows it. The first frame after a
        rung change replaces the cost model's guess outright."""
        if self._size is None or self._size_fresh:
            self._size, self._size_fresh = float(size), False
        else:
            self._size = 0.8 * self._size + 0.2 * size

    def _set_level(self, level, now, why=None):
        if self._size is not None:
            # The new rung's frames, guessed until its first one is out.
            self._size = self._size_at(level)
            self._size_fresh = True
        self._level = level
        self._level_at = now
        # Judge the new rung on its own sends, not the old rung's backlog.
        self._sends.clear()
        width, quality = self._current()
        print(f"[agent] auto quality -> {width}w q{quality}"
              + (f" ({why})" if why else ""))


# ---------------------------------------------------------------- input

class CameraStreamer:
    """Streams the PC webcam to a client as PJF1 binary frames (src=camera).

    OpenCV capture runs on a single-thread executor: VideoCapture objects
    are not safe to poke from arbitrary event-loop threads."""

    def __init__(self, agent, client_id, fps=10, max_width=640):
        self.agent = agent
        self.client_id = client_id
        self.fps = max(1, min(int(fps), 20))
        self.max_width = max(160, min(int(max_width), 1280))
        self.task = None

    def start(self):
        self.task = asyncio.create_task(self._run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def _error(self, text):
        await self.agent.send_to(self.client_id, {
            "event": "camera.error", "data": {"error": text},
        })

    async def _run(self):
        from concurrent.futures import ThreadPoolExecutor
        try:
            import cv2
        except ImportError:
            await self._error("opencv-python is not installed on the PC")
            return
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1) as pool:
            cap = await loop.run_in_executor(pool, cv2.VideoCapture, 0)
            try:
                if not await loop.run_in_executor(pool, cap.isOpened):
                    await self._error("no camera found (or it is busy)")
                    return
                while True:
                    t0 = time.monotonic()
                    ok, frame = await loop.run_in_executor(pool, cap.read)
                    if not ok:
                        await asyncio.sleep(0.3)
                        continue
                    h, w = frame.shape[:2]
                    if w > self.max_width:
                        frame = cv2.resize(
                            frame, (self.max_width,
                                    round(h * self.max_width / w)))
                    ok, buf = cv2.imencode(
                        ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 60])
                    if ok and not self.agent.congested(self.client_id):
                        header = json.dumps({
                            "w": int(frame.shape[1]),
                            "h": int(frame.shape[0]),
                            "src": "camera",
                        }).encode()
                        await self.agent.send_bytes_to(
                            self.client_id,
                            b"PJF1" + len(header).to_bytes(4, "little")
                            + header + buf.tobytes())
                    await asyncio.sleep(
                        max(0.0, 1.0 / self.fps - (time.monotonic() - t0)))
            finally:
                cap.release()


def one_line(err, limit=240):
    """ffmpeg stderr (bytes or str) squeezed onto one log line."""
    if isinstance(err, bytes):
        err = err.decode(errors="replace")
    return " | ".join(s.strip() for s in err.splitlines() if s.strip())[:limit]


def dxgi_outputs():
    """[(adapter index, output index, (left, top, right, bottom))] for every
    desktop-attached DXGI output, indexed the way ffmpeg's `d3d11va=<n>`
    device and ddagrab's output_idx count them (IDXGIFactory::EnumAdapters
    / EnumOutputs). Windows only; [] elsewhere or on any failure."""
    if platform.system() != "Windows":
        return []
    import ctypes
    from ctypes import wintypes

    class OutputDesc(ctypes.Structure):
        _fields_ = [("DeviceName", ctypes.c_wchar * 32),
                    ("DesktopCoordinates", wintypes.RECT),
                    ("AttachedToDesktop", wintypes.BOOL),
                    ("Rotation", ctypes.c_int),
                    ("Monitor", wintypes.HMONITOR)]

    def method(obj, index, *argtypes):
        # COM through the raw vtable: slot 2 is IUnknown::Release, slot 7
        # is EnumAdapters / EnumOutputs / GetDesc on the respective
        # interfaces (after IUnknown's 3 and IDXGIObject's 4 methods).
        vtbl = ctypes.cast(obj, ctypes.POINTER(
            ctypes.POINTER(ctypes.c_void_p)))[0]
        fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                *argtypes)(vtbl[index])
        return lambda *args: fn(obj, *args)

    out = []
    iid = (ctypes.c_ubyte * 16)(
        *uuid.UUID("7b7166ec-21c7-44ae-b21a-c9ae321ae369").bytes_le)
    factory = ctypes.c_void_p()
    try:
        if ctypes.windll.dxgi.CreateDXGIFactory(
                ctypes.byref(iid), ctypes.byref(factory)) != 0:
            return []
        pp = ctypes.POINTER(ctypes.c_void_p)
        a = 0
        while True:
            adapter = ctypes.c_void_p()
            if method(factory, 7, ctypes.c_uint, pp)(
                    a, ctypes.byref(adapter)) != 0:
                break
            o = 0
            while True:
                output = ctypes.c_void_p()
                if method(adapter, 7, ctypes.c_uint, pp)(
                        o, ctypes.byref(output)) != 0:
                    break
                desc = OutputDesc()
                method(output, 7, ctypes.POINTER(OutputDesc))(
                    ctypes.byref(desc))
                r = desc.DesktopCoordinates
                if desc.AttachedToDesktop:
                    out.append((a, o, (r.left, r.top, r.right, r.bottom)))
                method(output, 2)()
                o += 1
            method(adapter, 2)()
            a += 1
    except Exception:
        return []
    finally:
        if factory:
            method(factory, 2)()
    return out


def monitor_handle(rect):
    """HMONITOR (as an int) of the monitor under the centre of `rect`, 0 if
    none. Coordinates are physical pixels: mss made this process DPI aware."""
    if platform.system() != "Windows":
        return 0
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32")  # own instance: private restype
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.MonitorFromPoint.restype = ctypes.c_void_p
    pt = wintypes.POINT(rect["left"] + rect["width"] // 2,
                        rect["top"] + rect["height"] // 2)
    return user32.MonitorFromPoint(pt, 0) or 0  # MONITOR_DEFAULTTONULL


AUD_NAL = b"\x09\xf0"  # access unit delimiter, primary_pic_type "any"


class FlvAvcReader:
    """Pulls H.264 access units out of ffmpeg's FLV output on a pipe.

    FLV is used purely as framing: every video tag carries exactly one
    access unit plus its keyframe flag and timestamp, so an AU is known to
    be complete the moment its last byte arrives. (Raw Annex-B only reveals
    where an AU ends when the NEXT one starts — a whole frame of added
    latency.) Tags are AVCC (length-prefixed NAL units); next_au() returns
    them as Annex-B led by an AUD, with SPS/PPS from the sequence header
    put in front of every IDR so the decoder can (re)start on any keyframe.
    """

    def __init__(self, stream):
        self.stream = stream
        self.length_size = 4
        self.sps_pps = []   # parameter-set NAL units from the sequence header
        self._header_done = False

    async def next_au(self):
        """(key, ts in ms, Annex-B bytes) of the next access unit. Raises
        asyncio.IncompleteReadError when ffmpeg exits, ValueError on
        output that isn't FLV/H.264."""
        read = self.stream.readexactly
        if not self._header_done:
            head = await read(9)
            if head[:3] != b"FLV":
                raise ValueError("not an FLV stream")
            # rest of the header + PreviousTagSize0
            await read(int.from_bytes(head[5:9], "big") - 9 + 4)
            self._header_done = True
        while True:
            tag = await read(11)
            size = int.from_bytes(tag[1:4], "big")
            ts = int.from_bytes(tag[4:7], "big") | (tag[7] << 24)
            body = (await read(size + 4))[:size]  # + PreviousTagSize
            if tag[0] & 0x1F != 9 or len(body) < 2:
                continue  # script data (onMetaData), audio, padding
            if body[0] & 0x80:
                # Enhanced FLV: [type nibble][4cc][SI24 cts only for
                # CodedFrames (1); CodedFramesX (3) has none]
                pkt_type, data = body[0] & 0x0F, body[5:]
                if pkt_type == 1:
                    data = data[3:]
                elif pkt_type == 3:
                    pkt_type = 1
            else:
                if body[0] & 0x0F != 7:
                    raise ValueError("FLV video is not H.264")
                pkt_type, data = body[1], body[5:]  # skip SI24 cts
            if pkt_type == 0:
                self._parse_config(data)
            elif pkt_type == 1:
                return self._annexb(ts, data)

    def _parse_config(self, cfg):
        # AVCDecoderConfigurationRecord: version, profile, compat, level,
        # 0b111111xx (NAL length size - 1), 0b111xxxxx (#SPS), SPSs,
        # #PPS, PPSs — each parameter set prefixed with a 16-bit length.
        self.length_size = (cfg[4] & 3) + 1
        sets, pos = [], 6
        for count in (cfg[5] & 0x1F, None):
            if count is None:
                count, pos = cfg[pos], pos + 1
            for _ in range(count):
                n = int.from_bytes(cfg[pos:pos + 2], "big")
                sets.append(cfg[pos + 2:pos + 2 + n])
                pos += 2 + n
        self.sps_pps = sets

    def _annexb(self, ts, data):
        nals, pos, ls = [], 0, self.length_size
        while pos + ls <= len(data):
            n = int.from_bytes(data[pos:pos + ls], "big")
            nals.append(data[pos + ls:pos + ls + n])
            pos += ls + n
        types = {nal[0] & 0x1F for nal in nals if nal}
        key = 5 in types  # IDR slice
        units = [AUD_NAL]
        if key and not {7, 8} <= types:
            units += self.sps_pps
        units += [nal for nal in nals if nal and nal[0] & 0x1F != 9]
        return key, ts, b"".join(b"\x00\x00\x00\x01" + u for u in units)


class H264Streamer:
    """Streams one monitor as H.264 via an ffmpeg subprocess.

    Capture, cheapest first: ddagrab (Desktop Duplication: a GPU copy) when
    the monitor maps to a DXGI output; gfxcapture (Windows Graphics
    Capture, Windows 11) — on this hybrid AMD+NVIDIA dev laptop ddagrab
    fails with DXGI_ERROR_UNSUPPORTED while gfxcapture works at ~1/3 of
    gdigrab's CPU; then gdigrab (GDI BitBlt, works everywhere, but can't
    hold 30 fps at 1080p). GPU frames are hwdownload'ed to system memory, so
    any encoder — even one on the other GPU of a hybrid laptop — takes them.
    Encoders: h264_nvenc -> h264_qsv -> libx264, each probed on synthetic
    frames first (NVENC needs a recent driver: ffmpeg 8.1 refuses drivers
    older than 610). Failures are remembered for FAIL_TTL so every start
    doesn't re-pay them.

    Packets: [4B "H264"][4B LE header len][header JSON][ONE access unit,
    Annex-B, AUD first; SPS/PPS before every IDR]. Low latency: no
    B-frames, zerolatency/ll tuning, ~2 s GOP, VBV a quarter second deep.
    """

    ENCODERS = [
        # (name, encoder args, pixel format the encoder takes)
        ("h264_nvenc", ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ll",
                        "-rc", "cbr", "-zerolatency", "1", "-delay", "0",
                        "-profile:v", "baseline", "-bf", "0"], "yuv420p"),
        ("h264_qsv", ["-c:v", "h264_qsv", "-preset", "veryfast",
                      "-async_depth", "1", "-profile:v", "baseline",
                      "-bf", "0"], "nv12"),
        # h264_amf was tried here as a third hardware tier but produces
        # visibly wrong chroma (green-tinted regions) on-device with this
        # gdigrab+lowlatency pipeline — worse than the libx264 fallback it
        # was meant to avoid, so it's intentionally left out.
        # superfast over ultrafast: same CPU here (bench: ~0.7 core at
        # 1080p30), but keeps the deblocking filter — far fewer blocks on
        # text at screen-sharing bitrates.
        ("libx264", ["-c:v", "libx264", "-preset", "superfast",
                     "-tune", "zerolatency", "-profile:v", "baseline",
                     "-bf", "0"], "yuv420p"),
    ]
    FAIL_TTL = 600.0         # seconds a failed capture/encoder stays skipped
    # A capture that shows nothing by then is skipped for this start. Normally
    # the first unit arrives in ~1.5 s, but a PC pegged at 100% CPU (an
    # Android emulator running) took ~10 s; such a silent timeout is only
    # held against the capture for SLOW_START_TTL, not FAIL_TTL.
    FIRST_AU_TIMEOUT = 10.0
    SLOW_START_TTL = 60.0
    MAX_LAG = 0.3            # seconds behind real time before skipping ahead
    _failed = {}             # "capture:<name>" / "encoder:<name>" -> retry at
    _probed = {}             # encoder name -> time it last probed OK

    def __init__(self, agent, client_id, rect, fps=30, max_width=1920,
                 bitrate=6_000_000):
        self.agent = agent
        self.client_id = client_id
        self.rect = rect
        self.fps = max(5, min(int(fps), 60))
        self.bitrate = max(500_000, min(int(bitrate), 20_000_000))
        # Even dimensions (4:2:0), computed here so every packet header
        # states the exact coded size.
        width = min(max(320, min(int(max_width), 3840)), rect["width"])
        self.width = width - width % 2
        height = round(rect["height"] * self.width / rect["width"])
        self.height = max(2, height - height % 2)
        self.task = None
        self.proc = None
        self.encoder = None
        self.capture = None
        self._stopping = False
        self._last_error = ""
        self._timed_out = False  # last _stream gave up waiting for video

    @staticmethod
    def ffmpeg_path():
        base = Path(getattr(sys, "_MEIPASS", HERE))
        for cand in (base / "ffmpeg.exe", HERE / "ffmpeg.exe",
                     HERE / "tools" / "ffmpeg.exe",
                     HERE.parent / "tools" / "ffmpeg.exe"):
            if cand.exists():
                return str(cand)
        return "ffmpeg"  # hope for PATH

    def start(self):
        self.task = asyncio.create_task(self._run())

    async def stop(self):
        self._stopping = True
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    @classmethod
    def _recently_failed(cls, key):
        return time.monotonic() < cls._failed.get(key, 0.0)

    def _captures(self):
        """Capture backends for this monitor, cheapest first: (name, ffmpeg
        input args, source filter chain ending in system-memory frames —
        None for a demuxer input)."""
        r, fps = self.rect, self.fps
        out = []
        right, bottom = r["left"] + r["width"], r["top"] + r["height"]
        for adapter, output, coords in dxgi_outputs():
            if coords == (r["left"], r["top"], right, bottom):
                out.append(("ddagrab", [
                    "-init_hw_device", f"d3d11va=dda:{adapter}",
                    "-filter_hw_device", "dda",
                ], f"ddagrab=output_idx={output}:framerate={fps}"
                   ":draw_mouse=1,hwdownload,format=bgra"))
                break
        hmon = monitor_handle(r)
        if hmon and platform.system() == "Windows" \
                and sys.getwindowsversion().build >= 22000:
            # Windows 11 only: there the capture can be borderless; on 10
            # it would paint a yellow frame around the user's screen.
            out.append(("gfxcapture", [], (
                f"gfxcapture=hmonitor={hmon}:max_framerate={fps}"
                ":capture_cursor=1:display_border=0,hwdownload,format=bgra,"
                f"fps={fps}")))  # WGC only delivers frames on change
        out.append(("gdigrab", [
            "-f", "gdigrab", "-framerate", str(fps),
            "-offset_x", str(r["left"]), "-offset_y", str(r["top"]),
            "-video_size", f"{r['width']}x{r['height']}",
            "-draw_mouse", "1", "-i", "desktop",
        ], None))
        return out

    def _cmd(self, capture, enc_args, pix_fmt):
        _, inputs, source = capture
        scale = (f"scale={self.width}:{self.height}:flags=bicubic,"
                 f"format={pix_fmt}")
        graph = (["-filter_complex", f"{source},{scale}[v]", "-map", "[v]"]
                 if source else ["-vf", scale])
        return [
            self.ffmpeg_path(), "-hide_banner", "-loglevel", "error",
            "-nostdin", *inputs, *graph, *enc_args,
            "-g", str(self.fps * 2),
            "-b:v", str(self.bitrate), "-maxrate", str(self.bitrate),
            # A quarter second of VBV: room for a sharp IDR of a busy
            # desktop without letting bursts queue for long on the link.
            "-bufsize", str(self.bitrate // 4),
            # Every packet straight to the pipe — no 32 KB AVIO buffering
            # of tiny idle P-frames.
            "-flush_packets", "1", "-flvflags", "no_duration_filesize",
            "-f", "flv", "pipe:1",
        ]

    async def _spawn(self, cmd, stdout):
        # CREATE_NO_WINDOW: ffmpeg is a console app — without this it pops
        # a visible console window the user can close, killing the stream.
        no_window = 0x08000000 if platform.system() == "Windows" else 0
        return await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=stdout,
            stderr=asyncio.subprocess.PIPE, creationflags=no_window)

    async def _encoder_works(self, name, enc_args, pix_fmt):
        """Open the encoder on a few synthetic frames (~0.1-0.3 s): far
        cheaper than discovering a dead encoder through a real capture,
        which can take a second to start."""
        if time.monotonic() - self._probed.get(name, -1e9) < self.FAIL_TTL:
            return True
        if self._recently_failed(f"encoder:{name}"):
            return False
        proc = await self._spawn([
            self.ffmpeg_path(), "-hide_banner", "-loglevel", "error",
            "-nostdin", "-f", "lavfi", "-i",
            f"color=c=black:s={self.width}x{self.height}:r={self.fps}",
            "-frames:v", "3", "-pix_fmt", pix_fmt, *enc_args,
            "-f", "null", "-"], asyncio.subprocess.DEVNULL)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), 20)
        except asyncio.TimeoutError:
            err = b"probe timed out"
        finally:
            if proc.returncode is None:  # timed out, or the stream stopped
                proc.kill()
                await proc.wait()
        if proc.returncode == 0:
            self._probed[name] = time.monotonic()
            return True
        self._failed[f"encoder:{name}"] = time.monotonic() + self.FAIL_TTL
        print(f"[agent] h264 encoder {name} unavailable: {one_line(err)}")
        return False

    async def _run(self):
        # Probe every encoder at once: the usual failures (an NVIDIA driver
        # too old for NVENC, no Intel GPU for QSV) cost ~0.2-1 s each, and
        # paying them one after another delayed the first frame by seconds
        # on a busy PC. Results are cached, so later starts skip this.
        oks = await asyncio.gather(*(self._encoder_works(*enc)
                                     for enc in self.ENCODERS))
        encoders = [enc for enc, ok in zip(self.ENCODERS, oks) if ok]
        captures = [cap for cap in self._captures()
                    if not self._recently_failed(f"capture:{cap[0]}")]
        while encoders and captures and not self._stopping:
            capture, encoder = captures[0], encoders[0]
            try:
                worked = await self._stream(capture, *encoder)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                worked, self._last_error = False, str(e)
            if self._stopping:
                return
            if not worked:
                # ffmpeg prefixes errors with the failing component ("[h264_
                # nvenc @ 0x...] ..."): blame the encoder when it speaks up
                # (e.g. out of NVENC sessions), else the capture.
                if f"[{encoder[0]} @" in self._last_error:
                    culprit = f"encoder:{encoder[0]}"
                    self._probed.pop(encoder[0], None)
                    encoders.pop(0)
                else:
                    culprit = f"capture:{capture[0]}"
                    captures.pop(0)
                # Silence until the timeout is a slow PC, not proof the
                # capture can't work: retry it again soon.
                slow = self._timed_out and not self._last_error.strip()
                self._failed[culprit] = time.monotonic() + (
                    self.SLOW_START_TTL if slow else self.FAIL_TTL)
                print(f"[agent] h264 {culprit} failed: " + (
                    f"no video within {self.FIRST_AU_TIMEOUT:.0f} s" if slow
                    else one_line(self._last_error)))
                continue  # next capture/encoder in the chain
            # It worked but died mid-stream: restart the same pipeline a few
            # times instead of leaving the client's video frozen.
            restarts = 0
            while worked and restarts < 3 and not self._stopping:
                restarts += 1
                print(f"[agent] h264 {capture[0]}+{encoder[0]} died, "
                      f"restart {restarts}/3")
                await asyncio.sleep(0.5)
                try:
                    worked = await self._stream(capture, *encoder)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"[agent] h264 restart failed: {e}")
                    break
            return
        if not self._stopping:
            await self.agent.send_to(self.client_id, {
                "event": "screen.error",
                "data": {"error": "no working h264 capture/encoder found"},
            })

    async def _stream(self, capture, enc_name, enc_args, pix_fmt):
        """Run one capture+encoder pipeline until ffmpeg dies. Returns True
        if it produced video (it worked), False if it failed to start; the
        tail of ffmpeg's stderr is kept in self._last_error."""
        self.proc = proc = await self._spawn(
            self._cmd(capture, enc_args, pix_fmt), asyncio.subprocess.PIPE)
        err_tail = bytearray()

        async def drain_stderr():
            # Always drained: a full stderr pipe would block ffmpeg.
            while chunk := await proc.stderr.read(4096):
                err_tail.extend(chunk)
                del err_tail[:-2048]

        err_task = asyncio.create_task(drain_stderr())
        reader = FlvAvcReader(proc.stdout)
        produced = False
        self._timed_out = False
        try:
            try:
                au = await asyncio.wait_for(reader.next_au(),
                                            self.FIRST_AU_TIMEOUT)
            except asyncio.TimeoutError:
                self._timed_out = True
                return False
            except (asyncio.IncompleteReadError, ValueError):
                return False
            produced = True
            self.capture, self.encoder = capture[0], enc_name
            dropping = False
            min_delay = None
            while True:
                key, ts, data = au
                # Smallest (arrival - capture timestamp) seen = the
                # pipeline's own latency; anything above it is backlog:
                # the link or this loop fell behind real time.
                delay = time.monotonic() - ts / 1000.0
                if min_delay is None or delay < min_delay:
                    min_delay = delay
                if dropping and key:
                    dropping = False  # a keyframe re-syncs the decoder
                if not dropping and (
                        delay - min_delay > self.MAX_LAG
                        or self.agent.congested(self.client_id)):
                    # Never send part of an AU, nor a P-frame whose
                    # reference was dropped: skip whole AUs until the next
                    # keyframe. Skipping drains the pipe, catching us up.
                    dropping = True
                if not dropping:
                    header = json.dumps({
                        "w": self.width, "h": self.height, "codec": "h264",
                        "encoder": enc_name, "capture": capture[0],
                        "fps": self.fps, "key": key, "ts": ts,
                    }).encode()
                    await self.agent.send_bytes_to(
                        self.client_id,
                        b"H264" + len(header).to_bytes(4, "little")
                        + header + data)
                au = await reader.next_au()
        except (asyncio.IncompleteReadError, ValueError):
            return produced  # encoder exited (or garbled its output)
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                # Read both pipes to EOF so their transports close (a
                # paused, never-drained stdout would leak its handle).
                await asyncio.wait_for(asyncio.gather(
                    proc.stdout.read(), err_task, proc.wait()), 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                err_task.cancel()
            self._last_error = err_tail.decode(errors="replace")


BUTTONS = {"left": Button.left, "right": Button.right, "middle": Button.middle}

SPECIAL_KEYS = {
    "enter": Key.enter, "backspace": Key.backspace, "tab": Key.tab,
    "esc": Key.esc, "escape": Key.esc, "space": Key.space,
    "up": Key.up, "down": Key.down, "left": Key.left, "right": Key.right,
    "delete": Key.delete, "home": Key.home, "end": Key.end,
    "pageup": Key.page_up, "pagedown": Key.page_down,
    "shift": Key.shift, "ctrl": Key.ctrl, "alt": Key.alt, "cmd": Key.cmd,
    "win": Key.cmd, "f1": Key.f1, "f2": Key.f2, "f3": Key.f3, "f4": Key.f4,
    "f5": Key.f5, "f6": Key.f6, "f7": Key.f7, "f8": Key.f8, "f9": Key.f9,
    "f10": Key.f10, "f11": Key.f11, "f12": Key.f12,
    "media_play_pause": Key.media_play_pause,
    "media_next": Key.media_next, "media_previous": Key.media_previous,
    "media_volume_up": Key.media_volume_up,
    "media_volume_down": Key.media_volume_down,
    "media_volume_mute": Key.media_volume_mute,
}


def do_move(x, y, rect=None):
    """Absolute move: x/y are normalized 0..1 within `rect` (the client's
    streamed monitor), the primary monitor when None. Clamped to the last
    pixel: x=1.0 must not land on the first pixel of a monitor to the
    right."""
    if PORTAL is not None:
        PORTAL.move_abs(x, y)
        return
    if rect is None:
        rect = primary_monitor(list_monitors())
    w, h = rect["width"], rect["height"]
    mouse.position = (rect["left"] + min(w - 1, int(max(0.0, min(1.0, x)) * w)),
                      rect["top"] + min(h - 1, int(max(0.0, min(1.0, y)) * h)))


# Sub-pixel remainders from do_move_rel: truncating every delta to whole
# pixels on its own rounds small drags down to 0 and swallows them, which
# made the cursor feel like it "sometimes doesn't move". The carry makes
# every fraction of a pixel eventually move the cursor.
_move_carry = [0.0, 0.0]


def do_move_rel(dx, dy, rect=None):
    """Relative move — dx/dy are normalized deltas (-1..1 of the streamed
    monitor's size, the primary monitor's when `rect` is None), so a drag
    across the phone image crosses the same share of the monitor shown."""
    if PORTAL is not None:
        PORTAL.move_rel(dx, dy)  # the portal takes fractional pixels
        return
    w, h = (rect["width"], rect["height"]) if rect else screen_size()
    _move_carry[0] += dx * w
    _move_carry[1] += dy * h
    mx, my = int(_move_carry[0]), int(_move_carry[1])
    if mx or my:
        mouse.move(mx, my)
        _move_carry[0] -= mx
        _move_carry[1] -= my


def do_click(button="left", action="click", x=None, y=None, count=1,
             rect=None):
    if x is not None and y is not None:
        do_move(x, y, rect)
    if PORTAL is not None:
        if action in ("down", "up"):
            PORTAL.button(button, action == "down")
        else:
            PORTAL.click(button, count)
        return
    btn = BUTTONS.get(button, Button.left)
    if action == "down":
        mouse.press(btn)
    elif action == "up":
        mouse.release(btn)
    else:
        mouse.click(btn, max(1, int(count)))


def do_scroll(dx=0, dy=0):
    if PORTAL is not None:
        PORTAL.scroll(dx, dy)
        return
    mouse.scroll(int(dx), int(dy))


def do_key(name):
    if PORTAL is not None:
        PORTAL.key(name)
        return
    key = SPECIAL_KEYS.get(name.lower())
    if key is None:
        if len(name) == 1:
            kb.press(name)
            kb.release(name)
            return
        raise ValueError(f"unknown key: {name}")
    kb.press(key)
    kb.release(key)


def do_text(text):
    if PORTAL is not None:
        PORTAL.text(text)
        return
    kb.type(text)


# ---------------------------------------------------------------- fs

def list_dir(path=""):
    if not path:
        if platform.system() == "Windows":
            parts = psutil.disk_partitions()
            return {"path": "", "entries": [
                {"name": p.device, "path": p.device, "type": "drive",
                 "size": psutil.disk_usage(p.mountpoint).total}
                for p in parts
            ]}
        path = "/"
    p = Path(path)
    entries = []
    with os.scandir(p) as it:
        for e in it:
            try:
                st = e.stat()
                entries.append({
                    "name": e.name,
                    "path": str(Path(path) / e.name),
                    "type": "dir" if e.is_dir(follow_symlinks=False) else "file",
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                })
            except (PermissionError, OSError):
                continue
    entries.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    return {"path": str(p), "entries": entries}


def read_chunk(path, offset=0, length=CHUNK):
    p = Path(path)
    size = p.stat().st_size
    with open(p, "rb") as f:
        f.seek(int(offset))
        data = f.read(min(int(length), CHUNK))
    return {
        "size": size,
        "offset": offset,
        "data": base64.b64encode(data).decode(),
        "eof": offset + len(data) >= size,
    }


def pc_path(path):
    """A path the phone built, in this PC's own form. Apps built before the
    Linux support join Files-page paths with '\\' ("/home/bl9\\photo.jpg"),
    which on Linux names one odd file (and an upload into the home folder
    then fails on /home's permissions). On a POSIX PC a '\\' in a phone-built
    absolute path can only be that, so it becomes '/'."""
    path = str(path)
    if platform.system() != "Windows" and path.startswith("/") and "\\" in path:
        path = path.replace("\\", "/")
    return path


def write_chunk(path, data, offset=0, append=False):
    raw = base64.b64decode(data)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if append:
        with open(p, "ab") as f:
            f.write(raw)
    else:
        mode = "r+b" if p.exists() else "wb"
        with open(p, mode) as f:
            f.seek(int(offset))
            f.write(raw)
    return {"path": str(p), "size": p.stat().st_size}


def delete_path(path):
    p = Path(path)
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()
    return {"deleted": str(p)}


# ---------------------------------------------------------------- system

def run_command(cmd, cwd=None, timeout=30):
    # Linux: bash, not /bin/sh (dash), so commands behave like a terminal.
    bash = "/bin/bash" if platform.system() == "Linux" \
        and os.path.exists("/bin/bash") else None
    proc = subprocess.run(
        cmd, shell=True, cwd=cwd or None, executable=bash,
        capture_output=True, text=True, timeout=int(timeout),
        errors="replace",
    )
    out = (proc.stdout or "")[-200_000:]
    err = (proc.stderr or "")[-50_000:]
    return {"stdout": out, "stderr": err, "code": proc.returncode}


def list_processes():
    procs = []
    for p in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            procs.append({
                "pid": p.info["pid"],
                "name": p.info["name"],
                "mem": getattr(p.info["memory_info"], "rss", 0),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    procs.sort(key=lambda x: x["mem"], reverse=True)
    return {"processes": procs[:200]}


def kill_process(pid):
    psutil.Process(int(pid)).terminate()
    return {"killed": int(pid)}


def _winapps():
    """The wm.*/apps.* implementation (window list, thumbnails, Start-menu
    apps) lives in winapps.py. Imported lazily: it binds Win32 DLLs at
    import time, and the rest of the agent must still start elsewhere."""
    if platform.system() != "Windows":
        raise ValueError("window and app management is only supported on "
                         "Windows")
    import winapps as mod
    return mod


# How long the phone screen stays plugged in after the last phone left —
# long enough to ride out a Wi-Fi blip or an app restart.
PHONE_IDLE_OFF_S = 120


def _make_phone_monitor(settings, persist):
    """The phone-as-a-second-monitor controller, or None off Windows /
    when its modules fail to load (the rest of the agent must still run)."""
    if platform.system() != "Windows":
        return None
    try:
        from phone_monitor import PhoneMonitor
        return PhoneMonitor(settings=settings, persist=persist)
    except Exception as e:
        print(f"[agent] phone screen unavailable: {e}")
        return None


def _target(msg, key):
    """The window ("window") or Start-menu app ("app") a wm.*/apps.*
    command acts on. It can't ride in "id": that is the request-envelope
    key, so a target sent as "id" overwrites the request id and the reply
    never matches its request (the phone times out although the command
    ran). Older app builds sent wm.activate exactly like that — accept a
    numeric "id" as the window for them."""
    value = msg.get(key)
    if value is None and key == "window" and isinstance(msg.get("id"), int):
        value = msg["id"]
    if value is None:
        raise ValueError(f"missing '{key}'")
    return value


def sys_info():
    w, h = screen_size()
    return {
        "name": platform.node(),
        "platform": f"{platform.system()} {platform.release()}",
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "mem_total": psutil.virtual_memory().total,
        "mem_used": psutil.virtual_memory().used,
        "uptime": int(time.time() - psutil.boot_time()),
        "screen": {"w": w, "h": h},
    }


# ---------------------------------------------------------------- power

POWER_CMDS = {
    "Windows": {
        "lock": "rundll32.exe user32.dll,LockWorkStation",
        "shutdown": "shutdown /s /t 5",
        "restart": "shutdown /r /t 5",
        "sleep": "rundll32.exe powrprof.dll,SetSuspendState 0,1,0",
        "hibernate": "shutdown /h",
    },
    "Linux": {
        # loginctl needs a session id, which a systemd user service lacks;
        # the desktop's screensaver D-Bus API (KDE, GNOME) always works.
        "lock": "loginctl lock-session 2>/dev/null || dbus-send --session "
                "--dest=org.freedesktop.ScreenSaver --type=method_call "
                "/ScreenSaver org.freedesktop.ScreenSaver.Lock",
        "shutdown": "systemctl poweroff",
        "restart": "systemctl reboot",
        "sleep": "systemctl suspend",
        "hibernate": "systemctl hibernate",
    },
    "Darwin": {
        # No hibernate: macOS decides on its own (pmset hibernatemode)
        # whether a sleep also writes the hibernation image.
        "lock": 'osascript -e \'tell application "System Events" to keystroke "q" using {control down, command down}\'',
        "shutdown": "osascript -e 'tell app \"System Events\" to shut down'",
        "restart": "osascript -e 'tell app \"System Events\" to restart'",
        "sleep": "pmset sleepnow",
    },
}


def power_dry_run():
    """SUDO_POWER_DRY_RUN=1 (the test harness always sets it): every power
    command is logged instead of executed — including the harmless-looking
    `shutdown /t` arming and `/a` abort, so a test run can never touch the
    real machine's power state."""
    return os.environ.get("SUDO_POWER_DRY_RUN") == "1"


def power_cmd(action):
    """The shell command for a power action on this OS, or ValueError."""
    system = platform.system()
    cmd = POWER_CMDS.get(system, {}).get(action)
    if not cmd:
        raise ValueError(f"power.{action} not supported on {system}")
    return cmd


def power(action):
    cmd = power_cmd(action)
    if power_dry_run():
        print(f"[agent] power dry-run, would run: {cmd}")
        return {"action": action, "scheduled": True, "dry_run": True}
    # CREATE_NO_WINDOW: the service exe has no console, so the cmd.exe
    # behind shell=True would otherwise flash a console window on screen.
    subprocess.Popen(cmd, shell=True,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {"action": action, "scheduled": True}


# Scheduled power actions (power.schedule). The agent's own asyncio timer
# runs every action; shutdown/restart are ALSO handed to Windows' built-in
# timer (shutdown /s|/r /t N), which fires even if the agent crashes or is
# killed, and makes Windows show its own "you're about to be signed out"
# notice. Firing twice is harmless: whichever comes second finds a shutdown
# already in progress and fails.
POWER_SCHEDULE_ACTIONS = ("shutdown", "restart", "sleep", "hibernate", "lock")
POWER_SCHEDULE_MIN_S = 60
POWER_SCHEDULE_MAX_S = 7 * 24 * 3600
POWER_WARN_S = 60   # the PC-side "Shutting down in 60s — Cancel" notice
# The agent timer wakes at least this often to compare against the wall
# clock: asyncio's clock is monotonic and may not advance while the PC
# sleeps, so one long sleep() could fire hours late after a wake-up.
POWER_TICK_S = 15
# Found the deadline already this far behind (the PC slept through it)?
# Then the moment has passed: running a sleep/lock right after the user
# woke the PC would be hostile, so the schedule is dropped instead.
POWER_LATE_S = 120
POWER_VERBS = {"shutdown": "Shutting down", "restart": "Restarting",
               "sleep": "Going to sleep", "hibernate": "Hibernating",
               "lock": "Locking"}
WIN_TIMED_ACTIONS = {"shutdown": "/s", "restart": "/r"}
ERROR_SHUTDOWN_NOT_IN_PROGRESS = 1116  # shutdown /a with nothing pending
ERROR_SHUTDOWN_IS_SCHEDULED = 1190     # shutdown /t while one is pending


def run_power_exe(argv):
    """Runs one shutdown.exe call to completion -> (exit code, output).
    In dry-run it only logs the exact command line and reports success."""
    line = subprocess.list2cmdline(argv)
    if power_dry_run():
        print(f"[agent] power dry-run, would run: {line}")
        return 0, ""
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=15, errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def win_timer_arm(action, seconds):
    """Arms Windows' own shutdown/restart timer -> (exit code, output).
    The comment shows up in Windows' notice, so whoever sits at the PC
    knows where the countdown came from."""
    return run_power_exe(["shutdown", WIN_TIMED_ACTIONS[action],
                          "/t", str(int(seconds)),
                          "/c", "Scheduled from the Sudo app on your phone."])


def win_timer_abort():
    """shutdown /a; "no shutdown in progress" counts as success."""
    code, out = run_power_exe(["shutdown", "/a"])
    if code not in (0, ERROR_SHUTDOWN_NOT_IN_PROGRESS):
        raise RuntimeError(f"could not cancel the Windows shutdown timer "
                           f"({code}): {out}")


# ---------------------------------------------------------------- media

_MEDIA_PS1_CANDIDATES = (
    Path(getattr(sys, "_MEIPASS", HERE)) / "media_info.ps1",
    HERE / "media_info.ps1",
)


def _media_ps1():
    for cand in _MEDIA_PS1_CANDIDATES:
        if cand.exists():
            return str(cand)
    return str(_MEDIA_PS1_CANDIDATES[-1])


def _run_media_ps1(extra_args):
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", _media_ps1(), *extra_args],
        capture_output=True, text=True, timeout=15, errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out = proc.stdout.strip()
    return json.loads(out) if out else {}


def _playerctl(*args):
    """playerctl (MPRIS, any Linux media player) output, or None when it
    isn't installed or no player is running."""
    try:
        proc = subprocess.run(["playerctl", *args], capture_output=True,
                              text=True, timeout=3, errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _media_info():
    if platform.system() != "Windows":
        out = _playerctl("metadata", "--format",
                         "{{status}}\t{{title}}\t{{artist}}\t"
                         "{{position}}\t{{mpris:length}}\t{{mpris:artUrl}}"
                         "\t{{playerName}}")
        volume = _linux_volume()
        if not out:
            return volume
        status, title, artist, pos, length, art, player = \
            (out.split("\t") + [""] * 7)[:7]

        def seconds(us):
            try:
                return int(us) / 1e6
            except ValueError:
                return 0.0

        return {"title": title, "artist": artist,
                "playing": status == "Playing",
                "position_s": seconds(pos), "duration_s": seconds(length),
                # Only web art: the phone can't open the PC's file:// paths.
                "art_url": art if art.startswith("https://") else "",
                "player": player, **volume}
    return _run_media_ps1([])


def _linux_volume():
    """The default output's volume (0-150 %) and mute, through pactl:
    {"volume": int, "muted": bool}, or {} when pactl can't answer."""
    try:
        vol = subprocess.run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                             capture_output=True, text=True, timeout=2).stdout
        mute = subprocess.run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"],
                              capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    m = re.search(r"(\d+)%", vol)
    if not m:
        return {}
    return {"volume": int(m.group(1)), "muted": "yes" in mute}


# Media buttons on Linux: straight to the player (MPRIS) and the sound
# server, instead of a synthetic key that the desktop may route anywhere
# and that gives no answer. Volume steps match KDE's (5 %), capped at 100.
_LINUX_MEDIA = {
    "media_play_pause": ["playerctl", "play-pause"],
    "media_next": ["playerctl", "next"],
    "media_previous": ["playerctl", "previous"],
    "media_volume_up": ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+5%"],
    "media_volume_down": ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-5%"],
    "media_volume_mute": ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"],
}


def _linux_media(action):
    """Runs a media button on Linux; False when it has no direct command
    (or the tool is missing), so the caller falls back to the key."""
    cmd = _LINUX_MEDIA.get(action)
    if cmd is None or not shutil.which(cmd[0]):
        return False
    try:
        subprocess.run(cmd, capture_output=True, timeout=3)
        if action == "media_volume_up":
            # pactl has no ceiling: keep +5% steps from going past 100 %.
            if _linux_volume().get("volume", 0) > 100:
                subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@",
                                "100%"], capture_output=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return True


def _media_seek(seconds):
    if platform.system() != "Windows":
        _playerctl("position", str(max(0.0, float(seconds))))
        return _media_info()
    return _run_media_ps1(["-Seek", str(seconds)])


# ---------------------------------------------------------------- agent

class EmbeddedRelay:
    """The relay, embedded in the agent: phones connect straight to the PC.
    One exe is the whole PC side — no separate relay process.

    Two listeners share the phones: plain WebSocket on `port` for the LAN,
    and, with remote access on, TLS on `remote_port` for phones outside the
    home network (the router forwards it, see remote_access.py). The remote
    one only admits phones that already hold a trust token from pairing on
    the LAN — no pairing codes, no approval prompts from the internet."""

    def __init__(self, agent, port, remote_port=None, remote_ssl=None):
        self.tls_clients = set()
        self.agent = agent
        self.port = port
        self.remote_port = remote_port
        self.remote_ssl = remote_ssl
        self.clients = {}  # client_id -> websocket
        from remote_access import FailureLimiter
        self.limiter = FailureLimiter()
        self._unauth = {}  # peer ip -> remote sockets not yet authenticated
        # Local port of a tunnel bridge connection -> the phone's real
        # address (tunnel.serve_bridge), so limits apply per phone.
        self.peer_alias = {}
        # Phones connected from outside the LAN (the router forward or the
        # tunnel): likely mobile data, so their screen stream starts lower.
        self.internet_clients = set()

    def _serve(self, handler, port, max_size=8 * 1024 * 1024, **extra):
        return websockets.serve(
            handler, "0.0.0.0", port,
            max_size=max_size, ping_interval=20, ping_timeout=20,
            # No permessage-deflate (the phone offers it, websockets
            # accepts by default): screen frames are JPEG/H.264, which
            # don't compress, yet deflating ~20 of them a second on the
            # event loop stalled it 40-150 ms at a time — lag on every
            # cursor move.
            compression=None, **extra)

    async def serve(self):
        async with self._serve(self._handle, self.port):
            print(f"[agent] listening for phones on :{self.port}")
            self.agent._status(f"Listening on :{self.port}")
            if self.remote_ssl is None:
                await asyncio.Future()  # serve forever
            # 1 MiB: plenty for every command (an fs.upload chunk is ~350
            # KB) while capping what an unauthenticated client can make
            # the event loop parse.
            async with self._serve(self._handle_remote, self.remote_port,
                                   ssl=self.remote_ssl,
                                   max_size=1024 * 1024):
                print(f"[agent] remote access (TLS) on :{self.remote_port}")
                if self.agent.cfg.get("usb_link", True):
                    # Phones plugged in over USB reach the TLS port through
                    # the cable too (usb_link.py).
                    import usb_link
                    self._usb_task = asyncio.create_task(
                        usb_link.keep_reversed(self.remote_port))
                await self.agent.start_tunnel(self)
                await asyncio.Future()  # serve forever

    async def _handle_remote(self, ws):
        await self._handle(ws, remote=True)

    # Internet-facing port limits: unauthenticated sockets per address and
    # in total, and how long one may take to send its pair message.
    UNAUTH_PER_IP = 3
    UNAUTH_TOTAL = 32
    AUTH_TIMEOUT = 10.0

    async def _remote_auth(self, ws, peer):
        """The first message on the TLS port must be a pair carrying a
        token from a LAN pairing, within AUTH_TIMEOUT. Returns the token,
        or None after refusing (counted against the address). Codes and
        approval prompts are LAN-only and never reachable here."""
        if self.limiter.banned(peer) \
                or self._unauth.get(peer, 0) >= self.UNAUTH_PER_IP \
                or sum(self._unauth.values()) >= self.UNAUTH_TOTAL:
            await ws.close(1008, "try again later")
            return None
        self._unauth[peer] = self._unauth.get(peer, 0) + 1
        try:
            try:
                raw = await asyncio.wait_for(ws.recv(), self.AUTH_TIMEOUT)
                msg = json.loads(raw) if isinstance(raw, str) else {}
            except (asyncio.TimeoutError, ValueError):
                msg = {}
            token = str(msg.get("token") or "") \
                if isinstance(msg, dict) and msg.get("type") == "pair" else ""
            if token and not self.limiter.banned(peer) \
                    and token in self.agent.cfg.get("approved_tokens", []):
                self.limiter.succeeded(peer)
                return token
            self.limiter.failed(peer)
            try:
                await ws.send(json.dumps({
                    "type": "error",
                    "error": "this phone isn't paired with this PC: pair on "
                             "the home network first"}))
            finally:
                await ws.close(1008, "not paired")
            return None
        finally:
            left = self._unauth.get(peer, 1) - 1
            if left > 0:
                self._unauth[peer] = left
            else:
                self._unauth.pop(peer, None)

    async def _handle(self, ws, remote=False):
        client_id = uuid.uuid4().hex
        paired = False
        peer = ws.remote_address[0] if ws.remote_address else ""
        if remote and peer in ("127.0.0.1", "::1"):
            # Through the tunnel bridge: use the phone's own address.
            peer = self.peer_alias.get(ws.remote_address[1], peer)
        if remote:
            try:
                token = await self._remote_auth(ws, peer)
            except websockets.ConnectionClosed:
                return
            if token is None:
                return
            paired = True
            self.clients[client_id] = ws
            # TLS (LAN with the pin, router forward, tunnel): the only
            # connections a password may cross (session.unlock).
            self.tls_clients.add(client_id)
            self._limit_send_buffer(ws)
            try:
                await ws.send(json.dumps({
                    "type": "paired",
                    "deviceId": self.agent.cfg["device_id"],
                    "name": self.agent.cfg["name"],
                    "clientId": client_id,
                    "token": token,
                    "remote": True,
                }))
            except websockets.ConnectionClosed:
                self.clients.pop(client_id, None)
                return
            if not is_lan_address(peer):
                # Router forward, or the tunnel bridge (its alias is the
                # phone's Cf-Connecting-Ip, or "tunnel").
                self.internet_clients.add(client_id)
                print(f"[agent] remote phone connected from {peer}")
            self.agent._status("Phone connected")
        elif peer and not is_lan_address(peer):
            # Plain WebSocket from the internet (a manual port forward of the
            # LAN port): tokens would cross it in clear text.
            print(f"[agent] refused plain connection from {peer}; "
                  "outside the LAN only the TLS port is allowed")
            await ws.close(1008, "use the secure remote port")
            return
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if not paired:
                    if msg.get("type") != "pair":
                        await ws.send(json.dumps(
                            {"type": "error", "error": "pair first"}))
                        continue
                    # A non-empty code must match; codeless is fine on LAN.
                    code = str(msg.get("code") or "").strip().upper()
                    if code and code != self.agent.cfg["code"]:
                        await ws.send(json.dumps(
                            {"type": "error", "error": "wrong pairing code"}))
                        continue
                    # Trust flow: an approved token pairs instantly; unknown
                    # phones get a one-time Allow/Deny prompt ON THE PC.
                    token = str(msg.get("token") or "")
                    trusted = bool(token) and token in self.agent.cfg.get(
                        "approved_tokens", [])
                    if not trusted and self.agent.cfg.get(
                            "require_approval", True) \
                            and self.agent.approval is not None:
                        await ws.send(
                            json.dumps({"type": "approval.pending"}))
                        peer = str(msg.get("name") or "").strip()
                        if not peer:
                            peer = (ws.remote_address[0]
                                    if ws.remote_address else "unknown")
                        allowed = await self.agent.approval.ask(peer)
                        if not allowed:
                            await ws.send(json.dumps(
                                {"type": "error",
                                 "error": "denied on this PC"}))
                            await ws.close()
                            return
                        token = uuid.uuid4().hex
                        self.agent.cfg.setdefault(
                            "approved_tokens", []).append(token)
                        save_config(self.agent.cfg)
                    paired = True
                    self.clients[client_id] = ws
                    self._limit_send_buffer(ws)
                    await ws.send(json.dumps({
                        "type": "paired",
                        "deviceId": self.agent.cfg["device_id"],
                        "name": self.agent.cfg["name"],
                        "clientId": client_id,
                        "token": token,
                    }))
                    self.agent._status("Phone connected")
                    continue
                msg["from"] = client_id
                await self.agent.on_message(json.dumps(msg))
        except websockets.ConnectionClosed:
            pass
        finally:
            if paired:
                self.clients.pop(client_id, None)
                self.tls_clients.discard(client_id)
                self.internet_clients.discard(client_id)
                await self.agent.drop_client(client_id)

    async def send_to(self, client_id, obj):
        ws = self.clients.get(client_id)
        if ws:
            await ws.send(json.dumps(obj))

    async def broadcast(self, obj):
        data = json.dumps(obj)
        for ws in list(self.clients.values()):
            try:
                await ws.send(data)
            except websockets.ConnectionClosed:
                pass

    async def broadcast_bytes(self, data: bytes):
        for ws in list(self.clients.values()):
            try:
                await ws.send(data)
            except websockets.ConnectionClosed:
                pass

    # Kernel send buffer per phone socket. Windows otherwise lets a socket
    # soak up over a megabyte (measured on loopback with a slow reader):
    # every send "completes" instantly while seconds of stale frames queue
    # up, and the streamers never see the link back up. The kernel still
    # accepts one send past this, so a 128 KB cap keeps ~one frame queued
    # and still covers ~50 Mbit/s at a 20 ms Wi-Fi RTT.
    SEND_BUFFER_BYTES = 128 * 1024

    def _limit_send_buffer(self, ws):
        try:
            ws.transport.get_extra_info("socket").setsockopt(
                socket.SOL_SOCKET, socket.SO_SNDBUF, self.SEND_BUFFER_BYTES)
        except (OSError, AttributeError):
            pass  # keep the OS default; congestion detection degrades only

    async def send_bytes_to(self, client_id, data: bytes):
        ws = self.clients.get(client_id)
        if ws:
            try:
                await ws.send(data)
            except websockets.ConnectionClosed:
                pass  # drop_client tears the streams down

    def write_buffer_size(self, client_id=None):
        """Bytes queued in a phone's socket transport (the largest one when
        client_id is None) — what that phone's link hasn't absorbed yet."""
        sockets = [self.clients.get(client_id)] if client_id is not None \
            else list(self.clients.values())
        return max([ws.transport.get_write_buffer_size()
                    for ws in sockets if ws is not None] or [0])


class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ws = None
        self.streamers = {}          # client_id -> ScreenStreamer
        self.client_monitors = {}    # client_id -> streamed monitor spec
        self.known_clients = set()
        self._lanes = {}             # client_id -> (queue, worker task)
        self._clipboard_last = None
        self._clipboard_watch = False
        self.on_status = None        # optional callback(str), used by the GUI
        self.loop = None             # set in run()
        self.camera_streamers = {}   # client_id -> CameraStreamer
        self.video_streamers = {}    # client_id -> H264Streamer
        self.embedded = None         # EmbeddedRelay when running standalone
        self.approval = None         # ApprovalManager (service mode)
        self.brightness = BrightnessControl()
        # wm.* / apps.* on KDE Plasma (X11 and Wayland) go through KWin
        # scripts (kdeapps.py); on Windows through winapps.py.
        self.kde_apps = kdeapps.KdeApps(list_monitors) \
            if platform.system() == "Linux" and kdeapps.available() else None
        # Wayland: the screen and input through the desktop portal
        # (wayland_portal.py). Started by the first screen.start or input;
        # stopped a minute after the last phone left.
        global PORTAL
        self.portal = wayland_portal.Portal(
            _default_config_path().parent / "portal_token") \
            if wayland_session() and wayland_portal.available() else None
        PORTAL = self.portal
        self._portal_idle_task = None
        # Scheduled power action: {"action", "at" (unix s)} or None. Kept
        # in cfg["power_schedule"] too, so power.status survives a restart.
        self.power_sched = None
        self._power_task = None      # the agent-side timer for power_sched
        self._power_lock = asyncio.Lock()  # schedule/cancel/fire one at a time
        # Phone as a second monitor (Parsec virtual display + touch input).
        # Constructing it touches no driver; nothing is plugged in until a
        # phone asks for phone.enable.
        self.phone = _make_phone_monitor(cfg.get("phone_monitor"),
                                         self._phone_persist)
        self._phone_lock = asyncio.Lock()  # enable/disable/move one at a time
        self._phone_idle_task = None       # auto-off after the phones left
        # Phone-screen apps' sound played on the phone (phone_audio.py):
        # created on first phone.audio, streams to one client at a time.
        self.phone_audio = None
        self._phone_audio_client = None
        # The PC's whole sound played on a phone (pc_audio.py, audio.*):
        # created on the first audio.start, one listening phone at a time.
        # Its socket going away starts a grace period (_pc_audio_grace)
        # instead of stopping at once.
        self.pc_audio = None
        self._pc_audio_client = None
        self._pc_audio_grace = None
        self._pc_audio_lock = asyncio.Lock()  # start/stop one at a time
        # Last message from the listener, and the listeners that send
        # audio.alive heartbeats (see _pc_audio_watchdog).
        self._pc_audio_alive_at = 0.0
        self._pc_audio_heartbeat = set()
        self._pc_audio_watch = None
        self._pc_audio_stats_at = 0.0
        # The PC's half of those stats, since the last logged line: packets
        # sent, dropped (backed-up link), and the longest wait between a
        # packet's encoding and the event loop sending it.
        self._pc_audio_sent = 0
        self._pc_audio_dropped = 0
        self._pc_audio_lag = 0.0
        # Media players paused because the listening phone dropped (see
        # _pc_audio_pause): played again if it comes back within the grace
        # period, left paused otherwise. The task is the pause in flight.
        self._pc_audio_paused = []
        self._pc_audio_pausing = None
        # Window lock (window.lock, KDE): the locked window's {id, title,
        # prev_above}, also in cfg[WINDOW_LOCK_KEY] while on, so a crashed
        # agent's lock is undone by the next one. Released when no phone is
        # left for WINDOW_LOCK_GRACE_S.
        self._unlock_lock = asyncio.Lock()
        self._unlock_fails = 0
        self._unlock_blocked_until = 0.0
        self._wlock = None
        self._wlock_lock = asyncio.Lock()
        self._wlock_grace = None
        # Remote access from outside the LAN (remote_access.RemoteAccess),
        # set up in run() for the embedded relay.
        self.remote = None
        # The Cloudflare quick tunnel (tunnel.QuickTunnel) and its bridge,
        # for networks whose provider blocks incoming connections.
        self.tunnel = None
        self._bridge = None
        # Client-drawn cursor (ScreenStreamer.client_cursor): the last
        # pointer input `seq` handled per phone, echoed in `cursor` events
        # so the phone can drop the moves the PC has already applied, and
        # the per-phone sender state of those events (push_cursor).
        self._input_seq = {}
        self._cursor_push = {}

    def _status(self, text):
        cb = self.on_status
        if cb:
            try:
                cb(text)
            except Exception:
                pass

    async def send(self, obj):
        if self.embedded:
            await self.embedded.broadcast(obj)
        elif self.ws:
            await self.ws.send(json.dumps(obj))

    async def send_bytes(self, data: bytes):
        """Raw binary frame to every paired phone."""
        if self.embedded:
            await self.embedded.broadcast_bytes(data)
        elif self.ws:
            await self.ws.send(data)

    async def send_bytes_to(self, client_id, data: bytes):
        """Raw binary packet (screen/camera/video) for ONE client — a second
        phone must not pay for, or be slowed by, another phone's stream.
        The outbound relay protocol has no per-client binary routing, so
        there it goes out as before and the relay delivers it."""
        if self.embedded:
            await self.embedded.send_bytes_to(client_id, data)
        elif self.ws:
            await self.ws.send(data)

    # Write-buffer depth that counts as a backed-up link. The embedded relay
    # measures one phone's own socket, where every send already waits for
    # the previous one to drain — 512 KB queued is a real backlog (a
    # couple of max-preset frames). The outbound relay socket is shared.
    EMBEDDED_CONGESTED_BYTES = 512 * 1024
    RELAY_CONGESTED_BYTES = 2 * 1024 * 1024

    def congested(self, client_id=None):
        """True when the socket carrying this client's stream is backing
        up — streamers drop frames rather than letting backpressure freeze
        the agent or pile latency onto the phone."""
        try:
            if self.embedded:
                return self.embedded.write_buffer_size(client_id) \
                    > self.EMBEDDED_CONGESTED_BYTES
            return bool(self.ws) and self.ws.transport.get_write_buffer_size() \
                > self.RELAY_CONGESTED_BYTES
        except Exception:
            return False

    def internet_client(self, client_id):
        """True for a phone connected from outside the home network (see
        EmbeddedRelay.internet_clients)."""
        return self.embedded is not None \
            and client_id in self.embedded.internet_clients

    def resolve_monitor(self, spec="primary", max_age=MONITORS_TTL):
        """Capture rect {left, top, width, height} for a screen.start
        `monitor` spec: an index into mss().monitors (0 = all monitors) or
        "primary". Overridable — the phone-as-second-monitor feature adds
        its own specs on top. Raises ValueError for unknown specs."""
        if spec == "phone":
            # The virtual monitor only the phone sees. Its rect comes straight
            # from the driver's GDI device, never from a cached index: the
            # monitor list reshuffles whenever it is plugged or unplugged.
            r = self.phone.rect() if self.phone and self.phone.enabled else None
            if r is None:
                raise ValueError("the phone screen is off")
            return {"left": r[0], "top": r[1],
                    "width": r[2] - r[0], "height": r[3] - r[1]}
        monitors = list_monitors(max_age)
        if spec is None or spec == "primary":
            return monitor_rect(primary_monitor(monitors))
        if isinstance(spec, str) and spec.strip().isdigit():
            spec = int(spec)
        if isinstance(spec, int) and not isinstance(spec, bool):
            if 0 <= spec < len(monitors):
                return monitor_rect(monitors[spec])
            raise ValueError(f"no monitor {spec} (this PC has "
                             f"{len(monitors) - 1})")
        raise ValueError(f"unknown monitor: {spec!r}")

    def input_rect(self, client_id):
        """The monitor a client's absolute/relative pointer input maps
        into: the one it is streaming, else None (= primary)."""
        spec = self.client_monitors.get(client_id)
        if spec is None:
            return None
        try:
            return self.resolve_monitor(spec)
        except ValueError:
            return None  # that monitor is gone: fall back to the primary

    async def send_to(self, client_id, obj):
        if self.embedded:
            await self.embedded.send_to(client_id, obj)
            return
        obj["to"] = client_id
        await self.send(obj)

    async def reply(self, msg, ok=True, data=None, error=None):
        out = {"id": msg.get("id"), "ok": ok}
        if ok:
            out["data"] = data if data is not None else {}
        else:
            out["error"] = str(error)
        await self.send_to(msg.get("from"), out)

    # ---- command handlers: return data or raise ----

    async def h_ping(self, msg):
        return {"pong": True, "ts": time.time()}

    async def h_sys_info(self, msg):
        return await asyncio.to_thread(sys_info)

    async def h_sys_exec(self, msg):
        # The shell text rides in "command" — the envelope key "cmd" is the
        # routing field ("sys.exec"), so a param named "cmd" would collide.
        return await asyncio.to_thread(
            run_command, msg.get("command", ""),
            msg.get("cwd"), msg.get("timeout", 30),
        )

    async def h_sys_ps(self, msg):
        return await asyncio.to_thread(list_processes)

    async def h_sys_kill(self, msg):
        return await asyncio.to_thread(kill_process, msg.get("pid"))

    async def h_fs_list(self, msg):
        return await asyncio.to_thread(list_dir, msg.get("path", ""))

    async def h_fs_shortcuts(self, msg):
        home = Path.home()
        items = [("Home", str(home))]
        for name in ("Desktop", "Documents", "Downloads", "Pictures", "Videos"):
            p = home / name
            if p.exists():
                items.append((name, str(p)))
        if platform.system() == "Windows":
            items.append(("Drives", ""))
        else:
            items.append(("Computer", "/"))
        return {"shortcuts": [{"name": n, "path": p} for n, p in items]}

    async def h_fs_download(self, msg):
        return await asyncio.to_thread(
            read_chunk, msg["path"], msg.get("offset", 0), msg.get("length", CHUNK)
        )

    async def h_fs_upload(self, msg):
        return await asyncio.to_thread(
            write_chunk, pc_path(msg["path"]), msg["data"],
            msg.get("offset", 0), msg.get("append", False),
        )

    async def h_fs_delete(self, msg):
        return await asyncio.to_thread(delete_path, msg["path"])

    async def h_fs_mkdir(self, msg):
        path = pc_path(msg["path"])
        Path(path).mkdir(parents=True, exist_ok=True)
        return {"path": path}

    def _cursor_data(self, client_id):
        """The `cursor` event for a phone drawing the pointer itself: the
        position as a fraction of its streamed monitor (outside 0..1 when
        the pointer is on another monitor) and the last input seq applied.
        None when that phone's stream doesn't use a client cursor."""
        streamer = self.streamers.get(client_id)
        if streamer is None or not streamer.client_cursor:
            return None
        try:
            pos = mouse.position
        except Exception:
            pos = None
        if pos is None:
            # Windows: GetCursorPos fails while another desktop has the
            # input (lock screen, UAC, Ctrl+Alt+Del). cursor_watcher pushes
            # the position once it can be read again.
            return None
        x, y = pos
        rect = streamer.rect
        return {"x": (x - rect["left"]) / rect["width"],
                "y": (y - rect["top"]) / rect["height"],
                "seq": self._input_seq.get(client_id, 0)}

    def push_cursor(self, client_id):
        """Send that phone a `cursor` event, never waiting for it: input is
        handled inline in the socket's receive loop, which a send behind a
        backed-up stream must not hold. At most one send per phone is out;
        positions that change meanwhile are folded into one more send of
        the newest."""
        state = self._cursor_push.get(client_id)
        if state is not None:
            state["dirty"] = True
            return
        streamer = self.streamers.get(client_id)
        if streamer is None or not streamer.client_cursor:
            return
        state = self._cursor_push[client_id] = {"dirty": True}

        async def run():
            try:
                while state["dirty"]:
                    state["dirty"] = False
                    data = self._cursor_data(client_id)
                    if data is None:
                        break
                    await self.send_to(client_id,
                                       {"event": "cursor", "data": data})
            except Exception:
                pass  # socket gone: drop_client cleans up
            finally:
                if self._cursor_push.get(client_id) is state:
                    del self._cursor_push[client_id]

        asyncio.ensure_future(run())

    async def h_input(self, msg):
        action = msg.get("action")
        # Pointer coordinates are relative to the monitor this client is
        # watching (the primary unless its screen.start picked another).
        rect = self.input_rect(msg.get("from")) \
            if action in ("move", "move_rel", "click", "down", "up") else None
        if action == "move":
            do_move(msg.get("x", 0), msg.get("y", 0), rect)
        elif action == "move_rel":
            do_move_rel(msg.get("dx", 0), msg.get("dy", 0), rect)
        elif action == "click":
            do_click(msg.get("button", "left"), "click",
                     msg.get("x"), msg.get("y"), msg.get("count", 1), rect)
        elif action == "down":
            do_click(msg.get("button", "left"), "down", msg.get("x"),
                     msg.get("y"), rect=rect)
        elif action == "up":
            do_click(msg.get("button", "left"), "up", msg.get("x"),
                     msg.get("y"), rect=rect)
        elif action == "scroll":
            do_scroll(msg.get("dx", 0), msg.get("dy", 0))
        elif action == "key":
            do_key(msg["key"])
        elif action == "text":
            do_text(msg.get("text", ""))
        else:
            raise ValueError(f"unknown input action: {action}")
        client = msg.get("from")
        if action in ("move", "move_rel", "click", "down", "up"):
            seq = msg.get("seq")
            if isinstance(seq, int):
                self._input_seq[client] = seq
            self.push_cursor(client)
        # Any input changes the screen: wake the streamer NOW so the frame
        # capturing this change isn't delayed by the idle tick.
        s = self.streamers.get(client)
        if s:
            s.wake()
        return {}

    async def h_power(self, msg):
        action = msg.get("action", "lock")
        if action in WIN_TIMED_ACTIONS and self.power_sched:
            # "Shut down now" supersedes a scheduled action — and on Windows
            # a pending shutdown timer would make it fail outright ("already
            # scheduled"), so that timer goes first.
            async with self._power_lock:
                prev = self.power_sched
                if prev:
                    await self._power_disarm(
                        abort_windows=prev["action"] in WIN_TIMED_ACTIONS)
                    await self._power_broadcast(self.power_status())
        return power(action)

    async def h_power_schedule(self, msg):
        action = str(msg.get("action") or "")
        if action not in POWER_SCHEDULE_ACTIONS:
            raise ValueError(f"unknown power action: {action or '(none)'}")
        power_cmd(action)  # unsupported on this OS: fail now, not at the deadline
        try:
            seconds = round(float(msg["seconds"]))
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError("seconds is required") from None
        if not POWER_SCHEDULE_MIN_S <= seconds <= POWER_SCHEDULE_MAX_S:
            raise ValueError(f"seconds must be between {POWER_SCHEDULE_MIN_S}"
                             f" and {POWER_SCHEDULE_MAX_S}")
        async with self._power_lock:
            try:
                await self._power_arm(action, round(time.time() + seconds, 3))
            finally:
                # Also after a failed arm: the old schedule is gone by then,
                # and the other phones must not keep counting it down.
                # Inside the lock: racing reschedules must reach the phones
                # in the order they were applied, or they'd show a stale one.
                status = self.power_status()
                await self._power_broadcast(status)
        return status

    async def h_power_cancel(self, msg):
        async with self._power_lock:
            await self._power_disarm(abort_windows=True)
            status = self.power_status()
            await self._power_broadcast(status)
        return status

    async def h_power_status(self, msg):
        return self.power_status()

    def _detach(self, msg, work):
        """Runs a slow handler's work as its own task and answers from
        there. A phone's requests run one at a time on its lane (see
        FAST_CMDS), so awaiting a ~0.5s PowerShell launch in the handler
        would hold up its NEXT request — e.g. the following slider values,
        which then could never be merged by BrightnessControl. Taking the id off the message tells on_message
        not to reply itself; the task replies once the work is done."""
        rid = msg.pop("id", None)
        route = {"id": rid, "from": msg.get("from")}

        async def run():
            try:
                data, error = await work, None
            except Exception as e:
                data, error = None, e
            if not rid:
                if error is not None:
                    print(f"[agent] error in {msg.get('cmd')}: {error}")
                return
            try:
                await self.reply(route, ok=error is None, data=data,
                                 error=error)
            except websockets.ConnectionClosed:
                pass  # the phone left meanwhile: nobody to answer

        asyncio.create_task(run())
        return {}

    async def h_display_brightness_get(self, msg):
        return self._detach(msg, self.brightness.get())

    async def h_display_brightness_set(self, msg):
        # The target rides in "display": "id" is the envelope's request id
        # (same collision as sys.exec's "command" vs "cmd").
        return self._detach(msg, self.brightness.set(
            level=msg.get("level"), delta=msg.get("delta"),
            display_id=msg.get("display")))

    async def h_media(self, msg):
        """A media button. On Linux it answers with the state after it
        (`info`), so the phone's card updates at once; the media watcher
        tells the other phones."""
        action = msg.get("action", "media_play_pause")
        if platform.system() != "Windows" and \
                await asyncio.to_thread(_linux_media, action):
            # MPRIS applies a command asynchronously: give the player a
            # beat so the answer already shows the new state.
            await asyncio.sleep(0.15)
            return {"info": await asyncio.to_thread(_media_info)}
        do_key(action)
        return {}

    async def h_media_info(self, msg):
        # A 1-3 s PowerShell run: off the lane, see _detach.
        return self._detach(msg, asyncio.to_thread(_media_info))

    async def h_media_seek(self, msg):
        return await asyncio.to_thread(_media_seek, float(msg.get("seconds", 0)))

    async def h_clipboard_get(self, msg):
        return {"text": pyperclip.paste()}

    async def h_clipboard_set(self, msg):
        text = msg.get("text", "")
        self._clipboard_last = text  # suppress watcher echo
        pyperclip.copy(text)
        return {"text": text}

    async def h_clipboard_watch(self, msg):
        self._clipboard_watch = bool(msg.get("enabled", True))
        return {"watching": self._clipboard_watch}

    async def h_screen_start(self, msg):
        client = msg.get("from")
        preset = str(msg.get("preset") or "").lower() or None
        codec = str(msg.get("codec")
                    or ("h264" if preset == "smooth" else "jpeg")).lower()
        spec = msg.get("monitor", "primary")
        # Validate everything before touching the running stream: a bad
        # request must fail without freezing the phone's current picture.
        # max_age=0: monitors may have changed since the last query.
        if wayland_session():
            if self.portal is None:
                raise ValueError(
                    "This PC is in a Wayland session and the screen-sharing "
                    "pieces are missing: re-run agent/linux/install.sh (it "
                    "installs python3-gi, GStreamer and dbus-fast).")
            if spec not in (None, "primary"):
                raise ValueError("On Wayland only the main screen can be "
                                 "shown for now.")
            # The first time, the PC shows "Allow remote control".
            await self.portal.ensure()
            rect = dict(self.portal.rect)
        else:
            rect = await asyncio.to_thread(self.resolve_monitor, spec, 0)
        settings = None
        if codec != "h264":
            settings = screen_settings(msg, rect["width"],
                                       internet=self.internet_client(client))
        old = self.streamers.pop(client, None)
        stale = 0
        if old:
            await old.stop()
            # Its frames already on the way are acked after this request.
            stale = old.unacked_sent() if old.acks else 0
        old_v = self.video_streamers.pop(client, None)
        if old_v:
            await old_v.stop()
        self.client_monitors[client] = spec
        if codec == "h264":
            streamer = H264Streamer(
                self, client, rect,
                fps=msg.get("fps") or 30,
                max_width=msg.get("max_width") or 1920,
                bitrate=msg.get("bitrate") or 6_000_000,
            )
            streamer.start()
            self.video_streamers[client] = streamer
            return {"streaming": True, "codec": "h264", "binary": True,
                    "preset": preset or "custom", "fps": streamer.fps,
                    "bitrate": streamer.bitrate, "w": streamer.width,
                    "h": streamer.height, "max_width": streamer.width,
                    "monitor": rect}
        streamer = ScreenStreamer(
            self, client, settings, rect, spec,
            binary=bool(msg.get("binary", False)),
            acks=bool(msg.get("ack", False)),
            client_cursor=msg.get("cursor") == "client")
        if streamer.acks and stale:
            # Same socket, and acks carry no stream id: absorb the old
            # stream's (bounded in time, in case they never come).
            streamer._stale_acks = stale
            streamer._stale_until = time.monotonic() + 3.0
        streamer.start()
        self.streamers[client] = streamer
        w, h = streamer.frame_size()
        return {"streaming": True, "codec": "jpeg", "fps": streamer.fps,
                "binary": streamer.binary, "preset": streamer.preset,
                "quality": streamer._current()[1],
                "max_width": streamer.max_width,
                "subsampling": streamer.subsampling,
                "adaptive": streamer.adaptive, "w": w, "h": h,
                "ack": streamer.acks, "monitor": rect,
                "cursor": "client" if streamer.client_cursor else "frame",
                # The phone's first pointer position, in the reply so it
                # can't arrive before the phone knows to draw it; `cursor`
                # events follow every change.
                "pointer": self._cursor_data(client)}

    async def h_remote_info(self, msg):
        """How a paired phone reaches this PC from outside the home network:
        the router's public address, the forwarded TLS port and the
        certificate fingerprint to pin, plus the tunnel address (`tunnel`)
        and the secret topic that always holds its latest value
        (`rendezvous`). The phone stores it while on the LAN (and refreshes
        it on every connection)."""
        return self._remote_info()

    def _remote_info(self):
        if self.remote is None:
            return {"available": False, "error": "no secure port"}
        info = self.remote.info()
        if self.tunnel is not None:
            t = self.tunnel.info()
            info.update(tunnel=t["url"], tunnelError=t["error"],
                        rendezvous=t["rendezvous"])
        return info

    def _remote_changed(self, _info=None):
        """RemoteAccess/QuickTunnel thread: the public address, mapping or
        tunnel changed — tell connected phones so they keep the new one."""
        loop = self.loop
        if loop is None:
            return
        info = self._remote_info()
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(
            self.send({"event": "remote.changed", "data": info})))

    async def start_tunnel(self, relay):
        """The Cloudflare quick tunnel into the TLS port (tunnel.py), unless
        remote access or the tunnel is switched off in config. Its topic is
        made once and kept: paired phones hold it."""
        if self.remote is None or not self.cfg.get("remote_access", True) \
                or not self.cfg.get("tunnel", True):
            return
        import tunnel
        topic = self.cfg.get("tunnel_topic")
        if not topic:
            topic = self.cfg["tunnel_topic"] = tunnel.new_topic()
            save_config(self.cfg)
        server = str(self.cfg.get("ntfy_server") or tunnel.NTFY_SERVER)
        try:
            self._bridge, port = await tunnel.serve_bridge(
                self.remote.port, relay.peer_alias)
        except OSError as e:
            print(f"[agent] tunnel disabled: {e}")
            return
        self.tunnel = tunnel.QuickTunnel(
            port, _default_config_path().parent,
            f"{server.rstrip('/')}/{topic}")
        self.tunnel.on_change = self._remote_changed
        self.tunnel.start()

    def _setup_remote(self):
        """The TLS listener (cert, context) and, unless remote access is
        switched off in config, the router port forward. The TLS port always
        runs once a phone may have pinned it: paired phones use it on the
        LAN too, so their token never crosses a network in clear text.
        `remote_access: false` only takes the PC off the internet. None if
        TLS can't be set up — plain LAN keeps working."""
        import remote_access
        port = int(self.cfg.get("remote_port") or remote_access.DEFAULT_PORT)
        try:
            # Always the per-user folder (never a dev config.json's folder
            # inside the git checkout): the private key must not end up in
            # the repo.
            cert, key, fingerprint = remote_access.ensure_certificate(
                _default_config_path().parent, f"Sudo {self.cfg['name']}")
            ctx = remote_access.server_ssl_context(cert, key)
        except Exception as e:
            print(f"[agent] secure port disabled: {e}")
            return None
        self.remote = remote_access.RemoteAccess(port, fingerprint, lan_ip)
        if self.cfg.get("remote_access", True):
            self.remote.on_change = self._remote_changed
            self.remote.start()
        else:
            self.remote.error = "remote access is off"
            # Take down a forward an earlier run left behind.
            threading.Thread(target=remote_access.remove_mapping,
                             args=(port, lan_ip()), daemon=True).start()
        return port, ctx

    async def h_screen_ack(self, msg):
        streamer = self.streamers.get(msg.get("from"))
        if streamer:
            streamer.ack()
        return {}

    async def h_screen_stop(self, msg):
        client = msg.get("from")
        # No stream = no streamed monitor: pointer input maps to the
        # primary again.
        self.client_monitors.pop(client, None)
        streamer = self.streamers.pop(client, None)
        if streamer:
            await streamer.stop()
        video = self.video_streamers.pop(client, None)
        if video:
            await video.stop()
        return {"streaming": False}

    async def h_camera_start(self, msg):
        client = msg.get("from")
        old = self.camera_streamers.pop(client, None)
        if old:
            await old.stop()
        streamer = CameraStreamer(
            self, client,
            fps=msg.get("fps", 10), max_width=msg.get("max_width", 640),
        )
        streamer.start()
        self.camera_streamers[client] = streamer
        return {"streaming": True}

    async def h_camera_stop(self, msg):
        client = msg.get("from")
        streamer = self.camera_streamers.pop(client, None)
        if streamer:
            await streamer.stop()
        return {"streaming": False}

    async def h_wm_list(self, msg):
        if self.kde_apps:
            return await self.kde_apps.list_windows(bool(msg.get("icons", True)))
        winapps = _winapps()
        # The phone lists windows as the Screen page opens and its switcher
        # asks for the Start-menu apps soon after: load those in the
        # background now (once), so a cold PowerShell never sits in this
        # phone's in-order command stream ahead of thumbnails and taps.
        winapps.prefetch_start_apps()
        # icons:false lets a periodic refresh (the app dock) skip the PNGs
        # it already has — each is a few KB of base64.
        return await asyncio.to_thread(
            winapps.list_windows, bool(msg.get("icons", True)))

    async def h_wm_thumb(self, msg):
        if self.kde_apps:
            return await self.kde_apps.window_thumb(
                _target(msg, "window"), msg.get("max_width", 360))
        return await asyncio.to_thread(
            _winapps().window_thumb, _target(msg, "window"),
            msg.get("max_width", 360))

    async def h_wm_activate(self, msg):
        if self.kde_apps:
            return await self.kde_apps.activate_window(_target(msg, "window"))
        return await asyncio.to_thread(
            _winapps().activate_window, _target(msg, "window"))

    async def h_wm_close(self, msg):
        if self.kde_apps:
            return await self.kde_apps.close_window(_target(msg, "window"))
        return await asyncio.to_thread(
            _winapps().close_window, _target(msg, "window"))

    async def h_wm_minimize(self, msg):
        if self.kde_apps:
            return await self.kde_apps.minimize_window(_target(msg, "window"))
        return await asyncio.to_thread(
            _winapps().minimize_window, _target(msg, "window"))

    async def h_wm_maximize(self, msg):
        if self.kde_apps:
            return await self.kde_apps.maximize_window(_target(msg, "window"))
        return await asyncio.to_thread(
            _winapps().maximize_window, _target(msg, "window"))

    async def h_wm_restore(self, msg):
        if self.kde_apps:
            return await self.kde_apps.restore_window(_target(msg, "window"))
        return await asyncio.to_thread(
            _winapps().restore_window, _target(msg, "window"))

    async def h_apps_list(self, msg):
        if self.kde_apps:
            return await self.kde_apps.list_start_apps(bool(msg.get("refresh", False)))
        return await asyncio.to_thread(
            _winapps().list_start_apps, bool(msg.get("refresh", False)))

    async def h_apps_icon(self, msg):
        if self.kde_apps:
            return await self.kde_apps.start_app_icon(_target(msg, "app"))
        return await asyncio.to_thread(
            _winapps().start_app_icon, _target(msg, "app"))

    async def h_apps_launch(self, msg):
        if self.kde_apps:
            return await self.kde_apps.launch_start_app(_target(msg, "app"))
        return await asyncio.to_thread(
            _winapps().launch_start_app, _target(msg, "app"))

    # ---- phone as a second monitor (see phone_monitor.py) ----

    def _phone(self):
        if self.phone is None:
            raise ValueError("the phone screen is only supported on Windows")
        return self.phone

    async def _phone_broadcast(self, status):
        """Every phone hears about every change (apps moved, monitor on/off),
        so a second phone's chip row never goes stale. Best effort."""
        try:
            await self.send({"event": "phone.status", "data": status})
        except Exception as e:
            print(f"[agent] phone.status broadcast failed: {e}")

    async def _phone_change(self, fn, *args):
        """Runs one blocking monitor change off the loop — display mode
        switches take a second or two — then tells every phone."""
        async with self._phone_lock:
            status = await asyncio.to_thread(fn, *args)
        await self._phone_broadcast(status)
        return status

    async def h_phone_status(self, msg):
        return await asyncio.to_thread(self._phone().status)

    async def h_phone_setup(self, msg):
        """One-time driver install. It waits on a UAC prompt on the PC, far
        longer than a request may take, so it answers {started} at once and
        reports progress as phone.setup events."""
        phone = self._phone()
        loop = asyncio.get_running_loop()

        def progress(state):  # called on the install thread
            data = {k: state[k] for k in ("stage", "error") if k in state}
            asyncio.run_coroutine_threadsafe(
                self.send({"event": "phone.setup", "data": data}), loop)

        async def run():
            try:
                await asyncio.to_thread(phone.install, progress)
            except Exception as e:
                progress({"stage": "error", "error": str(e)})
            await self._phone_broadcast(await asyncio.to_thread(phone.status))

        asyncio.create_task(run())
        return {"started": True}

    async def h_phone_enable(self, msg):
        phone = self._phone()
        return await self._phone_change(
            phone.enable, msg.get("size"), msg.get("orientation"),
            msg.get("scale"))

    async def h_phone_disable(self, msg):
        await self._phone_audio_stop()
        return await self._phone_change(self._phone().disable)

    async def h_phone_settings(self, msg):
        phone = self._phone()
        return await self._phone_change(
            phone.update_settings, msg.get("size"), msg.get("orientation"),
            msg.get("scale"), msg.get("return_focus"))

    async def h_phone_move(self, msg):
        phone = self._phone()
        hwnd = int(_target(msg, "window"))
        if not phone.enabled:
            await self._phone_change(phone.enable)
        return await self._phone_change(phone.move_window_here, hwnd)

    async def h_phone_return(self, msg):
        return await self._phone_change(
            self._phone().move_window_back, int(_target(msg, "window")))

    async def h_phone_touch(self, msg):
        # Hot path (up to ~60/s while a finger moves): the injector queues
        # the frame onto its own thread, so this never blocks the loop.
        self._phone().touch(msg.get("action"), msg.get("pointer", 0),
                            msg.get("x"), msg.get("y"))
        return {}

    async def h_phone_text(self, msg):
        await asyncio.to_thread(self._phone().text, str(msg.get("text", "")))
        return {}

    async def h_phone_key(self, msg):
        await asyncio.to_thread(self._phone().key, str(msg.get("key", "")))
        return {}

    async def h_phone_wheel(self, msg):
        await asyncio.to_thread(
            self._phone().wheel, msg.get("x"), msg.get("y"),
            msg.get("dx", 0), msg.get("dy", 0))
        return {}

    async def h_phone_audio(self, msg):
        """phone.audio {enabled}: play the phone screen's apps on THIS
        phone (silenced on the PC), or hand the sound back to the PC."""
        if not msg.get("enabled", True):
            await self._phone_audio_stop()
            return {"enabled": False}
        self._phone()
        client = msg.get("from")
        loop = asyncio.get_running_loop()
        if self.phone_audio is None:
            import phone_audio
            self.phone_audio = phone_audio.PhoneAudio(
                on_chunk=lambda pcm: loop.call_soon_threadsafe(
                    self._phone_audio_send, pcm),
                saved=self.cfg.get("phone_audio_saved"),
                persist=self._phone_audio_persist)
        self._phone_audio_client = client
        self.phone_audio.start()
        await asyncio.to_thread(self.phone_audio.set_targets,
                                self.phone.window_pids())
        return {"enabled": True, "rate": 48000, "channels": 2,
                "errors": self.phone_audio.errors()}

    def _phone_audio_send(self, pcm):
        """Loop thread: one 40 ms chunk to the listening phone. Dropped
        when its link is backed up: late sound is worse than a gap."""
        client = self._phone_audio_client
        if client is None or self.congested(client):
            return
        header = b'{"rate":48000,"ch":2,"fmt":"s16"}'
        packet = b"PCM1" + len(header).to_bytes(4, "little") + header + pcm
        asyncio.ensure_future(self.send_bytes_to(client, packet))

    async def _phone_audio_stop(self):
        self._phone_audio_client = None
        if self.phone_audio is not None and self.phone_audio.running:
            await asyncio.to_thread(self.phone_audio.stop)

    def _phone_audio_persist(self, saved):
        """Original mixer volumes of the apps turned down for the phone,
        written BEFORE they are touched, so a crash can't leave an app
        near-silent on the PC for good (restored on the next start)."""
        self.cfg["phone_audio_saved"] = dict(saved)
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("config.json is not a JSON object")
            data["phone_audio_saved"] = dict(saved)
            save_config(data)
        except (OSError, ValueError) as e:
            print(f"[agent] could not persist phone audio volumes: {e}")

    async def restore_phone_audio(self):
        """Startup: apps a previous agent turned down get their volume
        back."""
        if not self.cfg.get("phone_audio_saved") or self.phone is None:
            return
        try:
            import phone_audio
            pa = phone_audio.PhoneAudio(
                on_chunk=lambda pcm: None,
                saved=self.cfg.get("phone_audio_saved"),
                persist=self._phone_audio_persist)
            await asyncio.to_thread(pa.restore_saved)
        except Exception as e:
            print(f"[agent] phone audio restore failed: {e}")

    async def _phone_idle_off(self):
        """Nobody left to see the phone screen: after a grace period (long
        enough for a Wi-Fi blip or an app restart to reconnect) move its apps
        back to the real screen and unplug it, so nothing stays stranded on a
        monitor no one can see."""
        await asyncio.sleep(PHONE_IDLE_OFF_S)
        if self.known_clients or not self.phone or not self.phone.enabled:
            return
        print("[agent] no phone connected — turning the phone screen off")
        await self._phone_audio_stop()
        await self._phone_change(self.phone.disable)

    # ---- unlock the PC with the user's password (session_unlock.py) ----

    # Wrong passwords: after UNLOCK_MAX_FAILS in a row, wait (doubling up
    # to 15 min) before the next try is even checked.
    UNLOCK_MAX_FAILS = 3
    UNLOCK_WAIT_S = 60

    async def h_session_status(self, msg):
        """{supported, locked}: is the PC's screen locked right now."""
        if platform.system() != "Linux":
            return {"supported": False, "locked": False}
        import session_unlock
        try:
            locked = await asyncio.to_thread(session_unlock.is_locked)
        except Exception:
            return {"supported": False, "locked": False}
        return {"supported": True, "locked": locked}

    async def h_session_unlock(self, msg):
        """session.unlock {password}: unlocks the PC's lock screen when the
        password is the PC user's (PAM). TLS connections only; the password
        is never logged or kept."""
        if platform.system() != "Linux":
            raise ValueError("Unlocking from the phone works on Linux PCs "
                             "for now")
        password = msg.pop("password", None)
        if not isinstance(password, str) or not password:
            raise ValueError("no password")
        if not self.embedded or msg.get("from") not in self.embedded.tls_clients:
            raise ValueError("Unlocking needs a secure connection: reconnect "
                             "to the PC once so the app switches to it")
        import session_unlock
        now = time.monotonic()
        if now < self._unlock_blocked_until:
            wait = int(self._unlock_blocked_until - now) + 1
            raise ValueError(f"Too many wrong passwords: try again in {wait} s")
        async with self._unlock_lock:
            ok = await asyncio.to_thread(session_unlock.check_password,
                                         password)
            del password
            if not ok:
                self._unlock_fails += 1
                print(f"[agent] unlock: wrong password "
                      f"({self._unlock_fails} in a row)")
                if self._unlock_fails >= self.UNLOCK_MAX_FAILS:
                    wait = min(900, self.UNLOCK_WAIT_S * 2 ** (
                        self._unlock_fails - self.UNLOCK_MAX_FAILS))
                    self._unlock_blocked_until = time.monotonic() + wait
                raise ValueError("Wrong password")
            self._unlock_fails = 0
            self._unlock_blocked_until = 0.0
            await asyncio.to_thread(session_unlock.unlock)
        print("[agent] unlock: the PC was unlocked from a phone")
        return {"locked": False}

    # ---- window lock (kdeapps.py _LOCK) ----

    WINDOW_LOCK_KEY = "window_lock"
    WINDOW_LOCK_GRACE_S = 20

    def _wlock_status(self):
        w = self._wlock
        return {"locked": w is not None, "title": w["title"] if w else ""}

    def _wlock_persist(self, value):
        if value:
            self.cfg[self.WINDOW_LOCK_KEY] = value
        else:
            self.cfg.pop(self.WINDOW_LOCK_KEY, None)
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                return
            if value:
                data[self.WINDOW_LOCK_KEY] = value
            elif self.WINDOW_LOCK_KEY in data:
                data.pop(self.WINDOW_LOCK_KEY)
            else:
                return
            save_config(data)
        except (OSError, ValueError) as e:
            print(f"[agent] could not persist the window lock: {e}")

    async def h_window_lock(self, msg):
        """window.lock {on, window?}: on keeps the PC's active window (or
        `window`, a wm.list id) above everything; windows that open
        meanwhile open underneath it without taking the keyboard. Every
        phone hears `window.lock` {locked, title}."""
        if self.kde_apps is None:
            raise ValueError("The window lock needs KDE Plasma on the PC "
                             "for now")
        on = bool(msg.get("on", True))
        async with self._wlock_lock:
            if on:
                if self._wlock is not None:
                    await self._wlock_release_locked()
                self.kde_apps.on_lock_ended = lambda: asyncio.ensure_future(
                    self.window_unlock("the locked window closed"))
                r = await self.kde_apps.lock_window(msg.get("window") or None)
                self._wlock = {"id": r["id"], "title": r.get("title") or "",
                               "prev_above": bool(r.get("prev_above"))}
                self._wlock_persist(self._wlock)
                print(f"[agent] window lock: on ({self._wlock['title'][:80]})")
            elif self._wlock is not None:
                await self._wlock_release_locked()
                print("[agent] window lock: off")
        status = self._wlock_status()
        asyncio.ensure_future(self._wlock_tell(status))
        return status

    async def h_window_lock_status(self, msg):
        return self._wlock_status()

    async def _wlock_tell(self, status):
        try:
            await self.send({"event": "window.lock", "data": status})
        except Exception:
            pass

    async def _wlock_release_locked(self):
        """With _wlock_lock held: undo the lock (script out, keepAbove back)."""
        w, self._wlock = self._wlock, None
        try:
            if w is not None and self.kde_apps is not None:
                await self.kde_apps.unlock_window(w["id"], w["prev_above"])
        finally:
            self._wlock_persist(None)

    async def window_unlock(self, why):
        async with self._wlock_lock:
            if self._wlock is None:
                return
            await self._wlock_release_locked()
        print(f"[agent] window lock: off ({why})")
        await self._wlock_tell(self._wlock_status())

    def _wlock_phones_changed(self):
        """A phone left: with none left, the lock goes after a grace
        period (a reconnect keeps it)."""
        if self._wlock is None or self.known_clients:
            return
        if self._wlock_grace is None or self._wlock_grace.done():
            self._wlock_grace = asyncio.create_task(self._wlock_grace_off())

    async def _wlock_grace_off(self):
        await asyncio.sleep(self.WINDOW_LOCK_GRACE_S)
        if not self.known_clients:
            await self.window_unlock("no phone connected")

    async def restore_window_lock(self):
        """Startup: a lock a previous agent left on (crash, kill) is undone."""
        saved = self.cfg.get(self.WINDOW_LOCK_KEY)
        if self.kde_apps is None:
            return
        await self.kde_apps.drop_lock_script()
        if isinstance(saved, dict) and saved.get("id"):
            try:
                await self.kde_apps.unlock_window(saved["id"],
                                                  bool(saved.get("prev_above")))
                print("[agent] window lock left on by the last run: released")
            except Exception as e:
                print(f"[agent] couldn't release the last run's window lock: {e}")
        if saved:
            self._wlock_persist(None)

    # ---- the PC's sound on the phone (see pc_audio.py) ----

    # How long the sound stays routed to a phone whose socket went away: a
    # network switch or a tunnel hiccup reconnects well within it.
    PC_AUDIO_GRACE_S = 20
    # The PC's default output from before it was switched to the phone,
    # kept in config while it is switched (see _pc_audio_persist).
    PC_AUDIO_KEY = "pc_audio_prev_sink"

    async def h_audio_start(self, msg):
        """audio.start {mute_pc?, bitrate?}: everything the PC plays comes
        out of THIS phone, as Opus in OPS1 packets, on the LAN or through
        the tunnel alike. With mute_pc (the default) the PC's speakers go
        quiet. One phone at a time: another phone's audio.start takes over,
        and the one before hears audio.stopped. A phone back within the
        grace period just carries on."""
        import pc_audio
        client = msg.get("from")
        mute_pc = bool(msg.get("mute_pc", True))
        bitrate = pc_audio.clamp_bitrate(
            msg.get("bitrate") or pc_audio.DEFAULT_BITRATE)
        grace, self._pc_audio_grace = self._pc_audio_grace, None
        if grace is not None:
            grace.cancel()
        started = False
        try:
            before = await self._pc_audio_start(client, mute_pc, bitrate)
            started = True
        finally:
            # Failed, or cancelled (drop_client cancels the lane mid-start,
            # and the start thread still finishes the switch): the grace
            # cancelled above is gone, so arm it again unless another phone
            # is still the listener, or the PC could stay muted for good.
            # _pc_audio_grace_off only stops a running capture nobody owns.
            if not started and self._pc_audio_client is None:
                self._pc_audio_left()
        if before not in (None, client) and before in self.known_clients:
            # Never waited on: the phone being replaced may be gone with a
            # backed-up socket, and a send to it would block for minutes.
            asyncio.ensure_future(self._pc_audio_tell_stopped(
                before, "another phone took over"))
        await self._pc_audio_resume()
        return {"enabled": True, "codec": "opus", "rate": pc_audio.RATE,
                "channels": pc_audio.CHANNELS, "frame_ms": pc_audio.FRAME_MS,
                "mute_pc": mute_pc, "bitrate": bitrate}

    async def _pc_audio_start(self, client, mute_pc, bitrate):
        """h_audio_start's switch, under the lock. Returns the listener from
        before."""
        import pc_audio
        async with self._pc_audio_lock:
            if self.pc_audio is None:
                loop = asyncio.get_running_loop()
                self.pc_audio = pc_audio.PcAudio(
                    on_packet=lambda seq, data: loop.call_soon_threadsafe(
                        self._pc_audio_send, seq, data, time.monotonic()),
                    persist=self._pc_audio_persist,
                    on_ended=lambda reason: loop.call_soon_threadsafe(
                        self._pc_audio_ended, reason))
            if self.cfg.get(self.PC_AUDIO_KEY) and not self.pc_audio.running:
                # A previous agent died with the sound on a phone and the
                # startup restore hasn't run yet: undo that first, or its
                # leftover default would be saved as "the PC's output".
                await self._pc_audio_restore_saved()
            before = self._pc_audio_client
            try:
                await asyncio.to_thread(self.pc_audio.start, mute_pc, bitrate)
            except Exception:
                if not self.pc_audio.running:
                    self._pc_audio_client = None
                raise
            self._pc_audio_client = client
            self._pc_audio_alive_at = time.monotonic()
            if self._pc_audio_watch is None or self._pc_audio_watch.done():
                self._pc_audio_watch = asyncio.create_task(
                    self._pc_audio_watchdog())
            return before

    async def h_audio_stop(self, msg):
        """audio.stop: the PC's sound back on the PC. From a phone that
        isn't the listener while the listener is still connected it changes
        nothing: that phone was taken over and must not cut the other one
        off. With the listener gone (in its grace period, or back under a
        new connection) any phone may stop it."""
        client = msg.get("from")
        reason = str(msg.get("reason") or "").strip()
        if reason:
            print(f"[agent] the phone asked to stop its sound: {reason[:200]}")
        listener = self._pc_audio_client
        if listener not in (None, client) and listener in self.known_clients:
            return {"enabled": False}
        grace, self._pc_audio_grace = self._pc_audio_grace, None
        if grace is not None:
            grace.cancel()
        if reason not in self.PC_AUDIO_USER_STOPS and self.pc_audio is not None \
                and self.pc_audio.running:
            # The phone stopped by itself (its headphones went, another app
            # took its audio, a start failed): the sound is about to come
            # back out of the PC's speakers, which nobody asked for.
            await self._pc_audio_pause(
                f"the phone stopped ({reason or 'no reason'})")
        self._pc_audio_paused = []
        async with self._pc_audio_lock:
            self._pc_audio_client = None
            if self.pc_audio is not None and self.pc_audio.running:
                await asyncio.to_thread(self.pc_audio.stop)
        return {"enabled": False}

    # audio.stop reasons that are the user's own choice: the sound simply
    # goes back to the PC. Any other stop pauses the PC's players first.
    PC_AUDIO_USER_STOPS = ("switched off", "left the dashboard")

    async def _pc_audio_pause(self, why):
        """Pause the PC's media players (MPRIS) because the phone playing
        its sound is gone: unheard, or about to come out of the speakers.
        The ones paused are kept in _pc_audio_paused for _pc_audio_resume."""
        import pc_audio
        paused = await asyncio.to_thread(pc_audio.pause_players)
        for name in paused:
            if name not in self._pc_audio_paused:
                self._pc_audio_paused.append(name)
        if paused:
            print(f"[agent] PC sound: {why} — paused {', '.join(paused)}")

    async def _pc_audio_resume(self):
        """The listener is back (within the grace period): play again what
        _pc_audio_pause paused."""
        import pc_audio
        pausing, self._pc_audio_pausing = self._pc_audio_pausing, None
        if pausing is not None:
            try:
                await pausing   # a pause still in flight lands first
            except Exception:
                pass
        names, self._pc_audio_paused = self._pc_audio_paused, []
        if names:
            await asyncio.to_thread(pc_audio.resume_players, names)
            print(f"[agent] PC sound: the phone is back — playing "
                  f"{', '.join(names)} again")

    # A listener that sends heartbeats (audio.alive, every 5 s) and then
    # says nothing for this long is gone, even if its socket looks open: a
    # phone that walked off the Wi-Fi leaves no FIN, and a screen stream's
    # backlog can hold off the keepalive ping that would notice. Its
    # connection is dropped, so the grace period starts.
    PC_AUDIO_SILENT_S = 30

    async def h_audio_alive(self, msg):
        """audio.alive (no reply): the listening phone's heartbeat. Newer
        apps add `stats`, what the phone's player is doing; logged every
        ~10 s (journalctl), since a phone on a hotspot has no reachable
        log of its own."""
        self._pc_audio_heartbeat.add(msg.get("from"))
        stats = msg.get("stats")
        now = time.monotonic()
        if isinstance(stats, dict) and now - self._pc_audio_stats_at >= 9.5:
            self._pc_audio_stats_at = now
            line = ", ".join(f"{k}={v}" for k, v in stats.items())
            # With the PC's side since the last line: dropped packets are
            # holes the phone can only rebuffer over, and a long loop lag
            # is jitter the phone's cushion has to absorb.
            pc = (f"pc_sent={self._pc_audio_sent}, "
                  f"pc_dropped={self._pc_audio_dropped}, "
                  f"pc_loop_lag_max_ms={self._pc_audio_lag * 1000:.0f}")
            self._pc_audio_sent = self._pc_audio_dropped = 0
            self._pc_audio_lag = 0.0
            print(f"[agent] phone sound: {line[:900]} | {pc}")
        return {}

    async def _pc_audio_watchdog(self):
        while self.pc_audio is not None and self.pc_audio.running:
            await asyncio.sleep(5)
            client = self._pc_audio_client
            if client is None or client not in self._pc_audio_heartbeat:
                continue
            if time.monotonic() - self._pc_audio_alive_at \
                    < self.PC_AUDIO_SILENT_S:
                continue
            print("[agent] the phone playing the PC's sound stopped "
                  "answering — dropping its connection")
            ws = self.embedded.clients.get(client) if self.embedded else None
            if ws is not None:
                ws.transport.abort()  # ends its handler: drop_client
            else:
                self._pc_audio_left()

    async def h_audio_status(self, msg):
        """For an app that reconnected: is the sound still on a phone, and
        is that phone this connection (`mine`)?"""
        on = self.pc_audio is not None and self.pc_audio.running
        return {"enabled": on, "mute_pc": on and self.pc_audio.mute_pc,
                "mine": on and self._pc_audio_client == msg.get("from")}

    # Audio backlog allowed per phone, under websockets' 32 KiB write limit:
    # a paused transport would block the keepalive ping, and a phone that
    # vanished (walked off the Wi-Fi) would go unnoticed until TCP gives
    # up, ~15 min, with the PC muted all along. 16 KB is ~1 s of sound.
    PC_AUDIO_BACKLOG_BYTES = 16 * 1024

    def _pc_audio_send(self, seq, data, made_at=None):
        """Loop thread: one 20 ms Opus packet to the listening phone.
        Dropped when its link is backed up: late sound is worse than a gap,
        and the gap in `seq` tells the phone. made_at: when the reader
        thread encoded it (time.monotonic), for the loop-lag stat."""
        client = self._pc_audio_client
        if client is None:
            return
        if made_at is not None:
            self._pc_audio_lag = max(self._pc_audio_lag,
                                     time.monotonic() - made_at)
        if self.embedded:
            try:
                backed_up = self.embedded.write_buffer_size(client) \
                    > self.PC_AUDIO_BACKLOG_BYTES
            except Exception:
                backed_up = True
        else:
            backed_up = self.congested(client)
        if backed_up:
            self._pc_audio_dropped += 1
            return
        self._pc_audio_sent += 1
        header = b'{"seq":%d,"ms":20}' % seq
        packet = b"OPS1" + len(header).to_bytes(4, "little") + header + data
        asyncio.ensure_future(self.send_bytes_to(client, packet))

    async def _pc_audio_tell_stopped(self, client, reason):
        try:
            await asyncio.wait_for(self.send_to(client, {
                "event": "audio.stopped", "data": {"reason": reason}}), 5)
        except Exception:
            pass  # that phone is gone already, or its link is backed up

    def _pc_audio_ended(self, reason):
        """Loop thread: the capture died on its own (the sound server
        restarted...). pc_audio already put the PC's output back; the
        listener turns its switch off."""
        client, self._pc_audio_client = self._pc_audio_client, None
        if client is not None:
            asyncio.ensure_future(self._pc_audio_tell_stopped(client, reason))
        # The speakers have the sound again: don't let it blare by itself.
        self._pc_audio_paused = []
        asyncio.ensure_future(self._pc_audio_pause("the capture stopped"))

    def _pc_audio_left(self):
        """The listening phone's socket is gone: no more packets, but the
        routing and capture stay for PC_AUDIO_GRACE_S, so a phone that
        reconnects and sends audio.start again carries on without the PC's
        speakers blaring in between."""
        self._pc_audio_client = None
        if self.pc_audio is not None and self.pc_audio.running and (
                self._pc_audio_pausing is None
                or self._pc_audio_pausing.done()):
            # Pause at once: nobody hears the PC any more. Played again if
            # the phone is back within the grace period.
            self._pc_audio_pausing = asyncio.create_task(
                self._pc_audio_pause("lost the phone"))
        if self._pc_audio_grace is None or self._pc_audio_grace.done():
            self._pc_audio_grace = asyncio.create_task(
                self._pc_audio_grace_off())

    async def _pc_audio_grace_off(self):
        await asyncio.sleep(self.PC_AUDIO_GRACE_S)
        async with self._pc_audio_lock:
            if self._pc_audio_client is None and self.pc_audio is not None \
                    and self.pc_audio.running:
                print("[agent] the phone playing the PC's sound didn't come "
                      "back — sound back on the PC")
                # The players it paused stay paused.
                self._pc_audio_paused = []
                await asyncio.to_thread(self.pc_audio.stop)

    def _pc_audio_persist(self, prev):
        """The PC's default output from before it was switched to the
        phone, written BEFORE the switch and cleared once it is back.
        PipeWire remembers a picked default for good, so an agent that dies
        meanwhile must leave a note for the next one (restore_pc_audio).
        Same rule as _power_persist: only this key is written over what is
        on disk."""
        key = self.PC_AUDIO_KEY
        if prev:
            self.cfg[key] = prev
        else:
            self.cfg.pop(key, None)
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("config.json is not a JSON object")
            if prev:
                data[key] = prev
            elif key in data:
                data.pop(key)
            else:
                return
            save_config(data)
        except (OSError, ValueError) as e:
            print(f"[agent] could not persist the PC's default output: {e}")

    async def _pc_audio_restore_saved(self):
        """With _pc_audio_lock held: put back the default output a previous
        agent left on the phone's sink, then forget it. Raises when the
        sound server can't be reached."""
        import pc_audio
        done = await asyncio.to_thread(pc_audio.restore_after_crash,
                                       self.cfg[self.PC_AUDIO_KEY])
        print(f"[agent] PC sound left on a phone by the last run: {done}")
        self._pc_audio_persist(None)

    async def restore_pc_audio(self):
        """Startup: an agent that died while the PC's sound played on a
        phone (crash, kill, `systemctl stop`: none of them reach run()'s
        finally) left the phone's sink as the PC's picked default output.
        Put the saved one back. Retried for a minute: at login the agent can
        be up before the sound server."""
        error = None
        for _ in range(12):
            async with self._pc_audio_lock:
                if not self.cfg.get(self.PC_AUDIO_KEY) or (
                        self.pc_audio is not None and self.pc_audio.running):
                    return  # nothing to do, or an audio.start did it
                try:
                    await self._pc_audio_restore_saved()
                    return
                except Exception as e:
                    error = e
            await asyncio.sleep(5)
        print(f"[agent] could not put the PC's default output back: {error}")

    async def h_notify_subscribe(self, msg):
        raise ValueError(
            "notification mirroring is not implemented yet "
            "(Windows toast capture needs the winrt bridge - see README roadmap)"
        )

    HANDLERS = {
        "ping": h_ping,
        "sys.info": h_sys_info,
        "sys.exec": h_sys_exec,
        "sys.ps": h_sys_ps,
        "sys.kill": h_sys_kill,
        "fs.list": h_fs_list,
        "fs.shortcuts": h_fs_shortcuts,
        "fs.download": h_fs_download,
        "fs.upload": h_fs_upload,
        "fs.delete": h_fs_delete,
        "fs.mkdir": h_fs_mkdir,
        "input": h_input,
        "power": h_power,
        "power.schedule": h_power_schedule,
        "power.cancel": h_power_cancel,
        "power.status": h_power_status,
        "display.brightness.get": h_display_brightness_get,
        "display.brightness.set": h_display_brightness_set,
        "media": h_media,
        "media.info": h_media_info,
        "media.seek": h_media_seek,
        "clipboard.get": h_clipboard_get,
        "clipboard.set": h_clipboard_set,
        "clipboard.watch": h_clipboard_watch,
        "screen.start": h_screen_start,
        "screen.stop": h_screen_stop,
        "screen.ack": h_screen_ack,
        "remote.info": h_remote_info,
        "camera.start": h_camera_start,
        "camera.stop": h_camera_stop,
        "wm.list": h_wm_list,
        "wm.thumb": h_wm_thumb,
        "wm.activate": h_wm_activate,
        "wm.close": h_wm_close,
        "wm.minimize": h_wm_minimize,
        "wm.maximize": h_wm_maximize,
        "wm.restore": h_wm_restore,
        "apps.list": h_apps_list,
        "apps.icon": h_apps_icon,
        "apps.launch": h_apps_launch,
        "phone.status": h_phone_status,
        "phone.setup": h_phone_setup,
        "phone.enable": h_phone_enable,
        "phone.disable": h_phone_disable,
        "phone.settings": h_phone_settings,
        "phone.move": h_phone_move,
        "phone.return": h_phone_return,
        "phone.touch": h_phone_touch,
        "phone.text": h_phone_text,
        "phone.key": h_phone_key,
        "phone.wheel": h_phone_wheel,
        "phone.audio": h_phone_audio,
        "audio.start": h_audio_start,
        "audio.stop": h_audio_stop,
        "audio.status": h_audio_status,
        "session.status": h_session_status,
        "session.unlock": h_session_unlock,
        "window.lock": h_window_lock,
        "window.lock.status": h_window_lock_status,
        "audio.alive": h_audio_alive,
        "notify.subscribe": h_notify_subscribe,
    }

    # ---- scheduled power actions ----
    # Everything that changes power_sched runs under _power_lock, so a
    # schedule, a cancel and the timer firing can never interleave.

    def power_status(self):
        s = self.power_sched
        if not s:
            return {"scheduled": False}
        return {"scheduled": True, "action": s["action"], "at": s["at"],
                "remaining_s": max(0, round(s["at"] - time.time()))}

    async def _power_broadcast(self, status, **extra):
        """Every phone hears about every change, so two phones showing the
        same PC always show the same countdown. Best effort: behind a
        remote relay a socket that just dropped raises here, and that must
        never stop a change half-way — least of all the action that is
        about to fire at its deadline."""
        try:
            await self.send({"event": "power.schedule",
                             "data": {**status, **extra}})
        except Exception as e:
            print(f"[agent] power.schedule broadcast failed: {e}")

    def _power_persist(self):
        """Mirrors power_sched into the config file. Best effort: a failed
        write only costs surviving an agent restart. Only this one key is
        written over what is on disk, never self.cfg as a whole: the GUI
        agent runs with require_approval switched off in memory, and the
        service exe shares config.json — saving that would silently turn
        off its Allow/Deny prompt for good."""
        if self.power_sched:
            self.cfg["power_schedule"] = dict(self.power_sched)
        else:
            self.cfg.pop("power_schedule", None)
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if not isinstance(saved, dict):
                raise ValueError("config.json is not a JSON object")
            if self.power_sched:
                saved["power_schedule"] = dict(self.power_sched)
            else:
                saved.pop("power_schedule", None)
            save_config(saved)
        except (OSError, ValueError) as e:
            print(f"[agent] could not persist the power schedule: {e}")

    def _phone_persist(self, settings):
        """Phone-screen settings (size, orientation, scale, return_focus)
        survive restarts. Same rule as _power_persist: only this key is
        written over what is on disk, never self.cfg as a whole."""
        self.cfg["phone_monitor"] = dict(settings)
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if not isinstance(saved, dict):
                raise ValueError("config.json is not a JSON object")
            saved["phone_monitor"] = dict(settings)
            save_config(saved)
        except (OSError, ValueError) as e:
            print(f"[agent] could not persist phone screen settings: {e}")

    def _power_stop_timer(self):
        task, self._power_task = self._power_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
        if self.approval is not None:
            self.approval.close_warning()

    async def _power_disarm(self, abort_windows):
        """Drops the current schedule. With abort_windows, Windows' own
        timer is stopped first (shutdown /a) — even when the agent knows of
        nothing pending: Cancel on the phone means "make sure nothing shuts
        this PC down", including a timer the agent lost track of. If the
        abort fails the schedule is kept, so power.status stays truthful."""
        if abort_windows and platform.system() == "Windows":
            await asyncio.to_thread(win_timer_abort)
        self._power_stop_timer()
        if self.power_sched is not None:
            self.power_sched = None
            self._power_persist()

    async def _power_arm(self, action, at, restoring=False):
        """Replaces any current schedule with `action` at `at` (unix s).
        restoring: re-arming a persisted schedule at startup. Then Windows'
        timer is armed without an abort first: if the agent merely
        restarted, the old timer is still pending and Windows answers
        "already scheduled" (fine, same deadline); after a reboot it was
        lost and this arms it again."""
        win_timed = platform.system() == "Windows" \
            and action in WIN_TIMED_ACTIONS
        if not restoring:
            prev = self.power_sched
            prev_timed = bool(prev) and prev["action"] in WIN_TIMED_ACTIONS
            # Arming fails while any shutdown timer is pending, so clear the
            # slot first — also when the new action replaces an old
            # shutdown with, say, a sleep.
            await self._power_disarm(abort_windows=win_timed or prev_timed)
        if win_timed:
            code, out = await asyncio.to_thread(
                win_timer_arm, action, round(at - time.time()))
            if code != 0 and not (
                    restoring and code == ERROR_SHUTDOWN_IS_SCHEDULED):
                raise RuntimeError(
                    f"Windows refused to schedule the {action} ({code}): {out}")
        self.power_sched = {"action": action, "at": at}
        self._power_persist()
        self._power_task = asyncio.create_task(self._power_timer(action, at))

    async def _power_timer(self, action, at):
        """The agent-side clock. Re-reads the wall clock at least every
        POWER_TICK_S (see there) and wakes exactly at T-60s for the PC-side
        warning and at T."""
        warned = False
        while True:
            left = at - time.time()
            if left <= 0:
                break
            if not warned and left <= POWER_WARN_S:
                warned = True
                self._power_warn(action, at)
            nxt = left - POWER_WARN_S if not warned else left
            await asyncio.sleep(min(nxt, POWER_TICK_S))
        await self._power_fire(action, at)

    async def _power_fire(self, action, at):
        async with self._power_lock:
            if not self.power_sched or self.power_sched["at"] != at:
                return  # replaced or cancelled while waiting for the lock
            self._power_stop_timer()
            self.power_sched = None
            self._power_persist()
            late = time.time() - at
            if late > POWER_LATE_S:
                print(f"[agent] scheduled {action} missed by {round(late)}s "
                      "(the PC was asleep?) — dropped")
                if platform.system() == "Windows" \
                        and action in WIN_TIMED_ACTIONS:
                    try:
                        await asyncio.to_thread(win_timer_abort)
                    except Exception as e:
                        print(f"[agent] {e}")
                await self._power_broadcast(self.power_status(), missed=action)
                return
            # Phones first: after a sleep or shutdown the sockets are gone.
            await self._power_broadcast(self.power_status(), fired=action)
        print(f"[agent] scheduled {action} is due — running it")
        try:
            power(action)
        except Exception as e:
            print(f"[agent] scheduled {action} failed: {e}")

    def _power_warn(self, action, at):
        """T-60s: an always-on-top notice on the PC itself, so whoever sits
        there can stop a shutdown scheduled from a phone. It borrows the
        approval dialog's Tk thread, which only exists in standalone
        (embedded relay) mode; behind a remote relay the phone and
        Windows' own notice are the only warnings."""
        if self.approval is None:
            return
        # Dry-run still shows the notice (harmless) but never leaves it up.
        fut = self.approval.warn(POWER_VERBS[action], at,
                                 auto_close_s=5 if power_dry_run() else None)

        def on_done(f):
            if not f.cancelled() and f.result():
                asyncio.get_running_loop().create_task(
                    self._power_cancel_from_pc(at))
        fut.add_done_callback(on_done)

    async def _power_cancel_from_pc(self, at):
        """Cancel pressed in the T-60s notice: same as power.cancel."""
        async with self._power_lock:
            if not self.power_sched or self.power_sched["at"] != at:
                return  # already replaced, cancelled or fired
            try:
                await self._power_disarm(abort_windows=True)
            except Exception as e:
                print(f"[agent] cancel from the PC failed: {e}")
                return
            print("[agent] scheduled power action cancelled on the PC")
            await self._power_broadcast(self.power_status(),
                                        cancelled_by="pc")

    async def restore_power_schedule(self):
        """Startup: brings back a schedule persisted by an earlier run, so
        power.status survives an agent restart (and a reboot). The agent
        timer is re-armed for every action; Windows' own timer for
        shutdown/restart too (see _power_arm). Deadlines already in the
        past are dropped — the action either happened, or the PC was off
        through it."""
        saved = self.cfg.get("power_schedule")
        if not saved:
            return
        async with self._power_lock:
            if self.power_sched is not None:
                return  # a phone already scheduled something newer
            try:
                action, at = str(saved["action"]), float(saved["at"])
                if action not in POWER_SCHEDULE_ACTIONS:
                    raise ValueError(action)
                power_cmd(action)
            except (KeyError, TypeError, ValueError) as e:
                print(f"[agent] dropping unreadable power schedule: {e}")
                self._power_persist()
                return
            left = at - time.time()
            if left < 1 or left > POWER_SCHEDULE_MAX_S + 60:
                print(f"[agent] dropping scheduled {action}: deadline "
                      f"{'passed' if left < 1 else 'out of range'}")
                self._power_persist()
                return
            try:
                await self._power_arm(action, at, restoring=True)
            except Exception as e:
                print(f"[agent] could not restore scheduled {action}: {e}")
                self._power_persist()
                return
            print(f"[agent] restored scheduled {action} in {round(left)}s")

    # ---- background tasks ----

    async def discovery_beacon(self):
        """Broadcast 'PCOCKET|<name>|<relay port>' over UDP every few seconds
        so the phone app can find this PC on the local network — no QR, no
        typing IP addresses. Outbound broadcast, so inbound firewall rules
        don't matter."""
        try:
            loop = asyncio.get_running_loop()
            transport, _ = await loop.create_datagram_endpoint(
                asyncio.DatagramProtocol, local_addr=("0.0.0.0", 0))
            transport.get_extra_info("socket").setsockopt(
                socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except Exception as e:
            print(f"[agent] discovery beacon disabled: {e}")
            return
        port = urlparse(self.cfg["relay"]).port or 8080
        msg = f"SUDO|{self.cfg['name']}|{port}".encode()
        while True:
            transport.sendto(msg, ("255.255.255.255", DISCOVERY_PORT))
            await asyncio.sleep(1)  # fast rediscovery after network switches

    async def media_watcher(self):
        """Linux: pushes `media.changed` (the full media.info) to every
        phone the moment the player changes track, plays, pauses or seeks,
        or the volume changes, so the phones' Now Playing cards follow the
        PC live instead of waiting for their next poll. Two followers:
        `playerctl -F` (MPRIS) and `pactl subscribe` (sink changes), their
        bursts coalesced into one push per 150 ms."""
        if platform.system() == "Windows" or not shutil.which("playerctl"):
            return
        wake = asyncio.Event()

        async def follow(cmd, wanted):
            while True:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL)
                    async for line in proc.stdout:
                        if wanted(line):
                            wake.set()
                    await proc.wait()
                except (OSError, ValueError):
                    pass
                await asyncio.sleep(2)   # player gone / sound server restart

        fmt = ("{{playerName}}\t{{status}}\t{{title}}\t{{artist}}\t"
               "{{mpris:length}}\t{{mpris:artUrl}}")
        tasks = [asyncio.create_task(follow(
            ["playerctl", "-F", "metadata", "--format", fmt],
            lambda line: True))]
        if shutil.which("pactl"):
            tasks.append(asyncio.create_task(follow(
                ["pactl", "subscribe"],
                lambda line: b"on sink #" in line or b"on server" in line)))
        # playerctl -F doesn't report a seek: catch it by comparing the
        # position against where it should be, every few seconds.
        last = None
        last_at = 0.0
        try:
            while True:
                try:
                    await asyncio.wait_for(wake.wait(), 3)
                except asyncio.TimeoutError:
                    pass
                woke = wake.is_set()
                await asyncio.sleep(0.15)
                wake.clear()
                if not self.known_clients:
                    last = None
                    continue
                info = await asyncio.to_thread(_media_info)
                now = time.monotonic()
                if not woke and last is not None:
                    expected = last.get("position_s", 0) + (
                        now - last_at if last.get("playing") else 0)
                    same = {k: v for k, v in info.items()
                            if k != "position_s"} == \
                        {k: v for k, v in last.items() if k != "position_s"}
                    if same and abs(info.get("position_s", 0) - expected) < 2:
                        continue
                last, last_at = info, now
                try:
                    await self.send({"event": "media.changed", "data": info})
                except Exception:
                    pass
        finally:
            for t in tasks:
                t.cancel()

    async def cursor_watcher(self):
        """Poll the cursor position (~16x/s) and wake screen streamers when
        it moves — so physical mouse movement revives the stream just as
        fast as phone input does. GetCursorPos is effectively free."""
        last = mouse.position
        sweep_tick = 0
        while True:
            await asyncio.sleep(0.06)
            try:
                pos = mouse.position
            except Exception:
                continue
            if pos != last:
                last = pos
                for client, s in list(self.streamers.items()):
                    s.wake()
                    # The PC's own mouse (or another phone) moved it.
                    if s.client_cursor:
                        self.push_cursor(client)
            # Keep the PC user's mouse off the phone screen they can't see
            # (a no-op unless it is plugged in).
            if self.phone is not None and self.phone.enabled:
                try:
                    self.phone.fence_cursor()
                    # The PC user opened an app that lives on the phone
                    # screen: bring it back to them (phone_monitor.py).
                    hwnd = self.phone.check_reclaim()
                    if hwnd:
                        await self._phone_change(self.phone.reclaim, hwnd)
                    # A click on the taskbar button of a phone-screen app
                    # that happens to be active minimizes it: put it back on
                    # the phone and give the PC its own window instead.
                    for hwnd in self.phone.check_minimized():
                        await self._phone_change(
                            self.phone.restore_on_phone, hwnd)
                    # ~1/s: windows the PC user opened that landed on the
                    # phone screen go to the PC screen instead.
                    sweep_tick = (sweep_tick + 1) % 16
                    if sweep_tick == 0:
                        async with self._phone_lock:
                            moved = await asyncio.to_thread(self.phone.sweep)
                        if moved:
                            await self._phone_broadcast(
                                await asyncio.to_thread(self.phone.status))
                        # Phone sound follows the apps on the phone screen
                        # (and catches sessions an app opens when it starts
                        # playing).
                        if self.phone_audio is not None \
                                and self.phone_audio.running:
                            await asyncio.to_thread(
                                self.phone_audio.set_targets,
                                self.phone.window_pids())
                except Exception:
                    pass

    async def clipboard_watcher(self):
        while True:
            await asyncio.sleep(1.5)
            if not self._clipboard_watch or not self.known_clients:
                continue
            try:
                text = await asyncio.to_thread(pyperclip.paste)
            except Exception:
                continue
            if text and text != self._clipboard_last:
                self._clipboard_last = text
                await self.send({"event": "clipboard.changed", "data": {"text": text}})

    async def drop_client(self, client_id):
        self._input_seq.pop(client_id, None)
        self._pc_audio_heartbeat.discard(client_id)
        lane = self._lanes.pop(client_id, None)
        if lane:
            lane[1].cancel()
        if client_id in self.known_clients:
            self.known_clients.discard(client_id)
            self.client_monitors.pop(client_id, None)
            streamer = self.streamers.pop(client_id, None)
            if streamer:
                await streamer.stop()
            video = self.video_streamers.pop(client_id, None)
            if video:
                await video.stop()
            cam = self.camera_streamers.pop(client_id, None)
            if cam:
                await cam.stop()
            if client_id == self._phone_audio_client:
                await self._phone_audio_stop()
            if client_id == self._pc_audio_client:
                self._pc_audio_left()
            self._wlock_phones_changed()
            if self.portal is not None and not self.known_clients and (
                    self._portal_idle_task is None
                    or self._portal_idle_task.done()):
                self._portal_idle_task = asyncio.create_task(
                    self._portal_idle_off())
            if self.phone is not None and self.phone.enabled:
                # A finger that was down can't be lifted over a dead socket.
                self.phone.release_touches()
                if not self.known_clients and (
                        self._phone_idle_task is None
                        or self._phone_idle_task.done()):
                    self._phone_idle_task = asyncio.create_task(
                        self._phone_idle_off())

    PORTAL_IDLE_OFF_S = 60

    async def _portal_idle_off(self):
        """No phone left: stop the portal session after a grace minute, so
        the desktop's "screen is being shared" indicator goes away. The next
        screen.start or input starts it again (no dialog: restore token)."""
        await asyncio.sleep(self.PORTAL_IDLE_OFF_S)
        if not self.known_clients and self.portal is not None:
            await self.portal.close()

    # ---- main loop ----

    async def run(self):
        self.loop = asyncio.get_running_loop()
        asyncio.create_task(self.discovery_beacon())
        asyncio.create_task(self.cursor_watcher())
        asyncio.create_task(self.media_watcher())
        asyncio.create_task(self.restore_power_schedule())
        asyncio.create_task(self.restore_phone_audio())
        asyncio.create_task(self.restore_pc_audio())
        asyncio.create_task(self.restore_window_lock())
        host = urlparse(self.cfg["relay"]).hostname
        if host in (None, "127.0.0.1", "localhost", "::1"):
            # Standalone: the relay is embedded — the phone connects straight
            # to this process. One exe is the whole PC side.
            from approval import ApprovalManager
            self.approval = ApprovalManager()
            self.approval.start(self.loop)
            port = urlparse(self.cfg["relay"]).port or 8080
            remote = self._setup_remote()
            self.embedded = EmbeddedRelay(
                self, port, *(remote if remote else (None, None)))
            try:
                await self.embedded.serve()
            finally:
                if self.tunnel is not None:
                    self.tunnel.stop()
                if self.pc_audio is not None:
                    self.pc_audio.stop()  # the PC's sound back on the PC
                try:
                    await asyncio.wait_for(
                        self.window_unlock("the agent stopped"), 3)
                except BaseException:
                    pass  # the next start releases it (restore_window_lock)
                if self.remote is not None:
                    self.remote.stop()
            return
        await self._run_outbound()

    async def _run_outbound(self):
        backoff = 1
        while True:
            try:
                async with websockets.connect(
                    self.cfg["relay"], ping_interval=20, ping_timeout=20,
                    max_size=8 * 1024 * 1024,
                    compression=None,  # frames don't compress (see serve)
                ) as ws:
                    self.ws = ws
                    await self.send({
                        "type": "register",
                        "code": self.cfg["code"],
                        "deviceId": self.cfg["device_id"],
                        "name": self.cfg["name"],
                    })
                    print(f"[agent] connected to {self.cfg['relay']} "
                          f"as '{self.cfg['name']}' code={self.cfg['code']}")
                    self._status("Connected — waiting for phone")
                    backoff = 1
                    watcher = asyncio.create_task(self.clipboard_watcher())
                    try:
                        async for raw in ws:
                            await self.on_message(raw)
                    finally:
                        watcher.cancel()
                        for c in set(self.streamers) | set(self._lanes):
                            await self.drop_client(c)
                        self.known_clients.clear()
                        self.ws = None
            except (OSError, websockets.WebSocketException) as e:
                print(f"[agent] connection failed: {e}; retry in {backoff}s")
                self._status("Relay unreachable — retrying…")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def on_message(self, raw):
        # Binary packets are reserved for screen frames; none are expected
        # from clients anymore, so just drop them.
        if isinstance(raw, bytes):
            return
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if msg.get("type") in ("registered", "error"):
            if msg.get("type") == "error":
                print(f"[agent] relay error: {msg.get('error')}")
            return
        if msg.get("type") == "client.gone":
            await self.drop_client(msg.get("clientId"))
            return
        client = msg.get("from")
        if client is not None and client == self._pc_audio_client:
            self._pc_audio_alive_at = time.monotonic()
        if client and client not in self.known_clients:
            self.known_clients.add(client)
            self._status("Phone connected")
        cmd = msg.get("cmd")
        handler = self.HANDLERS.get(cmd) if cmd else None
        if not handler:
            if msg.get("id"):
                await self.reply(msg, ok=False, error=f"unknown cmd: {cmd}")
            return
        if cmd in self.FAST_CMDS:
            await self._dispatch(handler, msg)
            return
        lane = self._lanes.get(client)
        if lane is None or lane[1].done():
            queue = asyncio.Queue()
            lane = self._lanes[client] = (
                queue, asyncio.create_task(self._lane_worker(queue)))
        lane[0].put_nowait((handler, msg))

    # Pointer, touch and key input, handled the moment it arrives. Every
    # other command goes to its phone's "lane": a queue worked in order by
    # one task, so requests keep their order among themselves but never
    # hold up input. Handled inline, one Now Playing poll (a ~1-3 s
    # PowerShell run, every 3 s) or one app-dock window list (up to ~3 s
    # with icons) queued all cursor moves behind it — seconds of lag.
    FAST_CMDS = frozenset({"ping", "input", "media", "phone.touch",
                           "phone.text", "phone.key", "phone.wheel",
                           # opening the Screen page must not wait behind a
                           # slow request, and acks pace the stream
                           "screen.start", "screen.stop", "screen.ack",
                           # never queued behind a slow request
                           "audio.alive"})

    async def _lane_worker(self, queue):
        while True:
            handler, msg = await queue.get()
            await self._dispatch(handler, msg)

    async def _dispatch(self, handler, msg):
        try:
            try:
                data = await handler(self, msg)
                if msg.get("id"):
                    await self.reply(msg, ok=True, data=data)
            except Exception as e:
                if msg.get("id"):
                    await self.reply(msg, ok=False, error=e)
                else:
                    print(f"[agent] error in {msg.get('cmd')}: {e}")
        except websockets.ConnectionClosed:
            pass  # the phone left before its answer: drop_client cleans up
        except Exception as e:
            # A failed reply must not kill a lane worker (its phone's
            # requests would never be answered again).
            print(f"[agent] error answering {msg.get('cmd')}: {e}")


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no traffic is actually sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def pairing_payload(cfg):
    """The pcocket:// URI encoded in the pairing QR."""
    relay = cfg["relay"]
    u = urlparse(relay)
    lan = None
    if u.hostname in ("127.0.0.1", "localhost", "::1"):
        # a phone can't reach loopback — advertise the LAN address instead
        relay = f"{u.scheme or 'ws'}://{lan_ip()}:{u.port or 8080}"
    else:
        # remote relay (tunnel/hosted): also offer the direct LAN address —
        # the app prefers it whenever phone and PC share a network, skipping
        # the internet round-trip that makes screen sharing laggy
        lan = f"ws://{lan_ip()}:8080"
    payload = f"sudo://pair?relay={quote(relay, safe='')}&code={cfg['code']}"
    if lan:
        payload += f"&lan={quote(lan, safe='')}"
    return payload


def show_pairing_qr(cfg):
    """Grandma-proof pairing: pop a QR the phone app can scan."""
    try:
        import qrcode
    except ImportError:
        print("[agent] install the 'qrcode' package for QR pairing")
        return
    payload = pairing_payload(cfg)
    print(f"[agent] pairing link: {payload}")
    qr = qrcode.QRCode(border=1)
    qr.add_data(payload)
    try:
        qr.print_ascii(invert=True)
    except Exception:
        pass
    try:
        png = HERE / "pairing-qr.png"
        qrcode.make(payload).save(png)
        if platform.system() == "Windows":
            os.startfile(png)  # pops the QR up in the image viewer
    except Exception as e:
        print(f"[agent] could not open QR image: {e}")


def ensure_startup_shortcut():
    """Frozen exe only: register once to auto-start at logon (per-user,
    no admin needed) — part of the zero-setup promise."""
    if not getattr(sys, "frozen", False) or platform.system() != "Windows":
        return
    link = Path(os.environ.get("APPDATA", "")) / (
        r"Microsoft\Windows\Start Menu\Programs\Startup\Sudo-Service.lnk")
    if link.exists():
        return
    ps = ("$s=(New-Object -ComObject WScript.Shell)"
          f".CreateShortcut('{link}');"
          f"$s.TargetPath='{sys.executable}';"
          "$s.Save()")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       check=False, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        pass


def stop_on_sigterm():
    """systemd stops and restarts the service with SIGTERM, which would end
    the process without Agent.run()'s finally. Take the Ctrl+C path instead,
    so the PC's sound comes back from a phone and the tunnel is stopped."""
    if sys.platform != "win32":
        import signal
        signal.signal(signal.SIGTERM,
                      lambda *_: signal.raise_signal(signal.SIGINT))


def main():
    if not ensure_single_instance():
        print("[agent] another instance is already running — exiting")
        return
    ensure_startup_shortcut()
    cfg = load_config()
    print("=" * 52)
    print("  Sudo agent")
    print(f"  PC name : {cfg['name']}")
    print(f"  Pairing code : {cfg['code']}")
    print(f"  Relay : {cfg['relay']}")
    print("  Scan the QR or enter the code in the mobile app.")
    print("=" * 52)
    agent = Agent(cfg)
    stop_on_sigterm()
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        print("\n[agent] stopped")


if __name__ == "__main__":
    main()
