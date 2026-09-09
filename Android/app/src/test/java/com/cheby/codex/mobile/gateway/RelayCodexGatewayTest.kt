package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.data.InMemorySecureRelayConfigStore
import com.cheby.codex.mobile.data.RelayDeviceSession
import com.cheby.codex.mobile.model.ActionsBlock
import com.cheby.codex.mobile.model.ConversationEvent
import com.cheby.codex.mobile.model.GatewayConnectionState
import com.cheby.codex.mobile.model.ThreadKey
import com.cheby.codex.mobile.model.ThreadRunState
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.filter
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.OkHttpClient
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.tls.HandshakeCertificates
import okhttp3.tls.HeldCertificate
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

class RelayCodexGatewayTest {
    private val assistantId = "asst_0000000000000000000000"
    private val deviceId = "dev_0000000000000000000000"

    @Test(timeout = 10_000)
    fun `matching terminal lifecycle makes inactive catalog thread idle`() = runBlocking {
        val fixture = fixture()
        val store = InMemorySecureRelayConfigStore(session(fixture.origin))
        val peer = RelayPeer(
            store,
            assistantId,
            deviceId,
            threadStatus = "running",
            threadLastTurnId = "turn-current",
            lifecycleEventsAfterThreadRead = listOf(
                TestLifecycleEvent(1, "turn.completed", "thr_public", "turn-current"),
            ),
        )
        fixture.server.enqueue(MockResponse().withWebSocketUpgrade(peer))
        val gateway = gateway(store, fixture)
        try {
            withTimeout(5_000) {
                gateway.connectionState.filter { it == GatewayConnectionState.ONLINE }.first()
                gateway.threads.filter { it.singleOrNull()?.state == ThreadRunState.RUNNING }.first()
            }

            // The selected conversation is deliberately not the lifecycle event's thread.
            val selected = ThreadKey("thr_selected_elsewhere")
            val snapshot = gateway.loadConversation(selected)
            val lifecycleObserved = CompletableDeferred<Unit>()
            val observer = launch {
                gateway.observeConversation(selected, snapshot.streamId, snapshot.cursor).collect {
                    if (it is ConversationEvent.TurnLifecycle) lifecycleObserved.complete(Unit)
                }
            }

            withTimeout(3_000) {
                lifecycleObserved.await()
                gateway.threads.filter { it.singleOrNull()?.state == ThreadRunState.IDLE }.first()
            }
            assertEquals("turn-current", gateway.threads.value.single().lastTurnId)
            observer.cancelAndJoin()
        } finally {
            gateway.close()
            fixture.server.shutdown()
        }
    }

    @Test(timeout = 10_000)
    fun `wrong terminal turn id cannot release catalog thread`() = runBlocking {
        val fixture = fixture()
        val store = InMemorySecureRelayConfigStore(session(fixture.origin))
        val peer = RelayPeer(
            store,
            assistantId,
            deviceId,
            threadStatus = "running",
            threadLastTurnId = "turn-current",
            lifecycleEventsAfterThreadRead = listOf(
                TestLifecycleEvent(1, "turn.completed", "thr_public", "turn-stale"),
            ),
        )
        fixture.server.enqueue(MockResponse().withWebSocketUpgrade(peer))
        val gateway = gateway(store, fixture)
        try {
            withTimeout(5_000) {
                gateway.connectionState.filter { it == GatewayConnectionState.ONLINE }.first()
                gateway.threads.filter { it.singleOrNull()?.state == ThreadRunState.RUNNING }.first()
            }

            val selected = ThreadKey("thr_selected_elsewhere")
            val snapshot = gateway.loadConversation(selected)
            val lifecycleObserved = CompletableDeferred<Unit>()
            val observer = launch {
                gateway.observeConversation(selected, snapshot.streamId, snapshot.cursor).collect {
                    if (it is ConversationEvent.TurnLifecycle) lifecycleObserved.complete(Unit)
                }
            }
            withTimeout(3_000) {
                lifecycleObserved.await()
                peer.acked.filter { it >= 3 }.first()
            }

            assertEquals(ThreadRunState.RUNNING, gateway.threads.value.single().state)
            assertEquals("turn-current", gateway.threads.value.single().lastTurnId)
            observer.cancelAndJoin()
        } finally {
            gateway.close()
            fixture.server.shutdown()
        }
    }

