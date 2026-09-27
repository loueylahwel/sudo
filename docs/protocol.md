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
| `power` | `action`: `lock\|shutdown\|restart\|sleep` | `{ action, scheduled }` |
| `media` | `action`: `media_play_pause\|media_next\|media_previous\|media_volume_up\|media_volume_down\|media_volume_mute` | `{}` |
| `clipboard.get` | – | `{ text }` |
| `clipboard.set` | `text` | `{ text }` |
| `clipboard.watch` | `enabled: bool` | `{ watching }` – when on, agent broadcasts `clipboard.changed` events |
| `screen.start` | `fps?` (1-20, dflt 10), `quality?` (20-90, dflt 50), `max_width?` (dflt 960), `binary?` (dflt false) | `{ streaming, fps, binary }` – then frames stream to this client |
| `screen.stop` | – | `{ streaming: false }` |
| `camera.start` | `fps?` (dflt 10), `max_width?` (dflt 640) | `{ streaming: true }` – webcam frames stream as PJF1 packets |
| `camera.stop` | – | `{ streaming: false }` |
| `wm.list` | – | `{ windows: [{id, title, pid, process}] }` – top-level windows, what Alt-Tab would show (Windows only) |
| `wm.activate` | `id` (a `wm.list` window id) | `{ activated: true }` – brings that window to the foreground |
| `input` | see below | `{}` |
| `notify.subscribe` | – | currently returns `ok:false` (not implemented yet) |

### `input` actions

All pointer coordinates are **normalized 0..1** relative to the primary
monitor; the agent maps them to pixels.

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

`move_rel` takes normalized **deltas** (fraction of screen size) for
touchpad-style control. `click`/`down`/`up` may omit `x`/`y` to act at the
PC cursor's current position — this is how "tap to click where the cursor
already is" is implemented.

The agent draws the PC cursor onto every `screen.frame` image itself, so the
phone always sees the real cursor position without any extra message.

Named keys: `enter backspace tab esc space up down left right delete home end
pageup pagedown shift ctrl alt cmd f1..f12` plus the `media_*` keys.

## 5. Typical flows

**Screen streaming:** `screen.start` (with `id`) -> `ok` response, then a
stream of frames until `screen.stop`. Send `input` messages without `id`
while streaming. With `binary: true` frames arrive as **binary WebSocket
packets** instead of JSON events (about 33% smaller, no base64 cost):

```
[4 bytes]  "PJF1" magic
[4 bytes]  header length N, little-endian uint32
[N bytes]  header JSON: {"w": 960, "h": 540, "src": "screen"}
[rest]     JPEG bytes
```

`src` is `"screen"` or `"camera"` — route frames to the right view.

With `binary: false` (or absent) frames arrive as legacy
`{event:"screen.frame", data:{jpeg: base64, w, h}}` events. In both modes the
agent **skips frames identical to the previous one**, so an idle desktop uses
almost no bandwidth — expect gaps between frames when nothing moves.

**File download:** repeat `fs.download` with increasing `offset`
(`offset += length` of decoded bytes) until `eof: true`.

**Clipboard sync both ways:** send `clipboard.watch {enabled:true}` once after
pairing; incoming `clipboard.changed` events carry PC->phone text; phone->PC
is `clipboard.set`.
