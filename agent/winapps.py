"""Win32 helpers behind the wm.* and apps.* commands: the Alt-Tab window
list (with icons, monitor and geometry), live window thumbnails, window
state changes, and the Start-menu app list / launcher.

Everything here is Windows-only and BLOCKING — agent.py calls it through
asyncio.to_thread so a hung window or a PowerShell cold start never stalls
the event loop (and with it the screen stream).

Every Win32 function is declared with explicit argtypes/restype on PRIVATE
WinDLL instances. ctypes' defaults pass and return plain C ints, which
truncates 64-bit handles (HICON/HBITMAP/LONG_PTR) on 64-bit Python; and
declaring signatures on the shared ctypes.windll objects instead would leak
into pynput/mss, which call some of the same functions with their own types.
"""

import base64
import ctypes
import io
import json
import os
import subprocess
import threading
import time
from ctypes import wintypes

import mss
import psutil
from PIL import Image, ImageChops

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

HANDLE = wintypes.HANDLE
HWND = wintypes.HWND
HDC = wintypes.HDC
HICON = wintypes.HICON
HBITMAP = wintypes.HBITMAP
HGDIOBJ = wintypes.HGDIOBJ
HMONITOR = wintypes.HMONITOR
HBRUSH = wintypes.HBRUSH
DWORD = wintypes.DWORD
UINT = wintypes.UINT
BOOL = wintypes.BOOL
LONG = wintypes.LONG
WPARAM = wintypes.WPARAM
LPARAM = wintypes.LPARAM
LONG_PTR = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t
LRESULT = LONG_PTR
HRESULT = ctypes.c_long  # checked by hand: ctypes.HRESULT would raise


class RECT(ctypes.Structure):
    _fields_ = [("left", LONG), ("top", LONG),
                ("right", LONG), ("bottom", LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", DWORD), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", DWORD)]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", UINT), ("flags", UINT), ("showCmd", UINT),
                ("ptMinPosition", wintypes.POINT),
                ("ptMaxPosition", wintypes.POINT),
                ("rcNormalPosition", RECT)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", DWORD), ("biWidth", LONG), ("biHeight", LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", DWORD), ("biSizeImage", DWORD),
                ("biXPelsPerMeter", LONG), ("biYPelsPerMeter", LONG),
                ("biClrUsed", DWORD), ("biClrImportant", DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", DWORD * 3)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", LONG), ("bmWidth", LONG), ("bmHeight", LONG),
                ("bmWidthBytes", LONG), ("bmPlanes", wintypes.WORD),
                ("bmBitsPixel", wintypes.WORD), ("bmBits", ctypes.c_void_p)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", BOOL), ("xHotspot", DWORD), ("yHotspot", DWORD),
                ("hbmMask", HBITMAP), ("hbmColor", HBITMAP)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text):
        import uuid
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", DWORD)]


class PROPVARIANT(ctypes.Structure):
    """Just enough of PROPVARIANT to read a VT_LPWSTR: the header and the
    full-width value union (two pointers: 24 bytes on 64-bit, the size the
    callee writes and PropVariantClear expects)."""
    _fields_ = [("vt", wintypes.USHORT), ("wReserved1", wintypes.WORD),
                ("wReserved2", wintypes.WORD), ("wReserved3", wintypes.WORD),
                ("pwszVal", ctypes.c_void_p), ("_union2", ctypes.c_void_p)]


WNDENUMPROC = ctypes.WINFUNCTYPE(BOOL, HWND, LPARAM)


def _sig(fn, restype, *argtypes):
    fn.restype = restype
    fn.argtypes = argtypes


_sig(user32.EnumWindows, BOOL, WNDENUMPROC, LPARAM)
_sig(user32.EnumChildWindows, BOOL, HWND, WNDENUMPROC, LPARAM)
_sig(user32.IsWindow, BOOL, HWND)
_sig(user32.IsWindowVisible, BOOL, HWND)
_sig(user32.IsIconic, BOOL, HWND)
_sig(user32.IsZoomed, BOOL, HWND)
_sig(user32.IsHungAppWindow, BOOL, HWND)
_sig(user32.GetWindow, HWND, HWND, UINT)
_sig(user32.GetAncestor, HWND, HWND, UINT)
_sig(user32.GetShellWindow, HWND)
_sig(user32.GetDesktopWindow, HWND)
_sig(user32.GetWindowLongPtrW, LONG_PTR, HWND, ctypes.c_int)
_sig(user32.GetClassLongPtrW, ULONG_PTR, HWND, ctypes.c_int)
_sig(user32.GetClassNameW, ctypes.c_int, HWND, wintypes.LPWSTR, ctypes.c_int)
_sig(user32.GetWindowTextLengthW, ctypes.c_int, HWND)
_sig(user32.GetWindowTextW, ctypes.c_int, HWND, wintypes.LPWSTR, ctypes.c_int)
_sig(user32.GetWindowThreadProcessId, DWORD, HWND, ctypes.POINTER(DWORD))
_sig(user32.GetForegroundWindow, HWND)
_sig(user32.GetWindowRect, BOOL, HWND, ctypes.POINTER(RECT))
_sig(user32.GetWindowPlacement, BOOL, HWND, ctypes.POINTER(WINDOWPLACEMENT))
_sig(user32.MonitorFromWindow, HMONITOR, HWND, DWORD)
_sig(user32.GetMonitorInfoW, BOOL, HMONITOR, ctypes.POINTER(MONITORINFO))
_sig(user32.SendMessageTimeoutW, LRESULT, HWND, UINT, WPARAM, LPARAM, UINT,
     UINT, ctypes.POINTER(ULONG_PTR))
