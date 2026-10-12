"""DXGI Desktop Duplication screen capture in pure ctypes + numpy.

No extra packages: DXGI / D3D11 COM objects are driven through their raw
vtables, the same way agent.py's dxgi_outputs() already does.

    cap = DXGICapture()                 # primary monitor
    img = cap.grab()                    # (h, w, 4) uint8 BGRA, or None
    img = cap.grab(rect, timeout_ms=16) # rect = mss-style dict, desktop px
    cap.close()

grab() returns None when the desktop image has not changed since the last
grab (Desktop Duplication only hands out a frame when something was
presented), so it doubles as the agent's "unchanged frame" check. The mouse
pointer is NOT in the image: its position comes in the frame info
(cap.pointer_pos / cap.pointer_visible, desktop coordinates) and its shape
via cap.pointer_shape when it changes.

The D3D11 device is created on the adapter that OWNS the output
(D3D11CreateDevice(adapter, D3D_DRIVER_TYPE_UNKNOWN)). On a hybrid laptop a
device on the other GPU makes DuplicateOutput fail with
DXGI_ERROR_UNSUPPORTED.

DXGI_ERROR_ACCESS_LOST (UAC / secure desktop, resolution or rotation
change, full-screen exclusive app, mode switch) and a few related errors
drop the duplication; grab() returns None and the next grab() re-opens it
(re-enumerating outputs, so a new resolution / arrangement is picked up).
While the secure desktop is up DuplicateOutput itself fails
(E_ACCESSDENIED); grab() then raises DXGIError so the caller backs off,
exactly like a failing mss grab today.

Must be used from ONE thread (D3D11 immediate context is not thread-safe),
e.g. the streamer's capture thread.

Run `python dxgi_capture.py` for a probe + benchmark (see --help).
"""
import ctypes
import time
import uuid
from ctypes import wintypes

import numpy as np

HRESULT = ctypes.c_long
UINT = ctypes.c_uint
PVOID = ctypes.c_void_p
PPVOID = ctypes.POINTER(ctypes.c_void_p)

# ---------------------------------------------------------------- HRESULTs
S_OK = 0
DXGI_ERROR_INVALID_CALL = 0x887A0001
DXGI_ERROR_NOT_FOUND = 0x887A0002
DXGI_ERROR_UNSUPPORTED = 0x887A0004
DXGI_ERROR_DEVICE_REMOVED = 0x887A0005
DXGI_ERROR_DEVICE_RESET = 0x887A0007
DXGI_ERROR_NOT_CURRENTLY_AVAILABLE = 0x887A0022
DXGI_ERROR_ACCESS_LOST = 0x887A0026
DXGI_ERROR_WAIT_TIMEOUT = 0x887A0027
DXGI_ERROR_SESSION_DISCONNECTED = 0x887A0028
E_INVALIDARG = 0x80070057
E_ACCESSDENIED = 0x80070005

HR_NAMES = {
    DXGI_ERROR_INVALID_CALL: "DXGI_ERROR_INVALID_CALL",
    DXGI_ERROR_NOT_FOUND: "DXGI_ERROR_NOT_FOUND",
    DXGI_ERROR_UNSUPPORTED: "DXGI_ERROR_UNSUPPORTED",
    DXGI_ERROR_DEVICE_REMOVED: "DXGI_ERROR_DEVICE_REMOVED",
    DXGI_ERROR_DEVICE_RESET: "DXGI_ERROR_DEVICE_RESET",
    DXGI_ERROR_NOT_CURRENTLY_AVAILABLE: "DXGI_ERROR_NOT_CURRENTLY_AVAILABLE",
    DXGI_ERROR_ACCESS_LOST: "DXGI_ERROR_ACCESS_LOST",
    DXGI_ERROR_WAIT_TIMEOUT: "DXGI_ERROR_WAIT_TIMEOUT",
    DXGI_ERROR_SESSION_DISCONNECTED: "DXGI_ERROR_SESSION_DISCONNECTED",
    E_INVALIDARG: "E_INVALIDARG",
    E_ACCESSDENIED: "E_ACCESSDENIED",
}

# Errors after which the duplication must be thrown away and re-created.
_REOPEN = {DXGI_ERROR_ACCESS_LOST, DXGI_ERROR_DEVICE_REMOVED,
           DXGI_ERROR_DEVICE_RESET, DXGI_ERROR_SESSION_DISCONNECTED,
           DXGI_ERROR_INVALID_CALL}


def hr_str(hr):
    hr &= 0xFFFFFFFF
    return f"0x{hr:08X} {HR_NAMES.get(hr, '')}".rstrip()


class DXGIError(OSError):
    def __init__(self, what, hr):
        self.hr = hr & 0xFFFFFFFF
        super().__init__(f"{what} failed: {hr_str(hr)}")


def _guid(s):
    return (ctypes.c_ubyte * 16)(*uuid.UUID(s).bytes_le)


IID_IDXGIFactory1 = _guid("770aae78-f26f-4dba-a829-253c83d1b387")
IID_IDXGIOutput1 = _guid("00cddea8-939b-4b83-a340-a685226666cc")
IID_IDXGIOutput5 = _guid("80a07424-ab52-42eb-833c-0c42fd282d98")
IID_ID3D11Texture2D = _guid("6f15aaf2-d208-4e89-9ab4-489535d34f9c")


