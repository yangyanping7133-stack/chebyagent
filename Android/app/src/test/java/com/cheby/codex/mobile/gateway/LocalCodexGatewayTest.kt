package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.model.ConversationEvent
import com.cheby.codex.mobile.model.EphemeralMediaStore
import com.cheby.codex.mobile.model.MediaBlock
import com.cheby.codex.mobile.model.MessageRole
import com.cheby.codex.mobile.model.MessagePresentation
import com.cheby.codex.mobile.model.PatchOperation
import com.cheby.codex.mobile.model.ThreadKey
import com.cheby.codex.mobile.model.TextBlock
import com.cheby.codex.mobile.model.TurnLifecyclePhase
import com.cheby.codex.mobile.model.UnknownBlock
import java.nio.file.Files
import java.util.Base64
import java.util.UUID
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import kotlinx.coroutines.async
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import kotlinx.serialization.json.put
import okhttp3.OkHttpClient
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

class LocalCodexGatewayTest {
    @Test(timeout = 15_000)
    fun `repeated runtime warnings update one notice and official terminal replaces waiting text`() = runBlocking {
        for ((status, text) in listOf("completed" to "本轮已完成。", "interrupted" to "本轮已停止。",
            "failed" to "本轮执行失败，请检查连接后手动重试。")) {
            val fixture = AppServerFixture(runtimeErrors = 3, terminalStatus = status)
            val server = MockWebServer()
            server.enqueue(MockResponse().withWebSocketUpgrade(fixture))
            val gateway = LocalCodexGateway(TEST_TOKEN,
                LocalAppServerClient(bearerToken = TEST_TOKEN, endpoint = server.url("/").toString()))
            try {
                val thread = gateway.createThread("runtime notices")
                val events = mutableListOf<ConversationEvent>()
                val terminal = async {
                    withTimeout(5_000) {
                        gateway.observeConversation(thread.key, "", -1).first {
                            events += it
                            it is ConversationEvent.TurnLifecycle && it.phase != TurnLifecyclePhase.STARTED
                        }
                    }
                }
                gateway.sendTurn(thread.key, "test", CLIENT_MESSAGE_ID)
                terminal.await()
                val notices = events.filterIsInstance<ConversationEvent.MessageAdded>()
                    .filter { it.message.id == "runtime-$TURN_ID" }
                assertEquals(1, notices.size)
                val updates = events.filterIsInstance<ConversationEvent.MessagePatched>()
                    .filter { it.messageId == "runtime-$TURN_ID" }
                assertEquals(1, updates.size)
                assertEquals(text, (updates.single().operations.single() as PatchOperation.PutBlock).block.fallbackText)
                assertEquals(1, fixture.turnStartCalls.get())
                assertEquals(1, server.requestCount)
            } finally {
                gateway.close()
                server.shutdown()
            }
        }
    }

    @Test(timeout = 40_000)
    fun `request timeout reports unknown delivery without resend or reconnect`() = runBlocking {
        val fixture = AppServerFixture(stallTurnRequest = true)
        val server = MockWebServer()
        server.enqueue(MockResponse().withWebSocketUpgrade(fixture))
        val gateway = LocalCodexGateway(TEST_TOKEN,
            LocalAppServerClient(bearerToken = TEST_TOKEN, endpoint = server.url("/").toString()))
        try {
            withTimeout(5_000) {
                gateway.connectionState.first { it == com.cheby.codex.mobile.model.GatewayConnectionState.ONLINE }
            }
            val thread = gateway.createThread("timeout")
            val failure = runCatching { gateway.sendTurn(thread.key, "test", "timeout-test") }.exceptionOrNull()
            assertTrue(failure is GatewayTransportException)
            assertEquals(1, fixture.turnStartCalls.get())
            assertEquals(1, server.requestCount)
            assertEquals(com.cheby.codex.mobile.model.GatewayConnectionState.ONLINE, gateway.connectionState.value)
        } finally {
            gateway.close()
            server.shutdown()
        }
    }

    @Test(timeout = 10_000)
    fun `model and effort are selected per thread from the official catalog`() = runBlocking {
        val fixture = AppServerFixture(listCreatedThread = true)
        val server = MockWebServer()
        server.enqueue(MockResponse().withWebSocketUpgrade(fixture))
        val gateway = LocalCodexGateway(TEST_TOKEN,
            LocalAppServerClient(bearerToken = TEST_TOKEN, endpoint = server.url("/").toString()))
        try {
            withTimeout(5_000) {
                gateway.connectionState.first { it == com.cheby.codex.mobile.model.GatewayConnectionState.ONLINE }
            }
            val models = gateway.loadCapabilities().models
            assertEquals(listOf("none", "low", "medium", "high", "xhigh", "max"),
                models.single().supportedReasoningEfforts.map(ModelReasoningEffort::value))
            gateway.loadConversation(ThreadKey(THREAD_ID))
            assertTrue(gateway.selectThreadModel(ThreadKey(THREAD_ID), "gpt-5.6-sol", "low"))
            gateway.sendTurn(ThreadKey(THREAD_ID), "测试", "effort-regression")
            assertEquals("low", fixture.lastTurnParams.get()?.get("effort")?.jsonPrimitive?.content)
            assertTrue(gateway.selectThreadModel(ThreadKey(THREAD_ID), "gpt-5.6-sol", "medium"))
            gateway.sendTurn(ThreadKey(THREAD_ID), "再测试", "effort-medium-regression")
            assertEquals("medium", fixture.lastTurnParams.get()?.get("effort")?.jsonPrimitive?.content)
            assertEquals("gpt-5.6-sol", fixture.lastTurnParams.get()?.get("model")?.jsonPrimitive?.content)
        } finally {
            gateway.close()
            server.shutdown()
        }
    }

