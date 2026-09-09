package com.cheby.codex.mobile.gateway

import android.util.Log
import com.cheby.codex.mobile.model.GatewayConnectionState
import java.io.IOException
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener

/** A bounded, reconnecting JSON-RPC client for the loopback-only Codex app-server. */
internal class LocalAppServerClient(
    private val bearerToken: String,
    httpClient: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .pingInterval(20, TimeUnit.SECONDS)
        .build(),
    private val endpoint: String = LOCAL_APP_SERVER_URL,
    private val beforeConnectAttempt: () -> Unit = {},
) {
    // Codex app-server intentionally uses an uncompressed WebSocket stream. Some Android
    // OkHttp builds advertise permessage-deflate and then fail after the server accepts auth,
    // while the same uncompressed handshake succeeds. Strip only that optional negotiation.
    private val websocketClient = httpClient.newBuilder()
        .addInterceptor { chain ->
            chain.proceed(
                chain.request().newBuilder()
                    .removeHeader("Sec-WebSocket-Extensions")
                    .build(),
            )
        }
        .build()
    private val json = Json { ignoreUnknownKeys = true }
    private val closed = AtomicBoolean(false)
    private val requestIds = AtomicLong(0)
    private val socket = AtomicReference<AppServerSocket?>()
    private val pending = ConcurrentHashMap<Long, CompletableDeferred<JsonObject>>()
    private val ready = AtomicReference(CompletableDeferred<Unit>())
    private val _connectionState = MutableStateFlow(GatewayConnectionState.CONNECTING)
    val connectionState: StateFlow<GatewayConnectionState> = _connectionState.asStateFlow()

    init {
        require(bearerToken.length in 32..256 && bearerToken.all { it.code in 0x21..0x7e })
    }

    fun start(
        scope: CoroutineScope,
        onNotification: (String, JsonObject) -> Unit,
        onReady: () -> Unit,
    ) {
        scope.launch {
            var retryMillis = INITIAL_RETRY_MILLIS
            while (isActive && !closed.get()) {
                _connectionState.value = GatewayConnectionState.CONNECTING
                // The appliance and UI start independently after an APK update or process death.
                // Reassert the desired runtime token before each loopback attempt so a server
                // restored with an older verifier cannot strand the UI offline.
                runCatching { beforeConnectAttempt() }
                val cycle = ConnectionCycle()
                var readerJob: Job? = null
                val candidate = if (isRawLoopbackEndpoint(endpoint)) {
                    val connection = LocalLoopbackWebSocket(
                        endpoint = endpoint,
                        bearerToken = bearerToken,
                        maxFrameBytes = MAX_INBOUND_CHARS,
                    )
                    LoopbackAppServerSocket(connection).also { localSocket ->
                        try {
                            connection.connect()
                            cycle.opened.complete(Unit)
                            readerJob = scope.launch {
                                readLoopback(connection, localSocket, cycle, onNotification)
                            }
                        } catch (_: Exception) {
                            cycle.opened.completeExceptionally(
                                IOException("Local Codex connection failed"),
                            )
                            cycle.terminal.complete(Unit)
                        }
                    }
                } else {
                    OkHttpAppServerSocket().also { okHttpSocket ->
                        val request = Request.Builder()
                            .url(endpoint)
                            .header("Authorization", "Bearer $bearerToken")
                            .build()
                        val listener = listenerFor(cycle, okHttpSocket, onNotification)
                        okHttpSocket.attach(websocketClient.newWebSocket(request, listener))
                    }
                }
                socket.set(candidate)
                try {
                    withTimeout(CONNECT_TIMEOUT_MILLIS) { cycle.opened.await() }
                    val initialize = buildJsonObject {
                        put("clientInfo", buildJsonObject {
                            put("name", "chebyagent_android")
                            put("title", "ChebyAgent Android")
                            put("version", "0.1.0")
                        })
                        put("capabilities", buildJsonObject {
                            // additionalContext is an experimental app-server field. Opt in at
                            // initialization so the server can validate later turn/start calls.
                            put("experimentalApi", true)
                        })
                    }
                    requestOn(candidate, "initialize", initialize)
                    sendNotificationOn(candidate, "initialized", buildJsonObject {})
                    ready.get().complete(Unit)
                    _connectionState.value = GatewayConnectionState.ONLINE
                    retryMillis = INITIAL_RETRY_MILLIS
                    onReady()
                    cycle.terminal.await()
                } catch (error: Exception) {
                    // Public state is enough for the UI. Raw protocol failures are never rendered.
                    runCatching {
                        Log.i(
                            CONNECTION_LOG_TAG,
                            "connection_cycle_failed type=${error.javaClass.simpleName}",
                        )
                    }
                } finally {
                    readerJob?.cancel()
                    socket.compareAndSet(candidate, null)
                    candidate.cancel()
                    failPending()
                    ready.getAndSet(CompletableDeferred()).cancel()
                }
                if (!isActive || closed.get()) break
                _connectionState.value = GatewayConnectionState.OFFLINE
                delay(retryMillis)
                retryMillis = (retryMillis * 2).coerceAtMost(MAX_RETRY_MILLIS)
            }
            _connectionState.value = GatewayConnectionState.OFFLINE
        }
    }

    suspend fun request(method: String, params: JsonObject = buildJsonObject {}): JsonObject {
        try {
            withTimeout(REQUEST_TIMEOUT_MILLIS) { ready.get().await() }
            val activeSocket = socket.get() ?: throw GatewayTransportException("Local Codex is unavailable")
            return requestOn(activeSocket, method, params)
        } catch (_: TimeoutCancellationException) {
            // Timeout is an unknown receipt, never a reason to replay a task automatically.
            throw GatewayTransportException("Local Codex response timed out")
        }
    }

    /** Explicit configuration/account refresh; silence alone never calls this. */
    fun requestReconnect() {
        socket.get()?.cancel()
    }

    fun close() {
        if (!closed.compareAndSet(false, true)) return
        socket.getAndSet(null)?.close(1000, "closed")
        failPending()
        ready.get().cancel()
        _connectionState.value = GatewayConnectionState.OFFLINE
        websocketClient.dispatcher.cancelAll()
    }

    private suspend fun requestOn(
        activeSocket: AppServerSocket,
        method: String,
        params: JsonObject,
    ): JsonObject {
        require(method.matches(METHOD_PATTERN))
        val requestId = requestIds.incrementAndGet()
        val response = CompletableDeferred<JsonObject>()
        pending[requestId] = response
        val body = buildJsonObject {
            put("id", requestId)
            put("method", method)
            put("params", params)
        }.toString()
        if (
            body.length > MAX_OUTBOUND_CHARS ||
            !withContext(Dispatchers.IO) { activeSocket.send(body) }
        ) {
            pending.remove(requestId)
            throw GatewayTransportException("Local Codex is unavailable")
        }
        val envelope = try {
            withTimeout(REQUEST_TIMEOUT_MILLIS) { response.await() }
        } finally {
            pending.remove(requestId)
        }
        envelope["error"]?.takeUnless { it is JsonNull }?.let { error ->
            val errorObject = runCatching { error.jsonObject }.getOrNull()
            val numericCode = (errorObject?.get("code") as? JsonPrimitive)?.content.orEmpty()
            val safeMessage = (errorObject?.get("message") as? JsonPrimitive)?.content
                ?.replace(UUID_PATTERN, "[id]")
                ?.replace(ABSOLUTE_PATH_PATTERN, "[path]")
                ?.replace(LONG_SECRET_PATTERN, "[redacted]")
                ?.take(MAX_SAFE_ERROR_CHARS)
                .orEmpty()
            runCatching {
                Log.i(
                    RPC_LOG_TAG,
                    "request_rejected method=$method code=$numericCode message=$safeMessage",
                )
            }
            val busy = method == "turn/start" && (
                safeMessage.contains("active turn", ignoreCase = true) ||
                safeMessage.contains("already running", ignoreCase = true) ||
                safeMessage.contains("turn in progress", ignoreCase = true))
            throw GatewayRequestException(if (busy) "THREAD_BUSY" else "LOCAL_REQUEST_REJECTED", retryable = false)
        }
        return envelope["result"]?.let { result ->
            runCatching { result.jsonObject }.getOrNull()
        } ?: throw GatewayTransportException("Local Codex protocol mismatch")
    }

    private fun sendNotificationOn(activeSocket: AppServerSocket, method: String, params: JsonObject) {
        val body = buildJsonObject {
            put("method", method)
            put("params", params)
        }.toString()
        if (body.length > MAX_OUTBOUND_CHARS || !activeSocket.send(body)) {
            throw GatewayTransportException("Local Codex is unavailable")
        }
    }

    private fun listenerFor(
        cycle: ConnectionCycle,
        activeSocket: AppServerSocket,
        onNotification: (String, JsonObject) -> Unit,
    ) = object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            cycle.opened.complete(Unit)
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            handleIncoming(activeSocket, text, cycle, onNotification)
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            runCatching {
                Log.i(CONNECTION_LOG_TAG, "socket_closing code=$code")
            }
            webSocket.close(code, "")
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            cycle.terminal.complete(Unit)
        }

        override fun onFailure(webSocket: WebSocket, error: Throwable, response: Response?) {
            cycle.opened.completeExceptionally(IOException("Local Codex connection failed"))
            cycle.terminal.complete(Unit)
        }
    }

    private fun readLoopback(
        connection: LocalLoopbackWebSocket,
        activeSocket: AppServerSocket,
        cycle: ConnectionCycle,
        onNotification: (String, JsonObject) -> Unit,
    ) {
        try {
            while (!closed.get()) {
                when (val event = connection.readEvent()) {
                    is BoundedSocketEvent.Text -> {
                        handleIncoming(activeSocket, event.value, cycle, onNotification)
                    }
                    is BoundedSocketEvent.Closed -> {
                        runCatching {
                            Log.i(CONNECTION_LOG_TAG, "socket_closing code=${event.code}")
                        }
                        cycle.terminal.complete(Unit)
                        return
                    }
                }
            }
        } catch (error: Exception) {
            runCatching {
                Log.i(CONNECTION_LOG_TAG, "loopback_failure type=${error.javaClass.simpleName}")
            }
            cycle.opened.completeExceptionally(IOException("Local Codex connection failed"))
            cycle.terminal.complete(Unit)
        }
    }

    private fun handleIncoming(
        activeSocket: AppServerSocket,
        text: String,
        cycle: ConnectionCycle,
        onNotification: (String, JsonObject) -> Unit,
    ) {
        if (text.length > MAX_INBOUND_CHARS) {
            activeSocket.cancel()
            cycle.terminal.complete(Unit)
            return
        }
        val envelope = runCatching { json.parseToJsonElement(text).jsonObject }.getOrNull()
            ?: return
        val method = (envelope["method"] as? JsonPrimitive)?.content
        val id = (envelope["id"] as? JsonPrimitive)?.longOrNull
        if (method != null) {
            val params = envelope["params"]?.let {
                runCatching { it.jsonObject }.getOrNull()
            } ?: buildJsonObject {}
            if (id == null) {
                runCatching { onNotification(method, params) }
            } else {
                rejectUnsupportedServerRequest(activeSocket, id)
            }
            return
        }
        if (id != null) pending.remove(id)?.complete(envelope)
    }

    private fun rejectUnsupportedServerRequest(activeSocket: AppServerSocket, id: Long) {
        activeSocket.send(
            buildJsonObject {
                put("id", id)
                put("error", buildJsonObject {
                    put("code", -32601)
                    put("message", "Unsupported by this client")
                })
            }.toString(),
        )
    }

    private fun failPending() {
        val failure = GatewayTransportException("Local Codex is unavailable")
        pending.values.forEach { it.completeExceptionally(failure) }
        pending.clear()
    }

    private class ConnectionCycle {
        val opened = CompletableDeferred<Unit>()
        val terminal = CompletableDeferred<Unit>()
    }

    private companion object {
        const val LOCAL_APP_SERVER_URL = "ws://127.0.0.1:4500"
        const val CONNECT_TIMEOUT_MILLIS = 10_000L
        const val REQUEST_TIMEOUT_MILLIS = 30_000L
        const val INITIAL_RETRY_MILLIS = 750L
        const val MAX_RETRY_MILLIS = 15_000L
        // thread/read includes historical tool payloads, including inline screenshot evidence.
        // Real phone-agent sessions can exceed 50 MiB before the reducer projects the bounded
        // message window. Keep a hard transport bound, but do not reconnect-loop on those valid
        // official snapshots.
        const val MAX_INBOUND_CHARS = 64 * 1024 * 1024
        const val MAX_OUTBOUND_CHARS = 2 * 1024 * 1024
        const val MAX_SAFE_ERROR_CHARS = 240
        const val RPC_LOG_TAG = "ChebyLocalRpc"
        const val CONNECTION_LOG_TAG = "ChebyLocalConnection"
        fun isRawLoopbackEndpoint(value: String): Boolean =
            value.startsWith("ws://127.0.0.1:") || value.startsWith("ws://localhost:")
        val METHOD_PATTERN = Regex("[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")
        val UUID_PATTERN = Regex("(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
        val ABSOLUTE_PATH_PATTERN = Regex("(?:/[A-Za-z0-9._-]+){2,}")
        val LONG_SECRET_PATTERN = Regex("[A-Za-z0-9_-]{32,}")
    }
}

private interface AppServerSocket {
    fun send(text: String): Boolean
    fun close(code: Int, reason: String): Boolean
    fun cancel()
}

private class OkHttpAppServerSocket : AppServerSocket {
    private val delegate = AtomicReference<WebSocket?>()

    fun attach(webSocket: WebSocket) {
        check(delegate.compareAndSet(null, webSocket))
    }

    override fun send(text: String): Boolean = delegate.get()?.send(text) == true

    override fun close(code: Int, reason: String): Boolean =
        delegate.get()?.close(code, reason) == true

    override fun cancel() {
        delegate.getAndSet(null)?.cancel()
    }
}

private class LoopbackAppServerSocket(
    private val delegate: LocalLoopbackWebSocket,
) : AppServerSocket {
    override fun send(text: String): Boolean = delegate.sendText(text)
    override fun close(code: Int, reason: String): Boolean {
        // close() may be called from the UI lifecycle; closing the TCP socket is non-blocking and
        // avoids a StrictMode network write from the main thread.
        delegate.cancel()
        return true
    }
    override fun cancel() = delegate.cancel()
}
