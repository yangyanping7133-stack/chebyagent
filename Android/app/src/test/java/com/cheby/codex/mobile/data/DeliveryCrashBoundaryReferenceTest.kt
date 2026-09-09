package com.cheby.codex.mobile.data

import com.cheby.codex.mobile.model.ThreadKey
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Model-only oracle spanning the ten public durability boundaries.
 *
 * This deliberately does not execute AppViewModel or any production store. It is useful only for
 * negative-model mutation coverage and must never be reported as crash, process-kill, persistence,
 * or release-gate evidence. Production-store reconstruction, subprocess SIGKILL, and real-device
 * process-death gates live separately.
 */
class DeliveryCrashBoundaryReferenceTest {
    @Test
    fun `model oracle spans ten boundaries twenty five times`() {
        CrashBoundary.entries.forEach { boundary ->
            repeat(25) { iteration ->
                val harness = DeliveryHarness()
                val first = key("a-first-$iteration", A)
                val sameThreadNext = key("a-next-$iteration", A)
                val otherThread = key("b-$iteration", B)
                harness.enqueue(first, order = 1)
                harness.enqueue(sameThreadNext, order = 2)
                harness.enqueue(otherThread, order = 3)

                harness.advanceUntilCrash(first, boundary)
                harness.crashAndRestart()

                // B must make progress while A2 remains behind the exact A1 head.
                harness.complete(otherThread)
                assertEquals(1, harness.turnCount(otherThread))
                assertEquals(0, harness.turnCount(sameThreadNext))

                harness.recoverAndDrain()
                harness.assertFinal(first, sameThreadNext, otherThread)
                assertEquals(listOf(first, sameThreadNext), harness.turnOrder.filter { it.thread == A })
                if (boundary == CrashBoundary.CODEX_ACCEPTED) {
                    assertEquals(FinalOutcome.OPERATION_OUTCOME_UNKNOWN, harness.outcomes[first])
                } else {
                    assertEquals(FinalOutcome.CONFIRMED, harness.outcomes[first])
                }
            }
        }
    }

    private class DeliveryHarness {
        private val outbox = linkedMapOf<BusinessKey, OutboxRecord>()
        private val relay = linkedMapOf<BusinessKey, BoundaryRecord>()
        private val connectorIntents = linkedMapOf<BusinessKey, BoundaryRecord>()
        private val gatewayReservations = linkedMapOf<BusinessKey, BoundaryRecord>()
        private val codexTurns = linkedMapOf<BusinessKey, BoundaryRecord>()
        private val activeDeliveries = mutableSetOf<BusinessKey>()
        private val activeIntents = mutableSetOf<BusinessKey>()
        private val activeReservations = mutableSetOf<BusinessKey>()
        private val activeTurnsByThread = mutableMapOf<ThreadKey, BusinessKey>()
        private val effectCounts = mutableMapOf<BusinessKey, Int>()
        val outcomes = mutableMapOf<BusinessKey, FinalOutcome>()
        val turnOrder = mutableListOf<BusinessKey>()

        fun enqueue(key: BusinessKey, order: Long) {
            outbox.putIfAbsent(key, OutboxRecord(order, DurablePhase.QUEUED))
        }

        fun advanceUntilCrash(key: BusinessKey, crash: CrashBoundary) {
            for (boundary in CrashBoundary.entries) {
                advance(key, boundary, crash == CrashBoundary.CODEX_ACCEPTED)
                if (boundary == crash) return
            }
            error("unknown crash boundary")
        }

        fun crashAndRestart() {
            activeTurnsByThread.clear()
            // Volatile ownership is rebuilt only from durable reservations/turns.
            codexTurns.keys.filter { key ->
                outbox[key]?.phase == DurablePhase.QUEUED ||
                    outbox[key]?.phase == DurablePhase.ACCEPTED
            }.forEach { key -> activeTurnsByThread.putIfAbsent(key.thread, key) }
            val uncertain = codexTurns.keys.filter {
                effectCounts.getOrDefault(it, 0) > 0 && outcomes[it] == null
            }
            uncertain.forEach { outcomes[it] = FinalOutcome.OPERATION_OUTCOME_UNKNOWN }
        }

        fun complete(key: BusinessKey) {
            val head = head(key.thread)
            require(head == key) { "same-Thread FIFO bypass: $key before $head" }
            CrashBoundary.entries.forEach { boundary -> advance(key, boundary, uncertainCrash = false) }
        }

        fun recoverAndDrain() {
            while (outbox.isNotEmpty()) {
                val heads = outbox.keys.groupBy(BusinessKey::thread).values
                    .map { threadKeys -> threadKeys.minBy { outbox.getValue(it).order } }
                    .sortedWith(compareBy({ outbox.getValue(it).order }, BusinessKey::clientMessageId))
                assertTrue("recovery made no progress", heads.isNotEmpty())
                heads.forEach(::complete)
            }
        }

        fun turnCount(key: BusinessKey): Int = if (key in codexTurns) 1 else 0

