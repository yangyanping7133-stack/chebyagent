package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.model.SafeDisplayText
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Proxy
import java.net.ProxySelector
import java.net.Socket
import java.net.UnknownHostException
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import java.util.Locale
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference
import java.util.concurrent.locks.ReentrantLock
import javax.net.ssl.SSLSocket
import javax.net.ssl.SSLSocketFactory
import kotlin.concurrent.withLock
import okhttp3.HttpUrl

internal sealed interface BoundedSocketEvent {
    data class Text(val value: String) : BoundedSocketEvent
    data class Closed(val code: Int, val reason: String) : BoundedSocketEvent
}

internal class WebSocketHandshakeException(val statusCode: Int) : IOException()
internal class WebSocketFrameTooLargeException(val frameBytes: Long) : IOException()
internal class UnsupportedBinaryFrameException : IOException()
internal class InvalidWebSocketFrameException : IOException()
internal class WebSocketPongTimeoutException : IOException()
internal class HttpProxyConnectException(val statusCode: Int) : IOException()
internal class HttpProxyResponseTooLargeException : IOException()

internal class WebSocketPingChallenge internal constructor(internal val id: Long)

private data class PendingPing(val id: Long, val payload: ByteArray)

/**
 * Minimal WSS transport for the public Gateway event protocol.
 *
 * The payload length is checked from the frame header before allocating a payload buffer. We do
 * not advertise compression and reject every RSV bit, so an inflated payload can never bypass the
 * byte limit. Incoming events are consumed synchronously by a capacity-one Flow upstream.
 */
