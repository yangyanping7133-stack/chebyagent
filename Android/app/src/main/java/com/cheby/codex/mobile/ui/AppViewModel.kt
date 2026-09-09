package com.cheby.codex.mobile.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.cheby.codex.mobile.data.LastThreadStore
import com.cheby.codex.mobile.data.DurableOutboxStore
import com.cheby.codex.mobile.data.InMemoryDurableOutboxStore
import com.cheby.codex.mobile.data.OutboxEntry
import com.cheby.codex.mobile.data.OutboxKey
import com.cheby.codex.mobile.data.OutboxState
import com.cheby.codex.mobile.data.PerThreadOutboxPlanner
import com.cheby.codex.mobile.data.key
import com.cheby.codex.mobile.gateway.CodexGateway
import com.cheby.codex.mobile.gateway.CloseableCodexGateway
import com.cheby.codex.mobile.gateway.DeliveryCrashCheckpoint
import com.cheby.codex.mobile.gateway.DeliveryCrashIdentity
import com.cheby.codex.mobile.gateway.DeliveryCrashTestHook
import com.cheby.codex.mobile.gateway.DemoGatewayControls
import com.cheby.codex.mobile.gateway.GatewayKind
import com.cheby.codex.mobile.gateway.GatewayTransportException
import com.cheby.codex.mobile.gateway.GatewayRequestException
import com.cheby.codex.mobile.gateway.GatewaySessionIdentity
import com.cheby.codex.mobile.gateway.LocalImageCapability
import com.cheby.codex.mobile.gateway.LocalRuntimeGatewayControls
import com.cheby.codex.mobile.gateway.LocalThreadModelControls
import com.cheby.codex.mobile.gateway.AvailableModel
import com.cheby.codex.mobile.gateway.OpenAiAccountGatewayControls
import com.cheby.codex.mobile.gateway.OpenAiAccountState
import com.cheby.codex.mobile.gateway.OpenAiDeviceLogin
import com.cheby.codex.mobile.gateway.NoOpDeliveryCrashTestHook
import com.cheby.codex.mobile.gateway.AssistantEnrollmentControls
import com.cheby.codex.mobile.gateway.TurnInput
import com.cheby.codex.mobile.gateway.requireGateOnlyDeliveryCrashHook
import com.cheby.codex.mobile.gateway.reachedForGate
import com.cheby.codex.mobile.model.CodexThread
import com.cheby.codex.mobile.model.ConversationReducer
import com.cheby.codex.mobile.model.ConversationReductionDisposition
import com.cheby.codex.mobile.model.ConversationEvent
import com.cheby.codex.mobile.model.GatewayConnectionState
import com.cheby.codex.mobile.model.RichMessage
import com.cheby.codex.mobile.model.RichMessageState
import com.cheby.codex.mobile.model.MessageRole
import com.cheby.codex.mobile.model.ThreadConversationState
import com.cheby.codex.mobile.model.ThreadKey
import com.cheby.codex.mobile.model.ThreadRunState
import com.cheby.codex.mobile.model.TurnLifecyclePhase
import com.cheby.codex.mobile.model.resolveInitialThread
import com.cheby.codex.mobile.model.orderedBlocks
import com.cheby.codex.mobile.model.readableText
import java.util.UUID
import kotlinx.coroutines.Job
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

data class AppUiState(
    val currentSessionScope: String = "",
    val currentSessionGeneration: Long = 0,
    val threads: List<CodexThread> = emptyList(),
    val currentThreadKey: ThreadKey? = null,
    val currentConversation: ThreadConversationState? = null,
    val isConversationRecovering: Boolean = false,
    val connectionState: GatewayConnectionState = GatewayConnectionState.CONNECTING,
    val gatewayKind: GatewayKind = GatewayKind.UNCONFIGURED,
    val localDeliveryNotices: Map<OutboxKey, String> = emptyMap(),
    val isLoading: Boolean = true,
    val showPairing: Boolean = false,
    val isPairing: Boolean = false,
    val pairingError: String? = null,
    val outbox: List<OutboxEntry> = emptyList(),
    val sendingOutboxKeys: Set<OutboxKey> = emptySet(),
    val acceptedUserMessages: List<AcceptedUserMessage> = emptyList(),
    val actionRetryNotBeforeMillis: Map<String, Long> = emptyMap(),
    val localImageCapability: LocalImageCapability? = null,
    val availableModels: List<AvailableModel> = emptyList(),
    val isCapabilityLoading: Boolean = true,
    val selectedImages: List<PendingImageAttachment> = emptyList(),
    val preparingOutboxKey: OutboxKey? = null,
    val attachmentError: String? = null,
    val stoppingTurn: Boolean = false,
    val stopTurnError: String? = null,
) {
    val currentThread: CodexThread?
        get() = threads.firstOrNull { it.key == currentThreadKey }

    val messages: List<RichMessage>
        get() = currentConversation?.messages.orEmpty()

    val canStopTurn: Boolean
        get() = gatewayKind == GatewayKind.LOCAL && currentThread?.state in setOf(
            ThreadRunState.RUNNING, ThreadRunState.WAITING_USER, ThreadRunState.WAITING_APPROVAL,
        )

    val canSend: Boolean
        get() {
            val conversation = currentConversation ?: return false
            return currentThreadKey != null &&
                conversation.threadKey == currentThreadKey &&
                !conversation.resyncRequired &&
                !isConversationRecovering
        }
}

data class AcceptedUserMessage(
    val clientMessageId: String,
    val sessionScope: String,
    val sessionGeneration: Long,
    val threadKey: ThreadKey,
    val text: String,
    val createdAtMillis: Long,
    val imageCount: Int = 0,
)

class ImageSelectionTicket internal constructor(
    internal val sessionIdentity: GatewaySessionIdentity,
    internal val threadKey: ThreadKey,
    internal val conversationGeneration: Long,
)

