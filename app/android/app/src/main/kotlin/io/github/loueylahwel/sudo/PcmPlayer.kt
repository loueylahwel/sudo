package io.github.loueylahwel.sudo

import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.os.Handler
import android.os.HandlerThread
import java.util.concurrent.atomic.AtomicInteger

/**
 * Plays the sound of the apps on the PC's phone screen: 16-bit PCM chunks
 * (40 ms each) streamed by the agent as PCM1 packets.
 *
 * Writes are handed to a private thread so the platform thread never blocks
 * on AudioTrack. Latency is bounded: when more than ~250 ms is already queued
 * (a burst after a Wi-Fi stall) new chunks are dropped, so the sound stays in
 * step with the picture instead of drifting further and further behind.
 */
class PcmPlayer {
    private val thread = HandlerThread("sudo-pcm").apply { start() }
    private val handler = Handler(thread.looper)

    @Volatile private var track: AudioTrack? = null
    private val queuedBytes = AtomicInteger(0)
    @Volatile private var maxQueuedBytes = 0

    @Suppress("DEPRECATION") // the stream-type constructor works on every API level
    fun start(rate: Int, channels: Int) {
        stop()
        val mask =
            if (channels == 1) AudioFormat.CHANNEL_OUT_MONO else AudioFormat.CHANNEL_OUT_STEREO
        val bytesPerSecond = rate * channels * 2
        val min = AudioTrack.getMinBufferSize(rate, mask, AudioFormat.ENCODING_PCM_16BIT)
        val t = AudioTrack(
            AudioManager.STREAM_MUSIC, rate, mask, AudioFormat.ENCODING_PCM_16BIT,
            maxOf(min * 2, bytesPerSecond / 5), // ~200 ms of device buffer
            AudioTrack.MODE_STREAM,
        )
        t.play()
        queuedBytes.set(0)
        maxQueuedBytes = bytesPerSecond / 4 // 250 ms waiting to be written
        track = t
    }

    fun write(bytes: ByteArray) {
        if (track == null) return
        if (queuedBytes.get() > maxQueuedBytes) return // behind: drop, catch up
        queuedBytes.addAndGet(bytes.size)
        handler.post {
            try {
                track?.write(bytes, 0, bytes.size)
            } catch (_: Exception) {
                // Track released mid-write (stop raced us); nothing to play.
            }
            queuedBytes.addAndGet(-bytes.size)
        }
    }

    fun stop() {
        val t = track ?: return
        track = null
        handler.post {
            try {
                t.stop()
            } catch (_: Exception) {
                // Already stopped.
            }
            t.release()
        }
    }

    fun release() {
        stop()
        thread.quitSafely()
    }
}
