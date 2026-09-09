package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.model.ActionItem
import com.cheby.codex.mobile.model.ActionKind
import com.cheby.codex.mobile.model.ActionsBlock
import com.cheby.codex.mobile.model.CodexThread
import com.cheby.codex.mobile.model.ConversationEvent
import com.cheby.codex.mobile.model.ConversationReducer
import com.cheby.codex.mobile.model.ConversationSnapshot
import com.cheby.codex.mobile.model.FactItem
import com.cheby.codex.mobile.model.FactsBlock
import com.cheby.codex.mobile.model.DiffBlock
import com.cheby.codex.mobile.model.FileBlock
import com.cheby.codex.mobile.model.FileChangeKind
import com.cheby.codex.mobile.model.GatewayConnectionState
import com.cheby.codex.mobile.model.MessageRole
import com.cheby.codex.mobile.model.MetricItem
import com.cheby.codex.mobile.model.MetricsBlock
import com.cheby.codex.mobile.model.NoticeBlock
import com.cheby.codex.mobile.model.PatchOperation
import com.cheby.codex.mobile.model.RelayBlock
import com.cheby.codex.mobile.model.RelayNode
import com.cheby.codex.mobile.model.RichMessage
import com.cheby.codex.mobile.model.RichMessageState
import com.cheby.codex.mobile.model.SourceBlock
import com.cheby.codex.mobile.model.StatusBlock
import com.cheby.codex.mobile.model.StepItem
import com.cheby.codex.mobile.model.StepState
import com.cheby.codex.mobile.model.StepsBlock
import com.cheby.codex.mobile.model.TextBlock
import com.cheby.codex.mobile.model.TerminalBlock
import com.cheby.codex.mobile.model.TestBlock
import com.cheby.codex.mobile.model.ThreadConversationState
import com.cheby.codex.mobile.model.ThreadKey
import com.cheby.codex.mobile.model.ThreadRunState
import com.cheby.codex.mobile.model.Tone
import com.cheby.codex.mobile.model.ToolBlock
import com.cheby.codex.mobile.model.TurnLifecyclePhase
import com.cheby.codex.mobile.model.UnknownBlock
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.filter
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.launch

class FakeCodexGateway : CodexGateway, DemoGatewayControls, CloseableCodexGateway {
    override val sessionScope: String = "demo"
    override val kind: StateFlow<GatewayKind> = MutableStateFlow(GatewayKind.DEMO)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val eventBus = MutableSharedFlow<Pair<ThreadKey, ConversationEvent>>(
        replay = 128,
        extraBufferCapacity = 64,
    )
    private val states = ConcurrentHashMap<ThreadKey, ThreadConversationState>()
    private val streamIds = ConcurrentHashMap<ThreadKey, String>()
    private val idempotencyKeys = ConcurrentHashMap.newKeySet<String>()
    private val turnSubmissions = ConcurrentHashMap<String, TurnSubmission>()
    private val pendingRetries = ConcurrentHashMap<String, PendingTurn>()
    private val threadCounter = AtomicInteger(2)
    private val eventCounter = AtomicInteger(0)
    private val closed = AtomicBoolean(false)

    private val _connectionState = MutableStateFlow(GatewayConnectionState.ONLINE)
    override val connectionState: StateFlow<GatewayConnectionState> = _connectionState.asStateFlow()

    private val _threads = MutableStateFlow(initialThreads())
    override val threads: StateFlow<List<CodexThread>> = _threads.asStateFlow()

    init {
        _threads.value.forEach { thread ->
            val streamId = newStreamId()
            streamIds[thread.key] = streamId
            val welcome = welcomeMessage(thread.title)
            states[thread.key] = ThreadConversationState(
                threadKey = thread.key,
                streamId = streamId,
                streamSeq = 1,
                messages = listOf(welcome),
            )
        }
    }

    override suspend fun createThread(title: String): CodexThread {
        val now = System.currentTimeMillis()
        val thread = CodexThread(
            key = ThreadKey("local-${UUID.randomUUID()}"),
            title = title.ifBlank { "新会话 ${threadCounter.incrementAndGet()}" },
            preview = "还没有消息",
            updatedAtMillis = now,
        )
        _threads.value = listOf(thread) + _threads.value
        val streamId = newStreamId()
        streamIds[thread.key] = streamId
        states[thread.key] = ThreadConversationState(
            threadKey = thread.key,
            streamId = streamId,
            streamSeq = 1,
            messages = listOf(welcomeMessage(thread.title)),
        )
        return thread
    }

