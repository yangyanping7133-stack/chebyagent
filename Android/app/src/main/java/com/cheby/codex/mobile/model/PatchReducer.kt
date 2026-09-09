package com.cheby.codex.mobile.model

import java.util.ArrayDeque

data class ConversationSnapshot(
    val threadKey: ThreadKey,
    val streamId: String,
    val cursor: Long,
    val messages: List<RichMessage>,
)

data class ThreadConversationState(
    val threadKey: ThreadKey,
    val streamId: String = "",
    val streamSeq: Long = 0,
    val messages: List<RichMessage> = emptyList(),
    val processedEvents: Map<String, ProcessedEventIdentity> = emptyMap(),
    val resyncRequired: Boolean = false,
) {
    val processedEventIds: Set<String> get() = processedEvents.keys
}

data class ProcessedEventIdentity(
    val streamId: String,
    val streamSeq: Long,
)

enum class EventSequenceDecision {
    NEW,
    EXACT_DUPLICATE,
    CONFLICT,
}

object EventSequenceGate {
    const val MAX_PROCESSED_EVENTS = 1_024

    fun classify(
        currentStreamId: String,
        currentStreamSeq: Long,
        processedEvents: Map<String, ProcessedEventIdentity>,
        eventId: String,
        eventStreamId: String,
        eventStreamSeq: Long,
    ): EventSequenceDecision {
        if (eventStreamId != currentStreamId || currentStreamSeq < 0) return EventSequenceDecision.CONFLICT
        val identity = ProcessedEventIdentity(eventStreamId, eventStreamSeq)
        val previous = processedEvents[eventId]
        val isExpectedNext = currentStreamSeq != Long.MAX_VALUE && eventStreamSeq == currentStreamSeq + 1
        if (isExpectedNext) {
            return if (previous == null) EventSequenceDecision.NEW else EventSequenceDecision.CONFLICT
        }
        return if (previous == identity) {
            EventSequenceDecision.EXACT_DUPLICATE
        } else {
            EventSequenceDecision.CONFLICT
        }
    }

    fun append(
        current: Map<String, ProcessedEventIdentity>,
        eventId: String,
        identity: ProcessedEventIdentity,
    ): Map<String, ProcessedEventIdentity> {
        val result = LinkedHashMap<String, ProcessedEventIdentity>(MAX_PROCESSED_EVENTS)
        current.entries.toList().takeLast((MAX_PROCESSED_EVENTS - 1).coerceAtLeast(0)).forEach { entry ->
            result[entry.key] = entry.value
        }
        result[eventId] = identity
        return result
    }
}

enum class ConversationReductionDisposition {
    APPLIED,
    EXACT_DUPLICATE,
    RESYNC_REQUIRED,
}

data class ConversationReductionResult(
    val state: ThreadConversationState,
    val disposition: ConversationReductionDisposition,
)

sealed interface PatchOperation {
    data class PutBlock(val block: RichBlock) : PatchOperation
    data class MergeBlock(val blockId: String, val value: Map<String, Any?>) : PatchOperation
    data class AppendBlockText(val blockId: String, val value: String) : PatchOperation
    data class AppendTerminal(val blockId: String, val value: String) : PatchOperation
    data class RemoveBlock(val blockId: String) : PatchOperation
    data class SetRootBlocks(val blockIds: List<String>) : PatchOperation
    data class SetMessageState(val state: RichMessageState) : PatchOperation
    data class SetMessagePresentation(val presentation: MessagePresentation) : PatchOperation
    data class ResolveAction(val blockId: String?, val actionId: String) : PatchOperation
}

sealed interface ConversationEvent {
    val eventId: String
    val streamId: String
    val streamSeq: Long

    data class MessageAdded(
        override val eventId: String,
        override val streamId: String,
        override val streamSeq: Long,
        val message: RichMessage,
    ) : ConversationEvent

    data class MessagePatched(
        override val eventId: String,
        override val streamId: String,
        override val streamSeq: Long,
        val messageId: String,
        val baseRevision: Long,
        val revision: Long,
        val operations: List<PatchOperation>,
    ) : ConversationEvent

    data class SyncRequired(
        override val eventId: String,
        override val streamId: String,
        override val streamSeq: Long,
    ) : ConversationEvent

    data class TurnLifecycle(
        override val eventId: String,
        override val streamId: String,
        override val streamSeq: Long,
        val threadKey: ThreadKey,
        val turnId: String,
        val phase: TurnLifecyclePhase,
    ) : ConversationEvent

    data class NoOp(
        override val eventId: String,
        override val streamId: String,
        override val streamSeq: Long,
    ) : ConversationEvent
}

enum class TurnLifecyclePhase {
    STARTED,
    COMPLETED,
    FAILED,
    INTERRUPTED,
}

object ConversationReducer {
    fun fromSnapshot(snapshot: ConversationSnapshot): ThreadConversationState {
        val safeStreamId = snapshot.streamId.takeIf { isSafeOpaque(it, MAX_EVENT_ID_BYTES) }.orEmpty()
        fun rejectedSnapshot() = ThreadConversationState(
            threadKey = snapshot.threadKey,
            streamId = safeStreamId,
            streamSeq = snapshot.cursor.coerceAtLeast(0),
            messages = emptyList(),
            resyncRequired = true,
        )
        if (
            snapshot.cursor < 0 ||
            safeStreamId.isEmpty() ||
            snapshot.messages.size > MAX_MESSAGES ||
            snapshot.messages.map(RichMessage::id).distinct().size != snapshot.messages.size
        ) return rejectedSnapshot()
        val normalizedMessages = ArrayList<RichMessage>(snapshot.messages.size)
        snapshot.messages.forEach { message ->
            normalizedMessages += normalizeMessage(message) ?: return rejectedSnapshot()
        }
        return ThreadConversationState(
            threadKey = snapshot.threadKey,
            streamId = safeStreamId,
            streamSeq = snapshot.cursor,
            messages = normalizedMessages,
        )
    }

    fun reduce(
        state: ThreadConversationState,
        event: ConversationEvent,
    ): ThreadConversationState = reduceWithDisposition(state, event).state

