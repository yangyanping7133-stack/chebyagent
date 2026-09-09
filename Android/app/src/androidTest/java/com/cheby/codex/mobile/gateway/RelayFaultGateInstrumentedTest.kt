package com.cheby.codex.mobile.gateway

import android.os.Bundle
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.ViewModelStore
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.cheby.codex.mobile.data.InMemoryLastThreadStore
import com.cheby.codex.mobile.data.OutboxEntry
import com.cheby.codex.mobile.data.OutboxState
import com.cheby.codex.mobile.data.SharedPreferencesDurableOutboxStore
import com.cheby.codex.mobile.model.GatewayConnectionState
import com.cheby.codex.mobile.model.MessageRole
import com.cheby.codex.mobile.model.RichMessage
import com.cheby.codex.mobile.model.RichMessageState
import com.cheby.codex.mobile.model.ThreadKey
import com.cheby.codex.mobile.model.orderedBlocks
import com.cheby.codex.mobile.model.readableText
import com.cheby.codex.mobile.ui.AppViewModel
import java.io.File
import java.security.MessageDigest
import java.util.UUID
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Opt-in real-device fault gate through the production AppViewModel and durable Outbox.
 *
 * The app-kill prepare phase never completes: after the host causally stops Gate Edge, Android
 * enqueues A1/A2/B1 while offline, proves all three business keys remain QUEUED, persists their
 * identities, emits the kill checkpoint and waits forever. The host must force-stop this live
 * instrumentation process. A separate recovery invocation then proves exactly one canonical Turn
 * and bubble pair per business key, same-Thread FIFO, other-Thread progress and post-recovery A3/B2.
 *
 * Every transport cycle is fault-held queued recovery: the host action goes down, Android observes
 * offline, and only then are A1/A2/B1 durably enqueued as QUEUED. This does not cross either the
 * NETWORK_SENT or ACCEPTED crash boundary and must not be counted as evidence for those boundaries.
 * The host then restores the component before recovery validation and A3/B2.
 */
@RunWith(AndroidJUnit4::class)
class RelayFaultGateInstrumentedTest {
    @Test
    fun prepareAppKillRecoveryPhase(): Unit = runBlocking {
        val arguments = InstrumentationRegistry.getArguments()
        assumeTrue(
            "App-kill prepare is opt-in",
            arguments.getString(PHASE_ARGUMENT) == PHASE_APP_KILL_PREPARE,
        )
        val runToken = arguments.requireHex(RUN_TOKEN_ARGUMENT)
        val actionNonce = arguments.requireHex(ACTION_NONCE_ARGUMENT)
        val iteration = arguments.requireIteration()
        val context = InstrumentationRegistry.getInstrumentation()
            .targetContext.applicationContext
        val stateFile = File(context.filesDir, APP_KILL_STATE_FILE)
        val acknowledgementFile = File(context.filesDir, HOST_ACK_FILE)
        assertTrue(!stateFile.exists() || stateFile.delete())
        assertTrue(!acknowledgementFile.exists() || acknowledgementFile.delete())

        val gateway = GatewayFactory.create(context)
        requirePairedOnline(gateway)
        val identity = gateway.sessionIdentity.value
        val outboxStore = SharedPreferencesDurableOutboxStore(context)
        requireGlobalOutboxCount(outboxStore, 0, "before app-kill prepare")
        val threadA = gateway.createThread(
            "Gate app A ${runToken.takeLast(6)} $iteration",
        )
        val threadB = gateway.createThread(
            "Gate app B ${runToken.takeLast(6)} $iteration",
        )
        val ownerA = createViewModel(gateway, outboxStore, threadA.key)
        val ownerB = createViewModel(
            NonClosingGateway(gateway),
            outboxStore,
            threadB.key,
        )
        awaitSelected(ownerA.model, threadA.key)
        awaitSelected(ownerB.model, threadB.key)

        awaitHostDown(
            gateways = listOf(gateway),
            acknowledgementFile = acknowledgementFile,
            checkpoint = CHECKPOINT_APP_PREPARE_OFFLINE_REQUIRED,
            nonce = actionNonce,
            component = COMPONENT_EDGE,
            iteration = iteration,
        )
        val planned = enqueueOfflineBatch(
            ownerA.model,
            ownerB.model,
            outboxStore,
            identity,
            threadA.key,
            threadB.key,
            runToken,
            iteration,
            "APP",
        )
        val heldOutboxEntries = requireGlobalOutboxCount(
            outboxStore,
            planned.size,
            "at app-kill checkpoint",
        )
        onMain { ownerB.store.clear() }
        val record = AppKillRecord(
            runToken = runToken,
            iteration = iteration,
            actionNonce = actionNonce,
            scope = identity.scope,
            generation = identity.generation,
            threadA = threadA.key.value,
            threadB = threadB.key.value,
            messages = planned,
        )
        writePrivateState(stateFile, record)
        signal(
            checkpoint = CHECKPOINT_APP_KILL_OUTBOX_READY,
            nonce = actionNonce,
            component = COMPONENT_APP,
            iteration = iteration,
            phaseDigest = record.digest(),
            turns = 0,
            outboxEntries = heldOutboxEntries,
            duplicateBubbles = 0,
            actionObserved = false,
        )

        // Passing this point without an OS force-stop is a gate failure. The host runner accepts
        // only the checkpoint followed by process termination and never a normal JUnit final code.
        awaitCancellation()
    }