_sig(user32.PostMessageW, BOOL, HWND, UINT, WPARAM, LPARAM)
_sig(user32.ShowWindow, BOOL, HWND, ctypes.c_int)
_sig(user32.ShowWindowAsync, BOOL, HWND, ctypes.c_int)
_sig(user32.BringWindowToTop, BOOL, HWND)
_sig(user32.SetForegroundWindow, BOOL, HWND)
_sig(user32.AttachThreadInput, BOOL, DWORD, DWORD, BOOL)
_sig(user32.DrawIconEx, BOOL, HDC, ctypes.c_int, ctypes.c_int, HICON,
     ctypes.c_int, ctypes.c_int, UINT, HBRUSH, UINT)
_sig(user32.DestroyIcon, BOOL, HICON)
_sig(user32.GetIconInfo, BOOL, HICON, ctypes.POINTER(ICONINFO))
_sig(user32.PrintWindow, BOOL, HWND, HDC, UINT)

_sig(gdi32.CreateCompatibleDC, HDC, HDC)
_sig(gdi32.CreateDIBSection, HBITMAP, HDC, ctypes.POINTER(BITMAPINFO), UINT,
     ctypes.POINTER(ctypes.c_void_p), HANDLE, DWORD)
_sig(gdi32.SelectObject, HGDIOBJ, HDC, HGDIOBJ)
_sig(gdi32.DeleteObject, BOOL, HGDIOBJ)
_sig(gdi32.DeleteDC, BOOL, HDC)
_sig(gdi32.GdiFlush, BOOL)
_sig(gdi32.GetObjectW, ctypes.c_int, HANDLE, ctypes.c_int, ctypes.c_void_p)
_sig(gdi32.GetDIBits, ctypes.c_int, HDC, HBITMAP, UINT, UINT, ctypes.c_void_p,
     ctypes.POINTER(BITMAPINFO), UINT)

_sig(kernel32.OpenProcess, HANDLE, DWORD, BOOL, DWORD)
_sig(kernel32.CloseHandle, BOOL, HANDLE)
_sig(kernel32.QueryFullProcessImageNameW, BOOL, HANDLE, DWORD,
     wintypes.LPWSTR, ctypes.POINTER(DWORD))
_sig(kernel32.GetApplicationUserModelId, LONG, HANDLE,
     ctypes.POINTER(ctypes.c_uint32), wintypes.LPWSTR)
_sig(kernel32.GetCurrentThreadId, DWORD)

_sig(shell32.SHDefExtractIconW, HRESULT, wintypes.LPCWSTR, ctypes.c_int, UINT,
     ctypes.POINTER(HICON), ctypes.POINTER(HICON), UINT)
_sig(shell32.SHCreateItemFromParsingName, HRESULT, wintypes.LPCWSTR,
     ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
_sig(shell32.SHGetPropertyStoreForWindow, HRESULT, HWND, ctypes.POINTER(GUID),
     ctypes.POINTER(ctypes.c_void_p))

_sig(ole32.CoInitializeEx, HRESULT, ctypes.c_void_p, DWORD)
_sig(ole32.CoUninitialize, None)
_sig(ole32.PropVariantClear, HRESULT, ctypes.POINTER(PROPVARIANT))

_sig(dwmapi.DwmGetWindowAttribute, HRESULT, HWND, DWORD, ctypes.c_void_p,
     DWORD)

GW_OWNER = 4
GA_ROOT = 2
GWL_EXSTYLE = -20
GCLP_HICON = -14
GCLP_HICONSM = -34
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
MONITOR_DEFAULTTONEAREST = 2
WM_CLOSE = 0x0010
WM_GETICON = 0x007F
ICON_SMALL, ICON_BIG, ICON_SMALL2 = 0, 1, 2
SMTO_ABORTIFHUNG = 0x0002
SMTO_ERRORONEXIT = 0x0020
SW_MAXIMIZE, SW_MINIMIZE, SW_RESTORE = 3, 6, 9
DI_NORMAL = 0x0003
PW_RENDERFULLCONTENT = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_INSUFFICIENT_BUFFER = 122
COINIT_APARTMENTTHREADED = 0x2
SIIGBF_ICONONLY = 0x4
VT_LPWSTR = 31

IID_IShellItemImageFactory = GUID.parse(
    "bcc18b79-ba16-442f-80c4-8a59c30c463b")
IID_IPropertyStore = GUID.parse("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")
PKEY_AppUserModel_ID = PROPERTYKEY(
    GUID.parse("9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3"), 5)

# The dock/switcher shows icons at ~40dp; 48px stays crisp on most phones
# while keeping each PNG to a few KB.
ICON_PX = 48


# ---------------------------------------------------------------- helpers

class _Com:
    """CoInitializeEx for the current worker thread, balanced on exit. The
    shell image factory needs COM, and asyncio.to_thread hands us arbitrary
    pool threads, so every call that touches COM brackets itself."""

    def __enter__(self):
        hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        # S_OK and S_FALSE (already initialized) both need a matching
        # CoUninitialize; RPC_E_CHANGED_MODE (thread is MTA) does not, and
        # COM is usable there anyway.
        self._balanced = hr in (0, 1)
        return self

    def __exit__(self, *exc):
        if self._balanced:
            ole32.CoUninitialize()


def _com_method(ptr, index, restype, *argtypes):
    """Bound method `index` of a raw COM interface pointer: no comtypes
    dependency for the two vtable calls this module needs."""
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0]
    fn_addr = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[index]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return lambda *args: proto(fn_addr)(ptr, *args)


