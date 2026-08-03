"""Sudo Agent — the normal-user way to run the agent.

Double-click the exe (or `python agent_gui.py`): a small window shows the
pairing QR immediately. No terminal, no Python install needed when frozen
with PyInstaller. Scan the QR with the Sudo phone app and you're connected.

Build the exe:
    pyinstaller --noconsole --onefile --name Sudo-Agent --icon sudo.ico agent_gui.py
"""

import asyncio
import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path

# PyInstaller --noconsole leaves stdout/stderr as None; agent.py prints.
if sys.stdout is None:
    sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qrcode  # noqa: E402
from PIL import ImageTk  # noqa: E402

from agent import Agent, ensure_single_instance, load_config, pairing_payload  # noqa: E402

BG = "#17141f"
FG = "#f3f4f6"
MUTED = "#9ca3af"
ACCENT = "#4f5bd5"

STARTUP_DIR = (
    Path(os.environ.get("APPDATA", ""))
    / r"Microsoft\Windows\Start Menu\Programs\Startup"
)
STARTUP_LINK = STARTUP_DIR / "Sudo-Agent.lnk"


def startup_enabled():
    return STARTUP_LINK.exists()


def set_startup(enabled):
    """Add/remove a Start-with-Windows shortcut to this exe."""
    if enabled:
        STARTUP_DIR.mkdir(parents=True, exist_ok=True)
        ps = (
            "$s=(New-Object -ComObject WScript.Shell)"
            f".CreateShortcut('{STARTUP_LINK}');"
            f"$s.TargetPath='{sys.executable}';"
            "$s.Save()"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            check=False, capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    else:
        STARTUP_LINK.unlink(missing_ok=True)


class AgentGui:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()

        root.title("Sudo")
        root.configure(bg=BG)
        root.resizable(False, False)

        tk.Label(root, text="Sudo", bg=BG, fg=FG,
                 font=("Segoe UI", 20, "bold")).pack(pady=(18, 2))
        tk.Label(root, text="Scan with the Sudo phone app",
                 bg=BG, fg=MUTED, font=("Segoe UI", 10)).pack()

        payload = pairing_payload(self.cfg)
        qr_img = qrcode.make(payload).resize((260, 260))
        self._qr_photo = ImageTk.PhotoImage(qr_img)
        tk.Label(root, image=self._qr_photo, bg="white",
                 padx=10, pady=10).pack(pady=14)

        code = tk.Entry(root, justify="center", font=("Consolas", 16),
                        fg=FG, bg=BG, bd=0, readonlybackground=BG,
                        insertbackground=FG)
        code.insert(0, self.cfg["code"])
        code.configure(state="readonly")
        code.pack()

        self.status = tk.Label(root, text="Connecting…", bg=BG, fg=MUTED,
                               font=("Segoe UI", 10))
        self.status.pack(pady=(10, 2))

        self.startup_var = tk.BooleanVar(value=startup_enabled())
        tk.Checkbutton(
            root, text="Start with Windows", variable=self.startup_var,
            command=self._toggle_startup, bg=BG, fg=MUTED,
            selectcolor=BG, activebackground=BG, activeforeground=FG,
        ).pack()

        tk.Button(root, text="Quit", command=root.destroy, bg=ACCENT, fg=FG,
                  relief="flat", padx=24, pady=4).pack(pady=14)

        self.agent = Agent(self.cfg)
        self.agent.on_status = self._set_status
        threading.Thread(target=self._run_agent, daemon=True).start()

    def _set_status(self, text):
        # Called from the agent thread — hop to the UI thread.
        self.root.after(0, lambda: self.status.configure(text=text))

    def _toggle_startup(self):
        try:
            set_startup(self.startup_var.get())
        except Exception as e:
            self._set_status(f"Startup toggle failed: {e}")

    def _run_agent(self):
        asyncio.run(self.agent.run())


def main():
    if not ensure_single_instance():
        # Another agent (service or GUI) is already running — nothing to do.
        return
    # The GUI agent means the user is sitting at this PC already — approval
    # prompts are unnecessary here (and a second Tk interpreter would
    # conflict with this window).
    cfg = load_config()
    cfg["require_approval"] = False
    root = tk.Tk()
    # bundled app icon (works both frozen and from source)
    ico = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) \
        / "sudo.ico"
    if ico.exists():
        root.iconbitmap(str(ico))
    AgentGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
