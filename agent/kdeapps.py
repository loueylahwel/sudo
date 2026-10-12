"""Window switcher and app launcher on KDE Plasma: the wm.* / apps.* backend
that winapps.py is on Windows, returning the same shapes.

Windows come from KWin itself, on X11 and Wayland alike (Wayland lets no
other client list or raise windows): each call loads a tiny KWin script
(org.kde.KWin /Scripting) that reads or changes the workspace and reports
back by calling this agent over D-Bus (callDBus to this connection's own
unique name), then unloads it. A call takes a few milliseconds.

- Window ids are KWin's internalId ("{uuid}"). The phone treats ids as
  opaque, and one is only put into a script after matching the uuid
  pattern.
- Apps are the .desktop files in the XDG data dirs (Flatpak exports
  included). Icons are looked up in the icon theme with kiconfinder6;
  SVGs are rasterized with GdkPixbuf (python3-gi, seen through the venv's
  system site packages).
- Thumbnails: on X11 the window's rect is grabbed from the screen (what is
  on top of it shows too). Wayland has no capture for one window without a
  portal prompt, so there the phone shows the app icon instead.
- Apps start through kstart, which gives each its own systemd scope: an
  agent restart must not take the apps it launched down with it.
"""

import asyncio
import base64
import io
import json
import os
import re
import secrets
import subprocess
import tempfile
from pathlib import Path

import psutil

IFACE = "io.github.loueylahwel.Sudo.KWin"
OBJ_PATH = "/io/github/loueylahwel/Sudo/KWin"
SCRIPT_TIMEOUT = 3.0
ICON_SIZE = 64
_UUID = re.compile(r"^\{[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\}$")

# Prepended to every script: the callback target and a report() helper.
_PRELUDE = """\
const SERVICE = %(service)s, TOKEN = %(token)s;
function report(o) {
    callDBus(SERVICE, %(path)s, %(iface)s, "Result", TOKEN, JSON.stringify(o));
}
function findWindow(id) {
    for (const w of workspace.windowList())
        if (String(w.internalId) === id) return w;
    return null;
}
"""

# Alt-Tab order: top of the stacking order first, switchable windows only.
_LIST = """\
const out = [], order = workspace.stackingOrder;
for (let i = order.length - 1; i >= 0; i--) {
    const w = order[i];
    if (!w.normalWindow || w.skipSwitcher || w.skipTaskbar) continue;
    const g = w.frameGeometry, o = w.output ? w.output.geometry : null;
    out.push({id: String(w.internalId), title: w.caption,
              cls: w.resourceClass, desktop: w.desktopFileName, pid: w.pid,
              active: w === workspace.activeWindow, minimized: w.minimized,
              maximized: w.maximizeMode === 3,
              rect: {left: g.x, top: g.y, width: g.width, height: g.height},
              output: o ? {left: o.x, top: o.y, width: o.width, height: o.height} : null});
}
report(out);
"""

_ACTIONS = {
    "activate": ("if (w.minimized) w.minimized = false; workspace.activeWindow = w;",
                 {"activated": True}),
    "close": ("w.closeWindow();", {"close_requested": True}),
    "minimize": ("w.minimized = true;", {"minimized": True}),
    "maximize": ("if (w.minimized) w.minimized = false; w.setMaximize(true, true);",
                 {"maximized": True}),
    # Like Windows' restore: back from minimized, else out of maximized.
    "restore": ("if (w.minimized) w.minimized = false; else w.setMaximize(false, false);",
                {"restored": True}),
}

_ACTION = """\
const w = findWindow(%(id)s);
if (!w) report({error: "window no longer exists"});
else { %(code)s report({ok: true}); }
"""


def available():
    return "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper().split(":")


def _window_id(window_id):
    wid = str(window_id or "")
    if not _UUID.match(wid):
        raise ValueError(f"bad window id: {window_id!r}")
    return wid


