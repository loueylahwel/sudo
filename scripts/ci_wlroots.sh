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

pipewire > "$OUT/pipewire.log" 2>&1 &
sleep 0.5
wireplumber > "$OUT/wireplumber.log" 2>&1 &

# No output chooser (nobody to click it): share the headless output.
mkdir -p "$HOME/.config/xdg-desktop-portal-wlr" "$HOME/.config/xdg-desktop-portal"
printf '[screencast]\nchooser_type=none\noutput_name=HEADLESS-1\n' \
    > "$HOME/.config/xdg-desktop-portal-wlr/config"
printf '[preferred]\ndefault=wlr;gtk\n' > "$HOME/.config/xdg-desktop-portal/sway-portals.conf"
/usr/libexec/xdg-desktop-portal-wlr -l DEBUG > "$OUT/xdpw.log" 2>&1 &
sleep 1
/usr/libexec/xdg-desktop-portal -v > "$OUT/xdp.log" 2>&1 &
sleep 2

"$PY" -I scripts/test_portal_live.py "$OUT" --expect-no-remote-desktop --virtual-input
