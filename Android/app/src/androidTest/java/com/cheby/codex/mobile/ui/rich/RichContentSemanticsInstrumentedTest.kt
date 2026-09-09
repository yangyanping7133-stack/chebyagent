package com.cheby.codex.mobile.ui.rich

import android.util.Base64
import androidx.compose.material3.MaterialTheme
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToIndex
import com.cheby.codex.mobile.model.BilingualCardBlock
import com.cheby.codex.mobile.model.EphemeralMediaStore
import com.cheby.codex.mobile.model.BilingualLine
import com.cheby.codex.mobile.model.ComparisonBlock
import com.cheby.codex.mobile.model.ComparisonFact
import com.cheby.codex.mobile.model.ComparisonItem
import com.cheby.codex.mobile.model.MediaBlock
import com.cheby.codex.mobile.model.InteractiveCollectionBlock
import com.cheby.codex.mobile.model.InteractiveCollectionGroup
import com.cheby.codex.mobile.model.InteractiveCollectionItem
import com.cheby.codex.mobile.model.InteractiveImageKind
import com.cheby.codex.mobile.model.MessageRole
import com.cheby.codex.mobile.model.NoticeBlock
import com.cheby.codex.mobile.model.RichBlock
import com.cheby.codex.mobile.model.RichMessage
import com.cheby.codex.mobile.model.RichMessageState
import com.cheby.codex.mobile.model.ReferenceGalleryBlock
import com.cheby.codex.mobile.model.ReferenceImageItem
import com.cheby.codex.mobile.model.TextBlock
import com.cheby.codex.mobile.model.Tone
import org.junit.Rule
import org.junit.Test

class RichContentSemanticsInstrumentedTest {
    @get:Rule
    val compose = createComposeRule()

    @Test
    fun tenUserOriginalPagesRenderAsLazyStripAndOpenFullScreen() {
        EphemeralMediaStore.clearForTest()
        val assetIds = List(10) {
            requireNotNull(EphemeralMediaStore.putOriginal(
                "image/png",
                Base64.decode(ONE_PIXEL_PNG_BASE64, Base64.DEFAULT),
            ))
        }
        try {
            setMessage(
                *buildList<RichBlock> {
                    add(TextBlock("request", "点菜", markdown = false))
                    assetIds.forEachIndexed { index, assetId ->
                        add(
                            MediaBlock(
                                "original-${index + 1}",
                                assetId,
                                caption = "原始图片 ${index + 1}",
                            ),
                        )
                    }
                }.toTypedArray(),
                role = MessageRole.USER,
            )

            compose.onNodeWithContentDescription("原始图片 1").assertIsDisplayed().performClick()
            compose.onNodeWithContentDescription("关闭原始图片预览").assertIsDisplayed().performClick()
            compose.onNodeWithTag("user_source_pages").performScrollToIndex(9)
            compose.onNodeWithContentDescription("原始图片 10").assertIsDisplayed()
            compose.onNodeWithText("原始图片暂不可用").assertDoesNotExist()
        } finally {
            EphemeralMediaStore.clearForTest()
        }
    }

    @Test
    fun mediaPreviewIsTalkBackReadableAndOpensAndClosesFullScreen() {
        val fallback = "Preview unavailable; the written result is still available."
        setMessage(MediaBlock("preview", "asset_summary_preview", caption = null, fallbackText = fallback))

        compose.onNodeWithText(fallback).assertIsDisplayed()
        compose.onNodeWithContentDescription("媒体占位：图片。$fallback")
            .assertExists()
            .performClick()
        compose.onNodeWithContentDescription("全屏媒体占位：图片。$fallback")
            .assertIsDisplayed()
        compose.onNodeWithContentDescription("关闭全屏预览")
            .assertIsDisplayed()
            .performClick()
        compose.onNodeWithContentDescription("媒体占位：图片。$fallback")
            .assertExists()
    }

    @Test
    fun markdownFlagControlsWhetherSyntaxIsInterpreted() {
        setMessage(
            TextBlock("markdown", "**bold result**", markdown = true),
            TextBlock("literal", "**literal result**", markdown = false),
        )

        compose.onNodeWithText("bold result").assertIsDisplayed()
        compose.onNodeWithText("**literal result**").assertIsDisplayed()
    }

    @Test
    fun frozenUnavailableFallbackIsVisibleAsASystemNotice() {
        val fallback = "Preview unavailable; the written result is still available."
        setMessage(
            NoticeBlock(
                id = "asset-unavailable-notice",
                title = "媒体内容不可用",
                body = fallback,
                tone = Tone.WARNING,
                fallbackText = fallback,
            ),
            role = MessageRole.SYSTEM,
        )

        compose.onNodeWithText("系统提示").assertIsDisplayed()
        compose.onNodeWithText(fallback).assertIsDisplayed()
    }

