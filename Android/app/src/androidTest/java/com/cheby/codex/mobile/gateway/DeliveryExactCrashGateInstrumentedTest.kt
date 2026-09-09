package com.cheby.codex.mobile.gateway

import android.content.Context
import android.os.Bundle
import android.os.Process
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.ViewModelStore
import androidx.lifecycle.ViewModelStoreOwner
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.cheby.codex.mobile.data.AndroidKeystoreRelayConfigStore
import com.cheby.codex.mobile.data.InMemoryLastThreadStore
import com.cheby.codex.mobile.data.OutboxEntry
import com.cheby.codex.mobile.data.OutboxKey
import com.cheby.codex.mobile.data.OutboxState
import com.cheby.codex.mobile.data.ProcessLockedRelayConfigStore
import com.cheby.codex.mobile.data.SharedPreferencesDurableOutboxStore
import com.cheby.codex.mobile.data.key
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
import java.util.concurrent.CountDownLatch
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Host-coordinated real process-death gate for five Android-owned public boundaries.
 *
 * `prepare` never returns: a Gate-only hook first commits an exact app-private checkpoint and then
 * blocks the production caller. The host verifies the checkpoint against the live PID and issues
 * `am force-stop com.cheby.codex.mobile.gate`. A separate instrumentation process runs `recover`.
 * Reconstructing objects in one process is deliberately not accepted as crash evidence.
 */
@RunWith(AndroidJUnit4::class)
class DeliveryExactCrashGateInstrumentedTest {
    @Test
    fun assistantMultiplicityNegativeControlsRejectFalseCompletion() {
        assertNull(
            exactCompletedAssistantIndex(
                emptyList(),
                "expected",
            ),
        )
        assertNull(
            exactCompletedAssistantIndex(
                listOf(AssistantProbe("IN_PROGRESS", "partial")),
                "expected",
            ),
        )
        assertEquals(
            0,
            exactCompletedAssistantIndex(
                listOf(AssistantProbe("COMPLETED", "expected")),
                "expected",
            ),
        )
        assertTrue(
            runCatching {
                exactCompletedAssistantIndex(
                    listOf(
                        AssistantProbe("COMPLETED", "expected"),
                        AssistantProbe("IN_PROGRESS", "extra"),
                    ),
                    "expected",
                )
            }.isFailure,
        )
        assertTrue(
            runCatching {
                exactCompletedAssistantIndex(
                    listOf(AssistantProbe("COMPLETED", "wrong")),
                    "expected",
                )
            }.isFailure,
        )
        assertTrue(
            runCatching {
                exactCompletedAssistantIndex(
                    listOf(AssistantProbe("FAILED", "failed")),
                    "expected",
                )
            }.isFailure,
        )
    }

    @Test
    fun canonicalUserProjectionWaitsOnZeroAndRejectsEveryDuplicate() {
        val expectedClientMessageId = "client-expected"
        val marker = "marker"
        assertNull(exactCanonicalUser(emptyList(), marker, expectedClientMessageId))
        assertNull(
            exactCanonicalUser(
                listOf(CanonicalUserProbe(0, marker, null, null)),
                marker,
                expectedClientMessageId,
            ),
        )
        assertNull(
            exactCanonicalUser(
                listOf(
                    CanonicalUserProbe(
                        0,
                        marker,
                        expectedClientMessageId,
                        null,
                    ),
                ),
                marker,
                expectedClientMessageId,
            ),
        )
        assertEquals(
            "turn-expected",
            exactCanonicalUser(
                listOf(
                    CanonicalUserProbe(
                        0,
                        marker,
                        expectedClientMessageId,
                        "turn-expected",
                    ),
                ),
                marker,
                expectedClientMessageId,
            )?.turnId,
        )
        assertTrue(
            runCatching {
                exactCanonicalUser(
                    listOf(
                        CanonicalUserProbe(
                            0,
                            marker,
                            expectedClientMessageId,
                            "turn-expected",
                        ),
                        CanonicalUserProbe(1, marker, "client-other", "turn-other"),
                    ),
                    marker,
                    expectedClientMessageId,
                )
            }.isFailure,
        )
        assertTrue(
            runCatching {
                exactCanonicalUser(
                    listOf(
                        CanonicalUserProbe(
                            0,
                            marker,
                            expectedClientMessageId,
                            "turn-expected",
                        ),
                        CanonicalUserProbe(
                            1,
                            "other",
                            expectedClientMessageId,
                            "turn-duplicate",
                        ),
                    ),
                    marker,
                    expectedClientMessageId,
                )
            }.isFailure,
        )
        assertTrue(
            runCatching {
                exactCanonicalUser(
                    listOf(
                        CanonicalUserProbe(0, marker, "client-other", "turn-marker"),
                        CanonicalUserProbe(
                            1,
                            "other",
                            expectedClientMessageId,
                            "turn-key",
                        ),
                    ),
                    marker,
                    expectedClientMessageId,
                )
            }.isFailure,
        )
    }

