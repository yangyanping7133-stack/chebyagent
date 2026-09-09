package com.cheby.codex.mobile.gateway

import android.os.Bundle
import android.os.SystemClock
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.ViewModelStore
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.cheby.codex.mobile.data.InMemoryLastThreadStore
import com.cheby.codex.mobile.data.OutboxEntry
import com.cheby.codex.mobile.data.OutboxState
import com.cheby.codex.mobile.data.SharedPreferencesDurableOutboxStore
import com.cheby.codex.mobile.model.CodexThread
import com.cheby.codex.mobile.model.GatewayConnectionState
import com.cheby.codex.mobile.model.MessageRole
import com.cheby.codex.mobile.model.RichMessage
import com.cheby.codex.mobile.model.RichMessageState
import com.cheby.codex.mobile.model.ThreadKey
import com.cheby.codex.mobile.model.orderedBlocks
import com.cheby.codex.mobile.model.readableText
import com.cheby.codex.mobile.ui.AppUiState
import com.cheby.codex.mobile.ui.AppViewModel
import com.cheby.codex.mobile.ui.ThreadTimelineEntry
import com.cheby.codex.mobile.ui.conversationTimeline
import com.cheby.codex.mobile.ui.visibleConversationMessages
import java.security.MessageDigest
import java.util.UUID
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.withTimeoutOrNull
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Opt-in load gate through the production AppViewModel queue and UI projection.
 *
 * Every Turn is enqueued with [AppViewModel.send], persisted in the real app-private outbox and
 * drained by the production per-Thread planner. The Gate Connector holds the first Turn until a
 * second distinct Thread reaches its server-side latch, so a defective global FIFO cannot pass.
 */
@RunWith(AndroidJUnit4::class)
class RelayLoadGateInstrumentedTest {
    @Test
    fun pairedGateSupportsProductionQueueLoadAndRecovery() = runBlocking {
        val arguments = InstrumentationRegistry.getArguments()
        assumeTrue(
            "Relay load gate is disabled unless $ENABLE_ARGUMENT=true is supplied",
            arguments.getString(ENABLE_ARGUMENT) == "true",
        )
        val threadCount = arguments.boundedInt(
            name = THREAD_COUNT_ARGUMENT,
            default = DEFAULT_THREAD_COUNT,
            range = MIN_THREAD_COUNT..MAX_THREAD_COUNT,
        )
        val totalTurns = arguments.boundedInt(
            name = TOTAL_TURNS_ARGUMENT,
            default = DEFAULT_TOTAL_TURNS,
            range = threadCount..MAX_TOTAL_TURNS,
        )
        val expectedReply = arguments.requireExpectedReply()
        val minimumDurationMillis = arguments.optionalSoakDurationMillis()

        val gateTimeout = if (minimumDurationMillis == 0L) {
            LOAD_TIMEOUT_MILLIS
        } else {
            minimumDurationMillis + SOAK_SETTLE_BUDGET_MILLIS
        }
        withTimeout(gateTimeout) {
            runLoadGate(
                threadCount,
                totalTurns,
                expectedReply,
                minimumDurationMillis,
            )
        }
    }

