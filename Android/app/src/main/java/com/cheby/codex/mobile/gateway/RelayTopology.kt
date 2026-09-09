package com.cheby.codex.mobile.gateway

import java.net.URI
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/**
 * The Relay endpoint is derived from the separately entered, strictly normalized HTTPS origin.
 * Credentials, cursors and account identifiers are forbidden in the URL.
 */
internal class RelayEndpoint private constructor(val wssUrl: String) {
    internal fun asTlsHttpUrl(): HttpUrl =
        wssUrl.replaceFirst("wss://", "https://").toHttpUrlOrNull()
            ?: throw GatewayTransportException("Relay configuration is invalid")

    override fun toString(): String = "RelayEndpoint(redacted)"

    companion object {
        fun parse(value: String): RelayEndpoint {
            val uri = runCatching { URI(value) }.getOrNull()
                ?: throw GatewayTransportException("Relay configuration is invalid")
            if (
                uri.scheme != "wss" ||
                uri.host.isNullOrBlank() ||
                uri.userInfo != null ||
                uri.query != null ||
                uri.fragment != null ||
                uri.rawPath != DEVICE_SOCKET_PATH
            ) {
                throw GatewayTransportException("Relay configuration is invalid")
            }
            return RelayEndpoint(uri.toASCIIString())
        }

        const val DEVICE_SOCKET_PATH = "/relay/v1/device"
    }
}

internal enum class RelayManagementOperation(val path: String) {
    ENROLL("/relay/v1/pairings/exchange"),
    REFRESH("/relay/v1/auth/refresh"),
}

/** Pairing and refresh use fixed HTTPS paths with body-bound PoP; neither is a WSS command. */
internal class RelayManagementEndpoint private constructor(val httpsUrl: String) {
    override fun toString(): String = "RelayManagementEndpoint(redacted)"

    companion object {
        fun parse(value: String, operation: RelayManagementOperation): RelayManagementEndpoint {
            val uri = runCatching { URI(value) }.getOrNull()
                ?: throw GatewayTransportException("Relay configuration is invalid")
            if (
                uri.scheme != "https" ||
                uri.host.isNullOrBlank() ||
                uri.userInfo != null ||
                uri.query != null ||
                uri.fragment != null ||
                uri.rawPath != operation.path
            ) {
                throw GatewayTransportException("Relay configuration is invalid")
            }
            return RelayManagementEndpoint(uri.toASCIIString())
        }
    }
}

internal const val MAX_RELAY_COMMAND_BYTES = 256 * 1024
internal const val MAX_RELAY_FRAME_BYTES = 12 * 1024 * 1024
