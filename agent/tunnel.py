"""Reaching this PC from any network, even when the provider blocks
incoming connections (remote_access.py's router forward then never works).

The PC dials out instead: cloudflared opens a Cloudflare "quick tunnel"
(no account) and gets a random https://<words>.trycloudflare.com address
that forwards to a loopback bridge here. Each WebSocket the phone opens on
that address carries one raw TCP stream into the agent's TLS port, so the
phone runs the same pinned TLS as on the LAN *through* the tunnel:
Cloudflare only carries encrypted bytes and can't pose as the PC, and only
phones holding a trust token from a LAN pairing get in.

A quick tunnel's address changes whenever cloudflared restarts (PC reboot,
crash). The PC posts the current one to a secret ntfy topic (128 random
bits in the name, shared only with paired phones); a phone whose saved
address stopped answering reads the latest from there. A forged post can
only send a phone to a server that fails its certificate pin.
"""

import asyncio
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

import websockets

NTFY_SERVER = "https://ntfy.sh"
HEALTH_PATH = "/sudo-health"
HEALTH_BODY = "sudo tunnel ok\n"
_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def new_topic():
    return "sudo-" + secrets.token_hex(16)


# ---------------------------------------------------------------- bridge

def _health(*args):
    """process_request hook: answers the tunnel self-check with a known
    body, so a reply proves the whole path (Cloudflare -> cloudflared ->
    this bridge) works. websockets >= 14 passes (connection, request), the
    legacy API (path, headers)."""
    first, second = args
    if hasattr(second, "path"):
        if second.path == HEALTH_PATH:
            return first.respond(200, HEALTH_BODY)
        return None
    if first == HEALTH_PATH:
        return 200, [("Content-Type", "text/plain")], HEALTH_BODY.encode()
    return None


