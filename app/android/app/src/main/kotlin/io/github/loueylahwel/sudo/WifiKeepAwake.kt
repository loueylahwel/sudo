package io.github.loueylahwel.sudo

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.SystemClock
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.Inet4Address
import java.net.InetAddress

/**
 * Keeps the phone's Wi-Fi radio awake while the PC's sound plays here.
 *
 * With the screen off (or Sudo in the background) Android lets Wi-Fi doze:
 * the radio wakes only every ~0.5 s to collect what the router held for it,
 * so the sound arrives in half-second clumps. Measured on the owner's
 * Infinix (Android 14), PC -> phone pings every 50 ms: 465, 413, 364 ... 5
 * ms, over and over. The Wi-Fi locks don't help there: LOW_LATENCY only
 * counts while the app is visible, and HIGH_PERF does nothing from Android
 * 14. Sending keeps the radio up for a short while after (the driver's
 * power-save timeout), so a 1-byte UDP datagram to the router every 40 ms
 * keeps it awake for as long as the sound plays: 25 packets/s, about 2 KB/s,
 * and it never leaves the Wi-Fi network (port 9 is "discard"). Only on
 * Wi-Fi: mobile data has no such doze and would pay for the bytes.
 */
class WifiKeepAwake(context: Context) {
    private val connectivity =
        context.applicationContext.getSystemService(ConnectivityManager::class.java)
    @Volatile private var thread: Thread? = null

    fun start() {
        if (thread != null) return
        val t = Thread({ loop() }, "sudo-wifi-awake")
        t.isDaemon = true
        thread = t
        t.start()
    }

    fun stop() {
        val t = thread ?: return
        thread = null
        t.interrupt()
    }

    private fun loop() {
        val me = Thread.currentThread()
        var socket: DatagramSocket? = null
        var target: InetAddress? = null
        var resolvedAt = 0L
        val payload = byteArrayOf(0)
        try {
            socket = DatagramSocket()
            while (thread === me) {
                val now = SystemClock.elapsedRealtime()
                // The network can change under us (Wi-Fi -> mobile data).
                if (now - resolvedAt >= RESOLVE_EVERY_MS) {
                    target = wifiGateway()
                    resolvedAt = now
                    active = target != null
                }
                val to = target
                if (to != null) {
                    try {
                        socket.send(DatagramPacket(payload, payload.size, to, DISCARD_PORT))
                        sent++
                    } catch (_: Exception) {
                        resolvedAt = 0 // network going away: look again
                    }
                }
                Thread.sleep(INTERVAL_MS)
            }
        } catch (_: InterruptedException) {
            // stop()
        } catch (_: Exception) {
            // No socket at all: nothing to keep awake with.
        } finally {
            socket?.close()
            active = false
        }
    }

    /** The router of the active network, when that network is Wi-Fi. */
    private fun wifiGateway(): InetAddress? {
        val network = connectivity.activeNetwork ?: return null
        val caps = connectivity.getNetworkCapabilities(network) ?: return null
        if (!caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) return null
        val routes = connectivity.getLinkProperties(network)?.routes ?: return null
        val gateways = routes.filter { it.isDefaultRoute }
            .mapNotNull { it.gateway }
            .filter { !it.isAnyLocalAddress }
        return gateways.firstOrNull { it is Inet4Address } ?: gateways.firstOrNull()
    }

    companion object {
        private const val INTERVAL_MS = 40L
        private const val RESOLVE_EVERY_MS = 5_000L
        private const val DISCARD_PORT = 9

        /** For the stats the PC logs: sending now, and how many so far. */
        @Volatile var active = false
        @Volatile var sent = 0L
    }
}
