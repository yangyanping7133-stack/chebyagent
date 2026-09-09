package com.cheby.codex.mobile.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import java.security.MessageDigest
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put

data class PairingBootstrap(
    val baseUrl: String,
    val pairingSecret: String,
) {
    override fun toString(): String = "PairingBootstrap(redacted)"
}

data class RemoteGatewaySession(
    val baseUrl: String,
    val accessToken: String,
    val refreshToken: String,
    val deviceId: String,
    val expiresAt: String,
    val refreshExpiresAt: String,
    val streamId: String,
    val lastAckSeq: Long = 0,
    val sessionScope: String = legacySessionScope(baseUrl, deviceId),
) {
    override fun toString(): String = "RemoteGatewaySession(redacted)"
}

data class PendingApprovalCommand(
    val sessionScope: String,
    val actionRef: String,
    val approvalId: String,
    val threadId: String,
    val decision: String,
    val actionToken: String,
    val expiresAtMillis: Long,
    val summary: String,
    val reason: String,
    val retryNotBeforeMillis: Long = 0,
) {
    override fun toString(): String = "PendingApprovalCommand(redacted)"
}

interface SecureGatewayConfigStore {
    fun loadPairing(): PairingBootstrap?
    fun savePairing(pairing: PairingBootstrap)
    fun clearPairing()
    fun loadSession(): RemoteGatewaySession?
    fun saveSession(session: RemoteGatewaySession): Boolean
    fun saveSessionIfScope(
        expectedSessionScope: String,
        session: RemoteGatewaySession,
    ): Boolean
    fun updateCursor(expectedSessionScope: String, streamId: String, seq: Long): Boolean
    fun loadApprovalCommands(): List<PendingApprovalCommand>
    fun replaceApprovalCommandsForScope(
        expectedSessionScope: String,
        commands: List<PendingApprovalCommand>,
    ): Boolean
    fun replaceApprovalCommandsForApproval(
        expectedSessionScope: String,
        approvalId: String,
        expectedActionRefs: Set<String>,
        commands: List<PendingApprovalCommand>,
    ): Boolean
    fun removeApprovalCommands(
        expectedSessionScope: String,
        approvalId: String,
        expectedActionRefs: Set<String>,
    ): Boolean
    fun clearSession()
    fun clearSessionIfScope(expectedSessionScope: String): Boolean
}

/**
 * Stores the complete pairing/session payload as AES-GCM ciphertext. The AES key
 * is non-exportable and generated inside AndroidKeyStore; SharedPreferences never
 * receives a bearer token or pairing secret in plaintext.
 */
class AndroidKeystoreGatewayConfigStore(context: Context) : SecureGatewayConfigStore {
    private val preferences = context.getSharedPreferences(PREFERENCES_NAME, Context.MODE_PRIVATE)
    private val json = Json { ignoreUnknownKeys = false }

    @Synchronized
    override fun loadPairing(): PairingBootstrap? = decrypt(KEY_PAIRING)?.let { raw ->
        runCatching {
            val body = json.parseToJsonElement(raw).jsonObject
            PairingBootstrap(
                baseUrl = body.requiredString("baseUrl"),
                pairingSecret = body.requiredString("pairingSecret"),
            )
        }.getOrNull()
    }

    @Synchronized
    override fun savePairing(pairing: PairingBootstrap) {
        require(pairing.baseUrl.isNotBlank() && pairing.pairingSecret.isNotBlank())
        val body = buildJsonObject {
            put("baseUrl", pairing.baseUrl)
            put("pairingSecret", pairing.pairingSecret)
        }
        preferences.edit().putString(KEY_PAIRING, encrypt(KEY_PAIRING, body.toString())).apply()
    }

    @Synchronized
    override fun clearPairing() {
        preferences.edit().remove(KEY_PAIRING).apply()
    }

