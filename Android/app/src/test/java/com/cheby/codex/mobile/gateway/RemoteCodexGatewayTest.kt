package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.data.InMemorySecureGatewayConfigStore
import com.cheby.codex.mobile.data.InMemoryDurableOutboxStore
import com.cheby.codex.mobile.data.OutboxEntry
import com.cheby.codex.mobile.data.OutboxState
import com.cheby.codex.mobile.data.PairingBootstrap
import com.cheby.codex.mobile.data.PendingApprovalCommand
import com.cheby.codex.mobile.data.RemoteGatewaySession
import com.cheby.codex.mobile.data.scopeKey
import com.cheby.codex.mobile.model.ConversationEvent
import com.cheby.codex.mobile.model.ActionsBlock
import com.cheby.codex.mobile.model.ConversationReducer
import com.cheby.codex.mobile.model.ConversationSnapshot
import com.cheby.codex.mobile.model.GatewayConnectionState
import com.cheby.codex.mobile.model.PatchOperation
import com.cheby.codex.mobile.model.ThreadKey
import java.io.File
import java.util.Base64
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import kotlinx.coroutines.async
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.take
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import okhttp3.OkHttpClient
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import okhttp3.mockwebserver.SocketPolicy
import okhttp3.tls.HandshakeCertificates
import okhttp3.tls.HeldCertificate
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlin.concurrent.thread
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class RemoteCodexGatewayTest {
    private lateinit var server: MockWebServer
    private lateinit var client: OkHttpClient
    private lateinit var store: InMemorySecureGatewayConfigStore
    private lateinit var deviceProofKey: TestDeviceProofKey

    @Before
    fun setUp() {
        val certificate = HeldCertificate.Builder()
            .commonName("localhost")
            .addSubjectAlternativeName("localhost")
            .build()
        val serverCertificates = HandshakeCertificates.Builder()
            .heldCertificate(certificate)
            .build()
        val clientCertificates = HandshakeCertificates.Builder()
            .addTrustedCertificate(certificate.certificate)
            .build()
        server = MockWebServer().apply {
            useHttps(serverCertificates.sslSocketFactory(), false)
            start()
        }
        client = OkHttpClient.Builder()
            .sslSocketFactory(clientCertificates.sslSocketFactory(), clientCertificates.trustManager)
            .readTimeout(3, TimeUnit.SECONDS)
            .build()
        deviceProofKey = TestDeviceProofKey.generate()
        store = InMemorySecureGatewayConfigStore(
            session = RemoteGatewaySession(
                baseUrl = server.url("/").toString(),
                accessToken = "test-access-token-never-rendered",
                refreshToken = "test-refresh-token-never-rendered",
                deviceId = "device_public",
                expiresAt = "2026-07-20T00:00:00Z",
                refreshExpiresAt = "2026-08-18T00:00:00Z",
                streamId = "stream_public",
            ),
        )
    }

    @After
    fun tearDown() {
        client.connectionPool.evictAll()
        client.dispatcher.executorService.shutdownNow()
        server.shutdown()
    }

    @Test
    fun `HTTPS list uses bearer auth and public thread contract`() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(200).setBody(threadList()))
        val gateway = gateway()

        gateway.refreshThreads()

        assertEquals("thr_public", gateway.threads.value.single().key.value)
        val request = server.takeRequest(2, TimeUnit.SECONDS)!!
        assertEquals("/v1/threads", request.path)
        assertEquals("Bearer test-access-token-never-rendered", request.getHeader("Authorization"))
        assertValidProof(request, "test-access-token-never-rendered")
    }

    @Test
    fun `capability is proof authenticated no-store and fails closed on absent input`() = runBlocking {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .addHeader("Cache-Control", "private, no-store")
                .setBody(LocalImageContractCodecTest.CAPABILITIES),
        )
        val gateway = gateway()

        val capability = gateway.loadCapabilities(gateway.sessionScope, 0).localImage

        assertEquals(10, capability?.maxImagesPerTurn)
        val request = requireNotNull(server.takeRequest(2, TimeUnit.SECONDS))
        assertEquals("GET", request.method)
        assertEquals("/v1/capabilities", request.path)
        assertValidProof(request, "test-access-token-never-rendered")
    }

    @Test
    fun `capability response without no-store is rejected`() = runBlocking {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setBody(LocalImageContractCodecTest.CAPABILITIES),
        )
        val gateway = gateway()

        val failure = runCatching {
            gateway.loadCapabilities(gateway.sessionScope, 0)
        }.exceptionOrNull()

        assertTrue(failure is GatewayTransportException)
    }

    @Test
    fun `image upload sends exact bytes length type and proof then turn preserves ordered inputs`() = runBlocking {
        val assetRef = "ast_abcdefghijklmnopqrstuvwxyz0123456789"
        server.enqueue(
            MockResponse()
                .setResponseCode(201)
                .addHeader("Cache-Control", "no-store")
                .setBody(
                    """{"assetRef":"$assetRef","mediaType":"image/png","width":16,"height":12,"byteCount":64,"expiresAt":"2026-07-20T00:00:00Z"}""",
                ),
        )
        server.enqueue(
            MockResponse().setResponseCode(202).setBody(
                """{"id":"turn_image_001","threadId":"thr_public","status":"running","clientMessageId":"client-image-message-001","createdAt":"2026-07-20T00:00:00Z"}""",
            ),
        )
        val gateway = gateway()
        val bytes = ByteArray(64) { it.toByte() }
        val messageId = "client-image-message-001"
        val assetId = "00000000-0000-4000-8000-000000000001"

        val uploaded = gateway.uploadTurnImage(
            ThreadKey("thr_public"),
            messageId,
            assetId,
            bytes,
            gateway.sessionScope,
            0,
        )
        gateway.sendTurnInput(
            ThreadKey("thr_public"),
            listOf(TurnInput.Text("inspect"), TurnInput.Image(uploaded.assetRef)),
            messageId,
            gateway.sessionScope,
            0,
        )

        val upload = requireNotNull(server.takeRequest(2, TimeUnit.SECONDS))
        assertEquals("PUT", upload.method)
        assertEquals(
            "/v1/threads/thr_public/turn-inputs/$messageId/images/$assetId",
            upload.path,
        )
        assertEquals("application/octet-stream", upload.getHeader("Content-Type"))
        assertEquals(bytes.size.toString(), upload.getHeader("Content-Length"))
        assertEquals(null, upload.getHeader("Transfer-Encoding"))
        assertTrue(upload.body.clone().readByteArray().contentEquals(bytes))
        assertValidProof(upload, "test-access-token-never-rendered")

        val turn = requireNotNull(server.takeRequest(2, TimeUnit.SECONDS))
        assertEquals("POST", turn.method)
        assertEquals("/v1/threads/thr_public/turns", turn.path)
        assertEquals(
            """{"clientMessageId":"$messageId","input":[{"type":"text","text":"inspect"},{"type":"image","assetRef":"$assetRef"}]}""",
            turn.body.clone().readUtf8(),
        )
        assertValidProof(turn, "test-access-token-never-rendered")
    }

    @Test
    fun `WSS resumes in stream order and ACKs each applied event`() = runBlocking {
        val acknowledgements = ConcurrentLinkedQueue<String>()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                if (request.requestUrl?.encodedPath != "/v1/events") return MockResponse().setResponseCode(404)
                return MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: okhttp3.Response) {
                        webSocket.send(snapshotEvent())
                        webSocket.send(patchEvent())
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        acknowledgements += text
                        if (acknowledgements.size >= 2) webSocket.close(1000, "complete")
                    }
                })
            }
        }
        val gateway = gateway()
        val received = ConcurrentLinkedQueue<ConversationEvent>()
        val job = launch {
            gateway.observeConversation(ThreadKey("thr_public"), "stream_public", 0)
                .collect { received += it }
        }

        withTimeout(5_000) {
            while ((store.loadSession()?.lastAckSeq ?: 0) < 2) delay(20)
        }
        job.cancelAndJoin()

        assertEquals(2, received.size)
        assertTrue(received.elementAt(0) is ConversationEvent.MessageAdded)
        assertTrue(received.elementAt(1) is ConversationEvent.MessagePatched)
        assertEquals(2, acknowledgements.size)
        assertTrue(acknowledgements.elementAt(0).contains("\"seq\":1"))
        assertTrue(acknowledgements.elementAt(1).contains("\"seq\":2"))
        val request = server.takeRequest(2, TimeUnit.SECONDS)!!
        assertEquals("stream_public", request.requestUrl?.queryParameter("streamId"))
        assertEquals("0", request.requestUrl?.queryParameter("afterSeq"))
        assertEquals("Bearer test-access-token-never-rendered", request.getHeader("Authorization"))
        assertValidProof(request, "test-access-token-never-rendered")
    }

    @Test
    fun `exact duplicate ACK uses committed cursor and never rolls durable state backward`() = runBlocking {
        val acknowledgements = ConcurrentLinkedQueue<String>()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                if (request.requestUrl?.encodedPath != "/v1/events") return MockResponse().setResponseCode(404)
                return MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: okhttp3.Response) {
                        webSocket.send(snapshotEvent())
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        acknowledgements += text
                        if (acknowledgements.size == 1) {
                            webSocket.send(snapshotEvent())
                        } else {
                            webSocket.close(1000, "complete")
                        }
                    }
                })
            }
        }
        val gateway = gateway()
        val received = ConcurrentLinkedQueue<ConversationEvent>()
        val job = launch {
            gateway.observeConversation(ThreadKey("thr_public"), "stream_public", 0)
                .collect { received += it }
        }

        withTimeout(5_000) {
            while (acknowledgements.size < 2) delay(20)
        }
        job.cancelAndJoin()

        assertEquals(2, received.size)
        assertEquals(listOf(1L, 1L), acknowledgements.map(::ackSequence))
        assertEquals("stream_public", store.loadSession()?.streamId)
        assertEquals(1L, store.loadSession()?.lastAckSeq)
    }

    @Test
    fun `same event id at a new sequence forces sync without ACK or cursor advance`() = runBlocking {
        val acknowledgements = ConcurrentLinkedQueue<String>()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                if (request.requestUrl?.encodedPath != "/v1/events") return MockResponse().setResponseCode(404)
                return MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: okhttp3.Response) {
                        webSocket.send(snapshotEvent())
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        acknowledgements += text
                        webSocket.send(snapshotEvent().replace("\"seq\":1", "\"seq\":2"))
                    }
                })
            }
        }

        val events = withTimeout(5_000) {
            gateway().observeConversation(ThreadKey("thr_public"), "stream_public", 0)
                .take(2)
                .toList()
        }

        assertTrue(events.first() is ConversationEvent.MessageAdded)
        assertTrue(events.last() is ConversationEvent.SyncRequired)
        assertEquals(listOf(1L), acknowledgements.map(::ackSequence))
        assertEquals("stream_public", store.loadSession()?.streamId)
        assertEquals(1L, store.loadSession()?.lastAckSeq)
    }

    @Test(expected = IllegalArgumentException::class)
    fun `cleartext gateway URL is rejected before network access`() {
        val invalid = RemoteGatewaySession(
            baseUrl = "http://gateway.invalid/",
            accessToken = "token",
            refreshToken = "refresh-token",
            deviceId = "device_public",
            expiresAt = "2026-07-20T00:00:00Z",
            refreshExpiresAt = "2026-08-18T00:00:00Z",
            streamId = "stream_public",
        )
        RemoteCodexGateway(
            InMemorySecureGatewayConfigStore(session = invalid),
            invalid,
            deviceProofKey,
            client,
            autoRefresh = false,
        )
    }

    @Test
    fun `concurrent TOKEN_EXPIRED responses single-flight refresh and rotate both tokens`() = runBlocking {
        val refreshCalls = AtomicInteger()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse = when {
                request.path == "/v1/auth/refresh" -> {
                    refreshCalls.incrementAndGet()
                    MockResponse().setResponseCode(200).setBody(refreshResponse())
                }
                request.getHeader("Authorization") == "Bearer test-access-token-never-rendered" ->
                    MockResponse().setResponseCode(401).setBody(tokenExpired())
                request.getHeader("Authorization") == "Bearer rotated-access-token" ->
                    MockResponse().setResponseCode(200).setBody(threadList())
                else -> MockResponse().setResponseCode(401)
            }
        }
        val gateway = gateway()

        val first = async { gateway.refreshThreads() }
        val second = async { gateway.refreshThreads() }
        first.await()
        second.await()

        assertEquals(1, refreshCalls.get())
        assertEquals("rotated-access-token", store.loadSession()?.accessToken)
        assertEquals("rotated-refresh-token", store.loadSession()?.refreshToken)
        assertEquals(GatewayKind.REMOTE, gateway.kind.value)
        assertEquals("thr_public", gateway.threads.value.single().key.value)
    }

    @Test
    fun `HTTP refresh and retry sign exact bytes with three fresh nonces`() = runBlocking {
        val threadAttempts = AtomicInteger()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse = when (request.path) {
                "/v1/auth/refresh" -> MockResponse().setResponseCode(200).setBody(refreshResponse())
                "/v1/threads" -> if (threadAttempts.incrementAndGet() == 1) {
                    MockResponse().setResponseCode(401).setBody(tokenExpired())
                } else {
                    MockResponse().setResponseCode(200).setBody(threadList())
                }
                else -> MockResponse().setResponseCode(404)
            }
        }

        gateway().refreshThreads()

        val original = requireNotNull(server.takeRequest(2, TimeUnit.SECONDS))
        val refresh = requireNotNull(server.takeRequest(2, TimeUnit.SECONDS))
        val retry = requireNotNull(server.takeRequest(2, TimeUnit.SECONDS))
        assertEquals(listOf("/v1/threads", "/v1/auth/refresh", "/v1/threads"), listOf(original.path, refresh.path, retry.path))
        assertValidProof(original, "test-access-token-never-rendered")
        assertEquals(null, refresh.getHeader("Authorization"))
        assertEquals(
            "{\"deviceId\":\"device_public\",\"refreshToken\":\"test-refresh-token-never-rendered\"}",
            refresh.body.clone().readUtf8(),
        )
        assertValidProof(refresh, "")
        assertValidProof(retry, "rotated-access-token")
        assertEquals("Bearer rotated-access-token", retry.getHeader("Authorization"))
        assertEquals(
            3,
            setOf(
                original.getHeader(DeviceProofV1.HEADER_NONCE),
                refresh.getHeader(DeviceProofV1.HEADER_NONCE),
                retry.getHeader(DeviceProofV1.HEADER_NONCE),
            ).size,
        )
    }

    @Test
    fun `device signing failure sends no request and clears owned session approvals and outbox`() = runBlocking {
        val ownedScope = store.loadSession()!!.scopeKey()
        store.replaceApprovalCommandsForScope(
            ownedScope,
            listOf(
                PendingApprovalCommand(
                    sessionScope = ownedScope,
                    actionRef = "do-not-replay",
                    approvalId = "approval-secret",
                    threadId = "thr_public",
                    decision = "approve",
                    actionToken = "secret-action-token",
                    expiresAtMillis = Long.MAX_VALUE,
                    summary = "Sensitive",
                    reason = "Must clear",
                ),
            ),
        )
        val outboxClears = AtomicInteger()
        val signingFailure = object : DeviceProofKeyHandle {
            override fun publicKeySpkiDer(): ByteArray = deviceProofKey.publicKeySpkiDer()
            override fun signSha256(message: ByteArray): ByteArray =
                throw DeviceIdentityUnavailableException()
            override fun verifySha256(message: ByteArray, signatureDer: ByteArray): Boolean = false
        }
        val gateway = RemoteCodexGateway(
            sessionStore = store,
            session = store.loadSession()!!,
            deviceProofKey = signingFailure,
            client = client,
            autoRefresh = false,
            onDeviceIdentityInvalidated = { outboxClears.incrementAndGet() },
        )

        gateway.refreshThreads()

        assertEquals(null, server.takeRequest(300, TimeUnit.MILLISECONDS))
        assertEquals(null, store.loadSession())
        assertTrue(store.loadApprovalCommands().isEmpty())
        assertEquals(1, outboxClears.get())
        assertEquals(GatewayKind.UNCONFIGURED, gateway.kind.value)
    }

    @Test
    fun `A invalidation scope clear preserves B outbox enqueued after A session removal`() = runBlocking {
        val sessionA = store.loadSession()!!
        val sessionB = sessionA.copy(
            accessToken = "b-access",
            refreshToken = "b-refresh",
            deviceId = "device-b",
            sessionScope = "scope-b-after-a-clear",
        )
        val entryA = outboxEntry("message-a", sessionA.scopeKey())
        val entryB = outboxEntry("message-b", sessionB.scopeKey())
        val outbox = InMemoryDurableOutboxStore(listOf(entryA))
        val aSessionCleared = CountDownLatch(1)
        val bEnqueued = CountDownLatch(1)
        val replacementFailure = AtomicReference<Throwable?>()
        val callbackScope = AtomicReference<String?>()
        val callbackSawClearedSession = AtomicBoolean()
        val callbackSawBEnqueued = AtomicBoolean()
        val callbackClearedA = AtomicBoolean()
        val replacement = thread(name = "pair-b-in-invalidation-window") {
            try {
                check(aSessionCleared.await(5, TimeUnit.SECONDS))
                check(store.saveSession(sessionB))
                check(outbox.enqueue(entryB))
            } catch (error: Throwable) {
                replacementFailure.set(error)
            } finally {
                bEnqueued.countDown()
            }
        }
        val signingFailure = object : DeviceProofKeyHandle {
            override fun publicKeySpkiDer(): ByteArray = deviceProofKey.publicKeySpkiDer()
            override fun signSha256(message: ByteArray): ByteArray =
                throw DeviceIdentityUnavailableException()
            override fun verifySha256(message: ByteArray, signatureDer: ByteArray): Boolean = false
        }
        val gatewayA = RemoteCodexGateway(
            sessionStore = store,
            session = sessionA,
            deviceProofKey = signingFailure,
            client = client,
            autoRefresh = false,
            onDeviceIdentityInvalidated = { invalidatedScope ->
                callbackScope.set(invalidatedScope)
                callbackSawClearedSession.set(store.loadSession() == null)
                aSessionCleared.countDown()
                callbackSawBEnqueued.set(bEnqueued.await(5, TimeUnit.SECONDS))
                callbackClearedA.set(outbox.clearScope(invalidatedScope))
            },
        )

        gatewayA.refreshThreads()
        replacement.join(5_000)

        assertFalse(replacement.isAlive)
        assertEquals(null, replacementFailure.get())
        assertEquals(sessionA.scopeKey(), callbackScope.get())
        assertTrue(callbackSawClearedSession.get())
        assertTrue(callbackSawBEnqueued.get())
        assertTrue(callbackClearedA.get())
        assertEquals(null, server.takeRequest(300, TimeUnit.MILLISECONDS))
        assertEquals(sessionB, store.loadSession())
        assertEquals(listOf(entryB), outbox.list())
    }

    @Test
    fun `late device failure from A cannot clear replacement B session approvals or outbox`() = runBlocking {
        val sessionA = store.loadSession()!!
        val sessionB = sessionA.copy(
            accessToken = "b-access",
            refreshToken = "b-refresh",
            deviceId = "device-b",
            sessionScope = "scope-b",
        )
        val commandB = PendingApprovalCommand(
            sessionScope = sessionB.scopeKey(),
            actionRef = "b-action",
            approvalId = "b-approval",
            threadId = "b-thread",
            decision = "approve",
            actionToken = "b-action-token",
            expiresAtMillis = Long.MAX_VALUE,
            summary = "B",
            reason = "B only",
        )
        val outboxClears = AtomicInteger()
        val switchThenFail = object : DeviceProofKeyHandle {
            override fun publicKeySpkiDer(): ByteArray = deviceProofKey.publicKeySpkiDer()
            override fun signSha256(message: ByteArray): ByteArray {
                store.saveSession(sessionB)
                store.replaceApprovalCommandsForScope(sessionB.scopeKey(), listOf(commandB))
                throw DeviceIdentityUnavailableException()
            }
            override fun verifySha256(message: ByteArray, signatureDer: ByteArray): Boolean = false
        }
        val gatewayA = RemoteCodexGateway(
            sessionStore = store,
            session = sessionA,
            deviceProofKey = switchThenFail,
            client = client,
            autoRefresh = false,
            onDeviceIdentityInvalidated = { outboxClears.incrementAndGet() },
        )

        gatewayA.refreshThreads()

        assertEquals(null, server.takeRequest(300, TimeUnit.MILLISECONDS))
        assertEquals(sessionB, store.loadSession())
        assertEquals(listOf(commandB), store.loadApprovalCommands())
        assertEquals(0, outboxClears.get())
    }

    @Test
    fun `pairing signing failure sends no request and preserves bootstrap for retry`() = runBlocking {
        store.clearSession()
        store.savePairing(PairingBootstrap(server.url("/").toString(), "one-time-pairing-secret"))
        val signingFailure = object : DeviceProofKeyHandle {
            override fun publicKeySpkiDer(): ByteArray = deviceProofKey.publicKeySpkiDer()
            override fun signSha256(message: ByteArray): ByteArray =
                throw DeviceIdentityUnavailableException()
            override fun verifySha256(message: ByteArray, signatureDer: ByteArray): Boolean = false
        }

        val failure = runCatching {
            RemotePairingClient.exchange(store, "JUY-AL00", signingFailure, client)
        }.exceptionOrNull()

        assertTrue(failure is GatewayTransportException)
        assertEquals(null, server.takeRequest(300, TimeUnit.MILLISECONDS))
        assertTrue(store.loadPairing() != null)
        assertEquals(null, store.loadSession())
    }

    @Test
    fun `transport defaults disable implicit retry and every redirect`() {
        val hardened = RemoteCodexGateway.defaultClient()

        assertFalse(hardened.retryOnConnectionFailure)
        assertFalse(hardened.followRedirects)
        assertFalse(hardened.followSslRedirects)
    }

    @Test
    fun `non retryable ambiguous turn response remains distinguishable`() = runBlocking {
        server.enqueue(
            MockResponse().setResponseCode(409).setBody(
                """{"error":{"code":"TURN_DELIVERY_AMBIGUOUS_RESYNC_REQUIRED","message":"Sync","retryable":false}}""",
            ),
        )

        val error = runCatching {
            gateway().sendTurn(ThreadKey("thr_public"), "hello", "fixed-client-id")
        }.exceptionOrNull() as GatewayRequestException

        assertEquals("TURN_DELIVERY_AMBIGUOUS_RESYNC_REQUIRED", error.code)
        assertFalse(error.retryable)
    }

    @Test
    fun `old remote refuses send and action after durable session scope changes`() = runBlocking {
        val remote = gateway()
        val replacement = store.loadSession()!!.copy(
            accessToken = "replacement-token",
            refreshToken = "replacement-refresh",
            sessionScope = "scope-b",
        )
        store.saveSession(replacement)

        val sendFailure = runCatching {
            remote.sendTurn(ThreadKey("thr_public"), "belongs to A", "a-id")
        }.exceptionOrNull()
        val actionFailure = runCatching { remote.performAction("a-action") }.exceptionOrNull()

        assertTrue(sendFailure is GatewayTransportException)
        assertTrue(actionFailure is GatewayTransportException)
        assertEquals(null, server.takeRequest(300, TimeUnit.MILLISECONDS))
        assertEquals("scope-b", store.loadSession()?.sessionScope)
    }

    @Test
    fun `refresh denial clears secure session and returns to unpaired state`() = runBlocking {
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse = when (request.path) {
                "/v1/auth/refresh" -> MockResponse().setResponseCode(401).setBody(
                    """{"error":{"code":"REFRESH_DENIED","message":"Denied","retryable":false}}""",
                )
                else -> MockResponse().setResponseCode(401).setBody(tokenExpired())
            }
        }
        val gateway = gateway()

        gateway.refreshThreads()

        assertEquals(null, store.loadSession())
        assertEquals(GatewayKind.UNCONFIGURED, gateway.kind.value)
        assertTrue(gateway.threads.value.isEmpty())
    }

    @Test
    fun `pairing stores rotated credential set without exposing it in model text`() = runBlocking {
        store.clearSession()
        val pairing = PairingBootstrap(server.url("/").toString(), "one-time-pairing-secret")
        store.savePairing(pairing)
        server.enqueue(MockResponse().setResponseCode(200).setBody(pairingResponse()))

        val session = RemotePairingClient.exchange(
            store = store,
            deviceName = "JUY-AL00",
            deviceProofKey = deviceProofKey,
            client = client,
        )

        assertEquals("rotated-refresh-token", store.loadSession()?.refreshToken)
        assertEquals(null, store.loadPairing())
        assertFalse(session.toString().contains(session.accessToken))
        assertFalse(pairing.toString().contains(pairing.pairingSecret))
        val request = requireNotNull(server.takeRequest(2, TimeUnit.SECONDS))
        assertEquals("/v1/pairings/exchange", request.path)
        assertEquals(null, request.getHeader("Authorization"))
        assertValidProof(request, "")
        assertEquals(
            Base64.getEncoder().encodeToString(deviceProofKey.publicKeySpkiDer()),
            Json.parseToJsonElement(request.body.clone().readUtf8())
                .jsonObject.getValue("devicePublicKey").jsonPrimitive.content,
        )
    }

    @Test
    fun `re-pair purges old approval secrets and pending prompt state before new session`() = runBlocking {
        store.replaceApprovalCommandsForScope(
            store.loadSession()!!.scopeKey(),
            listOf(
                PendingApprovalCommand(
                    sessionScope = store.loadSession()!!.scopeKey(),
                    actionRef = "old-ref",
                    approvalId = "old-approval",
                    threadId = "old-thread",
                    decision = "approve",
                    actionToken = "old-secret-token",
                    expiresAtMillis = Long.MAX_VALUE,
                    summary = "Old command",
                    reason = "Old reason",
                ),
            ),
        )
        val pendingPrompts = mutableListOf("sensitive prompt for A")
        val retainedOutboxScope = AtomicReference<String?>()
        store.savePairing(PairingBootstrap(server.url("/").toString(), "new-pairing-secret"))
        server.enqueue(MockResponse().setResponseCode(200).setBody(pairingResponse()))

        RemotePairingClient.exchange(
            store,
            "JUY-AL00",
            deviceProofKey,
            client,
            afterSessionReplace = { newScope ->
                retainedOutboxScope.set(newScope)
                pendingPrompts.clear()
            },
        )

        assertTrue(store.loadApprovalCommands().isEmpty())
        assertTrue(pendingPrompts.isEmpty())
        assertEquals(store.loadSession()?.scopeKey(), retainedOutboxScope.get())
        assertEquals("rotated-access-token", store.loadSession()?.accessToken)
    }

    @Test
    fun `pairing persistence failure preserves local pairing state and never reports success`() = runBlocking {
        val previous = store.loadSession()!!
        val failingStore = InMemorySecureGatewayConfigStore(
            session = previous,
            allowSessionWrites = false,
        )
        failingStore.savePairing(PairingBootstrap(server.url("/").toString(), "new-pairing-secret"))
        server.enqueue(MockResponse().setResponseCode(200).setBody(pairingResponse()))

        val failure = runCatching {
            RemotePairingClient.exchange(
                failingStore,
                "JUY-AL00",
                deviceProofKey,
                client,
            )
        }.exceptionOrNull()

        assertTrue(failure is GatewayTransportException)
        assertEquals(previous, failingStore.loadSession())
        assertTrue(failingStore.loadPairing() != null)
    }

    @Test
    fun `authoritative empty pending approvals removes stale local action`() = runBlocking {
        store.replaceApprovalCommandsForScope(
            store.loadSession()!!.scopeKey(),
            listOf(
                PendingApprovalCommand(
                    sessionScope = store.loadSession()!!.scopeKey(),
                    actionRef = "stale-ref",
                    approvalId = "stale-approval",
                    threadId = "thr_wire_01",
                    decision = "approve",
                    actionToken = "stale-token",
                    expiresAtMillis = Long.MAX_VALUE,
                    summary = "Already resolved",
                    reason = "Do not show",
                ),
            ),
        )
        server.enqueue(
            MockResponse().setResponseCode(200).setBody(
                """{"thread":{},"messages":[],"approvals":[],"streamId":"stream_wire_android","cursor":9}""",
            ),
        )

        val snapshot = gateway().loadConversation(ThreadKey("thr_wire_01"))

        assertTrue(snapshot.messages.isEmpty())
        assertTrue(store.loadApprovalCommands().isEmpty())
    }

    @Test
    fun `approval action survives adapter restart and duplicate tap posts only once`() = runBlocking {
        server.dispatcher = approvalDispatcher()
        val gateway = gateway()
        val requested = withTimeout(5_000) {
            gateway.observeConversation(ThreadKey("thr_wire_01"), "stream_wire_android", 0).first()
        } as ConversationEvent.MessageAdded
        val originalAction = (requested.message.blocks["approval-actions"] as ActionsBlock)
            .items.first { it.id == "approve" }

        val restartedGateway = gateway()
        val snapshot = restartedGateway.loadConversation(ThreadKey("thr_wire_01"))
        val restoredAction = (snapshot.messages.single().blocks["approval-actions"] as ActionsBlock)
            .items.first { it.id == "approve" }
        assertEquals(originalAction.actionRef, restoredAction.actionRef)
        assertFalse(restoredAction.actionRef.contains("action_token"))

        restartedGateway.performAction(restoredAction.actionRef)
        val duplicate = runCatching { restartedGateway.performAction(restoredAction.actionRef) }.exceptionOrNull()
        assertTrue(duplicate is GatewayTransportException)

        val requests = List(3) { server.takeRequest(2, TimeUnit.SECONDS)!! }
        val decision = requests.single { it.path == "/v1/approvals/approval_wire_01/decision" }
        val body = decision.body.readUtf8()
        assertTrue(body.contains("\"decision\":\"approve\""))
        assertTrue(body.contains("\"actionToken\":\"action_token_wire_0123456789\""))
        assertEquals(null, server.takeRequest(300, TimeUnit.MILLISECONDS))
    }

    @Test
    fun `reject action uses frozen decision endpoint and token`() = runBlocking {
        server.dispatcher = approvalDispatcher()
        val gateway = gateway()
        val requested = withTimeout(5_000) {
            gateway.observeConversation(ThreadKey("thr_wire_01"), "stream_wire_android", 0).first()
        } as ConversationEvent.MessageAdded
        val action = (requested.message.blocks["approval-actions"] as ActionsBlock)
            .items.first { it.id == "reject" }

        gateway.performAction(action.actionRef)

        val requests = List(2) { server.takeRequest(2, TimeUnit.SECONDS)!! }
        val decision = requests.single { it.path == "/v1/approvals/approval_wire_01/decision" }
        val body = decision.body.readUtf8()
        assertTrue(body.contains("\"decision\":\"reject\""))
        assertTrue(body.contains("\"actionToken\":\"action_token_wire_0123456789\""))
    }

    @Test
    fun `definite decision transport failure keeps durable action for retry`() = runBlocking {
        val decisions = AtomicInteger()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse = when {
                request.requestUrl?.encodedPath == "/v1/events" -> MockResponse()
                    .withWebSocketUpgrade(object : WebSocketListener() {
                        override fun onOpen(webSocket: WebSocket, response: okhttp3.Response) {
                            webSocket.send(activeApprovalEvent())
                        }
                        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                            webSocket.close(code, null)
                        }
                    })
                request.path == "/v1/approvals/approval_wire_01/decision" -> {
                    if (decisions.incrementAndGet() == 1) {
                        MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AFTER_REQUEST)
                    } else {
                        MockResponse().setResponseCode(200).setBody(goldenDecisionResponse())
                    }
                }
                else -> MockResponse().setResponseCode(404)
            }
        }
        val gateway = gateway()
        val requested = withTimeout(5_000) {
            gateway.observeConversation(ThreadKey("thr_wire_01"), "stream_wire_android", 0).first()
        } as ConversationEvent.MessageAdded
        val action = (requested.message.blocks["approval-actions"] as ActionsBlock).items.first()

        assertTrue(runCatching { gateway.performAction(action.actionRef) }.isFailure)
        assertTrue(store.loadApprovalCommands().isNotEmpty())
        gateway.performAction(action.actionRef)

        assertEquals(2, decisions.get())
        assertTrue(store.loadApprovalCommands().isEmpty())
    }

    @Test
    fun `switch after action post-check cannot remove colliding B action state`() = runBlocking {
        val completionWriteReached = CountDownLatch(1)
        val releaseCompletionWrite = CountDownLatch(1)
        val commandA = PendingApprovalCommand(
            sessionScope = store.loadSession()!!.scopeKey(),
            actionRef = "action-a",
            approvalId = "approval-a",
            threadId = "shared-thread",
            decision = "approve",
            actionToken = "secret-a",
            expiresAtMillis = Long.MAX_VALUE,
            summary = "A",
            reason = "A only",
        )
        store.replaceApprovalCommandsForScope(commandA.sessionScope, listOf(commandA))
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                if (request.path != "/v1/approvals/approval-a/decision") {
                    return MockResponse().setResponseCode(404)
                }
                return MockResponse().setResponseCode(200).setBody("{}")
            }
        }
        val remoteA = RemoteCodexGateway(
            sessionStore = store,
            session = store.loadSession()!!,
            deviceProofKey = deviceProofKey,
            client = client,
            autoRefresh = false,
            beforeApprovalStoreWrite = { kind ->
                if (kind == RemoteApprovalStoreWrite.ACTION_COMPLETION) {
                    completionWriteReached.countDown()
                    releaseCompletionWrite.await(5, TimeUnit.SECONDS)
                }
            },
        )
        val result = async(Dispatchers.IO) { runCatching { remoteA.performAction("action-a") } }
        assertTrue(completionWriteReached.await(5, TimeUnit.SECONDS))
        val sessionB = store.loadSession()!!.copy(
            accessToken = "b-access",
            refreshToken = "b-refresh",
            sessionScope = "scope-b",
        )
        val commandB = commandA.copy(
            sessionScope = "scope-b",
            actionToken = "secret-b",
            summary = "B",
            reason = "B only",
        )
        store.saveSession(sessionB)
        store.replaceApprovalCommandsForScope(commandB.sessionScope, listOf(commandB))

        releaseCompletionWrite.countDown()
        assertTrue(withTimeout(5_000) { result.await() }.isFailure)

        assertEquals("scope-b", store.loadSession()?.sessionScope)
        assertEquals(listOf(commandB), store.loadApprovalCommands())
    }

    @Test
    fun `resolved approval golden disables every projected action`() = runBlocking {
        server.dispatcher = approvalLifecycleDispatcher("approval_resolved_event.json")
        val events = withTimeout(5_000) {
            gateway().observeConversation(ThreadKey("thr_wire_01"), "stream_wire_android", 0)
                .take(2)
                .toList()
        }
        var state = ConversationReducer.fromSnapshot(
            ConversationSnapshot(ThreadKey("thr_wire_01"), "stream_wire_android", 0, emptyList()),
        )
        events.forEach { state = ConversationReducer.reduce(state, it) }

        val actions = state.messages.single().blocks["approval-actions"] as ActionsBlock
        assertTrue(actions.items.all { !it.enabled })
    }

    @Test
    fun `expired approval golden resolves both actions and clears durable commands`() = runBlocking {
        // This test isolates approval projection. The source golden is seq=3, so make its
        // envelope consecutive here; actual seq=1 -> seq=3 delivery is separately required
        // to fail closed into SyncRequired by the ordering tests.
        server.dispatcher = approvalLifecycleDispatcher("approval_expired_event.json") { raw ->
            raw.replace("\"seq\": 3", "\"seq\": 2")
        }
        val events = withTimeout(5_000) {
            gateway().observeConversation(ThreadKey("thr_wire_01"), "stream_wire_android", 0)
                .take(2)
                .toList()
        }

        val terminal = events.last() as ConversationEvent.MessagePatched
        val resolvedIds = terminal.operations.filterIsInstance<PatchOperation.ResolveAction>()
            .map(PatchOperation.ResolveAction::actionId)
        assertEquals(listOf("approve", "reject"), resolvedIds)
        assertTrue(store.loadApprovalCommands().isEmpty())
    }

    @Test
    fun `WSS 4401 performs one refresh and one authenticated reconnect`() = runBlocking {
        val socketCalls = AtomicInteger()
        val refreshCalls = AtomicInteger()
        val proofRequests = ConcurrentLinkedQueue<RecordedRequest>()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                proofRequests += request
                return when {
                request.path == "/v1/auth/refresh" -> {
                    refreshCalls.incrementAndGet()
                    MockResponse().setResponseCode(200).setBody(refreshResponse())
                }
                request.requestUrl?.encodedPath == "/v1/events" -> {
                    val attempt = socketCalls.incrementAndGet()
                    MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
                        override fun onOpen(webSocket: WebSocket, response: okhttp3.Response) {
                            if (attempt == 1) webSocket.close(4401, "") else webSocket.send(snapshotEvent())
                        }

                        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                            webSocket.close(code, null)
                        }
                    })
                }
                else -> MockResponse().setResponseCode(404)
                }
            }
        }

        val event = withTimeout(5_000) {
            gateway().observeConversation(ThreadKey("thr_public"), "stream_public", 0).first()
        }

        assertTrue(event is ConversationEvent.MessageAdded)
        assertEquals(1, refreshCalls.get())
        assertEquals(2, socketCalls.get())
        val firstSocket = proofRequests.elementAt(0)
        val refresh = proofRequests.elementAt(1)
        val secondSocket = proofRequests.elementAt(2)
        assertValidProof(firstSocket, "test-access-token-never-rendered")
        assertValidProof(refresh, "")
        assertValidProof(secondSocket, "rotated-access-token")
        assertEquals(
            3,
            proofRequests.mapTo(mutableSetOf()) {
                it.getHeader(DeviceProofV1.HEADER_NONCE)
            }.size,
        )
    }

    @Test
    fun `WSS 4409 emits one sync-required event without reconnect loop`() = runBlocking {
        val socketCalls = AtomicInteger()
        server.dispatcher = reconnectingSocketDispatcher(socketCalls) { _, webSocket ->
            webSocket.close(4409, "snapshot-required")
        }

        val event = withTimeout(5_000) {
            gateway().observeConversation(ThreadKey("thr_public"), "stream_public", 7).first()
        }

        assertTrue(event is ConversationEvent.SyncRequired)
        assertEquals("stream_public", event.streamId)
        assertEquals(8, event.streamSeq)
        assertEquals(1, socketCalls.get())
    }

    @Test
    fun `WSS handshake 401 performs one refresh and one reconnect`() = runBlocking {
        val socketCalls = AtomicInteger()
        val refreshCalls = AtomicInteger()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse = when {
                request.path == "/v1/auth/refresh" -> {
                    refreshCalls.incrementAndGet()
                    MockResponse().setResponseCode(200).setBody(refreshResponse())
                }
                request.requestUrl?.encodedPath == "/v1/events" -> {
                    if (socketCalls.incrementAndGet() == 1) {
                        MockResponse().setResponseCode(401)
                    } else {
                        MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
                            override fun onOpen(webSocket: WebSocket, response: okhttp3.Response) {
                                webSocket.send(snapshotEvent())
                            }

                            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                                webSocket.close(code, null)
                            }
                        })
                    }
                }
                else -> MockResponse().setResponseCode(404)
            }
        }

        val event = withTimeout(5_000) {
            gateway().observeConversation(ThreadKey("thr_public"), "stream_public", 0).first()
        }

        assertTrue(event is ConversationEvent.MessageAdded)
        assertEquals(1, refreshCalls.get())
        assertEquals(2, socketCalls.get())
    }

    @Test
    fun `HTTP 429 golden preserves client id and exposes bounded retry hint`() = runBlocking {
        val goldenResponse = Json.parseToJsonElement(golden("rate_limited_http.json"))
            .jsonObject.getValue("response").jsonObject
        val retryAfter = goldenResponse.getValue("headers").jsonObject
            .getValue("Retry-After").jsonPrimitive.content
        server.enqueue(
            MockResponse()
                .setResponseCode(goldenResponse.getValue("status").jsonPrimitive.content.toInt())
                .addHeader("Retry-After", retryAfter)
                .setBody(goldenResponse.getValue("body").toString()),
        )

        val error = runCatching {
            gateway().sendTurn(ThreadKey("thr_public"), "hello", "same-client-id")
        }.exceptionOrNull() as GatewayRequestException

        assertEquals("RATE_LIMITED", error.code)
        assertTrue(error.retryable)
        assertEquals(30_000L, error.retryAfterMillis)
        val request = server.takeRequest(2, TimeUnit.SECONDS)!!
        assertTrue(request.body.readUtf8().contains("\"clientMessageId\":\"same-client-id\""))
    }

    @Test
    fun `HTTP 429 honors valid sixty second hint above local reconnect cap`() = runBlocking {
        server.enqueue(
            MockResponse()
                .setResponseCode(429)
                .addHeader("Retry-After", "60")
                .setBody("not-json"),
        )

        val error = runCatching {
            gateway().sendTurn(ThreadKey("thr_public"), "hello", "sixty-second-id")
        }.exceptionOrNull() as GatewayRequestException

        assertEquals("RATE_LIMITED", error.code)
        assertTrue(error.retryable)
        assertEquals(60_000L, error.retryAfterMillis)
    }

    @Test
    fun `malformed remote error body falls back through bounded codec`() = runBlocking {
        server.enqueue(
            MockResponse()
                .setResponseCode(400)
                .setBody("{\"error\":{\"code\":1,\"retryable\":" + "9".repeat(2_048) + "}}"),
        )

        val error = runCatching {
            gateway().sendTurn(ThreadKey("thr_public"), "hello", "malformed-error-id")
        }.exceptionOrNull() as GatewayRequestException

        assertEquals("REQUEST_REJECTED", error.code)
        assertFalse(error.retryable)
    }

    @Test
    fun `unexpected call execute failure still releases active ownership`() = runBlocking {
        val throwingClient = client.newBuilder()
            .addInterceptor { throw IllegalStateException("synthetic execute failure") }
            .build()
        val remote = RemoteCodexGateway(
            sessionStore = store,
            session = store.loadSession()!!,
            deviceProofKey = deviceProofKey,
            client = throwingClient,
            autoRefresh = false,
        )

        val failure = runCatching {
            remote.sendTurn(ThreadKey("thr_public"), "hello", "runtime-failure-id")
        }.exceptionOrNull()
        val activeCallsField = RemoteCodexGateway::class.java.getDeclaredField("activeCalls")
            .apply { isAccessible = true }
        val activeCalls = activeCallsField.get(remote) as Set<*>

        assertTrue(failure is IllegalStateException)
        assertTrue(activeCalls.isEmpty())
        remote.close()
    }

    @Test
    fun `approval HTTP 429 keeps same durable action across adapter restart`() = runBlocking {
        val decisionCalls = AtomicInteger()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse = when {
                request.requestUrl?.encodedPath == "/v1/events" -> MockResponse()
                    .withWebSocketUpgrade(object : WebSocketListener() {
                        override fun onOpen(webSocket: WebSocket, response: okhttp3.Response) {
                            webSocket.send(activeApprovalEvent())
                        }

                        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                            webSocket.close(code, null)
                        }
                    })
                request.path == "/v1/threads/thr_wire_01" -> MockResponse().setResponseCode(200)
                    .setBody(threadDetailWithPendingApproval())
                request.path == "/v1/approvals/approval_wire_01/decision" -> {
                    decisionCalls.incrementAndGet()
                    rateLimitedResponse()
                }
                else -> MockResponse().setResponseCode(404)
            }
        }
        val firstGateway = gateway()
        val requested = withTimeout(5_000) {
            firstGateway.observeConversation(ThreadKey("thr_wire_01"), "stream_wire_android", 0).first()
        } as ConversationEvent.MessageAdded
        val action = (requested.message.blocks["approval-actions"] as ActionsBlock).items.first()

        val firstError = runCatching { firstGateway.performAction(action.actionRef) }
            .exceptionOrNull() as GatewayRequestException
        val persisted = store.loadApprovalCommands().first { it.actionRef == action.actionRef }
        assertEquals("RATE_LIMITED", firstError.code)
        assertTrue(persisted.retryNotBeforeMillis > System.currentTimeMillis())

        val secondError = runCatching { firstGateway.performAction(action.actionRef) }
            .exceptionOrNull() as GatewayRequestException
        assertEquals("RATE_LIMITED", secondError.code)
        assertEquals(1, decisionCalls.get())

        val snapshot = gateway().loadConversation(ThreadKey("thr_wire_01"))
        val restored = (snapshot.messages.single().blocks["approval-actions"] as ActionsBlock)
            .items.first { it.id == action.id }
        assertEquals(action.actionRef, restored.actionRef)
        assertEquals(persisted.retryNotBeforeMillis, restored.retryNotBeforeMillis)
    }

    @Test
    fun `WSS 4429 golden waits server hint before reconnect`() = runBlocking {
        val websocketGolden = Json.parseToJsonElement(golden("websocket_rate_limited.json")).jsonObject
        val closeCode = websocketGolden.getValue("closeCode").jsonPrimitive.content.toInt()
        val closeReason = websocketGolden.getValue("closeReason").jsonPrimitive.content
        val socketCalls = AtomicInteger()
        val delays = ConcurrentLinkedQueue<Long>()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                if (request.requestUrl?.encodedPath != "/v1/events") return MockResponse().setResponseCode(404)
                val attempt = socketCalls.incrementAndGet()
                return MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: okhttp3.Response) {
                        if (attempt == 1) webSocket.close(closeCode, closeReason) else webSocket.send(snapshotEvent())
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, null)
                    }
                })
            }
        }

        val event = withTimeout(5_000) {
            deterministicGateway(delays).observeConversation(
                ThreadKey("thr_public"),
                "stream_public",
                0,
            ).first()
        }

        assertTrue(event is ConversationEvent.MessageAdded)
        assertEquals(2, socketCalls.get())
        assertEquals(listOf(30_000L), delays.toList())
    }

    @Test
    fun `consecutive malformed 4429 closes use increasing bounded backoff`() = runBlocking {
        val socketCalls = AtomicInteger()
        val delays = ConcurrentLinkedQueue<Long>()
        server.dispatcher = reconnectingSocketDispatcher(socketCalls) { attempt, webSocket ->
            if (attempt <= 2) webSocket.close(4429, "retry-after=bad") else webSocket.send(snapshotEvent())
        }

        withTimeout(5_000) {
            deterministicGateway(delays).observeConversation(
                ThreadKey("thr_public"),
                "stream_public",
                0,
            ).first()
        }

        assertEquals(3, socketCalls.get())
        assertEquals(listOf(750L, 1_500L), delays.toList())
    }

    @Test
    fun `decoded event resets reconnect backoff before later 4429`() = runBlocking {
        val socketCalls = AtomicInteger()
        val delays = ConcurrentLinkedQueue<Long>()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                if (request.requestUrl?.encodedPath != "/v1/events") return MockResponse().setResponseCode(404)
                val attempt = socketCalls.incrementAndGet()
                return MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: okhttp3.Response) {
                        when (attempt) {
                            1 -> webSocket.close(4429, "retry-after=bad")
                            2 -> webSocket.send(snapshotEvent())
                            else -> webSocket.send(patchEvent())
                        }
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        if (attempt == 2 && text.contains("\"seq\":1")) {
                            webSocket.close(4429, "retry-after=bad")
                        }
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, null)
                    }
                })
            }
        }

        val events = withTimeout(5_000) {
            deterministicGateway(delays).observeConversation(
                ThreadKey("thr_public"),
                "stream_public",
                0,
            ).take(2).toList()
        }

        assertEquals(2, events.size)
        assertEquals(3, socketCalls.get())
        assertEquals(listOf(750L, 750L), delays.toList())
    }

    @Test
    fun `cancelling flow during 4429 delay cancels the pending reconnect`() = runBlocking {
        val socketCalls = AtomicInteger()
        server.dispatcher = reconnectingSocketDispatcher(socketCalls) { _, webSocket ->
            webSocket.close(4429, "retry-after=30")
        }
        val job = launch {
            gateway().observeConversation(ThreadKey("thr_public"), "stream_public", 0).collect { }
        }
        withTimeout(5_000) {
            while (socketCalls.get() < 1) delay(10)
        }

        job.cancelAndJoin()
        delay(150)

        assertEquals(1, socketCalls.get())
    }

    @Test
    fun `closing remote gateway cancels active websocket and prevents reconnect`() = runBlocking {
        val socketCalls = AtomicInteger()
        server.dispatcher = reconnectingSocketDispatcher(socketCalls) { _, _ -> Unit }
        val remote = gateway()
        val job = launch {
            remote.observeConversation(ThreadKey("thr_public"), "stream_public", 0).collect { }
        }
        withTimeout(5_000) {
            while (socketCalls.get() < 1) delay(10)
        }

        remote.close()
        withTimeout(5_000) { job.join() }
        delay(150)

        assertEquals(1, socketCalls.get())
        assertEquals(GatewayConnectionState.OFFLINE, remote.connectionState.value)
    }

    @Test
    fun `late second 4401 from A cannot clear durable B session`() = runBlocking {
        val socketCalls = AtomicInteger()
        val secondSocketReady = CountDownLatch(1)
        val releaseSecondClose = CountDownLatch(1)
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse = when {
                request.path == "/v1/auth/refresh" -> MockResponse().setResponseCode(200)
                    .setBody(refreshResponse())
                request.requestUrl?.encodedPath == "/v1/events" -> {
                    val attempt = socketCalls.incrementAndGet()
                    MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
                        override fun onOpen(webSocket: WebSocket, response: okhttp3.Response) {
                            if (attempt == 1) {
                                webSocket.close(4401, "")
                            } else {
                                secondSocketReady.countDown()
                                releaseSecondClose.await(5, TimeUnit.SECONDS)
                                webSocket.close(4401, "")
                            }
                        }

                        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                            webSocket.close(code, null)
                        }
                    })
                }
                else -> MockResponse().setResponseCode(404)
            }
        }
        val remoteA = gateway()
        val collector = launch(Dispatchers.IO) {
            remoteA.observeConversation(ThreadKey("thr_public"), "stream_public", 0).collect { }
        }
        assertTrue(secondSocketReady.await(5, TimeUnit.SECONDS))
        val sessionB = store.loadSession()!!.copy(
            accessToken = "b-access",
            refreshToken = "b-refresh",
            sessionScope = "scope-b",
        )
        store.saveSession(sessionB)

        releaseSecondClose.countDown()
        withTimeout(5_000) { collector.join() }

        assertEquals(2, socketCalls.get())
        assertEquals("scope-b", store.loadSession()?.sessionScope)
        assertEquals("b-access", store.loadSession()?.accessToken)
    }

    @Test
    fun `close during HTTP pre-registration barrier cancels ownership without request`() = runBlocking {
        val registrationReached = CountDownLatch(1)
        val releaseRegistration = CountDownLatch(1)
        val remoteA = RemoteCodexGateway(
            sessionStore = store,
            session = store.loadSession()!!,
            deviceProofKey = deviceProofKey,
            client = client,
            autoRefresh = false,
            beforeOperationRegistration = { kind ->
                if (kind == RemoteOperationRegistration.HTTP) {
                    registrationReached.countDown()
                    releaseRegistration.await(5, TimeUnit.SECONDS)
                }
            },
        )
        val send = async(Dispatchers.IO) {
            runCatching { remoteA.sendTurn(ThreadKey("thr_public"), "A only", "a-http-id") }
        }
        assertTrue(registrationReached.await(5, TimeUnit.SECONDS))

        remoteA.close()
        releaseRegistration.countDown()
        val result = withTimeout(5_000) { send.await() }

        assertTrue(result.isFailure)
        assertEquals(null, server.takeRequest(300, TimeUnit.MILLISECONDS))
    }

    @Test
    fun `close during websocket pre-registration barrier prevents handshake`() = runBlocking {
        val registrationReached = CountDownLatch(1)
        val releaseRegistration = CountDownLatch(1)
        val remoteA = RemoteCodexGateway(
            sessionStore = store,
            session = store.loadSession()!!,
            deviceProofKey = deviceProofKey,
            client = client,
            autoRefresh = false,
            beforeOperationRegistration = { kind ->
                if (kind == RemoteOperationRegistration.WEBSOCKET) {
                    registrationReached.countDown()
                    releaseRegistration.await(5, TimeUnit.SECONDS)
                }
            },
        )
        val collector = launch(Dispatchers.IO) {
            remoteA.observeConversation(ThreadKey("thr_public"), "stream_public", 0).collect { }
        }
        assertTrue(registrationReached.await(5, TimeUnit.SECONDS))

        remoteA.close()
        releaseRegistration.countDown()
        withTimeout(5_000) { collector.join() }

        assertEquals(null, server.takeRequest(300, TimeUnit.MILLISECONDS))
    }

    @Test
    fun `WSS approval projection cannot write A command after B session is saved`() = runBlocking {
        val projectionWriteReached = CountDownLatch(1)
        val releaseProjectionWrite = CountDownLatch(1)
        server.dispatcher = approvalDispatcher()
        val remoteA = RemoteCodexGateway(
            sessionStore = store,
            session = store.loadSession()!!,
            deviceProofKey = deviceProofKey,
            client = client,
            autoRefresh = false,
            beforeApprovalStoreWrite = { kind ->
                if (kind == RemoteApprovalStoreWrite.PROJECT_EVENT) {
                    projectionWriteReached.countDown()
                    releaseProjectionWrite.await(5, TimeUnit.SECONDS)
                }
            },
        )
        val projected = async(Dispatchers.IO) {
            withTimeout(5_000) {
                remoteA.observeConversation(ThreadKey("thr_wire_01"), "stream_wire_android", 0).first()
            }
        }
        assertTrue(projectionWriteReached.await(5, TimeUnit.SECONDS))

        val sessionB = store.loadSession()!!.copy(
            accessToken = "b-access",
            refreshToken = "b-refresh",
            sessionScope = "scope-b",
        )
        val commandB = PendingApprovalCommand(
            sessionScope = sessionB.scopeKey(),
            actionRef = "b-action-ref",
            approvalId = "approval_wire_01",
            threadId = "thr_wire_01",
            decision = "approve",
            actionToken = "b-action-token",
            expiresAtMillis = Long.MAX_VALUE,
            summary = "B command",
            reason = "B only",
        )
        store.saveSession(sessionB)
        store.replaceApprovalCommandsForScope(sessionB.scopeKey(), listOf(commandB))

        releaseProjectionWrite.countDown()
        assertTrue(withTimeout(5_000) { projected.await() } is ConversationEvent.NoOp)
        assertEquals("scope-b", store.loadSession()?.scopeKey())
        assertEquals(listOf(commandB), store.loadApprovalCommands())
    }

    private fun approvalDispatcher(): Dispatcher = object : Dispatcher() {
        override fun dispatch(request: RecordedRequest): MockResponse = when {
            request.requestUrl?.encodedPath == "/v1/events" -> MockResponse()
                .withWebSocketUpgrade(object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: okhttp3.Response) {
                        webSocket.send(activeApprovalEvent())
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, null)
                    }
                })
            request.path == "/v1/threads/thr_wire_01" -> MockResponse().setResponseCode(200)
                .setBody(threadDetailWithPendingApproval())
            request.path == "/v1/approvals/approval_wire_01/decision" -> MockResponse().setResponseCode(200)
                .setBody(goldenDecisionResponse())
            else -> MockResponse().setResponseCode(404)
        }
    }

    private fun approvalLifecycleDispatcher(
        terminal: String,
        transformTerminal: (String) -> String = { it },
    ): Dispatcher = object : Dispatcher() {
        override fun dispatch(request: RecordedRequest): MockResponse {
            if (request.requestUrl?.encodedPath != "/v1/events") return MockResponse().setResponseCode(404)
            return MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: okhttp3.Response) {
                    webSocket.send(activeApprovalEvent())
                    webSocket.send(transformTerminal(golden(terminal)))
                }

                override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                    webSocket.close(code, null)
                }
            })
        }
    }

    private fun golden(name: String): String = findRepositoryFile("gateway/tests/goldens/$name").readText()

    private fun goldenDecisionResponse(): String =
        Json.parseToJsonElement(golden("approval_decision_http.json"))
            .jsonObject.getValue("response").jsonObject.getValue("body").toString()

    private fun rateLimitedResponse(): MockResponse {
        val response = Json.parseToJsonElement(golden("rate_limited_http.json"))
            .jsonObject.getValue("response").jsonObject
        return MockResponse()
            .setResponseCode(response.getValue("status").jsonPrimitive.content.toInt())
            .addHeader(
                "Retry-After",
                response.getValue("headers").jsonObject.getValue("Retry-After").jsonPrimitive.content,
            )
            .setBody(response.getValue("body").toString())
    }

    private fun threadDetailWithPendingApproval(): String {
        val approval = kotlinx.serialization.json.Json.parseToJsonElement(
            activeApprovalEvent(),
        ).jsonObject.getValue("payload")
        return """{
            "thread":{},"messages":[],"approvals":[$approval],
            "streamId":"stream_wire_android","cursor":1
        }"""
    }

    /** Keeps the protocol golden immutable while preventing this wall-clock test from expiring. */
    private fun activeApprovalEvent(): String = golden("approval_requested_event.json")
        .replace("2026-07-19T12:05:00Z", "2099-07-19T12:05:00Z")

    private fun findRepositoryFile(relativePath: String): File {
        val workingDirectory = requireNotNull(System.getProperty("user.dir"))
        var cursor: File? = File(workingDirectory).absoluteFile
        while (cursor != null) {
            val candidate = File(cursor, relativePath)
            if (candidate.isFile) return candidate
            cursor = cursor.parentFile
        }
        error("Repository fixture not found")
    }

    private fun outboxEntry(clientMessageId: String, sessionScope: String): OutboxEntry = OutboxEntry(
        clientMessageId = clientMessageId,
        sessionScope = sessionScope,
        threadKey = ThreadKey("thread-$clientMessageId"),
        text = "text-$clientMessageId",
        state = OutboxState.QUEUED,
        createdAtMillis = 1,
    )

    private fun assertValidProof(request: RecordedRequest, bearerToken: String) {
        val timestamp = requireNotNull(request.getHeader(DeviceProofV1.HEADER_TIMESTAMP))
        val nonce = requireNotNull(request.getHeader(DeviceProofV1.HEADER_NONCE))
        val signature = Base64.getDecoder().decode(
            requireNotNull(request.getHeader(DeviceProofV1.HEADER_SIGNATURE)),
        )
        assertEquals(DeviceProofV1.VERSION, request.getHeader(DeviceProofV1.HEADER_VERSION))
        assertEquals(24, Base64.getDecoder().decode(nonce).size)
        EcdsaP256Der.parseStrict(signature, requireLowS = true)
        val canonical = DeviceProofV1.canonicalBytes(
            method = request.method ?: error("Missing method"),
            rawTarget = (request.path ?: error("Missing request target")).toByteArray(Charsets.US_ASCII),
            timestamp = timestamp,
            nonce = nonce,
            body = request.body.clone().readByteArray(),
            bearerToken = bearerToken,
        )
        assertTrue(deviceProofKey.verifySha256(canonical, signature))
    }

    private fun gateway(): RemoteCodexGateway = RemoteCodexGateway(
        sessionStore = store,
        session = store.loadSession()!!,
        deviceProofKey = deviceProofKey,
        client = client,
        autoRefresh = false,
    )

    private fun deterministicGateway(delays: ConcurrentLinkedQueue<Long>): RemoteCodexGateway =
        RemoteCodexGateway(
            sessionStore = store,
            session = store.loadSession()!!,
            deviceProofKey = deviceProofKey,
            client = client,
            autoRefresh = false,
            reconnectBackoffFactory = { BoundedReconnectBackoff(random = { 0.5 }) },
            reconnectDelay = { delays += it },
        )

    private fun reconnectingSocketDispatcher(
        socketCalls: AtomicInteger,
        onOpen: (Int, WebSocket) -> Unit,
    ): Dispatcher = object : Dispatcher() {
        override fun dispatch(request: RecordedRequest): MockResponse {
            if (request.requestUrl?.encodedPath != "/v1/events") return MockResponse().setResponseCode(404)
            val attempt = socketCalls.incrementAndGet()
            return MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: okhttp3.Response) {
                    onOpen(attempt, webSocket)
                }

                override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                    webSocket.close(code, null)
                }
            })
        }
    }

    private fun threadList(): String = """{
        "data":[{
            "id":"thr_public",
            "title":"Remote test",
            "preview":"Ready",
            "createdAt":"2026-07-19T00:00:00Z",
            "updatedAt":"2026-07-19T00:01:00Z",
            "status":"idle",
            "lastTurnId":null,
            "unread":false
        }]
    }"""

    private fun snapshotEvent(): String = """{
        "v":1,"streamId":"stream_public","eventId":"event_1","seq":1,
        "occurredAt":"2026-07-19T00:01:01Z","type":"message.snapshot",
        "threadId":"thr_public","turnId":"turn_public","itemId":"item_public",
        "payload":{
            "schema":"cheby.rich-message/1.0","messageId":"msg_public",
            "threadId":"thr_public","turnId":"turn_public","sourceItemId":"item_public",
            "role":"assistant","state":"streaming","revision":0,
            "rootBlockIds":["answer"],
            "blocks":{"answer":{"blockId":"answer","type":"text","text":"Hello","fallbackText":"Hello"}},
            "fallback":{"text":"Hello"},
            "createdAt":"2026-07-19T00:01:00Z","updatedAt":"2026-07-19T00:01:01Z"
        }
    }"""

    private fun ackSequence(raw: String): Long = Json.parseToJsonElement(raw)
        .jsonObject.getValue("seq").jsonPrimitive.content.toLong()

    private fun patchEvent(): String = """{
        "v":1,"streamId":"stream_public","eventId":"event_2","seq":2,
        "occurredAt":"2026-07-19T00:01:02Z","type":"message.patch",
        "threadId":"thr_public","turnId":"turn_public","itemId":"item_public",
        "payload":{"messageId":"msg_public","baseRevision":0,"nextRevision":1,
            "ops":[{"op":"block.text.append","blockId":"answer","value":" world"}]}
    }"""

    private fun tokenExpired(): String =
        """{"error":{"code":"TOKEN_EXPIRED","message":"Access token has expired","retryable":false}}"""

    private fun refreshResponse(): String = """{
        "deviceId":"device_public",
        "accessToken":"rotated-access-token",
        "expiresAt":"2026-07-20T01:00:00Z",
        "refreshToken":"rotated-refresh-token",
        "refreshExpiresAt":"2026-08-19T00:00:00Z",
        "streamId":"stream_public"
    }"""

    private fun pairingResponse(): String = refreshResponse()
}