    @Test
    fun prepareAtExactBoundaryAndRequireHostProcessDeath(): Unit = runBlocking {
        val arguments = InstrumentationRegistry.getArguments()
        assumeTrue(
            "Exact crash prepare is disabled",
            arguments.getString(ARG_ENABLE) == "true" &&
                arguments.getString(ARG_PHASE) == PHASE_PREPARE,
        )
        val input = GateInput.parse(arguments)
        val context = targetContext()
        val outbox = SharedPreferencesDurableOutboxStore(context)
        assertTrue("A previous exact-crash Outbox did not converge", outbox.list().isEmpty())
        deletePrivateEvidence(context)

        val hook = ExactCrashHook(context, outbox, input)
        val gateway = createRelayGateway(context, hook)
        var owner: ModelOwner? = null
        try {
            awaitOnline(gateway)
            val threadA = gateway.createThread(
                "Exact crash ${input.boundary.wire} ${input.iteration} A",
            )
            val threadB = gateway.createThread(
                "Exact crash ${input.boundary.wire} ${input.iteration} B",
            )
            val identity = gateway.sessionIdentity.value
            val plan = CrashPlan(
                runToken = input.runToken,
                boundary = input.boundary,
                iteration = input.iteration,
                nonce = input.nonce,
                sessionScope = identity.scope,
                sessionGeneration = identity.generation,
                threadA = threadA.key,
                threadB = threadB.key,
                targetMarker = marker(input, "A1"),
                followups = listOf(
                    PlannedMessage(
                        label = "A2",
                        key = OutboxKey(
                            identity.scope,
                            identity.generation,
                            threadA.key,
                            UUID.randomUUID().toString(),
                        ),
                        marker = marker(input, "A2"),
                    ),
                    PlannedMessage(
                        label = "B1",
                        key = OutboxKey(
                            identity.scope,
                            identity.generation,
                            threadB.key,
                            UUID.randomUUID().toString(),
                        ),
                        marker = marker(input, "B1"),
                    ),
                ),
            )
            writePrivateJson(context, PLAN_FILE, plan.toJson())
            hook.arm(plan)
            owner = createViewModel(gateway, outbox, threadA.key, hook)
            awaitSelected(owner.model, threadA.key)

            // ENQUEUE_PERSISTED blocks the main thread inside send(). Every later boundary lets
            // send() return, then blocks the exact production transport/store caller instead.
            onMain {
                assertTrue(
                    "Production AppViewModel rejected the target message",
                    owner.model.send(plan.targetMarker),
                )
            }
            hook.awaitCheckpoint()
            neverReturn()
        } finally {
            // A legitimate prepare is force-stopped and cannot execute this path. Reaching it
            // means the host did not kill the live process at the committed checkpoint.
            owner?.store?.clear()
            (gateway as? CloseableCodexGateway)?.close()
            throw AssertionError("Exact crash prepare returned without host process death")
        }
    }

    @Test
    fun recoverAfterHostProcessDeathAndProveQueueConvergence() = runBlocking {
        val arguments = InstrumentationRegistry.getArguments()
        assumeTrue(
            "Exact crash recovery is disabled",
            arguments.getString(ARG_ENABLE) == "true" &&
                arguments.getString(ARG_PHASE) == PHASE_RECOVER,
        )
        val input = GateInput.parse(arguments)
        val context = targetContext()
        val plan = CrashPlan.fromJson(readPrivateJson(context, PLAN_FILE))
        val checkpoint = CrashCheckpoint.fromJson(readPrivateJson(context, CHECKPOINT_FILE))
        require(plan.matches(input) && checkpoint.matches(plan))

        val outbox = SharedPreferencesDurableOutboxStore(context)
        assertCheckpointStoreSnapshot(outbox, checkpoint)
        val gateway = createRelayGateway(context, NoOpDeliveryCrashTestHook)
        var owner: ModelOwner? = null
        try {
            awaitOnline(gateway)
            assertEquals(plan.sessionScope, gateway.sessionScope)
            assertEquals(plan.sessionGeneration, gateway.sessionIdentity.value.generation)
            owner = createViewModel(
                gateway,
                outbox,
                plan.threadA,
                NoOpDeliveryCrashTestHook,
            )
            awaitSelected(owner.model, plan.threadA)

            val aMessages = awaitCanonicalMessages(
                gateway = gateway,
                threadKey = plan.threadA,
                expected = listOf(
                    ExpectedCanonicalMessage(
                        "A1",
                        plan.targetMarker,
                        checkpoint.targetKey.clientMessageId,
                    ),
                    ExpectedCanonicalMessage(
                        "A2",
                        plan.followup("A2").marker,
                        plan.followup("A2").key.clientMessageId,
                    ),
                ),
                expectedReply = input.expectedReply,
            )
            // Production recovery must complete B1 independently on Thread B. The fixed
            // exact-crash audit proves its isolated completion; the separate load latch Gate
            // proves cross-Thread parallel dispatch without imposing a B1/A2 race order.
            val bMessages = awaitCanonicalMessages(
                gateway = gateway,
                threadKey = plan.threadB,
                expected = listOf(
                    ExpectedCanonicalMessage(
                        "B1",
                        plan.followup("B1").marker,
                        plan.followup("B1").key.clientMessageId,
                    ),
                ),
                expectedReply = input.expectedReply,
            )

            awaitSelected(owner.model, plan.threadB)
            withTimeout(OUTBOX_TIMEOUT_MILLIS) {
                owner.model.uiState.first { state ->
                    state.outbox.isEmpty()
                }
            }
            assertTrue(
                "Production durable Outbox did not converge across every scope/generation",
                outbox.list().isEmpty(),
            )
            val bubbleCounts = mutableListOf(
                uiBubbleCount(owner.model, plan.followup("B1").key),
            )
            awaitSelected(owner.model, plan.threadA)
            bubbleCounts += uiBubbleCount(
                owner.model,
                checkpoint.targetKey,
            )
            bubbleCounts += uiBubbleCount(owner.model, plan.followup("A2").key)
            assertTrue(
                "UI contains a duplicate or missing user bubble",
                bubbleCounts.all { it == 1 },
            )

            val recovered = (aMessages + bMessages).sortedBy(RecoveredMessage::label)
            assertEquals(3, recovered.map(RecoveredMessage::turnId).distinct().size)
            val sameThreadFifo =
                aMessages.single { it.label == "A1" }.userIndex <
                    aMessages.single { it.label == "A2" }.userIndex
            assertTrue("Recovered same-Thread FIFO was reversed", sameThreadFifo)
            val otherThreadIndependent =
                bMessages.single().threadId != aMessages.first().threadId &&
                    bMessages.single().label == "B1"
            assertTrue("Recovered other Thread was not isolated", otherThreadIndependent)
            val finalOutboxEntries = outbox.list().size
            val duplicateBubbles = bubbleCounts.sumOf { (it - 1).coerceAtLeast(0) }
            val result = RecoveryResult(
                plan = plan,
                checkpoint = checkpoint,
                messages = recovered,
                outboxEntries = finalOutboxEntries,
                duplicateBubbles = duplicateBubbles,
                sameThreadFifo = sameThreadFifo,
                otherThreadIndependent = otherThreadIndependent,
            )
            writePrivateJson(context, RECOVERY_FILE, result.toJson())
            signalRecovery(result)
            assertTrue(File(context.filesDir, PLAN_FILE).delete())
            assertTrue(File(context.filesDir, CHECKPOINT_FILE).delete())
        } finally {
            owner?.store?.clear()
            (gateway as? CloseableCodexGateway)?.close()
        }
    }