    @Synchronized
    override fun loadSession(): RemoteGatewaySession? = decrypt(KEY_SESSION)?.let { raw ->
        runCatching {
            val body = json.parseToJsonElement(raw).jsonObject
            RemoteGatewaySession(
                baseUrl = body.requiredString("baseUrl"),
                accessToken = body.requiredString("accessToken"),
                refreshToken = body.requiredString("refreshToken"),
                deviceId = body.requiredString("deviceId"),
                expiresAt = body.requiredString("expiresAt"),
                refreshExpiresAt = body.requiredString("refreshExpiresAt"),
                streamId = body.requiredString("streamId"),
                lastAckSeq = body["lastAckSeq"]?.jsonPrimitive?.longOrNull ?: 0,
                sessionScope = body["sessionScope"]?.jsonPrimitive?.content
                    ?: legacySessionScope(body.requiredString("baseUrl"), body.requiredString("deviceId")),
            )
        }.getOrNull()
    }

    @Synchronized
    override fun saveSession(session: RemoteGatewaySession): Boolean {
        require(
            session.baseUrl.isNotBlank() &&
                session.accessToken.isNotBlank() &&
                session.refreshToken.isNotBlank() &&
                session.deviceId.isNotBlank() &&
                session.streamId.isNotBlank() &&
                session.sessionScope.isNotBlank() &&
                session.lastAckSeq >= 0,
        )
        val body = buildJsonObject {
            put("baseUrl", session.baseUrl)
            put("accessToken", session.accessToken)
            put("refreshToken", session.refreshToken)
            put("deviceId", session.deviceId)
            put("expiresAt", session.expiresAt)
            put("refreshExpiresAt", session.refreshExpiresAt)
            put("streamId", session.streamId)
            put("lastAckSeq", session.lastAckSeq)
            put("sessionScope", session.sessionScope)
        }
        return preferences.edit().putString(KEY_SESSION, encrypt(KEY_SESSION, body.toString())).commit()
    }

    @Synchronized
    override fun saveSessionIfScope(
        expectedSessionScope: String,
        session: RemoteGatewaySession,
    ): Boolean {
        if (loadSession()?.scopeKey() != expectedSessionScope) return false
        if (session.scopeKey() != expectedSessionScope) return false
        return saveSession(session)
    }

    @Synchronized
    override fun updateCursor(expectedSessionScope: String, streamId: String, seq: Long): Boolean {
        if (streamId.isBlank() || seq < 0) return false
        val current = loadSession() ?: return false
        if (current.scopeKey() != expectedSessionScope) return false
        val updated = if (current.streamId == streamId) {
            current.copy(lastAckSeq = maxOf(current.lastAckSeq, seq))
        } else {
            current.copy(streamId = streamId, lastAckSeq = seq)
        }
        return saveSession(updated)
    }

    @Synchronized
    override fun loadApprovalCommands(): List<PendingApprovalCommand> =
        decrypt(KEY_APPROVALS)?.let { raw ->
            runCatching {
                json.parseToJsonElement(raw).jsonArray.map { element ->
                    val body = element.jsonObject
                    PendingApprovalCommand(
                        actionRef = body.requiredString("actionRef"),
                        sessionScope = body.requiredString("sessionScope"),
                        approvalId = body.requiredString("approvalId"),
                        threadId = body.requiredString("threadId"),
                        decision = body.requiredString("decision"),
                        actionToken = body.requiredString("actionToken"),
                        expiresAtMillis = body.getValue("expiresAtMillis").jsonPrimitive.longOrNull
                            ?: error("Missing approval expiry"),
                        summary = body.requiredString("summary"),
                        reason = body.requiredString("reason"),
                        retryNotBeforeMillis = body["retryNotBeforeMillis"]?.jsonPrimitive?.longOrNull ?: 0,
                    )
                }
            }.getOrNull()
        }.orEmpty()

