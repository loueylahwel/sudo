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

## Next: Linux

The owner is formatting the PC and moving to Linux. The agent does NOT run on Linux yet.
- The plan is in docs/linux-port-plan.md (feature matrix, phases 0-5). It recommends Ubuntu 26.04 LTS with GNOME on Wayland.
- remote_access.py itself is portable (stdlib + cryptography).
- Still open with the owner: which distro and desktop, whether it must work with nobody at the PC, and the other questions at the end of the plan.

After reinstalling:
1. The PC gets a new TLS certificate, so on the phone remove the PC and pair again on the LAN. The pin is trust-on-first-use, and a re-pair resets it.
2. The PC's LAN IP may change, so update any manual port forward. UPnP updates its own mapping.

## Environment notes (from the Windows PC)

- **Phone:** Infinix X6833B. Wireless adb's port changes each time; run `adb mdns services`, then `adb connect`.
- **Router:** admin page at https://192.168.0.1 (French UI). UPnP is under Pare-Feu -> UPNP. Router settings are changed by the owner, not by Claude.
- **Agent tests:** import agent.py and run Agent(cfg) on side ports (never the live one) with a FakeMouse. Example harnesses were lag_agent.py, fps60_client.py, slowlink_client.py and remote_test.py; they were in the Windows scratchpad and are gone.
- **Owner preferences:** fast delivery, short updates, install straight to the phone.