def _com_release(ptr):
    _com_method(ptr, 2, ctypes.c_ulong)()  # IUnknown::Release


def _dib_info(w, h):
    """32-bit TOP-DOWN DIB header (negative height), so rows come out in
    the order PIL expects with no flip."""
    bmi = BITMAPINFO()
    hdr = bmi.bmiHeader
    hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    hdr.biWidth = w
    hdr.biHeight = -h
    hdr.biPlanes = 1
    hdr.biBitCount = 32
    hdr.biCompression = 0  # BI_RGB
    return bmi


def _png_b64(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _window_text(hwnd):
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value.strip()


def _class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _window_pid(hwnd):
    pid = DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _process_info(pid):
    """(exe full path or "", packaged-app AUMID or None) for a pid.
    PROCESS_QUERY_LIMITED_INFORMATION is enough for both and is granted
    even for elevated processes, unlike psutil's fuller open."""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return "", None
    try:
        size = DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        exe = buf.value if kernel32.QueryFullProcessImageNameW(
            h, 0, buf, ctypes.byref(size)) else ""
        # Packaged (Store / MSIX) apps keep their real icon in the package
        # assets, not in the exe — their AUMID lets the shell find it.
        aumid = None
        n = ctypes.c_uint32(0)
        if kernel32.GetApplicationUserModelId(
                h, ctypes.byref(n), None) == ERROR_INSUFFICIENT_BUFFER:
            abuf = ctypes.create_unicode_buffer(n.value)
            if kernel32.GetApplicationUserModelId(
                    h, ctypes.byref(n), abuf) == 0:
                aumid = abuf.value or None
        return exe, aumid
    finally:
        kernel32.CloseHandle(h)


def _window_aumid(hwnd):
    """The AppUserModelID a window carries in its shell property store (what
    the taskbar groups it by), or None. The caller must have COM
    initialized (see _Com)."""
    store = ctypes.c_void_p()
    if shell32.SHGetPropertyStoreForWindow(
            hwnd, ctypes.byref(IID_IPropertyStore),
            ctypes.byref(store)) != 0 or not store.value:
        return None
    try:
        get_value = _com_method(store.value, 5, HRESULT,  # ::GetValue
                                ctypes.POINTER(PROPERTYKEY),
                                ctypes.POINTER(PROPVARIANT))
        pv = PROPVARIANT()
        if get_value(ctypes.byref(PKEY_AppUserModel_ID),
                     ctypes.byref(pv)) != 0:
            return None
        try:
            if pv.vt == VT_LPWSTR and pv.pwszVal:
                return ctypes.wstring_at(pv.pwszVal) or None
            return None
        finally:
            ole32.PropVariantClear(ctypes.byref(pv))
    finally:
        _com_release(store.value)


def _frame_hosted_pid(hwnd, host_pid, top_windows):
    """UWP windows are a frame owned by ApplicationFrameHost.exe wrapping a
    CoreWindow child that belongs to the real app process. Returns that
    app's pid (or None) so the list shows "CalculatorApp.exe" with its own
    icon instead of the generic frame host. Needs COM (see _Com)."""
    found = []

    @WNDENUMPROC
    def enum_child(child, _lparam):
        pid = _window_pid(child)
        if pid and pid != host_pid:
            found.append(pid)
            return False
        return True

    user32.EnumChildWindows(hwnd, enum_child, 0)
    if found:
        return found[0]
    # A minimized (or suspended) UWP app's CoreWindow is DETACHED from its
    # frame and lives on as a top-level window of its own — without this,
    # every minimized Settings/Calculator window would read as
    # "ApplicationFrameHost.exe" and share one cached icon. The frame still
    # carries the app's AUMID; the CoreWindow whose process has the same
    # AUMID is the app.
    aumid = _window_aumid(hwnd)
    if not aumid:
        return None
    for other in top_windows:
        if _class_name(other) != "Windows.UI.Core.CoreWindow":
            continue
        pid = _window_pid(other)
        if pid and pid != host_pid:
            app_aumid = _process_info(pid)[1]
            if app_aumid and app_aumid.casefold() == aumid.casefold():
                return pid
    return None


def _is_switchable(hwnd):
    """Top-level, user-facing windows — what Alt-Tab would show."""
    if not user32.IsWindowVisible(hwnd):
        return False
    if user32.GetWindow(hwnd, GW_OWNER):
        return False  # dialogs/tooltips, not top-level apps
    if user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
        return False
    cloaked = DWORD(0)
    dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked),
                                 ctypes.sizeof(cloaked))
    if cloaked.value:
        return False  # UWP frame on another virtual desktop / suspended
    return True


