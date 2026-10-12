"""Parsec Virtual Display Driver (VDD) client — the virtual monitor behind
"phone as a second monitor".

The signed Parsec VDD creates monitors on demand through IOCTLs on its
device interface. An added display lives only while someone keeps pinging
the driver (IOCTL UPDATE, more often than every ~100ms): if the agent dies,
the pings stop, every display it added is unplugged about a second later
and Windows moves the apps back to the main screen. That crash-safety is
the reason this driver was chosen over a permanent virtual monitor.

Also here: the GDI/CCD display plumbing the phone monitor needs (finding
the added display, setting its mode/orientation/position, per-monitor DPI
scale) and the one-time, user-approved driver install.

All Win32 calls go through private WinDLL instances: setting argtypes on
the shared ctypes.windll objects would silently change the prototypes
agent.py relies on. Everything degrades to "missing" off Windows.

API reference: https://github.com/nomi-san/parsec-vdd (core/parsec-vdd.h).
"""

import ctypes
import http.client
import json
import os
import platform
import subprocess
import threading
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

IS_WINDOWS = platform.system() == "Windows"

# ---------------------------------------------------------------- constants

VDD_ADAPTER_NAME = "Parsec Virtual Display Adapter"
VDD_DISPLAY_ID = "PSCCDD0"           # monitor hardware id (MONITOR\PSCCDD0\...)
VDD_HARDWARE_ID = "Root\\Parsec\\VDA"

VDD_IOCTL_ADD = 0x0022E004           # out: index of the new display
VDD_IOCTL_REMOVE = 0x0022A008        # in: 16-bit index, big-endian
VDD_IOCTL_UPDATE = 0x0022A00C        # keepalive ping
VDD_IOCTL_VERSION = 0x0022E010       # out: driver minor version

KEEPALIVE_S = 0.05                   # driver unplugs after ~100ms of silence
IOCTL_TIMEOUT_MS = 5000

DEFAULT_VERSION = "0.45.0.0"
FALLBACK_VERSION = "0.41.0.0"
INSTALLER_URL = "https://builds.parsec.app/vdd/parsec-vdd-{version}.exe"

# Windows' scale-factor ladder. The undocumented DPI packets speak in
# steps along it, relative to the display's recommended scale.
DPI_VALUES = (100, 125, 150, 175, 200, 225, 250, 300, 350, 400, 450, 500)

DMDO_DEFAULT, DMDO_90, DMDO_180, DMDO_270 = 0, 1, 2, 3

DISP_CHANGE_NAMES = {
    0: "ok", 1: "restart required", -1: "failed", -2: "unsupported mode",
    -3: "could not save settings", -4: "bad flags", -5: "bad parameter",
    -6: "bad dual view",
}

_DIGCF_PRESENT = 0x2
_DIGCF_DEVICEINTERFACE = 0x10
_SPDRP_HARDWAREID = 0x1
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x1
_FILE_SHARE_WRITE = 0x2
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_FILE_FLAG_NO_BUFFERING = 0x20000000
_FILE_FLAG_OVERLAPPED = 0x40000000
_FILE_FLAG_WRITE_THROUGH = 0x80000000
_ERROR_IO_PENDING = 997
_ERROR_INSUFFICIENT_BUFFER = 122
_ERROR_CANCELLED = 1223
_WAIT_TIMEOUT = 258
_INVALID_HANDLE = ctypes.c_void_p(-1).value

_DN_DRIVER_LOADED = 0x2
_DN_STARTED = 0x8
_DN_HAS_PROBLEM = 0x400
_CM_PROB_NEED_RESTART = 14
_CM_PROB_DISABLED = 22
_CM_PROB_HARDWARE_DISABLED = 29
_CM_PROB_DISABLED_SERVICE = 32

_ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
_ENUM_REGISTRY_SETTINGS = 0xFFFFFFFE
_DM_POSITION = 0x20
_DM_DISPLAYORIENTATION = 0x80
_DM_PELSWIDTH = 0x80000
_DM_PELSHEIGHT = 0x100000
_CDS_UPDATEREGISTRY = 0x1
_CDS_NORESET = 0x10000000
_DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x1
_DISPLAY_DEVICE_ACTIVE = 0x1  # same bit, on a MONITOR child: it is in use
_DISPLAY_DEVICE_PRIMARY_DEVICE = 0x4
_EDD_GET_DEVICE_INTERFACE_NAME = 0x1
_SDC_TOPOLOGY_EXTEND = 0x4
_SDC_APPLY = 0x80
_MONITORINFOF_PRIMARY = 0x1

_QDC_ONLY_ACTIVE_PATHS = 0x2
_DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1
_DISPLAYCONFIG_DEVICE_INFO_GET_DPI_SCALE = -3
_DISPLAYCONFIG_DEVICE_INFO_SET_DPI_SCALE = -4

_SEE_MASK_NOCLOSEPROCESS = 0x40
_SEE_MASK_NOASYNC = 0x100
_SEE_MASK_FLAG_NO_UI = 0x400
_THREAD_PRIORITY_HIGHEST = 2


class DisplayError(RuntimeError):
    """A display/driver operation failed; the message is user-presentable."""


class InstallDeclined(DisplayError):
    """The user answered "No" to the Windows (UAC) prompt on the PC."""


# ---------------------------------------------------------------- structs

class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text):
        import uuid
        u = uuid.UUID(text)
        g = cls(u.time_low, u.time_mid, u.time_hi_version)
        g.Data4[:] = list(u.bytes[8:])
        return g