    fun reduceWithDisposition(
        state: ThreadConversationState,
        event: ConversationEvent,
    ): ConversationReductionResult {
        fun resync(): ConversationReductionResult = ConversationReductionResult(
            state.copy(resyncRequired = true),
            ConversationReductionDisposition.RESYNC_REQUIRED,
        )
        if (state.processedEvents.size > EventSequenceGate.MAX_PROCESSED_EVENTS) return resync()
        if (state.resyncRequired) return resync()
        if (
            event.eventId.isBlank() ||
            SafeDisplayText.exceedsUtf8Limit(event.eventId, MAX_EVENT_ID_BYTES) ||
            event.streamId.isBlank() ||
            SafeDisplayText.exceedsUtf8Limit(event.streamId, MAX_EVENT_ID_BYTES)
        ) return resync()

        when (
            EventSequenceGate.classify(
                currentStreamId = state.streamId,
                currentStreamSeq = state.streamSeq,
                processedEvents = state.processedEvents,
                eventId = event.eventId,
                eventStreamId = event.streamId,
                eventStreamSeq = event.streamSeq,
            )
        ) {
            EventSequenceDecision.EXACT_DUPLICATE -> return ConversationReductionResult(
                state,
                ConversationReductionDisposition.EXACT_DUPLICATE,
            )
            EventSequenceDecision.CONFLICT -> return resync()
            EventSequenceDecision.NEW -> Unit
        }

        val reduced = when (event) {
            is ConversationEvent.MessageAdded -> addMessage(state, event)
            is ConversationEvent.MessagePatched -> patchMessage(state, event)
            is ConversationEvent.SyncRequired -> state.copy(resyncRequired = true)
            is ConversationEvent.TurnLifecycle -> state
            is ConversationEvent.NoOp -> state
        }

        return if (reduced.resyncRequired) {
            ConversationReductionResult(reduced, ConversationReductionDisposition.RESYNC_REQUIRED)
        } else {
            ConversationReductionResult(
                reduced.copy(
                    streamSeq = event.streamSeq,
                    processedEvents = EventSequenceGate.append(
                        reduced.processedEvents,
                        event.eventId,
                        ProcessedEventIdentity(event.streamId, event.streamSeq),
                    ),
                ),
                ConversationReductionDisposition.APPLIED,
            )
        }
    }

    private fun addMessage(
        state: ThreadConversationState,
        event: ConversationEvent.MessageAdded,
    ): ThreadConversationState {
        val normalizedMessage = normalizeMessage(event.message)
            ?: return state.copy(resyncRequired = true)
        val existingIndex = state.messages.indexOfFirst { it.id == event.message.id }
        if (existingIndex < 0) {
            if (state.messages.size >= MAX_MESSAGES) return state.copy(resyncRequired = true)
            return state.copy(messages = state.messages + normalizedMessage)
        }

        val existing = state.messages[existingIndex]
        if (existing == normalizedMessage) return state
        return state.copy(resyncRequired = true)
    }

    private fun patchMessage(
        state: ThreadConversationState,
        event: ConversationEvent.MessagePatched,
    ): ThreadConversationState {
        if (event.operations.size > MAX_PATCH_OPERATIONS) return state.copy(resyncRequired = true)
        val index = state.messages.indexOfFirst { it.id == event.messageId }
        if (index < 0) return state.copy(resyncRequired = true)

        val message = state.messages[index]
        if (
            message.revision < 0 ||
            event.baseRevision < 0 ||
            event.baseRevision == Long.MAX_VALUE ||
            event.baseRevision != message.revision ||
            event.revision != event.baseRevision + 1
        ) {
            return state.copy(resyncRequired = true)
        }

        if (!message.hasValidBlockGraph()) return state.copy(resyncRequired = true)
        val normalizedBlocks = LinkedHashMap<String, RichBlock>(message.blocks.size)
        message.blocks.forEach { (blockId, block) ->
            val normalized = sanitizeBlock(block) ?: return state.copy(resyncRequired = true)
            if (normalized.id != blockId) return state.copy(resyncRequired = true)
            normalizedBlocks[blockId] = normalized
        }
        var blocks: Map<String, RichBlock> = normalizedBlocks
        var roots = message.rootBlockIds.distinct()
        var messageState = message.state
        var messagePresentation = message.presentation

        event.operations.forEach { operation ->
            when (operation) {
                is PatchOperation.PutBlock -> {
                    val normalized = sanitizeBlock(operation.block)
                        ?: return state.copy(resyncRequired = true)
                    if (normalized.id !in blocks && blocks.size >= MAX_MESSAGE_BLOCKS) {
                        return state.copy(resyncRequired = true)
                    }
                    blocks = blocks + (normalized.id to normalized)
                }
                is PatchOperation.MergeBlock -> {
                    if (!patchValueWithinLimits(operation.value)) {
                        return state.copy(resyncRequired = true)
                    }
                    val existing = blocks[operation.blockId]
                        ?: return state.copy(resyncRequired = true)
                    val merged = mergeBlock(existing, operation.value)
                        ?: return state.copy(resyncRequired = true)
                    val normalized = sanitizeBlock(merged)
                        ?: return state.copy(resyncRequired = true)
                    blocks = blocks + (operation.blockId to normalized)
                }
                is PatchOperation.AppendBlockText -> {
                    val existing = blocks[operation.blockId]
                    if (existing !is TextBlock) return state.copy(resyncRequired = true)
                    if (SafeDisplayText.exceedsUtf8Limit(operation.value, MAX_STREAM_TEXT_BYTES)) {
                        return state.copy(resyncRequired = true)
                    }
                    val text = SafeDisplayText.sanitizeStrict(
                        existing.text + operation.value,
                        MAX_STREAM_TEXT_BYTES,
                    ) ?: return state.copy(resyncRequired = true)
                    val fallback = SafeDisplayText.sanitizeStrict(
                        existing.fallbackText + operation.value,
                        MAX_STREAM_TEXT_BYTES,
                    ) ?: return state.copy(resyncRequired = true)
                    blocks = blocks + (
                        operation.blockId to existing.copy(
                            text = text,
                            fallbackText = fallback,
                        )
                    )
                }
                is PatchOperation.AppendTerminal -> {
                    if (SafeDisplayText.exceedsUtf8Limit(operation.value, MAX_STREAM_TEXT_BYTES)) {
                        return state.copy(resyncRequired = true)
                    }
                    val existing = blocks[operation.blockId]
                    when (existing) {
                        is TerminalBlock -> {
                            val (output, truncated) = SafeDisplayText.append(
                                existing.output,
                                operation.value,
                                SafeDisplayText.MAX_TERMINAL,
                            )
                            blocks = blocks + (
                                operation.blockId to existing.copy(
                                    output = output,
                                    truncated = existing.truncated || truncated,
                                )
                            )
                        }
                        is UnknownBlock -> {
                            if (existing.originalType != "terminal") return state.copy(resyncRequired = true)
                            val (fallback, _) = SafeDisplayText.append(
                                existing.fallbackText,
                                operation.value,
                                SafeDisplayText.MAX_FALLBACK,
                            )
                            blocks = blocks + (operation.blockId to existing.copy(fallbackText = fallback))
                        }
                        else -> return state.copy(resyncRequired = true)
                    }
                }
                is PatchOperation.RemoveBlock -> {
                    blocks = blocks - operation.blockId
                    roots = roots - operation.blockId
                }
                is PatchOperation.SetRootBlocks -> {
                    if (
                        operation.blockIds.size > MAX_ROOT_BLOCKS ||
                        operation.blockIds.distinct().size != operation.blockIds.size ||
                        operation.blockIds.any { it !in blocks }
                    ) return state.copy(resyncRequired = true)
                    roots = operation.blockIds
                }
                is PatchOperation.SetMessageState -> messageState = operation.state
                is PatchOperation.SetMessagePresentation -> messagePresentation = operation.presentation
                is PatchOperation.ResolveAction -> {
                    var resolved = false
                    blocks = blocks.mapValues { (blockId, block) ->
                        if (block is ActionsBlock && (operation.blockId == null || operation.blockId == blockId)) {
                            block.copy(
                                items = block.items.map { item ->
                                    if (item.id == operation.actionId) {
                                        resolved = true
                                        item.copy(enabled = false)
                                    } else {
                                        item
                                    }
                                },
                            )
                        } else {
                            block
                        }
                    }
                    if (!resolved) return state.copy(resyncRequired = true)
                }
            }
            if (!isValidBlockGraph(blocks, roots)) return state.copy(resyncRequired = true)
        }

        if (messageState == RichMessageState.COMPLETED && blocks.values.any { it.hasUnfinishedWork() }) {
            return state.copy(resyncRequired = true)
        }

        val updated = message.copy(
            state = messageState,
            revision = event.revision,
            rootBlockIds = roots,
            blocks = blocks,
            presentation = messagePresentation,
        )
        val messages = state.messages.toMutableList().also { it[index] = updated }
        return state.copy(messages = messages)
    }

