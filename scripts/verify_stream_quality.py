"""End-to-end screen streaming check against a RUNNING (test) agent.

For each config it pairs, sends `screen.start`, and measures what a phone
would get: frames per second, average KB per frame, resolution, the
effective quality/preset the agent reports, and the time to the first frame.
The `h264` config also validates the access-unit framing packet by packet
(exactly one picture per packet, `key` == has IDR, SPS/PPS before every
IDR, monotonic `ts`), concatenates the payloads into a .h264 file and
decodes it with `ffmpeg -v error` (must print nothing).

`--throttle` exercises the `auto` preset: a client that reads at a capped
rate (so the agent's sends back up), then reads freely again — the frame
headers must show quality/width stepping down and recovering.

Screen activity comes from a small animated window this script opens in a
corner of the primary monitor: click-through, never focused, topmost, and
gone when the script ends. No input is ever injected.

NEVER point this at the live agent on :8080 — run an isolated test agent.

Usage:
    python scripts/verify_stream_quality.py --port 8091
        [--configs legacy,saver,balanced,hd,max,auto,h264]
        [--seconds 8] [--throttle] [--no-animate] [--save-h264 out.h264]
"""

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
FFMPEG = ROOT / "tools" / "ffmpeg.exe"

# What each config sends with screen.start. `legacy` is exactly what the
# currently released app sends; `h264` is what the new app sends for the
# "Smooth video" choice.
CONFIGS = {
    "legacy": {"codec": "jpeg", "fps": 15, "quality": 65, "max_width": 1152,
               "binary": True},
    "saver": {"preset": "saver", "binary": True},
    "balanced": {"preset": "balanced", "binary": True},
    "hd": {"preset": "hd", "binary": True},
    "max": {"preset": "max", "binary": True},
    "auto": {"preset": "auto", "binary": True},
    "h264": {"codec": "h264", "preset": "smooth", "binary": True},
}


# ---------------------------------------------------------------- activity

def animate(seconds):
    """Child-process body: a small animated, click-through, non-activating
    topmost window so the stream has a changing screen to encode."""
    import ctypes
    import tkinter as tk

    user32 = ctypes.windll.user32
    prev_fg = user32.GetForegroundWindow()
    root = tk.Tk()
    root.withdraw()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    w, h = 360, 220
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{sw - w - 24}+{sh - h - 72}")
    root.update_idletasks()
    hwnd = user32.GetAncestor(root.winfo_id(), 2)  # GA_ROOT
    GWL_EXSTYLE = -20
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    # NOACTIVATE + TOOLWINDOW: never takes focus, no taskbar button;
    # LAYERED + TRANSPARENT: clicks fall through to whatever is below.
    ex |= 0x08000000 | 0x80 | 0x80000 | 0x20
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
    user32.SetLayeredWindowAttributes(hwnd, 0, 235, 2)  # LWA_ALPHA
    root.deiconify()
    root.update()
    if prev_fg and user32.GetForegroundWindow() != prev_fg:
        user32.SetForegroundWindow(prev_fg)  # belt and braces

    canvas = tk.Canvas(root, width=w, height=h, bg="#101820",
                       highlightthickness=0)
    canvas.pack()
    bars = [canvas.create_rectangle(0, 0, 0, 0, width=0) for _ in range(6)]
    text = canvas.create_text(12, 12, anchor="nw", fill="#e0e0e0",
                              font=("Consolas", 11))
    big = canvas.create_text(w // 2, h - 40, fill="#7fffd4",
                             font=("Segoe UI", 26, "bold"))
    t0 = time.monotonic()

    def tick():
        t = time.monotonic() - t0
        if t > seconds:
            root.destroy()
            return
        for i, bar in enumerate(bars):
            x = (t * (60 + 25 * i) + i * 50) % (w + 60) - 60
            canvas.coords(bar, x, 40 + i * 18, x + 60, 54 + i * 18)
            canvas.itemconfigure(
                bar, fill=f"#{(i * 40 + int(t * 90)) % 256:02x}80{200 - i * 25:02x}")
        canvas.itemconfigure(text, text="\n".join(
            f"line {int(t * 10) + k:06d}  stream quality test" for k in range(2)))
        canvas.itemconfigure(big, text=f"{t:7.2f}s")
        root.after(33, tick)

    tick()
    root.mainloop()