def _frame_rect(hwnd):
    """The visible frame in physical screen pixels. GetWindowRect includes
    the invisible ~7px resize borders Windows 10+ draws around every
    window; DWM's extended frame bounds are what the user actually sees."""
    r = RECT()
    if dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r),
            ctypes.sizeof(r)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r


def _monitor_rects():
    """Monitor rects in mss order — the same numbering screen capture
    uses, so a window's `monitor` can be fed straight back to it. Creating
    the mss instance also makes this process per-monitor DPI aware, so the
    window rects read afterwards are physical pixels like mss's."""
    with mss.mss() as sct:
        return [(m["left"], m["top"], m["width"], m["height"])
                for m in sct.monitors[1:]]


def _monitor_info(hwnd):
    """MONITORINFO of the monitor holding most of the window (for a
    minimized window: where it will restore), or None."""
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if not hmon or not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return None
    return mi


def _monitor_of(hwnd, monitors):
    """(1-based mss monitor index, MONITORINFO) for the window's monitor."""
    mi = _monitor_info(hwnd)
    if mi is None:
        return 1, None
    r = mi.rcMonitor
    key = (r.left, r.top, r.right - r.left, r.bottom - r.top)
    for i, m in enumerate(monitors):
        if m == key:
            return i + 1, mi
    # No exact match (topology changing under us): nearest top-left corner.
    best = min(range(len(monitors)),
               key=lambda i: abs(monitors[i][0] - key[0])
               + abs(monitors[i][1] - key[1]), default=0)
    return best + 1, mi


def _window_rect(hwnd, minimized, mi):
    if minimized:
        # A minimized window sits at -32000,-32000; report where it will
        # restore to. rcNormalPosition is in WORKSPACE coordinates (relative
        # to the work area), so shift by the work area's offset — matters
        # when the taskbar is docked left/top.
        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        user32.GetWindowPlacement(hwnd, ctypes.byref(wp))
        r = wp.rcNormalPosition
        dx = dy = 0
        if mi is not None:
            dx = mi.rcWork.left - mi.rcMonitor.left
            dy = mi.rcWork.top - mi.rcMonitor.top
        return {"left": r.left + dx, "top": r.top + dy,
                "width": r.right - r.left, "height": r.bottom - r.top}
    r = _frame_rect(hwnd)
    return {"left": r.left, "top": r.top,
            "width": r.right - r.left, "height": r.bottom - r.top}


# ---------------------------------------------------------------- icons

# key (AUMID or lower-cased exe path) -> base64 PNG, or None when nothing
# could be rendered (cached too, so a broken app doesn't cost a retry on
# every list). Plain dict: concurrent fills of one key are harmless.
_icon_cache = {}


def _icon_size(hicon):
    """Native pixel size of an HICON (0 if unknown). GetIconInfo hands back
    COPIES of the icon's bitmaps, which the caller must delete."""
    ii = ICONINFO()
    if not user32.GetIconInfo(hicon, ctypes.byref(ii)):
        return 0
    try:
        bm = BITMAP()
        src = ii.hbmColor or ii.hbmMask
        if not src or not gdi32.GetObjectW(src, ctypes.sizeof(bm),
                                           ctypes.byref(bm)):
            return 0
        return bm.bmWidth
    finally:
        if ii.hbmColor:
            gdi32.DeleteObject(ii.hbmColor)
        if ii.hbmMask:
            gdi32.DeleteObject(ii.hbmMask)


def _render_icon(hicon, size=ICON_PX):
    """HICON -> straight-alpha RGBA PIL image via GDI.

    The icon is drawn twice, onto black and onto white 32-bit DIBs. The
    difference between the two passes IS the transparency (white - black =
    (1 - alpha) * 255) and the black pass is the colour premultiplied by
    alpha — so 32-bit alpha icons and legacy AND-mask icons both come out
    right, with no guessing about which kind the app handed us."""
    hdc = gdi32.CreateCompatibleDC(None)
    if not hdc:
        return None
    passes = []
    try:
        for fill in (0x00, 0xFF):
            bits = ctypes.c_void_p()
            bmi = _dib_info(size, size)
            hbm = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0,
                                         ctypes.byref(bits), None, 0)
            if not hbm:
                return None
            old = gdi32.SelectObject(hdc, hbm)
            try:
                ctypes.memset(bits, fill, size * size * 4)
                if not user32.DrawIconEx(hdc, 0, 0, hicon, size, size, 0,
                                         None, DI_NORMAL):
                    return None
                gdi32.GdiFlush()  # DIB bits are only coherent after a flush
                passes.append(ctypes.string_at(bits, size * size * 4))
            finally:
                gdi32.SelectObject(hdc, old)
                gdi32.DeleteObject(hbm)
    finally:
        gdi32.DeleteDC(hdc)
    black = Image.frombuffer("RGB", (size, size), passes[0], "raw", "BGRX",
                             0, 1)
    white = Image.frombuffer("RGB", (size, size), passes[1], "raw", "BGRX",
                             0, 1)
    alpha = ImageChops.invert(ImageChops.subtract(white, black).convert("L"))
    if alpha.getextrema()[1] == 0:
        return None  # nothing was drawn
    r, g, b = black.split()
    return Image.merge("RGBa", (r, g, b, alpha)).convert("RGBA")