    @Test(timeout = 10_000)
    fun `reading an existing active thread restores official turn for stop without sending`() = runBlocking {
        val fixture = AppServerFixture(listCreatedThread = true, restoredActive = true)
        val server = MockWebServer()
        server.enqueue(MockResponse().withWebSocketUpgrade(fixture))
        val gateway = LocalCodexGateway(TEST_TOKEN,
            LocalAppServerClient(bearerToken = TEST_TOKEN, endpoint = server.url("/").toString()))
        try {
            withTimeout(5_000) {
                gateway.connectionState.first { it == com.cheby.codex.mobile.model.GatewayConnectionState.ONLINE }
            }
            gateway.loadConversation(ThreadKey(THREAD_ID))
            val thread = gateway.threads.value.first { it.key == ThreadKey(THREAD_ID) }
            assertEquals(com.cheby.codex.mobile.model.ThreadRunState.RUNNING, thread.state)
            assertEquals(TURN_ID, thread.lastTurnId)
            gateway.interruptTurn(thread.key, requireNotNull(thread.lastTurnId))
            assertEquals(TURN_ID, fixture.lastInterruptParams.get()?.get("turnId")?.jsonPrimitive?.content)
            assertEquals(0, fixture.turnStartCalls.get())
        } finally {
            gateway.close()
            server.shutdown()
        }
    }

    @Test(timeout = 10_000)
    fun `stop targets the exact turn and waits for upstream terminal event`() = runBlocking {
        val fixture = AppServerFixture(stallMainTurn = true)
        val server = MockWebServer()
        server.enqueue(MockResponse().withWebSocketUpgrade(fixture))
        val gateway = LocalCodexGateway(TEST_TOKEN,
            LocalAppServerClient(bearerToken = TEST_TOKEN, endpoint = server.url("/").toString()))
        try {
            withTimeout(5_000) {
                gateway.connectionState.first { it == com.cheby.codex.mobile.model.GatewayConnectionState.ONLINE }
            }
            val thread = gateway.createThread("停止测试")
            val submission = gateway.sendTurn(thread.key, "执行测试", "stop-test-message")
            withTimeout(2_000) {
                gateway.threads.first { rows -> rows.any { it.key == thread.key && it.lastTurnId == submission.turnId } }
            }
            gateway.interruptTurn(thread.key, submission.turnId)
            val params = requireNotNull(fixture.lastInterruptParams.get())
            assertEquals(THREAD_ID, params["threadId"]?.jsonPrimitive?.content)
            assertEquals(TURN_ID, params["turnId"]?.jsonPrimitive?.content)
            assertEquals(com.cheby.codex.mobile.model.ThreadRunState.RUNNING,
                gateway.threads.value.first { it.key == thread.key }.state)
            assertTrue(runCatching {
                gateway.interruptTurn(thread.key, submission.turnId, "wrong-session", 0)
            }.isFailure)
            assertEquals(1, fixture.interruptCalls.get())
        } finally {
            gateway.close()
            server.shutdown()
        }
    }

    @Test(timeout = 10_000)
    fun `raw loopback transport authenticates without websocket extensions`() = runBlocking {
        val server = MockWebServer()
        server.enqueue(MockResponse().withWebSocketUpgrade(AppServerFixture()))
        server.start()
        val endpoint = "ws://127.0.0.1:${server.port}/"
        val gateway = LocalCodexGateway(
            TEST_TOKEN,
            LocalAppServerClient(bearerToken = TEST_TOKEN, endpoint = endpoint),
        )
        try {
            withTimeout(5_000) {
                gateway.connectionState.first {
                    it == com.cheby.codex.mobile.model.GatewayConnectionState.ONLINE
                }
            }
            val handshake = server.takeRequest(2, TimeUnit.SECONDS)
            assertEquals("Bearer $TEST_TOKEN", handshake?.getHeader("Authorization"))
            assertEquals(null, handshake?.getHeader("Sec-WebSocket-Extensions"))
        } finally {
            gateway.close()
            server.shutdown()
        }
    }

