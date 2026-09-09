package com.cheby.codex.mobile.ui

import android.content.ContentResolver
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Matrix
import android.net.Uri
import androidx.exifinterface.media.ExifInterface
import com.cheby.codex.mobile.gateway.LocalImageCapability
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream
import java.util.UUID
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext

enum class AttachmentUploadState { SELECTED, UPLOADING, UPLOADED, FAILED }

data class PendingImageAttachment(
    val clientAssetId: String,
    val mediaType: String,
    val sourceBytes: ByteArray,
    val thumbnailBytes: ByteArray,
    val sourceWidth: Int,
    val sourceHeight: Int,
    val uploadState: AttachmentUploadState = AttachmentUploadState.SELECTED,
)

enum class ImageSelectionFailure {
    UNREADABLE,
    UNSUPPORTED,
    TOO_LARGE,
    INVALID_IMAGE,
}

class ImageSelectionException(val reason: ImageSelectionFailure) : Exception("Image selection failed")

interface ImageAttachmentReader {
    suspend fun read(
        sourceUris: List<String>,
        capability: LocalImageCapability,
    ): List<PendingImageAttachment>
}

class AndroidImageAttachmentReader(
    private val resolver: ContentResolver,
    private val trustedCameraAuthority: String? = null,
    private val ioDispatcher: CoroutineDispatcher = Dispatchers.IO,
) : ImageAttachmentReader {
    override suspend fun read(
        sourceUris: List<String>,
        capability: LocalImageCapability,
    ): List<PendingImageAttachment> = withContext(ioDispatcher) {
        val completed = mutableListOf<PendingImageAttachment>()
        try {
            sourceUris.forEach { source ->
                currentCoroutineContext().ensureActive()
                completed += readOne(source, capability)
            }
            completed
        } catch (cancelled: CancellationException) {
            completed.wipeImageBytes()
            throw cancelled
        } catch (error: ImageSelectionException) {
            completed.wipeImageBytes()
            throw error
        } catch (_: OutOfMemoryError) {
            completed.wipeImageBytes()
            throw ImageSelectionException(ImageSelectionFailure.TOO_LARGE)
        } catch (_: Exception) {
            completed.wipeImageBytes()
            throw ImageSelectionException(ImageSelectionFailure.UNREADABLE)
        }
    }

    private suspend fun readOne(
        source: String,
        capability: LocalImageCapability,
    ): PendingImageAttachment {
        val uri = runCatching { Uri.parse(source) }
            .getOrElse { throw ImageSelectionException(ImageSelectionFailure.UNREADABLE) }
        val trustedCameraInput = trustedCameraAuthority != null &&
            uri.scheme == "content" && uri.authority == trustedCameraAuthority
        val declaredType = runCatching { resolver.getType(uri)?.lowercase() }.getOrNull()
        val bytes = try {
            resolver.openInputStream(uri)?.use { input ->
                readBoundedImageBytes(
                    input,
                    if (trustedCameraInput) MAX_TRUSTED_CAMERA_BYTES else capability.maxUploadBytes,
                )
            } ?: throw ImageSelectionException(ImageSelectionFailure.UNREADABLE)
        } catch (error: ImageSelectionException) {
            throw error
        } catch (_: SecurityException) {
            throw ImageSelectionException(ImageSelectionFailure.UNREADABLE)
        } catch (_: IOException) {
            throw ImageSelectionException(ImageSelectionFailure.UNREADABLE)
        }
        if (trustedCameraInput) runCatching { resolver.delete(uri, null, null) }
        try {
            val actualType = detectMediaType(bytes)
            if (
                actualType !in capability.mediaTypes ||
                declaredType != null &&
                declaredType != "application/octet-stream" &&
                declaredType != actualType
            ) {
                throw ImageSelectionException(ImageSelectionFailure.UNSUPPORTED)
            }
            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
            val width = bounds.outWidth
            val height = bounds.outHeight
            if (width <= 0 || height <= 0) {
                throw ImageSelectionException(ImageSelectionFailure.INVALID_IMAGE)
            }
            if (trustedCameraInput) {
                if (
                    width > MAX_TRUSTED_CAMERA_EDGE ||
                    height > MAX_TRUSTED_CAMERA_EDGE ||
                    width.toLong() * height.toLong() > MAX_TRUSTED_CAMERA_PIXELS
                ) {
                    throw ImageSelectionException(ImageSelectionFailure.TOO_LARGE)
                }
                val normalized = normalizeCameraInput(
                    bytes = bytes,
                    mediaType = actualType,
                    width = width,
                    height = height,
                    maxUploadBytes = capability.maxUploadBytes,
                )
                bytes.fill(0)
                try {
                    val thumbnail = createThumbnail(
                        normalized.bytes,
                        normalized.mediaType,
                        normalized.width,
                        normalized.height,
                    )
                    return PendingImageAttachment(
                        clientAssetId = UUID.randomUUID().toString(),
                        mediaType = normalized.mediaType,
                        sourceBytes = normalized.bytes,
                        thumbnailBytes = thumbnail,
                        sourceWidth = normalized.width,
                        sourceHeight = normalized.height,
                    )
                } catch (error: Throwable) {
                    normalized.bytes.fill(0)
                    throw error
                }
            } else if (
                width > capability.maxEdgePixels ||
                height > capability.maxEdgePixels ||
                width.toLong() * height.toLong() > capability.maxPixels
            ) {
                throw ImageSelectionException(ImageSelectionFailure.TOO_LARGE)
            }
            val thumbnail = createThumbnail(bytes, actualType, width, height)
            return PendingImageAttachment(
                clientAssetId = UUID.randomUUID().toString(),
                mediaType = actualType,
                sourceBytes = bytes,
                thumbnailBytes = thumbnail,
                sourceWidth = width,
                sourceHeight = height,
            )
        } catch (error: Throwable) {
            bytes.fill(0)
            throw error
        }
    }

    private fun normalizeCameraInput(
        bytes: ByteArray,
        mediaType: String,
        width: Int,
        height: Int,
        maxUploadBytes: Int,
    ): NormalizedCameraImage {
        var sampleSize = 1
        while (
            maxOf(width, height) / sampleSize > MAX_CAMERA_OUTPUT_EDGE ||
            width.toLong() * height.toLong() / sampleSize / sampleSize > MAX_CAMERA_OUTPUT_PIXELS
        ) {
            sampleSize *= 2
        }
        val decoded = BitmapFactory.decodeByteArray(
            bytes,
            0,
            bytes.size,
            BitmapFactory.Options().apply {
                inSampleSize = sampleSize
                inPreferredConfig = Bitmap.Config.ARGB_8888
            },
        ) ?: throw ImageSelectionException(ImageSelectionFailure.INVALID_IMAGE)
        var working = decoded
        try {
            if (mediaType == "image/jpeg") {
                val orientation = runCatching {
                    ExifInterface(ByteArrayInputStream(bytes)).getAttributeInt(
                        ExifInterface.TAG_ORIENTATION,
                        ExifInterface.ORIENTATION_NORMAL,
                    )
                }.getOrDefault(ExifInterface.ORIENTATION_NORMAL)
                val matrix = orientationMatrix(orientation)
                if (!matrix.isIdentity) {
                    working = Bitmap.createBitmap(
                        decoded,
                        0,
                        0,
                        decoded.width,
                        decoded.height,
                        matrix,
                        true,
                    )
                    if (working !== decoded) decoded.recycle()
                }
            }
            val scale = minOf(
                1f,
                MAX_CAMERA_OUTPUT_EDGE.toFloat() / maxOf(working.width, working.height).toFloat(),
            )
            if (scale < 1f) {
                val scaled = Bitmap.createScaledBitmap(
                    working,
                    (working.width * scale).toInt().coerceAtLeast(1),
                    (working.height * scale).toInt().coerceAtLeast(1),
                    true,
                )
                if (scaled !== working) working.recycle()
                working = scaled
            }
            val flattened = Bitmap.createBitmap(working.width, working.height, Bitmap.Config.ARGB_8888)
            try {
                Canvas(flattened).apply {
                    drawColor(Color.WHITE)
                    drawBitmap(working, 0f, 0f, null)
                }
                for (quality in CAMERA_JPEG_QUALITIES) {
                    val output = ByteArrayOutputStream(minOf(maxUploadBytes, 2 * 1024 * 1024))
                    if (!flattened.compress(Bitmap.CompressFormat.JPEG, quality, output)) {
                        throw ImageSelectionException(ImageSelectionFailure.INVALID_IMAGE)
                    }
                    val encoded = output.toByteArray()
                    if (encoded.size <= maxUploadBytes) {
                        return NormalizedCameraImage(
                            bytes = encoded,
                            mediaType = "image/jpeg",
                            width = flattened.width,
                            height = flattened.height,
                        )
                    }
                    encoded.fill(0)
                }
                throw ImageSelectionException(ImageSelectionFailure.TOO_LARGE)
            } finally {
                flattened.recycle()
            }
        } finally {
            if (!working.isRecycled) working.recycle()
            if (!decoded.isRecycled) decoded.recycle()
        }
    }

    private fun createThumbnail(
        bytes: ByteArray,
        mediaType: String,
        width: Int,
        height: Int,
    ): ByteArray {
        var sampleSize = 1
        while (maxOf(width, height) / sampleSize > THUMBNAIL_EDGE) sampleSize *= 2
        val decoded = BitmapFactory.decodeByteArray(
            bytes,
            0,
            bytes.size,
            BitmapFactory.Options().apply {
                inSampleSize = sampleSize
                inPreferredConfig = Bitmap.Config.ARGB_8888
            },
        ) ?: throw ImageSelectionException(ImageSelectionFailure.INVALID_IMAGE)
        var working = decoded
        try {
            if (mediaType == "image/jpeg") {
                val orientation = runCatching {
                    ExifInterface(ByteArrayInputStream(bytes)).getAttributeInt(
                        ExifInterface.TAG_ORIENTATION,
                        ExifInterface.ORIENTATION_NORMAL,
                    )
                }.getOrDefault(ExifInterface.ORIENTATION_NORMAL)
                val matrix = orientationMatrix(orientation)
                if (!matrix.isIdentity) {
                    working = Bitmap.createBitmap(
                        decoded,
                        0,
                        0,
                        decoded.width,
                        decoded.height,
                        matrix,
                        true,
                    )
                    if (working !== decoded) decoded.recycle()
                }
            }
            val scale = minOf(
                1f,
                THUMBNAIL_EDGE.toFloat() / maxOf(working.width, working.height).toFloat(),
            )
            if (scale < 1f) {
                val scaled = Bitmap.createScaledBitmap(
                    working,
                    (working.width * scale).toInt().coerceAtLeast(1),
                    (working.height * scale).toInt().coerceAtLeast(1),
                    true,
                )
                if (scaled !== working) working.recycle()
                working = scaled
            }
            val flattened = Bitmap.createBitmap(working.width, working.height, Bitmap.Config.ARGB_8888)
            try {
                Canvas(flattened).apply {
                    drawColor(Color.WHITE)
                    drawBitmap(working, 0f, 0f, null)
                }
                val output = ByteArrayOutputStream(128 * 1024)
                if (!flattened.compress(Bitmap.CompressFormat.JPEG, 82, output)) {
                    throw ImageSelectionException(ImageSelectionFailure.INVALID_IMAGE)
                }
                return output.toByteArray().also {
                    if (it.size > MAX_THUMBNAIL_BYTES) {
                        it.fill(0)
                        throw ImageSelectionException(ImageSelectionFailure.TOO_LARGE)
                    }
                }
            } finally {
                flattened.recycle()
            }
        } finally {
            if (!working.isRecycled) working.recycle()
            if (!decoded.isRecycled) decoded.recycle()
        }
    }

    private fun detectMediaType(bytes: ByteArray): String = when {
        bytes.size >= PNG_SIGNATURE.size + PNG_IEND.size &&
            bytes.copyOfRange(0, PNG_SIGNATURE.size).contentEquals(PNG_SIGNATURE) &&
            bytes.hasSuffix(PNG_IEND) ->
            "image/png"
        bytes.size >= 4 &&
            bytes[0] == 0xff.toByte() &&
            bytes[1] == 0xd8.toByte() &&
            bytes[bytes.lastIndex - 1] == 0xff.toByte() &&
            bytes[bytes.lastIndex] == 0xd9.toByte() -> "image/jpeg"
        else -> throw ImageSelectionException(ImageSelectionFailure.UNSUPPORTED)
    }

    private fun ByteArray.hasSuffix(suffix: ByteArray): Boolean {
        if (size < suffix.size) return false
        val offset = size - suffix.size
        return suffix.indices.all { index -> this[offset + index] == suffix[index] }
    }

    private fun orientationMatrix(orientation: Int): Matrix = Matrix().apply {
        when (orientation) {
            ExifInterface.ORIENTATION_FLIP_HORIZONTAL -> setScale(-1f, 1f)
            ExifInterface.ORIENTATION_ROTATE_180 -> setRotate(180f)
            ExifInterface.ORIENTATION_FLIP_VERTICAL -> setScale(1f, -1f)
            ExifInterface.ORIENTATION_TRANSPOSE -> {
                setRotate(90f)
                postScale(-1f, 1f)
            }
            ExifInterface.ORIENTATION_ROTATE_90 -> setRotate(90f)
            ExifInterface.ORIENTATION_TRANSVERSE -> {
                setRotate(-90f)
                postScale(-1f, 1f)
            }
            ExifInterface.ORIENTATION_ROTATE_270 -> setRotate(-90f)
        }
    }

    private companion object {
        const val THUMBNAIL_EDGE = 512
        const val MAX_THUMBNAIL_BYTES = 1024 * 1024
        const val MAX_TRUSTED_CAMERA_BYTES = 32 * 1024 * 1024
        const val MAX_TRUSTED_CAMERA_EDGE = 20_000
        const val MAX_TRUSTED_CAMERA_PIXELS = 64_000_000L
        const val MAX_CAMERA_OUTPUT_EDGE = 3_072
        const val MAX_CAMERA_OUTPUT_PIXELS = 10_000_000L
        val CAMERA_JPEG_QUALITIES = intArrayOf(90, 82, 74)
        val PNG_SIGNATURE = byteArrayOf(
            0x89.toByte(), 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
        )
        val PNG_IEND = byteArrayOf(
            0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4e, 0x44,
            0xae.toByte(), 0x42, 0x60, 0x82.toByte(),
        )
    }

    private data class NormalizedCameraImage(
        val bytes: ByteArray,
        val mediaType: String,
        val width: Int,
        val height: Int,
    )
}

