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
import android.graphics.drawable.Icon
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log

/**
 * Keeps "PC sound on this phone" playing with the screen off or the app in
 * the background. It plays nothing itself (OpusPlayer, owned by
 * MainActivity, does): it is the foreground service Android wants before it
 * lets an app keep its network and CPU in the background, plus a partial
 * wake lock and a Wi-Fi lock so neither sleeps between packets. Its
 * notification ("Playing the PC's sound") has a Stop button.
 *
 * Started when the sound starts, stopped when it stops (see MainActivity).
 */
class PcAudioService : Service() {
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    // Android 10-13: LOW_LATENCY only works with the screen on and the app
    // in front, so with the screen off (what this service is for) only
    // HIGH_PERF keeps Wi-Fi power saving from batching the packets.
    private var highPerfLock: WifiManager.WifiLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
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
        val wifi = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        if (wifiLock == null) {
            // Low latency keeps Wi-Fi power saving from batching packets
            // into late clumps; before Android 10 high-perf is the closest.
            val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                WifiManager.WIFI_MODE_FULL_LOW_LATENCY
            } else {
                @Suppress("DEPRECATION")
                WifiManager.WIFI_MODE_FULL_HIGH_PERF
            }
            wifiLock = wifi.createWifiLock(mode, "sudo:pc-sound").apply {
                setReferenceCounted(false)
                acquire()
            }
        }
        if (highPerfLock == null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q &&
            Build.VERSION.SDK_INT < Build.VERSION_CODES.UPSIDE_DOWN_CAKE
        ) {
            @Suppress("DEPRECATION")
            highPerfLock = wifi.createWifiLock(
                WifiManager.WIFI_MODE_FULL_HIGH_PERF, "sudo:pc-sound-hp",
            ).apply {
                setReferenceCounted(false)
                acquire()
            }
        }
    }

    private fun releaseLocks() {
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
        wifiLock?.let { if (it.isHeld) it.release() }
        wifiLock = null
        highPerfLock?.let { if (it.isHeld) it.release() }
        highPerfLock = null
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
        return builder
            .setSmallIcon(R.drawable.ic_stat_pc_sound)
            .setContentTitle("Playing the PC's sound")
            .setContentText("The PC is quiet while its sound plays here")
            .setContentIntent(open)
            .setOngoing(true)
            .setShowWhen(false)
            .setCategory(Notification.CATEGORY_SERVICE)
            .addAction(
                Notification.Action.Builder(
                    Icon.createWithResource(this, R.drawable.ic_stat_stop), "Stop", stop,
                ).build(),
            )
            .build()
    }

    companion object {
        private const val TAG = "SudoPcAudio"
        private const val CHANNEL_ID = "pc_audio"
        private const val NOTIFICATION_ID = 4107
        private const val ACTION_STOP = "io.github.loueylahwel.sudo.action.PC_AUDIO_STOP"

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