    @Test(timeout = 10_000)
    fun `authenticated app server drives thread turn and conversation projection`() = runBlocking {
        val fixture = AppServerFixture()
        val server = MockWebServer()
        server.enqueue(MockResponse().withWebSocketUpgrade(fixture))
        val endpoint = server.url("/").toString()
        val client = LocalAppServerClient(
            bearerToken = TEST_TOKEN,
            httpClient = OkHttpClient.Builder().pingInterval(1, TimeUnit.SECONDS).build(),
            endpoint = endpoint,
        )
        val gateway = LocalCodexGateway(TEST_TOKEN, client)
        try {
            withTimeout(5_000) {
                gateway.connectionState.first { it == com.cheby.codex.mobile.model.GatewayConnectionState.ONLINE }
            }
            val handshake = server.takeRequest(2, TimeUnit.SECONDS)
            assertEquals("Bearer $TEST_TOKEN", handshake?.getHeader("Authorization"))
            assertEquals(null, handshake?.getHeader("Sec-WebSocket-Extensions"))
            assertEquals(
                "true",
                fixture.lastInitializeParams.get()
                    ?.get("capabilities")
                    ?.jsonObject
                    ?.get("experimentalApi")
                    ?.jsonPrimitive
                    ?.content,
            )

            val thread = gateway.createThread("手机单机验证")
            assertEquals(ThreadKey(THREAD_ID), thread.key)
            assertEquals("手机单机验证", thread.title)
            withTimeout(2_000) {
                gateway.threads.first { threads ->
                    threads.any { it.key == ThreadKey(EXISTING_THREAD_ID) }
                }
            }
            val listParams = requireNotNull(fixture.lastThreadListParams.get())
            assertEquals(null, listParams["cwd"])
            assertEquals("false", listParams["archived"]?.jsonPrimitive?.content)
            assertEquals("false", listParams["useStateDbOnly"]?.jsonPrimitive?.content)
            assertEquals(
                listOf(
                    "cli",
                    "vscode",
                    "exec",
                    "appServer",
                    "subAgent",
                    "subAgentReview",
                    "subAgentCompact",
                    "subAgentThreadSpawn",
                    "subAgentOther",
                    "unknown",
                ),
                listParams["sourceKinds"]?.jsonArray?.map { it.jsonPrimitive.content },
            )
            assertEquals(
                "An empty newly-created thread must survive the name-triggered catalog refresh",
                listOf(ThreadKey(THREAD_ID), ThreadKey(EXISTING_THREAD_ID)),
                gateway.threads.value.map { it.key },
            )

            val terminal = async {
                withTimeout(5_000) {
                    gateway.observeConversation(thread.key, "", -1).first { event ->
                        event is ConversationEvent.TurnLifecycle &&
                            event.phase == TurnLifecyclePhase.COMPLETED
                    }
                }
            }
            val streamedAgent = async {
                withTimeout(5_000) {
                    gateway.observeConversation(thread.key, "", -1).first { event ->
                        event is ConversationEvent.MessageAdded &&
                            event.message.id == AGENT_ITEM_ID &&
                            event.message.blocks.values.filterIsInstance<TextBlock>()
                                .any { it.text == "手机在线" }
                    }
                }
            }
            val finalAnswerPatch = async {
                withTimeout(5_000) {
                    gateway.observeConversation(thread.key, "", -1).first { event ->
                        event is ConversationEvent.MessagePatched &&
                            event.messageId == AGENT_ITEM_ID &&
                            event.operations.any {
                                it is PatchOperation.SetMessagePresentation &&
                                    it.presentation == MessagePresentation.FINAL_ANSWER
                            }
                    }
                }
            }
            val submission = gateway.sendTurn(
                threadKey = thread.key,
                text = "调用手机状态工具",
                idempotencyKey = CLIENT_MESSAGE_ID,
                expectedSessionScope = gateway.sessionScope,
            )
            assertEquals(TURN_ID, submission.turnId)
            terminal.await()
            streamedAgent.await()
            finalAnswerPatch.await()
            assertEquals(CLIENT_MESSAGE_ID, fixture.lastClientMessageId.get())
            val turnParams = fixture.lastTurnParams.get()
            assertFalse(requireNotNull(turnParams).containsKey("outputSchema"))
            assertEquals("high", turnParams["effort"]?.jsonPrimitive?.content)
            assertFalse(turnParams.containsKey("serviceTier"))

            val snapshot = gateway.loadConversation(thread.key)
            assertEquals(2, snapshot.messages.size)
            assertEquals(MessageRole.USER, snapshot.messages[0].role)
            assertEquals(CLIENT_MESSAGE_ID, snapshot.messages[0].clientMessageId)
            assertEquals(MessageRole.ASSISTANT, snapshot.messages[1].role)
            assertEquals(MessagePresentation.FINAL_ANSWER, snapshot.messages[1].presentation)
            assertTrue(snapshot.messages[1].blocks.values.first().fallbackText.contains("手机在线"))
        } finally {
            gateway.close()
            server.shutdown()
        }
    }

    @Test(timeout = 10_000)
    fun `official app server account surface drives device code login`() = runBlocking {
        val fixture = AppServerFixture()
        val server = MockWebServer()
        server.enqueue(MockResponse().withWebSocketUpgrade(fixture))
        server.start()
        val gateway = LocalCodexGateway(
            TEST_TOKEN,
            LocalAppServerClient(bearerToken = TEST_TOKEN, endpoint = server.url("/").toString()),
        )
        try {
            withTimeout(5_000) {
                gateway.connectionState.first {
                    it == com.cheby.codex.mobile.model.GatewayConnectionState.ONLINE
                }
            }
            val account = gateway.readOpenAiAccount()
            assertTrue(account.signedIn)
            assertEquals("plus", account.planType)
            val login = gateway.startOpenAiDeviceLogin()
            assertEquals("https://auth.openai.com/codex/device", login.verificationUrl)
            assertEquals("ABCD-1234", login.userCode)
            gateway.cancelOpenAiDeviceLogin(login.loginId)
            gateway.logoutOpenAiAccount()
            assertTrue(fixture.accountMethods.containsAll(listOf(
                "account/read", "account/login/start", "account/login/cancel", "account/logout",
            )))
        } finally {
            gateway.close()
            server.shutdown()
        }
    }

