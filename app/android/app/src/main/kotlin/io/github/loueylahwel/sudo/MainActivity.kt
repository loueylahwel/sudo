package io.github.loueylahwel.sudo

import android.Manifest
import android.content.BroadcastReceiver
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.content.pm.ActivityInfo
import android.content.pm.PackageManager
import android.media.AudioManager
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
    // "PC sound on this phone" does the same on its own: while OpusPlayer
    // runs the keys are never forwarded (see dispatchKeyEvent).
    private var volumeKeysChannel: MethodChannel? = null
    @Volatile private var forwardVolumeKeys = true

    // Sound of the PC's phone-screen apps (PCM1 packets), see PcmPlayer.
    private var pcmPlayer: PcmPlayer? = null

    // "PC sound on this phone": all of the PC's sound (OPS1 packets), see
    // OpusPlayer; PcAudioService keeps it going with the screen off.
    private var opusPlayer: OpusPlayer? = null

    companion object {
        // Lock taps from the home screen widget reach Dart through this
        // channel while the app process is alive (see WidgetActionReceiver).
        var widgetActionsChannel: MethodChannel? = null

        private const val NATIVE_PREFS = "sudo_native"
        private const val ASKED_NOTIFICATIONS = "asked_post_notifications"
        private const val REQUEST_NOTIFICATIONS = 4107
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
        configurePcAudio(flutterEngine)
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

    /**
     * "sudo/pcaudio": `start` (no args; throws with a message for the user
     * when the phone can't play), `write` (one Opus packet as bytes), `stop`.
     * Native -> Dart: `stopped(reason)` when the phone ended it by itself
     * (notification's Stop, another app took the audio focus, decoder
     * failure); Dart then hands the sound back to the PC.
     */
    private fun configurePcAudio(flutterEngine: FlutterEngine) {
        val player = OpusPlayer(this)
        opusPlayer = player
        val channel = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "sudo/pcaudio")
        player.onStopped = { reason ->
            endPcAudio()
            channel.invokeMethod("stopped", reason)
        }
        PcAudioService.stopListener = { reason ->
            if (player.isRunning) {
                endPcAudio()
                channel.invokeMethod("stopped", reason)
            }
        }
        channel.setMethodCallHandler { call, result ->
            when (call.method) {
                "start" -> {
                    try {
                        val wasRunning = player.isRunning
                        player.start()
                        if (!wasRunning) {
                            askForNotificationsOnce()
                            PcAudioService.start(this)
                            // The volume keys now set the phone's media
                            // volume, even between two sounds.
                            volumeControlStream = AudioManager.STREAM_MUSIC
                        }
                        result.success(true)
                    } catch (e: Exception) {
                        result.error("pcaudio", e.message ?: "The phone can't play the PC sound", null)
                    }
                }
                "write" -> {
                    val bytes = call.arguments as? ByteArray
                    if (bytes != null) player.write(bytes)
                    result.success(null)
                }
                "stop" -> {
                    endPcAudio()
                    result.success(true)
                }
                "stats" -> result.success(player.stats())
                else -> result.notImplemented()
            }
        }
    }

    private fun endPcAudio() {
        opusPlayer?.stop()
        PcAudioService.stop(this)
        volumeControlStream = AudioManager.USE_DEFAULT_STREAM_TYPE
    }

    /** Android 13+: the foreground service's notification (with its Stop
     * button) needs the notification permission. Asked once, the first time
     * the PC sound starts; the sound plays either way. */
    private fun askForNotificationsOnce() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) ==
            PackageManager.PERMISSION_GRANTED
        ) {
            return
        }
        val prefs = getSharedPreferences(NATIVE_PREFS, MODE_PRIVATE)
        if (prefs.getBoolean(ASKED_NOTIFICATIONS, false)) return
        prefs.edit().putBoolean(ASKED_NOTIFICATIONS, true).apply()
        requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQUEST_NOTIFICATIONS)
    }

    override fun cleanUpFlutterEngine(flutterEngine: FlutterEngine) {
        volumeKeysChannel = null
        widgetActionsChannel = null
        super.cleanUpFlutterEngine(flutterEngine)
    }

    // Back on the dashboard (the root route) ends in SystemNavigator.pop,
    // which would finish() this activity and, through onDestroy, stop the
    // PC sound without telling the PC. While it plays here, behave like a
    // music app instead: go to the background.
    override fun popSystemNavigator(): Boolean {
        if (opusPlayer?.isRunning == true) {
            moveTaskToBack(true)
            return true
        }
        return super.popSystemNavigator()
    }

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        // Consume volume keys while the activity is in the foreground and
        // forward them to Dart; the phone's own volume is left alone. Not
        // while the PC's sound plays here: then they set what you hear.
        val direction = when (event.keyCode) {
            KeyEvent.KEYCODE_VOLUME_UP -> "up"
            KeyEvent.KEYCODE_VOLUME_DOWN -> "down"
            else -> null
        }
        if (direction != null && forwardVolumeKeys && opusPlayer?.isRunning != true) {
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
        // The engine (and the Dart side with its socket) dies with this
        // activity: nothing would feed the player any more.
        PcAudioService.stopListener = null
        endPcAudio()
        opusPlayer = null
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
