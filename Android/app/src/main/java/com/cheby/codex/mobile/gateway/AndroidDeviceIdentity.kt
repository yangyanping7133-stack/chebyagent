package com.cheby.codex.mobile.gateway

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey
import java.security.PublicKey
import java.security.Signature
import java.security.interfaces.ECPublicKey
import java.security.spec.ECGenParameterSpec
import java.security.spec.X509EncodedKeySpec
import java.security.KeyFactory

/**
 * Owns the non-exportable Android Keystore identity used by Gateway PoP.
 *
 * Pairing is the only operation allowed to create the alias. Normal startup must load an existing
 * key so a missing, corrupt, or invalidated identity can never silently register a new signer for
 * an old session.
 */
internal object AndroidDeviceIdentity {
    @Synchronized
    fun createOrLoadForPairing(): DeviceProofKeyHandle = createOrReplaceInvalid(KEY_ALIAS)

    @Synchronized
    fun loadExisting(): DeviceProofKeyHandle = loadEntry(KEY_ALIAS, createIfMissing = false)

    /** Relay enrollment never reuses the signer registered with the superseded direct Gateway. */
    @Synchronized
    fun createOrLoadForRelayEnrollment(): DeviceProofKeyHandle =
        createOrReplaceInvalid(RELAY_KEY_ALIAS)

    @Synchronized
    fun loadExistingRelay(): DeviceProofKeyHandle =
        loadEntry(RELAY_KEY_ALIAS, createIfMissing = false)

    @Synchronized
    internal fun loadForDeviceTest(alias: String, createIfMissing: Boolean): DeviceProofKeyHandle {
        require(alias.startsWith(TEST_ALIAS_PREFIX))
        return loadEntry(alias, createIfMissing)
    }

    @Synchronized
    internal fun createOrLoadForDeviceTest(alias: String): DeviceProofKeyHandle {
        require(alias.startsWith(TEST_ALIAS_PREFIX))
        return createOrReplaceInvalid(alias)
    }

    private fun createOrReplaceInvalid(alias: String): DeviceProofKeyHandle {
        try {
            return requireOperational(loadEntry(alias, createIfMissing = true))
        } catch (_: DeviceIdentityUnavailableException) {
            deleteAlias(alias)
        }
        return requireOperational(loadEntry(alias, createIfMissing = true))
    }

    private fun loadEntry(alias: String, createIfMissing: Boolean): DeviceProofKeyHandle {
        try {
            val keyStore = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
            if (!keyStore.containsAlias(alias)) {
                if (!createIfMissing) throw DeviceIdentityUnavailableException()
                createKey(alias)
                keyStore.load(null)
            }
            val entry = keyStore.getEntry(alias, null) as? KeyStore.PrivateKeyEntry
                ?: throw DeviceIdentityUnavailableException()
            return AndroidKeyHandle(entry.privateKey, entry.certificate.publicKey)
        } catch (error: DeviceIdentityUnavailableException) {
            throw error
        } catch (error: Exception) {
            throw DeviceIdentityUnavailableException(error)
        }
    }

    private fun requireOperational(handle: DeviceProofKeyHandle): DeviceProofKeyHandle {
        val signature = try {
            EcdsaP256Der.normalizeLowS(handle.signSha256(SELF_TEST_MESSAGE))
        } catch (error: Exception) {
            throw DeviceIdentityUnavailableException(error)
        }
        if (!handle.verifySha256(SELF_TEST_MESSAGE, signature)) {
            throw DeviceIdentityUnavailableException()
        }
        return handle
    }

    private fun deleteAlias(alias: String) {
        try {
            KeyStore.getInstance(ANDROID_KEYSTORE).apply {
                load(null)
                if (containsAlias(alias)) deleteEntry(alias)
            }
        } catch (error: Exception) {
            throw DeviceIdentityUnavailableException(error)
        }
    }

    private fun createKey(alias: String) {
        val generator = KeyPairGenerator.getInstance(
            KeyProperties.KEY_ALGORITHM_EC,
            ANDROID_KEYSTORE,
        )
        generator.initialize(
            KeyGenParameterSpec.Builder(
                alias,
                KeyProperties.PURPOSE_SIGN or KeyProperties.PURPOSE_VERIFY,
            )
                .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
                .setDigests(KeyProperties.DIGEST_SHA256)
                .setUserAuthenticationRequired(false)
                .build(),
        )
        generator.generateKeyPair()
    }

    private class AndroidKeyHandle(
        private val privateKey: PrivateKey,
        private val publicKey: PublicKey,
    ) : DeviceProofKeyHandle {
        private val spki = validateAndCopySpki(publicKey)

        override fun publicKeySpkiDer(): ByteArray = spki.copyOf()

        override fun signSha256(message: ByteArray): ByteArray = try {
            Signature.getInstance(SIGNATURE_ALGORITHM).run {
                initSign(privateKey)
                update(message)
                sign()
            }
        } catch (error: Exception) {
            throw DeviceIdentityUnavailableException(error)
        }

        override fun verifySha256(message: ByteArray, signatureDer: ByteArray): Boolean = try {
            Signature.getInstance(SIGNATURE_ALGORITHM).run {
                initVerify(publicKey)
                update(message)
                verify(signatureDer)
            }
        } catch (error: Exception) {
            throw DeviceIdentityUnavailableException(error)
        }
    }

    private fun validateAndCopySpki(publicKey: PublicKey): ByteArray {
        val ecKey = publicKey as? ECPublicKey ?: throw DeviceIdentityUnavailableException()
        if (
            ecKey.params.curve.field.fieldSize != 256 ||
            ecKey.params.order != EcdsaP256Der.ORDER ||
            !publicKey.algorithm.equals("EC", ignoreCase = true)
        ) {
            throw DeviceIdentityUnavailableException()
        }
        val encoded = publicKey.encoded?.copyOf() ?: throw DeviceIdentityUnavailableException()
        val canonical = try {
            KeyFactory.getInstance("EC").generatePublic(X509EncodedKeySpec(encoded)).encoded
        } catch (error: Exception) {
            throw DeviceIdentityUnavailableException(error)
        }
        if (!encoded.contentEquals(canonical)) throw DeviceIdentityUnavailableException()
        return encoded
    }

    internal const val KEY_ALIAS = "chebycodex.gateway.device-identity.v1"
    internal const val RELAY_KEY_ALIAS = "chebycodex.relay.device-identity.v1"
    internal const val ANDROID_KEYSTORE = "AndroidKeyStore"
    internal const val TEST_ALIAS_PREFIX = "chebycodex.gateway.device-identity.test."
    private const val SIGNATURE_ALGORITHM = "SHA256withECDSA"
    private val SELF_TEST_MESSAGE = "cheby-device-identity-self-test-v1"
        .toByteArray(Charsets.US_ASCII)
}
