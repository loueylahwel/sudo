"""The PC's sound on the phone: whatever the PC plays comes out of the PHONE.

Not the same thing as phone_audio.py (Windows, only the apps on the phone
screen): this is ALL of the PC's sound, for listening on the phone in
another room or away from home. Away from home it has to fit through the
internet tunnel, which often carries only 2-3 Mbit/s on mobile data, so it
goes out as Opus at ~128 kbit/s in 20 ms packets (raw PCM would be 1.5
Mbit/s on its own).

Linux (PipeWire with pipewire-pulse):

  * `pw-cli` creates a null sink, "sudo_phone" ("Phone (Sudo)"), and is kept
    running: the sink belongs to that pw-cli (object.linger=false) and goes
    away the moment it exits. The child gets SIGTERM when the agent dies
    (PR_SET_PDEATHSIG), so a crashed agent can't leave a dead output behind;
  * mute_pc: the sink becomes the default output and the streams still on
    the old default are moved onto it, so every app plays into the sink and
    the PC's speakers go quiet. The old default is persisted (callback)
    BEFORE it changes: PipeWire remembers the picked default for good, so
    after a crash the next agent puts it back (restore_after_crash);
  * parec records the sink's monitor (without mute_pc: the default output's
    monitor, and the PC keeps playing too) as 16-bit stereo 48 kHz. A reader
    thread encodes 20 ms frames with libopus, through ctypes (no Python
    dependency, the library ships with every desktop), and hands each packet
    to on_packet.

Everything here blocks (subprocesses, waits of up to a few seconds): the
agent calls start() and stop() through asyncio.to_thread.

Windows: not yet (a WASAPI loopback of the default device would do it).
Nothing Windows can't import is imported at module level.
"""

import ctypes
import ctypes.util
import json
import platform
import re
import shutil
import subprocess
import threading
import time
from collections import deque

SINK = "sudo_phone"
RATE = 48000
CHANNELS = 2
FRAME_MS = 20
FRAME_SAMPLES = RATE * FRAME_MS // 1000      # 960 per channel
FRAME_BYTES = FRAME_SAMPLES * CHANNELS * 2   # 3840: s16le stereo
DEFAULT_BITRATE = 128000
MIN_BITRATE = 32000
MAX_BITRATE = 256000

SINK_WAIT_S = 3.0        # pw-cli normally has the sink up in ~20 ms
FIRST_FRAME_WAIT_S = 3.0  # parec delivers (silence) within ~50 ms

# Timing of the capture. The sink's graph runs at the smallest quantum any
# of its nodes asks for. Music players ask none (Spotify: 0; the graph
# default is 1024 = 21 ms) and parec's 20 ms brought it to 512 (10.7 ms):
# frames came out of parec on 10.7 ms cycles, up to a cycle late. Now the
# sink asks for 256 (5.3 ms cycles) itself and parec for 5 ms fragments
# (pipewire-pulse floors capture streams at 256 frames anyway), so each
# 20 ms frame is in within ~5 ms of its last sample. Measured 2026-10-16
# with a side agent, a beep played into the sink and a local client
# decoding the packets: their lateness against a steady 20 ms clock went
# from p95 9.5 to 4.3 ms (less for the phone's cushion to absorb) and,
# with the encoder change below, beep handed to the player -> packet
# received from median 43 to 38 ms (median of 5+ runs; the rest is the
# player's own buffer and the 20 ms frame itself, encoding and the hop to
# the socket take < 1 ms). Cost: parec 0.3 -> 1% of a core. Only our own
# sink is recorded this way: the default output's monitor (mute_pc off)
# keeps asking 20 ms, so the phone never pushes the PC's real sound card to
# small quanta.
SINK_QUANTUM = 256
SINK_PAREC_LATENCY_MS = 5
MONITOR_PAREC_LATENCY_MS = 20

_CREATE_SINK = (
    "create-node adapter { factory.name=support.null-audio-sink "
    f'node.name={SINK} node.description="Phone (Sudo)" '
    "media.class=Audio/Sink object.linger=false "
    f"node.latency={SINK_QUANTUM}/{RATE} "
    f"audio.position=[FL FR] audio.rate={RATE} }}\n")

# Debian/Ubuntu package of each piece, for the error the phone shows.
_PACKAGES = {"pw-cli": "pipewire-bin", "pactl": "pulseaudio-utils",
             "parec": "pulseaudio-utils", "libopus": "libopus0"}

