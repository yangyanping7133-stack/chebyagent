package com.cheby.codex.mobile.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ThreadRestorationTest {
    private val first = CodexThread(ThreadKey("first"), "第一会话", "", 2)
    private val second = CodexThread(ThreadKey("second"), "第二会话", "", 1)

    @Test
    fun `restores stored thread when it still exists`() {
        assertEquals(second.key, resolveInitialThread(second.key, listOf(first, second)))
    }

    @Test
    fun `falls back to most recent thread when stored thread is gone`() {
        assertEquals(first.key, resolveInitialThread(ThreadKey("archived"), listOf(first, second)))
    }

    @Test
    fun `returns no selection for an empty thread list`() {
        assertNull(resolveInitialThread(ThreadKey("missing"), emptyList()))
    }
}
