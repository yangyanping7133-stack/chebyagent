package com.cheby.codex.mobile.ui.rich

import com.cheby.codex.mobile.model.ActionItem
import com.cheby.codex.mobile.model.ActionsBlock
import org.junit.Assert.assertEquals
import org.junit.Test

class ActionBlockRateLimitTest {
    @Test
    fun `one limited approval action applies the same deadline to its sibling buttons`() {
        val block = ActionsBlock(
            id = "approval-actions",
            items = listOf(
                ActionItem("approve", "批准", "approve-ref"),
                ActionItem("reject", "拒绝", "reject-ref"),
            ),
        )

        val blockDeadline = actionBlockRetryNotBeforeMillis(
            block,
            mapOf("approve-ref" to 60_000L),
        )

        assertEquals(60_000L, blockDeadline)
        assertEquals(false, isActionBlockRateLimitElapsed(block, mapOf("approve-ref" to 60_000L), 59_999L))
        assertEquals(true, isActionBlockRateLimitElapsed(block, mapOf("approve-ref" to 60_000L), 60_000L))
    }

    @Test
    fun `persisted sibling deadline also controls the whole approval block`() {
        val block = ActionsBlock(
            id = "approval-actions",
            items = listOf(
                ActionItem("approve", "批准", "approve-ref"),
                ActionItem("reject", "拒绝", "reject-ref", retryNotBeforeMillis = 31_000L),
            ),
        )

        assertEquals(31_000L, actionBlockRetryNotBeforeMillis(block, emptyMap()))
    }
}
