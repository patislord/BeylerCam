package com.beyler.cam

import android.util.Log
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetSocketAddress
import kotlin.concurrent.thread

/**
 * PC'nin "Telefonu Bul" isteğine (UDP yayını) cevap verir.
 */
class DiscoveryResponder(private val port: Int, private val info: () -> JSONObject) {
    @Volatile private var socket: DatagramSocket? = null
    @Volatile private var running = false

    fun start() {
        if (running) return
        running = true
        thread(name = "bc-discovery", isDaemon = true) {
            while (running) {
                try {
                    DatagramSocket(null).use { s ->
                        s.reuseAddress = true
                        s.broadcast = true
                        s.bind(InetSocketAddress(port))
                        socket = s
                        val buffer = ByteArray(512)
                        while (running) {
                            val packet = DatagramPacket(buffer, buffer.size)
                            s.receive(packet)
                            val message = String(packet.data, 0, packet.length, Charsets.UTF_8).trim()
                            if (message == Protocol.DISCOVER_MAGIC) {
                                val reply = info().toString().toByteArray(Charsets.UTF_8)
                                s.send(DatagramPacket(reply, reply.size, packet.address, packet.port))
                            }
                        }
                    }
                } catch (e: Exception) {
                    if (running) {
                        Log.w(TAG, "Keşif hatası, yeniden deneniyor: ${e.message}")
                        Thread.sleep(1000)
                    }
                }
            }
        }
    }

    fun stop() {
        running = false
        socket?.close()
    }

    companion object {
        private const val TAG = "BeylerCam/Discovery"
    }
}
