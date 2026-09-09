package com.cheby.codex.mobile.gateway

import android.os.Bundle
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.cheby.codex.mobile.model.CodexThread
import com.cheby.codex.mobile.model.GatewayConnectionState
import com.cheby.codex.mobile.model.MessageRole
import com.cheby.codex.mobile.model.RichMessage
import com.cheby.codex.mobile.model.RichMessageState
import com.cheby.codex.mobile.model.ThreadKey
import com.cheby.codex.mobile.model.orderedBlocks
import com.cheby.codex.mobile.model.readableText
import java.io.File
import java.util.UUID
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Opt-in real Relay gate for an already enrolled installation.
 *
 * This test deliberately has no enrollment input and never reads credentials directly. The first
 * executable statement is the opt-in guard, so ordinary instrumentation runs cannot construct a
 * Relay gateway or reach the public network. It also uses no Activity, Espresso, UiAutomator or
 * shell-driven UI interaction.
 */
@RunWith(AndroidJUnit4::class)
class RelayHeadlessE2EInstrumentedTest {
    @Test
    fun pairedPrivateSessionSupportsThreadsTurnsAndReconstruction() = runBlocking {
        assumeTrue(
            "Real Relay E2E is disabled unless $ENABLE_ARGUMENT=true is supplied",
            InstrumentationRegistry.getArguments().getString(ENABLE_ARGUMENT) == "true",
        )

        val context = InstrumentationRegistry.getInstrumentation().targetContext.applicationContext
        val runToken = UUID.randomUUID().toString().replace("-", "").uppercase()
        val expectedReply = InstrumentationRegistry.getArguments().requireExpectedReply()
        var gateway: CodexGateway? = null
        var threadA: CodexThread? = null
        var threadB: CodexThread? = null

        try {
            val initialGateway = GatewayFactory.create(context)
            gateway = initialGateway
            requirePairedRelay(initialGateway)
            awaitOnline(initialGateway)

            val createdThreadA = initialGateway.createThread(
                "ChebyCodex E2E A ${runToken.takeLast(6)}",
            )
            threadA = createdThreadA
            val renamedTitle = "ChebyCodex E2E renamed ${runToken.takeLast(6)}"
            initialGateway.renameThread(createdThreadA.key, renamedTitle)
            val originalScope = initialGateway.sessionScope
            initialGateway.closeForTest()
            gateway = null

            // Reconstruct before the first turn. A freshly started Codex Thread can be readable
            // while still absent from thread/list, so this exercises the provisional server-side
            // identity path as well as the Android initial-refresh ordering.
            val preTurnReconstructed = GatewayFactory.create(context)
            gateway = preTurnReconstructed
            requirePairedRelay(preTurnReconstructed)
            assertTrue(
                "The durable Relay binding changed before the first turn",
                preTurnReconstructed.sessionScope == originalScope,
            )
            awaitOnline(preTurnReconstructed)
            withTimeout(STATE_TIMEOUT_MILLIS) {
                preTurnReconstructed.threads.first { threads ->
                    threads.any { it.key == createdThreadA.key && it.title == renamedTitle }
                }
            }
            val beforeFirstTurn = preTurnReconstructed.loadConversation(createdThreadA.key)
            val firstMarker = "Gate deterministic turn A ${runToken.take(16)}"
            val firstSubmission = preTurnReconstructed.sendTurn(
                threadKey = createdThreadA.key,
                text = firstMarker,
                idempotencyKey = UUID.randomUUID().toString(),
                expectedSessionScope = preTurnReconstructed.sessionScope,
            )
            val firstReply = awaitCompletedAssistant(
                gateway = preTurnReconstructed,
                threadKey = createdThreadA.key,
                excludedMessageIds = beforeFirstTurn.messages.mapTo(
                    mutableSetOf(),
                    RichMessage::id,
                ),
                expectedTurnId = firstSubmission.turnId,
                expectedText = expectedReply,
            )

            awaitCausallyConfirmedConnectorRestart(
                gateway = preTurnReconstructed,
                acknowledgementFile = File(context.filesDir, RESTART_ACK_FILE),
            )

            threadB = preTurnReconstructed.createThread("ChebyCodex E2E B ${runToken.takeLast(6)}")
            // Loading B and then A exercises the same thread-selection boundary used by the UI,
            // without ever operating the screen.
            preTurnReconstructed.loadConversation(threadB.key)
            val resumedA = preTurnReconstructed.loadConversation(createdThreadA.key)
            assertTrue(
                "Thread A lost its completed first reply after switching threads",
                resumedA.messages.any { it.id == firstReply.id },
            )

            val beforeContinuation = resumedA.messages.mapTo(mutableSetOf(), RichMessage::id)
            val continuationSubmission = preTurnReconstructed.sendTurn(
                threadKey = createdThreadA.key,
                text = "Gate deterministic continuation A ${runToken.takeLast(16)}",
                idempotencyKey = UUID.randomUUID().toString(),
                expectedSessionScope = preTurnReconstructed.sessionScope,
            )
            val continuationReply = awaitCompletedAssistant(
                gateway = preTurnReconstructed,
                threadKey = createdThreadA.key,
                excludedMessageIds = beforeContinuation,
                expectedTurnId = continuationSubmission.turnId,
                expectedText = expectedReply,
                requiredRememberedMarker = firstMarker,
            )

            val continuationScope = preTurnReconstructed.sessionScope
            preTurnReconstructed.closeForTest()
            gateway = null

            // Rebuild every process-owned Gateway object from the app-private durable binding.
            // A literal OS process kill cannot resume the same instrumentation method; this is the
            // strongest single-invocation persistence boundary available without a host-side phase.
            val reconstructed = GatewayFactory.create(context)
            gateway = reconstructed
            requirePairedRelay(reconstructed)
            assertTrue(
                "The durable Relay binding changed during process-level reconstruction",
                reconstructed.sessionScope == continuationScope,
            )
            awaitOnline(reconstructed)
            val reconstructedA = reconstructed.loadConversation(createdThreadA.key)
            val reconstructedContinuation = reconstructedA.messages.singleOrNull {
                it.id == continuationReply.id &&
                    it.turnId == continuationSubmission.turnId &&
                    it.role == MessageRole.ASSISTANT &&
                    it.state == RichMessageState.COMPLETED
            }
            requireNotNull(reconstructedContinuation) {
                "Thread A could not recover the completed continuation exactly once"
            }
            assertTrue(
                "The completed continuation lost its explicit pre-restart memory evidence",
                reconstructedContinuation.readableBody()
                    .hasRememberedLine(firstMarker),
            )
            signalHeadlessEvidenceComplete()
        } finally {
            val cleanupGateway = gateway
            if (cleanupGateway != null) {
                threadB?.let { runCatching { cleanupGateway.archiveThread(it.key) } }
                threadA?.let { runCatching { cleanupGateway.archiveThread(it.key) } }
                cleanupGateway.closeForTest()
            }
        }
    }

