package com.cheby.codex.mobile.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class RichMessageBoundsTest {
    @Test(timeout = 1_000)
    fun `rendered root list is deduplicated and capped`() {
        val blocks = (1..1_000).associate { index ->
            "block-$index" to TextBlock("block-$index", "value-$index")
        }
        val roots = (1..1_000).flatMap { index -> listOf("block-$index", "block-$index") }
        val message = RichMessage(
            id = "message",
            role = MessageRole.ASSISTANT,
            state = RichMessageState.COMPLETED,
            revision = 1,
            rootBlockIds = roots,
            blocks = blocks,
            createdAtMillis = 1,
        )

        val rendered = message.orderedBlocks()

        assertEquals(64, rendered.size)
        assertEquals("block-1", rendered.first().id)
        assertEquals("block-64", rendered.last().id)
        assertTrue(rendered.map(RichBlock::id).distinct().size == rendered.size)
    }
}
