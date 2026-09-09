package com.cheby.codex.mobile.data

import com.cheby.codex.mobile.model.ThreadKey

/**
 * Pure, deterministic scheduling policy for the durable outbox.
 *
 * Durable list order is the enqueue order and therefore the head of each Thread FIFO. Wall-clock
 * timestamps and random client ids are deliberately not used as ordering keys: two messages may
 * be enqueued in the same millisecond. A blocked head can never be bypassed, while eligible heads
 * from different Threads are returned together so callers may run them concurrently.
 */
internal object PerThreadOutboxPlanner {
    fun dispatchable(
        entries: List<OutboxEntry>,
        sessionScope: String,
        sessionGeneration: Long,
        nowMillis: Long,
        inFlightOutboxKeys: Set<OutboxKey>,
    ): List<OutboxEntry> = entries.asSequence()
        .filter {
            it.sessionScope == sessionScope &&
            it.sessionGeneration == sessionGeneration
        }
        .groupBy(OutboxEntry::threadKey)
        .values
        .mapNotNull { threadEntries ->
            val head = threadEntries.firstOrNull() ?: return@mapNotNull null
            if (head.key in inFlightOutboxKeys) return@mapNotNull null
            when (head.state) {
                OutboxState.QUEUED -> head
                OutboxState.RETRYABLE ->
                    head.takeIf { it.retryNotBeforeMillis <= nowMillis }
                OutboxState.ACCEPTED,
                OutboxState.WAITING_TURN,
                OutboxState.NEEDS_SYNC,
                OutboxState.REJECTED,
                OutboxState.NEEDS_ATTACHMENTS,
                -> null
            }
        }
        .toList()

    fun headForThread(
        entries: List<OutboxEntry>,
        sessionScope: String,
        sessionGeneration: Long,
        threadKey: ThreadKey,
    ): OutboxEntry? = entries.asSequence()
        .filter {
            it.sessionScope == sessionScope &&
                it.sessionGeneration == sessionGeneration &&
                it.threadKey == threadKey
        }
        .firstOrNull()
}
