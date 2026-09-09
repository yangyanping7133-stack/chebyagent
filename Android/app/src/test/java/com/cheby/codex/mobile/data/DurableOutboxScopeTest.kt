package com.cheby.codex.mobile.data

import com.cheby.codex.mobile.model.ThreadKey
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class DurableOutboxScopeTest {
    @Test
    fun `prepared image entry atomically becomes retryable asset input only for same generation`() {
        val prepared = OutboxEntry(
            clientMessageId = "client-image-message-001",
            sessionScope = "scope-a",
            threadKey = ThreadKey("thread-a"),
            text = "inspect",
            state = OutboxState.NEEDS_ATTACHMENTS,
            createdAtMillis = 1,
            sessionGeneration = 3,
            pendingImageCount = 2,
        )
        val store = InMemoryDurableOutboxStore()
        assertTrue(store.enqueue(prepared))
        val ready = prepared.copy(
            state = OutboxState.QUEUED,
            pendingImageCount = 0,
            imageAssetRefs = listOf(
                "ast_abcdefghijklmnopqrstuvwxyz0123456789",
                "ast_0123456789abcdefghijklmnopqrstuvwxyz",
            ),
        )

        assertFalse(store.replacePrepared(ready, "scope-a", 3, "scope-a", 4))
        assertEquals(prepared, store.list().single())
        assertTrue(store.replacePrepared(ready, "scope-a", 3, "scope-a", 3))
        assertEquals(ready, store.list().single())
    }

    @Test
    fun `outbox rejects path shaped asset reference`() {
        val invalid = OutboxEntry(
            clientMessageId = "client-image-message-001",
            sessionScope = "scope-a",
            threadKey = ThreadKey("thread-a"),
            text = "",
            state = OutboxState.QUEUED,
            createdAtMillis = 1,
            imageAssetRefs = listOf("/asset-staging/private-image.png"),
        )

        assertFalse(InMemoryDurableOutboxStore().enqueue(invalid))
    }

    @Test
    fun `outbox accepts ten image pages and rejects eleven`() {
        val tenRefs = (1..10).map { index -> "ast_" + index.toString().padStart(32, '0') }
        val accepted = entry("ten-pages", "scope-a").copy(imageAssetRefs = tenRefs)
        val rejected = entry("eleven-pages", "scope-a").copy(
            imageAssetRefs = tenRefs + ("ast_" + "11".padStart(32, '0')),
        )

        assertTrue(InMemoryDurableOutboxStore().enqueue(accepted))
        assertFalse(InMemoryDurableOutboxStore().enqueue(rejected))
    }

    @Test
    fun `process restart adopts current scope while live generation switch removes stale generation`() {
        val stale = entry("a", "scope-a").copy(sessionGeneration = 7)
        val otherScope = entry("b", "scope-b").copy(sessionGeneration = 9)
        val store = InMemoryDurableOutboxStore(listOf(stale, otherScope))

        assertTrue(store.adoptScopeGeneration("scope-a", 7, 0))
        assertEquals(0, store.list().first { it.sessionScope == "scope-a" }.sessionGeneration)
        assertEquals(9, store.list().first { it.sessionScope == "scope-b" }.sessionGeneration)

        assertTrue(store.clearOtherGenerations("scope-a", 1))
        assertEquals(listOf(otherScope), store.list())
    }

    @Test
    fun `generation adoption moves only exact predecessor and cannot revive older rows`() {
        val older = entry("older", "scope-a").copy(sessionGeneration = 4)
        val predecessor = entry("current", "scope-a").copy(sessionGeneration = 5)
        val store = InMemoryDurableOutboxStore(listOf(older, predecessor))

        assertTrue(store.adoptScopeGeneration("scope-a", 5, 6))
        assertEquals(4, store.list().single { it.clientMessageId == "older" }.sessionGeneration)
        assertEquals(6, store.list().single { it.clientMessageId == "current" }.sessionGeneration)

        assertTrue(store.clearOtherGenerations("scope-a", 6))
        assertEquals(listOf("current"), store.list().map(OutboxEntry::clientMessageId))
    }

    @Test
    fun `late A mark and remove cannot mutate same scope B generation`() {
        val b = entry("shared-message", "scope-a").copy(sessionGeneration = 2)
        val store = InMemoryDurableOutboxStore(listOf(b))

        assertFalse(
            store.mark(
                key = b.key.copy(sessionGeneration = 1),
                activeSessionScope = "scope-a",
                activeSessionGeneration = 2,
                state = OutboxState.REJECTED,
            ),
        )
        assertFalse(
            store.remove(
                key = b.key.copy(sessionGeneration = 1),
                activeSessionScope = "scope-a",
                activeSessionGeneration = 2,
            ),
        )
        assertEquals(listOf(b), store.list())
    }

    @Test
    fun `same client id cannot silently accept different image payload`() {
        val original = entry("same-message", "scope-a")
        val changed = original.copy(
            imageAssetRefs = listOf("ast_abcdefghijklmnopqrstuvwxyz0123456789"),
        )
        val store = InMemoryDurableOutboxStore()

        assertTrue(store.enqueue(original))
        assertFalse(store.enqueue(changed))
        assertEquals(listOf(original), store.list())
    }

    @Test
    fun `same client id in two threads is marked and removed by exact business key`() {
        val threadA = entry("shared-message", "scope-a")
            .copy(threadKey = ThreadKey("thread-a"), sessionGeneration = 7)
        val threadB = threadA.copy(threadKey = ThreadKey("thread-b"), text = "thread-b payload")
        val store = InMemoryDurableOutboxStore()

        assertTrue(store.enqueue(threadA))
        assertTrue(store.enqueue(threadB))
        assertTrue(
            store.mark(
                key = threadA.key,
                activeSessionScope = "scope-a",
                activeSessionGeneration = 7,
                state = OutboxState.REJECTED,
            ),
        )
        assertEquals(OutboxState.REJECTED, store.list().single { it.key == threadA.key }.state)
        assertEquals(OutboxState.QUEUED, store.list().single { it.key == threadB.key }.state)

        assertTrue(
            store.remove(
                key = threadA.key,
                activeSessionScope = "scope-a",
                activeSessionGeneration = 7,
            ),
        )
        assertEquals(listOf(threadB), store.list())
    }

    @Test
    fun `invalidation removes only invalidated scope`() {
        val a = entry("a", "scope-a")
        val b = entry("b", "scope-b")
        val store = InMemoryDurableOutboxStore(listOf(a, b))

        assertTrue(store.clearScope("scope-a"))

        assertEquals(listOf(b), store.list())
    }

    @Test
    fun `pairing cleanup preserves entries already queued for new scope`() {
        val staleA = entry("a", "scope-a")
        val newB = entry("b", "scope-b")
        val unrelated = entry("c", "scope-c")
        val store = InMemoryDurableOutboxStore(listOf(staleA, newB, unrelated))

        assertTrue(store.clearOtherScopes("scope-b"))

        assertEquals(listOf(newB), store.list())
    }

    @Test
    fun `scope cleanup fails without mutating when durable writes fail`() {
        val a = entry("a", "scope-a")
        val store = InMemoryDurableOutboxStore(listOf(a), allowWrites = false)

        assertFalse(store.clearScope("scope-a"))
        assertFalse(store.clearOtherScopes("scope-b"))

        assertEquals(listOf(a), store.list())
    }

    private fun entry(id: String, scope: String): OutboxEntry = OutboxEntry(
        clientMessageId = id,
        sessionScope = scope,
        threadKey = ThreadKey("thread-$id"),
        text = "text-$id",
        state = OutboxState.QUEUED,
        createdAtMillis = 1,
    )
}