    private suspend fun runLoadGate(
        threadCount: Int,
        totalTurns: Int,
        expectedReply: String,
        minimumDurationMillis: Long,
    ) {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val context = instrumentation.targetContext.applicationContext
        val runToken = UUID.randomUUID().toString().replace("-", "").lowercase()
        val outboxStore = SharedPreferencesDurableOutboxStore(context)
        val createdThreads = mutableListOf<CodexThread>()
        val businessKeys = mutableSetOf<BusinessKey>()
        var gateway: CodexGateway? = null
        var viewModelStore: ViewModelStore? = null

        try {
            val initialGateway = GatewayFactory.create(context)
            gateway = initialGateway
            requirePairedOnline(initialGateway)
            val initialIdentity = initialGateway.sessionIdentity.value
            requireGlobalOutboxCount(outboxStore, 0, "before load")

            repeat(threadCount) { index ->
                createdThreads += initialGateway.createThread(
                    "Gate load ${runToken.takeLast(6)} ${index + 1}",
                )
            }
            assertEquals(
                "The Gateway reused a Thread identity",
                threadCount,
                createdThreads.map(CodexThread::key).distinct().size,
            )

            val initialOwner = createViewModel(
                gateway = initialGateway,
                outboxStore = outboxStore,
                initialThread = createdThreads.first().key,
            )
            viewModelStore = initialOwner.store
            val initialModel = initialOwner.model
            val plannedByThread = createdThreads.associate { thread ->
                thread.key to mutableListOf<PlannedTurnEvidence>()
            }
            val workloadStartedAt = SystemClock.elapsedRealtime()
            repeat(totalTurns) { globalTurn ->
                awaitSoakSchedule(
                    workloadStartedAt = workloadStartedAt,
                    minimumDurationMillis = minimumDurationMillis,
                    globalTurn = globalTurn,
                    totalTurns = totalTurns,
                    initialBurstTurns = threadCount,
                )
                val threadIndex = globalTurn % threadCount
                val thread = createdThreads[threadIndex]
                awaitSelected(initialModel, thread.key, "enqueue-${globalTurn + 1}")
                val threadOrdinal = globalTurn / threadCount
                val marker = "GATE-$runToken-" +
                    "G${globalTurn + 1}-T${threadIndex + 1}-N${threadOrdinal + 1}"
                val entry = onMain {
                    assertTrue(
                        "The production composer rejected a deterministic Gate Turn",
                        initialModel.send(marker),
                    )
                    requireNotNull(
                        outboxStore.list().singleOrNull {
                            it.sessionScope == initialIdentity.scope &&
                                it.sessionGeneration == initialIdentity.generation &&
                                it.threadKey == thread.key &&
                                it.text == marker
                        },
                    ) {
                        "The production composer did not durably enqueue its Turn"
                    }
                }
                val businessKey = BusinessKey(
                    scope = entry.sessionScope,
                    generation = entry.sessionGeneration,
                    threadKey = entry.threadKey,
                    clientMessageId = entry.clientMessageId,
                )
                assertTrue(
                    "The production queue reused a four-part business key",
                    businessKeys.add(businessKey),
                )
                requireNotNull(plannedByThread[thread.key]) += PlannedTurnEvidence(
                    marker = marker,
                    businessKey = businessKey,
                )
            }
            val workloadElapsedMillis = SystemClock.elapsedRealtime() - workloadStartedAt
            assertTrue(
                "The real-device soak workload ended before its monotonic duration",
                workloadElapsedMillis >= minimumDurationMillis,
            )
            val plannedThreads = createdThreads.map { thread ->
                PlannedThreadEvidence(
                    thread.key,
                    requireNotNull(plannedByThread[thread.key]).toList(),
                )
            }

            assertEquals(totalTurns, businessKeys.size)
            assertEquals(totalTurns, plannedThreads.sumOf { it.turns.size })
            plannedThreads.forEachIndexed { threadIndex, planned ->
                assertEquals(
                    "A Thread did not enter the full assigned production queue workload",
                    ((totalTurns - 1 - threadIndex) / threadCount) + 1,
                    planned.turns.size,
                )
            }

            val initialCompleted = plannedThreads.mapIndexed { index, planned ->
                val settled = awaitThreadSettled(
                    model = initialModel,
                    outboxStore = outboxStore,
                    identity = initialIdentity,
                    planned = planned,
                    selectionStage = "initial-settle-${index + 1}",
                )
                assertCompleteThreadEvidence(
                    state = settled,
                    planned = planned,
                    allPlanned = plannedThreads,
                    runToken = runToken,
                    expectedReply = expectedReply,
                )
            }
            val initialBarrierThreads = initialCompleted.count(ThreadEvidence::serverBarrierProven)
            assertTrue(
                "The Gate Connector did not prove two distinct concurrent Thread submissions",
                initialBarrierThreads >= MIN_SERVER_BARRIER_THREADS,
            )
            requireGlobalOutboxCount(outboxStore, 0, "after load")

            onMain { requireNotNull(viewModelStore).clear() }
            viewModelStore = null
            gateway = null

            val reconstructedGateway = GatewayFactory.create(context)
            gateway = reconstructedGateway
            requirePairedOnline(reconstructedGateway)
            assertEquals(
                "The durable Relay identity changed during production queue recovery",
                initialIdentity,
                reconstructedGateway.sessionIdentity.value,
            )
            val reconstructedOwner = createViewModel(
                gateway = reconstructedGateway,
                outboxStore = outboxStore,
                initialThread = createdThreads.first().key,
            )
            viewModelStore = reconstructedOwner.store
            val reconstructedModel = reconstructedOwner.model
            val reconstructedCompleted = plannedThreads.mapIndexed { index, planned ->
                val settled = awaitThreadSettled(
                    model = reconstructedModel,
                    outboxStore = outboxStore,
                    identity = initialIdentity,
                    planned = planned,
                    selectionStage = "reconstructed-settle-${index + 1}",
                )
                assertCompleteThreadEvidence(
                    state = settled,
                    planned = planned,
                    allPlanned = plannedThreads,
                    runToken = runToken,
                    expectedReply = expectedReply,
                )
            }
            assertEquals(
                "Reconstruction changed canonical Turn/message evidence",
                initialCompleted,
                reconstructedCompleted,
            )
            val finalOutboxEntries = requireGlobalOutboxCount(
                outboxStore,
                0,
                "after reconstruction",
            )
            val completedTurns = initialCompleted.flatMap(ThreadEvidence::turns)
            signalLoadEvidenceComplete(
                runToken = runToken,
                threadCount = threadCount,
                totalTurns = totalTurns,
                businessKeyCount = businessKeys.size,
                serverBarrierThreads = initialBarrierThreads,
                completedTurns = completedTurns,
                outboxEntries = finalOutboxEntries,
                duplicateBubbles = completedTurns
                    .sumOf(TurnEvidence::duplicateBubbleCount),
                soakElapsedMillis = workloadElapsedMillis,
                soakMinimumDurationVerified =
                    workloadElapsedMillis >= minimumDurationMillis,
            )
        } finally {
            gateway?.let { cleanup ->
                createdThreads.forEach { thread ->
                    runCatching { cleanup.archiveThread(thread.key) }
                }
            }
            viewModelStore?.let { owner -> onMain { owner.clear() } }
            if (viewModelStore == null) {
                (gateway as? CloseableCodexGateway)?.close()
            }
        }
    }

