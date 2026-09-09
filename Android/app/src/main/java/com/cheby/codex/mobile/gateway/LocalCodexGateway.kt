package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.model.CodexThread
import com.cheby.codex.mobile.model.ConversationEvent
import com.cheby.codex.mobile.model.ConversationReducer
import com.cheby.codex.mobile.model.ConversationSnapshot
import com.cheby.codex.mobile.model.EphemeralMediaStore
import com.cheby.codex.mobile.model.GatewayConnectionState
import com.cheby.codex.mobile.model.MediaBlock
import com.cheby.codex.mobile.model.MessageRole
import com.cheby.codex.mobile.model.MessagePresentation
import com.cheby.codex.mobile.model.PatchOperation
import com.cheby.codex.mobile.model.RichMessage
import com.cheby.codex.mobile.model.RichMessageState
import com.cheby.codex.mobile.model.RichBlock
import com.cheby.codex.mobile.model.StepState
import com.cheby.codex.mobile.model.TextBlock
import com.cheby.codex.mobile.model.ThreadConversationState
import com.cheby.codex.mobile.model.ThreadKey
import com.cheby.codex.mobile.model.ThreadRunState
import com.cheby.codex.mobile.model.ToolBlock
import com.cheby.codex.mobile.model.TurnLifecyclePhase
import com.cheby.codex.mobile.model.UnknownBlock
import java.util.UUID
import java.util.Base64
import java.net.URI
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.flow.filter
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put

