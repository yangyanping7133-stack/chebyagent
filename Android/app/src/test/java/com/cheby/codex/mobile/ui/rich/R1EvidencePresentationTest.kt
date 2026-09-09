package com.cheby.codex.mobile.ui.rich

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class R1EvidencePresentationTest {
    @Test
    fun `line count handles empty single and multiline evidence`() {
        assertEquals(0, evidenceLineCount(""))
        assertEquals(1, evidenceLineCount("one"))
        assertEquals(3, evidenceLineCount("one\ntwo\nthree"))
    }

    @Test
    fun `long single line evidence can still be expanded`() {
        assertFalse(evidenceNeedsExpansion("short", 4))
        assertTrue(evidenceNeedsExpansion("x".repeat(481), 4))
        assertTrue(evidenceNeedsExpansion("1\n2\n3\n4\n5", 4))
    }

    @Test
    fun `collapsed preview bounds semantics and preserves Unicode code points`() {
        val lines = (1..40).joinToString("\n") { "line $it" }
        val collapsed = evidencePreviewText(lines, maxLines = 4, maxCodePoints = 480)
        val unicode = evidencePreviewText("🤖".repeat(20), maxLines = 4, maxCodePoints = 5)

        assertEquals(4, evidenceLineCount(collapsed))
        assertTrue(collapsed.endsWith("…"))
        assertEquals(5, unicode.codePointCount(0, unicode.length) - 2) // space plus ellipsis are the marker
        assertFalse(unicode.contains('\uFFFD'))
    }

    @Test
    fun `rich card avoids duplicate container semantics while controls keep labels`() {
        val source = locateRichMessageCard().readText()

        assertFalse(source.contains(".semantics {"))
        assertFalse(source.contains("contentDescription = message"))
        assertTrue(source.contains("Text(if (expanded) \"收起详情\" else \"展开详情\")"))
        assertTrue(source.contains("contentDescription = null")) // decorative icon only
    }

    private fun locateRichMessageCard(): File {
        var current = File(System.getProperty("user.dir") ?: ".").absoluteFile
        repeat(8) {
            val candidate = File(
                current,
                "app/src/main/java/com/cheby/codex/mobile/ui/rich/RichMessageCard.kt",
            )
            if (candidate.isFile) return candidate
            current = current.parentFile ?: return@repeat
        }
        throw AssertionError("RichMessageCard.kt not found from test working directory")
    }
}