    private fun RichBlock.hasUnfinishedWork(): Boolean = when (this) {
        is StepsBlock -> items.any { it.state == StepState.ACTIVE || it.state == StepState.PENDING }
        is RelayBlock -> nodes.any { it.state == StepState.ACTIVE || it.state == StepState.PENDING }
        is TerminalBlock -> state == StepState.ACTIVE || state == StepState.PENDING
        is FileBlock -> state == StepState.ACTIVE || state == StepState.PENDING
        is ToolBlock -> state == StepState.ACTIVE || state == StepState.PENDING
        is TestBlock -> state == StepState.ACTIVE || state == StepState.PENDING
        else -> false
    }

    private fun RichMessage.hasValidBlockGraph(): Boolean =
        revision >= 0 && isValidBlockGraph(blocks, rootBlockIds)

    private fun normalizeMessage(message: RichMessage): RichMessage? {
        if (!message.hasValidBlockGraph() || !isSafeOpaque(message.id, MAX_EVENT_ID_BYTES)) return null
        val normalized = LinkedHashMap<String, RichBlock>(message.blocks.size)
        message.blocks.forEach { (id, block) ->
            val safeBlock = sanitizeBlock(block) ?: return null
            if (safeBlock.id != id) return null
            normalized[id] = safeBlock
        }
        if (message.state == RichMessageState.COMPLETED && normalized.values.any { it.hasUnfinishedWork() }) {
            return null
        }
        return message.copy(blocks = normalized)
    }

    private fun isValidBlockGraph(
        blocks: Map<String, RichBlock>,
        roots: List<String>,
    ): Boolean =
        blocks.size <= MAX_MESSAGE_BLOCKS &&
            blocks.all { (id, block) -> id == block.id && block.withinItemLimits() } &&
            roots.size <= MAX_ROOT_BLOCKS &&
            roots.distinct().size == roots.size &&
            roots.all(blocks::containsKey)

    private fun RichBlock.withinItemLimits(): Boolean = when (this) {
        is StepsBlock -> items.size <= MAX_BLOCK_ITEMS
        is MetricsBlock -> items.size <= MAX_BLOCK_ITEMS
        is FactsBlock -> items.size <= MAX_BLOCK_ITEMS
        is RelayBlock -> nodes.size <= MAX_BLOCK_ITEMS
        is ActionsBlock -> items.size <= MAX_ACTION_ITEMS
        is InteractiveCollectionBlock ->
            groups.size <= MAX_INTERACTIVE_GROUPS &&
                groups.sumOf { it.items.size } <= MAX_INTERACTIVE_ITEMS
        is ReferenceGalleryBlock -> items.size <= MAX_REFERENCE_GALLERY_ITEMS
        is ComparisonBlock ->
            items.size <= MAX_COMPARISON_ITEMS &&
                items.all { it.facts.size <= MAX_COMPARISON_FACTS }
        is BilingualCardBlock -> lines.size <= MAX_BILINGUAL_LINES
        else -> true
    }