    private fun createViewModel(
        gateway: CodexGateway,
        outboxStore: SharedPreferencesDurableOutboxStore,
        initialThread: ThreadKey,
    ): ViewModelOwner = onMain {
        val store = ViewModelStore()
        val provider = ViewModelProvider(
            store,
            AppViewModel.Factory(
                gateway = gateway,
                lastThreadStore = InMemoryLastThreadStore(initialThread),
                outboxStore = outboxStore,
            ),
        )
        ViewModelOwner(
            store = store,
            model = provider[AppViewModel::class.java],
        )
    }

    private suspend fun requirePairedOnline(gateway: CodexGateway) {
        assertEquals(
            "Relay load gate requires an existing app-private paired session",
            GatewayKind.RELAY,
            withTimeout(STATE_TIMEOUT_MILLIS) { gateway.kind.first() },
        )
        withTimeout(ONLINE_TIMEOUT_MILLIS) {
            gateway.connectionState.first { it == GatewayConnectionState.ONLINE }
        }
    }

    private suspend fun awaitSelected(
        model: AppViewModel,
        threadKey: ThreadKey,
        stage: String,
    ): AppUiState {
        onMain { model.selectThread(threadKey) }
        val selected = withTimeoutOrNull(STATE_TIMEOUT_MILLIS) {
            model.uiState.first {
                it.currentThreadKey == threadKey &&
                    it.currentConversation?.threadKey == threadKey &&
                    it.canSend &&
                    !it.isConversationRecovering
            }
        }
        if (selected != null) return selected

        val state = model.uiState.value
        InstrumentationRegistry.getInstrumentation().sendStatus(
            EVIDENCE_STATUS_CODE,
            Bundle().apply {
                putString(EVIDENCE_FAILURE_STAGE_KEY, stage.take(64))
                putBoolean(EVIDENCE_SELECTED_THREAD_MATCH_KEY, state.currentThreadKey == threadKey)
                putBoolean(EVIDENCE_CONVERSATION_PRESENT_KEY, state.currentConversation != null)
                putBoolean(
                    EVIDENCE_CONVERSATION_THREAD_MATCH_KEY,
                    state.currentConversation?.threadKey == threadKey,
                )
                putBoolean(EVIDENCE_CAN_SEND_KEY, state.canSend)
                putBoolean(EVIDENCE_CONVERSATION_RECOVERING_KEY, state.isConversationRecovering)
                putBoolean(
                    EVIDENCE_CONVERSATION_RESYNC_KEY,
                    state.currentConversation?.resyncRequired == true,
                )
                putString(EVIDENCE_CONNECTION_STATE_KEY, state.connectionState.name)
                putString(EVIDENCE_GATEWAY_KIND_KEY, state.gatewayKind.name)
                putInt(EVIDENCE_OUTBOX_COUNT_KEY, state.outbox.size)
                putInt(EVIDENCE_THREADS_VISIBLE_KEY, state.threads.size)
            },
        )
        throw AssertionError("The selected Thread did not become sendable")
    }

