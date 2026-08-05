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
import json
import os
import platform
import secrets
import shutil
import socket
import string
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse

import mss
import psutil
import pyperclip
import websockets
from PIL import Image, ImageDraw
from pynput.keyboard import Controller as KbController, Key
from pynput.mouse import Button, Controller as MouseController

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

# ---------------------------------------------------------------- config

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def load_config():
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except Exception:
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

_screen_size_cache = None


def screen_size():
    # Resolved once and cached: creating a fresh mss instance for every
    # input event caused intermittent failures under rapid touchpad input.
    global _screen_size_cache
    if _screen_size_cache is None:
        with mss.mss() as sct:
            mon = sct.monitors[1]  # primary monitor
            _screen_size_cache = (mon["width"], mon["height"])
    return _screen_size_cache


def draw_cursor(img, mon):
    """mss does not capture the pointer, so paint it on the frame ourselves —
    the phone user needs to see where the PC cursor is. Returns the cursor
    position quantized to ~4px so frame-skip detection treats cursor-only
    movement as a frame change (an idle desktop still skips)."""
    cx, cy = mouse.position
    x = (cx - mon["left"]) * img.width / mon["width"]
    y = (cy - mon["top"]) * img.height / mon["height"]
    if 0 <= x < img.width and 0 <= y < img.height:
        s = max(10, img.width // 90)
        pts = [(x, y), (x, y + s * 1.4), (x + s * 0.38, y + s * 1.05),
               (x + s * 0.58, y + s * 1.5), (x + s * 0.78, y + s * 1.42),
               (x + s * 0.58, y + s * 0.98), (x + s * 0.95, y + s * 0.95)]
        ImageDraw.Draw(img).polygon(pts, fill="white", outline="black")
    return int(cx // 2), int(cy // 2)


def capture_jpeg(max_width=960, quality=50):
    with mss.mss() as sct:
        mon = sct.monitors[1]
        shot = sct.grab(mon)
    img = Image.frombytes("RGB", shot.size, shot.rgb)
    if img.width > max_width:
        h = round(img.height * max_width / img.width)
        img = img.resize((max_width, h), Image.BILINEAR)
    qpos = draw_cursor(img, mon)
    # tiny grayscale thumb + cursor position used to skip unchanged frames
    thumb = img.resize((32, 18), Image.NEAREST).convert("L").tobytes()
    thumb += qpos[0].to_bytes(2, "little") + qpos[1].to_bytes(2, "little")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue(), img.width, img.height, thumb


class ScreenStreamer:
    """Streams JPEG frames to one client until stopped.

    binary=True sends raw PJF1 packets (no base64, ~33% smaller); binary=False
    sends legacy base64 JSON events. Frames identical to the previous one are
    skipped entirely — an idle desktop costs almost no bandwidth.
    """

    def __init__(self, agent, client_id, fps=10, quality=50, max_width=960,
                 binary=False):
        self.agent = agent
        self.client_id = client_id
        self.fps = max(1, min(int(fps), 20))
        self.quality = max(20, min(int(quality), 90))
        self.max_width = max(320, min(int(max_width), 1920))
        self.binary = binary
        self._last_thumb = None
        self._wake = asyncio.Event()  # any cursor movement nudges a frame
        self._hot_until = 0.0         # stay at full rate briefly after change
        self.task = None

    def wake(self):
        self._wake.set()

    def start(self):
        self.task = asyncio.create_task(self._run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def _run(self):
        errors = 0
        while True:
            t0 = time.monotonic()
            changed = False
            try:
                jpeg, w, h, thumb = await asyncio.to_thread(
                    capture_jpeg, self.max_width, self.quality
                )
                # Drop frames when the link is congested instead of letting
                # backpressure stall the agent; the thumb is only consumed
                # when a frame actually goes out, so the accumulated change
                # is sent once the link drains.
                if thumb != self._last_thumb and not self.agent.congested():
                    self._last_thumb = thumb
                    changed = True
                    # Stay hot for a second after the last change so
                    # stop-start movement doesn't oscillate hot/idle.
                    self._hot_until = time.monotonic() + 1.0
                    if self.binary:
                        header = json.dumps(
                            {"w": w, "h": h, "src": "screen"}).encode()
                        packet = (b"PJF1" + len(header).to_bytes(4, "little")
                                  + header + jpeg)
                        await self.agent.send_bytes(packet)
                    else:
                        await self.agent.send_to(self.client_id, {
                            "event": "screen.frame",
                            "data": {"jpeg": base64.b64encode(jpeg).decode(),
                                     "w": w, "h": h},
                        })
                errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Transient capture hiccup (display sleep, UAC secure
                # desktop, GDI churn): skip the frame and keep streaming.
                # NEVER return here — a dead stream is a frozen phone screen.
                errors += 1
                if errors == 25:
                    print(f"[agent] screen capture failing repeatedly: {e}")
                await asyncio.sleep(0.2)
                continue
            # Adaptive rate: full fps while the screen changes (and for a
            # grace second after), a gentle tick when idle — but any cursor
            # movement (phone input or the physical mouse, via the watcher)
            # wakes us immediately, so idle never adds cursor latency.
            idle_fps = max(3, self.fps // 3)
            hot = changed or time.monotonic() < self._hot_until
            delay = max(0.0, 1.0 / (self.fps if hot else idle_fps)
                        - (time.monotonic() - t0))
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()


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
                    if ok and not self.agent.congested():
                        header = json.dumps({
                            "w": int(frame.shape[1]),
                            "h": int(frame.shape[0]),
                            "src": "camera",
                        }).encode()
                        await self.agent.send_bytes(
                            b"PJF1" + len(header).to_bytes(4, "little")
                            + header + buf.tobytes())
                    await asyncio.sleep(
                        max(0.0, 1.0 / self.fps - (time.monotonic() - t0)))
            finally:
                cap.release()


class H264Streamer:
    """Streams the screen as H.264 via an ffmpeg subprocess (NVENC when the
    driver supports it, QSV, then libx264 as the always-works fallback).

    Packets: [4B "H264"][4B LE header len][header JSON][Annex-B chunk].
    The stream always starts with SPS/PPS, so the client's decoder can
    configure itself from the first bytes."""

    ENCODERS = [
        ("h264_nvenc", ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ll",
                        "-profile:v", "baseline", "-bf", "0"]),
        ("h264_qsv", ["-c:v", "h264_qsv", "-preset", "veryfast",
                      "-profile:v", "baseline", "-bf", "0"]),
        ("libx264", ["-c:v", "libx264", "-preset", "ultrafast",
                     "-tune", "zerolatency", "-profile:v", "baseline",
                     "-bf", "0"]),
    ]

    def __init__(self, agent, client_id, fps=30, max_width=1280, bitrate=2_000_000):
        self.agent = agent
        self.client_id = client_id
        self.fps = max(5, min(int(fps), 60))
        self.max_width = max(320, min(int(max_width), 1920))
        self.bitrate = max(500_000, min(int(bitrate), 8_000_000))
        self.task = None
        self.proc = None
        self.encoder = None
        self._stopping = False

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

    def _cmd(self, enc_name, enc_args):
        w, h = screen_size()
        scale_w = min(w, self.max_width)
        return [
            self.ffmpeg_path(), "-hide_banner", "-loglevel", "error",
            "-f", "gdigrab", "-framerate", str(self.fps), "-i", "desktop",
            "-vf", f"scale=w={scale_w}:h=-2,format=yuv420p",
            *enc_args,
            "-g", str(self.fps * 2),
            "-b:v", str(self.bitrate), "-maxrate", str(self.bitrate),
            "-bufsize", str(self.bitrate * 2),
            "-f", "h264", "pipe:1",
        ]

    async def _run(self):
        for enc_name, enc_args in self.ENCODERS:
            try:
                worked = await self._stream(enc_name, enc_args)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[agent] h264 {enc_name} failed to start: {e}")
                continue  # next encoder in the chain
            if not worked:
                print(f"[agent] h264 {enc_name} unavailable, trying next")
                continue
            # It worked but died mid-stream: restart the same encoder a few
            # times instead of leaving the client's video frozen.
            restarts = 0
            while worked and restarts < 3 and not self._stopping:
                restarts += 1
                print(f"[agent] h264 {enc_name} died, restart {restarts}/3")
                await asyncio.sleep(0.5)
                try:
                    worked = await self._stream(enc_name, enc_args)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"[agent] h264 {enc_name} restart failed: {e}")
                    break
            return
        if not self._stopping:
            await self.agent.send_to(self.client_id, {
                "event": "screen.error",
                "data": {"error": "no working h264 encoder found"},
            })

    async def _stream(self, enc_name, enc_args):
        """Run one encoder until it dies. Returns True if it produced
        output (i.e. it actually worked); False if it failed immediately."""
        # CREATE_NO_WINDOW: ffmpeg is a console app — without this it pops a
        # visible console window the user can close, killing the stream.
        no_window = 0x08000000 if platform.system() == "Windows" else 0
        self.proc = await asyncio.create_subprocess_exec(
            *self._cmd(enc_name, enc_args),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=no_window,
        )
        self.encoder = enc_name
        produced = False
        header = json.dumps({
            "w": min(screen_size()[0], self.max_width),
            "h": round(screen_size()[1]
                       * min(screen_size()[0], self.max_width)
                       / screen_size()[0]),
            "codec": "h264", "encoder": enc_name, "fps": self.fps,
        }).encode()
        packet_head = b"H264" + len(header).to_bytes(4, "little") + header
        while True:
            chunk = await self.proc.stdout.read(65536)
            if not chunk:  # encoder exited
                return produced
            produced = True
            if not self.agent.congested():
                await self.agent.send_bytes(packet_head + chunk)


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


def do_move(x, y):
    w, h = screen_size()
    mouse.position = (int(max(0.0, min(1.0, x)) * w),
                      int(max(0.0, min(1.0, y)) * h))


# Sub-pixel remainders from do_move_rel: truncating every delta to whole
# pixels on its own rounds small drags down to 0 and swallows them, which
# made the cursor feel like it "sometimes doesn't move". The carry makes
# every fraction of a pixel eventually move the cursor.
_move_carry = [0.0, 0.0]


def do_move_rel(dx, dy):
    """Relative move — dx/dy are normalized deltas (-1..1 of screen size)."""
    w, h = screen_size()
    _move_carry[0] += dx * w
    _move_carry[1] += dy * h
    mx, my = int(_move_carry[0]), int(_move_carry[1])
    if mx or my:
        mouse.move(mx, my)
        _move_carry[0] -= mx
        _move_carry[1] -= my


def do_click(button="left", action="click", x=None, y=None, count=1):
    if x is not None and y is not None:
        do_move(x, y)
    btn = BUTTONS.get(button, Button.left)
    if action == "down":
        mouse.press(btn)
    elif action == "up":
        mouse.release(btn)
    else:
        mouse.click(btn, max(1, int(count)))


def do_scroll(dx=0, dy=0):
    mouse.scroll(int(dx), int(dy))


def do_key(name):
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
    proc = subprocess.run(
        cmd, shell=True, cwd=cwd or None,
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

def power(action):
    system = platform.system()
    cmds = {
        "Windows": {
            "lock": "rundll32.exe user32.dll,LockWorkStation",
            "shutdown": "shutdown /s /t 5",
            "restart": "shutdown /r /t 5",
            "sleep": "rundll32.exe powrprof.dll,SetSuspendState 0,1,0",
        },
        "Linux": {
            "lock": "loginctl lock-session",
            "shutdown": "systemctl poweroff",
            "restart": "systemctl reboot",
            "sleep": "systemctl suspend",
        },
        "Darwin": {
            "lock": 'osascript -e \'tell application "System Events" to keystroke "q" using {control down, command down}\'',
            "shutdown": "osascript -e 'tell app \"System Events\" to shut down'",
            "restart": "osascript -e 'tell app \"System Events\" to restart'",
            "sleep": "pmset sleepnow",
        },
    }
    cmd = cmds.get(system, {}).get(action)
    if not cmd:
        raise ValueError(f"power.{action} not supported on {system}")
    subprocess.Popen(cmd, shell=True)
    return {"action": action, "scheduled": True}


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


def _media_info():
    return _run_media_ps1([])


def _media_seek(seconds):
    return _run_media_ps1(["-Seek", str(seconds)])


# ---------------------------------------------------------------- agent

class EmbeddedRelay:
    """The relay, embedded in the agent: phones connect straight to the PC
    over the LAN. One exe is the whole PC side — no separate relay process."""

    def __init__(self, agent, port):
        self.agent = agent
        self.port = port
        self.clients = {}  # client_id -> websocket

    async def serve(self):
        async with websockets.serve(
            self._handle, "0.0.0.0", self.port,
            max_size=8 * 1024 * 1024, ping_interval=20, ping_timeout=20,
        ):
            print(f"[agent] listening for phones on :{self.port}")
            self.agent._status(f"Listening on :{self.port}")
            await asyncio.Future()  # serve forever

    async def _handle(self, ws):
        client_id = uuid.uuid4().hex
        paired = False
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


class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ws = None
        self.streamers = {}          # client_id -> ScreenStreamer
        self.known_clients = set()
        self._clipboard_last = None
        self._clipboard_watch = False
        self.on_status = None        # optional callback(str), used by the GUI
        self.loop = None             # set in run()
        self.camera_streamers = {}   # client_id -> CameraStreamer
        self.video_streamers = {}    # client_id -> H264Streamer
        self.embedded = None         # EmbeddedRelay when running standalone
        self.approval = None         # ApprovalManager (service mode)

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
        """Raw binary frame (screen/camera packets)."""
        if self.embedded:
            await self.embedded.broadcast_bytes(data)
        elif self.ws:
            await self.ws.send(data)

    def congested(self):
        """True when the socket write buffer is backing up — the streamer
        drops frames rather than letting backpressure freeze the agent."""
        try:
            return bool(self.ws) and \
                self.ws.transport.get_write_buffer_size() > 2 * 1024 * 1024
        except Exception:
            return False

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
        items.append(("Drives", ""))
        return {"shortcuts": [{"name": n, "path": p} for n, p in items]}

    async def h_fs_download(self, msg):
        return await asyncio.to_thread(
            read_chunk, msg["path"], msg.get("offset", 0), msg.get("length", CHUNK)
        )

    async def h_fs_upload(self, msg):
        return await asyncio.to_thread(
            write_chunk, msg["path"], msg["data"],
            msg.get("offset", 0), msg.get("append", False),
        )

    async def h_fs_delete(self, msg):
        return await asyncio.to_thread(delete_path, msg["path"])

    async def h_fs_mkdir(self, msg):
        Path(msg["path"]).mkdir(parents=True, exist_ok=True)
        return {"path": msg["path"]}

    async def h_input(self, msg):
        action = msg.get("action")
        if action == "move":
            do_move(msg.get("x", 0), msg.get("y", 0))
        elif action == "move_rel":
            do_move_rel(msg.get("dx", 0), msg.get("dy", 0))
        elif action == "click":
            do_click(msg.get("button", "left"), "click",
                     msg.get("x"), msg.get("y"), msg.get("count", 1))
        elif action == "down":
            do_click(msg.get("button", "left"), "down", msg.get("x"), msg.get("y"))
        elif action == "up":
            do_click(msg.get("button", "left"), "up", msg.get("x"), msg.get("y"))
        elif action == "scroll":
            do_scroll(msg.get("dx", 0), msg.get("dy", 0))
        elif action == "key":
            do_key(msg["key"])
        elif action == "text":
            do_text(msg.get("text", ""))
        else:
            raise ValueError(f"unknown input action: {action}")
        # Any input changes the screen: wake the streamer NOW so the frame
        # capturing this change isn't delayed by the idle tick.
        s = self.streamers.get(msg.get("from"))
        if s:
            s.wake()
        return {}

    async def h_power(self, msg):
        return power(msg.get("action", "lock"))

    async def h_media(self, msg):
        do_key(msg.get("action", "media_play_pause"))
        return {}

    async def h_media_info(self, msg):
        return await asyncio.to_thread(_media_info)

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
        old = self.streamers.pop(client, None)
        if old:
            await old.stop()
        old_v = self.video_streamers.pop(client, None)
        if old_v:
            await old_v.stop()
        if str(msg.get("codec", "jpeg")).lower() == "h264":
            streamer = H264Streamer(
                self, client,
                fps=msg.get("fps", 30),
                max_width=msg.get("max_width", 1280),
                bitrate=msg.get("bitrate", 4_000_000),
            )
            streamer.start()
            self.video_streamers[client] = streamer
            return {"streaming": True, "codec": "h264",
                    "fps": streamer.fps}
        streamer = ScreenStreamer(
            self, client,
            fps=msg.get("fps", 10), quality=msg.get("quality", 50),
            max_width=msg.get("max_width", 960),
            binary=bool(msg.get("binary", False)),
        )
        streamer.start()
        self.streamers[client] = streamer
        return {"streaming": True, "codec": "jpeg", "fps": streamer.fps,
                "binary": streamer.binary}

    async def h_screen_stop(self, msg):
        client = msg.get("from")
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
        "media": h_media,
        "media.info": h_media_info,
        "media.seek": h_media_seek,
        "clipboard.get": h_clipboard_get,
        "clipboard.set": h_clipboard_set,
        "clipboard.watch": h_clipboard_watch,
        "screen.start": h_screen_start,
        "screen.stop": h_screen_stop,
        "camera.start": h_camera_start,
        "camera.stop": h_camera_stop,
        "notify.subscribe": h_notify_subscribe,
    }

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

    async def cursor_watcher(self):
        """Poll the cursor position (~16x/s) and wake screen streamers when
        it moves — so physical mouse movement revives the stream just as
        fast as phone input does. GetCursorPos is effectively free."""
        last = mouse.position
        while True:
            await asyncio.sleep(0.06)
            try:
                pos = mouse.position
            except Exception:
                continue
            if pos != last:
                last = pos
                for s in list(self.streamers.values()):
                    s.wake()

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
        if client_id in self.known_clients:
            self.known_clients.discard(client_id)
            streamer = self.streamers.pop(client_id, None)
            if streamer:
                await streamer.stop()
            video = self.video_streamers.pop(client_id, None)
            if video:
                await video.stop()
            cam = self.camera_streamers.pop(client_id, None)
            if cam:
                await cam.stop()

    # ---- main loop ----

    async def run(self):
        self.loop = asyncio.get_running_loop()
        asyncio.create_task(self.discovery_beacon())
        asyncio.create_task(self.cursor_watcher())
        host = urlparse(self.cfg["relay"]).hostname
        if host in (None, "127.0.0.1", "localhost", "::1"):
            # Standalone: the relay is embedded — the phone connects straight
            # to this process. One exe is the whole PC side.
            from approval import ApprovalManager
            self.approval = ApprovalManager()
            self.approval.start(self.loop)
            port = urlparse(self.cfg["relay"]).port or 8080
            self.embedded = EmbeddedRelay(self, port)
            await self.embedded.serve()
            return
        await self._run_outbound()

    async def _run_outbound(self):
        backoff = 1
        while True:
            try:
                async with websockets.connect(
                    self.cfg["relay"], ping_interval=20, ping_timeout=20,
                    max_size=8 * 1024 * 1024,
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
                        for c in list(self.streamers):
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
        if client and client not in self.known_clients:
            self.known_clients.add(client)
            self._status("Phone connected")
        cmd = msg.get("cmd")
        handler = self.HANDLERS.get(cmd) if cmd else None
        if not handler:
            if msg.get("id"):
                await self.reply(msg, ok=False, error=f"unknown cmd: {cmd}")
            return
        try:
            data = await handler(self, msg)
            if msg.get("id"):
                await self.reply(msg, ok=True, data=data)
        except Exception as e:
            if msg.get("id"):
                await self.reply(msg, ok=False, error=e)
            else:
                print(f"[agent] error in {cmd}: {e}")


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
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        print("\n[agent] stopped")


if __name__ == "__main__":
    main()
