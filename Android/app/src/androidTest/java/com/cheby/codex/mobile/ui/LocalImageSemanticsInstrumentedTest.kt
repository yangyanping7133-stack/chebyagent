package com.cheby.codex.mobile.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithText
import org.junit.Rule
import org.junit.Test

class LocalImageSemanticsInstrumentedTest {
    @get:Rule
    val compose = createComposeRule()

    @Test
    fun selectedStateIsVisibleAndTalkBackReadable() {
        assertVisibleState(AttachmentUploadState.SELECTED, "已选择")
    }

    @Test
    fun uploadingStateIsVisibleAndTalkBackReadable() {
        assertVisibleState(AttachmentUploadState.UPLOADING, "上传中")
    }

    @Test
    fun uploadedStateIsVisibleAndTalkBackReadable() {
        assertVisibleState(AttachmentUploadState.UPLOADED, "已上传")
    }

    @Test
    fun failedStateIsVisibleAndTalkBackReadable() {
        assertVisibleState(AttachmentUploadState.FAILED, "上传失败")
    }

    private fun assertVisibleState(state: AttachmentUploadState, status: String) {
        compose.setContent {
            MaterialTheme {
                EvidenceFilmstrip(
                    images = listOf(
                        PendingImageAttachment(
                            clientAssetId = "semantics-$state",
                            mediaType = "image/png",
                            sourceBytes = byteArrayOf(1),
                            thumbnailBytes = byteArrayOf(),
                            sourceWidth = 1,
                            sourceHeight = 1,
                            uploadState = state,
                        ),
                    ),
                    maxImages = 10,
                    canRemove = false,
                    onRemoveImage = {},
                )
            }
        }

        compose.onNodeWithText(status).assertIsDisplayed()
        compose.onNodeWithContentDescription("第 1 张视觉证据，$status").assertIsDisplayed()
    }
}