    private suspend fun awaitThreadSettled(
        model: AppViewModel,
        outboxStore: SharedPreferencesDurableOutboxStore,
        identity: GatewaySessionIdentity,
        planned: PlannedThreadEvidence,
        selectionStage: String,
    ): AppUiState {
        awaitSelected(model, planned.threadKey, selectionStage)
        val expectedClientIds = planned.turns.mapTo(mutableSetOf()) {
            it.businessKey.clientMessageId
        }
        return withTimeout(THREAD_SETTLE_TIMEOUT_MILLIS) {
            while (true) {
                val activeOutbox = activeOutbox(outboxStore, identity)
                val threadOutbox = activeOutbox.filter { it.threadKey == planned.threadKey }
                assertTrue(
                    "The production queue entered an explicit non-success state",
                    threadOutbox.none { it.state in NON_SUCCESS_OUTBOX_STATES },
                )
                val state = model.uiState.value
                val visibleThreadOutbox = state.outbox.filter {
                    it.threadKey == planned.threadKey
                }
                val messages = state.currentConversation?.messages.orEmpty()
                val canonicalClientIds = messages.asSequence()
                    .filter { it.role == MessageRole.USER }
                    .mapNotNull(RichMessage::clientMessageId)
                    .filter { it in expectedClientIds }
                    .toSet()
                val completedTurnIds = messages.asSequence()
                    .filter {
                        it.role == MessageRole.ASSISTANT &&
                            it.state == RichMessageState.COMPLETED
                    }
                    .mapNotNull(RichMessage::turnId)
                    .toSet()
                val expectedTurnIds = messages.asSequence()
                    .filter {
                        it.role == MessageRole.USER &&
                            it.clientMessageId in expectedClientIds
                    }
                    .mapNotNull(RichMessage::turnId)
                    .toSet()
                val acceptedRemain = state.acceptedUserMessages.any {
                    it.threadKey == planned.threadKey &&
                        it.clientMessageId in expectedClientIds
                }
                if (
                    state.currentThreadKey == planned.threadKey &&
                    state.currentConversation?.threadKey == planned.threadKey &&
                    threadOutbox.isEmpty() &&
                    visibleThreadOutbox.isEmpty() &&
                    !acceptedRemain &&
                    canonicalClientIds == expectedClientIds &&
                    completedTurnIds.containsAll(expectedTurnIds) &&
                    expectedTurnIds.size == planned.turns.size
                ) {
                    return@withTimeout state
                }
                delay(POLL_INTERVAL_MILLIS)
            }
            error("unreachable")
        }
    }

