package io.github.loueylahwel.sudo

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTimestamp
import android.media.AudioTrack
import android.media.MediaCodec
import android.media.MediaFormat
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.Process
import android.os.SystemClock
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * Plays "PC sound on this phone": everything the PC plays, streamed by the
 * agent as OPS1 packets (one 20 ms Opus packet each, 48 kHz stereo).
 *
 * Packets arrive at the network's pace, not the speaker's: through the
 * tunnel on mobile data they come in clumps, with holes. So there is a
 * jitter buffer in front of the speaker, measured as what the AudioTrack
 * holds (frames written minus its playback head):
 *  - the track only starts once `target` ms are buffered, and when it runs
 *    dry it pauses and rebuffers to `target` instead of stuttering along on
 *    whatever trickles in;
 *  - `target` adapts to the link. It starts at 80 ms: on a home Wi-Fi the
 *    packets leave the PC within ~5 ms of their schedule and the Wi-Fi adds
 *    a few tens, while the old 150 (and the 230 two rebuffers made of it)
 *    was most of the ~400 ms the sound lagged. A rebuffer raises it by
 *    40 ms, and at least to the longest gap between two packets of the
 *    last ~10 s + 20 ms, so a clumpy link (the tunnel on mobile data) gets
 *    what it needs in one step instead of a rebuffer per 40 ms (max 400).
 *    It comes down 20 ms every 10 s (min 50) once the cushion stayed above
 *    40 ms for three minutes without a rebuffer, and as long as it keeps
 *    doing so: a link that nearly ran dry in the last three minutes keeps
 *    its target (simulated: with a one-minute memory the target decays
 *    between two Wi-Fi hiccups and rebuffers on the next one, twice as
 *    often as the old rules did);
 *  - latency stays bounded: above target + 300 ms (the burst after a stall)
 *    the decoded sound is dropped until it is back near target, and a
 *    cushion that stayed more than 40 ms above target for 3 s straight is
 *    trimmed the same way. One skip beats lagging behind the PC's picture
 *    for the rest of the session.
 * Every packet is decoded, even when its sound is then dropped: Opus
 * carries state from packet to packet, and skipping input would garble
 * the sound after the gap.
 *
 * The track asks for Android's low-latency output (API 26+). A plain music
 * track goes to the power-saving "deep buffer" output on many phones,
 * which holds tens to over a hundred ms more below the playback head, out
 * of reach of every rule above. A phone that doesn't grant it plays the
 * track as a normal one ([stats] says which: perf_mode, and out_ms, the
 * whole delay from write to speaker).
 *
 * Audio focus: it asks for the focus like any music app. Another app taking
 * it for good (the user starts music on the phone) ends the session through
 * [onStopped]; a transient loss (a notification sound, a call) mutes until
 * the focus comes back.
 *
 * Threading: [write] only queues. One private thread per session owns the
 * MediaCodec decoder and the AudioTrack. [start] and [stop] run on the main
 * thread (the platform channel and the focus callbacks both live there).
 */
class OpusPlayer(context: Context) {
    private val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private val appContext = context.applicationContext
    // Headphones unplugged or Bluetooth earbuds gone: Android would move the
    // sound to the loudspeaker, so stop, as media apps do.
    private var noisyRegistered = false
    private val noisyReceiver = object : BroadcastReceiver() {
        override fun onReceive(c: Context, intent: Intent) {
            if (intent.action == AudioManager.ACTION_AUDIO_BECOMING_NOISY) {
                endBySelf(session, "PC sound stopped: the headphones were disconnected")
            }
        }
    }
    private val mainHandler = Handler(Looper.getMainLooper())

    /** Called on the main thread when playback ended by itself (another app
     * took the audio focus for good, the decoder kept failing), with a
     * sentence for the user. Not called for [stop]. */
    var onStopped: ((String) -> Unit)? = null

    private val packets = LinkedBlockingQueue<ByteArray>(MAX_QUEUED_PACKETS)
    @Volatile private var running = false
    // Each start gets a new id; a session's thread runs while it is current,
    // so a thread that outlived a quick stop+start never touches the next
    // session.
    @Volatile private var session = 0
    private var worker: Thread? = null

    @Volatile private var muted = false
    @Volatile private var ducked = false