/** Reads no more than [maxUploadBytes] + 1 bytes, including from overlong providers. */
internal suspend fun readBoundedImageBytes(
    input: InputStream,
    maxUploadBytes: Int,
): ByteArray {
    if (maxUploadBytes <= 0) throw ImageSelectionException(ImageSelectionFailure.TOO_LARGE)
    val output = WipeableByteArrayOutputStream(minOf(maxUploadBytes, 64 * 1024))
    val buffer = ByteArray(32 * 1024)
    val maximumProbeBytes = maxUploadBytes.toLong() + 1L
    var total = 0L
    try {
        while (total < maximumProbeBytes) {
            currentCoroutineContext().ensureActive()
            val remaining = maximumProbeBytes - total
            val requested = minOf(buffer.size.toLong(), remaining).toInt()
            val read = input.read(buffer, 0, requested)
            if (read < 0) break
            if (read == 0) continue
            if (read > requested) throw IOException("InputStream exceeded requested byte count")
            total += read
            if (total > maxUploadBytes.toLong()) {
                throw ImageSelectionException(ImageSelectionFailure.TOO_LARGE)
            }
            output.write(buffer, 0, read)
        }
        return output.toByteArray()
    } finally {
        buffer.fill(0)
        output.wipe()
    }
}

private class WipeableByteArrayOutputStream(initialSize: Int) : ByteArrayOutputStream(initialSize) {
    fun wipe() {
        buf.fill(0)
        reset()
    }
}

internal fun Iterable<PendingImageAttachment>.wipeImageBytes() {
    forEach { image ->
        image.sourceBytes.fill(0)
        image.thumbnailBytes.fill(0)
    }
}
