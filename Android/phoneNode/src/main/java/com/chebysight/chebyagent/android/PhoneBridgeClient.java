package com.chebysight.chebyagent.android;

import android.content.Context;
import android.os.Build;
import android.util.Base64;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.net.URI;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.time.Instant;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import javax.net.ssl.SSLParameters;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.SSLSocketFactory;

final class PhoneBridgeClient {
    private static volatile JSONObject lastStatus = JsonUtil.obj(
            "running", false,
            "connection", "stopped",
            "message_for_user", "PhoneBridge has not started."
    );

    private static final String WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
    private static final int CONNECT_TIMEOUT_MS = 15000;
    private static final int HANDSHAKE_TIMEOUT_MS = 15000;
    private static final int READ_TIMEOUT_MS = 90000;
    private static final int MAX_INBOUND_FRAME_BYTES = 1024 * 1024;
    private static final long INITIAL_RECONNECT_DELAY_MS = 3000L;
    private static final long MAX_RECONNECT_DELAY_MS = 60000L;
    private static final SecureRandom RANDOM = new SecureRandom();

    private final Context context;
    private final AgentNodeSdk sdk;
    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private volatile boolean running = false;
    private Socket socket;
    private InputStream input;
    private OutputStream output;

    PhoneBridgeClient(Context context) {
        this.context = context.getApplicationContext();
        this.sdk = new AgentNodeSdk(context);
    }

    static JSONObject snapshot() {
        return lastStatus;
    }

    void start() {
        if (running) return;
        running = true;
        executor.execute(this::loop);
    }

    void stop() {
        running = false;
        closeSocket();
        executor.shutdownNow();
        setStatus("stopped", null, JsonUtil.obj("running", false));
    }

    private void loop() {
        setStatus("connecting", null, JsonUtil.obj("running", true));
        long reconnectDelayMs = INITIAL_RECONNECT_DELAY_MS;
        int consecutiveFailures = 0;
        while (running) {
            try {
                URI uri = CloudConfigStore.phoneBridgeUri(context);
                connect(uri);
                sendHello();
                reconnectDelayMs = INITIAL_RECONNECT_DELAY_MS;
                consecutiveFailures = 0;
                setStatus("connected", null, JsonUtil.obj(
                        "running", true,
                        "device_id", CloudConfigStore.getOrCreateDeviceId(context),
                        "bridge_url", uri.toString()
                ));
                AuditLog.append(context, "phonebridge_connected", JsonUtil.obj(
                        "bridge_host", uri.getHost() == null ? "" : uri.getHost()
                ));
                readLoop();
            } catch (Exception error) {
                if (running) {
                    consecutiveFailures++;
                    long retryInMs = reconnectDelayMs;
                    String errorCode = connectionErrorCode(error);
                    setStatus("error", connectionUserMessage(errorCode), JsonUtil.obj(
                            "running", true,
                            "error_code", errorCode,
                            "consecutive_failures", consecutiveFailures,
                            "retry_in_ms", retryInMs
                    ));
                    AuditLog.append(context, "phonebridge_connection_error", JsonUtil.obj(
                            "error_code", errorCode,
                            "cause_code", connectionErrorCode(error.getCause()),
                            "tls_client_identity", PhoneBridgeTlsStore.clientIdentityDecision(),
                            "consecutive_failures", consecutiveFailures,
                            "retry_in_ms", retryInMs
                    ));
                    sleep(retryInMs);
                    reconnectDelayMs = Math.min(MAX_RECONNECT_DELAY_MS, reconnectDelayMs * 2L);
                }
            } finally {
                closeSocket();
                if (running) setStatus("reconnecting", null, JsonUtil.obj("running", true));
            }
        }
    }