    // Diagnostics for [stats] (Dart sends them to the PC, whose log shows
    // them: the phone's own log isn't reachable over a hotspot).
    @Volatile private var statIn = 0L
    @Volatile private var statDecoded = 0L
    @Volatile private var statWrittenMs = 0L
    @Volatile private var statPlaying = false
    @Volatile private var statFlowing = false
    @Volatile private var statStalls = 0
    @Volatile private var statCushionMs = 0L
    @Volatile private var statTargetMs = 0L
    @Volatile private var statUnderruns = 0
    @Volatile private var statCodecErrors = 0
    @Volatile private var statLastError: String? = null
    // For tuning the targets from the PC's log: the delay from write to the
    // speaker (AudioTimestamp; cushion + the mixer and driver below it), the
    // performance mode Android granted (1 = low latency, 0 = none, -1 =
    // can't tell before API 26), the mixer's own underrun count, the lowest
    // cushion of the last 10 s period (0 after an underrun), and the
    // trims/drops.
    @Volatile private var statOutMs = -1L
    @Volatile private var statPerfMode = -1
    @Volatile private var statTrackUnderruns = 0
    @Volatile private var statFloorMs = -1L
    @Volatile private var statTrims = 0
    // Packet arrivals ([write], main thread): the longest gap between two
    // in the current and the previous GAP_WINDOW_MS window. Read by the
    // player thread (the target after a rebuffer) and [stats].
    @Volatile private var lastPacketAt = 0L
    @Volatile private var gapWindowAt = 0L
    @Volatile private var gapMaxNow = 0L
    @Volatile private var gapMaxBefore = 0L
    private var focusRequest: AudioFocusRequest? = null

    private val attributes = AudioAttributes.Builder()
        .setUsage(AudioAttributes.USAGE_MEDIA)
        .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
        .build()

