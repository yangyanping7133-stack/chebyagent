package com.cheby.codex.mobile.model

enum class MessageRole {
    USER,
    ASSISTANT,
    SYSTEM,
}

/**
 * Presentation hint supplied by the conversation provider.
 *
 * It is deliberately independent of message role and content so the UI can hide progress
 * narration and tool activity without guessing from localized text or block types.
 */
enum class MessagePresentation {
    STANDARD,
    PROCESS,
    FINAL_ANSWER,
}

enum class RichMessageState {
    QUEUED,
    STREAMING,
    WAITING_INPUT,
    COMPLETED,
    FAILED,
    INTERRUPTED,
    CANCELLED,
}

enum class Tone {
    NEUTRAL,
    INFO,
    SUCCESS,
    WARNING,
    DANGER,
}

enum class StepState {
    PENDING,
    ACTIVE,
    COMPLETED,
    FAILED,
}

sealed interface RichBlock {
    val id: String
    val fallbackText: String
}

data class TextBlock(
    override val id: String,
    val text: String,
    val markdown: Boolean = true,
    override val fallbackText: String = text,
) : RichBlock

data class StatusBlock(
    override val id: String,
    val label: String,
    val detail: String? = null,
    val tone: Tone = Tone.INFO,
    val progress: Float? = null,
    override val fallbackText: String = listOfNotNull(label, detail).joinToString("："),
) : RichBlock

data class StepItem(
    val id: String,
    val label: String,
    val state: StepState,
    val detail: String? = null,
)

data class StepsBlock(
    override val id: String,
    val title: String,
    val items: List<StepItem>,
    override val fallbackText: String = items.joinToString("；") { it.label },
) : RichBlock

data class MetricItem(
    val label: String,
    val value: String,
    val unit: String? = null,
)

data class MetricsBlock(
    override val id: String,
    val items: List<MetricItem>,
    override val fallbackText: String = items.joinToString("；") {
        listOfNotNull(it.label, it.value, it.unit).joinToString(" ")
    },
) : RichBlock

data class FactItem(
    val label: String,
    val value: String,
    val note: String? = null,
)

data class FactsBlock(
    override val id: String,
    val items: List<FactItem>,
    override val fallbackText: String = items.joinToString("；") { "${it.label}：${it.value}" },
) : RichBlock

data class MediaBlock(
    override val id: String,
    /** Opaque Gateway-owned identifier. It must never be rendered as a URI. */
    val assetId: String,
    val caption: String? = null,
    override val fallbackText: String = caption ?: "媒体内容暂不可用",
) : RichBlock

internal fun isSafeMediaAssetId(value: String): Boolean = MEDIA_ASSET_ID.matches(value)

private val MEDIA_ASSET_ID = Regex("[A-Za-z0-9][A-Za-z0-9._-]{0,255}")

data class SourceBlock(
    override val id: String,
    val label: String,
    val detail: String,
    val trustLabel: String? = null,
    override val fallbackText: String = "$label：$detail",
) : RichBlock

data class RelayNode(
    val id: String,
    val label: String,
    val detail: String? = null,
    val state: StepState = StepState.PENDING,
)

data class RelayBlock(
    override val id: String,
    val title: String,
    val nodes: List<RelayNode>,
    override val fallbackText: String = nodes.joinToString(" → ") { it.label },
) : RichBlock

data class NoticeBlock(
    override val id: String,
    val title: String,
    val body: String,
    val tone: Tone,
    override val fallbackText: String = "$title：$body",
) : RichBlock

enum class ActionKind {
    PRIMARY,
    SECONDARY,
    DESTRUCTIVE,
}

data class ActionItem(
    val id: String,
    val label: String,
    val actionRef: String,
    val kind: ActionKind = ActionKind.PRIMARY,
    val enabled: Boolean = true,
    val retryNotBeforeMillis: Long = 0,
)

data class ActionsBlock(
    override val id: String,
    val items: List<ActionItem>,
    override val fallbackText: String = items.joinToString("、") { it.label },
) : RichBlock

data class TerminalBlock(
    override val id: String,
    val title: String,
    val commandLabel: String? = null,
    val output: String,
    val state: StepState = StepState.COMPLETED,
    val truncated: Boolean = false,
    override val fallbackText: String = listOfNotNull(title, output.takeIf(String::isNotBlank)).joinToString("："),
) : RichBlock

enum class FileChangeKind {
    CREATED,
    MODIFIED,
    DELETED,
    RENAMED,
    READ,
    UNKNOWN,
}

data class FileBlock(
    override val id: String,
    val label: String,
    val change: FileChangeKind = FileChangeKind.UNKNOWN,
    val detail: String? = null,
    val language: String? = null,
    val state: StepState = StepState.COMPLETED,
    override val fallbackText: String = listOfNotNull(label, detail).joinToString("："),
) : RichBlock

data class DiffBlock(
    override val id: String,
    val fileLabel: String,
    val summary: String,
    val preview: String? = null,
    val additions: Int? = null,
    val deletions: Int? = null,
    val truncated: Boolean = false,
    override val fallbackText: String = listOf(fileLabel, summary).joinToString("："),
) : RichBlock

data class ToolBlock(
    override val id: String,
    val label: String,
    val detail: String? = null,
    val state: StepState = StepState.PENDING,
    val durationLabel: String? = null,
    override val fallbackText: String = listOfNotNull(label, detail).joinToString("："),
) : RichBlock