def _shell_icon(parsing_name, size=ICON_PX):
    """Icon of any shell item ("shell:AppsFolder\\<AppID>", a file path)
    via IShellItemImageFactory — the same artwork Start and the taskbar
    show, including packaged (Store) app logos that no exe contains.
    The caller must have COM initialized (see _Com)."""
    unk = ctypes.c_void_p()
    hr = shell32.SHCreateItemFromParsingName(
        parsing_name, None, ctypes.byref(IID_IShellItemImageFactory),
        ctypes.byref(unk))
    if hr != 0 or not unk.value:
        return None
    hbm = HBITMAP()
    try:
        get_image = _com_method(unk.value, 3, HRESULT, wintypes.SIZE,
                                ctypes.c_int, ctypes.POINTER(HBITMAP))
        hr = get_image(wintypes.SIZE(size, size), SIIGBF_ICONONLY,
                       ctypes.byref(hbm))
    finally:
        _com_release(unk.value)
    if hr != 0 or not hbm.value:
        return None
    try:
        return _bitmap_to_image(hbm.value)
    finally:
        gdi32.DeleteObject(hbm.value)


def _bitmap_to_image(hbm):
    """32-bit HBITMAP (premultiplied BGRA, as the shell returns it) ->
    straight-alpha RGBA PIL image."""
    bm = BITMAP()
    if not gdi32.GetObjectW(hbm, ctypes.sizeof(bm), ctypes.byref(bm)):
        return None
    w, h = bm.bmWidth, abs(bm.bmHeight)
    if w <= 0 or h <= 0:
        return None
    buf = ctypes.create_string_buffer(w * h * 4)
    bmi = _dib_info(w, h)
    hdc = gdi32.CreateCompatibleDC(None)
    try:
        if gdi32.GetDIBits(hdc, hbm, 0, h, buf, ctypes.byref(bmi), 0) != h:
            return None
    finally:
        gdi32.DeleteDC(hdc)
    img = Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRa", 0, 1)
    if img.getchannel("A").getextrema()[1] == 0:
        return None
    return img


def _window_icon_handle(hwnd, big):
    """The icon a window itself advertises (not owned by us: never destroy
    it). WM_GETICON goes through SendMessageTimeout so a hung app can't
    hang the list."""
    result = ULONG_PTR(0)
    kinds = (ICON_BIG,) if big else (ICON_SMALL2, ICON_SMALL)
    for kind in kinds:
        if user32.SendMessageTimeoutW(
                hwnd, WM_GETICON, kind, 0,
                SMTO_ABORTIFHUNG | SMTO_ERRORONEXIT, 50,
                ctypes.byref(result)) and result.value:
            return result.value
    icon = user32.GetClassLongPtrW(hwnd, GCLP_HICON if big else GCLP_HICONSM)
    return icon or None


def _exe_icon(exe, size=ICON_PX):
    """The exe's main icon extracted AT the requested size from its
    resources — crisp, unlike stretching a 32px window icon to 48."""
    large = HICON()
    hr = shell32.SHDefExtractIconW(exe, 0, 0, ctypes.byref(large), None,
                                   size & 0xFFFF)
    if hr != 0 or not large.value:
        return None
    try:
        return _render_icon(large.value, size)
    finally:
        user32.DestroyIcon(large.value)


def _window_icon(hwnd, exe, aumid):
    """Base64 PNG icon for a window, cached by app (AUMID / exe path).

    Order: a packaged app's shell logo; the window's own big icon when it
    is already ~48px; the exe's icon extracted at 48px; finally any small
    window icon, stretched. Every failure falls through, never raises."""
    key = aumid or (exe.lower() if exe else None)
    if key and key in _icon_cache:
        return _icon_cache[key]
    img = None
    if aumid:
        img = _shell_icon("shell:AppsFolder\\" + aumid)
    big = None
    if img is None:
        big = _window_icon_handle(hwnd, True)
        if big and _icon_size(big) >= ICON_PX * 5 // 6:
            img = _render_icon(big)
    if img is None and exe:
        img = _exe_icon(exe)
    if img is None:
        handle = big or _window_icon_handle(hwnd, False)
        if handle:
            img = _render_icon(handle)
    png = _png_b64(img) if img is not None else None
    if key:
        _icon_cache[key] = png
    return png


