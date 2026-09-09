package com.cheby.codex.mobile.gateway

import com.cheby.codex.mobile.data.RelayDeviceSession
import com.cheby.codex.mobile.data.SecureRelayConfigStore
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.time.Duration
import java.util.Base64
import java.util.UUID
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

internal class RelayCredentialClient(
    private val client: OkHttpClient = defaultRelayHttpClient(),
    private val json: Json = Json { ignoreUnknownKeys = false },
    private val originValidator: (String) -> Unit = {},
) {
    suspend fun exchange(
        bootstrap: RelayEnrollmentBootstrap,
        deviceName: String,
        proofKey: DeviceProofKeyHandle,
    ): RelayDeviceSession {
        val body = buildJsonObject {
            put("assistantId", bootstrap.assistantId)
            put("pairingSecret", bootstrap.pairingSecret)
            put("deviceName", deviceName.take(80))
            put(
                "devicePublicKey",
                Base64.getEncoder().encodeToString(proofKey.publicKeySpkiDer()),
            )
        }.toString().toByteArray(Charsets.UTF_8)
        val response = execute(
            bootstrap.relayOrigin,
            RelayManagementOperation.ENROLL,
            body,
            proofKey,
        )
        val root = strictObject(response, PAIR_RESPONSE_FIELDS)
        val assistantId = root.string("assistantId")
        if (assistantId != bootstrap.assistantId || !ASSISTANT_ID.matches(assistantId)) mismatch()
        return RelayDeviceSession(
            assistantId = assistantId,
            assistantAlias = bootstrap.assistantAlias,
            relayOrigin = bootstrap.relayOrigin,
            bindingScope = UUID.randomUUID().toString(),
            principalId = root.string("deviceId").also { if (!DEVICE_ID.matches(it)) mismatch() },
            accessToken = root.credential("accessToken"),
            refreshToken = root.credential("refreshToken"),
            accessExpiresAt = root.positiveLong("accessExpiresAt"),
            refreshExpiresAt = root.positiveLong("refreshExpiresAt"),
            relayAckCursor = 0,
            gatewayStreamId = "pending",
            gatewayAckSeq = 0,
        ).also { if (it.refreshExpiresAt <= it.accessExpiresAt) mismatch() }
    }

    suspend fun refresh(
        current: RelayDeviceSession,
        proofKey: DeviceProofKeyHandle,
        store: SecureRelayConfigStore,
    ): RelayDeviceSession = refreshAtOrigin(
        current = current,
        requestOrigin = current.relayOrigin,
        proofKey = proofKey,
        store = store,
        returnRequestOrigin = false,
    )

    /**
     * Rotates an existing binding at a candidate origin before an origin migration probe.
     *
     * Access credentials are intentionally short-lived and may already have been garbage-
     * collected when a phone migrates after a long offline period. Refresh is authenticated by
     * the durable device key and refresh token, so it is the correct first proof at the candidate
     * server. The rotated credentials are committed while the old origin is still durable; a
     * crash before the later Ready/CAS step can therefore reconnect through the old route with
     * the new tokens. Only the returned in-memory candidate carries the requested origin.
     */
    suspend fun refreshForOriginMigration(
        candidate: RelayDeviceSession,
        proofKey: DeviceProofKeyHandle,
        store: SecureRelayConfigStore,
    ): RelayDeviceSession = refreshAtOrigin(
        current = candidate,
        requestOrigin = candidate.relayOrigin,
        proofKey = proofKey,
        store = store,
        returnRequestOrigin = true,
    )

    private suspend fun refreshAtOrigin(
        current: RelayDeviceSession,
        requestOrigin: String,
        proofKey: DeviceProofKeyHandle,
        store: SecureRelayConfigStore,
        returnRequestOrigin: Boolean,
    ): RelayDeviceSession = PROCESS_REFRESH_MUTEX.withLock {
        // A configuration change can briefly leave two gateway instances alive in the same app
        // process. Serialize refreshes and re-read durable state inside the lock so a waiter adopts
        // credentials already installed by the winner. The Relay sender-constrains a stable refresh
        // credential to this device key and rotates only short-lived access credentials, making a
        // committed response safe to retry after loss or process death.
        val latest = store.loadSession()?.takeIf {
            it.bindingScope == current.bindingScope && it.principalId == current.principalId
        } ?: throw GatewayTransportException("Relay session changed")
        if (latest.refreshToken != current.refreshToken) {
            return@withLock if (returnRequestOrigin) {
                latest.copy(relayOrigin = requestOrigin)
            } else {
                latest
            }
        }

        val body = buildJsonObject {
            put("deviceId", latest.principalId)
            put("refreshToken", latest.refreshToken)
        }.toString().toByteArray(Charsets.UTF_8)
        val response = execute(
            requestOrigin,
            RelayManagementOperation.REFRESH,
            body,
            proofKey,
        )
        val root = strictObject(response, REFRESH_RESPONSE_FIELDS)
        if (root.string("deviceId") != latest.principalId) mismatch()
        val accessToken = root.credential("accessToken")
        val refreshToken = root.credential("refreshToken")
        val accessExpiresAt = root.positiveLong("accessExpiresAt")
        val refreshExpiresAt = root.positiveLong("refreshExpiresAt")
        if (refreshExpiresAt <= accessExpiresAt) {
            throw GatewayTransportException("Relay credential rotation failed")
        }
        val merged = store.mergeRotatedCredentials(
            expectedBindingScope = latest.bindingScope,
            expectedPrincipalId = latest.principalId,
            expectedRefreshToken = latest.refreshToken,
            accessToken = accessToken,
            refreshToken = refreshToken,
            accessExpiresAt = accessExpiresAt,
            refreshExpiresAt = refreshExpiresAt,
        ) ?: throw GatewayTransportException("Relay credential rotation failed")
        if (returnRequestOrigin) merged.copy(relayOrigin = requestOrigin) else merged
    }

    private suspend fun execute(
        relayOrigin: String,
        operation: RelayManagementOperation,
        body: ByteArray,
        proofKey: DeviceProofKeyHandle,
    ): String = withContext(Dispatchers.IO) {
        originValidator(relayOrigin)
        val rawUrl = relayOrigin + operation.path
        RelayManagementEndpoint.parse(rawUrl, operation)
        val url = rawUrl.toHttpUrlOrNull() ?: throw GatewayTransportException("Relay configuration is invalid")
        val proof = DeviceProofV1.sign(
            key = proofKey,
            method = "POST",
            rawTarget = DeviceProofV1.originForm(url),
            body = body,
            bearerToken = null,
        )
        val request = Request.Builder()
            .url(url)
            .post(body.toRequestBody(JSON_MEDIA_TYPE))
            .header("Accept", "application/json")
            .apply { proof.asMap().forEach(::header) }
            .build()
        val response = try {
            client.newCall(request).execute()
        } catch (_: Exception) {
            throw GatewayTransportException("Relay is unavailable")
        }
        response.use {
            val responseBody = readBounded(it.body?.byteStream(), MAX_RESPONSE_BYTES)
            if (it.code !in 200..299) throw decodeError(responseBody, it)
            val cacheControl = it.header("Cache-Control").orEmpty().lowercase()
            if ("no-store" !in cacheControl.split(',').map(String::trim)) {
                throw GatewayTransportException("Relay response cache policy is unsafe")
            }
            responseBody
        }
    }

    private fun decodeError(body: String, response: Response): GatewayRequestException {
        val root = runCatching { strictObject(body, ERROR_FIELDS) }.getOrNull()
        val code = root?.get("code")?.jsonPrimitive?.contentOrNull
            ?.takeIf(ERROR_CODE::matches) ?: "RELAY_REJECTED"
        val retryable = root?.get("retryable")?.jsonPrimitive?.contentOrNull?.toBooleanStrictOrNull()
            ?: (response.code == 429)
        val retryAfter = parseRetryAfterMillis(response.header("Retry-After"))
        return GatewayRequestException(code, retryable, retryAfter)
    }

    private fun strictObject(raw: String, fields: Set<String>): JsonObject = try {
        val root = json.parseToJsonElement(raw).jsonObject
        if (root.keys != fields) mismatch()
        root
    } catch (error: GatewayTransportException) {
        throw error
    } catch (_: Exception) {
        mismatch()
    }

    private fun JsonObject.string(key: String): String =
        getValue(key).jsonPrimitive.contentOrNull?.takeIf(String::isNotBlank) ?: mismatch()
    private fun JsonObject.credential(key: String): String = string(key).also {
        if (it.length !in 32..256 || it.any { char -> char.code !in 0x21..0x7e }) mismatch()
    }
    private fun JsonObject.positiveLong(key: String): Long =
        (getValue(key).jsonPrimitive.longOrNull ?: mismatch()).also { if (it <= 0) mismatch() }
    private fun mismatch(): Nothing = throw GatewayTransportException("Relay protocol mismatch")

    companion object {
        private val PROCESS_REFRESH_MUTEX = Mutex()
        private val JSON_MEDIA_TYPE = "application/json".toMediaType()
        private val ASSISTANT_ID = Regex("^asst_[A-Za-z0-9_-]{22}$")
        private val DEVICE_ID = Regex("^dev_[A-Za-z0-9_-]{22}$")
        private val ERROR_CODE = Regex("^[A-Z][A-Z0-9_]{1,63}$")
        private val PAIR_RESPONSE_FIELDS = setOf(
            "assistantId", "deviceId", "accessToken", "accessExpiresAt",
            "refreshToken", "refreshExpiresAt",
        )
        private val REFRESH_RESPONSE_FIELDS = setOf(
            "deviceId", "accessToken", "accessExpiresAt", "refreshToken", "refreshExpiresAt",
        )
        private val ERROR_FIELDS = setOf("code", "message", "retryable")
        private const val MAX_RESPONSE_BYTES = 64 * 1024
    }
}

private fun readBounded(input: InputStream?, limit: Int): String {
    if (input == null) return ""
    val output = ByteArrayOutputStream()
    val buffer = ByteArray(8 * 1024)
    while (true) {
        val read = input.read(buffer)
        if (read < 0) break
        if (output.size() + read > limit) throw GatewayTransportException("Relay response is too large")
        output.write(buffer, 0, read)
    }
    return output.toByteArray().toString(Charsets.UTF_8)
}

private fun defaultRelayHttpClient(): OkHttpClient = defaultRelayHttpClientBuilder().build()

internal fun defaultRelayHttpClientBuilder(): OkHttpClient.Builder = OkHttpClient.Builder()
    .connectTimeout(Duration.ofSeconds(15))
    .readTimeout(Duration.ofSeconds(30))
    .writeTimeout(Duration.ofSeconds(30))
    .callTimeout(Duration.ofSeconds(45))
    .followRedirects(false)
    .followSslRedirects(false)
    .retryOnConnectionFailure(false)
