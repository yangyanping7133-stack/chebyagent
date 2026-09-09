package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.data.InMemorySecureGatewayConfigStore
import com.cheby.codex.mobile.data.InMemorySecureRelayConfigStore
import com.cheby.codex.mobile.data.PairingBootstrap
import com.cheby.codex.mobile.data.RelayDeviceSession
import com.cheby.codex.mobile.data.RemoteGatewaySession
import com.cheby.codex.mobile.data.scopeKey
import com.cheby.codex.mobile.model.CodexThread
import com.cheby.codex.mobile.model.ConversationEvent
import com.cheby.codex.mobile.model.ConversationSnapshot
import com.cheby.codex.mobile.model.GatewayConnectionState
import com.cheby.codex.mobile.model.ThreadKey
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.asCoroutineDispatcher
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.emptyFlow
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.delay
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.yield
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class RelaySwitchingGatewayTest {
    private val migrationTargetOrigin = "https://192.0.0.8:27461"

    @Test
    fun `legacy direct credential is erased and never interpreted as relay`() {
        val direct = RemoteGatewaySession(
            baseUrl = "https://turkey.example",
            accessToken = "old-access",
            refreshToken = "old-refresh",
            deviceId = "old-device",
            expiresAt = "2026-07-20T00:00:00Z",
            refreshExpiresAt = "2026-08-20T00:00:00Z",
            streamId = "old-stream",
            sessionScope = "old-direct-scope",
        )
        val legacyStore = InMemorySecureGatewayConfigStore(
            pairing = PairingBootstrap("https://turkey.example", "old-code"),
            session = direct,
        )
        val cleared = mutableListOf<String>()

        quarantineLegacyDirectCredentials(legacyStore, cleared::add)

        assertNull(legacyStore.loadPairing())
        assertNull(legacyStore.loadSession())
        assertEquals(listOf(direct.scopeKey()), cleared)
    }

    @Test
    fun `concurrent enrollment is serialized and cannot replace an active assistant`() = runBlocking {
        val store = InMemorySecureRelayConfigStore()
        val exchangeStarted = CompletableDeferred<Unit>()
        val releaseExchange = CompletableDeferred<Unit>()
        val exchanges = AtomicInteger()
        val proofKey = TestDeviceProofKey.generate()
        val candidate = relaySession().copy(assistantId = "asst_0000000000000000000000")
        val accessKey = candidate.assistantId
        val secretKey = "pair_0000000000000000000000000000000000000000000"
        val relay = RelayRecordingGateway(candidate.bindingScope)
        val switching = RelaySwitchingCodexGateway(
            store = store,
            createOrLoadEnrollmentKey = { proofKey },
            enrollmentExchange = { bootstrap, _ ->
                exchanges.incrementAndGet()
                assertEquals("我的 Codex", bootstrap.assistantAlias)
                assertEquals(candidate.assistantId, bootstrap.assistantId)
                exchangeStarted.complete(Unit)
                releaseExchange.await()
                candidate
            },
            relayGatewayFactory = { session, _ ->
                assertEquals(candidate, session)
                relay
            },
            initialGateway = FakeCodexGateway(),
        )

        val first = async {
            runCatching { switching.connect("https://relay.example/", accessKey, secretKey) }
        }
        exchangeStarted.await()
        val second = async {
            runCatching { switching.connect("https://other.example", accessKey, secretKey) }
        }
        yield()
        assertEquals(1, exchanges.get())
        releaseExchange.complete(Unit)

        assertNull(first.await().exceptionOrNull())
        assertTrue(second.await().exceptionOrNull() is GatewayTransportException)
        withTimeout(2_000) {
            while (switching.kind.value != GatewayKind.RELAY) delay(10)
        }
        assertEquals(1, exchanges.get())
        assertEquals(GatewayKind.RELAY, switching.kind.value)
        assertEquals(candidate.bindingScope, switching.sessionScope)
        assertEquals("我的 Codex", store.loadSession()?.assistantAlias)
        assertFalse(relay.closed)
    }

    @Test
    fun `persisted relay session without relay Keystore identity fails closed`() {
        val session = relaySession()
        val store = InMemorySecureRelayConfigStore(session = session)
        val cleared = mutableListOf<String>()

        val switching = RelaySwitchingCodexGateway(
            store = store,
            clearDurableOutboxScope = cleared::add,
            loadExistingKey = { throw DeviceIdentityUnavailableException() },
        )

        assertEquals(GatewayKind.UNCONFIGURED, switching.kind.value)
        assertNull(store.loadSession())
        assertEquals(listOf(session.bindingScope), cleared)
    }

    @Test
    fun `relay delivery cursor and Gateway event cursor advance independently`() {
        val initial = relaySession()
        val store = InMemorySecureRelayConfigStore(session = initial)

        assertTrue(store.updateRelayCursor(initial.bindingScope, 11))
        assertTrue(store.updateGatewayCursor(initial.bindingScope, "gateway-stream-b", 4))
        assertTrue(store.updateRelayCursor(initial.bindingScope, 9))
        assertTrue(store.updateGatewayCursor(initial.bindingScope, "gateway-stream-b", 2))

        val current = requireNotNull(store.loadSession())
        assertEquals(11, current.relayAckCursor)
        assertEquals("gateway-stream-b", current.gatewayStreamId)
        assertEquals(4, current.gatewayAckSeq)
        assertFalse(RelayDeviceSession::class.java.declaredFields.any { it.name.contains("connection", true) })
        assertFalse(current.toString().contains(current.accessToken))
        assertFalse(current.toString().contains(current.refreshToken))
    }

    @Test
    fun `verified fixed origin migration atomically preserves binding credentials and cursors`() =
        runBlocking {
            val original = relaySession().copy(relayOrigin = LEGACY_RELAY_ORIGIN)
            val store = InMemorySecureRelayConfigStore(session = original)
            val proofKey = TestDeviceProofKey.generate()
            val legacy = RelayRecordingGateway(original.bindingScope)
            val replacements = CopyOnWriteArrayList<RelayDeviceSession>()
            val switching = RelaySwitchingCodexGateway(
                store = store,
                loadExistingKey = { proofKey },
                originMigrationProbe = { candidate, suppliedKey ->
                    assertEquals(proofKey, suppliedKey)
                    assertEquals(migrationTargetOrigin, candidate.relayOrigin)
                    candidate
                },
                legacyOriginMigrationEnabled = true,
                migrationTargetOrigin = migrationTargetOrigin,
                relayGatewayFactory = { session, suppliedKey ->
                    assertEquals(proofKey, suppliedKey)
                    replacements += session
                    RelayRecordingGateway(session.bindingScope)
                },
                initialGateway = legacy,
            )
            try {
                withTimeout(2_000) {
                    while (
                        store.loadSession()?.relayOrigin != migrationTargetOrigin ||
                            replacements.size != 1 ||
                            !legacy.closed
                    ) {
                        delay(10)
                    }
                }
                val committed = requireNotNull(store.loadSession())

                assertEquals(original.copy(relayOrigin = migrationTargetOrigin), committed)
                assertEquals(listOf(committed), replacements)
                assertTrue(legacy.closed)
                assertEquals(original.bindingScope, switching.sessionScope)
                assertEquals(
                    GatewaySessionIdentity(
                        scope = original.bindingScope,
                        generation = 1,
                        outboxCarryoverFromGeneration = 0,
                    ),
                    switching.sessionIdentity.value,
                )
            } finally {
                switching.close()
            }
        }

    @Test
    fun `failed fixed origin authentication leaves durable legacy session and gateway usable`() =
        runBlocking {
            val original = relaySession().copy(relayOrigin = LEGACY_RELAY_ORIGIN)
            val store = InMemorySecureRelayConfigStore(session = original)
            val legacy = RelayRecordingGateway(original.bindingScope)
            val factoryCalls = AtomicInteger()
            val switching = RelaySwitchingCodexGateway(
                store = store,
                loadExistingKey = TestDeviceProofKey::generate,
                originMigrationProbe = { _, _ ->
                    throw GatewayTransportException("fixed edge rejected authentication")
                },
                legacyOriginMigrationEnabled = true,
                migrationTargetOrigin = migrationTargetOrigin,
                relayGatewayFactory = { session, _ ->
                    factoryCalls.incrementAndGet()
                    RelayRecordingGateway(session.bindingScope)
                },
                initialGateway = legacy,
            )
            try {
                delay(150)

                assertEquals(original, store.loadSession())
                assertEquals(0, factoryCalls.get())
                assertFalse(legacy.closed)
                assertEquals(GatewayKind.RELAY, switching.kind.value)
                assertEquals(original.bindingScope, switching.sessionScope)
            } finally {
                switching.close()
            }
        }

    @Test
    fun `bound phone can authenticate and atomically change a configurable fixed origin`() =
        runBlocking {
            val original = relaySession().copy(relayOrigin = "https://198.51.100.10:27461")
            val target = "https://203.0.113.20:27461"
            val store = InMemorySecureRelayConfigStore(session = original)
            val proofKey = TestDeviceProofKey.generate()
            val oldGateway = RelayRecordingGateway(original.bindingScope)
            val switching = RelaySwitchingCodexGateway(
                store = store,
                loadExistingKey = { proofKey },
                originMigrationProbe = { candidate, suppliedKey ->
                    assertEquals(proofKey, suppliedKey)
                    assertEquals(target, candidate.relayOrigin)
                    candidate
                },
                relayGatewayFactory = { session, suppliedKey ->
                    assertEquals(proofKey, suppliedKey)
                    RelayRecordingGateway(session.bindingScope)
                },
                initialGateway = oldGateway,
            )
            try {
                switching.changeRelayOrigin("$target/")

                assertEquals(original.copy(relayOrigin = target), store.loadSession())
                assertTrue(oldGateway.closed)
                assertEquals(
                    GatewaySessionIdentity(
                        scope = original.bindingScope,
                        generation = 1,
                        outboxCarryoverFromGeneration = 0,
                    ),
                    switching.sessionIdentity.value,
                )
            } finally {
                switching.close()
            }
        }

    @Test
    fun `address authentication probe never blocks its caller thread`() = runBlocking {
        val original = relaySession().copy(relayOrigin = "https://198.51.100.10:27461")
        val target = "https://203.0.113.20:27461"
        val callerThread = Thread.currentThread().name
        val probeExecutor = Executors.newSingleThreadExecutor { task ->
            Thread(task, "relay-origin-probe-io")
        }
        probeExecutor.asCoroutineDispatcher().use { probeDispatcher ->
            val switching = RelaySwitchingCodexGateway(
                store = InMemorySecureRelayConfigStore(session = original),
                loadExistingKey = TestDeviceProofKey::generate,
                originMigrationProbe = { candidate, _ ->
                    assertTrue(Thread.currentThread().name.startsWith("relay-origin-probe-io"))
                    assertFalse(callerThread == Thread.currentThread().name)
                    candidate
                },
                originMigrationDispatcher = probeDispatcher,
                relayGatewayFactory = { session, _ ->
                    RelayRecordingGateway(session.bindingScope)
                },
                initialGateway = RelayRecordingGateway(original.bindingScope),
            )
            try {
                switching.changeRelayOrigin(target)
                assertEquals(callerThread, Thread.currentThread().name)
            } finally {
                switching.close()
            }
        }
    }

    @Test
    fun `bound phone rejects address whose ready identity is not the same assistant`() =
        runBlocking {
            val original = relaySession().copy(relayOrigin = "https://198.51.100.10:27461")
            val store = InMemorySecureRelayConfigStore(session = original)
            val oldGateway = RelayRecordingGateway(original.bindingScope)
            val switching = RelaySwitchingCodexGateway(
                store = store,
                loadExistingKey = TestDeviceProofKey::generate,
                originMigrationProbe = { candidate, _ ->
                    candidate.copy(principalId = "dev_wrongprincipal0000000000")
                },
                initialGateway = oldGateway,
            )
            try {
                val failure = runCatching {
                    switching.changeRelayOrigin("https://203.0.113.20:27461")
                }.exceptionOrNull()

                assertTrue(failure is GatewayTransportException)
                assertEquals(original, store.loadSession())
                assertFalse(oldGateway.closed)
                assertEquals(0, switching.sessionIdentity.value.generation)
            } finally {
                switching.close()
            }
        }

    @Test
    fun `transient fixed origin failure retries and migrates without another app restart`() =
        runBlocking {
            val original = relaySession().copy(relayOrigin = LEGACY_RELAY_ORIGIN)
            val store = InMemorySecureRelayConfigStore(session = original)
            val legacy = RelayRecordingGateway(original.bindingScope)
            val proofKey = TestDeviceProofKey.generate()
            val probes = AtomicInteger()
            val retryWaits = AtomicInteger()
            val switching = RelaySwitchingCodexGateway(
                store = store,
                loadExistingKey = { proofKey },
                originMigrationProbe = { candidate, _ ->
                    if (probes.incrementAndGet() == 1) {
                        throw GatewayTransportException("temporary Edge outage")
                    }
                    candidate
                },
                legacyOriginMigrationEnabled = true,
                migrationTargetOrigin = migrationTargetOrigin,
                waitBeforeOriginMigrationRetry = {
                    retryWaits.incrementAndGet()
                    yield()
                },
                relayGatewayFactory = { session, _ ->
                    RelayRecordingGateway(session.bindingScope)
                },
                initialGateway = legacy,
            )
            try {
                withTimeout(2_000) {
                    while (
                        store.loadSession()?.relayOrigin != migrationTargetOrigin ||
                        !legacy.closed
                    ) {
                        yield()
                    }
                }

                assertEquals(2, probes.get())
                assertEquals(1, retryWaits.get())
                assertTrue(legacy.closed)
                assertEquals(original.bindingScope, switching.sessionScope)
            } finally {
                switching.close()
            }
        }

    private fun relaySession() = RelayDeviceSession(
        assistantId = "assistant-internal-1",
        assistantAlias = "我的 Codex",
        relayOrigin = "https://relay.example",
        bindingScope = "relay-binding-scope-1",
        principalId = "device-principal-1",
        accessToken = "relay-device-access-token",
        refreshToken = "relay-device-refresh-token",
        accessExpiresAt = 1_800_000_000,
        refreshExpiresAt = 1_900_000_000,
        relayAckCursor = 7,
        gatewayStreamId = "gateway-stream-a",
        gatewayAckSeq = 3,
    )

    private class RelayRecordingGateway(
        override val sessionScope: String,
    ) : CodexGateway, CloseableCodexGateway {
        override val kind: StateFlow<GatewayKind> = MutableStateFlow(GatewayKind.RELAY)
        override val connectionState: StateFlow<GatewayConnectionState> =
            MutableStateFlow(GatewayConnectionState.ONLINE)
        override val threads: StateFlow<List<CodexThread>> = MutableStateFlow(emptyList())
        @Volatile
        var closed = false

        override suspend fun createThread(title: String): CodexThread = unsupported()
        override suspend fun renameThread(threadKey: ThreadKey, title: String) = unsupported<Unit>()
        override suspend fun archiveThread(threadKey: ThreadKey) = unsupported<Unit>()
        override suspend fun deleteThread(threadKey: ThreadKey) = unsupported<Unit>()
        override suspend fun loadConversation(threadKey: ThreadKey): ConversationSnapshot = unsupported()
        override fun observeConversation(
            threadKey: ThreadKey,
            afterStreamId: String,
            afterStreamSeq: Long,
        ): Flow<ConversationEvent> = emptyFlow()
        override suspend fun sendTurn(
            threadKey: ThreadKey,
            text: String,
            idempotencyKey: String,
            expectedSessionScope: String,
        ): TurnSubmission = unsupported()
        override suspend fun performAction(actionRef: String, expectedSessionScope: String) =
            unsupported<Unit>()

        override fun close() { closed = true }
        private fun <T> unsupported(): T = throw GatewayTransportException("Unsupported")
    }
}
