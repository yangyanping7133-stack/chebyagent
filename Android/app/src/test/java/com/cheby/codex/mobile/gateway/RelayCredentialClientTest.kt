package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.data.InMemorySecureRelayConfigStore
import com.cheby.codex.mobile.data.ProcessLockedRelayConfigStore
import com.cheby.codex.mobile.data.RelayDeviceSession
import com.cheby.codex.mobile.data.RelayResponseReceipt
import com.cheby.codex.mobile.data.SecureRelayConfigStore
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.async
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.tls.HandshakeCertificates
import okhttp3.tls.HeldCertificate
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

class RelayCredentialClientTest {
    private val assistantId = "asst_0000000000000000000000"
    private val deviceId = "dev_0000000000000000000000"

    @Test
    fun `concurrent gateway instances perform only one refresh rotation`() = runBlocking {
        val fixture = fixture()
        val proofKey = TestDeviceProofKey.generate()
        val firstClient = RelayCredentialClient(fixture.client)
        val secondClient = RelayCredentialClient(fixture.client)
        fixture.server.enqueue(jsonResponse(pairResponse()))
        fixture.server.enqueue(
            jsonResponse(refreshResponse()).setBodyDelay(300, TimeUnit.MILLISECONDS),
        )

        try {
            val paired = firstClient.exchange(
                bootstrap(fixture.origin),
                "Android test",
                proofKey,
            )
            val store = InMemorySecureRelayConfigStore(paired)
            val refreshed = listOf(firstClient, secondClient).map { client ->
                async(Dispatchers.Default) { client.refresh(paired, proofKey, store) }
            }.awaitAll()

            assertEquals(1, fixture.server.requestCount - 1)
            assertEquals(refreshed[0], refreshed[1])
            assertEquals(refreshed[0], store.loadSession())
        } finally {
            fixture.server.shutdown()
        }
    }

    @Test
    fun `two production store wrappers cannot overwrite rotated credentials with stale cursor`() =
        runBlocking {
            val fixture = fixture()
            val proofKey = TestDeviceProofKey.generate()
            val client = RelayCredentialClient(fixture.client)
            fixture.server.enqueue(jsonResponse(pairResponse()))
            fixture.server.enqueue(jsonResponse(refreshResponse()))

            try {
                val paired = client.exchange(bootstrap(fixture.origin), "Android test", proofKey)
                val shared = AtomicReference<RelayDeviceSession?>(paired)
                val cursorRead = CountDownLatch(1)
                val releaseCursorWrite = CountDownLatch(1)
                val refreshStore = ProcessLockedRelayConfigStore(SharedSessionStore(shared))
                val cursorStore = ProcessLockedRelayConfigStore(
                    SharedSessionStore(shared, cursorRead, releaseCursorWrite),
                )

                val cursor = async(Dispatchers.IO) {
                    cursorStore.updateRelayCursor(paired.bindingScope, 41)
                }
                assertTrue(cursorRead.await(2, TimeUnit.SECONDS))
                val refresh = async(Dispatchers.IO) {
                    client.refresh(paired, proofKey, refreshStore)
                }
                delay(100)
                // The process lock keeps refresh from reading the stale session while the other
                // store instance is between its read and write.
                assertEquals(1, fixture.server.requestCount)
                releaseCursorWrite.countDown()

                assertTrue(cursor.await())
                val refreshed = refresh.await()
                assertEquals(41, refreshed.relayAckCursor)
                assertEquals(
                    "rotated-refresh-token-000000000000000000000000000000000",
                    refreshed.refreshToken,
                )
                assertEquals(refreshed, shared.get())
            } finally {
                fixture.server.shutdown()
            }
        }

