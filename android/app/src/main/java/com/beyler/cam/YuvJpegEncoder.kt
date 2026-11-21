package com.beyler.cam

import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import androidx.camera.core.ImageProxy
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer

/**
 * CameraX YUV_420_888 karelerini JPEG'e çevirir.
 * Tamponlar yeniden kullanılır; tek bir iş parçacığından çağrılmalıdır.
 */
class YuvJpegEncoder {
    private var nv21 = ByteArray(0)
    private var uBytes = ByteArray(0)
    private var vBytes = ByteArray(0)
    private val out = ByteArrayOutputStream(512 * 1024)

    fun encode(image: ImageProxy, quality: Int): ByteArray {
        val width = image.width
        val height = image.height
        val ySize = width * height
        val chromaWidth = width / 2
        val chromaHeight = height / 2
        val total = ySize + 2 * chromaWidth * chromaHeight
        if (nv21.size != total) nv21 = ByteArray(total)

        val planes = image.planes

        // --- Y düzlemi ---
        val yPlane = planes[0]
        val yBuffer = yPlane.buffer.duplicate().apply { rewind() }
        val yRowStride = yPlane.rowStride
        val yPixelStride = yPlane.pixelStride
        if (yPixelStride == 1 && yRowStride == width) {
            yBuffer.get(nv21, 0, ySize)
        } else if (yPixelStride == 1) {
            for (row in 0 until height) {
                yBuffer.position(row * yRowStride)
                yBuffer.get(nv21, row * width, width)
            }
        } else {
            var pos = 0
            for (row in 0 until height) {
                var idx = row * yRowStride
                for (col in 0 until width) {
                    nv21[pos++] = yBuffer.get(idx)
                    idx += yPixelStride
                }
            }
        }

        // --- U/V düzlemleri -> NV21 (VU sırayla) ---
        val uPlane = planes[1]
        val vPlane = planes[2]
        uBytes = copyPlane(uPlane.buffer, uBytes)
        vBytes = copyPlane(vPlane.buffer, vBytes)
        val uRowStride = uPlane.rowStride
        val uPixelStride = uPlane.pixelStride
        val vRowStride = vPlane.rowStride
        val vPixelStride = vPlane.pixelStride
        val u = uBytes
        val v = vBytes
        var pos = ySize
        for (row in 0 until chromaHeight) {
            var ui = row * uRowStride
            var vi = row * vRowStride
            for (col in 0 until chromaWidth) {
                nv21[pos++] = v[vi]
                nv21[pos++] = u[ui]
                ui += uPixelStride
                vi += vPixelStride
            }
        }

        out.reset()
        YuvImage(nv21, ImageFormat.NV21, width, height, null)
            .compressToJpeg(Rect(0, 0, width, height), quality, out)
        return out.toByteArray()
    }

    private fun copyPlane(buffer: ByteBuffer, reuse: ByteArray): ByteArray {
        val b = buffer.duplicate().apply { rewind() }
        val n = b.remaining()
        val target = if (reuse.size >= n) reuse else ByteArray(n)
        b.get(target, 0, n)
        return target
    }
}
