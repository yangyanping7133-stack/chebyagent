package com.cheby.codex.mobile.gateway

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import java.security.KeyStore
import java.util.Base64
import java.util.concurrent.CyclicBarrier
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import javax.crypto.KeyGenerator
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class AndroidDeviceIdentityInstrumentedTest {
    @Test
    fun explicitPairingReplacesAnUnusableExistingAlias() {
        val alias = "${AndroidDeviceIdentity.TEST_ALIAS_PREFIX}explicit-recovery"
        KeyStore.getInstance(AndroidDeviceIdentity.ANDROID_KEYSTORE).run {
            load(null)
            if (containsAlias(alias)) deleteEntry(alias)
        }
        KeyGenerator.getInstance("AES", AndroidDeviceIdentity.ANDROID_KEYSTORE).apply {
            init(
                KeyGenParameterSpec.Builder(
                    alias,
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
                )
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .build(),
            )
            generateKey()
        }

        val recovered = AndroidDeviceIdentity.createOrLoadForDeviceTest(alias)
        val proof = DeviceProofV1.sign(
            recovered,
            "POST",
            "/v1/pairings/exchange".toByteArray(),
            "{}".toByteArray(),
            null,
        )

        EcdsaP256Der.parseStrict(Base64.getDecoder().decode(proof.signature), requireLowS = true)
        val entry = KeyStore.getInstance(AndroidDeviceIdentity.ANDROID_KEYSTORE).run {
            load(null)
            getEntry(alias, null)
        }
        assertTrue(entry is KeyStore.PrivateKeyEntry)
    }

    @Test
    fun concurrentPairingLoadsConvergeOnOneIdentity() {
        val alias = "${AndroidDeviceIdentity.TEST_ALIAS_PREFIX}concurrent-pairing"
        KeyStore.getInstance(AndroidDeviceIdentity.ANDROID_KEYSTORE).run {
            load(null)
            if (containsAlias(alias)) deleteEntry(alias)
        }
        val workers = 8
        val barrier = CyclicBarrier(workers)
        val executor = Executors.newFixedThreadPool(workers)
        try {
            val spkis = (0 until workers).map {
                executor.submit<String> {
                    barrier.await(5, TimeUnit.SECONDS)
                    Base64.getEncoder().encodeToString(
                        AndroidDeviceIdentity.loadForDeviceTest(alias, createIfMissing = true)
                            .publicKeySpkiDer(),
                    )
                }
            }.map { it.get(15, TimeUnit.SECONDS) }

            assertEquals(1, spkis.toSet().size)
        } finally {
            executor.shutdownNow()
        }
    }

    @Test
    fun normalLoadNeverRecreatesAMissingIdentity() {
        val alias = "${AndroidDeviceIdentity.TEST_ALIAS_PREFIX}missing-must-stay-missing"
        KeyStore.getInstance(AndroidDeviceIdentity.ANDROID_KEYSTORE).run {
            load(null)
            if (containsAlias(alias)) deleteEntry(alias)
        }

        val error = runCatching {
            AndroidDeviceIdentity.loadForDeviceTest(alias, createIfMissing = false)
        }.exceptionOrNull()

        assertTrue(error is DeviceIdentityUnavailableException)
        assertFalse(
            KeyStore.getInstance(AndroidDeviceIdentity.ANDROID_KEYSTORE).run {
                load(null)
                containsAlias(alias)
            },
        )
    }

    @Test
    fun deviceIdentityGate() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val phase = InstrumentationRegistry.getArguments().getString("keyPhase") ?: "full"
        val handle = AndroidDeviceIdentity.loadForDeviceTest(TEST_ALIAS, createIfMissing = true)
        val spki = handle.publicKeySpkiDer()
        val preferences = instrumentation.targetContext.getSharedPreferences(
            TEST_PREFERENCES,
            Context.MODE_PRIVATE,
        )

        val entry = KeyStore.getInstance(AndroidDeviceIdentity.ANDROID_KEYSTORE).run {
            load(null)
            getEntry(TEST_ALIAS, null) as KeyStore.PrivateKeyEntry
        }
        assertNull("Android Keystore private key must be non-exportable", entry.privateKey.encoded)
        assertArrayEquals(entry.certificate.publicKey.encoded, spki)

        when (phase) {
            "record" -> {
                assertTrue(
                    preferences.edit()
                        .putString(KEY_RECORDED_SPKI, Base64.getEncoder().encodeToString(spki))
                        .commit(),
                )
            }
            "verify" -> {
                val recorded = requireNotNull(preferences.getString(KEY_RECORDED_SPKI, null))
                assertArrayEquals(Base64.getDecoder().decode(recorded), spki)
            }
            else -> runSignatureGate(handle)
        }
    }

    private fun runSignatureGate(handle: DeviceProofKeyHandle) {
        val nonces = HashSet<String>(SIGNATURE_ITERATIONS)
        repeat(SIGNATURE_ITERATIONS) { iteration ->
            val proof = DeviceProofV1.sign(
                key = handle,
                method = "POST",
                rawTarget = "/v1/device-gate?iteration=$iteration".toByteArray(),
                body = "{\"iteration\":$iteration}".toByteArray(),
                bearerToken = "device-gate-token",
            )
            assertTrue(nonces.add(proof.nonce))
            assertEquals(DeviceProofV1.NONCE_BYTES, Base64.getDecoder().decode(proof.nonce).size)
            EcdsaP256Der.parseStrict(
                Base64.getDecoder().decode(proof.signature),
                requireLowS = true,
            )
        }
        assertEquals(SIGNATURE_ITERATIONS, nonces.size)
    }

    private companion object {
        const val TEST_ALIAS = "${AndroidDeviceIdentity.TEST_ALIAS_PREFIX}juy-p0"
        const val TEST_PREFERENCES = "device-proof-instrumentation"
        const val KEY_RECORDED_SPKI = "recorded-spki"
        const val SIGNATURE_ITERATIONS = 1_000
    }
}