    @Test
    fun `pairing and refresh use fixed HTTPS paths exact bodies PoP and rotated persistence`() = runBlocking {
        val fixture = fixture()
        val proofKey = TestDeviceProofKey.generate()
        val client = RelayCredentialClient(fixture.client)
        fixture.server.enqueue(jsonResponse(pairResponse()))
        fixture.server.enqueue(
            jsonResponse(refreshResponse()).setBodyDelay(300, TimeUnit.MILLISECONDS),
        )

        try {
            val paired = client.exchange(
                RelayEnrollmentBootstrap(
                    assistantId = assistantId,
                    assistantAlias = "我的 Codex",
                    relayOrigin = fixture.origin,
                    pairingSecret = "pair_0000000000000000000000000000000000000000000",
                ),
                "Android test",
                proofKey,
            )
            val pairRequest = requireNotNull(fixture.server.takeRequest())
            assertEquals("/relay/v1/pairings/exchange", pairRequest.path)
            assertEquals(null, pairRequest.getHeader("Authorization"))
            assertEquals("1", pairRequest.getHeader(DeviceProofV1.HEADER_VERSION))
            val pairBody = Json.parseToJsonElement(pairRequest.body.readUtf8()).jsonObject
            assertEquals(
                setOf("assistantId", "pairingSecret", "deviceName", "devicePublicKey"),
                pairBody.keys,
            )
            assertEquals(assistantId, pairBody.getValue("assistantId").jsonPrimitive.content)

            val store = InMemorySecureRelayConfigStore(paired)
            val refresh = async(Dispatchers.Default) { client.refresh(paired, proofKey, store) }
            val refreshRequest = requireNotNull(fixture.server.takeRequest())
            // Commit cursor progress while the refresh response is still in flight. Rotation
            // must merge only credential fields into the latest durable record.
            assertTrue(store.updateRelayCursor(paired.bindingScope, 41))
            assertTrue(store.updateGatewayCursor(paired.bindingScope, "stream-current", 17))
            val refreshed = refresh.await()
            assertEquals("/relay/v1/auth/refresh", refreshRequest.path)
            assertEquals(null, refreshRequest.getHeader("Authorization"))
            assertEquals("rotated-access-token-0000000000000000000000000000000000", refreshed.accessToken)
            assertEquals(41, refreshed.relayAckCursor)
            assertEquals("stream-current", refreshed.gatewayStreamId)
            assertEquals(17, refreshed.gatewayAckSeq)
            assertEquals(refreshed, store.loadSession())
        } finally {
            fixture.server.shutdown()
        }
    }

    @Test
    fun `origin migration refreshes at candidate but keeps old origin durable until ready CAS`() =
        runBlocking {
            val fixture = fixture()
            val proofKey = TestDeviceProofKey.generate()
            val requestedOrigins = mutableListOf<String>()
            val client = RelayCredentialClient(
                client = fixture.client,
                originValidator = requestedOrigins::add,
            )
            fixture.server.enqueue(jsonResponse(pairResponse()))
            fixture.server.enqueue(jsonResponse(refreshResponse()))

            try {
                val enrolled = client.exchange(bootstrap(fixture.origin), "Android test", proofKey)
                val legacyOrigin = "https://legacy-relay.example"
                val durableLegacy = enrolled.copy(relayOrigin = legacyOrigin)
                val store = InMemorySecureRelayConfigStore(durableLegacy)
                val candidate = durableLegacy.copy(relayOrigin = fixture.origin)

                val refreshedCandidate = client.refreshForOriginMigration(
                    candidate,
                    proofKey,
                    store,
                )
                val request = requireNotNull(fixture.server.takeRequest())
                val refreshRequest = requireNotNull(fixture.server.takeRequest())

                assertEquals("/relay/v1/pairings/exchange", request.path)
                assertEquals("/relay/v1/auth/refresh", refreshRequest.path)
                assertEquals(listOf(fixture.origin, fixture.origin), requestedOrigins)
                assertEquals(fixture.origin, refreshedCandidate.relayOrigin)
                assertEquals(
                    "rotated-access-token-0000000000000000000000000000000000",
                    refreshedCandidate.accessToken,
                )
                val durableAfterRefresh = requireNotNull(store.loadSession())
                assertEquals(legacyOrigin, durableAfterRefresh.relayOrigin)
                assertEquals(refreshedCandidate.accessToken, durableAfterRefresh.accessToken)
                assertEquals(refreshedCandidate.refreshToken, durableAfterRefresh.refreshToken)
                assertEquals(enrolled.bindingScope, durableAfterRefresh.bindingScope)
                assertEquals(enrolled.relayAckCursor, durableAfterRefresh.relayAckCursor)
                assertEquals(enrolled.gatewayAckSeq, durableAfterRefresh.gatewayAckSeq)
            } finally {
                fixture.server.shutdown()
            }
        }

    @Test
    fun `missing no-store and redirect fail closed`() = runBlocking {
        val fixture = fixture()
        val client = RelayCredentialClient(fixture.client)
        val proofKey = TestDeviceProofKey.generate()
        try {
            fixture.server.enqueue(
                MockResponse().setResponseCode(200).setHeader("Content-Type", "application/json")
                    .setBody(pairResponse()),
            )
            val noStore = runCatching { client.exchange(bootstrap(fixture.origin), "Android", proofKey) }
                .exceptionOrNull()
            assertTrue(noStore is GatewayTransportException)

            fixture.server.enqueue(MockResponse().setResponseCode(307).setHeader("Location", "/elsewhere"))
            val redirect = runCatching { client.exchange(bootstrap(fixture.origin), "Android", proofKey) }
                .exceptionOrNull()
            assertNotNull(redirect)
            assertFalse(fixture.server.requestCount > 2)
        } finally {
            fixture.server.shutdown()
        }
    }

