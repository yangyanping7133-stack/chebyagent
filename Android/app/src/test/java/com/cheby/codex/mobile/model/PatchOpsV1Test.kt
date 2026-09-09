package com.cheby.codex.mobile.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class PatchOpsV1Test {
    private val key = ThreadKey("thr_public")

    @Test
    fun `block put installs a typed block`() {
        val result = reduce(PatchOperation.PutBlock(StatusBlock("status", "Running")))

        assertEquals("Running", (result.message().blocks["status"] as StatusBlock).label)
    }

    @Test
    fun `block merge updates only supplied fields`() {
        val result = reduce(
            PatchOperation.PutBlock(StatusBlock("status", "Running", "safe", Tone.INFO, 0.2f)),
            PatchOperation.MergeBlock("status", mapOf("progress" to 0.8, "tone" to "success")),
        )
        val status = result.message().blocks["status"] as StatusBlock

        assertEquals("Running", status.label)
        assertEquals("safe", status.detail)
        assertEquals(Tone.SUCCESS, status.tone)
        assertEquals(0.8f, status.progress)
    }

    @Test
    fun `block text append preserves previous chunks`() {
        val result = reduce(
            PatchOperation.PutBlock(TextBlock("answer", "Hello")),
            PatchOperation.AppendBlockText("answer", " world"),
        )

        assertEquals("Hello world", (result.message().blocks["answer"] as TextBlock).text)
    }

    @Test
    fun `terminal append remains safe fallback text`() {
        val result = reduce(
            PatchOperation.PutBlock(UnknownBlock("terminal", "terminal", "12 checks")),
            PatchOperation.AppendTerminal("terminal", " passed"),
        )

        assertEquals("12 checks passed", result.message().blocks["terminal"]?.readableText())
    }

    @Test
    fun `block remove also removes root reference`() {
        val result = reduce(
            PatchOperation.PutBlock(TextBlock("answer", "Hello")),
            PatchOperation.SetRootBlocks(listOf("answer")),
            PatchOperation.RemoveBlock("answer"),
        )

        assertNull(result.message().blocks["answer"])
        assertTrue(result.message().rootBlockIds.isEmpty())
    }

    @Test
    fun `root set with an unknown block fails closed`() {
        val result = reduce(
            PatchOperation.PutBlock(TextBlock("one", "1")),
            PatchOperation.PutBlock(TextBlock("two", "2")),
            PatchOperation.SetRootBlocks(listOf("two", "missing", "one")),
        )

        assertTrue(result.resyncRequired)
        assertTrue(result.message().rootBlockIds.isEmpty())
    }

    @Test
    fun `message state set applies terminal state`() {
        val result = reduce(PatchOperation.SetMessageState(RichMessageState.COMPLETED))

        assertEquals(RichMessageState.COMPLETED, result.message().state)
    }

    @Test
    fun `action resolve disables the single action`() {
        val result = reduce(
            PatchOperation.PutBlock(
                ActionsBlock(
                    "actions",
                    listOf(ActionItem("retry", "Retry", "opaque-action-token")),
                ),
            ),
            PatchOperation.ResolveAction("actions", "retry"),
        )
        val actions = result.message().blocks["actions"] as ActionsBlock

        assertFalse(actions.items.single().enabled)
    }

    @Test
    fun `append to missing block requests snapshot resync`() {
        val result = reduce(PatchOperation.AppendBlockText("missing", "unsafe"))

        assertTrue(result.resyncRequired)
        assertEquals(0, result.message().revision)
    }

    @Test
    fun `streamed text cumulative overflow requests cold snapshot resync`() {
        val original = "x".repeat(65_535)
        val result = reduce(
            PatchOperation.PutBlock(TextBlock("answer", original)),
            PatchOperation.AppendBlockText("answer", "yz"),
        )

        assertTrue(result.resyncRequired)
        assertEquals(0, result.message().revision)
        assertTrue(result.message().blocks.isEmpty())
    }

    @Test
    fun `streamed text accepts exact frozen maximum`() {
        val result = reduce(
            PatchOperation.PutBlock(TextBlock("answer", "x".repeat(65_535))),
            PatchOperation.AppendBlockText("answer", "y"),
        )

        assertFalse(result.resyncRequired)
        assertEquals(65_536, (result.message().blocks["answer"] as TextBlock).text.length)
    }

    private fun reduce(vararg operations: PatchOperation): ThreadConversationState {
        val message = RichMessage(
            id = "msg_public",
            role = MessageRole.ASSISTANT,
            state = RichMessageState.STREAMING,
            revision = 0,
            rootBlockIds = emptyList(),
            blocks = emptyMap(),
            createdAtMillis = 1,
        )
        val initial = ThreadConversationState(
            threadKey = key,
            streamId = "stream_public",
            streamSeq = 1,
            messages = listOf(message),
        )
        return ConversationReducer.reduce(
            initial,
            ConversationEvent.MessagePatched(
                eventId = "event_public",
                streamId = "stream_public",
                streamSeq = 2,
                messageId = message.id,
                baseRevision = 0,
                revision = 1,
                operations = operations.toList(),
            ),
        )
    }

    private fun ThreadConversationState.message(): RichMessage = messages.single()
}
