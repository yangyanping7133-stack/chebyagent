package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.model.BilingualCardBlock
import com.cheby.codex.mobile.model.BilingualLine
import com.cheby.codex.mobile.model.ComparisonBlock
import com.cheby.codex.mobile.model.ComparisonFact
import com.cheby.codex.mobile.model.ComparisonItem
import com.cheby.codex.mobile.model.InteractiveCollectionBlock
import com.cheby.codex.mobile.model.InteractiveCollectionGroup
import com.cheby.codex.mobile.model.InteractiveCollectionItem
import com.cheby.codex.mobile.model.InteractiveImageKind
import com.cheby.codex.mobile.model.ReferenceGalleryBlock
import com.cheby.codex.mobile.model.ReferenceImageItem
import com.cheby.codex.mobile.model.RichBlock
import com.cheby.codex.mobile.model.TextBlock
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put

/**
 * Per-turn schema and fail-closed projection for the standalone Codex task response.
 *
 * The schema is intentionally business-agnostic. Codex chooses the smallest useful block type for
 * the user's goal; attaching an image does not imply any particular presentation primitive.
 */
internal object LocalStructuredOutputCodec {
    private val json = Json { ignoreUnknownKeys = false }

    fun outputSchema(): JsonObject = buildJsonObject {
        put("type", "object")
        put("additionalProperties", false)
        put("required", strings("version", "summary", "blocks"))
        put("properties", buildJsonObject {
            put("version", buildJsonObject {
                put("type", "integer")
                put("enum", integers(1))
            })
            put(
                "summary",
                stringSchema(
                    "Concise Chinese orientation for the result. Keep it empty when the first block already " +
                        "provides the same text.",
                ),
            )
            put("blocks", buildJsonObject {
                put("type", "array")
                put("minItems", 1)
                put("maxItems", MAX_BLOCKS)
                put(
                    "description",
                    "Choose blocks by the user's goal, not by input modality. Images may be ordinary text, " +
                        "a gallery, a comparison, a selectable collection, or a bilingual handoff.",
                )
                put("items", buildJsonObject {
                    put("anyOf", buildJsonArray {
                        add(textBlockSchema())
                        add(galleryBlockSchema())
                        add(comparisonBlockSchema())
                        add(selectionBlockSchema())
                        add(bilingualBlockSchema())
                    })
                })
            })
        })
    }

    fun looksStructured(text: String): Boolean = text.trimStart().startsWith('{')

    fun decode(text: String, blockIdPrefix: String): List<RichBlock>? {
        if (text.isBlank() || text.toByteArray(Charsets.UTF_8).size > MAX_JSON_BYTES) return null
        val root = runCatching { json.parseToJsonElement(text.trim()).jsonObject }.getOrNull() ?: return null
        if (root["version"] == null) {
            return decodeLegacyComparisonEnvelope(root, "$blockIdPrefix-block-0")?.let(::listOf)
        }
        if (root["version"]?.jsonPrimitive?.intOrNull != 1) return null
        val summary = root.string("summary", MAX_SUMMARY_CHARS) ?: return null
        val rawBlocks = root["blocks"] as? JsonArray ?: return null
        if (rawBlocks.isEmpty() || rawBlocks.size > MAX_BLOCKS) return null

        return buildList {
            if (summary.isNotBlank()) {
                add(TextBlock("$blockIdPrefix-summary", summary, markdown = true))
            }
            rawBlocks.forEachIndexed { index, element ->
                add(decodeBlock(element as? JsonObject ?: return null, "$blockIdPrefix-block-$index") ?: return null)
            }
        }.takeIf(List<RichBlock>::isNotEmpty)
    }

