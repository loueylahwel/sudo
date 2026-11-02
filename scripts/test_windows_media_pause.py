"""PC sound on the phone, Windows: when the phone drops, the PC's players
pause (pc_audio.pause_players) and play again when it comes back
(resume_players), through Windows' media sessions (agent/media_info.ps1).

Edge plays a looping tone from a page: like a YouTube tab or Spotify, it
shows up as a media session. Needs a sound card (CI: LABSN/sound-ci-helpers).

Usage: python -I scripts/test_windows_media_pause.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))
import pc_audio  # noqa: E402

EDGE = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) \
    / "Microsoft" / "Edge" / "Application" / "msedge.exe"

passed, failed = 0, 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL  {name} {extra}")


def now_playing():
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(ROOT / "agent" / "media_info.ps1")],
        capture_output=True, text=True, timeout=30)
    return json.loads(proc.stdout.strip() or "{}")


def wait_playing(want, timeout=30):
    end = time.monotonic() + timeout
    info = {}
    while time.monotonic() < end:
        info = now_playing()
        if bool(info.get("playing")) == want:
            return info
        time.sleep(1)
    return info


def main():
    tmp = Path(tempfile.mkdtemp(prefix="sudo-media-"))
    t = np.arange(48000 * 30) / 48000
    tone = (0.3 * np.sin(2 * np.pi * 440 * t) * 32767).astype("<i2")
    with wave.open(str(tmp / "tone.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(tone.tobytes())
    (tmp / "player.html").write_text(
        '<title>Sudo test</title><audio src="tone.wav" autoplay loop>'
        '</audio><script>navigator.mediaSession.metadata = new '
        'MediaMetadata({title: "Sudo test tone"});</script>')
    edge = subprocess.Popen([
        str(EDGE), "--no-first-run", "--no-default-browser-check",
        "--autoplay-policy=no-user-gesture-required",
        f"--user-data-dir={tmp / 'profile'}", (tmp / "player.html").as_uri()])
    try:
        info = wait_playing(True)
        check("Edge's tone is a playing media session",
              info.get("playing") is True, f"({info})")
        paused = pc_audio.pause_players()
        check("pause_players paused it", bool(paused), f"({paused})")
        info = wait_playing(False, 10)
        check("nothing plays after the pause", info.get("playing") is False,
              f"({info})")
        pc_audio.resume_players(paused)
        info = wait_playing(True, 10)
        check("resume_players played it again", info.get("playing") is True,
              f"({info})")
        pc_audio.pause_players()
        wait_playing(False, 10)
        again = pc_audio.pause_players()
        check("nothing playing: nothing to pause", again == [], f"({again})")
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(edge.pid)],
                       capture_output=True)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
