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

    // Written on the codec thread / platform thread, read on both.
    @Volatile private var codec: MediaCodec? = null
    @Volatile private var surface: Surface? = null
    @Volatile private var textureEntry: TextureRegistry.SurfaceTextureEntry? = null
    private var ptsUs = 0L

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
                val decoder = MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
                decoder.configure(format, outSurface, null, 0)
                decoder.start()
                codec = decoder
                ptsUs = 0L
            }
        } catch (e: Exception) {
            // Codec setup failed: don't leak the texture/surface.
            outSurface.release()
            entry.release()
            throw e
        }
        textureEntry = entry
        surface = outSurface
        return entry.id()
    }

    /**
     * Queues an Annex-B bytestream chunk for decoding and returns
     * immediately. Arbitrary split points are fine: MediaCodec in
     * byte-stream mode reassembles NAL units, and SPS/PPS lead the stream
     * so the decoder syncs on the first chunk.
     */
    fun feed(chunk: ByteArray) {
        handler.post {
            val decoder = codec ?: return@post
            try {
                feedInternal(decoder, chunk)
                drainInternal(decoder)
            } catch (_: Exception) {
                // A transient codec error must not kill the thread; the
                // stream resyncs on the next IDR.
            }
        }
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

    private fun feedInternal(decoder: MediaCodec, data: ByteArray) {
        var offset = 0
        while (offset < data.size) {
            val inputIndex = decoder.dequeueInputBuffer(INPUT_TIMEOUT_US)
            if (inputIndex < 0) return // no input buffer in time; drop the chunk
            val buffer = decoder.getInputBuffer(inputIndex) ?: return
            buffer.clear()
            val count = minOf(buffer.remaining(), data.size - offset)
            buffer.put(data, offset, count)
            ptsUs += FRAME_INTERVAL_US
            decoder.queueInputBuffer(inputIndex, 0, count, ptsUs, 0)
            offset += count
        }
    }

    private fun drainInternal(decoder: MediaCodec) {
        val info = MediaCodec.BufferInfo()
        while (true) {
            val outputIndex = decoder.dequeueOutputBuffer(info, 0)
            if (outputIndex >= 0) {
                // `true` = render to the configured Surface right away;
                // the SurfaceTexture then notifies Flutter of a new frame.
                decoder.releaseOutputBuffer(outputIndex, true)
            } else if (outputIndex == MediaCodec.INFO_TRY_AGAIN_LATER) {
                return
            }
            // INFO_OUTPUT_FORMAT_CHANGED / INFO_OUTPUT_BUFFERS_CHANGED need
            // no handling when rendering to a Surface.
        }
    }

    private companion object {
        const val INPUT_TIMEOUT_US = 10_000L
        const val FRAME_INTERVAL_US = 33_333L
        const val CODEC_OP_TIMEOUT_SEC = 10L
    }
}
