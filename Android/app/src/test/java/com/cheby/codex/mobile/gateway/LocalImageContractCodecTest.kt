package com.cheby.codex.mobile.gateway

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class LocalImageContractCodecTest {
    private val codec = ContractV1Codec()

    @Test
    fun `exact local image capability is accepted and absence fails closed`() {
        val capability = codec.decodeCapabilities(CAPABILITIES).localImage

        requireNotNull(capability)
        assertEquals(1, capability.uploadVersion)
        assertEquals(setOf("image/jpeg", "image/png"), capability.mediaTypes)
        assertEquals(8_388_608, capability.maxUploadBytes)
        assertEquals(10, capability.maxImagesPerTurn)
        assertEquals(
            4,
            codec.decodeCapabilities(
                CAPABILITIES.replace("\"maxImagesPerTurn\":10", "\"maxImagesPerTurn\":4"),
            ).localImage?.maxImagesPerTurn,
        )
        assertNull(codec.decodeCapabilities("""{"v":1,"inputs":{}}""").localImage)
    }

    @Test
    fun `changed capability limits and media ordering fail closed`() {
        val changedLimit = CAPABILITIES.replace("8388608", "8388607")
        val tooManyImages = CAPABILITIES.replace("\"maxImagesPerTurn\":10", "\"maxImagesPerTurn\":11")
        val changedOrder = CAPABILITIES.replace(
            "[\"image/jpeg\",\"image/png\"]",
            "[\"image/png\",\"image/jpeg\"]",
        )

        assertTrue(runCatching { codec.decodeCapabilities(changedLimit) }.exceptionOrNull() is ProtocolV1Exception)
        assertTrue(runCatching { codec.decodeCapabilities(tooManyImages) }.exceptionOrNull() is ProtocolV1Exception)
        assertTrue(runCatching { codec.decodeCapabilities(changedOrder) }.exceptionOrNull() is ProtocolV1Exception)
    }

    @Test
    fun `upload response accepts opaque asset and rejects path shaped or oversized values`() {
        val response = """{
            "assetRef":"ast_abcdefghijklmnopqrstuvwxyz0123456789",
            "mediaType":"image/png",
            "width":640,
            "height":480,
            "byteCount":12345,
            "expiresAt":"2026-07-20T00:00:00Z"
        }""".trimIndent()

        val decoded = codec.decodeImageUpload(response)

        assertEquals("image/png", decoded.mediaType)
        assertEquals(640, decoded.width)
        assertTrue(
            runCatching {
                codec.decodeImageUpload(response.replace("ast_abcdefghijklmnopqrstuvwxyz0123456789", "/asset-staging/private"))
            }.exceptionOrNull() is ProtocolV1Exception,
        )
        assertTrue(
            runCatching { codec.decodeImageUpload(response.replace("12345", "8388609")) }
                .exceptionOrNull() is ProtocolV1Exception,
        )
    }

    companion object {
        val CAPABILITIES = """{
            "v":1,
            "inputs":{
                "localImage":{
                    "uploadVersion":1,
                    "mediaTypes":["image/jpeg","image/png"],
                    "maxUploadBytes":8388608,
                    "maxPixels":25000000,
                    "maxEdgePixels":12000,
                    "maxImagesPerTurn":10
                }
            }
        }""".trimIndent()
    }
}