    /**
     * GLM can occasionally return the pre-v1 flat comparison shape even when the app-server sends
     * the strict v1 schema. Accept only that one bounded legacy shape so a grounded result is not
     * exposed as raw JSON. Arbitrary JSON and other obsolete block types continue to fail closed.
     */
    private fun decodeLegacyComparisonEnvelope(value: JsonObject, id: String): ComparisonBlock? {
        if (value.string("type") != "comparison") return null
        val alternatives = value["alternatives"] as? JsonArray ?: return null
        if (alternatives.size !in 1 until MAX_COMPARISON_ITEMS) return null

        val primary = decodeLegacyComparisonItem(value, index = 0, recommended = true) ?: return null
        val items = buildList {
            add(primary)
            alternatives.forEachIndexed { index, element ->
                add(
                    decodeLegacyComparisonItem(
                        element as? JsonObject ?: return null,
                        index = index + 1,
                        recommended = false,
                    ) ?: return null,
                )
            }
        }
        return ComparisonBlock(
            id = id,
            title = "候选比较",
            subtitle = "根据已核实的页面信息比较；请留意营业时间等实时变化。",
            selectLabel = "选择此项",
            items = items,
            fallbackText = "候选比较，共 ${items.size} 个候选项",
        )
    }

    private fun decodeLegacyComparisonItem(
        value: JsonObject,
        index: Int,
        recommended: Boolean,
    ): ComparisonItem? {
        val rawFacts = value["facts"] as? JsonArray ?: return null
        if (rawFacts.size > MAX_FACTS_PER_COMPARISON_ITEM) return null
        return ComparisonItem(
            id = "option-$index",
            primary = value.nonBlankString("primary") ?: value.nonBlankString("name") ?: return null,
            secondary = value.string("secondary").orEmpty(),
            detail = value.string("detail").orEmpty(),
            badge = value.string("badge", MAX_SHORT_CHARS).orEmpty(),
            facts = rawFacts.mapIndexed { factIndex, element ->
                val fact = (element as? JsonPrimitive)?.contentOrNull
                    ?.takeIf(String::isNotBlank)
                    ?.take(MAX_TEXT_CHARS)
                    ?: return null
                ComparisonFact(label = "信息 ${factIndex + 1}", value = fact)
            },
            imageQuery = value.string("imageQuery").orEmpty(),
            imageLabel = value.string("imageLabel").orEmpty(),
            recommended = recommended,
            uncertain = false,
        )
    }

    private fun decodeBlock(value: JsonObject, id: String): RichBlock? {
        return when (value.string("type")) {
            "text" -> TextBlock(
                id = id,
                text = value.string("text", MAX_TEXT_CHARS) ?: return null,
                markdown = value["markdown"]?.jsonPrimitive?.booleanOrNull ?: return null,
            )
            "gallery" -> decodeGallery(value, id)
            "comparison" -> decodeComparison(value, id)
            "selection" -> decodeSelection(value, id)
            "bilingual" -> decodeBilingual(value, id)
            else -> null
        }
    }

    private fun decodeGallery(value: JsonObject, id: String): ReferenceGalleryBlock? {
        val title = value.nonBlankString("title") ?: return null
        val rawItems = value["items"] as? JsonArray ?: return null
        if (rawItems.isEmpty() || rawItems.size > MAX_GALLERY_ITEMS) return null
        val items = rawItems.mapIndexed { index, element ->
            val item = element as? JsonObject ?: return null
            ReferenceImageItem(
                id = "image-$index",
                title = item.nonBlankString("title") ?: return null,
                caption = item.string("caption") ?: return null,
                imageQuery = item.nonBlankString("imageQuery") ?: return null,
                imageLabel = item.nonBlankString("imageLabel") ?: return null,
                uncertain = item["uncertain"]?.jsonPrimitive?.booleanOrNull ?: return null,
            )
        }
        return ReferenceGalleryBlock(
            id = id,
            title = title,
            subtitle = value.string("subtitle") ?: return null,
            items = items,
            fallbackText = "$title，共 ${items.size} 张参考图片",
        )
    }

