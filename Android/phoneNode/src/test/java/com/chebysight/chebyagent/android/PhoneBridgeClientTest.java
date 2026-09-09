package com.chebysight.chebyagent.android;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

import java.io.IOException;
import java.net.ConnectException;
import java.net.SocketTimeoutException;

import javax.net.ssl.SSLHandshakeException;

public final class PhoneBridgeClientTest {
    @Test
    public void connectionDiagnosticsUseBoundedCodesOnly() {
        assertEquals("none", PhoneBridgeClient.connectionErrorCode(null));
        assertEquals("timeout", PhoneBridgeClient.connectionErrorCode(new SocketTimeoutException("secret")));
        assertEquals("tls_handshake_failed", PhoneBridgeClient.connectionErrorCode(new SSLHandshakeException("secret")));
        assertEquals("network_unreachable", PhoneBridgeClient.connectionErrorCode(new ConnectException("10.0.0.1")));
        assertEquals("transport_io_failed", PhoneBridgeClient.connectionErrorCode(new IOException("/private/path")));
        assertEquals("protocol_or_configuration_failed", PhoneBridgeClient.connectionErrorCode(new IllegalStateException("token")));
        assertEquals("unexpected_failure", PhoneBridgeClient.connectionErrorCode(new RuntimeException("native address")));
    }

    @Test
    public void upgradeRequiredHasDedicatedCode() {
        assertEquals(
                "client_identity_upgrade_required",
                PhoneBridgeClient.connectionErrorCode(
                        new PhoneBridgeTlsStore.ClientIdentityUpgradeRequiredException()
                )
        );
    }
}