    @Test(timeout = 10_000)
    fun `native app server items pass through without custom response decoding`() = runBlocking {
        val structuredLookingText = """{"version":1,"type":"selection","title":"原样文本"}"""
        val fixture = AppServerFixture(
            historyAgentText = structuredLookingText,
            includeNativeItems = true,
            includeBlankReasoning = true,
        )
        val server = MockWebServer()
        server.enqueue(MockResponse().withWebSocketUpgrade(fixture))
        val gateway = LocalCodexGateway(
            TEST_TOKEN,
            LocalAppServerClient(bearerToken = TEST_TOKEN, endpoint = server.url("/").toString()),
        )
        try {
            val snapshot = gateway.loadConversation(ThreadKey(THREAD_ID))
            val assistantTexts = snapshot.messages
                .filter { it.role == MessageRole.ASSISTANT }
                .flatMap { it.blocks.values }
                .filterIsInstance<TextBlock>()
                .map(TextBlock::text)
            assertTrue(assistantTexts.contains("已检查环境并准备执行。"))
            assertTrue(assistantTexts.contains(structuredLookingText))
            assertFalse(assistantTexts.any(String::isBlank))
            assertTrue(snapshot.messages.flatMap { it.blocks.values }.any { it is UnknownBlock })
            assertEquals(
                MessagePresentation.FINAL_ANSWER,
                snapshot.messages.first { it.id == AGENT_ITEM_ID }.presentation,
            )
            assertTrue(
                snapshot.messages
                    .filter { it.id != AGENT_ITEM_ID && it.role == MessageRole.ASSISTANT }
                    .all { it.presentation == MessagePresentation.PROCESS },
            )
        } finally {
            gateway.close()
            server.shutdown()
        }
    }

    @Test(timeout = 10_000)
    fun `stalled initial catalog refresh does not tear down the usable socket`() = runBlocking {
        val fixture = AppServerFixture(stallThreadList = true)
        val server = MockWebServer()
        server.enqueue(MockResponse().withWebSocketUpgrade(fixture))
        val gateway = LocalCodexGateway(
            TEST_TOKEN,
            LocalAppServerClient(bearerToken = TEST_TOKEN, endpoint = server.url("/").toString()),
        )
        try {
            withTimeout(5_000) {
                gateway.connectionState.first {
                    it == com.cheby.codex.mobile.model.GatewayConnectionState.ONLINE
                }
            }

            val thread = gateway.createThread("列表超时仍可用")

            assertEquals(ThreadKey(THREAD_ID), thread.key)
            assertEquals(1, server.requestCount)
            assertEquals(
                com.cheby.codex.mobile.model.GatewayConnectionState.ONLINE,
                gateway.connectionState.value,
            )
        } finally {
            gateway.close()
            server.shutdown()
        }
    }

    @Test(timeout = 10_000)
    fun `silent active turn does not restart the official app server connection`() = runBlocking {
        val stalled = AppServerFixture(stallMainTurn = true)
        val recovered = AppServerFixture(listCreatedThread = true)
        val server = MockWebServer()
        server.enqueue(MockResponse().withWebSocketUpgrade(stalled))
        server.enqueue(MockResponse().withWebSocketUpgrade(recovered))
        val client = LocalAppServerClient(
            bearerToken = TEST_TOKEN,
            httpClient = OkHttpClient.Builder().pingInterval(1, TimeUnit.SECONDS).build(),
            endpoint = server.url("/").toString(),
        )
        val gateway = LocalCodexGateway(
            TEST_TOKEN,
            client,
        )
        try {
            withTimeout(5_000) {
                gateway.connectionState.first {
                    it == com.cheby.codex.mobile.model.GatewayConnectionState.ONLINE
                }
            }
            val thread = gateway.createThread("路线恢复")
            gateway.sendTurn(
                threadKey = thread.key,
                text = "带我去冬宫",
                idempotencyKey = CLIENT_MESSAGE_ID,
            )

            kotlinx.coroutines.delay(350)
            assertEquals(1, server.requestCount)
        } finally {
            gateway.close()
            server.shutdown()
        }
    }

    @Test(timeout = 10_000)
    fun `credential recovery refuses to mutate settings while a turn is active`() = runBlocking {
        val server = MockWebServer()
        server.enqueue(MockResponse().withWebSocketUpgrade(AppServerFixture(stallMainTurn = true)))
        val gateway = LocalCodexGateway(
            TEST_TOKEN,
            LocalAppServerClient(bearerToken = TEST_TOKEN, endpoint = server.url("/").toString()),
        )
        try {
            val thread = gateway.createThread("安全配置恢复")
            gateway.sendTurn(thread.key, "继续任务", CLIENT_MESSAGE_ID)
            withTimeout(5_000) {
                gateway.observeConversation(thread.key, "", -1).first {
                    it is ConversationEvent.TurnLifecycle && it.phase == TurnLifecyclePhase.STARTED
                }
            }
            var changed = false
            val error = gateway.changeConfigurationWhenIdle { changed = true; null }
            assertFalse(changed)
            assertTrue(error?.contains("任务") == true)
            assertEquals(1, server.requestCount)
        } finally {
            gateway.close()
            server.shutdown()
        }
    }