    private fun createRelayGateway(
        context: Context,
        hook: DeliveryCrashTestHook,
    ): RelayCodexGateway {
        val store = ProcessLockedRelayConfigStore(AndroidKeystoreRelayConfigStore(context))
        val session = requireNotNull(store.loadSession()) {
            "Exact crash gate requires an existing app-private Relay enrollment"
        }
        val trust = RelayTlsTrust.load(context)
        val credentialClient = RelayCredentialClient(
            client = trust.newHttpClient(),
            originValidator = trust::requireAllowedOrigin,
        )
        return RelayCodexGateway(
            sessionStore = store,
            session = session,
            deviceProofKey = AndroidDeviceIdentity.loadExisting(),
            credentialClient = credentialClient,
            sslSocketFactory = trust.socketFactory,
            originValidator = trust::requireAllowedOrigin,
            deliveryCrashTestHook = hook,
        )
    }

    private fun createViewModel(
        gateway: CodexGateway,
        outbox: SharedPreferencesDurableOutboxStore,
        initialThread: ThreadKey,
        hook: DeliveryCrashTestHook,
    ): ModelOwner = onMain {
        val store = ViewModelStore()
        ModelOwner(
            store = store,
            model = ViewModelProvider(
                object : ViewModelStoreOwner {
                    override val viewModelStore: ViewModelStore = store
                },
                AppViewModel.Factory(
                    gateway = gateway,
                    lastThreadStore = InMemoryLastThreadStore(initialThread),
                    outboxStore = outbox,
                    deliveryCrashTestHook = hook,
                ),
            )[AppViewModel::class.java],
        )
    }

    private suspend fun awaitOnline(gateway: CodexGateway) {
        withTimeout(ONLINE_TIMEOUT_MILLIS) {
            gateway.connectionState.first {
                it == com.cheby.codex.mobile.model.GatewayConnectionState.ONLINE
            }
        }
    }

    private suspend fun awaitSelected(model: AppViewModel, threadKey: ThreadKey) {
        onMain { model.selectThread(threadKey) }
        withTimeout(ONLINE_TIMEOUT_MILLIS) {
            model.uiState.first {
                it.currentThreadKey == threadKey &&
                    it.currentConversation?.threadKey == threadKey &&
                    it.canSend &&
                    !it.isConversationRecovering
            }
        }
    }