    private fun decodeComparison(value: JsonObject, id: String): ComparisonBlock? {
        val title = value.nonBlankString("title") ?: return null
        val rawItems = value["items"] as? JsonArray ?: return null
        if (rawItems.size !in 2..MAX_COMPARISON_ITEMS) return null
        val items = rawItems.mapIndexed { index, element ->
            val item = element as? JsonObject ?: return null
            val rawFacts = item["facts"] as? JsonArray ?: return null
            if (rawFacts.size > MAX_FACTS_PER_COMPARISON_ITEM) return null
            ComparisonItem(
                id = "option-$index",
                primary = item.nonBlankString("primary") ?: return null,
                secondary = item.string("secondary") ?: return null,
                detail = item.string("detail") ?: return null,
                badge = item.string("badge", MAX_SHORT_CHARS) ?: return null,
                facts = rawFacts.map { factElement ->
                    val fact = factElement as? JsonObject ?: return null
                    ComparisonFact(
                        label = fact.nonBlankString("label", MAX_SHORT_CHARS) ?: return null,
                        value = fact.nonBlankString("value") ?: return null,
                    )
                },
                imageQuery = item.string("imageQuery") ?: return null,
                imageLabel = item.string("imageLabel") ?: return null,
                recommended = item["recommended"]?.jsonPrimitive?.booleanOrNull ?: return null,
                uncertain = item["uncertain"]?.jsonPrimitive?.booleanOrNull ?: return null,
            )
        }
        return ComparisonBlock(
            id = id,
            title = title,
            subtitle = value.string("subtitle") ?: return null,
            selectLabel = value.nonBlankString("selectLabel", MAX_SHORT_CHARS) ?: return null,
            items = items,
            fallbackText = "$title，共 ${items.size} 个候选项",
        )
    }

    private fun decodeSelection(value: JsonObject, id: String): InteractiveCollectionBlock? {
        val title = value.nonBlankString("title") ?: return null
        val minorUnitScale = value["minorUnitScale"]?.jsonPrimitive?.intOrNull
            ?.takeIf { it in ALLOWED_MINOR_SCALES } ?: return null
        val rawGroups = value["groups"] as? JsonArray ?: return null
        if (rawGroups.isEmpty() || rawGroups.size > MAX_GROUPS) return null

        var totalItems = 0
        val groups = rawGroups.mapIndexed { groupIndex, groupElement ->
            val group = groupElement as? JsonObject ?: return null
            val rawItems = group["items"] as? JsonArray ?: return null
            if (rawItems.isEmpty() || rawItems.size > MAX_ITEMS_PER_GROUP) return null
            InteractiveCollectionGroup(
                id = "group-$groupIndex",
                title = group.nonBlankString("title") ?: return null,
                items = rawItems.mapIndexed { itemIndex, itemElement ->
                    totalItems += 1
                    if (totalItems > MAX_TOTAL_SELECTION_ITEMS) return null
                    decodeSelectionItem(
                        itemElement as? JsonObject ?: return null,
                        groupIndex,
                        itemIndex,
                    ) ?: return null
                },
            )
        }
        return InteractiveCollectionBlock(
            id = id,
            title = title,
            subtitle = value.string("subtitle") ?: return null,
            confirmLabel = value.nonBlankString("confirmLabel", MAX_SHORT_CHARS) ?: return null,
            quantityUnit = value.nonBlankString("quantityUnit", MAX_SHORT_CHARS) ?: return null,
            presentationTitle = value.nonBlankString("presentationTitle") ?: return null,
            presentationSubtitle = value.string("presentationSubtitle") ?: return null,
            presentationTotalLabel = value.nonBlankString(
                "presentationTotalLabel",
                MAX_SHORT_CHARS,
            ) ?: return null,
            presentationNote = value.string("presentationNote") ?: return null,
            currencyLabel = value.string("currencyLabel", MAX_SHORT_CHARS) ?: return null,
            minorUnitScale = minorUnitScale,
            groups = groups,
            fallbackText = "$title，共 $totalItems 个可选项",
        )
    }

