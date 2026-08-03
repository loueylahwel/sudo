"""Connection approval dialog for the agent.

When an unknown phone tries to pair, this pops a small always-on-top window
on the PC: Allow / Deny with a countdown. Approved phones get a token the
agent stores, so the prompt only ever appears once per phone.

THREADING RULE (same as the viewer): Tk objects live ONLY on the manager's
dedicated thread. asyncio callers hand requests in through a thread-safe
queue; decisions go back via loop.call_soon_threadsafe.
"""

import asyncio
import queue
import threading
import tkinter as tk

TIMEOUT_S = 30


class ApprovalManager:
    def __init__(self):
        self._thread = None
        self._requests = queue.Queue()
        self._loop = None
        self._root = None
        self._dialog = None

    def start(self, loop):
        if self._thread is None:
            self._loop = loop
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def ask(self, client_desc):
        """asyncio-side: returns a Future -> True (allow) / False (deny)."""
        fut = self._loop.create_future()
        self._requests.put((client_desc, fut))
        return fut

    # ---- everything below runs on the Tk thread only ----

    def _run(self):
        root = tk.Tk()
        self._root = root
        root.withdraw()
        root.title("Sudo")
        try:
            import sys
            from pathlib import Path
            ico = Path(getattr(sys, "_MEIPASS",
                               Path(__file__).resolve().parent)) / "sudo.ico"
            if ico.exists():
                root.iconbitmap(str(ico))
        except Exception:
            pass
        root.after(50, self._poll)
        root.mainloop()

    def _poll(self):
        try:
            while True:
                desc, fut = self._requests.get_nowait()
                if self._dialog is not None:
                    self._requests.put((desc, fut))  # busy: retry next tick
                else:
                    self._show(desc, fut)
        except queue.Empty:
            pass
        self._root.after(50, self._poll)

    def _show(self, desc, fut):
        dlg = tk.Toplevel(self._root)
        self._dialog = dlg
        dlg.title("Sudo — connection request")
        dlg.configure(bg="#0f0f0f", padx=24, pady=18)
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        try:
            import sys
            from pathlib import Path
            ico = Path(getattr(sys, "_MEIPASS",
                               Path(__file__).resolve().parent)) / "sudo.ico"
            if ico.exists():
                dlg.iconbitmap(str(ico))
        except Exception:
            pass

        tk.Label(dlg, text=f"'{desc}' wants to connect", bg="#0f0f0f",
                 fg="#f3f4f6", font=("Segoe UI", 14, "bold"),
                 wraplength=320, justify="left").pack()
        tk.Label(dlg, text="Allow it to control this PC?",
                 bg="#0f0f0f", fg="#9ca3af", font=("Segoe UI", 10),
                 justify="left").pack(pady=(6, 14))

        countdown = tk.Label(dlg, text=f"Auto-deny in {TIMEOUT_S}s",
                             bg="#0f0f0f", fg="#6b7280",
                             font=("Segoe UI", 9))
        countdown.pack(pady=(0, 10))

        def finish(allowed):
            if self._dialog is None:
                return
            self._dialog = None
            dlg.destroy()
            self._loop.call_soon_threadsafe(
                lambda: not fut.done() and fut.set_result(allowed))

        row = tk.Frame(dlg, bg="#0f0f0f")
        row.pack()
        tk.Button(row, text="Allow", width=10, bg="#0d9488", fg="white",
                  relief="flat", font=("Segoe UI", 10, "bold"),
                  command=lambda: finish(True)).pack(side="left", padx=6)
        tk.Button(row, text="Deny", width=10, bg="#3f3f46", fg="white",
                  relief="flat", font=("Segoe UI", 10),
                  command=lambda: finish(False)).pack(side="left", padx=6)

        remaining = [TIMEOUT_S]

        def tick():
            if self._dialog is None:
                return
            remaining[0] -= 1
            if remaining[0] <= 0:
                finish(False)
                return
            countdown.configure(text=f"Auto-deny in {remaining[0]}s")
            dlg.after(1000, tick)

        dlg.after(1000, tick)
        dlg.protocol("WM_DELETE_WINDOW", lambda: finish(False))
        dlg.lift()
        dlg.focus_force()