    private suspend fun awaitCanonicalMessages(
        gateway: CodexGateway,
        threadKey: ThreadKey,
        expected: List<ExpectedCanonicalMessage>,
        expectedReply: String,
    ): List<RecoveredMessage> = withTimeout(TURN_TIMEOUT_MILLIS) {
        while (true) {
            val snapshot = gateway.loadConversation(threadKey)
            val recovered = mutableListOf<RecoveredMessage>()
            var incomplete = false
            expected.forEach { expectation ->
                val user = exactCanonicalUser(
                    candidates = snapshot.messages.withIndex()
                        .filter { (_, message) -> message.role == MessageRole.USER }
                        .map { (index, message) ->
                            CanonicalUserProbe(
                                index = index,
                                body = message.readableBody(),
                                clientMessageId = message.clientMessageId,
                                turnId = message.turnId,
                            )
                        },
                    expectedBody = expectation.marker,
                    expectedClientMessageId = expectation.clientMessageId,
                )
                if (user == null) {
                    incomplete = true
                    return@forEach
                }
                val clientMessageId = requireNotNull(user.clientMessageId)
                val turnId = requireNotNull(user.turnId)
                val assistants = snapshot.messages.filter { message ->
                    message.turnId == turnId &&
                        message.role == MessageRole.ASSISTANT
                }
                val completedIndex = exactCompletedAssistantIndex(
                    assistants.map {
                        AssistantProbe(it.state.name, it.readableBody())
                    },
                    expectedReply,
                )
                if (completedIndex == null) {
                    incomplete = true
                } else {
                    recovered += RecoveredMessage(
                        label = expectation.label,
                        sessionScope = gateway.sessionScope,
                        sessionGeneration = gateway.sessionIdentity.value.generation,
                        threadId = threadKey.value,
                        clientMessageId = clientMessageId,
                        turnId = turnId,
                        userIndex = user.index,
                        turnCount = snapshot.messages.count {
                            it.role == MessageRole.USER &&
                                it.clientMessageId == expectation.clientMessageId
                        },
                    )
                }
            }
            if (!incomplete && recovered.size == expected.size) {
                return@withTimeout recovered
            }
            delay(POLL_MILLIS)
        }
        error("unreachable")
    }

    private fun uiBubbleCount(model: AppViewModel, key: OutboxKey): Int {
        val state = model.uiState.value
        assertEquals(key.threadKey, state.currentConversation?.threadKey)
        val canonical = state.messages.count {
            it.role == MessageRole.USER && it.clientMessageId == key.clientMessageId
        }
        val optimistic = state.acceptedUserMessages.count {
            it.sessionScope == key.sessionScope &&
                it.sessionGeneration == key.sessionGeneration &&
                it.threadKey == key.threadKey &&
                it.clientMessageId == key.clientMessageId
        }
        val queued = state.outbox.count { it.key == key }
        return canonical + optimistic + queued
    }

    private fun assertCheckpointStoreSnapshot(
        store: SharedPreferencesDurableOutboxStore,
        checkpoint: CrashCheckpoint,
    ) {
        val rows = store.list()
        val target = rows.filter { it.key == checkpoint.targetKey }
        when (checkpoint.targetState) {
            TARGET_ABSENT -> assertTrue(target.isEmpty())
            OutboxState.QUEUED.name -> {
                assertEquals(1, target.size)
                assertEquals(OutboxState.QUEUED, target.single().state)
            }
            OutboxState.ACCEPTED.name -> {
                assertEquals(1, target.size)
                assertEquals(OutboxState.ACCEPTED, target.single().state)
                assertEquals(checkpoint.turnId, target.single().acceptedTurnId)
            }
            else -> error("Invalid exact-crash target state")
        }
        checkpoint.pendingKeys.forEach { key ->
            assertEquals(
                OutboxState.QUEUED,
                rows.single { it.key == key }.state,
            )
        }
    }

    private fun signalRecovery(result: RecoveryResult) {
        InstrumentationRegistry.getInstrumentation().sendStatus(
            EVIDENCE_STATUS_CODE,
            Bundle().apply {
                putString(EVIDENCE_CHECKPOINT, "exact_crash_recovered")
                putString(EVIDENCE_BOUNDARY, result.plan.boundary.wire)
                putString(EVIDENCE_RUN_TOKEN, result.plan.runToken)
                putInt(EVIDENCE_ITERATION, result.plan.iteration)
                putInt(EVIDENCE_TURNS, result.messages.size)
                putInt(EVIDENCE_THREADS, result.messages.map { it.threadId }.distinct().size)
                putInt(EVIDENCE_OUTBOX, result.outboxEntries)
                putInt(EVIDENCE_DUPLICATES, result.duplicateBubbles)
                putBoolean(EVIDENCE_FIFO, result.sameThreadFifo)
                putBoolean(EVIDENCE_OTHER_THREAD, result.otherThreadIndependent)
                putString(EVIDENCE_TUPLE_DIGEST, result.tupleDigest())
                putString(EVIDENCE_BUSINESS_KEY_DIGEST, result.businessKeyDigest())
            },
        )
    }

