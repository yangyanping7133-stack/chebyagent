package com.cheby.codex.mobile.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class R1PatchReducerTest {
    @Test
    fun `terminal deltas update one panel in place and stop at display budget`() {
        val initial = stateWith(
            TerminalBlock("terminal", "Verification", output = "start\n", state = StepState.ACTIVE),
        )
        val reduced = ConversationReducer.reduce(
            initial,
            patch(
                initial,
                listOf(
                    PatchOperation.AppendTerminal(
                        "terminal",
                        "Bearer abcdefghijklmnop /Users/alice/private.txt " + "x".repeat(20_000),
                    ),
                ),
            ),
        )

        assertFalse(reduced.resyncRequired)
        assertEquals(1, reduced.messages.size)
        val terminal = reduced.messages.single().blocks.getValue("terminal") as TerminalBlock
        assertEquals(16_384, terminal.output.toByteArray(Charsets.UTF_8).size)
        assertTrue(terminal.truncated)
        assertFalse(terminal.output.contains("abcdefghijklmnop"))
        assertFalse(terminal.output.contains("/Users/"))
    }

    @Test
    fun `R1 merge completes operational evidence before message completion`() {
        val terminal = TerminalBlock("terminal", "Verification", output = "running", state = StepState.ACTIVE)
        val tool = ToolBlock("tool", "Analyzer", "Running", StepState.ACTIVE)
        val tests = TestBlock("tests", "Checks", "Running", state = StepState.ACTIVE)
        val initial = stateWith(terminal, tool, tests)
        val reduced = ConversationReducer.reduce(
            initial,
            patch(
                initial,
                listOf(
                    PatchOperation.MergeBlock("terminal", mapOf("state" to "completed", "output" to "done")),
                    PatchOperation.MergeBlock("tool", mapOf("state" to "completed", "detail" to "Done")),
                    PatchOperation.MergeBlock(
                        "tests",
                        mapOf("state" to "completed", "summary" to "Passed", "passed" to 7, "failed" to 0),
                    ),
                    PatchOperation.SetMessageState(RichMessageState.COMPLETED),
                ),
            ),
        )

        assertFalse(reduced.resyncRequired)
        assertEquals(RichMessageState.COMPLETED, reduced.messages.single().state)
        assertEquals("done", (reduced.messages.single().blocks["terminal"] as TerminalBlock).output)
        assertEquals(7, (reduced.messages.single().blocks["tests"] as TestBlock).passed)
    }

    @Test
    fun `completed panel with active tool fails closed into resync`() {
        val initial = stateWith(ToolBlock("tool", "Analyzer", state = StepState.ACTIVE))
        val reduced = ConversationReducer.reduce(
            initial,
            patch(initial, listOf(PatchOperation.SetMessageState(RichMessageState.COMPLETED))),
        )

        assertTrue(reduced.resyncRequired)
    }

    @Test
    fun `file merge keeps basename and unknown fallback stays redacted`() {
        val initial = stateWith(
            FileBlock("file", "Old.kt"),
            UnknownBlock("future", "future-v2", "safe"),
        )
        val reduced = ConversationReducer.reduce(
            initial,
            patch(
                initial,
                listOf(
                    PatchOperation.MergeBlock(
                        "file",
                        mapOf("label" to "C:\\Users\\Alice\\secret\\Main.kt", "change" to "modified"),
                    ),
                    PatchOperation.MergeBlock(
                        "future",
                        mapOf("fallbackText" to "Bearer abcdefghijklmnop /home/alice/report"),
                    ),
                ),
            ),
        )

        assertEquals("Main.kt", (reduced.messages.single().blocks["file"] as FileBlock).label)
        val fallback = (reduced.messages.single().blocks["future"] as UnknownBlock).fallbackText
        assertFalse(fallback.contains("abcdefghijklmnop"))
        assertFalse(fallback.contains("/home/"))
    }

    @Test
    fun `text append sanitizes existing and delta as one boundary`() {
        val initial = stateWith(TextBlock("text", "Bearer abc", fallbackText = "Bearer abc"))

        val reduced = ConversationReducer.reduce(
            initial,
            patch(
                initial,
                listOf(
                    PatchOperation.AppendBlockText(
                        "text",
                        "defghijklmnop /Users/alice/private.txt \u202Evisible",
                    ),
                ),
            ),
        )

        assertFalse(reduced.resyncRequired)
        val text = reduced.messages.single().blocks.getValue("text") as TextBlock
        assertFalse(text.text.contains("abcdefghijklmnop"))
        assertFalse(text.text.contains("/Users/"))
        assertFalse(text.text.contains('\u202E'))
        assertTrue(text.text.contains("<redacted>"))
        assertTrue(text.text.contains("<private path>"))
    }

    @Test
    fun `unrelated patch sanitizes unsafe historical block fields`() {
        val initial = stateWith(
            NoticeBlock(
                id = "notice",
                title = "Bearer abcdefghijklmnop\u061C",
                body = "http://localhost:3111/private /workspace",
                tone = Tone.WARNING,
            ),
        )

        val reduced = ConversationReducer.reduce(
            initial,
            patch(initial, listOf(PatchOperation.SetMessageState(RichMessageState.WAITING_INPUT))),
        )

        assertFalse(reduced.resyncRequired)
        val notice = reduced.messages.single().blocks.getValue("notice") as NoticeBlock
        val visible = notice.title + notice.body + notice.fallbackText
        assertFalse(visible.contains("abcdefghijklmnop"))
        assertFalse(visible.contains("localhost"))
        assertFalse(visible.contains("/workspace"))
        assertFalse(visible.contains('\u061C'))
    }

    @Test
    fun `oversize multibyte text append requests resync without mutation`() {
        val initial = stateWith(TextBlock("text", "safe", fallbackText = "safe"))

        val reduced = ConversationReducer.reduce(
            initial,
            patch(initial, listOf(PatchOperation.AppendBlockText("text", "汉".repeat(43_600)))),
        )

        assertTrue(reduced.resyncRequired)
        assertEquals("safe", (reduced.messages.single().blocks.getValue("text") as TextBlock).text)
    }

    @Test
    fun `thousand block puts fail before growing the message`() {
        val initial = stateWith(TextBlock("text", "safe"))
        val operations = (1..1_000).map { index ->
            PatchOperation.PutBlock(TextBlock("put-$index", "value"))
        }

        val reduced = ConversationReducer.reduce(initial, patch(initial, operations))

        assertTrue(reduced.resyncRequired)
        assertEquals(1, reduced.messages.single().blocks.size)
    }

    @Test
    fun `one hundred twenty ninth block fails closed`() {
        val blocks = (1..128).map { TextBlock("block-$it", "value") }
        val initial = stateWithRoots(blocks, listOf("block-1"))

        val reduced = ConversationReducer.reduce(
            initial,
            patch(initial, listOf(PatchOperation.PutBlock(TextBlock("block-129", "value")))),
        )

        assertTrue(reduced.resyncRequired)
        assertEquals(128, reduced.messages.single().blocks.size)
    }

    @Test
    fun `root and nested item collections are bounded`() {
        val blocks = (1..65).map { TextBlock("block-$it", "value") }
        val rootInitial = stateWithRoots(blocks, listOf("block-1"))
        val tooManyRoots = ConversationReducer.reduce(
            rootInitial,
            patch(rootInitial, listOf(PatchOperation.SetRootBlocks(blocks.map(RichBlock::id)))),
        )
        assertTrue(tooManyRoots.resyncRequired)

        val stepsInitial = stateWith(StepsBlock("steps", "Steps", emptyList()))
        val itemPatch = (1..65).map { index ->
            mapOf("id" to "step-$index", "label" to "Step $index", "state" to "pending")
        }
        val tooManyItems = ConversationReducer.reduce(
            stepsInitial,
            patch(stepsInitial, listOf(PatchOperation.MergeBlock("steps", mapOf("items" to itemPatch)))),
        )
        assertTrue(tooManyItems.resyncRequired)
    }

    @Test
    fun `processed event dedupe history remains bounded`() {
        var state = stateWith(TextBlock("text", "safe"))
        repeat(1_100) { index ->
            state = ConversationReducer.reduce(
                state,
                ConversationEvent.NoOp(
                    eventId = "noop-$index",
                    streamId = state.streamId,
                    streamSeq = state.streamSeq + 1,
                ),
            )
        }

        assertFalse(state.resyncRequired)
        assertEquals(1_024, state.processedEventIds.size)
        assertFalse("noop-0" in state.processedEventIds)
        assertTrue("noop-1099" in state.processedEventIds)
    }

    @Test
    fun `negative and overflowing revisions fail closed`() {
        val initial = stateWith(TextBlock("text", "safe"))
        val negative = patch(initial, emptyList()).copy(baseRevision = -1, revision = 0)
        assertTrue(ConversationReducer.reduce(initial, negative).resyncRequired)

        val maxMessage = initial.messages.single().copy(revision = Long.MAX_VALUE)
        val maxState = initial.copy(messages = listOf(maxMessage))
        val overflowing = patch(maxState, emptyList()).copy(
            baseRevision = Long.MAX_VALUE,
            revision = Long.MIN_VALUE,
        )
        assertTrue(ConversationReducer.reduce(maxState, overflowing).resyncRequired)
    }

    @Test
    fun `snapshot normalizes historical display fields before they can render`() {
        val raw = messageWith(
            id = "unsafe-message",
            blocks = listOf(
                TextBlock(
                    "text",
                    "Bearer abcdefghijklmnop /Users/alice/private.txt \u202Evisible",
                ),
            ),
        )

        val state = ConversationReducer.fromSnapshot(
            ConversationSnapshot(ThreadKey("thread-r1"), "stream-r1", 4, listOf(raw)),
        )

        assertFalse(state.resyncRequired)
        val visible = (state.messages.single().blocks.getValue("text") as TextBlock).text
        assertFalse(visible.contains("abcdefghijklmnop"))
        assertFalse(visible.contains("/Users/"))
        assertFalse(visible.contains('\u202E'))
        assertTrue(visible.contains("<redacted>"))
        assertTrue(visible.contains("<private path>"))
    }

    @Test
    fun `one oversized snapshot message rejects the entire batch atomically`() {
        val safe = messageWith("safe-message", listOf(TextBlock("safe", "safe")))
        val oversized = messageWith(
            "oversized-message",
            listOf(TextBlock("large", "汉".repeat(43_600))),
        )

        val state = ConversationReducer.fromSnapshot(
            ConversationSnapshot(ThreadKey("thread-r1"), "stream-r1", 4, listOf(safe, oversized)),
        )

        assertTrue(state.resyncRequired)
        assertTrue(state.messages.isEmpty())
    }

    @Test
    fun `snapshot rejects unbounded final state fields`() {
        val invalidBlocks = listOf<RichBlock>(
            StatusBlock("status-nan", "Status", progress = Float.NaN),
            StatusBlock("status-inf", "Status", progress = Float.POSITIVE_INFINITY),
            StatusBlock("status-range", "Status", progress = 1.1f),
            DiffBlock("diff-negative", "File.kt", "bad", additions = -1),
            DiffBlock("diff-large", "File.kt", "bad", deletions = 1_000_001),
            TestBlock("test-negative", "Tests", "bad", passed = -1),
            TestBlock("test-large", "Tests", "bad", failed = 1_000_001),
            ActionsBlock(
                "action-negative",
                listOf(ActionItem("retry", "Retry", "retry-action", retryNotBeforeMillis = -1)),
            ),
            ActionsBlock(
                "action-large",
                listOf(ActionItem("retry", "Retry", "retry-action", retryNotBeforeMillis = Long.MAX_VALUE)),
            ),
            UnknownBlock("unknown", "x".repeat(97), "safe"),
        )

        invalidBlocks.forEach { block ->
            val state = ConversationReducer.fromSnapshot(
                ConversationSnapshot(
                    ThreadKey("thread-r1"),
                    "stream-r1",
                    4,
                    listOf(messageWith("message-${block.id}", listOf(block))),
                ),
            )
            assertTrue("Expected invalid snapshot for ${block.id}", state.resyncRequired)
            assertTrue(state.messages.isEmpty())
        }
    }

    @Test
    fun `merge rejects non finite progress and non integral evidence counts`() {
        val status = stateWith(StatusBlock("status", "Status", progress = 0.5f))
        assertTrue(
            ConversationReducer.reduce(
                status,
                patch(status, listOf(PatchOperation.MergeBlock("status", mapOf("progress" to Double.NaN)))),
            ).resyncRequired,
        )

        val diff = stateWith(DiffBlock("diff", "File.kt", "safe", additions = 1))
        assertTrue(
            ConversationReducer.reduce(
                diff,
                patch(diff, listOf(PatchOperation.MergeBlock("diff", mapOf("additions" to 1.5)))),
            ).resyncRequired,
        )
    }

    private fun stateWith(vararg blocks: RichBlock): ThreadConversationState {
        return stateWithRoots(blocks.toList(), blocks.map(RichBlock::id))
    }

    private fun stateWithRoots(
        blocks: List<RichBlock>,
        roots: List<String>,
    ): ThreadConversationState {
        val message = messageWith("message-r1", blocks, roots)
        return ConversationReducer.fromSnapshot(
            ConversationSnapshot(ThreadKey("thread-r1"), "stream-r1", 4, listOf(message)),
        )
    }

    private fun messageWith(
        id: String,
        blocks: List<RichBlock>,
        roots: List<String> = blocks.map(RichBlock::id),
    ): RichMessage = RichMessage(
            id = id,
            role = MessageRole.ASSISTANT,
            state = RichMessageState.STREAMING,
            revision = 1,
            rootBlockIds = roots,
            blocks = blocks.associateBy(RichBlock::id),
            createdAtMillis = 1,
        )

    private fun patch(
        state: ThreadConversationState,
        operations: List<PatchOperation>,
    ): ConversationEvent.MessagePatched = ConversationEvent.MessagePatched(
        eventId = "event-${state.streamSeq + 1}",
        streamId = state.streamId,
        streamSeq = state.streamSeq + 1,
        messageId = state.messages.single().id,
        baseRevision = state.messages.single().revision,
        revision = state.messages.single().revision + 1,
        operations = operations,
    )
}
