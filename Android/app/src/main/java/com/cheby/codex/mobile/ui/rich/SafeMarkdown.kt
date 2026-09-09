package com.cheby.codex.mobile.ui.rich

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.cheby.codex.mobile.ui.theme.BlueSoft
import com.cheby.codex.mobile.ui.theme.Divider
import com.cheby.codex.mobile.ui.theme.Ink
import com.cheby.codex.mobile.ui.theme.Muted
import com.cheby.codex.mobile.ui.theme.SignalBlue

/**
 * Bounded Markdown with an explicit sanitized HTML-poster fence; ordinary code remains text.
 */
internal sealed interface SafeMarkdownBlock {
    data class Poster(val document: PosterDocument) : SafeMarkdownBlock
    data class Text(
        val level: Int?,
        val content: AnnotatedString,
    ) : SafeMarkdownBlock

    data class ListItems(
        val ordered: Boolean,
        val items: List<AnnotatedString>,
    ) : SafeMarkdownBlock

    data class Code(
        val language: String?,
        val content: String,
    ) : SafeMarkdownBlock
}

internal fun SafeMarkdownBlock.estimatedComposeNodes(): Int = when (this) {
    is SafeMarkdownBlock.Poster -> 3
    is SafeMarkdownBlock.Text -> 1
    is SafeMarkdownBlock.Code -> 3 // frame column, language label, code text
    is SafeMarkdownBlock.ListItems -> 1 + items.size * 4 // list column + row/text/spacer/text per item
}

@Composable
internal fun SafeMarkdownText(
    value: String,
    modifier: Modifier = Modifier,
) {
    val blocks = remember(value) { parseSafeMarkdown(value) }
    Column(
        modifier = modifier.fillMaxWidth(),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        blocks.forEach { block ->
            when (block) {
                is SafeMarkdownBlock.Poster -> PosterView(block.document)
                is SafeMarkdownBlock.Text -> SafeRichText(
                    value = block.content,
                    style = headingStyle(block.level),
                )
                is SafeMarkdownBlock.ListItems -> Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    block.items.forEachIndexed { index, item ->
                        Row(modifier = Modifier.fillMaxWidth()) {
                            Text(
                                text = if (block.ordered) "${index + 1}." else "•",
                                color = SignalBlue,
                                style = MaterialTheme.typography.bodyLarge,
                                fontWeight = FontWeight.SemiBold,
                            )
                            Spacer(Modifier.width(10.dp))
                            SafeRichText(
                                value = item,
                                style = MaterialTheme.typography.bodyLarge,
                                modifier = Modifier.weight(1f),
                            )
                        }
                    }
                }
                is SafeMarkdownBlock.Code -> Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .background(MaterialTheme.colorScheme.surfaceVariant, RoundedCornerShape(10.dp))
                        .border(1.dp, Divider, RoundedCornerShape(10.dp))
                        .padding(12.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    Text(
                        text = block.language?.uppercase() ?: "代码",
                        color = Muted,
                        style = MaterialTheme.typography.labelMedium.copy(fontFamily = FontFamily.Monospace),
                        fontWeight = FontWeight.SemiBold,
                    )
                    Text(
                        text = block.content,
                        color = Ink,
                        style = MaterialTheme.typography.bodyMedium.copy(fontFamily = FontFamily.Monospace),
                    )
                }
            }
        }
    }
}

@Composable
private fun SafeRichText(
    value: AnnotatedString,
    style: TextStyle,
    modifier: Modifier = Modifier,
) {
    Text(text = value, modifier = modifier, color = Ink, style = style)
}

@Composable
private fun headingStyle(level: Int?): TextStyle = when (level) {
    1 -> MaterialTheme.typography.headlineSmall.copy(fontWeight = FontWeight.Bold)
    2 -> MaterialTheme.typography.titleLarge.copy(fontWeight = FontWeight.Bold)
    3 -> MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.Bold)
    in 4..6 -> MaterialTheme.typography.bodyLarge.copy(fontWeight = FontWeight.Bold)
    else -> MaterialTheme.typography.bodyLarge
}