VDD_ADAPTER_GUID = "{00b41627-04c4-429e-a26e-0265cf50c8fa}"   # interface
VDD_CLASS_GUID = "{4d36e968-e325-11ce-bfc1-08002be10318}"     # Display class


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", wintypes.DWORD), ("Reserved", ctypes.c_size_t)]


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", GUID),
                ("DevInst", wintypes.DWORD), ("Reserved", ctypes.c_size_t)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE)]


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                ("DeviceString", wintypes.WCHAR * 128),
                ("StateFlags", wintypes.DWORD),
                ("DeviceID", wintypes.WCHAR * 128),
                ("DeviceKey", wintypes.WCHAR * 128)]


class DEVMODEW(ctypes.Structure):
    # The display flavour of DEVMODEW: the printer/display union is laid
    # out flat as dmPosition + dmDisplayOrientation + dmDisplayFixedOutput
    # (16 bytes either way), so field offsets match the C struct exactly.
    _fields_ = [
        ("dmDeviceName", wintypes.WCHAR * 32),
        ("dmSpecVersion", wintypes.WORD), ("dmDriverVersion", wintypes.WORD),
        ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD),
        ("dmPositionX", wintypes.LONG), ("dmPositionY", wintypes.LONG),
        ("dmDisplayOrientation", wintypes.DWORD),
        ("dmDisplayFixedOutput", wintypes.DWORD),
        ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", wintypes.WCHAR * 32),
        ("dmLogPixels", wintypes.WORD),
        ("dmBitsPerPel", wintypes.DWORD),
        ("dmPelsWidth", wintypes.DWORD), ("dmPelsHeight", wintypes.DWORD),
        ("dmDisplayFlags", wintypes.DWORD),
        ("dmDisplayFrequency", wintypes.DWORD),
        ("dmICMMethod", wintypes.DWORD), ("dmICMIntent", wintypes.DWORD),
        ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
        ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
        ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD),
    ]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32)]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int32), ("size", ctypes.c_uint32),
                ("adapterId", LUID), ("id", ctypes.c_uint32)]


class DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32),
                ("modeInfoIdx", ctypes.c_uint32),
                ("statusFlags", ctypes.c_uint32)]


class DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32),
                ("modeInfoIdx", ctypes.c_uint32),
                ("outputTechnology", ctypes.c_uint32),
                ("rotation", ctypes.c_uint32), ("scaling", ctypes.c_uint32),
                ("refreshNumerator", ctypes.c_uint32),
                ("refreshDenominator", ctypes.c_uint32),
                ("scanLineOrdering", ctypes.c_uint32),
                ("targetAvailable", wintypes.BOOL),
                ("statusFlags", ctypes.c_uint32)]


class DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
    _fields_ = [("sourceInfo", DISPLAYCONFIG_PATH_SOURCE_INFO),
                ("targetInfo", DISPLAYCONFIG_PATH_TARGET_INFO),
                ("flags", ctypes.c_uint32)]


class DISPLAYCONFIG_MODE_INFO(ctypes.Structure):
    # Only the header is read; the 48-byte union (target/source/desktop
    # mode, 8-byte aligned by its UINT64 pixel rate) stays opaque.
    _fields_ = [("infoType", ctypes.c_uint32), ("id", ctypes.c_uint32),
                ("adapterId", LUID), ("modeData", ctypes.c_uint64 * 6)]