        fun assertFinal(vararg keys: BusinessKey) {
            keys.forEach { key ->
                assertEquals(1, turnCount(key))
                assertTrue(effectCounts.getOrDefault(key, 0) <= 1)
                assertEquals(key, relay.getValue(key).key)
                assertEquals(key, connectorIntents.getValue(key).key)
                assertEquals(key, gatewayReservations.getValue(key).key)
                assertEquals(key, codexTurns.getValue(key).key)
                assertTrue(outcomes[key] == FinalOutcome.CONFIRMED ||
                    outcomes[key] == FinalOutcome.OPERATION_OUTCOME_UNKNOWN)
            }
            assertTrue(outbox.isEmpty())
            assertTrue(activeDeliveries.isEmpty())
            assertTrue(activeIntents.isEmpty())
            assertTrue(activeReservations.isEmpty())
            assertTrue(activeTurnsByThread.isEmpty())
            assertFalse(outcomes.values.any { it == FinalOutcome.ACCEPTED_WITHOUT_TERMINAL })
        }

        private fun advance(
            key: BusinessKey,
            boundary: CrashBoundary,
            uncertainCrash: Boolean,
        ) {
            when (boundary) {
                CrashBoundary.ENQUEUE_PERSISTED -> require(key in outbox)
                CrashBoundary.NETWORK_SENT -> {
                    relay.putIfAbsent(key, BoundaryRecord(key))
                    activeDeliveries += key
                }
                CrashBoundary.RELAY_SUBMITTED -> {
                    relay.putIfAbsent(key, BoundaryRecord(key))
                    activeDeliveries += key
                }
                CrashBoundary.ACCEPTED_SENT -> {
                    relay.putIfAbsent(key, BoundaryRecord(key))
                    activeDeliveries += key
                }
                CrashBoundary.CONNECTOR_INTENT -> {
                    connectorIntents.putIfAbsent(key, BoundaryRecord(key))
                    activeIntents += key
                }
                CrashBoundary.GATEWAY_RESERVATION -> {
                    gatewayReservations.putIfAbsent(key, BoundaryRecord(key))
                    activeReservations += key
                }
                CrashBoundary.CODEX_ACCEPTED -> {
                    gatewayReservations.putIfAbsent(key, BoundaryRecord(key))
                    connectorIntents.putIfAbsent(key, BoundaryRecord(key))
                    relay.putIfAbsent(key, BoundaryRecord(key))
                    if (codexTurns.putIfAbsent(key, BoundaryRecord(key)) == null) {
                        turnOrder += key
                    }
                    activeTurnsByThread.putIfAbsent(key.thread, key)
                    if (effectCounts.getOrDefault(key, 0) == 0) effectCounts[key] = 1
                    if (!uncertainCrash && outcomes[key] == null) {
                        outcomes[key] = FinalOutcome.CONFIRMED
                    }
                }
                CrashBoundary.ANDROID_ACCEPTED -> {
                    require(key in codexTurns)
                    outbox.computeIfPresent(key) { _, row ->
                        row.copy(phase = DurablePhase.ACCEPTED)
                    }
                }
                CrashBoundary.TERMINAL_COMMITTED -> {
                    require(key in codexTurns)
                    outcomes.putIfAbsent(key, FinalOutcome.CONFIRMED)
                    outbox.computeIfPresent(key) { _, row ->
                        row.copy(
                            phase = if (
                                outcomes[key] == FinalOutcome.OPERATION_OUTCOME_UNKNOWN
                            ) {
                                DurablePhase.OPERATION_OUTCOME_UNKNOWN
                            } else {
                                DurablePhase.TERMINAL
                            },
                        )
                    }
                    if (activeTurnsByThread[key.thread] == key) {
                        activeTurnsByThread.remove(key.thread)
                    }
                }
                CrashBoundary.OUTBOX_CLEANED -> {
                    require(outcomes[key] != null)
                    outbox.remove(key)
                    activeDeliveries -= key
                    activeIntents -= key
                    activeReservations -= key
                    if (activeTurnsByThread[key.thread] == key) {
                        activeTurnsByThread.remove(key.thread)
                    }
                }
            }
        }

        private fun head(thread: ThreadKey): BusinessKey? = outbox.entries
            .filter { it.key.thread == thread }
            .minByOrNull { it.value.order }
            ?.key
    }

    private data class BusinessKey(
        val bindingScope: String,
        val generation: Long,
        val thread: ThreadKey,
        val clientMessageId: String,
    )

    private data class BoundaryRecord(val key: BusinessKey)
    private data class OutboxRecord(val order: Long, val phase: DurablePhase)

    private enum class DurablePhase {
        QUEUED,
        ACCEPTED,
        TERMINAL,
        OPERATION_OUTCOME_UNKNOWN,
    }

    private enum class FinalOutcome {
        CONFIRMED,
        OPERATION_OUTCOME_UNKNOWN,
        ACCEPTED_WITHOUT_TERMINAL,
    }

    private enum class CrashBoundary {
        ENQUEUE_PERSISTED,
        NETWORK_SENT,
        RELAY_SUBMITTED,
        ACCEPTED_SENT,
        CONNECTOR_INTENT,
        GATEWAY_RESERVATION,
        CODEX_ACCEPTED,
        ANDROID_ACCEPTED,
        TERMINAL_COMMITTED,
        OUTBOX_CLEANED,
    }

    private fun key(messageId: String, thread: ThreadKey) = BusinessKey(
        bindingScope = "binding-fixed-phone",
        generation = 7,
        thread = thread,
        clientMessageId = messageId,
    )

    private companion object {
        val A = ThreadKey("thread-a")
        val B = ThreadKey("thread-b")
    }
}
