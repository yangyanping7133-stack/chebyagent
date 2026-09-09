package com.cheby.codex.mobile.data

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.cheby.codex.mobile.model.ThreadKey
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Android durable-store reconstruction coverage for the delivery crash gate.
 *
 * This is intentionally not described as process death: it reconstructs independent store
 * instances against the APK's real SharedPreferences journal and production per-Thread planner.
 * Device process-kill evidence remains a separate real-device release gate.
 */
@RunWith(AndroidJUnit4::class)
class DeliveryCrashRecoveryProductionInstrumentedTest {
    @Test
    fun durableOwnerRecoversTwentyFiveTimesWithoutResendingAcceptedOrBypassingHead() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        repeat(25) { iteration ->
            val scope = "crash-gate-scope-$iteration"
            val generation = 7L
            val threadA = ThreadKey("crash-gate-a-$iteration")
            val threadB = ThreadKey("crash-gate-b-$iteration")
            val first = entry(scope, generation, threadA, "crash-$iteration-a1", 1)
            val sameThreadNext = entry(scope, generation, threadA, "crash-$iteration-a2", 2)
            val otherThread = entry(scope, generation, threadB, "crash-$iteration-b1", 3)

            val writer = SharedPreferencesDurableOutboxStore(context)
            assertTrue(writer.clear())
            assertTrue(writer.enqueue(first))
            assertTrue(writer.enqueue(sameThreadNext))
            assertTrue(writer.enqueue(otherThread))

            // Process recreation after ENQUEUE_PERSISTED keeps all four business-key fields.
            val afterEnqueueCrash = SharedPreferencesDurableOutboxStore(context)
            assertEquals(
                listOf(first.key, sameThreadNext.key, otherThread.key),
                afterEnqueueCrash.list().map(OutboxEntry::key),
            )
            assertEquals(
                setOf(first.key, otherThread.key),
                dispatchable(afterEnqueueCrash, scope, generation).mapTo(mutableSetOf()) {
                    it.key
                },
            )

            // Android's ACCEPTED commit is the no-resubmit fence. A reconstruction may still
            // advance Thread B, but neither accepted A1 nor queued A2 can be selected.
            assertTrue(
                afterEnqueueCrash.mark(
                    first.key,
                    scope,
                    generation,
                    OutboxState.ACCEPTED,
                    acceptedTurnId = "turn-$iteration-a1",
                ),
            )
            val afterAcceptedCrash = SharedPreferencesDurableOutboxStore(context)
            val accepted = afterAcceptedCrash.list().first { it.key == first.key }
            assertEquals(OutboxState.ACCEPTED, accepted.state)
            assertEquals("turn-$iteration-a1", accepted.acceptedTurnId)
            assertEquals(
                listOf(otherThread.key),
                dispatchable(afterAcceptedCrash, scope, generation).map(OutboxEntry::key),
            )

            // A matching terminal event removes only A1. The next A message then becomes the
            // production FIFO head while B remains independently dispatchable.
            assertTrue(afterAcceptedCrash.remove(first.key, scope, generation))
            val afterTerminalCrash = SharedPreferencesDurableOutboxStore(context)
            assertEquals(
                setOf(sameThreadNext.key, otherThread.key),
                dispatchable(afterTerminalCrash, scope, generation).mapTo(mutableSetOf()) {
                    it.key
                },
            )
            assertTrue(afterTerminalCrash.remove(sameThreadNext.key, scope, generation))
            assertTrue(afterTerminalCrash.remove(otherThread.key, scope, generation))
            assertTrue(afterTerminalCrash.list().isEmpty())
        }
    }

    private fun dispatchable(
        store: SharedPreferencesDurableOutboxStore,
        scope: String,
        generation: Long,
    ): List<OutboxEntry> = PerThreadOutboxPlanner.dispatchable(
        entries = store.list(),
        sessionScope = scope,
        sessionGeneration = generation,
        nowMillis = 0,
        inFlightOutboxKeys = emptySet(),
    )

    private fun entry(
        scope: String,
        generation: Long,
        thread: ThreadKey,
        clientMessageId: String,
        order: Long,
    ) = OutboxEntry(
        clientMessageId = clientMessageId,
        sessionScope = scope,
        sessionGeneration = generation,
        threadKey = thread,
        text = clientMessageId,
        state = OutboxState.QUEUED,
        createdAtMillis = order,
    )
}
