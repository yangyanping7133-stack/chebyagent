package com.cheby.codex.mobile.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SafeDisplayTextTest {
    @Test
    fun `UTF8 budget never splits multibyte code point`() {
        val result = SafeDisplayText.sanitize("界".repeat(1_000), 128)

        assertTrue(result.toByteArray(Charsets.UTF_8).size <= 128)
        assertTrue(result.endsWith("…已截断"))
        assertFalse(result.contains('\uFFFD'))
    }

    @Test
    fun `display boundary removes terminal and direction controls`() {
        val raw = "\u001B]0;private\u0007\u001B[31mred\u001B[0m\u061C\u202E\u2062\u2066\u200B\u0085visible"
        val result = SafeDisplayText.sanitize(raw, 256)

        assertEquals("redvisible", result)
    }

    @Test
    fun `operational output hides workspace paths and internal identifiers`() {
        val result = SafeDisplayText.sanitize(
            "changed /workspace/project/Main.kt in 123e4567-e89b-12d3-a456-426614174000",
            256,
        )

        assertFalse(result.contains("/workspace/"))
        assertFalse(result.contains("123e4567"))
        assertTrue(result.contains("<private path>"))
        assertTrue(result.contains("<internal id>"))
    }

    @Test
    fun `private image markdown becomes a readable caption instead of broken syntax`() {
        val result = SafeDisplayText.sanitize(
            "已完成\n![Captured Yandex Maps café detail](/root/private/cafe.png)",
            512,
        )

        assertEquals("已完成\nCaptured Yandex Maps café detail", result)
        assertFalse(result.contains("!["))
        assertFalse(result.contains("<private path>"))
    }

    @Test
    fun `exact private roots and loopback URLs are hidden`() {
        val result = SafeDisplayText.sanitize(
            "/workspace http://localhost:3111/private ws://127.0.0.1:8765/events https://[::1]/debug",
            512,
        )

        assertFalse(result.contains("/workspace"))
        assertFalse(result.contains("localhost"))
        assertFalse(result.contains("127.0.0.1"))
        assertFalse(result.contains("::1"))
        assertTrue(result.contains("<private path>"))
        assertTrue(result.contains("<internal url>"))
    }

    @Test
    fun `strict sanitizer rejects oversize UTF8 instead of truncating`() {
        assertEquals(null, SafeDisplayText.sanitizeStrict("汉".repeat(43_600), 65_536))
        assertFalse(SafeDisplayText.exceedsUtf8Limit("界界", 6))
        assertTrue(SafeDisplayText.exceedsUtf8Limit("界界", 5))
    }

    @Test
    fun `common token families are redacted at the final display boundary`() {
        val jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.abcdefghijklmnopqrstuvwxyz"
        val github = "ghp_" + "abcdefghijklmnopqrstuvwxyz1234567890"
        val result = SafeDisplayText.sanitize(
            "token=plain-secret-value authorization:BasicSecret client_secret=client-value $jwt $github",
            1_024,
        )

        assertFalse(result.contains("plain-secret-value"))
        assertFalse(result.contains("BasicSecret"))
        assertFalse(result.contains("client-value"))
        assertFalse(result.contains("eyJhbGci"))
        assertFalse(result.contains("ghp_"))
        assertTrue(result.contains("<redacted>"))
        assertTrue(result.contains("<redacted token>"))
    }

    @Test
    fun `private network and internal DNS URLs are hidden`() {
        val result = SafeDisplayText.sanitize(
            listOf(
                "http://10.1.2.3/admin",
                "https://172.31.4.5:8443/api",
                "ws://192.168.1.20/events",
                "http://169.254.169.254/latest",
                "https://gateway.corp.internal/private",
                "http://service.local/debug",
                "https://[fe80::1]/link",
                "https://[fd00::1234]/ula",
            ).joinToString(" "),
            2_048,
        )

        listOf("10.1.2.3", "172.31.4.5", "192.168.1.20", "169.254.169.254", ".internal", ".local", "fe80", "fd00")
            .forEach { assertFalse("Leaked internal host $it", result.contains(it, ignoreCase = true)) }
        assertEquals(8, Regex("<internal url>").findAll(result).count())
    }

    @Test
    fun `file labels never expose parent paths and invalid names use safe fallback`() {
        assertEquals(
            "ThreadViewModel.kt",
            SafeDisplayText.fileLabel("/company/private/app/ThreadViewModel.kt", "文件"),
        )
        assertEquals(
            "ContractV1Codec.kt",
            SafeDisplayText.fileLabel("C:\\work\\secret\\ContractV1Codec.kt", "文件"),
        )
        assertEquals("文件", SafeDisplayText.fileLabel("/company/private/app/", "文件"))
        assertEquals("文件", SafeDisplayText.fileLabel("\u0000\u202E", "文件"))
    }
}