    @Test(timeout = 15_000)
    fun `one WSS multiplexes thread and turn and durable receipt suppresses crash retry`() = runBlocking {
        val fixture = fixture()
        val store = InMemorySecureRelayConfigStore(session(fixture.origin))
        val peer = RelayPeer(store, assistantId, deviceId)
        fixture.server.enqueue(MockResponse().withWebSocketUpgrade(peer))
        fixture.server.enqueue(MockResponse().withWebSocketUpgrade(peer))
        var first: RelayCodexGateway? = null
        var second: RelayCodexGateway? = null
        try {
            val firstGateway = gateway(store, fixture)
            first = firstGateway
            withTimeout(5_000) {
                firstGateway.connectionState.filter { it == GatewayConnectionState.ONLINE }.first()
                firstGateway.threads.filter { it.isNotEmpty() }.first()
            }
            assertEquals("Relay thread", firstGateway.threads.value.single().title)
            val snapshot = firstGateway.loadConversation(ThreadKey("thr_public"))
            val actions = snapshot.messages.single().blocks.getValue("approval-actions") as ActionsBlock
            assertEquals(listOf("批准", "拒绝"), actions.items.map { it.label })
            firstGateway.performAction(actions.items.first().actionRef, firstGateway.sessionScope)
            assertEquals(1, peer.operations.count { it == "approvals.decide" })
            val limited = runCatching {
                firstGateway.sendTurn(
                    ThreadKey("thr_public"),
                    "retry",
                    "client-message-retry",
                    firstGateway.sessionScope,
                )
            }.exceptionOrNull()
            assertTrue(limited is GatewayRequestException && limited.retryable)
            firstGateway.sendTurn(
                ThreadKey("thr_public"),
                "retry",
                "client-message-retry",
                firstGateway.sessionScope,
            )
            assertEquals(2, peer.retryTurnMessageIds.toSet().size)
            firstGateway.sendTurn(
                ThreadKey("thr_public"),
                "hello",
                "client-message-stable",
                firstGateway.sessionScope,
            )
            withTimeout(3_000) { peer.acked.filter { it >= 6 }.first() }
            assertEquals(3, peer.operations.count { it == "turns.start" })
            firstGateway.close()

            val secondGateway = gateway(store, fixture)
            second = secondGateway
            withTimeout(5_000) {
                secondGateway.connectionState.filter { it == GatewayConnectionState.ONLINE }.first()
            }
            secondGateway.sendTurn(
                ThreadKey("thr_public"),
                "hello",
                "client-message-stable",
                secondGateway.sessionScope,
            )
            assertEquals(3, peer.operations.count { it == "turns.start" })
            assertTrue(peer.socketCount.get() >= 2)
        } finally {
            first?.close()
            second?.close()
            fixture.server.shutdown()
        }
    }

    @Test(timeout = 10_000)
    fun `same client ids in different threads keep distinct Relay transport identities`() = runBlocking {
        val fixture = fixture()
        val store = InMemorySecureRelayConfigStore(session(fixture.origin))
        val peer = RelayPeer(store, assistantId, deviceId)
        fixture.server.enqueue(MockResponse().withWebSocketUpgrade(peer))
        val gateway = gateway(store, fixture)
        try {
            withTimeout(5_000) {
                gateway.connectionState.filter { it == GatewayConnectionState.ONLINE }.first()
            }
            val sharedMessageId = "client-message-shared"
            val firstThread = ThreadKey("thread-first")
            val secondThread = ThreadKey("thread-second")

            val first = gateway.sendTurn(
                firstThread,
                "first",
                sharedMessageId,
                gateway.sessionScope,
            )
            val second = gateway.sendTurn(
                secondThread,
                "second",
                sharedMessageId,
                gateway.sessionScope,
            )
            val image = byteArrayOf(
                0x89.toByte(),
                0x50,
                0x4e,
                0x47,
                0x0d,
                0x0a,
                0x1a,
                0x0a,
            )
            gateway.uploadTurnImage(
                firstThread,
                sharedMessageId,
                "client-asset-shared",
                image,
                gateway.sessionScope,
                0,
            )
            gateway.uploadTurnImage(
                secondThread,
                sharedMessageId,
                "client-asset-shared",
                image,
                gateway.sessionScope,
                0,
            )

            assertEquals(firstThread, first.threadKey)
            assertEquals(secondThread, second.threadKey)
            assertEquals(2, peer.turnRequestIds.toSet().size)
            assertEquals(2, peer.turnMessageIds.toSet().size)
            assertEquals(2, peer.assetRequestIds.toSet().size)
            assertEquals(2, peer.assetMessageIds.toSet().size)
            assertEquals(2, peer.operations.count { it == "turns.start" })
            assertEquals(2, peer.operations.count { it == "assets.upload" })
        } finally {
            gateway.close()
            fixture.server.shutdown()
        }
    }

    @Test(timeout = 10_000)
    fun `writer acceptance timeout cancels blocked read and reconnects pending request`() = runBlocking {
        val fixture = fixture()
        val store = InMemorySecureRelayConfigStore(session(fixture.origin))
        val peer = RelayPeer(store, assistantId, deviceId, ignoreMessagesOnFirstSocket = true)
        fixture.server.enqueue(MockResponse().withWebSocketUpgrade(peer))
        fixture.server.enqueue(MockResponse().withWebSocketUpgrade(peer))
        val gateway = gateway(store, fixture, acceptTimeoutMillis = 120)
        try {
            withTimeout(5_000) {
                peer.socketCountFlow.filter { it >= 2 }.first()
                gateway.threads.filter { it.isNotEmpty() }.first()
            }
            assertTrue(peer.operations.count { it == "threads.list" } >= 2)
        } finally {
            gateway.close()
            fixture.server.shutdown()
        }
    }