# -------------------------------------------------------------- structures
class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [("Description", ctypes.c_wchar * 128),
                ("VendorId", UINT), ("DeviceId", UINT),
                ("SubSysId", UINT), ("Revision", UINT),
                ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t),
                ("SharedSystemMemory", ctypes.c_size_t),
                ("AdapterLuid", LUID), ("Flags", UINT)]


class DXGI_OUTPUT_DESC(ctypes.Structure):
    _fields_ = [("DeviceName", ctypes.c_wchar * 32),
                ("DesktopCoordinates", wintypes.RECT),
                ("AttachedToDesktop", wintypes.BOOL),
                ("Rotation", UINT),
                ("Monitor", wintypes.HMONITOR)]


class DXGI_RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", UINT), ("Denominator", UINT)]


class DXGI_MODE_DESC(ctypes.Structure):
    _fields_ = [("Width", UINT), ("Height", UINT),
                ("RefreshRate", DXGI_RATIONAL), ("Format", UINT),
                ("ScanlineOrdering", UINT), ("Scaling", UINT)]


class DXGI_OUTDUPL_DESC(ctypes.Structure):
    _fields_ = [("ModeDesc", DXGI_MODE_DESC), ("Rotation", UINT),
                ("DesktopImageInSystemMemory", wintypes.BOOL)]


class DXGI_OUTDUPL_POINTER_POSITION(ctypes.Structure):
    _fields_ = [("Position", wintypes.POINT), ("Visible", wintypes.BOOL)]


class DXGI_OUTDUPL_FRAME_INFO(ctypes.Structure):
    _fields_ = [("LastPresentTime", ctypes.c_longlong),
                ("LastMouseUpdateTime", ctypes.c_longlong),
                ("AccumulatedFrames", UINT),
                ("RectsCoalesced", wintypes.BOOL),
                ("ProtectedContentMaskedOut", wintypes.BOOL),
                ("PointerPosition", DXGI_OUTDUPL_POINTER_POSITION),
                ("TotalMetadataBufferSize", UINT),
                ("PointerShapeBufferSize", UINT)]


class DXGI_OUTDUPL_POINTER_SHAPE_INFO(ctypes.Structure):
    _fields_ = [("Type", UINT), ("Width", UINT), ("Height", UINT),
                ("Pitch", UINT), ("HotSpot", wintypes.POINT)]


class DXGI_MAPPED_RECT(ctypes.Structure):
    _fields_ = [("Pitch", ctypes.c_int), ("pBits", PVOID)]


class D3D11_TEXTURE2D_DESC(ctypes.Structure):
    _fields_ = [("Width", UINT), ("Height", UINT), ("MipLevels", UINT),
                ("ArraySize", UINT), ("Format", UINT),
                ("SampleCount", UINT), ("SampleQuality", UINT),
                ("Usage", UINT), ("BindFlags", UINT),
                ("CPUAccessFlags", UINT), ("MiscFlags", UINT)]


class D3D11_MAPPED_SUBRESOURCE(ctypes.Structure):
    _fields_ = [("pData", PVOID), ("RowPitch", UINT), ("DepthPitch", UINT)]


class D3D11_BOX(ctypes.Structure):
    _fields_ = [("left", UINT), ("top", UINT), ("front", UINT),
                ("right", UINT), ("bottom", UINT), ("back", UINT)]


assert ctypes.sizeof(DXGI_OUTDUPL_FRAME_INFO) == 48
assert ctypes.sizeof(D3D11_TEXTURE2D_DESC) == 44

DXGI_FORMAT_B8G8R8A8_UNORM = 87
D3D11_USAGE_STAGING = 3
D3D11_CPU_ACCESS_READ = 0x20000
D3D11_MAP_READ = 1
D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20
D3D_DRIVER_TYPE_UNKNOWN = 0
D3D11_SDK_VERSION = 7
FEATURE_LEVELS = (ctypes.c_uint * 4)(0xB100, 0xB000, 0xA100, 0xA000)


# ------------------------------------------------------- raw vtable COM
class Com:
    """A COM interface pointer driven through its vtable. Slots used:
    0 QueryInterface, 2 Release, the rest per interface (see callers)."""
    __slots__ = ("p", "_fns")

    def __init__(self, p):
        self.p = p if isinstance(p, ctypes.c_void_p) else ctypes.c_void_p(p)
        self._fns = {}

    def __bool__(self):
        return bool(self.p and self.p.value)

    def fn(self, idx, restype, *argtypes):
        f = self._fns.get(idx)
        if f is None:
            vtbl = ctypes.cast(self.p, ctypes.POINTER(PPVOID))[0]
            f = ctypes.WINFUNCTYPE(restype, PVOID, *argtypes)(vtbl[idx])
            self._fns[idx] = f
        return f

    def call(self, idx, argtypes, *args, restype=HRESULT):
        return self.fn(idx, restype, *argtypes)(self.p, *args)

    def qi(self, iid):
        out = ctypes.c_void_p()
        hr = self.call(0, (PVOID, PPVOID), ctypes.byref(iid),
                       ctypes.byref(out))
        if hr < 0:
            raise DXGIError("QueryInterface", hr)
        return Com(out)

    def release(self):
        if self:
            self.call(2, (), restype=ctypes.c_ulong)
            self.p = ctypes.c_void_p()