class DISPLAYCONFIG_SOURCE_DEVICE_NAME(ctypes.Structure):
    _fields_ = [("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
                ("viewGdiDeviceName", wintypes.WCHAR * 32)]


class DISPLAYCONFIG_SOURCE_DPI_SCALE_GET(ctypes.Structure):
    _fields_ = [("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
                ("minScaleRel", ctypes.c_int32),
                ("curScaleRel", ctypes.c_int32),
                ("maxScaleRel", ctypes.c_int32)]


class DISPLAYCONFIG_SOURCE_DPI_SCALE_SET(ctypes.Structure):
    _fields_ = [("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
                ("scaleRel", ctypes.c_int32)]


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
                ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                ("lpFile", wintypes.LPCWSTR),
                ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE),
                ("hProcess", wintypes.HANDLE)]


# Expected 64-bit sizes; scripts/verify_phone_monitor.py asserts them so a
# layout slip shows up as a failed self-test, not as a corrupted IOCTL.
STRUCT_SIZES_64 = {
    SP_DEVICE_INTERFACE_DATA: 32, SP_DEVINFO_DATA: 32, OVERLAPPED: 32,
    DISPLAY_DEVICEW: 840, DEVMODEW: 220, MONITORINFOEXW: 104,
    DISPLAYCONFIG_DEVICE_INFO_HEADER: 20, DISPLAYCONFIG_PATH_INFO: 72,
    DISPLAYCONFIG_MODE_INFO: 64, DISPLAYCONFIG_SOURCE_DEVICE_NAME: 84,
    DISPLAYCONFIG_SOURCE_DPI_SCALE_GET: 32,
    DISPLAYCONFIG_SOURCE_DPI_SCALE_SET: 24, SHELLEXECUTEINFOW: 112,
}

# ---------------------------------------------------------------- dlls

_api = None


def api():
    """Lazily bound Win32 functions (private WinDLL instances, see the
    module docstring). Raises DisplayError off Windows."""
    global _api
    if _api is not None:
        return _api
    if not IS_WINDOWS:
        raise DisplayError("virtual displays are only supported on Windows")
    W = wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    sapi = ctypes.WinDLL("setupapi", use_last_error=True)
    cfg = ctypes.WinDLL("cfgmgr32", use_last_error=True)
    sh32 = ctypes.WinDLL("shell32", use_last_error=True)

    def bind(dll, name, restype, *argtypes):
        fn = getattr(dll, name)
        fn.restype = restype
        fn.argtypes = list(argtypes)
        return fn

    class _Api:
        pass

    a = _Api()
    a.SetupDiGetClassDevsW = bind(
        sapi, "SetupDiGetClassDevsW", ctypes.c_void_p,
        ctypes.POINTER(GUID), W.LPCWSTR, W.HWND, W.DWORD)
    a.SetupDiEnumDeviceInterfaces = bind(
        sapi, "SetupDiEnumDeviceInterfaces", W.BOOL, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.POINTER(GUID), W.DWORD,
        ctypes.POINTER(SP_DEVICE_INTERFACE_DATA))
    a.SetupDiGetDeviceInterfaceDetailW = bind(
        sapi, "SetupDiGetDeviceInterfaceDetailW", W.BOOL, ctypes.c_void_p,
        ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p, W.DWORD,
        ctypes.POINTER(W.DWORD), ctypes.c_void_p)
    a.SetupDiEnumDeviceInfo = bind(
        sapi, "SetupDiEnumDeviceInfo", W.BOOL, ctypes.c_void_p, W.DWORD,
        ctypes.POINTER(SP_DEVINFO_DATA))
    a.SetupDiGetDeviceRegistryPropertyW = bind(
        sapi, "SetupDiGetDeviceRegistryPropertyW", W.BOOL, ctypes.c_void_p,
        ctypes.POINTER(SP_DEVINFO_DATA), W.DWORD, ctypes.POINTER(W.DWORD),
        ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD))
    a.SetupDiDestroyDeviceInfoList = bind(
        sapi, "SetupDiDestroyDeviceInfoList", W.BOOL, ctypes.c_void_p)
    a.CM_Get_DevNode_Status = bind(
        cfg, "CM_Get_DevNode_Status", W.DWORD, ctypes.POINTER(W.ULONG),
        ctypes.POINTER(W.ULONG), W.DWORD, W.ULONG)
    a.CreateFileW = bind(
        k32, "CreateFileW", W.HANDLE, W.LPCWSTR, W.DWORD, W.DWORD,
        ctypes.c_void_p, W.DWORD, W.DWORD, W.HANDLE)
    a.DeviceIoControl = bind(
        k32, "DeviceIoControl", W.BOOL, W.HANDLE, W.DWORD, ctypes.c_void_p,
        W.DWORD, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD),
        ctypes.POINTER(OVERLAPPED))
    a.GetOverlappedResultEx = bind(
        k32, "GetOverlappedResultEx", W.BOOL, W.HANDLE,
        ctypes.POINTER(OVERLAPPED), ctypes.POINTER(W.DWORD), W.DWORD, W.BOOL)
    a.CancelIoEx = bind(k32, "CancelIoEx", W.BOOL, W.HANDLE,
                        ctypes.POINTER(OVERLAPPED))
    a.CreateEventW = bind(k32, "CreateEventW", W.HANDLE, ctypes.c_void_p,
                          W.BOOL, W.BOOL, W.LPCWSTR)
    a.CloseHandle = bind(k32, "CloseHandle", W.BOOL, W.HANDLE)
    a.WaitForSingleObject = bind(k32, "WaitForSingleObject", W.DWORD,
                                 W.HANDLE, W.DWORD)
    a.GetExitCodeProcess = bind(k32, "GetExitCodeProcess", W.BOOL, W.HANDLE,
                                ctypes.POINTER(W.DWORD))
    a.GetCurrentThread = bind(k32, "GetCurrentThread", W.HANDLE)
    a.SetThreadPriority = bind(k32, "SetThreadPriority", W.BOOL, W.HANDLE,
                               ctypes.c_int)
    a.EnumDisplayDevicesW = bind(
        u32, "EnumDisplayDevicesW", W.BOOL, W.LPCWSTR, W.DWORD,
        ctypes.POINTER(DISPLAY_DEVICEW), W.DWORD)
    a.EnumDisplaySettingsW = bind(
        u32, "EnumDisplaySettingsW", W.BOOL, W.LPCWSTR, W.DWORD,
        ctypes.POINTER(DEVMODEW))
    a.ChangeDisplaySettingsExW = bind(
        u32, "ChangeDisplaySettingsExW", W.LONG, W.LPCWSTR,
        ctypes.POINTER(DEVMODEW), W.HWND, W.DWORD, ctypes.c_void_p)
    a.MONITORENUMPROC = ctypes.WINFUNCTYPE(
        W.BOOL, W.HMONITOR, W.HDC, ctypes.POINTER(W.RECT), W.LPARAM)
    a.EnumDisplayMonitors = bind(
        u32, "EnumDisplayMonitors", W.BOOL, W.HDC, ctypes.c_void_p,
        a.MONITORENUMPROC, W.LPARAM)
    a.GetMonitorInfoW = bind(u32, "GetMonitorInfoW", W.BOOL, W.HMONITOR,
                             ctypes.POINTER(MONITORINFOEXW))
    a.GetDisplayConfigBufferSizes = bind(
        u32, "GetDisplayConfigBufferSizes", W.LONG, ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32))
    a.QueryDisplayConfig = bind(
        u32, "QueryDisplayConfig", W.LONG, ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(DISPLAYCONFIG_PATH_INFO),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(DISPLAYCONFIG_MODE_INFO), ctypes.c_void_p)
    a.DisplayConfigGetDeviceInfo = bind(
        u32, "DisplayConfigGetDeviceInfo", W.LONG, ctypes.c_void_p)
    a.DisplayConfigSetDeviceInfo = bind(
        u32, "DisplayConfigSetDeviceInfo", W.LONG, ctypes.c_void_p)
    a.ShellExecuteExW = bind(sh32, "ShellExecuteExW", W.BOOL,
                             ctypes.POINTER(SHELLEXECUTEINFOW))
    _api = a
    return a