    private class ExactCrashHook(
        private val context: Context,
        private val store: SharedPreferencesDurableOutboxStore,
        private val input: GateInput,
    ) : DeliveryCrashTestHook {
        override val isNoOp: Boolean = false
        private val claimed = AtomicBoolean(false)
        private val targetClientMessageId = AtomicReference<String?>()
        private val checkpointWritten = CountDownLatch(1)
        @Volatile private var plan: CrashPlan? = null

        fun arm(value: CrashPlan) {
            check(plan == null)
            plan = value
        }

        override fun reached(
            checkpoint: DeliveryCrashCheckpoint,
            identity: DeliveryCrashIdentity,
        ) {
            val active = plan ?: return
            if (
                identity.sessionScope != active.sessionScope ||
                identity.sessionGeneration != active.sessionGeneration ||
                identity.threadKey != active.threadA
            ) return
            if (checkpoint == DeliveryCrashCheckpoint.ENQUEUE_PERSISTED) {
                targetClientMessageId.compareAndSet(null, identity.clientMessageId)
            }
            if (
                identity.clientMessageId != targetClientMessageId.get() ||
                checkpoint != input.boundary.checkpoint ||
                !claimed.compareAndSet(false, true)
            ) return

            val targetBefore = store.list().singleOrNull { it.key == identity.outboxKey() }
            val baseTime = targetBefore?.createdAtMillis ?: System.currentTimeMillis()
            active.followups.forEachIndexed { index, message ->
                check(
                    store.enqueue(
                        OutboxEntry(
                            clientMessageId = message.key.clientMessageId,
                            sessionScope = message.key.sessionScope,
                            sessionGeneration = message.key.sessionGeneration,
                            threadKey = message.key.threadKey,
                            text = message.marker,
                            state = OutboxState.QUEUED,
                            createdAtMillis = baseTime + index + 1,
                        ),
                    ),
                )
            }
            val rows = store.list()
            val targetRows = rows.filter { it.key == identity.outboxKey() }
            val expectedState = when (checkpoint) {
                DeliveryCrashCheckpoint.ENQUEUE_PERSISTED,
                DeliveryCrashCheckpoint.NETWORK_SENT_UNCONFIRMED,
                DeliveryCrashCheckpoint.TRANSPORT_ACCEPTED,
                -> OutboxState.QUEUED.name
                DeliveryCrashCheckpoint.ANDROID_ACCEPTED -> OutboxState.ACCEPTED.name
                DeliveryCrashCheckpoint.OUTBOX_CLEANED -> TARGET_ABSENT
            }
            when (expectedState) {
                TARGET_ABSENT -> check(targetRows.isEmpty())
                OutboxState.QUEUED.name ->
                    check(targetRows.singleOrNull()?.state == OutboxState.QUEUED)
                OutboxState.ACCEPTED.name -> {
                    val target = targetRows.single()
                    check(target.state == OutboxState.ACCEPTED)
                    check(target.acceptedTurnId == identity.turnId)
                }
            }
            val pending = active.followups.map { message ->
                rows.single { it.key == message.key }.also {
                    check(it.state == OutboxState.QUEUED)
                }
            }
            val record = CrashCheckpoint(
                runToken = active.runToken,
                boundary = active.boundary,
                iteration = active.iteration,
                nonce = active.nonce,
                pid = Process.myPid(),
                targetKey = identity.outboxKey(),
                targetState = expectedState,
                turnId = identity.turnId,
                pendingKeys = pending.map(OutboxEntry::key),
                businessKeyDigest = calculateBusinessKeyDigest(
                    listOf(identity.outboxKey()) + pending.map(OutboxEntry::key),
                ),
            )
            writePrivateJson(context, CHECKPOINT_FILE, record.toJson())
            checkpointWritten.countDown()
            neverReturn()
        }

        fun awaitCheckpoint() {
            checkpointWritten.await()
            neverReturn()
        }
    }

    private data class GateInput(
        val runToken: String,
        val boundary: ExactBoundary,
        val iteration: Int,
        val nonce: String,
        val expectedReply: String,
    ) {
        companion object {
            fun parse(arguments: Bundle): GateInput {
                val runToken = requireNotNull(arguments.getString(ARG_RUN_TOKEN))
                val boundary = ExactBoundary.fromWire(
                    requireNotNull(arguments.getString(ARG_BOUNDARY)),
                )
                val iteration = requireNotNull(arguments.getString(ARG_ITERATION)).toInt()
                val nonce = requireNotNull(arguments.getString(ARG_NONCE))
                require(HEX_32.matches(runToken) && HEX_32.matches(nonce))
                require(iteration in 1..REQUIRED_REPETITIONS)
                return GateInput(
                    runToken,
                    boundary,
                    iteration,
                    nonce,
                    arguments.requireExpectedReply(),
                )
            }
        }
    }

    private enum class ExactBoundary(
        val wire: String,
        val checkpoint: DeliveryCrashCheckpoint,
    ) {
        ENQUEUE("ENQUEUE", DeliveryCrashCheckpoint.ENQUEUE_PERSISTED),
        NETWORK("NETWORK", DeliveryCrashCheckpoint.NETWORK_SENT_UNCONFIRMED),
        ACCEPTED("ACCEPTED", DeliveryCrashCheckpoint.TRANSPORT_ACCEPTED),
        ANDROID("ANDROID", DeliveryCrashCheckpoint.ANDROID_ACCEPTED),
        CLEANED("CLEANED", DeliveryCrashCheckpoint.OUTBOX_CLEANED);

        companion object {
            fun fromWire(value: String): ExactBoundary =
                entries.singleOrNull { it.wire == value } ?: error("Invalid exact crash boundary")
        }
    }