class KdeApps:
    def __init__(self, monitors_fn=None):
        # monitors_fn: the agent's list_monitors (mss numbering) for the
        # "monitor" field; None leaves it at 1.
        self._monitors_fn = monitors_fn
        self._bus = None
        self._waiting = {}
        self._bus_lock = asyncio.Lock()
        self._icons = {}          # icon key -> base64 PNG or None
        self._apps = None         # cached apps.list entries
        self._app_icon_names = {}  # app id -> Icon= value

    # ---------------------------------------------------------- KWin calls

    async def _connect(self):
        async with self._bus_lock:
            if self._bus is not None and self._bus.connected:
                return self._bus
            from dbus_fast import BusType
            from dbus_fast.aio import MessageBus
            from dbus_fast.service import ServiceInterface, method

            waiting = self._waiting

            class Receiver(ServiceInterface):
                def __init__(self):
                    super().__init__(IFACE)

                @method()
                def Result(self, token: "s", payload: "s") -> None:
                    fut = waiting.pop(token, None)
                    if fut is not None and not fut.done():
                        fut.set_result(payload)

            bus = await MessageBus(bus_type=BusType.SESSION).connect()
            bus.export(OBJ_PATH, Receiver())
            self._bus = bus
            return bus

    async def _kwin(self, bus, path, iface, member, signature="", body=()):
        from dbus_fast import Message, MessageType
        reply = await bus.call(Message(destination="org.kde.KWin", path=path,
                                       interface=iface, member=member,
                                       signature=signature, body=list(body)))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"KWin {member}: {reply.error_name} {reply.body}")
        return reply.body

    async def _script(self, body):
        """Runs `body` as a KWin script and returns what it report()ed."""
        bus = await self._connect()
        token = secrets.token_hex(8)
        text = _PRELUDE % {"service": json.dumps(bus.unique_name),
                           "token": json.dumps(token),
                           "path": json.dumps(OBJ_PATH),
                           "iface": json.dumps(IFACE)} + body
        fd, path = tempfile.mkstemp(prefix="sudo-kwin-", suffix=".js")
        name = "sudo_agent_" + token
        fut = asyncio.get_running_loop().create_future()
        self._waiting[token] = fut
        try:
            os.write(fd, text.encode())
            os.close(fd)
            (script_id,) = await self._kwin(bus, "/Scripting", "org.kde.kwin.Scripting",
                                            "loadScript", "ss", [path, name])
            if script_id < 0:
                raise RuntimeError("KWin refused the script")
            await self._kwin(bus, f"/Scripting/Script{script_id}", "org.kde.kwin.Script", "run")
            return json.loads(await asyncio.wait_for(fut, SCRIPT_TIMEOUT))
        finally:
            self._waiting.pop(token, None)
            try:
                await self._kwin(bus, "/Scripting", "org.kde.kwin.Scripting",
                                 "unloadScript", "s", [name])
            except Exception:
                pass
            os.unlink(path)

    # ------------------------------------------------------------- windows

    async def list_windows(self, icons=True):
        raw = await self._script(_LIST)
        return await asyncio.to_thread(self._shape_windows, raw, icons)

    def _shape_windows(self, raw, icons):
        monitors = []
        if self._monitors_fn is not None:
            try:
                monitors = self._monitors_fn()
            except Exception:
                monitors = []
        out = []
        for w in raw:
            process, exe = w.get("cls") or "", ""
            try:
                p = psutil.Process(int(w.get("pid") or 0))
                process, exe = p.name(), p.exe()
            except (psutil.Error, ValueError):
                pass
            win = {"id": w["id"], "title": w.get("title") or "", "pid": w.get("pid") or 0,
                   "process": process, "exe": exe, "active": bool(w.get("active")),
                   "minimized": bool(w.get("minimized")), "maximized": bool(w.get("maximized")),
                   "monitor": _monitor_index(monitors, w.get("output")), "rect": w.get("rect")}
            if icons:
                win["icon"] = self._window_icon(w)
            out.append(win)
        return {"windows": out}

    async def _act(self, action, window_id):
        code, result = _ACTIONS[action]
        r = await self._script(_ACTION % {"id": json.dumps(_window_id(window_id)), "code": code})
        if isinstance(r, dict) and r.get("error"):
            raise ValueError(r["error"])
        return dict(result)

    async def activate_window(self, window_id):
        return await self._act("activate", window_id)

    async def close_window(self, window_id):
        return await self._act("close", window_id)

    async def minimize_window(self, window_id):
        return await self._act("minimize", window_id)

    async def maximize_window(self, window_id):
        return await self._act("maximize", window_id)

    async def restore_window(self, window_id):
        return await self._act("restore", window_id)

    async def window_thumb(self, window_id, max_width=360):
        wid = _window_id(window_id)
        wins = (await self.list_windows(icons=False))["windows"]
        w = next((x for x in wins if x["id"] == wid), None)
        if w is None:
            raise ValueError("window no longer exists")
        rect = w["rect"] or {}
        if w["minimized"] or os.environ.get("XDG_SESSION_TYPE") == "wayland":
            return {"jpeg": None, "w": rect.get("width", 0), "h": rect.get("height", 0)}
        return await asyncio.to_thread(_grab_rect_jpeg, rect, int(max_width or 360))

    # ---------------------------------------------------------------- apps

    async def list_start_apps(self, refresh=False):
        if self._apps is None or refresh:
            self._apps = await asyncio.to_thread(_scan_desktop_files)
            self._app_icon_names = {a["id"]: a.pop("_icon") for a in self._apps}
        out = []
        for a in self._apps:
            item = dict(a)
            icon = self._icons.get(("app", a["id"]))
            if icon:
                item["icon"] = icon
            out.append(item)
        return {"apps": out}

    def _known_app(self, app_id):
        if self._apps is None:
            self._apps = _scan_desktop_files()
            self._app_icon_names = {a["id"]: a.pop("_icon") for a in self._apps}
        app = next((a for a in self._apps if a["id"] == app_id), None)
        if app is None:
            raise ValueError(f"unknown app: {app_id}")
        return app

    async def start_app_icon(self, app_id):
        app = await asyncio.to_thread(self._known_app, str(app_id))
        key = ("app", app["id"])
        if key not in self._icons:
            self._icons[key] = await asyncio.to_thread(
                _icon_png, self._app_icon_names.get(app["id"]))
        return {"id": app["id"], "icon": self._icons[key]}

    async def launch_start_app(self, app_id):
        app = await asyncio.to_thread(self._known_app, str(app_id))
        subprocess.Popen(["kstart", "--application", app["id"]],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return {"launched": True, "id": app["id"], "name": app["name"]}

    # --------------------------------------------------------------- icons

    def _window_icon(self, w):
        desktop = (w.get("desktop") or "").removesuffix(".desktop")
        key = ("win", desktop or w.get("cls") or "")
        if key not in self._icons:
            name = None
            if desktop:
                entry = _find_desktop_file(desktop)
                name = entry.get("Icon") if entry else None
            self._icons[key] = _icon_png(name or desktop or (w.get("cls") or "").lower())
        return self._icons[key]


def _monitor_index(monitors, output):
    """mss numbering (1 = first monitor) of the monitor at `output`'s origin."""
    if output:
        for i, m in enumerate(monitors[1:], start=1):
            if m["left"] == output["left"] and m["top"] == output["top"]:
                return i
    return 1


def _grab_rect_jpeg(rect, max_width):
    import mss
    from PIL import Image
    with (getattr(mss, "MSS", None) or mss.mss)() as sct:
        screen = sct.monitors[0]
        left = max(rect["left"], screen["left"])
        top = max(rect["top"], screen["top"])
        right = min(rect["left"] + rect["width"], screen["left"] + screen["width"])
        bottom = min(rect["top"] + rect["height"], screen["top"] + screen["height"])
        if right - left < 2 or bottom - top < 2:
            return {"jpeg": None, "w": rect["width"], "h": rect["height"]}
        shot = sct.grab({"left": left, "top": top, "width": right - left, "height": bottom - top})
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if img.width > max_width:
        img = img.resize((max_width, max(1, round(img.height * max_width / img.width))))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return {"jpeg": base64.b64encode(buf.getvalue()).decode(), "w": img.width, "h": img.height}


def _icon_png(name):
    """Base64 PNG (ICON_SIZE px) for an icon theme name or an absolute path,
    or None. Never raises: an icon must never break a list."""
    try:
        if not name:
            return None
        path = name if os.path.isabs(name) else subprocess.run(
            ["kiconfinder6", name], capture_output=True, text=True, timeout=3).stdout.strip()
        if not path or not os.path.exists(path):
            return None
        if path.endswith((".svg", ".svgz")):
            import gi
            gi.require_version("GdkPixbuf", "2.0")
            from gi.repository import GdkPixbuf
            pix = GdkPixbuf.Pixbuf.new_from_file_at_size(path, ICON_SIZE, ICON_SIZE)
            ok, data = pix.save_to_bufferv("png", [], [])
            return base64.b64encode(bytes(data)).decode() if ok else None
        from PIL import Image
        img = Image.open(path).convert("RGBA")
        img.thumbnail((ICON_SIZE, ICON_SIZE))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


# ------------------------------------------------------------ .desktop files

def _application_dirs():
    home = Path.home()
    dirs = [Path(os.environ.get("XDG_DATA_HOME", home / ".local/share")),
            home / ".local/share/flatpak/exports/share", Path("/var/lib/flatpak/exports/share")]
    dirs += [Path(p) for p in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":") if p]
    seen, out = set(), []
    for d in dirs:
        a = d / "applications"
        if a.is_dir() and a not in seen:
            seen.add(a)
            out.append(a)
    return out