async def serve_bridge(tls_port, aliases):
    """Loopback WebSocket server cloudflared forwards to. Every phone
    WebSocket becomes one TCP connection to the TLS port on 127.0.0.1;
    binary messages carry its bytes both ways. `aliases` maps that
    connection's local port to the phone's real address (Cloudflare's
    Cf-Connecting-Ip), so the TLS port's per-address limits still apply
    per phone instead of to 127.0.0.1 as a whole. Returns (server, port)."""

    async def handle(ws):
        request = getattr(ws, "request", None)
        headers = request.headers if request is not None \
            else getattr(ws, "request_headers", {})
        real = (headers.get("Cf-Connecting-Ip") or "").strip() or "tunnel"
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1",
                                                           tls_port)
        except OSError:
            await ws.close(1011, "PC not ready")
            return
        local_port = writer.get_extra_info("sockname")[1]
        aliases[local_port] = real
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass

        async def phone_to_pc():
            async for msg in ws:
                writer.write(msg if isinstance(msg, bytes) else msg.encode())
                await writer.drain()

        async def pc_to_phone():
            while True:
                data = await reader.read(65536)
                if not data:
                    return
                await ws.send(data)

        tasks = [asyncio.ensure_future(phone_to_pc()),
                 asyncio.ensure_future(pc_to_phone())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            aliases.pop(local_port, None)
            writer.close()
            try:
                await ws.close()
            except Exception:
                pass

    server = await websockets.serve(
        handle, "127.0.0.1", 0, compression=None, max_size=1024 * 1024,
        ping_interval=20, ping_timeout=20, process_request=_health)
    port = next(iter(server.sockets)).getsockname()[1]
    return server, port


# ---------------------------------------------------------------- cloudflared

def _exe_name():
    return "cloudflared.exe" if platform.system() == "Windows" \
        else "cloudflared"


def _asset_name():
    system, machine = platform.system(), platform.machine().lower()
    if system == "Windows":
        return "cloudflared-windows-amd64.exe" if machine.endswith("64") \
            else "cloudflared-windows-386.exe"
    if system == "Linux":
        if machine in ("x86_64", "amd64"):
            return "cloudflared-linux-amd64"
        if machine in ("aarch64", "arm64"):
            return "cloudflared-linux-arm64"
        if machine.startswith("arm"):
            return "cloudflared-linux-arm"
        if machine in ("i386", "i686", "x86"):
            return "cloudflared-linux-386"
    raise RuntimeError(f"no cloudflared build for {system} {machine}")


def find_cloudflared(folder):
    """A cloudflared already on this PC: our own download, one shipped next
    to the agent (tools/), or the system's."""
    here = Path(sys.executable).parent if getattr(sys, "frozen", False) \
        else Path(__file__).resolve().parent
    name = _exe_name()
    for candidate in (folder / "bin" / name, here / name,
                      here / "tools" / name, here.parent / "tools" / name):
        if candidate.is_file():
            return str(candidate)
    return shutil.which("cloudflared")


def ensure_cloudflared(folder):
    """Path to cloudflared, downloaded once from Cloudflare's GitHub
    releases (HTTPS) into `folder`/bin when the PC has none."""
    found = find_cloudflared(folder)
    if found:
        return found
    url = ("https://github.com/cloudflare/cloudflared/releases/latest/"
           "download/" + _asset_name())
    dest = folder / "bin" / _exe_name()
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    print(f"[agent] tunnel: downloading cloudflared from {url}")
    with urllib.request.urlopen(url, timeout=60) as resp, \
            open(part, "wb") as out:
        shutil.copyfileobj(resp, out)
    part.chmod(0o755)
    os.replace(part, dest)
    return str(dest)


def publish(rendezvous, url, timeout=15):
    """Post the tunnel address to the phone's secret ntfy topic."""
    body = json.dumps({"url": url, "t": int(time.time())}).encode()
    req = urllib.request.Request(rendezvous, data=body, method="POST",
                                 headers={"Content-Type": "text/plain"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


def check(url, timeout=15):
    """True when a request to the tunnel address reaches our bridge."""
    try:
        with urllib.request.urlopen(url + HEALTH_PATH, timeout=timeout) as r:
            return r.read().decode(errors="replace") == HEALTH_BODY
    except (OSError, ValueError):
        return False


def _die_with_parent():
    """Linux, in the child before exec: take cloudflared down with the
    agent (PR_SET_PDEATHSIG), even when the agent is killed outright."""
    try:
        import ctypes
        import signal
        ctypes.CDLL(None).prctl(1, signal.SIGTERM)
    except Exception:
        pass


class QuickTunnel:
    """Keeps a cloudflared quick tunnel to the bridge up on a background
    thread: restarts it when it exits or stops answering, and keeps the
    secret topic holding the current address (ntfy.sh keeps a post 12 h,
    so it is renewed every 6 h)."""

    CHECK_EVERY = 5 * 60      # self-check through Cloudflare
    CHECK_FAILURES = 3        # consecutive failed checks before a restart
    REPUBLISH_EVERY = 6 * 3600
    FIRST_CHECK = 45          # new hostnames take a moment to resolve

    def __init__(self, bridge_port, folder, rendezvous):
        self.bridge_port = bridge_port
        self.folder = folder
        self.rendezvous = rendezvous
        self.url = None
        self.error = "starting"
        self.on_change = None     # callback() when url/error changes
        self._proc = None
        self._stop = threading.Event()
        self._log = deque(maxlen=12)
        self._pid_file = folder / "tunnel.pid"

    def info(self):
        return {"url": self.url, "error": self.error,
                "rendezvous": self.rendezvous}

    def start(self):
        threading.Thread(target=self._loop, name="sudo-tunnel",
                         daemon=True).start()

    def stop(self):
        self._stop.set()
        self._kill()

    # ---- internals

    def _set(self, url, error):
        if (url, error) == (self.url, self.error):
            return
        self.url, self.error = url, error
        print("[agent] tunnel: " + (f"up at {url}" if url
                                    else f"down ({error})"))
        cb = self.on_change
        if cb:
            try:
                cb()
            except Exception:
                pass

    def _kill(self):
        proc = self._proc
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            self._pid_file.unlink()
        except OSError:
            pass

    def _kill_leftover(self):
        """A cloudflared an earlier agent left behind (Windows doesn't take
        children down with their parent): it would hold a dead tunnel."""
        try:
            pid = int(self._pid_file.read_text())
        except (OSError, ValueError):
            return
        try:
            import psutil
            p = psutil.Process(pid)
            if "cloudflared" in " ".join(p.cmdline()):
                p.kill()
        except Exception:
            pass

    def _spawn(self, exe):
        extra = {}
        if platform.system() == "Windows":
            extra["creationflags"] = subprocess.CREATE_NO_WINDOW
        elif platform.system() == "Linux":
            extra["preexec_fn"] = _die_with_parent
        self._proc = subprocess.Popen(
            [exe, "tunnel", "--no-autoupdate", "--url",
             f"http://127.0.0.1:{self.bridge_port}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, errors="replace", **extra)
        try:
            self._pid_file.write_text(str(self._proc.pid))
        except OSError:
            pass
        found = threading.Event()
        holder = {}

        def read(stream):
            for line in stream:
                self._log.append(line.strip())
                m = _URL_RE.search(line)
                if m and not found.is_set():
                    holder["url"] = m.group(0)
                    found.set()

        threading.Thread(target=read, args=(self._proc.stderr,),
                         daemon=True).start()
        # The address comes within seconds; give up after a minute.
        deadline = time.monotonic() + 60
        while not found.wait(1):
            if self._stop.is_set() or self._proc.poll() is not None \
                    or time.monotonic() > deadline:
                return None
        return holder["url"]

    def _publish(self, url):
        try:
            publish(self.rendezvous, url)
            return True
        except Exception as e:
            print(f"[agent] tunnel: could not post the address: {e}")
            return False

    def _loop(self):
        self._kill_leftover()
        backoff = 5
        while not self._stop.is_set():
            try:
                exe = ensure_cloudflared(self.folder)
            except Exception as e:
                self._set(None, f"cloudflared unavailable: {e}")
                self._stop.wait(300)
                continue
            url = self._spawn(exe)
            if url is None:
                detail = next((l for l in reversed(self._log)
                               if "ERR" in l or "error" in l.lower()),
                              "no address from cloudflared")
                self._kill()
                self._set(None, detail[-200:])
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 300)
                continue
            backoff = 5
            self._set(url, None)
            self._watch(url)
            self._kill()
            if not self._stop.is_set():
                self._set(None, "restarting the tunnel")
                self._stop.wait(backoff)
        self._kill()

    def _watch(self, url):
        """Until cloudflared exits or the address stops answering: keep the
        topic current and check the path through Cloudflare."""
        published_at = None
        next_check = time.monotonic() + self.FIRST_CHECK
        failures = 0
        while not self._stop.is_set() and self._proc.poll() is None:
            now = time.monotonic()
            if published_at is None or now - published_at > \
                    self.REPUBLISH_EVERY:
                if self._publish(url):
                    published_at = now
            if now >= next_check:
                if check(url):
                    failures = 0
                else:
                    failures += 1
                    print(f"[agent] tunnel: self-check failed ({failures})")
                    if failures >= self.CHECK_FAILURES:
                        return
                next_check = now + (self.CHECK_EVERY if failures == 0
                                    else 60)
            # Retry a failed post soon (network still coming up at boot).
            self._stop.wait(30 if published_at is None else 15)