internal fun parseSafeMarkdown(source: String): List<SafeMarkdownBlock> {
    if (source.isEmpty()) return listOf(SafeMarkdownBlock.Text(null, AnnotatedString("")))
    val lines = source.split('\n')
    val blocks = mutableListOf<SafeMarkdownBlock>()
    var renderedChildren = MARKDOWN_ROOT_RENDER_NODES
    var index = 0
    while (index < lines.size) {
        if (lines[index].isBlank()) {
            index += 1
            continue
        }
        if (
            blocks.size >= MAX_MARKDOWN_BLOCKS - 1 ||
            renderedChildren >= MAX_MARKDOWN_RENDERED_CHILDREN - 1
        ) {
            blocks += markdownLimitFallback(lines, index)
            break
        }

        val fence = FENCE_OPEN.matchEntire(lines[index])
        if (fence != null) {
            val close = ((index + 1) until lines.size).firstOrNull { FENCE_CLOSE.matches(lines[it]) }
            if (close == null) {
                blocks += SafeMarkdownBlock.Text(null, AnnotatedString(lines.drop(index).joinToString("\n")))
                break
            }
            if (renderedChildren + 3 >= MAX_MARKDOWN_RENDERED_CHILDREN) {
                blocks += markdownLimitFallback(lines, index)
                break
            }
            val language = fence.groupValues[1].trim().takeIf(SAFE_LANGUAGE::matches)
            val content = lines.subList(index + 1, close).joinToString("\n")
            val poster = if (language == "html" && blocks.none { it is SafeMarkdownBlock.Poster }) parsePoster(content) else null
            blocks += if (poster != null) SafeMarkdownBlock.Poster(poster)
                else SafeMarkdownBlock.Code(language, content)
            renderedChildren += blocks.last().estimatedComposeNodes()
            index = close + 1
            continue
        }

        val heading = HEADING.matchEntire(lines[index])
        if (heading != null) {
            blocks += SafeMarkdownBlock.Text(
                level = heading.groupValues[1].length,
                content = parseSafeInline(heading.groupValues[2]),
            )
            renderedChildren += blocks.last().estimatedComposeNodes()
            index += 1
            continue
        }

        val bullet = BULLET.matchEntire(lines[index])
        val ordered = ORDERED.matchEntire(lines[index])
        if (bullet != null || ordered != null) {
            val isOrdered = ordered != null
            val items = mutableListOf<AnnotatedString>()
            val listStart = index
            var listExceeded = false
            while (index < lines.size) {
                val match = if (isOrdered) {
                    ORDERED.matchEntire(lines[index])
                } else {
                    BULLET.matchEntire(lines[index])
                }
                if (match == null) break
                if (
                    items.size >= MAX_MARKDOWN_LIST_ITEMS ||
                    renderedChildren + 1 + (items.size + 1) * 4 >= MAX_MARKDOWN_RENDERED_CHILDREN
                ) {
                    listExceeded = true
                    break
                }
                items += parseSafeInline(match.groupValues.last())
                index += 1
            }
            if (listExceeded) {
                blocks += markdownLimitFallback(lines, listStart)
                break
            }
            blocks += SafeMarkdownBlock.ListItems(isOrdered, items)
            renderedChildren += blocks.last().estimatedComposeNodes()
            continue
        }

        val paragraph = mutableListOf<String>()
        while (index < lines.size && lines[index].isNotBlank() && !isBlockStart(lines[index])) {
            paragraph += lines[index]
            index += 1
        }
        if (paragraph.isEmpty()) {
            // A malformed/unsupported block opener is displayed literally instead of being interpreted.
            paragraph += lines[index]
            index += 1
        }
        blocks += SafeMarkdownBlock.Text(null, parseSafeInline(paragraph.joinToString("\n")))
        renderedChildren += blocks.last().estimatedComposeNodes()
    }
    return blocks.ifEmpty { listOf(SafeMarkdownBlock.Text(null, AnnotatedString(""))) }
}