    private void connect(URI uri) throws Exception {
        String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase(Locale.US);
        int port = uri.getPort();
        if (port <= 0) port = "wss".equals(scheme) ? 443 : 80;
        String host = uri.getHost();
        if (host == null || host.trim().isEmpty()) throw new IllegalStateException("PhoneBridge URL has no host.");

        boolean secure = "wss".equals(scheme);
        if (!secure && !"ws".equals(scheme)) {
            throw new IllegalStateException("PhoneBridge URL must use ws:// or wss://.");
        }
        Socket tcpSocket = new Socket();
        this.socket = tcpSocket;
        Socket rawSocket;
        try {
            tcpSocket.setSoTimeout(HANDSHAKE_TIMEOUT_MS);
            tcpSocket.setKeepAlive(true);
            tcpSocket.connect(new InetSocketAddress(host, port), CONNECT_TIMEOUT_MS);
            if (secure) {
                PhoneBridgeTlsStore.beginTlsAttempt();
                SSLSocketFactory sslSocketFactory = PhoneBridgeTlsStore.socketFactory(context);
                rawSocket = sslSocketFactory.createSocket(tcpSocket, host, port, true);
                if (rawSocket instanceof SSLSocket) {
                    SSLSocket sslSocket = (SSLSocket) rawSocket;
                    sslSocket.setSoTimeout(HANDSHAKE_TIMEOUT_MS);
                    SSLParameters parameters = sslSocket.getSSLParameters();
                    parameters.setEndpointIdentificationAlgorithm("HTTPS");
                    sslSocket.setSSLParameters(parameters);
                    try {
                        sslSocket.startHandshake();
                    } catch (SocketTimeoutException error) {
                        throw new SocketTimeoutException("PhoneBridge TLS handshake timed out after " + HANDSHAKE_TIMEOUT_MS + "ms.");
                    }
                }
            } else {
                rawSocket = tcpSocket;
            }
        } catch (Exception error) {
            try {
                tcpSocket.close();
            } catch (Exception ignored) {
            }
            throw error;
        }
        rawSocket.setSoTimeout(HANDSHAKE_TIMEOUT_MS);
        rawSocket.setKeepAlive(true);
        this.socket = rawSocket;
        this.input = rawSocket.getInputStream();
        this.output = rawSocket.getOutputStream();

        String path = uri.getRawPath();
        if (path == null || path.isEmpty()) path = "/";
        if (uri.getRawQuery() != null && !uri.getRawQuery().isEmpty()) path += "?" + uri.getRawQuery();
        String requestPathForSignature = uri.getRawPath() == null || uri.getRawPath().isEmpty() ? "/" : uri.getRawPath();
        String key = randomBase64(16);
        Map<String, String> authHeaders = signedHeaders(requestPathForSignature);
        StringBuilder request = new StringBuilder();
        request.append("GET ").append(path).append(" HTTP/1.1\r\n");
        request.append("Host: ").append(host).append(":").append(port).append("\r\n");
        request.append("Upgrade: websocket\r\n");
        request.append("Connection: Upgrade\r\n");
        request.append("Sec-WebSocket-Key: ").append(key).append("\r\n");
        request.append("Sec-WebSocket-Version: 13\r\n");
        for (Map.Entry<String, String> entry : authHeaders.entrySet()) {
            request.append(entry.getKey()).append(": ").append(entry.getValue()).append("\r\n");
        }
        request.append("\r\n");
        output.write(request.toString().getBytes(StandardCharsets.UTF_8));
        output.flush();

        BufferedReader reader = new BufferedReader(new InputStreamReader(input, StandardCharsets.UTF_8));
        String statusLine;
        try {
            statusLine = reader.readLine();
        } catch (SocketTimeoutException error) {
            throw new SocketTimeoutException("PhoneBridge WebSocket upgrade timed out after " + HANDSHAKE_TIMEOUT_MS + "ms.");
        }
        if (statusLine == null || !statusLine.contains("101")) {
            throw new IllegalStateException("PhoneBridge WebSocket upgrade failed: " + statusLine);
        }
        Map<String, String> headers = new HashMap<>();
        String line;
        while ((line = reader.readLine()) != null && !line.isEmpty()) {
            int separator = line.indexOf(':');
            if (separator > 0) {
                headers.put(line.substring(0, separator).trim().toLowerCase(Locale.US), line.substring(separator + 1).trim());
            }
        }
        String accept = headers.get("sec-websocket-accept");
        String expectedBase64 = Base64.encodeToString(
                MessageDigest.getInstance("SHA-1").digest((key + WS_GUID).getBytes(StandardCharsets.UTF_8)),
                Base64.NO_WRAP
        );
        if (accept == null || !MessageDigest.isEqual(accept.getBytes(StandardCharsets.UTF_8), expectedBase64.getBytes(StandardCharsets.UTF_8))) {
            throw new IllegalStateException("PhoneBridge WebSocket accept header is invalid.");
        }
        if (secure) PhoneBridgeTlsStore.authenticatedHandshakeCompleted(context);
        rawSocket.setSoTimeout(READ_TIMEOUT_MS);
    }

