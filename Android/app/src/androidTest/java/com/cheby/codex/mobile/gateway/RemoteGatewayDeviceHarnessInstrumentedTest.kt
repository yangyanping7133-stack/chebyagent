package com.cheby.codex.mobile.gateway

import androidx.test.ext.junit.runners.AndroidJUnit4
import com.cheby.codex.mobile.data.InMemorySecureGatewayConfigStore
import com.cheby.codex.mobile.data.PairingBootstrap
import com.cheby.codex.mobile.model.ConversationEvent
import com.cheby.codex.mobile.model.ThreadKey
import java.net.InetAddress
import java.net.Proxy
import java.util.Base64
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.OkHttpClient
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import okhttp3.tls.HandshakeCertificates
import okhttp3.tls.HeldCertificate
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RemoteGatewayDeviceHarnessInstrumentedTest {
    @Test
    fun pairingHttpAndWssUseOneRealKeystoreIdentity() = runBlocking {
        val certificate = HeldCertificate.Builder()
            .commonName("localhost")
            .addSubjectAlternativeName("localhost")
            .addSubjectAlternativeName("127.0.0.1")
            .build()
        val serverCertificates = HandshakeCertificates.Builder().heldCertificate(certificate).build()
        val clientCertificates = HandshakeCertificates.Builder()
            .addTrustedCertificate(certificate.certificate)
            .build()
        val server = MockWebServer().apply {
            useHttps(serverCertificates.sslSocketFactory(), false)
            // Keep Huawei proxy and IPv6 policies out of this in-process loopback device gate.
            // The client below also uses NO_PROXY so a device proxy cannot intercept CONNECT.
            start(InetAddress.getByName("127.0.0.1"), 0)
        }
        val client = OkHttpClient.Builder()
            .sslSocketFactory(clientCertificates.sslSocketFactory(), clientCertificates.trustManager)
            .proxy(Proxy.NO_PROXY)
            .retryOnConnectionFailure(false)
            .followRedirects(false)
            .followSslRedirects(false)
            .readTimeout(3, TimeUnit.SECONDS)
            .build()
        val requests = ConcurrentLinkedQueue<RecordedRequest>()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                requests += request
                return when (request.requestUrl?.encodedPath) {
                    "/v1/pairings/exchange" -> MockResponse().setResponseCode(200).setBody(sessionResponse())
                    "/v1/threads" -> MockResponse().setResponseCode(200).setBody(threadList())
                    "/v1/events" -> MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
                        override fun onOpen(webSocket: WebSocket, response: okhttp3.Response) {
                            webSocket.send(snapshotEvent())
                        }
                    })
                    else -> MockResponse().setResponseCode(404)
                }
            }
        }

        try {
            val key = AndroidDeviceIdentity.loadForDeviceTest(
                "${AndroidDeviceIdentity.TEST_ALIAS_PREFIX}local-harness",
                createIfMissing = true,
            )
            val store = InMemorySecureGatewayConfigStore()
            val loopbackOrigin = server.url("/").newBuilder().host("127.0.0.1").build().toString()
            store.savePairing(PairingBootstrap(loopbackOrigin, "device-harness-secret"))
            val session = RemotePairingClient.exchange(store, "JUY-AL00", key, client)
            val gateway = RemoteCodexGateway(
                sessionStore = store,
                session = session,
                deviceProofKey = key,
                client = client,
                autoRefresh = false,
            )

            gateway.refreshThreads()
            val event = withTimeout(5_000) {
                gateway.observeConversation(ThreadKey("thr_device"), "stream_device", 0).first()
            }

            assertTrue(event is ConversationEvent.MessageAdded)
            assertEquals("thr_device", gateway.threads.value.single().key.value)
            val pairing = requests.elementAt(0)
            val http = requests.elementAt(1)
            val wss = requests.elementAt(2)
            assertProof(pairing, "", key)
            assertProof(http, "device-access-token", key)
            assertProof(wss, "device-access-token", key)
            assertEquals(
                Base64.getEncoder().encodeToString(key.publicKeySpkiDer()),
                kotlinx.serialization.json.Json.parseToJsonElement(pairing.body.clone().readUtf8())
                    .jsonObject.getValue("devicePublicKey").jsonPrimitive.content,
            )
            assertEquals(3, requests.mapTo(mutableSetOf()) { it.getHeader(DeviceProofV1.HEADER_NONCE) }.size)
            gateway.close()
        } finally {
            client.connectionPool.evictAll()
            client.dispatcher.executorService.shutdownNow()
            server.shutdown()
        }
    }

    private fun assertProof(
        request: RecordedRequest,
        bearerToken: String,
        key: DeviceProofKeyHandle,
    ) {
        val timestamp = requireNotNull(request.getHeader(DeviceProofV1.HEADER_TIMESTAMP))
        val nonce = requireNotNull(request.getHeader(DeviceProofV1.HEADER_NONCE))
        val signature = Base64.getDecoder().decode(
            requireNotNull(request.getHeader(DeviceProofV1.HEADER_SIGNATURE)),
        )
        assertEquals(DeviceProofV1.VERSION, request.getHeader(DeviceProofV1.HEADER_VERSION))
        EcdsaP256Der.parseStrict(signature, requireLowS = true)
        val canonical = DeviceProofV1.canonicalBytes(
            method = requireNotNull(request.method),
            rawTarget = requireNotNull(request.path).toByteArray(Charsets.US_ASCII),
            timestamp = timestamp,
            nonce = nonce,
            body = request.body.clone().readByteArray(),
            bearerToken = bearerToken,
        )
        assertTrue(key.verifySha256(canonical, signature))
    }

    private fun sessionResponse(): String = """{
        "deviceId":"device_harness",
        "accessToken":"device-access-token",
        "expiresAt":"2026-07-20T01:00:00Z",
        "refreshToken":"device-refresh-token",
        "refreshExpiresAt":"2026-08-19T00:00:00Z",
        "streamId":"stream_device"
    }"""

    private fun threadList(): String = """{"data":[{
        "id":"thr_device","title":"Device harness","preview":"Ready",
        "createdAt":"2026-07-19T00:00:00Z","updatedAt":"2026-07-19T00:01:00Z",
        "status":"idle","lastTurnId":null,"unread":false
    }]}"""

    private fun snapshotEvent(): String = """{
        "v":1,"streamId":"stream_device","eventId":"event_device","seq":1,
        "occurredAt":"2026-07-19T00:01:01Z","type":"message.snapshot",
        "threadId":"thr_device","turnId":"turn_device","itemId":"item_device",
        "payload":{
            "schema":"cheby.rich-message/1.0","messageId":"msg_device",
            "threadId":"thr_device","turnId":"turn_device","sourceItemId":"item_device",
            "role":"assistant","state":"completed","revision":0,
            "rootBlockIds":["answer"],
            "blocks":{"answer":{"blockId":"answer","type":"text","text":"Hello","fallbackText":"Hello"}},
            "fallback":{"text":"Hello"},
            "createdAt":"2026-07-19T00:01:00Z","updatedAt":"2026-07-19T00:01:01Z"
        }
    }"""
}
