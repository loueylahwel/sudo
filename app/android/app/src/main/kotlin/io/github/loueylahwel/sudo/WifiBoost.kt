package io.github.loueylahwel.sudo

import android.content.Context
import android.net.wifi.WifiManager
import android.os.Build

/**
 * Wi-Fi without power-save naps while something needs it: the PC's sound
 * playing here (PcAudioService) or the Screen page being open (Dart, through
 * MainActivity's "sudo/wifi" channel). Each holds it under its own tag; it
 * stays on while any tag holds it.
 *
 * Why the Screen page needs it too: the owner's phone napped its Wi-Fi even
 * with the screen on and Sudo in front. Frames came in at exactly 2 fps (one
 * every 500 ms, the radio's wake-up rhythm), and the touchpad's moves left
 * the phone in bursts, so the PC's real cursor lagged while the phone's own
 * drawn one looked fine.
 *
 * Holds: a LOW_LATENCY Wi-Fi lock (Android honours it with the app in front
 * and the screen on), HIGH_PERF too on Android 10-13 (it still works there
 * with the screen off), and WifiKeepAwake's 40 ms datagram to the router,
 * which keeps the radio up in every case, Android 14 included.
 */
object WifiBoost {
    private val holders = mutableSetOf<String>()
    private var lowLatency: WifiManager.WifiLock? = null
    private var highPerf: WifiManager.WifiLock? = null
    private var keepAwake: WifiKeepAwake? = null

    @Synchronized
    fun hold(context: Context, tag: String, on: Boolean) {
        val was = holders.isNotEmpty()
        if (on) holders.add(tag) else holders.remove(tag)
        val now = holders.isNotEmpty()
        if (now && !was) start(context.applicationContext)
        if (!now && was) stop()
    }

    val active: Boolean @Synchronized get() = holders.isNotEmpty()

    private fun start(context: Context) {
        val wifi = context.getSystemService(Context.WIFI_SERVICE) as WifiManager
        lowLatency = wifi.createWifiLock(
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                WifiManager.WIFI_MODE_FULL_LOW_LATENCY
            } else {
                @Suppress("DEPRECATION")
                WifiManager.WIFI_MODE_FULL_HIGH_PERF
            },
            "sudo:wifi-boost",
        ).apply {
            setReferenceCounted(false)
            acquire()
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q &&
            Build.VERSION.SDK_INT < Build.VERSION_CODES.UPSIDE_DOWN_CAKE
        ) {
            @Suppress("DEPRECATION")
            highPerf = wifi.createWifiLock(
                WifiManager.WIFI_MODE_FULL_HIGH_PERF, "sudo:wifi-boost-hp",
            ).apply {
                setReferenceCounted(false)
                acquire()
            }
        }
        keepAwake = WifiKeepAwake(context).also { it.start() }
    }

    private fun stop() {
        keepAwake?.stop()
        keepAwake = null
        lowLatency?.let { if (it.isHeld) it.release() }
        lowLatency = null
        highPerf?.let { if (it.isHeld) it.release() }
        highPerf = null
    }
}
