package com.cheby.codex.mobile.gateway

import android.content.Context
import android.os.Build
import com.cheby.codex.mobile.BuildConfig
import com.cheby.codex.mobile.data.AndroidKeystoreGatewayConfigStore
import com.cheby.codex.mobile.data.AndroidKeystoreRelayConfigStore
import com.cheby.codex.mobile.data.LocalCapabilityTokenStore
import com.cheby.codex.mobile.data.PairingBootstrap
import com.cheby.codex.mobile.data.ProcessLockedRelayConfigStore
import com.cheby.codex.mobile.data.RemoteGatewaySession
import com.cheby.codex.mobile.data.SecureGatewayConfigStore
import com.cheby.codex.mobile.data.SharedPreferencesDurableOutboxStore
import com.cheby.codex.mobile.data.scopeKey
import com.cheby.codex.mobile.model.CodexThread
import com.cheby.codex.mobile.model.ConversationEvent
import com.cheby.codex.mobile.model.ConversationSnapshot
import com.cheby.codex.mobile.model.GatewayConnectionState
import com.cheby.codex.mobile.model.ThreadKey
import com.cheby.codex.mobile.runtime.StandaloneRuntimeSupervisor
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.channelFlow
import kotlinx.coroutines.flow.emptyFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.flatMapLatest
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.io.File
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong

interface PairingGatewayControls {
    suspend fun pair(baseUrl: String, pairingCode: String)
}

object GatewayFactory {
    private val creationCount = AtomicLong(0)

