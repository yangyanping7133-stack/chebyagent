package com.cheby.codex.mobile.data

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class SecureRelayConfigStoreInstrumentedTest {
    @Test
    fun relayCredentialsAreEncryptedAndTransportCursorIsIndependent() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val store = AndroidKeystoreRelayConfigStore(context)
        store.clearSession()
        val session = RelayDeviceSession(
            assistantId = "assistant-secret-id",
            assistantAlias = "我的 Codex",
            relayOrigin = "https://relay.example",
            bindingScope = "binding-secret-scope",
            principalId = "principal-secret-id",
            accessToken = "device-secret-access-token",
            refreshToken = "device-secret-refresh-token",
            accessExpiresAt = 1_800_000_000,
            refreshExpiresAt = 1_900_000_000,
            relayAckCursor = 3,
            gatewayStreamId = "gateway-secret-stream",
            gatewayAckSeq = 7,
        )

        try {
            assertTrue(store.saveSession(session))
            assertTrue(store.updateRelayCursor(session.bindingScope, 9))
            assertTrue(store.updateGatewayCursor(session.bindingScope, session.gatewayStreamId, 11))
            val rotated = requireNotNull(
                store.mergeRotatedCredentials(
                    expectedBindingScope = session.bindingScope,
                    expectedPrincipalId = session.principalId,
                    expectedRefreshToken = session.refreshToken,
                    accessToken = "rotated-device-secret-access-token",
                    refreshToken = "rotated-device-secret-refresh-token",
                    accessExpiresAt = 1_800_000_100,
                    refreshExpiresAt = 1_900_000_100,
                ),
            )
            assertEquals(9, rotated.relayAckCursor)
            assertEquals(11, rotated.gatewayAckSeq)
            val receipt = RelayResponseReceipt(
                requestId = "req_0000000000000000000000",
                deviceId = "dev_0000000000000000000000",
                operation = "turns.start",
                resultJson = "{}",
                errorCode = null,
                retryable = false,
                retryAfterMillis = null,
                deliverySeq = 9,
            )
            assertTrue(store.recordResponseReceipt(session.bindingScope, receipt))

            val loaded = requireNotNull(store.loadSession())
            assertEquals(9, loaded.relayAckCursor)
            assertEquals(11, loaded.gatewayAckSeq)
            assertEquals(
                receipt,
                store.loadResponseReceipt(session.bindingScope, receipt.requestId),
            )
            val rawValues = context
                .getSharedPreferences("chebycodex_secure_relay_v1", Context.MODE_PRIVATE)
                .all.values
                .joinToString("|")
            listOf(
                session.assistantId,
                session.bindingScope,
                session.principalId,
                session.accessToken,
                session.refreshToken,
                session.gatewayStreamId,
                rotated.accessToken,
                rotated.refreshToken,
            ).forEach { secret -> assertFalse(rawValues.contains(secret)) }
        } finally {
            store.clearSession()
        }
    }
}
