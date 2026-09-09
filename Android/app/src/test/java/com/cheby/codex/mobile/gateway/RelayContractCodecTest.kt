package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.data.InMemorySecureRelayConfigStore
import com.cheby.codex.mobile.data.RelayDeviceSession
import com.cheby.codex.mobile.data.RelayResponseReceipt
import java.util.Base64
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RelayContractCodecTest {
    private val assistantId = "asst_0000000000000000000000"
    private val deviceId = "dev_0000000000000000000000"
    private val requestId = "req_0000000000000000000000"
    private val messageId = "msg_0000000000000000000000"

    @Test
    fun `pairing code is exact unpadded base64url JSON and origin is separate`() {
        val json = """{"v":1,"assistantId":"$assistantId","pairingSecret":"pair_0000000000000000000000000000000000000000000"}"""
        val code = "CXC1." + Base64.getUrlEncoder().withoutPadding()
            .encodeToString(json.toByteArray(Charsets.UTF_8))

        val invitation = RelayInvitationCodec.decode(code)

        assertEquals(assistantId, invitation.assistantId)
        assertEquals("https://relay.example", RelayOrigin.normalize(" https://RELAY.example:443/ "))
        assertEquals("https://[2001:db8::1]:8443", RelayOrigin.normalize("https://[2001:db8::1]:8443/"))
        listOf(
            code + "=",
            code.replace("CXC1.", "CXC2."),
            encoded("""{"v":1,"assistantId":"$assistantId","pairingSecret":"pair_0000000000000000000000000000000000000000000","relayOrigin":"https://evil"}"""),
        ).forEach { invalid ->
            assertTrue(runCatching { RelayInvitationCodec.decode(invalid) }.exceptionOrNull() is GatewayTransportException)
        }
        listOf(
            "http://relay.example",
            "https://user@relay.example",
            "https://relay.example/path",
            "https://relay.example?token=secret",
            "https://relay.example:0",
        ).forEach { invalid ->
            assertTrue(runCatching { RelayOrigin.normalize(invalid) }.exceptionOrNull() is GatewayTransportException)
        }
    }

    @Test
    fun `relay codec accepts frozen frames and rejects unknown fields`() {
        val codec = RelayV1Codec()
        val command = codec.encodeCommand(
            messageId,
            requestId,
            deviceId,
            "threads.read",
            buildJsonObject { put("threadId", "thread-public") },
        )
        assertEquals(
            """{"v":1,"type":"message","messageId":"$messageId","payload":{"kind":"command","requestId":"$requestId","deviceId":"$deviceId","operation":"threads.read","params":{"threadId":"thread-public"}}}""",
            command,
        )
        val ready = codec.decode(
            """{"v":1,"type":"ready","assistantId":"$assistantId","principalId":"$deviceId","role":"device","ackCursor":0,"nextDeliverySeq":1,"nodeStatus":"online"}""",
        ) as RelayV1Frame.Ready
        assertTrue(ready.nodeOnline)

        val response = codec.decode(
            """{"v":1,"type":"delivery","deliverySeq":1,"messageId":"$messageId","payload":{"kind":"response","requestId":"$requestId","deviceId":"$deviceId","operation":"threads.read","ok":true,"result":{"threadId":"thread-public"}}}""",
        ) as RelayV1Frame.Delivery
        assertEquals(requestId, response.payload.requestId)

        val event = codec.decode(
            """{"v":1,"type":"delivery","deliverySeq":2,"messageId":"$messageId","payload":{"kind":"event","requestId":"$requestId","deviceId":"$deviceId","streamId":"stream-public","eventId":"event-00000001","eventSeq":1,"eventType":"message.patch","threadId":"thread-public","data":{"occurredAt":"2026-07-20T12:00:00Z","payload":{}}}}""",
        ) as RelayV1Frame.Delivery
        assertTrue(event.payload is RelayV1Payload.Event)

        val unknown = """{"v":1,"type":"acknowledged","deliverySeq":2,"token":"secret"}"""
        assertTrue(runCatching { codec.decode(unknown) }.exceptionOrNull() is GatewayTransportException)
    }

    @Test
    fun `durable response receipt survives transport ack and never crosses binding`() {
        val session = relaySession()
        val store = InMemorySecureRelayConfigStore(session)
        val receipt = RelayResponseReceipt(
            requestId = requestId,
            deviceId = deviceId,
            operation = "turns.start",
            resultJson = "{}",
            errorCode = null,
            retryable = false,
            retryAfterMillis = null,
            deliverySeq = 1,
        )

        assertTrue(store.recordResponseReceipt(session.bindingScope, receipt))
        assertTrue(store.updateRelayCursor(session.bindingScope, 1))
        assertEquals(receipt, store.loadResponseReceipt(session.bindingScope, requestId))
        assertFalse(store.recordResponseReceipt("other-binding", receipt))
        assertEquals(null, store.loadResponseReceipt("other-binding", requestId))
    }

    private fun encoded(json: String): String = "CXC1." + Base64.getUrlEncoder().withoutPadding()
        .encodeToString(json.toByteArray(Charsets.UTF_8))

    private fun relaySession() = RelayDeviceSession(
        assistantId = assistantId,
        assistantAlias = "我的 Codex",
        relayOrigin = "https://relay.example",
        bindingScope = "binding-public",
        principalId = deviceId,
        accessToken = "access-token-00000000000000000000000000000000",
        refreshToken = "refresh-token-0000000000000000000000000000000",
        accessExpiresAt = 1_800_000_000,
        refreshExpiresAt = 1_900_000_000,
        gatewayStreamId = "pending",
    )
}
