package com.cheby.codex.mobile.gateway

import java.net.URI
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.util.Base64
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

internal object RelayInvitationCodec {
    private const val PREFIX = "CXC1."
    private const val MAX_INVITATION_CHARS = 1_024
    private val json = Json { ignoreUnknownKeys = false }
    private val assistantPattern = Regex("^asst_[A-Za-z0-9_-]{22}$")
    private val pairingPattern = Regex("^pair_[A-Za-z0-9_-]{27,123}$")

    fun decode(value: String): RelayEnrollmentInvitation {
        try {
            require(value.length in (PREFIX.length + 1)..MAX_INVITATION_CHARS)
            require(value.startsWith(PREFIX))
            val encoded = value.substring(PREFIX.length)
            require('=' !in encoded && encoded.all { it.isLetterOrDigit() || it == '-' || it == '_' })
            val bytes = Base64.getUrlDecoder().decode(encoded)
            require(Base64.getUrlEncoder().withoutPadding().encodeToString(bytes) == encoded)
            val raw = Charsets.UTF_8.newDecoder()
                .onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(bytes))
                .toString()
            val root = json.parseToJsonElement(raw).jsonObject
            require(root.keys == INVITATION_FIELDS)
            require(root.getValue("v").jsonPrimitive.intOrNull == 1)
            val assistantId = root.getValue("assistantId").jsonPrimitive.content
            val pairingSecret = root.getValue("pairingSecret").jsonPrimitive.content
            require(assistantPattern.matches(assistantId))
            require(pairingPattern.matches(pairingSecret))
            return RelayEnrollmentInvitation(assistantId, pairingSecret)
        } catch (_: Exception) {
            throw GatewayTransportException("Enrollment invitation is invalid")
        }
    }

    private val INVITATION_FIELDS = setOf("v", "assistantId", "pairingSecret")
}

internal object RelayOrigin {
    fun normalize(value: String): String {
        try {
            val trimmed = value.trim()
            val candidate = if ("://" in trimmed) trimmed else "https://$trimmed"
            val uri = URI(candidate)
            require(
                uri.scheme == "https" &&
                    !uri.host.isNullOrBlank() &&
                    uri.userInfo == null &&
                    uri.rawPath.orEmpty() in setOf("", "/") &&
                    uri.query == null &&
                    uri.fragment == null &&
                    (uri.port == -1 || uri.port in 1..65_535),
            )
            val rawHost = uri.host.lowercase().removePrefix("[").removeSuffix("]")
            require('%' !in rawHost)
            val host = if (':' in rawHost) "[$rawHost]" else rawHost
            val port = if (uri.port == -1 || uri.port == 443) "" else ":${uri.port}"
            return "https://$host$port"
        } catch (_: Exception) {
            throw GatewayTransportException("Relay address is invalid")
        }
    }
}
