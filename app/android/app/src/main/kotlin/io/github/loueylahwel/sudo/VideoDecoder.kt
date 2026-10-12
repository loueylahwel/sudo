package io.github.loueylahwel.sudo

import android.media.MediaCodec
import android.media.MediaFormat
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.view.Surface
import io.flutter.view.TextureRegistry
import java.util.concurrent.Callable
import java.util.concurrent.ExecutionException
import java.util.concurrent.FutureTask
import java.util.concurrent.TimeUnit

/**
 * Hardware H.264 (AVC) decoder for the PC screen stream.
 *
 * Owns one [MediaCodec] decoder rendering into a Flutter [TextureRegistry]
 * SurfaceTexture; the texture id returned by [start] is shown in Dart with
 * a `Texture` widget, and every frame released to the Surface triggers a
 * repaint automatically (the engine owns the frame-available listener).
 *
 * Input is one complete access unit per [feed] (the agent frames the
 * stream that way), queued as one input buffer with its own timestamp and
 * key flag: many hardware decoders reject buffers that split or merge
 * access units, and none has to wait for the next AU to find where this
 * one ends.
 *
 * Threading model: all codec calls are serialized on a private
 * [HandlerThread] — MediaCodec is never touched from the UI thread.
 * [start]/[stop] are called from the platform thread via the MethodChannel
 * and block (bounded) until the codec thread finished the work, so a
 * `feed` sent right after `start` always finds a started codec (platform
 * channel messages are handled in order, and the codec thread is FIFO).
 */
class VideoDecoder(private val textures: TextureRegistry) {
    private val thread = HandlerThread("sudo-video-decoder").apply { start() }
    private val handler = Handler(thread.looper)

    /** Called on the codec thread when a decode error killed the codec —
     * Dart uses it to restart the stream (fresh SPS/PPS + IDR). */
    var onCodecError: (() -> Unit)? = null

    /** Called on the codec thread when the first picture after [start] was
     * rendered — Dart's "no picture" watchdog waits for it. */
    var onFirstFrame: (() -> Unit)? = null

    // Written on the codec thread / platform thread, read on both.
    @Volatile private var codec: MediaCodec? = null
    @Volatile private var surface: Surface? = null
    @Volatile private var textureEntry: TextureRegistry.SurfaceTextureEntry? = null
    // Codec thread only: decoding can only begin at an IDR, so after every
    // (re)start access units are dropped until the first key one.
    private var awaitingKey = true
    private var firstFrameRendered = false
    private var lastWidth = 0
    private var lastHeight = 0