    private fun assertCompleteThreadEvidence(
        state: AppUiState,
        planned: PlannedThreadEvidence,
        allPlanned: List<PlannedThreadEvidence>,
        runToken: String,
        expectedReply: String,
    ): ThreadEvidence {
        assertEquals(planned.threadKey, state.currentThreadKey)
        val messages = requireNotNull(state.currentConversation).messages
        val pending = state.outbox.filter { it.threadKey == planned.threadKey }
        val accepted = state.acceptedUserMessages.filter { it.threadKey == planned.threadKey }
        val visibleMessages = visibleConversationMessages(messages, pending)
        val timeline = conversationTimeline(visibleMessages, accepted, pending)
        assertTrue("The final production UI still contains pending outbox cards", pending.isEmpty())
        assertTrue(
            "The final production UI still contains accepted fallback cards",
            accepted.isEmpty(),
        )
        assertEquals(
            "The canonical conversation contains duplicate message ids",
            messages.size,
            messages.map(RichMessage::id).distinct().size,
        )
        assertEquals(
            "The production UI timeline contains duplicate keys",
            timeline.size,
            timeline.map(ThreadTimelineEntry::key).distinct().size,
        )

        val expectedClientIds =
            planned.turns.mapTo(mutableSetOf()) { it.businessKey.clientMessageId }
        val expectedOrder = planned.turns.map { it.businessKey.clientMessageId }
        val observedOrder = messages.asSequence()
            .filter {
                it.role == MessageRole.USER &&
                    it.clientMessageId in expectedClientIds
            }
            .map { requireNotNull(it.clientMessageId) }
            .toList()
        assertEquals(
            "The production queue changed same-Thread FIFO order",
            expectedOrder,
            observedOrder,
        )

        val foreignClientIds = allPlanned.asSequence()
            .filter { it.threadKey != planned.threadKey }
            .flatMap { it.turns.asSequence() }
            .map { it.businessKey.clientMessageId }
            .toSet()
        assertTrue(
            "A canonical user message leaked from another Gate Thread",
            messages.none {
                it.role == MessageRole.USER &&
                    (
                        it.clientMessageId in foreignClientIds ||
                            (
                                it.readableBody().startsWith("GATE-$runToken-") &&
                                    it.clientMessageId !in expectedClientIds
                                )
                        )
            },
        )

        val completed = planned.turns.map { evidence ->
            assertEquals(planned.threadKey, evidence.businessKey.threadKey)
            assertEquals(state.currentSessionScope, evidence.businessKey.scope)
            assertEquals(state.currentSessionGeneration, evidence.businessKey.generation)
            val users = messages.filter {
                it.role == MessageRole.USER &&
                    it.clientMessageId == evidence.businessKey.clientMessageId
            }
            assertEquals("A business key produced duplicate or missing user bubbles", 1, users.size)
            val user = users.single()
            assertEquals(evidence.marker, user.readableBody())
            val turnId = requireNotNull(user.turnId) {
                "A canonical user message lost its Turn identity"
            }
            val assistantEvidence = assistantBubbleEvidence(messages, turnId)
            val assistants = messages.filter {
                it.role == MessageRole.ASSISTANT &&
                    it.turnId == turnId &&
                    it.state == RichMessageState.COMPLETED
            }
            assertTrue(
                "A completed Turn retained an extra non-terminal assistant bubble",
                assistantEvidence.isExactCompleted,
            )
            assertEquals(
                "A Turn produced duplicate or missing terminal bubbles",
                1,
                assistants.size,
            )
            val assistant = assistants.single()
            assertTrue(assistant.readableBody().contains(expectedReply))
            assertEquals(
                "The production UI did not expose exactly one user bubble for a business key",
                1,
                timeline.count { entry ->
                    when (entry) {
                        is ThreadTimelineEntry.Canonical ->
                            entry.message.role == MessageRole.USER &&
                                entry.message.clientMessageId ==
                                evidence.businessKey.clientMessageId
                        is ThreadTimelineEntry.Accepted ->
                            entry.message.clientMessageId ==
                                evidence.businessKey.clientMessageId
                        is ThreadTimelineEntry.Pending ->
                            entry.message.clientMessageId ==
                                evidence.businessKey.clientMessageId
                    }
                },
            )
            TurnEvidence(
                businessKey = evidence.businessKey,
                turnId = turnId,
                userMessageId = user.id,
                assistantMessageId = assistant.id,
                duplicateBubbleCount =
                    (users.size - 1).coerceAtLeast(0) +
                        assistantEvidence.duplicateCount,
            )
        }
        val first = completed.first()
        val last = completed.last()
        assertEquals(planned.turns.first().businessKey, first.businessKey)
        assertEquals(planned.turns.last().businessKey, last.businessKey)
        val concurrencyLine = "$CONCURRENCY_PROOF_PREFIX: $runToken:2"
        val serverBarrierProven = messages.any {
            it.role == MessageRole.ASSISTANT &&
                it.turnId in completed.mapTo(mutableSetOf(), TurnEvidence::turnId) &&
                it.readableBody().lineSequence().any { line -> line == concurrencyLine }
        }
        return ThreadEvidence(
            threadKey = planned.threadKey,
            turns = completed,
            serverBarrierProven = serverBarrierProven,
        )
    }