    @Test(timeout = 10_000)
    fun `thread recovery projects bounded MCP screenshot without exposing payload`() {
        runBlocking {
            EphemeralMediaStore.clearForTest()
            val fixture = AppServerFixture(includeScreenshot = true)
            val server = MockWebServer()
            server.enqueue(MockResponse().withWebSocketUpgrade(fixture))
            val client = LocalAppServerClient(
                bearerToken = TEST_TOKEN,
                httpClient = OkHttpClient.Builder().pingInterval(1, TimeUnit.SECONDS).build(),
                endpoint = server.url("/").toString(),
            )
            val gateway = LocalCodexGateway(TEST_TOKEN, client)
            try {
                withTimeout(5_000) {
                    gateway.connectionState.first {
                        it == com.cheby.codex.mobile.model.GatewayConnectionState.ONLINE
                    }
                }

                val snapshot = gateway.loadConversation(ThreadKey(THREAD_ID))
                val media = snapshot.messages
                    .flatMap { it.blocks.values }
                    .filterIsInstance<MediaBlock>()
                    .single()
                val asset = EphemeralMediaStore.get(media.assetId)

                assertTrue(media.assetId.startsWith("local-mcp-"))
                assertEquals("手机截图", media.caption)
                assertFalse(media.fallbackText.contains(SCREENSHOT_BASE64))
                assertFalse(media.fallbackText.contains("/root/"))
                assertNotNull(asset)
                assertEquals("image/png", asset?.mediaType)
                assertTrue(asset?.bytes?.contentEquals(Base64.getDecoder().decode(SCREENSHOT_BASE64)) == true)
                asset?.bytes?.fill(0)
            } finally {
                gateway.close()
                server.shutdown()
                EphemeralMediaStore.clearForTest()
            }
        }
    }

    @Test(timeout = 10_000)
    fun `ten attachments stay in one turn as guest file references with original images retained`() = runBlocking {
        EphemeralMediaStore.clearForTest()
        val sandbox = Files.createTempDirectory("local-gateway-image").toFile()
        val fixture = AppServerFixture()
        val server = MockWebServer()
        server.enqueue(MockResponse().withWebSocketUpgrade(fixture))
        val client = LocalAppServerClient(
            bearerToken = TEST_TOKEN,
            httpClient = OkHttpClient.Builder().pingInterval(1, TimeUnit.SECONDS).build(),
            endpoint = server.url("/").toString(),
        )
        val store = LocalTurnImageStore(
            rootDirectory = sandbox.resolve("private-images"),
            codexRootPath = "/root/.cheby/turn-inputs",
            runtimeReady = { true },
        )
        val gateway = LocalCodexGateway(TEST_TOKEN, client, store)
        try {
            withTimeout(5_000) {
                gateway.connectionState.first {
                    it == com.cheby.codex.mobile.model.GatewayConnectionState.ONLINE
                }
            }
            val capability = gateway.loadCapabilities().localImage
            assertNotNull(capability)
            assertEquals(10, capability?.maxImagesPerTurn)

            val thread = gateway.createThread("图片理解")
            val sourceBytes = Base64.getDecoder().decode(SCREENSHOT_BASE64)
            val uploaded = List(10) {
                gateway.uploadTurnImage(
                    threadKey = thread.key,
                    clientMessageId = CLIENT_MESSAGE_ID,
                    clientAssetId = UUID.randomUUID().toString(),
                    bytes = sourceBytes,
                )
            }
            val terminal = async {
                withTimeout(5_000) {
                    gateway.observeConversation(thread.key, "", -1).first { event ->
                        event is ConversationEvent.TurnLifecycle &&
                            event.phase == TurnLifecyclePhase.COMPLETED
                    }
                }
            }

            gateway.sendTurnInput(
                threadKey = thread.key,
                input = buildList {
                    add(TurnInput.Text("点菜"))
                    uploaded.forEach { add(TurnInput.Image(it.assetRef)) }
                },
                idempotencyKey = CLIENT_MESSAGE_ID,
            )
            terminal.await()

            val privateInput = requireNotNull(fixture.lastTurnInput.get())
            val turnParams = requireNotNull(fixture.lastTurnParams.get())
            assertFalse(turnParams.containsKey("outputSchema"))
            assertEquals("never", turnParams["approvalPolicy"]?.jsonPrimitive?.content)
            assertEquals(
                "dangerFullAccess",
                turnParams["sandboxPolicy"]?.jsonObject?.get("type")?.jsonPrimitive?.content,
            )
            assertEquals("high", turnParams["effort"]?.jsonPrimitive?.content)
            assertFalse(turnParams.containsKey("serviceTier"))
            assertFalse(turnParams.containsKey("additionalContext"))
            assertEquals(11, privateInput.size)
            assertEquals("text", privateInput[0].jsonObject["type"]?.jsonPrimitive?.content)
            assertEquals("点菜", privateInput[0].jsonObject["text"]?.jsonPrimitive?.content)
            privateInput.drop(1).forEach { attachment ->
                assertEquals("localImage", attachment.jsonObject["type"]?.jsonPrimitive?.content)
                assertEquals("auto", attachment.jsonObject["detail"]?.jsonPrimitive?.content)
                val reference = attachment.jsonObject["path"]?.jsonPrimitive?.content.orEmpty()
                assertTrue(reference.startsWith("/root/.cheby/turn-inputs/"))
                assertFalse(reference.contains(THREAD_ID))
                assertFalse(reference.contains(CLIENT_MESSAGE_ID))
            }
            assertEquals(1, fixture.threadStartParams.size)
            assertFalse(fixture.threadStartParams.single().containsKey("ephemeral"))
            assertEquals(1, fixture.turnStartCalls.get())
            uploaded.forEach { assertFalse(privateInput.toString().contains(it.assetRef)) }

            val sourceMedia = gateway.loadConversation(thread.key).messages
                .first { it.role == MessageRole.USER }
                .blocks.values
                .filterIsInstance<MediaBlock>()
            assertEquals(10, sourceMedia.size)
            sourceMedia.forEachIndexed { index, media ->
                val original = EphemeralMediaStore.get(media.assetId)
                assertTrue(media.assetId.startsWith("local-source-"))
                assertEquals("原始图片 ${index + 1}", media.caption)
                assertTrue(original?.bytes?.contentEquals(sourceBytes) == true)
                original?.bytes?.fill(0)
            }
            Unit
        } finally {
            gateway.close()
            server.shutdown()
            sandbox.deleteRecursively()
            EphemeralMediaStore.clearForTest()
        }
    }

