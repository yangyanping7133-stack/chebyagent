package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.model.MessageRole
import com.cheby.codex.mobile.model.RichMessage
import com.cheby.codex.mobile.model.RichMessageState

internal data class AssistantBubbleEvidence(
    val matchingCount: Int,
    val completedCount: Int,
) {
    val duplicateCount: Int = (matchingCount - 1).coerceAtLeast(0)
    val isExactCompleted: Boolean =
        matchingCount == 1 && completedCount == 1
}

internal fun assistantBubbleEvidence(
    messages: List<RichMessage>,
    turnId: String,
): AssistantBubbleEvidence {
    val matching = messages.filter {
        it.role == MessageRole.ASSISTANT && it.turnId == turnId
    }
    return AssistantBubbleEvidence(
        matchingCount = matching.size,
        completedCount = matching.count {
            it.state == RichMessageState.COMPLETED
        },
    )
}