    @Test
    fun recoverAppKillRecoveryPhase() = runBlocking {
        val arguments = InstrumentationRegistry.getArguments()
        assumeTrue(
            "App-kill recovery is opt-in",
            arguments.getString(PHASE_ARGUMENT) == PHASE_APP_KILL_RECOVER,
        )
        val runToken = arguments.requireHex(RUN_TOKEN_ARGUMENT)
        val actionNonce = arguments.requireHex(ACTION_NONCE_ARGUMENT)
        val iteration = arguments.requireIteration()
        val expectedReply = arguments.requireExpectedReply()
        val context = InstrumentationRegistry.getInstrumentation()
            .targetContext.applicationContext
        val stateFile = File(context.filesDir, APP_KILL_STATE_FILE)
        val record = readPrivateState(stateFile)
        require(
            record.runToken == runToken &&
                record.iteration == iteration &&
                record.actionNonce == actionNonce
        ) {
            "Recovery invocation did not match the killed durable phase"
        }
        val threadA = ThreadKey(record.threadA)
        val threadB = ThreadKey(record.threadB)
        val outboxStore = SharedPreferencesDurableOutboxStore(context)
        var owner: ViewModelOwner? = null
        try {
            val gateway = GatewayFactory.create(context)
            requirePairedOnline(gateway)
            val identity = gateway.sessionIdentity.value
            assertEquals(record.scope, identity.scope)
            assertEquals(record.generation, identity.generation)
            owner = createViewModel(gateway, outboxStore, threadA)
            val recovered = awaitBatchRecovered(
                owner.model,
                outboxStore,
                identity,
                record.messages,
                expectedReply,
            )
            assertEquals(3, recovered.map(CompletedTurn::turnId).distinct().size)
            val post = enqueueOnlinePostRecovery(
                owner.model,
                outboxStore,
                identity,
                threadA,
                threadB,
                runToken,
                iteration,
                "APP",
            )
            val completedPost = post.map {
                awaitCompletedTurn(
                    owner.model,
                    outboxStore,
                    identity,
                    it,
                    expectedReply,
                )
            }
            val finalOutboxEntries = requireGlobalOutboxCount(
                outboxStore,
                0,
                "after app-kill recovery",
            )
            assertBusinessKeysRemoved(
                outboxStore,
                record.messages + post,
                "after app-kill recovery",
            )
            assertTrue(stateFile.delete())
            signal(
                checkpoint = CHECKPOINT_APP_KILL_RECOVERED,
                nonce = actionNonce,
                component = COMPONENT_APP,
                iteration = iteration,
                phaseDigest = record.digest(),
                turns = recovered.size + post.size,
                outboxEntries = finalOutboxEntries,
                duplicateBubbles = (recovered + completedPost)
                    .sumOf(CompletedTurn::duplicateBubbleCount),
                actionObserved = true,
            )
        } finally {
            owner?.let { onMain { it.store.clear() } }
            cleanupThreads(context, listOf(threadA, threadB))
        }
    }

