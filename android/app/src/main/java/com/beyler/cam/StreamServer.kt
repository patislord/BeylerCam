package com.beyler.cam

import android.util.Log
import org.json.JSONException
import org.json.JSONObject
import java.io.BufferedOutputStream
import java.io.DataOutputStream
import java.io.IOException
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.thread
import kotlin.concurrent.withLock

/**
 * PC bağlantılarını kabul eden TCP sunucusu.
 * Her istemci için ayrı yazma/okuma iş parçacığı vardır. Yavaş bir ağda
 * eski kareler atılır, her zaman en yeni kare gönderilir (düşük gecikme).
 */
class StreamServer(private val port: Int, private val callbacks: Callbacks) {

    interface Callbacks {
        fun helloJson(): JSONObject
        fun statusJson(): JSONObject
        fun onCommand(command: JSONObject)
        fun onClientsChanged(addresses: List<String>)
    }

    private class Packet(val type: Int, val head: ByteArray?, val body: ByteArray)

    private val clients = CopyOnWriteArrayList<Client>()
    @Volatile private var serverSocket: ServerSocket? = null
    @Volatile private var running = false

    val clientCount: Int get() = clients.size

    fun start() {
        if (running) return
        running = true
        thread(name = "bc-accept", isDaemon = true) {
            while (running) {
                try {
                    ServerSocket().use { ss ->
                        ss.reuseAddress = true
                        ss.bind(InetSocketAddress(port))
                        serverSocket = ss
                        Log.i(TAG, "TCP sunucu dinliyor: $port")
                        while (running) {
                            val socket = ss.accept()
                            val client = Client(socket)
                            clients.add(client)
                            client.start()
                            notifyClients()
                        }
                    }
                } catch (e: IOException) {
                    if (running) {
                        Log.w(TAG, "Sunucu hatası, yeniden deneniyor", e)
                        Thread.sleep(1000)
                    }
                }
            }
        }
    }

    fun stop() {
        running = false
        try {
            serverSocket?.close()
        } catch (_: IOException) {
        }
        clients.forEach { it.close() }
        clients.clear()
    }

    fun sendFrame(rotation: Int, front: Boolean, jpeg: ByteArray) {
        if (clients.isEmpty()) return
        val flags = if (front) Protocol.FLAG_FRONT else 0
        val head = byteArrayOf(
            ((rotation shr 8) and 0xFF).toByte(),
            (rotation and 0xFF).toByte(),
            flags.toByte(),
        )
        val packet = Packet(Protocol.TYPE_FRAME, head, jpeg)
        clients.forEach { it.offerFrame(packet) }
    }

    fun sendStatus(status: JSONObject) {
        if (clients.isEmpty()) return
        val packet = Packet(Protocol.TYPE_STATUS, null, status.toString().toByteArray(Charsets.UTF_8))
        clients.forEach { it.offerControl(packet) }
    }

    private fun notifyClients() {
        callbacks.onClientsChanged(clients.map { it.address })
    }

    private inner class Client(private val socket: Socket) {
        val address: String = socket.inetAddress?.hostAddress ?: "?"
        private val lock = ReentrantLock()
        private val hasData = lock.newCondition()
        private var pendingFrame: Packet? = null
        private val pendingControl = ArrayDeque<Packet>()
        private val open = AtomicBoolean(true)

        fun start() {
            try {
                socket.tcpNoDelay = true
                socket.sendBufferSize = 512 * 1024
            } catch (_: Exception) {
            }
            offerControl(Packet(Protocol.TYPE_HELLO, null, callbacks.helloJson().toString().toByteArray(Charsets.UTF_8)))
            thread(name = "bc-writer-$address", isDaemon = true) { writeLoop() }
            thread(name = "bc-reader-$address", isDaemon = true) { readLoop() }
            Log.i(TAG, "PC bağlandı: $address")
        }

        fun offerFrame(packet: Packet) {
            lock.withLock {
                pendingFrame = packet // önceki gönderilmemiş kare atılır
                hasData.signalAll()
            }
        }

        fun offerControl(packet: Packet) {
            lock.withLock {
                pendingControl.addLast(packet)
                hasData.signalAll()
            }
        }

        private fun writeLoop() {
            try {
                val out = DataOutputStream(BufferedOutputStream(socket.getOutputStream(), 64 * 1024))
                val batch = ArrayList<Packet>(4)
                while (open.get()) {
                    lock.withLock {
                        while (open.get() && pendingFrame == null && pendingControl.isEmpty()) {
                            hasData.await()
                        }
                        batch.addAll(pendingControl)
                        pendingControl.clear()
                        pendingFrame?.let { batch.add(it) }
                        pendingFrame = null
                    }
                    for (p in batch) {
                        val headSize = p.head?.size ?: 0
                        out.writeByte(p.type)
                        out.writeInt(headSize + p.body.size)
                        p.head?.let { out.write(it) }
                        out.write(p.body)
                    }
                    out.flush()
                    batch.clear()
                }
            } catch (e: Exception) {
                Log.d(TAG, "Yazma bitti ($address): ${e.message}")
            } finally {
                close()
            }
        }

        private fun readLoop() {
            try {
                val reader = socket.getInputStream().bufferedReader(Charsets.UTF_8)
                while (open.get()) {
                    val line = reader.readLine() ?: break
                    if (line.isBlank() || line.length > 16 * 1024) continue
                    val json = try {
                        JSONObject(line)
                    } catch (e: JSONException) {
                        continue
                    }
                    when (json.optString("cmd")) {
                        "ping" -> {
                            val pong = JSONObject().put("t", json.opt("t") ?: 0)
                            offerControl(Packet(Protocol.TYPE_PONG, null, pong.toString().toByteArray(Charsets.UTF_8)))
                        }
                        else -> callbacks.onCommand(json)
                    }
                }
            } catch (e: Exception) {
                Log.d(TAG, "Okuma bitti ($address): ${e.message}")
            } finally {
                close()
            }
        }

        fun close() {
            if (!open.compareAndSet(true, false)) return
            lock.withLock { hasData.signalAll() }
            try {
                socket.close()
            } catch (_: IOException) {
            }
            if (clients.remove(this)) {
                Log.i(TAG, "PC ayrıldı: $address")
                notifyClients()
            }
        }
    }

    companion object {
        private const val TAG = "BeylerCam/Server"
    }
}
