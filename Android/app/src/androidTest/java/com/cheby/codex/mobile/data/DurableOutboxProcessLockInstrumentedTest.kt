package com.cheby.codex.mobile.data

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.cheby.codex.mobile.model.ThreadKey
import java.util.concurrent.CyclicBarrier
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class DurableOutboxProcessLockInstrumentedTest {
    @Test
    fun legacyTextEntryDecodesAndImageOutboxPersistsOnlyOpaqueReferences() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val preferences = context.getSharedPreferences("cheby_outbox", 0)
        val legacy = """[{"clientMessageId":"legacy-message-001","sessionScope":"legacy-scope","threadId":"legacy-thread","text":"hello","state":"QUEUED","createdAtMillis":1,"retryNotBeforeMillis":0}]"""
        assertTrue(preferences.edit().putString("entries_v1", legacy).commit())
        val store = SharedPreferencesDurableOutboxStore(context)

        val restored = store.list().single()
        assertEquals("hello", restored.text)
        assertEquals(0, restored.imageAssetRefs.size)
        assertEquals(0, restored.sessionGeneration)

        assertTrue(store.clear())
        val prepared = OutboxEntry(
            clientMessageId = "client-image-message-001",
            sessionScope = "scope-image",
            threadKey = ThreadKey("thread-image"),
            text = "inspect",
            state = OutboxState.NEEDS_ATTACHMENTS,
            createdAtMillis = 2,
            sessionGeneration = 7,
            pendingImageCount = 1,
        )
        assertTrue(store.enqueue(prepared))
        val ready = prepared.copy(
            state = OutboxState.QUEUED,
            pendingImageCount = 0,
            imageAssetRefs = listOf("ast_abcdefghijklmnopqrstuvwxyz0123456789"),
        )
        assertTrue(store.replacePrepared(ready, "scope-image", 7, "scope-image", 7))

        val raw = requireNotNull(preferences.getString("entries_v1", null))
        assertTrue(raw.contains("ast_abcdefghijklmnopqrstuvwxyz0123456789"))
        assertTrue(!raw.contains("content://"))
        assertTrue(!raw.contains("/asset-staging/"))
        assertTrue(!raw.contains("sourceBytes"))
        assertTrue(store.clear())
    }

    @Test
    fun separateStoreInstancesCannotLoseConcurrentScopeWrites() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val first = SharedPreferencesDurableOutboxStore(context)
        val second = SharedPreferencesDurableOutboxStore(context)
        assertTrue(first.clearScope(TEST_SCOPE))
        val workers = 16
        val barrier = CyclicBarrier(workers)
        val executor = Executors.newFixedThreadPool(workers)
        try {
            val writes = (0 until workers).map { index ->
                executor.submit<Boolean> {
                    barrier.await(5, TimeUnit.SECONDS)
                    val store = if (index % 2 == 0) first else second
                    store.enqueue(
                        OutboxEntry(
                            clientMessageId = "process-lock-$index",
                            sessionScope = TEST_SCOPE,
                            threadKey = ThreadKey("thread-$index"),
                            text = "test-$index",
                            state = OutboxState.QUEUED,
                            createdAtMillis = index.toLong(),
                        ),
                    )
                }
            }
            assertTrue(writes.all { it.get(15, TimeUnit.SECONDS) })

            val persisted = first.list().filter { it.sessionScope == TEST_SCOPE }
            assertEquals(workers, persisted.size)
            assertEquals(
                (0 until workers).mapTo(mutableSetOf()) { "process-lock-$it" },
                persisted.mapTo(mutableSetOf(), OutboxEntry::clientMessageId),
            )
        } finally {
            executor.shutdownNow()
            assertTrue(first.clearScope(TEST_SCOPE))
        }
    }

    private companion object {
        const val TEST_SCOPE = "instrumentation-process-lock-scope-v1"
    }
}
