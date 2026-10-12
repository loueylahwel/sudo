package io.github.loueylahwel.sudo

import android.content.BroadcastReceiver
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.content.pm.ActivityInfo
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import android.view.KeyEvent
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.io.File

class MainActivity : FlutterActivity() {
    // Native bridge for saving files into the public Download folder
    // (MediaStore needs no permission on Android 10+).
    private val downloadsChannel = "pcocket/downloads"

    // Native bridge for the H.264 screen stream: hardware MediaCodec
    // decoder rendering into a Flutter texture.
    private val videoChannel = "pcocket/video"
    private var videoDecoder: VideoDecoder? = null

    // Hardware volume keys, forwarded to Dart ("volumeKey", "up"/"down") so
    // they adjust the PC's volume instead of the phone's. Dart switches the
    // forwarding off ("setForwarding", false) while the phone itself plays
    // the PC's sound (phone screen), so the keys control what you hear.
    private var volumeKeysChannel: MethodChannel? = null
    @Volatile private var forwardVolumeKeys = true

    // Sound of the PC's phone-screen apps (PCM1 packets), see PcmPlayer.
    private var pcmPlayer: PcmPlayer? = null

    companion object {
        // Lock taps from the home screen widget reach Dart through this
        // channel while the app process is alive (see WidgetActionReceiver).
        var widgetActionsChannel: MethodChannel? = null
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        volumeKeysChannel =
            MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "pcocket/volume_keys")
                .also { channel ->
                    channel.setMethodCallHandler { call, result ->
                        if (call.method == "setForwarding") {
                            forwardVolumeKeys = call.arguments as? Boolean ?: true
                            result.success(null)
                        } else {
                            result.notImplemented()
                        }
                    }
                }
        // Landscape that follows the motion sensor even with auto-rotate off.
        // Flutter's landscapeLeft+landscapeRight maps to USER_LANDSCAPE, which
        // obeys the rotation lock: flipping the phone over left the PC screen
        // upside down. "unlock" hands orientation back to the system.
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "pcocket/orientation")
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "sensorLandscape" -> {
                        requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
                        result.success(null)
                    }
                    "unlock" -> {
                        requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
                        result.success(null)
                    }
                    else -> result.notImplemented()
                }
            }
        val player = PcmPlayer()
        pcmPlayer = player
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "pcocket/audio")
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "start" -> {
                        player.start(
                            call.argument<Int>("rate") ?: 48000,
                            call.argument<Int>("channels") ?: 2,
                        )
                        result.success(true)
                    }
                    "write" -> {
                        val bytes = call.arguments as? ByteArray
                        if (bytes != null) player.write(bytes)
                        result.success(null)
                    }
                    "stop" -> {
                        player.stop()
                        result.success(true)
                    }
                    else -> result.notImplemented()
                }
            }
        widgetActionsChannel =
            MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "pcocket/widget_actions")
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, downloadsChannel)
            .setMethodCallHandler { call, result ->
                if (call.method != "saveToDownloads") {
                    result.notImplemented()
                    return@setMethodCallHandler
                }
                val name = call.argument<String>("name")
                val path = call.argument<String>("path")
                if (name.isNullOrEmpty() || path.isNullOrEmpty()) {
                    result.error("bad_args", "name and path are required", null)
                    return@setMethodCallHandler
                }
                try {
                    saveToDownloads(name, File(path))
                    result.success(true)
                } catch (e: Exception) {
                    result.error("save_failed", e.message, null)
                }
            }
        val decoder = VideoDecoder(flutterEngine.renderer)
        videoDecoder = decoder
        val videoMethodChannel =
            MethodChannel(flutterEngine.dartExecutor.binaryMessenger, videoChannel)
        decoder.onCodecError = {
            runOnUiThread { videoMethodChannel.invokeMethod("onCodecError", null) }
        }
        decoder.onFirstFrame = {
            runOnUiThread { videoMethodChannel.invokeMethod("onFrameRendered", null) }
        }
        videoMethodChannel.setMethodCallHandler { call, result ->
                try {
                    when (call.method) {
                        "start" -> {
                            val w = call.argument<Int>("w") ?: 0
                            val h = call.argument<Int>("h") ?: 0
                            if (w <= 0 || h <= 0) {
                                result.error("bad_args", "w and h are required", null)
                            } else {
                                result.success(decoder.start(w, h))
                            }
                        }
                        "feed" -> {
                            // One access unit per call: its bytes, whether
                            // it is a keyframe, and its capture time (ms).
                            val bytes = call.argument<ByteArray>("bytes")
                            val key = call.argument<Boolean>("key") ?: false
                            // Dart ints arrive as Integer or Long by size.
                            val ts = call.argument<Number>("ts")?.toLong() ?: 0L
                            if (bytes == null) {
                                result.error("bad_args", "bytes are required", null)
                            } else {
                                decoder.feed(bytes, key, ts)
                                result.success(true)
                            }
                        }
                        "stop" -> {
                            decoder.stop()
                            result.success(true)
                        }
                        else -> result.notImplemented()
                    }
                } catch (e: Exception) {
                    result.error("video_decoder", e.message, null)
                }
            }
    }

    override fun cleanUpFlutterEngine(flutterEngine: FlutterEngine) {
        volumeKeysChannel = null
        widgetActionsChannel = null
        super.cleanUpFlutterEngine(flutterEngine)
    }

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        // Consume volume keys while the activity is in the foreground and
        // forward them to Dart; the phone's own volume is left alone.
        val direction = when (event.keyCode) {
            KeyEvent.KEYCODE_VOLUME_UP -> "up"
            KeyEvent.KEYCODE_VOLUME_DOWN -> "down"
            else -> null
        }
        if (direction != null && forwardVolumeKeys) {
            if (event.action == KeyEvent.ACTION_DOWN) {
                volumeKeysChannel?.invokeMethod("volumeKey", direction)
            }
            return true
        }
        return super.dispatchKeyEvent(event)
    }

    override fun onDestroy() {
        videoDecoder?.release()
        videoDecoder = null
        pcmPlayer?.release()
        pcmPlayer = null
        super.onDestroy()
    }

    private fun saveToDownloads(name: String, src: File) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            throw UnsupportedOperationException("Downloads bridge requires Android 10+")
        }
        val values = ContentValues().apply {
            put(MediaStore.Downloads.DISPLAY_NAME, name)
            put(
                MediaStore.Downloads.RELATIVE_PATH,
                Environment.DIRECTORY_DOWNLOADS + File.separator + "Sudo",
            )
            put(MediaStore.Downloads.IS_PENDING, 1)
        }
        val resolver = contentResolver
        val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
            ?: throw IllegalStateException("MediaStore insert failed")
        resolver.openOutputStream(uri)?.use { out ->
            src.inputStream().use { it.copyTo(out) }
        } ?: throw IllegalStateException("Cannot open destination")
        values.clear()
        values.put(MediaStore.Downloads.IS_PENDING, 0)
        resolver.update(uri, values, null, null)
    }
}

/// Target of the home screen widget's Lock button. While the app process is
/// alive the tap is forwarded to Dart (which sends `power {action:'lock'}`
/// over the live relay connection); otherwise the app is simply launched.
class WidgetActionReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ACTION_LOCK) return
        val channel = MainActivity.widgetActionsChannel
        if (channel != null) {
            channel.invokeMethod("lock", null)
        } else {
            val launch =
                context.packageManager.getLaunchIntentForPackage(context.packageName)
                    ?.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            if (launch != null) context.startActivity(launch)
        }
    }

    companion object {
        const val ACTION_LOCK = "io.github.loueylahwel.sudo.action.LOCK"
    }
}