    private fun sanitizeBlock(block: RichBlock): RichBlock? {
        if (!isSafeOpaque(block.id, MAX_EVENT_ID_BYTES)) return null
        return when (block) {
        is TextBlock -> block.copy(
            text = SafeDisplayText.sanitizeStrict(block.text, MAX_STREAM_TEXT_BYTES) ?: return null,
            fallbackText = SafeDisplayText.sanitizeStrict(block.fallbackText, MAX_STREAM_TEXT_BYTES) ?: return null,
        )
        is StatusBlock -> {
            if (block.progress != null && (!block.progress.isFinite() || block.progress !in 0f..1f)) return null
            block.copy(
                label = SafeDisplayText.sanitize(block.label, SafeDisplayText.MAX_LABEL),
                detail = block.detail?.let { SafeDisplayText.sanitize(it, SafeDisplayText.MAX_DETAIL) },
                fallbackText = SafeDisplayText.sanitize(block.fallbackText, SafeDisplayText.MAX_FALLBACK),
            )
        }
        is StepsBlock -> {
            if (block.items.size > MAX_BLOCK_ITEMS) return null
            if (
                block.items.any { !isSafeOpaque(it.id, MAX_EVENT_ID_BYTES) } ||
                block.items.map(StepItem::id).distinct().size != block.items.size
            ) return null
            block.copy(
                title = SafeDisplayText.sanitize(block.title, SafeDisplayText.MAX_LABEL),
                items = block.items.map { item ->
                    item.copy(
                        label = SafeDisplayText.sanitize(item.label, SafeDisplayText.MAX_LABEL),
                        detail = item.detail?.let { SafeDisplayText.sanitize(it, SafeDisplayText.MAX_DETAIL) },
                    )
                },
                fallbackText = SafeDisplayText.sanitize(block.fallbackText, SafeDisplayText.MAX_FALLBACK),
            )
        }
        is MetricsBlock -> {
            if (block.items.size > MAX_BLOCK_ITEMS) return null
            block.copy(
                items = block.items.map { item ->
                    item.copy(
                        label = SafeDisplayText.sanitize(item.label, MAX_SHORT_VALUE_BYTES),
                        value = SafeDisplayText.sanitize(item.value, SafeDisplayText.MAX_LABEL),
                        unit = item.unit?.let { SafeDisplayText.sanitize(it, MAX_UNIT_BYTES) },
                    )
                },
                fallbackText = SafeDisplayText.sanitize(block.fallbackText, SafeDisplayText.MAX_FALLBACK),
            )
        }
        is FactsBlock -> {
            if (block.items.size > MAX_BLOCK_ITEMS) return null
            block.copy(
                items = block.items.map { item ->
                    item.copy(
                        label = SafeDisplayText.sanitize(item.label, MAX_SHORT_VALUE_BYTES),
                        value = SafeDisplayText.sanitize(item.value, SafeDisplayText.MAX_DETAIL),
                        note = item.note?.let { SafeDisplayText.sanitize(it, SafeDisplayText.MAX_DETAIL) },
                    )
                },
                fallbackText = SafeDisplayText.sanitize(block.fallbackText, SafeDisplayText.MAX_FALLBACK),
            )
        }
        is MediaBlock -> block.copy(
            assetId = block.assetId.takeIf(::isSafeMediaAssetId) ?: return null,
            caption = block.caption?.let { SafeDisplayText.sanitize(it, SafeDisplayText.MAX_LABEL) },
            fallbackText = SafeDisplayText.sanitize(block.fallbackText, SafeDisplayText.MAX_FALLBACK),
        )
        is SourceBlock -> block.copy(
            label = SafeDisplayText.sanitize(block.label, SafeDisplayText.MAX_LABEL),
            detail = SafeDisplayText.sanitize(block.detail, SafeDisplayText.MAX_DETAIL),
            trustLabel = block.trustLabel?.let { SafeDisplayText.sanitize(it, MAX_SHORT_VALUE_BYTES) },
            fallbackText = SafeDisplayText.sanitize(block.fallbackText, SafeDisplayText.MAX_FALLBACK),
        )
        is RelayBlock -> {
            if (block.nodes.size > MAX_BLOCK_ITEMS) return null
            if (
                block.nodes.any { !isSafeOpaque(it.id, MAX_EVENT_ID_BYTES) } ||
                block.nodes.map(RelayNode::id).distinct().size != block.nodes.size
            ) return null
            block.copy(
                title = SafeDisplayText.sanitize(block.title, SafeDisplayText.MAX_LABEL),
                nodes = block.nodes.map { node ->
                    node.copy(
                        label = SafeDisplayText.sanitize(node.label, SafeDisplayText.MAX_LABEL),
                        detail = node.detail?.let { SafeDisplayText.sanitize(it, SafeDisplayText.MAX_DETAIL) },
                    )
                },
                fallbackText = SafeDisplayText.sanitize(block.fallbackText, SafeDisplayText.MAX_FALLBACK),
            )
        }
        is NoticeBlock -> block.copy(
            title = SafeDisplayText.sanitize(block.title, SafeDisplayText.MAX_LABEL),
            body = SafeDisplayText.sanitize(block.body, SafeDisplayText.MAX_DETAIL),
            fallbackText = SafeDisplayText.sanitize(block.fallbackText, SafeDisplayText.MAX_FALLBACK),
        )
        is ActionsBlock -> {
            if (block.items.size > MAX_ACTION_ITEMS) return null
            if (
                block.items.any { !isSafeAction(it) } ||
                block.items.map(ActionItem::id).distinct().size != block.items.size
            ) return null
            block.copy(
                items = block.items.map { item ->
                    item.copy(label = SafeDisplayText.sanitize(item.label, MAX_SHORT_VALUE_BYTES))
                },
                fallbackText = SafeDisplayText.sanitize(block.fallbackText, SafeDisplayText.MAX_FALLBACK),
            )
        }
        is TerminalBlock -> block.copy(
            title = SafeDisplayText.sanitize(block.title, SafeDisplayText.MAX_LABEL),
            commandLabel = block.commandLabel?.let(::safeSemanticCommandLabel),
            output = SafeDisplayText.sanitize(block.output, SafeDisplayText.MAX_TERMINAL),
            truncated = block.truncated ||
                SafeDisplayText.exceedsUtf8Limit(block.output, SafeDisplayText.MAX_TERMINAL),
            fallbackText = SafeDisplayText.sanitize(block.fallbackText, SafeDisplayText.MAX_FALLBACK),
        )
        is FileBlock -> block.copy(
            label = SafeDisplayText.fileLabel(block.label, "文件"),
            detail = block.detail?.let { SafeDisplayText.sanitize(it, SafeDisplayText.MAX_DETAIL) },
            language = block.language?.let { SafeDisplayText.sanitize(it, MAX_UNIT_BYTES) },
            fallbackText = SafeDisplayText.sanitize(block.fallbackText, SafeDisplayText.MAX_FALLBACK),
        )
        is DiffBlock -> {
            if (
                block.additions?.let { !isValidEvidenceCount(it) } == true ||
                block.deletions?.let { !isValidEvidenceCount(it) } == true
            ) return null
            block.copy(
                fileLabel = SafeDisplayText.fileLabel(block.fileLabel, "变更内容"),
                summary = SafeDisplayText.sanitize(block.summary, SafeDisplayText.MAX_DETAIL),
                preview = block.preview?.let { SafeDisplayText.sanitize(it, SafeDisplayText.MAX_DIFF) },
                truncated = block.truncated ||
                    (block.preview?.let { SafeDisplayText.exceedsUtf8Limit(it, SafeDisplayText.MAX_DIFF) } ?: false),
                fallbackText = SafeDisplayText.sanitize(block.fallbackText, SafeDisplayText.MAX_FALLBACK),
            )
        }
        is ToolBlock -> block.copy(
            label = SafeDisplayText.sanitize(block.label, SafeDisplayText.MAX_LABEL),
            detail = block.detail?.let { SafeDisplayText.sanitize(it, SafeDisplayText.MAX_DETAIL) },
            durationLabel = block.durationLabel?.let { SafeDisplayText.sanitize(it, MAX_UNIT_BYTES) },
            fallbackText = SafeDisplayText.sanitize(block.fallbackText, SafeDisplayText.MAX_FALLBACK),
        )
        is TestBlock -> {
            if (
                block.passed?.let { !isValidEvidenceCount(it) } == true ||
                block.failed?.let { !isValidEvidenceCount(it) } == true ||
                block.skipped?.let { !isValidEvidenceCount(it) } == true
            ) return null
            block.copy(
                title = SafeDisplayText.sanitize(block.title, SafeDisplayText.MAX_LABEL),
                summary = SafeDisplayText.sanitize(block.summary, SafeDisplayText.MAX_DETAIL),
                durationLabel = block.durationLabel?.let { SafeDisplayText.sanitize(it, MAX_UNIT_BYTES) },
                fallbackText = SafeDisplayText.sanitize(block.fallbackText, SafeDisplayText.MAX_FALLBACK),
            )
        }
        is InteractiveCollectionBlock -> {
            val allItems = block.groups.flatMap(InteractiveCollectionGroup::items)
            if (
                block.groups.isEmpty() ||
                block.groups.size > MAX_INTERACTIVE_GROUPS ||
                allItems.isEmpty() ||
                allItems.size > MAX_INTERACTIVE_ITEMS ||
                block.minorUnitScale !in ALLOWED_MINOR_SCALES ||
                block.groups.any { group ->
                    group.items.isEmpty() ||
                        group.items.size > MAX_BLOCK_ITEMS ||
                        !isSafeOpaque(group.id, MAX_EVENT_ID_BYTES)
                } ||
                block.groups.map(InteractiveCollectionGroup::id).distinct().size != block.groups.size ||
                allItems.any { item ->
                    !isSafeOpaque(item.id, MAX_EVENT_ID_BYTES) ||
                        item.unitPriceMinor !in -1L..MAX_INTERACTIVE_PRICE_MINOR
                } ||
                allItems.map(InteractiveCollectionItem::id).distinct().size != allItems.size
            ) return null
            block.copy(
                title = SafeDisplayText.sanitize(block.title, SafeDisplayText.MAX_LABEL),
                subtitle = SafeDisplayText.sanitize(block.subtitle, SafeDisplayText.MAX_DETAIL),
                confirmLabel = SafeDisplayText.sanitize(block.confirmLabel, MAX_SHORT_VALUE_BYTES),
                quantityUnit = SafeDisplayText.sanitize(block.quantityUnit, MAX_UNIT_BYTES),
                presentationTitle = SafeDisplayText.sanitize(
                    block.presentationTitle,
                    SafeDisplayText.MAX_LABEL,
                ),
                presentationSubtitle = SafeDisplayText.sanitize(
                    block.presentationSubtitle,
                    SafeDisplayText.MAX_DETAIL,
                ),
                presentationTotalLabel = SafeDisplayText.sanitize(
                    block.presentationTotalLabel,
                    MAX_SHORT_VALUE_BYTES,
                ),
                presentationNote = SafeDisplayText.sanitize(
                    block.presentationNote,
                    SafeDisplayText.MAX_DETAIL,
                ),
                currencyLabel = SafeDisplayText.sanitize(block.currencyLabel, MAX_UNIT_BYTES),
                groups = block.groups.map { group ->
                    group.copy(
                        title = SafeDisplayText.sanitize(group.title, SafeDisplayText.MAX_LABEL),
                        items = group.items.map { item ->
                            item.copy(
                                primary = SafeDisplayText.sanitize(item.primary, SafeDisplayText.MAX_LABEL),
                                secondary = SafeDisplayText.sanitize(item.secondary, SafeDisplayText.MAX_LABEL),
                                detail = SafeDisplayText.sanitize(item.detail, SafeDisplayText.MAX_DETAIL),
                                priceLabel = SafeDisplayText.sanitize(item.priceLabel, MAX_SHORT_VALUE_BYTES),
                                imageQuery = SafeDisplayText.sanitize(item.imageQuery, SafeDisplayText.MAX_DETAIL),
                                imageLabel = SafeDisplayText.sanitize(item.imageLabel, SafeDisplayText.MAX_LABEL),
                            )
                        },
                    )
                },
                fallbackText = SafeDisplayText.sanitize(
                    block.fallbackText,
                    SafeDisplayText.MAX_FALLBACK,
                ),
            )
        }
        is ReferenceGalleryBlock -> {
            if (
                block.items.isEmpty() ||
                block.items.size > MAX_REFERENCE_GALLERY_ITEMS ||
                block.items.any { !isSafeOpaque(it.id, MAX_EVENT_ID_BYTES) } ||
                block.items.map(ReferenceImageItem::id).distinct().size != block.items.size
            ) return null
            block.copy(
                title = SafeDisplayText.sanitize(block.title, SafeDisplayText.MAX_LABEL),
                subtitle = SafeDisplayText.sanitize(block.subtitle, SafeDisplayText.MAX_DETAIL),
                items = block.items.map { item ->
                    item.copy(
                        title = SafeDisplayText.sanitize(item.title, SafeDisplayText.MAX_LABEL),
                        caption = SafeDisplayText.sanitize(item.caption, SafeDisplayText.MAX_DETAIL),
                        imageQuery = SafeDisplayText.sanitize(item.imageQuery, SafeDisplayText.MAX_DETAIL),
                        imageLabel = SafeDisplayText.sanitize(item.imageLabel, SafeDisplayText.MAX_LABEL),
                    )
                },
                fallbackText = SafeDisplayText.sanitize(block.fallbackText, SafeDisplayText.MAX_FALLBACK),
            )
        }
        is ComparisonBlock -> {
            if (
                block.items.size !in 2..MAX_COMPARISON_ITEMS ||
                block.items.any { item ->
                    !isSafeOpaque(item.id, MAX_EVENT_ID_BYTES) ||
                        item.facts.size > MAX_COMPARISON_FACTS
                } ||
                block.items.map(ComparisonItem::id).distinct().size != block.items.size
            ) return null
            block.copy(
                title = SafeDisplayText.sanitize(block.title, SafeDisplayText.MAX_LABEL),
                subtitle = SafeDisplayText.sanitize(block.subtitle, SafeDisplayText.MAX_DETAIL),
                selectLabel = SafeDisplayText.sanitize(block.selectLabel, MAX_SHORT_VALUE_BYTES),
                items = block.items.map { item ->
                    item.copy(
                        primary = SafeDisplayText.sanitize(item.primary, SafeDisplayText.MAX_LABEL),
                        secondary = SafeDisplayText.sanitize(item.secondary, SafeDisplayText.MAX_LABEL),
                        detail = SafeDisplayText.sanitize(item.detail, SafeDisplayText.MAX_DETAIL),
                        badge = SafeDisplayText.sanitize(item.badge, MAX_SHORT_VALUE_BYTES),
                        facts = item.facts.map { fact ->
                            fact.copy(
                                label = SafeDisplayText.sanitize(fact.label, MAX_SHORT_VALUE_BYTES),
                                value = SafeDisplayText.sanitize(fact.value, SafeDisplayText.MAX_LABEL),
                            )
                        },
                        imageQuery = SafeDisplayText.sanitize(item.imageQuery, SafeDisplayText.MAX_DETAIL),
                        imageLabel = SafeDisplayText.sanitize(item.imageLabel, SafeDisplayText.MAX_LABEL),
                    )
                },
                fallbackText = SafeDisplayText.sanitize(block.fallbackText, SafeDisplayText.MAX_FALLBACK),
            )
        }
        is BilingualCardBlock -> {
            if (
                block.lines.isEmpty() ||
                block.lines.size > MAX_BILINGUAL_LINES ||
                block.lines.any { !isSafeOpaque(it.id, MAX_EVENT_ID_BYTES) } ||
                block.lines.map(BilingualLine::id).distinct().size != block.lines.size
            ) return null
            block.copy(
                titleOriginal = SafeDisplayText.sanitize(block.titleOriginal, SafeDisplayText.MAX_LABEL),
                titleChinese = SafeDisplayText.sanitize(block.titleChinese, SafeDisplayText.MAX_LABEL),
                contextOriginal = SafeDisplayText.sanitize(block.contextOriginal, SafeDisplayText.MAX_DETAIL),
                contextChinese = SafeDisplayText.sanitize(block.contextChinese, SafeDisplayText.MAX_DETAIL),
                showLabel = SafeDisplayText.sanitize(block.showLabel, MAX_SHORT_VALUE_BYTES),
                lines = block.lines.map { line ->
                    line.copy(
                        original = SafeDisplayText.sanitize(line.original, SafeDisplayText.MAX_DETAIL),
                        translation = SafeDisplayText.sanitize(line.translation, SafeDisplayText.MAX_DETAIL),
                        note = SafeDisplayText.sanitize(line.note, SafeDisplayText.MAX_DETAIL),
                    )
                },
                fallbackText = SafeDisplayText.sanitize(block.fallbackText, SafeDisplayText.MAX_FALLBACK),
            )
        }
        is UnknownBlock -> block.copy(
            originalType = block.originalType.takeIf { isSafeOpaque(it, MAX_UNKNOWN_TYPE_BYTES) }
                ?: return null,
            fallbackText = SafeDisplayText.sanitize(block.fallbackText, SafeDisplayText.MAX_FALLBACK),
        )
        }
    }