def ensure_dpi_aware():
    """Make this process per-monitor DPI aware so every coordinate (monitor
    rects, window rects, cursor, injected touch) is in physical pixels.
    Uses the exact call mss already makes on its first capture, so the
    agent's behaviour doesn't change — whichever runs first wins and the
    other's call is a harmless no-op."""
    if not IS_WINDOWS:
        return
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except (OSError, AttributeError):
        try:
            ctypes.WinDLL("user32").SetProcessDPIAware()
        except (OSError, AttributeError):
            pass


# ---------------------------------------------------------------- driver

def _device_path():
    """Device path of the VDD adapter interface, or None when the driver
    isn't installed/present."""
    a = api()
    guid = GUID.parse(VDD_ADAPTER_GUID)
    info = a.SetupDiGetClassDevsW(ctypes.byref(guid), None, None,
                                  _DIGCF_PRESENT | _DIGCF_DEVICEINTERFACE)
    if not info or info == _INVALID_HANDLE:
        return None
    try:
        iface = SP_DEVICE_INTERFACE_DATA()
        iface.cbSize = ctypes.sizeof(iface)
        i = 0
        while a.SetupDiEnumDeviceInterfaces(info, None, ctypes.byref(guid), i,
                                            ctypes.byref(iface)):
            i += 1
            need = wintypes.DWORD(0)
            a.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(iface), None,
                                               0, ctypes.byref(need), None)
            if need.value < 8:
                continue
            buf = ctypes.create_string_buffer(need.value)
            # SP_DEVICE_INTERFACE_DETAIL_DATA_W.cbSize is the size of the
            # fixed part: 8 on 64-bit (DWORD + one WCHAR, padded), 6 on x86.
            ctypes.c_uint32.from_buffer(buf).value = \
                8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if a.SetupDiGetDeviceInterfaceDetailW(
                    info, ctypes.byref(iface), buf, need.value, None, None):
                return ctypes.wstring_at(ctypes.addressof(buf) + 4)
        return None
    finally:
        a.SetupDiDestroyDeviceInfoList(info)


def is_installed():
    """True when the VDD adapter interface is present (driver installed and
    running). Cheap enough to call on every status request."""
    if not IS_WINDOWS:
        return False
    try:
        return _device_path() is not None
    except OSError:
        return False


