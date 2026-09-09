package com.cheby.codex.mobile.gateway

import java.nio.charset.StandardCharsets
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.ResponseBody
import okio.Buffer
import okio.BufferedSource
import okio.ForwardingSource
import okio.buffer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class BoundedTransportBodyTest {
    @Test
    fun `oversized HTTP response fails closed and releases body`() {
        val body = TrackingBody("123456".toByteArray())
        val response = response(body)

        val failure = runCatching { response.consumeUtf8BodyBounded(5, null) }.exceptionOrNull()

        assertTrue(failure is GatewayTransportException)
        assertTrue(body.closed)
    }

    @Test
    fun `oversized chunked HTTP response also fails closed`() {
        val body = TrackingBody("123456".toByteArray(), reportLength = false)

        val failure = runCatching { response(body).consumeUtf8BodyBounded(5, null) }.exceptionOrNull()

        assertTrue(failure is GatewayTransportException)
        assertTrue(body.closed)
    }

    @Test
    fun `malformed UTF8 response fails closed and releases body`() {
        val body = TrackingBody(byteArrayOf(0xC3.toByte(), 0x28))

        val failure = runCatching { response(body).consumeUtf8BodyBounded(16, null) }.exceptionOrNull()

        assertTrue(failure is GatewayTransportException)
        assertTrue(body.closed)
    }

    @Test
    fun `bounded response accepts exact UTF8 byte budget`() {
        val body = TrackingBody("界界".toByteArray(StandardCharsets.UTF_8))

        assertEquals("界界", response(body).consumeUtf8BodyBounded(6, null))
        assertTrue(body.closed)
    }

    @Test
    fun `websocket cap counts UTF8 bytes not UTF16 characters`() {
        assertFalse("界界".exceedsUtf8Limit(6))
        assertTrue("界界".exceedsUtf8Limit(5))
    }

    @Test
    fun `transport budgets keep history separate from control frames`() {
        assertEquals(4 * 1024 * 1024, MAX_THREAD_DETAIL_RESPONSE_BYTES)
        assertEquals(2 * 1024 * 1024, MAX_CONTROL_RESPONSE_BYTES)
        assertEquals(64 * 1024, MAX_PAIRING_RESPONSE_BYTES)
        assertEquals(512 * 1024, MAX_WEBSOCKET_FRAME_BYTES)
        assertTrue(MAX_THREAD_DETAIL_RESPONSE_BYTES > MAX_CONTROL_RESPONSE_BYTES)
    }

    @Test
    fun `thread detail accepts exactly four MiB`() {
        val bytes = ByteArray(MAX_THREAD_DETAIL_RESPONSE_BYTES) { 'a'.code.toByte() }
        val body = TrackingBody(bytes)

        val decoded = response(body).consumeUtf8BodyBounded(MAX_THREAD_DETAIL_RESPONSE_BYTES, null)

        assertEquals(MAX_THREAD_DETAIL_RESPONSE_BYTES, decoded?.length)
        assertTrue(body.sourceAccessed)
        assertTrue(body.closed)
    }

    @Test
    fun `declared thread detail over four MiB is rejected before any body read`() {
        val body = TrackingBody(
            bytes = ByteArray(0),
            reportedLength = MAX_THREAD_DETAIL_RESPONSE_BYTES.toLong() + 1,
        )

        val failure = runCatching {
            response(body).consumeUtf8BodyBounded(MAX_THREAD_DETAIL_RESPONSE_BYTES, null)
        }.exceptionOrNull()

        assertTrue(failure is GatewayTransportException)
        assertFalse(body.readAttempted)
        assertTrue(body.closed)
    }

    private fun response(body: ResponseBody): Response = Response.Builder()
        .request(Request.Builder().url("https://gateway.example/v1/test").build())
        .protocol(Protocol.HTTP_1_1)
        .code(200)
        .message("OK")
        .body(body)
        .build()

    private class TrackingBody(
        bytes: ByteArray,
        private val reportLength: Boolean = true,
        private val reportedLength: Long? = null,
    ) : ResponseBody() {
        private val buffer = Buffer().write(bytes)
        private val trackingSource = object : ForwardingSource(buffer) {
            override fun read(sink: Buffer, byteCount: Long): Long {
                readAttempted = true
                return super.read(sink, byteCount)
            }
        }.buffer()
        var closed = false
            private set
        var sourceAccessed = false
            private set
        var readAttempted = false
            private set

        override fun contentType() = "application/json; charset=utf-8".toMediaType()
        override fun contentLength(): Long = reportedLength ?: if (reportLength) buffer.size else -1
        override fun source(): BufferedSource {
            sourceAccessed = true
            return trackingSource
        }
        override fun close() {
            closed = true
            super.close()
        }
    }
}
