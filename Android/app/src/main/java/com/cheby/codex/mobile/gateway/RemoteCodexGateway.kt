package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.data.RemoteGatewaySession
import com.cheby.codex.mobile.data.SecureGatewayConfigStore
import com.cheby.codex.mobile.data.PendingApprovalCommand
import com.cheby.codex.mobile.data.scopeKey
import com.cheby.codex.mobile.model.ActionItem
import com.cheby.codex.mobile.model.ActionKind
import com.cheby.codex.mobile.model.ActionsBlock
import com.cheby.codex.mobile.model.CodexThread
import com.cheby.codex.mobile.model.ConversationEvent
import com.cheby.codex.mobile.model.ConversationSnapshot
import com.cheby.codex.mobile.model.EventSequenceDecision
import com.cheby.codex.mobile.model.EventSequenceGate
import com.cheby.codex.mobile.model.GatewayConnectionState
import com.cheby.codex.mobile.model.MessageRole
import com.cheby.codex.mobile.model.NoticeBlock
import com.cheby.codex.mobile.model.PatchOperation
import com.cheby.codex.mobile.model.ProcessedEventIdentity
import com.cheby.codex.mobile.model.RichMessage
import com.cheby.codex.mobile.model.RichMessageState
import com.cheby.codex.mobile.model.ThreadKey
import com.cheby.codex.mobile.model.Tone
import java.io.FilterInputStream
import java.io.IOException
import java.io.InputStream
import java.io.InputStreamReader
import java.nio.charset.CodingErrorAction
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference
import javax.net.ssl.SSLSocketFactory
import kotlin.random.Random
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.buffer
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.job
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import okhttp3.HttpUrl
import okhttp3.Call
import okhttp3.MediaType
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.ResponseBody
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

open class GatewayTransportException(message: String) : Exception(message)

class GatewayRequestException(
    val code: String,
    val retryable: Boolean,
    val retryAfterMillis: Long? = null,
) : GatewayTransportException("Gateway request rejected")

internal class BoundedReconnectBackoff(
    private val random: () -> Double = { Random.nextDouble() },
    private val baseMillis: Long = MIN_RETRY_DELAY_MS,
    private val localMaxMillis: Long = MAX_LOCAL_RETRY_DELAY_MS,
) {
    private var attempt = 0

    fun nextDelay(serverHintMillis: Long?): Long {
        val exponent = attempt.coerceAtMost(10)
        attempt += 1
        val exponential = (baseMillis * (1L shl exponent)).coerceAtMost(localMaxMillis)
        val jittered = (exponential * (0.8 + random().coerceIn(0.0, 1.0) * 0.4))
            .toLong()
            .coerceIn(baseMillis, localMaxMillis)
        // Server hints are bounded separately when parsed. Re-applying the
        // local exponential cap here would violate the protocol's "wait at
        // least Retry-After" requirement for valid hints above 30 seconds.
        return maxOf(jittered, serverHintMillis ?: 0)
    }

    fun reset() {
        attempt = 0
    }
}

internal fun parseRetryAfterMillis(raw: String?): Long? {
    val normalized = raw?.trim()?.removePrefix("+") ?: return null
    if (normalized.isEmpty() || normalized.any { !it.isDigit() }) return null
    // A syntactically valid but numerically overflowing decimal is still a
    // server hint. Treat it as the public maximum instead of falling back to
    // a short local retry that could amplify throttling.
    val seconds = normalized.toLongOrNull() ?: MAX_SERVER_RETRY_AFTER_SECONDS
    return (seconds.coerceAtMost(MAX_SERVER_RETRY_AFTER_SECONDS) * 1_000)
        .coerceAtLeast(MIN_RETRY_DELAY_MS)
}

internal fun parseSocketRetryAfterMillis(reason: String): Long? {
    val match = Regex("^retry-after=([+-]?\\d+)$").matchEntire(reason.trim()) ?: return null
    return parseRetryAfterMillis(match.groupValues[1])
}

private const val MIN_RETRY_DELAY_MS = 750L
private const val MAX_LOCAL_RETRY_DELAY_MS = 30_000L
internal const val MAX_SERVER_RETRY_AFTER_SECONDS = 3_600L
internal const val MAX_CONTROL_RESPONSE_BYTES = 2 * 1024 * 1024
internal const val MAX_THREAD_DETAIL_RESPONSE_BYTES = 4 * 1024 * 1024
internal const val MAX_ERROR_RESPONSE_BYTES = 64 * 1024
internal const val MAX_PAIRING_RESPONSE_BYTES = 64 * 1024
internal const val MAX_WEBSOCKET_FRAME_BYTES = 512 * 1024

internal enum class RemoteOperationRegistration {
    HTTP,
    WEBSOCKET,
}

internal enum class RemoteApprovalStoreWrite {
    PROJECT_EVENT,
    ACTION_COMPLETION,
}

