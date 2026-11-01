#!/usr/bin/env bash
# A headless Sway desktop (wlroots) with PipeWire and xdg-desktop-portal-wlr,
# then scripts/test_portal_live.py against it: the real portal flow on a
# desktop with ScreenCast but no RemoteDesktop. For CI; run it on a session
# bus:  PY=<agent venv python> dbus-run-session -- bash scripts/ci_wlroots.sh OUT_DIR
set -euo pipefail
OUT="$1"
mkdir -p "$OUT"
XDG_RUNTIME_DIR="$(mktemp -d)"
chmod 700 "$XDG_RUNTIME_DIR"
export XDG_RUNTIME_DIR XDG_CURRENT_DESKTOP=sway XDG_SESSION_TYPE=wayland
export WLR_BACKENDS=headless WLR_RENDERER=pixman WLR_LIBINPUT_NO_DEVICES=1

conf="$(mktemp -d)"
echo "output HEADLESS-1 resolution 1280x720 bg #3060a0 solid_color" > "$conf/sway"
sway -c "$conf/sway" > "$OUT/sway.log" 2>&1 &
for _ in $(seq 50); do
    [ -S "$XDG_RUNTIME_DIR/wayland-1" ] && break
    sleep 0.2
done
export WAYLAND_DISPLAY=wayland-1
# A portal D-Bus restarts (after a crash) gets this session's environment.
dbus-update-activation-environment WAYLAND_DISPLAY XDG_RUNTIME_DIR \
    XDG_CURRENT_DESKTOP XDG_SESSION_TYPE

pipewire > "$OUT/pipewire.log" 2>&1 &
sleep 0.5
wireplumber > "$OUT/wireplumber.log" 2>&1 &

# No output chooser (nobody to click it): share the headless output.
mkdir -p "$HOME/.config/xdg-desktop-portal-wlr" "$HOME/.config/xdg-desktop-portal"
printf '[screencast]\nchooser_type=none\noutput_name=HEADLESS-1\n' \
    > "$HOME/.config/xdg-desktop-portal-wlr/config"
printf '[preferred]\ndefault=wlr;gtk\n' > "$HOME/.config/xdg-desktop-portal/sway-portals.conf"
# Debian/Ubuntu/Fedora keep them in /usr/libexec, Arch in /usr/lib.
bin() { for d in /usr/libexec /usr/lib; do [ -x "$d/$1" ] && { echo "$d/$1"; return; }; done; }
"$(bin xdg-desktop-portal-wlr)" -l DEBUG > "$OUT/xdpw.log" 2>&1 &
sleep 1
"$(bin xdg-desktop-portal)" -v > "$OUT/xdp.log" 2>&1 &
sleep 2

# The portal sends frames when the screen changes; a real desktop always
# does, this flat one only when told to: switch between two shades of blue.
# swaymsg finds sway through SWAYSOCK, which only sway's children inherit.
SWAYSOCK="$(ls "$XDG_RUNTIME_DIR"/sway-ipc.*.sock | head -1)"
export SWAYSOCK
( while sleep 0.4; do
      swaymsg 'output HEADLESS-1 bg #3060a0 solid_color' || true
      sleep 0.4
      swaymsg 'output HEADLESS-1 bg #3366aa solid_color' || true
  done ) > "$OUT/swaymsg.log" 2>&1 &

"$PY" -I scripts/test_portal_live.py "$OUT" --expect-no-remote-desktop