    private fun decodeSelectionItem(
        value: JsonObject,
        groupIndex: Int,
        itemIndex: Int,
    ): InteractiveCollectionItem? {
        val imageKind = when (value.string("imageKind")) {
            "representative" -> InteractiveImageKind.REPRESENTATIVE
            "none" -> InteractiveImageKind.NONE
            else -> return null
        }
        val price = value["unitPriceMinor"]?.jsonPrimitive?.longOrNull
            ?.takeIf { it in -1L..MAX_UNIT_PRICE_MINOR } ?: return null
        return InteractiveCollectionItem(
            id = "item-$groupIndex-$itemIndex",
            primary = value.nonBlankString("primary") ?: return null,
            secondary = value.string("secondary") ?: return null,
            detail = value.string("detail") ?: return null,
            unitPriceMinor = price,
            priceLabel = value.nonBlankString("priceLabel", MAX_SHORT_CHARS) ?: return null,
            imageQuery = value.string("imageQuery") ?: return null,
            imageLabel = value.string("imageLabel") ?: return null,
            imageKind = imageKind,
            uncertain = value["uncertain"]?.jsonPrimitive?.booleanOrNull ?: return null,
        )
    }

    private fun decodeBilingual(value: JsonObject, id: String): BilingualCardBlock? {
        val titleOriginal = value.nonBlankString("titleOriginal") ?: return null
        val rawLines = value["lines"] as? JsonArray ?: return null
        if (rawLines.isEmpty() || rawLines.size > MAX_BILINGUAL_LINES) return null
        val lines = rawLines.mapIndexed { index, element ->
            val line = element as? JsonObject ?: return null
            BilingualLine(
                id = "line-$index",
                original = line.nonBlankString("original") ?: return null,
                translation = line.nonBlankString("translation") ?: return null,
                note = line.string("note") ?: return null,
            )
        }
        return BilingualCardBlock(
            id = id,
            titleOriginal = titleOriginal,
            titleChinese = value.nonBlankString("titleChinese") ?: return null,
            contextOriginal = value.string("contextOriginal") ?: return null,
            contextChinese = value.string("contextChinese") ?: return null,
            showLabel = value.nonBlankString("showLabel", MAX_SHORT_CHARS) ?: return null,
            lines = lines,
            fallbackText = "$titleOriginal：${lines.joinToString("；") { it.original }}",
        )
    }

    private fun textBlockSchema(): JsonObject = objectSchema(
        required = arrayOf("type", "text", "markdown"),
        properties = buildJsonObject {
            put("type", enumString("text"))
            put("text", stringSchema("Chinese Markdown or plain text that directly answers the user."))
            put("markdown", buildJsonObject { put("type", "boolean") })
        },
    )

    private fun galleryBlockSchema(): JsonObject = objectSchema(
        required = arrayOf("type", "title", "subtitle", "items"),
        properties = buildJsonObject {
            put("type", enumString("gallery"))
            put("title", stringSchema("Chinese gallery title."))
            put("subtitle", stringSchema("Chinese context and image-source caveat."))
            put("items", arraySchema(1, MAX_GALLERY_ITEMS, objectSchema(
                required = arrayOf("title", "caption", "imageQuery", "imageLabel", "uncertain"),
                properties = buildJsonObject {
                    put("title", stringSchema("Short Chinese image title."))
                    put("caption", stringSchema("Chinese explanation of why the image helps."))
                    put("imageQuery", stringSchema("Specific English Wikimedia image-search phrase; never a URL."))
                    put("imageLabel", stringSchema("Honest label such as 参考图片，不代表现场实物."))
                    put("uncertain", booleanSchema())
                },
            )))
        },
    )