    fun create(context: Context): CodexGateway {
        creationCount.incrementAndGet()
        if (BuildConfig.STANDALONE_MODE) {
            val token = LocalCapabilityTokenStore(context.applicationContext).loadOrCreate()
            val runtimeSupervisor = StandaloneRuntimeSupervisor(context.applicationContext)
            runtimeSupervisor.ensureStarted(token.sha256)
            val runtimeRoot = File(
                context.applicationContext.filesDir,
                "usr/var/lib/cheby-runtime/debian-rootfs",
            )
            return LocalCodexGateway(
                bearerToken = token.bearer,
                savedModel = {
                    val settings = org.json.JSONObject(
                        com.cheby.codex.runtime.EmbeddedRuntimeRegistry.providerSettings(),
                    )
                    when (settings.getString("provider")) {
                        "glm" -> "glm-5.3-flash"
                        "openai" -> "gpt-5.6-sol"
                        else -> throw GatewayTransportException("Saved model configuration is unavailable")
                    }
                },
                savedReasoningEffort = {
                    val settings = org.json.JSONObject(
                        com.cheby.codex.runtime.EmbeddedRuntimeRegistry.providerSettings(),
                    )
                    val provider = settings.getString("provider")
                    val effort = settings.getJSONObject("profiles")
                        .getJSONObject(provider).getString("reasoningEffort")
                    val supported = when (provider) {
                        "glm" -> setOf("low", "high", "max")
                        "openai" -> setOf("none", "low", "medium", "high", "xhigh", "max")
                        else -> emptySet()
                    }
                    require(effort in supported)
                    effort
                },
                client = LocalAppServerClient(
                    bearerToken = token.bearer,
                    beforeConnectAttempt = {
                        runtimeSupervisor.ensureStarted(token.sha256)
                    },
                ),
                imageStore = LocalTurnImageStore(
                    rootDirectory = File(runtimeRoot, "root/.cheby/turn-inputs"),
                    codexRootPath = "/root/.cheby/turn-inputs",
                    runtimeReady = { File(runtimeRoot, ".cheby-runtime-pack").isFile },
                ),
            )
        }
        val outbox = SharedPreferencesDurableOutboxStore(context.applicationContext)
        val legacyStore = AndroidKeystoreGatewayConfigStore(context.applicationContext)
        quarantineLegacyDirectCredentials(legacyStore, outbox::clearScope)
        val relayStore = ProcessLockedRelayConfigStore(
            AndroidKeystoreRelayConfigStore(context.applicationContext),
        )
        val relayTrust = RelayTlsTrust.load(context.applicationContext)
        val credentialClient = RelayCredentialClient(
            client = relayTrust.newHttpClient(),
            originValidator = relayTrust::requireAllowedOrigin,
        )
        val platformRelayClient = if (BuildConfig.LEGACY_RELAY_MIGRATION_ENABLED) {
            defaultRelayHttpClientBuilder().build()
        } else {
            null
        }
        val legacyCredentialClient = platformRelayClient?.let { client ->
            RelayCredentialClient(
                client = client,
                originValidator = ::requireExactLegacyRelayOrigin,
            )
        }
        val originAuthenticator = RelayOriginAuthenticator(
            sslSocketFactory = relayTrust.socketFactory,
            originValidator = relayTrust::requireAllowedOrigin,
        )
        val deviceName = listOf(Build.MANUFACTURER, Build.MODEL)
            .filter(String::isNotBlank)
            .joinToString(" ")
            .take(80)
            .ifBlank { "Android" }
        return RelaySwitchingCodexGateway(
            store = relayStore,
            retainDurableOutboxScope = outbox::clearOtherScopes,
            clearDurableOutboxScope = outbox::clearScope,
            relayGatewayFactory = { session, deviceProofKey ->
                val legacy = BuildConfig.LEGACY_RELAY_MIGRATION_ENABLED &&
                    session.relayOrigin == LEGACY_RELAY_ORIGIN
                RelayCodexGateway(
                    sessionStore = relayStore,
                    session = session,
                    deviceProofKey = deviceProofKey,
                    credentialClient = if (legacy) {
                        requireNotNull(legacyCredentialClient)
                    } else {
                        credentialClient
                    },
                    sslSocketFactory = if (legacy) {
                        requireNotNull(platformRelayClient).sslSocketFactory
                    } else {
                        relayTrust.socketFactory
                    },
                    originValidator = if (legacy) {
                        ::requireExactLegacyRelayOrigin
                    } else {
                        relayTrust::requireAllowedOrigin
                    },
                    onSessionRevoked = outbox::clearScope,
                )
            },
            enrollmentExchange = { bootstrap, deviceProofKey ->
                credentialClient.exchange(bootstrap, deviceName, deviceProofKey)
            },
            originMigrationProbe = { candidate, deviceProofKey ->
                val refreshedCandidate = credentialClient.refreshForOriginMigration(
                    candidate,
                    deviceProofKey,
                    relayStore,
                )
                originAuthenticator.authenticate(refreshedCandidate, deviceProofKey)
            },
            migrationTargetOrigin = relayTrust.authorizedOrigin,
        )
    }

    internal fun creationCountForTests(): Long = creationCount.get()
}

private fun requireExactLegacyRelayOrigin(origin: String) {
    if (RelayOrigin.normalize(origin) != LEGACY_RELAY_ORIGIN) {
        throw GatewayTransportException("Legacy Relay address is not authorized")
    }
}

