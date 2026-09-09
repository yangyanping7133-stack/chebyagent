package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.model.ActionItem
import com.cheby.codex.mobile.model.ActionKind
import com.cheby.codex.mobile.model.ActionsBlock
import com.cheby.codex.mobile.model.BilingualCardBlock
import com.cheby.codex.mobile.model.BilingualLine
import com.cheby.codex.mobile.model.CodexThread
import com.cheby.codex.mobile.model.ComparisonBlock
import com.cheby.codex.mobile.model.ComparisonFact
import com.cheby.codex.mobile.model.ComparisonItem
import com.cheby.codex.mobile.model.ConversationEvent
import com.cheby.codex.mobile.model.FactItem
import com.cheby.codex.mobile.model.FactsBlock
import com.cheby.codex.mobile.model.DiffBlock
import com.cheby.codex.mobile.model.FileBlock
import com.cheby.codex.mobile.model.FileChangeKind
import com.cheby.codex.mobile.model.InteractiveCollectionBlock
import com.cheby.codex.mobile.model.InteractiveCollectionGroup
import com.cheby.codex.mobile.model.InteractiveCollectionItem
import com.cheby.codex.mobile.model.InteractiveImageKind
import com.cheby.codex.mobile.model.MessageRole
import com.cheby.codex.mobile.model.MediaBlock
import com.cheby.codex.mobile.model.isSafeMediaAssetId
import com.cheby.codex.mobile.model.MetricItem
import com.cheby.codex.mobile.model.MetricsBlock
import com.cheby.codex.mobile.model.NoticeBlock
import com.cheby.codex.mobile.model.PatchOperation
import com.cheby.codex.mobile.model.RelayBlock
import com.cheby.codex.mobile.model.RelayNode
import com.cheby.codex.mobile.model.ReferenceGalleryBlock
import com.cheby.codex.mobile.model.ReferenceImageItem
import com.cheby.codex.mobile.model.RichBlock
import com.cheby.codex.mobile.model.RichMessage
import com.cheby.codex.mobile.model.RichMessageState
import com.cheby.codex.mobile.model.SourceBlock
import com.cheby.codex.mobile.model.StatusBlock
import com.cheby.codex.mobile.model.StepItem
import com.cheby.codex.mobile.model.StepState
import com.cheby.codex.mobile.model.StepsBlock
import com.cheby.codex.mobile.model.TextBlock
import com.cheby.codex.mobile.model.TerminalBlock
import com.cheby.codex.mobile.model.TestBlock
import com.cheby.codex.mobile.model.ThreadKey
import com.cheby.codex.mobile.model.ThreadRunState
import com.cheby.codex.mobile.model.TurnLifecyclePhase
import com.cheby.codex.mobile.model.Tone
import com.cheby.codex.mobile.model.ToolBlock
import com.cheby.codex.mobile.model.UnknownBlock
import com.cheby.codex.mobile.model.SafeDisplayText
import java.time.Instant
import java.util.ArrayDeque
import java.util.UUID
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull

internal class ProtocolV1Exception(@Suppress("UNUSED_PARAMETER") detail: String) :
    GatewayTransportException("Gateway protocol mismatch")

internal data class DecodedEvent(
    val streamId: String,
    val seq: Long,
    val event: ConversationEvent,
    val approval: ApprovalMutation? = null,
)

internal data class DecodedThreadDetail(
    val streamId: String,
    val cursor: Long,
    val messages: List<RichMessage>,
    val approvals: List<ApprovalMutation.Requested>,
)

internal data class DecodedGatewayError(
    val code: String,
    val retryable: Boolean,
)

internal sealed interface ApprovalMutation {
    val approvalId: String

    data class Requested(
        override val approvalId: String,
        val threadId: String,
        val summary: String,
        val reason: String,
        val decisions: List<String>,
        val expiresAtMillis: Long,
        val actionToken: String?,
    ) : ApprovalMutation

    data class Terminal(
        override val approvalId: String,
        val state: String,
    ) : ApprovalMutation
}