/** Adapts the authenticated loopback Codex app-server to the existing mobile UI contract. */
internal class LocalCodexGateway(
    bearerToken: String,
    private val client: LocalAppServerClient = LocalAppServerClient(bearerToken),
    private val imageStore: LocalTurnImageStore? = null,
    private val scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.IO),
    private val savedReasoningEffort: () -> String? = { null },
    private val savedModel: () -> String? = { null },
) : CodexGateway, CloseableCodexGateway, LocalRuntimeGatewayControls,
    LocalThreadModelControls, OpenAiAccountGatewayControls {
    override val sessionScope: String = LOCAL_SESSION_SCOPE
    override val sessionIdentity: StateFlow<GatewaySessionIdentity> = MutableStateFlow(
        GatewaySessionIdentity(LOCAL_SESSION_SCOPE, 0),
    )
    override val kind: StateFlow<GatewayKind> = MutableStateFlow(GatewayKind.LOCAL)
    override val connectionState: StateFlow<GatewayConnectionState> = client.connectionState
    private val _threads = MutableStateFlow<List<CodexThread>>(emptyList())
    override val threads: StateFlow<List<CodexThread>> = _threads.asStateFlow()

    private val closed = AtomicBoolean(false)
    private val configurationMutex = Mutex()
    private val eventCounter = AtomicLong(0)
    private val eventBus = MutableSharedFlow<Pair<ThreadKey, ConversationEvent>>(
        replay = EVENT_REPLAY,
        extraBufferCapacity = EVENT_BUFFER,
    )
    private val streams = ConcurrentHashMap<ThreadKey, String>()
    private val states = ConcurrentHashMap<ThreadKey, ThreadConversationState>()
    private val resumedThreads = ConcurrentHashMap.newKeySet<ThreadKey>()
    /**
     * Codex does not return an empty thread from thread/list until its first turn persists.
     * Keep the locally-created row authoritative across name/status refreshes in that window.
     */
    private val provisionalThreads = ConcurrentHashMap<ThreadKey, CodexThread>()
    private val activeTurns = ConcurrentHashMap<ThreadKey, String>()
    private val turnLastActivityMillis = ConcurrentHashMap<String, Long>()
    private val turnWatchdogs = ConcurrentHashMap<String, Job>()
    private val taskResponsePendingClientIds = ConcurrentHashMap.newKeySet<String>()
    private val sourceMediaByUploadRef = ConcurrentHashMap<String, String>()
    private val sourceMediaByClientId = ConcurrentHashMap<String, List<String>>()
    @Volatile private var availableModels: List<AvailableModel> = emptyList()

    init {
        client.start(
            scope = scope,
            onNotification = ::handleNotification,
            onReady = {
                resumedThreads.clear()
                // Catalog repair can be slow on a phone-backed proot filesystem. It must not own
                // the WebSocket lifecycle: thread/start and turn/start remain usable even when a
                // best-effort catalog refresh is delayed or times out.
                scope.launch { runCatching { refreshThreads() } }
            },
        )
    }

    override suspend fun createThread(title: String): CodexThread {
        ensureOpen()
        val result = client.request(
            "thread/start",
            buildJsonObject {
                put("cwd", LOCAL_WORKDIR)
                put("approvalPolicy", "never")
                put("sandbox", "danger-full-access")
                put("serviceName", "chebyagent_android")
            },
        )
        val threadObject = result.objectValue("thread")
            ?: throw GatewayTransportException("Local Codex protocol mismatch")
        val threadKey = ThreadKey(threadObject.requiredString("id"))
        resumedThreads += threadKey
        val normalizedTitle = title.trim().take(MAX_TITLE_CHARS).ifBlank { "新会话" }
        val thread = parseThread(threadObject).copy(
            title = normalizedTitle,
            model = result.stringValue("model") ?: savedModel(),
            reasoningEffort = result.stringValue("reasoningEffort") ?: savedReasoningEffort(),
        )
        provisionalThreads[threadKey] = thread
        client.request(
            "thread/name/set",
            buildJsonObject {
                put("threadId", threadKey.value)
                put("name", normalizedTitle)
            },
        )
        _threads.value = listOf(thread) + _threads.value.filterNot { it.key == threadKey }
        ensureState(threadKey)
        return thread
    }

    override suspend fun renameThread(threadKey: ThreadKey, title: String) {
        ensureOpen()
        val normalized = title.trim().take(MAX_TITLE_CHARS)
        if (normalized.isBlank()) return
        client.request(
            "thread/name/set",
            buildJsonObject {
                put("threadId", threadKey.value)
                put("name", normalized)
            },
        )
        _threads.value = _threads.value.map { thread ->
            if (thread.key == threadKey) thread.copy(title = normalized) else thread
        }
    }

    override suspend fun archiveThread(threadKey: ThreadKey) {
        ensureOpen()
        client.request("thread/archive", threadIdParams(threadKey))
        removeThread(threadKey)
    }

    override suspend fun deleteThread(threadKey: ThreadKey) {
        ensureOpen()
        client.request("thread/delete", threadIdParams(threadKey))
        removeThread(threadKey)
    }

    override suspend fun loadConversation(threadKey: ThreadKey): ConversationSnapshot {
        ensureOpen()
        ensureResumed(threadKey)
        val result = client.request(
            "thread/read",
            buildJsonObject {
                put("threadId", threadKey.value)
                put("includeTurns", true)
            },
        )
        val thread = result.objectValue("thread")
            ?: throw GatewayTransportException("Local Codex protocol mismatch")
        if (thread.requiredString("id") != threadKey.value) {
            throw GatewayTransportException("Local Codex protocol mismatch")
        }
        applyOfficialThread(thread)
        val messages = thread.arrayValue("turns").orEmpty().flatMap { turnElement ->
            val turn = turnElement.asObject() ?: return@flatMap emptyList()
            val turnId = turn.stringValue("id") ?: return@flatMap emptyList()
            val createdAt = (turn.longValue("startedAt") ?: thread.longValue("createdAt") ?: 0L)
                .coerceAtLeast(0L) * 1_000L
            turn.arrayValue("items").orEmpty().mapNotNull { item ->
                item.asObject()?.let { projectItem(it, turnId, createdAt) }
            }
        }.takeLast(MAX_SNAPSHOT_MESSAGES)
        val current = synchronized(this) {
            val prior = ensureState(threadKey)
            val snapshotState = ThreadConversationState(
                threadKey = threadKey,
                streamId = prior.streamId,
                streamSeq = prior.streamSeq,
                messages = messages,
            )
            states[threadKey] = snapshotState
            snapshotState
        }
        return ConversationSnapshot(
            threadKey = threadKey,
            streamId = current.streamId,
            cursor = current.streamSeq,
            messages = current.messages,
        )
    }

    override fun observeConversation(
        threadKey: ThreadKey,
        afterStreamId: String,
        afterStreamSeq: Long,
    ): Flow<ConversationEvent> = eventBus.asSharedFlow()
        .filter { (key, event) ->
            key == threadKey && (event.streamId != afterStreamId || event.streamSeq > afterStreamSeq)
        }
        .map { it.second }

    override suspend fun loadCapabilities(
        expectedSessionScope: String,
        expectedSessionGeneration: Long,
    ): GatewayCapabilities {
        ensureOpen(expectedSessionScope, expectedSessionGeneration)
        val available = imageStore?.isAvailable() == true
        val result = client.request(
            "model/list",
            buildJsonObject {
                put("includeHidden", false)
                put("limit", MAX_MODELS)
            },
        )
        val models = result.arrayValue("data").orEmpty().mapNotNull { element ->
            val value = element.asObject() ?: return@mapNotNull null
            val id = value.stringValue("model") ?: value.stringValue("id") ?: return@mapNotNull null
            val supported = value.arrayValue("supportedReasoningEfforts").orEmpty().mapNotNull { raw ->
                val option = raw.asObject() ?: return@mapNotNull null
                val effort = option.stringValue("reasoningEffort") ?: return@mapNotNull null
                ModelReasoningEffort(effort, option.stringValue("description").orEmpty())
            }
            val defaultEffort = value.stringValue("defaultReasoningEffort") ?: return@mapNotNull null
            if (supported.none { it.value == defaultEffort }) return@mapNotNull null
            AvailableModel(
                id = id,
                displayName = value.stringValue("displayName") ?: id,
                description = value.stringValue("description").orEmpty(),
                isDefault = value.stringValue("isDefault") == "true",
                defaultReasoningEffort = defaultEffort,
                supportedReasoningEfforts = supported,
            )
        }.distinctBy(AvailableModel::id)
        availableModels = models
        return GatewayCapabilities(
            localImage = LOCAL_IMAGE_CAPABILITY.takeIf { available },
            models = models,
        )
    }

    override fun selectThreadModel(
        threadKey: ThreadKey,
        model: String,
        reasoningEffort: String,
    ): Boolean {
        if (_threads.value.none { it.key == threadKey }) return false
        val modelSpec = availableModels.firstOrNull { it.id == model } ?: return false
        if (modelSpec.supportedReasoningEfforts.none { it.value == reasoningEffort }) return false
        _threads.value = _threads.value.map { thread ->
            if (thread.key == threadKey) thread.copy(model = model, reasoningEffort = reasoningEffort)
            else thread
        }
        provisionalThreads.computeIfPresent(threadKey) { _, thread ->
            thread.copy(model = model, reasoningEffort = reasoningEffort)
        }
        return true
    }

    override suspend fun uploadTurnImage(
        threadKey: ThreadKey,
        clientMessageId: String,
        clientAssetId: String,
        bytes: ByteArray,
        expectedSessionScope: String,
        expectedSessionGeneration: Long,
    ): UploadedImageAsset {
        ensureOpen(expectedSessionScope, expectedSessionGeneration)
        val store = imageStore ?: throw GatewayTransportException("Image input is unavailable")
        val stored = withContext(Dispatchers.IO) {
            store.put(threadKey, clientMessageId, clientAssetId, bytes)
        }
        EphemeralMediaStore.putOriginal(stored.mediaType, bytes)?.let { mediaId ->
            sourceMediaByUploadRef[stored.assetRef] = mediaId
        }
        ensureOpen(expectedSessionScope, expectedSessionGeneration)
        return UploadedImageAsset(
            assetRef = stored.assetRef,
            mediaType = stored.mediaType,
            width = 0,
            height = 0,
            byteCount = stored.byteCount,
        )
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
        expectedSessionGeneration = sessionIdentity.value.generation,
    )

    override suspend fun sendTurnInput(
        threadKey: ThreadKey,
        input: List<TurnInput>,
        idempotencyKey: String,
        expectedSessionScope: String,
        expectedSessionGeneration: Long,
    ): TurnSubmission = configurationMutex.withLock {
        submitTurnInput(threadKey, input, idempotencyKey, expectedSessionScope, expectedSessionGeneration)
    }

    private suspend fun submitTurnInput(
        threadKey: ThreadKey,
        input: List<TurnInput>,
        idempotencyKey: String,
        expectedSessionScope: String,
        expectedSessionGeneration: Long,
    ): TurnSubmission {
        ensureOpen(expectedSessionScope, expectedSessionGeneration)
        if (
            input.isEmpty() ||
            input.size > MAX_TURN_INPUT_PARTS ||
            idempotencyKey.length !in 8..128 ||
            idempotencyKey.any { it.isWhitespace() || it.isISOControl() }
        ) {
            throw GatewayTransportException("Turn input is invalid")
        }
        var textCount = 0
        var imageCount = 0
        val storedImages = mutableListOf<LocalTurnImageStore.StoredImage>()
        val sourceMediaIds = mutableListOf<String>()
        val consumedUploadRefs = mutableListOf<String>()
        val directInput = buildJsonArray {
            input.forEach { part ->
                when (part) {
                    is TurnInput.Text -> {
                        val normalized = part.value.trim()
                        textCount += 1
                        if (textCount > 1 || normalized.isBlank() || normalized.length > MAX_TURN_CHARS) {
                            throw GatewayTransportException("Turn input is invalid")
                        }
                        add(buildJsonObject {
                            put("type", "text")
                            put("text", normalized)
                        })
                    }
                    is TurnInput.Image -> {
                        imageCount += 1
                        if (imageCount > LOCAL_IMAGE_CAPABILITY.maxImagesPerTurn) {
                            throw GatewayTransportException("Turn input is invalid")
                        }
                        val stored = imageStore?.resolve(threadKey, idempotencyKey, part.assetRef)
                            ?: throw GatewayTransportException("Image input is unavailable")
                        storedImages += stored
                        sourceMediaByUploadRef[part.assetRef]?.let(sourceMediaIds::add)
                        consumedUploadRefs += part.assetRef
                        add(buildJsonObject {
                            // Preserve the upstream Codex app-server image contract so a
                            // multimodal primary model receives the actual image bytes.
                            put("type", "localImage")
                            put("path", stored.codexPath)
                            put("detail", "auto")
                        })
                    }
                }
            }
        }
        if (textCount == 0 && imageCount == 0) throw GatewayTransportException("Turn input is invalid")
        if (sourceMediaIds.isNotEmpty()) {
            sourceMediaByClientId[idempotencyKey] = sourceMediaIds.toList()
        }
        ensureOpen(expectedSessionScope, expectedSessionGeneration)
        ensureResumed(threadKey)
        val threadConfiguration = _threads.value.firstOrNull { it.key == threadKey }
        val requestedEffort = threadConfiguration?.reasoningEffort ?: savedReasoningEffort()
        val requestedModel = threadConfiguration?.model ?: savedModel()
        taskResponsePendingClientIds += idempotencyKey
        val result = try {
            client.request(
                "turn/start",
                buildJsonObject {
                    put("threadId", threadKey.value)
                    put("clientUserMessageId", idempotencyKey)
                    put("input", directInput)
                    // App-server resolves these values again for every turn. Thread-level
                    // defaults alone silently fall back to workspace-write/on-request, which
                    // prevents the agent from reading its installed Skills under CODEX_HOME.
                    put("approvalPolicy", "never")
                    put("sandboxPolicy", buildJsonObject { put("type", "dangerFullAccess") })
                    // Resumed threads retain their previous effort; the saved UI selection
                    // must override it explicitly for each new turn.
                    requestedEffort?.let { put("effort", it) }
                    requestedModel?.let { put("model", it) }
                },
            )
        } catch (error: Exception) {
            taskResponsePendingClientIds.remove(idempotencyKey)
            throw error
        }
        val turn = result.objectValue("turn")
            ?: throw GatewayTransportException("Local Codex protocol mismatch")
        val turnId = turn.requiredString("id")
        consumedUploadRefs.forEach(sourceMediaByUploadRef::remove)
        taskResponsePendingClientIds.remove(idempotencyKey)
        val submission = TurnSubmission(
            turnId = turnId,
            threadKey = threadKey,
            clientMessageId = idempotencyKey,
            status = turn.stringValue("status") ?: "inProgress",
        )
        if (storedImages.isNotEmpty()) imageStore?.claim(turnId, storedImages)
        return submission
    }

    override suspend fun interruptTurn(
        threadKey: ThreadKey,
        expectedTurnId: String?,
        expectedSessionScope: String,
        expectedSessionGeneration: Long,
    ) = configurationMutex.withLock {
        ensureOpen(expectedSessionScope, expectedSessionGeneration)
        // After Activity/process recovery there may be no local turn/started event.
        // Resolve only this thread's live turn, never another thread or a replacement turn.
        val turnId = expectedTurnId ?: activeTurns[threadKey] ?: run {
            val result = client.request("thread/read", buildJsonObject {
                put("threadId", threadKey.value)
                put("includeTurns", true)
            })
            val thread = result.objectValue("thread")
                ?: throw GatewayTransportException("Local Codex protocol mismatch")
            if (thread.requiredString("id") != threadKey.value) {
                throw GatewayTransportException("Local Codex protocol mismatch")
            }
            thread.arrayValue("turns").orEmpty().mapNotNull { it.asObject() }
                .lastOrNull { it.stringValue("status") == "inProgress" }
                ?.stringValue("id") ?: return@withLock
        }
        ensureOpen(expectedSessionScope, expectedSessionGeneration)
        client.request("turn/interrupt", buildJsonObject {
            put("threadId", threadKey.value)
            put("turnId", turnId)
        })
        // Do not manufacture a completed/interrupted event from the empty response.
        Unit
    }

    override suspend fun performAction(actionRef: String, expectedSessionScope: String) {
        ensureOpen(expectedSessionScope)
        throw GatewayTransportException("Action is unavailable")
    }

    override suspend fun readOpenAiAccount(refreshToken: Boolean): OpenAiAccountState {
        ensureOpen()
        val result = client.request(
            "account/read",
            buildJsonObject { put("refreshToken", refreshToken) },
        )
        val account = result.objectValue("account")
        return OpenAiAccountState(
            signedIn = account?.stringValue("type") == "chatgpt",
            planType = account?.stringValue("planType")?.take(32),
        )
    }

    override suspend fun startOpenAiDeviceLogin(): OpenAiDeviceLogin {
        ensureOpen()
        val result = client.request(
            "account/login/start",
            buildJsonObject { put("type", "chatgptDeviceCode") },
        )
        if (result.stringValue("type") != "chatgptDeviceCode") {
            throw GatewayTransportException("Local Codex protocol mismatch")
        }
        val loginId = result.requiredString("loginId")
        val verificationUrl = result.requiredString("verificationUrl")
        val userCode = result.requiredString("userCode")
        val uri = runCatching { URI(verificationUrl) }.getOrNull()
        if (
            uri?.scheme != "https" || uri.host?.lowercase() != "auth.openai.com" ||
            uri.rawUserInfo != null || uri.rawFragment != null || userCode.length !in 4..32 ||
            userCode.any { !it.isLetterOrDigit() && it != '-' }
        ) {
            throw GatewayTransportException("Local Codex protocol mismatch")
        }
        return OpenAiDeviceLogin(loginId, verificationUrl, userCode)
    }

    override suspend fun cancelOpenAiDeviceLogin(loginId: String) {
        ensureOpen()
        if (loginId.length !in 8..128 || loginId.any { it.isISOControl() }) {
            throw GatewayTransportException("Local Codex protocol mismatch")
        }
        client.request(
            "account/login/cancel",
            buildJsonObject { put("loginId", loginId) },
        )
    }

    override suspend fun logoutOpenAiAccount() {
        ensureOpen()
        client.request("account/logout")
    }

    override fun applySavedConfiguration(): Boolean {
        if (configurationMutex.isLocked || !configurationChangeIsSafe()) return false
        client.requestReconnect()
        return true
    }

    override suspend fun changeConfigurationWhenIdle(change: suspend () -> String?): String? =
        configurationMutex.withLock {
            if (!configurationChangeIsSafe()) {
                return@withLock "当前仍有任务在进行，请结束任务后再重新建立安全配置。"
            }
            try { change() }
            finally {
                // Even a partial recovery may have published a tombstone. End the old
                // app-server process so it cannot keep using its captured credentials.
                client.requestReconnect()
            }
        }

    private fun configurationChangeIsSafe(): Boolean =
        !closed.get() && activeTurns.isEmpty() && taskResponsePendingClientIds.isEmpty() &&
            _threads.value.none { it.state.isActiveTurn() }

    override fun close() {
        if (!closed.compareAndSet(false, true)) return
        client.close()
        sourceMediaByUploadRef.clear()
        sourceMediaByClientId.clear()
        scope.cancel()
    }

    private suspend fun ensureResumed(threadKey: ThreadKey) {
        if (resumedThreads.contains(threadKey)) return
        val result = client.request("thread/resume", threadIdParams(threadKey))
        val thread = result.objectValue("thread")
            ?: throw GatewayTransportException("Local Codex protocol mismatch")
        if (thread.requiredString("id") != threadKey.value) {
            throw GatewayTransportException("Local Codex protocol mismatch")
        }
        applyOfficialThread(
            thread,
            model = result.stringValue("model"),
            reasoningEffort = result.stringValue("reasoningEffort"),
        )
        resumedThreads += threadKey
    }

    private fun applyOfficialThread(
        value: JsonObject,
        model: String? = null,
        reasoningEffort: String? = null,
    ) {
        val prior = _threads.value.firstOrNull { it.key.value == value.stringValue("id") }
        val thread = parseThread(value).copy(
            model = model ?: prior?.model,
            reasoningEffort = reasoningEffort ?: prior?.reasoningEffort,
        )
        if (thread.state.isActiveTurn()) {
            thread.lastTurnId?.let { activeTurns[thread.key] = it }
        } else {
            activeTurns.remove(thread.key)
        }
        _threads.value = _threads.value.filterNot { it.key == thread.key } + thread
    }

    private suspend fun refreshThreads() {
        val displayBeforeRead = _threads.value.associateBy(CodexThread::key)
        val accumulated = ArrayList<CodexThread>()
        var cursor: String? = null
        var page = 0
        do {
            val result = client.request(
                "thread/list",
                buildJsonObject {
                    put("cursor", cursor)
                    put("archived", false)
                    put("limit", THREAD_PAGE_SIZE)
                    put("sortKey", "updated_at")
                    put("sortDirection", "desc")
                    // Let the official app-server reconcile its state database with rollout
                    // files. A persisted thread can legitimately exist before its metadata has
                    // reached the state DB; hiding it here makes a cold start look like a blank
                    // new conversation even though thread/read can restore it.
                    put("useStateDbOnly", false)
                    put("sourceKinds", buildJsonArray {
                        THREAD_SOURCE_KINDS.forEach { add(JsonPrimitive(it)) }
                    })
                },
            )
            result.arrayValue("data").orEmpty().mapNotNullTo(accumulated) { element ->
                element.asObject()?.let(::parseThread)?.let { parsed ->
                    val existing = displayBeforeRead[parsed.key]
                    parsed.copy(
                        model = existing?.model,
                        reasoningEffort = existing?.reasoningEffort,
                    )
                }
            }
            cursor = result.stringValue("nextCursor")
            page += 1
        } while (cursor != null && page < MAX_THREAD_PAGES)
        // An older catalog response must not overwrite newer official events already
        // rendered while the request was in flight. This merge only orders UI updates.
        val authoritativeKeys = accumulated.mapTo(HashSet(), CodexThread::key)
        authoritativeKeys.forEach(provisionalThreads::remove)
        _threads.update { current ->
            val newerDisplay = current.filter { displayBeforeRead[it.key] != it }
            val provisional = provisionalThreads.values.map { pending ->
                current.firstOrNull { it.key == pending.key } ?: pending
            }.sortedByDescending(CodexThread::updatedAtMillis)
            (newerDisplay + provisional + accumulated).distinctBy(CodexThread::key).take(MAX_THREADS)
        }
    }

    private fun handleNotification(method: String, params: JsonObject) {
        if (closed.get()) return
        recordTurnActivity(params)
        when (method) {
            "turn/started" -> handleTurnStarted(params)
            "turn/completed" -> handleTurnCompleted(params)
            "error" -> {
                val threadKey = params.stringValue("threadId")?.let(::ThreadKey)
                val turnId = params.stringValue("turnId")
                if (threadKey != null && turnId != null && activeTurns[threadKey] == turnId) {
                    showRuntimeNotice(threadKey, turnId, "服务响应异常，正在等待服务恢复或本轮结束。你可以停止本轮；请勿重复发送。")
                }
            }
            "item/started" -> handleItem(params, completed = false)
            "item/completed" -> handleItem(params, completed = true)
            "item/agentMessage/delta", "item/plan/delta", "item/reasoning/summaryTextDelta" ->
                handleTextDelta(params)
            "thread/archived", "thread/deleted" -> {
                params.stringValue("threadId")?.let { removeThread(ThreadKey(it)) }
            }
            "thread/name/updated", "thread/status/changed", "thread/unarchived" -> {
                scope.launch { runCatching { refreshThreads() } }
            }
        }
    }

    private fun handleTurnStarted(params: JsonObject) {
        val threadKey = params.stringValue("threadId")?.let(::ThreadKey) ?: return
        val turnId = params.objectValue("turn")?.stringValue("id") ?: return
        activeTurns[threadKey] = turnId
        startTurnWatchdog(threadKey, turnId)
        updateThread(threadKey, ThreadRunState.RUNNING, lastTurnId = turnId)
        emit(threadKey) { streamId, seq ->
            ConversationEvent.TurnLifecycle(
                nextEventId(), streamId, seq, threadKey, turnId, TurnLifecyclePhase.STARTED,
            )
        }
    }

    private fun handleTurnCompleted(params: JsonObject) {
        val threadKey = params.stringValue("threadId")?.let(::ThreadKey) ?: return
        val turn = params.objectValue("turn") ?: return
        val turnId = turn.stringValue("id") ?: return
        val status = turn.stringValue("status") ?: "failed"
        val phase = when (status) {
            "completed" -> TurnLifecyclePhase.COMPLETED
            "interrupted" -> TurnLifecyclePhase.INTERRUPTED
            else -> TurnLifecyclePhase.FAILED
        }
        activeTurns.remove(threadKey, turnId)
        stopTurnWatchdog(turnId)
        showRuntimeNotice(threadKey, turnId, when (phase) {
            TurnLifecyclePhase.COMPLETED -> "本轮已完成。"
            TurnLifecyclePhase.INTERRUPTED -> "本轮已停止。"
            else -> "本轮执行失败，请检查连接后手动重试。"
        }, createIfMissing = false)
        updateThread(
            threadKey,
            if (phase == TurnLifecyclePhase.COMPLETED) ThreadRunState.IDLE else ThreadRunState.FAILED,
            lastTurnId = turnId,
        )
        emit(threadKey) { streamId, seq ->
            ConversationEvent.TurnLifecycle(nextEventId(), streamId, seq, threadKey, turnId, phase)
        }
        imageStore?.release(turnId)
        scope.launch { runCatching { refreshThreads() } }
    }

    private fun recordTurnActivity(params: JsonObject) {
        val turnId = params.stringValue("turnId")
            ?: params.objectValue("turn")?.stringValue("id")
            ?: return
        if (activeTurns.containsValue(turnId)) {
            turnLastActivityMillis[turnId] = monotonicMillis()
        }
    }

    private fun startTurnWatchdog(threadKey: ThreadKey, turnId: String) {
        turnLastActivityMillis.putIfAbsent(turnId, monotonicMillis())
        if (turnWatchdogs[turnId]?.isActive == true) return
        val job = scope.launch {
            var warned = false
            while (!closed.get() && activeTurns[threadKey] == turnId) {
                val quietMillis = monotonicMillis() -
                    (turnLastActivityMillis[turnId] ?: monotonicMillis())
                if (!warned && quietMillis >= 30_000L) {
                    warned = true
                    showRuntimeNotice(threadKey, turnId, "已收到任务，但最近30秒没有新的执行进展，仍在等待服务响应。你可以停止本轮；请勿重复发送。")
                    return@launch
                }
                delay(1_000L)
            }
        }
        turnWatchdogs.put(turnId, job)?.cancel()
    }

    @Synchronized
    private fun showRuntimeNotice(threadKey: ThreadKey, turnId: String, text: String, createIfMissing: Boolean = true) {
        // One presentation-only notice per official turn. Patching it must never
        // drive execution, reconnects, or delivery decisions.
        val id = "runtime-$turnId"
        val existing = ensureState(threadKey).messages.firstOrNull { it.id == id }
        if (existing != null) {
            val block = TextBlock("$id-text", text, markdown = true)
            if (existing.blocks[block.id] == block) return
            emit(threadKey) { streamId, seq ->
                ConversationEvent.MessagePatched(
                    nextEventId(), streamId, seq, id, existing.revision,
                    existing.revision + 1, listOf(PatchOperation.PutBlock(block)),
                )
            }
            return
        }
        if (!createIfMissing) return
        val message = textMessage(
            id = id, role = MessageRole.SYSTEM,
            text = text, state = RichMessageState.COMPLETED,
            createdAtMillis = System.currentTimeMillis(), turnId = turnId,
        )
        emit(threadKey) { streamId, seq ->
            ConversationEvent.MessageAdded(nextEventId(), streamId, seq, message)
        }
    }

    private fun stopTurnWatchdog(turnId: String) {
        turnLastActivityMillis.remove(turnId)
        turnWatchdogs.remove(turnId)?.cancel()
    }

    private fun handleItem(params: JsonObject, completed: Boolean) {
        val threadKey = params.stringValue("threadId")?.let(::ThreadKey) ?: return
        val turnId = params.stringValue("turnId") ?: return
        val item = params.objectValue("item") ?: return
        if (
            item.stringValue("type") == "userMessage" &&
            item.stringValue("clientId")?.let(taskResponsePendingClientIds::remove) == true
        ) Unit
        val createdAt = (
            params.longValue(if (completed) "completedAtMs" else "startedAtMs")
                ?: System.currentTimeMillis()
            ).coerceAtLeast(0L)
        val projected = projectItem(item, turnId, createdAt) ?: return
        val existing = synchronized(this) {
            ensureState(threadKey).messages.firstOrNull { it.id == projected.id }
        }
        if (existing == null) {
            emit(threadKey) { streamId, seq ->
                ConversationEvent.MessageAdded(nextEventId(), streamId, seq, projected)
            }
            return
        }
        if (!completed) return
        val projectedBlocks = projected.rootBlockIds.mapNotNull(projected.blocks::get)
        if (projectedBlocks.isEmpty()) return
        emit(threadKey) { streamId, seq ->
            ConversationEvent.MessagePatched(
                eventId = nextEventId(),
                streamId = streamId,
                streamSeq = seq,
                messageId = existing.id,
                baseRevision = existing.revision,
                revision = existing.revision + 1,
                operations = buildList {
                    projectedBlocks.forEach { add(PatchOperation.PutBlock(it)) }
                    add(PatchOperation.SetRootBlocks(projected.rootBlockIds))
                    add(PatchOperation.SetMessageState(projected.state))
                    if (existing.presentation != projected.presentation) {
                        add(PatchOperation.SetMessagePresentation(projected.presentation))
                    }
                },
            )
        }
    }

    /** Applies the app-server's native text deltas without imposing an output schema. */
    private fun handleTextDelta(params: JsonObject) {
        val threadKey = params.stringValue("threadId")?.let(::ThreadKey) ?: return
        val messageId = params.stringValue("itemId") ?: return
        val turnId = params.stringValue("turnId") ?: return
        val delta = params.stringValue("delta")?.take(MAX_DELTA_CHARS) ?: return
        if (delta.isEmpty()) return
        val existing = synchronized(this) {
            ensureState(threadKey).messages.firstOrNull { it.id == messageId }
        }
        if (existing == null) {
            val message = nativeTextItem(messageId, delta, System.currentTimeMillis(), turnId) ?: return
            emit(threadKey) { streamId, seq ->
                ConversationEvent.MessageAdded(nextEventId(), streamId, seq, message)
            }
            return
        }
        val blockId = existing.rootBlockIds.firstOrNull() ?: return
        emit(threadKey) { streamId, seq ->
            ConversationEvent.MessagePatched(
                eventId = nextEventId(),
                streamId = streamId,
                streamSeq = seq,
                messageId = messageId,
                baseRevision = existing.revision,
                revision = existing.revision + 1,
                operations = listOf(PatchOperation.AppendBlockText(blockId, delta)),
            )
        }
    }

    private fun projectItem(item: JsonObject, turnId: String, createdAtMillis: Long): RichMessage? {
        val itemId = item.stringValue("id") ?: return null
        return when (item.stringValue("type")) {
            "userMessage" -> {
                val content = item.arrayValue("content").orEmpty().mapNotNull { it.asObject() }
                val clientMessageId = item.stringValue("clientId")
                val sourceMediaIds = clientMessageId?.let(sourceMediaByClientId::get).orEmpty()
                val textParts = content.mapNotNull { value ->
                    if (value.stringValue("type") == "text" && !isImageAttachment(value)) {
                        value.stringValue("text")
                    } else null
                }
                val imageCount = content.count { value ->
                    value.stringValue("type") in setOf("image", "localImage", "inputImage") ||
                        isImageAttachment(value)
                }
                val text = buildList {
                    addAll(textParts)
                    if (sourceMediaIds.isEmpty() && imageCount > 0) add("[图片 × $imageCount]")
                }.joinToString("\n").take(MAX_RENDERED_TEXT_CHARS)
                userMessage(
                    id = itemId,
                    text = text,
                    createdAtMillis = createdAtMillis,
                    clientMessageId = clientMessageId,
                    turnId = turnId,
                    sourceMediaIds = sourceMediaIds,
                )
            }
            "agentMessage" -> nativeTextItem(
                id = itemId,
                text = item.stringValue("text").orEmpty().take(MAX_RENDERED_TEXT_CHARS),
                createdAtMillis = createdAtMillis,
                turnId = turnId,
                presentation = when (item.stringValue("phase")) {
                    "commentary" -> MessagePresentation.PROCESS
                    "final_answer" -> MessagePresentation.FINAL_ANSWER
                    else -> MessagePresentation.FINAL_ANSWER
                },
            )
            "plan" -> nativeTextItem(
                id = itemId,
                text = item.stringValue("text").orEmpty().take(MAX_RENDERED_TEXT_CHARS),
                createdAtMillis = createdAtMillis,
                turnId = turnId,
                presentation = MessagePresentation.PROCESS,
            )
            "reasoning" -> nativeTextItem(
                id = itemId,
                text = item.arrayValue("summary").orEmpty()
                    .mapNotNull { (it as? JsonPrimitive)?.contentOrNull }
                    .joinToString("\n\n")
                    .take(MAX_RENDERED_TEXT_CHARS),
                createdAtMillis = createdAtMillis,
                turnId = turnId,
                presentation = MessagePresentation.PROCESS,
            )
            "commandExecution" -> toolMessage(
                id = itemId,
                label = "本地命令",
                detail = statusLabel(item.stringValue("status")),
                state = itemState(item.stringValue("status")),
                createdAtMillis = createdAtMillis,
                turnId = turnId,
            )
            "mcpToolCall" -> mcpToolMessage(item, itemId, turnId, createdAtMillis)
            "fileChange" -> toolMessage(
                id = itemId,
                label = "文件变更",
                detail = statusLabel(item.stringValue("status")),
                state = itemState(item.stringValue("status")),
                createdAtMillis = createdAtMillis,
                turnId = turnId,
            )
            "functionCallOutput" -> nativeToolItem(itemId, "函数结果", item, turnId, createdAtMillis)
            "dynamicToolCall" -> nativeToolItem(itemId, "工具调用", item, turnId, createdAtMillis)
            "collabAgentToolCall" -> nativeToolItem(itemId, "协作任务", item, turnId, createdAtMillis)
            "subAgentActivity" -> nativeToolItem(itemId, "协作进展", item, turnId, createdAtMillis)
            "webSearch" -> nativeToolItem(itemId, "网络搜索", item, turnId, createdAtMillis)
            "imageView" -> nativeToolItem(itemId, "查看图片", item, turnId, createdAtMillis)
            "sleep" -> nativeToolItem(itemId, "等待", item, turnId, createdAtMillis)
            "imageGeneration" -> nativeToolItem(itemId, "生成图片", item, turnId, createdAtMillis)
            "enteredReviewMode" -> nativeToolItem(itemId, "开始代码审查", item, turnId, createdAtMillis)
            "exitedReviewMode" -> nativeToolItem(itemId, "完成代码审查", item, turnId, createdAtMillis)
            "contextCompaction" -> nativeToolItem(itemId, "已整理上下文", item, turnId, createdAtMillis)
            "hookPrompt" -> nativeToolItem(itemId, "Codex 扩展", item, turnId, createdAtMillis)
            else -> unknownNativeItem(itemId, item.stringValue("type"), turnId, createdAtMillis)
        }
    }

    private fun textMessage(
        id: String,
        role: MessageRole,
        text: String,
        state: RichMessageState,
        createdAtMillis: Long,
        clientMessageId: String? = null,
        turnId: String? = null,
        presentation: MessagePresentation = MessagePresentation.STANDARD,
    ): RichMessage {
        val block = TextBlock("$id-text", text, markdown = role != MessageRole.USER)
        return RichMessage(
            id = id,
            role = role,
            state = state,
            revision = 0,
            rootBlockIds = listOf(block.id),
            blocks = mapOf(block.id to block),
            createdAtMillis = createdAtMillis,
            clientMessageId = clientMessageId,
            turnId = turnId,
            presentation = presentation,
        )
    }

    private fun userMessage(
        id: String,
        text: String,
        createdAtMillis: Long,
        clientMessageId: String?,
        turnId: String,
        sourceMediaIds: List<String>,
    ): RichMessage {
        val blocks = buildList<RichBlock> {
            if (text.isNotBlank()) add(TextBlock("$id-text", text, markdown = false))
            sourceMediaIds.take(LOCAL_IMAGE_CAPABILITY.maxImagesPerTurn).forEachIndexed { index, assetId ->
                add(
                    MediaBlock(
                        id = "$id-source-${index + 1}",
                        assetId = assetId,
                        caption = "原始图片 ${index + 1}",
                        fallbackText = "原始图片暂不可用",
                    ),
                )
            }
        }
        return RichMessage(
            id = id,
            role = MessageRole.USER,
            state = RichMessageState.COMPLETED,
            revision = 0,
            rootBlockIds = blocks.map(RichBlock::id),
            blocks = blocks.associateBy(RichBlock::id),
            createdAtMillis = createdAtMillis,
            clientMessageId = clientMessageId,
            turnId = turnId,
        )
    }

    private fun nativeTextItem(
        id: String,
        text: String,
        createdAtMillis: Long,
        turnId: String,
        presentation: MessagePresentation = MessagePresentation.PROCESS,
    ): RichMessage? = text.takeIf(String::isNotBlank)?.let { visibleText ->
        textMessage(
            id = id,
            role = MessageRole.ASSISTANT,
            text = visibleText,
            state = RichMessageState.COMPLETED,
            createdAtMillis = createdAtMillis,
            turnId = turnId,
            presentation = presentation,
        )
    }

    private fun nativeToolItem(
        id: String,
        label: String,
        item: JsonObject,
        turnId: String,
        createdAtMillis: Long,
    ): RichMessage = toolMessage(
        id = id,
        label = label,
        detail = statusLabel(item.stringValue("status")),
        state = itemState(item.stringValue("status")),
        createdAtMillis = createdAtMillis,
        turnId = turnId,
    )

    private fun unknownNativeItem(
        id: String,
        type: String?,
        turnId: String,
        createdAtMillis: Long,
    ): RichMessage {
        val safeType = type.orEmpty().take(MAX_TOOL_LABEL_CHARS)
            .filter { it.isLetterOrDigit() || it in "_-" }
            .ifBlank { "unknown" }
        val block = UnknownBlock(
            id = "$id-unknown",
            originalType = safeType,
            fallbackText = "Codex 正在处理该消息",
        )
        return RichMessage(
            id = id,
            role = MessageRole.ASSISTANT,
            state = RichMessageState.COMPLETED,
            revision = 0,
            rootBlockIds = listOf(block.id),
            blocks = mapOf(block.id to block),
            createdAtMillis = createdAtMillis,
            turnId = turnId,
            presentation = MessagePresentation.PROCESS,
        )
    }

    private fun toolMessage(
        id: String,
        label: String,
        detail: String,
        state: RichMessageState,
        createdAtMillis: Long,
        turnId: String,
    ): RichMessage {
        val block = ToolBlock(
            id = "$id-tool",
            label = label,
            detail = detail,
            state = when (state) {
                RichMessageState.COMPLETED -> StepState.COMPLETED
                RichMessageState.FAILED -> StepState.FAILED
                else -> StepState.ACTIVE
            },
        )
        return RichMessage(
            id = id,
            role = MessageRole.ASSISTANT,
            state = state,
            revision = 0,
            rootBlockIds = listOf(block.id),
            blocks = mapOf(block.id to block),
            createdAtMillis = createdAtMillis,
            turnId = turnId,
            presentation = MessagePresentation.PROCESS,
        )
    }

    private fun mcpToolMessage(
        item: JsonObject,
        id: String,
        turnId: String,
        createdAtMillis: Long,
    ): RichMessage {
        val state = itemState(item.stringValue("status"))
        val toolName = item.stringValue("tool")?.take(MAX_TOOL_LABEL_CHARS)
        val toolBlock = ToolBlock(
            id = "$id-tool",
            label = "手机工具",
            detail = listOfNotNull(toolName, statusLabel(item.stringValue("status"))).joinToString(" · "),
            state = when (state) {
                RichMessageState.COMPLETED -> StepState.COMPLETED
                RichMessageState.FAILED -> StepState.FAILED
                else -> StepState.ACTIVE
            },
        )
        val mediaBlocks = projectMcpImages(item, id, toolName)
        val blocks = buildMap<String, com.cheby.codex.mobile.model.RichBlock> {
            put(toolBlock.id, toolBlock)
            mediaBlocks.forEach { put(it.id, it) }
        }
        return RichMessage(
            id = id,
            role = MessageRole.ASSISTANT,
            state = state,
            revision = 0,
            rootBlockIds = listOf(toolBlock.id) + mediaBlocks.map(MediaBlock::id),
            blocks = blocks,
            createdAtMillis = createdAtMillis,
            turnId = turnId,
            presentation = MessagePresentation.PROCESS,
        )
    }

    private fun projectMcpImages(item: JsonObject, itemId: String, toolName: String?): List<MediaBlock> {
        val content = item.objectValue("result")?.arrayValue("content").orEmpty()
        return content.asSequence().mapNotNull { element ->
            val image = element.asObject()?.takeIf { it.stringValue("type") == "image" }
                ?: return@mapNotNull null
            val mediaType = image.stringValue("mimeType") ?: return@mapNotNull null
            val encoded = image.stringValue("data") ?: return@mapNotNull null
            if (encoded.length !in 4..MAX_MCP_IMAGE_BASE64_CHARS) return@mapNotNull null
            val decoded = runCatching { Base64.getDecoder().decode(encoded) }.getOrNull()
                ?: return@mapNotNull null
            try {
                val assetId = EphemeralMediaStore.put(mediaType, decoded) ?: return@mapNotNull null
                MediaBlock(
                    id = "$itemId-media-${assetId.takeLast(12)}",
                    assetId = assetId,
                    caption = if (toolName == "android_capture_screenshot") "手机截图" else "手机工具返回的图片",
                    fallbackText = "图片已返回",
                )
            } finally {
                decoded.fill(0)
            }
        }.take(MAX_MCP_IMAGES_PER_ITEM).toList()
    }

    private fun parseThread(value: JsonObject): CodexThread {
        val key = ThreadKey(value.requiredString("id"))
        val preview = value.stringValue("preview").orEmpty()
            .replace(Regex("\\s+"), " ")
            .take(MAX_PREVIEW_CHARS)
        val title = value.stringValue("name")
            ?.trim()
            ?.take(MAX_TITLE_CHARS)
            ?.takeIf(String::isNotBlank)
            ?: preview.take(MAX_TITLE_CHARS).ifBlank { "新会话" }
        val status = value.objectValue("status")
        val activeFlags = status?.arrayValue("activeFlags").orEmpty().mapNotNull {
            (it as? JsonPrimitive)?.contentOrNull
        }
        val state = when (status?.stringValue("type")) {
            "active" -> when {
                "waitingOnApproval" in activeFlags -> ThreadRunState.WAITING_APPROVAL
                "waitingOnUserInput" in activeFlags -> ThreadRunState.WAITING_USER
                else -> ThreadRunState.RUNNING
            }
            "systemError" -> ThreadRunState.FAILED
            else -> ThreadRunState.IDLE
        }
        return CodexThread(
            key = key,
            title = title,
            preview = preview.ifBlank { "还没有消息" },
            updatedAtMillis = (value.longValue("updatedAt") ?: value.longValue("createdAt") ?: 0L)
                .coerceAtLeast(0L) * 1_000L,
            state = state,
            lastTurnId = value.arrayValue("turns").orEmpty().lastOrNull()
                ?.asObject()?.stringValue("id") ?: activeTurns[key],
        )
    }

    @Synchronized
    private fun emit(
        threadKey: ThreadKey,
        factory: (String, Long) -> ConversationEvent,
    ) {
        val current = ensureState(threadKey)
        val event = factory(current.streamId, current.streamSeq + 1)
        val reduced = ConversationReducer.reduce(current, event)
        if (reduced.resyncRequired) return
        states[threadKey] = reduced
        if (!eventBus.tryEmit(threadKey to event)) {
            val sync = ConversationEvent.SyncRequired(
                nextEventId(), current.streamId, reduced.streamSeq + 1,
            )
            eventBus.tryEmit(threadKey to sync)
        }
    }

    @Synchronized
    private fun ensureState(threadKey: ThreadKey): ThreadConversationState =
        states.getOrPut(threadKey) {
            ThreadConversationState(
                threadKey = threadKey,
                streamId = streams.getOrPut(threadKey) { "local-${UUID.randomUUID()}" },
            )
        }

    private fun updateThread(
        threadKey: ThreadKey,
        state: ThreadRunState,
        lastTurnId: String? = null,
    ) {
        val now = System.currentTimeMillis()
        _threads.update { current -> current.map { thread ->
            if (thread.key == threadKey) {
                thread.copy(state = state, updatedAtMillis = now, lastTurnId = lastTurnId ?: thread.lastTurnId)
            } else {
                thread
            }
        }.sortedByDescending(CodexThread::updatedAtMillis) }
    }

    private fun removeThread(threadKey: ThreadKey) {
        _threads.value = _threads.value.filterNot { it.key == threadKey }
        states.remove(threadKey)
        streams.remove(threadKey)
        resumedThreads.remove(threadKey)
        provisionalThreads.remove(threadKey)
        activeTurns.remove(threadKey)
    }

    private fun ensureOpen(
        expectedSessionScope: String = sessionScope,
        expectedSessionGeneration: Long = sessionIdentity.value.generation,
    ) {
        if (
            closed.get() ||
            expectedSessionScope != sessionScope ||
            expectedSessionGeneration != sessionIdentity.value.generation
        ) {
            throw GatewayTransportException("Gateway session changed")
        }
    }

    private fun nextEventId(): String = "local-event-${eventCounter.incrementAndGet()}"

    private fun monotonicMillis(): Long = System.nanoTime() / 1_000_000L

    private fun isImageAttachment(value: JsonObject): Boolean = when (value.stringValue("type")) {
        "localImage" -> value.stringValue("path")?.let(LOCAL_IMAGE_PATH::matches) == true
        // Keep older on-device histories readable after upgrading from the
        // temporary path-as-text implementation.
        "text" -> value.stringValue("text")?.let(IMAGE_ATTACHMENT_TEXT::matches) == true
        else -> false
    }

    private fun threadIdParams(threadKey: ThreadKey) = buildJsonObject {
        put("threadId", threadKey.value)
    }

    private fun statusLabel(value: String?): String = when (value) {
        "completed" -> "已完成"
        "failed", "declined" -> "失败"
        else -> "执行中"
    }

    private fun itemState(value: String?): RichMessageState = when (value) {
        "completed" -> RichMessageState.COMPLETED
        "failed", "declined" -> RichMessageState.FAILED
        else -> RichMessageState.STREAMING
    }

    private fun ThreadRunState.isActiveTurn(): Boolean =
        this == ThreadRunState.RUNNING ||
            this == ThreadRunState.WAITING_USER ||
            this == ThreadRunState.WAITING_APPROVAL

    private fun JsonObject.requiredString(key: String): String =
        stringValue(key)?.takeIf(String::isNotBlank)
            ?: throw GatewayTransportException("Local Codex protocol mismatch")

    private fun JsonObject.stringValue(key: String): String? =
        (get(key) as? JsonPrimitive)?.contentOrNull

    private fun JsonObject.longValue(key: String): Long? =
        (get(key) as? JsonPrimitive)?.longOrNull

    private fun JsonObject.objectValue(key: String): JsonObject? = get(key)?.asObject()

    private fun JsonObject.arrayValue(key: String): List<JsonElement>? =
        (get(key) as? JsonArray)?.toList()

    private fun JsonElement.asObject(): JsonObject? = runCatching { jsonObject }.getOrNull()

    private companion object {
        const val LOCAL_SESSION_SCOPE = "standalone-local-v1"
        const val LOCAL_WORKDIR = "/root"
        val LOCAL_IMAGE_PATH = Regex(
            "/root/\\.cheby/turn-inputs/[a-f0-9]{32}/[a-f0-9]{32}/img_[a-f0-9]{64}\\.(png|jpg)",
        )
        // Recognize path-as-text records from pre-native-image builds without
        // preserving that obsolete routing instruction in current prompts.
        val IMAGE_ATTACHMENT_TEXT = Regex(
            "Attached image file \\([^\\r\\n]{1,96}\\): " +
                LOCAL_IMAGE_PATH.pattern,
        )
        const val THREAD_PAGE_SIZE = 50
        const val MAX_THREAD_PAGES = 4
        const val MAX_THREADS = 200
        const val MAX_MODELS = 100
        val THREAD_SOURCE_KINDS = listOf(
            "cli",
            "vscode",
            "exec",
            "appServer",
            "subAgent",
            "subAgentReview",
            "subAgentCompact",
            "subAgentThreadSpawn",
            "subAgentOther",
            "unknown",
        )
        const val MAX_TITLE_CHARS = 120
        const val MAX_PREVIEW_CHARS = 240
        const val MAX_TURN_CHARS = 100_000
        const val MAX_TURN_INPUT_PARTS = 11
        const val MAX_RENDERED_TEXT_CHARS = 512_000
        const val MAX_DELTA_CHARS = 32_000
        const val MAX_TOOL_LABEL_CHARS = 80
        const val MAX_MCP_IMAGES_PER_ITEM = 4
        const val MAX_MCP_IMAGE_BASE64_CHARS = ((EphemeralMediaStore.MAX_ASSET_BYTES + 2) / 3) * 4
        const val MAX_SNAPSHOT_MESSAGES = 1_000
        const val EVENT_REPLAY = 512
        const val EVENT_BUFFER = 512
        val LOCAL_IMAGE_CAPABILITY = LocalImageCapability(
            uploadVersion = 1,
            mediaTypes = setOf("image/jpeg", "image/png"),
            maxUploadBytes = 8 * 1024 * 1024,
            maxPixels = 25_000_000,
            maxEdgePixels = 12_000,
            maxImagesPerTurn = 10,
        )
    }
}