_k32 = ctypes.WinDLL("kernel32")
_qpf = ctypes.c_longlong()
_k32.QueryPerformanceFrequency(ctypes.byref(_qpf))
_QPF = _qpf.value
_dxgi = ctypes.WinDLL("dxgi")
_d3d11 = ctypes.WinDLL("d3d11")
_dxgi.CreateDXGIFactory1.argtypes = [PVOID, PPVOID]
_dxgi.CreateDXGIFactory1.restype = HRESULT
_d3d11.D3D11CreateDevice.argtypes = [
    PVOID, UINT, PVOID, UINT, PVOID, UINT, UINT, PPVOID,
    ctypes.POINTER(UINT), PPVOID]
_d3d11.D3D11CreateDevice.restype = HRESULT


def prefer_display_gpu():
    """Hybrid NVIDIA Optimus laptops: make DXGI list the integrated GPU (the
    one wired to the panel) as the owner of the panel's output.

    Without this, a process that Optimus routes to the dGPU (this PC's
    default for python.exe, and every child of a process that inherited
    SHIM_MCCOMPAT=0x810000001) sees the panel output listed under the NVIDIA
    adapter, and DuplicateOutput on it fails with DXGI_ERROR_UNSUPPORTED
    whatever device is used. The value is taken when the process creates
    its FIRST DXGI factory, so call this before anything (dxgi_outputs(),
    ...) creates one; dxgi.dll/d3d11.dll merely being loaded (cv2, mss) is
    fine. Ignored on machines without Optimus. Inherited by child processes
    (ffmpeg's ddagrab then sees the same thing). Returns the previous value.
    """
    import os
    prev = os.environ.get("SHIM_MCCOMPAT")
    os.environ["SHIM_MCCOMPAT"] = "0x800000000"   # 0 = integrated GPU
    return prev


