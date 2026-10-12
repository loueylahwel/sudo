"""Sound for the phone screen: the apps on it play through the PHONE.

The phone screen (phone_monitor.py) moves picture only; a movie's sound would
still come out of the PC's speakers, over whatever the PC user is doing. So
while the phone asks for it (phone.audio), the agent:

  * captures each phone-screen app's own audio with Windows' per-process
    loopback (Windows 10 2004+, the API OBS's application capture uses),
    which taps that process tree only, not the whole PC mix;
  * turns those apps down to a silent 0.1% in the PC's volume mixer. A real
    mute would also silence the capture (the tap sits after the session
    volume), but capturing in float and multiplying by 1000 restores the
    original signal exactly while the PC hears it at -60 dB;
  * mixes the apps, converts to 16-bit stereo 48 kHz and hands 40 ms chunks
    to the agent, which sends them to the phone as PCM1 packets.

Windows remembers per-app mixer volumes, so an agent that died while an app
was turned down would leave it near-silent for good: every original volume is
persisted through a callback before it is changed, and restore_saved() puts
them back on the next start.
"""

import sys

if "comtypes" not in sys.modules:
    # Capture and the activation callback run on MTA threads; make the
    # importing thread MTA too (comtypes calls CoInitializeEx on import).
    sys.coinit_flags = 0

import array
import ctypes
import threading
import time
from ctypes import wintypes

import comtypes
import psutil
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown, COMObject

RATE = 48000
CHANNELS = 2
CHUNK_FRAMES = RATE // 25          # 40 ms per packet
PC_VOLUME = 0.001                  # what the PC hears while the phone plays
GAIN = 1.0 / PC_VOLUME             # undoes PC_VOLUME on the captured float

_LOOPBACK = 0x00020000
_EVENTCALLBACK = 0x00040000
_AUTOCONVERTPCM = 0x80000000
_SRC_DEFAULT_QUALITY = 0x08000000
_BUFFERFLAGS_SILENT = 0x2
_WAVE_FORMAT_IEEE_FLOAT = 3


# ---------------------------------------------------------------- COM

class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = [COMMETHOD(
        [], HRESULT, "GetActivateResult",
        (["out"], ctypes.POINTER(HRESULT), "activateResult"),
        (["out"], ctypes.POINTER(ctypes.POINTER(IUnknown)),
         "activatedInterface"))]


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = [COMMETHOD(
        [], HRESULT, "ActivateCompleted",
        (["in"], ctypes.POINTER(IActivateAudioInterfaceAsyncOperation), "op"))]


class IAgileObject(IUnknown):
    _iid_ = GUID("{94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90}")
    _methods_ = []


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
                ("nSamplesPerSec", wintypes.DWORD),
                ("nAvgBytesPerSec", wintypes.DWORD),
                ("nBlockAlign", wintypes.WORD),
                ("wBitsPerSample", wintypes.WORD), ("cbSize", wintypes.WORD)]


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetBuffer",
                  (["out"], ctypes.POINTER(ctypes.POINTER(ctypes.c_byte)),
                   "data"),
                  (["out"], ctypes.POINTER(ctypes.c_uint32), "frames"),
                  (["out"], ctypes.POINTER(wintypes.DWORD), "flags"),
                  (["out"], ctypes.POINTER(ctypes.c_uint64), "devpos"),
                  (["out"], ctypes.POINTER(ctypes.c_uint64), "qpcpos")),
        COMMETHOD([], HRESULT, "ReleaseBuffer",
                  (["in"], ctypes.c_uint32, "frames")),
        COMMETHOD([], HRESULT, "GetNextPacketSize",
                  (["out"], ctypes.POINTER(ctypes.c_uint32), "n")),
    ]


class IAudioClient(IUnknown):
    _iid_ = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Initialize", (["in"], ctypes.c_int, "mode"),
                  (["in"], wintypes.DWORD, "flags"),
                  (["in"], ctypes.c_int64, "duration"),
                  (["in"], ctypes.c_int64, "period"),
                  (["in"], ctypes.POINTER(WAVEFORMATEX), "fmt"),
                  (["in"], ctypes.c_void_p, "session")),
        COMMETHOD([], HRESULT, "GetBufferSize",
                  (["out"], ctypes.POINTER(ctypes.c_uint32), "n")),
        COMMETHOD([], HRESULT, "GetStreamLatency",
                  (["out"], ctypes.POINTER(ctypes.c_int64), "latency")),
        COMMETHOD([], HRESULT, "GetCurrentPadding",
                  (["out"], ctypes.POINTER(ctypes.c_uint32), "padding")),
        COMMETHOD([], HRESULT, "IsFormatSupported",
                  (["in"], ctypes.c_int, "mode"),
                  (["in"], ctypes.POINTER(WAVEFORMATEX), "fmt"),
                  (["in"], ctypes.c_void_p, "closest")),
        COMMETHOD([], HRESULT, "GetMixFormat", (["in"], ctypes.c_void_p, "f")),
        COMMETHOD([], HRESULT, "GetDevicePeriod",
                  (["in"], ctypes.c_void_p, "a"), (["in"], ctypes.c_void_p, "b")),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle",
                  (["in"], wintypes.HANDLE, "event")),
        COMMETHOD([], HRESULT, "GetService",
                  (["in"], ctypes.POINTER(GUID), "iid"),
                  (["out"], ctypes.POINTER(ctypes.POINTER(IUnknown)), "obj")),
    ]


