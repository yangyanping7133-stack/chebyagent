package com.cheby.codex.mobile.gateway

import android.util.Log
import com.cheby.codex.mobile.data.RelayDeviceSession
import com.cheby.codex.mobile.data.RelayResponseReceipt
import com.cheby.codex.mobile.data.SecureRelayConfigStore
import com.cheby.codex.mobile.model.ActionItem
import com.cheby.codex.mobile.model.ActionKind
import com.cheby.codex.mobile.model.ActionsBlock
import com.cheby.codex.mobile.model.CodexThread
import com.cheby.codex.mobile.model.ConversationEvent
import com.cheby.codex.mobile.model.ConversationSnapshot
import com.cheby.codex.mobile.model.GatewayConnectionState
import com.cheby.codex.mobile.model.MessageRole
import com.cheby.codex.mobile.model.NoticeBlock
import com.cheby.codex.mobile.model.PatchOperation
import com.cheby.codex.mobile.model.RichMessage
import com.cheby.codex.mobile.model.RichMessageState
import com.cheby.codex.mobile.model.ThreadKey
import com.cheby.codex.mobile.model.ThreadRunState
import com.cheby.codex.mobile.model.Tone
import java.util.Base64
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference
import javax.net.ssl.SSLSocketFactory
import kotlin.random.Random
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.filter
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put

