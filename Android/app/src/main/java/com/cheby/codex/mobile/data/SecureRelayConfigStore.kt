package com.cheby.codex.mobile.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put

/**
 * Durable account binding. No WebSocket connection id is persisted because reconnecting must not
 * create a new account binding or Codex Thread.
 */
data class RelayDeviceSession(
    val assistantId: String,
    val assistantAlias: String,
    val relayOrigin: String,
    val bindingScope: String,
    val principalId: String,
    val accessToken: String,
    val refreshToken: String,
    val accessExpiresAt: Long,
    val refreshExpiresAt: Long,
    val relayAckCursor: Long = 0,
    val gatewayStreamId: String,
    val gatewayAckSeq: Long = 0,
) {
    override fun toString(): String = "RelayDeviceSession(redacted)"
}

/** A response is durably recorded before its cumulative Relay acknowledgement is advanced. */
data class RelayResponseReceipt(
    val requestId: String,
    val deviceId: String,
    val operation: String,
    val resultJson: String?,
    val errorCode: String?,
    val retryable: Boolean,
    val retryAfterMillis: Long?,
    val deliverySeq: Long,
) {
    override fun toString(): String = "RelayResponseReceipt(redacted)"
}

interface SecureRelayConfigStore {
    fun loadSession(): RelayDeviceSession?
    fun saveSession(session: RelayDeviceSession): Boolean
    /**
     * Atomically changes only the origin after an authenticated candidate connection succeeded.
     * All credentials, cursors, receipts and binding identities are taken from the latest durable
     * session rather than from the earlier probe snapshot.
     */
    fun migrateRelayOrigin(
        expectedBindingScope: String,
        expectedOrigin: String,
        newOrigin: String,
    ): RelayDeviceSession?
    fun mergeRotatedCredentials(
        expectedBindingScope: String,
        expectedPrincipalId: String,
        expectedRefreshToken: String,
        accessToken: String,
        refreshToken: String,
        accessExpiresAt: Long,
        refreshExpiresAt: Long,
    ): RelayDeviceSession?
    fun updateRelayCursor(expectedBindingScope: String, deliverySeq: Long): Boolean
    fun updateGatewayCursor(expectedBindingScope: String, streamId: String, eventSeq: Long): Boolean
    fun loadResponseReceipt(expectedBindingScope: String, requestId: String): RelayResponseReceipt?
    fun recordResponseReceipt(expectedBindingScope: String, receipt: RelayResponseReceipt): Boolean
    fun clearSession()
    fun clearSessionIfScope(expectedBindingScope: String): Boolean
}

/**
 * Serializes each durable Relay-store operation across every store wrapper in this app process.
 *
 * SharedPreferences is process-wide, while Kotlin's `@Synchronized` locks only one store
 * instance. During a short gateway replacement window two Android store instances can otherwise
 * read the same encrypted session and overwrite each other's cursor or rotated credential update.
 */
internal class ProcessLockedRelayConfigStore(
    private val delegate: SecureRelayConfigStore,
) : SecureRelayConfigStore {
    override fun loadSession(): RelayDeviceSession? = locked { delegate.loadSession() }

    override fun saveSession(session: RelayDeviceSession): Boolean =
        locked { delegate.saveSession(session) }

    override fun migrateRelayOrigin(
        expectedBindingScope: String,
        expectedOrigin: String,
        newOrigin: String,
    ): RelayDeviceSession? = locked {
        delegate.migrateRelayOrigin(expectedBindingScope, expectedOrigin, newOrigin)
    }

    override fun mergeRotatedCredentials(
        expectedBindingScope: String,
        expectedPrincipalId: String,
        expectedRefreshToken: String,
        accessToken: String,
        refreshToken: String,
        accessExpiresAt: Long,
        refreshExpiresAt: Long,
    ): RelayDeviceSession? = locked {
        delegate.mergeRotatedCredentials(
            expectedBindingScope,
            expectedPrincipalId,
            expectedRefreshToken,
            accessToken,
            refreshToken,
            accessExpiresAt,
            refreshExpiresAt,
        )
    }

    override fun updateRelayCursor(expectedBindingScope: String, deliverySeq: Long): Boolean =
        locked { delegate.updateRelayCursor(expectedBindingScope, deliverySeq) }

    override fun updateGatewayCursor(
        expectedBindingScope: String,
        streamId: String,
        eventSeq: Long,
    ): Boolean = locked {
        delegate.updateGatewayCursor(expectedBindingScope, streamId, eventSeq)
    }

    override fun loadResponseReceipt(
        expectedBindingScope: String,
        requestId: String,
    ): RelayResponseReceipt? = locked {
        delegate.loadResponseReceipt(expectedBindingScope, requestId)
    }

    override fun recordResponseReceipt(
        expectedBindingScope: String,
        receipt: RelayResponseReceipt,
    ): Boolean = locked {
        delegate.recordResponseReceipt(expectedBindingScope, receipt)
    }

    override fun clearSession() = locked { delegate.clearSession() }

    override fun clearSessionIfScope(expectedBindingScope: String): Boolean =
        locked { delegate.clearSessionIfScope(expectedBindingScope) }

    private inline fun <T> locked(block: () -> T): T = synchronized(PROCESS_LOCK, block)

    private companion object {
        val PROCESS_LOCK = Any()
    }
}

