package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.data.RelayDeviceSession
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.tls.HandshakeCertificates
import okhttp3.tls.HeldCertificate
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class RelayOriginAuthenticatorTest {
    @Test(timeout = 10_000)
    fun `migration authentication consumes only Ready and sends no ack or command`() {
        val fixture = fixture()
        val received = CopyOnWriteArrayList<String>()
        val disconnected = CountDownLatch(1)
        val peer = object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                webSocket.send(
                    """{"v":1,"type":"ready","assistantId":"asst_0000000000000000000000","principalId":"dev_0000000000000000000000","role":"device","ackCursor":7,"nextDeliverySeq":8,"nodeStatus":"online"}""",
                )
                webSocket.send(
                    """{"v":1,"type":"delivery","deliverySeq":8,"messageId":"msg_0000000000000000000000","payload":{"kind":"event"}}""",
                )
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                received += text
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                disconnected.countDown()
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                disconnected.countDown()
            }
        }
        fixture.server.enqueue(MockResponse().withWebSocketUpgrade(peer))
        try {
            val candidate = session(fixture.origin)
            val verified = RelayOriginAuthenticator(
                sslSocketFactory = fixture.clientCertificates.sslSocketFactory(),
                originValidator = { assertEquals(fixture.origin, it) },
            ).authenticate(candidate, TestDeviceProofKey.generate())

            assertEquals(candidate, verified)
            fixture.server.takeRequest(2, TimeUnit.SECONDS)
            disconnected.await(2, TimeUnit.SECONDS)
            assertTrue(received.isEmpty())
        } finally {
            fixture.server.shutdown()
        }
    }

    @Test(timeout = 10_000)
    fun `http authentication rejection cannot mutate candidate credentials or cursors`() {
        val fixture = fixture()
        fixture.server.enqueue(MockResponse().setResponseCode(401))
        try {
            val candidate = session(fixture.origin)
            val error = runCatching {
                RelayOriginAuthenticator(
                    sslSocketFactory = fixture.clientCertificates.sslSocketFactory(),
                    originValidator = {},
                ).authenticate(candidate, TestDeviceProofKey.generate())
            }.exceptionOrNull()

            assertTrue(error is WebSocketHandshakeException && error.statusCode == 401)
            assertEquals(session(fixture.origin), candidate)
        } finally {
            fixture.server.shutdown()
        }
    }

    private fun session(origin: String) = RelayDeviceSession(
        assistantId = "asst_0000000000000000000000",
        assistantAlias = "我的 Codex",
        relayOrigin = origin,
        bindingScope = "binding-1",
        principalId = "dev_0000000000000000000000",
        accessToken = "access-token-00000000000000000000000000000000",
        refreshToken = "refresh-token-0000000000000000000000000000000",
        accessExpiresAt = 1_800_000_000,
        refreshExpiresAt = 1_900_000_000,
        relayAckCursor = 7,
        gatewayStreamId = "gateway-stream-1",
        gatewayAckSeq = 4,
    )

    private fun fixture(): TlsFixture {
        val certificate = HeldCertificate.Builder().addSubjectAlternativeName("localhost").build()
        val serverCertificates = HandshakeCertificates.Builder().heldCertificate(certificate).build()
        val clientCertificates = HandshakeCertificates.Builder()
            .addTrustedCertificate(certificate.certificate)
            .build()
        val server = MockWebServer().apply {
            useHttps(serverCertificates.sslSocketFactory(), false)
            start()
        }
        return TlsFixture(
            server,
            clientCertificates,
            server.url("/").toString().removeSuffix("/"),
        )
    }

    private data class TlsFixture(
        val server: MockWebServer,
        val clientCertificates: HandshakeCertificates,
        val origin: String,
    )
}
