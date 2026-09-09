package com.cheby.codex.mobile.model

import com.cheby.codex.mobile.gateway.completedRelayFixture
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PatchReducerTest {
    private val threadKey = ThreadKey("public-thread-key")
    private val initialMessage = RichMessage(
        id = "message-1",
        role = MessageRole.ASSISTANT,
        state = RichMessageState.STREAMING,
        revision = 0,
        rootBlockIds = listOf("status"),
        blocks = mapOf("status" to StatusBlock("status", "开始")),
        createdAtMillis = 1,
    )
    private val initial = ThreadConversationState(
        threadKey = threadKey,
        streamId = "stream-a",
        streamSeq = 4,
        messages = listOf(initialMessage),
    )

    @Test
    fun `duplicate event id is idempotent`() {
        val event = patch(eventId = "event-5", streamSeq = 5, label = "执行中")
        val once = ConversationReducer.reduce(initial, event)
        val twice = ConversationReducer.reduceWithDisposition(once, event)

        assertEquals(1, once.messages.single().revision)
        assertEquals(once, twice.state)
        assertEquals(ConversationReductionDisposition.EXACT_DUPLICATE, twice.disposition)
        assertFalse(twice.state.resyncRequired)
    }

    @Test
    fun `same event id at expected next sequence is a conflict`() {
        val first = patch(eventId = "event-stable", streamSeq = 5, label = "执行中")
        val once = ConversationReducer.reduce(initial, first)
        val conflicting = ConversationReducer.reduce(
            once,
            ConversationEvent.NoOp(
                eventId = first.eventId,
                streamId = first.streamId,
                streamSeq = 6,
            ),
        )

        assertTrue(conflicting.resyncRequired)
        assertEquals(5, conflicting.streamSeq)
    }

    @Test
    fun `same event id from a different stream is a conflict before duplicate handling`() {
        val first = patch(eventId = "event-stable", streamSeq = 5, label = "执行中")
        val once = ConversationReducer.reduce(initial, first)
        val conflicting = ConversationReducer.reduce(
            once,
            first.copy(streamId = "stream-b"),
        )

        assertTrue(conflicting.resyncRequired)
        assertEquals("stream-a", conflicting.streamId)
        assertEquals(5, conflicting.streamSeq)
    }

    @Test
    fun `old sequence with a different event id is not accepted as a duplicate`() {
        val once = ConversationReducer.reduce(
            initial,
            patch(eventId = "event-5", streamSeq = 5, label = "执行中"),
        )
        val conflicting = ConversationReducer.reduce(
            once,
            ConversationEvent.NoOp("different-id", "stream-a", 4),
        )

        assertTrue(conflicting.resyncRequired)
        assertEquals(5, conflicting.streamSeq)
    }

    @Test
    fun `maximum cursor permits only an exact registered duplicate`() {
        val identity = ProcessedEventIdentity("stream-a", Long.MAX_VALUE)
        val exhausted = initial.copy(
            streamSeq = Long.MAX_VALUE,
            processedEvents = mapOf("last-event" to identity),
        )

        val exact = ConversationReducer.reduceWithDisposition(
            exhausted,
            ConversationEvent.NoOp("last-event", "stream-a", Long.MAX_VALUE),
        )
        val wrapped = ConversationReducer.reduce(
            exhausted,
            ConversationEvent.NoOp("next-event", "stream-a", Long.MIN_VALUE),
        )

        assertEquals(ConversationReductionDisposition.EXACT_DUPLICATE, exact.disposition)
        assertEquals(exhausted, exact.state)
        assertTrue(wrapped.resyncRequired)
        assertEquals(Long.MAX_VALUE, wrapped.streamSeq)
    }

    @Test
    fun `sequence gap requires resync and does not apply patch`() {
        val result = ConversationReducer.reduce(
            initial,
            patch(eventId = "event-6", streamSeq = 6, label = "不应应用"),
        )

        assertTrue(result.resyncRequired)
        assertEquals("开始", (result.messages.single().blocks["status"] as StatusBlock).label)
        assertEquals(0, result.messages.single().revision)
    }

    @Test
    fun `stream change requires resync even when sequence looks continuous`() {
        val result = ConversationReducer.reduce(
            initial,
            patch(eventId = "event-5", streamSeq = 5, label = "新流").copy(streamId = "stream-b"),
        )

        assertTrue(result.resyncRequired)
        assertEquals("stream-a", result.streamId)
    }

    @Test
    fun `revision mismatch requires resync`() {
        val result = ConversationReducer.reduce(
            initial,
            patch(eventId = "event-5", streamSeq = 5, label = "越级").copy(baseRevision = 3, revision = 4),
        )

        assertTrue(result.resyncRequired)
        assertEquals(0, result.messages.single().revision)
    }

    @Test
    fun `completed fixture leaves relay static and fully completed`() {
        val relay = completedRelayFixture()
        val result = ConversationReducer.reduce(
            initial,
            ConversationEvent.MessagePatched(
                eventId = "event-5",
                streamId = "stream-a",
                streamSeq = 5,
                messageId = initialMessage.id,
                baseRevision = 0,
                revision = 1,
                operations = listOf(
                    PatchOperation.PutBlock(relay),
                    PatchOperation.SetRootBlocks(listOf("status", "relay")),
                    PatchOperation.SetMessageState(RichMessageState.COMPLETED),
                ),
            ),
        )

        assertFalse(result.resyncRequired)
        val reducedRelay = result.messages.single().blocks["relay"] as RelayBlock
        assertTrue(reducedRelay.nodes.all { it.state == StepState.COMPLETED })
        assertTrue(reducedRelay.nodes.all { it.detail == null })
    }

    @Test
    fun `completed message with active relay requires resync`() {
        val result = ConversationReducer.reduce(
            initial,
            ConversationEvent.MessagePatched(
                eventId = "event-5",
                streamId = "stream-a",
                streamSeq = 5,
                messageId = initialMessage.id,
                baseRevision = 0,
                revision = 1,
                operations = listOf(
                    PatchOperation.PutBlock(
                        RelayBlock(
                            id = "relay",
                            title = "执行接力",
                            nodes = listOf(RelayNode("codex", "Codex", "正在处理", StepState.ACTIVE)),
                        ),
                    ),
                    PatchOperation.SetMessageState(RichMessageState.COMPLETED),
                ),
            ),
        )

        assertTrue(result.resyncRequired)
        assertEquals(0, result.messages.single().revision)
    }

    @Test
    fun `media block put and merge preserve an opaque reference and safe caption`() {
        val result = ConversationReducer.reduce(
            initial,
            ConversationEvent.MessagePatched(
                eventId = "event-5",
                streamId = "stream-a",
                streamSeq = 5,
                messageId = initialMessage.id,
                baseRevision = 0,
                revision = 1,
                operations = listOf(
                    PatchOperation.PutBlock(
                        MediaBlock(
                            id = "preview",
                            assetId = "asset_summary_preview",
                            caption = "Initial preview",
                        ),
                    ),
                    PatchOperation.MergeBlock(
                        blockId = "preview",
                        value = mapOf("caption" to "Updated preview"),
                    ),
                    PatchOperation.SetRootBlocks(listOf("status", "preview")),
                ),
            ),
        )

        assertFalse(result.resyncRequired)
        val media = result.messages.single().blocks.getValue("preview") as MediaBlock
        assertEquals("asset_summary_preview", media.assetId)
        assertEquals("Updated preview", media.caption)
    }

    @Test
    fun `media merge fails closed when a patch tries to replace asset id with a URL`() {
        val message = initialMessage.copy(
            blocks = initialMessage.blocks + (
                "preview" to MediaBlock("preview", "asset_summary_preview", "Preview")
            ),
            rootBlockIds = listOf("status", "preview"),
        )
        val result = ConversationReducer.reduce(
            initial.copy(messages = listOf(message)),
            ConversationEvent.MessagePatched(
                eventId = "event-5",
                streamId = "stream-a",
                streamSeq = 5,
                messageId = message.id,
                baseRevision = 0,
                revision = 1,
                operations = listOf(
                    PatchOperation.MergeBlock(
                        blockId = "preview",
                        value = mapOf("assetId" to "https://example.com/preview"),
                    ),
                ),
            ),
        )

        assertTrue(result.resyncRequired)
        assertEquals("asset_summary_preview", (result.messages.single().blocks["preview"] as MediaBlock).assetId)
    }

    private fun patch(
        eventId: String,
        streamSeq: Long,
        label: String,
    ) = ConversationEvent.MessagePatched(
        eventId = eventId,
        streamId = "stream-a",
        streamSeq = streamSeq,
        messageId = initialMessage.id,
        baseRevision = 0,
        revision = 1,
        operations = listOf(PatchOperation.PutBlock(StatusBlock("status", label))),
    )
}
