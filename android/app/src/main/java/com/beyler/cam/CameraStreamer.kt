package com.beyler.cam

import android.content.Context
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CaptureRequest
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.util.Range
import android.util.Size
import androidx.annotation.OptIn
import androidx.camera.camera2.interop.Camera2CameraInfo
import androidx.camera.camera2.interop.Camera2Interop
import androidx.camera.camera2.interop.ExperimentalCamera2Interop
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.core.resolutionselector.AspectRatioStrategy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleOwner
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors
import kotlin.math.abs

/**
 * CameraX ile kamerayı açar, kareleri JPEG'e çevirip dinleyiciye verir.
 * Çözünürlük, FPS, kalite ve ön/arka kamera çalışırken değiştirilebilir.
 */
@OptIn(ExperimentalCamera2Interop::class)
class CameraStreamer(
    private val context: Context,
    private val lifecycleOwner: LifecycleOwner,
    private val previewView: PreviewView,
    private val displayRotation: () -> Int,
    private val listener: Listener,
) {
    interface Listener {
        fun hasConsumers(): Boolean
        fun onFrame(rotation: Int, front: Boolean, jpeg: ByteArray)
        fun onStatusChanged(status: JSONObject)
        fun onError(message: String)
    }

    data class Config(
        val width: Int = 1280,
        val height: Int = 720,
        val fps: Int = 30,
        val quality: Int = 80,
        val front: Boolean = false,
    )

    @Volatile var config = Config()
        private set
    @Volatile private var activeFront = false
    @Volatile private var activeFpsRange: Range<Int>? = null
    @Volatile private var actualWidth = 0
    @Volatile private var actualHeight = 0
    @Volatile private var cameras: List<String> = emptyList()
    @Volatile var sentFps = 0f
        private set

    private var provider: ProcessCameraProvider? = null
    private var previewEnabled = true
    private val mainHandler = Handler(Looper.getMainLooper())
    private val analysisExecutor = Executors.newSingleThreadExecutor { r -> Thread(r, "bc-encoder") }
    private val encoder = YuvJpegEncoder()

    // Sadece encoder iş parçacığında kullanılır
    private var nextDueNanos = 0L
    private var fpsWindowStart = 0L
    private var fpsWindowCount = 0

    fun start() {
        val future = ProcessCameraProvider.getInstance(context)
        future.addListener({
            try {
                val p = future.get()
                provider = p
                cameras = buildList {
                    if (p.safeHas(CameraSelector.DEFAULT_BACK_CAMERA)) add("back")
                    if (p.safeHas(CameraSelector.DEFAULT_FRONT_CAMERA)) add("front")
                }
                bind()
            } catch (e: Exception) {
                Log.e(TAG, "Kamera sağlayıcı alınamadı", e)
                listener.onError("Kamera açılamadı: ${e.message}")
            }
        }, ContextCompat.getMainExecutor(context))
    }

    /** Herhangi bir iş parçacığından çağrılabilir. */
    fun updateConfig(transform: (Config) -> Config) {
        mainHandler.post {
            val old = config
            val new = sanitize(transform(old))
            config = new
            val needsRebind = new.width != old.width || new.height != old.height ||
                new.fps != old.fps || new.front != old.front
            if (needsRebind) bind() else listener.onStatusChanged(statusJson())
        }
    }

    fun switchCamera() = updateConfig { it.copy(front = !it.front) }

    /** Ekran karartıldığında önizlemeyi kapatıp pil tasarrufu sağlar. */
    fun setPreviewEnabled(enabled: Boolean) {
        mainHandler.post {
            if (previewEnabled != enabled) {
                previewEnabled = enabled
                bind()
            }
        }
    }

    fun shutdown() {
        mainHandler.removeCallbacksAndMessages(null)
        try {
            provider?.unbindAll()
        } catch (_: Exception) {
        }
        analysisExecutor.shutdown()
    }

    fun statusJson(): JSONObject = JSONObject().apply {
        val cfg = config
        put("width", if (actualWidth > 0) actualWidth else cfg.width)
        put("height", if (actualHeight > 0) actualHeight else cfg.height)
        put("requestedWidth", cfg.width)
        put("requestedHeight", cfg.height)
        put("fps", cfg.fps)
        put("quality", cfg.quality)
        put("camera", if (activeFront) "front" else "back")
        put("cameras", JSONArray(cameras))
        activeFpsRange?.let { put("fpsRange", "${it.lower}-${it.upper}") }
    }

    private fun sanitize(c: Config) = c.copy(
        width = c.width.coerceIn(160, 3840),
        height = c.height.coerceIn(120, 2160),
        fps = c.fps.coerceIn(1, 60),
        quality = c.quality.coerceIn(10, 100),
    )

    private fun bind() {
        val p = provider ?: return
        val cfg = config
        val selector = when {
            cfg.front && p.safeHas(CameraSelector.DEFAULT_FRONT_CAMERA) -> CameraSelector.DEFAULT_FRONT_CAMERA
            !cfg.front && p.safeHas(CameraSelector.DEFAULT_BACK_CAMERA) -> CameraSelector.DEFAULT_BACK_CAMERA
            p.safeHas(CameraSelector.DEFAULT_BACK_CAMERA) -> CameraSelector.DEFAULT_BACK_CAMERA
            p.safeHas(CameraSelector.DEFAULT_FRONT_CAMERA) -> CameraSelector.DEFAULT_FRONT_CAMERA
            else -> {
                listener.onError("Telefonda kamera bulunamadı")
                return
            }
        }
        activeFront = selector == CameraSelector.DEFAULT_FRONT_CAMERA
        if (activeFront != cfg.front) config = cfg.copy(front = activeFront)

        val fpsRange = chooseFpsRange(p, selector, cfg.fps)
        if (!tryBind(p, selector, cfg, fpsRange) && fpsRange != null) {
            Log.w(TAG, "FPS aralığı ($fpsRange) ile açılamadı, varsayılanla deneniyor")
            tryBind(p, selector, cfg, null)
        }
        actualWidth = 0
        actualHeight = 0
        nextDueNanos = 0L
        listener.onStatusChanged(statusJson())
    }

    private fun tryBind(
        p: ProcessCameraProvider,
        selector: CameraSelector,
        cfg: Config,
        fpsRange: Range<Int>?,
    ): Boolean {
        val rotation = displayRotation()
        val aspect = if (abs(cfg.width.toFloat() / cfg.height - 4f / 3f) < 0.05f) {
            AspectRatioStrategy.RATIO_4_3_FALLBACK_AUTO_STRATEGY
        } else {
            AspectRatioStrategy.RATIO_16_9_FALLBACK_AUTO_STRATEGY
        }
        val analysisSelector = ResolutionSelector.Builder()
            .setAspectRatioStrategy(aspect)
            .setResolutionStrategy(
                ResolutionStrategy(
                    Size(cfg.width, cfg.height),
                    ResolutionStrategy.FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER,
                )
            )
            .build()

        val analysisBuilder = ImageAnalysis.Builder()
            .setResolutionSelector(analysisSelector)
            .setTargetRotation(rotation)
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_YUV_420_888)
        val previewBuilder = Preview.Builder()
            .setResolutionSelector(ResolutionSelector.Builder().setAspectRatioStrategy(aspect).build())
            .setTargetRotation(rotation)
        if (fpsRange != null) {
            Camera2Interop.Extender(analysisBuilder)
                .setCaptureRequestOption(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, fpsRange)
            Camera2Interop.Extender(previewBuilder)
                .setCaptureRequestOption(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, fpsRange)
        }

        val analysis = analysisBuilder.build()
        analysis.setAnalyzer(analysisExecutor, ::analyze)

        return try {
            p.unbindAll()
            if (previewEnabled) {
                val preview = previewBuilder.build()
                preview.setSurfaceProvider(previewView.surfaceProvider)
                p.bindToLifecycle(lifecycleOwner, selector, preview, analysis)
            } else {
                p.bindToLifecycle(lifecycleOwner, selector, analysis)
            }
            activeFpsRange = fpsRange
            true
        } catch (e: Exception) {
            Log.e(TAG, "Kamera bağlanamadı", e)
            listener.onError("Kamera başlatılamadı: ${e.message}")
            false
        }
    }

    private fun chooseFpsRange(p: ProcessCameraProvider, selector: CameraSelector, fps: Int): Range<Int>? = try {
        val info = selector.filter(p.availableCameraInfos).firstOrNull()
        val ranges = info?.let {
            Camera2CameraInfo.from(it)
                .getCameraCharacteristic(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES)
        }
        when {
            ranges.isNullOrEmpty() -> null
            // Hedef FPS'te biten, en sabit (alt sınırı en yüksek) aralık
            else -> ranges.filter { it.upper == fps }.maxByOrNull { it.lower }
                ?: ranges.filter { it.upper > fps }.minByOrNull { it.upper }
                ?: ranges.maxByOrNull { it.upper }
        }
    } catch (e: Exception) {
        Log.w(TAG, "FPS aralıkları okunamadı", e)
        null
    }

    private fun analyze(image: ImageProxy) {
        image.use { img ->
            if (!listener.hasConsumers()) {
                sentFps = 0f
                return
            }
            val cfg = config
            val now = System.nanoTime()

            // Yazılımsal FPS sınırlama (kamera daha hızlı veriyorsa)
            val interval = 1_000_000_000L / cfg.fps
            if (now < nextDueNanos - interval / 4) return
            nextDueNanos = if (now - nextDueNanos > interval) now + interval else nextDueNanos + interval

            if (img.width != actualWidth || img.height != actualHeight) {
                actualWidth = img.width
                actualHeight = img.height
                listener.onStatusChanged(statusJson())
            }

            val jpeg = try {
                encoder.encode(img, cfg.quality)
            } catch (e: Exception) {
                Log.w(TAG, "JPEG kodlama hatası", e)
                return
            }
            listener.onFrame(img.imageInfo.rotationDegrees, activeFront, jpeg)

            fpsWindowCount++
            if (fpsWindowStart == 0L) fpsWindowStart = now
            val elapsed = now - fpsWindowStart
            if (elapsed >= 1_000_000_000L) {
                sentFps = fpsWindowCount * 1e9f / elapsed
                fpsWindowCount = 0
                fpsWindowStart = now
            }
        }
    }

    private fun ProcessCameraProvider.safeHas(selector: CameraSelector): Boolean = try {
        hasCamera(selector)
    } catch (e: Exception) {
        false
    }

    companion object {
        private const val TAG = "BeylerCam/Camera"
    }
}
