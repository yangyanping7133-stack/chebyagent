package com.cheby.codex.mobile.gateway

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RelayTopologyTest {
    @Test
    fun `fixed endpoint rejects query credentials and redacts its value`() {
        assertTrue(
            runCatching { RelayEndpoint.parse("wss://relay.example/relay/v1/device?token=secret") }
                .exceptionOrNull() is GatewayTransportException,
        )
        val endpoint = RelayEndpoint.parse("wss://relay.example/relay/v1/device")
        assertFalse(endpoint.toString().contains("relay.example"))
        val enrollment = RelayManagementEndpoint.parse(
            "https://relay.example/relay/v1/pairings/exchange",
            RelayManagementOperation.ENROLL,
        )
        assertFalse(enrollment.toString().contains("relay.example"))
        assertTrue(
            runCatching {
                RelayManagementEndpoint.parse(
                    "https://relay.example/relay/v1/auth/refresh?refreshToken=secret",
                    RelayManagementOperation.REFRESH,
                )
            }.exceptionOrNull() is GatewayTransportException,
        )
    }
}
