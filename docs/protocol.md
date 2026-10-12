# pc-remote wire protocol

All messages are JSON text frames over WebSocket. There are two connection
roles on the relay: **agent** (the PC) and **client** (the phone).

## 1. Handshake

The first message a socket sends decides its role.

Agent registers (persistent, auto-reconnecting):

```json
{ "type": "register", "code": "ABC123", "deviceId": "uuid", "name": "DESKTOP-1" }
```

Relay replies: `{ "type": "registered", "code": "ABC123" }`

Client pairs:

```json
{ "type": "pair", "code": "ABC123" }
```

Relay replies on success:

```json
{ "type": "paired", "deviceId": "uuid", "name": "DESKTOP-1", "clientId": "uuid" }
```

or `{ "type": "error", "error": "no online device for that code" }`.

### QR pairing payload

The agent displays a QR code (terminal + `pairing-qr.png`) containing:

```
pcocket://pair?relay=<urlencoded ws(s)://host:port>&code=ABC123[&lan=<urlencoded ws://lan-ip:8080>]
```

When the configured relay is loopback, the agent substitutes the machine's
LAN address so phones on the same network can reach it. When the relay is
remote (tunnel/hosted), the optional `lan` parameter carries the direct LAN
address of the relay: the app should try `lan` first (short timeout) and
fall back to `relay`, so at home the connection skips the internet
round-trip. Scanning the QR in the app should prefill relay + code and pair
immediately.

Relay also pushes `{ "type": "agent.online" | "agent.offline" }` to paired
clients when the PC connects/disconnects.

## 2. Requests (client -> agent)

After pairing, the client sends commands. The relay transparently adds a
`from` field (the client id) before forwarding to the agent.

```json
{ "id": "req-1", "cmd": "<command>", ...params }
```

`id` is client-generated; omit it for fire-and-forget messages (e.g. input
events during screen streaming).

Ordering: a client's commands run one at a time, in the order sent —
except input and stream control (`ping`, `input`, `media`, `phone.touch`,
`phone.text`, `phone.key`, `phone.wheel`, `screen.start`, `screen.stop`,
`screen.ack`), which run the moment they arrive, so a slow request
(`media.info`, `wm.list`...) never holds up the cursor or the stream. A client
that needs input to follow a request must wait for that request's reply.

Clients should not offer `permessage-deflate`, and the embedded relay
declines it: frames are JPEG/H.264/PCM, which don't compress.

## 3. Responses and events (agent -> client)

Responses are routed to the requesting client:

```json
{ "id": "req-1", "ok": true, "data": { ... } }
{ "id": "req-1", "ok": false, "error": "message" }
```

Events are pushed without an `id`:

```json
{ "event": "screen.frame", "data": { "jpeg": "<base64>", "w": 1280, "h": 720 } }
{ "event": "clipboard.changed", "data": { "text": "..." } }
```

## 4. Commands

