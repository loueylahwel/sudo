"""Unlock the PC's screen from the phone with the PC user's own password
(Linux, logind; session.status / session.unlock in agent.py).

The password is checked with PAM, through the same "kde" service the Plasma
lock screen uses (Ubuntu has no /etc/pam.d/kde, so it falls back to
"other" -> common-auth -> pam_unix, whose setgid unix_chkpwd helper lets a
user check their own password). Only a correct one leads to `loginctl
unlock-session`, which the lock screen obeys. A screen locker's own grab
never sees synthetic keys this way, so nothing is typed anywhere.

The password is used for that one check and dropped: never logged, never
stored. The agent only accepts it over TLS (see h_session_unlock).
"""

import ctypes
import ctypes.util
import getpass
import os
import subprocess

PAM_SERVICE = "kde"

PAM_PROMPT_ECHO_OFF = 1
PAM_PROMPT_ECHO_ON = 2
PAM_SUCCESS = 0


class _Message(ctypes.Structure):
    _fields_ = [("msg_style", ctypes.c_int), ("msg", ctypes.c_char_p)]


class _Response(ctypes.Structure):
    _fields_ = [("resp", ctypes.c_char_p), ("resp_retcode", ctypes.c_int)]


_CONV_FUNC = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.POINTER(_Message)),
    ctypes.POINTER(ctypes.POINTER(_Response)), ctypes.c_void_p)


class _Conv(ctypes.Structure):
    _fields_ = [("conv", _CONV_FUNC), ("appdata_ptr", ctypes.c_void_p)]


def _libs():
    pam = ctypes.CDLL(ctypes.util.find_library("pam") or "libpam.so.0")
    libc = ctypes.CDLL(ctypes.util.find_library("c"))
    libc.calloc.restype = ctypes.c_void_p
    libc.calloc.argtypes = [ctypes.c_size_t, ctypes.c_size_t]
    libc.strdup.restype = ctypes.c_void_p
    libc.strdup.argtypes = [ctypes.c_char_p]
    pam.pam_start.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                              ctypes.POINTER(_Conv), ctypes.POINTER(ctypes.c_void_p)]
    pam.pam_authenticate.argtypes = [ctypes.c_void_p, ctypes.c_int]
    pam.pam_end.argtypes = [ctypes.c_void_p, ctypes.c_int]
    return pam, libc


def check_password(password, user=None):
    """True when `password` is the PC user's password (PAM). Slow on
    purpose when wrong (pam_faildelay, ~2-3 s)."""
    pam, libc = _libs()
    user = user or getpass.getuser()
    secret = password.encode("utf-8")

    @_CONV_FUNC
    def conv(n, messages, responses, _data):
        # PAM frees what we hand it: allocate with libc, not Python.
        arr = libc.calloc(n, ctypes.sizeof(_Response))
        if not arr:
            return 5  # PAM_BUF_ERR
        responses[0] = ctypes.cast(arr, ctypes.POINTER(_Response))
        for i in range(n):
            style = messages[i].contents.msg_style
            if style in (PAM_PROMPT_ECHO_OFF, PAM_PROMPT_ECHO_ON):
                responses[0][i].resp = ctypes.cast(
                    libc.strdup(secret), ctypes.c_char_p)
                responses[0][i].resp_retcode = 0
        return PAM_SUCCESS

    handle = ctypes.c_void_p()
    rc = pam.pam_start(PAM_SERVICE.encode(), user.encode(),
                       ctypes.byref(_Conv(conv, None)), ctypes.byref(handle))
    if rc != PAM_SUCCESS:
        raise RuntimeError(f"PAM didn't start ({rc})")
    try:
        rc = pam.pam_authenticate(handle, 0)
    finally:
        pam.pam_end(handle, rc)
    return rc == PAM_SUCCESS


def _session_id():
    """The user's graphical logind session (the agent runs as a systemd
    user service, which has no XDG_SESSION_ID of its own)."""
    sid = os.environ.get("XDG_SESSION_ID")
    if sid:
        return sid
    out = subprocess.run(["loginctl", "show-user", getpass.getuser(),
                          "-p", "Display", "--value"],
                         capture_output=True, text=True, timeout=5).stdout.strip()
    if not out:
        raise RuntimeError("no graphical session found")
    return out


def is_locked():
    out = subprocess.run(["loginctl", "show-session", _session_id(),
                          "-p", "LockedHint", "--value"],
                         capture_output=True, text=True, timeout=5).stdout.strip()
    return out == "yes"


def unlock():
    r = subprocess.run(["loginctl", "unlock-session", _session_id()],
                       capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:200]
                           or "loginctl unlock-session failed")
