package com.cheby.codex.mobile.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

internal class LocalCapabilityToken private constructor(
    val bearer: String,
) {
    val sha256: String
        get() = MessageDigest.getInstance("SHA-256")
            .digest(bearer.toByteArray(Charsets.US_ASCII))
            .joinToString("") { byte -> "%02x".format(byte) }

    override fun toString(): String = "LocalCapabilityToken(redacted)"

    companion object {
        fun fromBearer(value: String): LocalCapabilityToken {
            require(value.length in 32..256 && value.all { it.code in 0x21..0x7e })
            return LocalCapabilityToken(value)
        }
    }
}

/** Stores the localhost app-server bearer with a non-exportable AndroidKeyStore key. */
internal class LocalCapabilityTokenStore(context: Context) {
    private val preferences = context.getSharedPreferences(PREFERENCES_NAME, Context.MODE_PRIVATE)

    fun loadOrCreate(): LocalCapabilityToken = synchronized(PROCESS_LOCK) {
        decrypt()?.let { stored ->
            runCatching { LocalCapabilityToken.fromBearer(stored) }.getOrNull()?.let { return it }
        }
        val bytes = ByteArray(TOKEN_BYTES).also(SecureRandom()::nextBytes)
        val bearer = Base64.encodeToString(
            bytes,
            Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING,
        )
        val token = LocalCapabilityToken.fromBearer(bearer)
        check(preferences.edit().putString(KEY_TOKEN, encrypt(bearer)).commit())
        token
    }

    private fun encrypt(plaintext: String): String {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, getOrCreateSecretKey())
        cipher.updateAAD(KEY_TOKEN.toByteArray(Charsets.UTF_8))
        val ciphertext = cipher.doFinal(plaintext.toByteArray(Charsets.US_ASCII))
        return listOf(
            FORMAT_VERSION,
            Base64.encodeToString(cipher.iv, Base64.NO_WRAP),
            Base64.encodeToString(ciphertext, Base64.NO_WRAP),
        ).joinToString(".")
    }

    private fun decrypt(): String? {
        val encoded = preferences.getString(KEY_TOKEN, null) ?: return null
        return runCatching {
            val parts = encoded.split('.')
            require(parts.size == 3 && parts[0] == FORMAT_VERSION)
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(
                Cipher.DECRYPT_MODE,
                getOrCreateSecretKey(),
                GCMParameterSpec(128, Base64.decode(parts[1], Base64.NO_WRAP)),
            )
            cipher.updateAAD(KEY_TOKEN.toByteArray(Charsets.UTF_8))
            cipher.doFinal(Base64.decode(parts[2], Base64.NO_WRAP)).toString(Charsets.US_ASCII)
        }.getOrElse {
            // Keep recovery atomic with loadOrCreate(). An asynchronous removal could otherwise
            // erase a newly encrypted replacement written by another store instance.
            check(preferences.edit().remove(KEY_TOKEN).commit())
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

    private companion object {
        val PROCESS_LOCK = Any()
        const val PREFERENCES_NAME = "chebycodex_local_capability_v1"
        const val KEY_TOKEN = "app_server_bearer"
        const val KEY_ALIAS = "chebycodex.local.appserver.capability.v1"
        const val FORMAT_VERSION = "v1"
        const val TOKEN_BYTES = 48
        const val ANDROID_KEYSTORE = "AndroidKeyStore"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
    }
}
