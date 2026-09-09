package com.cheby.codex.mobile.gateway

import java.net.ProxySelector
import javax.net.ssl.SSLSocketFactory

/** Production socket adapter reuses the existing bounded WSS and Keystore PoP handshake. */
internal class RelayBoundedDuplexConnection(
    sslSocketFactory: SSLSocketFactory,
    maxFrameBytes: Int = MAX_RELAY_FRAME_BYTES,
    proxySelectorProvider: () -> ProxySelector? = { ProxySelector.getDefault() },
) {
    private val socket = BoundedWebSocketConnection(
        sslSocketFactory = sslSocketFactory,
        maxFrameBytes = maxFrameBytes,
        proxySelectorProvider = proxySelectorProvider,
    )

    fun connect(
        endpoint: RelayEndpoint,
        accessToken: String,
        deviceProofKey: DeviceProofKeyHandle,
    ) {
        // BoundedWebSocketConnection puts the credential and PoP only in handshake headers.
        socket.connect(endpoint.asTlsHttpUrl(), accessToken, deviceProofKey)
    }

    fun sendText(value: String): Boolean = socket.sendText(value)

    fun read(): BoundedSocketEvent = socket.readEvent()

    fun cancel() = socket.cancel()
}