    private data class PlannedMessage(
        val label: String,
        val key: OutboxKey,
        val marker: String,
    ) {
        fun toJson() = JSONObject().apply {
            put("label", label)
            put("scope", key.sessionScope)
            put("generation", key.sessionGeneration)
            put("threadId", key.threadKey.value)
            put("clientMessageId", key.clientMessageId)
            put("marker", marker)
        }

        companion object {
            fun fromJson(value: JSONObject) = PlannedMessage(
                label = value.getString("label"),
                key = OutboxKey(
                    value.getString("scope"),
                    value.getLong("generation"),
                    ThreadKey(value.getString("threadId")),
                    value.getString("clientMessageId"),
                ),
                marker = value.getString("marker"),
            )
        }
    }

    private data class CrashPlan(
        val runToken: String,
        val boundary: ExactBoundary,
        val iteration: Int,
        val nonce: String,
        val sessionScope: String,
        val sessionGeneration: Long,
        val threadA: ThreadKey,
        val threadB: ThreadKey,
        val targetMarker: String,
        val followups: List<PlannedMessage>,
    ) {
        fun followup(label: String) = followups.single { it.label == label }

        fun matches(input: GateInput) =
            runToken == input.runToken &&
                boundary == input.boundary &&
                iteration == input.iteration &&
                nonce == input.nonce

        fun toJson() = JSONObject().apply {
            put("schema", PLAN_SCHEMA)
            put("runToken", runToken)
            put("boundary", boundary.wire)
            put("iteration", iteration)
            put("nonce", nonce)
            put("scope", sessionScope)
            put("generation", sessionGeneration)
            put("threadA", threadA.value)
            put("threadB", threadB.value)
            put("targetMarker", targetMarker)
            put("followups", JSONArray().apply { followups.forEach { put(it.toJson()) } })
        }

        companion object {
            fun fromJson(value: JSONObject): CrashPlan {
                require(value.getString("schema") == PLAN_SCHEMA)
                val followups = value.getJSONArray("followups")
                return CrashPlan(
                    runToken = value.getString("runToken"),
                    boundary = ExactBoundary.fromWire(value.getString("boundary")),
                    iteration = value.getInt("iteration"),
                    nonce = value.getString("nonce"),
                    sessionScope = value.getString("scope"),
                    sessionGeneration = value.getLong("generation"),
                    threadA = ThreadKey(value.getString("threadA")),
                    threadB = ThreadKey(value.getString("threadB")),
                    targetMarker = value.getString("targetMarker"),
                    followups = List(followups.length()) {
                        PlannedMessage.fromJson(followups.getJSONObject(it))
                    },
                )
            }
        }
    }

    private data class CrashCheckpoint(
        val runToken: String,
        val boundary: ExactBoundary,
        val iteration: Int,
        val nonce: String,
        val pid: Int,
        val targetKey: OutboxKey,
        val targetState: String,
        val turnId: String?,
        val pendingKeys: List<OutboxKey>,
        val businessKeyDigest: String,
    ) {
        fun matches(plan: CrashPlan) =
            runToken == plan.runToken &&
                boundary == plan.boundary &&
                iteration == plan.iteration &&
                nonce == plan.nonce &&
                targetKey.sessionScope == plan.sessionScope &&
                targetKey.sessionGeneration == plan.sessionGeneration &&
                targetKey.threadKey == plan.threadA &&
                pendingKeys == plan.followups.map(PlannedMessage::key) &&
                businessKeyDigest == calculateBusinessKeyDigest(listOf(targetKey) + pendingKeys)

        fun toJson() = JSONObject().apply {
            put("schema", CHECKPOINT_SCHEMA)
            put("runToken", runToken)
            put("boundary", boundary.wire)
            put("iteration", iteration)
            put("nonce", nonce)
            put("pid", pid)
            put("target", targetKey.toJson(targetState, turnId))
            put("pending", JSONArray().apply {
                pendingKeys.forEach { put(it.toJson(OutboxState.QUEUED.name, null)) }
            })
            put("businessKeySha256", businessKeyDigest)
            put("normalJunitMustNotReturn", true)
        }

        companion object {
            fun fromJson(value: JSONObject): CrashCheckpoint {
                require(value.getString("schema") == CHECKPOINT_SCHEMA)
                require(value.getBoolean("normalJunitMustNotReturn"))
                val target = value.getJSONObject("target")
                val pending = value.getJSONArray("pending")
                return CrashCheckpoint(
                    runToken = value.getString("runToken"),
                    boundary = ExactBoundary.fromWire(value.getString("boundary")),
                    iteration = value.getInt("iteration"),
                    nonce = value.getString("nonce"),
                    pid = value.getInt("pid"),
                    targetKey = target.outboxKey(),
                    targetState = target.getString("state"),
                    turnId = target.optString("turnId").takeIf(String::isNotEmpty),
                    pendingKeys = List(pending.length()) { pending.getJSONObject(it).outboxKey() },
                    businessKeyDigest = value.getString("businessKeySha256"),
                )
            }
        }
    }

    private data class RecoveredMessage(
        val label: String,
        val sessionScope: String,
        val sessionGeneration: Long,
        val threadId: String,
        val clientMessageId: String,
        val turnId: String,
        val userIndex: Int,
        val turnCount: Int,
    ) {
        fun toJson() = JSONObject().apply {
            put("label", label)
            put("scope", sessionScope)
            put("generation", sessionGeneration)
            put("threadId", threadId)
            put("clientMessageId", clientMessageId)
            put("turnId", turnId)
            put("turnCount", turnCount)
        }
    }