@OptIn(ExperimentalCoroutinesApi::class)
internal class SwitchingCodexGateway(
    private val store: SecureGatewayConfigStore,
    private val retainDurableOutboxScope: (String) -> Unit = {},
    private val clearDurableOutboxScope: (String) -> Unit = {},
    private val remoteGatewayFactory: (RemoteGatewaySession, DeviceProofKeyHandle) -> CodexGateway =
        { session, deviceProofKey ->
            RemoteCodexGateway(
                sessionStore = store,
                session = session,
                deviceProofKey = deviceProofKey,
                onDeviceIdentityInvalidated = clearDurableOutboxScope,
            )
    },
    private val createOrLoadPairingKey: () -> DeviceProofKeyHandle =
        AndroidDeviceIdentity::createOrLoadForPairing,
    private val loadExistingKey: () -> DeviceProofKeyHandle = AndroidDeviceIdentity::loadExisting,
    private val deviceNameProvider: () -> String = {
        listOf(Build.MANUFACTURER, Build.MODEL)
            .filter(String::isNotBlank)
            .joinToString(" ")
            .take(80)
    },
    private val pairingExchange: suspend (
        SecureGatewayConfigStore,
        String,
        DeviceProofKeyHandle,
        (String) -> Unit,
    ) -> RemoteGatewaySession = { pairingStore, deviceName, deviceProofKey, afterSessionReplace ->
        RemotePairingClient.exchange(
            store = pairingStore,
            deviceName = deviceName,
            deviceProofKey = deviceProofKey,
            afterSessionReplace = afterSessionReplace,
        )
    },
    initialGateway: CodexGateway? = null,
) : CodexGateway, PairingGatewayControls, DemoGatewayControls, CloseableCodexGateway {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val router = GenerationDelegateRouter(initialGateway ?: selectInitialGateway(), scope)
    private val pairingMutex = Mutex()
    override val sessionScope: String get() = router.sessionScope
    override val sessionIdentity: StateFlow<GatewaySessionIdentity> = router.sessionIdentity
    override val kind: StateFlow<GatewayKind> = router.kind
    override val connectionState: StateFlow<GatewayConnectionState> = router.connectionState
    override val threads: StateFlow<List<CodexThread>> = router.threads

    override suspend fun pair(baseUrl: String, pairingCode: String) = pairingMutex.withLock {
        // The current server exchange revokes the previous device credential immediately. Until
        // a two-phase server protocol exists, a live REMOTE session cannot be safely re-paired:
        // any later client failure would leave the "restored" old token already invalid remotely.
        if (router.kind.value == GatewayKind.REMOTE || store.loadSession() != null) {
            store.clearPairing()
            throw GatewayTransportException("Re-pairing is unavailable")
        }
        try {
            requireSecureBaseUrl(baseUrl)
            if (pairingCode.length !in 8..512) throw GatewayTransportException("Pairing rejected")
            store.savePairing(PairingBootstrap(baseUrl, pairingCode))
            val deviceProofKey = createOrLoadPairingKey()
            val session = pairingExchange(
                store,
                deviceNameProvider(),
                deviceProofKey,
                { newSessionScope ->
                    // The new session is already durable; cleanup is best effort.
                    // Session scoping remains the safety boundary if this is interrupted.
                    runCatching { retainDurableOutboxScope(newSessionScope) }
                },
            )
            router.replace(remoteGatewayFactory(session, deviceProofKey))
        } catch (cancelled: CancellationException) {
            store.clearPairing()
            throw cancelled
        } catch (_: Exception) {
            store.clearPairing()
            throw GatewayTransportException("Pairing failed")
        }
    }

    override fun setOnline(online: Boolean) {
        (router.currentGateway as? DemoGatewayControls)?.setOnline(online)
    }

    override suspend fun createThread(title: String): CodexThread = router.createThread(title)
    override suspend fun renameThread(threadKey: ThreadKey, title: String) =
        router.renameThread(threadKey, title)
    override suspend fun archiveThread(threadKey: ThreadKey) = router.archiveThread(threadKey)
    override suspend fun deleteThread(threadKey: ThreadKey) = router.deleteThread(threadKey)
    override suspend fun loadConversation(threadKey: ThreadKey): ConversationSnapshot =
        router.loadConversation(threadKey)
    override fun observeConversation(
        threadKey: ThreadKey,
        afterStreamId: String,
        afterStreamSeq: Long,
    ): Flow<ConversationEvent> = router.observeConversation(threadKey, afterStreamId, afterStreamSeq)
    override suspend fun loadCapabilities(
        expectedSessionScope: String,
        expectedSessionGeneration: Long,
    ) = router.loadCapabilities(expectedSessionScope, expectedSessionGeneration)
    override suspend fun uploadTurnImage(
        threadKey: ThreadKey,
        clientMessageId: String,
        clientAssetId: String,
        bytes: ByteArray,
        expectedSessionScope: String,
        expectedSessionGeneration: Long,
    ) = router.uploadTurnImage(
        threadKey,
        clientMessageId,
        clientAssetId,
        bytes,
        expectedSessionScope,
        expectedSessionGeneration,
    )
    override suspend fun sendTurnInput(
        threadKey: ThreadKey,
        input: List<TurnInput>,
        idempotencyKey: String,
        expectedSessionScope: String,
        expectedSessionGeneration: Long,
    ) = router.sendTurnInput(
        threadKey,
        input,
        idempotencyKey,
        expectedSessionScope,
        expectedSessionGeneration,
    )
    override suspend fun sendTurn(
        threadKey: ThreadKey,
        text: String,
        idempotencyKey: String,
        expectedSessionScope: String,
    ) = router.sendTurn(threadKey, text, idempotencyKey, expectedSessionScope)
    override suspend fun interruptTurn(
        threadKey: ThreadKey,
        expectedTurnId: String?,
        expectedSessionScope: String,
        expectedSessionGeneration: Long,
    ) = router.interruptTurn(threadKey, expectedTurnId, expectedSessionScope, expectedSessionGeneration)

    override suspend fun performAction(actionRef: String, expectedSessionScope: String) =
        router.performAction(actionRef, expectedSessionScope)

    internal suspend fun replaceDelegate(next: CodexGateway) {
        router.replace(next)
    }

    override fun close() {
        router.close()
        scope.cancel()
    }

    private fun selectInitialGateway(): CodexGateway {
        val session = store.loadSession()
        if (session != null) {
            return try {
                val deviceProofKey = loadExistingKey()
                remoteGatewayFactory(session, deviceProofKey)
            } catch (_: Exception) {
                store.clearSessionIfScope(session.scopeKey())
                runCatching { clearDurableOutboxScope(session.scopeKey()) }
                UnconfiguredCodexGateway
            }
        }
        if (BuildConfig.DEBUG && BuildConfig.DEMO_GATEWAY_ENABLED && store.loadPairing() == null) {
            return FakeCodexGateway()
        }
        return UnconfiguredCodexGateway
    }
}