    @Test
    fun recoverFaultHeldQueuedTurnsAfterCausalTransportFaults() = runBlocking {
        val arguments = InstrumentationRegistry.getArguments()
        assumeTrue(
            "Transport fault gate is opt-in",
            arguments.getString(PHASE_ARGUMENT) == PHASE_TRANSPORT,
        )
        val runToken = arguments.requireHex(RUN_TOKEN_ARGUMENT)
        val repetitions = arguments.requireRepetitions()
        val expectedReply = arguments.requireExpectedReply()
        val context = InstrumentationRegistry.getInstrumentation()
            .targetContext.applicationContext
        val acknowledgementFile = File(context.filesDir, HOST_ACK_FILE)
        assertTrue(!acknowledgementFile.exists() || acknowledgementFile.delete())
        var completedTurns = 0
        var completedDuplicateBubbles = 0

        repeat(repetitions) { zeroBasedIteration ->
            val iteration = zeroBasedIteration + 1
            for (component in FAULT_COMPONENTS) {
                val gateway = GatewayFactory.create(context)
                requirePairedOnline(gateway)
                val identity = gateway.sessionIdentity.value
                val outboxStore = SharedPreferencesDurableOutboxStore(context)
                requireGlobalOutboxCount(outboxStore, 0, "before $component fault")
                val threadA = gateway.createThread(
                    "Gate $component A ${runToken.takeLast(6)} $iteration",
                )
                val threadB = gateway.createThread(
                    "Gate $component B ${runToken.takeLast(6)} $iteration",
                )
                val ownerA = createViewModel(gateway, outboxStore, threadA.key)
                val ownerB = createViewModel(
                    NonClosingGateway(gateway),
                    outboxStore,
                    threadB.key,
                )
                awaitSelected(ownerA.model, threadA.key)
                awaitSelected(ownerB.model, threadB.key)
                val nonce = UUID.randomUUID().toString().replace("-", "").lowercase()
                try {
                    awaitHostDown(
                        gateways = listOf(gateway),
                        acknowledgementFile = acknowledgementFile,
                        checkpoint = CHECKPOINT_FAULT_DOWN_REQUIRED,
                        nonce = nonce,
                        component = component,
                        iteration = iteration,
                    )
                    val planned = enqueueOfflineBatch(
                        ownerA.model,
                        ownerB.model,
                        outboxStore,
                        identity,
                        threadA.key,
                        threadB.key,
                        runToken,
                        iteration,
                        component.uppercase(),
                    )
                    val heldOutboxEntries = requireGlobalOutboxCount(
                        outboxStore,
                        planned.size,
                        "at $component Outbox checkpoint",
                    )
                    onMain { ownerB.store.clear() }
                    signal(
                        checkpoint = CHECKPOINT_FAULT_OUTBOX_READY,
                        nonce = nonce,
                        component = component,
                        iteration = iteration,
                        turns = 0,
                        outboxEntries = heldOutboxEntries,
                        duplicateBubbles = 0,
                        actionObserved = false,
                    )
                    awaitHostAcknowledgement(
                        acknowledgementFile,
                        expected = "$nonce|$component|$iteration|outbox-held",
                    )
                    signal(
                        checkpoint = CHECKPOINT_FAULT_UP_REQUIRED,
                        nonce = nonce,
                        component = component,
                        iteration = iteration,
                        turns = 0,
                        outboxEntries = heldOutboxEntries,
                        duplicateBubbles = 0,
                        actionObserved = false,
                    )
                    awaitHostAcknowledgement(
                        acknowledgementFile,
                        expected = "$nonce|$component|$iteration|" +
                            if (component == COMPONENT_WIFI) "enabled" else "started",
                    )
                    awaitOnline(gateway)
                    val recovered = awaitBatchRecovered(
                        ownerA.model,
                        outboxStore,
                        identity,
                        planned,
                        expectedReply,
                    )
                    assertEquals(
                        "A business key created more than one canonical Turn",
                        planned.size,
                        recovered.map(CompletedTurn::turnId).distinct().size,
                    )
                    val post = enqueueOnlinePostRecovery(
                        ownerA.model,
                        outboxStore,
                        identity,
                        threadA.key,
                        threadB.key,
                        runToken,
                        iteration,
                        component.uppercase(),
                    )
                    val completedPost = post.map {
                        awaitCompletedTurn(
                            ownerA.model,
                            outboxStore,
                            identity,
                            it,
                            expectedReply,
                        )
                    }
                    val finalOutboxEntries = requireGlobalOutboxCount(
                        outboxStore,
                        0,
                        "after $component fault",
                    )
                    assertBusinessKeysRemoved(
                        outboxStore,
                        planned + post,
                        "after $component fault",
                    )
                    completedTurns += recovered.size + post.size
                    val cycleDuplicateBubbles = (recovered + completedPost)
                        .sumOf(CompletedTurn::duplicateBubbleCount)
                    completedDuplicateBubbles += cycleDuplicateBubbles
                    signal(
                        checkpoint = CHECKPOINT_FAULT_RECOVERED,
                        nonce = nonce,
                        component = component,
                        iteration = iteration,
                        turns = recovered.size + post.size,
                        outboxEntries = finalOutboxEntries,
                        duplicateBubbles = cycleDuplicateBubbles,
                        actionObserved = true,
                    )
                } finally {
                    onMain { ownerB.store.clear() }
                    onMain { ownerA.store.clear() }
                    cleanupThreads(context, listOf(threadA.key, threadB.key))
                }
            }
        }
        assertEquals(repetitions * FAULT_COMPONENTS.size * 5, completedTurns)
        val finalOutboxEntries = requireGlobalOutboxCount(
            SharedPreferencesDurableOutboxStore(context),
            0,
            "at fault gate completion",
        )
        signal(
            checkpoint = CHECKPOINT_FAULT_GATE_COMPLETE,
            nonce = UUID.randomUUID().toString().replace("-", "").lowercase(),
            component = COMPONENT_ALL,
            iteration = repetitions,
            turns = completedTurns,
            outboxEntries = finalOutboxEntries,
            duplicateBubbles = completedDuplicateBubbles,
            actionObserved = true,
        )
    }

