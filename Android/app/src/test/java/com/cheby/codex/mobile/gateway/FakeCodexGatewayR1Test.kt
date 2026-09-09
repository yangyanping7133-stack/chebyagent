package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.model.DiffBlock
import com.cheby.codex.mobile.model.FileBlock
import com.cheby.codex.mobile.model.MessageRole
import com.cheby.codex.mobile.model.SourceBlock
import com.cheby.codex.mobile.model.TerminalBlock
import com.cheby.codex.mobile.model.TestBlock
import com.cheby.codex.mobile.model.ThreadKey
import com.cheby.codex.mobile.model.ToolBlock
import com.cheby.codex.mobile.model.UnknownBlock
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

class FakeCodexGatewayR1Test {
    @Test
    fun `demo updates one assistant panel and labels all evidence as local demo`() = runBlocking {
        val gateway = FakeCodexGateway()
        try {
            assertFalse(gateway.threads.value.any { it.title.contains("Turkey", ignoreCase = true) })
            val threadKey = ThreadKey("demo-main")
            gateway.sendTurn(threadKey, "Prepare a generic software summary", "demo-r1-id", gateway.sessionScope)

            val snapshot = withTimeout(5_000) {
                var current = gateway.loadConversation(threadKey)
                while (current.messages.none { it.blocks["test"] is TestBlock }) {
                    delay(50)
                    current = gateway.loadConversation(threadKey)
                }
                current
            }
            val userMessages = snapshot.messages.filter { it.role == MessageRole.USER }
            val taskPanels = snapshot.messages.filter { it.role == MessageRole.ASSISTANT && it.blocks.containsKey("status") }

            assertEquals(1, userMessages.size)
            assertEquals(1, taskPanels.size)
            val panel = taskPanels.single()
            assertNotNull(panel.blocks["terminal"] as? TerminalBlock)
            assertNotNull(panel.blocks["file"] as? FileBlock)
            assertNotNull(panel.blocks["diff"] as? DiffBlock)
            assertNotNull(panel.blocks["tool"] as? ToolBlock)
            assertNotNull(panel.blocks["test"] as? TestBlock)
            val source = panel.blocks["source"] as SourceBlock
            assertTrue(source.detail.contains("Fake Gateway"))
            assertTrue(source.trustLabel?.contains("非远端验收证据") == true)
        } finally {
            gateway.close()
        }
    }

    @Test
    fun `edge demo keeps long terminal bounded and unknown block readable`() = runBlocking {
        val gateway = FakeCodexGateway()
        try {
            val threadKey = ThreadKey("demo-main")
            gateway.sendTurn(threadKey, "#demo-edge", "demo-edge-id", gateway.sessionScope)
            val snapshot = withTimeout(3_000) {
                var current = gateway.loadConversation(threadKey)
                while (current.messages.none { it.blocks["future"] is UnknownBlock }) {
                    delay(25)
                    current = gateway.loadConversation(threadKey)
                }
                current
            }
            val panel = snapshot.messages.single { it.blocks["future"] is UnknownBlock }
            val terminal = panel.blocks["terminal"] as TerminalBlock
            val unknown = panel.blocks["future"] as UnknownBlock

            assertEquals(40, terminal.output.lineSequence().count())
            assertTrue(unknown.fallbackText.contains("安全显示文字摘要"))
            assertEquals(1, snapshot.messages.count { it.blocks.containsKey("status") })
        } finally {
            gateway.close()
        }
    }
}
