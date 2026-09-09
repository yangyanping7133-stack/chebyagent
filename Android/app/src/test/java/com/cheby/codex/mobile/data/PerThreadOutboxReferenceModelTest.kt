package com.cheby.codex.mobile.data

import com.cheby.codex.mobile.model.ThreadKey
import kotlin.random.Random
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Independent executable specification for per-Thread delivery.
 *
 * The model intentionally does not call AppViewModel or reuse its state decisions. Production is
 * observed only through [PerThreadOutboxPlanner.dispatchable].
 */
class PerThreadOutboxReferenceModelTest {
    @Test
    fun `exhaustive legal interleavings to depth ten match independent two thread model`() {
        val initial = ReferenceState(
            rows = listOf(
                row("a-1", A, 1),
                row("a-2", A, 2),
                row("b-1", B, 3),
                row("b-2", B, 4),
            ),
        )
        var visited = 0

        fun explore(state: ReferenceState, depth: Int) {
            assertPlannerMatches(state)
            visited += 1
            if (depth == 10) return
            state.legalTransitions().forEach { next -> explore(next, depth + 1) }
        }

        explore(initial, 0)

        assertTrue("expected broad interleaving coverage, visited=$visited", visited >= 1_000)
    }

    @Test
    fun `one thousand fixed seeds of two hundred steps match reference model`() {
        repeat(1_000) { seed ->
            val random = Random(seed)
            var nextId = 0
            var state = ReferenceState()
            repeat(200) {
                state = when (random.nextInt(7)) {
                    0, 1 -> {
                        val thread = if (random.nextBoolean()) A else B
                        nextId += 1
                        state.enqueue(row("${thread.value}-$nextId", thread, nextId.toLong()))
                    }
                    2 -> state.dispatchOne(random)
                    3 -> state.acceptOne(random)
                    4 -> state.finishOne(random)
                    5 -> state.busyOne(random)
                    else -> state.restart()
                }
                assertPlannerMatches(state)
                state.assertInvariants()
            }
        }
    }

    @Test
    fun `negative control global fifo is detected`() {
        val state = ReferenceState(listOf(row("a-1", A, 1), row("b-1", B, 2)))
        val expected = state.dispatchableIds()
        val mutant = expected.take(1)
        assertEquals(setOf("a-1", "b-1"), expected)
        assertNotEquals(expected, mutant)
    }

    @Test
    fun `same client id in flight on one thread does not serialize another thread`() {
        val threadA = row("shared-message", A, 1).toProduction()
        val threadB = row("shared-message", B, 2).toProduction()

        val dispatchable = PerThreadOutboxPlanner.dispatchable(
            entries = listOf(threadA, threadB),
            sessionScope = SCOPE,
            sessionGeneration = GENERATION,
            nowMillis = NOW,
            inFlightOutboxKeys = setOf(threadA.key),
        )

        assertEquals(listOf(threadB.key), dispatchable.map(OutboxEntry::key))
    }

    @Test
    fun `same millisecond messages preserve durable enqueue order instead of random id order`() {
        val enqueuedFirst = row("zzzz-first-message", A, 1).toProduction()
        val enqueuedSecond = row("aaaa-second-message", A, 1).toProduction()

        val dispatchable = PerThreadOutboxPlanner.dispatchable(
            entries = listOf(enqueuedFirst, enqueuedSecond),
            sessionScope = SCOPE,
            sessionGeneration = GENERATION,
            nowMillis = NOW,
            inFlightOutboxKeys = emptySet(),
        )

        assertEquals(listOf(enqueuedFirst.key), dispatchable.map(OutboxEntry::key))
        assertEquals(
            enqueuedFirst.key,
            PerThreadOutboxPlanner.headForThread(
                entries = listOf(enqueuedFirst, enqueuedSecond),
                sessionScope = SCOPE,
                sessionGeneration = GENERATION,
                threadKey = A,
            )?.key,
        )
    }

    @Test
    fun `negative control same thread concurrency is detected`() {
        val state = ReferenceState(listOf(row("a-1", A, 1), row("a-2", A, 2)))
        val mutant = state.rows.filter { it.phase == Phase.QUEUED }.map(ReferenceRow::id).toSet()
        assertEquals(setOf("a-1"), state.dispatchableIds())
        assertEquals(setOf("a-1", "a-2"), mutant)
    }