@OptIn(ExperimentalCoroutinesApi::class)
internal class GenerationDelegateRouter(
    initialGateway: CodexGateway,
    scope: CoroutineScope,
) {
    private data class DelegateGeneration(val generation: Long, val gateway: CodexGateway)
    private data class ObserverCapture(
        val delegate: DelegateGeneration,
        val lifecycleVersion: Long,
    )

    private val operationLock = Any()
    private val closed = AtomicBoolean(false)
    private val delegate = MutableStateFlow(DelegateGeneration(0, initialGateway))
    private val observerLifecycleVersion = MutableStateFlow(0L)
    private val mutableSessionIdentity = MutableStateFlow(
        GatewaySessionIdentity(initialGateway.sessionScope, 0),
    )
    val sessionScope: String get() = delegate.value.gateway.sessionScope
    val sessionIdentity: StateFlow<GatewaySessionIdentity> = mutableSessionIdentity
    val currentGateway: CodexGateway get() = delegate.value.gateway
    val kind: StateFlow<GatewayKind> = delegate
        .flatMapLatest { it.gateway.kind }
        .stateIn(scope, SharingStarted.Eagerly, initialGateway.kind.value)
    val connectionState: StateFlow<GatewayConnectionState> = delegate
        .flatMapLatest { it.gateway.connectionState }
        .stateIn(scope, SharingStarted.Eagerly, initialGateway.connectionState.value)
    val threads: StateFlow<List<CodexThread>> = delegate
        .flatMapLatest { it.gateway.threads }
        .stateIn(scope, SharingStarted.Eagerly, initialGateway.threads.value)

    suspend fun replace(
        next: CodexGateway,
        carryOverSameScopeOutbox: Boolean = false,
    ) {
        val previous = synchronized(operationLock) {
            if (closed.get()) null else {
                val current = delegate.value
                val replacement = DelegateGeneration(current.generation + 1, next)
                val outboxCarryoverFromGeneration = current.generation.takeIf {
                    carryOverSameScopeOutbox &&
                        current.gateway.sessionScope == replacement.gateway.sessionScope
                }
                // Close the UI/operation gate before exposing the next delegate. This ordering
                // also protects same-scope replacement where a string-only check is useless.
                mutableSessionIdentity.value = GatewaySessionIdentity(
                    replacement.gateway.sessionScope,
                    replacement.generation,
                    outboxCarryoverFromGeneration,
                )
                delegate.value = replacement
                observerLifecycleVersion.value += 1
                current.gateway
            }
        }
        if (previous == null) {
            (next as? CloseableCodexGateway)?.close()
            throw GatewayTransportException("Gateway is closed")
        }
        (previous as? CloseableCodexGateway)?.close()
    }

    fun close() {
        val current = synchronized(operationLock) {
            if (!closed.compareAndSet(false, true)) return
            // Active observers run in their callers' scopes, not this router's scope. Advancing
            // this gate makes close observable so their watcher can cancel and join the delegate.
            observerLifecycleVersion.value += 1
            delegate.value.gateway
        }
        (current as? CloseableCodexGateway)?.close()
    }

    suspend fun createThread(title: String): CodexThread =
        withCurrent { it.createThread(title) }
    suspend fun renameThread(threadKey: ThreadKey, title: String) =
        withCurrent { it.renameThread(threadKey, title) }
    suspend fun archiveThread(threadKey: ThreadKey) = withCurrent { it.archiveThread(threadKey) }
    suspend fun deleteThread(threadKey: ThreadKey) = withCurrent { it.deleteThread(threadKey) }
    suspend fun loadConversation(threadKey: ThreadKey): ConversationSnapshot =
        withCurrent { it.loadConversation(threadKey) }

    fun observeConversation(
        threadKey: ThreadKey,
        afterStreamId: String,
        afterStreamSeq: Long,
    ): Flow<ConversationEvent> = channelFlow {
        val captured = synchronized(operationLock) {
            if (closed.get()) null else ObserverCapture(
                delegate = delegate.value,
                lifecycleVersion = observerLifecycleVersion.value,
            )
        }
        if (captured == null) {
            close(GatewayTransportException("Gateway is closed"))
            return@channelFlow
        }
        val collector = launch {
            try {
                captured.delegate.gateway
                    .observeConversation(threadKey, afterStreamId, afterStreamSeq)
                    .collect { event ->
                        if (
                            !closed.get() &&
                            observerLifecycleVersion.value == captured.lifecycleVersion &&
                            delegate.value.generation == captured.delegate.generation
                        ) {
                            send(event)
                        }
                    }
            } finally {
                channel.close()
            }
        }
        val generationWatcher = launch {
            observerLifecycleVersion.first { it != captured.lifecycleVersion }
            collector.cancelAndJoin()
        }
        awaitClose {
            collector.cancel()
            generationWatcher.cancel()
        }
    }

    suspend fun loadCapabilities(
        expectedSessionScope: String,
        expectedSessionGeneration: Long,
    ): GatewayCapabilities = withExpectedSession(
        expectedSessionScope,
        expectedSessionGeneration,
    ) { captured ->
        captured.loadCapabilities(expectedSessionScope, captured.sessionIdentity.value.generation)
    }

    suspend fun uploadTurnImage(
        threadKey: ThreadKey,
        clientMessageId: String,
        clientAssetId: String,
        bytes: ByteArray,
        expectedSessionScope: String,
        expectedSessionGeneration: Long,
    ): UploadedImageAsset = withExpectedSession(
        expectedSessionScope,
        expectedSessionGeneration,
    ) { captured ->
        captured.uploadTurnImage(
            threadKey,
            clientMessageId,
            clientAssetId,
            bytes,
            expectedSessionScope,
            captured.sessionIdentity.value.generation,
        )
    }

    suspend fun sendTurnInput(
        threadKey: ThreadKey,
        input: List<TurnInput>,
        idempotencyKey: String,
        expectedSessionScope: String,
        expectedSessionGeneration: Long,
    ) = withExpectedSession(expectedSessionScope, expectedSessionGeneration) { captured ->
        captured.sendTurnInput(
            threadKey,
            input,
            idempotencyKey,
            expectedSessionScope,
            captured.sessionIdentity.value.generation,
        )
    }

    suspend fun sendTurn(
        threadKey: ThreadKey,
        text: String,
        idempotencyKey: String,
        expectedSessionScope: String,
    ) = withExpectedSession(expectedSessionScope) { captured ->
        captured.sendTurn(threadKey, text, idempotencyKey, expectedSessionScope)
    }

    suspend fun interruptTurn(
        threadKey: ThreadKey,
        expectedTurnId: String?,
        expectedSessionScope: String,
        expectedSessionGeneration: Long,
    ) = withExpectedSession(expectedSessionScope, expectedSessionGeneration) { captured ->
        captured.interruptTurn(threadKey, expectedTurnId, expectedSessionScope,
            captured.sessionIdentity.value.generation)
    }

    suspend fun performAction(actionRef: String, expectedSessionScope: String) =
        withExpectedSession(expectedSessionScope) { captured ->
            captured.performAction(actionRef, expectedSessionScope)
        }

    private suspend fun <T> withCurrent(block: suspend (CodexGateway) -> T): T {
        val captured = delegate.value
        val target = synchronized(operationLock) {
            if (closed.get() || delegate.value.generation != captured.generation) {
                throw GatewayTransportException("Gateway session changed")
            }
            captured.gateway
        }
        return block(target)
    }

    private suspend fun <T> withExpectedSession(
        expectedSessionScope: String,
        expectedSessionGeneration: Long? = null,
        block: suspend (CodexGateway) -> T,
    ): T {
        val captured = delegate.value
        val target = synchronized(operationLock) {
            val current = delegate.value
            if (
                closed.get() ||
                current.generation != captured.generation ||
                current.gateway.sessionScope != expectedSessionScope ||
                (expectedSessionGeneration != null && current.generation != expectedSessionGeneration)
            ) {
                throw GatewayTransportException("Gateway session changed")
            }
            current.gateway
        }
        return block(target)
    }
}

internal object UnconfiguredCodexGateway : CodexGateway {
    override val sessionScope: String = "unconfigured"
    override val kind: StateFlow<GatewayKind> = MutableStateFlow(GatewayKind.UNCONFIGURED)
    override val connectionState: StateFlow<GatewayConnectionState> =
        MutableStateFlow(GatewayConnectionState.OFFLINE)
    override val threads: StateFlow<List<CodexThread>> = MutableStateFlow(emptyList())

    override suspend fun createThread(title: String): CodexThread = unavailable()
    override suspend fun renameThread(threadKey: ThreadKey, title: String) = unavailable<Unit>()
    override suspend fun archiveThread(threadKey: ThreadKey) = unavailable<Unit>()
    override suspend fun deleteThread(threadKey: ThreadKey) = unavailable<Unit>()
    override suspend fun loadConversation(threadKey: ThreadKey): ConversationSnapshot = unavailable()
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
    ): TurnSubmission = unavailable()
    override suspend fun performAction(actionRef: String, expectedSessionScope: String) = unavailable<Unit>()

    private fun <T> unavailable(): T = throw GatewayTransportException("Gateway is not paired")
}