def driver_status():
    """Device-manager view of the driver: {state, problem?}. state is "ok",
    "missing", "disabled", "restart_required" or "error". Distinguishes
    "never installed" from "installed but broken" so the phone can tell the
    user to reboot instead of offering a pointless reinstall."""
    if not IS_WINDOWS:
        return {"state": "missing"}
    a = api()
    guid = GUID.parse(VDD_CLASS_GUID)
    info = a.SetupDiGetClassDevsW(ctypes.byref(guid), None, None,
                                  _DIGCF_PRESENT)
    if not info or info == _INVALID_HANDLE:
        return {"state": "missing"}
    try:
        dev = SP_DEVINFO_DATA()
        dev.cbSize = ctypes.sizeof(dev)
        i = 0
        while a.SetupDiEnumDeviceInfo(info, i, ctypes.byref(dev)):
            i += 1
            need = wintypes.DWORD(0)
            a.SetupDiGetDeviceRegistryPropertyW(
                info, ctypes.byref(dev), _SPDRP_HARDWAREID, None, None, 0,
                ctypes.byref(need))
            if not need.value:
                continue
            buf = ctypes.create_string_buffer(need.value + 4)
            if not a.SetupDiGetDeviceRegistryPropertyW(
                    info, ctypes.byref(dev), _SPDRP_HARDWAREID, None, buf,
                    need.value, None):
                continue
            ids = ctypes.wstring_at(buf, need.value // 2).split("\0")
            if not any(h.lower() == VDD_HARDWARE_ID.lower() for h in ids):
                continue
            status, problem = wintypes.ULONG(0), wintypes.ULONG(0)
            if a.CM_Get_DevNode_Status(ctypes.byref(status),
                                       ctypes.byref(problem),
                                       dev.DevInst, 0) != 0:
                return {"state": "error"}
            if status.value & (_DN_DRIVER_LOADED | _DN_STARTED):
                return {"state": "ok"}
            if status.value & _DN_HAS_PROBLEM:
                p = problem.value
                if p == _CM_PROB_NEED_RESTART:
                    return {"state": "restart_required", "problem": p}
                if p in (_CM_PROB_DISABLED, _CM_PROB_HARDWARE_DISABLED,
                         _CM_PROB_DISABLED_SERVICE):
                    return {"state": "disabled", "problem": p}
                return {"state": "error", "problem": p}
            return {"state": "error"}
        return {"state": "missing"}
    finally:
        a.SetupDiDestroyDeviceInfoList(info)


class ParsecVdd:
    """One open handle to the driver plus its keepalive thread.

    Every IOCTL gets its own OVERLAPPED + event, so add/remove may run on
    any thread concurrently with the keepalive: serializing them behind a
    lock would let a slow ADD starve the pings and unplug every display."""

    def __init__(self):
        self.handle = None
        self.indexes = set()       # displays this handle added
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()  # open/close lifecycle only
        self._leaked = []          # buffers of IOCTLs that never completed
        self.keepalive_errors = 0

    @property
    def is_open(self):
        return self.handle is not None

    def open(self):
        with self._lock:
            if self.handle is not None:
                return self
            a = api()
            path = _device_path()
            if path is None:
                raise DisplayError("the Parsec display driver is not installed")
            h = a.CreateFileW(
                path, _GENERIC_READ | _GENERIC_WRITE,
                _FILE_SHARE_READ | _FILE_SHARE_WRITE, None, _OPEN_EXISTING,
                _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_NO_BUFFERING
                | _FILE_FLAG_OVERLAPPED | _FILE_FLAG_WRITE_THROUGH, None)
            if not h or h == _INVALID_HANDLE:
                raise DisplayError(
                    f"could not open the display driver "
                    f"(error {ctypes.get_last_error()})")
            self.handle = h
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._keepalive, name="vdd-keepalive", daemon=True)
            self._thread.start()
            return self

    def close(self):
        """Removes every display this handle added, stops the keepalive and
        closes the handle. Safe to call twice."""
        for idx in list(self.indexes):
            try:
                self.remove(idx)
            except (OSError, DisplayError):
                pass  # handle already gone; stopping the keepalive unplugs it
        with self._lock:
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=2)
                self._thread = None
            if self.handle is not None:
                api().CloseHandle(self.handle)
                self.handle = None

    def _ioctl(self, code, data=b""):
        h = self.handle
        if h is None:
            raise DisplayError("display driver handle is closed")
        a = api()
        inbuf = (ctypes.c_ubyte * 32)()
        ctypes.memmove(inbuf, data, min(len(data), 32))
        out = wintypes.DWORD(0)
        ov = OVERLAPPED()
        ev = a.CreateEventW(None, True, False, None)
        if not ev:
            raise OSError(ctypes.get_last_error(), "CreateEvent failed")
        ov.hEvent = ev
        if not a.DeviceIoControl(h, code, inbuf, 32, ctypes.byref(out), 4,
                                 None, ctypes.byref(ov)):
            err = ctypes.get_last_error()
            if err != _ERROR_IO_PENDING:
                a.CloseHandle(ev)
                raise OSError(err, f"VDD ioctl {code:#x} failed")
        done = wintypes.DWORD(0)
        if not a.GetOverlappedResultEx(h, ctypes.byref(ov), ctypes.byref(done),
                                       IOCTL_TIMEOUT_MS, False):
            err = ctypes.get_last_error()
            if err == _WAIT_TIMEOUT:
                # The driver still owns our buffers: cancel, and if even the
                # cancellation doesn't land, keep them alive forever rather
                # than let the driver write into freed memory later.
                a.CancelIoEx(h, ctypes.byref(ov))
                if not a.GetOverlappedResultEx(h, ctypes.byref(ov),
                                               ctypes.byref(done), 1000, False) \
                        and ctypes.get_last_error() == _WAIT_TIMEOUT:
                    self._leaked.append((inbuf, out, ov, ev))
                    raise OSError(err, f"VDD ioctl {code:#x} timed out")
            a.CloseHandle(ev)
            raise OSError(err, f"VDD ioctl {code:#x} failed")
        a.CloseHandle(ev)
        return out.value

    def _keepalive(self):
        try:
            a = api()
            # A late ping unplugs every display; ask the scheduler to keep
            # this thread prompt even while the agent is busy encoding.
            a.SetThreadPriority(a.GetCurrentThread(), _THREAD_PRIORITY_HIGHEST)
        except OSError:
            pass
        while not self._stop.wait(KEEPALIVE_S):
            try:
                self._ioctl(VDD_IOCTL_UPDATE)
            except (OSError, DisplayError) as e:
                self.keepalive_errors += 1
                if self.keepalive_errors in (1, 100):
                    print(f"[vdisplay] keepalive failed: {e}")

    def version(self):
        """Driver minor version (e.g. 45 for 0.45)."""
        return self._ioctl(VDD_IOCTL_VERSION)

    def add(self):
        """Plugs in a new virtual display; returns its driver index."""
        idx = self._ioctl(VDD_IOCTL_ADD)
        if idx == 0xFFFFFFFF:
            raise DisplayError("the display driver refused to add a display")
        # Track it before anything else can fail, so close() removes it.
        self.indexes.add(idx)
        try:
            self._ioctl(VDD_IOCTL_UPDATE)  # as the reference client does
        except OSError:
            pass  # the keepalive thread pings again within 50ms
        return idx

    def remove(self, index):
        # The driver wants the 16-bit index byte-swapped (big-endian).
        self._ioctl(VDD_IOCTL_REMOVE, int(index & 0xFFFF).to_bytes(2, "big"))
        self.indexes.discard(index)
        try:
            self._ioctl(VDD_IOCTL_UPDATE)
        except OSError:
            pass


# ---------------------------------------------------------------- displays