    @Test
    fun `negative control missing accepted persistence is detected`() {
        val accepted = ReferenceState(listOf(row("a-1", A, 1).copy(phase = Phase.ACCEPTED)))
        val mutantAfterRestart = accepted.copy(
            rows = accepted.rows.map { it.copy(phase = Phase.QUEUED) },
        )
        assertTrue(accepted.restart().dispatchableIds().isEmpty())
        assertEquals(setOf("a-1"), mutantAfterRestart.dispatchableIds())
    }

    @Test
    fun `negative control wrong turn terminal release is detected`() {
        val state = ReferenceState(
            listOf(
                row("a-1", A, 1).copy(phase = Phase.ACCEPTED, turnId = "turn-correct"),
                row("a-2", A, 2),
            ),
        )
        val reference = state.terminal(A, "turn-wrong")
        val mutant = state.copy(rows = state.rows.filterNot { it.id == "a-1" })
        assertTrue(reference.dispatchableIds().isEmpty())
        assertEquals(setOf("a-2"), mutant.dispatchableIds())
    }

    @Test
    fun `negative control restart resends accepted is detected`() {
        val state = ReferenceState(
            listOf(row("b-1", B, 1).copy(phase = Phase.ACCEPTED, turnId = "turn-b-1")),
        )
        assertTrue(state.restart().dispatchableIds().isEmpty())
        assertFalse(
            state.copy(rows = state.rows.map { it.copy(phase = Phase.QUEUED) })
                .dispatchableIds()
                .isEmpty(),
        )
    }

    @Test
    fun `negative control bypasses waiting or needs sync head is detected`() {
        listOf(Phase.WAITING, Phase.NEEDS_SYNC).forEach { blocked ->
            val state = ReferenceState(
                listOf(
                    row("a-1", A, 1).copy(phase = blocked, turnId = "turn-running"),
                    row("a-2", A, 2),
                ),
            )
            val mutant = state.rows.first { it.id == "a-2" }.id
            assertTrue(state.dispatchableIds().isEmpty())
            assertEquals("a-2", mutant)
        }
    }

    private fun assertPlannerMatches(state: ReferenceState) {
        val actual = PerThreadOutboxPlanner.dispatchable(
            entries = state.rows.map(ReferenceRow::toProduction),
            sessionScope = SCOPE,
            sessionGeneration = GENERATION,
            nowMillis = NOW,
            inFlightOutboxKeys = state.rows
                .filter { it.phase == Phase.IN_FLIGHT }
                .map { it.toProduction().key }
                .toSet(),
        ).map(OutboxEntry::clientMessageId).toSet()
        assertEquals(state.dispatchableIds(), actual)
    }

