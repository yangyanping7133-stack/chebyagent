package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.data.InMemorySecureGatewayConfigStore
import com.cheby.codex.mobile.data.PairingBootstrap
import com.cheby.codex.mobile.data.PendingApprovalCommand
import com.cheby.codex.mobile.data.RemoteGatewaySession
import com.cheby.codex.mobile.data.scopeKey
import com.cheby.codex.mobile.model.CodexThread
import com.cheby.codex.mobile.model.ConversationEvent
import com.cheby.codex.mobile.model.ConversationSnapshot
import com.cheby.codex.mobile.model.GatewayConnectionState
import com.cheby.codex.mobile.model.ThreadKey
import java.util.Collections
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.yield
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class GatewaySwitchingTest {
    @Test
    fun `active remote rejects re-pair before key creation or exchange`() = runBlocking {
        val session = RemoteGatewaySession(
            baseUrl = "https://gateway.example/",
            accessToken = "existing-access-token",
            refreshToken = "existing-refresh-token",
            deviceId = "existing-device",
            expiresAt = "2026-07-20T00:00:00Z",
            refreshExpiresAt = "2026-08-20T00:00:00Z",
            streamId = "existing-stream",
            sessionScope = "existing-scope",
        )
        val store = InMemorySecureGatewayConfigStore(
            pairing = PairingBootstrap("https://stale.example/", "stale-one-time-secret"),
            session = session,
        )
        val active = RecordingGateway(session.scopeKey())
        val keyCreations = AtomicInteger()
        val switching = SwitchingCodexGateway(
            store = store,
            createOrLoadPairingKey = {
                keyCreations.incrementAndGet()
                TestDeviceProofKey.generate()
            },
            initialGateway = active,
        )

        val failure = runCatching {
            switching.pair("https://replacement.example", "new-one-time-code")
        }.exceptionOrNull()

        assertTrue(failure is GatewayTransportException)
        assertEquals("Re-pairing is unavailable", failure?.message)
        assertEquals(0, keyCreations.get())
        assertEquals(session, store.loadSession())
        assertEquals(null, store.loadPairing())
        assertEquals(session.scopeKey(), switching.sessionScope)
        assertTrue(!active.closed)
    }

    @Test
    fun `concurrent initial pair serializes exchange and second call rejects before bootstrap write`() = runBlocking {
        val candidate = RemoteGatewaySession(
            baseUrl = "https://first.example/",
            accessToken = "first-access-token",
            refreshToken = "first-refresh-token",
            deviceId = "first-device",
            expiresAt = "2026-07-20T00:00:00Z",
            refreshExpiresAt = "2026-08-20T00:00:00Z",
            streamId = "first-stream",
            sessionScope = "first-scope",
        )
        val store = InMemorySecureGatewayConfigStore()
        val exchangeStarted = CompletableDeferred<Unit>()
        val releaseExchange = CompletableDeferred<Unit>()
        val exchangeCalls = AtomicInteger()
        val factoryCalls = AtomicInteger()
        val replacement = RecordingGateway(candidate.scopeKey())
        val proofKey = TestDeviceProofKey.generate()
        val switching = SwitchingCodexGateway(
            store = store,
            remoteGatewayFactory = { session, _ ->
                assertEquals(candidate, session)
                factoryCalls.incrementAndGet()
                replacement
            },
            createOrLoadPairingKey = { proofKey },
            deviceNameProvider = { "test-device" },
            pairingExchange = { pairingStore, _, _, afterSessionReplace ->
                exchangeCalls.incrementAndGet()
                assertEquals("first-one-time-code", pairingStore.loadPairing()?.pairingSecret)
                exchangeStarted.complete(Unit)
                releaseExchange.await()
                assertTrue(pairingStore.saveSession(candidate))
                afterSessionReplace(candidate.scopeKey())
                pairingStore.clearPairing()
                candidate
            },
            initialGateway = FakeCodexGateway(),
        )

        val first = async {
            runCatching { switching.pair("https://first.example", "first-one-time-code") }
        }
        exchangeStarted.await()
        val second = async {
            runCatching { switching.pair("https://second.example", "second-one-time-code") }
        }
        yield()

        assertEquals(1, exchangeCalls.get())
        assertEquals(null, store.loadSession())
        releaseExchange.complete(Unit)

        assertEquals(null, first.await().exceptionOrNull())
        val secondFailure = second.await().exceptionOrNull()
        assertTrue(secondFailure is GatewayTransportException)
        assertEquals("Re-pairing is unavailable", secondFailure?.message)
        assertEquals(1, exchangeCalls.get())
        assertEquals(1, factoryCalls.get())
        assertEquals(candidate, store.loadSession())
        assertEquals(null, store.loadPairing())
        assertEquals(candidate.scopeKey(), switching.sessionScope)
        assertTrue(!replacement.closed)
    }

    @Test
    fun `successful initial pair ignores best effort outbox cleanup failure`() = runBlocking {
        val candidate = RemoteGatewaySession(
            baseUrl = "https://gateway.example/",
            accessToken = "new-access-token",
            refreshToken = "new-refresh-token",
            deviceId = "new-device",
            expiresAt = "2026-07-20T00:00:00Z",
            refreshExpiresAt = "2026-08-20T00:00:00Z",
            streamId = "new-stream",
            sessionScope = "new-scope",
        )
        val store = InMemorySecureGatewayConfigStore()
        val cleanupCalls = AtomicInteger()
        val replacement = RecordingGateway(candidate.scopeKey())
        val proofKey = TestDeviceProofKey.generate()
        val switching = SwitchingCodexGateway(
            store = store,
            retainDurableOutboxScope = {
                cleanupCalls.incrementAndGet()
                throw IllegalStateException("best effort cleanup failed")
            },
            remoteGatewayFactory = { _, _ -> replacement },
            createOrLoadPairingKey = { proofKey },
            deviceNameProvider = { "test-device" },
            pairingExchange = { pairingStore, _, _, afterSessionReplace ->
                assertTrue(pairingStore.saveSession(candidate))
                afterSessionReplace(candidate.scopeKey())
                pairingStore.clearPairing()
                candidate
            },
            initialGateway = FakeCodexGateway(),
        )

        switching.pair("https://gateway.example", "new-one-time-code")

        assertEquals(1, cleanupCalls.get())
        assertEquals(candidate, store.loadSession())
        assertEquals(null, store.loadPairing())
        assertEquals(candidate.scopeKey(), switching.sessionScope)
        assertTrue(!replacement.closed)
    }

    @Test
    fun `existing session without its device key fails closed and clears replayable state`() {
        val session = RemoteGatewaySession(
            baseUrl = "https://gateway.example/",
            accessToken = "secret-access",
            refreshToken = "secret-refresh",
            deviceId = "device-a",
            expiresAt = "2026-07-20T00:00:00Z",
            refreshExpiresAt = "2026-08-20T00:00:00Z",
            streamId = "stream-a",
        )
        val store = InMemorySecureGatewayConfigStore(session = session)
        store.replaceApprovalCommandsForScope(
            session.scopeKey(),
            listOf(
                PendingApprovalCommand(
                    sessionScope = session.scopeKey(),
                    actionRef = "stale-action",
                    approvalId = "stale-approval",
                    threadId = "thread-a",
                    decision = "approve",
                    actionToken = "secret-action-token",
                    expiresAtMillis = Long.MAX_VALUE,
                    summary = "Stale",
                    reason = "Must clear",
                ),
            ),
        )
        val outboxClears = AtomicInteger()

        val switching = SwitchingCodexGateway(
            store = store,
            clearDurableOutboxScope = { scope ->
                assertEquals(session.scopeKey(), scope)
                outboxClears.incrementAndGet()
            },
            loadExistingKey = { throw DeviceIdentityUnavailableException() },
        )

        assertEquals(GatewayKind.UNCONFIGURED, switching.kind.value)
        assertEquals(null, store.loadSession())
        assertTrue(store.loadApprovalCommands().isEmpty())
        assertEquals(1, outboxClears.get())
    }

    @Test
    fun `same scope delegate replacement still advances identity generation`() = runBlocking {
        val a = RecordingGateway("same-scope")
        val b = RecordingGateway("same-scope")
        val switching = SwitchingCodexGateway(
            store = InMemorySecureGatewayConfigStore(),
            initialGateway = a,
        )

        assertEquals(GatewaySessionIdentity("same-scope", 0), switching.sessionIdentity.value)
        switching.replaceDelegate(b)

        assertEquals(GatewaySessionIdentity("same-scope", 1), switching.sessionIdentity.value)
        assertTrue(a.closed)
    }

    @Test
    fun `same scope replacement rejects stale generation capability upload and rich turn`() = runBlocking {
        val a = RecordingGateway("same-scope")
        val b = RecordingGateway("same-scope")
        val switching = SwitchingCodexGateway(
            store = InMemorySecureGatewayConfigStore(),
            initialGateway = a,
        )
        val stale = switching.sessionIdentity.value
        switching.replaceDelegate(b)

        val failures = listOf(
            runCatching { switching.loadCapabilities(stale.scope, stale.generation) }.exceptionOrNull(),
            runCatching {
                switching.uploadTurnImage(
                    ThreadKey("shared"),
                    "client-image-message-001",
                    "00000000-0000-4000-8000-000000000001",
                    byteArrayOf(1),
                    stale.scope,
                    stale.generation,
                )
            }.exceptionOrNull(),
            runCatching {
                switching.sendTurnInput(
                    ThreadKey("shared"),
                    listOf(TurnInput.Text("stale")),
                    "client-image-message-001",
                    stale.scope,
                    stale.generation,
                )
            }.exceptionOrNull(),
        )

        assertTrue(failures.all { it is GatewayTransportException })
        assertEquals(0, b.richOperationCalls)
    }

    @Test
    fun `replacing A closes it and completes its captured event flow`() = runBlocking {
        val a = RecordingGateway("scope-a")
        val b = RecordingGateway("scope-b")
        val switching = SwitchingCodexGateway(
            store = InMemorySecureGatewayConfigStore(),
            initialGateway = a,
        )
        val events = Collections.synchronizedList(mutableListOf<ConversationEvent>())
        val collector = async {
            switching.observeConversation(ThreadKey("shared"), "stream-a", 0).toList(events)
        }
        delay(50)
        a.events.emit(ConversationEvent.NoOp("before", "stream-a", 1))
        withTimeout(2_000) {
            while (events.isEmpty()) delay(10)
        }

        switching.replaceDelegate(b)
        withTimeout(2_000) { collector.await() }
        a.events.emit(ConversationEvent.NoOp("late-a", "stream-a", 2))
        delay(50)

        assertTrue(a.closed)
        assertEquals(listOf("before"), events.map(ConversationEvent::eventId))
    }

    @Test
    fun `A scoped queued operations are rejected after generation switches to B`() = runBlocking {
        val a = RecordingGateway("scope-a")
        val b = RecordingGateway("scope-b")
        val switching = SwitchingCodexGateway(
            store = InMemorySecureGatewayConfigStore(),
            initialGateway = a,
        )

        switching.replaceDelegate(b)
        val sendFailure = runCatching {
            switching.sendTurn(ThreadKey("same-thread"), "A text", "same-id", "scope-a")
        }.exceptionOrNull()
        val actionFailure = runCatching {
            switching.performAction("a-action", "scope-a")
        }.exceptionOrNull()

        assertTrue(sendFailure is GatewayTransportException)
        assertTrue(actionFailure is GatewayTransportException)
        assertTrue(b.sentIds.isEmpty())
        assertTrue(b.actions.isEmpty())
    }

    @Test
    fun `operations already queued on A generation cannot fall through to B`() = runBlocking {
        val releaseFirstSend = CompletableDeferred<Unit>()
        val a = RecordingGateway("scope-a", releaseFirstSend)
        val b = RecordingGateway("scope-b")
        val switching = SwitchingCodexGateway(
            store = InMemorySecureGatewayConfigStore(),
            initialGateway = a,
        )
        val first = launch {
            switching.sendTurn(ThreadKey("same-thread"), "first", "first-a", "scope-a")
        }
        withTimeout(2_000) { a.firstSendStarted.await() }
        val replacement = launch { switching.replaceDelegate(b) }
        yield()
        val queuedSend = async {
            runCatching {
                switching.sendTurn(ThreadKey("same-thread"), "queued", "queued-a", "scope-a")
            }.exceptionOrNull()
        }
        val queuedAction = async {
            runCatching { switching.performAction("queued-action-a", "scope-a") }.exceptionOrNull()
        }

        releaseFirstSend.complete(Unit)
        first.join()
        replacement.join()

        assertTrue(queuedSend.await() is GatewayTransportException)
        assertTrue(queuedAction.await() is GatewayTransportException)
        assertEquals(listOf("first-a"), a.sentIds)
        assertTrue(b.sentIds.isEmpty())
        assertTrue(b.actions.isEmpty())
    }

    @Test
    fun `closing switch owner is idempotent and closes any rejected replacement`() = runBlocking {
        val current = RecordingGateway("scope-a")
        val late = RecordingGateway("scope-b")
        val switching = SwitchingCodexGateway(
            store = InMemorySecureGatewayConfigStore(),
            initialGateway = current,
        )

        switching.close()
        switching.close()
        val replacementFailure = runCatching { switching.replaceDelegate(late) }.exceptionOrNull()
        val operationFailure = runCatching {
            switching.sendTurn(ThreadKey("thread-a"), "late", "late-id", "scope-a")
        }.exceptionOrNull()

        assertEquals(1, current.closeCalls.get())
        assertEquals(1, late.closeCalls.get())
        assertTrue(replacementFailure is GatewayTransportException)
        assertTrue(operationFailure is GatewayTransportException)
    }

    @Test
    fun `close completes active observer backed by production Fake shared flow`() = runBlocking {
        val fake = FakeCodexGateway()
        val switching = SwitchingCodexGateway(
            store = InMemorySecureGatewayConfigStore(),
            initialGateway = fake,
        )
        val threadKey = fake.threads.value.first().key
        val observed = CompletableDeferred<Unit>()
        val collector = async {
            switching.observeConversation(threadKey, "before-close", 0)
                .onEach { observed.complete(Unit) }
                .toList()
        }
        fake.sendTurn(threadKey, "observer close proof", "observer-close-id", fake.sessionScope)
        withTimeout(2_000) { observed.await() }

        switching.close()

        withTimeout(2_000) { collector.await() }
        Unit
    }

    @Test
    fun `close and replace interleavings neither deadlock nor leak or double close`() = runBlocking {
        repeat(100) { iteration ->
            withTimeout(2_000) {
                val current = RecordingGateway("scope-a-$iteration")
                val late = RecordingGateway("scope-b-$iteration")
                val switching = SwitchingCodexGateway(
                    store = InMemorySecureGatewayConfigStore(),
                    initialGateway = current,
                )
                val start = CompletableDeferred<Unit>()
                val close = async(Dispatchers.Default) {
                    start.await()
                    switching.close()
                }
                val replace = async(Dispatchers.Default) {
                    start.await()
                    runCatching { switching.replaceDelegate(late) }.exceptionOrNull()
                }

                start.complete(Unit)
                close.await()
                val replacementFailure = replace.await()

                assertEquals(1, current.closeCalls.get())
                assertEquals(1, late.closeCalls.get())
                assertTrue(
                    replacementFailure == null || replacementFailure is GatewayTransportException,
                )
                assertTrue(
                    runCatching {
                        switching.sendTurn(
                            ThreadKey("thread-$iteration"),
                            "late",
                            "late-$iteration",
                            current.sessionScope,
                        )
                    }.exceptionOrNull() is GatewayTransportException,
                )
            }
        }
    }

    private class RecordingGateway(
        override val sessionScope: String,
        private val firstSendGate: CompletableDeferred<Unit>? = null,
    ) : CodexGateway, CloseableCodexGateway {
        val events = MutableSharedFlow<ConversationEvent>(extraBufferCapacity = 8)
        val sentIds = mutableListOf<String>()
        val actions = mutableListOf<String>()
        val firstSendStarted = CompletableDeferred<Unit>()
        val closeCalls = AtomicInteger()
        var closed = false
        var richOperationCalls = 0
        override val kind: StateFlow<GatewayKind> = MutableStateFlow(GatewayKind.REMOTE)
        override val connectionState: StateFlow<GatewayConnectionState> =
            MutableStateFlow(GatewayConnectionState.ONLINE)
        override val threads: StateFlow<List<CodexThread>> = MutableStateFlow(emptyList())

        override suspend fun createThread(title: String): CodexThread = error("unused")
        override suspend fun renameThread(threadKey: ThreadKey, title: String) = Unit
        override suspend fun archiveThread(threadKey: ThreadKey) = Unit
        override suspend fun deleteThread(threadKey: ThreadKey) = Unit
        override suspend fun loadConversation(threadKey: ThreadKey): ConversationSnapshot =
            ConversationSnapshot(threadKey, "stream-$sessionScope", 0, emptyList())

        override fun observeConversation(
            threadKey: ThreadKey,
            afterStreamId: String,
            afterStreamSeq: Long,
        ): Flow<ConversationEvent> = events

        override suspend fun loadCapabilities(
            expectedSessionScope: String,
            expectedSessionGeneration: Long,
        ): GatewayCapabilities {
            richOperationCalls += 1
            return GatewayCapabilities.NONE
        }

        override suspend fun uploadTurnImage(
            threadKey: ThreadKey,
            clientMessageId: String,
            clientAssetId: String,
            bytes: ByteArray,
            expectedSessionScope: String,
            expectedSessionGeneration: Long,
        ): UploadedImageAsset {
            richOperationCalls += 1
            return UploadedImageAsset("ast_abcdefghijklmnopqrstuvwxyz0123456789", "image/png", 1, 1, 1)
        }

        override suspend fun sendTurnInput(
            threadKey: ThreadKey,
            input: List<TurnInput>,
            idempotencyKey: String,
            expectedSessionScope: String,
            expectedSessionGeneration: Long,
        ): TurnSubmission {
            richOperationCalls += 1
            return TurnSubmission("turn-$idempotencyKey", threadKey, idempotencyKey, "running")
        }

        override suspend fun sendTurn(
            threadKey: ThreadKey,
            text: String,
            idempotencyKey: String,
            expectedSessionScope: String,
        ): TurnSubmission {
            firstSendStarted.complete(Unit)
            firstSendGate?.await()
            sentIds += idempotencyKey
            return TurnSubmission("turn-$idempotencyKey", threadKey, idempotencyKey, "running")
        }

        override suspend fun performAction(actionRef: String, expectedSessionScope: String) {
            actions += actionRef
        }

        override fun close() {
            closeCalls.incrementAndGet()
            closed = true
        }
    }
}