internal class RelayCodexGateway(
    private val sessionStore: SecureRelayConfigStore,
    session: RelayDeviceSession,
    private val deviceProofKey: DeviceProofKeyHandle,
    private val credentialClient: RelayCredentialClient = RelayCredentialClient(),
    private val codec: ContractV1Codec = ContractV1Codec(),
    private val relayCodec: RelayV1Codec = RelayV1Codec(),
    private val sslSocketFactory: SSLSocketFactory,
    private val scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.IO),
    private val nowEpochSeconds: () -> Long = { System.currentTimeMillis() / 1_000 },
    private val onSessionRevoked: (String) -> Unit = {},
    private val requestTimeoutMillis: Long = REQUEST_TIMEOUT_MS,
    private val acceptTimeoutMillis: Long = ACCEPT_TIMEOUT_MS,
    private val heartbeatIntervalMillis: Long = HEARTBEAT_INTERVAL_MS,
    private val heartbeatTimeoutMillis: Long = HEARTBEAT_TIMEOUT_MS,
    private val originValidator: (String) -> Unit = {},
    private val deliveryCrashTestHook: DeliveryCrashTestHook = NoOpDeliveryCrashTestHook,
) : CodexGateway, CloseableCodexGateway {
    override val sessionScope: String = session.bindingScope
    private val auth = AtomicReference(session)
    private val _kind = MutableStateFlow(GatewayKind.RELAY)
    override val kind: StateFlow<GatewayKind> = _kind.asStateFlow()
    private val _connectionState = MutableStateFlow(GatewayConnectionState.CONNECTING)
    override val connectionState: StateFlow<GatewayConnectionState> = _connectionState.asStateFlow()
    private val _threads = MutableStateFlow<List<CodexThread>>(emptyList())
    override val threads: StateFlow<List<CodexThread>> = _threads.asStateFlow()

    private data class PendingRequest(
        val messageId: String,
        val requestId: String,
        val operation: String,
        val frame: String,
        val response: CompletableDeferred<RelayV1Payload.Response>,
        val deliveryCrashIdentity: DeliveryCrashIdentity?,
        @Volatile var accepted: CompletableDeferred<Unit> = CompletableDeferred(),
        @Volatile var sent: Boolean = false,
    )

    private data class DeliveredEvent(
        val transportSequence: Long,
        val payload: RelayV1Payload.Event,
    )

    private data class ApplicationPongWaiter(
        val socket: RelayBoundedDuplexConnection,
        val nonce: String,
        val completed: CompletableDeferred<Unit>,
    )

    private data class ObservedEventIdentity(
        val streamId: String,
        val sequence: Long,
    )

    private class PendingRequestAbortedException : Exception()

    private data class ApprovalCommand(
        val approvalId: String,
        val threadId: String,
        val decision: String,
        val actionToken: String,
        val expiresAtMillis: Long,
    )

    private data class ApprovalRecord(
        val threadId: String,
        val message: RichMessage,
        val actionRefs: Map<String, String>,
    )

    private val closed = AtomicBoolean(false)
    private val activeSocket = AtomicReference<RelayBoundedDuplexConnection?>()
    private val pending = ConcurrentHashMap<String, PendingRequest>()
    private val awaitingAcceptance = AtomicReference<PendingRequest?>()
    private val pendingApplicationPong = AtomicReference<ApplicationPongWaiter?>()
    private val initialThreadRefresh = CompletableDeferred<Unit>()
    private val stabilizedSocket = AtomicReference<RelayBoundedDuplexConnection?>()
    private val onlineNodeSocket = AtomicReference<RelayBoundedDuplexConnection?>()
    private val outboundChanged = MutableSharedFlow<Unit>(extraBufferCapacity = 1)
    // A snapshot response and the collector that consumes events cannot be installed atomically.
    // Retain a bounded hand-off window so events delivered in that gap are not lost. Incoming
    // device frames are capped at 256 KiB by RelayV1Codec, keeping this replay window bounded.
    // Overflow intentionally drops the oldest event: EventSequenceGate will then force a fresh
    // authoritative snapshot instead of applying a sequence with a gap.
    private val deliveredEvents = MutableSharedFlow<DeliveredEvent>(
        replay = EVENT_HANDOFF_REPLAY,
        onBufferOverflow = BufferOverflow.DROP_OLDEST,
    )
    private val observerCount = AtomicInteger()
    private val approvalLock = Any()
    private val approvalCommands = mutableMapOf<String, ApprovalCommand>()
    private val approvals = mutableMapOf<String, ApprovalRecord>()
    private val approvalsInFlight = mutableSetOf<String>()
    private val abandonedRequestLock = Any()
    private val abandonedRequestByMessage = linkedMapOf<String, String>()
    private val abandonedMessageByRequest = linkedMapOf<String, String>()
    private val subscriptionEpoch = AtomicLong()
    @Volatile private var subscribedEpoch = -1L
    private val refreshMutex = Mutex()
    private val sessionMutationMutex = Mutex()
    private val subscriptionMutex = Mutex()
    private val socketWriteMutex = Mutex()
    private val stabilizationMutex = Mutex()
    private val connectionJob: Job = scope.launch { connectionLoop() }

    init {
        require(
            requestTimeoutMillis > 0 && acceptTimeoutMillis > 0 &&
                heartbeatIntervalMillis > 0 && heartbeatTimeoutMillis > 0,
        )
        requireGateOnlyDeliveryCrashHook(deliveryCrashTestHook)
    }

    override suspend fun createThread(title: String): CodexThread {
        // ONLINE means the transport is usable, but the one-time catalog reconciliation may still
        // be in flight. Let that authoritative scan finish before creating an empty thread that
        // Codex will not expose in thread/list until its first persisted turn.
        initialThreadRefresh.await()
        val result = request(
            "threads.create",
            buildJsonObject { title.trim().takeIf(String::isNotEmpty)?.let { put("title", it.take(120)) } },
        )
        val thread = codec.decodeThread(result.toString())
        // A newly started Codex thread is not catalog-visible until its first turn is persisted.
        // An immediate authoritative list would therefore erase the public mapping that create
        // just returned and make the first read/turn fail with THREAD_NOT_FOUND.
        _threads.update { current -> listOf(thread) + current.filterNot { it.key == thread.key } }
        return thread
    }

    override suspend fun renameThread(threadKey: ThreadKey, title: String) {
        request("threads.patch", buildJsonObject {
            put("threadId", threadKey.value)
            put("title", title.trim().take(120))
        })
        refreshThreads()
    }

    override suspend fun archiveThread(threadKey: ThreadKey) {
        request("threads.patch", buildJsonObject {
            put("threadId", threadKey.value)
            put("archived", true)
        })
        refreshThreads()
    }

    override suspend fun deleteThread(threadKey: ThreadKey) {
        val preview = request(
            "threads.delete.preview",
            buildJsonObject { put("threadId", threadKey.value) },
        )
        val impactToken = preview["impactToken"]?.jsonPrimitive?.contentOrNull
            ?.takeIf { it.length in 32..512 && it.none(Char::isISOControl) }
            ?: throw GatewayTransportException("Delete confirmation is unavailable")
        request("threads.delete.confirm", buildJsonObject {
            put("threadId", threadKey.value)
            put("confirmPermanentDelete", true)
            put("impactToken", impactToken)
        })
        refreshThreads()
    }

    override suspend fun loadConversation(threadKey: ThreadKey): ConversationSnapshot {
        val result = request("threads.read", buildJsonObject {
            put("threadId", threadKey.value)
            put("messageLimit", 100)
        })
        val detail = codec.decodeThreadDetail(result.toString())
        val approvalMessages = reconcileApprovalSnapshot(threadKey, detail.approvals)
        return ConversationSnapshot(
            threadKey,
            detail.streamId,
            detail.cursor,
            (detail.messages + approvalMessages).distinctBy(RichMessage::id),
        )
    }

    override fun observeConversation(
        threadKey: ThreadKey,
        afterStreamId: String,
        afterStreamSeq: Long,
    ): Flow<ConversationEvent> = flow {
        observerCount.incrementAndGet()
        val emittedEvents = linkedMapOf<String, ObservedEventIdentity>()
        // ACK only after emit returns: in a regular Flow that means the downstream reducer has
        // accepted the event. Persisting or acknowledging while an event is merely buffered can
        // advance the server beyond Android state and permanently hide a terminal patch after a
        // collector cancellation. Network ACKs remain asynchronous so a slow node response does
        // not head-of-line block later snapshots and patches.
        val pendingAcknowledgements = Channel<ObservedEventIdentity>(Channel.CONFLATED)
        val acknowledger = scope.launch {
            for (identity in pendingAcknowledgements) {
                retryTransientObservation {
                    acknowledgeGatewayEvent(identity.streamId, identity.sequence)
                }
            }
        }
        try {
            retryTransientObservation {
                ensureSubscription(afterStreamId, afterStreamSeq, subscriptionEpoch.get())
            }
            deliveredEvents.collect { delivery ->
                val event = delivery.payload
                if (event.deviceId != auth.get().principalId) {
                    throw GatewayTransportException("Relay binding changed")
                }
                val directEnvelope = buildJsonObject {
                    put("v", 1)
                    put("eventId", event.eventId)
                    put("streamId", event.streamId)
                    put("seq", event.eventSeq)
                    put("occurredAt", event.occurredAt)
                    put("type", event.eventType)
                    event.threadId?.let { put("threadId", it) }
                    event.turnId?.let { put("turnId", it) }
                    event.itemId?.let { put("itemId", it) }
                    put("payload", event.payload)
                }
                val decoded = codec.decodeEvent(directEnvelope.toString(), threadKey)
                val identity = ObservedEventIdentity(decoded.streamId, decoded.seq)
                val coveredBySnapshot =
                    decoded.streamId == afterStreamId && decoded.seq <= afterStreamSeq
                if (!coveredBySnapshot) {
                    val exactDuplicate = emittedEvents[decoded.event.eventId] == identity
                    if (!exactDuplicate) {
                        emit(projectApproval(decoded))
                        // A regular Flow emit returns only after the downstream reducer accepts
                        // the event. If the reducer detects a gap it throws and this projection
                        // is skipped, leaving the authoritative catalog refresh in control.
                        projectThreadLifecycle(event)
                        emittedEvents[decoded.event.eventId] = identity
                        if (emittedEvents.size > MAX_OBSERVED_EVENT_IDENTITIES) {
                            emittedEvents.remove(emittedEvents.keys.first())
                        }
                    }
                }
                // The authoritative snapshot proves every same-stream event through its cursor.
                // ACK replayed, snapshot-covered events as well so the server can compact them.
                pendingAcknowledgements.trySend(identity)
            }
        } finally {
            pendingAcknowledgements.close()
            acknowledger.cancelAndJoin()
            observerCount.decrementAndGet()
        }
    }

    override suspend fun loadCapabilities(
        expectedSessionScope: String,
        expectedSessionGeneration: Long,
    ): GatewayCapabilities {
        requireSession(expectedSessionScope, expectedSessionGeneration)
        return codec.decodeCapabilities(request("capabilities.get", buildJsonObject {}).toString())
    }

    override suspend fun uploadTurnImage(
        threadKey: ThreadKey,
        clientMessageId: String,
        clientAssetId: String,
        bytes: ByteArray,
        expectedSessionScope: String,
        expectedSessionGeneration: Long,
    ): UploadedImageAsset {
        requireSession(expectedSessionScope, expectedSessionGeneration)
        val mediaType = when {
            bytes.startsWith(PNG_SIGNATURE) -> "image/png"
            bytes.size >= 3 && bytes[0] == 0xff.toByte() && bytes[1] == 0xd8.toByte() && bytes[2] == 0xff.toByte() -> "image/jpeg"
            else -> throw GatewayTransportException("Image input is invalid")
        }
        val stable = relayBusinessIdentity(threadKey.value, clientMessageId, clientAssetId)
        val result = request(
            operation = "assets.upload",
            params = buildJsonObject {
                put("threadId", threadKey.value)
                put("clientMessageId", clientMessageId)
                put("clientAssetId", clientAssetId)
                put("mediaType", mediaType)
                put("bodyBase64", Base64.getEncoder().encodeToString(bytes))
            },
            stableIdentity = stable,
            timeoutMillis = ASSET_TIMEOUT_MS,
        )
        return codec.decodeImageUpload(result.toString())
    }

    override suspend fun sendTurnInput(
        threadKey: ThreadKey,
        input: List<TurnInput>,
        idempotencyKey: String,
        expectedSessionScope: String,
        expectedSessionGeneration: Long,
    ): TurnSubmission {
        requireSession(expectedSessionScope, expectedSessionGeneration)
        val parts = buildJsonArray {
            input.forEach { part ->
                add(when (part) {
                    is TurnInput.Text -> buildJsonObject {
                        put("type", "text")
                        put("text", part.value)
                    }
                    is TurnInput.Image -> buildJsonObject {
                        put("type", "image")
                        put("assetRef", part.assetRef)
                    }
                })
            }
        }
        val result = request(
            operation = "turns.start",
            params = buildJsonObject {
                put("threadId", threadKey.value)
                put("clientMessageId", idempotencyKey)
                put("input", parts)
            },
            stableIdentity = relayBusinessIdentity(threadKey.value, idempotencyKey),
            timeoutMillis = TURN_TIMEOUT_MS,
            deliveryCrashIdentity = DeliveryCrashIdentity(
                sessionScope = expectedSessionScope,
                sessionGeneration = expectedSessionGeneration,
                threadKey = threadKey,
                clientMessageId = idempotencyKey,
            ),
        )
        return codec.decodeTurnSubmission(result.toString()).also { submission ->
            if (
                submission.threadKey != threadKey ||
                submission.clientMessageId != idempotencyKey
            ) {
                throw GatewayTransportException("Turn submission identity changed")
            }
        }
    }

    override suspend fun sendTurn(
        threadKey: ThreadKey,
        text: String,
        idempotencyKey: String,
        expectedSessionScope: String,
    ): TurnSubmission = sendTurnInput(
        threadKey,
        listOf(TurnInput.Text(text)),
        idempotencyKey,
        expectedSessionScope,
        0,
    )

    override suspend fun performAction(actionRef: String, expectedSessionScope: String) {
        requireSession(expectedSessionScope, 0)
        val command = synchronized(approvalLock) {
            val candidate = approvalCommands[actionRef]
                ?: throw GatewayTransportException("Action is unavailable")
            if (candidate.expiresAtMillis <= System.currentTimeMillis()) {
                removeApprovalLocked(candidate.approvalId)
                throw GatewayTransportException("Action is unavailable")
            }
            if (!approvalsInFlight.add(candidate.approvalId)) {
                throw GatewayTransportException("Action is unavailable")
            }
            candidate
        }
        try {
            request(
                "approvals.decide",
                buildJsonObject {
                    put("approvalId", command.approvalId)
                    put("decision", command.decision)
                    put("actionToken", command.actionToken)
                },
                stableIdentity = "approval:${command.approvalId}:${command.decision}",
            )
            synchronized(approvalLock) { removeApprovalLocked(command.approvalId) }
        } finally {
            synchronized(approvalLock) { approvalsInFlight.remove(command.approvalId) }
        }
    }

    override fun close() {
        if (!closed.compareAndSet(false, true)) return
        activeSocket.getAndSet(null)?.cancel()
        connectionJob.cancel()
        val failure = GatewayTransportException("Relay session closed")
        pending.values.forEach { it.response.completeExceptionally(failure) }
        pending.clear()
        synchronized(abandonedRequestLock) {
            abandonedRequestByMessage.clear()
            abandonedMessageByRequest.clear()
        }
        initialThreadRefresh.complete(Unit)
        scope.cancel()
    }

    private suspend fun refreshThreads(waitForOnline: Boolean = true) {
        val result = request(
            "threads.list",
            buildJsonObject { put("archived", false) },
            waitForOnline = waitForOnline,
        )
        _threads.value = codec.decodeThreadList(result.toString())
    }

    private suspend fun ensureSubscription(afterStreamId: String, afterSeq: Long, epoch: Long) =
        subscriptionMutex.withLock {
            if (subscribedEpoch == epoch) return@withLock
            if (afterSeq < 0) throw GatewayTransportException("Event cursor is invalid")
            val current = auth.get()
            val resumeSeq = if (
                current.gatewayStreamId == "pending" ||
                current.gatewayStreamId == afterStreamId
            ) maxOf(current.gatewayAckSeq, afterSeq) else 0
            val response = request(
                "events.subscribe",
                buildJsonObject { put("afterSeq", resumeSeq) },
                stableIdentity = "subscription:${System.nanoTime()}",
            )
            val streamId = response["streamId"]?.jsonPrimitive?.contentOrNull
                ?.takeIf(String::isNotBlank) ?: throw GatewayTransportException("Event subscription failed")
            val syncRequired = response["syncRequired"]?.jsonPrimitive?.contentOrNull?.toBooleanStrictOrNull()
                ?: throw GatewayTransportException("Event subscription failed")
            if (!syncRequired) {
                persistGatewayCursor(streamId, resumeSeq)
            }
            // The connector also publishes an authoritative sync.required event when needed.
            subscribedEpoch = epoch
        }

    private fun reconcileApprovalSnapshot(
        threadKey: ThreadKey,
        pending: List<ApprovalMutation.Requested>,
    ): List<RichMessage> = synchronized(approvalLock) {
        val stale = approvals.filterValues { it.threadId == threadKey.value }.keys.toList()
        stale.forEach(::removeApprovalLocked)
        pending.map { approval ->
            createApprovalRecord(approval).also { record -> approvals[approval.approvalId] = record }.message
        }
    }

    private fun projectApproval(decoded: DecodedEvent): ConversationEvent {
        val mutation = decoded.approval ?: return decoded.event
        return synchronized(approvalLock) {
            when (mutation) {
                is ApprovalMutation.Requested -> {
                    val record = approvals[mutation.approvalId] ?: createApprovalRecord(mutation).also {
                        approvals[mutation.approvalId] = it
                    }
                    ConversationEvent.MessageAdded(
                        decoded.event.eventId,
                        decoded.event.streamId,
                        decoded.event.streamSeq,
                        record.message,
                    )
                }
                is ApprovalMutation.Terminal -> {
                    val record = approvals[mutation.approvalId]
                    removeApprovalLocked(mutation.approvalId)
                    if (record == null) decoded.event else ConversationEvent.MessagePatched(
                        eventId = decoded.event.eventId,
                        streamId = decoded.event.streamId,
                        streamSeq = decoded.event.streamSeq,
                        messageId = record.message.id,
                        baseRevision = record.message.revision,
                        revision = record.message.revision + 1,
                        operations = buildList {
                            add(PatchOperation.MergeBlock(
                                APPROVAL_NOTICE_BLOCK,
                                mapOf(
                                    "body" to when (mutation.state) {
                                        "approved" -> "已批准"
                                        "rejected" -> "已拒绝"
                                        else -> "已过期"
                                    },
                                    "tone" to Tone.NEUTRAL,
                                ),
                            ))
                            record.actionRefs.keys.forEach { decision ->
                                add(PatchOperation.ResolveAction(APPROVAL_ACTIONS_BLOCK, decision))
                            }
                            add(PatchOperation.SetMessageState(RichMessageState.COMPLETED))
                        },
                    )
                }
            }
        }
    }

    private fun createApprovalRecord(request: ApprovalMutation.Requested): ApprovalRecord {
        val actionable = request.actionToken != null && request.expiresAtMillis > System.currentTimeMillis()
        val refs = request.decisions.associateWith { UUID.randomUUID().toString() }
        val actions = request.decisions.map { decision ->
            val actionRef = refs.getValue(decision)
            if (actionable) {
                approvalCommands[actionRef] = ApprovalCommand(
                    request.approvalId,
                    request.threadId,
                    decision,
                    requireNotNull(request.actionToken),
                    request.expiresAtMillis,
                )
            }
            ActionItem(
                id = decision,
                label = if (decision == "approve") "批准" else "拒绝",
                actionRef = actionRef,
                kind = if (decision == "reject") ActionKind.DESTRUCTIVE else ActionKind.PRIMARY,
                enabled = actionable,
            )
        }
        val notice = NoticeBlock(
            APPROVAL_NOTICE_BLOCK,
            request.summary,
            request.reason,
            Tone.WARNING,
        )
        val actionBlock = ActionsBlock(APPROVAL_ACTIONS_BLOCK, actions)
        val message = RichMessage(
            id = "approval:${request.approvalId}",
            role = MessageRole.ASSISTANT,
            state = RichMessageState.WAITING_INPUT,
            revision = 0,
            rootBlockIds = listOf(notice.id, actionBlock.id),
            blocks = mapOf(notice.id to notice, actionBlock.id to actionBlock),
            createdAtMillis = System.currentTimeMillis(),
        )
        return ApprovalRecord(request.threadId, message, refs)
    }

    private fun removeApprovalLocked(approvalId: String) {
        approvals.remove(approvalId)?.actionRefs?.values?.forEach(approvalCommands::remove)
        approvalCommands.entries.removeAll { it.value.approvalId == approvalId }
        approvalsInFlight.remove(approvalId)
    }

    private suspend fun acknowledgeGatewayEvent(streamId: String, seq: Long) {
        // The server ACK is cumulative and may discard replay history. Persist the exact cursor
        // first so a process death can never leave the remote cursor ahead of durable Android
        // state. A failed network ACK is safe: reconnect repeats it from this durable cursor.
        persistGatewayCursor(streamId, seq)
        request("events.ack", buildJsonObject {
            put("streamId", streamId)
            put("seq", seq)
        }, stableIdentity = "event-ack:$streamId:$seq")
    }

    private suspend fun <T> retryTransientObservation(block: suspend () -> T): T {
        var attempt = 0
        while (true) {
            try {
                return block()
            } catch (request: GatewayRequestException) {
                if (!request.retryable) throw request
                delay(observationRetryDelay(attempt++, request.retryAfterMillis))
            } catch (transport: GatewayTransportException) {
                if (
                    closed.get() ||
                    _kind.value != GatewayKind.RELAY ||
                    sessionStore.loadSession()?.bindingScope != sessionScope
                ) throw transport
                delay(observationRetryDelay(attempt++, null))
            }
        }
    }

    private fun observationRetryDelay(attempt: Int, serverHintMillis: Long?): Long {
        val exponent = attempt.coerceIn(0, 4)
        val local = EVENT_RETRY_DELAY_MS * (1L shl exponent)
        return maxOf(local, serverHintMillis ?: 0L).coerceAtMost(MAX_EVENT_RETRY_DELAY_MS)
    }

    private suspend fun request(
        operation: String,
        params: JsonObject,
        stableIdentity: String? = null,
        timeoutMillis: Long = requestTimeoutMillis,
        waitForOnline: Boolean = true,
        deliveryCrashIdentity: DeliveryCrashIdentity? = null,
    ): JsonObject {
        if (waitForOnline) awaitUsableConnection()
        val current = auth.get()
        val baseStable = stableIdentity?.let { "$sessionScope|$operation|$it" }
        var transportStable = baseStable
        var requestId = RelayIds.request(transportStable)
        var messageId = RelayIds.message(transportStable)
        var retryHops = 0
        while (true) {
            val receipt = sessionStore.loadResponseReceipt(sessionScope, requestId) ?: break
            if (receipt.errorCode == null || !receipt.retryable || baseStable == null) {
                return consumeReceipt(receipt, operation)
            }
            if (++retryHops > MAX_RESPONSE_RETRY_CHAIN) {
                throw GatewayTransportException("Relay retry history is exhausted")
            }
            // A known retryable response ended that transport attempt. Use a new transport
            // identity while retaining the operation's business idempotency fields.
            transportStable = "$baseStable|retry:${receipt.deliverySeq}"
            requestId = RelayIds.request(transportStable)
            messageId = RelayIds.message(transportStable)
        }
        val pendingRequest = PendingRequest(
            messageId = messageId,
            requestId = requestId,
            operation = operation,
            frame = relayCodec.encodeCommand(messageId, requestId, current.principalId, operation, params),
            response = CompletableDeferred(),
            deliveryCrashIdentity = deliveryCrashIdentity,
        )
        val existing = pending.putIfAbsent(requestId, pendingRequest)
        val target = existing ?: pendingRequest
        outboundChanged.tryEmit(Unit)
        val response = try {
            withTimeout(timeoutMillis) { target.response.await() }
        } catch (_: TimeoutCancellationException) {
            throw GatewayTransportException("Relay request timed out")
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (request: GatewayRequestException) {
            throw request
        } finally {
            if (!target.response.isCompleted && target.sent) {
                rememberAbandonedRequest(target)
            }
            if (pending.remove(requestId, target)) {
                target.accepted.completeExceptionally(PendingRequestAbortedException())
                if (!target.response.isCompleted) target.response.cancel()
                awaitingAcceptance.compareAndSet(target, null)
                outboundChanged.tryEmit(Unit)
            }
        }
        if (response.deviceId != auth.get().principalId || response.operation != operation) {
            throw GatewayTransportException("Relay binding changed")
        }
        response.errorCode?.let { code ->
            throw GatewayRequestException(code, response.retryable, response.retryAfterMillis)
        }
        return response.result ?: throw GatewayTransportException("Relay response is invalid")
    }

    private suspend fun connectionLoop() {
        val backoff = BoundedReconnectBackoff()
        recordConnectionDiagnostic("gateway_started")
        while (scope.isActive && !closed.get()) {
            var socket: RelayBoundedDuplexConnection? = null
            var onlineSinceNanos: Long? = null
            try {
                recordConnectionDiagnostic("attempt")
                val current = freshSession()
                originValidator(current.relayOrigin)
                val endpoint = RelayEndpoint.parse(
                    current.relayOrigin.replaceFirst("https://", "wss://") + RelayEndpoint.DEVICE_SOCKET_PATH,
                )
                socket = RelayBoundedDuplexConnection(sslSocketFactory)
                socket.connect(endpoint, current.accessToken, deviceProofKey)
                val readyEvent = socket.read() as? BoundedSocketEvent.Text
                    ?: throw GatewayTransportException("Relay ready frame is missing")
                val ready = relayCodec.decode(readyEvent.value) as? RelayV1Frame.Ready
                    ?: throw GatewayTransportException("Relay ready frame is invalid")
                validateReady(ready, current)
                activeSocket.set(socket)
                val currentSubscriptionEpoch = subscriptionEpoch.incrementAndGet()
                _connectionState.value = if (ready.nodeOnline) {
                    onlineNodeSocket.set(socket)
                    GatewayConnectionState.CONNECTING
                } else GatewayConnectionState.OFFLINE
                recordConnectionDiagnostic(if (ready.nodeOnline) "ready_stabilizing" else "ready_node_offline")
                onlineSinceNanos = System.nanoTime()
                if (current.relayAckCursor > ready.ackCursor) {
                    sendSocket(relayCodec.encodeAck(current.relayAckCursor), socket)
                }
                pending.values.forEach { it.accepted = CompletableDeferred() }
                coroutineScope {
                    val writer = launch { writerLoop(socket) }
                    val heartbeat = launch { applicationHeartbeatLoop(socket) }
                    val reader = launch {
                        while (scope.isActive && !closed.get()) {
                            when (val event = socket.read()) {
                                is BoundedSocketEvent.Text -> handleFrame(relayCodec.decode(event.value), socket)
                                is BoundedSocketEvent.Closed -> throw socketCloseFailure(event.code)
                            }
                        }
                    }
                    try {
                        if (ready.nodeOnline) {
                            stabilizeConnection(socket)
                            if (observerCount.get() > 0) scope.launch {
                                ensureSubscription(
                                    current.gatewayStreamId,
                                    current.gatewayAckSeq,
                                    currentSubscriptionEpoch,
                                )
                            }
                        }
                        reader.join()
                    } finally {
                        reader.cancelAndJoin()
                        writer.cancelAndJoin()
                        heartbeat.cancelAndJoin()
                    }
                }
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (handshake: WebSocketHandshakeException) {
                recordConnectionDiagnostic("handshake_${handshake.statusCode}")
                if (handshake.statusCode == 401 || handshake.statusCode == 403) {
                    recoverAuthentication()
                } else {
                    _connectionState.value = GatewayConnectionState.OFFLINE
                }
            } catch (_: DeviceIdentityUnavailableException) {
                recordConnectionDiagnostic("device_identity_unavailable")
                markUnconfigured()
            } catch (request: GatewayRequestException) {
                recordConnectionDiagnostic("request_${request.code}")
                when (request.code) {
                    "AUTH_REQUIRED" -> recoverAuthentication()
                    "REFRESH_DENIED" -> markUnconfigured()
                    else -> _connectionState.value = GatewayConnectionState.OFFLINE
                }
            } catch (error: Exception) {
                recordConnectionDiagnostic("transport_${error.javaClass.simpleName}")
                _connectionState.value = GatewayConnectionState.OFFLINE
            } finally {
                onlineSinceNanos?.let { started ->
                    val stableNanos = heartbeatIntervalMillis * 1_000_000L
                    if (System.nanoTime() - started >= stableNanos) backoff.reset()
                }
                activeSocket.compareAndSet(socket, null)
                stabilizedSocket.compareAndSet(socket, null)
                onlineNodeSocket.compareAndSet(socket, null)
                socket?.cancel()
                awaitingAcceptance.getAndSet(null)?.accepted?.completeExceptionally(
                    GatewayTransportException("Relay connection changed"),
                )
                pendingApplicationPong.getAndSet(null)?.completed?.completeExceptionally(
                    GatewayTransportException("Relay connection changed"),
                )
            }
            if (!closed.get() && _kind.value == GatewayKind.RELAY) {
                delay(backoff.nextDelay(null))
            }
        }
    }

    private fun relayBusinessIdentity(vararg components: String): String =
        components.joinToString(separator = "") { component ->
            "${component.length}:$component"
        }

    private suspend fun writerLoop(socket: RelayBoundedDuplexConnection) {
        while (scope.isActive && activeSocket.get() === socket) {
            val next = pending.values.firstOrNull { !it.response.isCompleted && !it.accepted.isCompleted }
            if (next == null) {
                outboundChanged.first()
                continue
            }
            if (pending[next.requestId] !== next) continue
            awaitingAcceptance.set(next)
            try {
                // Mark before the write so cancellation cannot create an unrecognized late
                // accepted frame in the narrow send/finally race. A false-positive tombstone is
                // bounded and removed by the matching response.
                next.sent = true
                sendSocket(next.frame, socket)
                next.deliveryCrashIdentity?.let { identity ->
                    deliveryCrashTestHook.reachedForGate(
                        DeliveryCrashCheckpoint.NETWORK_SENT_UNCONFIRMED,
                        identity,
                    )
                }
                withTimeout(acceptTimeoutMillis) { next.accepted.await() }
            } catch (_: PendingRequestAbortedException) {
                continue
            } catch (_: TimeoutCancellationException) {
                socket.cancel()
                throw GatewayTransportException("Relay acceptance timed out")
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (error: Exception) {
                socket.cancel()
                throw error
            } finally {
                awaitingAcceptance.compareAndSet(next, null)
            }
        }
    }

    private suspend fun applicationHeartbeatLoop(socket: RelayBoundedDuplexConnection) {
        while (scope.isActive && activeSocket.get() === socket) {
            delay(heartbeatIntervalMillis)
            if (activeSocket.get() !== socket) return
            performApplicationHeartbeat(socket)
        }
    }

    private suspend fun performApplicationHeartbeat(socket: RelayBoundedDuplexConnection) {
        val waiter = ApplicationPongWaiter(socket, RelayIds.nonce(), CompletableDeferred())
        if (!pendingApplicationPong.compareAndSet(null, waiter)) {
            socket.cancel()
            throw GatewayTransportException("Relay heartbeat state changed")
        }
        try {
            sendSocket(relayCodec.encodePing(waiter.nonce), socket)
            withTimeout(heartbeatTimeoutMillis) { waiter.completed.await() }
        } catch (_: TimeoutCancellationException) {
            socket.cancel()
            throw GatewayTransportException("Relay heartbeat timed out")
        } finally {
            pendingApplicationPong.compareAndSet(waiter, null)
        }
    }

    private suspend fun stabilizeConnection(socket: RelayBoundedDuplexConnection) =
        stabilizationMutex.withLock {
            if (
                activeSocket.get() !== socket ||
                onlineNodeSocket.get() !== socket ||
                stabilizedSocket.get() === socket
            ) return@withLock
            _connectionState.value = GatewayConnectionState.CONNECTING
            performApplicationHeartbeat(socket)
            retryTransientObservation { refreshThreads(waitForOnline = false) }
            if (activeSocket.get() !== socket || onlineNodeSocket.get() !== socket) {
                throw GatewayTransportException("Relay connection changed")
            }
            stabilizedSocket.set(socket)
            initialThreadRefresh.complete(Unit)
            _connectionState.value = GatewayConnectionState.ONLINE
            recordConnectionDiagnostic("ready_online")
        }

    private suspend fun handleFrame(frame: RelayV1Frame, socket: RelayBoundedDuplexConnection) {
        when (frame) {
            is RelayV1Frame.Accepted -> {
                val target = pending.values.firstOrNull { it.messageId == frame.messageId }
                if (target == null) {
                    if (isAbandonedMessage(frame.messageId)) return
                    throw GatewayTransportException("Relay accepted an unknown request")
                }
                if (awaitingAcceptance.get() !== target) {
                    throw GatewayTransportException("Relay acceptance order changed")
                }
                target.deliveryCrashIdentity?.let { identity ->
                    deliveryCrashTestHook.reachedForGate(
                        DeliveryCrashCheckpoint.TRANSPORT_ACCEPTED,
                        identity,
                    )
                }
                target.accepted.complete(Unit)
            }
            is RelayV1Frame.Delivery -> handleDelivery(frame, socket)
            is RelayV1Frame.Acknowledged -> Unit
            is RelayV1Frame.NodeStatus -> {
                if (!frame.online) {
                    onlineNodeSocket.compareAndSet(socket, null)
                    stabilizedSocket.compareAndSet(socket, null)
                    _connectionState.value = GatewayConnectionState.OFFLINE
                } else {
                    onlineNodeSocket.set(socket)
                    if (stabilizedSocket.get() === socket) {
                        _connectionState.value = GatewayConnectionState.ONLINE
                    } else {
                        scope.launch {
                            try {
                                stabilizeConnection(socket)
                                if (observerCount.get() > 0) {
                                    val epoch = subscriptionEpoch.incrementAndGet()
                                    val current = auth.get()
                                    ensureSubscription(
                                        current.gatewayStreamId,
                                        current.gatewayAckSeq,
                                        epoch,
                                    )
                                }
                            } catch (_: Exception) {
                                socket.cancel()
                            }
                        }
                    }
                }
            }
            is RelayV1Frame.Ping -> sendSocket(relayCodec.encodePong(frame.nonce), socket)
            is RelayV1Frame.Pong -> {
                val waiter = pendingApplicationPong.get()
                if (waiter?.socket === socket && waiter.nonce == frame.nonce) {
                    waiter.completed.complete(Unit)
                }
            }
            is RelayV1Frame.Error -> {
                val target = awaitingAcceptance.getAndSet(null)
                    ?: throw GatewayTransportException("Uncorrelated Relay error")
                target.accepted.completeExceptionally(
                    GatewayRequestException(frame.code, frame.retryable),
                )
                target.response.completeExceptionally(
                    GatewayRequestException(frame.code, frame.retryable),
                )
                pending.remove(target.requestId, target)
            }
            is RelayV1Frame.Ready -> throw GatewayTransportException("Duplicate Relay ready frame")
        }
    }

    private suspend fun handleDelivery(
        frame: RelayV1Frame.Delivery,
        socket: RelayBoundedDuplexConnection,
    ) {
        val current = auth.get()
        if (frame.deliverySeq <= current.relayAckCursor) {
            sendSocket(relayCodec.encodeAck(current.relayAckCursor), socket)
            return
        }
        if (current.relayAckCursor == Long.MAX_VALUE || frame.deliverySeq != current.relayAckCursor + 1) {
            throw GatewayTransportException("Relay delivery sequence changed")
        }
        if (frame.payload.deviceId != current.principalId) {
            throw GatewayTransportException("Relay binding changed")
        }
        when (val payload = frame.payload) {
            is RelayV1Payload.Response -> {
                val receipt = RelayResponseReceipt(
                    requestId = payload.requestId,
                    deviceId = payload.deviceId,
                    operation = payload.operation,
                    resultJson = payload.result?.toString(),
                    errorCode = payload.errorCode,
                    retryable = payload.retryable,
                    retryAfterMillis = payload.retryAfterMillis,
                    deliverySeq = frame.deliverySeq,
                )
                if (!sessionStore.recordResponseReceipt(sessionScope, receipt)) {
                    throw GatewayTransportException("Relay response persistence failed")
                }
                forgetAbandonedRequest(payload.requestId)
                pending[payload.requestId]?.response?.complete(payload)
            }
            is RelayV1Payload.Event -> {
                // Relay already proves this event belongs to an accepted subscription for this
                // bound device. Older subscription ids may legitimately remain in its replay log.
                deliveredEvents.emit(DeliveredEvent(frame.deliverySeq, payload))
            }
        }
        persistAndAckRelay(frame.deliverySeq, socket)
    }

    /**
     * Keep the drawer's catalog projection aligned with structured lifecycle events. Catalog
     * refreshes are intentionally not performed from the reader loop because a request/response
     * round trip there would deadlock the same socket. A terminal event is allowed to change a
     * row only when it names that row's current turn, so a delayed or duplicate terminal cannot
     * make a newer turn appear idle.
     */
    private fun projectThreadLifecycle(event: RelayV1Payload.Event) {
        val threadId = event.threadId ?: return
        val turnId = event.turnId ?: return
        val nextState = when (event.eventType) {
            "turn.started" -> ThreadRunState.RUNNING
            "turn.completed", "turn.interrupted" -> ThreadRunState.IDLE
            "turn.failed" -> ThreadRunState.FAILED
            else -> return
        }
        _threads.update { current ->
            var changed = false
            val projected = current.map { thread ->
                if (thread.key.value != threadId) return@map thread
                if (nextState != ThreadRunState.RUNNING && thread.lastTurnId != turnId) {
                    return@map thread
                }
                val replacement = thread.copy(
                    state = nextState,
                    lastTurnId = turnId,
                )
                changed = changed || replacement != thread
                replacement
            }
            if (changed) projected else current
        }
    }

    private suspend fun persistAndAckRelay(sequence: Long, socket: RelayBoundedDuplexConnection) {
        sessionMutationMutex.withLock {
            if (!sessionStore.updateRelayCursor(sessionScope, sequence)) {
                throw GatewayTransportException("Relay cursor persistence failed")
            }
            auth.set(loadPersistedSession())
        }
        sendSocket(relayCodec.encodeAck(sequence), socket)
    }

    private suspend fun persistGatewayCursor(streamId: String, sequence: Long) {
        sessionMutationMutex.withLock {
            if (!sessionStore.updateGatewayCursor(sessionScope, streamId, sequence)) {
                throw GatewayTransportException("Event cursor persistence failed")
            }
            auth.set(loadPersistedSession())
        }
    }

    private fun consumeReceipt(receipt: RelayResponseReceipt, operation: String): JsonObject {
        if (receipt.deviceId != auth.get().principalId || receipt.operation != operation) {
            throw GatewayTransportException("Relay binding changed")
        }
        receipt.errorCode?.let { code ->
            throw GatewayRequestException(code, receipt.retryable, receipt.retryAfterMillis)
        }
        return try {
            relayCodec.decodeResult(receipt.resultJson ?: throw IllegalArgumentException()).also {
                if (it.toString().toByteArray(Charsets.UTF_8).size > MAX_DURABLE_RESPONSE_BYTES) {
                    throw IllegalArgumentException()
                }
            }
        } catch (_: Exception) {
            throw GatewayTransportException("Relay response is invalid")
        }
    }

    private suspend fun sendSocket(value: String, socket: RelayBoundedDuplexConnection) {
        socketWriteMutex.withLock {
            if (activeSocket.get() !== socket && activeSocket.get() != null) {
                throw GatewayTransportException("Relay connection changed")
            }
            if (!socket.sendText(value)) throw GatewayTransportException("Relay send failed")
        }
    }

    private suspend fun awaitUsableConnection() {
        if (_kind.value != GatewayKind.RELAY || closed.get()) {
            throw GatewayTransportException("Assistant is not bound")
        }
        withTimeout(CONNECTION_WAIT_MS) {
            connectionState.filter { it == GatewayConnectionState.ONLINE }.first()
        }
    }

    private suspend fun freshSession(): RelayDeviceSession = refreshMutex.withLock {
        val current = auth.get()
        if (current.accessExpiresAt > nowEpochSeconds() + REFRESH_SKEW_SECONDS) return current
        credentialClient.refresh(current, deviceProofKey, sessionStore)
        installPersistedSession()
    }

    private suspend fun forceRefresh() = refreshMutex.withLock {
        credentialClient.refresh(auth.get(), deviceProofKey, sessionStore)
        installPersistedSession()
    }

    private suspend fun installPersistedSession(): RelayDeviceSession = sessionMutationMutex.withLock {
        loadPersistedSession().also(auth::set)
    }

    private fun loadPersistedSession(): RelayDeviceSession = sessionStore.loadSession()
        ?.takeIf { it.bindingScope == sessionScope }
        ?: throw GatewayTransportException("Relay session changed")

    private suspend fun recoverAuthentication() {
        _connectionState.value = GatewayConnectionState.OFFLINE
        try {
            forceRefresh()
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (denied: GatewayRequestException) {
            if (denied.code == "REFRESH_DENIED") markUnconfigured()
        } catch (_: DeviceIdentityUnavailableException) {
            markUnconfigured()
        } catch (_: Exception) {
            // Network, Relay availability and local persistence failures are recoverable. The
            // binding remains intact so the next reconnect can retry without user intervention.
        }
    }

    private fun validateReady(ready: RelayV1Frame.Ready, current: RelayDeviceSession) {
        if (
            ready.assistantId != current.assistantId ||
            ready.principalId != current.principalId ||
            ready.ackCursor > current.relayAckCursor ||
            ready.nextDeliverySeq <= ready.ackCursor
        ) {
            throw GatewayTransportException("Relay binding changed")
        }
    }

    private fun requireSession(expectedScope: String, expectedGeneration: Long) {
        if (
            expectedScope != sessionScope ||
            expectedGeneration != 0L ||
            sessionStore.loadSession()?.bindingScope != sessionScope ||
            _kind.value != GatewayKind.RELAY
        ) throw GatewayTransportException("Relay session changed")
    }

    private fun markUnconfigured() {
        if (!sessionStore.clearSessionIfScope(sessionScope)) return
        runCatching { onSessionRevoked(sessionScope) }
        _kind.value = GatewayKind.UNCONFIGURED
        _connectionState.value = GatewayConnectionState.OFFLINE
        activeSocket.getAndSet(null)?.cancel()
    }

    private fun socketCloseFailure(code: Int): GatewayTransportException = when (code) {
        4401, 4403 -> GatewayRequestException("AUTH_REQUIRED", false)
        4408 -> GatewayRequestException("RATE_LIMITED", true)
        else -> GatewayTransportException("Relay connection closed")
    }

    private fun recordConnectionDiagnostic(code: String) {
        // Deliberately omit endpoints, identifiers, exception messages and credentials. The
        // bounded category is enough to distinguish local setup, TLS/HTTP and Relay readiness
        // failures on non-debuggable field builds.
        runCatching { Log.w(CONNECTION_DIAGNOSTIC_TAG, code.take(80)) }
    }

    private fun rememberAbandonedRequest(request: PendingRequest) {
        synchronized(abandonedRequestLock) {
            abandonedRequestByMessage[request.messageId] = request.requestId
            abandonedMessageByRequest[request.requestId] = request.messageId
            while (abandonedRequestByMessage.size > MAX_ABANDONED_REQUESTS) {
                val messageId = abandonedRequestByMessage.keys.first()
                val requestId = abandonedRequestByMessage.remove(messageId)
                if (requestId != null) abandonedMessageByRequest.remove(requestId)
            }
        }
    }

    private fun isAbandonedMessage(messageId: String): Boolean =
        synchronized(abandonedRequestLock) { messageId in abandonedRequestByMessage }

    private fun forgetAbandonedRequest(requestId: String) {
        synchronized(abandonedRequestLock) {
            val messageId = abandonedMessageByRequest.remove(requestId) ?: return
            abandonedRequestByMessage.remove(messageId)
        }
    }

    private fun ByteArray.startsWith(prefix: ByteArray): Boolean =
        size >= prefix.size && prefix.indices.all { this[it] == prefix[it] }

    private companion object {
        private const val REQUEST_TIMEOUT_MS = 45_000L
        private const val ASSET_TIMEOUT_MS = 120_000L
        private const val TURN_TIMEOUT_MS = 300_000L
        private const val ACCEPT_TIMEOUT_MS = 20_000L
        private const val CONNECTION_WAIT_MS = 30_000L
        private const val HEARTBEAT_INTERVAL_MS = 20_000L
        private const val CONNECTION_DIAGNOSTIC_TAG = "ChebyRelay"
        private const val HEARTBEAT_TIMEOUT_MS = 10_000L
        private const val EVENT_RETRY_DELAY_MS = 250L
        private const val MAX_EVENT_RETRY_DELAY_MS = 4_000L
        private const val REFRESH_SKEW_SECONDS = 60L
        private const val MAX_DURABLE_RESPONSE_BYTES = 4 * 1024 * 1024
        private const val MAX_RESPONSE_RETRY_CHAIN = 64
        private const val MAX_OBSERVED_EVENT_IDENTITIES = 256
        private const val EVENT_HANDOFF_REPLAY = 64
        private const val MAX_ABANDONED_REQUESTS = 512
        private const val APPROVAL_NOTICE_BLOCK = "approval-notice"
        private const val APPROVAL_ACTIONS_BLOCK = "approval-actions"
        private val PNG_SIGNATURE = byteArrayOf(0x89.toByte(), 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a)
    }
}