    private suspend fun requirePairedRelay(gateway: CodexGateway) {
        val kind = withTimeout(STATE_TIMEOUT_MILLIS) {
            gateway.kind.first()
        }
        assertTrue(
            "Real Relay E2E requires an existing app-private paired Relay session",
            kind == GatewayKind.RELAY,
        )
    }

    private suspend fun awaitOnline(gateway: CodexGateway) {
        withTimeout(ONLINE_TIMEOUT_MILLIS) {
            gateway.connectionState.first { it == GatewayConnectionState.ONLINE }
        }
    }

    private fun signalConnectorRestartRequired(nonce: String) {
        InstrumentationRegistry.getInstrumentation().sendStatus(
            EVIDENCE_STATUS_CODE,
            Bundle().apply {
                putString(RESTART_CHECKPOINT_KEY, RESTART_CHECKPOINT_VALUE)
                putString(RESTART_NONCE_KEY, nonce)
            },
        )
    }

    private fun signalHeadlessEvidenceComplete() {
        InstrumentationRegistry.getInstrumentation().sendStatus(
            EVIDENCE_STATUS_CODE,
            Bundle().apply {
                putString(RESTART_CHECKPOINT_KEY, COMPLETE_CHECKPOINT_VALUE)
                putBoolean(RESTART_OBSERVED_KEY, true)
                putBoolean(RESTART_CAUSAL_ACK_KEY, true)
                putBoolean(REMEMBERED_MARKER_VERIFIED_KEY, true)
                putInt(EVIDENCE_THREADS_KEY, 2)
                putInt(EVIDENCE_TURNS_KEY, 2)
            },
        )
    }