def display_adapters():
    """Every GDI display adapter output (\\\\.\\DISPLAYn) with its monitor:
    [{device, adapter, attached, primary, monitor_id, parsec}]."""
    a = api()
    out = []
    i = 0
    while True:
        dd = DISPLAY_DEVICEW()
        dd.cb = ctypes.sizeof(dd)
        if not a.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
            break
        i += 1
        mon = DISPLAY_DEVICEW()
        mon.cb = ctypes.sizeof(mon)
        monitor_id = ""
        if a.EnumDisplayDevicesW(dd.DeviceName, 0, ctypes.byref(mon), 0):
            monitor_id = mon.DeviceID
        out.append({
            "device": dd.DeviceName,
            "adapter": dd.DeviceString,
            "attached": bool(dd.StateFlags & _DISPLAY_DEVICE_ATTACHED_TO_DESKTOP),
            "primary": bool(dd.StateFlags & _DISPLAY_DEVICE_PRIMARY_DEVICE),
            "monitor_id": monitor_id,
            "parsec": VDD_ADAPTER_NAME.lower() in dd.DeviceString.lower()
            or VDD_DISPLAY_ID in monitor_id.upper(),
        })
    return out


def monitor_children(device):
    """Hardware ids (e.g. "PSCCDD0", "CMN153B") of the monitors a GDI source
    currently drives. More than one = Windows is duplicating that source
    onto several screens."""
    a = api()
    out = []
    i = 0
    while True:
        dd = DISPLAY_DEVICEW()
        dd.cb = ctypes.sizeof(dd)
        if not a.EnumDisplayDevicesW(device, i, ctypes.byref(dd),
                                     _EDD_GET_DEVICE_INTERFACE_NAME):
            break
        i += 1
        if not dd.StateFlags & _DISPLAY_DEVICE_ACTIVE:
            continue
        # \\?\DISPLAY#PSCCDD0#1&28a6823a&2&UID256#{...}
        parts = dd.DeviceID.split("#")
        out.append((parts[1] if len(parts) > 2 else dd.DeviceID).upper())
    return out


def is_parsec_monitor(device):
    """True when `device` is a source showing ONLY a Parsec virtual monitor,
    i.e. a real extra screen and not the PC's own screen mirrored onto it."""
    kids = monitor_children(device)
    return bool(kids) and all(VDD_DISPLAY_ID in k for k in kids)


def force_extend():
    """Win+P -> Extend. Windows remembers a topology per set of connected
    screens; if it last saw this combination in Duplicate mode it mirrors
    the PC's screen onto the virtual one instead of adding a second screen,
    and the phone would just see the PC's own screen."""
    u32 = ctypes.WinDLL("user32")
    u32.SetDisplayConfig.restype = ctypes.c_long
    u32.SetDisplayConfig.argtypes = [ctypes.c_uint32, ctypes.c_void_p,
                                     ctypes.c_uint32, ctypes.c_void_p,
                                     ctypes.c_uint32]
    r = u32.SetDisplayConfig(0, None, 0, None,
                             _SDC_APPLY | _SDC_TOPOLOGY_EXTEND)
    if r != 0:
        raise DisplayError(f"could not switch to Extend mode ({r})")


def parsec_displays():
    """GDI outputs that belong to the Parsec adapter (attached or not)."""
    return [d for d in display_adapters() if d["parsec"]]