| cmd | params | data on success |
|---|---|---|
| `ping` | – | `{ pong, ts }` |
| `sys.info` | – | `{ name, platform, cpu_percent, mem_total, mem_used, uptime, screen:{w,h} }` |
| `sys.exec` | `command, cwd?, timeout?` (s, default 30) | `{ stdout, stderr, code }` |
| `sys.ps` | – | `{ processes: [{pid, name, mem}] }` (top 200 by memory) |
| `sys.kill` | `pid` | `{ killed }` |
| `fs.list` | `path?` (empty = drives/root) | `{ path, entries: [{name, path, type: "dir"\|"file"\|"drive", size, mtime}] }` |
| `fs.shortcuts` | – | `{ shortcuts: [{name, path}] }` – Home, Desktop, Documents, Downloads, ..., Drives (`path: ""`) |
| `fs.download` | `path, offset?, length?` (<=256KB) | `{ size, offset, data: base64, eof }` |
| `fs.upload` | `path, data: base64, offset?, append?` | `{ path, size }` |
| `fs.delete` | `path` (file or dir, recursive) | `{ deleted }` |
| `fs.mkdir` | `path` | `{ path }` |
| `power` | `action`: `lock\|shutdown\|restart\|sleep\|hibernate` | `{ action, scheduled }` – runs now; a `shutdown`/`restart` also replaces any scheduled action |
| `power.schedule` | `action`: `shutdown\|restart\|sleep\|hibernate\|lock`, `seconds` (60 – 604800 = 7 days) | power status (below) – replaces any earlier schedule; broadcasts `power.schedule` |
| `power.cancel` | – | power status (`{ scheduled: false }`) – broadcasts `power.schedule` |
| `power.status` | – | `{ scheduled, action?, at?, remaining_s? }` – `at` in unix seconds |
| `display.brightness.get` | – | `{ supported, level, displays: [{id, name, level, method: "wmi"\|"ddc"}] }` – `level` (0-100) is the first display's, `null` when none is controllable |
| `display.brightness.set` | `level?` (0-100) or `delta?` (±), `display?` (a `displays[].id`; omitted = all) | same shape as `.get`, with the new levels |
| `media` | `action`: `media_play_pause\|media_next\|media_previous\|media_volume_up\|media_volume_down\|media_volume_mute` | `{}` |
| `clipboard.get` | – | `{ text }` |
| `clipboard.set` | `text` | `{ text }` |
| `clipboard.watch` | `enabled: bool` | `{ watching }` – when on, agent broadcasts `clipboard.changed` events |
| `screen.start` | see [`screen.start`](#screenstart) below | `{ streaming, codec, binary, preset, fps, quality, max_width, subsampling, adaptive, w, h, monitor }` (H.264: `bitrate` instead of `quality`/`subsampling`/`adaptive`) – then frames stream to this client |
| `screen.stop` | – | `{ streaming: false }` |
| `screen.ack` | – (send without `id`) | – one received frame, for a stream started with `ack: true` |
| `camera.start` | `fps?` (dflt 10), `max_width?` (dflt 640) | `{ streaming: true }` – webcam frames stream as PJF1 packets |
| `camera.stop` | – | `{ streaming: false }` |
| `wm.list` | `icons?` (dflt true) | `{ windows: [{id, title, pid, process, exe, active, minimized, maximized, monitor, rect:{left,top,width,height}, icon}] }` – top-level windows, what Alt-Tab would show, in Z-order (most recently used first). See below (Windows only) |
| `wm.thumb` | `window`, `max_width?` (64-1280, dflt 360) | `{ jpeg: base64 \| null, w, h }` – live thumbnail, also of covered windows; `null` for minimized/hung windows (`w`/`h` then give the restored size) |
| `wm.activate` | `window` | `{ activated: true }` – restores (if minimized) and brings that window to the foreground |
| `wm.close` | `window` | `{ close_requested: true }` – posts `WM_CLOSE`, like clicking X: the app may ask to save or refuse |
| `wm.minimize` / `wm.maximize` / `wm.restore` | `window` | `{ minimized \| maximized \| restored: true }` |
| `apps.list` | `refresh?` (dflt false) | `{ apps: [{id, name, icon?}] }` – Start menu "All apps" (Win32 shortcuts and Store apps), sorted by name, cached 10 min (loaded in the background on the first `wm.list`; an expired list is answered at once while it reloads; `refresh: true` waits for a fresh one); `icon` only when already cached |
| `apps.icon` | `app` (an `apps.list` id) | `{ id, icon: base64 PNG \| null }` – 48px icon, fetched lazily per row |
| `apps.launch` | `app` (an `apps.list` id) | `{ launched: true, id, name }` – starts the app (`shell:AppsFolder\<id>`) and brings its first new window to the front; ids not in the list are rejected |
| `input` | see below | `{}` |
| `notify.subscribe` | – | currently returns `ok:false` (not implemented yet) |

### `screen.start`

| param | values | default |
|---|---|---|
| `codec` | `"jpeg"` \| `"h264"` | `"jpeg"` (`"h264"` when `preset` is `"smooth"`) |
| `preset` | `auto` \| `saver` \| `balanced` \| `hd` \| `max` (JPEG), `smooth` (H.264) | none = the legacy defaults below |
| `fps` | JPEG 1-60, H.264 5-60 | from the preset |
| `quality` | JPEG quality 20-95 | from the preset |
| `max_width` | 320-3840, never above the monitor's native width (no upscaling) | from the preset |
| `subsampling` | `"420"` \| `"422"` \| `"444"` (chroma; JPEG only) | from the preset |
| `bitrate` | H.264 bits/s, 0.5-20 M | 6 000 000 |
| `monitor` | `"primary"` or an index into the agent's monitor list (`0` = all monitors, `1..n` = each one) | `"primary"` |
| `binary` | PJF1 binary frames instead of base64 events | `false` |
| `ack` | flow control (JPEG): the client sends `{"cmd": "screen.ack"}` (no `id`) for every frame it receives, and the agent never has more than 2 frames out unacked — frames can't queue up on a slow link. Answered as `ack: true` by agents that support it; older agents ignore it (then send no acks) | `false` |

Explicit `fps` / `quality` / `max_width` / `subsampling` override the
preset's values. With **no preset and no params** the agent uses the
pre-preset defaults (10 fps, quality 50, 960 wide, 4:2:0), so old apps get
exactly what they always got.

| preset | width | quality | fps | chroma |
|---|---|---|---|---|
| `saver` | 960 | 55 | 12 | 4:2:0 |
| `balanced` | 1280 | 70 | 20 | 4:2:0 |
| `hd` | native | 80 | 24 | 4:2:0 |
| `max` | native | 90 | 30 | 4:4:4 (crisp coloured text) |
| `auto` | starts at 1280 | starts at 70 | 20 | 4:2:0 |

`auto` adapts per client: when that phone's link backs up (the stream
spent over 60% of the last 2 s blocked sending to it, or its socket is
congested) it steps down a rung of 1280/q70 → 1120/q62 → 960/q55 →
800/q50 → 640/q45, at most one rung per second, and back up one rung
after ~6 s (and 20+ frames) under 25%. Pinning `quality` or `max_width`
alongside `auto` turns the adaptation off (reported as preset `custom`).