    @Synchronized
    private fun persistApprovalCommands(commands: List<PendingApprovalCommand>): Boolean = runCatching {
        val body = buildJsonArray {
            commands.forEach { command ->
                add(buildJsonObject {
                    put("actionRef", command.actionRef)
                    put("sessionScope", command.sessionScope)
                    put("approvalId", command.approvalId)
                    put("threadId", command.threadId)
                    put("decision", command.decision)
                    put("actionToken", command.actionToken)
                    put("expiresAtMillis", command.expiresAtMillis)
                    put("summary", command.summary)
                    put("reason", command.reason)
                    put("retryNotBeforeMillis", command.retryNotBeforeMillis)
                })
            }
        }
        preferences.edit().putString(KEY_APPROVALS, encrypt(KEY_APPROVALS, body.toString())).commit()
    }.getOrDefault(false)

    @Synchronized
    override fun replaceApprovalCommandsForScope(
        expectedSessionScope: String,
        commands: List<PendingApprovalCommand>,
    ): Boolean {
        if (loadSession()?.scopeKey() != expectedSessionScope) return false
        if (commands.any { it.sessionScope != expectedSessionScope }) return false
        return persistApprovalCommands(commands)
    }

    @Synchronized
    override fun replaceApprovalCommandsForApproval(
        expectedSessionScope: String,
        approvalId: String,
        expectedActionRefs: Set<String>,
        commands: List<PendingApprovalCommand>,
    ): Boolean {
        if (loadSession()?.scopeKey() != expectedSessionScope) return false
        if (commands.any {
                it.sessionScope != expectedSessionScope || it.approvalId != approvalId
            }
        ) return false
        val stored = loadApprovalCommands()
        val actualActionRefs = stored.asSequence()
            .filter { it.sessionScope == expectedSessionScope && it.approvalId == approvalId }
            .mapTo(mutableSetOf(), PendingApprovalCommand::actionRef)
        if (actualActionRefs != expectedActionRefs) return false
        return persistApprovalCommands(
            stored.filterNot {
                it.sessionScope == expectedSessionScope && it.approvalId == approvalId
            } + commands,
        )
    }

    @Synchronized
    override fun removeApprovalCommands(
        expectedSessionScope: String,
        approvalId: String,
        expectedActionRefs: Set<String>,
    ): Boolean {
        if (loadSession()?.scopeKey() != expectedSessionScope) return false
        val stored = loadApprovalCommands()
        val actualActionRefs = stored.asSequence()
            .filter { it.sessionScope == expectedSessionScope && it.approvalId == approvalId }
            .mapTo(mutableSetOf(), PendingApprovalCommand::actionRef)
        if (actualActionRefs != expectedActionRefs) return false
        return persistApprovalCommands(
            stored.filterNot {
                it.sessionScope == expectedSessionScope &&
                    it.approvalId == approvalId && it.actionRef in expectedActionRefs
            },
        )
    }

    @Synchronized
    override fun clearSession() {
        preferences.edit().remove(KEY_SESSION).remove(KEY_APPROVALS).apply()
    }

    @Synchronized
    override fun clearSessionIfScope(expectedSessionScope: String): Boolean {
        if (loadSession()?.scopeKey() != expectedSessionScope) return false
        return preferences.edit().remove(KEY_SESSION).remove(KEY_APPROVALS).commit()
    }

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

    private companion object {
        const val PREFERENCES_NAME = "chebycodex_secure_gateway_v1"
        const val KEY_PAIRING = "pairing"
        const val KEY_SESSION = "session"
        const val KEY_APPROVALS = "pending_approvals"
        const val KEY_ALIAS = "chebycodex.gateway.credentials.v1"
        const val FORMAT_VERSION = "v1"
        const val ANDROID_KEYSTORE = "AndroidKeyStore"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
    }
}

private fun legacySessionScope(baseUrl: String, deviceId: String): String {
    val raw = "${baseUrl.trimEnd('/').lowercase()}|$deviceId"
    return MessageDigest.getInstance("SHA-256")
        .digest(raw.toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it) }
}

fun RemoteGatewaySession.scopeKey(): String = sessionScope