    override suspend fun renameThread(threadKey: ThreadKey, title: String) {
        if (title.isBlank()) return
        _threads.value = _threads.value.map { thread ->
            if (thread.key == threadKey) thread.copy(title = title.trim()) else thread
        }
    }

    override suspend fun archiveThread(threadKey: ThreadKey) {
        _threads.value = _threads.value.filterNot { it.key == threadKey }
    }

    override suspend fun deleteThread(threadKey: ThreadKey) {
        _threads.value = _threads.value.filterNot { it.key == threadKey }
        states.remove(threadKey)
        streamIds.remove(threadKey)
        pendingRetries.entries.removeAll { it.value.threadKey == threadKey }
    }

    override suspend fun loadConversation(threadKey: ThreadKey): ConversationSnapshot {
        val state = states[threadKey] ?: ThreadConversationState(threadKey)
        return ConversationSnapshot(
            threadKey = threadKey,
            streamId = state.streamId,
            cursor = state.streamSeq,
            messages = state.messages,
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

    override suspend fun sendTurn(
        threadKey: ThreadKey,
        text: String,
        idempotencyKey: String,
        expectedSessionScope: String,
    ): TurnSubmission {
        if (closed.get() || expectedSessionScope != sessionScope) {
            throw GatewayTransportException("Gateway session changed")
        }
        turnSubmissions[idempotencyKey]?.let { return it }
        if (text.isBlank() || !idempotencyKeys.add(idempotencyKey)) {
            throw GatewayTransportException("Turn input is invalid")
        }
        val turnId = "turn-${UUID.randomUUID()}"
        val submission = TurnSubmission(turnId, threadKey, idempotencyKey, "running")
        turnSubmissions[idempotencyKey] = submission

        emitAdded(threadKey, userMessage(text, idempotencyKey))
        emitTurnLifecycle(threadKey, turnId, TurnLifecyclePhase.STARTED)
        val assistantId = "assistant-${UUID.randomUUID()}"
        updateThread(threadKey, preview = text, state = ThreadRunState.RUNNING, lastTurnId = turnId)

        if (_connectionState.value == GatewayConnectionState.OFFLINE) {
            val actionRef = "retry-${UUID.randomUUID()}"
            pendingRetries[actionRef] = PendingTurn(threadKey, assistantId, idempotencyKey, turnId)
            emitAdded(threadKey, offlineMessage(assistantId, actionRef))
            updateThread(threadKey, preview = "消息等待重试", state = ThreadRunState.FAILED)
            return submission
        }

        emitAdded(threadKey, runningMessage(assistantId))
        scope.launch {
            if (text.trim() == EDGE_CASE_DEMO_PROMPT) {
                runEdgeCaseDemo(threadKey, assistantId, turnId)
            } else {
                runDemo(threadKey, assistantId, turnId)
            }
        }
        return submission
    }

    override suspend fun performAction(actionRef: String, expectedSessionScope: String) {
        if (closed.get() || expectedSessionScope != sessionScope) {
            throw GatewayTransportException("Gateway session changed")
        }
        if (_connectionState.value != GatewayConnectionState.ONLINE) return
        val pending = pendingRetries.remove(actionRef) ?: return

        patch(
            threadKey = pending.threadKey,
            messageId = pending.assistantMessageId,
            operations = listOf(
                PatchOperation.PutBlock(StatusBlock("status", "连接已恢复，正在继续", tone = Tone.INFO)),
                PatchOperation.PutBlock(initialSteps()),
                PatchOperation.PutBlock(initialRelay()),
                PatchOperation.RemoveBlock("offline"),
                PatchOperation.RemoveBlock("actions"),
                PatchOperation.SetRootBlocks(listOf("status", "steps", "relay")),
                PatchOperation.SetMessageState(RichMessageState.STREAMING),
            ),
        )
        updateThread(pending.threadKey, preview = "已恢复执行", state = ThreadRunState.RUNNING)
        scope.launch { runDemo(pending.threadKey, pending.assistantMessageId, pending.turnId) }
    }

    override fun setOnline(online: Boolean) {
        val target = if (online) GatewayConnectionState.ONLINE else GatewayConnectionState.OFFLINE
        val wasOffline = _connectionState.value == GatewayConnectionState.OFFLINE
        _connectionState.value = target
        if (online && wasOffline) {
            states.entries.forEach { entry ->
                val streamId = newStreamId()
                streamIds[entry.key] = streamId
                entry.setValue(
                    entry.value.copy(
                        streamId = streamId,
                        streamSeq = 0,
                        processedEvents = emptyMap(),
                        resyncRequired = false,
                    ),
                )
            }
        }
    }

    override fun close() {
        if (!closed.compareAndSet(false, true)) return
        _connectionState.value = GatewayConnectionState.OFFLINE
        scope.cancel()
    }

    private suspend fun runDemo(threadKey: ThreadKey, messageId: String, turnId: String) {
        delay(650)
        patch(
            threadKey,
            messageId,
            listOf(
                PatchOperation.PutBlock(StatusBlock("status", "正在调用所需工具", "同一张卡片持续更新", Tone.INFO, 0.45f)),
                PatchOperation.PutBlock(
                    initialSteps().copy(
                        items = listOf(
                            StepItem("understand", "理解目标与安全边界", StepState.COMPLETED),
                            StepItem("context", "读取相关上下文", StepState.COMPLETED),
                            StepItem("tools", "执行所需工具", StepState.ACTIVE),
                            StepItem("result", "核对并整理结果", StepState.PENDING),
                        ),
                    ),
                ),
                PatchOperation.PutBlock(
                    initialRelay().copy(
                        nodes = listOf(
                            RelayNode("request", "请求", state = StepState.COMPLETED),
                            RelayNode("codex", "Codex", state = StepState.COMPLETED),
                            RelayNode("tools", "工具", "正在执行", StepState.ACTIVE),
                            RelayNode("result", "结果", state = StepState.PENDING),
                        ),
                    ),
                ),
                PatchOperation.PutBlock(
                    ToolBlock(
                        id = "tool",
                        label = "结构化数据解析器",
                        detail = "正在生成本地界面演示数据",
                        state = StepState.ACTIVE,
                        fallbackText = "本地演示工具正在运行",
                    ),
                ),
                PatchOperation.PutBlock(
                    TerminalBlock(
                        id = "terminal",
                        title = "校验任务（演示）",
                        commandLabel = "演示检查",
                        output = "demo: 正在组装安全的富消息块…",
                        state = StepState.ACTIVE,
                        fallbackText = "本地演示检查正在运行",
                    ),
                ),
                PatchOperation.SetRootBlocks(listOf("status", "steps", "relay", "tool", "terminal")),
            ),
        )

        delay(850)
        patch(
            threadKey,
            messageId,
            listOf(
                PatchOperation.PutBlock(StatusBlock("status", "正在核对结果", tone = Tone.INFO, progress = 0.8f)),
                PatchOperation.PutBlock(
                    initialSteps().copy(
                        items = listOf(
                            StepItem("understand", "理解目标与安全边界", StepState.COMPLETED),
                            StepItem("context", "读取相关上下文", StepState.COMPLETED),
                            StepItem("tools", "执行所需工具", StepState.COMPLETED),
                            StepItem("result", "核对并整理结果", StepState.ACTIVE),
                        ),
                    ),
                ),
                PatchOperation.PutBlock(
                    initialRelay().copy(
                        nodes = listOf(
                            RelayNode("request", "请求", state = StepState.COMPLETED),
                            RelayNode("codex", "Codex", state = StepState.COMPLETED),
                            RelayNode("tools", "工具", state = StepState.COMPLETED),
                            RelayNode("result", "结果", "正在汇总", StepState.ACTIVE),
                        ),
                    ),
                ),
                PatchOperation.PutBlock(
                    ToolBlock(
                        id = "tool",
                        label = "结构化数据解析器",
                        detail = "已生成通用软件交付演示块",
                        state = StepState.COMPLETED,
                        durationLabel = "本地演示",
                        fallbackText = "本地演示工具已完成",
                    ),
                ),
                PatchOperation.PutBlock(
                    TerminalBlock(
                        id = "terminal",
                        title = "校验任务（演示）",
                        commandLabel = "演示检查",
                        output = "demo: rich blocks assembled\ndemo: fallback and limits applied",
                        state = StepState.COMPLETED,
                        fallbackText = "本地演示检查已完成",
                    ),
                ),
            ),
        )

        delay(850)
        patch(
            threadKey,
            messageId,
            listOf(
                PatchOperation.PutBlock(StatusBlock("status", "任务完成", "结果已通过协议校验", Tone.SUCCESS, 1f)),
                PatchOperation.PutBlock(
                    initialSteps().copy(items = initialSteps().items.map { it.copy(state = StepState.COMPLETED) }),
                ),
                PatchOperation.PutBlock(
                    completedRelayFixture(),
                ),
                PatchOperation.PutBlock(
                    MetricsBlock(
                        "metrics",
                        listOf(
                            MetricItem("步骤", "4", "项"),
                            MetricItem("工具", "2", "个"),
                            MetricItem("状态", "通过"),
                        ),
                    ),
                ),
                PatchOperation.PutBlock(
                    FactsBlock(
                        "facts",
                        listOf(
                            FactItem("会话", "沿用当前 Codex Thread"),
                            FactItem("更新", "原地 Patch，没有新增日志气泡"),
                        ),
                    ),
                ),
                PatchOperation.PutBlock(
                    SourceBlock(
                        "source",
                        "执行来源",
                        "本地 Fake Gateway 界面演示",
                        "非远端验收证据",
                    ),
                ),
                PatchOperation.PutBlock(
                    FileBlock(
                        id = "file",
                        label = "ThreadViewModel.kt",
                        change = FileChangeKind.MODIFIED,
                        detail = "演示同一 Turn 内的卡片原地更新",
                        language = "Kotlin",
                        fallbackText = "演示文件已修改",
                    ),
                ),
                PatchOperation.PutBlock(
                    DiffBlock(
                        id = "diff",
                        fileLabel = "ThreadViewModel.kt",
                        summary = "将事件流聚合为单张任务卡",
                        preview = "- one bubble per event\n+ one task card per turn",
                        additions = 1,
                        deletions = 1,
                        fallbackText = "演示差异：单 Turn 使用单张任务卡",
                    ),
                ),
                PatchOperation.PutBlock(
                    TestBlock(
                        id = "test",
                        title = "富消息演示检查",
                        summary = "本地 Fake Gateway 生成的界面数据，不代表设备或远端环境验收。",
                        passed = 5,
                        failed = 0,
                        durationLabel = "演示数据",
                        state = StepState.COMPLETED,
                        fallbackText = "5 项本地界面演示检查已生成",
                    ),
                ),
                PatchOperation.PutBlock(
                    NoticeBlock(
                        "done",
                        "可以继续追问",
                        "下一条消息会作为当前 Thread 的新 Turn。",
                        Tone.SUCCESS,
                    ),
                ),
                PatchOperation.SetRootBlocks(
                    listOf(
                        "status",
                        "steps",
                        "relay",
                        "tool",
                        "terminal",
                        "file",
                        "diff",
                        "test",
                        "metrics",
                        "facts",
                        "source",
                        "done",
                    ),
                ),
                PatchOperation.SetMessageState(RichMessageState.COMPLETED),
            ),
        )
        updateThread(threadKey, preview = "任务完成，可以继续追问", state = ThreadRunState.IDLE)
        emitTurnLifecycle(threadKey, turnId, TurnLifecyclePhase.COMPLETED)
    }

    private suspend fun runEdgeCaseDemo(threadKey: ThreadKey, messageId: String, turnId: String) {
        delay(500)
        val longPreview = (1..40).joinToString("\n") { line ->
            "demo line ${line.toString().padStart(2, '0')}: bounded preview"
        }
        patch(
            threadKey,
            messageId,
            listOf(
                PatchOperation.PutBlock(
                    StatusBlock(
                        "status",
                        "边界演示已生成",
                        "仅用于检查长内容和未知块降级",
                        Tone.SUCCESS,
                        1f,
                    ),
                ),
                PatchOperation.PutBlock(
                    TerminalBlock(
                        id = "terminal",
                        title = "长内容预览（本地演示）",
                        commandLabel = "边界检查",
                        output = longPreview,
                        state = StepState.COMPLETED,
                        fallbackText = "本地长内容预览已生成",
                    ),
                ),
                PatchOperation.PutBlock(
                    UnknownBlock(
                        id = "future",
                        originalType = "future-chart-v2",
                        fallbackText = "当前版本不识别该演示图表，已安全显示文字摘要。",
                    ),
                ),
                PatchOperation.PutBlock(
                    initialSteps().copy(items = initialSteps().items.map { it.copy(state = StepState.COMPLETED) }),
                ),
                PatchOperation.PutBlock(completedRelayFixture()),
                PatchOperation.SetRootBlocks(listOf("status", "terminal", "future")),
                PatchOperation.SetMessageState(RichMessageState.COMPLETED),
            ),
        )
        updateThread(threadKey, preview = "本地边界演示已完成", state = ThreadRunState.IDLE)
        emitTurnLifecycle(threadKey, turnId, TurnLifecyclePhase.COMPLETED)
    }

    private suspend fun emitTurnLifecycle(
        threadKey: ThreadKey,
        turnId: String,
        phase: TurnLifecyclePhase,
    ) {
        val current = states[threadKey] ?: return
        applyAndEmit(
            threadKey,
            ConversationEvent.TurnLifecycle(
                eventId = nextEventId(),
                streamId = current.streamId,
                streamSeq = current.streamSeq + 1,
                threadKey = threadKey,
                turnId = turnId,
                phase = phase,
            ),
        )
    }

    private suspend fun emitAdded(threadKey: ThreadKey, message: RichMessage) {
        val current = states[threadKey] ?: ThreadConversationState(threadKey)
        val event = ConversationEvent.MessageAdded(
            eventId = nextEventId(),
            streamId = current.streamId,
            streamSeq = current.streamSeq + 1,
            message = message,
        )
        applyAndEmit(threadKey, event)
    }

    private suspend fun patch(
        threadKey: ThreadKey,
        messageId: String,
        operations: List<PatchOperation>,
    ) {
        val current = states[threadKey] ?: return
        val message = current.messages.firstOrNull { it.id == messageId } ?: return
        val event = ConversationEvent.MessagePatched(
            eventId = nextEventId(),
            streamId = current.streamId,
            streamSeq = current.streamSeq + 1,
            messageId = messageId,
            baseRevision = message.revision,
            revision = message.revision + 1,
            operations = operations,
        )
        applyAndEmit(threadKey, event)
    }

    private suspend fun applyAndEmit(threadKey: ThreadKey, event: ConversationEvent) {
        val current = states[threadKey] ?: ThreadConversationState(
            threadKey = threadKey,
            streamId = streamIds.getOrPut(threadKey, ::newStreamId),
        )
        val reduced = ConversationReducer.reduce(current, event)
        check(!reduced.resyncRequired) { "Fake gateway produced an invalid event sequence" }
        states[threadKey] = reduced
        eventBus.emit(threadKey to event)
    }

    private fun updateThread(
        key: ThreadKey,
        preview: String,
        state: ThreadRunState,
        lastTurnId: String? = null,
    ) {
        val now = System.currentTimeMillis()
        _threads.value = _threads.value
            .map { thread ->
                if (thread.key == key) {
                    thread.copy(
                        preview = preview,
                        state = state,
                        updatedAtMillis = now,
                        lastTurnId = lastTurnId ?: thread.lastTurnId,
                    )
                } else {
                    thread
                }
            }
            .sortedByDescending(CodexThread::updatedAtMillis)
    }

    private fun welcomeMessage(threadTitle: String): RichMessage {
        val block = TextBlock(
            id = "welcome-text",
            text = "你好，我是 ChebyAgent。当前已进入「$threadTitle」，可以直接继续上次的工作。",
        )
        return RichMessage(
            id = "welcome-${UUID.randomUUID()}",
            role = MessageRole.ASSISTANT,
            state = RichMessageState.COMPLETED,
            revision = 0,
            rootBlockIds = listOf(block.id),
            blocks = mapOf(block.id to block),
            createdAtMillis = System.currentTimeMillis(),
        )
    }

    private fun userMessage(text: String, clientMessageId: String): RichMessage {
        val block = TextBlock("user-text", text, markdown = false)
        return RichMessage(
            id = "user-${UUID.randomUUID()}",
            role = MessageRole.USER,
            state = RichMessageState.COMPLETED,
            revision = 0,
            rootBlockIds = listOf(block.id),
            blocks = mapOf(block.id to block),
            createdAtMillis = System.currentTimeMillis(),
            clientMessageId = clientMessageId,
        )
    }

    private fun runningMessage(id: String): RichMessage = RichMessage(
        id = id,
        role = MessageRole.ASSISTANT,
        state = RichMessageState.STREAMING,
        revision = 0,
        rootBlockIds = listOf("status", "steps", "relay"),
        blocks = listOf(
            StatusBlock("status", "正在理解请求", "将沿用当前 Codex Thread", Tone.INFO, 0.15f),
            initialSteps(),
            initialRelay(),
        ).associateBy(RichBlockId),
        createdAtMillis = System.currentTimeMillis(),
    )

    private fun offlineMessage(id: String, actionRef: String): RichMessage = RichMessage(
        id = id,
        role = MessageRole.ASSISTANT,
        state = RichMessageState.FAILED,
        revision = 0,
        rootBlockIds = listOf("offline", "actions"),
        blocks = listOf(
            NoticeBlock(
                "offline",
                "连接已断开",
                "消息没有重复提交。恢复网络后可以安全重试。",
                Tone.WARNING,
            ),
            ActionsBlock(
                "actions",
                listOf(ActionItem("retry", "重试", actionRef, ActionKind.PRIMARY)),
            ),
        ).associateBy(RichBlockId),
        createdAtMillis = System.currentTimeMillis(),
    )

    private fun initialSteps() = StepsBlock(
        id = "steps",
        title = "执行计划",
        items = listOf(
            StepItem("understand", "理解目标与安全边界", StepState.ACTIVE),
            StepItem("context", "读取相关上下文", StepState.PENDING),
            StepItem("tools", "执行所需工具", StepState.PENDING),
            StepItem("result", "核对并整理结果", StepState.PENDING),
        ),
    )

    private fun initialRelay() = RelayBlock(
        id = "relay",
        title = "执行接力",
        nodes = listOf(
            RelayNode("request", "请求", state = StepState.COMPLETED),
            RelayNode("codex", "Codex", "正在处理", StepState.ACTIVE),
            RelayNode("tools", "工具", state = StepState.PENDING),
            RelayNode("result", "结果", state = StepState.PENDING),
        ),
    )

    private fun initialThreads(): List<CodexThread> {
        val now = System.currentTimeMillis()
        return listOf(
            CodexThread(
                key = ThreadKey("demo-main"),
                title = "软件交付 · 富消息演示",
                preview = "本地演示：任务卡原地更新",
                updatedAtMillis = now,
            ),
            CodexThread(
                key = ThreadKey("demo-server"),
                title = "通用协作 · 富消息演示",
                preview = "本地演示：工具、文件与检查",
                updatedAtMillis = now - 86_400_000,
            ),
        )
    }

    private fun nextEventId(): String = "evt-${eventCounter.incrementAndGet()}-${UUID.randomUUID()}"

    private fun newStreamId(): String = "stream-${UUID.randomUUID()}"

    private data class PendingTurn(
        val threadKey: ThreadKey,
        val assistantMessageId: String,
        val idempotencyKey: String,
        val turnId: String,
    )

    private companion object {
        const val EDGE_CASE_DEMO_PROMPT = "#demo-edge"
        val RichBlockId: (com.cheby.codex.mobile.model.RichBlock) -> String = { it.id }
    }
}

internal fun completedRelayFixture() = RelayBlock(
    id = "relay",
    title = "执行接力",
    nodes = listOf(
        RelayNode("request", "请求", state = StepState.COMPLETED),
        RelayNode("codex", "Codex", state = StepState.COMPLETED),
        RelayNode("tools", "工具", state = StepState.COMPLETED),
        RelayNode("result", "结果", state = StepState.COMPLETED),
    ),
)
