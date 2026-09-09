package com.cheby.codex.mobile.ui.rich

import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SafeMarkdownTest {
    @Test
    fun `safe subset parses headings lists and fenced code`() {
        val blocks = parseSafeMarkdown(
            """# Delivery

- First
- Second

```kotlin
val result = "safe"
```""",
        )

        val heading = blocks[0] as SafeMarkdownBlock.Text
        val list = blocks[1] as SafeMarkdownBlock.ListItems
        val code = blocks[2] as SafeMarkdownBlock.Code
        assertEquals(1, heading.level)
        assertEquals("Delivery", heading.content.text)
        assertEquals(listOf("First", "Second"), list.items.map { it.text })
        assertFalse(list.ordered)
        assertEquals("kotlin", code.language)
        assertEquals("val result = \"safe\"", code.content)
    }

    @Test
    fun `safe inline parser applies emphasis and exposes link label and target as plain text`() {
        val parsed = parseSafeInline(
            "**bold** *italic* `code` [safe](https://example.com/a) " +
                "[blocked](javascript:alert) ![remote](https://example.com/image.png)",
        )

        assertEquals(
            "bold italic code safe (https://example.com/a) blocked (javascript:alert) " +
                "![remote](https://example.com/image.png)",
            parsed.text,
        )
        assertTrue(parsed.spanStyles.any { it.item.fontWeight == FontWeight.Bold })
        assertTrue(parsed.spanStyles.any { it.item.fontStyle == FontStyle.Italic })
        assertTrue(parsed.spanStyles.any { it.item.fontFamily != null })
        assertTrue(parsed.getStringAnnotations(0, parsed.length).isEmpty())
    }

    @Test
    fun `public localhost private and executable link targets are all non interactive`() {
        val parsed = parseSafeInline(
            "[public](https://example.com) [local](http://localhost:3111) " +
                "[private](http://192.168.1.2) [script](javascript:alert)",
        )

        assertEquals(
            "public (https://example.com) local (http://localhost:3111) " +
                "private (http://192.168.1.2) script (javascript:alert)",
            parsed.text,
        )
        assertTrue(parsed.getStringAnnotations(0, parsed.length).isEmpty())
    }

    @Test
    fun `unclosed fence and html stay readable plain text`() {
        val source = "<script>alert('text only')</script>\n```kotlin\nval unfinished = true"
        val blocks = parseSafeMarkdown(source)

        assertEquals(source, blocks.joinToString("\n") { (it as SafeMarkdownBlock.Text).content.text })
        assertNull((blocks.first() as SafeMarkdownBlock.Text).level)
    }

    @Test
    fun `pathological inline marker volume degrades to plain text`() {
        val source = "[".repeat(2_000) + "readable"

        val parsed = parseSafeInline(source)

        assertEquals(source, parsed.text)
        assertTrue(parsed.spanStyles.isEmpty())
        assertTrue(parsed.getStringAnnotations(0, parsed.length).isEmpty())
    }

    @Test
    fun `64 KiB heading bomb stays within block and rendered child limits`() {
        val source = buildBomb("# heading\n")

        val parsed = parseSafeMarkdown(source)

        assertTrue(parsed.size <= MAX_MARKDOWN_BLOCKS)
        assertTrue(renderedChildren(parsed) <= MAX_MARKDOWN_RENDERED_CHILDREN)
        assertTrue((parsed.last() as SafeMarkdownBlock.Text).content.text.startsWith(MARKDOWN_LIMIT_NOTICE))
        assertTrue((parsed.last() as SafeMarkdownBlock.Text).content.text.contains("# heading"))
    }

    @Test
    fun `64 KiB list bomb falls back before composing unbounded rows`() {
        val source = buildBomb("- item\n")

        val parsed = parseSafeMarkdown(source)

        assertTrue(parsed.size <= MAX_MARKDOWN_BLOCKS)
        assertTrue(renderedChildren(parsed) <= MAX_MARKDOWN_RENDERED_CHILDREN)
        assertTrue((parsed.last() as SafeMarkdownBlock.Text).content.text.startsWith(MARKDOWN_LIMIT_NOTICE))
        assertTrue((parsed.last() as SafeMarkdownBlock.Text).content.text.contains("- item"))
    }

    private fun buildBomb(line: String): String = buildString {
        while (length + line.length <= 65_536) append(line)
    }

    private fun renderedChildren(blocks: List<SafeMarkdownBlock>): Int =
        1 + blocks.sumOf { it.estimatedComposeNodes() }
}