    private fun isSafeOpaque(value: String, maxBytes: Int): Boolean =
        value.isNotBlank() &&
            !SafeDisplayText.exceedsUtf8Limit(value, maxBytes) &&
            value.none { character ->
                character.isWhitespace() || character.isISOControl() ||
                    Character.getType(character) == Character.FORMAT.toInt()
            }

    private fun isSafeAction(item: ActionItem): Boolean {
        if (
            !isSafeOpaque(item.id, MAX_EVENT_ID_BYTES) ||
            !isSafeOpaque(item.actionRef, MAX_ACTION_REF_BYTES) ||
            item.retryNotBeforeMillis !in 0L..MAX_ACTION_RETRY_NOT_BEFORE_MILLIS
        ) {
            return false
        }
        return item.actionRef.substringBefore(':', missingDelimiterValue = "").lowercase() !in
            FORBIDDEN_ACTION_SCHEMES
    }

    private fun isValidEvidenceCount(value: Int): Boolean = value in 0..MAX_EVIDENCE_COUNT

    private fun safeSemanticCommandLabel(value: String): String? {
        val sanitized = SafeDisplayText.sanitize(value, SafeDisplayText.MAX_LABEL)
        val looksLikeRawCommand = sanitized.any { it in "\\/$;|&><\"'`" } ||
            sanitized.contains("--") || sanitized.contains('=') || sanitized.contains('\n')
        return sanitized.takeUnless { looksLikeRawCommand }
    }