# RESTRICTED_LOWDELAY, not AUDIO: AUDIO delays the sound by 6.5 ms (312
# samples of lookahead, 4 ms of it only there so the encoder can switch to
# SILK for speech at low bitrates), LOWDELAY by 2.5 ms (120). At the 128
# kbit/s stereo sent here AUDIO codes music and speech with CELT anyway, so
# nothing else changes; the phone's decoder is told 120 (OpusPlayer
# PRE_SKIP). Measured on the decoded sound: a beep comes out 6.52 -> 2.52 ms
# after it went in.
_OPUS_APPLICATION_RESTRICTED_LOWDELAY = 2051
_OPUS_SET_BITRATE_REQUEST = 4002


def clamp_bitrate(bitrate):
    return max(MIN_BITRATE, min(MAX_BITRATE, int(bitrate)))


# ---------------------------------------------------------------- Opus

_opus_lib = None


def _opus():
    """libopus through ctypes (loaded once), or None when it is missing."""
    global _opus_lib
    if _opus_lib is not None:
        return _opus_lib
    for name in ("libopus.so.0", ctypes.util.find_library("opus")):
        if not name:
            continue
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            continue
        lib.opus_encoder_create.restype = ctypes.c_void_p
        lib.opus_encoder_create.argtypes = [
            ctypes.c_int32, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_int)]
        lib.opus_encode.restype = ctypes.c_int32
        lib.opus_encode.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                    ctypes.c_int, ctypes.c_char_p,
                                    ctypes.c_int32]
        lib.opus_encoder_destroy.restype = None
        lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
        # opus_encoder_ctl is variadic: no argtypes, typed values instead.
        lib.opus_encoder_ctl.restype = ctypes.c_int
        _opus_lib = lib
        return lib
    return None


class _Encoder:
    """One Opus encoder, 48 kHz stereo, 20 ms frames in, one packet out.
    Used by the reader thread only (libopus encoders aren't thread-safe)."""

    MAX_PACKET = 4000  # libopus' recommended output buffer

    def __init__(self, bitrate):
        self._lib = _opus()
        err = ctypes.c_int(0)
        self._enc = self._lib.opus_encoder_create(
            RATE, CHANNELS, _OPUS_APPLICATION_RESTRICTED_LOWDELAY,
            ctypes.byref(err))
        if not self._enc or err.value != 0:
            raise RuntimeError(f"couldn't create the Opus encoder "
                               f"(error {err.value})")
        self._out = ctypes.create_string_buffer(self.MAX_PACKET)
        self.set_bitrate(bitrate)

    def set_bitrate(self, bitrate):
        self._lib.opus_encoder_ctl(ctypes.c_void_p(self._enc),
                                   ctypes.c_int(_OPUS_SET_BITRATE_REQUEST),
                                   ctypes.c_int32(int(bitrate)))

    def encode(self, pcm):
        n = self._lib.opus_encode(self._enc, pcm, FRAME_SAMPLES,
                                  self._out, self.MAX_PACKET)
        if n < 0:
            raise RuntimeError(f"Opus encode failed (error {n})")
        return ctypes.string_at(self._out, n)

    def close(self):
        if self._enc:
            self._lib.opus_encoder_destroy(self._enc)
            self._enc = None


# ---------------------------------------------------------------- pactl

def _run(*args, timeout=5):
    """One pactl / pw-metadata call: its stdout, or RuntimeError with what
    it printed."""
    r = subprocess.run(args, capture_output=True, text=True,
                       errors="replace", timeout=timeout)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout).strip()[:200]
        raise RuntimeError(f"{' '.join(args[:2])} failed: {detail}")
    return r.stdout


# ------------------------------------------------------------ MPRIS players

def pause_players():
    """Pause every media player (MPRIS, through playerctl) that is playing
    right now. Returns the ones it paused, to resume_players() them later.
    Used when the phone playing the PC's sound drops: the music must not
    carry on unheard, nor come back out of the PC's speakers by itself."""
    try:
        out = _run("playerctl", "-a", "-f",
                   "{{playerInstance}}\t{{status}}", "status")
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return []   # playerctl missing, or no player at all
    paused = []
    for line in out.splitlines():
        name, _, status = line.partition("\t")
        if name and status.strip() == "Playing":
            try:
                _run("playerctl", "-p", name, "pause")
                paused.append(name)
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                pass
    return paused


def resume_players(names):
    """Play again the players pause_players() paused (those still there and
    still paused: one the user started or stopped meanwhile is left alone)."""
    for name in names:
        try:
            if _run("playerctl", "-p", name, "status").strip() == "Paused":
                _run("playerctl", "-p", name, "play")
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            pass