    @Test
    fun interactiveCollectionSelectsQuantityAndConfirmsOnceIntoPresentationCard() {
        setMessage(
            InteractiveCollectionBlock(
                id = "menu",
                title = "完整中文菜单",
                subtitle = "先查看完整菜单，再选择想吃的菜。",
                confirmLabel = "确定点菜",
                quantityUnit = "份",
                presentationTitle = "Мой заказ",
                presentationSubtitle = "Пожалуйста, примите заказ.",
                presentationTotalLabel = "Итого",
                presentationNote = "",
                currencyLabel = "₽",
                minorUnitScale = 1,
                groups = listOf(
                    InteractiveCollectionGroup(
                        id = "soups",
                        title = "汤",
                        items = listOf(
                            InteractiveCollectionItem(
                                id = "borscht",
                                primary = "Борщ",
                                secondary = "红菜汤",
                                detail = "牛肉红菜汤",
                                unitPriceMinor = 590,
                                priceLabel = "590 ₽",
                                imageQuery = "",
                                imageLabel = "菜品示意图，不代表本店实际摆盘",
                                imageKind = InteractiveImageKind.NONE,
                            ),
                        ),
                    ),
                ),
                fallbackText = "完整中文菜单，共 1 个可选项",
            ),
        )

        compose.onNodeWithTag("interactive_collection_open").performClick()
        compose.onNodeWithText("图片待补充").assertDoesNotExist()
        compose.onNodeWithText("红菜汤").assertIsDisplayed().performClick()
        compose.onNodeWithText("已选 1 份").assertIsDisplayed()
        compose.onNodeWithContentDescription("增加数量").performClick()
        compose.onNodeWithText("已选 2 份").assertIsDisplayed()
        compose.onNodeWithTag("interactive_confirm").performClick()
        compose.onNodeWithTag("interactive_presentation_card").assertIsDisplayed()
        compose.onNodeWithText("Мой заказ").assertIsDisplayed()
        compose.onNodeWithText("2×").assertIsDisplayed()
        compose.onNodeWithText("确定点菜").assertDoesNotExist()
    }

    @Test
    fun referenceGalleryHasReadablePlaceholderWithoutAResolvableImage() {
        setMessage(
            ReferenceGalleryBlock(
                id = "gallery",
                title = "参考内容",
                subtitle = "图片只帮助理解",
                items = listOf(
                    ReferenceImageItem(
                        id = "reference-a",
                        title = "对象甲",
                        caption = "信息尚未核实",
                        imageQuery = "",
                        imageLabel = "参考图片",
                        uncertain = true,
                    ),
                ),
                fallbackText = "一个参考项",
            ),
        )

        compose.onNodeWithText("参考内容").assertIsDisplayed()
        compose.onNodeWithText("对象甲").assertIsDisplayed()
        compose.onNodeWithText("图片待补充").assertIsDisplayed()
        compose.onNodeWithText("内容需核对").assertIsDisplayed()
    }

    @Test
    fun comparisonSelectionIsLocalAndVisible() {
        setMessage(
            ComparisonBlock(
                id = "comparison",
                title = "候选比较",
                subtitle = "按可见信息比较",
                selectLabel = "选择此项",
                items = listOf(
                    ComparisonItem(
                        id = "option-a",
                        primary = "选项甲",
                        secondary = "Option A",
                        detail = "距离更近",
                        badge = "较近",
                        facts = listOf(ComparisonFact("距离", "800 米")),
                        imageQuery = "",
                        imageLabel = "",
                        recommended = true,
                    ),
                    ComparisonItem(
                        id = "option-b",
                        primary = "选项乙",
                        secondary = "Option B",
                        detail = "开放时间更长",
                        badge = "",
                        facts = listOf(ComparisonFact("距离", "1.2 公里")),
                        imageQuery = "",
                        imageLabel = "",
                    ),
                ),
                fallbackText = "两个候选项",
            ),
        )

        compose.onNodeWithTag("comparison_open").performClick()
        compose.onNodeWithTag("comparison_dialog").assertIsDisplayed()
        compose.onNodeWithText("选项甲").performClick()
        compose.onNodeWithText("当前选择").assertIsDisplayed()
        compose.onNodeWithText("取消选择").assertIsDisplayed()
    }

    @Test
    fun bilingualHandoffOpensAsPresentationOnlyCard() {
        setMessage(
            BilingualCardBlock(
                id = "handoff",
                titleOriginal = "Information",
                titleChinese = "信息说明",
                contextOriginal = "Please read.",
                contextChinese = "请阅读。",
                showLabel = "展示给对方",
                lines = listOf(BilingualLine("line-a", "First line", "第一行", "")),
                fallbackText = "双语信息",
            ),
        )

        compose.onNodeWithTag("bilingual_open").performClick()
        compose.onNodeWithTag("bilingual_dialog").assertIsDisplayed()
        compose.onNodeWithText("First line").assertIsDisplayed()
        compose.onNodeWithText("第一行").assertIsDisplayed()
        compose.onNodeWithText("完成展示").assertIsDisplayed()
    }

    private fun setMessage(
        vararg blocks: RichBlock,
        role: MessageRole = MessageRole.ASSISTANT,
    ) {
        compose.setContent {
            MaterialTheme {
                MessageRow(
                    message = RichMessage(
                        id = "message",
                        role = role,
                        state = RichMessageState.COMPLETED,
                        revision = 1,
                        rootBlockIds = blocks.map(RichBlock::id),
                        blocks = blocks.associateBy(RichBlock::id),
                        createdAtMillis = 1,
                    ),
                    onAction = {},
                )
            }
        }
    }

    private companion object {
        const val ONE_PIXEL_PNG_BASE64 =
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    }
}