# ---------------------------------------------------------------- windows

def _top_windows():
    """Every top-level window handle in Z-order (EnumWindows order: topmost
    / most recently active first). Collected first, inspected after:
    exceptions inside a ctypes callback are swallowed, and a trivial
    callback keeps EnumWindows quick."""
    hwnds = []

    @WNDENUMPROC
    def enum_proc(hwnd, _lparam):
        hwnds.append(hwnd)
        return True

    user32.EnumWindows(enum_proc, 0)
    return [h for h in hwnds if h]


def list_windows(icons=True):
    """Alt-Tab style window list in Z-order, each with the app icon, which
    monitor it is on (mss numbering), its frame rect and state flags."""
    monitors = _monitor_rects()
    foreground = user32.GetForegroundWindow()
    windows = []
    with _Com():
        top_windows = _top_windows()
        for hwnd in top_windows:
            if not _is_switchable(hwnd):
                continue
            title = _window_text(hwnd)
            if not title or title == "Program Manager":
                continue
            pid = _window_pid(hwnd)
            exe, aumid = _process_info(pid)
            if os.path.basename(exe).lower() == "applicationframehost.exe":
                real = _frame_hosted_pid(hwnd, pid, top_windows)
                if real:
                    pid = real
                    exe, aumid = _process_info(pid)
            process = os.path.basename(exe)
            if not process:
                try:
                    process = psutil.Process(pid).name()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    process = ""
            minimized = bool(user32.IsIconic(hwnd))
            monitor, mi = _monitor_of(hwnd, monitors)
            win = {
                "id": int(hwnd), "title": title, "pid": pid,
                "process": process, "exe": exe,
                "active": hwnd == foreground,
                "minimized": minimized,
                "maximized": bool(user32.IsZoomed(hwnd)),
                "monitor": monitor,
                "rect": _window_rect(hwnd, minimized, mi),
            }
            if icons:
                try:
                    win["icon"] = _window_icon(hwnd, exe, aumid)
                except Exception:
                    win["icon"] = None  # an icon must never break the list
            windows.append(win)
    return {"windows": windows}


def _hwnd(window_id):
    try:
        hwnd = int(window_id)
    except (TypeError, ValueError):
        raise ValueError(f"bad window id: {window_id!r}") from None
    if hwnd <= 0:
        raise ValueError(f"bad window id: {window_id!r}")
    return hwnd


def _top_level(window_id):
    """Validates a wm.list id: a live top-level window that is not part of
    the shell itself (WM_CLOSE to the desktop opens the Shut Down dialog)."""
    hwnd = _hwnd(window_id)
    if not user32.IsWindow(hwnd):
        raise ValueError("window no longer exists")
    if user32.GetAncestor(hwnd, GA_ROOT) != hwnd:
        raise ValueError("not a top-level window")
    if hwnd in (user32.GetShellWindow(), user32.GetDesktopWindow()) or \
            _class_name(hwnd) in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                                  "Progman", "WorkerW"):
        raise ValueError("refusing to touch the Windows shell")
    return hwnd


# PrintWindow allocates a full-size DIB per call (8 MB for a 1080p window):
# cap concurrent captures so a phone requesting a whole grid of thumbnails
# at once can't spike memory or starve the screen stream of CPU.
_thumb_slots = threading.BoundedSemaphore(2)


