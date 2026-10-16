"""USB link to the phone: `adb reverse` of the agent's TLS port.

With the phone plugged in over USB (USB debugging on), `127.0.0.1:<tls
port>` on the phone reaches the agent through the cable: no Wi-Fi needed,
lower latency, and pulling the cable closes the socket at once, so the
agent notices a lost phone in a second instead of after a timeout (PC sound
on the phone pauses the PC's players right away, see agent._pc_audio_left).

The app tries that address as one more route when it connects (home_page
`_Route.usb`), with the PC's certificate pinned as on the internet routes.

Every few seconds the agent lists the attached devices (`adb devices`) and
(re)adds the reverse on each one that lacks it: a phone plugged in later,
or one whose adb restarted, gets it within POLL_S. Nothing at all happens
when adb isn't installed.
"""

import asyncio
import shutil
import subprocess

POLL_S = 3


def _adb(*args, timeout=5):
    r = subprocess.run(["adb", *args], capture_output=True, text=True,
                       errors="replace", timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:200])
    return r.stdout


def devices():
    """Serials of the attached devices that are ready (not unauthorized or
    offline)."""
    out = _adb("devices")
    serials = []
    for line in out.splitlines()[1:]:
        serial, _, state = line.partition("\t")
        if serial and state.strip() == "device":
            serials.append(serial)
    return serials


def ensure_reverse(serial, port):
    """Adds `tcp:port -> tcp:port` on the device unless it is there.
    Returns True when it had to be added."""
    rule = f"tcp:{port} tcp:{port}"
    if rule in _adb("-s", serial, "reverse", "--list"):
        return False
    _adb("-s", serial, "reverse", f"tcp:{port}", f"tcp:{port}")
    return True


async def keep_reversed(port):
    """Runs for the agent's lifetime: the reverse on every attached phone."""
    if not shutil.which("adb"):
        return
    attached = set()
    while True:
        try:
            now = set(await asyncio.to_thread(devices))
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            now = set()
        for serial in sorted(now):
            try:
                if await asyncio.to_thread(ensure_reverse, serial, port):
                    print(f"[agent] USB: phone {serial} can reach this PC "
                          f"through the cable (port {port})")
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:
                if serial not in attached:
                    print(f"[agent] USB: couldn't set up {serial}: {e}")
        for serial in sorted(attached - now):
            print(f"[agent] USB: phone {serial} unplugged")
        attached = now
        await asyncio.sleep(POLL_S)
