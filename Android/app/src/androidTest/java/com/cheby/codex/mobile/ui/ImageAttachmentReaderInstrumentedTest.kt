package com.cheby.codex.mobile.ui

import android.Manifest
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Color
import android.net.Uri
import androidx.core.content.FileProvider
import androidx.exifinterface.media.ExifInterface
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.cheby.codex.mobile.gateway.LocalImageCapability
import java.io.File
import java.io.FileOutputStream
import java.io.InputStream
import java.io.ByteArrayInputStream
import java.io.RandomAccessFile
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class ImageAttachmentReaderInstrumentedTest {
    @Test
    fun jpegSelectionReadsBoundedBytesAndAppliesExifToThumbnail() = runBlocking {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val source = File(context.cacheDir, "image-reader-orientation.jpg")
        val bitmap = Bitmap.createBitmap(120, 60, Bitmap.Config.ARGB_8888).apply {
            eraseColor(Color.BLUE)
        }
        try {
            FileOutputStream(source).use { output ->
                assertTrue(bitmap.compress(Bitmap.CompressFormat.JPEG, 90, output))
            }
            ExifInterface(source.absolutePath).apply {
                setAttribute(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_ROTATE_90.toString())
                saveAttributes()
            }
            val selected = AndroidImageAttachmentReader(context.contentResolver).read(
                listOf(Uri.fromFile(source).toString()),
                capability(),
            ).single()
            val thumbnail = BitmapFactory.decodeByteArray(
                selected.thumbnailBytes,
                0,
                selected.thumbnailBytes.size,
            )
            try {
                assertEquals("image/jpeg", selected.mediaType)
                assertEquals(120, selected.sourceWidth)
                assertEquals(60, selected.sourceHeight)
                assertEquals(60, thumbnail.width)
                assertEquals(120, thumbnail.height)
                assertTrue(selected.sourceBytes.contentEquals(source.readBytes()))
            } finally {
                thumbnail.recycle()
                listOf(selected).wipeImageBytes()
            }
        } finally {
            bitmap.recycle()
            source.delete()
        }
    }

    @Test
    fun trustedCameraCaptureIsRotatedReencodedAndMetadataFree() = runBlocking {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val directory = File(context.cacheDir, "camera-inputs").apply { mkdirs() }
        val source = File(directory, "camera-normalization.jpg")
        val bitmap = Bitmap.createBitmap(160, 80, Bitmap.Config.ARGB_8888).apply {
            eraseColor(Color.BLUE)
        }
        try {
            FileOutputStream(source).use { output ->
                assertTrue(bitmap.compress(Bitmap.CompressFormat.JPEG, 95, output))
            }
            ExifInterface(source.absolutePath).apply {
                setAttribute(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_ROTATE_90.toString())
                setLatLong(59.93, 30.31)
                saveAttributes()
            }
            val authority = "${context.packageName}.fileprovider"
            val uri = FileProvider.getUriForFile(context, authority, source)
            val originalBytes = source.readBytes()
            val selected = AndroidImageAttachmentReader(
                resolver = context.contentResolver,
                trustedCameraAuthority = authority,
            ).read(listOf(uri.toString()), capability()).single()
            val metadata = ExifInterface(ByteArrayInputStream(selected.sourceBytes))

            assertEquals("image/jpeg", selected.mediaType)
            assertEquals(80, selected.sourceWidth)
            assertEquals(160, selected.sourceHeight)
            assertTrue(selected.sourceBytes.size <= capability().maxUploadBytes)
            assertFalse(selected.sourceBytes.contentEquals(originalBytes))
            assertFalse(source.exists())
            assertEquals(null, metadata.latLong)
            assertEquals(
                ExifInterface.ORIENTATION_UNDEFINED,
                metadata.getAttributeInt(
                    ExifInterface.TAG_ORIENTATION,
                    ExifInterface.ORIENTATION_UNDEFINED,
                ),
            )
            originalBytes.fill(0)
            listOf(selected).wipeImageBytes()
        } finally {
            bitmap.recycle()
            source.delete()
        }
    }

    @Test
    fun streamStopsAtEightMiBPlusOneAndManifestAddsNoStoragePermission() = runBlocking {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val source = File(context.cacheDir, "image-reader-too-large.bin")
        try {
            RandomAccessFile(source, "rw").use { it.setLength(8L * 1024 * 1024 + 1) }
            val failure = runCatching {
                AndroidImageAttachmentReader(context.contentResolver).read(
                    listOf(Uri.fromFile(source).toString()),
                    capability(),
                )
            }.exceptionOrNull()
            assertTrue(failure is ImageSelectionException)
            assertEquals(ImageSelectionFailure.TOO_LARGE, (failure as ImageSelectionException).reason)

            val permissions = context.packageManager.getPackageInfo(
                context.packageName,
                PackageManager.GET_PERMISSIONS,
            ).requestedPermissions.orEmpty().toSet()
            assertTrue(Manifest.permission.INTERNET in permissions)
            assertFalse(Manifest.permission.READ_EXTERNAL_STORAGE in permissions)
            assertFalse(Manifest.permission.WRITE_EXTERNAL_STORAGE in permissions)
            assertFalse(Manifest.permission.READ_MEDIA_IMAGES in permissions)
        } finally {
            source.delete()
        }
    }

    @Test
    fun overlongProviderStreamIsNeverReadPastEightMiBPlusOne() = runBlocking {
        val limit = 8 * 1024 * 1024
        val stream = CountingProviderStream(limit + 128 * 1024)

        val failure = runCatching {
            readBoundedImageBytes(stream, limit)
        }.exceptionOrNull()

        assertTrue(failure is ImageSelectionException)
        assertEquals(ImageSelectionFailure.TOO_LARGE, (failure as ImageSelectionException).reason)
        assertEquals(limit + 1, stream.bytesRead)
        assertEquals(1, stream.requestSizes.last())
        assertTrue(stream.requestSizes.all { it in 1..32 * 1024 })
    }

    private fun capability() = LocalImageCapability(
        uploadVersion = 1,
        mediaTypes = setOf("image/jpeg", "image/png"),
        maxUploadBytes = 8 * 1024 * 1024,
        maxPixels = 25_000_000,
        maxEdgePixels = 12_000,
        maxImagesPerTurn = 10,
    )

    private class CountingProviderStream(private val availableBytes: Int) : InputStream() {
        var bytesRead: Int = 0
            private set
        val requestSizes = mutableListOf<Int>()

        override fun read(): Int = if (bytesRead < availableBytes) {
            bytesRead += 1
            0
        } else {
            -1
        }

        override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
            requestSizes += length
            if (bytesRead >= availableBytes) return -1
            val count = minOf(length, availableBytes - bytesRead)
            buffer.fill(0, offset, offset + count)
            bytesRead += count
            return count
        }
    }
}
