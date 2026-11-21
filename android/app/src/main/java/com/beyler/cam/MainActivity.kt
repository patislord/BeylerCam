package com.beyler.cam

import android.Manifest
import android.content.pm.PackageManager
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Surface
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import org.json.JSONObject

class MainActivity : AppCompatActivity(), StreamServer.Callbacks, CameraStreamer.Listener {

    private lateinit var previewView: PreviewView
    private lateinit var txtIp: TextView
    private lateinit var txtOtherIps: TextView
    private lateinit var txtStatus: TextView
    private lateinit var txtInfo: TextView
    private lateinit var dimOverlay: View

    private lateinit var streamer: CameraStreamer
    private lateinit var server: StreamServer
    private lateinit var discovery: DiscoveryResponder

    private var multicastLock: WifiManager.MulticastLock? = null
    private var wifiLock: WifiManager.WifiLock? = null

    private val uiHandler = Handler(Looper.getMainLooper())
    @Volatile private var connectedPcs: List<String> = emptyList()
    private var lastError: String? = null
    private var tick = 0

    private val permissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) streamer.start() else txtStatus.setText(R.string.camera_permission_denied)
        }

    private val refreshRunnable = object : Runnable {
        override fun run() {
            if (tick++ % 5 == 0) updateIpViews()
            updateInfoView()
            uiHandler.postDelayed(this, 1000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        hideSystemBars()

        previewView = findViewById(R.id.previewView)
        txtIp = findViewById(R.id.txtIp)
        txtOtherIps = findViewById(R.id.txtOtherIps)
        txtStatus = findViewById(R.id.txtStatus)
        txtInfo = findViewById(R.id.txtInfo)
        dimOverlay = findViewById(R.id.dimOverlay)

        streamer = CameraStreamer(this, this, previewView, ::currentDisplayRotation, this)
        server = StreamServer(Protocol.PORT, this)
        discovery = DiscoveryResponder(Protocol.PORT) { discoveryJson() }

        findViewById<Button>(R.id.btnSwitch).setOnClickListener { streamer.switchCamera() }
        findViewById<Button>(R.id.btnDim).setOnClickListener { setDimmed(true) }
        dimOverlay.setOnClickListener { setDimmed(false) }

        acquireWifiLocks()
        server.start()
        discovery.start()
        updateIpViews()
        updateStatusView()

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            streamer.start()
        } else {
            permissionLauncher.launch(Manifest.permission.CAMERA)
        }
    }

    override fun onResume() {
        super.onResume()
        hideSystemBars()
        uiHandler.post(refreshRunnable)
    }

    override fun onPause() {
        uiHandler.removeCallbacks(refreshRunnable)
        super.onPause()
    }

    override fun onDestroy() {
        streamer.shutdown()
        server.stop()
        discovery.stop()
        releaseWifiLocks()
        super.onDestroy()
    }

    // ---------------- CameraStreamer.Listener ----------------

    override fun hasConsumers(): Boolean = server.clientCount > 0

    override fun onFrame(rotation: Int, front: Boolean, jpeg: ByteArray) {
        server.sendFrame(rotation, front, jpeg)
    }

    override fun onStatusChanged(status: JSONObject) {
        server.sendStatus(status)
        lastError = null
        runOnUiThread { updateInfoView() }
    }

    override fun onError(message: String) {
        lastError = message
        runOnUiThread { updateInfoView() }
    }

    // ---------------- StreamServer.Callbacks ----------------

    override fun helloJson(): JSONObject = discoveryJson().apply {
        val status = streamer.statusJson()
        status.keys().forEach { key -> put(key, status.get(key)) }
    }

    override fun statusJson(): JSONObject = streamer.statusJson()

    override fun onCommand(command: JSONObject) {
        when (command.optString("cmd")) {
            "config" -> streamer.updateConfig { c ->
                c.copy(
                    width = command.optInt("width", c.width),
                    height = command.optInt("height", c.height),
                    fps = command.optInt("fps", c.fps),
                    quality = command.optInt("quality", c.quality),
                    front = when (command.optString("camera")) {
                        "front" -> true
                        "back" -> false
                        else -> c.front
                    },
                )
            }
            "switch" -> streamer.switchCamera()
        }
    }

    override fun onClientsChanged(addresses: List<String>) {
        connectedPcs = addresses
        runOnUiThread { updateStatusView() }
    }

    // ---------------- Arayüz ----------------

    private fun discoveryJson(): JSONObject = JSONObject().apply {
        put("app", Protocol.APP)
        put("version", Protocol.VERSION)
        put("device", deviceName())
        put("port", Protocol.PORT)
    }

    private fun deviceName(): String {
        val manufacturer = Build.MANUFACTURER.replaceFirstChar { it.uppercase() }
        return if (Build.MODEL.startsWith(Build.MANUFACTURER, ignoreCase = true)) Build.MODEL
        else "$manufacturer ${Build.MODEL}"
    }

    private fun updateIpViews() {
        val addresses = NetUtils.localIpv4Addresses()
        if (addresses.isEmpty()) {
            txtIp.setText(R.string.no_network)
            txtOtherIps.visibility = View.GONE
            return
        }
        txtIp.text = "${addresses[0].ip} : ${Protocol.PORT}"
        val others = addresses.drop(1).take(3)
        if (others.isEmpty()) {
            txtOtherIps.visibility = View.GONE
        } else {
            txtOtherIps.visibility = View.VISIBLE
            txtOtherIps.text = others.joinToString("\n") { "${it.ip} (${it.interfaceName})" }
        }
    }

    private fun updateStatusView() {
        val pcs = connectedPcs
        txtStatus.text = if (pcs.isEmpty()) getString(R.string.waiting_pc)
        else getString(R.string.connected_to, pcs.joinToString(", "))
    }

    private fun updateInfoView() {
        lastError?.let {
            txtInfo.text = it
            return
        }
        val status = streamer.statusJson()
        val camera = getString(if (status.optString("camera") == "front") R.string.camera_front else R.string.camera_back)
        val base = "${status.optInt("width")}×${status.optInt("height")} • ${status.optInt("fps")} FPS • $camera • Kalite ${status.optInt("quality")}"
        txtInfo.text = if (connectedPcs.isNotEmpty()) {
            "$base\nGönderilen: ${"%.1f".format(streamer.sentFps)} FPS"
        } else {
            base
        }
    }

    private fun setDimmed(dimmed: Boolean) {
        dimOverlay.visibility = if (dimmed) View.VISIBLE else View.GONE
        window.attributes = window.attributes.apply {
            screenBrightness = if (dimmed) 0.01f else WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE
        }
        streamer.setPreviewEnabled(!dimmed)
    }

    private fun hideSystemBars() {
        val controller = WindowCompat.getInsetsController(window, window.decorView)
        controller.systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        controller.hide(WindowInsetsCompat.Type.systemBars())
    }

    @Suppress("DEPRECATION")
    private fun currentDisplayRotation(): Int =
        previewView.display?.rotation ?: windowManager.defaultDisplay?.rotation ?: Surface.ROTATION_90

    @Suppress("DEPRECATION")
    private fun acquireWifiLocks() {
        val wifi = applicationContext.getSystemService(WIFI_SERVICE) as? WifiManager ?: return
        try {
            multicastLock = wifi.createMulticastLock("BeylerCam:discovery").apply {
                setReferenceCounted(false)
                acquire()
            }
            val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                WifiManager.WIFI_MODE_FULL_LOW_LATENCY
            } else {
                WifiManager.WIFI_MODE_FULL_HIGH_PERF
            }
            wifiLock = wifi.createWifiLock(mode, "BeylerCam:stream").apply {
                setReferenceCounted(false)
                acquire()
            }
        } catch (_: Exception) {
        }
    }

    private fun releaseWifiLocks() {
        try {
            multicastLock?.release()
            wifiLock?.release()
        } catch (_: Exception) {
        }
    }
}
