package com.cheby.codex.mobile.model

import java.util.Base64
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

class EphemeralMediaStoreTest {
    @After
    fun clearStore() = EphemeralMediaStore.clearForTest()

    @Test
    fun `accepted image stays process private behind an opaque identifier`() {
        val bytes = Base64.getDecoder().decode(PNG_BASE64)
        val assetId = EphemeralMediaStore.put("image/png", bytes)
        bytes.fill(0)

        assertNotNull(assetId)
        val asset = EphemeralMediaStore.get(requireNotNull(assetId))
        assertEquals("image/png", asset?.mediaType)
        assertArrayEquals(Base64.getDecoder().decode(PNG_BASE64), asset?.bytes)
        asset?.bytes?.fill(0)
    }

    @Test
    fun `mismatched unsupported and oversized image payloads fail closed`() {
        val png = Base64.getDecoder().decode(PNG_BASE64)

        assertNull(EphemeralMediaStore.put("image/jpeg", png))
        assertNull(EphemeralMediaStore.put("image/gif", png))
        assertNull(EphemeralMediaStore.put("image/png", ByteArray(EphemeralMediaStore.MAX_ASSET_BYTES + 1)))
        png.fill(0)
    }

    private companion object {
        const val PNG_BASE64 =
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    }
}
