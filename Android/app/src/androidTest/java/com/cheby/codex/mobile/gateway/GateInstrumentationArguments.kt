package com.cheby.codex.mobile.gateway

import android.os.Bundle
import android.util.Base64
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction

internal const val EXPECTED_REPLY_BASE64URL_ARGUMENT =
    "cheby_relay_expected_reply_b64url"

internal fun Bundle.requireExpectedReply(): String {
    val encoded = requireNotNull(getString(EXPECTED_REPLY_BASE64URL_ARGUMENT)) {
        "$EXPECTED_REPLY_BASE64URL_ARGUMENT is required"
    }
    require(encoded.matches(BASE64URL_VALUE) && encoded.length <= MAX_EXPECTED_REPLY_ENCODED) {
        "Expected reply evidence is not canonical Base64URL"
    }
    val bytes = Base64.decode(
        encoded,
        Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP,
    )
    val decoded = Charsets.UTF_8.newDecoder()
        .onMalformedInput(CodingErrorAction.REPORT)
        .onUnmappableCharacter(CodingErrorAction.REPORT)
        .decode(ByteBuffer.wrap(bytes))
        .toString()
    require(
        decoded.isNotBlank() &&
            decoded.length <= MAX_EXPECTED_REPLY_CHARS &&
            decoded.none { it.code < 0x20 },
    ) {
        "Expected reply evidence is invalid"
    }
    return decoded
}

private const val MAX_EXPECTED_REPLY_CHARS = 256
private const val MAX_EXPECTED_REPLY_ENCODED = 512
private val BASE64URL_VALUE = Regex("[A-Za-z0-9_-]{1,512}")
