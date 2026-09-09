package com.cheby.codex.mobile.gateway

import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.Signature
import java.security.spec.ECGenParameterSpec

internal class TestDeviceProofKey private constructor(
    private val keyPair: KeyPair,
) : DeviceProofKeyHandle {
    override fun publicKeySpkiDer(): ByteArray = keyPair.public.encoded.copyOf()

    override fun signSha256(message: ByteArray): ByteArray =
        Signature.getInstance("SHA256withECDSA").run {
            initSign(keyPair.private)
            update(message)
            sign()
        }

    override fun verifySha256(message: ByteArray, signatureDer: ByteArray): Boolean =
        Signature.getInstance("SHA256withECDSA").run {
            initVerify(keyPair.public)
            update(message)
            verify(signatureDer)
        }

    companion object {
        fun generate(): TestDeviceProofKey {
            val generator = KeyPairGenerator.getInstance("EC")
            generator.initialize(ECGenParameterSpec("secp256r1"))
            return TestDeviceProofKey(generator.generateKeyPair())
        }
    }
}