def monitors():
    """Active monitors from EnumDisplayMonitors (the same order mss uses):
    [{handle, device, left, top, right, bottom, work: (l, t, r, b), primary}]."""
    a = api()
    found = []

    def cb(hmon, _hdc, _rect, _data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if a.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r, w = info.rcMonitor, info.rcWork
            found.append({
                "handle": hmon, "device": info.szDevice,
                "left": r.left, "top": r.top, "right": r.right,
                "bottom": r.bottom,
                "work": (w.left, w.top, w.right, w.bottom),
                "primary": bool(info.dwFlags & _MONITORINFOF_PRIMARY),
            })
        return True

    proc = a.MONITORENUMPROC(cb)  # referenced until the call returns
    a.EnumDisplayMonitors(None, None, proc, 0)
    return found


def monitor_for_device(device):
    for m in monitors():
        if m["device"].lower() == device.lower():
            return m
    return None


def primary_monitor():
    mons = monitors()
    for m in mons:
        if m["primary"]:
            return m
    return mons[0] if mons else None


def current_mode(device):
    """{width, height, orientation, x, y, frequency} or None when the output
    has no active mode (not attached to the desktop)."""
    a = api()
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(dm)
    if not a.EnumDisplaySettingsW(device, _ENUM_CURRENT_SETTINGS,
                                  ctypes.byref(dm)):
        return None
    return {"width": dm.dmPelsWidth, "height": dm.dmPelsHeight,
            "orientation": dm.dmDisplayOrientation,
            "x": dm.dmPositionX, "y": dm.dmPositionY,
            "frequency": dm.dmDisplayFrequency, "bpp": dm.dmBitsPerPel}


def stage_mode(device, width=None, height=None, orientation=None,
               position=None):
    """Stages a mode change for one output (CDS_UPDATEREGISTRY|CDS_NORESET);
    nothing changes on screen until apply_staged(). width/height are the
    LANDSCAPE mode (e.g. 1920x1080): for DMDO_90/DMDO_270 they are swapped
    here, because Windows wants the rotated size in a rotated DEVMODE."""
    a = api()
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(dm)
    # Start from the current (or, for a not-yet-attached output, the last
    # saved) settings so unflagged fields stay sane.
    if not a.EnumDisplaySettingsW(device, _ENUM_CURRENT_SETTINGS,
                                  ctypes.byref(dm)):
        a.EnumDisplaySettingsW(device, _ENUM_REGISTRY_SETTINGS,
                               ctypes.byref(dm))
        dm.dmSize = ctypes.sizeof(dm)
    dm.dmDriverExtra = 0
    dm.dmFields = 0
    if width and height:
        if orientation in (DMDO_90, DMDO_270):
            width, height = height, width
        dm.dmPelsWidth, dm.dmPelsHeight = int(width), int(height)
        dm.dmFields |= _DM_PELSWIDTH | _DM_PELSHEIGHT
    if orientation is not None:
        dm.dmDisplayOrientation = int(orientation)
        dm.dmFields |= _DM_DISPLAYORIENTATION
    if position is not None:
        dm.dmPositionX, dm.dmPositionY = int(position[0]), int(position[1])
        dm.dmFields |= _DM_POSITION
    r = a.ChangeDisplaySettingsExW(device, ctypes.byref(dm), None,
                                   _CDS_UPDATEREGISTRY | _CDS_NORESET, None)
    if r != 0:
        raise DisplayError(
            f"display mode rejected ({DISP_CHANGE_NAMES.get(r, r)})")
    return r


def apply_staged():
    """Applies every staged change in ONE mode set (one flicker, not one
    per setting)."""
    r = api().ChangeDisplaySettingsExW(None, None, None, 0, None)
    if r not in (0, 1):
        raise DisplayError(
            f"display change failed ({DISP_CHANGE_NAMES.get(r, r)})")
    return r


def _source_for_device(device):
    """(adapterId LUID, source id) of the active CCD path showing `device`,
    or None. The DPI packets address sources, not GDI names."""
    a = api()
    for _ in range(3):  # the topology can change between size + query
        n_paths, n_modes = ctypes.c_uint32(0), ctypes.c_uint32(0)
        if a.GetDisplayConfigBufferSizes(_QDC_ONLY_ACTIVE_PATHS,
                                         ctypes.byref(n_paths),
                                         ctypes.byref(n_modes)) != 0:
            return None
        paths = (DISPLAYCONFIG_PATH_INFO * max(1, n_paths.value))()
        modes = (DISPLAYCONFIG_MODE_INFO * max(1, n_modes.value))()
        r = a.QueryDisplayConfig(_QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths),
                                 paths, ctypes.byref(n_modes), modes, None)
        if r == _ERROR_INSUFFICIENT_BUFFER:
            continue
        if r != 0:
            return None
        for p in paths[:n_paths.value]:
            req = DISPLAYCONFIG_SOURCE_DEVICE_NAME()
            req.header.type = _DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME
            req.header.size = ctypes.sizeof(req)
            req.header.adapterId = p.sourceInfo.adapterId
            req.header.id = p.sourceInfo.id
            if a.DisplayConfigGetDeviceInfo(ctypes.byref(req)) == 0 and \
                    req.viewGdiDeviceName.lower() == device.lower():
                return p.sourceInfo.adapterId, p.sourceInfo.id
        return None
    return None


def get_dpi_scale(device):
    """Per-monitor scale of `device` in percent: {current, recommended,
    maximum}, or None if Windows won't say. Uses the undocumented
    DisplayConfigGetDeviceInfo packet (-3) that the Settings app uses; the
    driver reports min/current/max as steps relative to the recommended
    scale along DPI_VALUES (approach from github.com/imniko/SetDPI)."""
    src = _source_for_device(device)
    if src is None:
        return None
    pkt = DISPLAYCONFIG_SOURCE_DPI_SCALE_GET()
    pkt.header.type = _DISPLAYCONFIG_DEVICE_INFO_GET_DPI_SCALE
    pkt.header.size = ctypes.sizeof(pkt)
    pkt.header.adapterId, pkt.header.id = src
    if api().DisplayConfigGetDeviceInfo(ctypes.byref(pkt)) != 0:
        return None
    cur = max(pkt.minScaleRel, min(pkt.curScaleRel, pkt.maxScaleRel))
    base = abs(pkt.minScaleRel)  # index of "recommended" in DPI_VALUES
    if base + pkt.maxScaleRel + 1 > len(DPI_VALUES) or base + cur < 0:
        return None
    return {"current": DPI_VALUES[base + cur],
            "recommended": DPI_VALUES[base],
            "maximum": DPI_VALUES[base + pkt.maxScaleRel]}


def set_dpi_scale(device, percent):
    """Sets the per-monitor scale (snapped to the nearest allowed step);
    returns the percent applied, or None when Windows refuses — callers
    treat that as cosmetic and carry on."""
    info = get_dpi_scale(device)
    if info is None:
        return None
    allowed = [v for v in DPI_VALUES if v <= info["maximum"]]
    target = min(allowed, key=lambda v: abs(v - int(percent)))
    if target == info["current"]:
        return target
    src = _source_for_device(device)
    if src is None:
        return None
    pkt = DISPLAYCONFIG_SOURCE_DPI_SCALE_SET()
    pkt.header.type = _DISPLAYCONFIG_DEVICE_INFO_SET_DPI_SCALE
    pkt.header.size = ctypes.sizeof(pkt)
    pkt.header.adapterId, pkt.header.id = src
    pkt.scaleRel = DPI_VALUES.index(target) - DPI_VALUES.index(
        info["recommended"])
    if api().DisplayConfigSetDeviceInfo(ctypes.byref(pkt)) != 0:
        return None
    return target


