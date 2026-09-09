package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.model.CodexThread
import com.cheby.codex.mobile.model.ConversationEvent
import com.cheby.codex.mobile.model.ConversationSnapshot
import com.cheby.codex.mobile.model.GatewayConnectionState
import com.cheby.codex.mobile.model.ThreadKey
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

data class GatewaySessionIdentity(
    val scope: String,
    val generation: Long,
    /**
     * Set only for an authenticated, same-binding transport migration. The ViewModel may move
     * durable Outbox rows from this exact predecessor generation to [generation]. Ordinary
     * delegate replacements leave this null and therefore retain the strict generation fence.
     */
    val outboxCarryoverFromGeneration: Long? = null,
)

data class LocalImageCapability(
    val uploadVersion: Int,
    val mediaTypes: Set<String>,
    val maxUploadBytes: Int,
    val maxPixels: Int,
    val maxEdgePixels: Int,
    val maxImagesPerTurn: Int,
)

data class ModelReasoningEffort(
    val value: String,
    val description: String,
)

data class AvailableModel(
    val id: String,
    val displayName: String,
    val description: String,
    val isDefault: Boolean,
    val defaultReasoningEffort: String,
    val supportedReasoningEfforts: List<ModelReasoningEffort>,
)

data class GatewayCapabilities(
    val localImage: LocalImageCapability? = null,
    val models: List<AvailableModel> = emptyList(),
) {
    companion object {
        val NONE = GatewayCapabilities()
    }
}

sealed interface TurnInput {
    data class Text(val value: String) : TurnInput
    data class Image(val assetRef: String) : TurnInput
}

data class UploadedImageAsset(
    val assetRef: String,
    val mediaType: String,
    val width: Int,
    val height: Int,
    val byteCount: Int,
)

data class OpenAiAccountState(
    val signedIn: Boolean,
    val planType: String? = null,
)

data class OpenAiDeviceLogin(
    /** Opaque cancellation handle. Never render or persist it. */
    val loginId: String,
    val verificationUrl: String,
    val userCode: String,
)

/**
 * The durable public identity returned by a successful turn submission.
 *
 * These are Gateway-owned identifiers. Android persists them with the outbox entry and uses the
 * exact thread/turn pair to release that thread's FIFO only after a matching terminal event.
 */
data class TurnSubmission(
    val turnId: String,
    val threadKey: ThreadKey,
    val clientMessageId: String,
    val status: String,
)

interface CodexGateway {
    val sessionScope: String
    /**
     * Changes whenever the backing container/delegate changes, even if its public scope string
     * or thread list happens to compare equal to the previous one.
     */
    val sessionIdentity: StateFlow<GatewaySessionIdentity>
        get() = MutableStateFlow(GatewaySessionIdentity(sessionScope, 0))
    val kind: StateFlow<GatewayKind>
    val connectionState: StateFlow<GatewayConnectionState>
    val threads: StateFlow<List<CodexThread>>

    suspend fun createThread(title: String): CodexThread
    suspend fun renameThread(threadKey: ThreadKey, title: String)
    suspend fun archiveThread(threadKey: ThreadKey)
    suspend fun deleteThread(threadKey: ThreadKey)

    suspend fun loadConversation(threadKey: ThreadKey): ConversationSnapshot
    fun observeConversation(
        threadKey: ThreadKey,
        afterStreamId: String,
        afterStreamSeq: Long,
    ): Flow<ConversationEvent>

    suspend fun loadCapabilities(
        expectedSessionScope: String = sessionScope,
        expectedSessionGeneration: Long = sessionIdentity.value.generation,
    ): GatewayCapabilities = GatewayCapabilities.NONE

    suspend fun uploadTurnImage(
        threadKey: ThreadKey,
        clientMessageId: String,
        clientAssetId: String,
        bytes: ByteArray,
        expectedSessionScope: String = sessionScope,
        expectedSessionGeneration: Long = sessionIdentity.value.generation,
    ): UploadedImageAsset = throw GatewayTransportException("Image input is unavailable")

    suspend fun sendTurnInput(
        threadKey: ThreadKey,
        input: List<TurnInput>,
        idempotencyKey: String,
        expectedSessionScope: String = sessionScope,
        expectedSessionGeneration: Long = sessionIdentity.value.generation,
    ): TurnSubmission {
        val text = input.singleOrNull() as? TurnInput.Text
            ?: throw GatewayTransportException("Rich input is unavailable")
        return sendTurn(threadKey, text.value, idempotencyKey, expectedSessionScope)
    }

    suspend fun sendTurn(
        threadKey: ThreadKey,
        text: String,
        idempotencyKey: String,
        expectedSessionScope: String = sessionScope,
    ): TurnSubmission

    /** Acknowledges the request only; the terminal event remains authoritative. */
    suspend fun interruptTurn(
        threadKey: ThreadKey,
        expectedTurnId: String?,
        expectedSessionScope: String = sessionScope,
        expectedSessionGeneration: Long = sessionIdentity.value.generation,
    ): Unit = throw GatewayTransportException("Stopping is unavailable")

    suspend fun performAction(
        actionRef: String,
        expectedSessionScope: String = sessionScope,
    )
}

internal interface CloseableCodexGateway {
    fun close()
}

internal interface LocalRuntimeGatewayControls {
    /** Reconnect only when it cannot interrupt an active or submitting turn. */
    fun applySavedConfiguration(): Boolean
    suspend fun changeConfigurationWhenIdle(change: suspend () -> String?): String?
}

internal interface LocalThreadModelControls {
    /** Updates only the selected Thread. App-server applies it on the next Turn and retains it. */
    fun selectThreadModel(threadKey: ThreadKey, model: String, reasoningEffort: String): Boolean
}

internal interface OpenAiAccountGatewayControls {
    suspend fun readOpenAiAccount(refreshToken: Boolean = false): OpenAiAccountState
    suspend fun startOpenAiDeviceLogin(): OpenAiDeviceLogin
    suspend fun cancelOpenAiDeviceLogin(loginId: String)
    suspend fun logoutOpenAiAccount()
}

enum class GatewayKind {
    LOCAL,
    RELAY,
    /** Superseded direct-host transport retained only for isolated legacy protocol tests. */
    REMOTE,
    DEMO,
    UNCONFIGURED,
}

interface DemoGatewayControls {
    fun setOnline(online: Boolean)
}