/** Relay credentials use a separate AndroidKeystore key and preference namespace from REMOTE. */
class AndroidKeystoreRelayConfigStore(context: Context) : SecureRelayConfigStore {
    private val preferences = context.getSharedPreferences(PREFERENCES_NAME, Context.MODE_PRIVATE)
    private val json = Json { ignoreUnknownKeys = false }

    @Synchronized
    override fun loadSession(): RelayDeviceSession? = decrypt(KEY_SESSION)?.let { raw ->
        runCatching {
            val body = json.parseToJsonElement(raw).jsonObject
            RelayDeviceSession(
                assistantId = body.requiredString("assistantId"),
                assistantAlias = body.requiredString("assistantAlias"),
                relayOrigin = body.requiredString("relayOrigin"),
                bindingScope = body.requiredString("bindingScope"),
                principalId = body.requiredString("principalId"),
                accessToken = body.requiredString("accessToken"),
                refreshToken = body.requiredString("refreshToken"),
                accessExpiresAt = body.requiredLong("accessExpiresAt"),
                refreshExpiresAt = body.requiredLong("refreshExpiresAt"),
                relayAckCursor = body["relayAckCursor"]?.jsonPrimitive?.longOrNull ?: 0,
                gatewayStreamId = body.requiredString("gatewayStreamId"),
                gatewayAckSeq = body["gatewayAckSeq"]?.jsonPrimitive?.longOrNull ?: 0,
            ).validated()
        }.getOrNull()
    }

    @Synchronized
    override fun saveSession(session: RelayDeviceSession): Boolean {
        session.validated()
        val replacesBinding = loadSession()?.bindingScope != session.bindingScope
        val body = buildJsonObject {
            put("assistantId", session.assistantId)
            put("assistantAlias", session.assistantAlias)
            put("relayOrigin", session.relayOrigin)
            put("bindingScope", session.bindingScope)
            put("principalId", session.principalId)
            put("accessToken", session.accessToken)
            put("refreshToken", session.refreshToken)
            put("accessExpiresAt", session.accessExpiresAt)
            put("refreshExpiresAt", session.refreshExpiresAt)
            put("relayAckCursor", session.relayAckCursor)
            put("gatewayStreamId", session.gatewayStreamId)
            put("gatewayAckSeq", session.gatewayAckSeq)
        }
        val saved = preferences.edit().putString(KEY_SESSION, encrypt(KEY_SESSION, body.toString())).commit()
        if (saved && replacesBinding) preferences.edit().remove(KEY_RESPONSES).commit()
        return saved
    }

    @Synchronized
    override fun migrateRelayOrigin(
        expectedBindingScope: String,
        expectedOrigin: String,
        newOrigin: String,
    ): RelayDeviceSession? {
        val current = loadSession() ?: return null
        if (
            current.bindingScope != expectedBindingScope ||
            current.relayOrigin != expectedOrigin ||
            newOrigin == expectedOrigin
        ) return null
        val migrated = current.copy(relayOrigin = newOrigin).validated()
        return migrated.takeIf(::saveSession)
    }

    @Synchronized
    override fun mergeRotatedCredentials(
        expectedBindingScope: String,
        expectedPrincipalId: String,
        expectedRefreshToken: String,
        accessToken: String,
        refreshToken: String,
        accessExpiresAt: Long,
        refreshExpiresAt: Long,
    ): RelayDeviceSession? {
        val current = loadSession() ?: return null
        if (
            current.bindingScope != expectedBindingScope ||
            current.principalId != expectedPrincipalId ||
            current.refreshToken != expectedRefreshToken
        ) return null
        val merged = current.copy(
            accessToken = accessToken,
            refreshToken = refreshToken,
            accessExpiresAt = accessExpiresAt,
            refreshExpiresAt = refreshExpiresAt,
        ).validated()
        return merged.takeIf(::saveSession)
    }