# ---------------------------------------------------------------- install

def drivers_dir():
    base = Path(os.environ.get("APPDATA", str(Path.home())))
    return base / "Sudo" / "drivers"


def verify_installer(path):
    """Authenticode check through PowerShell: the file must carry a VALID
    signature whose signer subject names Parsec. Returns {ok, status,
    subject}. The path goes in through an environment variable, never
    spliced into the script text."""
    script = (
        "$s = Get-AuthenticodeSignature -LiteralPath $env:SUDO_VERIFY_PATH; "
        "[pscustomobject]@{status=[string]$s.Status; "
        "subject=[string]$s.SignerCertificate.Subject} "
        "| ConvertTo-Json -Compress")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=60, errors="replace",
            env={**os.environ, "SUDO_VERIFY_PATH": str(path)},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        info = json.loads(proc.stdout.strip() or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "status": f"check failed: {e}", "subject": ""}
    status = str(info.get("status") or "")
    subject = str(info.get("subject") or "")
    return {"ok": status == "Valid" and "parsec" in subject.lower(),
            "status": status, "subject": subject}


def _download(url, dest, report):
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "Sudo-agent"})
    with urllib.request.urlopen(req, timeout=30) as resp, open(tmp, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        got = last = 0
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if got - last >= 256 * 1024:
                last = got
                report("downloading", received=got, total=total)
    if total and got != total:
        tmp.unlink(missing_ok=True)
        raise DisplayError("the driver download was cut short")
    os.replace(tmp, dest)


def _run_elevated(path, params, timeout_s=300, on_started=None):
    """Starts `path` through the UAC prompt ("runas"). Blocks until the
    user answers the prompt and the process exits; returns its exit code.
    on_started() runs once the prompt was approved and the process is up.
    Raises InstallDeclined if the user says No."""
    a = api()
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = _SEE_MASK_NOCLOSEPROCESS | _SEE_MASK_NOASYNC | _SEE_MASK_FLAG_NO_UI
    sei.lpVerb = "runas"
    sei.lpFile = str(path)
    sei.lpParameters = params
    sei.lpDirectory = str(Path(path).parent)
    sei.nShow = 1  # SW_SHOWNORMAL (the NSIS /S installer shows no UI anyway)
    if not a.ShellExecuteExW(ctypes.byref(sei)):
        err = ctypes.get_last_error()
        if err == _ERROR_CANCELLED:
            raise InstallDeclined("the Windows prompt on the PC was declined")
        raise DisplayError(f"could not start the installer (error {err})")
    if on_started:
        on_started()
    if not sei.hProcess:
        return 0
    try:
        if a.WaitForSingleObject(sei.hProcess, int(timeout_s * 1000)) \
                == _WAIT_TIMEOUT:
            raise DisplayError("the driver installer did not finish")
        code = wintypes.DWORD(0)
        a.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
        return code.value
    finally:
        a.CloseHandle(sei.hProcess)


def install(version=DEFAULT_VERSION, progress=None, _fallback=True):
    """One-time driver install, triggered by the user from the phone.

    Downloads the official installer into %APPDATA%/Sudo/drivers, refuses
    anything not validly signed by Parsec, runs it elevated with /S (the
    user approves the UAC prompt on the PC) and waits for the adapter to
    appear; if 0.45 doesn't come up, 0.41 is tried once. progress(dict)
    receives {stage, ...} with stage downloading / verifying /
    waiting_for_approval / installing / done / error. Returns the final
    progress dict; never raises."""

    def report(stage, **info):
        state = {"stage": stage, "version": version, **info}
        if progress:
            try:
                progress(state)
            except Exception:
                pass
        return state

    if is_installed():
        return report("done", already=True)
    try:
        if not IS_WINDOWS:
            raise DisplayError("virtual displays are only supported on Windows")
        folder = drivers_dir()
        folder.mkdir(parents=True, exist_ok=True)
        exe = folder / f"parsec-vdd-{version}.exe"
        report("downloading", received=0, total=0)
        _download(INSTALLER_URL.format(version=version), exe, report)
        report("verifying")
        sig = verify_installer(exe)
        if not sig["ok"]:
            exe.unlink(missing_ok=True)
            raise DisplayError(
                f"the downloaded driver is not signed by Parsec "
                f"({sig['status'] or 'no signature'})")
        report("waiting_for_approval")
        # "installing" as soon as the prompt is approved, not after the
        # installer already finished: that is what the phone is waiting on.
        code = _run_elevated(exe, "/S",
                             on_started=lambda: report("installing"))
        print(f"[vdisplay] driver installer {version} exited with {code}")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:  # PnP brings the adapter up async
            if is_installed():
                return report("done")
            time.sleep(0.5)
        if driver_status().get("state") == "restart_required":
            raise DisplayError("restart the PC to finish installing the driver")
        if _fallback and version != FALLBACK_VERSION:
            print(f"[vdisplay] driver {version} did not come up "
                  f"(exit {code}); trying {FALLBACK_VERSION}")
            return install(FALLBACK_VERSION, progress, _fallback=False)
        raise DisplayError("the display driver did not start after installing")
    except InstallDeclined as e:
        return report("error", error=str(e), declined=True)
    except (DisplayError, OSError, ValueError,
            http.client.HTTPException) as e:
        # HTTPException (e.g. IncompleteRead on a dropped download) is not
        # an OSError; without it a flaky network would escape "never raises".
        return report("error", error=str(e))
