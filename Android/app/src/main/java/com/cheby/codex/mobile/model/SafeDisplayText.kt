package com.cheby.codex.mobile.model

/**
 * Final display boundary for operational evidence. The Gateway remains responsible for
 * authoritative redaction; this keeps a malformed or older Gateway from exposing common
 * credentials and private machine paths in the mobile UI.
 */
internal object SafeDisplayText {
    const val MAX_FALLBACK = 2_048
    const val MAX_LABEL = 256
    const val MAX_DETAIL = 2_048
    const val MAX_TERMINAL = 16_384
    const val MAX_DIFF = 8_192

    private val bearer = Regex("(?i)\\bBearer\\s+[A-Za-z0-9._~+/=-]{8,}")
    private val ansiEscape = Regex("\u001B(?:\\[[0-?]*[ -/]*[@-~]|\\][^\u0007]*(?:\u0007|\u001B\\\\))")
    private val unsafeControl = Regex("[\\u0000-\\u0008\\u000B\\u000C\\u000E-\\u001F\\u007F-\\u009F]")
    // Unicode format controls include bidi overrides/isolates, Arabic Letter Mark and invisible
    // operators. None are needed in operational evidence and several can visually spoof content.
    private val unsafeFormat = Regex("\\p{Cf}")
    private val namedSecret = Regex(
        "(?i)\\b(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|" +
            "authorization|password|passwd|secret|token)" +
            "\\s*[:=]\\s*[\\\"']?[^\\s\\\"',;]{4,}",
    )
    private val openAiKey = Regex("\\bsk-[A-Za-z0-9_-]{8,}")
    private val jwt = Regex("\\beyJ[A-Za-z0-9_-]{5,}\\.[A-Za-z0-9_-]{5,}\\.[A-Za-z0-9_-]{5,}\\b")
    private val githubToken = Regex(
        "\\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\\b",
    )
    private val internalUuid = Regex(
        "(?i)\\b[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\\b",
    )
    private val privatePosixPath = Regex(
        "(?<!https:)(?<!http:)(?<![/A-Za-z0-9.])/(?:Users|home|root|private|var|tmp|etc|opt|srv|" +
            "workspace|workspaces|app|data|mnt|storage|sdcard|Applications|Library)" +
            "(?:/[^\\s\\\"'<>|)]+)?(?=$|[\\s\\\"'<>|,;:)])",
    )
    private val privateWindowsPath = Regex(
        "(?i)\\b[A-Z]:\\\\[^\\r\\n\\\"'<>|)]+",
    )
    private val fileUri = Regex("(?i)\\bfile:///?[^\\s\\\"'<>|)]+")
    private val privateImageMarkdown = Regex(
        "!\\[([^]\\r\\n]{0,256})]\\([^\\r\\n)]*<private path>[^\\r\\n)]*\\)",
    )
    private const val INTERNAL_HOST = "(?:localhost|127(?:\\.\\d{1,3}){3}|10(?:\\.\\d{1,3}){3}|" +
        "172\\.(?:1[6-9]|2\\d|3[01])(?:\\.\\d{1,3}){2}|192\\.168(?:\\.\\d{1,3}){2}|" +
        "169\\.254(?:\\.\\d{1,3}){2}|(?:[A-Za-z0-9-]+\\.)*(?:[A-Za-z0-9-]+\\.(?:internal|local))|" +
        "\\[(?:::1|[fF][eE]80:[0-9A-Fa-f:%._~-]*|[fF][cCdD][0-9A-Fa-f]{2}:[0-9A-Fa-f:]*)])"
    private val internalUrl = Regex(
        "(?i)\\b(?:https?|wss?)://$INTERNAL_HOST(?::\\d{1,5})?(?:/[^\\s\\\"'<>|]*)?",
    )
    private val internalHostPort = Regex(
        "(?i)(?<![A-Za-z0-9.-])$INTERNAL_HOST(?::\\d{1,5})(?:/[^\\s\\\"'<>|]*)?",
    )

