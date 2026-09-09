package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.BuildConfig
import com.cheby.codex.mobile.data.RelayDeviceSession
import com.cheby.codex.mobile.data.SecureGatewayConfigStore
import com.cheby.codex.mobile.data.SecureRelayConfigStore
import com.cheby.codex.mobile.data.scopeKey
import com.cheby.codex.mobile.model.CodexThread
import com.cheby.codex.mobile.model.ConversationEvent
import com.cheby.codex.mobile.model.ConversationSnapshot
import com.cheby.codex.mobile.model.GatewayConnectionState
import com.cheby.codex.mobile.model.ThreadKey
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

interface AssistantEnrollmentControls {
    suspend fun connect(relayOrigin: String, accessKey: String, secretKey: String)
    suspend fun changeRelayOrigin(relayOrigin: String) {
        throw GatewayTransportException("Relay origin migration is unavailable")
    }
}

internal data class RelayEnrollmentInvitation(
    val assistantId: String,
    val pairingSecret: String,
) {
    override fun toString(): String = "RelayEnrollmentInvitation(redacted)"
}

internal data class RelayEnrollmentBootstrap(
    val assistantId: String,
    val assistantAlias: String,
    val relayOrigin: String,
    val pairingSecret: String,
) {
    override fun toString(): String = "RelayEnrollmentBootstrap(redacted)"
}

/**
 * Relay account owner for Android. It reuses the existing generation router, outbox scope and UI
 * reducer while replacing direct-host identity with a durable assistant-account binding.
 */