def _read_entry(path):
    """The [Desktop Entry] group of a .desktop file as a dict."""
    entry, group = {}, None
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("["):
                group = line
            elif group == "[Desktop Entry]" and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                entry.setdefault(k.strip(), v.strip())
    except OSError:
        pass
    return entry


def _find_desktop_file(desktop_id):
    for d in _application_dirs():
        p = d / f"{desktop_id}.desktop"
        if p.is_file():
            return _read_entry(p)
    return None


def _scan_desktop_files():
    """Launchable apps, the way Plasma's menu shows them: id is the desktop
    file id without .desktop (what kstart --application takes)."""
    apps, seen = [], set()
    for d in _application_dirs():  # earlier dirs win (user over system)
        for p in sorted(d.rglob("*.desktop")):
            app_id = str(p.relative_to(d)).replace("/", "-").removesuffix(".desktop")
            if app_id in seen:
                continue
            seen.add(app_id)
            e = _read_entry(p)
            desktops = {x for x in os.environ.get("XDG_CURRENT_DESKTOP", "KDE").split(":") if x}
            if (e.get("Type") != "Application" or e.get("NoDisplay") == "true"
                    or e.get("Hidden") == "true" or not e.get("Exec") or not e.get("Name")):
                continue
            only = {x for x in e.get("OnlyShowIn", "").split(";") if x}
            never = {x for x in e.get("NotShowIn", "").split(";") if x}
            if (only and not only & desktops) or never & desktops:
                continue
            apps.append({"id": app_id, "name": e["Name"], "_icon": e.get("Icon")})
    apps.sort(key=lambda a: a["name"].lower())
    return apps