class RemoteCodexGateway internal constructor(
    private val sessionStore: SecureGatewayConfigStore,
    session: RemoteGatewaySession,
    private val deviceProofKey: DeviceProofKeyHandle,
    private val client: OkHttpClient = defaultClient(),
    private val webSocketSslSocketFactory: SSLSocketFactory = client.sslSocketFactory,
    private val codec: ContractV1Codec = ContractV1Codec(),
    private val scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.IO),
    autoRefresh: Boolean = true,
    private val reconnectBackoffFactory: () -> BoundedReconnectBackoff = { BoundedReconnectBackoff() },
    private val reconnectDelay: suspend (Long) -> Unit = { delay(it) },
    private val beforeOperationRegistration: (RemoteOperationRegistration) -> Unit = {},
    private val beforeApprovalStoreWrite: (RemoteApprovalStoreWrite) -> Unit = {},
    private val onDeviceIdentityInvalidated: (String) -> Unit = {},
) : CodexGateway, CloseableCodexGateway {
    override val sessionScope: String = session.scopeKey()
    private val _kind = MutableStateFlow(GatewayKind.REMOTE)
    override val kind: StateFlow<GatewayKind> = _kind.asStateFlow()
    private val baseUrl = requireSecureBaseUrl(session.baseUrl)
    private val auth = AtomicReference(session)
    private val refreshMutex = Mutex()
    private val cursor = AtomicReference(Cursor(session.streamId, session.lastAckSeq))
    private val _connectionState = MutableStateFlow(GatewayConnectionState.CONNECTING)
    override val connectionState: StateFlow<GatewayConnectionState> = _connectionState.asStateFlow()
    private val _threads = MutableStateFlow<List<CodexThread>>(emptyList())
    override val threads: StateFlow<List<CodexThread>> = _threads.asStateFlow()
    private val approvalLock = Any()
    private val approvalCommands = sessionStore.loadApprovalCommands()
        .filter { it.sessionScope == sessionScope }
        .associateByTo(mutableMapOf()) { it.actionRef }
    private val approvals = mutableMapOf<String, ApprovalRecord>()
    private val approvalInFlight = mutableSetOf<String>()
    private val closed = AtomicBoolean(false)
    private val operationOwnership = Any()
    private val activeSockets = ConcurrentHashMap.newKeySet<BoundedWebSocketConnection>()
    private val activeCalls = ConcurrentHashMap.newKeySet<Call>()

    init {
        require(session.accessToken.isNotBlank() && session.refreshToken.isNotBlank())
        if (autoRefresh) scope.launch { refreshThreads() }
    }

    override suspend fun createThread(title: String): CodexThread {
        val body = buildJsonObject { if (title.isNotBlank()) put("title", title.take(120)) }
        val thread = codec.decodeThread(execute("POST", url("v1", "threads"), body.toString()))
        refreshThreads()
        return thread
    }

    override suspend fun renameThread(threadKey: ThreadKey, title: String) {
        if (title.isBlank()) return
        val body = buildJsonObject { put("title", title.trim().take(120)) }
        codec.decodeThread(execute("PATCH", url("v1", "threads", threadKey.value), body.toString()))
        refreshThreads()
    }

    override suspend fun archiveThread(threadKey: ThreadKey) {
        val body = buildJsonObject { put("archived", true) }
        codec.decodeThread(execute("PATCH", url("v1", "threads", threadKey.value), body.toString()))
        refreshThreads()
    }

    override suspend fun deleteThread(threadKey: ThreadKey) {
        val target = url("v1", "threads", threadKey.value).newBuilder()
            .addQueryParameter("confirmPermanentDelete", "true")
            .build()
        execute("DELETE", target)
        refreshThreads()
    }

    override suspend fun loadConversation(threadKey: ThreadKey): ConversationSnapshot {
        val detail = codec.decodeThreadDetail(
            execute(
                "GET",
                url("v1", "threads", threadKey.value),
                maxResponseBytes = MAX_THREAD_DETAIL_RESPONSE_BYTES,
            ),
        )
        reconcileApprovalSnapshot(threadKey, detail.approvals)
        val approvalMessages = detail.approvals.mapNotNull { approval ->
            val envelope = ConversationEvent.NoOp(
                eventId = "snapshot:${approval.approvalId}",
                streamId = detail.streamId,
                streamSeq = detail.cursor,
            )
            (projectApprovalRequest(envelope, approval) as? ConversationEvent.MessageAdded)?.message
        }
        val recoveredApprovals = recoverApprovalMessages(threadKey)
        return ConversationSnapshot(
            threadKey,
            detail.streamId,
            detail.cursor,
            detail.messages + (approvalMessages + recoveredApprovals).distinctBy(RichMessage::id)
                .filterNot { recovered -> detail.messages.any { it.id == recovered.id } },
        )
    }

    override suspend fun loadCapabilities(
        expectedSessionScope: String,
        expectedSessionGeneration: Long,
    ): GatewayCapabilities {
        ensureUnderlyingSession(expectedSessionScope, expectedSessionGeneration)
        return codec.decodeCapabilities(
            execute(
                "GET",
                url("v1", "capabilities"),
                maxResponseBytes = MAX_PAIRING_RESPONSE_BYTES,
                requireNoStore = true,
            ),
        )
    }

    override suspend fun uploadTurnImage(
        threadKey: ThreadKey,
        clientMessageId: String,
        clientAssetId: String,
        bytes: ByteArray,
        expectedSessionScope: String,
        expectedSessionGeneration: Long,
    ): UploadedImageAsset {
        ensureUnderlyingSession(expectedSessionScope, expectedSessionGeneration)
        if (
            clientMessageId.length !in 8..128 ||
            clientMessageId.any { it.isWhitespace() || it.isISOControl() } ||
            bytes.isEmpty() ||
            bytes.size > LOCAL_IMAGE_MAX_UPLOAD_BYTES ||
            runCatching { UUID.fromString(clientAssetId).toString() }.getOrNull() != clientAssetId
        ) {
            throw GatewayTransportException("Image input is invalid")
        }
        val response = executeBinary(
            method = "PUT",
            target = url(
                "v1",
                "threads",
                threadKey.value,
                "turn-inputs",
                clientMessageId,
                "images",
                clientAssetId,
            ),
            body = bytes,
            requireNoStore = true,
        )
        ensureUnderlyingSession(expectedSessionScope, expectedSessionGeneration)
        return codec.decodeImageUpload(response)
    }

    private fun reconcileApprovalSnapshot(
        threadKey: ThreadKey,
        pending: List<ApprovalMutation.Requested>,
    ) = synchronized(approvalLock) {
        if (!ownsBackingSession()) return@synchronized
        val authoritativeIds = pending.mapTo(mutableSetOf(), ApprovalMutation.Requested::approvalId)
        val retained = sessionStore.loadApprovalCommands()
            .filter { it.sessionScope == sessionScope }
            .filterNot { command ->
                command.threadId == threadKey.value && command.approvalId !in authoritativeIds
            }
        if (!sessionStore.replaceApprovalCommandsForScope(sessionScope, retained)) return@synchronized
        approvalCommands.entries.removeAll { (_, command) ->
            command.threadId == threadKey.value && command.approvalId !in authoritativeIds
        }
        approvals.entries.removeAll { (approvalId, record) ->
            record.threadId == threadKey.value && approvalId !in authoritativeIds
        }
    }

    override fun observeConversation(
        threadKey: ThreadKey,
        afterStreamId: String,
        afterStreamSeq: Long,
    ): Flow<ConversationEvent> = flow {
        // The GET thread snapshot and these two cursor fields are one recovery
        // boundary. A cursor observed by another thread must never replace it.
        var resume = Cursor(afterStreamId, afterStreamSeq)
        var processedEvents: Map<String, ProcessedEventIdentity> = emptyMap()
        var authenticationReconnects = 0
        val reconnectBackoff = reconnectBackoffFactory()
        while (
            currentCoroutineContext().isActive &&
            !closed.get() &&
            _kind.value == GatewayKind.REMOTE
        ) {
            _connectionState.value = GatewayConnectionState.CONNECTING
            try {
                socketFrames(resume).collect { frame ->
                    val decoded = codec.decodeEvent(frame.text, threadKey)
                    val sequenceDecision = EventSequenceGate.classify(
                        currentStreamId = resume.streamId,
                        currentStreamSeq = resume.seq,
                        processedEvents = processedEvents,
                        eventId = decoded.event.eventId,
                        eventStreamId = decoded.streamId,
                        eventStreamSeq = decoded.seq,
                    )
                    if (sequenceDecision == EventSequenceDecision.CONFLICT) {
                        throw SyncRequiredSocketException()
                    }
                    authenticationReconnects = 0
                    reconnectBackoff.reset()
                    val received = Cursor(decoded.streamId, decoded.seq)
                    _connectionState.value = GatewayConnectionState.ONLINE
                    val projected = if (sequenceDecision == EventSequenceDecision.NEW) {
                        projectApproval(decoded)
                    } else {
                        // Exact duplicates are passed through only so the downstream reducer can
                        // independently confirm the same tuple. Projection has durable side effects
                        // and must never run twice.
                        decoded.event
                    }
                    emit(projected)
                    val acknowledged = if (sequenceDecision == EventSequenceDecision.NEW) received else resume
                    val ack = buildJsonObject {
                        put("type", "ack")
                        put("streamId", acknowledged.streamId)
                        put("seq", acknowledged.seq)
                    }
                    if (!sendSocketTextWithDeadline(frame.socket, ack.toString())) {
                        throw GatewayTransportException("Event acknowledgement failed")
                    }
                    if (sessionStore.loadSession()?.scopeKey() != sessionScope) {
                        throw GatewayTransportException("Gateway session changed")
                    }
                    if (sequenceDecision == EventSequenceDecision.NEW) {
                        if (!sessionStore.updateCursor(sessionScope, decoded.streamId, decoded.seq)) {
                            throw GatewayTransportException("Gateway session changed")
                        }
                        processedEvents = EventSequenceGate.append(
                            processedEvents,
                            decoded.event.eventId,
                            ProcessedEventIdentity(decoded.streamId, decoded.seq),
                        )
                        cursor.set(received)
                        resume = received
                    }
                }
            } catch (_: ProtocolV1Exception) {
                _connectionState.value = GatewayConnectionState.OFFLINE
                throw GatewayTransportException("Gateway protocol mismatch")
            } catch (expired: TokenExpiredSocketException) {
                if (authenticationReconnects >= MAX_AUTHENTICATION_RECONNECTS) {
                    markUnpaired()
                    break
                }
                authenticationReconnects += 1
                try {
                    refreshSession(expired.observedAccessToken)
                    val refreshedStreamId = auth.get().streamId
                    if (refreshedStreamId != resume.streamId) {
                        resume = Cursor(refreshedStreamId, 0)
                        processedEvents = emptyMap()
                    }
                } catch (_: GatewayTransportException) {
                    _connectionState.value = GatewayConnectionState.OFFLINE
                    if (!closed.get() && _kind.value == GatewayKind.REMOTE) {
                        reconnectDelay(reconnectBackoff.nextDelay(null))
                    }
                }
            } catch (limited: RateLimitedSocketException) {
                _connectionState.value = GatewayConnectionState.OFFLINE
                if (!ownsBackingSession()) break
                if (!closed.get()) reconnectDelay(reconnectBackoff.nextDelay(limited.retryAfterMillis))
            } catch (_: SyncRequiredSocketException) {
                _connectionState.value = GatewayConnectionState.OFFLINE
                if (resume.seq == Long.MAX_VALUE) throw GatewayTransportException("Gateway cursor exhausted")
                emit(
                    ConversationEvent.SyncRequired(
                        eventId = "socket-sync:${resume.seq + 1}",
                        streamId = resume.streamId,
                        streamSeq = resume.seq + 1,
                    ),
                )
                return@flow
            } catch (_: GatewayTransportException) {
                _connectionState.value = GatewayConnectionState.OFFLINE
                if (!ownsBackingSession()) break
                if (!closed.get()) reconnectDelay(reconnectBackoff.nextDelay(null))
            }
        }
    }

    override suspend fun sendTurn(
        threadKey: ThreadKey,
        text: String,
        idempotencyKey: String,
        expectedSessionScope: String,
    ): TurnSubmission = sendTurnInput(
        threadKey = threadKey,
        input = listOf(TurnInput.Text(text)),
        idempotencyKey = idempotencyKey,
        expectedSessionScope = expectedSessionScope,
        expectedSessionGeneration = 0,
    )

    override suspend fun sendTurnInput(
        threadKey: ThreadKey,
        input: List<TurnInput>,
        idempotencyKey: String,
        expectedSessionScope: String,
        expectedSessionGeneration: Long,
    ): TurnSubmission {
        ensureUnderlyingSession(expectedSessionScope, expectedSessionGeneration)
        if (
            input.isEmpty() ||
            input.size > MAX_TURN_INPUT_PARTS ||
            idempotencyKey.length !in 8..128 ||
            idempotencyKey.any { it.isWhitespace() || it.isISOControl() }
        ) {
            throw GatewayTransportException("Turn input is invalid")
        }
        var imageCount = 0
        var textCount = 0
        val body = buildJsonObject {
            put("clientMessageId", idempotencyKey)
            put("input", buildJsonArray {
                input.forEach { part ->
                    when (part) {
                        is TurnInput.Text -> {
                            textCount += 1
                            if (textCount > 1 || part.value.isBlank()) {
                                throw GatewayTransportException("Turn input is invalid")
                            }
                            add(buildJsonObject {
                                put("type", "text")
                                put("text", part.value.take(MAX_TURN_TEXT))
                            })
                        }
                        is TurnInput.Image -> {
                            imageCount += 1
                            if (imageCount > LOCAL_IMAGE_MAX_IMAGES || !ASSET_REF.matches(part.assetRef)) {
                                throw GatewayTransportException("Turn input is invalid")
                            }
                            add(buildJsonObject {
                                put("type", "image")
                                put("assetRef", part.assetRef)
                            })
                        }
                    }
                }
            })
        }
        ensureUnderlyingSession(expectedSessionScope, expectedSessionGeneration)
        val response = execute(
            "POST",
            url("v1", "threads", threadKey.value, "turns"),
            body.toString(),
        )
        ensureUnderlyingSession(expectedSessionScope, expectedSessionGeneration)
        return codec.decodeTurnSubmission(response).also { submission ->
            if (
                submission.threadKey != threadKey ||
                submission.clientMessageId != idempotencyKey
            ) {
                throw GatewayTransportException("Turn submission identity changed")
            }
        }
    }

    override suspend fun performAction(actionRef: String, expectedSessionScope: String) {
        ensureCurrentSession(expectedSessionScope)
        val command = synchronized(approvalLock) {
            val selected = approvalCommands[actionRef]
                ?: throw GatewayTransportException("Action is unavailable")
            if (selected.sessionScope != expectedSessionScope) {
                throw GatewayTransportException("Action is unavailable")
            }
            if (selected.expiresAtMillis <= System.currentTimeMillis()) {
                val expectedActionRefs = actionRefsForApproval(selected.approvalId)
                if (
                    sessionStore.removeApprovalCommands(
                        expectedSessionScope,
                        selected.approvalId,
                        expectedActionRefs,
                    )
                ) {
                    approvalCommands.entries.removeAll {
                        it.value.sessionScope == expectedSessionScope &&
                            it.value.approvalId == selected.approvalId
                    }
                }
                throw GatewayTransportException("Action is unavailable")
            }
            val remainingRateLimit = selected.retryNotBeforeMillis - System.currentTimeMillis()
            if (remainingRateLimit > 0) {
                throw GatewayRequestException("RATE_LIMITED", true, remainingRateLimit)
            }
            if (!approvalInFlight.add(selected.approvalId)) {
                throw GatewayTransportException("Action is unavailable")
            }
            selected
        }
        val body = buildJsonObject {
            put("decision", command.decision)
            put("actionToken", command.actionToken)
        }
        try {
            ensureCurrentSession(expectedSessionScope)
            execute(
                "POST",
                url("v1", "approvals", command.approvalId, "decision"),
                body.toString(),
            )
            ensureCurrentSession(expectedSessionScope)
            beforeApprovalStoreWrite(RemoteApprovalStoreWrite.ACTION_COMPLETION)
            synchronized(approvalLock) {
                val expectedActionRefs = actionRefsForApproval(command.approvalId)
                if (
                    !sessionStore.removeApprovalCommands(
                        expectedSessionScope,
                        command.approvalId,
                        expectedActionRefs,
                    )
                ) {
                    throw GatewayTransportException("Gateway session changed")
                }
                approvalCommands.entries.removeAll {
                    it.value.sessionScope == expectedSessionScope &&
                        it.value.approvalId == command.approvalId
                }
            }
        } catch (limited: GatewayRequestException) {
            if (limited.code == "RATE_LIMITED" && limited.retryAfterMillis != null) {
                val retryAt = System.currentTimeMillis() + limited.retryAfterMillis
                synchronized(approvalLock) {
                    val expectedActionRefs = actionRefsForApproval(command.approvalId)
                    val replacements = approvalCommands.values.map { existing ->
                        if (
                            existing.sessionScope == expectedSessionScope &&
                            existing.approvalId == command.approvalId
                        ) {
                            existing.copy(retryNotBeforeMillis = retryAt)
                        } else {
                            existing
                        }
                    }.filter {
                        it.sessionScope == expectedSessionScope &&
                            it.approvalId == command.approvalId
                    }
                    if (
                        sessionStore.replaceApprovalCommandsForApproval(
                            expectedSessionScope,
                            command.approvalId,
                            expectedActionRefs,
                            replacements,
                        )
                    ) {
                        replacements.forEach { approvalCommands[it.actionRef] = it }
                    }
                }
            }
            throw limited
        } finally {
            synchronized(approvalLock) { approvalInFlight.remove(command.approvalId) }
        }
    }

    private fun actionRefsForApproval(approvalId: String): Set<String> =
        approvalCommands.values.asSequence()
            .filter { it.sessionScope == sessionScope && it.approvalId == approvalId }
            .mapTo(mutableSetOf(), PendingApprovalCommand::actionRef)

    private fun ensureCurrentSession(expectedSessionScope: String) {
        if (
            closed.get() ||
            expectedSessionScope != sessionScope ||
            sessionStore.loadSession()?.scopeKey() != sessionScope
        ) {
            throw GatewayTransportException("Gateway session changed")
        }
    }

    private fun projectApproval(decoded: DecodedEvent): ConversationEvent {
        val mutation = decoded.approval ?: return decoded.event
        if (!ownsBackingSession()) return noOp(decoded.event)
        return when (mutation) {
            is ApprovalMutation.Requested -> projectApprovalRequest(decoded.event, mutation)
            is ApprovalMutation.Terminal -> projectApprovalTerminal(decoded.event, mutation)
        }
    }

    private fun projectApprovalRequest(
        envelope: ConversationEvent,
        request: ApprovalMutation.Requested,
    ): ConversationEvent = synchronized(approvalLock) {
        if (!ownsBackingSession()) return@synchronized noOp(envelope)
        val existing = approvals[request.approvalId]
        if (existing != null) {
            return@synchronized ConversationEvent.MessageAdded(
                envelope.eventId,
                envelope.streamId,
                envelope.streamSeq,
                existing.message,
            )
        }

        val timely = request.expiresAtMillis > System.currentTimeMillis()
        val durableExisting = sessionStore.loadApprovalCommands()
            .filter { it.sessionScope == sessionScope && it.approvalId == request.approvalId }
        val expectedActionRefs = durableExisting.mapTo(mutableSetOf(), PendingApprovalCommand::actionRef)
        val stored = durableExisting
            .filter { it.approvalId == request.approvalId && it.expiresAtMillis > System.currentTimeMillis() }
            .associateBy(PendingApprovalCommand::decision)
        val actionRefs = request.decisions.associateWith { decision ->
            stored[decision]?.actionRef ?: UUID.randomUUID().toString()
        }
        val commands = if (request.actionToken != null && timely) {
            request.decisions.map { decision ->
                PendingApprovalCommand(
                    sessionScope = sessionScope,
                    actionRef = actionRefs.getValue(decision),
                    approvalId = request.approvalId,
                    threadId = request.threadId,
                    decision = decision,
                    actionToken = request.actionToken,
                    expiresAtMillis = request.expiresAtMillis,
                    summary = request.summary,
                    reason = request.reason,
                    retryNotBeforeMillis = stored[decision]?.retryNotBeforeMillis ?: 0,
                )
            }
        } else {
            emptyList()
        }
        beforeApprovalStoreWrite(RemoteApprovalStoreWrite.PROJECT_EVENT)
        val durable = sessionStore.replaceApprovalCommandsForApproval(
            sessionScope,
            request.approvalId,
            expectedActionRefs,
            commands,
        )
        if (!durable) return@synchronized noOp(envelope)
        approvalCommands.entries.removeAll {
            it.value.sessionScope == sessionScope && it.value.approvalId == request.approvalId
        }
        val enabled = commands.isNotEmpty() && durable
        val actions = request.decisions.map { decision ->
            val actionRef = actionRefs.getValue(decision)
            if (enabled) {
                approvalCommands[actionRef] = commands.first { it.decision == decision }
            }
            ActionItem(
                id = decision,
                label = if (decision == "approve") "批准" else "拒绝",
                actionRef = actionRef,
                kind = if (decision == "reject") ActionKind.DESTRUCTIVE else ActionKind.PRIMARY,
                enabled = enabled,
                retryNotBeforeMillis = commands.firstOrNull { it.decision == decision }
                    ?.retryNotBeforeMillis ?: 0,
            )
        }
        val message = approvalMessage(
            approvalId = request.approvalId,
            summary = request.summary,
            reason = request.reason,
            actions = actions,
        )
        approvals[request.approvalId] = ApprovalRecord(request.threadId, message, actionRefs)
        ConversationEvent.MessageAdded(
            envelope.eventId,
            envelope.streamId,
            envelope.streamSeq,
            message,
        )
    }

    private fun recoverApprovalMessages(threadKey: ThreadKey): List<RichMessage> = synchronized(approvalLock) {
        val now = System.currentTimeMillis()
        val grouped = approvalCommands.values
            .filter { it.sessionScope == sessionScope }
            .filter { it.threadId == threadKey.value && it.expiresAtMillis > now }
            .groupBy(PendingApprovalCommand::approvalId)
        grouped.map { (approvalId, commands) ->
            val actions = commands.map { command ->
                ActionItem(
                    id = command.decision,
                    label = if (command.decision == "approve") "批准" else "拒绝",
                    actionRef = command.actionRef,
                    kind = if (command.decision == "reject") ActionKind.DESTRUCTIVE else ActionKind.PRIMARY,
                    enabled = true,
                    retryNotBeforeMillis = command.retryNotBeforeMillis,
                )
            }
            val message = approvalMessage(
                approvalId,
                commands.first().summary,
                commands.first().reason,
                actions,
            )
            approvals[approvalId] = ApprovalRecord(
                threadKey.value,
                message,
                commands.associate { it.decision to it.actionRef },
            )
            message
        }
    }

    private fun approvalMessage(
        approvalId: String,
        summary: String,
        reason: String,
        actions: List<ActionItem>,
    ): RichMessage {
        val notice = NoticeBlock(
            id = APPROVAL_NOTICE_BLOCK,
            title = summary,
            body = reason,
            tone = Tone.WARNING,
        )
        val actionBlock = ActionsBlock(APPROVAL_ACTIONS_BLOCK, actions)
        return RichMessage(
            id = "approval:$approvalId",
            role = MessageRole.ASSISTANT,
            state = RichMessageState.WAITING_INPUT,
            revision = 0,
            rootBlockIds = listOf(notice.id, actionBlock.id),
            blocks = mapOf(notice.id to notice, actionBlock.id to actionBlock),
            createdAtMillis = System.currentTimeMillis(),
        )
    }

    private fun projectApprovalTerminal(
        envelope: ConversationEvent,
        terminal: ApprovalMutation.Terminal,
    ): ConversationEvent = synchronized(approvalLock) {
        if (!ownsBackingSession()) return@synchronized noOp(envelope)
        val expectedActionRefs = actionRefsForApproval(terminal.approvalId)
        beforeApprovalStoreWrite(RemoteApprovalStoreWrite.PROJECT_EVENT)
        if (
            !sessionStore.removeApprovalCommands(
                sessionScope,
                terminal.approvalId,
                expectedActionRefs,
            )
        ) return@synchronized noOp(envelope)
        approvalCommands.entries.removeAll {
            it.value.sessionScope == sessionScope && it.value.approvalId == terminal.approvalId
        }
        approvalInFlight.remove(terminal.approvalId)
        val record = approvals.remove(terminal.approvalId) ?: return@synchronized envelope
        record.actionRefs.values.forEach(approvalCommands::remove)
        val stateLabel = when (terminal.state) {
            "approved" -> "已批准"
            "rejected" -> "已拒绝"
            else -> "已过期"
        }
        ConversationEvent.MessagePatched(
            eventId = envelope.eventId,
            streamId = envelope.streamId,
            streamSeq = envelope.streamSeq,
            messageId = record.message.id,
            baseRevision = record.message.revision,
            revision = record.message.revision + 1,
            operations = buildList {
                add(
                    PatchOperation.MergeBlock(
                        APPROVAL_NOTICE_BLOCK,
                        mapOf("body" to stateLabel, "tone" to Tone.NEUTRAL),
                    ),
                )
                record.actionRefs.keys.forEach { decision ->
                    add(PatchOperation.ResolveAction(APPROVAL_ACTIONS_BLOCK, decision))
                }
                add(PatchOperation.SetMessageState(RichMessageState.COMPLETED))
            },
        )
    }

    private fun noOp(envelope: ConversationEvent): ConversationEvent.NoOp = ConversationEvent.NoOp(
        eventId = envelope.eventId,
        streamId = envelope.streamId,
        streamSeq = envelope.streamSeq,
    )

    internal suspend fun refreshThreads() {
        try {
            val result = codec.decodeThreadList(execute("GET", url("v1", "threads")))
            _threads.value = result.sortedByDescending(CodexThread::updatedAtMillis)
            _connectionState.value = GatewayConnectionState.ONLINE
        } catch (_: GatewayTransportException) {
            _connectionState.value = GatewayConnectionState.OFFLINE
        }
    }

    private fun socketFrames(after: Cursor): Flow<SocketFrame> = callbackFlow {
        if (closed.get()) {
            close(GatewayTransportException("Gateway session changed"))
            return@callbackFlow
        }
        val socketUrl = url("v1", "events").newBuilder()
            .addQueryParameter("streamId", after.streamId)
            .addQueryParameter("afterSeq", after.seq.toString())
            .build()
        val observedAccessToken = auth.get().accessToken
        val connection = BoundedWebSocketConnection(
            sslSocketFactory = webSocketSslSocketFactory,
            maxFrameBytes = MAX_WEBSOCKET_FRAME_BYTES,
        )
        beforeOperationRegistration(RemoteOperationRegistration.WEBSOCKET)
        val registered = synchronized(operationOwnership) {
            if (!ownsBackingSession()) {
                false
            } else {
                activeSockets += connection
                if (ownsBackingSession()) {
                    true
                } else {
                    activeSockets -= connection
                    false
                }
            }
        }
        if (!registered) {
            connection.cancel()
            close(GatewayTransportException("Gateway session changed"))
            return@callbackFlow
        }
        val readerJob = launch(Dispatchers.IO) {
            try {
                connection.connect(socketUrl, observedAccessToken, deviceProofKey)
                if (!ownsBackingSession()) throw GatewayTransportException("Gateway session changed")
                val pingJob = launch {
                    while (isActive) {
                        delay(WEBSOCKET_PING_INTERVAL_MS)
                        val challenge = connection.preparePing()
                        if (challenge == null) {
                            connection.cancel()
                            break
                        }
                        val expired = AtomicBoolean(false)
                        val watchdog = launch(start = CoroutineStart.UNDISPATCHED) {
                            delay(WEBSOCKET_PONG_DEADLINE_MS)
                            expired.set(connection.expirePong(challenge))
                        }
                        val sent = connection.sendPing(challenge)
                        if (!sent) {
                            watchdog.cancel()
                            connection.cancel()
                            break
                        }
                        watchdog.join()
                        if (expired.get()) break
                    }
                }
                try {
                    while (isActive) {
                        when (val event = connection.readEvent()) {
                            is BoundedSocketEvent.Text -> send(SocketFrame(connection, event.value))
                            is BoundedSocketEvent.Closed -> {
                                cancelSocketFirst(connection, event.code, event.reason)
                                throw socketCloseException(event.code, event.reason, observedAccessToken)
                            }
                        }
                    }
                } finally {
                    pingJob.cancel()
                }
            } catch (error: Exception) {
                if (error is CancellationException) throw error
                val mapped = when (error) {
                    is DeviceIdentityUnavailableException -> {
                        invalidateDeviceIdentity()
                        GatewayTransportException("Device identity is unavailable")
                    }
                    is TokenExpiredSocketException,
                    is RateLimitedSocketException,
                    is SyncRequiredSocketException,
                    is ProtocolV1Exception,
                    is GatewayTransportException -> error
                    is WebSocketHandshakeException -> if (error.statusCode == 401) {
                        TokenExpiredSocketException(observedAccessToken)
                    } else {
                        GatewayTransportException("Event channel unavailable")
                    }
                    is WebSocketFrameTooLargeException -> {
                        cancelSocketFirst(connection, 1009, "frame-too-large")
                        ProtocolV1Exception("WebSocket frame exceeds limit")
                    }
                    is UnsupportedBinaryFrameException -> {
                        cancelSocketFirst(connection, 1003, "text-frames-only")
                        ProtocolV1Exception("Binary WebSocket frame is unsupported")
                    }
                    is InvalidWebSocketFrameException -> {
                        cancelSocketFirst(connection, 1002, "protocol-error")
                        ProtocolV1Exception("Invalid WebSocket frame")
                    }
                    is WebSocketPongTimeoutException -> {
                        cancelSocketFirst(connection, 1001, "pong-timeout")
                        GatewayTransportException("Event channel unavailable")
                    }
                    else -> GatewayTransportException("Event channel unavailable")
                }
                close(mapped)
            } finally {
                synchronized(operationOwnership) { activeSockets -= connection }
                connection.cancel()
            }
        }
        awaitClose {
            synchronized(operationOwnership) { activeSockets -= connection }
            cancelSocketFirst(connection, 1000, "client-cancelled")
            readerJob.cancel()
        }
    }.buffer(1)

    private fun cancelSocketFirst(
        connection: BoundedWebSocketConnection,
        code: Int,
        reason: String,
    ) {
        connection.cancel()
        // Closing the socket first interrupts a blocked read/write. The RFC close frame is then
        // attempted away from callbackFlow/awaitClose and never delays cancellation.
        scope.launch(Dispatchers.IO) {
            connection.bestEffortCloseFrameAfterCancellation(code, reason)
        }
    }

    private suspend fun sendSocketTextWithDeadline(
        connection: BoundedWebSocketConnection,
        value: String,
    ): Boolean = coroutineScope {
        val deadlineExpired = AtomicBoolean(false)
        val watchdog = launch(start = CoroutineStart.UNDISPATCHED) {
            delay(WEBSOCKET_WRITE_DEADLINE_MS)
            deadlineExpired.set(true)
            connection.cancel()
        }
        try {
            val sent = withContext(Dispatchers.IO) { connection.sendText(value) }
            sent && !deadlineExpired.get()
        } finally {
            watchdog.cancel()
        }
    }

    private fun socketCloseException(
        code: Int,
        reason: String,
        observedAccessToken: String,
    ): GatewayTransportException = when (code) {
        RATE_LIMIT_CLOSE_CODE -> RateLimitedSocketException(parseSocketRetryAfterMillis(reason))
        AUTHENTICATION_CLOSE_CODE -> TokenExpiredSocketException(observedAccessToken)
        SYNC_REQUIRED_CLOSE_CODE -> SyncRequiredSocketException()
        else -> GatewayTransportException("Event channel closed")
    }

    private suspend fun execute(
        method: String,
        target: HttpUrl,
        jsonBody: String? = null,
        maxResponseBytes: Int = MAX_CONTROL_RESPONSE_BYTES,
        requireNoStore: Boolean = false,
    ): String = executeAuthenticated(
        method = method,
        target = target,
        exactBody = jsonBody?.toByteArray(Charsets.UTF_8),
        requestMediaType = if (jsonBody == null) null else JSON_MEDIA_TYPE,
        maxResponseBytes = maxResponseBytes,
        requireNoStore = requireNoStore,
    )

    private suspend fun executeBinary(
        method: String,
        target: HttpUrl,
        body: ByteArray,
        requireNoStore: Boolean,
    ): String = executeAuthenticated(
        method = method,
        target = target,
        exactBody = body,
        requestMediaType = OCTET_STREAM_MEDIA_TYPE,
        maxResponseBytes = MAX_PAIRING_RESPONSE_BYTES,
        requireNoStore = requireNoStore,
    )

    private suspend fun executeAuthenticated(
        method: String,
        target: HttpUrl,
        exactBody: ByteArray?,
        requestMediaType: MediaType?,
        maxResponseBytes: Int,
        requireNoStore: Boolean,
    ): String {
        val observedToken = auth.get().accessToken
        var result = executeOnce(
            method,
            target,
            exactBody,
            observedToken,
            requestMediaType,
            maxResponseBytes,
        )
        if (result.code == 401 && isTokenExpired(result.body)) {
            refreshSession(observedToken)
            result = executeOnce(
                method,
                target,
                exactBody,
                auth.get().accessToken,
                requestMediaType,
                maxResponseBytes,
            )
        }
        if (result.code == 401) {
            if (sessionStore.loadSession()?.scopeKey() != sessionScope) {
                throw GatewayTransportException("Gateway session changed")
            }
            markUnpaired()
            throw GatewayRequestException("AUTH_REQUIRED", false)
        }
        if (result.code !in 200..299) {
            val error = decodeGatewayError(result.body)
            if (result.code == 429) {
                val retryAfter = parseRetryAfterMillis(result.retryAfterHeader) ?: MIN_RETRY_DELAY_MS
                throw GatewayRequestException("RATE_LIMITED", true, retryAfter)
            }
            throw GatewayRequestException(error.first, error.second)
        }
        if (requireNoStore && !result.cacheControlHeader.hasNoStoreDirective()) {
            throw GatewayTransportException("Gateway response cache policy is unsafe")
        }
        return result.body
    }

    private suspend fun refreshSession(observedAccessToken: String) {
        refreshMutex.withLock {
            val current = auth.get()
            if (current.accessToken != observedAccessToken) return
            try {
                val body = buildJsonObject {
                    put("deviceId", current.deviceId)
                    put("refreshToken", current.refreshToken)
                }.toString().toByteArray(Charsets.UTF_8)
                val result = executeOnce(
                    method = "POST",
                    target = url("v1", "auth", "refresh"),
                    exactBody = body,
                    bearerToken = null,
                    requestMediaType = JSON_MEDIA_TYPE,
                    maxResponseBytes = MAX_CONTROL_RESPONSE_BYTES,
                )
                if (result.code !in 200..299) throw GatewayTransportException("Session refresh rejected")
                val rotated = codec.decodeRefresh(result.body, current)
                if (!sessionStore.saveSessionIfScope(sessionScope, rotated)) {
                    throw GatewayTransportException("Session refresh persistence failed")
                }
                auth.set(rotated)
                if (rotated.streamId != cursor.get().streamId) {
                    cursor.set(Cursor(rotated.streamId, 0))
                }
            } catch (_: GatewayTransportException) {
                if (sessionStore.loadSession()?.scopeKey() == sessionScope) markUnpaired()
                throw GatewayTransportException("Session expired")
            } catch (_: IllegalArgumentException) {
                if (sessionStore.loadSession()?.scopeKey() == sessionScope) markUnpaired()
                throw GatewayTransportException("Session expired")
            }
        }
    }

    private suspend fun executeOnce(
        method: String,
        target: HttpUrl,
        exactBody: ByteArray?,
        bearerToken: String?,
        requestMediaType: MediaType?,
        maxResponseBytes: Int,
    ): HttpResult = withContext(Dispatchers.IO) {
        val bodyBytes = exactBody ?: DeviceProofV1.emptyBody()
        val proof = try {
            DeviceProofV1.sign(
                key = deviceProofKey,
                method = method,
                rawTarget = DeviceProofV1.originForm(target),
                body = bodyBytes,
                bearerToken = bearerToken,
            )
        } catch (error: DeviceIdentityUnavailableException) {
            invalidateDeviceIdentity()
            throw GatewayTransportException("Device identity is unavailable")
        } catch (_: DeviceProofException) {
            throw GatewayTransportException("Gateway request proof failed")
        }
        val requestBody = exactBody?.toRequestBody(requestMediaType ?: JSON_MEDIA_TYPE)
        val builder = Request.Builder()
            .url(target)
            .header("Accept", "application/json")
        proof.asMap().forEach(builder::header)
        if (bearerToken != null) builder.header("Authorization", "Bearer $bearerToken")
        val request = builder.method(method, requestBody).build()
        val call = client.newCall(request)
        beforeOperationRegistration(RemoteOperationRegistration.HTTP)
        val registered = synchronized(operationOwnership) {
            if (!ownsBackingSession()) {
                false
            } else {
                activeCalls += call
                if (ownsBackingSession()) {
                    true
                } else {
                    activeCalls -= call
                    false
                }
            }
        }
        if (!registered) {
            call.cancel()
            throw GatewayTransportException("Gateway session changed")
        }
        val cancellation = currentCoroutineContext().job.invokeOnCompletion { cause ->
            if (cause is CancellationException) call.cancel()
        }
        try {
            call.execute().use { response ->
                val responseCode = response.code
                val retryAfter = response.header("Retry-After")
                val responseLimit = if (responseCode in 200..299) {
                    maxResponseBytes
                } else {
                    minOf(maxResponseBytes, MAX_ERROR_RESPONSE_BYTES)
                }
                val responseBody = response.consumeUtf8BodyBounded(responseLimit, "{}") ?: "{}"
                HttpResult(
                    code = responseCode,
                    body = responseBody,
                    retryAfterHeader = retryAfter,
                    cacheControlHeader = response.header("Cache-Control"),
                )
            }
        } catch (error: IOException) {
            if (!currentCoroutineContext().isActive) {
                throw CancellationException("Gateway request cancelled").also { it.initCause(error) }
            }
            throw GatewayTransportException("Gateway unavailable")
        } finally {
            cancellation.dispose()
            synchronized(operationOwnership) { activeCalls -= call }
        }
    }

    private fun isTokenExpired(body: String): Boolean =
        runCatching { codec.decodeGatewayError(body).code == "TOKEN_EXPIRED" }.getOrDefault(false)

    private fun decodeGatewayError(body: String): Pair<String, Boolean> = runCatching {
        codec.decodeGatewayError(body).let { it.code to it.retryable }
    }.getOrDefault("REQUEST_REJECTED" to false)

    private fun ownsBackingSession(): Boolean =
        !closed.get() && sessionStore.loadSession()?.scopeKey() == sessionScope

    private fun ensureUnderlyingSession(expectedSessionScope: String, expectedSessionGeneration: Long) {
        if (expectedSessionGeneration != 0L) throw GatewayTransportException("Gateway session changed")
        ensureCurrentSession(expectedSessionScope)
    }

    private fun String?.hasNoStoreDirective(): Boolean = this
        ?.split(',')
        ?.any { directive -> directive.trim().equals("no-store", ignoreCase = true) }
        ?: false

    private fun markUnpaired(): Boolean = synchronized(operationOwnership) {
        // This check and conditional store delete form one ownership decision.
        // A late callback from a closed A generation can never clear durable B.
        if (!ownsBackingSession()) return@synchronized false
        if (!sessionStore.clearSessionIfScope(sessionScope)) return@synchronized false
        auth.set(auth.get().copy(accessToken = "", refreshToken = ""))
        synchronized(approvalLock) {
            approvalCommands.clear()
            approvals.clear()
            approvalInFlight.clear()
        }
        _threads.value = emptyList()
        _kind.value = GatewayKind.UNCONFIGURED
        _connectionState.value = GatewayConnectionState.OFFLINE
        true
    }

    private fun invalidateDeviceIdentity() {
        if (markUnpaired()) runCatching { onDeviceIdentityInvalidated(sessionScope) }
    }

    override fun close() {
        val ownedOperations = synchronized(operationOwnership) {
            if (!closed.compareAndSet(false, true)) return
            val calls = activeCalls.toList()
            val sockets = activeSockets.toList()
            activeCalls.clear()
            activeSockets.clear()
            calls to sockets
        }
        _connectionState.value = GatewayConnectionState.OFFLINE
        ownedOperations.first.forEach(Call::cancel)
        ownedOperations.second.forEach(BoundedWebSocketConnection::cancel)
        scope.cancel()
    }

    private fun url(vararg segments: String): HttpUrl = baseUrl.newBuilder().apply {
        segments.forEach(::addPathSegment)
    }.build()

    private data class Cursor(val streamId: String, val seq: Long)
    private data class SocketFrame(val socket: BoundedWebSocketConnection, val text: String)
    private data class HttpResult(
        val code: Int,
        val body: String,
        val retryAfterHeader: String?,
        val cacheControlHeader: String?,
    )
    private data class ApprovalRecord(
        val threadId: String,
        val message: RichMessage,
        val actionRefs: Map<String, String>,
    )
    private class TokenExpiredSocketException(val observedAccessToken: String) :
        GatewayTransportException("Session expired")
    private class RateLimitedSocketException(val retryAfterMillis: Long?) :
        GatewayTransportException("Event channel rate limited")
    private class SyncRequiredSocketException :
        GatewayTransportException("Gateway snapshot sync required")

    companion object {
        private const val MAX_AUTHENTICATION_RECONNECTS = 1
        private const val AUTHENTICATION_CLOSE_CODE = 4401
        private const val SYNC_REQUIRED_CLOSE_CODE = 4409
        private const val RATE_LIMIT_CLOSE_CODE = 4429
        private const val WEBSOCKET_PING_INTERVAL_MS = 20_000L
        private const val WEBSOCKET_PONG_DEADLINE_MS = 10_000L
        private const val WEBSOCKET_WRITE_DEADLINE_MS = 10_000L
        private const val APPROVAL_NOTICE_BLOCK = "approval-notice"
        private const val APPROVAL_ACTIONS_BLOCK = "approval-actions"
        private const val MAX_TURN_TEXT = 100_000
        private const val MAX_TURN_INPUT_PARTS = 11
        private const val LOCAL_IMAGE_MAX_UPLOAD_BYTES = 8 * 1024 * 1024
        private const val LOCAL_IMAGE_MAX_IMAGES = 10
        private val JSON_MEDIA_TYPE = "application/json; charset=utf-8".toMediaType()
        private val OCTET_STREAM_MEDIA_TYPE = "application/octet-stream".toMediaType()
        private val ASSET_REF = Regex("[A-Za-z0-9_-]{32,128}")

        internal fun defaultClient(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)
            .retryOnConnectionFailure(false)
            .followRedirects(false)
            .followSslRedirects(false)
            .build()
    }
}

