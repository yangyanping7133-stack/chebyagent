package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.model.BilingualCardBlock
import com.cheby.codex.mobile.model.ComparisonBlock
import com.cheby.codex.mobile.model.InteractiveCollectionBlock
import com.cheby.codex.mobile.model.ReferenceGalleryBlock
import com.cheby.codex.mobile.model.TextBlock
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class LocalStructuredOutputCodecTest {
    @Test
    fun `generic task response decodes every supported result primitive`() {
        val blocks = LocalStructuredOutputCodec.decode(TASK_RESPONSE, "answer")
        assertNotNull(blocks)
        assertEquals(6, blocks?.size)
        assertTrue(blocks?.get(0) is TextBlock)
        assertTrue(blocks?.get(1) is TextBlock)
        assertTrue(blocks?.get(2) is ReferenceGalleryBlock)
        assertTrue(blocks?.get(3) is ComparisonBlock)
        assertTrue(blocks?.get(4) is InteractiveCollectionBlock)
        assertTrue(blocks?.get(5) is BilingualCardBlock)

        val comparison = blocks?.filterIsInstance<ComparisonBlock>()?.single()
        assertEquals(listOf("候选甲", "候选乙"), comparison?.items?.map { it.primary })
        val selection = blocks?.filterIsInstance<InteractiveCollectionBlock>()?.single()
        assertEquals("件", selection?.quantityUnit)
        assertEquals("Total", selection?.presentationTotalLabel)
        assertEquals(120L, selection?.groups?.single()?.items?.single()?.unitPriceMinor)
    }

    @Test
    fun `malformed or unbounded task response fails closed`() {
        assertNull(LocalStructuredOutputCodec.decode(TASK_RESPONSE.replace("120", "-2"), "answer"))
        assertNull(LocalStructuredOutputCodec.decode("{}", "answer"))
        assertNull(LocalStructuredOutputCodec.decode(TASK_RESPONSE.replace("\"version\": 1", "\"version\": 2"), "answer"))
    }

    @Test
    fun `bounded legacy comparison is projected instead of rendered as raw json`() {
        val blocks = LocalStructuredOutputCodec.decode(LEGACY_COMPARISON, "legacy")
        val comparison = blocks?.single() as? ComparisonBlock

        assertNotNull(comparison)
        assertEquals(listOf("Kofe-Da!", "Tseh85"), comparison?.items?.map { it.primary })
        assertEquals(listOf(true, false), comparison?.items?.map { it.recommended })
        assertEquals("评分 5.0（256）", comparison?.items?.first()?.facts?.first()?.value)
        assertNull(LocalStructuredOutputCodec.decode("""{"type":"selection"}""", "legacy"))
    }

    @Test
    fun `schema chooses presentation by goal and contains no vertical names`() {
        val schema = LocalStructuredOutputCodec.outputSchema().toString()
        assertTrue(schema.contains("Choose blocks by the user's goal"))
        assertTrue(schema.contains("\"comparison\""))
        assertTrue(schema.contains("\"selection\""))
        assertTrue(schema.contains("\"bilingual\""))
        assertTrue(schema.contains("\"gallery\""))
        assertFalse(schema.contains("menu", ignoreCase = true))
        assertFalse(schema.contains("food", ignoreCase = true))
        assertFalse(schema.contains("Yandex", ignoreCase = true))
        assertFalse(schema.contains("Russian", ignoreCase = true))
    }

    @Test
    fun `large multi page selection accepts 256 items and rejects 257`() {
        val accepted = LocalStructuredOutputCodec.decode(selectionResponse(256), "large")
            ?.filterIsInstance<InteractiveCollectionBlock>()
            ?.single()

        assertEquals(256, accepted?.groups?.sumOf { it.items.size })
        assertNull(LocalStructuredOutputCodec.decode(selectionResponse(257), "too-large"))
    }

    private fun selectionResponse(itemCount: Int): String {
        val groups = (0 until itemCount).chunked(32).mapIndexed { groupIndex, indexes ->
            val items = indexes.joinToString(",") { index ->
                """{
                  "primary":"Блюдо $index",
                  "secondary":"菜品 $index",
                  "detail":"来源可见",
                  "unitPriceMinor":$index,
                  "priceLabel":"$index",
                  "imageQuery":"",
                  "imageLabel":"",
                  "imageKind":"none",
                  "uncertain":false
                }""".trimIndent()
            }
            """{"title":"第 ${groupIndex + 1} 组","items":[$items]}"""
        }.joinToString(",")
        return """{
          "version":1,
          "summary":"",
          "blocks":[{
            "type":"selection",
            "title":"完整菜单",
            "subtitle":"",
            "confirmLabel":"确认选择",
            "quantityUnit":"份",
            "presentationTitle":"Ваш выбор",
            "presentationSubtitle":"",
            "presentationTotalLabel":"Итого",
            "presentationNote":"",
            "currencyLabel":"₽",
            "minorUnitScale":1,
            "groups":[$groups]
          }]
        }""".trimIndent()
    }

    private companion object {
        val LEGACY_COMPARISON = """
            {
              "type": "comparison",
              "primary": "Kofe-Da!",
              "secondary": "评分最高",
              "detail": "即将关门",
              "facts": ["评分 5.0（256）", "11 min · 6.1 km"],
              "badge": "4 分钟后关门",
              "imageQuery": "",
              "imageLabel": "",
              "alternatives": [{
                "name": "Tseh85",
                "secondary": "更稳妥的替代",
                "detail": "营业至 22:00",
                "facts": ["评分 4.5（596）"],
                "badge": "营业至 22:00"
              }]
            }
        """.trimIndent()

        val TASK_RESPONSE = """
            {
              "version": 1,
              "summary": "以下内容可继续查看和选择。",
              "blocks": [
                {
                  "type": "text",
                  "text": "普通说明仍然使用安全 Markdown。",
                  "markdown": true
                },
                {
                  "type": "gallery",
                  "title": "参考图片",
                  "subtitle": "图片仅帮助理解。",
                  "items": [{
                    "title": "对象示意",
                    "caption": "不代表现场实物。",
                    "imageQuery": "generic object reference",
                    "imageLabel": "参考图片，不代表实际对象",
                    "uncertain": false
                  }]
                },
                {
                  "type": "comparison",
                  "title": "候选比较",
                  "subtitle": "根据已看到的信息比较。",
                  "selectLabel": "选择此项",
                  "items": [
                    {
                      "primary": "候选甲",
                      "secondary": "Option A",
                      "detail": "距离更近。",
                      "badge": "较近",
                      "facts": [{"label": "距离", "value": "800 米"}],
                      "imageQuery": "",
                      "imageLabel": "",
                      "recommended": true,
                      "uncertain": false
                    },
                    {
                      "primary": "候选乙",
                      "secondary": "Option B",
                      "detail": "开放时间更长。",
                      "badge": "",
                      "facts": [{"label": "距离", "value": "1.2 公里"}],
                      "imageQuery": "",
                      "imageLabel": "",
                      "recommended": false,
                      "uncertain": false
                    }
                  ]
                },
                {
                  "type": "selection",
                  "title": "完整可选内容",
                  "subtitle": "不清楚的内容会标记。",
                  "confirmLabel": "确认选择",
                  "quantityUnit": "件",
                  "presentationTitle": "Selected items",
                  "presentationSubtitle": "Please review",
                  "presentationTotalLabel": "Total",
                  "presentationNote": "",
                  "currencyLabel": "¥",
                  "minorUnitScale": 1,
                  "groups": [{
                    "title": "第一组",
                    "items": [{
                      "primary": "选项 A",
                      "secondary": "选择项甲",
                      "detail": "来源中可见的信息。",
                      "unitPriceMinor": 120,
                      "priceLabel": "120 ¥",
                      "imageQuery": "generic object A",
                      "imageLabel": "参考图片，不代表实际对象",
                      "imageKind": "representative",
                      "uncertain": false
                    }]
                  }]
                },
                {
                  "type": "bilingual",
                  "titleOriginal": "Information",
                  "titleChinese": "信息说明",
                  "contextOriginal": "Please read the following.",
                  "contextChinese": "请阅读以下内容。",
                  "showLabel": "展示给对方",
                  "lines": [{
                    "original": "First line",
                    "translation": "第一行",
                    "note": ""
                  }]
                }
              ]
            }
        """.trimIndent()
    }
}