    private fun bootstrap(origin: String) = RelayEnrollmentBootstrap(
        assistantId,
        "我的 Codex",
        origin,
        "pair_0000000000000000000000000000000000000000000",
    )

    private fun pairResponse(): String =
        """{"assistantId":"$assistantId","deviceId":"$deviceId","accessToken":"access-token-000000000000000000000000000000000000","accessExpiresAt":1800000000,"refreshToken":"refresh-token-00000000000000000000000000000000000","refreshExpiresAt":1900000000}"""

    private fun refreshResponse(): String =
        """{"deviceId":"$deviceId","accessToken":"rotated-access-token-0000000000000000000000000000000000","accessExpiresAt":1800000100,"refreshToken":"rotated-refresh-token-000000000000000000000000000000000","refreshExpiresAt":1900000100}"""

    private fun jsonResponse(body: String) = MockResponse()
        .setResponseCode(200)
        .setHeader("Content-Type", "application/json")
        .setHeader("Cache-Control", "no-store")
        .setBody(body)

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
        val client = OkHttpClient.Builder()
            .sslSocketFactory(clientCertificates.sslSocketFactory(), clientCertificates.trustManager)
            .hostnameVerifier { host, _ -> host == "localhost" }
            .followRedirects(false)
            .followSslRedirects(false)
            .build()
        return TlsFixture(server, client, server.url("/").toString().removeSuffix("/"))
    }

    private data class TlsFixture(
        val server: MockWebServer,
        val client: OkHttpClient,
        val origin: String,
    )
}

private class SharedSessionStore(
    private val shared: AtomicReference<RelayDeviceSession?>,
    private val cursorRead: CountDownLatch? = null,
    private val releaseCursorWrite: CountDownLatch? = null,
) : SecureRelayConfigStore {
    @Synchronized
    override fun loadSession(): RelayDeviceSession? = shared.get()

    @Synchronized
    override fun saveSession(session: RelayDeviceSession): Boolean {
        shared.set(session)
        return true
    }

    override fun migrateRelayOrigin(
        expectedBindingScope: String,
        expectedOrigin: String,
        newOrigin: String,
    ): RelayDeviceSession? {
        val current = shared.get() ?: return null
        if (
            current.bindingScope != expectedBindingScope ||
            current.relayOrigin != expectedOrigin
        ) return null
        return current.copy(relayOrigin = newOrigin).also(shared::set)
    }

    @Synchronized
    override fun mergeRotatedCredentials(
        expectedBindingScope: String,
        expectedPrincipalId: String,
        expectedRefreshToken: String,
        accessToken: String,
        refreshToken: String,
        accessExpiresAt: Long,
        refreshExpiresAt: Long,
    ): RelayDeviceSession? {
        val current = shared.get() ?: return null
        if (
            current.bindingScope != expectedBindingScope ||
            current.principalId != expectedPrincipalId ||
            current.refreshToken != expectedRefreshToken
        ) return null
        return current.copy(
            accessToken = accessToken,
            refreshToken = refreshToken,
            accessExpiresAt = accessExpiresAt,
            refreshExpiresAt = refreshExpiresAt,
        ).also(shared::set)
    }

    @Synchronized
    override fun updateRelayCursor(expectedBindingScope: String, deliverySeq: Long): Boolean {
        val stale = shared.get() ?: return false
        if (stale.bindingScope != expectedBindingScope) return false
        cursorRead?.countDown()
        if (releaseCursorWrite != null && !releaseCursorWrite.await(2, TimeUnit.SECONDS)) return false
        shared.set(stale.copy(relayAckCursor = maxOf(stale.relayAckCursor, deliverySeq)))
        return true
    }

    @Synchronized
    override fun updateGatewayCursor(
        expectedBindingScope: String,
        streamId: String,
        eventSeq: Long,
    ): Boolean {
        val current = shared.get() ?: return false
        if (current.bindingScope != expectedBindingScope) return false
        shared.set(current.copy(gatewayStreamId = streamId, gatewayAckSeq = eventSeq))
        return true
    }

    override fun loadResponseReceipt(
        expectedBindingScope: String,
        requestId: String,
    ): RelayResponseReceipt? = null

    override fun recordResponseReceipt(
        expectedBindingScope: String,
        receipt: RelayResponseReceipt,
    ): Boolean = true

    override fun clearSession() {
        shared.set(null)
    }

    override fun clearSessionIfScope(expectedBindingScope: String): Boolean {
        val current = shared.get() ?: return false
        return current.bindingScope == expectedBindingScope && shared.compareAndSet(current, null)
    }
}
