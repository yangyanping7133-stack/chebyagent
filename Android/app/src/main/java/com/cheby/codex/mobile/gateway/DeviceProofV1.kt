package com.cheby.codex.mobile.gateway

import java.math.BigInteger
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import java.util.Locale

internal open class DeviceProofException(message: String, cause: Throwable? = null) :
    Exception(message, cause)

internal class DeviceIdentityUnavailableException(cause: Throwable? = null) :
    DeviceProofException("Device identity is unavailable", cause)

/** One handle owns both the registered SPKI and the private-key signing operation. */
internal interface DeviceProofKeyHandle {
    fun publicKeySpkiDer(): ByteArray
    fun signSha256(message: ByteArray): ByteArray
    fun verifySha256(message: ByteArray, signatureDer: ByteArray): Boolean
}

internal data class DeviceProofHeaders(
    val version: String,
    val timestamp: String,
    val nonce: String,
    val signature: String,
) {
    init {
        require(version == DeviceProofV1.VERSION)
        require(timestamp.isNotEmpty() && timestamp.all { it in '0'..'9' })
        require(listOf(version, timestamp, nonce, signature).none(DeviceProofV1::containsInvalidHeaderByte))
    }

    fun asMap(): Map<String, String> = linkedMapOf(
        DeviceProofV1.HEADER_VERSION to version,
        DeviceProofV1.HEADER_TIMESTAMP to timestamp,
        DeviceProofV1.HEADER_NONCE to nonce,
        DeviceProofV1.HEADER_SIGNATURE to signature,
    )

    override fun toString(): String = "DeviceProofHeaders(redacted)"
}

internal object DeviceProofV1 {
    const val VERSION = "1"
    const val HEADER_VERSION = "X-Cheby-Signature-Version"
    const val HEADER_TIMESTAMP = "X-Cheby-Timestamp"
    const val HEADER_NONCE = "X-Cheby-Nonce"
    const val HEADER_SIGNATURE = "X-Cheby-Signature"
    const val NONCE_BYTES = 24
    private val EMPTY_BYTES = ByteArray(0)
    private val PREFIX = "CHEBY-POP-1".toByteArray(Charsets.US_ASCII)
    private val HEX = "0123456789abcdef".toCharArray()

    fun sign(
        key: DeviceProofKeyHandle,
        method: String,
        rawTarget: ByteArray,
        body: ByteArray,
        bearerToken: String?,
        timestampEpochSeconds: Long = System.currentTimeMillis() / 1_000,
        nonceBytes: ByteArray = ByteArray(NONCE_BYTES).also(SecureRandom()::nextBytes),
    ): DeviceProofHeaders {
        if (timestampEpochSeconds < 0 || timestampEpochSeconds.toString().length > 12) {
            throw DeviceProofException("Invalid proof timestamp")
        }
        if (nonceBytes.size != NONCE_BYTES) throw DeviceProofException("Invalid proof nonce")
        val timestamp = timestampEpochSeconds.toString()
        val nonce = Base64.getEncoder().encodeToString(nonceBytes)
        val canonical = canonicalBytes(
            method = method,
            rawTarget = rawTarget,
            timestamp = timestamp,
            nonce = nonce,
            body = body,
            bearerToken = bearerToken.orEmpty(),
        )
        val providerSignature = try {
            key.signSha256(canonical)
        } catch (error: DeviceIdentityUnavailableException) {
            throw error
        } catch (error: Exception) {
            throw DeviceIdentityUnavailableException(error)
        }
        val signature = try {
            EcdsaP256Der.normalizeLowS(providerSignature)
        } catch (error: DeviceProofException) {
            throw DeviceIdentityUnavailableException(error)
        }
        val verified = try {
            key.verifySha256(canonical, signature)
        } catch (error: Exception) {
            throw DeviceIdentityUnavailableException(error)
        }
        if (!verified) throw DeviceIdentityUnavailableException()
        return DeviceProofHeaders(
            version = VERSION,
            timestamp = timestamp,
            nonce = nonce,
            signature = Base64.getEncoder().encodeToString(signature),
        )
    }

    fun canonicalBytes(
        method: String,
        rawTarget: ByteArray,
        timestamp: String,
        nonce: String,
        body: ByteArray,
        bearerToken: String,
    ): ByteArray {
        val canonicalMethod = method.uppercase(Locale.US)
        if (
            canonicalMethod.isEmpty() ||
            canonicalMethod.any { it !in 'A'..'Z' } ||
            timestamp.isEmpty() ||
            timestamp.any { it !in '0'..'9' } ||
            (timestamp.length > 1 && timestamp[0] == '0') ||
            nonce.isEmpty() ||
            nonce.any { it.code !in 0x21..0x7e || it == '\r' || it == '\n' }
        ) {
            throw DeviceProofException("Invalid canonical proof field")
        }
        validateOriginForm(rawTarget)
        return listOf(
            PREFIX,
            canonicalMethod.toByteArray(Charsets.US_ASCII),
            rawTarget,
            timestamp.toByteArray(Charsets.US_ASCII),
            nonce.toByteArray(Charsets.US_ASCII),
            sha256Hex(body),
            sha256Hex(bearerToken.toByteArray(Charsets.UTF_8)),
        ).joinWithLf()
    }

