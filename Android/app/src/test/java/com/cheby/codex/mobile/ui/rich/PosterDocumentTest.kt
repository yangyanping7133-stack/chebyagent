package com.cheby.codex.mobile.ui.rich

import java.net.InetAddress
import org.junit.Assert.*
import org.junit.Test

class PosterDocumentTest {
    @Test fun `visual shortlists support ten choices while retaining an image bound`() {
        fun cards(count: Int) = "<article>" + (1..count).joinToString("") { index ->
            """<section class="card"><h2>Choice $index</h2><img src="https://example.com/photo-$index.jpg" alt="Observed photo $index"/><a href="https://example.com/listing/$index">Open source</a></section>"""
        } + "</article>"
        val ten = requireNotNull(parsePoster(cards(10)))
        assertEquals(10, ten.imageUrls.size)
        assertEquals(10, ten.links.size)
        assertTrue(ten.text.contains("Choice 10"))
        assertEquals(12, requireNotNull(parsePoster(cards(12))).imageUrls.size)
        assertNull(parsePoster(cards(13)))
    }

    @Test fun `inline poster height uses physical pixels without truncating the bottom`() {
        assertEquals(1260, posterHeightPx(420, 3f))
        assertEquals(527, posterHeightPx(421, 1.25f))
        assertEquals(1, posterHeightPx(1, 0.5f))
        assertNull(posterHeightPx(0, 3f))
        assertNull(posterHeightPx(420, 0f))
        assertNull(posterHeightPx(420, Float.NaN))
        assertNull(posterHeightPx(420, Float.POSITIVE_INFINITY))
    }

    @Test fun `poster preserves content photos and escaped branch links`() {
        val poster = requireNotNull(parsePoster("""<article><h1>附近的好去处</h1><section class="card recommended"><img src="cheby-image:phone-20260907T100000Z-ab12cd34.jpg" alt="照片页截图"/><h2>Observed name</h2><p>步行时间待核实</p><a class="button" href="https://yandex.ru/maps/org/123/?a=1&amp;b=2">查看门店</a></section></article>"""))
        assertEquals(setOf("https://yandex.ru/maps/org/123/?a=1&b=2"), poster.links)
        assertTrue(poster.imageUrls.single().startsWith("https://poster.invalid/artifacts/phone-"))
        assertTrue(poster.text.contains("步行时间待核实"))
        assertTrue(poster.html.contains("Content-Security-Policy"))
    }

    @Test fun `unsafe markup and schemes cannot become a poster`() {
        listOf("<script>alert(1)</script>", "<iframe/>",
            "<!DOCTYPE x [<!ENTITY x SYSTEM 'file:///etc/passwd'>]><p>&x;</p>",
            """<a href="javascript:alert(1)">x</a>""", """<img src="file:///secret"/><p>x</p>""",
            """<img src="cheby-image:../secret"/><p>x</p>""",
            """<a href="https://localhost/">x</a>""", """<a href="https://127.0.0.1/">x</a>""",
            """<a href="https://u:p@example.com">x</a>""").forEach { assertNull(it, parsePoster(it)) }
    }

    @Test fun `event and style attributes are stripped`() {
        val poster = requireNotNull(parsePoster("""<p onclick="evil()" style="background:url(https://example.com)">Hello</p>"""))
        assertFalse(poster.html.contains("onclick"))
        assertFalse(poster.html.contains("evil()"))
        assertFalse(poster.html.contains("background:url"))
    }

    @Test fun `DNS guard blocks private and relay ranges`() {
        listOf("127.0.0.1", "10.2.3.4", "172.16.1.2", "192.168.1.1", "169.254.169.254", "100.64.0.1", "::1", "fc00::1").forEach {
            assertFalse(it, isPublicPosterAddress(InetAddress.getByName(it)))
        }
        assertTrue(isPublicPosterAddress(InetAddress.getByName("8.8.8.8")))
    }

    @Test fun `only a complete safe html fence renders`() {
        assertTrue(parseSafeMarkdown("```html\n<p>Real result</p>\n```").single() is SafeMarkdownBlock.Poster)
        assertTrue(parseSafeMarkdown("```html\n<script>unsafe()</script>\n```").single() is SafeMarkdownBlock.Code)
        assertTrue(parseSafeMarkdown("```kotlin\n<p>code</p>\n```").single() is SafeMarkdownBlock.Code)
        assertTrue(parseSafeMarkdown("```html\n<p>unfinished</p>").single() is SafeMarkdownBlock.Text)
    }
}