    private fun patchValueWithinLimits(root: Map<String, Any?>): Boolean {
        data class Pending(val value: Any?, val depth: Int)

        val pending = ArrayDeque<Pending>()
        pending.add(Pending(root, 1))
        var nodes = 0
        while (pending.isNotEmpty()) {
            val (value, depth) = pending.removeLast()
            nodes += 1
            if (nodes > MAX_PATCH_VALUE_NODES || depth > MAX_PATCH_VALUE_DEPTH) return false
            when (value) {
                null, is Boolean, is Number, is Tone, is StepState, is ActionKind -> Unit
                is String -> if (SafeDisplayText.exceedsUtf8Limit(value, MAX_STREAM_TEXT_BYTES)) return false
                is Map<*, *> -> {
                    if (value.size > MAX_PATCH_MAP_FIELDS || value.keys.any { it !is String }) return false
                    value.values.forEach { pending.add(Pending(it, depth + 1)) }
                }
                is List<*> -> {
                    if (value.size > MAX_BLOCK_ITEMS) return false
                    value.forEach { pending.add(Pending(it, depth + 1)) }
                }
                else -> return false
            }
        }
        return true
    }

    private fun mergeBlock(block: RichBlock, value: Map<String, Any?>): RichBlock? {
        return when (block) {
        is TextBlock -> block.copy(
            text = value.string("text", block.text),
            markdown = value.boolean("markdown", block.markdown),
            fallbackText = value.string("fallbackText", block.fallbackText),
        )
        is StatusBlock -> {
            if (!value.hasValidProgressPatch("progress")) return null
            block.copy(
                label = value.string("label", block.label),
                detail = value.nullableString("detail", block.detail),
                tone = value.tone("tone", block.tone),
                progress = value.nullableFloat("progress", block.progress),
                fallbackText = value.string("fallbackText", block.fallbackText),
            )
        }
        is StepsBlock -> {
            val items = if ("items" in value) {
                value.stepItems("items") ?: return null
            } else {
                block.items
            }
            val activeItemId = value["activeItemId"] as? String
            block.copy(
                title = value.string("title", block.title),
                items = if (activeItemId == null) items else items.map { item ->
                    when {
                        item.id == activeItemId -> item.copy(state = StepState.ACTIVE)
                        item.state == StepState.ACTIVE -> item.copy(state = StepState.PENDING)
                        else -> item
                    }
                },
                fallbackText = value.string("fallbackText", block.fallbackText),
            )
        }
        is MetricsBlock -> block.copy(
            items = if ("items" in value) value.metricItems("items") ?: return null else block.items,
            fallbackText = value.string("fallbackText", block.fallbackText),
        )
        is FactsBlock -> block.copy(
            items = if ("items" in value) value.factItems("items") ?: return null else block.items,
            fallbackText = value.string("fallbackText", block.fallbackText),
        )
        is MediaBlock -> {
            val assetId = value.string("assetId", block.assetId)
            if (!isSafeMediaAssetId(assetId)) return null
            block.copy(
                assetId = assetId,
                caption = value.safeNullableString("caption", block.caption, SafeDisplayText.MAX_LABEL),
                fallbackText = value.safeString(
                    "fallbackText",
                    block.fallbackText,
                    SafeDisplayText.MAX_FALLBACK,
                ),
            )
        }
        is SourceBlock -> block.copy(
            label = value.string("label", block.label),
            detail = value.string("detail", block.detail),
            trustLabel = value.nullableString("trustLabel", block.trustLabel),
            fallbackText = value.string("fallbackText", block.fallbackText),
        )
        is RelayBlock -> {
            val nodes = if ("nodes" in value) {
                value.relayNodes("nodes") ?: return null
            } else {
                block.nodes
            }
            val activeNodeId = value["activeNodeId"] as? String
            block.copy(
                title = value.string("title", block.title),
                nodes = if (activeNodeId == null) nodes else nodes.map { node ->
                    when {
                        node.id == activeNodeId -> node.copy(state = StepState.ACTIVE)
                        node.state == StepState.ACTIVE -> node.copy(state = StepState.PENDING)
                        else -> node
                    }
                },
                fallbackText = value.string("fallbackText", block.fallbackText),
            )
        }
        is NoticeBlock -> block.copy(
            title = value.string("title", block.title),
            body = value.string("body", block.body),
            tone = value.tone("tone", block.tone),
            fallbackText = value.string("fallbackText", block.fallbackText),
        )
        is ActionsBlock -> block.copy(
            items = if ("items" in value) value.actionItems("items") ?: return null else block.items,
            fallbackText = value.string("fallbackText", block.fallbackText),
        )
        is TerminalBlock -> {
            val outputRaw = value.string("output", value.string("text", block.output))
            val output = SafeDisplayText.sanitize(outputRaw, SafeDisplayText.MAX_TERMINAL)
            block.copy(
                title = value.safeString("title", block.title, SafeDisplayText.MAX_LABEL),
                commandLabel = value.safeSemanticCommandLabel("commandLabel", block.commandLabel),
                output = output,
                state = value.stepState("state", block.state),
                truncated = value.boolean("truncated", block.truncated) ||
                    SafeDisplayText.exceedsUtf8Limit(outputRaw, SafeDisplayText.MAX_TERMINAL),
                fallbackText = value.safeString(
                    "fallbackText",
                    block.fallbackText,
                    SafeDisplayText.MAX_FALLBACK,
                ),
            )
        }
        is FileBlock -> block.copy(
            label = value.safeFileLabel(block.label),
            change = value.fileChange("change", value.fileChange("operation", block.change)),
            detail = value.safeNullableString("detail", block.detail, SafeDisplayText.MAX_DETAIL),
            language = value.safeNullableString("language", block.language, 64),
            state = value.stepState("state", block.state),
            fallbackText = value.safeString("fallbackText", block.fallbackText, SafeDisplayText.MAX_FALLBACK),
        )
        is DiffBlock -> {
            if (!value.hasValidEvidenceCountPatch("additions") || !value.hasValidEvidenceCountPatch("deletions")) {
                return null
            }
            val previewRaw = value.nullableString("preview", value.nullableString("text", block.preview))
            block.copy(
                fileLabel = value.safeFileLabel(block.fileLabel),
                summary = value.safeString("summary", block.summary, SafeDisplayText.MAX_DETAIL),
                preview = previewRaw?.let { SafeDisplayText.sanitize(it, SafeDisplayText.MAX_DIFF) },
                additions = value.nonNegativeInt("additions", block.additions),
                deletions = value.nonNegativeInt("deletions", block.deletions),
                truncated = value.boolean("truncated", block.truncated) ||
                    (previewRaw?.let { SafeDisplayText.exceedsUtf8Limit(it, SafeDisplayText.MAX_DIFF) } ?: false),
                fallbackText = value.safeString(
                    "fallbackText",
                    block.fallbackText,
                    SafeDisplayText.MAX_FALLBACK,
                ),
            )
        }
        is ToolBlock -> block.copy(
            label = value.safeString("label", block.label, SafeDisplayText.MAX_LABEL),
            detail = value.safeNullableString("detail", block.detail, SafeDisplayText.MAX_DETAIL),
            state = value.stepState("state", block.state),
            durationLabel = value.safeNullableString("durationLabel", block.durationLabel, 64),
            fallbackText = value.safeString("fallbackText", block.fallbackText, SafeDisplayText.MAX_FALLBACK),
        )
        is TestBlock -> {
            if (
                !value.hasValidEvidenceCountPatch("passed") ||
                !value.hasValidEvidenceCountPatch("failed") ||
                !value.hasValidEvidenceCountPatch("skipped")
            ) return null
            block.copy(
                title = value.safeString("title", block.title, SafeDisplayText.MAX_LABEL),
                summary = value.safeString("summary", block.summary, SafeDisplayText.MAX_DETAIL),
                passed = value.nonNegativeInt("passed", block.passed),
                failed = value.nonNegativeInt("failed", block.failed),
                skipped = value.nonNegativeInt("skipped", block.skipped),
                durationLabel = value.safeNullableString("durationLabel", block.durationLabel, 64),
                state = value.stepState("state", block.state),
                fallbackText = value.safeString("fallbackText", block.fallbackText, SafeDisplayText.MAX_FALLBACK),
            )
        }
        is InteractiveCollectionBlock -> block.copy(
            title = value.safeString("title", block.title, SafeDisplayText.MAX_LABEL),
            subtitle = value.safeString("subtitle", block.subtitle, SafeDisplayText.MAX_DETAIL),
            confirmLabel = value.safeString("confirmLabel", block.confirmLabel, MAX_SHORT_VALUE_BYTES),
            quantityUnit = value.safeString("quantityUnit", block.quantityUnit, MAX_UNIT_BYTES),
            presentationTitle = value.safeString(
                "presentationTitle",
                block.presentationTitle,
                SafeDisplayText.MAX_LABEL,
            ),
            presentationSubtitle = value.safeString(
                "presentationSubtitle",
                block.presentationSubtitle,
                SafeDisplayText.MAX_DETAIL,
            ),
            presentationTotalLabel = value.safeString(
                "presentationTotalLabel",
                block.presentationTotalLabel,
                MAX_SHORT_VALUE_BYTES,
            ),
            presentationNote = value.safeString(
                "presentationNote",
                block.presentationNote,
                SafeDisplayText.MAX_DETAIL,
            ),
            fallbackText = value.safeString(
                "fallbackText",
                block.fallbackText,
                SafeDisplayText.MAX_FALLBACK,
            ),
        )
        is ReferenceGalleryBlock -> block.copy(
            title = value.safeString("title", block.title, SafeDisplayText.MAX_LABEL),
            subtitle = value.safeString("subtitle", block.subtitle, SafeDisplayText.MAX_DETAIL),
            fallbackText = value.safeString(
                "fallbackText",
                block.fallbackText,
                SafeDisplayText.MAX_FALLBACK,
            ),
        )
        is ComparisonBlock -> block.copy(
            title = value.safeString("title", block.title, SafeDisplayText.MAX_LABEL),
            subtitle = value.safeString("subtitle", block.subtitle, SafeDisplayText.MAX_DETAIL),
            selectLabel = value.safeString("selectLabel", block.selectLabel, MAX_SHORT_VALUE_BYTES),
            fallbackText = value.safeString(
                "fallbackText",
                block.fallbackText,
                SafeDisplayText.MAX_FALLBACK,
            ),
        )
        is BilingualCardBlock -> block.copy(
            titleOriginal = value.safeString(
                "titleOriginal",
                block.titleOriginal,
                SafeDisplayText.MAX_LABEL,
            ),
            titleChinese = value.safeString(
                "titleChinese",
                block.titleChinese,
                SafeDisplayText.MAX_LABEL,
            ),
            contextOriginal = value.safeString(
                "contextOriginal",
                block.contextOriginal,
                SafeDisplayText.MAX_DETAIL,
            ),
            contextChinese = value.safeString(
                "contextChinese",
                block.contextChinese,
                SafeDisplayText.MAX_DETAIL,
            ),
            showLabel = value.safeString("showLabel", block.showLabel, MAX_SHORT_VALUE_BYTES),
            fallbackText = value.safeString(
                "fallbackText",
                block.fallbackText,
                SafeDisplayText.MAX_FALLBACK,
            ),
        )
        is UnknownBlock -> block.copy(
            fallbackText = value.safeString(
                "fallbackText",
                block.fallbackText,
                SafeDisplayText.MAX_FALLBACK,
            ),
        )
        }
    }