    private data class ReferenceState(
        val rows: List<ReferenceRow> = emptyList(),
    ) {
        fun dispatchableIds(): Set<String> = rows
            .groupBy(ReferenceRow::thread)
            .values
            .mapNotNull { queue ->
                queue.firstOrNull()
                    ?.takeIf { it.phase == Phase.QUEUED || it.phase == Phase.RETRYABLE }
            }
            .map(ReferenceRow::id)
            .toSet()

        fun enqueue(row: ReferenceRow): ReferenceState =
            if (rows.any { it.id == row.id }) this else copy(rows = rows + row)

        fun dispatchOne(random: Random): ReferenceState {
            val id = dispatchableIds().sorted().randomOrNull(random) ?: return this
            return update(id) { it.copy(phase = Phase.IN_FLIGHT) }
        }

        fun acceptOne(random: Random): ReferenceState {
            val row = rows.filter { it.phase == Phase.IN_FLIGHT }.randomOrNull(random) ?: return this
            return update(row.id) { it.copy(phase = Phase.ACCEPTED, turnId = "turn-${it.id}") }
        }

        fun busyOne(random: Random): ReferenceState {
            val row = rows.filter { it.phase == Phase.IN_FLIGHT }.randomOrNull(random) ?: return this
            return update(row.id) { it.copy(phase = Phase.WAITING, turnId = "active-${it.thread.value}") }
        }

        fun finishOne(random: Random): ReferenceState {
            val row = rows.filter {
                it.phase == Phase.ACCEPTED || it.phase == Phase.WAITING
            }.randomOrNull(random) ?: return this
            return terminal(row.thread, requireNotNull(row.turnId))
        }

        fun terminal(thread: ThreadKey, turnId: String): ReferenceState {
            val matching = rows.firstOrNull {
                it.thread == thread &&
                    it.turnId == turnId &&
                    (it.phase == Phase.ACCEPTED || it.phase == Phase.WAITING)
            } ?: return this
            return when (matching.phase) {
                Phase.ACCEPTED -> copy(rows = rows.filterNot { it.id == matching.id })
                Phase.WAITING -> update(matching.id) {
                    it.copy(phase = Phase.QUEUED, turnId = null)
                }
                else -> this
            }
        }

        fun restart(): ReferenceState = copy(
            rows = rows.map {
                if (it.phase == Phase.IN_FLIGHT) {
                    // Crash after the network boundary is ambiguous until snapshot reconciliation.
                    it.copy(phase = Phase.NEEDS_SYNC)
                } else {
                    it
                }
            },
        )

        fun legalTransitions(): List<ReferenceState> = buildList {
            dispatchableIds().forEach { id -> add(update(id) { it.copy(phase = Phase.IN_FLIGHT) }) }
            rows.filter { it.phase == Phase.IN_FLIGHT }.forEach { row ->
                add(update(row.id) { it.copy(phase = Phase.ACCEPTED, turnId = "turn-${it.id}") })
                add(update(row.id) { it.copy(phase = Phase.WAITING, turnId = "active-${it.thread.value}") })
            }
            rows.filter { it.phase == Phase.ACCEPTED || it.phase == Phase.WAITING }.forEach {
                add(terminal(it.thread, requireNotNull(it.turnId)))
            }
        }

        fun assertInvariants() {
            rows.groupBy(ReferenceRow::thread).values.forEach { queue ->
                val ordered = queue
                assertEquals(ordered.map(ReferenceRow::id).distinct().size, ordered.size)
                val active = ordered.count {
                    it.phase == Phase.IN_FLIGHT || it.phase == Phase.ACCEPTED
                }
                assertTrue(active <= 1)
                if (ordered.firstOrNull()?.phase in setOf(
                        Phase.WAITING,
                        Phase.ACCEPTED,
                        Phase.NEEDS_SYNC,
                    )
                ) {
                    assertTrue(ordered.drop(1).none { it.id in dispatchableIds() })
                }
            }
        }

        private fun update(id: String, transform: (ReferenceRow) -> ReferenceRow): ReferenceState =
            copy(rows = rows.map { if (it.id == id) transform(it) else it })
    }

    private data class ReferenceRow(
        val id: String,
        val thread: ThreadKey,
        val order: Long,
        val phase: Phase = Phase.QUEUED,
        val turnId: String? = null,
    ) {
        fun toProduction(): OutboxEntry = OutboxEntry(
            clientMessageId = id,
            sessionScope = SCOPE,
            threadKey = thread,
            text = id,
            state = when (phase) {
                Phase.QUEUED, Phase.IN_FLIGHT -> OutboxState.QUEUED
                Phase.RETRYABLE -> OutboxState.RETRYABLE
                Phase.ACCEPTED -> OutboxState.ACCEPTED
                Phase.WAITING -> OutboxState.WAITING_TURN
                Phase.NEEDS_SYNC -> OutboxState.NEEDS_SYNC
            },
            createdAtMillis = order,
            sessionGeneration = GENERATION,
            acceptedTurnId = turnId.takeIf { phase == Phase.ACCEPTED },
            waitingForTurnId = turnId.takeIf { phase == Phase.WAITING },
        )
    }

    private enum class Phase {
        QUEUED,
        RETRYABLE,
        IN_FLIGHT,
        ACCEPTED,
        WAITING,
        NEEDS_SYNC,
    }

    private fun row(id: String, thread: ThreadKey, order: Long) =
        ReferenceRow(id, thread, order)

    private companion object {
        val A = ThreadKey("thread-a")
        val B = ThreadKey("thread-b")
        const val SCOPE = "scope-reference"
        const val GENERATION = 7L
        const val NOW = 10_000L
    }
}