    private suspend fun awaitHostDown(
        gateways: List<CodexGateway>,
        acknowledgementFile: File,
        checkpoint: String,
        nonce: String,
        component: String,
        iteration: Int,
    ) = coroutineScope {
        require(
            gateways.isNotEmpty() &&
                gateways.all {
                    it.connectionState.value == GatewayConnectionState.ONLINE
                },
        )
        assertTrue(!acknowledgementFile.exists() || acknowledgementFile.delete())
        val offline = gateways.map { gateway ->
            async(start = CoroutineStart.UNDISPATCHED) {
                withTimeout(OFFLINE_TIMEOUT_MILLIS) {
                    gateway.connectionState.first {
                        it != GatewayConnectionState.ONLINE
                    }
                }
            }
        }
        signal(
            checkpoint = checkpoint,
            nonce = nonce,
            component = component,
            iteration = iteration,
            turns = 0,
            outboxEntries = 0,
            actionObserved = false,
        )
        awaitHostAcknowledgement(
            acknowledgementFile,
            expected = "$nonce|$component|$iteration|" +
                if (component == COMPONENT_WIFI) "disabled" else "stopped",
        )
        offline.forEach { it.await() }
    }

    private suspend fun enqueueOfflineBatch(
        modelA: AppViewModel,
        modelB: AppViewModel,
        outboxStore: SharedPreferencesDurableOutboxStore,
        identity: GatewaySessionIdentity,
        threadA: ThreadKey,
        threadB: ThreadKey,
        runToken: String,
        iteration: Int,
        label: String,
    ): List<PlannedTurn> {
        val planned = listOf(
            enqueueProductionTurn(
                modelA,
                outboxStore,
                identity,
                threadA,
                "FAULT-$runToken-$label-$iteration-A1",
                1,
            ),
            enqueueProductionTurn(
                modelA,
                outboxStore,
                identity,
                threadA,
                "FAULT-$runToken-$label-$iteration-A2",
                2,
            ),
            enqueueProductionTurn(
                modelB,
                outboxStore,
                identity,
                threadB,
                "FAULT-$runToken-$label-$iteration-B1",
                1,
            ),
        )
        val queued = activeOutbox(outboxStore, identity).filter {
            it.clientMessageId in planned.mapTo(mutableSetOf(), PlannedTurn::clientMessageId)
        }
        assertEquals("The offline batch lost a durable Outbox row", planned.size, queued.size)
        assertTrue(
            "The fault checkpoint did not hold a QUEUED production business key",
            queued.all { it.state == OutboxState.QUEUED },
        )
        return planned
    }

    private suspend fun enqueueOnlinePostRecovery(
        model: AppViewModel,
        outboxStore: SharedPreferencesDurableOutboxStore,
        identity: GatewaySessionIdentity,
        threadA: ThreadKey,
        threadB: ThreadKey,
        runToken: String,
        iteration: Int,
        label: String,
    ): List<PlannedTurn> = listOf(
        enqueueProductionTurn(
            model,
            outboxStore,
            identity,
            threadA,
            "FAULT-$runToken-$label-$iteration-A3",
            3,
        ),
        enqueueProductionTurn(
            model,
            outboxStore,
            identity,
            threadB,
            "FAULT-$runToken-$label-$iteration-B2",
            2,
        ),
    )

    private suspend fun enqueueProductionTurn(
        model: AppViewModel,
        outboxStore: SharedPreferencesDurableOutboxStore,
        identity: GatewaySessionIdentity,
        threadKey: ThreadKey,
        marker: String,
        threadOrdinal: Int,
    ): PlannedTurn {
        awaitSelected(model, threadKey)
        val entry = onMain {
            assertTrue("Production composer rejected a fault-gate Turn", model.send(marker))
            requireNotNull(
                outboxStore.list().singleOrNull {
                    it.sessionScope == identity.scope &&
                        it.sessionGeneration == identity.generation &&
                        it.threadKey == threadKey &&
                        it.text == marker
                },
            ) {
                "Production composer did not durably enqueue its business key"
            }
        }
        return PlannedTurn(
            threadId = threadKey.value,
            clientMessageId = entry.clientMessageId,
            marker = marker,
            threadOrdinal = threadOrdinal,
        )
    }

