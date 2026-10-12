"""Display brightness for the agent (display.brightness.get / .set).

Windows splits the job between two unrelated APIs, so there are two
backends:

- Built-in laptop panels: WMI (root/WMI WmiMonitorBrightness for the level,
  WmiMonitorBrightnessMethods.WmiSetBrightness to change it). The panel's
  backlight belongs to the GPU driver; DDC/CI can't reach it.
- External monitors: DDC/CI through dxva2.dll (the Monitor Configuration
  API). WMI only covers backlights a driver owns, so it never lists them.

A display is listed by whichever backend can actually read it; one that
answers neither (DDC/CI switched off in the monitor's OSD, a TV, a
DisplayLink dock) is simply absent, and "supported" is false when nothing
answers at all.

Display ids are opaque strings for the phone: "wmi:<InstanceName>" or
"ddc:<\\\\.\\DISPLAYn>#<index>". Levels are always 0-100.
"""

import asyncio
import base64
import json
import os
import platform
import subprocess

# ---------------------------------------------------------------- WMI

# One PowerShell round-trip does read -> (optional) set -> report, because
# PowerShell's start-up (~0.4s) is the expensive part. The target id and
# values travel in environment variables, never spliced into the script
# text, so a display id sent by the phone can't inject PowerShell.
# The script sticks to .NET (System.Management via [wmisearcher]) and
# builds its JSON by hand: Get-CimInstance / Where-Object / ConvertTo-Json
# would each auto-load a module, which measured ~150ms slower per call —
# and a dragged slider pays that on every change.
_WMI_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$mode = $env:SUDO_BRIGHT_MODE
$value = [int]$env:SUDO_BRIGHT_VALUE
$target = $env:SUDO_BRIGHT_ID
$scope = [System.Management.ManagementScope]'root\WMI'
$q = [wmisearcher]'SELECT InstanceName, CurrentBrightness, Active FROM WmiMonitorBrightness'
$q.Scope = $scope
# Desktops without a driver-owned backlight answer "Not supported".
try { $panels = @($q.Get()) } catch { $panels = @() }
$methods = @()
if ($mode -and $panels.Count) {
  $q = [wmisearcher]'SELECT * FROM WmiMonitorBrightnessMethods'
  $q.Scope = $scope
  $methods = @($q.Get())
}
$out = foreach ($p in $panels) {
  if (-not $p.Active) { continue }
  $level = [int]$p.CurrentBrightness
  if ($mode -and (-not $target -or $p.InstanceName -eq $target)) {
    $new = if ($mode -eq 'delta') { $level + $value } else { $value }
    $new = [Math]::Max(0, [Math]::Min(100, $new))
    $m = $null
    foreach ($c in $methods) {
      if ($c.InstanceName -eq $p.InstanceName) { $m = $c; break }
    }
    if ($m) {
      [void]$m.WmiSetBrightness(1, $new)  # (Timeout s, Brightness %)
      # Report what was set: CurrentBrightness can lag the call by a few
      # hundred ms, and a stale echo would yank the phone's slider back.
      $level = $new
    }
  }
  $id = ([string]$p.InstanceName).Replace('\', '\\').Replace('"', '\"')
  '{"id":"' + $id + '","level":' + $level + '}'
}
'[' + (@($out) -join ',') + ']'
"""
_WMI_ENCODED = base64.b64encode(_WMI_SCRIPT.encode("utf-16-le")).decode()


def _wmi_start(mode="", value=0, target=""):
    """Launches the WMI script without waiting for it; mode '' only reads.
    Split from _wmi_finish so the caller can do the DDC/CI half while
    PowerShell boots — the two never touch the same display.
    -EncodedCommand sidesteps every layer of command-line quoting between
    Python, CreateProcess and PowerShell."""
    env = dict(os.environ, SUDO_BRIGHT_MODE=mode,
               SUDO_BRIGHT_VALUE=str(int(value)), SUDO_BRIGHT_ID=target)
    return subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-EncodedCommand", _WMI_ENCODED],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, errors="replace", env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _wmi_finish(proc):
    """Waits for a _wmi_start process and returns the active panels as
    [{id, name, level, method}]."""
    try:
        out, err = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RuntimeError("WMI brightness timed out")
    if proc.returncode != 0:
        lines = [ln for ln in (err or out or "").strip().splitlines()
                 if ln.strip() and not ln.startswith("#< CLIXML")]
        raise RuntimeError(
            f"WMI brightness failed: {lines[0] if lines else proc.returncode}")
    out = out.strip()
    panels = json.loads(out) if out else []
    if isinstance(panels, dict):  # defensive: a lone object, not a list
        panels = [panels]
    many = len(panels) > 1
    return [{
        "id": f"wmi:{p['id']}",
        "name": f"Built-in display {i + 1}" if many else "Built-in display",
        "level": max(0, min(100, int(p["level"]))),
        "method": "wmi",
    } for i, p in enumerate(panels)]


def _wmi_run(mode="", value=0, target=""):
    return _wmi_finish(_wmi_start(mode, value, target))


# ---------------------------------------------------------------- DDC/CI

_ddc_api = None


def _ddc():
    """dxva2/user32 bound with explicit 64-bit-safe signatures. Private
    WinDLL instances, so these argtypes never leak into the shared
    ctypes.windll objects other agent code calls with plain ints."""
    global _ddc_api
    if _ddc_api is not None:
        return _ddc_api
    import ctypes
    from ctypes import wintypes

    class PHYSICAL_MONITOR(ctypes.Structure):
        # The SDK header declares it under #pragma pack(1). 8+256 bytes
        # pads to the same size either way, but say what the ABI says.
        _pack_ = 1
        _fields_ = [("hPhysicalMonitor", wintypes.HANDLE),
                    ("szPhysicalMonitorDescription", wintypes.WCHAR * 128)]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                    ("szDevice", wintypes.WCHAR * 32)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    p_dword = ctypes.POINTER(wintypes.DWORD)
    p_pm = ctypes.POINTER(PHYSICAL_MONITOR)
    sigs = (
        (user32.EnumDisplayMonitors,
         [wintypes.HDC, ctypes.POINTER(wintypes.RECT), enum_proc,
          wintypes.LPARAM]),
        (user32.GetMonitorInfoW,
         [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]),
        (dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR,
         [wintypes.HMONITOR, p_dword]),
        (dxva2.GetPhysicalMonitorsFromHMONITOR,
         [wintypes.HMONITOR, wintypes.DWORD, p_pm]),
        (dxva2.GetMonitorBrightness,
         [wintypes.HANDLE, p_dword, p_dword, p_dword]),
        (dxva2.SetMonitorBrightness, [wintypes.HANDLE, wintypes.DWORD]),
        (dxva2.DestroyPhysicalMonitors, [wintypes.DWORD, p_pm]),
    )
    for fn, argtypes in sigs:
        fn.argtypes = argtypes
        fn.restype = wintypes.BOOL
    _ddc_api = {
        "ctypes": ctypes, "wintypes": wintypes, "user32": user32,
        "dxva2": dxva2, "enum_proc": enum_proc,
        "PHYSICAL_MONITOR": PHYSICAL_MONITOR, "MONITORINFOEXW": MONITORINFOEXW,
    }
    return _ddc_api


def _ddc_session(fn):
    """Opens every physical monitor, calls fn(entries) with the ones that
    answer a DDC/CI brightness read, and always releases the handles.
    entries: [{id, name, handle, min, cur, max}]. Handles are opened per
    call rather than cached: monitors come and go (docks, sleep, KVMs)."""
    api = _ddc()
    ctypes, wintypes, dxva2 = api["ctypes"], api["wintypes"], api["dxva2"]
    hmons = []

    def on_monitor(hmon, _hdc, _rect, _lparam):
        hmons.append(hmon)
        return True

    api["user32"].EnumDisplayMonitors(None, None, api["enum_proc"](on_monitor), 0)
    opened, entries = [], []
    try:
        for hmon in hmons:
            info = api["MONITORINFOEXW"]()
            info.cbSize = ctypes.sizeof(info)
            device = info.szDevice if api["user32"].GetMonitorInfoW(
                hmon, ctypes.byref(info)) else "?"
            count = wintypes.DWORD()
            if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(
                    hmon, ctypes.byref(count)) or not count.value:
                continue
            arr = (api["PHYSICAL_MONITOR"] * count.value)()
            if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, count.value, arr):
                continue
            opened.append((count.value, arr))
            for i, pm in enumerate(arr):
                lo, cur, hi = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
                # Built-in panels have a physical monitor too, but no DDC/CI:
                # the read fails and they stay WMI's business.
                if not dxva2.GetMonitorBrightness(
                        pm.hPhysicalMonitor, ctypes.byref(lo),
                        ctypes.byref(cur), ctypes.byref(hi)) \
                        or hi.value <= lo.value:
                    continue
                entries.append({
                    "id": f"ddc:{device}#{i}",
                    "name": pm.szPhysicalMonitorDescription.strip()
                    or "External display",
                    "handle": pm.hPhysicalMonitor,
                    "min": lo.value, "cur": cur.value, "max": hi.value,
                })
        return fn(entries)
    finally:
        for count, arr in opened:
            dxva2.DestroyPhysicalMonitors(count, arr)


def _ddc_level(e):
    """Monitor-native brightness -> 0-100 (most monitors use 0-100 already,
    but the range is the monitor's to choose)."""
    return round((e["cur"] - e["min"]) * 100 / (e["max"] - e["min"]))


def _ddc_public(e):
    return {"id": e["id"], "name": e["name"], "level": _ddc_level(e),
            "method": "ddc"}


def _ddc_list():
    return _ddc_session(lambda entries: [_ddc_public(e) for e in entries])


def _ddc_set(level=None, delta=None, target=None):
    """Sets matching DDC monitors; returns all of them with their new
    levels. target None = every monitor: one that refuses the write keeps
    its old level in the result instead of failing the whole change (the
    others did change). A refusal from the one targeted monitor raises."""
    def apply(entries):
        dxva2 = _ddc()["dxva2"]
        for e in entries:
            if target is not None and e["id"] != target:
                continue
            new = level if level is not None else _ddc_level(e) + (delta or 0)
            new = max(0, min(100, int(new)))
            raw = e["min"] + round(new * (e["max"] - e["min"]) / 100)
            if not dxva2.SetMonitorBrightness(e["handle"], raw):
                if target is not None:
                    raise RuntimeError(
                        f"{e['name']} refused the brightness change")
                print(f"[agent] brightness: {e['name']} refused the change")
                continue
            e["cur"] = raw  # DDC reads right after a write are unreliable
        return [_ddc_public(e) for e in entries]
    return _ddc_session(apply)


# ---------------------------------------------------------------- backend

def _state(displays):
    return {
        "supported": bool(displays),
        "level": displays[0]["level"] if displays else None,
        "displays": displays,
    }


def _safe(label, fn, *args, **kwargs):
    """A failing backend only hides its own displays: a monitor with DDC/CI
    off must not take the laptop panel's slider down with it."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        print(f"[agent] brightness backend {label} failed: {e}")
        return []


def _side_by_side(wmi_args, ddc_fn, wmi_strict=False, ddc_strict=False):
    """Runs both backends at once -> (panels, monitors). PowerShell is
    launched first and the DDC/CI work (~50ms of I2C round-trips) happens
    while it boots, so a change costs one PowerShell launch instead of the
    sum of both. A non-strict side that fails is logged and contributes no
    displays (see _safe); the strict side is the one a request explicitly
    targeted, and its error propagates to the phone."""
    proc = None
    try:
        proc = _wmi_start(*wmi_args)
    except OSError as e:  # PowerShell missing or blocked by policy
        if wmi_strict:
            raise RuntimeError(f"WMI brightness failed: {e}") from e
        print(f"[agent] brightness backend WMI failed: {e}")
    try:
        monitors = ddc_fn() if ddc_strict else _safe("DDC/CI", ddc_fn)
    except BaseException:
        if proc is not None:
            proc.kill()
            proc.communicate()
        raise
    if proc is None:
        return [], monitors
    panels = _wmi_finish(proc) if wmi_strict else _safe(
        "WMI", _wmi_finish, proc)
    return panels, monitors


def get_state():
    """Blocking. {supported, level (first controllable display), displays}."""
    if platform.system() != "Windows":
        return _state([])
    panels, monitors = _side_by_side(("",), _ddc_list)
    return _state(panels + monitors)


def apply(level=None, delta=None, display_id=None):
    """Blocking. Sets one display (display_id) or every controllable one
    (None) to level, or moves it by delta; both clamp to 0-100. Returns
    the new state. Errors from the targeted backend propagate."""
    if platform.system() != "Windows":
        raise ValueError("brightness control is only supported on Windows")
    if level is not None:
        mode, value = "level", max(0, min(100, int(level)))
    else:
        mode, value = "delta", max(-100, min(100, int(delta or 0)))
    ddc_change = {"level": value if mode == "level" else None,
                  "delta": value if mode == "delta" else None}
    if display_id is None:
        panels, monitors = _side_by_side(
            (mode, value), lambda: _ddc_set(**ddc_change))
        if not panels and not monitors:
            raise ValueError("no display with adjustable brightness")
        return _state(panels + monitors)
    if display_id.startswith("wmi:"):
        panels, monitors = _side_by_side(
            (mode, value, display_id[4:]), _ddc_list, wmi_strict=True)
        if not any(p["id"] == display_id for p in panels):
            raise ValueError(f"unknown display: {display_id}")
        return _state(panels + monitors)
    if display_id.startswith("ddc:"):
        panels, monitors = _side_by_side(
            ("",), lambda: _ddc_set(**ddc_change, target=display_id),
            ddc_strict=True)
        if not any(m["id"] == display_id for m in monitors):
            raise ValueError(f"unknown display: {display_id}")
        return _state(panels + monitors)
    raise ValueError(f"unknown display: {display_id}")


# ---------------------------------------------------------------- control

class BrightnessControl:
    """Serializes and coalesces brightness changes for the agent.

    A phone slider fires a set every ~150ms, while one WMI change costs a
    PowerShell launch (~0.3-0.5s). Running every request in turn would
    queue seconds of stale values behind the user's finger; running them
    in parallel would race on the hardware. So exactly one backend call
    runs at a time, and requests that arrive meanwhile are merged into ONE
    pending change applied as soon as the running call returns: the latest
    absolute level wins, deltas (the -/+ buttons) add up. Every merged
    request resolves with the state that change produced.

    get_state/apply are injectable so the coalescing can be tested with a
    fake backend."""

    def __init__(self, get_state_fn=None, apply_fn=None):
        self._get_state = get_state_fn or get_state
        self._apply = apply_fn or apply
        self._io_lock = None   # asyncio.Lock, created on the agent's loop
        self._pending = {}     # target id (None = all) -> [level, delta]
        self._waiters = []     # futures resolved by the next applied batch
        self._worker = None

    def _lock(self):
        if self._io_lock is None:
            self._io_lock = asyncio.Lock()
        return self._io_lock

    async def get(self):
        async with self._lock():
            return await asyncio.to_thread(self._get_state)

    async def set(self, level=None, delta=None, display_id=None):
        if level is not None:
            level = max(0, min(100, round(float(level))))
        elif delta is not None:
            delta = max(-100, min(100, round(float(delta))))
        else:
            raise ValueError("level or delta is required")
        target = str(display_id) if display_id else None
        self._merge(target, level, delta)
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._drain())
        return await fut

    def _merge(self, target, level, delta):
        if target is None and level is not None:
            # "Everything to X" supersedes every change still pending.
            self._pending = {None: [level, 0]}
            return
        cur = self._pending.get(target)
        if level is not None:
            # Re-insert at the end so it applies after any pending
            # all-displays change, exactly as it was requested.
            self._pending.pop(target, None)
            self._pending[target] = [level, 0]
        elif cur is None:
            self._pending[target] = [None, delta]
        elif cur[0] is not None:
            # A "+" right after a slider drag: still one absolute level,
            # just moved — a level always wins over a delta in apply().
            cur[0] = max(0, min(100, cur[0] + delta))
        else:
            cur[1] += delta

    async def _drain(self):
        while self._pending:
            batch, waiters = self._pending, self._waiters
            self._pending, self._waiters = {}, []
            try:
                async with self._lock():
                    state = await asyncio.to_thread(self._apply_batch, batch)
            except Exception as e:
                for w in waiters:
                    if not w.done():
                        w.set_exception(e)
            else:
                for w in waiters:
                    if not w.done():
                        w.set_result(state)

    def _apply_batch(self, batch):
        state = None
        for target, (level, delta) in batch.items():
            state = self._apply(level=level,
                                delta=None if level is not None else delta,
                                display_id=target)
        return state
