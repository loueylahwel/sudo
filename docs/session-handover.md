# Session handover (2026-10-12, before moving the PC to Linux)

Continue from docs/session-handover.md on branch
`feat/60fps-live-typing-remote-access`.

## State

All work is in commit 921435e on `feat/60fps-live-typing-remote-access`. It is not merged into `main`.

Shipped to the phone and the Windows PC, and tested:
- **Cursor lag fixed.** Input runs outside the per-phone request lane, websocket compression is off, and the phone acks each frame (at most 2 unacked).
- **One stream mode:** JPEG auto at up to 60 fps, captured with DXGI Desktop Duplication on Windows. The quality options are gone.
- **App:** live typing, full screen in landscape that follows the phone either way round, and a shared FrameDecoder.

**Internet remote access, no third party (agent/remote_access.py).** It has a TLS port (8443) with a self-signed certificate that the phone pins, token-only auth from outside, and a UPnP port mapping. It passed every test from inside the LAN.
- From mobile data it does NOT connect yet. The router (Ooredoo TN 5G CPE) has a public IPv4 that matches what the internet sees, so this is not CGNAT. UPnP is on, the router shows the mapping, and Windows Firewall allows the agent. Even so, `https://<public ip>:8443` can't be opened from mobile data.
- The owner was about to add a manual port forward (Pare-Feu -> Redirection De Port: TCP 8443 -> PC LAN IP:8443) and retest in Chrome on mobile data.
  - A certificate warning means it is reachable, so look at the app.
  - A timeout or refusal means Ooredoo blocks inbound. The options are then to ask Ooredoo for inbound or a fixed public IP, to try IPv6 (the APN shows no IPv6 today), or to accept a relay.
- The router has no NAT loopback, so the public IP can't be tested from inside the LAN.
- **Residual:** the first token is still issued over plain ws on the LAN. Rotating the token over TLS would close that.

## Linux (the owner chose Kubuntu 26.04, KDE Plasma)

The relay/middle-server idea was dropped by the owner. Direct remote access stays in the code; it does nothing while Ooredoo blocks inbound.

The agent is Linux-ready:
- **Install** with `bash agent/linux/install.sh`. It installs the apt packages, copies the agent to ~/.local/share/sudo-agent with a venv, and enables a systemd user unit tied to graphical-session.target. The unit restarts the agent until the display is reachable.
- **Tested** with the real installer on Ubuntu 26.04 under WSL. Everything passed over the protocol: pairing, sys.info, fs.shortcuts (Home / Computer=/), fs.list, sys.exec (bash), processes, clipboard (xclip), media.info (playerctl/MPRIS), power (dry run), remote.info and screen capture with X11 forced.
- **Wayland session:** screen.start answers with a clear message. The agent can't capture yet.
- **Screen and mouse on Kubuntu 26.04 (Wayland-only by default):** install `plasma-session-x11` and pick "Plasma (X11)" at login. mss and pynput then work.
- **App:** the Files page joins paths with '/' on Linux PCs.

Next on the real Kubuntu machine, in order:
1. Install, pair and smoke-test on Plasma (X11).
2. Native KDE Wayland: xdg-desktop-portal RemoteDesktop + ScreenCast (one Allow click, restore_token), PipeWire frames via GStreamer pipewiresrc, and portal Notify* input. See linux-port-plan.md.
3. Brightness via powerdevil (org.kde.ScreenBrightness).
4. Window switcher and dock via KWin scripts.
5. Phone as second monitor via a portal VIRTUAL source.
6. Phone audio via a PipeWire null sink.

After reinstalling:
1. The PC gets a new TLS certificate, so on the phone remove the PC and pair again on the LAN. The pin is trust-on-first-use, and a re-pair resets it.
2. The PC's LAN IP may change, so update any manual port forward. UPnP updates its own mapping.

## Environment notes (from the Windows PC)

- **Phone:** Infinix X6833B. Wireless adb's port changes each time; run `adb mdns services`, then `adb connect`.
- **Router:** admin page at https://192.168.0.1 (French UI). UPnP is under Pare-Feu -> UPNP. Router settings are changed by the owner, not by Claude.
- **Agent tests:** import agent.py and run Agent(cfg) on side ports (never the live one) with a FakeMouse. Example harnesses were lag_agent.py, fps60_client.py, slowlink_client.py and remote_test.py; they were in the Windows scratchpad and are gone.
- **Owner preferences:** fast delivery, short updates, install straight to the phone.