    @Test(timeout = 10_000)
    fun `application heartbeat keeps one idle WSS alive`() = runBlocking {
        val fixture = fixture()
        val store = InMemorySecureRelayConfigStore(session(fixture.origin))
        val peer = RelayPeer(store, assistantId, deviceId)
        fixture.server.enqueue(MockResponse().withWebSocketUpgrade(peer))
        val gateway = gateway(
            store,
            fixture,
            heartbeatIntervalMillis = 60,
            heartbeatTimeoutMillis = 300,
        )
        try {
            withTimeout(3_000) { peer.pingCount.filter { it >= 2 }.first() }
            assertEquals(1, peer.socketCount.get())
        } finally {
            gateway.close()
            fixture.server.shutdown()
        }
    }

    @Test(timeout = 10_000)
    fun `new empty thread stays usable before Codex makes it catalog visible`() = runBlocking {
        val fixture = fixture()
        val store = InMemorySecureRelayConfigStore(session(fixture.origin))
        val peer = RelayPeer(store, assistantId, deviceId)
        fixture.server.enqueue(MockResponse().withWebSocketUpgrade(peer))
        val gateway = gateway(store, fixture)
        try {
            val created = gateway.createThread("Draft conversation")

            assertEquals("thr_created", created.key.value)
            assertEquals(created, gateway.threads.value.first())
            assertEquals(1, peer.operations.count { it == "threads.list" })
            assertTrue(
                peer.operations.indexOf("threads.list") < peer.operations.indexOf("threads.create"),
            )
        } finally {
            gateway.close()
            fixture.server.shutdown()
        }
    }

    @Test(timeout = 10_000)
    fun `request timeout and cancellation remove pending frames without reconnect churn`() = runBlocking {
        val fixture = fixture()
        val store = InMemorySecureRelayConfigStore(session(fixture.origin))
        val peer = RelayPeer(
            store,
            assistantId,
            deviceId,
            ignoredOperations = setOf("threads.create", "assets.upload"),
        )
        fixture.server.enqueue(MockResponse().withWebSocketUpgrade(peer))
        val gateway = gateway(store, fixture, requestTimeoutMillis = 180, acceptTimeoutMillis = 5_000)
        try {
            withTimeout(3_000) { gateway.threads.filter { it.isNotEmpty() }.first() }
            val timeout = runCatching { gateway.createThread("timeout") }.exceptionOrNull()
            assertTrue(timeout is GatewayTransportException)
            assertEquals(0, pendingRequestCount(gateway))

            val image = ByteArray(1024 * 1024).also {
                byteArrayOf(0x89.toByte(), 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a)
                    .copyInto(it)
            }
            val upload = launch {
                gateway.uploadTurnImage(
                    ThreadKey("thr_public"),
                    "client-message-cancel",
                    "client-asset-cancel",
                    image,
                    gateway.sessionScope,
                    0,
                )
            }
            withTimeout(3_000) {
                while (peer.operations.count { it == "assets.upload" } < 1) delay(10)
            }
            upload.cancelAndJoin()
            assertEquals(0, pendingRequestCount(gateway))
            delay(250)
            assertEquals(1, peer.operations.count { it == "threads.create" })
            assertEquals(1, peer.operations.count { it == "assets.upload" })
            assertEquals(1, peer.socketCount.get())
        } finally {
            gateway.close()
            fixture.server.shutdown()
        }
    }

    @Test(timeout = 10_000)
    fun `transient refresh failure preserves binding and outbox`() = runBlocking {
        val fixture = fixture()
        val initial = session(fixture.origin)
        val store = InMemorySecureRelayConfigStore(initial)
        val revoked = AtomicBoolean(false)
        fixture.server.enqueue(MockResponse().setResponseCode(401))
        fixture.server.enqueue(
            MockResponse().setResponseCode(503).setBody(
                """{"code":"RELAY_UNAVAILABLE","message":"Try later","retryable":true}""",
            ),
        )
        val gateway = RelayCodexGateway(
            sessionStore = store,
            session = initial,
            deviceProofKey = TestDeviceProofKey.generate(),
            credentialClient = RelayCredentialClient(fixture.client),
            sslSocketFactory = fixture.clientCertificates.sslSocketFactory(),
            onSessionRevoked = { revoked.set(true) },
        )
        try {
            withTimeout(4_000) {
                while (fixture.server.requestCount < 2) delay(10)
            }
            delay(100)
            assertNotNull(store.loadSession())
            assertEquals(GatewayKind.RELAY, gateway.kind.value)
            assertFalse(revoked.get())
        } finally {
            gateway.close()
            fixture.server.shutdown()
        }
    }

