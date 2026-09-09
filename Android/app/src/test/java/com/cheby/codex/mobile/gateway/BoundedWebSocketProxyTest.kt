package com.cheby.codex.mobile.gateway

import java.io.ByteArrayOutputStream
import java.io.Closeable
import java.io.IOException
import java.net.InetSocketAddress
import java.net.Proxy
import java.net.ProxySelector
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketAddress
import java.net.URI
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import javax.net.ssl.SSLSocketFactory
import kotlin.concurrent.thread
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.tls.HandshakeCertificates
import okhttp3.tls.HeldCertificate
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class BoundedWebSocketProxyTest {
    private val deviceProofKey = TestDeviceProofKey.generate()

    @Test(timeout = 5_000)
    fun `HTTP CONNECT tunnel succeeds without exposing credentials to proxy`() {
        val fixture = relayTlsFixture()
        fixture.server.enqueue(webSocketUpgrade())
        val proxy = RecordingHttpProxy.tunnelTo(fixture.server.port)
        val token = "proxy-must-never-see-this-token"
        val connection = connection(fixture.sslSocketFactory, proxy.selector())
        try {
            connection.connect(RELAY_URL, token, deviceProofKey)

            val connectRequest = proxy.awaitRequest()
            assertEquals("CONNECT relay.example:443 HTTP/1.1", connectRequest.lineSequence().first())
            assertTrue(connectRequest.contains("Host: relay.example:443\r\n"))
            assertFalse(connectRequest.contains(token))
            assertFalse(connectRequest.contains("Authorization:", ignoreCase = true))
            assertFalse(connectRequest.contains("X-Cheby-", ignoreCase = true))

            val relayRequest = fixture.server.takeRequest(2, TimeUnit.SECONDS)
            assertNotNull(relayRequest)
            assertEquals("Bearer $token", relayRequest?.getHeader("Authorization"))
            assertEquals(DeviceProofV1.VERSION, relayRequest?.getHeader(DeviceProofV1.HEADER_VERSION))
            assertNotNull(relayRequest?.getHeader(DeviceProofV1.HEADER_SIGNATURE))
        } finally {
            connection.cancel()
            proxy.close()
            fixture.server.shutdown()
        }
    }

    @Test(timeout = 5_000)
    fun `HTTP CONNECT preserves custom TLS port without resolving relay host locally`() {
        val fixture = relayTlsFixture()
        fixture.server.enqueue(webSocketUpgrade())
        val proxy = RecordingHttpProxy.tunnelTo(fixture.server.port)
        val token = "custom-port-secret-token"
        val directResolutionCalls = AtomicInteger()
        val connection = connection(
            sslSocketFactory = fixture.sslSocketFactory,
            proxySelector = proxy.selector(),
            directAddressFactory = { _, _ ->
                directResolutionCalls.incrementAndGet()
                error("Proxy routes must not resolve the relay host locally")
            },
        )
        try {
            connection.connect(CUSTOM_PORT_RELAY_URL, token, deviceProofKey)

            assertEquals(0, directResolutionCalls.get())
            val connectRequest = proxy.awaitRequest()
            assertEquals("CONNECT relay.example:8443 HTTP/1.1", connectRequest.lineSequence().first())
            assertTrue(connectRequest.contains("Host: relay.example:8443\r\n"))
            assertFalse(connectRequest.contains(token))
            assertFalse(connectRequest.contains("Authorization:", ignoreCase = true))
            assertFalse(connectRequest.contains("X-Cheby-", ignoreCase = true))

            val relayRequest = fixture.server.takeRequest(2, TimeUnit.SECONDS)
            assertNotNull(relayRequest)
            assertEquals("relay.example:8443", relayRequest?.getHeader("Host"))
            assertEquals("Bearer $token", relayRequest?.getHeader("Authorization"))
            assertNotNull(relayRequest?.getHeader(DeviceProofV1.HEADER_SIGNATURE))
        } finally {
            connection.cancel()
            proxy.close()
            fixture.server.shutdown()
        }
    }

    @Test(timeout = 3_000)
    fun `proxy path rejects a non TLS target before proxy selection or DNS`() {
        val selectorCalls = AtomicInteger()
        val directResolutionCalls = AtomicInteger()
        val selector = object : ProxySelector() {
            override fun select(uri: URI): List<Proxy> {
                selectorCalls.incrementAndGet()
                return listOf(Proxy.NO_PROXY)
            }

            override fun connectFailed(uri: URI, socketAddress: SocketAddress, error: IOException) = Unit
        }
        val connection = connection(
            sslSocketFactory = SSLSocketFactory.getDefault() as SSLSocketFactory,
            proxySelector = selector,
            directAddressFactory = { _, _ ->
                directResolutionCalls.incrementAndGet()
                error("Invalid targets must not reach DNS")
            },
        )

        val failure = runCatching {
            connection.connect("http://relay.example:8443/relay/v1/device".toHttpUrl(), "safe-token", deviceProofKey)
        }.exceptionOrNull()

        assertTrue(failure is IOException)
        assertEquals(0, selectorCalls.get())
        assertEquals(0, directResolutionCalls.get())
        connection.cancel()
    }

    @Test(timeout = 3_000)
    fun `HTTP proxy rejection is terminal and contains no credentials`() {
        val proxy = RecordingHttpProxy.respond("HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
        val token = "rejected-secret-token"
        val connection = connection(SSLSocketFactory.getDefault() as SSLSocketFactory, proxy.selector())
        try {
            val failure = runCatching { connection.connect(RELAY_URL, token, deviceProofKey) }.exceptionOrNull()

            assertTrue(failure is HttpProxyConnectException)
            assertEquals(407, (failure as HttpProxyConnectException).statusCode)
            val connectRequest = proxy.awaitRequest()
            assertFalse(connectRequest.contains(token))
            assertFalse(connectRequest.contains("Authorization:", ignoreCase = true))
            assertFalse(connectRequest.contains("X-Cheby-", ignoreCase = true))
        } finally {
            connection.cancel()
            proxy.close()
        }
    }

    @Test(timeout = 3_000)
    fun `oversized HTTP proxy response is rejected without exposing credentials`() {
        val oversized = "HTTP/1.1 200 Connection Established\r\nX-Fill: " + "a".repeat(17 * 1024)
        val proxy = RecordingHttpProxy.respond(oversized)
        val token = "overlimit-secret-token"
        val connection = connection(SSLSocketFactory.getDefault() as SSLSocketFactory, proxy.selector())
        try {
            val failure = runCatching { connection.connect(RELAY_URL, token, deviceProofKey) }.exceptionOrNull()

            assertTrue(failure is HttpProxyResponseTooLargeException)
            val connectRequest = proxy.awaitRequest()
            assertFalse(connectRequest.contains(token))
            assertFalse(connectRequest.contains("Authorization:", ignoreCase = true))
            assertFalse(connectRequest.contains("X-Cheby-", ignoreCase = true))
        } finally {
            connection.cancel()
            proxy.close()
        }
    }

    @Test(timeout = 3_000)
    fun `loopback fixtures bypass the process proxy selector`() {
        val fixture = localhostTlsFixture()
        fixture.server.enqueue(webSocketUpgrade())
        val selectorCalls = AtomicInteger()
        val rejectingSelector = object : ProxySelector() {
            override fun select(uri: URI): List<Proxy> {
                selectorCalls.incrementAndGet()
                error("Loopback must not query the process proxy")
            }

            override fun connectFailed(uri: URI, socketAddress: SocketAddress, error: IOException) = Unit
        }
        val connection = connection(fixture.sslSocketFactory, rejectingSelector)
        try {
            connection.connect(fixture.server.url("/events"), "safe-token", deviceProofKey)

            assertEquals(0, selectorCalls.get())
            assertNotNull(fixture.server.takeRequest(1, TimeUnit.SECONDS))
        } finally {
            connection.cancel()
            fixture.server.shutdown()
        }
    }

    private fun connection(
        sslSocketFactory: SSLSocketFactory,
        proxySelector: ProxySelector,
        directAddressFactory: (String, Int) -> InetSocketAddress = { host, port ->
            InetSocketAddress(host, port)
        },
    ) = BoundedWebSocketConnection(
        sslSocketFactory = sslSocketFactory,
        maxFrameBytes = 512 * 1024,
        connectTimeoutMillis = 1_000,
        handshakeTimeoutMillis = 1_000,
        readTimeoutMillis = 1_000,
        proxySelectorProvider = { proxySelector },
        directAddressFactory = directAddressFactory,
    )

    private fun relayTlsFixture(): TlsFixture = tlsFixture("relay.example")

    private fun localhostTlsFixture(): TlsFixture = tlsFixture("localhost")

    private fun tlsFixture(hostname: String): TlsFixture {
        val certificate = HeldCertificate.Builder()
            .commonName(hostname)
            .addSubjectAlternativeName(hostname)
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

    private fun webSocketUpgrade() = MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: okhttp3.Response) = Unit
    })

    private data class TlsFixture(
        val server: MockWebServer,
        val sslSocketFactory: SSLSocketFactory,
    )

    private class FixedProxySelector(private val proxy: Proxy) : ProxySelector() {
        override fun select(uri: URI): List<Proxy> = listOf(proxy)

        override fun connectFailed(uri: URI, socketAddress: SocketAddress, error: IOException) = Unit
    }

    private class RecordingHttpProxy private constructor(
        private val response: String,
        private val upstreamPort: Int?,
    ) : Closeable {
        private val server = ServerSocket(0)
        private val accepted = AtomicReference<Socket?>()
        private val upstream = AtomicReference<Socket?>()
        private val request = AtomicReference<String?>()
        private val requestReady = CountDownLatch(1)
        private val failure = AtomicReference<Throwable?>()
        private val worker = thread(name = "recording-http-proxy", isDaemon = true) {
            runCatching { serveOnce() }.onFailure(failure::set)
        }

        fun selector(): ProxySelector = FixedProxySelector(
            Proxy(
                Proxy.Type.HTTP,
                InetSocketAddress.createUnresolved("127.0.0.1", server.localPort),
            ),
        )

        fun awaitRequest(): String {
            assertTrue("Proxy did not receive CONNECT", requestReady.await(1, TimeUnit.SECONDS))
            failure.get()?.let { throw AssertionError("Proxy failed", it) }
            return request.get() ?: fail("Proxy request missing").let { "" }
        }

        private fun serveOnce() {
            val client = server.accept().also(accepted::set)
            val header = readHeader(client)
            request.set(header)
            requestReady.countDown()
            val destination = upstreamPort?.let { port ->
                Socket("127.0.0.1", port).also(upstream::set)
            }
            client.getOutputStream().apply {
                write(response.toByteArray(Charsets.ISO_8859_1))
                flush()
            }
            if (destination == null) return

            val relayBack = thread(name = "http-proxy-upstream", isDaemon = true) {
                runCatching { destination.getInputStream().copyTo(client.getOutputStream()) }
                runCatching { client.close() }
            }
            try {
                client.getInputStream().copyTo(destination.getOutputStream())
            } finally {
                runCatching { destination.close() }
                runCatching { client.close() }
                relayBack.join(1_000)
            }
        }

        private fun readHeader(socket: Socket): String {
            val bytes = ByteArrayOutputStream()
            var matched = 0
            while (bytes.size() < 64 * 1024) {
                val value = socket.getInputStream().read()
                if (value < 0) throw IOException("Incomplete CONNECT request")
                bytes.write(value)
                matched = when {
                    matched == 0 && value == '\r'.code -> 1
                    matched == 1 && value == '\n'.code -> 2
                    matched == 2 && value == '\r'.code -> 3
                    matched == 3 && value == '\n'.code -> return bytes.toString(Charsets.ISO_8859_1.name())
                    value == '\r'.code -> 1
                    else -> 0
                }
            }
            throw IOException("CONNECT request exceeded test limit")
        }

        override fun close() {
            // A rejection fixture normally finishes immediately after writing its response.
            // Let that worker leave serveOnce before teardown closes the accepted socket, or the
            // teardown itself can manufacture a release-only "Socket closed" test failure.
            worker.join(1_000)
            runCatching { accepted.getAndSet(null)?.close() }
            runCatching { upstream.getAndSet(null)?.close() }
            runCatching { server.close() }
            if (worker.isAlive) worker.join(1_000)
            assertFalse("Proxy worker did not stop", worker.isAlive)
            assertNull("Proxy worker failed", failure.get())
        }

        companion object {
            fun tunnelTo(upstreamPort: Int) = RecordingHttpProxy(
                response = "HTTP/1.1 200 Connection Established\r\n\r\n",
                upstreamPort = upstreamPort,
            )

            fun respond(response: String) = RecordingHttpProxy(response, upstreamPort = null)
        }
    }

    private companion object {
        val RELAY_URL = "https://relay.example/relay/v1/device".toHttpUrl()
        val CUSTOM_PORT_RELAY_URL = "https://relay.example:8443/relay/v1/device".toHttpUrl()
    }
}