    private data class RecoveryResult(
        val plan: CrashPlan,
        val checkpoint: CrashCheckpoint,
        val messages: List<RecoveredMessage>,
        val outboxEntries: Int,
        val duplicateBubbles: Int,
        val sameThreadFifo: Boolean,
        val otherThreadIndependent: Boolean,
    ) {
        fun tupleDigest(): String = calculateTupleDigest(messages)

        fun businessKeyDigest(): String = calculateBusinessKeyDigest(
            messages.map {
                OutboxKey(
                    it.sessionScope,
                    it.sessionGeneration,
                    ThreadKey(it.threadId),
                    it.clientMessageId,
                )
            },
        )

        fun toJson() = JSONObject().apply {
            put("schema", RECOVERY_SCHEMA)
            put("runToken", plan.runToken)
            put("boundary", plan.boundary.wire)
            put("iteration", plan.iteration)
            put("nonce", plan.nonce)
            put("killedPid", checkpoint.pid)
            put("messages", JSONArray().apply { messages.forEach { put(it.toJson()) } })
            put("tupleSha256", tupleDigest())
            put("businessKeySha256", businessKeyDigest())
            put("outboxEntries", outboxEntries)
            put("duplicateBubbles", duplicateBubbles)
            put("sameThreadFifo", sameThreadFifo)
            put("otherThreadIndependent", otherThreadIndependent)
        }
    }

    private data class ModelOwner(
        val store: ViewModelStore,
        val model: AppViewModel,
    )

    private fun RichMessage.readableBody() =
        orderedBlocks().joinToString("\n") { it.readableText() }.trim()

    private fun marker(input: GateInput, label: String) =
        "EXACT-${input.runToken}-${input.boundary.wire}-${input.iteration}-$label"

    private fun targetContext() =
        InstrumentationRegistry.getInstrumentation().targetContext.applicationContext

    private fun deletePrivateEvidence(context: Context) {
        listOf(PLAN_FILE, CHECKPOINT_FILE, RECOVERY_FILE).forEach { name ->
            val file = File(context.filesDir, name)
            check(!file.exists() || file.delete())
            val temporary = File(context.filesDir, "$name.tmp")
            check(!temporary.exists() || temporary.delete())
        }
    }

    private fun <T> onMain(block: () -> T): T {
        var result: Result<T>? = null
        InstrumentationRegistry.getInstrumentation().runOnMainSync {
            result = runCatching(block)
        }
        return requireNotNull(result).getOrThrow()
    }