internal object RemotePairingClient {
    suspend fun exchange(
        store: SecureGatewayConfigStore,
        deviceName: String,
        deviceProofKey: DeviceProofKeyHandle,
        client: OkHttpClient = RemoteCodexGateway.defaultClient(),
        afterSessionReplace: (String) -> Unit = {},
    ): RemoteGatewaySession {
        val pairing = store.loadPairing() ?: throw GatewayTransportException("Pairing is not configured")
        val baseUrl = requireSecureBaseUrl(pairing.baseUrl)
        val devicePublicKey = java.util.Base64.getEncoder().encodeToString(
            deviceProofKey.publicKeySpkiDer(),
        )
        val exactBody = buildJsonObject {
            put("pairingSecret", pairing.pairingSecret)
            put("deviceName", deviceName.take(80))
            put("devicePublicKey", devicePublicKey)
        }.toString().toByteArray(Charsets.UTF_8)
        val target = baseUrl.newBuilder().addPathSegments("v1/pairings/exchange").build()
        val proof = try {
            DeviceProofV1.sign(
                key = deviceProofKey,
                method = "POST",
                rawTarget = DeviceProofV1.originForm(target),
                body = exactBody,
                bearerToken = null,
            )
        } catch (_: DeviceProofException) {
            throw GatewayTransportException("Pairing device proof failed")
        }
        val builder = Request.Builder()
            .url(target)
            .post(exactBody.toRequestBody("application/json; charset=utf-8".toMediaType()))
            .header("Accept", "application/json")
        proof.asMap().forEach(builder::header)
        val request = builder.build()
        val responseBody = withContext(Dispatchers.IO) {
            val response = try {
                client.newCall(request).execute()
            } catch (_: IOException) {
                throw GatewayTransportException("Pairing service unavailable")
            }
            if (!response.isSuccessful) {
                response.close()
                throw GatewayTransportException("Pairing rejected")
            }
            response.consumeUtf8BodyBounded(MAX_PAIRING_RESPONSE_BYTES, null)
                ?: throw GatewayTransportException("Pairing response missing")
        }
        val session = ContractV1Codec().decodePairing(responseBody, pairing.baseUrl)
        if (!store.saveSession(session)) {
            throw GatewayTransportException("Pairing session persistence failed")
        }
        // Records are scope-bound, so a crash before best-effort cleanup cannot
        // replay the old container's prompt or action token into this session.
        store.replaceApprovalCommandsForScope(
            session.scopeKey(),
            store.loadApprovalCommands().filter { it.sessionScope == session.scopeKey() },
        )
        afterSessionReplace(session.scopeKey())
        store.clearPairing()
        return session
    }
}