    static String connectionErrorCode(Throwable error) {
        if (error == null) return "none";
        if (error instanceof PhoneBridgeTlsStore.ClientIdentityUpgradeRequiredException) {
            return "client_identity_upgrade_required";
        }
        if (error instanceof SocketTimeoutException) return "timeout";
        if (error instanceof javax.net.ssl.SSLHandshakeException
                || error instanceof javax.net.ssl.SSLProtocolException
                || error instanceof java.security.GeneralSecurityException) {
            return "tls_handshake_failed";
        }
        if (error instanceof java.net.ConnectException
                || error instanceof java.net.NoRouteToHostException
                || error instanceof java.net.UnknownHostException) {
            return "network_unreachable";
        }
        if (error instanceof java.io.IOException) return "transport_io_failed";
        if (error instanceof IllegalStateException) return "protocol_or_configuration_failed";
        return "unexpected_failure";
    }

    private static String connectionUserMessage(String errorCode) {
        switch (errorCode) {
            case "client_identity_upgrade_required":
                return "PhoneBridge 客户端身份需要重新置备。";
            case "timeout":
                return "PhoneBridge 连接超时，正在重试。";
            case "tls_handshake_failed":
                return "PhoneBridge 安全连接失败，正在重试。";
            case "network_unreachable":
                return "PhoneBridge 网络不可用，正在重试。";
            case "transport_io_failed":
                return "PhoneBridge 连接中断，正在重试。";
            default:
                return "PhoneBridge 暂时不可用，正在重试。";
        }
    }

