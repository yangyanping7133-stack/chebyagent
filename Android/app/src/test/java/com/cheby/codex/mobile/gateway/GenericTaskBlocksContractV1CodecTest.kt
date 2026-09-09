package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.model.BilingualCardBlock
import com.cheby.codex.mobile.model.ComparisonBlock
import com.cheby.codex.mobile.model.ReferenceGalleryBlock
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class GenericTaskBlocksContractV1CodecTest {
    private val codec = ContractV1Codec()

    @Test
    fun `remote rich contract decodes generic gallery comparison and bilingual blocks`() {
        val message = codec.decodeMessages(frame()).single()

        val gallery = message.blocks.getValue("gallery") as ReferenceGalleryBlock
        assertEquals("参考内容", gallery.title)
        assertEquals("generic object reference", gallery.items.single().imageQuery)

        val comparison = message.blocks.getValue("comparison") as ComparisonBlock
        assertEquals(listOf("选项甲", "选项乙"), comparison.items.map { it.primary })
        assertEquals("800 米", comparison.items.first().facts.single().value)

        val bilingual = message.blocks.getValue("handoff") as BilingualCardBlock
        assertEquals("Information", bilingual.titleOriginal)
        assertEquals("第一行", bilingual.lines.single().translation)
    }

    @Test
    fun `remote rich contract rejects duplicate generic item ids`() {
        val duplicate = COMPARISON_ITEM_A.replace("选项甲", "重复项")

        assertThrows(ProtocolV1Exception::class.java) {
            codec.decodeMessages(frame(comparisonItems = "$COMPARISON_ITEM_A,$duplicate"))
        }
    }

    private fun frame(comparisonItems: String = "$COMPARISON_ITEM_A,$COMPARISON_ITEM_B"): String = """
        {
          "messages": [{
            "messageId": "message-1",
            "role": "assistant",
            "state": "completed",
            "revision": 1,
            "schema": "cheby.rich-message/1.0",
            "fallback": {"text": "任务结果"},
            "createdAt": "2026-08-09T00:00:00Z",
            "updatedAt": "2026-08-09T00:00:00Z",
            "turnId": "turn-1",
            "rootBlockIds": ["gallery", "comparison", "handoff"],
            "blocks": {
              "gallery": {
                "type": "referenceGallery",
                "title": "参考内容",
                "subtitle": "仅帮助理解",
                "items": [{
                  "id": "reference-a",
                  "title": "对象甲",
                  "caption": "不代表现场实物",
                  "imageQuery": "generic object reference",
                  "imageLabel": "参考图片",
                  "uncertain": false
                }],
                "fallbackText": "一个参考项"
              },
              "comparison": {
                "type": "comparison",
                "title": "候选比较",
                "subtitle": "根据可见信息",
                "selectLabel": "选择此项",
                "items": [$comparisonItems],
                "fallbackText": "两个候选项"
              },
              "handoff": {
                "type": "bilingualCard",
                "titleOriginal": "Information",
                "titleChinese": "信息说明",
                "contextOriginal": "Please read.",
                "contextChinese": "请阅读。",
                "showLabel": "展示给对方",
                "lines": [{
                  "id": "line-a",
                  "original": "First line",
                  "translation": "第一行",
                  "note": ""
                }],
                "fallbackText": "双语信息"
              }
            }
          }]
        }
    """.trimIndent()

    private companion object {
        val COMPARISON_ITEM_A = """
            {
              "id": "option-a",
              "primary": "选项甲",
              "secondary": "Option A",
              "detail": "距离更近",
              "badge": "较近",
              "facts": [{"label": "距离", "value": "800 米"}],
              "imageQuery": "",
              "imageLabel": "",
              "recommended": true,
              "uncertain": false
            }
        """.trimIndent()

        val COMPARISON_ITEM_B = """
            {
              "id": "option-b",
              "primary": "选项乙",
              "secondary": "Option B",
              "detail": "开放时间更长",
              "badge": "",
              "facts": [{"label": "距离", "value": "1.2 公里"}],
              "imageQuery": "",
              "imageLabel": "",
              "recommended": false,
              "uncertain": false
            }
        """.trimIndent()
    }
}