    private fun requireGlobalOutboxCount(
        store: SharedPreferencesDurableOutboxStore,
        expected: Int,
        stage: String,
    ): Int {
        val actual = store.list().size
        assertEquals(
            "The global production Outbox count changed $stage",
            expected,
            actual,
        )
        return actual
    }

    private fun activeOutbox(
        store: SharedPreferencesDurableOutboxStore,
        identity: GatewaySessionIdentity,
    ): List<OutboxEntry> = store.list().filter {
        it.sessionScope == identity.scope &&
            it.sessionGeneration == identity.generation
    }

    private fun signalLoadEvidenceComplete(
        runToken: String,
        threadCount: Int,
        totalTurns: Int,
        businessKeyCount: Int,
        serverBarrierThreads: Int,
        completedTurns: List<TurnEvidence>,
        outboxEntries: Int,
        duplicateBubbles: Int,
        soakElapsedMillis: Long,
        soakMinimumDurationVerified: Boolean,
    ) {
        val scopeGenerations = completedTurns
            .map { it.businessKey.scope to it.businessKey.generation }
            .distinct()
        assertEquals(
            "The load run crossed an Android binding scope/generation boundary",
            1,
            scopeGenerations.size,
        )
        InstrumentationRegistry.getInstrumentation().sendStatus(
            EVIDENCE_STATUS_CODE,
            Bundle().apply {
                putString(EVIDENCE_CHECKPOINT_KEY, EVIDENCE_CHECKPOINT_VALUE)
                putString(EVIDENCE_RUN_TOKEN_KEY, runToken)
                putInt(EVIDENCE_THREADS_KEY, threadCount)
                putInt(EVIDENCE_TURNS_KEY, totalTurns)
                putInt(EVIDENCE_BUSINESS_KEYS_KEY, businessKeyCount)
                putInt(EVIDENCE_OUTBOX_ENTRIES_KEY, outboxEntries)
                putInt(EVIDENCE_DUPLICATE_BUBBLES_KEY, duplicateBubbles)
                putBoolean(EVIDENCE_FIRST_LAST_VERIFIED_KEY, true)
                putInt(EVIDENCE_SERVER_BARRIER_THREADS_KEY, serverBarrierThreads)
                putBoolean(EVIDENCE_PRODUCTION_QUEUE_KEY, true)
                putString(EVIDENCE_TUPLE_SHA256_KEY, tupleDigest(completedTurns))
                putString(
                    EVIDENCE_SCOPE_GENERATION_SHA256_KEY,
                    scopeGenerationDigest(scopeGenerations.single()),
                )
                putInt(EVIDENCE_SCOPE_GENERATION_PAIRS_KEY, scopeGenerations.size)
                putInt(EVIDENCE_SCOPE_GENERATION_OBSERVATIONS_KEY, completedTurns.size)
                putLong(EVIDENCE_SOAK_ELAPSED_MILLIS_KEY, soakElapsedMillis)
                putBoolean(
                    EVIDENCE_SOAK_MONOTONIC_VERIFIED_KEY,
                    soakMinimumDurationVerified,
                )
            },
        )
    }

    /**
     * The first Turn for every Thread is deliberately burst-enqueued so the Connector's
     * cross-Thread barrier cannot be defeated by soak pacing. Remaining Turns use a monotonic
     * target. Delay is only back-pressure; reaching the target predicate is what admits a Turn.
     */
    private suspend fun awaitSoakSchedule(
        workloadStartedAt: Long,
        minimumDurationMillis: Long,
        globalTurn: Int,
        totalTurns: Int,
        initialBurstTurns: Int,
    ) {
        if (minimumDurationMillis == 0L || globalTurn < initialBurstTurns) return
        val pacedTurns = totalTurns - initialBurstTurns
        require(pacedTurns > 0) { "Soak requires Turns after the concurrency burst" }
        val pacedOrdinal = globalTurn - initialBurstTurns + 1
        val targetElapsed =
            (minimumDurationMillis * pacedOrdinal.toLong()) / pacedTurns.toLong()
        while (true) {
            val remaining =
                targetElapsed - (SystemClock.elapsedRealtime() - workloadStartedAt)
            if (remaining <= 0L) return
            delay(minOf(remaining, SOAK_SCHEDULE_POLL_MILLIS))
        }
    }

