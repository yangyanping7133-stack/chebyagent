package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.model.ConversationEvent
import com.cheby.codex.mobile.model.ConversationReducer
import com.cheby.codex.mobile.model.ConversationReductionDisposition
import com.cheby.codex.mobile.model.NoticeBlock
import com.cheby.codex.mobile.model.PatchOperation
import com.cheby.codex.mobile.model.ThreadConversationState
import com.cheby.codex.mobile.model.ThreadKey
import com.cheby.codex.mobile.model.UnknownBlock
import com.cheby.codex.mobile.model.MessageRole
import com.cheby.codex.mobile.model.MediaBlock
import java.io.File
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ContractV1CodecTest {
    private val codec = ContractV1Codec()

    @Test
    fun `new thread accepts the explicit empty preview from Gateway`() {
        val thread = codec.decodeThread(
            """{
                "id":"thr_public",
                "title":"New conversation",
                "preview":"",
                "createdAt":"2026-07-20T18:00:00Z",
                "updatedAt":"2026-07-20T18:00:00Z",
                "status":"idle"
            }""",
        )

        assertEquals("", thread.preview)
    }

    @Test(expected = ProtocolV1Exception::class)
    fun `new thread still rejects a missing preview`() {
        codec.decodeThread(
            """{
                "id":"thr_public",
                "title":"New conversation",
                "createdAt":"2026-07-20T18:00:00Z",
                "updatedAt":"2026-07-20T18:00:00Z",
                "status":"idle"
            }""",
        )
    }

    @Test(expected = ProtocolV1Exception::class)
    fun `new thread rejects a non string preview`() {
        codec.decodeThread(
            """{
                "id":"thr_public",
                "title":"New conversation",
                "preview":7,
                "createdAt":"2026-07-20T18:00:00Z",
                "updatedAt":"2026-07-20T18:00:00Z",
                "status":"idle"
            }""",
        )
    }

    @Test
    fun `unknown block degrades to readable fallback`() {
        val messages = codec.decodeMessages(
            threadDetail(
                blocks = """{
                    "future": {
                        "blockId": "future",
                        "type": "calendar-v2",
                        "fallbackText": "Two events are scheduled tomorrow."
                    }
                }""",
                roots = "[\"future\"]",
            ),
        )

        val block = messages.single().blocks["future"] as UnknownBlock
        assertEquals("calendar-v2", block.originalType)
        assertEquals("Two events are scheduled tomorrow.", block.fallbackText)
    }

    @Test
    fun `message patch decodes all frozen V1 operations`() {
        val body = event(
            type = "message.patch",
            payload = """{
                "messageId":"msg_public",
                "baseRevision":7,
                "nextRevision":8,
                "ops":[
                    {"op":"block.put","blockId":"status","value":{"type":"status","label":"Running"}},
                    {"op":"block.merge","blockId":"status","value":{"progress":0.5}},
                    {"op":"block.text.append","blockId":"answer","value":"next"},
                    {"op":"block.terminal.append","blockId":"terminal","value":"ok"},
                    {"op":"block.remove","blockId":"old"},
                    {"op":"root.set","value":["status","answer"]},
                    {"op":"message.state.set","value":"streaming"},
                    {"op":"action.resolve","blockId":"actions","actionId":"approve"}
                ]
            }""",
        )

        val decoded = codec.decodeEvent(body, ThreadKey("thr_public")).event as ConversationEvent.MessagePatched

        assertEquals(8, decoded.operations.size)
        assertTrue(decoded.operations[0] is PatchOperation.PutBlock)
        assertTrue(decoded.operations[1] is PatchOperation.MergeBlock)
        assertTrue(decoded.operations[2] is PatchOperation.AppendBlockText)
        assertTrue(decoded.operations[3] is PatchOperation.AppendTerminal)
        assertTrue(decoded.operations[4] is PatchOperation.RemoveBlock)
        assertTrue(decoded.operations[5] is PatchOperation.SetRootBlocks)
        assertTrue(decoded.operations[6] is PatchOperation.SetMessageState)
        assertTrue(decoded.operations[7] is PatchOperation.ResolveAction)
    }

    @Test(expected = ProtocolV1Exception::class)
    fun `unknown event mutation is rejected`() {
        codec.decodeEvent(event("future.mutation", "{}"), ThreadKey("thr_public"))
    }

    @Test
    fun `repository golden fixture decodes direct message snapshot payload`() {
        val fixture = findRepositoryFile("fixtures/v1/event_streams/unknown_block.json")
        val event = Json.parseToJsonElement(fixture.readText())
            .jsonObject.getValue("events").jsonArray.single().toString()

        val decoded = codec.decodeEvent(event, ThreadKey("thr_stream_demo")).event

        assertTrue(decoded is ConversationEvent.MessageAdded)
        val block = (decoded as ConversationEvent.MessageAdded).message.blocks["calendar_preview"]
        assertTrue(block is UnknownBlock)
        assertEquals("Two events are scheduled tomorrow afternoon.", block?.fallbackText)
    }

    @Test
    fun `frozen general assistance fixture decodes opaque media without inventing a URI`() {
        val fixture = findRepositoryFile("fixtures/v1/scenarios/general_assistance.json")

        val media = codec.decodeMessages(fixture.readText())
            .single().blocks.getValue("preview_media") as MediaBlock

        assertEquals("asset_summary_preview", media.assetId)
        assertEquals("Summary preview", media.caption)
        assertEquals("The summary is ready and is waiting for approval before sharing.", media.fallbackText)
    }

    @Test(expected = ProtocolV1Exception::class)
    fun `media rejects URL shaped asset identifiers`() {
        codec.decodeMessages(
            threadDetail(
                blocks = """{
                    "preview": {
                        "blockId": "preview",
                        "type": "media",
                        "assetId": "https://example.com/not-an-opaque-id",
                        "caption": "Unsafe"
                    }
                }""",
                roots = "[\"preview\"]",
            ),
        )
    }

    @Test
    fun `media without caption preserves its readable fallback`() {
        val media = codec.decodeMessages(
            threadDetail(
                blocks = """{
                    "preview": {
                        "blockId": "preview",
                        "type": "media",
                        "assetId": "asset_summary_preview",
                        "fallbackText": "Preview unavailable; the written result is still available."
                    }
                }""",
                roots = "[\"preview\"]",
            ),
        ).single().blocks.getValue("preview") as MediaBlock

        assertEquals(null, media.caption)
        assertEquals("Preview unavailable; the written result is still available.", media.fallbackText)
    }

    @Test
    fun `frozen missing media fallback becomes a visible idempotent system notice`() {
        val fixture = findRepositoryFile("fixtures/v1/event_streams/missing_media.json")
        val event = Json.parseToJsonElement(fixture.readText())
            .jsonObject.getValue("events").jsonArray.single().toString()

        val decoded = codec.decodeEvent(event, ThreadKey("thr_stream_demo")).event as ConversationEvent.MessageAdded
        val initial = ThreadConversationState(
            threadKey = ThreadKey("thr_stream_demo"),
            streamId = "stream_device_alpha",
            streamSeq = 0,
        )
        val once = ConversationReducer.reduce(initial, decoded)
        val duplicate = ConversationReducer.reduceWithDisposition(once, decoded)
        val notice = once.messages.single().blocks.getValue("asset-unavailable-notice") as NoticeBlock

        assertEquals(MessageRole.SYSTEM, once.messages.single().role)
        assertEquals("Preview unavailable; the written result is still available.", notice.body)
        assertEquals(ConversationReductionDisposition.EXACT_DUPLICATE, duplicate.disposition)
        assertEquals(once, duplicate.state)
    }

    @Test
    fun `real gateway local image unavailable payload stays applied without leaking identifiers`() {
        val decoded = codec.decodeEvent(
            event(
                "asset.unavailable",
                """{"clientMessageId":"client-image-message-001","reason":"storageValidation"}""",
            ),
            ThreadKey("thr_public"),
        ).event as ConversationEvent.MessageAdded
        val reduced = ConversationReducer.reduce(
            ThreadConversationState(ThreadKey("thr_public"), "stream_public", 7),
            decoded,
        )
        val notice = reduced.messages.single().blocks.getValue("asset-unavailable-notice") as NoticeBlock

        assertTrue(!reduced.resyncRequired)
        assertEquals(8, reduced.streamSeq)
        assertEquals("图片附件已失效，请重新选择后发送。", notice.body)
        assertTrue(!notice.fallbackText.contains("client-image-message-001"))
        assertTrue(!notice.fallbackText.contains("storageValidation"))
    }

    @Test(expected = ProtocolV1Exception::class)
    fun `asset unavailable rejects mixed payload shapes`() {
        codec.decodeEvent(
            event(
                "asset.unavailable",
                """{
                    "assetId":"asset_summary_preview",
                    "fallbackText":"Unavailable",
                    "clientMessageId":"client-image-message-001",
                    "reason":"expired"
                }""",
            ),
            ThreadKey("thr_public"),
        )
    }

    @Test(expected = ProtocolV1Exception::class)
    fun `asset unavailable rejects unknown local image reason`() {
        codec.decodeEvent(
            event(
                "asset.unavailable",
                """{"clientMessageId":"client-image-message-001","reason":"internalPath"}""",
            ),
            ThreadKey("thr_public"),
        )
    }

    @Test
    fun `gateway wire goldens decode approval lifecycle without leaking terminal token`() {
        val requested = codec.decodeEvent(
            findRepositoryFile("gateway/tests/goldens/approval_requested_event.json").readText(),
            ThreadKey("thr_wire_01"),
        ).approval as ApprovalMutation.Requested
        val resolved = codec.decodeEvent(
            findRepositoryFile("gateway/tests/goldens/approval_resolved_event.json").readText(),
            ThreadKey("thr_wire_01"),
        ).approval as ApprovalMutation.Terminal
        val expired = codec.decodeEvent(
            findRepositoryFile("gateway/tests/goldens/approval_expired_event.json").readText(),
            ThreadKey("thr_wire_01"),
        ).approval as ApprovalMutation.Terminal

        assertEquals("action_token_wire_0123456789", requested.actionToken)
        assertEquals(listOf("approve", "reject"), requested.decisions)
        assertEquals("approved", resolved.state)
        assertEquals("expired", expired.state)
    }

    @Test
    fun `gateway recovery snapshot golden atomically carries history approvals and cursor`() {
        val detail = codec.decodeThreadDetail(
            findRepositoryFile("gateway/tests/goldens/thread_recovery_snapshot.json").readText(),
        )

        assertEquals("stream_recovery", detail.streamId)
        assertEquals(42L, detail.cursor)
        assertEquals(listOf(MessageRole.USER, MessageRole.ASSISTANT), detail.messages.map { it.role })
        assertEquals("client-recovery-message", detail.messages.first().clientMessageId)
        assertEquals("approval_recovery", detail.approvals.single().approvalId)
        assertEquals("recovery-action-token-1234567890", detail.approvals.single().actionToken)
    }

    @Test
    fun `thread history preserves user role`() {
        val userBody = threadDetail(
            blocks = """{"text":{"blockId":"text","type":"text","text":"User input"}}""",
            roots = "[\"text\"]",
        ).replace("\"role\":\"assistant\"", "\"role\":\"user\"")

        assertEquals(MessageRole.USER, codec.decodeMessages(userBody).single().role)
    }

    @Test(expected = ProtocolV1Exception::class)
    fun `role outside frozen assistant user enum is rejected`() {
        codec.decodeMessages(
            threadDetail(
                blocks = """{"text":{"blockId":"text","type":"text","text":"Hidden"}}""",
                roots = "[\"text\"]",
            ).replace("\"role\":\"assistant\"", "\"role\":\"system\""),
        )
    }

    private fun findRepositoryFile(relativePath: String): File {
        val workingDirectory = requireNotNull(System.getProperty("user.dir"))
        var cursor: File? = File(workingDirectory).absoluteFile
        while (cursor != null) {
            val candidate = File(cursor, relativePath)
            if (candidate.isFile) return candidate
            cursor = cursor.parentFile
        }
        error("Repository fixture not found")
    }

    private fun event(type: String, payload: String): String = """{
        "v":1,
        "streamId":"stream_public",
        "eventId":"event_public",
        "seq":8,
        "occurredAt":"2026-07-19T00:01:02Z",
        "type":"$type",
        "threadId":"thr_public",
        "payload":$payload
    }"""

    private fun threadDetail(blocks: String, roots: String): String = """{
        "thread":{},
        "messages":[{
            "schema":"cheby.rich-message/1.0",
            "messageId":"msg_public",
            "threadId":"thr_public",
            "turnId":"turn_public",
            "sourceItemId":"item_public",
            "role":"assistant",
            "state":"completed",
            "revision":1,
            "rootBlockIds":$roots,
            "blocks":$blocks,
            "fallback":{"text":"Readable fallback"},
            "createdAt":"2026-07-19T00:00:00Z",
            "updatedAt":"2026-07-19T00:01:00Z"
        }]
    }"""
}
