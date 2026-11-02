"""Smoke test of a built Windows exe (Sudo-Service.exe or Sudo-Agent.exe).

Starts the exe from its own folder with a config.json that skips the PC's
Allow/Deny prompt and remote access, then acts like a phone: listens for the
"I'm here" beacon, pairs on ws://127.0.0.1:8080, and checks ping, sys.info,
fs.list, pointer moves, a few JPEG screen frames and PC sound packets. The
exe is killed at the end. Run by
.github/workflows/windows-exes.yml on every build.

Usage: python -I scripts/test_windows_exe.py dist/Sudo-Service.exe
"""

import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import websockets

PORT = 8080
DISCOVERY_PORT = 47809

passed, failed = 0, 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL  {name} {extra}")


def wait_beacon(timeout):
    """The agent broadcasts SUDO|<name>|<port> once per second."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", DISCOVERY_PORT))
    sock.settimeout(1.0)
    end = time.monotonic() + timeout
    try:
        while time.monotonic() < end:
            try:
                data, _ = sock.recvfrom(1024)
            except socket.timeout:
                continue
            text = data.decode("utf-8", "replace")
            if text.startswith("SUDO|"):
                return text
    finally:
        sock.close()
    return None


async def connect(timeout):
    end = time.monotonic() + timeout
    while True:
        try:
            return await websockets.connect(
                f"ws://127.0.0.1:{PORT}", max_size=16 * 1024 * 1024)
        except OSError:
            if time.monotonic() > end:
                raise
            await asyncio.sleep(1)


async def phone():
    ws = await connect(60)
    req_id = 0
    pending = {}
    frames = []
    audio_packets = 0
    async with ws:
        await ws.send(json.dumps({"type": "pair", "name": "CI phone"}))
        hello = json.loads(await asyncio.wait_for(ws.recv(), 15))
        check("pair", hello.get("type") == "paired", f"({hello})")
        if hello.get("type") != "paired":
            return

        async def request(cmd, **params):
            nonlocal req_id
            req_id += 1
            rid = f"t{req_id}"
            fut = asyncio.get_running_loop().create_future()
            pending[rid] = fut
            await ws.send(json.dumps({"id": rid, "cmd": cmd, **params}))
            return await asyncio.wait_for(fut, timeout=20)

        async def receiver():
            nonlocal audio_packets
            async for raw in ws:
                if isinstance(raw, bytes):
                    audio_packets += raw.startswith(b"OPS1")
                    continue
                msg = json.loads(raw)
                rid = msg.get("id")
                if rid and rid in pending:
                    pending.pop(rid).set_result(msg)
                elif msg.get("event") == "screen.frame":
                    frames.append(msg["data"])

        rt = asyncio.create_task(receiver())
        try:
            r = await request("ping")
            check("ping", r.get("ok") and r["data"].get("pong"))
            r = await request("sys.info")
            d = r.get("data", {})
            check("sys.info", r.get("ok") and "screen" in d,
                  f"({d.get('platform')}, {d.get('screen')})")
            r = await request("fs.list", path="")
            check("fs.list drives", r.get("ok"),
                  f"({len(r.get('data', {}).get('entries', []))} drives)")
            r = await request("screen.start", fps=5, max_width=640,
                              quality=40)
            check("screen.start", r.get("ok") and r["data"].get("streaming"),
                  "" if r.get("ok") else f"({r})")
            # Frames identical to the last one are skipped, and a CI desktop
            # never changes: move the pointer (it counts as a change), which
            # also checks input from the phone.
            moved = 0
            for i in range(8):
                r = await request("input", action="move", x=0.2 + 0.08 * i,
                                  y=0.5)
                moved += bool(r.get("ok"))
                await asyncio.sleep(0.5)
            check("pointer moves", moved == 8, f"({moved}/8)")
            check("screen frames", len(frames) >= 3,
                  f"({len(frames)} frames, {frames[0]['w']}x{frames[0]['h']})"
                  if frames else "(none)")
            r = await request("screen.stop")
            check("screen.stop", r.get("ok"))
            # PC sound on the phone: opus.dll and the loopback in the exe.
            r = await request("audio.start", mute_pc=True)
            check("audio.start", r.get("ok"),
                  "" if r.get("ok") else f"({r.get('error')})")
            await asyncio.sleep(2)
            r = await request("audio.stop")
            check("audio.stop", r.get("ok"))
            check("audio packets", audio_packets >= 80,
                  f"({audio_packets} in 2 s)")
        finally:
            rt.cancel()


def main():
    exe = Path(sys.argv[1]).resolve()
    (exe.parent / "config.json").write_text(json.dumps(
        {"require_approval": False, "remote_access": False}))
    print(f"[test] starting {exe.name}")
    proc = subprocess.Popen([str(exe)], cwd=exe.parent)
    try:
        # A onefile exe unpacks itself first: give it time.
        beacon = wait_beacon(60)
        check("beacon", beacon is not None, f"({beacon})")
        check("still running", proc.poll() is None,
              f"(exit code {proc.returncode})")
        asyncio.run(phone())
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
