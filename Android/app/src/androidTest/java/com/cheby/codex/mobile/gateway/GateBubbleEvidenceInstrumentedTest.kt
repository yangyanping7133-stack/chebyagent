package com.cheby.codex.mobile.gateway

import androidx.test.ext.junit.runners.AndroidJUnit4
import com.cheby.codex.mobile.model.MessageRole
import com.cheby.codex.mobile.model.RichMessage
import com.cheby.codex.mobile.model.RichMessageState
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class GateBubbleEvidenceInstrumentedTest {
    @Test
    fun completedAssistantPlusNonTerminalAssistantIsRejected() {
        val messages = listOf(
            assistant("completed", RichMessageState.COMPLETED),
            assistant("running", RichMessageState.STREAMING),
        )

        val evidence = assistantBubbleEvidence(messages, TURN_ID)

        assertEquals(2, evidence.matchingCount)
        assertEquals(1, evidence.completedCount)
        assertEquals(1, evidence.duplicateCount)
        assertFalse(evidence.isExactCompleted)
    }

    private fun assistant(id: String, state: RichMessageState) = RichMessage(
        id = id,
        role = MessageRole.ASSISTANT,
        state = state,
        revision = 1,
        rootBlockIds = emptyList(),
        blocks = emptyMap(),
        createdAtMillis = 1,
        turnId = TURN_ID,
    )

    private companion object {
        const val TURN_ID = "turn-gate-negative"
    }
}
