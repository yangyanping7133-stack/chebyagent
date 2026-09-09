package com.cheby.codex.mobile.ui

import java.io.InputStream
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class BoundedImageReadTest {
    @Test
    fun `overlong stream is read through only limit plus one byte`() = runTest {
        val limit = 64 * 1024
        val stream = CountingInputStream(limit + 8 * 1024)

        val failure = runCatching {
            readBoundedImageBytes(stream, limit)
        }.exceptionOrNull()

        assertTrue(failure is ImageSelectionException)
        assertEquals(ImageSelectionFailure.TOO_LARGE, (failure as ImageSelectionException).reason)
        assertEquals(limit + 1, stream.bytesRead)
        assertEquals(1, stream.requestSizes.last())
        assertTrue(stream.requestSizes.all { it in 1..32 * 1024 })
    }

    @Test
    fun `stream at exact limit performs only a one byte eof probe`() = runTest {
        val limit = 64 * 1024
        val stream = CountingInputStream(limit)

        val bytes = readBoundedImageBytes(stream, limit)

        assertEquals(limit, bytes.size)
        assertEquals(limit, stream.bytesRead)
        assertEquals(1, stream.requestSizes.last())
        bytes.fill(0)
    }

    private class CountingInputStream(private val availableBytes: Int) : InputStream() {
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