    private fun Map<String, Any?>.string(key: String, fallback: String): String =
        (this[key] as? String) ?: fallback

    private const val MAX_STREAM_TEXT_BYTES = 65_536
    private const val MAX_MESSAGE_BLOCKS = 128
    private const val MAX_MESSAGES = 10_000
    private const val MAX_ROOT_BLOCKS = 64
    private const val MAX_PATCH_OPERATIONS = 64
    private const val MAX_EVENT_ID_BYTES = 256
    private const val MAX_BLOCK_ITEMS = 64
    private const val MAX_ACTION_ITEMS = 8
    private const val MAX_SHORT_VALUE_BYTES = 128
    private const val MAX_UNIT_BYTES = 64
    private const val MAX_PATCH_VALUE_NODES = 4_096
    private const val MAX_PATCH_VALUE_DEPTH = 8
    private const val MAX_PATCH_MAP_FIELDS = 512
    private const val MAX_ACTION_REF_BYTES = 512
    private const val MAX_UNKNOWN_TYPE_BYTES = 96
    private const val MAX_INTERACTIVE_GROUPS = 32
    private const val MAX_INTERACTIVE_ITEMS = 256
    private const val MAX_INTERACTIVE_PRICE_MINOR = 1_000_000_000_000L
    private const val MAX_REFERENCE_GALLERY_ITEMS = 8
    private const val MAX_COMPARISON_ITEMS = 12
    private const val MAX_COMPARISON_FACTS = 8
    private const val MAX_BILINGUAL_LINES = 24
    private const val MAX_ACTION_RETRY_NOT_BEFORE_MILLIS = 253_402_300_799_999L
    private val ALLOWED_MINOR_SCALES = setOf(1, 10, 100, 1_000)
    private val FORBIDDEN_ACTION_SCHEMES = setOf("javascript", "data", "file", "content", "intent")

    private fun Map<String, Any?>.boolean(key: String, fallback: Boolean): Boolean =
        (this[key] as? Boolean) ?: fallback