    private suspend fun awaitBatchRecovered(
        model: AppViewModel,
        outboxStore: SharedPreferencesDurableOutboxStore,
        identity: GatewaySessionIdentity,
        planned: List<PlannedTurn>,
        expectedReply: String,
    ): List<CompletedTurn> {
        val completed = planned.map {
            awaitCompletedTurn(model, outboxStore, identity, it, expectedReply)
        }
        planned.groupBy(PlannedTurn::threadId).forEach { (threadId, threadTurns) ->
            val state = awaitSelected(model, ThreadKey(threadId))
            val messages = requireNotNull(state.currentConversation).messages
            val expectedOrder = threadTurns.sortedBy(PlannedTurn::threadOrdinal)
                .map(PlannedTurn::clientMessageId)
            val observedOrder = messages.asSequence()
                .filter {
                    it.role == MessageRole.USER &&
                        it.clientMessageId in expectedOrder
                }
                .mapNotNull(RichMessage::clientMessageId)
                .toList()
            assertEquals("Same-Thread FIFO changed across the fault", expectedOrder, observedOrder)
        }
        return completed
    }

    private suspend fun awaitCompletedTurn(
        model: AppViewModel,
        outboxStore: SharedPreferencesDurableOutboxStore,
        identity: GatewaySessionIdentity,
        planned: PlannedTurn,
        expectedReply: String,
    ): CompletedTurn = withTimeout(TURN_TIMEOUT_MILLIS) {
        val threadKey = ThreadKey(planned.threadId)
        while (true) {
            val state = awaitSelected(model, threadKey)
            val messages = state.currentConversation?.messages.orEmpty()
            val users = messages.filter {
                it.role == MessageRole.USER &&
                    it.clientMessageId == planned.clientMessageId
            }
            assertTrue("Business key produced duplicate user bubbles", users.size <= 1)
            val user = users.singleOrNull()
            val turnId = user?.turnId
            val assistantEvidence = if (turnId == null) {
                AssistantBubbleEvidence(0, 0)
            } else {
                assistantBubbleEvidence(messages, turnId)
            }
            val completed = if (turnId == null) {
                emptyList()
            } else {
                messages.filter {
                    it.role == MessageRole.ASSISTANT &&
                        it.turnId == turnId &&
                        it.state == RichMessageState.COMPLETED
                }
            }
            assertTrue("Business key produced duplicate terminal bubbles", completed.size <= 1)
            if (assistantEvidence.completedCount > 0) {
                assertTrue(
                    "A completed Turn retained an extra non-terminal assistant bubble",
                    assistantEvidence.isExactCompleted,
                )
            }
            assertTrue(
                "Business key reached a terminal failure",
                messages.none {
                    it.role == MessageRole.ASSISTANT &&
                        it.turnId == turnId &&
                        it.state in TERMINAL_FAILURE_STATES
                },
            )
            val outbox = activeOutbox(outboxStore, identity).filter {
                it.clientMessageId == planned.clientMessageId
            }
            assertTrue(
                "Recovered Outbox entered a non-success state",
                outbox.none { it.state in NON_SUCCESS_OUTBOX_STATES },
            )
            if (
                user != null &&
                user.readableBody() == planned.marker &&
                completed.size == 1 &&
                completed.single().readableBody().contains(expectedReply) &&
                outbox.isEmpty() &&
                state.acceptedUserMessages.none {
                    it.clientMessageId == planned.clientMessageId
                }
            ) {
                return@withTimeout CompletedTurn(
                    clientMessageId = planned.clientMessageId,
                    turnId = requireNotNull(turnId),
                    userMessageId = user.id,
                    assistantMessageId = completed.single().id,
                    duplicateBubbleCount =
                        (users.size - 1).coerceAtLeast(0) +
                            assistantEvidence.duplicateCount,
                )
            }
            delay(POLL_INTERVAL_MILLIS)
        }
        error("unreachable")
    }

    private suspend fun awaitHostAcknowledgement(file: File, expected: String) {
        withTimeout(HOST_ACK_TIMEOUT_MILLIS) {
            while (true) {
                val actual = runCatching {
                    file.takeIf(File::isFile)?.readText(Charsets.US_ASCII)?.trim()
                }.getOrNull()
                if (actual == expected) break
                delay(ACK_POLL_MILLIS)
            }
        }
        assertTrue(file.delete())
    }

