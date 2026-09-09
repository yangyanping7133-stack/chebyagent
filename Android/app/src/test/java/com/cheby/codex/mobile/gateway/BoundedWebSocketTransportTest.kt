package com.cheby.codex.mobile.gateway

import java.io.ByteArrayInputStream
import java.io.BufferedOutputStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketTimeoutException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import javax.net.ssl.SSLSocketFactory
import kotlin.concurrent.thread
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.SocketPolicy
import okhttp3.tls.HandshakeCertificates
import okhttp3.tls.HeldCertificate
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class BoundedWebSocketTransportTest {
    private val deviceProofKey = TestDeviceProofKey.generate()

    @Test
    fun `oversized frame is rejected from header before payload allocation or read`() {
        val announcedLength = 5L * 1024 * 1024
        val header = byteArrayOf(0x81.toByte(), 0x7f) + longBytes(announcedLength)
        val input = TrackingInputStream(header)

        expect<WebSocketFrameTooLargeException> {
            BoundedWebSocketFrameReader(input, 512 * 1024).readNext()
        }

        assertEquals(header.size, input.bytesRead)
        assertEquals(0, input.bulkReads)
    }

    @Test
    fun `compressed frame is rejected after two header bytes`() {
        val input = TrackingInputStream(byteArrayOf(0xC1.toByte(), 0x01, 'x'.code.toByte()))

        expect<InvalidWebSocketFrameException> {
            BoundedWebSocketFrameReader(input, 512 * 1024).readNext()
        }

        assertEquals(2, input.bytesRead)
        assertEquals(0, input.bulkReads)
    }

    @Test
    fun `fragmented text aggregate is rejected before second payload read`() {
        val firstPayload = ByteArray(300 * 1024) { 'a'.code.toByte() }
        val secondLength = 300 * 1024
        val bytes = frameHeader(final = false, opcode = 1, firstPayload.size.toLong()) + firstPayload +
            frameHeader(final = true, opcode = 0, secondLength.toLong())
        val input = TrackingInputStream(bytes)

        expect<WebSocketFrameTooLargeException> {
            BoundedWebSocketFrameReader(input, 512 * 1024).readNext()
        }

        assertEquals(bytes.size, input.bytesRead)
        assertEquals(1, input.bulkReads)
    }

    @Test
    fun `valid fragmented UTF8 text is reconstructed exactly`() {
        val bytes = frameHeader(final = false, opcode = 1, length = 3) + "你".toByteArray() +
            frameHeader(final = true, opcode = 0, length = 3) + "好".toByteArray()

        val frame = BoundedWebSocketFrameReader(ByteArrayInputStream(bytes), 32).readNext()

        assertTrue(frame is RawSocketFrame.Text)
        assertEquals("你好", (frame as RawSocketFrame.Text).value)
    }

    @Test(timeout = 3_000)
    fun `half open TLS handshake is bounded`() {
        val server = ServerSocket(0)
        val accepted = CountDownLatch(1)
        val release = CountDownLatch(1)
        val serverThread = thread(name = "half-open-tls") {
            runCatching {
                server.accept().use {
                    accepted.countDown()
                    release.await(2, TimeUnit.SECONDS)
                }
            }
        }
        val connection = BoundedWebSocketConnection(
            sslSocketFactory = SSLSocketFactory.getDefault() as SSLSocketFactory,
            maxFrameBytes = 512 * 1024,
            connectTimeoutMillis = 500,
            handshakeTimeoutMillis = 150,
            readTimeoutMillis = 300,
        )

        val failure = runCatching {
            connection.connect(
                "https://localhost:${server.localPort}/events".toHttpUrl(),
                "safe-token",
                deviceProofKey,
            )
        }.exceptionOrNull()

        assertTrue(accepted.await(1, TimeUnit.SECONDS))
        assertTrue(failure is SocketTimeoutException)
        connection.cancel()
        release.countDown()
        server.close()
        serverThread.join(1_000)
    }

    @Test(timeout = 3_000)
    fun `HTTP upgrade and ongoing reads each have socket deadlines`() {
        val fixture = tlsFixture()
        fixture.server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.NO_RESPONSE))
        val upgradeConnection = fixture.connection(handshakeTimeoutMillis = 150, readTimeoutMillis = 300)
        val upgradeFailure = runCatching {
            upgradeConnection.connect(fixture.server.url("/events"), "safe-token", deviceProofKey)
        }.exceptionOrNull()
        assertTrue(upgradeFailure is SocketTimeoutException)
        upgradeConnection.cancel()
        fixture.server.shutdown()

        val readFixture = tlsFixture()
        readFixture.server.enqueue(
            MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: okhttp3.Response) = Unit
            }),
        )
        val readConnection = readFixture.connection(handshakeTimeoutMillis = 1_000, readTimeoutMillis = 150)
        readConnection.connect(readFixture.server.url("/events"), "safe-token", deviceProofKey)

        val readFailure = runCatching { readConnection.readEvent() }.exceptionOrNull()

        assertTrue(readFailure is SocketTimeoutException)
        readConnection.cancel()
        readFixture.server.shutdown()
    }

    @Test(timeout = 3_000)
    fun `pong deadline closes transport even when peer remains otherwise open`() {
        val fixture = tlsFixture()
        fixture.server.enqueue(
            MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: okhttp3.Response) = Unit
            }),
        )
        val connection = fixture.connection(handshakeTimeoutMillis = 1_000, readTimeoutMillis = 2_000)
        connection.connect(fixture.server.url("/events"), "safe-token", deviceProofKey)

        val challenge = connection.preparePing()
        assertNotNull(challenge)
        assertTrue(connection.sendPing(requireNotNull(challenge)))
        assertTrue(connection.expirePong(requireNotNull(challenge)))
        val failure = runCatching { connection.readEvent() }.exceptionOrNull()

        assertTrue(failure is WebSocketPongTimeoutException)
        fixture.server.shutdown()
    }

    @Test(timeout = 3_000)
    fun `cancel interrupts blocked ACK writer and close fallback never waits for its lock`() {
        val blockedOutput = BlockingOutputStream()
        val connection = BoundedWebSocketConnection(
            sslSocketFactory = SSLSocketFactory.getDefault() as SSLSocketFactory,
            maxFrameBytes = 64 * 1024,
        )
        connection.setPrivateField("output", BufferedOutputStream(blockedOutput, 8))
        connection.socketReference().set(object : Socket() {
            override fun close() = blockedOutput.release()
        })
        val result = AtomicReference<Boolean?>()
        val writer = thread(name = "blocked-websocket-writer") {
            result.set(connection.sendText("""{"type":"ack","streamId":"stream","seq":1}"""))
        }
        assertTrue(blockedOutput.entered.await(1, TimeUnit.SECONDS))

        val startedAt = System.nanoTime()
        connection.cancel()
        connection.bestEffortCloseFrameAfterCancellation(1000, "cancelled")
        val elapsedMillis = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - startedAt)
        writer.join(1_000)

        assertFalse(writer.isAlive)
        assertEquals(false, result.get())
        assertTrue("Cancellation took ${elapsedMillis}ms", elapsedMillis < 500)
    }

    @Test(timeout = 3_000)
    fun `independent pong watchdog interrupts a blocked ping flush`() {
        val blockedOutput = BlockingOutputStream()
        val connection = BoundedWebSocketConnection(
            sslSocketFactory = SSLSocketFactory.getDefault() as SSLSocketFactory,
            maxFrameBytes = 64 * 1024,
        )
        connection.setPrivateField("output", BufferedOutputStream(blockedOutput, 8))
        connection.socketReference().set(object : Socket() {
            override fun close() = blockedOutput.release()
        })
        val challenge = requireNotNull(connection.preparePing())
        val sendResult = AtomicReference<Boolean?>()
        val writer = thread(name = "blocked-ping-writer") {
            sendResult.set(connection.sendPing(challenge))
        }
        assertTrue(blockedOutput.entered.await(1, TimeUnit.SECONDS))

        val expired = AtomicReference<Boolean?>()
        val watchdog = thread(name = "independent-pong-watchdog") {
            expired.set(connection.expirePong(challenge))
        }
        watchdog.join(1_000)
        writer.join(1_000)

        assertEquals(true, expired.get())
        assertEquals(false, sendResult.get())
        assertFalse(writer.isAlive)
        assertTrue(runCatching { connection.readEvent() }.exceptionOrNull() is WebSocketPongTimeoutException)
    }

    private fun frameHeader(final: Boolean, opcode: Int, length: Long): ByteArray {
        val first = (if (final) 0x80 else 0) or opcode
        return when {
            length <= 125 -> byteArrayOf(first.toByte(), length.toByte())
            length <= 0xffff -> byteArrayOf(
                first.toByte(),
                126,
                (length ushr 8).toByte(),
                length.toByte(),
            )
            else -> byteArrayOf(first.toByte(), 127) + longBytes(length)
        }
    }

    private fun longBytes(value: Long): ByteArray = ByteArray(8) { index ->
        (value ushr ((7 - index) * 8)).toByte()
    }

    private inline fun <reified T : Throwable> expect(block: () -> Unit): T {
        try {
            block()
        } catch (error: Throwable) {
            if (error is T) return error
            throw error
        }
        fail("Expected ${T::class.java.simpleName}")
        throw AssertionError("unreachable")
    }

    private fun tlsFixture(): TlsFixture {
        val certificate = HeldCertificate.Builder()
            .commonName("localhost")
            .addSubjectAlternativeName("localhost")
            .build()
        val serverCertificates = HandshakeCertificates.Builder().heldCertificate(certificate).build()
        val clientCertificates = HandshakeCertificates.Builder()
            .addTrustedCertificate(certificate.certificate)
            .build()
        val server = MockWebServer().apply {
            useHttps(serverCertificates.sslSocketFactory(), false)
            start()
        }
        return TlsFixture(server, clientCertificates.sslSocketFactory())
    }

    private data class TlsFixture(
        val server: MockWebServer,
        val sslSocketFactory: SSLSocketFactory,
    ) {
        fun connection(
            handshakeTimeoutMillis: Int,
            readTimeoutMillis: Int,
        ) = BoundedWebSocketConnection(
            sslSocketFactory = sslSocketFactory,
            maxFrameBytes = 512 * 1024,
            connectTimeoutMillis = 1_000,
            handshakeTimeoutMillis = handshakeTimeoutMillis,
            readTimeoutMillis = readTimeoutMillis,
        )
    }

    private fun BoundedWebSocketConnection.setPrivateField(name: String, value: Any) {
        javaClass.getDeclaredField(name).apply { isAccessible = true }.set(this, value)
    }

    @Suppress("UNCHECKED_CAST")
    private fun BoundedWebSocketConnection.socketReference(): AtomicReference<Socket?> =
        javaClass.getDeclaredField("socket")
            .apply { isAccessible = true }
            .get(this) as AtomicReference<Socket?>

    private class BlockingOutputStream : OutputStream() {
        val entered = CountDownLatch(1)
        private val released = CountDownLatch(1)

        override fun write(value: Int) = blockUntilCancelled()

        override fun write(buffer: ByteArray, offset: Int, length: Int) = blockUntilCancelled()

        fun release() {
            released.countDown()
        }

        private fun blockUntilCancelled() {
            entered.countDown()
            released.await(2, TimeUnit.SECONDS)
            throw IOException("closed")
        }
    }

    private class TrackingInputStream(private val bytes: ByteArray) : InputStream() {
        var bytesRead: Int = 0
            private set
        var bulkReads: Int = 0
            private set

        override fun read(): Int = if (bytesRead < bytes.size) {
            bytes[bytesRead++].toInt() and 0xff
        } else {
            -1
        }

        override fun read(target: ByteArray, offset: Int, length: Int): Int {
            bulkReads += 1
            if (bytesRead >= bytes.size) return -1
            val count = minOf(length, bytes.size - bytesRead)
            bytes.copyInto(target, offset, bytesRead, bytesRead + count)
            bytesRead += count
            return count
        }
    }
}