    @Test(timeout = 10_000)
    fun `image only turn recovers as image placeholders without exposing private paths`() = runBlocking {
        EphemeralMediaStore.clearForTest()
        val sandbox = Files.createTempDirectory("local-gateway-image-fallback").toFile()
        val fixture = AppServerFixture()
        val server = MockWebServer()
        server.enqueue(MockResponse().withWebSocketUpgrade(fixture))
        val client = LocalAppServerClient(
            bearerToken = TEST_TOKEN,
            httpClient = OkHttpClient.Builder().pingInterval(1, TimeUnit.SECONDS).build(),
            endpoint = server.url("/").toString(),
        )
        val store = LocalTurnImageStore(
            rootDirectory = sandbox.resolve("private-images"),
            codexRootPath = "/root/.cheby/turn-inputs",
            runtimeReady = { true },
        )
        val gateway = LocalCodexGateway(TEST_TOKEN, client, store)
        try {
            withTimeout(5_000) {
                gateway.connectionState.first {
                    it == com.cheby.codex.mobile.model.GatewayConnectionState.ONLINE
                }
            }
            val thread = gateway.createThread("图片回退")
            val sourceBytes = Base64.getDecoder().decode(SCREENSHOT_BASE64)
            val uploaded = List(5) {
                gateway.uploadTurnImage(
                    threadKey = thread.key,
                    clientMessageId = CLIENT_MESSAGE_ID,
                    clientAssetId = UUID.randomUUID().toString(),
                    bytes = sourceBytes,
                )
            }
            val terminal = async {
                withTimeout(5_000) {
                    gateway.observeConversation(thread.key, "", -1).first { event ->
                        event is ConversationEvent.TurnLifecycle &&
                            event.phase == TurnLifecyclePhase.COMPLETED
                    }
                }
            }

            gateway.sendTurnInput(
                threadKey = thread.key,
                input = buildList {
                    uploaded.forEach { add(TurnInput.Image(it.assetRef)) }
                },
                idempotencyKey = CLIENT_MESSAGE_ID,
            )
            terminal.await()

            val params = requireNotNull(fixture.lastTurnParams.get())
            val directInput = requireNotNull(fixture.lastTurnInput.get())
            assertFalse(params.containsKey("additionalContext"))
            assertEquals(5, directInput.size)
            assertTrue(directInput.all { it.jsonObject["type"]?.jsonPrimitive?.content == "localImage" })
            assertTrue(directInput.all { it.jsonObject["detail"]?.jsonPrimitive?.content == "auto" })
            assertEquals(1, fixture.threadStartParams.size)
            assertEquals(1, fixture.turnStartCalls.get())

            gateway.close()
            server.enqueue(MockResponse().withWebSocketUpgrade(fixture))
            val restored = LocalCodexGateway(
                TEST_TOKEN,
                LocalAppServerClient(bearerToken = TEST_TOKEN, endpoint = server.url("/").toString()),
                store,
            )
            try {
                val snapshot = restored.loadConversation(thread.key)
                val user = snapshot.messages.first { it.role == MessageRole.USER }
                assertEquals("[图片 × 5]", user.blocks.values.single().fallbackText)
                assertFalse(user.blocks.values.any { it.fallbackText.contains("/root/") })
            } finally {
                restored.close()
            }
            sourceBytes.fill(0)
        } finally {
            gateway.close()
            server.shutdown()
            sandbox.deleteRecursively()
            EphemeralMediaStore.clearForTest()
        }
    }

