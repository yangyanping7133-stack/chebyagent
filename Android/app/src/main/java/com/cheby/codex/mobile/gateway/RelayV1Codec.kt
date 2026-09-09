package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.model.SafeDisplayText
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put

internal sealed interface RelayV1Frame {
    data class Ready(
        val assistantId: String,
        val principalId: String,
        val ackCursor: Long,
        val nextDeliverySeq: Long,
        val nodeOnline: Boolean,
    ) : RelayV1Frame

    data class Accepted(
        val messageId: String,
        val deliverySeq: Long,
        val duplicate: Boolean,
        val queued: Boolean,
    ) : RelayV1Frame

    data class Acknowledged(val deliverySeq: Long) : RelayV1Frame
    data class Delivery(
        val deliverySeq: Long,
        val messageId: String,
        val payload: RelayV1Payload,
    ) : RelayV1Frame

    data class NodeStatus(val online: Boolean) : RelayV1Frame
    data class Ping(val nonce: String) : RelayV1Frame
    data class Pong(val nonce: String) : RelayV1Frame
    data class Error(val code: String, val retryable: Boolean) : RelayV1Frame
}

internal sealed interface RelayV1Payload {
    val requestId: String
    val deviceId: String

    data class Response(
        override val requestId: String,
        override val deviceId: String,
        val operation: String,
        val result: JsonObject?,
        val errorCode: String?,
        val retryable: Boolean,
        val retryAfterMillis: Long?,
    ) : RelayV1Payload

    data class Event(
        override val requestId: String,
        override val deviceId: String,
        val streamId: String,
        val eventId: String,
        val eventSeq: Long,
        val eventType: String,
        val threadId: String?,
        val turnId: String?,
        val itemId: String?,
        val occurredAt: String,
        val payload: JsonObject,
    ) : RelayV1Payload
}