def start_animator(seconds):
    return subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--_animate",
         str(seconds)],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# ---------------------------------------------------------------- client

class Client:
    def __init__(self, url, max_queue=64, rcvbuf=None):
        self.url = url
        self.max_queue = max_queue
        self.rcvbuf = rcvbuf
        self.ws = None
        self.binary = []          # (monotonic ts, bytes)
        self.pending = {}
        self.next_id = 0
        self.reader = None
        self.read_delay = None    # callable(nbytes) -> seconds, for throttling

    async def __aenter__(self):
        sock = None
        if self.rcvbuf:
            # A small receive window, set before connecting so it is what
            # TCP negotiates: a slow reader then backs up the AGENT's send
            # queue, like a weak Wi-Fi link would, instead of piling
            # megabytes into this PC's loopback receive buffer.
            host, port = self.url.split("//")[1].rsplit(":", 1)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.rcvbuf)
            sock.connect((host, int(port)))
        self.ws = await websockets.connect(
            self.url, max_size=64 * 1024 * 1024, max_queue=self.max_queue,
            sock=sock)
        await self.ws.send(json.dumps(
            {"type": "pair", "name": "verify_stream_quality"}))
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), 30))
            if msg.get("type") == "paired":
                break
            if msg.get("type") == "error":
                raise RuntimeError(msg)
        self.reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *exc):
        self.reader.cancel()
        await self.ws.close()

    async def _read(self):
        async for raw in self.ws:
            if isinstance(raw, bytes):
                self.binary.append((time.monotonic(), raw))
                if self.read_delay:
                    await asyncio.sleep(self.read_delay(len(raw)))
                continue
            msg = json.loads(raw)
            fut = self.pending.pop(msg.get("id"), None)
            if fut:
                fut.set_result(msg)

    async def request(self, cmd, **params):
        self.next_id += 1
        rid = f"v{self.next_id}"
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        await self.ws.send(json.dumps({"id": rid, "cmd": cmd, **params}))
        msg = await asyncio.wait_for(fut, 30)
        if not msg.get("ok"):
            raise RuntimeError(f"{cmd}: {msg.get('error')}")
        return msg.get("data", {})


def parse_packet(raw):
    hlen = int.from_bytes(raw[4:8], "little")
    return raw[:4], json.loads(raw[8:8 + hlen]), raw[8 + hlen:]


# ---------------------------------------------------------------- h264

def nal_units(au):
    """Split an Annex-B buffer into NAL unit payloads (without start codes)."""
    out = []
    i, n = 0, len(au)
    starts = []
    while i < n - 2:
        if au[i] == 0 and au[i + 1] == 0 and au[i + 2] == 1:
            starts.append(i + 3)
            i += 3
        else:
            i += 1
    for k, s in enumerate(starts):
        end = starts[k + 1] - 3 if k + 1 < len(starts) else n
        # a 4-byte start code leaves one extra zero before the next unit
        while end > s and au[end - 1] == 0 and k + 1 < len(starts):
            end -= 1
        out.append(au[s:end])
    return out


def check_au(header, au):
    """Problems with one H264 packet (empty list = valid access unit)."""
    problems = []
    if not (au.startswith(b"\x00\x00\x00\x01") or au.startswith(b"\x00\x00\x01")):
        problems.append("does not start with a start code")
    nals = nal_units(au)
    types = [x[0] & 0x1F for x in nals if x]
    # A new picture starts at a slice whose first_mb_in_slice is 0
    # (ue(v) '1' => top bit of the byte after the NAL header is set).
    pictures = sum(1 for x in nals if x and (x[0] & 0x1F) in (1, 5)
                   and len(x) > 1 and x[1] & 0x80)
    if pictures != 1:
        problems.append(f"{pictures} pictures in one packet")
    has_idr = 5 in types
    if bool(header.get("key")) != has_idr:
        problems.append(f"key={header.get('key')} but IDR present={has_idr}")
    if has_idr:
        first_slice = types.index(5)
        if 7 not in types[:first_slice] or 8 not in types[:first_slice]:
            problems.append("IDR without SPS/PPS in front")
    if types.count(9) > 1:
        problems.append("more than one AUD")
    if 9 in types and types[0] != 9:
        problems.append("AUD is not the first NAL unit")
    return problems, types