def _sinks():
    """{sink name: pactl index} of every output."""
    sinks = {}
    for line in _run("pactl", "list", "short", "sinks").splitlines():
        f = line.split("\t")
        if len(f) >= 2 and f[0].strip().isdigit():
            sinks[f[1]] = f[0].strip()
    return sinks


def _sink_inputs():
    """[(stream index, index of the sink it plays on)]."""
    out = []
    for line in _run("pactl", "list", "short", "sink-inputs").splitlines():
        f = line.split("\t")
        if len(f) >= 2 and f[0].strip().isdigit():
            out.append((f[0].strip(), f[1].strip()))
    return out


def _default_sink():
    return _run("pactl", "get-default-sink").strip()


def _configured_default():
    """The default output as picked (by the user, or by us), which
    WirePlumber keeps across restarts even while that output doesn't exist
    (pactl only shows the effective one, a fallback in that case). None
    when unknown."""
    try:
        out = _run("pw-metadata", "-n", "default", "0",
                   "default.configured.audio.sink")
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return None
    m = re.search(r"value:'(.*)' type:", out)
    if not m:
        return None
    try:
        return json.loads(m.group(1)).get("name")
    except (ValueError, AttributeError):
        return None


def _move_streams(src, dst):
    """Move every stream playing on output `src` to output `dst` (names).
    Moving a stream onto the CURRENT default makes pipewire-pulse drop its
    pinned target (target.object -1): it follows the default from then on,
    so call this after switching the default."""
    sinks = _sinks()
    index = sinks.get(src)
    if index is None or dst not in sinks:
        return
    for stream, sink in _sink_inputs():
        if sink == index:
            try:
                _run("pactl", "move-sink-input", stream, dst)
            except (RuntimeError, subprocess.SubprocessError):
                pass  # the stream ended meanwhile


def restore_default(prev):
    """Make `prev` the default output again or, when it is gone (headphones
    unplugged since), the current non-phone default or any other output, so
    the default never stays on the phone's sink. Returns the output that is
    now the default, or None if there is none."""
    sinks = _sinks()
    target = prev if prev and prev != SINK and prev in sinks else None
    if target is None:
        current = _default_sink()
        target = current if current != SINK and current in sinks \
            else next((name for name in sinks if name != SINK), None)
    if target:
        _run("pactl", "set-default-sink", target)
    return target


def restore_after_crash(prev):
    """Agent startup after a run that died while the PC's sound was on the
    phone: its sink went with its pw-cli, but PipeWire keeps the phone's
    sink as the picked default (the PC then plays on whatever it falls back
    to). Put `prev` back, unless the user picked another output since, or
    the sink exists again (another running agent owns it). Returns what was
    done, for the log; raises when pactl can't be reached (retry later)."""
    if SINK in _sinks():
        return "left the default output alone: the phone's sink is in use"
    configured = _configured_default()
    if configured is not None and configured != SINK:
        return f"the default output is already {configured}"
    return f"default output back to {restore_default(prev)}"


# ---------------------------------------------------------------- capture

def _die_with_agent():
    """Linux, in the child before exec: SIGTERM when the agent dies, even
    when it is killed outright (PR_SET_PDEATHSIG). The 'parent' is the
    thread that started the child, so each child is started by a thread
    that lives exactly as long as the child does."""
    try:
        import signal
        ctypes.CDLL(None).prctl(1, signal.SIGTERM)
    except Exception:
        pass


def _end(proc, timeout=2.0):
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(1)
        except subprocess.TimeoutExpired:
            pass
    except OSError:
        pass


