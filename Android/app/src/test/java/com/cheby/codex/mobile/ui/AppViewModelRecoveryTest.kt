package com.cheby.codex.mobile.ui

import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.ViewModelStore
import androidx.lifecycle.ViewModelStoreOwner
import com.cheby.codex.mobile.data.InMemoryDurableOutboxStore
import com.cheby.codex.mobile.data.InMemoryLastThreadStore
import com.cheby.codex.mobile.data.OutboxEntry
import com.cheby.codex.mobile.data.OutboxState
import com.cheby.codex.mobile.data.key
import com.cheby.codex.mobile.gateway.CodexGateway
import com.cheby.codex.mobile.gateway.CloseableCodexGateway
import com.cheby.codex.mobile.gateway.GatewayKind
import com.cheby.codex.mobile.gateway.GatewayTransportException
import com.cheby.codex.mobile.gateway.GatewayRequestException
import com.cheby.codex.mobile.gateway.GatewaySessionIdentity
import com.cheby.codex.mobile.gateway.GatewayCapabilities
import com.cheby.codex.mobile.gateway.LocalImageCapability
import com.cheby.codex.mobile.gateway.TurnInput
import com.cheby.codex.mobile.gateway.TurnSubmission
import com.cheby.codex.mobile.gateway.UploadedImageAsset
import com.cheby.codex.mobile.model.CodexThread
import com.cheby.codex.mobile.model.ConversationEvent
import com.cheby.codex.mobile.model.ConversationSnapshot
import com.cheby.codex.mobile.model.GatewayConnectionState
import com.cheby.codex.mobile.model.ThreadKey
import com.cheby.codex.mobile.model.ThreadRunState
import com.cheby.codex.mobile.model.RichMessage
import com.cheby.codex.mobile.model.RichMessageState
import com.cheby.codex.mobile.model.MessageRole
import com.cheby.codex.mobile.model.MessagePresentation
import com.cheby.codex.mobile.model.TextBlock
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.emptyFlow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import kotlinx.coroutines.withContext
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class AppViewModelRecoveryTest {
    private val dispatcher = StandardTestDispatcher()

    @Before
    fun setUp() {
        Dispatchers.setMain(dispatcher)
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    @Test
    fun `local input bypasses uncertain old delivery without replaying it`() = runTest(dispatcher) {
        val selected = thread("local-direct")
        val gateway = FakeGateway(listOf(selected)).apply {
            mutableKind.value = GatewayKind.LOCAL
            sendError = GatewayTransportException("response lost")
        }
        val viewModel = AppViewModel(gateway, InMemoryLastThreadStore(selected.key))
        advanceUntilIdle()
        assertTrue(viewModel.send("first"))
        advanceUntilIdle()
        assertEquals(1, gateway.sentIds.size)
        val firstId = gateway.sentIds.single()
        gateway.sendError = null
        assertTrue(viewModel.send("second"))
        advanceUntilIdle()
        assertEquals(2, gateway.sentIds.size)
        assertEquals(1, gateway.sentIds.count { it == firstId })
        // Acknowledgement is a receipt, not a locally scheduled running task.
        assertFalse(viewModel.uiState.value.outbox.any { it.text == "second" })
        assertTrue(viewModel.uiState.value.outbox.any { it.text == "first" })
        assertTrue(viewModel.uiState.value.sendingOutboxKeys.isEmpty())
        assertTrue(viewModel.uiState.value.localDeliveryNotices.values.any { it.contains("送达未确认") })
        gateway.mutableConnection.value = GatewayConnectionState.OFFLINE
        advanceUntilIdle()
        gateway.mutableConnection.value = GatewayConnectionState.ONLINE
        advanceUntilIdle()
        assertEquals(2, gateway.sentIds.size)
    }

    @Test
    fun `local official busy rejection ends sending without replay`() = runTest(dispatcher) {
        val selected = thread("local-busy")
        val gateway = FakeGateway(listOf(selected)).apply {
            mutableKind.value = GatewayKind.LOCAL
            sendError = GatewayRequestException("THREAD_BUSY", retryable = false)
        }
        val viewModel = AppViewModel(gateway, InMemoryLastThreadStore(selected.key))
        advanceUntilIdle()
        assertTrue(viewModel.send("test"))
        advanceUntilIdle()
        assertTrue(viewModel.uiState.value.sendingOutboxKeys.isEmpty())
        assertTrue(viewModel.uiState.value.localDeliveryNotices.values.single().contains("本条未接受"))
        assertEquals(OutboxState.RETRYABLE, viewModel.uiState.value.outbox.single().state)
        assertEquals(1, gateway.sentIds.size)
        val key = viewModel.uiState.value.outbox.single().key
        gateway.sendError = null
        viewModel.retryOutbox(key)
        advanceUntilIdle()
        assertEquals(2, gateway.sentIds.size)
        assertTrue(viewModel.uiState.value.outbox.isEmpty())
    }

    @Test
    fun `local image receipt ends delivery without waiting for task completion`() = runTest(dispatcher) {
        val gateway = FakeGateway(listOf(thread("thread-a"))).apply {
            mutableKind.value = GatewayKind.LOCAL
            localImageCapability = testImageCapability()
        }
        val outbox = InMemoryDurableOutboxStore()
        val viewModel = AppViewModel(gateway, InMemoryLastThreadStore(), outbox,
            imageAttachmentReader = FakeImageReader(listOf(imageAttachment("local-image", byteArrayOf(1, 2)))))
        advanceUntilIdle()
        viewModel.selectImages(listOf("content://local/image"), requireNotNull(viewModel.createImageSelectionTicket()))
        advanceUntilIdle()
        assertTrue(viewModel.send("image"))
        advanceUntilIdle()
        assertEquals(1, gateway.sentInputs.size)
        assertTrue(gateway.sentInputs.single().any { it is TurnInput.Image })
        assertTrue(outbox.list().isEmpty())
        assertTrue(viewModel.uiState.value.sendingOutboxKeys.isEmpty())
    }

    @Test
    fun `stop uses selected local turn and does not infer completion from acknowledgement`() = runTest(dispatcher) {
        val running = thread("thread-a").copy(state = ThreadRunState.RUNNING, lastTurnId = "active-turn")
        val gateway = FakeGateway(listOf(running)).apply { mutableKind.value = GatewayKind.LOCAL }
        val viewModel = AppViewModel(gateway, InMemoryLastThreadStore(running.key))
        advanceUntilIdle()
        assertTrue(viewModel.uiState.value.canStopTurn)
        viewModel.stopCurrentTurn()
        advanceUntilIdle()
        assertEquals(listOf(running.key to "active-turn"), gateway.interruptedTurns)
        assertTrue(viewModel.uiState.value.canStopTurn)
        gateway.mutableThreads.value = listOf(running.copy(state = ThreadRunState.IDLE))
        advanceUntilIdle()
        assertFalse(viewModel.uiState.value.canStopTurn)
        viewModel.stopCurrentTurn()
        advanceUntilIdle()
        assertEquals(1, gateway.interruptedTurns.size)
    }

    @Test
    fun `cold start waits for remote thread list before restoring last thread`() = runTest(dispatcher) {
        val gateway = FakeGateway(emptyList()).apply {
            mutableConnection.value = GatewayConnectionState.CONNECTING
        }
        val remembered = ThreadKey("thread-b")
        val viewModel = AppViewModel(gateway, InMemoryLastThreadStore(remembered))
        advanceUntilIdle()

        assertEquals(null, viewModel.uiState.value.currentThreadKey)
        assertTrue(viewModel.uiState.value.isLoading)

        gateway.mutableThreads.value = listOf(thread("thread-a"), thread("thread-b"))
        gateway.mutableConnection.value = GatewayConnectionState.ONLINE
        advanceUntilIdle()

        assertEquals(remembered, viewModel.uiState.value.currentThreadKey)
        assertFalse(viewModel.uiState.value.isLoading)
    }

    @Test
    fun `cold start subscribes loadable provisional thread and terminal converges one user bubble`() =
        runTest(dispatcher) {
            val threadKey = ThreadKey("provisional-thread")
            val clientMessageId = "provisional-client"
            val turnId = "provisional-turn"
            val pending = OutboxEntry(
                clientMessageId = clientMessageId,
                sessionScope = "scope-a",
                threadKey = threadKey,
                text = "restore this turn",
                state = OutboxState.ACCEPTED,
                createdAtMillis = 10,
                acceptedTurnId = turnId,
            )
            val events = MutableSharedFlow<ConversationEvent>(extraBufferCapacity = 2)
            val userBlock = TextBlock("provisional-user-text", pending.text)
            val canonicalUser = RichMessage(
                id = "provisional-user",
                role = MessageRole.USER,
                state = RichMessageState.COMPLETED,
                revision = 0,
                rootBlockIds = listOf(userBlock.id),
                blocks = mapOf(userBlock.id to userBlock),
                createdAtMillis = pending.createdAtMillis,
                clientMessageId = clientMessageId,
                turnId = turnId,
            )
            val gateway = FakeGateway(emptyList()).apply {
                loader = { selected ->
                    assertEquals(threadKey, selected)
                    ConversationSnapshot(selected, "stream-provisional", 0, emptyList())
                }
                observer = { selected, streamId, cursor ->
                    assertEquals(threadKey, selected)
                    assertEquals("stream-provisional", streamId)
                    assertEquals(0L, cursor)
                    events
                }
            }
            val outbox = InMemoryDurableOutboxStore(listOf(pending))
            val viewModel = AppViewModel(
                gateway,
                InMemoryLastThreadStore(threadKey),
                outbox,
            )

            runCurrent()

            assertEquals(threadKey, viewModel.uiState.value.currentThreadKey)
            assertEquals(listOf(threadKey), viewModel.uiState.value.threads.map(CodexThread::key))
            assertEquals(listOf(Triple(threadKey, "stream-provisional", 0L)), gateway.observed)
            assertEquals(OutboxState.ACCEPTED, outbox.list().single().state)
            assertTrue(gateway.sentIds.isEmpty())

            events.emit(
                ConversationEvent.MessageAdded(
                    eventId = "provisional-user-added",
                    streamId = "stream-provisional",
                    streamSeq = 1,
                    message = canonicalUser,
                ),
            )
            events.emit(
                ConversationEvent.TurnLifecycle(
                    eventId = "provisional-terminal",
                    streamId = "stream-provisional",
                    streamSeq = 2,
                    threadKey = threadKey,
                    turnId = turnId,
                    phase = com.cheby.codex.mobile.model.TurnLifecyclePhase.COMPLETED,
                ),
            )
            runCurrent()

            val state = viewModel.uiState.value
            assertTrue(outbox.list().isEmpty())
            assertTrue(state.outbox.isEmpty())
            assertTrue(state.acceptedUserMessages.isEmpty())
            assertEquals(
                1,
                state.messages.count {
                    it.role == MessageRole.USER && it.clientMessageId == clientMessageId
                },
            )
            assertEquals(
                1,
                conversationTimeline(
                    messages = visibleConversationMessages(state.messages, state.outbox),
                    accepted = state.acceptedUserMessages,
                    pending = state.outbox,
                ).count { timeline ->
                    timeline is ThreadTimelineEntry.Canonical &&
                        timeline.message.role == MessageRole.USER &&
                        timeline.message.clientMessageId == clientMessageId
                },
            )
        }

    @Test
    fun `cold start retries selected conversation after transient snapshot failure`() =
        runTest(dispatcher) {
            val selected = ThreadKey("thread-a")
            val attempts = AtomicInteger()
            val gateway = FakeGateway(listOf(thread(selected.value))).apply {
                loader = { key ->
                    if (attempts.incrementAndGet() == 1) {
                        throw GatewayTransportException("transient disconnect")
                    }
                    ConversationSnapshot(key, "stream-a", 0, emptyList())
                }
            }

            val viewModel = AppViewModel(
                gateway,
                InMemoryLastThreadStore(selected),
            )
            advanceUntilIdle()

            assertEquals(2, attempts.get())
            assertEquals(selected, viewModel.uiState.value.currentConversation?.threadKey)
            assertFalse(viewModel.uiState.value.isConversationRecovering)
        }

    @Test
    fun `transient observation failure resumes same thread without clearing or duplicating events`() =
        runTest(dispatcher) {
            fun assistant(id: String, text: String): RichMessage {
                val block = TextBlock("text-$id", text)
                return RichMessage(
                    id = id,
                    role = MessageRole.ASSISTANT,
                    state = RichMessageState.COMPLETED,
                    revision = 0,
                    rootBlockIds = listOf(block.id),
                    blocks = mapOf(block.id to block),
                    createdAtMillis = 1,
                )
            }
            val first = ConversationEvent.MessageAdded(
                "event-1",
                "stream-a",
                1,
                assistant("message-1", "first"),
            )
            val second = ConversationEvent.MessageAdded(
                "event-2",
                "stream-a",
                2,
                assistant("message-2", "second"),
            )
            var subscriptions = 0
            val gateway = FakeGateway(listOf(thread("a"))).apply {
                loader = { key -> ConversationSnapshot(key, "stream-a", 0, emptyList()) }
                observer = { _, _, _ ->
                    subscriptions += 1
                    if (subscriptions == 1) {
                        flow {
                            mutableConnection.value = GatewayConnectionState.OFFLINE
                            emit(first)
                            throw GatewayTransportException("temporary offline")
                        }
                    } else {
                        flow {
                            mutableConnection.value = GatewayConnectionState.ONLINE
                            emit(first)
                            emit(second)
                            awaitCancellation()
                        }
                    }
                }
            }
            val viewModel = AppViewModel(gateway, InMemoryLastThreadStore(ThreadKey("a")))
            runCurrent()

            assertEquals(ThreadKey("a"), viewModel.uiState.value.currentThreadKey)
            assertEquals(listOf("message-1"), viewModel.uiState.value.messages.map(RichMessage::id))
            assertFalse(viewModel.uiState.value.isConversationRecovering)
            assertEquals(GatewayConnectionState.OFFLINE, viewModel.uiState.value.connectionState)

            advanceTimeBy(250)
            runCurrent()

            assertEquals(2, subscriptions)
            assertEquals(listOf(0L, 1L), gateway.observed.map { it.third })
            assertEquals(listOf("message-1", "message-2"), viewModel.uiState.value.messages.map(RichMessage::id))
            assertEquals(2L, viewModel.uiState.value.currentConversation?.streamSeq)
            assertFalse(viewModel.uiState.value.currentConversation?.resyncRequired ?: true)
            assertEquals(GatewayConnectionState.ONLINE, viewModel.uiState.value.connectionState)
        }

    @Test
    fun `transient empty thread list does not replace the active conversation with a blank draft`() =
        runTest(dispatcher) {
            val selected = ThreadKey("thread-a")
            val lastThreadStore = InMemoryLastThreadStore(selected)
            val gateway = FakeGateway(listOf(thread(selected.value)))
            val viewModel = AppViewModel(gateway, lastThreadStore)
            advanceUntilIdle()

            gateway.mutableConnection.value = GatewayConnectionState.OFFLINE
            gateway.mutableThreads.value = emptyList()
            runCurrent()

            assertEquals(selected, viewModel.uiState.value.currentThreadKey)
            assertEquals(listOf(selected), viewModel.uiState.value.threads.map(CodexThread::key))
            assertEquals(selected, lastThreadStore.read())

            gateway.mutableConnection.value = GatewayConnectionState.ONLINE
            runCurrent()
            assertEquals(selected, viewModel.uiState.value.currentThreadKey)

            gateway.mutableThreads.value = listOf(thread(selected.value))
            advanceUntilIdle()
            assertEquals(selected, viewModel.uiState.value.currentThreadKey)
        }

    @Test
    fun `reconnect reloads the selected conversation after a missed completion event`() =
        runTest(dispatcher) {
            val selected = ThreadKey("thread-recovered-completion")
            var recovered = false
            fun assistant(text: String): RichMessage {
                val block = TextBlock("assistant-text", text)
                return RichMessage(
                    id = "assistant",
                    role = MessageRole.ASSISTANT,
                    state = if (recovered) RichMessageState.COMPLETED else RichMessageState.STREAMING,
                    revision = if (recovered) 1 else 0,
                    rootBlockIds = listOf(block.id),
                    blocks = mapOf(block.id to block),
                    createdAtMillis = 1,
                    turnId = "turn-recovered-completion",
                )
            }
            val gateway = FakeGateway(listOf(thread(selected.value))).apply {
                loader = { key ->
                    loadCalls += 1
                    ConversationSnapshot(
                        key,
                        "stream-recovered-completion",
                        loadCalls.toLong(),
                        listOf(assistant(if (recovered) "路线结果" else "正在整理结果…")),
                    )
                }
            }
            val viewModel = AppViewModel(gateway, InMemoryLastThreadStore(selected))
            advanceUntilIdle()

            assertEquals("正在整理结果…", viewModel.uiState.value.currentConversation
                ?.messages?.single()?.blocks?.values?.single()?.fallbackText)

            gateway.mutableConnection.value = GatewayConnectionState.OFFLINE
            runCurrent()
            recovered = true
            gateway.mutableConnection.value = GatewayConnectionState.ONLINE
            advanceUntilIdle()

            assertTrue(gateway.loadCalls >= 2)
            assertEquals("路线结果", viewModel.uiState.value.currentConversation
                ?.messages?.single()?.blocks?.values?.single()?.fallbackText)
        }

    @Test
    fun `reconnect reloads an active turn after its last visible tool card completed`() =
        runTest(dispatcher) {
            val selected = ThreadKey("thread-recovered-tool-turn")
            var recovered = false
            var snapshotLoads = 0
            fun assistant(text: String): RichMessage {
                val block = TextBlock("assistant-tool-text", text)
                return RichMessage(
                    id = "assistant-tool",
                    role = MessageRole.ASSISTANT,
                    state = RichMessageState.COMPLETED,
                    revision = if (recovered) 1 else 0,
                    rootBlockIds = listOf(block.id),
                    blocks = mapOf(block.id to block),
                    createdAtMillis = 1,
                    turnId = "turn-recovered-tool",
                )
            }
            val running = thread(
                selected.value,
                state = ThreadRunState.RUNNING,
                lastTurnId = "turn-recovered-tool",
            )
            val gateway = FakeGateway(listOf(running)).apply {
                loader = { key ->
                    snapshotLoads += 1
                    ConversationSnapshot(
                        key,
                        "stream-recovered-tool",
                        snapshotLoads.toLong(),
                        listOf(assistant(if (recovered) "最终结果" else "工具已完成")),
                    )
                }
            }
            val viewModel = AppViewModel(gateway, InMemoryLastThreadStore(selected))
            advanceUntilIdle()

            assertEquals("工具已完成", viewModel.uiState.value.messages.single()
                .blocks.values.single().fallbackText)

            gateway.mutableConnection.value = GatewayConnectionState.OFFLINE
            runCurrent()
            recovered = true
            gateway.mutableConnection.value = GatewayConnectionState.ONLINE
            advanceUntilIdle()

            assertTrue(snapshotLoads >= 2)
            assertEquals("最终结果", viewModel.uiState.value.messages.single()
                .blocks.values.single().fallbackText)
        }

    @Test
    fun `retryable turn automatically resumes with the same idempotency key after reconnect`() =
        runTest(dispatcher) {
            val gateway = FakeGateway(listOf(thread("thread-a"))).apply { failSend = true }
            val outbox = InMemoryDurableOutboxStore()
            val viewModel = AppViewModel(gateway, InMemoryLastThreadStore(), outbox)
            advanceUntilIdle()

            assertTrue(viewModel.send("hi"))
            advanceUntilIdle()
            val pending = outbox.list().single()
            assertEquals(OutboxState.RETRYABLE, pending.state)

            gateway.failSend = false
            gateway.mutableConnection.value = GatewayConnectionState.OFFLINE
            runCurrent()
            gateway.mutableConnection.value = GatewayConnectionState.ONLINE
            advanceUntilIdle()

            assertEquals(listOf(pending.clientMessageId, pending.clientMessageId), gateway.sentIds)
            assertEquals(OutboxState.ACCEPTED, outbox.list().single().state)
            assertEquals("turn-${pending.clientMessageId}", outbox.list().single().acceptedTurnId)
        }

    @Test
    fun `terminal idempotent submission converges without waiting for a duplicate lifecycle event`() =
        runTest(dispatcher) {
            val gateway = FakeGateway(listOf(thread("thread-a"))).apply {
                submissionStatus = "completed"
            }
            val outbox = InMemoryDurableOutboxStore()
            val viewModel = AppViewModel(gateway, InMemoryLastThreadStore(), outbox)
            advanceUntilIdle()

            assertTrue(viewModel.send("already completed upstream"))
            advanceUntilIdle()

            assertEquals(1, gateway.sentIds.size)
            assertTrue(outbox.list().isEmpty())
            assertEquals(1, viewModel.uiState.value.acceptedUserMessages.size)
        }

    @Test
    fun `terminal assistant snapshot clears matching needs sync despite stale running catalog`() =
        runTest(dispatcher) {
            val threadKey = ThreadKey("thread-terminal-snapshot")
            val clientMessageId = "client-terminal-snapshot"
            val turnId = "turn-terminal-snapshot"
            val pending = OutboxEntry(
                clientMessageId = clientMessageId,
                sessionScope = "scope-a",
                threadKey = threadKey,
                text = "already completed",
                state = OutboxState.NEEDS_SYNC,
                createdAtMillis = 1,
            )
            val userText = TextBlock("terminal-user-text", pending.text)
            val canonicalUser = RichMessage(
                id = "terminal-user",
                role = MessageRole.USER,
                state = RichMessageState.COMPLETED,
                revision = 0,
                rootBlockIds = listOf(userText.id),
                blocks = mapOf(userText.id to userText),
                createdAtMillis = 1,
                clientMessageId = clientMessageId,
                turnId = turnId,
            )
            val terminalAssistant = RichMessage(
                id = "terminal-assistant",
                role = MessageRole.ASSISTANT,
                state = RichMessageState.COMPLETED,
                revision = 1,
                rootBlockIds = emptyList(),
                blocks = emptyMap(),
                createdAtMillis = 2,
                turnId = turnId,
            )
            val gateway = FakeGateway(
                listOf(
                    thread(threadKey.value).copy(
                        state = ThreadRunState.RUNNING,
                        lastTurnId = turnId,
                    ),
                ),
            ).apply {
                loader = { selected ->
                    ConversationSnapshot(
                        selected,
                        "stream-terminal-snapshot",
                        9,
                        listOf(canonicalUser, terminalAssistant),
                    )
                }
            }
            val outbox = InMemoryDurableOutboxStore(listOf(pending))

            AppViewModel(gateway, InMemoryLastThreadStore(threadKey), outbox)
            advanceUntilIdle()

            assertTrue(outbox.list().isEmpty())
            assertTrue(gateway.sentIds.isEmpty())
        }

    @Test
    fun `ambiguous send immediately snapshot reconciles without reconnect`() = runTest(dispatcher) {
        val threadKey = ThreadKey("thread-ambiguous-snapshot")
        lateinit var gateway: FakeGateway
        gateway = FakeGateway(listOf(thread(threadKey.value))).apply {
            sendError = GatewayTransportException("response lost after acceptance")
            loader = { selected ->
                val clientMessageId = sentIds.lastOrNull()
                val messages = if (clientMessageId == null) {
                    emptyList()
                } else {
                    val turnId = "turn-$clientMessageId"
                    val text = TextBlock("ambiguous-user-text", "hi")
                    listOf(
                        RichMessage(
                            id = "ambiguous-user",
                            role = MessageRole.USER,
                            state = RichMessageState.COMPLETED,
                            revision = 0,
                            rootBlockIds = listOf(text.id),
                            blocks = mapOf(text.id to text),
                            createdAtMillis = 1,
                            clientMessageId = clientMessageId,
                            turnId = turnId,
                        ),
                        RichMessage(
                            id = "ambiguous-assistant",
                            role = MessageRole.ASSISTANT,
                            state = RichMessageState.COMPLETED,
                            revision = 1,
                            rootBlockIds = emptyList(),
                            blocks = emptyMap(),
                            createdAtMillis = 2,
                            turnId = turnId,
                        ),
                    )
                }
                ConversationSnapshot(selected, "stream-ambiguous-snapshot", 3, messages)
            }
        }
        val outbox = InMemoryDurableOutboxStore()
        val viewModel = AppViewModel(gateway, InMemoryLastThreadStore(threadKey), outbox)
        advanceUntilIdle()

        assertTrue(viewModel.send("hi"))
        advanceUntilIdle()

        assertEquals(1, gateway.sentIds.size)
        assertTrue(outbox.list().isEmpty())
        assertTrue(viewModel.uiState.value.outbox.isEmpty())
    }

    @Test
    fun `catalog refresh retains provisional thread that still owns a durable turn`() =
        runTest(dispatcher) {
            val provisional = thread("provisional")
            val existing = thread("existing")
            val gateway = FakeGateway(listOf(provisional, existing)).apply { failSend = true }
            val outbox = InMemoryDurableOutboxStore()
            val viewModel = AppViewModel(
                gateway,
                InMemoryLastThreadStore(provisional.key),
                outbox,
            )
            advanceUntilIdle()

            assertTrue(viewModel.send("hi"))
            advanceUntilIdle()
            assertEquals(OutboxState.RETRYABLE, outbox.list().single().state)

            gateway.mutableThreads.value = listOf(existing)
            advanceUntilIdle()

            assertEquals(provisional.key, viewModel.uiState.value.currentThreadKey)
            assertEquals(
                listOf(provisional.key, existing.key),
                viewModel.uiState.value.threads.map(CodexThread::key),
            )
        }

    @Test
    fun `fresh online container with zero threads finishes loading`() = runTest(dispatcher) {
        val viewModel = AppViewModel(
            FakeGateway(emptyList()),
            InMemoryLastThreadStore(),
        )

        advanceUntilIdle()

        assertFalse(viewModel.uiState.value.isLoading)
        assertEquals(null, viewModel.uiState.value.currentThreadKey)
        assertEquals(GatewayKind.REMOTE, viewModel.uiState.value.gatewayKind)
    }

    @Test
    fun `permanent ViewModelStore clear closes its owned gateway exactly once`() =
        runTest(dispatcher) {
            val gateway = FakeGateway(emptyList())
            val owner = object : ViewModelStoreOwner {
                override val viewModelStore = ViewModelStore()
            }
            ViewModelProvider(
                owner,
                AppViewModel.Factory(gateway, InMemoryLastThreadStore()),
            )[AppViewModel::class.java]
            advanceUntilIdle()

            owner.viewModelStore.clear()
            owner.viewModelStore.clear()

            assertEquals(1, gateway.closeCalls.get())
        }

    @Test
    fun `failed turn restart retries the identical durable client message id`() = runTest(dispatcher) {
        val gateway = FakeGateway(listOf(thread("thread-a"))).apply { failSend = true }
        val outbox = InMemoryDurableOutboxStore()
        val first = AppViewModel(gateway, InMemoryLastThreadStore(), outbox)
        advanceUntilIdle()

        assertTrue(first.send("Keep this message"))
        advanceUntilIdle()
        val failed = outbox.list().single()
        assertEquals(OutboxState.RETRYABLE, failed.state)

        gateway.failSend = false
        val restarted = AppViewModel(gateway, InMemoryLastThreadStore(ThreadKey("thread-a")), outbox)
        advanceUntilIdle()
        restarted.retryOutbox(failed.key)
        advanceUntilIdle()

        assertEquals(listOf(failed.clientMessageId, failed.clientMessageId), gateway.sentIds)
        assertEquals(OutboxState.ACCEPTED, outbox.list().single().state)
        assertEquals("turn-${failed.clientMessageId}", outbox.list().single().acceptedTurnId)
    }

    @Test
    fun `queued entry from process death auto dispatches with stored id`() = runTest(dispatcher) {
        val entry = OutboxEntry(
            "fixed-client-id",
            "scope-a",
            ThreadKey("thread-a"),
            "Persist before send",
            OutboxState.QUEUED,
            100,
        )
        val outbox = InMemoryDurableOutboxStore(listOf(entry))
        val gateway = FakeGateway(listOf(thread("thread-a")))

        val restarted = AppViewModel(gateway, InMemoryLastThreadStore(), outbox)
        advanceUntilIdle()

        assertEquals(listOf("fixed-client-id"), gateway.sentIds)
        assertEquals(OutboxState.ACCEPTED, outbox.list().single().state)
        assertEquals("turn-fixed-client-id", outbox.list().single().acceptedTurnId)
    }

    @Test
    fun `persisted accepted head is never resent and only matching terminal releases same thread`() =
        runTest(dispatcher) {
            val threadKey = ThreadKey("thread-a")
            val accepted = OutboxEntry(
                clientMessageId = "accepted-client-id",
                sessionScope = "scope-a",
                threadKey = threadKey,
                text = "first",
                state = OutboxState.ACCEPTED,
                createdAtMillis = 1,
                acceptedTurnId = "turn-correct",
            )
            val next = OutboxEntry(
                clientMessageId = "next-client-id",
                sessionScope = "scope-a",
                threadKey = threadKey,
                text = "second",
                state = OutboxState.QUEUED,
                createdAtMillis = 2,
            )
            val events = MutableSharedFlow<ConversationEvent>(extraBufferCapacity = 4)
            val gateway = FakeGateway(
                listOf(
                    thread("thread-a").copy(
                        state = ThreadRunState.RUNNING,
                        lastTurnId = "turn-correct",
                    ),
                ),
            ).apply {
                observer = { _, _, _ -> events }
            }
            val outbox = InMemoryDurableOutboxStore(listOf(accepted, next))
            AppViewModel(gateway, InMemoryLastThreadStore(threadKey), outbox)
            advanceUntilIdle()

            assertTrue(gateway.sentIds.isEmpty())
            events.emit(
                ConversationEvent.TurnLifecycle(
                    eventId = "wrong-terminal",
                    streamId = "stream-thread-a",
                    streamSeq = 1,
                    threadKey = threadKey,
                    turnId = "turn-wrong",
                    phase = com.cheby.codex.mobile.model.TurnLifecyclePhase.COMPLETED,
                ),
            )
            runCurrent()
            assertTrue(gateway.sentIds.isEmpty())
            assertEquals(OutboxState.ACCEPTED, outbox.list().first().state)

            events.emit(
                ConversationEvent.TurnLifecycle(
                    eventId = "matching-terminal",
                    streamId = "stream-thread-a",
                    streamSeq = 2,
                    threadKey = threadKey,
                    turnId = "turn-correct",
                    phase = com.cheby.codex.mobile.model.TurnLifecyclePhase.COMPLETED,
                ),
            )
            advanceUntilIdle()

            assertEquals(listOf(next.clientMessageId), gateway.sentIds)
            assertEquals(OutboxState.ACCEPTED, outbox.list().single().state)
            assertEquals("turn-${next.clientMessageId}", outbox.list().single().acceptedTurnId)
        }

    @Test
    fun `same client id on different threads submits concurrently with exact UI state`() = runTest(dispatcher) {
        val a = OutboxEntry(
            "shared-client-message",
            "scope-a",
            ThreadKey("thread-a"),
            "A",
            OutboxState.QUEUED,
            1,
        )
        val b = OutboxEntry(
            "shared-client-message",
            "scope-a",
            ThreadKey("thread-b"),
            "B",
            OutboxState.QUEUED,
            2,
        )
        val sendBarrier = CompletableDeferred<Unit>()
        val gateway = FakeGateway(listOf(thread("thread-a"), thread("thread-b"))).apply {
            sendReturnBarrier = sendBarrier
        }
        val outbox = InMemoryDurableOutboxStore(listOf(a, b))

        val viewModel =
            AppViewModel(gateway, InMemoryLastThreadStore(ThreadKey("thread-a")), outbox)
        runCurrent()

        assertEquals(setOf(a.threadKey, b.threadKey), gateway.sentThreadKeys.toSet())
        assertEquals(setOf(a.key, b.key), viewModel.uiState.value.sendingOutboxKeys)

        sendBarrier.complete(Unit)
        advanceUntilIdle()

        assertEquals(listOf(a.clientMessageId, b.clientMessageId), gateway.sentIds)
        assertEquals(2, outbox.list().size)
        assertTrue(outbox.list().all { it.state == OutboxState.ACCEPTED })
    }

    @Test
    fun `authenticated same scope migration carries durable queue into new generation`() =
        runTest(dispatcher) {
            val threadA = ThreadKey("thread-a")
            val threadB = ThreadKey("thread-b")
            val threadC = ThreadKey("thread-c")
            val queued = OutboxEntry(
                clientMessageId = "queued-client",
                sessionScope = "scope-a",
                threadKey = threadA,
                text = "queued",
                state = OutboxState.QUEUED,
                createdAtMillis = 1,
            )
            val accepted = OutboxEntry(
                clientMessageId = "accepted-client",
                sessionScope = "scope-a",
                threadKey = threadB,
                text = "accepted",
                state = OutboxState.ACCEPTED,
                createdAtMillis = 2,
                acceptedTurnId = "turn-accepted",
            )
            val waiting = OutboxEntry(
                clientMessageId = "waiting-client",
                sessionScope = "scope-a",
                threadKey = threadC,
                text = "waiting",
                state = OutboxState.WAITING_TURN,
                createdAtMillis = 3,
                waitingForTurnId = "turn-running",
            )
            val events = MutableSharedFlow<ConversationEvent>(extraBufferCapacity = 4)
            val gateway = FakeGateway(
                listOf(
                    thread(threadA.value),
                    thread(threadB.value).copy(
                        state = ThreadRunState.RUNNING,
                        lastTurnId = "turn-accepted",
                    ),
                    thread(threadC.value).copy(
                        state = ThreadRunState.RUNNING,
                        lastTurnId = "turn-running",
                    ),
                ),
            ).apply {
                mutableConnection.value = GatewayConnectionState.OFFLINE
                observer = { _, _, _ -> events }
            }
            val outbox = InMemoryDurableOutboxStore(listOf(queued, accepted, waiting))
            val viewModel = AppViewModel(
                gateway,
                InMemoryLastThreadStore(threadA),
                outbox,
            )
            advanceUntilIdle()

            gateway.switchSession(carryOverSameScopeOutbox = true)
            advanceUntilIdle()

            assertEquals("scope-a", viewModel.uiState.value.currentSessionScope)
            assertEquals(1L, viewModel.uiState.value.currentSessionGeneration)
            assertEquals(
                setOf(
                    queued.clientMessageId,
                    accepted.clientMessageId,
                    waiting.clientMessageId,
                ),
                outbox.list().mapTo(mutableSetOf(), OutboxEntry::clientMessageId),
            )
            assertTrue(outbox.list().all { it.sessionGeneration == 1L })
            assertEquals(
                OutboxState.ACCEPTED,
                outbox.list().single { it.clientMessageId == accepted.clientMessageId }.state,
            )
            assertEquals(
                "turn-accepted",
                outbox.list().single {
                    it.clientMessageId == accepted.clientMessageId
                }.acceptedTurnId,
            )
            assertEquals(
                OutboxState.WAITING_TURN,
                outbox.list().single { it.clientMessageId == waiting.clientMessageId }.state,
            )
            assertEquals(
                "turn-running",
                outbox.list().single {
                    it.clientMessageId == waiting.clientMessageId
                }.waitingForTurnId,
            )
            assertTrue(gateway.sentIds.isEmpty())

            gateway.mutableConnection.value = GatewayConnectionState.ONLINE
            advanceUntilIdle()

            assertEquals(listOf(queued.clientMessageId), gateway.sentIds)
            gateway.mutableThreads.value = gateway.mutableThreads.value.map { thread ->
                if (thread.key == threadA) {
                    thread.copy(
                        state = ThreadRunState.RUNNING,
                        lastTurnId = "turn-${queued.clientMessageId}",
                    )
                } else {
                    thread
                }
            }
            runCurrent()
            events.emit(
                ConversationEvent.TurnLifecycle(
                    eventId = "terminal-accepted",
                    streamId = "stream-${threadA.value}",
                    streamSeq = 1,
                    threadKey = threadB,
                    turnId = "turn-accepted",
                    phase = com.cheby.codex.mobile.model.TurnLifecyclePhase.COMPLETED,
                ),
            )
            events.emit(
                ConversationEvent.TurnLifecycle(
                    eventId = "terminal-running",
                    streamId = "stream-${threadA.value}",
                    streamSeq = 2,
                    threadKey = threadC,
                    turnId = "turn-running",
                    phase = com.cheby.codex.mobile.model.TurnLifecyclePhase.COMPLETED,
                ),
            )
            advanceUntilIdle()

            assertEquals(
                listOf(queued.clientMessageId, waiting.clientMessageId),
                gateway.sentIds,
            )
            assertTrue(gateway.sentThreadKeys.none { it == threadB })
            assertEquals(
                setOf(queued.clientMessageId, waiting.clientMessageId),
                outbox.list().mapTo(mutableSetOf(), OutboxEntry::clientMessageId),
            )
            assertTrue(outbox.list().all {
                it.sessionGeneration == 1L && it.state == OutboxState.ACCEPTED
            })
        }

    @Test
    fun `discard targets only selected thread when client ids are reused`() = runTest(dispatcher) {
        val a = OutboxEntry(
            "shared-client-message",
            "scope-a",
            ThreadKey("thread-a"),
            "A",
            OutboxState.REJECTED,
            1,
        )
        val b = a.copy(threadKey = ThreadKey("thread-b"), text = "B", createdAtMillis = 2)
        val outbox = InMemoryDurableOutboxStore(listOf(a, b))
        val viewModel = AppViewModel(
            FakeGateway(listOf(thread("thread-a"), thread("thread-b"))),
            InMemoryLastThreadStore(ThreadKey("thread-a")),
            outbox,
        )
        advanceUntilIdle()

        viewModel.discardOutbox(a.key)

        assertEquals(listOf(b), outbox.list())
    }

    @Test
    fun `draft can remain when outbox persistence fails`() = runTest(dispatcher) {
        val gateway = FakeGateway(listOf(thread("thread-a")))
        val viewModel = AppViewModel(
            gateway,
            InMemoryLastThreadStore(),
            InMemoryDurableOutboxStore(allowWrites = false),
        )
        advanceUntilIdle()

        assertFalse(viewModel.send("Do not clear me"))
        advanceUntilIdle()
        assertTrue(gateway.sentIds.isEmpty())
    }

    @Test
    fun `delivery unknown is retained but never offered as a safe retry`() = runTest(dispatcher) {
        val gateway = FakeGateway(listOf(thread("thread-a"))).apply {
            sendError = GatewayRequestException("TURN_DELIVERY_AMBIGUOUS_RESYNC_REQUIRED", false)
        }
        val outbox = InMemoryDurableOutboxStore()
        val viewModel = AppViewModel(gateway, InMemoryLastThreadStore(), outbox)
        advanceUntilIdle()

        assertTrue(viewModel.send("Ambiguous delivery"))
        advanceUntilIdle()

        assertEquals(OutboxState.NEEDS_SYNC, outbox.list().single().state)
    }

    @Test
    fun `thread busy never waits on a stale idle catalog turn`() = runTest(dispatcher) {
        val staleThread = CodexThread(
            key = ThreadKey("thread-a"),
            title = "A",
            preview = "",
            updatedAtMillis = 1,
            state = ThreadRunState.IDLE,
            lastTurnId = "turn-already-terminal",
        )
        val gateway = FakeGateway(listOf(staleThread)).apply {
            sendError = GatewayRequestException("THREAD_BUSY", false)
        }
        val outbox = InMemoryDurableOutboxStore()
        val viewModel = AppViewModel(
            gateway,
            InMemoryLastThreadStore(staleThread.key),
            outbox,
        )
        advanceUntilIdle()

        assertTrue(viewModel.send("must not wait forever"))
        advanceUntilIdle()

        val retained = outbox.list().single()
        assertEquals(OutboxState.NEEDS_SYNC, retained.state)
        assertEquals(null, retained.waitingForTurnId)
    }

    @Test
    fun `HTTP 429 keeps same outbox id and blocks manual retry until deadline`() = runTest(dispatcher) {
        val gateway = FakeGateway(listOf(thread("thread-a"))).apply {
            sendError = GatewayRequestException("RATE_LIMITED", true, 30_000)
        }
        val outbox = InMemoryDurableOutboxStore()
        val viewModel = AppViewModel(gateway, InMemoryLastThreadStore(), outbox)
        advanceUntilIdle()

        val startedAt = System.currentTimeMillis()
        assertTrue(viewModel.send("Rate limited message"))
        advanceUntilIdle()
        val retained = outbox.list().single()

        assertEquals(OutboxState.RETRYABLE, retained.state)
        assertTrue(retained.retryNotBeforeMillis >= startedAt + 30_000)
        assertEquals(listOf(retained.clientMessageId), gateway.sentIds)

        viewModel.retryOutbox(retained.key)
        advanceUntilIdle()

        assertEquals(listOf(retained.clientMessageId), gateway.sentIds)
        assertEquals(retained.clientMessageId, outbox.list().single().clientMessageId)
    }

    @Test
    fun `approval HTTP 429 exposes wait deadline and suppresses duplicate tap`() = runTest(dispatcher) {
        val gateway = FakeGateway(listOf(thread("thread-a"))).apply {
            actionError = GatewayRequestException("RATE_LIMITED", true, 30_000)
        }
        val viewModel = AppViewModel(gateway, InMemoryLastThreadStore())
        advanceUntilIdle()

        val startedAt = System.currentTimeMillis()
        viewModel.performAction("same-action-ref")
        advanceUntilIdle()

        assertEquals(listOf("same-action-ref"), gateway.performedActions)
        assertTrue(
            viewModel.uiState.value.actionRetryNotBeforeMillis.getValue("same-action-ref") >=
                startedAt + 30_000,
        )

        viewModel.performAction("same-action-ref")
        advanceUntilIdle()

        assertEquals(listOf("same-action-ref"), gateway.performedActions)
    }

    @Test
    fun `queued A send cannot execute against B with the same thread key`() = runTest(dispatcher) {
        val gateway = FakeGateway(listOf(thread("shared-thread")))
        val outbox = InMemoryDurableOutboxStore()
        val viewModel = AppViewModel(gateway, InMemoryLastThreadStore(), outbox)
        advanceUntilIdle()

        assertTrue(viewModel.send("belongs to A"))
        gateway.sessionScope = "scope-b"
        runCurrent()

        assertTrue(gateway.sentIds.isEmpty())
        assertEquals("scope-a", outbox.list().single().sessionScope)
    }

    @Test
    fun `queued A action cannot execute against B`() = runTest(dispatcher) {
        val gateway = FakeGateway(listOf(thread("shared-thread")))
        val viewModel = AppViewModel(gateway, InMemoryLastThreadStore())
        advanceUntilIdle()

        viewModel.performAction("action-owned-by-a")
        gateway.sessionScope = "scope-b"
        runCurrent()

        assertTrue(gateway.performedActions.isEmpty())
    }

    @Test
    fun `switch after send post-check cannot commit A outbox or accepted state into B`() =
        runTest(dispatcher) {
        val commitReached = CompletableDeferred<Unit>()
        val releaseCommit = CompletableDeferred<Unit>()
        val gateway = FakeGateway(listOf(thread("shared-thread")))
        val outbox = InMemoryDurableOutboxStore()
        val viewModel = AppViewModel(
            gateway,
            InMemoryLastThreadStore(),
            outbox,
            beforeSuccessfulSendCommit = {
                commitReached.complete(Unit)
                releaseCommit.await()
            },
        )
        advanceUntilIdle()

        assertTrue(viewModel.send("accepted only by A"))
        runCurrent()
        commitReached.await()
        assertEquals(1, gateway.sentIds.size)
        gateway.sessionScope = "scope-b"
        releaseCommit.complete(Unit)
        advanceUntilIdle()

        assertTrue(viewModel.uiState.value.acceptedUserMessages.isEmpty())
        assertEquals("scope-a", outbox.list().single().sessionScope)
        assertTrue(viewModel.uiState.value.outbox.isEmpty())
    }

    @Test
    fun `late A event is discarded after B takes the same thread key`() = runTest(dispatcher) {
        val releaseOldEvent = CompletableDeferred<Unit>()
        var subscriptions = 0
        val oldBlock = TextBlock("old-a-text", "must not enter B")
        val oldMessage = RichMessage(
            id = "old-a-message",
            role = MessageRole.ASSISTANT,
            state = RichMessageState.COMPLETED,
            revision = 0,
            rootBlockIds = listOf(oldBlock.id),
            blocks = mapOf(oldBlock.id to oldBlock),
            createdAtMillis = 1,
        )
        val gateway = FakeGateway(listOf(thread("shared-thread"))).apply {
            observer = { _, _, _ ->
                subscriptions += 1
                if (subscriptions == 1) {
                    flow {
                        releaseOldEvent.await()
                        emit(ConversationEvent.MessageAdded("old-a", "stream-shared-thread", 1, oldMessage))
                    }
                } else {
                    emptyFlow()
                }
            }
        }
        val viewModel = AppViewModel(gateway, InMemoryLastThreadStore(ThreadKey("shared-thread")))
        runCurrent()

        gateway.sessionScope = "scope-b"
        releaseOldEvent.complete(Unit)
        advanceUntilIdle()

        assertTrue(subscriptions >= 2)
        assertTrue(viewModel.uiState.value.messages.none { it.id == "old-a-message" })
    }

    @Test
    fun `re-pair purge prevents A prompt from being retried against B`() = runTest(dispatcher) {
        val gateway = FakeGateway(listOf(thread("thread-a"))).apply { failSend = true }
        val outbox = InMemoryDurableOutboxStore()
        val viewModel = AppViewModel(gateway, InMemoryLastThreadStore(), outbox)
        advanceUntilIdle()
        assertTrue(viewModel.send("private prompt for container A"))
        advanceUntilIdle()
        val oldEntry = outbox.list().single()
        val oldId = oldEntry.clientMessageId

        gateway.failSend = false
        gateway.sessionScope = "scope-b"
        gateway.mutableKind.value = GatewayKind.UNCONFIGURED
        runCurrent()
        gateway.mutableKind.value = GatewayKind.REMOTE
        advanceUntilIdle()
        viewModel.retryOutbox(oldEntry.key)
        advanceUntilIdle()

        assertEquals(listOf(oldId), gateway.sentIds)
        assertTrue(viewModel.uiState.value.outbox.isEmpty())
    }

    @Test
    fun `late A snapshot cannot replace B and B opens from its own cursor`() = runTest(dispatcher) {
        val a = CompletableDeferred<ConversationSnapshot>()
        val b = CompletableDeferred<ConversationSnapshot>()
        val gateway = FakeGateway(listOf(thread("a"), thread("b"))).apply {
            loader = { key ->
                withContext(NonCancellable) {
                    if (key.value == "a") a.await() else b.await()
                }
            }
        }
        val viewModel = AppViewModel(gateway, InMemoryLastThreadStore(ThreadKey("a")))
        runCurrent()
        viewModel.selectThread(ThreadKey("b"))
        runCurrent()

        b.complete(ConversationSnapshot(ThreadKey("b"), "stream-b", 22, emptyList()))
        runCurrent()
        a.complete(ConversationSnapshot(ThreadKey("a"), "stream-a", 11, emptyList()))
        advanceUntilIdle()

        assertEquals(ThreadKey("b"), viewModel.uiState.value.currentThreadKey)
        assertEquals(ThreadKey("b"), viewModel.uiState.value.currentConversation?.threadKey)
        assertEquals(Triple(ThreadKey("b"), "stream-b", 22L), gateway.observed.single())
    }

    @Test
    fun `sequence gap cancels old stream and reopens from fresh snapshot boundary`() = runTest(dispatcher) {
        var loads = 0
        var oldEventsEmitted = 0
        val gateway = FakeGateway(listOf(thread("a"))).apply {
            loader = { key ->
                loads += 1
                if (loads == 1) {
                    ConversationSnapshot(key, "stream-old", 0, emptyList())
                } else {
                    ConversationSnapshot(key, "stream-new", 10, emptyList())
                }
            }
            observer = { _, stream, _ ->
                if (stream == "stream-old") {
                    flow {
                        oldEventsEmitted += 1
                        emit(ConversationEvent.NoOp("gap", "stream-old", 2))
                        oldEventsEmitted += 1
                        emit(ConversationEvent.NoOp("old-patch-already-in-snapshot", "stream-old", 3))
                    }
                } else {
                    emptyFlow()
                }
            }
        }

        AppViewModel(gateway, InMemoryLastThreadStore(ThreadKey("a")))
        advanceUntilIdle()

        assertEquals(2, loads)
        assertEquals(1, oldEventsEmitted)
        assertEquals(
            listOf(
                Triple(ThreadKey("a"), "stream-old", 0L),
                Triple(ThreadKey("a"), "stream-new", 10L),
            ),
            gateway.observed,
        )
    }

    @Test
    fun `gap on selected thread invalidates and reconciles accepted outbox on other thread`() =
        runTest(dispatcher) {
            val threadA = ThreadKey("a")
            val threadB = ThreadKey("b")
            val acceptedB = OutboxEntry(
                clientMessageId = "accepted-b",
                sessionScope = "scope-a",
                threadKey = threadB,
                text = "canonical on b",
                state = OutboxState.ACCEPTED,
                createdAtMillis = 1,
                acceptedTurnId = "turn-b",
            )
            val releaseGap = CompletableDeferred<Unit>()
            var aLoads = 0
            var bLoads = 0
            val gateway = FakeGateway(
                listOf(
                    thread("a"),
                    thread("b").copy(
                        state = ThreadRunState.RUNNING,
                        lastTurnId = acceptedB.acceptedTurnId,
                    ),
                ),
            ).apply {
                loader = { key ->
                    if (key == threadA) {
                        aLoads += 1
                        ConversationSnapshot(
                            key,
                            if (aLoads == 1) "stream-a-old" else "stream-a-new",
                            if (aLoads == 1) 0 else 2,
                            emptyList(),
                        )
                    } else {
                        bLoads += 1
                        val messages = if (bLoads == 1) {
                            emptyList()
                        } else {
                            val block = TextBlock("text-b", acceptedB.text)
                            listOf(
                                RichMessage(
                                    id = "canonical-b",
                                    role = MessageRole.USER,
                                    state = RichMessageState.COMPLETED,
                                    revision = 0,
                                    rootBlockIds = listOf(block.id),
                                    blocks = mapOf(block.id to block),
                                    createdAtMillis = 1,
                                    clientMessageId = acceptedB.clientMessageId,
                                    turnId = acceptedB.acceptedTurnId,
                                ),
                                RichMessage(
                                    id = "terminal-b",
                                    role = MessageRole.ASSISTANT,
                                    state = RichMessageState.COMPLETED,
                                    revision = 0,
                                    rootBlockIds = emptyList(),
                                    blocks = emptyMap(),
                                    createdAtMillis = 2,
                                    turnId = acceptedB.acceptedTurnId,
                                ),
                            )
                        }
                        ConversationSnapshot(key, "stream-b-$bLoads", bLoads.toLong(), messages)
                    }
                }
                observer = { key, stream, _ ->
                    if (key == threadA && stream == "stream-a-old") {
                        flow {
                            releaseGap.await()
                            emit(ConversationEvent.NoOp("gap-a", stream, 2))
                        }
                    } else {
                        emptyFlow()
                    }
                }
            }
            val outbox = InMemoryDurableOutboxStore(listOf(acceptedB))
            AppViewModel(gateway, InMemoryLastThreadStore(threadA), outbox)
            runCurrent()
            assertEquals(1, bLoads)
            assertEquals(listOf(acceptedB), outbox.list())

            gateway.mutableThreads.value = listOf(thread("a"), thread("b"))
            runCurrent()
            releaseGap.complete(Unit)
            advanceUntilIdle()

            assertEquals(2, aLoads)
            assertEquals(2, bLoads)
            assertTrue(outbox.list().isEmpty())
        }

    @Test
    fun `accepted snapshot with mismatched canonical turn freezes identity and needs sync`() =
        runTest(dispatcher) {
            val threadKey = ThreadKey("accepted-mismatch")
            val accepted = OutboxEntry(
                clientMessageId = "accepted-client",
                sessionScope = "scope-a",
                threadKey = threadKey,
                text = "must stay bound",
                state = OutboxState.ACCEPTED,
                createdAtMillis = 1,
                acceptedTurnId = "turn-original",
            )
            val text = TextBlock("canonical-text", accepted.text)
            val canonical = RichMessage(
                id = "canonical-wrong-turn",
                role = MessageRole.USER,
                state = RichMessageState.COMPLETED,
                revision = 0,
                rootBlockIds = listOf(text.id),
                blocks = mapOf(text.id to text),
                createdAtMillis = 1,
                clientMessageId = accepted.clientMessageId,
                turnId = "turn-wrong",
            )
            val wrongTerminal = RichMessage(
                id = "terminal-wrong-turn",
                role = MessageRole.ASSISTANT,
                state = RichMessageState.COMPLETED,
                revision = 0,
                rootBlockIds = emptyList(),
                blocks = emptyMap(),
                createdAtMillis = 2,
                turnId = "turn-wrong",
            )
            val gateway = FakeGateway(listOf(thread(threadKey.value))).apply {
                loader = { key ->
                    ConversationSnapshot(key, "stream-mismatch", 2, listOf(canonical, wrongTerminal))
                }
            }
            val outbox = InMemoryDurableOutboxStore(listOf(accepted))

            AppViewModel(gateway, InMemoryLastThreadStore(threadKey), outbox)
            advanceUntilIdle()

            val frozen = outbox.list().single()
            assertEquals(OutboxState.NEEDS_SYNC, frozen.state)
            assertEquals("turn-original", frozen.acceptedTurnId)
            assertTrue(gateway.sentIds.isEmpty())
        }

    @Test
    fun `sync required rotation cancels 4409 stream and subscribes at rotated cursor`() = runTest(dispatcher) {
        var loads = 0
        val gateway = FakeGateway(listOf(thread("a"))).apply {
            loader = { key ->
                loads += 1
                if (loads == 1) {
                    ConversationSnapshot(key, "stream-before-4409", 9, emptyList())
                } else {
                    ConversationSnapshot(key, "stream-after-4409", 0, emptyList())
                }
            }
            observer = { _, stream, _ ->
                if (stream == "stream-before-4409") {
                    flowOf(ConversationEvent.SyncRequired("sync", stream, 10))
                } else {
                    emptyFlow()
                }
            }
        }

        AppViewModel(gateway, InMemoryLastThreadStore(ThreadKey("a")))
        advanceUntilIdle()

        assertEquals(2, loads)
        assertEquals(
            listOf(
                Triple(ThreadKey("a"), "stream-before-4409", 9L),
                Triple(ThreadKey("a"), "stream-after-4409", 0L),
            ),
            gateway.observed,
        )
    }

    @Test
    fun `rejected snapshot reloads with bounded backoff before subscribing to silent flow`() = runTest(dispatcher) {
        val loadTimes = mutableListOf<Long>()
        var loads = 0
        fun message(
            id: String,
            text: String,
            role: MessageRole = MessageRole.ASSISTANT,
            clientMessageId: String? = null,
            turnId: String? = clientMessageId?.let { "turn-$it" },
        ): RichMessage {
            val block = TextBlock("text", text)
            return RichMessage(
                id = id,
                role = role,
                state = RichMessageState.COMPLETED,
                revision = 0,
                rootBlockIds = listOf(block.id),
                blocks = mapOf(block.id to block),
                createdAtMillis = 1,
                clientMessageId = clientMessageId,
                turnId = turnId,
            )
        }
        val recoveryEntry = OutboxEntry(
            clientMessageId = "recovery-client-id",
            sessionScope = "scope-a",
            threadKey = ThreadKey("a"),
            text = "accepted while recovering",
            state = OutboxState.QUEUED,
            createdAtMillis = 1,
        )
        val outbox = InMemoryDurableOutboxStore(listOf(recoveryEntry))
        val gateway = FakeGateway(listOf(thread("a"))).apply {
            loader = { key ->
                loadTimes += testScheduler.currentTime
                loads += 1
                if (loads <= 3) {
                    ConversationSnapshot(
                        key,
                        "stream-bad",
                        7,
                        listOf(message("bad-$loads", "汉".repeat(43_600))),
                    )
                } else {
                    ConversationSnapshot(
                        key,
                        "stream-recovered",
                        9,
                        listOf(
                            message(
                                "canonical-user",
                                recoveryEntry.text,
                                MessageRole.USER,
                                recoveryEntry.clientMessageId,
                            ),
                            message(
                                "recovered",
                                "safe",
                                turnId = "turn-${recoveryEntry.clientMessageId}",
                            ),
                        ),
                    )
                }
            }
            observer = { _, _, _ -> emptyFlow() }
        }

        val viewModel = AppViewModel(
            gateway,
            InMemoryLastThreadStore(ThreadKey("a")),
            outbox,
        )
        runCurrent()

        assertEquals(listOf(0L), loadTimes)
        assertTrue(viewModel.uiState.value.isConversationRecovering)
        assertEquals(null, viewModel.uiState.value.currentConversation)
        assertTrue(viewModel.uiState.value.acceptedUserMessages.isEmpty())
        assertFalse(viewModel.send("must not send during recovery"))
        assertTrue(gateway.sentIds.isEmpty())
        assertEquals(listOf(recoveryEntry), outbox.list())
        assertTrue(gateway.observed.isEmpty())

        advanceTimeBy(249)
        runCurrent()
        assertEquals(listOf(0L), loadTimes)
        advanceUntilIdle()

        assertEquals(listOf(0L, 250L, 750L, 1_750L), loadTimes)
        assertEquals(
            listOf(Triple(ThreadKey("a"), "stream-recovered", 9L)),
            gateway.observed,
        )
        assertFalse(viewModel.uiState.value.isConversationRecovering)
        assertFalse(viewModel.uiState.value.currentConversation?.resyncRequired ?: true)
        assertTrue(viewModel.uiState.value.acceptedUserMessages.isEmpty())
        assertTrue(gateway.sentIds.isEmpty())
        assertTrue(outbox.list().isEmpty())
        assertEquals(
            "safe",
            (viewModel.uiState.value.messages.first { it.id == "recovered" }.blocks["text"] as TextBlock).text,
        )
    }

    @Test
    fun `session generation switch clears same thread before loading new container`() = runTest(dispatcher) {
        val bSnapshot = CompletableDeferred<ConversationSnapshot>()
        val loadScopes = mutableListOf<GatewaySessionIdentity>()
        fun assistant(id: String, text: String): RichMessage {
            val block = TextBlock("text-$id", text)
            return RichMessage(
                id = id,
                role = MessageRole.ASSISTANT,
                state = RichMessageState.COMPLETED,
                revision = 0,
                rootBlockIds = listOf(block.id),
                blocks = mapOf(block.id to block),
                createdAtMillis = 1,
            )
        }
        val gateway = FakeGateway(listOf(thread("shared-thread"))).apply {
            loader = { key ->
                loadScopes += sessionIdentity.value
                if (sessionScope == "scope-a") {
                    ConversationSnapshot(key, "stream-a", 0, listOf(assistant("message-a", "A private")))
                } else {
                    bSnapshot.await()
                }
            }
        }
        val viewModel = AppViewModel(
            gateway,
            InMemoryLastThreadStore(ThreadKey("shared-thread")),
        )
        advanceUntilIdle()
        assertEquals(listOf("message-a"), viewModel.uiState.value.messages.map(RichMessage::id))
        assertTrue(viewModel.uiState.value.canSend)

        gateway.switchSession("scope-b")
        runCurrent()

        assertEquals("scope-b", viewModel.uiState.value.currentSessionScope)
        assertEquals(1L, viewModel.uiState.value.currentSessionGeneration)
        assertEquals(ThreadKey("shared-thread"), viewModel.uiState.value.currentThreadKey)
        assertEquals(null, viewModel.uiState.value.currentConversation)
        assertTrue(viewModel.uiState.value.isConversationRecovering)
        assertFalse(viewModel.uiState.value.canSend)
        assertTrue(viewModel.uiState.value.messages.isEmpty())
        assertFalse(viewModel.send("must never reach B before B snapshot"))
        assertTrue(gateway.sentIds.isEmpty())

        bSnapshot.complete(
            ConversationSnapshot(
                ThreadKey("shared-thread"),
                "stream-b",
                7,
                listOf(assistant("message-b", "B only")),
            ),
        )
        advanceUntilIdle()

        assertEquals(listOf("message-b"), viewModel.uiState.value.messages.map(RichMessage::id))
        assertTrue(viewModel.uiState.value.messages.none { it.id == "message-a" })
        assertFalse(viewModel.uiState.value.isConversationRecovering)
        assertTrue(viewModel.uiState.value.canSend)
        assertEquals(
            listOf(
                GatewaySessionIdentity("scope-a", 0),
                GatewaySessionIdentity("scope-b", 1),
            ),
            loadScopes,
        )
        assertTrue(viewModel.send("B accepted"))
        advanceUntilIdle()
        assertEquals("scope-b", gateway.sentScopes.single())
    }

    @Test
    fun `resync keeps accepted identity untouched until a valid canonical snapshot`() = runTest(dispatcher) {
        val releaseSync = CompletableDeferred<Unit>()
        var loads = 0
        var subscriptions = 0
        fun message(
            id: String,
            text: String,
            role: MessageRole = MessageRole.ASSISTANT,
            clientMessageId: String? = null,
            turnId: String? = clientMessageId?.let { "turn-$it" },
        ): RichMessage {
            val block = TextBlock("text-$id", text)
            return RichMessage(
                id = id,
                role = role,
                state = RichMessageState.COMPLETED,
                revision = 0,
                rootBlockIds = listOf(block.id),
                blocks = mapOf(block.id to block),
                createdAtMillis = 1,
                clientMessageId = clientMessageId,
                turnId = turnId,
            )
        }
        val gateway = FakeGateway(listOf(thread("a"))).apply {
            loader = { key ->
                loads += 1
                when (loads) {
                    1 -> ConversationSnapshot(key, "stream-initial", 0, emptyList())
                    2 -> ConversationSnapshot(
                        key,
                        "stream-rejected",
                        1,
                        listOf(message("oversized", "汉".repeat(43_600))),
                    )
                    else -> ConversationSnapshot(
                        key,
                        "stream-recovered",
                        3,
                        listOf(
                            message(
                                "canonical-user",
                                "accepted before resync",
                                MessageRole.USER,
                                sentIds.single(),
                            ),
                        ),
                    )
                }
            }
            observer = { _, stream, _ ->
                subscriptions += 1
                if (stream == "stream-initial") {
                    flow {
                        releaseSync.await()
                        emit(ConversationEvent.SyncRequired("sync", stream, 1))
                    }
                } else {
                    emptyFlow()
                }
            }
        }
        val viewModel = AppViewModel(gateway, InMemoryLastThreadStore(ThreadKey("a")))
        runCurrent()
        assertTrue(viewModel.uiState.value.canSend)

        assertTrue(viewModel.send("accepted before resync"))
        runCurrent()
        assertEquals(OutboxState.ACCEPTED, viewModel.uiState.value.outbox.single().state)

        releaseSync.complete(Unit)
        runCurrent()
        assertTrue(viewModel.uiState.value.isConversationRecovering)
        assertEquals(null, viewModel.uiState.value.currentConversation)
        assertFalse(viewModel.send("blocked while recovering"))

        advanceTimeBy(250)
        runCurrent()
        assertEquals(2, loads)
        assertEquals(OutboxState.ACCEPTED, viewModel.uiState.value.outbox.single().state)
        assertEquals(null, viewModel.uiState.value.currentConversation)

        advanceTimeBy(250)
        runCurrent()
        assertEquals(3, loads)
        assertFalse(viewModel.uiState.value.isConversationRecovering)
        assertTrue(viewModel.uiState.value.acceptedUserMessages.isEmpty())
        assertEquals(2, subscriptions)
    }

    @Test
    fun `ten page image turn uploads in order then sends durable asset inputs with stable ids`() =
        runTest(dispatcher) {
            val gateway = FakeGateway(listOf(thread("thread-a"))).apply {
                localImageCapability = testImageCapability()
            }
            val sources = (1..10).map { index -> byteArrayOf(index.toByte(), 2, 3, 4) }
            val attachments = sources.mapIndexed { index, source ->
                imageAttachment("asset-client-${index + 1}", source)
            }
            val reader = FakeImageReader(attachments)
            val outbox = InMemoryDurableOutboxStore()
            val viewModel = AppViewModel(
                gateway,
                InMemoryLastThreadStore(),
                outbox,
                imageAttachmentReader = reader,
            )
            advanceUntilIdle()

            viewModel.selectImages(
                (1..10).map { "content://not-persisted/source-$it" },
                requireNotNull(viewModel.createImageSelectionTicket()),
            )
            advanceUntilIdle()
            assertEquals(10, viewModel.uiState.value.selectedImages.size)
            assertTrue(viewModel.send(""))
            advanceUntilIdle()

            assertEquals((1..10).map { "asset-client-$it" }, gateway.uploadedAssetIds)
            assertEquals(1, gateway.sentInputs.size)
            assertEquals(
                List(10) { TurnInput.Image("ast_abcdefghijklmnopqrstuvwxyz0123456789") },
                gateway.sentInputs.single(),
            )
            assertEquals(List(10) { gateway.sentIds.single() }, gateway.uploadedMessageIds)
            assertEquals(OutboxState.ACCEPTED, outbox.list().single().state)
            assertEquals(
                "turn-${gateway.sentIds.single()}",
                outbox.list().single().acceptedTurnId,
            )
            assertTrue(sources.all { source -> source.all { it == 0.toByte() } })
            assertTrue(viewModel.uiState.value.selectedImages.isEmpty())
        }

    @Test
    fun `upload failure never sends text alone and leaves durable reselect marker`() =
        runTest(dispatcher) {
            val gateway = FakeGateway(listOf(thread("thread-a"))).apply {
                localImageCapability = testImageCapability()
                uploadError = GatewayTransportException("offline")
            }
            val source = byteArrayOf(8, 7, 6)
            val outbox = InMemoryDurableOutboxStore()
            val viewModel = AppViewModel(
                gateway,
                InMemoryLastThreadStore(),
                outbox,
                imageAttachmentReader = FakeImageReader(listOf(imageAttachment("asset-client-2", source))),
            )
            advanceUntilIdle()

            viewModel.selectImages(
                listOf("content://temporary"),
                requireNotNull(viewModel.createImageSelectionTicket()),
            )
            advanceUntilIdle()
            assertTrue(viewModel.send("do not send alone"))
            advanceUntilIdle()

            assertTrue(gateway.sentIds.isEmpty())
            assertEquals(OutboxState.NEEDS_ATTACHMENTS, outbox.list().single().state)
            assertEquals(1, outbox.list().single().pendingImageCount)
            assertTrue(outbox.list().single().imageAssetRefs.isEmpty())
            assertTrue(viewModel.uiState.value.attachmentError.orEmpty().contains("文字不会单独发送"))
            assertTrue(source.all { it == 0.toByte() })
        }

    @Test
    fun `restart does not dispatch marker that has no durable asset reference`() = runTest(dispatcher) {
        val marker = OutboxEntry(
            clientMessageId = "client-image-message-001",
            sessionScope = "scope-a",
            threadKey = ThreadKey("thread-a"),
            text = "inspect",
            state = OutboxState.NEEDS_ATTACHMENTS,
            createdAtMillis = 1,
            pendingImageCount = 2,
        )
        val outbox = InMemoryDurableOutboxStore(listOf(marker))
        val gateway = FakeGateway(listOf(thread("thread-a")))

        val restarted = AppViewModel(gateway, InMemoryLastThreadStore(), outbox)
        advanceUntilIdle()

        assertTrue(gateway.sentIds.isEmpty())
        assertEquals(marker, restarted.uiState.value.outbox.single())
    }

    @Test
    fun `same scope generation switch cancels upload and cannot send A input into B`() =
        runTest(dispatcher) {
            val uploadGate = CompletableDeferred<Unit>()
            val gateway = FakeGateway(listOf(thread("thread-a"))).apply {
                localImageCapability = testImageCapability()
                imageUploadGate = uploadGate
            }
            val source = byteArrayOf(4, 5, 6)
            val outbox = InMemoryDurableOutboxStore()
            val viewModel = AppViewModel(
                gateway,
                InMemoryLastThreadStore(),
                outbox,
                imageAttachmentReader = FakeImageReader(listOf(imageAttachment("asset-client-3", source))),
            )
            advanceUntilIdle()
            viewModel.selectImages(
                listOf("content://temporary"),
                requireNotNull(viewModel.createImageSelectionTicket()),
            )
            advanceUntilIdle()
            assertTrue(viewModel.send("belongs to A"))
            runCurrent()
            gateway.imageUploadStarted.await()

            gateway.switchSession("scope-a")
            runCurrent()
            uploadGate.complete(Unit)
            advanceUntilIdle()

            assertTrue(gateway.sentIds.isEmpty())
            assertEquals(0, viewModel.uiState.value.outbox.size)
            assertTrue(outbox.list().isEmpty())
            assertTrue(source.all { it == 0.toByte() })
        }

    @Test
    fun `missing capability fails closed before picker reader or image send`() = runTest(dispatcher) {
        val reader = FakeImageReader(listOf(imageAttachment("asset-client-4", byteArrayOf(1))))
        val viewModel = AppViewModel(
            FakeGateway(listOf(thread("thread-a"))),
            InMemoryLastThreadStore(),
            imageAttachmentReader = reader,
        )
        advanceUntilIdle()

        val ticket = viewModel.createImageSelectionTicket()
        if (ticket != null) viewModel.selectImages(listOf("content://temporary"), ticket)
        advanceUntilIdle()

        assertEquals(0, reader.calls)
        assertTrue(viewModel.uiState.value.selectedImages.isEmpty())
        assertEquals(null, viewModel.uiState.value.localImageCapability)
    }

    @Test
    fun `picker callback from A is rejected after switching to B and back to same public thread`() =
        runTest(dispatcher) {
            val gateway = FakeGateway(listOf(thread("thread-a"), thread("thread-b"))).apply {
                localImageCapability = testImageCapability()
            }
            val reader = FakeImageReader(listOf(imageAttachment("asset-client-5", byteArrayOf(1))))
            val viewModel = AppViewModel(
                gateway,
                InMemoryLastThreadStore(ThreadKey("thread-a")),
                imageAttachmentReader = reader,
            )
            advanceUntilIdle()
            val staleTicket = requireNotNull(viewModel.createImageSelectionTicket())

            viewModel.selectThread(ThreadKey("thread-b"))
            advanceUntilIdle()
            viewModel.selectThread(ThreadKey("thread-a"))
            advanceUntilIdle()
            viewModel.selectImages(listOf("content://stale-callback"), staleTicket)
            advanceUntilIdle()

            assertEquals(0, reader.calls)
            assertTrue(viewModel.uiState.value.selectedImages.isEmpty())
        }

    @Test
    fun `ViewModel owned picker request survives screen recreation callback handoff`() =
        runTest(dispatcher) {
            val gateway = FakeGateway(listOf(thread("thread-a"))).apply {
                localImageCapability = testImageCapability()
            }
            val reader = FakeImageReader(listOf(imageAttachment("asset-after-recreate", byteArrayOf(1))))
            val viewModel = AppViewModel(
                gateway,
                InMemoryLastThreadStore(ThreadKey("thread-a")),
                imageAttachmentReader = reader,
            )
            advanceUntilIdle()

            assertTrue(viewModel.beginImageSelection())
            // A new Activity/Composition receives the callback through the same ViewModel.
            viewModel.completeImageSelection(listOf("content://callback-after-activity-recreation"))
            advanceUntilIdle()

            assertEquals(1, reader.calls)
            assertEquals("asset-after-recreate", viewModel.uiState.value.selectedImages.single().clientAssetId)

            // The ActivityResult is single-consumption and cannot be replayed by another callback.
            viewModel.completeImageSelection(listOf("content://duplicate-callback"))
            advanceUntilIdle()
            assertEquals(1, reader.calls)
        }

    @Test
    fun `thread UI registry isolates draft and scroll per public thread`() {
        val registry = ThreadUiStateRegistry()
        val a = ThreadUiKey("scope-a", 0, ThreadKey("a"))
        val b = ThreadUiKey("scope-a", 0, ThreadKey("b"))
        val sameThreadNewContainer = ThreadUiKey("scope-b", 1, ThreadKey("a"))

        registry.updateDraft(a, "draft a")
        registry.updateDraft(b, "draft b")
        registry.saveScroll(a, 12, 40)
        registry.saveScroll(b, 3, 5)

        assertEquals("draft a", registry.draft(a))
        assertEquals("draft b", registry.draft(b))
        assertEquals(ThreadScrollPosition(12, 40), registry.scroll(a))
        assertEquals(ThreadScrollPosition(3, 5), registry.scroll(b))
        assertEquals("", registry.draft(sameThreadNewContainer))
        assertEquals(ThreadScrollPosition(), registry.scroll(sameThreadNewContainer))
    }

    @Test
    fun `timeline follows final answer appended after a tall media card`() {
        assertTrue(
            shouldFollowTimelineChange(
                previousTimelineSize = 7,
                currentTimelineSize = 8,
                lastVisibleItemIndex = 7,
            ),
        )
        assertFalse(
            shouldFollowTimelineChange(
                previousTimelineSize = 7,
                currentTimelineSize = 8,
                lastVisibleItemIndex = 6,
            ),
        )
        assertTrue(
            shouldFollowTimelineChange(
                previousTimelineSize = 0,
                currentTimelineSize = 1,
                lastVisibleItemIndex = -1,
            ),
        )
        assertFalse(
            shouldFollowTimelineChange(
                previousTimelineSize = 1,
                currentTimelineSize = 0,
                lastVisibleItemIndex = 1,
            ),
        )
        assertTrue(
            shouldFollowTimelineChange(
                previousTimelineSize = 8,
                currentTimelineSize = 9,
                lastVisibleItemIndex = 6,
                wasFollowingLatest = true,
            ),
        )
    }

    @Test
    fun `canonical failed user history and matching outbox render only one identity`() {
        val block = TextBlock("text", "same prompt")
        val canonical = RichMessage(
            id = "user-history",
            role = MessageRole.USER,
            state = RichMessageState.FAILED,
            revision = 0,
            rootBlockIds = listOf(block.id),
            blocks = mapOf(block.id to block),
            createdAtMillis = 1,
            clientMessageId = "same-client-id",
        )
        val pending = OutboxEntry(
            "same-client-id",
            "scope-a",
            ThreadKey("a"),
            "same prompt",
            OutboxState.RETRYABLE,
            1,
        )

        assertTrue(visibleConversationMessages(listOf(canonical), listOf(pending)).isEmpty())
        val withoutClientIdentity = canonical.copy(clientMessageId = null)
        assertTrue(
            visibleConversationMessages(listOf(withoutClientIdentity), listOf(pending)).isEmpty(),
        )
    }

    @Test
    fun `accepted user stays before a live assistant response`() {
        val threadKey = ThreadKey("thread-timeline")
        val assistant = RichMessage(
            id = "assistant-live",
            role = MessageRole.ASSISTANT,
            state = RichMessageState.COMPLETED,
            revision = 1,
            rootBlockIds = emptyList(),
            blocks = emptyMap(),
            createdAtMillis = 2_000,
        )
        val accepted = AcceptedUserMessage(
            clientMessageId = "client-live",
            sessionScope = "scope-live",
            sessionGeneration = 1,
            threadKey = threadKey,
            text = "hi",
            createdAtMillis = 1_000,
        )

        val timeline = conversationTimeline(
            messages = listOf(assistant),
            accepted = listOf(accepted),
            pending = emptyList(),
        )

        assertEquals(
            listOf("accepted-client-live", "message-assistant-live"),
            timeline.map(ThreadTimelineEntry::key),
        )
    }

    @Test
    fun `conversation timeline folds process messages and preserves final answer order`() {
        fun message(
            id: String,
            role: MessageRole,
            presentation: MessagePresentation = MessagePresentation.STANDARD,
        ): RichMessage {
            val block = TextBlock("$id-text", id)
            return RichMessage(
                id = id,
                role = role,
                state = RichMessageState.COMPLETED,
                revision = 0,
                rootBlockIds = listOf(block.id),
                blocks = mapOf(block.id to block),
                createdAtMillis = 1_000,
                turnId = "turn-1",
                presentation = presentation,
            )
        }
        val user = message("question", MessageRole.USER)
        val commentary = message("commentary", MessageRole.ASSISTANT, MessagePresentation.PROCESS)
        val tool = message("tool", MessageRole.ASSISTANT, MessagePresentation.PROCESS)
        val answer = message("answer", MessageRole.ASSISTANT, MessagePresentation.FINAL_ANSWER)

        val timeline = conversationTimeline(
            messages = listOf(user, commentary, tool, answer),
            accepted = emptyList(),
            pending = emptyList(),
        )

        assertEquals(
            listOf("message-question", "process-turn-1", "message-answer"),
            timeline.map(ThreadTimelineEntry::key),
        )
        assertEquals(
            listOf("commentary", "tool"),
            (timeline[1] as ThreadTimelineEntry.Process).messages.map(RichMessage::id),
        )
    }

    private class FakeGateway(initialThreads: List<CodexThread>) :
        CodexGateway,
        CloseableCodexGateway {
        private var sessionScopeValue: String = "scope-a"
        val mutableSessionIdentity = MutableStateFlow(GatewaySessionIdentity(sessionScopeValue, 0))
        override val sessionIdentity: StateFlow<GatewaySessionIdentity> = mutableSessionIdentity
        override var sessionScope: String
            get() = sessionScopeValue
            set(value) {
                switchSession(value)
            }

        fun switchSession(
            scope: String = sessionScopeValue,
            carryOverSameScopeOutbox: Boolean = false,
        ) {
            val previous = mutableSessionIdentity.value
            sessionScopeValue = scope
            mutableSessionIdentity.value = GatewaySessionIdentity(
                scope = scope,
                generation = previous.generation + 1,
                outboxCarryoverFromGeneration = previous.generation.takeIf {
                    carryOverSameScopeOutbox && previous.scope == scope
                },
            )
        }
        val mutableThreads = MutableStateFlow(initialThreads)
        override val threads: StateFlow<List<CodexThread>> = mutableThreads
        val mutableConnection = MutableStateFlow(GatewayConnectionState.ONLINE)
        override val connectionState: StateFlow<GatewayConnectionState> = mutableConnection
        val mutableKind = MutableStateFlow(GatewayKind.REMOTE)
        override val kind: StateFlow<GatewayKind> = mutableKind
        var failSend = false
        var sendError: GatewayTransportException? = null
        var actionError: GatewayTransportException? = null
        var sendReturnBarrier: CompletableDeferred<Unit>? = null
        val sentIds = mutableListOf<String>()
        val sentThreadKeys = mutableListOf<ThreadKey>()
        val sentScopes = mutableListOf<String>()
        val performedActions = mutableListOf<String>()
        val interruptedTurns = mutableListOf<Pair<ThreadKey, String?>>()
        val closeCalls = AtomicInteger()
        var localImageCapability: LocalImageCapability? = null
        var uploadError: GatewayTransportException? = null
        var submissionStatus: String = "running"
        var loadCalls: Int = 0
        var imageUploadGate: CompletableDeferred<Unit>? = null
        val imageUploadStarted = CompletableDeferred<Unit>()
        val uploadedAssetIds = mutableListOf<String>()
        val uploadedMessageIds = mutableListOf<String>()
        val sentInputs = mutableListOf<List<TurnInput>>()
        val observed = mutableListOf<Triple<ThreadKey, String, Long>>()
        var observer: (ThreadKey, String, Long) -> Flow<ConversationEvent> = { _, _, _ -> emptyFlow() }
        var loader: suspend (ThreadKey) -> ConversationSnapshot = { key ->
            ConversationSnapshot(key, "stream-${key.value}", 0, emptyList())
        }

        override suspend fun loadConversation(threadKey: ThreadKey): ConversationSnapshot = loader(threadKey)

        override suspend fun loadCapabilities(
            expectedSessionScope: String,
            expectedSessionGeneration: Long,
        ): GatewayCapabilities = GatewayCapabilities(localImageCapability)

        override suspend fun uploadTurnImage(
            threadKey: ThreadKey,
            clientMessageId: String,
            clientAssetId: String,
            bytes: ByteArray,
            expectedSessionScope: String,
            expectedSessionGeneration: Long,
        ): UploadedImageAsset {
            imageUploadStarted.complete(Unit)
            imageUploadGate?.await()
            uploadError?.let { throw it }
            if (
                sessionIdentity.value.scope != expectedSessionScope ||
                sessionIdentity.value.generation != expectedSessionGeneration
            ) {
                throw GatewayTransportException("session changed")
            }
            uploadedAssetIds += clientAssetId
            uploadedMessageIds += clientMessageId
            return UploadedImageAsset(
                "ast_abcdefghijklmnopqrstuvwxyz0123456789",
                "image/png",
                16,
                12,
                bytes.size,
            )
        }

        override suspend fun sendTurnInput(
            threadKey: ThreadKey,
            input: List<TurnInput>,
            idempotencyKey: String,
            expectedSessionScope: String,
            expectedSessionGeneration: Long,
        ): TurnSubmission {
            if (
                sessionIdentity.value.scope != expectedSessionScope ||
                sessionIdentity.value.generation != expectedSessionGeneration
            ) {
                throw GatewayTransportException("session changed")
            }
            sentInputs += input
            sentThreadKeys += threadKey
            recordSend(idempotencyKey, expectedSessionScope)
            return TurnSubmission(
                "turn-$idempotencyKey",
                threadKey,
                idempotencyKey,
                submissionStatus,
            )
        }

        override fun observeConversation(
            threadKey: ThreadKey,
            afterStreamId: String,
            afterStreamSeq: Long,
        ): Flow<ConversationEvent> {
            observed += Triple(threadKey, afterStreamId, afterStreamSeq)
            return observer(threadKey, afterStreamId, afterStreamSeq)
        }

        override suspend fun sendTurn(
            threadKey: ThreadKey,
            text: String,
            idempotencyKey: String,
            expectedSessionScope: String,
        ): TurnSubmission {
            recordSend(idempotencyKey, expectedSessionScope)
            return TurnSubmission(
                "turn-$idempotencyKey",
                threadKey,
                idempotencyKey,
                submissionStatus,
            )
        }

        private suspend fun recordSend(idempotencyKey: String, expectedSessionScope: String) {
            sentIds += idempotencyKey
            sentScopes += expectedSessionScope
            sendReturnBarrier?.await()
            sendError?.let { throw it }
            if (failSend) throw GatewayRequestException("NODE_OFFLINE", retryable = true)
        }

        override suspend fun interruptTurn(
            threadKey: ThreadKey,
            expectedTurnId: String?,
            expectedSessionScope: String,
            expectedSessionGeneration: Long,
        ) {
            check(expectedSessionScope == sessionScope)
            check(expectedSessionGeneration == sessionIdentity.value.generation)
            interruptedTurns += threadKey to expectedTurnId
        }

        override suspend fun createThread(title: String): CodexThread = error("unused")
        override suspend fun renameThread(threadKey: ThreadKey, title: String) = Unit
        override suspend fun archiveThread(threadKey: ThreadKey) = Unit
        override suspend fun deleteThread(threadKey: ThreadKey) = Unit
        override suspend fun performAction(actionRef: String, expectedSessionScope: String) {
            performedActions += actionRef
            actionError?.let { throw it }
        }

        override fun close() {
            closeCalls.incrementAndGet()
        }
    }

    private class FakeImageReader(
        private val result: List<PendingImageAttachment>,
    ) : ImageAttachmentReader {
        var calls = 0

        override suspend fun read(
            sourceUris: List<String>,
            capability: LocalImageCapability,
        ): List<PendingImageAttachment> {
            calls += 1
            return result
        }
    }

    private fun imageAttachment(id: String, source: ByteArray) = PendingImageAttachment(
        clientAssetId = id,
        mediaType = "image/png",
        sourceBytes = source,
        thumbnailBytes = byteArrayOf(1, 2),
        sourceWidth = 16,
        sourceHeight = 12,
    )

    private fun testImageCapability() = LocalImageCapability(
        uploadVersion = 1,
        mediaTypes = setOf("image/jpeg", "image/png"),
        maxUploadBytes = 8 * 1024 * 1024,
        maxPixels = 25_000_000,
        maxEdgePixels = 12_000,
        maxImagesPerTurn = 10,
    )

    private fun thread(
        id: String,
        state: ThreadRunState = ThreadRunState.IDLE,
        lastTurnId: String? = null,
    ) = CodexThread(
        key = ThreadKey(id),
        title = id,
        preview = "",
        updatedAtMillis = 1,
        state = state,
        lastTurnId = lastTurnId,
    )
}
