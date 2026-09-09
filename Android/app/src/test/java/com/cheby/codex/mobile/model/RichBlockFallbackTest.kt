package com.cheby.codex.mobile.model

import org.junit.Assert.assertEquals
import org.junit.Test

class RichBlockFallbackTest {
    @Test
    fun `unknown block uses readable fallback`() {
        val block = UnknownBlock(
            id = "future-widget",
            originalType = "future.chart.v9",
            fallbackText = "分析结果已生成，请升级后查看完整图表。",
        )

        assertEquals("分析结果已生成，请升级后查看完整图表。", block.readableText())
    }

    @Test
    fun `blank unknown fallback remains safe and readable`() {
        val block = UnknownBlock(
            id = "malformed",
            originalType = "untrusted-html",
            fallbackText = "",
        )

        assertEquals("暂不支持的消息内容", block.readableText())
    }
}
