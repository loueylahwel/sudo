"""PC sound on the phone, Windows (agent/pc_audio.py): what the PC plays
reaches the phone while the PC's outputs are muted, and they are unmuted
after, by stop() and by the crash restore.

Needs a sound card (CI: a virtual one, LABSN/sound-ci-helpers) and opus.dll
where pc_audio looks for it (CI: tools/opus.dll). A child process plays a
1 kHz tone (the capture leaves out the agent's own process, here this
one); the packets are decoded with libopus and must carry that tone.

Usage: python -I scripts/test_windows_pc_audio.py
"""

import ctypes
import json
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

TONE_HZ = 1000
PLAYER = ("import sys, time, winsound; winsound.PlaySound(sys.argv[1], "
          "winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP); "
          "time.sleep(120)")

passed, failed = 0, 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL  {name} {extra}")


def write_tone(path, seconds=30):
    t = np.arange(48000 * seconds) / 48000
    tone = (0.5 * np.sin(2 * np.pi * TONE_HZ * t) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(np.repeat(tone, 2).tobytes())   # L R interleaved


def decode(packets):
    lib = pc_audio._opus()
    lib.opus_decoder_create.restype = ctypes.c_void_p
    lib.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int,
                                        ctypes.POINTER(ctypes.c_int)]
    lib.opus_decode.restype = ctypes.c_int
    lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                ctypes.c_int32,
                                ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
                                ctypes.c_int]
    err = ctypes.c_int(0)
    dec = lib.opus_decoder_create(48000, 2, ctypes.byref(err))
    out = (ctypes.c_int16 * (pc_audio.FRAME_SAMPLES * 2))()
    pcm = []
    for packet in packets:
        n = lib.opus_decode(dec, packet, len(packet), out,
                            pc_audio.FRAME_SAMPLES, 0)
        if n > 0:
            pcm.append(np.frombuffer(bytes(out), np.int16)[:n * 2].copy())
    return np.concatenate(pcm) if pcm else np.zeros(0, np.int16)


def level_and_pitch(pcm):
    left = pcm[0::2].astype(np.float64)
    if len(left) < 4800:
        return 0.0, 0.0
    rms = float(np.sqrt(np.mean(left ** 2)))
    spectrum = np.abs(np.fft.rfft(left * np.hanning(len(left))))
    return rms, float(np.argmax(spectrum[1:]) + 1) * 48000 / len(left)


def output_peak():
    """The loudest output's peak meter right now: is the tone playing?"""
    def run():
        import comtypes
        from pycaw.constants import CLSID_MMDeviceEnumerator
        from pycaw.pycaw import IAudioMeterInformation, IMMDeviceEnumerator
        enum = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator,
                                         IMMDeviceEnumerator,
                                         comtypes.CLSCTX_INPROC_SERVER)
        devices = enum.EnumAudioEndpoints(0, 1)
        peak = 0.0
        for i in range(devices.GetCount()):
            meter = devices.Item(i).Activate(
                IAudioMeterInformation._iid_, comtypes.CLSCTX_ALL,
                None).QueryInterface(IAudioMeterInformation)
            peak = max(peak, meter.GetPeakValue())
        return peak
    return pc_audio._with_com(run)


def main():
    before = set(pc_audio._win_unmuted_outputs())
    check("an unmuted output", bool(before), f"({len(before)})")
    packets = []
    persisted = []
    audio = pc_audio.PcAudio(
        on_packet=lambda seq, data: packets.append(data),
        persist=persisted.append)
    wav = Path(tempfile.gettempdir()) / "sudo-tone.wav"
    write_tone(wav)
    # Not muted first: if the tone doesn't come through even then, the
    # capture (or the player) is at fault, not the mute.
    for mute in (False, True):
        label = "PC muted" if mute else "PC not muted"
        packets.clear()
        audio.start(mute_pc=mute)
        try:
            time.sleep(1.0)
            silent = len(packets)
            check(f"frames while nothing plays ({label})",
                  40 <= silent <= 60, f"({silent} in 1 s)")
            if mute:
                still = set(pc_audio._win_unmuted_outputs()) & before
                check("the PC's outputs are muted", not still,
                      f"({len(still)} still unmuted)")
            player = subprocess.Popen(
                [sys.executable, "-I", "-c", PLAYER, str(wav)],
                stderr=subprocess.PIPE, text=True)
            try:
                time.sleep(1.5)   # the player starts
                peak = output_peak()
                start = len(packets)
                time.sleep(3.0)
                tone = packets[start:]
                playing = player.poll() is None
            finally:
                player.kill()
                err = player.communicate()[1].strip()
            check(f"frames while the tone plays ({label})",
                  135 <= len(tone) <= 165, f"({len(tone)} in 3 s)")
            rms, pitch = level_and_pitch(decode(tone))
            check(f"the tone reaches the phone ({label})", rms > 2000,
                  f"(RMS {rms:.0f} of 32767; output meter {peak:.2f}; "
                  f"player {'playing' if playing else 'exited: ' + err})")
            check(f"at its pitch ({label})", abs(pitch - TONE_HZ) < 5,
                  f"({pitch:.1f} Hz)")
        finally:
            audio.stop()
    unmuted = set(pc_audio._win_unmuted_outputs())
    check("unmuted after stop", before <= unmuted,
          f"({len(before - unmuted)} left muted)")
    check("persisted then cleared",
          len(persisted) == 2 and persisted[0].startswith(pc_audio._WIN_MUTED)
          and persisted[1] is None, f"({persisted})")

    # An agent that died with the PC muted: the next one unmutes.
    ids = sorted(before)
    pc_audio._win_set_mute(ids, True)
    done = pc_audio.restore_after_crash(pc_audio._WIN_MUTED + json.dumps(ids))
    unmuted = set(pc_audio._win_unmuted_outputs())
    check("crash restore unmutes", before <= unmuted, f"({done})")

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
