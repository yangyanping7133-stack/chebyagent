package com.cheby.codex.mobile.gateway

import java.math.BigInteger
import java.security.KeyFactory
import java.security.MessageDigest
import java.security.Signature
import java.security.spec.X509EncodedKeySpec
import java.util.Base64
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.HttpUrl.Companion.toHttpUrl
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class DeviceProofV1Test {
    private val golden by lazy {
        val resource = requireNotNull(javaClass.classLoader?.getResource("device_proof_v1_golden.json"))
        Json.parseToJsonElement(resource.readText()).jsonObject
    }

    @Test
    fun `golden canonical bytes and supplied low-S signature interoperate`() {
        val canonical = goldenCanonical()

        assertEquals(goldenString("canonicalSha256"), canonical.sha256Hex())
        assertEquals(goldenString("bodySha256"), goldenString("body").toByteArray().sha256Hex())
        assertTrue(verifyGolden(canonical, goldenString("signatureLowS")))
        assertTrue(EcdsaP256Der.parseStrict(goldenBytes("signatureLowS"), requireLowS = true).s <= EcdsaP256Der.HALF_ORDER)
    }

    @Test
    fun `canonical request preserves encoded query order repeated parameters and exact body`() {
        val target = "https://gateway.example${goldenString("rawTarget")}".toHttpUrl()
        assertArrayEquals(goldenString("rawTarget").toByteArray(), DeviceProofV1.originForm(target))

        val canonical = DeviceProofV1.canonicalBytes(
            method = "post",
            rawTarget = DeviceProofV1.originForm(target),
            timestamp = goldenString("timestamp"),
            nonce = goldenString("nonce"),
            body = goldenString("body").toByteArray(),
            bearerToken = goldenString("bearerToken"),
        )

        assertArrayEquals(goldenCanonical(), canonical)
        assertFalse(
            canonical.contentEquals(
                DeviceProofV1.canonicalBytes(
                    "POST",
                    "/v1/auth/refresh?a=two+words&a=%E4%B8%AD&z=%2F".toByteArray(),
                    goldenString("timestamp"),
                    goldenString("nonce"),
                    goldenString("body").toByteArray(),
                    "",
                ),
            ),
        )
    }

    @Test
    fun `sign emits fresh 24-byte standard Base64 nonce low-S DER and redacts proof text`() {
        val key = TestDeviceProofKey.generate()
        val first = DeviceProofV1.sign(key, "GET", "/v1/threads".toByteArray(), byteArrayOf(), "token")
        val second = DeviceProofV1.sign(key, "GET", "/v1/threads".toByteArray(), byteArrayOf(), "token")

        assertEquals(24, Base64.getDecoder().decode(first.nonce).size)
        assertEquals(first.nonce, Base64.getEncoder().encodeToString(Base64.getDecoder().decode(first.nonce)))
        assertTrue(EcdsaP256Der.parseStrict(Base64.getDecoder().decode(first.signature), true).s <= EcdsaP256Der.HALF_ORDER)
        assertFalse(first.nonce == second.nonce)
        assertEquals("DeviceProofHeaders(redacted)", first.toString())
        assertFalse(first.toString().contains(first.signature))
    }

    @Test
    fun `known high-S DER normalizes exactly and parser rejects it when low-S is required`() {
        val high = goldenBytes("signatureHighS")

        assertArrayEquals(goldenBytes("signatureNormalized"), EcdsaP256Der.normalizeLowS(high))
        expectProofFailure { EcdsaP256Der.parseStrict(high, requireLowS = true) }
        EcdsaP256Der.parseStrict(goldenBytes("signatureNormalized"), requireLowS = true)
    }

    @Test
    fun `strict DER rejects malformed non-minimal trailing and out-of-range signatures`() {
        val order = EcdsaP256Der.ORDER.toByteArray()
        val malformed = listOf(
            byteArrayOf(),
            byteArrayOf(0x31, 0x06, 0x02, 0x01, 0x01, 0x02, 0x01, 0x01),
            byteArrayOf(0x30, 0x81.toByte(), 0x06, 0x02, 0x01, 0x01, 0x02, 0x01, 0x01),
            byteArrayOf(0x30, 0x07, 0x02, 0x01, 0x01, 0x02, 0x01, 0x01, 0x00),
            byteArrayOf(0x30, 0x06, 0x02, 0x01, 0x80.toByte(), 0x02, 0x01, 0x01),
            byteArrayOf(0x30, 0x07, 0x02, 0x02, 0x00, 0x01, 0x02, 0x01, 0x01),
            byteArrayOf(0x30, 0x06, 0x02, 0x01, 0x00, 0x02, 0x01, 0x01),
            byteArrayOf(0x30, (order.size + 5).toByte(), 0x02, order.size.toByte()) +
                order + byteArrayOf(0x02, 0x01, 0x01),
        )

        malformed.forEach { signature -> expectProofFailure { EcdsaP256Der.parseStrict(signature) } }
    }

    @Test
    fun `golden proof fails verification after target body or bearer tampering`() {
        val canonical = goldenCanonical()
        assertTrue(verifyGolden(canonical, goldenString("signatureLowS")))
        listOf(
            canonical + 0x00,
            DeviceProofV1.canonicalBytes(
                "POST",
                goldenString("rawTarget").replace("z=%2F", "z=%2f").toByteArray(),
                goldenString("timestamp"),
                goldenString("nonce"),
                goldenString("body").toByteArray(),
                "",
            ),
            DeviceProofV1.canonicalBytes(
                "POST",
                goldenString("rawTarget").toByteArray(),
                goldenString("timestamp"),
                goldenString("nonce"),
                "{}".toByteArray(),
                "",
            ),
            DeviceProofV1.canonicalBytes(
                "POST",
                goldenString("rawTarget").toByteArray(),
                goldenString("timestamp"),
                goldenString("nonce"),
                goldenString("body").toByteArray(),
                "unexpected-token",
            ),
        ).forEach { assertFalse(verifyGolden(it, goldenString("signatureLowS"))) }
    }

    @Test
    fun `canonical fields reject header injection invalid timestamp nonce and request target`() {
        val validTarget = "/v1/threads".toByteArray()
        expectProofFailure { DeviceProofV1.canonicalBytes("G\nET", validTarget, "1", "nonce", byteArrayOf(), "") }
        expectProofFailure { DeviceProofV1.canonicalBytes("GET", validTarget, "01", "nonce", byteArrayOf(), "") }
        expectProofFailure { DeviceProofV1.canonicalBytes("GET", validTarget, "1", "bad\rnonce", byteArrayOf(), "") }
        expectProofFailure { DeviceProofV1.canonicalBytes("GET", "https://host/v1".toByteArray(), "1", "nonce", byteArrayOf(), "") }
        expectProofFailure { DeviceProofV1.canonicalBytes("GET", "/v1#fragment".toByteArray(), "1", "nonce", byteArrayOf(), "") }
    }

    @Test
    fun `malformed provider signature is treated as unavailable device identity`() {
        val malformedProvider = object : DeviceProofKeyHandle {
            override fun publicKeySpkiDer(): ByteArray = byteArrayOf()
            override fun signSha256(message: ByteArray): ByteArray = byteArrayOf(0x30, 0x00)
            override fun verifySha256(message: ByteArray, signatureDer: ByteArray): Boolean = false
        }

        val error = runCatching {
            DeviceProofV1.sign(
                malformedProvider,
                "GET",
                "/v1/threads".toByteArray(),
                byteArrayOf(),
                "token",
            )
        }.exceptionOrNull()

        assertTrue(error is DeviceIdentityUnavailableException)
    }

    private fun goldenCanonical(): ByteArray = DeviceProofV1.canonicalBytes(
        method = goldenString("method"),
        rawTarget = goldenString("rawTarget").toByteArray(Charsets.US_ASCII),
        timestamp = goldenString("timestamp"),
        nonce = goldenString("nonce"),
        body = goldenString("body").toByteArray(Charsets.UTF_8),
        bearerToken = goldenString("bearerToken"),
    )

    private fun verifyGolden(canonical: ByteArray, signatureBase64: String): Boolean {
        val publicKey = KeyFactory.getInstance("EC").generatePublic(
            X509EncodedKeySpec(goldenBytes("publicKeySpki")),
        )
        return Signature.getInstance("SHA256withECDSA").run {
            initVerify(publicKey)
            update(canonical)
            verify(Base64.getDecoder().decode(signatureBase64))
        }
    }

    private fun goldenString(name: String): String = golden.getValue(name).jsonPrimitive.content

    private fun goldenBytes(name: String): ByteArray = Base64.getDecoder().decode(goldenString(name))

    private fun ByteArray.sha256Hex(): String = MessageDigest.getInstance("SHA-256")
        .digest(this)
        .joinToString("") { "%02x".format(it.toInt() and 0xff) }

    private fun expectProofFailure(block: () -> Unit) {
        try {
            block()
            fail("Expected DeviceProofException")
        } catch (_: DeviceProofException) {
            // Expected.
        }
    }
}