def window_thumb(window_id, max_width=360):
    """Live thumbnail of one window, even when it is covered by others:
    PrintWindow(PW_RENDERFULLCONTENT) asks DWM for the window's own
    redirection surface instead of reading screen pixels, so overlapping
    windows don't show through (and GPU-rendered apps like browsers aren't
    black, as with plain PrintWindow). Minimized windows have no surface:
    jpeg is null and w/h describe the restored size, so the phone can keep
    the card's aspect ratio and show the big icon instead."""
    hwnd = _top_level(window_id)
    max_width = max(64, min(int(max_width), 1280))

    def fit(w, h):
        scale = min(1.0, max_width / max(1, w), 2 * max_width / max(1, h))
        return max(1, round(w * scale)), max(1, round(h * scale))

    if user32.IsIconic(hwnd) or user32.IsHungAppWindow(hwnd):
        r = _window_rect(hwnd, bool(user32.IsIconic(hwnd)),
                         _monitor_info(hwnd))
        w, h = fit(r["width"], r["height"])
        return {"jpeg": None, "w": w, "h": h}
    wr = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(wr)):
        raise ValueError("window no longer exists")
    ww, wh = wr.right - wr.left, wr.bottom - wr.top
    if ww <= 0 or wh <= 0 or ww * wh > 8192 * 8192:
        return {"jpeg": None, "w": 0, "h": 0}
    # PrintWindow renders the whole window rect, invisible resize borders
    # included (they come out black): crop to the visible frame.
    fr = _frame_rect(hwnd)
    box = (max(0, fr.left - wr.left), max(0, fr.top - wr.top),
           min(ww, fr.right - wr.left), min(wh, fr.bottom - wr.top))
    if box[2] - box[0] <= 0 or box[3] - box[1] <= 0:
        box = (0, 0, ww, wh)
    # Bounded wait: if both slots are held by captures of a window that hung
    # mid-call, fail this request instead of parking yet another worker
    # thread forever.
    if not _thumb_slots.acquire(timeout=5):
        raise TimeoutError("thumbnail capture busy")
    try:
        hdc = gdi32.CreateCompatibleDC(None)
        if not hdc:
            raise OSError("CreateCompatibleDC failed")
        try:
            bits = ctypes.c_void_p()
            bmi = _dib_info(ww, wh)
            hbm = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0,
                                         ctypes.byref(bits), None, 0)
            if not hbm:
                raise OSError("CreateDIBSection failed")
            old = gdi32.SelectObject(hdc, hbm)
            try:
                if not user32.PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT):
                    return {"jpeg": None, "w": 0, "h": 0}
                gdi32.GdiFlush()
                # Wrap the DIB's memory without copying; the resize reads it
                # and produces a new image before the DIB is freed.
                raw = (ctypes.c_char * (ww * wh * 4)).from_address(bits.value)
                src = Image.frombuffer("RGB", (ww, wh), raw, "raw", "BGRX",
                                       0, 1)
                tw, th = fit(box[2] - box[0], box[3] - box[1])
                # box= scales straight from the visible region (a separate
                # crop would copy 8 MB first, ~11ms); reducing_gap does a
                # cheap box pre-shrink before the bilinear pass, ~2.5x
                # faster from 1080p to card size with the same look.
                img = src.resize((tw, th), Image.BILINEAR, box=box,
                                 reducing_gap=2.0)
            finally:
                gdi32.SelectObject(hdc, old)
                gdi32.DeleteObject(hbm)
        finally:
            gdi32.DeleteDC(hdc)
    finally:
        _thumb_slots.release()
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return {"jpeg": base64.b64encode(buf.getvalue()).decode(),
            "w": img.width, "h": img.height}


def activate_window(window_id):
    """Bring a window to the foreground. Windows enforces a "foreground
    lock" that blocks a background process from stealing focus outright;
    briefly attaching this thread's input queue to both the current
    foreground window's and the target's threads is the standard way
    around it."""
    hwnd = _hwnd(window_id)
    if not user32.IsWindow(hwnd):
        raise ValueError("window no longer exists")
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)

    fg_hwnd = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None) \
        if fg_hwnd else 0
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    current_thread = kernel32.GetCurrentThreadId()

    attached_fg = attached_target = False
    if fg_thread and fg_thread != current_thread:
        attached_fg = bool(
            user32.AttachThreadInput(current_thread, fg_thread, True))
    if target_thread and target_thread != current_thread:
        attached_target = bool(
            user32.AttachThreadInput(current_thread, target_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached_fg:
            user32.AttachThreadInput(current_thread, fg_thread, False)
        if attached_target:
            user32.AttachThreadInput(current_thread, target_thread, False)
    return {"activated": True}


def close_window(window_id):
    """Polite close, exactly like clicking X: WM_CLOSE is POSTED (never
    sent — a modal "Save changes?" prompt would block us), and the app is
    free to ask the user or refuse."""
    hwnd = _top_level(window_id)
    if not user32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
        raise OSError(f"WM_CLOSE failed ({ctypes.get_last_error()})")
    return {"close_requested": True}


def _show(window_id, cmd):
    # ShowWindowAsync: a hung target can't block us (plain ShowWindow on
    # another process's window waits for its message loop).
    hwnd = _top_level(window_id)
    user32.ShowWindowAsync(hwnd, cmd)
    return hwnd


def minimize_window(window_id):
    _show(window_id, SW_MINIMIZE)
    return {"minimized": True}


def maximize_window(window_id):
    _show(window_id, SW_MAXIMIZE)
    return {"maximized": True}


def restore_window(window_id):
    _show(window_id, SW_RESTORE)
    return {"restored": True}


# ---------------------------------------------------------------- start apps

APPS_TTL = 600  # s; installs are rare, a PowerShell start is 1-4s
APPS_RETRY = 60  # s between BACKGROUND attempts, so a broken PowerShell
                 # isn't respawned for every apps.icon row

# The agent handles one phone's commands strictly in order, so a cold
# Get-StartApps (PowerShell start-up: 1-4 s) would stall everything queued
# behind it — the switcher's thumbnails, a tap to switch. So the list is
# loaded in the background as soon as the phone lists windows
# (prefetch_start_apps), and once loaded an expired list is served at once
# while a background reload replaces it. Only the very first load and an
# explicit refresh make a caller wait.
_apps_lock = threading.Lock()  # one Get-StartApps at a time
_apps = None       # (loaded_at, list, by_id) — replaced whole, never mutated
_apps_tried = 0.0  # monotonic time of the last background attempt
_app_icon_cache = {}  # AppID -> base64 PNG or None


def _query_start_apps():
    """Get-StartApps lists exactly what Start's "All apps" shows: Win32
    shortcuts AND packaged Store apps, each with the AppID that
    shell:AppsFolder launches."""
    script = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
              "Get-StartApps | Select-Object Name,AppID"
              " | ConvertTo-Json -Compress")
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out = proc.stdout.decode("utf-8-sig", "replace").strip()
    if proc.returncode != 0 or not out:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"Get-StartApps failed: {err[:200] or 'no output'}")
    items = json.loads(out)
    if isinstance(items, dict):  # ConvertTo-Json unwraps a single result
        items = [items]
    apps, seen = [], set()
    for it in items:
        app_id = str(it.get("AppID") or "").strip()
        name = str(it.get("Name") or "").strip()
        if not app_id or not name or app_id in seen:
            continue
        # Start-menu clutter that isn't an app: uninstallers and web links.
        if name.lower().startswith("uninstall") or \
                app_id.lower().startswith(("http://", "https://")):
            continue
        seen.add(app_id)
        apps.append({"id": app_id, "name": name})
    apps.sort(key=lambda a: a["name"].casefold())
    return apps