    private fun comparisonBlockSchema(): JsonObject = objectSchema(
        required = arrayOf("type", "title", "subtitle", "selectLabel", "items"),
        properties = buildJsonObject {
            put("type", enumString("comparison"))
            put("title", stringSchema("Chinese comparison title."))
            put("subtitle", stringSchema("State scope, source gaps, and what the user is choosing."))
            put("selectLabel", stringSchema("Short Chinese local-selection label, for example 选择此项."))
            put("items", arraySchema(2, MAX_COMPARISON_ITEMS, objectSchema(
                required = arrayOf(
                    "primary",
                    "secondary",
                    "detail",
                    "badge",
                    "facts",
                    "imageQuery",
                    "imageLabel",
                    "recommended",
                    "uncertain",
                ),
                properties = buildJsonObject {
                    put("primary", stringSchema("Exact candidate name or route label."))
                    put("secondary", stringSchema("Chinese translation or concise secondary label."))
                    put("detail", stringSchema("Chinese rationale based only on observed facts."))
                    put("badge", stringSchema("Short observed status such as 营业中, 最快, or empty."))
                    put("facts", arraySchema(0, MAX_FACTS_PER_COMPARISON_ITEM, objectSchema(
                        required = arrayOf("label", "value"),
                        properties = buildJsonObject {
                            put("label", stringSchema("Short Chinese fact label."))
                            put("value", stringSchema("Observed value including its unit."))
                        },
                    )))
                    put("imageQuery", stringSchema("Specific English Wikimedia query, or empty when not useful."))
                    put("imageLabel", stringSchema("Honest reference-image label, or empty."))
                    put("recommended", booleanSchema())
                    put("uncertain", booleanSchema())
                },
            )))
        },
    )

    private fun selectionBlockSchema(): JsonObject = objectSchema(
        required = arrayOf(
            "type",
            "title",
            "subtitle",
            "confirmLabel",
            "quantityUnit",
            "presentationTitle",
            "presentationSubtitle",
            "presentationTotalLabel",
            "presentationNote",
            "currencyLabel",
            "minorUnitScale",
            "groups",
        ),
        properties = buildJsonObject {
            put("type", enumString("selection"))
            put("title", stringSchema("Chinese title for the complete selectable collection."))
            put("subtitle", stringSchema("State that unclear source text is marked rather than invented."))
            put("confirmLabel", stringSchema("Short Chinese review action, for example 确认选择."))
            put("quantityUnit", stringSchema("Chinese quantity unit supplied by the active Skill, for example 项."))
            put("presentationTitle", stringSchema("Short recipient-language title for the final presentation."))
            put("presentationSubtitle", stringSchema("Recipient-language presentation context, or empty."))
            put("presentationTotalLabel", stringSchema("Recipient-language total label when prices exist."))
            put("presentationNote", stringSchema("Recipient-language note, or empty."))
            put("currencyLabel", stringSchema("Visible currency symbol or label, or empty."))
            put("minorUnitScale", buildJsonObject {
                put("type", "integer")
                put("enum", integers(1, 10, 100, 1_000))
            })
            put("groups", arraySchema(1, MAX_GROUPS, groupSchema()))
        },
    )

    private fun groupSchema(): JsonObject = objectSchema(
        required = arrayOf("title", "items"),
        properties = buildJsonObject {
            put("title", stringSchema("Chinese category title followed by the source title when useful."))
            put("items", arraySchema(1, MAX_ITEMS_PER_GROUP, selectionItemSchema()))
        },
    )

    private fun selectionItemSchema(): JsonObject = objectSchema(
        required = arrayOf(
            "primary",
            "secondary",
            "detail",
            "unitPriceMinor",
            "priceLabel",
            "imageQuery",
            "imageLabel",
            "imageKind",
            "uncertain",
        ),
        properties = buildJsonObject {
            put("primary", stringSchema(
                "Exact source-language item name. Preserve visible punctuation and never invent obscured text.",
            ))
            put("secondary", stringSchema("Plain Chinese translation."))
            put("detail", stringSchema("Short Chinese explanation; explicitly mark uncertain ingredients."))
            put("unitPriceMinor", buildJsonObject {
                put("type", "integer")
                put("minimum", -1)
                put("maximum", MAX_UNIT_PRICE_MINOR)
                put(
                    "description",
                    "Visible same-row price multiplied by minorUnitScale, or -1 when unavailable. Re-read every " +
                        "digit; never infer a blurred digit.",
                )
            })
            put("priceLabel", stringSchema("Exact visible price label, or 价格不清."))
            put("imageQuery", stringSchema("Specific English image-search phrase, or empty when identity is uncertain."))
            put("imageLabel", stringSchema("Honest source label such as 参考图片，不代表实际对象."))
            put("imageKind", buildJsonObject {
                put("type", "string")
                put("enum", strings("representative", "none"))
            })
            put("uncertain", booleanSchema())
        },
    )