    private class AppServerFixture(
        private val includeScreenshot: Boolean = false,
        private val stallMainTurn: Boolean = false,
        private val listCreatedThread: Boolean = false,
        private val stallThreadList: Boolean = false,
        private val historyAgentText: String = "手机在线",
        private val includeNativeItems: Boolean = false,
        private val includeBlankReasoning: Boolean = false,
        private val restoredActive: Boolean = false,
        private val stallTurnRequest: Boolean = false,
        private val runtimeErrors: Int = 0,
        private val terminalStatus: String = "completed",
    ) : WebSocketListener() {
        private val json = Json { ignoreUnknownKeys = true }
        val lastClientMessageId = AtomicReference<String?>()
        val lastTurnInput = AtomicReference<JsonArray?>()
        val lastTurnParams = AtomicReference<JsonObject?>()
        val lastInterruptParams = AtomicReference<JsonObject?>()
        val interruptCalls = AtomicInteger()
        val lastInitializeParams = AtomicReference<JsonObject?>()
        val lastThreadListParams = AtomicReference<JsonObject?>()
        val threadStartParams = CopyOnWriteArrayList<JsonObject>()
        val turnStartCalls = AtomicInteger()
        val accountMethods = CopyOnWriteArrayList<String>()
        private val threadListCalls = AtomicInteger()

        override fun onOpen(webSocket: WebSocket, response: Response) = Unit

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            webSocket.close(code, reason)
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            val request = json.parseToJsonElement(text).jsonObject
            val method = request["method"]?.jsonPrimitive?.content ?: return
            val id = request["id"]?.jsonPrimitive?.long
            when (method) {
                "initialized" -> Unit
                "initialize" -> {
                    lastInitializeParams.set(request["params"]?.jsonObject)
                    respond(webSocket, requireNotNull(id), buildJsonObject {})
                }
                "account/read" -> {
                    accountMethods += method
                    respond(webSocket, requireNotNull(id), buildJsonObject {
                        put("account", buildJsonObject {
                            put("type", "chatgpt")
                            put("planType", "plus")
                        })
                        put("requiresOpenaiAuth", false)
                    })
                }
                "account/login/start" -> {
                    accountMethods += method
                    respond(webSocket, requireNotNull(id), buildJsonObject {
                        put("type", "chatgptDeviceCode")
                        put("loginId", "0198f000-0000-7000-8000-0000000000aa")
                        put("verificationUrl", "https://auth.openai.com/codex/device")
                        put("userCode", "ABCD-1234")
                    })
                }
                "account/login/cancel", "account/logout" -> {
                    accountMethods += method
                    respond(webSocket, requireNotNull(id), buildJsonObject {})
                }
                "thread/list" -> {
                    lastThreadListParams.set(request["params"]?.jsonObject)
                    if (stallThreadList) return
                    val call = threadListCalls.incrementAndGet()
                    respond(
                        webSocket,
                        requireNotNull(id),
                        buildJsonObject {
                            put("data", buildJsonArray {
                                if (listCreatedThread || (stallMainTurn && turnStartCalls.get() > 0)) {
                                    add(threadObject(THREAD_ID))
                                } else if (call >= 2) {
                                    add(threadObject(EXISTING_THREAD_ID))
                                }
                            })
                            put("nextCursor", kotlinx.serialization.json.JsonNull)
                        },
                    )
                }
                "thread/start" -> {
                    threadStartParams += requireNotNull(request["params"]?.jsonObject)
                    respond(
                        webSocket,
                        requireNotNull(id),
                        buildJsonObject {
                            put("thread", threadObject(THREAD_ID))
                            put("model", "gpt-5.6-sol")
                            put("reasoningEffort", "high")
                        },
                    )
                }
                "thread/resume" -> {
                    respond(webSocket, requireNotNull(id), buildJsonObject {
                        put("thread", threadObject(THREAD_ID))
                        put("model", "gpt-5.6-sol")
                        put("reasoningEffort", "high")
                    })
                }
                "model/list" -> {
                    respond(webSocket, requireNotNull(id), buildJsonObject {
                        put("data", buildJsonArray {
                            add(buildJsonObject {
                                put("id", "gpt-5.6-sol")
                                put("model", "gpt-5.6-sol")
                                put("displayName", "GPT-5.6 Sol")
                                put("description", "Fast coding model")
                                put("isDefault", true)
                                put("defaultReasoningEffort", "high")
                                put("supportedReasoningEfforts", buildJsonArray {
                                    listOf("none", "low", "medium", "high", "xhigh", "max").forEach { effort ->
                                        add(buildJsonObject {
                                            put("reasoningEffort", effort)
                                            put("description", effort)
                                        })
                                    }
                                })
                            })
                        })
                        put("nextCursor", kotlinx.serialization.json.JsonNull)
                    })
                }
                "thread/name/set" -> {
                    respond(webSocket, requireNotNull(id), buildJsonObject {})
                    notify(webSocket, "thread/name/updated", buildJsonObject {
                        put("threadId", THREAD_ID)
                        put("name", "手机单机验证")
                    })
                }
                "turn/interrupt" -> {
                    lastInterruptParams.set(request["params"]?.jsonObject)
                    interruptCalls.incrementAndGet()
                    respond(webSocket, requireNotNull(id), buildJsonObject {})
                }
                "turn/start" -> {
                    val params = requireNotNull(request["params"]?.jsonObject)
                    turnStartCalls.incrementAndGet()
                    if (stallTurnRequest) return
                    lastTurnParams.set(params)
                    lastClientMessageId.set(params["clientUserMessageId"]?.jsonPrimitive?.content)
                    lastTurnInput.set(params["input"] as? JsonArray)
                    respond(
                        webSocket,
                        requireNotNull(id),
                        buildJsonObject {
                            put("turn", buildJsonObject {
                                put("id", TURN_ID)
                                put("status", "inProgress")
                                put("items", buildJsonArray {})
                            })
                        },
                    )
                    notify(webSocket, "turn/started", buildJsonObject {
                        put("threadId", THREAD_ID)
                        put("turn", buildJsonObject { put("id", TURN_ID) })
                    })
                    notify(webSocket, "item/started", itemParams(userItem(), "startedAtMs"))
                    notify(webSocket, "item/started", itemParams(agentItem(""), "startedAtMs"))
                    if (stallMainTurn) return
                    repeat(runtimeErrors) {
                        notify(webSocket, "error", buildJsonObject {
                            put("threadId", THREAD_ID)
                            put("turnId", TURN_ID)
                        })
                    }
                    notify(webSocket, "item/agentMessage/delta", buildJsonObject {
                        put("threadId", THREAD_ID)
                        put("turnId", TURN_ID)
                        put("itemId", AGENT_ITEM_ID)
                        put("delta", "手机在线")
                    })
                    notify(webSocket, "item/completed", itemParams(agentItem("手机在线"), "completedAtMs"))
                    notify(webSocket, "turn/completed", buildJsonObject {
                        put("threadId", THREAD_ID)
                        put("turn", buildJsonObject {
                            put("id", TURN_ID)
                            put("status", terminalStatus)
                        })
                    })
                }
                "thread/read" -> respond(
                    webSocket,
                    requireNotNull(id),
                    buildJsonObject {
                        put("thread", buildJsonObject {
                            put("id", THREAD_ID)
                            put("createdAt", 1_700_000_000L)
                            put("status", buildJsonObject { put("type", if (restoredActive) "active" else "idle") })
                            put("turns", buildJsonArray {
                                add(buildJsonObject {
                                    put("id", TURN_ID)
                                    put("startedAt", 1_700_000_001L)
                                    put("items", buildJsonArray {
                                        add(userItem())
                                        if (includeScreenshot) add(screenshotItem())
                                        if (includeNativeItems) {
                                            add(buildJsonObject {
                                                put("id", "reasoning-item-1")
                                                put("type", "reasoning")
                                                put("summary", buildJsonArray {
                                                    add(kotlinx.serialization.json.JsonPrimitive("已检查环境并准备执行。"))
                                                })
                                            })
                                            add(buildJsonObject {
                                                put("id", "future-item-1")
                                                put("type", "futureItem")
                                            })
                                        }
                                        if (includeBlankReasoning) {
                                            add(buildJsonObject {
                                                put("id", "reasoning-item-empty")
                                                put("type", "reasoning")
                                                put("summary", buildJsonArray {})
                                            })
                                        }
                                        add(agentItem(historyAgentText))
                                    })
                                })
                            })
                        })
                    },
                )
            }
        }

