package com.cheby.codex.mobile.model

@JvmInline
value class ThreadKey(val value: String)

enum class ThreadRunState {
    IDLE,
    RUNNING,
    WAITING_USER,
    WAITING_APPROVAL,
    FAILED,
}

data class CodexThread(
    val key: ThreadKey,
    val title: String,
    val preview: String,
    val updatedAtMillis: Long,
    val state: ThreadRunState = ThreadRunState.IDLE,
    /** Last public Turn identity, used only to correlate a persisted per-Thread wait. */
    val lastTurnId: String? = null,
    /** App-server-owned settings for this Thread and its subsequent Turns. */
    val model: String? = null,
    val reasoningEffort: String? = null,
)

enum class GatewayConnectionState {
    ONLINE,
    CONNECTING,
    OFFLINE,
}

fun resolveInitialThread(
    stored: ThreadKey?,
    available: List<CodexThread>,
): ThreadKey? = available.firstOrNull { it.key == stored }?.key ?: available.firstOrNull()?.key
