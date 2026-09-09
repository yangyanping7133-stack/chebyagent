package com.cheby.codex.mobile.ui

import androidx.activity.ComponentActivity
import androidx.lifecycle.ViewModelProvider
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.cheby.codex.mobile.data.InMemoryLastThreadStore
import com.cheby.codex.mobile.gateway.CodexGateway
import com.cheby.codex.mobile.gateway.GatewayCapabilities
import com.cheby.codex.mobile.gateway.GatewayKind
import com.cheby.codex.mobile.gateway.GatewaySessionIdentity
import com.cheby.codex.mobile.gateway.LocalImageCapability
import com.cheby.codex.mobile.gateway.TurnSubmission
import com.cheby.codex.mobile.model.CodexThread
import com.cheby.codex.mobile.model.ConversationEvent
import com.cheby.codex.mobile.model.ConversationSnapshot
import com.cheby.codex.mobile.model.GatewayConnectionState
import com.cheby.codex.mobile.model.ThreadKey
import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.emptyFlow
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class AppViewModelRecreationInstrumentedTest {
    @Test
    fun pickerResultAndAllThreadComposerStateSurviveRealActivityRecreation() {
        val gateway = RecreationGateway()
        val reader = RecordingImageReader()
        val factory = AppViewModel.Factory(
            gateway = gateway,
            lastThreadStore = InMemoryLastThreadStore(ThreadKey("thread-a")),
            imageAttachmentReader = reader,
        )
        val scenario = ActivityScenario.launch(ComponentActivity::class.java)
        try {
            await(scenario) { activity ->
                model(activity, factory).uiState.value.canSend &&
                    model(activity, factory).uiState.value.localImageCapability != null
            }

            var originalIdentity = 0
            scenario.onActivity { activity ->
                val viewModel = model(activity, factory)
                originalIdentity = System.identityHashCode(viewModel)
                val current = ThreadUiKey("recreation", 0, ThreadKey("thread-a"))
                val background = ThreadUiKey("recreation", 0, ThreadKey("thread-b"))
                viewModel.threadUiStates.updateDraft(current, "当前会话草稿")
                viewModel.threadUiStates.saveScroll(current, 17, 29)
                viewModel.threadUiStates.updateDraft(background, "其他会话草稿")
                viewModel.threadUiStates.saveScroll(background, 5, 11)
                assertTrue(viewModel.beginImageSelection())
            }

            scenario.recreate()

            scenario.onActivity { activity ->
                val viewModel = model(activity, factory)
                assertEquals(originalIdentity, System.identityHashCode(viewModel))
                val current = ThreadUiKey("recreation", 0, ThreadKey("thread-a"))
                val background = ThreadUiKey("recreation", 0, ThreadKey("thread-b"))
                assertEquals("当前会话草稿", viewModel.threadUiStates.draft(current))
                assertEquals(ThreadScrollPosition(17, 29), viewModel.threadUiStates.scroll(current))
                assertEquals("其他会话草稿", viewModel.threadUiStates.draft(background))
                assertEquals(ThreadScrollPosition(5, 11), viewModel.threadUiStates.scroll(background))
                viewModel.completeImageSelection(listOf("content://result-after-rotation"))
            }

            await(scenario) { activity ->
                model(activity, factory).uiState.value.selectedImages.size == 1
            }
            assertEquals(listOf("content://result-after-rotation"), reader.lastUris)
            assertEquals(1, reader.calls)
        } finally {
            scenario.close()
        }
    }

    private fun model(
        activity: ComponentActivity,
        factory: AppViewModel.Factory,
    ): AppViewModel = ViewModelProvider(activity, factory)[AppViewModel::class.java]

    private fun await(
        scenario: ActivityScenario<ComponentActivity>,
        predicate: (ComponentActivity) -> Boolean,
    ) {
        val matched = AtomicBoolean(false)
        repeat(150) {
            scenario.onActivity { matched.set(predicate(it)) }
            if (matched.get()) return
            Thread.sleep(20)
        }
        assertTrue("Timed out waiting for Activity/ViewModel state", matched.get())
    }

    private class RecordingImageReader : ImageAttachmentReader {
        var calls = 0
        var lastUris: List<String> = emptyList()

        override suspend fun read(
            sourceUris: List<String>,
            capability: LocalImageCapability,
        ): List<PendingImageAttachment> {
            calls += 1
            lastUris = sourceUris
            return listOf(
                PendingImageAttachment(
                    clientAssetId = "asset-after-recreation",
                    mediaType = "image/png",
                    sourceBytes = byteArrayOf(1),
                    thumbnailBytes = byteArrayOf(2),
                    sourceWidth = 1,
                    sourceHeight = 1,
                ),
            )
        }
    }

    private class RecreationGateway : CodexGateway {
        override val sessionScope = "recreation"
        override val sessionIdentity: StateFlow<GatewaySessionIdentity> = MutableStateFlow(
            GatewaySessionIdentity(sessionScope, 0),
        )
        override val kind: StateFlow<GatewayKind> = MutableStateFlow(GatewayKind.REMOTE)
        override val connectionState: StateFlow<GatewayConnectionState> =
            MutableStateFlow(GatewayConnectionState.ONLINE)
        override val threads: StateFlow<List<CodexThread>> = MutableStateFlow(
            listOf(thread("thread-a"), thread("thread-b")),
        )

        override suspend fun loadCapabilities(
            expectedSessionScope: String,
            expectedSessionGeneration: Long,
        ) = GatewayCapabilities(
            LocalImageCapability(
                uploadVersion = 1,
                mediaTypes = setOf("image/jpeg", "image/png"),
                maxUploadBytes = 8 * 1024 * 1024,
                maxPixels = 25_000_000,
                maxEdgePixels = 12_000,
                maxImagesPerTurn = 10,
            ),
        )

        override suspend fun loadConversation(threadKey: ThreadKey) =
            ConversationSnapshot(threadKey, "stream-${threadKey.value}", 0, emptyList())

        override fun observeConversation(
            threadKey: ThreadKey,
            afterStreamId: String,
            afterStreamSeq: Long,
        ): Flow<ConversationEvent> = emptyFlow()

        override suspend fun createThread(title: String): CodexThread = error("unused")
        override suspend fun renameThread(threadKey: ThreadKey, title: String) = Unit
        override suspend fun archiveThread(threadKey: ThreadKey) = Unit
        override suspend fun deleteThread(threadKey: ThreadKey) = Unit
        override suspend fun sendTurn(
            threadKey: ThreadKey,
            text: String,
            idempotencyKey: String,
            expectedSessionScope: String,
        ) = TurnSubmission(
            turnId = "turn-$idempotencyKey",
            threadKey = threadKey,
            clientMessageId = idempotencyKey,
            status = "running",
        )

        override suspend fun performAction(actionRef: String, expectedSessionScope: String) = Unit

        private fun thread(key: String) = CodexThread(
            key = ThreadKey(key),
            title = key,
            preview = "",
            updatedAtMillis = 1,
        )
    }
}
