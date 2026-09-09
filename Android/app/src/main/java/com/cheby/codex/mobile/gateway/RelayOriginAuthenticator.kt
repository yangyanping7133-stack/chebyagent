package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.data.RelayDeviceSession
import javax.net.ssl.SSLSocketFactory

/**
 * Side-effect-free origin probe used before the durable origin CAS.
 *
 * It reads only the mandatory Ready frame, sends no command or acknowledgement, and never owns a
 * credential store. Any queued delivery therefore remains unacknowledged and will replay to the
 * real gateway after the origin commit.
 */
internal class RelayOriginAuthenticator(
    private val sslSocketFactory: SSLSocketFactory,
    private val originValidator: (String) -> Unit,
    private val codec: RelayV1Codec = RelayV1Codec(),
) {
    fun authenticate(
        candidate: RelayDeviceSession,
        deviceProofKey: DeviceProofKeyHandle,
    ): RelayDeviceSession {
        originValidator(candidate.relayOrigin)
        val endpoint = RelayEndpoint.parse(
            candidate.relayOrigin.replaceFirst("https://", "wss://") +
                RelayEndpoint.DEVICE_SOCKET_PATH,
        )
        val socket = RelayBoundedDuplexConnection(sslSocketFactory)
        try {
            socket.connect(endpoint, candidate.accessToken, deviceProofKey)
            val readyFrame = socket.read() as? BoundedSocketEvent.Text
                ?: throw GatewayTransportException("Relay ready frame is missing")
            val ready = codec.decode(readyFrame.value) as? RelayV1Frame.Ready
                ?: throw GatewayTransportException("Relay ready frame is invalid")
            if (
                ready.assistantId != candidate.assistantId ||
                ready.principalId != candidate.principalId ||
                ready.ackCursor > candidate.relayAckCursor ||
                ready.nextDeliverySeq <= ready.ackCursor
            ) {
                throw GatewayTransportException("Relay binding changed")
            }
            return candidate
        } finally {
            socket.cancel()
        }
    }
}