class _LoopbackParams(ctypes.Structure):
    _fields_ = [("TargetProcessId", wintypes.DWORD),
                ("ProcessLoopbackMode", ctypes.c_int)]  # 0 = include tree


class _ActivationParams(ctypes.Structure):
    _fields_ = [("ActivationType", ctypes.c_int),       # 1 = process loopback
                ("ProcessLoopbackParams", _LoopbackParams)]


class _Blob(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.ULONG), ("pBlobData", ctypes.c_void_p)]


class _PropVariant(ctypes.Structure):
    _fields_ = [("vt", wintypes.USHORT), ("r1", wintypes.WORD),
                ("r2", wintypes.WORD), ("r3", wintypes.WORD), ("blob", _Blob)]


class _Done(COMObject):
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self):
        super().__init__()
        self.event = threading.Event()

    def ActivateCompleted(self, op):
        self.event.set()
        return 0


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateEventW.restype = wintypes.HANDLE
_k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL,
                              wintypes.LPCWSTR]
_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


# ---------------------------------------------------------------- capture

class _ProcessCapture:
    """Float32 stereo 48 kHz loopback of one process tree, on its own MTA
    thread, into a deque of float samples the mixer drains."""

    def __init__(self, pid):
        self.pid = pid
        self.samples = array.array("f")
        self.lock = threading.Lock()
        self.error = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"sudo-audio-{pid}")
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)

    def take(self, n):
        """Up to n interleaved float samples (fewer when not enough came)."""
        with self.lock:
            out = self.samples[:n]
            del self.samples[:n]
            return out

    def _activate(self):
        params = _ActivationParams(1, _LoopbackParams(self.pid, 0))
        pv = _PropVariant()
        pv.vt = 65  # VT_BLOB
        pv.blob.cbSize = ctypes.sizeof(params)
        pv.blob.pBlobData = ctypes.cast(ctypes.pointer(params), ctypes.c_void_p)
        fn = ctypes.WinDLL("Mmdevapi").ActivateAudioInterfaceAsync
        fn.restype = HRESULT
        fn.argtypes = [
            wintypes.LPCWSTR, ctypes.POINTER(GUID), ctypes.POINTER(_PropVariant),
            ctypes.POINTER(IActivateAudioInterfaceCompletionHandler),
            ctypes.POINTER(ctypes.POINTER(IActivateAudioInterfaceAsyncOperation))]
        done = _Done()
        op = ctypes.POINTER(IActivateAudioInterfaceAsyncOperation)()
        fn("VAD\\Process_Loopback", ctypes.byref(IAudioClient._iid_),
           ctypes.byref(pv),
           done.QueryInterface(IActivateAudioInterfaceCompletionHandler),
           ctypes.byref(op))
        if not done.event.wait(5):
            raise OSError("audio capture activation timed out")
        hr, unk = op.GetActivateResult()
        if hr != 0:
            raise OSError(hr, "this PC can't capture app audio (needs "
                              "Windows 10 2004 or newer)")
        return unk.QueryInterface(IAudioClient)

    def _run(self):
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        event = None
        try:
            client = self._activate()
            fmt = WAVEFORMATEX(_WAVE_FORMAT_IEEE_FLOAT, CHANNELS, RATE,
                               RATE * CHANNELS * 4, CHANNELS * 4, 32, 0)
            event = _k32.CreateEventW(None, False, False, None)
            client.Initialize(0, _LOOPBACK | _EVENTCALLBACK | _AUTOCONVERTPCM
                              | _SRC_DEFAULT_QUALITY, 200000, 0,
                              ctypes.byref(fmt), None)
            client.SetEventHandle(event)
            cap = client.GetService(
                ctypes.byref(IAudioCaptureClient._iid_)).QueryInterface(
                    IAudioCaptureClient)
            client.Start()
            frame_bytes = CHANNELS * 4
            while not self._stop.is_set():
                _k32.WaitForSingleObject(event, 100)
                while cap.GetNextPacketSize() > 0:
                    data, n, flags, _, _ = cap.GetBuffer()
                    if n:
                        if flags & _BUFFERFLAGS_SILENT:
                            chunk = array.array("f", bytes(n * frame_bytes))
                        else:
                            chunk = array.array("f", ctypes.string_at(
                                data, n * frame_bytes))
                        with self.lock:
                            self.samples.extend(chunk)
                            # Never let a stalled consumer build up latency:
                            # keep at most ~0.5s.
                            excess = len(self.samples) - RATE * CHANNELS // 2
                            if excess > 0:
                                del self.samples[:excess]
                    cap.ReleaseBuffer(n)
            client.Stop()
        except Exception as e:
            self.error = e
            print(f"[agent] phone audio capture for pid {self.pid}: {e}")
        finally:
            if event:
                _k32.CloseHandle(event)
            comtypes.CoUninitialize()


