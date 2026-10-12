#!/usr/bin/env bash
# Installs the Sudo PC agent for the current user on Ubuntu / Kubuntu
# (26.04 LTS tested) and starts it with the desktop session.
#
#   bash agent/linux/install.sh
#
# Re-run it after pulling updates: it refreshes the files and restarts the
# agent. Logs: journalctl --user -u sudo-agent -f
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/.local/share/sudo-agent"
UNIT_DIR="$HOME/.config/systemd/user"

echo "== System packages (asks for your password)"
# python3-tk: the Allow/Deny prompt for new phones. xclip/wl-clipboard:
# clipboard sync. playerctl: Now Playing and seek. The -dev/build packages
# compile pynput's evdev dependency.
sudo apt-get update -qq
# python3-gi + gir1.2-gdkpixbuf-2.0: app icons on KDE (SVG -> PNG); the
# GStreamer/PipeWire packages: screen frames through the Wayland portal.
sudo apt-get install -y python3-venv python3-dev python3-tk build-essential \
    xclip wl-clipboard playerctl \
    python3-gi gir1.2-gdkpixbuf-2.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
    gstreamer1.0-pipewire gstreamer1.0-plugins-base

echo "== Agent files -> $DEST"
mkdir -p "$DEST"
cp "$SRC"/*.py "$SRC"/requirements.txt "$DEST"/
# The venv sees the system site packages: python3-gi (GObject bindings)
# only comes from apt. The venv's own packages still win over the system's.
if [ ! -x "$DEST/venv/bin/python" ]; then
    python3 -m venv --system-site-packages "$DEST/venv"
elif grep -q '^include-system-site-packages = false' "$DEST/venv/pyvenv.cfg"; then
    sed -i 's/^include-system-site-packages = false/include-system-site-packages = true/' \
        "$DEST/venv/pyvenv.cfg"
fi
"$DEST/venv/bin/pip" install -q --upgrade pip
"$DEST/venv/bin/pip" install -q -r "$DEST/requirements.txt"

echo "== Start with the desktop (systemd user service)"
mkdir -p "$UNIT_DIR"
cp "$SRC/linux/sudo-agent.service" "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable sudo-agent.service >/dev/null
systemctl --user restart sudo-agent.service

if command -v ufw >/dev/null && sudo ufw status | grep -q "Status: active"; then
    echo "== Firewall: allowing the phone in (8080 LAN, 8443 secure)"
    sudo ufw allow 8080/tcp >/dev/null
    sudo ufw allow 8443/tcp >/dev/null
fi

echo
echo "Sudo agent installed and running."
echo "  Status: systemctl --user status sudo-agent"
echo "  Logs:   journalctl --user -u sudo-agent -f"
echo "  On the phone: Add PC -> it appears in the list; tap it and click"
echo "  Allow on this PC."
if [ "${XDG_SESSION_TYPE:-}" = "wayland" ]; then
    echo
    echo "NOTE: you are in a Wayland session. Files, terminal, media, power and"
    echo "clipboard work there, but the Screen view and mouse/keyboard control"
    echo "need the X11 session for now:"
    echo "  sudo apt install plasma-session-x11"
    echo "then log out and pick 'Plasma (X11)' on the login screen."
fi