        private fun threadObject(id: String = THREAD_ID) = buildJsonObject {
            put("id", id)
            put("preview", "")
            put("createdAt", 1_700_000_000L)
            put("updatedAt", 1_700_000_000L)
            val running = (restoredActive || (stallMainTurn && turnStartCalls.get() > 0)) && id == THREAD_ID
            put("status", buildJsonObject { put("type", if (running) "active" else "idle") })
            if (running) put("turns", buildJsonArray {
                add(buildJsonObject { put("id", TURN_ID); put("status", "inProgress") })
            })
        }

        private fun userItem() = buildJsonObject {
            put("id", USER_ITEM_ID)
            put("type", "userMessage")
            put("clientId", CLIENT_MESSAGE_ID)
            put("content", lastTurnInput.get() ?: buildJsonArray {
                add(buildJsonObject {
                    put("type", "text")
                    put("text", "调用手机状态工具")
                })
            })
        }

        private fun agentItem(text: String) = buildJsonObject {
            put("id", AGENT_ITEM_ID)
            put("type", "agentMessage")
            put("text", text)
            put("phase", "final_answer")
        }

        private fun screenshotItem() = buildJsonObject {
            put("id", SCREENSHOT_ITEM_ID)
            put("type", "mcpToolCall")
            put("server", "phonebridge")
            put("tool", "android_capture_screenshot")
            put("arguments", buildJsonObject {})
            put("status", "completed")
            put("result", buildJsonObject {
                put("content", buildJsonArray {
                    add(buildJsonObject {
                        put("type", "text")
                        put("text", "Android screenshot saved to /root/private/screenshot.png.")
                    })
                    add(buildJsonObject {
                        put("type", "image")
                        put("data", SCREENSHOT_BASE64)
                        put("mimeType", "image/png")
                    })
                })
            })
        }

        private fun itemParams(item: JsonObject, timestampKey: String) = buildJsonObject {
            put("threadId", THREAD_ID)
            put("turnId", TURN_ID)
            put(timestampKey, 1_700_000_001_000L)
            put("item", item)
        }

        private fun respond(webSocket: WebSocket, id: Long, result: JsonObject) {
            webSocket.send(buildJsonObject {
                put("id", id)
                put("result", result)
            }.toString())
        }

        private fun notify(webSocket: WebSocket, method: String, params: JsonObject) {
            webSocket.send(buildJsonObject {
                put("method", method)
                put("params", params)
            }.toString())
        }
    }

    private companion object {
        const val TEST_TOKEN = "test-capability-token-0123456789-abcdefghijklmnopqrstuvwxyz"
        const val THREAD_ID = "0198f000-0000-7000-8000-000000000001"
        const val EXISTING_THREAD_ID = "0198f000-0000-7000-8000-000000000099"
        const val TURN_ID = "0198f000-0000-7000-8000-000000000002"
        const val USER_ITEM_ID = "item-user-1"
        const val AGENT_ITEM_ID = "item-agent-1"
        const val SCREENSHOT_ITEM_ID = "item-screenshot-1"
        const val CLIENT_MESSAGE_ID = "client-message-1"
        const val SCREENSHOT_BASE64 =
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    }
}