internal fun parseSafeInline(source: String): AnnotatedString {
    if (source.count { it in INLINE_MARKERS } > MAX_INLINE_MARKERS) return AnnotatedString(source)
    return buildAnnotatedString {
        fun appendRange(start: Int, end: Int, depth: Int) {
            var cursor = start
            while (cursor < end) {
                if (source[cursor] == '\\' && cursor + 1 < end && source[cursor + 1] in MARKDOWN_ESCAPES) {
                    append(source[cursor + 1])
                    cursor += 2
                    continue
                }

                if (source.startsWith("![", cursor)) {
                    val imageEnd = inlineTargetEnd(source, cursor + 1, end)
                    if (imageEnd != null) {
                        append(source.substring(cursor, imageEnd))
                        cursor = imageEnd
                        continue
                    }
                }

                if (source[cursor] == '`') {
                    val close = source.indexOf('`', cursor + 1).takeIf { it in (cursor + 1) until end }
                    if (close != null) {
                        val spanStart = length
                        append(source.substring(cursor + 1, close))
                        addStyle(
                            SpanStyle(fontFamily = FontFamily.Monospace, background = BlueSoft),
                            spanStart,
                            length,
                        )
                        cursor = close + 1
                        continue
                    }
                }

                if (source[cursor] == '[') {
                    val closeLabel = source.indexOf(']', cursor + 1).takeIf { it in (cursor + 1) until end }
                    val closeTarget = closeLabel?.takeIf { it + 1 < end && source[it + 1] == '(' }
                        ?.let { source.indexOf(')', it + 2).takeIf { close -> close in (it + 2) until end } }
                    if (closeLabel != null && closeTarget != null) {
                        val url = source.substring(closeLabel + 2, closeTarget)
                        append(source.substring(cursor + 1, closeLabel))
                        append(" (")
                        append(url)
                        append(')')
                        cursor = closeTarget + 1
                        continue
                    }
                }

                val strongDelimiter = when {
                    source.startsWith("**", cursor) -> "**"
                    source.startsWith("__", cursor) -> "__"
                    else -> null
                }
                if (strongDelimiter != null && depth < MAX_INLINE_DEPTH) {
                    val close = source.indexOf(strongDelimiter, cursor + 2)
                        .takeIf { it in (cursor + 2) until end }
                    if (close != null) {
                        val spanStart = length
                        appendRange(cursor + 2, close, depth + 1)
                        addStyle(SpanStyle(fontWeight = FontWeight.Bold), spanStart, length)
                        cursor = close + 2
                        continue
                    }
                }

                if (source[cursor] == '*' || source[cursor] == '_') {
                    val delimiter = source[cursor]
                    val close = source.indexOf(delimiter, cursor + 1)
                        .takeIf { it in (cursor + 1) until end }
                    if (close != null && depth < MAX_INLINE_DEPTH) {
                        val spanStart = length
                        appendRange(cursor + 1, close, depth + 1)
                        addStyle(SpanStyle(fontStyle = FontStyle.Italic), spanStart, length)
                        cursor = close + 1
                        continue
                    }
                }

                append(source[cursor])
                cursor += 1
            }
        }
        appendRange(0, source.length, 0)
    }
}

private fun inlineTargetEnd(source: String, labelStart: Int, end: Int): Int? {
    val closeLabel = source.indexOf(']', labelStart + 1).takeIf { it in (labelStart + 1) until end }
        ?: return null
    if (closeLabel + 1 >= end || source[closeLabel + 1] != '(') return null
    val closeTarget = source.indexOf(')', closeLabel + 2).takeIf { it in (closeLabel + 2) until end }
        ?: return null
    return closeTarget + 1
}

private fun markdownLimitFallback(lines: List<String>, start: Int): SafeMarkdownBlock.Text =
    SafeMarkdownBlock.Text(
        level = null,
        content = AnnotatedString(MARKDOWN_LIMIT_NOTICE + "\n" + lines.drop(start).joinToString("\n")),
    )

private fun isBlockStart(value: String): Boolean =
    FENCE_OPEN.matches(value) || HEADING.matches(value) || BULLET.matches(value) || ORDERED.matches(value)

internal const val MAX_MARKDOWN_BLOCKS = 128
internal const val MAX_MARKDOWN_LIST_ITEMS = 40
internal const val MAX_MARKDOWN_RENDERED_CHILDREN = 192
internal const val MARKDOWN_LIMIT_NOTICE = "[其余 Markdown 已按安全上限以纯文本显示]"
private const val MARKDOWN_ROOT_RENDER_NODES = 1
private const val MAX_INLINE_DEPTH = 8
private const val MAX_INLINE_MARKERS = 256
private val MARKDOWN_ESCAPES = setOf('\\', '`', '*', '_', '[', ']', '(', ')', '#', '+', '-', '!')
private val INLINE_MARKERS = setOf('\\', '`', '*', '_', '[', ']', '(', ')', '!')
private val FENCE_OPEN = Regex("^ {0,3}```([^`]*)$")
private val FENCE_CLOSE = Regex("^ {0,3}```\\s*$")
private val HEADING = Regex("^(#{1,6})\\s+(.+)$")
private val BULLET = Regex("^ {0,3}[-*+]\\s+(.+)$")
private val ORDERED = Regex("^ {0,3}\\d{1,6}\\.\\s+(.+)$")
private val SAFE_LANGUAGE = Regex("[A-Za-z0-9_+#.-]{1,24}")
