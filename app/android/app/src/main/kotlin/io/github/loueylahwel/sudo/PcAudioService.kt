package io.github.loueylahwel.sudo

import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.drawable.Icon
import android.media.MediaMetadata
import android.media.session.MediaSession
import android.media.session.PlaybackState
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import java.net.HttpURLConnection
import java.net.URL

/**
 * Keeps "PC sound on this phone" playing with the screen off or the app in
 * the background. It plays nothing itself (OpusPlayer, owned by
 * MainActivity, does): it is the foreground service Android wants before it
 * lets an app keep its network and CPU in the background, plus a partial
 * wake lock and a Wi-Fi lock so neither sleeps between packets.
 *
 * Its notification is a media notification for what the PC is playing
 * ([NowPlaying], from the agent's media.info / media.changed through Dart):
 * title, artist, cover, a progress bar and previous / play-pause / next,
 * plus Stop (the sound back on the PC). A MediaSession carries it, so it
 * sits in the media panel and the lock screen, and headset buttons work;
 * every button goes to the PC's player through [mediaListener].
 *
 * Started when the sound starts, stopped when it stops (see MainActivity).
 */
class PcAudioService : Service() {
    private var wakeLock: PowerManager.WakeLock? = null
    // Wi-Fi without power-save naps while the sound plays: see WifiBoost.