class PcAudio:
    """The PC's whole sound to one phone, as Opus.

    on_packet(seq, opus_bytes): called on the reader thread for every 20 ms
    frame; seq goes up by one per frame. persist(name | None): the default
    output from before the switch, called BEFORE it is switched and with
    None once it is back. on_ended(reason): the capture died on its own (the
    sound server restarted...); routing is already restored by then. The
    callbacks must not block, nor call start()/stop()."""

    def __init__(self, on_packet, persist=None, on_ended=None):
        self.on_packet = on_packet
        self.persist = persist
        self.on_ended = on_ended
        self.mute_pc = True
        self.bitrate = DEFAULT_BITRATE
        self._lock = threading.Lock()    # start/stop one at a time
        self._running = False
        self._session = 0                # one per start, for on_ended
        self._seq = 0
        self._pwcli = None
        self._parec = None
        self._reader = None
        self._encoder = None
        self._prev = None                # default output before the switch
        self._switched = False           # the default may point at SINK
        self._stopping = threading.Event()
        self._log = deque(maxlen=12)     # pw-cli / parec output, for errors

    @property
    def running(self):
        return self._running

    def start(self, mute_pc=True, bitrate=DEFAULT_BITRATE):
        """Start, or keep going with a new bitrate. A different mute_pc
        restarts (the routing differs). Raises with a message for the phone
        when this PC can't do it."""
        mute_pc = bool(mute_pc)
        bitrate = clamp_bitrate(bitrate)
        with self._lock:
            alive = self._reader is not None and self._reader.is_alive()
            if self._running and alive and mute_pc == self.mute_pc:
                self.bitrate = bitrate   # the reader applies it
                return
            self._stop_locked()
            self._check(mute_pc)
            try:
                self._start_locked(mute_pc, bitrate)
            except BaseException:
                self._stop_locked()
                raise

    def stop(self):
        """PC sound back on the PC. Safe to call any number of times."""
        with self._lock:
            self._stop_locked()

    # ---- internals

    @staticmethod
    def _check(mute_pc):
        system = platform.system()
        if system == "Windows":
            raise RuntimeError("Playing the PC's sound on the phone isn't "
                               "available on Windows yet")
        if system != "Linux":
            raise RuntimeError("Playing the PC's sound on the phone isn't "
                               f"available on {system} yet")
        need = ["pw-cli", "pactl", "parec"] if mute_pc else ["parec"]
        missing = [tool for tool in need if shutil.which(tool) is None]
        if _opus() is None:
            missing.append("libopus")
        if missing:
            packages = list(dict.fromkeys(_PACKAGES[m] for m in missing))
            raise RuntimeError(
                "Playing the PC's sound on the phone needs "
                f"{' and '.join(missing)} on the PC: install "
                f"{'them' if len(missing) > 1 else 'it'} with "
                f"`sudo apt install {' '.join(packages)}`")

    def _start_locked(self, mute_pc, bitrate):
        self._running = True   # from here on _stop_locked cleans up
        self._session += 1
        self._stopping.clear()
        self._log.clear()
        self.mute_pc = mute_pc
        self.bitrate = bitrate
        if mute_pc:
            self._wait_sink(False, 1.0)  # ours, still going away?
            if SINK in _sinks():
                raise RuntimeError("this PC's sound is already going to a "
                                   "phone (another Sudo agent is running)")
            # Read before the sink exists: PipeWire may make a new sink
            # the default by itself if it was the picked one once.
            prev = _default_sink()
            self._prev = prev if prev and prev != SINK else None
            self._spawn_sink()
            if self.persist and self._prev:
                self.persist(self._prev)   # before the default changes
            self._switched = True
            _run("pactl", "set-default-sink", SINK)
            # Streams that follow the default move by themselves; the ones
            # pinned to the old output need a push.
            if self._prev:
                _move_streams(self._prev, SINK)
            source = SINK + ".monitor"
        else:
            source = "@DEFAULT_MONITOR@"
        self._encoder = _Encoder(bitrate)
        self._spawn_capture(source)
        print(f"[agent] PC sound to the phone: on ({bitrate // 1000} kbit/s"
              f"{', PC muted' if mute_pc else ''})")

    def _stop_locked(self):
        if not self._running:
            return
        self._stopping.set()
        # 1. the capture (and, once the reader is out, its encoder)
        _end(self._parec)
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.join(3)
        if self._encoder is not None and (reader is None
                                          or not reader.is_alive()):
            self._encoder.close()   # a stuck reader keeps it (no crash)
        self._encoder = None
        self._parec = None
        # 2. the PC's own output back, while the sink still exists so the
        # streams on it can be moved by name
        restored = not self._switched
        if self._switched:
            try:
                target = restore_default(self._prev)
                if target:
                    _move_streams(SINK, target)
                restored = True
            except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                # Kept persisted: the next agent start tries again.
                print(f"[agent] PC sound: couldn't restore the default "
                      f"output: {e}")
        # 3. the sink (PipeWire re-routes anything left on it)
        pwcli, self._pwcli = self._pwcli, None
        if pwcli is not None:
            try:
                pwcli.stdin.close()
            except (OSError, ValueError):
                pass
            _end(pwcli)
            self._wait_sink(False, 1.0)
        if restored and self._switched and self.persist:
            self.persist(None)
        self._switched = False
        self._prev = None
        self._running = False
        print("[agent] PC sound to the phone: off")

    def _wait_sink(self, present, timeout):
        """Until the phone's sink is (or isn't) listed; False on timeout or
        when pw-cli died while we wait for it to appear."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                if (SINK in _sinks()) == present:
                    return True
            except (OSError, RuntimeError, subprocess.SubprocessError):
                pass
            if present and self._pwcli is not None \
                    and self._pwcli.poll() is not None:
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.03)

    def _spawn_sink(self):
        """pw-cli, started (and its output drained) by a thread that lives
        as long as it does: see _die_with_agent. The pipe stays open: pw-cli
        doesn't exit on EOF anyway, it is killed in stop()."""
        started = threading.Event()
        failure = []

        def hold():
            try:
                proc = subprocess.Popen(
                    ["pw-cli"], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, errors="replace", preexec_fn=_die_with_agent)
            except OSError as e:
                failure.append(e)
                started.set()
                return
            self._pwcli = proc
            started.set()
            for line in proc.stdout:   # drained, or pw-cli could block
                if "rror" in line:     # not its routine "node changed"
                    self._log.append("pw-cli: " + line.strip())

        threading.Thread(target=hold, name="sudo-pc-audio-sink",
                         daemon=True).start()
        started.wait(5)
        if failure or self._pwcli is None:
            raise RuntimeError(f"couldn't start pw-cli: "
                               f"{failure[0] if failure else 'timed out'}")
        try:
            self._pwcli.stdin.write(_CREATE_SINK)
            self._pwcli.stdin.flush()
        except (OSError, ValueError) as e:
            raise RuntimeError(f"pw-cli: {e}")
        if not self._wait_sink(True, SINK_WAIT_S):
            raise RuntimeError("the phone's sound device didn't appear on "
                               "the PC (is PipeWire running?)"
                               + self._last_log())

    def _spawn_capture(self, source):
        """parec on its own reader thread (which also starts it, see
        _die_with_agent); returns once the first frame is in."""
        ready = threading.Event()
        failure = []
        session = self._session
        reader = threading.Thread(
            target=self._read, args=(source, ready, failure, session),
            name="sudo-pc-audio", daemon=True)
        self._reader = reader
        reader.start()
        if not ready.wait(FIRST_FRAME_WAIT_S) or failure:
            raise RuntimeError("couldn't record the PC's sound: "
                               + (str(failure[0]) if failure
                                  else "no sound data") + self._last_log())

    def _read(self, source, ready, failure, session):
        latency = SINK_PAREC_LATENCY_MS if source == SINK + ".monitor" \
            else MONITOR_PAREC_LATENCY_MS   # see SINK_QUANTUM
        cmd = ["parec", "-d", source, "--format=s16le", f"--rate={RATE}",
               f"--channels={CHANNELS}", "--raw", f"--latency-msec={latency}",
               "--client-name=Sudo", "--stream-name=PC sound for the phone"]
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, preexec_fn=_die_with_agent)
        except OSError as e:
            failure.append(e)
            ready.set()
            return
        self._parec = proc

        def drain():
            for line in proc.stderr:
                text = line.decode(errors="replace").strip()
                if text:
                    self._log.append("parec: " + text)

        drainer = threading.Thread(target=drain, daemon=True)
        drainer.start()
        encoder = self._encoder
        bitrate = self.bitrate
        got_one = False
        try:
            while not self._stopping.is_set():
                pcm = proc.stdout.read(FRAME_BYTES)
                if len(pcm) < FRAME_BYTES:
                    break   # parec ended
                if not got_one:
                    got_one = True
                    ready.set()
                if self.bitrate != bitrate:
                    bitrate = self.bitrate
                    encoder.set_bitrate(bitrate)
                try:
                    packet = encoder.encode(pcm)
                except RuntimeError as e:
                    self._log.append(str(e))
                    continue
                seq = self._seq
                self._seq += 1
                try:
                    self.on_packet(seq, packet)
                except Exception as e:
                    self._log.append(f"on_packet: {e}")
        finally:
            if not got_one:
                # Let its error message reach the log first.
                try:
                    proc.wait(1)
                except subprocess.TimeoutExpired:
                    pass
                drainer.join(0.5)
                failure.append(RuntimeError("parec stopped"))
                ready.set()
        if got_one and not self._stopping.is_set():
            # Nobody asked: put the PC's output back from a fresh thread
            # (stop() joins this one), then tell the agent.
            reason = "the PC's sound capture stopped"
            print(f"[agent] PC sound: {reason}{self._last_log()}")
            threading.Thread(target=self._ended, args=(session, reason),
                             daemon=True).start()

    def _ended(self, session, reason):
        with self._lock:
            if session != self._session or not self._running:
                return   # already stopped or restarted meanwhile
            self._stop_locked()
        if self.on_ended:
            try:
                self.on_ended(reason)
            except Exception:
                pass

    def _last_log(self):
        return f" ({self._log[-1]})" if self._log else ""