class AppViewModel internal constructor(
    private val gateway: CodexGateway,
    private val lastThreadStore: LastThreadStore,
    private val outboxStore: DurableOutboxStore = InMemoryDurableOutboxStore(),
    private val beforeSuccessfulSendCommit: suspend (OutboxEntry) -> Unit = {},
    private val imageAttachmentReader: ImageAttachmentReader? = null,
    private val deliveryCrashTestHook: DeliveryCrashTestHook = NoOpDeliveryCrashTestHook,
) : ViewModel() {
    /**
     * Composer-only state belongs to the ViewModel so Activity recreation cannot detach an
     * outstanding picker result or erase another thread's draft/scroll position. It is never
     * written to saved state or disk.
     */
    internal val threadUiStates = ThreadUiStateRegistry()
    private val initialIdentity = gateway.sessionIdentity.value
    private val initialOutbox = outboxStore.run {
        list()
            .asSequence()
            .filter { it.sessionScope == initialIdentity.scope }
            .maxOfOrNull(OutboxEntry::sessionGeneration)
            ?.let { predecessor ->
                adoptScopeGeneration(
                    activeSessionScope = initialIdentity.scope,
                    expectedSessionGeneration = predecessor,
                    activeSessionGeneration = initialIdentity.generation,
                )
            }
        clearOtherGenerations(initialIdentity.scope, initialIdentity.generation)
        list()
    }
    private val _uiState = MutableStateFlow(
        AppUiState(
            currentSessionScope = initialIdentity.scope,
            currentSessionGeneration = initialIdentity.generation,
            gatewayKind = gateway.kind.value,
            outbox = initialOutbox.filter {
                it.sessionScope == initialIdentity.scope &&
                    it.sessionGeneration == initialIdentity.generation
            },
        ),
    )
    val uiState: StateFlow<AppUiState> = _uiState.asStateFlow()
    private var conversationJob: Job? = null
    private var initialSelectionResolved = false
    private var activeSessionIdentity = initialIdentity
    private var conversationGeneration = 0L
    private var conversationSawOffline = false
    private val inFlightOutboxKeys = mutableSetOf<OutboxKey>()
    /** A retryable failure is attempted once per live connection unless the user retries it. */
    private val retryBlockedOutboxKeys = mutableSetOf<OutboxKey>()
    private val activeTurnIdsByThread = mutableMapOf<ThreadKey, String>()
    private val recentlyTerminalTurns = LinkedHashSet<String>()
    private val reconciledOutboxThreads = mutableSetOf<ThreadKey>()
    private val outboxRecoveryJobs = mutableMapOf<ThreadKey, Job>()
    private var imageSelectionJob: Job? = null
    private var imageTurnJob: Job? = null
    private var pendingImageSelectionTicket: ImageSelectionTicket? = null

    init {
        requireGateOnlyDeliveryCrashHook(deliveryCrashTestHook)
        viewModelScope.launch {
            combine(
                gateway.threads,
                gateway.connectionState,
                gateway.kind,
                gateway.sessionIdentity,
            ) { threads, connection, kind, identity ->
                GatewayUiInputs(threads, connection, kind, identity)
            }.collectLatest { (threads, connection, kind, identity) ->
                    var current = _uiState.value.currentThreadKey
                    if (identity != activeSessionIdentity) {
                        cancelAttachmentWork()
                        activeTurnIdsByThread.clear()
                        recentlyTerminalTurns.clear()
                        retryBlockedOutboxKeys.clear()
                        reconciledOutboxThreads.clear()
                        outboxRecoveryJobs.values.forEach(Job::cancel)
                        outboxRecoveryJobs.clear()
                        val carriesOverOutbox =
                            identity.scope == activeSessionIdentity.scope &&
                                identity.generation == activeSessionIdentity.generation + 1 &&
                                identity.outboxCarryoverFromGeneration == activeSessionIdentity.generation
                        if (carriesOverOutbox) {
                            outboxStore.adoptScopeGeneration(
                                activeSessionScope = identity.scope,
                                expectedSessionGeneration = requireNotNull(
                                    identity.outboxCarryoverFromGeneration,
                                ),
                                activeSessionGeneration = identity.generation,
                            )
                        }
                        // Never retain or revive rows from an older, non-predecessor generation.
                        // On an adoption conflict, this keeps only rows already written by the
                        // active generation and fails closed for the predecessor queue.
                        outboxStore.clearOtherGenerations(identity.scope, identity.generation)
                        activeSessionIdentity = identity
                        conversationSawOffline = false
                        conversationJob?.cancel()
                        conversationGeneration += 1
                        initialSelectionResolved = false
                        current = null
                        _uiState.update {
                            it.copy(
                                currentSessionScope = identity.scope,
                                currentSessionGeneration = identity.generation,
                                threads = threads,
                                currentThreadKey = null,
                                currentConversation = null,
                                isConversationRecovering = false,
                                connectionState = connection,
                                gatewayKind = kind,
                                outbox = scopedOutbox(identity.scope),
                                sendingOutboxKeys = emptySet(),
                                actionRetryNotBeforeMillis = emptyMap(),
                                localImageCapability = null,
                                availableModels = emptyList(),
                                isCapabilityLoading = true,
                                selectedImages = emptyList(),
                                preparingOutboxKey = null,
                                attachmentError = null,
                                showPairing = if (kind == GatewayKind.UNCONFIGURED) true else it.showPairing,
                                isLoading = kind.isAssistantNetwork() && connection == GatewayConnectionState.CONNECTING,
                            )
                        }
                    }
                    val priorState = _uiState.value
                    val priorCurrent = priorState.threads.firstOrNull { it.key == current }
                    val currentHasLocalTurn = current != null && (
                        priorState.outbox.any { it.threadKey == current } ||
                            priorState.acceptedUserMessages.any { it.threadKey == current }
                        )
                    val coldStartProvisional = if (
                        !initialSelectionResolved &&
                        current == null &&
                        connection == GatewayConnectionState.ONLINE &&
                        kind.isAssistantNetwork()
                    ) {
                        provisionalThreadFromOutbox(
                            rememberedThread = lastThreadStore.read(),
                            catalog = threads,
                            identity = identity,
                        )
                    } else {
                        null
                    }
                    val restorableThreads = coldStartProvisional
                        ?.let { provisional -> listOf(provisional) + threads }
                        ?: threads
                    val effectiveThreads = if (
                        current != null &&
                        priorCurrent != null &&
                        currentHasLocalTurn &&
                        restorableThreads.none { it.key == current } &&
                        kind.isAssistantNetwork()
                    ) {
                        // A just-created Codex thread is absent from thread/list until its first
                        // turn persists. Keep that provisional selection while its durable local
                        // turn survives a reconnect/catalog refresh.
                        listOf(priorCurrent) + restorableThreads
                    } else if (
                        current != null &&
                        restorableThreads.isEmpty() &&
                        priorCurrent != null &&
                        kind.isAssistantNetwork() &&
                        (
                            connection != GatewayConnectionState.ONLINE ||
                                priorState.connectionState != GatewayConnectionState.ONLINE
                        )
                    ) {
                        // Relay reconnects briefly publish an empty list before the
                        // authoritative list is reloaded. Preserve the visible selection so a
                        // just-sent turn cannot appear to vanish into a blank new conversation.
                        priorState.threads
                    } else {
                        restorableThreads
                    }
                    if (
                        !initialSelectionResolved &&
                        effectiveThreads.isEmpty() &&
                        kind.isAssistantNetwork() &&
                        connection == GatewayConnectionState.CONNECTING
                    ) {
                        _uiState.update {
                            it.copy(
                                currentSessionScope = identity.scope,
                                currentSessionGeneration = identity.generation,
                                threads = effectiveThreads,
                                connectionState = connection,
                                gatewayKind = kind,
                                outbox = scopedOutbox(identity.scope),
                                showPairing = if (kind == GatewayKind.UNCONFIGURED) true else it.showPairing,
                                isLoading = kind.isAssistantNetwork(),
                            )
                        }
                        return@collectLatest
                    }
                    val selected = when {
                        !initialSelectionResolved -> {
                            initialSelectionResolved = true
                            resolveInitialThread(lastThreadStore.read(), effectiveThreads)
                        }
                        current != null && effectiveThreads.any { it.key == current } -> current
                        else -> effectiveThreads.firstOrNull()?.key
                    }
                    _uiState.update {
                        it.copy(
                            currentSessionScope = identity.scope,
                            currentSessionGeneration = identity.generation,
                            threads = effectiveThreads,
                            currentThreadKey = selected,
                            connectionState = connection,
                            gatewayKind = kind,
                            outbox = scopedOutbox(identity.scope),
                            showPairing = if (kind == GatewayKind.UNCONFIGURED) true else it.showPairing,
                            isLoading = false,
                        )
                    }
                    reconcileWaitingTurnIds(effectiveThreads, identity)
                    val reconnectedCurrent =
                        selected != null &&
                            connection == GatewayConnectionState.ONLINE &&
                            conversationSawOffline &&
                            (
                                priorState.currentConversation?.messages?.any {
                                    it.state == RichMessageState.STREAMING
                                } == true ||
                                    activeTurnIdsByThread.containsKey(selected)
                            )
                    if (selected != current || reconnectedCurrent) selectThread(selected, identity)
                    if (connection != GatewayConnectionState.ONLINE) {
                        retryBlockedOutboxKeys.clear()
                    }
                    if (connection == GatewayConnectionState.OFFLINE) {
                        conversationSawOffline = true
                    } else if (connection == GatewayConnectionState.ONLINE) {
                        conversationSawOffline = false
                    }
                    if (connection == GatewayConnectionState.ONLINE) {
                        recoverOutboxThreads(identity, effectiveThreads)
                        drainOutbox(identity)
                    }
                }
        }
        viewModelScope.launch {
            combine(gateway.sessionIdentity, gateway.connectionState) { identity, connection ->
                identity to connection
            }.collectLatest { (identity, connection) ->
                _uiState.first { it.matches(identity) }
                _uiState.update { state ->
                    if (!state.matches(identity)) state else state.copy(
                        localImageCapability = null,
                        availableModels = emptyList(),
                        isCapabilityLoading = true,
                    )
                }
                val capabilities = if (connection == GatewayConnectionState.OFFLINE) {
                    null
                } else {
                    try {
                        gateway.loadCapabilities(identity.scope, identity.generation)
                    } catch (_: GatewayTransportException) {
                        null
                    }
                }
                val capabilityLostWithAttachments = capabilities?.localImage == null &&
                    _uiState.value.matches(identity) &&
                    (
                        _uiState.value.selectedImages.isNotEmpty() ||
                            _uiState.value.preparingOutboxKey != null
                        )
                if (capabilityLostWithAttachments) cancelAttachmentWork()
                _uiState.update { state ->
                    if (!state.matches(identity) || !gatewayIdentityMatches(identity)) state else state.copy(
                        localImageCapability = capabilities?.localImage,
                        availableModels = capabilities?.models.orEmpty(),
                        isCapabilityLoading = false,
                        attachmentError = if (capabilityLostWithAttachments) {
                            "Codex 当前不能接收图片，请连接恢复后重新选择。"
                        } else {
                            state.attachmentError
                        },
                    )
                }
            }
        }
    }

    fun selectThread(threadKey: ThreadKey?) {
        selectThread(threadKey, gateway.sessionIdentity.value)
    }

    private fun selectThread(
        threadKey: ThreadKey?,
        expectedIdentity: GatewaySessionIdentity,
    ) {
        if (!gatewayIdentityMatches(expectedIdentity)) return
        if (_uiState.value.currentThreadKey != threadKey) cancelAttachmentWork()
        if (threadKey == null) {
            conversationJob?.cancel()
            conversationGeneration += 1
            lastThreadStore.write(null)
            _uiState.update {
                if (!it.matches(expectedIdentity)) it else it.copy(
                    currentThreadKey = null,
                    stopTurnError = null,
                    currentConversation = null,
                    isConversationRecovering = false,
                    selectedImages = emptyList(),
                    preparingOutboxKey = null,
                    attachmentError = null,
                )
            }
            return
        }

        conversationJob?.cancel()
        conversationGeneration += 1
        val capturedConversationGeneration = conversationGeneration
        lastThreadStore.write(threadKey)
        _uiState.update {
            if (!it.matches(expectedIdentity)) it else it.copy(
                currentThreadKey = threadKey,
                stopTurnError = null,
                currentConversation = null,
                isConversationRecovering = true,
                selectedImages = emptyList(),
                preparingOutboxKey = null,
                attachmentError = null,
            )
        }
        conversationJob = viewModelScope.launch {
            try {
                var rejectedSnapshotAttempts = 0
                conversationLoop@ while (
                    currentCoroutineContext().isActive &&
                    isActiveConversationSelection(
                        threadKey,
                        expectedIdentity,
                        capturedConversationGeneration,
                    )
                ) {
                    val snapshot = try {
                        gateway.loadConversation(threadKey)
                    } catch (request: GatewayRequestException) {
                        if (!request.retryable) throw request
                        markConversationRecovering(
                            threadKey,
                            expectedIdentity,
                            capturedConversationGeneration,
                        )
                        delay(resyncReloadDelayMillis(rejectedSnapshotAttempts++))
                        continue@conversationLoop
                    } catch (_: GatewayTransportException) {
                        // The selection can be made in the short interval between catalog
                        // recovery and a usable Node connection. Keep retrying this exact thread
                        // instead of leaving the UI permanently stuck without a snapshot.
                        markConversationRecovering(
                            threadKey,
                            expectedIdentity,
                            capturedConversationGeneration,
                        )
                        delay(resyncReloadDelayMillis(rejectedSnapshotAttempts++))
                        continue@conversationLoop
                    }
                    if (
                        !isActiveConversationSelection(
                            threadKey,
                            expectedIdentity,
                            capturedConversationGeneration,
                        )
                    ) return@launch
                    var conversation = ConversationReducer.fromSnapshot(snapshot)
                    if (conversation.threadKey != threadKey || conversation.resyncRequired) {
                        markConversationRecovering(
                            threadKey,
                            expectedIdentity,
                            capturedConversationGeneration,
                        )
                        delay(resyncReloadDelayMillis(rejectedSnapshotAttempts))
                        rejectedSnapshotAttempts += 1
                        continue
                    }
                    publishConversation(
                        threadKey,
                        expectedIdentity,
                        capturedConversationGeneration,
                        conversation,
                    )
                    rejectedSnapshotAttempts = 0
                    var subscriptionRetryAttempts = 0
                    subscriptionLoop@ while (
                        currentCoroutineContext().isActive &&
                        isActiveConversationSelection(
                            threadKey,
                            expectedIdentity,
                            capturedConversationGeneration,
                        )
                    ) {
                        try {
                            gateway.observeConversation(
                                threadKey,
                                conversation.streamId,
                                conversation.streamSeq,
                            ).collect { event ->
                                if (
                                    !isActiveConversationSelection(
                                        threadKey,
                                        expectedIdentity,
                                        capturedConversationGeneration,
                                    )
                                ) {
                                    throw GatewaySessionChangedSignal()
                                }
                                val reduced = ConversationReducer.reduceWithDisposition(conversation, event)
                                conversation = reduced.state
                                if (
                                    reduced.disposition ==
                                    ConversationReductionDisposition.RESYNC_REQUIRED
                                ) {
                                    invalidateOutboxReconciliation(expectedIdentity)
                                    markConversationRecovering(
                                        threadKey,
                                        expectedIdentity,
                                        capturedConversationGeneration,
                                    )
                                    throw ConversationResyncSignal()
                                }
                                if (
                                    reduced.disposition == ConversationReductionDisposition.APPLIED &&
                                    event is ConversationEvent.TurnLifecycle
                                ) {
                                    handleTurnLifecycle(event, expectedIdentity)
                                }
                                publishConversation(
                                    threadKey,
                                    expectedIdentity,
                                    capturedConversationGeneration,
                                    conversation,
                                )
                                subscriptionRetryAttempts = 0
                            }
                        } catch (_: ConversationResyncSignal) {
                            delay(RESYNC_RETRY_DELAY_MS)
                            continue@conversationLoop
                        } catch (_: GatewaySessionChangedSignal) {
                            continue@conversationLoop
                        } catch (request: GatewayRequestException) {
                            if (!request.retryable) {
                                markConversationRecovering(
                                    threadKey,
                                    expectedIdentity,
                                    capturedConversationGeneration,
                                )
                                delay(RESYNC_RETRY_DELAY_MS)
                                continue@conversationLoop
                            }
                            delay(resyncReloadDelayMillis(subscriptionRetryAttempts++))
                            continue@subscriptionLoop
                        } catch (_: GatewayTransportException) {
                            // Keep the last valid conversation visible and resume this exact
                            // thread/cursor after a transient Relay or Connector interruption.
                            delay(resyncReloadDelayMillis(subscriptionRetryAttempts++))
                            continue@subscriptionLoop
                        }
                        if (!gatewayIdentityMatches(expectedIdentity)) return@launch
                        break@conversationLoop
                    }
                }
            } catch (_: GatewayTransportException) {
                _uiState.update {
                    if (
                        it.matches(expectedIdentity) &&
                        it.currentThreadKey == threadKey &&
                        conversationGeneration == capturedConversationGeneration
                    ) {
                        it.copy(currentConversation = null, isConversationRecovering = false)
                    } else {
                        it
                    }
                }
            }
        }
        if (gateway.connectionState.value == GatewayConnectionState.ONLINE) {
            recoverOutboxThreads(expectedIdentity, gateway.threads.value)
        }
    }

    private fun gatewayIdentityMatches(expected: GatewaySessionIdentity): Boolean =
        gateway.sessionIdentity.value == expected && gateway.sessionScope == expected.scope

    private fun AppUiState.matches(expected: GatewaySessionIdentity): Boolean =
        currentSessionScope == expected.scope && currentSessionGeneration == expected.generation

    private fun isActiveComposerSelection(
        threadKey: ThreadKey,
        expectedIdentity: GatewaySessionIdentity,
        expectedConversationGeneration: Long,
    ): Boolean =
        gatewayIdentityMatches(expectedIdentity) &&
            conversationGeneration == expectedConversationGeneration &&
            _uiState.value.matches(expectedIdentity) &&
            _uiState.value.currentThreadKey == threadKey

    private fun requireActiveComposerSelection(
        threadKey: ThreadKey,
        expectedIdentity: GatewaySessionIdentity,
        expectedConversationGeneration: Long,
    ) {
        if (!isActiveComposerSelection(threadKey, expectedIdentity, expectedConversationGeneration)) {
            throw CancellationException("Composer selection changed")
        }
    }

    private fun updateAttachmentUploadState(
        expectedIdentity: GatewaySessionIdentity,
        threadKey: ThreadKey,
        clientAssetId: String,
        uploadState: AttachmentUploadState,
    ) {
        _uiState.update { state ->
            if (!state.matches(expectedIdentity) || state.currentThreadKey != threadKey) state else state.copy(
                selectedImages = state.selectedImages.map { attachment ->
                    if (attachment.clientAssetId == clientAssetId) {
                        attachment.copy(uploadState = uploadState)
                    } else {
                        attachment
                    }
                },
            )
        }
    }

    private fun clearPreparedImages(
        expectedIdentity: GatewaySessionIdentity,
        threadKey: ThreadKey,
        outboxKey: OutboxKey,
        error: String? = null,
    ) {
        _uiState.update { state ->
            if (
                !state.matches(expectedIdentity) ||
                state.currentThreadKey != threadKey ||
                state.preparingOutboxKey != outboxKey
            ) {
                state
            } else {
                state.copy(
                    selectedImages = emptyList(),
                    preparingOutboxKey = null,
                    attachmentError = error,
                )
            }
        }
    }

    private fun cancelAttachmentWork() {
        pendingImageSelectionTicket = null
        imageSelectionJob?.cancel()
        imageSelectionJob = null
        val selected = _uiState.value.selectedImages
        val activeTurn = imageTurnJob
        imageTurnJob = null
        _uiState.update {
            it.copy(
                selectedImages = emptyList(),
                preparingOutboxKey = null,
                attachmentError = null,
            )
        }
        if (activeTurn?.isActive == true) {
            activeTurn.invokeOnCompletion { selected.wipeImageBytes() }
            activeTurn.cancel()
        } else {
            selected.wipeImageBytes()
        }
    }

    private fun isActiveConversationSelection(
        threadKey: ThreadKey,
        expectedIdentity: GatewaySessionIdentity,
        expectedConversationGeneration: Long,
    ): Boolean =
        gatewayIdentityMatches(expectedIdentity) &&
            conversationGeneration == expectedConversationGeneration &&
            _uiState.value.matches(expectedIdentity) &&
            _uiState.value.currentThreadKey == threadKey

    private fun markConversationRecovering(
        threadKey: ThreadKey,
        expectedIdentity: GatewaySessionIdentity,
        expectedConversationGeneration: Long,
    ) {
        _uiState.update { state ->
            if (
                !gatewayIdentityMatches(expectedIdentity) ||
                conversationGeneration != expectedConversationGeneration ||
                !state.matches(expectedIdentity) ||
                state.currentThreadKey != threadKey
            ) {
                state
            } else {
                state.copy(
                    currentConversation = null,
                    isConversationRecovering = true,
                )
            }
        }
    }

    private fun publishConversation(
        threadKey: ThreadKey,
        expectedIdentity: GatewaySessionIdentity,
        expectedConversationGeneration: Long,
        conversation: ThreadConversationState,
    ) {
        if (conversation.resyncRequired || conversation.threadKey != threadKey) return
        var published = false
        _uiState.update { state ->
            if (
                !gatewayIdentityMatches(expectedIdentity) ||
                conversationGeneration != expectedConversationGeneration ||
                !state.matches(expectedIdentity) ||
                state.currentThreadKey != threadKey
            ) {
                state
            } else {
                published = true
                reconcileAccepted(
                    state.copy(
                        currentConversation = conversation,
                        isConversationRecovering = false,
                    ),
                    conversation,
                )
            }
        }
        if (published && gatewayIdentityMatches(expectedIdentity)) {
            reconcileOutboxSnapshot(
                conversation = conversation,
                thread = gateway.threads.value.firstOrNull { it.key == threadKey },
                identity = expectedIdentity,
            )
            reconciledOutboxThreads += threadKey
            publishOutbox()
            recoverOutboxThreads(expectedIdentity, gateway.threads.value)
            drainOutbox(expectedIdentity)
        }
    }

    private fun invalidateOutboxReconciliation(identity: GatewaySessionIdentity) {
        if (!gatewayIdentityMatches(identity)) return
        reconciledOutboxThreads.clear()
        outboxRecoveryJobs.values.toList().forEach(Job::cancel)
        outboxRecoveryJobs.clear()
    }

    fun createThread() {
        launchGatewayForCurrentSession { identity ->
            val thread = gateway.createThread("新会话")
            if (gatewayIdentityMatches(identity)) selectThread(thread.key, identity)
        }
    }

    fun renameThread(threadKey: ThreadKey, title: String) {
        launchGatewayForCurrentSession { gateway.renameThread(threadKey, title) }
    }

    fun archiveThread(threadKey: ThreadKey) {
        launchGatewayForCurrentSession { gateway.archiveThread(threadKey) }
    }

    fun deleteThread(threadKey: ThreadKey) {
        launchGatewayForCurrentSession { gateway.deleteThread(threadKey) }
    }

    fun createImageSelectionTicket(): ImageSelectionTicket? {
        val identity = gateway.sessionIdentity.value
        val state = _uiState.value
        val threadKey = state.currentThreadKey ?: return null
        if (
            state.localImageCapability == null ||
            !state.canSend ||
            state.preparingOutboxKey != null ||
            state.selectedImages.size >= state.localImageCapability.maxImagesPerTurn ||
            !gatewayIdentityMatches(identity) ||
            !state.matches(identity)
        ) return null
        return ImageSelectionTicket(identity, threadKey, conversationGeneration)
    }

    /** Captures only session/thread generations before the system document picker opens. */
    fun beginImageSelection(): Boolean {
        val ticket = createImageSelectionTicket() ?: return false
        pendingImageSelectionTicket = ticket
        return true
    }

    /**
     * Consumes the ViewModel-owned picker request exactly once after any Activity recreation.
     * URI strings are handed directly to the transient reader job and are never retained here.
     */
    fun completeImageSelection(sourceUris: List<String>) {
        val ticket = pendingImageSelectionTicket ?: return
        pendingImageSelectionTicket = null
        if (sourceUris.isNotEmpty()) selectImages(sourceUris, ticket)
    }

    fun selectImages(sourceUris: List<String>, ticket: ImageSelectionTicket) {
        val reader = imageAttachmentReader ?: return
        val identity = ticket.sessionIdentity
        val state = _uiState.value
        val threadKey = ticket.threadKey
        val capability = state.localImageCapability ?: return
        if (
            sourceUris.isEmpty() ||
            !state.canSend ||
            state.preparingOutboxKey != null ||
            !gatewayIdentityMatches(identity) ||
            !state.matches(identity) ||
            state.currentThreadKey != threadKey ||
            conversationGeneration != ticket.conversationGeneration
        ) return
        val remaining = capability.maxImagesPerTurn - state.selectedImages.size
        if (sourceUris.size > remaining) {
            _uiState.update {
                if (!it.matches(identity) || it.currentThreadKey != threadKey) it else {
                    it.copy(
                        attachmentError =
                            "一条消息最多添加 ${capability.maxImagesPerTurn} 张图片，请重新选择。",
                    )
                }
            }
            return
        }
        val capturedConversationGeneration = ticket.conversationGeneration
        imageSelectionJob?.cancel()
        imageSelectionJob = viewModelScope.launch {
            var decoded: List<PendingImageAttachment> = emptyList()
            try {
                decoded = reader.read(sourceUris, capability)
                if (
                    !isActiveComposerSelection(
                        threadKey,
                        identity,
                        capturedConversationGeneration,
                    )
                ) {
                    decoded.wipeImageBytes()
                    return@launch
                }
                var accepted = false
                _uiState.update { current ->
                    if (
                        !current.matches(identity) ||
                        current.currentThreadKey != threadKey ||
                        current.preparingOutboxKey != null ||
                        current.selectedImages.size + decoded.size > capability.maxImagesPerTurn
                    ) {
                        current
                    } else {
                        accepted = true
                        current.copy(
                            selectedImages = current.selectedImages + decoded,
                            attachmentError = null,
                        )
                    }
                }
                if (!accepted) decoded.wipeImageBytes()
            } catch (cancelled: CancellationException) {
                decoded.wipeImageBytes()
                throw cancelled
            } catch (error: ImageSelectionException) {
                if (
                    isActiveComposerSelection(
                        threadKey,
                        identity,
                        capturedConversationGeneration,
                    )
                ) {
                    val message = when (error.reason) {
                        ImageSelectionFailure.TOO_LARGE ->
                            "这张图片过大，请选择不超过 8 MB、边长不超过 12000 像素的图片。"
                        ImageSelectionFailure.UNSUPPORTED ->
                            "只能添加 JPEG 或 PNG 图片，请重新选择。"
                        ImageSelectionFailure.INVALID_IMAGE ->
                            "这张图片无法安全读取，请换一张再试。"
                        ImageSelectionFailure.UNREADABLE ->
                            "图片读取失败，请重新选择。"
                    }
                    _uiState.update { current ->
                        if (!current.matches(identity) || current.currentThreadKey != threadKey) current else {
                            current.copy(attachmentError = message)
                        }
                    }
                }
            } finally {
                if (imageSelectionJob === currentCoroutineContext()[Job]) imageSelectionJob = null
            }
        }
    }

    fun removeSelectedImage(clientAssetId: String) {
        val identity = gateway.sessionIdentity.value
        var removed: PendingImageAttachment? = null
        _uiState.update { state ->
            if (
                !state.matches(identity) ||
                state.preparingOutboxKey != null ||
                !gatewayIdentityMatches(identity)
            ) {
                state
            } else {
                removed = state.selectedImages.firstOrNull { it.clientAssetId == clientAssetId }
                state.copy(
                    selectedImages = state.selectedImages.filterNot { it.clientAssetId == clientAssetId },
                    attachmentError = null,
                )
            }
        }
        removed?.let { listOf(it).wipeImageBytes() }
    }

    fun dismissAttachmentError() {
        _uiState.update { it.copy(attachmentError = null) }
    }

    fun stopCurrentTurn() {
        val identity = gateway.sessionIdentity.value
        val state = _uiState.value
        if (!gatewayIdentityMatches(identity) || !state.matches(identity) ||
            !state.canStopTurn || state.stoppingTurn) return
        val threadKey = state.currentThreadKey ?: return
        val turnId = activeTurnIdsByThread[threadKey] ?: state.currentThread?.lastTurnId
        _uiState.update { it.copy(stoppingTurn = true, stopTurnError = null) }
        viewModelScope.launch {
            try {
                gateway.interruptTurn(threadKey, turnId, identity.scope, identity.generation)
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (_: Exception) {
                _uiState.update { current ->
                    if (!current.matches(identity) || current.currentThreadKey != threadKey) current
                    else current.copy(stopTurnError = "尚未确认停止，请检查连接后重试。")
                }
            } finally {
                _uiState.update { current ->
                    if (!current.matches(identity)) current else current.copy(stoppingTurn = false)
                }
            }
        }
    }

    fun send(text: String): Boolean {
        val identity = gateway.sessionIdentity.value
        val state = _uiState.value
        if (!gatewayIdentityMatches(identity) || !state.matches(identity) || !state.canSend) return false
        val threadKey = state.currentThreadKey ?: return false
        val normalized = text.trim()
        val selectedImages = state.selectedImages
        if (normalized.isBlank() && selectedImages.isEmpty()) return false
        if (selectedImages.isNotEmpty()) {
            val capability = state.localImageCapability ?: return false
            if (
                selectedImages.size > capability.maxImagesPerTurn ||
                state.preparingOutboxKey != null
            ) return false
            return beginImageTurn(
                identity = identity,
                threadKey = threadKey,
                normalizedText = normalized,
                attachments = selectedImages,
            )
        }
        val entry = OutboxEntry(
            clientMessageId = UUID.randomUUID().toString(),
            sessionScope = identity.scope,
            threadKey = threadKey,
            text = normalized,
            state = OutboxState.QUEUED,
            createdAtMillis = System.currentTimeMillis(),
            sessionGeneration = identity.generation,
        )
        if (!outboxStore.enqueue(entry)) return false
        deliveryCrashTestHook.reachedForGate(
            DeliveryCrashCheckpoint.ENQUEUE_PERSISTED,
            entry.deliveryCrashIdentity(),
        )
        publishOutbox()
        if (gateway.kind.value == GatewayKind.LOCAL) {
            dispatchOutbox(entry, identity)
        } else {
            drainOutbox(identity)
        }
        return true
    }

    private fun beginImageTurn(
        identity: GatewaySessionIdentity,
        threadKey: ThreadKey,
        normalizedText: String,
        attachments: List<PendingImageAttachment>,
    ): Boolean {
        val clientMessageId = UUID.randomUUID().toString()
        val prepared = OutboxEntry(
            clientMessageId = clientMessageId,
            sessionScope = identity.scope,
            threadKey = threadKey,
            text = normalizedText,
            state = OutboxState.NEEDS_ATTACHMENTS,
            createdAtMillis = System.currentTimeMillis(),
            sessionGeneration = identity.generation,
            pendingImageCount = attachments.size,
        )
        if (!outboxStore.enqueue(prepared)) return false
        val capturedConversationGeneration = conversationGeneration
        _uiState.update { state ->
            if (!state.matches(identity) || state.currentThreadKey != threadKey) state else state.copy(
                preparingOutboxKey = prepared.key,
                attachmentError = null,
                selectedImages = state.selectedImages.map { it.copy(uploadState = AttachmentUploadState.SELECTED) },
            )
        }
        publishOutbox()
        imageTurnJob = viewModelScope.launch {
            val assetRefs = mutableListOf<String>()
            try {
                attachments.forEach { attachment ->
                    requireActiveComposerSelection(
                        threadKey,
                        identity,
                        capturedConversationGeneration,
                    )
                    updateAttachmentUploadState(identity, threadKey, attachment.clientAssetId, AttachmentUploadState.UPLOADING)
                    val uploaded = gateway.uploadTurnImage(
                        threadKey = threadKey,
                        clientMessageId = clientMessageId,
                        clientAssetId = attachment.clientAssetId,
                        bytes = attachment.sourceBytes,
                        expectedSessionScope = identity.scope,
                        expectedSessionGeneration = identity.generation,
                    )
                    requireActiveComposerSelection(
                        threadKey,
                        identity,
                        capturedConversationGeneration,
                    )
                    assetRefs += uploaded.assetRef
                    updateAttachmentUploadState(identity, threadKey, attachment.clientAssetId, AttachmentUploadState.UPLOADED)
                }
                val ready = prepared.copy(
                    state = OutboxState.QUEUED,
                    imageAssetRefs = assetRefs.toList(),
                    pendingImageCount = 0,
                )
                requireActiveComposerSelection(
                    threadKey,
                    identity,
                    capturedConversationGeneration,
                )
                if (
                    !outboxStore.replacePrepared(
                        replacement = ready,
                        expectedSessionScope = identity.scope,
                        expectedSessionGeneration = identity.generation,
                        activeSessionScope = gateway.sessionIdentity.value.scope,
                        activeSessionGeneration = gateway.sessionIdentity.value.generation,
                    )
                ) {
                    throw GatewayTransportException("Image turn persistence failed")
                }
                clearPreparedImages(identity, threadKey, prepared.key)
                attachments.wipeImageBytes()
                publishOutbox()
                if (gateway.kind.value == GatewayKind.LOCAL) {
                    dispatchOutbox(ready, identity)
                } else {
                    drainOutbox(identity)
                }
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (_: GatewayTransportException) {
                if (
                    isActiveComposerSelection(
                        threadKey,
                        identity,
                        capturedConversationGeneration,
                    )
                ) {
                    clearPreparedImages(
                        identity,
                        threadKey,
                        prepared.key,
                        "图片未发送。请重新选择图片后再发送；文字不会单独发送。",
                    )
                }
            } finally {
                attachments.wipeImageBytes()
                if (imageTurnJob === currentCoroutineContext()[Job]) imageTurnJob = null
                publishOutbox()
            }
        }
        return true
    }

    fun retryOutbox(key: OutboxKey) {
        val entry = outboxStore.list().firstOrNull { it.key == key } ?: return
        val identity = gateway.sessionIdentity.value
        // Legacy local records are read-only. Only a rejection observed in this UI
        // session can be explicitly retried; uncertain delivery is never replayed.
        if (gateway.kind.value == GatewayKind.LOCAL &&
            (entry.state != OutboxState.RETRYABLE || key !in _uiState.value.localDeliveryNotices)) return
        if (
            entry.sessionScope != identity.scope ||
            entry.sessionGeneration != identity.generation ||
            entry.state !in setOf(OutboxState.QUEUED, OutboxState.RETRYABLE)
        ) return
        if (gateway.kind.value != GatewayKind.LOCAL && entry.retryNotBeforeMillis > System.currentTimeMillis()) return
        retryBlockedOutboxKeys.remove(key)
        if (gateway.kind.value == GatewayKind.LOCAL) {
            dispatchOutbox(entry, identity)
        } else {
            drainOutbox(identity)
        }
    }

    fun discardOutbox(key: OutboxKey) {
        val identity = gateway.sessionIdentity.value
        val entry = outboxStore.list().firstOrNull { it.key == key } ?: return
        if (
            entry.sessionScope != identity.scope ||
            entry.sessionGeneration != identity.generation
        ) return
        if (!gatewayIdentityMatches(identity)) return
        if (entry.state == OutboxState.ACCEPTED || entry.state == OutboxState.WAITING_TURN) return
        outboxStore.remove(
            key = entry.key,
            activeSessionScope = identity.scope,
            activeSessionGeneration = gateway.sessionIdentity.value.generation,
        )
        retryBlockedOutboxKeys.remove(key)
        publishOutbox()
    }

    fun performAction(actionRef: String) {
        val identity = gateway.sessionIdentity.value
        val state = _uiState.value
        if (!gatewayIdentityMatches(identity) || !state.matches(identity) || !state.canSend) return
        if ((state.actionRetryNotBeforeMillis[actionRef] ?: 0) > System.currentTimeMillis()) return
        val expectedSessionScope = identity.scope
        viewModelScope.launch {
            try {
                if (!gatewayIdentityMatches(identity)) return@launch
                gateway.performAction(actionRef, expectedSessionScope)
                if (!gatewayIdentityMatches(identity)) return@launch
                _uiState.update {
                    if (!it.matches(identity)) it else {
                        it.copy(actionRetryNotBeforeMillis = it.actionRetryNotBeforeMillis - actionRef)
                    }
                }
            } catch (error: GatewayRequestException) {
                if (error.code == "RATE_LIMITED" && error.retryAfterMillis != null) {
                    val retryAt = System.currentTimeMillis() + error.retryAfterMillis
                    _uiState.update {
                        if (!it.matches(identity) || !gatewayIdentityMatches(identity)) it else it.copy(
                            actionRetryNotBeforeMillis =
                                it.actionRetryNotBeforeMillis + (actionRef to retryAt),
                        )
                    }
                }
            } catch (_: GatewayTransportException) {
                // The durable action registry retains the same opaque actionRef.
            }
        }
    }

    fun toggleDemoConnection() {
        val controls = gateway as? DemoGatewayControls ?: return
        controls.setOnline(_uiState.value.connectionState != GatewayConnectionState.ONLINE)
    }

    fun applySavedConfiguration(): Boolean =
        (gateway as? LocalRuntimeGatewayControls)?.applySavedConfiguration() == true

    fun selectCurrentThreadModel(model: String, reasoningEffort: String): Boolean {
        val state = _uiState.value
        val threadKey = state.currentThreadKey ?: return false
        val spec = state.availableModels.firstOrNull { it.id == model } ?: return false
        if (spec.supportedReasoningEfforts.none { it.value == reasoningEffort }) return false
        return (gateway as? LocalThreadModelControls)
            ?.selectThreadModel(threadKey, model, reasoningEffort) == true
    }

    suspend fun rebuildProviderSettings(): String? {
        val controls = gateway as? LocalRuntimeGatewayControls ?: return "当前版本没有内置运行环境。"
        return controls.changeConfigurationWhenIdle {
            kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.IO) {
                com.cheby.codex.runtime.EmbeddedRuntimeRegistry.rebuildProviderSettings()
            }
        }
    }

    suspend fun readOpenAiAccount(refreshToken: Boolean = false): OpenAiAccountState =
        (gateway as? OpenAiAccountGatewayControls)?.readOpenAiAccount(refreshToken)
            ?: throw GatewayTransportException("ChatGPT 登录仅支持本机版。")

    suspend fun startOpenAiDeviceLogin(): OpenAiDeviceLogin =
        (gateway as? OpenAiAccountGatewayControls)?.startOpenAiDeviceLogin()
            ?: throw GatewayTransportException("ChatGPT 登录仅支持本机版。")

    suspend fun cancelOpenAiDeviceLogin(loginId: String) {
        (gateway as? OpenAiAccountGatewayControls)?.cancelOpenAiDeviceLogin(loginId)
            ?: throw GatewayTransportException("ChatGPT 登录仅支持本机版。")
    }

    suspend fun logoutOpenAiAccount() {
        (gateway as? OpenAiAccountGatewayControls)?.logoutOpenAiAccount()
            ?: throw GatewayTransportException("ChatGPT 登录仅支持本机版。")
    }

    fun openPairing() {
        _uiState.update { it.copy(showPairing = true, pairingError = null) }
    }

    fun cancelPairing() {
        if (_uiState.value.gatewayKind == GatewayKind.UNCONFIGURED) return
        _uiState.update { it.copy(showPairing = false, pairingError = null, isPairing = false) }
    }

    fun enrollAssistant(relayOrigin: String, accessKey: String, secretKey: String) {
        val controls = gateway as? AssistantEnrollmentControls ?: return
        _uiState.update { it.copy(isPairing = true, pairingError = null) }
        viewModelScope.launch {
            try {
                controls.connect(relayOrigin.trim(), accessKey.trim(), secretKey)
                val pairedIdentity = gateway.sessionIdentity.value
                // Keep the pairing screen over the old content until the session collector has
                // atomically cleared it and installed the new generation boundary.
                _uiState.first { it.matches(pairedIdentity) }
                _uiState.update {
                    if (!it.matches(pairedIdentity) || !gatewayIdentityMatches(pairedIdentity)) it else it.copy(
                        isPairing = false,
                        showPairing = false,
                        pairingError = null,
                    )
                }
            } catch (_: GatewayTransportException) {
                _uiState.update {
                    it.copy(
                        isPairing = false,
                        pairingError = "连接失败，请检查服务器地址、AK 和 SK。",
                    )
                }
            }
        }
    }

    fun changeAssistantServer(relayOrigin: String) {
        val controls = gateway as? AssistantEnrollmentControls ?: return
        _uiState.update { it.copy(isPairing = true, pairingError = null) }
        viewModelScope.launch {
            try {
                controls.changeRelayOrigin(relayOrigin.trim())
                val migratedIdentity = gateway.sessionIdentity.value
                _uiState.first { it.matches(migratedIdentity) }
                _uiState.update {
                    if (
                        !it.matches(migratedIdentity) ||
                        !gatewayIdentityMatches(migratedIdentity)
                    ) it else it.copy(
                        isPairing = false,
                        showPairing = false,
                        pairingError = null,
                    )
                }
            } catch (_: GatewayTransportException) {
                _uiState.update {
                    it.copy(
                        isPairing = false,
                        pairingError = "切换失败，请检查服务器地址和网络。",
                    )
                }
            }
        }
    }

    private fun launchGatewayForCurrentSession(block: suspend (GatewaySessionIdentity) -> Unit) {
        val identity = gateway.sessionIdentity.value
        val state = _uiState.value
        if (!gatewayIdentityMatches(identity) || !state.matches(identity)) return
        viewModelScope.launch {
            try {
                if (!gatewayIdentityMatches(identity)) return@launch
                block(identity)
            } catch (_: GatewayTransportException) {
                // Deliberately silent: no endpoint, token, raw IDs, or stack trace
                // crosses into UI/log output. Connection state carries the result.
            }
        }
    }

    private fun drainOutbox(identity: GatewaySessionIdentity = gateway.sessionIdentity.value) {
        // Official app-server owns execution. Local persisted delivery records are not a
        // scheduler: never replay them on reconnect or let an uncertain old row block new input.
        if (gateway.kind.value == GatewayKind.LOCAL) return
        if (
            !gatewayIdentityMatches(identity) ||
            gateway.connectionState.value != GatewayConnectionState.ONLINE
        ) return
        val inFlight = synchronized(inFlightOutboxKeys) {
            inFlightOutboxKeys.toSet() + retryBlockedOutboxKeys
        }
        PerThreadOutboxPlanner.dispatchable(
            entries = outboxStore.list().filter { it.threadKey in reconciledOutboxThreads },
            sessionScope = identity.scope,
            sessionGeneration = identity.generation,
            nowMillis = System.currentTimeMillis(),
            inFlightOutboxKeys = inFlight,
        ).forEach { dispatchOutbox(it, identity) }
    }

    /**
     * A persisted QUEUED/RETRYABLE row can be the result of a crash after Codex accepted the
     * idempotency key but before Android persisted ACCEPTED. Every affected Thread is therefore
     * snapshot-reconciled before any restart dispatch. Recovery is per Thread, so an unavailable
     * Thread cannot globally serialize healthy Threads.
     */
    private fun recoverOutboxThreads(
        identity: GatewaySessionIdentity,
        threads: List<CodexThread>,
    ) {
        if (gateway.kind.value == GatewayKind.LOCAL) return
        if (!gatewayIdentityMatches(identity)) return
        val byKey = threads.associateBy(CodexThread::key)
        scopedOutbox(identity.scope, identity.generation)
            .map(OutboxEntry::threadKey)
            .distinct()
            .filter {
                (
                    it != _uiState.value.currentThreadKey ||
                        _uiState.value.currentConversation != null
                    ) &&
                    it !in reconciledOutboxThreads &&
                    it !in outboxRecoveryJobs
            }
            .forEach { threadKey ->
                val job = viewModelScope.launch(start = CoroutineStart.LAZY) {
                    try {
                        if (
                            !currentCoroutineContext().isActive ||
                            !gatewayIdentityMatches(identity) ||
                            gateway.connectionState.value != GatewayConnectionState.ONLINE
                        ) return@launch
                        val snapshot = try {
                            gateway.loadConversation(threadKey)
                        } catch (request: GatewayRequestException) {
                            if (!request.retryable) {
                                scopedOutbox(identity.scope, identity.generation)
                                    .filter { it.threadKey == threadKey }
                                    .forEach { markNeedsSync(it, identity) }
                                reconciledOutboxThreads += threadKey
                                publishOutbox()
                            }
                            return@launch
                        } catch (_: GatewayTransportException) {
                            return@launch
                        }
                        val conversation = ConversationReducer.fromSnapshot(snapshot)
                        if (
                            conversation.threadKey != threadKey ||
                            conversation.resyncRequired
                        ) return@launch
                        reconcileOutboxSnapshot(
                            conversation = conversation,
                            thread = byKey[threadKey]
                                ?: gateway.threads.value.firstOrNull { it.key == threadKey },
                            identity = identity,
                        )
                        reconciledOutboxThreads += threadKey
                        publishOutbox()
                        drainOutbox(identity)
                    } finally {
                        outboxRecoveryJobs.remove(threadKey)
                    }
                }
                outboxRecoveryJobs[threadKey] = job
                job.start()
            }
    }

    private fun reconcileOutboxSnapshot(
        conversation: ThreadConversationState,
        thread: CodexThread?,
        identity: GatewaySessionIdentity,
    ) {
        if (gateway.kind.value == GatewayKind.LOCAL) return
        if (
            !gatewayIdentityMatches(identity) ||
            conversation.resyncRequired ||
            conversation.threadKey != thread?.key && thread != null
        ) return
        val canonicalUsers = conversation.messages.asSequence()
            .filter { it.role == MessageRole.USER && it.clientMessageId != null }
            .associateBy { requireNotNull(it.clientMessageId) }
        val terminalTurnIds = conversation.messages.asSequence()
            .filter {
                it.role == MessageRole.ASSISTANT &&
                    it.turnId != null &&
                    it.state.isTerminalTurnEvidence()
            }
            .map { requireNotNull(it.turnId) }
            .toSet()
        outboxStore.list()
            .filter {
                it.sessionScope == identity.scope &&
                    it.sessionGeneration == identity.generation &&
                    it.threadKey == conversation.threadKey
            }
            .forEach { entry ->
                val canonical = canonicalUsers[entry.clientMessageId]
                if (canonical != null) {
                    val turnId = canonical.turnId
                    if (entry.state == OutboxState.ACCEPTED) {
                        val acceptedTurnId = entry.acceptedTurnId
                        when {
                            acceptedTurnId == null || turnId != acceptedTurnId ->
                                markNeedsSync(entry, identity)
                            acceptedTurnId in terminalTurnIds ->
                                completeAcceptedEntry(entry, identity)
                            thread?.takeIf {
                                it.lastTurnId == acceptedTurnId && it.state.isTurnActive()
                            } != null ->
                                activeTurnIdsByThread[entry.threadKey] = acceptedTurnId
                            else -> markNeedsSync(entry, identity)
                        }
                        return@forEach
                    }
                    when {
                        turnId == null -> markNeedsSync(entry, identity)
                        turnId in terminalTurnIds -> completeAcceptedEntry(entry, identity)
                        thread?.takeIf {
                            it.lastTurnId == turnId && it.state.isTurnActive()
                        } != null ->
                            outboxStore.mark(
                                key = entry.key,
                                activeSessionScope = identity.scope,
                                activeSessionGeneration = gateway.sessionIdentity.value.generation,
                                state = OutboxState.ACCEPTED,
                                acceptedTurnId = turnId,
                            )
                        else -> markNeedsSync(entry, identity)
                    }
                    return@forEach
                }
                when (entry.state) {
                    OutboxState.ACCEPTED -> {
                        when {
                            thread?.takeIf {
                                it.lastTurnId == entry.acceptedTurnId && it.state.isTurnActive()
                            } != null ->
                                activeTurnIdsByThread[entry.threadKey] = requireNotNull(entry.acceptedTurnId)
                            thread?.state == ThreadRunState.IDLE ||
                                thread?.state == ThreadRunState.FAILED ->
                                markNeedsSync(entry, identity)
                        }
                    }
                    OutboxState.WAITING_TURN -> {
                        val activeTurnId = thread
                            ?.takeIf { it.state.isTurnActive() }
                            ?.lastTurnId
                        when {
                            activeTurnId == null && thread != null ->
                                outboxStore.mark(
                                    key = entry.key,
                                    activeSessionScope = identity.scope,
                                    activeSessionGeneration = gateway.sessionIdentity.value.generation,
                                    state = OutboxState.QUEUED,
                                )
                            entry.waitingForTurnId == null && activeTurnId != null ->
                                outboxStore.mark(
                                    key = entry.key,
                                    activeSessionScope = identity.scope,
                                    activeSessionGeneration = gateway.sessionIdentity.value.generation,
                                    state = OutboxState.WAITING_TURN,
                                    waitingForTurnId = activeTurnId,
                                )
                            activeTurnId != null && entry.waitingForTurnId != activeTurnId ->
                                markNeedsSync(entry, identity)
                        }
                    }
                    else -> Unit
                }
            }
    }

    private fun markNeedsSync(entry: OutboxEntry, identity: GatewaySessionIdentity) {
        outboxStore.mark(
            key = entry.key,
            activeSessionScope = identity.scope,
            activeSessionGeneration = gateway.sessionIdentity.value.generation,
            state = OutboxState.NEEDS_SYNC,
            acceptedTurnId = entry.acceptedTurnId,
        )
    }

    private fun ThreadRunState.isTurnActive(): Boolean =
        this == ThreadRunState.RUNNING ||
            this == ThreadRunState.WAITING_USER ||
            this == ThreadRunState.WAITING_APPROVAL

    private fun RichMessageState.isTerminalTurnEvidence(): Boolean =
        this == RichMessageState.COMPLETED ||
            this == RichMessageState.FAILED ||
            this == RichMessageState.INTERRUPTED ||
            this == RichMessageState.CANCELLED

    /** A local record is a delivery receipt only; it never schedules server execution. */
    private fun submitLocalInput(entry: OutboxEntry, identity: GatewaySessionIdentity) {
        if (!gatewayIdentityMatches(identity) || entry.sessionScope != identity.scope ||
            entry.sessionGeneration != identity.generation) return
        synchronized(inFlightOutboxKeys) {
            if (!inFlightOutboxKeys.add(entry.key)) return
        }
        _uiState.update { if (!it.matches(identity)) it else it.copy(
            sendingOutboxKeys = it.sendingOutboxKeys + entry.key,
        ) }
        viewModelScope.launch {
            try {
                val receipt = gateway.sendTurnInput(
                    threadKey = entry.threadKey,
                    input = buildList {
                        if (entry.text.isNotBlank()) add(TurnInput.Text(entry.text))
                        entry.imageAssetRefs.forEach { add(TurnInput.Image(it)) }
                    },
                    idempotencyKey = entry.clientMessageId,
                    expectedSessionScope = entry.sessionScope,
                    expectedSessionGeneration = entry.sessionGeneration,
                )
                if (!gatewayIdentityMatches(identity)) return@launch
                if (receipt.threadKey != entry.threadKey || receipt.clientMessageId != entry.clientMessageId) {
                    throw GatewayTransportException("Local receipt identity mismatch")
                }
                completeAcceptedEntry(entry.copy(acceptedTurnId = receipt.turnId), identity)
            } catch (error: GatewayTransportException) {
                if (gatewayIdentityMatches(identity)) {
                    val notice = when {
                        error is GatewayRequestException && error.code == "THREAD_BUSY" ->
                            "官方服务仍在执行上一轮，本条未接受；请停止上一轮或稍后重新发送"
                        error is GatewayRequestException -> "官方服务拒绝了本次输入，未自动重试"
                        gateway.connectionState.value != GatewayConnectionState.ONLINE ->
                            "连接已断开，送达未确认；不会自动重发"
                        else -> "送达未确认；不会自动重发，请先查看会话结果"
                    }
                    _uiState.update { if (!it.matches(identity)) it else it.copy(
                        localDeliveryNotices = it.localDeliveryNotices + (entry.key to notice),
                    ) }
                    outboxStore.mark(
                        key = entry.key,
                        activeSessionScope = identity.scope,
                        activeSessionGeneration = identity.generation,
                        state = when {
                            error is GatewayRequestException && error.code == "THREAD_BUSY" -> OutboxState.RETRYABLE
                            error is GatewayRequestException -> OutboxState.REJECTED
                            else -> OutboxState.NEEDS_SYNC
                        },
                    )
                }
            } finally {
                synchronized(inFlightOutboxKeys) { inFlightOutboxKeys.remove(entry.key) }
                _uiState.update { if (!it.matches(identity)) it else it.copy(
                    sendingOutboxKeys = it.sendingOutboxKeys - entry.key,
                ) }
                publishOutbox()
            }
        }
    }

    private fun dispatchOutbox(entry: OutboxEntry, identity: GatewaySessionIdentity) {
        if (gateway.kind.value == GatewayKind.LOCAL) {
            submitLocalInput(entry, identity)
            return
        }
        if (
            entry.sessionScope != identity.scope ||
            entry.sessionGeneration != identity.generation ||
            entry.state == OutboxState.NEEDS_ATTACHMENTS ||
            !gatewayIdentityMatches(identity)
        ) return
        synchronized(inFlightOutboxKeys) {
            if (!inFlightOutboxKeys.add(entry.key)) return
        }
        _uiState.update { state ->
            if (!state.matches(identity)) state else state.copy(
                sendingOutboxKeys = state.sendingOutboxKeys + entry.key,
            )
        }
        viewModelScope.launch {
            var needsSnapshotRecovery = false
            try {
                if (!gatewayIdentityMatches(identity)) return@launch
                val submission = gateway.sendTurnInput(
                    threadKey = entry.threadKey,
                    input = buildList {
                        if (entry.text.isNotBlank()) add(TurnInput.Text(entry.text))
                        entry.imageAssetRefs.forEach { add(TurnInput.Image(it)) }
                    },
                    idempotencyKey = entry.clientMessageId,
                    expectedSessionScope = entry.sessionScope,
                    expectedSessionGeneration = entry.sessionGeneration,
                )
                if (!gatewayIdentityMatches(identity)) return@launch
                if (
                    submission.threadKey != entry.threadKey ||
                    submission.clientMessageId != entry.clientMessageId
                ) {
                    throw GatewayRequestException(
                        "TURN_DELIVERY_AMBIGUOUS_RESYNC_REQUIRED",
                        false,
                    )
                }
                beforeSuccessfulSendCommit(entry)
                if (!gatewayIdentityMatches(identity)) return@launch
                val persisted = outboxStore.mark(
                    key = entry.key,
                    activeSessionScope = identity.scope,
                    activeSessionGeneration = gateway.sessionIdentity.value.generation,
                    state = OutboxState.ACCEPTED,
                    acceptedTurnId = submission.turnId,
                )
                if (!persisted) {
                    throw GatewayRequestException(
                        "TURN_DELIVERY_AMBIGUOUS_RESYNC_REQUIRED",
                        false,
                    )
                }
                deliveryCrashTestHook.reachedForGate(
                    DeliveryCrashCheckpoint.ANDROID_ACCEPTED,
                    entry.deliveryCrashIdentity(submission.turnId),
                )
                if (
                    submission.status.isTerminalTurnStatus() ||
                    terminalKey(entry.threadKey, submission.turnId) in recentlyTerminalTurns
                ) {
                    outboxStore.list().firstOrNull {
                        it.key == entry.key &&
                            it.state == OutboxState.ACCEPTED &&
                            it.acceptedTurnId == submission.turnId
                    }?.let { completeAcceptedEntry(it, identity) }
                }
            } catch (error: GatewayTransportException) {
                val waitingForTurnId = if (
                    error is GatewayRequestException && error.code == "THREAD_BUSY"
                ) {
                    activeTurnIdsByThread[entry.threadKey]
                        ?: gateway.threads.value.firstOrNull {
                            it.key == entry.threadKey && it.state.isTurnActive()
                        }?.lastTurnId
                } else {
                    null
                }
                val state = when {
                    error !is GatewayRequestException -> OutboxState.NEEDS_SYNC
                    error.code == "THREAD_BUSY" && waitingForTurnId != null ->
                        OutboxState.WAITING_TURN
                    error.code == "THREAD_BUSY" -> OutboxState.NEEDS_SYNC
                    error.retryable -> OutboxState.RETRYABLE
                    error.code in RESYNC_REQUIRED_CODES -> OutboxState.NEEDS_SYNC
                    else -> OutboxState.REJECTED
                }
                val retryAt = if (
                    error is GatewayRequestException &&
                    error.code == "RATE_LIMITED" &&
                    error.retryAfterMillis != null
                ) {
                    System.currentTimeMillis() + error.retryAfterMillis
                } else {
                    0
                }
                if (gatewayIdentityMatches(identity)) {
                    if (state == OutboxState.RETRYABLE) retryBlockedOutboxKeys += entry.key
                    val marked = outboxStore.mark(
                        key = entry.key,
                        activeSessionScope = identity.scope,
                        activeSessionGeneration = gateway.sessionIdentity.value.generation,
                        state = state,
                        retryNotBeforeMillis = retryAt,
                        waitingForTurnId = waitingForTurnId,
                    )
                    if (marked && state == OutboxState.NEEDS_SYNC) {
                        reconciledOutboxThreads.remove(entry.threadKey)
                        outboxRecoveryJobs.remove(entry.threadKey)?.cancel()
                        needsSnapshotRecovery = true
                    }
                }
            } finally {
                synchronized(inFlightOutboxKeys) { inFlightOutboxKeys.remove(entry.key) }
                _uiState.update { state ->
                    if (!state.matches(identity)) state else state.copy(
                        sendingOutboxKeys = state.sendingOutboxKeys - entry.key,
                    )
                }
                publishOutbox()
                if (needsSnapshotRecovery) {
                    recoverOutboxThreads(identity, gateway.threads.value)
                }
                drainOutbox(identity)
            }
        }
    }

    private fun publishOutbox() {
        val identity = gateway.sessionIdentity.value
        _uiState.update { state ->
            if (!gatewayIdentityMatches(identity) || !state.matches(identity)) state else {
                state.copy(outbox = scopedOutbox(identity.scope))
            }
        }
    }

    private fun reconcileWaitingTurnIds(
        threads: List<CodexThread>,
        identity: GatewaySessionIdentity,
    ) {
        if (gateway.kind.value == GatewayKind.LOCAL) return
        if (!gatewayIdentityMatches(identity)) return
        val threadsByKey = threads.associateBy(CodexThread::key)
        val active = threads.filter { it.state.isTurnActive() && it.lastTurnId != null }
            .associate { it.key to requireNotNull(it.lastTurnId) }
        activeTurnIdsByThread.putAll(active)
        val scopedEntries = outboxStore.list()
            .filter {
                it.sessionScope == identity.scope &&
                    it.sessionGeneration == identity.generation
            }
        scopedEntries.filter {
            val thread = threadsByKey[it.threadKey] ?: return@filter false
            when (it.state) {
                OutboxState.ACCEPTED ->
                    !thread.state.isTurnActive() ||
                        thread.lastTurnId != it.acceptedTurnId
                OutboxState.WAITING_TURN ->
                    !thread.state.isTurnActive() ||
                        it.waitingForTurnId != null &&
                        thread.lastTurnId != it.waitingForTurnId
                else -> false
            }
        }.forEach { reconciledOutboxThreads.remove(it.threadKey) }
        scopedEntries.filter {
            it.state == OutboxState.WAITING_TURN && it.waitingForTurnId == null
        }
            .forEach { entry ->
                val turnId = active[entry.threadKey] ?: return@forEach
                outboxStore.mark(
                    key = entry.key,
                    activeSessionScope = identity.scope,
                    activeSessionGeneration = gateway.sessionIdentity.value.generation,
                    state = OutboxState.WAITING_TURN,
                    waitingForTurnId = turnId,
                )
            }
        publishOutbox()
    }

    private fun handleTurnLifecycle(
        event: ConversationEvent.TurnLifecycle,
        identity: GatewaySessionIdentity,
    ) {
        if (!gatewayIdentityMatches(identity)) return
        if (gateway.kind.value == GatewayKind.LOCAL) {
            if (event.phase == TurnLifecyclePhase.STARTED) {
                activeTurnIdsByThread[event.threadKey] = event.turnId
            } else if (activeTurnIdsByThread[event.threadKey] == event.turnId) {
                activeTurnIdsByThread.remove(event.threadKey)
            }
            return
        }
        when (event.phase) {
            TurnLifecyclePhase.STARTED -> {
                activeTurnIdsByThread[event.threadKey] = event.turnId
                outboxStore.list()
                    .filter {
                        it.sessionScope == identity.scope &&
                            it.sessionGeneration == identity.generation &&
                            it.threadKey == event.threadKey &&
                            it.state == OutboxState.WAITING_TURN &&
                            it.waitingForTurnId == null
                    }
                    .forEach { entry ->
                        outboxStore.mark(
                            key = entry.key,
                            activeSessionScope = identity.scope,
                            activeSessionGeneration = gateway.sessionIdentity.value.generation,
                            state = OutboxState.WAITING_TURN,
                            waitingForTurnId = event.turnId,
                        )
                    }
            }
            TurnLifecyclePhase.COMPLETED,
            TurnLifecyclePhase.FAILED,
            TurnLifecyclePhase.INTERRUPTED,
            -> {
                val key = terminalKey(event.threadKey, event.turnId)
                recentlyTerminalTurns += key
                while (recentlyTerminalTurns.size > MAX_RECENT_TERMINAL_TURNS) {
                    recentlyTerminalTurns.remove(recentlyTerminalTurns.first())
                }
                if (activeTurnIdsByThread[event.threadKey] == event.turnId) {
                    activeTurnIdsByThread.remove(event.threadKey)
                }
                outboxStore.list()
                    .filter {
                        it.sessionScope == identity.scope &&
                            it.sessionGeneration == identity.generation &&
                            it.threadKey == event.threadKey
                    }
                    .forEach { entry ->
                        when {
                            entry.state == OutboxState.ACCEPTED &&
                                entry.acceptedTurnId == event.turnId ->
                                completeAcceptedEntry(entry, identity)
                            entry.state == OutboxState.NEEDS_SYNC &&
                                entry.acceptedTurnId == event.turnId ->
                                completeAcceptedEntry(entry, identity)
                            entry.state == OutboxState.WAITING_TURN &&
                                entry.waitingForTurnId == event.turnId ->
                                outboxStore.mark(
                                    key = entry.key,
                                    activeSessionScope = identity.scope,
                                    activeSessionGeneration = gateway.sessionIdentity.value.generation,
                                    state = OutboxState.QUEUED,
                                )
                        }
                    }
            }
        }
        publishOutbox()
        drainOutbox(identity)
    }

    private fun completeAcceptedEntry(
        entry: OutboxEntry,
        identity: GatewaySessionIdentity,
    ) {
        if (
            !outboxStore.remove(
                key = entry.key,
                activeSessionScope = identity.scope,
                activeSessionGeneration = gateway.sessionIdentity.value.generation,
            )
        ) return
        deliveryCrashTestHook.reachedForGate(
            DeliveryCrashCheckpoint.OUTBOX_CLEANED,
            entry.deliveryCrashIdentity(entry.acceptedTurnId),
        )
        _uiState.update { state ->
            if (!state.matches(identity) || !gatewayIdentityMatches(identity)) return@update state
            val canonicalExists = state.currentConversation?.messages.orEmpty().any { message ->
                state.currentConversation?.threadKey == entry.threadKey &&
                    message.role == MessageRole.USER &&
                    (
                        message.clientMessageId == entry.clientMessageId ||
                            (message.clientMessageId == null && messageText(message) == entry.displayText())
                        )
            }
            if (
                canonicalExists ||
                state.acceptedUserMessages.any {
                    it.sessionScope == entry.sessionScope &&
                        it.sessionGeneration == entry.sessionGeneration &&
                        it.threadKey == entry.threadKey &&
                        it.clientMessageId == entry.clientMessageId
                }
            ) {
                state
            } else {
                state.copy(
                    acceptedUserMessages = state.acceptedUserMessages + AcceptedUserMessage(
                        clientMessageId = entry.clientMessageId,
                        sessionScope = entry.sessionScope,
                        sessionGeneration = entry.sessionGeneration,
                        threadKey = entry.threadKey,
                        text = entry.text,
                        createdAtMillis = entry.createdAtMillis,
                        imageCount = entry.imageAssetRefs.size,
                    ),
                )
            }
        }
    }

    private fun terminalKey(threadKey: ThreadKey, turnId: String): String =
        "${threadKey.value}\u0000$turnId"

    private fun scopedOutbox(
        activeSessionScope: String,
        activeSessionGeneration: Long = gateway.sessionIdentity.value.generation,
    ): List<OutboxEntry> = outboxStore.list().filter {
        it.sessionScope == activeSessionScope && it.sessionGeneration == activeSessionGeneration
    }

    private fun provisionalThreadFromOutbox(
        rememberedThread: ThreadKey?,
        catalog: List<CodexThread>,
        identity: GatewaySessionIdentity,
    ): CodexThread? {
        val threadKey = rememberedThread ?: return null
        if (catalog.any { it.key == threadKey }) return null
        val latest = scopedOutbox(identity.scope, identity.generation)
            .filter { it.threadKey == threadKey }
            .maxByOrNull(OutboxEntry::createdAtMillis)
            ?: return null
        return CodexThread(
            key = threadKey,
            title = "新会话",
            preview = latest.displayText().take(120),
            updatedAtMillis = latest.createdAtMillis,
        )
    }

    private fun reconcileAccepted(
        state: AppUiState,
        conversation: ThreadConversationState,
    ): AppUiState {
        val canonicalUsers = conversation.messages
            .filter { it.role == MessageRole.USER }
            .toMutableList()
        val remaining = state.acceptedUserMessages.filter { accepted ->
            if (accepted.sessionScope != state.currentSessionScope) return@filter true
            if (accepted.sessionGeneration != state.currentSessionGeneration) return@filter true
            if (accepted.threadKey != conversation.threadKey) return@filter true
            val match = canonicalUsers.indexOfFirst { message ->
                message.clientMessageId == accepted.clientMessageId ||
                    (message.clientMessageId == null && messageText(message) == accepted.displayText())
            }
            if (match < 0) true else {
                canonicalUsers.removeAt(match)
                false
            }
        }
        return state.copy(acceptedUserMessages = remaining)
    }

    private fun messageText(message: RichMessage): String =
        message.orderedBlocks().joinToString("\n") { it.readableText() }.trim()

    private fun OutboxEntry.displayText(): String = listOfNotNull(
        text.takeIf(String::isNotBlank),
        imageAssetRefs.size.takeIf { it > 0 }?.let { "[图片 × $it]" },
    ).joinToString("\n")

    private fun OutboxEntry.deliveryCrashIdentity(turnId: String? = null) =
        DeliveryCrashIdentity(
            sessionScope = sessionScope,
            sessionGeneration = sessionGeneration,
            threadKey = threadKey,
            clientMessageId = clientMessageId,
            turnId = turnId,
        )

    private fun AcceptedUserMessage.displayText(): String = listOfNotNull(
        text.takeIf(String::isNotBlank),
        imageCount.takeIf { it > 0 }?.let { "[图片 × $it]" },
    ).joinToString("\n")

    internal class Factory(
        private val gateway: CodexGateway,
        private val lastThreadStore: LastThreadStore,
        private val outboxStore: DurableOutboxStore = InMemoryDurableOutboxStore(),
        private val imageAttachmentReader: ImageAttachmentReader? = null,
        private val deliveryCrashTestHook: DeliveryCrashTestHook = NoOpDeliveryCrashTestHook,
    ) : ViewModelProvider.Factory {
        @Suppress("UNCHECKED_CAST")
        override fun <T : ViewModel> create(modelClass: Class<T>): T {
            require(modelClass.isAssignableFrom(AppViewModel::class.java))
            return AppViewModel(
                gateway,
                lastThreadStore,
                outboxStore,
                imageAttachmentReader = imageAttachmentReader,
                deliveryCrashTestHook = deliveryCrashTestHook,
            ) as T
        }
    }

    private companion object {
        val RESYNC_REQUIRED_CODES = setOf("TURN_DELIVERY_AMBIGUOUS_RESYNC_REQUIRED")
        const val RESYNC_RETRY_DELAY_MS = 250L
        const val MAX_RESYNC_RETRY_DELAY_MS = 4_000L
        const val MAX_RESYNC_BACKOFF_EXPONENT = 4
        const val MAX_RECENT_TERMINAL_TURNS = 256

        fun resyncReloadDelayMillis(attempt: Int): Long {
            val exponent = attempt.coerceIn(0, MAX_RESYNC_BACKOFF_EXPONENT)
            return (RESYNC_RETRY_DELAY_MS * (1L shl exponent))
                .coerceAtMost(MAX_RESYNC_RETRY_DELAY_MS)
        }

        fun String.isTerminalTurnStatus(): Boolean =
            this == "completed" || this == "failed" || this == "interrupted"
    }

    private class ConversationResyncSignal : Exception()
    private class GatewaySessionChangedSignal : Exception()

    private data class GatewayUiInputs(
        val threads: List<CodexThread>,
        val connection: GatewayConnectionState,
        val kind: GatewayKind,
        val identity: GatewaySessionIdentity,
    )

    override fun onCleared() {
        conversationJob?.cancel()
        outboxRecoveryJobs.values.forEach(Job::cancel)
        outboxRecoveryJobs.clear()
        cancelAttachmentWork()
        (gateway as? CloseableCodexGateway)?.close()
        super.onCleared()
    }
}

private fun GatewayKind.isAssistantNetwork(): Boolean =
    this == GatewayKind.LOCAL || this == GatewayKind.RELAY || this == GatewayKind.REMOTE