    private Map<String, String> signedHeaders(String pathForSignature) throws Exception {
        String deviceId = CloudConfigStore.getOrCreateDeviceId(context);
        String token = PairingStore.getOrCreateToken(context);
        String timestamp = Instant.now().toString();
        String nonce = randomBase64Url(16);
        String payload = "GET\n"
                + pathForSignature + "\n"
                + timestamp + "\n"
                + nonce + "\n"
                + sha256Base64Url("") + "\n"
                + deviceId;

        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(token.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
        String signature = Base64.encodeToString(
                mac.doFinal(payload.getBytes(StandardCharsets.UTF_8)),
                Base64.URL_SAFE | Base64.NO_WRAP | Base64.NO_PADDING
        );

        Map<String, String> headers = new HashMap<>();
        headers.put("X-ChebyAgent-Key-Id", deviceId);
        headers.put("X-ChebyAgent-Device-Id", deviceId);
        headers.put("X-ChebyAgent-Timestamp", timestamp);
        headers.put("X-ChebyAgent-Nonce", nonce);
        headers.put("X-ChebyAgent-Signature", signature);
        return headers;
    }

    private void readLoop() throws Exception {
        while (running && socket != null && socket.isConnected() && !socket.isClosed()) {
            Frame frame = readFrame(input);
            if (frame == null) throw new IllegalStateException("PhoneBridge WebSocket closed.");
            if (frame.opcode == 0x8) throw new IllegalStateException("PhoneBridge closed the WebSocket.");
            if (frame.opcode == 0x9) {
                sendFrame(0xA, frame.payload);
                continue;
            }
            if (frame.opcode == 0xA) continue;
            if (frame.opcode != 0x1) continue;
            handleText(new String(frame.payload, StandardCharsets.UTF_8));
        }
    }

    private void handleText(String text) throws Exception {
        JSONObject command = new JSONObject(text);
        if (!"command".equals(command.optString("type"))) return;
        String commandId = command.optString("command_id", "");
        String tool = command.optString("tool", "");
        JSONObject args = command.optJSONObject("arguments");
        if (args == null) args = new JSONObject();
        try {
            setStatus("executing", null, JsonUtil.obj(
                    "running", true,
                    "command_id", commandId,
                    "tool_name", tool
            ));
            JSONObject result = sdk.executeTool(tool, args);
            sendJson(JsonUtil.obj(
                    "type", "result",
                    "command_id", commandId,
                    "ok", true,
                    "result", result
            ));
            sendHello();
            AuditLog.append(context, "phonebridge_command_completed", JsonUtil.obj(
                    "command_id", commandId,
                    "tool_name", tool
            ));
        } catch (Exception error) {
            sendJson(JsonUtil.obj(
                    "type", "result",
                    "command_id", commandId,
                    "ok", false,
                    "error", error.getMessage() == null ? String.valueOf(error) : error.getMessage()
            ));
            AuditLog.append(context, "phonebridge_command_failed", JsonUtil.obj(
                    "command_id", commandId,
                    "tool_name", tool,
                    "error", error.getMessage() == null ? String.valueOf(error) : error.getMessage()
            ));
        } finally {
            if (running) setStatus("connected", null, JsonUtil.obj("running", true));
        }
    }

    private void sendHello() throws Exception {
        JSONObject status;
        try {
            status = sdk.status();
        } catch (Exception error) {
            status = JsonUtil.obj("ok", false, "error", error.getMessage());
        }
        sendJson(JsonUtil.obj(
                "type", "hello",
                "device_id", CloudConfigStore.getOrCreateDeviceId(context),
                "device_name", Build.MANUFACTURER + " " + Build.MODEL,
                "product_name", BuildConfig.AGENT_NODE_PRODUCT_NAME,
                "app_version", BuildConfig.VERSION_NAME,
                "status", status
        ));
    }

    private synchronized void sendJson(JSONObject value) throws Exception {
        if (output == null) throw new IllegalStateException("PhoneBridge WebSocket is not connected.");
        sendFrame(0x1, value.toString().getBytes(StandardCharsets.UTF_8));
    }

    private synchronized void sendFrame(int opcode, byte[] payload) throws Exception {
        if (output == null) throw new IllegalStateException("PhoneBridge WebSocket is not connected.");
        output.write(encodeFrame(opcode, payload, true));
        output.flush();
    }

    private static Frame readFrame(InputStream input) throws Exception {
        int first = input.read();
        if (first < 0) return null;
        int second = input.read();
        if (second < 0) return null;
        boolean masked = (second & 0x80) != 0;
        long length = second & 0x7F;
        if (length == 126) {
            length = ((long) readByte(input) << 8) | readByte(input);
        } else if (length == 127) {
            length = 0L;
            for (int i = 0; i < 8; i++) length = (length << 8) | readByte(input);
        }
        if (length < 0 || length > MAX_INBOUND_FRAME_BYTES) {
            throw new IllegalStateException("PhoneBridge WebSocket frame is too large.");
        }
        byte[] mask = null;
        if (masked) {
            mask = new byte[]{(byte) readByte(input), (byte) readByte(input), (byte) readByte(input), (byte) readByte(input)};
        }
        byte[] payload = new byte[(int) length];
        int offset = 0;
        while (offset < payload.length) {
            int count = input.read(payload, offset, payload.length - offset);
            if (count < 0) throw new IllegalStateException("PhoneBridge WebSocket frame ended early.");
            offset += count;
        }
        if (masked) {
            for (int i = 0; i < payload.length; i++) payload[i] = (byte) (payload[i] ^ mask[i % 4]);
        }
        return new Frame(first & 0x0F, payload);
    }

    private static byte[] encodeFrame(int opcode, byte[] payload, boolean mask) throws Exception {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        output.write(0x80 | (opcode & 0x0F));
        int maskBit = mask ? 0x80 : 0;
        int length = payload.length;
        if (length < 126) {
            output.write(maskBit | length);
        } else if (length <= 0xFFFF) {
            output.write(maskBit | 126);
            output.write((length >>> 8) & 0xFF);
            output.write(length & 0xFF);
        } else {
            output.write(maskBit | 127);
            byte[] bytes = ByteBuffer.allocate(8).putLong(length).array();
            output.write(bytes);
        }
        byte[] body = payload;
        if (mask) {
            byte[] maskBytes = new byte[4];
            RANDOM.nextBytes(maskBytes);
            output.write(maskBytes);
            body = payload.clone();
            for (int i = 0; i < body.length; i++) body[i] = (byte) (body[i] ^ maskBytes[i % 4]);
        }
        output.write(body);
        return output.toByteArray();
    }

    private static int readByte(InputStream input) throws Exception {
        int value = input.read();
        if (value < 0) throw new IllegalStateException("Unexpected end of WebSocket frame.");
        return value & 0xFF;
    }

    private void closeSocket() {
        try {
            if (socket != null) socket.close();
        } catch (Exception ignored) {
        }
        socket = null;
        input = null;
        output = null;
    }

    private void setStatus(String connection, String error, JSONObject extra) {
        JSONObject status = JsonUtil.obj(
                "running", running,
                "connection", connection,
                "bridge_url", CloudConfigStore.phoneBridgeUrl(context),
                "device_id", CloudConfigStore.getOrCreateDeviceId(context),
                "updated_at_ms", System.currentTimeMillis(),
                "message_for_user", "connected".equals(connection)
                        ? "PhoneBridge connected."
                        : "error".equals(connection)
                        ? "PhoneBridge connection error."
                        : "PhoneBridge " + connection + "."
        );
        if (error != null && !error.trim().isEmpty()) {
            try {
                status.put("error", error);
            } catch (Exception ignored) {
            }
        }
        if (extra != null) {
            for (java.util.Iterator<String> it = extra.keys(); it.hasNext(); ) {
                String key = it.next();
                try {
                    status.put(key, extra.get(key));
                } catch (Exception ignored) {
                }
            }
        }
        try {
            status.put("tls", PhoneBridgeTlsStore.status(context));
        } catch (Exception ignored) {
        }
        lastStatus = status;
    }

    private static String sha256Base64Url(String value) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        return Base64.encodeToString(
                digest.digest(value.getBytes(StandardCharsets.UTF_8)),
                Base64.URL_SAFE | Base64.NO_WRAP | Base64.NO_PADDING
        );
    }

    private static String randomBase64(int byteCount) {
        byte[] random = new byte[byteCount];
        RANDOM.nextBytes(random);
        return Base64.encodeToString(random, Base64.NO_WRAP);
    }

    private static String randomBase64Url(int byteCount) {
        byte[] random = new byte[byteCount];
        RANDOM.nextBytes(random);
        return Base64.encodeToString(random, Base64.URL_SAFE | Base64.NO_WRAP | Base64.NO_PADDING);
    }

    private void sleep(long delayMs) {
        try {
            Thread.sleep(delayMs);
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }
    }

    private static final class Frame {
        final int opcode;
        final byte[] payload;

        Frame(int opcode, byte[] payload) {
            this.opcode = opcode;
            this.payload = payload;
        }
    }
}