    private val focusListener = AudioManager.OnAudioFocusChangeListener { change ->
        when (change) {
            AudioManager.AUDIOFOCUS_LOSS ->
                endBySelf(session, "PC sound stopped: another app on the phone started playing")
            AudioManager.AUDIOFOCUS_LOSS_TRANSIENT -> muted = true
            // From Android 8 on the system ducks the track by itself.
            AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK ->
                if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) ducked = true
            AudioManager.AUDIOFOCUS_GAIN -> {
                muted = false
                ducked = false
            }
        }
    }

    val isRunning: Boolean get() = running

    /**
     * Opens the decoder and the audio output, takes the audio focus and
     * starts the player thread. A no-op while already running (Dart asks
     * again after every reconnect). Throws with a message for the user when
     * the phone can't play: no Opus decoder, or the focus refused (a call).
     */
    fun start() {
        if (running) return
        // First: newTrack notes what it couldn't set up in statLastError.
        resetStats()
        val track = newTrack()
        val codec = try {
            newDecoder()
        } catch (e: Exception) {
            track.release()
            throw IllegalStateException("This phone can't decode the PC sound (no Opus decoder)", e)
        }
        if (!requestFocus()) {
            abandonFocus()
            codec.release()
            track.release()
            throw IllegalStateException("The phone won't play sound right now (is a call on?)")
        }
        muted = false
        ducked = false
        packets.clear()
        val id = session + 1
        session = id
        running = true
        worker = Thread(Session(id, codec, track), "sudo-pc-sound").apply { start() }
        val noisy = IntentFilter(AudioManager.ACTION_AUDIO_BECOMING_NOISY)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            appContext.registerReceiver(noisyReceiver, noisy, Context.RECEIVER_NOT_EXPORTED)
        } else {
            appContext.registerReceiver(noisyReceiver, noisy)
        }
        noisyRegistered = true
    }

    /** Queues one Opus packet; returns at once. */
    fun write(packet: ByteArray) {
        if (!running) return
        statIn++
        noteArrival(SystemClock.elapsedRealtime())
        // A stalled player thread must not hoard memory: keep the newest.
        while (!packets.offer(packet)) packets.poll()
    }

    /** [write]'s thread only: keeps the longest recent gap between packets. */
    private fun noteArrival(now: Long) {
        val last = lastPacketAt
        lastPacketAt = now
        if (now - gapWindowAt >= GAP_WINDOW_MS) {
            gapMaxBefore = gapMaxNow
            gapMaxNow = 0
            gapWindowAt = now
        }
        if (last != 0L && now - last > gapMaxNow) gapMaxNow = now - last
    }

    /** The longest gap between two packets in the last 5-10 s. */
    private fun recentGapMs(): Long = maxOf(gapMaxNow, gapMaxBefore)

    /** What the player is doing, for the PC's log (see the stat fields). */
    fun stats(): Map<String, Any?> = mapOf(
        "running" to running,
        "in" to statIn,
        "decoded" to statDecoded,
        "written_ms" to statWrittenMs,
        "playing" to statPlaying,
        "flowing" to statFlowing,
        "stalls" to statStalls,
        "cushion_ms" to statCushionMs,
        "target_ms" to statTargetMs,
        "floor_ms" to statFloorMs,
        "out_ms" to statOutMs,
        "gap_max_ms" to recentGapMs(),
        "underruns" to statUnderruns,
        "track_underruns" to statTrackUnderruns,
        "trims" to statTrims,
        "perf_mode" to statPerfMode,
        "keepawake" to WifiKeepAwake.active,
        "keepawake_sent" to WifiKeepAwake.sent,
        "codec_errors" to statCodecErrors,
        "last_error" to statLastError,
        "muted" to muted,
        "ducked" to ducked,
        "volume" to audioManager.getStreamVolume(AudioManager.STREAM_MUSIC),
        "volume_max" to audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC),
    )

    private fun resetStats() {
        statIn = 0
        statDecoded = 0
        statWrittenMs = 0
        statPlaying = false
        statFlowing = false
        statStalls = 0
        statCushionMs = 0
        statTargetMs = 0
        statUnderruns = 0
        statCodecErrors = 0
        statLastError = null
        statOutMs = -1
        statPerfMode = -1
        statTrackUnderruns = 0
        statFloorMs = -1
        statTrims = 0
        lastPacketAt = 0
        gapWindowAt = 0
        gapMaxNow = 0
        gapMaxBefore = 0
    }

    /** Stops playing and gives the audio focus back. Idempotent. */
    fun stop() {
        if (!running) return
        running = false
        session += 1
        val thread = worker
        worker = null
        packets.clear()
        abandonFocus()
        if (noisyRegistered) {
            noisyRegistered = false
            try {
                appContext.unregisterReceiver(noisyReceiver)
            } catch (_: IllegalArgumentException) {
                // Never registered after all.
            }
        }
        // The thread releases the decoder and the track on its way out; it
        // notices within one loop turn (~10 ms).
        thread?.join(STOP_WAIT_MS)
    }

    /** Session [id] ended without Dart asking: stop, then tell Dart why. */
    private fun endBySelf(id: Int, reason: String) {
        if (!running || session != id) return
        stop()
        onStopped?.invoke(reason)
    }

    private fun requestFocus(): Boolean {
        val result = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val request = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN)
                .setAudioAttributes(attributes)
                .setOnAudioFocusChangeListener(focusListener, mainHandler)
                .build()
            focusRequest = request
            audioManager.requestAudioFocus(request)
        } else {
            @Suppress("DEPRECATION")
            audioManager.requestAudioFocus(
                focusListener, AudioManager.STREAM_MUSIC, AudioManager.AUDIOFOCUS_GAIN,
            )
        }
        return result == AudioManager.AUDIOFOCUS_REQUEST_GRANTED
    }

    private fun abandonFocus() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            focusRequest?.let { audioManager.abandonAudioFocusRequest(it) }
            focusRequest = null
        } else {
            @Suppress("DEPRECATION")
            audioManager.abandonAudioFocus(focusListener)
        }
    }

    /** The low-latency track where Android has one (see the class comment),
     * else a normal one. */
    private fun newTrack(): AudioTrack {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            try {
                return buildTrack(lowLatency = true)
            } catch (e: Exception) {
                statLastError = "low-latency track: ${e.message}"
            }
        }
        return buildTrack(lowLatency = false)
    }

    private fun buildTrack(lowLatency: Boolean): AudioTrack {
        val format = AudioFormat.Builder()
            .setSampleRate(SAMPLE_RATE)
            .setChannelMask(AudioFormat.CHANNEL_OUT_STEREO)
            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
            .build()
        val min = AudioTrack.getMinBufferSize(
            SAMPLE_RATE, AudioFormat.CHANNEL_OUT_STEREO, AudioFormat.ENCODING_PCM_16BIT,
        )
        // ~1 s: room for the largest cushion (400 ms target + 300 ms burst)
        // so a write never has to wait for the speaker. A low-latency track
        // may be that big: the latency is what it holds, not its capacity.
        val builder = AudioTrack.Builder()
            .setAudioAttributes(attributes)
            .setAudioFormat(format)
            .setBufferSizeInBytes(maxOf(min, SAMPLE_RATE * BYTES_PER_FRAME))
            .setTransferMode(AudioTrack.MODE_STREAM)
        if (lowLatency && Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            builder.setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY)
        }
        val track = builder.build()
        if (track.state != AudioTrack.STATE_INITIALIZED) {
            track.release()
            throw IllegalStateException("The phone's audio output didn't open")
        }
        // What Android granted: it may hand out a normal track instead.
        statPerfMode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            track.performanceMode
        } else {
            -1
        }
        // By default the mixer starts a track only once its WHOLE buffer is
        // full (1 s here), and disables it after ~1 s of waiting. The
        // cushion rules below never fill it that far, so the track stayed
        // silent for good while everything else looked fine. This player
        // decides itself when there is enough (play() at `target`): let the
        // mixer go as soon as there is anything. Older Android: see
        // Session.flowing.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            try {
                track.setStartThresholdInFrames(START_THRESHOLD_FRAMES)
            } catch (e: Exception) {
                statLastError = "start threshold: ${e.message}"
            }
        }
        return track
    }

    private fun newDecoder(): MediaCodec {
        val format =
            MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_OPUS, SAMPLE_RATE, CHANNELS)
        // Android's Opus decoder wants the Ogg identification header plus
        // the codec delay and seek pre-roll (ns), as in an Ogg/WebM file.
        format.setByteBuffer("csd-0", ByteBuffer.wrap(opusHead()))
        format.setByteBuffer("csd-1", nativeLong(PRE_SKIP * 1_000_000_000L / SAMPLE_RATE))
        format.setByteBuffer("csd-2", nativeLong(SEEK_PREROLL_NS))
        format.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, MAX_PACKET_BYTES)
        val codec = MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_AUDIO_OPUS)
        try {
            codec.configure(format, null, null, 0)
            codec.start()
        } catch (e: Exception) {
            codec.release()
            throw e
        }
        return codec
    }

    /** "OpusHead": version 1, 2 channels, pre-skip, 48 kHz, gain 0, mapping 0. */
    private fun opusHead(): ByteArray =
        ByteBuffer.allocate(19).order(ByteOrder.LITTLE_ENDIAN)
            .put("OpusHead".toByteArray(Charsets.US_ASCII))
            .put(1.toByte())
            .put(CHANNELS.toByte())
            .putShort(PRE_SKIP.toShort())
            .putInt(SAMPLE_RATE)
            .putShort(0)
            .put(0.toByte())
            .array()

    private fun nativeLong(value: Long): ByteBuffer =
        ByteBuffer.allocate(8).order(ByteOrder.nativeOrder()).putLong(value).apply { flip() }

    /**
     * One start..stop on its own thread: owns the decoder, the track and the
     * jitter-buffer state (see the class comment for the rules).
     */
    private inner class Session(
        private val id: Int,
        private var codec: MediaCodec?,
        private val track: AudioTrack,
    ) : Runnable {
        private val info = MediaCodec.BufferInfo()
        private var ptsUs = 0L
        private var framesWritten = 0L
        // The track is playing; false while (re)buffering.
        private var playing = false
        // The playback head moved since play(): the mixer really consumes
        // the track. Until then nothing is dropped (an Android without a
        // start threshold only starts a full track) and nothing trimmed.
        private var flowing = false
        private var headAtPlay = 0L
        private var playAt = 0L
        private var targetMs = START_TARGET_MS
        // Rebuffering after an underrun (not the first fill): the target
        // also follows the gap that caused it (raiseForGap).
        private var rebuffering = false
        // The calm rule: the lowest cushion of each of the last
        // FLOOR_PERIODS periods of CALM_PERIOD_MS, counted since the target
        // last went up.
        private val floors = LongArray(FLOOR_PERIODS)
        private var periods = 0
        private var periodStart = SystemClock.elapsedRealtime()
        private var periodMinMs = Long.MAX_VALUE
        // Throwing decoded sound away until the cushion is back near target.
        private var dropping = false
        // Lowest cushion seen in the current trim window.
        private var windowStart = 0L
        private var windowMinMs = Long.MAX_VALUE
        private val timestamp = AudioTimestamp()
        private var measuredAt = 0L
        private var volume = 1f
        private var codecFailures = 0

        override fun run() {
            Process.setThreadPriority(Process.THREAD_PRIORITY_AUDIO)
            try {
                while (session == id) {
                    val packet = packets.poll(LOOP_WAIT_MS, TimeUnit.MILLISECONDS)
                    if (session != id) break
                    try {
                        val decoder = codec ?: newDecoder().also { codec = it }
                        if (packet != null) {
                            queue(decoder, packet)
                            drain(decoder, OUTPUT_WAIT_US)
                            codecFailures = 0
                        } else {
                            drain(decoder, 0)
                        }
                    } catch (e: Exception) {
                        // A broken decoder: a fresh one picks up from the
                        // next packet (Opus resyncs within a packet or two).
                        statCodecErrors++
                        statLastError = "${e.javaClass.simpleName}: ${e.message}"
                        releaseCodec()
                        if (++codecFailures >= MAX_CODEC_FAILURES) {
                            mainHandler.post {
                                endBySelf(id, "PC sound stopped: the phone couldn't decode it")
                            }
                            break
                        }
                    }
                    try {
                        watchCushion()
                        applyVolume()
                    } catch (e: Exception) {
                        statLastError = "${e.javaClass.simpleName}: ${e.message}"
                    }
                }
            } catch (_: InterruptedException) {
                // Not used for stopping, but never leak the codec over it.
            } finally {
                releaseCodec()
                try {
                    track.pause()
                    track.flush()
                    track.stop()
                } catch (_: Exception) {
                    // Never started, or already stopped.
                }
                track.release()
            }
        }

        private fun releaseCodec() {
            val c = codec ?: return
            codec = null
            try {
                c.stop()
            } catch (_: Exception) {
                // Already in an error state; release anyway.
            }
            c.release()
        }

        private fun queue(decoder: MediaCodec, packet: ByteArray) {
            val index = decoder.dequeueInputBuffer(INPUT_WAIT_US)
            // Decoder backed up: lose these 20 ms rather than fall behind.
            if (index < 0) return
            val buffer = decoder.getInputBuffer(index)
                ?: throw IllegalStateException("no input buffer")
            buffer.clear()
            // An oversized packet (never sent by the agent) is skipped as an
            // empty buffer; the input slot must go back either way.
            val size = if (packet.size <= buffer.remaining()) packet.size else 0
            buffer.put(packet, 0, size)
            decoder.queueInputBuffer(index, 0, size, ptsUs, 0)
            ptsUs += FRAME_US
        }

        /** Plays every decoded block that is ready; waits up to [firstWaitUs]
         * for the first one. */
        private fun drain(decoder: MediaCodec, firstWaitUs: Long) {
            var waitUs = firstWaitUs
            while (true) {
                val index = decoder.dequeueOutputBuffer(info, waitUs)
                waitUs = 0
                if (index >= 0) {
                    val pcm = decoder.getOutputBuffer(index)
                    if (pcm != null && info.size > 0) {
                        statDecoded++
                        play(pcm, info.offset, info.size)
                    }
                    decoder.releaseOutputBuffer(index, false)
                } else if (index == MediaCodec.INFO_TRY_AGAIN_LATER) {
                    return
                }
                // Format / buffers changed: always 48 kHz stereo 16-bit
                // here, nothing to redo; keep draining.
            }
        }

        /** Hands one decoded block to the track, unless the cushion is too
         * deep (then the block is dropped to catch up). */
        private fun play(pcm: ByteBuffer, offset: Int, size: Int) {
            val cushion = cushionMs()
            if (dropping) {
                if (cushion > targetMs + RESUME_ABOVE_TARGET_MS) return
                dropping = false
            } else if (playing && flowing && cushion > targetMs + MAX_ABOVE_TARGET_MS) {
                dropping = true
                statTrims++
                return
            }
            pcm.position(offset)
            pcm.limit(offset + size)
            val written = track.write(pcm, size, AudioTrack.WRITE_NON_BLOCKING)
            if (written > 0) framesWritten += written / BYTES_PER_FRAME
            if (written < 0) statLastError = "AudioTrack.write error $written"
            statWrittenMs = framesWritten * 1000 / SAMPLE_RATE
            if (!playing) {
                if (rebuffering) raiseForGap()
                // Buffered enough (or the track is full): go.
                if (cushionMs() >= targetMs || written < size) {
                    rebuffering = false
                    startTrack()
                }
            }
        }

        /**
         * Rebuffering after an underrun: the target becomes at least the
         * longest recent gap between packets + GAP_MARGIN_MS (by now the
         * packets that ended the hole are in, so it is counted). A gap over
         * MAX_TARGET_MS is an outage (a reconnect), not jitter: no cushion
         * rides that out, so it leaves the target alone.
         */
        private fun raiseForGap() {
            val gap = recentGapMs()
            if (gap > MAX_TARGET_MS) return
            val wanted = minOf(MAX_TARGET_MS, gap + GAP_MARGIN_MS)
            if (wanted > targetMs) raiseTarget(wanted, SystemClock.elapsedRealtime())
        }

        private fun raiseTarget(value: Long, now: Long) {
            targetMs = value
            statTargetMs = value
            periods = 0
            periodStart = now
            periodMinMs = Long.MAX_VALUE
        }

        private fun startTrack() {
            track.play()
            playing = true
            statPlaying = true
            flowing = false
            statFlowing = false
            headAtPlay = head()
            playAt = SystemClock.elapsedRealtime()
            restartWindow(playAt)
        }

        /** Underrun -> pause and rebuffer with a bigger target; a calm link
         * lowers the target; a cushion that stays too deep gets trimmed.
         * Rules and numbers: the class comment. */
        private fun watchCushion() {
            if (!playing) return
            val now = SystemClock.elapsedRealtime()
            val cushion = cushionMs()
            statCushionMs = cushion
            statTargetMs = targetMs
            if (!flowing) {
                if (head() != headAtPlay) {
                    flowing = true
                    statFlowing = true
                    restartWindow(now)
                } else if (now - playAt >= STALL_MS) {
                    // Still not consumed: the mixer gave up on the track
                    // (disabled after waiting). Kick it again; the writes
                    // that keep coming fill it meanwhile.
                    statStalls++
                    statLastError = "track stalled, restarting it"
                    track.pause()
                    startTrack()
                }
                return
            }
            measureOutput(now)
            if (cushion < UNDERRUN_MS && packets.isEmpty()) {
                track.pause()
                playing = false
                statPlaying = false
                statUnderruns++
                statFloorMs = 0
                dropping = false
                rebuffering = true
                raiseTarget(minOf(MAX_TARGET_MS, targetMs + TARGET_UP_MS), now)
                return
            }
            periodMinMs = minOf(periodMinMs, cushion)
            if (now - periodStart >= CALM_PERIOD_MS) {
                floors[periods % FLOOR_PERIODS] = periodMinMs
                periods++
                statFloorMs = periodMinMs
                // Calm for FLOOR_PERIODS periods in a row, and the cushion
                // never came near running dry in any of them: one step down.
                val floor = floors.minOrNull() ?: 0L
                if (periods >= FLOOR_PERIODS && targetMs > MIN_TARGET_MS &&
                    floor >= TARGET_DOWN_MS + FLOOR_MARGIN_MS
                ) {
                    targetMs = maxOf(MIN_TARGET_MS, targetMs - TARGET_DOWN_MS)
                }
                periodStart = now
                periodMinMs = Long.MAX_VALUE
            }
            windowMinMs = minOf(windowMinMs, cushion)
            if (now - windowStart >= TRIM_WINDOW_MS) {
                if (windowMinMs > targetMs + TRIM_ABOVE_TARGET_MS) {
                    dropping = true
                    statTrims++
                }
                restartWindow(now)
            }
        }

        /** out_ms (write -> speaker) and the mixer's underrun count, for
         * [stats]; once a second (getTimestamp isn't free). */
        private fun measureOutput(now: Long) {
            if (now - measuredAt < MEASURE_EVERY_MS) return
            measuredAt = now
            statTrackUnderruns = track.underrunCount
            if (!track.getTimestamp(timestamp)) return
            // The frame at the speaker at nanoTime, moved on to now. Its low
            // 32 bits wrap like the head's.
            val ageNs = System.nanoTime() - timestamp.nanoTime
            val presented = timestamp.framePosition + ageNs * SAMPLE_RATE / 1_000_000_000L
            val frames = (framesWritten - presented) and 0xFFFFFFFFL
            if (frames <= Int.MAX_VALUE) statOutMs = frames * 1000 / SAMPLE_RATE
        }

        private fun restartWindow(now: Long) {
            windowStart = now
            windowMinMs = Long.MAX_VALUE
        }

        // The head is an unsigned 32-bit frame count (wraps after ~25 h).
        private fun head(): Long = track.playbackHeadPosition.toLong() and 0xFFFFFFFFL

        /** Milliseconds of sound the track holds that haven't played yet. */
        private fun cushionMs(): Long {
            val frames = (framesWritten - head()) and 0xFFFFFFFFL
            // The head never passes what was written; anything else is a
            // stale read, count it as empty.
            if (frames > Int.MAX_VALUE) return 0
            return frames * 1000 / SAMPLE_RATE
        }

        private fun applyVolume() {
            val wanted = if (muted) 0f else if (ducked) DUCK_VOLUME else 1f
            if (wanted != volume) {
                track.setVolume(wanted)
                volume = wanted
            }
        }
    }

    private companion object {
        const val SAMPLE_RATE = 48000
        const val CHANNELS = 2
        const val BYTES_PER_FRAME = CHANNELS * 2
        const val FRAME_US = 20_000L
        // The agent's libopus encoder delay at 48 kHz: 120 since it encodes
        // with RESTRICTED_LOWDELAY (agent/pc_audio.py), 312 before. Only the
        // first samples of a session depend on it: an older agent's extra
        // 4 ms of encoder priming (near silence) play, nothing else changes.
        const val PRE_SKIP = 120
        const val SEEK_PREROLL_NS = 80_000_000L
        // libopus never makes a packet bigger than the agent's 4000 B buffer.
        const val MAX_PACKET_BYTES = 8192
        // 2 s of packets; only reached when the player thread is stuck.
        const val MAX_QUEUED_PACKETS = 100

        // The jitter buffer (class comment). 50 at least: packets come 20 ms
        // at a time and the head moves in mixer bursts.
        const val START_TARGET_MS = 80L
        const val MIN_TARGET_MS = 50L
        const val MAX_TARGET_MS = 400L
        const val TARGET_UP_MS = 40L
        const val GAP_MARGIN_MS = 20L
        const val TARGET_DOWN_MS = 20L
        // One step down per period, once FLOOR_PERIODS of them in a row
        // (3 min) kept the cushion FLOOR_MARGIN_MS above the step: a Wi-Fi
        // that hiccups every minute or two keeps the target that rides its
        // hiccups out, instead of rebuffering on every other one.
        const val CALM_PERIOD_MS = 10_000L
        const val FLOOR_PERIODS = 18
        const val FLOOR_MARGIN_MS = 20L
        const val MAX_ABOVE_TARGET_MS = 300L
        const val RESUME_ABOVE_TARGET_MS = 20L
        const val TRIM_WINDOW_MS = 3_000L
        const val TRIM_ABOVE_TARGET_MS = 40L
        const val UNDERRUN_MS = 5L
        // Packet gaps are kept per window of this (the last 5-10 s count).
        const val GAP_WINDOW_MS = 5_000L
        const val MEASURE_EVERY_MS = 1_000L
        // 20 ms: the mixer may start the track as soon as it holds this.
        const val START_THRESHOLD_FRAMES = SAMPLE_RATE / 50
        // A played track whose head hasn't moved after this is stuck.
        const val STALL_MS = 1_500L

        const val LOOP_WAIT_MS = 10L
        const val INPUT_WAIT_US = 10_000L
        const val OUTPUT_WAIT_US = 2_000L
        const val MAX_CODEC_FAILURES = 5
        const val STOP_WAIT_MS = 500L
        const val DUCK_VOLUME = 0.25f
    }
}