internal class RelaySwitchingCodexGateway(
    private val store: SecureRelayConfigStore,
    private val retainDurableOutboxScope: (String) -> Unit = {},
    private val clearDurableOutboxScope: (String) -> Unit = {},
    private val relayGatewayFactory: (RelayDeviceSession, DeviceProofKeyHandle) -> CodexGateway =
        { _, _ -> throw GatewayTransportException("Relay wire contract is unavailable") },
    private val createOrLoadEnrollmentKey: () -> DeviceProofKeyHandle =
        AndroidDeviceIdentity::createOrLoadForRelayEnrollment,
    private val loadExistingKey: () -> DeviceProofKeyHandle = AndroidDeviceIdentity::loadExistingRelay,
    private val enrollmentExchange: suspend (
        RelayEnrollmentBootstrap,
        DeviceProofKeyHandle,
    ) -> RelayDeviceSession = { _, _ ->
        throw GatewayTransportException("Relay enrollment is unavailable")
    },
    private val originMigrationProbe: suspend (
        RelayDeviceSession,
        DeviceProofKeyHandle,
    ) -> RelayDeviceSession = { _, _ ->
        throw GatewayTransportException("Relay origin migration is unavailable")
    },
    private val originMigrationDispatcher: CoroutineDispatcher = Dispatchers.IO,
    private val legacyOriginMigrationEnabled: Boolean =
        BuildConfig.LEGACY_RELAY_MIGRATION_ENABLED,
    private val migrationTargetOrigin: String? = null,
    private val waitBeforeOriginMigrationRetry: suspend (Int) -> Unit = { attempt ->
        val boundedAttempt = attempt.coerceIn(0, MAX_ORIGIN_MIGRATION_BACKOFF_EXPONENT)
        delay(
            (ORIGIN_MIGRATION_RETRY_BASE_MILLIS * (1L shl boundedAttempt))
                .coerceAtMost(ORIGIN_MIGRATION_RETRY_MAX_MILLIS),
        )
    },
    initialGateway: CodexGateway? = null,
) : CodexGateway, AssistantEnrollmentControls, DemoGatewayControls, CloseableCodexGateway {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val router = GenerationDelegateRouter(initialGateway ?: selectInitialGateway(), scope)
    private val enrollmentMutex = Mutex()

    override val sessionScope: String get() = router.sessionScope
    override val sessionIdentity: StateFlow<GatewaySessionIdentity> = router.sessionIdentity
    override val kind: StateFlow<GatewayKind> = router.kind
    override val connectionState: StateFlow<GatewayConnectionState> = router.connectionState
    override val threads: StateFlow<List<CodexThread>> = router.threads

    init {
        if (legacyOriginMigrationEnabled && migrationTargetOrigin != null) {
            scope.launch {
                var attempt = 0
                while (migratePinnedOriginIfEligible()) {
                    waitBeforeOriginMigrationRetry(attempt)
                    attempt += 1
                }
            }
        }
    }

    override suspend fun connect(
        relayOrigin: String,
        accessKey: String,
        secretKey: String,
    ) = enrollmentMutex.withLock {
        if (router.kind.value == GatewayKind.RELAY || store.loadSession() != null) {
            throw GatewayTransportException("Assistant is already bound")
        }
        var savedScope: String? = null
        try {
            val alias = DEFAULT_ASSISTANT_ALIAS
            if (
                !ACCESS_KEY.matches(accessKey) ||
                !SECRET_KEY.matches(secretKey) ||
                secretKey.any(Char::isISOControl)
            ) {
                throw GatewayTransportException("Enrollment rejected")
            }
            val normalizedOrigin = RelayOrigin.normalize(relayOrigin)
            val bootstrap = RelayEnrollmentBootstrap(
                assistantId = accessKey,
                assistantAlias = alias,
                relayOrigin = normalizedOrigin,
                pairingSecret = secretKey,
            )
            val proofKey = createOrLoadEnrollmentKey()
            val candidate = enrollmentExchange(bootstrap, proofKey).copy(assistantAlias = alias)
            if (
                candidate.assistantId != bootstrap.assistantId ||
                candidate.relayOrigin != bootstrap.relayOrigin
            ) {
                throw GatewayTransportException("Relay binding mismatch")
            }
            if (!store.saveSession(candidate)) {
                throw GatewayTransportException("Enrollment persistence failed")
            }
            savedScope = candidate.bindingScope
            val next = relayGatewayFactory(candidate, proofKey)
            runCatching { retainDurableOutboxScope(candidate.bindingScope) }
            router.replace(next)
        } catch (cancelled: CancellationException) {
            savedScope?.let(store::clearSessionIfScope)
            throw cancelled
        } catch (_: Exception) {
            savedScope?.let {
                store.clearSessionIfScope(it)
                runCatching { clearDurableOutboxScope(it) }
            }
            throw GatewayTransportException("Assistant enrollment failed")
        }
    }

    override suspend fun changeRelayOrigin(relayOrigin: String) = enrollmentMutex.withLock {
        val current = store.loadSession()
            ?: throw GatewayTransportException("Assistant is not bound")
        val targetOrigin = try {
            RelayOrigin.normalize(relayOrigin)
        } catch (_: Exception) {
            throw GatewayTransportException("Relay address is invalid")
        }
        if (targetOrigin == current.relayOrigin) return@withLock
        val proofKey = try {
            loadExistingKey()
        } catch (_: Exception) {
            throw GatewayTransportException("Relay device identity is unavailable")
        }
        migrateAuthenticatedOrigin(current, targetOrigin, proofKey)
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
    override suspend fun performAction(actionRef: String, expectedSessionScope: String) =
        router.performAction(actionRef, expectedSessionScope)

    internal suspend fun replaceDelegate(next: CodexGateway) = router.replace(next)

    override fun close() {
        router.close()
        scope.cancel()
    }

    private fun selectInitialGateway(): CodexGateway {
        val session = store.loadSession()
        if (session != null) {
            return try {
                val key = loadExistingKey()
                relayGatewayFactory(session, key)
            } catch (_: Exception) {
                store.clearSessionIfScope(session.bindingScope)
                runCatching { clearDurableOutboxScope(session.bindingScope) }
                UnconfiguredCodexGateway
            }
        }
        if (BuildConfig.DEBUG && BuildConfig.DEMO_GATEWAY_ENABLED) {
            return FakeCodexGateway()
        }
        return UnconfiguredCodexGateway
    }

    /**
     * Returns true only while the durable session still needs a retry.
     *
     * The migration build may be installed during a short network or Edge outage. A one-shot
     * probe would then strand the phone on the legacy origin and make the final fixed-origin build
     * unusable. Retrying is safe because the probe is read-only and the final write is an origin-
     * only compare-and-set against the latest durable session.
     */
    private suspend fun migratePinnedOriginIfEligible(): Boolean = enrollmentMutex.withLock {
        val targetOrigin = migrationTargetOrigin ?: return@withLock false
        val current = store.loadSession()?.takeIf {
            it.relayOrigin == LEGACY_RELAY_ORIGIN
        } ?: return@withLock false
        val proofKey = try {
            loadExistingKey()
        } catch (_: Exception) {
            return@withLock true
        }
        try {
            migrateAuthenticatedOrigin(current, targetOrigin, proofKey)
            false
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: Exception) {
            store.loadSession()?.relayOrigin == LEGACY_RELAY_ORIGIN
        }
    }

    private suspend fun migrateAuthenticatedOrigin(
        current: RelayDeviceSession,
        targetOrigin: String,
        proofKey: DeviceProofKeyHandle,
    ) {
        val candidate = current.copy(relayOrigin = targetOrigin)
        // A probe performs a TLS connect, handshake and authenticated Ready exchange. Address
        // changes are initiated by the Main-scoped ViewModel, so never perform this blocking I/O
        // on its caller thread. The mutex remains held across the probe and the following CAS to
        // preserve the single-writer migration contract.
        val verified = withContext(originMigrationDispatcher) {
            originMigrationProbe(candidate, proofKey)
        }
        if (
            verified.assistantId != current.assistantId ||
            verified.bindingScope != current.bindingScope ||
            verified.principalId != current.principalId ||
            verified.relayOrigin != targetOrigin
        ) {
            throw GatewayTransportException("Relay binding mismatch")
        }
        val committed = store.migrateRelayOrigin(
            expectedBindingScope = current.bindingScope,
            expectedOrigin = current.relayOrigin,
            newOrigin = targetOrigin,
        ) ?: throw GatewayTransportException("Relay origin changed concurrently")
        router.replace(
            relayGatewayFactory(committed, proofKey),
            carryOverSameScopeOutbox = true,
        )
    }

    private companion object {
        const val DEFAULT_ASSISTANT_ALIAS = "我的 Codex"
        val ACCESS_KEY = Regex("^asst_[A-Za-z0-9_-]{22}$")
        val SECRET_KEY = Regex("^pair_[A-Za-z0-9_-]{27,123}$")
        const val ORIGIN_MIGRATION_RETRY_BASE_MILLIS = 1_000L
        const val ORIGIN_MIGRATION_RETRY_MAX_MILLIS = 60_000L
        const val MAX_ORIGIN_MIGRATION_BACKOFF_EXPONENT = 6
    }
}

internal val LEGACY_RELAY_ORIGIN: String
    get() = BuildConfig.LEGACY_RELAY_ORIGIN

/**
 * A direct REMOTE credential is a different security principal. Migration erases it and its
 * replayable state; it is never interpreted as a Relay device credential.
 */
internal fun quarantineLegacyDirectCredentials(
    legacyStore: SecureGatewayConfigStore,
    clearDurableOutboxScope: (String) -> Unit,
) {
    legacyStore.clearPairing()
    val legacySession = legacyStore.loadSession()
    legacyStore.clearSession()
    legacySession?.scopeKey()?.let { runCatching { clearDurableOutboxScope(it) } }
}
