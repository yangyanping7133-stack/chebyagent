package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.model.InteractiveCollectionBlock
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class InteractiveCollectionContractV1CodecTest {
    private val codec = ContractV1Codec()

    @Test
    fun `remote rich contract decodes interactive collection`() {
        val message = codec.decodeMessages(frame()).single()
        val block = message.blocks["menu"] as InteractiveCollectionBlock

        assertEquals("完整菜单", block.title)
        assertEquals("Борщ", block.groups.single().items.single().primary)
        assertEquals(590L, block.groups.single().items.single().unitPriceMinor)
        assertEquals("份", block.quantityUnit)
        assertEquals("Итого", block.presentationTotalLabel)
    }

    @Test
    fun `remote rich contract rejects duplicate selectable item ids`() {
        val duplicateItem = ITEM.replace("Борщ", "Уха")
        assertThrows(ProtocolV1Exception::class.java) {
            codec.decodeMessages(frame(items = "$ITEM,$duplicateItem"))
        }
    }

    private fun frame(items: String = ITEM): String = """
        {
          "messages": [{
            "messageId": "message-1",
            "role": "assistant",
            "state": "completed",
            "revision": 1,
            "schema": "cheby.rich-message/1.0",
            "fallback": {"text": "完整菜单，共 1 个可选项"},
            "createdAt": "2026-08-09T00:00:00Z",
            "updatedAt": "2026-08-09T00:00:00Z",
            "turnId": "turn-1",
            "rootBlockIds": ["menu"],
            "blocks": {
              "menu": {
                "type": "interactiveCollection",
                "title": "完整菜单",
                "subtitle": "不清楚的内容会标记",
                "confirmLabel": "确定点菜",
                "quantityUnit": "份",
                "presentationTitle": "Мой заказ",
                "presentationSubtitle": "Пожалуйста, примите заказ.",
                "presentationTotalLabel": "Итого",
                "presentationNote": "",
                "currencyLabel": "₽",
                "minorUnitScale": 1,
                "groups": [{
                  "id": "soups",
                  "title": "汤",
                  "items": [$items]
                }],
                "fallbackText": "完整菜单，共 1 个可选项"
              }
            }
          }]
        }
    """.trimIndent()

    private companion object {
        val ITEM = """
            {
              "id": "borscht",
              "primary": "Борщ",
              "secondary": "红菜汤",
              "detail": "牛肉红菜汤",
              "unitPriceMinor": 590,
              "priceLabel": "590 ₽",
              "imageQuery": "Russian borscht soup",
              "imageLabel": "菜品示意图，不代表本店实际摆盘",
              "imageKind": "representative",
              "uncertain": false
            }
        """.trimIndent()
    }
}