    @Test(timeout = 10_000)
    fun `retryable event ack failure keeps flow alive deduplicates replay and advances cursor`() =
        runBlocking {
            val fixture = fixture()
            val store = InMemorySecureRelayConfigStore(session(fixture.origin))
            val peer = RelayPeer(store, assistantId, deviceId, recoverEventAck = true)
            fixture.server.enqueue(MockResponse().withWebSocketUpgrade(peer))
            val gateway = gateway(store, fixture, requestTimeoutMillis = 2_000)
            val events = CopyOnWriteArrayList<ConversationEvent>()
            var collector: kotlinx.coroutines.Job? = null
            try {
                withTimeout(3_000) { gateway.threads.filter { it.isNotEmpty() }.first() }
                val snapshot = gateway.loadConversation(ThreadKey("thr_public"))
                collector = launch {
                    gateway.observeConversation(
                        ThreadKey("thr_public"),
                        snapshot.streamId,
                        snapshot.cursor,
                    ).collect(events::add)
                }

                withTimeout(5_000) {
                    while (store.loadSession()?.gatewayAckSeq != 2L) delay(10)
                }

                assertEquals(listOf(1L, 2L), events.map(ConversationEvent::streamSeq))
                assertEquals("stream-public", store.loadSession()?.gatewayStreamId)
                assertTrue(peer.eventAckSequences.size >= 2)
                assertEquals(1L, peer.eventAckSequences.first())
                assertEquals(2L, peer.eventAckSequences.last())
                assertTrue(peer.eventAckSequences.zipWithNext().all { (before, after) -> after >= before })
                assertEquals(1, peer.operations.count { it == "events.subscribe" })
                assertEquals(GatewayConnectionState.ONLINE, gateway.connectionState.value)
            } finally {
                collector?.cancelAndJoin()
                gateway.close()
                fixture.server.shutdown()
            }
        }

    @Test(timeout = 10_000)
    fun `event ack never advances server beyond a failed durable cursor`() = runBlocking {
        val fixture = fixture()
        val store = InMemorySecureRelayConfigStore(
            session = session(fixture.origin),
            rejectGatewayCursorAtOrAbove = 1,
        )
        val peer = RelayPeer(store, assistantId, deviceId, recoverEventAck = true)
        fixture.server.enqueue(MockResponse().withWebSocketUpgrade(peer))
        val gateway = gateway(store, fixture, requestTimeoutMillis = 500)
        val events = CopyOnWriteArrayList<ConversationEvent>()
        var collector: kotlinx.coroutines.Job? = null
        try {
            withTimeout(3_000) { gateway.threads.filter { it.isNotEmpty() }.first() }
            val snapshot = gateway.loadConversation(ThreadKey("thr_public"))
            collector = launch {
                gateway.observeConversation(
                    ThreadKey("thr_public"),
                    snapshot.streamId,
                    snapshot.cursor,
                ).collect(events::add)
            }
            withTimeout(3_000) {
                while (events.isEmpty()) delay(10)
            }
            delay(650)

            assertEquals(listOf(1L), events.map(ConversationEvent::streamSeq))
            assertEquals(0L, store.loadSession()?.gatewayAckSeq)
            assertTrue(peer.eventAckSequences.isEmpty())
        } finally {
            collector?.cancelAndJoin()
            gateway.close()
            fixture.server.shutdown()
        }
    }

    @Test(timeout = 10_000)
    fun `stalled event ack does not block later conversation events`() = runBlocking {
        val fixture = fixture()
        val store = InMemorySecureRelayConfigStore(session(fixture.origin))
        val peer = RelayPeer(
            store,
            assistantId,
            deviceId,
            burstEventsBeforeAck = true,
            stallEventAck = true,
        )
        fixture.server.enqueue(MockResponse().withWebSocketUpgrade(peer))
        val gateway = gateway(store, fixture, requestTimeoutMillis = 2_000)
        val events = CopyOnWriteArrayList<ConversationEvent>()
        var collector: kotlinx.coroutines.Job? = null
        try {
            withTimeout(3_000) { gateway.threads.filter { it.isNotEmpty() }.first() }
            val snapshot = gateway.loadConversation(ThreadKey("thr_public"))
            collector = launch {
                gateway.observeConversation(
                    ThreadKey("thr_public"),
                    snapshot.streamId,
                    snapshot.cursor,
                ).collect(events::add)
            }

            withTimeout(3_000) {
                while (events.size < 2) delay(10)
            }

            assertEquals(listOf(1L, 2L), events.map(ConversationEvent::streamSeq))
            assertTrue(peer.eventAckSequences.isNotEmpty())
        } finally {
            collector?.cancelAndJoin()
            gateway.close()
            fixture.server.shutdown()
        }
    }

