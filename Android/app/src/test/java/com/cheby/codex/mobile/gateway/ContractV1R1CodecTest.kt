package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.model.DiffBlock
import com.cheby.codex.mobile.model.ConversationEvent
import com.cheby.codex.mobile.model.FileBlock
import com.cheby.codex.mobile.model.FileChangeKind
import com.cheby.codex.mobile.model.StepState
import com.cheby.codex.mobile.model.TerminalBlock
import com.cheby.codex.mobile.model.TestBlock
import com.cheby.codex.mobile.model.PatchOperation
import com.cheby.codex.mobile.model.ThreadKey
import com.cheby.codex.mobile.model.ToolBlock
import com.cheby.codex.mobile.model.UnknownBlock
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ContractV1R1CodecTest {
    private val codec = ContractV1Codec()

    @Test
    fun `R1 allowlist restores terminal file diff tool and test blocks`() {
        val message = codec.decodeMessages(messageBody(r1Blocks())).single()

        val terminal = message.blocks.getValue("terminal") as TerminalBlock
        val file = message.blocks.getValue("file") as FileBlock
        val diff = message.blocks.getValue("diff") as DiffBlock
        val tool = message.blocks.getValue("tool") as ToolBlock
        val test = message.blocks.getValue("test") as TestBlock

        assertEquals("Verification", terminal.title)
        assertEquals("Local checks", terminal.commandLabel)
        assertEquals(StepState.COMPLETED, terminal.state)
        assertEquals("Main.kt", file.label)
        assertEquals(FileChangeKind.MODIFIED, file.change)
        assertEquals(8, diff.additions)
        assertEquals(2, diff.deletions)
        assertEquals(StepState.ACTIVE, tool.state)
        assertEquals(12, test.passed)
        assertEquals(0, test.failed)
        assertEquals(listOf("terminal", "file", "diff", "tool", "test"), message.rootBlockIds)
    }

    @Test
    fun `thread recovery snapshot keeps typed R1 evidence and cursor`() {
        val body = messageBody(r1Blocks()).dropLast(1) +
            ",\"streamId\":\"stream-r1\",\"cursor\":17,\"approvals\":[]}"

        val detail = codec.decodeThreadDetail(body)

        assertEquals("stream-r1", detail.streamId)
        assertEquals(17, detail.cursor)
        assertTrue(detail.messages.single().blocks["terminal"] is TerminalBlock)
        assertTrue(detail.messages.single().blocks["test"] is TestBlock)
    }

    @Test
    fun `live block put decodes typed terminal for in-place reducer update`() {
        val body = """{
            "v":1,"streamId":"stream-r1","eventId":"event-r1","seq":18,
            "occurredAt":"2026-07-19T00:00:18Z","type":"message.patch","threadId":"thread-r1",
            "payload":{"messageId":"msg-r1","baseRevision":3,"nextRevision":4,"ops":[
                {"op":"block.put","blockId":"terminal","value":{"type":"terminal","title":"Check","text":"running","state":"running","fallbackText":"Check running"}}
            ]}
        }"""

        val event = codec.decodeEvent(body, ThreadKey("thread-r1")).event as ConversationEvent.MessagePatched

        val operation = event.operations.single() as PatchOperation.PutBlock
        assertTrue(operation.block is TerminalBlock)
        assertEquals(StepState.ACTIVE, (operation.block as TerminalBlock).state)
    }

    @Test
    fun `R1 evidence strips ANSI control secrets and private paths`() {
        val blocks = """{
            "terminal":{"type":"terminal","title":"Check\u202E\u200B","commandLabel":"./gradlew test --token=raw","text":"\u001b[31mBearer abcdefghijklmnop /Users/alice/work/a.txt API_KEY=secretvalue\u0007\u0085","fallbackText":"terminal /home/alice/work"},
            "file":{"type":"file","label":"/company/private/work/Main.kt","change":"modified","fallbackText":"file ready"},
            "diff":{"type":"diff","fileLabel":"C:\\Users\\Alice\\secret\\Main.kt","summary":"access_token=abcdefghijklmnop","text":"- password=hunter22\n+ secret=somethinglong","fallbackText":"diff ready"},
            "tool":{"type":"tool","label":"Runner","detail":"file:///Users/alice/private/report.txt","fallbackText":"tool ready"},
            "test":{"type":"test","title":"Checks","summary":"sk-abcdefghijklmnop","fallbackText":"test ready"},
            "future":{"type":"future.private","fallbackText":"Bearer abcdefghijklmnop at /var/private/result"}
        }"""

        val message = codec.decodeMessages(
            messageBody(blocks, "[\"terminal\",\"file\",\"diff\",\"tool\",\"test\",\"future\"]"),
        ).single()
        val visible = message.blocks.values.joinToString("\n") { it.fallbackText } + "\n" +
            (message.blocks.getValue("terminal") as TerminalBlock).output + "\n" +
            (message.blocks.getValue("diff") as DiffBlock).preview + "\n" +
            (message.blocks.getValue("tool") as ToolBlock).detail

        assertNull((message.blocks.getValue("terminal") as TerminalBlock).commandLabel)
        assertEquals("Main.kt", (message.blocks.getValue("file") as FileBlock).label)
        assertEquals("Main.kt", (message.blocks.getValue("diff") as DiffBlock).fileLabel)
        assertFalse(visible.contains("abcdefghijklmnop"))
        assertFalse(visible.contains("hunter22"))
        assertFalse(visible.contains("somethinglong"))
        assertFalse(visible.contains("/Users/"))
        assertFalse(visible.contains("/home/"))
        assertFalse(visible.contains("/var/"))
        assertFalse(visible.contains("file://"))
        assertFalse(visible.contains("\u001b"))
        assertFalse(visible.contains("\u0007"))
        assertFalse((message.blocks.getValue("terminal") as TerminalBlock).title.contains("\u202E"))
        assertFalse((message.blocks.getValue("terminal") as TerminalBlock).title.contains("\u200B"))
        assertTrue(visible.contains("<redacted>"))
        assertTrue(message.blocks.getValue("future") is UnknownBlock)
    }

    @Test
    fun `terminal snapshot is deterministically bounded and marked truncated`() {
        val longOutput = "x".repeat(20_000)
        val blocks = """{
            "terminal":{"type":"terminal","title":"Check","text":"$longOutput","fallbackText":"done"}
        }"""

        val terminal = codec.decodeMessages(messageBody(blocks, "[\"terminal\"]"))
            .single().blocks.getValue("terminal") as TerminalBlock

        assertEquals(16_384, terminal.output.toByteArray(Charsets.UTF_8).size)
        assertTrue(terminal.output.endsWith("…已截断"))
        assertTrue(terminal.truncated)
    }

    @Test
    fun `terminal truncation uses UTF8 bytes instead of UTF16 length`() {
        val unicodeOutput = "界".repeat(6_000)
        val blocks = """{
            "terminal":{"type":"terminal","title":"Check","text":"$unicodeOutput","fallbackText":"done"}
        }"""

        val terminal = codec.decodeMessages(messageBody(blocks, "[\"terminal\"]"))
            .single().blocks.getValue("terminal") as TerminalBlock

        assertTrue(terminal.output.toByteArray(Charsets.UTF_8).size <= 16_384)
        assertTrue(terminal.truncated)
    }

    @Test
    fun `unknown fallback is bounded without interpreting its payload`() {
        val longFallback = "safe ".repeat(1_000)
        val blocks = """{
            "future":{"type":"html-script-widget","html":"<script>run()</script>","fallbackText":"$longFallback"}
        }"""

        val block = codec.decodeMessages(messageBody(blocks, "[\"future\"]"))
            .single().blocks.getValue("future") as UnknownBlock

        assertEquals("html-script-widget", block.originalType)
        assertEquals(2_048, block.fallbackText.toByteArray(Charsets.UTF_8).size)
        assertTrue(block.fallbackText.endsWith("…已截断"))
    }

    @Test(expected = ProtocolV1Exception::class)
    fun `negative test count is rejected`() {
        val blocks = """{
            "test":{"type":"test","title":"Checks","summary":"invalid","passed":-1,"fallbackText":"invalid"}
        }"""
        codec.decodeMessages(messageBody(blocks, "[\"test\"]"))
    }

    @Test(expected = ProtocolV1Exception::class)
    fun `R1 child arrays are structurally bounded`() {
        val items = (0..64).joinToString(",") {
            "{\"id\":\"step-$it\",\"label\":\"Step $it\",\"state\":\"pending\"}"
        }
        val blocks = """{
            "steps":{"type":"steps","items":[$items],"fallbackText":"steps"}
        }"""
        codec.decodeMessages(messageBody(blocks, "[\"steps\"]"))
    }

    @Test
    fun `unsafe executable action schemes are rejected`() {
        listOf("javascript:alert(1)", "data:text/plain,x", "file:/private/a", "content:private", "intent:open")
            .forEach { actionRef ->
                val blocks = """{
                    "actions":{"type":"actions","items":[{"id":"open","label":"Open","actionRef":"$actionRef"}],"fallbackText":"open"}
                }"""
                assertTrue(
                    runCatching { codec.decodeMessages(messageBody(blocks, "[\"actions\"]")) }
                        .exceptionOrNull() is ProtocolV1Exception,
                )
            }
    }

    @Test(expected = ProtocolV1Exception::class)
    fun `block identifiers reject bidi controls`() {
        val blocks = """{
            "safe\u202E":{"type":"notice","title":"Notice","body":"Body","fallbackText":"notice"}
        }"""
        codec.decodeMessages(messageBody(blocks, "[\"safe\u202E\"]"))
    }

    @Test(expected = ProtocolV1Exception::class)
    fun `deeply nested unused payload is rejected before block decoding`() {
        val nested = (1..20).fold("0") { value, _ -> "[$value]" }
        val blocks = """{
            "future":{"type":"future","fallbackText":"safe","unused":$nested}
        }"""
        codec.decodeMessages(messageBody(blocks, "[\"future\"]"))
    }

    @Test(timeout = 1_000)
    fun `extreme nesting is rejected by lexical preflight before parser recursion`() {
        val body = "[".repeat(100_000) + "0" + "]".repeat(100_000)

        assertTrue(runCatching { codec.decodeMessages(body) }.exceptionOrNull() is ProtocolV1Exception)
    }

    @Test
    fun `lexical preflight ignores escaped structural characters inside strings`() {
        val blocks = """{
            "future":{"type":"future","fallbackText":"escaped quote: \" and brackets [{]} remain text"}
        }"""

        val decoded = codec.decodeMessages(messageBody(blocks, "[\"future\"]"))

        assertTrue((decoded.single().blocks["future"] as UnknownBlock).fallbackText.contains("brackets [{]}"))
    }

    @Test(expected = ProtocolV1Exception::class)
    fun `one rich message cannot exceed serialized public budget`() {
        val payload = "x".repeat(60_000)
        val blocks = (1..5).joinToString(",", prefix = "{", postfix = "}") {
            "\"text-$it\":{\"type\":\"text\",\"text\":\"$payload\",\"fallbackText\":\"safe\"}"
        }
        val roots = (1..5).joinToString(",", prefix = "[", postfix = "]") { "\"text-$it\"" }
        codec.decodeMessages(messageBody(blocks, roots))
    }

    @Test(timeout = 2_000)
    fun `multi megabyte unquoted scalar is rejected by lexical preflight`() {
        val body = "{\"messages\":" + "9".repeat(5 * 1024 * 1024) + "}"

        assertTrue(runCatching { codec.decodeMessages(body) }.exceptionOrNull() is ProtocolV1Exception)
    }

    @Test
    fun `gateway error uses the same bounded protocol decoder`() {
        val decoded = codec.decodeGatewayError(
            """{"error":{"code":"RATE_LIMITED","retryable":true}}""",
        )

        assertEquals("RATE_LIMITED", decoded.code)
        assertTrue(decoded.retryable)
        val oversizedScalar = "{\"error\":{\"code\":1,\"retryable\":" + "9".repeat(2_048) + "}}"
        assertTrue(
            runCatching { codec.decodeGatewayError(oversizedScalar) }.exceptionOrNull() is ProtocolV1Exception,
        )
    }

    @Test
    fun `duplicate and unknown root identifiers are rejected`() {
        val blocks = """{"text":{"type":"text","text":"safe","fallbackText":"safe"}}"""
        listOf("[\"text\",\"text\"]", "[\"text\",\"missing\"]").forEach { roots ->
            assertTrue(
                runCatching { codec.decodeMessages(messageBody(blocks, roots)) }
                    .exceptionOrNull() is ProtocolV1Exception,
            )
        }
    }

    @Test
    fun `negative message and patch revisions are rejected`() {
        val blocks = """{"text":{"type":"text","text":"safe","fallbackText":"safe"}}"""
        val negativeMessage = messageBody(blocks, "[\"text\"]").replace("\"revision\":3", "\"revision\":-1")
        assertTrue(
            runCatching { codec.decodeMessages(negativeMessage) }.exceptionOrNull() is ProtocolV1Exception,
        )

        val negativePatch = """{
            "v":1,"streamId":"stream-r1","eventId":"event-r1","seq":18,
            "occurredAt":"2026-07-19T00:00:18Z","type":"message.patch","threadId":"thread-r1",
            "payload":{"messageId":"msg-r1","baseRevision":-1,"nextRevision":0,"ops":[]}
        }"""
        assertTrue(
            runCatching { codec.decodeEvent(negativePatch, ThreadKey("thread-r1")) }
                .exceptionOrNull() is ProtocolV1Exception,
        )
    }

    private fun r1Blocks(): String = """{
        "terminal":{"type":"terminal","title":"Verification","commandLabel":"Local checks","text":"12 checks passed","state":"completed","fallbackText":"Verification completed"},
        "file":{"type":"file","path":"/Users/private/project/Main.kt","change":"modified","detail":"Retry path updated","language":"Kotlin","state":"completed","fallbackText":"Main file updated"},
        "diff":{"type":"diff","fileLabel":"Main.kt","summary":"+8 -2","preview":"- old\n+ new","additions":8,"deletions":2,"fallbackText":"Eight additions and two removals"},
        "tool":{"type":"tool","label":"Static analyzer","detail":"Reviewing result","state":"running","durationLabel":"2 s","fallbackText":"Analyzer is running"},
        "test":{"type":"test","title":"Unit checks","summary":"All checks passed","passed":12,"failed":0,"skipped":1,"durationLabel":"3 s","state":"passed","fallbackText":"Twelve checks passed"}
    }"""

    private fun messageBody(blocks: String, roots: String = "[\"terminal\",\"file\",\"diff\",\"tool\",\"test\"]"): String = """{
        "messages":[{
            "schema":"cheby.rich-message/1.0",
            "messageId":"msg-r1",
            "threadId":"thr-r1",
            "turnId":"turn-r1",
            "sourceItemId":"item-r1",
            "role":"assistant",
            "state":"streaming",
            "revision":3,
            "rootBlockIds":$roots,
            "blocks":$blocks,
            "fallback":{"text":"Operational evidence"},
            "createdAt":"2026-07-19T00:00:00Z",
            "updatedAt":"2026-07-19T00:00:03Z"
        }]
    }"""
}