data class TestBlock(
    override val id: String,
    val title: String,
    val summary: String,
    val passed: Int? = null,
    val failed: Int? = null,
    val skipped: Int? = null,
    val durationLabel: String? = null,
    val state: StepState = StepState.COMPLETED,
    override val fallbackText: String = listOf(title, summary).joinToString("："),
) : RichBlock

enum class InteractiveImageKind {
    SOURCE,
    PROVIDER,
    REPRESENTATIVE,
    NONE,
}

data class InteractiveCollectionItem(
    val id: String,
    val primary: String,
    val secondary: String,
    val detail: String,
    /** Price in the smallest unit described by [InteractiveCollectionBlock.minorUnitScale]. */
    val unitPriceMinor: Long,
    val priceLabel: String,
    val imageQuery: String,
    val imageLabel: String,
    val imageKind: InteractiveImageKind,
    val uncertain: Boolean = false,
)

data class InteractiveCollectionGroup(
    val id: String,
    val title: String,
    val items: List<InteractiveCollectionItem>,
)

/**
 * A business-agnostic collection that a person can browse, select, review, and present.
 *
 * Selection state is intentionally client-owned. Rendering this block never invokes a Gateway
 * action, so ordinary taps and quantity changes cannot accidentally cause an external side effect.
 */
data class InteractiveCollectionBlock(
    override val id: String,
    val title: String,
    val subtitle: String,
    val confirmLabel: String,
    val quantityUnit: String = "项",
    val presentationTitle: String,
    val presentationSubtitle: String = "",
    val presentationTotalLabel: String = "合计",
    val presentationNote: String,
    val currencyLabel: String,
    val minorUnitScale: Int,
    val groups: List<InteractiveCollectionGroup>,
    override val fallbackText: String,
) : RichBlock

data class ReferenceImageItem(
    val id: String,
    val title: String,
    val caption: String,
    val imageQuery: String,
    val imageLabel: String,
    val uncertain: Boolean = false,
)

/** A bounded set of clearly-labelled supplemental images chosen by search query, never raw URLs. */
data class ReferenceGalleryBlock(
    override val id: String,
    val title: String,
    val subtitle: String,
    val items: List<ReferenceImageItem>,
    override val fallbackText: String,
) : RichBlock

data class ComparisonFact(
    val label: String,
    val value: String,
)

data class ComparisonItem(
    val id: String,
    val primary: String,
    val secondary: String,
    val detail: String,
    val badge: String,
    val facts: List<ComparisonFact>,
    val imageQuery: String,
    val imageLabel: String,
    val recommended: Boolean = false,
    val uncertain: Boolean = false,
)

/** A business-agnostic, locally selectable comparison for routes, places, stays, and services. */
data class ComparisonBlock(
    override val id: String,
    val title: String,
    val subtitle: String,
    val selectLabel: String,
    val items: List<ComparisonItem>,
    override val fallbackText: String,
) : RichBlock

data class BilingualLine(
    val id: String,
    val original: String,
    val translation: String,
    val note: String,
)

/** A presentation-only handoff card. Opening it never sends, books, calls, or pays. */
data class BilingualCardBlock(
    override val id: String,
    val titleOriginal: String,
    val titleChinese: String,
    val contextOriginal: String,
    val contextChinese: String,
    val showLabel: String,
    val lines: List<BilingualLine>,
    override val fallbackText: String,
) : RichBlock

data class UnknownBlock(
    override val id: String,
    val originalType: String,
    override val fallbackText: String,
) : RichBlock

data class RichMessage(
    val id: String,
    val role: MessageRole,
    val state: RichMessageState,
    val revision: Long,
    val rootBlockIds: List<String>,
    val blocks: Map<String, RichBlock>,
    val createdAtMillis: Long,
    val clientMessageId: String? = null,
    /** Public Codex Turn identity used to reconcile the durable outbox after process death. */
    val turnId: String? = null,
    val presentation: MessagePresentation = MessagePresentation.STANDARD,
)

fun RichBlock.readableText(): String = when (this) {
    is TextBlock -> text
    is StatusBlock -> listOfNotNull(label, detail).joinToString("：")
    is StepsBlock -> items.joinToString("；") { it.label }
    is MetricsBlock -> fallbackText
    is FactsBlock -> fallbackText
    is MediaBlock -> fallbackText
    is SourceBlock -> fallbackText
    is RelayBlock -> fallbackText
    is NoticeBlock -> fallbackText
    is ActionsBlock -> fallbackText
    is TerminalBlock -> fallbackText
    is FileBlock -> fallbackText
    is DiffBlock -> fallbackText
    is ToolBlock -> fallbackText
    is TestBlock -> fallbackText
    is InteractiveCollectionBlock -> fallbackText
    is ReferenceGalleryBlock -> fallbackText
    is ComparisonBlock -> fallbackText
    is BilingualCardBlock -> fallbackText
    is UnknownBlock -> fallbackText.ifBlank { "暂不支持的消息内容" }
}

fun RichMessage.orderedBlocks(): List<RichBlock> = rootBlockIds.asSequence()
    .distinct()
    .take(MAX_RENDERED_ROOT_BLOCKS)
    .mapNotNull(blocks::get)
    .toList()

private const val MAX_RENDERED_ROOT_BLOCKS = 64