    @Test(timeout = 10_000)
    fun `event ack waits until downstream reducer returns`() = runBlocking {
        val fixture = fixture()
        val store = InMemorySecureRelayConfigStore(session(fixture.origin))
        val peer = RelayPeer(
            store,
            assistantId,
            deviceId,
            burstEventsBeforeAck = true,
        )
        fixture.server.enqueue(MockResponse().withWebSocketUpgrade(peer))
        val gateway = gateway(store, fixture, requestTimeoutMillis = 2_000)
        val events = CopyOnWriteArrayList<ConversationEvent>()
        val firstEnteredReducer = CompletableDeferred<Unit>()
        val releaseReducer = CompletableDeferred<Unit>()
        var collector: kotlinx.coroutines.Job? = null
        try {
            withTimeout(3_000) { gateway.threads.filter { it.isNotEmpty() }.first() }
            val snapshot = gateway.loadConversation(ThreadKey("thr_public"))
            collector = launch {
                gateway.observeConversation(
                    ThreadKey("thr_public"),
                    snapshot.streamId,
                    snapshot.cursor,
                ).collect { event ->
                    events += event
                    if (event.streamSeq == 1L) {
                        firstEnteredReducer.complete(Unit)
                        releaseReducer.await()
                    }
                }
            }

            withTimeout(3_000) { firstEnteredReducer.await() }
            delay(200)
            assertEquals(0L, store.loadSession()?.gatewayAckSeq)
            assertTrue(peer.eventAckSequences.isEmpty())

            releaseReducer.complete(Unit)
            withTimeout(3_000) {
                while ((store.loadSession()?.gatewayAckSeq ?: 0L) < 2L) delay(10)
            }
            assertEquals(listOf(1L, 2L), events.map(ConversationEvent::streamSeq))
            assertEquals(2L, store.loadSession()?.gatewayAckSeq)
        } finally {
            collector?.cancelAndJoin()
            gateway.close()
            fixture.server.shutdown()
        }
    }

    @Test(timeout = 10_000)
    fun `snapshot observer handoff replays only events newer than snapshot cursor`() = runBlocking {
        val fixture = fixture()
        val store = InMemorySecureRelayConfigStore(session(fixture.origin))
        val peer = RelayPeer(
            store,
            assistantId,
            deviceId,
            threadSnapshotCursor = 2,
            eventsAfterThreadRead = listOf(1, 2, 3),
        )
        fixture.server.enqueue(MockResponse().withWebSocketUpgrade(peer))
        val gateway = gateway(store, fixture, requestTimeoutMillis = 2_000)
        val events = CopyOnWriteArrayList<ConversationEvent>()
        var collector: kotlinx.coroutines.Job? = null
        try {
            withTimeout(3_000) { gateway.threads.filter { it.isNotEmpty() }.first() }
            val snapshot = gateway.loadConversation(ThreadKey("thr_public"))
            assertEquals(2L, snapshot.cursor)

            // Let the socket reader consume the deliberately injected hand-off events before
            // installing the conversation observer. They must survive locally, while entries
            // already covered by the snapshot must not be projected into the reducer.
            withTimeout(3_000) {
                while ((store.loadSession()?.relayAckCursor ?: 0L) < 5L) delay(10)
            }
            collector = launch {
                gateway.observeConversation(
                    ThreadKey("thr_public"),
                    snapshot.streamId,
                    snapshot.cursor,
                ).collect(events::add)
            }

            withTimeout(3_000) {
                while (events.size < 1) delay(10)
            }
            delay(100)

            assertEquals(listOf(3L), events.map(ConversationEvent::streamSeq))
            withTimeout(3_000) {
                while ((store.loadSession()?.gatewayAckSeq ?: 0L) < 3L) delay(10)
            }
        } finally {
            collector?.cancelAndJoin()
            gateway.close()
            fixture.server.shutdown()
        }
    }

    @Test(timeout = 10_000)
    fun `late acceptance for a cancelled request does not tear down the relay socket`() =
        runBlocking {
            val fixture = fixture()
            val store = InMemorySecureRelayConfigStore(session(fixture.origin))
            val peer = RelayPeer(
                store,
                assistantId,
                deviceId,
                delayFirstAcceptanceOperation = "threads.read",
                delayFirstAcceptanceMillis = 250,
            )
            fixture.server.enqueue(MockResponse().withWebSocketUpgrade(peer))
            val gateway = gateway(store, fixture, requestTimeoutMillis = 80)
            try {
                withTimeout(3_000) { gateway.threads.filter { it.isNotEmpty() }.first() }
                val timeout = runCatching {
                    gateway.loadConversation(ThreadKey("thr_public"))
                }.exceptionOrNull()
                assertTrue(timeout is GatewayTransportException)

                withTimeout(3_000) {
                    peer.delayedAcceptanceReleased.filter { it }.first()
                }
                delay(100)
                assertEquals(GatewayConnectionState.ONLINE, gateway.connectionState.value)
                assertEquals(1L, peer.socketCount.get())

                val recovered = gateway.loadConversation(ThreadKey("thr_public"))
                assertEquals(ThreadKey("thr_public"), recovered.threadKey)
                assertEquals(1L, peer.socketCount.get())
            } finally {
                gateway.close()
                fixture.server.shutdown()
            }
        }

    @Test(timeout = 10_000)
    fun `initial catalog response timeout retries while socket and node stay online`() = runBlocking {
        val fixture = fixture()
        val store = InMemorySecureRelayConfigStore(session(fixture.origin))
        val peer = RelayPeer(
            store,
            assistantId,
            deviceId,
            stallFirstCatalogResponse = true,
        )
        fixture.server.enqueue(MockResponse().withWebSocketUpgrade(peer))
        val gateway = gateway(store, fixture, requestTimeoutMillis = 150)
        try {
            withTimeout(5_000) { gateway.threads.filter { it.isNotEmpty() }.first() }

            assertEquals(2, peer.operations.count { it == "threads.list" })
            assertEquals(1, peer.socketCount.get())
            assertEquals(GatewayConnectionState.ONLINE, gateway.connectionState.value)
        } finally {
            gateway.close()
            fixture.server.shutdown()
        }
    }