internal class ContractV1Codec(
    private val json: Json = Json { ignoreUnknownKeys = false },
) {
    fun decodeThreadList(body: String): List<CodexThread> = protocolGuard {
        val root = body.objectRoot()
        val threads = root.requiredArray("data").boundedItems(MAX_THREADS, "threads")
        threads.map { decodeThread(it.requireJsonObject()) }
    }

    fun decodeThread(body: String): CodexThread = protocolGuard { decodeThread(body.objectRoot()) }

    fun decodeMessages(body: String): List<RichMessage> = protocolGuard {
        val root = body.objectRoot()
        root.requiredArray("messages")
            .boundedItems(MAX_MESSAGES, "messages")
            .map { decodeMessage(it.requireJsonObject()) }
    }

    fun decodeThreadDetail(body: String): DecodedThreadDetail = protocolGuard {
        val root = body.objectRoot()
        val cursor = root.requiredLong("cursor")
        if (cursor < 0) throw ProtocolV1Exception("Negative conversation cursor")
        DecodedThreadDetail(
            streamId = root.requiredString("streamId").requireOpaque("stream id", MAX_ID_BYTES),
            cursor = cursor,
            messages = root.requiredArray("messages")
                .boundedItems(MAX_MESSAGES, "messages")
                .map { decodeMessage(it.requireJsonObject()) },
            approvals = root.requiredArray("approvals")
                .boundedItems(MAX_APPROVALS, "approvals")
                .map { decodeApprovalRequested(it.requireJsonObject()) },
        )
    }

    fun decodePairing(
        body: String,
        baseUrl: String,
    ): com.cheby.codex.mobile.data.RemoteGatewaySession = protocolGuard {
        val root = body.objectRoot()
        com.cheby.codex.mobile.data.RemoteGatewaySession(
            baseUrl = baseUrl,
            accessToken = root.requiredString("accessToken").boundedUtf8(MAX_CREDENTIAL_BYTES),
            refreshToken = root.requiredString("refreshToken").boundedUtf8(MAX_CREDENTIAL_BYTES),
            deviceId = root.requiredString("deviceId").requireOpaque("device id", MAX_ID_BYTES),
            expiresAt = root.requiredString("expiresAt"),
            refreshExpiresAt = root.requiredString("refreshExpiresAt"),
            streamId = root.requiredString("streamId").requireOpaque("stream id", MAX_ID_BYTES),
            sessionScope = UUID.randomUUID().toString(),
        )
    }

    fun decodeRefresh(
        body: String,
        current: com.cheby.codex.mobile.data.RemoteGatewaySession,
    ): com.cheby.codex.mobile.data.RemoteGatewaySession = protocolGuard {
        val root = body.objectRoot()
        current.copy(
            deviceId = root.requiredString("deviceId").requireOpaque("device id", MAX_ID_BYTES),
            accessToken = root.requiredString("accessToken").boundedUtf8(MAX_CREDENTIAL_BYTES),
            refreshToken = root.requiredString("refreshToken").boundedUtf8(MAX_CREDENTIAL_BYTES),
            expiresAt = root.requiredString("expiresAt"),
            refreshExpiresAt = root.requiredString("refreshExpiresAt"),
            streamId = root.requiredString("streamId").requireOpaque("stream id", MAX_ID_BYTES),
        )
    }

    fun decodeGatewayError(body: String): DecodedGatewayError = protocolGuard {
        val error = body.objectRoot().requiredObject("error")
        DecodedGatewayError(
            code = error.requiredString("code").requireOpaque("error code", MAX_TYPE_LENGTH),
            retryable = error["retryable"]?.jsonPrimitive?.booleanOrNull ?: false,
        )
    }

    fun decodeCapabilities(body: String): GatewayCapabilities = protocolGuard {
        val root = body.objectRoot()
        if (root.requiredInt("v") != 1) throw ProtocolV1Exception("Unsupported capability version")
        val localImage = root.requiredObject("inputs")["localImage"] ?: return@protocolGuard GatewayCapabilities.NONE
        val raw = localImage.requireJsonObject()
        val mediaTypes = raw.requiredArray("mediaTypes").map { element ->
            element.jsonPrimitive.contentOrNull
                ?: throw ProtocolV1Exception("Invalid image media type")
        }
        val maxImagesPerTurn = raw.requiredInt("maxImagesPerTurn")
        if (
            raw.requiredInt("uploadVersion") != 1 ||
            mediaTypes != listOf("image/jpeg", "image/png") ||
            raw.requiredInt("maxUploadBytes") != LOCAL_IMAGE_MAX_UPLOAD_BYTES ||
            raw.requiredInt("maxPixels") != LOCAL_IMAGE_MAX_PIXELS ||
            raw.requiredInt("maxEdgePixels") != LOCAL_IMAGE_MAX_EDGE_PIXELS ||
            maxImagesPerTurn !in 1..LOCAL_IMAGE_MAX_IMAGES
        ) {
            throw ProtocolV1Exception("Unsupported local image capability")
        }
        GatewayCapabilities(
            localImage = LocalImageCapability(
                uploadVersion = 1,
                mediaTypes = mediaTypes.toSet(),
                maxUploadBytes = LOCAL_IMAGE_MAX_UPLOAD_BYTES,
                maxPixels = LOCAL_IMAGE_MAX_PIXELS,
                maxEdgePixels = LOCAL_IMAGE_MAX_EDGE_PIXELS,
                maxImagesPerTurn = maxImagesPerTurn,
            ),
        )
    }

    fun decodeImageUpload(body: String): UploadedImageAsset = protocolGuard {
        val root = body.objectRoot()
        val assetRef = root.requiredString("assetRef")
            .requireOpaque("asset reference", MAX_ASSET_REF_BYTES)
        if (!ASSET_REF.matches(assetRef)) throw ProtocolV1Exception("Invalid asset reference")
        val mediaType = root.requiredString("mediaType")
        if (mediaType !in LOCAL_IMAGE_MEDIA_TYPES) throw ProtocolV1Exception("Invalid image media type")
        val width = root.requiredInt("width")
        val height = root.requiredInt("height")
        val byteCount = root.requiredInt("byteCount")
        if (
            width !in 1..LOCAL_IMAGE_MAX_EDGE_PIXELS ||
            height !in 1..LOCAL_IMAGE_MAX_EDGE_PIXELS ||
            width.toLong() * height.toLong() > LOCAL_IMAGE_MAX_PIXELS ||
            byteCount !in 1..LOCAL_IMAGE_MAX_UPLOAD_BYTES
        ) {
            throw ProtocolV1Exception("Invalid uploaded image bounds")
        }
        root.requiredString("expiresAt").requireTimestamp()
        UploadedImageAsset(assetRef, mediaType, width, height, byteCount)
    }

    fun decodeTurnSubmission(body: String): TurnSubmission = protocolGuard {
        val root = body.objectRoot()
        root.requiredString("createdAt").requireTimestamp()
        val status = root.requiredString("status").also {
            if (it !in TURN_STATUSES) throw ProtocolV1Exception("Unknown turn status")
        }
        TurnSubmission(
            turnId = root.requiredString("id").requireOpaque("turn id", MAX_ID_BYTES),
            threadKey = ThreadKey(
                root.requiredString("threadId").requireOpaque("thread id", MAX_ID_BYTES),
            ),
            clientMessageId = root.requiredString("clientMessageId")
                .requireOpaque("client message id", MAX_ID_BYTES),
            status = status,
        )
    }

    fun decodeEvent(body: String, selectedThread: ThreadKey): DecodedEvent = protocolGuard {
        val root = body.objectRoot()
        if (root.requiredInt("v") != 1) throw ProtocolV1Exception("Unsupported protocol version")
        val eventId = root.requiredString("eventId").requireOpaque("event id", MAX_ID_BYTES)
        val streamId = root.requiredString("streamId").requireOpaque("stream id", MAX_ID_BYTES)
        val seq = root.requiredLong("seq").also {
            if (it < 0) throw ProtocolV1Exception("Negative sequence")
        }
        val occurredAt = root.requiredString("occurredAt").requireTimestamp().toEpochMilli()
        val type = root.requiredString("type").requireOpaque("event type", MAX_TYPE_LENGTH)
        if (type !in KNOWN_EVENT_TYPES) throw ProtocolV1Exception("Unknown event type")
        val threadId = root["threadId"]?.jsonPrimitive?.contentOrNull
            ?.requireOpaque("thread id", MAX_ID_BYTES)
        val turnId = root["turnId"]?.jsonPrimitive?.contentOrNull
            ?.requireOpaque("turn id", MAX_ID_BYTES)
        val payload = root.requiredObject("payload")
        var approval: ApprovalMutation? = null
        val event = when {
            type == "sync.required" -> ConversationEvent.SyncRequired(eventId, streamId, seq)
            type in TURN_LIFECYCLE_TYPES -> {
                val lifecycleThread = threadId
                    ?: throw ProtocolV1Exception("Turn lifecycle requires thread id")
                val lifecycleTurn = turnId
                    ?: throw ProtocolV1Exception("Turn lifecycle requires turn id")
                ConversationEvent.TurnLifecycle(
                    eventId = eventId,
                    streamId = streamId,
                    streamSeq = seq,
                    threadKey = ThreadKey(lifecycleThread),
                    turnId = lifecycleTurn,
                    phase = when (type) {
                        "turn.started" -> TurnLifecyclePhase.STARTED
                        "turn.completed" -> TurnLifecyclePhase.COMPLETED
                        "turn.failed" -> TurnLifecyclePhase.FAILED
                        else -> TurnLifecyclePhase.INTERRUPTED
                    },
                )
            }
            threadId != selectedThread.value -> ConversationEvent.NoOp(eventId, streamId, seq)
            type == "message.snapshot" -> {
                val message = decodeMessage(payload)
                ConversationEvent.MessageAdded(eventId, streamId, seq, message)
            }
            type == "message.patch" -> decodePatch(eventId, streamId, seq, payload)
            type == "asset.unavailable" -> decodeAssetUnavailable(
                eventId = eventId,
                streamId = streamId,
                seq = seq,
                occurredAtMillis = occurredAt,
                payload = payload,
            )
            type == "approval.requested" -> {
                approval = decodeApprovalRequested(payload)
                ConversationEvent.NoOp(eventId, streamId, seq)
            }
            type == "approval.resolved" -> {
                approval = ApprovalMutation.Terminal(
                    approvalId = payload.requiredString("approvalId").requireOpaque("approval id", MAX_ID_BYTES),
                    state = payload.requiredString("state").also {
                        if (it !in TERMINAL_APPROVAL_STATES) {
                            throw ProtocolV1Exception("Unknown approval state")
                        }
                    },
                )
                ConversationEvent.NoOp(eventId, streamId, seq)
            }
            type == "approval.expired" -> {
                approval = ApprovalMutation.Terminal(
                    payload.requiredString("approvalId").requireOpaque("approval id", MAX_ID_BYTES),
                    "expired",
                )
                ConversationEvent.NoOp(eventId, streamId, seq)
            }
            else -> ConversationEvent.NoOp(eventId, streamId, seq)
        }
        DecodedEvent(streamId, seq, event, approval)
    }

    private fun decodeAssetUnavailable(
        eventId: String,
        streamId: String,
        seq: Long,
        occurredAtMillis: Long,
        payload: JsonObject,
    ): ConversationEvent.MessageAdded {
        val body = when (payload.keys) {
            FROZEN_MEDIA_UNAVAILABLE_FIELDS -> {
                val assetId = payload.requiredString("assetId").requireOpaque("asset id", MAX_ID_BYTES)
                if (!isSafeMediaAssetId(assetId)) throw ProtocolV1Exception("Invalid media asset id")
                safeEvidence(payload.requiredString("fallbackText"), SafeDisplayText.MAX_FALLBACK)
            }
            LOCAL_IMAGE_UNAVAILABLE_FIELDS -> {
                payload.requiredString("clientMessageId")
                    .requireOpaque("client message id", MAX_ID_BYTES)
                val reason = payload.requiredString("reason")
                if (reason !in LOCAL_IMAGE_UNAVAILABLE_REASONS) {
                    throw ProtocolV1Exception("Unknown asset unavailable reason")
                }
                LOCAL_IMAGE_UNAVAILABLE_FALLBACK
            }
            else -> throw ProtocolV1Exception("Unknown asset unavailable payload")
        }
        val notice = NoticeBlock(
            id = ASSET_UNAVAILABLE_BLOCK_ID,
            title = "媒体内容不可用",
            body = body,
            tone = Tone.WARNING,
            fallbackText = body,
        )
        val localMessageId = "local-asset-unavailable-" + UUID.nameUUIDFromBytes(
            "asset.unavailable:$eventId".toByteArray(Charsets.UTF_8),
        )
        return ConversationEvent.MessageAdded(
            eventId = eventId,
            streamId = streamId,
            streamSeq = seq,
            message = RichMessage(
                id = localMessageId,
                role = MessageRole.SYSTEM,
                state = RichMessageState.COMPLETED,
                revision = 0,
                rootBlockIds = listOf(notice.id),
                blocks = mapOf(notice.id to notice),
                createdAtMillis = occurredAtMillis,
            ),
        )
    }

    private inline fun <T> protocolGuard(block: () -> T): T = try {
        block()
    } catch (error: ProtocolV1Exception) {
        throw error
    } catch (_: Exception) {
        throw ProtocolV1Exception("Malformed protocol value")
    }

    private fun decodeApprovalRequested(payload: JsonObject): ApprovalMutation.Requested {
        val decisions = payload.requiredArray("decisions")
            .boundedItems(MAX_APPROVAL_DECISIONS, "approval decisions")
            .map { it.jsonPrimitive.content.requireOpaque("approval decision", MAX_ID_BYTES) }
        if (decisions.isEmpty() || decisions.any { it !in APPROVAL_DECISIONS }) {
            throw ProtocolV1Exception("Unknown approval decision")
        }
        val state = payload.requiredString("state")
        if (state != "pending") throw ProtocolV1Exception("Approval request is not pending")
        return ApprovalMutation.Requested(
            approvalId = payload.requiredString("approvalId").requireOpaque("approval id", MAX_ID_BYTES),
            threadId = payload.requiredString("threadId").requireOpaque("thread id", MAX_ID_BYTES),
            summary = safeEvidence(payload.requiredString("summary"), SafeDisplayText.MAX_LABEL),
            reason = safeEvidence(payload.requiredString("reason"), SafeDisplayText.MAX_DETAIL),
            decisions = decisions.distinct(),
            expiresAtMillis = payload.requiredString("expiresAt").requireTimestamp().toEpochMilli(),
            actionToken = payload["actionToken"]?.jsonPrimitive?.contentOrNull
                ?.takeIf(String::isNotBlank)
                ?.boundedUtf8(MAX_CREDENTIAL_BYTES),
        )
    }

    private fun decodePatch(
        eventId: String,
        streamId: String,
        seq: Long,
        payload: JsonObject,
    ): ConversationEvent.MessagePatched {
        val rawOps = payload.requiredArray("ops")
        if (rawOps.size > MAX_PATCH_OPS) throw ProtocolV1Exception("Too many patch operations")
        val baseRevision = payload.requiredLong("baseRevision").requireRevision()
        val nextRevision = payload.requiredLong("nextRevision").requireRevision()
        if (baseRevision == Long.MAX_VALUE || nextRevision != baseRevision + 1) {
            throw ProtocolV1Exception("Invalid patch revision")
        }
        return ConversationEvent.MessagePatched(
            eventId = eventId,
            streamId = streamId,
            streamSeq = seq,
            messageId = payload.requiredString("messageId").requireOpaque("message id", MAX_ID_BYTES),
            baseRevision = baseRevision,
            revision = nextRevision,
            operations = rawOps.map { decodePatchOperation(it.requireJsonObject()) },
        )
    }

    private fun decodePatchOperation(raw: JsonObject): PatchOperation {
        val op = raw.requiredString("op")
        val blockId = raw["blockId"]?.jsonPrimitive?.contentOrNull
            ?.requireOpaque("block id", MAX_ID_BYTES)
        return when (op) {
            "block.put" -> PatchOperation.PutBlock(
                decodeBlock(
                    blockId ?: throw ProtocolV1Exception("block.put requires blockId"),
                    raw.requiredObject("value"),
                ),
            )
            "block.merge" -> {
                val value = raw.requiredObject("value")
                validateJsonStructure(
                    root = value,
                    maxNodes = MAX_PATCH_VALUE_NODES,
                    maxDepth = MAX_PATCH_VALUE_DEPTH,
                    maxArrayItems = MAX_BLOCK_ITEMS,
                )
                PatchOperation.MergeBlock(
                    blockId ?: throw ProtocolV1Exception("block.merge requires blockId"),
                    value.mapValues { (_, item) -> item.toPatchValue() },
                )
            }
            "block.text.append" -> PatchOperation.AppendBlockText(
                blockId ?: throw ProtocolV1Exception("text append requires blockId"),
                raw.requiredString("value").boundedText(),
            )
            "block.terminal.append" -> PatchOperation.AppendTerminal(
                blockId ?: throw ProtocolV1Exception("terminal append requires blockId"),
                raw.requiredString("value").boundedText(),
            )
            "block.remove" -> PatchOperation.RemoveBlock(
                blockId ?: throw ProtocolV1Exception("block.remove requires blockId"),
            )
            "root.set" -> {
                val roots = raw.requiredArray("value")
                    .boundedItems(MAX_ROOT_BLOCKS, "root blocks")
                    .map { it.jsonPrimitive.content.requireOpaque("root block id", MAX_ID_BYTES) }
                if (roots.distinct().size != roots.size) throw ProtocolV1Exception("Duplicate root blocks")
                PatchOperation.SetRootBlocks(roots)
            }
            "message.state.set" -> PatchOperation.SetMessageState(
                decodeMessageState(raw.requiredString("value")),
            )
            "action.resolve" -> {
                val value = raw["value"] as? JsonObject
                val actionId = raw["actionId"]?.jsonPrimitive?.contentOrNull
                    ?: value?.get("actionId")?.jsonPrimitive?.contentOrNull
                    ?: throw ProtocolV1Exception("action.resolve requires actionId")
                PatchOperation.ResolveAction(blockId, actionId.requireOpaque("action id", MAX_ID_BYTES))
            }
            else -> throw ProtocolV1Exception("Unknown patch operation")
        }
    }

    private fun decodeThread(raw: JsonObject): CodexThread {
        raw.requiredString("createdAt").requireTimestamp()
        val updatedAt = raw.requiredString("updatedAt").requireTimestamp()
        return CodexThread(
            key = ThreadKey(raw.requiredString("id").requireOpaque("thread id", MAX_ID_BYTES)),
            title = safeEvidence(raw.requiredString("title"), SafeDisplayText.MAX_LABEL),
            preview = safeEvidence(raw.requiredPossiblyEmptyString("preview"), SafeDisplayText.MAX_DETAIL),
            updatedAtMillis = updatedAt.toEpochMilli(),
            state = when (raw.requiredString("status")) {
                "idle", "archived" -> ThreadRunState.IDLE
                "running" -> ThreadRunState.RUNNING
                "waitingUser" -> ThreadRunState.WAITING_USER
                "waitingApproval" -> ThreadRunState.WAITING_APPROVAL
                "failed" -> ThreadRunState.FAILED
                else -> throw ProtocolV1Exception("Unknown thread status")
            },
            lastTurnId = raw["lastTurnId"]?.jsonPrimitive?.contentOrNull
                ?.requireOpaque("turn id", MAX_ID_BYTES),
        )
    }

    private fun decodeMessage(raw: JsonObject): RichMessage {
        val normalizedMessage = raw.toString()
        if (SafeDisplayText.exceedsUtf8Limit(normalizedMessage, MAX_MESSAGE_JSON_BYTES)) {
            throw ProtocolV1Exception("Message exceeds limit")
        }
        validateJsonStructure(raw, MAX_MESSAGE_JSON_NODES)
        val messageId = raw.requiredString("messageId").requireOpaque("message id", MAX_ID_BYTES)
        val role = when (raw.requiredString("role")) {
            "user" -> MessageRole.USER
            "assistant" -> MessageRole.ASSISTANT
            else -> throw ProtocolV1Exception("Unsupported role")
        }
        val fallback = safeEvidence(
            raw.requiredObject("fallback").requiredString("text").boundedUtf8(MAX_TEXT_LENGTH),
            MAX_TEXT_LENGTH,
        )
        val createdAt = raw.requiredString("createdAt").requireTimestamp().toEpochMilli()
        raw.requiredString("updatedAt").requireTimestamp()
        val schema = raw.requiredString("schema").requireOpaque("message schema", MAX_TYPE_LENGTH)
        if (schema != RICH_SCHEMA) {
            val block = TextBlock("fallback", fallback, markdown = false)
            return RichMessage(
                id = messageId,
                role = role,
                state = decodeMessageState(raw.requiredString("state")),
                revision = raw.requiredLong("revision").requireRevision(),
                rootBlockIds = listOf(block.id),
                blocks = mapOf(block.id to block),
                createdAtMillis = createdAt,
                clientMessageId = raw["clientMessageId"]?.jsonPrimitive?.contentOrNull
                    ?.requireOpaque("client message id", MAX_ID_BYTES),
                turnId = raw.requiredString("turnId").requireOpaque("turn id", MAX_ID_BYTES),
            )
        }
        val blocksObject = raw.requiredObject("blocks")
        if (blocksObject.size > MAX_BLOCKS) throw ProtocolV1Exception("Too many blocks")
        val blocks = blocksObject.mapValues { (id, value) ->
            val safeId = id.requireOpaque("block id", MAX_ID_BYTES)
            val blockObject = value.requireJsonObject()
            blockObject.optionalString("blockId")?.let { wireId ->
                if (wireId.requireOpaque("block id", MAX_ID_BYTES) != safeId) {
                    throw ProtocolV1Exception("Block id mismatch")
                }
            }
            decodeBlock(safeId, blockObject, fallback)
        }
        val roots = raw.requiredArray("rootBlockIds")
            .boundedItems(MAX_ROOT_BLOCKS, "root blocks")
            .map { it.jsonPrimitive.content.requireOpaque("root block id", MAX_ID_BYTES) }
        if (roots.distinct().size != roots.size) throw ProtocolV1Exception("Duplicate root blocks")
        if (roots.any { it !in blocks }) throw ProtocolV1Exception("Unknown root block")
        return RichMessage(
            id = messageId,
            role = role,
            state = decodeMessageState(raw.requiredString("state")),
            revision = raw.requiredLong("revision").requireRevision(),
            rootBlockIds = roots,
            blocks = blocks,
            createdAtMillis = createdAt,
            clientMessageId = raw["clientMessageId"]?.jsonPrimitive?.contentOrNull
                ?.requireOpaque("client message id", MAX_ID_BYTES),
            turnId = raw.requiredString("turnId").requireOpaque("turn id", MAX_ID_BYTES),
        )
    }

    private fun decodeBlock(id: String, raw: JsonObject, messageFallback: String = SAFE_FALLBACK): RichBlock {
        val type = raw.requiredString("type").requireOpaque("block type", MAX_TYPE_LENGTH)
        val fallback = raw["fallbackText"]?.jsonPrimitive?.contentOrNull
            ?.takeIf(String::isNotBlank)
            ?.boundedText()
            ?: messageFallback
        return when (type) {
            "text" -> TextBlock(
                id,
                safeEvidence(raw.requiredString("text").boundedUtf8(MAX_TEXT_LENGTH), MAX_TEXT_LENGTH),
                raw["markdown"]?.jsonPrimitive?.booleanOrNull ?: true,
                safeEvidence(fallback, MAX_TEXT_LENGTH),
            )
            "status" -> StatusBlock(
                id,
                safeEvidence(raw.requiredString("label"), SafeDisplayText.MAX_LABEL),
                raw.optionalString("detail")?.let { safeEvidence(it, SafeDisplayText.MAX_DETAIL) },
                decodeTone(raw["tone"]?.jsonPrimitive?.contentOrNull ?: "info"),
                raw["progress"]?.jsonPrimitive?.doubleOrNull?.toFloat()?.also {
                    if (!it.isFinite() || it !in 0f..1f) throw ProtocolV1Exception("Invalid progress")
                },
                safeEvidence(fallback, SafeDisplayText.MAX_FALLBACK),
            )
            "steps" -> StepsBlock(
                id,
                safeEvidence(raw.optionalString("title") ?: "执行计划", SafeDisplayText.MAX_LABEL),
                raw.requiredArray("items").boundedItems(MAX_BLOCK_ITEMS, "steps").map { item ->
                    val value = item.requireJsonObject()
                    StepItem(
                        value.requiredString("id").requireOpaque("step id", MAX_ID_BYTES),
                        safeEvidence(value.requiredString("label"), SafeDisplayText.MAX_LABEL),
                        decodeStepState(value.requiredString("state")),
                        value.optionalString("detail")?.let { safeEvidence(it, SafeDisplayText.MAX_DETAIL) },
                    )
                },
                safeEvidence(fallback, SafeDisplayText.MAX_FALLBACK),
            )
            "metrics" -> MetricsBlock(
                id,
                raw.requiredArray("items").boundedItems(MAX_BLOCK_ITEMS, "metrics").map { item ->
                    val value = item.requireJsonObject()
                    MetricItem(
                        safeEvidence(value.requiredString("label"), MAX_SHORT_VALUE_BYTES),
                        safeEvidence(value.requiredString("value"), SafeDisplayText.MAX_LABEL),
                        value.optionalString("unit")?.let { safeEvidence(it, MAX_UNIT_BYTES) },
                    )
                },
                safeEvidence(fallback, SafeDisplayText.MAX_FALLBACK),
            )
            "facts" -> FactsBlock(
                id,
                raw.requiredArray("items").boundedItems(MAX_BLOCK_ITEMS, "facts").map { item ->
                    val value = item.requireJsonObject()
                    FactItem(
                        safeEvidence(value.requiredString("label"), MAX_SHORT_VALUE_BYTES),
                        safeEvidence(value.requiredString("value"), SafeDisplayText.MAX_DETAIL),
                        value.optionalString("note")?.let { safeEvidence(it, SafeDisplayText.MAX_DETAIL) },
                    )
                },
                safeEvidence(fallback, SafeDisplayText.MAX_FALLBACK),
            )
            "media" -> {
                val assetId = raw.requiredString("assetId").requireOpaque("asset id", MAX_ID_BYTES)
                if (!isSafeMediaAssetId(assetId)) throw ProtocolV1Exception("Invalid media asset id")
                MediaBlock(
                    id = id,
                    assetId = assetId,
                    caption = raw.optionalString("caption")?.let {
                        safeEvidence(it, SafeDisplayText.MAX_LABEL)
                    },
                    fallbackText = safeEvidence(fallback, SafeDisplayText.MAX_FALLBACK),
                )
            }
            "source" -> decodeSource(id, raw, fallback)
            "relay" -> RelayBlock(
                id,
                safeEvidence(raw.optionalString("title") ?: "执行接力", SafeDisplayText.MAX_LABEL),
                raw.requiredArray("nodes").boundedItems(MAX_BLOCK_ITEMS, "relay nodes").map { item ->
                    val value = item.requireJsonObject()
                    RelayNode(
                        value.requiredString("id").requireOpaque("relay id", MAX_ID_BYTES),
                        safeEvidence(value.requiredString("label"), SafeDisplayText.MAX_LABEL),
                        value.optionalString("detail")?.let { safeEvidence(it, SafeDisplayText.MAX_DETAIL) },
                        decodeStepState(value.requiredString("state")),
                    )
                },
                safeEvidence(fallback, SafeDisplayText.MAX_FALLBACK),
            )
            "notice" -> NoticeBlock(
                id,
                safeEvidence(raw.requiredString("title"), SafeDisplayText.MAX_LABEL),
                safeEvidence(
                    raw.optionalString("body") ?: raw.optionalString("text") ?: fallback,
                    SafeDisplayText.MAX_DETAIL,
                ),
                decodeTone(raw["tone"]?.jsonPrimitive?.contentOrNull ?: "neutral"),
                safeEvidence(fallback, SafeDisplayText.MAX_FALLBACK),
            )
            "actions" -> ActionsBlock(
                id,
                (raw["items"] as? JsonArray ?: raw["actions"] as? JsonArray ?: JsonArray(emptyList()))
                    .boundedItems(MAX_ACTION_ITEMS, "actions").map { item ->
                    val value = item.requireJsonObject()
                    val actionId = value.requiredString("id").requireOpaque("action id", MAX_ID_BYTES)
                    ActionItem(
                        id = actionId,
                        label = safeEvidence(value.requiredString("label"), MAX_SHORT_VALUE_BYTES),
                        actionRef = requireSafeActionRef(value.optionalString("actionRef") ?: actionId),
                        kind = decodeActionKind(value["kind"]?.jsonPrimitive?.contentOrNull),
                        enabled = value["enabled"]?.jsonPrimitive?.booleanOrNull ?: true,
                    )
                },
                safeEvidence(fallback, SafeDisplayText.MAX_FALLBACK),
            )
            "terminal" -> decodeTerminal(id, raw, fallback)
            "file" -> decodeFile(id, raw, fallback)
            "diff" -> decodeDiff(id, raw, fallback)
            "tool" -> decodeTool(id, raw, fallback)
            "test" -> decodeTest(id, raw, fallback)
            "interactiveCollection", "interactive_collection" -> decodeInteractiveCollection(id, raw, fallback)
            "referenceGallery", "reference_gallery" -> decodeReferenceGallery(id, raw, fallback)
            "comparison" -> decodeComparison(id, raw, fallback)
            "bilingualCard", "bilingual_card" -> decodeBilingualCard(id, raw, fallback)
            else -> UnknownBlock(
                id = id,
                originalType = type.take(MAX_TYPE_LENGTH),
                fallbackText = safeEvidence(fallback, SafeDisplayText.MAX_FALLBACK),
            )
        }
    }

    private fun decodeTerminal(id: String, raw: JsonObject, fallback: String): TerminalBlock {
        val outputRaw = raw.optionalString("text") ?: raw.optionalString("output") ?: ""
        val output = safeEvidence(outputRaw, SafeDisplayText.MAX_TERMINAL)
        return TerminalBlock(
            id = id,
            title = safeEvidence(raw.optionalString("title") ?: "终端输出", SafeDisplayText.MAX_LABEL),
            commandLabel = raw.optionalString("commandLabel")?.let(::safeSemanticCommandLabel),
            output = output,
            state = decodeEvidenceState(raw.optionalString("state"), StepState.COMPLETED),
            truncated = (raw["truncated"]?.jsonPrimitive?.booleanOrNull ?: false) ||
                outputRaw.exceedsUtf8Limit(SafeDisplayText.MAX_TERMINAL),
            fallbackText = safeEvidence(fallback, SafeDisplayText.MAX_FALLBACK),
        )
    }

    private fun decodeFile(id: String, raw: JsonObject, fallback: String): FileBlock = FileBlock(
        id = id,
        label = safeFileLabel(raw, "文件"),
        change = decodeFileChange(
            raw.optionalString("change") ?: raw.optionalString("operation") ?: raw.optionalString("kind"),
        ),
        detail = raw.optionalString("detail")?.let { safeEvidence(it, SafeDisplayText.MAX_DETAIL) },
        language = raw.optionalString("language")?.let { safeEvidence(it, 64) },
        state = decodeEvidenceState(raw.optionalString("state"), StepState.COMPLETED),
        fallbackText = safeEvidence(fallback, SafeDisplayText.MAX_FALLBACK),
    )

    private fun decodeDiff(id: String, raw: JsonObject, fallback: String): DiffBlock {
        val previewRaw = raw.optionalString("preview") ?: raw.optionalString("text") ?: raw.optionalString("diff")
        val preview = previewRaw?.let { safeEvidence(it, SafeDisplayText.MAX_DIFF) }
        return DiffBlock(
            id = id,
            fileLabel = safeFileLabel(raw, "变更内容"),
            summary = safeEvidence(raw.optionalString("summary") ?: fallback, SafeDisplayText.MAX_DETAIL),
            preview = preview,
            additions = raw.optionalNonNegativeInt("additions"),
            deletions = raw.optionalNonNegativeInt("deletions"),
            truncated = (raw["truncated"]?.jsonPrimitive?.booleanOrNull ?: false) ||
                (previewRaw?.exceedsUtf8Limit(SafeDisplayText.MAX_DIFF) ?: false),
            fallbackText = safeEvidence(fallback, SafeDisplayText.MAX_FALLBACK),
        )
    }

    private fun decodeTool(id: String, raw: JsonObject, fallback: String): ToolBlock = ToolBlock(
        id = id,
        label = safeEvidence(
            raw.optionalString("label") ?: raw.optionalString("title") ?: raw.optionalString("toolName") ?: "工具",
            SafeDisplayText.MAX_LABEL,
        ),
        detail = (raw.optionalString("detail") ?: raw.optionalString("summary") ?: raw.optionalString("result"))
            ?.let { safeEvidence(it, SafeDisplayText.MAX_DETAIL) },
        state = decodeEvidenceState(raw.optionalString("state"), StepState.COMPLETED),
        durationLabel = raw.optionalString("durationLabel")?.let { safeEvidence(it, 64) },
        fallbackText = safeEvidence(fallback, SafeDisplayText.MAX_FALLBACK),
    )

    private fun decodeTest(id: String, raw: JsonObject, fallback: String): TestBlock = TestBlock(
        id = id,
        title = safeEvidence(raw.optionalString("title") ?: "测试", SafeDisplayText.MAX_LABEL),
        summary = safeEvidence(raw.optionalString("summary") ?: fallback, SafeDisplayText.MAX_DETAIL),
        passed = raw.optionalNonNegativeInt("passed"),
        failed = raw.optionalNonNegativeInt("failed"),
        skipped = raw.optionalNonNegativeInt("skipped"),
        durationLabel = raw.optionalString("durationLabel")?.let { safeEvidence(it, 64) },
        state = decodeEvidenceState(raw.optionalString("state"), StepState.COMPLETED),
        fallbackText = safeEvidence(fallback, SafeDisplayText.MAX_FALLBACK),
    )

    private fun decodeInteractiveCollection(
        id: String,
        raw: JsonObject,
        fallback: String,
    ): InteractiveCollectionBlock {
        val minorUnitScale = raw.requiredInt("minorUnitScale")
        if (minorUnitScale !in INTERACTIVE_MINOR_UNIT_SCALES) {
            throw ProtocolV1Exception("Unsupported minor unit scale")
        }
        var totalItems = 0
        val groups = raw.requiredArray("groups")
            .boundedItems(MAX_INTERACTIVE_GROUPS, "interactive groups")
            .also { if (it.isEmpty()) throw ProtocolV1Exception("Empty interactive collection") }
            .map { groupElement ->
                val group = groupElement.requireJsonObject()
                val groupId = group.requiredString("id").requireOpaque("group id", MAX_ID_BYTES)
                val items = group.requiredArray("items")
                    .boundedItems(MAX_BLOCK_ITEMS, "interactive items")
                    .also { if (it.isEmpty()) throw ProtocolV1Exception("Empty interactive group") }
                    .map { itemElement ->
                        totalItems += 1
                        if (totalItems > MAX_INTERACTIVE_ITEMS) {
                            throw ProtocolV1Exception("Too many interactive items")
                        }
                        val item = itemElement.requireJsonObject()
                        val price = item.requiredLong("unitPriceMinor")
                        if (price !in -1L..MAX_INTERACTIVE_PRICE_MINOR) {
                            throw ProtocolV1Exception("Invalid interactive item price")
                        }
                        InteractiveCollectionItem(
                            id = item.requiredString("id").requireOpaque("item id", MAX_ID_BYTES),
                            primary = safeEvidence(item.requiredString("primary"), SafeDisplayText.MAX_LABEL),
                            secondary = safeEvidence(item.requiredString("secondary"), SafeDisplayText.MAX_LABEL),
                            detail = safeEvidence(
                                item.requiredPossiblyEmptyString("detail"),
                                SafeDisplayText.MAX_DETAIL,
                            ),
                            unitPriceMinor = price,
                            priceLabel = safeEvidence(
                                item.requiredString("priceLabel"),
                                MAX_SHORT_VALUE_BYTES,
                            ),
                            imageQuery = safeEvidence(
                                item.requiredPossiblyEmptyString("imageQuery"),
                                SafeDisplayText.MAX_DETAIL,
                            ),
                            imageLabel = safeEvidence(
                                item.requiredPossiblyEmptyString("imageLabel"),
                                SafeDisplayText.MAX_LABEL,
                            ),
                            imageKind = when (item.requiredString("imageKind")) {
                                "source" -> InteractiveImageKind.SOURCE
                                "provider" -> InteractiveImageKind.PROVIDER
                                "representative" -> InteractiveImageKind.REPRESENTATIVE
                                "none" -> InteractiveImageKind.NONE
                                else -> throw ProtocolV1Exception("Unknown interactive image kind")
                            },
                            uncertain = item["uncertain"]?.jsonPrimitive?.booleanOrNull
                                ?: throw ProtocolV1Exception("Missing uncertain flag"),
                        )
                    }
                InteractiveCollectionGroup(
                    id = groupId,
                    title = safeEvidence(group.requiredString("title"), SafeDisplayText.MAX_LABEL),
                    items = items,
                )
            }
        if (groups.map(InteractiveCollectionGroup::id).distinct().size != groups.size) {
            throw ProtocolV1Exception("Duplicate interactive group id")
        }
        val allItems = groups.flatMap(InteractiveCollectionGroup::items)
        if (allItems.map(InteractiveCollectionItem::id).distinct().size != allItems.size) {
            throw ProtocolV1Exception("Duplicate interactive item id")
        }
        return InteractiveCollectionBlock(
            id = id,
            title = safeEvidence(raw.requiredString("title"), SafeDisplayText.MAX_LABEL),
            subtitle = safeEvidence(raw.requiredString("subtitle"), SafeDisplayText.MAX_DETAIL),
            confirmLabel = safeEvidence(raw.requiredString("confirmLabel"), MAX_SHORT_VALUE_BYTES),
            quantityUnit = safeEvidence(
                raw.optionalString("quantityUnit") ?: "项",
                MAX_UNIT_BYTES,
            ),
            presentationTitle = safeEvidence(
                raw.requiredString("presentationTitle"),
                SafeDisplayText.MAX_LABEL,
            ),
            presentationSubtitle = safeEvidence(
                raw.optionalString("presentationSubtitle") ?: "",
                SafeDisplayText.MAX_DETAIL,
            ),
            presentationTotalLabel = safeEvidence(
                raw.optionalString("presentationTotalLabel") ?: "合计",
                MAX_SHORT_VALUE_BYTES,
            ),
            presentationNote = safeEvidence(
                raw.requiredPossiblyEmptyString("presentationNote"),
                SafeDisplayText.MAX_DETAIL,
            ),
            currencyLabel = safeEvidence(raw.requiredString("currencyLabel"), MAX_UNIT_BYTES),
            minorUnitScale = minorUnitScale,
            groups = groups,
            fallbackText = safeEvidence(fallback, SafeDisplayText.MAX_FALLBACK),
        )
    }

    private fun decodeReferenceGallery(
        id: String,
        raw: JsonObject,
        fallback: String,
    ): ReferenceGalleryBlock {
        val items = raw.requiredArray("items")
            .boundedItems(MAX_REFERENCE_GALLERY_ITEMS, "reference gallery items")
            .also { if (it.isEmpty()) throw ProtocolV1Exception("Empty reference gallery") }
            .map { element ->
                val item = element.requireJsonObject()
                ReferenceImageItem(
                    id = item.requiredString("id").requireOpaque("reference image id", MAX_ID_BYTES),
                    title = safeEvidence(item.requiredString("title"), SafeDisplayText.MAX_LABEL),
                    caption = safeEvidence(
                        item.requiredPossiblyEmptyString("caption"),
                        SafeDisplayText.MAX_DETAIL,
                    ),
                    imageQuery = safeEvidence(
                        item.requiredString("imageQuery"),
                        SafeDisplayText.MAX_DETAIL,
                    ),
                    imageLabel = safeEvidence(
                        item.requiredString("imageLabel"),
                        SafeDisplayText.MAX_LABEL,
                    ),
                    uncertain = item["uncertain"]?.jsonPrimitive?.booleanOrNull
                        ?: throw ProtocolV1Exception("Missing uncertain flag"),
                )
            }
        if (items.map(ReferenceImageItem::id).distinct().size != items.size) {
            throw ProtocolV1Exception("Duplicate reference image id")
        }
        return ReferenceGalleryBlock(
            id = id,
            title = safeEvidence(raw.requiredString("title"), SafeDisplayText.MAX_LABEL),
            subtitle = safeEvidence(
                raw.requiredPossiblyEmptyString("subtitle"),
                SafeDisplayText.MAX_DETAIL,
            ),
            items = items,
            fallbackText = safeEvidence(fallback, SafeDisplayText.MAX_FALLBACK),
        )
    }

    private fun decodeComparison(
        id: String,
        raw: JsonObject,
        fallback: String,
    ): ComparisonBlock {
        val items = raw.requiredArray("items")
            .boundedItems(MAX_COMPARISON_ITEMS, "comparison items")
            .also { if (it.size < 2) throw ProtocolV1Exception("Comparison requires two items") }
            .map { element ->
                val item = element.requireJsonObject()
                val facts = item.requiredArray("facts")
                    .boundedItems(MAX_COMPARISON_FACTS, "comparison facts")
                    .map { factElement ->
                        val fact = factElement.requireJsonObject()
                        ComparisonFact(
                            label = safeEvidence(
                                fact.requiredString("label"),
                                MAX_SHORT_VALUE_BYTES,
                            ),
                            value = safeEvidence(
                                fact.requiredString("value"),
                                SafeDisplayText.MAX_LABEL,
                            ),
                        )
                    }
                ComparisonItem(
                    id = item.requiredString("id").requireOpaque("comparison item id", MAX_ID_BYTES),
                    primary = safeEvidence(item.requiredString("primary"), SafeDisplayText.MAX_LABEL),
                    secondary = safeEvidence(
                        item.requiredPossiblyEmptyString("secondary"),
                        SafeDisplayText.MAX_LABEL,
                    ),
                    detail = safeEvidence(
                        item.requiredPossiblyEmptyString("detail"),
                        SafeDisplayText.MAX_DETAIL,
                    ),
                    badge = safeEvidence(
                        item.requiredPossiblyEmptyString("badge"),
                        MAX_SHORT_VALUE_BYTES,
                    ),
                    facts = facts,
                    imageQuery = safeEvidence(
                        item.requiredPossiblyEmptyString("imageQuery"),
                        SafeDisplayText.MAX_DETAIL,
                    ),
                    imageLabel = safeEvidence(
                        item.requiredPossiblyEmptyString("imageLabel"),
                        SafeDisplayText.MAX_LABEL,
                    ),
                    recommended = item["recommended"]?.jsonPrimitive?.booleanOrNull
                        ?: throw ProtocolV1Exception("Missing recommendation flag"),
                    uncertain = item["uncertain"]?.jsonPrimitive?.booleanOrNull
                        ?: throw ProtocolV1Exception("Missing uncertain flag"),
                )
            }
        if (items.map(ComparisonItem::id).distinct().size != items.size) {
            throw ProtocolV1Exception("Duplicate comparison item id")
        }
        return ComparisonBlock(
            id = id,
            title = safeEvidence(raw.requiredString("title"), SafeDisplayText.MAX_LABEL),
            subtitle = safeEvidence(
                raw.requiredPossiblyEmptyString("subtitle"),
                SafeDisplayText.MAX_DETAIL,
            ),
            selectLabel = safeEvidence(raw.requiredString("selectLabel"), MAX_SHORT_VALUE_BYTES),
            items = items,
            fallbackText = safeEvidence(fallback, SafeDisplayText.MAX_FALLBACK),
        )
    }

    private fun decodeBilingualCard(
        id: String,
        raw: JsonObject,
        fallback: String,
    ): BilingualCardBlock {
        val lines = raw.requiredArray("lines")
            .boundedItems(MAX_BILINGUAL_LINES, "bilingual lines")
            .also { if (it.isEmpty()) throw ProtocolV1Exception("Empty bilingual card") }
            .map { element ->
                val line = element.requireJsonObject()
                BilingualLine(
                    id = line.requiredString("id").requireOpaque("bilingual line id", MAX_ID_BYTES),
                    original = safeEvidence(
                        line.requiredString("original"),
                        SafeDisplayText.MAX_DETAIL,
                    ),
                    translation = safeEvidence(
                        line.requiredString("translation"),
                        SafeDisplayText.MAX_DETAIL,
                    ),
                    note = safeEvidence(
                        line.requiredPossiblyEmptyString("note"),
                        SafeDisplayText.MAX_DETAIL,
                    ),
                )
            }
        if (lines.map(BilingualLine::id).distinct().size != lines.size) {
            throw ProtocolV1Exception("Duplicate bilingual line id")
        }
        return BilingualCardBlock(
            id = id,
            titleOriginal = safeEvidence(raw.requiredString("titleOriginal"), SafeDisplayText.MAX_LABEL),
            titleChinese = safeEvidence(raw.requiredString("titleChinese"), SafeDisplayText.MAX_LABEL),
            contextOriginal = safeEvidence(
                raw.requiredPossiblyEmptyString("contextOriginal"),
                SafeDisplayText.MAX_DETAIL,
            ),
            contextChinese = safeEvidence(
                raw.requiredPossiblyEmptyString("contextChinese"),
                SafeDisplayText.MAX_DETAIL,
            ),
            showLabel = safeEvidence(raw.requiredString("showLabel"), MAX_SHORT_VALUE_BYTES),
            lines = lines,
            fallbackText = safeEvidence(fallback, SafeDisplayText.MAX_FALLBACK),
        )
    }

    private fun safeFileLabel(raw: JsonObject, fallback: String): String {
        val candidate = raw.optionalString("fileLabel")
            ?: raw.optionalString("label")
            ?: raw.optionalString("name")
            ?: raw.optionalString("path")
            ?: fallback
        return SafeDisplayText.fileLabel(candidate, fallback)
    }

    private fun safeSemanticCommandLabel(value: String): String? {
        val sanitized = safeEvidence(value, SafeDisplayText.MAX_LABEL)
        val looksLikeRawCommand = sanitized.any { it in "\\/$;|&><\"'`" } ||
            sanitized.contains("--") || sanitized.contains('=') || sanitized.contains('\n')
        return sanitized.takeUnless { looksLikeRawCommand }
    }

    private fun decodeFileChange(value: String?): FileChangeKind = when (value?.lowercase()) {
        "create", "created", "add", "added" -> FileChangeKind.CREATED
        "modify", "modified", "update", "updated" -> FileChangeKind.MODIFIED
        "delete", "deleted", "remove", "removed" -> FileChangeKind.DELETED
        "rename", "renamed", "move", "moved" -> FileChangeKind.RENAMED
        "read", "viewed" -> FileChangeKind.READ
        else -> FileChangeKind.UNKNOWN
    }

    private fun decodeEvidenceState(value: String?, fallback: StepState): StepState = when (value?.lowercase()) {
        null -> fallback
        "pending", "queued" -> StepState.PENDING
        "running", "active", "inprogress", "in_progress" -> StepState.ACTIVE
        "completed", "complete", "success", "passed", "skipped" -> StepState.COMPLETED
        "failed", "failure", "error" -> StepState.FAILED
        else -> throw ProtocolV1Exception("Unknown evidence state")
    }

    private fun safeEvidence(value: String, maxLength: Int): String = SafeDisplayText.sanitize(value, maxLength)

    private fun decodeSource(id: String, raw: JsonObject, fallback: String): SourceBlock {
        val sourceItems = (raw["items"] as? JsonArray)?.boundedItems(MAX_SOURCE_ITEMS, "sources")
        val first = sourceItems?.firstOrNull()?.requireJsonObject()
        return SourceBlock(
            id = id,
            label = safeEvidence(
                raw.optionalString("label")
                    ?: first?.optionalString("label")
                    ?: "来源",
                SafeDisplayText.MAX_LABEL,
            ),
            detail = safeEvidence(
                raw.optionalString("detail")
                    ?: first?.optionalString("capturedAt")
                    ?: fallback,
                SafeDisplayText.MAX_DETAIL,
            ),
            trustLabel = (raw.optionalString("trustLabel") ?: first?.optionalString("trust"))
                ?.let { safeEvidence(it, MAX_SHORT_VALUE_BYTES) },
            fallbackText = safeEvidence(fallback, SafeDisplayText.MAX_FALLBACK),
        )
    }

    private fun decodeMessageState(value: String): RichMessageState = when (value) {
        "queued" -> RichMessageState.QUEUED
        "streaming" -> RichMessageState.STREAMING
        "waitingInput" -> RichMessageState.WAITING_INPUT
        "completed" -> RichMessageState.COMPLETED
        "failed" -> RichMessageState.FAILED
        "interrupted" -> RichMessageState.INTERRUPTED
        "cancelled" -> RichMessageState.CANCELLED
        else -> throw ProtocolV1Exception("Unknown message state")
    }

    private fun decodeTone(value: String): Tone = when (value) {
        "neutral" -> Tone.NEUTRAL
        "info" -> Tone.INFO
        "success" -> Tone.SUCCESS
        "warning" -> Tone.WARNING
        "danger" -> Tone.DANGER
        else -> throw ProtocolV1Exception("Unknown tone")
    }

    private fun decodeStepState(value: String): StepState = when (value) {
        "pending" -> StepState.PENDING
        "running" -> StepState.ACTIVE
        "completed" -> StepState.COMPLETED
        "failed" -> StepState.FAILED
        else -> throw ProtocolV1Exception("Unknown step state")
    }

    private fun decodeActionKind(value: String?): ActionKind = when (value) {
        "reject", "destructive" -> ActionKind.DESTRUCTIVE
        "open", "submit", "secondary" -> ActionKind.SECONDARY
        else -> ActionKind.PRIMARY
    }

    private fun requireSafeActionRef(value: String): String {
        val bounded = value.requireOpaque("action reference", MAX_ACTION_REF_BYTES)
        val scheme = bounded.substringBefore(':', missingDelimiterValue = "").lowercase()
        if (scheme in FORBIDDEN_ACTION_SCHEMES) throw ProtocolV1Exception("Unsafe action reference")
        return bounded
    }

    private fun String.objectRoot(): JsonObject = try {
        if (SafeDisplayText.exceedsUtf8Limit(this, MAX_JSON_FRAME_BYTES)) {
            throw ProtocolV1Exception("JSON frame exceeds limit")
        }
        preflightJsonStructure()
        json.parseToJsonElement(this).also(::validateJsonStructure).requireJsonObject()
    } catch (_: Exception) {
        throw ProtocolV1Exception("Invalid protocol JSON")
    }

    private fun JsonObject.requiredString(key: String): String =
        this[key]?.jsonPrimitive?.contentOrNull?.takeIf(String::isNotBlank)
            ?: throw ProtocolV1Exception("Missing string field")

    private fun JsonObject.requiredPossiblyEmptyString(key: String): String {
        val value = this[key] as? JsonPrimitive
            ?: throw ProtocolV1Exception("Missing string field")
        if (!value.isString) throw ProtocolV1Exception("Invalid string field")
        return value.content
    }

    private fun JsonObject.requiredInt(key: String): Int =
        this[key]?.jsonPrimitive?.intOrNull ?: throw ProtocolV1Exception("Missing integer field")

    private fun JsonObject.requiredLong(key: String): Long =
        this[key]?.jsonPrimitive?.longOrNull ?: throw ProtocolV1Exception("Missing integer field")

    private fun JsonObject.requiredArray(key: String): JsonArray =
        this[key] as? JsonArray ?: throw ProtocolV1Exception("Missing array field")

    private fun JsonObject.requiredObject(key: String): JsonObject =
        this[key] as? JsonObject ?: throw ProtocolV1Exception("Missing object field")

    private fun JsonElement.requireJsonObject(): JsonObject =
        this as? JsonObject ?: throw ProtocolV1Exception("Expected object")

    private fun JsonObject.optionalString(key: String): String? =
        this[key]?.jsonPrimitive?.contentOrNull?.takeIf(String::isNotBlank)

    private fun JsonObject.optionalNonNegativeInt(key: String): Int? {
        val value = this[key]?.jsonPrimitive?.intOrNull ?: return null
        if (value < 0 || value > MAX_EVIDENCE_COUNT) throw ProtocolV1Exception("Invalid evidence count")
        return value
    }

    private fun String.requireTimestamp(): Instant = try {
        Instant.parse(this)
    } catch (_: Exception) {
        throw ProtocolV1Exception("Invalid timestamp")
    }

    private fun String.boundedText(): String = boundedUtf8(MAX_TEXT_LENGTH)

    private fun String.boundedUtf8(maxBytes: Int): String {
        if (SafeDisplayText.exceedsUtf8Limit(this, maxBytes)) {
            throw ProtocolV1Exception("Text exceeds limit")
        }
        return this
    }

    private fun Long.requireRevision(): Long {
        if (this < 0) throw ProtocolV1Exception("Invalid revision")
        return this
    }

    private fun String.requireOpaque(field: String, maxBytes: Int): String {
        boundedUtf8(maxBytes)
        if (isEmpty() || any { it.isWhitespace() || it.isISOControl() } || OPAQUE_UNSAFE.containsMatchIn(this)) {
            throw ProtocolV1Exception("Invalid $field")
        }
        return this
    }

    private fun String.preflightJsonStructure() {
        val containers = CharArray(MAX_JSON_DEPTH)
        val itemCounts = IntArray(MAX_JSON_DEPTH)
        var depth = 0
        var nodes = 0
        var inString = false
        var escaped = false
        var stringLiteralBytes = 0
        var scalarActive = false
        var scalarBytes = 0

        fun finishScalar() {
            if (!scalarActive) return
            nodes += 1
            if (nodes > MAX_JSON_NODES) throw ProtocolV1Exception("JSON node limit exceeded")
            scalarActive = false
            scalarBytes = 0
        }

        for (character in this) {
            if (inString) {
                if (character.code < 0x20) throw ProtocolV1Exception("Invalid JSON control character")
                stringLiteralBytes += when {
                    character.code <= 0x7f -> 1
                    character.code <= 0x7ff -> 2
                    else -> 3
                }
                if (stringLiteralBytes > MAX_JSON_STRING_LITERAL_BYTES) {
                    throw ProtocolV1Exception("JSON string exceeds preflight limit")
                }
                when {
                    escaped -> escaped = false
                    character == '\\' -> escaped = true
                    character == '"' -> inString = false
                }
                continue
            }

            when (character) {
                '"' -> {
                    finishScalar()
                    inString = true
                    stringLiteralBytes = 0
                    nodes += 1
                }
                '{', '[' -> {
                    finishScalar()
                    if (depth >= MAX_JSON_DEPTH) throw ProtocolV1Exception("JSON nesting limit exceeded")
                    containers[depth] = character
                    itemCounts[depth] = 1
                    depth += 1
                    nodes += 1
                }
                '}', ']' -> {
                    finishScalar()
                    if (depth == 0) throw ProtocolV1Exception("Unbalanced JSON structure")
                    val expected = if (character == '}') '{' else '['
                    if (containers[depth - 1] != expected) throw ProtocolV1Exception("Mismatched JSON structure")
                    depth -= 1
                }
                ',' -> if (depth > 0) {
                    finishScalar()
                    val containerIndex = depth - 1
                    itemCounts[containerIndex] += 1
                    val limit = if (containers[containerIndex] == '{') {
                        MAX_JSON_OBJECT_FIELDS
                    } else {
                        MAX_JSON_ARRAY_ITEMS
                    }
                    if (itemCounts[containerIndex] > limit) {
                        throw ProtocolV1Exception("JSON container limit exceeded")
                    }
                }
                ':' -> finishScalar()
                ' ', '\t', '\r', '\n' -> finishScalar()
                else -> {
                    scalarActive = true
                    scalarBytes += when {
                        character.code <= 0x7f -> 1
                        character.code <= 0x7ff -> 2
                        else -> 3
                    }
                    if (scalarBytes > MAX_JSON_SCALAR_LITERAL_BYTES) {
                        throw ProtocolV1Exception("JSON scalar exceeds preflight limit")
                    }
                }
            }
            if (nodes > MAX_JSON_NODES) throw ProtocolV1Exception("JSON node limit exceeded")
        }

        finishScalar()
        if (inString || escaped || depth != 0) throw ProtocolV1Exception("Incomplete JSON structure")
    }

    private fun JsonArray.boundedItems(max: Int, field: String): JsonArray {
        if (size > max) throw ProtocolV1Exception("Too many $field")
        return this
    }

    private fun validateJsonStructure(
        root: JsonElement,
        maxNodes: Int = MAX_JSON_NODES,
        maxDepth: Int = MAX_JSON_DEPTH,
        maxArrayItems: Int = MAX_JSON_ARRAY_ITEMS,
    ) {
        data class Node(val value: JsonElement, val depth: Int)

        val pending = ArrayDeque<Node>()
        pending.add(Node(root, 1))
        var nodes = 0
        while (pending.isNotEmpty()) {
            val (value, depth) = pending.removeLast()
            nodes += 1
            if (nodes > maxNodes) throw ProtocolV1Exception("JSON node limit exceeded")
            if (depth > maxDepth) throw ProtocolV1Exception("JSON nesting limit exceeded")
            when (value) {
                is JsonArray -> {
                    if (value.size > maxArrayItems) throw ProtocolV1Exception("JSON array limit exceeded")
                    value.forEach { pending.add(Node(it, depth + 1)) }
                }
                is JsonObject -> {
                    if (value.size > MAX_JSON_OBJECT_FIELDS) throw ProtocolV1Exception("JSON object limit exceeded")
                    value.values.forEach { pending.add(Node(it, depth + 1)) }
                }
                is JsonPrimitive -> if (value.isString) value.content.boundedUtf8(MAX_TEXT_LENGTH)
                else -> Unit
            }
        }
    }

    private fun JsonElement.toPatchValue(): Any? = when (this) {
        JsonNull -> null
        is JsonObject -> mapValues { (_, value) -> value.toPatchValue() }
        is JsonArray -> map { it.toPatchValue() }
        is JsonPrimitive -> when {
            isString -> content
            booleanOrNull != null -> booleanOrNull
            longOrNull != null -> longOrNull
            doubleOrNull != null -> doubleOrNull
            else -> content
        }
    }

    private companion object {
        const val RICH_SCHEMA = "cheby.rich-message/1.0"
        const val SAFE_FALLBACK = "暂不支持的消息内容"
        const val MAX_BLOCKS = 128
        const val MAX_ROOT_BLOCKS = 64
        const val MAX_PATCH_OPS = 64
        const val MAX_TEXT_LENGTH = 65_536
        const val MAX_TYPE_LENGTH = 96
        const val MAX_EVIDENCE_COUNT = 1_000_000
        const val MAX_ID_BYTES = 256
        const val MAX_ACTION_REF_BYTES = 512
        const val MAX_ASSET_REF_BYTES = 128
        const val MAX_CREDENTIAL_BYTES = 16_384
        const val MAX_SHORT_VALUE_BYTES = 128
        const val MAX_UNIT_BYTES = 64
        const val MAX_BLOCK_ITEMS = 64
        const val MAX_ACTION_ITEMS = 8
        const val MAX_SOURCE_ITEMS = 32
        const val MAX_INTERACTIVE_GROUPS = 32
        const val MAX_INTERACTIVE_ITEMS = 256
        const val MAX_INTERACTIVE_PRICE_MINOR = 1_000_000_000_000L
        const val MAX_REFERENCE_GALLERY_ITEMS = 8
        const val MAX_COMPARISON_ITEMS = 12
        const val MAX_COMPARISON_FACTS = 8
        const val MAX_BILINGUAL_LINES = 24
        const val MAX_THREADS = 2_000
        const val MAX_MESSAGES = 10_000
        const val MAX_APPROVALS = 128
        const val MAX_APPROVAL_DECISIONS = 2
        val INTERACTIVE_MINOR_UNIT_SCALES = setOf(1, 10, 100, 1_000)
        const val MAX_JSON_DEPTH = 16
        const val MAX_JSON_ARRAY_ITEMS = 10_000
        const val MAX_JSON_OBJECT_FIELDS = 512
        const val MAX_JSON_NODES = 500_000
        const val MAX_JSON_STRING_LITERAL_BYTES = MAX_TEXT_LENGTH * 6
        const val MAX_JSON_SCALAR_LITERAL_BYTES = 1_024
        const val MAX_MESSAGE_JSON_NODES = 32_768
        const val MAX_JSON_FRAME_BYTES = 64 * 1024 * 1024
        const val MAX_MESSAGE_JSON_BYTES = 256 * 1024
        const val MAX_PATCH_VALUE_NODES = 4_096
        const val MAX_PATCH_VALUE_DEPTH = 8
        const val LOCAL_IMAGE_MAX_UPLOAD_BYTES = 8 * 1024 * 1024
        const val LOCAL_IMAGE_MAX_PIXELS = 25_000_000
        const val LOCAL_IMAGE_MAX_EDGE_PIXELS = 12_000
        const val LOCAL_IMAGE_MAX_IMAGES = 10
        const val ASSET_UNAVAILABLE_BLOCK_ID = "asset-unavailable-notice"
        const val LOCAL_IMAGE_UNAVAILABLE_FALLBACK = "图片附件已失效，请重新选择后发送。"
        val OPAQUE_UNSAFE = Regex("\\p{Cf}")
        val ASSET_REF = Regex("[A-Za-z0-9_-]{32,128}")
        val LOCAL_IMAGE_MEDIA_TYPES = setOf("image/jpeg", "image/png")
        val FORBIDDEN_ACTION_SCHEMES = setOf("javascript", "data", "file", "content", "intent")
        val APPROVAL_DECISIONS = setOf("approve", "reject")
        val TERMINAL_APPROVAL_STATES = setOf("approved", "rejected", "expired")
        val TURN_STATUSES = setOf(
            "queued", "running", "inProgress", "completed", "failed", "interrupted",
        )
        val TURN_LIFECYCLE_TYPES = setOf(
            "turn.started", "turn.completed", "turn.failed", "turn.interrupted",
        )
        val FROZEN_MEDIA_UNAVAILABLE_FIELDS = setOf("assetId", "fallbackText")
        val LOCAL_IMAGE_UNAVAILABLE_FIELDS = setOf("clientMessageId", "reason")
        val LOCAL_IMAGE_UNAVAILABLE_REASONS = setOf("binding", "storageValidation", "expired")
        val KNOWN_EVENT_TYPES = setOf(
            "thread.snapshot",
            "thread.updated",
            "thread.deleted",
            "turn.started",
            "turn.completed",
            "turn.failed",
            "turn.interrupted",
            "message.snapshot",
            "message.patch",
            "approval.requested",
            "approval.resolved",
            "approval.expired",
            "asset.unavailable",
            "sync.required",
            "error",
            "audit.action",
        )
    }
}