internal fun String.exceedsUtf8Limit(maxBytes: Int): Boolean =
    com.cheby.codex.mobile.model.SafeDisplayText.exceedsUtf8Limit(this, maxBytes)

internal fun Response.consumeUtf8BodyBounded(maxBytes: Int, missingBody: String?): String? = use { response ->
    val responseBody = response.body ?: return@use missingBody
    responseBody.readUtf8Bounded(maxBytes)
}

private fun ResponseBody.readUtf8Bounded(maxBytes: Int): String {
    if (contentLength() > maxBytes) throw GatewayTransportException("Gateway response exceeds limit")
    try {
        val decoder = Charsets.UTF_8.newDecoder()
            .onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT)
        val reader = InputStreamReader(LimitedInputStream(byteStream(), maxBytes), decoder)
        val output = StringBuilder(minOf(maxBytes, 16 * 1024))
        val characters = CharArray(4 * 1024)
        while (true) {
            val count = reader.read(characters)
            if (count < 0) break
            output.append(characters, 0, count)
        }
        return output.toString()
    } catch (error: GatewayTransportException) {
        throw error
    } catch (_: Exception) {
        throw GatewayTransportException("Gateway response is invalid UTF-8")
    }
}

private class LimitedInputStream(
    delegate: InputStream,
    private val maxBytes: Int,
) : FilterInputStream(delegate) {
    private var consumed = 0

    override fun read(): Int {
        val value = super.read()
        if (value < 0) return value
        consumed += 1
        enforceLimit()
        return value
    }

    override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
        if (length == 0) return 0
        val remainingWithProbe = (maxBytes - consumed + 1).coerceAtLeast(1)
        val count = super.read(buffer, offset, minOf(length, remainingWithProbe))
        if (count < 0) return count
        consumed += count
        enforceLimit()
        return count
    }

    private fun enforceLimit() {
        if (consumed > maxBytes) throw GatewayTransportException("Gateway response exceeds limit")
    }
}

internal fun requireSecureBaseUrl(raw: String): HttpUrl {
    val parsed = raw.toHttpUrlOrNull() ?: throw IllegalArgumentException("Invalid gateway URL")
    require(parsed.scheme == "https") { "Gateway requires HTTPS" }
    require(parsed.username.isEmpty() && parsed.password.isEmpty()) { "Gateway URL must not contain credentials" }
    require(parsed.query == null && parsed.fragment == null) { "Gateway URL must not contain query or fragment" }
    return parsed
}