    @Synchronized
    override fun updateRelayCursor(expectedBindingScope: String, deliverySeq: Long): Boolean {
        if (deliverySeq < 0) return false
        val current = loadSession() ?: return false
        if (current.bindingScope != expectedBindingScope) return false
        return saveSession(current.copy(relayAckCursor = maxOf(current.relayAckCursor, deliverySeq)))
    }

    @Synchronized
    override fun updateGatewayCursor(
        expectedBindingScope: String,
        streamId: String,
        eventSeq: Long,
    ): Boolean {
        if (streamId.isBlank() || eventSeq < 0) return false
        val current = loadSession() ?: return false
        if (current.bindingScope != expectedBindingScope) return false
        val updated = if (current.gatewayStreamId == streamId) {
            current.copy(gatewayAckSeq = maxOf(current.gatewayAckSeq, eventSeq))
        } else {
            current.copy(gatewayStreamId = streamId, gatewayAckSeq = eventSeq)
        }
        return saveSession(updated)
    }

    @Synchronized
    override fun loadResponseReceipt(
        expectedBindingScope: String,
        requestId: String,
    ): RelayResponseReceipt? {
        if (loadSession()?.bindingScope != expectedBindingScope) return null
        return loadResponseReceipts().firstOrNull { it.requestId == requestId }
    }

    @Synchronized
    override fun recordResponseReceipt(
        expectedBindingScope: String,
        receipt: RelayResponseReceipt,
    ): Boolean {
        if (loadSession()?.bindingScope != expectedBindingScope) return false
        receipt.validated()
        val retained = (loadResponseReceipts()
            .filterNot { it.requestId == receipt.requestId }
            .takeLast(MAX_RESPONSE_RECEIPTS - 1) + receipt).toMutableList()
        var encoded = encodeResponseReceipts(retained)
        while (encoded.toByteArray(Charsets.UTF_8).size > MAX_RESPONSE_RECEIPTS_BYTES && retained.size > 1) {
            retained.removeAt(0)
            encoded = encodeResponseReceipts(retained)
        }
        if (encoded.toByteArray(Charsets.UTF_8).size > MAX_RESPONSE_RECEIPTS_BYTES) return false
        return preferences.edit().putString(KEY_RESPONSES, encrypt(KEY_RESPONSES, encoded)).commit()
    }

    private fun encodeResponseReceipts(receipts: List<RelayResponseReceipt>): String =
        buildJsonArray {
            receipts.forEach { item ->
                add(buildJsonObject {
                    put("requestId", item.requestId)
                    put("deviceId", item.deviceId)
                    put("operation", item.operation)
                    item.resultJson?.let { put("resultJson", it) }
                    item.errorCode?.let { put("errorCode", it) }
                    put("retryable", item.retryable)
                    item.retryAfterMillis?.let { put("retryAfterMillis", it) }
                    put("deliverySeq", item.deliverySeq)
                })
            }
        }.toString()

    @Synchronized
    override fun clearSession() {
        preferences.edit().remove(KEY_SESSION).remove(KEY_RESPONSES).apply()
    }

    @Synchronized
    override fun clearSessionIfScope(expectedBindingScope: String): Boolean {
        if (loadSession()?.bindingScope != expectedBindingScope) return false
        return preferences.edit().remove(KEY_SESSION).remove(KEY_RESPONSES).commit()
    }

    private fun loadResponseReceipts(): List<RelayResponseReceipt> =
        decrypt(KEY_RESPONSES)?.let { raw ->
            runCatching {
                json.parseToJsonElement(raw).jsonArray.map { element ->
                    val body = element.jsonObject
                    RelayResponseReceipt(
                        requestId = body.requiredString("requestId"),
                        deviceId = body.requiredString("deviceId"),
                        operation = body.requiredString("operation"),
                        resultJson = body["resultJson"]?.jsonPrimitive?.content,
                        errorCode = body["errorCode"]?.jsonPrimitive?.content,
                        retryable = body.getValue("retryable").jsonPrimitive.content.toBooleanStrict(),
                        retryAfterMillis = body["retryAfterMillis"]?.jsonPrimitive?.longOrNull,
                        deliverySeq = body.requiredLong("deliverySeq"),
                    ).validated()
                }
            }.getOrNull()
        }.orEmpty()