def _load_start_apps():
    """Runs Get-StartApps and publishes the result. Hold _apps_lock."""
    global _apps
    apps = _query_start_apps()
    _apps = (time.monotonic(), apps, {a["id"]: a for a in apps})


def _reload_in_background():
    if not _apps_lock.acquire(blocking=False):
        return  # a load is already running
    try:
        _load_start_apps()
    except Exception as e:
        # The previous list (if any) stays; the next caller retries.
        print(f"[agent] Start-menu app list reload failed: {e}")
    finally:
        _apps_lock.release()


def _schedule_reload():
    global _apps_tried
    now = time.monotonic()
    if _apps_lock.locked() or now - _apps_tried < APPS_RETRY:
        return
    _apps_tried = now
    threading.Thread(target=_reload_in_background, daemon=True).start()


def prefetch_start_apps():
    """Starts loading the Start-menu list in the background unless it is
    loaded already (then a cheap no-op). Called whenever the phone lists
    windows: the switcher's Launch tab asks for the apps right after."""
    if _apps is None:
        _schedule_reload()


def _start_apps(refresh=False):
    """(list, by_id) of the Start-menu apps. Waits only for the first load
    or an explicit refresh; an expired list is returned at once and
    reloaded in the background."""
    asked = time.monotonic()
    snap = _apps
    if snap is not None and not refresh:
        if asked - snap[0] > APPS_TTL:
            _schedule_reload()
        return snap[1], snap[2]
    with _apps_lock:
        # A load that finished while we waited for the lock (the prefetch,
        # or another phone's refresh) is as fresh as one we'd start now.
        if _apps is None or (refresh and _apps[0] < asked):
            _load_start_apps()
        snap = _apps
    return snap[1], snap[2]


def list_start_apps(refresh=False):
    """Installed apps for the launcher. Icons are only included when
    already cached — rendering ~150 of them up front would make the list
    slow, so the phone fetches missing ones lazily with apps.icon."""
    apps, _ = _start_apps(refresh)
    out = []
    for a in apps:
        item = dict(a)
        icon = _app_icon_cache.get(a["id"])
        if icon:
            item["icon"] = icon
        out.append(item)
    return {"apps": out}


def _known_app(app_id):
    app_id = str(app_id or "")
    _, by_id = _start_apps()
    app = by_id.get(app_id)
    if app is None:
        raise ValueError("unknown app id (refresh the app list)")
    return app


def start_app_icon(app_id):
    app = _known_app(app_id)
    if app["id"] not in _app_icon_cache:
        with _Com():
            img = _shell_icon("shell:AppsFolder\\" + app["id"])
        _app_icon_cache[app["id"]] = _png_b64(img) if img is not None \
            else None
    return {"id": app["id"], "icon": _app_icon_cache[app["id"]]}


def _switchable_windows():
    """Alt-Tab windows in Z-order (topmost first), handles only."""
    return [h for h in _top_windows() if _is_switchable(h) and _window_text(h)]


def _focus_new_window(before, timeout=8.0):
    """Explorer starts the app on behalf of a background process (us), so
    Windows' foreground lock may open it BEHIND the current window with
    just a flashing taskbar button — useless to someone looking at the PC
    through the phone. Watch for the first new top-level window and bring
    it forward. An app that was already running opens no new window; then
    this simply times out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.25)
        new = [h for h in _switchable_windows() if h not in before]
        if new:
            if user32.GetForegroundWindow() != new[0]:
                try:
                    activate_window(new[0])
                except Exception:
                    pass  # it closed again (splash screen) — nothing to do
            return


def launch_start_app(app_id):
    """Starts a Start-menu app by AppID. The id must come from the cached
    Get-StartApps list (never an arbitrary string from the wire), and it is
    passed as ONE argv element — no shell, so nothing in it is ever
    interpreted as a command."""
    app = _known_app(app_id)
    before = set(_switchable_windows())
    subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + app["id"]],
                     close_fds=True)
    threading.Thread(target=_focus_new_window, args=(before,),
                     daemon=True).start()
    return {"launched": True, "id": app["id"], "name": app["name"]}