    private suspend fun awaitCausallyConfirmedConnectorRestart(
        gateway: CodexGateway,
        acknowledgementFile: File,
    ) = coroutineScope {
        if (acknowledgementFile.exists()) {
            assertTrue(
                "A stale Connector restart acknowledgement could not be removed",
                acknowledgementFile.delete(),
            )
        }
        val nonce = UUID.randomUUID().toString().replace("-", "").lowercase()
        assertTrue(
            "The Connector restart checkpoint was emitted from a non-online state",
            gateway.connectionState.value == GatewayConnectionState.ONLINE,
        )
        signalConnectorRestartRequired(nonce)
        // StateFlow retains the current value, so starting after the checkpoint
        // cannot miss the restart transition and cannot consume a stale
        // pre-checkpoint offline state.
        val offlineObservation = async(start = CoroutineStart.UNDISPATCHED) {
            withTimeout(RESTART_OFFLINE_TIMEOUT_MILLIS) {
                gateway.connectionState.first { it != GatewayConnectionState.ONLINE }
            }
        }
        withTimeout(RESTART_ACK_TIMEOUT_MILLIS) {
            while (true) {
                val acknowledged = runCatching {
                    acknowledgementFile.takeIf(File::isFile)
                        ?.readText(Charsets.US_ASCII)
                        ?.trim() == nonce
                }.getOrDefault(false)
                if (acknowledged) break
                delay(RESTART_ACK_POLL_MILLIS)
            }
        }
        assertTrue(
            "The Connector restart acknowledgement could not be consumed",
            acknowledgementFile.delete(),
        )
        offlineObservation.await()
        awaitOnline(gateway)
    }

    private suspend fun awaitCompletedAssistant(
        gateway: CodexGateway,
        threadKey: ThreadKey,
        excludedMessageIds: Set<String>,
        expectedTurnId: String,
        expectedText: String,
        requiredRememberedMarker: String? = null,
    ): RichMessage = withTimeout(TURN_TIMEOUT_MILLIS) {
        while (true) {
            val messages = gateway.loadConversation(threadKey).messages
            messages.firstOrNull { message ->
                message.id !in excludedMessageIds &&
                    message.turnId == expectedTurnId &&
                    message.role == MessageRole.ASSISTANT &&
                    message.state == RichMessageState.COMPLETED &&
                    message.orderedBlocks().joinToString("\n") { it.readableText() }
                        .let { body ->
                            body.contains(expectedText) &&
                                (
                                    requiredRememberedMarker == null ||
                                        body.hasRememberedLine(requiredRememberedMarker)
                                    )
                        }
            }?.let { return@withTimeout it }

            assertTrue(
                "The Relay turn reached a terminal failure before producing completed text",
                messages.none { message ->
                    message.id !in excludedMessageIds &&
                        message.turnId == expectedTurnId &&
                        message.role == MessageRole.ASSISTANT &&
                        message.state in TERMINAL_FAILURE_STATES
                },
            )
            delay(POLL_INTERVAL_MILLIS)
        }
        error("unreachable")
    }

    private fun CodexGateway.closeForTest() {
        (this as? CloseableCodexGateway)?.close()
    }

    private fun RichMessage.readableBody(): String =
        orderedBlocks().joinToString("\n") { it.readableText() }

    private fun String.hasRememberedLine(marker: String): Boolean =
        lineSequence().any { it == "Remembered: $marker" }

    private companion object {
        const val ENABLE_ARGUMENT = "cheby_relay_e2e"
        const val EVIDENCE_STATUS_CODE = 2
        const val RESTART_CHECKPOINT_KEY = "cheby_gate_checkpoint"
        const val RESTART_CHECKPOINT_VALUE = "connector_restart_required"
        const val COMPLETE_CHECKPOINT_VALUE = "headless_gate_complete"
        const val RESTART_NONCE_KEY = "cheby_gate_restart_nonce"
        const val RESTART_OBSERVED_KEY = "cheby_gate_restart_observed"
        const val RESTART_CAUSAL_ACK_KEY = "cheby_gate_restart_causal_ack"
        const val REMEMBERED_MARKER_VERIFIED_KEY = "cheby_gate_remembered_marker_verified"
        const val EVIDENCE_THREADS_KEY = "cheby_gate_threads"
        const val EVIDENCE_TURNS_KEY = "cheby_gate_turns"
        const val RESTART_ACK_FILE = "cheby-gate-restart-ack-v1"
        const val STATE_TIMEOUT_MILLIS = 5_000L
        const val ONLINE_TIMEOUT_MILLIS = 90_000L
        const val RESTART_OFFLINE_TIMEOUT_MILLIS = 180_000L
        const val RESTART_ACK_TIMEOUT_MILLIS = 180_000L
        const val RESTART_ACK_POLL_MILLIS = 100L
        const val TURN_TIMEOUT_MILLIS = 300_000L
        const val POLL_INTERVAL_MILLIS = 1_000L
        val TERMINAL_FAILURE_STATES = setOf(
            RichMessageState.FAILED,
            RichMessageState.INTERRUPTED,
            RichMessageState.CANCELLED,
        )
    }
}