    private fun encrypt(preferenceKey: String, plaintext: String): String {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, getOrCreateSecretKey())
        cipher.updateAAD(preferenceKey.toByteArray(Charsets.UTF_8))
        val ciphertext = cipher.doFinal(plaintext.toByteArray(Charsets.UTF_8))
        return listOf(
            FORMAT_VERSION,
            Base64.encodeToString(cipher.iv, Base64.NO_WRAP),
            Base64.encodeToString(ciphertext, Base64.NO_WRAP),
        ).joinToString(".")
    }

    private fun decrypt(preferenceKey: String): String? {
        val encoded = preferences.getString(preferenceKey, null) ?: return null
        return runCatching {
            val parts = encoded.split('.')
            require(parts.size == 3 && parts[0] == FORMAT_VERSION)
            val cipher = Cipher.getInstance(TRANSFORMATION)
            val iv = Base64.decode(parts[1], Base64.NO_WRAP)
            val ciphertext = Base64.decode(parts[2], Base64.NO_WRAP)
            cipher.init(Cipher.DECRYPT_MODE, getOrCreateSecretKey(), GCMParameterSpec(128, iv))
            cipher.updateAAD(preferenceKey.toByteArray(Charsets.UTF_8))
            cipher.doFinal(ciphertext).toString(Charsets.UTF_8)
        }.getOrElse {
            preferences.edit().remove(preferenceKey).apply()
            null
        }
    }

    private fun getOrCreateSecretKey(): SecretKey {
        val keyStore = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
        (keyStore.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
        generator.init(
            KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .build(),
        )
        return generator.generateKey()
    }

    private fun kotlinx.serialization.json.JsonObject.requiredString(key: String): String =
        getValue(key).jsonPrimitive.content.takeIf(String::isNotBlank)
            ?: error("Missing secure configuration field")

    private fun kotlinx.serialization.json.JsonObject.requiredLong(key: String): Long =
        getValue(key).jsonPrimitive.longOrNull ?: error("Missing secure configuration field")

    private companion object {
        const val PREFERENCES_NAME = "chebycodex_secure_relay_v1"
        const val KEY_SESSION = "session"
        const val KEY_RESPONSES = "responses"
        const val KEY_ALIAS = "chebycodex.relay.credentials.v1"
        const val FORMAT_VERSION = "v1"
        const val ANDROID_KEYSTORE = "AndroidKeyStore"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val MAX_RESPONSE_RECEIPTS = 64
        const val MAX_RESPONSE_RECEIPTS_BYTES = 4 * 1024 * 1024
    }
}