    fun sanitize(value: String, maxLength: Int): String = sanitizeResult(value, maxLength).text

    fun sanitizeStrict(value: String, maxLength: Int): String? =
        sanitizeResult(value, maxLength).takeUnless(SanitizedText::truncated)?.text

    fun exceedsUtf8Limit(value: String, maxBytes: Int): Boolean {
        if (value.length > maxBytes) return true
        var usedBytes = 0
        var index = 0
        while (index < value.length) {
            val codePoint = value.codePointAt(index)
            usedBytes += when {
                codePoint <= 0x7f -> 1
                codePoint <= 0x7ff -> 2
                codePoint <= 0xffff -> 3
                else -> 4
            }
            if (usedBytes > maxBytes) return true
            index += Character.charCount(codePoint)
        }
        return false
    }

    private fun sanitizeResult(value: String, maxLength: Int): SanitizedText {
        val redacted = value
            .replace(ansiEscape, "")
            .replace(unsafeControl, "")
            .replace(unsafeFormat, "")
            .replace(bearer, "Bearer <redacted>")
            .replace(namedSecret) { match ->
                val name = match.groupValues[1].replace('_', ' ').replace('-', ' ')
                "$name=<redacted>"
            }
            .replace(openAiKey, "<redacted key>")
            .replace(jwt, "<redacted token>")
            .replace(githubToken, "<redacted token>")
            .replace(internalUuid, "<internal id>")
            .replace(internalUrl, "<internal url>")
            .replace(internalHostPort, "<internal url>")
            .replace(fileUri, "<private path>")
            .replace(privateWindowsPath, "<private path>")
            .replace(privatePosixPath, "<private path>")
            .replace(privateImageMarkdown) { match ->
                match.groupValues[1].trim().ifBlank { "图片" }
            }
        if (!exceedsUtf8Limit(redacted, maxLength)) return SanitizedText(redacted, truncated = false)
        val markerBytes = TRUNCATION_MARKER.utf8Size()
        val contentBudget = (maxLength - markerBytes).coerceAtLeast(0)
        val builder = StringBuilder()
        var index = 0
        var usedBytes = 0
        while (index < redacted.length) {
            val codePoint = redacted.codePointAt(index)
            val text = String(Character.toChars(codePoint))
            val codePointBytes = text.utf8Size()
            if (usedBytes + codePointBytes > contentBudget) break
            builder.append(text)
            usedBytes += codePointBytes
            index += Character.charCount(codePoint)
        }
        return SanitizedText(builder.append(TRUNCATION_MARKER).toString(), truncated = true)
    }

    fun append(current: String, delta: String, maxLength: Int): Pair<String, Boolean> {
        if (current.endsWith(TRUNCATION_MARKER)) return current to true
        val combined = sanitize(current + delta, maxLength)
        return combined to combined.endsWith(TRUNCATION_MARKER)
    }

    /**
     * File evidence is descriptive, never a path browser. Keep only the final path segment even
     * when an older Gateway puts a full path in `label`/`fileLabel` instead of `path`.
     */
    fun fileLabel(value: String, fallback: String): String =
        safeBasename(value) ?: safeBasename(fallback) ?: "文件"

    private fun safeBasename(value: String): String? {
        val normalized = value.trim()
        if (normalized.isEmpty() || normalized.endsWith('/') || normalized.endsWith('\\')) return null
        val basename = normalized.substringAfterLast('/').substringAfterLast('\\')
        return sanitize(basename, MAX_LABEL).trim().takeIf(String::isNotEmpty)
    }

    private const val TRUNCATION_MARKER = "\n…已截断"

    private data class SanitizedText(val text: String, val truncated: Boolean)

    private fun String.utf8Size(): Int = toByteArray(Charsets.UTF_8).size
}