    fun originForm(target: okhttp3.HttpUrl): ByteArray {
        val raw = buildString {
            append(target.encodedPath.ifEmpty { "/" })
            target.encodedQuery?.let { append('?').append(it) }
        }
        if (raw.any { it.code !in 0x21..0x7e }) {
            throw DeviceProofException("Invalid origin-form request target")
        }
        val value = raw.toByteArray(Charsets.US_ASCII)
        validateOriginForm(value)
        return value
    }

    fun emptyBody(): ByteArray = EMPTY_BYTES

    internal fun containsInvalidHeaderByte(value: String): Boolean =
        value.isEmpty() || value.any { it.code !in 0x21..0x7e }

    private fun validateOriginForm(rawTarget: ByteArray) {
        if (
            rawTarget.isEmpty() ||
            rawTarget[0] != '/'.code.toByte() ||
            rawTarget.any { byte ->
                val value = byte.toInt() and 0xff
                value !in 0x21..0x7e || value == '#'.code
            }
        ) {
            throw DeviceProofException("Invalid origin-form request target")
        }
    }

    private fun sha256Hex(value: ByteArray): ByteArray {
        val digest = MessageDigest.getInstance("SHA-256").digest(value)
        val result = CharArray(digest.size * 2)
        digest.forEachIndexed { index, byte ->
            val unsigned = byte.toInt() and 0xff
            result[index * 2] = HEX[unsigned ushr 4]
            result[index * 2 + 1] = HEX[unsigned and 0x0f]
        }
        return String(result).toByteArray(Charsets.US_ASCII)
    }

    private fun List<ByteArray>.joinWithLf(): ByteArray {
        val result = ByteArray(sumOf(ByteArray::size) + size - 1)
        var offset = 0
        forEachIndexed { index, value ->
            if (index > 0) result[offset++] = '\n'.code.toByte()
            value.copyInto(result, offset)
            offset += value.size
        }
        return result
    }
}

internal object EcdsaP256Der {
    val ORDER: BigInteger = BigInteger(
        "FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551",
        16,
    )
    val HALF_ORDER: BigInteger = ORDER.shiftRight(1)

    data class Components(val r: BigInteger, val s: BigInteger)

    fun parseStrict(signature: ByteArray, requireLowS: Boolean = false): Components {
        if (signature.size !in 8..72) throw DeviceProofException("Invalid ECDSA DER length")
        var offset = 0
        fun readByte(): Int {
            if (offset >= signature.size) throw DeviceProofException("Truncated ECDSA DER")
            return signature[offset++].toInt() and 0xff
        }
        if (readByte() != 0x30) throw DeviceProofException("Invalid ECDSA DER sequence")
        val sequenceLength = readByte()
        if (sequenceLength and 0x80 != 0 || sequenceLength != signature.size - 2) {
            throw DeviceProofException("Non-canonical ECDSA DER sequence length")
        }
        fun readInteger(): BigInteger {
            if (readByte() != 0x02) throw DeviceProofException("Invalid ECDSA DER integer")
            val length = readByte()
            if (length !in 1..33 || offset + length > signature.size) {
                throw DeviceProofException("Invalid ECDSA DER integer length")
            }
            val bytes = signature.copyOfRange(offset, offset + length)
            offset += length
            if ((bytes[0].toInt() and 0x80) != 0) {
                throw DeviceProofException("Negative ECDSA DER integer")
            }
            if (
                bytes.size > 1 &&
                bytes[0] == 0.toByte() &&
                (bytes[1].toInt() and 0x80) == 0
            ) {
                throw DeviceProofException("Non-minimal ECDSA DER integer")
            }
            return BigInteger(1, bytes)
        }
        val r = readInteger()
        val s = readInteger()
        if (offset != signature.size) throw DeviceProofException("Trailing ECDSA DER data")
        if (r < BigInteger.ONE || r >= ORDER || s < BigInteger.ONE || s >= ORDER) {
            throw DeviceProofException("ECDSA value outside P-256 range")
        }
        if (requireLowS && s > HALF_ORDER) throw DeviceProofException("High-S ECDSA signature")
        return Components(r, s)
    }

    fun normalizeLowS(signature: ByteArray): ByteArray {
        val (r, providerS) = parseStrict(signature)
        val s = if (providerS > HALF_ORDER) ORDER.subtract(providerS) else providerS
        return encode(r, s)
    }

    fun encode(r: BigInteger, s: BigInteger): ByteArray {
        if (r < BigInteger.ONE || r >= ORDER || s < BigInteger.ONE || s >= ORDER) {
            throw DeviceProofException("ECDSA value outside P-256 range")
        }
        fun integer(value: BigInteger): ByteArray {
            val encoded = value.toByteArray()
            return byteArrayOf(0x02.toByte(), encoded.size.toByte()) + encoded
        }
        val rDer = integer(r)
        val sDer = integer(s)
        val length = rDer.size + sDer.size
        return byteArrayOf(0x30.toByte(), length.toByte()) + rDer + sDer
    }
}