def ffmpeg_check(path):
    err = subprocess.run(
        [str(FFMPEG), "-hide_banner", "-v", "error", "-i", str(path),
         "-f", "null", "-"], capture_output=True, text=True).stderr.strip()
    info = subprocess.run(
        [str(FFMPEG), "-hide_banner", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True).stderr
    frames = 0
    for tok in info.replace("\r", "\n").split("frame=")[1:]:
        try:
            frames = int(tok.split()[0])
        except (ValueError, IndexError):
            pass
    return err, frames


# ---------------------------------------------------------------- runs

async def measure(url, name, params, seconds, warmup=1.5, save_h264=None):
    async with Client(url) as c:
        t_req = time.monotonic()
        resp = await c.request("screen.start", **params)
        await asyncio.sleep(warmup)
        mark = len(c.binary)
        t_mark = time.monotonic()
        await asyncio.sleep(seconds)
        t_end = time.monotonic()
        await c.request("screen.stop")
        packets = c.binary[mark:]
        first = c.binary[0][0] - t_req if c.binary else None
        all_packets = list(c.binary)
    span = t_end - t_mark
    row = {"config": name, "resp": resp, "frames": len(packets),
           "fps": len(packets) / span, "first_ms": first and first * 1000}
    if not packets:
        return row
    sizes, dims, qs, presets = [], set(), set(), set()
    for _, raw in packets:
        magic, header, payload = parse_packet(raw)
        sizes.append(len(payload))
        dims.add(f"{header.get('w')}x{header.get('h')}")
        if "q" in header:
            qs.add(header["q"])
        if "preset" in header:
            presets.add(header["preset"])
    row.update(kb_frame=sum(sizes) / len(sizes) / 1024,
               kbps=sum(sizes) / span / 1024, dims=sorted(dims),
               q=sorted(qs), presets=sorted(presets), magic=magic.decode())
    if magic == b"H264":
        row.update(h264_report(all_packets, save_h264))
    return row


def h264_report(packets, save_path):
    bad, keys, last_ts, ts_back = [], 0, None, 0
    encoders, captures = set(), set()
    blob = bytearray()
    for i, (_, raw) in enumerate(packets):
        _, header, au = parse_packet(raw)
        encoders.add(header.get("encoder"))
        captures.add(header.get("capture"))
        problems, _ = check_au(header, au)
        if problems:
            bad.append((i, problems))
        keys += bool(header.get("key"))
        ts = header.get("ts")
        if last_ts is not None and ts is not None and ts < last_ts:
            ts_back += 1
        last_ts = ts
        blob += au
    path = Path(save_path) if save_path else Path(
        tempfile.gettempdir()) / "verify_stream_quality.h264"
    path.write_bytes(bytes(blob))
    err, decoded = ffmpeg_check(path)
    first_ts = parse_packet(packets[0][1])[1].get("ts", 0)
    span_s = max(0.001, ((last_ts or 0) - first_ts) / 1000)
    return {"aus": len(packets), "bad_aus": bad[:5], "bad_count": len(bad),
            "keyframes": keys, "ts_backwards": ts_back,
            "encoder": sorted(e for e in encoders if e),
            "capture": sorted(c for c in captures if c),
            "decode_errors": err, "decoded_frames": decoded,
            "stream_fps": (len(packets) - 1) / span_s,
            "mbps": len(blob) * 8 / span_s / 1e6, "file": str(path)}


async def throttle_test(url, seconds):
    """auto preset under a capped read rate, then free again."""
    timeline = []
    async with Client(url, max_queue=2, rcvbuf=64 * 1024) as c:
        cap = 150 * 1024  # bytes/s the "slow link" delivers
        c.read_delay = lambda n: n / cap
        resp = await c.request("screen.start", preset="auto", binary=True)
        t0 = time.monotonic()
        phase_end = t0 + seconds
        while time.monotonic() < phase_end + seconds:
            if c.read_delay and time.monotonic() >= phase_end:
                c.read_delay = None  # link recovers
            await asyncio.sleep(0.25)
        await c.request("screen.stop")
        for ts, raw in c.binary:
            _, header, payload = parse_packet(raw)
            timeline.append((ts - t0, header.get("q"), header.get("w"),
                             len(payload)))
    return resp, timeline


def print_row(r):
    if not r["frames"]:
        print(f"{r['config']:9}  NO FRAMES  resp={r['resp']}")
        return
    print(f"{r['config']:9} {r['fps']:6.1f} {r['kb_frame']:8.1f} "
          f"{r['kbps']:8.0f}  {','.join(r['dims']):12} "
          f"q={','.join(map(str, r['q'])) or '-':10} "
          f"preset={','.join(r['presets']) or '-':9} "
          f"first={r['first_ms'] or 0:5.0f}ms")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--configs", default="legacy,saver,balanced,hd,max,auto,h264")
    ap.add_argument("--seconds", type=float, default=8)
    ap.add_argument("--throttle", action="store_true")
    ap.add_argument("--no-animate", action="store_true")
    ap.add_argument("--save-h264")
    args = ap.parse_args()
    if args.port == 8080:
        sys.exit("refusing to touch the live agent on :8080")
    url = f"ws://{args.host}:{args.port}"
    names = [n.strip() for n in args.configs.split(",") if n.strip()]
    total = len(names) * (args.seconds + 4) + (
        2 * args.seconds + 6 if args.throttle else 0) + 10
    anim = None if args.no_animate else start_animator(total)
    rows = []
    try:
        await asyncio.sleep(1.0)  # let the window appear
        print(f"{'config':9} {'fps':>6} {'KB/frame':>8} {'KB/s':>8}  "
              f"{'resolution':12} {'quality':12} {'preset':16} first-frame")
        for name in names:
            r = await measure(url, name, CONFIGS[name], args.seconds,
                              save_h264=args.save_h264)
            rows.append(r)
            print_row(r)
            await asyncio.sleep(0.5)
        for r in rows:
            print(f"  {r['config']} screen.start -> {r['resp']}")
            if "aus" in r:
                print(f"  h264: {r['aus']} AUs, {r['bad_count']} malformed "
                      f"{r['bad_aus']}, {r['keyframes']} keyframes, "
                      f"ts backwards {r['ts_backwards']}, encoder "
                      f"{r['encoder']}, capture {r['capture']}, "
                      f"{r['stream_fps']:.1f} fps, {r['mbps']:.2f} Mbit/s")
                print(f"  ffmpeg -v error decode of {r['file']}: "
                      f"{'OK (no errors)' if not r['decode_errors'] else r['decode_errors'][:500]}"
                      f", {r['decoded_frames']} frames decoded")
        if args.throttle:
            resp, timeline = await throttle_test(url, args.seconds)
            print(f"\nauto under a 150 KB/s link for {args.seconds:.0f}s, "
                  f"then unthrottled for {args.seconds:.0f}s: {resp}")
            for sec in range(int(2 * args.seconds) + 1):
                window = [t for t in timeline if sec <= t[0] < sec + 1]
                if window:
                    qs = sorted({t[1] for t in window})
                    ws_ = sorted({t[2] for t in window})
                    kb = sum(t[3] for t in window) / 1024
                    print(f"  t={sec:3d}s frames={len(window):3d} "
                          f"q={qs} w={ws_} {kb:7.1f} KB")
    finally:
        if anim:
            anim.kill()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--_animate":
        animate(float(sys.argv[2]))
    else:
        asyncio.run(main())