    private fun createViewModel(
        gateway: CodexGateway,
        outboxStore: SharedPreferencesDurableOutboxStore,
        initialThread: ThreadKey,
    ): ViewModelOwner = onMain {
        val store = ViewModelStore()
        ViewModelOwner(
            store,
            ViewModelProvider(
                store,
                AppViewModel.Factory(
                    gateway = gateway,
                    lastThreadStore = InMemoryLastThreadStore(initialThread),
                    outboxStore = outboxStore,
                ),
            )[AppViewModel::class.java],
        )
    }

    private suspend fun requirePairedOnline(gateway: CodexGateway) {
        assertEquals(
            GatewayKind.RELAY,
            withTimeout(STATE_TIMEOUT_MILLIS) { gateway.kind.first() },
        )
        awaitOnline(gateway)
    }

    private suspend fun awaitOnline(gateway: CodexGateway) {
        withTimeout(ONLINE_TIMEOUT_MILLIS) {
            gateway.connectionState.first { it == GatewayConnectionState.ONLINE }
        }
    }

    private suspend fun awaitSelected(model: AppViewModel, key: ThreadKey) =
        withTimeout(STATE_TIMEOUT_MILLIS) {
            onMain { model.selectThread(key) }
            model.uiState.first {
                it.currentThreadKey == key &&
                    it.currentConversation?.threadKey == key &&
                    it.canSend &&
                    !it.isConversationRecovering
            }
        }

    private fun activeOutbox(
        store: SharedPreferencesDurableOutboxStore,
        identity: GatewaySessionIdentity,
    ): List<OutboxEntry> = store.list().filter {
        it.sessionScope == identity.scope &&
            it.sessionGeneration == identity.generation
    }

    private fun requireGlobalOutboxCount(
        store: SharedPreferencesDurableOutboxStore,
        expected: Int,
        stage: String,
    ): Int {
        val actual = store.list().size
        assertEquals(
            "Global production Outbox count changed $stage",
            expected,
            actual,
        )
        return actual
    }

    private fun assertBusinessKeysRemoved(
        store: SharedPreferencesDurableOutboxStore,
        turns: List<PlannedTurn>,
        stage: String,
    ) {
        val clientMessageIds = turns.mapTo(mutableSetOf()) {
            it.clientMessageId
        }
        assertTrue(
            "A fault business key remained in the production Outbox $stage",
            store.list().none { it.clientMessageId in clientMessageIds },
        )
    }

    private suspend fun cleanupThreads(
        context: android.content.Context,
        keys: List<ThreadKey>,
    ) {
        runCatching {
            val gateway = GatewayFactory.create(context)
            keys.forEach { runCatching { gateway.archiveThread(it) } }
            (gateway as? CloseableCodexGateway)?.close()
        }
    }

    private fun writePrivateState(file: File, value: AppKillRecord) {
        val temporary = File(file.parentFile, "${file.name}.tmp")
        assertTrue(!temporary.exists() || temporary.delete())
        temporary.writeText(value.toJson().toString(), Charsets.UTF_8)
        assertTrue(temporary.renameTo(file))
    }

    private fun readPrivateState(file: File): AppKillRecord {
        require(file.isFile && file.length() in 1..MAX_STATE_BYTES)
        return AppKillRecord.fromJson(JSONObject(file.readText(Charsets.UTF_8)))
    }

    private fun signal(
        checkpoint: String,
        nonce: String,
        component: String,
        iteration: Int,
        phaseDigest: String? = null,
        turns: Int,
        outboxEntries: Int,
        duplicateBubbles: Int = 0,
        actionObserved: Boolean,
    ) {
        InstrumentationRegistry.getInstrumentation().sendStatus(
            EVIDENCE_STATUS_CODE,
            Bundle().apply {
                putString(EVIDENCE_CHECKPOINT_KEY, checkpoint)
                putString(EVIDENCE_ACTION_NONCE_KEY, nonce)
                putString(EVIDENCE_COMPONENT_KEY, component)
                putInt(EVIDENCE_ITERATION_KEY, iteration)
                phaseDigest?.let { putString(EVIDENCE_PHASE_DIGEST_KEY, it) }
                putInt(EVIDENCE_TURNS_KEY, turns)
                putInt(EVIDENCE_OUTBOX_ENTRIES_KEY, outboxEntries)
                putInt(EVIDENCE_DUPLICATE_BUBBLES_KEY, duplicateBubbles)
                putBoolean(EVIDENCE_PRODUCTION_QUEUE_KEY, true)
                putBoolean(EVIDENCE_ACTION_OBSERVED_KEY, actionObserved)
            },
        )
    }

    private fun <T> onMain(block: () -> T): T {
        var outcome: Result<T>? = null
        InstrumentationRegistry.getInstrumentation().runOnMainSync {
            outcome = runCatching(block)
        }
        return requireNotNull(outcome).getOrThrow()
    }

