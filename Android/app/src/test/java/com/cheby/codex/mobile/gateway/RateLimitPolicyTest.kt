package com.cheby.codex.mobile.gateway

import java.io.File
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class RateLimitPolicyTest {
    @Test
    fun `shared HTTP golden retry after is bounded milliseconds`() {
        val response = Json.parseToJsonElement(
            findRepositoryFile("gateway/tests/goldens/rate_limited_http.json").readText(),
        ).jsonObject.getValue("response").jsonObject
        val header = response.getValue("headers").jsonObject
            .getValue("Retry-After").jsonPrimitive.content

        assertEquals(30_000L, parseRetryAfterMillis(header))
    }

    @Test
    fun `shared websocket golden reason is parsed`() {
        val golden = Json.parseToJsonElement(
            findRepositoryFile("gateway/tests/goldens/websocket_rate_limited.json").readText(),
        ).jsonObject

        assertEquals(
            30_000L,
            parseSocketRetryAfterMillis(golden.getValue("closeReason").jsonPrimitive.content),
        )
        assertEquals(4429, golden.getValue("closeCode").jsonPrimitive.content.toInt())
    }

    @Test
    fun `malformed negative and oversized retry hints fail safe`() {
        assertNull(parseSocketRetryAfterMillis("retry-after=oops"))
        assertNull(parseSocketRetryAfterMillis("retry-after=-1"))
        assertNull(parseSocketRetryAfterMillis("other=30"))
        assertEquals(
            MAX_SERVER_RETRY_AFTER_SECONDS * 1_000,
            parseSocketRetryAfterMillis("retry-after=999999999999999999999999"),
        )
        assertEquals(
            MAX_SERVER_RETRY_AFTER_SECONDS * 1_000,
            parseSocketRetryAfterMillis("retry-after=999999999"),
        )
        assertEquals(750L, BoundedReconnectBackoff(random = { 0.5 }).nextDelay(null))
    }

    @Test
    fun `valid server hints above local cap are preserved through public bound`() {
        assertEquals(31_000L, parseRetryAfterMillis("31"))
        assertEquals(60_000L, parseRetryAfterMillis("60"))
        assertEquals(
            MAX_SERVER_RETRY_AFTER_SECONDS * 1_000,
            parseRetryAfterMillis(MAX_SERVER_RETRY_AFTER_SECONDS.toString()),
        )
        assertEquals(
            MAX_SERVER_RETRY_AFTER_SECONDS * 1_000,
            parseRetryAfterMillis((MAX_SERVER_RETRY_AFTER_SECONDS + 1).toString()),
        )

        val policy = BoundedReconnectBackoff(random = { 0.5 })
        assertEquals(60_000L, policy.nextDelay(60_000L))
    }

    @Test
    fun `consecutive failures grow bounded and success resets`() {
        val policy = BoundedReconnectBackoff(random = { 0.5 })

        assertEquals(750L, policy.nextDelay(null))
        assertEquals(1_500L, policy.nextDelay(null))
        assertEquals(3_000L, policy.nextDelay(null))
        policy.reset()
        assertEquals(750L, policy.nextDelay(null))

        repeat(20) { policy.nextDelay(null) }
        assertEquals(30_000L, policy.nextDelay(null))
    }

    private fun findRepositoryFile(relativePath: String): File {
        var cursor: File? = File(requireNotNull(System.getProperty("user.dir"))).absoluteFile
        while (cursor != null) {
            val candidate = File(cursor, relativePath)
            if (candidate.isFile) return candidate
            cursor = cursor.parentFile
        }
        error("Repository fixture not found")
    }
}