internal class BoundedWebSocketConnection(
    private val sslSocketFactory: SSLSocketFactory,
    private val maxFrameBytes: Int,
    private val connectTimeoutMillis: Int = 15_000,
    private val handshakeTimeoutMillis: Int = 15_000,
    private val readTimeoutMillis: Int = 45_000,
    private val secureRandom: SecureRandom = SecureRandom(),
    private val proxySelectorProvider: () -> ProxySelector? = { null },
    private val directAddressFactory: (String, Int) -> InetSocketAddress = ::InetSocketAddress,
) {
    private val socket = AtomicReference<Socket?>()
    private val cancelled = AtomicBoolean(false)
    private val closeSent = AtomicBoolean(false)
    private val writeLock = ReentrantLock()
    private val pingSequence = AtomicLong()
    private val pendingPing = AtomicReference<PendingPing?>()
    private val terminalReadFailure = AtomicReference<IOException?>()
    private var input: BufferedInputStream? = null
    private var output: BufferedOutputStream? = null
    private var reader: BoundedWebSocketFrameReader? = null

    fun connect(
        target: HttpUrl,
        bearerToken: String,
        deviceProofKey: DeviceProofKeyHandle,
    ) {
        if (cancelled.get()) throw IOException("Connection cancelled")
        if (bearerToken.isBlank() || bearerToken.any { it.code !in 0x21..0x7e }) {
            throw IOException("Invalid authorization header")
        }
        val requestTarget = DeviceProofV1.originForm(target)
        val proof = DeviceProofV1.sign(
            key = deviceProofKey,
            method = "GET",
            rawTarget = requestTarget,
            body = DeviceProofV1.emptyBody(),
            bearerToken = bearerToken,
        )
        val route = selectRoute(target)
        val connectAddress = when (route) {
            is SocketRoute.Direct -> directAddressFactory(route.host, route.port)
            is SocketRoute.HttpProxy -> route.socketAddress
        }
        val plain = Socket()
        if (!socket.compareAndSet(null, plain)) throw IOException("Connection already opened")
        try {
            plain.tcpNoDelay = true
            plain.soTimeout = handshakeTimeoutMillis
            plain.connect(connectAddress, connectTimeoutMillis)
            if (route is SocketRoute.HttpProxy) establishHttpTunnel(plain, target)
            if (cancelled.get()) throw IOException("Connection cancelled")
            val tls = sslSocketFactory.createSocket(plain, target.host, target.port, true) as SSLSocket
            if (!socket.compareAndSet(plain, tls)) {
                runCatching { tls.close() }
                throw IOException("Connection cancelled")
            }
            tls.sslParameters = tls.sslParameters.apply { endpointIdentificationAlgorithm = "HTTPS" }
            tls.soTimeout = handshakeTimeoutMillis
            tls.startHandshake()
            if (cancelled.get()) throw IOException("Connection cancelled")

            val bufferedInput = BufferedInputStream(tls.inputStream, IO_BUFFER_BYTES)
            val bufferedOutput = BufferedOutputStream(tls.outputStream, IO_BUFFER_BYTES)
            performHandshake(
                target = target,
                requestTarget = requestTarget,
                bearerToken = bearerToken,
                proof = proof,
                input = bufferedInput,
                output = bufferedOutput,
            )
            if (cancelled.get()) throw IOException("Connection cancelled")
            tls.soTimeout = readTimeoutMillis
            input = bufferedInput
            output = bufferedOutput
            reader = BoundedWebSocketFrameReader(bufferedInput, maxFrameBytes)
        } catch (error: Exception) {
            if (route is SocketRoute.HttpProxy && error is IOException) {
                runCatching { route.selector.connectFailed(target.toUri(), route.socketAddress, error) }
            }
            cancel()
            throw error
        }
    }

    fun readEvent(): BoundedSocketEvent {
        terminalReadFailure.get()?.let { throw it }
        val frameReader = reader ?: throw IOException("Connection is not open")
        while (!cancelled.get()) {
            val frame = try {
                frameReader.readNext()
            } catch (error: IOException) {
                throw terminalReadFailure.get() ?: error
            }
            when (frame) {
                is RawSocketFrame.Text -> return BoundedSocketEvent.Text(frame.value)
                is RawSocketFrame.Ping -> if (!writeControl(OPCODE_PONG, frame.payload)) {
                    throw IOException("Pong failed")
                }
                is RawSocketFrame.Pong -> {
                    val expected = pendingPing.get()
                    if (expected != null && expected.payload.contentEquals(frame.payload)) {
                        pendingPing.compareAndSet(expected, null)
                    }
                }
                is RawSocketFrame.Close -> return BoundedSocketEvent.Closed(frame.code, frame.reason)
            }
        }
        throw terminalReadFailure.get() ?: IOException("Connection cancelled")
    }

    fun sendText(value: String): Boolean {
        if (SafeDisplayText.exceedsUtf8Limit(value, maxFrameBytes)) return false
        return writeFrame(OPCODE_TEXT, value.toByteArray(Charsets.UTF_8))
    }

    fun preparePing(): WebSocketPingChallenge? {
        if (cancelled.get() || pendingPing.get() != null) return null
        val challenge = WebSocketPingChallenge(pingSequence.incrementAndGet())
        val value = PendingPing(challenge.id, ByteArray(PING_PAYLOAD_BYTES).also(secureRandom::nextBytes))
        if (!pendingPing.compareAndSet(null, value)) return null
        return challenge
    }

    fun sendPing(challenge: WebSocketPingChallenge): Boolean {
        val value = pendingPing.get() ?: return false
        if (value.id != challenge.id) return false
        return writeControl(OPCODE_PING, value.payload)
    }

    fun expirePong(challenge: WebSocketPingChallenge): Boolean {
        val value = pendingPing.get() ?: return false
        if (value.id != challenge.id || !pendingPing.compareAndSet(value, null)) return false
        terminalReadFailure.compareAndSet(null, WebSocketPongTimeoutException())
        cancel()
        return true
    }

    fun close(code: Int, reason: String = ""): Boolean = writeClose(code, reason)

    fun cancel() {
        cancelled.set(true)
        runCatching { socket.getAndSet(null)?.close() }
    }

    /** Called only after [cancel]; it never waits for another writer's lock. */
    fun bestEffortCloseFrameAfterCancellation(code: Int, reason: String = "") {
        if (!cancelled.get() || !closeSent.compareAndSet(false, true)) return
        if (!writeLock.tryLock()) return
        try {
            val sink = output ?: return
            runCatching { writeFrameLocked(sink, OPCODE_CLOSE, closePayload(code, reason)) }
        } finally {
            writeLock.unlock()
        }
    }

    private sealed interface SocketRoute {
        data class Direct(val host: String, val port: Int) : SocketRoute
        data class HttpProxy(
            val socketAddress: InetSocketAddress,
            val selector: ProxySelector,
        ) : SocketRoute
    }

    private fun selectRoute(target: HttpUrl): SocketRoute {
        if (!target.isHttps) throw IOException("WebSocket target must use TLS")
        val direct = SocketRoute.Direct(target.host, target.port)
        if (isLoopbackHost(target.host)) return direct
        val proxySelector = proxySelectorProvider() ?: return direct

        val proxies = proxySelector.select(target.toUri()).orEmpty()
        for (proxy in proxies) {
            when (proxy.type()) {
                Proxy.Type.DIRECT -> return direct
                Proxy.Type.HTTP -> {
                    val selectedAddress = proxy.address() as? InetSocketAddress
                        ?: throw IOException("Invalid HTTP proxy address")
                    if (selectedAddress.port !in 1..65535) {
                        throw IOException("Invalid HTTP proxy port")
                    }
                    val address = if (selectedAddress.isUnresolved) {
                        InetSocketAddress(selectedAddress.hostString, selectedAddress.port)
                    } else {
                        selectedAddress
                    }
                    if (address.isUnresolved) {
                        throw UnknownHostException("HTTP proxy is unavailable")
                    }
                    return SocketRoute.HttpProxy(address, proxySelector)
                }
                Proxy.Type.SOCKS -> Unit
            }
        }
        throw IOException("No supported proxy route")
    }

    private fun establishHttpTunnel(socket: Socket, target: HttpUrl) {
        val authority = if (':' in target.host) "[${target.host}]:${target.port}" else "${target.host}:${target.port}"
        val request = buildString {
            append("CONNECT ").append(authority).append(" HTTP/1.1\r\n")
            append("Host: ").append(authority).append("\r\n")
            append("Proxy-Connection: Keep-Alive\r\n")
            append("\r\n")
        }
        socket.getOutputStream().apply {
            write(request.toByteArray(Charsets.US_ASCII))
            flush()
        }

        val response = readHttpHeader(
            input = socket.getInputStream(),
            maxBytes = MAX_PROXY_RESPONSE_HEADER_BYTES,
            incompleteMessage = "Incomplete HTTP proxy response",
            tooLarge = { HttpProxyResponseTooLargeException() },
        )
        val lines = response.split("\r\n")
        val statusLine = lines.firstOrNull().orEmpty()
        val match = PROXY_STATUS_LINE.matchEntire(statusLine)
            ?: throw IOException("Invalid HTTP proxy response")
        validateProxyHeaders(lines.drop(1))
        val statusCode = match.groupValues[1].toInt()
        if (statusCode != HTTP_OK) throw HttpProxyConnectException(statusCode)
    }

    private fun validateProxyHeaders(lines: List<String>) {
        lines.filter(String::isNotEmpty).forEach { line ->
            if (line.firstOrNull()?.isWhitespace() == true) throw IOException("Invalid HTTP proxy header")
            val separator = line.indexOf(':')
            if (separator <= 0 || !HTTP_HEADER_NAME.matches(line.substring(0, separator))) {
                throw IOException("Invalid HTTP proxy header")
            }
            if (line.substring(separator + 1).any { it == '\u0000' || it == '\r' || it == '\n' }) {
                throw IOException("Invalid HTTP proxy header")
            }
        }
    }

    private fun performHandshake(
        target: HttpUrl,
        requestTarget: ByteArray,
        bearerToken: String,
        proof: DeviceProofHeaders,
        input: InputStream,
        output: OutputStream,
    ) {
        val nonce = ByteArray(16).also(secureRandom::nextBytes)
        val key = Base64.getEncoder().encodeToString(nonce)
        val host = if (':' in target.host) "[${target.host}]" else target.host
        val hostHeader = if (target.port == 443) host else "$host:${target.port}"
        val requestHeaders = buildString {
            append(" HTTP/1.1\r\n")
            append("Host: ").append(hostHeader).append("\r\n")
            append("Upgrade: websocket\r\n")
            append("Connection: Upgrade\r\n")
            append("Sec-WebSocket-Key: ").append(key).append("\r\n")
            append("Sec-WebSocket-Version: 13\r\n")
            append("Authorization: Bearer ").append(bearerToken).append("\r\n")
            append(DeviceProofV1.HEADER_VERSION).append(": ").append(proof.version).append("\r\n")
            append(DeviceProofV1.HEADER_TIMESTAMP).append(": ").append(proof.timestamp).append("\r\n")
            append(DeviceProofV1.HEADER_NONCE).append(": ").append(proof.nonce).append("\r\n")
            append(DeviceProofV1.HEADER_SIGNATURE).append(": ").append(proof.signature).append("\r\n")
            append("\r\n")
        }
        output.write("GET ".toByteArray(Charsets.US_ASCII))
        output.write(requestTarget)
        output.write(requestHeaders.toByteArray(Charsets.US_ASCII))
        output.flush()

        val response = readHttpHeader(
            input = input,
            maxBytes = MAX_HANDSHAKE_HEADER_BYTES,
            incompleteMessage = "Incomplete WebSocket handshake",
            tooLarge = { IOException("WebSocket handshake header exceeds limit") },
        )
        val lines = response.split("\r\n")
        val statusCode = lines.firstOrNull()
            ?.split(' ')
            ?.getOrNull(1)
            ?.toIntOrNull()
            ?: throw InvalidWebSocketFrameException()
        if (statusCode != 101) throw WebSocketHandshakeException(statusCode)
        val headers = LinkedHashMap<String, MutableList<String>>()
        lines.drop(1).filter(String::isNotEmpty).forEach { line ->
            val separator = line.indexOf(':')
            if (separator <= 0) throw InvalidWebSocketFrameException()
            val name = line.substring(0, separator).trim().lowercase(Locale.US)
            val value = line.substring(separator + 1).trim()
            headers.getOrPut(name, ::mutableListOf).add(value)
        }
        fun tokens(name: String): Set<String> = headers[name].orEmpty()
            .flatMap { it.split(',') }
            .mapTo(mutableSetOf()) { it.trim().lowercase(Locale.US) }

        if ("websocket" !in tokens("upgrade") || "upgrade" !in tokens("connection")) {
            throw InvalidWebSocketFrameException()
        }
        val expectedAccept = Base64.getEncoder().encodeToString(
            MessageDigest.getInstance("SHA-1").digest((key + WEB_SOCKET_GUID).toByteArray(Charsets.US_ASCII)),
        )
        if (headers["sec-websocket-accept"]?.singleOrNull() != expectedAccept) {
            throw InvalidWebSocketFrameException()
        }
        // No extension is offered. An unsolicited extension could change the post-header size.
        if (!headers["sec-websocket-extensions"].isNullOrEmpty()) {
            throw InvalidWebSocketFrameException()
        }
    }

    private fun readHttpHeader(
        input: InputStream,
        maxBytes: Int,
        incompleteMessage: String,
        tooLarge: () -> IOException,
    ): String {
        val bytes = ByteArrayOutputStream()
        var matched = 0
        while (bytes.size() < maxBytes) {
            val value = input.read()
            if (value < 0) throw IOException(incompleteMessage)
            bytes.write(value)
            matched = when {
                matched == 0 && value == '\r'.code -> 1
                matched == 1 && value == '\n'.code -> 2
                matched == 2 && value == '\r'.code -> 3
                matched == 3 && value == '\n'.code -> return bytes.toString(Charsets.ISO_8859_1.name())
                value == '\r'.code -> 1
                else -> 0
            }
        }
        throw tooLarge()
    }

    private fun writeClose(code: Int, reason: String): Boolean {
        if (!closeSent.compareAndSet(false, true)) return true
        return writeControl(OPCODE_CLOSE, closePayload(code, reason))
    }

    private fun closePayload(code: Int, reason: String): ByteArray {
        val reasonBytes = reason.toByteArray(Charsets.UTF_8)
        val boundedReason = if (reasonBytes.size <= 123) reasonBytes else ByteArray(0)
        return ByteBuffer.allocate(2 + boundedReason.size)
            .putShort(code.toShort())
            .put(boundedReason)
            .array()
    }

    private fun writeControl(opcode: Int, payload: ByteArray): Boolean {
        if (payload.size > MAX_CONTROL_PAYLOAD_BYTES) return false
        return writeFrame(opcode, payload)
    }

    private fun writeFrame(opcode: Int, payload: ByteArray): Boolean = writeLock.withLock {
        if (cancelled.get() || payload.size > maxFrameBytes) return@withLock false
        val sink = output ?: return@withLock false
        try {
            writeFrameLocked(sink, opcode, payload)
            true
        } catch (_: IOException) {
            false
        }
    }

    private fun writeFrameLocked(sink: OutputStream, opcode: Int, payload: ByteArray) {
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
        payload.forEachIndexed { index, byte -> sink.write(byte.toInt() xor mask[index % 4].toInt()) }
        sink.flush()
    }

    private companion object {
        const val HTTP_OK = 200
        const val IO_BUFFER_BYTES = 8 * 1024
        const val MAX_HANDSHAKE_HEADER_BYTES = 32 * 1024
        const val MAX_PROXY_RESPONSE_HEADER_BYTES = 16 * 1024
        const val MAX_CONTROL_PAYLOAD_BYTES = 125
        const val FIN_BIT = 0x80
        const val MASK_BIT = 0x80
        const val OPCODE_TEXT = 0x1
        const val OPCODE_CLOSE = 0x8
        const val OPCODE_PING = 0x9
        const val OPCODE_PONG = 0xA
        const val PING_PAYLOAD_BYTES = 8
        const val WEB_SOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        val PROXY_STATUS_LINE = Regex("HTTP/1\\.[01] ([0-9]{3})(?: [^\\r\\n]*)?")
        val HTTP_HEADER_NAME = Regex("[!#$%&'*+.^_`|~0-9A-Za-z-]+")

        fun isLoopbackHost(host: String): Boolean {
            if (host.equals("localhost", ignoreCase = true) || host == "::1") return true
            val octets = host.split('.')
            return octets.size == 4 && octets.all { part ->
                part.isNotEmpty() && part.length <= 3 && part.all(Char::isDigit) && part.toIntOrNull() in 0..255
            } && octets.first() == "127"
        }
    }
}