`"primary"` is the monitor whose rect starts at (0,0). The monitor list is
re-queried on every `screen.start` (displays can be added at runtime), so
indices follow the PC's current layout; an unknown index fails the request
without touching a running stream. The response's `monitor` is the
captured rect `{left, top, width, height}` in desktop pixels, and `w`/`h`
the size of the frames being sent.

### `input` actions

All pointer coordinates are **normalized 0..1** relative to the monitor
this client is streaming (`screen.start`'s `monitor`; the primary monitor
when it isn't streaming); the agent maps them to pixels.

```json
{ "cmd": "input", "action": "move",   "x": 0.5, "y": 0.5 }
{ "cmd": "input", "action": "move_rel", "dx": 0.02, "dy": -0.01 }
{ "cmd": "input", "action": "click",  "button": "left|right|middle", "x": 0.5, "y": 0.5, "count": 1 }
{ "cmd": "input", "action": "down",   "button": "left", "x": 0.5, "y": 0.5 }
{ "cmd": "input", "action": "up",     "button": "left", "x": 0.5, "y": 0.5 }
{ "cmd": "input", "action": "scroll", "dx": 0, "dy": -3 }
{ "cmd": "input", "action": "key",    "key": "enter" }        // named key or single char
{ "cmd": "input", "action": "text",   "text": "hello world" } // type a string
```

`move_rel` takes normalized **deltas** (fraction of that same monitor's
size) for touchpad-style control. `click`/`down`/`up` may omit `x`/`y` to
act at the PC cursor's current position — this is how "tap to click where
the cursor already is" is implemented.

The agent draws the PC cursor onto every `screen.frame` image itself, so the
phone always sees the real cursor position without any extra message.

Named keys: `enter backspace tab esc space up down left right delete home end
pageup pagedown shift ctrl alt cmd f1..f12` plus the `media_*` keys.

### Windows and apps (`wm.*`, `apps.*`)

The target of a `wm.*` command rides in **`window`** (a `wm.list` id) and
that of `apps.icon`/`apps.launch` in **`app`** (an `apps.list` id) — never
in `id`, which is the request-envelope key: a target sent as `id` would
overwrite the request id and the reply could not be matched. (Agents still
accept a numeric `id` as the window of `wm.activate` from older apps.)

`wm.list` window fields:

| field | meaning |
|---|---|
| `id` | window handle (HWND) — pass it back as `window` |
| `title`, `pid`, `process`, `exe` | window title, owning process, its exe name and full path (`exe` empty if inaccessible). Store apps hosted by `ApplicationFrameHost.exe` report the real app process |
| `active` | it is the foreground window |
| `minimized`, `maximized` | window state |
| `monitor` | 1-based monitor index in the same order as the agent's screen capture (`mss().monitors`) |
| `rect` | `{left, top, width, height}` of the visible frame in physical screen pixels; for a minimized window, where it will restore to (outer rect) |
| `icon` | base64 48x48 RGBA PNG of the app icon, or `null`; cached per app on the PC. Omitted with `icons: false` — a periodic refresh can skip the PNGs it already has |

### Display brightness

The agent drives two backends: built-in laptop panels through WMI
(`method: "wmi"`) and external monitors through DDC/CI (`method: "ddc"`).
A display that answers neither (DDC/CI switched off in the monitor's menu,
most TVs) is not listed; with no display listed, `supported` is `false`.

```json
{ "cmd": "display.brightness.get" }
{ "cmd": "display.brightness.set", "level": 40 }
{ "cmd": "display.brightness.set", "delta": -10, "display": "ddc:\\\\.\\DISPLAY2#0" }
```

`level` wins when both are given; values clamp to 0-100. The target is
`display`, **not** `id` — `id` is the request id of the envelope. Display
ids are opaque strings: pass back what `.get` returned.

One change costs the PC a fraction of a second, so the agent **coalesces**:
only one change runs at a time, and everything requested meanwhile is merged
into a single follow-up change (the latest `level` wins, `delta`s add up).
A slider can therefore send every ~150 ms while dragging (plus one final
send on release); intermediate replies may already reflect a newer value
than the request they answer.

### Scheduled power actions

```json
{ "cmd": "power.schedule", "action": "shutdown", "seconds": 3600 }
{ "cmd": "power.cancel" }
{ "cmd": "power.status" }
```

The PC owns the schedule: one at a time (a new `power.schedule` replaces the
old one), kept across agent restarts, and every change is broadcast to all
connected phones so they agree:

```json
{ "event": "power.schedule", "data": { "scheduled": true, "action": "shutdown", "at": 1791482107.219, "remaining_s": 3600 } }
{ "event": "power.schedule", "data": { "scheduled": false } }
{ "event": "power.schedule", "data": { "scheduled": false, "fired": "shutdown" } }
```

The `data` is the same status `power.status` returns, plus, when the
schedule ended on its own, `fired: "<action>"` (it ran), `missed:
"<action>"` (the PC slept through the deadline, so it was dropped instead
of running late) or `cancelled_by: "pc"` (Cancel was pressed on the PC).
Phones should render the countdown from `remaining_s` / `at` and re-sync
with `power.status` after reconnecting.

On Windows, `shutdown`/`restart` are also handed to Windows' own timer
(`shutdown /s|/r /t N`), so they still happen if the agent dies, and Windows
shows its own notice; `power.cancel` always aborts that timer
(`shutdown /a`). In standalone mode the PC also shows a small always-on-top
"Shutting down in 60s — Cancel" notice a minute before any action.

## 5. Typical flows

**Screen streaming:** `screen.start` (with `id`) -> `ok` response, then a
stream of frames until `screen.stop`. Send `input` messages without `id`
while streaming. With `binary: true` frames arrive as **binary WebSocket
packets** instead of JSON events (about 33% smaller, no base64 cost):

```
[4 bytes]  "PJF1" magic
[4 bytes]  header length N, little-endian uint32
[N bytes]  header JSON: {"w": 1280, "h": 720, "src": "screen", "q": 70, "preset": "auto"}
[rest]     JPEG bytes
```

`src` is `"screen"` or `"camera"` — route frames to the right view. Screen
frames also carry the effective JPEG quality `q` and the `preset` (`auto`'s
`q` and `w`/`h` change as it adapts); older agents send only `w`/`h`/`src`,
and clients must ignore keys they don't know.

Binary packets go only to the phone that started the stream (with the
embedded relay; the hosted relay still fans them out), and each phone's
socket is throttled on its own — a slow phone drops its own frames, never
another's.

With `binary: false` (or absent) frames arrive as legacy
`{event:"screen.frame", data:{jpeg: base64, w, h}}` events. In both modes the
agent **skips frames identical to the previous one**, so an idle desktop uses
almost no bandwidth — expect gaps between frames when nothing moves.

**H.264 (`codec: "h264"`, "smooth video"):** binary packets with an `H264`
magic, each carrying exactly **one access unit**:

```
[4 bytes]  "H264" magic
[4 bytes]  header length N, little-endian uint32
[N bytes]  header JSON: {"w": 1920, "h": 1080, "codec": "h264", "encoder": "libx264",
                         "capture": "gfxcapture", "fps": 30, "key": true, "ts": 4033}
[rest]     one access unit, Annex-B: AUD, then (key units) SPS + PPS, then the slices
```

- `key` marks an IDR: decoding can only (re)start there, and every IDR is
  preceded by SPS/PPS inside its own unit. Feed one unit per decoder input
  buffer, with `ts` (capture time in ms since the stream started) as its
  presentation time; drop units until the first `key` after a (re)start.
- No B-frames; about one keyframe every 2 s (plus scene cuts).
- Under congestion the agent drops **whole** units until the next keyframe,
  never part of one — the stream resumes cleanly, just with a gap.
- `encoder` is `h264_nvenc`, `h264_qsv` or `libx264` (first that works);
  `capture` is `ddagrab`, `gfxcapture` or `gdigrab` (cheapest that works).
- If no capture/encoder combination works the agent sends
  `{event: "screen.error", data: {error}}`; clients should fall back to JPEG.

**File download:** repeat `fs.download` with increasing `offset`
(`offset += length` of decoded bytes) until `eof: true`.

**Clipboard sync both ways:** send `clipboard.watch {enabled:true}` once after
pairing; incoming `clipboard.changed` events carry PC->phone text; phone->PC
is `clipboard.set`.

### Phone screen — the phone as a second monitor (`phone.*`)

Puts one or more PC apps (Discord, say) on a **virtual monitor only the
phone sees**, while the PC's own screen keeps showing whatever the PC user
is doing. The monitor is created on demand by the Parsec Virtual Display
Driver (a one-time, user-approved install); the agent keeps it alive with a
keepalive, so if the agent dies it unplugs within a second and Windows moves
the apps back to the real screen. It is also turned off automatically two
minutes after the last phone disconnects.

| cmd | params | data on success |
|---|---|---|
| `phone.status` | – | `{driver: "installed"\|"missing"\|"error", enabled, monitor?: {index, left, top, width, height}, settings: {size, orientation, scale, return_focus}, windows: [{id, title, process}], setup?: {stage, error?}}` |
| `phone.setup` | – | `{started: true}` — downloads Parsec's signed installer, verifies its signature and runs it elevated (approve the Windows prompt **on the PC**); progress arrives as `phone.setup` events |
| `phone.enable` | `size?` (`fhd`\|`hdplus`\|`hd`), `orientation?` (`portrait`\|`landscape`), `scale?` (100-200 %) | status |
| `phone.disable` | – | status — every app goes back to the PC's screen, the monitor is unplugged |
| `phone.settings` | any of `size`, `orientation`, `scale`, `return_focus` | status |
| `phone.move` / `phone.return` | `window` (a `wm.list` id) | status — moves that window onto / off the phone screen (`phone.move` turns the screen on if needed) |
| `phone.touch` | `action: down\|move\|up\|cancel`, `pointer` (int), `x`, `y` normalized 0..1 inside the phone screen | – (fire-and-forget) |
| `phone.text` / `phone.key` | `text` / `key` | `{}` — typed into the app on the phone screen the phone last touched |
| `phone.wheel` | `x`, `y`, `dx`, `dy` | `{}` |

The phone streams the monitor with `screen.start {monitor: "phone", ...}`.
Taps are injected as **touch** input, so the PC's mouse cursor is restored
right after a gesture, and with `return_focus` (default on) the window the
PC user was using gets the focus back after every tap. A ~16 Hz cursor fence
keeps the PC user's own mouse off the invisible monitor.

Events: `{event: "phone.status", data: <status>}` after every change, and
`{event: "phone.setup", data: {stage: "downloading"|"verifying"|"waiting_for_approval"|"installing"|"done"|"error", error?}}`
during the install.