    private fun RichMessage.readableBody(): String =
        orderedBlocks().joinToString("\n") { it.readableText() }

    private fun Bundle.requireHex(name: String): String =
        requireNotNull(getString(name)?.takeIf(HEX_32::matches))

    private fun Bundle.requireIteration(): Int =
        requireNotNull(
            getString(ITERATION_ARGUMENT)?.toIntOrNull()
                ?.takeIf { it in 1..REQUIRED_REPETITIONS },
        )

    private fun Bundle.requireRepetitions(): Int =
        requireNotNull(
            getString(REPETITIONS_ARGUMENT)?.toIntOrNull()
                ?.takeIf { it == REQUIRED_REPETITIONS },
        )

    private data class ViewModelOwner(
        val store: ViewModelStore,
        val model: AppViewModel,
    )

    /** Lets a transient second composer share the one allowed device Relay socket. */
    private class NonClosingGateway(
        delegate: CodexGateway,
    ) : CodexGateway by delegate

    private data class PlannedTurn(
        val threadId: String,
        val clientMessageId: String,
        val marker: String,
        val threadOrdinal: Int,
    ) {
        fun toJson(): JSONObject = JSONObject()
            .put("threadId", threadId)
            .put("clientMessageId", clientMessageId)
            .put("marker", marker)
            .put("threadOrdinal", threadOrdinal)

        companion object {
            fun fromJson(value: JSONObject): PlannedTurn {
                require(value.keys().asSequence().toSet() == PLANNED_KEYS)
                return PlannedTurn(
                    threadId = value.getString("threadId"),
                    clientMessageId = value.getString("clientMessageId"),
                    marker = value.getString("marker"),
                    threadOrdinal = value.getInt("threadOrdinal"),
                ).also {
                    require(
                        it.threadId.isNotBlank() &&
                            it.clientMessageId.isNotBlank() &&
                            it.marker.isNotBlank() &&
                            it.threadOrdinal in 1..3,
                    )
                }
            }
        }
    }

    private data class CompletedTurn(
        val clientMessageId: String,
        val turnId: String,
        val userMessageId: String,
        val assistantMessageId: String,
        val duplicateBubbleCount: Int,
    )

    private data class AppKillRecord(
        val runToken: String,
        val iteration: Int,
        val actionNonce: String,
        val scope: String,
        val generation: Long,
        val threadA: String,
        val threadB: String,
        val messages: List<PlannedTurn>,
    ) {
        fun digest(): String {
            val digest = MessageDigest.getInstance("SHA-256")
            val fields = listOf(
                runToken,
                iteration.toString(),
                actionNonce,
                scope,
                generation.toString(),
                threadA,
                threadB,
            ) + messages.flatMap {
                listOf(
                    it.threadId,
                    it.clientMessageId,
                    it.marker,
                    it.threadOrdinal.toString(),
                )
            }
            fields.forEach {
                require('\u0000' !in it)
                digest.update(it.toByteArray(Charsets.UTF_8))
                digest.update(0.toByte())
            }
            return digest.digest().joinToString("") { "%02x".format(it) }
        }

        fun toJson(): JSONObject = JSONObject()
            .put("runToken", runToken)
            .put("iteration", iteration)
            .put("actionNonce", actionNonce)
            .put("scope", scope)
            .put("generation", generation)
            .put("threadA", threadA)
            .put("threadB", threadB)
            .put(
                "messages",
                JSONArray().apply { messages.forEach { put(it.toJson()) } },
            )

        companion object {
            fun fromJson(value: JSONObject): AppKillRecord {
                require(value.keys().asSequence().toSet() == RECORD_KEYS)
                val array = value.getJSONArray("messages")
                val messages = (0 until array.length()).map {
                    PlannedTurn.fromJson(array.getJSONObject(it))
                }
                return AppKillRecord(
                    runToken = value.getString("runToken"),
                    iteration = value.getInt("iteration"),
                    actionNonce = value.getString("actionNonce"),
                    scope = value.getString("scope"),
                    generation = value.getLong("generation"),
                    threadA = value.getString("threadA"),
                    threadB = value.getString("threadB"),
                    messages = messages,
                ).also {
                    require(
                        HEX_32.matches(it.runToken) &&
                            HEX_32.matches(it.actionNonce) &&
                            it.iteration in 1..REQUIRED_REPETITIONS &&
                            it.scope.isNotBlank() &&
                            it.threadA.isNotBlank() &&
                            it.threadB.isNotBlank() &&
                            it.threadA != it.threadB &&
                            it.messages.size == 3 &&
                            it.messages.map(PlannedTurn::clientMessageId).distinct().size == 3,
                    )
                }
            }
        }
    }