    /**
     * Creates the decoder and its output texture. Called from the platform
     * (main) thread; blocks until the codec is started on the codec thread
     * and returns the Flutter texture id for the video output.
     */
    fun start(width: Int, height: Int): Long {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.LOLLIPOP) {
            throw UnsupportedOperationException("VideoDecoder requires Android 5.0+")
        }
        // A previous stream may still be alive (restart after a reconnect):
        // tear it down first so only one decoder ever owns the thread.
        stop()
        // TextureRegistry is main-thread-affine: create the entry and the
        // Surface here, hand only codec work to the codec thread.
        val entry = textures.createSurfaceTexture()
        entry.surfaceTexture().setDefaultBufferSize(width, height)
        val outSurface = Surface(entry.surfaceTexture())
        try {
            runOnCodecThread {
                val format =
                    MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, width, height)
                // A whole access unit must fit one input buffer; an IDR of a
                // busy desktop can be far larger than some decoders' default.
                format.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, maxOf(width * height, 1 shl 20))
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                    format.setInteger(MediaFormat.KEY_PRIORITY, 0) // realtime
                }
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                    // Output each frame as soon as it's decoded instead of
                    // holding a few for reordering (there are no B-frames).
                    format.setInteger(MediaFormat.KEY_LOW_LATENCY, 1)
                }
                val decoder = MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
                decoder.configure(format, outSurface, null, 0)
                decoder.start()
                codec = decoder
                awaitingKey = true
                firstFrameRendered = false
            }
        } catch (e: Exception) {
            // Codec setup failed: don't leak the texture/surface.
            outSurface.release()
            entry.release()
            throw e
        }
        textureEntry = entry
        surface = outSurface
        lastWidth = width
        lastHeight = height
        return entry.id()
    }

    /**
     * Queues ONE complete Annex-B access unit for decoding and returns
     * immediately. [key] marks an IDR (SPS/PPS lead it); [tsMs] is the
     * agent's capture timestamp, used as the presentation time.
     */
    fun feed(accessUnit: ByteArray, key: Boolean, tsMs: Long) {
        handler.post {
            val decoder = codec ?: return@post
            if (awaitingKey) {
                if (!key) return@post
                awaitingKey = false
            }
            try {
                queueAccessUnit(decoder, accessUnit, key, tsMs * 1000)
                // Give the decoder a moment to finish this frame so it is
                // rendered now, not when the next access unit arrives.
                drainInternal(decoder, OUTPUT_WAIT_US)
            } catch (_: Exception) {
                // Never feed a wedged codec mid-GOP (that is the green
                // corruption): release it and let Dart restart the stream —
                // a fresh SPS/PPS + IDR sequence resyncs cleanly.
                errorRelease()
            }
        }
    }

    /** Releases a broken codec and notifies Dart to restart the stream. */
    private fun errorRelease() {
        codec?.let {
            try {
                it.stop()
            } catch (_: Exception) {
                // Already errored; force release below.
            }
            it.release()
        }
        codec = null
        onCodecError?.invoke()
    }

    /**
     * Stops and releases the codec, then releases the output surface and
     * the Flutter texture entry. Idempotent; called from the platform
     * thread.
     */
    fun stop() {
        if (codec == null && textureEntry == null) return
        runOnCodecThread {
            codec?.let {
                try {
                    it.stop()
                } catch (_: Exception) {
                    // Codec already in an error state; release anyway.
                }
                it.release()
            }
            codec = null
        }
        surface?.release()
        surface = null
        textureEntry?.release()
        textureEntry = null
    }

    /** Stops the decoder and quits the codec thread for good. */
    fun release() {
        stop()
        thread.quitSafely()
    }

    /** Runs [block] on the codec thread and waits for it, rethrowing failures. */
    private fun runOnCodecThread(block: () -> Unit) {
        if (Thread.currentThread() === thread) {
            block()
            return
        }
        val task =
            FutureTask(
                Callable {
                    block()
                    null
                },
            )
        handler.post(task)
        try {
            task.get(CODEC_OP_TIMEOUT_SEC, TimeUnit.SECONDS)
        } catch (e: ExecutionException) {
            throw e.cause ?: e
        }
    }

    private fun queueAccessUnit(decoder: MediaCodec, data: ByteArray, key: Boolean, ptsUs: Long) {
        var inputIndex = decoder.dequeueInputBuffer(INPUT_TIMEOUT_US)
        var retries = 0
        while (inputIndex < 0 && retries < INPUT_RETRIES) {
            // Queue full: free output and retry instead of dropping the
            // access unit — a dropped one corrupts the rest of the GOP and
            // wedges the picture.
            drainInternal(decoder, 0)
            Thread.sleep(2)
            inputIndex = decoder.dequeueInputBuffer(INPUT_TIMEOUT_US)
            retries++
        }
        if (inputIndex < 0) throw IllegalStateException("decoder input starved")
        val buffer = decoder.getInputBuffer(inputIndex)
            ?: throw IllegalStateException("no input buffer")
        buffer.clear()
        // Splitting an access unit across buffers is exactly what decoders
        // reject: an AU that doesn't fit is an error (restart), not a split.
        if (data.size > buffer.remaining()) {
            throw IllegalStateException("access unit larger than the input buffer")
        }
        buffer.put(data)
        val flags = if (key) MediaCodec.BUFFER_FLAG_KEY_FRAME else 0
        decoder.queueInputBuffer(inputIndex, 0, data.size, ptsUs, flags)
    }

    /** Renders every decoded frame that is ready; waits up to [firstWaitUs]
     * for the first one. */
    private fun drainInternal(decoder: MediaCodec, firstWaitUs: Long) {
        val info = MediaCodec.BufferInfo()
        var waitUs = firstWaitUs
        while (true) {
            val outputIndex = decoder.dequeueOutputBuffer(info, waitUs)
            waitUs = 0
            if (outputIndex >= 0) {
                // `true` = render to the configured Surface right away (no
                // PTS scheduling); the SurfaceTexture then notifies Flutter
                // of a new frame.
                decoder.releaseOutputBuffer(outputIndex, true)
                if (!firstFrameRendered) {
                    firstFrameRendered = true
                    onFirstFrame?.invoke()
                }
            } else if (outputIndex == MediaCodec.INFO_TRY_AGAIN_LATER) {
                return
            }
            // INFO_OUTPUT_FORMAT_CHANGED / INFO_OUTPUT_BUFFERS_CHANGED need
            // no handling when rendering to a Surface.
        }
    }

    private companion object {
        const val INPUT_TIMEOUT_US = 10_000L
        const val INPUT_RETRIES = 5
        // Long enough for a hardware decoder to turn one 1080p frame
        // around, short next to the ~33 ms between access units.
        const val OUTPUT_WAIT_US = 8_000L
        const val CODEC_OP_TIMEOUT_SEC = 10L
    }
}
