package com.cheby.codex.mobile.ui

import com.cheby.codex.mobile.data.InMemoryLastThreadStore
import com.cheby.codex.mobile.gateway.CodexGateway
import com.cheby.codex.mobile.gateway.GatewayKind
import com.cheby.codex.mobile.gateway.GatewayTransportException
import com.cheby.codex.mobile.gateway.AssistantEnrollmentControls
import com.cheby.codex.mobile.gateway.TurnSubmission
import com.cheby.codex.mobile.model.CodexThread
import com.cheby.codex.mobile.model.ConversationEvent
import com.cheby.codex.mobile.model.ConversationSnapshot
import com.cheby.codex.mobile.model.GatewayConnectionState
import com.cheby.codex.mobile.model.ThreadKey
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.emptyFlow
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class PairingViewModelTest {
    private val dispatcher = StandardTestDispatcher()

    @Before
    fun setUp() {
        Dispatchers.setMain(dispatcher)
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    @Test
    fun `unconfigured state opens enrollment and successful exchange enters relay`() = runTest(dispatcher) {
        val gateway = PairingFakeGateway(fail = false)
        val viewModel = AppViewModel(gateway, InMemoryLastThreadStore())
        advanceUntilIdle()

        assertTrue(viewModel.uiState.value.showPairing)
        viewModel.enrollAssistant(
            "https://relay.example",
            "asst_0000000000000000000000",
            "pair_0000000000000000000000000000000000000000000",
        )
        advanceUntilIdle()

        assertEquals(GatewayKind.RELAY, viewModel.uiState.value.gatewayKind)
        assertFalse(viewModel.uiState.value.showPairing)
        assertEquals(null, viewModel.uiState.value.pairingError)
    }

    @Test
    fun `pairing failure is generic and never reflects secret`() = runTest(dispatcher) {
        val gateway = PairingFakeGateway(fail = true)
        val viewModel = AppViewModel(gateway, InMemoryLastThreadStore())
        advanceUntilIdle()
        val accessKey = "asst_0000000000000000000000"
        val secret = "pair_0000000000000000000000000000000000000000000"

        viewModel.enrollAssistant("https://relay.example", accessKey, secret)
        advanceUntilIdle()

        val error = viewModel.uiState.value.pairingError.orEmpty()
        assertTrue(error.isNotBlank())
        assertFalse(error.contains(secret))
        assertEquals(GatewayKind.UNCONFIGURED, viewModel.uiState.value.gatewayKind)
    }

    @Test
    fun `bound relay can change its client configured server address`() = runTest(dispatcher) {
        val gateway = PairingFakeGateway(fail = false).apply { enterRelay() }
        val viewModel = AppViewModel(gateway, InMemoryLastThreadStore())
        advanceUntilIdle()

        viewModel.openPairing()
        viewModel.changeAssistantServer(" https://203.0.113.20:27461 ")
        advanceUntilIdle()

        assertEquals("https://203.0.113.20:27461", gateway.changedOrigin)
        assertFalse(viewModel.uiState.value.showPairing)
        assertEquals(null, viewModel.uiState.value.pairingError)
    }

    private class PairingFakeGateway(private val fail: Boolean) :
        CodexGateway,
        AssistantEnrollmentControls {
        override val sessionScope: String = "pairing-test"
        private val mutableKind = MutableStateFlow(GatewayKind.UNCONFIGURED)
        override val kind: StateFlow<GatewayKind> = mutableKind
        override val connectionState: StateFlow<GatewayConnectionState> =
            MutableStateFlow(GatewayConnectionState.OFFLINE)
        override val threads: StateFlow<List<CodexThread>> = MutableStateFlow(emptyList())

        override suspend fun connect(relayOrigin: String, accessKey: String, secretKey: String) {
            if (fail) throw GatewayTransportException("Rejected")
            mutableKind.value = GatewayKind.RELAY
        }

        var changedOrigin: String? = null
            private set

        fun enterRelay() {
            mutableKind.value = GatewayKind.RELAY
        }

        override suspend fun changeRelayOrigin(relayOrigin: String) {
            if (fail) throw GatewayTransportException("Rejected")
            changedOrigin = relayOrigin
        }

        override suspend fun createThread(title: String): CodexThread = unsupported()
        override suspend fun renameThread(threadKey: ThreadKey, title: String) = unsupported<Unit>()
        override suspend fun archiveThread(threadKey: ThreadKey) = unsupported<Unit>()
        override suspend fun deleteThread(threadKey: ThreadKey) = unsupported<Unit>()
        override suspend fun loadConversation(threadKey: ThreadKey): ConversationSnapshot = unsupported()
        override fun observeConversation(
            threadKey: ThreadKey,
            afterStreamId: String,
            afterStreamSeq: Long,
        ): Flow<ConversationEvent> = emptyFlow()
        override suspend fun sendTurn(
            threadKey: ThreadKey,
            text: String,
            idempotencyKey: String,
            expectedSessionScope: String,
        ): TurnSubmission = unsupported()
        override suspend fun performAction(actionRef: String, expectedSessionScope: String) = unsupported<Unit>()

        private fun <T> unsupported(): T = throw GatewayTransportException("Unsupported")
    }
}
