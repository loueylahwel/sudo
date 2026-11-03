#!/usr/bin/env bash
# Installs the Sudo PC agent for the current user on Linux and starts it
# with the desktop session. Debian / Ubuntu / Mint (apt), Fedora (dnf),
# Arch / Manjaro (pacman) and openSUSE (zypper); X11 or Wayland (GNOME and
# KDE Plasma through the desktop portal; Sway, Hyprland and other wlroots
# desktops through the portal for the screen and a virtual input device).
#
#   bash agent/linux/install.sh
#
# Re-run it after pulling updates: it refreshes the files and restarts the
# agent. Logs: journalctl --user -u sudo-agent -f
# SUDO_AGENT_NO_SERVICE=1: packages and files only, nothing started (CI).
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/.local/share/sudo-agent"
UNIT_DIR="$HOME/.config/systemd/user"

# CI containers run as root without sudo.
as_root() {
    if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi
}

desktop="${XDG_CURRENT_DESKTOP:-}"
wayland=""
if [ "${XDG_SESSION_TYPE:-}" = "wayland" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
    wayland=1
fi
# GNOME and KDE give remote control through the portal; the other Wayland
# desktops only share the screen there, so input takes a virtual device
# (/dev/uinput) and text goes through wtype.
input_fallback=""
if [ -n "$wayland" ] && ! echo "$desktop" | grep -qiE 'gnome|kde|plasma'; then
    input_fallback=1
fi

echo "== System packages (asks for your password)"
# What each piece is for:
#  python venv/dev, compiler, kernel headers: the agent's Python packages
#    (pynput builds evdev);
#  tk: the Allow/Deny prompt for new phones; libX11/libXtst: typing on X11;
#  xclip/wl-clipboard: clipboard sync; playerctl: Now Playing and seek;
#  pipewire tools, pactl, opus: the PC's sound on the phone; adb: USB;
#  python GObject + GStreamer (pipewire plugin): the screen on Wayland, and
#    app icons on KDE (GdkPixbuf); wtype: typing on wlroots desktops.
if command -v apt-get >/dev/null; then
    pkgs=(python3-venv python3-dev python3-tk build-essential linux-libc-dev
          libx11-6 libxtst6 xclip wl-clipboard playerctl pipewire-bin
          pulseaudio-utils libopus0 adb python3-gi gir1.2-gdkpixbuf-2.0
          gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0
          gstreamer1.0-pipewire gstreamer1.0-plugins-base wtype)
    as_root apt-get update -qq
    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "${pkgs[@]}"
elif command -v dnf >/dev/null; then
    pkgs=(python3 python3-devel python3-tkinter gcc kernel-headers libX11
          libXtst xclip wl-clipboard playerctl pipewire-utils
          pulseaudio-utils opus android-tools python3-gobject gdk-pixbuf2
          gstreamer1 gstreamer1-plugins-base pipewire-gstreamer wtype)
    as_root dnf install -y "${pkgs[@]}"
elif command -v zypper >/dev/null; then
    pkgs=(python3 python3-devel python3-tk gcc linux-glibc-devel libX11-6
          libXtst6 xclip wl-clipboard playerctl pipewire-tools
          pulseaudio-utils libopus0 android-tools python3-gobject
          typelib-1_0-GdkPixbuf-2_0 typelib-1_0-Gst-1_0
          typelib-1_0-GstApp-1_0 typelib-1_0-GstVideo-1_0
          gstreamer-plugin-pipewire gstreamer-plugins-base wtype)
    as_root zypper --non-interactive install "${pkgs[@]}"
elif command -v pacman >/dev/null; then
    pkgs=(python tk base-devel linux-api-headers libx11 libxtst xclip
          wl-clipboard playerctl pipewire libpulse opus android-tools
          python-gobject gdk-pixbuf2 gstreamer gst-plugins-base
          gst-plugin-pipewire wtype)
    # Arch supports no partial upgrades: sync and upgrade with the install.
    if [ -n "${CI:-}" ]; then
        as_root pacman -Syu --needed --noconfirm "${pkgs[@]}"
    else
        as_root pacman -Syu --needed "${pkgs[@]}"
    fi
else
    echo "No apt, dnf, zypper or pacman here: install these yourself, then"
    echo "re-run this script:"
    echo "  Python 3 with venv and headers, a C compiler, kernel headers, Tk"
    echo "  for Python, libX11, libXtst, xclip, wl-clipboard, playerctl,"
    echo "  PipeWire tools (pw-cat), pactl, libopus, adb, PyGObject,"
    echo "  GStreamer with its base plugins and the PipeWire plugin, wtype."
    if ! command -v python3 >/dev/null; then
        exit 1
    fi
fi

echo "== Agent files -> $DEST"
mkdir -p "$DEST"
cp "$SRC"/*.py "$SRC"/requirements.txt "$DEST"/
# The venv sees the system site packages: PyGObject (gi) only comes from
# the distribution. The venv's own packages still win over the system's.
if [ ! -x "$DEST/venv/bin/python" ]; then
    python3 -m venv --system-site-packages "$DEST/venv"
elif grep -q '^include-system-site-packages = false' "$DEST/venv/pyvenv.cfg"; then
    sed -i 's/^include-system-site-packages = false/include-system-site-packages = true/' \
        "$DEST/venv/pyvenv.cfg"
fi
"$DEST/venv/bin/pip" install -q --upgrade pip
"$DEST/venv/bin/pip" install -q -r "$DEST/requirements.txt"

if [ -n "$input_fallback" ]; then
    echo "== Mouse and keyboard on $desktop: virtual input device for you"
    # The logged-in user may create input devices (/dev/uinput), like
    # ydotool or a game controller mapper; nobody else gets it.
    echo 'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"' \
        | as_root tee /etc/udev/rules.d/70-sudo-agent-uinput.rules >/dev/null
    as_root modprobe uinput 2>/dev/null || true
    as_root udevadm control --reload 2>/dev/null || true
    as_root udevadm trigger --name-match=uinput 2>/dev/null || true
fi

if [ -n "${SUDO_AGENT_NO_SERVICE:-}" ]; then
    echo "== Not starting the agent (SUDO_AGENT_NO_SERVICE)"
    exit 0
fi

if systemctl --user show-environment >/dev/null 2>&1; then
    echo "== Start with the desktop (systemd user service)"
    mkdir -p "$UNIT_DIR"
    cp "$SRC/linux/sudo-agent.service" "$UNIT_DIR/"
    # GNOME and KDE hand these to user services themselves; Sway, Hyprland
    # and others often don't, and the agent then can't find the desktop.
    vars=()
    for v in DISPLAY WAYLAND_DISPLAY XDG_SESSION_TYPE XDG_CURRENT_DESKTOP XAUTHORITY; do
        if [ -n "${!v:-}" ]; then vars+=("$v"); fi
    done
    if [ "${#vars[@]}" -gt 0 ]; then
        systemctl --user import-environment "${vars[@]}"
    fi
    systemctl --user daemon-reload
    systemctl --user enable sudo-agent.service >/dev/null
    systemctl --user restart sudo-agent.service
    started="systemctl --user status sudo-agent"
    logs="journalctl --user -u sudo-agent -f"
    if ! systemctl --user is-active -q graphical-session.target; then
        echo
        echo "NOTE: your desktop doesn't start graphical-session.target, so the"
        echo "agent won't come back by itself after you log in again. Add this"
        echo "line to your compositor's config (Sway: exec ..., Hyprland: exec-once = ...):"
        echo "  systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XDG_SESSION_TYPE XDG_CURRENT_DESKTOP; systemctl --user restart sudo-agent"
    fi
else
    echo "== Start with the desktop (autostart entry, no systemd here)"
    mkdir -p "$HOME/.config/autostart"
    cat > "$HOME/.config/autostart/sudo-agent.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Rem agent
Comment=Phone remote control
Exec=$DEST/venv/bin/python $DEST/agent.py
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF
    pkill -f "$DEST/agent.py" 2>/dev/null || true
    mkdir -p "$HOME/.cache"
    PYTHONUNBUFFERED=1 nohup "$DEST/venv/bin/python" "$DEST/agent.py" \
        >"$HOME/.cache/sudo-agent.log" 2>&1 &
    started="pgrep -af sudo-agent/agent.py"
    logs="tail -f ~/.cache/sudo-agent.log"
fi

if command -v ufw >/dev/null && as_root ufw status | grep -q "Status: active"; then
    echo "== Firewall: allowing the phone in (8080 LAN, 8443 secure)"
    as_root ufw allow 8080/tcp >/dev/null
    as_root ufw allow 8443/tcp >/dev/null
elif command -v firewall-cmd >/dev/null && as_root firewall-cmd --state >/dev/null 2>&1; then
    echo "== Firewall: allowing the phone in (8080 LAN, 8443 secure)"
    as_root firewall-cmd -q --permanent --add-port=8080/tcp --add-port=8443/tcp
    as_root firewall-cmd -q --reload
fi

echo
echo "Rem agent installed and running."
echo "  Status: $started"
echo "  Logs:   $logs"
echo "  On the phone: Add PC -> it appears in the list; tap it and click"
echo "  Allow on this PC."
if [ -n "$wayland" ]; then
    echo
    echo "Wayland: the first time the phone opens the Screen page, this PC asks"
    echo "to allow screen sharing (and remote control). Allow it once; Rem"
    echo "remembers it."
fi