    /**
     * Keep this byte contract identical to deploy/turkey/audit_gate_business_keys.py:
     * sort public (threadId, clientMessageId, turnId), append every UTF-8 field and NUL.
     */
    private fun tupleDigest(completedTurns: List<TurnEvidence>): String {
        val digest = MessageDigest.getInstance("SHA-256")
        completedTurns
            .map {
                Triple(
                    it.businessKey.threadKey.value,
                    it.businessKey.clientMessageId,
                    it.turnId,
                )
            }
            .sortedWith(
                compareBy<Triple<String, String, String>>(
                    { it.first },
                    { it.second },
                    { it.third },
                ),
            )
            .forEach { value ->
                listOf(value.first, value.second, value.third).forEach { field ->
                    require('\u0000' !in field) { "Gate tuple field contains NUL" }
                    digest.update(field.toByteArray(Charsets.UTF_8))
                    digest.update(0.toByte())
                }
            }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

    private fun scopeGenerationDigest(value: Pair<String, Long>): String {
        require('\u0000' !in value.first) { "Gate scope contains NUL" }
        val digest = MessageDigest.getInstance("SHA-256")
        digest.update(value.first.toByteArray(Charsets.UTF_8))
        digest.update(0.toByte())
        digest.update(value.second.toString().toByteArray(Charsets.US_ASCII))
        digest.update(0.toByte())
        return digest.digest().joinToString("") { "%02x".format(it) }
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

    private fun android.os.Bundle.boundedInt(
        name: String,
        default: Int,
        range: IntRange,
    ): Int {
        val raw = getString(name) ?: return default
        return requireNotNull(raw.toIntOrNull()?.takeIf { it in range }) {
            "$name must be in ${range.first}..${range.last}"
        }
    }

    private fun android.os.Bundle.optionalSoakDurationMillis(): Long {
        val raw = getString(SOAK_MIN_DURATION_ARGUMENT) ?: return 0L
        return requireNotNull(
            raw.toLongOrNull()?.takeIf { it == REQUIRED_SOAK_DURATION_MILLIS },
        ) {
            "$SOAK_MIN_DURATION_ARGUMENT must be exactly " +
                "$REQUIRED_SOAK_DURATION_MILLIS"
        }
    }

    private data class ViewModelOwner(
        val store: ViewModelStore,
        val model: AppViewModel,
    )

    private data class PlannedTurnEvidence(
        val marker: String,
        val businessKey: BusinessKey,
    )

    private data class PlannedThreadEvidence(
        val threadKey: ThreadKey,
        val turns: List<PlannedTurnEvidence>,
    )

    private data class TurnEvidence(
        val businessKey: BusinessKey,
        val turnId: String,
        val userMessageId: String,
        val assistantMessageId: String,
        val duplicateBubbleCount: Int,
    )

    private data class ThreadEvidence(
        val threadKey: ThreadKey,
        val turns: List<TurnEvidence>,
        val serverBarrierProven: Boolean,
    )

    private data class BusinessKey(
        val scope: String,
        val generation: Long,
        val threadKey: ThreadKey,
        val clientMessageId: String,
    )

    private companion object {
        const val ENABLE_ARGUMENT = "cheby_relay_load_gate"
        const val THREAD_COUNT_ARGUMENT = "cheby_relay_load_threads"
        const val TOTAL_TURNS_ARGUMENT = "cheby_relay_load_turns"
        const val CONCURRENCY_PROOF_PREFIX = "Gate-Concurrency"
        const val EVIDENCE_STATUS_CODE = 2
        const val EVIDENCE_CHECKPOINT_KEY = "cheby_gate_checkpoint"
        const val EVIDENCE_CHECKPOINT_VALUE = "load_gate_complete"
        const val EVIDENCE_RUN_TOKEN_KEY = "cheby_gate_run_token"
        const val EVIDENCE_THREADS_KEY = "cheby_gate_threads"
        const val EVIDENCE_TURNS_KEY = "cheby_gate_turns"
        const val EVIDENCE_BUSINESS_KEYS_KEY = "cheby_gate_business_keys"
        const val EVIDENCE_OUTBOX_ENTRIES_KEY = "cheby_gate_outbox_entries"
        const val EVIDENCE_DUPLICATE_BUBBLES_KEY = "cheby_gate_duplicate_bubbles"
        const val EVIDENCE_FIRST_LAST_VERIFIED_KEY = "cheby_gate_first_last_verified"
        const val EVIDENCE_SERVER_BARRIER_THREADS_KEY = "cheby_gate_server_barrier_threads"
        const val EVIDENCE_PRODUCTION_QUEUE_KEY = "cheby_gate_production_queue"
        const val EVIDENCE_TUPLE_SHA256_KEY = "cheby_gate_tuple_sha256"
        const val EVIDENCE_SCOPE_GENERATION_SHA256_KEY =
            "cheby_gate_scope_generation_sha256"
        const val EVIDENCE_SCOPE_GENERATION_PAIRS_KEY =
            "cheby_gate_scope_generation_pairs"
        const val EVIDENCE_SCOPE_GENERATION_OBSERVATIONS_KEY =
            "cheby_gate_scope_generation_observations"
        const val EVIDENCE_SOAK_ELAPSED_MILLIS_KEY =
            "cheby_gate_soak_elapsed_millis"
        const val EVIDENCE_SOAK_MONOTONIC_VERIFIED_KEY =
            "cheby_gate_soak_monotonic_verified"
        const val EVIDENCE_FAILURE_STAGE_KEY = "cheby_gate_failure_stage"
        const val EVIDENCE_SELECTED_THREAD_MATCH_KEY = "cheby_gate_selected_thread_match"
        const val EVIDENCE_CONVERSATION_PRESENT_KEY = "cheby_gate_conversation_present"
        const val EVIDENCE_CONVERSATION_THREAD_MATCH_KEY =
            "cheby_gate_conversation_thread_match"
        const val EVIDENCE_CAN_SEND_KEY = "cheby_gate_can_send"
        const val EVIDENCE_CONVERSATION_RECOVERING_KEY =
            "cheby_gate_conversation_recovering"
        const val EVIDENCE_CONVERSATION_RESYNC_KEY = "cheby_gate_conversation_resync"
        const val EVIDENCE_CONNECTION_STATE_KEY = "cheby_gate_connection_state"
        const val EVIDENCE_GATEWAY_KIND_KEY = "cheby_gate_gateway_kind"
        const val EVIDENCE_OUTBOX_COUNT_KEY = "cheby_gate_outbox_count"
        const val EVIDENCE_THREADS_VISIBLE_KEY = "cheby_gate_threads_visible"
        const val SOAK_MIN_DURATION_ARGUMENT =
            "cheby_relay_soak_min_duration_millis"
        const val DEFAULT_THREAD_COUNT = 8
        const val DEFAULT_TOTAL_TURNS = 500
        const val MIN_THREAD_COUNT = 8
        const val MAX_THREAD_COUNT = 32
        const val MAX_TOTAL_TURNS = 2_000
        const val MIN_SERVER_BARRIER_THREADS = 2
        const val STATE_TIMEOUT_MILLIS = 60_000L
        const val ONLINE_TIMEOUT_MILLIS = 90_000L
        const val THREAD_SETTLE_TIMEOUT_MILLIS = 45 * 60 * 1_000L
        const val LOAD_TIMEOUT_MILLIS = 90 * 60 * 1_000L
        const val REQUIRED_SOAK_DURATION_MILLIS = 8 * 60 * 60 * 1_000L
        const val SOAK_SETTLE_BUDGET_MILLIS = 2 * 60 * 60 * 1_000L
        const val SOAK_SCHEDULE_POLL_MILLIS = 1_000L
        const val POLL_INTERVAL_MILLIS = 100L
        val NON_SUCCESS_OUTBOX_STATES = setOf(
            OutboxState.RETRYABLE,
            OutboxState.NEEDS_SYNC,
            OutboxState.REJECTED,
            OutboxState.NEEDS_ATTACHMENTS,
        )
    }
}