    private fun bilingualBlockSchema(): JsonObject = objectSchema(
        required = arrayOf(
            "type",
            "titleOriginal",
            "titleChinese",
            "contextOriginal",
            "contextChinese",
            "showLabel",
            "lines",
        ),
        properties = buildJsonObject {
            put("type", enumString("bilingual"))
            put("titleOriginal", stringSchema("Short title in the recipient's language."))
            put("titleChinese", stringSchema("Equivalent Chinese title for the user."))
            put("contextOriginal", stringSchema("Recipient-language context or empty."))
            put("contextChinese", stringSchema("Chinese context or empty."))
            put("showLabel", stringSchema("Chinese local presentation label, for example 展示给对方."))
            put("lines", arraySchema(1, MAX_BILINGUAL_LINES, objectSchema(
                required = arrayOf("original", "translation", "note"),
                properties = buildJsonObject {
                    put("original", stringSchema("Exact recipient-facing sentence or item."))
                    put("translation", stringSchema("Faithful Chinese translation."))
                    put("note", stringSchema("Chinese caveat or empty; never claim an action was completed."))
                },
            )))
        },
    )

    private fun objectSchema(required: Array<String>, properties: JsonObject): JsonObject = buildJsonObject {
        put("type", "object")
        put("additionalProperties", false)
        put("required", strings(*required))
        put("properties", properties)
    }

    private fun arraySchema(min: Int, max: Int, items: JsonObject): JsonObject = buildJsonObject {
        put("type", "array")
        put("minItems", min)
        put("maxItems", max)
        put("items", items)
    }

    private fun stringSchema(description: String): JsonObject = buildJsonObject {
        put("type", "string")
        put("description", description)
    }

    private fun booleanSchema(): JsonObject = buildJsonObject { put("type", "boolean") }

    private fun enumString(value: String): JsonObject = buildJsonObject {
        put("type", "string")
        put("enum", strings(value))
    }

    private fun strings(vararg values: String): JsonArray = buildJsonArray {
        values.forEach { add(JsonPrimitive(it)) }
    }

    private fun integers(vararg values: Int): JsonArray = buildJsonArray {
        values.forEach { add(JsonPrimitive(it)) }
    }

    private fun JsonObject.string(key: String, maxChars: Int = MAX_FIELD_CHARS): String? =
        (this[key] as? JsonPrimitive)?.contentOrNull?.trim()?.take(maxChars)

    private fun JsonObject.nonBlankString(key: String, maxChars: Int = MAX_FIELD_CHARS): String? =
        string(key, maxChars)?.takeIf(String::isNotBlank)

    private const val MAX_JSON_BYTES = 512 * 1024
    private const val MAX_SUMMARY_CHARS = 2_000
    private const val MAX_TEXT_CHARS = 32_000
    private const val MAX_FIELD_CHARS = 2_000
    private const val MAX_SHORT_CHARS = 128
    private const val MAX_BLOCKS = 8
    private const val MAX_GALLERY_ITEMS = 8
    private const val MAX_COMPARISON_ITEMS = 12
    private const val MAX_FACTS_PER_COMPARISON_ITEM = 8
    private const val MAX_BILINGUAL_LINES = 24
    private const val MAX_GROUPS = 32
    private const val MAX_ITEMS_PER_GROUP = 32
    private const val MAX_TOTAL_SELECTION_ITEMS = 256
    private const val MAX_UNIT_PRICE_MINOR = 1_000_000_000_000L
    private val ALLOWED_MINOR_SCALES = setOf(1, 10, 100, 1_000)
}