internal class RelayV1Codec(
    private val json: Json = Json { ignoreUnknownKeys = false },
) {
    fun encodeCommand(
        messageId: String,
        requestId: String,
        deviceId: String,
        operation: String,
        params: JsonObject,
    ): String {
        requireRelayId(messageId, MESSAGE_ID)
        requireRelayId(requestId, REQUEST_ID)
        requireRelayId(deviceId, DEVICE_ID)
        require(operation in OPERATIONS)
        val value = buildJsonObject {
            put("v", 1)
            put("type", "message")
            put("messageId", messageId)
            put("payload", buildJsonObject {
                put("kind", "command")
                put("requestId", requestId)
                put("deviceId", deviceId)
                put("operation", operation)
                put("params", params)
            })
        }.toString()
        val limit = if (operation == "assets.upload") MAX_RELAY_FRAME_BYTES else MAX_RELAY_COMMAND_BYTES
        if (SafeDisplayText.exceedsUtf8Limit(value, limit)) {
            throw GatewayTransportException("Relay command is too large")
        }
        return value
    }

    fun encodeAck(deliverySeq: Long): String {
        require(deliverySeq >= 0)
        return "{\"v\":1,\"type\":\"ack\",\"deliverySeq\":$deliverySeq}"
    }

    fun encodePong(nonce: String): String {
        requireNonce(nonce)
        return buildJsonObject {
            put("v", 1)
            put("type", "pong")
            put("nonce", nonce)
        }.toString()
    }

    fun encodePing(nonce: String): String {
        requireNonce(nonce)
        return buildJsonObject {
            put("v", 1)
            put("type", "ping")
            put("nonce", nonce)
        }.toString()
    }

    fun decodeResult(raw: String): JsonObject = protocolGuard {
        if (SafeDisplayText.exceedsUtf8Limit(raw, MAX_DURABLE_RESULT_BYTES)) mismatch()
        json.parseToJsonElement(raw).also(::validateStructure).jsonObject
    }

    fun decode(raw: String): RelayV1Frame = protocolGuard {
        // Device-bound frames never contain the one allowed large field (asset upload body).
        if (SafeDisplayText.exceedsUtf8Limit(raw, MAX_RELAY_COMMAND_BYTES)) mismatch()
        val root = json.parseToJsonElement(raw).also(::validateStructure).jsonObject
        if (root.int("v") != 1) mismatch()
        when (root.string("type")) {
            "ready" -> decodeReady(root)
            "accepted" -> decodeAccepted(root)
            "acknowledged" -> {
                root.requireKeys(ACKNOWLEDGED_FIELDS)
                RelayV1Frame.Acknowledged(root.nonNegativeLong("deliverySeq"))
            }
            "delivery" -> decodeDelivery(root)
            "node.status" -> {
                root.requireKeys(NODE_STATUS_FIELDS)
                RelayV1Frame.NodeStatus(root.status("status"))
            }
            "ping" -> {
                root.requireKeys(PING_FIELDS)
                RelayV1Frame.Ping(root.string("nonce").also(::requireNonce))
            }
            "pong" -> {
                root.requireKeys(PING_FIELDS)
                RelayV1Frame.Pong(root.string("nonce").also(::requireNonce))
            }
            "error" -> {
                root.requireKeys(ERROR_FIELDS)
                RelayV1Frame.Error(root.errorCode("code"), root.boolean("retryable"))
            }
            else -> mismatch()
        }
    }

    private fun decodeReady(root: JsonObject): RelayV1Frame.Ready {
        root.requireKeys(READY_FIELDS)
        if (root.string("role") != "device") mismatch()
        return RelayV1Frame.Ready(
            assistantId = root.string("assistantId").also { requireRelayId(it, ASSISTANT_ID) },
            principalId = root.string("principalId").also { requireRelayId(it, DEVICE_ID) },
            ackCursor = root.nonNegativeLong("ackCursor"),
            nextDeliverySeq = root.positiveLong("nextDeliverySeq"),
            nodeOnline = root.status("nodeStatus"),
        )
    }

    private fun decodeAccepted(root: JsonObject): RelayV1Frame.Accepted {
        root.requireKeys(ACCEPTED_FIELDS)
        return RelayV1Frame.Accepted(
            messageId = root.string("messageId").also { requireRelayId(it, MESSAGE_ID) },
            deliverySeq = root.positiveLong("deliverySeq"),
            duplicate = root.boolean("duplicate"),
            queued = root.boolean("queued"),
        )
    }

    private fun decodeDelivery(root: JsonObject): RelayV1Frame.Delivery {
        root.requireKeys(DELIVERY_FIELDS)
        val payload = root.objectValue("payload")
        return RelayV1Frame.Delivery(
            deliverySeq = root.positiveLong("deliverySeq"),
            messageId = root.string("messageId").also { requireRelayId(it, MESSAGE_ID) },
            payload = when (payload.string("kind")) {
                "response" -> decodeResponse(payload)
                "event" -> decodeEvent(payload)
                else -> mismatch()
            },
        )
    }

    private fun decodeResponse(raw: JsonObject): RelayV1Payload.Response {
        val ok = raw.boolean("ok")
        raw.requireKeys(if (ok) RESPONSE_SUCCESS_FIELDS else RESPONSE_ERROR_FIELDS)
        val requestId = raw.string("requestId").also { requireRelayId(it, REQUEST_ID) }
        val deviceId = raw.string("deviceId").also { requireRelayId(it, DEVICE_ID) }
        val operation = raw.string("operation").also { if (it !in OPERATIONS) mismatch() }
        if (ok) {
            return RelayV1Payload.Response(
                requestId, deviceId, operation, raw.objectValue("result"), null, false, null,
            )
        }
        val error = raw.objectValue("error")
        val allowed = if ("retryAfterSeconds" in error) OPERATION_ERROR_RETRY_FIELDS else OPERATION_ERROR_FIELDS
        error.requireKeys(allowed)
        val retryAfter = error["retryAfterSeconds"]?.jsonPrimitive?.longOrNull?.let {
            if (it !in 1..3_600) mismatch()
            it * 1_000
        }
        return RelayV1Payload.Response(
            requestId = requestId,
            deviceId = deviceId,
            operation = operation,
            result = null,
            errorCode = error.errorCode("code"),
            retryable = error.boolean("retryable"),
            retryAfterMillis = retryAfter,
        )
    }

    private fun decodeEvent(raw: JsonObject): RelayV1Payload.Event {
        raw.requireKeys(if ("threadId" in raw) EVENT_THREAD_FIELDS else EVENT_FIELDS)
        val data = raw.objectValue("data")
        if (data.keys !in EVENT_DATA_FIELD_SETS) mismatch()
        val turnId = data["turnId"]?.jsonPrimitive?.contentOrNull?.also(::requireOpaque)
        val itemId = data["itemId"]?.jsonPrimitive?.contentOrNull?.also(::requireOpaque)
        return RelayV1Payload.Event(
            requestId = raw.string("requestId").also { requireRelayId(it, REQUEST_ID) },
            deviceId = raw.string("deviceId").also { requireRelayId(it, DEVICE_ID) },
            streamId = raw.safeOpaque("streamId", 128),
            eventId = raw.safeOpaque("eventId", 128),
            eventSeq = raw.positiveLong("eventSeq"),
            eventType = raw.string("eventType").also { if (it !in EVENT_TYPES) mismatch() },
            threadId = raw["threadId"]?.jsonPrimitive?.contentOrNull?.also(::requireOpaque),
            turnId = turnId,
            itemId = itemId,
            occurredAt = data.string("occurredAt"),
            payload = data.objectValue("payload"),
        )
    }

    private fun validateStructure(root: JsonElement) {
        val stack = ArrayDeque<Pair<JsonElement, Int>>()
        stack.add(root to 1)
        var nodes = 0
        while (stack.isNotEmpty()) {
            val (value, depth) = stack.removeLast()
            nodes += 1
            if (nodes > MAX_JSON_NODES || depth > MAX_JSON_DEPTH) mismatch()
            when (value) {
                is JsonObject -> value.values.forEach { stack.add(it to depth + 1) }
                is JsonArray -> value.forEach { stack.add(it to depth + 1) }
                is JsonPrimitive, JsonNull -> Unit
            }
        }
    }

    private inline fun <T> protocolGuard(block: () -> T): T = try {
        block()
    } catch (error: GatewayTransportException) {
        throw error
    } catch (_: Exception) {
        throw GatewayTransportException("Relay protocol mismatch")
    }

    private fun mismatch(): Nothing = throw GatewayTransportException("Relay protocol mismatch")

    private fun JsonObject.requireKeys(expected: Set<String>) {
        if (keys != expected) mismatch()
    }

    private fun JsonObject.string(key: String): String =
        this[key]?.jsonPrimitive?.contentOrNull?.takeIf(String::isNotBlank) ?: mismatch()
    private fun JsonObject.int(key: String): Int = this[key]?.jsonPrimitive?.intOrNull ?: mismatch()
    private fun JsonObject.boolean(key: String): Boolean =
        this[key]?.jsonPrimitive?.booleanOrNull ?: mismatch()
    private fun JsonObject.nonNegativeLong(key: String): Long =
        (this[key]?.jsonPrimitive?.longOrNull ?: mismatch()).also { if (it < 0) mismatch() }
    private fun JsonObject.positiveLong(key: String): Long =
        nonNegativeLong(key).also { if (it == 0L) mismatch() }
    private fun JsonObject.objectValue(key: String): JsonObject = this[key] as? JsonObject ?: mismatch()
    private fun JsonObject.status(key: String): Boolean = when (string(key)) {
        "online" -> true
        "offline" -> false
        else -> mismatch()
    }
    private fun JsonObject.errorCode(key: String): String = string(key).also {
        if (!ERROR_CODE.matches(it)) mismatch()
    }
    private fun JsonObject.safeOpaque(key: String, maxBytes: Int): String = string(key).also {
        requireOpaque(it, maxBytes)
    }

    private fun requireNonce(value: String) {
        if (value.length !in 8..128 || !BASE64URL.matches(value)) mismatch()
    }

    private fun requireOpaque(value: String, maxBytes: Int = 1_024) {
        if (
            value.isBlank() ||
            value.toByteArray(Charsets.UTF_8).size > maxBytes ||
            value.any { it.isWhitespace() || it.isISOControl() }
        ) mismatch()
    }

    private fun requireRelayId(value: String, pattern: Regex) {
        if (!pattern.matches(value)) mismatch()
    }

    companion object {
        private val ASSISTANT_ID = Regex("^asst_[A-Za-z0-9_-]{22}$")
        private val DEVICE_ID = Regex("^dev_[A-Za-z0-9_-]{22}$")
        private val MESSAGE_ID = Regex("^msg_[A-Za-z0-9_-]{22}$")
        private val REQUEST_ID = Regex("^req_[A-Za-z0-9_-]{22}$")
        private val BASE64URL = Regex("^[A-Za-z0-9_-]+$")
        private val ERROR_CODE = Regex("^[A-Z][A-Z0-9_]{1,63}$")
        private val OPERATIONS = setOf(
            "server.info", "capabilities.get", "threads.list", "threads.create",
            "threads.read", "threads.resume", "threads.patch", "threads.delete.preview",
            "threads.delete.confirm", "turns.start", "assets.upload", "turns.interrupt",
            "approvals.decide", "events.subscribe", "events.ack",
        )
        private val EVENT_TYPES = setOf(
            "thread.snapshot", "thread.updated", "thread.deleted", "turn.started",
            "turn.completed", "turn.failed", "turn.interrupted", "message.snapshot",
            "message.patch", "approval.requested", "approval.resolved", "approval.expired",
            "asset.unavailable", "sync.required", "error", "audit.action",
        )
        private val READY_FIELDS = setOf(
            "v", "type", "assistantId", "principalId", "role", "ackCursor",
            "nextDeliverySeq", "nodeStatus",
        )
        private val ACCEPTED_FIELDS = setOf(
            "v", "type", "messageId", "deliverySeq", "duplicate", "queued",
        )
        private val ACKNOWLEDGED_FIELDS = setOf("v", "type", "deliverySeq")
        private val DELIVERY_FIELDS = setOf("v", "type", "deliverySeq", "messageId", "payload")
        private val NODE_STATUS_FIELDS = setOf("v", "type", "status")
        private val PING_FIELDS = setOf("v", "type", "nonce")
        private val ERROR_FIELDS = setOf("v", "type", "code", "message", "retryable")
        private val RESPONSE_SUCCESS_FIELDS =
            setOf("kind", "requestId", "deviceId", "operation", "ok", "result")
        private val RESPONSE_ERROR_FIELDS =
            setOf("kind", "requestId", "deviceId", "operation", "ok", "error")
        private val OPERATION_ERROR_FIELDS = setOf("code", "message", "retryable")
        private val OPERATION_ERROR_RETRY_FIELDS =
            setOf("code", "message", "retryable", "retryAfterSeconds")
        private val EVENT_FIELDS =
            setOf("kind", "requestId", "deviceId", "streamId", "eventId", "eventSeq", "eventType", "data")
        private val EVENT_THREAD_FIELDS = EVENT_FIELDS + "threadId"
        private val EVENT_DATA_FIELD_SETS = setOf(
            setOf("occurredAt", "payload"),
            setOf("occurredAt", "turnId", "payload"),
            setOf("occurredAt", "itemId", "payload"),
            setOf("occurredAt", "turnId", "itemId", "payload"),
        )
        private const val MAX_JSON_DEPTH = 32
        private const val MAX_JSON_NODES = 20_000
        private const val MAX_DURABLE_RESULT_BYTES = 4 * 1024 * 1024
    }
}

internal object RelayIds {
    private val random = SecureRandom()

    fun message(stableIdentity: String? = null): String = "msg_" + token("message", stableIdentity)
    fun request(stableIdentity: String? = null): String = "req_" + token("request", stableIdentity)
    fun nonce(): String = Base64.getUrlEncoder().withoutPadding().encodeToString(
        ByteArray(16).also(random::nextBytes),
    )

    private fun token(kind: String, stableIdentity: String?): String {
        val bytes = if (stableIdentity == null) {
            ByteArray(16).also(random::nextBytes)
        } else {
            MessageDigest.getInstance("SHA-256")
                .digest("CHEBY-ANDROID-RELAY-1\u0000$kind\u0000$stableIdentity".toByteArray(Charsets.UTF_8))
                .copyOf(16)
        }
        return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
    }
}