internal class BoundedWebSocketFrameReader(
    private val input: InputStream,
    private val maxFrameBytes: Int,
) {
    private var fragmentedText = false
    private var fragmentedBytes = 0
    private val fragments = ArrayList<ByteArray>()

    fun readNext(): RawSocketFrame {
        while (true) {
            val first = readRequired()
            val second = readRequired()
            val final = first and FIN_BIT != 0
            val opcode = first and OPCODE_MASK
            if (first and RSV_MASK != 0 || second and MASK_BIT != 0) throw InvalidWebSocketFrameException()
            val length = readLength(second and LENGTH_MASK)
            val control = opcode >= OPCODE_CLOSE
            if (control && (!final || length > MAX_CONTROL_PAYLOAD_BYTES)) throw InvalidWebSocketFrameException()
            if (length > maxFrameBytes) throw WebSocketFrameTooLargeException(length)
            if (opcode == OPCODE_BINARY) throw UnsupportedBinaryFrameException()
            if (opcode !in VALID_OPCODES) throw InvalidWebSocketFrameException()
            if (opcode == OPCODE_CONTINUATION && !fragmentedText) throw InvalidWebSocketFrameException()
            if (opcode == OPCODE_TEXT && fragmentedText) throw InvalidWebSocketFrameException()
            if (opcode == OPCODE_CONTINUATION || (!final && opcode == OPCODE_TEXT)) {
                if (fragmentedBytes.toLong() + length > maxFrameBytes) {
                    throw WebSocketFrameTooLargeException(fragmentedBytes.toLong() + length)
                }
                if (fragments.size >= MAX_FRAGMENTS) throw InvalidWebSocketFrameException()
            }
            val payload = readPayload(length.toInt())
            when (opcode) {
                OPCODE_TEXT -> {
                    if (final) return RawSocketFrame.Text(decodeUtf8(payload))
                    fragmentedText = true
                    appendFragment(payload)
                }
                OPCODE_CONTINUATION -> {
                    appendFragment(payload)
                    if (final) {
                        val combined = ByteArray(fragmentedBytes)
                        var offset = 0
                        fragments.forEach { fragment ->
                            fragment.copyInto(combined, offset)
                            offset += fragment.size
                        }
                        fragments.clear()
                        fragmentedBytes = 0
                        fragmentedText = false
                        return RawSocketFrame.Text(decodeUtf8(combined))
                    }
                }
                OPCODE_CLOSE -> return decodeClose(payload)
                OPCODE_PING -> return RawSocketFrame.Ping(payload)
                OPCODE_PONG -> return RawSocketFrame.Pong(payload)
                else -> throw InvalidWebSocketFrameException()
            }
        }
    }

    private fun readLength(shortLength: Int): Long = when (shortLength) {
        126 -> ((readRequired() shl 8) or readRequired()).toLong().also {
            if (it < 126) throw InvalidWebSocketFrameException()
        }
        127 -> {
            var value = 0L
            repeat(8) { index ->
                val next = readRequired()
                if (index == 0 && next and 0x80 != 0) throw InvalidWebSocketFrameException()
                value = (value shl 8) or next.toLong()
            }
            value.also { if (it <= 0xffff) throw InvalidWebSocketFrameException() }
        }
        else -> shortLength.toLong()
    }

    private fun readPayload(size: Int): ByteArray {
        val payload = ByteArray(size)
        var offset = 0
        while (offset < size) {
            val count = input.read(payload, offset, size - offset)
            if (count <= 0) throw IOException("Incomplete WebSocket frame")
            offset += count
        }
        return payload
    }

    private fun appendFragment(payload: ByteArray) {
        if (fragments.size >= MAX_FRAGMENTS) throw InvalidWebSocketFrameException()
        fragmentedBytes += payload.size
        fragments += payload
    }

    private fun decodeClose(payload: ByteArray): RawSocketFrame.Close {
        if (payload.isEmpty()) return RawSocketFrame.Close(1000, "")
        if (payload.size == 1) throw InvalidWebSocketFrameException()
        val code = ((payload[0].toInt() and 0xff) shl 8) or (payload[1].toInt() and 0xff)
        if (!isValidCloseCode(code)) throw InvalidWebSocketFrameException()
        return RawSocketFrame.Close(code, decodeUtf8(payload.copyOfRange(2, payload.size)))
    }

    private fun decodeUtf8(payload: ByteArray): String = try {
        Charsets.UTF_8.newDecoder()
            .onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT)
            .decode(ByteBuffer.wrap(payload))
            .toString()
    } catch (_: Exception) {
        throw InvalidWebSocketFrameException()
    }

    private fun readRequired(): Int = input.read().also { if (it < 0) throw IOException("WebSocket closed") }

    private fun isValidCloseCode(code: Int): Boolean =
        code in 1000..4999 && code !in setOf(1004, 1005, 1006, 1015)

    private companion object {
        const val FIN_BIT = 0x80
        const val RSV_MASK = 0x70
        const val OPCODE_MASK = 0x0f
        const val MASK_BIT = 0x80
        const val LENGTH_MASK = 0x7f
        const val OPCODE_CONTINUATION = 0x0
        const val OPCODE_TEXT = 0x1
        const val OPCODE_BINARY = 0x2
        const val OPCODE_CLOSE = 0x8
        const val OPCODE_PING = 0x9
        const val OPCODE_PONG = 0xA
        const val MAX_CONTROL_PAYLOAD_BYTES = 125L
        const val MAX_FRAGMENTS = 1_024
        val VALID_OPCODES = setOf(OPCODE_CONTINUATION, OPCODE_TEXT, OPCODE_CLOSE, OPCODE_PING, OPCODE_PONG)
    }
}

internal sealed interface RawSocketFrame {
    data class Text(val value: String) : RawSocketFrame
    data class Ping(val payload: ByteArray) : RawSocketFrame
    data class Close(val code: Int, val reason: String) : RawSocketFrame
    data class Pong(val payload: ByteArray) : RawSocketFrame
}