    private fun Map<String, Any?>.nullableString(key: String, fallback: String?): String? =
        if (containsKey(key)) this[key] as? String else fallback

    private fun Map<String, Any?>.nullableFloat(key: String, fallback: Float?): Float? =
        if (containsKey(key)) (this[key] as? Number)?.toFloat() else fallback

    private fun Map<String, Any?>.hasValidProgressPatch(key: String): Boolean {
        if (!containsKey(key) || this[key] == null) return true
        val value = (this[key] as? Number)?.toDouble() ?: return false
        return value.isFinite() && value in 0.0..1.0
    }

    private fun Map<String, Any?>.hasValidEvidenceCountPatch(key: String): Boolean {
        if (!containsKey(key) || this[key] == null) return true
        val value = (this[key] as? Number)?.exactLongOrNull() ?: return false
        return value in 0L..MAX_EVIDENCE_COUNT.toLong()
    }

    private fun Number.exactLongOrNull(): Long? = when (this) {
        is Byte, is Short, is Int, is Long -> toLong()
        is Float -> takeIf(Float::isFinite)?.toLong()?.takeIf { it.toFloat() == this }
        is Double -> takeIf(Double::isFinite)?.toLong()?.takeIf { it.toDouble() == this }
        else -> null
    }

    private fun Map<String, Any?>.safeString(key: String, fallback: String, maxLength: Int): String =
        SafeDisplayText.sanitize(string(key, fallback), maxLength)

    private fun Map<String, Any?>.safeNullableString(
        key: String,
        fallback: String?,
        maxLength: Int,
    ): String? = nullableString(key, fallback)?.let { SafeDisplayText.sanitize(it, maxLength) }

    private fun Map<String, Any?>.safeSemanticCommandLabel(key: String, fallback: String?): String? {
        val raw = nullableString(key, fallback) ?: return null
        return safeSemanticCommandLabel(raw)
    }

    private fun Map<String, Any?>.safeFileLabel(fallback: String): String {
        val candidate = (this["fileLabel"] as? String)
            ?: (this["label"] as? String)
            ?: (this["name"] as? String)
            ?: (this["path"] as? String)
            ?: fallback
        return SafeDisplayText.fileLabel(candidate, fallback)
    }

    private fun Map<String, Any?>.fileChange(key: String, fallback: FileChangeKind): FileChangeKind =
        when ((this[key] as? String)?.lowercase()) {
            "create", "created", "add", "added" -> FileChangeKind.CREATED
            "modify", "modified", "update", "updated" -> FileChangeKind.MODIFIED
            "delete", "deleted", "remove", "removed" -> FileChangeKind.DELETED
            "rename", "renamed", "move", "moved" -> FileChangeKind.RENAMED
            "read", "viewed" -> FileChangeKind.READ
            null -> fallback
            else -> FileChangeKind.UNKNOWN
        }

    private fun Map<String, Any?>.nonNegativeInt(key: String, fallback: Int?): Int? {
        val value = (this[key] as? Number)?.toLong() ?: return fallback
        return value.takeIf { it in 0L..MAX_EVIDENCE_COUNT.toLong() }?.toInt() ?: fallback
    }

    private fun Map<String, Any?>.tone(key: String, fallback: Tone): Tone = when (val raw = this[key]) {
        is Tone -> raw
        is String -> runCatching { Tone.valueOf(raw.uppercase()) }.getOrDefault(fallback)
        else -> fallback
    }

    private fun Map<String, Any?>.stepItems(key: String): List<StepItem>? {
        val raw = strictListOfMaps(key, MAX_BLOCK_ITEMS) ?: return null
        val result = ArrayList<StepItem>(raw.size)
        raw.forEach { item ->
            val id = item["id"] as? String ?: return null
            val label = item["label"] as? String ?: return null
            val detail = item["detail"]?.let { it as? String ?: return null }
            result += StepItem(id, label, item.stepState("state", StepState.PENDING), detail)
        }
        return result
    }

    private fun Map<String, Any?>.metricItems(key: String): List<MetricItem>? {
        val raw = strictListOfMaps(key, MAX_BLOCK_ITEMS) ?: return null
        val result = ArrayList<MetricItem>(raw.size)
        raw.forEach { item ->
            val label = item["label"] as? String ?: return null
            val metricValue = item["value"] as? String ?: return null
            val unit = item["unit"]?.let { it as? String ?: return null }
            result += MetricItem(label, metricValue, unit)
        }
        return result
    }

    private fun Map<String, Any?>.factItems(key: String): List<FactItem>? {
        val raw = strictListOfMaps(key, MAX_BLOCK_ITEMS) ?: return null
        val result = ArrayList<FactItem>(raw.size)
        raw.forEach { item ->
            val label = item["label"] as? String ?: return null
            val factValue = item["value"] as? String ?: return null
            val note = item["note"]?.let { it as? String ?: return null }
            result += FactItem(label, factValue, note)
        }
        return result
    }

    private fun Map<String, Any?>.relayNodes(key: String): List<RelayNode>? {
        val raw = strictListOfMaps(key, MAX_BLOCK_ITEMS) ?: return null
        val result = ArrayList<RelayNode>(raw.size)
        raw.forEach { item ->
            val id = item["id"] as? String ?: return null
            val label = item["label"] as? String ?: return null
            val detail = item["detail"]?.let { it as? String ?: return null }
            result += RelayNode(id, label, detail, item.stepState("state", StepState.PENDING))
        }
        return result
    }

    private fun Map<String, Any?>.actionItems(key: String): List<ActionItem>? {
        val raw = strictListOfMaps(key, MAX_ACTION_ITEMS) ?: return null
        val result = ArrayList<ActionItem>(raw.size)
        raw.forEach { item ->
            val id = item["id"] as? String ?: return null
            val label = item["label"] as? String ?: return null
            val actionRef = item["actionRef"] as? String ?: return null
            val enabled = item["enabled"]?.let { it as? Boolean ?: return null } ?: true
            result += ActionItem(
                id = id,
                label = label,
                actionRef = actionRef,
                kind = when (val rawKind = item["kind"]) {
                    is ActionKind -> rawKind
                    is String -> runCatching { ActionKind.valueOf(rawKind.uppercase()) }
                        .getOrElse { return null }
                    null -> ActionKind.PRIMARY
                    else -> return null
                },
                enabled = enabled,
            )
        }
        return result
    }

    @Suppress("UNCHECKED_CAST")
    private fun Map<String, Any?>.strictListOfMaps(
        key: String,
        maxItems: Int,
    ): List<Map<String, Any?>>? {
        val raw = this[key] as? List<*> ?: return null
        if (raw.size > maxItems || raw.any { it !is Map<*, *> }) return null
        return raw.map { it as Map<String, Any?> }
    }

    private fun Map<String, Any?>.stepState(key: String, fallback: StepState): StepState = when (val raw = this[key]) {
        is StepState -> raw
        "running", "active" -> StepState.ACTIVE
        "completed" -> StepState.COMPLETED
        "failed" -> StepState.FAILED
        "pending" -> StepState.PENDING
        else -> fallback
    }

    private const val MAX_EVIDENCE_COUNT = 1_000_000
}
