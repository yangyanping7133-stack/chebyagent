package com.cheby.codex.mobile.ui

import com.cheby.codex.mobile.data.OutboxEntry
import com.cheby.codex.mobile.data.OutboxState
import com.cheby.codex.mobile.model.ThreadKey
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class LocalImagePresentationTest {
    @Test
    fun `filmstrip counter carries real order and caps at protocol maximum`() {
        assertEquals("0 / 10", evidenceCounterText(0, 10))
        assertEquals("3 / 10", evidenceCounterText(3, 10))
        assertEquals("10 / 10", evidenceCounterText(19, 10))
    }

    @Test
    fun `attachment states have explicit Chinese status text`() {
        assertEquals("已选择", attachmentStatusText(AttachmentUploadState.SELECTED))
        assertEquals("上传中", attachmentStatusText(AttachmentUploadState.UPLOADING))
        assertEquals("已上传", attachmentStatusText(AttachmentUploadState.UPLOADED))
        assertEquals("上传失败", attachmentStatusText(AttachmentUploadState.FAILED))
    }

    @Test
    fun `crash recovery copy requires reselect and leaks no internal value`() {
        val entry = OutboxEntry(
            clientMessageId = "client-image-message-001",
            sessionScope = "scope-a",
            threadKey = ThreadKey("thread-a"),
            text = "inspect",
            state = OutboxState.NEEDS_ATTACHMENTS,
            createdAtMillis = 1,
            pendingImageCount = 2,
        )

        val text = outboxStatusText(entry, preparing = false, nowMillis = 1)

        assertTrue(text.contains("重新选择"))
        assertTrue(text.contains("文字未单独发送"))
        assertFalse(text.contains(entry.clientMessageId))
        assertFalse(text.contains("content://"))
        assertFalse(text.contains("/asset-staging/"))
    }

    @Test
    fun `rejected uploaded image explains the only safe recovery`() {
        val entry = OutboxEntry(
            clientMessageId = "client-image-message-001",
            sessionScope = "scope-a",
            threadKey = ThreadKey("thread-a"),
            text = "inspect",
            state = OutboxState.REJECTED,
            createdAtMillis = 1,
            imageAssetRefs = listOf("ast_abcdefghijklmnopqrstuvwxyz0123456789"),
        )

        val text = outboxStatusText(entry, preparing = false, nowMillis = 1)

        assertTrue(text.contains("移除"))
        assertTrue(text.contains("重新选择"))
        assertFalse(text.contains(entry.imageAssetRefs.single()))
    }
}