    private var session: MediaSession? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
        session = MediaSession(this, "SudoPcSound").apply {
            setCallback(object : MediaSession.Callback() {
                override fun onPlay() = media("media_play_pause")
                override fun onPause() = media("media_play_pause")
                override fun onSkipToNext() = media("media_next")
                override fun onSkipToPrevious() = media("media_previous")
                override fun onStop() {
                    stopListener?.invoke("PC sound is back on the PC")
                    stopSelf()
                }
                override fun onSeekTo(pos: Long) {
                    mediaListener?.invoke("seek", pos / 1000.0)
                    // Show the jump at once; the PC's answer follows.
                    nowPlaying = nowPlaying.copy(positionMs = pos, at = SystemClock.elapsedRealtime())
                    refresh()
                }
            })
            isActive = true
        }
        applySession()
    }

    private fun media(action: String) {
        if (action == "media_play_pause") {
            // Optimistic, like the app's card; the PC's state follows.
            val np = nowPlaying
            nowPlaying = np.copy(playing = !np.playing, positionMs = np.positionNow(),
                at = SystemClock.elapsedRealtime())
            refresh()
        }
        mediaListener?.invoke(action, null)
    }

    /** New [nowPlaying]: session and notification follow. Main thread. */
    private fun refresh() {
        applySession()
        try {
            getSystemService(NotificationManager::class.java)
                .notify(NOTIFICATION_ID, buildNotification())
        } catch (e: Exception) {
            Log.w(TAG, "notification update failed", e)
        }
    }

    private fun applySession() {
        val s = session ?: return
        val np = nowPlaying
        val meta = MediaMetadata.Builder()
            .putString(MediaMetadata.METADATA_KEY_TITLE, np.title.ifEmpty { "PC sound" })
            .putString(MediaMetadata.METADATA_KEY_ARTIST, np.artist)
            .putLong(MediaMetadata.METADATA_KEY_DURATION, np.durationMs)
        artBitmap?.takeIf { artFor == np.artUrl }?.let {
            meta.putBitmap(MediaMetadata.METADATA_KEY_ALBUM_ART, it)
        }
        s.setMetadata(meta.build())
        var actions = PlaybackState.ACTION_PLAY or PlaybackState.ACTION_PAUSE or
            PlaybackState.ACTION_PLAY_PAUSE or PlaybackState.ACTION_SKIP_TO_NEXT or
            PlaybackState.ACTION_SKIP_TO_PREVIOUS or PlaybackState.ACTION_STOP
        if (np.durationMs > 0) actions = actions or PlaybackState.ACTION_SEEK_TO
        s.setPlaybackState(
            PlaybackState.Builder()
                .setActions(actions)
                .setState(
                    if (np.playing) PlaybackState.STATE_PLAYING else PlaybackState.STATE_PAUSED,
                    np.positionMs, if (np.playing) 1f else 0f, np.at,
                )
                .build(),
        )
        loadArt(np.artUrl)
    }

    /** Fetches the cover once per URL, off the main thread. */
    private fun loadArt(url: String) {
        if (url.isEmpty() || url == artFor || url == artLoading) return
        artLoading = url
        Thread {
            val bitmap = try {
                val conn = URL(url).openConnection() as HttpURLConnection
                conn.connectTimeout = 5000
                conn.readTimeout = 5000
                conn.inputStream.use { BitmapFactory.decodeStream(it) }
            } catch (e: Exception) {
                null
            }
            mainHandler.post {
                if (artLoading == url) artLoading = null
                if (bitmap != null && nowPlaying.artUrl == url) {
                    artBitmap = bitmap
                    artFor = url
                    refresh()
                }
            }
        }.start()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_PLAY_PAUSE -> { media("media_play_pause"); return START_NOT_STICKY }
            ACTION_NEXT -> { media("media_next"); return START_NOT_STICKY }
            ACTION_PREVIOUS -> { media("media_previous"); return START_NOT_STICKY }
        }
        if (intent?.action == ACTION_STOP) {
            // The notification's Stop: MainActivity stops the player and
            // tells Dart, which hands the sound back to the PC.
            stopListener?.invoke("PC sound is back on the PC")
            stopSelf()
            return START_NOT_STICKY
        }
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                startForeground(
                    NOTIFICATION_ID, buildNotification(),
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK,
                )
            } else {
                startForeground(NOTIFICATION_ID, buildNotification())
            }
        } catch (e: Exception) {
            // Refused (the app was not in the foreground): the sound still
            // plays while the app is open, it just may stop with the screen.
            Log.w(TAG, "foreground service refused", e)
            stopSelf()
            return START_NOT_STICKY
        }
        acquireLocks()
        return START_NOT_STICKY
    }

    /** Swiped away from the recent apps: the Dart side goes with it. */
    override fun onTaskRemoved(rootIntent: Intent?) {
        stopListener?.invoke("PC sound is back on the PC")
        stopSelf()
        super.onTaskRemoved(rootIntent)
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        session?.release()
        session = null
        releaseLocks()
        super.onDestroy()
    }

    @SuppressLint("WakelockTimeout") // held exactly as long as the sound plays
    private fun acquireLocks() {
        if (wakeLock == null) {
            val power = getSystemService(Context.POWER_SERVICE) as PowerManager
            wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "sudo:pc-sound").apply {
                setReferenceCounted(false)
                acquire()
            }
        }
        WifiBoost.hold(this, BOOST_TAG, true)
    }

    private fun releaseLocks() {
        WifiBoost.hold(this, BOOST_TAG, false)
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
    }

    private fun buildNotification(): Notification {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID, "PC sound", NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = "Shown while the PC's sound plays on this phone"
                setShowBadge(false)
            }
            getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        }
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        // Tapping the notification brings the app back as it was.
        val launch = packageManager.getLaunchIntentForPackage(packageName)
            ?: Intent(this, MainActivity::class.java)
        val open = PendingIntent.getActivity(this, 0, launch, flags)
        val stop = PendingIntent.getService(
            this, 1, Intent(this, PcAudioService::class.java).setAction(ACTION_STOP), flags,
        )
        fun button(action: String, code: Int): PendingIntent = PendingIntent.getService(
            this, code, Intent(this, PcAudioService::class.java).setAction(action), flags,
        )
        val np = nowPlaying
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this).setPriority(Notification.PRIORITY_LOW)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            // Show it right away, not after Android's 10 s grace.
            builder.setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE)
        }
        fun action(icon: Int, title: String, intent: PendingIntent) =
            Notification.Action.Builder(Icon.createWithResource(this, icon), title, intent).build()
        builder
            .setSmallIcon(R.drawable.ic_stat_pc_sound)
            .setContentTitle(np.title.ifEmpty { "Playing the PC's sound" })
            .setContentText(
                np.artist.ifEmpty { "The PC is quiet while its sound plays here" },
            )
            .setSubText("PC sound")
            .setContentIntent(open)
            .setOngoing(true)
            .setShowWhen(false)
            .setCategory(Notification.CATEGORY_TRANSPORT)
            .setVisibility(Notification.VISIBILITY_PUBLIC)
            .addAction(action(android.R.drawable.ic_media_previous, "Previous",
                button(ACTION_PREVIOUS, 2)))
            .addAction(
                if (np.playing) {
                    action(android.R.drawable.ic_media_pause, "Pause", button(ACTION_PLAY_PAUSE, 3))
                } else {
                    action(android.R.drawable.ic_media_play, "Play", button(ACTION_PLAY_PAUSE, 3))
                },
            )
            .addAction(action(android.R.drawable.ic_media_next, "Next", button(ACTION_NEXT, 4)))
            .addAction(action(R.drawable.ic_stat_stop, "Stop", stop))
        artBitmap?.takeIf { artFor == np.artUrl }?.let { builder.setLargeIcon(it) }
        session?.let {
            builder.setStyle(
                Notification.MediaStyle()
                    .setMediaSession(it.sessionToken)
                    .setShowActionsInCompactView(0, 1, 2),
            )
        }
        return builder.build()
    }

    companion object {
        private const val BOOST_TAG = "pc-sound"
        private const val TAG = "SudoPcAudio"
        private const val CHANNEL_ID = "pc_audio"
        private const val NOTIFICATION_ID = 4107
        private const val ACTION_STOP = "io.github.loueylahwel.sudo.action.PC_AUDIO_STOP"
        private const val ACTION_PLAY_PAUSE = "io.github.loueylahwel.sudo.action.PC_PLAY_PAUSE"
        private const val ACTION_NEXT = "io.github.loueylahwel.sudo.action.PC_NEXT"
        private const val ACTION_PREVIOUS = "io.github.loueylahwel.sudo.action.PC_PREVIOUS"

        private val mainHandler = Handler(Looper.getMainLooper())
        private var instance: PcAudioService? = null

        private var nowPlaying = NowPlaying()
        private var artBitmap: Bitmap? = null
        private var artFor: String? = null
        private var artLoading: String? = null

        /** Main thread: the PC's player changed (Dart's media info). */
        fun setNowPlaying(np: NowPlaying) {
            nowPlaying = np
            instance?.refresh()
        }

        /** Set by MainActivity: a media button in the notification, the
         * lock screen or a headset — (action, seek seconds or null). */
        @Volatile var mediaListener: ((String, Double?) -> Unit)? = null

        /** Set by MainActivity: the user asked to stop from outside the app
         * (notification, recent apps). Called on the main thread with a
         * sentence for the user. */
        @Volatile var stopListener: ((String) -> Unit)? = null

        fun start(context: Context) {
            val intent = Intent(context, PcAudioService::class.java)
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    context.startForegroundService(intent)
                } else {
                    context.startService(intent)
                }
            } catch (e: Exception) {
                // Background start refused: play without it (see above).
                Log.w(TAG, "could not start the foreground service", e)
            }
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, PcAudioService::class.java))
        }
    }
}

/** What the PC plays, as the notification shows it. */
data class NowPlaying(
    val title: String = "",
    val artist: String = "",
    val artUrl: String = "",
    val playing: Boolean = false,
    val positionMs: Long = 0,
    val durationMs: Long = 0,
    // elapsedRealtime of positionMs.
    val at: Long = SystemClock.elapsedRealtime(),
) {
    fun positionNow(): Long {
        val p = positionMs + if (playing) SystemClock.elapsedRealtime() - at else 0
        return if (durationMs > 0) p.coerceAtMost(durationMs) else p
    }
}