# ---------------------------------------------------------------- mixer volumes

def _tree(pid):
    try:
        p = psutil.Process(pid)
        return {pid} | {c.pid for c in p.children(recursive=True)}
    except psutil.Error:
        return {pid}


def _sessions_for(pids):
    """(exe path, ISimpleAudioVolume) for every audio session of pids."""
    from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
    out = []
    for s in AudioUtilities.GetAllSessions():
        proc = s.Process
        if proc is None or proc.pid not in pids:
            continue
        try:
            exe = proc.exe()
        except psutil.Error:
            exe = proc.name()
        out.append((exe, s._ctl.QueryInterface(ISimpleAudioVolume)))
    return out


# ---------------------------------------------------------------- manager

class PhoneAudio:
    """Plays the phone screen's apps on the phone, silenced on the PC.

    set_targets(pids) follows the apps on the phone screen (call it about
    once a second); on_chunk(pcm_bytes) gets 40 ms of s16le stereo 48 kHz.
    saved / persist: {exe path: original mixer volume} of every app the
    agent turned down, persisted BEFORE turning it down."""

    def __init__(self, on_chunk, saved=None, persist=None):
        self.on_chunk = on_chunk
        self.saved = dict(saved or {})
        self.persist = persist
        self._captures = {}       # root pid -> _ProcessCapture
        self._lock = threading.Lock()
        self._running = False
        self._mixer = None

    @property
    def running(self):
        return self._running

    def start(self):
        if self._running:
            return
        self._running = True
        self._mixer = threading.Thread(target=self._mix_loop, daemon=True,
                                       name="sudo-audio-mixer")
        self._mixer.start()

    def stop(self):
        self._running = False
        if self._mixer:
            self._mixer.join(timeout=2)
            self._mixer = None
        self.set_targets([])

    def errors(self):
        return [str(c.error) for c in self._captures.values() if c.error]

    def set_targets(self, pids):
        """Capture exactly these process trees (each pid = a phone-screen
        app's window owner) and keep their PC volume down; restore apps that
        left the phone screen."""
        pids = set(int(p) for p in pids) if self._running else set()
        with self._lock:
            for pid in list(self._captures):
                if pid not in pids:
                    self._captures.pop(pid).stop()
            for pid in pids:
                if pid not in self._captures:
                    self._captures[pid] = _ProcessCapture(pid)
        self._apply_volumes(pids)

    def _apply_volumes(self, pids):
        """Turn the target trees down (new sessions appear whenever an app
        starts playing, so this runs on every set_targets) and restore every
        app the agent turned down that isn't a target any more."""
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        try:
            tree = set()
            for pid in pids:
                tree |= _tree(pid)
            targets = _sessions_for(tree) if tree else []
            target_exes = {exe for exe, _ in targets}
            changed = False
            for exe, vol in targets:
                if exe not in self.saved:
                    self.saved[exe] = float(vol.GetMasterVolume())
                    changed = True
            if changed and self.persist:
                self.persist(dict(self.saved))  # before touching anything
            for exe, vol in targets:
                if abs(vol.GetMasterVolume() - PC_VOLUME) > 1e-4:
                    vol.SetMasterVolume(PC_VOLUME, None)
            stale = {exe: v for exe, v in self.saved.items()
                     if exe not in target_exes}
            if stale:
                self._restore(stale)
        except Exception as e:
            print(f"[agent] phone audio volume: {e}")
        finally:
            comtypes.CoUninitialize()

    def _restore(self, volumes):
        """Put the given apps' mixer volumes back (sessions of any running
        process with that exe), then forget them."""
        from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
        for s in AudioUtilities.GetAllSessions():
            proc = s.Process
            if proc is None:
                continue
            try:
                exe = proc.exe()
            except psutil.Error:
                exe = proc.name()
            if exe in volumes:
                try:
                    s._ctl.QueryInterface(ISimpleAudioVolume).SetMasterVolume(
                        float(volumes[exe]), None)
                except Exception:
                    pass
        for exe in volumes:
            self.saved.pop(exe, None)
        if self.persist:
            self.persist(dict(self.saved))

    def restore_saved(self):
        """Startup: put back volumes a previous agent left turned down."""
        if not self.saved:
            return
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        try:
            self._restore(dict(self.saved))
        except Exception as e:
            print(f"[agent] phone audio restore: {e}")
        finally:
            comtypes.CoUninitialize()

    def _mix_loop(self):
        import numpy as np
        n = CHUNK_FRAMES * CHANNELS
        period = CHUNK_FRAMES / RATE
        next_at = time.monotonic()
        while self._running:
            next_at += period
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_at = time.monotonic()  # fell behind: don't burst
            with self._lock:
                caps = list(self._captures.values())
            mix = np.zeros(n, np.float32)
            got = False
            for c in caps:
                part = c.take(n)
                if part:
                    got = True
                    mix[:len(part)] += np.frombuffer(part, np.float32)
            if not got:
                continue
            pcm = (np.clip(mix * GAIN, -1.0, 1.0) * 32767).astype("<i2")
            try:
                self.on_chunk(pcm.tobytes())
            except Exception:
                pass
