package com.cheby.codex.mobile.gateway

import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.net.InetSocketAddress
import java.net.Socket
import java.net.URI
import java.nio.ByteBuffer
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import java.util.Locale
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

/** Minimal uncompressed WebSocket transport restricted to a loopback app-server. */
internal class LocalLoopbackWebSocket(
    endpoint: String,
    private val bearerToken: String,
    private val maxFrameBytes: Int,
    private val connectTimeoutMillis: Int = 5_000,
    private val handshakeTimeoutMillis: Int = 10_000,
    private val secureRandom: SecureRandom = SecureRandom(),
) {
    private val target = URI(endpoint)
    private val socket = AtomicReference<Socket?>()
    private val cancelled = AtomicBoolean(false)
    private val closeSent = AtomicBoolean(false)
    private val writeLock = ReentrantLock()
    private var input: BufferedInputStream? = null
    private var output: BufferedOutputStream? = null
    private var reader: BoundedWebSocketFrameReader? = null

    init {
        require(target.scheme == "ws")
        require(target.host == "127.0.0.1" || target.host.equals("localhost", ignoreCase = true))
        require(bearerToken.length in 32..256 && bearerToken.all { it.code in 0x21..0x7e })
    }

    fun connect() {
        if (cancelled.get()) throw IOException("Connection cancelled")
        val port = if (target.port >= 0) target.port else 80
        val plain = Socket()
        if (!socket.compareAndSet(null, plain)) throw IOException("Connection already opened")
        try {
            plain.tcpNoDelay = true
            plain.soTimeout = handshakeTimeoutMillis
            plain.connect(InetSocketAddress(target.host, port), connectTimeoutMillis)
            val bufferedInput = BufferedInputStream(plain.inputStream, IO_BUFFER_BYTES)
            val bufferedOutput = BufferedOutputStream(plain.outputStream, IO_BUFFER_BYTES)
            performHandshake(port, bufferedInput, bufferedOutput)
            if (cancelled.get()) throw IOException("Connection cancelled")
            plain.soTimeout = 0
            input = bufferedInput
            output = bufferedOutput
            reader = BoundedWebSocketFrameReader(bufferedInput, maxFrameBytes)
        } catch (error: Exception) {
            cancel()
            throw error
        }
    }

    fun readEvent(): BoundedSocketEvent {
        val frameReader = reader ?: throw IOException("Connection is not open")
        while (!cancelled.get()) {
            when (val frame = frameReader.readNext()) {
                is RawSocketFrame.Text -> return BoundedSocketEvent.Text(frame.value)
                is RawSocketFrame.Ping -> if (!writeFrame(OPCODE_PONG, frame.payload)) {
                    throw IOException("Pong failed")
                }
                is RawSocketFrame.Pong -> Unit
                is RawSocketFrame.Close -> return BoundedSocketEvent.Closed(frame.code, frame.reason)
            }
        }
        throw IOException("Connection cancelled")
    }

    fun sendText(value: String): Boolean {
        val payload = value.toByteArray(Charsets.UTF_8)
        return payload.size <= maxFrameBytes && writeFrame(OPCODE_TEXT, payload)
    }

    fun close(code: Int, reason: String = ""): Boolean {
        if (!closeSent.compareAndSet(false, true)) return true
        val reasonBytes = reason.toByteArray(Charsets.UTF_8).takeIf { it.size <= 123 } ?: ByteArray(0)
        val payload = ByteBuffer.allocate(2 + reasonBytes.size)
            .putShort(code.toShort())
            .put(reasonBytes)
            .array()
        val sent = writeFrame(OPCODE_CLOSE, payload)
        cancel()
        return sent
    }

    fun cancel() {
        cancelled.set(true)
        runCatching { socket.getAndSet(null)?.close() }
    }

    private fun performHandshake(
        port: Int,
        source: BufferedInputStream,
        sink: BufferedOutputStream,
    ) {
        val nonce = ByteArray(16).also(secureRandom::nextBytes)
        val key = Base64.getEncoder().encodeToString(nonce)
        val path = buildString {
            append(target.rawPath?.takeIf(String::isNotEmpty) ?: "/")
            target.rawQuery?.let { append('?').append(it) }
        }
        val host = if (port == 80) target.host else "${target.host}:$port"
        val request = buildString {
            append("GET ").append(path).append(" HTTP/1.1\r\n")
            append("Host: ").append(host).append("\r\n")
            append("Upgrade: websocket\r\n")
            append("Connection: Upgrade\r\n")
            append("Sec-WebSocket-Key: ").append(key).append("\r\n")
            append("Sec-WebSocket-Version: 13\r\n")
            append("Authorization: Bearer ").append(bearerToken).append("\r\n")
            append("\r\n")
        }
        sink.write(request.toByteArray(Charsets.US_ASCII))
        sink.flush()

        val response = readHttpHeader(source)
        val lines = response.split("\r\n")
        val status = lines.firstOrNull()?.split(' ')?.getOrNull(1)?.toIntOrNull()
            ?: throw InvalidWebSocketFrameException()
        if (status != 101) throw WebSocketHandshakeException(status)
        val headers = LinkedHashMap<String, MutableList<String>>()
        lines.drop(1).filter(String::isNotEmpty).forEach { line ->
            val separator = line.indexOf(':')
            if (separator <= 0) throw InvalidWebSocketFrameException()
            val name = line.substring(0, separator).trim().lowercase(Locale.US)
            headers.getOrPut(name, ::mutableListOf).add(line.substring(separator + 1).trim())
        }
        fun tokens(name: String): Set<String> = headers[name].orEmpty()
            .flatMap { it.split(',') }
            .mapTo(mutableSetOf()) { it.trim().lowercase(Locale.US) }
        if ("websocket" !in tokens("upgrade") || "upgrade" !in tokens("connection")) {
            throw InvalidWebSocketFrameException()
        }
        val expectedAccept = Base64.getEncoder().encodeToString(
            MessageDigest.getInstance("SHA-1")
                .digest((key + WEB_SOCKET_GUID).toByteArray(Charsets.US_ASCII)),
        )
        if (headers["sec-websocket-accept"]?.singleOrNull() != expectedAccept) {
            throw InvalidWebSocketFrameException()
        }
        if (!headers["sec-websocket-extensions"].isNullOrEmpty()) {
            throw InvalidWebSocketFrameException()
        }
    }

    private fun readHttpHeader(source: BufferedInputStream): String {
        val bytes = ByteArrayOutputStream()
        var matched = 0
        while (bytes.size() < MAX_HANDSHAKE_HEADER_BYTES) {
            val value = source.read()
            if (value < 0) throw IOException("Incomplete WebSocket handshake")
            bytes.write(value)
            matched = when {
                matched == 0 && value == '\r'.code -> 1
                matched == 1 && value == '\n'.code -> 2
                matched == 2 && value == '\r'.code -> 3
                matched == 3 && value == '\n'.code -> {
                    return bytes.toString(Charsets.ISO_8859_1.name())
                }
                value == '\r'.code -> 1
                else -> 0
            }
        }
        throw IOException("WebSocket handshake header exceeds limit")
    }

    private fun writeFrame(opcode: Int, payload: ByteArray): Boolean = writeLock.withLock {
        if (cancelled.get() || payload.size > maxFrameBytes) return@withLock false
        val sink = output ?: return@withLock false
        try {
            val mask = ByteArray(4).also(secureRandom::nextBytes)
            sink.write(FIN_BIT or opcode)
            when {
                payload.size <= 125 -> sink.write(MASK_BIT or payload.size)
                payload.size <= 0xffff -> {
                    sink.write(MASK_BIT or 126)
                    sink.write(payload.size ushr 8)
                    sink.write(payload.size)
                }
                else -> {
                    sink.write(MASK_BIT or 127)
                    repeat(4) { sink.write(0) }
                    sink.write(payload.size ushr 24)
                    sink.write(payload.size ushr 16)
                    sink.write(payload.size ushr 8)
                    sink.write(payload.size)
                }
            }
            sink.write(mask)
            payload.forEachIndexed { index, byte ->
                sink.write(byte.toInt() xor mask[index % 4].toInt())
            }
            sink.flush()
            true
        } catch (_: IOException) {
            false
        }
    }

    private companion object {
        const val IO_BUFFER_BYTES = 8 * 1024
        const val MAX_HANDSHAKE_HEADER_BYTES = 32 * 1024
        const val FIN_BIT = 0x80
        const val MASK_BIT = 0x80
        const val OPCODE_TEXT = 0x1
        const val OPCODE_CLOSE = 0x8
        const val OPCODE_PONG = 0xA
        const val WEB_SOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    }
}