    private fun gateway(
        store: InMemorySecureRelayConfigStore,
        fixture: TlsFixture,
        requestTimeoutMillis: Long = 45_000,
        acceptTimeoutMillis: Long = 20_000,
        heartbeatIntervalMillis: Long = 20_000,
        heartbeatTimeoutMillis: Long = 10_000,
    ) = RelayCodexGateway(
        sessionStore = store,
        session = requireNotNull(store.loadSession()),
        deviceProofKey = TestDeviceProofKey.generate(),
        sslSocketFactory = fixture.clientCertificates.sslSocketFactory(),
        requestTimeoutMillis = requestTimeoutMillis,
        acceptTimeoutMillis = acceptTimeoutMillis,
        heartbeatIntervalMillis = heartbeatIntervalMillis,
        heartbeatTimeoutMillis = heartbeatTimeoutMillis,
    )

    private fun pendingRequestCount(gateway: RelayCodexGateway): Int {
        val field = RelayCodexGateway::class.java.getDeclaredField("pending").apply { isAccessible = true }
        return (field.get(gateway) as ConcurrentHashMap<*, *>).size
    }

    private fun session(origin: String) = RelayDeviceSession(
        assistantId = assistantId,
        assistantAlias = "我的 Codex",
        relayOrigin = origin,
        bindingScope = "binding-public",
        principalId = deviceId,
        accessToken = "access-token-00000000000000000000000000000000",
        refreshToken = "refresh-token-0000000000000000000000000000000",
        accessExpiresAt = System.currentTimeMillis() / 1_000 + 3_600,
        refreshExpiresAt = System.currentTimeMillis() / 1_000 + 86_400,
        gatewayStreamId = "pending",
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
        val client = OkHttpClient.Builder()
            .sslSocketFactory(clientCertificates.sslSocketFactory(), clientCertificates.trustManager)
            .hostnameVerifier { host, _ -> host == "localhost" }
            .build()
        return TlsFixture(server, client, clientCertificates, server.url("/").toString().removeSuffix("/"))
    }

    private data class TestLifecycleEvent(
        val sequence: Long,
        val type: String,
        val threadId: String,
        val turnId: String,
    )