    private companion object {
        const val PHASE_ARGUMENT = "cheby_relay_fault_phase"
        const val RUN_TOKEN_ARGUMENT = "cheby_relay_fault_run_token"
        const val ACTION_NONCE_ARGUMENT = "cheby_relay_fault_action_nonce"
        const val ITERATION_ARGUMENT = "cheby_relay_fault_iteration"
        const val REPETITIONS_ARGUMENT = "cheby_relay_fault_repetitions"
        const val PHASE_APP_KILL_PREPARE = "app-kill-prepare"
        const val PHASE_APP_KILL_RECOVER = "app-kill-recover"
        const val PHASE_TRANSPORT = "transport"
        const val COMPONENT_APP = "app"
        const val COMPONENT_WIFI = "wifi"
        const val COMPONENT_EDGE = "edge"
        const val COMPONENT_RELAY = "relay"
        const val COMPONENT_CONNECTOR = "connector"
        const val COMPONENT_ALL = "all"
        val FAULT_COMPONENTS = listOf(
            COMPONENT_EDGE,
            COMPONENT_RELAY,
            COMPONENT_CONNECTOR,
            COMPONENT_WIFI,
        )
        const val REQUIRED_REPETITIONS = 10
        const val APP_KILL_STATE_FILE = "cheby-gate-app-kill-state-v2.json"
        const val HOST_ACK_FILE = "cheby-gate-fault-ack-v2"
        const val EVIDENCE_STATUS_CODE = 2
        const val EVIDENCE_CHECKPOINT_KEY = "cheby_gate_checkpoint"
        const val EVIDENCE_ACTION_NONCE_KEY = "cheby_gate_action_nonce"
        const val EVIDENCE_COMPONENT_KEY = "cheby_gate_component"
        const val EVIDENCE_ITERATION_KEY = "cheby_gate_iteration"
        const val EVIDENCE_PHASE_DIGEST_KEY = "cheby_gate_phase_digest"
        const val EVIDENCE_TURNS_KEY = "cheby_gate_turns"
        const val EVIDENCE_OUTBOX_ENTRIES_KEY = "cheby_gate_outbox_entries"
        const val EVIDENCE_DUPLICATE_BUBBLES_KEY = "cheby_gate_duplicate_bubbles"
        const val EVIDENCE_PRODUCTION_QUEUE_KEY = "cheby_gate_production_queue"
        const val EVIDENCE_ACTION_OBSERVED_KEY = "cheby_gate_action_observed"
        const val CHECKPOINT_APP_PREPARE_OFFLINE_REQUIRED =
            "app_prepare_offline_required"
        const val CHECKPOINT_APP_KILL_OUTBOX_READY = "app_kill_outbox_ready"
        const val CHECKPOINT_APP_KILL_RECOVERED = "app_kill_recovered"
        const val CHECKPOINT_FAULT_DOWN_REQUIRED = "fault_down_required"
        const val CHECKPOINT_FAULT_OUTBOX_READY = "fault_outbox_ready"
        const val CHECKPOINT_FAULT_UP_REQUIRED = "fault_up_required"
        const val CHECKPOINT_FAULT_RECOVERED = "fault_recovered"
        const val CHECKPOINT_FAULT_GATE_COMPLETE = "fault_gate_complete"
        const val STATE_TIMEOUT_MILLIS = 90_000L
        const val ONLINE_TIMEOUT_MILLIS = 180_000L
        const val OFFLINE_TIMEOUT_MILLIS = 180_000L
        const val HOST_ACK_TIMEOUT_MILLIS = 180_000L
        const val TURN_TIMEOUT_MILLIS = 15 * 60 * 1_000L
        const val ACK_POLL_MILLIS = 100L
        const val POLL_INTERVAL_MILLIS = 100L
        const val MAX_STATE_BYTES = 128 * 1024L
        val HEX_32 = Regex("[a-f0-9]{32}")
        val PLANNED_KEYS = setOf(
            "threadId",
            "clientMessageId",
            "marker",
            "threadOrdinal",
        )
        val RECORD_KEYS = setOf(
            "runToken",
            "iteration",
            "actionNonce",
            "scope",
            "generation",
            "threadA",
            "threadB",
            "messages",
        )
        val NON_SUCCESS_OUTBOX_STATES = setOf(
            OutboxState.RETRYABLE,
            OutboxState.NEEDS_SYNC,
            OutboxState.REJECTED,
            OutboxState.NEEDS_ATTACHMENTS,
        )
        val TERMINAL_FAILURE_STATES = setOf(
            RichMessageState.FAILED,
            RichMessageState.INTERRUPTED,
            RichMessageState.CANCELLED,
        )
    }
}
