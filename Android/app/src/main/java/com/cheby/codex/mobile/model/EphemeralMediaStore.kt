package com.cheby.codex.mobile.model

import java.util.UUID

/**
 * Process-private storage for media bytes that arrived through a trusted Gateway or local input.
 *
 * [MediaBlock] keeps only an opaque identifier, so neither a filesystem path nor a data URI can
 * cross the display model. Tool results can be repopulated during thread recovery. Original local
 * inputs degrade to their readable fallback after process death rather than exposing a private path.
 */
internal object EphemeralMediaStore {
    data class Asset(
        val mediaType: String,
        val bytes: ByteArray,
    )

    private data class StoredAsset(
        val mediaType: String,
        val bytes: ByteArray,
    )

    private val assets = LinkedHashMap<String, StoredAsset>(16, 0.75f, true)
    private var totalBytes = 0

    @Synchronized
    fun put(mediaType: String, source: ByteArray): String? =
        putInternal("local-mcp", mediaType, source)

    @Synchronized
    fun putOriginal(mediaType: String, source: ByteArray): String? =
        putInternal("local-source", mediaType, source)

    private fun putInternal(prefix: String, mediaType: String, source: ByteArray): String? {
        if (source.isEmpty() || source.size > MAX_ASSET_BYTES) return null
        if (!matchesMediaType(mediaType, source)) return null

        val storedBytes = source.copyOf()
        while (assets.isNotEmpty() && totalBytes + storedBytes.size > MAX_TOTAL_BYTES) {
            val eldest = assets.entries.iterator().next()
            assets.remove(eldest.key)
            totalBytes -= eldest.value.bytes.size
            eldest.value.bytes.fill(0)
        }
        if (totalBytes + storedBytes.size > MAX_TOTAL_BYTES) {
            storedBytes.fill(0)
            return null
        }

        val assetId = "$prefix-${UUID.randomUUID()}"
        assets[assetId] = StoredAsset(mediaType, storedBytes)
        totalBytes += storedBytes.size
        return assetId
    }

    @Synchronized
    fun get(assetId: String): Asset? = assets[assetId]?.let { stored ->
        Asset(stored.mediaType, stored.bytes.copyOf())
    }

    @Synchronized
    internal fun clearForTest() {
        assets.values.forEach { it.bytes.fill(0) }
        assets.clear()
        totalBytes = 0
    }

    private fun matchesMediaType(mediaType: String, bytes: ByteArray): Boolean = when (mediaType) {
        "image/png" -> bytes.size >= PNG_SIGNATURE.size + PNG_IEND.size &&
            bytes.startsWith(PNG_SIGNATURE) && bytes.endsWith(PNG_IEND)
        "image/jpeg" -> bytes.size >= 4 &&
            bytes[0] == 0xff.toByte() && bytes[1] == 0xd8.toByte() &&
            bytes[bytes.lastIndex - 1] == 0xff.toByte() && bytes[bytes.lastIndex] == 0xd9.toByte()
        "image/webp" -> bytes.size >= 12 &&
            bytes.startsWith(WEBP_RIFF) &&
            bytes.copyOfRange(8, 12).contentEquals(WEBP_SIGNATURE)
        else -> false
    }

    private fun ByteArray.startsWith(prefix: ByteArray): Boolean =
        size >= prefix.size && prefix.indices.all { index -> this[index] == prefix[index] }

    private fun ByteArray.endsWith(suffix: ByteArray): Boolean {
        if (size < suffix.size) return false
        val offset = size - suffix.size
        return suffix.indices.all { index -> this[offset + index] == suffix[index] }
    }

    const val MAX_ASSET_BYTES = 8 * 1024 * 1024
    private const val MAX_TOTAL_BYTES = 96 * 1024 * 1024
    private val PNG_SIGNATURE = byteArrayOf(
        0x89.toByte(), 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
    )
    private val PNG_IEND = byteArrayOf(
        0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4e, 0x44,
        0xae.toByte(), 0x42, 0x60, 0x82.toByte(),
    )
    private val WEBP_RIFF = byteArrayOf(0x52, 0x49, 0x46, 0x46)
    private val WEBP_SIGNATURE = byteArrayOf(0x57, 0x45, 0x42, 0x50)
}