    private companion object {
        const val ARG_ENABLE = "cheby_exact_crash_gate"
        const val ARG_PHASE = "cheby_exact_crash_phase"
        const val ARG_RUN_TOKEN = "cheby_exact_crash_run_token"
        const val ARG_BOUNDARY = "cheby_exact_crash_boundary"
        const val ARG_ITERATION = "cheby_exact_crash_iteration"
        const val ARG_NONCE = "cheby_exact_crash_nonce"
        const val PHASE_PREPARE = "prepare"
        const val PHASE_RECOVER = "recover"
        const val REQUIRED_REPETITIONS = 25
        const val PLAN_FILE = "cheby-exact-crash-plan-v1.json"
        const val CHECKPOINT_FILE = "cheby-exact-crash-checkpoint-v1.json"
        const val RECOVERY_FILE = "cheby-exact-crash-recovery-v1.json"
        const val PLAN_SCHEMA = "chebycodex.android-exact-crash-plan.v1"
        const val CHECKPOINT_SCHEMA = "chebycodex.android-exact-crash-checkpoint.v1"
        const val RECOVERY_SCHEMA = "chebycodex.android-exact-crash-recovery.v1"
        const val TARGET_ABSENT = "ABSENT"
        const val EVIDENCE_STATUS_CODE = 2
        const val EVIDENCE_CHECKPOINT = "cheby_exact_checkpoint"
        const val EVIDENCE_BOUNDARY = "cheby_exact_boundary"
        const val EVIDENCE_RUN_TOKEN = "cheby_exact_run_token"
        const val EVIDENCE_ITERATION = "cheby_exact_iteration"
        const val EVIDENCE_TURNS = "cheby_exact_turns"
        const val EVIDENCE_THREADS = "cheby_exact_threads"
        const val EVIDENCE_OUTBOX = "cheby_exact_outbox_entries"
        const val EVIDENCE_DUPLICATES = "cheby_exact_duplicate_bubbles"
        const val EVIDENCE_FIFO = "cheby_exact_same_thread_fifo"
        const val EVIDENCE_OTHER_THREAD = "cheby_exact_other_thread_independent"
        const val EVIDENCE_TUPLE_DIGEST = "cheby_exact_tuple_sha256"
        const val EVIDENCE_BUSINESS_KEY_DIGEST = "cheby_exact_business_key_sha256"
        const val ONLINE_TIMEOUT_MILLIS = 180_000L
        const val TURN_TIMEOUT_MILLIS = 15 * 60 * 1_000L
        const val OUTBOX_TIMEOUT_MILLIS = 180_000L
        const val POLL_MILLIS = 100L
        val HEX_32 = Regex("[a-f0-9]{32}")
        fun DeliveryCrashIdentity.outboxKey() = OutboxKey(
            sessionScope,
            sessionGeneration,
            threadKey,
            clientMessageId,
        )

        fun OutboxKey.toJson(state: String, turnId: String?) = JSONObject().apply {
            put("scope", sessionScope)
            put("generation", sessionGeneration)
            put("threadId", threadKey.value)
            put("clientMessageId", clientMessageId)
            put("state", state)
            turnId?.let { put("turnId", it) }
        }

        fun JSONObject.outboxKey() = OutboxKey(
            getString("scope"),
            getLong("generation"),
            ThreadKey(getString("threadId")),
            getString("clientMessageId"),
        )

        fun readPrivateJson(context: Context, name: String) =
            JSONObject(File(context.filesDir, name).readText(Charsets.UTF_8))

        fun writePrivateJson(context: Context, name: String, value: JSONObject) {
            val destination = File(context.filesDir, name)
            val temporary = File(context.filesDir, "$name.tmp")
            check(!temporary.exists() || temporary.delete())
            temporary.writeText(value.toString(), Charsets.UTF_8)
            check(temporary.setReadable(false, false))
            check(temporary.setWritable(false, false))
            check(temporary.setReadable(true, true))
            check(temporary.setWritable(true, true))
            check(temporary.renameTo(destination))
        }

        fun calculateBusinessKeyDigest(keys: List<OutboxKey>): String =
            sha256Hex(keys.sortedWith(
                compareBy(
                    OutboxKey::sessionScope,
                    OutboxKey::sessionGeneration,
                    { it.threadKey.value },
                    OutboxKey::clientMessageId,
                ),
            ).flatMap { key ->
                listOf(
                    key.sessionScope,
                    key.sessionGeneration.toString(),
                    key.threadKey.value,
                    key.clientMessageId,
                )
            })

        fun calculateTupleDigest(messages: List<RecoveredMessage>): String =
            sha256Hex(messages.sortedWith(
                compareBy(
                    RecoveredMessage::threadId,
                    RecoveredMessage::clientMessageId,
                    RecoveredMessage::turnId,
                ),
            ).flatMap { listOf(it.threadId, it.clientMessageId, it.turnId) })

        fun sha256Hex(values: List<String>): String {
            val digest = MessageDigest.getInstance("SHA-256")
            values.forEach { value ->
                digest.update(value.toByteArray(Charsets.UTF_8))
                digest.update(0)
            }
            return digest.digest().joinToString("") { "%02x".format(it) }
        }

        fun neverReturn(): Nothing {
            while (true) {
                try {
                    CountDownLatch(1).await()
                } catch (_: InterruptedException) {
                    // Only host force-stop is accepted. Coroutine/thread interruption must not
                    // turn the exact checkpoint into a normal JUnit completion.
                }
            }
        }
    }
}

private data class ExpectedCanonicalMessage(
    val label: String,
    val marker: String,
    val clientMessageId: String,
)

private data class CanonicalUserProbe(
    val index: Int,
    val body: String,
    val clientMessageId: String?,
    val turnId: String?,
)

/**
 * Returns null while either the marker or its exact business key is not projected yet. Once both
 * exist they must identify the same single canonical user; every duplicate or split projection
 * fails closed instead of being mistaken for normal asynchronous recovery.
 */
private fun exactCanonicalUser(
    candidates: List<CanonicalUserProbe>,
    expectedBody: String,
    expectedClientMessageId: String,
): CanonicalUserProbe? {
    val markerMatches = candidates.filter { it.body == expectedBody }
    require(markerMatches.size <= 1) {
        "Canonical user message was duplicated"
    }
    val keyMatches = candidates.filter {
        it.clientMessageId == expectedClientMessageId
    }
    require(keyMatches.size <= 1) {
        "Canonical user business key was duplicated"
    }
    if (markerMatches.isEmpty() || keyMatches.isEmpty()) return null
    val markerMatch = markerMatches.single()
    val keyMatch = keyMatches.single()
    require(markerMatch.index == keyMatch.index) {
        "Canonical user marker and business key were split"
    }
    if (markerMatch.turnId == null) return null
    return markerMatch
}

private data class AssistantProbe(
    val state: String,
    val body: String,
)

/**
 * Returns the single completed candidate, waits on zero/non-terminal candidates, and fails closed
 * on every duplicate, terminal failure, or completed response lacking the expected evidence.
 */
private fun exactCompletedAssistantIndex(
    candidates: List<AssistantProbe>,
    expectedReply: String,
): Int? {
    if (candidates.isEmpty()) return null
    require(candidates.size == 1) {
        "Canonical assistant message was duplicated"
    }
    val candidate = candidates.single()
    return when (candidate.state) {
        RichMessageState.COMPLETED.name -> {
            require(candidate.body.contains(expectedReply)) {
                "Completed assistant message lacked expected evidence"
            }
            0
        }
        RichMessageState.FAILED.name,
        RichMessageState.INTERRUPTED.name,
        RichMessageState.CANCELLED.name,
        -> error("Turn reached a terminal failure")
        else -> null
    }
}