def set_dpi_aware():
    """Per-monitor-v2 DPI awareness (what mss also asks for) so output
    coordinates are physical pixels. Harmless if already set."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass


def enum_outputs():
    """[(adapter Com, adapter desc, output Com, output desc, a, o)] for every
    output of every adapter (attached or not). Caller releases the Coms
    (release_enum)."""
    factory = ctypes.c_void_p()
    hr = _dxgi.CreateDXGIFactory1(ctypes.byref(IID_IDXGIFactory1),
                                  ctypes.byref(factory))
    if hr < 0:
        raise DXGIError("CreateDXGIFactory1", hr)
    factory = Com(factory)
    out = []
    try:
        a = 0
        while True:
            adapter = ctypes.c_void_p()
            # IDXGIFactory1::EnumAdapters1 = slot 12
            if factory.call(12, (UINT, PPVOID), a, ctypes.byref(adapter)) < 0:
                break
            adapter = Com(adapter)
            ad = DXGI_ADAPTER_DESC1()
            adapter.call(10, (ctypes.POINTER(DXGI_ADAPTER_DESC1),),
                         ctypes.byref(ad))          # GetDesc1
            o = 0
            any_out = False
            while True:
                output = ctypes.c_void_p()
                # IDXGIAdapter::EnumOutputs = slot 7
                if adapter.call(7, (UINT, PPVOID), o,
                                ctypes.byref(output)) < 0:
                    break
                output = Com(output)
                od = DXGI_OUTPUT_DESC()
                output.call(7, (ctypes.POINTER(DXGI_OUTPUT_DESC),),
                            ctypes.byref(od))       # IDXGIOutput::GetDesc
                adapter.call(1, (), restype=ctypes.c_ulong)  # AddRef
                out.append((adapter, ad, output, od, a, o))
                any_out = True
                o += 1
            if not any_out:
                out.append((adapter, ad, None, None, a, -1))
            else:
                # each entry holds its own AddRef; drop the enum's ref
                # without zeroing the shared Com
                adapter.call(2, (), restype=ctypes.c_ulong)
            a += 1
    finally:
        factory.release()
    return out


def release_enum(entries):
    for adapter, _, output, _, _, _ in entries:
        if output:
            output.release()
        if adapter:
            # Com.release zeroes p, so release by raw call for shared ptrs
            adapter.call(2, (), restype=ctypes.c_ulong)


def create_device(adapter):
    """D3D11 device + immediate context on `adapter` (Com IDXGIAdapter)."""
    dev, ctx, fl = ctypes.c_void_p(), ctypes.c_void_p(), UINT()
    hr = _d3d11.D3D11CreateDevice(
        adapter.p, D3D_DRIVER_TYPE_UNKNOWN, None,
        D3D11_CREATE_DEVICE_BGRA_SUPPORT, FEATURE_LEVELS, len(FEATURE_LEVELS),
        D3D11_SDK_VERSION, ctypes.byref(dev), ctypes.byref(fl),
        ctypes.byref(ctx))
    if hr < 0:
        raise DXGIError("D3D11CreateDevice", hr)
    return Com(dev), Com(ctx), fl.value


def duplicate(output, device, use_output5=False):
    """IDXGIOutputDuplication for `output` on `device`. Returns (dup, hr)."""
    dup = ctypes.c_void_p()
    if use_output5:
        o5 = output.qi(IID_IDXGIOutput5)
        try:
            fmts = (UINT * 1)(DXGI_FORMAT_B8G8R8A8_UNORM)
            # IDXGIOutput5::DuplicateOutput1 = slot 26
            hr = o5.call(26, (PVOID, UINT, UINT, PVOID, PPVOID),
                         device.p, 0, 1, fmts, ctypes.byref(dup))
        finally:
            o5.release()
    else:
        o1 = output.qi(IID_IDXGIOutput1)
        try:
            # IDXGIOutput1::DuplicateOutput = slot 22
            hr = o1.call(22, (PVOID, PPVOID), device.p, ctypes.byref(dup))
        finally:
            o1.release()
    return (Com(dup) if hr >= 0 else None), hr


# IDXGIOutputDuplication slots
_DUP_GETDESC, _DUP_ACQUIRE, _DUP_POINTERSHAPE = 7, 8, 11
_DUP_MAP, _DUP_UNMAP, _DUP_RELEASE = 12, 13, 14
# ID3D11Device / ID3D11DeviceContext / ID3D11Texture2D slots
_DEV_CREATETEX2D = 5
_CTX_MAP, _CTX_UNMAP, _CTX_COPYREGION, _CTX_COPY = 14, 15, 46, 47
_TEX_GETDESC = 10


def _rect_tuple(rect):
    if rect is None:
        return None
    return (int(rect["left"]), int(rect["top"]),
            int(rect["width"]), int(rect["height"]))


class DXGICapture:
    """Desktop Duplication of the monitor containing `rect` (default: the
    output at desktop (0,0) = primary).

    grab(rect=None, timeout_ms=0) -> np.ndarray (h, w, 4) BGRA uint8 when the
    desktop changed since the last grab, else None.

    hold_frame=True keeps the acquired frame until the next grab (Microsoft's
    recommendation: while the app owns no frame the OS copies every update
    into the duplication surface). The pixels are copied out before grab()
    returns either way.
    """

    def __init__(self, rect=None, hold_frame=True, use_output5=False,
                 ring=0):
        set_dpi_aware()
        self.hold_frame = hold_frame
        # ring > 0: return images from `ring` preallocated arrays, round
        # robin (an image stays valid until `ring` more images were
        # returned). Saves ~3-4 ms/frame of page faults on a fresh 8 MB
        # array at 1080p. 0 = a new array every time (always safe).
        self.ring = ring
        self._bufs = []
        self._buf_i = 0
        self.use_output5 = use_output5
        self._want = _rect_tuple(rect)
        self._dup = self._dev = self._ctx = None
        self._staging = None
        self._staging_size = None
        self._holding = False
        self.origin = (0, 0)          # output's desktop top-left
        self.size = (0, 0)            # output's desktop size
        self.rotation = 1
        self.adapter_name = ""
        self.output_name = ""
        self.in_sysmem = False
        self.pointer_pos = None       # desktop coords, last reported
        self.pointer_visible = False
        self.pointer_shape = None     # (type, w, h, pitch, hotspot, bytes)
        self.last_info = DXGI_OUTDUPL_FRAME_INFO()
        self.last = None              # last image returned (for force=True)
        self._fresh = True
        self.reopens = 0
        self.last_error = None
        # per-grab timings (ms): blocked in AcquireNextFrame, copy-out, and
        # present->acquired latency (QPC now - LastPresentTime)
        self.t_acquire = self.t_copy = self.t_latency = 0.0

    # ------------------------------------------------------------ open
    def _pick(self, entries):
        want = self._want
        cx, cy = (0, 0) if want is None else (want[0] + want[2] // 2,
                                              want[1] + want[3] // 2)
        for e in entries:
            od = e[3]
            if od is None or not od.AttachedToDesktop:
                continue
            r = od.DesktopCoordinates
            if r.left <= cx < r.right and r.top <= cy < r.bottom:
                return e
        raise DXGIError("no DXGI output contains the capture rect",
                        DXGI_ERROR_NOT_FOUND)

    def open(self):
        self.close()
        entries = enum_outputs()
        try:
            adapter, ad, output, od, a, o = self._pick(entries)
            self._dev, self._ctx, self.feature_level = create_device(adapter)
            dup, hr = duplicate(output, self._dev, self.use_output5)
            if dup is None:
                raise DXGIError(f"DuplicateOutput (adapter {a} "
                                f"'{ad.Description}', output {o})", hr)
            self._dup = dup
            desc = DXGI_OUTDUPL_DESC()
            dup.call(_DUP_GETDESC, (ctypes.POINTER(DXGI_OUTDUPL_DESC),),
                     ctypes.byref(desc), restype=None)
            r = od.DesktopCoordinates
            self.origin = (r.left, r.top)
            self.size = (r.right - r.left, r.bottom - r.top)
            self.rotation = desc.Rotation
            self.in_sysmem = bool(desc.DesktopImageInSystemMemory)
            self.mode = (desc.ModeDesc.Width, desc.ModeDesc.Height,
                         desc.ModeDesc.RefreshRate.Numerator,
                         desc.ModeDesc.RefreshRate.Denominator,
                         desc.ModeDesc.Format)
            self.adapter_name = ad.Description
            self.output_name = od.DeviceName
            self.adapter_index, self.output_index = a, o
            self._fresh = True    # next grab waits for a full image
        except Exception:
            self.close()
            raise
        finally:
            release_enum(entries)

    def close(self):
        if self._holding and self._dup:
            self._dup.call(_DUP_RELEASE, ())
        self._holding = False
        for name in ("_staging", "_dup", "_ctx", "_dev"):
            obj = getattr(self, name)
            if obj:
                obj.release()
            setattr(self, name, None)
        self._staging_size = None

    def _lost(self, hr):
        """Drop everything; the next grab() re-opens."""
        self.last_error = hr & 0xFFFFFFFF
        self.reopens += 1
        self.close()

    # ------------------------------------------------------------ grab
    def _ensure_staging(self, w, h):
        if self._staging_size == (w, h):
            return
        if self._staging:
            self._staging.release()
        d = D3D11_TEXTURE2D_DESC(w, h, 1, 1, DXGI_FORMAT_B8G8R8A8_UNORM, 1, 0,
                                 D3D11_USAGE_STAGING, 0,
                                 D3D11_CPU_ACCESS_READ, 0)
        tex = ctypes.c_void_p()
        hr = self._dev.call(_DEV_CREATETEX2D,
                            (ctypes.POINTER(D3D11_TEXTURE2D_DESC), PVOID,
                             PPVOID), ctypes.byref(d), None,
                            ctypes.byref(tex))
        if hr < 0:
            raise DXGIError("CreateTexture2D(staging)", hr)
        self._staging = Com(tex)
        self._staging_size = (w, h)

    def _box(self, rect):
        """Source box inside the output for `rect` (desktop coords)."""
        ow, oh = self.size
        if rect is None:
            return 0, 0, ow, oh
        x0 = max(0, rect[0] - self.origin[0])
        y0 = max(0, rect[1] - self.origin[1])
        x1 = min(ow, rect[0] - self.origin[0] + rect[2])
        y1 = min(oh, rect[1] - self.origin[1] + rect[3])
        if x1 <= x0 or y1 <= y0:
            raise DXGIError("capture rect outside the output",
                            E_INVALIDARG)
        return x0, y0, x1, y1

    def grab(self, rect=None, timeout_ms=0, force=False):
        """BGRA (h, w, 4) uint8 copy of `rect` (desktop px, mss-style dict;
        None = the whole output) if the desktop changed since the last
        grab, else None. Waits up to timeout_ms for a change. force=True
        returns the last image instead of None (same array object; treat it
        as read-only). Right after (re)opening, waits up to
        max(timeout_ms, FIRST_IMAGE_MS) for the first image: the first
        acquires after DuplicateOutput are often pointer-only."""
        self._follow_rect(_rect_tuple(rect))
        if not self._dup:
            self.open()           # raises DXGIError (e.g. E_ACCESSDENIED)
        if self._fresh:
            # deadline counted from AFTER open(): the first open in a
            # process costs 80-350 ms (enumeration + device creation)
            deadline = time.perf_counter() + max(timeout_ms,
                                                 self.FIRST_IMAGE_MS) / 1000
            while True:
                left = int((deadline - time.perf_counter()) * 1000)
                img = self._grab_once(rect, max(0, left))
                if img is not None or not self._dup or left <= 0:
                    break
        else:
            img = self._grab_once(rect, timeout_ms)
        if img is not None:
            self._fresh = False
            self.last = img
            return img
        return self.last if force else None

    FIRST_IMAGE_MS = 1000

    def _follow_rect(self, rect):
        """A new capture rect (resolution change, other monitor): drop the
        duplication so the next acquire re-picks the output and hands out
        a full image (an unchanged desktop would otherwise never deliver
        the newly covered area)."""
        if rect is not None and rect != self._want:
            self.close()
            self._want = rect
            self.last = None

    def _grab_once(self, rect=None, timeout_ms=0):
        rect = _rect_tuple(rect)
        if not self._dup:
            self.open()           # raises DXGIError (e.g. E_ACCESSDENIED)
        dup = self._dup
        if self._holding:
            dup.call(_DUP_RELEASE, ())
            self._holding = False
        info = self.last_info
        res = ctypes.c_void_p()
        t0 = time.perf_counter()
        hr = dup.call(_DUP_ACQUIRE,
                      (UINT, ctypes.POINTER(DXGI_OUTDUPL_FRAME_INFO), PPVOID),
                      int(timeout_ms), ctypes.byref(info), ctypes.byref(res))
        t1 = time.perf_counter()
        self.t_acquire, self.t_copy = (t1 - t0) * 1000, 0.0
        if hr < 0:
            hr &= 0xFFFFFFFF
            if hr == DXGI_ERROR_WAIT_TIMEOUT:
                return None
            if hr in _REOPEN:
                self._lost(hr)
                return None
            raise DXGIError("AcquireNextFrame", hr)
        self._holding = True
        res = Com(res)
        try:
            if info.LastMouseUpdateTime:
                p = info.PointerPosition
                self.pointer_visible = bool(p.Visible)
                self.pointer_pos = (p.Position.x + self.origin[0],
                                    p.Position.y + self.origin[1])
            if info.PointerShapeBufferSize:
                self._read_pointer_shape(info.PointerShapeBufferSize)
            if info.LastPresentTime == 0:
                return None       # pointer-only update: image unchanged
            now = ctypes.c_longlong()
            _k32.QueryPerformanceCounter(ctypes.byref(now))
            self.t_latency = (now.value - info.LastPresentTime) * 1000 / _QPF
            img = self._copy_out(res, rect)
            self.t_copy = (time.perf_counter() - t1) * 1000
            return img
        finally:
            res.release()
            if not self.hold_frame and self._holding:
                dup.call(_DUP_RELEASE, ())
                self._holding = False

    def _read_pointer_shape(self, size):
        buf = (ctypes.c_ubyte * size)()
        need = UINT()
        si = DXGI_OUTDUPL_POINTER_SHAPE_INFO()
        hr = self._dup.call(_DUP_POINTERSHAPE,
                            (UINT, PVOID, ctypes.POINTER(UINT),
                             ctypes.POINTER(DXGI_OUTDUPL_POINTER_SHAPE_INFO)),
                            size, buf, ctypes.byref(need), ctypes.byref(si))
        if hr >= 0:
            self.pointer_shape = (si.Type, si.Width, si.Height, si.Pitch,
                                  (si.HotSpot.x, si.HotSpot.y), bytes(buf))

    def _copy_out(self, res, rect):
        if self.rotation not in (0, 1):
            # Untested here (no rotated panel): the duplicated surface is in
            # scan-out orientation; copy it whole and let numpy rotate.
            full = self._copy_box(res, (0, 0) + self._tex_size(res))
            k = {2: 1, 3: 2, 4: 3}[self.rotation]
            img = np.ascontiguousarray(np.rot90(full, k))
            x0, y0, x1, y1 = self._box(rect)
            return np.ascontiguousarray(img[y0:y1, x0:x1])
        return self._copy_box(res, self._box(rect))

    def _tex_size(self, res):
        tex = res.qi(IID_ID3D11Texture2D)
        try:
            d = D3D11_TEXTURE2D_DESC()
            tex.call(_TEX_GETDESC, (ctypes.POINTER(D3D11_TEXTURE2D_DESC),),
                     ctypes.byref(d), restype=None)
            return d.Width, d.Height
        finally:
            tex.release()

    def _copy_box(self, res, box):
        x0, y0, x1, y1 = box
        w, h = x1 - x0, y1 - y0
        if self.in_sysmem:
            # Rare: the desktop image already lives in system memory.
            mr = DXGI_MAPPED_RECT()
            hr = self._dup.call(_DUP_MAP, (ctypes.POINTER(DXGI_MAPPED_RECT),),
                                ctypes.byref(mr))
            if hr < 0:
                raise DXGIError("MapDesktopSurface", hr)
            try:
                return self._rows(mr.pBits, mr.Pitch, x0, y0, w, h)
            finally:
                self._dup.call(_DUP_UNMAP, ())
        tex = res.qi(IID_ID3D11Texture2D)
        try:
            self._ensure_staging(w, h)
            if (x0, y0) == (0, 0) and (w, h) == self.size:
                self._ctx.call(_CTX_COPY, (PVOID, PVOID), self._staging.p,
                               tex.p, restype=None)
            else:
                b = D3D11_BOX(x0, y0, 0, x1, y1, 1)
                self._ctx.call(_CTX_COPYREGION,
                               (PVOID, UINT, UINT, UINT, UINT, PVOID, UINT,
                                ctypes.POINTER(D3D11_BOX)),
                               self._staging.p, 0, 0, 0, 0, tex.p, 0,
                               ctypes.byref(b), restype=None)
        finally:
            tex.release()
        return self._map_staging(w, h)

    def _map_staging(self, w, h):
        m = D3D11_MAPPED_SUBRESOURCE()
        hr = self._ctx.call(_CTX_MAP,
                            (PVOID, UINT, UINT, UINT,
                             ctypes.POINTER(D3D11_MAPPED_SUBRESOURCE)),
                            self._staging.p, 0, D3D11_MAP_READ, 0,
                            ctypes.byref(m))
        if hr < 0:
            raise DXGIError("Map(staging)", hr)
        try:
            return self._rows(m.pData, m.RowPitch, 0, 0, w, h)
        finally:
            self._ctx.call(_CTX_UNMAP, (PVOID, UINT), self._staging.p, 0,
                           restype=None)

    def _out(self, w, h):
        if not self.ring:
            return np.empty((h, w, 4), np.uint8)
        if not self._bufs or self._bufs[0].shape != (h, w, 4):
            self._bufs = [np.empty((h, w, 4), np.uint8)
                          for _ in range(self.ring)]
        self._buf_i = (self._buf_i + 1) % self.ring
        return self._bufs[self._buf_i]

    def _rows(self, ptr, pitch, x0, y0, w, h):
        src = np.ctypeslib.as_array(
            ctypes.cast(ptr, ctypes.POINTER(ctypes.c_ubyte)),
            shape=((y0 + h) * pitch,))
        view = src.reshape(y0 + h, pitch)[y0:y0 + h, x0 * 4:(x0 + w) * 4]
        out = self._out(w, h)
        out.reshape(h, w * 4)[...] = view   # one memcpy when pitch == w*4
        return out

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ============================================================ __main__
def _pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def _stats(name, xs, unit="ms"):
    import statistics
    if not xs:
        print(f"  {name:36} (no samples)")
        return
    print(f"  {name:36} median {statistics.median(xs):7.2f}  p5 "
          f"{_pct(xs, 5):7.2f}  p95 {_pct(xs, 95):7.2f}  max {max(xs):7.2f}"
          f" {unit}  (n={len(xs)})")


def probe():
    """Try DuplicateOutput for every (device adapter, attached output)."""
    set_dpi_aware()
    entries = enum_outputs()
    try:
        adapters = {}
        for adapter, ad, output, od, a, o in entries:
            adapters.setdefault(a, (adapter, ad))
            if od is None:
                print(f"adapter {a}: '{ad.Description}' (no outputs)")
                continue
            r = od.DesktopCoordinates
            print(f"adapter {a}: '{ad.Description}'  output {o}: "
                  f"{od.DeviceName} attached={bool(od.AttachedToDesktop)} "
                  f"rect=({r.left},{r.top},{r.right},{r.bottom})")
        for adapter, ad, output, od, a, o in entries:
            if od is None or not od.AttachedToDesktop:
                continue
            for da, (dadapter, dad) in adapters.items():
                for use5 in (False, True):
                    dev, ctx, fl = create_device(dadapter)
                    try:
                        dup, hr = duplicate(output, dev, use5)
                    except DXGIError as e:
                        dup, hr = None, e.hr
                    tag = "DuplicateOutput1" if use5 else "DuplicateOutput "
                    print(f"  output {a}/{o} <- device on adapter {da} "
                          f"('{dad.Description}'): {tag} -> "
                          f"{hr_str(hr) if hr else 'S_OK'}")
                    if dup:
                        dup.release()
                    ctx.release()
                    dev.release()
    finally:
        release_enum(entries)


def bench_live(seconds, timeout_ms, hold=True, rect=None, ring=0):
    """Grab loop as a 60 fps streamer would run it. Frames only arrive when
    something on the monitor was presented, so the rate here is whatever
    the desktop produced in that window -- NOT a capacity number."""
    cap = DXGICapture(rect, hold_frame=hold, ring=ring)
    t0 = time.perf_counter()
    cap.open()
    print(f"\nopen(): {(time.perf_counter() - t0) * 1000:.1f} ms  adapter "
          f"{cap.adapter_index} '{cap.adapter_name}' output "
          f"{cap.output_index} {cap.output_name} origin={cap.origin} "
          f"size={cap.size} mode={cap.mode} rotation={cap.rotation} "
          f"DesktopImageInSystemMemory={cap.in_sysmem} hold_frame={hold} "
          f"ring={ring}")
    t0 = time.perf_counter()
    first = cap.grab(rect, timeout_ms=1000)
    print(f"first image after open: "
          f"{None if first is None else first.shape} in "
          f"{(time.perf_counter() - t0) * 1000:.1f} ms, mean "
          f"{first[..., :3].mean():.1f}")
    acq_new, copy, lat, accum, arrivals = [], [], [], [], []
    ptr_only = timeouts = 0
    c0, w0 = time.process_time(), time.perf_counter()
    end = w0 + seconds
    while time.perf_counter() < end:
        img = cap.grab(rect, timeout_ms=timeout_ms)
        if img is None:
            if cap._holding:
                ptr_only += 1
            else:
                timeouts += 1
            continue
        arrivals.append(time.perf_counter())
        acq_new.append(cap.t_acquire)
        copy.append(cap.t_copy)
        lat.append(cap.t_latency)
        accum.append(cap.last_info.AccumulatedFrames)
    wall = time.perf_counter() - w0
    cpu = time.process_time() - c0
    n = len(arrivals)
    print(f"live {wall:.1f}s, timeout {timeout_ms} ms: {n} new images "
          f"({n / wall:.1f}/s), {ptr_only} pointer-only, {timeouts} "
          f"timeouts, {cap.reopens} reopens; process CPU "
          f"{cpu / wall * 100:.1f}% of one core")
    _stats("AcquireNextFrame wait (new image)", acq_new)
    _stats("present -> acquired latency", lat)
    _stats("copy-out (copy+Map+memcpy)", copy)
    _stats("AccumulatedFrames per image", accum, "")
    if n > 1:
        _stats("interval between new images",
               [(b - a) * 1000 for a, b in zip(arrivals, arrivals[1:])])
    cap.close()


def bench_copy(iters, box=None, pace_fps=0, ring=0):
    """Copy-out capacity, independent of how often the desktop changes:
    snapshot one duplicated frame into a GPU texture, then repeat
    CopyResource/CopySubresourceRegion -> staging -> Map -> numpy copy.
    pace_fps > 0 runs it on 1/fps deadlines to price CPU at that rate."""
    cap = DXGICapture(ring=ring)
    cap.open()
    while True:   # skip pointer-only acquires: their surface is still black
        info = DXGI_OUTDUPL_FRAME_INFO()
        res = ctypes.c_void_p()
        hr = cap._dup.call(_DUP_ACQUIRE, (UINT, ctypes.POINTER(
            DXGI_OUTDUPL_FRAME_INFO), PPVOID), 5000, ctypes.byref(info),
            ctypes.byref(res))
        if hr < 0:
            raise DXGIError("AcquireNextFrame", hr)
        res = Com(res)
        if info.LastPresentTime:
            break
        res.release()
        cap._dup.call(_DUP_RELEASE, ())
    src = res.qi(IID_ID3D11Texture2D)
    w0, h0 = cap.size
    d = D3D11_TEXTURE2D_DESC(w0, h0, 1, 1, DXGI_FORMAT_B8G8R8A8_UNORM, 1, 0,
                             0, 0, 0, 0)                 # USAGE_DEFAULT
    snap = ctypes.c_void_p()
    hr = cap._dev.call(_DEV_CREATETEX2D, (ctypes.POINTER(
        D3D11_TEXTURE2D_DESC), PVOID, PPVOID), ctypes.byref(d), None,
        ctypes.byref(snap))
    if hr < 0:
        raise DXGIError("CreateTexture2D", hr)
    snap = Com(snap)
    cap._ctx.call(_CTX_COPY, (PVOID, PVOID), snap.p, src.p, restype=None)
    src.release()
    res.release()
    cap._dup.call(_DUP_RELEASE, ())

    x0, y0, x1, y1 = (0, 0, w0, h0) if box is None else \
        (box[0], box[1], box[0] + box[2], box[1] + box[3])
    w, h = x1 - x0, y1 - y0
    cap._ensure_staging(w, h)
    bx = D3D11_BOX(x0, y0, 0, x1, y1, 1)
    sub, mp, tot, late = [], [], [], 0
    period = 1 / pace_fps if pace_fps else 0
    c0 = time.process_time()
    w_start = deadline = time.perf_counter()
    for _ in range(iters):
        if period:
            now = time.perf_counter()
            if now < deadline:
                time.sleep(deadline - now)
        t0 = time.perf_counter()
        if box is None:
            cap._ctx.call(_CTX_COPY, (PVOID, PVOID), cap._staging.p, snap.p,
                          restype=None)
        else:
            cap._ctx.call(_CTX_COPYREGION, (PVOID, UINT, UINT, UINT, UINT,
                          PVOID, UINT, ctypes.POINTER(D3D11_BOX)),
                          cap._staging.p, 0, 0, 0, 0, snap.p, 0,
                          ctypes.byref(bx), restype=None)
        t1 = time.perf_counter()
        img = cap._map_staging(w, h)
        t2 = time.perf_counter()
        sub.append((t1 - t0) * 1000)
        mp.append((t2 - t1) * 1000)
        tot.append((t2 - t0) * 1000)
        if period:
            deadline += period
            if t2 > deadline:
                late += 1
                deadline = t2
    wall = time.perf_counter() - w_start
    cpu = time.process_time() - c0
    label = ("paced %d fps" % pace_fps if pace_fps else "flat out") \
        + f", ring={ring}"
    print(f"\ncopy-out {label}, {iters} iters, "
          f"{'full' if box is None else box} -> {img.shape}: "
          f"{iters / wall:.1f}/s, {late} late, process CPU "
          f"{cpu / wall * 100:.1f}% of one core ({cpu / iters * 1000:.2f} ms "
          f"CPU per frame)")
    _stats("Copy*Resource submit", sub)
    _stats("Map(wait GPU)+memcpy+Unmap", mp)
    _stats("total", tot)
    print(f"  image mean {img[..., :3].mean():.1f} (0 would mean black)")
    snap.release()
    cap.close()
    return img


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="DXGI Desktop Duplication probe"
                                 " + benchmark (read-only screen capture)")
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--timeout", type=int, default=16,
                    help="AcquireNextFrame timeout (ms) in the live loop")
    ap.add_argument("--iters", type=int, default=300)
    ap.add_argument("--no-gpu-fix", action="store_true",
                    help="don't set SHIM_MCCOMPAT (reproduces UNSUPPORTED)")
    ap.add_argument("--no-hold", action="store_true")
    ap.add_argument("--only", choices=["probe", "live", "copy"])
    ap.add_argument("--ring", type=int, default=4,
                    help="preallocated output arrays (0 = new array each)")
    args = ap.parse_args()
    if not args.no_gpu_fix:
        prev = prefer_display_gpu()
        print(f"SHIM_MCCOMPAT: inherited {prev!r} -> 0x800000000")
    if args.only in (None, "probe"):
        probe()
    if args.only in (None, "live"):
        bench_live(args.seconds, args.timeout, hold=not args.no_hold,
                   ring=args.ring)
    if args.only in (None, "copy"):
        bench_copy(args.iters, ring=0)
        bench_copy(args.iters, ring=args.ring)
        bench_copy(args.iters, pace_fps=60, ring=args.ring)
        bench_copy(args.iters, box=(320, 180, 1280, 720), ring=args.ring)
