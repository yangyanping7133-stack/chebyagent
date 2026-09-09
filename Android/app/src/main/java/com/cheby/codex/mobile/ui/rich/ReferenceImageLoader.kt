package com.cheby.codex.mobile.ui.rich

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.LruCache
import java.util.Locale
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response

/** Loads bounded, supplemental reference images without accepting model-provided URLs. */
internal object ReferenceImageLoader {
    private const val MAX_IMAGE_BYTES = 4L * 1024L * 1024L
    private const val MAX_API_BYTES = 512L * 1024L
    private val client = OkHttpClient.Builder().build()
    private val json = Json { ignoreUnknownKeys = true }
    private val cache = object : LruCache<String, Bitmap>(16 * 1024 * 1024) {
        override fun sizeOf(key: String, value: Bitmap): Int = value.byteCount
    }

    fun load(query: String): Bitmap? {
        val cacheKey = query.trim().lowercase(Locale.ROOT)
        if (cacheKey.isBlank()) return null
        synchronized(cache) { cache.get(cacheKey)?.let { return it } }
        val url = findCommonsThumbnail(query) ?: findWikipediaThumbnail(query) ?: return null
        if (url.scheme != "https" || url.host != "upload.wikimedia.org") return null
        val request = Request.Builder()
            .url(url)
            .header("User-Agent", "ChebyCodex-Mobile/0.4.5 reference-images")
            .build()
        val bitmap = runCatching {
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) return@use null
                decodeBounded(readBounded(response, MAX_IMAGE_BYTES) ?: return@use null)
            }
        }.getOrNull() ?: return null
        synchronized(cache) { cache.put(cacheKey, bitmap) }
        return bitmap
    }

    private fun findCommonsThumbnail(query: String): HttpUrl? {
        val api = "https://commons.wikimedia.org/w/api.php".toHttpUrl().newBuilder()
            .addQueryParameter("action", "query")
            .addQueryParameter("generator", "search")
            .addQueryParameter("gsrsearch", query)
            .addQueryParameter("gsrnamespace", "6")
            .addQueryParameter("gsrlimit", "1")
            .addQueryParameter("prop", "imageinfo")
            .addQueryParameter("iiprop", "url")
            .addQueryParameter("iiurlwidth", "640")
            .addQueryParameter("format", "json")
            .addQueryParameter("formatversion", "2")
            .addQueryParameter("origin", "*")
            .build()
        return queryApi(api) { root ->
            val first = root["query"]?.jsonObject?.get("pages")?.jsonArray
                ?.firstOrNull()?.jsonObject ?: return@queryApi null
            val title = first["title"]?.jsonPrimitive?.content ?: return@queryApi null
            if (!hasMeaningfulTokenOverlap(query, title)) return@queryApi null
            first["imageinfo"]?.jsonArray?.firstOrNull()?.jsonObject
                ?.get("thumburl")?.jsonPrimitive?.content?.toHttpUrl()
        }
    }

    private fun findWikipediaThumbnail(query: String): HttpUrl? {
        val language = if (query.any { it in '\u0400'..'\u04FF' }) "ru" else "en"
        val api = "https://$language.wikipedia.org/w/api.php".toHttpUrl().newBuilder()
            .addQueryParameter("action", "query")
            .addQueryParameter("generator", "search")
            .addQueryParameter("gsrsearch", query)
            .addQueryParameter("gsrlimit", "1")
            .addQueryParameter("prop", "pageimages")
            .addQueryParameter("piprop", "thumbnail")
            .addQueryParameter("pithumbsize", "640")
            .addQueryParameter("format", "json")
            .addQueryParameter("formatversion", "2")
            .addQueryParameter("origin", "*")
            .build()
        return queryApi(api) { root ->
            val first = root["query"]?.jsonObject?.get("pages")?.jsonArray
                ?.firstOrNull()?.jsonObject ?: return@queryApi null
            val title = first["title"]?.jsonPrimitive?.content ?: return@queryApi null
            if (!hasMeaningfulTokenOverlap(query, title)) return@queryApi null
            first["thumbnail"]?.jsonObject?.get("source")?.jsonPrimitive?.content?.toHttpUrl()
        }
    }

    private fun queryApi(api: HttpUrl, project: (kotlinx.serialization.json.JsonObject) -> HttpUrl?): HttpUrl? {
        val request = Request.Builder()
            .url(api)
            .header("User-Agent", "ChebyCodex-Mobile/0.4.5 reference-images")
            .build()
        return runCatching {
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) return@use null
                val body = readBounded(response, MAX_API_BYTES)?.toString(Charsets.UTF_8) ?: return@use null
                project(json.parseToJsonElement(body).jsonObject)
            }
        }.getOrNull()
    }

    private fun readBounded(response: Response, maxBytes: Long): ByteArray? {
        val body = response.body ?: return null
        val declared = body.contentLength()
        if (declared > maxBytes) return null
        val source = body.source()
        source.request(maxBytes + 1L)
        if (source.buffer.size > maxBytes) return null
        return source.readByteArray()
    }

    private fun hasMeaningfulTokenOverlap(query: String, title: String): Boolean {
        fun tokens(value: String): Set<String> = value.lowercase(Locale.ROOT)
            .split(Regex("[^\\p{L}\\p{N}]+"))
            .filter { it.length >= 4 }
            .toSet()
        return tokens(query).intersect(tokens(title)).isNotEmpty()
    }

    private fun decodeBounded(bytes: ByteArray): Bitmap? {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null
        var sample = 1
        while (
            bounds.outWidth / sample > 1_024 ||
            bounds.outHeight / sample > 1_024 ||
            (bounds.outWidth.toLong() / sample) * (bounds.outHeight.toLong() / sample) > 2_000_000L
        ) {
            sample *= 2
        }
        return BitmapFactory.decodeByteArray(
            bytes,
            0,
            bytes.size,
            BitmapFactory.Options().apply { inSampleSize = sample },
        )
    }
}