class InMemorySecureGatewayConfigStore(
    pairing: PairingBootstrap? = null,
    session: RemoteGatewaySession? = null,
    private val allowSessionWrites: Boolean = true,
) : SecureGatewayConfigStore {
    private var pairingValue = pairing
    private var sessionValue = session
    private var approvalCommands = emptyList<PendingApprovalCommand>()

    @Synchronized
    override fun loadPairing(): PairingBootstrap? = pairingValue
    @Synchronized
    override fun savePairing(pairing: PairingBootstrap) { pairingValue = pairing }
    @Synchronized
    override fun clearPairing() { pairingValue = null }
    @Synchronized
    override fun loadSession(): RemoteGatewaySession? = sessionValue
    @Synchronized
    override fun saveSession(session: RemoteGatewaySession): Boolean {
        if (!allowSessionWrites) return false
        sessionValue = session
        return true
    }
    @Synchronized
    override fun saveSessionIfScope(
        expectedSessionScope: String,
        session: RemoteGatewaySession,
    ): Boolean {
        if (sessionValue?.scopeKey() != expectedSessionScope) return false
        if (session.scopeKey() != expectedSessionScope) return false
        return saveSession(session)
    }
    @Synchronized
    override fun updateCursor(expectedSessionScope: String, streamId: String, seq: Long): Boolean {
        val current = sessionValue ?: return false
        if (current.scopeKey() != expectedSessionScope) return false
        sessionValue = current.let {
            if (it.streamId == streamId) it.copy(lastAckSeq = maxOf(it.lastAckSeq, seq))
            else it.copy(streamId = streamId, lastAckSeq = seq)
        }
        return true
    }
    @Synchronized
    override fun loadApprovalCommands(): List<PendingApprovalCommand> = approvalCommands
    @Synchronized
    override fun replaceApprovalCommandsForScope(
        expectedSessionScope: String,
        commands: List<PendingApprovalCommand>,
    ): Boolean {
        if (sessionValue?.scopeKey() != expectedSessionScope) return false
        if (commands.any { it.sessionScope != expectedSessionScope }) return false
        approvalCommands = commands.toList()
        return true
    }
    @Synchronized
    override fun replaceApprovalCommandsForApproval(
        expectedSessionScope: String,
        approvalId: String,
        expectedActionRefs: Set<String>,
        commands: List<PendingApprovalCommand>,
    ): Boolean {
        if (sessionValue?.scopeKey() != expectedSessionScope) return false
        if (commands.any {
                it.sessionScope != expectedSessionScope || it.approvalId != approvalId
            }
        ) return false
        val actualActionRefs = approvalCommands.asSequence()
            .filter { it.sessionScope == expectedSessionScope && it.approvalId == approvalId }
            .mapTo(mutableSetOf(), PendingApprovalCommand::actionRef)
        if (actualActionRefs != expectedActionRefs) return false
        approvalCommands = approvalCommands.filterNot {
            it.sessionScope == expectedSessionScope && it.approvalId == approvalId
        } + commands
        return true
    }
    @Synchronized
    override fun removeApprovalCommands(
        expectedSessionScope: String,
        approvalId: String,
        expectedActionRefs: Set<String>,
    ): Boolean {
        if (sessionValue?.scopeKey() != expectedSessionScope) return false
        val actualActionRefs = approvalCommands.asSequence()
            .filter { it.sessionScope == expectedSessionScope && it.approvalId == approvalId }
            .mapTo(mutableSetOf(), PendingApprovalCommand::actionRef)
        if (actualActionRefs != expectedActionRefs) return false
        approvalCommands = approvalCommands.filterNot {
            it.sessionScope == expectedSessionScope &&
                it.approvalId == approvalId && it.actionRef in expectedActionRefs
        }
        return true
    }
    @Synchronized
    override fun clearSession() {
        sessionValue = null
        approvalCommands = emptyList()
    }
    @Synchronized
    override fun clearSessionIfScope(expectedSessionScope: String): Boolean {
        if (sessionValue?.scopeKey() != expectedSessionScope) return false
        sessionValue = null
        approvalCommands = emptyList()
        return true
    }
}