class InMemorySecureRelayConfigStore(
    session: RelayDeviceSession? = null,
    private val allowSessionWrites: Boolean = true,
    private val rejectGatewayCursorAtOrAbove: Long? = null,
) : SecureRelayConfigStore {
    private var sessionValue = session
    private val receipts = linkedMapOf<String, RelayResponseReceipt>()
    @Synchronized
    override fun loadSession(): RelayDeviceSession? = sessionValue
    @Synchronized
    override fun saveSession(session: RelayDeviceSession): Boolean {
        if (!allowSessionWrites) return false
        if (sessionValue?.bindingScope != session.bindingScope) receipts.clear()
        sessionValue = session.validated()
        return true
    }
    @Synchronized
    override fun migrateRelayOrigin(
        expectedBindingScope: String,
        expectedOrigin: String,
        newOrigin: String,
    ): RelayDeviceSession? {
        val current = sessionValue ?: return null
        if (
            !allowSessionWrites ||
            current.bindingScope != expectedBindingScope ||
            current.relayOrigin != expectedOrigin ||
            newOrigin == expectedOrigin
        ) return null
        return current.copy(relayOrigin = newOrigin).validated().also { sessionValue = it }
    }
    @Synchronized
    override fun mergeRotatedCredentials(
        expectedBindingScope: String,
        expectedPrincipalId: String,
        expectedRefreshToken: String,
        accessToken: String,
        refreshToken: String,
        accessExpiresAt: Long,
        refreshExpiresAt: Long,
    ): RelayDeviceSession? {
        val current = sessionValue ?: return null
        if (
            current.bindingScope != expectedBindingScope ||
            current.principalId != expectedPrincipalId ||
            current.refreshToken != expectedRefreshToken ||
            !allowSessionWrites
        ) return null
        return current.copy(
            accessToken = accessToken,
            refreshToken = refreshToken,
            accessExpiresAt = accessExpiresAt,
            refreshExpiresAt = refreshExpiresAt,
        ).validated().also { sessionValue = it }
    }
    @Synchronized
    override fun updateRelayCursor(expectedBindingScope: String, deliverySeq: Long): Boolean {
        if (deliverySeq < 0) return false
        val current = sessionValue ?: return false
        if (current.bindingScope != expectedBindingScope) return false
        sessionValue = current.copy(relayAckCursor = maxOf(current.relayAckCursor, deliverySeq))
        return true
    }
    @Synchronized
    override fun updateGatewayCursor(
        expectedBindingScope: String,
        streamId: String,
        eventSeq: Long,
    ): Boolean {
        if (streamId.isBlank() || eventSeq < 0) return false
        if (rejectGatewayCursorAtOrAbove?.let { eventSeq >= it } == true) return false
        val current = sessionValue ?: return false
        if (current.bindingScope != expectedBindingScope) return false
        sessionValue = if (current.gatewayStreamId == streamId) {
            current.copy(gatewayAckSeq = maxOf(current.gatewayAckSeq, eventSeq))
        } else {
            current.copy(gatewayStreamId = streamId, gatewayAckSeq = eventSeq)
        }
        return true
    }
    @Synchronized
    override fun loadResponseReceipt(
        expectedBindingScope: String,
        requestId: String,
    ): RelayResponseReceipt? = if (sessionValue?.bindingScope == expectedBindingScope) {
        receipts[requestId]
    } else null
    @Synchronized
    override fun recordResponseReceipt(
        expectedBindingScope: String,
        receipt: RelayResponseReceipt,
    ): Boolean {
        if (sessionValue?.bindingScope != expectedBindingScope) return false
        receipt.validated()
        receipts.remove(receipt.requestId)
        receipts[receipt.requestId] = receipt
        while (receipts.size > 64) receipts.remove(receipts.keys.first())
        return true
    }
    @Synchronized
    override fun clearSession() {
        sessionValue = null
        receipts.clear()
    }
    @Synchronized
    override fun clearSessionIfScope(expectedBindingScope: String): Boolean {
        if (sessionValue?.bindingScope != expectedBindingScope) return false
        sessionValue = null
        receipts.clear()
        return true
    }
}

private fun RelayDeviceSession.validated(): RelayDeviceSession = apply {
    require(
        assistantId.isSafeOpaque() &&
            assistantAlias.isSafeAlias() &&
            relayOrigin.isSafeRelayOrigin() &&
            bindingScope.isSafeOpaque() &&
            principalId.isSafeOpaque() &&
            accessToken.isSafeCredential() &&
            refreshToken.isSafeCredential() &&
            accessExpiresAt > 0 &&
            refreshExpiresAt > accessExpiresAt &&
            relayAckCursor >= 0 &&
            gatewayStreamId.isSafeOpaque() &&
            gatewayAckSeq >= 0,
    )
}

private fun RelayResponseReceipt.validated(): RelayResponseReceipt = apply {
    require(
        requestId.matches(Regex("^req_[A-Za-z0-9_-]{22}$")) &&
            deviceId.matches(Regex("^dev_[A-Za-z0-9_-]{22}$")) &&
            operation.isSafeOpaque() &&
            (resultJson == null) != (errorCode == null) &&
            (resultJson == null || resultJson.toByteArray(Charsets.UTF_8).size <= 4 * 1024 * 1024) &&
            (errorCode == null || errorCode.matches(Regex("^[A-Z][A-Z0-9_]{1,63}$"))) &&
            (retryAfterMillis == null || retryAfterMillis > 0) &&
            deliverySeq > 0,
    )
}

private fun String.isSafeOpaque(): Boolean =
    isNotBlank() && toByteArray(Charsets.UTF_8).size <= 512 && none(Char::isISOControl)

private fun String.isSafeAlias(): Boolean =
    isNotBlank() &&
        this == trim() &&
        toByteArray(Charsets.UTF_8).size <= 120 &&
        none(Char::isISOControl)

private fun String.isSafeCredential(): Boolean =
    length in 8..4_096 && all { it.code in 0x21..0x7e }

private fun String.isSafeRelayOrigin(): Boolean = runCatching {
    val uri = java.net.URI(this)
    uri.scheme == "https" &&
        !uri.host.isNullOrBlank() &&
        uri.userInfo == null &&
        uri.rawPath.orEmpty().isEmpty() &&
        uri.query == null &&
        uri.fragment == null &&
        uri.toASCIIString() == this
}.getOrDefault(false)
