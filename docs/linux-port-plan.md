# Sudo agent on Linux: port plan

Sources are the four audits: **[Capture]** for screen and streaming, **[Input]** for input and touch, **[System]** for system features, and **[WM]** for window management, the phone monitor and phone audio. I checked these agent.py lines against the current file:
- `import platform`: 20
- mss, pyperclip and pynput imports: 44, 46, 49-50
- global mouse and keyboard controllers: 108-109
- `_winapps` and `_make_phone_monitor`: 1652, 1668
- `approval.ask`: 1919
- `clipboard_watcher`: 3082

Some other agent.py line numbers in the audits may be about 4 lines off past line 509, because the file was edited while they ran.

## Short answer: no, not today

- **It can crash at startup.** pynput and mss are imported, and the mouse and keyboard controllers created, when the module loads (agent.py:44, 49-50, 108-109). On Linux, pynput raises without an X display, so a systemd unit started before the session, or a Wayland session without Xwayland, kills the agent before it does anything [System, Input].
- **On Wayland, the parts that do start fail silently:**
  - The screen view freezes. mss cannot grab Wayland, and the retry-forever loop at agent.py:719-731 hides the error [Capture].
  - Mouse and keys only reach Xwayland windows.
  - Media and volume keys do nothing [Input, System].
- **Some features are Windows-only by design:** the window switcher, app dock, phone as second monitor, phone audio, brightness and Now Playing (agent.py:1652, 1668; brightness.py:325-338; media_info.ps1) [WM, System].
- **The phone app would create broken files.** It joins paths with a backslash (files_page.dart:130, 141), so an upload on Linux makes a file whose name contains a literal `\` [System].

These already work, or nearly: processes, camera, discovery, the power-timer logic, pairing, and the terminal (with fixes).

Getting it to "function the same" is possible for almost every feature. It means rebuilding screen capture, input and window control per desktop, because GNOME 50 has no X11 session any more and Plasma 6.8 (due 14 Oct 2026) offers only Wayland at login [Capture].

---

## 1. Feature matrix

Legend:
- **Yes**: same as Windows.
- **Yes+**: better than Windows.
- **Caveats**: works, with the listed differences.
- **Hard**: possible, but needs out-of-tree or fragile pieces.

Effort: S is under a day, M is 2-4 days, L is 1-2 weeks.

| Feature | Windows today | X11 | GNOME Wayland | KDE Wayland | Effort |
|---|---|---|---|---|---|
| **Screen view + touchpad** | ScreenStreamer agent.py:424-880; DXGI (dxgi_capture.py); pynput `do_move_rel` 1496-1515 | **Yes.** mss>=10.2,<11 over XShm takes a few ms per grab, so 60 fps is possible without a DXGI equivalent. pynput/XTest move is unaccelerated, as on Windows. Fix `primary_monitor` (188-198) to use `is_primary`, not the (0,0) origin [Capture] | **Caveats.** New capture path: `org.gnome.Mutter.ScreenCast` to PipeWire, with input through `Mutter.RemoteDesktop`. No consent dialog. A "sharing" indicator shows in the top bar while a phone is connected. 60 fps not yet measured. Relative motion may be accelerated (verify) [Capture, Input] | **Caveats.** Portal RemoteDesktop + ScreenCast: one consent click at the PC, then a single-use restore token. The dialog comes back after a monitor-layout change, and token bugs were fixed only in 6.6.6. Plasma 6.3+ can pre-authorize [Capture] | X11 S, Wayland L |
| **Keyboard / live typing / dictation** | `do_key`/`do_text` 1534-1547 (Unicode SendInput); live_typing.dart | **Yes.** XTest, with keycode borrowing for characters outside the layout. Add a small delay between keys [Input] | **Caveats.** Named keys are fine. Characters outside the active layout (emoji, some accents) need Mutter `SetKeymap` (first release unverified) or a paste fallback that saves and restores the clipboard [Input] | **Caveats.** keysyms are limited to the layout, so out-of-layout characters need the paste fallback. Terminals need Ctrl+Shift+V [Input] | S-M |
| **Media keys / Now Playing / volume** | SPECIAL_KEYS 1476-1481; media_info.ps1 via 1825-1856 (1-3 s of PowerShell per poll) | **Yes+.** MPRIS (playerctl/playerctld) for play/pause/next/previous, metadata and seek; `wpctl` for volume. Each call takes milliseconds [System] | **Yes+.** Same, and required here: XTest keys silently do nothing on Wayland. `wpctl` shows no on-screen indicator; uinput KEY_VOLUMEUP gives it back [System, Input] | **Yes+.** Same. The on-screen indicator comes back via `kglobalaccel invokeShortcut` [System] | M |
| **Brightness** | brightness.py:38-274 (WMI through PowerShell; DDC through dxva2) | **Yes.** Read sysfs, write with logind `Session.SetBrightness` (no root). `ddcutil` for external monitors [System] | **Caveats.** On GNOME 49+ Mutter owns the backlight, so use its D-Bus API or the Shell slider desyncs and may revert. The agent runs `ddcutil` itself [System] | **Yes.** powerdevil `org.kde.ScreenBrightness` covers the panel and DDC monitors, with the on-screen indicator [System] | M |
| **Power + timer** | POWER_CMDS 1711-1735; timer 2789-2857; shutdown.exe backstop 1787-1820 | **Caveats, the same on all three desktops.** logind and polkit need no password for the active session. Changes needed: (1) lock with `lock-session auto`, because the existing `loginctl lock-session` fails from a user unit; (2) wait for each command's result instead of always replying `scheduled:true`; (3) use logind `ScheduleShutdown` as the backstop, and on sleep cancel it and re-arm on wake, or it fires immediately on resume; (4) hide Hibernate via `CanHibernate`, since Secure Boot lockdown probably blocks it; (5) enable the NVIDIA suspend services. The 60 s warning becomes Tk through Xwayland or a desktop notification with a Cancel button [System] | | | S+M |
| **Clipboard** | pyperclip, agent.py:46, 2324-2331 | **Yes.** xclip, moved off the event loop [System] | **Caveats.** Mutter has no data-control protocol, so force xclip through Xwayland [System] | **Yes.** wl-clipboard, including `--watch` [System] | S |
| **Files** | list_dir 1552-1616; files_page.dart:128-143 | **Yes once the app is fixed.** The app must join with `/` (send `sep` in sys.info). "Drives" becomes `/`, home and `/run/media/*`. XDG user folders are localized. Symlinked folders show as files (1571) [System] | same | same | S agent + S app |
| **Terminal** | run_command 1621-1630 (cmd.exe) | **Yes, with fixes.** `/bin/sh` is dash, so use `bash -lc`. Kill the process group on timeout. `sudo` has no TTY. Launch GUI apps via `systemd-run --user`. Change the hint text (terminal_page.dart:171) [System] | same | same | S |
| **Processes** | psutil 1632-1649 | **Yes.** `terminate()` sends SIGTERM, so follow with SIGKILL after 3 s to match TerminateProcess [System] | same | same | S |
| **Camera** | CameraStreamer 883-952 | **Yes.** V4L2; set `CAP_PROP_BUFFERSIZE=1` [Capture] | same | same | S |
| **Window switcher / app dock / launcher** | winapps.py (EnumWindows, PrintWindow, Get-StartApps) | **Yes.** EWMH through python-xlib; `_NET_WM_ICON`; XComposite thumbnails [WM] | **Caveats.** Needs our own GNOME Shell extension: enable it once and log out/in, and re-test it on every GNOME major release [WM] | **Caveats.** KWin scripts loaded at runtime, nothing to install. Thumbnails need KWin ScreenShot2, which authorizes by executable path, so the agent must ship frozen. Window ids are UUID strings, which breaks the `int()` casts at agent.py:2535 and 2542 [WM] | X11 M, GNOME L, KDE L |
| ...app list/launch (all) | | **Yes+.** Gio `.desktop` files take about 100 ms (Get-StartApps takes 1-4 s). Launch through systemd-run, the extension or KIO, so apps don't die when the agent's unit stops [WM] | | | S-M |
| **Phone as second monitor (incl. touch, fence, focus return)** | vdisplay.py (Parsec VDD + keepalive), phone_monitor.py, touch.py | **Hard.** Needs the evdi DKMS module (Secure Boot key enrollment; verified up to kernel ~6.15), or xrandr tricks that aren't crash-safe. Touch via a uinput touchscreen plus `xinput map-to-output`; fence via XFixes barriers [WM, Input] | **Caveats; the best option.** Mutter `RecordVirtual`: no driver, no dialog, and the monitor unplugs itself when the agent's D-Bus connection drops, the same crash safety as the Parsec keepalive. `NotifyTouch*` works in the monitor's own coordinates and leaves the PC cursor alone, which is better than Windows. But monitors must share an edge, so corner parking is impossible; a `Meta.Barrier` in the extension fences instead. Uses a private API [WM, Input] | **Caveats.** Portal VIRTUAL source (Plasma 6.6+) with a consent dialog and unreliable tokens. Touch takes global coordinates (the portal ignores the stream), and KDE touch is unverified. KWin can't warp the pointer, so the fence is weak [WM, Input] | L |
| **Phone audio** | phone_audio.py (WASAPI process loopback + pycaw ducking) | **Yes+ on all three.** Create a PipeWire null sink "Sudo phone", move the app's streams into it and record its monitor. The PC goes exactly silent without the 0.001 / x1000 trick, and if the agent dies, pipewire-pulse removes the sink. Caveat: WirePlumber remembers moved streams, so clear `target.object` on stop [WM] | | | M |
| **Approval prompt** | approval.py (Tk + user32 `_no_activate`) | **Yes.** Tk; use `iconphoto` instead of a .ico [System] | **Caveats.** Tk runs through Xwayland and needs the session's DISPLAY/XAUTHORITY. Add a timeout, because `approval.ask` at 1919 waits forever if Tk dies. Fall back to a desktop notification with Allow and Deny buttons [System] | same as GNOME | M |
| **Autostart** | Startup-folder .lnk (3224-3242), run-hidden.vbs | **Yes.** A `systemd --user` unit tied to `graphical-session.target` (or XDG autostart). Logs go to `journalctl --user` [System] | same | same; name the unit `app-<id>.service` so KDE pre-authorization can work | S |
| **Discovery** | beacon 2929-2947; relay on :8080 | **Yes.** Linux firewalls never ask, so if ufw/firewalld is active, open TCP 8080 once. Also broadcast on each network interface (a VPN can take the default route) [System] | same | same | S |

Extras outside the list:
- **PC notification mirroring.** Not implemented on Windows; on Linux it is easy via a session-bus monitor (M) [System].
- **H.264.** Dormant in the app; port it last (L) [Capture].
- **Single instance.** Use `flock` (S) [System].

---

## 2. Recommendation: Ubuntu 26.04 LTS, GNOME on Wayland

Use the default session, keep the GPU in hybrid mode, and keep 100% scaling.

Why:
1. **No prompts, as on Windows.** GNOME is the only Wayland desktop where an unsandboxed agent can capture the screen, inject mouse, keys and touch, and create a virtual monitor without any consent dialog, through `org.gnome.Mutter.ScreenCast` and `RemoteDesktop`. Any unsandboxed process could call them as of May 2026 (https://discourse.gnome.org/t/security-for-mutter-screencast/35009) [Input, WM]. On KDE all of this goes through the portal: a click at the PC, then single-use tokens that re-prompt after layout changes and had bugs into 6.6.6 and 6.7.2. A phone user away from the PC can't answer that dialog [Capture].
2. **The phone monitor stays closest to the Parsec design** (the Discord-on-phone setup): a built-in virtual monitor, crash safety tied to the D-Bus connection, real touch that doesn't move the PC cursor, and a hard pointer barrier instead of 16 Hz polling [WM, Input]. On KDE, KWin can't warp the pointer and portal touch is unverified. On X11 it needs the evdi kernel module plus Secure Boot key enrollment.
3. **An LTS release stays on GNOME 50 for its whole life.** The extension and the private Mutter API then don't move every six months. Those are GNOME's two main risks: extension churn, and a proposed list of allowed callers (gnome-shell#3943) [WM].
4. **X11 isn't available on GNOME 50 or Plasma 6.8 anyway.** An X11 desktop (Mint Cinnamon, Xfce) would make most rows cheap, but the phone monitor would still need evdi, and the code would target a shrinking platform.
5. **Fedora would need more setup.** It needs RPM Fusion for ffmpeg, x264 and VA-API H.264 [Capture], and it changes GNOME version every six months.

What GNOME costs, to accept knowingly:
- We write and maintain a small Sudo GNOME Shell extension. It handles window list, actions, thumbnails and icons, focus, cursor position and the fence.
- A "screen is being shared" indicator shows in the top bar whenever a phone is connected.
- The clipboard goes through xclip and Xwayland.
- Brightness goes through Mutter's API.
- `wpctl` volume changes show no on-screen indicator unless we send uinput keys.
- Emoji and other out-of-layout characters need `SetKeymap` or the paste fallback.

Pick KDE instead if the owner prefers KDE and accepts clicking Allow at the PC once, and again after monitor changes. Clipboard, brightness and DDC are easier there; the effort is similar; the fence is weaker.

**One-time setup (GNOME):**
1. Install Ubuntu 26.04 and use the default GNOME (Wayland) session. Keep the 1080p panel at 100% scaling; fractional scaling breaks the physical-pixel contract in protocol.md:217 [WM].
2. Install the proprietary NVIDIA driver in hybrid/on-demand mode, so the AMD iGPU drives the panel. Enable `nvidia-suspend`, `nvidia-resume` and `nvidia-hibernate` with `NVreg_PreserveVideoMemoryAllocations=1`, then test suspend and resume. Never start the agent with `prime-run` or `DRI_PRIME` [System, Capture].
3. Install the packages: `python3-tk python3-gi gstreamer1.0-pipewire playerctl xclip pulseaudio-utils`, plus `ddcutil` if an external monitor's brightness matters. The install script will print the exact list.
4. Install the agent in a venv under `~/.local/share/sudo-agent`, then run `systemctl --user enable --now sudo-agent`. Call the binary `sudo-agent`, never `sudo`, or it would hide `/usr/bin/sudo` [System].
5. Run `gnome-extensions enable sudo-agent@…`, then log out and back in once [WM].
6. Only if ufw is enabled: `sudo ufw allow 8080/tcp`, ideally limited to the LAN subnet [System].
7. Optional: a udev rule for `/dev/uinput` (only for the volume on-screen indicator, or as a fallback input path), and `i2c-dev` for DDC.
8. Pair the phone. The approval window appears through Xwayland.

The main path needs no portal clicks.

---

## 3. Architecture and port order

**Don't name the package `platform/`.** agent.py:20 does `import platform`; a sibling `platform/` package would hide the standard-library module. Use `agent/plat/`.

```
agent/
  agent.py              protocol, handlers, streamers, relay (no OS calls after the split)
  plat/
    __init__.py         detect() -> Platform(os, session, desktop, caps) + lazy backend getters
    base.py             interfaces (typing.Protocol)
    win32.py            thin adapters over the existing Windows modules (files unchanged)
    linux_common.py     logind, MPRIS, wpctl, PipeWire audio, Gio apps, xdg dirs, clipboard choice
    linux_x11.py        mss, pynput, python-xlib EWMH (mostly free)
    linux_gnome.py      Mutter ScreenCast / RemoteDesktop / DisplayConfig + extension client
    linux_portal.py     xdg-desktop-portal session (GNOME fallback, KDE primary)
    linux_kde.py        KWin scripts, kscreen-doctor (optional phase)
  dxgi_capture.py winapps.py vdisplay.py touch.py phone_audio.py brightness.py   (stay put, lazy-imported)
  linux/  gnome-extension/  sudo-agent.service  install-linux.sh  pwcap/ (optional native helper)
```

**Interfaces** (combined from the audits):
- **Capture** [Capture]:
  - `monitors()` returns `[{id, left, top, width, height, is_primary, name, scale}]`;
  - `open(mon).grab(timeout_ms)` returns `Frame(bgra, cursor{x, y, visible}, seq)` or `None`;
  - the frame carries its own cursor, which removes the global `mouse.position` reads at 574, 662 and 2949-2954.
- **Input** [Input]: `move_abs`, `move_rel`, `button`, `scroll`, `key`, `type_text`, `cursor_pos() -> pt|None`, `warp() -> bool`.
- **Windows** [WM, Input]:
  - actions: list, thumb, icon, activate, close, minimize, maximize, restore, move_to_monitor, foreground, window_at;
  - events: focus_changed, window_added, minimized_changed.
- **Apps**: list, icon, launch.
- **VirtualDisplay**: status, enable(size, orientation, scale) returning a rect plus a FrameSource, disable, set_geometry.
- **PhoneInput**: touch, keys, wheel, fence.
- **AppAudio**: start, stop, set_targets(pids), restore_saved.
- **System** [System]:
  - power and can_power;
  - os_timer arm and abort;
  - brightness;
  - media and volume;
  - clipboard get, set and watch;
  - prompt ask and warn;
  - autostart;
  - instance lock;
  - config path.

**Backend selection.** Start from `platform.system()`. On Linux, check `XDG_SESSION_TYPE`/`WAYLAND_DISPLAY` first, then `XDG_CURRENT_DESKTOP`, then probe for what is actually there: the Mutter bus name, whether the extension answers, the portal's `AvailableSourceTypes`. Never decide from `DISPLAY` alone: Xwayland sets it inside Wayland sessions, so mss would "start" and then fail silently [Capture].

**Not breaking Windows:**
- Phase 0 neither moves nor edits the Windows modules. `win32.py` calls the same functions agent.py calls today.
- Every backend is imported lazily inside try/except, so a missing tool disables only its own feature (the existing pattern at 1652 and 1668).
- `requirements.txt`:
  - mark `comtypes` and `pycaw` with `; sys_platform == "win32"`;
  - add `dbus-fast`, `pulsectl`, `python-xlib` and `opencv-python-headless`;
  - pin `mss>=10.2,<11`, because mss 11 makes `.raw` read-only and encode_frame paints the cursor into it (314-326) [Capture].
- `sys.info` reports `os, session, desktop, sep, power_actions (logind CanX), features`, and the app hides whatever is unavailable.
- Run the Windows regression check every phase: `scripts/verify_stream_quality.py`, `scripts/verify_phone_monitor.py`, and a phone smoke test.

**Where the audits disagreed, and what I decided:**
- **GNOME: Mutter's own API or the portal?** [Capture] wanted the portal as the default; [Input] and [WM] wanted Mutter directly. **Mutter directly**, because it is prompt-free and the LTS release freezes it. The portal fallback comes in phase 5, or sooner if GNOME restricts callers.
- **libei `ConnectToEIS` or the `Notify*` D-Bus calls?** **`Notify*` everywhere.** Once EIS is connected, the `Notify*` calls fail on KDE [Input]. This also avoids a libei dependency.
- **KDE portal touch:** [Input] read the xdg-desktop-portal-kde source and found it implemented, with global coordinates; [WM] calls it unverified. I treat it as implemented but untested.
- **Native PipeWire helper or GStreamer?** **Start with GStreamer `pipewiresrc → appsink`** with the cursor hidden and its position taken from the extension, so the agent keeps drawing its own arrow as today. **Build the native helper only if the bench shows under ~55 fps or stutter.** It is a C or Rust build step that [Capture] says is needed for cursor metadata and full frame rate.

**Phases.** Each one ships and can be tested on its own.

| # | Ships | How to test | Effort |
|---|---|---|---|
| 0 | **The platform seam.** `plat/` package, lazy imports (44, 46, 49-50, 108-109), capability report, requirements markers. Fix bugs that also affect Windows: `clipboard_watcher` never starts in standalone mode (3082); `approval.ask` has no timeout (1919); config.json gets mode 0600 and atomic writes; agent_gui drops `require_approval`. Windows behaves the same as before. | Windows regression scripts; the agent starts and pairs on a GNOME 50 VM | M |
| 1 | **Linux basics.** The systemd unit and `install-linux.sh`. Power through logind (lock `auto`, wait for results, `CanX`, `ScheduleShutdown`, sleep handling; rename the `win_timer_*` functions and remove the gates at 2782/2796/2843). MPRIS media and Now Playing, `wpctl` volume. Clipboard chosen per desktop. Brightness (Mutter API on GNOME 49+, logind elsewhere). Files, terminal, processes and camera fixes. Broadcast on each interface. **App update:** path separator, OS-aware labels, hide Hibernate. X11 screen view comes free (mss pin + primary fix). | Every Home, Files, Terminal, Processes and Camera screen on the phone against the VM | M |
| 2 | **Screen, touchpad and keyboard on GNOME Wayland.** Mutter `RecordMonitor` with a linked `RemoteDesktop` session. `pipewiresrc` consumer. Extension v1 (pointer position). Pointer, keys and scroll (Wayland's axis sign is the opposite of pynput's). If relative motion turns out to be accelerated, send absolute = cursor + delta. Paste fallback for Unicode. Send `screen.error` instead of freezing. | screen_page touchpad, live typing, dictation; **gate:** a bench mode reaches at least 55 fps on the real laptop, otherwise build `pwcap` (+M-L) | L |
| 3 | **Window switcher, dock and launcher on GNOME.** Extension v2: list in Alt-Tab order, actions, thumbnails (`paint_to_content`), icons, events. Gio for apps. | window_switcher.dart flows | L |
| 4 | **Phone as second monitor and phone audio on GNOME.** `RecordVirtual` (size via `modes` on GNOME 50). Edge placement through DisplayConfig. `Meta.Barrier` fence. `NotifyTouch*`. A "focus confirmed" handshake before phone text is sent. Event-driven reclaim, sweep and minimize checks. Keep the policy code in phone_monitor.py; replace `_win_api` (961-1026) and the touch and vdisplay calls. PipeWire null-sink audio. OS-aware setup text (phone_monitor_page.dart:812, 833-835, 891-894). | Port verify_phone_monitor.py. Discord-on-phone end to end. `kill -9` the agent and check that the monitor and audio revert | L |
| 5 | **Hardening.** Portal fallback on GNOME. Recovery when the session is closed ("Stop sharing"). Lock-screen behaviour tests. Packaging (venv, or PyInstaller onedir; avoid Flatpak/Snap). Notification mirroring (bonus). | Reboot and lock tests on the laptop | M |
| opt | KDE backend (L); X11 phone monitor via evdi (L); H.264 on Linux through GStreamer/VA-API on the iGPU (L) | | |

Phases 0-3 can be built and tested in a GNOME 50 VM before the laptop is reinstalled. The phase 2 frame-rate gate and the phase 4 hybrid-GPU behaviour need the real laptop.

---

## 4. Where parity isn't possible, and the closest substitute

| Gap | Why | Closest substitute |
|---|---|---|
| Remote control that never needs a click at the PC (KDE) | The portal asks for consent, and restore tokens re-prompt after layout changes or "Stop sharing" [Capture] | GNOME through Mutter directly; on KDE, Plasma 6.3+ pre-authorization (reported not to always take effect) |
| A remote session nobody at the PC can see | GNOME's top bar and KDE's tray show an indicator by design [Input] | None. It is a privacy feature. |
| Phone monitor touching the PC screen only at a corner (phone_monitor.py:360-376) | Mutter rejects layouts where monitors don't share an edge [Input, WM] | Share an edge, fenced by a `Meta.Barrier`: a hard barrier instead of 16 Hz polling |
| Typing any Unicode character (emoji, characters outside the layout) on Wayland | keysym, libei and uinput only cover the active layout [Input] | Mutter `SetKeymap` (verify which release has it), or clipboard paste with save and restore |
| Pointer fence and focus control on KDE | KWin scripts can't warp the pointer [WM, Input] | Push the cursor back with portal absolute motion, or leave a gap between outputs (unverified) |
| Volume on-screen indicator on GNOME through `wpctl` | GNOME's ShowOSD is restricted to approved callers [System] | uinput KEY_VOLUMEUP/DOWN/MUTE (needs a udev rule), or accept no indicator |
| Hibernate | Secure Boot lockdown and swap layout make it most likely unavailable [System] | Hide it via `CanHibernate`; offer Sleep |
| Viewing or unlocking the lock screen from the phone on Wayland | Mutter reportedly refuses screencast sessions while locked; not yet tested [Input, Capture] | Lock from the phone works; unlocking may need someone at the PC. Send a clear `screen.error` |
| The OS choosing the "current media session" | MPRIS has no system-wide current player [System] | `playerctld` (most recently active player). Spotify reports position poorly |
| 60 fps guaranteed | The compositor sets the frame rate; there are reports of `pipewiresrc` running at about 40 fps [Capture] | Bench gate in phase 2; native helper if needed |
| Window thumbnails on KDE from a plain Python script | ScreenShot2 authorizes by executable path [WM] | Ship the agent frozen |
| H.264 capture with ffmpeg on Wayland | ffmpeg has no PipeWire source and x11grab is black [Capture] | A GStreamer encoder on the same PipeWire stream (dormant in the app anyway) |

---

## 5. Open questions only the owner can answer

1. **Distro and desktop.** Is Ubuntu 26.04 LTS with GNOME acceptable, or do you prefer KDE or Mint/Cinnamon (X11)? This choice changes about 40% of the work.
2. **Use with nobody at the PC.** Must it work after a reboot, or while locked? That decides whether one consent click (KDE) is acceptable and whether to turn on auto-login.
3. **The sharing indicator.** Is it OK for GNOME's top bar to show "screen being shared" whenever the phone is connected?
4. **Is the phone-as-second-monitor (Discord on the phone) needed on day one?** If it is, phase 4 moves ahead of phase 3.
5. **Will Windows stay** (dual boot, or other people on Windows, since you said "we")? That decides whether the Windows build keeps shipping and needs testing every phase. Will several people log in to the Linux PC? That affects power-action password prompts and config file permissions.
6. **GPU mode and Secure Boot.** Hybrid mode with the iGPU driving the panel is recommended. Will you keep Secure Boot on? It affects the NVIDIA driver, hibernate and evdi.
7. **External monitors.** Do you use them, and do you need their brightness from the phone? That decides whether `ddcutil`/i2c setup is needed.
8. **What do you type from the phone?** Emoji or non-Latin scripts decide how much Unicode work is needed.
9. **Install changes.** Are you OK with a GNOME extension (log out/in once), a systemd user service, opening TCP 8080 on the firewall, and an optional `/dev/uinput` udev rule?
10. **H.264.** Should it stay dormant and be skipped on Linux?