    private inner class RelayPeer(
        private val store: InMemorySecureRelayConfigStore,
        private val assistantId: String,
        private val deviceId: String,
        private val ignoreMessagesOnFirstSocket: Boolean = false,
        private val ignoredOperations: Set<String> = emptySet(),
        private val recoverEventAck: Boolean = false,
        private val burstEventsBeforeAck: Boolean = false,
        private val stallEventAck: Boolean = false,
        private val stallFirstCatalogResponse: Boolean = false,
        private val threadSnapshotCursor: Long = 0,
        private val eventsAfterThreadRead: List<Long> = emptyList(),
        private val delayFirstAcceptanceOperation: String? = null,
        private val delayFirstAcceptanceMillis: Long = 0,
        private val threadStatus: String = "idle",
        private val threadLastTurnId: String? = null,
        private val lifecycleEventsAfterThreadRead: List<TestLifecycleEvent> = emptyList(),
    ) : WebSocketListener() {
        val operations = CopyOnWriteArrayList<String>()
        val retryTurnMessageIds = CopyOnWriteArrayList<String>()
        val turnRequestIds = CopyOnWriteArrayList<String>()
        val turnMessageIds = CopyOnWriteArrayList<String>()
        val assetRequestIds = CopyOnWriteArrayList<String>()
        val assetMessageIds = CopyOnWriteArrayList<String>()
        val acked = kotlinx.coroutines.flow.MutableStateFlow(0L)
        val pingCount = kotlinx.coroutines.flow.MutableStateFlow(0L)
        val socketCountFlow = kotlinx.coroutines.flow.MutableStateFlow(0L)
        val socketCount = AtomicLong()
        val eventAckSequences = CopyOnWriteArrayList<Long>()
        val delayedAcceptanceReleased = kotlinx.coroutines.flow.MutableStateFlow(false)
        private val acceptedSequence = AtomicLong()
        private val deliverySequence = AtomicLong()
        private val eventAckAttempts = AtomicLong()
        private val emittedEventFrames = AtomicLong()
        private val eventStreamStarted = AtomicBoolean(false)
        private val delayedAcceptanceStarted = AtomicBoolean(false)
        private val acceptedByMessage = ConcurrentHashMap<String, Long>()
        private val socketOrdinals = ConcurrentHashMap<WebSocket, Long>()

        override fun onOpen(webSocket: WebSocket, response: Response) {
            val ordinal = socketCount.incrementAndGet()
            socketOrdinals[webSocket] = ordinal
            socketCountFlow.value = ordinal
            val cursor = store.loadSession()?.relayAckCursor ?: 0
            deliverySequence.updateAndGet { maxOf(it, cursor) }
            webSocket.send(
                """{"v":1,"type":"ready","assistantId":"$assistantId","principalId":"$deviceId","role":"device","ackCursor":$cursor,"nextDeliverySeq":${cursor + 1},"nodeStatus":"online"}""",
            )
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            val root = Json.parseToJsonElement(text).jsonObject
            when (root.getValue("type").jsonPrimitive.content) {
                "ack" -> acked.value = root.getValue("deliverySeq").jsonPrimitive.content.toLong()
                "message" -> respond(webSocket, root, socketOrdinals.getValue(webSocket))
                "ping" -> {
                    pingCount.value += 1
                    webSocket.send(buildJsonObject {
                        put("v", 1)
                        put("type", "pong")
                        put("nonce", root.getValue("nonce").jsonPrimitive.content)
                    }.toString())
                }
            }
        }

        private fun respond(webSocket: WebSocket, root: JsonObject, socketOrdinal: Long) {
            val messageId = root.getValue("messageId").jsonPrimitive.content
            val payload = root.getValue("payload").jsonObject
            val requestId = payload.getValue("requestId").jsonPrimitive.content
            val operation = payload.getValue("operation").jsonPrimitive.content
            val params = payload.getValue("params").jsonObject
            operations += operation
            when (operation) {
                "turns.start" -> {
                    turnRequestIds += requestId
                    turnMessageIds += messageId
                }
                "assets.upload" -> {
                    assetRequestIds += requestId
                    assetMessageIds += messageId
                }
            }
            if ((ignoreMessagesOnFirstSocket && socketOrdinal == 1L) || operation in ignoredOperations) return
            val eventAckSequence = if (operation == "events.ack") {
                params.getValue("seq").jsonPrimitive.content.toLong().also(eventAckSequences::add)
            } else {
                null
            }
            val eventAckAttempt = if (operation == "events.ack") eventAckAttempts.incrementAndGet() else 0
            val delayedAcceptance =
                operation == delayFirstAcceptanceOperation &&
                    delayedAcceptanceStarted.compareAndSet(false, true)
            if (delayedAcceptance) Thread.sleep(delayFirstAcceptanceMillis)
            val newAccepted = acceptedSequence.incrementAndGet()
            val accepted = acceptedByMessage.putIfAbsent(messageId, newAccepted) ?: newAccepted
            webSocket.send(
                """{"v":1,"type":"accepted","messageId":"$messageId","deliverySeq":$accepted,"duplicate":${accepted != newAccepted},"queued":false}""",
            )
            if (
                stallFirstCatalogResponse && operation == "threads.list" &&
                operations.count { it == "threads.list" } == 1
            ) return
            if (operation == "events.ack" && stallEventAck) return
            val result = when (operation) {
                "threads.list" -> buildJsonObject {
                    put("data", kotlinx.serialization.json.buildJsonArray { add(threadJson()) })
                }
                "threads.create" -> createdThreadJson()
                "threads.read" -> buildJsonObject {
                    put("streamId", "stream-public")
                    put("cursor", threadSnapshotCursor)
                    put("messages", kotlinx.serialization.json.buildJsonArray {})
                    put("approvals", kotlinx.serialization.json.buildJsonArray {
                        add(buildJsonObject {
                            put("approvalId", "approval-public")
                            put("threadId", "thr_public")
                            put("state", "pending")
                            put("summary", "Allow this action?")
                            put("reason", "Codex needs confirmation")
                            put("decisions", kotlinx.serialization.json.buildJsonArray {
                                add(kotlinx.serialization.json.JsonPrimitive("approve"))
                                add(kotlinx.serialization.json.JsonPrimitive("reject"))
                            })
                            put("expiresAt", "2099-07-20T12:00:00Z")
                            put("actionToken", "action-token-000000000000000000000000")
                        })
                    })
                }
                "events.subscribe" -> buildJsonObject {
                    put("streamId", "stream-public")
                    put("currentSeq", 2)
                    put("syncRequired", false)
                }
                "events.ack" -> buildJsonObject { put("acknowledged", true) }
                "turns.start" -> buildJsonObject {
                    val threadId = params.getValue("threadId").jsonPrimitive.content
                    val clientMessageId = params.getValue("clientMessageId").jsonPrimitive.content
                    put("id", "turn-public-$threadId-$clientMessageId")
                    put("threadId", threadId)
                    put("status", "running")
                    put("clientMessageId", clientMessageId)
                    put("createdAt", "2026-07-20T12:00:00Z")
                }
                "assets.upload" -> buildJsonObject {
                    put("assetRef", "ast_abcdefghijklmnopqrstuvwxyz0123456789")
                    put("mediaType", params.getValue("mediaType").jsonPrimitive.content)
                    put("width", 1)
                    put("height", 1)
                    put("byteCount", 8)
                    put("expiresAt", "2099-07-20T12:00:00Z")
                }
                "approvals.decide" -> buildJsonObject {}
                else -> error("Unexpected operation $operation")
            }
            val delivery = deliverySequence.incrementAndGet()
            val isFirstRetryableTurn = operation == "turns.start" &&
                payload.getValue("params").jsonObject["clientMessageId"]?.jsonPrimitive?.content ==
                "client-message-retry" && retryTurnMessageIds.apply { add(messageId) }.size == 1
            val isFirstRetryableEventAck = recoverEventAck &&
                operation == "events.ack" && eventAckAttempt == 1L
            webSocket.send(
                buildJsonObject {
                    put("v", 1)
                    put("type", "delivery")
                    put("deliverySeq", delivery)
                    put("messageId", messageId)
                    put("payload", buildJsonObject {
                        put("kind", "response")
                        put("requestId", requestId)
                        put("deviceId", deviceId)
                        put("operation", operation)
                        put("ok", !isFirstRetryableTurn && !isFirstRetryableEventAck)
                        if (isFirstRetryableTurn || isFirstRetryableEventAck) {
                            put("error", buildJsonObject {
                                put("code", if (isFirstRetryableEventAck) "NODE_OFFLINE" else "RATE_LIMITED")
                                put("message", "Try again")
                                put("retryable", true)
                                if (isFirstRetryableTurn) put("retryAfterSeconds", 1)
                            })
                        } else {
                            put("result", result)
                        }
                    })
                }.toString(),
            )
            if (delayedAcceptance) delayedAcceptanceReleased.value = true
            when {
                operation == "threads.read" && eventsAfterThreadRead.isNotEmpty() ->
                    eventsAfterThreadRead.forEach { emitEvent(webSocket, it) }
                operation == "threads.read" && lifecycleEventsAfterThreadRead.isNotEmpty() ->
                    lifecycleEventsAfterThreadRead.forEach { emitLifecycleEvent(webSocket, it) }
                burstEventsBeforeAck && operation == "events.subscribe" &&
                    eventStreamStarted.compareAndSet(false, true) -> {
                    emitEvent(webSocket, 1)
                    emitEvent(webSocket, 2)
                }
                recoverEventAck && operation == "events.subscribe" &&
                    eventStreamStarted.compareAndSet(false, true) -> emitEvent(webSocket, 1)
                isFirstRetryableEventAck -> emitEvent(webSocket, 1)
                recoverEventAck && operation == "events.ack" &&
                    eventAckSequence == 1L && eventAckAttempt == 2L -> emitEvent(webSocket, 2)
            }
        }

        private fun emitEvent(webSocket: WebSocket, sequence: Long) {
            val frame = emittedEventFrames.incrementAndGet()
            webSocket.send(
                buildJsonObject {
                    put("v", 1)
                    put("type", "delivery")
                    put("deliverySeq", deliverySequence.incrementAndGet())
                    put("messageId", RelayIds.message("peer-event-frame:$frame"))
                    put("payload", buildJsonObject {
                        put("kind", "event")
                        put("requestId", RelayIds.request("peer-event-subscription"))
                        put("deviceId", deviceId)
                        put("streamId", "stream-public")
                        put("eventId", "event-public-$sequence")
                        put("eventSeq", sequence)
                        put("eventType", "thread.updated")
                        put("threadId", "thr_public")
                        put("data", buildJsonObject {
                            put("occurredAt", "2026-07-20T12:00:00Z")
                            put("payload", buildJsonObject {})
                        })
                    })
                }.toString(),
            )
        }

        private fun emitLifecycleEvent(webSocket: WebSocket, event: TestLifecycleEvent) {
            val frame = emittedEventFrames.incrementAndGet()
            webSocket.send(
                buildJsonObject {
                    put("v", 1)
                    put("type", "delivery")
                    put("deliverySeq", deliverySequence.incrementAndGet())
                    put("messageId", RelayIds.message("peer-lifecycle-frame:$frame"))
                    put("payload", buildJsonObject {
                        put("kind", "event")
                        put("requestId", RelayIds.request("peer-event-subscription"))
                        put("deviceId", deviceId)
                        put("streamId", "stream-public")
                        put("eventId", "event-public-${event.sequence}")
                        put("eventSeq", event.sequence)
                        put("eventType", event.type)
                        put("threadId", event.threadId)
                        put("data", buildJsonObject {
                            put("occurredAt", "2026-07-20T12:00:00Z")
                            put("turnId", event.turnId)
                            put("payload", buildJsonObject {})
                        })
                    })
                }.toString(),
            )
        }

        private fun threadJson() = buildJsonObject {
            put("id", "thr_public")
            put("title", "Relay thread")
            put("preview", "Ready")
            put("createdAt", "2026-07-20T12:00:00Z")
            put("updatedAt", "2026-07-20T12:00:01Z")
            put("status", threadStatus)
            threadLastTurnId?.let { put("lastTurnId", it) }
        }

        private fun createdThreadJson() = buildJsonObject {
            put("id", "thr_created")
            put("title", "Draft conversation")
            put("preview", "")
            put("createdAt", "2026-07-20T12:01:00Z")
            put("updatedAt", "2026-07-20T12:01:00Z")
            put("status", "idle")
        }
    }

    private data class TlsFixture(
        val server: MockWebServer,
        val client: OkHttpClient,
        val clientCertificates: HandshakeCertificates,
        val origin: String,
    )
}
