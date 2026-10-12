"""Connection approval dialog for the agent.

When an unknown phone tries to pair, this pops a small always-on-top window
on the PC: Allow / Deny with a countdown. Approved phones get a token the
agent stores, so the prompt only ever appears once per phone.

The same Tk thread also shows the "Shutting down in 60s — Cancel" notice
of a power action scheduled from a phone (warn / close_warning).

THREADING RULE (same as the viewer): Tk objects live ONLY on the manager's
dedicated thread. asyncio callers hand requests in through a thread-safe
queue; decisions go back via loop.call_soon_threadsafe.
"""

import asyncio
import math
import queue
import threading
import time
import tkinter as tk

TIMEOUT_S = 30


class ApprovalManager:
    def __init__(self):
        self._thread = None
        self._requests = queue.Queue()
        self._loop = None
        self._root = None
        self._dialog = None
        # Scheduled-power-action notice (warn/close_warning below). Its own
        # queue and slot: it may sit next to an approval dialog.
        self._warnings = queue.Queue()
        self._warning = None  # finish callback of the notice on screen

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

    def warn(self, verb, at, auto_close_s=None):
        """asyncio-side: the T-60s notice of a scheduled power action — an
        always-on-top "<verb> in 60s" countdown to unix time `at` with a
        Cancel button. Returns a Future -> True when Cancel was pressed;
        False when the countdown ran out, the window was closed, a newer
        notice replaced it or close_warning() dismissed it. auto_close_s
        takes it down early (power dry-run: shown, never left waiting)."""
        fut = self._loop.create_future()
        self._warnings.put(("show", (verb, at, auto_close_s), fut))
        return fut

    def close_warning(self):
        """asyncio-side: dismisses the notice, if one is up (the schedule
        was cancelled from a phone, replaced, or has fired)."""
        self._warnings.put(("close", None, None))

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
        self._poll_warnings()
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

    # ---- scheduled power action notice (Tk thread) ----

    def _poll_warnings(self):
        try:
            while True:
                kind, args, fut = self._warnings.get_nowait()
                # One notice at a time: a newer one (rescheduled) or a
                # close replaces whatever is on screen.
                if self._warning is not None:
                    self._warning(False)
                if kind == "show":
                    try:
                        self._show_warning(fut, *args)
                    except Exception as e:
                        # Raising here would end _poll's after() chain, and
                        # with it every later approval prompt: drop just
                        # this notice; the schedule itself is unaffected.
                        print(f"[agent] power notice failed: {e}")
                        if self._warning is not None:
                            self._warning(False)
                        self._loop.call_soon_threadsafe(
                            lambda f=fut: not f.done() and f.set_result(False))
        except queue.Empty:
            pass

    def _show_warning(self, fut, verb, at, auto_close_s):
        dry_run = auto_close_s is not None
        dlg = tk.Toplevel(self._root)
        dlg.withdraw()  # mapped only once it can no longer take focus
        dlg.title("Sudo — scheduled power action"
                  + (" (dry run)" if dry_run else ""))
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

        def left():
            # ceil: "1s" until the very end, closing right at the deadline
            return max(0, math.ceil(at - time.time()))

        headline = tk.Label(dlg, text=f"{verb} in {left()}s", bg="#0f0f0f",
                            fg="#f3f4f6", font=("Segoe UI", 14, "bold"),
                            wraplength=320, justify="left")
        headline.pack()
        detail = ("Dry run: nothing will actually happen." if dry_run
                  else "Scheduled from the Sudo app on your phone. "
                       "Save your work, or cancel it here.")
        tk.Label(dlg, text=detail, bg="#0f0f0f", fg="#9ca3af",
                 font=("Segoe UI", 10), wraplength=320,
                 justify="left").pack(pady=(6, 14))

        def finish(cancelled):
            if self._warning is not finish:
                return  # already closed or replaced
            self._warning = None
            dlg.destroy()
            self._loop.call_soon_threadsafe(
                lambda: not fut.done() and fut.set_result(cancelled))

        self._warning = finish
        tk.Button(dlg, text="Cancel", width=12, bg="#0d9488", fg="white",
                  relief="flat", font=("Segoe UI", 10, "bold"),
                  command=lambda: finish(True)).pack()

        def tick():
            if self._warning is not finish:
                return
            n = left()
            if n <= 0:
                finish(False)
                return
            headline.configure(text=f"{verb} in {n}s")
            dlg.after(500, tick)

        dlg.after(500, tick)
        if auto_close_s:
            dlg.after(int(auto_close_s * 1000), lambda: finish(False))
        # Closing the window only hides the notice; Cancel cancels.
        dlg.protocol("WM_DELETE_WINDOW", lambda: finish(False))
        # Bottom-right, above the taskbar, like a toast.
        dlg.update_idletasks()
        x = dlg.winfo_screenwidth() - dlg.winfo_reqwidth() - 24
        y = dlg.winfo_screenheight() - dlg.winfo_reqheight() - 96
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        self._no_activate(dlg)
        dlg.deiconify()

    @staticmethod
    def _no_activate(win):
        """Unlike the approval dialog (focus_force), the notice must never
        become the active window: it can pop up while the user is typing,
        and a stray Space would press Cancel for them. WS_EX_NOACTIVATE on
        the Tk wrapper keeps it out of the foreground both when it appears
        (Windows lets a process take focus after the user has been idle a
        while) and when its button is clicked — the click still works."""
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.WinDLL("user32")
            user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.GetWindowLongW.restype = wintypes.LONG
            user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int,
                                              wintypes.LONG]
            user32.SetWindowLongW.restype = wintypes.LONG
            hwnd = int(win.wm_frame(), 16)
            GWL_EXSTYLE, WS_EX_NOACTIVATE = -20, 0x08000000
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, user32.GetWindowLongW(
                hwnd, GWL_EXSTYLE) | WS_EX_NOACTIVATE)
        except Exception:
            pass  # not Windows, or no frame yet: a plain topmost window
